"""IMF atlas connector (agent A-IMF): offline, on captured live fixtures (IMF SDMX API, 2026-10-15).

Fixtures: ``tests/fixtures/source/atlas.imf`` -- WEO October 2026 vintage, BOP, GFS_SOO, ER, MFS_IR,
IL for ten IMF codes (FRA DZA IND EGY KOS WBG USA BRA G001 TWN) from 2018, the IMTS country
codelist and IMTS bilateral trade of FRA and DZA (2022-2023).
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from reva.adapters.connectors import atlas_imf as m
from reva.atlas.catalog import load_catalog
from reva.atlas.ingest import AtlasRun
from reva.atlas.tables import country_iso3, validate_atlas_frame
from reva.evidence.runner import FixtureHttp
from reva.kernel.errors import SourceResponseError

FIX_COUNTRIES = m.FIXTURE_COUNTRIES
FIX_START = m.FIXTURE_START_YEAR
SHA = "a" * 64
CATALOG = Path(__file__).resolve().parents[2] / "registry" / "atlas" / "catalog"


@pytest.fixture(scope="module")
def raws() -> dict[str, bytes]:
    http = AtlasRun.offline(m.atlas_source(), FixtureHttp(m.CONNECTOR_NAME))

    async def go() -> dict[str, bytes]:
        out = {q.dataset: (await m.fetch(q.fetch_item(), http)).body for q in m.flow_queries(FIX_COUNTRIES, FIX_START)}
        out["codelist"] = (await m.fetch(m.imts_codelist_item(), http)).body
        (q,) = m.imts_queries(("FRA", "DZA"), 2022, 2023)
        out["imts"] = (await m.fetch(q.fetch_item(), http)).body
        return out

    return asyncio.run(go())


def _validated(frame: pl.DataFrame, dataset: str) -> tuple[pl.DataFrame, list[dict]]:
    return validate_atlas_frame(frame, source="imf", dataset=dataset, raw_sha256s=[SHA], geo_scheme=m.GEO_SCHEME)


@pytest.fixture(scope="module")
def weo(raws):
    frame, st = m.parse_weo(m.parse_sdmx(raws["weo"]), SHA)
    v, unmapped = _validated(frame, "weo")
    return v, st, unmapped


def _cells(df: pl.DataFrame, geo: str, indicator: str) -> dict[int, dict]:
    rows = df.filter((pl.col("provider_geo") == geo) & (pl.col("indicator") == indicator)).to_dicts()
    return {r["period_start"].year: {**r, "flags": json.loads(r["flags"])} for r in rows}


# --------------------------------------------------------------------------- WEO
def test_weo_vintage_is_labelled(weo):
    _, st, _ = weo
    v = st.notes["vintage"]
    assert v["vintage"] == "2026-10" and v["vintage_label"] == "WEO October 2026"
    assert v["flow"] == "IMF.RES:WEO(9.0.0)" and v["publication_date"].startswith("2026-10-13")


def test_weo_values_are_full_units_as_reported(weo):
    df, _, _ = weo
    fra = _cells(df, "FRA", "NGDPD")
    assert fra[2024]["value"] == "3177842806000"  # US dollars, not billions
    assert fra[2024]["unit"] == "US dollars (current prices)"
    assert fra[2024]["flags"]["display_scale"] == "9"
    assert fra[2024]["iso3"] == "FRA"
    lp = _cells(df, "FRA", "LP")
    assert lp[2024]["unit"] == "persons" and int(lp[2024]["value"]) > 60_000_000


def test_weo_projections_flagged_after_latest_actual(weo):
    df, st, _ = weo
    ca = _cells(df, "FRA", "BCA_NGDPD")  # current account balance, % of GDP
    assert ca[2025]["flags"]["latest_actual"] == "2025"
    assert "estimate_kind" not in ca[2025]["flags"]
    for y in range(2026, 2032):
        assert ca[y]["flags"]["estimate_kind"] == "projection"
    assert ca[2024]["unit"] == "% of GDP"
    assert st.projection_rows > 0
    # every row after its declared boundary is a projection, none before
    for r in df.select("period_start", "flags").to_dicts():
        f = json.loads(r["flags"])
        la = f.get("latest_actual")
        if la and la.isdigit():
            assert (f.get("estimate_kind") == "projection") == (r["period_start"].year > int(la))


def test_weo_fiscal_year_boundaries_follow_the_series_notes(weo):
    df, _, _ = weo
    # Egypt: FY2024/25 latest actual, notes "FY(t-1/t) = CY(t)" -> WEO year 2025 is actual
    egy = _cells(df, "EGY", "NGDP_RPCH")
    assert egy[2025]["flags"]["latest_actual"] == "FY2024/25"
    assert egy[2025]["flags"]["boundary_rule"] == "fiscal_year_mapped"
    assert "estimate_kind" not in egy[2025]["flags"] and egy[2026]["flags"]["estimate_kind"] == "projection"
    # India: FY2025/26 latest actual, notes "FY(t/t+1) = CY(t)" -> 2025 actual, 2026 projection
    ind = _cells(df, "IND", "NGDPD")
    assert ind[2025]["flags"]["latest_actual"] == "FY2025/26"
    assert "estimate_kind" not in ind[2025]["flags"] and ind[2026]["flags"]["estimate_kind"] == "projection"
    assert ind[2025]["flags"]["reporting_year"] == "April/March"


def test_weo_series_without_declared_boundary_are_never_presented_as_actuals(weo):
    df, _, _ = weo
    ppp = _cells(df, "FRA", "PPPGDP")  # WEO declares no latest actual for PPP series
    assert ppp[2025]["flags"]["estimate_kind"] == "unknown_boundary"
    assert ppp[2026]["flags"]["estimate_kind"] == "projection"
    assert ppp[2025]["flags"]["boundary_rule"] == "not_declared_ngdp_inherited"
    # no WEO row is left without either a declared latest actual or an estimate_kind
    for f in map(json.loads, df["flags"].to_list()):
        assert f.get("latest_actual") or f.get("estimate_kind") in ("projection", "unknown_boundary")


def test_weo_geography_mapping(weo):
    df, _, unmapped = weo
    assert _cells(df, "KOS", "LP")[2024]["iso3"] == "XKX"
    assert _cells(df, "WBG", "NGDPD")[2024]["iso3"] == "PSE"
    assert {u["provider_geo"] for u in unmapped} == {"G001"}  # kept, listed, never dropped
    assert _cells(df, "TWN", "NGDPD")[2024]["iso3"] == "TWN"  # crosswalk 1.1.0 (ISO 3166-1)
    world = _cells(df, "G001", "NGDP_RPCH")
    assert world and all(r["iso3"] is None for r in world.values())
    # aggregates declare no latest actual: from the publication year on they are projections
    assert world[2026]["flags"]["estimate_kind"] == "projection"
    assert world[2025]["flags"]["estimate_kind"] == "unknown_boundary"


def test_weo_boundary_rules():
    assert m.weo_boundary("2025", None) == (2025, "declared")
    assert m.weo_boundary("FY2024/25", "Fiscal year data are mapped ... FY(t-1/t) = CY(t).") == (
        2025,
        "fiscal_year_mapped",
    )
    assert m.weo_boundary("FY2025/26", "mapped as follows: FY(t/t+1) = CY(t). The base") == (2025, "fiscal_year_mapped")
    assert m.weo_boundary("FY2024/25", "no mapping stated") == (2024, "fiscal_year_conservative")
    assert m.weo_boundary(None, None) == (None, "not_declared")
    assert m.weo_boundary("Not applicable", None) == (None, "unparsed")


# --------------------------------------------------------------------------- BOP / GFS / ex-IFS
def test_bop_goods_and_current_account_balances(raws):
    frame, _st = m.parse_bop(m.parse_sdmx(raws["bop"]), SHA)
    df, unmapped = _validated(frame, "bop")
    assert unmapped == []
    inds = set(df["indicator"].unique().to_list())
    assert {"CAB|entry=NETCD_T", "G|entry=NETCD_T", "S|entry=NETCD_T", "GS|entry=CD_T", "GS|entry=DB_T"} <= inds
    assert set(df["unit"].unique().to_list()) == {"US dollars"}
    g = _cells(df, "FRA", "G|entry=NETCD_T")
    assert g and float(g[2023]["value"]) < 0  # France: merchandise trade deficit (BOP basis)
    assert g[2023]["flags"]["derivation_type"] == "O" and g[2023]["flags"]["methodology"] == "BPM6"
    # net = credit - debit as reported (identity holds to the provider's precision)
    cd, db = _cells(df, "FRA", "G|entry=CD_T"), _cells(df, "FRA", "G|entry=DB_T")
    gap = float(cd[2023]["value"]) - float(db[2023]["value"]) - float(g[2023]["value"])
    assert abs(gap) < 1e-6 * float(cd[2023]["value"])  # provider rounding only (~1e5 USD on 6.8e11)
    assert _cells(df, "WBG", "CAB|entry=NETCD_T")[2023]["iso3"] == "PSE"


def test_gfs_headlines(raws):
    frame, st = m.parse_gfs(m.parse_sdmx(raws["gfs_soo"]), SHA)
    df, _ = _validated(frame, "gfs_soo")
    assert set(df["unit"].unique().to_list()) == {"% of GDP"}
    nl = _cells(df, "FRA", "GNLB_T|sector=S13")
    assert nl and float(nl[2023]["value"]) < 0 and nl[2023]["flags"]["recording_basis"] == "AC"
    assert st.skipped_no_value > 0  # series rows without OBS_VALUE produce no cell (no invented zero)


def test_ex_ifs_exchange_rates_interest_rates_reserves(raws):
    er, _ = _validated(m.parse_er(m.parse_sdmx(raws["er"]), SHA)[0], "er")
    dz = _cells(er, "DZA", "XDC_USD|PA_RT")
    assert dz and 100 < float(dz[2023]["value"]) < 200  # Algerian dinar per USD
    assert dz[2023]["unit"] == "national currency per US dollar"
    assert _cells(er, "USA", "XDC_USD|PA_RT")[2023]["value"] == "1"
    ir, _ = _validated(m.parse_ir(m.parse_sdmx(raws["mfs_ir"]), SHA)[0], "mfs_ir")
    assert _cells(ir, "USA", "MFS166_RT_PT_A_PT")  # policy-related rate
    assert set(ir["unit"].unique().to_list()) == {"% per annum"}
    il, unmapped = _validated(m.parse_il(m.parse_sdmx(raws["il"]), SHA)[0], "il")
    fr = _cells(il, "FRA", "TRGMV_REVS|unit=USD")
    assert fr and float(fr[2024]["value"]) > 1e11 and fr[2024]["flags"]["ifs_series"] == "true"
    assert {"G001"} >= {u["provider_geo"] for u in unmapped}


# --------------------------------------------------------------------------- IMTS (ex-DOTS)
def test_imts_bilateral_partners_mapped(raws):
    names = m.parse_codelist(raws["codelist"])
    assert names["G001"] == "World" and names["WBG"] == "West Bank and Gaza"
    reporters = m.imts_reporters(names)
    assert "FRA" in reporters and "G001" not in reporters and "EUR" not in reporters
    frame, st = m.parse_imts(m.parse_sdmx(raws["imts"]), SHA, "imts:DZA-FRA", names)
    df, _ = _validated(frame, "imts")
    assert set(df["provider_geo"].unique().to_list()) == {"DZA", "FRA"}
    x = _cells(df, "FRA", "XG_FOB_USD|partner=DEU")
    assert x[2023]["flags"]["partner_iso3"] == "DEU" and x[2023]["flags"]["flow_valuation"] == "FOB"
    assert x[2023]["value"] == "88443156585"
    w = _cells(df, "DZA", "XG_FOB_USD|partner=G001")
    assert w[2023]["flags"]["partner_unmapped"] is True and "partner_iso3" not in w[2023]["flags"]
    assert w[2023]["concept_id"] == "imf.goods_exports_world_usd"
    assert x[2023]["concept_id"] is None  # bilateral rows carry no catalog hint
    assert "G001" in st.notes["unmapped_partners"]
    assert st.notes["unmapped_partner_names"]["G001"] == "World"
    # partner codes in the indicator are verbatim IMF codes; mapped ones agree with the crosswalk
    for f in map(json.loads, df["flags"].unique().to_list()):
        if f.get("partner_iso3"):
            assert country_iso3(f["partner"], "imf") == f["partner_iso3"]


def test_imts_query_chunks():
    qs = m.imts_queries([f"A{i:02d}" for i in range(25)], 2000, None, chunk=12)
    assert len(qs) == 3 and qs[0].key.startswith("A00+A01")
    assert "startPeriod=2000" in qs[0].url() and "endPeriod" not in qs[0].url()
    assert len({q.item_key for q in qs}) == 3


# --------------------------------------------------------------------------- parser safety
def test_dtd_and_entities_are_refused():
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>'
    with pytest.raises(m.SdmxError):
        m.parse_sdmx(bomb)
    with pytest.raises(m.SdmxError):
        m.parse_sdmx(b'{"error": "not xml"}')
    with pytest.raises(m.SdmxError):  # an SDMX error/structure message without a DataSet
        m.parse_sdmx(b"<message:Error xmlns:message='urn:x'><x/></message:Error>")


def _msg(series_xml: str, ds_attrs: str = 'PUBLICATION_DATE="2026-10-13T02:00:00Z"') -> bytes:
    return (
        '<?xml version="1.0"?><m:StructureSpecificData xmlns:m="urn:m" xmlns:c="urn:c">'
        '<m:Header><m:Structure><c:StructureUsage><Ref agencyID="IMF.STA" id="BOP" version="1.0.0"/>'
        f"</c:StructureUsage></m:Structure></m:Header><m:DataSet {ds_attrs}>{series_xml}</m:DataSet>"
        "</m:StructureSpecificData>"
    ).encode()


def test_unexpected_units_and_duplicates_block():
    s = '<Series COUNTRY="FRA" BOP_ACCOUNTING_ENTRY="CD_T" INDICATOR="G" UNIT="EUR" FREQUENCY="A"><Obs TIME_PERIOD="2020" OBS_VALUE="1"/></Series>'
    with pytest.raises(m.SdmxError, match="UNIT"):
        m.parse_bop(m.parse_sdmx(_msg(s)), SHA)
    dup = '<Series COUNTRY="FRA" INDICATOR="MFS166_RT_PT_A_PT" FREQUENCY="A"><Obs TIME_PERIOD="2020" OBS_VALUE="1"/><Obs TIME_PERIOD="2020" OBS_VALUE="2"/></Series>'
    with pytest.raises(m.SdmxError, match="duplicate"):
        m.parse_ir(m.parse_sdmx(_msg(dup)), SHA)


def test_missing_and_nonfinite_values_produce_no_cell():
    s = (
        '<Series COUNTRY="FRA" INDICATOR="MFS166_RT_PT_A_PT" FREQUENCY="A" SCALE="0">'
        '<Obs TIME_PERIOD="2019"/><Obs TIME_PERIOD="2020" OBS_VALUE="NaN"/>'
        '<Obs TIME_PERIOD="2021" OBS_VALUE="1.2500"/><Obs TIME_PERIOD="2022" OBS_VALUE="-0.0" STATUS="F"/>'
        '<Obs TIME_PERIOD="2023-Q1" OBS_VALUE="3"/></Series>'
        '<Series COUNTRY="FRA" INDICATOR="DISR_RT_PT_A_PT" FREQUENCY="M"><Obs TIME_PERIOD="2021-01" OBS_VALUE="1"/></Series>'
    )
    frame, st = m.parse_ir(m.parse_sdmx(_msg(s)), SHA)
    assert frame["value"].to_list() == ["1.25", "0"]
    assert st.skipped_no_value == 2 and st.skipped_non_annual == 2
    f = json.loads(frame["flags"][1])
    assert f["estimate_kind"] == "projection" and f["provider_status"] == "F"
    assert frame["period_start"][0] == date(2021, 1, 1) and frame["period_end"][0] == date(2022, 1, 1)


def test_decimal_text():
    assert m.decimal_text("50277985062.13087") == "50277985062.13087"
    assert m.decimal_text("1E+3") == "1000"
    assert m.decimal_text("-0.0") == "0"
    assert m.decimal_text("2.500") == "2.5"
    for bad in (None, "", "NaN", "inf", "1,000", "abc"):
        assert m.decimal_text(bad) is None


def test_weo_without_publication_date_cannot_be_labelled():
    with pytest.raises(m.SdmxError, match="vintage"):
        m.parse_weo(m.parse_sdmx(_msg("", ds_attrs='LANGUAGE="EN"')), SHA)


# --------------------------------------------------------------------------- catalog / spec / fixtures
def test_catalog_matches_tables(raws):
    cat = load_catalog(CATALOG, strict=False)
    imf = {cid: c for cid, c in cat.concepts.items() if c.file == "imf.yaml"}
    assert len(imf) >= 60 and not [e for e in cat.errors if e["file"] == "imf.yaml"]
    assert all(c.domain == "imf" for c in imf.values())
    frames = {
        "weo": m.parse_weo(m.parse_sdmx(raws["weo"]), SHA)[0],
        "bop": m.parse_bop(m.parse_sdmx(raws["bop"]), SHA)[0],
        "gfs_soo": m.parse_gfs(m.parse_sdmx(raws["gfs_soo"]), SHA)[0],
        "er": m.parse_er(m.parse_sdmx(raws["er"]), SHA)[0],
        "mfs_ir": m.parse_ir(m.parse_sdmx(raws["mfs_ir"]), SHA)[0],
        "il": m.parse_il(m.parse_sdmx(raws["il"]), SHA)[0],
        "imts": m.parse_imts(m.parse_sdmx(raws["imts"]), SHA, "imts:x")[0],
    }
    hints = set().union(*(set(f["concept_id"].drop_nulls().unique().to_list()) for f in frames.values()))
    assert hints <= set(imf), sorted(hints - set(imf))
    for c in imf.values():
        for s in c.source_order:
            assert s.source == "imf" and s.dataset in m.DATASETS
            if s.dataset in frames and s.dataset not in ("mfs_ir", "il"):  # fixture subset lacks some rates
                assert s.indicator in set(frames[s.dataset]["indicator"].unique().to_list()), s.key
        # a hinted row's concept lists that row's series
    for ds, f in frames.items():
        for cid, ind in f.select("concept_id", "indicator").unique().drop_nulls().iter_rows():
            assert f"imf.{ds}.{ind}" in {s.key for s in imf[cid].source_order}


def test_spec_is_an_atlas_source_spec():
    from reva.atlas.sources import atlas_sources

    spec = m.atlas_source()
    assert spec.template == "atlas_bulk" and spec.allowed_hosts == ("api.imf.org",)
    assert spec.source_key == "imf.sdmx" and spec.budget_key == "imf.sdmx" and spec.parser_version == m.PARSER_VERSION
    assert spec.code_digest.startswith("sha256:") and spec.datasets == m.DATASETS
    assert atlas_sources()[m.CONNECTOR_NAME].module == m.__name__  # discovered by the atlas platform
    src = Path(m.__file__).read_text()
    for forbidden in ("SafeHttpClient(", "make_live_client(", "httpx.", "model_copy(", "ConnectorSpec"):
        assert forbidden not in src


def test_fixture_manifest_is_complete_and_offline():
    man = json.loads(
        (Path(__file__).resolve().parents[1] / "fixtures" / "source" / m.CONNECTOR_NAME / "manifest.json").read_text()
    )
    assert {e["item"] for e in man} == {"weo", "bop", "gfs_soo", "er", "mfs_ir", "il", "imts:codelist", "imts:DZA-FRA"}
    assert all(e["url"].startswith("https://api.imf.org/") for e in man)
    run = AtlasRun.offline(m.atlas_source(), FixtureHttp(m.CONNECTOR_NAME))
    with pytest.raises(SourceResponseError):  # an unrecorded request never reaches the network
        asyncio.run(m.fetch(m.flow_queries(("ZZZ",))[0].fetch_item(), run))
