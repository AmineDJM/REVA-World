"""KGPOLITY: Cliopatria polity rows -> KG records and the derived territorial overlay (offline, no database).

Synthetic geometries test the overlay arithmetic exactly; the captured fixture
(``tests/fixtures/source/atlas.cliopatria``: real Cliopatria v0.2.1 rows and real Natural Earth 1:10m map units,
both trimmed and simplified, see its manifest) tests the parser and the example chains.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
import shapely
import yaml
from shapely.geometry import box

from reva.adapters.connectors import atlas_cliopatria as ac
from reva.kg import overlay as ov
from reva.kg.model import PID_RE, QID_RE, _normalise_statement, statement_key

REPO = Path(__file__).resolve().parents[2]
FIX = REPO / "tests" / "fixtures" / "source" / ac.NAME


def _fixture(url: str) -> bytes:
    man = json.loads((FIX / "manifest.json").read_text())
    (e,) = [e for e in man if e["url"] == url]
    return (FIX / e["file"]).read_bytes()


def _feature(name: str, fy: int, ty: int, geom, **props) -> dict:
    p = {
        "Name": name,
        "FromYear": fy,
        "ToYear": ty,
        "Area": 1000.5,
        "Type": "POLITY",
        "Wikipedia": name,
        "Wikidata": "",
        "SeshatID": "",
        "Components": "",
        "MemberOf": "",
    }
    p.update(props)
    return {"type": "Feature", "properties": p, "geometry": shapely.geometry.mapping(geom)}


def _zip(features: list[dict], member: str = "cliopatria_polities_only_v021.geojson", one_line: bool = True) -> bytes:
    if one_line:
        body = '{"type":"FeatureCollection","name":"x","features":[' + ",".join(json.dumps(f) for f in features) + "]}"
    else:
        body = json.dumps({"type": "FeatureCollection", "features": features}, indent=2)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(member, body)
    return buf.getvalue()


# two "countries": A = [0,10]x[0,10], B = [10,20]x[0,10]
def _layer() -> ov.CountryLayer:
    return ov.country_layer_from_geometries(
        [("AAA", "AAA", "Country A", box(0, 0, 10, 10)), ("BBB", "BBB", "Country B", box(10, 0, 20, 10))]
    )


# ---------------------------------------------------------------------------------------------- overlay maths
def test_area_matches_closed_form_and_tiling_does_not_change_results() -> None:
    from reva.adapters.connectors.atlas_geodesy import ellipsoid_zone_area_m2

    a = ov.ellipsoidal_area_km2(box(0, 0, 10, 10))
    assert a == pytest.approx(ellipsoid_zone_area_m2(0, 10, 10) / 1e6, rel=1e-9)
    holes = shapely.Polygon(box(0, 0, 10, 10).exterior.coords, [box(2, 2, 4, 4).exterior.coords])
    assert ov.ellipsoidal_area_km2(holes) == pytest.approx(a - ov.ellipsoidal_area_km2(box(2, 2, 4, 4)), rel=1e-9)
    poly = box(5, 2, 15, 6)
    coarse = ov.CountryLayer(_layer().territories, "t", [], tile_deg=50.0).overlaps_km2(poly)
    fine = ov.CountryLayer(_layer().territories, "t", [], tile_deg=1.0).overlaps_km2(poly)
    assert coarse.keys() == fine.keys() == {0, 1}
    for k in coarse:
        assert coarse[k] == pytest.approx(fine[k], rel=1e-9)


def test_overlay_shares_and_fast_path() -> None:
    layer = _layer()
    o = ov.Overlayer(layer)
    area, hits = o.overlay(shapely.geometry.mapping(box(5, 0, 20, 10)))  # half of A, all of B
    got = {h.territory: h for h in hits}
    assert got["AAA"].share == pytest.approx(0.5, abs=1e-6)
    assert got["BBB"].share == pytest.approx(1.0, abs=1e-9)
    assert area == pytest.approx(got["AAA"].overlap_km2 + got["BBB"].overlap_km2, rel=1e-9)
    assert o.counters["geometries_intersected"] == 1
    o.overlay(shapely.geometry.mapping(box(5, 0, 20, 10)))
    assert o.counters["cache_hits"] == 1
    # outside everything / invalid (bow-tie) geometry repaired, counted
    assert o.overlay(shapely.geometry.mapping(box(50, 50, 60, 60)))[1] == []
    bow = {"type": "Polygon", "coordinates": [[[0, 0], [10, 10], [10, 0], [0, 10], [0, 0]]]}
    _, hits = o.overlay(bow)
    assert o.counters["polity_geometry_repaired"] == 1 and hits and hits[0].territory == "AAA"
    assert ov.share_text(1.0000001) == "1.0000" and ov.share_text(0.00004) == "0.0000"


def test_spans_fold_contiguous_rows_and_break_on_gaps_or_low_share() -> None:
    H = ov.Hit
    rows = [
        ov.RowRef(0, "P", -100, -51, "g0"),
        ov.RowRef(1, "P", -50, 0, "g1"),
        ov.RowRef(2, "P", 1, 20, "g2"),  # provider writes 0 then 1: contiguous
        ov.RowRef(3, "P", 21, 40, "g3"),  # share below threshold: ends the run
        ov.RowRef(4, "P", 41, 60, "g4"),
        ov.RowRef(5, "P", 100, 120, "g5"),  # gap in the polity's own rows: new run
    ]
    hits = {
        0: [H("AAA", 10, 0.5)],
        1: [H("AAA", 12, 0.6), H("BBB", 1, 0.02)],
        2: [H("AAA", 14, 0.7)],
        3: [H("AAA", 0.1, 0.005)],
        4: [H("AAA", 9, 0.4)],
        5: [H("AAA", 9, 0.4)],
    }
    sp, stats = ov.spans(rows, hits, min_share=0.01)
    got = [(s.territory, s.from_year, s.to_year, len(s.rows)) for s in sp]
    assert got == [("AAA", -100, 20, 3), ("AAA", 41, 60, 1), ("AAA", 100, 120, 1), ("BBB", -50, 0, 1)]
    assert sp[0].summary() == {"max": "0.7000", "min": "0.5000", "first": "0.5000", "last": "0.7000"}
    assert stats["below_min_share"] == 1


def test_streaming_reader_is_layout_independent_and_bounded() -> None:
    feats = [_feature(f"P{i}", 1, 2, box(i, 0, i + 1, 1)) for i in range(5)]
    for one_line in (True, False):
        z = zipfile.ZipFile(io.BytesIO(_zip(feats, one_line=one_line)))
        with z.open(z.namelist()[0]) as fh:
            got = list(ov.iter_geojson_features(fh))
        assert [f["properties"]["Name"] for f in got] == [f"P{i}" for i in range(5)]
    z = zipfile.ZipFile(io.BytesIO(_zip(feats)))
    with z.open(z.namelist()[0]) as fh, pytest.raises(ov.OverlayError, match="larger"):
        list(ov.iter_geojson_features(fh, max_feature_bytes=50))
    with pytest.raises(ov.OverlayError, match="truncated|malformed"):
        list(ov.iter_geojson_features(io.BytesIO(b'{"features": [{"type": "Feature", "prop')))


# ---------------------------------------------------------------------------------------------- connector
def test_years_are_astronomical_with_provider_years_kept() -> None:
    assert [ac.astro_year(y) for y in (-3400, -1, 0, 1, 2024)] == [-3399, 0, 0, 1, 2024]
    assert ac.kg_year(-3400).iso == "-3399-01-01T00:00:00Z" and ac.kg_year(0).iso == "+0000-01-01T00:00:00Z"
    assert ac.kg_year(1822).precision == 9
    assert ac.year_label(-3400) == "3400 BCE"


def test_entity_ids_are_valid_stable_and_collision_safe() -> None:
    ids = ac.entity_ids(["Portuguese Empire", "(Portuguese Empire)", "Chú", "Chu", "Đại Việt"])
    assert ids["Portuguese Empire"] == "cliopatria:polity:portuguese_empire"
    assert ids["(Portuguese Empire)"] == "cliopatria:polity:grp_portuguese_empire"
    assert ids["Chu"] != ids["Chú"] and ids["Chu"].startswith("cliopatria:polity:chu-")
    assert all(QID_RE.match(v) for v in ids.values())
    assert ac.entity_ids(["Chu", "Chú"]) == {k: ids[k] for k in ("Chu", "Chú")}


def test_predicates_file_is_valid_and_registered_names_fit_kgcore() -> None:
    props = ac.load_predicates()
    assert {p.pid for p in props} == {ac.P_OVERLAY, ac.P_DENOTES, ac.P_AREA, ac.P_MEMBER, ac.P_SESHAT}
    assert all(PID_RE.match(p.pid) for p in props)
    doc = yaml.safe_load(ac.PREDICATES_FILE.read_text())
    derived = {p["pid"] for p in doc["predicates"] if p["derived"]}
    assert derived == {ac.P_OVERLAY}


def test_scan_and_records_on_synthetic_file() -> None:
    feats = [
        _feature("Alpha", -60, -31, box(0, 0, 10, 10), Wikidata="Q1", SeshatID="xx_alpha", MemberOf="(League)"),
        _feature("Alpha", -30, 0, box(0, 0, 15, 10), Wikidata="Q1", SeshatID="xx_alpha"),
        _feature("Alpha", 1, 10, box(0, 0, 15, 10), Wikidata=""),
        _feature("(League)", -60, -31, box(0, 0, 10, 10), Type="RELATION", Components="Alpha"),
        _feature("Bad", 5, 1, box(0, 0, 1, 1)),
        _feature("Weird", 1, 2, box(0, 0, 1, 1), Wikidata="not-a-qid"),
    ]
    sc = ac.scan(_zip(feats), ov.Overlayer(_layer()))
    assert sc.release == "v0.2.1" and len(sc.rows) == 5
    assert sc.counters["row_invalid_years"] == 1 and sc.counters["row_invalid_wikidata"] == 1
    b = ac.kg_records(sc, _layer(), raws={"cliopatria": "a" * 64, "naturalearth": "b" * 64}, country_qids={"AAA": "Q7"})
    by = {}
    for s in b.statements:
        by.setdefault(s.property_pid, []).append(s)
    (d,) = by[ac.P_DENOTES]  # Q1 on the two BCE rows only; the third row carries no QID -> nothing guessed
    assert d.object_qid == "Q1" and d.start.iso == "-0059-01-01T00:00:00Z" and d.end.iso == "+0000-01-01T00:00:00Z"
    assert d.qualifiers["provider_years"] == [-60, 0] and "provider_year_zero" in d.qualifiers
    assert ("Alpha", 1, 10) in b.report["rows_without_wikidata"]
    (m,) = by[ac.P_MEMBER]
    assert m.subject_qid == "cliopatria:polity:alpha" and m.object_qid == "cliopatria:polity:grp_league"
    assert m.qualifiers["fields"] == ["Components", "MemberOf"]
    assert by[ac.P_SESHAT][0].value == {"type": "external_id", "value": "xx_alpha"}
    assert len(by[ac.P_AREA]) == 5 and by[ac.P_AREA][0].value["amount"] == "1000.5"
    ovs = {(s.subject_qid, s.object_qid): s for s in by[ac.P_OVERLAY]}
    a = ovs[("cliopatria:polity:alpha", "Q7")]  # QID resolved
    assert (a.start.iso, a.end.iso) == ("-0059-01-01T00:00:00Z", "+0010-01-01T00:00:00Z")
    assert a.qualifiers["share_of_country_area"]["max"] == "1.0000"
    assert a.qualifiers["derivation"] == "computed_spatial_overlay" and a.qualifiers["label"] == "derived"
    assert a.qualifiers["ne_raw_sha256"] == "b" * 64 and len(a.qualifiers["rows"]) == 3
    assert all(r["geometry_sha256"] for r in a.qualifiers["rows"])
    bb = ovs[("cliopatria:polity:alpha", "iso3:BBB")]  # no QID for BBB -> REVA node, entity created
    assert bb.qualifiers["share_of_country_area"]["max"] == "0.5000" and bb.qualifiers["country_node"].startswith("iso3")
    assert any(e.qid == "iso3:BBB" for e in b.entities)
    # every statement passes KGCORE's own normalisation (allow-list of the five predicates)
    kinds = {p.pid: p.value_kind for p in ac.load_predicates()}
    for i, s in enumerate(b.statements):
        _normalise_statement(i, s, ac.SOURCE_KEY, {"a" * 64}, kinds)
    # statement ids do not depend on the country node chosen (QID now, iso3 node before)
    b2 = ac.kg_records(sc, _layer(), raws={"cliopatria": "a" * 64, "naturalearth": "b" * 64}, country_qids={})
    k1 = {statement_key(s, ac.SOURCE_KEY) for s in b.statements}
    k2 = {statement_key(s, ac.SOURCE_KEY) for s in b2.statements}
    assert k1 == k2


def test_release_and_licence_checks() -> None:
    with pytest.raises(ac.CliopatriaError, match="release"):
        ac.scan(_zip([], member="cliopatria_polities_only_v030.geojson"), None)
    with pytest.raises(ac.CliopatriaError, match="member"):
        ac.scan(_zip([], member="other.geojson"), None)
    with pytest.raises(ac.CliopatriaError, match="CC BY 4.0"):
        ac.check_licence(b"All rights reserved")
    assert ac.check_licence(_fixture(ac.URL_LICENCE)) == "CC BY 4.0"


def test_spec_declares_budgeted_hosts_only() -> None:
    spec = ac.atlas_source()
    assert spec.source_key == spec.budget_key == "cliopatria.github"
    assert set(spec.allowed_hosts) == {"raw.githubusercontent.com", "naciscdn.org"}
    src = (REPO / "src/reva/adapters/connectors/atlas_cliopatria.py").read_text()
    src += (REPO / "src/reva/kg/overlay.py").read_text()
    for marker in ("SafeHttpClient(", "make_live_client(", "httpx.", "urlopen("):
        assert marker not in src
    reg = yaml.safe_load((REPO / "registry/sources/cliopatria.yaml").read_text())
    assert reg["source_key"] == spec.source_key and reg["rights"]["licence"] == "CC BY 4.0"


# ---------------------------------------------------------------------------------------------- real fixture
@pytest.fixture(scope="module")
def real() -> tuple[ac.Scan, ov.CountryLayer, ac.KgBuild]:
    layer = ov.country_layer_from_ne(_fixture(ac.URL_NE))
    sc = ac.scan(_fixture(ac.URL_ZIP), ov.Overlayer(layer))
    b = ac.kg_records(sc, layer, raws={"cliopatria": "a" * 64, "naturalearth": "b" * 64}, country_qids={})
    return sc, layer, b


def _runs(b: ac.KgBuild, polity: str, iso3: str) -> list[tuple[int, int, str]]:
    return sorted(
        (s.qualifiers["provider_years"][0], s.qualifiers["provider_years"][1], s.qualifiers["share_of_country_area"]["max"])
        for s in b.statements
        if s.property_pid == ac.P_OVERLAY and s.subject_qid == ac.entity_ids([polity])[polity] and s.object_qid == f"iso3:{iso3}"
    )


def test_real_fixture_parses(real) -> None:
    sc, layer, b = real
    assert sc.release == "v0.2.1" and not sc.problems
    assert layer.ne_version and {"BRA", "PRT", "DZA", "IND"} <= {t.key for t in layer.territories}
    assert b.report["statements"][ac.P_AREA] == len(sc.rows)
    assert min(r.from_year for r in sc.rows) < 0  # BCE rows present


def test_real_fixture_example_chains(real) -> None:
    _, _, b = real
    # Brazil <- Portuguese Empire, Algeria <- Ottoman Empire / French Algeria, India <- Mughal Empire / British Raj
    for polity, iso3 in (
        ("Portuguese Empire", "BRA"),
        ("Ottoman Empire", "DZA"),
        ("French Algeria", "DZA"),
        ("Mughal Empire", "IND"),
        ("British Raj", "IND"),
    ):
        runs = _runs(b, polity, iso3)
        assert runs, (polity, iso3)
    (d,) = {s.object_qid for s in b.statements if s.property_pid == ac.P_DENOTES and s.subject_qid.endswith(":portuguese_empire")}
    assert d == "Q200464"  # the provider's own Wikidata id
