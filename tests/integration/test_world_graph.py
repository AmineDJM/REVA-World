"""World graph library against a real database with captured Comtrade fixtures.

Data: ``un.comtrade.public_exact`` replayed from tests/fixtures (origin
test_fixture) through the real ingest path. Extra rows written by this module
(test concepts, aliases, occurrences, a release-pinned edge, a causal
assertion) use unique ``test.wg.<tag>`` names so they cannot collide with
other tests sharing the session database.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import ProgrammingError

from reva.adapters.connectors.registry import builtin_connectors
from reva.adapters.postgres import repositories as repo
from reva.belief.publication import publish_release
from reva.cli.seed import entity_id
from reva.evidence.runner import FixtureHttp, run_connector
from reva.kernel.context import Origin
from reva.kernel.errors import IncompatibleSemantics, ValidationProblem
from reva.kernel.support import PopulationRef, SpatialRef, Support
from reva.kernel.time import TimeExtent, UncertainBound
from reva.world import events, queries
from reva.world.entities import get_entity, list_entities
from reva.world.identities import key_scopes, resolve_alias, resolve_aliases
from reva.world.queries import GraphContext

pytestmark = pytest.mark.db

LAYER = "trade.crude_petroleum"
FRA, DZA, CHN, USA, HS = (entity_id(k) for k in ("FRA", "DZA", "CHN", "USA", "HS2709"))
Y2023 = (datetime(2023, 1, 1, tzinfo=UTC), datetime(2024, 1, 1, tzinfo=UTC))


@dataclass
class World:
    before_ingest: datetime
    before_seed: datetime
    release_id: UUID
    tag: str
    activity_id: UUID
    occ: dict[str, UUID]
    occ_kind: str
    event_type: str
    causal_predicate: str
    role_cause: str
    role_effect: str
    before_test_rows: datetime
    alias_provider: str


def _concept(conn: Connection, cid: str, kind: str, act: UUID, profile: dict[str, Any] | None = None) -> None:
    conn.execute(
        text(
            """INSERT INTO ontology.concept_version(concept_id, version, kind, labels, definition, semantic_profile,
                 registry_sha256, provenance_activity_id)
               VALUES (:c, '1.0.0', :k, '{}'::jsonb, 'world graph test concept', CAST(:p AS jsonb), 'test', :a)"""
        ),
        {"c": cid, "k": kind, "p": json.dumps(profile or {}), "a": act},
    )


def _publish_with_pinned_relations(db, monkeypatch, write: Any) -> UUID:
    """Publish a *new* release whose extra release-pinned relations are written inside the publication
    transaction, before ``ops.publication`` seals it (R5-07: a published release accepts no new rows)."""
    from reva.belief import publication

    orig = publication.pin_release_relations

    def pin(conn: Connection, release_id: Any, activity_id: Any) -> int:
        n = orig(conn, release_id, activity_id)
        write(conn, release_id)
        return n

    monkeypatch.setattr(publication, "pin_release_relations", pin)
    rel = publish_release(db.worker, db.store, origin=Origin.test_fixture, environment="test",
                          concepts=["trade.hs2709.import_value.world_total"])
    monkeypatch.setattr(publication, "pin_release_relations", orig)
    return rel.release_id


@pytest.fixture(scope="module")
def world(db) -> World:
    c = builtin_connectors()["un.comtrade.public_exact"]
    rep = asyncio.run(run_connector(c, FixtureHttp(c.describe().name), db.store, db.worker, persist_cursor=False))
    assert not rep.errors, rep.errors
    with db.owner.connect() as conn:
        first_obs = conn.execute(
            text(
                """SELECT min(o.recorded_at) FROM evidence.observation o JOIN evidence.source s ON s.id = o.source_id
               WHERE s.source_key = 'un.comtrade.public_preview'"""
            )
        ).scalar_one()
        first_alias = conn.execute(text("SELECT min(recorded_at) FROM world.alias")).scalar_one()
        cutoff = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
    rel = publish_release(
        db.worker,
        db.store,
        cutoff=cutoff,
        origin=Origin.test_fixture,
        environment="test",
        concepts=["trade.hs2709.import_value.world_total"],
    )
    tag = uuid4().hex[:8]
    with db.owner.connect() as conn:
        before_rows = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
    occ_kind, event_type = f"test.wg.{tag}.occurrence", f"test.wg.{tag}.event"
    role_cause, role_effect = f"test.wg.{tag}.role.cause", f"test.wg.{tag}.role.effect"
    causal = f"predicate.causal.test_wg_{tag}"
    provider = f"test_wg_{tag}"
    with db.worker.begin() as conn:
        act = repo.simple_activity("test.world_graph", "review", "test:world_graph")
        repo.record_activity(conn, act)
        _concept(conn, occ_kind, "entity_type", act.id)
        _concept(conn, event_type, "category", act.id)
        _concept(conn, role_cause, "role", act.id)
        _concept(conn, role_effect, "role", act.id)
        sig = {
            "roles": {
                role_cause: {"entity_kinds": [occ_kind], "min": 1, "max": 1},
                role_effect: {"entity_kinds": [occ_kind], "min": 1, "max": 1},
            }
        }
        _concept(conn, causal, "predicate", act.id, {"assertion_class": "causal_hypothesis", "signature": sig})
        extents = {
            "A": TimeExtent.on_date(date(2020, 3, 1)),
            "B": TimeExtent(
                kind="instant",
                precision="day",
                start=date(2020, 1, 1),
                start_uncertainty=UncertainBound(earliest=date(2020, 1, 1), latest=date(2020, 6, 30)),
            ),
            "C": TimeExtent.calendar_year(2021),
        }
        occ: dict[str, UUID] = {}
        for name, te in extents.items():
            eid = uuid4()
            occ[name] = eid
            conn.execute(
                text("""INSERT INTO world.entity(id, kind_concept_id, namespace, referent_mode, label,
                          created_activity_id) VALUES (:i, :k, 'test', 'real', :l, :a)"""),
                {"i": eid, "k": repo.concept_version_id(conn, occ_kind), "l": f"Test occurrence {name}", "a": act.id},
            )
            sup = repo.ensure_support(
                conn,
                Support(
                    temporal=te,
                    spatial=SpatialRef(scheme="test", key=f"test:{tag}:{name}", authority="test"),
                    population=PopulationRef(universe="not_applicable"),
                    aggregation="not_applicable",
                ),
                act.id,
            )
            conn.execute(
                text("""INSERT INTO world.occurrence(entity_id, event_type_id, support_id, component,
                          provenance_activity_id) VALUES (:e, :t, :s, CAST(:c AS jsonb), :a)"""),
                {
                    "e": eid,
                    "t": repo.concept_version_id(conn, event_type),
                    "s": sup,
                    "c": json.dumps({"description": f"synthetic test occurrence {name}"}),
                    "a": act.id,
                },
            )
        for key, ent, rng, version, conf in (
            ("X1", FRA, "[2000-01-01,2010-01-01)", "1.0.0", "reviewed_exact"),
            ("X1", DZA, "[2010-01-01,)", "2.0.0", "reviewed_exact"),
            ("X1", CHN, None, "3.0.0", "candidate"),
        ):
            conn.execute(
                text("""INSERT INTO world.alias(entity_id, provider, external_key, valid_range, mapping_version,
                          confidence_kind, provenance_activity_id)
                        VALUES (:e, :p, :k, CAST(:r AS daterange), :v, :c, :a)"""),
                {"e": ent, "p": provider, "k": key, "r": rng, "v": version, "c": conf, "a": act.id},
            )
    return World(
        first_obs - timedelta(microseconds=1),
        first_alias - timedelta(microseconds=1),
        rel.release_id,
        tag,
        act.id,
        occ,
        occ_kind,
        event_type,
        causal,
        role_cause,
        role_effect,
        before_rows,
        provider,
    )


def _ev(as_of: datetime | None = None) -> GraphContext:
    return GraphContext.evidence(as_of=as_of, origin="test_fixture")


# ---------------------------------------------------------------------------
# Hyperedges, roles and neighbourhood
# ---------------------------------------------------------------------------


def test_participant_roles_and_directions_preserved(db, world) -> None:
    with db.api.connect() as conn:
        nb = queries.neighbourhood(conn, FRA, context=_ev(), layers=[LAYER], roles=["role.reporter"])
    assert nb.edges and not nb.truncated
    flows = set()
    for e in nb.edges:
        assert e.predicate == "predicate.trade_flow" and e.layer == LAYER
        assert [p.ordinal for p in e.participants] == [0, 1, 2]
        assert [p.role for p in e.participants] == ["role.reporter", "role.partner", "role.product"]
        reporter, partner, product = e.participants
        assert reporter.entity_id == FRA and product.entity_id == HS and product.direction == "undirected"
        flow = e.component["flow"]
        flows.add(flow)
        expected = ("in", "out") if flow == "M" else ("out", "in")
        assert (reporter.direction, partner.direction) == expected
        assert e.provenance.kind == "evidence" and e.provenance.origin == "test_fixture"
    assert flows == {"M", "X"}
    # get_hyperedge returns the same ordered participants
    with db.api.connect() as conn:
        one = queries.get_hyperedge(conn, nb.edges[0].id, context=_ev())
    assert one is not None and one.participants == nb.edges[0].participants


def test_four_quantities_stay_distinct(db, world) -> None:
    with db.api.connect() as conn:
        nb = queries.neighbourhood(conn, FRA, context=_ev(), layers=[LAYER], max_edges=5)
    d = nb.edges[0].as_dict()
    assert {"existence_uncertainty", "flow_magnitude", "tie_strength", "scientific_confidence"} <= d.keys()
    assert d["existence_uncertainty"]["status"] == "unquantified"
    assert d["flow_magnitude"]["status"] in ("reported", "missing")
    assert d["flow_magnitude"]["concept_id"].endswith(".bilateral")
    assert d["tie_strength"]["status"] == "not_assessed"
    assert d["scientific_confidence"]["status"] == "not_assessed"
    assert d["flow_magnitude"] is not d["tie_strength"]


def test_neighbourhood_of_fra_lists_partners(db, world) -> None:
    with db.api.connect() as conn:
        nb = queries.neighbourhood(conn, FRA, context=_ev(), layers=[LAYER], valid_during=Y2023)
    by_id = {n.entity_id: n for n in nb.neighbours}
    assert DZA in by_id, "Algeria is a reported crude partner of France in 2023"
    alg = by_id[DZA]
    assert "role.partner" in alg.roles and "role.reporter" in alg.focal_roles and alg.layers == (LAYER,)
    assert HS in by_id and by_id[HS].roles == ("role.product",)
    partners = {n.entity_id for n in nb.neighbours if "role.partner" in n.roles}
    assert len(partners) >= 10
    assert all(FRA in e.entity_ids() for e in nb.edges)


def test_neighbourhood_interval_layer_and_truncation(db, world) -> None:
    with db.api.connect() as conn:
        none_2019 = queries.neighbourhood(
            conn,
            FRA,
            context=_ev(),
            layers=[LAYER],
            valid_during=(datetime(2019, 1, 1, tzinfo=UTC), datetime(2020, 1, 1, tzinfo=UTC)),
        )
        other_layer = queries.neighbourhood(conn, FRA, context=_ev(), layers=["trade.no_such_layer"])
        small = queries.neighbourhood(conn, FRA, context=_ev(), layers=[LAYER], max_edges=3)
        live = queries.neighbourhood(conn, FRA, context=GraphContext.evidence(origin="live"), layers=[LAYER])
    assert none_2019.edges == () and not none_2019.truncated and none_2019.notes
    assert other_layer.edges == ()
    assert len(small.edges) == 3 and small.truncated is True
    assert live.edges == ()  # fixture-origin evidence is not live evidence


def test_as_of_before_ingestion_returns_nothing(db, world) -> None:
    t = world.before_ingest
    with db.api.connect() as conn:
        nb = queries.neighbourhood(conn, FRA, context=_ev(t), layers=[LAYER])
        paths = queries.path_search(conn, FRA, USA, context=_ev(t), excluded_roles=["role.product"])
        sub = queries.extract_subgraph(conn, context=_ev(t), layers=[LAYER])
        later = queries.neighbourhood(conn, FRA, context=_ev(datetime.now(UTC)), layers=[LAYER], max_edges=1)
        edge = queries.get_hyperedge(conn, later.edges[0].id, context=_ev(t))
        before_seed = resolve_alias(conn, "comtrade_reporter", "251", as_of=world.before_seed)
        ents = list_entities(conn, as_of=world.before_seed)
        first_entity = conn.execute(text("SELECT min(recorded_at) FROM world.entity")).scalar_one()
        no_ents = list_entities(conn, as_of=first_entity - timedelta(microseconds=1))
        obs_ids = [e.provenance.observation_id for e in later.edges if e.provenance.observation_id]
        agg = queries.aggregate_flows(conn, observation_ids=obs_ids, as_of=t)
    assert nb.edges == () and nb.neighbours == () and not nb.truncated
    assert paths.paths == () and not paths.truncated
    assert sub.edges == ()
    assert later.edges and edge is None
    assert before_seed.status == "unresolved"
    assert all(e.aliases == () and e.recorded_at <= world.before_seed for e in ents.items)
    assert no_ents.items == ()
    assert agg.groups == () and agg.excluded and agg.excluded[0]["reason"] == "recorded_after_as_of"


# ---------------------------------------------------------------------------
# Path search
# ---------------------------------------------------------------------------


def test_path_search_through_shared_partners(db, world) -> None:
    with db.api.connect() as conn:
        res = queries.path_search(conn, FRA, USA, context=_ev(), layers=[LAYER], excluded_roles=["role.product"])
    assert res.bounds == {"max_depth": 2, "max_edges": 500, "timeout_ms": 5000}
    assert res.paths and not res.truncated and res.truncation_reasons == ()
    edges = {e.id: e for e in res.edges}
    for p in res.paths:
        assert p.entity_ids[0] == FRA and p.entity_ids[-1] == USA and 1 <= p.depth <= 2
        for (a, b), eid in zip(zip(p.entity_ids, p.entity_ids[1:], strict=False), p.edge_ids, strict=True):
            members = set(edges[eid].entity_ids())
            assert {a, b} <= members and HS in members  # product participates but is not a pivot
        assert HS not in p.entity_ids
    assert res.reached == {USA: min(p.depth for p in res.paths)}


def test_path_search_bounds_and_truncated_flag(db, world) -> None:
    with db.api.connect() as conn:
        capped = queries.path_search(conn, FRA, context=_ev(), layers=[LAYER], max_edges=5)
        deep = queries.path_search(conn, FRA, DZA, context=_ev(), layers=[LAYER], max_depth=4, max_edges=20)
        via_product = queries.path_search(conn, FRA, USA, context=_ev(), layers=[LAYER], max_depth=2)
        with pytest.raises(ValidationProblem):
            queries.path_search(conn, FRA, context=_ev(), max_depth=5)
        with pytest.raises(ValidationProblem):
            queries.path_search(conn, FRA, context=_ev(), max_edges=501)
    assert capped.truncated and set(capped.truncation_reasons) <= {"row_limit", "edge_limit"}
    assert capped.truncation_reasons and len(capped.edges) <= 5
    assert deep.paths and len({e for p in deep.paths for e in p.edge_ids}) <= 20
    assert any(HS in p.entity_ids for p in via_product.paths)


def test_path_search_time_limit_is_reported(db, world, monkeypatch) -> None:
    monkeypatch.setattr(
        queries,
        "_path_sql",
        lambda ctx: (
            "SELECT CAST(NULL AS uuid) AS entity_id, 1 AS depth, ARRAY[]::uuid[] AS path_entities, "
            "ARRAY[]::uuid[] AS path_edges FROM pg_sleep(2)"
        ),
    )
    with db.api.connect() as conn:
        before = conn.execute(text("SHOW statement_timeout")).scalar_one()
        res = queries.path_search(conn, FRA, USA, context=_ev(), timeout_ms=50)
        assert conn.execute(text("SELECT 1")).scalar_one() == 1  # transaction still usable
        assert conn.execute(text("SHOW statement_timeout")).scalar_one() == before
    assert res.truncated and res.truncation_reasons == ("time_limit",) and res.paths == ()


# ---------------------------------------------------------------------------
# Release-pinned edges
# ---------------------------------------------------------------------------


def test_release_pinned_context(db, world, monkeypatch) -> None:
    layer = f"test.wg.{world.tag}.release_pinned"
    made: list[UUID] = []

    def write(conn: Connection, release_id: Any) -> None:
        sup = conn.execute(
            text("SELECT support_id FROM world.relation WHERE layer = :l LIMIT 1"), {"l": LAYER}
        ).scalar_one()
        rid = conn.execute(
            text("""INSERT INTO world.relation(predicate_id, layer, support_id, release_id, existence_uncertainty,
                      strength, component, provenance_activity_id)
                    VALUES (:p, :l, :s, :r, '{"status":"unquantified"}'::jsonb, NULL,
                            '{"flow":"M","flow_magnitude":{"value":{"kind":"missing","reason":"not_collected"}}}'::jsonb,
                            :a) RETURNING id"""),
            {
                "p": repo.concept_version_id(conn, "predicate.trade_flow"),
                "l": layer,
                "s": sup,
                "r": release_id,
                "a": world.activity_id,
            },
        ).scalar_one()
        for i, (role, ent, d) in enumerate(
            (("role.reporter", FRA, "in"), ("role.partner", DZA, "out"), ("role.product", HS, "undirected"))
        ):
            conn.execute(
                text("""INSERT INTO world.relation_participant(relation_id, ordinal, role_concept_id, entity_id,
                          direction) VALUES (:r, :o, :role, :e, :d)"""),
                {"r": rid, "o": i, "role": repo.concept_version_id(conn, role), "e": ent, "d": d},
            )
        made.append(rid)

    release_id = _publish_with_pinned_relations(db, monkeypatch, write)
    (rid,) = made
    with db.api.connect() as conn:
        pinned = queries.neighbourhood(conn, FRA, context=GraphContext.release(release_id), layers=[layer])
        as_evidence = queries.neighbourhood(conn, FRA, context=_ev(), layers=[layer])
        missing = queries.neighbourhood(conn, FRA, context=GraphContext.release(uuid4()), layers=[layer])
        ctx = queries.release_context_as_of(conn, None, "test_fixture")
    assert [e.id for e in pinned.edges] == [rid]
    e = pinned.edges[0]
    assert e.provenance.kind == "release" and e.provenance.release_id == release_id
    assert e.flow_magnitude["status"] == "selected" and e.tie_strength["status"] == "not_assessed"
    assert as_evidence.edges == ()
    assert missing.edges == () and "does not exist" in missing.notes[0]
    assert ctx is not None and ctx.mode == "release"


# ---------------------------------------------------------------------------
# Flow aggregation and claim comparison
# ---------------------------------------------------------------------------


def _obs_id(conn: Connection, logical_key: str) -> UUID:
    return conn.execute(
        text(
            """SELECT o.id FROM evidence.observation o JOIN evidence.acquisition a ON a.id = o.acquisition_id
               WHERE o.logical_key = :k AND a.origin = 'test_fixture' ORDER BY o.revision_no DESC LIMIT 1"""
        ),
        {"k": logical_key},
    ).scalar_one()


def test_flow_aggregation_compatible_bilateral(db, world) -> None:
    with db.api.connect() as conn:
        nb = queries.neighbourhood(
            conn, FRA, context=_ev(), layers=[LAYER], roles=["role.reporter"], valid_during=Y2023
        )
        chosen = [
            e
            for e in nb.edges
            if e.component["flow"] == "M"
            and e.component["classification"] == "H6"
            and e.component["valuation"] == "CIF"
            and e.flow_magnitude["status"] == "reported"
        ]
        assert len(chosen) >= 5
        agg = queries.aggregate_flows(conn, observation_ids=[e.provenance.observation_id for e in chosen])
    expected = sum(Decimal(e.flow_magnitude["value"]["value"]) for e in chosen)
    assert len(agg.groups) == 1 and agg.groups[0].period_label == "2023"
    assert Decimal(agg.groups[0].total["value"]) == expected
    assert agg.semantics["breakdown"] == "bilateral" and agg.semantics["valuation"] == "CIF"
    assert agg.modality == "derived_aggregate_of_reported_observations"


def test_aggregation_refuses_world_total_plus_bilateral(db, world) -> None:
    with db.api.connect() as conn:
        total = _obs_id(conn, "comtrade:HS2709:M:251:0:2023:primaryValue")
        bilateral = _obs_id(conn, "comtrade:HS2709:M:251:12:2023:primaryValue")
        with pytest.raises(IncompatibleSemantics) as exc:
            queries.aggregate_flows(conn, observation_ids=[total, bilateral])
        assert exc.value.details["reason"] == "mixed_breakdown"
        years = [_obs_id(conn, f"comtrade:HS2709:M:251:0:{y}:primaryValue") for y in (2020, 2024)]
        with pytest.raises(IncompatibleSemantics) as exc2:
            queries.aggregate_flows(conn, observation_ids=years)
        assert exc2.value.details["reason"] == "mixed_classification"
        same = queries.aggregate_flows(conn, observation_ids=[total])
    assert same.groups[0].total["kind"] == "decimal"


def test_claim_vs_accepted_estimate(db, world) -> None:
    concept = "trade.hs2709.import_value.world_total"
    with db.api.connect() as conn:
        oid = _obs_id(conn, "comtrade:HS2709:M:251:0:2023:primaryValue")
        support = conn.execute(
            text("SELECT support_id FROM evidence.observation WHERE id = :i"), {"i": oid}
        ).scalar_one()
        cmp = queries.compare_claims(conn, FRA, concept, support, release_id=world.release_id)
        early = queries.compare_claims(conn, FRA, concept, support, as_of=world.before_ingest, origin="test_fixture")
    assert len(cmp.claims) == 1 and cmp.claims[0]["is_latest"] and cmp.claims[0]["modality"] == "reported_observation"
    assert len(cmp.estimates) == 1 and cmp.estimates[0]["modality"] == "selected_report"
    c = cmp.comparisons[0]
    assert c["comparable"] and c["identical"] and c["claim_is_input"] and c["difference"] == "0.000"
    assert any("not a calibrated posterior" in n for n in cmp.notes)
    assert early.claims == () and early.estimates == ()


# ---------------------------------------------------------------------------
# Entities and identities
# ---------------------------------------------------------------------------


def test_entities_keyset_pagination(db, world) -> None:
    seen: list[tuple[str, UUID]] = []
    with db.api.connect() as conn:
        cursor = None
        for _ in range(100):
            page = list_entities(conn, kind="entity.country", limit=50, cursor=cursor)
            seen.extend((e.label, e.id) for e in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        with pytest.raises(ValidationProblem):
            list_entities(conn, limit=1001)
        fra = get_entity(conn, FRA)
        hs = list_entities(conn, alias_provider="hs_heading")
    assert cursor is None and len(seen) == len(set(seen)) > 50
    assert {e for _, e in seen} >= {FRA, DZA, CHN, USA}
    assert fra is not None and fra.kind == "entity.country" and fra.referent_mode == "real"
    keys = {(a.provider, a.external_key, a.confidence_kind) for a in fra.aliases}
    assert ("comtrade_reporter", "251", "reviewed_exact") in keys and ("un_m49", "250", "reviewed_exact") in keys
    assert [e.id for e in hs.items] == [HS] and hs.items[0].referent_mode == "abstract"


def test_alias_resolution_is_provider_scoped(db, world) -> None:
    with db.api.connect() as conn:
        assert resolve_alias(conn, "comtrade_reporter", "251").entity_id == FRA
        assert resolve_alias(conn, "un_m49", "251").status == "unresolved"
        assert resolve_alias(conn, "un_m49", "250").entity_id == FRA
        scopes = key_scopes(conn, "12")
        many = resolve_aliases(conn, "iso3166_alpha3", ["FRA", "USA", "XXX"])
        with pytest.raises(ValidationProblem):
            resolve_alias(conn, "", "251")
    assert {"comtrade_reporter", "un_m49", "comtrade_partner"} <= scopes.keys()
    assert all(r.entity_id == DZA for r in scopes.values())
    assert many["FRA"].entity_id == FRA and many["USA"].entity_id == USA and many["XXX"].status == "unresolved"


def test_alias_ambiguity_is_explicit(db, world) -> None:
    p = world.alias_provider
    with db.api.connect() as conn:
        amb = resolve_alias(conn, p, "X1")
        early = resolve_alias(conn, p, "X1", valid_on=date(2005, 6, 1))
        late = resolve_alias(conn, p, "X1", valid_on=date(2015, 6, 1))
        with_cand = resolve_alias(conn, p, "X1", valid_on=date(2015, 6, 1), include_candidates=True)
        past = resolve_alias(conn, p, "X1", as_of=world.before_test_rows)
    assert amb.status == "ambiguous" and amb.entity_id is None
    assert set(amb.candidate_entity_ids) == {FRA, DZA}
    assert any("valid_on" in n for n in amb.notes) and any("candidate" in n for n in amb.notes)
    assert early.status == "resolved" and early.entity_id == FRA
    assert late.status == "resolved" and late.entity_id == DZA
    assert with_cand.status == "ambiguous" and set(with_cand.candidate_entity_ids) == {DZA, CHN}
    assert past.status == "unresolved"


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def test_occurrences_with_uncertain_time(db, world) -> None:
    occ = world.occ
    with db.api.connect() as conn:
        none = events.list_occurrences(conn, event_type=f"test.wg.{world.tag}.no_such_event")
        page = events.list_occurrences(conn, event_type=world.event_type)
        feb = events.list_occurrences(conn, event_type=world.event_type, during=(date(2020, 2, 1), date(2020, 3, 1)))
        early = events.list_occurrences(conn, event_type=world.event_type, as_of=world.before_test_rows)
    assert none.items == () and none.notes
    by_id = {o.entity_id: o for o in page.items}
    assert set(by_id) == set(occ.values())
    assert by_id[occ["A"]].time.status == "exact" and by_id[occ["B"]].time.status == "uncertain"
    assert {o.entity_id for o in feb.items} == {occ["B"]}  # B may fall in February; A (1 March) does not
    assert early.items == ()


def test_history_separates_sequence_from_causal_hypotheses(db, world, monkeypatch) -> None:
    occ = world.occ
    ids = list(occ.values())
    with db.api.connect() as conn:
        view = events.history_view(conn, occurrence_ids=ids)
    rel = {(r.a, r.b): r.relation for r in view.observed_sequence}
    assert rel[(occ["A"], occ["C"])] == "before" and rel[(occ["B"], occ["C"])] == "before"
    assert all(not r.causal_claim for r in view.observed_sequence)
    assert any(
        d["relation"] == "indeterminate" and {d["a"], d["b"]} == {str(occ["A"]), str(occ["B"])}
        for d in view.disputed_sequence
    )
    assert view.causal_hypotheses == ()  # A preceded C, but nothing asserts that A caused C
    assert any("no causal hypothesis" in n for n in view.notes)

    made: list[UUID] = []

    def write(conn: Connection, release_id: Any) -> None:  # a separate, explicit causal assertion
        sup = conn.execute(
            text("SELECT support_id FROM world.occurrence WHERE entity_id = :e"), {"e": occ["C"]}
        ).scalar_one()
        rid = conn.execute(
            text("""INSERT INTO world.relation(predicate_id, layer, support_id, release_id, existence_uncertainty,
                      strength, component, provenance_activity_id)
                    VALUES (:p, :l, :s, :r, '{"status":"unquantified","kind":"unknown"}'::jsonb, NULL,
                            '{"mechanism":"synthetic test hypothesis"}'::jsonb, :a) RETURNING id"""),
            {
                "p": repo.concept_version_id(conn, world.causal_predicate),
                "l": f"test.wg.{world.tag}.causal",
                "s": sup,
                "r": release_id,
                "a": world.activity_id,
            },
        ).scalar_one()
        for i, (role, ent) in enumerate(((world.role_cause, occ["A"]), (world.role_effect, occ["C"]))):
            conn.execute(
                text("""INSERT INTO world.relation_participant(relation_id, ordinal, role_concept_id, entity_id,
                          direction) VALUES (:r, :o, :role, :e, :d)"""),
                {
                    "r": rid,
                    "o": i,
                    "role": repo.concept_version_id(conn, role),
                    "e": ent,
                    "d": "out" if i == 0 else "in",
                },
            )
        made.append(rid)

    _publish_with_pinned_relations(db, monkeypatch, write)
    (rid,) = made
    with db.api.connect() as conn:
        after = events.history_view(conn, occurrence_ids=ids)
        parts, truncated = events.occurrence_participants(conn, occ["A"])
    assert [h.edge.id for h in after.causal_hypotheses] == [rid]
    h = after.causal_hypotheses[0].as_dict()
    assert h["modality"] == "modelled_causal_hypothesis"
    assert [p["role"] for p in h["assertion"]["participants"]] == [world.role_cause, world.role_effect]
    assert after.observed_sequence == view.observed_sequence  # the sequence view is unchanged
    assert [e.id for e in parts] == [rid] and not truncated


# ---------------------------------------------------------------------------
# Read-only role
# ---------------------------------------------------------------------------


def test_library_works_under_read_only_api_role(db, world) -> None:
    with db.api.connect() as conn:
        assert conn.execute(text("SELECT current_user")).scalar_one() == "reva_api"
        nb = queries.neighbourhood(conn, FRA, context=_ev(), layers=[LAYER], max_edges=10)
        sub = queries.extract_subgraph(conn, context=_ev(), layers=[LAYER], max_edges=50)
        res = queries.path_search(conn, FRA, DZA, context=_ev(), layers=[LAYER], excluded_roles=["role.product"])
        assert nb.edges and sub.edges and res.paths
        with pytest.raises(ProgrammingError):
            conn.execute(
                text(
                    "INSERT INTO world.alias(entity_id, provider, external_key, mapping_version, "
                    "confidence_kind, provenance_activity_id) VALUES (:e, 'x', 'y', '1', 'candidate', :a)"
                ),
                {"e": FRA, "a": world.activity_id},
            )
