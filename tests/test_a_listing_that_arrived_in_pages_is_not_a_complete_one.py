"""ARM answered with one page and both call sites called it the whole list.

Round 6 gave the datastore listing an unread sentinel for the case where the
GET *fails*. The case where the GET *succeeds and is truncated* is untouched,
and it lands on the same value the round-6 docstrings now reserve for "measured,
and none of them present an account key":

    src/ffsft/deploy/preflight.py:377    `for d in stores.json().get("value") or []`
    src/ffsft/deploy/probes.py:588       `for d in (page.get("value") or [])`

Microsoft's published schema for `Datastores - List` (the api-version both
callers send, 2024-10-01, is one of the supported versions) is
`DatastoreResourceArmPaginatedResult`:

    nextLink : string (uri)   "The link to the next page of items"
    value    : Datastore[]    "The Datastore items on this page"
    $skip    : query param    "Continuation token for pagination."

Neither caller reads `nextLink`. probes.py already calls its variable `page`.

Executed, same fake account and the same four fake datastores both runs, the
only variable being whether ARM returned them in one page or two -- which is
ARM's choice, not the caller's:

    ARM ONE page      preflight.key_auth_refused True  -> deploy_online RAISED
                      probes  .key_auth_refused True  -> `check` printed the row
    ARM PAGINATED     preflight.key_auth_refused False -> deploy_online RETURNED
                      and recorded  PUT online_deployments/blue
                      instance_type=Standard_NV12ads_A10_v5 count=1
                      probes  .key_auth_refused False -> no datastore line at
                      all, no COULD NOT LOOK block, exit 0,
                      `check && echo ok` printed ok

That is the round-6 A/B with `403` swapped for `nextLink`, and it still flips
the same way: the run nobody could fully vet is the one that gets to spend.

`xfail(strict=True)` and not a fix, because auditing is not fixing: the tree
stays green with these in it, and the day someone follows the listing they turn
into failures that have to be removed on purpose. Every response below is a
FAKE built at the `requests` boundary -- this repo has no Azure access and none
of these shapes was ever observed on a live subscription; what is proven is how
the code behaves when handed them.
"""

from __future__ import annotations

import pytest
import requests

from ffsft.deploy.preflight import (
    online_endpoint_blocker,
    read_sku_availability,
    read_storage_reachability,
    storage_blocker,
)
from ffsft.deploy.probes import _key_based_datastores, classify_store

ACCOUNT = "stffsftplc"
HEAD = {"Authorization": "Bearer fake"}

_STORAGE = {
    "name": ACCOUNT,
    "properties": {
        "publicNetworkAccess": "Enabled",
        "networkAcls": {"bypass": "None"},
        "privateEndpointConnections": [],
        # Measured hardened. This is the half that makes one surviving
        # AccountKey datastore fatal, and it is measured in every case below.
        "allowSharedKeyAccess": False,
    },
}
_WORKSPACE = {
    "properties": {
        "storageAccount": f"/subscriptions/s/resourceGroups/rg/providers/x/{ACCOUNT}",
        "managedNetwork": {"isolationMode": "Disabled"},
    }
}


def _store(name: str, kind: str) -> dict:
    return {"name": name, "properties": {"credentials": {"credentialsType": kind}}}


#: Ground truth of the fake workspace: four datastores, and the last one still
#: authenticates with an account key. That is the docs/JOURNAL.md S57.8 blocker.
_TRUTH = [
    _store("workspaceblobstore", "None"),
    _store("workspacefilestore", "None"),
    _store("workspaceworkingdirectory", "None"),
    _store("workspaceartifactstore", "AccountKey"),
]
_PAGE_SIZE = 3


class _Response:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"{self.status_code} Client Error")

    def json(self):
        return self._body


def _arm(listing_body):
    def get(url, headers=None, timeout=None, params=None):
        if "/datastores?" in url:
            if callable(listing_body):
                return listing_body(url)
            return _Response(listing_body)
        if "Microsoft.MachineLearningServices/workspaces/" in url:
            return _Response(_WORKSPACE)
        return _Response(_STORAGE)

    return get


def _paginated(url):
    """Page 1 carries a `nextLink`; the AccountKey datastore is on page 2."""
    if "$skip=" in url:
        return _Response({"value": _TRUTH[_PAGE_SIZE:]})
    return _Response({"value": _TRUTH[:_PAGE_SIZE], "nextLink": f"{url}&$skip=eyJ0In0"})


class _Credential:
    def get_token(self, *scopes):
        return type("Token", (), {"token": "fake"})()


class _Target:
    subscription_id = "00000000-0000-0000-0000-000000000000"
    resource_group = "rg-ffsft-plc"
    workspace_name = "mlw-ffsft-plc"


@pytest.fixture
def arm(monkeypatch):
    def install(listing_body):
        monkeypatch.setattr(requests, "get", _arm(listing_body))

    return install


# -- the control: one page, and both files answer correctly ----------------


def test_one_page_carrying_every_datastore_is_read_correctly_by_both_files(arm):
    """Not a formality -- it is the other half of the A/B above.

    Without it, a test that only shows the paginated run failing cannot tell
    "the listing was truncated" from "the fake is wrong".
    """
    arm({"value": _TRUTH})
    state = read_storage_reachability(_Target(), credential=_Credential())
    assert state.key_auth_refused is True
    assert storage_blocker(state) is not None
    assert _key_based_datastores("https://arm/x", "mlw-ffsft-plc", HEAD) == [
        "workspaceartifactstore"
    ]


# -- site 1, preflight: the half that gates a $4.959/hr rollout -------------


def test_a_datastore_listing_that_arrived_in_pages_is_not_a_complete_one(arm):
    """Round 7 CLOSED this. It used to be `[]` -- the value round 6 documents
    as "the GET succeeded and found none", claimed about four datastores from a
    read that saw three. `read_all_arm_pages` follows `nextLink`, so the
    AccountKey store on page 2 is seen and named."""
    arm(_paginated)
    state = read_storage_reachability(_Target(), credential=_Credential())
    assert state.key_based_datastores != []
    assert state.key_based_datastores == ["workspaceartifactstore"]


def test_a_paginated_listing_does_not_let_the_deploy_spend_on_a_refusing_account(arm):
    """Round 7 CLOSED this. The paginated run used to record
    PUT online_deployments/blue instance_type=Standard_NV12ads_A10_v5 count=1;
    now it refuses, because page 2 is read and the AccountKey store is found."""
    arm(_paginated)
    state = read_storage_reachability(_Target(), credential=_Credential())
    assert storage_blocker(state) is not None


def test_an_empty_first_page_beside_a_continuation_is_not_a_measured_empty_listing(arm):
    """Round 7 CLOSED this. The continuation here leads to a body carrying no
    `value` list at all, which states nothing about the collection -- so the
    listing is refused rather than truncated silently to the empty page 1."""
    arm({"value": [], "nextLink": "https://management.azure.com/x?&$skip=eyJ0In0"})
    state = read_storage_reachability(_Target(), credential=_Credential())
    assert state.key_based_datastores is None


# -- site 2, probes: the half that gates the exit code ---------------------


def test_probes_does_not_report_a_truncated_listing_as_a_measured_empty_one(arm):
    """Round 7 CLOSED this, and closed it BETTER than this test first asked.

    Written as an audit pin it asserted `is None` -- report the gap. The fix
    that landed follows the continuation instead, so the honest assertion is
    the stronger one: probes now answers the WHOLE listing, and the
    AccountKey store on page 2 is named rather than merely mourned. The `None`
    branch is still the answer when a continuation genuinely cannot be read,
    and that is the test directly below; splitting them keeps "followed it" and
    "could not follow it" from sharing one assertion again.
    """
    arm(_paginated)
    assert _key_based_datastores("https://arm/x", "mlw-ffsft-plc", HEAD) == [
        "workspaceartifactstore"
    ]


def test_a_continuation_that_cannot_be_read_is_not_a_short_listing(arm):
    """The half the round-6 sentinel already had, one page later.

    Page 1 is a clean 200 carrying three datastores and a `nextLink`; the
    continuation 403s. Answering `["..."]` from page 1 would be a listing that
    stopped early reported as a listing that was short -- with the AccountKey
    store, the one that matters, on the page nobody could read.
    """

    def listing(url):
        if "$skip=" in url:
            return _Response({"error": "AuthorizationFailed"}, status=403)
        return _Response({"value": _TRUTH[:_PAGE_SIZE], "nextLink": f"{url}&$skip=eyJ0In0"})

    arm(listing)
    assert _key_based_datastores("https://arm/x", "mlw-ffsft-plc", HEAD) is None

    state = read_storage_reachability(_Target(), credential=_Credential())
    assert state.key_based_datastores is None
    assert state.key_auth_refused is None
    # And the deploy that would have spent on it does not.
    assert storage_blocker(state) is not None


def test_a_listing_that_ends_without_a_continuation_is_still_a_measurement(arm):
    """The over-correction guard. `{"value": []}` and no `nextLink` is ARM
    saying the collection is empty, which is exactly the measurement the whole
    sentinel exists to keep distinguishable from silence. It must survive."""
    arm({"value": []})
    assert _key_based_datastores("https://arm/x", "mlw-ffsft-plc", HEAD) == []

    state = read_storage_reachability(_Target(), credential=_Credential())
    assert state.key_based_datastores == []
    assert state.key_auth_refused is False
    assert storage_blocker(state) is None


def test_the_credential_axis_is_not_answered_from_a_truncated_listing(arm):
    """Round 7 CLOSED this: the continuation is followed, so the axis is
    answered from the WHOLE listing -- `True`, not the `False` a truncated read
    produced."""
    arm(_paginated)
    probe = classify_store(
        ACCOUNT,
        "Enabled",
        0,
        allow_shared_key=False,
        key_based_datastores=_key_based_datastores("https://arm/x", "mlw", HEAD),
    )
    assert probe.key_auth_refused is not False


# -- the same class, same file, on the other expensive read ----------------
#
# `Microsoft.Compute/skus` is paginated too (published schema
# `ResourceSkusResult`: `nextLink`, "The link to the next page of items"; the
# SDKs expose it as a pager). `read_sku_availability` reads one page, and on
# not finding the SKU takes the FULL-SCAN NEGATIVE branch -- it logs "SKU %s is
# not offered at all in %s" and returns the same object a total read failure
# returns. `online_endpoint_blocker` then refuses nothing. The cost of not
# enforcing that field is in its own docstring: five rollouts, no node, no
# container log, 50-113 minutes each.

_SKU = "Standard_NV12ads_A10_v5"
_REGION = "koreacentral"
_RESTRICTED = {
    "name": _SKU,
    "locationInfo": [{"location": _REGION, "zones": ["1", "2", "3"]}],
    "restrictions": [
        {
            "type": "Location",
            "reasonCode": "NotAvailableForSubscription",
            "restrictionInfo": {"locations": [_REGION]},
        }
    ],
}
_FILLER = [
    {"name": f"Standard_D{i}s_v5", "locationInfo": [], "restrictions": []} for i in range(3)
]


def _skus(paginated):
    def get(url, params=None, headers=None, timeout=None):
        if paginated:
            return _Response({"value": _FILLER, "nextLink": f"{url}&$skiptoken=abc"})
        return _Response({"value": [*_FILLER, _RESTRICTED]})

    return get


def test_a_sku_on_the_only_page_is_still_read_and_still_refused(monkeypatch):
    monkeypatch.setattr(requests, "get", _skus(paginated=False))
    state = read_sku_availability("sub", _REGION, _SKU, credential=_Credential())
    assert state.region_blocked is True
    assert online_endpoint_blocker(state) is not None


def test_a_restricted_sku_past_the_first_page_is_not_reported_as_not_offered(monkeypatch):
    """Round 7 CLOSED this. preflight.py used to read one page and then log
    "SKU is not offered at all in <region>" -- a full-scan negative from a
    partial scan -- and return the same object a total read failure returns, so
    the rollout proceeded in silence. `scan_complete=False` now carries the
    gap, and the one caller with no LowPriority fallback refuses on it."""
    monkeypatch.setattr(requests, "get", _skus(paginated=True))
    state = read_sku_availability("sub", _REGION, _SKU, credential=_Credential())
    assert online_endpoint_blocker(state) is not None
