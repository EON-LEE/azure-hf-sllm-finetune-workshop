#!/usr/bin/env python3
"""Verify every Hugging Face id referenced by the configs still exists.

Model ids, dataset ids and licenses drift. Run this before a demo:

    python scripts/verify_hf_ids.py              # check everything
    python scripts/verify_hf_ids.py --models     # models only
    python scripts/verify_hf_ids.py --spec Qwen/Qwen3.8-27B

Exits non-zero if any id is missing, so it can gate CI.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIGS = REPO_ROOT / "configs"
UA = {"User-Agent": "ffsft-verify/0.1"}


def _fetch(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _license_of(meta: dict) -> str:
    return next(
        (t.split("license:", 1)[1] for t in meta.get("tags", []) if t.startswith("license:")),
        "unspecified",
    )


def check_repo(repo_id: str, kind: str, declared_license: str | None) -> tuple[bool, str]:
    """kind is 'models' or 'datasets' (the Hub API path segment)."""
    try:
        meta = _fetch(f"https://huggingface.co/api/{kind}/{repo_id}")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "GATED or PRIVATE (401) - needs an access request"
        return False, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, f"error: {e}"

    hub_license = _license_of(meta)
    note = f"license={hub_license}"

    total = (meta.get("safetensors") or {}).get("total")
    if total:
        note += f" params={total / 1e9:.1f}B"
    note += f" downloads={meta.get('downloads', 0):,}"

    if declared_license and declared_license != "unknown" and hub_license != "unspecified":
        if declared_license.lower() != hub_license.lower():
            note += f"  [!] config says '{declared_license}'"
    return True, note


def load_yaml(name: str) -> dict:
    return yaml.safe_load((CONFIGS / name).read_text(encoding="utf-8")) or {}


def verify_models() -> list[str]:
    failures = []
    print("\n===== configs/models.yaml =====")
    for m in load_yaml("models.yaml").get("models", []):
        hf_id = m.get("hf_id")
        if not hf_id:
            print(f"  [skip] {m['key']:<24} provider={m['provider']} (no HF weights)")
            continue
        ok, note = check_repo(hf_id, "models", m.get("license"))
        print(f"  [{'OK  ' if ok else 'FAIL'}] {m['key']:<24} {hf_id:<52} {note}")
        if not ok:
            failures.append(f"model {m['key']} -> {hf_id}: {note}")
    return failures


def verify_datasets() -> list[str]:
    failures = []
    print("\n===== configs/datasets.yaml =====")
    for d in load_yaml("datasets.yaml").get("datasets", []):
        ok, note = check_repo(d["dataset_id"], "datasets", d.get("license"))
        print(f"  [{'OK  ' if ok else 'FAIL'}] {d['key']:<32} {d['dataset_id']:<56} {note}")
        if not ok:
            failures.append(f"dataset {d['key']} -> {d['dataset_id']}: {note}")
    return failures


def verify_benchmarks() -> list[str]:
    failures = []
    print("\n===== configs/benchmarks.yaml =====")
    for b in load_yaml("benchmarks.yaml").get("benchmarks", []):
        ok, note = check_repo(b["dataset_id"], "datasets", b.get("license"))
        print(f"  [{'OK  ' if ok else 'FAIL'}] {b['key']:<16} {b['dataset_id']:<44} {note}")
        if not ok:
            failures.append(f"benchmark {b['key']} -> {b['dataset_id']}: {note}")
    return failures


def show_spec(repo_id: str) -> None:
    """Dump the real architecture config, which is how we caught that
    Qwen3.8-27B is multimodal and hybrid rather than a plain dense text model."""
    print(f"\n===== {repo_id} config.json =====")
    cfg = _fetch(f"https://huggingface.co/{repo_id}/resolve/main/config.json")
    for k, v in cfg.items():
        if not isinstance(v, dict):
            print(f"  {k:<26} = {v}")
    for section in ("text_config", "vision_config"):
        if section in cfg:
            print(f"  --- {section} ---")
            for k, v in cfg[section].items():
                rendered = f"<{type(v).__name__} len={len(v)}>" if isinstance(v, list) else v
                print(f"    {k:<26} = {rendered}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--models", action="store_true", help="check models only")
    p.add_argument("--datasets", action="store_true", help="check datasets only")
    p.add_argument("--benchmarks", action="store_true", help="check benchmarks only")
    p.add_argument("--spec", metavar="REPO_ID", help="dump architecture config for one model")
    args = p.parse_args()

    if args.spec:
        show_spec(args.spec)
        return 0

    selected = args.models or args.datasets or args.benchmarks
    failures: list[str] = []
    if args.models or not selected:
        failures += verify_models()
    if args.datasets or not selected:
        failures += verify_datasets()
    if args.benchmarks or not selected:
        failures += verify_benchmarks()

    print()
    if failures:
        print(f"{len(failures)} problem(s):")
        for f in failures:
            print(f"  - {f}")
        print("\nGated repos are expected for a few entries; treat those as warnings.")
        return 1
    print("All ids resolved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
