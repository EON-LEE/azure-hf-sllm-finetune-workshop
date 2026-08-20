"""`ffsft` command line entry point.

Right now this exposes the registry layers that are implemented: models,
datasets and benchmarks. Train / eval / deploy subcommands are added as those
backends land (see docs/PLAN.md section 7).
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from .models import Provider, TuningMethod, get_registry

app = typer.Typer(
    add_completion=False,
    help="Fabric + Foundry sLLM fine-tuning asset.",
    no_args_is_help=True,
)
models_app = typer.Typer(no_args_is_help=True, help="Inspect the swappable model registry.")
app.add_typer(models_app, name="models")

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


if __name__ == "__main__":
    app()
