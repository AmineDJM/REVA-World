"""POST /v1/ask pipeline (reva.nlqa): defects found by the V3 review (docs/dev/review_v3.md, ASK-*).

Uses the NLQA in-memory fakes (tests/fixtures/nlqa/nlqa_fakes.py) with cells shaped like the real ILOSTAT /
IMF WEO atlas cells (unit per cell, ``estimate_kind`` / ``projection`` flags as the connectors write them).
"""

from __future__ import annotations

import json
from typing import Any

import nlqa_fakes as F
import pytest

from reva.nlqa.compose import Sentence, compose
from reva.nlqa.executor import Executor
from reva.nlqa.plan import validate_plan
from reva.nlqa.rephrase import check_faithful

CAT = F.catalog()
MW = "labour.minimum_wage_monthly"  # fixture concept: ilo.ilostat_mw.EAR_INEE_NOC_NB|cur=LCU
UNE = "labour.unemployment_rate"  # fixture concept: ilo une first, wb.wdi second
UNE_SRC = ("ilo", "ilostat_une", "UNE_DEAP_SEX_AGE_RT|sex=SEX_T|age=AGE_YTHADULT_YGE15")


def _run(question: str, ops: list[dict[str, Any]], cells: list[Any], lang: str = "en"):
    plan = validate_plan(
        {"input_language": lang, "restatement": "", "operations": ops, "unsupported": []}, CAT, question
    )
    x = Executor(F.backends(atlas=F.FakeAtlas(cells)), CAT).run(plan)
    return x, compose(x, question, CAT)


def _facts(c: Any) -> list[str]:
    return [s.text for s in c.sentences if s.kind == "fact"]


@pytest.mark.xfail(
    strict=True,
    reason="ASK-1: atlas_rank ranks one series' cells across countries even when every cell has a different "
    "currency (ILOSTAT CUR_TYPE_LCU names the currency per cell) and labels the whole ranking with the FIRST "
    "row's unit (compose.op_atlas_rank: unit = rows[0][0].unit)",
)
def test_rank_never_compares_or_mislabels_different_units() -> None:
    cells = [
        F.cell("ilo", "ilostat_mw", "EAR_INEE_NOC_NB|cur=LCU", "KOR", 2019, "1745150", "KRW per month", F.RAW_ILO, MW),
        F.cell("ilo", "ilostat_mw", "EAR_INEE_NOC_NB|cur=LCU", "SEN", 2019, "58900", "XOF per month", F.RAW_ILO, MW),
        F.cell("ilo", "ilostat_mw", "EAR_INEE_NOC_NB|cur=LCU", "FRA", 2019, "1521.22", "EUR per month", F.RAW_ILO, MW),
    ]
    _, c = _run(
        "Which countries have the highest minimum wage in 2019?",
        [{"id": "s1", "op": "atlas_rank", "concept": MW, "year": 2019, "order": "desc", "limit": 3}],
        cells,
    )
    text = " ".join(_facts(c))
    # Either the ranking is refused (units differ) or every value carries its own unit; never
    # "(KRW per month) ... 1. South Korea: 1,745,150; 2. Senegal: 58,900; 3. France: 1,521.22".
    assert "(KRW per month)" not in text, text


def _weo_une(iso3: str, year: int, value: str, projection: bool) -> Any:
    flags = {"vintage": "2026-10", "latest_actual": "2025", "display_scale": "0"}
    if projection:
        flags["estimate_kind"] = "projection"
    return F.cell(*UNE_SRC, iso3, year, value, "% of labour force", F.RAW_WDI, UNE, flags)


@pytest.mark.xfail(
    strict=True,
    reason="ASK-2: atlas_series and atlas_rank sentences never render cell flags, so IMF WEO / ILO modelled "
    "projections are stated as plain facts (compose.op_atlas_series / op_atlas_rank)",
)
def test_series_sentence_marks_projections() -> None:
    cells = [_weo_une("USA", y, v, y > 2025) for y, v in ((2024, "4.025"), (2025, "4.279167"), (2030, "3.896672"))]
    _, c = _run(
        "US unemployment rate 2024-2030",
        [
            {"id": "s1", "op": "atlas_series", "concept": UNE, "country": "USA", "from_year": 2024, "to_year": 2030},
        ],
        cells,
    )
    text = " ".join(_facts(c))
    assert "2030" in text and "projection" in text.lower(), text


@pytest.mark.xfail(
    strict=True,
    reason="ASK-2: with no year, atlas_rank picks the most recent well-covered year — a WEO projection year when "
    "the source projects every country — and the ranking sentence does not say it is a projection",
)
def test_rank_latest_year_is_not_silently_a_projection() -> None:
    cells = [
        _weo_une(iso, y, v, y > 2025)
        for iso, v in (("USA", "3.9"), ("FRA", "7.1"), ("SEN", "3.0"))
        for y in (2024, 2025, 2031)
    ]
    _, c = _run(
        "Which countries have the highest unemployment rate?",
        [{"id": "s1", "op": "atlas_rank", "concept": UNE, "order": "desc", "limit": 3}],
        cells,
    )
    text = " ".join(_facts(c))
    assert "2031" not in text or "projection" in text.lower(), text


@pytest.mark.xfail(
    strict=True,
    reason="ASK-3: compose._flags truncates the rendered flags to 160 characters after sorting keys; an ILOSTAT "
    "modelled-estimate cell's 'projection' flag sorts after its long 'notes' and is cut from the sentence",
)
def test_value_sentence_keeps_the_projection_flag() -> None:
    flags = {
        "estimate_type": "ilo_modelled",
        "ilo_source": "BA:1234",
        "ilo_source_label": "ILO - Modelled Estimates",
        "notes": {"T2:84": "Age coverage - 15 years and over", "R1:3513": "Population: Labour force"},
        "projection": "reference year after the series vintage (Nov. 2025): an ILO projection, not an observation",
    }
    cells = [F.cell(*UNE_SRC, "SEN", 2026, "3.1", "%", F.RAW_ILO, UNE, flags)]
    _, c = _run(
        "Unemployment rate of Senegal in 2026",
        [{"id": "s1", "op": "atlas_value", "concept": UNE, "country": "SEN", "year": 2026}],
        cells,
    )
    text = " ".join(_facts(c))
    assert "projection" in text.lower(), text


EV = {
    "E1": {"id": "E1", "kind": "atlas_cell", "iso3": "FRA", "value": "1747.2", "unit": "EUR per month"},
    "E2": {"id": "E2", "kind": "atlas_cell", "iso3": "SEN", "value": "58900", "unit": "XOF per month"},
}


@pytest.mark.xfail(
    strict=True,
    reason="ASK-4: the rephrase faithfulness check is set-based (numbers/names present somewhere in the sentence "
    "or its evidence), so an LLM rephrase that swaps two countries' values is accepted as faithful",
)
def test_rephrase_that_swaps_values_between_countries_is_rejected() -> None:
    s = Sentence(
        "S1",
        "Monthly minimum wage, 2023: France: 1,747.2 EUR per month; Senegal: 58,900 XOF per month "
        "(source: ilo/ilostat_mw).",
        ["E1", "E2"],
        "fact",
        "s1",
    )
    swapped = (
        "In 2023 the monthly minimum wage was 58,900 EUR per month in France and 1,747.2 XOF per month in "
        "Senegal (source: ilo/ilostat_mw)."
    )
    report, _ = check_faithful([s], {"sentences": [{"id": "S1", "text": swapped}]}, EV)
    assert not report.ok, json.dumps(report.problems)


@pytest.mark.xfail(
    strict=True,
    reason="ASK-4: 'projection'/'estimate' are not KEEP_MARKERS, so a rephrase may drop the projection label",
)
def test_rephrase_that_drops_the_projection_label_is_rejected() -> None:
    s = Sentence(
        "S1",
        "Unemployment rate — United States, 2030: 3.9 % (source: imf/weo, indicator LUR; source flags: "
        "estimate_kind=projection).",
        ["E1"],
        "fact",
        "s1",
    )
    dropped = "Unemployment rate — United States, 2030: 3.9 % (source: imf/weo, indicator LUR)."
    ev = {"E1": {"id": "E1", "value": "3.9", "flags": {"estimate_kind": "projection"}}}
    report, _ = check_faithful([s], {"sentences": [{"id": "S1", "text": dropped}]}, ev)
    assert not report.ok
