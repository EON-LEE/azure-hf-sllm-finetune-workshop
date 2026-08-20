"""The serving container's launch flags must follow the model, not the image.

This file exists because of a live failure. `ffsft-serve:2` ships with
MAMBA_CACHE_MODE=align, LANGUAGE_MODEL_ONLY=1 and REASONING_PARSER=qwen3 baked
in as ENV defaults, because those are the flags Qwen3.8-27B needs. The deploy
path never overrode them, so a smoke deployment of the dense, text-only
Qwen3-0.6B was launched with --language-model-only and --mamba-cache-mode
against a model that has neither a vision tower nor Mamba state.

The whole point of this repo is that the model is swappable, so the polarity
has to be the other way round: the image defaults to a plain vLLM launch and a
ModelSpec opts *in* to the flags its architecture actually requires.
"""

from __future__ import annotations

import pytest

from ffsft.deploy.endpoint import serving_env
from ffsft.models.registry import get_registry
from ffsft.models.spec import ModelSpec

ARCH_KEYS = ("MAMBA_CACHE_MODE", "LANGUAGE_MODEL_ONLY", "REASONING_PARSER")


def _spec(**kw) -> ModelSpec:
    base = {
        "key": "test-model",
        "display_name": "Test Model",
        "provider": "hf",
        "hf_id": "org/test-model",
    }
    base.update(kw)
    return ModelSpec(**base)


# --------------------------------------------------------------------------
# The regression guard: never rely on the image's ENV defaults.
# --------------------------------------------------------------------------


def test_arch_keys_are_always_emitted_even_when_neutral():
    """A stale image default must never be able to leak into a deployment."""
    env = serving_env(_spec(), hf_model="org/test-model")
    for key in ARCH_KEYS:
        assert key in env, f"{key} must be set explicitly, not inherited from the image"


def test_plain_model_neutralises_every_qwen38_flag():
    env = serving_env(_spec(), hf_model="org/test-model")
    assert env["MAMBA_CACHE_MODE"] == ""
    assert env["LANGUAGE_MODEL_ONLY"] == "0"
    assert env["REASONING_PARSER"] == ""


def test_unknown_model_is_neutral_rather_than_qwen38_shaped():
    """spec=None is the 'I don't know this model' case and must be safe."""
    env = serving_env(None, hf_model="some/unlisted-model")
    assert env["MAMBA_CACHE_MODE"] == ""
    assert env["LANGUAGE_MODEL_ONLY"] == "0"
    assert env["REASONING_PARSER"] == ""


# --------------------------------------------------------------------------
# Architecture flags are derived from the spec.
# --------------------------------------------------------------------------


def test_hybrid_model_requests_aligned_mamba_cache():
    env = serving_env(_spec(mamba_cache_mode="align"), hf_model="org/hybrid")
    assert env["MAMBA_CACHE_MODE"] == "align"


def test_multimodal_model_skips_the_vision_tower():
    env = serving_env(_spec(multimodal=True), hf_model="org/vl")
    assert env["LANGUAGE_MODEL_ONLY"] == "1"


def test_text_only_model_does_not_pass_language_model_only():
    env = serving_env(_spec(multimodal=False), hf_model="org/text")
    assert env["LANGUAGE_MODEL_ONLY"] == "0"


def test_reasoning_parser_comes_from_the_spec():
    env = serving_env(_spec(reasoning_parser="qwen3"), hf_model="org/thinker")
    assert env["REASONING_PARSER"] == "qwen3"


# --------------------------------------------------------------------------
# Model source and passthrough settings.
# --------------------------------------------------------------------------


def test_hf_model_becomes_the_model_path():
    env = serving_env(_spec(), hf_model="Qwen/Qwen3-0.6B")
    assert env["MODEL_PATH"] == "Qwen/Qwen3-0.6B"


def test_without_hf_model_the_mount_point_is_used():
    env = serving_env(_spec())
    assert env["MODEL_PATH"] == "/var/azureml-app/azureml-models"


def test_scalar_settings_are_stringified():
    env = serving_env(_spec(), max_model_len=4096, gpu_memory_utilization=0.85)
    assert env["MAX_MODEL_LEN"] == "4096"
    assert env["GPU_MEMORY_UTILIZATION"] == "0.85"
    assert all(isinstance(v, str) for v in env.values()), "AML rejects non-string env values"


def test_quantization_is_omitted_unless_requested():
    assert "QUANTIZATION" not in serving_env(_spec())
    assert serving_env(_spec(), quantization="awq")["QUANTIZATION"] == "awq"


def test_extra_args_is_omitted_unless_requested():
    assert "EXTRA_ARGS" not in serving_env(_spec())
    assert serving_env(_spec(), extra_args="--seed 0")["EXTRA_ARGS"] == "--seed 0"


# --------------------------------------------------------------------------
# The registry must carry the architecture facts we measured.
# --------------------------------------------------------------------------


def test_qwen38_registry_entry_declares_its_measured_architecture():
    """Measured from the real config.json: hybrid attention plus a vision tower."""
    spec = get_registry().get("qwen3.8-27b")
    assert spec.multimodal is True, "config.json carries vision_config/image_token_id"
    assert spec.mamba_cache_mode == "align", "48 of 64 layers are Gated DeltaNet"
    assert spec.reasoning_parser == "qwen3"


def test_qwen38_deployment_still_gets_all_three_flags():
    """The behaviour the image defaults used to provide must survive the flip."""
    env = serving_env(get_registry().get("qwen3.8-27b"), hf_model="Qwen/Qwen3.8-27B")
    assert env["MAMBA_CACHE_MODE"] == "align"
    assert env["LANGUAGE_MODEL_ONLY"] == "1"
    assert env["REASONING_PARSER"] == "qwen3"


@pytest.mark.parametrize("key", ["phi4-mini", "exaone-4.0-1.2b", "kanana2-3b"])
def test_dense_korean_models_are_not_treated_as_hybrid_multimodal(key):
    spec = get_registry().get(key)
    env = serving_env(spec, hf_model=spec.hf_id)
    assert env["MAMBA_CACHE_MODE"] == ""
    assert env["LANGUAGE_MODEL_ONLY"] == "0"
