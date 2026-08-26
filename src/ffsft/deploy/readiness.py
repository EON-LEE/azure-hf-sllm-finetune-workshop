"""How long a container may take to become healthy, and where that budget goes.

Split out of `endpoint.py` because it is the one stretch of the deploy path
that is pure arithmetic -- no client, no network, no state -- and because both
questions it answers cost a rollout each to get wrong:

* how long a 27B container needs before "not answering yet" means "broken"
  (JOURNAL §38: the budget used to live in `initial_delay`, which made every
  start slower instead of more patient), and
* which of Azure's three probe fields the answer belongs in (§38.6:
  `failure_threshold` is validated at request time and caps at 119, so the
  remainder has to go into `period`).

`endpoint.py` re-exports every name here, so an existing
`from ffsft.deploy.endpoint import startup_grace_for` keeps working.
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models.spec import ModelSpec


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


