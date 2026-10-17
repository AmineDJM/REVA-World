"""Event identity for the grader and the chain search: clusters of records joined by evidenced ``same_as`` links.

``events:same_as`` statements (``reva.events.same_as``, rules ``registry/events/same_as_rules.yaml``) say that two KG
records denote the same real event, each with its evidence (a shared identifier, a provider-published crosswalk or an
evidenced mapping table) -- never name or date similarity. This module reads them and builds the connected
components ("identity clusters"):

* the grader counts claims on the same (cause cluster, effect cluster, family, stance) as asserting the same link,
  so two independent sources coding the same pair of events under their own ids corroborate each other;
* a link whose rule declares ``independent_sources: false`` (one record is derived from the other, e.g. EUSANCT
  cases taken over from TIES) gives both records the upstream key ``lineage:<link>``: claims on them count once;
* the chain search may cross a link as an *identity hop* (not a causal hop), shown with its evidence.

Pure functions plus one bounded read; nothing here writes.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection, text

SAME_AS_PID = "events:same_as"
#: Upper bound of same_as statements read per origin (the links store caps derivation at 50,000).
MAX_LINKS = 200_000
#: A cluster larger than this is not used for corroboration or traversal (reported): the one-per-source and
#: ambiguity guards keep real clusters at a handful of records, so a large one means chained links to review.
MAX_CLUSTER = 12


@dataclass(frozen=True)
class IdentityLink:
    left: str
    right: str
    statement_key: str
    rule: str
    independent_sources: bool | None
    evidence: Mapping[str, Any] = field(default_factory=dict, compare=False, hash=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "left": self.left,
            "right": self.right,
            "statement_key": self.statement_key,
            "rule": self.rule,
            "independent_sources": self.independent_sources,
            "evidence": dict(self.evidence),
        }


@dataclass
class Identity:
    """Identity clusters of one origin."""

    links: list[IdentityLink] = field(default_factory=list)
    canon_of: dict[str, str] = field(default_factory=dict)  # member -> canonical id (smallest member)
    members_of: dict[str, list[str]] = field(default_factory=dict)  # canonical -> sorted members
    lineage_of: dict[str, list[str]] = field(default_factory=dict)  # record -> lineage upstream keys
    oversized: list[list[str]] = field(default_factory=list)

    def canon(self, node: str) -> str:
        return self.canon_of.get(node, node)

    def members(self, node: str) -> list[str]:
        return self.members_of.get(self.canon(node), [node])

    def lineage(self, node: str) -> list[str]:
        return self.lineage_of.get(node, [])

    def links_of(self, node: str) -> list[IdentityLink]:
        return [x for x in self.links if node in (x.left, x.right)]

    def summary(self) -> dict[str, Any]:
        sizes: dict[int, int] = defaultdict(int)
        for m in self.members_of.values():
            sizes[len(m)] += 1
        return {
            "links": len(self.links),
            "clusters": len(self.members_of),
            "cluster_sizes": dict(sorted(sizes.items())),
            "oversized_clusters_ignored": len(self.oversized),
            "lineage_links": sum(1 for x in self.links if x.independent_sources is False),
        }


def build(links: Sequence[IdentityLink], *, max_cluster: int = MAX_CLUSTER) -> Identity:
    """Connected components of the links (union-find); clusters above ``max_cluster`` are left unresolved."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for lk in links:
        a, b = find(lk.left), find(lk.right)
        if a != b:
            parent[max(a, b)] = min(a, b)
    comps: dict[str, list[str]] = defaultdict(list)
    for x in list(parent):
        comps[find(x)].append(x)
    ident = Identity(links=sorted(links, key=lambda x: x.statement_key))
    for members in comps.values():
        ms = sorted(members)
        if len(ms) > max_cluster:
            ident.oversized.append(ms)
            continue
        c = ms[0]
        ident.members_of[c] = ms
        for m in ms:
            ident.canon_of[m] = c
    lineage: dict[str, set[str]] = defaultdict(set)
    for lk in links:
        if lk.independent_sources is False and lk.left in ident.canon_of:
            key = f"lineage:{lk.statement_key}"
            lineage[lk.left].add(key)
            lineage[lk.right].add(key)
    ident.lineage_of = {k: sorted(v) for k, v in lineage.items()}
    return ident


def _independent(q: Mapping[str, Any]) -> bool | None:
    ev = q.get("evidence") if isinstance(q.get("evidence"), Mapping) else {}
    v = q.get("independent_sources", ev.get("independent_sources") if ev else None)
    return None if v is None else bool(v)


def fetch_links(conn: Connection, *, origin: str | None, nodes: Iterable[str] | None = None) -> list[IdentityLink]:
    """Current ``events:same_as`` statements (all, or those touching ``nodes``)."""
    params: dict[str, Any] = {"p": SAME_AS_PID, "o": origin, "cap": MAX_LINKS}
    where = "s.property_pid = :p AND s.object_qid IS NOT NULL"
    if origin:
        where += " AND s.origin = :o"
    if nodes is not None:
        params["n"] = sorted(set(nodes))
        if not params["n"]:
            return []
        where += " AND (s.subject_qid = ANY(:n) OR s.object_qid = ANY(:n))"
    rows = conn.execute(
        text(f"""SELECT s.subject_qid, s.object_qid, s.statement_key, s.qualifiers
                   FROM kg.statement_current s WHERE {where} ORDER BY s.statement_key LIMIT :cap"""),
        params,
    ).all()
    out = []
    for subj, obj, key, q in rows:
        q = dict(q or {})
        out.append(IdentityLink(subj, obj, key, str(q.get("rule", "")), _independent(q), dict(q.get("evidence") or {})))
    return out


def load(conn: Connection, *, origin: str | None) -> Identity:
    return build(fetch_links(conn, origin=origin))
