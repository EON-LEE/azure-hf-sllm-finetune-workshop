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
    ONLINE_ENDPOINT_CORE_MULTIPLIER,
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

    def test_multiplier_is_two(self):
        assert ONLINE_ENDPOINT_CORE_MULTIPLIER == 2

    def test_scales_with_instance_count(self):
        assert required_dedicated_cores("Standard_NV18ads_A10_v5", instances=2) == 72

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
        # Two 36-core instances need 144 cores, not 72.
        reason = online_spec().blocked_reason(72, instances=2)
        assert reason is not None
        assert "144" in reason
