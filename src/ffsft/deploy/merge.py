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
from typing import Any

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


#: What `save_pretrained` writes for a Qwen3.5/3.8 text model, and what vLLM
#: expects to read. They disagree.
#:
#: `AutoModelForCausalLM` resolves the multimodal checkpoint to its text-only
#: class and drops the vision tower -- the merge really is text-only, 850
#: tensors with no `visual.*` among them (job silly_ocean_n4k5szy7gj). But the
#: module tree it saves is still rooted at `model.language_model.`, because that
#: is where transformers keeps the decoder inside the multimodal wrapper. The
#: config it writes alongside says `Qwen3_5ForCausalLM` / `qwen3_5_text`, and
#: vLLM believes it: it builds `Qwen3_5Model` with `layers.*` directly, strips
#: the leading `model.` off each incoming key, and looks for a `language_model`
#: submodule that its own tree does not have:
#:
#:   ValueError: There is no module or parameter named 'language_model' in
#:   Qwen3_5Model
#:
#: -- jobs purple_wolf_g3hhc4q5qj and brave_bone_2kbcknyrgr, verbatim, five
#: hours apart, and `nifty_neck_d3b9x8z5x9` a third time *after* this rename was
#: in place.
#:
#: That third failure is why the constant below is not the fix and is not
#: described as one any more. `from_pretrained` records the checkpoint->runtime
#: renaming it applied on the way in, and `save_pretrained` plays it back in
#: reverse on the way out -- `modeling_utils.py:3497`:
#:
#:   if save_original_format and not is_offloaded and not _hf_peft_config_loaded:
#:       state_dict = revert_weight_conversion(model_to_save, state_dict)
#:
#: So whatever names the caller hands to `state_dict=` are re-mapped back to the
#: checkpoint's own convention before a byte is written, and the runtime names
#: were already `model.layers.*` -- this rename matched nothing and would have
#: been undone if it had. `save_original_format=False` is the switch that
#: decides the names on disk; see `merge_adapter`.
TEXT_PREFIX_FIX = ("model.language_model.", "model.")

#: The substring whose presence in a saved tensor name means vLLM will refuse
#: the checkpoint -- given a config that declares no vision tower. Checked
#: against the written files by `assert_servable_names`, because what the caller
#: asked `save_pretrained` for and what it wrote are not the same fact.
UNSERVABLE_FRAGMENT = "language_model."


def text_only_state_dict(model: Any) -> dict[str, Any]:
    """`model.state_dict()` with the multimodal wrapper's prefix removed.

    Kept as a belt-and-braces for the case where a model really does expose the
    wrapper prefix at runtime, and as the place the tensor count gets logged.
    It is **not** what makes the saved checkpoint servable -- see the note on
    `TEXT_PREFIX_FIX` and `save_original_format=False` in `merge_adapter`. On
    Qwen3.8-27B it renames zero of 850 tensors, which is the correct answer and
    was mistaken for the fix working.

    A no-op for a model that never had the prefix, so it is safe to apply to
    every architecture rather than special-casing Qwen: `str.startswith` on a
    key that does not start that way returns the key.
    """
    old, new = TEXT_PREFIX_FIX
    state = model.state_dict()
    renamed = {
        (new + key[len(old) :] if key.startswith(old) else key): value
        for key, value in state.items()
    }
    moved = sum(1 for key in state if key.startswith(old))
    log.info("state dict: %d tensors, %d renamed %s -> %s", len(state), moved, old, new)
    return renamed


def saved_tensor_names(output_dir: str) -> list[str]:
    """Every tensor name in the checkpoint on disk, without loading it.

    A sharded save writes `model.safetensors.index.json`, whose `weight_map` is
    already the full list. A single-file save has no index, so the names come
    out of the safetensors header itself: eight little-endian bytes of length,
    then that many bytes of JSON. Either way this reads kilobytes -- loading the
    tensors to ask the same question would read 54 GB.
    """
    index = os.path.join(output_dir, "model.safetensors.index.json")
    if os.path.isfile(index):
        with open(index, encoding="utf-8") as fh:
            return sorted(json.load(fh).get("weight_map") or {})

    names: list[str] = []
    for shard in sorted(f for f in os.listdir(output_dir) if f.endswith(".safetensors")):
        with open(os.path.join(output_dir, shard), "rb") as fh:
            header = json.loads(fh.read(int.from_bytes(fh.read(8), "little")))
        names.extend(key for key in header if key != "__metadata__")
    return sorted(names)


def assert_servable_names(output_dir: str) -> int:
    """Refuse to publish a checkpoint whose tensor names contradict its config.

    The contradiction this catches is the only one that has ever occurred here,
    and it has occurred three times: a config that says `Qwen3_5ForCausalLM` /
    `qwen3_5_text` with no `vision_config`, over tensors still named
    `model.language_model.*`. vLLM believes the config, builds `Qwen3_5Model`
    with `layers.*` directly, and dies in `load_weights`.

    Checking here rather than trusting `save_original_format=False` is the point.
    That argument reaches `save_pretrained` through `**kwargs` on some versions,
    where an unrecognised name is ignored rather than rejected -- so "I passed
    the flag" and "the names on disk changed" are two independent facts. The
    cost of not separating them, measured: `nifty_neck_d3b9x8z5x9` spent 41
    minutes allocating an A100, pulling a 9 GB image and downloading 54 GB of
    weights to report the same ValueError as the run before it. This check reads
    an index file at the end of the merge job and costs milliseconds.

    Mirrors the guard in `docker/serve_entrypoint.sh:88`, which asks the same
    question of the same `config.json` at the other end of the handoff.
    """
    config_path = os.path.join(output_dir, "config.json")
    multimodal = False
    if os.path.isfile(config_path):
        with open(config_path, encoding="utf-8") as fh:
            multimodal = "vision_config" in json.load(fh)

    names = saved_tensor_names(output_dir)
    offenders = [name for name in names if UNSERVABLE_FRAGMENT in name]

    if offenders and not multimodal:
        raise RuntimeError(
            f"{len(offenders)} of {len(names)} tensors in {output_dir} are named "
            f"under {UNSERVABLE_FRAGMENT!r} (e.g. {offenders[0]}), but the config "
            f"written beside them declares no vision_config. vLLM reads the config, "
            f"builds the text-only module tree and cannot find that submodule -- it "
            f"fails with \"There is no module or parameter named 'language_model' in "
            f"Qwen3_5Model\". save_pretrained reverses the renaming from_pretrained "
            f"applied unless save_original_format=False reaches it; that this check "
            f"fired means it did not."
        )

    log.info(
        "checkpoint names verified: %d tensors, %d under %r, config multimodal=%s",
        len(names), len(offenders), UNSERVABLE_FRAGMENT, multimodal,
    )
    return len(names)


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
    model.save_pretrained(
        output_dir,
        safe_serialization=True,
        max_shard_size=max_shard_size,
        state_dict=text_only_state_dict(model),
        # The argument that decides the names on disk. `from_pretrained` records
        # the checkpoint->runtime renaming it applied, and `save_pretrained`
        # replays it in reverse unless this is False -- so the multimodal
        # `model.language_model.*` prefix is written back over a config that
        # declares no vision tower, and vLLM cannot load the result. Passing a
        # renamed `state_dict=` does not help: the reversal happens after.
        save_original_format=False,
    )
    assert_servable_names(output_dir)

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
        # The architecture the merge actually wrote, which is not always the one
        # the base declared: `AutoModelForCausalLM` resolves a multimodal
        # checkpoint to its text-only class, so Qwen3.8-27B goes in as
        # `Qwen3_5ForConditionalGeneration` and comes out as
        # `Qwen3_5ForCausalLM`. vLLM has to have that name registered or the
        # server exits at startup, so it is the one field worth checking before
        # paying for a rollout.
        "architectures": list(getattr(model.config, "architectures", None) or []),
        "model_type": str(getattr(model.config, "model_type", "")),
        "files": len([f for f in os.listdir(output_dir) if f.endswith(".safetensors")]),
    }

    if push_to_hub:
        log.info("pushing merged model to https://huggingface.co/%s", push_to_hub)
        model.push_to_hub(push_to_hub, private=private, max_shard_size=max_shard_size)
        tokenizer.push_to_hub(push_to_hub, private=private)
        summary["pushed_to"] = push_to_hub

    with open(os.path.join(output_dir, "merge_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)
    log.info("summary: %s", json.dumps(summary, ensure_ascii=False))
    # Both of the above land in blob, which answers AuthorizationFailure to a
    # client outside the workspace network -- so on this workspace a merge that
    # succeeded and one that wrote nothing look identical from the submitter's
    # side. `qlora.py` already learned this; the merge path had not, and
    # recovering `architectures` from a completed run cost a second job.
    from ..train.report import publish

    publish(summary, prefix="merge.")
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
