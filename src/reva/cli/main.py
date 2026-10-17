"""`reva` command line. Thin wrappers over the application services."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import typer

app = typer.Typer(no_args_is_help=True, help="REVA World command line")
db_app = typer.Typer(no_args_is_help=True, help="Database bootstrap and migrations")
fixtures_app = typer.Typer(no_args_is_help=True, help="Captured source fixtures")
auth_app = typer.Typer(no_args_is_help=True, help="Authentication helpers")
beliefs_app = typer.Typer(no_args_is_help=True, help="Belief releases")
app.add_typer(db_app, name="db")
app.add_typer(fixtures_app, name="fixtures")
app.add_typer(auth_app, name="auth")
app.add_typer(beliefs_app, name="beliefs")
atlas_app = typer.Typer(no_args_is_help=True, help="V3 atlas: budget-metered source runs, snapshots, listing")
app.add_typer(atlas_app, name="atlas")
links_app = typer.Typer(no_args_is_help=True, help="V3 links: materialised link store (country-country neurons)")
app.add_typer(links_app, name="links")
causal_app = typer.Typer(no_args_is_help=True, help="V3 causality: sourced causal claims and their grades")
app.add_typer(causal_app, name="causal")
kg_app = typer.Typer(no_args_is_help=True, help="V3 knowledge graph: governed imports")
app.add_typer(kg_app, name="kg")
events_app = typer.Typer(no_args_is_help=True, help="V3 events: evidence-based same_as identity links")
app.add_typer(events_app, name="events")
forecasts_app = typer.Typer(no_args_is_help=True, help="V5 prospective forecast registry (PRED-REGISTRY)")
app.add_typer(forecasts_app, name="forecasts")


def _out(obj: Any) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str))


@db_app.command("bootstrap")
def db_bootstrap() -> None:
    """Advisory-locked migrations + runtime role provisioning (migration owner only)."""
    from reva.cli.bootstrap import bootstrap

    _out(bootstrap())


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Apply Alembic migrations only (migration owner, under the migration advisory lock).

    Unlike `db bootstrap`, this does not (re)provision runtime role passwords.
    """
    from alembic import command
    from sqlalchemy import text

    from reva.adapters.postgres.engine import make_engine
    from reva.cli.bootstrap import MIGRATION_LOCK_KEY, alembic_config

    eng = make_engine("migration")
    with eng.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": MIGRATION_LOCK_KEY})
        conn.commit()
        try:
            cfg = alembic_config()
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
            conn.commit()
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": MIGRATION_LOCK_KEY})
            conn.commit()
    eng.dispose()
    _out({"schema_version": version})


@db_app.command("verify-schema")
def db_verify_schema(
    role: str = typer.Option("auto", help="connect as migration|api|worker|sim (auto: migration if configured)"),
    limit: int = typer.Option(20, help="max objects listed per category"),
) -> None:
    """Compare the live catalog with migrations/schema_contract.json (read-only). Exit 1 on drift."""
    from sqlalchemy import text

    from reva.adapters.postgres.engine import make_engine
    from reva.adapters.postgres.schema_contract import verify_schema
    from reva.settings import load_settings

    s = load_settings()
    kind = role if role != "auto" else ("migration" if s.reva_migration_database_url is not None else "worker")
    if kind not in ("migration", "api", "worker", "sim"):
        typer.echo("--role must be auto, migration, api, worker or sim", err=True)
        raise typer.Exit(2)
    eng = make_engine(kind, s)  # type: ignore[arg-type]
    try:
        with eng.connect() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            d = verify_schema(conn)
            conn.rollback()
    finally:
        eng.dispose()
    _out(
        {
            "database": s.reva_db_name if kind != "migration" else s.migration_url().database,
            "role": kind,
            **d.as_dict(limit=limit),
        }
    )
    typer.echo(d.summary(limit=limit), err=True)
    if not d.ok:
        raise typer.Exit(1)


@app.command("seed")
def seed_cmd(profile: str = typer.Option("mvp", help="registry profile")) -> None:
    """Seed concepts, entities, sources, connectors, mappings, models (migration owner).

    The reviewed registry is approved state that runtime roles may not write (R5-01);
    `reva db bootstrap` already seeds it, this re-runs the (idempotent) seed alone."""
    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres.engine import get_engine
    from reva.cli.seed import seed
    from reva.settings import get_settings

    _out(
        {
            "profile": profile,
            **seed(get_engine("migration"), get_store(), contact_email=get_settings().reva_http_contact_email),
        }
    )


def _run_ingest(names: list[str], live: bool, capture: bool) -> list[dict[str, Any]]:
    from reva.adapters.connectors.registry import builtin_connectors
    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres.engine import get_engine
    from reva.evidence.runner import FixtureHttp, RecordingHttp, make_live_client, post_publish_for, run_connector
    from reva.settings import get_settings

    settings = get_settings()
    conns = dict(builtin_connectors(settings.reva_http_contact_email))
    try:  # declarative connectors promoted by the autonomy gate (e.g. Eurostat demo_pjan)
        from reva.autonomy.jobs import promoted_connectors

        conns.update(promoted_connectors(get_engine("worker"), get_store()))
    except Exception as e:
        typer.echo(f"promoted connectors unavailable: {type(e).__name__}", err=True)
    reports = []

    async def go() -> None:
        for name in names:
            c = conns[name]
            spec = c.describe()
            reservation = None
            fetch_budget = None
            if live:
                # Invariant 30: direct CLI live runs are charged like ingest jobs.
                import uuid

                from reva.evidence.runner import load_cursor
                from reva.operations.budgets import BudgetExhausted, budget_source_key, metered, reserve_ingest
                from reva.ports.connectors import FetchBudget

                default = FetchBudget()
                planned = len(c.plan(load_cursor(get_engine("worker"), spec.name), default).items)
                try:
                    reservation = reserve_ingest(
                        get_engine("worker"),
                        job_id=f"cli-{uuid.uuid4()}",
                        attempt=1,
                        source_key=budget_source_key(spec),
                        planned_requests=planned,
                    )
                except BudgetExhausted as e:  # typed refusal; no client is created, no request is made
                    reports.append({**e.problem(), "connector": spec.name, "mode": "live", "status": "refused"})
                    continue
                fetch_budget = FetchBudget(
                    max_requests=min(default.max_requests, reservation.max_requests),
                    max_bytes=min(default.max_bytes, reservation.max_bytes),
                )
                client = make_live_client(spec, settings)
                http: Any = RecordingHttp(client, spec.name, spec.licence) if capture else client
                if not capture:
                    http.origin = "live"
                http = metered(http, get_engine("worker"), reservation)  # each request covered by the ledger
            else:
                http = FixtureHttp(spec.name)
            try:
                hook = getattr(c, "post_publish", None) or post_publish_for(spec.template)  # dataset inputs
                rep = await run_connector(
                    c,
                    http,
                    get_store(),
                    get_engine("worker"),
                    budget=fetch_budget,
                    post_publish=hook,
                    persist_cursor=live,
                )
            finally:
                if capture and live:
                    http.write_manifest()
                await http.aclose()
            budget_info = None
            if reservation is not None:
                from reva.operations.budgets import settle_metered

                budget_info = settle_metered(get_engine("worker"), http)  # actual requests/bytes made
            d = rep.as_dict()
            d["budget"] = budget_info
            d.pop("quality", None)
            d["mode"] = "live" if live else "fixture"
            reports.append(d)

    asyncio.run(go())
    return reports


@app.command("ingest")
def ingest(
    profile: str = typer.Option("mvp"),
    live: bool = typer.Option(False, "--live/--fixtures", help="real provider requests vs captured fixtures"),
    capture: bool = typer.Option(False, help="record live responses as fixtures"),
    connector: list[str] = typer.Option(None, help="restrict to connector names"),
) -> None:
    """Run connectors. Live runs carry origin=live; fixture runs origin=test_fixture."""
    from reva.adapters.connectors.registry import builtin_connectors

    names = connector or list(builtin_connectors())  # promoted connectors: pass --connector explicitly
    _out({"profile": profile, "reports": _run_ingest(names, live, capture)})


@fixtures_app.command("load")
def fixtures_load(profile: str = typer.Option("mvp")) -> None:
    """Replay captured fixtures through the real ingest path (origin=test_fixture).

    Includes declarative connectors already promoted by the autonomy gate.
    """
    from reva.adapters.connectors.registry import builtin_connectors

    names = list(builtin_connectors())
    try:
        from reva.adapters.objects.factory import get_store
        from reva.adapters.postgres.engine import get_engine
        from reva.autonomy.jobs import promoted_connectors

        names += [n for n in promoted_connectors(get_engine("worker"), get_store()) if n not in names]
    except Exception as e:
        typer.echo(f"promoted connectors unavailable: {type(e).__name__}", err=True)
    _out({"profile": profile, "reports": _run_ingest(names, live=False, capture=False)})


@beliefs_app.command("publish")
def beliefs_publish(origin: str = typer.Option("live", help="live or test_fixture")) -> None:
    """Publish a source_select_v1 belief release at the current time."""
    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres.engine import get_engine
    from reva.belief.publication import publish_release
    from reva.kernel.context import Origin
    from reva.settings import get_settings

    # cutoff=None: the DB clock is read inside the publication lock
    r = publish_release(
        get_engine("worker"), get_store(), cutoff=None, origin=Origin(origin), environment=get_settings().reva_env
    )
    _out(r.__dict__)


@auth_app.command("hash-password")
def hash_password(password: str = typer.Option(..., prompt=True, hide_input=True, confirmation_prompt=True)) -> None:
    """Print an Argon2id hash for REVA_ADMIN_PASSWORD_HASH (minimum 12 characters)."""
    from reva.api.auth import hash_password as _hash

    if len(password) < 12:
        typer.echo("password must be at least 12 characters", err=True)
        raise typer.Exit(2)
    typer.echo(_hash(password))


@auth_app.command("create-token")
def create_token(
    label: str = typer.Option(..., help="e.g. human:alice or service:ci"),
    scopes: str = typer.Option("read", help="comma list of read,simulate,ingest,review"),
    human: bool = typer.Option(False, "--human", help="token issued to a named human (required for reviews)"),
) -> None:
    """Create a hashed, scoped API token (admin operation; uses the migration owner). Printed once."""
    from sqlalchemy import text

    from reva.adapters.postgres.engine import make_engine
    from reva.api.auth import new_api_token

    allowed = {"read", "simulate", "ingest", "review"}
    sc = [x.strip() for x in scopes.split(",") if x.strip()]
    if not sc or set(sc) - allowed:
        typer.echo(f"scopes must be a subset of {sorted(allowed)}", err=True)
        raise typer.Exit(2)
    if human and not label.startswith("human:"):
        typer.echo("--human tokens need a label of the form human:<name>", err=True)
        raise typer.Exit(2)
    token, sha = new_api_token()
    eng = make_engine("migration")
    with eng.begin() as c:
        tid = c.execute(
            text(
                "INSERT INTO ops.api_token(token_sha256, scopes, label, principal_kind) "
                "VALUES (:s, :sc, :l, :pk) RETURNING id"
            ),
            {"s": sha, "sc": sc, "l": label, "pk": "human" if human else "service"},
        ).scalar_one()
        c.execute(
            text(
                "INSERT INTO ops.audit(actor, action, target, details) VALUES ('admin:cli', 'token.create', "
                "jsonb_build_object('token_id', CAST(:i AS text)), jsonb_build_object('label', CAST(:l AS text)))"
            ),
            {"i": str(tid), "l": label},
        )
    eng.dispose()
    _out(
        {
            "token_id": str(tid),
            "label": label,
            "scopes": sc,
            "token": token,
            "note": "store this token securely; only its SHA-256 is kept",
        }
    )


@auth_app.command("revoke-token")
def revoke_token(token_id: str) -> None:
    """Revoke an API token (admin operation)."""
    from sqlalchemy import text

    from reva.adapters.postgres.engine import make_engine

    eng = make_engine("migration")
    with eng.begin() as c:
        n = c.execute(
            text(
                "UPDATE ops.api_token SET revoked_at = clock_timestamp() WHERE id = CAST(:i AS uuid) "
                "AND revoked_at IS NULL"
            ),
            {"i": token_id},
        ).rowcount
        c.execute(
            text(
                "INSERT INTO ops.audit(actor, action, target, details) VALUES ('admin:cli', 'token.revoke', "
                "jsonb_build_object('token_id', CAST(:i AS text)), '{}'::jsonb)"
            ),
            {"i": token_id},
        )
    eng.dispose()
    _out({"token_id": token_id, "revoked": bool(n)})


@app.command("api")
def api(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run the API locally (one Uvicorn worker)."""
    import uvicorn

    uvicorn.run("reva.api.app:app", host=host, port=port, workers=1)


@app.command("worker")
def worker() -> None:
    """Run the durable worker locally."""
    from reva.operations.worker import main

    main()


@app.command("gate")
def gate(
    once: bool = typer.Option(False, "--once", help="evaluate the pending gate requests once and exit"),
    poll_seconds: float | None = typer.Option(None, help="poll interval (default REVA_GATE_POLL_SECONDS)"),
) -> None:
    """Run the independent promotion gate process (login reva_gate; records gate decisions)."""
    from reva.gate.service import main

    argv = ["--once"] if once else []
    if poll_seconds is not None:
        argv += ["--poll-seconds", str(poll_seconds)]
    code = main(argv)
    if code:
        raise typer.Exit(code)


@app.command("backtest")
def backtest(
    model: str = typer.Option(..., "--model", help="model key, e.g. demography.cohort_component"),
    release: str | None = typer.Option(None, "--release", help="published release id (default: current)"),
    country: list[str] = typer.Option(None, "--country", help="ISO3 code (repeatable; default: model default)"),
) -> None:
    """Enqueue a back-test job (revised-data retrospective) for a V2 model; a worker runs it."""
    from reva.adapters.postgres.engine import get_engine
    from reva.evaluation.backtests import enqueue_backtest

    jid = enqueue_backtest(
        get_engine("worker"), model, release_id=release, countries=country or None, created_by="cli:backtest"
    )
    _out({"job_id": str(jid), "model_key": model})


@app.command("verify")
def verify(profile: str = typer.Option("local")) -> None:
    """Check configuration, DB schema/roles, registry and object store (no source calls)."""
    from sqlalchemy import text

    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres.engine import get_engine
    from reva.ontology.registry import load_registry
    from reva.settings import load_settings

    checks: dict[str, Any] = {"profile": profile}
    s = load_settings()
    checks["environment"] = s.reva_env
    reg = load_registry()
    checks["registry"] = {"concepts": len(reg.concepts), "mappings": len(reg.mappings)}
    for role in ("api", "worker", "sim"):
        with get_engine(role).connect() as c:  # type: ignore[arg-type]
            checks[f"db_{role}"] = {
                "current_user": c.execute(text("SELECT current_user")).scalar_one(),
                "schema_version": c.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                if role == "worker"
                else "n/a",
            }
    st = get_store()
    probe = st.put_bytes(b'{"probe":true}', "quarantine", "application/json")
    checks["object_store"] = {"backend": s.reva_object_backend, "probe_sha256": probe.sha256}
    # --- GATE2: the independent gate login connects and holds exactly its privileges (skipped, with a
    # note, when REVA_GATE_DB_PASSWORD is not configured outside production)
    from reva.gate.verify import verify_gate

    checks["db_gate"] = verify_gate(s)
    ok = all(v for v in checks.values()) and bool(checks["db_gate"]["ok"])
    # --- end GATE2
    _out({"ok": ok, **checks})
    if not ok:
        raise typer.Exit(1)


@app.command("demo")
def demo(
    scenario: str = typer.Option("crude-border-price-25"),
    countries: str = typer.Option("FRA,CHN,USA"),
    fidelity: str = typer.Option("L1"),
    origin: str = typer.Option("live", help="baseline release origin: live | test_fixture"),
    wait_seconds: float = typer.Option(600.0, help="max wait when a running worker executes the job"),
) -> None:
    """Create and run the crude border-price scenario against the latest published baseline."""
    from reva.cli.demo import run_demo

    _out(
        run_demo(
            scenario=scenario,
            countries=countries.split(","),
            fidelity=fidelity,
            origin=origin,
            wait_seconds=wait_seconds,
        )
    )


# --------------------------------------------------------------------------- atlas (V3, ATLASOPS)
@atlas_app.command("list")
def atlas_list() -> None:
    """Every discovered atlas source (spec, budget key, datasets) and modules that failed to import."""
    from reva.atlas.ingest import list_sources

    _out(list_sources())


@atlas_app.command("ingest")
def atlas_ingest_cmd(
    source: str = typer.Argument(..., help="atlas source name, e.g. atlas.imf (see `reva atlas list`)"),
    dataset: str | None = typer.Option(None, help="only this atlas dataset of the source"),
    fixture: bool = typer.Option(False, "--fixture", help="replay recorded fixtures (test_fixture origin)"),
    option: list[str] = typer.Option([], "--option", help="source run option KEY=VALUE (e.g. countries=FR,DE)"),
) -> None:
    """Run one atlas source through the single metered entry point (worker credentials).

    Live runs reserve the source's planned requests on its budget key before the first request,
    meter every request and settle the actual usage (reva.atlas.ingest.run_atlas_ingest)."""
    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres.engine import make_engine
    from reva.atlas.ingest import parse_options, run_atlas_ingest
    from reva.operations.budgets import BudgetExhausted
    from reva.settings import get_settings

    settings = get_settings()
    try:
        out = asyncio.run(
            run_atlas_ingest(
                settings,
                make_engine("worker", settings),
                get_store(),
                source,
                dataset=dataset,
                mode="fixture" if fixture else "live",
                options=parse_options(option),
            )
        )
    except BudgetExhausted as e:
        _out(e.problem())
        raise typer.Exit(2) from e
    _out(out)
    if out.get("status") != "completed":
        raise typer.Exit(3)


@atlas_app.command("snapshot")
def atlas_snapshot_cmd(
    origin: str = typer.Option("live", help="live or test_fixture"),
    warm: bool = typer.Option(False, "--warm", help="also build the local DuckDB query file now"),
    skip_unchanged: bool = typer.Option(False, "--skip-unchanged", help="reuse the latest snapshot if unchanged"),
) -> None:
    """Record an atlas snapshot (worker credentials): pins the latest version of every atlas table."""
    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres.engine import make_engine
    from reva.atlas.snapshot import take_snapshot
    from reva.settings import get_settings

    if origin not in ("live", "test_fixture"):
        typer.echo("--origin must be live or test_fixture", err=True)
        raise typer.Exit(2)
    settings = get_settings()
    _out(
        take_snapshot(
            settings,
            make_engine("worker", settings),
            get_store(),
            origin=origin,
            warm=warm,
            skip_unchanged=skip_unchanged,
        )
    )


# --------------------------------------------------------------------------- links (V3, LINKWIRE)
@links_app.command("materialise")
def links_materialise_cmd(
    atlas_origin: str | None = typer.Option(None, help="live or test_fixture (default: latest snapshot of any origin)"),
    atlas_snapshot: str | None = typer.Option(None, help="atlas snapshot sha256 (default: latest)"),
    kg_origin: str | None = typer.Option(None, help="live or test_fixture (default: every origin)"),
    top_k: int = typer.Option(10, help="atlas kinds: partners kept per country, period, facet and direction"),
    no_atlas: bool = typer.Option(False, "--no-atlas", help="KG and dataset kinds only"),
    duckdb_memory_limit: str = typer.Option("3GB", help="DuckDB memory limit for the snapshot read"),
) -> None:
    """Materialise the link store (worker credentials): every link of (atlas snapshot, KG watermark), one
    transaction. Alias of `python -m reva.links.store materialise`."""
    from reva.links.store import main as store_main

    argv = ["materialise", "--top-k", str(top_k), "--duckdb-memory-limit", duckdb_memory_limit]
    for flag, value in (
        ("--atlas-origin", atlas_origin),
        ("--atlas-snapshot", atlas_snapshot),
        ("--kg-origin", kg_origin),
    ):
        if value is not None:
            argv += [flag, value]
    if no_atlas:
        argv.append("--no-atlas")
    try:
        code = store_main(argv)
    except SystemExit as e:  # argparse refusal (bad origin)
        raise typer.Exit(2) from e
    if code:
        raise typer.Exit(code)


@links_app.command("prune")
def links_prune_cmd(
    keep: int = typer.Option(2, "--keep", help="newest materialisations kept (the current one included), >= 1"),
    dry_run: bool = typer.Option(False, "--dry-run", help="report what would be pruned; write nothing"),
    vacuum: str = typer.Option(
        "plain", "--vacuum", help="after pruning: none, plain (space reusable) or full (returns disk; locks the table)"
    ),
    reason: str = typer.Option("link store retention policy", "--reason", help="recorded with each prune"),
) -> None:
    """Prune old link store materialisations (owner credentials, migration 0017): deletes the derived link rows
    of materialisations nothing serves, pins or references; keeps every materialisation row (its inputs), records
    `kg.link_prune` + an `ops.audit` row (`links.prune`) per pruned materialisation."""
    from reva.adapters.postgres.engine import make_engine
    from reva.links.retention import RetentionError, prune
    from reva.settings import get_settings

    try:
        out = prune(
            make_engine("migration", get_settings()),
            keep=keep,
            dry_run=dry_run,
            reason=reason,
            vacuum_mode=vacuum,
            log_fn=lambda s: typer.echo(s, err=True),
        )
    except RetentionError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from e
    _out(out)


# --------------------------------------------------------------------------- causal (V3, CAUSOPS)
@causal_app.command("refresh")
def causal_refresh_cmd(
    origin: str = typer.Option("live", help="live, test_fixture or any (every origin)"),
) -> None:
    """Load causal claims from the KG current view, then grade every current claim (worker credentials).

    Same code path as the `causal_refresh` job kind; idempotent (nothing is written when nothing changed)."""
    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres.engine import make_engine
    from reva.causal import refresh
    from reva.operations.jobs import parse_causal_refresh_payload
    from reva.settings import get_settings

    if origin not in ("live", "test_fixture", "any"):
        typer.echo("--origin must be live, test_fixture or any", err=True)
        raise typer.Exit(2)
    p = parse_causal_refresh_payload({"origin": None if origin == "any" else origin})
    settings = get_settings()
    out = refresh(make_engine("worker", settings), get_store(), origin=p["origin"])
    _out({"status": "completed", "origin": p["origin"], **out})


@causal_app.command("annotate-retractions")
def causal_annotate_retractions_cmd(
    origin: str = typer.Option("any", help="live, test_fixture or any"),
    dry_run: bool = typer.Option(False, "--dry-run", help="derive and count, write nothing"),
) -> None:
    """Append corrected reasons (kg.retraction_annotation, migration 0018) for the claim / statement retractions
    stored as removed_at_source although the retracting load had the same raw bytes (audit2 N1). The retraction
    rows are never edited; idempotent (worker credentials)."""
    from reva.adapters.postgres.engine import make_engine
    from reva.causal.retractions import annotate_retraction_reasons
    from reva.settings import get_settings

    if origin not in ("live", "test_fixture", "any"):
        typer.echo("--origin must be live, test_fixture or any", err=True)
        raise typer.Exit(2)
    eng = make_engine("worker", get_settings())
    _out(annotate_retraction_reasons(eng, origin=None if origin == "any" else origin, apply=not dry_run))


@causal_app.command("queue-labels")
def causal_queue_labels_cmd(
    origin: str = typer.Option("live", help="live or test_fixture"),
    dry_run: bool = typer.Option(False, "--dry-run", help="list, do not touch the harvester state"),
) -> None:
    """Put the Wikidata items named by causal claims that the KG cannot label yet (P3680 parties, P5102 / P1480
    values, scope qualifiers, endpoints) at tier 1 of the causal harvester's queue (audit2 N3). No network."""
    from sqlalchemy import text as text_sql

    from reva.adapters.postgres.engine import make_engine
    from reva.causal.labels import queue_for_harvest, unlabelled_items
    from reva.settings import get_settings

    eng = make_engine("api", get_settings())
    with eng.connect() as conn:
        conn.execute(text_sql("SET statement_timeout = 0"))
        items = unlabelled_items(conn, origin=origin)
    groups: dict[str, int] = {}
    for v in items.values():
        groups[v["group"]] = groups.get(v["group"], 0) + 1
    out: dict[str, Any] = {"origin": origin, "unlabelled": len(items), "by_group": groups}
    out["qualifier_items"] = {k: v for k, v in items.items() if v["group"] == "qualifier"}
    if not dry_run:
        out["queue"] = queue_for_harvest(items)
    _out(out)


# --------------------------------------------------------------------------- kg (V3, LOADALL)
@kg_app.command("import-harvest")
def kg_import_harvest_cmd(
    from_db: str = typer.Option(..., "--from-db", help="throwaway harvest database, e.g. reva_kg_wdharvest"),
    sources: list[str] = typer.Option(["wikidata*"], "--sources", help="kg.load source_key glob(s)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="derive and count, write nothing"),
    no_verify: bool = typer.Option(False, "--no-verify", help="skip the source/target content comparison"),
) -> None:
    """Re-load a Wikidata harvest into the configured KG database from the ARCHIVED raw responses
    (re-derived with the harvester's extraction, written through load_batch; never row copies)."""
    from reva.kg.import_harvest import run

    _out(run(from_db, sources=tuple(sources), dry_run=dry_run, verify=not no_verify))


# --------------------------------------------------------------------------- events (V3, SAMEAS)
@events_app.command("same-as")
def events_same_as_cmd(
    origin: str = typer.Option("live", help="live or test_fixture"),
    dry_run: bool = typer.Option(False, "--dry-run", help="derive and report, write nothing"),
) -> None:
    """Derive `events:same_as` links from the evidence rules (registry/events/same_as_rules.yaml: shared
    identifiers, provider crosswalks, evidenced mapping tables -- never name or date matching) and load them as KG
    statements (worker credentials; links whose evidence disappeared are retracted). Then run `reva causal refresh`."""
    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres.engine import make_engine
    from reva.events import same_as as sa
    from reva.settings import get_settings

    if origin not in ("live", "test_fixture"):
        typer.echo("--origin must be live or test_fixture", err=True)
        raise typer.Exit(2)
    engine = make_engine("worker", get_settings())
    with engine.connect() as conn:
        report = sa.derive_links(conn, origin=origin)
    out: dict[str, Any] = {"origin": origin, "derived": report.as_dict()}
    if not dry_run:
        out["loaded"] = sa.load_links(
            engine, get_store(), report, origin=origin, rules_sha256=sa.rules_sha256()
        ).as_dict()
    _out({"status": "dry_run" if dry_run else "completed", **out})


# --------------------------------------------------------------------------- forecasts (V5, PRED-REGISTRY)
def _forecast_job(payload: dict[str, Any]) -> dict[str, Any]:
    from types import SimpleNamespace

    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres.engine import make_engine
    from reva.evaluation.registry.service import handle_forecast_registry
    from reva.settings import get_settings

    ctx = SimpleNamespace(worker_engine=make_engine("worker", get_settings()), store=get_store())
    return handle_forecast_registry(payload, ctx)


@forecasts_app.command("issue")
def forecasts_issue_cmd(
    origin: str = typer.Option("live", help="live or test_fixture"),
    family: list[str] = typer.Option([], "--family", help="limit to these question families (repeatable)"),
) -> None:
    """Generate this quarterly cycle's questions not issued yet, forecast them with the baselines and SEAL the batch
    (worker credentials; same code path as the scheduled `forecast_registry` issue job)."""
    payload: dict[str, Any] = {"action": "issue", "origin": origin}
    if family:
        payload["families"] = list(family)
    _out(_forecast_job(payload))


@forecasts_app.command("resolve")
def forecasts_resolve_cmd(origin: str = typer.Option("live", help="live or test_fixture")) -> None:
    """Resolve due questions from table versions registered after each seal and score the frozen forecasts."""
    _out(_forecast_job({"action": "resolve", "origin": origin}))


@forecasts_app.command("backtest")
def forecasts_backtest_cmd(
    origin: str = typer.Option("live", help="live or test_fixture"),
    years: int = typer.Option(8, help="years of rolling origins"),
    family: list[str] = typer.Option([], "--family", help="limit to these question families (repeatable)"),
) -> None:
    """Final-vintage pseudo-real-time back-test of the baselines (labelled; never real-time skill)."""
    payload: dict[str, Any] = {"action": "backtest", "origin": origin, "years": years}
    if family:
        payload["families"] = list(family)
    _out(_forecast_job(payload))


@forecasts_app.command("status")
def forecasts_status_cmd(origin: str = typer.Option("live", help="live, test_fixture or any")) -> None:
    """Sealed batches, question counts by status and the leaderboard (read-only)."""
    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres.engine import make_engine
    from reva.evaluation.registry import read as R
    from reva.settings import get_settings

    o = None if origin == "any" else origin
    eng = make_engine("api", get_settings())
    with eng.connect() as conn:
        bs = R.batches(conn, o)
        lb = R.leaderboard(conn, get_store(), o)
        fams = R.families(conn, o)
    _out(
        {
            "batches": [{k: str(v) if k == "recorded_at" else v for k, v in b.items()} for b in bs],
            "questions": lb["questions"],
            "leaderboard": [{k: r[k] for k in ("model", "n_issued", "n_scored", "mae", "crps", "brier")} for r in lb["models"]],
            "families": {f["key"]: f["status"] for f in fams},
        }
    )


def main() -> None:  # pragma: no cover
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
_ = Path
