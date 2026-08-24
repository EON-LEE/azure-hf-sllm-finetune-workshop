"""Turn a finished QLoRA run into something an inference engine can serve.

A PEFT adapter directory is not a servable model. It is a few tens of MB of
low-rank deltas plus a pointer to the base repo, and vLLM/TGI will not read the
4-bit base the trainer used. There are exactly two ways forward, and the serving
registry names both (`adapter_modes` in configs/serving.yaml):

**merged** -- what this module does. Load the base in bf16 (*not* 4-bit: merging
into dequantized-then-requantized weights silently loses most of the adapter's
effect, because the NF4 round trip is lossy and the LoRA delta is small relative
to that error), apply the adapter, `merge_and_unload()`, save an ordinary HF
checkpoint. Self-contained, engine-agnostic, zero inference overhead.

**runtime_adapter** -- keep the adapter as a file and let vLLM load it beside a
shared base with `--enable-lora`. No merge step, many adapters per GPU, but the
engine has to know every module the adapter targets. For a hybrid-attention model
like Qwen3.8 that is an open risk, which is why this module exists as the safe
default.

Merging a 27B model needs ~54 GB of host RAM (or GPU) for bf16 weights plus room
for the output, so it is a job, not a laptop task -- run it on the same
LowPriority cluster that did the training.

    python -m ffsft.deploy.merge --model qwen3.8-27b \\
        --adapter outputs/qlora --output outputs/merged
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time

log = logging.getLogger("ffsft.deploy.merge")


def _read_adapter_targets(adapter_dir: str) -> list[str]:
    """Read `target_modules` back out of the adapter config.

    Used to fail loudly when an adapter was trained with PEFT's defaults against
    a hybrid model -- the exact silent-under-adaptation bug the training script
    refuses to allow. If it happened anyway (adapter trained elsewhere), the
    merge is the last place to catch it.
    """
    cfg_path = os.path.join(adapter_dir, "adapter_config.json")
    if not os.path.isfile(cfg_path):
        return []
    with open(cfg_path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    targets = cfg.get("target_modules") or []
    return sorted(targets) if isinstance(targets, list) else [str(targets)]


def merge_adapter(
    model_key: str,
    adapter_dir: str,
    output_dir: str,
    *,
    dtype: str = "bfloat16",
    device_map: str = "auto",
    max_shard_size: str = "4GB",
    push_to_hub: str | None = None,
    private: bool = True,
) -> dict:
    """Merge `adapter_dir` into its base and write a servable HF checkpoint.

    `push_to_hub` is not a convenience here. This workspace's blob storage is
    network-isolated by policy, so pushing straight to the Hub is currently the
    only way a merged checkpoint leaves the training node at all.
    """
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    from ffsft.models import get_model

    spec = get_model(model_key)
    if not spec.hf_id:
        raise ValueError(f"model '{spec.key}' has no hf_id; nothing to merge into")

    declared = spec.lora_target_modules
    found = _read_adapter_targets(adapter_dir)
    if declared and found and set(found) != set(declared):
        log.warning(
            "adapter target_modules %s do not match the registry's %s for %s. "
            "Merging anyway, but the adapter may not cover every layer.",
            found, sorted(declared), spec.key,
        )
    log.info("adapter targets %d modules: %s", len(found), ", ".join(found[:12]))

    torch_dtype = getattr(torch, dtype)
    started = time.time()

    log.info(
        "loading base %s in %s (merging into NF4 would be lossy)", spec.hf_id, dtype
    )
    base = AutoModelForCausalLM.from_pretrained(
        spec.hf_id,
        dtype=torch_dtype,
        device_map=device_map,
        low_cpu_mem_usage=True,
        trust_remote_code=spec.trust_remote_code,
    )

    log.info("applying adapter from %s", adapter_dir)
    model = PeftModel.from_pretrained(base, adapter_dir, dtype=torch_dtype)

    log.info("merging ...")
    model = model.merge_and_unload()
    model.config.use_cache = True

    os.makedirs(output_dir, exist_ok=True)
    log.info("saving merged checkpoint to %s", output_dir)
    model.save_pretrained(output_dir, safe_serialization=True, max_shard_size=max_shard_size)

    # The tokenizer must come from the *adapter* dir when training saved one, so
    # any added special tokens survive; fall back to the base otherwise.
    tok_src = (
        adapter_dir
        if os.path.isfile(os.path.join(adapter_dir, "tokenizer_config.json"))
        else spec.hf_id
    )
    tokenizer = AutoTokenizer.from_pretrained(
        tok_src, trust_remote_code=spec.trust_remote_code
    )
    tokenizer.save_pretrained(output_dir)
    log.info("tokenizer taken from %s", tok_src)

    size_gb = sum(
        os.path.getsize(os.path.join(output_dir, f))
        for f in os.listdir(output_dir)
        if f.endswith(".safetensors")
    ) / 1e9

    summary = {
        "model": spec.key,
        "base_hf_id": spec.hf_id,
        "adapter_dir": adapter_dir,
        "output_dir": output_dir,
        "adapter_target_modules": found,
        "dtype": dtype,
        "merged_size_gb": round(size_gb, 2),
        "wall_seconds": round(time.time() - started, 1),
        "pushed_to": None,
    }

    if push_to_hub:
        log.info("pushing merged model to https://huggingface.co/%s", push_to_hub)
        model.push_to_hub(push_to_hub, private=private, max_shard_size=max_shard_size)
        tokenizer.push_to_hub(push_to_hub, private=private)
        summary["pushed_to"] = push_to_hub

    with open(os.path.join(output_dir, "merge_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)
    log.info("summary: %s", json.dumps(summary, ensure_ascii=False))
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Merge a LoRA adapter into its base model")
    ap.add_argument("--model", default="qwen3.8-27b", help="Registry key of the base model.")
    ap.add_argument("--adapter", required=True, help="Directory produced by the trainer.")
    ap.add_argument("--output", default="outputs/merged")
    ap.add_argument("--dtype", default="bfloat16", choices=["bfloat16", "float16", "float32"])
    ap.add_argument(
        "--device-map",
        default="auto",
        help="'auto' uses GPU when present. Use 'cpu' to merge without a GPU (slow, needs RAM).",
    )
    ap.add_argument("--max-shard-size", default="4GB")
    ap.add_argument(
        "--push-to-hub",
        default=None,
        help="Repo id, e.g. me/qwen3.8-27b-ko. Currently the only way out of the "
        "network-isolated workspace.",
    )
    ap.add_argument(
        "--public", action="store_true", help="Push as a public repo (default private)."
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s | %(message)s"
    )
    merge_adapter(
        args.model,
        args.adapter,
        args.output,
        dtype=args.dtype,
        device_map=args.device_map,
        max_shard_size=args.max_shard_size,
        push_to_hub=args.push_to_hub,
        private=not args.public,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
