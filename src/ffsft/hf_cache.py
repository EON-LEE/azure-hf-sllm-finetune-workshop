"""Undo the biggest cost `from_pretrained` leaves behind: the download itself.

Once a checkpoint has been loaded (the quantized weights end up in GPU memory),
the safetensors files `snapshot_download` pulled down sit on local disk unread
for the rest of the process. For Qwen3.8-27B that is measured at ~65 GB against
a fresh node's ~67-72 GB of free local disk (JOURNAL §90) -- most of a node's
disk budget sitting idle in a cache nothing will read again, leaving too little
margin for whatever runs next and tipping into `UserScriptFilledDisk`.

This module sits at the package root rather than under `train/` because the
same problem hits every job that loads a full checkpoint from the Hub, not just
training: QLoRA training loads it once, and `eval.run` loads it twice more in
the same process (once for the base model, once with the adapter applied) to
compare before/after scores. Both call sites need the fix. Putting it under
`train/` would force `eval` to import from a package it otherwise has no
dependency on -- and did, in fact, need it: `eval.run` was the one call site
still missing this fix when JOURNAL §92 was written, so it redownloaded the
weights training had just evicted and filled the disk during eval instead of
during training.
"""

from __future__ import annotations

import logging

log = logging.getLogger("ffsft.hf_cache")


def free_hf_download_cache(hf_id: str) -> float | str:
    """Delete `hf_id`'s downloaded weights from the local HF cache, return GB freed.

    `scan_cache_dir` walks reference counts before deleting, so this is safe
    even if some other repo shares a blob.

    Returns `0.0` for a confirmed-empty result (the repo genuinely isn't
    cached) and the string `"scan_failed"` when the cache could not be read at
    all -- the two must stay distinguishable so a caller can't mistake
    "nothing to free" for "could not check" (that conflation is exactly what
    test_no_except_handler_hands_a_caller_an_empty_value_it_never_read.py
    guards against). `mlflow_report.split_metrics_and_tags` already routes a
    non-numeric value to a tag instead of a metric, so passing this straight
    into a report dict keeps the distinction visible downstream for free.
    """
    from huggingface_hub import scan_cache_dir

    try:
        cache_info = scan_cache_dir()
    except Exception:
        log.warning("could not scan HF cache to free disk; continuing", exc_info=True)
        return "scan_failed"

    revisions = [
        rev.commit_hash
        for repo in cache_info.repos
        if repo.repo_id == hf_id
        for rev in repo.revisions
    ]
    if not revisions:
        return 0.0
    freed_bytes = sum(
        rev.size_on_disk
        for repo in cache_info.repos
        if repo.repo_id == hf_id
        for rev in repo.revisions
    )
    cache_info.delete_revisions(*revisions).execute()
    freed_gb = freed_bytes / 1e9
    log.info("freed %.1f GB of HF cache for %s (no longer needed on disk)", freed_gb, hf_id)
    return freed_gb
