"""`eval/run.py::publish` used to be a second, un-fixed copy of the bug that
`mlflow_report.publish` was written to close (see its module docstring and
`docs/JOURNAL.md` §85).

It wrapped the whole metric loop *and* the three identity tags in one shared
`try`, so on a tenant that blocks `log_metric`'s storage-authenticated value
write, the very first delta metric raised and discarded everything after it
-- not just the eval scores, but `eval.model` / `eval.adapter` /
`eval.benchmarks` too. Confirmed live: smoke job `tidy_bee_b4q7j1479y` finished
with `eval.kobest.base` registered (via `lastvalues`) but zero `eval.*` tags
anywhere on the run.

The fix is not a new fallback -- it is deleting the duplicate and routing
through the one already fixed and tested in `test_train_report.py`.
"""

from __future__ import annotations

import sys

import pytest

from ffsft.eval.run import publish


class _FakeMlflow:
    def __init__(self, raise_on=None):
        self.metrics: dict[str, float] = {}
        self.tags: dict[str, str] = {}
        self._raise_on = raise_on

    def log_metric(self, key, value):
        if self._raise_on in ("metric", "all"):
            raise RuntimeError("tracking store unreachable")
        self.metrics[key] = value

    def set_tag(self, key, value):
        if self._raise_on in ("tag", "all"):
            raise RuntimeError("tracking store unreachable")
        self.tags[key] = value


@pytest.fixture
def fake_mlflow(monkeypatch):
    fake = _FakeMlflow()
    monkeypatch.setitem(sys.modules, "mlflow", fake)
    return fake


_REPORT = {
    "model": "qwen3.5-0.8b",
    "adapter": "outputs/qlora",
    "benchmarks": ["kobest"],
    "comparison": [
        {"task": "kobest", "base": 0.41, "tuned": 0.53, "delta": 0.12, "delta_pct": 29.27},
    ],
}


def test_publish_sends_deltas_and_identity_tags(fake_mlflow):
    publish(_REPORT)
    assert fake_mlflow.metrics == {
        "eval.kobest.base": 0.41,
        "eval.kobest.tuned": 0.53,
        "eval.kobest.delta": 0.12,
    }
    assert fake_mlflow.tags == {
        "eval.model": "qwen3.5-0.8b",
        "eval.adapter": "outputs/qlora",
        "eval.benchmarks": "kobest",
    }


def test_publish_falls_back_to_tags_and_still_reports_identity_when_metrics_are_blocked(
    monkeypatch,
):
    """This is the exact live failure: `log_metric` blocked, and the identity
    tags must land regardless of what the metric loop did.
    """
    fake = _FakeMlflow(raise_on="metric")
    monkeypatch.setitem(sys.modules, "mlflow", fake)
    publish(_REPORT)
    assert fake.metrics == {}
    assert fake.tags == {
        "eval.kobest.base": "0.41",
        "eval.kobest.tuned": "0.53",
        "eval.kobest.delta": "0.12",
        "eval.model": "qwen3.5-0.8b",
        "eval.adapter": "outputs/qlora",
        "eval.benchmarks": "kobest",
    }


def test_publish_skips_a_task_with_no_delta_without_dropping_the_others():
    """`compare()` leaves `delta` (and `base`/`tuned`) as `None` when a task
    only ran in one of the two passes; `split_metrics_and_tags` would stringify
    a bare `None` into a metric-shaped key if it were not filtered first.
    """
    fake = _FakeMlflow()
    report = {
        "model": "m",
        "adapter": None,
        "benchmarks": ["kobest", "kmmlu"],
        "comparison": [
            {"task": "kobest", "base": 0.4, "tuned": None, "delta": None, "delta_pct": None},
            {"task": "kmmlu", "base": 0.3, "tuned": 0.35, "delta": 0.05, "delta_pct": 16.67},
        ],
    }
    import sys as _sys

    _sys.modules["mlflow"] = fake
    try:
        publish(report)
    finally:
        del _sys.modules["mlflow"]
    assert fake.metrics == {
        "eval.kobest.base": 0.4,
        "eval.kmmlu.base": 0.3,
        "eval.kmmlu.tuned": 0.35,
        "eval.kmmlu.delta": 0.05,
    }
    assert fake.tags == {
        "eval.model": "m",
        "eval.adapter": "None",
        "eval.benchmarks": "kobest,kmmlu",
    }


def test_publish_never_raises_when_mlflow_is_not_installed(monkeypatch):
    monkeypatch.setitem(sys.modules, "mlflow", None)
    publish(_REPORT)  # must not raise
