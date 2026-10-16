"""Offline tests of the A-QOL atlas connectors (UNDP HDR/MPI, WHR, OWID, WID).

Fixtures: tests/fixtures/source/atlas.{undp_hdr,whr,owid,wid}/ — excerpts of the live responses of
2026-10-15 (see each manifest's ``excerpt``); WID is served by byte range from a small rebuilt archive.
"""

from __future__ import annotations

import hashlib
import json
import zlib
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from reva.adapters.connectors import atlas_owid, atlas_undp, atlas_whr, atlas_wid
from reva.adapters.connectors.atlas_qol_common import NameIndex, decimal_text, fetch_all
from reva.adapters.http.safe_client import HttpResult
from reva.atlas.ingest import ArchiveRangeFixtureHttp, AtlasRun
from reva.atlas.tables import validate_atlas_frame
from reva.evidence.runner import FixtureHttp, _fixture_fingerprint
from reva.kernel.errors import SourceResponseError

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "source"


async def _builds(connector: Any, http: Any) -> dict[str, Any]:
    raws = await fetch_all(connector, AtlasRun.offline(connector.describe(), http))
    out = {}
    for b in connector.build(raws):
        f, unmapped = validate_atlas_frame(
            b.frame, source=b.source, dataset=b.dataset, raw_sha256s=b.raw_sha256s, geo_scheme=b.geo_scheme
        )
        out[f"{b.source}.{b.dataset}"] = (b, f, unmapped)
    return out


def _cell(f: pl.DataFrame, **kw: Any) -> dict[str, Any]:
    q = f
    for k, v in kw.items():
        q = q.filter(pl.col(k) == v)
    assert q.height == 1, (kw, q.height)
    return q.row(0, named=True)


# ----------------------------------------------------------------------------- common helpers
def test_decimal_text_is_lossless_and_strict() -> None:
    assert decimal_text("0.97") == "0.97"
    assert decimal_text(" 2.00117E-2 ") == "2.00117E-2"
    assert decimal_text(".5") == "0.5"
    assert decimal_text("-.5") == "-0.5"
    assert decimal_text("+7") == "7"
    assert decimal_text("007.10") == "7.10"
    assert decimal_text("12.") == "12"
    for bad in ("..", "", "NaN", "inf", "1,000", "e", "3 d", None):
        assert decimal_text(bad) is None


def test_name_index_uses_aliases_then_crosswalk_labels_only() -> None:
    ni = NameIndex({"DR Congo": "COD", "North Cyprus": None})
    assert ni.lookup("DR Congo") == ("COD", "alias")
    assert ni.lookup("North Cyprus") == (None, "alias_unmapped")
    assert ni.lookup("Bolivia") == ("BOL", "crosswalk")  # crosswalk worldbank_name
    assert ni.lookup("Atlantis") == (None, "unmapped")
    with pytest.raises(ValueError):
        NameIndex({"X": "ZZZ"})


def test_specs_are_atlas_sources_and_hosts_are_pinned() -> None:
    from reva.atlas.sources import atlas_sources

    mods = (atlas_undp, atlas_whr, atlas_owid, atlas_wid)
    specs = [m.atlas_source() for m in mods]
    assert [s.template for s in specs] == ["atlas_bulk"] * 4
    assert specs[0].allowed_hosts == ("hdr.undp.org",)
    assert specs[3].allowed_hosts == ("wid.world",)
    assert all(s.code_digest.startswith("sha256:") for s in specs)
    assert [s.budget_key for s in specs] == ["undp.hdr", "whr.figure21", "owid.compilations", "wid.bulk"]
    found = atlas_sources()
    for m, s in zip(mods, specs, strict=True):
        assert found[s.name].module == m.__name__  # discovered by the atlas platform
    common = Path(atlas_undp.__file__).with_name("atlas_qol_common.py")
    for f in (*(Path(m.__file__) for m in mods), common):  # fetch only through the run (metered)
        src = f.read_text()
        for forbidden in ("SafeHttpClient(", "make_live_client(", "httpx.", "model_copy(", "ConnectorSpec"):
            assert forbidden not in src, (f.name, forbidden)


# ----------------------------------------------------------------------------- UNDP
async def test_undp_composite_and_mpi_tables() -> None:
    out = await _builds(atlas_undp.UndpHdrConnector(), FixtureHttp("atlas.undp_hdr"))
    b, f, unmapped = out["undp.hdr25"]
    assert b.report["encoding"] == "cp1252"
    assert {u["provider_geo"] for u in unmapped} == {"ZZK.WORLD", "ZZJ.SSA"}  # aggregates kept, unmapped
    hdi = _cell(f, indicator="hdi", iso3="FRA", period_start=date(2023, 1, 1))
    assert hdi["unit"] == "index (0-1)" and hdi["period_end"] == date(2024, 1, 1)
    sel = json.loads(hdi["selector"])
    assert sel["column"] == "hdi_2023" and sel["file"] == atlas_undp.COMPOSITE_FILE
    # the selector resolves to the exact source text
    body = (FIX / "atlas.undp_hdr").glob("*.csv")
    text = next(body).read_bytes().decode("cp1252").splitlines()
    header = text[0].split(",")
    assert text[sel["row"] - 1].split(",")[header.index("hdi_2023")] == hdi["value"]
    # carried-forward years are flagged, never presented as new observations
    inc = _cell(f, indicator="ineq_inc", iso3="FRA", period_start=date(2023, 1, 1))
    assert json.loads(inc["flags"])["carried_forward_from"] == 2022
    assert "carried_forward_from" not in json.loads(
        _cell(f, indicator="ineq_inc", iso3="FRA", period_start=date(2021, 1, 1))["flags"]
    )
    mmr = _cell(f, indicator="mmr", iso3="DZA", period_start=date(2021, 1, 1))
    assert json.loads(mmr["flags"])["carried_forward_from"] == 2020
    # categories as text; Côte d'Ivoire decoded from cp1252 maps by ISO3
    assert _cell(f, indicator="hdicode", iso3="FRA")["value_text"] == "Very High"
    assert f.filter(pl.col("iso3") == "CIV").height > 0
    # no empty cell became a value
    assert f.filter(pl.col("value").is_null() & pl.col("value_text").is_null()).height == 0

    b2, m, unmapped2 = out["undp.gmpi2026"]
    # the fixture's HDR excerpt holds a few countries only: other MPI names map via crosswalk labels or stay
    # unmapped (kept, listed); the live run maps every name (docs/dev/v3_a_qol.md)
    assert b2.report["unparsed_surveys"] == [] and b2.report["name_mapping"]["provider_table"] > 0
    cod = m.filter(pl.col("provider_geo") == "Congo (Democratic Republic of the)")["iso3"].unique().to_list()
    assert cod == ["COD"]  # provider spelling resolved through the HDR25 (iso3, country) pairs
    assert all(u["provider_geo"] not in ("Afghanistan", "Algeria") for u in unmapped2)
    assert "Sub-Saharan Africa" in b2.report["aggregates_skipped"]
    afg = m.filter((pl.col("provider_geo") == "Afghanistan") & (pl.col("indicator") == "t1.mpi")).row(0, named=True)
    assert afg["iso3"] == "AFG" and afg["value"] == "0.3603053"
    assert (afg["period_start"], afg["period_end"]) == (date(2022, 1, 1), date(2024, 1, 1))
    fl = json.loads(afg["flags"])
    assert fl["survey_type"] == "M" and "d" in fl["footnotes"] and fl["iso3_via"] == "provider_table"
    mon = m.filter(
        (pl.col("provider_geo") == "Afghanistan") & (pl.col("indicator") == "t1.below_national_poverty_line_pct")
    ).row(0, named=True)
    assert (mon["period_start"], mon["period_end"]) == (date(2014, 1, 1), date(2026, 1, 1))
    assert "not given" in json.loads(mon["flags"])["period_note"]
    # '..' cells are omitted, not zero: Afghanistan's PPP $3.00 cell is '..'
    assert (
        m.filter(
            (pl.col("provider_geo") == "Afghanistan") & (pl.col("indicator") == "t1.below_ppp_3_usd_day_pct")
        ).height
        == 0
    )
    # Table 2 has several surveys per country (time series)
    assert m.filter((pl.col("iso3") == "DZA") & (pl.col("indicator") == "t2.mpi")).height >= 2


def test_undp_unknown_variable_fails_closed() -> None:
    cb = {"hdi": {"carried": {}}}
    body = b"iso3,country,hdicode,region,newindex_2023\r\nFRA,France,Very High,,1\r\n"
    with pytest.raises(SourceResponseError, match="without a declared unit"):
        atlas_undp.parse_composite(body, "a" * 64, cb)


# ----------------------------------------------------------------------------- WHR
async def test_whr_three_year_windows_aliases_and_licence_flag() -> None:
    out = await _builds(atlas_whr.WhrConnector(), FixtureHttp("atlas.whr"))
    b, f, unmapped = out["whr.whr26_fig21"]
    assert b.rights["licence_status"] == "free_download_no_explicit_licence"
    assert b.report["excluded_rows_unknown_window"] > 0  # 2011 rows (window not stated) excluded
    assert f["period_start"].min() == date(2010, 1, 1)  # Year 2012 -> 2010..2012
    fin = _cell(f, indicator="life_evaluation", iso3="FIN", period_start=date(2023, 1, 1))
    assert fin["value"] == "7.764" and fin["period_end"] == date(2026, 1, 1)
    assert json.loads(fin["flags"])["averaging_window_years"] == [2023, 2025]
    assert _cell(f, indicator="rank", iso3="FIN", period_start=date(2023, 1, 1))["value"] == "1"
    assert f.filter(pl.col("provider_geo") == "DR Congo")["iso3"].unique().to_list() == ["COD"]
    assert {"North Cyprus", "Somaliland Region"} == {u["provider_geo"] for u in unmapped}
    # crosswalk 1.1.0: WHR "Taiwan Province of China" -> TWN (alias); "China" stays CHN (validator: no clash)
    assert f.filter(pl.col("provider_geo") == "Taiwan Province of China")["iso3"].unique().to_list() == ["TWN"]
    assert f.filter(pl.col("provider_geo") == "China")["iso3"].unique().to_list() == ["CHN"]


# ----------------------------------------------------------------------------- OWID
async def test_owid_tables_keep_original_sources_and_exclude_pass_through_columns() -> None:
    out = await _builds(atlas_owid.OwidConnector(), FixtureHttp("atlas.owid"))
    assert set(out) == {"owid.energy", "owid.co2", "owid.g_ghg_by_sector", "owid.g_temperature_anomaly"}
    _b, energy, unm = out["owid.energy"]
    assert not energy.filter(pl.col("indicator").is_in(["population", "gdp"])).height
    cell = _cell(energy, indicator="renewables_share_elec", iso3="FRA", period_start=date(2023, 1, 1))
    src = json.loads(cell["flags"])["original_source"]
    assert "Ember" in src and cell["unit"] == "%"
    assert {"World", "Africa (EI)"} <= {u["provider_geo"] for u in unm}
    _b, co2, _u = out["owid.co2"]
    kos = co2.filter(pl.col("provider_geo") == "Kosovo")
    assert kos.height > 0 and kos["iso3"].unique().to_list() == ["XKX"]
    assert (
        "Global Carbon Budget"
        in json.loads(_cell(co2, indicator="co2", iso3="DZA", period_start=date(2022, 1, 1))["flags"])[
            "original_source"
        ]
    )
    bg, ghg, unm_g = out["owid.g_ghg_by_sector"]
    assert "OWID_WRL" in {u["provider_geo"] for u in unm_g}
    fl = json.loads(ghg.row(0, named=True)["flags"])
    assert fl["original_producers"] == ["Climate Watch"] and fl["owid_processing"] == "major"
    assert any("CC BY 4.0" in o for o in bg.rights["original_sources"])
    _b, temp, _u = out["owid.g_temperature_anomaly"]
    assert temp.filter(pl.col("provider_geo") == "OWID_KOS")["iso3"].unique().to_list() == ["XKX"]


async def test_owid_refuses_non_redistributable_columns() -> None:
    man = json.loads((FIX / "atlas.owid" / "manifest.json").read_text())
    entry = next(
        e
        for e in man
        if "/v1/indicators/" in e["url"]
        and json.loads((FIX / "atlas.owid" / e["file"]).read_bytes()).get("shortName") == "temperature_anomaly"
    )
    doc = json.loads((FIX / "atlas.owid" / entry["file"]).read_bytes())
    doc["nonRedistributable"] = True
    http = FixtureHttp("atlas.owid", overrides={_fixture_fingerprint(entry["url"]): json.dumps(doc).encode()})
    conn = atlas_owid.OwidConnector(charts={"annual-temperature-anomalies": ("g_temperature_anomaly", "test")})
    raws = await fetch_all(conn, AtlasRun.offline(conn.describe(), http))
    builds = {f"{b.source}.{b.dataset}" for b in conn.build(raws)}
    assert "owid.g_temperature_anomaly" not in builds and "owid.co2" in builds


# ----------------------------------------------------------------------------- WID (byte ranges)
class RangeFixtureHttp:
    """Serves byte ranges of the rebuilt fixture archive like the WID server (206 + Content-Range)."""

    origin = "test_fixture"

    def __init__(self, etag: str | None = None, corrupt: str | None = None) -> None:
        man = json.loads((FIX / "atlas.wid" / "manifest.json").read_text())
        self.url = man["url"]
        self.body = (FIX / "atlas.wid" / man["file"]).read_bytes()
        assert hashlib.sha256(self.body).hexdigest() == man["sha256"]
        self.etag = man["etag"]
        self.serve_etag = etag or self.etag
        self.corrupt = corrupt
        self.requests: list[dict[str, str]] = []

    async def get(
        self, url: str, *, headers: dict[str, str], max_bytes: int, accept_status: tuple[int, ...]
    ) -> HttpResult:
        assert url == self.url and accept_status == (206,)
        self.requests.append(dict(headers))
        if "If-Range" in headers and headers["If-Range"] != self.serve_etag:
            raise SourceResponseError("HTTP 200 from wid.world (If-Range mismatch: whole file)", retryable=False)
        spec = headers["Range"].removeprefix("bytes=")
        n = len(self.body)
        if spec.startswith("-"):
            a, b = max(0, n - int(spec[1:])), n - 1
        else:
            a, b = (int(x) for x in spec.split("-"))
            b = min(b, n - 1)
        chunk = bytearray(self.body[a : b + 1])
        if self.corrupt and self.corrupt.encode() in chunk[:80] and len(chunk) > 200:
            chunk[60] ^= 0xFF  # flip a byte inside the member's deflate stream
        assert len(chunk) <= max_bytes
        return HttpResult(
            url,
            url,
            206,
            {"content-range": f"bytes {a}-{b}/{n}", "etag": self.serve_etag, "content-type": "application/zip"},
            bytes(chunk),
            datetime(2026, 10, 15, tzinfo=UTC),
            0.0,
            (),
            1,
        )


async def test_wid_reads_only_needed_members_by_range() -> None:
    http = RangeFixtureHttp()
    out = await _builds(atlas_wid.WidConnector(), http)
    b, f, unmapped = out["wid.wid_key"]
    members = [r.get("Range") for r in http.requests]
    assert members[0] == f"bytes=-{atlas_wid.TAIL_BYTES}"
    assert all("If-Range" in r for r in http.requests[1:])
    fetched = set(f["provider_geo"].unique().to_list())
    assert fetched == {"FR", "KS", "DZ"} and unmapped == []
    # XK (alias excluded) is never fetched; TW maps to TWN since crosswalk 1.1.0 (ISO 3166-1): it is
    # fetched, and the fixture excerpt holds none of the selected series for it (no row, no zero).
    assert b.report["countries_fetched"] == 4 and b.report["countries_without_selected_series"] == ["TW"]
    assert set(f["indicator"].unique().to_list()) <= {f"{v}|{p}" for v, p in atlas_wid.SERIES}
    assert f.filter(pl.col("provider_geo") == "KS")["iso3"].unique().to_list() == ["XKX"]
    top10 = f.filter(
        (pl.col("iso3") == "FRA")
        & (pl.col("indicator") == "sptincj992|p90p100")
        & (pl.col("period_start") == date(2023, 1, 1))
    ).row(0, named=True)
    fl = json.loads(top10["flags"])
    assert top10["unit"].startswith("share") and "data_quality" in fl and fl["wid_method"]
    sel = json.loads(top10["selector"])
    assert sel["member"] == "WID_data_FR.csv" and sel["etag"] == http.etag
    avg = f.filter((pl.col("iso3") == "DZA") & (pl.col("indicator") == "anninci992|p0p100")).row(0, named=True)
    assert avg["unit"].startswith("DZD")
    # the platform's byte-range fixture client (used by `reva atlas ingest atlas.wid --fixture`) serves the same
    same = await _builds(atlas_wid.WidConnector(), ArchiveRangeFixtureHttp(atlas_wid.CONNECTOR_NAME))
    assert same["wid.wid_key"][1].equals(f)


async def test_wid_refuses_corrupt_member_and_changed_archive() -> None:
    with pytest.raises(SourceResponseError, match=r"CRC-32|inflate|deflate|Error"):
        await _builds(atlas_wid.WidConnector(countries=["FR"]), RangeFixtureHttp(corrupt="WID_data_FR.csv"))

    class Changing(RangeFixtureHttp):
        async def get(self, url: str, **kw: Any) -> HttpResult:  # archive replaced after the tail was read
            if len(self.requests) == 1:
                self.serve_etag = '"new"'
            return await super().get(url, **kw)

    with pytest.raises(SourceResponseError, match="If-Range"):
        await _builds(atlas_wid.WidConnector(countries=["FR"]), Changing())


def test_wid_zip_helpers_detect_bad_offsets() -> None:
    m = atlas_wid.Member("WID_data_FR.csv", 8, zlib.crc32(b"x"), 3, 1, 100, 15, 0)
    with pytest.raises(SourceResponseError, match="range starts"):
        atlas_wid.inflate_member(b"PK\x03\x04" + b"\0" * 60, 99, m)
    with pytest.raises(SourceResponseError, match="end of central directory"):
        atlas_wid.parse_central_directory(b"\0" * 100, 0)
