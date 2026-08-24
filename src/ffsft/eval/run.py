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


def model_load_kwargs(
    *, load_in_4bit: bool, dtype: str, trust_remote_code: bool = False
) -> dict:
    """Everything `AutoModelForCausalLM.from_pretrained` needs, as primitives.

    The model is built here rather than by lm-eval because lm-eval's loader
    has no supported route to on-the-fly bitsandbytes quantization. `HFLM`
    does accept a `quantization_config`, but that name is reserved for the one
    it reads off the *checkpoint's own* config (for repos that ship
    pre-quantized) and forwards itself -- passing our own collides:

        TypeError: HFLM._create_model() got multiple values for keyword
                   argument 'quantization_config'

    and the older spelling, `load_in_4bit`, is a transformers v4 shim that v5
    deleted, so it falls through into the model constructor and raises there
    instead. Both were real job failures (`ashy_hamster_9lvxsm0y2s`,
    `hungry_bird_hlyr5cwzl8`).

    Placement and attention kernel mirror `train/qlora.py` exactly. That pair
    is what ran 27B at a 28.19 GB peak on `olden_bean_302vkc7nbz`; evaluating
    through a different one would measure a model that was never trained.

    The quantization value stays a plain dict so the mapping is testable
    without transformers, which lives in the training image rather than the
    dev environment. `load_for_eval` materialises it.

    Evaluating the tuned model in the *same* 4-bit quantization it was trained
    in is deliberate: a bf16 evaluation of a QLoRA adapter measures a
    configuration that will never be served, and the quantization error is on
    the same order as the fine-tuning delta we are trying to detect.

    `trust_remote_code` has to match what training used. A model whose
    architecture only exists inside its own repo cannot be scored without it,
    and scoring a differently-loaded model is not scoring the trained one.
    """
    kwargs: dict = {
        "dtype": dtype,
        "device_map": {"": 0},
        "attn_implementation": "sdpa",
        "trust_remote_code": trust_remote_code,
    }
    if load_in_4bit:
        kwargs["quantization_config"] = {
            "load_in_4bit": True,
            "bnb_4bit_quant_type": "nf4",
            "bnb_4bit_use_double_quant": True,
            "bnb_4bit_compute_dtype": dtype,
        }
    return kwargs


def harness_kwargs(*, max_length: int, batch_size: str | int) -> dict:
    """Everything `HFLM(...)` needs once the model and tokenizer already exist.

    Deliberately names neither a checkpoint nor an adapter. `HFLM.__init__`
    only calls `_create_model` when `pretrained` is a `str`; handed an object
    it just assigns `self._model` and reads `self._config` off it. Keeping a
    repo id out of here is what holds that branch, and with it the whole class
    of loader-signature breakage.

    `backend` is pinned rather than inferred: left at `default`, HFLM guesses
    causal vs seq2seq from the architecture name, and it would be guessing
    about a `PeftModel` wrapping a checkpoint whose class is a
    ConditionalGeneration.
    """
    return {"backend": "causal", "max_length": max_length, "batch_size": batch_size}


def describe_model_args(kwargs: dict) -> str:
    """Render kwargs the way lm-eval's `--model_args` string reads.

    Derived from the kwargs rather than built alongside them. The original
    code logged a string advertising `bnb_4bit_quant_type=nf4` while
    constructing HFLM with a bare `load_in_4bit=True`, so the log described a
    configuration that was not the one running.
    """
    parts = []
    for key, value in kwargs.items():
        if isinstance(value, dict):
            parts += [f"{k}={v}" for k, v in value.items()]
        else:
            parts.append(f"{key}={value}")
    return ",".join(parts)


def unknown_harness_tasks(tasks: list[str], known: set[str] | None = None) -> list[str]:
    """Which of `tasks` lm-eval has never heard of.

    Called before any weights load. A misspelled task only fails inside
    `simple_evaluate`, which runs *after* the model is downloaded and
    quantised -- on a 27B model that is several minutes of A100 spent to learn
    that a string in a YAML file was wrong. Every offender is returned at once
    so one round trip names them all.

    `known` is injectable so the mapping can be tested without lm-eval, which
    lives in the training image rather than the dev environment.
    """
    if known is None:
        from lm_eval.tasks import TaskManager

        known = set(TaskManager().all_tasks)
    return [t for t in tasks if t not in known]


def build_model_args(
    hf_id: str,
    adapter: str | None,
    *,
    load_in_4bit: bool,
    dtype: str,
    max_length: int,
    batch_size: str | int,
    trust_remote_code: bool = False,
) -> str:
    """One log line describing the model actually being scored."""
    described: dict = {"pretrained": hf_id}
    if adapter:
        described["peft"] = adapter
    described.update(
        model_load_kwargs(
            load_in_4bit=load_in_4bit, dtype=dtype, trust_remote_code=trust_remote_code
        )
    )
    described.update(harness_kwargs(max_length=max_length, batch_size=batch_size))
    return describe_model_args(described)


def load_for_eval(
    hf_id: str,
    adapter: str | None,
    *,
    load_in_4bit: bool,
    dtype: str,
    trust_remote_code: bool = False,
):
    """Build the model and tokenizer to hand HFLM, adapter already applied.

    `PeftModel.from_pretrained` is used directly instead of lm-eval's `peft=`
    argument, because that argument is only honoured inside `_create_model` --
    the code path being avoided.
    """
    import torch
    import transformers

    kwargs = model_load_kwargs(
        load_in_4bit=load_in_4bit, dtype=dtype, trust_remote_code=trust_remote_code
    )
    # The dicts carry dtype *names* so the mapping stays importable without
    # torch; only here is there a torch to resolve them against.
    kwargs["dtype"] = getattr(torch, kwargs["dtype"])
    quant = kwargs.get("quantization_config")
    if quant is not None:
        quant = dict(quant)
        quant["bnb_4bit_compute_dtype"] = getattr(torch, quant["bnb_4bit_compute_dtype"])
        kwargs["quantization_config"] = transformers.BitsAndBytesConfig(**quant)

    model = transformers.AutoModelForCausalLM.from_pretrained(hf_id, **kwargs)
    if adapter:
        from peft import PeftModel

        model = PeftModel.from_pretrained(model, adapter)
    model.eval()
    tokenizer = transformers.AutoTokenizer.from_pretrained(
        hf_id, trust_remote_code=trust_remote_code
    )
    return model, tokenizer


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
    trust_remote_code: bool = False,
) -> dict:
    """Invoke lm-evaluation-harness in-process and return its results dict."""
    import torch
    from lm_eval import simple_evaluate
    from lm_eval.models.huggingface import HFLM

    log.info(
        "lm-eval | tasks=%s | %s",
        ",".join(tasks),
        build_model_args(
            hf_id, adapter,
            load_in_4bit=load_in_4bit, dtype=dtype,
            max_length=max_length, batch_size=batch_size,
            trust_remote_code=trust_remote_code,
        ),
    )

    model, tokenizer = load_for_eval(
        hf_id, adapter, load_in_4bit=load_in_4bit, dtype=dtype,
        trust_remote_code=trust_remote_code,
    )
    lm = HFLM(
        pretrained=model,
        tokenizer=tokenizer,
        **harness_kwargs(max_length=max_length, batch_size=batch_size),
    )
    out = simple_evaluate(model=lm, tasks=tasks, num_fewshot=num_fewshot, limit=limit)

    # Free the weights before the next model loads, or the pair evaluation OOMs
    # on any card smaller than 2x the model.
    del lm, model
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
            "skipped, not runnable by the harness: %s",
            ", ".join(
                f"{b.key} (needs a judge LLM -- run `python -m ffsft.eval.judge` "
                f"against a served endpoint)"
                if b.judge_required
                else f"{b.key} (no upstream lm-eval task; needs a custom task YAML)"
                for b in needs_judge
            ),
        )
    if not tasks:
        raise ValueError("no harness-runnable benchmarks selected")

    # Before the download, not after it.
    missing = unknown_harness_tasks(tasks)
    if missing:
        raise ValueError(
            f"lm-eval does not define these tasks: {', '.join(missing)}. "
            f"Fix `harness_task` in configs/benchmarks.yaml -- the Korean groups "
            f"the harness actually ships are kobest, kmmlu and haerae. A benchmark "
            f"with no upstream task needs a custom task YAML; leave its "
            f"`harness_task` unset until then."
        )

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
