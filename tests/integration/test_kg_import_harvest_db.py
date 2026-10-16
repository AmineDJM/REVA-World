"""LOADALL: a Wikidata harvest DB re-loaded into another KG DB from the archived raw responses (two throwaway DBs).

The source is filled by the real harvester (scripted HTTP, real archive); the import re-derives every entity and
statement from the archive and writes through ``load_batch``. The test proves provenance is preserved (statement
ids, revisions, references, raw sha256, retrieval time), raw bytes land in the target's own store, the import is
idempotent (no new load row on a re-run) and a tampered raw object refuses its load.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from reva.adapters.connectors import wikidata as wd
from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.adapters.postgres.engine import make_engine
from reva.kg import harvest as hv
from reva.kg import import_harvest as ih
from reva.settings import Settings

pytestmark = pytest.mark.db


def _helpers() -> Any:
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "unit" / "test_wikidata_harvest.py"
    spec = importlib.util.spec_from_file_location("_wdharvest_helpers_ih", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_h = _helpers()


class _Db:
    def __init__(self, name: str, settings: Settings, root: Path) -> None:
        self.name = name
        self.settings = settings
        self.worker = make_engine("worker", settings)
        self.owner = make_engine("migration", settings)
        self.store = FilesystemArtifactStore(root, settings.reva_s3_prefix)

    def dispose(self) -> None:
        self.worker.dispose()
        self.owner.dispose()


@pytest.fixture(scope="module")
def two_dbs(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[_Db, _Db]]:
    base = Settings(_env_file=Path(__file__).parents[2] / ".env")  # type: ignore[call-arg]
    if base.reva_migration_database_url is None:
        pytest.skip("REVA_MIGRATION_DATABASE_URL not configured")
    admin = create_engine(base.migration_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception:
        admin.dispose()
        pytest.skip("PostgreSQL not reachable")
    from reva.cli.bootstrap import bootstrap

    made: list[_Db] = []
    try:
        for _ in range(2):
            name = f"reva_test_{uuid.uuid4().hex[:10]}"
            with admin.connect() as c:
                c.execute(text(f'CREATE DATABASE "{name}"'))
            url = base.migration_url().set(database=name).render_as_string(hide_password=False)
            root = tmp_path_factory.mktemp("objects")
            data = base.model_dump()
            data.update(reva_migration_database_url=url, reva_db_name=name, reva_env="test", reva_object_root=root)
            settings = Settings.model_validate(data)
            bootstrap(settings)
            made.append(_Db(name, settings, root))
        yield made[0], made[1]
    finally:
        for d in made:
            d.dispose()
            if not os.environ.get("REVA_KEEP_TEST_DB"):
                with admin.connect() as c:
                    c.execute(text(f'DROP DATABASE IF EXISTS "{d.name}" WITH (FORCE)'))
        admin.dispose()


ROWS_SQL = """SELECT statement_key, statement_id, content_sha256, source_revision, raw_sha256, refs, rank,
                     retrieved_at, origin FROM kg.statement_current ORDER BY statement_key"""


async def test_import_rederives_from_archive_with_provenance_and_is_idempotent(
    two_dbs: tuple[_Db, _Db], tmp_path: Path
) -> None:
    src, dst = two_dbs
    archive = FilesystemArtifactStore(tmp_path / "archive")  # the harvest archive (var/kg/objects in production)
    index = tmp_path / "archive_index.jsonl"
    seen: list[list[str]] = []
    client = _h.client_for(_h.ScriptedHttp(router=_h._router(3, seen)), tmp_path)
    client.archive = wd.Archive(archive, index)
    sink = hv.KgDbSink(src.worker, archive, origin="test_fixture")
    allow = wd.load_allowlist()
    sink.register_properties(allow)
    spec = _h._spec(tmp_path, {**_h.BASE, "entity_kind": "empire", "expand_hops": 1})
    rep = await hv.Harvester(client, sink, allow=allow, state_dir=tmp_path / "state").run_spec(spec)
    assert rep.status == "done", rep.failures

    out = ih.import_harvest(
        src.worker,
        dst.worker,
        archive=archive,
        to_store=dst.store,
        from_db=src.name,
        index_path=index,
        specs={spec.id: spec},
        allow=allow,
    )
    d = out.as_dict()
    with src.owner.connect() as c:
        n_loads = c.execute(text("SELECT count(*) FROM kg.load")).scalar_one()
        src_rows = [tuple(r) for r in c.execute(text(ROWS_SQL)).all()]
        src_ents = c.execute(text("SELECT qid, kind, source_revision FROM kg.entity_current ORDER BY qid")).all()
    assert d["loads_imported"] == n_loads >= 2 and not d["skipped"], d
    v = d["verification"]
    assert v["identical"] == v["source_current_statements"] == len(src_rows) > 0 and v["differing"] == 0, v
    with dst.owner.connect() as c:
        dst_rows = [tuple(r) for r in c.execute(text(ROWS_SQL)).all()]
        dst_ents = c.execute(text("SELECT qid, kind, source_revision FROM kg.entity_current ORDER BY qid")).all()
        params = c.execute(text("SELECT parameters FROM kg.load ORDER BY id")).scalars().all()
        raws = c.execute(text("SELECT DISTINCT raw_sha256 FROM kg.statement")).scalars().all()
        verified = [c.execute(text("SELECT meta.artifact_is_verified(:s)"), {"s": s}).scalar_one() for s in raws]
    # same statement ids, content, revisions, raw sha256, references, rank, retrieval time, origin
    assert dst_rows == src_rows
    assert [tuple(r) for r in dst_ents] == [tuple(r) for r in src_ents]
    assert {r[0] for r in dst_ents} >= {"Q17167", "Q220"}
    assert all(p["import"]["importer"] == ih.IMPORT_VERSION and p["import"]["from_db"] == src.name for p in params)
    assert all(verified) and all(dst.store.head("raw", s) is not None for s in raws)

    again = ih.import_harvest(
        src.worker,
        dst.worker,
        archive=archive,
        to_store=dst.store,
        from_db=src.name,
        index_path=index,
        specs={spec.id: spec},
        allow=allow,
    ).as_dict()
    assert again["loads_imported"] == 0 and again["skipped"] == {"already_imported": n_loads}
    with dst.owner.connect() as c:
        assert c.execute(text("SELECT count(*) FROM kg.load")).scalar_one() == len(params)


def test_tampered_archive_refuses_the_load(two_dbs: tuple[_Db, _Db], tmp_path: Path) -> None:
    src, dst = two_dbs
    with dst.owner.connect() as c:
        before = c.execute(text("SELECT count(*) FROM kg.load")).scalar_one()

    class Tampered:
        def get(self, ns: str, sha: str) -> bytes:
            return b'{"entities": {}}'

    out = ih.import_harvest(
        src.worker,
        dst.worker,
        archive=Tampered(),
        to_store=dst.store,
        from_db=src.name,
        index_path=None,
        specs={},
        allow=wd.load_allowlist(),
        verify=False,
    ).as_dict()
    # every source load is either already imported (previous test) or refused: nothing new is written
    assert out["loads_imported"] == 0
    with dst.owner.connect() as c:
        assert c.execute(text("SELECT count(*) FROM kg.load")).scalar_one() == before


def test_cli_refuses_the_dev_database_as_source() -> None:
    with pytest.raises(hv.SpecError):
        ih.run("reva", dry_run=True)
