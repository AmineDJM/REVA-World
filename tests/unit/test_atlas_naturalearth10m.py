"""A-GEO: Natural Earth 1:10m geography (area, 1:10m coastline, borders, landlocked, exclaves, disputed areas).

Offline: the four large layers are a synthetic world (``tests/fixtures/source/atlas.naturalearth_10m/
synthetic_world.py``, layout in its docstring); the two small real layers (disputed areas, disputed boundary
lines) are captured provider responses served by ``FixtureHttp``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from reva.adapters.connectors import atlas_naturalearth10m as ne
from reva.adapters.connectors.atlas_geodesy import ellipsoid_zone_area_m2, line_length_m
from reva.atlas.tables import ATLAS_SCHEMA, validate_atlas_frame
from reva.evidence.runner import FixtureHttp
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import RawAcquisition

FIXDIR = Path(__file__).parents[1] / "fixtures" / "source" / "atlas.naturalearth_10m"
_spec = importlib.util.spec_from_file_location("ne_synthetic_world", FIXDIR / "synthetic_world.py")
sw = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(sw)  # type: ignore[union-attr]

KEYS = frozenset({"FRA", "VAT", "ESP", "DEU", "CHE", "ITA", "AUT", "KAZ", "RUS", "GBR", "UMI", "XKX", "FJI", "USA"})
T = datetime(2026, 10, 15, 22, 0, tzinfo=UTC)


def _raw(key: str, body: bytes) -> RawAcquisition:
    item = next(i for i in ne.fetch_items() if i.key == key)
    return RawAcquisition(
        item=item,
        body=body,
        media_type="application/zip",
        retrieved_at=T,
        response_metadata={},
        request_fingerprint="fp-" + key,
        origin="test_fixture",
    )


def _real_small_layers() -> dict[str, bytes]:
    http = FixtureHttp("atlas.naturalearth_10m")
    out = {}
    for it in ne.fetch_items():
        if it.key in ("disputed_areas", "boundary_lines_disputed"):
            out[it.key] = asyncio.run(http.get(it.url)).body
    return out


@pytest.fixture(scope="module")
def raws() -> dict[str, RawAcquisition]:
    bodies = {**sw.layers(), **_real_small_layers()}
    return {k: _raw(k, b) for k, b in bodies.items()}


@pytest.fixture(scope="module")
def built(raws: dict[str, RawAcquisition]) -> ne.NeBuild:
    shas = {k: f"{i:064x}" for i, k in enumerate(raws, start=1)}
    return ne.build(raws, shas, keys=KEYS)


def _rows(b: ne.NeBuild, ds: str, key: str, indicator: str) -> list[dict[str, Any]]:
    return [r for r in b.rows[ds] if (r["iso3"] or r["provider_geo"]) == key and r["indicator"] == indicator]


def _one(b: ne.NeBuild, ds: str, key: str, indicator: str) -> dict[str, Any]:
    rs = _rows(b, ds, key, indicator)
    assert len(rs) == 1, (key, indicator, rs)
    return rs[0]


def _len_km(*pts: tuple[float, float]) -> float:
    d = sw._dense(list(pts))
    return line_length_m([p[0] for p in d], [p[1] for p in d]) / 1e3


# ----------------------------------------------------------------------------- mapping rules
def test_entity_assignment_rules() -> None:
    keys = {"FRA", "UMI", "USA", "GBR", "XKX", "KAZ"}
    assert ne.assign_entity({"ISO_A3": "FRA", "ADM0_A3": "FRA", "ISO_A3_EH": "FRA"}, keys) == ("FRA", "ISO_A3")
    # ADM0_A3 wins over ISO_A3_EH: Johnston Atoll is UMI, not USA
    assert ne.assign_entity({"ISO_A3": "-99", "ADM0_A3": "UMI", "ISO_A3_EH": "USA"}, keys) == ("UMI", "ADM0_A3")
    assert ne.assign_entity({"ISO_A3": "-99", "ADM0_A3": "KAB", "ISO_A3_EH": "KAZ"}, keys) == ("KAZ", "ISO_A3_EH")
    assert ne.assign_entity({"ISO_A3": "-99", "ADM0_A3": "KOS", "ISO_A3_EH": "-99"}, keys) == ("XKX", "reviewed_alias")
    # never merged into a claimant: Somaliland stays unmapped
    assert ne.assign_entity({"ISO_A3": "-99", "ADM0_A3": "SOL", "ISO_A3_EH": "-99"}, keys) == (None, "unmapped")


# ----------------------------------------------------------------------------- geometry results
def test_areas_match_closed_forms(built: ne.NeBuild) -> None:
    c = built.computation
    z = ellipsoid_zone_area_m2
    assert c.area_m2["VAT"] == pytest.approx(z(1, 2, 1), rel=1e-9)
    fra = z(0, 4, 4) - z(1, 2, 1) - z(2.5, 3.5, 1) + z(0, 1, 1)  # main box - two holes + coastal exclave
    assert c.area_m2["FRA"] == pytest.approx(fra, rel=1e-9)
    assert c.lakes_m2["ESP"] == pytest.approx(z(1, 2, 1), rel=1e-9)
    assert float(_one(built, ne.DS_COUNTRY, "VAT", "area_km2")["value"]) == pytest.approx(z(1, 2, 1) / 1e6, abs=0.005)


def test_coastline_is_unshared_boundary_and_labelled_with_scale(built: ne.NeBuild) -> None:
    c = built.computation
    # FRA main box: west and south edges are coast; coastal exclave [8,9]x[0,1]: south, east, north edges.
    want = (
        _len_km((0, 0), (0, 4))
        + _len_km((0, 0), (4, 0))
        + _len_km((8, 0), (9, 0))
        + _len_km((9, 0), (9, 1))
        + _len_km((8, 1), (9, 1))
    )
    assert c.coast_m["FRA"] / 1e3 == pytest.approx(want, rel=1e-9)
    row = _one(built, ne.DS_COUNTRY, "FRA", "coastline_length_km|scale=ne_1:10m")
    flags = json.loads(row["flags"])
    assert flags["scale_dependent"] is True and flags["scale"] == "1:10,000,000"
    # antimeridian cut edges are not coast: RUS = two boxes minus the two cut meridian edges
    rus = (
        _len_km((178, 60), (180, 60))
        + _len_km((178, 62), (180, 62))
        + _len_km((178, 60), (178, 62))
        + _len_km((-180, 60), (-178, 60))
        + _len_km((-180, 62), (-178, 62))
        + _len_km((-178, 60), (-178, 62))
    )
    assert c.coast_m["RUS"] / 1e3 == pytest.approx(rus, rel=1e-9)
    assert c.diagnostics["artificial_cut_km_excluded"] > 0


def test_landlocked_and_caspian(built: ne.NeBuild) -> None:
    che = _one(built, ne.DS_COUNTRY, "CHE", "landlocked")
    assert che["value_text"] == "yes" and json.loads(che["flags"])["doubly_landlocked"] is False
    kaz = _one(built, ne.DS_COUNTRY, "KAZ", "landlocked")
    assert kaz["value_text"] == "yes" and json.loads(kaz["flags"])["caspian_only_shore"] is True
    assert _rows(built, ne.DS_COUNTRY, "KAZ", "caspian_shore_km|scale=ne_1:10m")
    assert _one(built, ne.DS_COUNTRY, "DEU", "landlocked")["value_text"] == "no"
    assert _one(built, ne.DS_COUNTRY, "VAT", "landlocked")["value_text"] == "yes"


def test_borders_typed_by_boundary_lines(built: ne.NeBuild) -> None:
    b = built.computation.borders
    fra_esp = b[("ESP", "FRA")]
    assert fra_esp.by_class_m["international"] / 1e3 == pytest.approx(_len_km((4, 0), (4, 4)), rel=1e-9)
    # the enclosed ESP exclave and the coastal FRA exclave also touch: no NE line there -> untyped, kept
    assert fra_esp.by_class_m["untyped"] > 0 and not fra_esp.disputed
    assert b[("CHE", "FRA")].disputed and set(b[("CHE", "FRA")].by_class_m) == {"disputed"}
    assert b[("CHE", "ESP")].water_m == pytest.approx(b[("CHE", "ESP")].length_m)
    assert set(b[("AUT", "DEU")].by_class_m) == {"untyped"}
    # map units of one country (England/Scotland) share an internal edge: no border, no coast there
    assert not [k for k in b if "GBR" in k]
    row = _one(built, ne.DS_BORDERS, "CHE", "land_border_km|partner=FRA")
    flags = json.loads(row["flags"])
    assert flags["disputed"] is True and flags["by_class_km"] == {
        "disputed": pytest.approx(_len_km((2, 4), (4, 4)), abs=0.01)
    }
    # both directions are emitted
    assert _one(built, ne.DS_BORDERS, "FRA", "land_border_km|partner=CHE")["value"] == row["value"]


def test_unmapped_feature_kept_not_merged(built: ne.NeBuild) -> None:
    sol = _one(built, ne.DS_COUNTRY, "SOL", "area_km2")
    assert sol["iso3"] is None and json.loads(sol["flags"])["unmapped_feature"] is True
    esp = _one(built, ne.DS_BORDERS, "ESP", "land_border_km|partner=SOL")
    assert json.loads(esp["flags"])["partner_unmapped_ne_feature"] == "SOL"
    back = _one(built, ne.DS_BORDERS, "SOL", "land_border_km|partner=ESP")
    assert back["iso3"] is None and back["provider_geo"] == "SOL"
    count = _one(built, ne.DS_COUNTRY, "ESP", "land_neighbours_count")
    assert json.loads(count["flags"])["unmapped_ne_neighbours"] == ["SOL"]


def test_exclaves_enclaves_islands_and_antimeridian(built: ne.NeBuild) -> None:
    assert _one(built, ne.DS_COUNTRY, "VAT", "enclave_of")["value_text"] == "FRA"
    esp_ex = _rows(built, ne.DS_COUNTRY, "ESP", "exclave_area_km2|component=2")  # rank by area: main, island, exclave
    assert len(esp_ex) == 1
    f = json.loads(esp_ex[0]["flags"])
    assert f["exclave_kind"] == "enclosed" and f["enclave_of"] == "FRA"
    fra_ex = json.loads(_one(built, ne.DS_COUNTRY, "FRA", "exclave_area_km2|component=1")["flags"])
    assert fra_ex["exclave_kind"] == "coastal" and list(fra_ex["neighbours_km"]) == ["ESP"]
    # the ESP island is a component, not an exclave
    assert _one(built, ne.DS_COUNTRY, "ESP", "land_components_count")["value"] == "3"
    assert not _rows(built, ne.DS_COUNTRY, "ESP", "exclave_area_km2|component=1")
    # split at the antimeridian, joined again; England + Scotland = one piece
    assert _one(built, ne.DS_COUNTRY, "RUS", "land_components_count")["value"] == "1"
    assert _one(built, ne.DS_COUNTRY, "GBR", "land_components_count")["value"] == "1"


def test_atlas_frames_validate_against_atlascore_schema(built: ne.NeBuild) -> None:
    for ds, rows in built.rows.items():
        frame = pl.DataFrame(rows, schema=ATLAS_SCHEMA)
        f, unmapped = validate_atlas_frame(
            frame, source="naturalearth", dataset=ds, raw_sha256s=sorted(set(frame["raw_sha256"].to_list()))
        )
        assert f.height == len(rows)
        assert all(u["rows"] > 0 for u in unmapped)
    geos = {r["provider_geo"] for r in built.rows[ne.DS_COUNTRY] if r["iso3"] == "GBR"}
    assert geos == {"ENG+SCT"}


def test_real_disputed_areas_rows(built: ne.NeBuild) -> None:
    rows = built.rows[ne.DS_DISPUTED]
    assert len(rows) >= 60
    crimea = [r for r in rows if json.loads(r["flags"]).get("ne_name") == "Crimea"]
    assert len(crimea) == 1
    fl = json.loads(crimea[0]["flags"])
    assert "Claimed by Ukraine" in fl["ne_note"] and fl["ne_view"].startswith("default (de facto)")
    assert float(crimea[0]["value"]) > 20_000


def test_adjacency_statements() -> None:
    pytest.importorskip("reva.kg.model")
    bodies = {**sw.layers(), **_real_small_layers()}
    layers = {k: ne.read_layer(b, ne.LAYERS[k][0]) for k, b in bodies.items()}
    comp = ne.compute(layers, KEYS)
    qids = {"FRA": "Q142", "ESP": "Q29", "CHE": "Q39", "DEU": "Q183", "ITA": "Q38", "AUT": "Q40"}
    sts, info = ne.adjacency_statements(comp, qids, raw_sha256="a" * 64, source_revision="9.9.9", retrieved_at=T)
    pairs = {(s.subject_qid, s.object_qid) for s in sts}
    assert ("Q142", "Q29") in pairs and ("Q29", "Q142") in pairs
    s = next(s for s in sts if (s.subject_qid, s.object_qid) == ("Q39", "Q142"))
    assert s.property_pid == "P47" and s.qualifiers["disputed_boundary"] is True
    assert s.qualifiers["border_length_km"]["type"] == "quantity"
    assert isinstance(s.qualifiers["border_length_km"]["amount"], str)
    assert s.statement_id == "ne10m-border:CHE:FRA" and s.source_key == ne.SOURCE_KEY
    assert info["skipped_pairs"]["unmapped_ne_feature"] == 1  # ESP-SOL
    assert info["skipped_pairs"]["no_qid"] >= 1  # VAT has no QID in this map


def test_topology_violation_is_refused() -> None:
    import io
    import zipfile

    import shapefile

    def zip_of(polys: list[list[list[tuple[float, float]]]]) -> bytes:
        shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
        w = shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shapefile.POLYGON)
        for f in sw.MU_FIELDS:
            w.field(f, "C", size=20)
        for i, rings in enumerate(polys):
            w.poly(rings)
            w.record(*["Admin-0 map unit", f"A{i}", f"A{i}", f"A{i}", f"A{i}", "Country", f"A{i}", f"A{i}"])
        w.close()
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            for ext, b in (("shp", shp), ("shx", shx), ("dbf", dbf)):
                z.writestr(f"x.{ext}", b.getvalue())
        return out.getvalue()

    box = sw.box_cw(0, 0, 1, 1)
    layer = ne.read_layer(zip_of([[box], [box], [box]]), "x")  # one edge used by three polygons
    empty = ne.read_layer(sw.layers()["boundary_lines_land"], "l")
    with pytest.raises(SourceResponseError, match="topology"):
        ne.compute(
            {
                "map_units": layer,
                "boundary_lines_land": empty,
                "boundary_lines_disputed": empty,
                "countries": layer,
                "countries_lakes": layer,
            },
            {"A0"},
        )


def test_connector_blocks_schema_drift_and_truncation(raws: dict[str, RawAcquisition]) -> None:
    conn = ne.NaturalEarth10mConnector()
    spec = conn.describe()
    assert spec.template == "atlas_bulk" and spec.allowed_hosts == ("naciscdn.org",)
    batch = next(iter(conn.parse(raws["disputed_areas"])))
    assert not conn.validate(batch).blocking
    small = raws["boundary_lines_land"].model_copy(
        update={"item": next(i for i in ne.fetch_items() if i.key == "map_units")}
    )
    with pytest.raises(SourceResponseError):
        ne.build({**raws, "map_units": small}, {k: "0" * 64 for k in raws}, keys=KEYS)
    with pytest.raises(SourceResponseError, match="zip"):
        ne.read_layer(b"not a zip", "x")


# ----------------------------------------------------------------------------- datasets and fixture excerpt
def _fixture_raws(origin: str = "test_fixture") -> dict[str, RawAcquisition]:
    from reva.adapters.connectors.atlas_geo_common import fetch_raw

    http = FixtureHttp(ne.CONNECTOR_NAME)
    raws = {it.key: asyncio.run(fetch_raw(it, http)) for it in ne.fetch_items()}
    return {k: r.model_copy(update={"origin": origin}) for k, r in raws.items()}


def test_dataset_runs_fetch_only_the_layers_they_need() -> None:
    assert [i.key for i in ne.fetch_items()] == list(ne.LAYERS)
    assert [i.key for i in ne.fetch_items([ne.DS_DISPUTED])] == ["disputed_areas"]
    assert [i.key for i in ne.fetch_items([ne.DS_COUNTRY])] == ["map_units", "countries", "countries_lakes"]
    assert [i.key for i in ne.fetch_items([ne.DS_BORDERS])] == [
        "map_units",
        "boundary_lines_land",
        "boundary_lines_disputed",
    ]
    with pytest.raises(SourceResponseError):
        ne.fetch_items(["nope"])


def test_fixture_excerpt_is_real_closed_and_flagged() -> None:
    """The committed fixture: real 1:10m features of closed country groups (cut_excerpt.py); every table
    built from an excerpt says so, the full real disputed layer does not."""
    raws = _fixture_raws()
    shas = {k: f"{i:064x}" for i, k in enumerate(raws, start=1)}
    nb = ne.build(raws, shas)
    assert nb.excerpt_layers == ["boundary_lines_land", "countries", "countries_lakes", "map_units"]
    assert nb.ne_version == "5.1.1"
    c = nb.computation
    assert c is not None and c.diagnostics["unmapped_units"] == ["CNM", "CYN", "ESB", "WSB"]
    assert set(c.borders) >= {("GBR", "IRL"), ("DOM", "HTI"), ("MAF", "SXM"), ("CYP", "ne:CNM")}
    assert 360 < c.borders[("GBR", "IRL")].length_m / 1e3 < 380
    irl = c.borders[("GBR", "IRL")]
    assert irl.by_class_m["international"] / irl.length_m > 0.95  # typed by the real boundary line
    assert c.borders[("ne:CNM", "ne:CYN")].disputed  # line of control
    assert 69_000 < c.area_m2["IRL"] / 1e6 < 70_000
    for ds in (ne.DS_COUNTRY, ne.DS_BORDERS):
        assert all("derived excerpt" in json.loads(r["flags"])["ne_excerpt"] for r in nb.rows[ds])
    assert not any("ne_excerpt" in json.loads(r["flags"]) for r in nb.rows[ne.DS_DISPUTED])
    assert len(nb.rows[ne.DS_DISPUTED]) >= 60


def test_excerpt_is_refused_from_a_live_acquisition() -> None:
    raws = _fixture_raws(origin="live")
    with pytest.raises(SourceResponseError, match="excerpt marker in a live layer"):
        ne.build(raws, {k: "0" * 64 for k in raws})
    # the real (non-excerpt) small layers stay accepted from live
    assert ne.build(raws, {k: "0" * 64 for k in raws}, datasets=[ne.DS_DISPUTED]).rows[ne.DS_DISPUTED]


def test_dataset_build_equals_the_full_build_for_that_dataset(raws: dict[str, RawAcquisition]) -> None:
    shas = {k: f"{i:064x}" for i, k in enumerate(raws, start=1)}
    full = ne.build(raws, shas, keys=KEYS)
    for ds in (ne.DS_COUNTRY, ne.DS_BORDERS, ne.DS_DISPUTED):
        need = {k: raws[k] for k in ne.layers_for([ds])}
        part = ne.build(need, {k: shas[k] for k in need}, keys=KEYS, datasets=[ds])
        assert list(part.rows) == [ds] and part.rows[ds] == full.rows[ds]
    with pytest.raises(SourceResponseError, match="missing"):
        ne.build({"disputed_areas": raws["disputed_areas"]}, shas, keys=KEYS, datasets=[ne.DS_COUNTRY])
