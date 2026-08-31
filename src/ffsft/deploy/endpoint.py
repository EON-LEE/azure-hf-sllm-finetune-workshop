"""Deploy a tuned model to an Azure ML endpoint.

Two surfaces, and which one you get is not a preference -- it is decided by
quota, which is why `ServingSpec.blocked_reason` is checked before anything is
created rather than after a 20-minute rollout fails.

**Managed online endpoint** (`aml_online_vllm`). Interactive, OpenAI-compatible,
Microsoft owns TLS/auth/autoscale/blue-green. Bills against per-family
*dedicated* GPU quota and cannot use LowPriority, so on a subscription whose
dedicated GPU limits are all 0 it simply cannot be created.

**Batch endpoint** (`aml_batch_vllm`, `aml_batch`). Runs on an ordinary
AmlCompute cluster, therefore inherits `min_instances=0` and LowPriority
pricing: nothing when idle, ~80% off when running. Not interactive, but it is
the pattern that works today and the right one for evaluation and bulk scoring.

    python -m ffsft.deploy.endpoint check
    python -m ffsft.deploy.endpoint deploy-batch --model-uri azureml:qwen-ko:1
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from typing import TYPE_CHECKING

from ..azure_ml import image_tag
from ..logging_setup import quiet_azure_sdk_logs
from .batch import BATCH_DEPLOYMENT_NAME, deploy_batch, ensure_batch_endpoint
from .lifecycle import EXIT_COULD_NOT_LOOK
from .preflight import QUOTA_SCOPE, scope_lines
from .probes import (
    SkuProbe,
    StoreProbe,
    _absence_is_proven,
    _summary,
    check_pattern,
    classify_cluster_error,
    classify_store,
    probe_model_store,
    probe_report,
    probe_sku,
    quota_family_for,
    read_dedicated_quota,
)
from .readiness import (
    AZURE_DEFAULT_FAILURE_THRESHOLD,
    AZURE_MAX_FAILURE_THRESHOLD,
    IN_FLIGHT_QUANTIZATION_FACTOR,
    PROBE_INITIAL_DELAY,
    PROBE_PERIOD,
    params_from_hf_id,
    probe_settings_for,
    resolve_params_b,
    startup_grace_for,
)
from .registry import get_serving_registry
from .spec import Surface

#: Re-exported so that the names this module has always exposed keep resolving
#: from here after the split. `check_pattern` and the probes now live in
#: `probes.py`; the startup-budget arithmetic in `readiness.py`. A test that
#: fakes one of the probes must patch it on the module the caller reaches for
#: -- `probes`, not this one.
__all__ = [
    "AZURE_DEFAULT_FAILURE_THRESHOLD",
    "AZURE_MAX_FAILURE_THRESHOLD",
    "IN_FLIGHT_QUANTIZATION_FACTOR",
    "MODEL_MOUNT",
    "PROBE_INITIAL_DELAY",
    "PROBE_PERIOD",
    "SERVE_ENVIRONMENT_NAME",
    "SERVE_ENVIRONMENT_VERSION",
    "SERVE_IMAGE",
    "SERVE_IMAGE_ENV_VAR",
    "SkuProbe",
    "StoreProbe",
    "check_pattern",
    "classify_cluster_error",
    "classify_store",
    "deploy_batch",
    "deploy_online",
    "egress_for",
    "ensure_batch_endpoint",
    "ensure_endpoint",
    "get_serving_registry",
    "params_from_hf_id",
    "probe_model_store",
    "probe_settings_for",
    "probe_sku",
    "quota_family_for",
    "read_dedicated_quota",
    "resolve_params_b",
    "resolve_serve_image",
    "serve_environment",
    "serving_env",
    "startup_grace_for",
]

if TYPE_CHECKING:
    from azure.ai.ml import MLClient
    from azure.ai.ml.entities import Environment

    from ..models.spec import ModelSpec

log = logging.getLogger("ffsft.deploy.endpoint")

#: Image built by docker/Dockerfile.serve. Bump with the tag, like the trainer's.
#: :3 makes the vLLM launch neutral by default -- architecture flags now come
#: from the ModelSpec via serving_env() instead of being baked into the image.
#: :4 adds the guard in `serve_entrypoint.sh:88`: the registry says
#: qwen3.8-27b is multimodal, which is true of the base on the Hub and false of
#: the merged checkpoint, so `LANGUAGE_MODEL_ONLY=1` was being passed to a
#: text-only model and vLLM died in `load_weights` with "There is no module or
#: parameter named 'language_model' in Qwen3_5Model" (job
#: purple_wolf_g3hhc4q5qj). The bench path moved to :4 when `ffsft-bench:8` was
#: built; this constant did not, so a managed deployment of the very model this
#: project produces would still have hit that error the moment a node was
#: allocated. See docs/JOURNAL.md 51.5.
SERVE_IMAGE = "acrffsftkc.azurecr.io/ffsft-serve:5"

SERVE_ENVIRONMENT_NAME = "ffsft-serve"

#: Derived from the tag rather than typed, for the same reason the trainer and
#: the bench derive theirs. This was the one of the three registrations that
#: passed no version at all, which is how `ffsft-serve` ended up with versions
#: 1 and 2 holding images `:2` and `:3` -- a numbering that tells a reader
#: nothing about which image a version holds.
#:
#: It is the version of `SERVE_IMAGE` and of nothing else. Now that the image
#: is a runtime value (`--image` / `FFSFT_SERVE_IMAGE`), nothing on the deploy
#: path may read this name: an immutable version pinned at import time to an
#: image chosen at call time is the silent-wrong-code failure the derivation
#: exists to prevent -- Azure ML hands back the version already holding the
#: authors' `:5`. `tests/test_serve_image_is_parameterised.py` pins that.
SERVE_ENVIRONMENT_VERSION = image_tag(SERVE_IMAGE)

#: Set once per shell instead of passing `--image` to every deploy.
SERVE_IMAGE_ENV_VAR = "FFSFT_SERVE_IMAGE"


def resolve_serve_image(image: str | None = None) -> str:
    """Which serving image to deploy: `--image`, then the env var, then the default.

    `SERVE_IMAGE` is the authors' private registry, which nobody else can pull
    from: lab4 has a participant `az acr build` their own `ffsft-serve:1` and
    there was then no supported way to deploy it -- the end of the
    managed-online track for anyone outside this subscription.

    One resolver per deploy, because the image settles three things that must
    agree -- environment version, environment image, and the registry the
    endpoint identity gets AcrPull on -- and two of those disagree silently.
    """
    explicit = (image or "").strip()
    if explicit:
        return explicit
    return os.environ.get(SERVE_IMAGE_ENV_VAR, "").strip() or SERVE_IMAGE


#: Where Azure ML mounts a registered model inside the inference container.
MODEL_MOUNT = "/var/azureml-app/azureml-models"


def serve_environment(client: MLClient, image: str | None = None) -> Environment:
    """The serving environment, checked against what is already registered.

    Third of the three registrations in this repo, and until now the odd one:
    `train.aml_job.ensure_environment` and `serve.bench_job.ensure_bench_environment`
    both pin an explicit version derived from the image tag and refuse to
    proceed when the registered version holds a different image. This one
    passed no version at all, so Azure ML auto-numbered it -- which is how
    `ffsft-serve` versions 1 and 2 came to hold images `:2` and `:3`.

    The check matters more here than there. An environment version is
    immutable, so `create_or_update` over a stale version returns the stored
    entity rather than correcting it; a deployment then serves an image the
    caller never named, and the way that surfaces is a container that never
    becomes healthy after Azure has spent an hour trying to allocate a node for
    it. Noticing here is free.

    The version is derived from the `image` passed in, never from
    `SERVE_ENVIRONMENT_VERSION`: a participant's `ffsft-serve:1` registered
    under version `5` collides with the version holding the authors' `:5`.

    Returns the entity rather than registering it: it is passed inline to the
    `ManagedOnlineDeployment`, which is what performs the registration.
    """
    from azure.ai.ml.entities import Environment
    from azure.core.exceptions import ResourceNotFoundError

    name = SERVE_ENVIRONMENT_NAME
    image = resolve_serve_image(image)
    version = image_tag(image)
    try:
        registered = client.environments.get(name, version=version)
    except ResourceNotFoundError:
        registered = None

    if registered is not None and registered.image != image:
        raise RuntimeError(
            f"environment '{name}:{version}' is already registered against "
            f"'{registered.image}', not '{image}'. An Azure ML environment version "
            f"is immutable, so this cannot be re-pointed -- build a new tag and "
            f"pass it with --image (or {SERVE_IMAGE_ENV_VAR}), which moves the "
            f"version with it."
        )

    return Environment(
        name=name,
        version=version,
        image=image,
        # vLLM's own OpenAI server, so the container speaks the protocol the
        # load-test client and the eval harness already target.
        inference_config={
            "liveness_route": {"port": 8000, "path": "/health"},
            "readiness_route": {"port": 8000, "path": "/health"},
            "scoring_route": {"port": 8000, "path": "/v1/chat/completions"},
        },
    )


def serving_env(
    spec: ModelSpec | None,
    *,
    hf_model: str | None = None,
    served_model_name: str = "ffsft",
    max_model_len: int = 8192,
    gpu_memory_utilization: float = 0.9,
    quantization: str | None = None,
    extra_args: str | None = None,
    model_blob_uri: str | None = None,
) -> dict[str, str]:
    """Build the container environment for one model.

    Every architecture-dependent key is emitted unconditionally, including when
    its value is neutral. That is deliberate: the image carries ENV defaults,
    and a deployment that simply omits a key silently inherits whatever the
    image was built with. A smoke deployment of the dense, text-only
    Qwen3-0.6B was launched with `--language-model-only` and
    `--mamba-cache-mode align` exactly that way, because those are what
    Qwen3.8-27B needs and the image had them baked in.

    Passing `spec=None` means "model not in the registry" and yields a plain
    vLLM launch rather than Qwen3.8-shaped guesswork.
    """
    env = {
        # MODEL_PATH doubles as a repo id when nothing is mounted: the
        # entrypoint looks for config.json under the mount and falls back to
        # treating the value as a Hub reference, so one variable covers both
        # deployment styles.
        "MODEL_PATH": hf_model or MODEL_MOUNT,
        # Emitted even when empty, for the same reason as the architecture keys
        # above: the image carries `MODEL_BLOB_URI=""` as a default, and a
        # deployment that omits the key inherits whatever the image was built
        # with. An image rebuilt with a URI baked in would otherwise silently
        # pull that checkpoint into every deployment that never asked for one.
        "MODEL_BLOB_URI": model_blob_uri or "",
        "SERVED_MODEL_NAME": served_model_name,
        "MAX_MODEL_LEN": str(max_model_len),
        "GPU_MEMORY_UTILIZATION": str(gpu_memory_utilization),
        "MAMBA_CACHE_MODE": (spec.mamba_cache_mode or "") if spec else "",
        "LANGUAGE_MODEL_ONLY": "1" if (spec and spec.multimodal) else "0",
        "REASONING_PARSER": (spec.reasoning_parser or "") if spec else "",
    }
    if quantization:
        env["QUANTIZATION"] = quantization
    if extra_args:
        env["EXTRA_ARGS"] = extra_args
    return env


def egress_for(explicit: str | None, reachability: object | None) -> str | None:
    """Whether to set `egressPublicNetworkAccess` on the deployment at all.

    On a workspace secured by a managed VNet: never. That workspace governs its
    deployments' egress itself, and Azure rejects the per-deployment setting
    outright -- a 400 at submit time, before a node is allocated:

        The EgressPublicNetworkAccess under online deployment is no longer
        supported when your workspace is secured with managed virtual network.
        Please avoid setting EgressPublicNetworkAccess on the deployment in
        this case.

    Asking for ``"disabled"`` earns a second clause in the same 400 -- private
    networking requires a Premium ACR, and the registry holding the serve image
    is Basic.

    Reading the setting back is not evidence anyone set it: ARM reports
    `Enabled` for a deployment that never specified it, which is what the live
    `blue` deployment shows despite being created without the argument. Only an
    explicit value reaches the validator, so leaving it unset is both the
    working configuration and the measured one.

    The isolation mode is what decides this, so it is read off the preflight the
    caller already ran rather than inferred from where the weights live. A
    `None` reachability means the workspace could not be read, and the safe
    answer there is the same one: leave it unset.

    See docs/JOURNAL.md S64.
    """
    if getattr(reachability, "workspace_is_isolated", False):
        # The managed VNet already places the container on the network that
        # reaches workspace storage, so there is nothing here to choose --
        # only a 400 to earn.
        return None
    # `None` leaves the SDK default alone. Returning "enabled" here would rewrite
    # the setting on every existing deployment that never asked about networking.
    return explicit


def ensure_endpoint(client, endpoint_name: str) -> None:
    """Create the endpoint if it is missing; never touch it if it exists.

    `begin_create_or_update` is a PUT that replaces, and a `ManagedOnlineEndpoint`
    built fresh here -- never read back from Azure -- serialises with
    `properties.traffic == {}`::

        ManagedOnlineEndpoint(name=..., auth_mode="key")
            ._to_rest_online_endpoint(location=...).properties.traffic
        -> {}

    That is an explicit empty map, not an omitted field ARM might merge, so
    PUTting it at a live endpoint sends every deployment to 0% traffic. Nothing
    reports it: the deployments stay `Succeeded`, the endpoint stays `Succeeded`,
    and only the scoring URI goes dead. This endpoint served a full 100-request
    load test on that URI and later read back `traffic: {}`, with deploys the
    only writes in between.

    It also made `deploy_online`'s `--traffic 0` branch untrue -- that branch
    promises not to take the endpoint down, then logs the `{}` this step created
    as if it had found it that way.

    See docs/JOURNAL.md S65.
    """
    from azure.ai.ml.entities import ManagedOnlineEndpoint
    from azure.core.exceptions import ResourceNotFoundError

    try:
        existing = client.online_endpoints.get(endpoint_name)
    except ResourceNotFoundError:
        existing = None

    if existing is None:
        log.info("creating endpoint %s", endpoint_name)
        client.online_endpoints.begin_create_or_update(
            ManagedOnlineEndpoint(name=endpoint_name, auth_mode="key")
        ).result()
        return

    log.info(
        "endpoint %s exists; leaving it as-is (traffic=%s)",
        endpoint_name,
        existing.traffic or {},
    )
    if (existing.auth_mode or "").lower() != "key":
        # Worth saying rather than silently rewriting: changing auth mode rotates
        # how every existing client authenticates.
        log.warning(
            "endpoint %s has auth_mode=%s, not 'key'; leaving it alone",
            endpoint_name,
            existing.auth_mode,
        )


def deploy_online(
    endpoint_name: str,
    model_uri: str | None,
    *,
    deployment_name: str = "blue",
    traffic_percent: int = 100,
    pattern_key: str = "aml_online_vllm",
    instance_count: int = 1,
    sku: str | None = None,
    image: str | None = None,
    served_model_name: str = "ffsft",
    max_model_len: int = 4096,
    gpu_memory_utilization: float = 0.90,
    request_timeout_ms: int = 180_000,
    hf_model: str | None = None,
    model_blob_uri: str | None = None,
    model_spec: ModelSpec | None = None,
    params_b: float | None = None,
    quantization: str | None = None,
    extra_args: str = "",
    egress_public_network_access: str | None = None,
    force: bool = False,
):
    """Create/update a managed online endpoint running vLLM.

    Pass exactly one weight source:

    * `model_uri` -- a registered Azure ML model, mounted into the container.
    * `hf_model` -- a Hugging Face repo id that vLLM downloads at startup. This
      path exists because this workspace's storage account is network-isolated:
      registering a model requires a write path that is not always available,
      whereas the container's outbound HTTPS is. It is also much faster to
      iterate on.
    * `model_blob_uri` -- an https blob URL the container downloads with its own
      managed identity before vLLM starts. This is the only route for a
      *fine-tuned* checkpoint on a tenant where model registration is closed:
      `MCAPSGovDeployPolicies` disables shared-key auth account-wide, and Azure
      ML's Model Registry enumerates blobs with an account key, so no model
      asset can be created at all. Managed deployments accept only a registered
      asset id -- datastore paths and job-output references are rejected at
      parse time -- so the container fetches the weights itself instead. See
      `docker/fetch_model.py`.

    `image` names the serving container (`--image`, then `FFSFT_SERVE_IMAGE`,
    then `SERVE_IMAGE`); it drives the environment version *and* the AcrPull
    grant, so a participant's own registry works end to end.

    `request_timeout_ms` defaults far above Azure ML's 5s default: a 27B model
    generating 128 tokens takes tens of seconds, and the default silently turns
    every real request into a 504.
    """
    from azure.ai.ml.entities import (
        ManagedOnlineDeployment,
        OnlineRequestSettings,
        ProbeSettings,
    )

    from ffsft.azure_ml import AzureTarget, get_ml_client

    target = AzureTarget.from_env()

    # Resolved once, then reused for the environment *and* the AcrPull grant.
    # `image_tag` also refuses an untagged reference here, where it is free,
    # rather than after Azure spends 15-30 minutes allocating a node.
    serve_image = resolve_serve_image(image)
    log.info("serving image %s (environment version %s)", serve_image, image_tag(serve_image))

    spec, blocker = check_pattern(
        pattern_key,
        target.subscription_id,
        target.location,
        sku=sku,
        instances=instance_count,
        # Named for the Hub because that was the first case, but what the
        # flag actually declares is "the server resolves its own weights, so no
        # model asset is involved". A blob fetch is that same case.
        from_hub=bool(hf_model or model_blob_uri),
    )
    if blocker and not force:
        raise RuntimeError(blocker)
    if blocker:
        log.warning("deploying despite: %s", blocker)

    # Quota is not the only thing that makes a rollout impossible before it
    # starts. Two endpoints were lost to a permissions gap that produces no
    # logs at all: the endpoint's *own* managed identity had no AcrPull on the
    # image registry, so the container never started and Azure could only
    # report a generic error an hour later.
    #
    # That check used to run *here*, and it was useless: the endpoint -- and so
    # the identity -- does not exist yet at this point, the ARM read 404s, and
    # the preflight returns "nothing known" precisely on a first deployment,
    # which is the one case where the grant is guaranteed to be missing. It now
    # runs after the endpoint is created, below. Creating an endpoint is free;
    # only a deployment allocates a GPU.
    from .preflight import (
        RestrictedSkuError,
        online_endpoint_blocker,
        read_sku_availability,
        read_storage_reachability,
        sku_advisory,
        storage_blocker,
    )

    reachability = read_storage_reachability(target)
    storage_issue = storage_blocker(reachability) if reachability else None
    if storage_issue and not force:
        raise RuntimeError(storage_issue)
    if storage_issue:
        log.warning("deploying despite: %s", storage_issue)

    instance_type = sku or spec.default_sku

    # Enforced here, advisory everywhere else -- see `online_endpoint_blocker`.
    # A managed online endpoint cannot use LowPriority, so the Spot pool that
    # makes this field inconclusive for an AmlCompute cluster does not exist for
    # this caller. Two rollouts were spent today confirming that, each sitting at
    # `percentComplete: 0.0` for the better part of two hours after this same
    # advisory had been logged and treated as one signal among several.
    availability = read_sku_availability(target.subscription_id, target.location, instance_type)
    blocker = online_endpoint_blocker(availability)
    if blocker and not force:
        raise RestrictedSkuError(blocker)
    if blocker:
        log.warning("force=True, proceeding despite: %s", blocker)
    else:
        note = sku_advisory(availability)
        if note:
            log.warning("%s", note)

    client = get_ml_client(target)

    ensure_endpoint(client, endpoint_name)

    # Now the identity exists and can be checked -- and fixed. Azure wires up
    # AcrPull automatically only for the workspace-linked registry; this
    # workspace has none, so a customer registry needs an explicit assignment
    # that nothing else creates. Measured cost of skipping it: ~10 minutes of
    # provisioning followed by
    #   (BadArgument) Endpoint identity does not have pull permission
    from .identity import (
        acr_id_for_image,
        ensure_acr_pull,
        identity_blocker,
        identity_unread_note,
        read_identity_grants,
    )

    acr_id = acr_id_for_image(serve_image, target.subscription_id, target.resource_group)
    if acr_id:
        principal = getattr(
            getattr(client.online_endpoints.get(name=endpoint_name), "identity", None),
            "principal_id",
            None,
        )
        result = ensure_acr_pull(acr_id, principal)
        if result.granted:
            # RBAC is eventually consistent; the image pull happens minutes from
            # now, but a fresh assignment can still be invisible to the data
            # plane for a short while.
            log.info("granted AcrPull to the endpoint identity; waiting 60s to propagate")
            time.sleep(60)
        elif result.error:
            log.warning(
                "could not grant AcrPull automatically (%s).\nRun this yourself:\n%s",
                result.error,
                result.manual_fix,
            )

        grants = read_identity_grants(target, endpoint_name, acr_id)
        # The blocker only ever speaks about grants that were MEASURED absent.
        # A roleAssignments listing that stopped early is neither a finding nor
        # a clean bill of health, so it travels on its own channel rather than
        # being folded into either -- S79.
        unread = identity_unread_note(grants) if grants else None
        if unread:
            log.warning("%s", unread)
        identity_issue = identity_blocker(grants) if grants else None
        if identity_issue and not force:
            raise RuntimeError(identity_issue)
        if identity_issue:
            log.warning("deploying despite: %s", identity_issue)

    # Azure refuses to update a deployment whose first provisioning failed:
    #   "Specified deployment [blue] failed during initial provisioning and is
    #    in an unrecoverable state. Delete and re-create."
    # Retrying after a quota rejection therefore fails for a *second*, unrelated
    # reason unless the corpse is cleared first.
    try:
        existing = client.online_deployments.get(name=deployment_name, endpoint_name=endpoint_name)
        state = (getattr(existing, "provisioning_state", "") or "").lower()
        if state in {"failed", "canceled"}:
            log.warning(
                "deployment '%s' is in state '%s' and cannot be updated; deleting it",
                deployment_name,
                state,
            )
            try:
                client.online_deployments.begin_delete(
                    name=deployment_name, endpoint_name=endpoint_name
                ).result()
            except Exception as exc:  # noqa: BLE001 - a refused DELETE is not a failed GET
                # This call sat inside the GET's `try`, so a refused delete was
                # logged as "could not check whether deployment 'blue' already
                # exists" -- the one thing that had just been checked. It
                # exists, it is in `Failed`, and Azure will refuse this deploy
                # with the unrecoverable-state message above.
                log.warning(
                    "deployment '%s' of endpoint '%s' is in '%s' and could NOT be "
                    "deleted (%s). Azure refuses to update a deployment in this state, "
                    "so this deploy is expected to fail on it; delete it by hand.",
                    deployment_name,
                    endpoint_name,
                    state,
                    exc,
                )
    except Exception as exc:  # noqa: BLE001 - the classification is in _absence_is_proven
        if _absence_is_proven(exc):
            log.debug("no existing deployment to clean up: %s", exc)
        else:
            # A 403 is not a 404, and this branch used to spell both `debug`.
            # A refused GET is the one case where "nothing is there, so create
            # freely" is unverified, and it was the case the operator could not
            # see: the deploy went out clean and Azure refused it 20 minutes
            # later with the unrecoverable-state message above.
            log.warning(
                "could not check whether deployment '%s' of endpoint '%s' already "
                "exists (%s). That is not evidence that it does not. Deploying "
                "anyway; if a previous attempt left one in a failed state, Azure "
                "will refuse this update and it has to be deleted by hand.",
                deployment_name,
                endpoint_name,
                exc,
            )

    env = serve_environment(client, serve_image)

    if not model_uri and not hf_model and not model_blob_uri:
        raise ValueError(
            "pass one of model_uri (registered model), hf_model (Hub id) "
            "or model_blob_uri (blob URL fetched by the container)"
        )

    if model_spec is None and hf_model:
        # `--hf-model` carries a Hub repo id, and the CLI has no way to also name
        # a registry key, so `model_spec` arrived as None and every measured
        # serving flag went out empty -- `--mamba-cache-mode` among them, for a
        # checkpoint whose 48 of 64 layers are Gated DeltaNet. Recover the spec
        # from the id itself; an id the registry has never seen stays None.
        from ffsft.models.registry import get_registry

        model_spec = get_registry().by_hf_id(hf_model)
        if model_spec is not None:
            log.info("matched --hf-model %s to registry key %s", hf_model, model_spec.key)

    env_vars = serving_env(
        model_spec,
        hf_model=hf_model,
        served_model_name=served_model_name,
        max_model_len=max_model_len,
        gpu_memory_utilization=gpu_memory_utilization,
        quantization=quantization,
        extra_args=extra_args or None,
        model_blob_uri=model_blob_uri,
    )
    log.info("serving env: %s", {k: v for k, v in env_vars.items() if k != "EXTRA_ARGS"})

    sized_from = resolve_params_b(explicit=params_b, spec=model_spec, hf_model=hf_model)
    grace = startup_grace_for(sized_from, quantization=quantization)
    probe = probe_settings_for(grace)
    log.info(
        "startup budget %ds from params_b=%s quantization=%s: "
        "first probe at %ds, then %d retries every %ds",
        grace,
        sized_from,
        quantization,
        probe["initial_delay"],
        probe["failure_threshold"],
        probe["period"],
    )

    egress = egress_for(egress_public_network_access, reachability)
    if egress_public_network_access is not None and egress is None:
        log.warning(
            "ignoring --egress-public-network-access=%s: this workspace is secured "
            "with a managed VNet, which governs deployment egress itself and makes "
            "Azure reject the setting on the deployment",
            egress_public_network_access,
        )

    deployment = ManagedOnlineDeployment(
        name=deployment_name,
        endpoint_name=endpoint_name,
        model=model_uri,
        environment=env,
        instance_type=instance_type,
        instance_count=instance_count,
        environment_variables=env_vars,
        request_settings=OnlineRequestSettings(
            request_timeout_ms=request_timeout_ms,
            max_concurrent_requests_per_instance=64,
        ),
        # The budget lives in `failure_threshold`, deliberately -- see
        # `probe_settings_for`. Still sized from the model rather than fixed,
        # because a budget large enough for a 27B model means a container that
        # can never start holds the deployment in `Creating` for the better part
        # of an hour, with the logs withheld until it is terminal.
        liveness_probe=ProbeSettings(**probe),
        readiness_probe=ProbeSettings(**probe),
        egress_public_network_access=egress,
    )

    log.info("creating deployment on %s x%d (this takes 15-30 min)", instance_type, instance_count)
    client.online_deployments.begin_create_or_update(deployment).result()

    if traffic_percent <= 0:
        # Blue/green: bring the new deployment up beside the serving one and
        # leave the traffic map alone, so a bad rollout cannot take the endpoint
        # down. Shift traffic in a second call once it answers correctly.
        current = client.online_endpoints.get(endpoint_name).traffic
        log.info(
            "deployment %s created with no traffic; endpoint traffic stays %s",
            deployment_name,
            current,
        )
    else:
        endpoint = client.online_endpoints.get(endpoint_name)
        traffic = dict(endpoint.traffic or {})
        others = [n for n in traffic if n != deployment_name]
        remainder = 100 - traffic_percent
        if remainder and len(others) != 1:
            raise ValueError(
                f"--traffic {traffic_percent} leaves {remainder}% to assign, but "
                f"the endpoint has {len(others)} other deployment(s) {others}; "
                f"Azure requires the map to sum to 100. Use --traffic 100, or "
                f"split against exactly one other deployment."
            )
        traffic = dict.fromkeys(traffic, 0)
        traffic[deployment_name] = traffic_percent
        if remainder:
            traffic[others[0]] = remainder
        endpoint.traffic = traffic
        client.online_endpoints.begin_create_or_update(endpoint).result()
        log.info("traffic set to %s", traffic)

    scoring_uri = client.online_endpoints.get(endpoint_name).scoring_uri
    log.info("endpoint ready: %s", scoring_uri)
    return scoring_uri


def _store_posture_unread(store) -> bool:
    """True when `probe_model_store` never managed to read the account.

    It swallows its own errors and returns `classify_store("unknown",
    "Unknown", 0)`, on purpose: a probe that cannot see must not become a
    broken resource. But "Unknown" is not "Disabled", so that sentinel comes
    back `reachable=True` -- `check` then printed no datastore line at all and
    rated every pattern `ok?` over a workspace it had read nothing from. A
    posture that WAS read is "Enabled" or "Disabled".
    """
    return getattr(store, "public_access", None) == "Unknown"


def cmd_check(args) -> int:
    """Report, per serving pattern, whether it can be deployed right now."""
    from ffsft.azure_ml import AzureTarget, get_ml_client

    target = AzureTarget.from_env()
    registry = get_serving_registry()
    # The header `ffsft-lifecycle status` prints, from the same helper. This was
    # `subscription <id> / <location>`, which named the one value get_ml_client
    # never sends and omitted the resource group and workspace that are most of
    # what the lines below query -- under a lab that says to trust it. See
    # `preflight.scope_lines`.
    print("\n".join(scope_lines(target, QUOTA_SCOPE)) + "\n")

    # Reads that came back with no answer at all. A blocker is an answer and is
    # deliberately NOT collected here: `check` exiting non-zero because a
    # pattern is legitimately out of quota would make the exit code useless for
    # the thing it is being fixed for.
    blind: list[str] = []

    store = probe_model_store(target)
    if _store_posture_unread(store):
        blind.append("the workspace storage account's network posture")
        print(
            "  datastore  UNKNOWN      could not read the posture of the workspace "
            "storage account\n"
        )
    elif not store.reachable:
        print(
            f"  datastore  UNREACHABLE  {store.account} "
            f"(publicNetworkAccess={store.public_access}, "
            f"{store.private_endpoints} private endpoints)\n"
        )
    if store.key_auth_refused is None:
        # The third state, and the one this report used to render as a clean
        # pass: allowSharedKeyAccess=false was measured, the datastore listing
        # that decides the other half was not, so the S57.8 credential blocker
        # was never evaluated at all. `probes._key_based_datastores` used to
        # return `[]` for that, which reads as measured-empty -- the unread
        # workspace printed no datastore line and exited 0, cleaner than the
        # broken one it may well be. It answers `None` now (S78.2 extended that
        # to a truncated listing as well), which is what makes this branch
        # reachable at all. Deliberately its own `if`: a posture that went
        # unread leaves this False, so the two never both fire.
        blind.append("whether any workspace datastore still authenticates with an account key")
        print(
            f"  datastore  UNKNOWN      {store.account}: allowSharedKeyAccess=false and "
            "the datastore list could not be read\n"
        )

    client = get_ml_client(target) if args.probe else None
    width = max(len(s.key) for s in registry)
    for index, spec in enumerate(sorted(registry, key=lambda s: s.key)):
        if spec.surface is Surface.LOCAL:
            print(f"  {spec.key:<{width}}  n/a       (local, no Azure quota involved)")
            continue
        try:
            _, blocker = check_pattern(
                spec.key, target.subscription_id, target.location, store=store
            )
        except Exception as exc:  # noqa: BLE001 - a failed read is a row, not a traceback
            # `check_pattern` reads dedicated quota for every non-LowPriority
            # pattern, so a 403 on Microsoft.Quota ended the report in a
            # traceback partway down the table, leaving the rows already printed
            # to read as the whole answer.
            blind.append(f"whether {spec.key} can deploy ({_summary(str(exc))})")
            print(f"  {spec.key:<{width}}  UNKNOWN   the read failed: {_summary(str(exc))}")
            continue
        if blocker and spec.can_serve_from_hub and not store.reachable:
            # The datastore is dark, but this server resolves its own weights.
            # Re-ask without the storage constraint before calling it blocked --
            # ffsft-a10 deployed exactly this way while storage stayed dark.
            try:
                _, hub_blocker = check_pattern(
                    spec.key,
                    target.subscription_id,
                    target.location,
                    store=store,
                    from_hub=True,
                )
            except Exception as exc:  # noqa: BLE001 - same reason as the first ask
                blind.append(f"whether {spec.key} can deploy from the Hub ({_summary(str(exc))})")
                hub_blocker = blocker
            if hub_blocker is None:
                print(
                    f"  {spec.key:<{width}}  ok        via --hf-model "
                    "(no model asset, storage not involved)"
                )
                continue
        if blocker:
            print(f"  {spec.key:<{width}}  BLOCKED   {_summary(blocker)}")
            continue

        if client is not None:
            tier = "LowPriority" if spec.allows_low_priority else "Dedicated"
            # A distinct name per pattern: the delete is asynchronous, so reusing
            # one name races the next create against the previous teardown.
            probe = probe_sku(client, spec.default_sku, tier, name=f"ffsft-probe-{index}")
            # A probe has three outcomes and this printed two words for them.
            # `probe_report` owns the split, and hands back the subject to add
            # here when the probe ended without asking anything -- rendering an
            # unasked question as BLOCKED, rc=0, is JOURNAL §75.
            rows, unread = probe_report(probe, spec.key, width)
            print("\n".join(rows))
            if unread:
                blind.append(unread)
            continue

        if spec.allows_low_priority:
            print(f"  {spec.key:<{width}}  ok?       LowPriority pool ({spec.default_sku})")
        else:
            try:
                cores = read_dedicated_quota(
                    target.subscription_id, target.location, spec.quota_family
                )
            except Exception as exc:  # noqa: BLE001 - a failed read is a row, not a traceback
                blind.append(
                    f"dedicated {spec.quota_family} quota in {target.location} "
                    f"({_summary(str(exc))})"
                )
                print(
                    f"  {spec.key:<{width}}  UNKNOWN   could not read dedicated "
                    f"{spec.quota_family} quota"
                )
                continue
            print(
                f"  {spec.key:<{width}}  ok?       dedicated {spec.quota_family}="
                f"{cores} cores ({spec.default_sku})"
            )
    if client is None:
        print(
            "\n  ok? means quota only, and quota is necessary but not sufficient: "
            "a family can report free cores and still refuse every create call.\n"
            "  Re-run with --probe to ask the control plane itself (free -- a "
            "refusal creates nothing, an acceptance is deleted).\n"
            "  Note that AmlCompute and managed online endpoints do not share a "
            "SKU catalogue: A10 v5 is refused for clusters here and accepted for\n"
            "  online endpoints (measured 2026-08-21, ffsft-a10 on "
            "Standard_NV12ads_A10_v5)."
        )
    if blind:
        print("\nCOULD NOT LOOK: these reads returned no answer, so nothing above")
        print("covers them -- an unread row is neither ok nor blocked:")
        for subject in blind:
            print(f"  - {subject}")
        print("fix the errors above and re-run before trusting this report.")
        # Round 4 made the prose honest and left this `return 0`, so
        # `ffsft-deploy check && echo ok` still printed ok over a workspace whose
        # datastore probe and quota reads had all failed. `lifecycle.cmd_status`
        # settled this split already; its constant is reused rather than a second
        # scheme invented, and 2 there is the usage refusal, not this.
        return EXIT_COULD_NOT_LOOK
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Deploy a tuned model to an Azure ML endpoint")
    sub = ap.add_subparsers(dest="cmd", required=True)

    check = sub.add_parser("check", help="Show which serving patterns are deployable right now.")
    check.add_argument(
        "--probe",
        action="store_true",
        help="Ask the control plane for real instead of trusting the quota number. "
        "Free: refusals create nothing, acceptances are min=0 and deleted.",
    )

    online = sub.add_parser(
        "deploy-online", help="Managed online endpoint (needs dedicated quota)."
    )
    online.add_argument("--endpoint", default="ffsft-online")
    online.add_argument(
        "--model-uri",
        default=None,
        help="A registered Azure ML model, e.g. azureml:qwen3-ko:1. Mutually "
        "exclusive with --hf-model.",
    )
    online.add_argument(
        "--hf-model",
        default=None,
        help="A Hugging Face repo id, e.g. Qwen/Qwen3.5-0.8B. vLLM downloads the "
        "weights at container start, so this path needs no model asset and "
        "no reachable workspace storage account (see JOURNAL.md section 24).",
    )
    online.add_argument(
        "--model-blob-uri",
        default=None,
        help="An https blob URL (…/container/prefix/) holding a checkpoint. The "
        "container downloads it with the endpoint's managed identity before "
        "vLLM starts. Use this for a fine-tuned model on a tenant where "
        "model registration is blocked by policy. Pair with --model-key so "
        "the architecture flags are not left empty.",
    )
    online.add_argument(
        "--model-key",
        default=None,
        help="Registry key (e.g. qwen3.8-27b) naming the architecture being "
        "served. --hf-model infers this from the repo id; --model-blob-uri "
        "cannot, and a missing spec means --mamba-cache-mode goes out empty "
        "-- which for Qwen3.8 is not a default but a crash.",
    )
    online.add_argument(
        "--image",
        default=None,
        help="Serving container image, e.g. myacr.azurecr.io/ffsft-serve:1. The "
        "default sits in a private registry you cannot pull from: build your own "
        f"with `az acr build` (lab 4), then pass it here or export "
        f"{SERVE_IMAGE_ENV_VAR}. The tag becomes the immutable Azure ML "
        "environment version, so a rebuild needs a new tag.",
    )
    online.add_argument(
        "--deployment",
        default="blue",
        help="Deployment name under the endpoint. Use a second name (e.g. green) "
        "with --traffic 0 to bring a new version up beside the serving one.",
    )
    online.add_argument(
        "--traffic",
        type=int,
        default=100,
        help="Percent of endpoint traffic to send to this deployment once it is "
        "healthy. 0 leaves the endpoint's traffic map untouched.",
    )
    online.add_argument("--sku", default=None)
    online.add_argument("--instance-count", type=int, default=1)
    online.add_argument("--max-model-len", type=int, default=4096)
    online.add_argument("--gpu-memory-utilization", type=float, default=0.90)
    online.add_argument(
        "--egress-public-network-access",
        choices=["enabled", "disabled"],
        default=None,
        help=(
            "Which network the container's outbound traffic uses. Defaults to "
            "'disabled' with --model-blob-uri, because the blob account is "
            "private-endpoint-only and 'enabled' resolves it to a public IP that "
            "refuses the request. 'disabled' joins the workspace managed VNet; "
            "with an AllowInternetOutbound VNet a public ACR still pulls fine."
        ),
    )
    online.add_argument("--force", action="store_true", help="Ignore the quota precheck.")

    shift = sub.add_parser(
        "shift",
        help="Point the endpoint URL at one deployment (blue/green cutover).",
    )
    shift.add_argument("--endpoint", required=True)
    shift.add_argument(
        "--to",
        required=True,
        help="Deployment name to send 100%% of traffic to. Every sibling goes to 0.",
    )
    shift.add_argument(
        "--allow-unfinished",
        action="store_true",
        help="Shift even if the deployment is not Succeeded. For a rollback to a "
        "deployment mid-update; never for a first cutover.",
    )

    batch = sub.add_parser("deploy-batch", help="Batch endpoint on the LowPriority cluster.")
    batch.add_argument("--endpoint", default="ffsft-batch")
    batch.add_argument("--model-uri", required=True)
    batch.add_argument("--compute", default=None)
    # The name the PUT lands on. It was hardcoded, so an operator whose batch
    # endpoint already had a `default` deployment had no way to ask for another
    # one and no way to see which resource was about to be replaced.
    batch.add_argument("--deployment", default=BATCH_DEPLOYMENT_NAME)
    batch.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing batch deployment that runs a different model or cluster.",
    )
    batch.add_argument("--instance-count", type=int, default=1)
    batch.add_argument("--mini-batch-size", type=int, default=8)

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s | %(message)s"
    )
    quiet_azure_sdk_logs()

    if args.cmd == "check":
        return cmd_check(args)
    if args.cmd == "shift":
        from ..azure_ml import AzureTarget, get_ml_client
        from .traffic import shift_traffic

        client = get_ml_client(AzureTarget.from_env())
        after = shift_traffic(
            client,
            args.endpoint,
            args.to,
            require_succeeded=not args.allow_unfinished,
        )
        print(f"{args.endpoint} now routes to {args.to}: {after}")
        return 0
    if args.cmd == "deploy-online":
        # Exactly one weight source. None leaves vLLM with nothing to serve;
        # more than one is a contradiction the deployment would resolve silently.
        sources = [bool(args.model_uri), bool(args.hf_model), bool(args.model_blob_uri)]
        if sum(sources) != 1:
            ap.error(
                "deploy-online needs exactly one of --model-uri, --hf-model or --model-blob-uri"
            )
        # A blob URI carries no architecture information -- it is just a path --
        # so nothing can infer the spec the way `--hf-model` infers it from the
        # repo id. Refusing here beats a deployment that comes up without
        # `--mamba-cache-mode align` and dies inside vLLM twenty minutes later
        # with a NotImplementedError, which is what an empty spec produces on
        # every Qwen3.5/3.8 checkpoint.
        if args.model_blob_uri and not args.model_key:
            ap.error("--model-blob-uri requires --model-key (e.g. --model-key qwen3.8-27b)")
        spec = None
        if args.model_key:
            from ffsft.models.registry import get_model

            spec = get_model(args.model_key)
        deploy_online(
            args.endpoint,
            args.model_uri,
            hf_model=args.hf_model,
            sku=args.sku,
            image=args.image,
            model_blob_uri=args.model_blob_uri,
            model_spec=spec,
            deployment_name=args.deployment,
            traffic_percent=args.traffic,
            instance_count=args.instance_count,
            max_model_len=args.max_model_len,
            gpu_memory_utilization=args.gpu_memory_utilization,
            egress_public_network_access=args.egress_public_network_access,
            force=args.force,
        )
        return 0
    deploy_batch(
        args.endpoint,
        args.model_uri,
        compute_name=args.compute,
        deployment_name=args.deployment,
        instance_count=args.instance_count,
        mini_batch_size=args.mini_batch_size,
        force=args.force,
    )
    return 0


if __name__ == "__main__":
    os.environ.setdefault("AZURE_CORE_ONLY_SHOW_ERRORS", "true")
    raise SystemExit(main())
