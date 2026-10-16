"""A-COLONIAL sources end to end on a throwaway database through the atlas platform (fixture mode: no network).

Raw files are archived as evidence acquisitions, the atlas tables are registered, the KG statements load without
rejection, no causal claim is delivered (relations_colonial.yaml: none of the coded relations identifies a cause
event), re-running is a no-op, and an ICOW independence and the COW Territorial Change independence of the same state
from the same ruler share one ``colonial:independence_key`` value (the evidence EVENTS' ``same_property`` rule uses).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_colonial, atlas_terrchange
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered

pytestmark = pytest.mark.db


async def _run(db: Any, spec: Any) -> dict[str, Any]:
    ensure_registered(db.owner, db.store, spec)
    out = await run_atlas_ingest(db.settings, db.worker, db.store, spec.name, mode="fixture")
    assert out["status"] == "completed", out
    summary: dict[str, Any] = out["summary"]
    return summary


def _spec(name: str) -> Any:
    specs = [*atlas_colonial.atlas_source(), atlas_terrchange.atlas_source()]
    return next(s for s in specs if s.name == name)


async def test_colonial_sources_end_to_end(db: Any) -> None:
    coldat = await _run(db, _spec("atlas.coldat"))
    assert coldat["atlas"]["dyads_v3"]["created"] and coldat["claims"]["delivered"] == 0
    assert not coldat["kg"]["rejected"] and coldat["kg"]["statements_inserted"] == coldat["kg_counts"]["statements"]
    icow = await _run(db, _spec("atlas.icow"))
    assert icow["atlas"]["colhist_v11"]["created"] and icow["atlas"]["claims_prov101"]["created"]
    for k in ("colhist", "claims"):
        assert not icow["kg"][k]["rejected"], icow["kg"][k]
    tc = await _run(db, _spec("atlas.cowtc"))
    assert tc["changes"] == 842 and not tc["kg"]["rejected"], tc["kg"]
    assert tc["kg"]["statements_inserted"] == tc["kg_counts"]["statements"]

    with db.owner.connect() as c:
        brazil = c.execute(
            text(
                "SELECT s.subject_qid, s.object_qid, s.qualifiers->>'aggregation' FROM kg.statement_current s "
                "WHERE s.property_pid = 'coldat:colonized_by' AND s.statement_key = :k"
            ),
            {"k": "harvard.dataverse.coldat:coldat:BRA:portugal:max"},
        ).one()
        assert brazil[1] == "Q45" and brazil[2] == "last date aggregation"
        holders = (
            c.execute(
                text(
                    "SELECT subject_qid FROM kg.statement_current WHERE property_pid = 'colonial:independence_key' "
                    "AND value->>'value' = 'independence|cow:140|from|cow:235' ORDER BY 1"
                )
            )
            .scalars()
            .all()
        )
        assert holders == ["cowtc:46", "icowcol:independence:140"]
        if c.execute(text("SELECT to_regclass('kg.causal_load') IS NOT NULL")).scalar_one():
            n_loads = c.execute(
                text(
                    "SELECT count(*) FROM kg.causal_load WHERE source_key IN "
                    "('icow.paulhensel', 'cow.terrchange', 'harvard.dataverse.coldat')"
                )
            ).scalar_one()
            assert n_loads == 0  # no causal claim from these datasets
        archived = c.execute(
            text(
                "SELECT count(DISTINCT a.raw_sha256) FROM evidence.acquisition a "
                "JOIN evidence.connector_version v ON v.id = a.connector_version_id "
                "WHERE v.name IN ('atlas.coldat', 'atlas.icow', 'atlas.cowtc')"
            )
        ).scalar_one()
        assert archived >= 8  # coldat 5 (incl. countrycode), icow 2 + countrycode, cowtc 1 + countrycode

    from reva.events.same_as import derive_links

    rule = {
        "rules": [
            {
                "id": "colonial_independence_key",
                "kind": "same_property",
                "property": "colonial:independence_key",
                "status": "active",
                "max_group": 2,
            }
        ]
    }
    with db.worker.connect() as c:
        rep = derive_links(c, rule, origin="test_fixture")
    pairs = {(x.left, x.right) for x in rep.links}
    assert ("cowtc:46", "icowcol:independence:140") in pairs

    again = await _run(db, _spec("atlas.cowtc"))
    assert not again["atlas"]["changes_cy"]["created"] and again["kg"]["statements_inserted"] == 0
