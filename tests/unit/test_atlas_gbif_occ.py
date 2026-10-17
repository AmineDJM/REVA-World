"""V4ECO GBIF occurrence facets: parsers, atlas rows and KG statements on captured fixtures (no network)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from reva.adapters.connectors import atlas_gbif_occ as g

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "source" / "atlas.gbif_occ"


def _body(url_suffix: str) -> bytes:
    for e in json.loads((FIX / "manifest.json").read_text()):
        if e["url"].endswith(url_suffix):
            return (FIX / e["file"]).read_bytes()
    raise KeyError(url_suffix)


def _graphql_bodies() -> list[bytes]:
    return [
        (FIX / e["file"]).read_bytes()
        for e in json.loads((FIX / "manifest.json").read_text())
        if e["url"] == g.GRAPHQL_URL
    ]


def test_spec_and_plan() -> None:
    spec = g.atlas_source()
    assert spec.name == "atlas.gbif_occ" and spec.atlas_source == "gbif_occ"
    assert set(spec.allowed_hosts) == {"graphql.gbif.org", "api.gbif.org"}
    live = g.plan_for("live")
    assert spec.planned_requests == g.planned_requests(live) < 400
    fx = g.plan_for("test_fixture")
    assert [x.slug for x in fx.groups] == ["all", "mammalia"] and fx.seeds == (5219404, 1341976)
    # the base filter is part of every predicate
    for p in (g.group_predicate(g.GROUPS[3]), g.year_predicate(2000), g.iucn_predicate("CR"), g.taxon_predicate(1)):
        assert p["predicates"][: len(g.BASE_PREDICATES)] == list(g.BASE_PREDICATES)


def test_parse_country_facets_from_fixture() -> None:
    parsed = [json.loads(b) for b in _graphql_bodies()]
    with_card = [
        b for b, d in zip(_graphql_bodies(), parsed, strict=True) if "cardinality" in d["data"]["occurrenceSearch"]
    ]
    assert len(with_card) == 4  # all, mammalia, year 2020, CR
    for body in with_card:
        res = g.parse_facet(body, with_cardinality=True)
        assert 150 < len(res.buckets) < g.FACET_SIZE
        assert sum(b.count for b in res.buckets) <= res.total
        assert all(b.distinct_species is not None and b.distinct_species <= max(res.distinct_species or 0, 1) * 2
                   for b in res.buckets)  # fmt: skip


def test_parse_facet_blocks_errors_and_truncation() -> None:
    with pytest.raises(g.GbifParseError):
        g.parse_facet(b'{"errors":[{"message":"boom"}]}', with_cardinality=True)
    buckets = [{"key": f"A{i}", "count": 1} for i in range(g.FACET_SIZE)]
    doc = {"data": {"occurrenceSearch": {"documents": {"total": 10_000}, "facet": {"countryCode": buckets}}}}
    with pytest.raises(g.GbifParseError, match="truncated"):
        g.parse_facet(json.dumps(doc).encode(), with_cardinality=False)
    doc["data"]["occurrenceSearch"]["facet"]["countryCode"] = [{"key": "FR", "count": 20_000}]
    with pytest.raises(g.GbifParseError, match="exceed"):
        g.parse_facet(json.dumps(doc).encode(), with_cardinality=False)


def test_species_and_iucn() -> None:
    lion = g.parse_species(_body("/species/5219404"), 5219404)
    assert lion.canonical_name == "Panthera leo" and lion.rank == "SPECIES" and lion.vernacular_name == "lion"
    with pytest.raises(g.GbifParseError):
        g.parse_species(_body("/species/5219404"), 1)
    vu = g.parse_iucn(_body("/species/5219404/iucnRedListCategory"), 5219404)
    assert vu is not None and vu.code == "VU" and vu.iucn_taxon_id == "15951"
    ne = g.parse_iucn(_body("/species/1341976/iucnRedListCategory"), 1341976)
    assert ne is not None and ne.code == "NE"
    assert g.parse_iucn(b"", 1) is None and g.parse_iucn(b"{}", 1) is None


def test_rows_flags_and_kg() -> None:
    res = g.FacetResult(100, 9, (g.Bucket("FR", 40, 5), g.Bucket("XZ", 12, 3), g.Bucket("BE", 3, 2)))
    rows = g.facet_rows("country_group", "mammalia", res, period=(date(2026, 10, 17), date(2026, 10, 18)),
                        snapshot=date(2026, 10, 17), raw_sha256="a" * 64, query_filter="classKey=359")  # fmt: skip
    assert len(rows) == 6
    occ = [r for r in rows if r["indicator"] == "occurrences.mammalia"]
    dist = [r for r in rows if r["indicator"] == "distinct_species.mammalia"]
    assert [r["value"] for r in occ] == ["40", "12", "3"]
    assert json.loads(dist[0]["flags"])["approximate"] is True
    assert json.loads(occ[0]["flags"])["approximate"] is False
    assert "not abundance" in json.loads(occ[0]["flags"])["caveat"]
    frame = g.atlas_frame(rows)
    assert frame.height == 6

    lion = g.parse_species(_body("/species/5219404"), 5219404)
    iucn = g.parse_iucn(_body("/species/5219404/iucnRedListCategory"), 5219404)
    raw = {"species:5219404": "b" * 64, "iucn:5219404": "c" * 64, "taxon:5219404": "d" * 64}
    ents, sts, rep = g.kg_parts(
        groups=[],
        seeds=[(lion, iucn, res)],
        raw_by_key=raw,
        snapshot=date(2026, 10, 17),
        country_qids={"FRA": "Q142"},
        iso3_of=lambda c: {"FR": "FRA", "BE": "BEL"}.get(c),
    )
    rec = [s for s in sts if s.property_pid == "gbif_occ:recorded_in"]
    assert [(s.object_qid, s.qualifiers["occurrences"]) for s in rec] == [("Q142", 40)]  # BE below 10, XZ unmapped
    assert rep["below_threshold"] == 1 and rep["unmapped_geo"] == ["XZ"]
    cat = [s for s in sts if s.property_pid == "gbif:iucn_red_list_category"]
    assert cat[0].value == {"type": "string", "value": "VU"} and cat[0].raw_sha256 == "c" * 64
    assert {e.qid for e in ents} == {"gbif:5219404", "atlas:gbif_occ.taxon_country"}
    assert all(not s.property_pid.startswith("P") for s in sts)
