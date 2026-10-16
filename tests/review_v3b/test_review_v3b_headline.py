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

import pytest
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


@pytest.mark.xfail(
    strict=True,
    reason="HL-1: the headline series switches from WPP (metropolitan France) to WDI (France incl. overseas "
    "departments) between 2023 and 2024 (+3.2 %) and no cell says the series breaks (query.py _headline/"
    "_reported_cell; source_order/population.yaml declares the two 'the same definition')",
)
def test_headline_series_marks_a_switch_of_answering_source(synth_atlas: SynthAtlas) -> None:
    # real values (dev snapshot b11f1c51; WB API 2026-10-16): WPP FRA 2022-2023 in thousands, WDI FRA 2022-2024
    synth_atlas.cell(WPP, "FRA", 2022, "66277.409", unit="thousands", flags={"projection": False})
    synth_atlas.cell(WPP, "FRA", 2023, "66438.822", unit="thousands", flags={"projection": False})
    synth_atlas.cell(WDI, "FRA", 2022, "68170228", unit="persons")
    synth_atlas.cell(WDI, "FRA", 2023, "68372286", unit="persons")
    synth_atlas.cell(WDI, "FRA", 2024, "68551653", unit="persons")
    q = synth_atlas.query()
    try:
        s = q.headline_series(load_headlines(), "reva.population.total", "FRA")
    finally:
        q.close()
    by_year = {c["year"]: c for c in s["best"]}
    assert by_year[2023]["answered_by"]["series"] == WPP and by_year[2023]["value"] == "66438822"
    assert by_year[2024]["answered_by"]["series"] == WDI and by_year[2024]["value"] == "68551653"
    # the two sources disagree by 2.9 % on the overlapping year 2023: the switch must be stated on the answer
    assert _mentions(by_year[2024], "break", "switch", "discontinu")


@pytest.mark.xfail(
    strict=True,
    reason="HL-2: a WDI cell that is the WPP 2024 projection (WPP estimates stop in 2023, the WPP projection of "
    "the same year is in the snapshot) answers the headline as a reported actual with no projection flag "
    "(query.py PROJECTION_SQL only reads the answering cell's own flags)",
)
def test_headline_wdi_value_equal_to_the_excluded_wpp_projection_is_not_an_actual(synth_atlas: SynthAtlas) -> None:
    synth_atlas.cell(WPP, "IND", 2023, "1438069.596", unit="thousands", flags={"projection": False})
    synth_atlas.cell(WPP_PROJ, "IND", 2025, "1463865.525", unit="thousands", flags={"projection": True})
    synth_atlas.cell(WDI, "IND", 2025, "1463865525", unit="persons")  # = the WPP projection, to the person
    q = synth_atlas.query()
    try:
        m = q.headline_map(load_headlines(), "reva.population.total", 2025)
    finally:
        q.close()
    (cell,) = [c for c in m["values"] if c["iso3"] == "IND"]
    assert cell["answered_by"]["series"] == WDI
    assert _mentions(cell, "projection")


# --------------------------------------------------------------------------- the same, on the dev snapshot
@pytest.mark.xfail(
    strict=True,
    reason="HL-1 (dev data): Moldova reva.population.total 2023 = 3,067,070 (WPP) then 2024 = 2,402,306 (WDI), "
    "-21.7 % in one year from a change of source definition, not flagged",
)
def test_dev_moldova_population_series_break_is_flagged(dev_atlas: Any) -> None:
    s = dev_atlas.headline_series(load_headlines(), "reva.population.total", "MDA")
    by_year = {c["year"]: c for c in s["best"]}
    a, b = by_year[2023], by_year[2024]
    assert a["answered_by"]["series"] == WPP and b["answered_by"]["series"] == WDI
    assert abs(int(b["value"]) / int(a["value"]) - 1) > 0.2
    assert _mentions(b, "break", "switch", "discontinu")


@pytest.mark.xfail(
    strict=True,
    reason="HL-2 (dev data): 120 of 216 WDI 2025 populations equal the WPP 2024 medium projection to the person "
    "(129 of 216 in 2024); India 2025 = 1,463,865,525 is served as reported with flags {}",
)
def test_dev_india_2025_population_is_not_presented_as_an_actual(dev_atlas: Any) -> None:
    proj = rows(
        dev_atlas,
        """SELECT x.value FROM cells x JOIN series s USING (sid)
           WHERE s.source='un_wpp' AND s.dataset='wpp2024_demo' AND s.indicator='TPopulation1July|proj=medium'
             AND x.iso3='IND' AND x.year=2025""",
    )
    assert proj and proj[0]["value"] == "1463865.525"
    m = dev_atlas.headline_series(load_headlines(), "reva.population.total", "IND")
    c = {x["year"]: x for x in m["best"]}[2025]
    assert c["value"] == "1463865525" and c["answered_by"]["series"] == WDI
    assert _mentions(c, "projection")
