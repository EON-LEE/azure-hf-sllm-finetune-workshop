"""Tests for the only reporting channel a run on this workspace actually has.

Azure ML persists stdout and everything under `./outputs` to the workspace blob
store and hands them out over a SAS URL. On a network-isolated storage account
that URL answers `AuthorizationFailure` to anyone outside the VNet, which is
exactly what `MLClient.jobs.stream()` prints here. So a training run's loss,
its peak VRAM and its trainable-parameter count are all written and all
unreadable.

MLflow's tracking service is reachable with an ordinary ARM token and touches
no blob, so it is the channel. These tests pin the two things that make it
useful: the right values reach the right channel, and a broken channel never
takes the run down with it.
"""

from __future__ import annotations

import sys

import pytest

from ffsft.mlflow_report import publish, split_metrics_and_tags


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


# --------------------------------------------------------------------------
# routing
# --------------------------------------------------------------------------


def test_numbers_become_metrics_and_text_becomes_tags():
    metrics, tags = split_metrics_and_tags(
        {"train_loss": 1.234, "steps": 30, "model": "qwen3.8-27b"}
    )
    assert metrics == {"train_loss": 1.234, "steps": 30.0}
    assert tags == {"model": "qwen3.8-27b"}


def test_booleans_are_tags_even_though_bool_is_an_int():
    """`isinstance(True, int)` is True, so the obvious check logs 1.0.

    A pass/fail fact rendered as a metric sits in the MLflow chart next to real
    measurements and reads like one, which is how `nf4_matmul_ok` would end up
    plotted against the loss curve.
    """
    metrics, tags = split_metrics_and_tags({"cuda_available": True, "peak_gb": 27.4})
    assert metrics == {"peak_gb": 27.4}
    assert tags == {"cuda_available": "True"}


def test_none_and_nested_values_survive_as_tags():
    """A report is assembled from several checks and may carry anything."""
    metrics, tags = split_metrics_and_tags({"scratch": None, "targets": ["q_proj"]})
    assert metrics == {}
    assert tags == {"scratch": "None", "targets": "['q_proj']"}


def test_prefix_namespaces_every_key():
    metrics, tags = split_metrics_and_tags(
        {"loss": 0.5, "model": "x"}, prefix="train."
    )
    assert set(metrics) == {"train.loss"}
    assert set(tags) == {"train.model"}


# --------------------------------------------------------------------------
# publish
# --------------------------------------------------------------------------


def test_publish_sends_both_channels(fake_mlflow):
    assert publish({"train_loss": 1.5, "model": "qwen3.8-27b"}, prefix="train.") is True
    assert fake_mlflow.metrics == {"train.train_loss": 1.5}
    assert fake_mlflow.tags == {"train.model": "qwen3.8-27b"}


def test_publish_falls_back_to_a_tag_when_the_metric_channel_is_blocked(monkeypatch):
    """One tenant policy blocks `log_metric`'s storage-authenticated value write
    while leaving `set_tag` untouched (see module docstring). The number must
    still land somewhere a human can read it.
    """
    fake = _FakeMlflow(raise_on="metric")
    monkeypatch.setitem(sys.modules, "mlflow", fake)
    assert publish({"train_loss": 1.5}) is True
    assert fake.metrics == {}
    assert fake.tags == {"train_loss": "1.5"}


def test_publish_reports_failure_without_raising_when_nothing_gets_through(monkeypatch):
    """An unreachable tracking store must not fail a run that trained fine.

    The adapter is already on disk by the time we report; losing the numbers is
    bad, losing the run is worse.
    """
    monkeypatch.setitem(sys.modules, "mlflow", _FakeMlflow(raise_on="all"))
    assert publish({"train_loss": 1.5}) is False


def test_publish_one_bad_metric_does_not_take_the_rest_of_the_report_down(monkeypatch):
    """The old `publish` ran every write inside one shared `try`, so the first
    failing `log_metric` silently discarded every metric and tag queued after
    it -- including `preflight.passed`, which is only ever set by a second
    `publish` call gated on the first one's return value.
    """
    fake = _FakeMlflow(raise_on="metric")
    monkeypatch.setitem(sys.modules, "mlflow", fake)
    assert publish({"a": 1.0, "b": 2.0, "model": "qwen3.8-27b"}) is True
    assert fake.metrics == {}
    assert fake.tags == {"a": "1.0", "b": "2.0", "model": "qwen3.8-27b"}


def test_publish_survives_mlflow_not_being_installed(monkeypatch):
    """Local runs outside the training image have no mlflow at all."""
    monkeypatch.setitem(sys.modules, "mlflow", None)
    assert publish({"train_loss": 1.5}) is False
