"""KGGEOPOL connectors (UCDP, UNGA votes, CShapes) on trimmed recorded fixtures -- offline, no database.

Fixtures (``tests/fixtures/source/atlas.{ucdp,unga_votes,cshapes}``) are slices of the real downloads of
2026-10-15 in the providers' own formats (see each manifest's ``note``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from reva.adapters.connectors import atlas_cshapes as CS
from reva.adapters.connectors import atlas_geopol_common as G
from reva.adapters.connectors import atlas_ucdp as U
from reva.adapters.connectors import atlas_unga_votes as V
from reva.atlas.tables import validate_atlas_frame
from reva.evidence.runner import FixtureHttp
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import FetchItem, RawAcquisition

ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "source"


def _raw(name: str, url: str, key: str = "k") -> RawAcquisition:
    r = asyncio.run(FixtureHttp(name).get(url))
    return RawAcquisition(
        item=FetchItem(key=key, url=url, max_bytes=10**8),
        body=r.body,
        media_type="x",
        retrieved_at=r.retrieved_at,
        response_metadata={},
        request_fingerprint="x",
        origin="test_fixture",
    )


@pytest.fixture(scope="module")
def crosswalk() -> G.StateCodeCrosswalk:
    body = _raw("atlas.ucdp", G.COUNTRYCODE_URL).body
    return G.crosswalk_from_countrycode_tarball(body, G.crosswalk_keys())


# ----------------------------------------------------------------------------- state codes


def test_state_code_policy(crosswalk: G.StateCodeCrosswalk) -> None:
    r = crosswalk.resolve
    assert r("cow", 2, date(1950, 1, 1)).iso3 == "USA"
    ussr = r("cow", 365, date(1960, 1, 1))
    assert ussr.iso3 is None and ussr.qid == "Q15180" and ussr.reason == "historical_qid"
    assert r("cow", 365, date(1995, 1, 1)).iso3 == "RUS"
    # GW and COW disagree on 970 (Kiribati vs Nauru): never guessed from the number alone
    assert r("gw", 970, date(2000, 1, 1)).iso3 == "KIR"
    assert r("cow", 970, date(2000, 1, 1)).iso3 == "NRU"
    # documented continuations
    assert r("gw", 678, date(1995, 1, 1)).iso3 == "YEM"
    assert r("gw", 678, date(1980, 1, 1)).iso3 is None
    assert r("cow", 345, date(2010, 1, 1)).iso3 == "SRB"
    assert r("ucdp", 345, date(2010, 1, 1)).iso3 == "SRB"
    assert r("gw", 345, date(2010, 1, 1)).iso3 is None  # GW uses 340 for Serbia
    # COW 260 after unification is not DEU (COW codes unified Germany 255): no double mapping
    assert r("cow", 260, date(1991, 1, 1)).iso3 is None
    assert r("cow", 255, date(1995, 1, 1)).iso3 == "DEU"
    assert r("gw", 99999, date(2000, 1, 1)).reason == "unmapped"


def test_resolver_uses_qids_and_code_entities(crosswalk: G.StateCodeCrosswalk) -> None:
    res = G.StateEntityResolver(crosswalk, {"FRA": "Q142"})
    assert res.entity("cow", 220, date(1960, 1, 1)) == "Q142"
    assert res.entity("cow", 365, date(1960, 1, 1)) == "Q15180"
    assert res.entity("cow", 2, date(1960, 1, 1)) == "cow:2"  # no QID map entry -> code entity
    sts = G.code_correspondence(res, raw_sha256="0" * 64)
    assert [(s.subject, s.value["value"]) for s in sts] == [("cow:2", "USA")]


# ----------------------------------------------------------------------------- UCDP


@pytest.fixture(scope="module")
def ucdp(crosswalk: G.StateCodeCrosswalk) -> dict[str, Any]:
    raws = {it.key: _raw("atlas.ucdp", it.url, it.key) for it in U.fetch_items()}
    return U.build(
        raws,
        crosswalk=crosswalk,
        qid_for_iso3={"FRA": "Q142", "IND": "Q668", "PAK": "Q843"},
        countrycode_sha256="c" * 64,
        strict=False,
    )


def test_ucdp_orgvio_cy_values_verbatim(ucdp: dict[str, Any]) -> None:
    rows = ucdp["orgvio_cy"]
    f, unmapped = validate_atlas_frame(
        G.atlas_frame(rows), source="ucdp", dataset="orgvio_cy", raw_sha256s=[ucdp["shas"]["orgvio_cy"]]
    )
    assert f.height == len(rows) > 100
    afg89 = [
        r
        for r in rows
        if r["iso3"] == "AFG" and r["period_start"] == date(1989, 1, 1) and r["indicator"] == "sb_total_deaths_best"
    ]
    assert [r["value"] for r in afg89] == ["5174"]  # CSV row: Afghanistan 1989 sb_total_deaths_best
    ussr = [r for r in rows if r["provider_geo"] == "gw:365" and r["period_start"].year == 1989]
    assert ussr and all(r["iso3"] is None and r["entity_qid"] == "Q15180" for r in ussr)
    srb = {r["iso3"] for r in rows if r["provider_geo"] == "gw:345" and r["period_start"].year >= 2007}
    assert srb == {"SRB"}
    zero = [r for r in rows if r["value"] == "0" and r["unit"] == "deaths"]
    assert zero and all("zero_semantics" in json.loads(r["flags"]) for r in zero)
    assert {u["provider_geo"] for u in unmapped} >= {"gw:365", "gw:713"}


def test_ucdp_acd_counts_are_labelled_computed(ucdp: dict[str, Any]) -> None:
    rows = ucdp["acd_cy"]
    validate_atlas_frame(G.atlas_frame(rows), source="ucdp", dataset="acd_cy", raw_sha256s=[ucdp["shas"]["acd"]])
    fra = [
        r
        for r in rows
        if r["iso3"] == "FRA"
        and r["indicator"] == "acd_conflicts_primary_party"
        and r["period_start"] == date(1956, 1, 1)
    ]
    assert len(fra) == 1
    flags = json.loads(fra[0]["flags"])
    assert "computed_from" in flags and set(flags["conflict_ids"]) >= {"244", "246"}


def test_ucdp_kg_records(ucdp: dict[str, Any]) -> None:
    sts = ucdp["statements"]
    by = Counter(s.property for s in sts)
    assert by["ucdp:battle_related_deaths"] > 0 and by["ucdp:intensity_level"] > 0
    alg = [s for s in sts if s.subject == "ucdp:conflict:246"]
    party = {(s.property, s.object_qid) for s in alg}
    assert ("ucdp:primary_party", "Q142") in party  # France, extrasystemic war in Algeria
    assert ("ucdp:nonstate_side", "ucdp:actor:403") in party
    ctype = [s.value["value"] for s in alg if s.property == "ucdp:conflict_type"]
    assert ctype == ["extrasystemic"]
    ent = {e.qid: e for e in ucdp["entities"]}
    assert ent["ucdp:actor:403"].labels["en"] == "FLN"
    # every statement points at its raw file and row(s)
    assert all(s.raw_sha256 in ucdp["shas"].values() or s.raw_sha256 == "c" * 64 for s in sts)
    _ents, msts = G.to_kg_model(ucdp["entities"], sts, source_key=U.KG_SOURCE_KEY, source_revision="ucdp-26.1")
    assert len(msts) == len(sts) and all(m.statement_id for m in msts)
    assert set(by) <= set(G.GEOPOL_PROPERTIES)


def test_ucdp_truncation_guard() -> None:
    body = _raw("atlas.ucdp", U.BASE + U.FILES["acd"][0]).body
    with pytest.raises(SourceResponseError):
        U.parse_file("acd", body)  # strict: the trimmed slice is below the expected row count
    assert U.parse_file("acd", body, strict=False)


# ----------------------------------------------------------------------------- UNGA


@pytest.fixture(scope="module")
def unga(crosswalk: G.StateCodeCrosswalk) -> dict[str, Any]:
    raws = {it.key: _raw("atlas.unga_votes", it.url, it.key) for it in V.fetch_items()}
    return V.build(
        raws,
        crosswalk=crosswalk,
        qid_for_iso3={"USA": "Q30", "FRA": "Q142", "GBR": "Q145"},
        countrycode_sha256="c" * 64,
        strict=False,
    )


def test_unga_tables_validate(unga: dict[str, Any]) -> None:
    for ds, rows in (("votes", unga["votes"]), ("rollcalls", unga["rollcalls"]), ("vote_shares", unga["shares"])):
        validate_atlas_frame(G.atlas_frame(rows), source="unga", dataset=ds, raw_sha256s=[unga["sha"]])
    assert unga["meta"]["licence"] == "CC0 1.0"


def test_unga_provider_defects_are_flagged_not_counted(unga: dict[str, Any]) -> None:
    st = unga["stats"]
    assert st["cells_conflicting"] >= 1 and st["not_member_rows_skipped"] >= 1
    yug = [r for r in unga["votes"] if r["provider_geo"] == "cow:345" and r["indicator"] == "vote|rcid=5640"]
    assert len(yug) == 1
    flags = json.loads(yug[0]["flags"])
    assert flags["provider_status"] in ("duplicate_identical", "conflicting") and len(flags["provider_rows"]) == 3
    kg_keys = {s.key for s in unga["statements"]}
    bad = [r for r in unga["votes"] if json.loads(r["flags"]).get("provider_status") in ("conflicting", "inconsistent")]
    assert bad
    for r in bad:
        rcid = json.loads(r["flags"])["rcid"]
        assert f"{rcid}:{r['provider_geo'].replace(':', '')}" not in kg_keys


def test_unga_shares_recomputed_from_votes(unga: dict[str, Any]) -> None:
    """share_yes equals yes/(yes+no+abstain) over the clean vote cells of the same country/issue/decade."""
    co_rcids = {
        json.loads(r["flags"])["rcid"] for r in unga["rollcalls"] if "co" in json.loads(r["flags"])["issue_codes"]
    }
    votes = [
        r
        for r in unga["votes"]
        if r["iso3"] == "USA"
        and r["period_start"].year // 10 == 196
        and json.loads(r["flags"])["rcid"] in co_rcids
        and r["value_text"] in ("yes", "no", "abstain")
    ]
    assert votes
    n_yes = sum(r["value_text"] == "yes" for r in votes)
    share = [
        r
        for r in unga["shares"]
        if r["iso3"] == "USA"
        and r["indicator"] == "share_yes|issue=co|grain=decade"
        and r["period_start"] == date(1960, 1, 1)
    ]
    assert len(share) == 1
    assert abs(float(share[0]["value"]) - n_yes / len(votes)) < 1e-6
    flags = json.loads(share[0]["flags"])
    assert "computed_from" in flags and "issue_code_caveat" in flags and flags["n_recorded"] == len(votes)


def test_unga_kg_votes(unga: dict[str, Any]) -> None:
    us = [s for s in unga["statements"] if s.subject == "Q30" and s.property.startswith("unga:voted_")]
    assert us and all(s.object_qid.startswith("unga:rcid:") and s.pit_precision == 11 for s in us)
    ussr = [s for s in unga["statements"] if s.subject == "Q15180"]
    assert ussr  # COW 365 before 1991-12-21 -> Soviet Union (historical QID)
    ents = {e.qid for e in unga["entities"]}
    assert {f"unga:rcid:{json.loads(r['flags'])['rcid']}" for r in unga["rollcalls"]} <= ents


def test_unga_licence_and_checksum_drift() -> None:
    meta = _raw("atlas.unga_votes", V.VERSION_URL).body
    votes = _raw("atlas.unga_votes", V.FILE_URL.format(V.VOTES_FILE_ID)).body
    V.check_version(meta, votes)
    with pytest.raises(SourceResponseError):
        V.check_version(meta, votes + b"x")
    doc = json.loads(meta)
    doc["data"]["license"]["name"] = "CC BY-NC 4.0"
    with pytest.raises(SourceResponseError):
        V.check_version(json.dumps(doc).encode(), votes)


# ----------------------------------------------------------------------------- CShapes


@pytest.fixture(scope="module")
def cshapes(crosswalk: G.StateCodeCrosswalk) -> dict[str, Any]:
    body = _raw("atlas.cshapes", CS.PACKAGE_URL).body
    return CS.build(body, crosswalk=crosswalk, qid_for_iso3={"FRA": "Q142"}, countrycode_sha256="c" * 64, strict=False)


def test_cshapes_atlas_and_polygons(cshapes: dict[str, Any]) -> None:
    for ds, rows in cshapes["atlas"].items():
        validate_atlas_frame(G.atlas_frame(rows), source="cshapes", dataset=ds, raw_sha256s=[cshapes["sha"]])
    t = cshapes["polygons"]
    assert t.num_rows == len(cshapes["polities"]["gw"]) + len(cshapes["polities"]["cow"])
    g = json.loads(t.column("geometry_geojson")[0].as_py())
    assert g["type"] in ("Polygon", "MultiPolygon")
    alg = [r for r in cshapes["atlas"]["polities_gw"] if r["provider_geo"] == "gw:615" and r["indicator"] == "status"]
    assert {r["value_text"] for r in alg} >= {"colony", "independent"}
    dep = [r for r in alg if r["value_text"] == "colony"]
    assert all("iso3_meaning" in json.loads(r["flags"]) for r in dep if r["iso3"])


def test_cshapes_kg_colonial_rule_and_borders(cshapes: dict[str, Any]) -> None:
    sts = cshapes["statements"]
    ruled = [s for s in sts if s.subject == "cshapes:gw:615" and s.property == "cshapes:ruled_by"]
    assert ruled and {s.object_qid for s in ruled} == {"Q142"}
    assert max(s.end for s in ruled) < "1963"  # Algeria independent in 1962
    borders = {
        (s.subject, s.object_qid) for s in sts if s.property == "cshapes:shares_border_with" and s.end >= "2019-12-31"
    }
    assert ("cshapes:gw:211", "cshapes:gw:220") in borders  # Belgium - France
    assert ("cshapes:gw:220", "cshapes:gw:230") in borders  # France - Spain
    assert ("cshapes:gw:220", "cshapes:gw:615") not in borders  # no land border across the Mediterranean


def test_cshapes_adjacency_merges_consecutive_periods() -> None:
    def pol(code: int, fid: int, s: str, e: str, arcs: set[int]) -> CS.Polity:
        return CS.Polity(
            "gw",
            code,
            fid,
            str(code),
            date.fromisoformat(s),
            date.fromisoformat(e),
            "independent",
            None,
            None,
            None,
            None,
            1,
            frozenset(arcs),
            {},
            fid,
        )

    ps = [
        pol(1, 1, "1900-01-01", "1949-12-31", {1}),
        pol(1, 2, "1950-01-01", "2000-12-31", {1}),
        pol(2, 3, "1900-01-01", "2000-12-31", {1}),
        pol(3, 4, "1990-01-01", "2000-12-31", {9}),
    ]
    adj = CS.adjacency(ps)
    assert [(a, b, lo.isoformat(), hi.isoformat()) for a, b, lo, hi, _f in adj] == [(1, 2, "1900-01-01", "2000-12-31")]


def test_fixture_manifests_are_hash_pinned() -> None:
    for name in ("atlas.ucdp", "atlas.unga_votes", "atlas.cshapes"):
        for e in json.loads((ROOT / name / "manifest.json").read_text()):
            assert hashlib.sha256((ROOT / name / e["file"]).read_bytes()).hexdigest() == e["sha256"]
            assert e["licence"] and e["note"]
