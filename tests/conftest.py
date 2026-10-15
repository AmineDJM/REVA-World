"""Shared fixtures.

DB tests run against a fresh, uniquely named database per test session (so
parallel developers/agents never collide), created with the local migration
owner, bootstrapped (migrations + runtime role passwords) and seeded by the
migration owner (bootstrap). Requires REVA_MIGRATION_DATABASE_URL (see .env.example); DB tests
are skipped when the server is unreachable.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text

from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.adapters.postgres.engine import make_engine
from reva.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _no_public_sandbox_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never send packets to the internet: point the sandbox's public-network
    probe at a closed local port (it is only a reachability check)."""
    try:
        import reva.autonomy.sandbox as sandbox
    except ImportError:  # pragma: no cover
        return
    monkeypatch.setattr(sandbox, "PUBLIC_PROBE_ADDR", ("127.0.0.1", 1))


@dataclass
class DB:
    settings: Settings
    name: str
    owner: Engine
    api: Engine
    worker: Engine
    sim: Engine
    store: FilesystemArtifactStore
    #: The independent promotion gate's login (reva_gate): the only one that may record gate results.
    gate: Engine | None = None


def _base_settings(**over: object) -> Settings:
    env = {k: v for k, v in os.environ.items()}
    s = Settings(_env_file=Path(__file__).parents[1] / ".env")  # type: ignore[call-arg]
    data = s.model_dump()
    data.update(over)
    _ = env
    return Settings.model_validate(data)


@pytest.fixture(scope="session")
def db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[DB]:
    base = _base_settings()
    if base.reva_migration_database_url is None:
        pytest.skip("REVA_MIGRATION_DATABASE_URL not configured")
    admin_url = base.migration_url().set(database="postgres")
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("PostgreSQL not reachable")
    name = f"reva_test_{uuid.uuid4().hex[:10]}"
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = base.migration_url().set(database=name).render_as_string(hide_password=False)
    root = tmp_path_factory.mktemp("objects")
    settings = _base_settings(
        reva_migration_database_url=url, reva_db_name=name, reva_env="test", reva_object_root=root
    )
    from reva.cli.bootstrap import bootstrap
    from reva.cli.seed import seed

    for attempt in range(3):  # concurrent sessions may race on cluster-global role DDL
        try:
            bootstrap(settings)
            break
        except Exception:
            if attempt == 2:
                raise
    store = FilesystemArtifactStore(root, settings.reva_s3_prefix)
    d = DB(
        settings=settings,
        name=name,
        owner=make_engine("migration", settings),
        api=make_engine("api", settings),
        worker=make_engine("worker", settings),
        sim=make_engine("sim", settings),
        store=store,
        gate=make_engine("gate", settings) if settings.reva_gate_db_password is not None else None,
    )
    # bootstrap() seeds the reviewed registry as the migration owner (R5-01: runtime roles may not
    # write approved registry state); re-running it as the owner is an idempotent no-op.
    seed(d.owner, store)
    yield d
    for e in (d.owner, d.api, d.worker, d.sim, d.gate):
        if e is not None:
            e.dispose()
    if not os.environ.get("REVA_KEEP_TEST_DB"):
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture()
def gate_service(db: DB) -> Iterator[object]:
    """The independent gate process (reva_gate login) running in a background thread, so worker
    autonomy steps that file gate requests receive a decision (``reva.gate.service.GateService``)."""
    if db.gate is None:
        pytest.skip("REVA_GATE_DB_PASSWORD not configured (see .env.example)")
    from reva.gate.service import GateService

    engine = make_engine("gate", db.settings)  # own pool: tests may use db.gate concurrently
    svc = GateService(engine, db.store, db.settings, poll_seconds=0.05)
    with svc.background():
        yield svc
    engine.dispose()


@pytest.fixture()
def store(tmp_path: Path) -> FilesystemArtifactStore:
    return FilesystemArtifactStore(tmp_path / "objects")
