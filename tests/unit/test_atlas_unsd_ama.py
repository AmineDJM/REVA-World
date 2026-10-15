"""UNSD National Accounts AMA connector (agent A-FACTBOOK): offline.

Fixtures ``tests/fixtures/source/atlas.unsd_ama``: the recorded AMA file list and three AMA bulk
workbooks (files 2, 9, 30) trimmed to the rows of CountryID 250 France, 408 DPR Korea, 412 Kosovo,
660 Anguilla, 810 former USSR, 835 Tanzania Mainland (derived fixtures: every other workbook part,
including shared strings and the footnote sheets, is unchanged).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date
from typing import Any

import polars as pl
import pytest

from reva.adapters.connectors import atlas_unsd_ama as m
from reva.atlas.catalog import load_catalog
from reva.atlas.sources import discover
from reva.atlas.tables import country_iso3, validate_atlas_frame
from reva.evidence.runner import FixtureHttp
from reva.kernel.errors import SourceResponseError


class _Run:
    def __init__(self, options: dict[str, Any]) -> None:
        self.spec = m.atlas_source()
        self.http = FixtureHttp(m.NAME)
        self.options = options
        self.frame: pl.DataFrame | None = None
        self.unmapped: list[dict[str, Any]] = []

    def wants(self, dataset: str) -> bool:
        return dataset in self.spec.datasets

    async def get(self, url: str, **kw: Any) -> Any:
        r = await self.http.get(url)

        class Raw:
            body = r.body

        return Raw()

    def archive(self, raw: Any) -> str:
        return hashlib.sha256(raw.body).hexdigest()

    def register(self, *, dataset: str, frame: pl.DataFrame, raw_sha256s: list[str], geo_scheme: str, **kw: Any):
        self.frame, self.unmapped = validate_atlas_frame(
            frame, source=m.ATLAS_SOURCE, dataset=dataset, raw_sha256s=raw_sha256s, geo_scheme=geo_scheme
        )
        return {"dataset_key": "atlas.unsd.ama", "row_count": self.frame.height, "created": True}


@pytest.fixture(scope="module")
def run() -> tuple[_Run, dict[str, Any]]:
    r = _Run({"files": "2,9,30"})
    return r, asyncio.run(m.atlas_ingest(r))


def test_spec_is_discovered():
    found, errors = discover(["reva.adapters.connectors.atlas_unsd_ama"])
    assert errors == [] and set(found) == {m.NAME}
    spec = found[m.NAME].spec
    assert spec.source_key == "unsd.ama" and spec.planned_requests == len(m.FILES) + 1
    assert spec.allowed_hosts == ("unstats.un.org",)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1691108977.3168273", "1691108977.3168273"), ("4.4998427079926302E-2", "4.4998427079926302E-2"),
     ("-2.5", "-2.5"), ("1E+3", "1E+3"), (".5", "0.5"), ("X", None), ("NaN", None), ("", None)],
)  # fmt: skip
def test_decimal_text(text, expected):
    assert m.decimal_text(text) == expected


def test_geo_scheme_m49_plus_kosovo():
    assert country_iso3("250", m.GEO_SCHEME) == "FRA"
    assert country_iso3("412", m.GEO_SCHEME) == "XKX"
    assert country_iso3("660", m.GEO_SCHEME) == "AIA"
    assert country_iso3("810", m.GEO_SCHEME) is None and country_iso3("835", m.GEO_SCHEME) is None


def test_ama_table_from_fixtures(run):
    r, out = run
    assert out["failed"] == [] and set(out["files"]) == {2, 9, 30}
    assert out["files"][2]["title"].startswith("GDP/breakdown at current prices in US Dollars")
    f = r.frame
    assert f is not None
    gdp = f.filter((pl.col("indicator") == "gdp_current_usd|Gross Domestic Product (GDP)") & (pl.col("iso3") == "AIA"))
    assert gdp.height >= 50 and gdp["unit"].unique().to_list() == ["current US$"]
    assert gdp["period_start"].min() == date(1970, 1, 1)
    prk = f.filter((pl.col("indicator") == "gdp_pc_current_usd") & (pl.col("iso3") == "PRK"))
    assert prk.height > 0
    pop = f.filter((pl.col("indicator") == "xr_pop|Population") & (pl.col("iso3") == "FRA"))
    assert pop["unit"].unique().to_list() == ["persons"]
    xr = f.filter((pl.col("indicator") == "xr_pop|AMA exchange rate") & (pl.col("iso3") == "FRA")).to_dicts()
    assert xr and json.loads(xr[0]["flags"])["currency"]
    assert {u["provider_geo"] for u in r.unmapped} == {"810", "835"}  # former USSR, Tanzania Mainland kept
    assert f.filter(pl.col("flags").str.contains('"footnotes"')).height > 0
    assert all(json.loads(x)["provider_nature"].startswith("UNSD AMA") for x in f["flags"].head(20).to_list())


def test_unexpected_workbook_is_schema_drift():
    cols = m.empty_columns()
    with pytest.raises(SourceResponseError):
        m.parse_file(b"not a workbook", 2, "a" * 64, cols, {})


def test_catalog_concepts():
    cat = load_catalog()
    ama = cat.by_domain("unsd_ama")
    assert {c.id for c in ama} >= {"unsd_ama.gdp_current_usd", "unsd_ama.population"}
    assert all(c.provider_declared for c in ama)
