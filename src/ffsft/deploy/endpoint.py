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

from .registry import get_serving_registry
from .spec import ServingSpec, Surface

log = logging.getLogger("ffsft.deploy.endpoint")

#: Image built by docker/Dockerfile.serve. Bump with the tag, like the trainer's.
#: :2 makes the Qwen3.5/3.8-only flags (--mamba-cache-mode, --language-model-only)
#: opt-out, so the image can serve any model in configs/models.yaml.
SERVE_IMAGE = "acrffsftkc.azurecr.io/ffsft-serve:2"


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


def check_pattern(
    pattern_key: str,
    subscription_id: str,
    location: str,
    *,
    sku: str | None = None,
    instances: int = 1,
) -> tuple[ServingSpec, str | None]:
    """Return the spec plus a human-readable blocker, or None if it can deploy."""
    spec = get_serving_registry().get(pattern_key)
    if spec.allows_low_priority or not spec.quota_family:
        return spec, None
    available = read_dedicated_quota(subscription_id, location, spec.quota_family)
    return spec, spec.blocked_reason(available, instances=instances, sku=sku)


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
    )
    if blocker and not force:
        raise RuntimeError(blocker)
    if blocker:
        log.warning("deploying despite: %s", blocker)

    instance_type = sku or spec.default_sku
    client = get_ml_client(target)

    log.info("ensuring endpoint %s", endpoint_name)
    client.online_endpoints.begin_create_or_update(
        ManagedOnlineEndpoint(name=endpoint_name, auth_mode="key")
    ).result()

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

    # MODEL_PATH doubles as a repo id when nothing is mounted: the entrypoint
    # looks for config.json under the mount and falls back to treating the value
    # as a Hub reference, so one variable covers both deployment styles.
    env_vars = {
        "MODEL_PATH": hf_model or "/var/azureml-app/azureml-models",
        "SERVED_MODEL_NAME": served_model_name,
        "MAX_MODEL_LEN": str(max_model_len),
        "GPU_MEMORY_UTILIZATION": str(gpu_memory_utilization),
    }
    if quantization:
        env_vars["QUANTIZATION"] = quantization
    if extra_args:
        env_vars["EXTRA_ARGS"] = extra_args

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
        # A 27B model takes minutes to load. Without a generous startup budget
        # the probe kills the container before vLLM finishes warming up.
        liveness_probe=ProbeSettings(initial_delay=600, period=30, failure_threshold=10),
        readiness_probe=ProbeSettings(initial_delay=600, period=30, failure_threshold=30),
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
    from ffsft.azure_ml import AzureTarget

    target = AzureTarget.from_env()
    registry = get_serving_registry()
    print(f"subscription {target.subscription_id} / {target.location}\n")

    width = max(len(s.key) for s in registry)
    for spec in sorted(registry, key=lambda s: s.key):
        if spec.surface is Surface.LOCAL:
            print(f"  {spec.key:<{width}}  n/a       (local, no Azure quota involved)")
            continue
        _, blocker = check_pattern(spec.key, target.subscription_id, target.location)
        if blocker:
            print(f"  {spec.key:<{width}}  BLOCKED   {blocker.split('. ')[1]}")
        elif spec.allows_low_priority:
            print(f"  {spec.key:<{width}}  ok        LowPriority pool ({spec.default_sku})")
        else:
            cores = read_dedicated_quota(
                target.subscription_id, target.location, spec.quota_family
            )
            print(
                f"  {spec.key:<{width}}  ok        dedicated {spec.quota_family}="
                f"{cores} cores ({spec.default_sku})"
            )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Deploy a tuned model to an Azure ML endpoint")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="Show which serving patterns are deployable right now.")

    online = sub.add_parser(
        "deploy-online", help="Managed online endpoint (needs dedicated quota)."
    )
    online.add_argument("--endpoint", default="ffsft-online")
    online.add_argument("--model-uri", required=True, help="e.g. azureml:qwen3-ko:1")
    online.add_argument("--sku", default=None)
    online.add_argument("--instance-count", type=int, default=1)
    online.add_argument("--max-model-len", type=int, default=4096)
    online.add_argument("--force", action="store_true", help="Ignore the quota precheck.")

    batch = sub.add_parser("deploy-batch", help="Batch endpoint on the LowPriority cluster.")
    batch.add_argument("--endpoint", default="ffsft-batch")
    batch.add_argument("--model-uri", required=True)
    batch.add_argument("--compute", default=None)
    batch.add_argument("--instance-count", type=int, default=1)
    batch.add_argument("--mini-batch-size", type=int, default=8)

    args = ap.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s | %(message)s"
    )

    if args.cmd == "check":
        return cmd_check(args)
    if args.cmd == "deploy-online":
        deploy_online(
            args.endpoint, args.model_uri, sku=args.sku,
            instance_count=args.instance_count, max_model_len=args.max_model_len,
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
