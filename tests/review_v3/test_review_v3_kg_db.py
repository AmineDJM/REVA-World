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


@pytest.mark.xfail(
    strict=True,
    reason="KG-1: a statement whose source has no revision ids (source_revision NULL: dataset statements such "
    "as UCDP/UNGA/CShapes) can never be reinstated once retracted: the reload matches the retracted row as "
    "'unchanged' (NULL IS NOT DISTINCT FROM NULL) before the retraction check, and the unique "
    "(statement_key, origin, source_revision) identity would refuse a new row anyway",
)
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
