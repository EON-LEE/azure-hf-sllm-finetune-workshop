"""Provision the Azure ML side of the asset.

Creates (idempotently) the workspace and the GPU compute cluster that the
training backend submits jobs to. Sizing comes from the model registry, so the
cluster matches whatever model is selected rather than being hardcoded.

    ffsft azure provision --model qwen3.8-27b
"""

from __future__ import annotations

import dataclasses
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # keep azure-ai-ml an optional import
    from azure.ai.ml import MLClient

from .models import ModelSpec, TuningMethod

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
    def from_env(cls) -> AzureTarget:
        """Build a target from ``FFSFT_*`` environment variables.

        Only the subscription id has no sensible default, because it is the one
        value that is account-specific and must never be committed. Everything
        else defaults to the resources this asset provisions.
        """
        subscription = os.environ.get("FFSFT_SUBSCRIPTION_ID") or os.environ.get(
            "AZURE_SUBSCRIPTION_ID"
        )
        if not subscription:
            raise RuntimeError(
                "set FFSFT_SUBSCRIPTION_ID (or AZURE_SUBSCRIPTION_ID) to the target "
                "Azure subscription id"
            )
        tenant = os.environ.get("FFSFT_TENANT_ID") or os.environ.get("AZURE_TENANT_ID")
        return cls(
            subscription_id=subscription,
            resource_group=os.environ.get("FFSFT_RESOURCE_GROUP", "rg-ffsft-kc"),
            workspace_name=os.environ.get("FFSFT_WORKSPACE", "mlw-ffsft"),
            location=os.environ.get("FFSFT_LOCATION", "koreacentral"),
            compute_name=os.environ.get("FFSFT_COMPUTE", "gpu-a100-lp"),
            compute_sku=os.environ.get("FFSFT_SKU", "Standard_NC24ads_A100_v4"),
            vm_priority=os.environ.get("FFSFT_VM_PRIORITY", "LowPriority"),
            tenant_id=(tenant or "").strip() or None,
        )


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


def _ml_client_class():
    from azure.ai.ml import MLClient

    return MLClient


def build_credential(target: AzureTarget):
    """The one credential every caller in this package uses.

    When `target.tenant_id` is set the directory is stated rather than inferred.
    Inferring it means taking whatever the Azure CLI currently has selected,
    which on a multi-directory workstation can change between two calls in the
    same session and produces:

        (InvalidAuthenticationTokenTenant) The access token is from the wrong
        issuer '...' It must match one of the tenants '...'

    `additionally_allowed_tenants=["*"]` goes with it. Without it the credential
    declines to reuse a cached CLI login for any tenant but its own default, so
    pinning alone would swap one authentication failure for another.

    When no tenant is known the argument is omitted entirely -- an explicit
    `None` is a different request from silence, and the CLI's own default is the
    right answer for a workstation with nothing to disambiguate.
    """
    kwargs: dict[str, object] = {}
    if target.tenant_id:
        kwargs["tenant_id"] = target.tenant_id
        kwargs["additionally_allowed_tenants"] = ["*"]
    return _credential_class()(**kwargs)


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


def ensure_compute(target: AzureTarget) -> str:
    """Create the GPU cluster if missing, scaled to zero when idle."""
    from azure.ai.ml.entities import AmlCompute, IdentityConfiguration
    from azure.core.exceptions import ResourceNotFoundError

    client = get_ml_client(target)
    try:
        existing = client.compute.get(target.compute_name)
        if existing.identity is not None:
            return existing.name
        # A cluster created without an identity is not a cosmetic gap: jobs fail
        # at start with "Identity of the specified managed compute is not found"
        # as soon as the workspace storage disallows shared keys, because the node
        # then has no way to authenticate to the datastore. Repair it in place.
        existing.identity = IdentityConfiguration(type="system_assigned")
        return client.compute.begin_create_or_update(existing).result().name
    except ResourceNotFoundError:
        pass

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
    return client.compute.begin_create_or_update(cluster).result().name
