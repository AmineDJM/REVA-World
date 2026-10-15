"""A-TRADE atlas connectors, offline (rebuilt excerpts of real 2026-10-15 bytes + real BaTIS responses).

Fixtures: tests/fixtures/source/atlas.baci (excerpt zip of BACI_HS92_V202601.zip served by byte
range), atlas.unctad (UNCTADstat bulk 7z: LSCI whole, the others rebuilt excerpts of original lines),
atlas.oecd.batis (real SDMX-CSV responses restricted to six reference areas + the dataflow).
"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from reva.adapters.connectors import atlas_baci as baci
from reva.adapters.connectors import atlas_trade_batis as batis
from reva.adapters.connectors import atlas_trade_common as tc
from reva.adapters.connectors import atlas_unctad as unctad
from reva.atlas.catalog import load_catalog
from reva.atlas.ingest import ArchiveRangeFixtureHttp
from reva.atlas.sources import AtlasSourceSpec, atlas_sources
from reva.atlas.tables import country_iso3, validate_atlas_frame
from reva.evidence.runner import FIXTURE_ROOT, FixtureHttp
from reva.kernel.errors import SourceResponseError

BACI_DIR = FIXTURE_ROOT / "atlas.baci"


# --------------------------------------------------------------------------- helpers
def _baci_zip() -> bytes:
    man = json.loads((BACI_DIR / "manifest.json").read_text())
    return (BACI_DIR / man["file"]).read_bytes()


@pytest.fixture(scope="module")
def baci_tables() -> dict[str, pl.DataFrame]:
    """The connector's own path: tail by range -> central directory -> member ranges -> aggregates."""
    http = ArchiveRangeFixtureHttp(baci.CONNECTOR_NAME)

    async def go() -> list[baci.YearAgg]:
        tail = await http.get(baci.URL, headers={"Range": f"bytes=-{baci.TAIL_BYTES}"})
        start, _e, total = tc.parse_content_range(tail.headers["content-range"])
        members = {m.name: m for m in tc.central_directory(tail.body, start, total)}
        out = []
        for y in baci.FIXTURE_YEARS:
            m = members[baci.year_member_name(y)]
            r = await http.get(baci.URL, headers={"Range": f"bytes={m.offset}-{m.end}", "If-Range": http.etag})
            s, _, _ = tc.parse_content_range(r.headers["content-range"])
            blocks = tc.iter_csv_blocks(tc.iter_member(r.body, s, m), header=baci.CSV_HEADER, block=50_000)
            out.append(baci.aggregate_year(blocks, year=y, member=m.name, raw_sha256=f"{y:064d}"))
        return out

    aggs = asyncio.run(go())
    frames = baci.build_frames(aggs, baci.DATASETS)
    shas = [a.raw_sha256 for a in aggs]
    return {
        k: validate_atlas_frame(f, source="baci", dataset=k, raw_sha256s=shas, geo_scheme="baci")[0]
        for k, f in frames.items()
    }


def _raw_baci_rows(year: int) -> list[tuple[str, str, str, Decimal]]:
    with zipfile.ZipFile(io.BytesIO(_baci_zip())) as z:
        text = z.read(baci.year_member_name(year)).decode()
    out = []
    for line in text.splitlines()[1:]:
        _t, i, j, k, v, _q = line.split(",")
        out.append((i, j, k, Decimal(v).quantize(Decimal("0.001"))))
    return out


def _cell(frame: pl.DataFrame, geo: str, indicator: str, year: int) -> dict[str, Any]:
    rows = frame.filter(
        (pl.col("provider_geo") == geo)
        & (pl.col("indicator") == indicator)
        & (pl.col("period_start").dt.year() == year)
    ).to_dicts()
    assert len(rows) == 1, (geo, indicator, year, rows)
    return rows[0]


# --------------------------------------------------------------------------- common helpers
def test_7z_round_trip_and_refusals() -> None:
    content = b"a,b\n" + b"1,2\n" * 600_000  # > 2**21 bytes: 7z numbers of several bytes
    data = tc.write_7z_single("x.csv", content)
    assert tc.read_7z_single(data) == ("x.csv", content)
    with pytest.raises(SourceResponseError):
        tc.read_7z_single(b"PK" + data[2:])
    broken = bytearray(data)
    broken[40] ^= 0xFF  # inside the packed stream: data CRC (or LZMA) must fail
    with pytest.raises(SourceResponseError):
        tc.read_7z_single(bytes(broken))
    with pytest.raises(SourceResponseError):
        tc.read_7z_single(data, limit=1000)


def test_zip_member_crc_and_header_checks() -> None:
    body = _baci_zip()
    members = {m.name: m for m in tc.central_directory(body, 0, len(body))}
    m = members[baci.year_member_name(2024)]
    chunk = body[m.offset : m.end + 1]
    assert b"".join(tc.iter_member(chunk, m.offset, m)).startswith(b"t,i,j,k,v,q\n")
    bad = bytearray(chunk)
    bad[len(bad) // 2] ^= 0x55
    with pytest.raises(SourceResponseError):
        b"".join(tc.iter_member(bytes(bad), m.offset, m))
    with pytest.raises(SourceResponseError):
        list(tc.iter_member(chunk, m.offset + 1, m))
    with pytest.raises(SourceResponseError):
        list(tc.iter_csv_blocks(iter([b"a,b\n1,2\n"]), header=baci.CSV_HEADER))


def test_exact_decimal_helpers() -> None:
    df = pl.DataFrame({"v": ["122.03", "0.001", "5", "34.471000000000004", "3.3729999999999998"]})
    v, r = baci.thousandths(df["v"])
    assert v.to_list() == [122030, 1, 5000, 34471, 3373] and r.to_list() == [0, 0, 0, 1, 1]
    with pytest.raises(SourceResponseError):
        baci.thousandths(pl.Series("v", ["1.2345"]))
    out = pl.DataFrame({"n": [122030, -5, 0, 1000]}).select(tc.scaled_to_text_expr("n", 3).alias("t"))["t"].to_list()
    assert out == ["122.03", "-0.005", "0", "1"]
    assert tc.scaled_to_text(-1234500, 3) == "-1234.5"
    assert tc.quarter_period("2006Q04")[1].isoformat() == "2007-01-01"
    assert tc.strict_decimal("9.7E-05") == "9.7E-05" and tc.strict_decimal(" ") is None


# --------------------------------------------------------------------------- BACI
def test_baci_tables_are_exact_sums(baci_tables: dict[str, pl.DataFrame]) -> None:
    assert set(baci_tables) == {d for d in baci.DATASETS if d != "hs92_pair_section_1995_2015"}
    rows = _raw_baci_rows(2024)
    pair: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    sec: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    xs: dict[str, Decimal] = defaultdict(Decimal)
    ms: dict[str, Decimal] = defaultdict(Decimal)
    for i, j, k, v in rows:
        pair[i, j] += v
        sec[i, j, baci.CHAPTER_SECTION[k[:2]]] += v
        xs[i] += v
        ms[j] += v
    pt = baci_tables["hs92_pair_total"]
    for (i, j), v in list(pair.items())[:40]:
        assert Decimal(_cell(pt, i, f"X|partner={j}|section=TOTAL", 2024)["value"]) == v
    ps = baci_tables["hs92_pair_section_2016_2024"]
    for (i, j, s), v in list(sec.items())[:40]:
        assert Decimal(_cell(ps, i, f"X|partner={j}|section={s}", 2024)["value"]) == v
    c = baci_tables["hs92_country"]
    for code in ("251", "842", "156", "12"):
        assert Decimal(_cell(c, code, "X|section=TOTAL", 2024)["value"]) == xs[code]
        assert Decimal(_cell(c, code, "M|section=TOTAL", 2024)["value"]) == ms[code]
        assert Decimal(_cell(c, code, "BAL|section=TOTAL", 2024)["value"]) == xs[code] - ms[code]
    bal = _cell(pt, "251", "BAL|partner=842|section=TOTAL", 2024)
    assert Decimal(bal["value"]) == pair["251", "842"] - pair["842", "251"]
    assert json.loads(bal["flags"])["inputs"] == [
        "X|partner=842|section=TOTAL of 251",
        "X|partner=251|section=TOTAL of 842",
    ]


def test_baci_flags_geo_and_selectors(baci_tables: dict[str, pl.DataFrame]) -> None:
    pt = baci_tables["hs92_pair_total"]
    row = _cell(pt, "842", "X|partner=490|section=TOTAL", 2024)
    flags = json.loads(row["flags"])
    assert flags["partner_unmapped"] is True and flags["estimate"] == "cepii_reconciled"
    assert row["iso3"] == "USA"
    sel = json.loads(row["selector"])
    assert sel["member"] == "BACI_HS92_Y2024_V202601.csv" and sel["filter"] == {"i": "842", "j": "490"}
    assert (
        pt.filter(pl.col("provider_geo") == "490")["iso3"].null_count()
        == pt.filter(pl.col("provider_geo") == "490").height
    )
    hs2 = baci_tables["hs92_pair_hs2"]
    assert set(hs2["indicator"].str.extract(r"hs2=(\d\d)$").unique().to_list()) <= set(baci.HS2_CHAPTERS)
    # every value is a strict decimal; no zero-filled flows
    for f in baci_tables.values():
        assert f["value"].null_count() == 0
    assert country_iso3("16", "baci") == "ASM" and country_iso3("58", "baci") is None
    assert country_iso3("699", "baci") == "IND" and country_iso3("280", "baci") is None


def test_baci_sections_cover_every_chapter() -> None:
    chapters = {f"{c:02d}" for c in range(1, 98) if c != 77} | {"99"}
    assert set(baci.CHAPTER_SECTION) == chapters
    assert baci.CHAPTER_SECTION["27"] == "V" and baci.CHAPTER_SECTION["93"] == "XIX"
    assert baci.CHAPTER_SECTION["71"] == "XIV" and baci.CHAPTER_SECTION["85"] == "XVI"


# --------------------------------------------------------------------------- UNCTAD
@pytest.fixture(scope="module")
def unctad_tables() -> dict[str, pl.DataFrame]:
    http = FixtureHttp(unctad.CONNECTOR_NAME)

    async def fetch(url: str) -> bytes:
        return (await http.get(url)).body

    out = {}
    for s in unctad.SETS:
        body = asyncio.run(fetch(s.url))
        frame, _stats = unctad.parse_report(body, "d" * 64, s)
        out[s.dataset] = validate_atlas_frame(
            frame, source="unctad", dataset=s.dataset, raw_sha256s=["d" * 64], geo_scheme="unctad"
        )[0]
    return out


def test_unctad_tables(unctad_tables: dict[str, pl.DataFrame]) -> None:
    svc = unctad_tables["services_cat"]
    x = _cell(svc, "250", "usd_millions|flow=02|category=S", 2024)
    m = _cell(svc, "250", "usd_millions|flow=01|category=S", 2024)
    b = _cell(svc, "250", "usd_millions_balance|category=S", 2024)
    assert x["iso3"] == "FRA" and Decimal(b["value"]) == Decimal(x["value"]) - Decimal(m["value"])
    fb = json.loads(b["flags"])
    assert fb["derived"] == "exports_minus_imports" and fb["inputs"][0] == x["indicator"]
    assert json.loads(x["selector"])["file"] == "US_TradeServCatTotal.csv"
    # balances only for top-level categories
    cats = svc.filter(pl.col("indicator").str.starts_with("usd_millions_balance"))["indicator"].str.extract(
        r"category=(\w+)$"
    )
    assert set(cats.unique().to_list()) <= unctad.SERVICE_BALANCE_CATEGORIES
    assert svc.filter(pl.col("provider_geo") == "412")["iso3"].unique().to_list() == ["XKX"]
    assert svc.filter(pl.col("provider_geo") == "158")["iso3"].null_count() > 0
    merch = unctad_tables["merch_total"]
    assert merch.filter(pl.col("period_start").dt.year() <= 1950).height > 0
    fdi = unctad_tables["fdi"]
    assert fdi.filter(pl.col("indicator") == "pct_gdp|flow=08|direction=1")["unit"].unique().to_list() == ["% of GDP"]
    lsbci = unctad_tables["lsbci"]
    row = lsbci.filter((pl.col("provider_geo") == "250") & (pl.col("indicator") == "index|partner=840")).to_dicts()[0]
    assert json.loads(row["flags"]) == {"partner_iso3": "USA"}
    lsci = unctad_tables["lsci"]
    assert lsci.filter(pl.col("iso3") == "FRA")["period_start"].dt.month().unique().sort().to_list() == [1, 4, 7, 10]


def test_unctad_missing_cells_are_omitted() -> None:
    s = unctad.SETS_BY_DATASET["services_cat"]
    csv = (
        b"Year,Economy,Economy Label,Flow,Flow Label,Category,Category Label,Millions of US$ at current prices,"
        b"Millions of US$ at current prices Footnote,Millions of US$ at current prices Missing value\n"
        b'2024,250,"France",02,"Exports",S,"Services",10.5,Estimated,\n'
        b'2024,250,"France",01,"Imports",S,"Services",,,"Not publishable"\n'
        b'2024,840,"USA",02,"Exports",S,"Services",3,,\n'
        b'2024,840,"USA",01,"Imports",S,"Services",4.25,,\n'
    )
    frame, stats = unctad.parse_report(tc.write_7z_single(s.csv_name, csv), "e" * 64, s)
    assert stats["omitted_missing"]["usd_millions"] == {"Not publishable": 1}
    assert frame.filter(pl.col("provider_geo") == "250")["indicator"].to_list() == ["usd_millions|flow=02|category=S"]
    assert json.loads(frame.filter(pl.col("provider_geo") == "250")["flags"][0])["unctad_estimate"] is True
    usa = frame.filter(pl.col("indicator") == "usd_millions_balance|category=S")
    assert usa["value"].to_list() == ["-1.25"]
    with pytest.raises(SourceResponseError):
        unctad.parse_report(tc.write_7z_single("other.csv", csv), "e" * 64, s)


# --------------------------------------------------------------------------- BaTIS
@pytest.fixture(scope="module")
def batis_table() -> pl.DataFrame:
    http = FixtureHttp(batis.CONNECTOR_NAME)

    async def fetch(url: str) -> bytes:
        return (await http.get(url)).body

    edition = batis.parse_edition(asyncio.run(fetch(batis.DATAFLOW_URL)))
    frames = [
        batis.parse_year(asyncio.run(fetch(batis.data_url(y, batis.FIXTURE_REF_AREAS))), f"{y:064d}", y, edition)
        for y in batis.FIXTURE_YEARS
    ]
    return validate_atlas_frame(
        pl.concat(frames),
        source="oecd",
        dataset=batis.DATASET,
        raw_sha256s=[f"{y:064d}" for y in batis.FIXTURE_YEARS],
        geo_scheme="batis",
    )[0]


def test_batis_balanced_pairs(batis_table: pl.DataFrame) -> None:
    x = _cell(batis_table, "FRA", "X|partner=USA", 2024)
    back = _cell(batis_table, "USA", "X|partner=FRA", 2024)
    bal = _cell(batis_table, "FRA", "BAL|partner=USA", 2024)
    assert Decimal(bal["value"]) == Decimal(x["value"]) - Decimal(back["value"])
    f = json.loads(x["flags"])
    assert f["estimate"] == "oecd_wto_balanced" and f["batis_edition"] == "December2025"
    assert batis_table.filter(pl.col("provider_geo") == "XKV")["iso3"].unique().to_list() == ["XKX"]
    assert batis_table.filter(pl.col("indicator").str.starts_with("BAL|partner=W")).is_empty()  # no aggregate balances
    assert _cell(batis_table, "FRA", "X|partner=W", 2023)["iso3"] == "FRA"


# --------------------------------------------------------------------------- specs, catalog, invariants
def test_specs_are_discovered_and_budgeted_paths_only() -> None:
    names = set(atlas_sources())
    assert {baci.CONNECTOR_NAME, unctad.CONNECTOR_NAME, batis.CONNECTOR_NAME} <= names
    for mod in (baci, unctad, batis):
        spec = mod.atlas_source()
        assert isinstance(spec, AtlasSourceSpec) and spec.code_digest.startswith("sha256:")
        src = Path(mod.__file__).read_text()
        for bad in ("make_live_client(", "SafeHttpClient(", "httpx.", "urlopen("):
            assert bad not in src
    assert baci.atlas_source().planned_requests == 3 + len(baci.YEARS)
    assert baci.atlas_source().max_response_bytes >= 99_200_000


def test_catalog_series_exist(
    baci_tables: dict[str, pl.DataFrame], unctad_tables: dict[str, pl.DataFrame], batis_table: pl.DataFrame
) -> None:
    cat = load_catalog()
    trade = (
        [c for c in cat.concepts.values() if c.domain == "trade"]
        if isinstance(cat.concepts, dict)
        else [c for c in cat.concepts if c.domain == "trade"]
    )
    assert len(trade) > 90
    have: dict[str, set[str]] = {f"baci.{k}": set(v["indicator"].unique().to_list()) for k, v in baci_tables.items()}
    have |= {f"unctad.{k}": set(v["indicator"].unique().to_list()) for k, v in unctad_tables.items()}
    have[f"oecd.{batis.DATASET}"] = set(batis_table["indicator"].unique().to_list())
    for c in trade:
        assert c.provider_declared is True
        for s in c.source_order:
            key = f"{s.source}.{s.dataset}"
            assert key in have, (c.id, key)
            if s.source == "baci":  # the excerpt fixture does not hold every section
                kind, _, sec = s.indicator.partition("|section=")
                assert kind in ("X", "M", "BAL") and (sec in baci.SECTION_CODES or sec == "TOTAL"), s.indicator
            else:
                assert s.indicator in have[key], (c.id, s.key)
