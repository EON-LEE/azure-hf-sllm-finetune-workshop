"""An ad-hoc Hugging Face model should still get a model-sized probe.

`startup_grace_for` already scales the readiness probe with parameter count, but
it only ever sees a number when the model came from `configs/models.yaml`. The
first live smoke deployment went out as `--hf-model Qwen/Qwen3-0.6B` with no
registry key, so `params_b` was `None`, so the probe fell back to the
conservative 600 s -- and a 0.6B container that could never start still took
about 25 minutes to be declared failed, with the logs withheld until then.

That is the exact failure the probe sizing was meant to remove, leaking back in
through the "model is swappable, so it may not be in the registry" path.

Hugging Face repo ids almost always encode the size (`Qwen3-0.6B`,
`Llama-3.1-8B-Instruct`, `Qwen3-30B-A3B`), so the number is recoverable without
a network call or a registry entry. Where it is not, an explicit override has to
win, and an unparseable id has to stay conservative rather than guess.
"""

from __future__ import annotations

import pytest

from ffsft.deploy.endpoint import params_from_hf_id, resolve_params_b, startup_grace_for


class FakeSpec:
    """Stand-in for ModelSpec; only `params_b` matters here."""

    def __init__(self, params_b):
        self.params_b = params_b


# --- reading the size out of the repo id -----------------------------------


@pytest.mark.parametrize(
    ("hf_id", "expected"),
    [
        ("Qwen/Qwen3-0.6B", 0.6),
        ("Qwen/Qwen3-8B", 8.0),
        ("meta-llama/Llama-3.1-8B-Instruct", 8.0),
        ("Qwen/Qwen3.8-27B", 27.0),
        ("openai/gpt-oss-20b", 20.0),
    ],
)
def test_the_size_suffix_is_recovered(hf_id, expected):
    assert params_from_hf_id(hf_id) == pytest.approx(expected)


def test_a_version_number_is_not_mistaken_for_a_size():
    """`3.1` in Llama-3.1-8B is a version; only a B-suffixed number is a size."""
    assert params_from_hf_id("meta-llama/Llama-3.1-8B-Instruct") == pytest.approx(8.0)


def test_moe_active_params_do_not_shrink_the_estimate():
    """Qwen3-30B-A3B downloads 30B of weights; the A3B is what stays active.

    Probe sizing follows the download, so the larger number has to win. Taking
    the last match instead of the largest would pick 3 here and under-size the
    grace period by a factor of ten.
    """
    assert params_from_hf_id("Qwen/Qwen3-30B-A3B") == pytest.approx(30.0)


def test_a_mixture_of_experts_counts_all_experts():
    """`8x7B` is ~56B on disk, not 7B, and disk is what startup pays for."""
    assert params_from_hf_id("mistralai/Mixtral-8x7B-Instruct-v0.1") >= 46.0


def test_an_id_with_no_size_is_not_guessed():
    assert params_from_hf_id("google-bert/bert-base-uncased") is None


def test_no_id_at_all_is_not_guessed():
    assert params_from_hf_id(None) is None


def test_a_bare_b_is_not_a_size():
    """`-Base` and `-bf16` must not read as a size suffix."""
    assert params_from_hf_id("some-org/model-7Base") is None


# --- deciding which source of truth wins -----------------------------------


def test_an_explicit_override_beats_everything():
    """The operator has looked at the model; the parser has looked at a string."""
    got = resolve_params_b(explicit=13.0, spec=FakeSpec(27.0), hf_model="Qwen/Qwen3-0.6B")
    assert got == pytest.approx(13.0)


def test_the_registry_beats_the_repo_id():
    """A curated spec is measured; a parsed id is inferred."""
    got = resolve_params_b(explicit=None, spec=FakeSpec(27.0), hf_model="Qwen/Qwen3-0.6B")
    assert got == pytest.approx(27.0)


def test_the_repo_id_is_used_when_there_is_no_registry_entry():
    """This is the case that produced the 25-minute smoke failure."""
    got = resolve_params_b(explicit=None, spec=None, hf_model="Qwen/Qwen3-0.6B")
    assert got == pytest.approx(0.6)


def test_a_spec_without_a_size_falls_through_to_the_repo_id():
    """A half-filled registry entry should not be worse than no entry at all."""
    got = resolve_params_b(explicit=None, spec=FakeSpec(None), hf_model="Qwen/Qwen3-8B")
    assert got == pytest.approx(8.0)


def test_nothing_knowable_stays_none():
    """`None` is meaningful: it is what makes the probe stay conservative."""
    assert resolve_params_b(explicit=None, spec=None, hf_model=None) is None
    assert resolve_params_b(explicit=None, spec=None, hf_model="azureml:qwen-ko:1") is None


# --- the reason any of this exists -----------------------------------------


def test_the_smoke_deployment_would_now_fail_fast():
    """The regression guard: end to end, 0.6B must not buy a 27B grace period.

    600 s of grace plus 10 failures at 30 s is 15 minutes of probing on top of
    the image pull. Sized from the id it is closer to two.
    """
    params = resolve_params_b(explicit=None, spec=None, hf_model="Qwen/Qwen3-0.6B")
    assert startup_grace_for(params) < 300


def test_a_real_27b_deployment_keeps_its_long_grace():
    """Failing fast must not come at the cost of failing a slow-loading model."""
    params = resolve_params_b(explicit=None, spec=None, hf_model="Qwen/Qwen3.8-27B")
    assert startup_grace_for(params) >= 600
