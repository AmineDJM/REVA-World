"""A-GEO: GeoNames countryInfo.txt -> atlas table (captured real response, offline)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import polars as pl
import pytest

from reva.adapters.connectors import atlas_geonames as gn
from reva.atlas.tables import ATLAS_SCHEMA, validate_atlas_frame
from reva.evidence.runner import FixtureHttp
from reva.kernel.errors import SourceResponseError

T = datetime(2026, 10, 15, 22, 0, tzinfo=UTC)
SHA = "b" * 64


@pytest.fixture(scope="module")
def body() -> bytes:
    return asyncio.run(FixtureHttp(gn.CONNECTOR_NAME).get(gn.URL)).body


@pytest.fixture(scope="module")
def rows(body: bytes) -> list[gn.CountryInfo]:
    return gn.parse_country_info(body)


def _cell(rows: list[dict], geo: str, indicator: str) -> dict:
    (r,) = [r for r in rows if r["provider_geo"] == geo and r["indicator"] == indicator]
    return r


def test_parse_and_validate(rows: list[gn.CountryInfo]) -> None:
    assert len(rows) >= gn.MIN_ROWS
    assert gn.validate_rows(rows) == []
    fra = next(r for r in rows if r.iso3 == "FRA")
    assert fra.cells["Capital"] == "Paris" and fra.cells["Continent"] == "EU"


def test_atlas_rows_are_as_declared(rows: list[gn.CountryInfo]) -> None:
    out = gn.atlas_rows(rows, raw_sha256=SHA, retrieved_at=T)
    frame, unmapped = validate_atlas_frame(
        pl.DataFrame(out, schema=ATLAS_SCHEMA),
        source="geonames",
        dataset="countryinfo",
        raw_sha256s=[SHA],
        geo_scheme="iso3",
    )
    assert frame.height == len(out)
    # historical codes are kept, unmapped (never dropped)
    assert {u["provider_geo"] for u in unmapped} >= {"ANT", "SCG"}
    fra_area = _cell(out, "FRA", "area_km2")
    assert fra_area["value"] == next(r for r in rows if r.iso3 == "FRA").cells["Area(in sq km)"]
    assert json.loads(fra_area["flags"])["period_basis"].startswith("retrieval snapshot")
    assert json.loads(fra_area["selector"])["column"] == "Area(in sq km)"
    assert _cell(out, "XKX", "capital")["iso3"] == "XKX"
    # a declared zero area is kept as reported and flagged
    umi = _cell(out, "UMI", "area_km2")
    assert umi["value"] == "0" and json.loads(umi["flags"])["provider_zero_implausible"] is True
    # empty cells are omitted, never filled
    assert not [r for r in out if r["provider_geo"] == "ATA" and r["indicator"] == "capital"]
    assert all(r["period_end"] > r["period_start"] for r in out)


def test_neighbours_and_kg_statements(rows: list[gn.CountryInfo]) -> None:
    pairs, bad = gn.neighbour_pairs(rows)
    assert bad == []
    assert ("FRA", "ESP", next(r.line for r in rows if r.iso3 == "FRA")) in pairs
    sts, info = gn.neighbour_statements(
        rows, {"FRA": "Q142", "ESP": "Q29", "AND": "Q228"}, raw_sha256=SHA, retrieved_at=T
    )
    got = {(s.subject_qid, s.object_qid) for s in sts}
    assert {("Q142", "Q29"), ("Q29", "Q142"), ("Q228", "Q142")} <= got
    assert all(s.property_pid == "P47" and s.source_key == gn.SOURCE_KEY for s in sts)
    assert info["skipped_no_qid_or_unmapped"] > 0


def test_schema_drift_is_refused(body: bytes) -> None:
    drifted = body.replace(b"#ISO\tISO3\tISO-Numeric", b"#ISO\tISO3\tNumeric", 1)
    with pytest.raises(SourceResponseError, match="column layout"):
        gn.parse_country_info(drifted)
    with pytest.raises(SourceResponseError):
        gn.parse_country_info(b"FR\tFRA\n")
    truncated = b"\n".join(body.splitlines()[:-200])
    assert any("only" in p for p in gn.validate_rows(gn.parse_country_info(truncated)))


def test_spec() -> None:
    spec = gn.GeoNamesCountryInfoConnector().describe()
    assert spec.template == "atlas_bulk" and spec.allowed_hosts == ("download.geonames.org",)
    assert spec.licence == "CC BY 4.0"
