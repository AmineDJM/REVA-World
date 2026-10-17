"""PRED-HF daily series: ECB, BIS (bulk), US Treasury, EIA spot prices, Bank of England, New York Fed.

Fixtures: tests/fixtures/source/atlas.{ecb_daily,bis_daily,ust_yields,eia_spot,boe_daily,nyfed_rates} — excerpts of
the real provider files downloaded 2026-10-17 (rows kept verbatim, most rows dropped; policy pages reduced to their
visible text). Licences per file in each manifest.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from typing import Any

import pytest

from reva.adapters.connectors import atlas_hf_bis as bis
from reva.adapters.connectors import atlas_hf_boe as boe
from reva.adapters.connectors import atlas_hf_ecb as ecb
from reva.adapters.connectors import atlas_hf_eia as eia
from reva.adapters.connectors import atlas_hf_nyfed as nyfed
from reva.adapters.connectors import atlas_hf_ust as ust
from reva.adapters.connectors import hf_daily_common as hc
from reva.atlas.sources import AtlasSourceSpec
from reva.atlas.tables import validate_atlas_frame
from reva.evidence.runner import FixtureHttp

MODULES = (ecb, bis, ust, eia, boe, nyfed)
GEO_SCHEMES = {bis: "iso2", ust: "iso2", boe: "iso2", nyfed: "iso2"}


def _specs(mod: Any) -> tuple[hc.FileSpec, ...]:
    return mod.files_for(None) if hasattr(mod, "files_for") else mod.FILES


def _files(mod: Any) -> dict[str, hc.Fetched]:
    http = FixtureHttp(mod.NAME)
    out = {}
    for i, f in enumerate(_specs(mod), start=1):
        r = asyncio.run(http.get(f.url))
        out[f.key] = hc.Fetched(f.key, f.url, r.body, f"{i:064x}", r.retrieved_at)
    return out


@pytest.fixture(scope="module")
def built() -> dict[Any, tuple[dict[str, hc.TableBuild], dict[str, Any]]]:
    return {m: m.build(_files(m)) for m in MODULES}


def _rows(built: dict, mod: Any, ds: str) -> list[dict[str, Any]]:
    return built[mod][0][ds].rows.rows()


def _flags(r: dict[str, Any]) -> dict[str, Any]:
    return json.loads(r["flags"])


@pytest.mark.parametrize("mod", MODULES, ids=lambda m: m.NAME)
def test_specs_are_valid_atlas_sources(mod: Any) -> None:
    spec = mod.atlas_source()
    assert isinstance(spec, AtlasSourceSpec) and spec.name == mod.NAME and spec.datasets == mod.DATASETS
    for f in _specs(mod):
        host = f.url.split("/")[2]
        assert host in spec.allowed_hosts, (mod.NAME, host)
    assert spec.planned_requests >= len(_specs(mod)) or mod is ust


@pytest.mark.parametrize("mod", MODULES, ids=lambda m: m.NAME)
def test_tables_validate_and_carry_day_semantics(built: dict, mod: Any) -> None:
    builds, report = built[mod]
    assert set(builds) == set(mod.DATASETS)
    for ds, b in builds.items():
        frame = b.rows.frame()
        f, _ = validate_atlas_frame(
            frame, source=mod.ATLAS_SOURCE, dataset=ds, raw_sha256s=sorted(set(frame["raw_sha256"])),
            geo_scheme=GEO_SCHEMES.get(mod),
        )  # fmt: skip
        assert f.height == len(b.rows) > 0
        for r in f.head(50).to_dicts() + f.tail(50).to_dicts():
            fl = _flags(r)
            assert fl["licence"] == mod.RIGHTS[ds]["licence"]
            assert fl["as_of"] == "2026-10-17" and fl["estimate_kind"] == "observation"
            assert fl["publication"] and fl["revision_policy"] and fl["pub_lag"]
            if r["value"] is not None:  # data rows: exactly one day; text rows (series metadata) span the series
                assert fl["precision"] == "day" and (r["period_end"] - r["period_start"]).days == 1
    # every verbatim policy quote is still on the archived provider page
    assert report["policy_quotes_found"] and all(report["policy_quotes_found"].values()), report["policy_quotes_found"]


def test_number_keeps_provider_text_and_never_invents() -> None:
    assert hc.number("18.60") == ("18.60", False)
    assert hc.number("-36.98") == ("-36.98", False)
    assert hc.number(".5") == ("0.5", True)
    for bad in ("N/A", "NaN", "", "  ", "W", "--", "inf", None):
        assert hc.number(bad) == (None, False)


def test_duplicate_cells_are_refused_and_changed_quotes_are_reported() -> None:
    rows = hc.DailyRows("ecb", "fx_ref")
    kw = dict(indicator="x", provider_geo="U2", day=date(2026, 1, 2), value="1", unit="u", flags="{}",
              raw_sha256="0" * 64, selector={"line": 1})  # fmt: skip
    rows.add(**kw)  # type: ignore[arg-type]
    with pytest.raises(hc.HfError):
        rows.add(**kw)  # type: ignore[arg-type]
    page = hc.Fetched("p", "https://x", b"<p>The rate is published at <a>9am</a> .</p>", "0" * 64, datetime.now(UTC))
    found = hc.quotes_found({"p": page}, {"ok": ("p", "The rate is published at 9am."), "gone": ("p", "revised")})
    assert found == {"ok": True, "gone": False}


# --------------------------------------------------------------------------- ECB
def test_ecb_reference_rates(built: dict) -> None:
    rows = _rows(built, ecb, "fx_ref")
    usd = {r["period_start"]: r for r in rows if r["indicator"] == "EUR_REF|currency=USD"}
    assert usd[date(1999, 1, 4)]["value"] == "1.1789" and usd[date(1999, 1, 4)]["unit"] == "USD per EUR"
    assert usd[date(2026, 10, 16)]["value"] == "1.1253"
    assert all(r["provider_geo"] == "U2" and r["iso3"] is None for r in rows)
    # N/A cells are no row: Cyprus pound quoted in 1999, not in 2026 (euro adoption)
    cyp = {r["period_start"] for r in rows if r["indicator"] == "EUR_REF|currency=CYP"}
    assert date(1999, 1, 4) in cyp and date(2026, 10, 16) not in cyp
    assert built[ecb][1]["fx_ref"]["not_quoted_cells"]["N/A"] > 0
    assert _flags(rows[0])["pub_lag"] == "same_day"


def test_ecb_estr_and_key_rates(built: dict) -> None:
    estr = _rows(built, ecb, "estr")
    wt = [r for r in estr if r["indicator"] == "EST.B.EU000A2X2A25.WT"]
    assert wt and wt[0]["unit"] == "Percent (PC)" and _flags(wt[0])["pub_lag"] == "next_business_day"
    tt = next(r for r in estr if r["indicator"] == "EST.B.EU000A2X2A25.TT")
    assert _flags(tt)["unit_mult"] == "6: Millions" and tt["unit"] == "Euro (EUR)"  # multiplier never applied
    kr = _rows(built, ecb, "key_rates")
    dfr = next(r for r in kr if r["indicator"] == "FM.D.U2.EUR.4F.KR.DFR.LEV" and r["period_start"] == date(1999, 1, 1))
    assert dfr["value"] == "2" and dfr["unit"] == "Percent per annum (PCPA)"
    assert "date of changes" in _flags(dfr)["series_title"]


def test_ecb_unknown_unit_fails_closed() -> None:
    f = hc.Fetched("estr_csv", "u", b"KEY,FREQ,TIME_PERIOD,OBS_VALUE,OBS_STATUS,TITLE,UNIT_MEASURE,UNIT_MULT\n"
                   b"EST.X,B,2026-01-02,1,A,t,XYZ,0\n", "0" * 64, datetime.now(UTC))  # fmt: skip
    with pytest.raises(hc.HfError, match="codelists"):
        ecb.parse_sdmx_csv("estr", f, hc.DailyRows("ecb", "estr"), {})


# --------------------------------------------------------------------------- BIS
def test_bis_daily_policy_rates_and_series_texts(built: dict) -> None:
    rows = _rows(built, bis, "cbpol_d")
    us = [r for r in rows if r["provider_geo"] == "US"]
    assert us and all(r["indicator"] == "CBPOL" and r["unit"] == "Per cent per year" for r in us)
    first = min(us, key=lambda r: r["period_start"])
    assert first["period_start"] == date(1954, 7, 1) and first["value"] == "1.13"
    assert _flags(first)["series_texts"] == "bis.cbpol_d_series"
    rep = built[bis][1]["cbpol_d"]
    assert rep["dropped"].get("status_M", 0) > 0  # "Missing value; data cannot exist" is never a row
    texts = {(r["provider_geo"], r["indicator"]): r for r in _rows(built, bis, "cbpol_d_series")}
    comp = texts[("US", "CBPOL_SERIES|attr=COMPILATION")]
    assert comp["value"] is None and "mid-point of the Federal Reserve target rate" in comp["value_text"]
    assert comp["period_start"] == date(1954, 7, 1)


def test_bis_daily_eer_drops_holiday_values_and_takes_group_units(built: dict) -> None:
    rows = _rows(built, bis, "eer_d")
    assert {r["indicator"] for r in rows} <= {"EER|eer_type=N|eer_basket=B", "EER|eer_type=N|eer_basket=N"}
    assert all(r["unit"] == "Index, 2020 = 100" for r in rows)  # from the partial-key attribute rows
    assert all(date.weekday(r["period_start"]) < 5 for r in rows)
    rep = built[bis][1]["eer_d"]
    assert rep["dropped"].get("status_H", 0) > 0 and rep["attribute_rows"] > 0
    xm = next(r for r in rows if r["provider_geo"] == "XM")
    assert _flags(xm)["collection"] == "Average of observations through period"


# --------------------------------------------------------------------------- US Treasury
def test_treasury_par_and_real_yields(built: dict) -> None:
    par = _rows(built, ust, "par_yield")
    jan90 = {r["indicator"]: r for r in par if r["period_start"] == date(1990, 1, 2)}
    assert jan90["UST_PAR|tenor=10 Yr"]["value"] == "7.94" and jan90["UST_PAR|tenor=3 Mo"]["unit"] == "percent"
    assert _flags(jan90["UST_PAR|tenor=10 Yr"])["curve_method"] == "quasi-cubic hermite spline"
    recent = [r for r in par if r["period_start"] >= date(2025, 1, 1)]
    assert recent and all(_flags(r)["curve_method"] == "monotone convex" for r in recent)
    # the 1.5-month tenor had no value on 2025-01-02: empty cell, no row
    assert not [r for r in par if r["period_start"] == date(2025, 1, 2) and r["indicator"].endswith("1.5 Month")]
    real = _rows(built, ust, "real_yield")
    assert {r["indicator"] for r in real} >= {"UST_REAL_PAR|tenor=5 YR", "UST_REAL_PAR|tenor=10 YR"}


def test_treasury_live_years_cover_the_history() -> None:
    class Live:
        origin = "live"

    keys = {f.key for f in ust.files_for(Live())}
    assert "par_yield_1990" in keys and "real_yield_2003" in keys and "par_yield_1989" not in keys
    assert f"par_yield_{datetime.now(UTC).year}" in keys


# --------------------------------------------------------------------------- EIA
def test_eia_spot_prices_days_and_licence_review(built: dict) -> None:
    rows = _rows(built, eia, "spot_d")
    wti = {r["period_start"]: r for r in rows if r["indicator"] == "EIA_SPOT|series=RWTC"}
    assert wti[date(2020, 4, 20)]["value"] == "-36.98"  # negative close kept as printed
    assert (
        wti[date(2020, 4, 20)]["provider_geo"] == "Cushing, OK"
        and wti[date(2020, 4, 20)]["unit"] == "Dollars per Barrel"
    )
    assert json.loads(wti[date(2020, 4, 20)]["selector"])["column"] == "Mon"
    hh = [r for r in rows if r["indicator"] == "EIA_SPOT|series=RNGWHHD"]
    assert hh and hh[0]["unit"] == "Dollars per Million Btu" and hh[0]["provider_geo"] == "Henry Hub"
    assert all(_flags(r)["licence_review"] == eia.LICENCE_REVIEW for r in rows)
    assert all(_flags(r)["release_date"] == "2026-10-15" for r in rows)
    assert all(r["period_start"].weekday() < 5 for r in rows)


def test_eia_week_layout_is_checked() -> None:
    bad = ("<td colspan='6' class='Title1'>Cushing, OK WTI Spot Price FOB  (Dollars per Barrel)</td>"
           "<th>Week Of</th><th>Mon&nbsp;</th><th>Tue&nbsp;</th><th>Wed&nbsp;</th><th>Thu&nbsp;</th><th>Fri&nbsp;</th>"
           "<tr><td class='B6'>&nbsp;&nbsp;1987 May-19 to May-23</td>" + "<td class='B3'>1</td>" * 5 + "</tr>"
           "Release Date: 10/15/2026")  # fmt: skip
    f = hc.Fetched("hist_RWTC", "u", bad.encode("latin-1"), "0" * 64, datetime.now(UTC))
    with pytest.raises(hc.HfError, match="Monday"):
        eia.parse_page(eia.SERIES[0], f, hc.DailyRows("eia", "spot_d"), {})


# --------------------------------------------------------------------------- Bank of England
def test_boe_bank_rate_and_sonia(built: dict) -> None:
    rows = _rows(built, boe, "rates_d")
    br = {r["period_start"]: r for r in rows if r["indicator"] == "IUDBEDR"}
    assert (
        br[date(1975, 1, 2)]["value"] == "11.5" and _flags(br[date(1975, 1, 2)])["series_title"] == "Official Bank Rate"
    )
    sonia = [r for r in rows if r["indicator"] == "IUDSOIA"]
    assert sonia and all("Open Government Licence v3.0" in _flags(r)["attribution"] for r in sonia)
    assert all(_flags(r)["pub_lag"] == "second_business_day" for r in sonia)
    assert "attribution" not in _flags(next(iter(br.values())))


# --------------------------------------------------------------------------- New York Fed
def test_nyfed_rates_revisions_and_not_available(built: dict) -> None:
    rows = _rows(built, nyfed, "rates_d")
    revised = [r for r in rows if _flags(r).get("revision_indicator") == "r"]
    assert revised  # the API's same-day revision mark is kept on the value
    assert all("Terms of Use posted at newyorkfed.org" in _flags(r)["required_notice"] for r in rows)
    sofr = [r for r in rows if r["indicator"].startswith("SOFR|")]
    assert sofr and all("DTCC" in _flags(r)["data_licence_note"] for r in sofr)
    rep = built[nyfed][1]["rates"]["SOFR"]
    assert sum(rep["not_available"].values()) > 0  # "NA" percentiles are no row
    vol = next(r for r in rows if r["indicator"] == "SOFR|field=volumeInBillions")
    assert vol["unit"] == "billions of US dollars" and "." not in vol["value"]
