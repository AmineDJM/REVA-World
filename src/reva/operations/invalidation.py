"""Incremental invalidation from the transactional outbox.

Node keys (ops.dependency / ops.dirty_node):
  ``observation:{concept_id}:{subject_key}``  a source lineage family
  ``belief:{concept_id}``                     belief estimates of a concept
  anything downstream (``scenario:…``, ``model:…``, ``card:…``) registered in ops.dependency.

``evidence.observations_changed`` events seed both forms for each changed
(concept, subject). Propagation follows ops.dependency edges (DAG descendants).
Every strongly connected component with more than one node (a feedback
coupling) is invalidated as a whole once any member is reached
(reason ``coupled_component``) — cycles are never partially refreshed.

Materiality: an edge whose every registered version declares
``approximation_policy = {"kind": "materiality", "threshold": t, "error_budget": B}``
may skip refreshing its downstream node only when the event quantifies the
upstream change (``payload.change_bounds[upstream_key]``) and it is ≤ t. Each
skip adds the bound to ``ops.dirty_node.accumulated_error_bound`` (reason
``materiality_skip``); once the accumulated bound exceeds B the node is forced
dirty (reason ``error_budget_exhausted``). Unquantified changes are never
skipped. Skipped nodes do not propagate further (their value is unchanged).

Belief nodes already covered by a published release of the same origin whose
knowledge cutoff is at/after the event are not re-dirtied (prevents duplicate
releases when the outbox is processed after publication). When belief nodes
are dirty after a batch, a publish_beliefs job is enqueued idempotently per
origin.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Connection, Engine, text

log = logging.getLogger("reva.invalidation")

OBS_EVENT = "evidence.observations_changed"
SKIP = "materiality_skip"
EXHAUSTED = "error_budget_exhausted"


@dataclass
class InvalidationReport:
    events: int = 0
    observation_events: int = 0
    dirty: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    forced: list[str] = field(default_factory=list)
    coupled_components: list[list[str]] = field(default_factory=list)
    publish_jobs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Edge:
    upstream: str
    downstream: str
    threshold: Decimal | None  # None = exact (always propagate)
    budget: Decimal | None


def _dec(v: Any) -> Decimal | None:
    if v is None:
        return None
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        return None
    return d if d.is_finite() and d >= 0 else None


def strongly_connected_components(nodes: set[str], adj: dict[str, list[str]]) -> list[list[str]]:
    """Iterative Tarjan. Returns all SCCs (singletons included)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    counter = 0
    for root in sorted(nodes):
        if root in index:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            v, i = work.pop()
            if i == 0:
                index[v] = low[v] = counter
                counter += 1
                stack.append(v)
                on_stack.add(v)
            succ = adj.get(v, [])
            recurse = False
            while i < len(succ):
                w = succ[i]
                i += 1
                if w not in index:
                    work.append((v, i))
                    work.append((w, 0))
                    recurse = True
                    break
                if w in on_stack:
                    low[v] = min(low[v], index[w])
            if recurse:
                continue
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                out.append(sorted(comp))
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
    return out


def _reachable_edges(conn: Connection, seeds: list[str]) -> list[Edge]:
    rows = conn.execute(
        text(
            """WITH RECURSIVE reach(k) AS (
                 SELECT unnest(CAST(:seeds AS text[]))
                 UNION
                 SELECT d.downstream_key FROM ops.dependency d JOIN reach r ON d.upstream_key = r.k)
               SELECT d.upstream_key, d.downstream_key, d.approximation_policy
                 FROM ops.dependency d WHERE d.upstream_key IN (SELECT k FROM reach)"""
        ),
        {"seeds": seeds},
    ).all()
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for up, down, pol in rows:
        grouped[(up, down)].append(dict(pol or {}))
    edges = []
    for (up, down), policies in grouped.items():
        thresholds = [_dec(p.get("threshold")) for p in policies]
        budgets = [_dec(p.get("error_budget")) for p in policies]
        if all(p.get("kind") == "materiality" for p in policies) and all(
            t is not None for t in thresholds) and all(b is not None for b in budgets):
            edges.append(Edge(up, down, min(t for t in thresholds if t is not None),
                              min(b for b in budgets if b is not None)))
        else:  # any exact version makes the edge exact (strictest wins)
            edges.append(Edge(up, down, None, None))
    return edges


def _event_seeds(payload: dict[str, Any]) -> list[str]:
    seeds: set[str] = set()
    for item in payload.get("keys", []):
        if isinstance(item, list | tuple) and len(item) >= 1:
            seeds.add(f"belief:{item[0]}")
            if len(item) >= 2:
                seeds.add(f"observation:{item[0]}:{item[1]}")
    for k in payload.get("node_keys", []):
        seeds.add(str(k))
    return sorted(seeds)


def _propagate(seeds: list[str], edges: list[Edge], bounds: dict[str, Decimal]
               ) -> tuple[dict[str, str], dict[str, tuple[Decimal, Decimal]], list[list[str]]]:
    """Returns (dirty node -> reason, skipped node -> (bound, budget), coupled components)."""
    adj: dict[str, list[str]] = defaultdict(list)
    out_edges: dict[str, list[Edge]] = defaultdict(list)
    nodes: set[str] = set(seeds)
    for e in edges:
        adj[e.upstream].append(e.downstream)
        out_edges[e.upstream].append(e)
        nodes.update((e.upstream, e.downstream))
    comps = [c for c in strongly_connected_components(nodes, adj) if len(c) > 1]
    comp_of = {n: i for i, c in enumerate(comps) for n in c}
    dirty: dict[str, str] = {}
    skipped: dict[str, tuple[Decimal, Decimal]] = {}
    touched_components: set[int] = set()
    queue: deque[str] = deque()

    def mark(n: str, reason: str) -> None:
        if n in dirty:
            return
        dirty[n] = reason
        skipped.pop(n, None)
        queue.append(n)
        ci = comp_of.get(n)
        if ci is not None and ci not in touched_components:
            touched_components.add(ci)
            for m in comps[ci]:
                if m not in dirty:
                    dirty[m] = "coupled_component"
                    skipped.pop(m, None)
                    queue.append(m)

    for s in seeds:
        mark(s, "upstream_changed")
    while queue:
        u = queue.popleft()
        for e in out_edges.get(u, []):
            v = e.downstream
            if v in dirty:
                continue
            same_component = comp_of.get(u) is not None and comp_of.get(u) == comp_of.get(v)
            b = bounds.get(u)
            if (e.threshold is not None and e.budget is not None and b is not None and b <= e.threshold
                    and not same_component):
                prev = skipped.get(v)
                skipped[v] = ((prev[0] if prev else Decimal(0)) + b, min(e.budget, prev[1]) if prev else e.budget)
                continue
            mark(v, "upstream_changed")
    return dirty, skipped, [comps[i] for i in sorted(touched_components)]


def _apply(conn: Connection, seq: int, dirty: dict[str, str], skipped: dict[str, tuple[Decimal, Decimal]],
           report: InvalidationReport) -> None:
    keys = sorted(set(dirty) | set(skipped))
    if not keys:
        return
    existing = {
        r.node_key: (r.reason, dict(r.accumulated_error_bound))
        for r in conn.execute(
            text("SELECT node_key, reason, accumulated_error_bound FROM ops.dirty_node "
                 "WHERE node_key = ANY(:k) FOR UPDATE"), {"k": keys}
        )
    }
    upsert = text(
        """INSERT INTO ops.dirty_node(node_key, since_sequence, accumulated_error_bound, reason)
           VALUES (:k, :s, CAST(:b AS jsonb), :r)
           ON CONFLICT (node_key) DO UPDATE SET reason = EXCLUDED.reason,
             accumulated_error_bound = EXCLUDED.accumulated_error_bound,
             since_sequence = LEAST(ops.dirty_node.since_sequence, EXCLUDED.since_sequence),
             updated_at = clock_timestamp()"""
    )
    for k in sorted(skipped):
        bound, budget = skipped[k]
        prev_reason, prev_bound = existing.get(k, (None, {}))
        if prev_reason is not None and not prev_reason.startswith(SKIP):
            continue  # already dirty: a refresh is pending anyway
        acc = (_dec(prev_bound.get("value")) or Decimal(0)) + bound
        skips = int(prev_bound.get("skips", 0)) + 1
        doc = {"kind": "materiality", "metric": "declared_change_bound", "value": str(acc), "budget": str(budget),
               "skips": skips}
        if acc > budget:
            conn.execute(upsert, {"k": k, "s": seq, "b": json.dumps({**doc, "exhausted": True}), "r": EXHAUSTED})
            report.forced.append(k)
        else:
            conn.execute(upsert, {"k": k, "s": seq, "b": json.dumps(doc), "r": SKIP})
            report.skipped.append(k)
    for k in sorted(dirty):
        prev_reason, prev_bound = existing.get(k, (None, {}))
        bound_doc = prev_bound if prev_bound else {"kind": "none", "value": "0"}
        conn.execute(upsert, {"k": k, "s": seq, "b": json.dumps(bound_doc), "r": dirty[k]})
        report.dirty.append(k)


def process_outbox(engine: Engine, *, batch: int = 200, environment: str = "local",
                   actor: str = "worker:invalidation") -> InvalidationReport:
    """Process one batch of unprocessed outbox events in a single transaction."""
    from reva.operations.jobs import dirty_belief_nodes, enqueue_publish_beliefs, latest_observation_event

    report = InvalidationReport()
    with engine.begin() as conn:
        events = conn.execute(
            text("SELECT id, event_type, payload, created_at FROM ops.outbox WHERE processed_at IS NULL "
                 "ORDER BY id LIMIT :n FOR UPDATE SKIP LOCKED"),
            {"n": batch},
        ).all()
        if not events:
            return report
        report.events = len(events)
        release_cutoffs: dict[str, datetime] = {
            o: c for o, c in conn.execute(
                text("SELECT r.origin, max(r.knowledge_cutoff) FROM belief.release r "
                     "JOIN ops.publication p ON p.release_id = r.id GROUP BY r.origin")
            ).all()
        }
        origins_seen: dict[str, int] = {}
        for ev in events:
            if ev.event_type != OBS_EVENT:
                continue
            report.observation_events += 1
            payload = dict(ev.payload)
            origin = None
            if payload.get("acquisition_id"):
                origin = conn.execute(text("SELECT origin FROM evidence.acquisition WHERE id = :a"),
                                      {"a": payload["acquisition_id"]}).scalar_one_or_none()
            seeds = _event_seeds(payload)
            if not seeds:
                continue
            bounds = {k: d for k, v in (payload.get("change_bounds") or {}).items()
                      if (d := _dec(v)) is not None}
            dirty, skipped, comps = _propagate(seeds, _reachable_edges(conn, seeds), bounds)
            covered = release_cutoffs.get(origin) if origin else None
            if covered is not None and covered >= ev.created_at:
                dirty = {k: r for k, r in dirty.items() if not k.startswith("belief:")}
                skipped = {k: v for k, v in skipped.items() if not k.startswith("belief:")}
            _apply(conn, int(ev.id), dirty, skipped, report)
            report.coupled_components.extend(comps)
            if origin:
                origins_seen[origin] = max(origins_seen.get(origin, 0), int(ev.id))
        conn.execute(text("UPDATE ops.outbox SET processed_at = clock_timestamp() WHERE id = ANY(:ids)"),
                     {"ids": [int(e.id) for e in events]})
        if origins_seen and dirty_belief_nodes(conn) > 0:
            # Same key derivation as the ingest handler (origin + newest observation
            # event) so both paths usually coincide; any residual duplicate is a
            # no-op because publication clears the dirty belief nodes.
            through = latest_observation_event(conn)
            for origin in sorted(origins_seen):
                if environment == "production" and origin != "live":
                    continue
                jid = enqueue_publish_beliefs(conn, origin, through, created_by=actor)
                report.publish_jobs.append(str(jid))
    report.dirty = sorted(set(report.dirty))
    if report.events:
        log.info("outbox: %d events, %d dirty, %d skipped, %d forced, %d publish jobs", report.events,
                 len(report.dirty), len(report.skipped), len(report.forced), len(report.publish_jobs))
    return report
