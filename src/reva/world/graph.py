"""Temporal multilayer incidence (hypergraph) core types and bounded projections.

A *hyperedge* (``world.relation``) has an id, a predicate concept, a layer, a
support (when/where/at what scale it applies), a context (a pinned belief
release, or one evidence observation) and an ordered list of role-bound
participants. Repeated participants in distinct roles are allowed. The native
representation is the incidence structure (entity x hyperedge); every pairwise
graph is a *derived projection*.

Four quantities are deliberately kept in separate fields and never merged:

* ``existence_uncertainty`` -- uncertainty that the relation exists at all;
* ``flow_magnitude``        -- the size of a reported flow (a typed value,
  ``Missing`` is never zero);
* ``tie_strength``          -- an assessed strength of the tie, if any;
* ``scientific_confidence`` -- confidence from scientific evidence, if any.

Bounds (architecture section 14): path search defaults to depth 2 (maximum 4)
and at most 500 returned edges; NetworkX is used only for small extracted
subgraphs (<= 10,000 vertices and <= 50,000 edges) and raises
``CapabilityGap`` above that; larger projections use ``scipy.sparse`` CSR.

This module holds no SQL. Database queries live in :mod:`reva.world.queries`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import numpy as np
from scipy import sparse

from reva.kernel.errors import CapabilityGap, ValidationProblem

DEFAULT_DEPTH = 2
MAX_DEPTH = 4
DEFAULT_MAX_EDGES = 500
MAX_EDGES = 500
DEFAULT_TIMEOUT_MS = 5_000
MAX_TIMEOUT_MS = 30_000
NX_MAX_VERTICES = 10_000
NX_MAX_EDGES = 50_000
MAX_SUBGRAPH_EDGES = 50_000

DERIVED_PROJECTION_WARNING = (
    "derived_projection: a hyperedge expanded into a clique of pairwise ties is a derived projection, not an "
    "observed set of dyadic relations. It discards roles, direction and edge arity and may distort contagion, "
    "diffusion or other dynamics. Clique weights count shared hyperedges; they are neither tie strength nor "
    "flow magnitude."
)
NOT_ASSESSED_STRENGTH: dict[str, Any] = {
    "status": "not_assessed",
    "note": "no tie strength has been assessed for this relation",
}
NOT_ASSESSED_CONFIDENCE: dict[str, Any] = {
    "status": "not_assessed",
    "note": "no scientific evidence card assesses this relation; a reported flow is descriptive, not causal",
}


def require_aware(ts: datetime | None, name: str) -> datetime | None:
    """Reject naive timestamps: every as-of/interval instant must be timezone-aware."""
    if ts is not None and (ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None):
        raise ValidationProblem(f"{name} must be a timezone-aware instant", field=name)
    return ts


def check_interval(valid_during: tuple[datetime, datetime] | None) -> tuple[datetime, datetime] | None:
    """Validate a half-open ``[start, end)`` interval of aware instants."""
    if valid_during is None:
        return None
    start, end = valid_during
    require_aware(start, "valid_during.start")
    require_aware(end, "valid_during.end")
    if end <= start:
        raise ValidationProblem("valid_during must satisfy start < end (half-open interval)")
    return start, end


def check_bounds(max_depth: int, max_edges: int, timeout_ms: int) -> None:
    if not 1 <= max_depth <= MAX_DEPTH:
        raise ValidationProblem(f"max_depth must be within 1..{MAX_DEPTH}", max_depth=max_depth)
    if not 1 <= max_edges <= MAX_EDGES:
        raise ValidationProblem(f"max_edges must be within 1..{MAX_EDGES}", max_edges=max_edges)
    if not 1 <= timeout_ms <= MAX_TIMEOUT_MS:
        raise ValidationProblem(f"timeout_ms must be within 1..{MAX_TIMEOUT_MS}", timeout_ms=timeout_ms)


# ---------------------------------------------------------------------------
# Core records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Participant:
    """A role-bound participant of a hyperedge; ``ordinal`` preserves order."""

    ordinal: int
    role: str
    entity_id: UUID
    entity_label: str
    entity_kind: str
    direction: Literal["in", "out", "undirected"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "role": self.role,
            "entity_id": str(self.entity_id),
            "entity_label": self.entity_label,
            "entity_kind": self.entity_kind,
            "direction": self.direction,
        }


@dataclass(frozen=True)
class EdgeProvenance:
    """Where a hyperedge comes from: a pinned belief release or one evidence observation."""

    kind: Literal["release", "evidence"]
    release_id: UUID | None
    observation_id: UUID | None
    origin: str | None
    source_key: str | None
    revision_no: int | None
    observation_recorded_at: datetime | None
    provenance_activity_id: UUID

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "release_id": _s(self.release_id),
            "observation_id": _s(self.observation_id),
            "origin": self.origin,
            "source_key": self.source_key,
            "revision_no": self.revision_no,
            "observation_recorded_at": _iso(self.observation_recorded_at),
            "provenance_activity_id": str(self.provenance_activity_id),
        }


@dataclass(frozen=True)
class Hyperedge:
    """One n-ary relation with ordered participants and four distinct epistemic fields."""

    id: UUID
    predicate: str
    predicate_version: str
    layer: str
    support: dict[str, Any]
    provenance: EdgeProvenance
    component: dict[str, Any]
    existence_uncertainty: dict[str, Any]
    flow_magnitude: dict[str, Any]
    tie_strength: dict[str, Any]
    scientific_confidence: dict[str, Any]
    recorded_at: datetime
    participants: tuple[Participant, ...]

    def entity_ids(self) -> list[UUID]:
        """Distinct participating entities in ordinal order."""
        seen: dict[UUID, None] = {}
        for p in self.participants:
            seen.setdefault(p.entity_id, None)
        return list(seen)

    def with_role(self, role: str) -> list[Participant]:
        return [p for p in self.participants if p.role == role]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "predicate": self.predicate,
            "predicate_version": self.predicate_version,
            "layer": self.layer,
            "support": self.support,
            "provenance": self.provenance.as_dict(),
            "component": self.component,
            "existence_uncertainty": self.existence_uncertainty,
            "flow_magnitude": self.flow_magnitude,
            "tie_strength": self.tie_strength,
            "scientific_confidence": self.scientific_confidence,
            "recorded_at": _iso(self.recorded_at),
            "participants": [p.as_dict() for p in self.participants],
        }


# ---------------------------------------------------------------------------
# Projections
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IncidenceProjection:
    """Faithful sparse incidence matrix: rows are entities, columns hyperedges.

    ``matrix[i, j]`` counts the participations of entity ``i`` in hyperedge
    ``j`` (an entity filling two roles counts twice). Roles per non-zero are in
    ``roles[(i, j)]``. This is not a derived projection.
    """

    entity_ids: tuple[UUID, ...]
    edge_ids: tuple[UUID, ...]
    matrix: sparse.csr_matrix
    roles: dict[tuple[int, int], tuple[str, ...]]
    derived_projection: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class CliqueProjection:
    """Pairwise co-membership adjacency derived from hyperedges (always flagged derived)."""

    entity_ids: tuple[UUID, ...]
    matrix: sparse.csr_matrix
    source_edge_ids: tuple[UUID, ...]
    derived_projection: bool = True
    warnings: tuple[str, ...] = (DERIVED_PROJECTION_WARNING,)
    weight_meaning: str = "count of shared hyperedges (not tie strength, not flow magnitude)"

    def as_dict(self) -> dict[str, Any]:
        coo = self.matrix.tocoo()
        pairs = [
            {"a": str(self.entity_ids[i]), "b": str(self.entity_ids[j]), "shared_hyperedges": int(w)}
            for i, j, w in zip(coo.row, coo.col, coo.data, strict=True)
            if i < j
        ]
        return {
            "derived_projection": True,
            "warnings": list(self.warnings),
            "weight_meaning": self.weight_meaning,
            "entity_ids": [str(e) for e in self.entity_ids],
            "pairs": pairs,
            "source_edge_ids": [str(e) for e in self.source_edge_ids],
        }


def incidence_matrix(edges: Sequence[Hyperedge]) -> IncidenceProjection:
    """Build the entity x hyperedge CSR incidence matrix (any size; no NetworkX)."""
    index: dict[UUID, int] = {}
    rows: list[int] = []
    cols: list[int] = []
    roles: dict[tuple[int, int], list[str]] = {}
    for j, e in enumerate(edges):
        for p in e.participants:
            i = index.setdefault(p.entity_id, len(index))
            rows.append(i)
            cols.append(j)
            roles.setdefault((i, j), []).append(p.role)
    data = np.ones(len(rows), dtype=np.int32)
    m = sparse.csr_matrix((data, (rows, cols)), shape=(len(index), len(edges)), dtype=np.int32)
    m.sum_duplicates()
    return IncidenceProjection(
        entity_ids=tuple(index),
        edge_ids=tuple(e.id for e in edges),
        matrix=m,
        roles={k: tuple(v) for k, v in roles.items()},
    )


def clique_projection(edges: Sequence[Hyperedge]) -> CliqueProjection:
    """Expand hyperedges into a symmetric entity x entity co-membership CSR matrix.

    Computed as ``B B^T`` on the binary incidence matrix with the diagonal
    removed. The result is flagged ``derived_projection=True`` with a warning:
    it may distort dynamics on the original hypergraph.
    """
    inc = incidence_matrix(edges)
    binary = inc.matrix.copy()
    binary.data = np.ones_like(binary.data)
    adj = (binary @ binary.T).tocsr()
    adj.setdiag(0)
    adj.eliminate_zeros()
    return CliqueProjection(entity_ids=inc.entity_ids, matrix=adj, source_edge_ids=inc.edge_ids)


def clique_pair_count(edges: Iterable[Hyperedge]) -> int:
    """Upper bound on pairwise ties a clique expansion would create (before de-duplication)."""
    total = 0
    for e in edges:
        k = len(e.entity_ids())
        total += k * (k - 1) // 2
    return total


def check_networkx_caps(
    vertices: int, edges: int, *, max_vertices: int = NX_MAX_VERTICES, max_edges: int = NX_MAX_EDGES
) -> None:
    """Raise ``CapabilityGap`` when a graph is too large for NetworkX (use CSR projections instead)."""
    max_vertices = min(max_vertices, NX_MAX_VERTICES)
    max_edges = min(max_edges, NX_MAX_EDGES)
    if vertices > max_vertices or edges > max_edges:
        raise CapabilityGap(
            "subgraph exceeds the NetworkX cap; use incidence_matrix()/clique_projection() (scipy.sparse CSR)",
            vertices=vertices,
            edges=edges,
            max_vertices=max_vertices,
            max_edges=max_edges,
        )


def to_networkx(
    edges: Sequence[Hyperedge],
    *,
    mode: Literal["incidence", "clique"] = "incidence",
    max_vertices: int = NX_MAX_VERTICES,
    max_edges: int = NX_MAX_EDGES,
) -> Any:
    """Materialise a *small* extracted subgraph as a NetworkX graph.

    ``mode="incidence"`` (default) returns the bipartite incidence graph:
    a ``nx.Graph`` with entity nodes (``bipartite=0``) and
    hyperedge nodes (``bipartite=1``), one link per participation carrying
    ``role``/``ordinal``/``direction``. It is faithful to the hypergraph.

    ``mode="clique"`` returns a weighted ``nx.Graph`` of pairwise co-membership.
    It is a derived projection: ``G.graph["derived_projection"] is True`` and
    ``G.graph["warnings"]`` carries the distortion warning.

    Caps are checked *before* NetworkX is imported or any object is built.
    """
    entity_ids = {p.entity_id for e in edges for p in e.participants}
    if mode == "incidence":
        n_vertices = len(entity_ids) + len(edges)
        n_links = sum(len(e.participants) for e in edges)
    elif mode == "clique":
        n_vertices = len(entity_ids)
        n_links = clique_pair_count(edges)
    else:
        raise ValidationProblem("mode must be 'incidence' or 'clique'", mode=mode)
    check_networkx_caps(n_vertices, n_links, max_vertices=max_vertices, max_edges=max_edges)
    try:
        import networkx as nx  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise CapabilityGap("networkx is not installed in this environment", dependency="networkx") from exc

    labels = {p.entity_id: (p.entity_label, p.entity_kind) for e in edges for p in e.participants}
    if mode == "incidence":
        g = nx.Graph(derived_projection=False, representation="bipartite_incidence", warnings=[])
        for eid, (label, kind) in labels.items():
            g.add_node(("entity", eid), bipartite=0, label=label, kind=kind)
        for e in edges:
            g.add_node(("hyperedge", e.id), bipartite=1, predicate=e.predicate, layer=e.layer)
            for p in e.participants:
                # An entity in two roles of one hyperedge keeps both roles on the single link.
                key = (("entity", p.entity_id), ("hyperedge", e.id))
                if g.has_edge(*key):
                    g.edges[key]["roles"].append(p.role)
                    g.edges[key]["ordinals"].append(p.ordinal)
                else:
                    g.add_edge(*key, roles=[p.role], ordinals=[p.ordinal], direction=p.direction)
        return g
    proj = clique_projection(edges)
    g = nx.Graph(
        derived_projection=True,
        representation="clique_projection",
        warnings=list(proj.warnings),
        weight_meaning=proj.weight_meaning,
    )
    for eid in proj.entity_ids:
        label, kind = labels[eid]
        g.add_node(eid, label=label, kind=kind)
    coo = proj.matrix.tocoo()
    for i, j, w in zip(coo.row, coo.col, coo.data, strict=True):
        if i < j:
            g.add_edge(proj.entity_ids[i], proj.entity_ids[j], shared_hyperedges=int(w))
    return g


@dataclass(frozen=True)
class EdgeSet:
    """A bounded set of hyperedges with an explicit truncation flag."""

    edges: tuple[Hyperedge, ...]
    truncated: bool
    max_edges: int
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "edges": [e.as_dict() for e in self.edges],
            "truncated": self.truncated,
            "max_edges": self.max_edges,
            "notes": list(self.notes),
        }


def _s(v: UUID | None) -> str | None:
    return None if v is None else str(v)


def _iso(v: datetime | None) -> str | None:
    return None if v is None else v.isoformat()
