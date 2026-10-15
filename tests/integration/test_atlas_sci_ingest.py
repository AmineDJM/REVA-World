"""A-SCI atlas connectors end to end on a throwaway database (fixture bytes, no network)."""

from __future__ import annotations

import polars as pl
import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_nobel, atlas_openalex, atlas_wipo
from reva.adapters.connectors.atlas_sci_common import register_connector
from reva.adapters.connectors.atlas_sci_ingest import ingest_gii, ingest_nobel, ingest_openalex, ingest_wipo
from reva.atlas.tables import read_atlas_table
from reva.evidence.runner import FixtureHttp

pytestmark = pytest.mark.db


async def test_nobel_atlas_and_kg(db):
    register_connector(db.owner, db.store, atlas_nobel.NobelAtlasConnector().describe())
    rep = await ingest_nobel(db.worker, db.store, FixtureHttp(atlas_nobel.CONNECTOR_NAME), min_expected=1)
    assert rep["origin"] == "test_fixture"
    atlas = rep["atlas"]
    assert atlas["dataset_key"] == "atlas.nobel.laureates" and atlas["created"] is True
    assert [u["provider_geo"] for u in atlas["unmapped_provider_geo"]] == ["Taiwan"]
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
    again = await ingest_nobel(db.worker, db.store, FixtureHttp(atlas_nobel.CONNECTOR_NAME), min_expected=1)
    assert again["atlas"]["created"] is False
    assert again["kg"]["statements_inserted"] == 0 and again["kg"]["statements_unchanged"] == kg["statements_built"]


async def test_nobel_partial_list_writes_nothing(db):
    from reva.kernel.errors import SourceResponseError

    register_connector(db.owner, db.store, atlas_nobel.NobelAtlasConnector().describe())
    with pytest.raises(SourceResponseError):  # 18-record excerpt < 1,000 expected laureates
        await ingest_nobel(db.worker, db.store, FixtureHttp(atlas_nobel.CONNECTOR_NAME))


async def test_wipo_bulk_and_gii(db):
    register_connector(db.owner, db.store, atlas_wipo.WipoIpStatsConnector().describe())
    register_connector(db.owner, db.store, atlas_wipo.WipoGiiConnector().describe())
    rep = await ingest_wipo(db.worker, db.store, FixtureHttp(atlas_wipo.CONNECTOR_IPSTATS))
    assert rep["errors"] == []
    assert set(rep["tables"]) == {b.dataset for b in atlas_wipo.BULK_FILES}
    pub = rep["tables"]["patent_pub_tech"]
    assert "**" in {u["provider_geo"] for u in pub["unmapped_provider_geo"]}
    frame = read_atlas_table(db.store, pub["artifact_sha256"])
    fr = frame.filter(
        (pl.col("iso3") == "FRA")
        & (pl.col("indicator") == "patent_publications|office=EP|tech=6")
        & (pl.col("period_start").dt.year() == 2020)
    )
    assert fr["value"].to_list() == ["529"]
    gii = await ingest_gii(db.worker, db.store, FixtureHttp(atlas_wipo.CONNECTOR_GII))
    assert gii["economies"] == 139 and gii["atlas"]["unmapped_provider_geo"] == []


async def test_openalex_rate_limit_stops_run(db):
    """A provider 429 stops the run: nothing is invented for the queries not made."""
    from reva.adapters.http.safe_client import HttpResult
    from reva.kernel.errors import SourceResponseError

    class Refusing:
        origin = "test_fixture"
        calls = 0

        async def get(self, url: str, **kw: object) -> HttpResult:
            self.calls += 1
            raise SourceResponseError("HTTP 429 from api.openalex.org", retryable=True, status=429)

    conn = atlas_openalex.OpenAlexAtlasConnector(years=(2020, 2021), domain_years=None)
    register_connector(db.owner, db.store, conn.describe())
    http = Refusing()
    rep = await ingest_openalex(db.worker, db.store, http, conn)
    assert http.calls == 1 and rep["tables"] == {}
    assert [i["status"] for i in rep["incomplete"]] == ["failed", "not_attempted"]
