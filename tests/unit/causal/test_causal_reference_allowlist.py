"""The Wikidata reference allow-list keeps DOI (P356) and ISBN (P212 / P957) snaks (CAUSOPS, CAUSCORE request 2),
so a Wikidata causal statement whose own reference cites a publication reaches tier T2 through the real path:
``wbgetentities`` JSON -> WDHARVEST extraction -> KG statement -> CAUSCORE claim row."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from reva.adapters.connectors import wikidata as wd
from reva.causal import grading as gr
from reva.causal.model import claim_from_statement
from reva.kg.harvest import to_kg_statement

NOW = datetime(2026, 10, 17, tzinfo=UTC)


def _snak(pid: str, datatype: str, value: Any, vtype: str = "string") -> dict[str, Any]:
    return {
        "snaktype": "value",
        "property": pid,
        "datatype": datatype,
        "datavalue": {"type": vtype, "value": value},
    }


def _claim(sid: str, refs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": sid,
        "rank": "normal",
        "type": "statement",
        "mainsnak": _snak("P828", "wikibase-item", {"entity-type": "item", "id": "Q201684"}, "wikibase-entityid"),
        "references": [{"hash": f"h{i}", "snaks": r} for i, r in enumerate(refs)],
    }


def _entity() -> dict[str, Any]:
    doi = {"P356": [_snak("P356", "external-id", "10.1017/S0022050700000000")]}
    isbn = {
        "P212": [_snak("P212", "external-id", "978-0-691-14216-6")],
        "P957": [_snak("P957", "external-id", "0-691-14216-1")],
        "P9999": [_snak("P9999", "string", "not allow-listed")],
    }
    url = {"P854": [_snak("P854", "url", "https://example.org/x")]}
    return {
        "id": "Q8698",
        "lastrevid": 2_553_658_571,
        "labels": {"en": {"language": "en", "value": "Great Depression"}},
        "claims": {"P828": [_claim("Q8698$DOI", [doi]), _claim("Q8698$ISBN", [isbn]), _claim("Q8698$URL", [url])]},
    }


def _row(st: Any, row_id: int) -> dict[str, Any]:
    return {
        "id": row_id,
        "statement_key": f"{st.source_key}:{st.statement_id}",
        "statement_id": st.statement_id,
        "origin": "test_fixture",
        "subject_qid": st.subject_qid,
        "property_pid": st.property_pid,
        "object_qid": st.object_qid,
        "qualifiers": dict(st.qualifiers or {}),
        "rank": "normal",
        "refs": list(st.references or []),
        "source_key": st.source_key,
        "source_revision": "2553658571",
        "raw_sha256": "a" * 64,
        "retrieved_at": NOW,
        "start_time": None,
        "start_precision": None,
        "end_time": None,
        "end_precision": None,
        "pit_time": None,
        "pit_precision": None,
    }


def test_reference_allowlist_has_doi_and_isbn() -> None:
    refs = wd.load_allowlist().references
    assert {"P356", "P212", "P957"} <= refs and {"P248", "P854", "P813", "P143"} <= refs
    assert set(gr.DOI_PIDS) | set(gr.ISBN_PIDS) <= refs  # every identifier the grader reads is harvested


def test_doi_and_isbn_references_reach_t2_through_the_real_path() -> None:
    e = wd.extract_entity(_entity(), wd.load_allowlist(), raw_sha256="a" * 64, retrieved_at=NOW)
    by = {s.statement_id: s for s in e.statements}
    assert by["Q8698$DOI"].references == [{"P356": ["10.1017/S0022050700000000"]}]
    assert by["Q8698$ISBN"].references == [{"P212": ["978-0-691-14216-6"], "P957": ["0-691-14216-1"]}]
    rows = {sid: _row(to_kg_statement(s), i) for i, (sid, s) in enumerate(sorted(by.items()), start=1)}
    doi = claim_from_statement(rows["Q8698$DOI"])
    assert doi["source_tier"] == "T2" and doi["citation"] == {"doi": ["10.1017/S0022050700000000"]}
    assert (doi["cause_id"], doi["effect_id"], doi["relation"]) == ("Q201684", "Q8698", "has_cause")
    isbn = claim_from_statement(rows["Q8698$ISBN"])
    assert isbn["source_tier"] == "T2" and set(isbn["citation"]) == {"isbn"}
    assert claim_from_statement(rows["Q8698$URL"])["source_tier"] == "T3"  # a URL alone is not scholarly
    assert gr.reference_keys(rows["Q8698$DOI"]["refs"][0]) == ["doi:10.1017/s0022050700000000"]
