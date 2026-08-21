"""Tests for mapping the recipe's knobs onto whatever SFTConfig actually accepts.

This exists because the training image deliberately tracks bleeding-edge model
libraries -- Qwen3.8 needs transformers>=5.8, so the image ships 5.15.1 and
trl 1.10.0 -- and those libraries rename constructor arguments between
releases. transformers v5 removed `warmup_ratio` in favour of a `warmup_steps`
that accepts a float below 1 with the identical meaning, and trl renamed
`max_seq_length` to `max_length`.

Each of those renames is a `TypeError` raised by `SFTConfig.__init__`, which on
Azure ML happens *after* the node is allocated, the image is pulled and the
weights are downloaded. For Qwen3.8-27B that is 54 GB and roughly an hour of
A100 time to learn that one keyword moved.

So the mapping is a pure function over a set of accepted field names, and these
tests pin its behaviour for both library generations without importing trl --
which is not installed locally, and should not have to be.
"""

from __future__ import annotations

import pytest

from ffsft.train.qlora import QLoRAConfig, accepted_fields, sft_config_kwargs

# The names that matter, as each generation spells them.
V4_FIELDS = {
    "output_dir", "per_device_train_batch_size", "gradient_accumulation_steps",
    "gradient_checkpointing", "gradient_checkpointing_kwargs", "learning_rate",
    "warmup_ratio", "num_train_epochs", "max_steps", "logging_steps",
    "save_steps", "save_total_limit", "bf16", "optim", "lr_scheduler_type",
    "max_seq_length", "seed", "report_to",
}
V5_FIELDS = (V4_FIELDS - {"warmup_ratio", "max_seq_length"}) | {
    "warmup_steps", "max_length"
}


@pytest.fixture
def cfg():
    return QLoRAConfig(warmup_ratio=0.03, max_seq_length=1024)


def test_v4_libraries_get_the_original_names(cfg):
    kwargs = sft_config_kwargs(cfg, V4_FIELDS)
    assert kwargs["warmup_ratio"] == 0.03
    assert kwargs["max_seq_length"] == 1024
    assert "warmup_steps" not in kwargs
    assert "max_length" not in kwargs


def test_v5_libraries_get_the_renamed_ones_with_the_same_values(cfg):
    """transformers v5 reads a float below 1 on warmup_steps as a ratio.

    So the migration really is a rename and the value carries over untouched;
    converting it to a step count here would silently change the schedule.
    """
    kwargs = sft_config_kwargs(cfg, V5_FIELDS)
    assert kwargs["warmup_steps"] == 0.03
    assert kwargs["max_length"] == 1024
    assert "warmup_ratio" not in kwargs
    assert "max_seq_length" not in kwargs


def test_a_knob_neither_generation_accepts_is_dropped_rather_than_raised(cfg):
    """A missing optional knob must not be fatal.

    Losing `warmup_ratio` costs a slightly different LR schedule. Raising costs
    the whole run, an hour after it was submitted.
    """
    kwargs = sft_config_kwargs(cfg, V5_FIELDS - {"warmup_steps"})
    assert "warmup_steps" not in kwargs
    assert "warmup_ratio" not in kwargs
    assert kwargs["output_dir"] == cfg.output_dir


def test_no_key_is_ever_emitted_that_the_constructor_would_reject(cfg):
    for accepted in (V4_FIELDS, V5_FIELDS, {"output_dir", "seed"}):
        assert set(sft_config_kwargs(cfg, accepted)) <= accepted


def test_the_knobs_that_change_the_result_are_always_passed(cfg):
    """These are not stylistic. Dropping bf16 or optim silently changes memory.

    A 27B QLoRA run sized for 80 GB in bf16 with a paged 8-bit optimiser does
    not fit in fp32 with plain AdamW, so if either name ever moves we want a
    loud KeyError here rather than an out-of-memory crash on the node.
    """
    kwargs = sft_config_kwargs(cfg, V5_FIELDS)
    assert kwargs["bf16"] is True
    assert kwargs["optim"] == "paged_adamw_8bit"
    assert kwargs["gradient_checkpointing"] is True
    assert kwargs["per_device_train_batch_size"] == cfg.per_device_batch_size
    assert kwargs["gradient_accumulation_steps"] == cfg.grad_accumulation


def test_max_steps_sentinel_is_passed_through_untouched(cfg):
    """-1 is trl's own "no cap" value, unlike the CLI where we omit the flag."""
    assert sft_config_kwargs(cfg, V5_FIELDS)["max_steps"] == -1


def test_accepted_fields_reads_a_dataclass():
    import dataclasses

    @dataclasses.dataclass
    class Fake:
        a: int = 1
        b: str = "x"

    assert accepted_fields(Fake) == {"a", "b"}


def test_accepted_fields_falls_back_to_the_signature_for_a_plain_class():
    """trl has not always used a dataclass, and may not in future."""

    class Fake:
        def __init__(self, a, b=2, **kwargs):
            pass

    fields = accepted_fields(Fake)
    assert {"a", "b"} <= fields
    assert "self" not in fields
    assert "kwargs" not in fields
