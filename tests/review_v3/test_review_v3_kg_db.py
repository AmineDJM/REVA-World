"""KG store (reva.kg.model.load_batch): defects found by the V3 review (docs/dev/review_v3.md, KG-*).

Synthetic provider-scoped ids; nothing here is a real-world fact.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from reva.kg.model import KgProperty, KgStatement, load_batch, register_properties, statement_key

pytestmark = pytest.mark.db

PROP = KgProperty("rv3:linked", "item", "rv3_linked_to", "linked to")


def test_retracted_statement_reasserted_by_a_revisionless_source_is_current_again(db) -> None:  # type: ignore[no-untyped-def]
    register_properties(db.worker, [PROP])
    src = f"rv3kg{uuid.uuid4().hex[:8]}"
    st = KgStatement("rv3:a", "rv3:linked", "rv3:b")  # no statement id, no source revision

    def raw(tag: str) -> str:
        return db.store.put_bytes(f"rv3 {tag} {uuid.uuid4()}".encode(), "raw", "application/json").sha256

    t0 = datetime.now(UTC) - timedelta(minutes=3)
    kw = dict(source_key=src, origin="test_fixture")
    load_batch(db.worker, db.store, statements=[st], raw_sha256s=[raw("v1")], retrieved_at=t0, **kw)
    # the next delivery of the dataset no longer contains the statement
    load_batch(
        db.worker,
        db.store,
        retract_keys=[statement_key(st, src)],
        raw_sha256s=[raw("v2")],
        retrieved_at=t0 + timedelta(minutes=1),
        **kw,
    )
    # a later delivery asserts it again
    rep = load_batch(
        db.worker, db.store, statements=[st], raw_sha256s=[raw("v3")], retrieved_at=t0 + timedelta(minutes=2), **kw
    )
    with db.api.connect() as c:
        n = c.execute(text("SELECT count(*) FROM kg.statement_current WHERE source_key = :s"), {"s": src}).scalar_one()
    assert n == 1, f"re-asserted statement still retracted (load report: unchanged={rep.statements_unchanged})"
    assert rep.statements_reinstated == 1 and rep.statements_inserted == 1

    # reloading the same delivery is idempotent; a second retract/reassert cycle works too; a reload that
    # predates the retraction does not reinstate (stale)
    kw2 = dict(source_key=src, origin="test_fixture")
    rep = load_batch(
        db.worker, db.store, statements=[st], raw_sha256s=[raw("v3b")], retrieved_at=t0 + timedelta(minutes=2), **kw2
    )
    assert rep.statements_unchanged == 1 and rep.statements_inserted == 0
    load_batch(
        db.worker,
        db.store,
        retract_keys=[statement_key(st, src)],
        raw_sha256s=[raw("v4")],
        retrieved_at=t0 + timedelta(minutes=3),
        **kw2,
    )
    old = load_batch(
        db.worker, db.store, statements=[st], raw_sha256s=[raw("v2b")], retrieved_at=t0 + timedelta(minutes=1), **kw2
    )
    assert old.statements_reinstated == 0 and old.statements_inserted == 0
    rep = load_batch(
        db.worker, db.store, statements=[st], raw_sha256s=[raw("v5")], retrieved_at=t0 + timedelta(minutes=4), **kw2
    )
    assert rep.statements_reinstated == 1
    with db.api.connect() as c:
        rows = c.execute(
            text("""SELECT count(*) AS n, count(reinstates_row_id) AS r FROM kg.statement WHERE source_key = :s"""),
            {"s": src},
        ).one()
        cur = c.execute(
            text("SELECT count(*) FROM kg.statement_current WHERE source_key = :s"), {"s": src}
        ).scalar_one()
    assert (rows.n, rows.r, cur) == (3, 2, 1)


def test_reinstatement_must_name_a_retracted_row_of_the_same_statement(db) -> None:  # type: ignore[no-untyped-def]
    """The 0011 trigger refuses a reinstates_row_id that is not a retracted row of the same key."""
    register_properties(db.worker, [PROP])
    src = f"rv3kg{uuid.uuid4().hex[:8]}"
    st = KgStatement("rv3:a", "rv3:linked", "rv3:b")
    raw = db.store.put_bytes(f"rv3 x {uuid.uuid4()}".encode(), "raw", "application/json").sha256
    t = datetime.now(UTC) - timedelta(minutes=1)
    load_batch(
        db.worker, db.store, statements=[st], raw_sha256s=[raw], retrieved_at=t, source_key=src, origin="test_fixture"
    )
    with db.owner.begin() as c:
        row = c.execute(text("SELECT * FROM kg.statement WHERE source_key = :s"), {"s": src}).mappings().one()
        lid = c.execute(
            text("""INSERT INTO kg.load(source_key, origin, raw_sha256s, retrieved_at, loader_version, parameters)
                    VALUES (:sk, 'test_fixture', ARRAY[:r], :t, 'test', '{}'::jsonb) RETURNING id"""),
            {"sk": src, "r": raw, "t": t},
        ).scalar_one()
        with pytest.raises(Exception, match="reinstates a row that is not a retracted row"):
            with c.begin_nested():
                c.execute(
                    text("""INSERT INTO kg.statement(load_id, statement_key, origin, content_sha256, subject_qid,
                              property_pid, object_qid, rank, source_key, retrieved_at, reinstates_row_id)
                            VALUES (:l, :k, 'test_fixture', :h, 'rv3:a', 'rv3:linked', 'rv3:b', 'normal', :sk, :t, :id)"""),
                    {
                        "l": lid,
                        "k": row["statement_key"],
                        "h": row["content_sha256"],
                        "sk": src,
                        "t": t,
                        "id": row["id"],
                    },
                )
