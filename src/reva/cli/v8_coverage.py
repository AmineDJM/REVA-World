"""``reva v8 coverage refresh``: recompute the V8 coverage aggregates (read-only on the DB and the object store)."""

from __future__ import annotations

from pathlib import Path

import typer

cli = typer.Typer(no_args_is_help=True, help="V8 coverage: what is loaded / missing per domain x country x period")
NAME = "coverage"


@cli.command("refresh")
def refresh(
    origin: str = typer.Option("live", "--origin", help="live | test_fixture"),
    out_dir: Path | None = typer.Option(None, "--out-dir", help="default var/v8/coverage/"),
    no_kg: bool = typer.Option(False, "--no-kg", help="skip the knowledge-graph pass (~1-2 min on the dev DB)"),
) -> None:
    """Read the atlas tables, declared sources, acquisitions and KG counts; write var/v8/coverage/coverage.json."""
    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres.engine import get_engine
    from reva.v8.coverage.compute import DEFAULT_DIR, compute, write

    if origin not in ("live", "test_fixture"):
        typer.echo("origin must be live or test_fixture", err=True)
        raise typer.Exit(2)
    doc = compute(get_engine("api"), get_store(), origin=origin, kg=not no_kg, log=lambda m: typer.echo(m, err=True))
    path = write(doc, out_dir or DEFAULT_DIR)
    typer.echo(
        f"wrote {path} ({path.stat().st_size / 1e6:.2f} MB): {len(doc['tables'])} atlas tables, "
        f"{sum(t['rows'] for t in doc['tables']):,} rows, {len(doc['connectors'])} declared sources, "
        f"{doc['elapsed_s']} s"
    )


@cli.command("check-pending")
def check_pending() -> None:
    """Validate registry/coverage/pending.yaml (exit 1 on problems)."""
    from reva.v8.coverage.pending import load_pending

    try:
        reg = load_pending()
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    typer.echo(f"{len(reg.items)} items valid")
