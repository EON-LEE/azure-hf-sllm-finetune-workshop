"""`ffsft <x>` and `ffsft-<x>` must stay the same code path.

Before the delegating commands existed, `ffsft --help` listed three read-only
registry commands, while everything a workshop participant actually runs lived
behind six other console scripts and two files in `scripts/`. Eleven entry
points, and the one on PATH could not find the other ten.

The delegates are one line each, so the risk is not that they break -- it is
that they quietly stop pointing at the same function `[project.scripts]` points
at, and a lab instruction and a console script start doing different things.
`test_a_delegate_names_the_module_its_console_script_names` is the test that
matters here: it reads `pyproject.toml` and refuses to let the two drift.

Nothing in this file imports a delegate's module. That is deliberate -- these
tests have to pass on a checkout with only the `dev` extra installed, which is
also the environment `ffsft models list` has to work in.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from ffsft import cli

_ROOT = Path(__file__).resolve().parents[1]
runner = CliRunner()


def _console_scripts() -> dict[str, str]:
    """`{"ffsft-loadtest": "ffsft.serve.loadtest:main"}` from pyproject.toml.

    Hand-parsed rather than `tomllib`-parsed: `requires-python` is >=3.10 because
    the Azure ML ACPT images ship 3.10, and `tomllib` arrived in 3.11.
    """
    text = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    body = text.split("[project.scripts]", 1)[1].split("\n[", 1)[0]
    return dict(re.findall(r'^\s*([\w-]+)\s*=\s*"([^"]+)"\s*$', body, re.M))


#: command path under `ffsft` -> the module its console script names.
DELEGATES = {
    ("eval",): "ffsft.eval.run",
    ("deploy",): "ffsft.deploy.endpoint",
    ("lifecycle",): "ffsft.deploy.lifecycle",
    ("loadtest",): "ffsft.serve.loadtest",
    ("plot",): "ffsft.serve.plot",
    ("serve-local",): "ffsft.serve.local",
    ("merge", "local"): "ffsft.deploy.merge",
}


def _record(monkeypatch) -> dict:
    """Replace the import step so a delegate can be driven without its extra."""
    seen: dict = {}

    def fake_module_main(module: str, extra: str):
        seen["module"] = module
        seen["extra"] = extra

        def main():
            seen["argv"] = list(sys.argv)
            return seen.get("code")

        return main

    monkeypatch.setattr(cli, "_module_main", fake_module_main)
    return seen


# --- the drift guard -------------------------------------------------------


@pytest.mark.parametrize(("path", "module"), sorted(DELEGATES.items()))
def test_a_delegate_names_the_module_its_console_script_names(monkeypatch, path, module):
    scripts = _console_scripts()
    target = f"{module}:main"
    assert target in scripts.values(), f"{target} is no longer a console script"

    seen = _record(monkeypatch)
    assert runner.invoke(cli.app, [*path]).exit_code == 0
    assert seen["module"] == module


def test_every_console_script_is_reachable_from_the_ffsft_command(monkeypatch):
    """A seventh console script must not appear with no way to reach it."""
    delegated = {f"{m}:main" for m in DELEGATES.values()}
    unreachable = {
        name: target
        for name, target in _console_scripts().items()
        if name != "ffsft" and target not in delegated
    }
    assert not unreachable, f"no `ffsft ...` command delegates to: {unreachable}"


def test_the_submit_scripts_are_reachable_too():
    """The two `scripts/*.py` entry points are half the reason this exists."""
    for name in ("submit_training", "submit_merge"):
        assert (_ROOT / "scripts" / f"{name}.py").is_file()
    assert callable(cli._script_main("submit_training"))
    assert callable(cli._script_main("submit_merge"))


# --- what `_run` guarantees ------------------------------------------------


def test_the_delegate_hands_the_tail_of_the_command_line_over_untouched(monkeypatch):
    """Typer must not eat `--concurrency` or reject an option it has never seen."""
    seen = _record(monkeypatch)
    result = runner.invoke(
        cli.app, ["loadtest", "--base-url", "http://x/v1", "--concurrency", "8", "--json"]
    )
    assert result.exit_code == 0
    assert seen["argv"][1:] == ["--base-url", "http://x/v1", "--concurrency", "8", "--json"]


def test_help_reaches_the_delegate_rather_than_being_answered_by_typer(monkeypatch):
    """`ffsft loadtest --help` has to show the delegate's flags, not Typer's."""
    seen = _record(monkeypatch)
    assert runner.invoke(cli.app, ["loadtest", "--help"]).exit_code == 0
    assert seen["argv"][1:] == ["--help"]


def test_the_delegate_is_told_it_is_called_ffsft(monkeypatch):
    """argparse builds its usage line from argv[0]; it should not read `cli.py`."""
    seen = _record(monkeypatch)
    runner.invoke(cli.app, ["merge", "local", "--adapter", "x"])
    assert seen["argv"][0] == "ffsft merge local"


def test_a_nonzero_return_becomes_the_exit_code(monkeypatch):
    seen = _record(monkeypatch)
    seen["code"] = 3
    assert runner.invoke(cli.app, ["deploy", "check"]).exit_code == 3


def test_returning_none_is_success(monkeypatch):
    _record(monkeypatch)
    assert runner.invoke(cli.app, ["lifecycle", "status"]).exit_code == 0


def test_sys_argv_is_restored_when_the_delegate_raises(monkeypatch):
    """A delegate that dies must not leave the process holding a rewritten argv."""

    def boom():
        raise RuntimeError("delegate died")

    before = list(sys.argv)
    with pytest.raises(RuntimeError):
        cli._run(boom, ["--flag"], "ffsft boom")
    assert sys.argv == before


def test_sys_argv_is_restored_after_a_normal_call():
    before = list(sys.argv)
    with pytest.raises(typer.Exit):
        cli._run(lambda: 0, ["--flag"], "ffsft ok")
    assert sys.argv == before


# --- failures a participant will actually hit ------------------------------


def test_a_missing_extra_is_reported_as_the_extra_to_install(capsys):
    """`No module named 'httpx'` does not tell anyone the fix is `--extra serve`."""
    with pytest.raises(typer.Exit) as exc:
        cli._module_main("ffsft._no_such_module", "serve")
    assert exc.value.exit_code == 1
    assert "uv sync --extra serve" in capsys.readouterr().out


def test_a_missing_submit_script_says_the_scripts_ship_in_the_repo(capsys):
    """An installed wheel has no `scripts/`; say so, do not raise FileNotFoundError."""
    with pytest.raises(typer.Exit) as exc:
        cli._script_main("no_such_script")
    assert exc.value.exit_code == 1
    assert "checkout" in capsys.readouterr().out


# --- the help screen is the workshop's table of contents -------------------


def test_help_lists_commands_in_lab_order():
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    seen = [n for n in cli._COMMAND_ORDER if n in result.output]
    assert seen == cli._COMMAND_ORDER
    positions = [result.output.index(n) for n in cli._COMMAND_ORDER]
    assert positions == sorted(positions)


def test_a_command_left_out_of_the_order_list_still_lists():
    """Ordering is presentation; forgetting to add a name must not hide it."""
    app = typer.Typer(cls=cli._LabOrderGroup)
    app.command("zzz-unlisted")(lambda: None)
    assert "zzz-unlisted" in runner.invoke(app, ["--help"]).output


# --- the import contract ---------------------------------------------------


def test_the_cli_module_imports_nothing_heavy_at_module_level():
    """`ffsft models list` runs on a laptop with no train/azure/serve extra.

    A module-level `import torch` here would break every command for everyone,
    so the delegates' imports live inside the functions. This reads the source
    rather than `sys.modules`, which another test may already have populated.
    """
    allowed = {
        "__future__",
        "collections.abc",
        "typer",
        "rich.console",
        "rich.table",
        ".deploy",
        ".eval",
        ".models",
    }
    tree = ast.parse((_ROOT / "src" / "ffsft" / "cli.py").read_text(encoding="utf-8"))
    found = set()
    for node in tree.body:  # module level only -- function bodies are not walked
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add("." * node.level + (node.module or ""))
    assert found <= allowed, f"new module-level import in cli.py: {found - allowed}"
