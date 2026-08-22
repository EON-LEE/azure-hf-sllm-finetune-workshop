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
"""

from __future__ import annotations

import pytest

from ffsft.deploy import preflight
from ffsft.deploy.preflight import SkuAvailability, sku_advisory


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