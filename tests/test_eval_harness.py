"""How the evaluator builds the model lm-eval scores.

Two failed runs got us here, and both were lm-eval's model loader.

`hungry_bird_hlyr5cwzl8`:

    TypeError: Qwen3_5ForCausalLM.__init__() got an unexpected keyword
               argument 'load_in_4bit'

transformers v4 intercepted `load_in_4bit` and turned it into a
`BitsAndBytesConfig`; v5 removed that shim, so lm-eval's pass-through drops it
into the model constructor.

`ashy_hamster_9lvxsm0y2s`, after switching to the supported spelling:

    TypeError: HFLM._create_model() got multiple values for keyword argument
               'quantization_config'

because `quantization_config` is not an input to HFLM at all -- it reads that
name off the *checkpoint's own* config (for repos that ship pre-quantized) and
forwards it itself. There is no supported way to ask this HFLM for on-the-fly
bitsandbytes quantization.

So the model is built here and handed to HFLM as an object. `HFLM.__init__`
takes `pretrained: str | PreTrainedModel`, and the object branch assigns
`self._model = pretrained` and skips `_create_model` entirely -- no loader, no
collision, and no third round trip when lm-eval renames something else. It
also skips the `.to(self.device)` call that a bitsandbytes model rejects.

These tests pin the kwarg *mapping*. transformers, torch and lm_eval are not
installed in the dev environment -- they live in the training image.
"""

from __future__ import annotations

from ffsft.eval import run as eval_run

# -- what `AutoModelForCausalLM.from_pretrained` is asked for ---------------


def test_four_bit_is_requested_as_a_quantization_config():
    """`load_in_4bit` as a bare kwarg is the thing transformers v5 removed."""
    kwargs = eval_run.model_load_kwargs(load_in_4bit=True, dtype="bfloat16")
    assert "load_in_4bit" not in kwargs
    assert "quantization_config" in kwargs


def test_the_quantization_matches_what_training_used():
    """A different quantisation at eval time measures a model nobody trained."""
    quant = eval_run.model_load_kwargs(load_in_4bit=True, dtype="bfloat16")[
        "quantization_config"
    ]
    assert quant["load_in_4bit"] is True
    assert quant["bnb_4bit_quant_type"] == "nf4"
    assert quant["bnb_4bit_use_double_quant"] is True
    assert quant["bnb_4bit_compute_dtype"] == "bfloat16"


def test_bf16_evaluation_carries_no_quantization_config():
    kwargs = eval_run.model_load_kwargs(load_in_4bit=False, dtype="bfloat16")
    assert "quantization_config" not in kwargs
    assert "load_in_4bit" not in kwargs
    assert kwargs["dtype"] == "bfloat16"


def test_the_model_is_loaded_the_way_training_loaded_it():
    """Same placement and attention kernel as `qlora.py`, which ran 27B at 28GB.

    HFLM will not move an object-mode model for us, and a bitsandbytes model
    refuses `.to()` anyway, so placement has to be right at construction.
    `{"": 0}` and `sdpa` are the pair proven on `olden_bean_302vkc7nbz`;
    `device_map="auto"` is not, and the hybrid Gated-DeltaNet layers are not
    something to take an unproven kernel path through.
    """
    kwargs = eval_run.model_load_kwargs(load_in_4bit=True, dtype="bfloat16")
    assert kwargs["device_map"] == {"": 0}
    assert kwargs["attn_implementation"] == "sdpa"


# -- what HFLM itself is asked for -----------------------------------------


def test_harness_kwargs_do_not_mention_the_checkpoint():
    """The model arrives as an object, so nothing here may name a repo or path.

    Any `pretrained=<str>` here would send HFLM back down `_create_model`,
    which is the code path both failures came from.
    """
    kwargs = eval_run.harness_kwargs(max_length=4096, batch_size="auto")
    assert "pretrained" not in kwargs
    assert "peft" not in kwargs
    assert "quantization_config" not in kwargs
    assert "dtype" not in kwargs


def test_harness_kwargs_pin_the_backend():
    """Left to `default`, HFLM infers causal vs seq2seq from the architecture.

    The adapter arrives wrapped in a PeftModel and Qwen3.8's checkpoint
    declares a ConditionalGeneration class, so the inference is not worth
    trusting when the answer is known.
    """
    assert eval_run.harness_kwargs(max_length=4096, batch_size="auto")["backend"] == "causal"


def test_harness_kwargs_carry_sizing():
    kwargs = eval_run.harness_kwargs(max_length=2048, batch_size=8)
    assert kwargs["max_length"] == 2048
    assert kwargs["batch_size"] == 8


# -- the log line ----------------------------------------------------------


def test_the_logged_description_is_derived_from_the_real_kwargs():
    """The original code logged a `--model_args` string it never used.

    It advertised `bnb_4bit_quant_type=nf4` while constructing HFLM with a
    plain `load_in_4bit=True`, so the log described a configuration that was
    not running. Deriving one from the other removes that class of lie.
    """
    described = eval_run.build_model_args(
        "Qwen/Qwen3.8-27B", "./outputs",
        load_in_4bit=True, dtype="bfloat16", max_length=4096, batch_size="auto",
    )
    assert "pretrained=Qwen/Qwen3.8-27B" in described
    assert "peft=./outputs" in described
    assert "bnb_4bit_quant_type=nf4" in described
    assert "load_in_4bit=True" in described


def test_the_base_pass_is_described_without_an_adapter():
    described = eval_run.build_model_args(
        "Qwen/Qwen3.8-27B", None,
        load_in_4bit=True, dtype="bfloat16", max_length=4096, batch_size="auto",
    )
    assert "peft=" not in described


# -- refusing a task lm-eval has never heard of ----------------------------
#
# configs/benchmarks.yaml claimed `harness_task: ifeval_ko` and
# `harness_task: hae_rae_bench`. Neither is a task in
# EleutherAI/lm-evaluation-harness -- the real Korean groups are `kobest`,
# `kmmlu` and `haerae`. A wrong name is not caught until simple_evaluate runs,
# which is *after* the weights are loaded: on a 27B model that is a download,
# a quantisation and several minutes of A100 before anything can fail.


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
    named = [b.harness_task for b in get_benchmark_registry() if b.harness_task]
    assert named, "no benchmark declares a harness task"
    assert eval_run.unknown_harness_tasks(named, known=upstream) == []
