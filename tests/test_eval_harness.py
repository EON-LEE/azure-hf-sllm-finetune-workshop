"""How the evaluator asks lm-eval for a 4-bit model.

`hungry_bird_hlyr5cwzl8` trained fine and then died in the eval stage with

    TypeError: Qwen3_5ForCausalLM.__init__() got an unexpected keyword
               argument 'load_in_4bit'

lm-eval forwards `load_in_4bit` straight into `AutoModel.from_pretrained` as a
model kwarg. transformers v4 intercepted it and turned it into a
`BitsAndBytesConfig`; v5 removed that shim, so the flag falls through to the
model constructor. Same family as the `warmup_ratio` removal in
tests/test_qlora_config.py -- a deprecated shortcut that finally went away.

The supported spelling is `quantization_config`, which lm-eval passes through
untouched. These tests pin the *mapping* rather than the object, because
transformers is not installed in the dev environment (it lives in the training
image), and because the mapping is the part that broke.
"""

from __future__ import annotations

from ffsft.eval import run as eval_run


def test_four_bit_is_requested_as_a_quantization_config():
    """The whole point: `load_in_4bit` must not reach `from_pretrained`."""
    kwargs = eval_run.hflm_kwargs(
        "Qwen/Qwen3.8-27B", None,
        load_in_4bit=True, dtype="bfloat16", max_length=4096, batch_size="auto",
    )
    assert "load_in_4bit" not in kwargs
    assert "quantization_config" in kwargs


def test_the_quantization_matches_what_training_used():
    """A different quantisation at eval time measures a model nobody trained."""
    kwargs = eval_run.hflm_kwargs(
        "Qwen/Qwen3.8-27B", None,
        load_in_4bit=True, dtype="bfloat16", max_length=4096, batch_size="auto",
    )
    quant = kwargs["quantization_config"]
    assert quant["load_in_4bit"] is True
    assert quant["bnb_4bit_quant_type"] == "nf4"
    assert quant["bnb_4bit_use_double_quant"] is True
    assert quant["bnb_4bit_compute_dtype"] == "bfloat16"


def test_bf16_evaluation_carries_no_quantization_config():
    kwargs = eval_run.hflm_kwargs(
        "Qwen/Qwen3.8-27B", None,
        load_in_4bit=False, dtype="bfloat16", max_length=4096, batch_size="auto",
    )
    assert "quantization_config" not in kwargs
    assert "load_in_4bit" not in kwargs


def test_the_adapter_is_passed_as_peft():
    kwargs = eval_run.hflm_kwargs(
        "Qwen/Qwen3.8-27B", "./outputs",
        load_in_4bit=True, dtype="bfloat16", max_length=4096, batch_size="auto",
    )
    assert kwargs["peft"] == "./outputs"


def test_the_base_pass_sends_no_peft_key_at_all():
    """`peft=None` is not the same as absent for every lm-eval version."""
    kwargs = eval_run.hflm_kwargs(
        "Qwen/Qwen3.8-27B", None,
        load_in_4bit=True, dtype="bfloat16", max_length=4096, batch_size="auto",
    )
    assert "peft" not in kwargs


def test_pretrained_and_sizing_survive():
    kwargs = eval_run.hflm_kwargs(
        "Qwen/Qwen3.8-27B", None,
        load_in_4bit=True, dtype="bfloat16", max_length=2048, batch_size=8,
    )
    assert kwargs["pretrained"] == "Qwen/Qwen3.8-27B"
    assert kwargs["max_length"] == 2048
    assert kwargs["batch_size"] == 8
    assert kwargs["dtype"] == "bfloat16"


def test_the_logged_description_is_derived_from_the_real_kwargs():
    """The old code logged a `--model_args` string it never used to build HFLM.

    It advertised `bnb_4bit_quant_type=nf4` while constructing the model with
    plain `load_in_4bit=True`, so the log described a configuration that was
    not running. Deriving one from the other removes that whole class of lie.
    """
    kwargs = eval_run.hflm_kwargs(
        "Qwen/Qwen3.8-27B", "./outputs",
        load_in_4bit=True, dtype="bfloat16", max_length=4096, batch_size="auto",
    )
    described = eval_run.describe_model_args(kwargs)
    assert "pretrained=Qwen/Qwen3.8-27B" in described
    assert "peft=./outputs" in described
    assert "bnb_4bit_quant_type=nf4" in described
    assert "load_in_4bit=True" in described


# ---------------------------------------------------------------------------
# Refusing a task lm-eval has never heard of.
#
# configs/benchmarks.yaml claimed `harness_task: ifeval_ko` and
# `harness_task: hae_rae_bench`. Neither is a task in
# EleutherAI/lm-evaluation-harness -- the real Korean groups are `kobest`,
# `kmmlu` and `haerae`. A wrong name is not caught until simple_evaluate runs,
# which is *after* the weights are loaded: on a 27B model that is a download,
# a quantisation and several minutes of A100 before anything can fail.
# ---------------------------------------------------------------------------


def test_known_tasks_are_not_reported():
    assert eval_run.unknown_harness_tasks(["kobest", "kmmlu"], known={"kobest", "kmmlu"}) == []


def test_an_invented_task_is_reported():
    assert eval_run.unknown_harness_tasks(
        ["kobest", "ifeval_ko"], known={"kobest", "kmmlu", "haerae"}
    ) == ["ifeval_ko"]


def test_every_offender_is_reported_not_just_the_first():
    """One round trip should name all of them, not one per GPU hour."""
    assert eval_run.unknown_harness_tasks(
        ["ifeval_ko", "kobest", "hae_rae_bench"], known={"kobest"}
    ) == ["ifeval_ko", "hae_rae_bench"]


def test_the_configured_harness_tasks_all_exist_upstream():
    """The registry must not name a task the harness does not define.

    Checked against a literal set rather than by importing lm_eval, which is
    not installed in the dev environment. Verified against the upstream repo:
    lm_eval/tasks/{kobest,kmmlu,haerae}.
    """
    from ffsft.eval.registry import get_benchmark_registry

    upstream = {"kobest", "kmmlu", "kmmlu_hard", "haerae", "hrm8k"}
    named = [
        b.harness_task
        for b in get_benchmark_registry()
        if b.harness_task
    ]
    assert named, "no benchmark declares a harness task"
    assert eval_run.unknown_harness_tasks(named, known=upstream) == []
