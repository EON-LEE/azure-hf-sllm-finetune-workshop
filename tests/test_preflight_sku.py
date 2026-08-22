"""Quota is permission to ask. `restrictions` is whether you may ask at all.

Three managed online deployments were created in koreacentral and none ever got
a node. The last two had everything measured correct -- 36 dedicated A10 cores
granted, a SKU small enough to fit, AcrPull in place, no model asset to stage --
and still sat in `Creating` for 50 and 85 minutes with no container logs, which
is what Azure looks like when the scheduler cannot place the workload anywhere.

The answer was in an API nobody had asked:

    GET /subscriptions/{sub}/providers/Microsoft.Compute/skus
        ?$filter=location eq 'koreacentral'

    Standard_NV12ads_A10_v5
      restrictions: [{ type: "Zone",
                       reasonCode: "NotAvailableForSubscription",
                       restrictionInfo: { zones: ["1","2","3"] } }]

koreacentral has exactly three zones, so all three being restricted means there
is nowhere to put it. The 36-core grant was real and completely worthless: we
had permission to ask for something the subscription is not allowed to have in
that region.

That is why this check reads `restrictions` and not quota, and why it runs
before the endpoint is created rather than after -- it costs one ARM read and
saves an hour of `Creating` per attempt.
"""

from __future__ import annotations

import pytest

from ffsft.deploy.preflight import SkuAvailability, sku_blocker


def avail(**overrides) -> SkuAvailability:
    base = {
        "sku": "Standard_NV12ads_A10_v5",
        "region": "westus2",
        "restrictions": [],
        "zones": ["1", "2", "3"],
    }
    base.update(overrides)
    return SkuAvailability(**base)


# -- the case that cost three deployments -------------------------------


def test_all_zones_restricted_is_blocked():
    state = avail(
        region="koreacentral",
        zones=["1", "2", "3"],
        restrictions=[
            {
                "type": "Zone",
                "reasonCode": "NotAvailableForSubscription",
                "restrictionInfo": {"zones": ["1", "2", "3"]},
            }
        ],
    )
    reason = sku_blocker(state)
    assert reason is not None
    assert "koreacentral" in reason
    assert "Standard_NV12ads_A10_v5" in reason


def test_the_blocked_message_says_quota_will_not_help():
    """The whole point: stop the next person buying a useless quota increase."""
    state = avail(
        region="koreacentral",
        restrictions=[
            {
                "type": "Zone",
                "reasonCode": "NotAvailableForSubscription",
                "restrictionInfo": {"zones": ["1", "2", "3"]},
            }
        ],
    )
    reason = sku_blocker(state)
    assert "quota" in reason.lower()


def test_location_restriction_is_blocked():
    state = avail(
        region="eastus",
        restrictions=[
            {
                "type": "Location",
                "reasonCode": "NotAvailableForSubscription",
                "restrictionInfo": {"locations": ["eastus"]},
            }
        ],
    )
    assert sku_blocker(state) is not None


# -- the cases that must stay deployable --------------------------------


def test_unrestricted_sku_is_not_blocked():
    assert sku_blocker(avail()) is None


def test_partial_zone_restriction_still_leaves_somewhere_to_land():
    """Two of three zones blocked is survivable -- zone 3 remains."""
    state = avail(
        zones=["1", "2", "3"],
        restrictions=[
            {
                "type": "Zone",
                "reasonCode": "NotAvailableForSubscription",
                "restrictionInfo": {"zones": ["1", "2"]},
            }
        ],
    )
    assert sku_blocker(state) is None


def test_non_availability_reason_codes_are_not_treated_as_blockers():
    """`QuotaId` restrictions describe offer eligibility, not placement.

    Blocking on every reasonCode would make the check refuse deployments that
    actually work, which is worse than not having it.
    """
    state = avail(
        restrictions=[
            {
                "type": "Location",
                "reasonCode": "QuotaId",
                "restrictionInfo": {"locations": ["westus2"]},
            }
        ],
    )
    assert sku_blocker(state) is None


# -- "not measured" must never be confused with "blocked" ---------------


def test_unknown_availability_does_not_block():
    """`None` means the read did not happen, which is not evidence of a problem.

    `read_storage_reachability` already established this convention and the
    AcrPull precheck violated it, which is how it stayed silent in the only
    case it existed to catch (VERIFIED 26.4).
    """
    assert sku_blocker(None) is None


def test_restrictions_none_is_unknown_not_permissive():
    """Distinguish "no restrictions" (`[]`) from "not read" (`None`)."""
    state = avail(restrictions=None)
    assert sku_blocker(state) is None


# -- shape of the record -------------------------------------------------


def test_blocked_zones_lists_what_was_refused():
    state = avail(
        restrictions=[
            {
                "type": "Zone",
                "reasonCode": "NotAvailableForSubscription",
                "restrictionInfo": {"zones": ["2", "1"]},
            }
        ]
    )
    assert state.blocked_zones == {"1", "2"}


def test_usable_zones_is_what_is_left():
    state = avail(
        zones=["1", "2", "3"],
        restrictions=[
            {
                "type": "Zone",
                "reasonCode": "NotAvailableForSubscription",
                "restrictionInfo": {"zones": ["1"]},
            }
        ],
    )
    assert state.usable_zones == {"2", "3"}


def test_region_wide_restriction_leaves_no_usable_zone():
    state = avail(
        zones=["1", "2", "3"],
        restrictions=[
            {
                "type": "Location",
                "reasonCode": "NotAvailableForSubscription",
                "restrictionInfo": {"locations": ["westus2"]},
            }
        ],
    )
    assert state.usable_zones == set()


@pytest.mark.parametrize(
    "zones,restricted,expected_block",
    [
        (["1", "2", "3"], ["1", "2", "3"], True),
        (["1", "2", "3"], ["1", "2"], False),
        (["1", "3"], ["1", "3"], True),
        ([], [], False),
    ],
)
def test_zone_arithmetic(zones, restricted, expected_block):
    state = avail(
        zones=zones,
        restrictions=(
            [
                {
                    "type": "Zone",
                    "reasonCode": "NotAvailableForSubscription",
                    "restrictionInfo": {"zones": restricted},
                }
            ]
            if restricted
            else []
        ),
    )
    assert (sku_blocker(state) is not None) is expected_block
