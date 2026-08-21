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
import dataclasses
import logging
import os
import re
import time
from typing import TYPE_CHECKING

from .registry import get_serving_registry
from .spec import ServingSpec, Surface

if TYPE_CHECKING:
    from ..models.spec import ModelSpec

log = logging.getLogger("ffsft.deploy.endpoint")

#: Image built by docker/Dockerfile.serve. Bump with the tag, like the trainer's.
#: :3 makes the vLLM launch neutral by default -- architecture flags now come
#: from the ModelSpec via serving_env() instead of being baked into the image.
SERVE_IMAGE = "acrffsftkc.azurecr.io/ffsft-serve:3"

#: Where Azure ML mounts a registered model inside the inference container.
MODEL_MOUNT = "/var/azureml-app/azureml-models"


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

    This is the only honest answer to 'can this SKU be deployed'. Quota says
    yes for A10 v5 and the create call says no; the catalogue lists all sixteen
    GPU SKUs and the create call still says no.

    Free: a refusal returns in about two seconds having created nothing, and an
    acceptance is a `min_instances=0` cluster that allocates no node before it
    is deleted.
    """
    from azure.ai.ml.entities import AmlCompute

    try:
        client.compute.begin_create_or_update(
            AmlCompute(
                name=name, size=sku, min_instances=0, max_instances=1,
                tier=tier, idle_time_before_scale_down=120,
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


def classify_store(account: str, public_access: str, private_endpoints: int) -> StoreProbe:
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

    Anything this function cannot read reports reachable. A probe that cannot
    see is not the same as a resource that is broken, and the expensive mistake
    in this project has consistently been turning the former into the latter.
    """
    if public_access != "Disabled":
        return StoreProbe(account, public_access, private_endpoints, True, "")
    if private_endpoints > 0:
        return StoreProbe(
            account,
            public_access,
            private_endpoints,
            True,
            f"{account}: public access off, reached over {private_endpoints} private endpoint(s)",
        )
    detail = (
        f"no reachable datastore: '{account}' has publicNetworkAccess=Disabled "
        f"and 0 private endpoints, so neither this client nor the Azure ML "
        f"compute node can open a session against it. Job outputs never upload "
        f"(artifacts=0 on every finished run), so there is nothing to register "
        f"as a model -- and every hosted pattern deploys a model asset. "
        f"Fix: attach a private endpoint to the account and put the compute in "
        f"that VNet. Turning public access back on is rejected silently by "
        f"tenant-level enforcement."
    )
    return StoreProbe(account, public_access, private_endpoints, False, detail)


def probe_model_store(target) -> StoreProbe:
    """Read the live public-access posture of the workspace's default datastore.

    Free and read-only: two ARM GETs, no resource is created or touched.
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
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable probe must not block
        log.warning("could not read the datastore posture: %s", exc)
        return classify_store("unknown", "Unknown", 0)


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
    available = read_dedicated_quota(subscription_id, location, spec.quota_family)
    return spec, spec.blocked_reason(available, instances=instances, sku=sku)



def startup_grace_for(params_b: float | None) -> int:
    """Seconds to wait before the readiness probe starts judging the container.

    Startup is dominated by pulling weights from the Hub and loading them onto
    the GPU, both of which scale with parameter count. Roughly 2 GB of bf16
    weights per billion parameters, and a Hub download that sustains on the
    order of 100 MB/s into an Azure ML node, works out near 25 s per billion.
    The fixed 120 s covers image start and CUDA graph capture.

    The reason this is a function rather than the old hardcoded 600: a fixed
    27B-sized grace period means a 0.6B smoke deployment that can never become
    healthy still takes ~45 minutes to be declared failed, and Azure withholds
    the container logs until it is. The upper bound exists for the same reason
    -- a grace period long enough to never fail is not a grace period.
    """
    if params_b is None:
        return 600
    return int(min(1800, max(120, 120 + params_b * 25)))


#: A parameter count written as a size suffix: `-8B`, `-0.6b`, `-70B`.
#: The trailing guard is what stops `-4bit`, `-8bit` and `-7Base` from reading
#: as sizes, and requiring the `B` is what stops the `3.1` in `Llama-3.1-8B`
#: from being mistaken for one.
_SIZE_SUFFIX = re.compile(r"(\d+(?:\.\d+)?)[Bb](?![A-Za-z0-9])")

#: Mixture-of-experts shorthand: `8x7B` is eight 7B experts on disk, not 7B.
_MOE_SUFFIX = re.compile(r"(\d+)\s*[xX]\s*(\d+(?:\.\d+)?)[Bb](?![A-Za-z0-9])")


def params_from_hf_id(hf_id: str | None) -> float | None:
    """Recover a parameter count from a Hugging Face repo id, or None.

    Exists so that a model swapped in by repo id -- the whole point of this
    repo -- still gets a probe sized for it, without a registry entry and
    without a network call. Hub naming is consistent enough to rely on: the
    size is a B-suffixed number, and everything else in the id is a version, a
    quantisation, or a variant tag.

    The largest candidate wins rather than the last one. `Qwen3-30B-A3B` names
    both its total and its active parameters; startup pays for the download, so
    the total is the honest input. Picking the last match would read 3B there
    and under-size the grace period by a factor of ten.

    Returning None is a real answer, not a failure: it is what keeps the probe
    on its conservative default instead of acting on a guess.
    """
    if not hf_id:
        return None
    candidates = [float(a) * float(b) for a, b in _MOE_SUFFIX.findall(hf_id)]
    candidates += [float(m) for m in _SIZE_SUFFIX.findall(hf_id)]
    return max(candidates) if candidates else None


def resolve_params_b(
    *,
    explicit: float | None,
    spec: ModelSpec | None,
    hf_model: str | None,
) -> float | None:
    """Pick the most trustworthy parameter count available.

    Ordered by how much the number was actually looked at: an operator flag
    beats a curated registry entry, which beats a string parsed out of a repo
    id. A registry entry that simply has no size recorded falls through rather
    than blocking the inference behind it.
    """
    if explicit is not None:
        return explicit
    if spec is not None and getattr(spec, "params_b", None) is not None:
        return spec.params_b
    return params_from_hf_id(hf_model)


def serving_env(
    spec: ModelSpec | None,
    *,
    hf_model: str | None = None,
    served_model_name: str = "ffsft",
    max_model_len: int = 8192,
    gpu_memory_utilization: float = 0.9,
    quantization: str | None = None,
    extra_args: str | None = None,
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


def deploy_online(
    endpoint_name: str,
    model_uri: str | None,
    *,
    pattern_key: str = "aml_online_vllm",
    instance_count: int = 1,
    sku: str | None = None,
    served_model_name: str = "ffsft",
    max_model_len: int = 4096,
    gpu_memory_utilization: float = 0.90,
    request_timeout_ms: int = 180_000,
    hf_model: str | None = None,
    model_spec: ModelSpec | None = None,
    params_b: float | None = None,
    quantization: str | None = None,
    extra_args: str = "",
    force: bool = False,
):
    """Create/update a managed online endpoint running vLLM.

    Pass either `model_uri` (a registered Azure ML model, mounted into the
    container) or `hf_model` (a Hugging Face repo id that vLLM downloads at
    startup). The Hub path exists because this workspace's storage account is
    network-isolated: registering a model requires a write path that is not
    always available, whereas the container's outbound HTTPS is. It is also much
    faster to iterate on.

    `request_timeout_ms` defaults far above Azure ML's 5s default: a 27B model
    generating 128 tokens takes tens of seconds, and the default silently turns
    every real request into a 504.
    """
    from azure.ai.ml.entities import (
        Environment,
        ManagedOnlineDeployment,
        ManagedOnlineEndpoint,
        OnlineRequestSettings,
        ProbeSettings,
    )

    from ffsft.azure_ml import AzureTarget, get_ml_client

    target = AzureTarget.from_env()
    spec, blocker = check_pattern(
        pattern_key,
        target.subscription_id,
        target.location,
        sku=sku,
        instances=instance_count,
        from_hub=bool(hf_model),
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
    from .preflight import read_storage_reachability, storage_blocker

    reachability = read_storage_reachability(target)
    storage_issue = storage_blocker(reachability) if reachability else None
    if storage_issue and not force:
        raise RuntimeError(storage_issue)
    if storage_issue:
        log.warning("deploying despite: %s", storage_issue)

    instance_type = sku or spec.default_sku
    client = get_ml_client(target)

    log.info("ensuring endpoint %s", endpoint_name)
    client.online_endpoints.begin_create_or_update(
        ManagedOnlineEndpoint(name=endpoint_name, auth_mode="key")
    ).result()

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
        read_identity_grants,
    )

    acr_id = acr_id_for_image(SERVE_IMAGE, target.subscription_id, target.resource_group)
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
                result.error, result.manual_fix,
            )

        grants = read_identity_grants(target, endpoint_name, acr_id)
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
        existing = client.online_deployments.get(name="blue", endpoint_name=endpoint_name)
        state = (getattr(existing, "provisioning_state", "") or "").lower()
        if state in {"failed", "canceled"}:
            log.warning(
                "deployment 'blue' is in state '%s' and cannot be updated; deleting it",
                state,
            )
            client.online_deployments.begin_delete(
                name="blue", endpoint_name=endpoint_name
            ).result()
    except Exception as exc:  # noqa: BLE001 - absence is the normal first-run case
        log.debug("no existing deployment to clean up: %s", exc)

    env = Environment(
        name="ffsft-serve",
        image=SERVE_IMAGE,
        # vLLM's own OpenAI server, so the container speaks the protocol the
        # load-test client and the eval harness already target.
        inference_config={
            "liveness_route": {"port": 8000, "path": "/health"},
            "readiness_route": {"port": 8000, "path": "/health"},
            "scoring_route": {"port": 8000, "path": "/v1/chat/completions"},
        },
    )

    if not model_uri and not hf_model:
        raise ValueError("pass either model_uri (registered model) or hf_model (Hub id)")

    env_vars = serving_env(
        model_spec,
        hf_model=hf_model,
        served_model_name=served_model_name,
        max_model_len=max_model_len,
        gpu_memory_utilization=gpu_memory_utilization,
        quantization=quantization,
        extra_args=extra_args or None,
    )
    log.info("serving env: %s", {k: v for k, v in env_vars.items() if k != "EXTRA_ARGS"})

    sized_from = resolve_params_b(explicit=params_b, spec=model_spec, hf_model=hf_model)
    grace = startup_grace_for(sized_from)
    log.info(
        "startup grace: %ds from params_b=%s (probe gives up ~%ds after that)",
        grace,
        sized_from,
        10 * 30,
    )

    deployment = ManagedOnlineDeployment(
        name="blue",
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
        # Sized from the model rather than fixed: a large model needs minutes to
        # load, but using that budget for a small one means a container that can
        # never start still takes ~45 minutes to be reported as failed, with the
        # logs withheld until then.
        liveness_probe=ProbeSettings(initial_delay=grace, period=30, failure_threshold=10),
        readiness_probe=ProbeSettings(initial_delay=grace, period=30, failure_threshold=10),
    )

    log.info("creating deployment on %s x%d (this takes 15-30 min)", instance_type, instance_count)
    client.online_deployments.begin_create_or_update(deployment).result()

    endpoint = client.online_endpoints.get(endpoint_name)
    endpoint.traffic = {"blue": 100}
    client.online_endpoints.begin_create_or_update(endpoint).result()

    scoring_uri = client.online_endpoints.get(endpoint_name).scoring_uri
    log.info("endpoint ready: %s", scoring_uri)
    return scoring_uri


def deploy_batch(
    endpoint_name: str,
    model_uri: str,
    *,
    pattern_key: str = "aml_batch",
    compute_name: str | None = None,
    instance_count: int = 1,
    max_concurrency_per_instance: int = 1,
    mini_batch_size: int = 8,
):
    """Create/update a batch endpoint backed by the LowPriority training cluster.

    Reuses the existing cluster deliberately: it already has the system-assigned
    identity and ACR pull rights that this workspace's storage configuration
    requires, and a second cluster would double the quota footprint for nothing.
    """
    from azure.ai.ml.entities import (
        BatchEndpoint,
        ModelBatchDeployment,
        ModelBatchDeploymentSettings,
    )

    from ffsft.azure_ml import AzureTarget, get_ml_client

    target = AzureTarget.from_env()
    spec = get_serving_registry().get(pattern_key)
    if not spec.allows_low_priority:
        raise ValueError(
            f"pattern '{pattern_key}' is an online pattern; use deploy_online instead"
        )

    client = get_ml_client(target)
    compute = compute_name or target.compute_name

    log.info("ensuring batch endpoint %s", endpoint_name)
    client.batch_endpoints.begin_create_or_update(
        BatchEndpoint(name=endpoint_name, description="ffsft offline scoring")
    ).result()

    deployment = ModelBatchDeployment(
        name="default",
        endpoint_name=endpoint_name,
        model=model_uri,
        compute=compute,
        settings=ModelBatchDeploymentSettings(
            instance_count=instance_count,
            max_concurrency_per_instance=max_concurrency_per_instance,
            mini_batch_size=mini_batch_size,
            output_action="append_row",
            output_file_name="predictions.csv",
            retry_settings={"max_retries": 3, "timeout": 3000},
            error_threshold=-1,
            logging_level="info",
        ),
    )
    log.info("creating batch deployment on %s", compute)
    client.batch_deployments.begin_create_or_update(deployment).result()

    endpoint = client.batch_endpoints.get(endpoint_name)
    endpoint.defaults = {"deployment_name": "default"}
    client.batch_endpoints.begin_create_or_update(endpoint).result()
    log.info("batch endpoint ready: %s", endpoint.scoring_uri)
    return endpoint.scoring_uri


def cmd_check(args) -> int:
    """Report, per serving pattern, whether it can be deployed right now."""
    from ffsft.azure_ml import AzureTarget, get_ml_client

    target = AzureTarget.from_env()
    registry = get_serving_registry()
    print(f"subscription {target.subscription_id} / {target.location}\n")

    store = probe_model_store(target)
    if not store.reachable:
        print(f"  datastore  UNREACHABLE  {store.account} "
              f"(publicNetworkAccess={store.public_access}, "
              f"{store.private_endpoints} private endpoints)\n")

    client = get_ml_client(target) if args.probe else None
    width = max(len(s.key) for s in registry)
    for index, spec in enumerate(sorted(registry, key=lambda s: s.key)):
        if spec.surface is Surface.LOCAL:
            print(f"  {spec.key:<{width}}  n/a       (local, no Azure quota involved)")
            continue
        _, blocker = check_pattern(
            spec.key, target.subscription_id, target.location, store=store
        )
        if blocker and spec.can_serve_from_hub and not store.reachable:
            # The datastore is dark, but this server resolves its own weights.
            # Re-ask without the storage constraint before calling it blocked --
            # ffsft-a10 deployed exactly this way while storage stayed dark.
            _, hub_blocker = check_pattern(
                spec.key, target.subscription_id, target.location,
                store=store, from_hub=True,
            )
            if hub_blocker is None:
                print(f"  {spec.key:<{width}}  ok        via --hf-model "
                      "(no model asset, storage not involved)")
                continue
        if blocker:
            summary = blocker if len(blocker) <= 110 else blocker[:107].rstrip() + "..."
            print(f"  {spec.key:<{width}}  BLOCKED   {summary}")
            continue

        if client is not None:
            tier = "LowPriority" if spec.allows_low_priority else "Dedicated"
            # A distinct name per pattern: the delete is asynchronous, so reusing
            # one name races the next create against the previous teardown.
            probe = probe_sku(client, spec.default_sku, tier, name=f"ffsft-probe-{index}")
            if probe.blocker:
                print(f"  {spec.key:<{width}}  BLOCKED   {probe.blocker}")
            else:
                print(f"  {spec.key:<{width}}  ok        {tier} {spec.default_sku} "
                      "(create accepted)")
            continue

        if spec.allows_low_priority:
            print(f"  {spec.key:<{width}}  ok?       LowPriority pool ({spec.default_sku})")
        else:
            cores = read_dedicated_quota(
                target.subscription_id, target.location, spec.quota_family
            )
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
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Deploy a tuned model to an Azure ML endpoint")
    sub = ap.add_subparsers(dest="cmd", required=True)

    check = sub.add_parser("check", help="Show which serving patterns are deployable right now.")
    check.add_argument(
        "--probe", action="store_true",
        help="Ask the control plane for real instead of trusting the quota number. "
             "Free: refusals create nothing, acceptances are min=0 and deleted.",
    )

    online = sub.add_parser(
        "deploy-online", help="Managed online endpoint (needs dedicated quota)."
    )
    online.add_argument("--endpoint", default="ffsft-online")
    online.add_argument(
        "--model-uri", default=None,
        help="A registered Azure ML model, e.g. azureml:qwen3-ko:1. Mutually "
             "exclusive with --hf-model.",
    )
    online.add_argument(
        "--hf-model", default=None,
        help="A Hugging Face repo id, e.g. Qwen/Qwen3.5-0.8B. vLLM downloads the "
             "weights at container start, so this path needs no model asset and "
             "no reachable workspace storage account (see VERIFIED.md section 24).",
    )
    online.add_argument("--sku", default=None)
    online.add_argument("--instance-count", type=int, default=1)
    online.add_argument("--max-model-len", type=int, default=4096)
    online.add_argument("--gpu-memory-utilization", type=float, default=0.90)
    online.add_argument("--force", action="store_true", help="Ignore the quota precheck.")

    batch = sub.add_parser("deploy-batch", help="Batch endpoint on the LowPriority cluster.")
    batch.add_argument("--endpoint", default="ffsft-batch")
    batch.add_argument("--model-uri", required=True)
    batch.add_argument("--compute", default=None)
    batch.add_argument("--instance-count", type=int, default=1)
    batch.add_argument("--mini-batch-size", type=int, default=8)

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s | %(message)s"
    )

    if args.cmd == "check":
        return cmd_check(args)
    if args.cmd == "deploy-online":
        # Exactly one weight source. Neither leaves vLLM with nothing to serve;
        # both is a contradiction the deployment would resolve silently.
        if bool(args.model_uri) == bool(args.hf_model):
            ap.error("deploy-online needs exactly one of --model-uri or --hf-model")
        deploy_online(
            args.endpoint, args.model_uri, hf_model=args.hf_model, sku=args.sku,
            instance_count=args.instance_count, max_model_len=args.max_model_len,
            gpu_memory_utilization=args.gpu_memory_utilization,
            force=args.force,
        )
        return 0
    deploy_batch(
        args.endpoint, args.model_uri, compute_name=args.compute,
        instance_count=args.instance_count, mini_batch_size=args.mini_batch_size,
    )
    return 0


if __name__ == "__main__":
    os.environ.setdefault("AZURE_CORE_ONLY_SHOW_ERRORS", "true")
    raise SystemExit(main())
