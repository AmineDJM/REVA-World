"""``reva v8 index …`` — V8 universal search index (agent V8API): build, status, query.

Discovered by ``reva.cli.main`` (modules ``reva.cli.v8_<name>`` exposing ``cli``). Reads the database as the
``reva_api`` role (read-only); the only file written is the index (``var/v8/search_index/search.sqlite`` or
``$REVA_V8_SEARCH_DIR``).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import typer

NAME = "index"
cli = typer.Typer(no_args_is_help=True, help="V8 universal search index (build, status, query)")


def _out(obj: Any) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


@cli.command("build")
def index_build(
    out: Path | None = typer.Option(None, help="Index directory (default var/v8/search_index)"),
    origin: str = typer.Option("any", help="live | test_fixture | any"),
    questions: bool = typer.Option(True, help="Include forecast questions (reads sealed manifests)"),
) -> None:
    """(Re)build the search index from the database (read-only) and the registries."""
    from reva.adapters.postgres.engine import make_engine
    from reva.settings import get_settings
    from reva.v8.search.build import build_index

    settings = get_settings()
    store = None
    if questions:
        from reva.adapters.objects.factory import build_store

        store = build_store(settings)
    eng = make_engine("api", settings)
    try:
        with eng.connect() as conn, conn.begin():
            meta = build_index(
                conn,
                out_dir=out,
                origin=None if origin == "any" else origin,
                store=store,
                log=lambda m: typer.echo(m, err=True),
            )
    finally:
        eng.dispose()
    _out(meta)
    if meta.get("errors"):
        raise typer.Exit(2)


@cli.command("status")
def index_status(out: Path | None = typer.Option(None, help="Index directory")) -> None:
    from reva.v8.search.build import index_path
    from reva.v8.search.index import IndexMissing, SearchIndex

    try:
        _out(SearchIndex(index_path(out)).status())
    except IndexMissing as e:
        _out({"built": False, "path": str(e)})
        raise typer.Exit(1) from None


@cli.command("query")
def search(
    q: str,
    kinds: str | None = typer.Option(None, help="Comma-separated search kinds"),
    limit: int = typer.Option(10),
    lang: str = typer.Option("fr"),
    out: Path | None = typer.Option(None, help="Index directory"),
) -> None:
    """Query the index with the API's ranking (prints the hits and the time taken)."""
    from reva.v8.search.build import index_path
    from reva.v8.search.index import SearchIndex

    idx = SearchIndex(index_path(out))
    t = time.perf_counter()
    res = idx.search(q, kinds=kinds.split(",") if kinds else None, limit=limit, lang=lang)
    ms = (time.perf_counter() - t) * 1000
    for h in res["hits"]:
        typer.echo(
            f"{h['score']:.3f} {h['match']:<7} {h['kind']:<9} {h['id']:<28} {h['label'][:60]} | {h['sublabel'][:70]}"
        )
    more = " (more matches exist)" if res["truncated"] else ""
    typer.echo(f"-- {len(res['hits'])} hits{more} in {ms:.1f} ms")
