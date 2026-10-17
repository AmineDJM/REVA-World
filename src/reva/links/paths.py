"""Path engine: *how* are two things connected, hop by hop, each hop citing its evidence.

Graph
-----
A combined, in-memory multigraph over node ids (QIDs, provider-scoped keys):

* **statement edges** — one KG statement whose property is listed in ``statement_edges`` of
  ``registry/links/link_types.yaml`` (``Brazil —official language→ Portuguese``), traversable both ways
  (the reverse direction uses the inverse label: ``Portuguese —official language of→ Brazil``);
* **derived links** — :class:`~reva.links.model.Link` records from :mod:`reva.links.derive`
  (``Brazil —former colony of→ Kingdom of Portugal``).

The graph is built from Python lists (tests, offline fixture) or from ``kg.statement_current`` through
:class:`KgNeighbourhood` (bounded frontier expansion; see :func:`graph_from_db`).

Search
------
1. bounded BFS from the target (hop distances ≤ ``max_hops``), then from the source restricted to nodes
   that can still reach the target in the remaining hops (bidirectional pruning);
2. weighted k-shortest *simple* paths (Yen, via ``networkx.shortest_simple_paths``) on the pruned subgraph.
   Edge weight = the edge kind's ``path_cost`` + a penalty for the node entered: declared hub penalty
   (``registry/links/hubs.yaml``) + degree penalty (per doubling of the degree above a threshold). Blocked
   hubs ("human", "country", "Earth"...) are never intermediate nodes unless ``include_blocked_hubs``.
3. filters: ``as_of`` year or ``window`` (years), link kinds, ``max_hops`` ≤ 6, ``include_undated``.

Bounds (review V3, SEC-1 / LINKS-1): rows read per neighbourhood (``MAX_ROWS_PER_NEIGHBOURHOOD``), Yen
candidates (``MAX_YEN_CANDIDATES``) and an optional wall-clock ``deadline`` (``time.monotonic``) checked
between hops, segments and candidates. Any cut — including a neighbourhood truncated when read from the
database (``PathGraph.truncated_nodes``) — sets ``truncated`` with its reasons: a "no path" over a cut graph
never reads as a definitive absence (invariant 17).

Temporal filter semantics (per edge):
* interval [start, end] (open bounds = not stated): kept if it contains ``as_of`` / overlaps ``window``;
* point (a dated event): kept if it happened by ``as_of`` (≤) / within ``window``;
* snapshot (atlas reference year): kept if the year equals ``as_of`` / lies within ``window``;
* undated: kept only if ``include_undated`` (default true) and flagged "undated in source" in the text.

Honesty
-------
A path is a chain of "connected via <label>" hops, never an explanation of causes. A hop is worded
causally only when its statement's property is causal (P828 has cause, P1542 has effect, and their
immediate-cause / contributing-factor refinements) and then always as "selon <source>, X a pour cause Y"
in the statement's own direction, whatever the traversal direction.
"""

from __future__ import annotations

import itertools
import math
import re
import time
from collections import Counter, defaultdict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import networkx as nx

from reva.links.model import Evidence, Link, LinkError, NoEvidence, Stmt, Temporal, is_wikidata
from reva.links.taxonomy import Hubs, Taxonomy, load_hubs, load_taxonomy

MAX_HOPS_LIMIT = 8
#: DFS steps of one constrained search (ordered hop types, via_any / via_all, non-transit nodes)
MAX_CONSTRAINED_STEPS = 2_000_000
MAX_STEPS = 8
MAX_K = 10
DEFAULT_MAX_EXPANSIONS = 200_000
MAX_YEN_CANDIDATES = 200
#: Statement rows read from the database for one neighbourhood (all hops of one side together).
MAX_ROWS_PER_NEIGHBOURHOOD = 100_000
#: Default wall-clock budget of one /v1/links/path request (all segments).
TIME_BUDGET_S = 20.0
LANGS = ("fr", "en")
NS_PID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}:[a-z][a-z0-9_]{0,63}$")

DISCLAIMER = {
    "fr": "Chemin de connexions (« relié via »), pas une explication causale. Chaque étape cite sa source.",
    "en": "A path of connections (“connected via”), not a causal explanation. Every hop cites its source.",
}
UNDATED = {"fr": "non daté dans la source", "en": "undated in source"}
SOURCE_NAMES = {
    "wikidata": {"fr": "Wikidata (contribution communautaire)", "en": "Wikidata (community-edited)"},
}


class PathQueryError(ValueError):
    pass


# ---------------------------------------------------------------------------------------------- edges
@dataclass(frozen=True)
class Edge:
    """One traversable direction of a statement or link."""

    u: str
    v: str
    kind: str  # "statement" | "link"
    type_id: str  # property id or link type id
    forward: bool  # traversal follows the record's subject -> object direction
    temporal: Temporal
    cost: float
    causal: bool = False
    stmt: Stmt | None = None
    link: Link | None = None
    #: V4 relation family of this traversal direction (statement edges whose property is mapped), else None
    family: str | None = None
    #: family of the opposite traversal (v -> u), used when a search walks this edge backwards from the target
    rev_family: str | None = None

    @property
    def kind_key(self) -> str:
        return f"{self.kind}:{self.type_id}"

    @property
    def evidence(self) -> tuple[Evidence, ...]:
        if self.stmt is not None:
            return (Evidence.of_stmt(self.stmt),)
        assert self.link is not None
        return self.link.evidence


@dataclass(frozen=True)
class PathQuery:
    source: str
    target: str
    k: int = 3
    max_hops: int = 4
    as_of: int | None = None
    window: tuple[int, int] | None = None
    kinds: frozenset[str] | None = None  # e.g. {"statement:P37", "link:former_colony_of", "P1891"}
    exclude_kinds: frozenset[str] = frozenset()
    include_undated: bool = True
    include_blocked_hubs: bool = False
    max_expansions: int = DEFAULT_MAX_EXPANSIONS
    #: ordered hop-type constraints: each step is a set of kinds (``P945``, ``link:co_member``); the path must
    #: match the steps in order, each by a distinct hop (other hops may come between them)
    steps: tuple[frozenset[str], ...] = ()
    #: the path must pass through at least one of these nodes / through all of them (any order)
    via_any: frozenset[str] = frozenset()
    via_all: frozenset[str] = frozenset()
    #: objects of these properties (languages, continents, eras) may end a path but are never passed through
    non_transit_props: frozenset[str] = frozenset()
    max_constrained_steps: int = MAX_CONSTRAINED_STEPS
    #: V4: only statement hops whose family reading (in traversal direction) is one of these
    families: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.max_hops <= MAX_HOPS_LIMIT:
            raise PathQueryError(f"max_hops must be within 1..{MAX_HOPS_LIMIT}")
        if not 1 <= self.k <= MAX_K:
            raise PathQueryError(f"k must be within 1..{MAX_K}")
        if self.as_of is not None and self.window is not None:
            raise PathQueryError("give either as_of or window, not both")
        if self.window is not None and self.window[0] > self.window[1]:
            raise PathQueryError("window must be (from_year, to_year) with from <= to")
        if self.source == self.target:
            raise PathQueryError("source and target are the same node")
        if len(self.steps) > MAX_STEPS or len(self.steps) > self.max_hops:
            raise PathQueryError(f"at most {MAX_STEPS} ordered steps, and no more than max_hops")
        if not 1 <= self.max_constrained_steps <= MAX_CONSTRAINED_STEPS:
            raise PathQueryError(f"max_constrained_steps must be within 1..{MAX_CONSTRAINED_STEPS}")

    @property
    def constrained(self) -> bool:
        return bool(self.steps or self.via_any or self.via_all)

    @property
    def step_sets(self) -> tuple[set[str], ...]:
        return tuple(self._norm(st) for st in self.steps)

    def _norm(self, kinds: Iterable[str]) -> set[str]:
        out = set()
        for k in kinds:
            if ":" in k and k.split(":", 1)[0] in ("statement", "link"):
                out.add(k)
            elif (k.startswith("P") and k[1:].isdigit()) or NS_PID_RE.match(k):
                out.add(
                    f"statement:{k}"
                )  # Wikidata property or namespaced dataset predicate (geo:covered_territory_of)
            else:
                out.add(f"link:{k}")
        return out

    @property
    def kind_set(self) -> set[str] | None:
        return None if self.kinds is None else self._norm(self.kinds)

    @property
    def exclude_set(self) -> set[str]:
        return self._norm(self.exclude_kinds)

    def to_json(self) -> dict[str, Any]:
        return {
            "from": self.source,
            "to": self.target,
            "k": self.k,
            "max_hops": self.max_hops,
            "as_of": self.as_of,
            "window": list(self.window) if self.window else None,
            "kinds": sorted(self.kind_set) if self.kinds is not None else None,
            "exclude_kinds": sorted(self.exclude_set),
            "include_undated": self.include_undated,
            "include_blocked_hubs": self.include_blocked_hubs,
            "steps": [sorted(x) for x in self.step_sets],
            "via_any": sorted(self.via_any),
            "via_all": sorted(self.via_all),
            "non_transit_props": sorted(self.non_transit_props),
            "families": sorted(self.families) if self.families is not None else None,
        }


def temporal_allows(t: Temporal, q: PathQuery) -> bool:
    if q.as_of is None and q.window is None:
        return True
    if t.kind == "undated":
        return q.include_undated
    lo, hi = (q.as_of, q.as_of) if q.as_of is not None else q.window  # type: ignore[misc]
    if t.kind == "snapshot":
        assert t.year is not None
        return lo <= t.year <= hi
    if t.kind == "point":
        assert t.point is not None
        return t.point.year <= hi if q.as_of is not None else lo <= t.point.year <= hi
    start = t.start.year if t.start else None
    end = t.end.year if t.end else None
    return (start is None or start <= hi) and (end is None or end >= lo)


# ---------------------------------------------------------------------------------------------- graph
class PathGraph:
    """In-memory multigraph of statement edges and derived links."""

    def __init__(self, taxonomy: Taxonomy | None = None, hubs: Hubs | None = None) -> None:
        self.taxonomy = taxonomy or load_taxonomy()
        self.hubs = hubs or load_hubs()
        self.adj: dict[str, list[Edge]] = defaultdict(list)
        self.labels: dict[str, dict[str, str]] = {}
        self.skipped: Counter[str] = Counter()
        self._degree: dict[str, int] | None = None
        self._seen: set[tuple[str, ...]] = set()
        #: nodes whose neighbourhood was truncated when read from the database (degree is a lower bound)
        self.truncated_nodes: set[str] = set()
        self._non_transit: tuple[frozenset[str], set[str]] | None = None

    # -- building -------------------------------------------------------------------------------------
    def add_statement(self, s: Stmt) -> bool:
        if s.object is None:
            self.skipped["literal_value"] += 1
            return False
        et = self.taxonomy.edge_types.get(s.pid)
        if et is None:
            self.skipped["property_not_traversable"] += 1
            return False
        if s.subject == s.object:
            self.skipped["self_loop"] += 1
            return False
        key = ("s", s.statement_id)
        if key in self._seen:
            return False
        self._seen.add(key)
        t = s.temporal
        fwd_fam, back_fam = self._families(s.pid) if et.family else (None, None)
        self.adj[s.subject].append(
            Edge(
                s.subject,
                s.object,
                "statement",
                s.pid,
                True,
                t,
                et.path_cost,
                et.causal,
                stmt=s,
                family=fwd_fam,
                rev_family=back_fam,
            )
        )
        self.adj[s.object].append(
            Edge(
                s.object,
                s.subject,
                "statement",
                s.pid,
                False,
                t,
                et.path_cost,
                et.causal,
                stmt=s,
                family=back_fam,
                rev_family=fwd_fam,
            )
        )
        self._degree = None
        return True

    def _families(self, pid: str) -> tuple[str | None, str | None]:
        from reva.kg.families import FamilyError, load_registry

        try:
            reg = load_registry()
        except FamilyError:  # pragma: no cover - the taxonomy already degraded without families
            return None, None
        f, b = reg.edge_family(pid, "out"), reg.edge_family(pid, "in")
        return (f.family if f else None), (b.family if b else None)

    def add_link(self, lk: Link) -> bool:
        if not lk.evidence:  # Link refuses this already; defence in depth
            raise NoEvidence(lk.link_type)
        lt = self.taxonomy.link(lk.link_type)
        key = ("l", *map(str, lk.key))
        if key in self._seen:
            return False
        self._seen.add(key)
        fwd_back = not lk.directed
        self.adj[lk.subject].append(
            Edge(lk.subject, lk.object, "link", lk.link_type, True, lk.temporal, lt.path_cost, link=lk)
        )
        self.adj[lk.object].append(
            Edge(lk.object, lk.subject, "link", lk.link_type, fwd_back, lk.temporal, lt.path_cost, link=lk)
        )
        self._degree = None
        return True

    def add_statements(self, stmts: Iterable[Stmt]) -> int:
        return sum(1 for s in stmts if self.add_statement(s))

    def add_links(self, links: Iterable[Link]) -> int:
        return sum(1 for lk in links if self.add_link(lk))

    def add_labels(self, labels: Mapping[str, Mapping[str, str]]) -> None:
        for q, d in labels.items():
            self.labels.setdefault(q, {}).update({k: v for k, v in d.items() if v})

    # -- inspection ---------------------------------------------------------------------------------
    def __contains__(self, node: object) -> bool:
        return node in self.adj

    def degree(self, node: str) -> int:
        if self._degree is None:
            self._degree = {n: len({e.v for e in es}) for n, es in self.adj.items()}
        return self._degree.get(node, 0)

    def label(self, node: str, lang: str) -> str:
        d = self.labels.get(node) or {}
        return d.get(lang) or d.get("en") or d.get("fr") or node

    def node_penalty(self, node: str) -> float:
        p = self.hubs.penalised.get(node, 0.0)
        deg = self.degree(node)
        if node in self.truncated_nodes:
            deg = max(deg, self.hubs.degree_threshold * 64)
        if deg > self.hubs.degree_threshold > 0:
            p += self.hubs.degree_per_doubling * (math.log2(deg) - math.log2(self.hubs.degree_threshold))
        return p

    # -- filtering ----------------------------------------------------------------------------------
    def edge_allowed(self, e: Edge, q: PathQuery, *, reverse: bool = False) -> bool:
        """``reverse``: the edge is walked backwards (search from the target), so a family filter applies to
        the reading v -> u in path order."""
        ks = q.kind_set
        if ks is not None and e.kind_key not in ks:
            return False
        if e.kind_key in q.exclude_set:
            return False
        fams = q.families
        fam = e.rev_family if reverse else e.family
        if fams is not None and (e.kind != "statement" or fam not in fams):
            return False
        named = (ks is not None and e.kind_key in ks) or (fams is not None and fam in fams)
        if e.kind == "statement" and e.type_id in self.hubs.excluded_properties and not named:
            return False
        if e.kind == "statement" and e.type_id in self.taxonomy.opt_in_properties and not named:
            return False
        if e.link is not None and not q.include_blocked_hubs and any(v in self.hubs.blocked for v in e.link.via):
            return False  # a derived link *through* a blocked hub is the hub path in disguise
        return temporal_allows(e.temporal, q)

    def edge_cost(self, e: Edge) -> float:
        """The kind's cost plus the hub penalty of every ``via`` node of a derived link (a co-membership via
        the United Nations is damped like the two hops through the UN node it stands for)."""
        if e.link is None or not e.link.via:
            return e.cost
        return e.cost + sum(self.node_penalty(v) for v in e.link.via)

    def passable(self, node: str, q: PathQuery) -> bool:
        if node in (q.source, q.target):
            return True
        if q.non_transit_props and node in self.non_transit(q.non_transit_props):
            return False
        return q.include_blocked_hubs or node not in self.hubs.blocked

    def non_transit(self, props: frozenset[str]) -> set[str]:
        """Objects of statements with these properties (languages of P37, continents of P30...): such nodes
        may end a path but are never passed through."""
        if self._non_transit is None or self._non_transit[0] != props:
            objs = {
                e.v
                for es in self.adj.values()
                for e in es
                if e.kind == "statement" and e.forward and e.type_id in props
            }
            self._non_transit = (props, objs)
        return self._non_transit[1]


# ---------------------------------------------------------------------------------------------- results
@dataclass
class Hop:
    edge: Edge
    alternatives: int
    text: dict[str, str]
    #: the relation's own label in the traversal direction ({fr, en}), e.g. "langue officielle"
    label: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        e = self.edge
        d: dict[str, Any] = {
            "from": e.u,
            "to": e.v,
            "kind": e.kind,
            "type": e.type_id,
            "forward": e.forward,
            "causal": e.causal,
            "temporal": e.temporal.to_json(),
            "evidence": [ev.to_json() | {"cite": ev.cite()} for ev in e.evidence],
            "sources": sorted({ev.source_label for ev in e.evidence}),
            "alternatives": self.alternatives,
            "text": self.text,
            "label": self.label,
            "family": e.family,
        }
        if e.link is not None:
            d["via"] = list(e.link.via)
            if e.link.weight is not None:
                d["weight"] = e.link.weight
                d["unit"] = e.link.unit
        return d


@dataclass
class Path:
    nodes: list[str]
    hops: list[Hop]
    cost: float

    @property
    def text(self) -> dict[str, list[str]]:
        return {lang: [h.text[lang] for h in self.hops] for lang in LANGS}

    def to_json(self) -> dict[str, Any]:
        return {
            "nodes": self.nodes,
            "hops": [h.to_json() for h in self.hops],
            "hop_count": len(self.hops),
            "cost": round(self.cost, 4),
            "text": self.text,
        }


@dataclass
class PathResult:
    query: PathQuery
    paths: list[Path] = field(default_factory=list)
    reason: str | None = None
    truncated: bool = False
    explored_nodes: int = 0
    labels: dict[str, dict[str, str]] = field(default_factory=dict)
    truncation_reasons: list[str] = field(default_factory=list)
    truncated_nodes: set[str] = field(default_factory=set)

    def cut(self, reason: str) -> None:
        self.truncated = True
        if reason not in self.truncation_reasons:
            self.truncation_reasons.append(reason)

    @property
    def found(self) -> bool:
        return bool(self.paths)

    def to_json(self) -> dict[str, Any]:
        nodes = {n for p in self.paths for n in p.nodes} | {self.query.source, self.query.target}
        return {
            "query": self.query.to_json(),
            "found": self.found,
            "reason": self.reason,
            "truncated": self.truncated,
            "truncation_reasons": self.truncation_reasons,
            "truncated_nodes": sorted(self.truncated_nodes)[:50],
            "explored_nodes": self.explored_nodes,
            "paths": [p.to_json() for p in self.paths],
            "labels": {n: self.labels.get(n, {}) for n in sorted(nodes)},
            "disclaimer": DISCLAIMER,
        }


# ---------------------------------------------------------------------------------------------- rendering
def _source_name(source: str, lang: str) -> str:
    if is_wikidata(source):
        return SOURCE_NAMES["wikidata"][lang]
    from reva.links.model import SOURCE_LABELS

    return SOURCE_LABELS.get(source, source)


def _dates(t: Temporal, lang: str) -> str:
    if t.kind == "undated":
        return f" ({UNDATED[lang]})"
    if t.kind == "interval" and t.start is not None and t.end is None:
        return f" ({'depuis' if lang == 'fr' else 'since'} {t.start.iso()})"
    if t.kind == "interval" and t.start is None and t.end is not None:
        return f" ({'jusqu’en' if lang == 'fr' else 'until'} {t.end.iso()})"
    return f" ({t.label()})"


def _cite(evidence: Sequence[Evidence], limit: int = 4) -> str:
    cites = [e.cite() for e in evidence]
    more = f"; +{len(cites) - limit}" if len(cites) > limit else ""
    return " [" + "; ".join(cites[:limit]) + more + "]"


def hop_label(edge: Edge, graph: PathGraph) -> dict[str, str]:
    """The relation label of a hop in its traversal direction, per language (no node names)."""
    if edge.kind == "statement":
        et = graph.taxonomy.edge_types[edge.type_id]
        if et.causal and et.causal_phrase is not None:
            return {lang: et.causal_phrase[lang] for lang in LANGS if lang in et.causal_phrase}
        src = et.labels if edge.forward else et.inverse_labels
    else:
        lt = graph.taxonomy.link(edge.type_id)
        src = lt.labels if edge.forward else lt.inverse_labels
    return {lang: src[lang] for lang in LANGS if lang in src}


def render_hop(edge: Edge, graph: PathGraph, lang: str, *, cite: bool = True) -> str:
    """One sentence per hop, e.g. ``Brésil — langue officielle → portugais (non daté dans la source)``."""
    if lang not in LANGS:
        raise PathQueryError(f"language must be one of {LANGS}")
    L = lambda n: graph.label(n, lang)  # noqa: E731
    tail = _dates(edge.temporal, lang) + (_cite(edge.evidence) if cite else "")
    if edge.kind == "statement":
        assert edge.stmt is not None
        et = graph.taxonomy.edge_types[edge.type_id]
        if et.causal:
            assert et.causal_phrase is not None and edge.stmt.object is not None
            src = _source_name(edge.stmt.source_key, lang)
            x, y = L(edge.stmt.subject), L(edge.stmt.object)
            if lang == "fr":
                return f"selon {src}, {x} {et.causal_phrase['fr']} {y}{tail}"
            return f"according to {src}, {x} {et.causal_phrase['en']} {y}{tail}"
        label = (et.labels if edge.forward else et.inverse_labels)[lang]
        return f"{L(edge.u)} — {label} → {L(edge.v)}{tail}"
    assert edge.link is not None
    lt = graph.taxonomy.link(edge.type_id)
    label = (lt.labels if edge.forward else lt.inverse_labels)[lang]
    via = ""
    if edge.link.via:
        via = " (via " + ", ".join(L(v) for v in edge.link.via) + ")"
    weight = ""
    if edge.link.weight is not None:
        unit = f" {edge.link.unit}" if edge.link.unit else ""
        weight = f" = {edge.link.weight}{unit}"
    return f"{L(edge.u)} — {label}{via} → {L(edge.v)}{weight}{tail}"


# ---------------------------------------------------------------------------------------------- search
def _bfs(
    graph: PathGraph, start: str, q: PathQuery, depth: int, budget: list[int], restrict: Mapping[str, int] | None
) -> tuple[dict[str, int], bool]:
    dist = {start: 0}
    queue = deque([start])
    truncated = False
    while queue:
        n = queue.popleft()
        d = dist[n]
        if d >= depth:
            continue
        if n != start and not graph.passable(n, q):
            continue  # blocked hub: may be reached (as an endpoint) but not expanded
        if n in (q.source, q.target) and n != start:
            continue  # endpoints are never intermediate
        for e in graph.adj.get(n, ()):
            if e.v in dist or not graph.edge_allowed(e, q, reverse=start == q.target and restrict is None):
                continue
            if restrict is not None:
                r = restrict.get(e.v)
                if r is None or d + 1 + r > q.max_hops:
                    continue
            budget[0] -= 1
            if budget[0] < 0:
                truncated = True
                queue.clear()
                break
            dist[e.v] = d + 1
            queue.append(e.v)
    return dist, truncated


def _best_edge(graph: PathGraph, edges: Sequence[Edge]) -> Edge:
    def rank(e: Edge) -> tuple[Any, ...]:
        return (graph.edge_cost(e), e.temporal.kind == "undated", not e.forward, e.kind != "statement", e.type_id)

    return min(edges, key=rank)


def _late(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() > deadline


def find_paths(graph: PathGraph, q: PathQuery, *, deadline: float | None = None) -> PathResult:
    res = PathResult(q, labels={})
    if graph.truncated_nodes:
        # LINKS-1: the neighbourhood read from the database was cut at these nodes; whatever the outcome,
        # the search did not see the whole graph
        res.truncated_nodes = set(graph.truncated_nodes)
        res.cut(
            f"neighbourhood truncated at {len(graph.truncated_nodes)} node(s) (statement rows beyond the "
            "per-node / per-hop read caps were not loaded)"
        )
    if graph.skipped.get("stored_links_cap_reached"):
        res.cut("stored links (kg.link_row) around the reached nodes capped: some derived links were not loaded")
    for n in (q.source, q.target):
        # an endpoint whose only edges the query never allows (opt-in classification edges, other families)
        # has no traversable statement for this query
        if n not in graph or not any(graph.edge_allowed(e, q, reverse=n == q.target) for e in graph.adj[n]):
            # known entity (labelled) without any traversable statement vs an id the graph has never seen
            res.reason = "no_traversable_statements" if (n in graph.labels or n in graph) else "unknown_node"
            res.labels = {n: graph.labels.get(n, {}) for n in (q.source, q.target)}
            return res
    budget = [q.max_expansions]
    dt, trunc_t = _bfs(graph, q.target, q, q.max_hops, budget, None)
    ds, trunc_s = _bfs(graph, q.source, q, q.max_hops, budget, dt)
    if trunc_t or trunc_s:
        res.cut(f"breadth-first expansion budget of {q.max_expansions} edges exhausted")
    res.explored_nodes = len(set(dt) | set(ds))
    if q.target not in ds:
        res.reason = "no_path_within_max_hops"
        return res
    # pruned weighted digraph
    g = nx.DiGraph()
    parallel: dict[tuple[str, str], list[Edge]] = defaultdict(list)
    for u, du in ds.items():
        if u != q.source and (u == q.target or not graph.passable(u, q)):
            continue
        for e in graph.adj.get(u, ()):
            dv = dt.get(e.v)
            if dv is None or du + 1 + dv > q.max_hops or e.v == q.source:
                continue
            if not graph.edge_allowed(e, q):
                continue
            if e.v not in (q.source, q.target) and not graph.passable(e.v, q):
                continue
            parallel[(u, e.v)].append(e)
    for (u, v), es in parallel.items():
        best = _best_edge(graph, es)
        pen = 0.0 if v == q.target else graph.node_penalty(v)
        g.add_edge(u, v, weight=graph.edge_cost(best) + pen)
    if q.constrained:
        _constrained_search(graph, q, g, parallel, dt, res, deadline)
        return _finish(graph, q, res)
    seen_candidates = 0
    # Yen yields paths by non-decreasing cost, but the order of equal-cost paths follows the graph's insertion
    # order (i.e. the database row order), so unrelated rows could flip which of two equally good paths is shown.
    # Candidates tied with the k-th kept path are therefore also collected, and the kept paths are ranked like the
    # constrained search's: (cost, hops, node ids) (SWEEP2).
    found: list[tuple[float, list[str]]] = []
    try:
        for nodes in nx.shortest_simple_paths(g, q.source, q.target, weight="weight"):
            seen_candidates += 1
            # once k paths are kept, only ties are being collected: stopping then cuts nothing that was asked
            if seen_candidates > MAX_YEN_CANDIDATES:
                if len(found) < q.k:
                    res.cut(f"more than {MAX_YEN_CANDIDATES} candidate paths examined")
                break
            if _late(deadline):
                if len(found) < q.k:
                    res.cut("time budget exhausted while ranking candidate paths")
                break
            if len(nodes) - 1 > q.max_hops:
                continue
            cost = sum(g[u][v]["weight"] for u, v in itertools.pairwise(nodes))
            if len(found) >= q.k and cost > found[q.k - 1][0] + 1e-9:
                break
            found.append((cost, list(nodes)))
    except nx.NetworkXNoPath:
        pass
    found.sort(key=lambda x: (round(x[0], 9), len(x[1]), x[1]))
    for _cost, nodes in found[: q.k]:
        hops = []
        cost = 0.0
        for u, v in itertools.pairwise(nodes):
            es = parallel[(u, v)]
            e = _best_edge(graph, es)
            cost += g[u][v]["weight"]
            hops.append(Hop(e, len(es) - 1, {lang: render_hop(e, graph, lang) for lang in LANGS}, hop_label(e, graph)))
        res.paths.append(Path(list(nodes), hops, cost))
    return _finish(graph, q, res)


def _finish(graph: PathGraph, q: PathQuery, res: PathResult) -> PathResult:
    if not res.paths and res.reason is None:
        res.reason = "no_path_satisfying_constraints" if q.constrained else "no_path_within_max_hops"
    nodes_used = {n for p in res.paths for n in p.nodes} | {q.source, q.target}
    for p in res.paths:
        for h in p.hops:
            if h.edge.link is not None:
                nodes_used |= set(h.edge.link.via)
            if h.edge.stmt is not None and h.edge.stmt.object:
                nodes_used |= {h.edge.stmt.subject, h.edge.stmt.object}
    res.labels = {n: dict(graph.labels.get(n, {})) for n in sorted(nodes_used)}
    return res


def _choose_edges(
    graph: PathGraph,
    nodes: Sequence[str],
    parallel: Mapping[tuple[str, str], list[Edge]],
    steps: Sequence[set[str]],
    q: PathQuery,
) -> list[Edge] | None:
    """One edge per hop such that the ordered ``steps`` are matched by distinct hops (greedy subsequence
    matching is exact: advancing on the earliest matching hop never prevents a later match)."""
    out: list[Edge] = []
    i = 0
    for u, v in itertools.pairwise(nodes):
        es = parallel[(u, v)]
        if i < len(steps):
            match = [e for e in es if e.kind_key in steps[i]]
            if match:
                out.append(_best_edge(graph, match))
                i += 1
                continue
        out.append(_best_edge(graph, es))
    return out if i == len(steps) else None


def _constrained_search(
    graph: PathGraph,
    q: PathQuery,
    g: Any,
    parallel: Mapping[tuple[str, str], list[Edge]],
    dt: Mapping[str, int],
    res: PathResult,
    deadline: float | None,
) -> None:
    """Simple paths of the pruned graph satisfying ordered hop-type steps and waypoint sets, by iterative
    deepening (shortest admissible first), bounded by ``q.max_constrained_steps`` DFS steps and the deadline;
    up to ``max(q.k, 10)`` admissible candidates are ranked by cost and the best ``q.k`` kept."""
    steps = list(q.step_sets)
    want = max(q.k, 10)
    succ: dict[str, list[str]] = defaultdict(list)
    for u, v in g.edges:
        succ[u].append(v)
    for u in succ:
        succ[u].sort(key=lambda v: (g[u][v]["weight"], v))
    work = [0]
    found: list[tuple[float, list[str], list[Edge]]] = []
    seen: set[tuple[str, ...]] = set()

    def dfs(node: str, nodes: list[str], limit: int) -> bool:
        work[0] += 1
        if work[0] > q.max_constrained_steps or (work[0] % 4096 == 0 and _late(deadline)):
            return False
        if node == q.target:
            ns = set(nodes)
            key = tuple(nodes)
            if key not in seen and (not q.via_any or q.via_any & ns) and q.via_all <= ns:
                edges = _choose_edges(graph, nodes, parallel, steps, q)
                if edges is not None:
                    seen.add(key)
                    cost = sum(g[u][v]["weight"] for u, v in itertools.pairwise(nodes))
                    found.append((cost, list(nodes), edges))
            return len(found) < want
        if len(nodes) - 1 >= limit:
            return True
        for v in succ.get(node, ()):
            if v in nodes:
                continue
            dv = dt.get(v)
            if dv is None or len(nodes) + dv > limit:
                continue
            nodes.append(v)
            go = dfs(v, nodes, limit)
            nodes.pop()
            if not go:
                return False
        return True

    start = dt.get(q.source, q.max_hops)
    for limit in range(max(start, len(steps), 1), q.max_hops + 1):
        if not dfs(q.source, [q.source], limit):
            break
    if work[0] > q.max_constrained_steps:
        res.cut(f"constrained search budget of {q.max_constrained_steps} steps exhausted")
    elif _late(deadline):
        res.cut("time budget exhausted during the constrained search")
    found.sort(key=lambda x: (x[0], len(x[1]), x[1]))
    for cost, nodes, edges in found[: q.k]:
        hops = [
            Hop(
                e,
                len(parallel[(e.u, e.v)]) - 1,
                {lang: render_hop(e, graph, lang) for lang in LANGS},
                hop_label(e, graph),
            )
            for e in edges
        ]
        res.paths.append(Path(nodes, hops, cost))


MAX_WAYPOINTS = 4


def find_chain(
    graph: PathGraph | Callable[[PathQuery], PathGraph],
    waypoints: Sequence[str],
    *,
    deadline: float | None = None,
    **query: Any,
) -> PathResult:
    """A path forced through ``waypoints`` (``[Columbus, Treaty of Tordesillas, Brazil, Portuguese]``): the
    best path of each consecutive segment, concatenated. Each segment obeys the same filters and
    ``max_hops``; the result has one path (or none, with ``reason = "no_path_segment:<a>-><b>"``). Segments
    are searched independently, so the concatenation may revisit a node (it is a walk, said as such)."""
    if not 3 <= len(waypoints) <= MAX_WAYPOINTS + 2:
        raise PathQueryError(f"a chain needs 1..{MAX_WAYPOINTS} intermediate waypoints")
    query = {**query, "k": 1}
    first = PathQuery(waypoints[0], waypoints[-1], **query)
    out = PathResult(first)
    nodes: list[str] = [waypoints[0]]
    hops: list[Hop] = []
    cost = 0.0
    for a, b in itertools.pairwise(waypoints):
        if _late(deadline):
            out.cut("time budget exhausted before segment " + f"{a}->{b}")
            out.reason = f"no_path_segment:{a}->{b} (time_budget_exhausted)"
            return out
        sq = PathQuery(a, b, **query)
        seg = find_paths(graph if isinstance(graph, PathGraph) else graph(sq), sq, deadline=deadline)
        for r in seg.truncation_reasons:
            out.cut(r)
        out.truncated |= seg.truncated
        out.truncated_nodes |= seg.truncated_nodes
        out.explored_nodes += seg.explored_nodes
        out.labels.update(seg.labels)
        if not seg.paths:
            out.reason = f"no_path_segment:{a}->{b} ({seg.reason})"
            return out
        p = seg.paths[0]
        nodes += p.nodes[1:]
        hops += p.hops
        cost += p.cost
    out.paths = [Path(nodes, hops, cost)]
    return out


def build_graph(
    statements: Iterable[Stmt],
    links: Iterable[Link] = (),
    labels: Mapping[str, Mapping[str, str]] | None = None,
    *,
    taxonomy: Taxonomy | None = None,
    hubs: Hubs | None = None,
) -> PathGraph:
    g = PathGraph(taxonomy, hubs)
    g.add_statements(statements)
    g.add_links(links)
    if labels:
        g.add_labels(labels)
    return g


# ---------------------------------------------------------------------------------------------- database adapter
_ST_SELECT = """
SELECT statement_key, statement_id, subject_qid, property_pid, object_qid, value, start_time, start_precision,
       end_time, end_precision, pit_time, pit_precision, qualifiers, rank, refs, source_key, source_revision,
       raw_sha256, retrieved_at, origin
  FROM kg.statement_current
"""
CONTEXT_PROPERTIES = ("P31", "P279", "P571", "P576", "P580", "P582", "P585", "P298")


def row_to_stmt(row: Mapping[str, Any]) -> Stmt:
    """A ``kg.statement_current`` row → :class:`Stmt` (statement id = Wikidata GUID, else the KG key)."""
    from reva.links.model import stmt_from_any

    d = dict(row)
    if not d.get("statement_id"):
        d["statement_id"] = d.get("statement_key")
    return stmt_from_any(d)


@dataclass
class KgNeighbourhood:
    """Bounded reads of ``kg.statement_current`` around a few seed nodes (``conn``: SQLAlchemy connection).

    Expansion fetches the incident statements (traversable properties only) of every frontier node, hop by
    hop, never expanding blocked hubs; a frontier whose rows exceed ``max_rows_per_hop`` is truncated and
    flagged. Context statements (classification P31/P279, dates, ISO codes) of the reached nodes are read
    too so that :mod:`reva.links.derive` can classify colonies, polities and alliances.
    """

    conn: Any
    origin: str | None = None  # None: live and test_fixture rows (each hop says which)
    max_rows_per_hop: int = 50_000
    per_node_limit: int = 2_000
    #: rows read by one ``expand`` call over all its hops (SEC-1)
    max_total_rows: int = MAX_ROWS_PER_NEIGHBOURHOOD
    #: ``time.monotonic()`` deadline checked before each hop (``None``: no wall-clock bound)
    deadline: float | None = None
    #: ISO3 codes held by several KG items and absent from the reviewed registry (LINKS-2)
    ambiguous_iso3: dict[str, list[str]] = field(default_factory=dict)

    def _rows(self, sql: str, params: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        from sqlalchemy import text

        return [dict(r._mapping) for r in self.conn.execute(text(sql), dict(params))]

    def expand(
        self, seeds: Sequence[str], depth: int, properties: Sequence[str], blocked: Iterable[str] = ()
    ) -> tuple[list[Stmt], set[str], set[str]]:
        blocked_set = set(blocked)
        reached: set[str] = set(seeds)
        frontier = [s for s in seeds]
        stmts: dict[str, Stmt] = {}
        truncated: set[str] = set()
        total = 0
        for _ in range(depth):
            if not frontier:
                break
            if total >= self.max_total_rows or _late(self.deadline):
                truncated |= set(frontier)  # not expanded: the bound or the time budget is spent
                break
            rows = self._rows(
                _ST_SELECT
                + """ WHERE (CAST(:origin AS text) IS NULL OR origin = CAST(:origin AS text)) AND object_qid IS NOT NULL AND property_pid = ANY(:props)
                      AND (subject_qid = ANY(:f) OR object_qid = ANY(:f))
                      LIMIT :cap""",
                {"origin": self.origin, "props": list(properties), "f": frontier, "cap": self.max_rows_per_hop + 1},
            )
            if len(rows) > self.max_rows_per_hop:
                truncated |= set(frontier)
                rows = rows[: self.max_rows_per_hop]
            total += len(rows)
            per_node: Counter[str] = Counter()
            nxt: set[str] = set()
            fset = set(frontier)
            for r in rows:
                s = row_to_stmt(r)
                for end in (s.subject, s.object):
                    if end in fset:
                        per_node[end] += 1
                if any(per_node[n] > self.per_node_limit for n in (s.subject, s.object) if n in fset):
                    truncated |= {n for n in (s.subject, s.object) if n in fset and per_node[n] > self.per_node_limit}
                    continue
                stmts[s.statement_id] = s
                for end in (s.subject, s.object):
                    if end is not None and end not in reached:
                        reached.add(end)
                        if end not in blocked_set:
                            nxt.add(end)
            frontier = sorted(nxt)
        return list(stmts.values()), reached, truncated

    def context(self, nodes: Iterable[str], class_depth: int = 3) -> list[Stmt]:
        nodes = sorted(set(nodes))
        out: dict[str, Stmt] = {}
        for i in range(0, len(nodes), 5_000):
            for r in self._rows(
                _ST_SELECT
                + " WHERE (CAST(:origin AS text) IS NULL OR origin = CAST(:origin AS text)) AND subject_qid = ANY(:n) AND property_pid = ANY(:p)",
                {"origin": self.origin, "n": nodes[i : i + 5_000], "p": list(CONTEXT_PROPERTIES)},
            ):
                s = row_to_stmt(r)
                out[s.statement_id] = s
        classes = {s.object for s in out.values() if s.pid in ("P31", "P279") and s.object}
        seen: set[str] = set(nodes)
        for _ in range(class_depth):
            todo = sorted(c for c in classes if c not in seen)
            if not todo:
                break
            seen |= set(todo)
            rows = self._rows(
                _ST_SELECT
                + " WHERE (CAST(:origin AS text) IS NULL OR origin = CAST(:origin AS text)) AND subject_qid = ANY(:n) AND property_pid = 'P279'",
                {"origin": self.origin, "n": todo},
            )
            classes = set()
            for r in rows:
                s = row_to_stmt(r)
                out[s.statement_id] = s
                if s.object:
                    classes.add(s.object)
        return list(out.values())

    def labels(self, nodes: Iterable[str]) -> dict[str, dict[str, str]]:
        nodes = sorted(set(nodes))
        out: dict[str, dict[str, str]] = {}
        for i in range(0, len(nodes), 5_000):
            for r in self._rows(
                """SELECT l.qid, l.lang, l.label FROM kg.entity_label l
                     JOIN kg.entity_current e ON e.id = l.entity_id
                    WHERE l.qid = ANY(:n) AND NOT l.is_alias AND l.lang IN ('fr', 'en') AND (CAST(:origin AS text) IS NULL OR e.origin = CAST(:origin AS text))""",
                {"n": nodes[i : i + 5_000], "origin": self.origin},
            ):
                out.setdefault(r["qid"], {})[r["lang"]] = r["label"]
        return out

    def iso3_to_qid(self, iso3s: Iterable[str] | None = None) -> dict[str, str]:
        """ISO 3166 alpha-3 → QID. The reviewed ``registry/entities/country_qids.json`` decides for the codes
        it lists; otherwise the KG's P298 statements, and only when exactly one item holds the code. A code
        held by several items (PSE, YUG, ATA on Wikidata) is left out and recorded in ``ambiguous_iso3``
        (never resolved by rank or identifier order)."""
        from reva.kg.query import country_qid_registry

        sql = (
            _ST_SELECT
            + " WHERE (CAST(:origin AS text) IS NULL OR origin = CAST(:origin AS text)) AND property_pid = 'P298'"
        )
        rows = self._rows(sql, {"origin": self.origin})
        want = None if iso3s is None else {x.upper() for x in iso3s}
        holders: dict[str, set[str]] = defaultdict(set)
        for r in rows:
            s = row_to_stmt(r)
            if s.value and (want is None or s.value.upper() in want):
                holders[s.value.upper()].add(s.subject)
        registry = country_qid_registry()
        out: dict[str, str] = {}
        for code in sorted(set(holders) | ({c for c in registry if want is None or c in want})):
            if code in registry:
                out[code] = registry[code]
            elif len(holders[code]) == 1:
                out[code] = next(iter(holders[code]))
            else:
                self.ambiguous_iso3[code] = sorted(holders[code])
        return out


def graph_from_db(
    conn: Any,
    q: PathQuery,
    *,
    origin: str | None = None,
    with_derived: bool = True,
    taxonomy: Taxonomy | None = None,
    hubs: Hubs | None = None,
    deadline: float | None = None,
) -> PathGraph:
    """Read the neighbourhoods of ``q.source`` (⌈max_hops/2⌉ hops) and ``q.target`` (⌊max_hops/2⌋ hops):
    every path of length ≤ max_hops has each of its edges incident to a node expanded on one side.
    Derived links (shared language, colonial ties...) are computed on the fetched statements."""
    from reva.links.derive import derive_all

    tax = taxonomy or load_taxonomy()
    hb = hubs or load_hubs()
    nb = KgNeighbourhood(conn, origin=origin, deadline=deadline)
    props = sorted(tax.edge_types)
    if q.families is not None:  # V4: read only the properties of the families asked for
        props = sorted({p for p, et in tax.edge_types.items() if _family_pid_allowed(p, q.families)})
    a = math.ceil(q.max_hops / 2)
    b = q.max_hops - a
    blocked = set() if q.include_blocked_hubs else set(hb.blocked)
    s1, r1, t1 = nb.expand([q.source], a, props, blocked)
    s2, r2, t2 = nb.expand([q.target], max(b, 1), props, blocked)
    reached = r1 | r2
    stmts = {s.statement_id: s for s in (*s1, *s2)}
    ctx = nb.context(reached)
    all_stmts = list({**stmts, **{s.statement_id: s for s in ctx}}.values())
    g = PathGraph(tax, hb)
    g.add_statements(stmts.values())
    if with_derived:
        try:
            g.add_links(derive_all(all_stmts).links)
        except (LinkError, NoEvidence):  # pragma: no cover - a malformed row never blocks the path query
            g.skipped["derivation_error"] += 1
    g.truncated_nodes = t1 | t2
    if with_derived:
        _add_stored_links(conn, g, reached, origin)
    g.add_labels(nb.labels(set(g.adj) | reached))
    return g


def _family_pid_allowed(pid: str, families: frozenset[str]) -> bool:
    from reva.kg.families import FamilyError, load_registry

    try:
        reg = load_registry()
    except FamilyError:  # pragma: no cover
        return False
    return any((ef := reg.edge_family(pid, d)) is not None and ef.family in families for d in ("out", "in"))


#: stored links read around the reached nodes of one path query (SEC-1 bound)
MAX_STORED_LINKS_PER_QUERY = 20_000


def _add_stored_links(conn: Any, g: PathGraph, nodes: set[str], origin: str | None) -> None:
    """Links of the latest materialisation (``kg.link_row``: atlas flows, vote agreement, CShapes/UCDP kinds...)
    incident to the reached nodes; a materialisation built from another KG origin is not mixed in."""
    from reva.links.store import latest_materialisation, rows_touching

    m = latest_materialisation(conn)
    if m is None or (origin is not None and m.get("kg_origin") not in (None, origin)):
        return
    links, cut = rows_touching(conn, m["id"], nodes, cap=MAX_STORED_LINKS_PER_QUERY)
    g.add_links(links)
    g.skipped["stored_links"] += len(links)
    if cut:
        g.skipped["stored_links_cap_reached"] += 1  # reported by find_paths as a truncation
