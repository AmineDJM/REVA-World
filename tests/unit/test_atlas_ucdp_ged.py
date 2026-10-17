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
    assert st["placed:territory_exception"] >= len(gaza)  # reviewed exception ISR->PSE (FIX6, AUDIT4 M1/M2)
    assert st["placed:ged_country_differs"] == 0  # status retired: other cross-border hits are not placed
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


# --------------------------------------------------------------------------- FIX6 (AUDIT4 M1, M2, L1, L2)
def _square_index(squares: dict[str, tuple[float, float, float, float]]) -> g.Adm1Index:
    from shapely import STRtree, box  # type: ignore[import-untyped]

    codes = list(squares)
    geoms = [box(*squares[c]) for c in codes]
    return g.Adm1Index(codes, set(codes), STRtree(geoms), geoms, "e" * 64, {})


def _events(rows: list[tuple[float, float, str, str]]) -> pl.DataFrame:
    from datetime import date

    return pl.DataFrame(
        {
            "latitude": [r[0] for r in rows],
            "longitude": [r[1] for r in rows],
            "where_prec": ["1"] * len(rows),
            "d0": [date.fromisoformat(r[3]) for r in rows],
        }
    )


def test_border_rule_and_territory_exceptions() -> None:
    # TR-63 square next to a SY square; PS-GZZ next to an EG square; SS square (South Sudan)
    idx = _square_index(
        {
            "TR-63": (38.0, 36.8, 39.0, 37.5),
            "SY-HL": (38.0, 36.0, 39.0, 36.8),
            "PS-GZZ": (34.2, 31.2, 34.6, 31.6),
            "EG-SIN": (33.0, 30.0, 34.2, 31.2),
            "SS-EC": (30.0, 4.0, 32.0, 6.0),
        }
    )
    df = _events(
        [
            (37.0, 38.4, "SYR", "2015-01-20"),  # Kobane-like point in the Turkish polygon: border generalisation
            (36.5, 38.4, "SYR", "2015-01-20"),  # same-country hit
            (31.4, 34.4, "ISR", "2024-01-01"),  # Gaza event coded Israel: reviewed exception ISR->PSE
            (31.4, 34.4, "EGY", "2015-01-01"),  # "Egyptian side of Rafah" in the Gaza polygon: not placed
            (5.0, 31.0, "SDN", "2005-01-01"),  # Sudan before 2011-07-09 in present-day South Sudan: exception
            (5.0, 31.0, "SDN", "2012-01-01"),  # after independence: not an exception
        ]
    )
    pl_ = g.place_adm1(df, idx, ["SYR", "SYR", "ISR", "EGY", "SDN", "SDN"])
    assert pl_.status == [
        "not_placed:border_generalisation",
        "placed",
        "placed:territory_exception",
        "not_placed:border_generalisation",
        "placed:territory_exception",
        "not_placed:border_generalisation",
    ]
    assert pl_.codes == [None, "SY-HL", "PS-GZZ", None, "SS-EC", None]
    assert pl_.polygon == ["TR-63", None, None, "PS-GZZ", None, "SS-EC"]
    assert pl_.exception[2] == "ISR->PSE" and pl_.exception[4] == "SDN->SSD"


def test_territory_exceptions_registry() -> None:
    exc = {e.label: e for e in g.load_territory_exceptions()}
    assert set(exc) == {"ISR->PSE", "SDN->SSD", "IDN->TLS", "ETH->ERI", "ZAF->NAM"}
    assert exc["ISR->PSE"].before is None and str(exc["SDN->SSD"].before) == "2011-07-09"
    doc = yaml.safe_load(g.territory_exceptions_file().read_text())
    assert doc["review"] == "pending_owner"


def test_country_month_other_territory_flag(world: dict[str, Any]) -> None:
    p = world["p"]
    f, _ = validate_atlas_frame(
        g.country_month_rows(p, "country_month").frame(),
        source="ucdp_ged",
        dataset="country_month",
        raw_sha256s=["a" * 64],
    )
    isr = f.filter((pl.col("provider_geo") == "gw:666") & (pl.col("indicator") == "events|tov=all"))
    fl = [json.loads(x) for x in isr["flags"].to_list()]
    flagged = [x for x in fl if x.get("events_placed_in_other_territory")]
    assert flagged and all(x["other_territory_iso3"] == ["PSE"] for x in flagged)
    n_exc = sum(1 for e in p.adm1_exception if e == "ISR->PSE")
    assert sum(x["events_placed_in_other_territory"] for x in flagged) == n_exc
    assert all("lower bound" in x["other_territory_note"] for x in flagged)
    # values themselves are unchanged (GED's coding kept): every event still counted in its GED country
    assert f.filter(pl.col("indicator") == "events|tov=all")["value"].cast(pl.Int64).sum() == 168


def test_kg_left_censoring_and_ne_reference(world: dict[str, Any]) -> None:
    from reva.adapters.connectors.atlas_geopol_common import to_kg_model

    sts = g.ged_kg_statements(world["p"], "d" * 64)
    first = [s for s in sts if s.property == "ucdpged:first_event_date"]
    cens = [s for s in first if s.qualifiers.get("left_censored")]
    assert cens and all(s.value and s.value["time"] < "1989-04-01" for s in cens)
    assert all(s.value and s.value["time"] >= "1989-04-01" for s in first if s not in cens)
    adm = [s for s in sts if s.property == "ucdpged:events_in_adm1"]
    _, ms = to_kg_model((), adm[:1], source_key="ucdp.ged", source_revision=g.KG_REVISION)
    refs = ms[0].references
    assert refs[1] == {
        "raw_sha256": "d" * 64,
        "role": "adm1_polygons",
        "licence": "Public domain (Natural Earth)",
        "attribution": "Made with Natural Earth",
    }


def test_family_rights_serve_input_licences() -> None:
    from reva.kg.family_query import _input_licences, _rights

    ged = "8" * 64
    row = {
        "source_key": "ucdp.ged",
        "raw_sha256": ged,
        "refs": [{"raw_sha256": ged, "selector": {}}],
        "qualifiers": {"natural_earth_sha256": "e" * 64},
    }
    by_sha = {ged: {"licence": "CC BY 4.0", "recorded_for_source_key": "ucdp.ged"}}
    r = _rights(row, by_sha)
    assert r is not None and r["licence"] == "CC BY 4.0"  # the statement's own terms stay
    assert r["inputs"] == [
        {
            "raw_sha256": "e" * 64,
            "role": "adm1_polygons",
            "licence": "Public domain (Natural Earth)",
            "attribution": "Made with Natural Earth",
            "basis": "qualifier natural_earth_sha256",
        }
    ]
    row2 = {**row, "refs": [{"raw_sha256": ged}, {"raw_sha256": "e" * 64, "licence": "Public domain (Natural Earth)"}]}
    assert len(_input_licences(row2)) == 1  # the explicit reference wins, no duplicate from the qualifier


def test_gw_260_unified_germany() -> None:
    from datetime import date

    cw = crosswalk_from_countrycode_tarball(_body("countrycode_1.9.0.tar.gz"), crosswalk_keys())
    assert cw.resolve("ucdp", 260, date(2020, 7, 1)).iso3 == "DEU"
    assert cw.resolve("gw", 260, date(1990, 10, 3)).iso3 == "DEU"
    assert cw.resolve("ucdp", 260, date(1989, 7, 1)).iso3 is None  # FRG before unification: no ISO entity
    assert cw.resolve("cow", 260, date(2020, 7, 1)).iso3 is None  # COW codes unified Germany 255
    # 345: Serbia only from 2006-06-05 (UCDP keeps 345); SFRY / FRY / Serbia and Montenegro have no ISO3
    assert cw.resolve("ucdp", 345, date(2006, 7, 1)).iso3 == "SRB"
    assert cw.resolve("ucdp", 345, date(2005, 7, 1)).iso3 is None
