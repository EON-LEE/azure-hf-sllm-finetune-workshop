"""S79 gave a truncated roleAssignments listing a third state. Nothing checked
it was ever *said out loud*.

`identity_unread_note` is well tested as a function. Its only delivery to a
human is one line in `deploy/endpoint.py`:

    unread = identity_unread_note(grants) if grants else None
    if unread:
        log.warning("%s", unread)

Nothing in the suite drove `deploy_online` through that branch. Measured by
mutation against the tree as it shipped -- one word changed, `log.warning` ->
`log.debug`, and nothing else:

    1142 passed, 2 skipped, 1 xfailed in 8.73s

and the same fake ARM that truncates page 2 then produced:

    deploy_online : RETURNED, deployments created=1
    ARM PUTs      : 0
    UNKNOWN note logged as WARNING : False

A deploy that could not read the grants went out with the operator told
nothing. That is the silence half of the same invariant the tri-state exists to
break: not refusing over an unread listing is only half the bargain, and the
other half only exists if it reaches somebody. `SkuProbe.probed` and
`format_inventory` are both pinned at the point where they PRINT, for this
reason; this one was pinned only at the point where it decides.

The two over-correction cases are driven through the same door on purpose. A
unit test on `identity_blocker` cannot show that `deploy_online` still refuses,
because `deploy_online` is what holds the `force` flag and the `raise`.

EVERY ARM RESPONSE HERE IS A FAKE built at the `requests` boundary. This repo
has no Azure access and no shape below was observed on a live subscription;
what is proven is how our own code behaves when handed them.
"""

from __future__ import annotations

import json
import logging
import types

import azure.identity
import pytest
import requests

from ffsft import azure_ml
from ffsft.deploy import endpoint as ep
from ffsft.deploy import identity as ident
from ffsft.deploy import preflight as pf
from ffsft.deploy.preflight import TruncatedListing, read_all_arm_pages
from ffsft.deploy.registry import get_serving_registry

SUB = "00000000-0000-0000-0000-0000000000ff"
PRINCIPAL = "11111111-2222-3333-4444-555555555555"
ENDPOINT = "ffsft-smoke2"
IMAGE = "acrffsftkc.azurecr.io/ffsft-serve:1"
ACR_ID = (
    f"/subscriptions/{SUB}/resourceGroups/rg-ffsft-kc"
    f"/providers/Microsoft.ContainerRegistry/registries/acrffsftkc"
)
SA_ID = (
    f"/subscriptions/{SUB}/resourceGroups/rg-ffsft-kc"
    f"/providers/Microsoft.Storage/storageAccounts/stffsftkc"
)
_READER_GUID = "acdd72a7-3385-48ef-bd42-f606fba81ae7"
_STORAGE_READ_GUID = "2a2b9908-6ea1-4ae2-8e65-a410df84e7d1"


def _assignment(guid: str) -> dict:
    return {
        "properties": {
            "roleDefinitionId": (
                f"/subscriptions/{SUB}/providers/Microsoft.Authorization"
                f"/roleDefinitions/{guid}"
            )
        }
    }


#: Ground truth of the fake registry: the identity DOES hold AcrPull, and it is
#: the last row, so it is the one that falls off the end of page 1.
_ACR_TRUTH = [_assignment(_READER_GUID), _assignment(ident.ACR_PULL_ROLE_GUID)]


class _Response:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status
        self.text = json.dumps(body)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"{self.status_code} Client Error")

    def json(self):
        return self._body


class _Credential:
    def get_token(self, *_scopes):
        return types.SimpleNamespace(token="fake")


class _Poller:
    def __init__(self, entity):
        self._entity = entity

    def result(self, *_a, **_k):
        return self._entity


class _FakeDeployments:
    def __init__(self):
        self.created: list = []

    def get(self, *_a, **_k):
        raise RuntimeError("(UserError) Deployment blue not found")

    def begin_create_or_update(self, deployment):
        self.created.append(deployment)
        return _Poller(deployment)

    def begin_delete(self, *_a, **_k):
        return _Poller(None)


class _FakeEndpoints:
    def __init__(self):
        self._ep = types.SimpleNamespace(
            traffic={},
            identity=types.SimpleNamespace(principal_id=PRINCIPAL),
            scoring_uri="https://fake/score",
        )

    def get(self, *_a, **_k):
        return self._ep

    def begin_create_or_update(self, entity):
        return _Poller(entity)


class _FakeClient:
    def __init__(self):
        self.online_deployments = _FakeDeployments()
        self.online_endpoints = _FakeEndpoints()
        self.environments = types.SimpleNamespace(create_or_update=lambda e: e)


def _arm(acr_listing, sa_listing=None):
    """Route the GETs `read_identity_grants` and `ArmRoleAuth` make."""
    sa_listing = sa_listing or (
        lambda _url: _Response({"value": [_assignment(_STORAGE_READ_GUID)]})
    )

    def get(url, headers=None, timeout=None, params=None):
        if "/roleAssignments" in url:
            listing = acr_listing if "/registries/" in url else sa_listing
            return listing(url)
        if "/onlineEndpoints/" in url:
            return _Response({"identity": {"principalId": PRINCIPAL}})
        return _Response({"properties": {"containerRegistry": "", "storageAccount": SA_ID}})

    return get


def _paginated(url):
    """AcrPull is on page 2. ARM's choice of page boundary, not the caller's."""
    if "$skipToken=" in url:
        return _Response({"value": _ACR_TRUTH[1:]})
    return _Response({"value": _ACR_TRUTH[:1], "nextLink": f"{url}&$skipToken=eyJ0In0"})


def _unfollowable(url):
    """Page 1 is a clean 200 carrying a `nextLink`; the continuation 403s."""
    if "$skipToken=" in url:
        return _Response({"error": "AuthorizationFailed"}, status=403)
    return _Response({"value": _ACR_TRUTH[:1], "nextLink": f"{url}&$skipToken=eyJ0In0"})


@pytest.fixture
def run_deploy(monkeypatch):
    """Drive the REAL `deploy_online`, `read_identity_grants` and `ArmRoleAuth`.

    Only `requests`, the credential and the ML client are faked. Patching
    `ffsft.deploy.identity.read_identity_grants` -- which is what every other
    `deploy_online` test does -- would fake the very seam under test.
    """

    def go(acr_listing, sa_listing=None, put_status=403):
        monkeypatch.setenv("FFSFT_SUBSCRIPTION_ID", SUB)
        monkeypatch.setenv("FFSFT_TENANT_ID", "00000000-0000-0000-0000-0000000000ee")
        monkeypatch.setenv("FFSFT_RESOURCE_GROUP", "rg-ffsft-kc")
        monkeypatch.setenv("FFSFT_WORKSPACE", "mlw-ffsft")

        puts: list[str] = []
        monkeypatch.setattr(requests, "get", _arm(acr_listing, sa_listing))
        monkeypatch.setattr(
            requests,
            "put",
            lambda url, **_k: (puts.append(url), _Response({"e": "denied"}, put_status))[1],
        )
        monkeypatch.setattr(
            azure.identity, "DefaultAzureCredential", lambda *_a, **_k: _Credential()
        )

        spec = get_serving_registry().get("aml_online_vllm")
        monkeypatch.setattr(ep, "check_pattern", lambda *_a, **_k: (spec, None))
        monkeypatch.setattr(ep, "ensure_endpoint", lambda *_a, **_k: None)
        monkeypatch.setattr(
            ep, "serve_environment", lambda _c, image: types.SimpleNamespace(image=image)
        )
        monkeypatch.setattr(pf, "read_storage_reachability", lambda *_a, **_k: None)
        monkeypatch.setattr(pf, "read_sku_availability", lambda *_a, **_k: None)
        monkeypatch.setattr(pf, "online_endpoint_blocker", lambda *_a, **_k: None)
        monkeypatch.setattr(pf, "sku_advisory", lambda *_a, **_k: None)
        # The registry lookup is a separate ARM listing with its own tests; fix
        # it so this file measures only the roleAssignments read.
        monkeypatch.setattr(ident, "acr_id_for_image", lambda *_a, **_k: ACR_ID)

        client = _FakeClient()
        monkeypatch.setattr(azure_ml, "get_ml_client", lambda *_a, **_k: client)

        raised = None
        try:
            ep.deploy_online(ENDPOINT, None, hf_model="Qwen/Qwen3-0.6B", image=IMAGE)
        except RuntimeError as exc:  # noqa: BLE001 - the refusal is the measurement
            raised = exc
        return raised, client, puts

    return go


def _unread_warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and "could NOT be read to the end" in r.getMessage()
    ]


def test_a_deploy_over_a_grant_listing_nobody_could_finish_says_so_where_the_operator_looks(
    run_deploy, caplog
):
    """The note has to be a WARNING on the deploy path, not a value in a record.

    `log.warning` -> `log.debug` on that one line left the whole suite green
    before this test existed, and took the only sentence an operator would ever
    see with it.
    """
    with caplog.at_level(logging.DEBUG, logger="ffsft.deploy.endpoint"):
        raised, client, _puts = run_deploy(_unfollowable)

    assert raised is None
    (note,) = _unread_warnings(caplog)
    assert ident.ACR_PULL in note
    assert ACR_ID in note
    assert "az role assignment list" in note
    # The deploy went out; the operator was told which grant nobody could read.
    assert len(client.online_deployments.created) == 1


def test_a_deploy_over_a_grant_listing_nobody_could_finish_is_not_refused_and_writes_no_rbac(
    run_deploy, caplog
):
    """The sign-flipped half: refusing here costs a deployment that would have
    worked, and granting here writes RBAC on rows nobody read."""
    with caplog.at_level(logging.DEBUG, logger="ffsft.deploy.endpoint"):
        raised, client, puts = run_deploy(_unfollowable)

    assert raised is None
    assert puts == [], "an unread listing authorised a role assignment write"
    assert len(client.online_deployments.created) == 1


def test_a_grant_arm_returned_on_page_two_lets_the_whole_deploy_through(run_deploy, caplog):
    """The control, end to end: the grant is there, so nothing is refused and
    nothing is reported UNKNOWN."""
    with caplog.at_level(logging.DEBUG, logger="ffsft.deploy.endpoint"):
        raised, client, puts = run_deploy(_paginated)

    assert raised is None
    assert _unread_warnings(caplog) == []
    assert puts == [], "AcrPull was already held; nothing needed writing"
    assert len(client.online_deployments.created) == 1


def test_a_complete_listing_that_lacks_acr_pull_still_refuses_the_whole_deploy(
    run_deploy, caplog
):
    """The over-correction guard where it actually bites. `identity_blocker`
    returning a string proves nothing about whether `deploy_online` raises it --
    that is where `force` and the `raise` live, and it is the ~$8, four-hour,
    no-logs failure this module was written for."""
    with caplog.at_level(logging.DEBUG, logger="ffsft.deploy.endpoint"):
        raised, client, _puts = run_deploy(lambda _u: _Response({"value": [_assignment(
            _READER_GUID
        )]}))

    assert raised is not None
    assert ident.ACR_PULL in str(raised)
    assert _unread_warnings(caplog) == [], "a complete listing is not an unread one"
    assert client.online_deployments.created == []


def test_a_nextlink_that_points_at_a_page_already_fetched_is_a_truncated_listing(monkeypatch):
    """The cycle guard was unpinned: deleting it left 1142 passed, 2 skipped,
    1 xfailed. A server that hands back a `nextLink` it has already served has
    not ended the listing, and the rows collected so far are a partial read --
    never a short one."""
    base = "https://management.azure.com/x?api-version=2022-04-01"
    seen: list[str] = []

    def get(url, headers=None, timeout=None, params=None):
        seen.append(url)
        return _Response(
            {
                "value": [_assignment(_READER_GUID)],
                "nextLink": base if url != base else f"{base}&p=2",
            }
        )

    monkeypatch.setattr(requests, "get", get)
    with pytest.raises(TruncatedListing, match="already served"):
        read_all_arm_pages(requests, base, headers={"Authorization": "Bearer fake"})
    assert len(seen) == 2, "it kept walking a listing that had started repeating"
