"""Path search and ego graph over :class:`reva.v8.paths.index.PathIndex` (agent V8LIENS).

Search (k cheapest diverse paths of at most ``max_hops`` hops):

1. hop-limited cheapest-cost relaxation from both ends (⌈H/2⌉ levels from ``from``, ⌊H/2⌋ from ``to``),
   vectorised over the CSR arrays. Cost of a hop = its edge cost in path order (statement: the family's
   ``path_cost``; causal claim: by grade, A cheapest; estimate: fixed; walking a claim against its cause → effect
   direction costs more) + the penalty of the node entered (declared hub penalty + per doubling of its degree above
   the hubs.yaml threshold). Blocked hubs ("Earth", "human", "country"…) are never intermediate nodes; nodes with
   more than ``hub_limit`` incident edges are reached but not expanded (reported).
2. every node reached from both sides is a meeting point; its cheapest from-part + to-part is a candidate path
   (exact: the cheapest path of ≤ H hops has a node with ≤ ⌈H/2⌉ hops on its left and ≤ ⌊H/2⌋ on its right).
3. candidates in cost order; non-simple ones dropped; a candidate that differs from a kept path only by one
   intermediate node (same hop types) is counted as a *variant* of it instead of filling the list.

Every bound that cuts the search is reported (``truncated`` + reasons): a "no path" over a cut search never reads
as a definitive absence.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from reva.v8.paths.index import EDGE_CLAIM, EDGE_KINDS, EDGE_STATEMENT, NO_FAMILY, PathIndex

MAX_HOPS = 6
MAX_K = 10
DEFAULT_HUB_LIMIT = 2_000
MAX_HUB_LIMIT = 50_000
#: CSR entries scanned in one relaxation level (both sides apply it); more are cut by cheapest frontier first.
MAX_ENTRIES_PER_LEVEL = 3_000_000
MAX_CANDIDATES = 600
MAX_VARIANT_EXAMPLES = 5


class PathSearchError(ValueError):
    pass


@dataclass(frozen=True)
class Hop:
    frm: int
    to: int
    edge: int
    rec_fwd: bool  # the hop, in path order, follows the record's subject→object / cause→effect direction
    cost: float  # edge cost in path order
    penalty: float  # penalty of the node entered (0 for the end node)


@dataclass
class FoundPath:
    nodes: list[int]
    hops: list[Hop]
    cost: float
    variants: int = 0
    variant_position: int | None = None
    variant_examples: list[int] = field(default_factory=list)


@dataclass
class SearchStats:
    levels: int = 0
    entries_scanned: int = 0
    nodes_reached: int = 0
    candidates: int = 0
    hubs_not_expanded: dict[int, int] = field(default_factory=dict)
    truncated: bool = False
    truncation_reasons: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0

    def cut(self, reason: str) -> None:
        self.truncated = True
        if reason not in self.truncation_reasons:
            self.truncation_reasons.append(reason)


@dataclass(frozen=True)
class EdgeFilter:
    """Which edges a query may traverse."""

    families: frozenset[int] | None = None  # family indices allowed (None: every family)
    include_causal: bool = False
    origin: str | None = None  # live | test_fixture | None (both)

    def mask(self, idx: PathIndex, edges: np.ndarray, rec_fwd: np.ndarray) -> np.ndarray:
        kind = idx.e_kind[edges]
        stmt = kind == EDGE_STATEMENT
        fam = np.where(rec_fwd, idx.e_fam_fwd[edges], idx.e_fam_rev[edges])
        if self.families is None:
            ok_stmt = stmt & ~idx.e_excluded[edges]
        else:
            ok_stmt = stmt & np.isin(fam, np.fromiter(self.families, dtype=np.int16)) & (fam != NO_FAMILY)
        ok = ok_stmt | ((kind != EDGE_STATEMENT) & self.include_causal)
        if self.origin == "live":
            ok &= ~idx.e_fixture[edges]
        elif self.origin == "test_fixture":
            ok &= idx.e_fixture[edges]
        return np.asarray(ok, dtype=bool)


def edge_cost(idx: PathIndex, edges: np.ndarray, rec_fwd: np.ndarray) -> np.ndarray:
    return np.asarray(np.where(rec_fwd, idx.e_cost_fwd[edges], idx.e_cost_rev[edges]), dtype=np.float64)


def _gather(idx: PathIndex, nodes: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """CSR positions of every entry of ``nodes`` and the owner of each position."""
    starts = idx.indptr[nodes]
    counts = idx.indptr[nodes + 1] - starts
    total = int(counts.sum())
    if total == 0:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int32)
    owner = np.repeat(nodes, counts)
    offs = np.arange(total, dtype=np.int64) - np.repeat(np.cumsum(counts) - counts, counts)
    return np.repeat(starts, counts) + offs, owner


@dataclass
class _Side:
    start: int
    best: np.ndarray
    level: np.ndarray
    #: per level: nodes (sorted), CSR position, parent node, parent level
    records: list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]]


def _relax(
    idx: PathIndex,
    start: int,
    other: int,
    levels: int,
    flt: EdgeFilter,
    hub_limit: int,
    reverse: bool,
    stats: SearchStats,
    deadline: float | None,
) -> _Side:
    n = idx.n_nodes
    best = np.full(n, np.inf)
    lvl = np.full(n, -1, dtype=np.int8)
    best[start] = 0.0
    lvl[start] = 0
    side = _Side(start, best, lvl, [])
    frontier = np.array([start], dtype=np.int32)
    for level in range(1, levels + 1):
        if deadline is not None and time.monotonic() > deadline:
            stats.cut("time budget exhausted")
            break
        # expandable: the side's own start always; never the other end, a blocked hub or a node over hub_limit
        exp = frontier[(frontier != other)]
        if len(exp):
            inter = exp != start
            hub = inter & (idx.degree[exp] > hub_limit)
            for q in exp[hub][:200]:
                stats.hubs_not_expanded[int(q)] = int(idx.degree[q])
            exp = exp[~(inter & (idx.blocked[exp] | hub))]
        if not len(exp):
            break
        # cheapest frontier first when the level would scan too many entries
        counts = idx.indptr[exp + 1] - idx.indptr[exp]
        if int(counts.sum()) > MAX_ENTRIES_PER_LEVEL:
            order = np.argsort(best[exp], kind="stable")
            keep = np.searchsorted(np.cumsum(counts[order]), MAX_ENTRIES_PER_LEVEL, side="right")
            stats.cut(f"level {level}: {len(exp) - keep} frontier node(s) not expanded (entry cap)")
            exp = exp[order[: max(int(keep), 1)]]
        pos, owner = _gather(idx, exp)
        stats.entries_scanned += len(pos)
        stats.levels += 1
        if not len(pos):
            break
        edges = idx.adj_edge[pos]
        nbr = idx.adj_nbr[pos]
        trav_fwd = idx.adj_fwd[pos]
        rec_fwd = ~trav_fwd if reverse else trav_fwd
        ok = flt.mask(idx, edges, rec_fwd) & (nbr != start)
        # blocked hubs are never intermediate: reachable only when they are the other end
        ok &= ~idx.blocked[nbr] | (nbr == other)
        if not ok.any():
            break
        pos, owner, edges, nbr, rec_fwd = pos[ok], owner[ok], edges[ok], nbr[ok], rec_fwd[ok]
        pen = np.where(nbr == other, 0.0, idx.penalty[nbr].astype(np.float64))
        newc = best[owner] + edge_cost(idx, edges, rec_fwd) + pen
        order = np.lexsort((newc, nbr))
        nb_sorted = nbr[order]
        first = np.ones(len(order), dtype=bool)
        first[1:] = nb_sorted[1:] != nb_sorted[:-1]
        sel = order[first]
        v = nbr[sel]
        c = newc[sel]
        improve = c < best[v] - 1e-12
        sel, v, c = sel[improve], v[improve], c[improve]
        if not len(v):
            break
        parent = owner[sel]
        rec = np.argsort(v)
        side.records.append((v[rec], pos[sel][rec], parent[rec], lvl[parent][rec].copy()))
        best[v] = c
        lvl[v] = level
        frontier = v.astype(np.int32)
    return side


def _walk(idx: PathIndex, side: _Side, node: int, reverse: bool) -> list[Hop] | None:
    """Hops from the side's start to ``node`` (``reverse``: from ``node`` to the side's start, path order)."""
    hops: list[Hop] = []
    cur = node
    lv = int(side.level[cur])
    guard = 0
    while lv > 0:
        guard += 1
        if guard > MAX_HOPS + 1:
            return None
        nodes, pos, parent, plevel = side.records[lv - 1]
        i = int(np.searchsorted(nodes, cur))
        if i >= len(nodes) or nodes[i] != cur:
            return None
        p, u = int(pos[i]), int(parent[i])
        e = int(idx.adj_edge[p])
        trav_fwd = bool(idx.adj_fwd[p])
        if reverse:
            rec_fwd = not trav_fwd
            c = float(idx.e_cost_fwd[e] if rec_fwd else idx.e_cost_rev[e])
            hops.append(Hop(cur, u, e, rec_fwd, c, 0.0))
        else:
            c = float(idx.e_cost_fwd[e] if trav_fwd else idx.e_cost_rev[e])
            hops.append(Hop(u, cur, e, trav_fwd, c, 0.0))
        cur, lv = u, int(plevel[i])
    if cur != side.start:
        return None
    if not reverse:
        hops.reverse()
    return hops


def _signature(idx: PathIndex, hops: Sequence[Hop]) -> tuple[Any, ...]:
    return tuple((int(idx.e_kind[h.edge]), int(idx.e_pid[h.edge]), h.rec_fwd) for h in hops)


def find_paths(
    idx: PathIndex,
    src: int,
    dst: int,
    *,
    max_hops: int = 4,
    k: int = 5,
    flt: EdgeFilter | None = None,
    hub_limit: int = DEFAULT_HUB_LIMIT,
    time_budget_s: float | None = 5.0,
) -> tuple[list[FoundPath], SearchStats]:
    if not 1 <= max_hops <= MAX_HOPS:
        raise PathSearchError(f"max_hops must be within 1..{MAX_HOPS}")
    if not 1 <= k <= MAX_K:
        raise PathSearchError(f"k must be within 1..{MAX_K}")
    if src == dst:
        raise PathSearchError("from and to are the same node")
    flt = flt or EdgeFilter()
    t0 = time.perf_counter()
    deadline = None if time_budget_s is None else time.monotonic() + time_budget_s
    stats = SearchStats()
    hs = math.ceil(max_hops / 2)
    ht = max_hops - hs
    fwd = _relax(idx, src, dst, hs, flt, hub_limit, False, stats, deadline)
    bwd = _relax(idx, dst, src, ht, flt, hub_limit, True, stats, deadline)
    both = np.flatnonzero(np.isfinite(fwd.best) & np.isfinite(bwd.best))
    stats.nodes_reached = int(np.isfinite(fwd.best).sum() + np.isfinite(bwd.best).sum())
    out: list[FoundPath] = []
    if len(both):
        inter = (both != src) & (both != dst)
        total = fwd.best[both] + bwd.best[both] - np.where(inter, idx.penalty[both], 0.0)
        order = np.argsort(total, kind="stable")[:MAX_CANDIDATES]
        stats.candidates = len(order)
        seen: set[tuple[int, ...]] = set()
        for oi in order:
            m = int(both[oi])
            left = _walk(idx, fwd, m, reverse=False)
            right = _walk(idx, bwd, m, reverse=True)
            if left is None or right is None:
                continue
            hops = left + right
            if not hops or len(hops) > max_hops:
                continue
            nodes = [hops[0].frm] + [h.to for h in hops]
            if len(set(nodes)) != len(nodes):
                continue  # not simple
            sig = tuple(h.edge for h in hops)
            if sig in seen:
                continue
            seen.add(sig)
            hops = [
                Hop(h.frm, h.to, h.edge, h.rec_fwd, h.cost, 0.0 if h.to in (src, dst) else float(idx.penalty[h.to]))
                for h in hops
            ]
            cost = sum(h.cost + h.penalty for h in hops)
            if _merge_variant(idx, out, nodes, hops):
                continue
            if len(out) < k:
                out.append(FoundPath(nodes, hops, cost))
            elif not any(p.variants < 1000 for p in out):
                break
    out.sort(key=lambda p: (round(p.cost, 9), len(p.hops), p.nodes))
    stats.elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
    return out, stats


def _merge_variant(idx: PathIndex, kept: list[FoundPath], nodes: list[int], hops: list[Hop]) -> bool:
    sig = _signature(idx, hops)
    for p in kept:
        if len(p.nodes) != len(nodes) or _signature(idx, p.hops) != sig:
            continue
        diff = [i for i, (a, b) in enumerate(zip(p.nodes, nodes, strict=True)) if a != b]
        # one intermediate node differs, or two adjacent ones (a country and its scoped estimate node, a facility
        # and the region it is located in): the same route through another member of the same set
        block = len(diff) == 1 or (len(diff) == 2 and diff[1] == diff[0] + 1)
        if block and 0 < diff[0] and diff[-1] < len(nodes) - 1:
            if p.variant_position is None:
                p.variant_position = diff[0]
            p.variants += 1
            if len(p.variant_examples) < MAX_VARIANT_EXAMPLES and nodes[diff[0]] not in p.variant_examples:
                p.variant_examples.append(nodes[diff[0]])
            return True
    return False


# ---------------------------------------------------------------------------------------------- ego graph
@dataclass(frozen=True)
class EgoEntry:
    nbr: int
    edge: int
    rec_fwd: bool  # read from the centre: follows the record direction
    group: str  # family id, or the edge kind for non-statement edges


def neighbourhood(
    idx: PathIndex, node: int, *, flt: EdgeFilter | None = None, limit: int = 40, per_group: int | None = None
) -> tuple[list[EgoEntry], dict[str, int]]:
    """Up to ``limit`` neighbours of ``node``, spread round-robin over relation groups (family, or causal claim /
    estimate / scope / bridge), most connected first within a group; plus the total per group."""
    flt = flt or EdgeFilter()
    pos, _ = _gather(idx, np.array([node], dtype=np.int32))
    if not len(pos):
        return [], {}
    edges = idx.adj_edge[pos]
    rec_fwd = idx.adj_fwd[pos]
    ok = flt.mask(idx, edges, rec_fwd)
    edges, rec_fwd, nbr = edges[ok], rec_fwd[ok], idx.adj_nbr[pos][ok]
    kind = idx.e_kind[edges]
    fam = np.where(rec_fwd, idx.e_fam_fwd[edges], idx.e_fam_rev[edges])
    gkey = np.where(kind == EDGE_STATEMENT, fam.astype(np.int32), -10 - kind.astype(np.int32))
    totals: dict[str, int] = {}
    groups: list[list[EgoEntry]] = []
    for g in np.unique(gkey):
        sel = np.flatnonzero(gkey == g)
        name = idx.families[int(g)] if g >= 0 else EDGE_KINDS[-10 - int(g)]
        totals[name] = len(sel)
        # most connected neighbour first (ties: claims by grade, then node id); one entry per neighbour
        deg = idx.degree[nbr[sel]].astype(np.int64)
        grade = np.where(idx.e_kind[edges[sel]] == EDGE_CLAIM, idx.e_grade[edges[sel]], 9).astype(np.int64)
        order = sel[np.lexsort((nbr[sel], -deg, grade))]
        seen: set[int] = set()
        lst: list[EgoEntry] = []
        cap = per_group or limit
        for i in order:
            q = int(nbr[i])
            if q in seen:
                continue
            seen.add(q)
            lst.append(EgoEntry(q, int(edges[i]), bool(rec_fwd[i]), name))
            if len(lst) >= cap:
                break
        groups.append(lst)
    out: list[EgoEntry] = []
    taken: set[int] = set()
    i = 0
    while len(out) < limit and any(i < len(g) for g in groups):
        for g in groups:
            if i < len(g) and g[i].nbr not in taken and len(out) < limit:
                out.append(g[i])
                taken.add(g[i].nbr)
        i += 1
    return out, totals


def parallel_count(idx: PathIndex, hop: Hop, flt: EdgeFilter) -> int:
    """Other allowed edges between the same two nodes, in the same path order (alternatives to the chosen hop)."""
    pos, _ = _gather(idx, np.array([hop.frm], dtype=np.int32))
    sel = pos[idx.adj_nbr[pos] == hop.to]
    if not len(sel):
        return 0
    edges = idx.adj_edge[sel]
    ok = flt.mask(idx, edges, idx.adj_fwd[sel])
    return max(0, int(ok.sum()) - 1)


def family_filter(idx: PathIndex, families: Iterable[str] | None) -> frozenset[int] | None:
    if families is None:
        return None
    fams = list(families)
    unknown = [f for f in fams if f not in idx.families]
    if unknown:
        raise PathSearchError(f"unknown relation family: {', '.join(sorted(unknown))}")
    return frozenset(idx.families.index(f) for f in fams)
