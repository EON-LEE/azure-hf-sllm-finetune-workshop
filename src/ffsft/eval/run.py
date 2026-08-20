"""Run Korean benchmarks against a base model and its fine-tuned adapter.

The only number worth reporting from a fine-tune is a *delta*. An absolute
KMMLU score says almost nothing -- it is dominated by the base model, so a good
base with a broken adapter still looks fine. So the runner's unit of work is a
pair: identical harness, identical few-shot setting, identical chat template,
base vs tuned, and the report is the difference.

Two scoring backends, picked per benchmark by the registry:

* **lm-evaluation-harness** for anything with a `harness_task` (KMMLU, HAE-RAE,
  KoBEST, IFEval-Ko). Log-likelihood scoring for multiple choice, and IFEval's
  programmatic constraint checks. Deterministic and cheap.
* **LLM-as-judge** for open-ended generation (LogicKor). See `judge.py`.

Runs as an ordinary AML command job on the LowPriority cluster, so evaluation
costs the same as training and needs no dedicated quota.

    python -m ffsft.eval.run --model qwen3.8-27b --adapter outputs/qlora \\
        --suite ko_fast --limit 200
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time

from .registry import BenchmarkSpec, get_benchmark_registry

log = logging.getLogger("ffsft.eval")


def build_model_args(
    hf_id: str,
    adapter: str | None,
    *,
    load_in_4bit: bool,
    dtype: str,
    max_length: int,
    batch_size: str | int,
) -> str:
    """Build lm-eval's `--model_args` string.

    Evaluating the tuned model in the *same* 4-bit quantization it was trained in
    is deliberate: a bf16 evaluation of a QLoRA adapter measures a configuration
    that will never be served, and the quantization error is on the same order as
    the fine-tuning delta we are trying to detect.
    """
    parts = [f"pretrained={hf_id}", f"dtype={dtype}", f"max_length={max_length}"]
    if load_in_4bit:
        parts += ["load_in_4bit=True", "bnb_4bit_quant_type=nf4", "bnb_4bit_use_double_quant=True"]
    if adapter:
        parts.append(f"peft={adapter}")
    parts.append(f"batch_size={batch_size}")
    return ",".join(parts)


def run_harness(
    hf_id: str,
    tasks: list[str],
    adapter: str | None = None,
    *,
    num_fewshot: int | None = None,
    limit: int | None = None,
    load_in_4bit: bool = True,
    dtype: str = "bfloat16",
    max_length: int = 4096,
    batch_size: str | int = "auto",
) -> dict:
    """Invoke lm-evaluation-harness in-process and return its results dict."""
    import torch
    from lm_eval import simple_evaluate
    from lm_eval.models.huggingface import HFLM

    model_args = build_model_args(
        hf_id, adapter,
        load_in_4bit=load_in_4bit, dtype=dtype, max_length=max_length, batch_size=batch_size,
    )
    log.info("lm-eval | tasks=%s | %s", ",".join(tasks), model_args)

    lm = HFLM(pretrained=hf_id, peft=adapter, dtype=dtype, max_length=max_length,
              batch_size=batch_size, load_in_4bit=load_in_4bit)
    out = simple_evaluate(model=lm, tasks=tasks, num_fewshot=num_fewshot, limit=limit)

    # Free the weights before the next model loads, or the pair evaluation OOMs
    # on any card smaller than 2x the model.
    del lm
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    return out or {}


def extract_scores(harness_out: dict) -> dict[str, float]:
    """Pull one headline number per task out of lm-eval's nested results."""
    scores: dict[str, float] = {}
    for task, metrics in (harness_out.get("results") or {}).items():
        for name in ("acc_norm,none", "acc,none", "exact_match,none",
                     "prompt_level_strict_acc,none", "inst_level_strict_acc,none"):
            if name in metrics and isinstance(metrics[name], (int, float)):
                scores[task] = round(float(metrics[name]), 4)
                break
        else:
            numeric = [
                (k, v) for k, v in metrics.items()
                if isinstance(v, (int, float)) and not k.endswith("_stderr,none") and k != "alias"
            ]
            if numeric:
                scores[task] = round(float(numeric[0][1]), 4)
    return scores


def compare(base: dict[str, float], tuned: dict[str, float]) -> list[dict]:
    """Join base and tuned scores into a per-task delta table."""
    rows = []
    for task in sorted(set(base) | set(tuned)):
        b, t = base.get(task), tuned.get(task)
        delta = round(t - b, 4) if (b is not None and t is not None) else None
        rows.append({
            "task": task,
            "base": b,
            "tuned": t,
            "delta": delta,
            "delta_pct": round(delta / b * 100, 2) if (delta is not None and b) else None,
        })
    return rows


def format_table(rows: list[dict]) -> str:
    head = f"{'task':<28} {'base':>9} {'tuned':>9} {'delta':>9} {'delta %':>9}"
    lines = [head, "-" * len(head)]
    for r in rows:
        def fmt(v, w=9, p=4):
            return f"{v:>{w}.{p}f}" if isinstance(v, (int, float)) else f"{'-':>{w}}"
        lines.append(
            f"{r['task']:<28} {fmt(r['base'])} {fmt(r['tuned'])} "
            f"{fmt(r['delta'])} {fmt(r['delta_pct'], 9, 2)}"
        )
    return "\n".join(lines)


def split_benchmarks(specs: list[BenchmarkSpec]) -> tuple[list[str], list[BenchmarkSpec]]:
    """Separate harness-runnable tasks from ones needing a judge or a custom runner."""
    tasks, others = [], []
    for spec in specs:
        if spec.runnable_by_harness:
            tasks.append(spec.harness_task)
        else:
            others.append(spec)
    return tasks, others


def evaluate(
    model_key: str,
    adapter: str | None,
    benchmarks: list[str] | None,
    *,
    limit: int | None = None,
    num_fewshot: int | None = None,
    skip_base: bool = False,
    load_in_4bit: bool = True,
    max_length: int = 4096,
    batch_size: str | int = "auto",
    output_dir: str = "outputs/eval",
) -> dict:
    """Evaluate base and tuned, and report the delta."""
    from ffsft.models import get_model

    spec = get_model(model_key)
    if not spec.hf_id:
        raise ValueError(f"model '{spec.key}' has no hf_id; nothing to evaluate")

    registry = get_benchmark_registry()
    selected = registry.resolve(benchmarks)
    tasks, needs_judge = split_benchmarks(selected)

    if needs_judge:
        log.warning(
            "these benchmarks need a judge LLM and are skipped by this runner: %s. "
            "Run `python -m ffsft.eval.judge` against a served endpoint instead.",
            ", ".join(b.key for b in needs_judge),
        )
    if not tasks:
        raise ValueError("no harness-runnable benchmarks selected")

    started = time.time()
    common = dict(
        num_fewshot=num_fewshot, limit=limit, load_in_4bit=load_in_4bit,
        max_length=max_length, batch_size=batch_size,
    )

    base_scores: dict[str, float] = {}
    if not skip_base:
        log.info("=== BASE: %s ===", spec.hf_id)
        base_scores = extract_scores(run_harness(spec.hf_id, tasks, None, **common))

    tuned_scores: dict[str, float] = {}
    if adapter:
        log.info("=== TUNED: %s + %s ===", spec.hf_id, adapter)
        tuned_scores = extract_scores(run_harness(spec.hf_id, tasks, adapter, **common))

    rows = compare(base_scores, tuned_scores)
    report = {
        "model": spec.key,
        "hf_id": spec.hf_id,
        "adapter": adapter,
        "benchmarks": [b.key for b in selected],
        "harness_tasks": tasks,
        "skipped_need_judge": [b.key for b in needs_judge],
        "limit": limit,
        "num_fewshot": num_fewshot,
        "load_in_4bit": load_in_4bit,
        "base": base_scores,
        "tuned": tuned_scores,
        "comparison": rows,
        "wall_seconds": round(time.time() - started, 1),
    }

    os.makedirs(output_dir, exist_ok=True)
    with open(os.path.join(output_dir, "eval_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    publish(report)
    return report


def publish(report: dict) -> None:
    """Mirror the scores into MLflow.

    Same reason as the preflight report: this workspace's blob storage is
    network-isolated, so job outputs written to disk cannot be read back. The
    MLflow tracking service is a separate endpoint and is reachable.
    """
    try:
        import mlflow
    except ImportError:
        log.info("mlflow not installed; skipping publish")
        return
    try:
        for row in report.get("comparison", []):
            task = row["task"].replace("/", "_")
            for field in ("base", "tuned", "delta"):
                if isinstance(row.get(field), (int, float)):
                    mlflow.log_metric(f"eval.{task}.{field}", float(row[field]))
        mlflow.set_tag("eval.model", report.get("model", ""))
        mlflow.set_tag("eval.adapter", str(report.get("adapter")))
        mlflow.set_tag("eval.benchmarks", ",".join(report.get("benchmarks", [])))
    except Exception as exc:  # noqa: BLE001 - reporting must never fail the job
        log.warning("mlflow publish failed: %s", exc)


def main() -> int:
    ap = argparse.ArgumentParser(description="Korean benchmark evaluation, base vs tuned")
    ap.add_argument("--model", default="qwen3.8-27b")
    ap.add_argument("--adapter", default=None, help="Adapter dir. Omit to score the base only.")
    ap.add_argument(
        "--suite", action="append", default=None,
        help="Suite or benchmark key; repeatable. Defaults to the registry's default suite.",
    )
    ap.add_argument(
        "--limit", type=int, default=None, help="Examples per task. Use for smoke runs."
    )
    ap.add_argument("--num-fewshot", type=int, default=None)
    ap.add_argument("--max-length", type=int, default=4096)
    ap.add_argument("--batch-size", default="auto")
    ap.add_argument("--skip-base", action="store_true", help="Skip the base pass (halves runtime).")
    ap.add_argument("--no-4bit", action="store_true", help="Evaluate in bf16 instead of NF4.")
    ap.add_argument("--output-dir", default="outputs/eval")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s | %(message)s"
    )
    report = evaluate(
        args.model, args.adapter, args.suite,
        limit=args.limit, num_fewshot=args.num_fewshot, skip_base=args.skip_base,
        load_in_4bit=not args.no_4bit, max_length=args.max_length,
        batch_size=args.batch_size, output_dir=args.output_dir,
    )
    print()
    print(format_table(report["comparison"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
