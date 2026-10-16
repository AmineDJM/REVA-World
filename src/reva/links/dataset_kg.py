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
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from reva.links.derive import Derivation, KgIndex, _ev, derive_shared_subject  # noqa: F401 (re-export)
from reva.links.model import Link, Stmt, Temporal

CORRESPONDS = "geopol:corresponds_to_iso3"
CSHAPES_BORDER = "cshapes:shares_border_with"
CSHAPES_RULED_BY = "cshapes:ruled_by"
UCDP_PARTIES = ("ucdp:primary_party", "ucdp:secondary_party")
COVERED_TERRITORY = "geo:covered_territory_of"
UNPK_CONTRIB = "unpk:contributed_personnel_to"
UNPK_MISSION_COUNTRY = "unpk:mission_country"
DATASET_PROPERTIES = (
    CORRESPONDS, CSHAPES_BORDER, CSHAPES_RULED_BY, *UCDP_PARTIES, COVERED_TERRITORY, UNPK_CONTRIB, UNPK_MISSION_COUNTRY,
)  # fmt: skip
#: bound on derived peacekeeping links (one per contribution statement x mission country; linear, not pairwise)
MAX_PEACEKEEPING_LINKS = 200_000

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
                d.add(
                    Link(
                        a,
                        b,
                        "land_border_cshapes",
                        False,
                        tb,
                        _ev(s, *ea, *eb),
                        basis={
                            "property": CSHAPES_BORDER,
                            "territories": [s.subject, s.object],
                            "iso3": sorted(x for x in (ia, ib) if x),
                            "licence": "CC BY-NC-SA 4.0 (CShapes 2.0): non-commercial",
                        },
                    )
                )
                if ia is None or ib is None:
                    d.skip("territory_without_country", statement=s.statement_id)
    return d.finish()


def derive_cshapes_colonial(
    index: KgIndex, node_of: NodeOf, qid_iso3: Mapping[str, str], *, focus: set[str] | None = None
) -> Derivation:
    d = Derivation("colonial_rule_cshapes")
    corr = Correspondence(index)
    for s in index.by_prop.get(CSHAPES_RULED_BY, ()):
        if s.object is None:
            continue
        # the territory is named by the one country it corresponds to (any period): its link keeps the rule
        # period (a correspondence starting at independence must not trim the colonial period)
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
                d.add(
                    Link(
                        na,
                        nb,
                        "colonial_rule_cshapes",
                        True,
                        tb,
                        _ev(s, *ea, *eb),
                        basis={
                            "property": CSHAPES_RULED_BY,
                            "status": (s.qualifier("status") or ("?",))[0],
                            "territory": s.subject,
                            "ruler": s.object,
                            "territory_mapping": mapping,
                            "iso3": [ia, ib],
                            "licence": "CC BY-NC-SA 4.0 (CShapes 2.0): non-commercial",
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
    return out


def report(ds: Mapping[str, Derivation]) -> dict[str, Any]:
    return {k: v.to_json() for k, v in sorted(ds.items())}
