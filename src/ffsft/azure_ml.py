"""Provision the Azure ML side of the asset.

Creates (idempotently) the workspace and the GPU compute cluster that the
training backend submits jobs to. Sizing comes from the model registry, so the
cluster matches whatever model is selected rather than being hardcoded.

    ffsft azure provision --model qwen3.8-27b
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # keep azure-ai-ml an optional import
    from azure.ai.ml import MLClient

from .models import ModelSpec, TuningMethod

#: GPU SKUs we know how to size against, with usable VRAM per node.
#: `cores` is what the subscription quota is actually measured in.
#:
#: `aml_supported` is a separate, non-obvious axis. Azure ML's compute
#: provisioner keeps its own VM-size allowlist that is NARROWER than the region's
#: `Microsoft.MachineLearningServices/locations/{region}/vmSizes` response. The
#: NVadsA10v5 family is listed by that API in koreacentral but is rejected at
#: create time for BOTH AmlCompute clusters and compute instances with
#: `InvalidPropertyValue ... not a supported VM size` (verified 2026-08-20).
#: So quota for those SKUs is real but unusable from Azure ML — they only work
#: as plain Microsoft.Compute VMs. Check this before requesting quota.
GPU_SKUS: dict[str, dict] = {
    "Standard_NV36ads_A10_v5": {
        "vram_gb": 24, "gpus": 1, "cores": 36,
        "family": "standardNVADSA10v5Family", "aml_supported": False,
    },
    "Standard_NV72ads_A10_v5": {
        "vram_gb": 48, "gpus": 2, "cores": 72,
        "family": "standardNVADSA10v5Family", "aml_supported": False,
    },
    "Standard_NC4as_T4_v3": {
        "vram_gb": 16, "gpus": 1, "cores": 4,
        "family": "standardNCASv3_T4Family", "aml_supported": True,
    },
    "Standard_NC64as_T4_v3": {
        "vram_gb": 64, "gpus": 4, "cores": 64,
        "family": "standardNCASv3_T4Family", "aml_supported": True,
    },
    "Standard_NC24ads_A100_v4": {
        "vram_gb": 80, "gpus": 1, "cores": 24,
        "family": "standardNCADSA100v4Family", "aml_supported": True,
    },
    "Standard_NC48ads_A100_v4": {
        "vram_gb": 160, "gpus": 2, "cores": 48,
        "family": "standardNCADSA100v4Family", "aml_supported": True,
    },
    "Standard_NC40ads_H100_v5": {
        "vram_gb": 94, "gpus": 1, "cores": 40,
        "family": "standardNCadsH100v5Family", "aml_supported": True,
    },
    "Standard_ND96isr_H100_v5": {
        "vram_gb": 752, "gpus": 8, "cores": 96,
        "family": "standardNDv5H100Family", "aml_supported": True,
    },
}


@dataclasses.dataclass(frozen=True)
class AzureTarget:
    subscription_id: str
    resource_group: str
    workspace_name: str
    location: str = "koreacentral"
    compute_name: str = "gpu-cluster"
    compute_sku: str = "Standard_NC24ads_A100_v4"
    max_nodes: int = 1


def required_vram_gb(spec: ModelSpec, method: TuningMethod) -> int | None:
    return getattr(spec.vram_gb, method.value, None)


def check_sku_fits(spec: ModelSpec, method: TuningMethod, sku: str) -> tuple[bool, str]:
    """Compare the registry's VRAM estimate against a SKU before we spend money.

    Also rejects SKUs Azure ML refuses to provision at all, which is a distinct
    failure from "too small" and is invisible until the ARM deployment fails.

    Returns (fits, human readable explanation).
    """
    info = GPU_SKUS.get(sku)
    if info is None:
        return True, f"{sku} is not in GPU_SKUS, cannot verify sizing"

    if not info.get("aml_supported", True):
        return False, (
            f"{sku} cannot be provisioned by Azure ML. The {info['family']} family is "
            f"absent from the AmlCompute allowlist even though the region reports it as "
            f"available, so both clusters and compute instances fail with "
            f"InvalidPropertyValue. Use an NC-series SKU, or run it as a plain Azure VM."
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


def get_ml_client(target: AzureTarget) -> MLClient:
    from azure.ai.ml import MLClient
    from azure.identity import DefaultAzureCredential

    return MLClient(
        credential=DefaultAzureCredential(),
        subscription_id=target.subscription_id,
        resource_group_name=target.resource_group,
        workspace_name=target.workspace_name,
    )


def ensure_workspace(target: AzureTarget) -> str:
    """Create the workspace if missing. Returns its resource id."""
    from azure.ai.ml import MLClient
    from azure.ai.ml.entities import Workspace
    from azure.core.exceptions import ResourceNotFoundError
    from azure.identity import DefaultAzureCredential

    client = MLClient(
        credential=DefaultAzureCredential(),
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
    from azure.ai.ml.entities import AmlCompute
    from azure.core.exceptions import ResourceNotFoundError

    client = get_ml_client(target)
    try:
        return client.compute.get(target.compute_name).name
    except ResourceNotFoundError:
        pass

    cluster = AmlCompute(
        name=target.compute_name,
        type="amlcompute",
        size=target.compute_sku,
        min_instances=0,
        max_instances=target.max_nodes,
        # Idle GPU nodes are the main way this asset wastes money.
        idle_time_before_scale_down=120,
        tier="Dedicated",
    )
    return client.compute.begin_create_or_update(cluster).result().name
