"""Database-backed entry points used by the API (``/v1/links/...``): read a bounded neighbourhood of
``kg.statement_current``, derive links, search paths. Read-only."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from reva.links.derive import DerivedSet, assert_all_evidenced, derive_all
from reva.links.model import AtlasRowRef
from reva.links.paths import KgNeighbourhood, PathQuery, PathResult, find_paths, graph_from_db
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


def resolve_node(nb: KgNeighbourhood, ref: str) -> str:
    """A QID / provider key is returned as is; an ISO 3166 alpha-3 code is resolved through the KG's P298
    statements."""
    if ISO3_RE.match(ref):
        q = nb.iso3_to_qid([ref]).get(ref)
        if q is None:
            raise UnknownCountry(ref)
        return q
    return ref


def path_between(
    conn: Any,
    source: str,
    target: str,
    *,
    origin: str | None = None,
    **query: Any,
) -> PathResult:
    nb = KgNeighbourhood(conn, origin=origin)
    q = PathQuery(resolve_node(nb, source), resolve_node(nb, target), **query)
    g = graph_from_db(conn, q, origin=origin)
    return find_paths(g, q)


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
    s3, reached3, _ = nb.expand(sorted(langs), 6, LANGUAGE_GENEALOGY)
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
        "truncated": bool(trunc1 or trunc2),
    }
