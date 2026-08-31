"""The endpoint's own identity needs permissions, and nobody was granting them.

This file exists because of a two-endpoint, ~4-hour, ~$8 misdiagnosis.

Two managed online deployments sat in `Creating` for over an hour and died with
a bare `InternalServerError`. No container logs. App Insights `traces` empty.
I concluded the workspace storage account was unreachable and wrote a preflight
for that. **That conclusion was wrong**, and the evidence that disproved it was
available the whole time:

    networkAcls.bypass            = "AzureServices"   <- AML is a trusted service
    workspace MI on storage       = Storage Blob Data Contributor
    workspace MI on ACR           = AcrPull
    datastore credentialsType     = None  (identity-based, so allowSharedKeyAccess
                                           being false is irrelevant)

Microsoft states the bypass survives `publicNetworkAccess: Disabled`:

    "access to a storage account from trusted services takes the highest
     precedence over other network access restrictions ... exceptions that you
     previously configured ... will remain in effect."
    -- learn.microsoft.com/azure/storage/common/storage-network-security-limitations

So AML could always reach the storage. What could *not* reach anything was the
**online endpoint's own system-assigned managed identity**, which is a different
principal from the workspace's. Measured on a freshly created endpoint:

    endpoint MI on ACR      -> NONE
    endpoint MI on storage  -> NONE

Azure grants those automatically only for the **workspace-linked** ACR. This
workspace has none -- `properties.containerRegistry` is empty -- so
`acrffsftkc` is a customer registry and the grants must be made by hand.

The endpoint therefore could not pull a 9.15 GB image it had no right to read.
The container never started, which is exactly why there were no logs and no
traces, and why the platform could only report a generic error when its internal
timeout expired. Every symptom follows from this one fact.

The lesson worth keeping: *the workspace identity having a permission tells you
nothing about whether the endpoint identity has it.*
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger("ffsft.deploy.identity")

#: Pulling the inference image. Without this the container never starts, and the
#: failure is invisible: no logs, no traces, just a long wait and a generic error.
ACR_PULL = "AcrPull"

#: Reading model/code artifacts staged in the workspace's default storage.
#: Reader is enough for serving; the endpoint never writes artifacts.
STORAGE_READ = "Storage Blob Data Reader"

#: Writing them. A *training* cluster needs this and Reader is not enough: it
#: uploads logs, artifacts and the output mount. See `ensure_compute`.
STORAGE_WRITE = "Storage Blob Data Contributor"


@dataclass
class IdentityGrants:
    """What the endpoint's managed identity can actually do, as measured.

    Each role list is tri-state, and the third state is the point:

      * ``[...]``  ARM listed the assignments and these are they;
      * ``[]``     ARM listed them and there are none -- a measurement;
      * ``None``   the listing was never completed, so nothing is known.

    The default stays ``[]`` so a record built by hand still means "looked, saw
    none"; only :func:`read_identity_grants` sets ``None``, and only when a read
    it started did not finish. Before S79 the third state did not exist, and a
    listing ARM had truncated arrived here as ``[]`` -- which is why the two
    scopes now travel with the listing, so a caller can name which read is
    missing instead of doubting all of them.
    """

    endpoint_name: str
    principal_id: str | None = None
    acr_roles: list[str] | None = field(default_factory=list)
    storage_roles: list[str] | None = field(default_factory=list)
    #: True when the image registry is the workspace-linked ACR, in which case
    #: Azure grants pull rights itself and a missing explicit role is fine.
    acr_is_workspace_linked: bool = False
    #: The ARM ids the two lists above are about. Carried so an unread listing
    #: can be reported against the resource it failed on -- and so the `az`
    #: command that closes the gap can be printed with the scope filled in.
    acr_scope: str = ""
    storage_scope: str = ""

    @property
    def can_pull_image(self) -> bool | None:
        """True / False / None, where None means the listing was not read."""
        if self.acr_is_workspace_linked:
            return True
        if self.acr_roles is None:
            return None
        return any(r in {ACR_PULL, "Owner", "Contributor"} for r in self.acr_roles)

    @property
    def can_read_artifacts(self) -> bool | None:
        if self.storage_roles is None:
            return None
        return any(
            r
            in {
                STORAGE_READ,
                "Storage Blob Data Contributor",
                "Storage Blob Data Owner",
                "Owner",
            }
            for r in self.storage_roles
        )


def identity_blocker(grants: IdentityGrants) -> str | None:
    """Explain why this endpoint cannot deploy, or None if it can.

    Deliberately says what to *run*, not merely what is wrong. The failure this
    guards against costs an hour of GPU time before it reports anything, so the
    message has to be enough to act on without a second round of investigation.
    """
    if grants.principal_id is None:
        return None  # unknown identity: never block on missing information

    # `is False`, not `not ...`: None means the roleAssignments listing was
    # never completed, and a role nobody measured may not become a finding.
    # Executed both ways against the same fake registry, the only variable
    # being whether ARM paged the two assignments (S79):
    #     one page  -> acr_roles ['Reader', 'AcrPull'] -> deploys
    #     paginated -> acr_roles ['Reader']            -> BLOCKS
    # The grant was there. `identity_unread_note` is what says so out loud.
    #
    # Round 9: ONE list drives both the bullets and the commands. They used to
    # be built separately -- per-scope bullets under a fixed `Fix it with:`
    # paragraph naming both roles -- so the footer prescribed a grant the
    # bullets had just refused to claim was missing. Executed pre-fix over a
    # hand-built record with the registry listing unread:
    #
    #   --- storage missing, registry listing NEVER READ ---
    #      FINDINGS : ['- Storage Blob Data Reader on the workspace storage ...']
    #      PRESCRIBES: ['--role "AcrPull" --scope <acr-resource-id>',
    #                   '--role "Storage Blob Data Reader" --scope <storage...>']
    #
    # `acr_roles is None` is "the listing did not finish". An `az role
    # assignment create` printed over it is the sign-flipped invariant with an
    # action stapled on: the operator runs it, ARM grants a role that may have
    # been there all along, and the tool has produced a finding it never
    # measured. The milder half is the same shape -- prescribing a blob
    # data-plane grant this function just measured as already present. S81.5.
    gaps: list[tuple[str, str, str, str]] = []
    if grants.can_pull_image is False:
        gaps.append(
            (
                ACR_PULL,
                "on the container registry (cannot pull the image)",
                grants.acr_scope,
                "<acr-resource-id>",
            )
        )
    if grants.can_read_artifacts is False:
        gaps.append(
            (
                STORAGE_READ,
                "on the workspace storage (cannot read artifacts)",
                grants.storage_scope,
                "<storage-resource-id>",
            )
        )

    if not gaps:
        return None

    missing = [f"  - {role} {why}" for role, why, _scope, _ph in gaps]
    commands: list[str] = []
    for role, _why, scope, placeholder in gaps:
        commands += [
            f"  az role assignment create --assignee-object-id {grants.principal_id} \\",
            "    --assignee-principal-type ServicePrincipal \\",
            f'    --role "{role}" --scope {scope or placeholder}',
        ]

    return "\n".join(
        [
            f"endpoint '{grants.endpoint_name}' has a managed identity "
            f"({grants.principal_id}) that is missing:",
            *missing,
            "",
            "Azure grants these automatically only for the workspace-linked registry.",
            "For any other registry you must assign them yourself, and until you do the",
            "deployment fails the slow way: the image pull is refused, so the container",
            "never starts, so there are no container logs and App Insights stays empty,",
            "and the rollout reports a bare InternalServerError once it times out --",
            "after an hour or more of GPU billing.",
            "",
            "Fix it with:",
            *commands,
            "",
            "Role assignments can take a few minutes to propagate.",
            "",
            "Pass force=True to deploy anyway.",
        ]
    )


def identity_unread_note(grants: IdentityGrants) -> str | None:
    """Name the grants this tool could not read, or None if it read them all.

    The other half of :func:`identity_blocker`, and it exists because the two
    answers are not opposites. A roleAssignments listing that stopped early
    cannot show a grant ABSENT, so the blocker must not refuse over it --
    CLAUDE.md prices that direction exactly: "refusing on a value nobody
    measured ... blocks a deployment that would have worked". But *not*
    refusing is not the same as saying the grant is there, and passing in
    silence is the first half of the same invariant.

    So the gap is stated, against the scope it is about, with the command that
    closes it. Same vocabulary as `probe_report` for a SKU it never probed and
    `format_inventory` for a listing that raised: the word is UNKNOWN, and no
    verdict is printed beside it.
    """
    if grants.principal_id is None:
        return None  # no identity, so no listing was owed in the first place

    gaps: list[tuple[str, str, str]] = []
    # A workspace-linked registry needs no explicit grant, so an unread listing
    # there leaves nothing unknown that matters.
    if grants.acr_roles is None and not grants.acr_is_workspace_linked:
        gaps.append(("the container registry", grants.acr_scope, ACR_PULL))
    if grants.storage_roles is None:
        gaps.append(("the workspace storage", grants.storage_scope, STORAGE_READ))
    if not gaps:
        return None

    lines = [
        f"endpoint '{grants.endpoint_name}': the role assignments of identity "
        f"{grants.principal_id} could NOT be read to the end, so:",
    ]
    for what, scope, role in gaps:
        where = scope or "scope could not be resolved, nothing was queried"
        lines.append(f"  - {role} on {what} is UNKNOWN ({where})")
    lines += [
        "",
        "UNKNOWN is not 'missing'. This tool cannot show the grant is absent, so it is",
        "not refusing the deploy over it -- and it cannot show the grant is present",
        "either, so it is not claiming that. If the rollout sits in Creating with no",
        "container logs, this is the first thing to rule out:",
    ]
    for _what, scope, _role in gaps:
        if scope:
            lines.append(
                f"  az role assignment list --assignee {grants.principal_id} "
                f"--scope {scope} --all -o table"
            )
    return "\n".join(lines)


#: Role definition GUIDs, because listing assignments returns GUIDs and
#: resolving each one to a name is an extra round trip per assignment.
_ROLE_NAMES = {
    "7f951dda-4ed3-4680-a7ca-43fe172d538d": ACR_PULL,
    "2a2b9908-6ea1-4ae2-8e65-a410df84e7d1": STORAGE_READ,
    "ba92f5b4-2d11-453d-a403-e96b0029c9fe": "Storage Blob Data Contributor",
    "b7e6dc6d-f1e8-4753-8033-0f276bb0955b": "Storage Blob Data Owner",
    "8e3af657-a8ff-443c-a75c-2fe8c4bcb635": "Owner",
    "b24988ac-6180-42a0-ab88-20f7382dd24c": "Contributor",
    "acdd72a7-3385-48ef-bd42-f606fba81ae7": "Reader",
}


def read_identity_grants(
    target, endpoint_name: str, acr_id: str, *, credential=None
) -> IdentityGrants | None:
    """Read what `endpoint_name`'s managed identity may actually do.

    Returns None when the facts cannot be established -- an endpoint that does
    not exist yet, a missing optional dependency, any ARM failure. A preflight
    that guesses is worse than no preflight, and this one runs in front of an
    operation that is expensive to get wrong in either direction.
    """
    try:
        import requests
        from azure.identity import DefaultAzureCredential

        from .preflight import read_all_arm_pages
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        log.debug("identity preflight skipped, azure libraries missing: %s", exc)
        return None

    try:
        cred = credential or DefaultAzureCredential()
        token = cred.get_token("https://management.azure.com/.default").token
        headers = {"Authorization": f"Bearer {token}"}
        arm = "https://management.azure.com"
        ws = (
            f"{arm}/subscriptions/{target.subscription_id}"
            f"/resourceGroups/{target.resource_group}/providers"
            f"/Microsoft.MachineLearningServices/workspaces/{target.workspace_name}"
        )

        ep = requests.get(
            f"{ws}/onlineEndpoints/{endpoint_name}?api-version=2024-10-01",
            headers=headers,
            timeout=30,
        )
        if ep.status_code == 404:
            # First deployment to a new name: the identity does not exist yet, so
            # there is nothing to check and nothing to warn about.
            return None
        ep.raise_for_status()
        principal_id = (ep.json().get("identity") or {}).get("principalId")

        ws_body = requests.get(f"{ws}?api-version=2024-10-01", headers=headers, timeout=30)
        ws_body.raise_for_status()
        linked_acr = (ws_body.json().get("properties") or {}).get("containerRegistry") or ""

        sa_id = (ws_body.json().get("properties") or {}).get("storageAccount") or ""

        def roles_on(scope: str) -> list[str] | None:
            """Every role `principal_id` holds on `scope`, or None if unread.

            This did ONE `requests.get` and iterated `.json().get("value", [])`.
            Microsoft types that 200 as `RoleAssignmentListResult`, which
            carries `nextLink` beside "The RoleAssignment items **on this
            page**" -- so page 1 was being read as the whole list, and the same
            `.get(..., [])` also turned a body that is not a list result at all
            into "this identity holds no roles".

            Both landed on `[]`, and `[]` here is a *finding*: it makes
            `can_pull_image` False and `identity_blocker` refuse a deployment
            whose AcrPull grant is sitting on page 2. Executed, same fake
            registry and same two assignments, the only variable being whether
            ARM paged them -- which is ARM's choice, not ours (S79):

                one page   -> ['Reader', 'AcrPull'] -> deploys
                paginated  -> ['Reader']            -> BLOCKS, "missing AcrPull"

            `acr_id_for_image`, ~40 lines down this same file, already went
            through `read_all_arm_pages`. This is that, plus the third state
            the dataclass now has for a read that did not finish.
            """
            if not principal_id:
                # Nothing to hold roles. `identity_blocker` exits on a null
                # principal before it reads either list.
                return None
            if not scope:
                # No ARM id, so no listing was made. `[]` here used to mean
                # "measured, holds nothing" and refused the deploy over a
                # resource id this tool had simply failed to build.
                return None
            try:
                assignments = read_all_arm_pages(
                    requests,
                    f"{arm}{scope}/providers/Microsoft.Authorization/roleAssignments"
                    f"?api-version=2022-04-01&$filter=principalId eq '{principal_id}'",
                    headers=headers,
                    timeout=30,
                )
            except Exception as exc:  # noqa: BLE001 - scoped unread, see below
                # Scoped to ONE listing on purpose. The enclosing handler turns
                # any failure into `return None` for the whole record, which
                # loses the other scope's perfectly good reading -- a truncated
                # registry listing would take a real, measured, missing storage
                # role down with it. Catching here keeps the status next to the
                # rows it explains, which is what `SectionScan` does for the
                # same reason.
                #
                # A warning, not a debug line: this is the branch where the
                # preflight stopped being able to answer, and it is rendered to
                # the operator by `identity_unread_note`.
                log.warning(
                    "role assignments of %s on %s could not be listed to the end "
                    "(%s); this preflight can no longer tell a missing grant from "
                    "an unread one, and will not refuse the deploy over it",
                    principal_id, scope, exc,
                )
                return None
            names = []
            for a in assignments:
                rid = (a.get("properties") or {}).get("roleDefinitionId", "")
                names.append(_ROLE_NAMES.get(rid.rsplit("/", 1)[-1], rid.rsplit("/", 1)[-1]))
            return names

        return IdentityGrants(
            endpoint_name=endpoint_name,
            principal_id=principal_id,
            acr_roles=roles_on(acr_id),
            storage_roles=roles_on(sa_id),
            acr_is_workspace_linked=(
                bool(linked_acr) and linked_acr.lower() == acr_id.lower()
            ),
            acr_scope=acr_id,
            storage_scope=sa_id,
        )
    except Exception as exc:  # noqa: BLE001 - never block on a failed read
        log.debug("identity preflight could not read grants: %s", exc)
        return None


def acr_id_for_image(
    image: str, subscription_id: str, resource_group: str, *, credential=None
) -> str:
    """ARM id for the registry an image lives in, or "" if it is not an ACR.

    The registry is looked up by name across the subscription rather than
    assumed to sit in `resource_group`. Assuming cost a deployment: a
    polandcentral endpoint was pointed at `acrffsftkc`, which lives in
    `rg-ffsft-kc`, and the constructed id named `rg-ffsft-plc` instead. ARM
    answered 404, `ensure_acr_pull` reported "could not read role assignments"
    and granted nothing -- reproducing, in a new region, the exact
    no-AcrPull/no-logs failure this module was written for.

    `resource_group` stays the fallback for when the lookup cannot run (no
    azure libraries, no credential, a read the caller is not entitled to), so
    behaviour is unchanged in the same-resource-group case this repo builds.

    What round 7 changed is which of those the fallback SAYS it is. The lookup
    read one page of a paginated ARM list, so a registry sitting on page 2 was
    indistinguishable from a registry that is not in the subscription at all,
    and both fell through to `assumed` on a `log.debug` nobody reads. The three
    outcomes are now three different log lines, and the one that means "this
    guess may name the wrong resource group" is a warning. S78.4.

    Round 9 found that it made only ONE of the two blind outcomes a warning:
    the handler named `TruncatedListing`, so the paginator's own page cap was
    loud while a listing that 403'd on page 2 fell two levels out to the
    `log.debug` this paragraph is about. Both leave `assumed` a guess, so both
    are now the same warning, told apart by the exception type in the message.
    The DEBUG line survives for the case where the lookup never ran at all.
    S81.4.
    """
    host = str(image).split("/", 1)[0]
    if not host.endswith(".azurecr.io"):
        return ""
    registry = host.split(".", 1)[0]
    assumed = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.ContainerRegistry/registries/{registry}"
    )

    try:
        import requests
        from azure.identity import DefaultAzureCredential

        from .preflight import read_all_arm_pages

        cred = credential or DefaultAzureCredential()
        token = cred.get_token("https://management.azure.com/.default").token
        try:
            registries = read_all_arm_pages(
                requests,
                f"https://management.azure.com/subscriptions/{subscription_id}"
                f"/providers/Microsoft.ContainerRegistry/registries"
                f"?api-version=2023-01-01-preview",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001 - re-reported at WARNING, never dropped
            # Louder than the outer handler on purpose: this is the branch where
            # `assumed` might name the wrong resource group and nobody looked.
            #
            # Round 9 widened it from `except TruncatedListing`. Round 7 made
            # the paginator's own cap a warning and left every OTHER way for
            # the same listing to stop short falling two levels out to
            # `log.debug`. Executed against the pre-fix module with a fake
            # `requests` (page 1 carries a `nextLink`, page 2 raises) and a
            # fake credential returning the literal string "fake-token":
            #
            #   B: page 2 of the listing raises (mid-listing HTTP failure)
            #      DEBUG: could not resolve ACR acrffsft by name, assuming
            #             rg-fake: (AuthorizationFailed) fake 403 on page 2
            #   A: the page cap is hit (TruncatedListing from the paginator)
            #      WARNING: the registry listing for this subscription stopped
            #             short (...)
            #
            # Same guess, same blindness, two volumes -- and `log.debug` is off
            # in every shipped entry point, so B printed nothing at all. §80 is
            # this exact failure one file over. The wording was the other half:
            # "could not resolve ACR X by name" is what you say after looking.
            #
            # The exception TYPE goes in the message because that is how the
            # rest of this codebase tells a truncation from a 403
            # (`SectionScan.detail`); one level, still two stories. S81.4.
            log.warning(
                "the registry listing for this subscription did not complete "
                "(%s: %s), so whether %s lives outside %s was never established; "
                "assuming %s",
                type(exc).__name__, exc, registry, resource_group, assumed,
            )
            return assumed

        for item in registries:
            if str(item.get("name", "")).lower() == registry.lower() and item.get("id"):
                return str(item["id"])

        # Scanned every page and it is genuinely not there. Worth a line too:
        # it used to be silent, and silence here reads the same as a hit.
        log.info(
            "ACR %s is not in subscription %s; assuming %s",
            registry, subscription_id, assumed,
        )
    except Exception as exc:  # noqa: BLE001 - a failed lookup must not block deploy
        # What reaches here after round 9 is only the seam BEFORE the listing:
        # the azure extra absent, or a credential that refuses. The lookup did
        # not run, which is the case this fallback was designed around, and on
        # both call paths (`deploy/endpoint.py::deploy_online`, `azure_ml.py`)
        # the very next Azure call fails loudly with the same credential -- so
        # a warning here would put a second line under every real auth failure
        # and teach the operator to skip the one above. The wording no longer
        # says "could not resolve ACR X by name": nothing was resolved because
        # nothing was asked.
        log.debug(
            "the ACR lookup for %s did not run, so %s is assumed to hold it: %s",
            registry, resource_group, exc,
        )

    return assumed


#: ARM GUID for AcrPull, needed to *create* an assignment (the read path maps
#: GUIDs to names; the write path needs the GUID back).
ACR_PULL_ROLE_GUID = "7f951dda-4ed3-4680-a7ca-43fe172d538d"

#: Roles that already imply pull rights, so re-granting would be noise.
_PULL_EQUIVALENT = {ACR_PULL, "Owner", "Contributor"}

#: Same idea per grantable role. Note what is *absent* from the storage entry:
#: `Contributor` implies AcrPull but grants no blob **data-plane** access at
#: all. It lets you read the account keys, which is exactly the door
#: `allowSharedKeyAccess=false` closes -- so on a hardened account a Contributor
#: is not a substitute for the data role, and treating it as one would make this
#: check pass on the configuration it exists to catch.
_EQUIVALENT = {
    ACR_PULL: _PULL_EQUIVALENT,
    STORAGE_WRITE: {STORAGE_WRITE, "Storage Blob Data Owner", "Owner"},
}

#: Name -> GUID, for the write path. `_ROLE_NAMES` is the read direction.
_ROLE_GUIDS = {name: guid for guid, name in _ROLE_NAMES.items()}


@dataclass
class GrantResult:
    """What `ensure_acr_pull` actually did, so the caller can report honestly."""

    granted: bool = False
    already_had: bool = False
    error: str | None = None
    manual_fix: str | None = None


class ArmRoleAuth:
    """The ARM role-assignment API, narrowed to the two calls needed here."""

    def __init__(self, credential=None):
        self._credential = credential

    def _headers(self):
        from azure.identity import DefaultAzureCredential

        cred = self._credential or DefaultAzureCredential()
        token = cred.get_token("https://management.azure.com/.default").token
        return {"Authorization": f"Bearer {token}"}

    def list_roles(self, scope: str, principal_id: str) -> list[str]:
        """Every role held on `scope`, or raise if the listing did not finish.

        Raising is the contract `read_all_arm_pages` documents, and it lands
        exactly where it should: `ensure_role`'s existing could-not-look
        handler, which answers with the `az` command instead of a write.

        Reading one page here was worse than in the preflight, because this
        list decides whether to *write* RBAC. Executed against the same fake
        (S79), before the fix, with AcrPull already held and sitting on page 2:

            paginated  -> granted=True already_had=False PUTs=1
            page 2 403 -> granted=True already_had=False PUTs=1

        Both re-granted a role the principal already had, on the strength of
        rows nobody read; the second did it over a page ARM had refused. ARM
        answers the first with 409 RoleAssignmentExists, which `ensure_role`
        then reports to the operator as a permissions problem that does not
        exist -- and `deploy_online` believes `granted` enough to sleep 60s of
        GPU billing waiting for a propagation that is not happening.
        """
        import requests

        from .preflight import read_all_arm_pages

        assignments = read_all_arm_pages(
            requests,
            f"https://management.azure.com{scope}"
            f"/providers/Microsoft.Authorization/roleAssignments"
            f"?api-version=2022-04-01&$filter=principalId eq '{principal_id}'",
            headers=self._headers(),
            timeout=30,
        )
        out = []
        for a in assignments:
            rid = (a.get("properties") or {}).get("roleDefinitionId", "")
            out.append(_ROLE_NAMES.get(rid.rsplit("/", 1)[-1], rid.rsplit("/", 1)[-1]))
        return out

    def create_role(self, scope: str, principal_id: str, role: str) -> None:
        import uuid

        import requests

        subscription = scope.split("/")[2]
        # This used to ignore `role` and always write the AcrPull GUID. A caller
        # asking for a storage role would have been handed a registry role and
        # told it succeeded.
        guid = _ROLE_GUIDS.get(role)
        if guid is None:
            raise ValueError(f"no role definition GUID known for {role!r}")
        body = {
            "properties": {
                "roleDefinitionId": (
                    f"/subscriptions/{subscription}/providers"
                    f"/Microsoft.Authorization/roleDefinitions/{guid}"
                ),
                "principalId": principal_id,
                # Required for a freshly created managed identity: without it ARM
                # tries to look the principal up in Entra and fails while the new
                # identity is still replicating.
                "principalType": "ServicePrincipal",
            }
        }
        resp = requests.put(
            f"https://management.azure.com{scope}"
            f"/providers/Microsoft.Authorization/roleAssignments/{uuid.uuid4()}"
            f"?api-version=2022-04-01",
            headers={**self._headers(), "Content-Type": "application/json"},
            json=body,
            timeout=30,
        )
        if resp.status_code >= 400:
            raise PermissionError(f"HTTP {resp.status_code}: {resp.text[:300]}")


def _manual_fix(scope: str, principal_id: str, role: str = ACR_PULL) -> str:
    return (
        "  az role assignment create \\\n"
        f"    --assignee-object-id {principal_id} \\\n"
        "    --assignee-principal-type ServicePrincipal \\\n"
        f'    --role "{role}" \\\n'
        f"    --scope {scope}"
    )


def ensure_role(scope, principal_id, role, *, auth=None) -> GrantResult:
    """Give `principal_id` `role` on `scope`, if it does not already have it.

    Written after the preflight in this module failed to prevent the very
    failure it documents. Detecting a missing grant and then asking a human to
    run one `az` command is not much of an improvement when the tool is already
    authenticated and the command is deterministic -- so this makes the grant.

    Never raises. A credential without `Microsoft.Authorization/roleAssignments/write`
    is a normal situation on a locked-down subscription, and the useful response
    is the exact command to hand to somebody who does have it.
    """
    if not scope or not principal_id:
        return GrantResult(error="no scope or no principal to grant to")

    auth = auth or ArmRoleAuth()
    try:
        existing = auth.list_roles(scope, principal_id)
    except Exception as exc:  # noqa: BLE001 - a failed read must not stop a deploy
        return GrantResult(
            error=f"could not read role assignments: {exc}",
            manual_fix=_manual_fix(scope, principal_id, role),
        )

    if any(r in _EQUIVALENT.get(role, {role}) for r in existing):
        return GrantResult(already_had=True)

    try:
        auth.create_role(scope, principal_id, role)
    except Exception as exc:  # noqa: BLE001 - report, do not crash the deploy
        return GrantResult(
            error=str(exc), manual_fix=_manual_fix(scope, principal_id, role)
        )

    log.info("granted %s to %s on %s", role, principal_id, scope.rsplit("/", 1)[-1])
    return GrantResult(granted=True)


def ensure_acr_pull(scope, principal_id, *, auth=None) -> GrantResult:
    """`ensure_role` fixed to the registry role. Kept because it names the case."""
    return ensure_role(scope, principal_id, ACR_PULL, auth=auth)


def compute_role_grants(*, storage_id: str | None, acr_id: str | None) -> list[tuple[str, str]]:
    """The `(scope, role)` pairs an AmlCompute identity needs in this layout.

    Pure, so the decision can be tested without ARM. Two entries, and both were
    learned the same way -- by a job dying because one was missing:

    * storage: the node uploads logs, artifacts and the output mount. It needs
      the **write** role. `STORAGE_READ` is what a serving endpoint needs and it
      is not enough here; a cluster with only Reader finishes with `artifacts: 0`.
    * registry: the training image lives in a registry this workspace is not
      linked to, so nothing wires up AcrPull on its own.

    A scope that could not be resolved is dropped rather than guessed. A wrong
    scope does not fail loudly -- ARM answers 404, the grant is reported as "could
    not read role assignments", and the caller is told about a permissions problem
    that does not exist.
    """
    grants = []
    if storage_id:
        grants.append((storage_id, STORAGE_WRITE))
    if acr_id:
        grants.append((acr_id, ACR_PULL))
    return grants
