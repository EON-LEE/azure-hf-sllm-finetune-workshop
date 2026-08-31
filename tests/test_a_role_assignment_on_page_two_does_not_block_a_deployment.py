"""An AcrPull grant ARM returned on page 2 refused a deployment that worked.

Six rounds hunted the swallow shape -- an `except` that hands a caller an empty
value. This is the *other* half of the same invariant and it is SIGN-FLIPPED:
nothing raises, HTTP 200 all the way, and the rows nobody read become a
**finding** rather than a silence.

`deploy/identity.py` asks ARM for the role assignments of one principal:

    GET {scope}/providers/Microsoft.Authorization/roleAssignments
        ?api-version=2022-04-01&$filter=principalId eq '{pid}'

Microsoft's published reference for that operation *at that api-version* types
the 200 body as `RoleAssignmentListResult`:

    nextLink : string (uri)      "The link to the next page of items"
    value    : RoleAssignment[]  "The RoleAssignment items on this page"

-- learn.microsoft.com/rest/api/authorization/role-assignments/list-for-scope
   ?view=rest-authorization-2022-04-01

"on this page". Both call sites read `resp.json().get("value", [])` and drop
`nextLink`, so page 1 was the whole list. `acr_id_for_image`, in the SAME FILE,
already goes through `read_all_arm_pages`.

Executed against the fake below -- identical fake registry, identical two
assignments, the only variable being whether ARM handed them over in one page
or two, which is ARM's choice and not the caller's:

    ARM ONE page   read_identity_grants -> acr_roles ['Reader', 'AcrPull']
                   can_pull_image True   -> identity_blocker None -> DEPLOYS
    ARM PAGINATED  read_identity_grants -> acr_roles ['Reader']
                   can_pull_image False  -> identity_blocker BLOCKS
                   "endpoint 'ffsft-smoke2' has a managed identity ... missing:
                      - AcrPull on the container registry (cannot pull the image)"

The grant is there. ARM said so. The tool refused the deploy over a page it
never asked for -- the failure mode CLAUDE.md prices as "blocks a deployment
that would have worked", which on this path is a workshop participant told to
go chase an RBAC problem that does not exist.

WHAT IS MEASURED AND WHAT IS NOT. This repo has no Azure access. The schema
above is quoted from Microsoft's reference; every response in this file is a
FAKE built at the `requests` boundary and no shape here was observed on a live
subscription. What is proven is how our code behaves when handed them. Two
caveats kept deliberately, because a fake that is not checked against reality
is how a round ships a false finding:

  * That doc also says `$skipToken` is "Only supported on provider level calls",
    so *client-driven* continuation at a resource scope may not exist. It does
    not say the server never emits `nextLink`, and the response model in the
    same document says it may. Following a `nextLink` that never arrives is a
    no-op: `read_all_arm_pages` on a body with no `nextLink` returns exactly
    what `.get("value", [])` returned. The change cannot cost anything.
  * The pagination question is not the only leg, and the second leg needs no
    pagination at all. `.get("value", [])` also turns a body that is not a
    `RoleAssignmentListResult` -- `value` absent, `null`, or not a list -- into
    "measured: this identity holds no roles". This workspace has already
    measured ARM answering a refusal as prose at HTTP 200 on the container
    `getLogs` path (`deploy/logs.py`). Same 200, same `[]`, same refusal.
"""

from __future__ import annotations

import json
import re
import uuid

import pytest
import requests

from ffsft.deploy.identity import (
    ACR_PULL,
    ACR_PULL_ROLE_GUID,
    STORAGE_READ,
    STORAGE_WRITE,
    ArmRoleAuth,
    IdentityGrants,
    ensure_role,
    identity_blocker,
    identity_unread_note,
    read_identity_grants,
)

SUB = "00000000-0000-0000-0000-000000000000"
PRINCIPAL = "11111111-2222-3333-4444-555555555555"
ENDPOINT = "ffsft-smoke2"
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
    """One `RoleAssignment`, trimmed to the two fields this code reads."""
    return {
        "name": str(uuid.uuid4()),
        "type": "Microsoft.Authorization/roleAssignments",
        "properties": {
            "roleDefinitionId": (
                f"/subscriptions/{SUB}/providers/Microsoft.Authorization"
                f"/roleDefinitions/{guid}"
            ),
            "principalId": PRINCIPAL,
            "principalType": "ServicePrincipal",
        },
    }


#: Ground truth of the fake registry: the endpoint identity holds Reader AND
#: AcrPull on it. AcrPull is deliberately the LAST row, because ARM's paging
#: boundary is ARM's business and the grant that matters is the one that falls
#: off the end.
_ACR_TRUTH = [_assignment(_READER_GUID), _assignment(ACR_PULL_ROLE_GUID)]
_SA_TRUTH = [_assignment(_STORAGE_READ_GUID)]


class _Response:
    def __init__(self, body, status=200, text=""):
        self._body = body
        self.status_code = status
        self.text = text or json.dumps(body)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"{self.status_code} Client Error")

    def json(self):
        return self._body


class _Credential:
    def get_token(self, *scopes):
        return type("Token", (), {"token": "fake"})()


class _Target:
    subscription_id = SUB
    resource_group = "rg-ffsft-kc"
    workspace_name = "mlw-ffsft"


_WORKSPACE = {
    "properties": {
        # Empty on purpose: this workspace has no linked registry, which is the
        # whole reason the AcrPull grant has to be explicit. S57.
        "containerRegistry": "",
        "storageAccount": SA_ID,
    }
}


def _arm(acr_listing, sa_listing=None):
    """Route the four GETs `read_identity_grants` makes."""
    sa_listing = sa_listing if sa_listing is not None else {"value": _SA_TRUTH}

    def get(url, headers=None, timeout=None, params=None):
        if "/roleAssignments" in url:
            listing = acr_listing if "/registries/" in url else sa_listing
            return listing(url) if callable(listing) else _Response(listing)
        if "/onlineEndpoints/" in url:
            return _Response({"identity": {"principalId": PRINCIPAL}})
        return _Response(_WORKSPACE)

    return get


def _one_page(url):
    return _Response({"value": _ACR_TRUTH})


def _paginated(url):
    """Page 1 carries a `nextLink`; AcrPull is on page 2."""
    if "$skipToken=" in url:
        return _Response({"value": _ACR_TRUTH[1:]})
    return _Response({"value": _ACR_TRUTH[:1], "nextLink": f"{url}&$skipToken=eyJ0In0"})


def _unfollowable(url):
    """Page 1 is a clean 200 with a `nextLink`; the continuation 403s."""
    if "$skipToken=" in url:
        return _Response({"error": "AuthorizationFailed"}, status=403)
    return _Response({"value": _ACR_TRUTH[:1], "nextLink": f"{url}&$skipToken=eyJ0In0"})


def _prose_at_200(url):
    """ARM answering with something that is not a list result, at HTTP 200.

    Not invented for this test: `deploy/logs.py` exists because this workspace
    measured Azure answering a refusal with prose at 200 on the container log
    path. `.get("value", [])` reads this as "the identity holds no roles".
    """
    return _Response({"error": {"code": "AuthorizationFailed", "message": "no"}})


@pytest.fixture
def arm(monkeypatch):
    def install(acr_listing, sa_listing=None):
        monkeypatch.setattr(requests, "get", _arm(acr_listing, sa_listing))

    return install


# -- the control: one page, and the deploy goes ahead -------------------------


def test_a_registry_listing_that_fits_one_page_is_read_and_lets_the_deploy_go(arm):
    """The other half of the A/B. Without it a paginated run that blocks cannot
    be told from a fake that is simply wrong."""
    arm(_one_page)
    grants = read_identity_grants(_Target(), ENDPOINT, ACR_ID, credential=_Credential())
    assert grants.acr_roles == ["Reader", ACR_PULL]
    assert grants.can_pull_image is True
    assert identity_blocker(grants) is None
    assert identity_unread_note(grants) is None


# -- the defect ---------------------------------------------------------------


def test_an_acr_pull_grant_arm_returned_on_page_two_is_not_a_missing_grant(arm):
    """The grant exists, ARM said so, and the tool refused the deploy over it."""
    arm(_paginated)
    grants = read_identity_grants(_Target(), ENDPOINT, ACR_ID, credential=_Credential())
    assert grants.acr_roles == ["Reader", ACR_PULL]
    assert grants.can_pull_image is True
    assert identity_blocker(grants) is None


def test_a_deployment_is_not_refused_over_a_page_of_grants_nobody_could_read(arm):
    """The unread case, which is the one this round has to *decide*, not just fix.

    Page 1 is a clean 200 carrying Reader and a `nextLink`; the continuation
    403s. "Could not look" here means the tool cannot prove AcrPull is absent,
    and refusing on a value nobody measured is the sign-flipped half of the
    invariant -- it costs a deployment that would have worked.
    """
    arm(_unfollowable)
    grants = read_identity_grants(_Target(), ENDPOINT, ACR_ID, credential=_Credential())
    assert grants.acr_roles is None
    assert grants.can_pull_image is None
    assert identity_blocker(grants) is None


def test_a_truncated_grant_listing_is_said_out_loud_rather_than_passed_off_as_a_grant(arm):
    """...and the other direction of the same rule: not blocking is not the same
    as claiming the grant is present. The gap is named, with the scope it is
    about, in the same COULD NOT LOOK vocabulary `probe_report` and
    `format_inventory` already use."""
    arm(_unfollowable)
    grants = read_identity_grants(_Target(), ENDPOINT, ACR_ID, credential=_Credential())
    note = identity_unread_note(grants)
    assert note is not None
    assert "could not" in note.lower()
    assert ACR_ID in note
    assert ACR_PULL in note
    # The storage half WAS read, so the note must not smear the doubt over it.
    assert STORAGE_READ not in note


def test_a_role_listing_that_is_not_a_list_result_is_not_an_identity_with_no_roles(arm):
    """The leg that needs no pagination at all: `value` absent at HTTP 200.

    `.get("value", [])` turned this into "measured: holds no roles" and blocked.
    Nothing was stated about the collection, so nothing may be concluded.
    """
    arm(_prose_at_200)
    grants = read_identity_grants(_Target(), ENDPOINT, ACR_ID, credential=_Credential())
    assert grants.acr_roles is None
    assert identity_blocker(grants) is None
    assert identity_unread_note(grants) is not None


# -- the over-correction guard: a real finding must still be a finding --------


def test_a_complete_listing_that_genuinely_lacks_acr_pull_still_blocks(arm):
    """`{"value": [...]}` with no `nextLink` is ARM stating the whole collection.

    This is the failure the module was written for -- ~$8 and four hours of two
    endpoints sitting in `Creating` with no logs. Un-blocking it to fix the
    paging bug would trade one expensive mistake for the other one.
    """
    arm(lambda url: _Response({"value": [_assignment(_READER_GUID)]}))
    grants = read_identity_grants(_Target(), ENDPOINT, ACR_ID, credential=_Credential())
    assert grants.acr_roles == ["Reader"]
    assert grants.can_pull_image is False
    blocker = identity_blocker(grants)
    assert blocker is not None and ACR_PULL in blocker
    assert identity_unread_note(grants) is None


def test_a_measured_empty_role_listing_is_still_a_measurement(arm):
    """`{"value": []}` and no `nextLink`: ARM saying the principal holds nothing
    here. That is the measurement the whole sentinel exists to keep telling
    apart from silence, and it must keep blocking."""
    arm(lambda url: _Response({"value": []}))
    grants = read_identity_grants(_Target(), ENDPOINT, ACR_ID, credential=_Credential())
    assert grants.acr_roles == []
    assert grants.can_pull_image is False
    assert identity_blocker(grants) is not None
    assert identity_unread_note(grants) is None


def test_the_storage_half_is_judged_on_its_own_reading(arm):
    """A truncated registry listing must not suppress a real storage finding.

    Per-scope status, not one flag for the whole dataclass: `SectionScan` keeps
    the status next to the rows it explains for exactly this reason.
    """
    arm(_unfollowable, sa_listing={"value": []})
    grants = read_identity_grants(_Target(), ENDPOINT, ACR_ID, credential=_Credential())
    assert grants.acr_roles is None and grants.storage_roles == []
    blocker = identity_blocker(grants)
    assert blocker is not None
    assert STORAGE_READ in blocker
    # ...and it must not also claim the registry grant is missing. Round 9
    # closed the loophole this comment used to describe: the footer named both
    # roles unconditionally, so the message refused to call the registry grant
    # missing in the bullets and then printed the `az` command that grants it
    # anyway (S81.5). Findings and remedies are now one list, so neither the
    # BULLET nor the COMMAND may name the registry here.
    assert f"{ACR_PULL} on the container registry" not in blocker
    assert f'--role "{ACR_PULL}"' not in blocker, blocker
    assert ACR_ID in identity_unread_note(grants)


def test_a_hand_built_grants_record_still_means_measured_empty():
    """The dataclass default stays `[]`, so every existing caller and test that
    builds `IdentityGrants(acr_roles=[])` keeps meaning "looked, saw none".
    Only the reader that failed to read sets the `None`."""
    grants = IdentityGrants(endpoint_name=ENDPOINT, principal_id=PRINCIPAL)
    assert grants.acr_roles == [] and grants.storage_roles == []
    assert grants.can_pull_image is False
    assert identity_blocker(grants) is not None
    assert identity_unread_note(grants) is None


# -- the same defect on the write path's read --------------------------------


class _Auth(ArmRoleAuth):
    def _headers(self):
        return {"Authorization": "Bearer fake"}


def test_a_grant_on_page_two_is_not_re_granted_as_a_missing_one(monkeypatch):
    """`ArmRoleAuth.list_roles` fed `ensure_role`, so a truncated read there
    meant "not held" and sent a PUT. ARM answers an existing assignment with
    409 RoleAssignmentExists, which `ensure_role` then reports to the operator
    as a failed grant -- a permissions problem invented out of a short read."""
    monkeypatch.setattr(requests, "get", _arm(_paginated))
    puts: list = []
    monkeypatch.setattr(requests, "put", lambda *a, **k: puts.append((a, k)))

    got = ensure_role(ACR_ID, PRINCIPAL, ACR_PULL, auth=_Auth())
    assert got.already_had and not got.granted
    assert got.error is None
    assert puts == []


def test_a_grant_listing_that_stopped_short_does_not_authorise_a_write(monkeypatch):
    """"A caller may not act on the absence of rows it never managed to read."
    Writing an RBAC assignment is acting. The honest answer is the `az` command,
    which is what `ensure_role`'s existing could-not-look handler already gives."""
    monkeypatch.setattr(requests, "get", _arm(_unfollowable))
    puts: list = []
    monkeypatch.setattr(requests, "put", lambda *a, **k: puts.append((a, k)))

    got = ensure_role(ACR_ID, PRINCIPAL, ACR_PULL, auth=_Auth())
    assert puts == []
    assert not got.granted and not got.already_had
    assert "could not read role assignments" in (got.error or "")
    assert ACR_PULL in (got.manual_fix or "")


# -- the RBAC write itself, which no test had ever executed ------------------
#
# Measured with `coverage` against the suite as it stood: of `create_role`'s
# body only the `_ROLE_GUIDS` lookup and its `ValueError` ran, from
# `test_an_unknown_role_is_refused_rather_than_written_as_acrpull`. Lines 389
# through 411 -- the request body and the `requests.put` that sends it -- were
# never executed. It is the only RBAC-granting write in this repository.


class _PutRecorder:
    def __init__(self, status=201, text="{}"):
        self.calls: list[dict] = []
        self.status = status
        self.text = text

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "body": json, "timeout": timeout})
        return _Response({}, status=self.status, text=self.text)


class _EmptyButComplete(_Auth):
    """Reads an empty but COMPLETE listing, so `ensure_role` reaches the PUT."""

    def list_roles(self, scope, principal_id):
        return []


def test_the_role_assignment_write_names_the_scope_the_caller_asked_for(monkeypatch):
    put = _PutRecorder()
    monkeypatch.setattr(requests, "put", put)

    _Auth().create_role(ACR_ID, PRINCIPAL, ACR_PULL)

    (call,) = put.calls
    assert call["url"].startswith(f"https://management.azure.com{ACR_ID}/providers/")
    assert "/Microsoft.Authorization/roleAssignments/" in call["url"]
    assert call["url"].endswith("?api-version=2022-04-01")


def test_the_role_assignment_write_names_the_role_the_caller_asked_for(monkeypatch):
    """A wrong `roleDefinitionId` grants a real principal a real, wrong power,
    and the caller is told it succeeded."""
    put = _PutRecorder()
    monkeypatch.setattr(requests, "put", put)

    _Auth().create_role(SA_ID, PRINCIPAL, STORAGE_WRITE)

    props = put.calls[0]["body"]["properties"]
    assert props["roleDefinitionId"] == (
        f"/subscriptions/{SUB}/providers/Microsoft.Authorization"
        f"/roleDefinitions/ba92f5b4-2d11-453d-a403-e96b0029c9fe"
    )
    assert ACR_PULL_ROLE_GUID not in props["roleDefinitionId"]
    assert props["principalId"] == PRINCIPAL
    # Without it ARM fails the lookup while a fresh managed identity is still
    # replicating into Entra.
    assert props["principalType"] == "ServicePrincipal"


def test_the_role_definition_is_scoped_to_the_subscription_in_the_scope(monkeypatch):
    """The subscription is parsed out of `scope`, not out of the caller's env.

    `acr_id_for_image` looks the registry up across the subscription and can
    hand back an id from a different resource group than the one assumed; a
    roleDefinitionId built from somewhere else is a 404 that `ensure_role`
    reports as a permissions problem which does not exist.
    """
    put = _PutRecorder()
    monkeypatch.setattr(requests, "put", put)
    other = "99999999-8888-7777-6666-555555555555"

    _Auth().create_role(
        f"/subscriptions/{other}/resourceGroups/rg/providers/x/y", PRINCIPAL, ACR_PULL
    )

    assert put.calls[0]["body"]["properties"]["roleDefinitionId"].startswith(
        f"/subscriptions/{other}/providers/"
    )


def test_each_role_assignment_is_written_to_a_fresh_name_it_cannot_be_overwriting(
    monkeypatch,
):
    """The assignment name is the PUT's identity, so a name derived from the
    principal and the scope would silently replace whatever an operator had put
    there -- their condition, their description, their different role
    definition, all gone under a 200. A fresh uuid4 per call cannot collide
    with an assignment this tool did not create, so ARM's answer to "that grant
    already exists" is a 409 the caller can see rather than an overwrite it
    cannot."""
    put = _PutRecorder()
    monkeypatch.setattr(requests, "put", put)

    _Auth().create_role(ACR_ID, PRINCIPAL, ACR_PULL)
    _Auth().create_role(ACR_ID, PRINCIPAL, ACR_PULL)

    names = [call["url"].split("/roleAssignments/")[1].split("?")[0] for call in put.calls]
    assert names[0] != names[1], "same scope+principal+role twice reused one name"
    for name in names:
        assert re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", name), name
        # A digest of the inputs would look fresh without being fresh.
        assert PRINCIPAL not in name


def test_a_refused_role_assignment_write_is_raised_and_not_reported_as_a_grant(monkeypatch):
    """`requests` does not raise on 4xx by itself. Without the status check a
    403 from a subscription that forbids roleAssignments/write returns None,
    and `ensure_role` logs "granted AcrPull to the endpoint identity"."""
    monkeypatch.setattr(
        requests, "put", _PutRecorder(status=403, text='{"error":{"code":"AuthorizationFailed"}}')
    )

    with pytest.raises(PermissionError, match="403"):
        _Auth().create_role(ACR_ID, PRINCIPAL, ACR_PULL)

    got = ensure_role(ACR_ID, PRINCIPAL, ACR_PULL, auth=_EmptyButComplete())
    assert not got.granted and got.error and "403" in got.error
    assert "az role assignment create" in got.manual_fix


def test_a_conflict_over_an_existing_assignment_is_not_reported_as_a_grant(monkeypatch):
    """ARM answers an assignment that is already there with 409
    RoleAssignmentExists. It is not a grant this run made and must not be
    counted as one: `deploy_online` believes `granted` enough to sleep 60s
    waiting for a propagation that is not happening, on a billing GPU."""
    monkeypatch.setattr(requests, "put", _PutRecorder(status=409, text="RoleAssignmentExists"))

    got = ensure_role(ACR_ID, PRINCIPAL, ACR_PULL, auth=_EmptyButComplete())
    assert not got.granted
    assert got.error and "409" in got.error
    assert "RoleAssignmentExists" in got.error


def test_a_role_assignment_write_that_succeeds_is_reported_as_the_grant_it_is(monkeypatch):
    """The over-correction guard on the write path: 201 is a real grant, and
    `deploy_online`'s 60s propagation wait depends on knowing that."""
    monkeypatch.setattr(requests, "put", _PutRecorder(status=201))

    got = ensure_role(ACR_ID, PRINCIPAL, ACR_PULL, auth=_EmptyButComplete())
    assert got.granted and not got.already_had and got.error is None
