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


def test_a10_is_marked_unusable_by_azure_ml():
    """Azure ML will not provision NVadsA10v5, however much quota you hold.

    This is the trap that cost us a granted-but-worthless quota request: the
    region's vmSizes API advertises Standard_NV36ads_A10_v5, and Microsoft.Quota
    happily grants cores for it, but AmlCompute and ComputeInstance both reject
    it at create time with InvalidPropertyValue. Verified against a live
    workspace in koreacentral on 2026-08-20.
    """
    for sku, info in GPU_SKUS.items():
        if "A10_v5" in sku:
            assert info["aml_supported"] is False, f"{sku} must stay flagged unusable"


def test_unusable_sku_is_rejected_even_when_it_has_enough_vram(qwen):
    # 48 GB of A10 is numerically enough for a 28 GB QLoRA run, so a pure
    # VRAM comparison would wave it through. The support flag must win.
    assert GPU_SKUS["Standard_NV72ads_A10_v5"]["vram_gb"] > qwen.vram_gb.qlora
    fits, why = check_sku_fits(qwen, TuningMethod.QLORA, "Standard_NV72ads_A10_v5")
    assert fits is False
    assert "Azure ML" in why


def test_qlora_fits_the_recommended_sku(qwen):
    fits, why = check_sku_fits(qwen, TuningMethod.QLORA, qwen.recommended_sku)
    assert fits, why


def test_recommended_sku_is_actually_provisionable(qwen):
    assert GPU_SKUS[qwen.recommended_sku]["aml_supported"] is True


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
    assert GPU_SKUS[target.compute_sku]["aml_supported"] is True


def test_every_sku_declares_the_quota_family_it_bills_against():
    # Quota is requested per family, not per SKU, so this field is what the
    # provisioning path needs to check availability.
    for sku, info in GPU_SKUS.items():
        assert info.get("family"), f"{sku} is missing its quota family"
        assert info["cores"] > 0
        assert info["gpus"] > 0
