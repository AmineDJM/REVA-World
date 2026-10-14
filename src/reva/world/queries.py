"""Read-only hypergraph queries over ``world.relation`` / ``world.relation_participant``.

Every function takes an explicit SQLAlchemy ``Connection`` and only issues
``SELECT`` statements (plus ``SET LOCAL statement_timeout`` inside a savepoint
for bounded queries), so the library works under the ``reva_api`` role.

Contexts (:class:`GraphContext`)
--------------------------------
* ``release`` -- edges pinned to one *published* belief release
  (``world.relation.release_id``).
* ``evidence`` -- edges derived from one evidence observation
  (``evidence_observation_id``), visible at ``as_of`` when the observation and
  the edge were recorded at/before ``as_of``, the observation was neither
  superseded by a revision nor retracted at/before ``as_of``. With
  ``as_of=None`` the latest recorded state is used.

Bounds are explicit and results carry ``truncated`` -- nothing is silently cut.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from reva.belief.snapshots import release_as_of
from reva.kernel.errors import IncompatibleSemantics, ValidationProblem
from reva.world.graph import (
    DEFAULT_DEPTH,
    DEFAULT_MAX_EDGES,
    DEFAULT_TIMEOUT_MS,
    MAX_SUBGRAPH_EDGES,
    NOT_ASSESSED_CONFIDENCE,
    NOT_ASSESSED_STRENGTH,
    EdgeProvenance,
    EdgeSet,
    Hyperedge,
    Participant,
    check_bounds,
    check_interval,
    require_aware,
)

ORIGINS = ("live", "test_fixture")


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GraphContext:
    """Which edges are visible: a pinned release, or evidence as known at ``as_of``."""

    mode: Literal["release", "evidence"]
    release_id: UUID | None = None
    as_of: datetime | None = None
    origin: str | None = None

    def __post_init__(self) -> None:
        if self.mode == "release":
            if self.release_id is None:
                raise ValidationProblem("release context requires a release_id")
            if self.as_of is not None or self.origin is not None:
                raise ValidationProblem("a release context is pinned by release_id; as_of/origin do not apply")
        elif self.mode == "evidence":
            if self.release_id is not None:
                raise ValidationProblem("evidence context takes no release_id")
            require_aware(self.as_of, "as_of")
            if self.origin is not None and self.origin not in ORIGINS:
                raise ValidationProblem(f"origin must be one of {ORIGINS}", origin=self.origin)
        else:
            raise ValidationProblem("mode must be 'release' or 'evidence'", mode=str(self.mode))

    @classmethod
    def evidence(cls, as_of: datetime | None = None, origin: str | None = None) -> GraphContext:
        return cls(mode="evidence", as_of=as_of, origin=origin)

    @classmethod
    def release(cls, release_id: UUID) -> GraphContext:
        return cls(mode="release", release_id=release_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "release_id": None if self.release_id is None else str(self.release_id),
            "as_of": None if self.as_of is None else self.as_of.isoformat(),
            "origin": self.origin,
        }


def release_context_as_of(conn: Connection, as_of: datetime | None, origin: str | None = None) -> GraphContext | None:
    """The release context REVA had actually published at ``as_of`` (``None`` if none was)."""
    require_aware(as_of, "as_of")
    rel = release_as_of(conn, as_of, origin)
    return None if rel is None else GraphContext.release(UUID(str(rel["id"])))


# ---------------------------------------------------------------------------
# SQL building blocks (fixed text; only bind parameters vary)
# ---------------------------------------------------------------------------

_COMMON_FILTERS = """
    AND (CAST(:layers AS text[]) IS NULL OR r.layer = ANY(CAST(:layers AS text[])))
    AND (CAST(:predicate AS text) IS NULL OR pc.concept_id = :predicate)
    AND (CAST(:vstart AS timestamptz) IS NULL OR sp.valid_range &&
         tstzrange(CAST(:vstart AS timestamptz), CAST(:vend AS timestamptz), '[)'))
    AND (CAST(:focal AS uuid) IS NULL OR EXISTS (
          SELECT 1 FROM world.relation_participant fp
          JOIN ontology.concept_version frc ON frc.id = fp.role_concept_id
          WHERE fp.relation_id = r.id AND fp.entity_id = CAST(:focal AS uuid)
            AND (CAST(:roles AS text[]) IS NULL OR frc.concept_id = ANY(CAST(:roles AS text[])))))
"""

_VISIBLE_EVIDENCE = (
    """
visible AS (
  SELECT r.id, lower(sp.valid_range) AS valid_start
  FROM world.relation r
  JOIN world.support sp ON sp.id = r.support_id
  JOIN ontology.concept_version pc ON pc.id = r.predicate_id
  JOIN evidence.observation o ON o.id = r.evidence_observation_id
  JOIN evidence.acquisition a ON a.id = o.acquisition_id
  WHERE r.evidence_observation_id IS NOT NULL
    AND (CAST(:as_of AS timestamptz) IS NULL OR (r.recorded_at <= :as_of AND o.recorded_at <= :as_of))
    AND (CAST(:origin AS text) IS NULL OR a.origin = :origin)
    AND NOT EXISTS (SELECT 1 FROM evidence.observation o2 WHERE o2.supersedes_id = o.id
                    AND (CAST(:as_of AS timestamptz) IS NULL OR o2.recorded_at <= :as_of))
    AND NOT EXISTS (SELECT 1 FROM evidence.observation_retraction t WHERE t.observation_id = o.id
                    AND (CAST(:as_of AS timestamptz) IS NULL OR t.recorded_at <= :as_of))
"""
    + _COMMON_FILTERS
    + ")"
)

_VISIBLE_RELEASE = (
    """
visible AS (
  SELECT r.id, lower(sp.valid_range) AS valid_start
  FROM world.relation r
  JOIN world.support sp ON sp.id = r.support_id
  JOIN ontology.concept_version pc ON pc.id = r.predicate_id
  WHERE r.release_id = CAST(:release_id AS uuid)
    AND EXISTS (SELECT 1 FROM ops.publication pub WHERE pub.release_id = r.release_id)
"""
    + _COMMON_FILTERS
    + ")"
)

_DETAIL_SQL = """
SELECT r.id, pc.concept_id AS predicate, pc.version AS predicate_version, r.layer, r.component,
       r.existence_uncertainty, r.strength, r.evidence_observation_id, r.release_id, r.recorded_at,
       r.provenance_activity_id, sp.id AS support_id, sp.temporal_extent, lower(sp.valid_range) AS valid_start,
       upper(sp.valid_range) AS valid_end, sp.spatial_spec, sp.population_spec, sp.aggregation, sp.resolution,
       o.value AS obs_value, oc.concept_id AS obs_concept, oc.version AS obs_concept_version, o.revision_no,
       o.recorded_at AS obs_recorded_at, src.source_key, COALESCE(a.origin, rel.origin) AS origin
FROM world.relation r
JOIN ontology.concept_version pc ON pc.id = r.predicate_id
JOIN world.support sp ON sp.id = r.support_id
LEFT JOIN evidence.observation o ON o.id = r.evidence_observation_id
LEFT JOIN ontology.concept_version oc ON oc.id = o.concept_version_id
LEFT JOIN evidence.source src ON src.id = o.source_id
LEFT JOIN evidence.acquisition a ON a.id = o.acquisition_id
LEFT JOIN belief.release rel ON rel.id = r.release_id
WHERE r.id = ANY(:ids)
"""

_PARTICIPANTS_SQL = """
SELECT p.relation_id, p.ordinal, rc.concept_id AS role, p.entity_id, e.label AS entity_label,
       ec.concept_id AS entity_kind, p.direction
FROM world.relation_participant p
JOIN ontology.concept_version rc ON rc.id = p.role_concept_id
JOIN world.entity e ON e.id = p.entity_id
JOIN ontology.concept_version ec ON ec.id = e.kind_concept_id
WHERE p.relation_id = ANY(:ids)
ORDER BY p.relation_id, p.ordinal
"""


def _visible_cte(ctx: GraphContext) -> str:
    return _VISIBLE_RELEASE if ctx.mode == "release" else _VISIBLE_EVIDENCE


def _params(
    ctx: GraphContext,
    *,
    layers: Sequence[str] | None = None,
    predicate: str | None = None,
    valid_during: tuple[datetime, datetime] | None = None,
    focal: UUID | None = None,
    roles: Sequence[str] | None = None,
) -> dict[str, Any]:
    interval = check_interval(valid_during)
    return {
        "as_of": ctx.as_of,
        "origin": ctx.origin,
        "release_id": ctx.release_id,
        "layers": list(layers) if layers else None,
        "predicate": predicate,
        "vstart": interval[0] if interval else None,
        "vend": interval[1] if interval else None,
        "focal": focal,
        "roles": list(roles) if roles else None,
    }


def _is_timeout(exc: DBAPIError) -> bool:
    return getattr(exc.orig, "sqlstate", None) == "57014"


def _run_bounded(conn: Connection, timeout_ms: int, sql: str, params: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Run one query under ``SET LOCAL statement_timeout`` inside a savepoint.

    Returns ``None`` when the time limit cancelled the statement; the savepoint
    is rolled back so the caller's transaction stays usable, and the previous
    ``statement_timeout`` is restored in every case. A stricter limit already
    set on the session is kept.
    """
    nested = conn.begin_nested()
    try:
        prev, prev_ms = conn.execute(
            text(
                "SELECT current_setting('statement_timeout'), setting::bigint FROM pg_settings "
                "WHERE name = 'statement_timeout'"
            )
        ).one()
        # Never loosen a stricter limit the session already has (e.g. the API engine's).
        effective = int(timeout_ms) if not prev_ms else min(int(timeout_ms), int(prev_ms))
        conn.exec_driver_sql(f"SET LOCAL statement_timeout = {effective}")
        rows = [dict(r) for r in conn.execute(text(sql), params).mappings()]
        conn.execute(text("SELECT set_config('statement_timeout', :v, true)"), {"v": prev})
    except DBAPIError as exc:
        nested.rollback()
        if _is_timeout(exc):
            return None
        raise
    except BaseException:
        nested.rollback()
        raise
    nested.commit()
    return rows


def _check_release(conn: Connection, ctx: GraphContext) -> list[str]:
    if ctx.mode != "release":
        return []
    row = conn.execute(
        text(
            """SELECT r.id, p.sequence FROM belief.release r LEFT JOIN ops.publication p ON p.release_id = r.id
               WHERE r.id = :i"""
        ),
        {"i": ctx.release_id},
    ).one_or_none()
    if row is None:
        return [f"release {ctx.release_id} does not exist; no edges are visible"]
    if row[1] is None:
        return [f"release {ctx.release_id} is not published; unpublished releases expose no edges"]
    return []


# ---------------------------------------------------------------------------
# Hyperedge loading / inspection
# ---------------------------------------------------------------------------


def _flow_magnitude(d: dict[str, Any]) -> dict[str, Any]:
    if d["evidence_observation_id"] is not None:
        value = d["obs_value"] or {}
        return {
            "status": "missing" if value.get("kind") == "missing" else "reported",
            "value": value,
            "concept_id": d["obs_concept"],
            "concept_version": d["obs_concept_version"],
            "basis": "evidence observation value as reported by the source",
        }
    comp = d["component"] or {}
    if isinstance(comp.get("flow_magnitude"), dict):
        return {"status": "selected", **comp["flow_magnitude"]}
    return {"status": "not_recorded", "note": "this release-pinned relation carries no flow magnitude"}


def _support(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(d["support_id"]),
        "temporal_extent": d["temporal_extent"],
        "valid_start": None if d["valid_start"] is None else d["valid_start"].isoformat(),
        "valid_end": None if d["valid_end"] is None else d["valid_end"].isoformat(),
        "spatial_spec": d["spatial_spec"],
        "population_spec": d["population_spec"],
        "aggregation": d["aggregation"],
        "resolution": d["resolution"],
    }


def load_hyperedges(conn: Connection, relation_ids: Sequence[UUID]) -> list[Hyperedge]:
    """Load hyperedges by id with ordered participants (no visibility filter).

    Output order: valid start (unknown last), layer, id.
    """
    if not relation_ids:
        return []
    ids = list(dict.fromkeys(relation_ids))
    parts: dict[UUID, list[Participant]] = defaultdict(list)
    for p in conn.execute(text(_PARTICIPANTS_SQL), {"ids": ids}).mappings():
        parts[UUID(str(p["relation_id"]))].append(
            Participant(
                ordinal=int(p["ordinal"]),
                role=p["role"],
                entity_id=UUID(str(p["entity_id"])),
                entity_label=p["entity_label"],
                entity_kind=p["entity_kind"],
                direction=p["direction"],
            )
        )
    out: list[Hyperedge] = []
    for d in (dict(r) for r in conn.execute(text(_DETAIL_SQL), {"ids": ids}).mappings()):
        rid = UUID(str(d["id"]))
        comp = dict(d["component"] or {})
        prov = EdgeProvenance(
            kind="evidence" if d["evidence_observation_id"] is not None else "release",
            release_id=None if d["release_id"] is None else UUID(str(d["release_id"])),
            observation_id=None if d["evidence_observation_id"] is None else UUID(str(d["evidence_observation_id"])),
            origin=d["origin"],
            source_key=d["source_key"],
            revision_no=d["revision_no"],
            observation_recorded_at=d["obs_recorded_at"],
            provenance_activity_id=UUID(str(d["provenance_activity_id"])),
        )
        sci = comp.get("scientific_confidence")
        out.append(
            Hyperedge(
                id=rid,
                predicate=d["predicate"],
                predicate_version=d["predicate_version"],
                layer=d["layer"],
                support=_support(d),
                provenance=prov,
                component=comp,
                existence_uncertainty=dict(d["existence_uncertainty"]),
                flow_magnitude=_flow_magnitude(d),
                tie_strength=dict(d["strength"]) if isinstance(d["strength"], dict) else dict(NOT_ASSESSED_STRENGTH),
                scientific_confidence=dict(sci) if isinstance(sci, dict) else dict(NOT_ASSESSED_CONFIDENCE),
                recorded_at=d["recorded_at"],
                participants=tuple(sorted(parts.get(rid, []), key=lambda p: p.ordinal)),
            )
        )
    out.sort(key=lambda e: (e.support["valid_start"] is None, e.support["valid_start"] or "", e.layer, str(e.id)))
    return out


def get_hyperedge(conn: Connection, relation_id: UUID, *, context: GraphContext | None = None) -> Hyperedge | None:
    """Inspect one hyperedge with ordered, role-bound participants.

    With ``context`` the edge is returned only if visible in that context
    (e.g. not yet recorded at ``as_of`` -> ``None``).
    """
    if context is not None:
        params = _params(context)
        params["rid"] = relation_id
        visible = conn.execute(
            text("WITH " + _visible_cte(context) + " SELECT 1 FROM visible WHERE id = :rid"), params
        ).scalar_one_or_none()
        if visible is None:
            return None
    edges = load_hyperedges(conn, [relation_id])
    return edges[0] if edges else None


def extract_subgraph(
    conn: Connection,
    *,
    context: GraphContext,
    layers: Sequence[str] | None = None,
    predicate: str | None = None,
    valid_during: tuple[datetime, datetime] | None = None,
    max_edges: int = MAX_SUBGRAPH_EDGES,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> EdgeSet:
    """Extract a bounded set of visible hyperedges, e.g. for projections or NetworkX.

    ``truncated`` is set when more edges match than ``max_edges`` (at most 50,000)
    or when the time limit was hit.
    """
    if not 1 <= max_edges <= MAX_SUBGRAPH_EDGES:
        raise ValidationProblem(f"max_edges must be within 1..{MAX_SUBGRAPH_EDGES}")
    check_bounds(1, 1, timeout_ms)
    notes = _check_release(conn, context)
    params = _params(context, layers=layers, predicate=predicate, valid_during=valid_during)
    params["lim"] = max_edges + 1
    rows = _run_bounded(
        conn,
        timeout_ms,
        "WITH " + _visible_cte(context) + " SELECT id FROM visible ORDER BY valid_start NULLS LAST, id LIMIT :lim",
        params,
    )
    if rows is None:
        return EdgeSet((), True, max_edges, (*notes, f"time limit of {timeout_ms} ms reached; no edges returned"))
    truncated = len(rows) > max_edges
    ids = [UUID(str(r["id"])) for r in rows[:max_edges]]
    return EdgeSet(tuple(load_hyperedges(conn, ids)), truncated, max_edges, tuple(notes))


# ---------------------------------------------------------------------------
# Neighbourhood
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Neighbour:
    entity_id: UUID
    label: str
    kind: str
    roles: tuple[str, ...]
    focal_roles: tuple[str, ...]
    layers: tuple[str, ...]
    edge_ids: tuple[UUID, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": str(self.entity_id),
            "label": self.label,
            "kind": self.kind,
            "roles": list(self.roles),
            "focal_roles": list(self.focal_roles),
            "layers": list(self.layers),
            "edge_ids": [str(e) for e in self.edge_ids],
            "n_edges": len(self.edge_ids),
        }


@dataclass(frozen=True)
class Neighbourhood:
    entity_id: UUID
    context: GraphContext
    edges: tuple[Hyperedge, ...]
    neighbours: tuple[Neighbour, ...]
    truncated: bool
    max_edges: int
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": str(self.entity_id),
            "context": self.context.as_dict(),
            "edges": [e.as_dict() for e in self.edges],
            "neighbours": [n.as_dict() for n in self.neighbours],
            "truncated": self.truncated,
            "max_edges": self.max_edges,
            "notes": list(self.notes),
        }


def _neighbours(focal: UUID, edges: Iterable[Hyperedge]) -> list[Neighbour]:
    acc: dict[UUID, dict[str, Any]] = {}
    for e in edges:
        focal_roles = {p.role for p in e.participants if p.entity_id == focal}
        for p in e.participants:
            if p.entity_id == focal:
                continue
            n = acc.setdefault(
                p.entity_id,
                {
                    "label": p.entity_label,
                    "kind": p.entity_kind,
                    "roles": set(),
                    "focal": set(),
                    "layers": set(),
                    "edges": {},
                },
            )
            n["roles"].add(p.role)
            n["focal"].update(focal_roles)
            n["layers"].add(e.layer)
            n["edges"].setdefault(e.id, None)
    out = [
        Neighbour(
            entity_id=eid,
            label=n["label"],
            kind=n["kind"],
            roles=tuple(sorted(n["roles"])),
            focal_roles=tuple(sorted(n["focal"])),
            layers=tuple(sorted(n["layers"])),
            edge_ids=tuple(n["edges"]),
        )
        for eid, n in acc.items()
    ]
    out.sort(key=lambda n: (n.label, str(n.entity_id)))
    return out


def neighbourhood(
    conn: Connection,
    entity_id: UUID,
    *,
    context: GraphContext,
    layers: Sequence[str] | None = None,
    valid_during: tuple[datetime, datetime] | None = None,
    roles: Sequence[str] | None = None,
    predicate: str | None = None,
    max_edges: int = DEFAULT_MAX_EDGES,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> Neighbourhood:
    """Hyperedges incident to ``entity_id`` and the co-participating entities.

    Filters: ``layers``; ``valid_during`` (half-open interval overlapping the
    edge support's valid range -- supports with unknown range are excluded);
    ``roles`` restricts the role the *focal* entity plays. Edges are ordered by
    valid start then id; ``truncated`` reports that more edges match.
    """
    check_bounds(1, max_edges, timeout_ms)
    notes = list(_check_release(conn, context))
    params = _params(
        context, layers=layers, predicate=predicate, valid_during=valid_during, focal=entity_id, roles=roles
    )
    params["lim"] = max_edges + 1
    rows = _run_bounded(
        conn,
        timeout_ms,
        "WITH " + _visible_cte(context) + " SELECT id FROM visible ORDER BY valid_start NULLS LAST, id LIMIT :lim",
        params,
    )
    if rows is None:
        notes.append(f"time limit of {timeout_ms} ms reached; no edges returned")
        return Neighbourhood(entity_id, context, (), (), True, max_edges, tuple(notes))
    truncated = len(rows) > max_edges
    edges = load_hyperedges(conn, [UUID(str(r["id"])) for r in rows[:max_edges]])
    if not edges:
        notes.append("no visible hyperedges for this entity in this context")
    return Neighbourhood(
        entity_id, context, tuple(edges), tuple(_neighbours(entity_id, edges)), truncated, max_edges, tuple(notes)
    )


# ---------------------------------------------------------------------------
# Bounded path search
# ---------------------------------------------------------------------------

_PATH_TAIL = """,
inc AS (
  SELECT DISTINCT p.relation_id, p.entity_id
  FROM world.relation_participant p
  JOIN visible v ON v.id = p.relation_id
  JOIN ontology.concept_version rc ON rc.id = p.role_concept_id
  WHERE NOT (rc.concept_id = ANY(CAST(:excluded_roles AS text[])))
),
walk(entity_id, depth, path_entities, path_edges) AS (
  SELECT CAST(:source AS uuid), 0, ARRAY[CAST(:source AS uuid)], ARRAY[]::uuid[]
  UNION ALL
  SELECT b.entity_id, w.depth + 1, w.path_entities || b.entity_id, w.path_edges || a.relation_id
  FROM walk w
  JOIN inc a ON a.entity_id = w.entity_id
  JOIN inc b ON b.relation_id = a.relation_id AND b.entity_id <> w.entity_id
  WHERE w.depth < :max_depth
    AND NOT (b.entity_id = ANY(w.path_entities))
    AND NOT (a.relation_id = ANY(w.path_edges))
    AND (CAST(:target AS uuid) IS NULL OR w.entity_id <> CAST(:target AS uuid))
)
SELECT entity_id, depth, path_entities, path_edges FROM walk
WHERE depth > 0 AND (CAST(:target AS uuid) IS NULL OR entity_id = CAST(:target AS uuid))
LIMIT :row_cap
"""


def _path_sql(ctx: GraphContext) -> str:
    return "WITH RECURSIVE " + _visible_cte(ctx) + _PATH_TAIL


@dataclass(frozen=True)
class Path:
    entity_ids: tuple[UUID, ...]
    edge_ids: tuple[UUID, ...]

    @property
    def depth(self) -> int:
        return len(self.edge_ids)

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_ids": [str(e) for e in self.entity_ids],
            "edge_ids": [str(e) for e in self.edge_ids],
            "depth": self.depth,
        }


@dataclass(frozen=True)
class PathResult:
    source_id: UUID
    target_id: UUID | None
    context: GraphContext
    paths: tuple[Path, ...]
    reached: dict[UUID, int]
    edges: tuple[Hyperedge, ...]
    truncated: bool
    truncation_reasons: tuple[str, ...]
    bounds: dict[str, Any]
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": str(self.source_id),
            "target_id": None if self.target_id is None else str(self.target_id),
            "context": self.context.as_dict(),
            "paths": [p.as_dict() for p in self.paths],
            "reached": {str(k): v for k, v in self.reached.items()},
            "edges": [e.as_dict() for e in self.edges],
            "truncated": self.truncated,
            "truncation_reasons": list(self.truncation_reasons),
            "bounds": self.bounds,
            "notes": list(self.notes),
        }


def path_search(
    conn: Connection,
    source_id: UUID,
    target_id: UUID | None = None,
    *,
    context: GraphContext,
    layers: Sequence[str] | None = None,
    valid_during: tuple[datetime, datetime] | None = None,
    predicate: str | None = None,
    excluded_roles: Sequence[str] = (),
    max_depth: int = DEFAULT_DEPTH,
    max_edges: int = DEFAULT_MAX_EDGES,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> PathResult:
    """Bounded simple-path search through hyperedges (entity -> hyperedge -> entity).

    One recursive CTE, breadth-first, without revisiting entities or edges on a
    path. Without ``target_id`` every reachable entity within ``max_depth`` is
    returned with one path row per walk; with ``target_id`` only paths ending at
    the target. ``excluded_roles`` removes pivot participants (for instance
    ``role.product``, which otherwise connects every trade flow).

    Bounds: ``max_depth`` 1..4 (default 2), ``max_edges`` 1..500 distinct edges
    across returned paths (default 500), ``timeout_ms`` applied with
    ``SET LOCAL statement_timeout``. When any bound is hit ``truncated`` is
    true and ``truncation_reasons`` says which.
    """
    check_bounds(max_depth, max_edges, timeout_ms)
    notes = list(_check_release(conn, context))
    params = _params(context, layers=layers, predicate=predicate, valid_during=valid_during)
    params.update(
        {
            "source": source_id,
            "target": target_id,
            "excluded_roles": list(excluded_roles),
            "max_depth": max_depth,
            "row_cap": max_edges + 1,
        }
    )
    bounds = {"max_depth": max_depth, "max_edges": max_edges, "timeout_ms": timeout_ms}
    rows = _run_bounded(conn, timeout_ms, _path_sql(context), params)
    if rows is None:
        notes.append("the time limit cancelled the search before any result was produced")
        return PathResult(source_id, target_id, context, (), {}, (), True, ("time_limit",), bounds, tuple(notes))
    reasons: list[str] = []
    if len(rows) > max_edges:
        reasons.append("row_limit")
    seen_edges: dict[UUID, None] = {}
    paths: list[Path] = []
    reached: dict[UUID, int] = {}
    for r in rows[:max_edges]:
        p = Path(tuple(UUID(str(x)) for x in r["path_entities"]), tuple(UUID(str(x)) for x in r["path_edges"]))
        new = [e for e in p.edge_ids if e not in seen_edges]
        if len(seen_edges) + len(new) > max_edges:
            reasons.append("edge_limit")
            break
        for e in new:
            seen_edges[e] = None
        paths.append(p)
        end = UUID(str(r["entity_id"]))
        reached[end] = min(reached.get(end, p.depth), p.depth)
    if not paths and not reasons:
        notes.append("no path within the depth bound in this context" if target_id else "no reachable entity")
    edges = load_hyperedges(conn, list(seen_edges))
    return PathResult(
        source_id,
        target_id,
        context,
        tuple(paths),
        reached,
        tuple(edges),
        bool(reasons),
        tuple(reasons),
        bounds,
        tuple(notes),
    )


# ---------------------------------------------------------------------------
# Flow aggregation with semantic compatibility checks
# ---------------------------------------------------------------------------

ADDITIVE_SCALE_TYPES = ("flow", "stock", "count")


@dataclass(frozen=True)
class FlowItem:
    """One flow magnitude with the semantics that decide whether it may be added."""

    id: UUID
    record_kind: Literal["observation", "estimate"]
    subject_id: UUID
    concept_id: str
    concept_version: str
    scale_type: str | None
    breakdown: str
    support_id: UUID
    temporal_extent: dict[str, Any]
    value: dict[str, Any]
    classification: str | None
    flow: str | None
    partner_entity_id: UUID | None = None
    partner_kind: str | None = None
    uncertainty_status: str | None = None
    provider_derived: bool = False

    @property
    def is_missing(self) -> bool:
        return self.value.get("kind") != "decimal"

    @property
    def unit_ref(self) -> str | None:
        return self.value.get("unit_ref")

    @property
    def basis(self) -> dict[str, Any]:
        b = self.value.get("basis")
        return b if isinstance(b, dict) else {}

    @property
    def period(self) -> tuple[str | None, str | None, str | None]:
        t = self.temporal_extent or {}
        return (t.get("start"), t.get("end"), t.get("label"))


@dataclass(frozen=True)
class FlowGroup:
    period_start: str | None
    period_end: str | None
    period_label: str | None
    total: dict[str, Any]
    n_items: int
    n_missing: int
    is_lower_bound: bool
    item_ids: tuple[UUID, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "period": {"start": self.period_start, "end": self.period_end, "label": self.period_label},
            "total": self.total,
            "n_items": self.n_items,
            "n_missing": self.n_missing,
            "is_lower_bound": self.is_lower_bound,
            "item_ids": [str(i) for i in self.item_ids],
        }


@dataclass(frozen=True)
class FlowAggregate:
    record_kind: str | None
    semantics: dict[str, Any]
    groups: tuple[FlowGroup, ...]
    modality: str
    uncertainty: dict[str, Any]
    warnings: tuple[str, ...]
    excluded: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_kind": self.record_kind,
            "semantics": self.semantics,
            "groups": [g.as_dict() for g in self.groups],
            "modality": self.modality,
            "derived": True,
            "uncertainty": self.uncertainty,
            "warnings": list(self.warnings),
            "excluded": list(self.excluded),
            "field_note": "an aggregate is a flow magnitude only; existence uncertainty, tie strength and "
            "scientific confidence are not aggregated",
        }


def _breakdown(spatial_spec: dict[str, Any], resolution: dict[str, Any]) -> str:
    partner = (resolution or {}).get("partner")
    scheme = (spatial_spec or {}).get("scheme")
    if partner is not None and str(partner) == "0":
        return "partner_world_total"
    if scheme == "provider_reporter_partner_pair" or partner is not None:
        return "bilateral"
    return str(scheme or "unspecified")


def _uniform(items: Sequence[FlowItem], what: str, key: Callable[[FlowItem], Any], code: str, msg: str) -> Any:
    values = {key(i) for i in items}
    if len(values) > 1:
        raise IncompatibleSemantics(
            f"{msg}: found {sorted(map(str, values))}", reason=code, field=what, values=sorted(map(str, values))
        )
    return next(iter(values)) if values else None


def aggregate_flow_items(items: Sequence[FlowItem], *, skip_missing: bool = False) -> FlowAggregate:
    """Sum compatible flow magnitudes per period; refuse incompatible semantics.

    Refused with :class:`IncompatibleSemantics` (``details.reason``):

    * ``mixed_record_kind`` -- source claims and accepted estimates together;
    * ``mixed_breakdown`` -- partner-world totals with bilateral breakdowns
      (the total already contains the partner flows: double counting);
    * ``non_additive_quantity`` -- intensive quantities;
    * ``mixed_concept``, ``mixed_unit``, ``mixed_currency_basis``,
      ``mixed_classification`` (classification revisions, e.g. H5 vs H6),
      ``mixed_valuation`` (CIF vs FOB vs undetermined), ``mixed_flow_direction``;
    * ``duplicate_record`` -- two records for the same subject/concept/support
      (e.g. two revisions of one lineage).

    A ``Missing`` value is never zero: a group with a Missing item has a
    Missing total unless ``skip_missing``, in which case the total is flagged
    ``is_lower_bound``. Periods are never summed together.
    """
    uniq: dict[tuple[str, UUID], FlowItem] = {}
    for it in items:
        uniq.setdefault((it.record_kind, it.id), it)
    items = list(uniq.values())
    if not items:
        return FlowAggregate(
            None,
            {},
            (),
            "unsupported",
            {"status": "not_applicable"},
            ("no flow items were supplied; nothing to aggregate",),
        )
    kind = _uniform(
        items,
        "record_kind",
        lambda i: i.record_kind,
        "mixed_record_kind",
        "source claims (observations) and accepted estimates cannot be aggregated together",
    )
    breakdown = _uniform(
        items,
        "breakdown",
        lambda i: i.breakdown,
        "mixed_breakdown",
        "partner-world totals and bilateral breakdowns cannot be added together (the world total already "
        "includes the bilateral flows)",
    )
    concept = _uniform(
        items,
        "concept",
        lambda i: f"{i.concept_id}@{i.concept_version}",
        "mixed_concept",
        "flows of different concepts cannot be added",
    )
    scale = items[0].scale_type
    if scale not in ADDITIVE_SCALE_TYPES:
        raise IncompatibleSemantics(f"quantity scale type {scale!r} is not additive", reason="non_additive_quantity")
    classification = _uniform(
        items,
        "classification",
        lambda i: i.classification,
        "mixed_classification",
        "mixing classification revisions is refused unless they are identical",
    )
    flow = _uniform(items, "flow", lambda i: i.flow, "mixed_flow_direction", "imports and exports cannot be added")
    present = [i for i in items if not i.is_missing]
    unit = _uniform(present, "unit_ref", lambda i: i.unit_ref, "mixed_unit", "different units cannot be added")
    currency = _uniform(
        present,
        "currency_basis",
        lambda i: (i.basis.get("currency"), i.basis.get("price_basis")),
        "mixed_currency_basis",
        "different currency or price bases cannot be added",
    )
    valuation = _uniform(
        present,
        "valuation",
        lambda i: i.basis.get("valuation"),
        "mixed_valuation",
        "mixing valuation bases (e.g. CIF and FOB) is refused unless they are identical",
    )
    seen: dict[tuple[UUID, str, UUID], UUID] = {}
    for i in items:
        k = (i.subject_id, concept, i.support_id)
        if k in seen:
            raise IncompatibleSemantics(
                "two records describe the same subject, concept and support; adding them would double count",
                reason="duplicate_record",
                ids=[str(seen[k]), str(i.id)],
            )
        seen[k] = i.id

    groups: dict[tuple[str | None, str | None, str | None], list[FlowItem]] = defaultdict(list)
    for i in items:
        groups[i.period].append(i)
    out: list[FlowGroup] = []
    for (start, end, label), members in sorted(groups.items(), key=lambda kv: (kv[0][0] or "", kv[0][1] or "")):
        missing = [m for m in members if m.is_missing]
        total_dec = sum((Decimal(str(m.value["value"])) for m in members if not m.is_missing), Decimal(0))
        if missing and not skip_missing:
            total: dict[str, Any] = {
                "kind": "missing",
                "reason": "unavailable",
                "note": f"{len(missing)} input value(s) are Missing; a Missing value is never treated as zero",
            }
        elif not [m for m in members if not m.is_missing]:
            total = {"kind": "missing", "reason": "unavailable", "note": "every input value is Missing"}
        else:
            total = {
                "kind": "decimal",
                "value": format(total_dec, "f"),
                "unit_ref": unit,
                "basis": dict(present[0].basis) if present else {},
            }
        out.append(
            FlowGroup(
                start,
                end,
                label,
                total,
                len(members),
                len(missing),
                bool(missing) and skip_missing and total["kind"] == "decimal",
                tuple(m.id for m in members),
            )
        )
    warnings: list[str] = []
    if any(i.partner_kind == "entity.world_aggregate" for i in items):
        warnings.append("some partners are provider aggregate areas; check they do not overlap other partners")
    if any(i.provider_derived for i in items):
        warnings.append("some inputs are provider-derived aggregates (isReported=false), not direct customs records")
    statuses = sorted({i.uncertainty_status or "unknown" for i in items})
    uncertainty = (
        {
            "status": "unquantified",
            "input_statuses": statuses,
            "note": "inputs carry no quantified uncertainty; none is invented for the sum",
        }
        if any(s in ("unknown", "unquantified") for s in statuses)
        else {
            "status": "not_propagated",
            "input_statuses": statuses,
            "note": "input uncertainty exists but is not propagated by this sum",
        }
    )
    return FlowAggregate(
        record_kind=kind,
        semantics={
            "concept": concept,
            "breakdown": breakdown,
            "scale_type": scale,
            "unit_ref": unit,
            "currency_basis": None if currency is None else {"currency": currency[0], "price_basis": currency[1]},
            "valuation": valuation,
            "classification": classification,
            "flow": flow,
        },
        groups=tuple(out),
        modality="derived_aggregate_of_reported_observations"
        if kind == "observation"
        else "derived_aggregate_of_selected_estimates",
        uncertainty=uncertainty,
        warnings=tuple(warnings),
    )


_OBS_ITEMS_SQL = """
SELECT o.id, o.subject_id, o.value, o.uncertainty, o.source_flags, o.recorded_at, c.concept_id,
       c.version AS concept_version, c.semantic_profile, sp.id AS support_id, sp.temporal_extent, sp.spatial_spec,
       sp.resolution, pt.entity_id AS partner_entity_id, pt.kind AS partner_kind,
       (CAST(:as_of AS timestamptz) IS NULL OR o.recorded_at <= :as_of) AS recorded_by_as_of,
       EXISTS (SELECT 1 FROM evidence.observation_retraction t WHERE t.observation_id = o.id
               AND (CAST(:as_of AS timestamptz) IS NULL OR t.recorded_at <= :as_of)) AS retracted,
       EXISTS (SELECT 1 FROM evidence.observation o2 WHERE o2.supersedes_id = o.id
               AND (CAST(:as_of AS timestamptz) IS NULL OR o2.recorded_at <= :as_of)) AS superseded
FROM evidence.observation o
JOIN ontology.concept_version c ON c.id = o.concept_version_id
JOIN world.support sp ON sp.id = o.support_id
LEFT JOIN LATERAL (
  SELECT p.entity_id, ec.concept_id AS kind FROM world.relation r
  JOIN world.relation_participant p ON p.relation_id = r.id
  JOIN ontology.concept_version rc ON rc.id = p.role_concept_id AND rc.concept_id = 'role.partner'
  JOIN world.entity e ON e.id = p.entity_id
  JOIN ontology.concept_version ec ON ec.id = e.kind_concept_id
  WHERE r.evidence_observation_id = o.id ORDER BY p.ordinal LIMIT 1
) pt ON true
WHERE o.id = ANY(:ids)
"""

_EST_ITEMS_SQL = """
SELECT e.id, e.subject_id, e.value, e.uncertainty, e.release_id, c.concept_id, c.version AS concept_version,
       c.semantic_profile, sp.id AS support_id, sp.temporal_extent, sp.spatial_spec, sp.resolution,
       (SELECT o.source_flags FROM belief.input i JOIN evidence.observation o ON o.id = i.observation_id
        WHERE i.estimate_id = e.id ORDER BY o.id LIMIT 1) AS source_flags,
       pub.published_at,
       (CAST(:as_of AS timestamptz) IS NULL OR (pub.published_at IS NOT NULL AND pub.published_at <= :as_of))
         AS recorded_by_as_of
FROM belief.estimate e
JOIN ontology.concept_version c ON c.id = e.concept_version_id
JOIN world.support sp ON sp.id = e.support_id
LEFT JOIN ops.publication pub ON pub.release_id = e.release_id
WHERE e.id = ANY(:ids)
"""


def _item(r: dict[str, Any], kind: Literal["observation", "estimate"]) -> FlowItem:
    flags = r.get("source_flags") or {}
    profile = r.get("semantic_profile") or {}
    value = dict(r["value"])
    return FlowItem(
        id=UUID(str(r["id"])),
        record_kind=kind,
        subject_id=UUID(str(r["subject_id"])),
        concept_id=r["concept_id"],
        concept_version=r["concept_version"],
        scale_type=profile.get("scale_type"),
        breakdown=_breakdown(r["spatial_spec"], r["resolution"]),
        support_id=UUID(str(r["support_id"])),
        temporal_extent=dict(r["temporal_extent"] or {}),
        value=value,
        classification=flags.get("classificationCode"),
        flow=flags.get("flowCode"),
        partner_entity_id=None if r.get("partner_entity_id") is None else UUID(str(r["partner_entity_id"])),
        partner_kind=r.get("partner_kind"),
        uncertainty_status=(r.get("uncertainty") or {}).get("status"),
        provider_derived=bool(flags.get("provider_derived_aggregate")),
    )


def load_flow_items(
    conn: Connection,
    *,
    observation_ids: Sequence[UUID] = (),
    estimate_ids: Sequence[UUID] = (),
    as_of: datetime | None = None,
) -> tuple[list[FlowItem], list[dict[str, Any]]]:
    """Load flow items. Returns ``(items, excluded)``; records not visible at
    ``as_of`` (not yet recorded/published, retracted, or superseded by a later
    revision) are excluded with a reason."""
    require_aware(as_of, "as_of")
    if len(observation_ids) + len(estimate_ids) > MAX_SUBGRAPH_EDGES:
        raise ValidationProblem(f"at most {MAX_SUBGRAPH_EDGES} records per aggregation")
    items: list[FlowItem] = []
    excluded: list[dict[str, Any]] = []
    found: set[UUID] = set()
    if observation_ids:
        for r in conn.execute(text(_OBS_ITEMS_SQL), {"ids": list(observation_ids), "as_of": as_of}).mappings():
            d = dict(r)
            found.add(UUID(str(d["id"])))
            if not d["recorded_by_as_of"]:
                excluded.append({"id": str(d["id"]), "reason": "recorded_after_as_of"})
            elif d["retracted"]:
                excluded.append({"id": str(d["id"]), "reason": "retracted_by_as_of"})
            elif d["superseded"]:
                excluded.append({"id": str(d["id"]), "reason": "superseded_by_as_of"})
            else:
                items.append(_item(d, "observation"))
    if estimate_ids:
        for r in conn.execute(text(_EST_ITEMS_SQL), {"ids": list(estimate_ids), "as_of": as_of}).mappings():
            d = dict(r)
            found.add(UUID(str(d["id"])))
            if d["published_at"] is None:
                excluded.append({"id": str(d["id"]), "reason": "release_not_published"})
            elif not d["recorded_by_as_of"]:
                excluded.append({"id": str(d["id"]), "reason": "published_after_as_of"})
            else:
                items.append(_item(d, "estimate"))
    for i in (*observation_ids, *estimate_ids):
        if i not in found:
            excluded.append({"id": str(i), "reason": "not_found"})
    return items, excluded


def aggregate_flows(
    conn: Connection,
    *,
    observation_ids: Sequence[UUID] = (),
    estimate_ids: Sequence[UUID] = (),
    as_of: datetime | None = None,
    skip_missing: bool = False,
) -> FlowAggregate:
    """Load and aggregate flow records (see :func:`aggregate_flow_items` for the refusal rules).

    To aggregate the flows of a neighbourhood, pass
    ``[e.provenance.observation_id for e in nb.edges]``.
    """
    items, excluded = load_flow_items(conn, observation_ids=observation_ids, estimate_ids=estimate_ids, as_of=as_of)
    agg = aggregate_flow_items(items, skip_missing=skip_missing)
    if not excluded:
        return agg
    return FlowAggregate(
        agg.record_kind,
        agg.semantics,
        agg.groups,
        agg.modality,
        agg.uncertainty,
        (*agg.warnings, f"{len(excluded)} requested record(s) were excluded"),
        tuple(excluded),
    )


# ---------------------------------------------------------------------------
# Source claim vs accepted estimate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimComparison:
    subject_id: UUID
    concept_id: str
    support_id: UUID
    as_of: datetime | None
    release: dict[str, Any] | None
    claims: tuple[dict[str, Any], ...]
    estimates: tuple[dict[str, Any], ...]
    comparisons: tuple[dict[str, Any], ...]
    notes: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject_id": str(self.subject_id),
            "concept_id": self.concept_id,
            "support_id": str(self.support_id),
            "as_of": None if self.as_of is None else self.as_of.isoformat(),
            "release": self.release,
            "claims": list(self.claims),
            "estimates": list(self.estimates),
            "comparisons": list(self.comparisons),
            "notes": list(self.notes),
        }


_CLAIMS_SQL = """
SELECT o.id, src.source_key, o.logical_key, o.revision_no, o.supersedes_id, o.value, o.uncertainty, o.source_flags,
       o.recorded_at, o.source_published_at, o.source_available_at, a.origin, c.version AS concept_version,
       NOT EXISTS (SELECT 1 FROM evidence.observation o2 WHERE o2.supersedes_id = o.id
                   AND (CAST(:as_of AS timestamptz) IS NULL OR o2.recorded_at <= :as_of)) AS is_latest,
       EXISTS (SELECT 1 FROM evidence.observation_retraction t WHERE t.observation_id = o.id
               AND (CAST(:as_of AS timestamptz) IS NULL OR t.recorded_at <= :as_of)) AS retracted
FROM evidence.observation o
JOIN evidence.source src ON src.id = o.source_id
JOIN evidence.acquisition a ON a.id = o.acquisition_id
JOIN ontology.concept_version c ON c.id = o.concept_version_id
WHERE o.subject_id = :subject AND c.concept_id = :concept AND o.support_id = :support
  AND (CAST(:as_of AS timestamptz) IS NULL OR o.recorded_at <= :as_of)
ORDER BY src.source_key, o.logical_key, o.revision_no
"""

_ESTIMATES_SQL = """
SELECT e.id, e.modality, e.value, e.uncertainty, e.alternatives, e.discrepancy_flags, c.version AS concept_version,
       mv.model_key, mv.version AS method_version,
       COALESCE((SELECT array_agg(i.observation_id ORDER BY i.observation_id) FROM belief.input i
                 WHERE i.estimate_id = e.id AND i.observation_id IS NOT NULL), ARRAY[]::uuid[]) AS input_observation_ids
FROM belief.estimate e
JOIN ontology.concept_version c ON c.id = e.concept_version_id
JOIN model.version mv ON mv.id = e.method_version_id
WHERE e.release_id = :release AND e.subject_id = :subject AND c.concept_id = :concept AND e.support_id = :support
ORDER BY e.modality, e.id
"""


def _dec(v: dict[str, Any]) -> Decimal | None:
    if not isinstance(v, dict) or v.get("kind") != "decimal":
        return None
    try:
        return Decimal(str(v["value"]))
    except (InvalidOperation, KeyError):
        return None


def compare_claims(
    conn: Connection,
    subject_id: UUID,
    concept_id: str,
    support_id: UUID,
    *,
    release_id: UUID | None = None,
    as_of: datetime | None = None,
    origin: str | None = None,
) -> ClaimComparison:
    """Compare source claims with the accepted estimate for one (subject, concept, support).

    Claims are every evidence revision recorded at/before ``as_of`` (flagged
    ``is_latest``/``retracted`` as of then). The estimate comes from
    ``release_id`` or, if omitted, the release published at ``as_of``
    (optionally restricted to ``origin``). A ``selected_report`` estimate is a
    source value chosen by a named method, not a calibrated posterior.
    Differences are computed only for decimal values with identical units.
    """
    require_aware(as_of, "as_of")
    notes: list[str] = []
    params = {"subject": subject_id, "concept": concept_id, "support": support_id, "as_of": as_of}
    claims = []
    for r in conn.execute(text(_CLAIMS_SQL), params).mappings():
        d = dict(r)
        claims.append(
            {
                "observation_id": str(d["id"]),
                "source_key": d["source_key"],
                "logical_key": d["logical_key"],
                "revision_no": d["revision_no"],
                "supersedes_id": None if d["supersedes_id"] is None else str(d["supersedes_id"]),
                "value": d["value"],
                "uncertainty": d["uncertainty"],
                "source_flags": d["source_flags"],
                "recorded_at": d["recorded_at"].isoformat(),
                "source_published_at": None
                if d["source_published_at"] is None
                else d["source_published_at"].isoformat(),
                "origin": d["origin"],
                "concept_version": d["concept_version"],
                "is_latest": bool(d["is_latest"]),
                "retracted": bool(d["retracted"]),
                "modality": "reported_observation",
            }
        )
    if not claims:
        notes.append(
            "no source claim recorded for this subject/concept/support" + ("" if as_of is None else " at/before as_of")
        )
    release: dict[str, Any] | None
    if release_id is not None:
        row = (
            conn.execute(
                text("""SELECT r.id, r.knowledge_cutoff, r.origin, r.method, p.sequence, p.published_at
                    FROM belief.release r LEFT JOIN ops.publication p ON p.release_id = r.id WHERE r.id = :i"""),
                {"i": release_id},
            )
            .mappings()
            .one_or_none()
        )
        release = None if row is None else dict(row)
        if release is not None and release["published_at"] is None:
            notes.append("the requested release is not published; its estimates are not accepted beliefs")
            release = None
        elif release is not None and as_of is not None and release["published_at"] > as_of:
            notes.append("the requested release was published after as_of")
    else:
        release = release_as_of(conn, as_of, origin)
    estimates: list[dict[str, Any]] = []
    if release is None:
        notes.append("no published belief release is available for this query")
    else:
        for r in conn.execute(text(_ESTIMATES_SQL), {**params, "release": release["id"]}).mappings():
            d = dict(r)
            estimates.append(
                {
                    "estimate_id": str(d["id"]),
                    "modality": d["modality"],
                    "value": d["value"],
                    "uncertainty": d["uncertainty"],
                    "alternatives": d["alternatives"],
                    "discrepancy_flags": d["discrepancy_flags"],
                    "method": f"{d['model_key']}@{d['method_version']}",
                    "concept_version": d["concept_version"],
                    "input_observation_ids": [str(x) for x in d["input_observation_ids"]],
                }
            )
        if not estimates:
            notes.append("the release holds no accepted estimate for this subject/concept/support")
        if any(e["modality"] == "selected_report" for e in estimates):
            notes.append("selected_report: a source value selected by a named method, not a calibrated posterior")
    comparisons = []
    for c in claims:
        for e in estimates:
            cv, ev = _dec(c["value"]), _dec(e["value"])
            entry: dict[str, Any] = {
                "observation_id": c["observation_id"],
                "estimate_id": e["estimate_id"],
                "claim_is_input": c["observation_id"] in e["input_observation_ids"],
            }
            if cv is None or ev is None:
                entry.update({"comparable": False, "reason": "missing_value"})
            elif c["value"].get("unit_ref") != e["value"].get("unit_ref"):
                entry.update({"comparable": False, "reason": "unit_mismatch"})
            else:
                diff = cv - ev
                entry.update(
                    {
                        "comparable": True,
                        "difference": format(diff, "f"),
                        "relative_difference": None if ev == 0 else format(diff / abs(ev), ".6E"),
                        "unit_ref": c["value"].get("unit_ref"),
                        "identical": diff == 0,
                    }
                )
            comparisons.append(entry)
    rel_out = None
    if release is not None:
        rel_out = {
            k: (v.isoformat() if isinstance(v, datetime) else str(v) if isinstance(v, UUID) else v)
            for k, v in release.items()
            if k in ("id", "knowledge_cutoff", "origin", "method", "sequence", "published_at")
        }
    return ClaimComparison(
        subject_id,
        concept_id,
        support_id,
        as_of,
        rel_out,
        tuple(claims),
        tuple(estimates),
        tuple(comparisons),
        tuple(notes),
    )
