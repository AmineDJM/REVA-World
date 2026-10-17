"""grading.yaml version 2 (agent CAUSFIX, docs/dev/v3_causfix.md): Wikidata tiering of non-source references and
WDQS rows, the temporal rule for entities dated by inception, attribution / nature / doubtful-certainty caps,
self-loop and cycle flags. Pure functions; the database side is in tests/integration/test_causfix_store.py.

Dates and ids mirror the Wikidata findings of docs/dev/v3_causwd.md; nothing here is a REVA statement."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from reva.causal import grading as gr
from reva.causal.grader import _qualified, structural_flags
from reva.causal.model import Deferred, claim_from_statement
from reva.causal.temporal import DateValue, claim_time_window, event_dates, temporal_detail, window_of_time

RULES = gr.load_rules()


# ---------------------------------------------------------------------------------------------- tier (H1)
@pytest.mark.parametrize(
    ("refs", "tier", "basis"),
    [
        ([], "T4", "no_references"),
        ([{}], "T4", "no_references"),
        ([{"P143": ["Q58251"]}], "T4", "non_source_references_only"),
        (
            [{"P143": ["Q58251"], "P4656": ["https://az.wikipedia.org/?oldid=7446971"]}],
            "T4",
            "non_source_references_only",
        ),
        (
            [{"P4656": ["https://ru.wikipedia.org/?oldid=1"], "P813": [{"time": "+2024-01-01"}]}],
            "T4",
            "non_source_references_only",
        ),
        ([{"P3452": ["Q112042826"]}], "T4", "non_source_references_only"),
        ([{"P887": ["Q69652283"]}], "T4", "non_source_references_only"),
        ([{"P143": ["Q328"]}, {"P854": ["https://example.org/a"]}], "T3", "references"),
        ([{"P143": ["Q328"], "P248": ["Q36578"]}], "T3", "references"),
        ([{"P356": ["10.1000/xyz"]}], "T2", "citation"),
    ],
)
def test_kg_tier_non_source_references(refs: list[dict[str, Any]], tier: str, basis: str) -> None:
    assert gr.kg_tier("wikidata.wbapi", refs) == (tier, basis)


def _st(source_key: str, refs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": 1,
        "statement_key": f"{source_key}:Q361$x",
        "statement_id": "Q361$x",
        "subject_qid": "Q361",
        "property_pid": "P1542",
        "object_qid": "Q34266",
        "qualifiers": {},
        "rank": "normal",
        "refs": refs,
        "source_key": source_key,
        "source_revision": "1",
        "raw_sha256": "a" * 64,
        "retrieved_at": datetime(2026, 10, 17, tzinfo=UTC),
    }


def test_wdqs_rows_are_references_unknown_not_unreferenced() -> None:
    """WDQS listings never read references: the loader defers the row instead of tiering it T4."""
    assert gr.kg_tier("wikidata.wdqs", []) == (None, "references_unknown")
    with pytest.raises(Deferred) as e:
        claim_from_statement(_st("wikidata.wdqs", []))
    assert e.value.reason == "references_unknown"
    row = claim_from_statement(_st("wikidata.wbapi", [{"P143": ["Q58251"]}]))
    assert row["source_tier"] == "T4" and row["citation"] is None
    assert row["upstream_keys"] == ["community:wikidata"]  # a pointer is not an independent upstream (H2)


# ---------------------------------------------------------------------------------------------- corroboration (H2)
def _ref(key: str, refs: list[dict[str, Any]]) -> gr.ClaimRef:
    return gr.ClaimRef(key, "wikidata.wbapi", tuple(gr.upstream_keys(source_key="wikidata.wbapi", refs=refs)))


def test_mirror_statements_with_non_source_references_do_not_corroborate() -> None:
    # 27138 / 27363 shape: a Wikipedia import on one side, a Wikidata inference on the mirror
    assert gr.corroboration([_ref("a", [{"P143": ["Q206855"]}]), _ref("b", [{"P3452": ["Q112042826"]}])]).count == 1
    # 27346 / 27350 shape: a URL on one side, an inference on the other
    assert (
        gr.corroboration(
            [_ref("a", [{"P854": ["https://europarl.europa.eu/x"]}]), _ref("b", [{"P3452": ["Q1"]}])]
        ).count
        == 1
    )
    # 27214 / 27568 shape: the same quotation (P1683) under two references counts once
    quote = "the Act of Declaration of Independence of Ukraine"
    a = _ref("a", [{"P143": ["Q199698"]}, {"P1683": [quote], "P1476": ["t1"]}])
    b = _ref("b", [{"P1683": [quote.upper()], "P577": ["+1991"]}])
    assert gr.corroboration([a, b]).count == 1
    # two genuinely different sources still corroborate
    assert gr.corroboration([_ref("a", [{"P854": ["https://a.org"]}]), _ref("b", [{"P248": ["Q5"]}])]).count == 2


# ---------------------------------------------------------------------------------------------- temporal
def _dates(**pids: tuple[str, int]) -> Any:
    vals = []
    for pid, (iso, prec) in pids.items():
        w = window_of_time(iso, prec, pid)
        assert w is not None
        vals.append(DateValue(pid, _lit(w.lo), _lit(w.hi), prec, iso))
    return event_dates(vals)


def _lit(t: tuple[int, int, int]) -> str:
    y, m, d = t
    return f"{abs(y) + (1 if y <= 0 else 0):04d}-{m:02d}-{d:02d}" + (" BC" if y <= 0 else "")


WWI = {"P580": ("+1914-07-28T00:00:00Z", 11)}


def test_effect_entity_dated_by_inception_is_undetermined_not_inconsistent() -> None:
    # World War I -> German Empire (1871-01-18 .. 1918-11-09): the founding date does not date the effect
    german_empire = _dates(P571=("+1871-01-18T00:00:00Z", 11), P576=("+1918-11-09T00:00:00Z", 11))
    tc, d = temporal_detail(_dates(**WWI), german_empire)
    assert tc == "undetermined" and d["entity_dated_by_inception"] == ["effect"]
    assert d["effect_time"]["kind"] == "entity_existence" and d["effect_time"]["to"] == "1918-11-09"
    assert "not an event" in d["reason"] and d["effect_start"]["basis"] == "P571"
    # no dissolution date: the span is open
    tc2, d2 = temporal_detail(_dates(**WWI), _dates(P571=("+1871-01-18T00:00:00Z", 11)))
    assert tc2 == "undetermined" and d2["effect_time"]["to"] is None
    # the cause precedes the founding (WWI -> Weimar Republic 1918-11-09): consistent for every reading
    weimar = _dates(P571=("+1918-11-09T00:00:00Z", 11), P576=("+1933-00-00T00:00:00Z", 9))
    assert temporal_detail(_dates(**WWI), weimar)[0] == "consistent"
    # the cause starts after the entity was dissolved: inconsistent for every reading
    assert temporal_detail(_dates(P580=("+1920-00-00T00:00:00Z", 9)), german_empire)[0] == "inconsistent"


def test_cause_entity_dated_by_inception() -> None:
    eighty_years_war = _dates(P580=("+1568-00-00T00:00:00Z", 9))
    # Protestant Church in the Netherlands (founded 2004) -> Eighty Years' War: a genuine order problem
    tc, d = temporal_detail(_dates(P571=("+2004-05-01T00:00:00Z", 11)), eighty_years_war)
    assert tc == "inconsistent" and d["entity_dated_by_inception"] == ["cause"]
    # an entity founded before the effect may have acted at any time of its existence: undetermined
    assert temporal_detail(_dates(P571=("+1922-12-30T00:00:00Z", 11)), _dates(P580=("+1947-00-00T00:00:00Z", 9)))[
        0
    ] == ("undetermined")
    # dissolved before the effect started: consistent
    gone = _dates(P571=("+1500-00-00T00:00:00Z", 9), P576=("+1550-00-00T00:00:00Z", 9))
    assert temporal_detail(gone, eighty_years_war)[0] == "consistent"


def test_genuine_event_order_problems_stay_inconsistent() -> None:
    # Truman Doctrine (1947-03-12) -> Cold War (start 1945): unchanged event-to-event rule
    tc, d = temporal_detail(_dates(P585=("+1947-03-12T00:00:00Z", 11)), _dates(P580=("+1945-00-00T00:00:00Z", 9)))
    assert tc == "inconsistent" and "entity_dated_by_inception" not in d and "cause_time" not in d
    assert (
        temporal_detail(_dates(P580=("+1914-00-00T00:00:00Z", 9)), _dates(P580=("+1914-07-28T00:00:00Z", 11)))[0]
        == "overlapping"
    )


def test_claim_time_qualifier_dates_an_entity_effect() -> None:
    german_empire = _dates(P571=("+1871-01-18T00:00:00Z", 11), P576=("+1918-11-09T00:00:00Z", 11))
    q = claim_time_window({"pit": {"time": "+1918-11-09T00:00:00Z", "precision": 11}})
    tc, d = temporal_detail(_dates(**WWI), german_empire, q)
    assert tc == "consistent" and d["effect_time"]["kind"] == "claim_time_qualifier"
    assert claim_time_window(None) is None and claim_time_window({"end": {"time": "+1", "precision": 9}}) is None
    # event effects ignore the qualifier (version 1 rule unchanged)
    tc2, d2 = temporal_detail(_dates(**WWI), _dates(P580=("+1917-00-00T00:00:00Z", 9)), q)
    assert tc2 == "consistent" and "effect_time" not in d2


# ---------------------------------------------------------------------------------------------- caps (H3, H4)
@pytest.mark.parametrize(
    ("flags", "rule"),
    [
        ({"attributed": True}, "grade.D.attributed"),
        ({"nature_qualified": True}, "grade.D.nature_qualified"),
        ({"doubtful": True}, "grade.D.source_doubtful"),
    ],
)
def test_attributed_qualified_and_doubtful_claims_are_capped(flags: dict[str, bool], rule: str) -> None:
    g = gr.decide(gr.GradeInputs("T1", "consistent", 3, False, **flags), RULES)
    assert (g.grade, g.deciding_rule) == ("D", rule)
    # E rules still come first
    g2 = gr.decide(gr.GradeInputs("T3", "inconsistent", 1, False, **flags), RULES)
    assert g2.grade == "E"


def test_qualifier_flags_from_claim_rows() -> None:
    c = {
        "qualifiers": {"P3680": ["Q7747"], "P5102": ["Q132555"]},
        "scope": {"source_validity": [{"field": "eventValidity", "code": "1", "certainty": "doubtful"}]},
    }
    assert _qualified(c) == (True, True, True)
    ok = {"qualifiers": {}, "scope": {"source_validity": [{"certainty": "definite_or_probable"}]}}
    assert _qualified(ok) == (False, False, False)


def test_note_facts_are_declared_and_ordered() -> None:
    assert set(gr.NOTE_FACTS) <= set(RULES.facts) and set(gr.CHAIN_QUARANTINE_FACTS) <= set(gr.NOTE_FACTS)
    g = gr.decide(
        gr.GradeInputs("T4", "undetermined", 1, False, True), RULES, ["consistency.cycle", "attribution.supported_by"]
    )
    assert g.rule_ids[4:] == ("attribution.supported_by", "consistency.cycle", "grade.D.attributed")


# ---------------------------------------------------------------------------------------------- structure (L7)
def test_self_loops_and_cycles_are_flagged() -> None:
    def c(i: int, a: str, b: str, rel: str = "has_effect") -> dict[str, Any]:
        return {"id": i, "cause_id": a, "effect_id": b, "stance": "asserts", "family": "cause", "relation": rel}

    claims = [
        c(1, "Q625298", "Q625298"),  # peace treaty has cause peace treaty
        c(2, "Q8683", "Q189266"),  # Cold War -> Barbarossa -> Eastern Front -> Cold War
        c(3, "Q189266", "Q153376"),
        c(4, "Q153376", "Q8683"),
        c(5, "Q8683", "Q1"),  # leaves the cycle
        c(6, "x", "x:outcome", "coded_outcome"),
        c(7, "x:outcome", "x", "coded_outcome"),  # outcome relations are excluded from cycle detection
    ]
    flags = structural_flags(claims, {"coded_outcome"})
    assert flags == {1: "consistency.self_loop", 2: "consistency.cycle", 3: "consistency.cycle", 4: "consistency.cycle"}
