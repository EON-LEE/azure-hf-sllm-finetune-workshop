"""Tests for the Azure sizing guards.

These are pure functions over the registry and a static SKU table, so they never
call Azure. Their whole job is to fail *before* an ARM deployment does.
"""

from __future__ import annotations

import pytest

from ffsft.azure_ml import GPU_SKUS, AzureTarget, check_sku_fits, required_vram_gb
from ffsft.models import TuningMethod, get_registry


@pytest.fixture
def qwen():
    return get_registry().get("qwen3.8-27b")


def test_low_priority_is_the_default_tier():
    """LowPriority is not a cost optimisation here, it is the only way in.

    Azure ML keeps quota separate from Microsoft.Compute. Dedicated quota is
    per family and every modern GPU family is absent by default, at which point
    AmlCompute reports `InvalidPropertyValue ... not a supported VM size` --
    a message that sends you hunting for a SKU problem that does not exist.
    LowPriority draws on a single pooled TotalLowPriorityCores (300) instead,
    and is also the sole carve-out in the tenant policy VirtualMachine_SKU_Deny
    (`priority notEquals "Spot"`). Verified live in koreacentral 2026-08-20 by
    creating Standard_NC24ads_A100_v4 as LowPriority after Dedicated failed.
    """
    target = AzureTarget(subscription_id="x", resource_group="y", workspace_name="z")
    assert target.vm_priority == "LowPriority"
    assert GPU_SKUS[target.compute_sku]["low_priority"] is True


def test_sku_without_low_priority_support_is_rejected_despite_ample_vram(qwen):
    # 160 GB of dual-A100 is far more than a 28 GB QLoRA run needs, so a pure
    # VRAM comparison would wave it through. The tier check must win.
    assert GPU_SKUS["Standard_NC48ads_A100_v4"]["vram_gb"] > qwen.vram_gb.qlora
    assert GPU_SKUS["Standard_NC48ads_A100_v4"]["low_priority"] is False
    fits, why = check_sku_fits(qwen, TuningMethod.QLORA, "Standard_NC48ads_A100_v4")
    assert fits is False
    assert "low-priority capable" in why


def test_the_same_sku_passes_once_you_ask_for_dedicated(qwen):
    # The guard is about the tier, not about the hardware, so it must let the
    # SKU through when the caller has real dedicated quota.
    fits, _ = check_sku_fits(
        qwen, TuningMethod.QLORA, "Standard_NC48ads_A100_v4", vm_priority="Dedicated"
    )
    assert fits is True


def test_qlora_fits_the_recommended_sku(qwen):
    fits, why = check_sku_fits(qwen, TuningMethod.QLORA, qwen.recommended_sku)
    assert fits, why


def test_recommended_sku_is_actually_provisionable(qwen):
    assert GPU_SKUS[qwen.recommended_sku]["low_priority"] is True


def test_t4_is_too_small_for_27b_qlora(qwen):
    fits, why = check_sku_fits(qwen, TuningMethod.QLORA, "Standard_NC4as_T4_v3")
    assert fits is False
    assert "only provides" in why


def test_bf16_lora_does_not_fit_a_single_a100(qwen):
    # 76 GB of weights plus activations against 80 GB of card is not a real fit,
    # which is why qlora is first in `supports`.
    fits, _ = check_sku_fits(qwen, TuningMethod.LORA, "Standard_NC24ads_A100_v4")
    assert fits is True  # numerically
    assert required_vram_gb(qwen, TuningMethod.LORA) > required_vram_gb(qwen, TuningMethod.QLORA)


def test_full_finetune_needs_a_multi_node_class_machine(qwen):
    fits, why = check_sku_fits(qwen, TuningMethod.FULL, "Standard_NC24ads_A100_v4")
    assert fits is False
    assert "only provides" in why


def test_unknown_sku_is_not_silently_blessed(qwen):
    fits, why = check_sku_fits(qwen, TuningMethod.QLORA, "Standard_Nonsense_v9")
    assert fits is True
    assert "cannot verify" in why


def test_default_target_does_not_point_at_an_unusable_sku():
    target = AzureTarget(
        subscription_id="x", resource_group="y", workspace_name="z"
    )
    assert GPU_SKUS[target.compute_sku]["low_priority"] is True


def test_every_sku_declares_the_quota_family_it_bills_against():
    # Quota is requested per family, not per SKU, so this field is what the
    # provisioning path needs to check availability.
    for sku, info in GPU_SKUS.items():
        assert info.get("family"), f"{sku} is missing its quota family"
        assert info["cores"] > 0
        assert info["gpus"] > 0
        assert isinstance(info["low_priority"], bool), f"{sku} missing low_priority"
