"""Korean SFT data loading, driven by `configs/datasets.yaml`.

Korean instruction datasets on the Hub disagree wildly about schema -- some ship
`instruction`/`output`, some `conversations` in ShareGPT form, some a plain
`messages` list. This module normalises all of them to a single `messages` list
and then renders with the *model's own* chat template, so the training surface
form matches inference exactly.

Non-commercial datasets are excluded unless explicitly allowed, because the
default mixes are meant to be safe for an enterprise demo.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("ffsft.data")

_CONFIG = Path(__file__).resolve().parents[3] / "configs" / "datasets.yaml"

#: Field names seen in the wild, in priority order.
_INSTRUCTION_KEYS = ("instruction", "prompt", "question", "input_text")
_INPUT_KEYS = ("input", "context")
_OUTPUT_KEYS = ("output", "response", "answer", "completion", "chosen")
_CONVERSATION_KEYS = ("messages", "conversations", "conversation")

_ROLE_ALIASES = {
    "human": "user",
    "user": "user",
    "prompter": "user",
    "gpt": "assistant",
    "assistant": "assistant",
    "bot": "assistant",
    "system": "system",
}


def load_config(path: Path | None = None) -> dict:
    with open(path or _CONFIG, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def resolve_mix(mix: str, *, allow_noncommercial: bool = False, config: dict | None = None):
    """Return the dataset entries for a named mix, filtering by license."""
    cfg = config or load_config()
    mixes = cfg.get("mixes", {})
    if mix not in mixes:
        raise KeyError(f"unknown mix '{mix}'. Available: {', '.join(sorted(mixes))}")

    by_key = {d["key"]: d for d in cfg.get("datasets", [])}
    chosen = []
    for key in mixes[mix]["datasets"]:
        entry = by_key.get(key)
        if entry is None:
            raise KeyError(f"mix '{mix}' references unknown dataset '{key}'")
        if not entry.get("commercial_use", False) and not allow_noncommercial:
            log.warning(
                "skipping %s (%s): not commercially usable. "
                "Pass allow_noncommercial=True to include it.",
                key, entry.get("license", "unknown"),
            )
            continue
        chosen.append(entry)
    if not chosen:
        raise ValueError(f"mix '{mix}' selected no datasets after license filtering")
    return chosen


def _first(row: dict, keys: Iterable[str]):
    for k in keys:
        v = row.get(k)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, list) and v:
            return v
    return None


def to_messages(row: dict) -> list[dict] | None:
    """Normalise one arbitrary row into a `messages` list, or None if unusable."""
    convo = _first(row, _CONVERSATION_KEYS)
    if isinstance(convo, list):
        out = []
        for turn in convo:
            if not isinstance(turn, dict):
                return None
            role = turn.get("role") or turn.get("from")
            content = turn.get("content") or turn.get("value")
            role = _ROLE_ALIASES.get(str(role).lower())
            if not role or not isinstance(content, str) or not content.strip():
                continue
            out.append({"role": role, "content": content})
        return out if len(out) >= 2 else None

    instruction = _first(row, _INSTRUCTION_KEYS)
    output = _first(row, _OUTPUT_KEYS)
    if not isinstance(instruction, str) or not isinstance(output, str):
        return None

    extra = _first(row, _INPUT_KEYS)
    user = f"{instruction}\n\n{extra}" if isinstance(extra, str) and extra.strip() else instruction
    return [
        {"role": "user", "content": user},
        {"role": "assistant", "content": output},
    ]


def load_sft_dataset(
    mix: str,
    tokenizer: Any = None,
    spec: Any = None,
    *,
    render: Callable[[list[dict]], str] | None = None,
    max_samples: int | None = None,
    allow_noncommercial: bool = False,
    config: dict | None = None,
):
    """Load, normalise and render a Korean SFT mix into a `text` column."""
    from datasets import concatenate_datasets, load_dataset

    cfg = config or load_config()
    defaults = cfg.get("defaults", {})
    entries = resolve_mix(mix, allow_noncommercial=allow_noncommercial, config=cfg)

    min_chars = defaults.get("min_chars", 10)
    max_chars = defaults.get("max_chars", 8000)

    parts = []
    per_source = max_samples // len(entries) if max_samples else None
    for entry in entries:
        dataset_id = entry["dataset_id"]
        split = "train" if per_source is None else f"train[:{per_source * 4}]"
        log.info("loading %s (%s)", dataset_id, entry.get("license", "?"))
        try:
            ds = load_dataset(dataset_id, split=split)
        except Exception as exc:  # noqa: BLE001
            log.warning("  skipped %s: %s: %s", dataset_id, type(exc).__name__, exc)
            continue

        columns = list(ds.column_names)

        def _map(row, _cols=columns):
            messages = to_messages(row)
            if not messages:
                return {"text": None}
            text = render(messages) if render else "\n".join(m["content"] for m in messages)
            return {"text": text}

        ds = ds.map(_map, remove_columns=columns, desc=f"normalise {dataset_id}")
        ds = ds.filter(
            lambda r: r["text"] is not None and min_chars <= len(r["text"]) <= max_chars
        )
        if per_source:
            ds = ds.select(range(min(per_source, len(ds))))
        log.info("  -> %d usable examples", len(ds))
        parts.append(ds)

    if not parts:
        raise RuntimeError(f"mix '{mix}' produced no usable examples")

    merged = parts[0] if len(parts) == 1 else concatenate_datasets(parts)
    merged = merged.shuffle(seed=defaults.get("seed", 42))
    if max_samples:
        merged = merged.select(range(min(max_samples, len(merged))))
    return merged
