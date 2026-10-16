"""Regression tests for the V3 review fixes in /v1/ask (docs/dev/review_v3.md: ASK-1..6, SEC-1).

The reproductions themselves live in tests/review_v3/test_review_v3_nlqa.py; these cover the other
branches of each fix (uniform "LCU" unit, ALL answers, time budget, faithful rephrases still accepted).
"""

from __future__ import annotations

from typing import Any, ClassVar

import nlqa_fakes as F
import pytest

from reva.nlqa.compose import Sentence, compose, period_label
from reva.nlqa.executor import MAX_ALL_CELLS, Executor, RankRefused, rank_cells
from reva.nlqa.plan import validate_plan
from reva.nlqa.ports import AtlasCell, Concept, estimate_status, is_projection, per_country_unit
from reva.nlqa.rephrase import check_faithful

CAT = F.catalog()
UNE = "labour.unemployment_rate"
UNE_SRC = ("ilo", "ilostat_une", "UNE_DEAP_SEX_AGE_RT|sex=SEX_T|age=AGE_YTHADULT_YGE15")
DENS = "population.density"


def _run(question: str, ops: list[dict[str, Any]], cells: list[AtlasCell], lang: str = "en", **kw: Any):
    plan = validate_plan(
        {"input_language": lang, "restatement": "", "operations": ops, "unsupported": []}, CAT, question
    )
    x = Executor(F.backends(atlas=F.FakeAtlas(cells)), CAT, **kw).run(plan)
    return x, compose(x, question, CAT)


def _une(iso3: str, year: int, value: str, flags: dict[str, Any] | None = None, unit: str = "%") -> AtlasCell:
    return F.cell(*UNE_SRC, iso3, year, value, unit, F.RAW_ILO, UNE, flags)


# --------------------------------------------------------------------------- helpers
@pytest.mark.parametrize(
    ("flags", "status"),
    [
        ({"estimate_kind": "projection"}, "projection"),
        ({"projection": True}, "projection"),
        ({"projection": "reference year after the series vintage"}, "projection"),
        ({"projection": False}, None),
        ({"projection": "false"}, None),
        ({"period_after_vintage": "true"}, "projection"),
        ({"estimate_kind": "forecast"}, "projection"),
        ({"estimate_kind": "estimated"}, "estimate"),
        ({"estimate_type": "ilo_modelled"}, "estimate"),
        ({"estimate_type": "national"}, None),
        ({}, None),
    ],
)
def test_estimate_status_follows_the_atlas_projection_semantics(flags: dict[str, Any], status: str | None) -> None:
    assert estimate_status(flags) == status
    assert is_projection(flags) == (status == "projection")


def test_per_country_units_are_recognised() -> None:
    for u in ("current LCU", "local currency per month", "national currency per US dollar", "LCU per international $"):
        assert per_country_unit(u), u
    for u in ("%", "EUR per month", "people per sq. km of land area", "current US$"):
        assert not per_country_unit(u), u
    assert not Concept("x", unit="local currency per month").comparable_across_countries
    assert Concept("x", unit="local currency per month", rankable=True).comparable_across_countries
    assert not Concept("x", unit="%", rankable=False).comparable_across_countries


# --------------------------------------------------------------------------- ASK-1
def test_rank_refuses_a_single_unit_string_that_names_a_currency_per_country() -> None:
    """WDI 'current LCU' is one unit string but a different currency in every country."""
    cells = [_une(iso, 2022, v, unit="current LCU") for iso, v in (("FRA", "10"), ("SEN", "2000"))]
    with pytest.raises(RankRefused) as e:
        rank_cells(cells, (".".join(UNE_SRC),), 2022, "desc", 5)
    assert e.value.reason == "unit_named_per_country"


def test_rank_refuses_mixed_units_and_the_answer_says_why() -> None:
    cells = [_une("FRA", 2022, "7.3"), _une("SEN", 2022, "3.0", unit="% of labour force")]
    x, c = _run("highest unemployment 2022", [{"id": "s1", "op": "atlas_rank", "concept": UNE, "year": 2022}], cells)
    assert x.results[0].status == "refused" and x.results[0].data["reason"] == "units_differ"
    text = c.text
    assert "No ranking" in text and "7.3" not in text and "does not convert" in text
    assert all(s.citations for s in c.sentences)


def test_rank_of_a_comparable_concept_keeps_one_unit_header() -> None:
    x, c = _run("densest 2022", [{"id": "s1", "op": "atlas_rank", "concept": DENS, "year": 2022, "limit": 2}], F.CELLS)
    assert x.results[0].status == "ok"
    assert "(people per sq. km of land area), 2022," in c.text


# --------------------------------------------------------------------------- ASK-2
def test_rank_with_only_projections_and_no_year_is_not_ranked() -> None:
    cells = [_une(iso, 2030, "5", {"estimate_kind": "projection"}) for iso in ("FRA", "SEN")]
    x, c = _run("highest unemployment", [{"id": "s1", "op": "atlas_rank", "concept": UNE}], cells)
    assert x.results[0].status == "refused" and x.results[0].data["reason"] == "only_projections"
    assert "2030" not in c.text and "projection" in c.text


def test_all_countries_answer_is_one_actual_year_and_capped() -> None:
    isos = [f"X{i:02d}" for i in range(MAX_ALL_CELLS + 20)]
    cells = [
        _une(iso, y, "4", {"estimate_kind": "projection"} if y > 2025 else None)
        for iso in isos
        for y in (2024, 2025, 2031)
    ]
    x, c = _run("unemployment everywhere", [{"id": "s1", "op": "atlas_value", "concept": UNE, "country": "ALL"}], cells)
    d = x.results[0].data
    assert d["year"] == 2025 and d["n"] == len(isos) and d["truncated"] is True
    assert len(d["cells"]) == MAX_ALL_CELLS and len(x.evidence.items) <= MAX_ALL_CELLS + 1
    assert "2031" not in c.text and "list truncated" in c.text and "projections excluded" in c.text


def test_all_countries_answer_for_a_projection_year_says_so() -> None:
    cells = [_une(iso, 2031, "4", {"estimate_kind": "projection"}) for iso in ("FRA", "SEN")]
    _, c = _run(
        "unemployment 2031", [{"id": "s1", "op": "atlas_value", "concept": UNE, "country": "ALL", "year": 2031}], cells
    )
    assert "2 of these values are projections" in c.text


def test_value_sentence_marks_an_estimate_in_french() -> None:
    cells = [_une("SEN", 2020, "3.1", {"estimate_type": "ilo_modelled"})]
    _, c = _run(
        "chômage du Sénégal en 2020",
        [{"id": "s1", "op": "atlas_value", "concept": UNE, "country": "SEN", "year": 2020}],
        cells,
        lang="fr",
    )
    assert "2020 (estimation de la source)" in c.text


def test_long_flags_never_cut_the_honesty_flags() -> None:
    flags = {"aaa": "x" * 400, "estimate_kind": "projection", "zzz": "y"}
    _, c = _run(
        "Unemployment Senegal 2030",
        [{"id": "s1", "op": "atlas_value", "concept": UNE, "country": "SEN", "year": 2030}],
        [_une("SEN", 2030, "3", flags)],
    )
    text = c.text
    assert "source flags: estimate_kind=projection, aaa=" in text and "…" in text


# --------------------------------------------------------------------------- ASK-4
EV = {
    "E1": {"id": "E1", "kind": "atlas_cell", "iso3": "FRA", "value": "1747.2", "unit": "EUR per month"},
    "E2": {"id": "E2", "kind": "atlas_cell", "iso3": "SEN", "value": "58900", "unit": "XOF per month"},
}
TEMPLATE = Sentence(
    "S1",
    "Monthly minimum wage, 2023: France: 1,747.2 EUR per month; Senegal: 58,900 XOF per month "
    "(source: ilo/ilostat_mw).",
    ["E1", "E2"],
    "fact",
    "s1",
)


@pytest.mark.parametrize(
    "text",
    [
        "In 2023 the monthly minimum wage was 1,747.2 EUR per month in France and 58,900 XOF per month in "
        "Senegal (source: ilo/ilostat_mw).",
        "In 2023 Senegal's minimum wage was 58,900 XOF per month, France's 1,747.2 EUR per month "
        "(source: ilo/ilostat_mw).",
    ],
)
def test_faithful_rephrase_keeping_each_value_with_its_country_is_accepted(text: str) -> None:
    report, out = check_faithful([TEMPLATE], {"sentences": [{"id": "S1", "text": text}]}, EV)
    assert report.ok, report.problems
    assert out["S1"] == text


def test_rephrase_moving_a_currency_code_is_rejected() -> None:
    text = (
        "In 2023 the monthly minimum wage was 1,747.2 XOF per month in France and 58,900 EUR per month in Senegal "
        "(source: ilo/ilostat_mw)."
    )
    report, _ = check_faithful([TEMPLATE], {"sentences": [{"id": "S1", "text": text}]}, EV)
    assert not report.ok and any("value_moved_between_entities" in p for x in report.problems for p in x["problems"])


def test_rephrase_dropping_an_estimate_label_is_rejected() -> None:
    s = Sentence("S1", "Unemployment rate — Senegal, 2020 (source estimate): 3.1 % (source: ilo/x).", ["E1"], "fact")
    report, _ = check_faithful(
        [s], {"sentences": [{"id": "S1", "text": "Unemployment rate — Senegal, 2020: 3.1 % (source: ilo/x)."}]}, {}
    )
    assert not report.ok and "marker_dropped:estimate" in report.problems[0]["problems"]


# --------------------------------------------------------------------------- ASK-5
def test_bce_period_label_and_year() -> None:
    c = _une("ITA", 2000, "1")
    bce = AtlasCell(**{**c.__dict__, "period_start": "-0043-01-01", "period_end": "-0042-01-01"})
    assert bce.year == -43 and period_label(bce) == "-43"
    assert period_label(c) == "2000"


# --------------------------------------------------------------------------- SEC-1
def test_time_budget_stops_further_operations_with_a_typed_error() -> None:
    x, c = _run(
        "density 2022 and 2021",
        [
            {"id": "s1", "op": "atlas_rank", "concept": DENS, "year": 2022},
            {"id": "s2", "op": "atlas_rank", "concept": DENS, "year": 2021},
        ],
        F.CELLS,
        time_budget_s=-1.0,
    )
    assert [r.status for r in x.results] == ["error", "error"]
    assert {r.message for r in x.results} == {"time_budget_exceeded"} and "time_budget_exceeded" in x.warnings
    assert "time_budget_exceeded" in c.text


def test_latest_value_of_one_country_is_the_latest_actual_not_a_projection() -> None:
    cells = [_une("USA", 2025, "4.28"), _une("USA", 2031, "3.9", {"estimate_kind": "projection"})]
    x, c = _run("US unemployment", [{"id": "s1", "op": "atlas_value", "concept": UNE, "country": "USA"}], cells)
    assert [cell.year for cell, _ in x.results[0].data["groups"][0][1]] == [2025]
    assert "2031" not in c.text
    # a series with projections only still answers, labelled
    x, c = _run("US unemployment", [{"id": "s1", "op": "atlas_value", "concept": UNE, "country": "USA"}], cells[1:])
    assert "2031 (source projection)" in c.text


def test_atlas_adapter_latest_prefers_an_actual_cell() -> None:
    from reva.nlqa.adapters import AtlasQueryAdapter

    def cell(year: int, flags: dict[str, Any]) -> dict[str, Any]:
        return {
            "source": "imf", "dataset": "weo", "indicator": "LUR", "series": "imf.weo.LUR", "year": year,
            "period_start": f"{year}-01-01", "period_end": f"{year + 1}-01-01", "value": "4", "unit": "%",
            "flags": flags, "provenance": {},
        }  # fmt: skip

    class Q:
        snapshot = None

        def series(self, catalog: Any, concept_id: str, iso3: str) -> dict[str, Any]:
            return {"per_source": [{"cells": [cell(2025, {}), cell(2031, {"estimate_kind": "projection"})]}]}

    class Cat:
        concepts: ClassVar[dict[str, Any]] = {}

    (c,) = AtlasQueryAdapter(Q(), Cat()).values("x", ["USA"], None).cells
    assert c.year == 2025
