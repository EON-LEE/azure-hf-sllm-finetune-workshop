"""Tests for the serving-pattern registry.

These are the constraints that decide whether a deployment succeeds, so they are
worth pinning: an online endpoint must never claim it can run on LowPriority,
and `blocked_reason` must key off measured quota rather than optimism.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ffsft.deploy import (
    AdapterMode,
    Engine,
    ServingRegistry,
    ServingSpec,
    Surface,
    get_serving,
    get_serving_registry,
)


def make_spec(**overrides) -> ServingSpec:
    base = {
        "key": "test",
        "display_name": "Test",
        "surface": "aml_batch_endpoint",
        "engine": "vllm_offline",
        "default_sku": "Standard_NC24ads_A100_v4",
    }
    base.update(overrides)
    return ServingSpec.model_validate(base)


# -- shipped registry ---------------------------------------------------


def test_shipped_registry_loads():
    registry = get_serving_registry()
    assert len(registry) >= 4
    assert "aml_batch_vllm" in registry
    assert registry.default.key in registry


def test_online_pattern_cannot_use_low_priority():
    """The single most expensive mistake this registry prevents."""
    for spec in get_serving_registry():
        if spec.surface is Surface.AML_ONLINE_ENDPOINT:
            assert spec.allows_low_priority is False
            assert spec.scale_to_zero is False


def test_batch_patterns_run_on_low_priority():
    for spec in get_serving_registry():
        if spec.surface is Surface.AML_BATCH_ENDPOINT:
            assert spec.allows_low_priority is True
            assert spec.scale_to_zero is True


def test_only_openai_compatible_patterns_are_load_testable():
    for spec in get_serving_registry():
        if spec.load_testable:
            assert spec.openai_compatible
            assert spec.is_interactive


def test_get_serving_rejects_unknown_key():
    with pytest.raises(KeyError, match="unknown serving pattern"):
        get_serving("does-not-exist")


def test_adapter_modes_documented():
    registry = get_serving_registry()
    keys = {m.key for m in registry.adapter_modes}
    assert keys == {AdapterMode.MERGED, AdapterMode.RUNTIME_ADAPTER}
    assert registry.adapter_mode("merged").tradeoff


# -- validation ---------------------------------------------------------


def test_online_endpoint_with_low_priority_is_rejected():
    with pytest.raises(ValidationError, match="reject LowPriority"):
        make_spec(surface="aml_online_endpoint", allows_low_priority=True)


def test_streaming_requires_openai_compatible():
    with pytest.raises(ValidationError, match="streaming requires openai_compatible"):
        make_spec(streaming=True, openai_compatible=False)


def test_non_local_pattern_needs_a_sku():
    with pytest.raises(ValidationError, match="needs a default_sku"):
        make_spec(default_sku=None)


def test_local_pattern_needs_no_sku():
    spec = make_spec(surface="local", default_sku=None)
    assert spec.default_sku is None


def test_duplicate_keys_rejected():
    with pytest.raises(ValueError, match="duplicate serving keys"):
        ServingRegistry([make_spec(), make_spec()])


def test_unknown_default_pattern_rejected():
    with pytest.raises(ValueError, match="default serving pattern"):
        ServingRegistry([make_spec()], default_key="nope")


# -- blocked_reason -----------------------------------------------------


def test_low_priority_pattern_is_never_blocked():
    spec = make_spec(allows_low_priority=True)
    assert spec.blocked_reason(0) is None


def test_dedicated_pattern_blocked_when_quota_is_zero():
    spec = make_spec(
        surface="aml_online_endpoint",
        engine="vllm",
        openai_compatible=True,
        allows_low_priority=False,
        quota_family="StandardNVADSA10v5Family",
    )
    reason = spec.blocked_reason(0)
    assert reason is not None
    assert "StandardNVADSA10v5Family" in reason
    assert "0 cores" in reason


def test_dedicated_pattern_unblocked_once_quota_granted():
    spec = make_spec(
        surface="aml_online_endpoint",
        engine="vllm",
        openai_compatible=True,
        allows_low_priority=False,
        quota_family="StandardNVADSA10v5Family",
    )
    # make_spec defaults to a 24-core A100 SKU, and a managed online endpoint
    # reserves double for rolling updates, so 48 is the threshold -- not 24.
    # This assertion used to read `blocked_reason(36) is None`, which is how a
    # deployment reached Azure and came back with "quota requested is 72".
    assert spec.blocked_reason(36) is not None
    assert spec.blocked_reason(48) is None


# -- filtering ----------------------------------------------------------


def test_filter_by_low_priority_excludes_online():
    deployable = get_serving_registry().filter(low_priority_only=True)
    assert deployable
    assert all(s.allows_low_priority for s in deployable)
    assert "aml_online_vllm" not in {s.key for s in deployable}


def test_filter_by_engine():
    vllm = get_serving_registry().filter(engine=Engine.VLLM)
    assert {s.key for s in vllm} >= {"aml_online_vllm", "local_vllm"}
