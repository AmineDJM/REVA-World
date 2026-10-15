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
from collections import Counter, defaultdict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import networkx as nx

from reva.links.model import Evidence, Link, LinkError, NoEvidence, Stmt, Temporal, is_wikidata
from reva.links.taxonomy import Hubs, Taxonomy, load_hubs, load_taxonomy

MAX_HOPS_LIMIT = 6
MAX_K = 10
DEFAULT_MAX_EXPANSIONS = 200_000
MAX_YEN_CANDIDATES = 400
LANGS = ("fr", "en")

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

    def _norm(self, kinds: Iterable[str]) -> set[str]:
        out = set()
        for k in kinds:
            if ":" in k and k.split(":", 1)[0] in ("statement", "link"):
                out.add(k)
            elif k.startswith("P") and k[1:].isdigit():
                out.add(f"statement:{k}")
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
        self.adj[s.subject].append(
            Edge(s.subject, s.object, "statement", s.pid, True, t, et.path_cost, et.causal, stmt=s)
        )
        self.adj[s.object].append(
            Edge(s.object, s.subject, "statement", s.pid, False, t, et.path_cost, et.causal, stmt=s)
        )
        self._degree = None
        return True

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
    def edge_allowed(self, e: Edge, q: PathQuery) -> bool:
        ks = q.kind_set
        if ks is not None and e.kind_key not in ks:
            return False
        if e.kind_key in q.exclude_set:
            return False
        if e.kind == "statement" and e.type_id in self.hubs.excluded_properties:
            if ks is None or e.kind_key not in ks:
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
        return node in (q.source, q.target) or q.include_blocked_hubs or node not in self.hubs.blocked


# ---------------------------------------------------------------------------------------------- results
@dataclass
class Hop:
    edge: Edge
    alternatives: int
    text: dict[str, str]

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
            if e.v in dist or not graph.edge_allowed(e, q):
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


def find_paths(graph: PathGraph, q: PathQuery) -> PathResult:
    res = PathResult(q, labels={})
    for n in (q.source, q.target):
        if n not in graph:
            # known entity (labelled) without any traversable statement vs an id the graph has never seen
            res.reason = "no_traversable_statements" if n in graph.labels else "unknown_node"
            res.labels = {n: graph.labels.get(n, {}) for n in (q.source, q.target)}
            return res
    budget = [q.max_expansions]
    dt, trunc_t = _bfs(graph, q.target, q, q.max_hops, budget, None)
    ds, trunc_s = _bfs(graph, q.source, q, q.max_hops, budget, dt)
    res.truncated = trunc_t or trunc_s
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
    seen_candidates = 0
    try:
        for nodes in nx.shortest_simple_paths(g, q.source, q.target, weight="weight"):
            seen_candidates += 1
            if seen_candidates > MAX_YEN_CANDIDATES:
                res.truncated = True
                break
            if len(nodes) - 1 > q.max_hops:
                continue
            hops = []
            cost = 0.0
            for u, v in itertools.pairwise(nodes):
                es = parallel[(u, v)]
                e = _best_edge(graph, es)
                cost += g[u][v]["weight"]
                hops.append(Hop(e, len(es) - 1, {lang: render_hop(e, graph, lang) for lang in LANGS}))
            res.paths.append(Path(list(nodes), hops, cost))
            if len(res.paths) >= q.k:
                break
    except nx.NetworkXNoPath:
        pass
    if not res.paths:
        res.reason = "no_path_within_max_hops"
    nodes_used = {n for p in res.paths for n in p.nodes} | {q.source, q.target}
    for p in res.paths:
        for h in p.hops:
            if h.edge.link is not None:
                nodes_used |= set(h.edge.link.via)
            if h.edge.stmt is not None and h.edge.stmt.object:
                nodes_used |= {h.edge.stmt.subject, h.edge.stmt.object}
    res.labels = {n: dict(graph.labels.get(n, {})) for n in sorted(nodes_used)}
    return res


MAX_WAYPOINTS = 4


def find_chain(
    graph: PathGraph | Callable[[PathQuery], PathGraph], waypoints: Sequence[str], **query: Any
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
        sq = PathQuery(a, b, **query)
        seg = find_paths(graph if isinstance(graph, PathGraph) else graph(sq), sq)
        out.truncated |= seg.truncated
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
        for _ in range(depth):
            if not frontier:
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
        sql = (
            _ST_SELECT
            + " WHERE (CAST(:origin AS text) IS NULL OR origin = CAST(:origin AS text)) AND property_pid = 'P298'"
        )
        rows = self._rows(sql, {"origin": self.origin})
        out: dict[str, str] = {}
        want = None if iso3s is None else {x.upper() for x in iso3s}
        for r in sorted(rows, key=lambda r: (r.get("rank") != "preferred", str(r.get("subject_qid")))):
            s = row_to_stmt(r)
            if s.value and (want is None or s.value.upper() in want):
                out.setdefault(s.value.upper(), s.subject)
        return out


def graph_from_db(
    conn: Any,
    q: PathQuery,
    *,
    origin: str | None = None,
    with_derived: bool = True,
    taxonomy: Taxonomy | None = None,
    hubs: Hubs | None = None,
) -> PathGraph:
    """Read the neighbourhoods of ``q.source`` (⌈max_hops/2⌉ hops) and ``q.target`` (⌊max_hops/2⌋ hops):
    every path of length ≤ max_hops has each of its edges incident to a node expanded on one side.
    Derived links (shared language, colonial ties...) are computed on the fetched statements."""
    from reva.links.derive import derive_all

    tax = taxonomy or load_taxonomy()
    hb = hubs or load_hubs()
    nb = KgNeighbourhood(conn, origin=origin)
    props = sorted(tax.edge_types)
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
    g.add_labels(nb.labels(set(g.adj) | reached))
    return g
