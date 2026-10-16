"""A-SOCIETY atlas sources end to end through ATLASCORE's ingest entry point on captured fixtures (origin
test_fixture): every raw response is archived as an evidence acquisition of the source's connector version,
each atlas table is registered from the archived bytes with its rights record, a re-run is idempotent, and a
drifted licence record archives the bytes but registers nothing."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_society_justice as jus
from reva.adapters.http.safe_client import request_fingerprint
from reva.atlas.ingest import AtlasRun, run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.atlas.tables import read_atlas_table
from reva.evidence.runner import FixtureHttp
from reva.kernel.errors import SourceResponseError

pytestmark = pytest.mark.db

EXPECTED = {
    "atlas.rcs_dem": ("atlas.rcs.dem2", {"USA", "FRA", "XKX", "IND", "RUS"}),
    "atlas.wrp": ("atlas.wrp.national", {"USA", "FRA", "RUS", "DEU", "IND"}),  # COW 713: historical QID (ROC)
    "atlas.hief": ("atlas.hief.hief", {"GBR", "DEU", "SRB", "IRN", "TWN"}),
    "atlas.sscp_frac": ("atlas.sscp.frac_v1", {"AFG", "ASM", "FRA", "XKX", "CHI", "TWN"}),
    "atlas.cdpd": ("atlas.cdpd.cdpd_v1", {"FRA", "RUS", "USA", "TWN", "ETH", "KGZ"}),
    "atlas.who_violence": ("atlas.who.gho_violence", {"BRA", "FRA", "JPN", "NGA", "ZAF"}),
    "atlas.ti_cpi": ("atlas.ti.cpi", {"DNK", "FRA", "XKX", "TWN", "SOM"}),
    "atlas.oecd_sigi": ("atlas.oecd.sigi2023", {"FRA", "XKX", "TWN", "NGA"}),
    "atlas.parlgov": ("atlas.parlgov.elections", {"DEU", "FRA", "GBR"}),
}


def test_new_sources_are_seeded(db) -> None:
    with db.owner.connect() as c:
        keys = set(c.execute(text("SELECT source_key FROM evidence.source")).scalars())
    assert {"rcs_dem.osf", "hief.dataverse", "sscp.dataverse", "cdpd.dataverse", "ti.cpi", "parlgov.dataverse"} <= keys


@pytest.mark.parametrize("name", sorted(EXPECTED))
async def test_fixture_ingest_registers_one_table_idempotently(db, name: str) -> None:
    spec = get_source(name).spec
    ensure_registered(db.owner, db.store, spec)
    out = await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="fixture")
    assert out["status"] == "completed" and out["budget"] is None, out["error"]
    key, countries = EXPECTED[name]
    atlas = out["summary"]["atlas"]
    assert atlas["dataset_key"] == key and atlas["row_count"] > 0
    frame = read_atlas_table(db.store, atlas["artifact_sha256"])
    assert frame.height == atlas["row_count"]
    assert countries <= set(frame["iso3"].drop_nulls().unique().to_list())
    with db.owner.connect() as c:
        archived = set(
            c.execute(
                text(
                    """SELECT a.raw_sha256 FROM evidence.acquisition a
                       JOIN evidence.connector_version v ON v.id = a.connector_version_id
                       WHERE a.origin = 'test_fixture' AND v.name = :n"""
                ),
                {"n": spec.name},
            ).scalars()
        )
    assert set(frame["raw_sha256"].unique().to_list()) <= archived
    assert out["archived_raw"] == len(spec_items(name))
    again = await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="fixture")
    assert again["summary"]["atlas"]["created"] is False
    assert again["summary"]["atlas"]["artifact_sha256"] == atlas["artifact_sha256"]


def spec_items(name: str) -> tuple:
    from reva.adapters.connectors import atlas_society_frac as frac
    from reva.adapters.connectors import atlas_society_gov as gov
    from reva.adapters.connectors import atlas_society_religion as rel

    return {
        "atlas.rcs_dem": rel.rcs_items,
        "atlas.wrp": rel.wrp_items,
        "atlas.hief": frac.hief_items,
        "atlas.sscp_frac": frac.sscp_items,
        "atlas.cdpd": jus.cdpd_items,
        "atlas.who_violence": lambda: jus.who_items(jus.WHOV_FIXTURE_COUNTRIES),
        "atlas.ti_cpi": gov.cpi_items,
        "atlas.oecd_sigi": gov.sigi_items,
        "atlas.parlgov": gov.parlgov_items,
    }[name]()


async def test_a_licence_change_blocks_registration(db) -> None:
    spec = get_source(jus.CDPD_CONNECTOR).spec
    ensure_registered(db.owner, db.store, spec)
    rec_url = jus.dataverse_record_url(jus.CDPD_DOI)
    http0 = FixtureHttp(spec.name)
    doc = json.loads((await http0.get(rec_url)).body)
    doc["data"]["latestVersion"]["license"] = {
        "name": "CC BY-NC 4.0",
        "uri": "http://creativecommons.org/licenses/by-nc/4.0",
    }
    http = FixtureHttp(spec.name, overrides={request_fingerprint("GET", rec_url): json.dumps(doc).encode()})
    run = AtlasRun(spec=spec, http=http, engine=db.worker, store=db.store, settings=db.settings, origin="test_fixture")
    with db.owner.connect() as c:
        before = c.execute(
            text("SELECT count(*) FROM evidence.dataset WHERE dataset_key='atlas.cdpd.cdpd_v1'")
        ).scalar_one()
    with pytest.raises(SourceResponseError, match="licence"):
        await jus.atlas_ingest(run)
    assert len(run.archived) == 2 and run.registered == []  # bytes archived, nothing registered
    with db.owner.connect() as c:
        after = c.execute(
            text("SELECT count(*) FROM evidence.dataset WHERE dataset_key='atlas.cdpd.cdpd_v1'")
        ).scalar_one()
    assert after == before
