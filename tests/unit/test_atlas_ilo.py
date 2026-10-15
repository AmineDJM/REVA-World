"""A-ILO labour atlas connectors, offline (recorded real responses, restricted to a few areas).

Fixtures: tests/fixtures/source/atlas.ilostat (ILOSTAT ref_area=FRA+DZA+KOS+AUT+TWN+X01 + full code
dictionaries + TOC), atlas.eurostat.earn_mw_cur (geo FR, AT, EL, UK, DE), atlas.oecd.earnings
(REF_AREA FRA, USA, OECD), captured 2026-10-15.
"""

from __future__ import annotations

import asyncio
import gzip
import json
from typing import Any

import polars as pl
import pytest

from reva.adapters.connectors import atlas_ilo as ilo
from reva.adapters.connectors import atlas_wages_gapfill as gap
from reva.adapters.connectors.atlas_labour_common import (
    AtlasRunReport,
    atlas_spec,
    check_body,
    fetch_plan,
    gunzip_bounded,
    strict_decimal,
)
from reva.atlas.catalog import load_catalog
from reva.atlas.tables import country_iso3, validate_atlas_frame
from reva.evidence.runner import FixtureHttp
from reva.kernel.errors import SourceResponseError, ValidationProblem
from reva.ports.connectors import FetchItem, RawAcquisition

AREAS = ("FRA", "DZA", "KOS", "AUT", "TWN", "X01")
GEOS = ("FR", "AT", "EL", "UK", "DE")
OECD_AREAS = ("FRA", "USA", "OECD")


def _tables(conn: Any) -> dict[str, pl.DataFrame]:
    rep = AtlasRunReport(conn.describe().name, "test_fixture")
    raws = asyncio.run(fetch_plan(FixtureHttp(conn.describe().name), conn.plan(), rep))
    assert rep.errors == [], rep.errors
    out = {}
    for p in conn.build_tables(raws):
        frame, _ = validate_atlas_frame(
            p.frame, source=p.source, dataset=p.dataset, raw_sha256s=p.raw_sha256s, geo_scheme=p.geo_scheme
        )
        out[f"{p.source}.{p.dataset}"] = frame
    return out


@pytest.fixture(scope="module")
def ilo_tables() -> dict[str, pl.DataFrame]:
    return _tables(ilo.IlostatConnector(ref_areas=AREAS))


@pytest.fixture(scope="module")
def gap_tables() -> dict[str, pl.DataFrame]:
    return {
        **_tables(gap.EurostatMinimumWageConnector(geos=GEOS)),
        **_tables(gap.OecdEarningsConnector(areas=OECD_AREAS)),
    }


def _cell(frame: pl.DataFrame, iso3: str, indicator: str, year: int) -> dict[str, Any]:
    rows = frame.filter(
        (pl.col("iso3") == iso3) & (pl.col("indicator") == indicator) & (pl.col("period_start").dt.year() == year)
    ).to_dicts()
    assert len(rows) == 1, rows
    return rows[0]


# --------------------------------------------------------------------------- ILOSTAT
def test_every_indicator_becomes_a_valid_table(ilo_tables: dict[str, pl.DataFrame]) -> None:
    assert set(ilo_tables) == {f"ilostat.{i.dataset}" for i in ilo.INDICATORS}
    for key, frame in ilo_tables.items():
        assert frame.height > 0, key
        assert frame["indicator"].str.contains(r"[^|]").all()


def test_minimum_wage_keeps_provider_currencies(ilo_tables: dict[str, pl.DataFrame]) -> None:
    mw = ilo_tables["ilostat.ear_inee_cur_nb_a"]
    lcu = _cell(mw, "FRA", "EAR_INEE_CUR_NB|classif1=CUR_TYPE_LCU", 2026)
    assert lcu["value"] == "1867" and lcu["unit"] == "EUR per month"
    flags = json.loads(lcu["flags"])
    assert flags["local_currency"] == "EUR" and flags["currency_basis"] == "local currency as reported"
    assert flags["notes"]["T30:175"] == "Currency: FRA - Euro (EUR)"
    usd = _cell(mw, "DZA", "EAR_INEE_CUR_NB|classif1=CUR_TYPE_USD", 2024)
    assert usd["value"] == "149.195" and usd["unit"].startswith("USD per month (ILOSTAT conversion")
    ppp = _cell(mw, "DZA", "EAR_INEE_CUR_NB|classif1=CUR_TYPE_PPP", 2024)
    assert ppp["unit"] == "2021 PPP $ per month (ILOSTAT conversion)"
    assert _cell(mw, "DZA", "EAR_INEE_CUR_NB|classif1=CUR_TYPE_LCU", 2024)["unit"] == "DZD per month"
    # Austria has no statutory national minimum wage: ILOSTAT publishes no row (nothing invented)
    assert mw.filter(pl.col("iso3") == "AUT").height == 0
    # selector points at the row of the raw CSV
    assert json.loads(lcu["selector"])["file"] == "EAR_INEE_CUR_NB_A.csv"


def test_mean_earnings_and_geo_mapping(ilo_tables: dict[str, pl.DataFrame]) -> None:
    ea = ilo_tables["ilostat.ear_emta_sex_cur_nb_a"]
    assert _cell(ea, "FRA", "EAR_EMTA_SEX_CUR_NB|sex=SEX_T|classif1=CUR_TYPE_LCU", 2025)["value"] == "4445.62"
    # ILOSTAT KOS -> crosswalk XKX (registry/atlas/geo_aliases/ilo.yaml); TWN / X01 stay unmapped but kept
    assert country_iso3("KOS", "ilo") == "XKX" and country_iso3("CHA", "ilo") == "CHI"
    assert "XKX" in set(ea["iso3"].drop_nulls().to_list())
    une = ilo_tables["ilostat.une_deap_sex_age_rt_a"]
    unmapped = set(une.filter(pl.col("iso3").is_null())["provider_geo"].to_list())
    assert unmapped == {"TWN", "X01"}


def test_modelled_and_national_estimates_are_labelled(ilo_tables: dict[str, pl.DataFrame]) -> None:
    mod = ilo_tables["ilostat.une_2eap_sex_age_rt_a"]
    ind = "UNE_2EAP_SEX_AGE_RT|sex=SEX_T|classif1=AGE_YTHADULT_YGE15"
    f24 = json.loads(_cell(mod, "FRA", ind, 2024)["flags"])
    assert f24["estimate_type"] == "ilo_modelled" and f24["ilo_source_label"] == "ILO - Modelled Estimates"
    assert "projection" not in f24
    # values for years after the series vintage (Nov. 2025) are projections, flagged as such
    f27 = json.loads(_cell(mod, "FRA", ind, 2027)["flags"])
    assert "Nov. 2025" in f27["projection"]
    nat = ilo_tables["ilostat.une_deap_sex_age_rt_a"]
    nat_fra = json.loads(_cell(nat, "FRA", "UNE_DEAP_SEX_AGE_RT|sex=SEX_T|classif1=AGE_YTHADULT_YGE15", 2024)["flags"])
    assert nat_fra["estimate_type"] == "national"
    world = nat.filter(pl.col("provider_geo") == "X01")["flags"].map_elements(
        lambda s: json.loads(s)["estimate_type"], return_dtype=pl.String
    )
    assert set(world.to_list()) == {"ilo_estimate_in_national_series"}


def test_missing_cells_carry_a_reason(ilo_tables: dict[str, pl.DataFrame]) -> None:
    for key, frame in ilo_tables.items():
        empty = frame.filter(pl.col("value").is_null())
        for flags in empty["flags"].to_list():
            assert json.loads(flags)["missing"] in ("unreliable", "not_reported"), key


def test_catalog_series_exist_in_the_tables(
    ilo_tables: dict[str, pl.DataFrame], gap_tables: dict[str, pl.DataFrame]
) -> None:
    tables = {**ilo_tables, **gap_tables}
    have = {f"{k}.{i}" for k, f in tables.items() for i in f["indicator"].unique().to_list()}
    cat = load_catalog()
    labour = [c for c in cat.concepts.values() if c.domain == "labour"]
    assert len(labour) >= 30
    missing = [(c.id, s.key) for c in labour for s in c.source_order if s.key not in have]
    # every series string of labour.yaml must name a real (table, indicator); the restricted fixtures
    # contain every one of them (checked against the full 2026-10-15 files too)
    assert missing == []
    for c in labour:
        assert c.provider_declared is True
        srcs = {s.key.split(".", 2)[1] for s in c.source_order}
        # modelled and national ILOSTAT series are never in one source order
        assert not ({d for d in srcs if "_2" in d} and {d for d in srcs if d.startswith(("une_d", "eap_d"))}), c.id


def test_parse_refuses_bad_files() -> None:
    ind = ilo.INDICATORS[0]
    dicts: dict[str, dict[str, str]] = {v: {} for v in ilo.DICTIONARIES}
    head = '"ref_area","source","indicator","sex","classif1","time","obs_value"\n'
    good = '"FRA","XA:2048","UNE_2EAP_SEX_AGE_RT","SEX_T","AGE_YTHADULT_YGE15","2024",7.4\n'
    frame = ilo.parse_indicator(gzip.compress((head + good).encode()), "a" * 64, ind, dicts)
    assert frame["value"].to_list() == ["7.4"]
    with pytest.raises(ValidationProblem):  # two sources for one cell: refuse, never pick one silently
        ilo.parse_indicator(
            gzip.compress((head + good + good.replace("XA:2048", "BA:1")).encode()), "a" * 64, ind, dicts
        )
    with pytest.raises(SourceResponseError):  # another indicator's file
        ilo.parse_indicator(gzip.compress((head + good.replace("UNE_2EAP", "UNE_DEAP")).encode()), "a" * 64, ind, dicts)
    with pytest.raises(SourceResponseError):  # quarterly time
        ilo.parse_indicator(gzip.compress((head + good.replace('"2024"', '"2024Q1"')).encode()), "a" * 64, ind, dicts)
    with pytest.raises(SourceResponseError):  # not gzip / truncated
        ilo.parse_indicator(gzip.compress((head + good).encode())[:30], "a" * 64, ind, dicts)
    with pytest.raises(ValidationProblem):  # non-numeric value
        ilo.parse_indicator(gzip.compress((head + good.replace("7.4", '"n/a"')).encode()), "a" * 64, ind, dicts)


def test_empty_body_is_a_failed_request_not_no_data() -> None:
    item = FetchItem(key="data:X", url="https://rplumber.ilo.org/x", max_bytes=10, expected_media_types=ilo.GZIP_MEDIA)
    from datetime import UTC, datetime

    raw = RawAcquisition(
        item=item,
        body=b"",
        media_type="application/octet-stream",
        retrieved_at=datetime.now(UTC),
        response_metadata={},
        request_fingerprint="f",
        origin="test_fixture",
    )
    with pytest.raises(SourceResponseError):
        check_body(item, raw)
    html = raw.model_copy(update={"body": b"<html>", "media_type": "text/html"})
    with pytest.raises(SourceResponseError):
        check_body(item, html)


def test_build_tables_needs_dictionaries() -> None:
    with pytest.raises(SourceResponseError):
        ilo.IlostatConnector(ref_areas=AREAS).build_tables({})


def test_spec_and_plan() -> None:
    c = ilo.IlostatConnector()
    spec = c.describe()
    assert str(spec.template) == "atlas_bulk" and spec.source_key == "ilostat.rplumber"
    assert spec.allowed_hosts == ("rplumber.ilo.org",)
    urls = [i.url for i in c.plan()]
    assert all(u.startswith("https://rplumber.ilo.org/") for u in urls)
    assert "https://rplumber.ilo.org/data/indicator/?id=EAR_INEE_CUR_NB_A&format=.csv.gz" in urls
    assert atlas_spec(**{k: v for k, v in spec.model_dump().items() if k != "template"}).template == spec.template


# --------------------------------------------------------------------------- helpers
def test_strict_decimal_and_gunzip_bounds() -> None:
    assert strict_decimal("7.558") == "7.558"
    assert strict_decimal(" 5500 ") == "5500"
    assert strict_decimal(".5") == "0.5"
    assert strict_decimal("1E3") == "1E3"
    assert strict_decimal("") is None and strict_decimal(None) is None
    for bad in ("NaN", "inf", "1,000", ".."):
        with pytest.raises(ValidationProblem):
            strict_decimal(bad)
    blob = gzip.compress(b"x" * 10_000)
    assert len(gunzip_bounded(blob, 10_000)) == 10_000
    with pytest.raises(SourceResponseError):
        gunzip_bounded(blob, 9_999)
    with pytest.raises(SourceResponseError):
        gunzip_bounded(b"<html>not gzip</html>", 10_000)


# --------------------------------------------------------------------------- gap fill
def test_eurostat_states_absence_of_minimum_wage(gap_tables: dict[str, pl.DataFrame]) -> None:
    mw = gap_tables["eurostat.earn_mw_cur"]
    aut = mw.filter(pl.col("iso3") == "AUT")
    assert aut.height > 0 and aut["value"].null_count() == aut.height
    flags = json.loads(aut["flags"][0])
    assert flags["missing"] == "not_applicable" and flags["provider_flag"] == "m"
    assert flags["provider_flag_label"] == "missing value; data cannot exist"
    fra = mw.filter((pl.col("iso3") == "FRA") & (pl.col("indicator") == "earn_mw_cur|currency=NAC"))
    last = fra.sort("period_start").tail(1).to_dicts()[0]
    assert (str(last["period_start"]), str(last["period_end"])) == ("2026-07-01", "2027-01-01")
    assert last["value"] == "1867" and last["unit"].startswith("national currency per month")
    assert set(mw["iso3"].drop_nulls().to_list()) == {"AUT", "DEU", "FRA", "GBR", "GRC"}  # EL -> GRC, UK -> GBR


def test_eurostat_semester_periods() -> None:
    assert [str(d) for d in gap.semester_period("2024-S1")] == ["2024-01-01", "2024-07-01"]
    assert [str(d) for d in gap.semester_period("2024-S2")] == ["2024-07-01", "2025-01-01"]
    with pytest.raises(SourceResponseError):
        gap.semester_period("2024-Q1")


def test_oecd_national_currency_is_folded_but_kept(gap_tables: dict[str, pl.DataFrame]) -> None:
    mw = gap_tables["oecd.earnings_mw_curp"]
    us = _cell(mw, "USA", "SM_WG|unit=NCU|pay_period=H|price_base=V", 2025)
    assert us["value"] == "7.25" and us["unit"] == "USD per hour, current prices"
    assert json.loads(us["flags"])["currency"] == "USD"
    av = gap_tables["oecd.earnings_av_an_wage"]
    ppp = _cell(av, "FRA", "WG|unit=USD_PPP|pay_period=A|price_base=Q|agg=MEAN", 2025)
    assert ppp["unit"] == "USD_PPP per year, constant prices (base 2025)"
    assert "currency" not in json.loads(ppp["flags"])
    assert av.filter(pl.col("provider_geo") == "OECD")["iso3"].null_count() > 0  # aggregate kept, unmapped


def test_sdmx_codelist_refuses_dtd() -> None:
    with pytest.raises(SourceResponseError):
        gap.parse_sdmx_codelist(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x/>')
