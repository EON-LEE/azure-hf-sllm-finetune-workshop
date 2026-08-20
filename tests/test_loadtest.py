"""Tests for the load-test client's measurement logic.

The percentile and TPOT maths are worth testing precisely because they are the
numbers a capacity decision is made from, and because a load test that silently
reports optimistic latency is worse than no load test. No network is touched:
`summarize` and `find_knee` are pure functions over `RequestResult`.
"""

from __future__ import annotations

from ffsft.serve import LevelResult, RequestResult, find_knee, format_table, summarize
from ffsft.serve.loadtest import _pct

# -- percentiles --------------------------------------------------------


def test_pct_empty_is_zero():
    assert _pct([], 0.95) == 0.0


def test_pct_single_value():
    assert _pct([1.5], 0.95) == 1.5


def test_pct_interpolates():
    # p50 of 1..4 sits between 2 and 3.
    assert _pct([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5


def test_pct_p100_is_max():
    assert _pct([0.1, 5.0, 0.3], 1.0) == 5.0


# -- TPOT ---------------------------------------------------------------


def test_tpot_excludes_the_first_token():
    """TPOT is decode speed, so the prefill-bound first token must not be counted."""
    r = RequestResult(ok=True, ttft_s=1.0, total_s=3.0, output_tokens=11)
    assert r.tpot_s == 0.2  # 2 seconds over 10 subsequent tokens


def test_tpot_is_zero_for_single_token_response():
    assert RequestResult(ok=True, ttft_s=1.0, total_s=1.0, output_tokens=1).tpot_s == 0.0


# -- summarize ----------------------------------------------------------


def _ok(ttft: float, total: float, tokens: int = 11) -> RequestResult:
    return RequestResult(ok=True, status=200, ttft_s=ttft, total_s=total, output_tokens=tokens)


def test_summarize_counts_successes_and_failures():
    results = [_ok(0.1, 1.0), _ok(0.2, 1.1), RequestResult(ok=False, error="boom")]
    level = summarize(results, concurrency=4, wall=2.0)
    assert level.requests == 3
    assert level.succeeded == 2
    assert level.failed == 1
    assert level.concurrency == 4


def test_summarize_excludes_failures_from_percentiles():
    """A failed request has ttft 0.0; letting it into the sample would flatter p50."""
    results = [_ok(1.0, 2.0), _ok(1.0, 2.0), RequestResult(ok=False, error="timeout")]
    level = summarize(results, concurrency=2, wall=2.0)
    assert level.ttft_p50 == 1.0


def test_summarize_throughput_uses_wall_clock():
    results = [_ok(0.1, 1.0, tokens=100), _ok(0.1, 1.0, tokens=100)]
    level = summarize(results, concurrency=2, wall=2.0)
    assert level.output_tokens == 200
    assert level.output_tok_per_s == 100.0
    assert level.requests_per_s == 1.0


def test_summarize_groups_errors():
    results = [
        RequestResult(ok=False, error="timeout"),
        RequestResult(ok=False, error="timeout"),
        RequestResult(ok=False, error="refused"),
    ]
    level = summarize(results, concurrency=1, wall=1.0)
    assert level.errors == {"timeout": 2, "refused": 1}


def test_summarize_handles_zero_wall_time():
    level = summarize([_ok(0.1, 0.2)], concurrency=1, wall=0.0)
    assert level.output_tok_per_s == 0.0


# -- find_knee ----------------------------------------------------------


def _level(conc: int, ttft_p95: float, failed: int = 0, tok_s: float = 100.0) -> LevelResult:
    return LevelResult(
        concurrency=conc, requests=10, succeeded=10 - failed, failed=failed,
        wall_s=1.0, ttft_p95=ttft_p95, output_tok_per_s=tok_s,
    )


def test_find_knee_picks_highest_passing_concurrency():
    levels = [_level(1, 0.2), _level(4, 0.5), _level(8, 0.9), _level(16, 2.0)]
    knee = find_knee(levels, ttft_slo_s=1.0)
    assert knee is not None and knee.concurrency == 8


def test_find_knee_rejects_levels_with_failures():
    """Throughput at a level that dropped requests is not capacity."""
    levels = [_level(1, 0.2), _level(4, 0.3, failed=2)]
    knee = find_knee(levels, ttft_slo_s=1.0)
    assert knee is not None and knee.concurrency == 1


def test_find_knee_returns_none_when_slo_never_met():
    assert find_knee([_level(1, 5.0), _level(4, 9.0)], ttft_slo_s=1.0) is None


# -- formatting ---------------------------------------------------------


def test_format_table_has_a_row_per_level():
    out = format_table([_level(1, 0.2), _level(4, 0.5)])
    lines = out.splitlines()
    assert len(lines) == 4  # header + rule + 2 rows
    assert "TTFT p95" in lines[0]
