"""Pure helpers of the Wikidata causal harvest (agent CAUSWD): no network, no database.

* the six causal properties and their direction (which side is the cause);
* seeds: event QIDs found in recorded snapshots, explicit ``VALUES ?item`` lists of query specs and KG rows;
* causal-statement enrichment: the generic harvester keeps a compact allow-list of qualifiers/references;
  for *causal* statements the full reference snaks (quote, DOI, ISBN, pages, title …) and causality-relevant
  qualifiers are kept, because the causal tiers (T2 scholarly reference / T3 referenced / T4 unreferenced)
  and verbatim mechanisms depend on them;
* frontier prioritisation (one hop along causal properties, bounded);
* the consistency analysis used by the report (temporal precedence with date precision, cycles, chains).

REVA asserts nothing here: an edge is "Wikidata asserts that <cause> → <effect>" (statement id + revision).
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reva.adapters.connectors.wikidata import (
    AllowList,
    WdEntity,
    WdStatement,
    _compact_snak_value,
)

#: pid -> (relation family, cause side). ``object``: the object is the cause (``has cause`` style);
#: ``subject``: the subject is the cause (``has effect`` / ``… of`` style).
CAUSAL_PROPERTIES: dict[str, tuple[str, str]] = {
    "P828": ("has_cause", "object"),
    "P1542": ("has_effect", "subject"),
    "P1478": ("immediate_cause", "object"),
    "P1479": ("contributing_factor", "object"),
    "P1536": ("immediate_cause", "subject"),
    "P1537": ("contributing_factor", "subject"),
}
CAUSAL_PIDS = frozenset(CAUSAL_PROPERTIES)
#: Properties kept on harvested entities (causal + what a consistency check and the event view need).
#: Kept small on purpose: the full allow-list would load ~100 statements per event (disk budget).
CONTEXT_PIDS = (
    "P31",
    "P279",
    "P361",
    "P527",
    "P155",
    "P156",
    "P580",
    "P582",
    "P585",
    "P571",
    "P576",
    "P577",
    "P17",
    "P276",
    "P710",
    "P793",
    "P1344",
    "P607",
)
HARVEST_PIDS = tuple(sorted(CAUSAL_PIDS)) + CONTEXT_PIDS
#: Reference snaks kept on causal statements (in addition to the global P248/P854/P813/P143).
CAUSAL_REFERENCE_PIDS = frozenset(
    {
        "P248",
        "P854",
        "P813",
        "P143",  # stated in, URL, retrieved, imported from (global allow-list)
        "P1683",
        "P304",
        "P792",
        "P478",
        "P433",
        "P1476",
        "P50",
        "P2093",
        "P577",
        "P407",  # quote, pages, work
        "P356",
        "P212",
        "P957",
        "P698",
        "P932",
        "P888",
        "P1065",
        "P2960",  # DOI, ISBN-13/10, PMID, PMCID, JSTOR, archive
        "P4656",
        "P887",
        "P3452",
        "P5997",  # Wikimedia import URL, based on heuristic, inferred from, stated in reference as
    }
)
#: Qualifiers kept on causal statements (in addition to the global qualifier allow-list).
CAUSAL_QUALIFIER_PIDS = frozenset(
    {
        "P1683",
        "P887",
        "P3452",
        "P1013",
        "P1932",
        "P5102",
        "P1480",
        "P1310",
        "P3680",
        "P7452",
        "P805",
        "P518",
        "P276",
        "P17",
        "P3831",
        "P2868",
        "P1545",
    }
)
WD_IMPORT_PIDS = frozenset({"P143", "P4656", "P887", "P3452"})  # "imported from Wikipedia" style, not a source
SCHOLARLY_PIDS = frozenset({"P356", "P212", "P957", "P698", "P932", "P888"})
QID = re.compile(r"\bwd:(Q[1-9][0-9]*)\b")


@dataclass(frozen=True)
class CausalEdge:
    cause: str
    effect: str
    relation: str
    pid: str
    statement_id: str | None
    subject: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.cause, self.effect)


def causal_edge(subject: str, pid: str, obj: str | None, statement_id: str | None = None) -> CausalEdge | None:
    """The asserted direction of one causal statement (``None`` for non-causal or non-entity objects)."""
    if pid not in CAUSAL_PROPERTIES or not obj or not obj.startswith("Q"):
        return None
    relation, cause_side = CAUSAL_PROPERTIES[pid]
    cause, effect = (obj, subject) if cause_side == "object" else (subject, obj)
    return CausalEdge(cause, effect, relation, pid, statement_id, subject)


# --------------------------------------------------------------------------- reference tiers
def reference_class(refs: Sequence[Mapping[str, Any]], *, wdqs_row: bool = False) -> str:
    """Coarse class of a statement's references, for reporting (CAUSCORE's grader is authoritative).

    ``unknown`` (WDQS row: references not read), ``none``, ``wikimedia_import_only`` (only "imported from" /
    heuristic snaks: a pointer to another wiki, not a source), ``scholarly`` (DOI/ISBN/PMID/… present),
    ``referenced`` (any other source: stated in / URL / title)."""
    if wdqs_row:
        return "unknown"
    if not refs:
        return "none"
    keys = set().union(*(set(r) for r in refs))
    if keys & SCHOLARLY_PIDS:
        return "scholarly"
    if keys <= WD_IMPORT_PIDS | {"P813"}:
        return "wikimedia_import_only"
    return "referenced"


# --------------------------------------------------------------------------- enrichment
def enrich_causal_statements(entity: WdEntity, raw: Mapping[str, Any], allow: AllowList) -> WdEntity:
    """Replace the references/qualifiers of the entity's causal statements by the extended causal sets.

    Non-causal statements are left exactly as the generic extractor produced them."""
    claims = raw.get("claims") or {}
    by_id: dict[str, Mapping[str, Any]] = {}
    for pid in CAUSAL_PIDS:
        for c in claims.get(pid) or []:
            if c.get("id"):
                by_id[str(c["id"])] = c
    if not by_id:
        return entity
    ref_keep = CAUSAL_REFERENCE_PIDS | allow.references
    qual_keep = (CAUSAL_QUALIFIER_PIDS | allow.qualifiers) - {"P580", "P582", "P585"}
    out: list[WdStatement] = []
    for s in entity.statements:
        c = by_id.get(s.statement_id or "") if s.property in CAUSAL_PIDS else None
        if c is None:
            out.append(s)
            continue
        refs: list[dict[str, list[Any]]] = []
        for ref in c.get("references") or []:
            compact = {
                p: [_compact_snak_value(x) for x in snaks]
                for p, snaks in sorted((ref.get("snaks") or {}).items())
                if p in ref_keep
            }
            if compact:
                refs.append(compact)
        quals = {
            p: [_compact_snak_value(x) for x in snaks]
            for p, snaks in sorted((c.get("qualifiers") or {}).items())
            if p in qual_keep
        }
        out.append(dataclasses.replace(s, references=refs, qualifiers=quals))
    return dataclasses.replace(entity, statements=tuple(out))


def causal_objects(entity: WdEntity) -> list[str]:
    """QIDs on the other side of the entity's causal statements (the one-hop frontier)."""
    out: dict[str, None] = {}
    for s in entity.statements:
        if s.property in CAUSAL_PIDS and s.object_qid and s.object_qid.startswith("Q"):
            out.setdefault(s.object_qid)
    return list(out)


# --------------------------------------------------------------------------- seeds
def seeds_from_recorded(paths: Iterable[Path]) -> dict[str, str]:
    """QID -> reason, from ``registry/kg/queries/*/_recorded/snapshot_claims_*.json`` files.

    Every recorded entity carrying a causal or event-context claim (P793/P1344/P607) is a seed, and so is
    every object of such a claim."""
    out: dict[str, str] = {}
    for p in paths:
        try:
            doc = json.loads(Path(p).read_text())
        except (OSError, ValueError):
            continue
        ents = doc.get("entities") if isinstance(doc, dict) else None
        if not isinstance(ents, dict):
            continue
        for q, e in ents.items():
            for c in (e or {}).get("claims") or []:
                pid, obj = c.get("p"), c.get("o")
                if pid in CAUSAL_PIDS or pid in ("P793", "P1344", "P607"):
                    if pid in CAUSAL_PIDS:
                        out.setdefault(q, f"recorded:{Path(p).name}:causal_subject")
                    if isinstance(obj, str) and re.fullmatch(r"Q[1-9][0-9]*", obj):
                        out.setdefault(obj, f"recorded:{Path(p).name}:{pid}")
    return out


def seeds_from_specs(spec_files: Iterable[Path]) -> dict[str, str]:
    """QIDs listed explicitly as items (``VALUES ?item { wd:Q… }``) in query specs (classes are not seeds)."""
    out: dict[str, str] = {}
    pat = re.compile(r"VALUES\s+\?item\s*\{([^}]*)\}", re.S)
    for f in spec_files:
        try:
            text = Path(f).read_text()
        except OSError:
            continue
        for block in pat.findall(text):
            for q in QID.findall(block):
                out.setdefault(q, f"spec:{Path(f).parent.name}/{Path(f).name}")
    return out


def prioritise(frontier: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Order unfetched QIDs: by tier, then causal in-degree, the best sitelink count of an entity pointing at
    them, the search rank (incoming links) for discovered items, then numerically (older items first)."""

    def key(q: str) -> tuple[int, int, int, int, int]:
        f = frontier[q]
        return (
            int(f.get("tier", 9)),
            -int(f.get("indegree", 0)),
            -int(f.get("via_sitelinks", 0)),
            int(f.get("rank") or 0),
            int(q[1:]),
        )

    return sorted(frontier, key=key)


# --------------------------------------------------------------------------- time
YMD = tuple[int, int, int]


def time_bounds(time: str | None, precision: int | None) -> tuple[YMD, YMD] | None:
    """[lo, hi] (year, month, day) covered by a Wikibase time at its precision. Years are astronomical
    (BCE negative) and kept as integers, so BCE precedence is compared exactly; the calendar model is ignored
    (Julian/Gregorian differ by days, below the precision of almost every causal check)."""
    if not time or precision is None:
        return None
    m = re.match(r"^([+-]?)(\d+)-(\d\d)-(\d\d)", time)
    if not m:
        return None
    year = int(m.group(2)) * (-1 if m.group(1) == "-" else 1)
    month, day = min(12, max(1, int(m.group(3)))), max(1, int(m.group(4)))
    if precision >= 11:
        return (year, month, day), (year, month, day)
    if precision == 10:
        return (year, month, 1), (year, month, 31)
    span = {9: 1, 8: 10, 7: 100, 6: 1000, 5: 10_000, 4: 100_000, 3: 1_000_000}.get(precision)
    if span is None:
        return None
    if span == 1:
        start = year
    elif precision == 8:
        start = year - year % 10
    else:  # century/millennium as Wikibase renders it: 1901..2000 = 20th century (year 2000 or 1901 stored)
        start = ((year - 1) // span) * span + 1
    return (start, 1, 1), (start + span - 1, 12, 31)


def temporal_check(cause_start: tuple[YMD, YMD] | None, effect_start: tuple[YMD, YMD] | None) -> str:
    """``consistent`` (cause surely starts no later than the effect), ``inconsistent`` (cause surely starts
    after the effect), ``overlapping`` (precision does not decide), ``undetermined`` (a start is missing)."""
    if cause_start is None or effect_start is None:
        return "undetermined"
    if cause_start[1] <= effect_start[0]:
        return "consistent"
    if cause_start[0] > effect_start[1]:
        return "inconsistent"
    return "overlapping"


# --------------------------------------------------------------------------- graph
def strongly_connected(nodes: Iterable[str], succ: Mapping[str, Iterable[str]]) -> list[list[str]]:
    """Tarjan's SCCs (iterative); only components with a cycle (size > 1) are returned."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    counter = 0
    for root in nodes:
        if root in index:
            continue
        work: list[tuple[str, Any]] = [(root, iter(succ.get(root, ())))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(succ.get(w, ()))))
                    advanced = True
                    break
                if w in on:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) > 1:
                    out.append(sorted(comp, key=lambda q: int(q[1:]) if q[1:].isdigit() else 0))
    return out


def chains(start: str, succ: Mapping[str, Sequence[str]], *, max_hops: int = 6, limit: int = 50) -> list[list[str]]:
    """Simple paths from ``start`` along cause→effect edges (2..max_hops hops), longest first, bounded."""
    out: list[list[str]] = []
    stack: list[list[str]] = [[start]]
    while stack and len(out) < 10 * limit:
        path = stack.pop()
        nxt = [w for w in succ.get(path[-1], ()) if w not in path]
        if len(path) >= 3 and (not nxt or len(path) - 1 >= max_hops):
            out.append(path)
        if len(path) - 1 < max_hops:
            stack.extend([*path, w] for w in nxt)
    out.sort(key=lambda p: (-len(p), p))
    return out[:limit]
