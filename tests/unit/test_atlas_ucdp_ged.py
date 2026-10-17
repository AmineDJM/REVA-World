"""V6CONFLICT: UCDP GED / Candidate / Non-State / One-Sided connector, offline on the captured fixture slices."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import polars as pl
import pytest
import yaml

from reva.adapters.connectors import atlas_ucdp_ged as g
from reva.adapters.connectors.atlas_geopol_common import (
    StateEntityResolver,
    crosswalk_from_countrycode_tarball,
    crosswalk_keys,
    read_csv_member,
)
from reva.atlas.tables import validate_atlas_frame

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "source" / "atlas.ucdp_ged"


def _body(url_part: str) -> bytes:
    man = json.loads((FIX / "manifest.json").read_text())
    (e,) = [e for e in man if e["url"].endswith(url_part)]
    return (FIX / e["file"]).read_bytes()


@pytest.fixture(scope="module")
def world() -> dict[str, Any]:
    cw = crosswalk_from_countrycode_tarball(_body("countrycode_1.9.0.tar.gz"), crosswalk_keys())
    idx = g.adm1_index(_body("ne_10m_admin_1_states_provinces.zip"), origin="test_fixture")
    res = g.Resolver(cw)
    ged = g.read_ged(_body("ged261-csv.zip"), member=g.GED_MEMBER)
    p = g.prepare(ged, resolver=res, idx=idx, sha="a" * 64, file=g.GED_MEMBER, candidate=False)
    cands = g.discover_candidates(_body("/downloads/"))
    frames = [(c, g.read_ged(_body(c.name), member=None), f"{i:064d}") for i, c in enumerate(cands)]
    return {"cw": cw, "idx": idx, "res": res, "p": p, "cands": cands, "frames": frames}


def test_candidate_discovery_order(world: dict[str, Any]) -> None:
    names = [c.name for c in world["cands"]]
    assert names == ["GEDEvent_v26_01_26_06.csv", "GEDEvent_v26_0_7.csv", "GEDEvent_v26_0_8.csv"]
    assert world["cands"][0].kind == "quarterly" and world["cands"][0].end_month == 6
    html = b'<a href="https://ucdp.uu.se/downloads/candidateged/GEDEvent_v26_01_26_09.csv">' + _body("/downloads/")
    later = g.discover_candidates(html)  # a newer quarterly re-release supersedes the monthly files it covers
    assert [c.name for c in later] == ["GEDEvent_v26_01_26_09.csv"]


def test_ged_parse_and_checks(world: dict[str, Any]) -> None:
    p = world["p"]
    assert p.df.height == 168 and p.df["id"].n_unique() == 168
    assert p.checks["best_not_sum_of_parts"] == 0
    assert p.checks["spanning_months"] >= 1


def test_adm1_placement_rules(world: dict[str, Any]) -> None:
    p = world["p"]
    st = Counter(p.adm1_status)
    assert st["placed"] > 50
    for wp in ("5", "6", "7"):
        assert st[f"not_placed:where_prec_{wp}"] >= 1
        assert all(c is None for c, s in zip(p.adm1, p.adm1_status, strict=True) if s.endswith(f"_{wp}"))
    gaza = [c for c, gw in zip(p.adm1, p.df["country_id"].to_list(), strict=True) if gw == "666" and c]
    assert gaza and all(c.startswith("PS-") for c in gaza)  # geometry decides; GED's Israel coding is kept in flags
    assert st["placed:ged_country_differs"] >= len(gaza)
    assert st["placed:ged_country_historical_or_unmapped"] >= 1  # USSR 1989-91 events
    # GED's adm_1 names are never read
    assert "adm_1" not in g.GED_COLUMNS


def test_event_rows_valid_atlas(world: dict[str, Any]) -> None:
    p = world["p"]
    rows = g.event_rows(p, "events")
    f, _unm = validate_atlas_frame(rows.frame(), source="ucdp_ged", dataset="events", raw_sha256s=["a" * 64])
    assert f.height == 168
    r = f.filter(pl.col("indicator").str.starts_with("deaths_best|event=")).row(0, named=True)
    fl = json.loads(r["flags"])
    assert {"tov", "low", "high", "conflict_new_id", "dyad_new_id", "where_prec"} <= set(fl)
    assert r["period_end"] > r["period_start"]


def test_country_month_sums_and_no_zero_cells(world: dict[str, Any]) -> None:
    p = world["p"]
    f, _ = validate_atlas_frame(
        g.country_month_rows(p, "country_month").frame(),
        source="ucdp_ged",
        dataset="country_month",
        raw_sha256s=["a" * 64],
    )
    ev = f.filter(pl.col("indicator") == "events|tov=all")["value"].cast(pl.Int64).sum()
    assert ev == 168
    deaths = f.filter(pl.col("indicator") == "deaths_best|tov=all")["value"].cast(pl.Int64).sum()
    assert deaths == p.df["best"].sum()
    assert f.filter((pl.col("indicator").str.starts_with("events")) & (pl.col("value") == "0")).height == 0
    # tov parts add up to the total
    parts = f.filter(pl.col("indicator").is_in(["events|tov=sb", "events|tov=ns", "events|tov=os"]))
    assert parts["value"].cast(pl.Int64).sum() == 168
    ussr = f.filter(pl.col("provider_geo") == "gw:365")
    assert ussr.height and ussr["iso3"].null_count() == ussr.height and ussr["entity_qid"].drop_nulls().len()


def test_adm1_and_priogrid_tables(world: dict[str, Any]) -> None:
    p = world["p"]
    a, _unm = validate_atlas_frame(
        g.adm1_month_rows(p, "adm1_month").frame(), source="ucdp_ged", dataset="adm1_month", raw_sha256s=["a" * 64]
    )
    assert a["iso3"].null_count() == a.height  # iso3 null by design (several subdivisions per country)
    assert all(geo.startswith("iso3166_2:") for geo in a["provider_geo"].unique().to_list())
    gz = a.filter(pl.col("provider_geo").str.starts_with("iso3166_2:PS-")).row(0, named=True)
    assert json.loads(gz["flags"])["country_iso3"] == "PSE" and json.loads(gz["flags"])["events_ged_country_differs"]
    placed = sum(1 for s in p.adm1_status if s.startswith("placed"))
    assert a.filter(pl.col("indicator") == "events|tov=all")["value"].cast(pl.Int64).sum() == placed
    pg, _ = validate_atlas_frame(
        g.priogrid_month_rows(p, "priogrid_month").frame(),
        source="ucdp_ged",
        dataset="priogrid_month",
        raw_sha256s=["a" * 64],
    )
    assert pg.filter(pl.col("indicator") == "events|tov=all")["value"].cast(pl.Int64).sum() == 168


def test_candidate_merge(world: dict[str, Any]) -> None:
    df, rel, shas, rep = g.merge_candidates(world["frames"])
    assert rep["superseded_by_later_release"] > 0 and df["id"].n_unique() == df.height
    assert df["d0"].min() > g.GED_COVERAGE_END and "dated_in_final_coverage" in rep
    # an id present in two releases is kept from the later one
    q, m7 = world["frames"][0][1], world["frames"][1][1]
    shared = set(q["id"].to_list()) & set(m7["id"].to_list())
    i = df["id"].to_list().index(next(iter(shared)))
    assert rel[i] == "GEDEvent_v26_0_7.csv" and shas[i] == world["frames"][1][2]
    p = g.prepare(
        df.drop(["_release", "_sha", "_k"]),
        resolver=world["res"],
        idx=world["idx"],
        sha=shas[0],
        file="candidate",
        candidate=True,
        release=rel,
        release_sha=shas,
    )
    ev = g.event_rows(p, "cand_events").frame()
    f, _ = validate_atlas_frame(ev, source="ucdp_ged", dataset="cand_events", raw_sha256s=sorted(set(shas)))
    fl = json.loads(f.row(0, named=True)["flags"])
    assert fl["candidate"] is True and fl["code_status"] and fl["release"].startswith("GEDEvent_v26")


def test_nsos_kg(world: dict[str, Any]) -> None:
    ns = read_csv_member(_body("ucdp-nonstate-261-csv.zip"), "NonState_v26_1.csv")
    os_ = read_csv_member(_body("ucdp-onesided-261-csv.zip"), "OneSided_v26_1.csv")
    act = read_csv_member(_body("ucdp-actor-261-csv.zip"), "Actor_v26_1.csv", encoding="cp1252")
    r = StateEntityResolver(world["cw"], {"AFG": "Q889"})
    ents, sts, rep = g.nsos_kg(ns, os_, act, shas={"nonstate": "b" * 64, "onesided": "c" * 64}, resolver=r)
    kinds = Counter(e.kind for e in ents)
    assert kinds["non_state_conflict"] == rep["nonstate_conflicts"] > 0
    assert kinds["one_sided_violence"] == rep["onesided_instances"] > 0
    coal = next(e for e in ents if e.qid == "ucdp:actor:3818")
    assert coal.labels["en"] == "NPFL, ULIMO - K" and "coalition" in coal.descriptions["en"]
    gov = [s for s in sts if s.property == "ucdpnsos:perpetrator" and s.object_qid == "Q889"]
    assert gov and gov[0].qualifiers["ucdp_actor_id"]
    assert not any(s.property.startswith(("P828", "P1542")) for s in sts)
    props = {s.property for s in sts}
    assert props <= set(g.PREDICATES) | set(g.REUSED_GEOPOL)


def test_ged_kg_statements(world: dict[str, Any]) -> None:
    sts = g.ged_kg_statements(world["p"], "d" * 64)
    subj = {s.subject.rsplit(":", 1)[0] for s in sts}
    assert subj <= {"ucdp:conflict", "ucdp:nonstate", "ucdp:onesided"}
    adm = [s for s in sts if s.property == "ucdpged:events_in_adm1"]
    assert adm and all(
        s.object_qid.startswith("iso3166_2:") and s.qualifiers["derived"] == "point_in_polygon" for s in adm
    )
    fat = [s for s in sts if s.property == "ucdpged:fatalities"]
    assert sum(int(s.value["amount"]) for s in fat if s.value) == world["p"].df["best"].sum()


def test_predicates_registry_matches_code() -> None:
    doc = yaml.safe_load(g.predicates_file().read_text())
    reg = {p["pid"]: (p["value_kind"], p["reva_predicate"], p["label_en"], p["label_fr"]) for p in doc["predicates"]}
    assert reg == g.PREDICATES


def test_source_spec() -> None:
    spec = g.atlas_source()
    assert spec.source_key == "ucdp.ged" and set(spec.datasets) == set(g.DATASETS)
    assert "naciscdn.org" in spec.allowed_hosts and spec.max_response_bytes >= 40_000_000
