"""grading.yaml version 3 and the second-audit fixes (agent FIXA, docs/dev/v3_fixa.md; audit
docs/dev/review_v3_causal_audit2.md N1, N2, N3, N7, N9, N11, M1, M2). Pure functions; the database side is in
tests/integration/test_fixa_store.py. Synthetic ids; nothing here is a REVA statement."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from reva.causal import grading as gr
from reva.causal.grader import compute, outcome_dated_by_cause_end
from reva.causal.graph import _ref, attribution_of, licence_warning
from reva.causal.labels import QUALIFIER_ITEM_PIDS, unloaded_label
from reva.causal.model import dataset_retraction_reason
from reva.causal.temporal import DateValue, event_dates

RULES = gr.load_rules()
ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------------------------- rules v3
def test_version_3_rules_and_facts() -> None:
    assert RULES.version == "3"
    ids = [r.id for r in RULES.rules]
    assert ids.index("grade.D.sourcing_hedged") < ids.index("grade.A.expert_consistent_corroborated")
    assert ids.index("grade.C.t2_wikidata_single") < ids.index("grade.B.expert")
    for f in (
        "nature.sourcing_circumstances",
        "scope.qualified",
        "temporal.effect_dated_by_cause_end",
        "tier.T2.wikidata_reference",
    ):
        assert RULES.facts[f]["en"] and RULES.facts[f]["fr"]
    assert "nature.sourcing_circumstances" in gr.CHAIN_QUARANTINE_FACTS
    assert "P1480" == gr.SOURCING_CIRCUMSTANCES_PID
    assert set(gr.SCOPE_QUALIFIER_PIDS) <= set(QUALIFIER_ITEM_PIDS)


@pytest.mark.parametrize(
    ("inp", "grade", "rule"),
    [
        # N2: a P1480-hedged referenced statement is capped at D (was C)
        (
            gr.GradeInputs("T3", "consistent", 1, False, hedged=True, community_edited=True),
            "D",
            "grade.D.sourcing_hedged",
        ),
        (
            gr.GradeInputs("T3", "consistent", 2, False, hedged=True, community_edited=True),
            "D",
            "grade.D.sourcing_hedged",
        ),
        # N11: a Wikidata statement citing a DOI, alone -> C (was B); corroborated -> the T1/T2 rules
        (gr.GradeInputs("T2", "undetermined", 1, False, community_edited=True), "C", "grade.C.t2_wikidata_single"),
        (gr.GradeInputs("T2", "undetermined", 2, False, community_edited=True), "B", "grade.B.expert"),
        (gr.GradeInputs("T2", "consistent", 1, False), "B", "grade.B.expert"),  # a dataset's T2 claim: unchanged
        # N7: a coded outcome dated by its cause's end is never A on that "consistent"
        (gr.GradeInputs("T1", "consistent", 2, False, effect_dated_by_cause_end=True), "B", "grade.B.expert"),
        (gr.GradeInputs("T1", "consistent", 2, False), "A", "grade.A.expert_consistent_corroborated"),
        # a hedged statement that is also contested and uncorroborated stays E (E rules first)
        (gr.GradeInputs("T3", "consistent", 1, True, hedged=True), "E", "grade.E.contested_uncorroborated"),
    ],
)
def test_version_3_decisions(inp: gr.GradeInputs, grade: str, rule: str) -> None:
    g = gr.decide(inp, RULES)
    assert (g.grade, g.deciding_rule) == (grade, rule)


# ---------------------------------------------------------------------------------------------- grader
def _claim(i: int, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": i,
        "claim_key": f"kg:wikidata.wbapi:Q9100000{i}$x",
        "cause_id": f"Q910000{i}0",
        "effect_id": f"Q910000{i}1",
        "family": "cause",
        "stance": "asserts",
        "source_key": "wikidata.wbapi",
        "source_tier": "T3",
        "upstream_keys": [f"url:https://example.org/{i}"],
        "contests": [],
        "disputed_by": [],
        "content_sha256": "0" * 64,
        "statement_key": f"wikidata.wbapi:Q9100000{i}$x",
        "refs": [{"P854": [f"https://example.org/{i}"]}],
        "scope": {},
        "qualifiers": {},
        "relation": "has_cause",
    }
    return {**base, **kw}


def _dates(start: str, end: str | None = None, prec: int = 11) -> Any:
    vals = [DateValue("P580", start, start, prec)]
    if end:
        vals.append(DateValue("P582", end, end, prec))
    return event_dates(vals)


def test_grader_caps_hedged_and_wikidata_doi_claims_and_flags_scope() -> None:
    hedged = _claim(1, qualifiers={"P1480": ["Q18122778"]})
    doi = _claim(2, source_tier="T2", refs=[{"P356": ["10.1126/x"]}], upstream_keys=["doi:10.1126/x"])
    scoped = _claim(3, qualifiers={"P518": ["Q7569"]})
    by = {x.claim_row_id: x for x in compute([hedged, doi, scoped], {}, RULES)}
    assert by[1].grade.grade == "D" and "nature.sourcing_circumstances" in by[1].grade.rule_ids
    assert by[2].grade.grade == "C" and by[2].grade.deciding_rule == "grade.C.t2_wikidata_single"
    assert "tier.T2.wikidata_reference" in by[2].grade.rule_ids
    assert by[3].grade.grade == "C" and "scope.qualified" in by[3].grade.rule_ids


def test_coded_outcome_dated_by_its_cause_end_is_flagged_not_a() -> None:
    war = _dates("1950-06-25", "1953-07-27")
    outcome = _dates("1953-07-27")
    c = _claim(
        4,
        claim_key="ds:t.src:wars:4",
        cause_id="t:war:4",
        effect_id="t:war:4:outcome",
        relation="coded_outcome",
        source_key="t.src",
        source_tier="T1",
        statement_key=None,
        refs=[],
        upstream_keys=["dataset:t.src/wars"],
    )
    twin = {**c, "id": 5, "claim_key": "ds:u.src:wars:4", "source_key": "u.src", "upstream_keys": ["dataset:u.src/w"]}
    dates = {"t:war:4": war, "t:war:4:outcome": outcome}
    assert outcome_dated_by_cause_end(c, war, outcome, {"coded_outcome"}) is not None
    assert outcome_dated_by_cause_end({**c, "relation": "has_effect"}, war, outcome, {"coded_outcome"}) is None
    assert outcome_dated_by_cause_end(c, war, _dates("1953-08-01"), {"coded_outcome"}) is None
    out = compute([c, twin], dates, RULES)
    for x in out:  # consistent and corroborated, but the order is true by construction: not A
        assert x.temporal_check == "consistent" and x.temporal["reason"] == "effect_dated_by_cause_end"
        assert "temporal.effect_dated_by_cause_end" in x.grade.rule_ids and x.grade.grade == "B"


# ---------------------------------------------------------------------------------------------- retraction reasons
def test_dataset_retraction_reason_is_derived_from_the_archive() -> None:
    lt = {"raw_sha256": "a" * 64, "source_revision": "v1", "load_id": 7, "selector": {"row": 3}}
    r, d = dataset_retraction_reason(lt, raws={"a" * 64}, dataset_revision="v1", parameters={"connector": "x"})
    assert r == "reclassified_by_reva" and d["basis"] == "raw_unchanged" and d["connector"] == "x"
    r, d = dataset_retraction_reason(lt, raws={"b" * 64}, dataset_revision="v2", parameters={})
    assert r == "absent_from_delivery" and d["basis"] == "raw_changed_row_not_checked"
    assert (d["source_revision_from"], d["source_revision_to"]) == ("v1", "v2")
    r, _ = dataset_retraction_reason(
        lt, raws={"b" * 64}, dataset_revision="v2", parameters={}, source_row_present=lambda _x: False
    )
    assert r == "removed_at_source"
    r, d = dataset_retraction_reason(
        lt, raws={"b" * 64}, dataset_revision="v2", parameters={}, source_row_present=lambda _x: True
    )
    assert r == "reclassified_by_reva" and d["basis"] == "row_present_in_new_revision"


def test_migration_0018_reasons_and_annotation_table() -> None:
    p = ROOT / "migrations" / "versions" / "0018_retraction_reasons.py"
    spec = importlib.util.spec_from_file_location("m0018", p)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert (m.revision, m.down_revision) == ("0018_retraction_reasons", "0017_links_retention")
    assert {"reclassified_by_reva", "absent_from_delivery", "removed_at_source"} <= set(m.CLAIM_REASONS)
    assert {"reclassified_by_reva", "absent_from_delivery", "deprecated_at_source"} <= set(m.STATEMENT_REASONS)
    assert "kg.retraction_annotation" in m.SQL_TABLE
    assert "UPDATE" not in m.SQL_REASONS.upper()  # old rows are re-validated, never changed


# ---------------------------------------------------------------------------------------------- wording
def test_unloaded_labels_are_honest_never_bare_ids() -> None:
    r = _ref("Q7747", {}, {})
    assert r["label"] == "Q7747 (label not loaded)" and r["label_fr"] == "Q7747 (libellé non chargé)"
    assert r["label_loaded"] is False
    r = _ref("Q212", {"Q212": {"en": "Ukraine"}}, {})
    assert (r["label"], r["label_fr"], r["label_loaded"]) == ("Ukraine", None, True)
    assert unloaded_label("Q1", "fr") == "Q1 (libellé non chargé)"


def test_sourcing_circumstances_and_scope_are_worded() -> None:
    row = {"qualifiers": {"P1480": ["Q18122778"], "P518": [{"id": "Q7569"}], "P1011": ["Q1"]}, "scope": {}}
    a = attribution_of(row, {"Q7569": {"en": "child", "fr": "enfant"}})
    assert a is not None
    assert a["sourcing_circumstances"][0]["id"] == "Q18122778"
    assert "Wikidata qualifie : Q18122778 (libellé non chargé) (circonstances de la source, P1480)" in a["note_fr"]
    assert "Wikidata qualifies: Q18122778 (label not loaded) (sourcing circumstances, P1480)" in a["note_en"]
    assert "s'applique à la partie : enfant" in a["note_fr"] and "applies to part: child" in a["note_en"]
    assert "à l'exclusion de : Q1 (libellé non chargé)" in a["note_fr"]
    assert attribution_of({"qualifiers": {}, "scope": {}}, {}) is None


def test_licence_warning_names_restricted_sources() -> None:
    nc = {"licence": "CC BY-NC 4.0", "restricted": True, "non_commercial": True, "open_licence": False}
    claims = [
        {"source": {"source_key": "idmc.gidd", "rights": nc}},
        {"source": {"source_key": "ucdp.downloads", "rights": {"licence": "CC BY 4.0", "restricted": False}}},
        {"source": {"source_key": "x", "rights": None}},
    ]
    w = licence_warning(claims)
    assert w is not None and w["code"] == "licence_restricted"
    assert "idmc.gidd: CC BY-NC 4.0 (non-commercial, not an open licence)" in w["message"]
    assert "ucdp" not in w["message"]
    assert licence_warning(claims[1:]) is None
