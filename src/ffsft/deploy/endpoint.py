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
import math
import os
import re
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING

from ..azure_ml import image_tag
from .registry import get_serving_registry
from .spec import ServingSpec, Surface

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
SERVE_ENVIRONMENT_VERSION = image_tag(SERVE_IMAGE)

#: Where Azure ML mounts a registered model inside the inference container.
MODEL_MOUNT = "/var/azureml-app/azureml-models"


def serve_environment(client: MLClient) -> Environment:
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

    Returns the entity rather than registering it: it is passed inline to the
    `ManagedOnlineDeployment`, which is what performs the registration.
    """
    from azure.ai.ml.entities import Environment
    from azure.core.exceptions import ResourceNotFoundError

    name, version, image = (
        SERVE_ENVIRONMENT_NAME,
        SERVE_ENVIRONMENT_VERSION,
        SERVE_IMAGE,
    )
    try:
        registered = client.environments.get(name, version=version)
    except ResourceNotFoundError:
        registered = None

    if registered is not None and registered.image != image:
        raise RuntimeError(
            f"environment '{name}:{version}' is already registered against "
            f"'{registered.image}', not '{image}'. An Azure ML environment version "
            f"is immutable, so this cannot be re-pointed -- build a new tag and "
            f"bump SERVE_IMAGE, which moves the version with it."
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

    Scope, stated first because this function was read as answering a broader
    question than it does: this creates an **AmlCompute cluster**, so it answers
    "can a training job run on this SKU". It says nothing about a managed online
    endpoint, which is a different resource type on a different control plane.

    Reading it as a deployment probe inverts its answer. In koreacentral all six
    A10 v5 SKUs are MIR-only -- their `supportedComputeTypes` lists MIR and not
    AmlCompute -- so this call refuses precisely the SKUs a managed endpoint
    accepts. JOURNAL 43 concluded "every GPU SKU is NotAvailableForSubscription"
    from exactly that inversion; JOURNAL 51 retracts it, having created an
    endpoint in 69 seconds. For the deployment question, attempt a
    `ManagedOnlineDeployment` -- nothing else is evidence.

    Within its own scope it is the honest answer, and that part still holds:
    quota says yes for A10 v5 and the create call says no; the catalogue lists
    all sixteen GPU SKUs and the create call still says no.

    Free: a refusal returns in about two seconds having created nothing, and an
    acceptance is a `min_instances=0` cluster that allocates no node before it
    is deleted.
    """
    from azure.ai.ml.entities import AmlCompute

    try:
        client.compute.begin_create_or_update(
            AmlCompute(
                name=name,
                size=sku,
                min_instances=0,
                max_instances=1,
                tier=tier,
                idle_time_before_scale_down=120,
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
    key_auth_refused: bool = False
    key_based_datastores: tuple[str, ...] = ()


def classify_store(
    account: str,
    public_access: str,
    private_endpoints: int,
    *,
    allow_shared_key: bool | None = None,
    key_based_datastores: Sequence[str] = (),
) -> StoreProbe:
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

    Network reachability is necessary and *not* sufficient. A datastore also
    names how to authenticate, and that is a separate axis this check was blind
    to until polandcentral (S57.8): `mlw-ffsft-plc` sat behind two working
    private endpoints -- reachable by the rule above, and this function said so
    -- while every write still failed, because all four of its datastores were
    created with `credentialsType: AccountKey` against a storage account with
    `allowSharedKeyAccess: false`. The account refuses the key the datastore
    insists on presenting, so job log upload, artifact upload, output mounts and
    client-side `jobs.download()` all return `KeyBasedAuthenticationNotPermitted`
    -- the *same* zero-artifact symptom as an unreachable account, from a cause
    no amount of private endpoints or RBAC can fix. Two workspaces created the
    same way disagreed on this: koreacentral came up `None`, polandcentral came
    up `AccountKey`, so it cannot be assumed from the deployment path either.

    Anything this function cannot read reports reachable. A probe that cannot
    see is not the same as a resource that is broken, and the expensive mistake
    in this project has consistently been turning the former into the latter.
    That is why `allow_shared_key=None` (unread) never fails the check: only a
    measured `False` alongside a measured `AccountKey` datastore does.
    """
    key_based = tuple(key_based_datastores)

    if public_access != "Disabled":
        net_ok, net_detail = True, ""
    elif private_endpoints > 0:
        net_ok, net_detail = (
            True,
            (f"{account}: public access off, reached over {private_endpoints} private endpoint(s)"),
        )
    else:
        net_ok, net_detail = (
            False,
            (
                f"no reachable datastore: '{account}' has publicNetworkAccess=Disabled "
                f"and 0 private endpoints, so neither this client nor the Azure ML "
                f"compute node can open a session against it. Job outputs never upload "
                f"(artifacts=0 on every finished run), so there is nothing to register "
                f"as a model -- and every hosted pattern deploys a model asset. "
                f"Fix: attach a private endpoint to the account and put the compute in "
                f"that VNet. Turning public access back on is rejected silently by "
                f"tenant-level enforcement."
            ),
        )

    if allow_shared_key is False and key_based:
        # Reported even when the network posture passes, because it is
        # orthogonal to it: the key is refused on the public endpoint and over a
        # private link alike, so a green network answer says nothing about this.
        detail = (
            f"datastore credential mismatch: '{account}' has "
            f"allowSharedKeyAccess=false, but datastore(s) {', '.join(key_based)} "
            f"authenticate with credentialsType=AccountKey. Every write fails "
            f"with KeyBasedAuthenticationNotPermitted -- job logs, artifacts, "
            f"output mounts and jobs.download() alike -- so runs finish with "
            f"artifacts=0 and there is nothing to register as a model. Private "
            f"endpoints and role assignments do not fix this. Fix: PUT each "
            f"datastore with credentials.credentialsType='None' (identity-based) "
            f"and grant the workspace MSI, the cluster identity and yourself "
            f"Storage Blob Data Contributor on the account. Keep isDefault=true "
            f"on the workspace default datastore or the PUT is rejected."
        )
        if not net_ok:
            # Both broken at once (measured on `mlw-ffsft-jpe`). Reporting only
            # the first sends the caller through a fix-verify-fix round trip for
            # a blocker that was already visible here.
            detail += f" A second, independent blocker is also present -- {net_detail}"
        return StoreProbe(account, public_access, private_endpoints, False, detail, True, key_based)

    return StoreProbe(
        account, public_access, private_endpoints, net_ok, net_detail, False, key_based
    )


def _key_based_datastores(root: str, workspace: str, head: dict) -> list[str]:
    """Names of datastores that authenticate with an account key.

    Read separately from the account so an unreadable datastore list degrades to
    "no key-based datastores found" rather than to a false blocker -- the same
    reason `probe_model_store` reports reachable when it cannot see.
    """
    import requests

    try:
        page = requests.get(
            f"{root}/Microsoft.MachineLearningServices/workspaces/"
            f"{workspace}/datastores?api-version=2024-10-01",
            headers=head,
            timeout=60,
        ).json()
        return sorted(
            d["name"]
            for d in (page.get("value") or [])
            if ((d.get("properties") or {}).get("credentials") or {}).get("credentialsType")
            == "AccountKey"
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable list must not block
        log.warning("could not read datastore credentials: %s", exc)
        return []


def probe_model_store(target) -> StoreProbe:
    """Read the live posture of the workspace's default datastore.

    Free and read-only: three ARM GETs, no resource is created or touched. Two
    independent things can make the datastore unusable -- the account being
    unreachable, and the datastore presenting a credential the account refuses
    -- so both are read here and both are handed to `classify_store`.
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
            allow_shared_key=sa.get("allowSharedKeyAccess"),
            key_based_datastores=_key_based_datastores(root, target.workspace_name, head),
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable probe must not block
        log.warning("could not read the datastore posture: %s", exc)
        return classify_store("unknown", "Unknown", 0)


def quota_family_for(sku: str | None) -> str | None:
    """Dedicated quota family `sku` bills against, or None if unknown.

    Unknown returns None rather than a guess so the caller falls back to the
    pattern's declared family -- the same reason `required_dedicated_cores`
    raises instead of assuming a core count.
    """
    if not sku:
        return None
    from ffsft.azure_ml import GPU_SKUS

    entry = GPU_SKUS.get(sku)
    return entry.get("family") if entry else None


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
    # A `--sku` override can cross quota families. The pattern names the family
    # of its *default* SKU, but Azure bills the family the *chosen* SKU belongs
    # to, so reading `spec.quota_family` here measures a pool the deployment
    # never touches: an A100 SKU was refused in a region with 48 A100 cores
    # granted because the A10 pool it would never use read 0.
    family = quota_family_for(sku or spec.default_sku) or spec.quota_family
    available = read_dedicated_quota(subscription_id, location, family)
    return spec, spec.blocked_reason(available, instances=instances, sku=sku, quota_family=family)


#: Seconds before the first probe fires. Small and fixed on purpose.
#:
#: This constant used to be the whole startup budget, and that was the wrong
#: knob -- see JOURNAL §38. A probe that fails costs nothing; Azure simply
#: tries again `period` seconds later. So every second spent here is a second
#: the deployment cannot go healthy in *even when the container is already
#: serving*, while `failure_threshold` buys the same patience for free in the
#: success case. Azure's own defaults have this shape (`initial_delay 10`,
#: `failure_threshold 30`) and the inverted version here was local invention.
#:
#: 120 s covers image start and the entrypoint reaching the point where it binds
#: a port. Probing before that yields connection refusals, which are noise.
PROBE_INITIAL_DELAY = 120

#: Seconds between probes. Azure's default is 10; 15 halves the request volume
#: against a loading container while still noticing readiness within a quarter
#: of a minute of it becoming true.
PROBE_PERIOD = 15

#: Azure's own documented default for `failure_threshold`, used here as a floor.
#: A vLLM container holding tens of gigabytes of weights is strictly harder to
#: start than the generic deployment that default was chosen for, so being *less*
#: patient than the platform default is never the right answer. The value that
#: shipped before this comment existed was 10.
AZURE_DEFAULT_FAILURE_THRESHOLD = 30

#: Hard server-side ceiling on `failure_threshold`, discovered by hitting it.
#:
#: The YAML schema reference documents a minimum of 1 and a default of 30 for
#: this field and states no maximum at all, so the first version of
#: `probe_settings_for` put the whole budget here and sent 125. Azure rejected
#: the deployment in 61 seconds:
#:
#:     LivenessProbe.FailureThreshold: Invalid value provided for Failure
#:     Threshold for Probe: <125>. The value should be less than 120.
#:
#: "less than 120" means 119 is the largest accepted value. Cheap failure --
#: a 400 before any node was allocated -- but only because it is validated at
#: request time; the same mistake in a field validated later would have cost
#: another rollout. See JOURNAL §38.6.
AZURE_MAX_FAILURE_THRESHOLD = 119


#: How much longer a container takes to start when vLLM quantises on the way to
#: the card instead of loading weights that are already in their served format.
#:
#: It reads the whole bf16 checkpoint and converts every tensor to NF4 during
#: load, so the work is proportional to the *unquantised* size while the benefit
#: only shows up afterwards. On one A10 that is compute-bound, which is why it
#: does not appear anywhere in the per-billion download estimate. It also repeats
#: on every container start -- a restart or a scale-out pays it again.
IN_FLIGHT_QUANTIZATION_FACTOR = 2.5


def startup_grace_for(params_b: float | None, *, quantization: str | None = None) -> int:
    """Total seconds a container may take to become healthy.

    A budget, not a delay -- `probe_settings_for` decides how it is spent.
    Startup is dominated by moving weights onto the GPU, which scales with
    parameter count: roughly 2 GB of bf16 weights per billion parameters, and a
    download sustaining on the order of 100 MB/s into an Azure ML node, works out
    near 25 s per billion. The fixed 120 s covers image start and CUDA graph
    capture.

    The cap is 3600 rather than the 1800 it was, because in-flight quantisation
    is not download-bound and so is not covered by the per-billion estimate: vLLM
    reads the full bf16 checkpoint and quantises to NF4 on the way to the card,
    and on a single A10 that is compute, not bandwidth. A cap still exists,
    because a budget large enough never to be exceeded can never report a
    failure -- and Azure withholds the container logs until the deployment
    reaches a terminal state, so "never fails" also means "never readable".
    """
    factor = IN_FLIGHT_QUANTIZATION_FACTOR if quantization else 1.0
    if params_b is None:
        return int(min(3600, 600 * factor))
    return int(min(3600, max(120, (120 + params_b * 25) * factor)))


def probe_settings_for(grace_seconds: int) -> dict[str, int]:
    """Split a startup budget into the three fields Azure actually accepts.

    The budget goes into `failure_threshold` for as long as it fits, because
    that is the spelling that lets the deployment go healthy the moment the
    container does, instead of holding it in `Creating` for a fixed term sized
    for the worst case.

    It stops fitting at `AZURE_MAX_FAILURE_THRESHOLD`, which a 27B model with
    in-flight quantisation exceeds. Past that point the remainder goes into
    `period` instead. The cost of a longer period is bounded and small: it is
    how late readiness can be *noticed* after it becomes true, so stretching 15 s
    to 16 s to buy 33 minutes of patience is a second of latency for half an hour
    of budget. Widening `initial_delay` would buy the same patience by making
    every start slower, which is the trade this function exists to refuse.
    """
    remaining = max(0, grace_seconds - PROBE_INITIAL_DELAY)
    period = PROBE_PERIOD
    retries = math.ceil(remaining / period)
    if retries > AZURE_MAX_FAILURE_THRESHOLD:
        period = math.ceil(remaining / AZURE_MAX_FAILURE_THRESHOLD)
        retries = math.ceil(remaining / period)
    return {
        "initial_delay": PROBE_INITIAL_DELAY,
        "period": period,
        "failure_threshold": min(
            AZURE_MAX_FAILURE_THRESHOLD,
            max(AZURE_DEFAULT_FAILURE_THRESHOLD, retries),
        ),
    }


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
                result.error,
                result.manual_fix,
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
        existing = client.online_deployments.get(name=deployment_name, endpoint_name=endpoint_name)
        state = (getattr(existing, "provisioning_state", "") or "").lower()
        if state in {"failed", "canceled"}:
            log.warning(
                "deployment '%s' is in state '%s' and cannot be updated; deleting it",
                deployment_name,
                state,
            )
            client.online_deployments.begin_delete(
                name=deployment_name, endpoint_name=endpoint_name
            ).result()
    except Exception as exc:  # noqa: BLE001 - absence is the normal first-run case
        log.debug("no existing deployment to clean up: %s", exc)

    env = serve_environment(client)

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
        raise ValueError(f"pattern '{pattern_key}' is an online pattern; use deploy_online instead")

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
        print(
            f"  datastore  UNREACHABLE  {store.account} "
            f"(publicNetworkAccess={store.public_access}, "
            f"{store.private_endpoints} private endpoints)\n"
        )

    client = get_ml_client(target) if args.probe else None
    width = max(len(s.key) for s in registry)
    for index, spec in enumerate(sorted(registry, key=lambda s: s.key)):
        if spec.surface is Surface.LOCAL:
            print(f"  {spec.key:<{width}}  n/a       (local, no Azure quota involved)")
            continue
        _, blocker = check_pattern(spec.key, target.subscription_id, target.location, store=store)
        if blocker and spec.can_serve_from_hub and not store.reachable:
            # The datastore is dark, but this server resolves its own weights.
            # Re-ask without the storage constraint before calling it blocked --
            # ffsft-a10 deployed exactly this way while storage stayed dark.
            _, hub_blocker = check_pattern(
                spec.key,
                target.subscription_id,
                target.location,
                store=store,
                from_hub=True,
            )
            if hub_blocker is None:
                print(
                    f"  {spec.key:<{width}}  ok        via --hf-model "
                    "(no model asset, storage not involved)"
                )
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
                print(
                    f"  {spec.key:<{width}}  ok        {tier} {spec.default_sku} (create accepted)"
                )
            continue

        if spec.allows_low_priority:
            print(f"  {spec.key:<{width}}  ok?       LowPriority pool ({spec.default_sku})")
        else:
            cores = read_dedicated_quota(target.subscription_id, target.location, spec.quota_family)
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
        instance_count=args.instance_count,
        mini_batch_size=args.mini_batch_size,
    )
    return 0


if __name__ == "__main__":
    os.environ.setdefault("AZURE_CORE_ONLY_SHOW_ERRORS", "true")
    raise SystemExit(main())
