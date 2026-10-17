"""V4CLIMATE: global climate indices (NOAA GML, GISTEMP, HadCRUT5, Berkeley Earth, ONI, NSIDC, sea level).

Fixtures: tests/fixtures/source/atlas.climate_indices — the real provider files captured 2026-10-17 (licences per
file in the manifest: US Government works for NOAA/NASA/EPA, Open Government Licence v3 for HadCRUT5, CC BY-NC 4.0
for Berkeley Earth, NSIDC G02135 citation required).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime

import polars as pl
import pytest

from reva.adapters.connectors import atlas_climate_indices as ci
from reva.atlas.tables import validate_atlas_frame
from reva.evidence.runner import FixtureHttp
from reva.kernel.errors import SourceResponseError

T = datetime(2026, 10, 17, 13, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def files() -> dict[str, ci.Fetched]:
    http = FixtureHttp(ci.NAME)
    return {
        f.key: ci.Fetched(f.key, asyncio.run(http.get(f.url)).body, f"{i:064x}", f.url, T)
        for i, f in enumerate(ci.FILES, start=1)
    }


@pytest.fixture(scope="module")
def built(files: dict[str, ci.Fetched]) -> tuple[dict, dict]:
    return ci.build(files)


def _rows(built: tuple[dict, dict], ds: str) -> list[dict]:
    return built[0][ds].rows


def test_every_table_validates_and_is_not_a_country(built: tuple[dict, dict]) -> None:
    for ds, rb in built[0].items():
        f, unmapped = validate_atlas_frame(
            rb.frame(), source=ci.ATLAS_SOURCE, dataset=ds, raw_sha256s=sorted({r["raw_sha256"] for r in rb.rows}),
            geo_scheme="iso3",
        )  # fmt: skip
        assert f.height == len(rb.rows) > 0
        assert set(f["iso3"].to_list()) == {None}
        assert {u["provider_geo"] for u in unmapped} <= {"GLOBAL", "MLO", "NH", "SH", "NINO34"}
        lic = {json.loads(r["flags"])["licence"] for r in rb.rows}
        assert lic == {ci.RIGHTS[ds]["licence"]}


def test_gml_values_sentinels_and_notes(built: tuple[dict, dict]) -> None:
    rows = _rows(built, "noaa_gml")
    mlo = {(r["indicator"], r["period_start"]): r for r in rows if r["provider_geo"] == "MLO"}
    first = mlo[("co2_mlo|average", date(1958, 3, 1))]
    assert first["value"] == "315.71" and first["unit"] == "ppm"
    f = json.loads(first["flags"])
    assert "negative stdev" in f["provider_note"] and f["unit_note"].startswith("ppm")
    ch4 = [r for r in rows if r["indicator"] == "ch4_gl|average"]
    assert ch4 and all(r["unit"] == "ppb" for r in ch4)
    assert "nanomol/mol" in json.loads(ch4[0]["flags"])["definition"]
    assert all(not r["value"].startswith("-") for r in rows)  # -9.99 sentinels never become values


def test_temperature_anomalies(built: tuple[dict, dict]) -> None:
    g = {(r["indicator"], r["period_start"]): r["value"] for r in _rows(built, "gistemp")}
    assert g[("land_ocean_anomaly", date(1880, 1, 1))] == "-0.19"
    assert ("land_ocean_anomaly|annual=J-D", date(1880, 1, 1)) in g
    h = _rows(built, "hadcrut5")
    assert h[0]["period_start"] == date(1850, 1, 1) and "lower_2_5" in json.loads(h[0]["flags"])
    b = _rows(built, "berkeley")
    assert {r["indicator"] for r in b} == {"anomaly|sea_ice=air", "anomaly|sea_ice=water"}
    assert json.loads(b[0]["flags"])["licence"].startswith("CC BY-NC")


def test_oni_seasons_and_psl_cross_check(files: dict[str, ci.Fetched], built: tuple[dict, dict]) -> None:
    assert ci.season_span("DJF", 1950) == (date(1949, 12, 1), date(1950, 3, 1), 1)
    assert ci.season_span("NDJ", 1950) == (date(1950, 11, 1), date(1951, 2, 1), 12)
    r = next(x for x in _rows(built, "enso_oni") if x["indicator"] == "oni_anomaly")
    assert (r["period_start"], r["value"]) == (date(1949, 12, 1), "-1.32")
    tampered = ci.Fetched("cpc_oni", files["cpc_oni"].body.replace(b"DJF 1950  25.01  -1.32", b"DJF 1950  25.01  -1.30"),
                          "a" * 64, files["cpc_oni"].url, T)  # fmt: skip
    with pytest.raises(SourceResponseError, match="PSL"):
        ci.parse_oni(tampered, files["psl_oni"], ci.cc.ValueRows("climind", "enso_oni"), {})


def test_sea_ice_and_sea_level(built: tuple[dict, dict]) -> None:
    ice = _rows(built, "sea_ice")
    sept = [r for r in ice if r["provider_geo"] == "NH" and r["indicator"] == "extent" and r["period_start"].month == 9]
    assert sept[0]["period_start"] == date(1979, 9, 1) and sept[0]["unit"] == "10^6 km2"
    # Dec 1987 is -9999 (satellite gap): no row, never zero
    assert not [r for r in ice if r["provider_geo"] == "NH" and r["period_start"] == date(1987, 12, 1)]
    sl = _rows(built, "sea_level")
    star = [r for r in sl if r["indicator"].startswith("gmsl_anomaly|mission=")]
    assert star[0]["period_end"] - star[0]["period_start"] == date(2000, 1, 2) - date(2000, 1, 1)
    assert "decimal_year" in json.loads(star[0]["selector"])
    epa = [r for r in sl if r["indicator"] == "csiro_adjusted_sea_level"]
    assert epa[0]["period_start"] == date(1880, 1, 1) and epa[0]["unit"] == "inches"
    assert ci.decimal_year_day(ci.Decimal("2000.5")) == date(2000, 7, 2)


def test_kg_climate_index_entities(files: dict[str, ci.Fetched], built: tuple[dict, dict]) -> None:
    ents, sts = ci.kg_items(files, built[1]["definitions"], ci.DATASETS)
    assert {e.kind for e in ents} == {"climate_index"} and len(ents) == len(ci.SERIES)
    assert {s.property_pid for s in sts} <= set(ci.KG_PREDICATES)
    oni = {s.property_pid: s.value["value"] for s in sts if s.subject_qid == "climind:oni"}
    assert "Niño 3.4" in oni["climind:provider_definition"]
    assert oni["climind:atlas_series"] == "climind.enso_oni.oni_anomaly @ provider_geo=NINO34"
    assert all(s.object_qid is None for s in sts)  # descriptive literals only, nothing causal


def test_frames_have_unique_cells(built: tuple[dict, dict]) -> None:
    for rb in built[0].values():
        f: pl.DataFrame = rb.frame()
        assert f.select(["indicator", "provider_geo", "period_start", "period_end"]).is_duplicated().sum() == 0
