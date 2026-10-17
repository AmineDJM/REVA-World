"""Offline tests of the V4ENERGY connectors: World Bank Pink Sheet (atlas.wb_pinksheet), EIA Short-Term Energy
Outlook (atlas.eia_steo) and the WRI Global Power Plant Database (atlas.wri_gppd, KG facilities).

Fixtures (captured live 2026-10-17; each manifest entry records the live sha256/size and what the excerpt holds):
``tests/fixtures/source/atlas.wb_pinksheet/`` (whole Commodity Markets page; both CMO workbooks with period rows cut
to the first 2 / last 3), ``atlas.eia_steo/`` (whole bulk manifest; STEO.zip re-zipped with verbatim lines),
``atlas.wri_gppd/`` (whole README and DATABASE_VERSION; CSV header + verbatim rows).
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
import pytest
import yaml

from reva.adapters.connectors import atlas_eia_steo, atlas_gppd, atlas_pinksheet
from reva.atlas.sources import MAX_ATLAS_RESPONSE_BYTES, discover
from reva.atlas.tables import validate_atlas_frame

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "source"
SHA = "a" * 64


def _fixture(conn: str, url_part: str) -> bytes:
    man = json.loads((FIX / conn / "manifest.json").read_text())
    hits = [e for e in man if url_part in e["url"]]
    assert len(hits) == 1, url_part
    return (FIX / conn / hits[0]["file"]).read_bytes()


# ---------------------------------------------------------------------------------------------- common
@pytest.mark.parametrize("name", ["atlas.wb_pinksheet", "atlas.eia_steo", "atlas.wri_gppd"])
def test_specs_are_discovered_and_bounded(name: str) -> None:
    found, errors = discover()
    assert not [e for e in errors if name.split(".")[1] in json.dumps(e)]
    spec = found[name].spec
    assert spec.max_response_bytes <= MAX_ATLAS_RESPONSE_BYTES
    assert spec.planned_requests <= 3


@pytest.mark.parametrize("conn", ["atlas.wb_pinksheet", "atlas.eia_steo", "atlas.wri_gppd"])
def test_fixture_manifests_match_their_bytes(conn: str) -> None:
    man = json.loads((FIX / conn / "manifest.json").read_text())
    for e in man:
        body = (FIX / conn / e["file"]).read_bytes()
        assert hashlib.sha256(body).hexdigest() == e["sha256"]
        assert e["licence"] and e["live_sha256"] and e["excerpt"]


def test_registry_files_and_budget_request() -> None:
    keys = {yaml.safe_load(p.read_text())["source_key"] for p in (ROOT / "registry" / "sources").glob("*.yaml")}
    assert {"worldbank.cmo", "wri.gppd", "eia.opendata_bulk"} <= keys
    req = yaml.safe_load((ROOT / "registry" / "atlas" / "budget_requests" / "v4energy.yaml").read_text())
    assert {r["source_key"] for r in req["requests"]} == {"worldbank.cmo", "wri.gppd", "eia.opendata_bulk"}


# ---------------------------------------------------------------------------------------------- Pink Sheet
def test_pinksheet_page_links_and_licence_check() -> None:
    page = _fixture("atlas.wb_pinksheet", "commodity-markets")
    links = atlas_pinksheet.parse_page(page)
    assert links["Monthly"].endswith("/CMO-Historical-Data-Monthly.xlsx")
    assert links["Annual"].endswith("/CMO-Historical-Data-Annual.xlsx")
    with pytest.raises(atlas_pinksheet.PinkSheetParseError, match="licence unverified"):
        atlas_pinksheet.parse_page(page.replace(atlas_pinksheet.LICENCE_LINK.encode(), b"https://example.org/x"))


def test_pinksheet_monthly_prices_verbatim_and_never_zero() -> None:
    body = _fixture("atlas.wb_pinksheet", "Monthly.xlsx")
    frames, reps, notes = atlas_pinksheet.parse_workbook(body, "Monthly", SHA, {"monthly_prices", "monthly_indices"})
    f = frames["monthly_prices"]
    validate_atlas_frame(f, source="wb_cmo", dataset="monthly_prices", raw_sha256s=[SHA])
    brent = f.filter((pl.col("indicator") == "Crude oil, Brent") & (pl.col("period_start") == date(2026, 9, 1)))
    assert brent["value"].to_list() == ["116.8"]
    assert brent["unit"].to_list() == ["($/bbl) | monthly prices in nominal US dollars, 1960 to present"]
    assert brent["period_end"].to_list() == [date(2026, 10, 1)]
    assert set(f["provider_geo"].unique()) == {"global_market"} and f["iso3"].null_count() == f.height
    # "…" (not available) gives no cell; spreadsheet errors are value-less and flagged, never zero
    assert reps["monthly_prices"].not_available > 0
    err = f.filter(pl.col("value").is_null())
    assert err.height == 4
    assert all(json.loads(x)["missing"] == "provider_error" for x in err["flags"])
    hm = f.filter(pl.col("indicator") == "Coal, South African **")
    assert json.loads(hm["flags"][0])["header_marker"] == "**"
    assert "* denotes forecast series" in notes
    idx = frames["monthly_indices"]
    assert {"Total Index", "Energy", "Precious Metals"} <= set(idx["indicator"].unique())
    assert idx["unit"][0].startswith("monthly indices based on nominal US dollars, 2010=100")


def test_pinksheet_annual_real_prices_say_real() -> None:
    body = _fixture("atlas.wb_pinksheet", "Annual.xlsx")
    want = {s.dataset for s in atlas_pinksheet.SHEETS if s.workbook == "Annual"}
    frames, _, _ = atlas_pinksheet.parse_workbook(body, "Annual", SHA, want)
    assert set(frames) == want
    real = frames["annual_prices_real"]
    b = real.filter((pl.col("indicator") == "Crude oil, Brent") & (pl.col("period_start") == date(2025, 1, 1)))
    assert b["value"].to_list() == ["59.5"] and "real 2010 US dollars" in b["unit"][0]
    nom = frames["annual_prices_nominal"]
    assert "nominal US dollars" in nom["unit"][0]


# ---------------------------------------------------------------------------------------------- STEO
def test_steo_observed_and_forecast_never_mixed() -> None:
    atlas_eia_steo.parse_manifest(_fixture("atlas.eia_steo", "manifest.txt"))
    frames, rep = atlas_eia_steo.parse_bulk(_fixture("atlas.eia_steo", "STEO.zip"), SHA)
    obs, fc = frames[atlas_eia_steo.DS_OBS], frames[atlas_eia_steo.DS_FC]
    for ds, f in frames.items():
        validate_atlas_frame(f, source="eia_steo", dataset=ds, raw_sha256s=[SHA])
    b_obs = obs.filter(pl.col("indicator") == "STEO.BREPUUS.M")
    b_fc = fc.filter(pl.col("indicator") == "STEO.BREPUUS.M")
    assert b_obs["period_start"].max() == date(2026, 9, 1)  # lastHistoricalPeriod 202609
    assert b_fc["period_start"].min() == date(2026, 10, 1)
    assert b_obs.filter(pl.col("period_start") == date(1990, 1, 1))["value"].to_list() == ["21.251"]
    assert all(json.loads(x)["estimate_kind"] == "projection" for x in b_fc["flags"])
    assert all("estimate_kind" not in json.loads(x) for x in obs["flags"])
    # a series without lastHistoricalPeriod never reaches the observed table
    assert "STEO.PAPR_VE.A" in rep.no_boundary_series
    assert obs.filter(pl.col("indicator") == "STEO.PAPR_VE.A").height == 0
    ve = fc.filter(pl.col("indicator") == "STEO.PAPR_VE.A")
    assert ve.height > 0 and all(json.loads(x)["estimate_kind"] == "unknown_boundary" for x in ve["flags"])
    # quarterly periods are quarters; Saudi crude production maps to SAU
    q = obs.filter(pl.col("indicator") == "STEO.BREPUUS.Q").sort("period_start").head(1)
    assert q["period_start"].to_list() == [date(1990, 1, 1)] and q["period_end"].to_list() == [date(1990, 4, 1)]
    assert set(obs.filter(pl.col("indicator") == "STEO.PAPR_SA.M")["iso3"].unique()) == {"SAU"}
    states = obs.filter(pl.col("provider_geo").str.starts_with("USA-AL+"))
    assert states.height > 0 and states["iso3"].null_count() == states.height


def test_steo_manifest_must_name_the_fetched_url() -> None:
    doc = json.loads(_fixture("atlas.eia_steo", "manifest.txt"))
    doc["dataset"]["STEO"]["accessURL"] = "https://www.eia.gov/opendata/bulk/OTHER.zip"
    with pytest.raises(atlas_eia_steo.SteoParseError):
        atlas_eia_steo.parse_manifest(json.dumps(doc).encode())


# ---------------------------------------------------------------------------------------------- GPPD
def _plants() -> tuple[list[Any], Any]:
    return atlas_gppd.parse_csv(_fixture("atlas.wri_gppd", "global_power_plant_database.csv"))


def test_gppd_licence_and_version_checks() -> None:
    atlas_gppd.check_licence(_fixture("atlas.wri_gppd", "README.md"))
    with pytest.raises(atlas_gppd.GppdParseError):
        atlas_gppd.check_licence(b"# no licence here")
    assert atlas_gppd.parse_version(_fixture("atlas.wri_gppd", "DATABASE_VERSION")) == "1.3.0"


def test_gppd_kg_statements_are_valid_and_never_by_name() -> None:
    from reva.kg.model import _normalise_entity, _normalise_statement

    plants, rep = _plants()
    assert rep.plants == len(plants) > 10
    parts = atlas_gppd.build_kg(plants, raw_sha256=SHA, version="1.3.0", country_qids={"FRA": "Q142"})
    props = {p.pid: p.value_kind for p in atlas_gppd.load_predicates()}
    for i, s in enumerate(parts.statements):
        _normalise_statement(i, s, atlas_gppd.SOURCE_KEY, {SHA}, props)
    for i, e in enumerate(parts.entities):
        _normalise_entity(i, e)
    by = Counter(s.property_pid for s in parts.statements)
    assert by["gppd:country"] == by["gppd:capacity_mw"] == rep.plants
    fra = [s for s in parts.statements if s.property_pid == "gppd:country" and s.object_qid == "Q142"]
    assert fra and all(s.subject_qid.startswith("gppd:") for s in fra)
    assert any(s.object_qid == "iso3:USA" for s in parts.statements if s.property_pid == "gppd:country")
    # owners stay strings: no company entity is created
    assert {e.kind for e in parts.entities} == {"facility", "energy_source"}
    assert all(s.value["type"] == "string" for s in parts.statements if s.property_pid == "gppd:owner_name")
    fuels = {e.qid for e in parts.entities if e.kind == "energy_source"}
    assert all(s.object_qid in fuels for s in parts.statements if s.property_pid == "gppd:primary_fuel")
    gen = [s for s in parts.statements if s.property_pid == "gppd:generation_gwh"]
    assert gen and all(s.point_in_time is not None and s.value["unit"] == "GWh" for s in gen)
    assert not any("estimated" in s.property_pid for s in parts.statements)


def test_gppd_kosovo_code_is_an_evidenced_alias() -> None:
    from reva.atlas.tables import country_iso3

    assert country_iso3("KOS", atlas_gppd.GEO_SCHEME) == "XKX"
    assert country_iso3("FRA", atlas_gppd.GEO_SCHEME) == "FRA"


def test_v4energy_relation_mappings_are_valid() -> None:
    doc = yaml.safe_load((ROOT / "registry" / "kg" / "relations_v4.d" / "v4energy.yaml").read_text())
    assert doc["agent"] == "V4ENERGY"
    preds = {
        p["pid"]
        for p in yaml.safe_load((ROOT / "registry" / "kg" / "predicates_energy.yaml").read_text())["predicates"]
    }
    for m in doc["mappings"]:
        if ":" in m["property"]:
            assert m["property"] in preds
