"""Get a run's findings somewhere the submitter can actually read them.

Azure ML writes `user_logs/std_log.txt` and everything under `./outputs` to the
workspace's default blob store and serves them through a SAS URL. On a workspace
whose storage account is network-isolated that URL returns

    <Error><Code>AuthorizationFailure</Code>

to anyone outside the VNet -- which is what `MLClient.jobs.stream()` prints from
a developer laptop here. The job runs, the artifacts are written, and the
numbers are unreadable. A training run that cannot report its loss is worth
nothing.

MLflow is the way out: Azure ML's tracking service takes metrics and tags over
its own endpoint, authorised by an ordinary ARM token, with no blob access
anywhere in the path. So anything a human needs to see goes through here.

Reporting must never be what fails a run, so every failure in this module is
swallowed and logged.

This module sits at the package root rather than under `train/` because
nothing in it is about training. Three separate job kinds report through it --
QLoRA training, the LoRA merge, and the serving load test -- and while it
lived at `ffsft.train.report` the serving side had to import from the training
package to reach it. That import was the only thing tying the two workshop
tracks together in the dependency graph.
"""

from __future__ import annotations

import logging

log = logging.getLogger("ffsft.report")


def split_metrics_and_tags(
    report: dict, prefix: str = ""
) -> tuple[dict[str, float], dict[str, str]]:
    """Sort a flat report into MLflow's two channels.

    MLflow metrics must be floats, so anything else has to become a tag. The
    subtle one is `bool`: it is a subclass of `int`, so an isinstance check for
    numbers accepts it and `True` silently lands as the metric 1.0, which then
    renders as a number in the UI next to real measurements. Booleans are facts,
    not measurements, so they go to tags.
    """
    metrics: dict[str, float] = {}
    tags: dict[str, str] = {}
    for key, value in report.items():
        name = f"{prefix}{key}"
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            tags[name] = str(value)
        else:
            metrics[name] = float(value)
    return metrics, tags


def publish(report: dict, prefix: str = "") -> bool:
    """Send a report to MLflow. Returns whether it got there.

    Never raises. A run that trained correctly must not be marked failed because
    its reporting channel was unavailable.
    """
    try:
        import mlflow
    except ImportError:
        log.info("mlflow unavailable; report is stdout-only")
        return False

    metrics, tags = split_metrics_and_tags(report, prefix)
    try:
        for name, value in metrics.items():
            mlflow.log_metric(name, value)
        for name, value in tags.items():
            mlflow.set_tag(name, value)
    except Exception as exc:  # noqa: BLE001 - reporting must never fail the run
        log.warning("mlflow publish failed: %s: %s", type(exc).__name__, exc)
        return False
    log.info("published %d metrics and %d tags to MLflow", len(metrics), len(tags))
    return True
