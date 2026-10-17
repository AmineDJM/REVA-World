"""AUDFIX loader fixes on a throwaway database (docs/dev/v3_audfix.md; audit M2, M4, L8).

* M2: both loaders count (and sample) the claims whose cause or effect is not in the KG;
* L8: retraction exercised end to end -- a complete delivery minus one row retracts that row's claim (append-only
  retraction row, the claim leaves the current view, the other rows are unchanged); a Wikidata statement whose
  value became "unknown value" is retracted as ``no_longer_causal`` with the detail ``value_not_an_item`` (the
  statement still exists); a partly rejected complete delivery records ``complete_requested`` /
  ``complete_effective`` and why;
* M4: a T1 claim carries where its quote sits in the archived codebook (``codebook.quote_source``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from reva.causal import Codebook, DatasetClaim, load_dataset_claims, load_kg_claims
from reva.kg.model import KgEntity, KgProperty, KgStatement, load_batch, register_properties

pytestmark = pytest.mark.db

PROPS = [
    KgProperty("P828", "item", "has_cause", "has cause", "a pour cause", "cause_of", "cause of"),
    KgProperty("P1542", "item", "has_effect", "has effect", "a pour effet", "effect_of", "effect of"),
    KgProperty("P580", "time", "start_time", "start time"),
]


@pytest.fixture(scope="module")
def az(db):  # type: ignore[no-untyped-def]
    register_properties(db.worker, PROPS)
    return db


def _ns() -> str:
    return "af" + uuid.uuid4().hex[:8]


def _raw(db, tag: str) -> str:  # type: ignore[no-untyped-def]
    return db.store.put_bytes(f"audfix-test {tag} {uuid.uuid4()}".encode(), "raw", "application/json").sha256


def _kg(db, src: str, sts, *, entities=(), when=None):  # type: ignore[no-untyped-def]
    rep = load_batch(
        db.worker,
        db.store,
        entities=list(entities),
        statements=sts,
        source_key=src,
        raw_sha256s=[_raw(db, src)],
        retrieved_at=when or datetime.now(UTC),
        origin="test_fixture",
    )
    assert rep.rejected_total == 0, rep.rejected[:5]


def _retractions(db, prefix: str) -> list[tuple[str, str]]:  # type: ignore[no-untyped-def]
    with db.api.connect() as c:
        return [
            (r[0], r[1])
            for r in c.execute(
                text("""SELECT r.claim_key, r.reason FROM kg.causal_claim_retraction r
                         WHERE starts_with(r.claim_key, :p) ORDER BY r.claim_key"""),
                {"p": prefix},
            )
        ]


def _current_keys(db, prefix: str) -> list[str]:  # type: ignore[no-untyped-def]
    with db.api.connect() as c:
        return list(
            c.execute(
                text("SELECT claim_key FROM kg.causal_claim_current WHERE starts_with(claim_key, :p) ORDER BY 1"),
                {"p": prefix},
            ).scalars()
        )


def test_kg_loader_counts_unknown_endpoints_and_labels_somevalue_retraction(az) -> None:  # type: ignore[no-untyped-def]
    ns = _ns()
    src = f"wikidata.{ns}"
    a, b, c = f"{ns}:a", f"{ns}:b", f"{ns}:c"
    t0 = datetime.now(UTC) - timedelta(minutes=5)
    # a and b are KG entities; c exists only as the object of a statement (no entity row, no statement about it)
    _kg(
        az,
        src,
        [
            KgStatement(b, "P828", a, statement_id="K1", source_revision="r1"),
            KgStatement(a, "P1542", c, statement_id="K2", source_revision="r1"),
        ],
        entities=[KgEntity(a, "event", {"en": "A"}), KgEntity(b, "event", {"en": "B"})],
        when=t0,
    )
    r = load_kg_claims(az.worker, origin="test_fixture", source_keys=[src])
    assert (r.inserted, r.unknown_entities, r.unknown_endpoint_claims) == (2, 1, 1)
    assert r.unknown_endpoint_sample == [{"claim_key": f"kg:{src}:K2", "missing": ["effect"]}]
    # K2's value becomes "unknown value": the statement exists, it no longer names an item
    later = KgStatement(a, "P1542", value={"type": "somevalue"}, statement_id="K2", source_revision="r2")
    _kg(az, src, [later], when=t0 + timedelta(minutes=1))
    r = load_kg_claims(az.worker, origin="test_fixture", source_keys=[src])
    assert r.retracted == 1 and r.retraction_details == {"value_not_an_item": 1}
    assert _retractions(az, f"kg:{src}:") == [(f"kg:{src}:K2", "no_longer_causal")]
    assert _current_keys(az, f"kg:{src}:") == [f"kg:{src}:K1"]
    with az.api.connect() as conn:
        par = conn.execute(
            text("SELECT parameters FROM kg.causal_load WHERE id = :i"), {"i": r.load_ids[-1]}
        ).scalar_one()
    assert par["statements_value_not_an_item"] == 1


def test_dataset_delivery_minus_one_row_retracts_it(az) -> None:  # type: ignore[no-untyped-def]
    ns = _ns()
    src, raw, book = f"{ns}.data", _raw(az, "ds"), _raw(az, "codebook")
    cb = Codebook(
        "Synthetic codebook",
        "1",
        None,
        {"outcome": "The coded outcome (verbatim)."},
        sources={"outcome": {"document": "codebook.pdf", "sha256": book, "page": 7, "extraction": "pdftotext"}},
    )
    t0 = datetime.now(UTC) - timedelta(minutes=30)
    _kg(az, src, [], entities=[KgEntity(f"{ns}:ev{i}", "event", {"en": f"E{i}"}) for i in range(3)], when=t0)

    def claim(i: int, **kw):  # type: ignore[no-untyped-def]
        base = dict(
            cause_id=f"{ns}:ev{i}",
            effect_id=f"{ns}:ev{i}:outcome",
            relation="coded_outcome",
            selector={"file": "d.csv", "row": i},
            raw_sha256=raw,
            codebook_term="outcome",
            source_claim_id=f"{i}",
        )
        return DatasetClaim(**{**base, **kw})

    kw = dict(
        source_key=src,
        dataset="cases",
        dataset_revision="v1",
        codebook=cb,
        raw_sha256s=[raw, book],
        origin="test_fixture",
    )
    r = load_dataset_claims(az.worker, az.store, retrieved_at=t0, claims=[claim(i) for i in range(3)], **kw)
    assert (r.inserted, r.unknown_endpoint_claims) == (3, 3)  # outcome entities are not in the KG
    assert {s["missing"][0] for s in r.unknown_endpoint_sample} == {"effect"}
    with az.api.connect() as conn:
        cbk = conn.execute(
            text("SELECT codebook FROM kg.causal_claim_current WHERE claim_key = :k"), {"k": f"ds:{src}:cases:0"}
        ).scalar_one()
    assert cbk["quote_source"] == {"document": "codebook.pdf", "sha256": book, "page": 7, "extraction": "pdftotext"}
    assert "sources" not in cbk and cbk["definition"] == "The coded outcome (verbatim)."
    # the same delivery minus row 1: row 1 is retracted (append-only), rows 0 and 2 unchanged
    r = load_dataset_claims(
        az.worker, az.store, retrieved_at=t0 + timedelta(minutes=1), claims=[claim(0), claim(2)], **kw
    )
    assert (r.retracted, r.unchanged, r.inserted) == (1, 2, 0)
    assert _retractions(az, f"ds:{src}:") == [(f"ds:{src}:cases:1", "removed_at_source")]
    assert _current_keys(az, f"ds:{src}:") == [f"ds:{src}:cases:0", f"ds:{src}:cases:2"]
    with az.api.connect() as conn:  # the retracted row is still stored (nothing is deleted)
        assert (
            conn.execute(
                text("SELECT count(*) FROM kg.causal_claim WHERE claim_key = :k"), {"k": f"ds:{src}:cases:1"}
            ).scalar_one()
            == 1
        )
    # the row comes back: reinstated
    r = load_dataset_claims(
        az.worker, az.store, retrieved_at=t0 + timedelta(minutes=2), claims=[claim(i) for i in range(3)], **kw
    )
    assert r.reinstated == 1
    # a complete delivery with a rejected row is applied as partial, and the load says so
    r = load_dataset_claims(
        az.worker,
        az.store,
        retrieved_at=t0 + timedelta(minutes=3),
        claims=[claim(0, mechanism="New quoted text."), claim(9, codebook_term="nope")],
        **kw,
    )
    assert r.retracted == 0 and r.rejected_counts["complete_downgraded_to_partial"] == 1
    with az.api.connect() as conn:
        par = conn.execute(
            text("SELECT parameters FROM kg.causal_load WHERE id = :i"), {"i": r.load_ids[-1]}
        ).scalar_one()
    assert (par["complete_requested"], par["complete_effective"], par["complete"]) == (True, False, False)
    assert par["complete_downgrade_reason"] == "1 row(s) rejected"
    # an empty complete delivery retracts every remaining claim of the dataset (a source dropped all its claims)
    r = load_dataset_claims(az.worker, az.store, retrieved_at=t0 + timedelta(minutes=4), claims=[], **kw)
    assert r.retracted == 3 and _current_keys(az, f"ds:{src}:") == []
