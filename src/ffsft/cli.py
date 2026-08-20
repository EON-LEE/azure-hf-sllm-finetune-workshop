"""`ffsft` command line entry point.

Exposes the registry layers: models, benchmarks and serving patterns. Train /
eval / deploy are separate module entry points (`python -m ffsft.train.qlora`,
`ffsft-eval`, `ffsft-deploy`, `ffsft-loadtest`) because they need heavyweight
optional dependencies that this CLI must import lazily or not at all.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from .deploy import get_serving_registry
from .eval import get_benchmark_registry
from .models import Provider, TuningMethod, get_registry

app = typer.Typer(
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


if __name__ == "__main__":
    app()
