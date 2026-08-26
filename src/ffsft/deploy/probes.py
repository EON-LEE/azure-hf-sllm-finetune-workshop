"""Preflight probes: ask Azure, rather than assume.

Split out of `endpoint.py`. Every function here answers "would this actually
work?" before anything expensive is created, by making the cheapest real call
that can return a real answer -- a quota read, a `min_instances=0` cluster
created and immediately deleted, a storage account's own network and key
properties. Each one replaced an assumption that had already cost a rollout.

This is the *live* half of preflight; `preflight.py` holds the pure classifiers
that turn a read state into a blocker string. `check_pattern` is the entry
point that combines them.

Azure SDK imports stay function-local, per the docstring of
`tests/test_aml_job.py`: tests inject fakes by monkeypatching the attribute on
the module the caller reaches for, which for `check_pattern` is *this* module.

`endpoint.py` re-exports every public name here.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from collections.abc import Sequence

from .registry import get_serving_registry
from .spec import ServingSpec

log = logging.getLogger("ffsft.deploy.probes")


def read_dedicated_quota(subscription_id: str, location: str, family: str) -> int:
    """Read the *measured* dedicated-core limit for one VM family.

    Uses the Microsoft.Quota provider rather than the AML usages API on purpose:
    the AML usages endpoint reports `-1` for families that have no dedicated
    allocation, which reads like 'unlimited' and is the opposite of the truth.
    """
    import requests
    from azure.identity import DefaultAzureCredential

    cred = DefaultAzureCredential()
    token = cred.get_token("https://management.azure.com/.default").token
    scope = (
        f"subscriptions/{subscription_id}/providers/Microsoft.MachineLearningServices"
        f"/locations/{location}"
    )
    url = (
        f"https://management.azure.com/{scope}/providers/Microsoft.Quota"
        f"/quotas/{family}?api-version=2023-02-01"
    )
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    if resp.status_code == 404:
        log.warning("quota family '%s' is not defined in %s", family, location)
        return 0
    resp.raise_for_status()
    return int(resp.json()["properties"]["limit"]["value"])


@dataclasses.dataclass(frozen=True)
class SkuProbe:
    """What the control plane said when actually asked to create the cluster."""

    sku: str
    tier: str
    creatable: bool
    code: str
    detail: str

    @property
    def blocker(self) -> str | None:
        if self.creatable:
            return None
        return f"{self.code}. {self.detail}"


def classify_cluster_error(message: str) -> tuple[str, str]:
    """Turn an AmlCompute create failure into a code plus an actionable reason.

    The two responses differ in what they ask of you -- one is a support
    ticket, the other is 'pick a different SKU' -- so collapsing them into
    'deployment failed' throws away the only useful part.

    `InvalidPropertyValue` arrives with a list of "supported VM sizes" that is
    old enough to omit `Standard_NC24ads_A100_v4`, the SKU this project trains
    on every day. Repeating it would send the reader looking for a K80. The
    honest summary is that the control plane refuses this SKU here regardless
    of what the catalogue and the quota say.
    """
    if "ClusterMinNodesExceedCoreQuota" in message:
        family = re.search(r"Standard\s+(\w+)\s+family", message)
        quota = re.search(r"quota of (\d+)", message)
        detail = (
            f"dedicated quota for {family.group(1) if family else 'this family'} is "
            f"{quota.group(1) if quota else '0'}. Managed online endpoints are always "
            "dedicated, so no amount of retrying helps -- request a quota increase."
        )
        return "ClusterMinNodesExceedCoreQuota", detail
    if "InvalidPropertyValue" in message:
        sku = re.search(r"value (\S+) for property", message)
        detail = (
            f"{sku.group(1) if sku else 'this SKU'} cannot be created in this "
            "workspace at either tier, however many cores the catalogue and the "
            "usage APIs advertise. Choose a SKU that a real create call accepts."
        )
        return "InvalidPropertyValue", detail
    return "Unknown", message.strip()[:300]


def probe_sku(client, sku: str, tier: str, *, name: str = "ffsft-probe") -> SkuProbe:
    """Ask the control plane to create the cluster, then take it straight back.

    Scope, stated first because this function was read as answering a broader
    question than it does: this creates an **AmlCompute cluster**, so it answers
    "can a training job run on this SKU". It says nothing about a managed online
    endpoint, which is a different resource type on a different control plane.

    Reading it as a deployment probe inverts its answer. In koreacentral all six
    A10 v5 SKUs are MIR-only -- their `supportedComputeTypes` lists MIR and not
    AmlCompute -- so this call refuses precisely the SKUs a managed endpoint
    accepts. JOURNAL 43 concluded "every GPU SKU is NotAvailableForSubscription"
    from exactly that inversion; JOURNAL 51 retracts it, having created an
    endpoint in 69 seconds. For the deployment question, attempt a
    `ManagedOnlineDeployment` -- nothing else is evidence.

    Within its own scope it is the honest answer, and that part still holds:
    quota says yes for A10 v5 and the create call says no; the catalogue lists
    all sixteen GPU SKUs and the create call still says no.

    Free: a refusal returns in about two seconds having created nothing, and an
    acceptance is a `min_instances=0` cluster that allocates no node before it
    is deleted.
    """
    from azure.ai.ml.entities import AmlCompute

    try:
        client.compute.begin_create_or_update(
            AmlCompute(
                name=name,
                size=sku,
                min_instances=0,
                max_instances=1,
                tier=tier,
                idle_time_before_scale_down=120,
            )
        ).result()
    except Exception as exc:  # noqa: BLE001 - the message is the whole point
        code, detail = classify_cluster_error(str(exc))
        # A refused create still leaves a compute record in `Failed`. It holds no
        # nodes and bills nothing, but it accumulates, and this project's whole
        # teardown story is that nothing is left behind.
        _discard_probe(client, name)
        return SkuProbe(sku=sku, tier=tier, creatable=False, code=code, detail=detail)

    _discard_probe(client, name)
    return SkuProbe(sku=sku, tier=tier, creatable=True, code="", detail="")


def _discard_probe(client, name: str) -> None:
    try:
        client.compute.begin_delete(name)
    except Exception:  # noqa: BLE001 - a leaked min=0 cluster allocates nothing
        log.warning("probe cluster %s could not be deleted; it holds no nodes", name)


@dataclasses.dataclass(frozen=True)
class StoreProbe:
    """Whether a model asset can be created at all, and why not.

    Azure exposes no API that answers "can I register a model?", so this
    reconstructs the answer from the two properties that decide it on the
    workspace's default datastore account.
    """

    account: str
    public_access: str
    private_endpoints: int
    reachable: bool
    detail: str
    key_auth_refused: bool = False
    key_based_datastores: tuple[str, ...] = ()


def classify_store(
    account: str,
    public_access: str,
    private_endpoints: int,
    *,
    allow_shared_key: bool | None = None,
    key_based_datastores: Sequence[str] = (),
) -> StoreProbe:
    """Decide whether a storage account is reachable by anything.

    Two ways to be reachable, and they are the only two:

    * the public endpoint is on -- then `networkAcls` decides who gets in, and
      an `Allow` default action lets in the compute node and this laptop alike;
    * the public endpoint is off but a private endpoint exists -- the designed
      hardened posture, where traffic arrives over a private link instead.

    Off with no private endpoint is not a posture, it is an outage. Measured on
    this subscription (§24): three finished training runs each uploaded zero
    artifacts, `mount_outputs=True` fails during node setup, and registering a
    model from a job output returns `NoMatchingArtifactsFoundFromJob` -- all one
    cause. An ARM `PATCH` setting `publicNetworkAccess: Enabled` returns 200 and
    changes nothing, and a *newly created* account asked for `Enabled` comes
    back `Disabled`, so this is enforced above the subscription and cannot be
    fixed from here.

    Network reachability is necessary and *not* sufficient. A datastore also
    names how to authenticate, and that is a separate axis this check was blind
    to until polandcentral (S57.8): `mlw-ffsft-plc` sat behind two working
    private endpoints -- reachable by the rule above, and this function said so
    -- while every write still failed, because all four of its datastores were
    created with `credentialsType: AccountKey` against a storage account with
    `allowSharedKeyAccess: false`. The account refuses the key the datastore
    insists on presenting, so job log upload, artifact upload, output mounts and
    client-side `jobs.download()` all return `KeyBasedAuthenticationNotPermitted`
    -- the *same* zero-artifact symptom as an unreachable account, from a cause
    no amount of private endpoints or RBAC can fix. Two workspaces created the
    same way disagreed on this: koreacentral came up `None`, polandcentral came
    up `AccountKey`, so it cannot be assumed from the deployment path either.

    Anything this function cannot read reports reachable. A probe that cannot
    see is not the same as a resource that is broken, and the expensive mistake
    in this project has consistently been turning the former into the latter.
    That is why `allow_shared_key=None` (unread) never fails the check: only a
    measured `False` alongside a measured `AccountKey` datastore does.
    """
    key_based = tuple(key_based_datastores)

    if public_access != "Disabled":
        net_ok, net_detail = True, ""
    elif private_endpoints > 0:
        net_ok, net_detail = (
            True,
            (f"{account}: public access off, reached over {private_endpoints} private endpoint(s)"),
        )
    else:
        net_ok, net_detail = (
            False,
            (
                f"no reachable datastore: '{account}' has publicNetworkAccess=Disabled "
                f"and 0 private endpoints, so neither this client nor the Azure ML "
                f"compute node can open a session against it. Job outputs never upload "
                f"(artifacts=0 on every finished run), so there is nothing to register "
                f"as a model -- and every hosted pattern deploys a model asset. "
                f"Fix: attach a private endpoint to the account and put the compute in "
                f"that VNet. Turning public access back on is rejected silently by "
                f"tenant-level enforcement."
            ),
        )

    if allow_shared_key is False and key_based:
        # Reported even when the network posture passes, because it is
        # orthogonal to it: the key is refused on the public endpoint and over a
        # private link alike, so a green network answer says nothing about this.
        detail = (
            f"datastore credential mismatch: '{account}' has "
            f"allowSharedKeyAccess=false, but datastore(s) {', '.join(key_based)} "
            f"authenticate with credentialsType=AccountKey. Every write fails "
            f"with KeyBasedAuthenticationNotPermitted -- job logs, artifacts, "
            f"output mounts and jobs.download() alike -- so runs finish with "
            f"artifacts=0 and there is nothing to register as a model. Private "
            f"endpoints and role assignments do not fix this. Fix: PUT each "
            f"datastore with credentials.credentialsType='None' (identity-based) "
            f"and grant the workspace MSI, the cluster identity and yourself "
            f"Storage Blob Data Contributor on the account. Keep isDefault=true "
            f"on the workspace default datastore or the PUT is rejected."
        )
        if not net_ok:
            # Both broken at once (measured on `mlw-ffsft-jpe`). Reporting only
            # the first sends the caller through a fix-verify-fix round trip for
            # a blocker that was already visible here.
            detail += f" A second, independent blocker is also present -- {net_detail}"
        return StoreProbe(account, public_access, private_endpoints, False, detail, True, key_based)

    return StoreProbe(
        account, public_access, private_endpoints, net_ok, net_detail, False, key_based
    )


def _key_based_datastores(root: str, workspace: str, head: dict) -> list[str]:
    """Names of datastores that authenticate with an account key.

    Read separately from the account so an unreadable datastore list degrades to
    "no key-based datastores found" rather than to a false blocker -- the same
    reason `probe_model_store` reports reachable when it cannot see.
    """
    import requests

    try:
        page = requests.get(
            f"{root}/Microsoft.MachineLearningServices/workspaces/"
            f"{workspace}/datastores?api-version=2024-10-01",
            headers=head,
            timeout=60,
        ).json()
        return sorted(
            d["name"]
            for d in (page.get("value") or [])
            if ((d.get("properties") or {}).get("credentials") or {}).get("credentialsType")
            == "AccountKey"
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable list must not block
        log.warning("could not read datastore credentials: %s", exc)
        return []


def probe_model_store(target) -> StoreProbe:
    """Read the live posture of the workspace's default datastore.

    Free and read-only: three ARM GETs, no resource is created or touched. Two
    independent things can make the datastore unusable -- the account being
    unreachable, and the datastore presenting a credential the account refuses
    -- so both are read here and both are handed to `classify_store`.
    """
    import requests
    from azure.identity import AzureCliCredential

    cred = AzureCliCredential()
    tok = cred.get_token("https://management.azure.com/.default").token
    head = {"Authorization": f"Bearer {tok}"}
    root = (
        f"https://management.azure.com/subscriptions/{target.subscription_id}"
        f"/resourceGroups/{target.resource_group}/providers"
    )
    try:
        ws = requests.get(
            f"{root}/Microsoft.MachineLearningServices/workspaces/"
            f"{target.workspace_name}?api-version=2024-10-01",
            headers=head,
            timeout=60,
        ).json()
        account_id = ws["properties"]["storageAccount"]
        account = account_id.rsplit("/", 1)[-1]
        sa = requests.get(
            f"https://management.azure.com{account_id}?api-version=2023-05-01",
            headers=head,
            timeout=60,
        ).json()["properties"]
        return classify_store(
            account,
            sa.get("publicNetworkAccess", "Unknown"),
            len(sa.get("privateEndpointConnections") or []),
            allow_shared_key=sa.get("allowSharedKeyAccess"),
            key_based_datastores=_key_based_datastores(root, target.workspace_name, head),
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable probe must not block
        log.warning("could not read the datastore posture: %s", exc)
        return classify_store("unknown", "Unknown", 0)


def quota_family_for(sku: str | None) -> str | None:
    """Dedicated quota family `sku` bills against, or None if unknown.

    Unknown returns None rather than a guess so the caller falls back to the
    pattern's declared family -- the same reason `required_dedicated_cores`
    raises instead of assuming a core count.
    """
    if not sku:
        return None
    from ffsft.azure_ml import GPU_SKUS

    entry = GPU_SKUS.get(sku)
    return entry.get("family") if entry else None


def check_pattern(
    pattern_key: str,
    subscription_id: str,
    location: str,
    *,
    sku: str | None = None,
    instances: int = 1,
    store: StoreProbe | None = None,
    from_hub: bool = False,
) -> tuple[ServingSpec, str | None]:
    """Return the spec plus a human-readable blocker, or None if it can deploy.

    `from_hub` declares that the weights will come from the Hugging Face Hub at
    container start. For a pattern whose server resolves its own model that
    takes the datastore out of the picture entirely, so the storage check is
    skipped -- see `ServingSpec.can_serve_from_hub`.
    """
    spec = get_serving_registry().get(pattern_key)
    needs_store = spec.requires_model_asset and not (from_hub and spec.can_serve_from_hub)
    if store is not None and needs_store and not store.reachable:
        # Checked before quota on purpose: no model asset means no deployment of
        # any kind, so leading with a quota number would imply that raising the
        # quota would help.
        return spec, store.detail
    if spec.allows_low_priority or not spec.quota_family:
        return spec, None
    # A `--sku` override can cross quota families. The pattern names the family
    # of its *default* SKU, but Azure bills the family the *chosen* SKU belongs
    # to, so reading `spec.quota_family` here measures a pool the deployment
    # never touches: an A100 SKU was refused in a region with 48 A100 cores
    # granted because the A10 pool it would never use read 0.
    family = quota_family_for(sku or spec.default_sku) or spec.quota_family
    available = read_dedicated_quota(subscription_id, location, family)
    return spec, spec.blocked_reason(available, instances=instances, sku=sku, quota_family=family)
