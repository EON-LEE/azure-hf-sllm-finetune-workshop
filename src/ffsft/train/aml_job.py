"""Run the recipe on Azure ML instead of on whatever laptop you happen to have.

This is the managed-training path -- the Azure equivalent of a SageMaker
training job. Azure ML owns the node lifecycle, the GPU driver and the container,
so the only things this module has to get right are the image and the command.

Three decisions here are not obvious and are the difference between a job that
runs and one that never gets a node:

1. `LowPriority`. Azure ML quota is separate from Microsoft.Compute quota, and
   dedicated GPU quota is granted per family -- absent by default, at which
   point cluster creation fails with a misleading "not a supported VM size".
   Low-priority draws on one pooled regional allowance instead, and is also the
   only tier permitted by the tenant policy that denies N-series SKUs. The cost
   is preemption, so long runs must checkpoint.

2. A custom image built on ACPT rather than a curated environment. The
   Qwen3.5/3.6/3.8 checkpoints need transformers>=5.8; the curated
   `acft-hf-nlp-gpu` pins 5.5.0, and the built-in chat-completion finetune
   component exposes LoRA knobs only, with no NF4 quantisation. So
   `docker/Dockerfile.train` upgrades the model-side libraries on top of ACPT
   and the result is pushed to our own registry.

3. No code snapshot. `command(code=...)` zips the working tree and uploads it to
   the workspace storage account *from the client machine*, and this account
   refuses connections from outside its allowed networks -- a developer laptop
   included. Note the asymmetry: Azure ML itself reaches the account fine via
   the `AzureServices` trusted-services bypass, so this is a limitation of
   where the *client* sits, not of the account being unreachable. Baking the
   code into the image sidesteps it entirely, because `az acr build` runs the
   build in the registry and never touches workspace storage. It is also what
   Microsoft's own finetune components do.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from azure.ai.ml import MLClient

from ..azure_ml import AzureTarget, check_sku_fits, get_ml_client
from ..models import TuningMethod, get_model

#: Built by `az acr build` from docker/Dockerfile.train. Bump the tag whenever the
#: image is rebuilt -- the code lives inside it, so a code change is an image
#: change, and reusing a tag would silently run the old training script.
TRAIN_IMAGE = "acrffsftkc.azurecr.io/ffsft-train:5"

#: Azure ML environments are immutable per version, so this has to move with the
#: image tag or `create_or_update` returns the stale registration.
ENVIRONMENT_NAME = "ffsft-train"
ENVIRONMENT_VERSION = "5"

#: Where the code sits inside the image (see the COPY in docker/Dockerfile.train).
IMAGE_CODE_ROOT = "/opt/ffsft"


@dataclasses.dataclass
class JobSpec:
    """What to run. Deliberately mirrors the CLI flags of the trainer."""

    model_key: str = "qwen3.8-27b"
    mix: str = "ko_smoke"
    max_steps: int = -1
    max_samples: int | None = None
    max_seq_length: int = 1024
    batch_size: int = 1
    grad_accum: int = 16
    rank: int = 16
    experiment_name: str = "ffsft-qlora"
    display_name: str | None = None
    #: Run the node self-test instead of training. Cheap, and the only honest
    #: way to know a cluster works before committing to a long run.
    preflight: bool = False
    #: Accept PEFT's per-architecture LoRA defaults for a model whose registry
    #: entry declares no `lora_target_modules`. Off by default because on a
    #: hybrid-attention model the defaults adapt a small minority of layers and
    #: train without any error -- see `qlora.resolve_target_modules`.
    allow_default_lora_targets: bool = False
    #: Mount `uri_folder` outputs on the node. OFF by default, and that default
    #: is load-bearing rather than a preference: the mount is a FUSE session the
    #: *node* opens against `workspaceblobstore`, and it is not covered by the
    #: `AzureServices` trusted-service bypass that lets the Azure ML control
    #: plane reach the same account. On a workspace whose storage account has
    #: public network access disabled and no private endpoint it fails with
    #: `data-capability.AssetMountOutputSession.Exception`, inside the
    #: lifecycler, *before* the user command starts -- so the run pays for node
    #: allocation and a 9 GB image pull and then trains nothing. Writing to
    #: `./outputs` instead is uploaded by the run-history artifact service,
    #: which is a separate code path that mounts nothing.
    mount_outputs: bool = False


def ensure_environment(client: MLClient) -> str:
    """Register the prebuilt training image as an Azure ML environment.

    This only wraps an image reference; Azure ML does not build anything. The
    build already happened in ACR, which keeps a multi-gigabyte context off the
    client's uplink and away from the workspace storage account.
    """
    from azure.ai.ml.entities import Environment
    from azure.core.exceptions import ResourceNotFoundError

    try:
        env = client.environments.get(ENVIRONMENT_NAME, version=ENVIRONMENT_VERSION)
        return f"{env.name}:{env.version}"
    except ResourceNotFoundError:
        pass

    env = Environment(
        name=ENVIRONMENT_NAME,
        version=ENVIRONMENT_VERSION,
        description="ACPT + Hugging Face QLoRA stack for Qwen3.x hybrid-attention models",
        image=TRAIN_IMAGE,
    )
    created = client.environments.create_or_update(env)
    return f"{created.name}:{created.version}"


def build_command(job: JobSpec) -> str:
    """The shell line the node runs.

    No `pip install` step: the package is already on `PYTHONPATH` inside the
    image, and installing at job start would re-resolve dependencies against
    PyPI and could quietly replace the torch build that ACPT validated.
    """
    if job.preflight:
        return "python -m ffsft.train.preflight"

    parts = [
        "python -m ffsft.train.qlora",
        f"--model {job.model_key}",
        f"--mix {job.mix}",
        f"--rank {job.rank}",
        f"--max-seq-length {job.max_seq_length}",
        f"--batch-size {job.batch_size}",
        f"--grad-accum {job.grad_accum}",
    ]
    # `${{outputs.model_dir}}` only resolves to a path when the output is mounted.
    parts.append(
        "--output-dir ${{outputs.model_dir}}" if job.mount_outputs else "--output-dir ./outputs"
    )
    if job.max_steps > 0:
        parts.append(f"--max-steps {job.max_steps}")
    if job.max_samples:
        parts.append(f"--max-samples {job.max_samples}")
    if job.allow_default_lora_targets:
        parts.append("--allow-default-lora-targets")
    return " ".join(parts)


def submit(target: AzureTarget, job: JobSpec, wait: bool = False) -> dict:
    from azure.ai.ml import Output, command

    client = get_ml_client(target)

    if not job.preflight:
        spec = get_model(job.model_key)
        ok, why = check_sku_fits(
            spec, TuningMethod.QLORA, target.compute_sku, target.vm_priority
        )
        if not ok:
            raise ValueError(f"refusing to submit: {why}")
        # The same refusal `qlora.resolve_target_modules` makes on the node, made
        # here instead. On the node it costs a node allocation, a 9 GB image pull
        # and a model download before it fires; from the registry it is free.
        if not spec.lora_target_modules and not job.allow_default_lora_targets:
            raise ValueError(
                f"refusing to submit: model '{spec.key}' declares no "
                f"lora_target_modules, so the trainer would refuse on the node "
                f"after the cluster has already been paid for. Either add the "
                f"modules to configs/models.yaml (run "
                f"`python scripts/probe_architecture.py {spec.key}` to discover "
                f"them) or set JobSpec(allow_default_lora_targets=True) to "
                f"accept PEFT's defaults."
            )

    environment = ensure_environment(client)

    outputs = (
        {"model_dir": Output(type="uri_folder"), "report": Output(type="uri_folder")}
        if job.mount_outputs
        else None
    )

    node = command(
        # Deliberately no `code=`: see the module docstring.
        command=build_command(job),
        environment=f"azureml:{environment}",
        compute=target.compute_name,
        experiment_name=job.experiment_name,
        display_name=job.display_name
        or (f"preflight-{target.compute_sku}" if job.preflight else f"qlora-{job.model_key}"),
        environment_variables={
            # Keep the 54 GB of weights off the container's root filesystem.
            "HF_HOME": "/mnt/hf",
            "HF_HUB_ENABLE_HF_TRANSFER": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "PYTHONPATH": f"{IMAGE_CODE_ROOT}/src",
        },
        outputs=outputs,
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
    }
    if wait:
        client.jobs.stream(submitted.name)
        info["status"] = client.jobs.get(submitted.name).status
    return info
