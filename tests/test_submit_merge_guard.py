"""The merge path refuses a storage account nothing can reach.

`submit_training.py` has had this guard since the training path wasted two 27B
runs on it. `submit_merge.py` did not have it, and the merge is the job with the
most to lose: it is the only one that both *reads* a mount and *writes* one.

That gap was not theoretical. It cost three jobs. A merge submitted while the
workspace sat at `systemDatastoresAuthMode=accesskey` died seven times over on

    Failed to mount URI azureml://.../model_dir/ at mount point .../INPUT_adapter

with the real reason -- the account refuses the account key its own datastore
presents -- nowhere in the message. The state that produced it was fully visible
in the two reads this guard already makes (docs/VERIFIED.md S63).

These tests drive `scripts/submit_merge.py::main` directly, and exist mostly to
prove the guard can *fire*. A preflight that always passes is indistinguishable
from no preflight at all, and that failure is invisible in production, because
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
        "submit_merge_script", _ROOT / "scripts" / "submit_merge.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def script():
    return _load_script()


def _state(**kw) -> StorageReachability:
    base = dict(
        account_name="mlwffsftstorage09dd66111",
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
    "submit_merge.py",
    "--subscription", "00000000-0000-0000-0000-000000000000",
    "--resource-group", "rg-ffsft-plc",
    "--workspace", "mlw-ffsft-plc",
    "--adapter", "qwen3_8-27b-ko-lora:1",
]


def _run(script, monkeypatch, *, reachability, argv=ARGV):
    """Drive main() with the network reads stubbed. Records whether it submitted."""
    import ffsft.deploy.preflight as preflight

    submitted: list[object] = []
    monkeypatch.setattr(
        preflight, "read_storage_reachability", lambda *a, **k: reachability
    )
    monkeypatch.setattr(
        script, "submit", lambda t, s, wait=False: submitted.append((t, s)) or {}
    )
    # `AzureTarget.from_env` refuses to guess a subscription. The flag overrides
    # it afterwards, but only if construction got that far.
    monkeypatch.setenv("FFSFT_SUBSCRIPTION_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setattr(sys, "argv", list(argv))
    return script.main(), submitted


def test_an_unreachable_account_stops_the_merge_before_it_costs_a_node(
    script, monkeypatch
):
    rc, submitted = _run(script, monkeypatch, reachability=_state())
    assert rc == 1
    assert submitted == [], "the whole point is that no node is allocated"


def test_the_refusal_names_the_failure_the_merge_would_have_shown(
    script, monkeypatch, capsys
):
    """The mount error is the least informative message in the system.

    Someone who has just watched seven identical `Failed to mount URI` retries
    needs this text to connect that message to the credential axis, because the
    message itself never mentions credentials.
    """
    _run(script, monkeypatch, reachability=_state())
    err = capsys.readouterr().err
    assert "mlwffsftstorage09dd66111" in err
    assert "fail to mount the adapter" in err
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


def test_the_arrangement_that_actually_killed_the_merge_is_refused(
    script, monkeypatch
):
    """The exact live state on 2026-08-26, as measured.

    plc's network arrangement is fine -- two private endpoints, an isolated
    workspace -- and that is the trap: every network check comes back green.
    `allowSharedKeyAccess=False` next to an `AccountKey` datastore is the whole
    fault, and it is on a completely separate axis.
    """
    killed_it = _state(
        private_endpoints=["pe-one", "pe-two"],
        workspace_isolation_mode="AllowInternetOutbound",
        allow_shared_key=False,
        key_based_datastores=[
            "workspaceblobstore",
            "workspaceartifactstore",
            "workspacefilestore",
            "workspaceworkingdirectory",
        ],
    )
    rc, submitted = _run(script, monkeypatch, reachability=killed_it)
    assert rc == 1
    assert submitted == []


def test_the_state_after_the_fix_submits(script, monkeypatch):
    """Same account, same network, datastores flipped to identity.

    This is what `systemDatastoresAuthMode='identity'` produces, and it is the
    state in which the merge ran. Pinned so a future tightening of the guard
    cannot start refusing the arrangement that works.
    """
    fixed = _state(
        private_endpoints=["pe-one", "pe-two"],
        workspace_isolation_mode="AllowInternetOutbound",
        allow_shared_key=False,
        key_based_datastores=[],
    )
    rc, submitted = _run(script, monkeypatch, reachability=fixed)
    assert rc == 0
    assert len(submitted) == 1


def test_an_unreadable_subscription_does_not_block_a_workable_merge(
    script, monkeypatch
):
    """`read_storage_reachability` returns None on every failure path.

    Degrading to "not blocked" is deliberate: a guard that refuses on its own
    inability to read would make an expired token look like a policy refusal.
    """
    rc, submitted = _run(script, monkeypatch, reachability=None)
    assert rc == 0
    assert len(submitted) == 1
