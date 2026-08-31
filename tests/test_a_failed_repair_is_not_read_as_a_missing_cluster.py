"""A repair that 404s is not the same fact as a cluster that does not exist.

`ensure_compute` wrapped four statements in one `try` and caught
`ResourceNotFoundError` from all of them with `pass`:

    try:
        existing = client.compute.get(target.compute_name)      # may 404: absent
        ...
        existing.identity = IdentityConfiguration(...)
        repaired = client.compute.begin_create_or_update(existing).result().name
        ...
    except ResourceNotFoundError:
        pass
    cluster = AmlCompute(... size=target.compute_sku, max_instances=target.max_nodes ...)

The `pass` falls through to the create path, which is correct for a 404 from
the `get` and catastrophic for a 404 from the repair PUT: the same name is
re-PUT as a FRESH `AmlCompute` assembled from the environment defaults. The
operator's hand-built cluster is not repaired, it is overwritten -- under a
comment that reads "Repair it in place."

Reproduced against the unpatched function with the fakes below, no Azure:

    operator's cluster BEFORE : size=Standard_NC96ads_A100_v4 max=8 tier=Dedicated min=2
      PUT #1: size=Standard_NC96ads_A100_v4 max=8 tier=Dedicated min=2   <- repair, 404s
      PUT #2: size=Standard_NC24ads_A100_v4 max=1 tier=LowPriority min=0 <- fresh, env defaults

and `ensure_compute` returned the cluster name, so the caller was told the
cluster was fine. Eight A100s Dedicated became one A100 LowPriority, silently.

The narrow fix is the one the invariant asks for everywhere: "could not do it"
must not be reported as "there was nothing there". Only the `get` may answer
the question "does this compute exist", so only the `get` is inside the try.
A 404 from the repair is a failed repair and is raised.

Reachable only when `existing.identity is None` and the repair PUT 404s -- the
cluster deleted between the read and the write, or a workspace whose parent
scope has gone. Narrow, and unaudited: `tests/test_compute_grants.py` covers
the grants `ensure_compute` makes and never drove this branch.
"""

from __future__ import annotations

import pytest
from azure.ai.ml.entities import AmlCompute, IdentityConfiguration
from azure.core.exceptions import ResourceNotFoundError

from ffsft import azure_ml
from ffsft.azure_ml import AzureTarget

#: The defaults `AzureTarget` supplies, i.e. exactly what the create path would
#: PUT. Kept as a literal so a change to the defaults cannot quietly make the
#: downgrade assertion compare a value against itself.
TARGET = AzureTarget("sub", "rg", "ws")
ENV_DEFAULTS = ("Standard_NC24ads_A100_v4", 1, "LowPriority", 0)

#: What an operator built by hand to escape LowPriority preemption. Nothing
#: about it is derivable from the environment, which is why losing it is
#: unrecoverable rather than inconvenient.
OPERATOR = ("Standard_NC96ads_A100_v4", 8, "Dedicated", 2)


def _operator_cluster(name: str) -> AmlCompute:
    size, max_instances, tier, min_instances = OPERATOR
    return AmlCompute(
        name=name,
        type="amlcompute",
        size=size,
        min_instances=min_instances,
        max_instances=max_instances,
        tier=tier,
        # The condition that opens this branch at all.
        identity=None,
    )


class _Put:
    """One PUT, snapshotted.

    A snapshot rather than the object: `ensure_compute` mutates the very
    instance it got back from `get` and PUTs that, so holding the reference
    would let PUT #1 be re-read after PUT #2 and hide the difference.
    """

    def __init__(self, compute):
        self.name = compute.name
        self.spec = (
            compute.size,
            compute.max_instances,
            getattr(compute, "tier", None),
            compute.min_instances,
        )
        self.identity = compute.identity


class _Poller:
    def __init__(self, value):
        self._value = value

    def result(self):
        return self._value


class _FakeCompute:
    """The compute collection of a workspace, recording every write.

    `put_raises` is consumed one entry per PUT, so a run can say "the repair
    404s and a later create would succeed" -- the shape that makes the
    downgrade silent instead of loud.
    """

    def __init__(self, existing=None, put_raises=()):
        self.existing = existing
        self.put_raises = list(put_raises)
        self.puts: list[_Put] = []

    def get(self, name):
        if self.existing is None:
            raise ResourceNotFoundError(f"compute {name} not found")
        return self.existing

    def begin_create_or_update(self, compute):
        self.puts.append(_Put(compute))
        outcome = self.put_raises.pop(0) if self.put_raises else None
        if outcome is not None:
            # Whether ARM's 404 surfaces from the call or from the poller does
            # not matter to the defect: both were inside the same `try`.
            raise outcome
        return _Poller(compute)


class _FakeClient:
    def __init__(self, compute):
        self.compute = compute


def _wired(monkeypatch, **kwargs):
    """A client that never leaves the process, and no role grants.

    `grant_compute_data_roles` is stubbed to a recorder: it has its own tests,
    it documents that it never raises, and letting it run here would send ARM
    reads at a subscription that does not exist.
    """
    compute = _FakeCompute(**kwargs)
    granted: list[str] = []
    monkeypatch.setattr(azure_ml, "get_ml_client", lambda target: _FakeClient(compute))
    def _record(target, name):
        granted.append(name)
        # The real one returns a GrantsOutcome and `ensure_compute` reads
        # `.unverified` off it. A recorder that returns None is a double that
        # has drifted from the contract, which is a green test over a
        # TypeError in production.
        return azure_ml.GrantsOutcome()

    monkeypatch.setattr(azure_ml, "grant_compute_data_roles", _record)
    return compute, granted


# --- the defect --------------------------------------------------------------


def test_a_failed_repair_does_not_put_env_defaults_over_the_operators_cluster(monkeypatch):
    """The reproduction. What is asserted is the writes, not the return value.

    The 404 is swallowed deliberately here rather than with `pytest.raises`, so
    that on the unpatched function this fails on the PUT log -- which shows the
    downgrade -- instead of on "DID NOT RAISE", which does not.
    """
    compute, _ = _wired(
        monkeypatch,
        existing=_operator_cluster(TARGET.compute_name),
        put_raises=[ResourceNotFoundError("compute gpu-a100-lp not found")],
    )

    try:
        azure_ml.ensure_compute(TARGET)
    except ResourceNotFoundError:
        pass

    puts = [put.spec for put in compute.puts]
    assert puts == [OPERATOR], (
        "the failed repair fell through to the create path, which re-PUT the same "
        f"name as a fresh cluster from the environment defaults: {puts}"
    )
    assert ENV_DEFAULTS not in puts


def test_a_failed_repair_is_raised_rather_than_reported_as_a_cluster_that_is_ready(monkeypatch):
    """Returning the name told the caller the cluster was fine. It was not.

    `scripts/provision_azure.py` prints the returned name as the provisioning
    result, so swallowing the 404 turned an unrepaired cluster into a green
    line of output.
    """
    compute, granted = _wired(
        monkeypatch,
        existing=_operator_cluster(TARGET.compute_name),
        put_raises=[ResourceNotFoundError("compute gpu-a100-lp not found")],
    )

    with pytest.raises(ResourceNotFoundError):
        azure_ml.ensure_compute(TARGET)

    assert granted == [], "grants were made against a cluster whose repair had failed"


# --- what the narrowing must not break ---------------------------------------


def test_a_missing_cluster_is_still_created_from_the_target(monkeypatch):
    """The 404 the `try` is actually for: the `get`, and only the `get`."""
    compute, granted = _wired(monkeypatch, existing=None)

    assert azure_ml.ensure_compute(TARGET) == TARGET.compute_name
    assert [put.spec for put in compute.puts] == [ENV_DEFAULTS]
    assert compute.puts[0].identity is not None, "a cluster created without an identity"
    assert granted == [TARGET.compute_name]


def test_a_cluster_that_already_has_an_identity_is_returned_without_a_write(monkeypatch):
    existing = _operator_cluster(TARGET.compute_name)
    # An IdentityConfiguration and not a bare `object()`: the guard reads the
    # `type` now, because ARM spells "no identity" as `{"type": "None"}` as well
    # as by omitting the key, and a sentinel with no `type` at all is a shape
    # `client.compute.get` never returns.
    existing.identity = IdentityConfiguration(type="system_assigned")
    compute, granted = _wired(monkeypatch, existing=existing)

    assert azure_ml.ensure_compute(TARGET) == TARGET.compute_name
    assert compute.puts == [], "an existing healthy cluster was written to"
    assert granted == [TARGET.compute_name]


def test_a_repair_that_succeeds_keeps_the_operators_own_shape(monkeypatch):
    """The repair adds an identity and changes nothing else -- that is what
    "in place" means, and it is the behaviour the create path destroyed."""
    compute, granted = _wired(monkeypatch, existing=_operator_cluster(TARGET.compute_name))

    assert azure_ml.ensure_compute(TARGET) == TARGET.compute_name
    assert [put.spec for put in compute.puts] == [OPERATOR]
    assert compute.puts[0].identity is not None
    assert granted == [TARGET.compute_name]


def test_a_404_from_the_grants_after_a_create_is_not_a_second_create(monkeypatch):
    """`grant_compute_data_roles` was inside the try too, on the identity branch.

    It documents that it never raises, so this is a guard rather than a
    reproduction: if that promise is ever broken, the caught 404 must not send
    a cluster that already exists back through the create path.
    """
    existing = _operator_cluster(TARGET.compute_name)
    existing.identity = IdentityConfiguration(type="system_assigned")
    compute, _ = _wired(monkeypatch, existing=existing)

    def _explode(target, name):
        raise ResourceNotFoundError("role assignment scope not found")

    monkeypatch.setattr(azure_ml, "grant_compute_data_roles", _explode)

    with pytest.raises(ResourceNotFoundError):
        azure_ml.ensure_compute(TARGET)

    assert compute.puts == []
