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
    _out({"database": s.reva_db_name if kind != "migration" else s.migration_url().database, "role": kind,
          **d.as_dict(limit=limit)})
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

    _out({"profile": profile, **seed(get_engine("migration"), get_store(),
                                      contact_email=get_settings().reva_http_contact_email)})


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
                    reservation = reserve_ingest(get_engine("worker"), job_id=f"cli-{uuid.uuid4()}", attempt=1,
                                                 source_key=budget_source_key(spec), planned_requests=planned)
                except BudgetExhausted as e:  # typed refusal; no client is created, no request is made
                    reports.append({**e.problem(), "connector": spec.name, "mode": "live", "status": "refused"})
                    continue
                fetch_budget = FetchBudget(max_requests=min(default.max_requests, reservation.max_requests),
                                           max_bytes=min(default.max_bytes, reservation.max_bytes))
                client = make_live_client(spec, settings)
                http: Any = RecordingHttp(client, spec.name, spec.licence) if capture else client
                if not capture:
                    http.origin = "live"
                http = metered(http, get_engine("worker"), reservation)  # each request covered by the ledger
            else:
                http = FixtureHttp(spec.name)
            try:
                rep = await run_connector(c, http, get_store(), get_engine("worker"), budget=fetch_budget,
                                          post_publish=post_publish_for(spec.template), persist_cursor=live)
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
    r = publish_release(get_engine("worker"), get_store(), cutoff=None, origin=Origin(origin),
                        environment=get_settings().reva_env)
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
            text("INSERT INTO ops.api_token(token_sha256, scopes, label, principal_kind) "
                 "VALUES (:s, :sc, :l, :pk) RETURNING id"),
            {"s": sha, "sc": sc, "l": label, "pk": "human" if human else "service"},
        ).scalar_one()
        c.execute(
            text("INSERT INTO ops.audit(actor, action, target, details) VALUES ('admin:cli', 'token.create', "
                 "jsonb_build_object('token_id', CAST(:i AS text)), jsonb_build_object('label', CAST(:l AS text)))"),
            {"i": str(tid), "l": label},
        )
    eng.dispose()
    _out({"token_id": str(tid), "label": label, "scopes": sc, "token": token,
          "note": "store this token securely; only its SHA-256 is kept"})


@auth_app.command("revoke-token")
def revoke_token(token_id: str) -> None:
    """Revoke an API token (admin operation)."""
    from sqlalchemy import text

    from reva.adapters.postgres.engine import make_engine

    eng = make_engine("migration")
    with eng.begin() as c:
        n = c.execute(
            text("UPDATE ops.api_token SET revoked_at = clock_timestamp() WHERE id = CAST(:i AS uuid) "
                 "AND revoked_at IS NULL"),
            {"i": token_id},
        ).rowcount
        c.execute(
            text("INSERT INTO ops.audit(actor, action, target, details) VALUES ('admin:cli', 'token.revoke', "
                 "jsonb_build_object('token_id', CAST(:i AS text)), '{}'::jsonb)"),
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
                if role == "worker" else "n/a",
            }
    st = get_store()
    probe = st.put_bytes(b'{"probe":true}', "quarantine", "application/json")
    checks["object_store"] = {"backend": s.reva_object_backend, "probe_sha256": probe.sha256}
    ok = all(v for v in checks.values())
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

    _out(run_demo(scenario=scenario, countries=countries.split(","), fidelity=fidelity, origin=origin,
                  wait_seconds=wait_seconds))


def main() -> None:  # pragma: no cover
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
_ = Path
