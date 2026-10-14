"""`reva` command line. Thin wrappers over the application services."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
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
    """Apply Alembic migrations (idempotent; same lock as bootstrap)."""
    from reva.cli.bootstrap import bootstrap

    _out(bootstrap())


@app.command("seed")
def seed_cmd(profile: str = typer.Option("mvp", help="registry profile")) -> None:
    """Seed concepts, entities, sources, connectors, mappings, models (worker role)."""
    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres.engine import get_engine
    from reva.cli.seed import seed
    from reva.settings import get_settings

    _out({"profile": profile, **seed(get_engine("worker"), get_store(),
                                      contact_email=get_settings().reva_http_contact_email)})


def _run_ingest(names: list[str], live: bool, capture: bool) -> list[dict[str, Any]]:
    from reva.adapters.connectors.registry import builtin_connectors
    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres.engine import get_engine
    from reva.evidence.runner import FixtureHttp, RecordingHttp, make_live_client, post_publish_for, run_connector
    from reva.settings import get_settings

    settings = get_settings()
    conns = builtin_connectors(settings.reva_http_contact_email)
    reports = []

    async def go() -> None:
        for name in names:
            c = conns[name]
            spec = c.describe()
            if live:
                client = make_live_client(spec, settings)
                http: Any = RecordingHttp(client, spec.name, spec.licence) if capture else client
                if not capture:
                    http.origin = "live"
            else:
                http = FixtureHttp(spec.name)
            try:
                rep = await run_connector(c, http, get_store(), get_engine("worker"),
                                          post_publish=post_publish_for(spec.template), persist_cursor=live)
            finally:
                if capture and live:
                    http.write_manifest()
                await http.aclose()
            d = rep.as_dict()
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

    names = connector or list(builtin_connectors())
    _out({"profile": profile, "reports": _run_ingest(names, live, capture)})


@fixtures_app.command("load")
def fixtures_load(profile: str = typer.Option("mvp")) -> None:
    """Replay captured fixtures through the real ingest path (origin=test_fixture)."""
    from reva.adapters.connectors.registry import builtin_connectors

    _out({"profile": profile, "reports": _run_ingest(list(builtin_connectors()), live=False, capture=False)})


@beliefs_app.command("publish")
def beliefs_publish(origin: str = typer.Option("live", help="live or test_fixture")) -> None:
    """Publish a source_select_v1 belief release at the current time."""
    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres.engine import get_engine
    from reva.belief.publication import publish_release
    from reva.kernel.context import Origin
    from reva.settings import get_settings

    r = publish_release(get_engine("worker"), get_store(), cutoff=datetime.now(UTC), origin=Origin(origin),
                        environment=get_settings().reva_env)
    _out(r.__dict__)


@auth_app.command("hash-password")
def hash_password(password: str = typer.Option(..., prompt=True, hide_input=True, confirmation_prompt=True)) -> None:
    """Print an Argon2id hash for REVA_ADMIN_PASSWORD_HASH."""
    from argon2 import PasswordHasher

    typer.echo(PasswordHasher().hash(password))


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
) -> None:
    """Create and run the crude border-price scenario against the latest published baseline."""
    from reva.cli.demo import run_demo

    _out(run_demo(scenario=scenario, countries=countries.split(","), fidelity=fidelity))


def main() -> None:  # pragma: no cover
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
_ = Path
