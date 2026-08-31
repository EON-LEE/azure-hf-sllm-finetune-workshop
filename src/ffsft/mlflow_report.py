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

That is true for tags and false for metric *values* on at least one tenant.
`log_metric` registers the metric's name, then writes the value through a path
that authenticates to the workspace's storage account with a shared key; a
policy that disables shared-key storage access (`allowSharedKeyAccess: false`,
seen on this asset's own storage account -- see JOURNAL) makes that write fail
with "Authentication to workspace storage account failed" every time, while
`set_tag`, which never touches storage, keeps working. Confirmed by a throwaway
diagnostic job that called both from the same run: `set_tag` landed, immediately
followed by `log_metric` raising the exact `RestException` above. `publish` used
to run every `log_metric`/`set_tag` call inside one shared `try`, so the first
metric to hit this would silently discard every metric and tag queued after it
-- including `preflight.passed`, which is only ever set from a *second* publish
call gated on the first one's return value. Every metric is now logged and, on
failure, re-sent as a tag holding its stringified value, so the number is still
readable even when this tenant's policy blocks the metric channel outright.

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
    """Send a report to MLflow. Returns whether anything of it got there.

    Never raises. A run that trained correctly must not be marked failed because
    its reporting channel was unavailable. Each value is written independently
    -- one metric failing storage auth (see module docstring) must not take
    every other metric and tag in the same call down with it. A metric whose
    `log_metric` write fails is retried as a tag holding its stringified value,
    since tags use a path that keeps working on a tenant that blocks the
    metric channel.
    """
    try:
        import mlflow
    except ImportError:
        log.info("mlflow unavailable; report is stdout-only")
        return False

    metrics, tags = split_metrics_and_tags(report, prefix)
    if not metrics and not tags:
        return True

    sent = 0
    for name, value in metrics.items():
        try:
            mlflow.log_metric(name, value)
            sent += 1
            continue
        except Exception as exc:  # noqa: BLE001 - reporting must never fail the run
            log.warning(
                "mlflow log_metric(%s) failed, falling back to tag: %s: %s",
                name,
                type(exc).__name__,
                exc,
            )
        try:
            mlflow.set_tag(name, str(value))
            sent += 1
        except Exception as exc:  # noqa: BLE001 - reporting must never fail the run
            log.warning("mlflow set_tag(%s) fallback failed: %s: %s", name, type(exc).__name__, exc)

    for name, value in tags.items():
        try:
            mlflow.set_tag(name, value)
            sent += 1
        except Exception as exc:  # noqa: BLE001 - reporting must never fail the run
            log.warning("mlflow set_tag(%s) failed: %s: %s", name, type(exc).__name__, exc)

    total = len(metrics) + len(tags)
    if sent == 0:
        log.warning("mlflow publish failed: 0/%d values sent", total)
        return False
    log.info("published %d/%d values to MLflow", sent, total)
    return True
