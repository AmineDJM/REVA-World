"""A-GEO end to end (offline, fresh test database): archive raw -> atlas tables -> KG statements.

Natural Earth's four large layers are the synthetic world of
``tests/fixtures/source/atlas.naturalearth_10m/synthetic_world.py`` served through ``FixtureHttp`` overrides;
everything else is a captured provider response. Origin is ``test_fixture`` throughout.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import polars as pl
import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_climate as cl
from reva.adapters.connectors import atlas_geonames as gn
from reva.adapters.connectors import atlas_hazards as hz
from reva.adapters.connectors import atlas_naturalearth10m as ne
from reva.adapters.connectors.atlas_geo_common import register_connector
from reva.atlas.tables import read_atlas_table
from reva.evidence.runner import FixtureHttp, _fixture_fingerprint

pytestmark = pytest.mark.db

FIXDIR = Path(__file__).parents[1] / "fixtures" / "source" / "atlas.naturalearth_10m"
_spec = importlib.util.spec_from_file_location("ne_synthetic_world_it", FIXDIR / "synthetic_world.py")
sw = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(sw)  # type: ignore[union-attr]

QIDS = {"FRA": "Q142", "ESP": "Q29", "CHE": "Q39", "DEU": "Q183", "ITA": "Q38", "AUT": "Q40", "VAT": "Q237"}


@pytest.fixture(scope="module")
def registered(db):  # type: ignore[no-untyped-def]
    for c in (ne.NaturalEarth10mConnector(), gn.GeoNamesCountryInfoConnector(), cl.CckpClimatologyConnector(),
              hz.InformHazardsConnector()):
        register_connector(db.owner, db.store, c.describe())
    return db


def _ne_http() -> FixtureHttp:
    layers = sw.layers()
    overrides = {_fixture_fingerprint(it.url): layers[it.key] for it in ne.fetch_items() if it.key in layers}
    return FixtureHttp(ne.CONNECTOR_NAME, overrides=overrides)


async def test_natural_earth_ingest_tables_and_kg(registered) -> None:  # type: ignore[no-untyped-def]
    db = registered
    out = await ne.ingest(db.worker, db.store, _ne_http(), qids=QIDS)
    tables = out["tables"]
    assert set(tables) == {ne.DS_COUNTRY, ne.DS_BORDERS, ne.DS_DISPUTED}
    assert all(t["origin"] == "test_fixture" and t["created"] for t in tables.values())
    country = read_atlas_table(db.store, tables[ne.DS_COUNTRY]["artifact_sha256"])
    che = country.filter((pl.col("iso3") == "CHE") & (pl.col("indicator") == "landlocked"))
    assert che["value_text"].to_list() == ["yes"]
    assert "SOL" in {u["provider_geo"] for u in tables[ne.DS_COUNTRY]["unmapped_provider_geo"]}
    kg = out["kg"]
    assert kg["loaded"] and kg["report"]["statements_inserted"] == kg["statements"] > 0
    with db.worker.connect() as c:
        n = c.execute(text("SELECT count(*) FROM kg.statement WHERE source_key = :s AND property_pid = 'P47'"),
                      {"s": ne.SOURCE_KEY}).scalar_one()
        q = c.execute(text("""SELECT qualifiers->'border_length_km'->>'amount', qualifiers->>'disputed_boundary'
                              FROM kg.statement WHERE subject_qid = 'Q39' AND object_qid = 'Q142'""")).one()
    assert n == kg["statements"]
    assert float(q[0]) > 200 and q[1] == "true"

    # idempotent: same bytes -> same tables (created False), same statements (nothing inserted)
    again = await ne.ingest(db.worker, db.store, _ne_http(), qids=QIDS)
    assert not any(t["created"] for t in again["tables"].values())
    assert again["kg"]["report"]["statements_inserted"] == 0


async def test_kg_blocked_without_qids(registered, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import reva.adapters.connectors.atlas_geo_common as common

    monkeypatch.setattr(common, "load_country_qids", lambda path=None: {})
    out = await ne.ingest(registered.worker, registered.store, _ne_http())
    assert out["kg"] == {"loaded": False, "blocked": "registry/entities/country_qids.json (ISO3 -> QID) not available"}


async def test_geonames_climate_hazards_ingest(registered) -> None:  # type: ignore[no-untyped-def]
    db = registered
    g = await gn.ingest(db.worker, db.store, FixtureHttp(gn.CONNECTOR_NAME), qids=QIDS)
    assert g["table"]["row_count"] > 3000 and g["kg"]["loaded"] and g["kg"]["statements"] > 0
    c = await cl.ingest(db.worker, db.store, FixtureHttp(cl.CONNECTOR_NAME))
    assert set(c["tables"]) == {cl.DS_CRU, cl.DS_ERA5}
    assert [u["provider_geo"] for u in c["tables"][cl.DS_CRU]["unmapped_provider_geo"]] == ["TWN"]
    h = await hz.ingest(db.worker, db.store, FixtureHttp(hz.CONNECTOR_NAME))
    assert h["workflow"]["name"] == "INFORM Risk 2026" and h["table"]["row_count"] >= 8 * hz.MIN_COUNTRIES
    with db.worker.connect() as conn:
        keys = {r[0] for r in conn.execute(text("SELECT dataset_key FROM evidence.dataset WHERE dataset_key LIKE 'atlas.%'"))}
    assert {"atlas.geonames.countryinfo", f"atlas.cckp.{cl.DS_CRU}", f"atlas.inform.{hz.ATLAS_DATASET}"} <= keys
