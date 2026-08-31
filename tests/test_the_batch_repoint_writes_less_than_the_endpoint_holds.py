"""Findings from the adversarial audit of the S76 `deploy_batch` guard.

The guard itself holds. `ensure_batch_endpoint` reads before it writes, only
`ResourceNotFoundError` counts as absent, and the endpoint PUT no longer runs
ahead of the deployment create -- independently reproduced this round against
the patched function, and `tests/test_batch_deploy_does_not_clobber_an_operator_owned_endpoint.py`
pins all three.

What the fix did not answer is what the surviving PUT at
`src/ffsft/deploy/batch.py:195` carries. That PUT was justified by the
read-back-and-mutate pattern `deploy/traffic.py:78-84` uses, and the pattern
does not transfer: read-back-and-mutate is only non-destructive when the entity
round-trips everything the resource holds, and the batch entity does not.
Measured against the installed azure-ai-ml 1.34.1:

    BatchEndpoint._from_rest_object(...)   drops `identity` and `kind`
    BatchEndpoint._to_rest_batch_endpoint  sends neither

while the online twin, the one traffic.py was written against, carries both --
and goes further, defaulting `identity` to SystemAssigned rather than omitting
it, which is the SDK defending against precisely this on the path that got the
attention. See `test_the_online_endpoint_round_trip_keeps_what_the_batch_one_drops`.

The second finding is one function lower. `deploy_batch` PUTs a
`ModelBatchDeployment` built fresh at `src/ffsft/deploy/batch.py:154-169` at the
hardcoded name `"default"` (`batch.py:31`) with no read of
`client.batch_deployments` anywhere in the module. It is the original defect's
shape -- fresh entity, ungated PUT -- moved from the endpoint to the deployment
underneath it. S76.7 reports it as out of scope because "`deploy_online` does
the same with its `--deployment` name"; the two are not equivalent, and
`test_the_batch_command_offers_no_deployment_name_flag_the_way_deploy_online_does`
is the difference: `deploy-online` takes `--deployment`, so the operator names
the resource they are about to replace. `deploy-batch` has no such flag.

EVERY CLIENT BELOW IS A FAKE. There is no Azure access in this repo and no
network in this file; the endpoint name, the tags, the identity resource id and
the scoring URI are values invented for the test. What is NOT invented is the
serialisation on both sides of the wire: bodies are built by the installed
SDK's own `_to_rest_batch_endpoint` and read back by its `_from_rest_object`,
which is exactly what `BatchEndpointOperations.get` /
`.begin_create_or_update` do (verified: `_batch_endpoint_operations.py:170,235`).

The one rule these fakes invent is the same one the fix and its own test module
invent -- ARM's endpoint PUT is create-or-REPLACE, so what the resource holds
afterwards is what the body carried. Nothing here is evidence about ARM's real
merge semantics, and it is not offered as any.

The failing cases are `xfail(strict=True)`, the convention
`test_an_identity_that_is_explicitly_none_is_not_an_identity.py` set: auditing
is not fixing, the tree stays green, and the moment somebody fixes one it turns
into an XPASS failure that says so.
"""

from __future__ import annotations

import pytest
from azure.ai.ml._restclient.arm_ml_service.models import BatchEndpoint as BatchEndpointData
from azure.ai.ml._restclient.arm_ml_service.models import (
    BatchEndpointDefaults,
    BatchEndpointProperties,
    ManagedServiceIdentity,
    UserAssignedIdentity,
)
from azure.ai.ml.entities import BatchEndpoint
from azure.core.exceptions import ResourceNotFoundError

import ffsft.deploy.endpoint as ep
from ffsft.deploy.batch import BatchDeploymentInUse

LOCATION = "koreacentral"
SCORING_URI = "https://ffsft-batch.koreacentral.inference.ml.azure.com/jobs"
#: Invented. A batch endpoint reaching a private-endpoint-only storage account
#: authenticates as this identity; losing it is how a scoring job starts
#: returning 403 on the data it used to read.
OPERATOR_UAI = (
    "/subscriptions/sub/resourceGroups/rg/providers"
    "/Microsoft.ManagedIdentity/userAssignedIdentities/ffsft-batch-uai"
)


def operator_owned(default_deployment: str = "green") -> BatchEndpointData:
    """A batch endpoint as an operator left it: identified, tagged, routed."""
    return BatchEndpointData(
        location=LOCATION,
        tags={"cost-centre": "kc-ml-01"},
        kind="ffsft-managed",
        identity=ManagedServiceIdentity(
            type="UserAssigned",
            user_assigned_identities={OPERATOR_UAI: UserAssignedIdentity()},
        ),
        properties=BatchEndpointProperties(
            description="nightly scoring for the pricing team",
            auth_mode="AADToken",
            defaults=BatchEndpointDefaults(deployment_name=default_deployment),
            scoring_uri=SCORING_URI,
        ),
    )


class _Poller:
    def __init__(self, value):
        self._value = value

    def result(self):
        return self._value


class FakeBatchEndpoints:
    """`client.batch_endpoints` with the invented PUT-replace rule."""

    def __init__(self, stored: BatchEndpointData, journal: list):
        self.stored = stored
        self.journal = journal
        self.puts: list[BatchEndpointData] = []

    def get(self, name):
        self.journal.append(("GET endpoint", name))
        entity = BatchEndpoint._from_rest_object(self.stored)
        entity.name = name
        return entity

    def begin_create_or_update(self, entity):
        body = entity._to_rest_batch_endpoint(location=LOCATION)
        self.puts.append(body)
        self.journal.append(("PUT endpoint", entity.name))
        body.properties.scoring_uri = SCORING_URI  # ARM repopulates readonly fields
        self.stored = body
        return _Poller(entity)


class OperatorDeployment:
    """What the operator already has under the name this tool hardcodes."""

    name = "default"
    model = "azureml:pricing-prod-model:7"
    compute = "operator-prod-cluster"


class FakeBatchDeployments:
    def __init__(self, journal: list, existing):
        self.journal = journal
        self.store = {d.name: d for d in existing}

    def get(self, name, endpoint_name):
        self.journal.append(("GET deployment", name))
        if name not in self.store:
            # What `BatchDeploymentOperations.get` does, and the distinction the
            # round-7 gate is built on: this is the one answer that positively
            # establishes absence. Returning `None` would let "could not look"
            # in through the same door.
            raise ResourceNotFoundError(f"batch deployment {name} not found")
        return self.store[name]

    def list(self, *args, **kwargs):
        self.journal.append(("LIST deployments", args or kwargs))
        return list(self.store.values())

    def begin_create_or_update(self, deployment):
        self.journal.append(("PUT deployment", deployment.name))
        self.store[deployment.name] = deployment  # PUT replaces
        return _Poller(deployment)


class FakeClient:
    def __init__(self, stored, existing_deployments=()):
        self.journal: list = []
        self.batch_endpoints = FakeBatchEndpoints(stored, self.journal)
        self.batch_deployments = FakeBatchDeployments(self.journal, existing_deployments)


def run_deploy(monkeypatch, *, stored, existing_deployments=(), expect_refusal=False, **kwargs):
    """Drive the real `deploy_batch` against fakes and hand back the client."""
    monkeypatch.setenv("FFSFT_SUBSCRIPTION_ID", "00000000-0000-0000-0000-0000000000ff")
    from ffsft import azure_ml

    client = FakeClient(stored, existing_deployments)
    monkeypatch.setattr(azure_ml, "get_ml_client", lambda *a, **k: client)
    if expect_refusal:
        with pytest.raises(BatchDeploymentInUse) as caught:
            ep.deploy_batch("ffsft-batch", "azureml:qwen-ko:1", **kwargs)
        return client, str(caught.value)
    ep.deploy_batch("ffsft-batch", "azureml:qwen-ko:1", **kwargs)
    return client


# -- what the SDK actually round-trips ------------------------------------


def test_the_batch_entity_azure_hands_back_carries_no_identity_and_no_kind():
    """The premise the surviving PUT rests on, checked rather than assumed.

    `deploy/batch.py:177` reads the endpoint back and `:195` PUTs it. That is
    only non-destructive if the entity carries everything the resource holds.
    It does not: `identity` and `kind` live on the REST resource, and neither
    survives the trip through `BatchEndpoint`.
    """
    stored = operator_owned("green")
    assert stored.identity is not None
    assert stored.kind == "ffsft-managed"

    entity = BatchEndpoint._from_rest_object(stored)
    assert not hasattr(entity, "identity"), "the SDK grew an identity field; re-derive this"

    entity.name = "ffsft-batch"
    entity.defaults = {"deployment_name": "default"}
    body = entity._to_rest_batch_endpoint(location=LOCATION)
    assert body.identity is None
    assert body.kind is None
    assert sorted(body.as_dict()) == ["location", "properties", "tags"]


def test_the_online_endpoint_round_trip_keeps_what_the_batch_one_drops():
    """Why the traffic.py pattern does not transfer to the batch endpoint.

    `deploy/batch.py:173-176` cites read-back-and-mutate, whose home is
    `deploy/traffic.py:77-84` -- an ONLINE endpoint. The online entity carries
    `identity` and `kind` through the round trip, and `_to_rest_online_endpoint`
    goes further: with no identity on the entity it sends
    `type="SystemAssigned"` rather than omitting the field. The SDK is defending
    against an omitted-identity PUT on that path. On the batch path there is
    nothing to defend with, so the same pattern is not the same guarantee.
    """
    from azure.ai.ml.entities import ManagedOnlineEndpoint

    source = inspect_source(ManagedOnlineEndpoint, "_to_rest_online_endpoint")
    assert "identity=identity" in source
    assert 'RestManagedServiceIdentityConfiguration(type="SystemAssigned")' in source

    batch_source = inspect_source(BatchEndpoint, "_to_rest_batch_endpoint")
    assert "identity" not in batch_source, "the batch serialiser learned to send identity"


def inspect_source(cls, name: str) -> str:
    import inspect

    for klass in cls.__mro__:
        fn = klass.__dict__.get(name)
        if fn is not None:
            return inspect.getsource(fn)
    raise AssertionError(f"{cls.__name__} has no {name}; the SDK moved under this test")


def test_both_deploy_commands_let_the_operator_name_the_deployment_they_replace():
    """Round 7 CLOSED this. The asymmetry S76.7 leaned on is gone.

    S76.7 declined to gate the deployment PUT because "`deploy_online` does the
    same with its `--deployment` name". `deploy-online` has that flag, so the
    operator names the resource the PUT will replace; `deploy-batch` did not --
    the name was hardcoded `"default"` in `deploy/batch.py:31` with no way to
    ask for another one. It has the flag now, and `--force` beside it for the
    case where replacing what is there is the actual intent.
    """
    parser = ep.build_parser()
    subparsers = [
        action for action in parser._actions if hasattr(action, "choices") and action.choices
    ]
    choices = subparsers[0].choices
    online_flags = {s for a in choices["deploy-online"]._actions for s in a.option_strings}
    batch_flags = {s for a in choices["deploy-batch"]._actions for s in a.option_strings}
    assert "--deployment" in online_flags
    assert "--deployment" in batch_flags
    assert "--force" in batch_flags


# -- what survives ---------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="SURVIVES round 6: deploy/batch.py:195 PUTs a body that omits `identity`, "
    "so the repoint drops the endpoint's user-assigned identity. The read-back it "
    "cites (traffic.py) is an online endpoint, whose entity carries identity; the "
    "batch entity does not.",
)
def test_the_repoint_leaves_the_operators_user_assigned_identity_on_the_endpoint(monkeypatch):
    """Repointing `defaults` is this command's job. Dropping the identity is not.

    The endpoint keeps answering and the deployment stays `Succeeded`; what
    stops is the scoring job's access to whatever the identity was granted.
    """
    client = run_deploy(monkeypatch, stored=operator_owned("green"))
    assert client.batch_endpoints.stored.identity is not None, "identity was dropped by the PUT"


def test_the_deployment_named_default_is_read_before_it_is_replaced(monkeypatch):
    """Round 7 CLOSED this. The original defect's shape, one resource down.

    `ensure_batch_endpoint` was given the read the endpoint PUT needed. The
    deployment PUT directly below it never got one, so the journal went
    straight to `('PUT deployment', 'default')`. It now reads first, and what
    it finds decides whether the write happens at all.
    """
    client, message = run_deploy(
        monkeypatch,
        stored=operator_owned("green"),
        existing_deployments=[OperatorDeployment()],
        expect_refusal=True,
    )
    kinds = [kind for kind, _ in client.journal]
    assert "GET deployment" in kinds, f"the deployment was written with no prior read: {kinds}"
    assert "PUT deployment" not in kinds, f"it read, and then wrote anyway: {kinds}"
    assert "azureml:pricing-prod-model:7" in message
    assert "operator-prod-cluster" in message


def test_a_redeploy_does_not_change_which_model_an_existing_default_deployment_runs(monkeypatch):
    """Round 7 CLOSED this. The quiet case, which is the one that mattered.

    With `defaults` already naming `default`, the S76 fix correctly skips the
    endpoint PUT -- so nothing about the endpoint warned, and its routing was
    intact, while the URI quietly started serving a different model on a
    different cluster. The deployment gate now refuses before the write, and
    the refusal names both halves of the change.
    """
    client, message = run_deploy(
        monkeypatch,
        stored=operator_owned("default"),
        existing_deployments=[OperatorDeployment()],
        expect_refusal=True,
    )
    assert client.batch_endpoints.puts == [], "precondition: the endpoint itself is left alone"
    assert client.batch_deployments.store["default"].model == "azureml:pricing-prod-model:7"
    assert client.batch_deployments.store["default"].compute == "operator-prod-cluster"
    assert "--force" in message and "--deployment" in message


def test_an_operator_who_asks_for_the_replacement_on_purpose_still_gets_it(monkeypatch):
    """The escape hatch, because a gate with no way through is a broken command.

    `--force` is the operator saying the replacement IS the intent. Without
    this test the gate could be tightened to a refusal nobody can clear, which
    is the sign-flipped version of the same mistake.
    """
    client = run_deploy(
        monkeypatch,
        stored=operator_owned("default"),
        existing_deployments=[OperatorDeployment()],
        force=True,
    )
    assert client.batch_deployments.store["default"].model == "azureml:qwen-ko:1"


def test_a_deployment_under_another_name_does_not_touch_the_operators_default(monkeypatch):
    """`--deployment` is the other way through, and the one that costs nothing.

    The operator's `default` keeps its model; the new deployment lands beside
    it, and the endpoint's routing pointer moves to the name that was asked for.
    """
    client = run_deploy(
        monkeypatch,
        stored=operator_owned("default"),
        existing_deployments=[OperatorDeployment()],
        deployment_name="ffsft-ko",
    )
    assert client.batch_deployments.store["default"].model == "azureml:pricing-prod-model:7"
    assert client.batch_deployments.store["ffsft-ko"].model == "azureml:qwen-ko:1"
    assert client.batch_endpoints.stored.properties.defaults.deployment_name == "ffsft-ko"


def test_a_redeploy_of_the_very_same_model_on_the_very_same_cluster_is_not_refused(monkeypatch):
    """The over-correction guard. Idempotent redeploy is what this command is for.

    A gate that refused here would break the ordinary case -- re-running
    `deploy-batch` after a failed rollout -- to protect against a change that
    is not happening.
    """

    class SameThing:
        name = "default"
        model = "azureml:qwen-ko:1"
        compute = "gpu-a100-lp"

    client = run_deploy(
        monkeypatch,
        stored=operator_owned("default"),
        existing_deployments=[SameThing()],
        compute_name="gpu-a100-lp",
    )
    assert client.batch_deployments.store["default"].model == "azureml:qwen-ko:1"
