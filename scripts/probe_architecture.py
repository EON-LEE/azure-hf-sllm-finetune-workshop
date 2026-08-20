"""Probe a model's real architecture without a GPU and without downloading weights.

Instantiates the model on the `meta` device from its config alone, then reports
the facts that actually decide a fine-tuning recipe:

* whether this `transformers` build even knows the architecture,
* the hybrid layer map (full attention vs linear/SSM), if any,
* the exact set of `nn.Linear` leaf names, so LoRA `target_modules` can be
  chosen from evidence instead of from the usual q/k/v/o_proj folklore --
  which silently under-adapts hybrid models,
* a measured NF4 / bf16 memory split, instead of a params x 0.5 guess,
* which chat-template knobs exist.

This is the script `configs/models.yaml` cites for the Qwen3.8-27B numbers.

    uv run python scripts/probe_architecture.py qwen3.8-27b
    uv run python scripts/probe_architecture.py --hf-id Qwen/Qwen3.5-9B
    uv run python scripts/probe_architecture.py qwen3.8-27b --check

`--check` compares the registry's declared `lora_target_modules` against what
the model actually exposes and exits non-zero on a mismatch, so it can gate CI.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict

# Modules that are never worth adapting or quantizing.
_SKIP_LEAVES = {"lm_head"}


def _load_registry_spec(key: str):
    from ffsft.models import get_model

    return get_model(key)


def _resolve(args) -> tuple[str, object | None]:
    if args.hf_id:
        return args.hf_id, None
    spec = _load_registry_spec(args.model)
    if not spec.hf_id:
        raise SystemExit(f"model '{args.model}' has no hf_id to probe")
    return spec.hf_id, spec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("model", nargs="?", help="registry key, e.g. qwen3.8-27b")
    ap.add_argument("--hf-id", help="probe a raw Hugging Face id instead")
    ap.add_argument(
        "--check",
        action="store_true",
        help="fail if the registry's lora_target_modules do not match reality",
    )
    ap.add_argument("--lora-rank", type=int, default=16)
    args = ap.parse_args()
    if not args.model and not args.hf_id:
        ap.error("give a registry key or --hf-id")

    hf_id, spec = _resolve(args)

    import torch
    from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer

    print(f"model            : {hf_id}")
    print(f"transformers     : {__import__('transformers').__version__}")

    cfg = AutoConfig.from_pretrained(hf_id)
    tcfg = getattr(cfg, "text_config", None) or cfg
    print(f"config class     : {type(cfg).__name__}")
    print(f"model_type       : {cfg.model_type}")
    print(f"architectures    : {getattr(cfg, 'architectures', None)}")
    print(f"multimodal       : {hasattr(cfg, 'vision_config')}")

    from transformers.models.auto import modeling_auto

    known = set(modeling_auto.MODEL_MAPPING_NAMES) | set(
        getattr(modeling_auto, "MODEL_FOR_CAUSAL_LM_MAPPING_NAMES", {})
    )
    registered = cfg.model_type in known
    print(f"natively supported: {registered}"
          f"{'' if registered else '  <-- needs trust_remote_code or a newer transformers'}")

    with torch.device("meta"):
        model = AutoModelForCausalLM.from_config(cfg)
    print(f"instantiated as  : {type(model).__name__}")

    layer_types = list(getattr(tcfg, "layer_types", []) or [])
    if layer_types:
        print(f"layer map        : {dict(Counter(layer_types))}")

    linear_mods = {
        name for name, m in model.named_modules() if type(m).__name__ == "Linear"
    }
    other_kinds = Counter(
        type(m).__name__
        for _, m in model.named_modules()
        if type(m).__name__ in {"Conv1d", "Embedding"}
    )

    total = linear_w = 0
    groups: dict[str, int] = defaultdict(int)
    for name, p in model.named_parameters():
        n = p.numel()
        total += n
        owner = name.rsplit(".", 1)[0]
        if owner in linear_mods and name.endswith("weight"):
            linear_w += n
            groups[owner.split(".")[-1]] += n
        elif "embed" in name:
            groups["<embedding>"] += n
        else:
            groups["<other>"] += n

    print(f"\ntotal params     : {total / 1e9:.2f} B")
    print(f"tie_word_embeddings: {getattr(tcfg, 'tie_word_embeddings', None)}")
    print(f"non-Linear modules : {dict(other_kinds)}")

    leaves = Counter(n.split(".")[-1] for n in linear_mods)
    targets = sorted(k for k in leaves if k not in _SKIP_LEAVES)
    covered = sum(v for k, v in leaves.items() if k in targets)

    print("\n== Linear leaf names (LoRA target candidates) ==")
    for name, count in leaves.most_common():
        mark = "  (skipped)" if name in _SKIP_LEAVES else ""
        print(f"  {name:<20} x{count:<5} {groups.get(name, 0) / 1e9:7.3f} B{mark}")

    naive = {"q_proj", "k_proj", "v_proj", "o_proj"}
    naive_hit = sum(v for k, v in leaves.items() if k in naive)
    n_linear = sum(leaves.values())
    print(f"\nconventional {sorted(naive)}")
    print(f"  covers {naive_hit}/{n_linear} Linear modules ({naive_hit / n_linear * 100:.0f}%)")
    if layer_types and len(set(layer_types)) > 1:
        missed = sum(v for k, v in Counter(layer_types).items() if k != "full_attention")
        print(f"  WARNING hybrid model: {missed} non-full-attention layers get NO adapter")
    print(f"evidence-based targets ({len(targets)}): {targets}")
    print(f"  covers {covered}/{n_linear} Linear modules ({covered / n_linear * 100:.0f}%)")

    # Memory model. bitsandbytes quantizes nn.Linear only, and transformers keeps
    # the output head in bf16, so lm_head and the embedding table are full width.
    quantizable = sum(
        v for k, v in groups.items() if k not in _SKIP_LEAVES and not k.startswith("<")
    )
    bf16_bytes = (total - quantizable) * 2
    nf4_bytes = quantizable * 0.53
    weights = (nf4_bytes + bf16_bytes) / 1e9
    adapters = args.lora_rank * 2 * covered * tcfg.hidden_size * 2 * 3 / 1e9
    print("\n== measured QLoRA memory ==")
    print(f"  NF4 Linear       : {nf4_bytes / 1e9:6.2f} GB")
    print(f"  bf16 remainder   : {bf16_bytes / 1e9:6.2f} GB  (embedding + lm_head + norms)")
    print(f"  LoRA r={args.lora_rank} + grads + AdamW: {adapters:6.2f} GB")
    print("  activations      :   4-8 GB (grad checkpointing, seq 1024-2048)")
    lo = weights + adapters + 4
    hi = weights + adapters + 8
    print(f"  PEAK             : {lo:6.2f} - {hi:.2f} GB")
    print(f"  -> smallest safe GPU: {'24 GB' if hi <= 24 else '40 GB' if hi <= 40 else '80 GB'}")

    try:
        tok = AutoTokenizer.from_pretrained(hf_id)
        tmpl = tok.chat_template or ""
        knobs = [k for k in ("enable_thinking", "reasoning_effort") if k in tmpl]
        print(f"\nchat template knobs: {knobs or 'none'}")
        sample = "한국어 토크나이저 효율 테스트입니다."
        ids = tok(sample)["input_ids"]
        print(f"korean efficiency  : {len(ids)} tokens / {len(sample)} chars "
              f"(vocab {tok.vocab_size:,})")
    except Exception as exc:  # noqa: BLE001
        print(f"\ntokenizer probe failed: {type(exc).__name__}: {exc}")

    if args.check:
        if spec is None:
            print("\n--check needs a registry key, not --hf-id")
            return 2
        declared = sorted(spec.lora_target_modules)
        if not declared:
            print(f"\nFAIL {spec.key} declares no lora_target_modules; "
                  f"PEFT defaults would cover {naive_hit}/{n_linear}")
            return 1
        if declared != targets:
            print(f"\nFAIL lora_target_modules mismatch for {spec.key}")
            print(f"  declared: {declared}")
            print(f"  actual  : {targets}")
            print(f"  missing : {sorted(set(targets) - set(declared))}")
            print(f"  extra   : {sorted(set(declared) - set(targets))}")
            return 1
        print(f"\nOK {spec.key} lora_target_modules match the real architecture")

    return 0


if __name__ == "__main__":
    sys.exit(main())
