"""Serve the merged model and load-test it inside a single Azure ML command job.

Why the load test does not run against an endpoint
--------------------------------------------------
It was supposed to. `ffsft.deploy.endpoint` builds a managed online deployment
and `loadtest.py` was written to point at its scoring URI. That path is closed
in this subscription, but it is worth being exact about *which* part is closed,
because an earlier version of this note was not and the imprecision cost a
round of wrong conclusions (VERIFIED 51).

Working: the endpoint resource itself. `ffsft-ep-probe` was created in 69
seconds. Nothing about Azure Policy, RBAC or the endpoint feature is blocked,
and the A10 v5 family has 72 dedicated cores of quota at both subscription and
workspace scope -- exactly one `Standard_NV36ads_A10_v5` at the endpoint's 2x
core multiplier. A deployment create against it is accepted and begins
provisioning.

Closed: node allocation, and only that. Every A10 v5 SKU in koreacentral
carries a `Zone` restriction of `NotAvailableForSubscription` over zones 1, 2
and 3, while the SKU is offered in zones 2 and 3 -- so no zone survives. The
quota is real and unusable. A managed endpoint rejects LowPriority, so unlike
an AmlCompute cluster it has no separate pool that ignores the restriction, and
five rollouts sat at `percentComplete: 0.0` for 50 to 113 minutes each without
ever getting a node (VERIFIED 40). `preflight.online_endpoint_blocker` reads
that same restriction field and refuses the spend up front.

Not a way out: other regions. A10 ML quota measured 0 in koreasouth, japaneast,
southeastasia, eastus, westus3 and westeurope, so moving the workspace trades a
restricted grant for no grant. The only other koreacentral GPU families with
quota are `standardNCFamily` and `standardNVFamily` -- K80 and M60, compute
capability 3.7 and 5.2, below the 7.0 vLLM requires and far below the memory a
27B needs at any precision.

AmlCompute LowPriority is a different pool and does get A100s here -- it is
what trained the adapter. So the server moves to the compute that exists.

What that changes about the numbers
-----------------------------------
Kept: TTFT, TPOT and the p50/p95/p99 spread of a real 27B decoding on a real
A100, with weights in bf16 rather than quantised down to fit a 24 GB card we
cannot get. Those describe the model and the GPU, which is what the sweep is
for.

Lost: TLS termination, endpoint auth, one WAN hop, and anything about Azure's
routing or autoscaling. A report produced here must not be presented as an
endpoint SLO. The client is in the same container as the server.
"""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from azure.ai.ml import MLClient

    from ..models.spec import ModelSpec

from ..azure_ml import GPU_SKUS, AzureTarget, get_ml_client
from ..deploy.endpoint import serving_env
from ..deploy.merge_job import split_asset_ref
from ..models import get_model
from ..train.aml_job import image_tag

#: Built by `az acr build` from docker/Dockerfile.bench, which is FROM the serve
#: image -- so this tag moves when either the serving stack or `ffsft` changes.
#: Same rule as TRAIN_IMAGE: the code is inside the image, so a code change is
#: an image change and reusing a tag runs the old script.
#:
#: `:1` exists in the registry and is superseded, not broken. `az acr build`
#: snapshots the context when the build is *submitted*, so the two entrypoint
#: fixes made while it was pulling its 9 GB base -- the overridable server
#: command that lets the script be exercised without a GPU, and dropping `-m` to
#: silence runpy's double-import warning -- landed after the snapshot. Retagging
#: `:1` would have been the cheaper move and the exact thing this comment warns
#: against, so the tag advanced instead.
#:
#: `:2` ran on a real A100 and failed in five minutes: the entrypoint took its
#: paths from a `VAR=value` command prefix, which the serve image's own
#: `ENV MODEL_PATH` shadowed, so vLLM was launched against a path that does not
#: exist on a job node (docs/VERIFIED.md §42). `:4` takes them as arguments and
#: resolves the model directory itself before starting the server.
#:
#: `:3` was cancelled mid-build and never pushed. Its context had already been
#: snapshotted when the report-to-stdout block was found to swallow the JSON's
#: closing brace, and the same snapshot-at-submission rule that stranded `:1`
#: applied again -- so the run was cancelled rather than allowed to produce an
#: image the repo would then have to describe as subtly wrong.
#: `:4` fixed the model path but could still only report through stdout and
#: `./outputs`, and both of those are blob-backed: job helpful_jelly_gndv8d135q
#: proved `jobs.stream` returns nothing but RunId and Execution Summary on this
#: workspace. It would have run correctly and measured nothing readable. `:5`
#: adds mlflow in a `--target` directory and publishes the sweep as metrics.
#: `:5` proved the channel works -- job careful_door_6fqvn7v4x4 died in vLLM
#: startup and said so from outside the VNet -- but its tail carried only the
#: API server's re-raise, which names nothing. `:6` added a cause window that
#: opens at the first error instead of the last, and quirky_bee_4yh061560n
#: proved it: the failure is inside `load_model`. It stopped there, because a
#: traceback names its failure on the LAST line and the window kept the first.
#: `:7` keeps both ends of the block and strips the 57-character speaker stamp
#: that was eating a third of every tag.
#: `:8` is `:7` plus `ffsft-serve:4`, which stops handing a text-only merge the
#: multimodal flag. purple_wolf_g3hhc4q5qj named the failure exactly:
#: "ValueError: There is no module or parameter named 'language_model' in
#: Qwen3_5Model" -- see docs/VERIFIED.md 46.
BENCH_IMAGE = "acrffsftkc.azurecr.io/ffsft-bench:9"

BENCH_ENVIRONMENT_NAME = "ffsft-bench"

#: Derived from the tag, never typed by hand -- see `train.aml_job.image_tag`.
BENCH_ENVIRONMENT_VERSION = image_tag(BENCH_IMAGE)

#: Bytes per parameter at bf16. The whole reason this job exists on an 80 GB
#: card instead of a 24 GB one is that this number does not have to be 0.5.
BF16_BYTES_PER_PARAM = 2


@dataclasses.dataclass
class BenchSpec:
    """What to serve and how hard to hit it."""

    model_asset: str
    model_key: str = "qwen3.8-27b"

    #: 4096 rather than the model's 262144. Context length costs KV cache per
    #: sequence, and a sweep that runs out of KV blocks measures preemption
    #: rather than latency. The prompts in `loadtest.DEFAULT_PROMPTS` are short.
    max_model_len: int = 4096

    #: 0.90 of 80 GB is 72 GB, against ~54 GB of bf16 weights. The remaining
    #: ~18 GB covers KV, Gated-DeltaNet recurrent state and CUDA graphs. Raising
    #: this buys concurrency and removes the margin that keeps a 40-minute
    #: LowPriority job from ending in an OOM.
    gpu_memory_utilization: float = 0.90

    #: GDN state is allocated per sequence slot up front, not on demand
    #: (docs/VERIFIED.md §35.3), so this is a fixed VRAM cost rather than a
    #: ceiling. 16 slots is what the headroom above pays for comfortably.
    max_num_seqs: int = 16

    #: Sweeps one level past `max_num_seqs` on purpose: 16 saturates the batch
    #: and 32 measures what queueing behind a full batch costs. Going further
    #: would only measure the queue.
    concurrency: str = "1,2,4,8,16,32"

    #: Must be at least the highest concurrency level, and is 2x it here so the
    #: top level runs two full waves rather than one. `loadtest.run_level` sends
    #: exactly this many requests through a semaphore of `concurrency`, so a
    #: value below the level silently caps the real concurrency at the number of
    #: requests and reports it under the level's name: asking for 32 with 16
    #: requests measures 16-way load and labels the row 32. `__post_init__`
    #: refuses that rather than leaving it to be noticed in the table.
    requests_per_level: int = 64
    max_tokens: int = 128
    ttft_slo: float = 1.0

    #: None means bf16. This field exists so the same job can re-measure the
    #: quantised configuration the A10 deployment would have used, for
    #: comparison -- not because anything here needs it.
    quantization: str | None = None

    #: Wall-clock budget for weights to load and the engine to answer /health.
    startup_timeout: int = 3600

    experiment_name: str = "ffsft-bench"
    display_name: str | None = None
    extra_args: str | None = None
    shm_size: str = "2g"

    def __post_init__(self) -> None:
        levels = self.concurrency_levels()
        if not levels:
            raise ValueError("BenchSpec.concurrency is empty; there is nothing to sweep")
        if min(levels) < 1:
            raise ValueError(f"concurrency levels must be >= 1, got {sorted(levels)}")
        top = max(levels)
        if self.requests_per_level < top:
            raise ValueError(
                f"requests_per_level={self.requests_per_level} is below the highest "
                f"concurrency level ({top}). loadtest.run_level bounds "
                f"{self.requests_per_level} requests with a semaphore of {top}, so "
                f"only {self.requests_per_level} would ever be in flight and the row "
                f"would still be labelled {top}. Raise requests_per_level to at "
                f"least {top}."
            )

    def concurrency_levels(self) -> list[int]:
        return [int(x) for x in self.concurrency.split(",") if x.strip()]

    def declared_outputs(self) -> set[str]:
        """`bench` and nothing else.

        A command job keeps its logs and its *declared* outputs; anything else
        the node writes dies with it. Two completed training runs lost their
        adapters to exactly that, which is why the name is pinned here rather
        than left to the caller.
        """
        return {"bench"}

    def vllm_extra_args(self) -> str:
        """Flags appended to the ones `serve_entrypoint.sh` derives from the model.

        `--max-num-seqs` is here rather than in the entrypoint because it is a
        capacity decision about this node, not an architecture fact about the
        model, and the entrypoint's job is to turn the registry's architecture
        facts into flags.
        """
        parts = [f"--max-num-seqs {self.max_num_seqs}"]
        if self.extra_args:
            parts.append(self.extra_args)
        return " ".join(parts)


def check_serving_fits(
    spec: ModelSpec, sku: str, gpu_memory_utilization: float, quantization: str | None
) -> tuple[bool, str]:
    """Will the weights fit, before a node is allocated.

    Deliberately separate from `azure_ml.check_sku_fits`, which reads
    `vram_gb.{qlora,lora,full}` -- training footprints that include optimizer
    state and activations and say nothing about inference. Serving bf16 needs
    2 bytes per parameter plus room for KV; that is a different sum, and using
    the training one here would have called a 27B a 28 GB model.

    Returns (fits, explanation). A SKU absent from `GPU_SKUS` is not a failure:
    "cannot check" is not the same finding as "does not fit".
    """
    info = GPU_SKUS.get(sku)
    if info is None:
        return True, f"{sku} is not in GPU_SKUS, cannot verify serving footprint"
    if quantization:
        return True, f"{quantization} quantisation requested; bf16 sizing does not apply"
    if spec.params_b is None:
        return True, f"{spec.key} declares no params_b, cannot verify serving footprint"

    weights = spec.params_b * BF16_BYTES_PER_PARAM
    budget = info["vram_gb"] * gpu_memory_utilization
    headroom = budget - weights
    detail = (
        f"{spec.key} at bf16 is ~{weights:.1f} GB; {sku} gives "
        f"{info['vram_gb']} GB and utilization {gpu_memory_utilization} caps vLLM "
        f"at ~{budget:.1f} GB"
    )
    if headroom <= 0:
        return False, (
            f"{detail} -- the weights alone do not fit. Raise the SKU, lower "
            f"gpu_memory_utilization's denominator by using more GPUs, or set "
            f"quantization."
        )
    # vLLM refuses to start when the KV cache would be smaller than one block,
    # and a couple of GB of nominal headroom disappears into CUDA graphs and
    # activations. Warn rather than refuse: the exact floor is engine-specific.
    if headroom < 8:
        return True, f"{detail} -- only {headroom:.1f} GB left for KV cache and graphs"
    return True, f"{detail} -- {headroom:.1f} GB for KV cache, GDN state and graphs"


def build_command(spec: BenchSpec) -> str:
    """The shell line the node runs.

    The two paths are passed as arguments, and they have to come from the
    command string because `${{inputs.*}}` and `${{outputs.*}}` are substituted
    by Azure ML there and nowhere else -- put them in `environment_variables`
    and the node receives the literal placeholder text.

    They are arguments rather than a `VAR=value` command prefix because a
    prefix loses to the image. `docker/Dockerfile.serve:45` bakes
    `ENV MODEL_PATH=/var/azureml-app/azureml-models`, this image is FROM that
    one, and job `sharp_date_dcg59pbtt5` reached vLLM with exactly that path
    (docs/VERIFIED.md §42). An always-set inherited value also makes the
    entrypoint's `:?` guard unfireable, converting "nobody bound the model"
    from an error into a server that loads the wrong thing.
    """
    return (
        "bash /usr/local/bin/bench_entrypoint.sh "
        '"${{inputs.model}}" "${{outputs.bench}}"'
    )


def bench_env(spec: BenchSpec, model: ModelSpec | None) -> dict[str, str]:
    """Container environment: the serving half from the registry, the client half from the spec.

    `serving_env` is reused rather than reimplemented so the server under test
    is configured exactly as a deployment would configure it -- same
    `--mamba-cache-mode`, same `--language-model-only`, same
    `--reasoning-parser`. Measuring a differently-flagged server would make the
    numbers unattributable to the thing we intend to ship.
    """
    env = serving_env(
        model,
        max_model_len=spec.max_model_len,
        gpu_memory_utilization=spec.gpu_memory_utilization,
        quantization=spec.quantization,
        extra_args=spec.vllm_extra_args(),
    )
    # Set in the command instead; see `build_command`. Left here it would be the
    # literal mount default and would win over nothing, but it invites the
    # reader to think this is where the path comes from.
    env.pop("MODEL_PATH", None)
    env.update(
        {
            "BENCH_CONCURRENCY": spec.concurrency,
            "BENCH_REQUESTS_PER_LEVEL": str(spec.requests_per_level),
            "BENCH_MAX_TOKENS": str(spec.max_tokens),
            "BENCH_TTFT_SLO": str(spec.ttft_slo),
            "BENCH_STARTUP_TIMEOUT": str(spec.startup_timeout),
            "PYTHONPATH": "/opt/ffsft/src",
            "HF_HOME": "/mnt/hf",
            "TOKENIZERS_PARALLELISM": "false",
            # Every cache vLLM and torch would open lives under `$HOME/.cache`
            # by default, and `$HOME` on an Azure ML node is inside
            # `AZ_BATCH_NODE_ROOT_DIR` -- which measured 64197 MB on this SKU,
            # not the ~1 TB the SKU advertises (VERIFIED 50). The bench image
            # is 9.2 GB and the downloaded 27B is 54 GB, so that root has
            # ~1.3 GB free by the time vLLM starts. A CUDA-graph capture and an
            # inductor compile of a 64-layer model do not fit in 1.3 GB, and
            # the job would die with no traceback the way
            # `dynamic_ship_yj1dmrfdlp` did. `/mnt` is the node's real NVMe and
            # is not under the batch root -- the merge job already writes its
            # 54 GB HF cache there.
            "VLLM_CACHE_ROOT": "/mnt/vllm-cache",
            "TORCHINDUCTOR_CACHE_DIR": "/mnt/inductor-cache",
            "TRITON_CACHE_DIR": "/mnt/triton-cache",
            "XDG_CACHE_HOME": "/mnt/xdg-cache",
        }
    )
    # The client half of the same decision `serving_env` makes for the server.
    # `--reasoning-parser qwen3` routes a <think> block into `reasoning_content`,
    # so a client that does not ask for the registry's mode measures a thinking
    # model while the training prompts were rendered with thinking off
    # (`train/qlora.py`). In `plum_wall_318nsvlvt6` that cost 40 of 64 requests
    # per level -- see VERIFIED 55. Absent rather than "{}" when the registry
    # declares nothing, because "{}" and "unset" reach the server differently.
    if model is not None and model.chat_template_kwargs:
        env["BENCH_CHAT_TEMPLATE_KWARGS"] = json.dumps(
            model.chat_template_kwargs, ensure_ascii=False
        )
    return env


def ensure_bench_environment(client: MLClient) -> str:
    """Register the prebuilt bench image as an Azure ML environment.

    Same shape and same reasoning as `train.aml_job.ensure_environment`: an
    existing version is reused only after its image is checked, because an
    environment version is immutable and `create_or_update` over a stale one
    silently returns the stored entity. Duplicated rather than shared because
    the third caller (`deploy.endpoint`) registers without an explicit version
    at all and needs fixing first (docs/VERIFIED.md §37.1); converging two of
    three now would leave a helper shaped around the wrong pair.
    """
    from azure.ai.ml.entities import Environment
    from azure.core.exceptions import ResourceNotFoundError

    name, version, image = (
        BENCH_ENVIRONMENT_NAME,
        BENCH_ENVIRONMENT_VERSION,
        BENCH_IMAGE,
    )
    try:
        env = client.environments.get(name, version=version)
    except ResourceNotFoundError:
        env = None

    if env is not None:
        if env.image != image:
            raise RuntimeError(
                f"environment '{name}:{version}' is already registered against "
                f"'{env.image}', not '{image}'. An Azure ML environment version is "
                f"immutable, so this cannot be re-pointed -- build a new tag and "
                f"bump BENCH_IMAGE, which moves the version with it."
            )
        return f"{env.name}:{env.version}"

    created = client.environments.create_or_update(
        Environment(
            name=name,
            version=version,
            description="vLLM serving stack plus the ffsft load generator, in one container",
            image=image,
        )
    )
    return f"{created.name}:{created.version}"


def submit(target: AzureTarget, spec: BenchSpec, wait: bool = False) -> dict:
    """Submit the bench job, refusing first on everything checkable from here."""
    from azure.ai.ml import Input, Output, command
    from azure.ai.ml.constants import AssetTypes

    if not spec.model_asset:
        raise ValueError("BenchSpec.model_asset is empty; there is nothing to serve")
    # Rejects a bare name for the same reason the merge does: "latest" moves,
    # and the asset named here is the one whose latency gets published.
    split_asset_ref(spec.model_asset)

    model = get_model(spec.model_key)
    fits, why = check_serving_fits(
        model, target.compute_sku, spec.gpu_memory_utilization, spec.quantization
    )
    if not fits:
        raise ValueError(f"refusing to submit: {why}")

    client = get_ml_client(target)
    environment = ensure_bench_environment(client)

    node = command(
        # No `code=`: the package is baked into the image. See train.aml_job.
        command=build_command(spec),
        environment=f"azureml:{environment}",
        compute=target.compute_name,
        experiment_name=spec.experiment_name,
        display_name=spec.display_name or f"bench-{spec.model_key}",
        inputs={
            "model": Input(
                type=AssetTypes.CUSTOM_MODEL,
                path=f"azureml:{spec.model_asset}",
                # `download`, not `ro_mount`. vLLM reads safetensors through
                # mmap, and mmap over a blobfuse mount turns a sequential 54 GB
                # read into random page faults against blob storage. Downloading
                # to the node's local NVMe first is one predictable sequential
                # copy.
                #
                # The budget for that copy is far tighter than it looks. An
                # earlier version of this comment claimed the NC24ads_A100_v4
                # has ~1 TB of local disk; the SKU does, but Azure ML does not
                # run jobs on it. `AZ_BATCH_NODE_ROOT_DIR` -- which holds the
                # image, the downloaded inputs and anything written outside a
                # mount -- measured 64197 MB on this SKU. Job
                # dynamic_ship_yj1dmrfdlp died there: image (~9 GB) plus one
                # 54 GB download left 1332 MB, and a second 54 GB artefact had
                # nowhere to go. So exactly one downloaded model fits, and
                # anything a job produces at that scale must go to a
                # `rw_mount` output rather than local disk. See VERIFIED 50.
                mode="download",
            )
        },
        outputs={
            name: Output(type="uri_folder", mode="upload")
            for name in sorted(spec.declared_outputs())
        },
        environment_variables=bench_env(spec, model),
        # vLLM's engine talks to its workers over /dev/shm. The default 64 MB is
        # enough for one process but not for the tensor-parallel path, and the
        # failure is a hang rather than an error.
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
        "image": BENCH_IMAGE,
        "model_asset": spec.model_asset,
        "sizing": why,
    }
    if wait:
        client.jobs.stream(submitted.name)
        info["status"] = client.jobs.get(submitted.name).status
    return info
