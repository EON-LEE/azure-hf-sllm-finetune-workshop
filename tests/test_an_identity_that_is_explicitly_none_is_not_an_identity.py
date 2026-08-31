"""Findings from the adversarial audit of the `ensure_compute` try-narrowing.

The narrowing itself holds: the repair PUT is out of the `try`, so a 404 from
it is no longer read as "the compute does not exist" and the create path can no
longer PUT environment defaults over an operator's cluster. That is verified by
tests/test_a_failed_repair_is_not_read_as_a_missing_cluster.py and reproduced
independently in this round's audit.

What the narrowing did not touch is the GUARD that decides whether the repair
runs at all, and the handler that decides which grants are attempted. Both
conflate two different facts, which is this round's invariant:

    could not look  !=  looked, saw nothing
    explicitly none !=  present

Every client below is a FAKE at the azure-ai-ml SDK boundary and every ARM body
is a FAKE I wrote; there is no Azure access and no network in this file. What
is NOT faked is the deserialisation: the entity handed to `ensure_compute` is
built by the installed SDK's own `Compute._from_rest_object`, which is exactly
what `MLClient.compute.get` returns (verified: `ComputeOperations.get` is
`Compute._from_rest_object(self._operation.get(...))`).

The three failing cases arrived here as `xfail(strict=True)` audit pins. Round 7
CLOSED all three, so they are plain tests now -- a strict xfail that starts
passing is reported as a failure, which is the mechanism working. Two of them
assert exactly what the pin asserted; `test_a_storage_read_that_failed_...` had
to be rewritten, and its docstring says why and what changed.
"""

from __future__ import annotations

import azure.ai.ml._restclient.v2022_10_01_preview.models as rest_models
import pytest
from azure.ai.ml.entities import Compute
from azure.core.exceptions import ClientAuthenticationError
from msrest import Deserializer

from ffsft import azure_ml
from ffsft.azure_ml import AzureTarget
from ffsft.deploy import identity as identity_module

TARGET = AzureTarget("sub", "rg", "ws")

#: The operator's hand-built cluster, as ARM would describe it.
OPERATOR_PROPERTIES = {
    "vmSize": "Standard_NC96ads_A100_v4",
    "vmPriority": "Dedicated",
    "scaleSettings": {"maxNodeCount": 8, "minNodeCount": 2},
}

_DESERIALIZER = Deserializer(
    {name: value for name, value in rest_models.__dict__.items() if isinstance(value, type)}
)


def _as_the_sdk_returns_it(identity_block: dict | None):
    """Deserialise a compute the way `client.compute.get` does.

    `identity_block=None` omits the key entirely. `{"type": "None"}` is the
    other spelling of the same fact, and `ManagedServiceIdentityType` in the
    installed SDK declares it legal:
    ['None', 'SystemAssigned', 'UserAssigned', 'SystemAssigned,UserAssigned'].
    """
    body: dict = {
        "id": (
            "/subscriptions/sub/resourceGroups/rg/providers/"
            "Microsoft.MachineLearningServices/workspaces/ws/computes/gpu-a100-lp"
        ),
        "name": TARGET.compute_name,
        "location": "koreacentral",
        "properties": {
            "computeType": "AmlCompute",
            "provisioningState": "Succeeded",
            "properties": OPERATOR_PROPERTIES,
        },
    }
    if identity_block is not None:
        body["identity"] = identity_block
    return Compute._from_rest_object(_DESERIALIZER("ComputeResource", body))


class _Poller:
    def __init__(self, value):
        self._value = value

    def result(self):
        return self._value


#: The principal this fake hands back once a system-assigned identity is
#: enabled. MODELLED, not measured: this repo has no Azure access and never
#: watched ARM answer a compute PUT. Stating it here keeps the assumption out of
#: the assertions -- what the tests below pin is our side of the boundary, that
#: a cluster whose identity HAS a principal gets both data-plane grants
#: attempted. A fake that left `principal_id` at `None` forever would model an
#: identity that can never be granted anything, which is not the case under
#: test.
_ASSIGNED_PRINCIPAL = "99999999-8888-7777-6666-555555555555"


class _FakeCompute:
    def __init__(self, existing, get_raises=None, principal_on_put=_ASSIGNED_PRINCIPAL):
        self.existing = existing
        self.get_raises = get_raises
        self.principal_on_put = principal_on_put
        self.puts: list[tuple] = []

    def get(self, name):
        if self.get_raises is not None:
            raise self.get_raises
        return self.existing

    def begin_create_or_update(self, compute):
        self.puts.append(
            (
                type(compute).__name__,
                getattr(compute, "size", None),
                getattr(compute, "max_instances", None),
                getattr(compute, "tier", None),
            )
        )
        identity = getattr(compute, "identity", None)
        if identity is not None and getattr(identity, "principal_id", None) is None:
            identity.principal_id = self.principal_on_put
        # A PUT is a write: the next `get` must see what was written, or the
        # fake models a workspace that forgets.
        self.existing = compute
        return _Poller(compute)


class _FakeWorkspaces:
    def __init__(self, outcome):
        self.outcome = outcome

    def get(self, name):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class _Workspace:
    def __init__(self, storage_account):
        self.storage_account = storage_account


def _wire(monkeypatch, existing, *, workspace, get_raises=None):
    """A whole client in-process, and a recorder in place of the ARM writes."""
    compute = _FakeCompute(existing, get_raises)
    grants: list[tuple[str, str]] = []
    monkeypatch.setattr(
        azure_ml,
        "get_ml_client",
        lambda target: type(
            "_Client", (), {"compute": compute, "workspaces": _FakeWorkspaces(workspace)}
        )(),
    )

    def _record(scope, principal, role, **kwargs):
        grants.append((scope.rsplit("/", 1)[-1], role))
        return identity_module.GrantResult(granted=True)

    monkeypatch.setattr(identity_module, "ensure_role", _record)
    monkeypatch.setattr(
        identity_module,
        "acr_id_for_image",
        lambda *args, **kwargs: (
            "/subscriptions/sub/resourceGroups/rg/providers/"
            "Microsoft.ContainerRegistry/registries/acrffsftkc"
        ),
    )
    return compute, grants


# --- the fact the SDK can spell two ways -------------------------------------


def test_the_two_wire_spellings_of_no_identity_deserialise_differently():
    """Not a defect on its own -- the evidence the next test rests on.

    ARM may answer with the identity key omitted, or with `{"type": "None"}`.
    Both mean "this cluster has no managed identity". The SDK turns the first
    into `None` and the second into an `IdentityConfiguration` object, because
    `AmlCompute._load_from_rest` guards on `if rest_obj.identity` and the REST
    model is truthy whatever its `type` says.
    """
    omitted = _as_the_sdk_returns_it(None)
    explicit = _as_the_sdk_returns_it({"type": "None"})

    assert omitted.identity is None
    assert explicit.identity is not None
    assert explicit.identity.type == "none"
    assert explicit.identity.principal_id is None


def test_a_cluster_whose_wire_identity_is_none_is_repaired_like_one_with_no_identity(
    monkeypatch,
):
    """Round 7 CLOSED this. The branch this whole round is about, now entered.

    `ensure_compute` repairs a cluster that has no identity because a node with
    no identity cannot authenticate to the datastore once the workspace storage
    disallows shared keys. The guard used to be `existing.identity is not None`,
    which `IdentityConfiguration(type='none')` satisfies, so when ARM spelled
    the absence as `{"type": "None"}` no PUT was made, no grant was attempted,
    and the cluster name was returned -- which `scripts/provision_azure.py:121`
    prints as the provisioning result. `_has_managed_identity` now reads the
    `type`, so both spellings of "no identity" reach the repair.
    """
    compute, grants = _wire(
        monkeypatch,
        _as_the_sdk_returns_it({"type": "None"}),
        workspace=_Workspace("/subscriptions/sub/resourceGroups/rg/storageAccounts/stffsft"),
    )

    returned = azure_ml.ensure_compute(TARGET)

    assert returned == TARGET.compute_name
    assert compute.puts, (
        "the identity repair never ran: ensure_compute read "
        "IdentityConfiguration(type='none') as an identity that is present"
    )
    assert grants, "no data-plane role was even attempted on an identity-less cluster"


# --- could not look, reported as looked and saw nothing ----------------------


def test_a_storage_read_that_failed_is_not_a_workspace_that_has_no_storage(monkeypatch):
    """Round 7 CLOSED this, and the assertion MOVED. Read this before trusting it.

    The pin compared the two runs' GRANT LISTS and demanded they differ. They
    still do not, on purpose: an unresolvable scope is dropped either way
    because there is no scope to grant on, and dropping the ACR grant as well
    -- the one that CAN be made -- to manufacture a difference would cost a
    working `AcrPull` to make a test go green. The reproduced defect was never
    the grant list anyway; it was that the caller could not tell the two apart:

        a storage account that could not be READ produced the same grants as a
        workspace that HAS none: [('acrffsftkc', 'AcrPull')]

    So the difference now lives where the caller actually looks. The failed read
    comes back as `GRANTS UNVERIFIED`, the genuine absence comes back as the
    bare cluster name, and `scripts/provision_azure.py:121` prints whichever it
    got. The grant lists are asserted identical below so the over-correction
    stays visible rather than being quietly available later.
    """
    healthy = _as_the_sdk_returns_it(
        {"type": "SystemAssigned", "principalId": "11111111-2222-3333-4444-555555555555"}
    )

    _, after_failed_read = _wire(
        monkeypatch,
        healthy,
        workspace=RuntimeError("HTTPSConnectionPool(host='management.azure.com'): timed out"),
    )
    could_not_read = azure_ml.ensure_compute(TARGET)

    _, after_genuine_absence = _wire(monkeypatch, healthy, workspace=_Workspace(None))
    has_none = azure_ml.ensure_compute(TARGET)

    assert after_failed_read == after_genuine_absence == [("acrffsftkc", "AcrPull")]
    assert could_not_read != has_none, (
        "a storage account that could not be READ was reported exactly as a "
        f"workspace that HAS none: {could_not_read!r}"
    )
    assert could_not_read.name == TARGET.compute_name
    assert "GRANTS UNVERIFIED" in could_not_read
    assert "workspace storage account" in could_not_read
    # The over-correction guard: a workspace that HAS no storage account was
    # measured, not missed, so it is still a plain green cluster name.
    assert has_none == TARGET.compute_name
    assert has_none.unverified == ()


def test_an_identity_read_that_failed_is_not_a_cluster_that_needed_no_grants(monkeypatch):
    """Round 7 CLOSED this. `grant_compute_data_roles` returning was read as success.

    It logged "could not read the identity of %s" -- accurate -- and then
    returned normally, so `ensure_compute` returned the cluster name and the
    operator saw the same green line as a run that granted both roles.
    Reproduced before the fix: returned 'gpu-a100-lp', grants attempted [].
    It now returns a `GrantsOutcome` whose `unverified` names the failed read,
    and `ensure_compute` carries that into the string the caller prints. It
    still does not raise: an unreadable role assignment must not stop a cluster
    from existing.
    """
    healthy = _as_the_sdk_returns_it(
        {"type": "SystemAssigned", "principalId": "11111111-2222-3333-4444-555555555555"}
    )
    grants: list[tuple[str, str]] = []

    def _record(scope, principal, role, **kwargs):
        grants.append((scope.rsplit("/", 1)[-1], role))
        return identity_module.GrantResult(granted=True)

    monkeypatch.setattr(identity_module, "ensure_role", _record)
    monkeypatch.setattr(identity_module, "acr_id_for_image", lambda *a, **k: "/sub/acr")

    def _wire_pair(inner_failure):
        clients = iter(
            [_FakeCompute(healthy), _FakeCompute(healthy, inner_failure)]
            if inner_failure is not None
            else [_FakeCompute(healthy), _FakeCompute(healthy)]
        )
        monkeypatch.setattr(
            azure_ml,
            "get_ml_client",
            lambda target: type(
                "_Client",
                (),
                {
                    "compute": next(clients),
                    "workspaces": _FakeWorkspaces(_Workspace("/sub/sa")),
                },
            )(),
        )

    _wire_pair(ClientAuthenticationError("AADSTS700082: refresh token expired"))
    grants.clear()
    returned = azure_ml.ensure_compute(TARGET)
    after_failed_read = list(grants)

    assert returned != TARGET.compute_name, (
        "a run whose identity read FAILED returned exactly what a fully successful "
        f"run returns ({returned!r}), having attempted these grants: {after_failed_read}. "
        "The caller has no way to tell the two apart; the only difference is one "
        "WARNING line in a log that azure-ai-ml also writes to."
    )


# --- a name that exists, but is not an AmlCompute ----------------------------


@pytest.mark.parametrize("compute_type", ["Kubernetes", "SynapseSpark"])
def test_a_name_held_by_another_kind_of_compute_is_written_to_anyway(monkeypatch, compute_type):
    """Characterisation, so the write is on the record rather than a surprise.

    `client.compute.get` answers for every compute kind in the workspace, not
    just AmlCompute. A Kubernetes attach or a Synapse Spark pool sharing the
    configured name has `identity is None`, so `ensure_compute` PUTs a
    system-assigned identity onto the operator's resource and returns its name
    as "the GPU cluster" -- which the training backend then submits jobs to.
    """
    body = {
        "id": "/subscriptions/sub/.../computes/gpu-a100-lp",
        "name": TARGET.compute_name,
        "location": "koreacentral",
        "properties": {
            "computeType": compute_type,
            "provisioningState": "Succeeded",
            "properties": {},
        },
    }
    other_kind = Compute._from_rest_object(_DESERIALIZER("ComputeResource", body))
    compute, _ = _wire(monkeypatch, other_kind, workspace=_Workspace("/sub/sa"))

    assert azure_ml.ensure_compute(TARGET) == TARGET.compute_name
    assert len(compute.puts) == 1, (
        f"a {compute_type} compute named {TARGET.compute_name} was written to by "
        "ensure_compute, which only ever means to touch an AmlCompute"
    )
