"""``reva qa run [--domain …]``: read-only data-quality checks (V6 QA-DATA)."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import typer

from reva.qa.config import DOMAINS

qa_app = typer.Typer(no_args_is_help=True, help="V6 data-quality checks (read-only): cross-source, units, time, graph…")


@qa_app.command("run")
def qa_run(
    domain: list[str] | None = typer.Option(
        None, "--domain", help=f"repeatable; one of {', '.join(DOMAINS)} (default all)"
    ),
    origin: str = typer.Option("live", "--origin"),
    json_out: Path | None = typer.Option(None, "--json", help="write the JSON report here"),
    md_out: Path | None = typer.Option(None, "--md", help="write the markdown report here"),
    table: list[str] | None = typer.Option(None, "--table", help="restrict the atlas pass to these tables (regex)"),
    verify_all: bool = typer.Option(False, "--verify-all", help="decode + re-hash every archived object"),
    as_of: str | None = typer.Option(None, "--as-of", help="date treated as today (YYYY-MM-DD)"),
    fail_on: str | None = typer.Option(None, "--fail-on", help="exit 1 if any check has this status (fail|warn)"),
) -> None:
    """Run the checks against the configured database (READ ONLY transactions) and object store."""
    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres.engine import get_engine
    from reva.qa.backend import postgres_backend
    from reva.qa.runner import run_qa

    bad = [d for d in domain or [] if d not in DOMAINS]
    if bad:
        typer.echo(f"unknown domain(s): {bad}; choose from {DOMAINS}", err=True)
        raise typer.Exit(2)
    be = postgres_backend(get_engine("worker"), get_store(), today=date.fromisoformat(as_of) if as_of else None)
    try:
        rep = run_qa(
            be,
            domains=domain or None,
            origin=origin,
            tables=table,
            verify_all=verify_all,
            log=lambda m: typer.echo(m, err=True),
        )
    finally:
        be.close()
    if json_out:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(rep.to_json())
    if md_out:
        md_out.parent.mkdir(parents=True, exist_ok=True)
        md_out.write_text(rep.to_markdown())
    s = rep.summary()
    typer.echo(" ".join(f"{k}={v}" for k, v in s.items()))
    for c in rep.checks:
        typer.echo(f"  {c.status:5} {c.id}: {c.count:,}" + (f" / {c.checked:,}" if c.checked is not None else ""))
    if fail_on and any(c.status == fail_on or (fail_on == "warn" and c.status == "fail") for c in rep.checks):
        sys.exit(1)
