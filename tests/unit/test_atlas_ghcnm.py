"""V4CLIMATE: GHCN-Monthly v4 stations, selected TAVG series and ADM1 placement (offline).

Fixtures (tests/fixtures/source/atlas.ghcnm): readme.txt and ghcnm-countries.txt as captured 2026-10-17 (NOAA NCEI,
US Government work); the QCF tarball and the Natural Earth admin-1 ZIP are *derived excerpts* cut from the real
files by ``cut_excerpt.py`` (real lines/features, unchanged; REVA_EXCERPT.json member; Natural Earth public domain).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from typing import Any

import pytest

from reva.adapters.connectors import atlas_ghcnm as gm
from reva.adapters.connectors.atlas_geo_common import load_country_qids
from reva.atlas.tables import validate_atlas_frame
from reva.evidence.runner import FixtureHttp
from reva.kernel.errors import SourceResponseError

T = datetime(2026, 10, 17, 13, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def files() -> dict[str, gm.Fetched]:
    http = FixtureHttp(gm.NAME)
    return {
        k: gm.Fetched(k, asyncio.run(http.get(u)).body, f"{i:064x}", u, T)
        for i, (k, u) in enumerate(gm.URLS.items(), start=1)
    }


@pytest.fixture(scope="module")
def parsed(files: dict[str, gm.Fetched]) -> dict[str, Any]:
    g = gm.read_tarball(files["tavg_qcf"].body, origin="test_fixture")
    recs, geoms, rep = gm.read_ne_admin1(files["ne_admin1"].body, origin="test_fixture")
    sel = gm.select_stations(g.stations)
    return {"g": g, "recs": recs, "geoms": geoms, "ne": rep, "sel": sel, "loc": gm.locate(g.stations, recs, geoms)}


def test_readme_quotes_and_excerpt_guard(files: dict[str, gm.Fetched]) -> None:
    assert gm.check_readme(files["readme"].body)["fips"] == "First two characters are FIPS country code"
    with pytest.raises(SourceResponseError, match="readme"):
        gm.check_readme(b"nothing here")
    with pytest.raises(SourceResponseError, match="excerpt"):
        gm.read_tarball(files["tavg_qcf"].body, origin="live")
    with pytest.raises(SourceResponseError, match="excerpt"):
        gm.read_ne_admin1(files["ne_admin1"].body, origin="live")


def test_inventory_counts_and_selection(parsed: dict[str, Any]) -> None:
    g, sel = parsed["g"], parsed["sel"]
    assert g.excerpt and g.version_dir.startswith("ghcnm.v4.")
    acw = g.stations["ACW00011604"]
    assert (acw.lat, acw.lon, acw.name) == ("57.7667", "11.8667", "SAVE")
    assert acw.valid > 0 and acw.first is not None and acw.last is not None
    # per FIPS code at most PER_COUNTRY stations, chosen by valid months (ties by id)
    per_cc: dict[str, list[str]] = {}
    for sid, why in sel.items():
        if any(w.startswith("country_top") for w in why):
            per_cc.setdefault(sid[:2], []).append(sid)
    assert per_cc and all(len(v) <= gm.PER_COUNTRY for v in per_cc.values())
    sw = sorted((s for s in g.stations.values() if s.id.startswith("SW") and s.valid), key=lambda s: (-s.valid, s.id))
    assert set(per_cc["SW"]) == {s.id for s in sw[: gm.PER_COUNTRY]}


def test_series_rows_validate(parsed: dict[str, Any]) -> None:
    g, sel = parsed["g"], parsed["sel"]
    rb = gm.series_rows(g, sel, "f" * 64)
    f, unmapped = validate_atlas_frame(
        rb.frame(), source=gm.ATLAS_SOURCE, dataset=gm.DATASET, raw_sha256s=["f" * 64], geo_scheme="iso3"
    )
    assert set(f["iso3"].to_list()) == {None} and {u["provider_geo"] for u in unmapped} == set(sel)
    acw = [r for r in rb.rows if r["provider_geo"] == "ACW00011604"]
    if acw:  # the record ranks within its FIPS code AC (it is alone there)
        jan61 = next(r for r in acw if r["period_start"] == date(1961, 1, 1))
        assert jan61["value"] == "-0.71"  # -71 hundredths as published
        fl = json.loads(jan61["flags"])
        assert fl["dsflag"] == "k" and fl["fips_country_code"] == "AC" and fl["country_iso3"] == "ATG"
    assert all(r["value"] != "-99.99" for r in rb.rows)  # -9999 never a value


def test_adm1_point_in_polygon_and_country_checks(parsed: dict[str, Any], files: dict[str, gm.Fetched]) -> None:
    loc, recs, g = parsed["loc"], parsed["recs"], parsed["g"]
    assert parsed["ne"]["excerpt"] and all(gm.ISO_3166_2_RE.match(a.code) for a in loc.values())
    # the AC-coded station lies in Sweden: located by geometry, but its FIPS country (Antigua) disagrees
    assert loc["ACW00011604"].code.startswith("SE-")
    (ents, sts), (a_ents, a_sts), rep = gm.kg_items(g, parsed["sel"], loc, recs, files, load_country_qids())
    by = {(s.subject_qid, s.property_pid): s for s in sts}
    assert ("ghcnm:ACW00011604", "ghcnm:in_adm1") not in by
    p17 = by[("ghcnm:ACW00011604", "P17")]
    assert p17.qualifiers["coordinates_fall_in"].startswith("SE-") and p17.qualifiers["fips"] == "AC"
    assert rep["adm1_country_mismatch"] >= 1
    sw = next(s for s in sts if s.property_pid == "ghcnm:in_adm1" and s.subject_qid.startswith("ghcnm:SW"))
    assert sw.object_qid.startswith("iso3166_2:SE-") and sw.qualifiers["derived"] == "point_in_polygon"
    assert {e.kind for e in ents} == {"weather_station"} and len(ents) == len(g.stations)
    assert {e.kind for e in a_ents} == {"region_adm1"}
    assert all(e.qid.startswith("iso3166_2:") for e in a_ents)
    pr = [s for s in a_sts if s.subject_qid == "iso3166_2:US-PR" and s.property_pid == "P17"]
    assert pr and pr[0].qualifiers["ne_iso_a2"] == "PR"  # Natural Earth's iso_a2 kept beside the ISO code prefix
    coords = by[("ghcnm:ACW00011604", "P625")].value
    assert coords == {"type": "globe_coordinate", "lat": "57.7667", "lon": "11.8667", "globe": "earth"}
