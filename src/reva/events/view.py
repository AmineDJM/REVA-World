"""Read-only event view over the knowledge graph (V3 causality, agent EVENTS).

An *event* is a KG entity that the taxonomy (``registry/events/taxonomy.yaml``) recognises as one:

* a Wikidata item whose ``P31`` claims — **all non-deprecated ranks**, never the truthy subset — reach a taxonomy
  class through ``P279`` chains taken from recorded data: the KG's own current ``P279`` statements first and, for
  class items whose ``P279`` statements are not in the KG, the recorded verification lookups
  (``registry/events/_recorded/wikidata_verification.json``). Every type carries the chain that produced it;
* a dataset record that is an event by construction of its dataset (UCDP conflict, COW war, UN peace operation,
  UNTC treaty — ``dataset_kinds`` in the taxonomy);
* a *derived* status change of a coded dataset (CShapes: a dependency that becomes independent), clearly labelled
  as derived by REVA from coded periods, never as a source-reported event record.

The view returns, for one event, its types, time interval with precision (``P585``, ``P580``/``P582``,
``P571``/``P576`` and dataset dates), places, participants with roles and sides, structure (``P361``/``P527``,
``P155``/``P156``, ``P793``, both directions), source-asserted causal statements (attributed, never REVA's) and
evidence-based ``same_as`` links. Every item cites its statement (source, statement key, revision, raw sha256).
All queries are bounded and say when they are truncated. Nothing here is written to the database.

Honesty: participation, location, sequence (follows) and part-of are *context*, not causation; the view never
words them causally. Causal statements are listed as "asserted by <source>" with a pointer to the causal claims
service, which grades them.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from sqlalchemy import Connection, text

from reva.events.taxonomy import (
    RecordedClasses,
    Taxonomy,
    all_type_ids,
    load_taxonomy,
    recorded_classes,
)
from reva.kernel.errors import ValidationProblem
from reva.kg import query as kq
from reva.kg.model import CAUSAL_PIDS, QID_RE

VIEW_VERSION = "reva.events.view/1"
MAX_CLASS_DEPTH = 8
MAX_CLASS_NODES = 600
MAX_DESCENDANT_CLASSES = 20_000
MAX_STATEMENTS_PER_FAMILY = 2_000
MAX_PARTICIPANTS = 500
MAX_STATEMENTS_PER_ITEM = 10
MAX_LIST = 200
SAME_AS_PID = "events:same_as"
CSHAPES_PREFIX = "cshapes:independence:"
CONTEXT_NOTE = (
    "Participation, place, time, part-of and sequence (follows / followed by) are context, not causation. "
    "Causal statements are listed only as asserted by their source; REVA asserts no cause."
)
CAUSAL_NOTE = (
    "Causal statements asserted by the source named on each item (not established by REVA). Graded causal "
    "claims: /v1/causal/events/{id}."
)

_ID_RE = QID_RE
_CSHAPES_ID_RE = re.compile(r"^cshapes:independence:([0-9]{1,4}):(-?[0-9]{4}-[0-9]{2}-[0-9]{2})$")


def _check_id(event_id: str) -> str:
    if not isinstance(event_id, str) or not _ID_RE.match(event_id):
        raise ValidationProblem("invalid event id (Wikidata Q-id or provider-scoped key)", id=str(event_id)[:64])
    return event_id


# --------------------------------------------------------------------------------------------- statements
_ST_SELECT = kq._ST_SELECT  # statement columns with BCE-safe date rendering (KGCORE)


def _rows(
    conn: Connection, where: str, params: Mapping[str, Any], origin: str | None, limit: int
) -> list[dict[str, Any]]:
    sql = (
        f"SELECT {_ST_SELECT} FROM kg.statement_current s WHERE {where}{kq._origin_sql(origin)} "
        f"ORDER BY s.id LIMIT :lim"
    )
    return [dict(r) for r in conn.execute(text(sql), {**params, "origin": origin, "lim": limit}).mappings()]


def _prov(r: Mapping[str, Any]) -> dict[str, Any]:
    verification, derivation, _method = kq.verification_of(r["source_key"], r.get("qualifiers"))
    return {
        "statement_key": r["statement_key"],
        "property": r["property_pid"],
        "source_key": r["source_key"],
        "source_revision": r["source_revision"],
        "raw_sha256": r["raw_sha256"],
        "rank": r["rank"],
        "origin": r["origin"],
        "verification": verification,
        "derivation": derivation,
    }


def _tval(r: Mapping[str, Any]) -> dict[str, Any] | None:
    """The time literal of a time-valued statement: source string, precision and derived inclusive bounds."""
    v = r.get("value") or {}
    if not isinstance(v, Mapping) or v.get("type") != "time":
        return None
    return {
        "time": v.get("time"),
        "precision": v.get("precision"),
        "calendar": v.get("calendar"),
        "lo": kq.astro_iso(r.get("value_time_lo")),
        "hi": kq.astro_iso(r.get("value_time_hi")),
        "_lo": r.get("value_time_lo"),
        "_hi": r.get("value_time_hi"),
    }


def _key(pg: str | None, *, high: bool = False) -> tuple[int, int, int]:
    if pg is None:
        return (10**9, 0, 0) if not high else (-(10**9), 0, 0)
    return kq.date_key(pg)


# --------------------------------------------------------------------------------------------- classes
@dataclass
class ClassGraph:
    """Upward ``P279`` edges reachable from some classes (KG statements first, recorded lookups as fallback)."""

    parents: dict[str, list[dict[str, Any]]]
    truncated: bool


def class_graph(
    conn: Connection, start: Iterable[str], *, origin: str | None = None, recorded: RecordedClasses | None = None
) -> ClassGraph:
    rec = recorded if recorded is not None else recorded_classes()
    parents: dict[str, list[dict[str, Any]]] = {}
    frontier = sorted({c for c in start if c})
    seen = set(frontier)
    truncated = False
    for _ in range(MAX_CLASS_DEPTH):
        if not frontier:
            break
        kg_rows = conn.execute(
            text(f"""SELECT s.subject_qid, s.object_qid, s.statement_key, s.rank, s.source_key
                       FROM kg.statement_current s
                      WHERE s.property_pid = 'P279' AND s.subject_qid = ANY(:f) AND s.object_qid IS NOT NULL
                            {kq._origin_sql(origin)}"""),
            {"f": frontier, "origin": origin},
        ).all()
        in_kg: set[str] = set()
        for sub, obj, key, rank, src in kg_rows:
            in_kg.add(sub)
            parents.setdefault(sub, []).append(
                {"parent": obj, "rank": rank, "via": "kg", "statement_key": key, "source_key": src}
            )
        for c in frontier:
            if c in in_kg:
                continue
            for p, rank, sid in rec.parents.get(c, ()):
                m = rec.meta.get(c, {})
                parents.setdefault(c, []).append(
                    {
                        "parent": p,
                        "rank": rank,
                        "via": "recorded",
                        "statement_id": sid,
                        "lastrevid": m.get("lastrevid"),
                        "raw_sha256": m.get("raw_sha256"),
                    }
                )
        nxt: list[str] = []
        for c in frontier:
            for e in parents.get(c, []):
                if e["parent"] not in seen:
                    seen.add(e["parent"])
                    nxt.append(e["parent"])
        if len(seen) > MAX_CLASS_NODES:
            truncated = True
            nxt = nxt[: max(0, MAX_CLASS_NODES - (len(seen) - len(nxt)))]
        frontier = sorted(nxt)
    else:
        truncated = truncated or bool(frontier)
    return ClassGraph(parents, truncated)


def _chains(graph: ClassGraph, start: str, targets: Mapping[str, Sequence[str]]) -> dict[str, list[dict[str, Any]]]:
    """Shortest P279 chain from ``start`` to the nearest taxonomy classes (BFS). A path stops at the first class
    the taxonomy maps: the most specific mapping on each path decides (a military expedition is not also typed
    as an expedition because Wikidata makes it a subclass of both)."""
    out: dict[str, list[dict[str, Any]]] = {}
    prev: dict[str, tuple[str, dict[str, Any]] | None] = {start: None}
    q: deque[str] = deque([start])
    while q:
        c = q.popleft()
        if c in targets and c not in out:
            chain: list[dict[str, Any]] = []
            cur = c
            while prev[cur] is not None:
                p, edge = prev[cur]  # type: ignore[misc]
                chain.append({"class": p, "subclass_of": cur, **{k: v for k, v in edge.items() if k != "parent"}})
                cur = p
            out[c] = list(reversed(chain))
            continue
        for e in graph.parents.get(c, []):
            if e["parent"] not in prev:
                prev[e["parent"]] = (c, e)
                q.append(e["parent"])
    return out


def _dataset_kind(conn: Connection, event_id: str, tax: Taxonomy, origin: str | None) -> Any:
    """The dataset kind of a record: by id prefix, else by a marker statement of the kind's source."""
    kind = tax.dataset_kind_for(event_id)
    if kind is not None:
        return kind
    for d in tax.marker_kinds():
        hit = conn.execute(
            text(
                f"""SELECT 1 FROM kg.statement_current s WHERE s.subject_qid = :q AND s.property_pid = :p
                     AND (CAST(:sk AS text) IS NULL OR s.source_key = :sk){kq._origin_sql(origin)} LIMIT 1"""
            ),
            {"q": event_id, "p": d.marker_property, "sk": d.source_key, "origin": origin},
        ).first()
        if hit is not None:
            return d
    return None


def classify(
    conn: Connection,
    event_id: str,
    *,
    origin: str | None = None,
    tax: Taxonomy | None = None,
    recorded: RecordedClasses | None = None,
) -> dict[str, Any]:
    """Event types of an entity from its ``P31`` statements (every non-deprecated rank) and recorded ``P279``
    chains, or from its dataset kind. ``primary`` = most specific types reached from preferred-rank ``P31``
    statements when the entity has any, else from all of them; the others stay listed (``secondary``)."""
    tax = tax or load_taxonomy()
    p31 = _rows(
        conn, "s.subject_qid = :q AND s.property_pid = 'P31' AND s.object_qid IS NOT NULL", {"q": event_id}, origin, 200
    )
    graph = class_graph(conn, [r["object_qid"] for r in p31], origin=origin, recorded=recorded)
    matches: list[dict[str, Any]] = []
    for r in p31:
        for cls, chain in _chains(graph, r["object_qid"], tax.class_index).items():
            for type_id in tax.class_index[cls]:
                matches.append(
                    {
                        "type": type_id,
                        "p31_class": r["object_qid"],
                        "matched_class": cls,
                        "rank": r["rank"],
                        "chain": chain,
                        "chain_length": len(chain),
                        "statement": _prov(r),
                        "p31_valid_from": kq.astro_iso(r["valid_lo"]),
                        "p31_valid_to": kq.astro_iso(r["valid_hi"]),
                    }
                )
    basis = "wikidata_p31"
    kind = _dataset_kind(conn, event_id, tax, origin)
    if kind is not None:
        kind_type: str | None = kind.type
        coded = None
        excluded = False
        if kind.coded_type_property:
            vals = _rows(
                conn,
                "s.subject_qid = :q AND s.property_pid = :p",
                {"q": event_id, "p": kind.coded_type_property},
                origin,
                20,
            )
            for v in vals:
                code = str((v.get("value") or {}).get("value", ""))
                coded = code or coded
                if code in kind.coded_type_map and kind.coded_type_map[code] is None:
                    excluded = True  # the dataset codes this record as something other than an event
                    continue
                mapped = kind.coded_type_map.get(code, kind.type)
                matches.append(
                    {
                        "type": mapped,
                        "dataset_kind": kind.id_prefix or kind.marker_property,
                        "coded_type": code,
                        "rank": v["rank"],
                        "chain": [],
                        "chain_length": 0,
                        "statement": _prov(v),
                    }
                )
        if excluded and not any(m.get("dataset_kind") for m in matches):
            kind_type = None
        if kind_type is not None and not any(m.get("dataset_kind") for m in matches):
            matches.append(
                {
                    "type": kind_type,
                    "dataset_kind": kind.id_prefix or kind.marker_property,
                    "coded_type": coded,
                    "rank": "normal",
                    "chain": [],
                    "chain_length": 0,
                    "statement": None,
                }
            )
        if kind_type is not None:
            basis = "dataset_kind" if not p31 else "wikidata_p31+dataset_kind"
    preferred = [m for m in matches if m["rank"] == "preferred"]
    pool = preferred or matches
    primary = tax.most_specific({m["type"] for m in pool})
    secondary = [t for t in tax.most_specific({m["type"] for m in matches}) if t not in primary]
    p31_unmatched = sorted({r["object_qid"] for r in p31} - {m["p31_class"] for m in matches if "p31_class" in m})
    return {
        "is_event": bool(matches),
        "basis": basis if matches else None,
        "primary": [tax.type_ref(t) for t in primary],
        "secondary": [tax.type_ref(t) for t in secondary],
        "rank_rule": "preferred-rank P31 claims decide the primary type when present; all non-deprecated "
        "P31 claims are listed"
        if preferred
        else "no preferred-rank P31 claim: all non-deprecated P31 claims decide",
        "matches": sorted(matches, key=lambda m: (m["chain_length"], m["type"])),
        "p31_unmatched": p31_unmatched,
        "class_search_truncated": graph.truncated,
    }


def descendant_classes(
    conn: Connection,
    type_ids: Iterable[str],
    *,
    origin: str | None = None,
    tax: Taxonomy | None = None,
    recorded: RecordedClasses | None = None,
) -> tuple[set[str], bool]:
    """Every class whose recorded ``P279`` chain reaches a class of the given types (reverse closure, bounded)."""
    tax = tax or load_taxonomy()
    rec = recorded if recorded is not None else recorded_classes()
    rec_children = rec.children()
    type_ids = list(type_ids)
    allowed = set(type_ids)
    seeds = {q for t in type_ids for q in tax.types[t].wikidata_classes}
    seen = set(seeds)
    frontier = sorted(seeds)
    truncated = False
    for _ in range(MAX_CLASS_DEPTH):
        if not frontier:
            break
        kids = {
            r[0]
            for r in conn.execute(
                text(f"""SELECT DISTINCT s.subject_qid FROM kg.statement_current s
                      WHERE s.property_pid = 'P279' AND s.object_qid = ANY(:f){kq._origin_sql(origin)}"""),
                {"f": frontier, "origin": origin},
            ).all()
        }
        for c in frontier:
            kids.update(rec_children.get(c, ()))
        # consistent with classification: do not descend into a class the taxonomy maps to another type
        nxt = sorted(k for k in kids - seen if not (set(tax.class_index.get(k, ())) - allowed))
        seen.update(nxt)
        if len(seen) > MAX_DESCENDANT_CLASSES:
            truncated = True
            break
        frontier = nxt
    return seen, truncated


# --------------------------------------------------------------------------------------------- interval
def _interval(
    tax: Taxonomy,
    own: Sequence[Mapping[str, Any]],
    incoming_spells: Sequence[Mapping[str, Any]],
    derived_from: str | None = None,
) -> dict[str, Any]:
    time_props = tax.props("time", "out")
    vals: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in own:
        pr = time_props.get(r["property_pid"])
        tv = _tval(r)
        if pr is None or tv is None:
            continue
        vals[pr.role].append({**tv, "provenance": _prov(r)})

    def pick(role: str, *, latest: bool) -> dict[str, Any] | None:
        cands = vals.get(role) or []
        if not cands:
            return None
        pref = [c for c in cands if c["provenance"]["rank"] == "preferred"] or cands
        if latest:
            return max(pref, key=lambda c: _key(c["_hi"], high=True))
        return min(pref, key=lambda c: _key(c["_lo"]))

    start = pick("start", latest=False)
    end = pick("end", latest=True)
    pit = pick("point_in_time", latest=False)
    inception = pick("inception", latest=False)
    dissolved = pick("dissolved", latest=True)
    episode_end = pick("episode_end", latest=True)
    basis = "source_dates"
    # Same order as CAUSCORE's temporal check: start = P580, else P585, else P571; end = P582, else P585, else
    # P576 (then a dataset's last episode end).
    lo_src = start or pit or inception
    hi_src = end or pit or dissolved or episode_end
    lo = lo_src["_lo"] if lo_src else None
    hi = hi_src["_hi"] if hi_src else None
    if lo is None and hi is None and incoming_spells:
        los = [s["valid_lo"] for s in incoming_spells if s.get("valid_lo")]
        his = [s["valid_hi"] for s in incoming_spells if s.get("valid_hi")]
        lo = min(los, key=_key) if los else None
        hi = max(his, key=lambda x: _key(x, high=True)) if his else None
        basis = derived_from or "derived_from_participation_spells"
    if lo is None and hi is None:
        basis = "undated"
    consistency = "undetermined"
    if lo is not None and hi is not None:
        consistency = "consistent" if _key(lo) <= _key(hi, high=True) else "inconsistent"
    chosen = {id(x) for x in (start, end, pit, inception, dissolved, episode_end) if x is not None}
    alternatives = [
        {k: v for k, v in c.items() if not k.startswith("_")} | {"role": role}
        for role, cs in vals.items()
        for c in cs
        if id(c) not in chosen
    ]
    notes: list[str] = []
    if pit is not None and lo is not None and hi is not None and (start or end):
        if _key(pit["_hi"], high=True) < _key(lo) or _key(pit["_lo"]) > _key(hi, high=True):
            notes.append("point in time lies outside the start..end interval (kept as stated by the source)")
    if basis.startswith("derived"):
        notes.append(
            "interval derived by REVA from the participation spells the dataset codes (earliest start, "
            "latest end); the dataset gives no event-level dates"
        )
    if episode_end is not None and end is None:
        notes.append("end = last episode end coded by the dataset (definition in the statement qualifiers)")

    def clean(x: dict[str, Any] | None) -> dict[str, Any] | None:
        return None if x is None else {k: v for k, v in x.items() if not k.startswith("_")}

    return {
        "start": clean(start),
        "end": clean(end),
        "point_in_time": clean(pit),
        "inception": clean(inception),
        "dissolved": clean(dissolved),
        "episode_end": clean(episode_end),
        "earliest": kq.astro_iso(lo),
        "latest": kq.astro_iso(hi),
        "basis": basis,
        "consistency": consistency,
        "alternatives": alternatives,
        "conflicting_values": len(alternatives) > 0,
        "notes": notes,
        "_lo": lo,
        "_hi": hi,
    }


# --------------------------------------------------------------------------------------------- card
def _ref(qid: str | None, labels: Mapping[str, Mapping[str, str]], kinds: Mapping[str, str]) -> dict[str, Any] | None:
    if qid is None:
        return None
    lab = labels.get(qid, {})
    return {
        "id": qid,
        "label": lab.get("en") or lab.get("fr") or qid,
        "label_fr": lab.get("fr"),
        "kind": kinds.get(qid),
    }


def _qual_objects(q: Mapping[str, Any] | None, pids: Sequence[str]) -> list[str]:
    out: list[str] = []
    for pid in pids:
        for v in (q or {}).get(pid) or []:
            if isinstance(v, str) and _ID_RE.match(v):
                out.append(v)
            elif isinstance(v, Mapping) and isinstance(v.get("id"), str):
                out.append(v["id"])
    return out


def _side(q: Mapping[str, Any] | None) -> str | None:
    s = (q or {}).get("side")
    return None if s is None else str(s)


def event_card(
    conn: Connection,
    event_id: str,
    *,
    origin: str | None = None,
    tax: Taxonomy | None = None,
    recorded: RecordedClasses | None = None,
) -> dict[str, Any] | None:
    """Everything the view knows about one event, or ``None`` when the id is not an event (or unknown)."""
    _check_id(event_id)
    kq._check_origin(origin)
    tax = tax or load_taxonomy()
    if event_id.startswith(CSHAPES_PREFIX):
        return _cshapes_card(conn, event_id, origin=origin, tax=tax)
    cls = classify(conn, event_id, origin=origin, tax=tax, recorded=recorded)
    if not cls["is_event"]:
        return None
    kind = _dataset_kind(conn, event_id, tax, origin)
    out_pids = sorted({p.pid for p in tax.properties if p.direction == "out"} | CAUSAL_PIDS | {SAME_AS_PID})
    in_pids = sorted(
        {p.pid for p in tax.properties if p.direction == "in"}
        | {p.pid for p in tax.properties if p.family == "structure"}
        | CAUSAL_PIDS
        | {SAME_AS_PID}
    )
    own = _rows(
        conn,
        "s.subject_qid = :q AND s.property_pid = ANY(:p)",
        {"q": event_id, "p": out_pids},
        origin,
        MAX_STATEMENTS_PER_FAMILY + 1,
    )
    inc = _rows(
        conn,
        "s.object_qid = :q AND s.property_pid = ANY(:p)",
        {"q": event_id, "p": in_pids},
        origin,
        MAX_STATEMENTS_PER_FAMILY + 1,
    )
    truncated = len(own) > MAX_STATEMENTS_PER_FAMILY or len(inc) > MAX_STATEMENTS_PER_FAMILY
    own, inc = own[:MAX_STATEMENTS_PER_FAMILY], inc[:MAX_STATEMENTS_PER_FAMILY]
    spells = [r for r in inc if kind is not None and r["property_pid"] in kind.interval_from_incoming]
    interval = _interval(tax, own, spells)
    ids: set[str] = {event_id}
    for r in own:
        if r["object_qid"]:
            ids.add(r["object_qid"])
        ids.update(_qual_objects(r["qualifiers"], ("P2868", "P3831")))
    for r in inc:
        ids.add(r["subject_qid"])
        ids.update(_qual_objects(r["qualifiers"], ("P2868", "P3831")))
    labels = kq.labels_for(conn, ids)
    kinds = kq.kinds_for(conn, ids)

    # places
    place_props = tax.props("place", "out")
    places: list[dict[str, Any]] = []
    for r in own:
        pr = place_props.get(r["property_pid"])
        if pr is None:
            continue
        places.append(
            {
                "relation": pr.role,
                "place": _ref(r["object_qid"], labels, kinds),
                "value": None if r["object_qid"] else r["value"],
                "valid_from": kq.astro_iso(r["valid_lo"]),
                "valid_to": kq.astro_iso(r["valid_hi"]),
                "provenance": _prov(r),
            }
        )

    # participants (grouped per participant, role, side)
    groups: dict[tuple[str, str, str | None, str], dict[str, Any]] = {}
    for direction, rows in (("out", own), ("in", inc)):
        for r in rows:
            pr = tax.role_for("participant", r["property_pid"], direction, r["source_key"])
            if pr is None:
                continue
            who = r["object_qid"] if direction == "out" else r["subject_qid"]
            literal = None
            if who is None:
                literal = r["value"]
                who = "literal:" + str((r["value"] or {}).get("value", ""))[:100]
            side = _side(r["qualifiers"])
            g = groups.setdefault(
                (who, pr.role, side, r["property_pid"]),
                {
                    "participant": None if literal is not None else _ref(who, labels, kinds),
                    "value": literal,
                    "role": tax.role(pr.role),
                    "side": side,
                    "property": r["property_pid"],
                    "direction": direction,
                    "role_qualifiers": [],
                    "valid_from": None,
                    "valid_to": None,
                    "statement_count": 0,
                    "statements": [],
                    "_lo": None,
                    "_hi": None,
                },
            )
            g["statement_count"] += 1
            for q in _qual_objects(r["qualifiers"], ("P2868", "P3831")):
                ref = _ref(q, labels, kinds)
                if ref not in g["role_qualifiers"]:
                    g["role_qualifiers"].append(ref)
            if r["valid_lo"] and (g["_lo"] is None or _key(r["valid_lo"]) < _key(g["_lo"])):
                g["_lo"] = r["valid_lo"]
            if r["valid_hi"] and (g["_hi"] is None or _key(r["valid_hi"], high=True) > _key(g["_hi"], high=True)):
                g["_hi"] = r["valid_hi"]
            if r["pit_time"] and r["valid_lo"] and g["_lo"] is None:
                g["_lo"] = r["valid_lo"]
            if len(g["statements"]) < MAX_STATEMENTS_PER_ITEM:
                coded_q = {
                    k: v
                    for k, v in (r["qualifiers"] or {}).items()
                    if k not in ("P2868", "P3831", "side", "years") and not k.startswith("_")
                }
                g["statements"].append(
                    {
                        **_prov(r),
                        "point_in_time": kq._time_out(r["pit_time"], r["pit_precision"]),
                        "coded": coded_q or None,
                    }
                )
    participants = []
    for g in groups.values():
        g["valid_from"], g["valid_to"] = kq.astro_iso(g.pop("_lo")), kq.astro_iso(g.pop("_hi"))
        participants.append(g)
    participants.sort(key=lambda g: (g["role"]["id"], g["side"] or "", (g["participant"] or {}).get("label") or ""))
    participants_truncated = len(participants) > MAX_PARTICIPANTS
    participants = participants[:MAX_PARTICIPANTS]

    # structure (both directions; an inverse statement is cited as such)
    inverse_of = {
        "part_of": "has_part",
        "has_part": "part_of",
        "follows": "followed_by",
        "followed_by": "follows",
        "significant_event": "significant_event_of",
    }
    st_props = tax.props("structure")
    structure = []
    for direction, rows in (("out", own), ("in", inc)):
        for r in rows:
            pr = st_props.get(r["property_pid"])
            if pr is None:
                continue
            other = r["object_qid"] if direction == "out" else r["subject_qid"]
            if other is None or other == event_id:
                continue
            rel = pr.role if direction == "out" else inverse_of[pr.role]
            structure.append(
                {
                    "relation": rel,
                    "event": _ref(other, labels, kinds),
                    "direction": direction,
                    "stated_on": event_id if direction == "out" else other,
                    "valid_from": kq.astro_iso(r["valid_lo"]),
                    "valid_to": kq.astro_iso(r["valid_hi"]),
                    "provenance": _prov(r),
                }
            )

    # source-asserted causal statements (raw; CAUSCORE grades them)
    causal = []
    plabels = kq.property_info(conn, CAUSAL_PIDS)
    for direction, rows in (("out", own), ("in", inc)):
        for r in rows:
            if r["property_pid"] not in CAUSAL_PIDS:
                continue
            other = r["object_qid"] if direction == "out" else r["subject_qid"]
            pi = plabels.get(r["property_pid"], {})
            causal.append(
                {
                    "subject": _ref(r["subject_qid"], labels, kinds),
                    "object": _ref(r["object_qid"], labels, kinds),
                    "other": _ref(other, labels, kinds),
                    "direction": direction,
                    "property": r["property_pid"],
                    "property_label_en": pi.get("label_en"),
                    "property_label_fr": pi.get("label_fr"),
                    "asserted_by": r["source_key"],
                    "wording": f"asserted by {r['source_key']} (statement {r['statement_key']}), not established by REVA",
                    "provenance": _prov(r),
                }
            )

    # same_as (evidence-based identity links)
    same_as = []
    for direction, rows in (("out", own), ("in", inc)):
        for r in rows:
            if r["property_pid"] != SAME_AS_PID:
                continue
            other = r["object_qid"] if direction == "out" else r["subject_qid"]
            same_as.append(
                {
                    "event": _ref(other, labels, kinds),
                    "evidence": (r["qualifiers"] or {}).get("evidence"),
                    "provenance": _prov(r),
                }
            )

    # dataset-coded attributes, shown verbatim
    coded: list[dict[str, Any]] = []
    if kind is not None and kind.coded_properties:
        for r in _rows(
            conn,
            "s.subject_qid = :q AND s.property_pid = ANY(:p)",
            {"q": event_id, "p": list(kind.coded_properties)},
            origin,
            200,
        ):
            coded.append(
                {
                    "property": r["property_pid"],
                    "value": r["value"],
                    "object": _ref(r["object_qid"], labels, kinds),
                    "valid_from": kq.astro_iso(r["valid_lo"]),
                    "valid_to": kq.astro_iso(r["valid_hi"]),
                    "qualifiers": r["qualifiers"],
                    "provenance": _prov(r),
                }
            )

    ent = kq.entity(conn, event_id, origin=origin)
    lab = labels.get(event_id, {})
    sources = sorted(
        {p["provenance"]["source_key"] for p in places}
        | {s["source_key"] for g in participants for s in g["statements"]}
        | {m["statement"]["source_key"] for m in cls["matches"] if m.get("statement")}
    )
    interval.pop("_lo")
    interval.pop("_hi")
    return {
        "id": event_id,
        "label": lab.get("en") or lab.get("fr") or event_id,
        "labels": (ent or {}).get("labels") or lab,
        "descriptions": (ent or {}).get("descriptions") or {},
        "kind": (ent or {}).get("kind"),
        "dataset_kind": None
        if kind is None
        else {"id_prefix": kind.id_prefix, "source_key": kind.source_key, "label_en": kind.label_en, "note": kind.note},
        "types": cls,
        "interval": interval,
        "places": places,
        "participants": participants,
        "structure": structure,
        "causal_statements": causal,
        "same_as": same_as,
        "coded": coded,
        "sources": sources,
        "entity_provenance": (ent or {}).get("provenance"),
        "truncated": truncated or participants_truncated,
        "notes": [CONTEXT_NOTE] + ([CAUSAL_NOTE] if causal else []),
        "view_version": VIEW_VERSION,
        "taxonomy_sha256": tax.sha256,
    }


# --------------------------------------------------------------------------------------------- CShapes (derived)
def cshapes_independence_events(conn: Connection, *, origin: str | None = None) -> list[dict[str, Any]]:
    """Status changes coded by CShapes where a territory's polity period turns ``independent`` after a period
    with another status. Derived by REVA from the coded periods (method ``reva.events.cshapes_status_change/1``):
    CShapes codes periods, not events."""
    rows = _rows(
        conn, "s.property_pid = 'cshapes:polity_period' AND s.subject_qid LIKE 'cshapes:gw:%'", {}, origin, 20_000
    )
    ruled = _rows(conn, "s.property_pid = 'cshapes:ruled_by' AND s.subject_qid LIKE 'cshapes:gw:%'", {}, origin, 20_000)
    by_terr: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r["valid_lo"]:
            by_terr[r["subject_qid"]].append(r)
    rulers: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in ruled:
        rulers[r["subject_qid"]].append(r)
    out = []
    for terr, periods in by_terr.items():
        periods.sort(key=lambda r: _key(r["valid_lo"]))
        for prev, cur in pairwise(periods):
            ps = str((prev["value"] or {}).get("status"))
            cs = str((cur["value"] or {}).get("status"))
            if cs != "independent" or ps == "independent":
                continue
            date = kq.astro_iso(cur["valid_lo"])
            ruler = [r for r in rulers.get(terr, []) if r["valid_hi"] and r["valid_hi"] == prev["valid_hi"]]
            out.append(
                {
                    "id": f"{CSHAPES_PREFIX}{terr.rsplit(':', 1)[1]}:{date}",
                    "territory": terr,
                    "date": date,
                    "_lo": cur["valid_lo"],
                    "previous": prev,
                    "current": cur,
                    "ruler": ruler,
                    "previous_status": ps,
                    "name": (cur["value"] or {}).get("name"),
                }
            )
    out.sort(key=lambda e: (_key(e["_lo"]), e["id"]))
    return out


def _cshapes_card(conn: Connection, event_id: str, *, origin: str | None, tax: Taxonomy) -> dict[str, Any] | None:
    m = _CSHAPES_ID_RE.match(event_id)
    if not m:
        return None
    ev = next((e for e in cshapes_independence_events(conn, origin=origin) if e["id"] == event_id), None)
    if ev is None:
        return None
    spec = tax.derived.get("cshapes_independence") or {}
    iso = _rows(
        conn, "s.subject_qid = :q AND s.property_pid = 'geopol:corresponds_to_iso3'", {"q": ev["territory"]}, origin, 20
    )
    ids = {ev["territory"]} | {r["object_qid"] for r in ev["ruler"] if r["object_qid"]}
    labels = kq.labels_for(conn, ids)
    kinds = kq.kinds_for(conn, ids)
    cur, prev = ev["current"], ev["previous"]
    point = {
        "time": cur["start_time"],
        "precision": cur["start_precision"],
        "lo": ev["date"],
        "hi": ev["date"],
        "provenance": _prov(cur),
    }
    participants = [
        {
            "participant": _ref(r["object_qid"], labels, kinds),
            "value": None,
            "role": tax.role("former_ruler"),
            "side": None,
            "property": "cshapes:ruled_by",
            "direction": "out",
            "role_qualifiers": [],
            "valid_from": kq.astro_iso(r["valid_lo"]),
            "valid_to": kq.astro_iso(r["valid_hi"]),
            "statement_count": 1,
            "statements": [{**_prov(r), "point_in_time": None, "coded": r["qualifiers"]}],
        }
        for r in ev["ruler"]
    ]
    places = [
        {
            "relation": "location",
            "place": _ref(ev["territory"], labels, kinds),
            "value": None,
            "valid_from": kq.astro_iso(cur["valid_lo"]),
            "valid_to": kq.astro_iso(cur["valid_hi"]),
            "provenance": _prov(cur),
        }
    ]
    places += [
        {
            "relation": "country",
            "place": None,
            "value": r["value"],
            "valid_from": kq.astro_iso(r["valid_lo"]),
            "valid_to": kq.astro_iso(r["valid_hi"]),
            "provenance": _prov(r),
        }
        for r in iso
    ]
    type_id = spec.get("type", "independence")
    name = ev["name"] or ev["territory"]
    return {
        "id": event_id,
        "label": f"{name}: {spec.get('label_en', 'independence (CShapes status change)')}",
        "labels": {
            "en": f"{name}: independence (CShapes status change, {ev['date']})",
            "fr": f"{name} : indépendance (changement de statut CShapes, {ev['date']})",
        },
        "descriptions": {},
        "kind": "derived_event",
        "dataset_kind": {
            "id_prefix": CSHAPES_PREFIX,
            "source_key": cur["source_key"],
            "label_en": "CShapes status change (derived)",
            "note": spec.get("caveat", ""),
        },
        "types": {
            "is_event": True,
            "basis": "derived_status_change",
            "primary": [tax.type_ref(type_id)],
            "secondary": [],
            "rank_rule": "derived event: no P31",
            "matches": [],
            "p31_unmatched": [],
            "class_search_truncated": False,
        },
        "interval": {
            "start": None,
            "end": None,
            "point_in_time": point,
            "inception": None,
            "dissolved": None,
            "episode_end": None,
            "earliest": ev["date"],
            "latest": ev["date"],
            "basis": "derived_status_change",
            "consistency": "consistent",
            "alternatives": [],
            "conflicting_values": False,
            "notes": [spec.get("caveat", "")],
        },
        "places": places,
        "participants": participants,
        "structure": [],
        "causal_statements": [],
        "same_as": [],
        "coded": [
            {
                "property": "cshapes:polity_period",
                "value": prev["value"],
                "object": None,
                "valid_from": kq.astro_iso(prev["valid_lo"]),
                "valid_to": kq.astro_iso(prev["valid_hi"]),
                "qualifiers": prev["qualifiers"],
                "provenance": _prov(prev),
            },
            {
                "property": "cshapes:polity_period",
                "value": cur["value"],
                "object": None,
                "valid_from": kq.astro_iso(cur["valid_lo"]),
                "valid_to": kq.astro_iso(cur["valid_hi"]),
                "qualifiers": cur["qualifiers"],
                "provenance": _prov(cur),
            },
        ],
        "sources": [cur["source_key"]],
        "entity_provenance": None,
        "derived": {
            "method": spec.get("method", "reva.events.cshapes_status_change/1"),
            "from_statements": [prev["statement_key"], cur["statement_key"]],
            "caveat": spec.get("caveat", ""),
        },
        "truncated": False,
        "notes": [CONTEXT_NOTE, spec.get("caveat", "")],
        "view_version": VIEW_VERSION,
        "taxonomy_sha256": tax.sha256,
    }


# --------------------------------------------------------------------------------------------- listing
def _country_qid(country: str | None) -> str | None:
    if country is None:
        return None
    c = country.strip()
    if re.fullmatch(r"[A-Z]{3}", c):
        q = kq.country_qid_registry().get(c)
        if q is None:
            raise ValidationProblem("unknown ISO 3166-1 alpha-3 code (registry/entities/country_qids.json)", iso3=c)
        return q
    return _check_id(c)


def list_events(
    conn: Connection,
    *,
    types: Sequence[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    country: str | None = None,
    involving: bool = False,
    include_undated: bool = False,
    limit: int = 50,
    cursor: tuple[str | None, str] | None = None,
    origin: str | None = None,
    tax: Taxonomy | None = None,
    recorded: RecordedClasses | None = None,
) -> dict[str, Any]:
    """Events of the given types (and their sub-types), overlapping ``[start, end]`` (``YYYY[-MM[-DD]]``,
    astronomical years), located in ``country`` (Q-id or ISO3; with ``involving`` also events where it is a
    participant). Sorted by earliest date then id; keyset ``cursor`` = (PostgreSQL date literal | None, id)."""
    tax = tax or load_taxonomy()
    kq._check_origin(origin)
    if not 1 <= limit <= MAX_LIST:
        raise ValidationProblem(f"limit must be 1..{MAX_LIST}")
    try:
        type_ids = all_type_ids(tax, types)
    except ValueError as e:
        raise ValidationProblem(str(e)) from e
    w = kq.window(start, end, include_undated=include_undated)
    cq = _country_qid(country)
    classes, cls_trunc = descendant_classes(conn, type_ids, origin=origin, tax=tax, recorded=recorded)
    kinds = [d for d in tax.dataset_kinds if d.type in type_ids or set(d.coded_type_map.values()) & type_ids]
    time_pids = sorted(tax.props("time", "out"))
    spell_pids = sorted({p for d in kinds for p in d.interval_from_incoming})
    place_pids = sorted(tax.props("place", "out"))
    part_out = sorted(tax.pids("participant", "out"))
    part_in = sorted(tax.pids("participant", "in"))
    o = kq._origin_sql(origin, "x")
    prefix_sql = " OR ".join(f"e.qid LIKE :pfx{i}" for i, d in enumerate(kinds) if d.id_prefix) or "FALSE"
    markers = [d for d in kinds if d.marker_property]
    params: dict[str, Any] = {
        "classes": sorted(classes),
        "tp": time_pids,
        "sp": spell_pids,
        "origin": origin,
        "lim": limit + 1,
        **{f"pfx{i}": d.id_prefix.replace("_", r"\_") + "%" for i, d in enumerate(kinds) if d.id_prefix},
        **{f"mp{i}": d.marker_property for i, d in enumerate(markers)},
        **{f"ms{i}": d.source_key for i, d in enumerate(markers)},
        **{f"mx{i}": sorted(k for k, v in d.coded_type_map.items() if v is None) for i, d in enumerate(markers)},
    }
    for i, d in enumerate(markers):
        # coded values that land in the requested types (all non-excluded values when the default type does)
        params[f"mv{i}"] = sorted(k for k, v in d.coded_type_map.items() if v is not None and v in type_ids)
    marker_sql = (
        " OR ".join(
            f"(x.property_pid = :mp{i} AND (CAST(:ms{i} AS text) IS NULL OR x.source_key = :ms{i})"
            + (
                (
                    f" AND NOT coalesce(x.value->>'value', '') = ANY(:mx{i}))"
                    if d.type in type_ids
                    else f" AND coalesce(x.value->>'value', '') = ANY(:mv{i}))"
                )
                if d.coded_type_property == d.marker_property
                else ")"
            )
            for i, d in enumerate(markers)
        )
        or "FALSE"
    )
    filters = []
    if w is not None:
        overlap = "daterange(t.lo, t.hi, '[]') && daterange(CAST(:w_lo AS date), CAST(:w_hi AS date), '[]')"
        dated = f"(t.lo IS NOT NULL OR t.hi IS NOT NULL) AND {overlap}"
        filters.append(f"(({dated}) OR (t.lo IS NULL AND t.hi IS NULL))" if w.include_undated else f"({dated})")
        params.update(w_lo=w.lo, w_hi=w.hi)
    if cq is not None:
        params.update(country=cq, pp=place_pids, po=part_out, pi=part_in)
        cond = (
            "EXISTS (SELECT 1 FROM kg.statement_current x WHERE x.subject_qid = t.qid AND x.property_pid = "
            f"ANY(:pp) AND x.object_qid = :country{o})"
        )
        if involving:
            cond += (
                " OR EXISTS (SELECT 1 FROM kg.statement_current x WHERE x.subject_qid = t.qid AND "
                f"x.property_pid = ANY(:po) AND x.object_qid = :country{o})"
                " OR EXISTS (SELECT 1 FROM kg.statement_current x WHERE x.object_qid = t.qid AND "
                f"x.property_pid = ANY(:pi) AND x.subject_qid = :country{o})"
            )
        filters.append(f"({cond})")
    if cursor is not None:
        c_lo, c_id = cursor
        params["c_id"] = c_id
        if c_lo is None:
            filters.append("(t.lo IS NULL AND t.qid > :c_id)")
        else:
            params["c_lo"] = c_lo
            filters.append(
                "((t.lo > CAST(:c_lo AS date)) OR (t.lo = CAST(:c_lo AS date) AND t.qid > :c_id) OR t.lo IS NULL)"
            )
    where = (" WHERE " + " AND ".join(filters)) if filters else ""
    sql = f"""
      WITH cand AS (
        SELECT DISTINCT x.subject_qid AS qid FROM kg.statement_current x
         WHERE x.property_pid = 'P31' AND x.object_qid = ANY(:classes){o}
        UNION
        SELECT DISTINCT e.qid FROM kg.entity_current e WHERE ({prefix_sql}){kq._origin_sql(origin, "e")}
        UNION
        SELECT DISTINCT x.subject_qid FROM kg.statement_current x
         WHERE ({marker_sql}){o}
      ), tv AS (
        SELECT c.qid, coalesce(x.value_time_lo, x.valid_lo) AS lo, coalesce(x.value_time_hi, x.valid_hi) AS hi
          FROM cand c JOIN kg.statement_current x ON x.subject_qid = c.qid AND x.property_pid = ANY(:tp){o}
        UNION ALL
        SELECT c.qid, x.valid_lo, x.valid_hi
          FROM cand c JOIN kg.statement_current x ON x.object_qid = c.qid AND x.property_pid = ANY(:sp){o}
      ), t AS (
        SELECT c.qid, min(tv.lo) AS lo, max(tv.hi) AS hi FROM cand c LEFT JOIN tv ON tv.qid = c.qid GROUP BY c.qid
      )
      SELECT t.qid, {kq._date_sql("t.lo", "lo")}, {kq._date_sql("t.hi", "hi")} FROM t{where}
       ORDER BY t.lo NULLS LAST, t.qid LIMIT :lim"""
    rows = [dict(r) for r in conn.execute(text(sql), params).mappings()]
    # derived CShapes independence events
    if "independence" in type_ids and (tax.derived.get("cshapes_independence") or {}).get("enabled", True):
        for ev in cshapes_independence_events(conn, origin=origin):
            lo = ev["_lo"]
            if w is not None and ((w.lo and _key(lo) < _key(w.lo)) or (w.hi and _key(lo) > _key(w.hi))):
                continue
            if cq is not None and not _cshapes_in_country(conn, ev, cq, involving, origin):
                continue
            if cursor is not None and cursor[0] is not None and (_key(lo), ev["id"]) <= (_key(cursor[0]), cursor[1]):
                continue
            rows.append({"qid": ev["id"], "lo": lo, "hi": lo, "name": ev["name"]})
        rows.sort(key=lambda r: (r["lo"] is None, _key(r["lo"]) if r["lo"] else (0, 0, 0), r["qid"]))
    more = len(rows) > limit
    rows = rows[:limit]
    labels = kq.labels_for(conn, [r["qid"] for r in rows])
    items = []
    origins: set[str] = set()
    for r in rows:
        if r["qid"].startswith(CSHAPES_PREFIX):
            origins.update(
                x[0]
                for x in conn.execute(
                    text(f"""SELECT DISTINCT s.origin FROM kg.statement_current s WHERE s.property_pid =
                         'cshapes:polity_period'{kq._origin_sql(origin)} LIMIT 2"""),
                    {"origin": origin},
                ).all()
            )
            items.append(
                {
                    "id": r["qid"],
                    "label": f"{r.get('name') or r['qid']}: independence (CShapes status change)",
                    "types": [tax.type_ref("independence")],
                    "earliest": kq.astro_iso(r["lo"]),
                    "latest": kq.astro_iso(r["hi"]),
                    "basis": "derived_status_change",
                }
            )
            continue
        cls = classify(conn, r["qid"], origin=origin, tax=tax, recorded=recorded)
        if not cls["is_event"] or (types and not ({m["type"] for m in cls["matches"]} & type_ids)):
            # a dataset record coded as an outcome (not an event), or coded with a type outside the request;
            # a page may then hold fewer items than ``limit`` (the cursor still advances)
            continue
        origins.update(m["statement"]["origin"] for m in cls["matches"] if m.get("statement"))
        if not any(m.get("statement") for m in cls["matches"]):
            origins.update(
                x[0]
                for x in conn.execute(
                    text(
                        f"SELECT DISTINCT e.origin FROM kg.entity_current e WHERE e.qid = :q{kq._origin_sql(origin, 'e')}"
                    ),
                    {"q": r["qid"], "origin": origin},
                ).all()
            )
        lab = labels.get(r["qid"], {})
        items.append(
            {
                "id": r["qid"],
                "label": lab.get("en") or lab.get("fr") or r["qid"],
                "label_fr": lab.get("fr"),
                "types": cls["primary"],
                "earliest": kq.astro_iso(r["lo"]),
                "latest": kq.astro_iso(r["hi"]),
                "basis": cls["basis"],
            }
        )
    nxt = (rows[-1]["lo"], rows[-1]["qid"]) if more and rows else None
    return {
        "items": items,
        "next": nxt,
        "origins": sorted(origins),
        "types": sorted(type_ids),
        "classes_considered": len(classes),
        "class_closure_truncated": cls_trunc,
        "country": cq,
        "involving": involving,
        "window": None
        if w is None
        else {"from": kq.astro_iso(w.lo), "to": kq.astro_iso(w.hi), "include_undated": w.include_undated},
    }


def _cshapes_in_country(
    conn: Connection, ev: Mapping[str, Any], country_qid: str, involving: bool, origin: str | None
) -> bool:
    if involving and any(r["object_qid"] == country_qid for r in ev["ruler"]):
        return True
    iso = _rows(
        conn, "s.subject_qid = :q AND s.property_pid = 'geopol:corresponds_to_iso3'", {"q": ev["territory"]}, origin, 20
    )
    reg = kq.country_qid_registry()
    return any(reg.get(str((r["value"] or {}).get("value"))) == country_qid for r in iso)
