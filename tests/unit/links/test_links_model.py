"""Link records: dates, temporal overlap, the no-evidence-no-link invariant, input adapters.

Statement ids prefixed ``syn:`` are synthetic (unit-level logic only); real data is in
test_links_fixture.py.
"""

from __future__ import annotations

import pytest

from reva.kg.model import KgStatement, KgTime
from reva.links.model import (
    AtlasRowRef,
    Evidence,
    Link,
    LinkError,
    NoEvidence,
    PartialDate,
    Stmt,
    Temporal,
    split_indicator,
    stmt_from_any,
)

D = PartialDate.parse


def _ev(sid: str = "syn:1") -> Evidence:
    return Evidence(kind="kg_statement", source="wikidata", statement_id=sid)


def test_partial_date_parsing() -> None:
    assert D("+1494-06-07T00:00:00Z", 11) == PartialDate(1494, 6, 7, 11)
    assert D("+1500-00-00T00:00:00Z", 9) == PartialDate(1500, None, None, 9)
    assert D({"time": "+1956-10-29T00:00:00Z", "precision": 11}).iso() == "1956-10-29"
    assert D(KgTime("+1960", 9)).iso() == "1960"
    assert D(1822).iso() == "1822"
    assert D(None) is None
    with pytest.raises(LinkError):
        D("sometime")


def test_temporal_overlap() -> None:
    a = Temporal.of(D(1949), D(1999))
    b = Temporal.of(D(1990), None)
    assert a.overlap(b) == Temporal.of(D(1990), D(1999))
    assert Temporal.of(D(1900), D(1910)).overlap(Temporal.of(D(1950), None)) is None  # disjoint: no link
    assert a.overlap(Temporal("undated")).kind == "undated"  # not established, not refuted
    p = Temporal("point", point=D(1956))
    assert p.overlap(a) == p
    with pytest.raises(LinkError):
        Temporal.of(D(2000), D(1990))


def test_no_evidence_no_link() -> None:
    with pytest.raises(NoEvidence):
        Link("Q155", "Q45", "shared_official_language", False, Temporal("undated"), ())
    with pytest.raises(NoEvidence):
        Evidence(kind="kg_statement", source="wikidata", statement_id="")
    with pytest.raises(NoEvidence):
        Evidence(kind="atlas_row", source="imf_dots", raw_sha256="a" * 64, selector="")
    with pytest.raises(NoEvidence):
        Stmt(statement_id="", subject="Q1", pid="P37", object="Q2")


def test_link_checks() -> None:
    with pytest.raises(LinkError):
        Link("Q155", "Q155", "co_member", False, Temporal("undated"), (_ev(),))
    with pytest.raises(LinkError):
        Link("not a node", "Q1", "co_member", False, Temporal("undated"), (_ev(),))
    # a weight needs a source quantity (atlas row / dataset statement), never a Wikidata edge
    with pytest.raises(LinkError):
        Link("Q155", "Q45", "bilateral_trade", True, Temporal("snapshot", year=2020), (_ev(),), weight="1")
    row_ev = Evidence(kind="atlas_row", source="imf_dots", raw_sha256="a" * 64, selector='{"row":1}')
    with pytest.raises(LinkError):
        Link("Q155", "Q45", "bilateral_trade", True, Temporal("snapshot", year=2020), (row_ev,), weight=1.5)  # type: ignore[arg-type]
    lk = Link("Q155", "Q45", "bilateral_trade", True, Temporal("snapshot", year=2020), (row_ev,), weight="12.50")
    assert lk.to_json()["weight"] == "12.50"
    assert Link("iso3:XKX", "Q45", "migrant_stock", True, Temporal("snapshot", year=2020), (row_ev,), weight="3")


def test_deprecated_statements_refused() -> None:
    with pytest.raises(LinkError):
        Stmt(statement_id="syn:1", subject="Q1", pid="P37", object="Q2", rank="deprecated")


def test_stmt_from_kgcore_statement_and_db_row() -> None:
    kg = KgStatement(
        subject_qid="Q155",
        property_pid="P37",
        object_qid="Q5146",
        start=KgTime("+1822-09-07T00:00:00Z", 11),
        source_key="wikidata",
        source_revision="123",
        statement_id="Q155$abc",
    )
    s = stmt_from_any(kg)
    assert (s.subject, s.pid, s.object, s.statement_id) == ("Q155", "P37", "Q5146", "Q155$abc")
    assert s.temporal == Temporal.of(PartialDate(1822, 9, 7, 11), None)
    row = {
        "statement_key": "wikidata:Q155$def",
        "statement_id": None,
        "subject_qid": "Q155",
        "property_pid": "P298",
        "object_qid": None,
        "value": '{"type": "external_id", "value": "BRA"}',
        "pit_time": None,
        "pit_precision": None,
        "start_time": "+1960",
        "start_precision": 9,
        "qualifiers": "{}",
        "rank": "normal",
        "refs": "[]",
        "source_key": "wikidata",
        "origin": "live",
    }
    r = stmt_from_any(row)
    assert r.statement_id == "wikidata:Q155$def" and r.value == "BRA" and r.object is None
    assert r.start == PartialDate(1960)
    t = stmt_from_any(
        {
            "statement_id": "x",
            "subject_qid": "Q1",
            "property_pid": "P571",
            "value": {"type": "time", "time": "+1500-04-22T00:00:00Z", "precision": 11},
        }
    )
    assert t.value_date == PartialDate(1500, 4, 22, 11)


def test_atlas_row_counterpart_from_folded_indicator() -> None:
    assert split_indicator("TXG_FOB_USD|counterpart=USA|freq=A") == ("TXG_FOB_USD", {"counterpart": "USA", "freq": "A"})
    base = {
        "source": "imf",
        "dataset": "dots",
        "period_start": "2022-01-01",
        "period_end": "2023-01-01",
        "value": "10",
        "value_text": None,
        "unit": "USD",
        "raw_sha256": "b" * 64,
        "selector": {"row": 3},
        "flags": "{}",
    }
    r = AtlasRowRef.from_mapping({**base, "indicator": "TXG_FOB_USD|counterpart=usa", "iso3": "BRA"})
    assert r.subject_object == ("BRA", "USA") and r.year == 2022 and r.selector == '{"row":3}'
    m = AtlasRowRef.from_mapping({**base, "indicator": "MIGSTOCK|origin=PRT", "iso3": "BRA"})
    assert m.subject_object == ("PRT", "BRA")  # origin -> destination
