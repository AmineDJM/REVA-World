"""Grading rules (registry/causal/grading.yaml), corroboration dedupe, the pure grader, claim rows from KG
statements and dataset rows, cycle detection, and the real Wikidata fixture (offline)."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from typing import Any

import pytest

from reva.causal import grading as gr
from reva.causal.fixture import FIXTURE_FILE, load_fixture
from reva.causal.grader import compute
from reva.causal.graph import _cycle_in, _sccs
from reva.causal.model import DatasetClaim, _dataset_row, _Reject, claim_from_statement
from reva.causal.temporal import EventDates, check, event_dates, window_of_time
from reva.causal.vocab import CAUSAL_PIDS, normalise_wikidata, vocabulary

RULES = gr.load_rules()


# ---------------------------------------------------------------------------------------------- rules
def test_rule_table_is_versioned_and_complete() -> None:
    assert RULES.version == "1" and len(RULES.sha256) == 64 and RULES.corroborated_min == 2
    for tier, tc, corr, contested in itertools.product(gr.TIERS, gr.TEMPORAL, (1, 2, 3), (False, True)):
        g = gr.decide(gr.GradeInputs(tier, tc, corr, contested), RULES)
        assert g.grade in gr.GRADES and len(g.rule_ids) == 5
        assert all(r["en"] and r["fr"] for r in g.reasons)


@pytest.mark.parametrize(
    ("tier", "temporal", "corr", "contested", "grade", "rule"),
    [
        ("T1", "consistent", 2, False, "A", "grade.A.expert_consistent_corroborated"),
        ("T2", "consistent", 3, False, "A", "grade.A.expert_consistent_corroborated"),
        ("T1", "overlapping", 2, False, "B", "grade.B.expert"),  # not consistent -> not A
        ("T1", "undetermined", 5, False, "B", "grade.B.expert"),
        ("T1", "consistent", 1, False, "B", "grade.B.expert"),  # not corroborated -> not A
        ("T3", "consistent", 2, False, "B", "grade.B.t3_corroborated"),
        ("T3", "undetermined", 1, False, "C", "grade.C.t3_single"),
        ("T4", "consistent", 3, False, "D", "grade.D.t4"),  # an unreferenced statement stays D
        ("T1", "inconsistent", 9, False, "E", "grade.E.temporal_inconsistent"),
        ("T3", "consistent", 1, True, "E", "grade.E.contested_uncorroborated"),
        ("T3", "consistent", 2, True, "B", "grade.B.t3_corroborated"),  # contested but corroborated
        ("T4", "overlapping", 1, True, "E", "grade.E.contested_uncorroborated"),
    ],
)
def test_grade_decisions(tier: str, temporal: str, corr: int, contested: bool, grade: str, rule: str) -> None:
    g = gr.decide(gr.GradeInputs(tier, temporal, corr, contested), RULES)
    assert (g.grade, g.deciding_rule) == (grade, rule)
    assert g.rule_ids[0] == f"tier.{tier}" and g.rule_ids[1] == f"temporal.{temporal}"
    assert g.rule_ids[3] == ("contest.contested" if contested else "contest.none") and g.rule_ids[-1] == rule


def test_invalid_rule_tables_are_refused() -> None:
    base = {"version": "x", "rules": [], "facts": {k: {"en": k, "fr": k} for k in gr._fact_ids()}}
    with pytest.raises(ValueError, match="unknown condition"):
        gr.parse_rules(
            {**base, "rules": [{"id": "r", "grade": "A", "when": {"vibes": 1}, "reason": {"en": "", "fr": ""}}]}, "0"
        )
    with pytest.raises(ValueError, match="lacks the fact rule"):
        gr.parse_rules({**base, "facts": {}}, "0")
    empty = gr.parse_rules(base, "0")
    with pytest.raises(ValueError, match="no grading rule decides"):
        gr.decide(gr.GradeInputs("T1", "consistent", 1, False), empty)


def test_weakest_and_best() -> None:
    assert gr.weakest(["B", "D", "C"]) == "D" and gr.weakest(["A", "E"]) == "E"
    assert gr.best(["D", "B", "C"]) == "B" and gr.at_least("B", "C") and not gr.at_least("D", "C")


# ---------------------------------------------------------------------------------------------- upstream keys
def test_reference_keys_and_url_normalisation() -> None:
    a = gr.reference_keys({"P854": ["http://www.Example.org/Page/#x"], "P813": [{"time": "+2025-01-01"}]})
    b = gr.reference_keys({"P854": ["https://example.org/Page"], "P813": [{"time": "+2026-01-01"}]})
    assert a == b == ["url:https://example.org/Page"]
    assert gr.reference_keys({"P248": ["Q36578"], "P143": ["Q328"]}) == ["item:Q36578", "imported_from:Q328"]
    # a reference with no identifying property hashes its content, retrieval date (P813) excluded
    r1 = gr.reference_keys({"P1683": ["quote"], "P813": [{"time": "+2025-01-01"}]})
    r2 = gr.reference_keys({"P1683": ["quote"], "P813": [{"time": "+2026-06-06"}]})
    assert r1 == r2 and r1[0].startswith("refhash:")
    assert gr.reference_keys({"P356": ["10.1000/ABC"]}) == ["doi:10.1000/abc"]
    assert gr.citation_of([{"P212": ["978-0-00-000000-2"]}]) == {"isbn": ["978-0-00-000000-2"]}
    assert gr.citation_of([{"P854": ["https://x"]}]) is None


def test_upstream_keys_per_source_kind() -> None:
    assert gr.upstream_keys(source_key="wikidata.causal_fixture") == ["community:wikidata"]
    assert gr.upstream_keys(source_key="icb.data", dataset="icb2") == ["dataset:icb.data/icb2"]
    assert gr.upstream_keys(source_key="x.data", dataset="d", citation={"doi": ["10.1/Z"]}) == [
        "dataset:x.data/d",
        "doi:10.1/z",
    ]


def _cr(key: str, *up: str, src: str = "wikidata") -> gr.ClaimRef:
    return gr.ClaimRef(key, src, tuple(up))


def test_corroboration_dedupes_shared_upstreams() -> None:
    # two Wikidata statements citing the same reference count once
    c = gr.corroboration([_cr("a", "url:https://e.org/x"), _cr("b", "url:https://e.org/x")])
    assert c.count == 1 and c.group_claims == [["a", "b"]]
    # transitively shared upstreams merge (a~b via u1, b~c via u2)
    c = gr.corroboration([_cr("a", "u1"), _cr("b", "u1", "u2"), _cr("c", "u2"), _cr("d", "u3")])
    assert c.count == 2
    # independent references: corroborated
    assert gr.corroboration([_cr("a", "item:Q1"), _cr("b", "item:Q2")]).count == 2
    # unreferenced restatements are one community group...
    assert gr.corroboration([_cr("a", "community:wikidata"), _cr("b", "community:wikidata")]).count == 1
    # ...which does not count beside a referenced group of the same source root
    c = gr.corroboration([_cr("a", "community:wikidata"), _cr("b", "item:Q1")])
    assert c.count == 1 and c.dropped_community == ["a"]
    # a dataset and a referenced Wikidata statement are independent
    c = gr.corroboration([_cr("a", "item:Q1"), _cr("ds", "dataset:icb.data/icb2", src="icb.data")])
    assert c.count == 2
    # two rows of one dataset are one source
    assert gr.corroboration([_cr("r1", "dataset:x/d", src="x"), _cr("r2", "dataset:x/d", src="x")]).count == 1


# ---------------------------------------------------------------------------------------------- pure grader
def _claim(i: int, cause: str, effect: str, *, tier: str = "T3", up: tuple[str, ...] = ("item:Q1",), **kw: Any) -> dict:
    return {
        "id": i,
        "claim_key": f"k{i}",
        "cause_id": cause,
        "effect_id": effect,
        "family": kw.get("family", "cause"),
        "stance": kw.get("stance", "asserts"),
        "source_key": kw.get("source_key", "wikidata"),
        "source_tier": tier,
        "upstream_keys": list(up),
        "contests": kw.get("contests", []),
        "disputed_by": kw.get("disputed_by", []),
        "content_sha256": f"c{i}",
    }


def _dates(**years: int) -> dict[str, EventDates]:
    out = {}
    for q, y in years.items():
        out[q] = EventDates(window_of_time(f"{y:+05d}", 9, "P580"), None)
    return out


def test_compute_grades_with_corroboration_contest_and_time() -> None:
    dates = _dates(x=1914, y=1918, z=1700)
    claims = [
        _claim(1, "x", "y", up=("item:Q1",)),
        _claim(2, "x", "y", up=("item:Q2",)),  # independent -> both corroborated (B)
        _claim(3, "x", "z"),  # cause after effect -> E
        _claim(4, "y", "x", tier="T4", up=("community:wikidata",), stance="denies"),
        _claim(5, "q", "r", disputed_by=[{"property": "P1310", "by": "Q5"}]),  # P1310 -> contested, single -> E
        _claim(6, "u", "t", tier="T1", up=("dataset:a/b",), contests=["k7"], source_key="a"),
        _claim(7, "s", "t", tier="T3"),  # explicitly contested by k6 -> E
        _claim(8, "x", "y", family="prevention", up=("item:Q9",)),  # other family: not corroborating 1/2
    ]
    out = {c.claim_key: c for c in compute(claims, dates, RULES)}
    assert (out["k1"].grade.grade, out["k1"].corroboration.count, out["k1"].corroborating) == ("B", 2, ["k2"])
    assert out["k3"].grade.grade == "E" and out["k3"].temporal_check == "inconsistent"
    assert out["k4"].temporal_check == "inconsistent"  # y (1918) -> x (1914), a denial is checked too
    assert out["k5"].contested_by == ["qualifier:P1310:Q5"] and out["k5"].grade.grade == "E"
    assert out["k7"].contested_by == ["k6"] and out["k7"].grade.grade == "E"
    assert out["k6"].grade.grade == "B" and out["k6"].temporal_check == "undetermined"
    assert out["k8"].corroboration.count == 1 and out["k8"].grade.grade == "C"
    # deterministic: same inputs -> same inputs sha; a changed date -> a new sha
    again = {c.claim_key: c.inputs_sha256 for c in compute(claims, dates, RULES)}
    assert again == {k: c.inputs_sha256 for k, c in out.items()}
    moved = {c.claim_key: c.inputs_sha256 for c in compute(claims, _dates(x=1913, y=1918, z=1700), RULES)}
    assert moved["k1"] != out["k1"].inputs_sha256 and moved["k5"] == out["k5"].inputs_sha256


def test_denial_contests_assertion_symmetrically() -> None:
    claims = [_claim(1, "a", "b"), _claim(2, "a", "b", stance="denies", up=("item:Q7",))]
    out = {c.claim_key: c for c in compute(claims, {}, RULES)}
    assert out["k1"].contested_by == ["k2"] and out["k2"].contested_by == ["k1"]
    assert out["k1"].grade.grade == out["k2"].grade.grade == "E"


# ---------------------------------------------------------------------------------------------- rows
def _st(pid: str, refs: list | None = None, quals: dict | None = None, src: str = "wikidata") -> dict:
    return {
        "id": 10,
        "statement_key": f"{src}:S$1",
        "statement_id": "S$1",
        "origin": "test_fixture",
        "subject_qid": "Q8698",
        "property_pid": pid,
        "object_qid": "Q201684",
        "qualifiers": quals or {},
        "rank": "normal",
        "refs": refs or [],
        "source_key": src,
        "source_revision": "123",
        "raw_sha256": "a" * 64,
        "retrieved_at": datetime(2026, 10, 16, tzinfo=UTC),
        "start_time": None,
        "start_precision": None,
        "end_time": None,
        "end_precision": None,
        "pit_time": "+1930-00-00T00:00:00Z",
        "pit_precision": 9,
    }


def test_claim_from_statement() -> None:
    c = claim_from_statement(_st("P828"))
    assert (c["cause_id"], c["effect_id"], c["relation"], c["asserted_on"]) == (
        "Q201684",
        "Q8698",
        "has_cause",
        "effect",
    )
    assert c["source_tier"] == "T4" and c["claim_key"] == "kg:wikidata:S$1" and c["statement_row_id"] == 10
    assert c["scope"] == {"period": {"pit": {"time": "+1930-00-00T00:00:00Z", "precision": 9}}}
    c = claim_from_statement(_st("P1542", refs=[{"P248": ["Q5"]}], quals={"P1310": ["Q9"], "P276": ["Q30"]}))
    assert (c["cause_id"], c["effect_id"], c["source_tier"]) == ("Q8698", "Q201684", "T3")
    assert c["disputed_by"] == [{"property": "P1310", "by": "Q9"}] and c["scope"]["place"] == ["Q30"]
    assert claim_from_statement(_st("P828", refs=[{"P356": ["10.1/x"]}]))["source_tier"] == "T2"
    # the content hash ignores retrieval time but not the statement row (a new KG revision is a new claim version)
    a = claim_from_statement(_st("P828"))
    b = claim_from_statement({**_st("P828"), "retrieved_at": datetime(2027, 1, 1, tzinfo=UTC)})
    c2 = claim_from_statement({**_st("P828"), "id": 11})
    assert a["content_sha256"] == b["content_sha256"] != c2["content_sha256"]
    with pytest.raises(ValueError):
        claim_from_statement(_st("P710"))
    with pytest.raises(ValueError):
        claim_from_statement(_st("P828", src="ucdp.downloads"))  # datasets come through load_dataset_claims


def test_dataset_rows_validate() -> None:
    from reva.causal.model import Codebook

    cb = Codebook("cb", "1", None, {"trigger": "verbatim"})
    kw = dict(
        source_key="icb.data",
        dataset="icb2",
        dataset_revision="16",
        codebook=cb,
        raws={"b" * 64},
        retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
        vocab=vocabulary(),
    )
    ok = DatasetClaim("icb:c:1:t", "icb:c:1", "coded_trigger", {"row": 1}, codebook_term="trigger")
    row = _dataset_row(0, ok, **kw)  # type: ignore[arg-type]
    assert row["source_tier"] == "T1" and row["codebook"]["definition"] == "verbatim" and row["raw_sha256"] == "b" * 64
    assert row["claim_key"].startswith("ds:icb.data:icb2:") and row["upstream_keys"] == ["dataset:icb.data/icb2"]
    inh = _dataset_row(
        0, DatasetClaim("ab:x", "ab:y", "has_effect", {"r": 1}, codebook_term="trigger", polarity="inhibits"), **kw
    )  # type: ignore[arg-type]
    assert inh["family"] == "prevention"
    bad = [
        (DatasetClaim("ab:x", "ab:y", "coded_trigger", {}, codebook_term="trigger"), "selector_required"),
        (DatasetClaim("ab:x", "ab:y", "coded_trigger", {"r": 1}), "codebook_term_not_defined"),
        (
            DatasetClaim("ab:x", "ab:y", "coded_trigger", {"r": 1}, codebook_term="trigger", tier="T3"),
            "dataset_tier_must_be_t1_or_t2",
        ),
        (
            DatasetClaim("ab:x", "ab:y", "coded_trigger", {"r": 1}, codebook_term="trigger", raw_sha256="c" * 64),
            "raw_sha256_not_in_batch",
        ),
        (
            DatasetClaim("ab:x", "ab:y", "coded_trigger", {"r": 1}, codebook_term="trigger", polarity="maybe"),
            "invalid_polarity",
        ),
        (
            DatasetClaim("ab:x", "ab:y", "coded_trigger", {"r": 1}, tier="T2", citation={"url": "x"}),
            "citation_needs_doi_or_isbn",
        ),
    ]
    for claim, reason in bad:
        with pytest.raises(_Reject) as e:
            _dataset_row(0, claim, **kw)  # type: ignore[arg-type]
        assert e.value.reason == reason


# ---------------------------------------------------------------------------------------------- cycles
def test_scc_cycle_detection() -> None:
    import time

    adj = {"a": ["b"], "b": ["c"], "c": ["a", "d"], "d": [], "e": ["f"], "f": ["e"], "g": ["g"]}
    comps, timed_out = _sccs(adj, time.perf_counter() + 5)
    assert not timed_out and sorted(comps) == [["a", "b", "c"], ["e", "f"]]  # self-loops are reported apart
    assert _cycle_in(["a", "b", "c"], adj) == ["a", "b", "c", "a"]
    # a long chain is not a cycle; the iteration is not recursive (no recursion limit)
    long = {str(i): [str(i + 1)] for i in range(5000)}
    assert _sccs(long, time.perf_counter() + 5) == ([], False)


# ---------------------------------------------------------------------------------------------- real fixture
@pytest.mark.skipif(not FIXTURE_FILE.exists(), reason="causal fixture not captured")
def test_real_fixture_offline() -> None:
    doc = load_fixture()
    assert doc["meta"]["community_edited"] is True and set(doc["meta"]["seeds"]) == {
        "Q8698",
        "Q201684",
        "Q178275",
        "Q361",
    }
    ents = doc["entities"]
    assert ents["Q8698"]["labels"]["en"] == "Great Depression"
    assert ents["Q201684"]["labels"]["en"] == "Wall Street crash of 1929"
    assert ents["Q178275"]["labels"]["en"].startswith("1918")
    causal = [s for s in doc["statements"] if s["property_pid"] in CAUSAL_PIDS]
    pairs = {
        (n.cause_id, n.effect_id, s["property_pid"])
        for s in causal
        for n in [normalise_wikidata(s["subject_qid"], s["property_pid"], s["object_qid"])]
    }
    assert ("Q201684", "Q8698", "P828") in pairs and ("Q201684", "Q8698", "P1542") in pairs
    assert ("Q361", "Q178275", "P828") in pairs
    assert all(s["statement_id"] and s["source_revision"] and s["raw_sha256"] for s in causal)

    def start(q: str) -> Any:
        vals = [s for s in doc["statements"] if s["subject_qid"] == q and s["property_pid"] in ("P580", "P585", "P571")]
        from reva.causal.temporal import DateValue
        from reva.kg.model import KgTime, parse_time

        dv = []
        for s in vals:
            lo, hi = parse_time(
                KgTime(s["value"]["time"], s["value"]["precision"], s["value"].get("calendar", "gregorian"))
            )
            dv.append(DateValue(s["property_pid"], lo, hi, s["value"]["precision"], s["value"]["time"], s["rank"]))
        return event_dates(dv).start

    assert check(start("Q201684"), start("Q8698")) == "consistent"
    assert check(start("Q361"), start("Q178275")) == "consistent"
