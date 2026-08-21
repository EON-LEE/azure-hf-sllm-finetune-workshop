"""QLoRA supervised fine-tuning, driven entirely by the model registry.

Nothing here is specific to Qwen. The recipe reads a `ModelSpec` and derives the
quantization config, the LoRA target modules and the chat-template kwargs from
it, so pointing this at a different model is a `--model` flag, not a code edit.

The one thing this module refuses to do is guess LoRA target modules. For hybrid
attention models (Qwen3.5/3.6/3.8) PEFT's per-architecture default silently
adapts only the full-attention layers -- on Qwen3.8-27B that is 13% of the Linear
modules and 16 of 64 layers, and it trains without any error. So a spec that
declares no `lora_target_modules` is a hard failure unless the caller explicitly
opts into the default with --allow-default-lora-targets.

    ffsft-train --model qwen3.8-27b --mix ko_smoke --max-steps 20
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ffsft.models import ModelSpec

log = logging.getLogger("ffsft.train")


@dataclass
class QLoRAConfig:
    """Everything tunable about a QLoRA run, with conservative defaults.

    The defaults target the smallest GPU the registry says can hold the model,
    so they favour not running out of memory over running fast.
    """

    model_key: str = "qwen3.8-27b"
    mix: str = "ko_smoke"

    # LoRA
    rank: int = 16
    alpha: int = 32
    dropout: float = 0.05
    target_modules: list[str] = field(default_factory=list)

    # Sequence / batching. seq_len is the dominant activation-memory term.
    max_seq_length: int = 1024
    per_device_batch_size: int = 1
    grad_accumulation: int = 16
    gradient_checkpointing: bool = True

    # Optimisation
    learning_rate: float = 2e-4
    warmup_ratio: float = 0.03
    num_train_epochs: float = 1.0
    max_steps: int = -1
    logging_steps: int = 1
    save_steps: int = 200
    seed: int = 42

    output_dir: str = "outputs/qlora"
    max_samples: int | None = None
    bf16: bool = True

    @property
    def effective_batch_size(self) -> int:
        return self.per_device_batch_size * self.grad_accumulation


def build_quantization_config(cfg: QLoRAConfig):
    """NF4 double-quantized 4-bit, the standard QLoRA setup."""
    import torch
    from transformers import BitsAndBytesConfig

    return BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=torch.bfloat16 if cfg.bf16 else torch.float16,
    )


def resolve_target_modules(
    spec: ModelSpec, cfg: QLoRAConfig, allow_default: bool = False
) -> list[str] | str:
    """Pick LoRA target modules, refusing to silently under-adapt hybrid models."""
    if cfg.target_modules:
        return list(cfg.target_modules)
    if spec.lora_target_modules:
        return list(spec.lora_target_modules)
    if allow_default:
        log.warning(
            "%s declares no lora_target_modules; falling back to PEFT defaults. "
            "If this is a hybrid-attention model, most layers will NOT be adapted.",
            spec.key,
        )
        return "all-linear"
    raise ValueError(
        f"model '{spec.key}' declares no lora_target_modules.\n"
        f"Run `python scripts/probe_architecture.py {spec.key}` to discover the real "
        f"module names and add them to configs/models.yaml, or pass "
        f"--allow-default-lora-targets to accept PEFT's defaults."
    )


def load_model_and_tokenizer(spec: ModelSpec, cfg: QLoRAConfig):
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    log.info("loading %s in NF4 ...", spec.hf_id)
    model = AutoModelForCausalLM.from_pretrained(
        spec.hf_id,
        quantization_config=build_quantization_config(cfg),
        dtype=torch.bfloat16 if cfg.bf16 else torch.float16,
        device_map={"": 0},
        attn_implementation="sdpa",
    )
    model.config.use_cache = False

    tokenizer = AutoTokenizer.from_pretrained(spec.hf_id)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    return model, tokenizer


def apply_chat_template(tokenizer, spec: ModelSpec, messages: list[dict]) -> str:
    """Render one conversation using the spec's pinned template kwargs.

    This must be identical to what inference does. On Qwen3.8, enable_thinking=false
    does not remove the thinking block -- it emits an empty one -- so training on a
    differently-rendered string would teach the model the wrong surface form.
    """
    kwargs: dict[str, Any] = dict(spec.chat_template_kwargs)
    try:
        return tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=False, **kwargs
        )
    except TypeError:
        # Older templates reject unknown kwargs; drop them rather than crash.
        return tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=False
        )


def build_peft_config(spec: ModelSpec, cfg: QLoRAConfig, allow_default: bool = False):
    from peft import LoraConfig

    targets = resolve_target_modules(spec, cfg, allow_default)
    log.info(
        "LoRA r=%d alpha=%d over %s target modules",
        cfg.rank,
        cfg.alpha,
        len(targets) if isinstance(targets, list) else targets,
    )
    return LoraConfig(
        r=cfg.rank,
        lora_alpha=cfg.alpha,
        lora_dropout=cfg.dropout,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=targets,
    )


#: Constructor arguments that moved between library generations, as
#: `preferred -> fallback`. transformers v5 removed `warmup_ratio` in favour of
#: a `warmup_steps` that reads a float below 1 as a ratio, and trl renamed
#: `max_seq_length` to `max_length`. Both renames are pure: the value carries
#: over unchanged, so this is a lookup and not a conversion.
_SFT_ARG_FALLBACKS = {
    "warmup_ratio": "warmup_steps",
    "max_length": "max_seq_length",
}


def accepted_fields(cls: type) -> set[str]:
    """Every keyword `cls` will accept, whether it is a dataclass or not."""
    import dataclasses
    import inspect

    if dataclasses.is_dataclass(cls):
        return {f.name for f in dataclasses.fields(cls)}
    return {
        name
        for name, param in inspect.signature(cls.__init__).parameters.items()
        if name != "self"
        and param.kind
        not in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
    }


def sft_config_kwargs(cfg: QLoRAConfig, accepted: Collection[str]) -> dict[str, Any]:
    """Map the recipe's knobs onto the argument names this trl actually has.

    The image tracks bleeding-edge model libraries because Qwen3.8 requires
    them, and those libraries rename constructor arguments between releases.
    Passing a name that moved raises `TypeError` inside `SFTConfig.__init__`,
    which on Azure ML happens after the node, the image and 54 GB of weights
    have all been paid for. Resolving the name against the real class costs
    nothing and turns that into a warning.
    """
    accepted = set(accepted)
    desired: dict[str, Any] = {
        "output_dir": cfg.output_dir,
        "per_device_train_batch_size": cfg.per_device_batch_size,
        "gradient_accumulation_steps": cfg.grad_accumulation,
        "gradient_checkpointing": cfg.gradient_checkpointing,
        "gradient_checkpointing_kwargs": {"use_reentrant": False},
        "learning_rate": cfg.learning_rate,
        "warmup_ratio": cfg.warmup_ratio,
        "num_train_epochs": cfg.num_train_epochs,
        "max_steps": cfg.max_steps,
        "logging_steps": cfg.logging_steps,
        "save_steps": cfg.save_steps,
        "save_total_limit": 2,
        "bf16": cfg.bf16,
        "optim": "paged_adamw_8bit",
        "lr_scheduler_type": "cosine",
        "max_length": cfg.max_seq_length,
        "seed": cfg.seed,
        "report_to": [],
    }

    resolved: dict[str, Any] = {}
    for name, value in desired.items():
        if name in accepted:
            resolved[name] = value
            continue
        fallback = _SFT_ARG_FALLBACKS.get(name)
        if fallback and fallback in accepted:
            log.info("SFTConfig has no '%s'; using '%s' instead", name, fallback)
            resolved[fallback] = value
            continue
        log.warning(
            "SFTConfig accepts neither '%s' nor its known alternatives; "
            "dropping it and using the library default",
            name,
        )
    return resolved


def report_memory(tag: str) -> dict[str, float]:
    import torch

    if not torch.cuda.is_available():
        return {}
    stats = {
        "allocated_gb": torch.cuda.memory_allocated() / 1e9,
        "reserved_gb": torch.cuda.memory_reserved() / 1e9,
        "peak_gb": torch.cuda.max_memory_allocated() / 1e9,
        "total_gb": torch.cuda.get_device_properties(0).total_memory / 1e9,
    }
    log.info(
        "[%s] VRAM allocated %.2f GB | reserved %.2f GB | peak %.2f GB | card %.1f GB",
        tag, stats["allocated_gb"], stats["reserved_gb"],
        stats["peak_gb"], stats["total_gb"],
    )
    return stats


def train(cfg: QLoRAConfig, allow_default_targets: bool = False) -> dict:
    import torch
    from peft import prepare_model_for_kbit_training
    from trl import SFTConfig, SFTTrainer

    from ffsft.data.korean import load_sft_dataset
    from ffsft.models import get_model

    spec = get_model(cfg.model_key)
    if not spec.hf_id:
        raise ValueError(f"model '{spec.key}' has no hf_id; QLoRA needs open weights")

    started = time.time()
    model, tokenizer = load_model_and_tokenizer(spec, cfg)
    load_stats = report_memory("after load")

    model = prepare_model_for_kbit_training(
        model, use_gradient_checkpointing=cfg.gradient_checkpointing
    )
    peft_config = build_peft_config(spec, cfg, allow_default_targets)

    dataset = load_sft_dataset(
        mix=cfg.mix,
        tokenizer=tokenizer,
        spec=spec,
        max_samples=cfg.max_samples,
        render=lambda msgs: apply_chat_template(tokenizer, spec, msgs),
    )
    log.info("dataset: %d examples", len(dataset))

    sft_config = SFTConfig(**sft_config_kwargs(cfg, accepted_fields(SFTConfig)))

    trainer = SFTTrainer(
        model=model,
        args=sft_config,
        train_dataset=dataset,
        peft_config=peft_config,
    )

    trainable = sum(p.numel() for p in trainer.model.parameters() if p.requires_grad)
    total = sum(p.numel() for p in trainer.model.parameters())
    log.info(
        "trainable params: %.1f M / %.2f B (%.3f%%)",
        trainable / 1e6, total / 1e9, trainable / total * 100,
    )

    result = trainer.train()
    peak = report_memory("after train")

    trainer.save_model(cfg.output_dir)
    tokenizer.save_pretrained(cfg.output_dir)

    summary = {
        "model": spec.key,
        "hf_id": spec.hf_id,
        "mix": cfg.mix,
        "examples": len(dataset),
        "effective_batch_size": cfg.effective_batch_size,
        "max_seq_length": cfg.max_seq_length,
        "trainable_params_m": round(trainable / 1e6, 2),
        "trainable_pct": round(trainable / total * 100, 4),
        "train_loss": round(float(result.training_loss), 4),
        "steps": int(result.global_step),
        "wall_seconds": round(time.time() - started, 1),
        "vram_after_load_gb": round(load_stats.get("allocated_gb", 0.0), 2),
        "vram_peak_gb": round(peak.get("peak_gb", 0.0), 2),
        "vram_card_gb": round(peak.get("total_gb", 0.0), 1),
        "torch": torch.__version__,
    }
    os.makedirs(cfg.output_dir, exist_ok=True)
    with open(os.path.join(cfg.output_dir, "run_summary.json"), "w") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)
    log.info("summary: %s", json.dumps(summary, ensure_ascii=False))
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="QLoRA SFT driven by the model registry")
    ap.add_argument("--model", default="qwen3.8-27b")
    ap.add_argument("--mix", default="ko_smoke")
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--alpha", type=int, default=32)
    ap.add_argument("--max-seq-length", type=int, default=1024)
    ap.add_argument("--batch-size", type=int, default=1)
    ap.add_argument("--grad-accum", type=int, default=16)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--epochs", type=float, default=1.0)
    ap.add_argument("--max-steps", type=int, default=-1)
    ap.add_argument("--max-samples", type=int, default=None)
    ap.add_argument("--output-dir", default="outputs/qlora")
    ap.add_argument("--allow-default-lora-targets", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s | %(message)s"
    )

    cfg = QLoRAConfig(
        model_key=args.model,
        mix=args.mix,
        rank=args.rank,
        alpha=args.alpha,
        max_seq_length=args.max_seq_length,
        per_device_batch_size=args.batch_size,
        grad_accumulation=args.grad_accum,
        learning_rate=args.lr,
        num_train_epochs=args.epochs,
        max_steps=args.max_steps,
        max_samples=args.max_samples,
        output_dir=args.output_dir,
    )
    train(cfg, allow_default_targets=args.allow_default_lora_targets)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
