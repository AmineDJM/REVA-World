"""A-SCI atlas sources end to end on a throwaway database, through ``reva.atlas.ingest`` (no network)."""

from __future__ import annotations

from typing import Any

import polars as pl
import pytest
from sqlalchemy import text

from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.atlas.tables import read_atlas_table

pytestmark = pytest.mark.db


def _reg(db: Any, name: str) -> None:
    ensure_registered(db.owner, db.store, get_source(name).spec)


async def test_nobel_atlas_and_kg(db):
    _reg(db, "atlas.nobel")
    out = await run_atlas_ingest(db.settings, db.worker, db.store, "atlas.nobel", mode="fixture")
    assert out["status"] == "completed", out
    rep = out["summary"]
    atlas = rep["atlas"]
    assert atlas["dataset_key"] == "atlas.nobel.laureates" and atlas["created"] is True
    assert atlas["unmapped_provider_geo"] == []
    frame = read_atlas_table(db.store, atlas["artifact_sha256"])
    pol = frame.filter((pl.col("iso3") == "POL") & (pl.col("indicator") == "born_total|category=all"))
    assert pol["value"].to_list() and int(pol["value"][0]) >= 2  # Marie Curie twice
    kg = rep["kg"]
    assert kg["status"] == "loaded", kg
    assert kg["statements_inserted"] == kg["statements_built"] > 0 and kg["rejected_counts"] == {}
    with db.owner.connect() as c:
        n = c.execute(
            text(
                "SELECT count(*) FROM kg.statement_current WHERE subject_qid = 'Q7186' AND property_pid = 'P166' "
                "AND origin = 'test_fixture'"
            )
        ).scalar_one()
    assert n == 2
    again = await run_atlas_ingest(db.settings, db.worker, db.store, "atlas.nobel", mode="fixture")
    assert again["summary"]["atlas"]["created"] is False
    assert again["summary"]["kg"]["statements_inserted"] == 0


async def test_nobel_partial_list_writes_nothing(db):
    """Live mode keeps the 1,000-laureate threshold: an 18-record list is refused, no table registered."""
    from reva.adapters.http.safe_client import HttpResult
    from reva.evidence.runner import FixtureHttp
    from reva.kernel.errors import SourceResponseError

    _reg(db, "atlas.nobel")
    inner = FixtureHttp("atlas.nobel")

    class Live:
        origin = "live"

        async def get(self, url: str, **kw: Any) -> HttpResult:
            return await inner.get(url, **kw)

        async def aclose(self) -> None:
            return None

    with pytest.raises(SourceResponseError):
        await run_atlas_ingest(db.settings, db.worker, db.store, "atlas.nobel", http_factory=lambda s, st: Live())
    with db.owner.connect() as c:
        n = c.execute(
            text(
                "SELECT count(*) FROM evidence.dataset WHERE dataset_key = 'atlas.nobel.laureates' AND origin = 'live'"
            )
        ).scalar_one()
    assert n == 0


async def test_wipo_bulk_and_gii(db):
    _reg(db, "atlas.wipo_ipstats")
    _reg(db, "atlas.wipo_gii")
    out = await run_atlas_ingest(db.settings, db.worker, db.store, "atlas.wipo_ipstats", mode="fixture")
    rep = out["summary"]
    assert rep["errors"] == []
    assert set(rep["tables"]) == {
        "patent_pub_tech",
        "patent_grant_tech",
        "tm_appl_class",
        "tm_reg_class",
        "design_appl_class",
        "design_reg_class",
    }
    pub = rep["tables"]["patent_pub_tech"]
    assert "**" in {u["provider_geo"] for u in pub["unmapped_provider_geo"]}
    frame = read_atlas_table(db.store, pub["artifact_sha256"])
    fr = frame.filter(
        (pl.col("iso3") == "FRA")
        & (pl.col("indicator") == "patent_publications|office=EP|tech=6")
        & (pl.col("period_start").dt.year() == 2020)
    )
    assert fr["value"].to_list() == ["529"]
    one = await run_atlas_ingest(
        db.settings, db.worker, db.store, "atlas.wipo_ipstats", dataset="design_reg_class", mode="fixture"
    )
    assert set(one["summary"]["tables"]) == {"design_reg_class"}
    gii = await run_atlas_ingest(db.settings, db.worker, db.store, "atlas.wipo_gii", mode="fixture")
    assert gii["summary"]["economies"] == 139 and gii["summary"]["atlas"]["unmapped_provider_geo"] == []


async def test_openalex_rate_limit_stops_run(db):
    """A provider 429 stops the run: nothing is invented for the queries not made; usage is settled."""
    from reva.adapters.http.safe_client import HttpResult
    from reva.kernel.errors import SourceResponseError

    class Refusing:
        origin = "live"
        calls = 0

        async def get(self, url: str, **kw: object) -> HttpResult:
            self.calls += 1
            raise SourceResponseError("HTTP 429 from api.openalex.org", retryable=True, status=429)

        async def aclose(self) -> None:
            return None

    _reg(db, "atlas.openalex")
    http = Refusing()
    out = await run_atlas_ingest(
        db.settings,
        db.worker,
        db.store,
        "atlas.openalex",
        dataset="works_country_year",
        http_factory=lambda s, st: http,
    )
    rep = out["summary"]
    assert http.calls == 1 and rep["tables"] == {}
    assert rep["incomplete"][0]["status"] == "failed"
    assert {i["status"] for i in rep["incomplete"][1:]} == {"not_attempted"}


async def test_openalex_fixture_run(db):
    """Recorded live responses of the 2020 queries (first page + complement queries) -> two atlas tables."""
    _reg(db, "atlas.openalex")
    out = await run_atlas_ingest(db.settings, db.worker, db.store, "atlas.openalex", mode="fixture")
    rep = out["summary"]
    assert out["status"] == "completed" and rep["incomplete"] == [] and rep["stopped"] is None
    year = read_atlas_table(db.store, rep["tables"]["works_country_year"]["artifact_sha256"])
    assert year.filter(pl.col("iso3").is_not_null())["iso3"].n_unique() > 200
    assert {u["provider_geo"] for u in rep["tables"]["works_country_year"]["unmapped_provider_geo"]} <= {
        "_all_works",
        "unknown",
    }
    fra = year.filter(pl.col("iso3") == "FRA")["value"].to_list()
    assert len(fra) == 1 and int(fra[0]) > 100_000
    xkx = year.filter(pl.col("iso3") == "XKX")  # OpenAlex XK -> Kosovo via registry/atlas/geo_aliases/a_sci.yaml
    assert xkx.height == 1
    dom = read_atlas_table(db.store, rep["tables"]["works_country_domain"]["artifact_sha256"])
    assert dom["indicator"].n_unique() == 4
