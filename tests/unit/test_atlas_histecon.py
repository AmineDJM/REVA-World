"""A-HISTECON long-run history connectors (Maddison, Clio-Infra, Gapminder, COW): offline parse tests.

Fixtures under ``tests/fixtures/source/<connector>/`` are captured live responses, several of them
TRIMMED (rows subset; noted per file in the manifest) to keep the repository small.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, date, datetime
from typing import Any

import polars as pl
import pytest

from reva.adapters.connectors import atlas_clio, atlas_cow, atlas_gapminder, atlas_maddison
from reva.adapters.connectors.atlas_history_common import decimal_text, fetch_all, historical_qid, historical_qids
from reva.adapters.connectors.atlas_xlsx import Workbook, XlsxError, col_index, col_letters
from reva.atlas.tables import validate_atlas_frame
from reva.evidence.runner import FixtureHttp
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import FetchItem, RawAcquisition


async def _builds(connector: Any, name: str) -> tuple[list[Any], list[RawAcquisition]]:
    raws = await fetch_all(connector, FixtureHttp(name))
    return connector.build(raws), raws


def _valid(b: Any) -> tuple[pl.DataFrame, list[dict[str, Any]]]:
    return validate_atlas_frame(
        b.frame, source=b.source, dataset=b.dataset, raw_sha256s=b.raw_sha256s, geo_scheme=b.geo_scheme
    )


def _raw(key: str, body: bytes, url: str = "https://example.invalid/x", **params: Any) -> RawAcquisition:
    return RawAcquisition(
        item=FetchItem(key=key, url=url, max_bytes=10**8, params=params),
        body=body,
        media_type="application/octet-stream",
        retrieved_at=datetime(2026, 10, 15, tzinfo=UTC),
        response_metadata={},
        request_fingerprint="x",
        origin="test_fixture",
    )


# --------------------------------------------------------------------------- shared helpers
def test_decimal_text_is_lossless_only() -> None:
    assert decimal_text(".039697491") == "0.039697491"
    assert decimal_text(" 1407.5 ") == "1407.5"
    assert decimal_text("-0") == "0"
    assert decimal_text("1E-3") == "1E-3"
    for bad in ("", "NaN", "1,000", "inf", "12a", None):
        assert decimal_text(bad) is None


def test_xlsx_reader_refuses_dtd_and_garbage() -> None:
    with pytest.raises(XlsxError):
        Workbook(b"not a zip")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><workbook/>')
        z.writestr("xl/_rels/workbook.xml.rels", "<Relationships/>")
    with pytest.raises(XlsxError, match="DTD"):
        Workbook(buf.getvalue())
    assert col_index("A") == 0 and col_index("AA") == 26 and col_letters(26) == "AA"


def test_historical_qids_file_is_well_formed() -> None:
    entries = (
        historical_qids("maddison") + historical_qids("cow") + historical_qids("clio") + historical_qids("gapminder")
    )
    assert entries, "registry/entities/historical_states_qids.json must list the mapped historical states"
    for e in entries:
        assert e["qid"].startswith("Q") and e["review_status"] == "agent_mapped_pending_maintainer_review"
        assert e["evidence"] and e.get("wikidata_label_en")
    assert historical_qid("maddison", "SUN")["qid"] == "Q15180"
    assert historical_qid("cow", "365", 1950)["qid"] == "Q15180"
    assert historical_qid("cow", "365", 2000) is None  # Russian Federation period: a crosswalk country


# --------------------------------------------------------------------------- Maddison
async def test_maddison_fixture_parses_with_provider_notes() -> None:
    (b,), _ = await _builds(atlas_maddison.MaddisonConnector(min_rows=100), atlas_maddison.NAME)
    f, unmapped = _valid(b)
    fra = f.filter(pl.col("provider_geo") == "FRA")
    assert set(fra["iso3"].unique().to_list()) == {"FRA"}
    y1 = fra.filter((pl.col("period_start") == date(1, 1, 1)) & (pl.col("indicator") == "gdppc")).to_dicts()[0]
    assert y1["value"] == "956" and y1["period_end"] == date(2, 1, 1)
    flags = json.loads(y1["flags"])
    assert flags["mpd_cell_flag"] == "none_published" and flags["mpd_sparse_period"] is True
    assert flags["mpd_sources"][0]["source"].startswith("Scheidel")  # the provider's own source for year 1
    g22 = json.loads(
        fra.filter((pl.col("period_start") == date(2022, 1, 1)) & (pl.col("indicator") == "gdppc"))["flags"][0]
    )
    assert "Total Economy Database" in g22["mpd_general_note"]
    # historical / non-crosswalk codes kept, never dropped, with agent-mapped QIDs
    geos = {u["provider_geo"] for u in unmapped}
    assert {"SUN", "CSK", "TWN", "world"} <= geos
    sun = f.filter(pl.col("provider_geo") == "SUN")
    assert sun["iso3"].null_count() == sun.height and set(sun["entity_qid"].to_list()) == {"Q15180"}
    # empty cells are omitted, never zero-filled
    assert f.filter(pl.col("value") == "0").height == 0
    assert set(f["unit"].unique().to_list()) == set(atlas_maddison.UNITS.values())


def test_maddison_spans_and_blocks() -> None:
    assert atlas_maddison.span_years("1820, 1870") == [(1820, 1820, None), (1870, 1870, None)]
    assert atlas_maddison.span_years("1993 -") == [(1993, 9999, None)]
    assert atlas_maddison.span_years("1252–1700 (England)") == [(1252, 1700, "England")]
    assert atlas_maddison.span_years("1861-1938/9") == []
    rec = {"data": {"latestVersion": {"license": {"name": "CC-BY-NC-4.0"}, "files": []}}}
    with pytest.raises(SourceResponseError) as e:
        atlas_maddison.parse_record(json.dumps(rec).encode())
    assert e.value.details.get("kind") == "licence" or "licence" in str(e.value)


async def test_maddison_checksum_mismatch_blocks() -> None:
    raws = await fetch_all(atlas_maddison.MaddisonConnector(min_rows=100), FixtureHttp(atlas_maddison.NAME))
    rec, xl = sorted(raws, key=lambda r: r.item.key)
    tampered = xl.model_copy(update={"body": xl.body + b"\0"})
    with pytest.raises(SourceResponseError, match="SHA-1"):
        atlas_maddison.MaddisonConnector(min_rows=100).build([rec, tampered])


# --------------------------------------------------------------------------- Clio-Infra
async def test_clio_fixture_parses_polities_and_licences() -> None:
    (b,), _ = await _builds(atlas_clio.ClioConnector(min_datasets=1), atlas_clio.NAME)
    f, unmapped = _valid(b)
    rep = b.report
    assert {s["reason"] for s in rep["skipped"]} == {"restricted_on_dataverse", "layout_not_a_clio_data_sheet"}
    h = f.filter((pl.col("indicator") == "Height-historical") & (pl.col("iso3") == "FRA"))
    assert h.height > 10
    fl = json.loads(h["flags"][0])
    assert fl["licence"] == "CC0 1.0" and "birth-decade" in fl["provider_period_note"]
    assert fl["polity"]["ccode"] == "250" and fl["territory"].startswith("country_series")
    # CC BY-SA dataset rows say so
    im = f.filter(pl.col("indicator") == "Infant_mortality-historical")
    assert {json.loads(x)["licence"] for x in im["flags"].unique().to_list()} == {"CC BY-SA 4.0"}
    # historical-border polities are kept with iso3 null
    gea = f.filter(pl.col("provider_geo").str.starts_with("geacron/"))
    assert gea.height > 0 and gea["iso3"].null_count() == gea.height
    assert all(u["rows"] > 0 for u in unmapped)
    assert set(f["indicator"].unique().to_list()) == {
        "Height-historical",
        "Labourers_wage-historical",
        "Infant_mortality-historical",
        "Total_Population-historical",
    }


async def test_clio_unknown_licence_is_never_published() -> None:
    raws = await fetch_all(atlas_clio.ClioConnector(min_datasets=1), FixtureHttp(atlas_clio.NAME))
    out = []
    for r in raws:
        if r.item.key == "clio.record.doi:10.34894/CGMOIO":
            doc = json.loads(r.body)
            doc["data"]["latestVersion"]["license"] = {"name": "All rights reserved"}
            r = r.model_copy(update={"body": json.dumps(doc).encode()})
        out.append(r)
    (b,) = atlas_clio.ClioConnector(min_datasets=1).build(out)
    assert "Height-historical" not in set(b.frame["indicator"].to_list())
    assert any("licence" in s["reason"] for s in b.report["skipped"])


# --------------------------------------------------------------------------- Gapminder
async def test_gapminder_excludes_projections_and_flags_segments() -> None:
    (b,), _ = await _builds(atlas_gapminder.GapminderConnector(), atlas_gapminder.NAME)
    f, unmapped = _valid(b)
    last = {
        r["indicator"]: r["period_start"].year
        for r in f.group_by("indicator").agg(pl.col("period_start").max()).to_dicts()
    }
    assert last == {"pop": 2023, "lex": 2019, "gdp_pcap": 2024}
    rep = b.report["series"]
    assert all(s["excluded_projection_cells"] > 0 for s in rep.values())
    assert rep["lex"]["version_mismatch"] is True
    lex = json.loads(f.filter(pl.col("indicator") == "lex")["flags"][0])
    assert lex["documentation_version_mismatch"]["documented"] == "v14"
    fra1800 = json.loads(
        f.filter(
            (pl.col("provider_geo") == "fra")
            & (pl.col("indicator") == "pop")
            & (pl.col("period_start") == date(1800, 1, 1))
        )["flags"][0]
    )
    assert fra1800["composite_reconstruction"] is True and "Clio Infra" in fra1800["segment"]
    assert unmapped == [{"provider_geo": "twn", "rows": unmapped[0]["rows"]}]
    twn = f.filter(pl.col("provider_geo") == "twn")
    assert set(twn["entity_qid"].to_list()) == {"Q865"}


def test_gapminder_name_alias_requires_exact_provider_name() -> None:
    geo = {"kos": {"name": "Kosovo", "alpha3": ""}, "chanisl": {"name": "Channel Isles", "alpha3": ""}}
    assert atlas_gapminder.geo_iso3("kos", geo) == ("XKX", "agent_alias_exact_name")
    assert atlas_gapminder.geo_iso3("chanisl", geo) == (None, "no_iso_code")


# --------------------------------------------------------------------------- COW
async def test_cow_tables_units_and_code_resolution() -> None:
    c = atlas_cow.CowConnector()
    builds, _ = await _builds(c, atlas_cow.NAME)
    by = {b.dataset: _valid(b)[0] for b in builds}
    nmc = by["nmc7"]
    usa = nmc.filter((pl.col("provider_geo") == "2") & (pl.col("indicator") == "milex"))
    units = {r["period_start"].year: r["unit"] for r in usa.to_dicts()}
    assert units[1913] == atlas_cow.MILEX_UNIT_GBP and units[1914] == atlas_cow.MILEX_UNIT_USD
    assert set(usa["iso3"].unique().to_list()) == {"USA"}
    ussr = nmc.filter((pl.col("provider_geo") == "365") & (pl.col("period_start") == date(1950, 1, 1)))
    assert ussr["iso3"].null_count() == ussr.height  # KGGEOPOL policy: the USSR is not RUS
    assert set(ussr["entity_qid"].to_list()) == {"Q15180"}
    cinc = nmc.filter(pl.col("indicator") == "cinc")["value"].to_list()
    assert all(not v.startswith(".") for v in cinc)
    assert builds[0].report["nmc"]["missing_code_minus9"]  # -9 omitted and counted
    assert by["system2024"]["value"].unique().to_list() == ["1"]
    wars = by["wars4"]
    w1 = wars.filter(pl.col("indicator") == "batdeath|type=inter|war=1|side=2")
    assert w1["value"].to_list() == ["600"] and w1["iso3"].to_list() == ["ESP"]


async def test_cow_kg_membership_statements() -> None:
    c = atlas_cow.CowConnector()
    await _builds(c, atlas_cow.NAME)
    kg = c.kg
    assert kg is not None
    sts = kg["statements"]
    usa = [s for s in sts if s.subject_qid == "cow:2" and s.object_qid == "cow:system:interstate"]
    assert len(usa) == 1 and usa[0].start.iso == "1816-01-01" and usa[0].end is None
    assert usa[0].qualifiers["observed_until"] == "2024-12-31"
    fra = sorted(
        (
            s
            for s in sts
            if s.subject_qid == "cow:220" and s.property_pid == "P463" and s.object_qid == "cow:system:interstate"
        ),
        key=lambda s: s.start.iso,
    )
    assert [(s.start.iso, s.end.iso if s.end else None) for s in fra] == [
        ("1816-01-01", "1942-11-11"),
        ("1944-08-25", None),
    ]
    majors = [s for s in sts if s.object_qid == "cow:system:major_powers"]
    assert majors and all(s.property_pid == "P463" for s in majors)
    wars = [s for s in sts if s.property_pid == "P1344"]
    assert wars and all(s.object_qid.startswith("cow:war:") for s in wars)
    denotes = [s for s in sts if s.property_pid == "cow:code_denotes"]
    assert any(s.subject_qid == "cow:365" and s.object_qid == "Q15180" for s in denotes)
    ids = [s.statement_id for s in sts]
    assert len(ids) == len(set(ids))
    kinds = {e.kind for e in kg["entities"]}
    assert kinds == {"state_code", "international_system", "war"}


def test_cow_war_dates_precision() -> None:
    assert atlas_cow._war_date("1823", "4", "7") == ("1823-04-07", 11, "day")
    assert atlas_cow._war_date("1818", "-9", "-9") == ("1818-01-01", 9, "year")
    assert atlas_cow._war_date("1822", "11", "-9") == ("1822-11-01", 10, "month")
    assert atlas_cow._war_date("-7", "-7", "-7") == (None, None, "ongoing")
    assert atlas_cow._war_date("-8", "-8", "-8") == (None, None, "not_applicable")


def test_cow_zip_member_is_exact_and_bounded() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("States2024/statelist2024.csv", "a,b\n1,2\n")
        z.writestr("__MACOSX/States2024/._statelist2024.csv", "junk")
    assert atlas_cow.zip_member(buf.getvalue(), "States2024/statelist2024.csv") == b"a,b\n1,2\n"
    with pytest.raises(SourceResponseError):
        atlas_cow.zip_member(buf.getvalue(), "statelist2024.csv")


# --------------------------------------------------------------------------- HYDE
def test_inflate64_real_member_crc() -> None:
    import zlib
    from pathlib import Path

    from reva.adapters.connectors.atlas_inflate64 import InflateError, inflate

    d = Path(__file__).resolve().parents[1] / "fixtures" / "source" / "atlas.hyde.hyde321"
    meta = json.loads((d / "deflate64_rf_rice_r.json").read_text())
    out = inflate((d / "deflate64_rf_rice_r.bin").read_bytes(), deflate64=True)
    assert len(out) == meta["size"] and zlib.crc32(out) == meta["crc32"]
    with pytest.raises(InflateError):
        inflate((d / "deflate64_rf_rice_r.bin").read_bytes(), deflate64=True, max_out=1000)
    with pytest.raises(InflateError):
        inflate(b"\xff\xff\xff", deflate64=True)


async def test_hyde_tail_parse_and_bce_exclusion() -> None:
    from reva.adapters.connectors import atlas_hyde

    (b,), _ = await _builds(atlas_hyde.HydeConnector(tail_bytes=10_000_000), atlas_hyde.NAME)
    f, unmapped = _valid(b)
    assert {u["provider_geo"] for u in unmapped} == {"158", "891", "world"}
    fra = f.filter((pl.col("iso3") == "FRA") & (pl.col("indicator") == "popc"))
    assert fra["period_start"].min() == date(100, 1, 1)  # BCE steps and year 0 are not representable
    assert b.report["excluded_bce_or_year0_cells"] > 0 and b.report["bce_steps"][0] == -10000
    fl = json.loads(fra["flags"][0])
    assert fl["hyde_modelled"] is True and fl["licence"] == "CC BY 3.0" and "inhabitants" in fl["unit_note"]
    assert set(f["indicator"].unique().to_list()) == {v[0] for v in atlas_hyde.SUMMARIES.values()}
    twn = f.filter(pl.col("provider_geo") == "158")
    assert set(twn["entity_qid"].to_list()) == {"Q865"}


def test_hyde_readme_licence_clause_required() -> None:
    from reva.adapters.connectors import atlas_hyde

    assert (
        atlas_hyde.check_readme(b"The Creative Commons License (CC BY 3.0) applies to all of the HYDE data.")
        == "CC BY 3.0"
    )
    with pytest.raises(SourceResponseError):
        atlas_hyde.check_readme(b"All rights reserved.")
