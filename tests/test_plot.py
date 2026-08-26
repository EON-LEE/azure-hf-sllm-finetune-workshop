"""Guarantees for the SVG renderer.

The charts are committed to `docs/results/` and read as evidence, so the ways
they can lie matter more than the ways they can crash: an axis that clips a cap
line, a legend that omits a series, a background that leaves dark text on a
dark page.
"""

from __future__ import annotations

import re

import pytest

from ffsft.serve.plot import (
    Chart,
    Limit,
    Series,
    axis_ticks,
    bar_chart,
    charts_from_reports,
    line_chart,
    nice_ceiling,
    prompt_token_chart,
)

_PLOT = {"left": 74, "right": 720 - 24, "top": 52, "bottom": 380 - 62}


def _level(c, **kw):
    base = {
        "concurrency": c,
        "succeeded": 20,
        "failed": 0,
        "ttft_p50": 1.1,
        "ttft_p95": 1.3,
        "ttft_p99": 1.4,
        "tpot_p50": 0.036,
        "tpot_p95": 0.037,
        "e2e_p50": 5.7,
        "e2e_p95": 5.8,
        "e2e_p99": 5.9,
        "output_tok_per_s": 22.0 * c,
        "requests_per_s": 0.18 * c,
        "output_tokens": 2435,
        "wall_s": 110.0 / c,
        "errors": {},
    }
    base.update(kw)
    return base


def _report(**kw):
    r = {
        "max_tokens": 128,
        "ttft_slo_s": 2.0,
        "levels": [_level(c) for c in (1, 2, 4, 8, 16)],
    }
    r.update(kw)
    return r


def _comparison(**kw):
    r = {
        "max_tokens": 128,
        "deployments": {
            "blue": [
                {"completion_tokens": t, "finish_reason": f}
                for t, f in [(101, "stop"), (128, "length"), (128, "length")]
            ],
            "green": [
                {"completion_tokens": t, "finish_reason": f}
                for t, f in [(111, "stop"), (128, "length"), (29, "stop")]
            ],
        },
    }
    r.update(kw)
    return r


def _points(svg: str) -> list[tuple[float, float]]:
    return [
        tuple(map(float, p.split(",")))
        for group in re.findall(r'points="([^"]+)"', svg)
        for p in group.split()
    ]


def _bars(svg: str) -> list[tuple[float, float, float, float]]:
    return [
        tuple(map(float, m))
        for m in re.findall(
            r'<rect x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)"', svg
        )
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(204.29, 250.0), (1.948, 2.0), (0.0427, 0.05), (134.4, 150.0), (0.0, 1.0), (-3.0, 1.0)],
)
def test_the_axis_ceiling_is_a_number_a_participant_can_read(raw, expected):
    assert nice_ceiling(raw) == pytest.approx(expected)


def test_the_ticks_start_at_zero_and_end_at_the_ceiling():
    assert axis_ticks(250.0) == [0.0, 50.0, 100.0, 150.0, 200.0, 250.0]


def test_the_axis_leaves_room_for_a_limit_line_above_the_data():
    """A cap drawn off-canvas reads as 'nothing hit the cap' -- worse than no cap."""
    svg = bar_chart(
        Chart(
            title="t",
            x_labels=["a"],
            series=[Series("s", [10.0])],
            limits=[Limit("cap", 128.0)],
        )
    )
    ticks = re.findall(r'text-anchor="end">([\d.]+)<', svg)
    assert float(ticks[-1]) >= 128.0


def test_a_flat_series_at_zero_does_not_divide_by_zero():
    svg = line_chart(Chart(title="t", x_labels=["a", "b"], series=[Series("s", [0.0, 0.0])]))
    assert "<polyline" in svg


def test_every_series_and_limit_is_named_in_the_legend():
    svg = line_chart(
        Chart(
            title="t",
            x_labels=["1", "2"],
            series=[Series("blue p50", [1.0, 2.0]), Series("green p50", [1.0, 2.0])],
            limits=[Limit("SLO 2.0s", 2.0)],
        )
    )
    for label in ("blue p50", "green p50", "SLO 2.0s"):
        assert f">{label}<" in svg


def test_a_line_chart_draws_one_point_per_level():
    svg = line_chart(Chart(title="t", x_labels=list("abcde"), series=[Series("s", [1.0] * 5)]))
    assert len(_points(svg)) == 5


def test_a_short_series_draws_what_it_has_rather_than_raising():
    """Two reports need not have swept the same levels."""
    svg = line_chart(Chart(title="t", x_labels=list("abcde"), series=[Series("s", [1.0, 2.0])]))
    assert len(_points(svg)) == 2


def test_points_and_bars_stay_inside_the_plot_area():
    charts = charts_from_reports({"blue": _report(), "green": _report()})
    charts["p.svg"] = prompt_token_chart(_comparison())
    for name, svg in charts.items():
        for x, y in _points(svg):
            assert _PLOT["left"] - 1 <= x <= _PLOT["right"] + 1, name
            assert _PLOT["top"] - 1 <= y <= _PLOT["bottom"] + 1, name
        for x, y, w, h in _bars(svg):
            assert _PLOT["left"] - 1 <= x and x + w <= _PLOT["right"] + 1, name
            assert _PLOT["top"] - 1 <= y and y + h <= _PLOT["bottom"] + 1, name


def test_every_svg_paints_its_own_background():
    """GitHub serves the same file on a white page and a dark one."""
    for svg in charts_from_reports({"blue": _report()}).values():
        assert 'fill="#ffffff"' in svg.split("\n")[1]


def test_the_charts_cover_latency_throughput_and_length():
    """Three questions a knee point cannot answer on its own."""
    assert set(charts_from_reports({"blue": _report()})) == {
        "ttft-vs-concurrency.svg",
        "tpot-vs-concurrency.svg",
        "throughput-vs-concurrency.svg",
        "tokens-per-request.svg",
    }


def test_no_reports_produces_no_charts():
    assert charts_from_reports({}) == {}


def test_the_slo_line_is_taken_from_the_report_not_hardcoded():
    svg = charts_from_reports({"blue": _report(ttft_slo_s=0.75)})["ttft-vs-concurrency.svg"]
    assert ">SLO 0.75s<" in svg


def test_a_report_without_an_slo_still_renders():
    r = _report()
    del r["ttft_slo_s"]
    assert "<polyline" in charts_from_reports({"blue": r})["ttft-vs-concurrency.svg"]


def test_tokens_per_request_divides_by_the_requests_that_succeeded():
    level = _level(1, output_tokens=200, succeeded=10)
    svg = charts_from_reports({"blue": _report(levels=[level])})["tokens-per-request.svg"]
    top = float(re.findall(r'text-anchor="end">([\d.]+)<', svg)[-1])
    x, y, w, h = _bars(svg)[0]
    assert (h / (_PLOT["bottom"] - _PLOT["top"])) * top == pytest.approx(20.0, rel=0.02)


def test_a_level_with_no_successes_does_not_divide_by_zero():
    svg = charts_from_reports({"blue": _report(levels=[_level(1, succeeded=0, output_tokens=0)])})
    assert "<rect" in svg["tokens-per-request.svg"]


def test_the_prompt_chart_counts_the_replies_that_hit_the_cap():
    """The note is the finding: an aggregate over truncated replies is a floor."""
    svg = prompt_token_chart(_comparison())
    assert "6 개 응답 중 3 개가 상한에서 잘렸다" in svg


def test_the_prompt_chart_draws_one_bar_per_prompt_per_deployment():
    assert len(_bars(prompt_token_chart(_comparison()))) == 6


def test_korean_labels_survive_into_the_svg():
    assert "출력 토큰" in prompt_token_chart(_comparison())


def test_a_label_with_markup_in_it_is_escaped():
    svg = line_chart(Chart(title="a & b <c>", x_labels=["1"], series=[Series("x", [1.0])]))
    assert "a &amp; b &lt;c&gt;" in svg
    assert "<c>" not in svg
