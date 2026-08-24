"""A model that ships its own modelling code has to be allowed to run it.

The registry advertises that any Hugging Face model is a drop-in target, and
that held right up until a model shipped custom code. `kanana2-1.3b` is such a
model, and the job that tried to train it died before the first step:

    ValueError: The repository kakaocorp/kanana-2-1.3b-instruct contains custom
    code which must be executed to correctly load the model.
    Please pass the argument `trust_remote_code=True` to allow custom code to
    be run.

    (job frank_cushion_b725dqgkyf, 2026-08-24)

transformers actually tries to *prompt* for consent, which on a compute node
reaches a closed stdin and raises `EOFError` first, so the traceback arrives
doubled and the real sentence is the second one.

The fix is deliberately not a blanket `trust_remote_code=True`. That flag
executes arbitrary Python from a model repo at load time, so it is a per-model
decision that belongs in `configs/models.yaml` next to the license and the
target modules, where it is reviewable in a diff. Default off; a model opts in.

Every loader has to agree. Training that trusts the code and evaluation that
does not is not a safer system, it is one that trains a model it cannot score.
"""

from __future__ import annotations

import pytest

from ffsft.eval.run import model_load_kwargs
from ffsft.models.registry import get_model
from ffsft.models.spec import ModelSpec
from ffsft.train.qlora import QLoRAConfig, base_load_kwargs


def _spec(**over) -> ModelSpec:
    base = dict(
        key="test-model",
        display_name="Test",
        provider="hf",
        hf_id="acme/test",
    )
    base.update(over)
    return ModelSpec(**base)


# --- the registry field ------------------------------------------------


def test_a_model_does_not_run_repo_code_unless_it_says_so():
    """Off by default. The flag executes code from the repo, so silence is a no."""
    assert _spec().trust_remote_code is False


def test_a_model_can_opt_in_from_the_registry():
    assert _spec(trust_remote_code=True).trust_remote_code is True


def test_kanana_is_marked_as_needing_its_own_code():
    """The model whose failure motivated this. Pins the registry, not the loader."""
    assert get_model("kanana2-1.3b").trust_remote_code is True


def test_qwen_is_not_marked_because_it_does_not_need_it():
    """Guards against someone 'fixing' this by turning the flag on everywhere."""
    assert get_model("qwen3.8-27b").trust_remote_code is False


# --- training ----------------------------------------------------------


def test_training_forwards_the_flag_to_the_model():
    kwargs = base_load_kwargs(_spec(trust_remote_code=True), QLoRAConfig())
    assert kwargs["trust_remote_code"] is True


def test_training_does_not_trust_a_model_that_did_not_ask():
    kwargs = base_load_kwargs(_spec(), QLoRAConfig())
    assert kwargs["trust_remote_code"] is False


def test_training_still_pins_placement_and_attention():
    """The new kwarg must not displace the two that made 27B fit."""
    kwargs = base_load_kwargs(_spec(), QLoRAConfig())
    assert kwargs["device_map"] == {"": 0}
    assert kwargs["attn_implementation"] == "sdpa"


# --- evaluation --------------------------------------------------------


def test_eval_forwards_the_flag():
    kwargs = model_load_kwargs(
        load_in_4bit=True, dtype="bfloat16", trust_remote_code=True
    )
    assert kwargs["trust_remote_code"] is True


def test_eval_defaults_to_not_trusting():
    kwargs = model_load_kwargs(load_in_4bit=True, dtype="bfloat16")
    assert kwargs["trust_remote_code"] is False


@pytest.mark.parametrize("flag", [True, False])
def test_eval_keeps_the_quantization_mapping_intact(flag: bool):
    """The kwarg rides alongside; it does not disturb what lm-eval needs."""
    kwargs = model_load_kwargs(
        load_in_4bit=True, dtype="bfloat16", trust_remote_code=flag
    )
    assert "quantization_config" in kwargs
    assert kwargs["dtype"] == "bfloat16"
