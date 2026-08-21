"""Tests for the benchmark registry and the eval comparison logic.

The licence constraint is the important one here: these datasets are mostly
CC-BY-ND/NC, so `eval_only` must be impossible to turn off by editing YAML.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ffsft.eval import BenchmarkRegistry, BenchmarkSpec, get_benchmark, get_benchmark_registry
from ffsft.eval.run import build_model_args, compare, extract_scores, split_benchmarks

# -- registry -----------------------------------------------------------


def test_shipped_registry_loads():
    registry = get_benchmark_registry()
    assert len(registry) >= 8
    assert "kmmlu" in registry
    assert registry.default_suite.benchmarks


def test_every_benchmark_is_eval_only():
    assert all(spec.eval_only for spec in get_benchmark_registry())


def test_eval_only_cannot_be_disabled():
    with pytest.raises(ValidationError, match="eval_only by construction"):
        BenchmarkSpec(key="x", dataset_id="y", eval_only=False)


def test_judge_benchmarks_are_not_harness_runnable():
    for spec in get_benchmark_registry():
        if spec.judge_required:
            assert not spec.runnable_by_harness


def test_unknown_benchmark_key_rejected():
    with pytest.raises(KeyError, match="unknown benchmark"):
        get_benchmark("nope")


def test_suite_expansion():
    specs = get_benchmark_registry().suite("ko_fast")
    assert {s.key for s in specs} == {"kobest"}


def test_unknown_suite_rejected():
    with pytest.raises(KeyError, match="unknown suite"):
        get_benchmark_registry().suite("ko_nope")


def test_resolve_defaults_to_default_suite():
    registry = get_benchmark_registry()
    assert [s.key for s in registry.resolve(None)] == registry.default_suite.benchmarks


def test_resolve_mixes_suites_and_keys_without_duplicates():
    resolved = get_benchmark_registry().resolve(["ko_fast", "kobest", "kmmlu"])
    keys = [s.key for s in resolved]
    assert keys.count("kobest") == 1
    assert set(keys) == {"kobest", "kmmlu"}


def test_suite_referencing_unknown_benchmark_rejected():
    from ffsft.eval.registry import SuiteSpec

    spec = BenchmarkSpec(key="a", dataset_id="d")
    with pytest.raises(ValueError, match="unknown benchmarks"):
        BenchmarkRegistry([spec], [SuiteSpec(key="s", benchmarks=["a", "ghost"])])


# -- eval helpers -------------------------------------------------------


def test_split_benchmarks_separates_judge_tasks():
    """`others` is everything the harness cannot run, for either reason.

    `logickor` needs a judge LLM; `ifeval_ko` has no upstream lm-eval task at
    all, so it has no `harness_task` and lands here too.
    """
    specs = get_benchmark_registry().resolve(["ko_core"])
    tasks, others = split_benchmarks(specs)
    assert "kmmlu" in tasks
    assert {s.key for s in others} == {"logickor", "ifeval_ko"}


def test_build_model_args_includes_adapter_and_quantization():
    args = build_model_args(
        "Qwen/Qwen3.8-27B", "outputs/qlora",
        load_in_4bit=True, dtype="bfloat16", max_length=4096, batch_size="auto",
    )
    assert "pretrained=Qwen/Qwen3.8-27B" in args
    assert "peft=outputs/qlora" in args
    assert "load_in_4bit=True" in args
    assert "bnb_4bit_quant_type=nf4" in args


def test_build_model_args_omits_adapter_for_base_run():
    args = build_model_args(
        "Qwen/Qwen3.8-27B", None,
        load_in_4bit=False, dtype="bfloat16", max_length=2048, batch_size=4,
    )
    assert "peft=" not in args
    assert "load_in_4bit" not in args


def test_extract_scores_prefers_acc_norm():
    out = {"results": {"kmmlu": {"acc,none": 0.41, "acc_norm,none": 0.43, "alias": "kmmlu"}}}
    assert extract_scores(out) == {"kmmlu": 0.43}


def test_extract_scores_ignores_stderr():
    out = {"results": {"t": {"acc_stderr,none": 0.01, "acc,none": 0.5}}}
    assert extract_scores(out) == {"t": 0.5}


def test_extract_scores_falls_back_to_first_numeric():
    out = {"results": {"custom": {"alias": "custom", "weird_metric,none": 0.77}}}
    assert extract_scores(out) == {"custom": 0.77}


def test_compare_computes_delta_and_pct():
    rows = compare({"kmmlu": 0.40}, {"kmmlu": 0.44})
    assert rows == [
        {"task": "kmmlu", "base": 0.40, "tuned": 0.44, "delta": 0.04, "delta_pct": 10.0}
    ]


def test_compare_handles_missing_side():
    rows = compare({"a": 0.5}, {})
    assert rows[0]["delta"] is None
    assert rows[0]["delta_pct"] is None


def test_compare_survives_zero_base_score():
    rows = compare({"a": 0.0}, {"a": 0.1})
    assert rows[0]["delta"] == 0.1
    assert rows[0]["delta_pct"] is None
