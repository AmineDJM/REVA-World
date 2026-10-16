"""Database-backed entry points used by the API (``/v1/links/...``): read a bounded neighbourhood of
``kg.statement_current``, derive links, search paths. Read-only."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from reva.links.derive import DerivedSet, assert_all_evidenced, derive_all
from reva.links.model import AtlasRowRef
from reva.links.paths import (
    TIME_BUDGET_S,
    KgNeighbourhood,
    PathQuery,
    PathResult,
    find_chain,
    find_paths,
    graph_from_db,
)
from reva.links.taxonomy import load_taxonomy

ISO3_RE = re.compile(r"^[A-Z]{3}$")
#: properties read around a country for ``/v1/links/{iso3}`` (2 hops: country → shared object → others)
COUNTRY_LINK_PROPERTIES = ("P47", "P530", "P37", "P2936", "P38", "P35", "P463", "P1891", "P710", "P1344", "P607")
SUCCESSION_PROPERTIES = ("P1365", "P1366", "P155", "P156", "P17", "P361")
LANGUAGE_GENEALOGY = ("P279", "P361")

#: atlas rows provider: (iso3 or None for all, link types) -> {link_type: (rows, directed)}
AtlasProvider = Callable[[str | None, Iterable[str]], Mapping[str, tuple[Iterable[AtlasRowRef], bool]]]


class UnknownCountry(LookupError):
    pass


class AmbiguousCountry(LookupError):
    """An ISO alpha-3 code held by several KG items and not decided by the reviewed registry (LINKS-2)."""

    def __init__(self, iso3: str, candidates: list[str]) -> None:
        super().__init__(iso3)
        self.iso3 = iso3
        self.candidates = candidates


def resolve_node(nb: KgNeighbourhood, ref: str) -> str:
    """A QID / provider key is returned as is; an ISO 3166 alpha-3 code is resolved through the reviewed
    registry (``registry/entities/country_qids.json``), else the KG's P298 statements when exactly one item
    holds it (several holders: :class:`AmbiguousCountry`, never a silent pick)."""
    if ISO3_RE.match(ref):
        q = nb.iso3_to_qid([ref]).get(ref)
        if q is None:
            if ref in nb.ambiguous_iso3:
                raise AmbiguousCountry(ref, nb.ambiguous_iso3[ref])
            raise UnknownCountry(ref)
        return q
    return ref


def path_between(
    conn: Any,
    source: str,
    target: str,
    *,
    origin: str | None = None,
    via: Iterable[str] = (),
    time_budget_s: float | None = TIME_BUDGET_S,
    **query: Any,
) -> PathResult:
    """Paths ``source`` → ``target`` (QIDs or ISO3 codes); with ``via`` waypoints, one path forced through
    them (each segment read and searched on its own bounded neighbourhood). ``time_budget_s`` bounds the
    whole request (reads, segments, candidate ranking); a spent budget is reported as truncation."""
    deadline = None if time_budget_s is None else time.monotonic() + time_budget_s
    nb = KgNeighbourhood(conn, origin=origin)
    ends = [resolve_node(nb, x) for x in (source, *via, target)]
    if len(ends) > 2:
        return find_chain(
            lambda sq: graph_from_db(conn, sq, origin=origin, deadline=deadline), ends, deadline=deadline, **query
        )
    q = PathQuery(ends[0], ends[1], **query)
    g = graph_from_db(conn, q, origin=origin, deadline=deadline)
    return find_paths(g, q, deadline=deadline)


def links_for_country(
    conn: Any,
    ref: str,
    *,
    origin: str | None = None,
    link_types: Iterable[str] | None = None,
    year: int | None = None,
    atlas: AtlasProvider | None = None,
    max_members: int = 60,
) -> dict[str, Any]:
    """Every derivable link touching one country (focus), from its KG neighbourhood (+ atlas rows when an
    atlas provider is given). Link types whose source is not delivered are listed as such."""
    tax = load_taxonomy()
    nb = KgNeighbourhood(conn, origin=origin)
    qid = resolve_node(nb, ref)
    s1, reached, trunc1 = nb.expand([qid], 2, COUNTRY_LINK_PROPERTIES)
    s2, reached2, trunc2 = nb.expand([qid], 4, SUCCESSION_PROPERTIES)
    langs = {s.object for s in s1 if s.pid in ("P37", "P2936") and s.object}
    s3, reached3, trunc3 = nb.expand(sorted(langs), 6, LANGUAGE_GENEALOGY)
    nodes = reached | reached2 | reached3
    stmts = {s.statement_id: s for s in (*s1, *s2, *s3, *nb.context(nodes))}
    iso_map: dict[str, str] = {}
    atlas_rows: Mapping[str, tuple[Iterable[AtlasRowRef], bool]] = {}
    if atlas is not None:
        iso_map = nb.iso3_to_qid()
        iso3 = next((k for k, v in iso_map.items() if v == qid), None)
        atlas_rows = atlas(iso3, [t for t, lt in tax.link_types.items() if lt.method == "atlas_dyadic"])
    derived: DerivedSet = derive_all(
        list(stmts.values()), focus={qid}, max_members=max_members, atlas_dyadic=atlas_rows, iso3_to_qid=iso_map
    )
    wanted = set(link_types) if link_types else None
    links = [lk for lk in derived.links if wanted is None or lk.link_type in wanted]
    if year is not None:
        from reva.links.paths import temporal_allows

        yq = PathQuery("Q1", "Q2", as_of=year)
        links = [lk for lk in links if temporal_allows(lk.temporal, yq)]
    assert_all_evidenced(links)
    labels = nb.labels({qid} | {n for lk in links for n in (lk.subject, lk.object, *lk.via)})
    status = {
        t: {"status": lt.status, "derived": len(derived.by_type[t].links) if t in derived.by_type else 0}
        for t, lt in sorted(tax.link_types.items())
    }
    return {
        "qid": qid,
        "query": ref,
        "links": [lk.to_json() for lk in links],
        "labels": labels,
        "link_types": status,
        "derivation": derived.report(),
        "truncated": bool(trunc1 or trunc2 or trunc3),
        "truncated_nodes": sorted(trunc1 | trunc2 | trunc3)[:50],
    }
