"""Tests for online-endpoint quota arithmetic.

Written after a real deployment failure and before the fix. Azure rejected a
deployment with:

    (OutOfQuota) Not enough subscription CPU quota. The amount of CPU quota
    requested is 72 and your maximum amount of quota is [N/A].

The requested SKU was Standard_NV36ads_A10_v5, which is 36 cores, and the
subscription had exactly 36 dedicated A10 cores granted. Azure asked for 72
because a managed online endpoint reserves a second full set of instances so it
can roll a new version out before tearing the old one down.

`blocked_reason` previously passed anything above zero, so it cleared a
deployment that could never have been created. These tests pin the doubling.
"""

from __future__ import annotations

import pytest

from ffsft.deploy.spec import (
    ONLINE_ENDPOINT_UPGRADE_RESERVATION,
    Engine,
    ServingSpec,
    Surface,
    required_dedicated_cores,
)


def online_spec(sku: str = "Standard_NV36ads_A10_v5") -> ServingSpec:
    return ServingSpec(
        key="aml_online_vllm",
        display_name="AML managed online endpoint (vLLM)",
        surface=Surface.AML_ONLINE_ENDPOINT,
        engine=Engine.VLLM,
        openai_compatible=True,
        streaming=True,
        allows_low_priority=False,
        quota_family="StandardNVADSA10v5Family",
        default_sku=sku,
        scale_to_zero=False,
    )


def batch_spec() -> ServingSpec:
    return ServingSpec(
        key="aml_batch_vllm",
        display_name="AML batch endpoint (vLLM)",
        surface=Surface.AML_BATCH_ENDPOINT,
        engine=Engine.VLLM,
        allows_low_priority=True,
        quota_family="TotalLowPriorityCores",
        default_sku="Standard_NC24ads_A100_v4",
    )


class TestRequiredDedicatedCores:
    def test_doubles_the_sku_cores_for_rolling_update_headroom(self):
        # Azure asked for 72 when the SKU was 36 cores.
        assert required_dedicated_cores("Standard_NV36ads_A10_v5") == 72

    def test_reservation_factor_is_twenty_percent(self):
        assert ONLINE_ENDPOINT_UPGRADE_RESERVATION == 1.2

    def test_reservation_rounds_up_per_deployment_not_per_instance(self):
        # ceil(1.2 * 2) == 3 instances' worth, not 4. The flat x2 model agreed
        # with every observation at one instance and over-charged by a whole
        # instance from two upwards.
        assert required_dedicated_cores("Standard_NV18ads_A10_v5", instances=2) == 54

    def test_matches_the_worked_example_in_the_quota_doc(self):
        # "if you request 10 instances of a [4-core VM] in a deployment, you
        # should have a quota for 48 cores (12 instances * 4 cores)".
        assert required_dedicated_cores("Standard_NC4as_T4_v3", instances=10) == 48

    def test_a100_family_skips_the_reservation_entirely(self):
        # "Skip 20% Reservation: Yes" in the supported-SKU list. Charging this
        # family double asks for 48 cores where Azure asks for 24 -- the
        # difference between fitting a 48-core grant twice and not at all.
        assert required_dedicated_cores("Standard_NC24ads_A100_v4") == 24

    def test_h100_family_skips_the_reservation_entirely(self):
        assert required_dedicated_cores("Standard_NC40ads_H100_v5") == 40

    def test_a10_family_still_pays_the_reservation(self):
        # The exemption is per-family. A10 is not on the exempt list, which is
        # why 36 granted cores could not host a 36-core SKU.
        assert required_dedicated_cores("Standard_NV6ads_A10_v5") == 12

    def test_smaller_sku_needs_proportionally_less(self):
        assert required_dedicated_cores("Standard_NV18ads_A10_v5") == 36

    def test_unknown_sku_raises_rather_than_guessing(self):
        # Silently assuming a core count would reproduce exactly the failure
        # this module exists to prevent.
        with pytest.raises(KeyError):
            required_dedicated_cores("Standard_Nonexistent_v9")


class TestBlockedReason:
    def test_exact_sku_cores_are_not_enough(self):
        # The bug: 36 granted, 36-core SKU, previously reported deployable.
        reason = online_spec().blocked_reason(36)
        assert reason is not None
        assert "72" in reason

    def test_double_the_cores_clears_it(self):
        assert online_spec().blocked_reason(72) is None

    def test_zero_quota_is_blocked(self):
        reason = online_spec().blocked_reason(0)
        assert reason is not None
        assert "0" in reason

    def test_smaller_sku_fits_the_same_quota(self):
        # The workaround used live: an 18-core SKU needs 36, which was granted.
        assert online_spec("Standard_NV18ads_A10_v5").blocked_reason(36) is None

    def test_low_priority_pattern_ignores_dedicated_quota(self):
        assert batch_spec().blocked_reason(0) is None

    def test_reason_names_the_quota_family_to_request(self):
        reason = online_spec().blocked_reason(0)
        assert "StandardNVADSA10v5Family" in reason

    def test_reason_suggests_a_pattern_that_works_today(self):
        reason = online_spec().blocked_reason(0)
        assert "low_priority" in reason or "batch" in reason

    def test_instances_are_accounted_for(self):
        # Two 36-core instances need ceil(1.2 * 2) = 3 instances' worth: 108.
        reason = online_spec().blocked_reason(72, instances=2)
        assert reason is not None
        assert "108" in reason
