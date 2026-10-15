"""Knowledge-graph storage (migration 0009_kg) and the bulk loader ``reva.kg.model.load_batch``.

Synthetic, provider-scoped ids (``syn:...``) and source keys unique to this module keep it independent of
other suites sharing the session database. Nothing here is a real-world fact.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from reva.kernel.errors import IntegrityFailure, ValidationProblem
from reva.kg.model import (
    MAX_STATEMENTS_PER_BATCH,
    KgEntity,
    KgProperty,
    KgStatement,
    KgTime,
    load_batch,
    register_properties,
    statement_key,
)

pytestmark = pytest.mark.db

PROPS = [
    KgProperty("syn:linked", "item", "linked_to", "linked to", "lié à", "linked_from", "linked from", "lié depuis"),
    KgProperty("syn:count", "quantity", "count", "count", "nombre"),
]


@pytest.fixture(scope="module")
def kg(db):  # type: ignore[no-untyped-def]
    register_properties(db.worker, PROPS)
    return db


def _raw(db, tag: str) -> str:  # type: ignore[no-untyped-def]
    return db.store.put_bytes(f"kg-store-test {tag} {uuid.uuid4()}".encode(), "raw", "application/json").sha256


def _source() -> str:
    return f"kgtest{uuid.uuid4().hex[:8]}"


def _current(engine: Engine, source: str) -> list[dict]:
    with engine.connect() as c:
        return [
            dict(r)
            for r in c.execute(
                text("""SELECT statement_key, object_qid, value, source_revision, rank
                          FROM kg.statement_current WHERE source_key = :s ORDER BY statement_key"""),
                {"s": source},
            ).mappings()
        ]


def test_load_is_idempotent_and_records_provenance(kg) -> None:  # type: ignore[no-untyped-def]
    src, raw = _source(), _raw(kg, "idem")
    now = datetime.now(UTC)
    ents = [KgEntity("kgs:a", "test_thing", {"en": "Alpha", "fr": "Alpha"}, {"en": "first"}, {"en": ["A"]}, 3, "r1")]
    sts = [
        KgStatement("kgs:a", "syn:linked", "kgs:b", statement_id="S1", source_revision="r1", start=KgTime("1900", 9)),
        KgStatement("kgs:a", "syn:count", value={"type": "quantity", "amount": "+12", "unit": "1"}),
    ]
    kw = dict(source_key=src, raw_sha256s=[raw], origin="test_fixture")
    r1 = load_batch(kg.worker, kg.store, entities=ents, statements=sts, retrieved_at=now, **kw)
    assert (r1.entities_inserted, r1.labels_inserted, r1.statements_inserted, r1.rejected_total) == (1, 3, 2, 0)
    r2 = load_batch(kg.worker, kg.store, entities=ents, statements=sts, retrieved_at=now + timedelta(seconds=1), **kw)
    assert (r2.entities_inserted, r2.entities_unchanged, r2.statements_inserted, r2.statements_unchanged) == (
        0,
        1,
        0,
        2,
    )
    with kg.api.connect() as c:
        n = c.execute(text("SELECT count(*) FROM kg.statement WHERE source_key = :s"), {"s": src}).scalar_one()
        assert n == 2
        load = c.execute(text("SELECT * FROM kg.load WHERE id = :i"), {"i": r1.load_id}).mappings().one()
        assert load["recorded_by"] == "reva_worker" and load["raw_sha256s"] == [raw]
        assert load["recorded_at"] >= now - timedelta(seconds=5)
        row = (
            c.execute(text("SELECT * FROM kg.statement_current WHERE statement_key = :k"), {"k": f"{src}:S1"})
            .mappings()
            .one()
        )
        assert row["raw_sha256"] == raw and row["recorded_at"] == load["recorded_at"]
        assert str(row["valid_lo"]) == "1900-01-01" and row["valid_hi"] is None
        # the raw artifact was registered as a verified meta.artifact by the loader
        assert c.execute(text("SELECT meta.artifact_is_verified(:s)"), {"s": raw}).scalar_one()


def test_revisions_append_and_current_view_shows_latest(kg) -> None:  # type: ignore[no-untyped-def]
    src, raw = _source(), _raw(kg, "rev")
    t0 = datetime.now(UTC) - timedelta(hours=1)
    kw = dict(source_key=src, raw_sha256s=[raw], origin="test_fixture")

    def st(obj: str, rev: str) -> KgStatement:
        return KgStatement("kgs:c", "syn:linked", obj, statement_id="S", source_revision=rev)

    assert (
        load_batch(kg.worker, kg.store, statements=[st("kgs:x", "1")], retrieved_at=t0, **kw).statements_inserted == 1
    )
    r = load_batch(kg.worker, kg.store, statements=[st("kgs:y", "2")], retrieved_at=t0 + timedelta(minutes=1), **kw)
    assert (r.statements_inserted, r.statements_new_revision) == (1, 1)
    assert [x["object_qid"] for x in _current(kg.api, src)] == ["kgs:y"]
    # same key + revision with different content: a conflicting source, not stored
    r = load_batch(kg.worker, kg.store, statements=[st("kgs:z", "2")], retrieved_at=t0 + timedelta(minutes=2), **kw)
    assert (r.statements_inserted, r.statements_conflicting) == (0, 1)
    # a later revision with unchanged content is not re-stored
    r = load_batch(kg.worker, kg.store, statements=[st("kgs:y", "3")], retrieved_at=t0 + timedelta(minutes=3), **kw)
    assert (r.statements_inserted, r.statements_unchanged) == (0, 1)
    # a batch retrieved before the stored latest row is stale and never becomes current
    r = load_batch(kg.worker, kg.store, statements=[st("kgs:w", "0")], retrieved_at=t0 - timedelta(minutes=5), **kw)
    assert (r.statements_inserted, r.statements_stale) == (0, 1)
    cur = _current(kg.api, src)
    assert [(x["object_qid"], x["source_revision"]) for x in cur] == [("kgs:y", "2")]
    with kg.api.connect() as c:  # history kept: both revisions are stored
        assert c.execute(text("SELECT count(*) FROM kg.statement WHERE source_key = :s"), {"s": src}).scalar_one() == 2


def test_deprecated_rank_rejected_and_retracts_earlier_version(kg) -> None:  # type: ignore[no-untyped-def]
    src, raw = _source(), _raw(kg, "dep")
    t0 = datetime.now(UTC) - timedelta(hours=1)
    kw = dict(source_key=src, raw_sha256s=[raw], origin="test_fixture")
    keep = KgStatement("kgs:d", "syn:linked", "kgs:e", statement_id="K")
    dep = KgStatement("kgs:d", "syn:linked", "kgs:f", statement_id="D", source_revision="1")
    gone = KgStatement("kgs:d", "syn:linked", "kgs:g", statement_id="G")
    load_batch(kg.worker, kg.store, statements=[keep, dep, gone], retrieved_at=t0, **kw)
    assert len(_current(kg.api, src)) == 3
    dep2 = KgStatement("kgs:d", "syn:linked", "kgs:f", statement_id="D", source_revision="2", rank="deprecated")
    r = load_batch(
        kg.worker,
        kg.store,
        statements=[dep2],
        retract_keys=[statement_key(gone, src)],
        retrieved_at=t0 + timedelta(minutes=1),
        **kw,
    )
    assert r.rejected_counts == {"deprecated_rank": 1} and r.statements_inserted == 0
    assert r.retractions_inserted == 2
    assert [x["statement_key"] for x in _current(kg.api, src)] == [f"{src}:K"]
    # a deprecated statement never seen before is only rejected
    r = load_batch(
        kg.worker,
        kg.store,
        statements=[KgStatement("kgs:d", "syn:linked", "kgs:h", statement_id="N", rank="deprecated")],
        retrieved_at=t0 + timedelta(minutes=2),
        **kw,
    )
    assert r.rejected_counts == {"deprecated_rank": 1} and r.retractions_inserted == 0
    with kg.api.connect() as c:
        assert (
            c.execute(text("SELECT count(*) FROM kg.statement WHERE rank NOT IN ('preferred','normal')")).scalar_one()
            == 0
        )


def test_batch_validation_and_rejections_are_reported(kg) -> None:  # type: ignore[no-untyped-def]
    src, raw = _source(), _raw(kg, "val")
    now = datetime.now(UTC)
    kw = dict(source_key=src, raw_sha256s=[raw], origin="test_fixture")
    r = load_batch(
        kg.worker,
        kg.store,
        retrieved_at=now,
        **kw,
        statements=[
            KgStatement("kgs:a", "syn:unregistered", "kgs:b"),
            KgStatement("kgs:a", "syn:linked", "kgs:b", statement_id="X"),
            KgStatement("kgs:a", "syn:linked", "kgs:c", statement_id="X"),  # same id+revision, other content
            KgStatement("kgs:a", "syn:linked", "kgs:b", statement_id="X"),  # exact duplicate: deduplicated
        ],
    )
    assert r.statements_inserted == 1
    assert r.rejected_counts == {"unknown_property": 1, "conflict_in_batch": 1}
    with pytest.raises(ValidationProblem):
        load_batch(
            kg.worker,
            kg.store,
            statements=[],
            retrieved_at=now,
            source_key="Bad Key",
            raw_sha256s=[raw],
            origin="test_fixture",
        )
    with pytest.raises(ValidationProblem):
        load_batch(
            kg.worker, kg.store, statements=[], retrieved_at=now, source_key=src, raw_sha256s=[raw], origin="synthetic"
        )
    with pytest.raises(ValidationProblem):
        load_batch(
            kg.worker,
            kg.store,
            retrieved_at=now,
            **kw,
            statements=[KgStatement("kgs:a", "syn:linked", "kgs:b")] * (MAX_STATEMENTS_PER_BATCH + 1),
        )
    # a raw artifact that is neither registered nor archived refuses the whole batch
    with pytest.raises(IntegrityFailure):
        load_batch(
            kg.worker,
            kg.store,
            statements=[],
            retrieved_at=now,
            source_key=src,
            raw_sha256s=["f" * 64],
            origin="test_fixture",
        )
    # retrieval time in the future is refused by the database
    with pytest.raises(DBAPIError):
        load_batch(kg.worker, kg.store, statements=[], retrieved_at=now + timedelta(days=1), **kw)


def test_origins_are_kept_apart(kg) -> None:  # type: ignore[no-untyped-def]
    src, raw = _source(), _raw(kg, "origin")
    st = KgStatement("kgs:o", "syn:linked", "kgs:p", statement_id="O")
    now = datetime.now(UTC)
    a = load_batch(
        kg.worker, kg.store, statements=[st], retrieved_at=now, source_key=src, raw_sha256s=[raw], origin="test_fixture"
    )
    b = load_batch(
        kg.worker, kg.store, statements=[st], retrieved_at=now, source_key=src, raw_sha256s=[raw], origin="live"
    )
    assert a.statements_inserted == b.statements_inserted == 1
    with kg.api.connect() as c:
        got = (
            c.execute(text("SELECT origin FROM kg.statement_current WHERE source_key = :s ORDER BY origin"), {"s": src})
            .scalars()
            .all()
        )
    assert got == ["live", "test_fixture"]


def _one_statement_load(kg, src: str) -> tuple[int, str]:  # type: ignore[no-untyped-def]
    raw = _raw(kg, "grant")
    r = load_batch(
        kg.worker,
        kg.store,
        statements=[KgStatement("kgs:q", "syn:linked", "kgs:r", statement_id="Q")],
        retrieved_at=datetime.now(UTC),
        source_key=src,
        raw_sha256s=[raw],
        origin="test_fixture",
    )
    return int(r.load_id), raw  # type: ignore[arg-type]


INSERT_STATEMENT = """INSERT INTO kg.statement(load_id, statement_key, origin, content_sha256, subject_qid, property_pid,
    object_qid, rank, source_key, retrieved_at, raw_sha256)
  SELECT l.id, l.source_key || chr(58) || 'forged', l.origin, repeat('0', 64), 'kgs:q', 'syn:linked', 'kgs:z', 'normal',
         l.source_key, l.retrieved_at, :raw FROM kg.load l WHERE l.id = :load"""


@pytest.mark.parametrize("role", ["api", "sim", "gate"])
def test_read_only_roles_cannot_write(kg, role: str) -> None:  # type: ignore[no-untyped-def]
    engine = getattr(kg, role)
    if engine is None:
        pytest.skip(f"{role} login not configured")
    load_id, raw = _one_statement_load(kg, _source())
    with engine.connect() as c:
        assert (
            c.execute(text("SELECT count(*) FROM kg.statement_current WHERE load_id = :l"), {"l": load_id}).scalar_one()
            == 1
        )
    for sql in (
        INSERT_STATEMENT,
        "INSERT INTO kg.property(pid, value_kind) VALUES ('syn:forged', 'item')",
        "INSERT INTO kg.load(source_key, origin, raw_sha256s, retrieved_at, loader_version) "
        "VALUES ('x', 'live', ARRAY[:raw], now(), 'x')",
        "UPDATE kg.statement SET rank = 'preferred' WHERE load_id = :load",
        "DELETE FROM kg.statement WHERE load_id = :load",
    ):
        with engine.connect() as c, pytest.raises(DBAPIError) as e:
            c.execute(text(sql), {"load": load_id, "raw": raw})
        assert "permission denied" in str(e.value.orig)


def test_history_is_immutable_and_rows_are_sealed_to_their_load(kg) -> None:  # type: ignore[no-untyped-def]
    load_id, raw = _one_statement_load(kg, _source())
    # the worker holds INSERT only
    with kg.worker.connect() as c, pytest.raises(DBAPIError) as e:
        c.execute(text("UPDATE kg.statement SET rank = 'preferred' WHERE load_id = :l"), {"l": load_id})
    assert "permission denied" in str(e.value.orig)
    # even the migration owner cannot rewrite or delete history (append-only trigger)
    for sql in (
        "UPDATE kg.statement SET rank = 'preferred' WHERE load_id = :l",
        "DELETE FROM kg.load WHERE id = :l",
        "TRUNCATE kg.statement_retraction",
    ):
        with kg.owner.begin() as c, pytest.raises(DBAPIError) as e:
            c.execute(text(sql), {"l": load_id})
        assert "mmutable" in str(e.value.orig)
    # rows cannot be attached to a load committed in an earlier transaction
    with kg.worker.begin() as c, pytest.raises(DBAPIError) as e:
        c.execute(text(INSERT_STATEMENT), {"load": load_id, "raw": raw})
    assert "sealed" in str(e.value.orig)
    # nor cite a raw artifact that is not part of their load
    with kg.worker.begin() as c, pytest.raises(DBAPIError) as e:
        new = c.execute(
            text("""INSERT INTO kg.load(source_key, origin, raw_sha256s, retrieved_at, loader_version)
                    VALUES ('kgtestforge', 'test_fixture', ARRAY[:raw], now(), 'test') RETURNING id"""),
            {"raw": raw},
        ).scalar_one()
        c.execute(text(INSERT_STATEMENT.replace(":raw", "repeat('e', 64)")), {"load": new})
    assert "does not match its load" in str(e.value.orig)
    # a load must cite verified raw artifacts
    with kg.worker.begin() as c, pytest.raises(DBAPIError) as e:
        c.execute(
            text("""INSERT INTO kg.load(source_key, origin, raw_sha256s, retrieved_at, loader_version)
                          VALUES ('kgtestforge', 'test_fixture', ARRAY[repeat('d', 64)], now(), 'test')""")
        )
    assert "no verified storage location" in str(e.value.orig)
    # recorded_at is server time; a runtime role cannot backdate a load
    with kg.worker.begin() as c, pytest.raises(DBAPIError) as e:
        c.execute(
            text("""INSERT INTO kg.load(source_key, origin, raw_sha256s, retrieved_at, loader_version, recorded_at)
                    VALUES ('kgtestforge', 'test_fixture', ARRAY[:raw], now(), 'test', now() - interval '1 day')"""),
            {"raw": raw},
        )
    assert "server-generated" in str(e.value.orig)


def test_deprecated_rank_refused_by_database_too(kg) -> None:  # type: ignore[no-untyped-def]
    load_id, raw = _one_statement_load(kg, _source())
    with kg.owner.begin() as c, pytest.raises(DBAPIError) as e:
        c.execute(text(INSERT_STATEMENT.replace("'normal'", "'deprecated'")), {"load": load_id, "raw": raw})
    assert "rank" in str(e.value.orig)


def test_kg_functions_are_not_executable_by_public(kg) -> None:  # type: ignore[no-untyped-def]
    with kg.owner.connect() as c:
        rows = (
            c.execute(
                text("""SELECT p.oid::regprocedure::text FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                     WHERE n.nspname = 'kg' AND has_function_privilege('public', p.oid, 'EXECUTE')""")
            )
            .scalars()
            .all()
        )
        assert rows == []
        tables = (
            c.execute(
                text("""SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                     WHERE n.nspname = 'kg' AND c.relkind = 'r' AND NOT EXISTS (
                       SELECT 1 FROM pg_trigger g WHERE g.tgrelid = c.oid AND g.tgname = 'immutable_record')""")
            )
            .scalars()
            .all()
        )
        assert tables == []


def test_property_registry_is_versioned_and_kind_is_fixed(kg) -> None:  # type: ignore[no-untyped-def]
    p = KgProperty("syn:versioned", "item", "versioned", "versioned")
    assert register_properties(kg.worker, [p]).versions_inserted == 1
    assert register_properties(kg.worker, [p]).unchanged == 1
    p2 = KgProperty("syn:versioned", "item", "versioned", "versioned (renamed)", "versionné")
    assert register_properties(kg.worker, [p2]).versions_inserted == 1
    with kg.api.connect() as c:
        cur = c.execute(text("SELECT label_en, label_fr FROM kg.property_current WHERE pid = 'syn:versioned'")).one()
        assert tuple(cur) == ("versioned (renamed)", "versionné")
    with pytest.raises(ValidationProblem):
        register_properties(kg.worker, [KgProperty("syn:versioned", "string", "versioned", "versioned")])
