"""KGPOLITY end to end on a throwaway database: the Cliopatria atlas source in fixture mode through the atlas
platform (``reva.atlas.ingest.run_atlas_ingest``; no network), the KG load (polities, provider-declared Wikidata
ids, the derived territorial overlay), idempotence, retraction of statements a new build no longer has, and the
"neurons": KGCORE's path engine connects a present-day country to the Wikidata item of a past polity through a
derived overlay statement.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_cliopatria as ac
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered
from reva.kg import query as kq

pytestmark = pytest.mark.db


async def _run(db: Any) -> dict[str, Any]:
    spec = ac.atlas_source()
    ensure_registered(db.owner, db.store, spec)
    out = await run_atlas_ingest(db.settings, db.worker, db.store, spec.name, mode="fixture")
    assert out["status"] == "completed", out
    return out["summary"]


def _node(iso3: str) -> str:
    """The KG node the overlay uses for a country: its QID from WDHARVEST's registry/entities/country_qids.json when
    that file maps it, else the REVA node iso3:<KEY> (same rule as the connector)."""
    from reva.adapters.connectors.atlas_geo_common import load_country_qids

    return load_country_qids().get(iso3) or f"iso3:{iso3}"


def _pid(name: str) -> str:
    return ac.entity_ids([name])[name]


async def test_cliopatria_fixture_ingest_overlay_and_paths(db: Any) -> None:
    s = await _run(db)
    kg = s["kg"]
    assert not kg["rejected_counts"], kg["rejected_sample"]
    assert kg["statements_inserted"] > 100 and kg["entities_inserted"] > 10
    assert s["release"] == "v0.2.1" and s["licence"] == "CC BY 4.0"
    bra = _node("BRA")
    assert (s["country_qids"]["resolved"] > 0) == bra.startswith("Q")
    with db.owner.connect() as c:
        rows = c.execute(
            text("""SELECT start_time, end_time, qualifiers, raw_sha256, source_key, origin FROM kg.statement_current
                    WHERE subject_qid = :s AND property_pid = 'geo:covered_territory_of' AND object_qid = :o
                    ORDER BY start_time"""),
            {"s": _pid("Portuguese Empire"), "o": bra},
        ).all()
        assert rows, "Brazil <- Portuguese Empire"
        q = rows[0].qualifiers
        assert q["derivation"] == "computed_spatial_overlay" and q["label"] == "derived"
        assert q["cliopatria_raw_sha256"] == rows[0].raw_sha256 and len(q["ne_raw_sha256"]) == 64
        assert q["country_geometry_sha256"] and all(r["geometry_sha256"] for r in q["rows"])
        assert rows[0].source_key == ac.SOURCE_KEY and rows[0].origin == "test_fixture"
        # both raw artifacts (and the licence text) are verified artifacts of the load
        raws = c.execute(
            text("SELECT raw_sha256s FROM kg.load WHERE source_key = :k ORDER BY id DESC LIMIT 1"), {"k": ac.SOURCE_KEY}
        ).scalar_one()
        assert len(raws) == 3 and q["ne_raw_sha256"] in raws
        # BCE rows keep astronomical times and the provider's integers
        bce = c.execute(
            text("""SELECT start_time, qualifiers->'provider_years' FROM kg.statement_current
                    WHERE property_pid = 'cliopatria:reported_area' AND start_time LIKE '-%' LIMIT 1""")
        ).first()
        assert bce is not None and int(bce[0][:5]) == bce[1][0] + 1
        # provider-declared Wikidata id
        assert c.execute(
            text("""SELECT count(*) FROM kg.statement_current WHERE subject_qid = :s
                    AND property_pid = 'cliopatria:denotes' AND object_qid = 'Q200464'"""),
            {"s": _pid("Portuguese Empire")},
        ).scalar_one()
        # the neurons: Brazil -- (overlay) -- Portuguese Empire (Cliopatria) -- denotes -- Q200464 (Wikidata)
        res = kq.paths(c, bra, "Q200464", max_hops=2)
        assert res["paths"], res
        hops = res["paths"][0]["hops"]
        assert [h["property"]["pid"] for h in hops] == ["geo:covered_territory_of", "cliopatria:denotes"]

    again = await _run(db)
    k2 = again["kg"]
    assert k2["statements_inserted"] == 0 and k2["statements_new_revision"] == 0 and k2["retractions_inserted"] == 0


async def test_new_build_without_a_statement_retracts_it(db: Any) -> None:
    from reva.kg import overlay as ov
    from reva.kg.model import statement_key

    s = await _run(db)
    raw = s["raw"]
    with db.owner.connect() as c:
        key, n0 = c.execute(
            text("""SELECT min(statement_key), count(*) FROM kg.statement_current
                    WHERE source_key = :k AND property_pid = 'geo:covered_territory_of'"""),
            {"k": ac.SOURCE_KEY},
        ).one()
    # the same build from the archived fixture bytes, minus one overlay statement
    layer = ov.country_layer_from_ne(db.store.get("raw", raw["naturalearth"]["sha256"]))
    sc = ac.scan(db.store.get("raw", raw["cliopatria"]["sha256"]), ov.Overlayer(layer))
    b = ac.kg_records(
        sc,
        layer,
        raws={"cliopatria": raw["cliopatria"]["sha256"], "naturalearth": raw["naturalearth"]["sha256"]},
        country_qids={},
    )
    kept = [st for st in b.statements if statement_key(st, ac.SOURCE_KEY) != key]
    assert len(kept) == len(b.statements) - 1
    rep = ac.load_kg(
        db.worker,
        db.store,
        ac.KgBuild(b.entities, kept, b.spans, b.report),
        raw_sha256s=[v["sha256"] for v in raw.values()],
        retrieved_at=datetime.now(UTC),
        origin="test_fixture",
    )
    assert rep["retractions_inserted"] == 1 and rep["statements_inserted"] == 0, rep
    with db.owner.connect() as c:
        n1 = c.execute(
            text("""SELECT count(*) FROM kg.statement_current
                    WHERE source_key = :k AND property_pid = 'geo:covered_territory_of'"""),
            {"k": ac.SOURCE_KEY},
        ).scalar_one()
    assert n1 == n0 - 1


async def test_path_across_bce_dated_overlay(db: Any) -> None:
    """Regression (KGBCE): BCE-dated statements are read as text, so paths, neighbours and timelines touching
    them work; the overlay hop is labelled derived, with its method."""
    await _run(db)
    with db.owner.connect() as c:
        ita = _node("ITA")
        res = kq.paths(c, ita, "Q12544", max_hops=2)  # Italy -- Roman Empire (-31..20) -- Q12544
        assert res["paths"], res
        hop = res["paths"][0]["hops"][0]
        assert hop["property"]["pid"] == "geo:covered_territory_of"
        prov = hop["provenance"]
        assert prov["verification"] == "derived" and prov["derivation"] == "computed_spatial_overlay"
        assert prov["derivation_method"] and prov["derivation_method"] == hop["qualifiers"]["method"]
        bce = [e for e in kq.neighbours(c, ita, direction="in")["edges"] if (e["valid_from"] or "").startswith("-")]
        assert bce and all(e["valid_from"][1:5].isdigit() for e in bce)  # astronomical, e.g. -0030-01-01
        tl = kq.timeline(c, ita)["events"]
        assert tl[0]["sort_date"].startswith("-")
        keys = [tuple(int(x) for x in e["sort_date"].lstrip("-").split("-")) for e in tl]
        signed = [
            (-k[0] if e["sort_date"].startswith("-") else k[0], k[1], k[2]) for k, e in zip(keys, tl, strict=True)
        ]
        assert signed == sorted(signed)
