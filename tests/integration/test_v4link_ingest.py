"""V4LINK end to end on a throwaway database: the UNSD classification source in fixture mode through the atlas
platform (no network): KG codes with verbatim titles, hierarchy, concordance rows with the tables' flags and raw
provenance, idempotence, and the product -> industry path HS 2022 2709.00 -> HS 2017 -> CPC 2.1 12010 -> ISIC 0610.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_unsd_classif as uc
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.kg import query as kq

pytestmark = pytest.mark.db


async def _run(db: Any, name: str) -> dict[str, Any]:
    ensure_registered(db.owner, db.store, get_source(name).spec)
    out = await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="fixture")
    assert out["status"] == "completed", out
    return out


async def test_unsd_classifications_fixture_ingest_and_concordance_path(db: Any) -> None:
    out = await _run(db, uc.NAME)
    s = out["summary"]
    kg = s["kg"]
    assert not kg["rejected_counts"], kg["rejected_sample"]
    assert kg["entities_inserted"] == sum(v for k, v in s["build"].items() if k.startswith("entities_"))
    assert kg["statements_inserted"] > 0
    with db.owner.connect() as c:
        row = c.execute(
            text("""SELECT s.qualifiers, s.raw_sha256, s.source_key, s.refs, l.raw_sha256s
                    FROM kg.statement_current s JOIN kg.load l ON l.id = s.load_id
                    WHERE s.subject_qid = 'hs2017:270900' AND s.property_pid = 'v4link:corresponds_to'
                      AND s.object_qid = 'cpc21:12010'""")
        ).one()
        assert row.source_key == uc.SOURCE_KEY and row.raw_sha256 == s["files"]["hs2017_cpc21"]["raw_sha256"]
        assert row.raw_sha256 in row.raw_sha256s
        assert row.qualifiers["hs_partial"] == "0" and row.qualifiers["table"] == "CPC21-HS2017"
        ent = kq.entity(c, "isic4:0610")
        assert ent is not None and ent["kind"] == "isic_code", ent
        props = ["v4link:corresponds_to"]
        res = kq.paths(c, "hs2022:270900", "isic4:0610", max_hops=3, allowed_properties=props)
        assert res["paths"], res
        assert [h["property"]["pid"] for h in res["paths"][0]["hops"]] == props * 3
    again = await _run(db, uc.NAME)
    k2 = again["summary"]["kg"]
    assert k2["statements_inserted"] == 0 and k2["entities_inserted"] == 0 and k2["retractions_inserted"] == 0


async def test_wikidata_bridges_from_qlever_fixture(db: Any) -> None:
    """atlas.v4link_qlever, pack `places` (trimmed live QLever responses): identity statements, best rank, the
    ambiguous holder (Ile-de-France holds NUTS FR1 and FR10) not linked, best-rank P17 only, new item entities."""
    from reva.adapters.connectors import atlas_v4link_wikidata as wd

    ensure_registered(db.owner, db.store, get_source(wd.QL_NAME).spec)
    out = await run_atlas_ingest(
        db.settings, db.worker, db.store, wd.QL_NAME, mode="fixture", options={"packs": "places"}
    )
    assert out["status"] == "completed", out
    s = out["summary"]
    assert "Complete Wikidata" in s["qlever_index"]["name_index"]
    pk = s["packs"]["places"]
    assert not pk["kg"]["rejected_counts"], pk["kg"]["rejected_sample"]
    assert pk["bridges"]["by_property"]["P605"]["ambiguous_holder"] == 2
    with db.owner.connect() as c:
        rows = {
            (r.subject_qid, r.object_qid): r
            for r in c.execute(
                text("""SELECT subject_qid, object_qid, source_key, qualifiers, rank FROM kg.statement_current
                        WHERE property_pid = 'v4link:wikidata_item'""")
            )
        }
        assert ("iso3166_2:DE-BY", "Q980") in rows and ("nuts:DE2", "Q980") in rows
        assert ("iso3166_2:FR-IDF", "Q13917") in rows and ("iso3166_2:FR-J", "Q13917") not in rows  # best rank
        assert not any(k[0].startswith("nuts:FR1") for k in rows)  # ambiguous holder, never linked
        r = rows[("iso3166_2:DE-BY", "Q980")]
        assert r.source_key == "qlever.wikidata" and r.qualifiers["wikidata_statement_id"].startswith("Q980$")
        countries = (
            c.execute(
                text("""SELECT object_qid FROM kg.statement_current WHERE subject_qid = 'Q980' AND property_pid = 'P17'
                    AND source_key = 'qlever.wikidata'""")
            )
            .scalars()
            .all()
        )
        assert countries == ["Q183"]  # the preferred statement only
        res = kq.paths(c, "iso3166_2:DE-BY", "nuts:DE2", max_hops=2, allowed_properties=["v4link:wikidata_item"])
        assert res["paths"], res
