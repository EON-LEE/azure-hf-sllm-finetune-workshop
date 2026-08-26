"""Serving abstraction: every way this asset can host a tuned model is a `ServingSpec`.

Same idea as `ffsft.models.spec` -- deployment code asks the registry for a
pattern by key and reads capabilities off it, so moving a model from a batch
endpoint to a managed online endpoint to AKS is a config change.

The field that actually decides whether a deployment succeeds is
`allows_low_priority`. Azure ML keeps *two* separate GPU quota pools and they are
not interchangeable:

* AmlCompute clusters can draw on a single pooled ``TotalLowPriorityCores``
  allowance (300 on this subscription), which is why training works here today.
* Managed online endpoints bill against per-family *dedicated* quota and reject
  LowPriority outright. Every modern GPU family on this subscription has a
  dedicated limit of 0.

So a pattern with ``allows_low_priority: false`` is a pattern that needs a quota
increase before it can be deployed, and `ServingSpec.blocked_reason` says so
instead of letting a deployment fail 20 minutes in.
"""

from __future__ import annotations

import math
import sys
from enum import Enum

from pydantic import BaseModel, Field, model_validator

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    # See ffsft.models.spec for why the ACPT base image pins us to Python 3.10.
    class StrEnum(str, Enum):  # type: ignore[no-redef]
        """Backport of :class:`enum.StrEnum` for Python 3.10."""

        __str__ = str.__str__
        __format__ = str.__format__


class Surface(StrEnum):
    """Who owns the hosting layer."""

    #: Azure ML managed online endpoint. Microsoft owns LB/TLS/auth/autoscale.
    AML_ONLINE_ENDPOINT = "aml_online_endpoint"

    #: Azure ML batch endpoint, backed by an ordinary AmlCompute cluster.
    AML_BATCH_ENDPOINT = "aml_batch_endpoint"

    #: AKS or Container Apps. We own the cluster.
    KUBERNETES = "kubernetes"

    #: A GPU box we already have a shell on. Development only.
    LOCAL = "local"


class Engine(StrEnum):
    """The inference runtime inside the container."""

    #: vLLM's OpenAI-compatible HTTP server. Continuous batching, paged attention.
    VLLM = "vllm"

    #: vLLM's in-process `LLM.generate` API. Same kernels, no HTTP server.
    VLLM_OFFLINE = "vllm_offline"

    #: Plain `transformers.generate`. Slow, but reads a 4-bit PEFT adapter as-is.
    TRANSFORMERS = "transformers"


class AdapterMode(StrEnum):
    """How a LoRA adapter reaches the engine."""

    #: Folded into the base weights, saved as an ordinary checkpoint.
    MERGED = "merged"

    #: Loaded at runtime beside a shared base (vLLM --enable-lora).
    RUNTIME_ADAPTER = "runtime_adapter"


#: A managed online endpoint keeps headroom to bring up a new version before
#: retiring the old one, so Azure charges 20% more instances than the deployment
#: asks for, rounded up to a whole instance:
#:
#:     ceil(ONLINE_ENDPOINT_UPGRADE_RESERVATION * instances) * cores_per_instance
#:
#: At one instance the round-up makes this exactly double, which is how a 36-core
#: SKU with 36 cores granted failed with "quota requested is 72". Every
#: observation this repo had was at one instance, so a flat x2 fitted them all
#: and was still wrong from two instances up: two pay for three, not four.
ONLINE_ENDPOINT_UPGRADE_RESERVATION = 1.2

#: Families Azure exempts from the reservation above -- the "Skip 20% Reservation"
#: column of the supported-SKU list for managed online endpoints. Charging these
#: double asks for twice the quota Azure does, which turns a grant that fits into
#: one that appears not to. The exemption is granted per family, not per SKU.
UPGRADE_RESERVATION_EXEMPT_FAMILIES = frozenset(
    {
        "standardncadsa100v4family",
        "standardncadsh100v5family",
        "standardndv5h100family",
    }
)


def required_dedicated_cores(sku: str, *, instances: int = 1) -> int:
    """Dedicated cores a managed online deployment of `sku` will ask Azure for.

    Raises KeyError for an unknown SKU rather than assuming a core count --
    guessing here reproduces exactly the failed rollout this guards against.
    """
    from ffsft.azure_ml import GPU_SKUS

    entry = GPU_SKUS[sku]
    cores = entry["cores"]
    if entry["family"].lower() in UPGRADE_RESERVATION_EXEMPT_FAMILIES:
        return cores * instances
    return math.ceil(ONLINE_ENDPOINT_UPGRADE_RESERVATION * instances) * cores


class ServingSpec(BaseModel):
    """A single swappable way to host a model."""

    key: str = Field(description="Short stable alias, e.g. 'aml_batch_vllm'.")
    display_name: str

    surface: Surface
    engine: Engine

    #: Whether the endpoint speaks the OpenAI chat-completions wire protocol.
    #: The load-test client and the eval harness both require this.
    openai_compatible: bool = False
    streaming: bool = False

    #: False means "managed online endpoint rules apply": dedicated quota only.
    allows_low_priority: bool = True

    #: Azure quota bucket this pattern consumes. `TotalLowPriorityCores` is the
    #: pooled AmlCompute allowance; anything else is a per-family dedicated pool.
    quota_family: str | None = None
    default_sku: str | None = None

    #: Whether the pattern can drop to zero instances when idle. Managed online
    #: endpoints cannot, which dominates their cost profile.
    scale_to_zero: bool = True

    description: str = ""
    caveats: str = ""

    @model_validator(mode="after")
    def _check_capabilities(self) -> ServingSpec:
        if self.streaming and not self.openai_compatible:
            raise ValueError(
                f"serving pattern '{self.key}': streaming requires openai_compatible"
            )
        if self.surface is Surface.AML_ONLINE_ENDPOINT and self.allows_low_priority:
            raise ValueError(
                f"serving pattern '{self.key}': AML managed online endpoints bill "
                f"against dedicated quota and reject LowPriority; set "
                f"allows_low_priority: false"
            )
        if self.surface is not Surface.LOCAL and not self.default_sku:
            raise ValueError(f"serving pattern '{self.key}': needs a default_sku")
        return self

    @property
    def is_interactive(self) -> bool:
        """Can a client hold open a request and get a per-request latency?"""
        return self.surface in {Surface.AML_ONLINE_ENDPOINT, Surface.KUBERNETES, Surface.LOCAL}

    @property
    def requires_model_asset(self) -> bool:
        """Does deploying this pattern mean registering a model in Azure ML?

        Every hosted surface does. An Azure ML deployment -- online or batch --
        names a model asset as its input, and a model asset is a path in a
        datastore. Only the local pattern escapes it, because it reads weights
        straight off the Hub onto the machine running the container.

        This matters because it is a *second*, independent way for a pattern to
        be undeployable, and it is invisible to every quota API. A subscription
        can hold plenty of LowPriority quota and still be unable to create a
        model asset, if no datastore is reachable to hold one.
        """
        return self.surface is not Surface.LOCAL

    @property
    def can_serve_from_hub(self) -> bool:
        """Can this pattern take a Hugging Face repo id instead of a model asset?

        Only where the *serving process* resolves the weights itself. vLLM does:
        `--model Qwen/Qwen3.5-0.8B` makes the container download from the Hub at
        startup, so nothing is ever read from a datastore. A batch deployment
        cannot: the model asset is named in the deployment resource and mounted
        by the platform before any of our code runs.

        This is the escape hatch from the storage wall in VERIFIED.md section 24,
        and it is the reason `requires_model_asset` alone over-reports blockage.
        """
        return self.openai_compatible and self.surface in {
            Surface.AML_ONLINE_ENDPOINT,
            Surface.KUBERNETES,
            Surface.LOCAL,
        }

    @property
    def load_testable(self) -> bool:
        """Only an OpenAI-compatible interactive endpoint can be load-tested."""
        return self.is_interactive and self.openai_compatible

    def blocked_reason(
        self,
        dedicated_cores_available: int,
        *,
        instances: int = 1,
        sku: str | None = None,
        quota_family: str | None = None,
    ) -> str | None:
        """Explain why this pattern cannot deploy right now, or None if it can.

        `dedicated_cores_available` is the *measured* limit for `quota_family`,
        read from the Microsoft.Quota API -- not a guess. Patterns that run on
        LowPriority ignore it entirely.

        This used to return None for any non-zero quota, and that let a
        deployment through that Azure then rejected with

            (OutOfQuota) The amount of CPU quota requested is 72 and your
            maximum amount of quota is [N/A]

        on a 36-core SKU with exactly 36 cores granted. A managed online
        endpoint reserves upgrade headroom on top of the instances asked for,
        so the real requirement is higher. See required_dedicated_cores.
        """
        if self.allows_low_priority:
            return None

        target_sku = sku or self.default_sku
        try:
            needed = required_dedicated_cores(target_sku or "", instances=instances)
        except KeyError:
            # An unrecognised SKU cannot be checked; fall back to the old
            # any-quota-at-all test rather than blocking a valid deployment.
            if dedicated_cores_available > 0:
                return None
            needed = 0

        if needed and dedicated_cores_available >= needed:
            return None

        return (
            f"{self.display_name} on {target_sku} x{instances} needs "
            f"{needed} dedicated cores, because a managed online endpoint "
            f"reserves {ONLINE_ENDPOINT_UPGRADE_RESERVATION:.0%} of the "
            f"instances -- rounded up to a whole one -- as headroom to roll out "
            f"a new version. '{quota_family or self.quota_family}' has a limit of "
            f"{dedicated_cores_available} cores in this region. Request an "
            f"increase, pick a smaller SKU, or use a pattern with "
            f"allows_low_priority: true (e.g. aml_batch_vllm)."
        )


class AdapterModeSpec(BaseModel):
    """Documentation for one adapter delivery strategy."""

    key: AdapterMode
    display_name: str
    description: str = ""
    tradeoff: str = ""
