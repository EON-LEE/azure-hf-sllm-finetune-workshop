"""`ffsft` command line entry point.

Two kinds of command live here.

The registry commands (`models`, `serving`, `bench`) read `configs/*.yaml` and
answer questions about what can be trained, served and measured. They are the
original contents of this module.

The rest **delegate**. Until they existed, `ffsft --help` listed three
read-only commands while every command a workshop participant actually needs
lived behind six other console scripts and two files in `scripts/` -- eleven
entry points, and no way to discover them from the one that is on PATH. Each
delegating command swaps `sys.argv` and calls the same `main()` the console
script calls, so `ffsft loadtest` and `ffsft-loadtest` are the same code path
and cannot drift. The old names all still work; the labs use them.

**Imports for the delegates stay inside the functions.** This module has to
import with only the registry dependencies present -- `uv run ffsft models
list` runs on a laptop with no `train`, `azure` or `serve` extra installed,
and a module-level `import torch` here would break every command for everyone.
"""

from __future__ import annotations

from collections.abc import Callable

import typer
from rich.console import Console
from rich.table import Table

from .deploy import get_serving_registry
from .eval import get_benchmark_registry
from .models import Provider, TuningMethod, get_registry

#: `ffsft --help` is the first thing a workshop participant runs, so it is
#: ordered by the lab it belongs to rather than by how Click happens to collect
#: commands -- which is registration order for plain commands and *after all of
#: them* for sub-groups, putting `models` (Lab 0) below `loadtest` (Lab 6).
#: A name missing from this list still lists, at the end.
_COMMAND_ORDER = [
    "models",       # Lab 0  what can be trained
    "serving",      # Lab 0  what can be served
    "bench",        # Lab 0  what can be measured
    "train",        # Lab 2
    "eval",         # Lab 3
    "deploy",       # Lab 5
    "lifecycle",    # Lab 5, and Lab 7 -- `down` is not optional
    "loadtest",     # Lab 6
    "plot",         # Lab 6  the report as SVG
    "merge",        # Lab 8
    "serve-local",  # no GPU, no Azure
]


class _LabOrderGroup(typer.core.TyperGroup):
    def list_commands(self, ctx) -> list[str]:
        names = super().list_commands(ctx)
        return sorted(names, key=lambda n: (_COMMAND_ORDER.index(n)
                                            if n in _COMMAND_ORDER else len(_COMMAND_ORDER), n))


app = typer.Typer(
    cls=_LabOrderGroup,
    add_completion=False,
    help="Fabric + Foundry sLLM fine-tuning asset.",
    no_args_is_help=True,
)
models_app = typer.Typer(no_args_is_help=True, help="Inspect the swappable model registry.")
serving_app = typer.Typer(no_args_is_help=True, help="Inspect the swappable serving patterns.")
eval_app = typer.Typer(no_args_is_help=True, help="Inspect the Korean benchmark registry.")
app.add_typer(models_app, name="models")
app.add_typer(serving_app, name="serving")
app.add_typer(eval_app, name="bench")

console = Console()


@app.callback()
def _main() -> None:
    """Runs before every `ffsft` command, registry ones included.

    The delegates each quiet the SDK themselves, but `ffsft deploy ...` imports
    `azure.ai.ml` through `_module_main` *before* that delegate's `main()` gets
    to run, and that import writes its own INFO handler on the way in. Quieting
    here means the import itself is already covered.

    The import is function-local like every other one below it: this module has
    to import with only the registry deps present, and `tests/test_cli_delegates
    .py::test_the_cli_module_imports_nothing_heavy_at_module_level` walks the
    module body to prove it. `logging_setup` is stdlib-only and would pass that
    walk, but adding a name to the allowlist to make room for it would loosen
    the guard for the next import too.
    """
    from .logging_setup import quiet_azure_sdk_logs

    quiet_azure_sdk_logs()


def _fmt_params(spec) -> str:
    if spec.params_b is None:
        return "-"
    if spec.active_params_b:
        return f"{spec.params_b:g}B (A{spec.active_params_b:g}B)"
    return f"{spec.params_b:g}B"


@models_app.command("list")
def models_list(
    provider: str | None = typer.Option(None, help="Filter by provider, e.g. hf."),
    method: str | None = typer.Option(None, help="Filter by tuning method, e.g. qlora."),
    commercial_only: bool = typer.Option(False, help="Only models cleared for commercial use."),
    max_params_b: float | None = typer.Option(None, help="Cap on total parameters, in billions."),
) -> None:
    """List every model that can be swapped into a training run."""
    registry = get_registry()
    specs = registry.filter(
        provider=Provider(provider) if provider else None,
        method=TuningMethod(method) if method else None,
        commercial_use=True if commercial_only else None,
        max_params_b=max_params_b,
    )

    table = Table(title=f"models ({len(specs)}/{len(registry)})", highlight=True)
    for col in ("key", "params", "provider", "tuning", "korean", "license", "id"):
        table.add_column(col, overflow="fold")

    for s in specs:
        table.add_row(
            s.key,
            _fmt_params(s),
            s.provider.value,
            ",".join(m.value for m in s.supports) or "-",
            s.korean_tier.value,
            s.license,
            s.hf_id or s.foundry_model or "-",
        )
    console.print(table)


@models_app.command("show")
def models_show(key: str = typer.Argument(..., help="Model key, e.g. qwen3.8-27b.")) -> None:
    """Show everything the registry knows about one model."""
    spec = get_registry().get(key)

    table = Table(title=spec.display_name, show_header=False, highlight=True)
    table.add_column("field", style="bold")
    table.add_column("value", overflow="fold")

    rows = [
        ("key", spec.key),
        ("provider", spec.provider.value),
        ("hf_id", spec.hf_id or "-"),
        ("foundry_model", spec.foundry_model or "-"),
        ("params", _fmt_params(spec)),
        ("context_length", f"{spec.context_length:,}" if spec.context_length else "-"),
        ("license", spec.license),
        ("commercial_use", str(spec.commercial_use)),
        ("tuning methods", ", ".join(m.value for m in spec.supports) or "none (inference only)"),
        ("recommended recipe", spec.recommended_method.value if spec.recommended_method else "-"),
        ("korean tier", spec.korean_tier.value),
        ("recommended SKU", spec.recommended_sku or "-"),
    ]
    vram = {k: v for k, v in spec.vram_gb.model_dump().items() if v}
    if vram:
        rows.append(("VRAM (GB)", ", ".join(f"{k}={v}" for k, v in vram.items())))
    if spec.chat_template_kwargs:
        rows.append(("chat template kwargs", str(spec.chat_template_kwargs)))
    if spec.korean_notes:
        rows.append(("korean notes", spec.korean_notes.strip()))
    if spec.notes:
        rows.append(("notes", spec.notes.strip()))
    if spec.source_url:
        rows.append(("source", spec.source_url))

    for field, value in rows:
        table.add_row(field, value)
    console.print(table)


@models_app.command("trainable")
def models_trainable() -> None:
    """Show which models this asset can actually fine-tune, and which it cannot.

    This is the honest answer to 'can we fine-tune MAI?' — no, and the registry
    records why rather than silently omitting it.
    """
    registry = get_registry()
    table = Table(title="fine-tuning eligibility", highlight=True)
    for col in ("key", "trainable", "how", "why not"):
        table.add_column(col, overflow="fold")

    for spec in sorted(registry, key=lambda s: (not s.trainable, s.key)):
        table.add_row(
            spec.key,
            "yes" if spec.trainable else "NO",
            ", ".join(m.value for m in spec.supports) or "-",
            "" if spec.trainable else (spec.notes.strip().split(".")[0] + "."),
        )
    console.print(table)


@serving_app.command("list")
def serving_list(
    low_priority_only: bool = typer.Option(
        False, help="Only patterns that run without dedicated GPU quota."
    ),
    load_testable: bool = typer.Option(False, help="Only interactive OpenAI-compatible patterns."),
) -> None:
    """List the ways a tuned model can be served."""
    registry = get_serving_registry()
    specs = registry.filter(
        low_priority_only=low_priority_only,
        load_testable=True if load_testable else None,
    )

    table = Table(title=f"serving patterns ({len(specs)}/{len(registry)})", highlight=True)
    for col in ("key", "surface", "engine", "openai", "low-pri", "scale-0", "default SKU"):
        table.add_column(col, overflow="fold")

    for s in specs:
        table.add_row(
            s.key,
            s.surface.value,
            s.engine.value,
            "yes" if s.openai_compatible else "-",
            "[green]yes[/green]" if s.allows_low_priority else "[red]NO[/red]",
            "yes" if s.scale_to_zero else "-",
            s.default_sku or "-",
        )
    console.print(table)
    console.print(
        "\n[dim]low-pri = NO means the pattern needs dedicated GPU quota. "
        "Run `python -m ffsft.deploy.endpoint check` to see live quota.[/dim]"
    )


@serving_app.command("show")
def serving_show(key: str = typer.Argument(..., help="Pattern key, e.g. aml_batch_vllm.")) -> None:
    """Show everything the registry knows about one serving pattern."""
    spec = get_serving_registry().get(key)

    table = Table(title=spec.display_name, show_header=False, highlight=True)
    table.add_column("field", style="bold")
    table.add_column("value", overflow="fold")

    for field, value in [
        ("key", spec.key),
        ("surface", spec.surface.value),
        ("engine", spec.engine.value),
        ("openai compatible", str(spec.openai_compatible)),
        ("streaming", str(spec.streaming)),
        ("allows low priority", str(spec.allows_low_priority)),
        ("quota family", spec.quota_family or "-"),
        ("default SKU", spec.default_sku or "-"),
        ("scale to zero", str(spec.scale_to_zero)),
        ("interactive", str(spec.is_interactive)),
        ("load testable", str(spec.load_testable)),
    ]:
        table.add_row(field, value)
    if spec.description:
        table.add_row("description", spec.description.strip())
    if spec.caveats:
        table.add_row("caveats", spec.caveats.strip())
    console.print(table)


@serving_app.command("adapter-modes")
def serving_adapter_modes() -> None:
    """Explain how a LoRA adapter reaches the serving engine."""
    for mode in get_serving_registry().adapter_modes:
        table = Table(title=mode.display_name, show_header=False, highlight=True)
        table.add_column("field", style="bold")
        table.add_column("value", overflow="fold")
        table.add_row("key", mode.key.value)
        table.add_row("how", mode.description.strip())
        table.add_row("tradeoff", mode.tradeoff.strip())
        console.print(table)


@eval_app.command("list")
def bench_list() -> None:
    """List the Korean benchmarks available for evaluation."""
    registry = get_benchmark_registry()
    table = Table(title=f"benchmarks ({len(registry)})", highlight=True)
    for col in ("key", "dataset", "metric", "harness task", "judge", "license"):
        table.add_column(col, overflow="fold")

    for spec in sorted(registry, key=lambda s: s.key):
        table.add_row(
            spec.key,
            spec.dataset_id,
            spec.metric,
            spec.harness_task or "[yellow]custom[/yellow]",
            "yes" if spec.judge_required else "-",
            spec.license,
        )
    console.print(table)
    console.print(
        "\n[dim]Every benchmark is eval-only: most are CC-BY-ND/NC, so training on "
        "them would be both a licence violation and test-set contamination.[/dim]"
    )


@eval_app.command("suites")
def bench_suites() -> None:
    """List the named benchmark suites."""
    registry = get_benchmark_registry()
    table = Table(title="benchmark suites", highlight=True)
    for col in ("suite", "benchmarks", "harness-runnable"):
        table.add_column(col, overflow="fold")

    for key in sorted(registry.suite_keys):
        specs = registry.suite(key)
        runnable = sum(1 for s in specs if s.runnable_by_harness)
        table.add_row(key, ", ".join(s.key for s in specs), f"{runnable}/{len(specs)}")
    console.print(table)


# ---------------------------------------------------------------------------
# Delegating commands. Nothing below reimplements anything -- see the module
# docstring for why they exist and why every import is function-local.
# ---------------------------------------------------------------------------

#: Hand the whole tail of the command line to the delegate untouched. Without
#: `add_help_option=False` Typer would answer `--help` itself and the
#: participant would never see the flags that matter.
_PASSTHROUGH = {"allow_extra_args": True, "ignore_unknown_options": True}


def _run(main: Callable[[], int | None], argv: list[str], prog: str) -> None:
    """Call a console-script `main()` with `argv`, then exit with its code.

    `sys.argv` is swapped rather than passed as a parameter because all but one
    of these `main()`s take no arguments and read `sys.argv` themselves. It is
    restored in a `finally` so a delegate that raises does not leave the
    process holding a rewritten command line.
    """
    import sys

    saved = sys.argv
    sys.argv = [prog, *argv]
    try:
        code = main()
    finally:
        sys.argv = saved
    raise typer.Exit(code or 0)


def _module_main(module: str, extra: str) -> Callable[[], int | None]:
    """Import a delegate's module and return its `main`.

    Names the extra on ImportError. The bare failure is
    `ModuleNotFoundError: No module named 'httpx'`, which does not tell anyone
    on a fresh checkout that the fix is `uv sync --extra serve`.
    """
    import importlib

    try:
        return importlib.import_module(module).main
    except ImportError as exc:
        console.print(
            f"[red]{module} is not importable:[/red] {exc}\n"
            f"This command needs the '{extra}' extra: [bold]uv sync --extra {extra}[/bold]"
        )
        raise typer.Exit(1) from exc


def _script_main(name: str) -> Callable[[], int | None]:
    """Load `scripts/<name>.py` and return its `main`.

    The submit scripts are not importable modules and are not packaged: they
    are client-side, they are what a participant reads before spending money,
    and they live next to the shell tooling for that reason. Loading one by
    path is already how `tests/test_submit_training_guard.py` drives it -- this
    is the same loader, not a second convention.

    Only reachable from a checkout. An installed wheel has no `scripts/`, so
    say that instead of raising `FileNotFoundError` from importlib.
    """
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / f"{name}.py"
    if not path.is_file():
        console.print(
            f"[red]{path} not found.[/red] The submit scripts ship in the repo, "
            f"not in the package -- run this from a checkout."
        )
        raise typer.Exit(1)
    spec = importlib.util.spec_from_file_location(f"ffsft_script_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


train_app = typer.Typer(no_args_is_help=True, help="Submit training to Azure ML (Lab 2, Lab 3).")
merge_app = typer.Typer(no_args_is_help=True, help="Fold a LoRA adapter into base weights (Lab 8).")
app.add_typer(train_app, name="train")
app.add_typer(merge_app, name="merge")


@train_app.command("submit", context_settings=_PASSTHROUGH, add_help_option=False)
def train_submit(ctx: typer.Context) -> None:
    """Submit a QLoRA job. Same as `python scripts/submit_training.py`."""
    _run(_script_main("submit_training"), ctx.args, "ffsft train submit")


@merge_app.command("submit", context_settings=_PASSTHROUGH, add_help_option=False)
def merge_submit(ctx: typer.Context) -> None:
    """Submit the merge as a job. Same as `python scripts/submit_merge.py`."""
    _run(_script_main("submit_merge"), ctx.args, "ffsft merge submit")


@merge_app.command("local", context_settings=_PASSTHROUGH, add_help_option=False)
def merge_local(ctx: typer.Context) -> None:
    """Run the merge here rather than submitting it. Same as `ffsft-merge`.

    This is the code the job runs on the node. Merging a 27B needs ~54 GB of
    bf16 weights materialised, so on a workstation it is a way to merge a small
    model, not the 27B.
    """
    _run(_module_main("ffsft.deploy.merge", "train"), ctx.args, "ffsft merge local")


@app.command("eval", context_settings=_PASSTHROUGH, add_help_option=False)
def eval_cmd(ctx: typer.Context) -> None:
    """Score base against tuned on identical items. Same as `ffsft-eval`."""
    _run(_module_main("ffsft.eval.run", "eval"), ctx.args, "ffsft eval")


@app.command("deploy", context_settings=_PASSTHROUGH, add_help_option=False)
def deploy_cmd(ctx: typer.Context) -> None:
    """check | deploy-online | deploy-batch | shift. Same as `ffsft-deploy`."""
    _run(_module_main("ffsft.deploy.endpoint", "azure"), ctx.args, "ffsft deploy")


@app.command("lifecycle", context_settings=_PASSTHROUGH, add_help_option=False)
def lifecycle_cmd(ctx: typer.Context) -> None:
    """status | up | down. Same as `ffsft-lifecycle`. Run `status` often."""
    _run(_module_main("ffsft.deploy.lifecycle", "azure"), ctx.args, "ffsft lifecycle")


@app.command("loadtest", context_settings=_PASSTHROUGH, add_help_option=False)
def loadtest_cmd(ctx: typer.Context) -> None:
    """TTFT / TPOT / knee against an OpenAI-compatible URL. Same as `ffsft-loadtest`."""
    _run(_module_main("ffsft.serve.loadtest", "serve"), ctx.args, "ffsft loadtest")


@app.command("plot", context_settings=_PASSTHROUGH, add_help_option=False)
def plot_cmd(ctx: typer.Context) -> None:
    """Render a `loadtest --output` report as SVG charts. Same as `ffsft-plot`."""
    _run(_module_main("ffsft.serve.plot", "serve"), ctx.args, "ffsft plot")


@app.command("serve-local", context_settings=_PASSTHROUGH, add_help_option=False)
def serve_local_cmd(ctx: typer.Context) -> None:
    """CPU transformers server, OpenAI-compatible. Same as `ffsft-serve-local`."""
    _run(_module_main("ffsft.serve.local", "train"), ctx.args, "ffsft serve-local")


if __name__ == "__main__":
    app()
