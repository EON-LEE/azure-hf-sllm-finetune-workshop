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


@dataclass
class IdentityGrants:
    """What the endpoint's managed identity can actually do, as measured."""

    endpoint_name: str
    principal_id: str | None = None
    acr_roles: list[str] = field(default_factory=list)
    storage_roles: list[str] = field(default_factory=list)
    #: True when the image registry is the workspace-linked ACR, in which case
    #: Azure grants pull rights itself and a missing explicit role is fine.
    acr_is_workspace_linked: bool = False

    @property
    def can_pull_image(self) -> bool:
        if self.acr_is_workspace_linked:
            return True
        return any(r in {ACR_PULL, "Owner", "Contributor"} for r in self.acr_roles)

    @property
    def can_read_artifacts(self) -> bool:
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

    missing = []
    if not grants.can_pull_image:
        missing.append(
            f"  - {ACR_PULL} on the container registry (cannot pull the image)"
        )
    if not grants.can_read_artifacts:
        missing.append(
            f"  - {STORAGE_READ} on the workspace storage (cannot read artifacts)"
        )

    if not missing:
        return None

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
            f"  az role assignment create --assignee-object-id {grants.principal_id} \\",
            "    --assignee-principal-type ServicePrincipal \\",
            f'    --role "{ACR_PULL}" --scope <acr-resource-id>',
            f"  az role assignment create --assignee-object-id {grants.principal_id} \\",
            "    --assignee-principal-type ServicePrincipal \\",
            f'    --role "{STORAGE_READ}" --scope <storage-resource-id>',
            "",
            "Role assignments can take a few minutes to propagate.",
            "",
            "Pass force=True to deploy anyway.",
        ]
    )


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

        def roles_on(scope: str) -> list[str]:
            if not scope or not principal_id:
                return []
            resp = requests.get(
                f"{arm}{scope}/providers/Microsoft.Authorization/roleAssignments"
                f"?api-version=2022-04-01&$filter=principalId eq '{principal_id}'",
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            names = []
            for a in resp.json().get("value", []):
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
        )
    except Exception as exc:  # noqa: BLE001 - never block on a failed read
        log.debug("identity preflight could not read grants: %s", exc)
        return None


def acr_id_for_image(image: str, subscription_id: str, resource_group: str) -> str:
    """Best-effort ARM id for the registry an image lives in.

    Only handles `<name>.azurecr.io/...` in the same resource group, which is
    what this repo builds. Anything else returns "" and the check quietly
    declines to run rather than checking the wrong registry.
    """
    host = str(image).split("/", 1)[0]
    if not host.endswith(".azurecr.io"):
        return ""
    registry = host.split(".", 1)[0]
    return (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.ContainerRegistry/registries/{registry}"
    )
