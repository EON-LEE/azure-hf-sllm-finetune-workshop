"""Publish the load test's numbers over the one channel that leaves the node.

Azure ML writes `./outputs` and `user_logs/std_log.txt` to the workspace's
default blob store. This workspace's storage account has publicNetworkAccess
disabled, so from outside the VNet `jobs.download` returns AuthorizationFailure,
a service-issued SAS link returns 403, the artifact `content` proxy endpoint has
been removed from the API ("request a SAS link instead"), and -- the one that is
easy to get wrong -- `jobs.stream` is blocked too, because streaming reads
`std_log.txt` from that same blob. Job `helpful_jelly_gndv8d135q` ran to
Completed and its stream delivered three lines: RunId, Web View, Execution
Summary. So a bench job that only writes files and prints to stdout produces
measurements nobody can read, which is the same as not measuring.

MLflow is the exception, for the reason `ffsft.mlflow_report` gives at length:
metrics and tags go to the tracking service over its own endpoint, authorised by
an ordinary token, with no blob anywhere in the path. That module is already the
training job's only usable reporting channel; this one reshapes a sweep report
into the flat metric/tag pairs it publishes, so the load test gets the same
treatment.

Why the numbers are torn apart into `bench.c8.ttft_p95`-style scalars rather than
posted as one JSON blob: MLflow tag values are short and capped, and a run's
metrics are what the workspace UI plots and what a REST `runs/get` returns in
full. A flat scalar per measurement is legible in both. The whole JSON is still
written to the declared output for anyone inside the VNet.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys

log = logging.getLogger("ffsft.bench_report")

#: Where Dockerfile.bench `pip install --target`s mlflow. See `enable_mlflow_lib`.
MLFLOW_LIB = os.environ.get("FFSFT_MLFLOW_LIB", "/opt/mlflowlib")

#: Tag values are truncated to this. MLflow's own limit is 5000 characters, but
#: Azure ML's tracking store enforces a much shorter one, and `publish()` sends
#: tags in a single try block -- so one over-long value does not fail loudly, it
#: silently drops every tag after it. 240 is comfortably under any limit either
#: service has been observed to apply.
TAG_LIMIT = 240

#: Lines that mark where a vLLM startup actually went wrong. The API server's
#: own traceback ends in "See root cause above" -- the cause is printed earlier,
#: by the EngineCore subprocess, so a plain tail captures the symptom and drops
#: the reason. Job careful_door_6fqvn7v4x4 failed exactly that way: the tail
#: arrived intact and said only that engine init had failed.
ERROR_MARKS = (
    "Traceback (most recent call last)",
    "ERROR ",
    "Error:",
    "error:",
    "raise ",
    "out of memory",
    "No available memory",
    "not supported",
    "Unsupported",
    "AssertionError",
    "ValueError",
    "RuntimeError",
)

#: Who is speaking: the `(EngineCore pid=287)` stamp vLLM puts on every line a
#: subprocess emits. The cause block is the run of lines carrying one speaker,
#: and it ends exactly where the API server starts re-raising.
_SPEAKER = re.compile(r"^\([A-Za-z][^)\n]*\)")

#: The whole stamp, including the level and timestamp:
#: "(EngineCore pid=287) ERROR 08-25 02:11:16 [core.py:1349] ". 57 characters,
#: repeated on every line of the traceback, against a 240-character tag. Job
#: quirky_bee_4yh061560n spent a third of its window restating this.
_LOG_PREFIX = re.compile(
    r"^\([A-Za-z][^)\n]*\)\s*"
    r"(?:(?:ERROR|WARNING|INFO|DEBUG)\s+[\d:\- ]+\[[^\]]+\] ?)?"
)

#: Level fields that become metrics. `concurrency` is deliberately absent: it is
#: already in the key, and emitting it as `bench.c8.concurrency = 8` would put a
#: tautology next to real measurements. `errors` is a dict and becomes a tag.
LEVEL_METRICS = (
    "requests",
    "succeeded",
    "failed",
    "wall_s",
    "ttft_p50",
    "ttft_p95",
    "ttft_p99",
    "tpot_p50",
    "tpot_p95",
    "e2e_p50",
    "e2e_p95",
    "e2e_p99",
    "output_tokens",
    "output_tok_per_s",
    "requests_per_s",
)


def enable_mlflow_lib(path: str = MLFLOW_LIB) -> bool:
    """Make a `pip install --target` directory importable, shadowing nothing.

    Appended to `sys.path` rather than prepended, and set here rather than in
    PYTHONPATH, because PYTHONPATH entries sort ahead of site-packages: a
    --target directory placed there would hand this process its own copy of
    every transitive dependency mlflow dragged in -- requests, protobuf,
    typing-extensions -- in a container whose site-packages is vLLM's, tuned to
    versions vLLM pins. Last place on sys.path inverts that to the behaviour
    actually wanted: supply what is missing, shadow nothing that already works.

    Returns whether the directory was there to add. A False is not an error;
    on a laptop with mlflow installed normally there is nothing to add.
    """
    if not os.path.isdir(path):
        return False
    if path not in sys.path:
        sys.path.append(path)
    return True


def _tag(tags: dict[str, str], name: str, value: object) -> None:
    """Set a tag, truncated, and skip it entirely when there is nothing to say."""
    text = str(value).strip()
    if not text:
        return
    tags[name] = text[:TAG_LIMIT]


def _tag_long(tags: dict[str, str], name: str, text: str, chunks: int = 4) -> None:
    """Spread a long string across numbered tags.

    Used for the excerpts of vllm.log when the server never came up. Those are
    the entire reason a failed bench job is diagnosable, and on this workspace
    the log is otherwise written only to blob storage this account cannot read.
    A chunk is roughly three lines, so callers pass a count sized to what they
    are carrying: the cause gets more than the tail.
    """
    text = text.strip()
    for index in range(chunks):
        piece = text[index * TAG_LIMIT : (index + 1) * TAG_LIMIT]
        if not piece:
            return
        # Zero-padded so the chunks read in order everywhere they are shown.
        # Tags sort as strings, and unpadded ".10" lands between ".1" and ".2"
        # -- which puts a traceback back together in the wrong order in the
        # Studio UI, where nobody can re-sort it.
        tags[f"{name}.{index + 1:02d}"] = piece


def error_excerpt(lines: list[str], window: int = 28, head: int = 6) -> str:
    """The first thing that went wrong, including the line that names it.

    Two properties of a vLLM startup failure defeat the obvious implementations.

    It is reported twice. The EngineCore subprocess prints the real exception;
    the API server then prints its own traceback, ending in "See root cause
    above". Tailing the file gets the second one, which names nothing -- job
    careful_door_6fqvn7v4x4 published exactly that. So the window opens at the
    earliest `ERROR_MARKS` line rather than at the end of the file.

    And a traceback names the failure on its LAST line, not its first. Job
    quirky_bee_4yh061560n opened the window correctly, spent it on 28 lines of
    call stack, and stopped short of the exception: it established that the
    failure was inside `load_model` without saying what the failure was. So
    when the block does not fit, both ends are kept rather than the head alone.

    The block is bounded by its speaker, because everything after that belongs
    to the re-raise, and each line is stripped of the speaker stamp on the way
    out -- it costs 57 of every 240-character tag to repeat what the first line
    already said.

    Returns "" when nothing matches, so the caller can fall back to the tail
    rather than publishing an empty tag.
    """
    for index, line in enumerate(lines):
        if not any(mark in line for mark in ERROR_MARKS):
            continue
        speaker = _SPEAKER.match(line)
        if speaker is None:
            # No subprocess stamp: nothing to bound the block with and nothing
            # to strip, so keep the plain fixed window.
            return "".join(lines[index : index + window])
        block = []
        for candidate in lines[index:]:
            if not candidate.startswith(speaker.group()):
                break
            stripped = _LOG_PREFIX.sub("", candidate).rstrip()
            if stripped:
                block.append(stripped + "\n")
        if len(block) <= window:
            return "".join(block)
        omitted = f"... {len(block) - window} lines omitted ...\n"
        return "".join(block[:head] + [omitted] + block[head - window :])
    return ""


def flatten(report: dict, prefix: str = "bench.") -> dict[str, object]:
    """Turn one sweep report into flat `name -> value` pairs.

    The caller hands the result to `ffsft.mlflow_report.split_metrics_and_tags`,
    which sorts numbers into metrics and everything else into tags, so this
    function only has to choose names and leave values in their natural type.
    """
    flat: dict[str, object] = {}

    for key in ("model", "base_url"):
        if report.get(key):
            flat[f"{prefix}{key}"] = str(report[key])
    for key in ("max_tokens", "ttft_slo_s", "peak_output_tok_per_s"):
        if isinstance(report.get(key), (int, float)):
            flat[f"{prefix}{key}"] = report[key]

    levels = report.get("levels") or []
    flat[f"{prefix}levels"] = len(levels)

    # A knee of None means no concurrency level held p95 TTFT under the SLO.
    # That is a finding, not a missing value, so it is recorded rather than
    # dropped -- as a tag, because there is no number that says "none" without
    # being mistaken for one that says "zero".
    knee = report.get("knee_concurrency")
    if isinstance(knee, (int, float)):
        flat[f"{prefix}knee_concurrency"] = knee
    else:
        flat[f"{prefix}knee_concurrency_none"] = "no level met the p95 TTFT SLO"

    failed_total = 0
    for level in levels:
        conc = level.get("concurrency")
        if conc is None:
            continue
        for field in LEVEL_METRICS:
            value = level.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                flat[f"{prefix}c{conc}.{field}"] = value
        failed_total += int(level.get("failed") or 0)
        errors = level.get("errors") or {}
        if errors:
            flat[f"{prefix}c{conc}.errors"] = json.dumps(errors, ensure_ascii=False)

    flat[f"{prefix}failed_total"] = failed_total
    return flat


def flatten_smoke(smoke: dict, prefix: str = "bench.smoke.") -> dict[str, object]:
    """Pull the one Korean completion's shape and text out of the smoke reply.

    The reply text matters as much as the latencies: a sweep against a server
    that answers every request with fluent nonsense produces a table that looks
    exactly like a good one. This is the only place a human reading the run in
    the workspace UI can see what the fine-tuned model actually said.
    """
    flat: dict[str, object] = {}
    usage = smoke.get("usage") or {}
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if isinstance(usage.get(field), (int, float)):
            flat[f"{prefix}{field}"] = usage[field]
    try:
        reply = smoke["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        reply = ""
    if reply:
        flat[f"{prefix}reply"] = reply
    return flat


def _read_json(path: str) -> dict | None:
    """Read a JSON file, or explain in one line why there is nothing to read."""
    if not path or not os.path.isfile(path):
        log.info("no file at %s", path)
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        log.warning("could not read %s: %s: %s", path, type(exc).__name__, exc)
        return None


def build_report(
    output_dir: str,
    status: str = "",
    vllm_log: str = "",
    tail_lines: int = 40,
) -> dict[str, object]:
    """Assemble everything worth reporting from whatever the job managed to write.

    Every part is optional on purpose. This runs from the entrypoint's EXIT trap,
    so it is reached by the paths where the server never became healthy and there
    is no sweep at all -- and those are precisely the runs whose reason for
    failing has to survive, because a LowPriority A100 allocation is not cheap to
    repeat blind.
    """
    flat: dict[str, object] = {}
    if status:
        flat["bench.status"] = status

    report = _read_json(os.path.join(output_dir, "loadtest.json"))
    if report:
        flat.update(flatten(report))

    smoke = _read_json(os.path.join(output_dir, "smoke.json"))
    if smoke:
        flat.update(flatten_smoke(smoke))

    # Only on a bad ending. On a good one the tail is vLLM's steady-state
    # throughput chatter, which says nothing and would crowd the run's tags.
    if vllm_log and os.path.isfile(vllm_log) and status and status != "swept":
        try:
            with open(vllm_log, encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError as exc:
            flat["_vllm_tail"] = f"could not read {vllm_log}: {exc}"
            return flat
        cause = error_excerpt(lines)
        if cause:
            flat["_vllm_cause"] = cause
        flat["_vllm_tail"] = "".join(lines[-tail_lines:])
    return flat


def publish_report(flat: dict[str, object]) -> bool:
    """Send the flat report to MLflow. Never raises.

    Wrapped rather than trusted: `publish` swallows its own failures, but the
    shaping above it -- truncation, chunking, classification -- is this module's
    code running on whatever a partly-written artefact contained, and a bench
    run that measured a 27B correctly must not be marked failed because its
    reporter tripped over a malformed smoke reply.
    """
    try:
        return _publish_report(flat)
    except Exception as exc:  # noqa: BLE001 - reporting must never fail the run
        log.warning("bench report failed: %s: %s", type(exc).__name__, exc)
        return False


def _publish_report(flat: dict[str, object]) -> bool:
    from ffsft.mlflow_report import publish, split_metrics_and_tags

    tail = flat.pop("_vllm_tail", None)
    cause = flat.pop("_vllm_cause", None)

    # `split_metrics_and_tags` owns the rule for which side a value belongs on
    # -- including the subtle one about bool being a subclass of int -- so it
    # makes the call here rather than having a second copy of that rule. Only
    # the tag side is touched, to truncate; `publish` then classifies the result
    # again and reaches the same answer, because truncating a string leaves a
    # string and the metrics were not touched at all.
    metrics, tags = split_metrics_and_tags(flat)
    ready: dict[str, object] = dict(metrics)
    for name, value in tags.items():
        _tag(ready, name, value)
    # Cause before tail, and given the larger share of chunks: when only one of
    # the two can be read at a glance it should be the one naming the failure.
    if isinstance(cause, str):
        _tag_long(ready, "bench.vllm_cause", cause, chunks=14)
    if isinstance(tail, str):
        _tag_long(ready, "bench.vllm_tail", tail, chunks=8)
    return publish(ready)


def main(argv: list[str] | None = None) -> int:
    """Read the job's artefacts, print them, and publish them.

    Prints as well as publishes because the two channels fail independently: the
    stdout copy is what a colleague inside the VNet reads without a tracking
    client, and it costs one screen.
    """
    logging.basicConfig(level=logging.INFO, format="[bench-report] %(message)s")
    parser = argparse.ArgumentParser(description="Publish bench results to MLflow.")
    parser.add_argument("--output-dir", required=True, help="the job's declared output")
    parser.add_argument("--status", default="", help="how far the run got")
    parser.add_argument("--vllm-log", default="", help="server log, tailed on failure")
    parser.add_argument(
        "--mlflow-lib",
        default=MLFLOW_LIB,
        help="pip --target directory holding mlflow (appended to sys.path)",
    )
    args = parser.parse_args(argv)

    enable_mlflow_lib(args.mlflow_lib)
    flat = build_report(args.output_dir, args.status, args.vllm_log)
    for name in sorted(flat):
        if name != "_vllm_tail":
            print(f"[bench-report] {name} = {flat[name]}")
    ok = publish_report(flat)
    print(f"[bench-report] published={ok}")
    # Zero regardless. This is the reporter for a measurement run; if reporting
    # is the only thing that broke, the run's own exit code is the one that
    # should reach Azure ML, and the caller passes it through.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
