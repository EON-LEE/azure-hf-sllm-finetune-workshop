"""Provision the Azure ML side of the asset.

Creates (idempotently) the workspace and the GPU compute cluster that the
training backend submits jobs to. Sizing comes from the model registry, so the
cluster matches whatever model is selected rather than being hardcoded.

    ffsft azure provision --model qwen3.8-27b
"""

from __future__ import annotations

import dataclasses
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # keep azure-ai-ml an optional import
    from azure.ai.ml import MLClient

from .models import ModelSpec, TuningMethod

log = logging.getLogger("ffsft.azure_ml")

#: Every GPU SKU Azure ML offers, as reported by
#: `Microsoft.MachineLearningServices/locations/koreacentral/vmSizes`
#: (verified live 2026-08-20). `cores` is the unit quota is measured in and
#: `vram_gb` is the whole node, not one card.
#:
#: `low_priority` is the field that decides whether you can actually get the
#: node, and it is the least obvious thing in this file. Azure ML keeps its OWN
#: quota, separate from `Microsoft.Compute`:
#:
#:   * Dedicated quota is per family (`standardNCADSA100v4Family`, ...). On a
#:     stock subscription those entries simply do not exist, and AmlCompute then
#:     reports `InvalidPropertyValue ... "<sku>" is not a supported VM size`.
#:     That message is a lie -- the SKU is supported, the dedicated quota is
#:     absent. Chasing it as a SKU problem costs hours.
#:   * LowPriority quota is a single pooled `TotalLowPriorityCores` per region
#:     (300 here) that is NOT split by family, so any low-priority-capable GPU
#:     SKU can be provisioned immediately with no quota request at all.
#:
#: LowPriority is also the only way past the tenant policy `MCAPSGovDenyPolicies
#: / VirtualMachine_SKU_Deny`, whose rule is `priority notEquals "Spot"` and so
#: blocks every dedicated N-series VM. Both constraints point the same way:
#: default to LowPriority and checkpoint often.
GPU_SKUS: dict[str, dict] = {
    # A10 -- NV-series, fractional vGPU below 36 cores.
    "Standard_NV6ads_A10_v5": {
        "vram_gb": 4, "gpus": 1, "cores": 6,
        "family": "standardNVADSA10v5Family", "low_priority": True,
    },
    "Standard_NV12ads_A10_v5": {
        "vram_gb": 8, "gpus": 1, "cores": 12,
        "family": "standardNVADSA10v5Family", "low_priority": True,
    },
    "Standard_NV18ads_A10_v5": {
        "vram_gb": 12, "gpus": 1, "cores": 18,
        "family": "standardNVADSA10v5Family", "low_priority": True,
    },
    "Standard_NV36ads_A10_v5": {
        "vram_gb": 24, "gpus": 1, "cores": 36,
        "family": "standardNVADSA10v5Family", "low_priority": True,
    },
    "Standard_NV36adms_A10_v5": {
        "vram_gb": 24, "gpus": 1, "cores": 36,
        "family": "standardNVADSA10v5Family", "low_priority": True,
    },
    "Standard_NV72ads_A10_v5": {
        "vram_gb": 48, "gpus": 2, "cores": 72,
        "family": "standardNVADSA10v5Family", "low_priority": True,
    },
    # T4 -- Turing, no bfloat16. Avoid for modern recipes.
    "Standard_NC4as_T4_v3": {
        "vram_gb": 16, "gpus": 1, "cores": 4,
        "family": "standardNCASv3_T4Family", "low_priority": True,
    },
    "Standard_NC8as_T4_v3": {
        "vram_gb": 16, "gpus": 1, "cores": 8,
        "family": "standardNCASv3_T4Family", "low_priority": True,
    },
    "Standard_NC16as_T4_v3": {
        "vram_gb": 16, "gpus": 1, "cores": 16,
        "family": "standardNCASv3_T4Family", "low_priority": True,
    },
    "Standard_NC64as_T4_v3": {
        "vram_gb": 64, "gpus": 4, "cores": 64,
        "family": "standardNCASv3_T4Family", "low_priority": True,
    },
    # A100 80GB.
    "Standard_NC24ads_A100_v4": {
        "vram_gb": 80, "gpus": 1, "cores": 24,
        "family": "standardNCADSA100v4Family", "low_priority": True,
    },
    "Standard_NC48ads_A100_v4": {
        "vram_gb": 160, "gpus": 2, "cores": 48,
        "family": "standardNCADSA100v4Family", "low_priority": False,
    },
    "Standard_NC96ads_A100_v4": {
        "vram_gb": 320, "gpus": 4, "cores": 96,
        "family": "standardNCADSA100v4Family", "low_priority": False,
    },
    # H100.
    "Standard_NC40ads_H100_v5": {
        "vram_gb": 94, "gpus": 1, "cores": 40,
        "family": "standardNCadsH100v5Family", "low_priority": True,
    },
    "Standard_NC80adis_H100_v5": {
        "vram_gb": 188, "gpus": 2, "cores": 80,
        "family": "standardNCadsH100v5Family", "low_priority": True,
    },
    "Standard_ND96isr_H100_v5": {
        "vram_gb": 640, "gpus": 8, "cores": 96,
        "family": "standardNDv5H100Family", "low_priority": True,
    },
}

#: Regional cap on pooled low-priority cores, verified in koreacentral,
#: eastus2, swedencentral, japaneast, southeastasia and westus3.
TOTAL_LOW_PRIORITY_CORES = 300


def image_tag(image: str) -> str:
    """The tag of a container reference, which is also its environment version.

    Azure ML environments are immutable per version, so the version and the
    image tag have to agree. They used to be two hand-maintained constants held
    together by a comment; deriving one from the other is what stops them
    drifting, and `plum_station_dxwtzlz94q` is what drifting costs.

    All three images go through here -- training, serving and bench -- so the
    rule lives beside the Azure ML client rather than in any one of them.

    Splits on the *last* colon because a registry host may carry a port
    (`localhost:5000/img:3`), and rejects anything untagged or digest-pinned:
    `:latest` is mutable, which is the same bug wearing a different hat, and a
    `sha256:` digest is not a legal Azure ML version string.
    """
    host, _, rest = image.rpartition("/")
    name, sep, tag = rest.rpartition(":")
    if not sep or not name or "@" in rest:
        raise ValueError(
            f"image '{image}' carries no tag. An Azure ML environment "
            f"version is derived from the tag, and an untagged or digest-pinned "
            f"reference cannot supply one -- use an explicit tag such as "
            f"'{image.split('@')[0]}:11'."
        )
    return tag



def _env_setting(*names: str) -> str | None:
    """The first of ``names`` holding something other than whitespace, else None.

    Blank means UNSET here, which `os.environ.get(name, default)` does not do --
    that default fires only on *absent*, so an exported-but-empty variable sails
    straight through as ''.

    lab0 §4 appends to `~/.ffsft-env` with an *unquoted* heredoc, so
    `export FFSFT_RESOURCE_GROUP=$FFSFT_RESOURCE_GROUP` expands when the file is
    written -- and the "없으면 만듭니다" branch never exports those two, so that
    branch bakes `export FFSFT_RESOURCE_GROUP=` (empty) into the file. Every
    later command in that shell then built a target with rg='' ws='', and
    `ffsft-lifecycle status` queried a workspace named '', failed inside
    `collect_inventory`, and printed `BILLING NOW: nothing` while an endpoint
    billed $4.959/hr in the region nobody was looking at -- the exact
    misreading lab7's opening warning exists to prevent, arriving through the
    environment instead of through the wrong profile.

    `export FFSFT_WORKSPACE=` is a shell accident, not a request to target a
    workspace named ''. No Azure resource is named '', so there is no reading of
    a blank value more useful than the documented default; the same rule already
    governs `FFSFT_SERVE_IMAGE` in `endpoint.resolve_serve_image`.

    Stripping rather than testing `== ""` catches the same accident with a space
    in it, and a pasted ` rg-ffsft-kc` besides -- Azure 404s on that and prints
    the name back looking correct.
    """
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def _explicit(overrides: dict) -> dict:
    """The overrides a caller actually supplied, under the same blank-is-unset rule.

    `dataclasses.replace(target, resource_group=args.resource_group)` is how the
    submit scripts used to apply their flags, and it is a hole straight through
    `_env_setting`: `replace` writes whatever it is handed. Two shapes reach it.
    `--resource-group ""` is one, and `--resource-group "$RG"` with `RG` unset is
    the one that actually happens, because the shell hands argparse an empty
    string and argparse has no opinion about it. Either way the target that
    passed every guard in `from_env` is reassembled with rg='' -- and a workspace
    read at rg='' is the §11.4 failure the guard exists to stop, arriving through
    a flag instead of through the environment.

    A flag is a stronger statement than an environment variable, so a supplied
    one wins; a blank one is not a statement at all, so it loses to the
    environment and then to the documented default, exactly as a blank
    FFSFT_RESOURCE_GROUP does. Non-strings (`max_nodes`) pass through untouched
    -- 0 is a legitimate value there and must not be read as absent.
    """
    fields = {f.name for f in dataclasses.fields(AzureTarget)}
    unknown = sorted(set(overrides) - fields)
    if unknown:
        raise TypeError(f"AzureTarget has no field(s) {unknown}")
    clean = {}
    for name, value in overrides.items():
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        clean[name] = value
    return clean


@dataclasses.dataclass(frozen=True)
class AzureTarget:
    subscription_id: str
    resource_group: str
    workspace_name: str
    location: str = "koreacentral"
    compute_name: str = "gpu-a100-lp"
    compute_sku: str = "Standard_NC24ads_A100_v4"
    max_nodes: int = 1
    #: LowPriority is the default on purpose -- see the GPU_SKUS comment. It is
    #: both the only tier with usable quota and the only one the tenant policy
    #: permits for N-series. Nodes can be preempted, so training must checkpoint.
    vm_priority: str = "LowPriority"
    #: Which Entra directory to authenticate against. `None` means "whatever the
    #: Azure CLI has selected", which is correct until a workstation is signed in
    #: to more than one directory -- then the CLI's default can move underneath a
    #: run and every call fails with `InvalidAuthenticationTokenTenant`, an error
    #: that looks like a permissions problem and is not.
    tenant_id: str | None = None

    @classmethod
    def from_env(cls, **overrides) -> AzureTarget:
        """Build a target from ``FFSFT_*`` environment variables, flags winning.

        ``overrides`` are the values a CLI actually supplied, keyed by field
        name; each goes through `_explicit`, so a flag left off (None) or handed
        a blank by the shell falls back to the environment rather than
        overwriting it with ''. Read that docstring before adding a caller.

        Only the subscription id has no sensible default, because it is the one
        value that is account-specific and must never be committed. Everything
        else defaults to the resources this asset provisions.

        Every value goes through `_env_setting`, so blank is unset throughout --
        read that docstring before removing it. The asymmetry below is the whole
        point and is meant to be visible line by line: a missing subscription
        raises, a missing tenant stays None because absent is legitimate, and
        everything else falls back to the default CLAUDE.md "Azure environment"
        documents. A default is only safe because it is stated -- `status`
        prints the target it resolved, so a participant whose `~/.ffsft-env`
        blanked `FFSFT_WORKSPACE` sees `mlw-ffsft` instead of their own name and
        can tell that their profile, not the workspace, is what is empty.
        """
        given = _explicit(overrides)
        subscription = given.pop("subscription_id", None) or _env_setting(
            "FFSFT_SUBSCRIPTION_ID", "AZURE_SUBSCRIPTION_ID"
        )
        if not subscription:
            raise RuntimeError(
                "set FFSFT_SUBSCRIPTION_ID (or AZURE_SUBSCRIPTION_ID) to the target "
                "Azure subscription id"
            )
        resolved = dict(
            subscription_id=subscription,
            resource_group=_env_setting("FFSFT_RESOURCE_GROUP") or "rg-ffsft-kc",
            workspace_name=_env_setting("FFSFT_WORKSPACE") or "mlw-ffsft",
            location=_env_setting("FFSFT_LOCATION") or "koreacentral",
            compute_name=_env_setting("FFSFT_COMPUTE") or "gpu-a100-lp",
            compute_sku=_env_setting("FFSFT_SKU") or "Standard_NC24ads_A100_v4",
            vm_priority=_env_setting("FFSFT_VM_PRIORITY") or "LowPriority",
            tenant_id=_env_setting("FFSFT_TENANT_ID", "AZURE_TENANT_ID"),
        )
        resolved.update(given)
        return cls(**resolved)


def required_vram_gb(spec: ModelSpec, method: TuningMethod) -> int | None:
    return getattr(spec.vram_gb, method.value, None)


def check_sku_fits(
    spec: ModelSpec,
    method: TuningMethod,
    sku: str,
    vm_priority: str = "LowPriority",
) -> tuple[bool, str]:
    """Compare the registry's VRAM estimate against a SKU before we spend money.

    Also rejects SKUs that cannot be provisioned at the requested priority,
    which is a distinct failure from "too small" and surfaces as a misleading
    `not a supported VM size` error at ARM deployment time.

    Returns (fits, human readable explanation).
    """
    info = GPU_SKUS.get(sku)
    if info is None:
        return True, f"{sku} is not in GPU_SKUS, cannot verify sizing"

    if vm_priority == "LowPriority" and not info["low_priority"]:
        return False, (
            f"{sku} is not low-priority capable, and LowPriority is the only tier with "
            f"pooled quota ({TOTAL_LOW_PRIORITY_CORES} cores) and the only one the "
            f"tenant N-series deny policy allows. Requesting it as Dedicated needs "
            f"{info['family']} dedicated quota, which is absent by default."
        )

    if vm_priority == "LowPriority" and info["cores"] > TOTAL_LOW_PRIORITY_CORES:
        return False, (
            f"{sku} needs {info['cores']} cores but the region pools only "
            f"{TOTAL_LOW_PRIORITY_CORES} low-priority cores."
        )

    need = required_vram_gb(spec, method)
    if need is None:
        return True, f"{spec.key} declares no VRAM estimate for {method.value}"

    have = info["vram_gb"]
    if need <= have:
        head = have - need
        return True, (
            f"{method.value} of {spec.key} needs ~{need} GB, {sku} provides {have} GB "
            f"across {info['gpus']} GPU(s) -- {head} GB headroom"
        )
    return False, (
        f"{method.value} of {spec.key} needs ~{need} GB but {sku} only provides {have} GB. "
        f"Use a larger SKU, or switch to a cheaper method "
        f"(supported: {', '.join(m.value for m in spec.supports)})."
    )


def _credential_class():
    """Indirection so tests can substitute a fake without touching the network."""
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential


def _cli_credential_class():
    from azure.identity import AzureCliCredential

    return AzureCliCredential


def _chained_credential_class():
    from azure.identity import ChainedTokenCredential

    return ChainedTokenCredential


def _ml_client_class():
    from azure.ai.ml import MLClient

    return MLClient


def build_credential(target: AzureTarget):
    """The one credential every caller in this package uses.

    With no `target.tenant_id` this is exactly `DefaultAzureCredential()` and
    nothing changes: a workstation signed in to a single directory has no
    ambiguity to resolve.

    With a tenant, the directory is stated rather than inferred. Inferring it
    means taking whatever the Azure CLI currently has selected, which on a
    multi-directory workstation can move between two calls in the same session:

        (InvalidAuthenticationTokenTenant) The access token is from the wrong
        issuer '...'. It must match one of the tenants '...'

    The tenant is pinned on `AzureCliCredential`, not on `DefaultAzureCredential`,
    because the latter rejects the argument outright --
    `TypeError: 'tenant_id' is not supported in DefaultAzureCredential.` It
    accepts only per-credential variants (`shared_cache_tenant_id` and friends),
    none of which reach the CLI credential that actually serves the token here.

    `additionally_allowed_tenants=["*"]` accompanies it, because the CLI
    credential otherwise declines any tenant but the CLI's active one -- which
    would replace the original failure with an identical-looking one.

    What pinning the tenant does NOT do is decide *which user* asks for the
    token. That comes from the CLI's active account, which lives in one global
    file (`$AZURE_CONFIG_DIR/azureProfile.json`, default `~/.azure`) that any
    `az` call on the machine may rewrite. On a workstation signed in to two
    directories, an identity from the wrong one requesting a token for the right
    tenant fails with `AADSTS90072` -- an error that names a tenant, and so reads
    as a tenant problem even when the tenant is already pinned. `az account set`
    does not hold, because it is a write to shared state and the next writer
    wins. Point `AZURE_CONFIG_DIR` at a private copy, or supply a service
    principal so the CLI is out of the path entirely. See JOURNAL §39.

    `DefaultAzureCredential` stays behind it in the chain so the same code still
    authenticates where there is no Azure CLI. On a compute node the CLI
    credential fails immediately and the managed identity answers instead;
    dropping it would fix the workstation by breaking the cluster.
    """
    if not target.tenant_id:
        return _credential_class()()

    return _chained_credential_class()(
        _cli_credential_class()(
            tenant_id=target.tenant_id,
            additionally_allowed_tenants=["*"],
        ),
        _credential_class()(),
    )


def get_ml_client(target: AzureTarget) -> MLClient:
    return _ml_client_class()(
        credential=build_credential(target),
        subscription_id=target.subscription_id,
        resource_group_name=target.resource_group,
        workspace_name=target.workspace_name,
    )


def ensure_workspace(target: AzureTarget) -> str:
    """Create the workspace if missing. Returns its resource id."""
    from azure.ai.ml import MLClient
    from azure.ai.ml.entities import Workspace
    from azure.core.exceptions import ResourceNotFoundError

    client = MLClient(
        credential=build_credential(target),
        subscription_id=target.subscription_id,
        resource_group_name=target.resource_group,
    )
    try:
        ws = client.workspaces.get(target.workspace_name)
        return ws.id
    except ResourceNotFoundError:
        pass

    ws = Workspace(
        name=target.workspace_name,
        location=target.location,
        description="Fabric + Foundry Korean sLLM fine-tuning asset",
    )
    return client.workspaces.begin_create(ws).result().id


#: A read that did not happen, as distinct from `None`, which here means "the
#: workspace genuinely has no storage account attached". Both used to arrive at
#: `compute_role_grants` as `None` and both dropped the STORAGE_WRITE scope, so
#: the two runs were byte-identical apart from one WARNING line. Reproduced:
#:     a storage account that could not be READ produced the same grants as a
#:     workspace that HAS none: [('acrffsftkc', 'AcrPull')]
UNREAD = object()


@dataclasses.dataclass(frozen=True)
class GrantsOutcome:
    """What `grant_compute_data_roles` did, and what it never managed to read.

    `unverified` is the could-not-look half and only that half. A workspace with
    no storage account leaves it empty -- that is a measurement, and the repo's
    invariant is about not dressing silence up as one, not about treating every
    measurement as a finding.
    """

    granted: tuple[tuple[str, str], ...] = ()
    unverified: tuple[str, ...] = ()


class ComputeReadiness(str):
    """The cluster name, plus whatever could not be verified about its grants.

    A `str` subclass rather than a new type, because the return value of
    `ensure_compute` IS the operator-facing report: its only non-test caller is
    `scripts/provision_azure.py:121`, `print(f"  -> {ensure_compute(target)}")`.
    A run whose identity read failed returned exactly what a fully successful
    run returns -- measured, `'gpu-a100-lp'`, having attempted these grants:
    `[]` -- so the operator read a green line over a cluster whose jobs will die
    with `401 authentication required`.

    On a fully verified run it compares equal to the bare cluster name, so every
    existing `== target.compute_name` contract holds unchanged. `.name` is
    always the identifier; the string is always the report.
    """

    name: str
    unverified: tuple[str, ...]

    def __new__(cls, name: str, unverified: tuple[str, ...] = ()) -> ComputeReadiness:
        unverified = tuple(unverified)
        text = name if not unverified else f"{name} -- GRANTS UNVERIFIED: {'; '.join(unverified)}"
        obj = super().__new__(cls, text)
        obj.name = name
        obj.unverified = unverified
        return obj


def _has_managed_identity(existing) -> bool:
    """True only when ARM described an identity that actually exists.

    ARM spells "no managed identity" two ways and the SDK preserves the
    difference. Measured against the installed SDK's own
    `Compute._from_rest_object`, with `ManagedServiceIdentityType` declaring
    `['None', 'SystemAssigned', 'UserAssigned', 'SystemAssigned,UserAssigned']`:

        identity key OMITTED       -> entity.identity is None       -> absent
        identity {"type": "None"}  -> IdentityConfiguration(type='none')

    The second is an object, so the old guard `existing.identity is not None`
    read it as an identity that is present and skipped the repair for one of the
    two legal spellings of the same fact -- leaving the cluster in the state
    that fails a job at start with "Identity of the specified managed compute is
    not found" once the workspace storage disallows shared keys.
    """
    identity = getattr(existing, "identity", None)
    if identity is None:
        return False
    # Present beats the type string, in that order and deliberately: this guard
    # decides whether to PUT an identity ONTO an operator's cluster, so the
    # expensive direction is the false negative. A principal id or a
    # user-assigned entry is an identity that exists whatever `type` says.
    if getattr(identity, "principal_id", None):
        return True
    if getattr(identity, "user_assigned_identities", None):
        return True
    kind = str(getattr(identity, "type", "") or "")
    return kind.replace("_", "").replace(",", "").strip().lower() not in ("", "none")


def grant_compute_data_roles(target: AzureTarget, compute_name: str) -> GrantsOutcome:
    """Give the cluster's identity the data-plane roles its jobs need.

    `ensure_compute` used to *document* these two grants in a comment and make
    neither. That comment was read and still not acted on: a cluster created by
    hand on 2026-08-26 was given the storage role and not the registry one, and
    the first job died in 75 seconds with

        Failed to pull Docker image `acrffsftkc.azurecr.io/ffsft-train:12`
        ... status_code: 401, "authentication required"

    The lesson is not "remember the second grant". It is that **a new cluster
    gets a new identity and inherits nothing**, so the grants have to be made by
    whatever creates it, not by whoever remembers. Run on every call, not only on
    creation: `ensure_role` is a no-op when the role is already held, and that
    makes this repair a hand-made cluster too.

    Never raises -- a missing grant should stop a job with a clear message, but
    an unreadable role assignment must not stop a cluster from existing. It
    *reports* instead: the returned `GrantsOutcome.unverified` names every input
    it could not read, and `ensure_compute` puts that in front of the operator.
    Returning nothing was the bug: the caller could not tell a run that granted
    both roles from one that read neither.
    """
    from .deploy.identity import compute_role_grants, ensure_role
    from .train.aml_job import TRAIN_IMAGE

    client = get_ml_client(target)
    try:
        principal = getattr(
            getattr(client.compute.get(compute_name), "identity", None),
            "principal_id",
            None,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read the identity of %s: %s", compute_name, exc)
        # Not `GrantsOutcome()`: an empty grant list from here means "nothing
        # needed granting", which is precisely the sentence this read failed to
        # earn the right to say.
        return GrantsOutcome(unverified=(f"could not read the identity of {compute_name}: {exc}",))
    if not principal:
        log.warning("%s has no managed identity; skipping role grants", compute_name)
        return GrantsOutcome()

    unverified: list[str] = []
    try:
        storage_id = getattr(client.workspaces.get(target.workspace_name), "storage_account", None)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read the workspace storage account: %s", exc)
        # UNREAD, not None. `compute_role_grants` drops an unresolved scope
        # either way -- there is no scope to grant on -- but the caller now
        # learns that the STORAGE_WRITE grant is unknown rather than absent.
        storage_id = UNREAD
        unverified.append(f"could not read the workspace storage account: {exc}")

    from .deploy.identity import acr_id_for_image

    acr_id = acr_id_for_image(TRAIN_IMAGE, target.subscription_id, target.resource_group)

    granted: list[tuple[str, str]] = []
    resolved_storage = None if storage_id is UNREAD else storage_id
    for scope, role in compute_role_grants(storage_id=resolved_storage, acr_id=acr_id or None):
        result = ensure_role(scope, principal, role)
        if result.granted:
            granted.append((scope, role))
            log.info("granted %s to %s on %s", role, compute_name, scope.rsplit("/", 1)[-1])
        elif result.error:
            unverified.append(f"could not grant {role}: {result.error}")
            log.warning(
                "could not grant %s to %s automatically (%s).\nRun this yourself:\n%s",
                role, compute_name, result.error, result.manual_fix,
            )
    return GrantsOutcome(granted=tuple(granted), unverified=tuple(unverified))


def ensure_compute(target: AzureTarget) -> ComputeReadiness:
    """Create the GPU cluster if missing, scaled to zero when idle.

    Also grants the identity its data-plane roles -- see `grant_compute_data_roles`.

    Returns a `ComputeReadiness`, which IS the cluster name (`str`, equal to
    `target.compute_name` on a run that verified everything) and additionally
    carries what it could not read. The only non-test caller prints it.
    """
    from azure.ai.ml.entities import AmlCompute, IdentityConfiguration
    from azure.core.exceptions import ResourceNotFoundError

    client = get_ml_client(target)
    # Only the `get` is inside the try, and the reason is expensive. This used
    # to wrap the repair PUT below as well, so a 404 from the *repair* was read
    # as "the compute does not exist" and fell through to the create path --
    # which PUTs a fresh AmlCompute, built from the environment defaults, over
    # the same name. Reproduced with fakes at the SDK boundary
    # (tests/test_a_failed_repair_is_not_read_as_a_missing_cluster.py):
    #     PUT #1: Standard_NC96ads_A100_v4 max=8 Dedicated min=2  <- repair, 404s
    #     PUT #2: Standard_NC24ads_A100_v4 max=1 LowPriority min=0 <- fresh
    # and the cluster name was returned, so the caller was told it was fine.
    # A failed repair is a failed repair; it is not evidence of absence.
    try:
        existing = client.compute.get(target.compute_name)
    except ResourceNotFoundError:
        existing = None

    if existing is not None:
        if _has_managed_identity(existing):
            outcome = grant_compute_data_roles(target, existing.name)
            return ComputeReadiness(existing.name, outcome.unverified)
        # A cluster created without an identity is not a cosmetic gap: jobs fail
        # at start with "Identity of the specified managed compute is not found"
        # as soon as the workspace storage disallows shared keys, because the node
        # then has no way to authenticate to the datastore. Repair it in place.
        existing.identity = IdentityConfiguration(type="system_assigned")
        repaired = client.compute.begin_create_or_update(existing).result().name
        outcome = grant_compute_data_roles(target, repaired)
        return ComputeReadiness(repaired, outcome.unverified)

    cluster = AmlCompute(
        name=target.compute_name,
        type="amlcompute",
        size=target.compute_sku,
        min_instances=0,
        max_instances=target.max_nodes,
        # Idle GPU nodes are the main way this asset wastes money.
        idle_time_before_scale_down=900,
        tier=target.vm_priority,
        # Required, not optional, on any workspace whose storage account has
        # allowSharedKeyAccess=false -- which is the default under most
        # enterprise policy sets. The identity still needs data-plane roles
        # (Storage Blob Data Contributor, and AcrPull for a custom image).
        identity=IdentityConfiguration(type="system_assigned"),
    )
    created = client.compute.begin_create_or_update(cluster).result().name
    outcome = grant_compute_data_roles(target, created)
    return ComputeReadiness(created, outcome.unverified)
