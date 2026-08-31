"""Tests for the shared Azure-SDK log quieting helper.

`ffsft-lifecycle status` prints a cost table that only helps if you can see it,
and the Azure SDK emits hundreds of INFO lines of HTTP dumps ahead of it. These
tests pin the three measured facts that make the fix more than a `setLevel`
one-liner, each of which quietly defeats the obvious implementation:

* `azure.ai.ml` holds an explicit INFO level, so quieting the `azure` parent
  never reaches it;
* it also sets `propagate = False` and attaches its own `StreamHandler`, so a
  filter on the root handler never sees its records either;
* it does both at *import* time, which -- with Azure imports function-local by
  convention -- lands after a CLI has configured logging.

Nothing here imports the Azure SDK. `logging.getLogger` names a logger without
creating the package behind it, and the SDK's behaviour is reproduced by doing
to a logger exactly what the SDK was measured doing to its own. That keeps these
tests honest when the `azure` extra is not installed at all.
"""

from __future__ import annotations

import ast
import io
import logging
from contextlib import contextmanager
from pathlib import Path

import pytest

import ffsft.logging_setup
from ffsft.logging_setup import (
    QUIET_LOGGER_NAMES,
    VERBOSE_ENV_VAR,
    AzureNoiseFilter,
    azure_logs_are_verbose,
    quiet_azure_sdk_logs,
    restore_azure_sdk_logs,
)

#: Loggers `azure.ai.ml` configures on itself at import time, measured 2026-08:
#: level INFO, propagate False, one StreamHandler each.
SELF_CONFIGURED_SDK_LOGGERS = (
    "azure.ai.ml",
    "azure.ai.ml._arm_deployments.arm_deployment_executor",
    "azure.ai.ml._utils._endpoint_utils",
)

#: The loudest logger in the original report -- full HTTP request/response dumps.
#: It holds no level of its own and propagates, so it inherits `azure.core`.
HTTP_DUMP_LOGGER = "azure.core.pipeline.policies.http_logging_policy"


def _azure_logger_names() -> set[str]:
    return {
        name
        for name, logger in logging.Logger.manager.loggerDict.items()
        if isinstance(logger, logging.Logger) and name.split(".", 1)[0] == "azure"
    }


@contextmanager
def clean_logging_state():
    """Snapshot and restore global logging state -- there is no conftest.py here.

    Quieting sweeps every `azure` logger that exists, and elsewhere in this suite
    `tests/test_aml_job.py` really does import `azure.ai.ml`. Restoring only the
    names below would leak ERROR levels onto the real SDK loggers for whatever
    runs next, so the snapshot covers all of them.
    """
    names = set(QUIET_LOGGER_NAMES) | set(SELF_CONFIGURED_SDK_LOGGERS)
    names |= {HTTP_DUMP_LOGGER} | _azure_logger_names()
    loggers = {name: logging.getLogger(name) for name in names}
    root = logging.getLogger()
    loggers["<root>"] = root

    def snapshot(logger):
        return (
            logger.level,
            logger.propagate,
            list(logger.handlers),
            {id(h): list(h.filters) for h in logger.handlers},
        )

    saved = {name: snapshot(logger) for name, logger in loggers.items()}
    try:
        yield root
    finally:
        for name, (level, propagate, handlers, filters) in saved.items():
            logger = loggers[name]
            logger.setLevel(level)
            logger.propagate = propagate
            logger.handlers[:] = handlers
            for handler in handlers:
                handler.filters[:] = filters[id(handler)]


@contextmanager
def captured_root_logging():
    """A root handler writing to a buffer, configured the way the CLI mains configure it."""
    with clean_logging_state() as root:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.handlers[:] = [handler]
        root.setLevel(logging.INFO)
        yield stream


def sdk_style_logger(name: str, stream: io.StringIO) -> logging.Logger:
    """Do to a logger exactly what importing `azure.ai.ml` was measured to do to its own."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.handlers[:] = [handler]
    return logger


def test_quiet_azure_sdk_logs_silences_the_sdk_loggers_the_shipped_cli_left_shouting(monkeypatch):
    monkeypatch.delenv(VERBOSE_ENV_VAR, raising=False)
    with clean_logging_state():
        assert quiet_azure_sdk_logs() is True
        for name in QUIET_LOGGER_NAMES:
            assert logging.getLogger(name).level == logging.ERROR


def test_quieting_needs_no_azure_package_to_be_installed_or_imported(monkeypatch):
    """The helper must work on the registry-only CLI paths, where `azure` is absent."""
    monkeypatch.delenv(VERBOSE_ENV_VAR, raising=False)
    tree = ast.parse(Path(ffsft.logging_setup.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):  # every node, not just module level -- no lazy import either
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    offenders = {name for name in imported if name.split(".", 1)[0] == "azure"}
    assert not offenders, f"logging_setup.py must not import the Azure SDK: {offenders}"


def test_the_http_dump_logger_stays_quiet_because_it_inherits_from_azure_core(monkeypatch):
    monkeypatch.delenv(VERBOSE_ENV_VAR, raising=False)
    with clean_logging_state():
        logging.getLogger(HTTP_DUMP_LOGGER).setLevel(logging.NOTSET)
        quiet_azure_sdk_logs()
        assert logging.getLogger(HTTP_DUMP_LOGGER).getEffectiveLevel() == logging.ERROR


def test_a_sdk_logger_with_its_own_handler_is_silenced_though_it_never_reaches_root(monkeypatch):
    """`azure.ai.ml` sets propagate=False, so a root-only filter would miss it entirely."""
    monkeypatch.delenv(VERBOSE_ENV_VAR, raising=False)
    with captured_root_logging() as root_stream:
        sdk_stream = io.StringIO()
        for name in SELF_CONFIGURED_SDK_LOGGERS:
            sdk_style_logger(name, sdk_stream)
        quiet_azure_sdk_logs()
        for name in SELF_CONFIGURED_SDK_LOGGERS:
            logging.getLogger(name).info("HTTP dump that would bury the cost table")
        assert sdk_stream.getvalue() == ""
        assert root_stream.getvalue() == ""


def test_the_filter_still_holds_when_the_sdk_resets_its_own_level_behind_us(monkeypatch):
    """The level half alone is undone by a later import; the handler filter is not."""
    monkeypatch.delenv(VERBOSE_ENV_VAR, raising=False)
    with captured_root_logging():
        sdk_stream = io.StringIO()
        logger = sdk_style_logger("azure.ai.ml", sdk_stream)
        quiet_azure_sdk_logs()
        logger.setLevel(logging.INFO)  # what re-importing the SDK does to the level
        logger.info("Uploading adapter to the datastore")
        assert sdk_stream.getvalue() == ""


def test_the_cost_table_survives_the_quieting(monkeypatch):
    monkeypatch.delenv(VERBOSE_ENV_VAR, raising=False)
    with captured_root_logging() as stream:
        quiet_azure_sdk_logs()
        logging.getLogger("ffsft.deploy.lifecycle").info("BILLING NOW: 1 resource(s)")
        assert "BILLING NOW: 1 resource(s)" in stream.getvalue()


def test_a_real_azure_warning_is_never_swallowed(monkeypatch):
    """Quota rejections and auth failures arrive as WARNING; losing those is worse than noise."""
    monkeypatch.delenv(VERBOSE_ENV_VAR, raising=False)
    with captured_root_logging() as root_stream:
        sdk_stream = io.StringIO()
        logger = sdk_style_logger("azure.ai.ml", sdk_stream)
        quiet_azure_sdk_logs()
        logger.setLevel(logging.INFO)
        logger.warning("quota exceeded")
        logging.getLogger("azure.core").error("InvalidAuthenticationTokenTenant")
        assert "quota exceeded" in sdk_stream.getvalue()
        assert "InvalidAuthenticationTokenTenant" in root_stream.getvalue()


def test_a_logger_merely_named_like_azure_is_not_filtered(monkeypatch):
    monkeypatch.delenv(VERBOSE_ENV_VAR, raising=False)
    with captured_root_logging() as stream:
        quiet_azure_sdk_logs()
        logging.getLogger("azureml_helper").info("not the SDK")
        assert "not the SDK" in stream.getvalue()


def test_quieting_twice_does_not_stack_duplicate_filters(monkeypatch):
    monkeypatch.delenv(VERBOSE_ENV_VAR, raising=False)
    with captured_root_logging():
        sdk_stream = io.StringIO()
        sdk_logger = sdk_style_logger("azure.ai.ml", sdk_stream)
        root_handler = logging.getLogger().handlers[0]
        quiet_azure_sdk_logs()
        quiet_azure_sdk_logs()
        quiet_azure_sdk_logs()
        for handler in (root_handler, sdk_logger.handlers[0]):
            installed = [f for f in handler.filters if isinstance(f, AzureNoiseFilter)]
            assert len(installed) == 1


def test_the_verbose_env_var_restores_the_dumps_that_diagnose_a_failed_deployment(monkeypatch):
    monkeypatch.setenv(VERBOSE_ENV_VAR, "1")
    with captured_root_logging() as stream:
        handler = logging.getLogger().handlers[0]
        assert quiet_azure_sdk_logs() is False
        for name in QUIET_LOGGER_NAMES:
            assert logging.getLogger(name).level == logging.NOTSET
        assert not [f for f in handler.filters if isinstance(f, AzureNoiseFilter)]
        logging.getLogger(HTTP_DUMP_LOGGER).info("Request URL: https://...")
        assert "Request URL" in stream.getvalue()


def test_the_verbose_env_var_undoes_a_quieting_that_already_happened(monkeypatch):
    """Order must not matter: a CLI may quiet before anything reads the environment."""
    monkeypatch.delenv(VERBOSE_ENV_VAR, raising=False)
    with captured_root_logging() as root_stream:
        sdk_stream = io.StringIO()
        sdk_logger = sdk_style_logger("azure.ai.ml", sdk_stream)
        quiet_azure_sdk_logs()
        monkeypatch.setenv(VERBOSE_ENV_VAR, "1")
        quiet_azure_sdk_logs()
        logging.getLogger("azure.core").info("Response status: 200")
        sdk_logger.setLevel(logging.INFO)
        sdk_logger.info("Uploading adapter to the datastore")
        assert "Response status: 200" in root_stream.getvalue()
        assert "Uploading adapter" in sdk_stream.getvalue()


def test_restoring_is_idempotent_and_safe_when_nothing_was_quieted(monkeypatch):
    monkeypatch.delenv(VERBOSE_ENV_VAR, raising=False)
    with captured_root_logging():
        restore_azure_sdk_logs()
        restore_azure_sdk_logs()
        for name in QUIET_LOGGER_NAMES:
            assert logging.getLogger(name).level == logging.NOTSET


@pytest.mark.parametrize("value", ["", "0", "false", "FALSE", "no", "off", " off "])
def test_a_falsey_verbose_value_still_quiets_the_sdk(value):
    assert azure_logs_are_verbose({VERBOSE_ENV_VAR: value}) is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "verbose"])
def test_any_other_verbose_value_opts_back_in(value):
    assert azure_logs_are_verbose({VERBOSE_ENV_VAR: value}) is True


def test_an_unset_verbose_var_means_quiet():
    assert azure_logs_are_verbose({}) is False


def test_quieting_works_before_any_handler_exists_so_call_order_cannot_crash_a_cli(monkeypatch):
    """A CLI calling this before basicConfig must still get the level half, not a traceback."""
    monkeypatch.delenv(VERBOSE_ENV_VAR, raising=False)
    with clean_logging_state() as root:
        root.handlers[:] = []
        assert quiet_azure_sdk_logs() is True
        assert logging.getLogger("azure.core").level == logging.ERROR
