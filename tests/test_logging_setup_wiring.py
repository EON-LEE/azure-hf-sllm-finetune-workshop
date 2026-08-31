"""Every console script must quiet the Azure SDK before it prints anything.

`ffsft-lifecycle status` exists to print one small cost table, and shipped
emitting hundreds of `azure.core` request/response INFO lines first, so the
table scrolled off screen. Every throwaway verification script written against
this repo opened with the same `setLevel(ERROR)` calls by hand; the shipped
entry points did not, which is the whole defect.

Wiring one entry point fixes one command. The failure mode worth pinning is the
*next* console script -- added to `[project.scripts]`, never wired, and noisy
again. So this reads `pyproject.toml` rather than a list maintained here: a new
script has to be wired, or these tests name it.

The check is static (an ast walk over the entry point's own body) because
calling these `main()`s for real would mean submitting jobs and opening
sockets. `test_the_ffsft_command_quiets_the_sdk_before_it_runs_anything` is the
one behavioural anchor -- it proves the helper is reached and actually lowers a
logger, so the static half is checking placement and not existence.
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ffsft import cli
from ffsft.logging_setup import QUIET_LOGGER_NAMES, restore_azure_sdk_logs

_ROOT = Path(__file__).resolve().parents[1]
_HELPER = "quiet_azure_sdk_logs"
runner = CliRunner()


def _console_scripts() -> dict[str, str]:
    """`{"ffsft-plot": "ffsft.serve.plot:main"}` from pyproject.toml.

    Hand-parsed rather than `tomllib`-parsed, for the reason given in
    `tests/test_cli_delegates.py`: `requires-python` is >=3.10 because the Azure
    ML ACPT images ship 3.10, and `tomllib` arrived in 3.11.
    """
    text = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    body = text.split("[project.scripts]", 1)[1].split("\n[", 1)[0]
    return dict(re.findall(r'^\s*([\w-]+)\s*=\s*"([^"]+)"\s*$', body, re.M))


def _module_source(dotted: str) -> ast.Module:
    """Parse an entry point's module from disk, without importing it.

    Importing would need the `azure`, `train` and `serve` extras; these tests
    have to pass on a checkout with only `dev`, which is also the environment
    `ffsft models list` has to work in.
    """
    path = _ROOT.joinpath("src", *dotted.split(".")).with_suffix(".py")
    assert path.is_file(), f"{dotted} has no file at {path}"
    return ast.parse(path.read_text(encoding="utf-8"))


def _calls_the_helper(node: ast.AST) -> bool:
    return any(
        isinstance(call.func, ast.Name) and call.func.id == _HELPER
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
    )


def _entry_point_bodies() -> dict[str, ast.AST]:
    """script name -> the function `[project.scripts]` actually lands in.

    `ffsft = "ffsft.cli:app"` names a Typer app rather than a function, so the
    body that runs on every invocation is its `@app.callback()`.
    """
    bodies: dict[str, ast.AST] = {}
    for name, target in _console_scripts().items():
        module, _, attr = target.partition(":")
        tree = _module_source(module)
        wanted = "_main" if attr == "app" else attr
        found = [
            n
            for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == wanted
        ]
        assert found, f"{target}: no `{wanted}` at module level in {module}"
        bodies[name] = found[0]
    return bodies


@pytest.mark.parametrize("script", sorted(_console_scripts()))
def test_every_console_script_quiets_the_azure_sdk_loggers(script):
    """A new entry point in `[project.scripts]` cannot ship noisy."""
    body = _entry_point_bodies()[script]
    assert _calls_the_helper(body), (
        f"{script} never calls {_HELPER}(); import it from ffsft.logging_setup "
        f"and call it after logging.basicConfig(...)"
    )


@pytest.mark.parametrize("script", sorted(_console_scripts()))
def test_the_helper_is_called_after_basic_config_and_not_before(script):
    """Order is the whole difference between quiet and half-quiet.

    The filter half attaches to the root handlers that exist at call time, and
    `basicConfig` is what creates them. Called first, the level half still stops
    the `azure.core` HTTP dumps, but `azure.ai.ml` -- which sets its own level
    and `propagate = False` at import -- keeps printing.
    """
    body = _entry_point_bodies()[script]
    lines = [
        node.lineno
        for node in ast.walk(body)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "basicConfig"
    ]
    if not lines:
        pytest.skip(f"{script} configures no logging of its own")
    helper_lines = [
        node.lineno
        for node in ast.walk(body)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == _HELPER
    ]
    assert max(helper_lines) > min(lines), f"{script} calls {_HELPER}() before basicConfig()"


@pytest.mark.parametrize("script", sorted(_console_scripts()))
def test_the_helper_is_imported_from_the_one_module_that_defines_it(script):
    """Re-deriving the logger list locally is how the two copies drift apart."""
    module = _console_scripts()[script].partition(":")[0]
    tree = _module_source(module)
    imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and any(alias.name == _HELPER for alias in node.names)
    ]
    assert imports, f"{module} does not import {_HELPER} from ffsft.logging_setup"
    for node in imports:
        assert (node.module or "").endswith("logging_setup"), (
            f"{module} imports {_HELPER} from {node.module}, not ffsft.logging_setup"
        )


def test_the_ffsft_command_quiets_the_sdk_before_it_runs_anything():
    """The behavioural anchor: a registry command is enough to lower the levels.

    `ffsft models list` needs no Azure extra, so this runs everywhere -- and the
    callback under test is the one that covers `ffsft deploy`, where the SDK is
    imported by the delegate lookup before that delegate's own `main()` runs.
    """
    for name in QUIET_LOGGER_NAMES:
        logging.getLogger(name).setLevel(logging.NOTSET)
    try:
        result = runner.invoke(cli.app, ["models", "list"])
        assert result.exit_code == 0
        for name in QUIET_LOGGER_NAMES:
            assert logging.getLogger(name).level == logging.ERROR, f"{name} left at INFO"
    finally:
        restore_azure_sdk_logs()


def test_the_callback_import_stays_function_local():
    """`cli.py` must import with only the registry deps present.

    `logging_setup` is stdlib-only and would survive the module-level allowlist
    in `test_cli_delegates.py`, but widening that allowlist to admit it is what
    makes room for the next import that is not stdlib-only.
    """
    tree = _module_source("ffsft.cli")
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").endswith("logging_setup"), (
                "cli.py imports logging_setup at module level; keep it inside the callback"
            )
