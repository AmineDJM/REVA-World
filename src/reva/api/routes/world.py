"""Entities, relations (bounded hyperedges), events and display locations.

Relations are read through ``reva.world.queries`` (read-only, bounded, supersession- and
retraction-aware); the HTTP schema (``RelationGraph``: truncated flag, ordered role-bound
participants) is unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import Connection, text

from reva.api import schemas as s
from reva.api.auth import require_scope
from reva.api.deps import (
    Timer,
    decode_cursor,
    origin_of,
    origin_warnings,
    page,
    page_limit,
    period_label,
    read_conn,
    release_ref,
    require_aware,
    support_summary,
)
from reva.api.errors import NotFound
from reva.belief.snapshots import release_as_of, release_by_id
from reva.kernel.errors import ValidationProblem
from reva.world import queries as wq
from reva.world.graph import Hyperedge
from reva.world.queries import GraphContext

router = APIRouter(prefix="/v1", tags=["world"], dependencies=[Depends(require_scope("read"))])

MAX_EDGES = 500
MAX_DEPTH = 2
MAX_FEATURES = 500
DISPLAY_NOTE = (
    "Display geometries (Natural Earth) are for orientation only. They are not authoritative boundaries and "
    "do not resolve disputed territory; a match_status other than reviewed_match is shown as such."
)


def resolve_entity(conn: Connection, ref: str) -> UUID:
    """Accept a REVA entity UUID or an ISO 3166 alpha-3 key (e.g. FRA)."""
    try:
        eid = UUID(ref)
        hit = conn.execute(text("SELECT id FROM world.entity WHERE id = :i"), {"i": eid}).scalar_one_or_none()
    except ValueError:
        hit = conn.execute(
            text("""SELECT entity_id FROM world.alias WHERE provider = 'iso3166_alpha3' AND external_key = :k
                    ORDER BY recorded_at DESC LIMIT 1"""),
            {"k": ref.upper()[:16]},
        ).scalar_one_or_none()
    if hit is None:
        raise NotFound("entity not found", entity=ref[:64])
    return UUID(str(hit))


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


@router.get("/entities", response_model=s.EntityListResponse)
def list_entities(
    request: Request,
    kind: str | None = Query(None, max_length=100, description="Entity kind concept, e.g. entity.country"),
    q: str | None = Query(None, min_length=1, max_length=100, description="Case-insensitive label substring"),
    alias_provider: str | None = Query(None, max_length=64),
    alias_key: str | None = Query(None, max_length=64),
    referent_mode: str | None = Query(None, max_length=20),
    cursor: str | None = None,
    limit: int = Depends(page_limit),
) -> s.EntityListResponse:
    t = Timer()
    cur = decode_cursor(cursor, 2)
    pattern = None
    if q:
        pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    with read_conn(request) as conn:
        rows = [dict(r) for r in conn.execute(
            text(
                r"""SELECT e.id, e.label, c.concept_id AS kind, e.namespace, e.referent_mode,
                       (SELECT count(*) FROM world.alias a WHERE a.entity_id = e.id) AS alias_count
                FROM world.entity e JOIN ontology.concept_version c ON c.id = e.kind_concept_id
                WHERE (CAST(:kind AS text) IS NULL OR c.concept_id = :kind)
                  AND (CAST(:mode AS text) IS NULL OR e.referent_mode = :mode)
                  AND (CAST(:pat AS text) IS NULL OR e.label ILIKE :pat ESCAPE '\')
                  AND (CAST(:ap AS text) IS NULL OR EXISTS (
                        SELECT 1 FROM world.alias a WHERE a.entity_id = e.id AND a.provider = :ap
                          AND (CAST(:ak AS text) IS NULL OR a.external_key = :ak)))
                  AND (CAST(:c0 AS text) IS NULL OR (e.label, e.id) > (CAST(:c0 AS text), CAST(:c1 AS uuid)))
                ORDER BY e.label, e.id LIMIT :lim"""
            ),
            {"kind": kind, "mode": referent_mode, "pat": pattern, "ap": alias_provider, "ak": alias_key,
             "c0": cur[0] if cur else None, "c1": cur[1] if cur else None, "lim": limit + 1},
        ).mappings()]
    rows, nxt = page(rows, limit, lambda r: (r["label"], r["id"]))
    return s.EntityListResponse(
        data=[s.EntitySummary(**r) for r in rows], context_mode="registry", origin="none", modality="metadata",
        cost=t.cost(len(rows)), next_cursor=nxt,
    )


@router.get("/entities/{entity_ref}", response_model=s.EntityDetailResponse,
            responses={404: {"model": s.Problem}})
def get_entity(entity_ref: str, request: Request) -> s.EntityDetailResponse:
    t = Timer()
    with read_conn(request) as conn:
        eid = resolve_entity(conn, entity_ref)
        ent = conn.execute(
            text("""SELECT e.id, e.label, c.concept_id AS kind, e.namespace, e.referent_mode, e.recorded_at
                    FROM world.entity e JOIN ontology.concept_version c ON c.id = e.kind_concept_id
                    WHERE e.id = :i"""), {"i": eid},
        ).mappings().one()
        aliases = [s.Alias(provider=a["provider"], external_key=a["external_key"],
                           confidence_kind=a["confidence_kind"], mapping_version=a["mapping_version"],
                           valid_range=str(a["valid_range"]) if a["valid_range"] is not None else None)
                   for a in conn.execute(text(
                       "SELECT * FROM world.alias WHERE entity_id = :i ORDER BY provider, external_key"),
                       {"i": eid}).mappings()]
        comps: list[s.EntityComponent] = []
        for r in conn.execute(text(
            """SELECT c.concept_id, count(*) AS n, min(sp.temporal_extent->>'start') AS first,
                      max(sp.temporal_extent->>'start') AS last
               FROM evidence.observation o JOIN ontology.concept_version c ON c.id = o.concept_version_id
               JOIN world.support sp ON sp.id = o.support_id
               WHERE o.subject_id = :i GROUP BY c.concept_id ORDER BY c.concept_id"""), {"i": eid}).mappings():
            comps.append(s.EntityComponent(component="observations", concept_id=r["concept_id"], count=r["n"],
                                           first_period=r["first"], last_period=r["last"]))
        latest = release_as_of(conn, None)
        if latest:
            for r in conn.execute(text(
                """SELECT c.concept_id, count(*) AS n, min(sp.temporal_extent->>'start') AS first,
                          max(sp.temporal_extent->>'start') AS last
                   FROM belief.estimate e JOIN ontology.concept_version c ON c.id = e.concept_version_id
                   JOIN world.support sp ON sp.id = e.support_id
                   WHERE e.subject_id = :i AND e.release_id = :r GROUP BY c.concept_id ORDER BY c.concept_id"""),
                    {"i": eid, "r": latest["id"]}).mappings():
                comps.append(s.EntityComponent(component="estimates", concept_id=r["concept_id"], count=r["n"],
                                               first_period=r["first"], last_period=r["last"]))
        n_geo = conn.execute(text("SELECT count(*) FROM world.display_geometry WHERE entity_id = :i"),
                             {"i": eid}).scalar_one()
        if n_geo:
            comps.append(s.EntityComponent(component="display_geometry", count=n_geo))
        for r in conn.execute(text(
            """SELECT r.layer, count(DISTINCT r.id) AS n FROM world.relation_participant p
               JOIN world.relation r ON r.id = p.relation_id WHERE p.entity_id = :i GROUP BY r.layer"""),
                {"i": eid}).mappings():
            comps.append(s.EntityComponent(component="relations", layer=r["layer"], count=r["n"]))
        contexts: list[s.SupportedContext] = []
        if any(c.component == "observations" for c in comps):
            contexts.append(s.SupportedContext(mode="evidence", description="Source records (all vintages)"))
        if latest and any(c.component == "estimates" for c in comps):
            contexts.append(s.SupportedContext(
                mode="belief", context_id=latest["context_id"], release_id=latest["id"], origin=latest["origin"],
                knowledge_cutoff=latest["knowledge_cutoff"],
                description=f"Pinned belief release #{latest['sequence']}"))
        iso = next((a.external_key for a in aliases if a.provider == "iso3166_alpha3"), None)
        if iso:
            for r in conn.execute(text(
                """SELECT sc.id, sc.context_id, c.origin, c.knowledge_cutoff, sc.spec->>'title' AS title
                   FROM sim.scenario sc JOIN meta.context c ON c.id = sc.context_id
                   WHERE sc.spec->'countries' ? :iso ORDER BY sc.recorded_at DESC LIMIT 20"""),
                    {"iso": iso}).mappings():
                contexts.append(s.SupportedContext(
                    mode="simulation", context_id=r["context_id"], origin=r["origin"],
                    knowledge_cutoff=r["knowledge_cutoff"], description=f"Scenario {r['id']}: {r['title']}"))
    detail = s.EntityDetail(**dict(ent), aliases=aliases, components=comps, supported_contexts=contexts)
    return s.EntityDetailResponse(data=detail, context_mode="registry", origin="none", modality="metadata",
                                  baseline_release=release_ref(latest), cost=t.cost(len(comps)))


# ---------------------------------------------------------------------------
# Relations
# ---------------------------------------------------------------------------

def _year_interval(period: int | None) -> tuple[datetime, datetime] | None:
    if period is None:
        return None
    return datetime(period, 1, 1, tzinfo=UTC), datetime(period + 1, 1, 1, tzinfo=UTC)


def _relation(e: Hyperedge) -> s.Relation:
    fm = e.flow_magnitude or {}
    is_evidence = e.provenance.kind == "evidence"
    value = fm.get("value") if is_evidence and isinstance(fm.get("value"), dict) and fm.get("value") else None
    sup = e.support
    temporal = sup.get("temporal_extent") or {}
    return s.Relation(
        id=e.id, predicate=e.predicate, layer=e.layer,
        support=s.SupportSummary(id=sup["id"], temporal=temporal, spatial=sup.get("spatial_spec") or {},
                                 population=sup.get("population_spec") or {}, aggregation=sup.get("aggregation"),
                                 period_label=period_label(temporal)),
        component=e.component, existence_uncertainty=e.existence_uncertainty, strength=e.tie_strength,
        value=value, concept_id=fm.get("concept_id") if is_evidence else None,
        evidence_observation_id=e.provenance.observation_id, release_id=e.provenance.release_id,
        origin=e.provenance.origin, recorded_at=e.recorded_at,  # type: ignore[arg-type]
        participants=[s.Participant(ordinal=p.ordinal, role=p.role, entity_id=p.entity_id,
                                    entity_label=p.entity_label, entity_kind=p.entity_kind, direction=p.direction)
                      for p in e.participants],
    )


def relation_graph(
    conn: Connection, *, entity_id: UUID | None, layer: str | None, period: int | None, as_of: datetime | None,
    origin: str | None, depth: int, max_edges: int, release_id: UUID | None = None,
) -> tuple[s.RelationGraph, list[str], list[str]]:
    """Visible hyperedges via ``reva.world.queries`` (supersession- and retraction-aware, bounded).

    Context: a pinned published release (``release_id``) or evidence as known at ``as_of`` (optionally
    one origin). ``depth=1`` is the entity's neighbourhood; ``depth=2`` a bounded path search from it;
    without an entity a bounded subgraph. Returns (graph, origins, library notes).
    """
    if release_id is not None:
        if as_of is not None or origin is not None:
            raise ValidationProblem("release_id pins the context; as_of/origin do not apply to it")
        ctx = GraphContext.release(release_id)
    else:
        ctx = GraphContext.evidence(as_of, origin)
    layers = [layer] if layer else None
    during = _year_interval(period)
    notes: list[str] = []
    if entity_id is None:
        es = wq.extract_subgraph(conn, context=ctx, layers=layers, valid_during=during, max_edges=max_edges)
        edges, truncated, notes = list(es.edges), es.truncated, list(es.notes)
    elif depth == 1:
        nb = wq.neighbourhood(conn, entity_id, context=ctx, layers=layers, valid_during=during, max_edges=max_edges)
        edges, truncated, notes = list(nb.edges), nb.truncated, list(nb.notes)
    else:
        pr = wq.path_search(conn, entity_id, None, context=ctx, layers=layers, valid_during=during,
                            max_depth=depth, max_edges=max_edges)
        edges, truncated = list(pr.edges), pr.truncated
        notes = list(pr.notes) + [f"bound reached: {r}" for r in pr.truncation_reasons]
    rels = [_relation(e) for e in edges]
    nodes: dict[Any, s.GraphNode] = {}
    for r in rels:
        for p in r.participants:
            nodes[p.entity_id] = s.GraphNode(id=p.entity_id, label=p.entity_label, kind=p.entity_kind)
    graph = s.RelationGraph(relations=rels, nodes=sorted(nodes.values(), key=lambda n: (n.label, str(n.id))),
                            truncated=truncated, max_edges=max_edges, depth=depth,
                            matched_lower_bound=len(rels) + (1 if truncated else 0))
    return graph, [r.origin for r in rels if r.origin], notes


def relations_response(conn: Connection, t: Timer, **kw: Any) -> s.RelationListResponse:
    graph, origins, notes = relation_graph(conn, **kw)
    origin = origin_of(origins)
    warnings = origin_warnings(origin)
    if graph.truncated:
        warnings.append(s.ApiWarning(
            code="truncated",
            message=f"Result bounded at max_edges={graph.max_edges}, depth={graph.depth}; more relations match. "
                    "Narrow the query (layer, period, entity) to see them."))
    warnings += [s.ApiWarning(code="graph_note", message=n[:500]) for n in notes]
    rel = None
    if kw.get("release_id") is not None:
        rel = release_by_id(conn, kw["release_id"])
    return s.RelationListResponse(
        data=graph, context_id=rel["context_id"] if rel else None,
        context_mode="belief" if rel else "evidence", origin=origin,  # type: ignore[arg-type]
        modality="selected_estimate" if rel else "reported_observation",
        baseline_release=release_ref(rel), knowledge_cutoff=rel["knowledge_cutoff"] if rel else kw.get("as_of"),
        assumptions=["Relations are reported flows; existence is as reported by the source",
                     "Superseded and retracted observations are excluded as known at the as_of time"],
        warnings=warnings, cost=t.cost(len(graph.relations), truncated=graph.truncated),
    )


@router.get("/relations", response_model=s.RelationListResponse)
def list_relations(
    request: Request,
    entity_id: str | None = Query(None, max_length=64, description="Entity UUID or ISO3 key"),
    layer: str | None = Query(None, max_length=200),
    period: int | None = Query(None, ge=1900, le=2100, description="Calendar year of the valid support"),
    as_of: datetime | None = Query(None, description="Evidence as known at this instant (recorded, not "
                                   "superseded, not retracted at/before it)"),
    origin: str | None = Query(None, pattern="^(live|test_fixture)$"),
    release_id: UUID | None = Query(None, description="Relations pinned to this published belief release instead "
                                    "of evidence (excludes as_of/origin)"),
    depth: int = Query(1, ge=1, le=MAX_DEPTH),
    max_edges: int = Query(MAX_EDGES, ge=1, le=MAX_EDGES),
) -> s.RelationListResponse:
    """Bounded hyperedge query with participants and roles. Never silently truncated."""
    t = Timer()
    require_aware(as_of, "as_of")
    with read_conn(request) as conn:
        eid = resolve_entity(conn, entity_id) if entity_id else None
        return relations_response(conn, t, entity_id=eid, layer=layer, period=period, as_of=as_of, origin=origin,
                                  depth=depth, max_edges=max_edges, release_id=release_id)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@router.get("/events", response_model=s.EventListResponse)
def list_events(
    request: Request,
    entity_id: UUID | None = None,
    event_type: str | None = Query(None, max_length=200),
    cursor: str | None = None,
    limit: int = Depends(page_limit),
) -> s.EventListResponse:
    """Occurrences (real-world events). An empty list means none are recorded."""
    t = Timer()
    cur = decode_cursor(cursor, 1)
    with read_conn(request) as conn:
        rows = [dict(r) for r in conn.execute(text(
            """SELECT oc.entity_id, e.label, c.concept_id AS event_type, oc.component, sp.id AS support_id,
                      sp.temporal_extent, sp.spatial_spec, sp.population_spec, sp.aggregation
               FROM world.occurrence oc JOIN world.entity e ON e.id = oc.entity_id
               JOIN ontology.concept_version c ON c.id = oc.event_type_id
               JOIN world.support sp ON sp.id = oc.support_id
               WHERE (CAST(:e AS uuid) IS NULL OR oc.entity_id = :e)
                 AND (CAST(:et AS text) IS NULL OR c.concept_id = :et)
                 AND (CAST(:c0 AS uuid) IS NULL OR oc.entity_id > CAST(:c0 AS uuid))
               ORDER BY oc.entity_id LIMIT :lim"""),
            {"e": entity_id, "et": event_type, "c0": cur[0] if cur else None, "lim": limit + 1}).mappings()]
    rows, nxt = page(rows, limit, lambda r: (r["entity_id"],))
    data = [s.Event(entity_id=r["entity_id"], label=r["label"], event_type=r["event_type"],
                    support=support_summary(r), component=r["component"]) for r in rows]
    warnings = [] if data else [s.ApiWarning(code="no_events", message="No occurrences are recorded for this query")]
    return s.EventListResponse(data=data, context_mode="evidence", origin="none", modality="reported_observation",
                               warnings=warnings, cost=t.cost(len(data)), next_cursor=nxt)


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


@router.get("/locations", response_model=s.LocationResponse)
def list_locations(
    request: Request,
    entity_id: str | None = Query(None, max_length=64),
    match_status: str | None = Query(None, pattern="^(reviewed_match|unmatched|disputed|overseas_part)$"),
    dataset_sha256: str | None = Query(None, pattern="^[0-9a-f]{64}$"),
    precision: int = Query(3, ge=1, le=6, description="Coordinate decimal places"),
) -> s.LocationResponse:
    """Display geometries as a GeoJSON FeatureCollection (latest dataset by default)."""
    t = Timer()
    with read_conn(request) as conn:
        eid = resolve_entity(conn, entity_id) if entity_id else None
        ds = dataset_sha256 or conn.execute(text(
            "SELECT dataset_sha256 FROM world.display_geometry ORDER BY recorded_at DESC LIMIT 1")).scalar_one_or_none()
        rows = [dict(r) for r in conn.execute(text(
            """SELECT g.id, g.feature_key, g.entity_id, e.label AS entity_label, g.match_status, g.properties,
                      g.dataset_sha256, ST_AsGeoJSON(g.geometry_4326, :prec)::jsonb AS geometry
               FROM world.display_geometry g LEFT JOIN world.entity e ON e.id = g.entity_id
               WHERE g.dataset_sha256 = :ds
                 AND (CAST(:e AS uuid) IS NULL OR g.entity_id = :e)
                 AND (CAST(:ms AS text) IS NULL OR g.match_status = :ms)
               ORDER BY g.feature_key LIMIT :lim"""),
            {"ds": ds, "e": eid, "ms": match_status, "prec": precision, "lim": MAX_FEATURES + 1}).mappings()]
        origins = list(conn.execute(text(
            "SELECT DISTINCT origin FROM evidence.acquisition WHERE raw_sha256 = :ds"), {"ds": ds}).scalars()) if ds \
            else []
    truncated = len(rows) > MAX_FEATURES
    rows = rows[:MAX_FEATURES]
    feats = []
    for r in rows:
        props = r["properties"] or {}
        name = props.get("NAME") or props.get("ADMIN") or props.get("name")
        feats.append(s.GeoFeature(id=str(r["id"]), geometry=r["geometry"], properties=s.GeoFeatureProperties(
            feature_key=r["feature_key"], entity_id=r["entity_id"], entity_label=r["entity_label"],
            match_status=r["match_status"], name=str(name) if name is not None else None,
            dataset_sha256=r["dataset_sha256"])))
    origin = origin_of(origins)
    warnings = [*origin_warnings(origin), s.ApiWarning(code="display_only", message=DISPLAY_NOTE)]
    if truncated:
        warnings.append(s.ApiWarning(code="truncated", message=f"Bounded at {MAX_FEATURES} features"))
    if not feats:
        warnings.append(s.ApiWarning(code="no_geometry", message="No display geometry has been ingested"))
    return s.LocationResponse(
        data=s.FeatureCollection(features=feats, display_note=DISPLAY_NOTE, truncated=truncated),
        context_mode="evidence", origin=origin, modality="metadata", warnings=warnings,  # type: ignore[arg-type]
        cost=t.cost(len(feats)),
    )
