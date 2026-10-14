"""Private, throw-away databases for tests that must create state.

The shared session ``db`` fixture (tests/conftest.py) is used by many test
modules at once; tests that ingest fixtures, publish releases or append
eligibility decisions would change other modules' expectations (counts,
revisions, as-of answers). Those tests use a private database built the same
way: fresh database, migrations + runtime role passwords (bootstrap), registry
seed through reva_worker, filesystem object store under a temp dir.

Load this file by path (``load_isolated_db_helper()`` pattern in the test
modules); it is not a package.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text

from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.adapters.postgres.engine import make_engine
from reva.settings import Settings

REPO = Path(__file__).resolve().parents[3]


@dataclass
class IsolatedDB:
    settings: Settings
    name: str
    owner: Engine
    api: Engine
    worker: Engine
    sim: Engine
    store: FilesystemArtifactStore
    object_root: Path


def _settings(**over: object) -> Settings:
    s = Settings(_env_file=REPO / ".env")  # type: ignore[call-arg]
    data = s.model_dump()
    data.update(over)
    return Settings.model_validate(data)


@contextmanager
def isolated_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[IsolatedDB]:
    base = _settings()
    if base.reva_migration_database_url is None:
        pytest.skip("REVA_MIGRATION_DATABASE_URL not configured")
    admin = create_engine(base.migration_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception:
        admin.dispose()
        pytest.skip("PostgreSQL not reachable")
    name = f"reva_h2_{uuid.uuid4().hex[:10]}"
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    root = tmp_path_factory.mktemp("h2objects")
    url = base.migration_url().set(database=name).render_as_string(hide_password=False)
    settings = _settings(reva_migration_database_url=url, reva_db_name=name, reva_env="test", reva_object_root=root)
    from reva.cli.bootstrap import bootstrap
    from reva.cli.seed import seed

    engines: list[Engine] = []
    try:
        for attempt in range(3):  # concurrent sessions may race on cluster-global role DDL
            try:
                bootstrap(settings)
                break
            except Exception:
                if attempt == 2:
                    raise
        store = FilesystemArtifactStore(root, settings.reva_s3_prefix)
        d = IsolatedDB(
            settings=settings,
            name=name,
            owner=make_engine("migration", settings),
            api=make_engine("api", settings),
            worker=make_engine("worker", settings),
            sim=make_engine("sim", settings),
            store=store,
            object_root=root,
        )
        engines = [d.owner, d.api, d.worker, d.sim]
        seed(d.worker, store)
        yield d
    finally:
        for e in engines:
            e.dispose()
        if not os.environ.get("REVA_KEEP_TEST_DB"):
            with admin.connect() as c:
                c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
