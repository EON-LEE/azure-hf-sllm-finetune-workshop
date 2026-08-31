"""Deploy a tuned model to an Azure ML *batch* endpoint.

Split out of `endpoint.py`, which was over the line budget
`test_deploy_module_split.test_the_split_left_endpoint_readable` guards and
whose docstring asks the next person to move code out rather than raise the
number a third time. This is that move: the batch surface is self-contained --
one endpoint guard, one deployment create, one routing repoint -- and nothing
outside `endpoint.main` called it. `endpoint.py` re-exports both public names,
so every existing import keeps resolving.

The reason it is worth reading as its own file is the ARM write ordering. A
batch endpoint carries a routing pointer (`defaults.deployment_name`) that
decides which deployment a scoring job actually runs on, and
`begin_create_or_update` is a PUT that replaces. Every write below is therefore
either gated on a read that proved the resource absent, or made to an entity
read back from Azure. See `ensure_batch_endpoint` and docs/JOURNAL.md S76.
"""

from __future__ import annotations

import logging

from .registry import get_serving_registry

log = logging.getLogger("ffsft.deploy.batch")

#: The one deployment name `deploy_batch` owns. It is hardcoded rather than a
#: parameter, which is exactly why the endpoint's `defaults` pointer must be
#: read before it is moved: an operator whose endpoint defaults to anything else
#: is being repointed, and has to be told which name to put back.
BATCH_DEPLOYMENT_NAME = "default"

#: Description written only onto an endpoint this tool creates from nothing.
#: It is never sent at an endpoint that already exists -- an operator's own
#: description is theirs, and overwriting it was part of the S76 defect.
BATCH_ENDPOINT_DESCRIPTION = "ffsft offline scoring"


def _default_deployment_name(defaults) -> str | None:
    """Read `deployment_name` out of whatever shape `defaults` arrived in.

    The SDK is asymmetric here and the asymmetry is not cosmetic. Writing takes
    a plain dict (`_to_rest_batch_endpoint` builds the REST object from it), but
    reading hands back the REST `BatchEndpointDefaults`::

        BatchEndpoint._from_rest_object(live).defaults
        -> <BatchEndpointDefaults>, not a dict     # azure-ai-ml 1.34.1

    So `endpoint.defaults.get(...)` raises on a value that came from Azure and
    `endpoint.defaults.deployment_name` raises on one this module just set.
    Both shapes reach here, plus `None` for an endpoint with no default yet.
    """
    if defaults is None:
        return None
    if isinstance(defaults, dict):
        return defaults.get("deployment_name") or defaults.get("deploymentName")
    return getattr(defaults, "deployment_name", None)


class BatchDeploymentInUse(RuntimeError):
    """Raised rather than replacing a batch deployment somebody else built.

    A `RuntimeError` and not a log line because the PUT it stands in front of is
    the whole point of the command: swallowing this would mean the run reports
    success while the scoring URI still serves the old model.
    """


def deployment_replacement_blocker(
    existing, *, deployment_name: str, model_uri: str, compute: str
) -> str | None:
    """Why this run must not PUT over the deployment that is already there.

    `None` means the write is safe -- either nothing is there, or what is there
    already runs exactly this model on exactly this cluster, so the PUT is the
    idempotent redeploy the command advertises.

    Pure, so the decision is testable without ARM, and separate from the read so
    that "could not look" cannot arrive here disguised as "found nothing":
    the caller passes `None` only after a `ResourceNotFoundError`, which is the
    one answer that positively establishes absence.

    The cost this exists to stop: `deploy-batch` hardcoded the deployment name
    `"default"` and PUT a freshly built `ModelBatchDeployment` at it with no read
    of `client.batch_deployments` anywhere in the module. Driven against a fake
    that recorded the writes, an operator's `default` deployment went from
    `model='azureml:pricing-prod-model:7' compute='operator-prod-cluster'` to
    this tool's model on the training cluster -- and when the endpoint already
    defaulted to `default`, the S76 endpoint guard correctly skipped the endpoint
    PUT, so the only line about the endpoint was "already defaults to default;
    not writing it". The scoring URI served a different model and nothing said so.
    """
    if existing is None:
        return None
    current_model = str(getattr(existing, "model", "") or "")
    current_compute = str(getattr(existing, "compute", "") or "")
    changes = []
    if current_model and current_model != model_uri:
        changes.append(f"model {current_model} -> {model_uri}")
    if current_compute and current_compute != compute:
        changes.append(f"compute {current_compute} -> {compute}")
    if not changes:
        return None
    return (
        f"batch deployment '{deployment_name}' already exists and this run would "
        f"change what it serves: {'; '.join(changes)}. Nothing about the endpoint "
        "would warn you -- its routing pointer does not move when it already names "
        f"'{deployment_name}'. Re-run with --deployment NAME to deploy alongside it, "
        "or --force to replace it on purpose."
    )


def read_batch_deployment(client, endpoint_name: str, deployment_name: str):
    """The deployment this run is about to write over, or `None` if there is none.

    `ResourceNotFoundError` is the only failure that means absent -- the same
    line `ensure_batch_endpoint` draws one function up, for the same reason: a
    403 on the read turned into "there is nothing there" is "could not look"
    reported as "looked, saw nothing", with an ARM PUT attached.
    """
    from azure.core.exceptions import ResourceNotFoundError

    try:
        return client.batch_deployments.get(deployment_name, endpoint_name)
    except ResourceNotFoundError:
        return None


def ensure_batch_endpoint(
    client, endpoint_name: str, *, description: str = BATCH_ENDPOINT_DESCRIPTION
) -> None:
    """Create the batch endpoint if it is missing; never write it if it exists.

    The batch twin of `endpoint.ensure_endpoint`, and it exists because
    `deploy_batch` was missing the guard `ensure_endpoint` had already been
    given four hundred lines above it in the same file. `begin_create_or_update`
    is a PUT that replaces, and the entity `deploy_batch` sent was built fresh
    at the call site rather than read back from Azure. Measured against
    azure-ai-ml 1.34.1::

        BatchEndpoint(name="ffsft-batch", description="ffsft offline scoring")
            ._to_rest_batch_endpoint(location="koreacentral").as_dict()
        -> {"location": "koreacentral", "tags": {},
            "properties": {"description": "ffsft offline scoring",
                           "authMode": "aadToken", "properties": {}}}

    `tags` is an explicit empty map, `description` is this tool's own string,
    and `defaults` -- the pointer that decides which deployment a scoring job
    runs on -- is absent from the body altogether. Sent at an endpoint an
    operator owns, that request is what their cost-centre tags and their routing
    get replaced with.

    Worse was the ordering: the PUT ran *before* the deployment create, so a
    create that raised -- quota is the ordinary reason -- aborted the run with
    the routing already gone and only the quota error on screen. Creating only
    when absent removes the ordering question instead of answering it.

    `ResourceNotFoundError` is the only failure that means "absent". Anything
    else -- a 403 on the read, a 503 -- is an unanswered question and is left to
    propagate, because turning it into a create is precisely "could not look"
    reported as "looked, saw nothing", with an ARM PUT attached.

    See docs/JOURNAL.md S65 (the online case) and S76 (this one).
    """
    from azure.ai.ml.entities import BatchEndpoint
    from azure.core.exceptions import ResourceNotFoundError

    try:
        existing = client.batch_endpoints.get(endpoint_name)
    except ResourceNotFoundError:
        existing = None

    if existing is None:
        log.info("creating batch endpoint %s", endpoint_name)
        client.batch_endpoints.begin_create_or_update(
            BatchEndpoint(name=endpoint_name, description=description)
        ).result()
        return

    log.info(
        "batch endpoint %s exists; leaving it as-is (default deployment=%s)",
        endpoint_name,
        _default_deployment_name(existing.defaults) or "(unset)",
    )


def deploy_batch(
    endpoint_name: str,
    model_uri: str,
    *,
    pattern_key: str = "aml_batch",
    compute_name: str | None = None,
    deployment_name: str = BATCH_DEPLOYMENT_NAME,
    instance_count: int = 1,
    max_concurrency_per_instance: int = 1,
    mini_batch_size: int = 8,
    force: bool = False,
):
    """Create/update a batch endpoint backed by the LowPriority training cluster.

    Reuses the existing cluster deliberately: it already has the system-assigned
    identity and ACR pull rights that this workspace's storage configuration
    requires, and a second cluster would double the quota footprint for nothing.

    Three ARM writes, in this order and no other: the endpoint is created only
    if absent, the deployment is created, and only then is the endpoint's
    routing pointer moved. A failure at step two therefore leaves the operator's
    endpoint exactly as it was.

    Both of the first two writes are now gated on a read. The endpoint gate is
    S76; the deployment gate is S78, and it exists because the endpoint gate
    moved the defect down one resource rather than removing it -- a fresh
    `ModelBatchDeployment` at a hardcoded name, PUT with nothing ever read.
    `--deployment` names the resource being written, the way `deploy-online`
    already lets an operator do; `force=True` replaces one on purpose.
    """
    from azure.ai.ml.entities import ModelBatchDeployment, ModelBatchDeploymentSettings

    from ffsft.azure_ml import AzureTarget, get_ml_client

    target = AzureTarget.from_env()
    spec = get_serving_registry().get(pattern_key)
    if not spec.allows_low_priority:
        raise ValueError(f"pattern '{pattern_key}' is an online pattern; use deploy_online instead")

    client = get_ml_client(target)
    compute = compute_name or target.compute_name

    log.info("ensuring batch endpoint %s", endpoint_name)
    ensure_batch_endpoint(client, endpoint_name)

    existing = read_batch_deployment(client, endpoint_name, deployment_name)
    blocker = deployment_replacement_blocker(
        existing, deployment_name=deployment_name, model_uri=model_uri, compute=compute
    )
    if blocker and not force:
        raise BatchDeploymentInUse(blocker)
    if blocker:
        log.warning("--force: replacing an existing batch deployment. %s", blocker)

    deployment = ModelBatchDeployment(
        name=deployment_name,
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

    # Read back, then mutate. Never construct a fresh entity here -- see
    # `ensure_batch_endpoint`. Repointing `defaults` IS this command's job, but
    # it happens only after the deployment exists, so a create that raises
    # cannot leave the endpoint routing to nothing.
    endpoint = client.batch_endpoints.get(endpoint_name)
    previous = _default_deployment_name(endpoint.defaults)
    if previous == deployment_name:
        log.info(
            "batch endpoint %s already defaults to %s; not writing it",
            endpoint_name,
            deployment_name,
        )
    else:
        # Named, not silent: the old pointer is the only thing that tells an
        # operator what to put back if this was not the deploy they wanted.
        log.info(
            "repointing batch endpoint %s default deployment: %s -> %s",
            endpoint_name,
            previous or "(unset)",
            deployment_name,
        )
        endpoint.defaults = {"deployment_name": deployment_name}
        client.batch_endpoints.begin_create_or_update(endpoint).result()
        endpoint = client.batch_endpoints.get(endpoint_name)

    log.info("batch endpoint ready: %s", endpoint.scoring_uri)
    return endpoint.scoring_uri
