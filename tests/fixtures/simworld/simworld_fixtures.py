"""Offline fixtures of agent SIMWORLD (WPP 2024 / ILOSTAT excerpts of the real bytes, retrieved 2026-10-15).

``tests/fixtures/source/un.wpp`` holds byte-range parts of small gzip excerpts of the four WPP 2024 files
(Country/Area rows of SEN, JPN and BRA, Medium variant, 2005-2023, plus a World row, a Low-variant row and
an out-of-range year row that the parser must drop and count); ``fixture_files.json`` gives their sizes and
the part size. ``tests/fixtures/source/ilostat.labour_age5`` holds verbatim ILOSTAT lines of SEN, JPN, BRA,
KOS (Kosovo -> XKX) and ANT (Netherlands Antilles, unmapped). Every manifest entry names what was kept.
"""

from __future__ import annotations

import asyncio
import json
from functools import cache
from pathlib import Path
from typing import Any

from reva.adapters.connectors import ilostat_labour as IL
from reva.adapters.connectors import un_wpp_demography as W
from reva.evidence.runner import FIXTURE_ROOT, FixtureHttp

WPP_DIR = FIXTURE_ROOT / W.NAME
COUNTRIES = ("BRA", "JPN", "SEN")


@cache
def wpp_fixture_spec() -> dict[str, Any]:
    entries = json.loads((WPP_DIR / "fixture_files.json").read_text())
    return {"files": entries, "chunk_bytes": entries[0]["chunk_bytes"], "first_year": entries[0]["first_year"]}


def wpp_connector() -> W.UnWppDemographyConnector:
    spec = wpp_fixture_spec()
    pinned = {f.key: f for f in W.FILES}
    files = [
        W.WppFile(e["key"], e["file"], e["size"], None, pinned[e["key"]].last_modified, pinned[e["key"]].header)
        for e in spec["files"]
    ]
    return W.UnWppDemographyConnector(files, chunk_bytes=spec["chunk_bytes"], first_year=spec["first_year"])


@cache
def wpp_parts() -> dict[str, list[bytes]]:
    """``{file_key: [part bytes]}`` fetched through the connector from the fixture manifest."""
    c = wpp_connector()
    http = FixtureHttp(W.NAME)

    async def go() -> dict[str, list[bytes]]:
        out: dict[str, list[bytes]] = {}
        for item in c.all_items():
            raw = await c.fetch(item, http)
            out.setdefault(item.params["file_key"], []).append(raw.body)
        return out

    return asyncio.run(go())


@cache
def wpp_tables() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    c = wpp_connector()
    return W.build_tables(wpp_parts(), first_year=c.first_year, last_year=c.last_year)


def wpp_table(dataset: str, iso3: str) -> Any:
    return wpp_tables()[0][dataset].get(iso3)


@cache
def ilo_tables() -> dict[str, dict[str, Any]]:
    out = {}
    for ind in IL.INDICATORS:
        body = (FIXTURE_ROOT / IL.NAME / f"{ind.id}.csv.gz").read_bytes()
        out[ind.id] = IL.build_tables(ind.id, body)[0]
    return out


def ilo_table(name: str, iso3: str) -> Any:
    ind = {"employment_rate": "EMP_DWAP_SEX_AGE_RT_A", "activity_rate": "EAP_DWAP_SEX_AGE_RT_A"}[name]
    return ilo_tables()[ind].get(iso3)


WPP_INPUTS = {
    "population": "wpp2024_pop_jan1",
    "deaths": "wpp2024_deaths",
    "births": "wpp2024_births",
    "wpp_indicators": "wpp2024_indicators",
}


def fixture_root() -> Path:
    return FIXTURE_ROOT
