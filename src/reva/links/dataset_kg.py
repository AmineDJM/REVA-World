"""Country–country links from dataset statements in the KG (KGGEOPOL's CShapes and UCDP loads, KGPOLITY's
Cliopatria overlay; agent LINKS2).

These sources name states by dataset entities (``gw:<code>``, ``cow:<code>``, ``cshapes:gw:<code>``) or QIDs.
A dataset entity is mapped to a crosswalk country through its dated ``geopol:corresponds_to_iso3`` statements
(KGGEOPOL); the correspondence statements are part of the link's evidence. Without a correspondence the
entity keeps its own node (a historical state is never re-labelled as a present-day country).

* ``land_border_cshapes`` — ``cshapes:shares_border_with`` (dated, land only) between two territories; the
  link holds over the border interval intersected with both correspondences.
* ``colonial_rule_cshapes`` — ``cshapes:ruled_by`` territory → ruling state (status colony / protectorate /
  mandate / occupied, dated). The territory is named by the country it *later became* when no correspondence
  overlaps the rule period (``basis.territory_mapping = later_state``: borders may differ).
* ``conflict_same_side`` / ``conflict_opposing_sides`` — ``ucdp:primary_party`` / ``ucdp:secondary_party``
  (qualifier ``side`` a/b, runs of conflict-years) of the same ``ucdp:conflict:<id>``, overlapping years.
* ``shared_polity_territory`` — two countries objects of ``geo:covered_territory_of`` with the same polity
  subject over overlapping periods (hub rule: polities covering more than ``max_members`` countries are not
  expanded pairwise).

EVMAP (A-COLONIAL, A-HAZARDS):

* ``colonised_by_coldat`` — ``coldat:colonized_by`` country → colonizer (COLDAT, first .. last year; statements
  of both date aggregations with the same years give one link citing both).
* ``colonial_rule_cow`` — ``cowent:colony_of|protectorate_of|mandated_to|possession_of`` territory → ruler (COW
  entities list, dated as printed, left-censored at 1816), territory named like ``colonial_rule_cshapes``.
* ``hazard_shared_event`` — one NCEI tsunami record: source country (``ncei:country``) → each other country
  where runups were observed (``ncei:runup_country``), via the event, dated by its P585. The same natural event
  observed in two countries: never a cause, never an exchange.
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from reva.links.derive import Derivation, KgIndex, _ev, derive_shared_subject  # noqa: F401 (re-export)
from reva.links.model import Link, PartialDate, Stmt, Temporal

CORRESPONDS = "geopol:corresponds_to_iso3"
CSHAPES_BORDER = "cshapes:shares_border_with"
CSHAPES_RULED_BY = "cshapes:ruled_by"
UCDP_PARTIES = ("ucdp:primary_party", "ucdp:secondary_party")
COVERED_TERRITORY = "geo:covered_territory_of"
UNPK_CONTRIB = "unpk:contributed_personnel_to"
UNPK_MISSION_COUNTRY = "unpk:mission_country"
COLDAT_COLONIZED_BY = "coldat:colonized_by"
#: COW entities list status spells that are colonial rule (occupied_by, claimed_by, leased_to, part_of are not)
COW_RULE_STATUS = {
    "cowent:colony_of": "colony",
    "cowent:protectorate_of": "protectorate",
    "cowent:mandated_to": "mandate",
    "cowent:possession_of": "possession",
}
NCEI_COUNTRY = "ncei:country"
NCEI_RUNUP_COUNTRY = "ncei:runup_country"
NCEI_VALIDITY = "ncei:tsunami_validity"
TSUNAMI_PREFIX = "ncei_hazards:tsunami:"
DATASET_PROPERTIES = (
    CORRESPONDS, CSHAPES_BORDER, CSHAPES_RULED_BY, *UCDP_PARTIES, COVERED_TERRITORY, UNPK_CONTRIB, UNPK_MISSION_COUNTRY,
    COLDAT_COLONIZED_BY, *COW_RULE_STATUS, NCEI_COUNTRY, NCEI_RUNUP_COUNTRY, NCEI_VALIDITY,
)  # fmt: skip
#: bounds of hazard_shared_event (per event: runup countries expanded; overall: links)
MAX_HAZARD_COUNTRIES_PER_EVENT = 60
MAX_HAZARD_LINKS = 200_000
COLDAT_LICENCE = "CC0 1.0 (COLDAT v3.0, Becker 2019, Harvard Dataverse doi:10.7910/DVN/T9SDEW)"
COW_LICENCE = "COW terms: cite (Tir et al. 1998, COW Territorial Change v6); users may not charge for access"
NCEI_LICENCE = "US Government work (NOAA NCEI), public domain; cite doi:10.7289/V5PN93H7"
#: The COW entities list starts in 1816: a spell printed from 1816 may be older (left-censored)
COW_COVERAGE_START_YEAR = 1816
COW_START_BASIS = (
    "dataset coverage start (the COW entities list begins in 1816): the rule began in or before this year; "
    "the start is not a date of the relation"
)
#: bound on derived peacekeeping links (one per contribution statement x mission country; linear, not pairwise)
MAX_PEACEKEEPING_LINKS = 200_000
#: CSH-1 (review V3b): CShapes 2.0 starts on 1886-01-01. A period starting that day is *left-censored*: the rule
#: or border may be older (Algeria: French rule from 1830). Its start is served at year precision and the basis
#: says so; renderers show "≤ 1886", never the day.
CSHAPES_COVERAGE_START = PartialDate(1886, 1, 1, 11)
CSHAPES_START_BASIS = (
    "dataset coverage start (CShapes 2.0 begins 1886-01-01): the situation began on or before this date; "
    "the start is not a date of the relation"
)


def _left_censor(t: Temporal, basis: dict[str, Any]) -> Temporal:
    """A temporal whose start is CShapes' first day: start at year precision + ``left_censored`` in ``basis``."""
    if t.kind != "interval" or t.start is None or t.start.key() > CSHAPES_COVERAGE_START.key():
        return t
    if t.start.key() < CSHAPES_COVERAGE_START.key():  # earlier than the dataset: from another source, kept
        return t
    basis["left_censored"] = True
    basis["coverage_start"] = CSHAPES_COVERAGE_START.iso()
    basis["start_basis"] = CSHAPES_START_BASIS
    return Temporal("interval", PartialDate(CSHAPES_COVERAGE_START.year), t.end)


NodeOf = Callable[[str], str]


class Correspondence:
    """Dataset entity → crosswalk ISO3 over time (``geopol:corresponds_to_iso3``)."""

    def __init__(self, index: KgIndex) -> None:
        self.by_node: dict[str, list[tuple[str, Stmt]]] = defaultdict(list)
        for s in index.by_prop.get(CORRESPONDS, ()):
            if s.value and len(s.value) == 3 and s.value.isupper():
                self.by_node[s.subject].append((s.value, s))

    def during(self, node: str, t: Temporal) -> list[tuple[str, Temporal, Stmt]]:
        """Correspondences overlapping ``t``: (iso3, overlap, statement)."""
        out = []
        for iso3, s in self.by_node.get(node, ()):
            ov = s.temporal.overlap(t)
            if ov is not None and ov.kind != "undated":
                out.append((iso3, ov, s))
        return out

    def any_iso3(self, node: str) -> tuple[str, Stmt] | None:
        """The single country a node ever corresponds to (``None`` when none or several)."""
        codes = {iso3 for iso3, _ in self.by_node.get(node, ())}
        if len(codes) != 1:
            return None
        iso3 = next(iter(codes))
        return iso3, next(s for c, s in self.by_node[node] if c == iso3)


def _resolve(
    node: str, t: Temporal, corr: Correspondence, node_of: NodeOf, qid_iso3: Mapping[str, str]
) -> list[tuple[str, Temporal, tuple[Stmt, ...], str | None]]:
    """[(link node, temporal, extra evidence statements, iso3)] for one endpoint over ``t``."""
    if node in qid_iso3:
        return [(node_of(qid_iso3[node]), t, (), qid_iso3[node])]
    hits = corr.during(node, t)
    if hits:
        return [(node_of(iso3), ov, (s,), iso3) for iso3, ov, s in hits]
    return [(node, t, (), None)]


def derive_cshapes_borders(
    index: KgIndex, node_of: NodeOf, qid_iso3: Mapping[str, str], *, focus: set[str] | None = None
) -> Derivation:
    d = Derivation("land_border_cshapes")
    corr = Correspondence(index)
    for s in index.by_prop.get(CSHAPES_BORDER, ()):
        if s.object is None:
            continue
        for na, ta, ea, ia in _resolve(s.subject, s.temporal, corr, node_of, qid_iso3):
            for nb, tb, eb, ib in _resolve(s.object, ta, corr, node_of, qid_iso3):
                if na == nb:
                    d.skip("same_country", statement=s.statement_id)
                    continue
                if focus is not None and na not in focus and nb not in focus:
                    continue
                a, b = sorted((na, nb))
                basis: dict[str, Any] = {
                    "property": CSHAPES_BORDER,
                    "territories": [s.subject, s.object],
                    "iso3": sorted(x for x in (ia, ib) if x),
                    "licence": "CC BY-NC-SA 4.0 (CShapes 2.0): non-commercial",
                }
                d.add(Link(a, b, "land_border_cshapes", False, _left_censor(tb, basis), _ev(s, *ea, *eb), basis=basis))
                if ia is None or ib is None:
                    d.skip("territory_without_country", statement=s.statement_id)
    return d.finish()


def _cow_left_censor(t: Temporal, basis: dict[str, Any]) -> Temporal:
    if t.kind == "interval" and t.start is not None and t.start.year == COW_COVERAGE_START_YEAR:
        basis["left_censored"] = True
        basis["coverage_start"] = f"{COW_COVERAGE_START_YEAR}"
        basis["start_basis"] = COW_START_BASIS
    return t


def _derive_territory_rule(
    d: Derivation,
    rules: Iterable[tuple[Stmt, str]],
    *,
    licence: str,
    censor: Callable[[Temporal, dict[str, Any]], Temporal],
    index: KgIndex,
    node_of: NodeOf,
    qid_iso3: Mapping[str, str],
    focus: set[str] | None,
) -> Derivation:
    """territory —ruled by→ state statements (with their status) → directed territory → ruler links. The territory
    is named by the one country it corresponds to (any period), so the link keeps the rule period."""
    corr = Correspondence(index)
    for s, status in rules:
        if s.object is None:
            continue
        subj: list[tuple[str, Temporal, tuple[Stmt, ...], str | None]]
        if s.subject in qid_iso3:
            subj = [(node_of(qid_iso3[s.subject]), s.temporal, (), qid_iso3[s.subject])]
            mapping = "qid"
        else:
            later = corr.any_iso3(s.subject)
            if later is None:
                d.skip("territory_without_country", statement=s.statement_id)
                subj = [(s.subject, s.temporal, (), None)]
                mapping = "none"
            else:
                cs = later[1].temporal.first_year
                rs = s.temporal.first_year
                mapping = "later_state" if cs is not None and rs is not None and cs > rs else "same_entity"
                subj = [(node_of(later[0]), s.temporal, (later[1],), later[0])]
        for na, ta, ea, ia in subj:
            for nb, tb, eb, ib in _resolve(s.object, ta, corr, node_of, qid_iso3):
                if na == nb:
                    d.skip("same_country", statement=s.statement_id)
                    continue
                if focus is not None and na not in focus and nb not in focus:
                    continue
                cbasis: dict[str, Any] = {
                    "property": s.pid,
                    "status": status,
                    "territory": s.subject,
                    "ruler": s.object,
                    "territory_mapping": mapping,
                    "iso3": [ia, ib],
                    "licence": licence,
                }
                d.add(Link(na, nb, d.link_type, True, censor(tb, cbasis), _ev(s, *ea, *eb), basis=cbasis))
    return d.finish()


def derive_cshapes_colonial(
    index: KgIndex, node_of: NodeOf, qid_iso3: Mapping[str, str], *, focus: set[str] | None = None
) -> Derivation:
    rules = ((s, (s.qualifier("status") or ("?",))[0]) for s in index.by_prop.get(CSHAPES_RULED_BY, ()))
    return _derive_territory_rule(
        Derivation("colonial_rule_cshapes"),
        rules,
        licence="CC BY-NC-SA 4.0 (CShapes 2.0): non-commercial",
        censor=_left_censor,
        index=index,
        node_of=node_of,
        qid_iso3=qid_iso3,
        focus=focus,
    )


def derive_cow_colonial(
    index: KgIndex, node_of: NodeOf, qid_iso3: Mapping[str, str], *, focus: set[str] | None = None
) -> Derivation:
    """``colonial_rule_cow``: COW entities list colony / protectorate / mandate / possession spells (A-COLONIAL)."""
    rules = [(s, status) for prop, status in COW_RULE_STATUS.items() for s in index.by_prop.get(prop, ())]
    return _derive_territory_rule(
        Derivation("colonial_rule_cow"),
        rules,
        licence=COW_LICENCE,
        censor=_cow_left_censor,
        index=index,
        node_of=node_of,
        qid_iso3=qid_iso3,
        focus=focus,
    )


def derive_coldat(
    index: KgIndex, node_of: NodeOf, qid_iso3: Mapping[str, str], *, focus: set[str] | None = None
) -> Derivation:
    """``colonised_by_coldat``: country —coldat:colonized_by→ colonizer, first .. last year. COLDAT states each
    spell under two date aggregations; statements with the same years are one link citing both (the aggregations
    are listed in ``basis.aggregations``), different years stay two links."""
    d = Derivation("colonised_by_coldat")
    corr = Correspondence(index)
    groups: dict[tuple[str, str, str], list[Stmt]] = defaultdict(list)
    for s in index.by_prop.get(COLDAT_COLONIZED_BY, ()):
        if s.object is None:
            d.skip("colonizer_not_an_entity", statement=s.statement_id)
            continue
        groups[(s.subject, s.object, s.temporal.label())].append(s)
    for (subject, obj, _), ss in sorted(groups.items()):
        ss = sorted(ss, key=lambda x: x.statement_id)
        aggs = sorted({a for x in ss for a in x.qualifier("aggregation")})
        for na, ta, ea, ia in _resolve(subject, ss[0].temporal, corr, node_of, qid_iso3):
            for nb, tb, eb, ib in _resolve(obj, ta, corr, node_of, qid_iso3):
                if na == nb:
                    d.skip("same_country", statement=ss[0].statement_id)
                    continue
                if focus is not None and na not in focus and nb not in focus:
                    continue
                if ia is None:
                    d.skip("country_without_iso3", statement=ss[0].statement_id)
                basis: dict[str, Any] = {
                    "property": COLDAT_COLONIZED_BY,
                    "aggregations": aggs,
                    "aggregation": " + ".join(aggs) or "?",
                    "country_as_coded": (ss[0].qualifier("country_as_coded") or (None,))[0],
                    "colonizer_as_coded": (ss[0].qualifier("colonizer_as_coded") or (None,))[0],
                    "iso3": [ia, ib],
                    "licence": COLDAT_LICENCE,
                    "note": "one spell per (country, colonial power), first .. last year: interruptions are not coded",
                }
                d.add(Link(na, nb, d.link_type, True, tb, _ev(*ss, *ea, *eb), basis=basis))
    return d.finish()


def derive_hazard_shared_event(
    index: KgIndex,
    node_of: NodeOf,
    qid_iso3: Mapping[str, str],
    *,
    focus: set[str] | None = None,
    max_countries: int = MAX_HAZARD_COUNTRIES_PER_EVENT,
    cap: int = MAX_HAZARD_LINKS,
) -> Derivation:
    """``hazard_shared_event``: one NCEI tsunami source record with a source country and runups observed in other
    countries → source country → runup country, via the event, dated by the event's P585 (undated without one).
    Erroneous records (validity -1) and events with more than ``max_countries`` runup countries are skipped and
    counted; at most ``cap`` links (truncation recorded)."""
    d = Derivation("hazard_shared_event")

    def node(q: str) -> tuple[str, str | None]:
        return (node_of(qid_iso3[q]), qid_iso3[q]) if q in qid_iso3 else (q, None)

    runups: dict[str, list[Stmt]] = defaultdict(list)
    for s in index.by_prop.get(NCEI_RUNUP_COUNTRY, ()):
        if s.object is not None and s.subject.startswith(TSUNAMI_PREFIX):
            runups[s.subject].append(s)
    n = 0
    for ev, rs in sorted(runups.items()):
        validity = [v for v in index.out.get((ev, NCEI_VALIDITY), ()) if v.value is not None]
        if any(str(v.value).strip() == "-1" for v in validity):
            d.skip("erroneous_record", event=ev)
            continue
        srcs = index.objects(ev, NCEI_COUNTRY)
        if not srcs:
            d.skip("source_without_country", event=ev)
            continue
        if len({x.object for x in srcs}) > 1:
            d.skip("several_source_countries", event=ev)
            continue
        countries = {r.object for r in rs}
        if len(countries) > max_countries:
            d.skip("too_many_runup_countries", event=ev, countries=len(countries))
            continue
        src = srcs[0]
        assert src.object is not None
        dated = next((p for p in index.out.get((ev, "P585"), ()) if p.value_date is not None), None)
        temporal = Temporal("point", point=dated.value_date) if dated is not None else Temporal("undated")
        na, ia = node(src.object)
        for r in sorted(rs, key=lambda x: (x.object or "", x.statement_id)):
            assert r.object is not None
            nb, ib = node(r.object)
            if nb == na:
                d.skip("runup_in_source_country", event=ev)
                continue
            if focus is not None and na not in focus and nb not in focus:
                continue
            if n >= cap:
                d.skip("cap_reached", cap=cap)
                return d.finish()
            n += 1
            evs = _ev(src, r, *((dated,) if dated is not None else ()), *validity)
            d.add(
                Link(
                    na,
                    nb,
                    d.link_type,
                    True,
                    temporal,
                    evs,
                    via=(ev,),
                    basis={
                        "event": ev,
                        "runup_records": (r.qualifier("runup_records") or (None,))[0],
                        "runup_deaths_published_sum": (r.qualifier("runup_deaths_published_sum") or (None,))[0],
                        "validity_code": (validity[0].value if validity else None),
                        "runup_countries_of_event": len(countries),
                        "iso3": [ia, ib],
                        "licence": NCEI_LICENCE,
                        "note": "the same natural event observed in both countries (source in one, runups recorded "
                        "in the other); not a cause, not an exchange",
                    },
                )
            )
    return d.finish()


def derive_ucdp_sides(
    index: KgIndex, node_of: NodeOf, qid_iso3: Mapping[str, str], *, focus: set[str] | None = None
) -> tuple[Derivation, Derivation]:
    same, opp = Derivation("conflict_same_side"), Derivation("conflict_opposing_sides")
    corr = Correspondence(index)
    parties: dict[str, list[tuple[str, str, Temporal, tuple[Stmt, ...], str | None, Stmt]]] = defaultdict(list)
    for prop in UCDP_PARTIES:
        for s in index.by_prop.get(prop, ()):
            side = (s.qualifier("side") or (None,))[0]
            if s.object is None or side not in ("a", "b"):
                continue
            for n, t, ev, iso3 in _resolve(s.object, s.temporal, corr, node_of, qid_iso3):
                parties[s.subject].append((n, side, t, ev, iso3, s))
    for conflict, ps in sorted(parties.items()):
        for (na, sa, ta, ea, ia, xa), (nb, sb, tb, eb, ib, xb) in itertools.combinations(
            sorted(ps, key=lambda p: (p[0], p[5].statement_id)), 2
        ):
            if na == nb:
                continue
            if focus is not None and na not in focus and nb not in focus:
                continue
            ov = ta.overlap(tb)
            if ov is None or ov.kind == "undated":
                continue
            target = same if sa == sb else opp
            (a, ra, ia2), (b, rb, ib2) = sorted(((na, xa, ia), (nb, xb, ib)))
            years_a = set(ra.qualifier("years"))
            years_b = set(rb.qualifier("years"))
            target.add(
                Link(
                    a,
                    b,
                    target.link_type,
                    False,
                    ov,
                    _ev(xa, xb, *ea, *eb),
                    via=(conflict,),
                    basis={
                        "sides": [sa, sb] if a == na else [sb, sa],
                        "roles": [ra.pid, rb.pid],
                        "common_years": sorted(years_a & years_b, key=lambda y: int(y) if y.isdigit() else 0)[:100],
                        "iso3": [ia2, ib2],
                        "source_definition": "UCDP: primary party = state on side A/B; secondary party = state "
                        "supporting a side with troops",
                    },
                )
            )
    return same.finish(), opp.finish()


def derive_peacekeeping(
    index: KgIndex, *, focus: set[str] | None = None, cap: int = MAX_PEACEKEEPING_LINKS
) -> Derivation:
    """``peacekeeping_contributor_to_host``: country A —unpk:contributed_personnel_to→ mission (one calendar year)
    and mission —unpk:mission_country→ B over an overlapping period → A → B via the mission. Linear in the
    contribution statements (bounded by ``cap``, truncation recorded)."""
    d = Derivation("peacekeeping_contributor_to_host")
    hosts: dict[str, list[Stmt]] = defaultdict(list)
    for s in index.by_prop.get(UNPK_MISSION_COUNTRY, ()):
        if s.object is not None:
            hosts[s.subject].append(s)
    n = 0
    for c in index.by_prop.get(UNPK_CONTRIB, ()):
        if c.object is None:
            continue
        hs = hosts.get(c.object)
        if not hs:
            d.skip("mission_without_reported_country", mission=c.object)
            continue
        for h in hs:
            if h.object is None or h.object == c.subject:
                continue
            if focus is not None and c.subject not in focus and h.object not in focus:
                continue
            ov = c.temporal.overlap(h.temporal)
            if ov is None or ov.kind == "undated":
                d.skip("disjoint_periods", mission=c.object, contributor=c.subject)
                continue
            if n >= cap:
                d.skip("cap_reached", cap=cap)
                return d.finish()
            n += 1
            d.add(
                Link(
                    c.subject,
                    h.object,
                    "peacekeeping_contributor_to_host",
                    True,
                    ov,
                    _ev(c, h),
                    via=(c.object,),
                    basis={
                        "year": (c.qualifier("year") or (None,))[0],
                        "max_uniformed_personnel_reported": (
                            c.qualifier("max_uniformed_personnel_reported") or (None,)
                        )[0],
                        "note": "deployment under a UN mandate as reported by the UN Peace and Security Data Hub; "
                        "says nothing about alliances or positions",
                    },
                )
            )
    return d.finish()


def derive_dataset_links(
    statements: Iterable[Stmt] | KgIndex,
    *,
    node_of: NodeOf,
    qid_iso3: Mapping[str, str],
    focus: set[str] | None = None,
    max_members: int = 60,
) -> dict[str, Derivation]:
    index = statements if isinstance(statements, KgIndex) else KgIndex(statements)
    out: dict[str, Derivation] = {}
    out["land_border_cshapes"] = derive_cshapes_borders(index, node_of, qid_iso3, focus=focus)
    out["colonial_rule_cshapes"] = derive_cshapes_colonial(index, node_of, qid_iso3, focus=focus)
    same, opp = derive_ucdp_sides(index, node_of, qid_iso3, focus=focus)
    out["conflict_same_side"], out["conflict_opposing_sides"] = same, opp
    out["peacekeeping_contributor_to_host"] = derive_peacekeeping(index, focus=focus)
    out["colonised_by_coldat"] = derive_coldat(index, node_of, qid_iso3, focus=focus)
    out["colonial_rule_cow"] = derive_cow_colonial(index, node_of, qid_iso3, focus=focus)
    out["hazard_shared_event"] = derive_hazard_shared_event(index, node_of, qid_iso3, focus=focus)
    return out


def report(ds: Mapping[str, Derivation]) -> dict[str, Any]:
    return {k: v.to_json() for k, v in sorted(ds.items())}
