"""Review V3b — headline (cross-source best-available) layer: reva.atlas.query.AtlasQuery.headline_*.

HL-1: a per-cell fallback WPP -> WDI stitches two different territorial/statistical definitions into one series
      without saying so (France: metropolitan WPP 66.4 M in 2023, WDI incl. overseas departments 68.6 M in 2024;
      Moldova -21.7 % in one year).
HL-2: the headline concepts exclude UN WPP projection years, but the WDI values that answer 2024-2025 are, for
      most countries, those very WPP 2024 medium-variant projections (India 2025: identical to the person) and
      are served as reported actuals with empty flags.
"""

from __future__ import annotations

import json
from typing import Any

from v3b_helpers import SynthAtlas, rows

from reva.atlas.source_order import load_headlines

WPP = "un_wpp.wpp2024_demo.TPopulation1July"
WPP_PROJ = "un_wpp.wpp2024_demo.TPopulation1July|proj=medium"
WDI = "wb.wdi_social.SP.POP.TOTL"


def _mentions(cell: dict[str, Any], *words: str) -> bool:
    """True when the answer itself (top-level keys / flags, not justification prose) states one of ``words``."""
    keys = " ".join([*cell.keys(), *(cell.get("flags") or {}).keys()]).lower()
    vals = json.dumps(cell.get("flags") or {}).lower()
    return any(w in keys or w in vals for w in words)


def _status(cell_or_row: dict[str, Any], series: str) -> str:
    (c,) = [x for x in cell_or_row["consulted"] if x["series"] == series]
    return str(c["status"])


# Fixed (agent REVFIX2): source_order cell_rules / not_equivalent_for + query._mark_source_breaks.
def test_headline_series_marks_a_switch_of_answering_source(synth_atlas: SynthAtlas) -> None:
    # real values (dev snapshot b11f1c51; WB API 2026-10-16): WPP FRA 2022-2023 in thousands, WDI FRA 2022-2024
    synth_atlas.cell(WPP, "FRA", 2022, "66277.409", unit="thousands", flags={"projection": False})
    synth_atlas.cell(WPP, "FRA", 2023, "66438.822", unit="thousands", flags={"projection": False})
    synth_atlas.cell(WDI, "FRA", 2022, "68170228", unit="persons")
    synth_atlas.cell(WDI, "FRA", 2023, "68372286", unit="persons")
    synth_atlas.cell(WDI, "FRA", 2024, "68551653", unit="persons")
    # USA: WPP 2021-2022 only, WDI 2021-2024 (a switch inside the WPP estimate horizon, then projections)
    synth_atlas.cell(WPP, "USA", 2021, "336997.624", unit="thousands", flags={"projection": False})
    synth_atlas.cell(WPP, "USA", 2022, "341534.046", unit="thousands", flags={"projection": False})
    synth_atlas.cell(WDI, "USA", 2021, "332099760", unit="persons")
    synth_atlas.cell(WDI, "USA", 2022, "334017321", unit="persons")
    synth_atlas.cell(WDI, "USA", 2023, "336806231", unit="persons")
    synth_atlas.cell(WDI, "USA", 2024, "340110988", unit="persons")
    q = synth_atlas.query()
    try:
        reg = load_headlines()
        fra = q.headline_series(reg, "reva.population.total", "FRA", projections="include")
        usa = q.headline_series(reg, "reva.population.total", "USA")
        usa_p = q.headline_series(reg, "reva.population.total", "USA", projections="include")
    finally:
        q.close()
    # France: WDI France includes the overseas departments -> never spliced onto WPP metropolitan France
    by_year = {c["year"]: c for c in fra["best"]}
    assert by_year[2023]["answered_by"]["series"] == WPP and by_year[2023]["value"] == "66438822"
    assert 2024 not in by_year and fra["source_breaks"] == []
    assert {c["answered_by"]["series"] for c in fra["best"]} == {WPP}
    assert _status(by_year[2023], WDI) == "not_equivalent_for_country"
    # USA: the switch WPP -> WDI in 2023 is stated on the answer, with both values of the overlap year
    u = {c["year"]: c for c in usa["best"]}
    assert u[2022]["answered_by"]["series"] == WPP and u[2023]["answered_by"]["series"] == WDI
    assert 2024 not in u  # after the WPP estimate horizon: the WDI value is a WPP projection (excluded)
    assert _mentions(u[2023], "break", "switch", "discontinu")
    brk = u[2023]["source_break"]
    assert (brk["from"], brk["to"], brk["from_year"], brk["year"]) == (WPP, WDI, 2022, 2023)
    assert brk["overlap"] == {"year": 2022, "from_value": "341534046", "to_value": "334017321", "ratio": "0.977991"}
    assert usa["source_breaks"] == [brk] and "source_break" not in u[2022]
    # with projections: 2024 is answered, flagged as the WPP projection, after the break
    up = {c["year"]: c for c in usa_p["best"]}
    assert up[2024]["projection"] is True and up[2024]["flags"]["estimate_kind"] == "projection_upstream"
    assert "source_break" not in up[2024] and usa_p["projection_years"] == [2024]


# Fixed (agent REVFIX2): WDI cells after the WPP 2024 estimate horizon carry estimate_kind=projection_upstream
# (registry cell rule), never answer by default and answer flagged when projections are requested.
def test_headline_wdi_value_equal_to_the_excluded_wpp_projection_is_not_an_actual(synth_atlas: SynthAtlas) -> None:
    synth_atlas.cell(WPP, "IND", 2023, "1438069.596", unit="thousands", flags={"projection": False})
    synth_atlas.cell(WPP_PROJ, "IND", 2025, "1463865.525", unit="thousands", flags={"projection": True})
    synth_atlas.cell(WDI, "IND", 2025, "1463865525", unit="persons")  # = the WPP projection, to the person
    q = synth_atlas.query()
    try:
        reg = load_headlines()
        default = q.headline_map(reg, "reva.population.total", 2025)
        m = q.headline_map(reg, "reva.population.total", 2025, projections="include")
    finally:
        q.close()
    assert not [c for c in default["values"] if c["iso3"] == "IND"]
    (miss,) = [c for c in default["countries_without_value"] if c["iso3"] == "IND"]
    assert _status(miss, WDI) == "projection_upstream"
    (cell,) = [c for c in m["values"] if c["iso3"] == "IND"]
    assert cell["answered_by"]["series"] == WDI
    assert _mentions(cell, "projection")
    assert cell["projection"] is True and m["projection_values"] == 1
    assert cell["flags"]["estimate_rule"]["label"]["en"] == "projection (UN WPP via World Bank)"


# --------------------------------------------------------------------------- the same, on the dev snapshot
def test_dev_moldova_population_series_break_is_flagged(dev_atlas: Any) -> None:
    """MDA: WPP includes Transnistria, the WDI does not (-21.7 % if spliced): the WDI never answers MDA."""
    reg = load_headlines()
    s = dev_atlas.headline_series(reg, "reva.population.total", "MDA", projections="include")
    by_year = {c["year"]: c for c in s["best"]}
    a = by_year[2023]
    assert a["answered_by"]["series"] == WPP and a["value"] == "3067070"
    assert {c["answered_by"]["series"] for c in s["best"]} == {WPP}
    assert _status(a, WDI) == "not_equivalent_for_country"
    assert 2024 not in by_year  # nothing spliced from another territorial scope


def test_dev_india_2025_population_is_not_presented_as_an_actual(dev_atlas: Any) -> None:
    proj = rows(
        dev_atlas,
        """SELECT x.value FROM cells x JOIN series s USING (sid)
           WHERE s.source='un_wpp' AND s.dataset='wpp2024_demo' AND s.indicator='TPopulation1July|proj=medium'
             AND x.iso3='IND' AND x.year=2025""",
    )
    assert proj and proj[0]["value"] == "1463865.525"
    reg = load_headlines()
    default = {x["year"]: x for x in dev_atlas.headline_series(reg, "reva.population.total", "IND")["best"]}
    assert max(default) == 2023 and not any(x.get("projection") for x in default.values())
    m = dev_atlas.headline_series(reg, "reva.population.total", "IND", projections="include")
    by_year = {x["year"]: x for x in m["best"]}
    c = by_year[2025]
    assert c["value"] == "1463865525" and c["answered_by"]["series"] == WDI
    assert _mentions(c, "projection")
    brk = by_year[2024]["source_break"]  # WPP -> WDI in 2024, both values of 2023 shown
    assert brk["from"] == WPP and brk["to"] == WDI and brk["overlap"]["year"] == 2023
