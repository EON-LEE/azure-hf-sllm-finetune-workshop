"""Register a finished training job's adapter as an Azure ML model asset.

The bridge between the training half of this asset and the serving half. Every
hosted pattern -- managed online *and* batch -- deploys a registered model, so
until a job could produce one, nothing downstream could be exercised at all.

Verified end to end on 2026-08-24. Job `helpful_sand_971pqxtj0l` trained
`kanana2-1.3b` for 30 steps; `register_adapter` turned its `model_dir` output
into `kanana2-1_3b-ko-lora:1`; and a follow-up job mounted that asset read-only
and listed 19 files totalling 133,476,918 bytes, including a 37,415,384-byte
`adapter_model.safetensors`. Registration alone proves nothing -- it happily
succeeds against a folder that does not exist -- so the mount is the real check
and `ffsft.deploy.model_asset` is only half the story without it.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from azure.ai.ml import MLClient

log = logging.getLogger("ffsft.deploy.model_asset")

#: Azure ML resource names allow these and nothing else. Measured, not guessed:
#: registering `kanana2-1.3b-ko-lora` returns `(RequestInvalid) Resource name is
#: invalid. Resource name can only contain alphanumeric characters, dashes, and
#: underscores, with a limit of 255 characters.`
_ILLEGAL = re.compile(r"[^A-Za-z0-9_-]")
_MAX_NAME = 255

#: Where a declared output with no explicit path actually lands. The intuitive
#: `azureml://jobs/{job}/outputs/{name}` is rejected by the service with
#: `NoMatchingArtifactsFoundFromJob`, so this shape is the only one that works.
_OUTPUT_URI = "azureml://datastores/{datastore}/paths/azureml/{job}/{output}/"


def asset_name(model_key: str, *, suffix: str | None = None) -> str:
    """A registry key turned into a name Azure ML will accept.

    Almost every key in `configs/models.yaml` carries a dot -- `kanana2-1.3b`,
    `qwen3.8-27b`, `qwen3.5-0.8b` -- and a dot is exactly what the service
    rejects, so this is the common path rather than a defensive corner.

    The transformation is lossy: `kanana2-1_3b` cannot be looked up in the
    registry. `register_adapter` therefore keeps the original key in a tag.
    """
    if not model_key or not model_key.strip():
        raise ValueError("model key is empty; an asset name has to come from somewhere")

    parts = [_ILLEGAL.sub("_", model_key)]
    if suffix:
        parts.append(_ILLEGAL.sub("_", suffix))
    name = "-".join(parts)

    if not re.search(r"[A-Za-z0-9]", name):
        raise ValueError(
            f"model key {model_key!r} has no usable characters for an Azure ML "
            f"name; it would register as {name!r}, which no one can trace back "
            f"to a model."
        )
    return name[:_MAX_NAME]


def job_output_uri(
    job_name: str, output: str = "model_dir", datastore: str = "workspaceblobstore"
) -> str:
    """The datastore URI a declared job output is uploaded to.

    Do not substitute `job.outputs[name].path` for this. ARM reports that field
    as `null` even for an upload that demonstrably succeeded, so reading it back
    looks like a failed run and is not evidence of one.
    """
    if not job_name or not job_name.strip():
        raise ValueError("job name is empty; there is no output path without one")
    return _OUTPUT_URI.format(datastore=datastore, job=job_name, output=output)


def register_adapter(
    client: MLClient,
    job_name: str,
    model_key: str,
    *,
    suffix: str = "ko-lora",
    base_model: str | None = None,
    mix: str | None = None,
    output: str = "model_dir",
    description: str | None = None,
    extra_tags: dict[str, str] | None = None,
) -> str:
    """Register a job's adapter output and return `name:version`.

    Registered as `custom_model`: a PEFT adapter folder is not an MLflow model,
    and claiming otherwise makes Azure ML look for an `MLmodel` file and fail at
    rollout -- long after the deployment has started costing money.

    This does not verify that anything is actually at the URI. The service does
    not check either, so a successful call here is not evidence of a trained
    model; mount the asset from a job and list it. See the module docstring.
    """
    from azure.ai.ml.constants import AssetTypes
    from azure.ai.ml.entities import Model

    name = asset_name(model_key, suffix=suffix)
    path = job_output_uri(job_name, output=output)

    tags: dict[str, Any] = {"job": job_name, "model_key": model_key}
    if base_model:
        tags["base_model"] = base_model
    if mix:
        tags["mix"] = mix
    if extra_tags:
        tags.update(extra_tags)

    registered = client.models.create_or_update(
        Model(
            name=name,
            path=path,
            type=AssetTypes.CUSTOM_MODEL,
            description=description or f"QLoRA adapter for {model_key} from job {job_name}",
            tags=tags,
        )
    )
    ref = f"{registered.name}:{registered.version}"
    log.info("registered %s from %s", ref, path)
    return ref
