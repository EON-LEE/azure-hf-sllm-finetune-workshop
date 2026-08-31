"""A datastore listing nobody could read must not be reported as an empty one.

Both files that read the workspace's datastore list swallowed a failed read into
`[]`, and `[]` is the value that means "measured, and none of them present an
account key" -- the measurement that says the docs/JOURNAL.md S57.8 blocker is
absent. So one 403 on one ARM GET turned "could not look" into "looked, saw
nothing" on the two paths where it costs the most.

Site 1, `preflight.read_storage_reachability`, gates a $4.959/hr deployment.
Executed against the same fake account twice, the listing the only difference:

    listing 200 -> key_auth_refused True  -> deploy_online raises, refuses
    listing 403 -> key_auth_refused False -> deploy_online PROCEEDS

Site 2, `probes._key_based_datastores`, gates the exit code of `check`:

    listing readable  -> datastore  UNREACHABLE  stffsftkc ...   rc 0
    listing 403       -> no datastore line at all               rc 0
                         `check && echo ok` printed ok

The unread workspace rendered strictly cleaner than the broken one it may be.
Worse on that second path, the GET had no `raise_for_status`, so a 403 whose
body still parsed as JSON produced `[]` without even reaching the `log.warning`.

Both now carry `None` for "not read", and `key_auth_refused` is tri-state in
both files. What the third state *does* differs by caller and deliberately so:
`deploy_online` refuses (money, one re-run to recover -- see
`preflight._credential_unread_blocker`), `check` reports it in COULD NOT LOOK
and exits `EXIT_COULD_NOT_LOOK`, like every other unread read there.

Half the tests below are over-correction guards. A listing that succeeds and
finds no key-based datastores is a *measurement*, and it must stay a clean pass
in both files -- turning "could not look" into a blocker everywhere would be the
same defect pointed the other way.
"""

from __future__ import annotations

import argparse

import pytest
import requests

import ffsft.deploy.endpoint as ep
import ffsft.deploy.probes as probes
from ffsft.deploy.preflight import (
    StorageReachability,
    read_storage_reachability,
    storage_blocker,
)
from ffsft.deploy.probes import StoreProbe, _key_based_datastores, classify_store

ACCOUNT = "stffsftplc"
KEYED = ["workspaceartifactstore", "workspaceblobstore"]

# -- the fake ARM boundary -------------------------------------------------
#
# Every response below is INVENTED. Nothing in this suite may reach Azure, and
# the shapes are the ones the two callers actually index into, so a fake that
# drifts from the real payload fails at the same line the real one would.

_STORAGE = {
    "name": ACCOUNT,
    "properties": {
        "publicNetworkAccess": "Enabled",
        "networkAcls": {"bypass": "None"},
        "privateEndpointConnections": [],
        "allowSharedKeyAccess": False,
    },
}
_WORKSPACE = {
    "properties": {
        "storageAccount": f"/subscriptions/s/resourceGroups/rg/providers/x/{ACCOUNT}",
        "managedNetwork": {"isolationMode": "Disabled"},
    }
}
_DATASTORES = {
    "value": [
        {"name": n, "properties": {"credentials": {"credentialsType": "AccountKey"}}}
        for n in ("workspaceblobstore", "workspaceartifactstore")
    ]
}
_IDENTITY_DATASTORES = {
    "value": [
        {"name": n, "properties": {"credentials": {"credentialsType": "None"}}}
        for n in ("workspaceblobstore", "workspaceartifactstore")
    ]
}


class _Response:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"{self.status_code} Client Error: (AuthorizationFailed)")

    def json(self):
        return self._body


def _arm(listing):
    """A fake `requests.get`. `listing` is a body, or an Exception to raise."""

    def get(url, headers=None, timeout=None):
        if "/datastores?" in url:
            if isinstance(listing, BaseException):
                raise listing
            return listing
        if "Microsoft.MachineLearningServices/workspaces/" in url:
            return _Response(_WORKSPACE)
        return _Response(_STORAGE)

    return get


class _Credential:
    def get_token(self, *scopes):
        return type("Token", (), {"token": "fake"})()


class _Target:
    subscription_id = "00000000-0000-0000-0000-000000000000"
    resource_group = "rg-ffsft-plc"
    workspace_name = "mlw-ffsft-plc"


@pytest.fixture
def arm(monkeypatch):
    """Patch the attribute the callers reach for: both do `import requests`."""

    def install(listing):
        monkeypatch.setattr(requests, "get", _arm(listing))

    return install


# -- site 1: preflight, which gates the spend ------------------------------


def test_a_datastore_listing_that_could_not_be_read_is_not_an_empty_one(arm):
    arm(_Response({"error": {"code": "AuthorizationFailed"}}, 403))
    state = read_storage_reachability(_Target(), credential=_Credential())
    assert state.key_based_datastores is None
    assert state.key_auth_refused is None


def test_a_datastore_listing_that_was_read_answers_with_the_names(arm):
    arm(_Response(_DATASTORES))
    state = read_storage_reachability(_Target(), credential=_Credential())
    assert state.key_based_datastores == KEYED
    assert state.key_auth_refused is True


def test_the_deploy_no_longer_turns_on_whether_the_listing_could_be_read(arm):
    """The executed A/B this round was opened with. One account, one variable.

    Before: 200 refused the deployment and 403 waved it through, which is the
    invariant inverted -- the run nobody could vet was the one that got to spend.
    """
    arm(_Response(_DATASTORES))
    read = storage_blocker(read_storage_reachability(_Target(), credential=_Credential()))

    arm(_Response({"error": {"code": "AuthorizationFailed"}}, 403))
    unread = storage_blocker(read_storage_reachability(_Target(), credential=_Credential()))

    # `deploy_online` raises on any non-None `storage_blocker`, force aside.
    # No line number: endpoint.py is under an active split and they rot.
    assert read is not None
    assert unread is not None
    # ...and they are not the same sentence: one is a verdict, one is a gap.
    assert "credentialsType=AccountKey" in read
    assert "could not be read" in unread


def test_an_unread_listing_says_it_is_unknown_rather_than_naming_a_blocker():
    state = StorageReachability(
        account_name=ACCOUNT,
        public_network_access="Enabled",
        allow_shared_key=False,
        key_based_datastores=None,
    )
    blocker = storage_blocker(state)
    assert "UNKNOWN" in blocker
    assert "allowSharedKeyAccess=false" in blocker
    # The refusal has to be recoverable in one command, or it is just an outage
    # of its own -- that is the price the money argument is weighed against.
    assert "force=True" in blocker
    assert "Reader on the workspace" in blocker


def test_a_measured_empty_listing_on_a_hardened_account_is_still_a_clean_pass(arm):
    """The over-correction guard, and the posture section 58 deliberately built.

    `allowSharedKeyAccess: false` with every datastore identity-based is the
    fixed state, not a fault. If this ever fails, the fix above has eaten the
    workspace it was meant to protect.
    """
    arm(_Response(_IDENTITY_DATASTORES))
    state = read_storage_reachability(_Target(), credential=_Credential())
    assert state.key_based_datastores == []
    assert state.key_auth_refused is False
    assert storage_blocker(state) is None


def test_an_unread_listing_beside_an_unread_key_policy_is_not_a_blocker():
    """Blindness is only reported where it could change the answer.

    Nothing measured says this account refuses keys, so what its datastores
    present cannot make `key_auth_refused` True and the missing listing costs
    nothing. Reporting every unread read regardless is how a check becomes one
    nobody can pass.
    """
    state = StorageReachability(
        account_name=ACCOUNT,
        public_network_access="Enabled",
        allow_shared_key=None,
        key_based_datastores=None,
    )
    assert state.key_auth_refused is False
    assert storage_blocker(state) is None


def test_an_unread_listing_beside_a_key_allowing_account_is_not_a_blocker():
    state = StorageReachability(
        account_name=ACCOUNT,
        public_network_access="Enabled",
        allow_shared_key=True,
        key_based_datastores=None,
    )
    assert state.key_auth_refused is False
    assert storage_blocker(state) is None


def test_a_whole_read_that_failed_still_returns_nothing_rather_than_a_refusal(monkeypatch):
    """The narrowing stops where the evidence does.

    A partial read measured the hardened posture and then went blind on the half
    that makes it fatal. A read that produced nothing measured nothing, and this
    function's contract -- a preflight must never be the reason a workable
    deployment does not happen -- still holds there. Every GET fails here, not
    just the listing, which is the distinction the fixture above cannot express.
    """

    def dead(*a, **kw):
        raise RuntimeError("HTTPSConnectionPool: read timed out")

    monkeypatch.setattr(requests, "get", dead)
    assert read_storage_reachability(_Target(), credential=_Credential()) is None


# -- site 2: probes, which gates the exit code -----------------------------


def _head():
    return {"Authorization": "Bearer fake"}


def test_a_datastore_listing_that_raised_is_not_a_measured_empty_listing(arm):
    arm(RuntimeError("HTTPSConnectionPool: read timed out"))
    assert _key_based_datastores("https://arm/x", "mlw-ffsft-plc", _head()) is None


def test_a_403_whose_body_still_parses_as_json_is_not_an_empty_listing(arm):
    """The worse half: with no `raise_for_status` this raised nothing at all.

    `page.get("value") or []` turned an error document into a measured-empty
    listing, so not even the `log.warning` fired and stderr was clean too.
    """
    arm(_Response({"error": {"code": "AuthorizationFailed"}}, 403))
    assert _key_based_datastores("https://arm/x", "mlw-ffsft-plc", _head()) is None


def test_a_readable_datastore_listing_still_returns_the_key_based_names(arm):
    arm(_Response(_DATASTORES))
    assert _key_based_datastores("https://arm/x", "mlw-ffsft-plc", _head()) == KEYED


def test_a_readable_listing_of_identity_datastores_returns_a_measured_empty(arm):
    arm(_Response(_IDENTITY_DATASTORES))
    assert _key_based_datastores("https://arm/x", "mlw-ffsft-plc", _head()) == []


def test_classify_store_refuses_to_answer_the_credential_axis_it_could_not_read():
    probe = classify_store(
        ACCOUNT, "Enabled", 0, allow_shared_key=False, key_based_datastores=None
    )
    assert probe.key_auth_refused is None
    # `reachable` stays the network answer. Flipping it would print UNREACHABLE,
    # which is a claim about the account that nothing here measured.
    assert probe.reachable is True


def test_a_measured_empty_listing_still_classifies_as_answered_and_reachable():
    """The `tests/test_model_store.py` guard, restated against the sentinel."""
    probe = classify_store(
        ACCOUNT, "Disabled", 2, allow_shared_key=False, key_based_datastores=()
    )
    assert probe.key_auth_refused is False
    assert probe.reachable is True


def test_an_unread_listing_is_not_reported_when_the_key_policy_was_not_read_either():
    probe = classify_store(ACCOUNT, "Enabled", 0, allow_shared_key=None)
    assert probe.key_auth_refused is False


# -- site 2, end to end: the exit code the report leaves behind ------------

_FFSFT_VARS = (
    "FFSFT_SUBSCRIPTION_ID",
    "AZURE_SUBSCRIPTION_ID",
    "FFSFT_TENANT_ID",
    "AZURE_TENANT_ID",
    "FFSFT_RESOURCE_GROUP",
    "FFSFT_WORKSPACE",
    "FFSFT_LOCATION",
    "FFSFT_COMPUTE",
    "FFSFT_SKU",
    "FFSFT_VM_PRIORITY",
)


@pytest.fixture
def run_check(monkeypatch, capsys):
    """Drive `cmd_check` with no Azure at all; return (exit code, stdout).

    Patched on `endpoint`, because that is the module `cmd_check` reaches for.
    """

    def run(store):
        for name in _FFSFT_VARS:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("FFSFT_SUBSCRIPTION_ID", "sub-participant")
        monkeypatch.setenv("FFSFT_RESOURCE_GROUP", "rg-mine")
        monkeypatch.setenv("FFSFT_WORKSPACE", "mlw-mine")
        monkeypatch.setenv("FFSFT_LOCATION", "koreacentral")
        monkeypatch.setattr(ep, "probe_model_store", lambda target: store)
        monkeypatch.setattr(
            ep,
            "check_pattern",
            lambda key, sub, loc, **kw: (ep.get_serving_registry().get(key), None),
        )
        monkeypatch.setattr(ep, "read_dedicated_quota", lambda sub, loc, family: 24)

        def no_network(*a, **kw):
            raise AssertionError("the suite must not reach management.azure.com")

        monkeypatch.setattr(probes, "read_dedicated_quota", no_network)
        capsys.readouterr()
        code = ep.cmd_check(argparse.Namespace(probe=False))
        return code, capsys.readouterr().out

    return run


def test_check_says_it_could_not_look_when_the_datastore_listing_was_unread(run_check):
    """The measured before-state: no datastore line, no COULD NOT LOOK, rc 0."""
    unread = classify_store(
        ACCOUNT, "Enabled", 0, allow_shared_key=False, key_based_datastores=None
    )
    code, out = run_check(unread)
    assert "datastore  UNKNOWN" in out
    assert "COULD NOT LOOK" in out
    assert "still authenticates with an account key" in out
    assert code == ep.EXIT_COULD_NOT_LOOK


def test_check_still_exits_zero_when_the_listing_was_read_and_found_nothing(run_check):
    """The over-correction guard on the report path."""
    measured = classify_store(
        ACCOUNT, "Enabled", 0, allow_shared_key=False, key_based_datastores=()
    )
    code, out = run_check(measured)
    assert "COULD NOT LOOK" not in out
    assert code == 0


def test_check_still_exits_zero_when_the_credential_axis_never_applied(run_check):
    """No measured `allowSharedKeyAccess: false`, so the listing decides nothing."""
    code, out = run_check(StoreProbe(ACCOUNT, "Enabled", 0, True, ""))
    assert "COULD NOT LOOK" not in out
    assert code == 0
