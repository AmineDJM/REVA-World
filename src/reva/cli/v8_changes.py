"""``reva v8 changes …`` — V8 change feed in the terminal (agent V8API). Read-only (``reva_api`` role)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import typer

NAME = "changes"
cli = typer.Typer(no_args_is_help=True, help="V8 change feed (what changed in REVA)")


@cli.command("tail")
def changes_tail(
    since: str | None = typer.Option(None, help="ISO time (default: 24 h ago)"),
    kinds: str | None = typer.Option(None, help="Comma-separated change kinds"),
    limit: int = typer.Option(50),
    follow: bool = typer.Option(False, help="Keep polling every --interval seconds"),
    interval: int = typer.Option(60),
) -> None:
    """Print the change feed (newest last), optionally following it."""
    from reva.adapters.postgres.engine import make_engine
    from reva.settings import get_settings
    from reva.v8.changes.feed import ChangeQuery, read_changes

    settings = get_settings()
    start = datetime.fromisoformat(since) if since else datetime.now(UTC) - timedelta(hours=24)
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    eng = make_engine("api", settings)
    seen: set[str] = set()
    try:
        while True:
            with eng.connect() as conn, conn.begin():
                res = read_changes(
                    conn,
                    ChangeQuery(since=start, until=None, kinds=kinds.split(",") if kinds else None, limit=limit),
                    store=None,
                )
            for it in reversed(res["items"]):
                if it["id"] in seen:
                    continue
                seen.add(it["id"])
                typer.echo(f"{it['at']}  {it['kind']:<17} {it['title']}")
            if not follow:
                break
            time.sleep(max(5, interval))
    finally:
        eng.dispose()
