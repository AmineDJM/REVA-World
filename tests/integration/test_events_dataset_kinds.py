"""EVMAP: every dataset event of the causality datasets is typed by the event taxonomy, verified on the dataset
agents' REAL fixtures (fixture-mode ingests, no network) loaded together into the throwaway database.

Sources: CAUSDATA (ICB, COW MID, UCDP termination + peace agreements, with KGGEOPOL's UCDP conflicts), A-COUPS
(Powell & Thyne, Archigos, REIGN-format list), A-HAZARDS (NOAA NCEI 2018 window), A-SANCTIONS (TIES, EUSANCT, EU
sanctions map, OFAC), A-CONTENTION (NAVCO, MEC, Mass Mobilization), A-ECRISES (Laeven-Valencia, Reinhart-Rogoff,
JST, ESRB), A-COLONIAL (COLDAT, ICOW colonial history + claims, COW territorial change + entities). Asserts: no
record of an event kind is left untyped, coded outcomes / places / regimes / persons are never typed as events,
and every causal claim's cause or effect is a typed event (the other end being a typed event or a known non-event
record such as a coded outcome). The same loads then feed one link-store materialisation (EVMAP link kinds:
sanctions episodes, OFAC programs, COLDAT / COW colonial rule, shared tsunami observations).
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import pytest
from sqlalchemy import text

from reva.adapters.connectors import (
    atlas_colonial,
    atlas_coups,
    atlas_ecrises,
    atlas_icb,
    atlas_leaders,
    atlas_massmob,
    atlas_mid,
    atlas_navco,
    atlas_noaa_hazards,
    atlas_sanctions,
    atlas_terrchange,
    atlas_ucdp,
    atlas_ucdp_term,
)
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered
from reva.events import same_as as sa
from reva.events import view as ev
from reva.events.taxonomy import load_taxonomy

pytestmark = pytest.mark.db

#: (module, source names to run (None = every spec of the module), run options)
SOURCES: list[tuple[Any, tuple[str, ...] | None, dict[str, Any]]] = [
    (atlas_ucdp, None, {}),
    (atlas_icb, None, {}),
    (atlas_mid, None, {}),
    (atlas_ucdp_term, None, {}),
    (atlas_coups, None, {}),
    (atlas_leaders, None, {}),
    (atlas_noaa_hazards, ("atlas.noaa_hazards",), {"min_year": 2018, "max_year": 2018}),
    (atlas_sanctions, None, {}),
    (atlas_navco, None, {}),
    (atlas_massmob, None, {}),
    (atlas_ecrises, None, {}),
    (atlas_colonial, None, {}),
    (atlas_terrchange, None, {}),
]

#: Namespaces of the dataset records checked here.
PREFIXES = (
    "icb:", "mid:", "ucdp:", "ptcoup:", "archigos:", "reign:", "ncei_hazards:", "ties:", "eusanct:",
    "eusanctionsmap:", "ofac:", "sanctions:", "navco", "mec:", "mmp:", "ecrises:", "icowcol:", "icowclaim:", "cowtc:",
    "cowent:", "coldat:",
)  # fmt: skip

#: KG entity kinds of event records (each must be typed with a type more specific than the root ``event``).
EVENT_KINDS = {
    "event", "armed_conflict", "earthquake", "tsunami", "volcanic_eruption", "sanction_episode",
    "international_crisis", "foreign_policy_crisis", "crisis_trigger", "crisis_response",
    "militarized_interstate_dispute", "militarized_incident", "armed_conflict_episode", "peace_agreement",
}  # fmt: skip
#: KG entity kinds of records that are not events (never typed as events): coded outcomes and terminations,
#: places, persons, groups, organisations, regimes / programs, conditions, groupings of crises.
NON_EVENT_KINDS = {
    "crisis_outcome", "dispute_outcome", "conflict_termination", "coded_outcome", "campaign_outcome", "volcano",
    "person", "armed_group", "organization", "place_name", "sanction_regime", "sanction_program", "condition",
    "protracted_conflict", "state_code", "territory",
}  # fmt: skip

#: record family -> the types its records may get (every family must be present in the fixtures).
EXPECTED = {
    "ucdp:conflict": {"interstate_armed_conflict", "intrastate_armed_conflict", "extrasystemic_armed_conflict"},
    "ucdp:episode": {"armed_conflict_episode"},
    "ucdp:pa": {"peace_agreement"},
    "icb:crisis": {"international_crisis", "foreign_policy_crisis", "crisis_trigger", "crisis_response"},
    "mid:dispute": {"militarized_interstate_dispute"},
    "mid:incident": {"militarized_incident"},
    "ptcoup": {"coup"},
    "archigos:coup": {"coup"},
    "archigos:exit": {"leader_removal"},
    "ncei_hazards:earthquake": {"earthquake"},
    "ncei_hazards:tsunami": {"tsunami"},
    "ncei_hazards:eruption": {"volcanic_eruption"},
    "ties:case": {"sanction_episode"},
    "eusanct:case": {"sanction_episode"},
    "navco12": {"resistance_campaign", "state_repression"},
    "navco13": {"resistance_campaign"},
    "navco21": {"resistance_campaign", "state_repression"},
    "mec": {"protest", "state_repression"},
    "mmp": {"protest", "state_response"},
    "ecrises:lv": {"banking_crisis", "currency_crisis", "sovereign_debt_crisis"},
    "ecrises:rr": {"banking_crisis", "currency_crisis", "sovereign_debt_crisis", "inflation_crisis"},
    "ecrises:jst": {"banking_crisis"},
    "ecrises:esrb": {"financial_crisis", "financial_stress_episode"},
    "icowcol:independence": {"independence"},
    "icowcol:secession": {"secession"},
    "icowcol:merger": {"state_merger"},
    "cowtc": {"independence", "transfer_of_sovereignty"},
    "icowclaim": {"territorial_claim"},
}


@pytest.fixture(scope="module")
async def loaded(db: Any, tmp_path_factory: pytest.TempPathFactory) -> Any:
    staging = tmp_path_factory.mktemp("claims")
    for module, names, options in SOURCES:
        specs = module.atlas_source()
        specs = specs if isinstance(specs, list) else [specs]
        for spec in specs:
            if names is not None and spec.name not in names:
                continue
            ensure_registered(db.owner, db.store, spec)
            out = await run_atlas_ingest(
                db.settings,
                db.worker,
                db.store,
                spec.name,
                mode="fixture",
                options={**options, "claims_staging": str(staging)},
            )
            assert out["status"] == "completed", (spec.name, out.get("error"))
    return db


def _family(qid: str) -> str:
    head = qid.split(":")[0]
    if head.startswith(("navco", "mec", "mmp", "ptcoup", "cowtc", "icowclaim")):
        return head
    return ":".join(qid.split(":")[:2])


def _classified(c: Any) -> dict[str, tuple[str, dict[str, Any]]]:
    """qid -> (entity kind, classification) for every record of the checked namespaces."""
    tax = load_taxonomy()
    out: dict[str, tuple[str, dict[str, Any]]] = {}
    for qid, kind in c.execute(text("SELECT qid, kind FROM kg.entity_current WHERE origin = 'test_fixture'")).all():
        if qid.startswith(PREFIXES):
            out[qid] = (kind, ev.classify(c, qid, origin="test_fixture", tax=tax))
    return out


@pytest.fixture(scope="module")
def classified(loaded: Any) -> dict[str, tuple[str, dict[str, Any]]]:
    with loaded.api.connect() as c:
        return _classified(c)


def test_every_event_record_is_typed(classified: dict[str, tuple[str, dict[str, Any]]]) -> None:
    unknown_kinds = Counter(k for k, _ in classified.values() if k not in EVENT_KINDS | NON_EVENT_KINDS)
    assert not unknown_kinds, f"entity kinds without an EVMAP decision: {dict(unknown_kinds)}"
    untyped = Counter(
        _family(q) for q, (k, cl) in classified.items() if k in EVENT_KINDS and not (cl["is_event"] and cl["primary"])
    )
    assert not untyped, f"event records without a type: {dict(untyped)}"
    generic = Counter(
        _family(q)
        for q, (k, cl) in classified.items()
        if k in EVENT_KINDS and [t["id"] for t in cl["primary"]] == ["event"]
    )
    assert not generic, f"event records typed only as the root 'event': {dict(generic)}"
    wrongly = Counter(_family(q) for q, (k, cl) in classified.items() if k in NON_EVENT_KINDS and cl["is_event"])
    assert not wrongly, f"non-event records typed as events: {dict(wrongly)}"


def test_types_per_dataset(classified: dict[str, tuple[str, dict[str, Any]]]) -> None:
    seen: dict[str, set[str]] = {}
    for q, (k, cl) in classified.items():
        if k in EVENT_KINDS:
            seen.setdefault(_family(q), set()).update(t["id"] for t in cl["primary"])
    missing = sorted(set(EXPECTED) - set(seen))
    assert not missing, f"datasets absent from the fixtures: {missing}"
    for fam, types in EXPECTED.items():
        assert seen[fam] <= types, (fam, seen[fam])
    # every type the datasets promise is reached at least once
    assert set().union(*EXPECTED.values()) == set().union(*(seen[f] for f in EXPECTED))
    # dataset typing basis is visible
    _, cl = classified[next(q for q in classified if q.startswith("ecrises:lv:systemic_banking:"))]
    m = [x for x in cl["matches"] if x.get("dataset_kind")]
    assert m and m[0]["coded_type"] == "systemic banking crisis" and m[0]["statement"]["source_key"]


def test_causal_claims_attach_to_typed_events(loaded: Any, classified: dict[str, tuple[str, dict[str, Any]]]) -> None:
    tax = load_taxonomy()
    with loaded.api.connect() as c:
        if not c.execute(text("SELECT to_regclass('kg.causal_claim_current')")).scalar():
            pytest.skip("CAUSCORE schema absent: claims were staged, not loaded")
        rows = c.execute(
            text("SELECT source_key, cause_id, effect_id FROM kg.causal_claim_current WHERE origin = 'test_fixture'")
        ).all()
        extra = {e for _, a, b in rows for e in (a, b) if e not in classified}
        kinds = {
            q: k
            for q, k in c.execute(
                text("SELECT qid, kind FROM kg.entity_current WHERE qid = ANY(:q)"), {"q": sorted(extra)}
            ).all()
        }
        for e in extra:
            classified[e] = (kinds.get(e, "?"), ev.classify(c, e, origin="test_fixture", tax=tax))
    assert len(rows) > 500
    per_source: Counter[str] = Counter()
    for src, cause, effect in rows:
        ends = [classified[cause], classified[effect]]
        assert any(cl["is_event"] for _, cl in ends), (src, cause, effect)
        for (kind, cl), qid in zip(ends, (cause, effect), strict=True):
            assert cl["is_event"] or kind in NON_EVENT_KINDS, (src, qid, kind)
            if kind in EVENT_KINDS:
                assert cl["is_event"], (src, qid, kind)
        per_source[src] += 1
    assert set(per_source) >= {
        "icb.data", "cow.data", "ucdp.downloads", "archigos.leaders", "noaa.ncei_hazards", "ties.unc",
        "eusanct.kondata", "harvard.dataverse.navco", "harvard.dataverse.mec", "harvard.dataverse.massmob",
        "esrb.fcdb",
    }, per_source  # fmt: skip


def test_cards_and_listing_of_dataset_events(loaded: Any) -> None:
    with loaded.api.connect() as c:
        case = ev.event_card(c, "ties:case:1945121601", origin="test_fixture")
        protests = ev.list_events(c, types=["protest"], origin="test_fixture", limit=200)
        repression = ev.list_events(c, types=["state_repression"], origin="test_fixture", limit=200)
        banking = ev.list_events(c, types=["banking_crisis"], origin="test_fixture", limit=200)
        removals = ev.list_events(c, types=["leader_removal"], origin="test_fixture", limit=200)
        tsunami = c.execute(
            text(
                "SELECT subject_qid FROM kg.statement_current WHERE property_pid = 'ncei:runup_country' "
                "AND origin = 'test_fixture' ORDER BY subject_qid LIMIT 1"
            )
        ).scalar()
        ts_card = ev.event_card(c, tsunami, origin="test_fixture") if tsunami else None
    assert case is not None and case["types"]["primary"][0]["id"] == "sanction_episode"
    roles = {p["role"]["id"] for p in case["participants"]}
    assert {"sender", "target"} <= roles
    assert {x["property"] for x in case["coded"]} >= {"ties:issue"}
    # the listing never mixes coded types: protests do not include state repression or responses
    p_ids = {i["id"] for i in protests["items"]}
    assert p_ids and not any(":repression:" in i or "regime_violence" in i or "state_response" in i for i in p_ids)
    assert repression["items"] and all(
        ":repression:" in i["id"] or "regime_violence" in i["id"] for i in repression["items"]
    )
    assert banking["items"] and all(i["id"].startswith("ecrises:") for i in banking["items"])
    assert removals["items"] and all(i["id"].startswith("archigos:exit:") for i in removals["items"])
    assert ts_card is not None and any(pl["place"] and pl["place"]["id"].startswith("Q") for pl in ts_card["places"])


def test_colonial_independence_same_as_is_cross_dataset_only(loaded: Any) -> None:
    """A-COLONIAL's independence key links an ICOW independence to the COW TC independence of the same state from
    the same ruler; two records of ONE dataset sharing the key (Baltic states 1918 and 1991) are never linked."""
    with loaded.worker.connect() as c:
        report = sa.derive_links(c, origin="test_fixture")
    col = [x for x in report.links if x.rule == "colonial_independence_key"]
    assert col and all({x.left.split(":")[0], x.right.split(":")[0]} == {"cowtc", "icowcol"} for x in col)
    assert ("cowtc:46", "icowcol:independence:140") in {(x.left, x.right) for x in col}  # Brazil from Portugal
    amb = [a for a in report.ambiguous if a["rule"] == "colonial_independence_key"]
    assert amb and all(a.get("reason") == "several_records_of_one_source" for a in amb if len(a["holders"]) == 2)
    assert not any({x.left, x.right} == set(a["holders"]) for x in col for a in amb)


# ------------------------------------------------------------------------------------------------ EVMAP links
EVMAP_LINKS = (
    "sanction_episode",
    "us_sanctions_program",
    "colonised_by_coldat",
    "colonial_rule_cow",
    "hazard_shared_event",
)


@pytest.fixture(scope="module")
def materialised(loaded: Any, tmp_path_factory: pytest.TempPathFactory) -> Any:
    """One link-store materialisation over the same fixture loads (test_fixture atlas snapshot + KG)."""
    from reva.atlas.snapshot import create_atlas_snapshot
    from reva.links.store import materialise

    mp = pytest.MonkeyPatch()
    mp.setenv("REVA_ATLAS_CACHE_DIR", str(tmp_path_factory.mktemp("evmap_atlas_cache")))
    snap = create_atlas_snapshot(loaded.worker, loaded.store, origin="test_fixture")
    res = materialise(loaded.worker, loaded.store, atlas_sha256=snap.sha256, top_k=5, duckdb_memory_limit="1GB")
    yield res, snap
    mp.undo()


def _atlas(db: Any) -> Any:
    from reva.atlas.query import query_for
    from reva.atlas.snapshot import resolve_snapshot

    def make(sha: str | None) -> Any:
        with db.api.connect() as c:
            snap = resolve_snapshot(c, db.store, sha256=sha)
        return query_for(snap, db.store)

    return make


def test_evmap_links_are_materialised_with_terms_and_evidence(loaded: Any, materialised: Any) -> None:
    res, _snap = materialised
    counts = res.report["counts"]
    for kind in EVMAP_LINKS:
        assert counts.get(kind, 0) > 0, (kind, counts)
    assert counts["us_sanctions_program"] == 20
    print("EVMAP counts", {k: counts[k] for k in EVMAP_LINKS})  # shown with -s (docs/dev/v3_links.md figures)
    print("EVMAP skipped", {k: res.report["kg_dataset"][k]["skipped"] for k in EVMAP_LINKS[2:]})
    print("EVMAP atlas", {k: res.report["atlas"][k]["skipped"] for k in EVMAP_LINKS[:2]})
    with loaded.api.connect() as c:
        rows = c.execute(
            text(
                """SELECT link_type, count(*) AS n,
                          count(*) FILTER (WHERE jsonb_array_length(evidence) = 0 OR weight_num = 0) AS bad,
                          count(*) FILTER (WHERE NOT basis ? 'licence') AS no_terms,
                          count(DISTINCT facet) AS facets
                     FROM kg.link_row WHERE materialisation_id = :m AND link_type = ANY(:k) GROUP BY link_type"""
            ),
            {"m": res.id, "k": list(EVMAP_LINKS)},
        ).mappings()
        by = {r["link_type"]: dict(r) for r in rows}
        hz = c.execute(
            text(
                "SELECT via, temporal, basis FROM kg.link_row WHERE materialisation_id = :m "
                "AND link_type = 'hazard_shared_event' ORDER BY subject, object LIMIT 1"
            ),
            {"m": res.id},
        ).one()
    for kind, r in by.items():
        assert r["bad"] == 0 and r["no_terms"] == 0, (kind, r)
    assert by["sanction_episode"]["facets"] == 2  # TIES and EUSANCT never merged
    assert hz.via[0].startswith("ncei_hazards:tsunami:") and hz.temporal["kind"] == "point"
    assert res.report["kg_dataset"]["hazard_shared_event"]["count"] == counts["hazard_shared_event"]


def test_evmap_pair_and_country_views(loaded: Any, materialised: Any) -> None:
    from reva.links import service as svc

    with loaded.api.connect() as c:
        pair = svc.pair_view(c, "USA", "CUB", atlas=_atlas(loaded))
        view = svc.country_view(c, "CUB", kinds=["sanction_episode", "us_sanctions_program"], atlas=_atlas(loaded))
    se = pair["kinds"]["sanction_episode"]
    assert se["count"] > 10 and se["first_year"] <= 1960
    assert {lk["facet"] for lk in se["links"]} >= {"source=ties"}
    assert all(lk["basis"]["licence"] for lk in se["links"])
    assert pair["kinds"]["us_sanctions_program"]["links"][0]["weight"] == "1"
    groups = view["by_kind"]["sanction_episode"]["groups"]
    assert any(g["facet"] == "source=ties" and g["direction"] == "incoming" for g in groups)
