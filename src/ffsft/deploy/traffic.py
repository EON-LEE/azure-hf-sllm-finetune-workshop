"""Point an endpoint's URL at one deployment.

Deliberately separate from deploying and from load-testing. A deployment can
be created at 0% traffic and proved on its own routing header while the
endpoint URL still serves the old one; only once it answers correctly is it
worth moving traffic. That ordering is the whole point of blue/green, and it
only works if shifting is its own step.

Two things about this operation are not guessable, and both cost real time to
rediscover.

**It cannot be a PATCH.** `PATCH` on `onlineEndpoints` binds to
`PartialMinimalTrackedResourceWithIdentity` -- tags and identity, nothing else
-- and rejects the rest outright:

    Could not find member 'properties' on object of type
    'PartialMinimalTrackedResourceWithIdentity'

The first version of this ran that PATCH with output suppressed, so the 400
disappeared and the before/after print read as a harmless no-op.

**The endpoint must be read back and mutated, never rebuilt.** An entity
constructed fresh serialises `traffic` as `{}`, and PUTting it wipes the map --
the endpoint keeps answering, but routes to nothing. `tests/
test_endpoint_traffic_preserved.py` pins the same hazard on the deploy path.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # keep azure-ai-ml an optional import
    from azure.ai.ml import MLClient

log = logging.getLogger("ffsft.traffic")


def traffic_map(deployments: list[str], target: str) -> dict[str, int]:
    """All of the endpoint's traffic on `target`, and every sibling at zero.

    Every deployment is named explicitly rather than sending `{target: 100}`.
    A map that omits a deployment does not zero it -- it says nothing about it
    -- and the sum has to be 100 across the endpoint, so an omitted sibling
    still holding traffic makes the write invalid or the split wrong.
    """
    if target not in deployments:
        raise ValueError(
            f"deployment '{target}' is not on this endpoint. "
            f"Present: {', '.join(sorted(deployments)) or '(none)'}"
        )
    return {name: (100 if name == target else 0) for name in deployments}


def shift_traffic(
    client: MLClient,
    endpoint: str,
    target: str,
    *,
    require_succeeded: bool = True,
) -> dict[str, int]:
    """Move 100% of `endpoint` to `target`. Returns the traffic map read back.

    Refuses a deployment that is not `Succeeded`. A deployment stuck in
    `Creating` accepts a traffic assignment and then serves 5xx behind the
    endpoint URL, which looks like the model failing rather than the rollout
    never having finished.
    """
    deployment = client.online_deployments.get(name=target, endpoint_name=endpoint)
    state = getattr(deployment, "provisioning_state", None)
    if require_succeeded and state != "Succeeded":
        raise RuntimeError(
            f"{endpoint}/{target} is {state}, not Succeeded -- refusing to shift "
            f"traffic to a deployment that is not serving yet"
        )

    # Read back, then mutate. Never construct a fresh entity here.
    entity = client.online_endpoints.get(endpoint)
    before = dict(getattr(entity, "traffic", None) or {})
    log.info("traffic before: %s", before or "{} (nothing is served)")

    names = [d.name for d in client.online_deployments.list(endpoint_name=endpoint)]
    entity.traffic = traffic_map(names, target)
    client.online_endpoints.begin_create_or_update(entity).result()

    after = dict(getattr(client.online_endpoints.get(endpoint), "traffic", None) or {})
    log.info("traffic after : %s", after)
    if after.get(target) != 100:
        raise RuntimeError(
            f"shift failed: {target} is at {after.get(target)}%, expected 100. "
            f"Full map: {after}"
        )
    return after
