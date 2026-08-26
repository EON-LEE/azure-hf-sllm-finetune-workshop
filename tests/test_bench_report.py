"""The reporting channel: what has to be true for a bench run to be readable.

This workspace's storage account has publicNetworkAccess disabled, so from this
side of the VNet the job's declared output, its SAS link and its live log stream
are all AuthorizationFailure. MLflow is the only path a measurement can take off
the node. Every assertion here stands for a way that path silently produces a
run whose numbers are gone -- a tag over the store's length limit taking the
tags after it down with it, a knee of None reading as a knee of zero, a failed
server whose reason was written only to blob.
"""

from __future__ import annotations

import json
import sys

from ffsft.serve.bench_report import (
    TAG_LIMIT,
    _tag,
    _tag_long,
    build_report,
    enable_mlflow_lib,
    error_excerpt,
    flatten,
    flatten_smoke,
    main,
    publish_report,
)

SWEEP = {
    "base_url": "http://127.0.0.1:8000/v1",
    "model": "ffsft",
    "max_tokens": 128,
    "ttft_slo_s": 1.0,
    "levels": [
        {
            "concurrency": 1,
            "requests": 16,
            "succeeded": 16,
            "failed": 0,
            "wall_s": 40.0,
            "ttft_p50": 0.21,
            "ttft_p95": 0.33,
            "ttft_p99": 0.4,
            "tpot_p50": 0.02,
            "tpot_p95": 0.03,
            "e2e_p50": 2.5,
            "e2e_p95": 2.9,
            "e2e_p99": 3.1,
            "output_tokens": 2048,
            "output_tok_per_s": 51.2,
            "requests_per_s": 0.4,
            "errors": {},
        },
        {
            "concurrency": 8,
            "requests": 16,
            "succeeded": 14,
            "failed": 2,
            "wall_s": 20.0,
            "ttft_p50": 0.9,
            "ttft_p95": 1.4,
            "ttft_p99": 1.8,
            "tpot_p50": 0.05,
            "tpot_p95": 0.08,
            "e2e_p50": 6.0,
            "e2e_p95": 7.5,
            "e2e_p99": 8.0,
            "output_tokens": 1792,
            "output_tok_per_s": 89.6,
            "requests_per_s": 0.7,
            "errors": {"HTTP 500": 2},
        },
    ],
    "knee_concurrency": 4,
    "peak_output_tok_per_s": 89.6,
}


def test_each_level_becomes_scalars_named_by_its_concurrency():
    flat = flatten(SWEEP)
    assert flat["bench.c1.ttft_p95"] == 0.33
    assert flat["bench.c8.ttft_p95"] == 1.4
    assert flat["bench.c8.tpot_p50"] == 0.05
    assert flat["bench.c8.e2e_p99"] == 8.0
    assert flat["bench.c8.output_tok_per_s"] == 89.6


def test_concurrency_is_the_key_and_never_also_a_metric():
    """`bench.c8.concurrency = 8` would sit in the UI looking like a reading."""
    flat = flatten(SWEEP)
    assert "bench.c8.concurrency" not in flat
    assert "bench.c1.concurrency" not in flat


def test_a_missing_knee_is_recorded_as_a_finding_and_never_as_zero():
    """No level meeting the SLO is a result. A metric of 0.0 would read as
    "the knee is at concurrency zero", which is a different and false claim."""
    flat = flatten({**SWEEP, "knee_concurrency": None})
    assert "bench.knee_concurrency" not in flat
    assert flat["bench.knee_concurrency_none"] == "no level met the p95 TTFT SLO"

    flat = flatten(SWEEP)
    assert flat["bench.knee_concurrency"] == 4
    assert "bench.knee_concurrency_none" not in flat


def test_errors_are_carried_only_when_there_are_some():
    flat = flatten(SWEEP)
    assert "bench.c1.errors" not in flat
    assert json.loads(flat["bench.c8.errors"]) == {"HTTP 500": 2}


def test_the_failure_count_is_summed_across_the_whole_sweep():
    """A sweep whose late levels 500 on everything still prints a full table."""
    flat = flatten(SWEEP)
    assert flat["bench.failed_total"] == 2
    assert flat["bench.levels"] == 2


def test_the_korean_reply_travels_with_the_latencies():
    """A server answering every request with nonsense produces a table that
    looks exactly like a good one; the reply text is what tells them apart."""
    smoke = {
        "choices": [{"message": {"content": "서울입니다."}}],
        "usage": {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42},
    }
    flat = flatten_smoke(smoke)
    assert flat["bench.smoke.reply"] == "서울입니다."
    assert flat["bench.smoke.completion_tokens"] == 12


def test_a_malformed_smoke_reply_does_not_take_the_usage_numbers_with_it():
    flat = flatten_smoke({"choices": [], "usage": {"total_tokens": 42}})
    assert flat["bench.smoke.total_tokens"] == 42
    assert "bench.smoke.reply" not in flat


def test_tags_are_truncated_because_one_long_value_drops_the_rest():
    """`ffsft.train.report.publish` sends every tag inside one try block, so a
    value the store rejects for length does not fail loudly -- it silently
    discards each tag queued behind it."""
    tags: dict[str, str] = {}
    _tag(tags, "bench.smoke.reply", "가" * 5000)
    assert len(tags["bench.smoke.reply"]) == TAG_LIMIT


def test_an_empty_tag_is_dropped_rather_than_sent_as_an_empty_string():
    tags: dict[str, str] = {}
    _tag(tags, "bench.status", "   ")
    assert tags == {}


def test_a_failed_servers_log_tail_survives_in_numbered_chunks():
    """The tail is the only reason a failed run is diagnosable from outside the
    VNet; one tag cannot hold it and dropping it costs another A100 allocation."""
    tags: dict[str, str] = {}
    _tag_long(tags, "bench.vllm_tail", "x" * (TAG_LIMIT * 2 + 5))
    assert list(tags) == ["bench.vllm_tail.01", "bench.vllm_tail.02", "bench.vllm_tail.03"]
    assert "".join(tags.values()) == "x" * (TAG_LIMIT * 2 + 5)


def test_the_mlflow_target_dir_is_appended_so_it_cannot_shadow_vllms_deps(tmp_path):
    """PYTHONPATH sorts ahead of site-packages. A --target install placed there
    would hand this process its own requests/protobuf/typing-extensions inside a
    container whose site-packages is vLLM's."""
    lib = tmp_path / "mlflowlib"
    lib.mkdir()
    before = list(sys.path)
    try:
        assert enable_mlflow_lib(str(lib)) is True
        assert sys.path[-1] == str(lib)
        assert enable_mlflow_lib(str(lib)) is True
        assert sys.path.count(str(lib)) == 1
    finally:
        sys.path[:] = before


def test_a_missing_mlflow_dir_is_not_an_error(tmp_path):
    """On a laptop mlflow is installed normally and there is nothing to add."""
    assert enable_mlflow_lib(str(tmp_path / "absent")) is False


def test_a_run_that_never_reached_the_sweep_still_reports_why(tmp_path):
    """The paths that matter most are the ones with no loadtest.json at all."""
    (tmp_path / "vllm.log").write_text("Traceback\nValueError: bad flag\n")
    flat = build_report(str(tmp_path), status="startup_failed",
                        vllm_log=str(tmp_path / "vllm.log"))
    assert flat["bench.status"] == "startup_failed"
    assert "ValueError: bad flag" in flat["_vllm_tail"]


def test_a_good_run_does_not_carry_the_servers_steady_state_chatter(tmp_path):
    (tmp_path / "vllm.log").write_text("Avg generation throughput: 51.2 tokens/s\n")
    (tmp_path / "loadtest.json").write_text(json.dumps(SWEEP))
    flat = build_report(str(tmp_path), status="swept",
                        vllm_log=str(tmp_path / "vllm.log"))
    assert "_vllm_tail" not in flat
    assert flat["bench.c8.ttft_p95"] == 1.4


def test_what_reaches_mlflow_is_classified_and_truncated_at_this_boundary(
    monkeypatch, tmp_path
):
    """The shaping has to happen before the handoff, because `publish` sends
    every tag in one try block and the store's length limit is enforced there."""
    import ffsft.train.report as report_mod

    sent: dict[str, object] = {}

    def capture(report, prefix=""):
        sent.update(report)
        return True

    monkeypatch.setattr(report_mod, "publish", capture)

    (tmp_path / "loadtest.json").write_text(json.dumps(SWEEP))
    (tmp_path / "smoke.json").write_text(
        json.dumps({"choices": [{"message": {"content": "서" * 5000}}]})
    )
    (tmp_path / "vllm.log").write_text("y" * 900)
    flat = build_report(str(tmp_path), status="smoke_failed",
                        vllm_log=str(tmp_path / "vllm.log"))

    assert publish_report(flat) is True
    assert sent["bench.c8.ttft_p95"] == 1.4
    assert len(sent["bench.smoke.reply"]) == TAG_LIMIT
    assert sent["bench.vllm_tail.01"] == "y" * TAG_LIMIT
    assert "_vllm_tail" not in sent


def test_a_reporter_that_trips_over_a_bad_artefact_returns_false_not_a_raise(
    monkeypatch, tmp_path
):
    """Reporting must not be what fails a run that measured correctly."""
    import ffsft.train.report as report_mod

    def explode(report, prefix=""):
        raise RuntimeError("tracking store unreachable")

    monkeypatch.setattr(report_mod, "publish", explode)
    assert publish_report(build_report(str(tmp_path), status="swept")) is False


def test_the_reporter_exits_zero_so_the_runs_own_code_is_the_one_azure_sees(tmp_path):
    assert main(["--output-dir", str(tmp_path), "--status", "swept"]) == 0


# A real vLLM startup failure prints the cause first, from the EngineCore
# subprocess, then a second traceback from the API server that only points back
# at it. Trimmed from job careful_door_6fqvn7v4x4.
VLLM_FAILURE = [
    "INFO 08-25 loading weights ...\n",
    "INFO 08-25 model loaded in 214.7s\n",
    "(EngineCore_0) ERROR  Failed to infer device type\n",
    "(EngineCore_0) ValueError: No available memory for the cache blocks. Try\n",
    "(EngineCore_0) increasing gpu_memory_utilization when initializing.\n",
    "(EngineCore_0) frame two\n",
    "(APIServer pid=83) Traceback (most recent call last):\n",
    "(APIServer pid=83)   File core_client.py, line 987, in __init__\n",
    "(APIServer pid=83) RuntimeError: Engine core initialization failed. See\n",
    "(APIServer pid=83) root cause above. Failed core proc(s)\n",
]


def test_the_excerpt_starts_at_the_cause_not_the_last_traceback() -> None:
    """vLLM's final traceback says "See root cause above" and nothing else.

    A tail of this log carries the API server's re-raise, which names no cause.
    The window has to open at the EngineCore line instead.
    """
    excerpt = error_excerpt(VLLM_FAILURE, window=4)
    assert "No available memory for the cache blocks" in excerpt
    assert "Failed to infer device type" in excerpt
    assert "loading weights" not in excerpt


def test_a_log_that_never_errored_yields_nothing_rather_than_a_guess() -> None:
    """Empty, so the caller falls back to the tail instead of tagging noise."""
    assert error_excerpt(["INFO ready\n", "INFO serving\n"]) == ""


def test_the_cause_and_the_tail_both_leave_the_node() -> None:
    """They are different windows on the same file, and the far end has neither."""
    import os

    def _run(tmp: str) -> dict:
        with open(os.path.join(tmp, "vllm.log"), "w", encoding="utf-8") as fh:
            fh.writelines(VLLM_FAILURE)
        return build_report(tmp, status="server_exited_1",
                            vllm_log=os.path.join(tmp, "vllm.log"))

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        flat = _run(tmp)
    assert "No available memory" in flat["_vllm_cause"]
    assert "Engine core initialization failed" in flat["_vllm_tail"]


def test_the_chunks_of_one_excerpt_sort_back_into_order() -> None:
    """Tags sort as strings, and the Studio UI offers no way to re-sort them.

    Unpadded, ".10" lands between ".1" and ".2" and reassembles a traceback
    scrambled.
    """
    tags: dict[str, str] = {}
    _tag_long(tags, "bench.vllm_cause", "z" * (TAG_LIMIT * 11), chunks=12)
    assert list(tags) == sorted(tags)
    assert len(tags) == 11


#: The shape job quirky_bee_4yh061560n actually produced: a stamped EngineCore
#: traceback far longer than any window, whose only informative line is the
#: last, followed by the API server's re-raise.
def _stamped_failure(depth: int = 30) -> list[str]:
    stamp = "(EngineCore pid=287) ERROR 08-25 02:11:16 [core.py:1349] "
    body = ["EngineCore failed to start.", "Traceback (most recent call last):"]
    body += [f'  File "vllm/pad/{i}.py", line {i}, in pad_{i}' for i in range(depth)]
    body += ["ValueError: Unsupported weight name: visual.patch_embed.proj.weight"]
    return [stamp + line + "\n" for line in body] + [
        "(APIServer pid=85) RuntimeError: Engine core initialization failed.\n",
        "(APIServer pid=85) See root cause above.\n",
    ]


def test_the_exception_line_survives_a_traceback_longer_than_the_window() -> None:
    """A traceback names the failure on its last line, not its first.

    quirky_bee_4yh061560n published a window that opened at the right place,
    spent itself on call stack, and stopped short of the exception -- so it
    established that the failure was inside `load_model` and never said what
    the failure was. The window has two ends for that reason.
    """
    excerpt = error_excerpt(_stamped_failure(), window=10, head=3)
    assert "Unsupported weight name" in excerpt
    assert "EngineCore failed to start." in excerpt
    assert "lines omitted" in excerpt


def test_the_speaker_stamp_is_not_repeated_on_every_line() -> None:
    """57 characters of stamp per line, against a 240-character tag."""
    excerpt = error_excerpt(_stamped_failure())
    assert "EngineCore pid=287" not in excerpt
    assert "[core.py:1349]" not in excerpt
    assert "Unsupported weight name" in excerpt


def test_the_reraise_does_not_follow_the_cause_out() -> None:
    """The block ends where the API server starts saying nothing useful."""
    excerpt = error_excerpt(_stamped_failure())
    assert "APIServer" not in excerpt
    assert "See root cause above" not in excerpt
