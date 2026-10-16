"""Derivations: KG statements and atlas rows → :class:`~reva.links.model.Link` records.

Pure functions over in-memory inputs (no database, no network). Every derived link carries the
statements / atlas rows it rests on; a candidate without evidence is never produced (``Link`` refuses it).
Each derivation also returns what it did *not* derive and why (``Derivation.skipped``): disjoint
membership periods, hub organisations not expanded pairwise, a zero reported by a trade source, a row
without counterpart... so that "no link" is explainable and never confused with "no data".

Rules
-----
* No invented links: a link is derived only from the statements named in the taxonomy
  (``registry/links/link_types.yaml``).
* No zero for missing: a missing atlas value gives no link (reason ``missing_value``); a *reported* zero
  gives no link either (``reported_zero``) but is counted.
* Shared-* links (official language, membership, head of state, currency) hold over the *intersection* of
  the two statements' periods. Disjoint periods → no link. An undated statement gives an ``undated`` link
  (overlap not established by the source), flagged ``temporal_basis: undated``.
"""

from __future__ import annotations

import itertools
from collections import Counter, defaultdict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from reva.links.model import (
    AtlasRowRef,
    Evidence,
    Link,
    LinkError,
    NoEvidence,
    Stmt,
    Temporal,
    is_wikidata,
)

# --- classes (Wikidata) -------------------------------------------------------------------------------
COLONY_ROOTS: tuple[str, ...] = ("Q133156", "Q164142", "Q161243")  # colony, protectorate, dependent territory
POLITY_ROOTS: tuple[str, ...] = (
    "Q7275",  # state
    "Q3624078",  # sovereign state
    "Q6256",  # country
    "Q3024240",  # historical country
    "Q48349",  # empire
    "Q417175",  # kingdom
    "Q1250464",  # realm
    *COLONY_ROOTS,
)
ALLIANCE_ROOTS: tuple[str, ...] = ("Q1127126",)  # military alliance
#: Genealogy ancestors that say nothing ("language", "natural language"...).
NON_GENEALOGICAL: frozenset[str] = frozenset(
    {"Q34770", "Q315", "Q33742", "Q1288568", "Q20162172", "Q17376908", "Q25295", "Q951873", "Q2315359"}
)
SUCCESSION_FORWARD = ("P1365", "P155")  # S replaces/follows P: subject is the successor
SUCCESSION_BACKWARD = ("P1366", "P156")  # P replaced by/followed by S: object is the successor
CONFLICT_SIDE_QUALIFIERS = ("ucdp:side", "side")
MAX_SKIP_SAMPLES = 200


@dataclass
class Derivation:
    """Links of one type plus the accounting of what was not derived."""

    link_type: str
    links: list[Link] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)
    samples: list[dict[str, Any]] = field(default_factory=list)

    def skip(self, reason: str, **detail: Any) -> None:
        self.skipped[reason] += 1
        if len(self.samples) < MAX_SKIP_SAMPLES:
            self.samples.append({"reason": reason, **detail})

    def add(self, link: Link) -> None:
        self.links.append(link)

    def finish(self) -> Derivation:
        self.links = merge_links(self.links)
        return self

    def to_json(self) -> dict[str, Any]:
        return {
            "link_type": self.link_type,
            "count": len(self.links),
            "skipped": dict(self.skipped),
            "skipped_samples": self.samples[:20],
        }


def merge_links(links: Iterable[Link]) -> list[Link]:
    """De-duplicate links with the same identity (``Link.key``), uniting their evidence."""
    out: dict[tuple[Any, ...], Link] = {}
    for lk in links:
        k = lk.key
        if k not in out:
            out[k] = lk
            continue
        prev = out[k]
        ev = list(prev.evidence)
        seen = {(e.kind, e.statement_id, e.raw_sha256, e.selector) for e in ev}
        for e in lk.evidence:
            if (e.kind, e.statement_id, e.raw_sha256, e.selector) not in seen:
                ev.append(e)
                seen.add((e.kind, e.statement_id, e.raw_sha256, e.selector))
        out[k] = Link(
            prev.subject,
            prev.object,
            prev.link_type,
            prev.directed,
            prev.temporal,
            tuple(ev),
            prev.via,
            prev.weight,
            prev.unit,
            prev.basis,
        )
    return sorted(out.values(), key=lambda x: (x.link_type, x.subject, x.object, x.via, x.temporal.label()))


# ---------------------------------------------------------------------------------------------- KG index
class KgIndex:
    """In-memory index over statements (by property, subject, object) with class closures."""

    def __init__(self, statements: Iterable[Stmt]) -> None:
        self.statements: list[Stmt] = []
        self.by_prop: dict[str, list[Stmt]] = defaultdict(list)
        self.out: dict[tuple[str, str], list[Stmt]] = defaultdict(list)
        self.inc: dict[tuple[str, str], list[Stmt]] = defaultdict(list)
        seen: set[str] = set()
        for s in statements:
            if s.statement_id in seen:
                continue
            seen.add(s.statement_id)
            self.statements.append(s)
            self.by_prop[s.pid].append(s)
            self.out[(s.subject, s.pid)].append(s)
            if s.object is not None:
                self.inc[(s.object, s.pid)].append(s)
        self._closure_cache: dict[tuple[str, ...], dict[str, tuple[Stmt, ...]]] = {}

    def objects(self, subject: str, prop: str) -> list[Stmt]:
        return [s for s in self.out.get((subject, prop), ()) if s.object is not None]

    def subclass_closure(self, roots: Sequence[str], max_depth: int = 8) -> dict[str, tuple[Stmt, ...]]:
        """Every class C with ``C P279* root`` (root included, empty chain) → the P279 statements of the chain."""
        key = tuple(roots)
        if key in self._closure_cache:
            return self._closure_cache[key]
        chains: dict[str, tuple[Stmt, ...]] = {r: () for r in roots}
        q = deque((r, 0) for r in roots)
        while q:
            cls, depth = q.popleft()
            if depth >= max_depth:
                continue
            for s in self.inc.get((cls, "P279"), ()):
                if s.subject not in chains:
                    chains[s.subject] = (s, *chains[cls])
                    q.append((s.subject, depth + 1))
        self._closure_cache[key] = chains
        return chains

    def instances(self, roots: Sequence[str]) -> dict[str, tuple[Stmt, ...]]:
        """Every subject with a P31 to a class in the closure of ``roots`` → evidence (P31 + P279 chain)."""
        closure = self.subclass_closure(roots)
        out: dict[str, tuple[Stmt, ...]] = {}
        for cls, chain in closure.items():
            for s in self.inc.get((cls, "P31"), ()):
                if s.subject not in out:
                    out[s.subject] = (s, *chain)
        return out

    def entity_temporal(self, qid: str) -> tuple[Temporal, tuple[Stmt, ...]]:
        """The entity's own dates: P580/P582 (start/end time), P571/P576 (inception/dissolution) or P585
        (point in time), as main-value statements; ``undated`` when the KG has none."""

        def first(prop: str) -> Stmt | None:
            cands = [s for s in self.out.get((qid, prop), ()) if s.value_date is not None]
            cands.sort(key=lambda s: (s.rank != "preferred", s.value_date.key() if s.value_date else (0, 0, 0)))
            return cands[0] if cands else None

        for a, b in (("P580", "P582"), ("P571", "P576")):
            sa, sb = first(a), first(b)
            if sa or sb:
                start = sa.value_date if sa else None
                end = sb.value_date if sb else None
                try:
                    t = Temporal.of(start, end)
                except LinkError:
                    continue
                return t, tuple(x for x in (sa, sb) if x is not None)
        sp = first("P585")
        if sp is not None:
            return Temporal("point", point=sp.value_date), (sp,)
        return Temporal("undated"), ()


def _ev(*stmts: Stmt) -> tuple[Evidence, ...]:
    seen: set[str] = set()
    out = []
    for s in stmts:
        if s.statement_id not in seen:
            seen.add(s.statement_id)
            out.append(Evidence.of_stmt(s))
    return tuple(out)


def polities(index: KgIndex, extra: Iterable[str] = ()) -> set[str]:
    """Nodes treated as states/polities: instances of :data:`POLITY_ROOTS` classes + ``extra`` (e.g. the
    crosswalk countries' QIDs)."""
    return set(index.instances(POLITY_ROOTS)) | set(extra)


def _pairs_ok(a: str, b: str, focus: set[str] | None) -> bool:
    return a != b and (focus is None or a in focus or b in focus)


def _temporal_basis(t: Temporal) -> str:
    return "undated" if t.kind == "undated" else "intersection"


# ---------------------------------------------------------------------------------------------- KG derivations
def derive_symmetric(
    index: KgIndex,
    prop: str,
    link_type: str,
    nodes: set[str],
    *,
    focus: set[str] | None = None,
    sources: Callable[[str], bool] | None = None,
) -> Derivation:
    """P47 (shares border with) / P530 (diplomatic relation) between two nodes; both directions merge.
    ``sources``: keep only statements whose source key passes (``border_wikidata`` is Wikidata's P47 only;
    Natural Earth / GeoNames P47 loads are the ``land_border`` atlas kind)."""
    d = Derivation(link_type)
    for s in index.by_prop.get(prop, ()):
        if s.object is None:
            continue
        if sources is not None and not sources(s.source_key):
            d.skip("other_source", statement=s.statement_id, source=s.source_key)
            continue
        if s.subject not in nodes or s.object not in nodes:
            d.skip("endpoint_not_a_polity", statement=s.statement_id)
            continue
        if not _pairs_ok(s.subject, s.object, focus):
            continue
        a, b = sorted((s.subject, s.object))
        d.add(Link(a, b, link_type, False, s.temporal, _ev(s), basis={"property": prop}))
    return d.finish()


def derive_shared_object(
    index: KgIndex,
    prop: str,
    link_type: str,
    nodes: set[str],
    *,
    focus: set[str] | None = None,
    max_members: int | None = None,
    object_filter: Callable[[str], bool] | None = None,
) -> Derivation:
    """Two nodes whose ``prop`` statements point to the same object over overlapping periods
    (shared official language P37, language used P2936, currency P38, head of state P35, membership P463)."""
    d = Derivation(link_type)
    groups: dict[str, list[Stmt]] = defaultdict(list)
    for s in index.by_prop.get(prop, ()):
        if s.object is None or s.subject not in nodes:
            continue
        if object_filter is not None and not object_filter(s.object):
            continue
        groups[s.object].append(s)
    for obj, stmts in sorted(groups.items()):
        members = {s.subject for s in stmts}
        if len(members) < 2:
            continue
        if max_members is not None and len(members) > max_members:
            d.skip("hub_object_not_expanded", via=obj, members=len(members))
            continue
        for sa, sb in itertools.combinations(sorted(stmts, key=lambda s: (s.subject, s.statement_id)), 2):
            if not _pairs_ok(sa.subject, sb.subject, focus):
                continue
            ov = sa.temporal.overlap(sb.temporal)
            if ov is None:
                d.skip(
                    "disjoint_periods",
                    via=obj,
                    pair=[sa.subject, sb.subject],
                    periods=[sa.temporal.label(), sb.temporal.label()],
                )
                continue
            a, b = (sa, sb) if sa.subject < sb.subject else (sb, sa)
            d.add(
                Link(
                    a.subject,
                    b.subject,
                    link_type,
                    False,
                    ov,
                    _ev(a, b),
                    via=(obj,),
                    basis={"property": prop, "temporal_basis": _temporal_basis(ov)},
                )
            )
    return d.finish()


def derive_co_membership(
    index: KgIndex, nodes: set[str], *, focus: set[str] | None = None, max_members: int = 60
) -> tuple[Derivation, Derivation]:
    """``co_member`` and ``alliance_co_member`` from dated P463 memberships (overlapping interval)."""
    alliances = set(index.instances(ALLIANCE_ROOTS))
    co = derive_shared_object(
        index,
        "P463",
        "co_member",
        nodes,
        focus=focus,
        max_members=max_members,
        object_filter=lambda o: o not in alliances,
    )
    al = derive_shared_object(
        index,
        "P463",
        "alliance_co_member",
        nodes,
        focus=focus,
        max_members=max_members,
        object_filter=lambda o: o in alliances,
    )
    # the alliance classification is evidence too
    al.links = [
        Link(
            lk.subject,
            lk.object,
            lk.link_type,
            lk.directed,
            lk.temporal,
            lk.evidence + _ev(*index.instances(ALLIANCE_ROOTS)[lk.via[0]]),
            lk.via,
            basis=lk.basis,
        )
        for lk in al.links
    ]
    return co, al


def _ancestors(index: KgIndex, lang: str, max_depth: int) -> dict[str, tuple[int, tuple[Stmt, ...]]]:
    out: dict[str, tuple[int, tuple[Stmt, ...]]] = {lang: (0, ())}
    q = deque([lang])
    while q:
        cur = q.popleft()
        depth, chain = out[cur]
        if depth >= max_depth:
            continue
        for prop in ("P279", "P361"):
            for s in index.objects(cur, prop):
                assert s.object is not None
                if s.object in NON_GENEALOGICAL or s.object in out:
                    continue
                out[s.object] = (depth + 1, (*chain, s))
                q.append(s.object)
    return out


def derive_language_family(
    index: KgIndex,
    nodes: set[str],
    *,
    focus: set[str] | None = None,
    props: Sequence[str] = ("P37", "P2936"),
    max_depth: int = 8,
    exclude_ancestors: Iterable[str] = (),
) -> Derivation:
    """Countries whose (official / used) languages differ but share a genealogical ancestor."""
    d = Derivation("shared_language_family")
    excluded = set(exclude_ancestors)
    lang_stmts: dict[str, list[Stmt]] = defaultdict(list)
    for prop in props:
        for s in index.by_prop.get(prop, ()):
            if s.object is not None and s.subject in nodes:
                lang_stmts[s.subject].append(s)
    anc_cache: dict[str, dict[str, tuple[int, tuple[Stmt, ...]]]] = {}

    def anc(lang: str) -> dict[str, tuple[int, tuple[Stmt, ...]]]:
        if lang not in anc_cache:
            anc_cache[lang] = _ancestors(index, lang, max_depth)
        return anc_cache[lang]

    for ca, cb in itertools.combinations(sorted(lang_stmts), 2):
        if not _pairs_ok(ca, cb, focus):
            continue
        for sa in lang_stmts[ca]:
            for sb in lang_stmts[cb]:
                la, lb = sa.object, sb.object
                assert la is not None and lb is not None
                if la == lb:
                    continue  # same language: that is shared_official_language / shared_language_used
                aa, ab = anc(la), anc(lb)
                common = [(aa[x][0] + ab[x][0], x) for x in set(aa) & set(ab) if x not in excluded]
                if not common:
                    continue
                _, fam = min(common)
                ov = sa.temporal.overlap(sb.temporal)
                if ov is None:
                    d.skip("disjoint_periods", pair=[ca, cb], via=fam)
                    continue
                d.add(
                    Link(
                        ca,
                        cb,
                        "shared_language_family",
                        False,
                        ov,
                        _ev(sa, sb, *aa[fam][1], *ab[fam][1]),
                        via=(fam,),
                        basis={
                            "languages": [la, lb],
                            "depth": [aa[fam][0], ab[fam][0]],
                            "properties": [sa.pid, sb.pid],
                            "temporal_basis": _temporal_basis(ov),
                        },
                    )
                )
    return d.finish()


def derive_colonial(index: KgIndex, polity_nodes: set[str]) -> Derivation:
    """``colony_of``: a subject classified as colony / protectorate / dependent territory whose P17
    (country) or P361 (part of) is a polity. Dates: the P17/P361 qualifiers, else the colony's own dates."""
    d = Derivation("colony_of")
    colonies = index.instances(COLONY_ROOTS)
    for colony, cls_ev in sorted(colonies.items()):
        found = False
        for prop in ("P17", "P361"):
            for s in index.objects(colony, prop):
                assert s.object is not None
                if s.object == colony:
                    continue
                if s.object not in polity_nodes:
                    d.skip("container_not_a_polity", colony=colony, object=s.object, property=prop)
                    continue
                if s.object in colonies and prop == "P361":
                    # a colony that is part of a larger colony: not a colonial power
                    d.skip("part_of_another_colony", colony=colony, object=s.object)
                    continue
                t = s.temporal
                date_ev: tuple[Stmt, ...] = ()
                if t.kind == "undated":
                    t, date_ev = index.entity_temporal(colony)
                d.add(
                    Link(
                        colony,
                        s.object,
                        "colony_of",
                        True,
                        t,
                        _ev(*cls_ev, s, *date_ev),
                        basis={
                            "property": prop,
                            "classified_as": cls_ev[0].object,
                            "temporal_basis": "statement"
                            if s.temporal.kind != "undated"
                            else ("entity_dates" if date_ev else "undated"),
                        },
                    )
                )
                found = True
        if not found:
            d.skip("colony_without_polity", colony=colony)
    return d.finish()


def succession_steps(index: KgIndex, polity_nodes: set[str]) -> dict[str, list[tuple[str, Stmt]]]:
    """successor → [(predecessor, statement)] from P1365/P155 (subject succeeds object) and P1366/P156
    (object succeeds subject). P155/P156 only between polities (they are also used for offices, events)."""
    steps: dict[str, list[tuple[str, Stmt]]] = defaultdict(list)
    for prop in (*SUCCESSION_FORWARD, *SUCCESSION_BACKWARD):
        for s in index.by_prop.get(prop, ()):
            if s.object is None:
                continue
            succ, pred = (s.subject, s.object) if prop in SUCCESSION_FORWARD else (s.object, s.subject)
            if prop in ("P155", "P156") and not (succ in polity_nodes and pred in polity_nodes):
                continue
            if prop in ("P1365", "P1366") and not (succ in polity_nodes or pred in polity_nodes):
                continue
            if succ != pred:
                steps[succ].append((pred, s))
    return steps


def derive_succession(index: KgIndex, polity_nodes: set[str]) -> Derivation:
    d = Derivation("successor_of")
    for succ, preds in sorted(succession_steps(index, polity_nodes).items()):
        for pred, s in preds:
            if succ not in polity_nodes or pred not in polity_nodes:
                d.skip("endpoint_not_a_polity", statement=s.statement_id)
                continue
            d.add(Link(succ, pred, "successor_of", True, s.temporal, _ev(s), basis={"property": s.pid}))
    # the same succession stated both ways (P1365 on one side, P1366 on the other) merges by key only when
    # dates agree; fold undated duplicates into the dated link's evidence
    return d.finish()


def derive_former_colony(
    index: KgIndex,
    polity_nodes: set[str],
    colony_links: Iterable[Link],
    *,
    targets: set[str] | None = None,
    max_chain: int = 3,
) -> Derivation:
    """``former_colony_of``: state S succeeds (through ≤ ``max_chain`` successions) an entity C that is
    ``colony_of`` P. ``via`` = the intermediate states then C."""
    d = Derivation("former_colony_of")
    by_colony: dict[str, list[Link]] = defaultdict(list)
    for lk in colony_links:
        by_colony[lk.subject].append(lk)
    steps = succession_steps(index, polity_nodes)
    starts = sorted(targets if targets is not None else (set(steps) & polity_nodes))
    for s0 in starts:
        # BFS backwards through predecessors, simple paths only
        q: deque[tuple[str, tuple[str, ...], tuple[Stmt, ...]]] = deque([(s0, (), ())])
        while q:
            cur, via, chain = q.popleft()
            if len(chain) >= max_chain:
                continue
            for pred, st in steps.get(cur, ()):
                if pred == s0 or pred in via:
                    continue
                nvia, nchain = (*via, pred), (*chain, st)
                for cl in by_colony.get(pred, ()):
                    if cl.object == s0:
                        d.skip("successor_is_the_power", state=s0, colony=pred)
                        continue
                    d.add(
                        Link(
                            s0,
                            cl.object,
                            "former_colony_of",
                            True,
                            cl.temporal,
                            _ev(*nchain) + cl.evidence,
                            via=nvia,
                            basis={"succession_properties": [x.pid for x in nchain], "colony": pred},
                        )
                    )
                q.append((pred, nvia, nchain))
    return d.finish()


def _signature_temporal(index: KgIndex, treaty: str, sa: Stmt, sb: Stmt) -> tuple[Temporal, tuple[Stmt, ...]]:
    pa, pb = sa.point, sb.point
    if pa and pb:
        lo, hi = sorted((pa, pb), key=lambda p: p.key())
        return (Temporal("point", point=lo) if lo == hi else Temporal("interval", lo, hi)), ()
    return index.entity_temporal(treaty)


def derive_co_signatory(index: KgIndex, *, focus: set[str] | None = None, max_signatories: int = 80) -> Derivation:
    """Two signatories (P1891) of the same treaty."""
    d = Derivation("co_signatory")
    groups: dict[str, list[Stmt]] = defaultdict(list)
    for s in index.by_prop.get("P1891", ()):
        if s.object is not None:
            groups[s.subject].append(s)
    for treaty, stmts in sorted(groups.items()):
        if len({s.object for s in stmts}) > max_signatories:
            d.skip("hub_object_not_expanded", via=treaty, members=len(stmts))
            continue
        for sa, sb in itertools.combinations(sorted(stmts, key=lambda s: (s.object or "", s.statement_id)), 2):
            assert sa.object is not None and sb.object is not None
            if not _pairs_ok(sa.object, sb.object, focus):
                continue
            t, date_ev = _signature_temporal(index, treaty, sa, sb)
            a, b = sorted((sa.object, sb.object))
            d.add(
                Link(a, b, "co_signatory", False, t, _ev(sa, sb, *date_ev), via=(treaty,), basis={"property": "P1891"})
            )
    return d.finish()


def _participations(index: KgIndex) -> dict[str, list[tuple[str, Stmt]]]:
    """conflict → [(participant, statement)] from P710 (conflict→participant), P1344 and P607
    (participant→conflict)."""
    out: dict[str, list[tuple[str, Stmt]]] = defaultdict(list)
    for s in index.by_prop.get("P710", ()):
        if s.object is not None:
            out[s.subject].append((s.object, s))
    for prop in ("P1344", "P607"):
        for s in index.by_prop.get(prop, ()):
            if s.object is not None:
                out[s.object].append((s.subject, s))
    return out


def derive_conflict_co_participants(
    index: KgIndex,
    nodes: set[str] | None = None,
    *,
    focus: set[str] | None = None,
    conflicts: set[str] | None = None,
    max_participants: int = 60,
) -> Derivation:
    """Both participants in the same conflict/event (sides not stated by Wikidata)."""
    d = Derivation("conflict_co_participant")
    for conflict, parts in sorted(_participations(index).items()):
        if conflicts is not None and conflict not in conflicts:
            continue
        by_part: dict[str, list[Stmt]] = defaultdict(list)
        for p, s in parts:
            if nodes is None or p in nodes:
                by_part[p].append(s)
        if len(by_part) > max_participants:
            d.skip("hub_object_not_expanded", via=conflict, members=len(by_part))
            continue
        t_conf, date_ev = index.entity_temporal(conflict)
        for a, b in itertools.combinations(sorted(by_part), 2):
            if not _pairs_ok(a, b, focus):
                continue
            sa, sb = by_part[a][0], by_part[b][0]
            t = t_conf
            if t.kind == "undated":
                ov = sa.temporal.overlap(sb.temporal)
                t = ov if ov is not None else Temporal("undated")
            d.add(
                Link(
                    a,
                    b,
                    "conflict_co_participant",
                    False,
                    t,
                    _ev(*by_part[a], *by_part[b], *date_ev),
                    via=(conflict,),
                    basis={"sides": "not_stated_by_source"},
                )
            )
    return d.finish()


def derive_conflict_sides(index: KgIndex, *, focus: set[str] | None = None) -> tuple[Derivation, Derivation]:
    """``conflict_same_side`` / ``conflict_opposing_sides`` from participation statements that carry a side
    (UCDP dyads loaded by KGGEOPOL with a ``ucdp:side`` qualifier). Statements without a side are ignored."""
    same, opp = Derivation("conflict_same_side"), Derivation("conflict_opposing_sides")
    for conflict, parts in sorted(_participations(index).items()):
        sided: list[tuple[str, str, Stmt]] = []
        for p, s in parts:
            side = next((v for q in CONFLICT_SIDE_QUALIFIERS for v in s.qualifier(q)), None)
            if side:
                sided.append((p, side, s))
        for (pa, sa_side, sa), (pb, sb_side, sb) in itertools.combinations(sorted(sided, key=lambda x: x[0]), 2):
            if not _pairs_ok(pa, pb, focus):
                continue
            ov = sa.temporal.overlap(sb.temporal)
            if ov is None:
                continue
            target = same if sa_side == sb_side else opp
            a, b = sorted((pa, pb))
            target.add(
                Link(
                    a, b, target.link_type, False, ov, _ev(sa, sb), via=(conflict,), basis={"sides": [sa_side, sb_side]}
                )
            )
    return same.finish(), opp.finish()


# ---------------------------------------------------------------------------------------------- atlas
_ISO3 = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")


def _node_for_iso3(iso3: str, iso3_to_qid: Mapping[str, str]) -> tuple[str, bool]:
    q = iso3_to_qid.get(iso3)
    return (q, True) if q else (f"iso3:{iso3}", False)


def derive_atlas_dyadic(
    rows: Iterable[AtlasRowRef],
    link_type: str,
    *,
    directed: bool,
    iso3_to_qid: Mapping[str, str],
    focus: set[str] | None = None,
) -> Derivation:
    """Country → country flows / scores (IMF DOTS exports, UN DESA migrant stock, UNGA agreement): one link
    per row with a value, weighted by the source's own decimal string."""
    d = Derivation(link_type)
    for r in rows:
        sel = {"selector": r.selector, "iso3": r.iso3, "counterpart": r.counterpart_iso3, "year": r.period_start}
        if not r.iso3:
            d.skip("iso3_unmapped", **sel)
            continue
        if not r.counterpart_iso3:
            d.skip("no_counterpart", **sel)
            continue
        if len(r.counterpart_iso3) != 3 or not set(r.counterpart_iso3) <= _ISO3 or r.flags.get("aggregate"):
            d.skip("counterpart_not_a_country", **sel)
            continue
        if r.iso3 == r.counterpart_iso3:
            d.skip("self_pair", **sel)
            continue
        if r.value is None:
            d.skip("missing_value", **sel)  # never zero-filled
            continue
        try:
            v = Decimal(r.value)
        except InvalidOperation:
            d.skip("value_not_decimal", **sel)
            continue
        if v == 0:
            d.skip("reported_zero", **sel)
            continue
        if not r.raw_sha256 or not r.selector:
            d.skip("row_without_provenance", **sel)
            continue
        subj, obj = r.subject_object
        assert obj is not None
        a, a_ok = _node_for_iso3(subj, iso3_to_qid)
        b, b_ok = _node_for_iso3(obj, iso3_to_qid)
        if focus is not None and a not in focus and b not in focus:
            continue
        if not directed and b < a:
            a, b = b, a
        try:
            year = r.year
        except ValueError:
            d.skip("bad_period", **sel)
            continue
        basis: dict[str, Any] = {
            "source": r.source,
            "dataset": r.dataset,
            "indicator": r.indicator,
            "iso3": [subj, obj],
        }
        if not (a_ok and b_ok):
            basis["qid_unmapped"] = [x for x, ok in ((subj, a_ok), (obj, b_ok)) if not ok]
        if r.flags:
            basis["source_flags"] = dict(r.flags)
        d.add(
            Link(
                a,
                b,
                link_type,
                directed,
                Temporal("snapshot", year=year),
                (Evidence.of_row(r),),
                weight=r.value,
                unit=r.unit,
                basis=basis,
            )
        )
    return d.finish()


def derive_atlas_shared_value(
    rows: Iterable[AtlasRowRef],
    link_type: str,
    *,
    iso3_to_qid: Mapping[str, str],
    focus: set[str] | None = None,
    max_group: int | None = None,
) -> Derivation:
    """Two countries with the same categorical value (``value_text``) for the same concept and year
    (e.g. legal family). The shared value is recorded in ``basis.value``; no node is invented for it."""
    d = Derivation(link_type)
    groups: dict[tuple[str, int, str], list[AtlasRowRef]] = defaultdict(list)
    for r in rows:
        if not r.value_text:
            d.skip("missing_value", iso3=r.iso3, selector=r.selector)
            continue
        groups[(r.concept_id or r.indicator, r.year, r.value_text.strip().casefold())].append(r)
    for (concept, year, _), rs in sorted(groups.items()):
        if max_group is not None and len(rs) > max_group and focus is None:
            d.skip("hub_value_not_expanded", value=rs[0].value_text, members=len(rs), year=year)
            continue
        for ra, rb in itertools.combinations(sorted(rs, key=lambda r: r.iso3), 2):
            a, _ = _node_for_iso3(ra.iso3, iso3_to_qid)
            b, _ = _node_for_iso3(rb.iso3, iso3_to_qid)
            if not _pairs_ok(a, b, focus):
                continue
            d.add(
                Link(
                    a,
                    b,
                    link_type,
                    False,
                    Temporal("snapshot", year=year),
                    (Evidence.of_row(ra), Evidence.of_row(rb)),
                    basis={
                        "value": ra.value_text,
                        "reported": {ra.iso3: ra.value_text, rb.iso3: rb.value_text},
                        "concept": concept,
                        "source": ra.source,
                    },
                )
            )
    return d.finish()


# ---------------------------------------------------------------------------------------------- all at once
@dataclass
class DerivedSet:
    by_type: dict[str, Derivation]

    @property
    def links(self) -> list[Link]:
        return [lk for d in self.by_type.values() for lk in d.links]

    def report(self) -> dict[str, Any]:
        return {k: v.to_json() for k, v in sorted(self.by_type.items())}


def derive_all(
    statements: Iterable[Stmt] | KgIndex,
    *,
    countries: Iterable[str] = (),
    focus: Iterable[str] | None = None,
    max_members: int = 60,
    atlas_dyadic: Mapping[str, tuple[Iterable[AtlasRowRef], bool]] | None = None,
    iso3_to_qid: Mapping[str, str] | None = None,
) -> DerivedSet:
    """Every KG-derivable link type (plus atlas dyadic types when rows are given).

    ``countries``: QIDs to treat as polities in addition to the KG's own classification (the crosswalk's
    countries). ``focus``: only links touching these nodes (``/v1/links/{iso3}``). ``atlas_dyadic``:
    ``{link_type: (rows, directed)}``."""
    index = statements if isinstance(statements, KgIndex) else KgIndex(statements)
    pol = polities(index, countries)
    fset = set(focus) if focus is not None else None
    out: dict[str, Derivation] = {}
    out["border_wikidata"] = derive_symmetric(index, "P47", "border_wikidata", pol, focus=fset, sources=is_wikidata)
    out["diplomatic_relation"] = derive_symmetric(index, "P530", "diplomatic_relation", pol, focus=fset)
    out["shared_official_language"] = derive_shared_object(index, "P37", "shared_official_language", pol, focus=fset)
    out["shared_language_used"] = derive_shared_object(index, "P2936", "shared_language_used", pol, focus=fset)
    out["shared_language_family"] = derive_language_family(index, pol, focus=fset)
    out["shared_currency"] = derive_shared_object(index, "P38", "shared_currency", pol, focus=fset)
    out["shared_head_of_state"] = derive_shared_object(index, "P35", "shared_head_of_state", pol, focus=fset)
    co, al = derive_co_membership(index, pol, focus=fset, max_members=max_members)
    out["co_member"], out["alliance_co_member"] = co, al
    out["co_signatory"] = derive_co_signatory(index, focus=fset)
    out["conflict_co_participant"] = derive_conflict_co_participants(index, None, focus=fset)
    out["conflict_same_side"], out["conflict_opposing_sides"] = derive_conflict_sides(index, focus=fset)
    col = derive_colonial(index, pol)
    if fset is not None:
        col_all = col
        col = Derivation(
            "colony_of", [lk for lk in col.links if lk.subject in fset or lk.object in fset], col.skipped, col.samples
        )
    else:
        col_all = col
    out["colony_of"] = col
    succ = derive_succession(index, pol)
    if fset is not None:
        succ.links = [lk for lk in succ.links if lk.subject in fset or lk.object in fset]
    out["successor_of"] = succ
    former = derive_former_colony(index, pol, col_all.links)
    if fset is not None:
        former.links = [lk for lk in former.links if lk.subject in fset or lk.object in fset]
    out["former_colony_of"] = former
    for lt, (rows, directed) in (atlas_dyadic or {}).items():
        out[lt] = derive_atlas_dyadic(rows, lt, directed=directed, iso3_to_qid=iso3_to_qid or {}, focus=fset)
    return DerivedSet(out)


def assert_all_evidenced(links: Iterable[Link]) -> None:
    """Invariant check used by tests and the API: every link has evidence (``Link`` already refuses an
    empty tuple; this also rejects evidence that cites nothing)."""
    for lk in links:
        if not lk.evidence:
            raise NoEvidence(f"{lk.link_type} {lk.subject}->{lk.object}")
        for e in lk.evidence:
            if e.kind == "kg_statement" and not e.statement_id:
                raise NoEvidence(f"{lk.link_type}: statement evidence without id")
            if e.kind == "atlas_row" and not (e.raw_sha256 and e.selector):
                raise NoEvidence(f"{lk.link_type}: atlas evidence without raw_sha256/selector")
