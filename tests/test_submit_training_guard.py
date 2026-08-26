"""The training path refuses a storage account nothing can reach.

The deploy path has refused this since section 58. The training path did not,
and it is the path that can waste more: the run allocates a node, pulls a 9 GB
image, and only then discovers it has nowhere to put an adapter. Two completed
27B runs already died that way -- they left six artifacts each, all logs.

These tests drive `scripts/submit_training.py::main` directly. They exist mostly
to prove the guard can *fire*: a preflight that always passes is indistinguishable
from no preflight at all, and that failure is invisible in production because
production is the case where it correctly passes.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from ffsft.deploy.preflight import StorageReachability

_ROOT = Path(__file__).resolve().parents[1]


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "submit_training_script", _ROOT / "scripts" / "submit_training.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def script():
    return _load_script()


def _state(**kw) -> StorageReachability:
    base = dict(
        account_name="mlwffsftstorage8cb451dd1",
        public_network_access="Disabled",
        ip_rules=[],
        vnet_rules=[],
        private_endpoints=[],
        workspace_isolation_mode="Disabled",
        allow_shared_key=None,
        key_based_datastores=[],
    )
    base.update(kw)
    return StorageReachability(**base)


ARGV = [
    "submit_training.py",
    "--subscription", "00000000-0000-0000-0000-000000000000",
    "--resource-group", "rg-ffsft-plc",
    "--workspace", "mlw-ffsft-plc",
    "--location", "polandcentral",
]


def _run(script, monkeypatch, *, reachability, argv=ARGV):
    """Drive main() with the network reads stubbed. Records whether it submitted."""
    import ffsft.deploy.preflight as preflight

    submitted: list[object] = []
    monkeypatch.setattr(
        preflight, "read_storage_reachability", lambda *a, **k: reachability
    )
    monkeypatch.setattr(
        script, "submit", lambda t, j, wait=False: submitted.append((t, j)) or {}
    )
    monkeypatch.setattr(sys, "argv", list(argv))
    return script.main(), submitted


def test_an_unreachable_account_stops_the_run_before_it_costs_a_node(
    script, monkeypatch
):
    rc, submitted = _run(script, monkeypatch, reachability=_state())
    assert rc == 1
    assert submitted == [], "the whole point is that no node is allocated"


def test_the_refusal_says_what_the_run_would_have_produced(
    script, monkeypatch, capsys
):
    _run(script, monkeypatch, reachability=_state())
    err = capsys.readouterr().err
    # Naming the account is not enough. The reason a person overrides this guard
    # wrongly is not knowing what they lose by overriding it.
    assert "mlwffsftstorage8cb451dd1" in err
    assert "produce nothing that outlives it" in err
    assert "--force" in err


def test_force_submits_anyway_and_says_so(script, monkeypatch, capsys):
    rc, submitted = _run(
        script, monkeypatch, reachability=_state(), argv=[*ARGV, "--force"]
    )
    assert rc == 0
    assert len(submitted) == 1
    assert "submitting despite" in capsys.readouterr().err


def test_a_reachable_account_is_not_mentioned_at_all(script, monkeypatch, capsys):
    reachable = _state(
        public_network_access="Enabled", workspace_isolation_mode="Disabled"
    )
    rc, submitted = _run(script, monkeypatch, reachability=reachable)
    assert rc == 0
    assert len(submitted) == 1
    assert capsys.readouterr().err == ""


def test_the_live_plc_arrangement_passes(script, monkeypatch):
    """Private endpoint plus an isolated workspace -- what plc actually has.

    Pinned because this is the arrangement the guard must not refuse. It is also
    the one that made `key_auth_refused` matter: the account has
    allowSharedKeyAccess=False, which is only survivable because section 58
    converted every datastore to identity-based.
    """
    plc = _state(
        private_endpoints=["pe-one", "pe-two"],
        workspace_isolation_mode="AllowInternetOutbound",
        allow_shared_key=False,
        key_based_datastores=[],
    )
    rc, submitted = _run(script, monkeypatch, reachability=plc)
    assert rc == 0
    assert len(submitted) == 1


def test_a_key_based_datastore_on_that_same_account_would_have_refused(
    script, monkeypatch
):
    """The credential axis is independent of the network one.

    Same plc network arrangement, one datastore still on AccountKey: the account
    refuses the key its own datastore presents, and no private endpoint fixes it.
    """
    regressed = _state(
        private_endpoints=["pe-one", "pe-two"],
        workspace_isolation_mode="AllowInternetOutbound",
        allow_shared_key=False,
        key_based_datastores=["workspaceblobstore"],
    )
    rc, submitted = _run(script, monkeypatch, reachability=regressed)
    assert rc == 1
    assert submitted == []


def test_an_unreadable_subscription_does_not_block_a_workable_run(
    script, monkeypatch
):
    """`read_storage_reachability` returns None on every failure path.

    A preflight may prevent a doomed deployment; it must never be the reason a
    workable one does not happen.
    """
    rc, submitted = _run(script, monkeypatch, reachability=None)
    assert rc == 0
    assert len(submitted) == 1
