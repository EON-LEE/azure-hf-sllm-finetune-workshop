"""`restrictions` describes purchase eligibility, not placement.

An earlier version of this module raised on any `NotAvailableForSubscription`
restriction, on the theory that it explained why three managed online
deployments in koreacentral never got a node. Before trusting it, it was run
against a SKU whose behaviour is not in doubt:

    Standard_NC24ads_A100_v4, koreacentral
      restrictions: [{ type: "Location",
                       reasonCode: "NotAvailableForSubscription",
                       restrictionInfo: { locations: ["KoreaCentral"] } }]

That is the strongest restriction the API expresses -- the entire region, not
a zone -- and that exact SKU in that exact region is the LowPriority AmlCompute
cluster which fine-tuned a 27B model for 42 minutes at train_loss 1.2638.

The check would therefore have refused the one GPU configuration this
subscription is proven able to run. `restrictions` reflects on-demand dedicated
eligibility; LowPriority/Spot allocates from a different pool and is unaffected.
Five ordinary CPU SKUs in koreacentral carry the same restriction while the
region happily runs CPU workloads.

So these facts are reported and never enforced. A preflight with false
negatives gets ignored; a preflight with false positives gets deleted, and
takes the checks that did work with it.

Refined 2026-08-24, without retracting any of the above. The reasoning holds
wherever LowPriority is available, which is every AmlCompute caller. It does not
hold for managed online endpoints, which reject LowPriority outright and so have
no pool that ignores the restriction. `online_endpoint_blocker` enforces the
field for that one caller only; `sku_advisory` still merely reports it for
everyone else, and a test below pins the A100 case above so this split cannot
quietly become a blanket refusal again. Cost of the missing distinction: two
further rollouts at `percentComplete: 0.0`, 108 and 113 minutes, no container
ever created. See VERIFIED §40.
"""

from __future__ import annotations

import pytest

from ffsft.deploy import preflight
from ffsft.deploy.preflight import (
    SkuAvailability,
    online_endpoint_blocker,
    sku_advisory,
)


def avail(**overrides) -> SkuAvailability:
    base = {
        "sku": "Standard_NV12ads_A10_v5",
        "region": "westus2",
        "restrictions": [],
        "zones": ["1", "2", "3"],
    }
    base.update(overrides)
    return SkuAvailability(**base)


LOCATION_RESTRICTION = [
    {
        "type": "Location",
        "reasonCode": "NotAvailableForSubscription",
        "restrictionInfo": {"locations": ["KoreaCentral"]},
    }
]

ALL_ZONES_RESTRICTION = [
    {
        "type": "Zone",
        "reasonCode": "NotAvailableForSubscription",
        "restrictionInfo": {"locations": ["KoreaCentral"], "zones": ["1", "2", "3"]},
    }
]


# -- the falsification, pinned so it cannot be undone -------------------


def test_the_training_cluster_sku_is_never_refused():
    """Standard_NC24ads_A100_v4 is Location-restricted in koreacentral and runs.

    If this test ever fails, the module has gone back to refusing the only GPU
    this subscription can actually allocate.
    """
    state = avail(
        sku="Standard_NC24ads_A100_v4",
        region="koreacentral",
        zones=["3"],
        restrictions=LOCATION_RESTRICTION,
    )
    note = sku_advisory(state)
    assert note is not None, "the fact should still be reported"
    assert "LowPriority" in note, "must say why this may be survivable"


def test_module_exposes_no_blocker_for_skus():
    """Guards against reintroducing a hard block under the old name."""
    assert not hasattr(preflight, "sku_blocker")


@pytest.mark.parametrize(
    "restrictions",
    [LOCATION_RESTRICTION, ALL_ZONES_RESTRICTION, [], None],
)
def test_advisory_never_raises(restrictions):
    sku_advisory(avail(restrictions=restrictions))


# -- what the advisory has to say ---------------------------------------


def test_advisory_names_region_and_sku():
    note = sku_advisory(
        avail(sku="Standard_NV12ads_A10_v5", region="koreacentral",
              restrictions=ALL_ZONES_RESTRICTION)
    )
    assert "koreacentral" in note and "Standard_NV12ads_A10_v5" in note


def test_advisory_does_not_promise_quota_is_the_answer():
    """The A10 rollouts failed with quota granted. Do not send anyone there."""
    note = sku_advisory(avail(region="koreacentral", restrictions=ALL_ZONES_RESTRICTION))
    assert "not conclusive" in note.lower()


def test_unrestricted_sku_has_nothing_to_say():
    assert sku_advisory(avail()) is None


def test_unknown_reason_codes_are_ignored():
    """`QuotaId` is offer eligibility and appears on SKUs that deploy fine."""
    state = avail(
        restrictions=[
            {"type": "Location", "reasonCode": "QuotaId",
             "restrictionInfo": {"locations": ["westus2"]}}
        ]
    )
    assert sku_advisory(state) is None


# -- "not measured" is not a finding ------------------------------------


def test_none_state_is_silent():
    assert sku_advisory(None) is None


def test_unread_restrictions_are_silent():
    assert sku_advisory(avail(restrictions=None)) is None


# -- the factual properties still hold ----------------------------------


def test_blocked_zones_lists_what_was_named():
    assert avail(restrictions=ALL_ZONES_RESTRICTION).blocked_zones == {"1", "2", "3"}


def test_usable_zones_subtracts_rather_than_counts():
    """koreacentral offers A10 in zones 2 and 3 but restricts 1, 2 and 3.

    The restricted set is not a subset of the offered set, so comparing
    lengths would be wrong.
    """
    state = avail(region="koreacentral", zones=["2", "3"],
                  restrictions=ALL_ZONES_RESTRICTION)
    assert state.usable_zones == set()


def test_partial_zone_restriction_leaves_the_rest():
    state = avail(
        restrictions=[
            {"type": "Zone", "reasonCode": "NotAvailableForSubscription",
             "restrictionInfo": {"zones": ["1"]}}
        ]
    )
    assert state.usable_zones == {"2", "3"}


def test_region_restriction_leaves_no_zone():
    assert avail(restrictions=LOCATION_RESTRICTION).usable_zones == set()
    assert avail(restrictions=LOCATION_RESTRICTION).region_blocked is True


# -- managed online endpoints have no Spot escape hatch -------------------


def _koreacentral_a10() -> SkuAvailability:
    """The exact ARM response that cost two rollouts on 2026-08-24.

    Copied from `Microsoft.Compute/skus` for `Standard_NV36ads_A10_v5` in
    koreacentral rather than invented, so the test fails if the shape of the
    field ever changes underneath it.
    """
    return SkuAvailability(
        sku="Standard_NV36ads_A10_v5",
        region="koreacentral",
        zones=["2", "3"],
        restrictions=[
            {
                "type": "Zone",
                "reasonCode": "NotAvailableForSubscription",
                "values": ["KoreaCentral"],
                "restrictionInfo": {
                    "locations": ["KoreaCentral"],
                    "zones": ["1", "2", "3"],
                },
            }
        ],
    )


def test_the_sku_that_stalled_twice_is_refused_before_anything_is_spent():
    blocker = online_endpoint_blocker(_koreacentral_a10())
    assert blocker is not None
    assert "Standard_NV36ads_A10_v5" in blocker
    assert "LowPriority" in blocker


def test_the_a100_that_trains_here_is_still_only_an_advisory():
    """`sku_advisory` must keep reporting rather than refusing.

    The A100 carries a stricter `Location` restriction than the A10 and is the
    cluster that fine-tuned the 27B model. The new blocker applies to managed
    online endpoints; it must not have turned the advisory into a refusal for
    everyone else.
    """
    a100 = SkuAvailability(
        sku="Standard_NC24ads_A100_v4",
        region="koreacentral",
        zones=[],
        restrictions=[
            {"type": "Location", "reasonCode": "NotAvailableForSubscription"}
        ],
    )
    assert sku_advisory(a100) is not None
    assert "not conclusive" in sku_advisory(a100)


def test_an_unread_restriction_is_never_a_refusal():
    """"Could not look" is not "looked and it is blocked"."""
    assert online_endpoint_blocker(None) is None
    assert (
        online_endpoint_blocker(
            SkuAvailability(sku="x", region="koreacentral", restrictions=None)
        )
        is None
    )


def test_a_sku_with_one_zone_left_is_allowed_through():
    ok = SkuAvailability(
        sku="Standard_NV36ads_A10_v5",
        region="koreacentral",
        zones=["1", "2", "3"],
        restrictions=[
            {
                "type": "Zone",
                "reasonCode": "NotAvailableForSubscription",
                "restrictionInfo": {"zones": ["1", "2"]},
            }
        ],
    )
    assert online_endpoint_blocker(ok) is None


def test_quota_id_restrictions_do_not_block():
    """`QuotaId` describes which offers may buy the SKU, not placement."""
    state = SkuAvailability(
        sku="Standard_NV36ads_A10_v5",
        region="koreacentral",
        zones=["1"],
        restrictions=[
            {"type": "Zone", "reasonCode": "QuotaId", "restrictionInfo": {"zones": ["1"]}}
        ],
    )
    assert online_endpoint_blocker(state) is None
