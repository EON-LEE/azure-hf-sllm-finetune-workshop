"""Submit the LoRA merge as an Azure ML job.

`ffsft.deploy.merge` runs on the node; this builds the job that runs it. The two
are separate because merging is not a laptop task -- folding an adapter into a
27B base means materialising ~54 GB of bf16 weights, and `merge_adapter` loads
the base with `device_map="auto"` precisely so it can spill across an A100 and
host RAM.

This module exists because the shape of that job was, until now, recorded
nowhere except inside a completed run. `heroic_kettle_sl64y6tznv` merged
kanana2-1.3b successfully on 2026-08-24 and every detail that made it work --
the `custom_model` input type, `ro_mount`, the `merged` output being *declared*
so it survives the node -- had to be read back off the finished job with the
SDK. Reassembling that by hand for the next model is how one of those details
gets dropped.

It reuses `train.aml_job.ensure_environment`: the merge runs the same image the
trainer does, because the merge code is already baked into it. Nothing here
needs a new image tag, which is the whole reason this is client-side code.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from azure.ai.ml import MLClient

from ..azure_ml import AzureTarget, get_ml_client
from ..models import get_model
from ..train.aml_job import TRAIN_IMAGE, ensure_environment

#: Kept distinct from `ffsft-qlora` so the studio's experiment list separates
#: "what was trained" from "what was packaged for serving".
DEFAULT_EXPERIMENT = "ffsft-merge"


@dataclasses.dataclass
class MergeSpec:
    """What to merge. Mirrors the CLI flags of `ffsft.deploy.merge`."""

    #: Registry key of the *base* model. Must match the adapter's base or the
    #: merge produces confident nonsense -- see `_check_adapter_matches`.
    model_key: str = "qwen3.8-27b"
    #: A registered model asset, `name:version`. Not a raw path: the node cannot
    #: open a session against `workspaceblobstore` by URI alone, and an asset
    #: input is what makes Azure ML mount it for us.
    adapter: str = ""
    #: bf16, never 4-bit. The adapter was trained against an NF4 view of the base
    #: but the deltas are full precision, and merging them back into a quantised
    #: base bakes the quantisation error into the weights permanently.
    dtype: str = "bfloat16"
    device_map: str = "auto"
    max_shard_size: str = "4GB"
    experiment_name: str = DEFAULT_EXPERIMENT
    display_name: str | None = None
    #: The SDK's own default. Stated rather than inherited so a future SDK
    #: change to it shows up as a diff here instead of as an OOM on the node.
    shm_size: str = "2g"

    def declared_outputs(self) -> set[str]:
        """`merged` and nothing else.

        A v2 command job collects its logs and its *declared* outputs; whatever
        else the script writes dies with the node. Two completed 27B training
        runs lost their adapters to exactly this, which is why the name is
        pinned here rather than left to the caller.
        """
        return {"merged"}


def build_command(spec: MergeSpec) -> str:
    """The shell line the node runs.

    `${{inputs.adapter}}` and `${{outputs.merged}}` are resolved by Azure ML to
    local paths on the node, so the merge script needs no Azure awareness at all.
    """
    parts = [
        "python -m ffsft.deploy.merge",
        f"--model {spec.model_key}",
        "--adapter ${{inputs.adapter}}",
        "--output ${{outputs.merged}}",
        f"--dtype {spec.dtype}",
        f"--device-map {spec.device_map}",
        f"--max-shard-size {spec.max_shard_size}",
    ]
    return " ".join(parts)


def split_asset_ref(ref: str) -> tuple[str, str]:
    """`name:version` -> (name, version).

    A bare name is rejected rather than resolved to `latest`. "Latest" moves:
    registering a second adapter between reading and merging would silently
    change what got merged, and the merge is the step whose output is served.
    """
    name, sep, version = ref.rpartition(":")
    if not sep or not name or not version:
        raise ValueError(
            f"adapter {ref!r} must be an explicit 'name:version'. A bare name would "
            f"resolve to whatever is latest at submit time, which is not the same "
            f"thing as what you looked at."
        )
    return name, version


def _check_adapter_matches(client: MLClient, spec: MergeSpec) -> None:
    """Refuse to merge an adapter into a base it was not trained against.

    `asset_name()` is lossy -- `qwen3.8-27b` registers as `qwen3_8-27b` -- so the
    original key is preserved in a `model_key` tag, and that tag is the only
    reliable way to tell what a registered folder actually adapts. Without this
    check the mismatch is not an error anywhere: PEFT applies the deltas to
    whatever module names happen to collide, `save_pretrained` succeeds, and the
    first sign of trouble is a served model that emits fluent garbage.

    A missing tag is not treated as a mismatch. Assets registered before the tag
    existed are legitimate, and refusing them would make this function harder to
    adopt than to skip.
    """
    from azure.core.exceptions import ResourceNotFoundError

    name, version = split_asset_ref(spec.adapter)
    try:
        asset = client.models.get(name, version=version)
    except ResourceNotFoundError as exc:
        raise ValueError(
            f"adapter asset '{spec.adapter}' does not exist in this workspace. "
            f"Register the training job's model_dir output first "
            f"(ffsft.deploy.model_asset.register_adapter)."
        ) from exc

    declared = (asset.tags or {}).get("model_key")
    if declared and declared != spec.model_key:
        raise ValueError(
            f"refusing to merge: adapter '{spec.adapter}' is tagged "
            f"model_key='{declared}' but the merge was asked for "
            f"'{spec.model_key}'. Merging an adapter into the wrong base raises "
            f"no error at any layer -- it produces a model that loads, serves and "
            f"is wrong."
        )


def submit(target: AzureTarget, spec: MergeSpec, wait: bool = False) -> dict:
    """Submit the merge. Refuses, before spending anything, on what is checkable.

    The registry lookup is the cheapest of the checks and the most valuable: a
    typo'd model key would otherwise surface after node allocation and a 9 GB
    image pull.
    """
    from azure.ai.ml import Input, Output, command
    from azure.ai.ml.constants import AssetTypes

    if not spec.adapter:
        raise ValueError("MergeSpec.adapter is empty; there is nothing to merge")

    base = get_model(spec.model_key)
    if not base.hf_id:
        raise ValueError(
            f"refusing to submit: model '{spec.model_key}' declares no hf_id, so "
            f"the node has no base weights to merge into."
        )

    client = get_ml_client(target)
    _check_adapter_matches(client, spec)

    environment = ensure_environment(client)

    node = command(
        # No `code=`: the package is baked into the image. See train.aml_job.
        command=build_command(spec),
        environment=f"azureml:{environment}",
        compute=target.compute_name,
        experiment_name=spec.experiment_name,
        display_name=spec.display_name or f"merge-{spec.model_key}",
        inputs={
            "adapter": Input(
                type=AssetTypes.CUSTOM_MODEL,
                path=f"azureml:{spec.adapter}",
                # Read-only: the merge reads the adapter and writes elsewhere, and
                # a writable mount of a registered asset invites a job to mutate
                # something another job already depends on.
                mode="ro_mount",
            )
        },
        outputs={
            name: Output(type="uri_folder", mode="upload")
            for name in sorted(spec.declared_outputs())
        },
        environment_variables={
            "HF_HOME": "/mnt/hf",
            "HF_HUB_ENABLE_HF_TRANSFER": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "PYTHONPATH": "/opt/ffsft/src",
        },
        shm_size=spec.shm_size,
    )

    submitted = client.jobs.create_or_update(node)
    info = {
        "name": submitted.name,
        "status": submitted.status,
        "studio_url": submitted.studio_url,
        "compute": target.compute_name,
        "sku": target.compute_sku,
        "priority": target.vm_priority,
        "environment": environment,
        "image": TRAIN_IMAGE,
        "adapter": spec.adapter,
        "base_model": base.hf_id,
    }
    if wait:
        client.jobs.stream(submitted.name)
        info["status"] = client.jobs.get(submitted.name).status
    return info
