"""Vintage connectors (V5, PRED-VINTAGE): parsers on captured fixtures, availability rules, atlas validity.

Fixtures (``tests/fixtures/source/atlas.vintage_*``, licence and live sha256 in each manifest): DBnomics IMF
WEO 2008-04 / 2009-10 (5 countries), IMF portal WEO_2025_OCT_VINTAGE (FRA, USA, IND, World), Philadelphia Fed
RTDSM (ROUTPUT, RUC: vintages 07Q4..09Q1) + SPF release dates (whole) + SPF median workbooks (9 surveys),
ECB SPF (rounds 1999-Q1, 2008-Q1..2009-Q4), OECD STES revisions (FRA, USA; editions of 2008).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest
import yaml

from reva.adapters.connectors import (
    atlas_vintage_ecb_spf as ecb,
)
from reva.adapters.connectors import (
    atlas_vintage_oecd as oecd,
)
from reva.adapters.connectors import (
    atlas_vintage_philfed as pf,
)
from reva.adapters.connectors import (
    atlas_vintage_weo as weo,
)
from reva.adapters.connectors.vintage_common import AVAILABILITY_RULES, FAMILIES, Rows, vintage_flags
from reva.atlas.sources import MAX_ATLAS_RESPONSE_BYTES, discover
from reva.atlas.tables import validate_atlas_frame
from reva.evaluation.vintages import cells_from_atlas_frame

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "source"
SHA = "a" * 64
NAMES = ["atlas.vintage_weo_dbn", "atlas.vintage_weo_imf", "atlas.vintage_philfed", "atlas.vintage_ecb_spf",
         "atlas.vintage_oecd_rt"]


def _fixture(conn: str, url_part: str) -> bytes:
    man = json.loads((FIX / conn / "manifest.json").read_text())
    hits = [e for e in man if url_part in e["url"]]
    assert len(hits) == 1, url_part
    return (FIX / conn / hits[0]["file"]).read_bytes()


def _flags(df: pl.DataFrame) -> list[dict]:
    return [json.loads(f) for f in df["flags"]]


# ---------------------------------------------------------------------------------------------- common
@pytest.mark.parametrize("name", NAMES)
def test_specs_are_discovered_and_bounded(name: str) -> None:
    found, errors = discover()
    assert not [e for e in errors if "vintage" in json.dumps(e)]
    spec = found[name].spec
    assert spec.max_response_bytes <= MAX_ATLAS_RESPONSE_BYTES and spec.planned_requests <= 40


@pytest.mark.parametrize("name", NAMES)
def test_fixture_manifests_match_their_bytes(name: str) -> None:
    man = json.loads((FIX / name / "manifest.json").read_text())
    for e in man:
        body = (FIX / name / e["file"]).read_bytes()
        assert hashlib.sha256(body).hexdigest() == e["sha256"]
        assert e["licence"] and e["live_sha256"] and e["excerpt"]


def test_registry_sources_and_budget_request() -> None:
    keys = {yaml.safe_load(p.read_text())["source_key"] for p in (ROOT / "registry" / "sources").glob("*.yaml")}
    assert {"dbnomics.api", "philfed.data", "ecb.data_api", "imf.sdmx", "oecd.sdmx"} <= keys
    req = yaml.safe_load((ROOT / "registry" / "atlas" / "budget_requests" / "pred_vintage.yaml").read_text())
    assert {r["source_key"] for r in req["requests"]} >= {"dbnomics.api", "philfed.data", "ecb.data_api"}


def test_vintage_flags_refuse_unknown_terms_and_half_availability() -> None:
    ok = dict(family="imf_weo", vintage="2008-04", variable="real_gdp_growth", provider_variable="NGDP_RPCH",
              estimate_kind="projection")
    with pytest.raises(ValueError):
        vintage_flags(**ok, available_at=datetime(2008, 5, 1, tzinfo=UTC), basis=None)
    with pytest.raises(ValueError):
        vintage_flags(**ok, available_at=None, basis="weo_release_month_end")
    with pytest.raises(ValueError):
        vintage_flags(**{**ok, "family": "made_up"}, available_at=None, basis=None)
    assert set(FAMILIES) == {"imf_weo", "philfed_rtdsm", "philfed_spf", "ecb_spf", "oecd_mei_rt"}
    assert len(AVAILABILITY_RULES) == 6


# ---------------------------------------------------------------------------------------------- WEO
def test_weo_dbnomics_vintages_are_month_end_and_never_mix_projections() -> None:
    rows: Rows = Rows()
    stats: dict = {}
    for v in weo.FIXTURE_DBN_VINTAGES:
        docs, n = weo.parse_dbn_page(_fixture("atlas.vintage_weo_dbn", f"WEO:{v}"), v)
        assert n == len(docs)
        weo.parse_dbn(docs, v, SHA, rows, stats)
    f, unmapped = validate_atlas_frame(rows.frame(), source="vintage_weo", dataset="weo_dbnomics",
                                       raw_sha256s=[SHA], geo_scheme="imf")
    assert {u["provider_geo"] for u in unmapped} <= {"UVK"}  # Kosovo (WEO code UVK, later vintages) stays unmapped
    assert set(f["iso3"].drop_nulls().unique()) == {"FRA", "USA", "DEU"}  # WBG/UVK: not in these early vintages
    fra = f.filter((pl.col("provider_geo") == "FRA") & (pl.col("indicator") == "NGDP_RPCH|v=2008-04"))
    fl = {r["period_start"].year: json.loads(r["flags"]) for r in fra.to_dicts()}
    assert fl[2008]["estimate_kind"] == "projection" and fl[2007]["estimate_kind"] == "published_history_boundary_unknown"
    assert fl[2007]["available_at"] == "2008-05-01T00:00:00Z" and fl[2007]["availability_basis"] == "weo_release_month_end"
    assert fl[2007]["vintage_date_text"] is None  # DBnomics keeps no release day: never invented
    v2 = f.filter((pl.col("provider_geo") == "FRA") & (pl.col("indicator") == "NGDP_RPCH|v=2009-10"))
    assert json.loads(v2["flags"][0])["available_at"] == "2009-11-01T00:00:00Z"
    # 2008 growth for France: projection in April 2008, a published value by October 2009 (it changed)
    v08 = fra.filter(pl.col("period_start") == date(2008, 1, 1))["value"][0]
    v09 = v2.filter(pl.col("period_start") == date(2008, 1, 1))["value"][0]
    assert v08 != v09
    assert weo.dbn_available_at("2008-10") == datetime(2008, 11, 1, tzinfo=UTC)
    assert "2011-10" not in weo.DBN_VINTAGES and len(weo.DBN_VINTAGES) == 33


def test_weo_imf_portal_vintage_uses_publication_date_and_boundary() -> None:
    rows: Rows = Rows()
    stats: dict = {}
    vint = weo.parse_imf(_fixture("atlas.vintage_weo_imf", "WEO_2025_OCT_VINTAGE"), SHA, "WEO_2025_OCT_VINTAGE",
                         "1.0.0", rows, stats)
    assert vint["vintage"] == "2025-10" and vint["publication_date"] == "2025-10-14T13:00:00Z"
    f, unmapped = validate_atlas_frame(rows.frame(), source="vintage_weo", dataset="weo_imf", raw_sha256s=[SHA],
                                       geo_scheme="imf")
    assert {u["provider_geo"] for u in unmapped} == {"G001"}
    usa = f.filter((pl.col("provider_geo") == "USA") & (pl.col("indicator") == "NGDP_RPCH|v=2025-10"))
    fl = {r["period_start"].year: json.loads(r["flags"]) for r in usa.to_dicts()}
    assert fl[2024]["estimate_kind"] == "actual" and fl[2025]["estimate_kind"] == "projection"
    assert fl[2025]["available_at"] == "2025-10-14T13:00:00Z"
    # periods are WEO year labels (calendar) in every vintage table, fiscal windows kept in flags
    assert f.select((pl.col("period_start").dt.month() == 1).all()).item()
    with pytest.raises(weo.WeoVintageError):
        weo.parse_imf(_fixture("atlas.vintage_weo_imf", "WEO_2025_OCT_VINTAGE"), SHA, "WEO", "9.0.0", Rows(), {})


# ---------------------------------------------------------------------------------------------- Philadelphia Fed
def test_rtdsm_vintage_columns_and_middle_month_rule() -> None:
    rows: Rows = Rows()
    stats: dict = {}
    for f in pf.FIXTURE_RTDSM:
        pf.parse_rtdsm(_fixture("atlas.vintage_philfed", f"xlsx/{f}"), f, SHA, rows, stats)
    df, _ = validate_atlas_frame(rows.frame(), source="vintage_us", dataset="rtdsm", raw_sha256s=[SHA],
                                 geo_scheme="iso3")
    # 2007:Q4 real GDP first appears in the 2008:Q1 vintage (BEA advance estimate)
    q4 = df.filter((pl.col("period_start") == date(2007, 10, 1)) & pl.col("indicator").str.starts_with("ROUTPUT|"))
    vint = sorted(json.loads(x)["vintage"] for x in q4["flags"])
    assert vint[0] == "2008Q1" and "2007Q4" not in vint
    assert stats["na"] > 0  # '#N/A' = not in that vintage: no cell, never a zero
    assert pf.rtdsm_available_at(2008, 1) == datetime(2008, 3, 1, tzinfo=UTC)
    assert pf.rtdsm_available_at(2008, 4) == datetime(2008, 12, 1, tzinfo=UTC)
    assert pf.rtdsm_vintage("ROUTPUT65Q4", "ROUTPUT") == (1965, 4)
    assert pf.rtdsm_vintage("ROUTPUT26Q3", "ROUTPUT") == (2026, 3)
    ruc = df.filter(pl.col("indicator") == "RUC|v=2008Q2")
    assert ruc.select((pl.col("period_end") <= date(2008, 5, 1)).all()).item()  # monthly obs known by mid-May


def test_spf_release_dates_verbatim_and_unknown_before_1990q2() -> None:
    dates = pf.parse_release_dates(_fixture("atlas.vintage_philfed", "spf-release-dates.txt"))
    assert dates[(1990, 2)] == {"deadline": "8/23/90*", "release": "8/31/90*"}
    assert (1990, 1) not in dates
    rows: Rows = Rows()
    stats: dict = {}
    pf.parse_spf(_fixture("atlas.vintage_philfed", "medianlevel.xlsx"), "level", SHA, dates, rows, stats)
    pf.parse_spf(_fixture("atlas.vintage_philfed", "mediangrowth.xlsx"), "growth", SHA, dates, rows, stats)
    df, _ = validate_atlas_frame(rows.frame(), source="vintage_us", dataset="spf_median", raw_sha256s=[SHA],
                                 geo_scheme="iso3")
    fl = _flags(df)
    assert all(f["estimate_kind"] == "projection" for f in fl)
    old = [f for f in fl if f["vintage"] in ("1989Q4", "1990Q1")]
    assert old and all(f["available_at"] is None and f["availability_basis"] is None for f in old)
    r3 = df.filter(pl.col("indicator") == "RGDP3|v=2008Q1").to_dicts()[0]
    f3 = json.loads(r3["flags"])
    assert r3["period_start"] == date(2008, 4, 1)  # horizon 3 = the quarter after the survey quarter
    assert f3["vintage_date_text"] == "2/12/08" and f3["available_at"] == "2008-02-13T00:00:00Z"
    a = df.filter(pl.col("indicator") == "CPIA|v=2008Q1").to_dicts()[0]
    assert json.loads(a["flags"])["variable"] == "cpi_inflation_q4q4" and a["period_start"] == date(2008, 1, 1)
    h1 = df.filter(pl.col("indicator") == "RGDP1|v=2008Q1").to_dicts()[0]
    assert h1["period_start"] == date(2007, 10, 1) and json.loads(h1["flags"])["jump_off"] is True
    assert pf.horizon_period(2008, 4, "4")[:2] == (date(2009, 4, 1), date(2009, 7, 1))


# ---------------------------------------------------------------------------------------------- ECB / OECD
def test_ecb_spf_calendar_and_longer_term_horizons_only() -> None:
    rows: Rows = Rows()
    stats: dict = {}
    ecb.parse_csv(_fixture("atlas.vintage_ecb_spf", "data-api.ecb.europa.eu"), SHA, rows, stats)
    df, unmapped = validate_atlas_frame(rows.frame(), source="vintage_ea", dataset="ecb_spf", raw_sha256s=[SHA])
    assert stats["rolling_horizon_not_loaded"] > 0
    lt = df.filter(pl.col("indicator") == "HICP.LT|v=2008-Q3").to_dicts()[0]
    assert lt["period_start"] == date(2013, 1, 1)  # 5 calendar years ahead in Q3 rounds
    lt1 = df.filter(pl.col("indicator") == "HICP.LT|v=2008-Q1").to_dicts()[0]
    assert lt1["period_start"] == date(2012, 1, 1)  # 4 calendar years ahead in Q1 rounds
    assert json.loads(lt["flags"])["available_at"] == "2008-10-01T00:00:00Z"
    assert ecb.round_available_at(2008, 4) == datetime(2009, 1, 1, tzinfo=UTC)


def test_oecd_editions_month_end_and_values_verbatim() -> None:
    rows: Rows = Rows()
    stats: dict = {}
    oecd.parse_csv(_fixture("atlas.vintage_oecd_rt", "sdmx.oecd.org"), SHA, rows, stats, year=2008)
    df, _ = validate_atlas_frame(rows.frame(), source="vintage_oecd", dataset="stes_revisions", raw_sha256s=[SHA],
                                 geo_scheme="iso3")
    assert set(df["iso3"].unique()) == {"FRA", "USA"}
    fl = _flags(df)
    assert {f["vintage"] for f in fl} == {"200802", "200805", "200808", "200811"}
    assert oecd.edition_available_at("200811") == datetime(2008, 12, 1, tzinfo=UTC)
    with pytest.raises(oecd.OecdRtError):
        oecd.parse_csv(_fixture("atlas.vintage_oecd_rt", "sdmx.oecd.org"), SHA, Rows(), {}, year=2009)
    assert "200802+200805+200808+200811?startPeriod=2005" in oecd.url(2008)


def test_every_vintage_table_normalises_into_cells() -> None:
    rows: Rows = Rows()
    ecb.parse_csv(_fixture("atlas.vintage_ecb_spf", "data-api.ecb.europa.eu"), SHA, rows, {})
    cells = cells_from_atlas_frame(rows.frame(), dataset_key="atlas.vintage_ea.ecb_spf", table_recorded_at=None)
    assert cells.height == len(rows) and cells["available_at"].null_count() == 0
    assert set(cells["family"].unique()) == {"ecb_spf"}
