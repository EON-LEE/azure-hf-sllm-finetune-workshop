"""Backwards-compatible alias for `ffsft.mlflow_report`.

The module moved to the package root because reporting is not a training
concern -- see the docstring there. Import from `ffsft.mlflow_report` in new
code; this shim exists so that job scripts baked into an already-built
training image keep working after the move.
"""

from __future__ import annotations

from ..mlflow_report import log, publish, split_metrics_and_tags

__all__ = ["log", "publish", "split_metrics_and_tags"]
