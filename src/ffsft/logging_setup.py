"""Keep the Azure SDK's INFO chatter from burying the output the user asked for.

`ffsft-lifecycle status` exists to print one small cost table. As shipped it
printed hundreds of lines of `azure.core` HTTP request and response dumps first,
so the table scrolled off screen -- on the one command whose entire job is to
make you notice that something is billing. Every throwaway verification script
written against this repo opened by hand with the same four `setLevel(ERROR)`
calls; that knowledge never reached the shipped entry points, which is the only
reason this module exists.

Two measured details decide the implementation, and both look like needless
complexity until you re-measure them:

1. **Naming the parent is not enough.** `azure.ai.ml` calls `setLevel(INFO)` on
   its own logger (and on four of its children) at import time. An explicit
   level short-circuits the effective-level walk, so `getLogger("azure")` set to
   ERROR never reaches them.
2. **Setting the level early does not survive, and those loggers do not
   propagate.** Azure imports in this repo are function-local by convention (see
   the docstring of `tests/test_aml_job.py`), so the SDK is imported *after* a
   CLI configures logging -- and that import resets `azure.ai.ml` back to INFO,
   sets `propagate = False`, and attaches a `StreamHandler` of its own. Records
   from it therefore never reach the root handler, so a filter installed there
   cannot see them either.

Levels alone cannot do the job, and neither can a root filter alone, which is
why this is not the four-line snippet it looks like it should be:

* Setting the level on the names above permanently silences the high-volume
  `azure.core` HTTP dumps -- the hundreds of lines in the original report. Those
  loggers hold no explicit level and do propagate, so nothing clobbers them and
  this half works no matter when it is called.
* Every `azure` logger that already exists is quieted directly, and the filter
  is installed on its own handlers as well as on the root handlers. A handler
  filter runs *after* record creation, so it still applies to a logger that has
  reset its own level behind our back.

One gap is left open on purpose rather than papered over: a logger the SDK
creates *after* this runs cannot be reached, because there is nothing there to
attach to yet. `azure.ai.ml` imported later keeps its own stderr handler until
this is called again -- which is cheap and idempotent, so an entry point that
wants the last word can call it once more after building its client.

`FFSFT_VERBOSE_AZURE=1` puts all of it back. That opt-out is not decoration.
When a deployment fails, the SDK's HTTP dumps are routinely the only evidence
that exists -- and this repo has already lost a failure permanently by tearing
an endpoint down before its logs were captured. A log switch with only an off
position is how that happens a second time.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

#: Logger names quieted by :func:`quiet_azure_sdk_logs`, matching the set every
#: ad-hoc verification script converged on. Naming the children as well as the
#: parent is not redundancy -- see note 1 in the module docstring.
QUIET_LOGGER_NAMES = ("azure", "azure.core", "azure.identity", "azure.ai.ml")

#: Any value except 0/false/no/off restores the SDK's own logging.
VERBOSE_ENV_VAR = "FFSFT_VERBOSE_AZURE"

#: Azure records at or above this level are always shown. A quota rejection or
#: an auth failure arrives as a WARNING, and swallowing those to tidy up the
#: screen would trade a scrolling problem for a silent one.
QUIET_THRESHOLD = logging.WARNING

_FALSEY = frozenset({"", "0", "false", "no", "off"})


class AzureNoiseFilter(logging.Filter):
    """Drops sub-WARNING Azure SDK records at the handler.

    Lives on the handler rather than the logger because handler filters run
    after record creation, and so still apply to a logger that has reset its own
    level to INFO behind our back.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= QUIET_THRESHOLD:
            return True
        # Prefix match on the first dotted segment, so `azure.core` matches and a
        # hypothetical `azureml_something` does not.
        return record.name.split(".", 1)[0] != "azure"


def azure_logs_are_verbose(env: Mapping[str, str] | None = None) -> bool:
    """Whether the operator has asked to keep the SDK's own logs."""
    source = os.environ if env is None else env
    return source.get(VERBOSE_ENV_VAR, "").strip().lower() not in _FALSEY


def quiet_azure_sdk_logs(env: Mapping[str, str] | None = None) -> bool:
    """Silence Azure SDK INFO logs. Returns True if quieted, False if left verbose.

    Idempotent, and safe to call in either direction: with `FFSFT_VERBOSE_AZURE`
    set it actively restores anything a previous call silenced.

    Call it *after* `logging.basicConfig(...)`. The filter half attaches to the
    root handlers that exist at call time, and basicConfig is what creates them;
    called first, this still stops the `azure.core` HTTP dumps by level, but the
    `azure.ai.ml` lines described in the module docstring would survive.

    Imports no Azure package -- `logging.getLogger` happily names a logger that
    nothing has created yet, so this stays importable with the `azure` extra
    absent, and stays cheap on the registry-only CLI paths.
    """
    if azure_logs_are_verbose(env):
        restore_azure_sdk_logs()
        return False

    for name in QUIET_LOGGER_NAMES:
        logging.getLogger(name).setLevel(logging.ERROR)
    # Anything the SDK has already created needs handling one by one: an explicit
    # level ignores the parent, and `propagate = False` puts the record out of
    # reach of the root handler entirely.
    for logger in _existing_azure_loggers():
        logger.setLevel(logging.ERROR)
        _install_filter(logger.handlers)
    _install_filter(logging.getLogger().handlers)
    return True


def _existing_azure_loggers() -> list[logging.Logger]:
    """Every `azure*` logger that exists right now. Names nothing, so it needs no SDK."""
    found = []
    for name, logger in list(logging.Logger.manager.loggerDict.items()):
        # loggerDict holds PlaceHolder objects for unrealised parents; they carry
        # neither a level nor handlers and cannot be configured.
        if isinstance(logger, logging.Logger) and name.split(".", 1)[0] == "azure":
            found.append(logger)
    return found


def _install_filter(handlers: list[logging.Handler]) -> None:
    for handler in handlers:
        if not any(isinstance(f, AzureNoiseFilter) for f in handler.filters):
            handler.addFilter(AzureNoiseFilter())


def restore_azure_sdk_logs() -> None:
    """Undo :func:`quiet_azure_sdk_logs`, returning the SDK loggers to the root level."""
    for name in QUIET_LOGGER_NAMES:
        logging.getLogger(name).setLevel(logging.NOTSET)
    for logger in _existing_azure_loggers():
        logger.setLevel(logging.NOTSET)
        _remove_filter(logger.handlers)
    _remove_filter(logging.getLogger().handlers)


def _remove_filter(handlers: list[logging.Handler]) -> None:
    for handler in handlers:
        for noise_filter in [f for f in handler.filters if isinstance(f, AzureNoiseFilter)]:
            handler.removeFilter(noise_filter)
