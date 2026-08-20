"""Tests for the model registry.

These never touch the network or Azure, so they are safe and free to run.
"""

from __future__ import annotations

import textwrap

import pytest
import yaml

from ffsft.models import KoreanTier, ModelRegistry, ModelSpec, Provider, TuningMethod, get_registry

# --------------------------------------------------------------------------
# The shipped registry
# --------------------------------------------------------------------------


def test_shipped_registry_loads():
    registry = get_registry()
    assert len(registry) > 0


def test_default_model_is_qwen38_27b():
    spec = get_registry().get("qwen3.8-27b")
    assert spec.hf_id == "Qwen/Qwen3.8-27B"
    assert spec.license == "apache-2.0"
    assert spec.commercial_use is True
    assert spec.params_b == pytest.approx(27.8)


def test_qwen38_defaults_to_qlora_because_bf16_lora_has_no_headroom():
    spec = get_registry().get("qwen3.8-27b")
    # 27.8B params in bf16 is ~55.6 GB of weights alone. QLoRA leaves real
    # headroom on one 80 GB A100; bf16 LoRA is estimated at ~76 GB, which is
    # "fits on paper, OOMs in practice" territory once sequences get long.
    single_gpu_gb = 80
    assert spec.supports_method(TuningMethod.QLORA)
    assert spec.recommended_method is TuningMethod.QLORA
    assert spec.vram_gb.qlora is not None
    assert spec.vram_gb.lora is not None
    assert spec.vram_gb.qlora < single_gpu_gb * 0.7
    assert spec.vram_gb.lora > single_gpu_gb * 0.9
    assert spec.vram_gb.full > single_gpu_gb


def test_qwen38_disables_thinking_in_chat_template():
    spec = get_registry().get("qwen3.8-27b")
    assert spec.chat_template_kwargs.get("enable_thinking") is False


def test_every_hf_model_has_an_hf_id():
    for spec in get_registry():
        if spec.provider in (Provider.HF, Provider.FOUNDRY_MANAGED):
            assert spec.hf_id, f"{spec.key} is missing hf_id"


def test_every_trainable_model_has_a_recommended_recipe():
    # The first entry of `supports` is the default recipe, so the YAML ordering
    # is load-bearing rather than cosmetic.
    for spec in get_registry():
        if spec.trainable:
            assert spec.recommended_method is spec.supports[0]
        else:
            assert spec.recommended_method is None


def test_mai_thinking_is_registered_but_not_trainable():
    spec = get_registry().get("mai-thinking-1")
    assert spec.provider is Provider.INFERENCE_ONLY
    assert spec.trainable is False
    assert spec.is_open_weights is False


def test_mai_ds_r1_is_the_only_open_weight_mai_model():
    open_mai = [
        s for s in get_registry() if s.key.startswith("mai-") and s.is_open_weights
    ]
    assert [s.key for s in open_mai] == ["mai-ds-r1"]


def test_korean_native_baseline_is_available_and_permissive():
    spec = get_registry().get("midm-2.0-mini")
    assert spec.korean_tier is KoreanTier.NATIVE
    assert spec.license == "mit"
    assert spec.commercial_use is True


def test_registry_keys_are_unique_and_sorted_filterable():
    registry = get_registry()
    assert len(registry.keys) == len(set(registry.keys))
    small = registry.filter(provider=Provider.HF, max_params_b=5.0)
    assert small, "expected at least one small HF model"
    assert all(s.params_b <= 5.0 for s in small)


# --------------------------------------------------------------------------
# Registry behaviour
# --------------------------------------------------------------------------


def _write_registry(tmp_path, body: str):
    path = tmp_path / "models.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_swapping_registry_file_swaps_the_models(tmp_path):
    path = _write_registry(
        tmp_path,
        """
        models:
          - key: my-model
            display_name: My Model
            provider: hf
            hf_id: acme/my-model
            supports: [lora]
            params_b: 1.0
            license: mit
            commercial_use: true
        """,
    )
    registry = ModelRegistry.load(path)
    assert registry.keys == ["my-model"]
    assert registry.get("my-model").hf_id == "acme/my-model"


def test_env_var_overrides_registry_path(tmp_path, monkeypatch):
    path = _write_registry(
        tmp_path,
        """
        models:
          - key: env-model
            display_name: Env Model
            provider: hf
            hf_id: acme/env-model
            supports: [qlora]
        """,
    )
    monkeypatch.setenv("FFSFT_MODEL_REGISTRY", str(path))
    assert ModelRegistry.load().keys == ["env-model"]


def test_duplicate_keys_are_rejected(tmp_path):
    path = _write_registry(
        tmp_path,
        """
        models:
          - key: dup
            display_name: A
            provider: hf
            hf_id: acme/a
          - key: dup
            display_name: B
            provider: hf
            hf_id: acme/b
        """,
    )
    with pytest.raises(ValueError, match="duplicate model keys"):
        ModelRegistry.load(path)


def test_unknown_key_lists_available_keys():
    with pytest.raises(KeyError, match="qwen3.8-27b"):
        get_registry().get("does-not-exist")


def test_hf_provider_requires_hf_id():
    with pytest.raises(ValueError, match="requires hf_id"):
        ModelSpec(key="x", display_name="X", provider=Provider.HF)


def test_inference_only_cannot_declare_tuning_methods():
    with pytest.raises(ValueError, match="cannot declare tuning methods"):
        ModelSpec(
            key="x",
            display_name="X",
            provider=Provider.INFERENCE_ONLY,
            foundry_model="X",
            supports=[TuningMethod.LORA],
        )


def test_require_method_explains_what_is_supported():
    spec = get_registry().get("mai-thinking-1")
    with pytest.raises(ValueError, match="does not support tuning method"):
        spec.require_method(TuningMethod.LORA)


# --------------------------------------------------------------------------
# Config file invariants that protect us legally / scientifically
# --------------------------------------------------------------------------


def _load_config(name: str) -> dict:
    from ffsft.models.registry import _DEFAULT_REGISTRY

    path = _DEFAULT_REGISTRY.parent / name
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_every_benchmark_is_eval_only():
    # Korean benchmarks are largely CC-BY-ND / CC-BY-NC. Training on them would
    # be both a license violation and test-set contamination.
    for bench in _load_config("benchmarks.yaml")["benchmarks"]:
        assert bench["eval_only"] is True, f"{bench['key']} must be eval_only"


def test_no_benchmark_id_appears_in_the_training_datasets():
    bench_ids = {b["dataset_id"] for b in _load_config("benchmarks.yaml")["benchmarks"]}
    train_ids = {d["dataset_id"] for d in _load_config("datasets.yaml")["datasets"]}
    assert not (bench_ids & train_ids)


def test_default_mix_contains_only_commercially_safe_datasets():
    cfg = _load_config("datasets.yaml")
    by_key = {d["key"]: d for d in cfg["datasets"]}
    default_mix = cfg["defaults"]["mix"]
    for key in cfg["mixes"][default_mix]["datasets"]:
        assert by_key[key]["commercial_use"] is True, f"{key} is not commercially safe"


def test_every_mix_references_known_datasets():
    cfg = _load_config("datasets.yaml")
    known = {d["key"] for d in cfg["datasets"]}
    for mix_name, mix in cfg["mixes"].items():
        for key in mix["datasets"]:
            assert key in known, f"mix {mix_name} references unknown dataset {key}"


def test_every_suite_references_known_benchmarks():
    cfg = _load_config("benchmarks.yaml")
    known = {b["key"] for b in cfg["benchmarks"]}
    for suite_name, suite in cfg["suites"].items():
        for key in suite["benchmarks"]:
            assert key in known, f"suite {suite_name} references unknown benchmark {key}"
