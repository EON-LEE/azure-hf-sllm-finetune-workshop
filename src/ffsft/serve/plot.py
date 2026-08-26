"""Render a `ffsft loadtest --output` report as SVG, with no plotting dependency.

The JSON tells you the knee is at concurrency 16. It does not *show* you that
TTFT is flat until then and that the two deployments' TPOT curves lie on top of
each other -- and that second fact is the whole argument of PERFORMANCE §6.
matplotlib is ~60 MB of wheels for four line charts, and the `serve` extra is
deliberately httpx-only so the serving half installs without CUDA. So: stdlib,
and SVG, which diffs in review and renders inline on GitHub.

Every chart paints an explicit background rect. GitHub serves the same file on
a white page and a dark one, and a transparent SVG with dark text is
unreadable on the second.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

BLUE = "#2563eb"
GREEN = "#16a34a"
LIMIT = "#dc2626"
INK = "#1f2937"
MUTED = "#6b7280"
GRID = "#e5e7eb"
PAPER = "#ffffff"


@dataclass
class Series:
    """One line (or one bar colour) on a chart."""

    label: str
    values: list[float]
    color: str = BLUE
    dashed: bool = False


@dataclass
class Limit:
    """A horizontal reference line -- an SLO, or a `max_tokens` cap."""

    label: str
    value: float
    color: str = LIMIT


@dataclass
class Chart:
    title: str
    x_labels: list[str]
    series: list[Series]
    y_label: str = ""
    x_label: str = ""
    limits: list[Limit] = field(default_factory=list)
    y_max: float | None = None
    note: str = ""


def nice_ceiling(value: float) -> float:
    """Round `value` up to 1, 2, 2.5, or 5 times a power of ten.

    An axis that ends at 204.29 gives ticks nobody can read at a glance; one
    that ends at 250 gives 0/50/100/150/200/250.
    """
    if value <= 0:
        return 1.0
    exp = 0
    scaled = float(value)
    while scaled >= 10:
        scaled /= 10
        exp += 1
    while scaled < 1:
        scaled *= 10
        exp -= 1
    for step in (1.0, 1.5, 2.0, 2.5, 5.0, 10.0):
        if scaled <= step:
            return step * (10.0**exp)
    return 10.0 * (10.0**exp)


def axis_ticks(top: float, count: int = 5) -> list[float]:
    """`count` + 1 evenly spaced values from 0 to `top`, inclusive."""
    return [top * i / count for i in range(count + 1)]


def _fmt(value: float) -> str:
    if value == 0:
        return "0"
    if value >= 100:
        return f"{value:.0f}"
    if value >= 10:
        return f"{value:.1f}".rstrip("0").rstrip(".")
    if value >= 1:
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _top_of(chart: Chart) -> float:
    """The y value the axis runs to -- data and limit lines both have to fit.

    A cap line drawn off-canvas is worse than no cap line: the chart then reads
    as "nothing hit the cap".
    """
    if chart.y_max is not None:
        return chart.y_max
    seen = [v for s in chart.series for v in s.values] + [x.value for x in chart.limits]
    return nice_ceiling(max(seen) * 1.05) if seen else 1.0


_W, _H = 720, 380
_L, _R, _T, _B = 74, 24, 52, 62


def _frame(chart: Chart, top: float) -> list[str]:
    """Background, title, gridlines, axis labels -- everything but the data."""
    plot_w, plot_h = _W - _L - _R, _H - _T - _B
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_W}" height="{_H}" '
        f'viewBox="0 0 {_W} {_H}" font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">',
        f'<rect width="{_W}" height="{_H}" fill="{PAPER}"/>',
        f'<text x="{_L}" y="26" font-size="15" font-weight="600" fill="{INK}">'
        f"{escape(chart.title)}</text>",
    ]
    if chart.note:
        out.append(
            f'<text x="{_L}" y="44" font-size="11" fill="{MUTED}">{escape(chart.note)}</text>'
        )
    for tick in axis_ticks(top):
        y = _T + plot_h - (tick / top if top else 0) * plot_h
        out.append(
            f'<line x1="{_L}" y1="{y:.1f}" x2="{_L + plot_w}" y2="{y:.1f}" stroke="{GRID}"/>'
        )
        out.append(
            f'<text x="{_L - 8}" y="{y + 4:.1f}" font-size="11" fill="{MUTED}" '
            f'text-anchor="end">{_fmt(tick)}</text>'
        )
    if chart.y_label:
        out.append(
            f'<text x="{_L - 60}" y="{_T + plot_h / 2:.1f}" font-size="11" fill="{MUTED}" '
            f'text-anchor="middle" transform="rotate(-90 {_L - 60} {_T + plot_h / 2:.1f})">'
            f"{escape(chart.y_label)}</text>"
        )
    if chart.x_label:
        out.append(
            f'<text x="{_L + plot_w / 2:.1f}" y="{_H - 12}" font-size="11" fill="{MUTED}" '
            f'text-anchor="middle">{escape(chart.x_label)}</text>'
        )
    return out


def _legend(chart: Chart) -> list[str]:
    out, x = [], _L
    y = _H - 34
    for s in chart.series:
        dash = ' stroke-dasharray="5 3"' if s.dashed else ""
        out.append(
            f'<line x1="{x}" y1="{y - 4}" x2="{x + 20}" y2="{y - 4}" stroke="{s.color}" '
            f'stroke-width="2.5"{dash}/>'
        )
        out.append(
            f'<text x="{x + 26}" y="{y}" font-size="11" fill="{INK}">{escape(s.label)}</text>'
        )
        x += 34 + int(len(s.label) * 6.4)
    for lim in chart.limits:
        out.append(
            f'<line x1="{x}" y1="{y - 4}" x2="{x + 20}" y2="{y - 4}" stroke="{lim.color}" '
            f'stroke-width="2" stroke-dasharray="6 4"/>'
        )
        out.append(
            f'<text x="{x + 26}" y="{y}" font-size="11" fill="{INK}">{escape(lim.label)}</text>'
        )
        x += 34 + int(len(lim.label) * 6.4)
    return out


def _limits(chart: Chart, top: float) -> list[str]:
    plot_w, plot_h = _W - _L - _R, _H - _T - _B
    out = []
    for lim in chart.limits:
        y = _T + plot_h - (lim.value / top if top else 0) * plot_h
        out.append(
            f'<line x1="{_L}" y1="{y:.1f}" x2="{_L + plot_w}" y2="{y:.1f}" stroke="{lim.color}" '
            f'stroke-width="1.5" stroke-dasharray="6 4"/>'
        )
    return out


def line_chart(chart: Chart) -> str:
    """A grouped line chart. x is categorical -- concurrency 1,2,4,8,16 is not linear."""
    top = _top_of(chart)
    plot_w, plot_h = _W - _L - _R, _H - _T - _B
    n = max(len(chart.x_labels), 1)
    step = plot_w / n
    xs = [_L + step * (i + 0.5) for i in range(n)]

    out = _frame(chart, top)
    for x, label in zip(xs, chart.x_labels, strict=False):
        out.append(
            f'<text x="{x:.1f}" y="{_T + plot_h + 18}" font-size="11" fill="{MUTED}" '
            f'text-anchor="middle">{escape(label)}</text>'
        )
    out += _limits(chart, top)
    for s in chart.series:
        pts = [
            f"{x:.1f},{_T + plot_h - (v / top if top else 0) * plot_h:.1f}"
            for x, v in zip(xs, s.values, strict=False)
        ]
        dash = ' stroke-dasharray="5 3"' if s.dashed else ""
        out.append(
            f'<polyline points="{" ".join(pts)}" fill="none" stroke="{s.color}" '
            f'stroke-width="2.5" stroke-linejoin="round"{dash}/>'
        )
        for pt in pts:
            cx, cy = pt.split(",")
            out.append(f'<circle cx="{cx}" cy="{cy}" r="3.5" fill="{s.color}"/>')
    out += _legend(chart)
    out.append("</svg>")
    return "\n".join(out)


def bar_chart(chart: Chart) -> str:
    """Grouped bars, one group per x label."""
    top = _top_of(chart)
    plot_w, plot_h = _W - _L - _R, _H - _T - _B
    n = max(len(chart.x_labels), 1)
    step = plot_w / n
    k = max(len(chart.series), 1)
    bar_w = step * 0.72 / k

    out = _frame(chart, top)
    for i, label in enumerate(chart.x_labels):
        out.append(
            f'<text x="{_L + step * (i + 0.5):.1f}" y="{_T + plot_h + 18}" font-size="11" '
            f'fill="{MUTED}" text-anchor="middle">{escape(label)}</text>'
        )
    out += _limits(chart, top)
    for j, s in enumerate(chart.series):
        for i, v in enumerate(s.values):
            h = (v / top if top else 0) * plot_h
            x = _L + step * (i + 0.5) - (step * 0.72 / 2) + bar_w * j
            out.append(
                f'<rect x="{x:.1f}" y="{_T + plot_h - h:.1f}" width="{bar_w:.1f}" '
                f'height="{h:.1f}" fill="{s.color}" rx="1.5"/>'
            )
    out += _legend(chart)
    out.append("</svg>")
    return "\n".join(out)


def _levels(report: dict) -> list[dict]:
    return report.get("levels", [])


def charts_from_reports(reports: dict[str, dict]) -> dict[str, str]:
    """`{label: report}` -> `{filename: svg}`.

    One report renders its own curves; two or more render on shared axes, which
    is the only way to see that the TPOT lines coincide.
    """
    if not reports:
        return {}
    labels = list(reports)
    palette = {labels[0]: BLUE}
    palette.update({name: GREEN for name in labels[1:]})
    first = reports[labels[0]]
    x = [str(lvl["concurrency"]) for lvl in _levels(first)]
    slo = first.get("ttft_slo_s")
    cap = first.get("max_tokens")

    def col(name: str, key: str) -> list[float]:
        return [float(lvl[key]) for lvl in _levels(reports[name])]

    out = {}
    out["ttft-vs-concurrency.svg"] = line_chart(
        Chart(
            title="TTFT — 첫 토큰까지 (p50 실선 / p95 점선)",
            note="p95 가 SLO 선을 넘는 직전이 knee. 두 배포 모두 c=16 까지 여유가 있다.",
            x_labels=x,
            x_label="동시성 (concurrent requests)",
            y_label="초",
            series=[
                s
                for name in labels
                for s in (
                    Series(f"{name} p50", col(name, "ttft_p50"), palette[name]),
                    Series(f"{name} p95", col(name, "ttft_p95"), palette[name], dashed=True),
                )
            ],
            limits=[Limit(f"SLO {slo}s", float(slo))] if slo else [],
        )
    )
    out["tpot-vs-concurrency.svg"] = line_chart(
        Chart(
            title="TPOT — 토큰당 생성 시간 (p50)",
            note="두 선이 겹친다. 병합 가중치는 구조·파라미터 수·dtype 이 베이스와 같다.",
            x_labels=x,
            x_label="동시성",
            y_label="초 / 토큰",
            series=[Series(name, col(name, "tpot_p50"), palette[name]) for name in labels],
        )
    )
    out["throughput-vs-concurrency.svg"] = line_chart(
        Chart(
            title="처리량 — 출력 tok/s (실선) 과 req/s (점선, ×100)",
            note="tok/s 는 green 이 낮고 req/s 는 green 이 높다. 같은 현상의 두 얼굴이다.",
            x_labels=x,
            x_label="동시성",
            y_label="출력 tok/s   ·   req/s ×100",
            series=[
                s
                for name in labels
                for s in (
                    Series(f"{name} tok/s", col(name, "output_tok_per_s"), palette[name]),
                    Series(
                        f"{name} req/s×100",
                        [v * 100 for v in col(name, "requests_per_s")],
                        palette[name],
                        dashed=True,
                    ),
                )
            ],
        )
    )
    out["tokens-per-request.svg"] = bar_chart(
        Chart(
            title="요청당 출력 토큰 — 처리량 차이의 진짜 원인",
            note="tok/s 가 낮은 쪽이 느린 게 아니라 짧다. 토큰이 적으면 tok/s 도 낮게 찍힌다.",
            x_labels=x,
            x_label="동시성",
            y_label="토큰 / 요청",
            series=[
                Series(
                    name,
                    [
                        lvl["output_tokens"] / max(lvl["succeeded"], 1)
                        for lvl in _levels(reports[name])
                    ],
                    palette[name],
                )
                for name in labels
            ],
            limits=[Limit(f"max_tokens={cap}", float(cap))] if cap else [],
        )
    )
    return out


def prompt_token_chart(report: dict) -> str:
    """Per-prompt output tokens from `scripts/compare_deployments.py`.

    The aggregate chart shows a gap; this one shows where it comes from. Bars
    sitting on the cap line are truncated answers, and a truncated answer has
    no measured length at all -- which is the whole reason this chart exists.
    """
    deployments = report.get("deployments", {})
    labels = list(deployments)
    palette = {labels[0]: BLUE} if labels else {}
    palette.update({name: GREEN for name in labels[1:]})
    cap = report.get("max_tokens")
    n = max((len(v) for v in deployments.values()), default=0)
    capped = sum(
        1 for v in deployments.values() for r in v if r.get("finish_reason") == "length"
    )
    total = sum(len(v) for v in deployments.values())
    return bar_chart(
        Chart(
            title="프롬프트별 출력 토큰 — 총합 차이가 어디서 오는지",
            note=f"{total} 개 응답 중 {capped} 개가 상한에서 잘렸다. "
            "잘린 막대는 길이가 아니라 하한이다.",
            x_labels=[f"P{i}" for i in range(n)],
            x_label="loadtest 기본 프롬프트 (DEFAULT_PROMPTS)",
            y_label="출력 토큰",
            series=[
                Series(
                    name,
                    [float(r["completion_tokens"]) for r in deployments[name]],
                    palette[name],
                )
                for name in labels
            ],
            limits=[Limit(f"max_tokens={cap}", float(cap))] if cap else [],
        )
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="ffsft-plot",
        description="Render `ffsft loadtest --output` JSON as SVG charts (no matplotlib).",
    )
    ap.add_argument(
        "reports",
        nargs="*",
        metavar="LABEL=REPORT.json",
        help="one or more reports; a bare path is labelled by its filename stem",
    )
    ap.add_argument("--out-dir", default="docs/results", help="where the .svg files land")
    ap.add_argument(
        "--prompts",
        default=None,
        metavar="COMPARE.json",
        help="a scripts/compare_deployments.py report -> tokens-per-prompt.svg",
    )
    args = ap.parse_args()

    reports: dict[str, dict] = {}
    for item in args.reports:
        label, _, path = item.partition("=")
        if not path:
            label, path = Path(label).stem, label
        try:
            reports[label] = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"cannot read {path}: {exc}", file=sys.stderr)
            return 1

    charts = charts_from_reports(reports)
    if args.prompts:
        try:
            charts["tokens-per-prompt.svg"] = prompt_token_chart(
                json.loads(Path(args.prompts).read_text(encoding="utf-8"))
            )
        except (OSError, ValueError, KeyError) as exc:
            print(f"cannot read {args.prompts}: {exc}", file=sys.stderr)
            return 1
    if not charts:
        print("nothing to plot: pass a loadtest report, --prompts, or both", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, svg in charts.items():
        (out_dir / name).write_text(svg + "\n", encoding="utf-8")
        print(f"wrote {out_dir / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
