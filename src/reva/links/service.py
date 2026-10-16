"""Database-backed entry points used by the API (``/v1/links/...``): read a bounded neighbourhood of
``kg.statement_current``, derive links, search paths. Read-only."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from sqlalchemy import text

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
COUNTRY_LINK_PROPERTIES = (
    "P47", "P530", "P37", "P2936", "P38", "P35", "P463", "P1891", "P710", "P1344", "P607", "geo:covered_territory_of",
)  # fmt: skip
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


# ---------------------------------------------------------------------------------------------- LINKS2
# Country view and pair view over the materialised store (kg.link_row) with on-demand fallback.
from reva.links.model import Link  # noqa: E402

MAX_LIMIT = 10
PAIR_CAP_PER_KIND = 1_200
PAIR_TIME_BUDGET_S = 8.0
#: atlas kinds re-read from the snapshot for a pair (full history); the store keeps only top-k of these
ATLAS_METHODS = ("atlas_dyadic", "atlas_computed")
#: kinds whose "latest period" is shown when no year is asked (one reference year / decade per row)
PERIOD_KINDS_EXTRA = ("unga_vote_agreement",)
NOT_CAUSAL = {
    "fr": "Un lien dit « relié via », jamais « parce que ». Une part de votes identiques n'est ni une alliance ni "
    "un alignement d'intérêts. Donnée absente = pas de lien (jamais un lien de poids nul).",
    "en": "A link says “connected via”, never “because”. A share of identical votes is not an alliance nor an "
    "alignment of interests. Missing data = no link (never a zero-weight link).",
}

AtlasFactory = Callable[[str | None], Any]  # snapshot sha (None = latest) -> AtlasQuery | None


class UnknownLinkKind(ValueError):
    pass


def _crosswalk_labels() -> dict[str, dict[str, str]]:
    from functools import cache

    @cache
    def load() -> dict[str, dict[str, str]]:
        import json

        from reva.links.taxonomy import REPO_ROOT

        doc = json.loads((REPO_ROOT / "registry" / "entities" / "country_crosswalk.json").read_text(encoding="utf-8"))
        out = {}
        for e in doc["entities"]:
            d = {"en": e.get("label") or e["key"]}
            if e.get("label_fr"):
                d["fr"] = e["label_fr"]
            out[e["key"]] = d
        return out

    return load()


def _labels(conn: Any, nodes: Iterable[str], origin: str | None = None) -> dict[str, dict[str, str]]:
    nodes = set(nodes)
    cw = _crosswalk_labels()
    out: dict[str, dict[str, str]] = {}
    for n in nodes:
        if n.startswith("iso3:") and n[5:] in cw:
            out[n] = dict(cw[n[5:]])
        elif ISO3_RE.match(n) and n in cw:
            out[n] = dict(cw[n])
    q = [n for n in nodes if n not in out]
    if q:
        for k, v in KgNeighbourhood(conn, origin=origin).labels(q).items():
            out[k] = v
    return out


def _check_kinds(kinds: Iterable[str] | None) -> list[str] | None:
    if not kinds:
        return None
    tax = load_taxonomy()
    bad = [k for k in kinds if k not in tax.link_types]
    if bad:
        raise UnknownLinkKind(", ".join(sorted(bad))[:200])
    return sorted(set(kinds))


def describe(lk: Link, labels: Mapping[str, Mapping[str, str]]) -> dict[str, str]:
    """fr/en sentence for one link, e.g. ``Brésil — exportations ... → Portugal = 1234 US dollars (2024) [imf:…]``."""
    from reva.links.paths import Edge, PathGraph, render_hop

    g = PathGraph()
    g.add_labels(labels)
    lt = g.taxonomy.link(lk.link_type)
    e = Edge(lk.subject, lk.object, "link", lk.link_type, True, lk.temporal, lt.path_cost, link=lk)
    return {lang: render_hop(e, g, lang) for lang in ("fr", "en")}


def _link_json(
    lk: Link, labels: Mapping[str, Mapping[str, str]], iso3: tuple[str | None, str | None]
) -> dict[str, Any]:
    d = lk.to_json()
    d["iso3"] = list(iso3)
    d["facet"] = str(lk.basis.get("facet") or (f"issue={lk.basis['issue']}" if "issue" in lk.basis else ""))
    d["text"] = describe(lk, labels)
    if lk.link_type == "unga_vote_agreement":
        d["weight_kind"] = "derived_index"
    elif lk.weight is not None:
        d["weight_kind"] = "source_quantity"
    return d


def _period_kinds(tax: Any) -> list[str]:
    return sorted({k for k, lt in tax.link_types.items() if lt.temporal_kind == "snapshot"} | set(PERIOD_KINDS_EXTRA))


def _facets(facet: str | None) -> list[str]:
    from reva.links.atlas_links import DEFAULT_FACETS

    out = set(DEFAULT_FACETS.values())
    if facet:
        key = facet.split("=", 1)[0]
        out = {f for f in out if not f.startswith(key + "=")} | {facet}
    return sorted(out)


def _kind_meta(tax: Any, kind: str) -> dict[str, Any]:
    lt = tax.link(kind)
    return {
        "labels": dict(lt.labels),
        "inverse_labels": dict(lt.inverse_labels),
        "honesty": dict(lt.honesty),
        "directed": lt.directed,
        "status": lt.status,
        "temporal": lt.temporal_kind,
        "weight": {"kind": lt.weight_kind, "unit": lt.weight_unit},
        "sources": list(lt.sources),
    }


def country_view(
    conn: Any,
    ref: str,
    *,
    kinds: Iterable[str] | None = None,
    year: int | None = None,
    facet: str | None = None,
    limit: int = 5,
    atlas: AtlasFactory | None = None,
    live_only: bool = False,
) -> dict[str, Any]:
    """Top ``limit`` links per kind (and direction, facet) of one country, from the latest materialisation;
    without one, derived on demand (KG neighbourhood + the latest atlas snapshot)."""
    from reva.links import store as st

    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be within 1..{MAX_LIMIT}")
    tax = load_taxonomy()
    kinds_l = _check_kinds(kinds)
    m = st.latest_materialisation(conn, live_only=live_only)
    iso3 = ref if ISO3_RE.match(ref) else None
    if m is not None and iso3 is None:
        row = conn.execute(
            text(
                """SELECT subject_iso3 AS c FROM kg.link_row WHERE materialisation_id = :m AND subject = :q
                     AND subject_iso3 IS NOT NULL
                   UNION ALL SELECT object_iso3 FROM kg.link_row WHERE materialisation_id = :m AND object = :q
                     AND object_iso3 IS NOT NULL LIMIT 1"""
            ),
            {"m": m["id"], "q": ref},
        ).first()
        iso3 = row[0] if row else None
    if m is None or iso3 is None:
        return _country_on_demand(conn, ref, kinds_l, year, facet, limit, atlas, live_only)
    if iso3 not in _crosswalk_labels():
        raise UnknownCountry(iso3)
    rows = st.country_rows(
        conn,
        m["id"],
        iso3,
        kinds=kinds_l,
        facets=_facets(facet),
        year=year,
        latest_period_kinds=_period_kinds(tax),
        limit=limit,
    )
    from reva.links.paths import PathQuery, temporal_allows

    yq = PathQuery("Q1", "Q2", as_of=year) if year is not None else None
    items: list[tuple[dict[str, Any], Link]] = []
    for r in rows:
        lk = st.link_from_row(r)
        if yq is not None and not temporal_allows(lk.temporal, yq):
            continue
        items.append((r, lk))
    labels = _labels(conn, {n for _, lk in items for n in (lk.subject, lk.object, *lk.via)} | {f"iso3:{iso3}"})
    counts = (m.get("parameters") or {}).get("counts") or {}
    by_kind: dict[str, Any] = {}
    flat = []
    for r, lk in items:
        k = by_kind.setdefault(lk.link_type, {**_kind_meta(tax, lk.link_type), "groups": {}})
        gkey = f"{r['dir']}|{r['facet']}"
        grp = k["groups"].setdefault(
            gkey, {"direction": r["dir"], "facet": r["facet"], "total": int(r["total"]), "period": None, "links": []}
        )
        lj = _link_json(lk, labels, (r["subject_iso3"], r["object_iso3"]))
        lj["rank"] = int(r["rn"])
        grp["links"].append(lj)
        if lk.link_type in _period_kinds(tax) and year is None:
            grp["period"] = lk.temporal.label()
        flat.append(lj)
    for k in by_kind.values():
        k["groups"] = list(k["groups"].values())
    status = {
        t: {"status": lt.status, "stored": counts.get(t)}
        for t, lt in sorted(tax.link_types.items())
        if kinds_l is None or t in kinds_l
    }
    return {
        "mode": "materialised",
        "query": ref,
        "iso3": iso3,
        "node": next((lk.subject if r["subject_iso3"] == iso3 else lk.object for r, lk in items), f"iso3:{iso3}"),
        "year": year,
        "limit": limit,
        "facets": _facets(facet),
        "store": _store_ref(m),
        "by_kind": by_kind,
        "links": flat,
        "labels": labels,
        "link_types": status,
        "notes": NOT_CAUSAL,
        "truncated": False,
    }


def _store_ref(m: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "materialisation_id": m["id"],
        "method_version": m["method_version"],
        "atlas_snapshot_sha256": m["atlas_snapshot_sha256"],
        "atlas_origin": m["atlas_origin"],
        "kg_origin": m["kg_origin"],
        "kg_load_watermark": m["kg_load_watermark"],
        "kg_recorded_watermark": m["kg_recorded_watermark"],
        "recorded_at": m["recorded_at"],
        "top_k": (m.get("parameters") or {}).get("top_k"),
        "link_count": m["link_count"],
    }


def _rank_in_python(
    links: list[Link], node: str, year: int | None, facets: list[str], limit: int, period_kinds: list[str]
) -> list[tuple[str, str, int, int, Link]]:
    """Same ranking as :func:`reva.links.store.country_rows`, in memory (on-demand mode)."""
    groups: dict[tuple[str, str, str], list[Link]] = {}
    for lk in links:
        fac = str(lk.basis.get("facet") or (f"issue={lk.basis['issue']}" if "issue" in lk.basis else ""))
        if fac and fac not in facets:
            continue
        d = "both" if not lk.directed else ("outgoing" if lk.subject == node else "incoming")
        groups.setdefault((lk.link_type, fac, d), []).append(lk)
    out = []
    for (kind, fac, d), ls in sorted(groups.items()):
        if year is None and kind in period_kinds:
            hi = max((lk.temporal.last_year or -(10**9)) for lk in ls)
            ls = [lk for lk in ls if lk.temporal.last_year == hi]
        ls.sort(
            key=lambda lk: (
                -(float(lk.weight) if lk.weight is not None else float("-inf")),
                -(lk.temporal.last_year or 10**9),
            )
        )
        for i, lk in enumerate(ls[:limit], 1):
            out.append((d, fac, i, len(ls), lk))
    return out


def _country_on_demand(
    conn: Any,
    ref: str,
    kinds: list[str] | None,
    year: int | None,
    facet: str | None,
    limit: int,
    atlas: AtlasFactory | None,
    live_only: bool,
) -> dict[str, Any]:
    tax = load_taxonomy()
    base = links_for_country(conn, ref, link_types=kinds, year=year)
    qid = base["qid"]
    nb = KgNeighbourhood(conn)
    iso_map = nb.iso3_to_qid()
    qid_iso3 = {q: c for c, q in iso_map.items()}
    iso3 = ref if ISO3_RE.match(ref) else qid_iso3.get(qid)
    links: list[Link] = []
    from reva.links.store import link_from_row

    for d in base["links"]:
        d2 = dict(d)
        d2.setdefault("via", [])
        links.append(link_from_row(d2))
    atlas_ref = None
    if atlas is not None and iso3 is not None:
        q = atlas(None)
        if q is not None:
            from reva.links.atlas_links import AtlasLinkReader

            atlas_ref = q.snapshot.ref()
            reader = AtlasLinkReader.of_query(q, node_of=lambda c: iso_map.get(c) or f"iso3:{c}")
            for lt in reader.dyadic_types():
                if kinds is not None and lt.id not in kinds:
                    continue
                yr = f" AND year = {int(year)}" if year is not None else ""
                d = reader.dyadic(lt, where=f"(row_iso3 = '{iso3}' OR partner = '{iso3}'){yr}", cap=50_000)
                links.extend(d.links)
            if kinds is None or "unga_vote_agreement" in kinds:
                v = reader.votes(
                    grain="decade",
                    node=iso3,
                    year_lo=None if year is None else year - year % 10,
                    year_hi=None if year is None else year - year % 10 + 9,
                    cap=20_000,
                )
                links.extend(v.links)
    node = iso_map.get(iso3 or "", qid) if iso3 else qid
    ranked = _rank_in_python(links, node, year, _facets(facet), limit, _period_kinds(tax))
    labels = _labels(conn, {n for *_, lk in ranked for n in (lk.subject, lk.object, *lk.via)} | {node})
    by_kind: dict[str, Any] = {}
    flat = []
    for d, fac, rank, total, lk in ranked:
        k = by_kind.setdefault(lk.link_type, {**_kind_meta(tax, lk.link_type), "groups": {}})
        grp = k["groups"].setdefault(
            f"{d}|{fac}", {"direction": d, "facet": fac, "total": total, "period": None, "links": []}
        )
        lj = _link_json(
            lk,
            labels,
            (
                qid_iso3.get(lk.subject) or (lk.subject[5:] if lk.subject.startswith("iso3:") else None),
                qid_iso3.get(lk.object) or (lk.object[5:] if lk.object.startswith("iso3:") else None),
            ),
        )
        lj["rank"] = rank
        if year is None and lk.link_type in _period_kinds(tax):
            grp["period"] = lk.temporal.label()
        grp["links"].append(lj)
        flat.append(lj)
    for k in by_kind.values():
        k["groups"] = list(k["groups"].values())
    base.update(
        mode="on_demand",
        iso3=iso3,
        node=node,
        year=year,
        limit=limit,
        facets=_facets(facet),
        store=None,
        atlas_snapshot=atlas_ref,
        by_kind=by_kind,
        links=flat,
        labels={**base.get("labels", {}), **labels},
        notes=NOT_CAUSAL,
    )
    return base


def pair_view(
    conn: Any,
    a: str,
    b: str,
    *,
    kinds: Iterable[str] | None = None,
    from_year: int | None = None,
    to_year: int | None = None,
    detail_votes: tuple[int, int, str] | None = None,
    atlas: AtlasFactory | None = None,
    live_only: bool = False,
    time_budget_s: float = PAIR_TIME_BUDGET_S,
) -> dict[str, Any]:
    """Every link between two countries over time: KG / dataset kinds from the store (or on demand), atlas
    kinds (trade, migration, borders, vote agreement by year and decade) re-read from the snapshot."""
    from reva.links import store as st

    if not (ISO3_RE.match(a) and ISO3_RE.match(b)) or a == b:
        raise ValueError("a and b must be two different ISO 3166 alpha-3 codes")
    cw = _crosswalk_labels()
    for c in (a, b):
        if c not in cw:
            raise UnknownCountry(c)
    deadline = time.monotonic() + time_budget_s
    tax = load_taxonomy()
    kinds_l = _check_kinds(kinds)
    atlas_kinds = sorted(k for k, lt in tax.link_types.items() if lt.method in ATLAS_METHODS)
    m = st.latest_materialisation(conn, live_only=live_only)
    links: list[tuple[Link, tuple[str | None, str | None]]] = []
    truncated: list[str] = []
    notes: list[str] = []
    q = atlas(m["atlas_snapshot_sha256"] if m else None) if atlas is not None else None
    if m is not None:
        rows = st.pair_rows(
            conn,
            m["id"],
            a,
            b,
            kinds=kinds_l,
            exclude_kinds=atlas_kinds if q is not None else (),
            year_lo=from_year,
            year_hi=to_year,
            cap=5_000,
        )
        if len(rows) > 5_000:
            truncated.append("store_rows")
            rows = rows[:5_000]
        for r in rows:
            links.append((st.link_from_row(r), (r["subject_iso3"], r["object_iso3"])))
        if q is None:
            notes.append("atlas snapshot not readable here: atlas kinds come from the store (top-k per country only)")
    else:
        try:
            base = links_for_country(conn, a, link_types=kinds_l)
        except (UnknownCountry, AmbiguousCountry):
            base = {"links": []}
        nb = KgNeighbourhood(conn)
        qb = nb.iso3_to_qid([a, b])
        for d in base["links"]:
            if {d["subject"], d["object"]} == {qb.get(a), qb.get(b)} and qb.get(a) and qb.get(b):
                d2 = dict(d)
                lk = st.link_from_row(d2)
                links.append((lk, (a if lk.subject == qb.get(a) else b, b if lk.object == qb.get(b) else a)))
        notes.append("no materialised link store: KG kinds derived on demand from the neighbourhood of " + a)
    atlas_ref = None
    vote_detail = None
    if q is not None:
        from reva.links.atlas_links import AtlasLinkReader

        atlas_ref = q.snapshot.ref()
        nb = KgNeighbourhood(conn)
        iso_map = nb.iso3_to_qid([a, b])
        reader = AtlasLinkReader.of_query(q, node_of=lambda c: iso_map.get(c) or f"iso3:{c}")
        node_iso = {iso_map.get(c) or f"iso3:{c}": c for c in (a, b)}

        def iso_of(lk: Link) -> tuple[str | None, str | None]:
            return node_iso.get(lk.subject), node_iso.get(lk.object)

        yr = ""
        if from_year is not None:
            yr += f" AND year >= {int(from_year)}"
        if to_year is not None:
            yr += f" AND year <= {int(to_year)}"
        where = f"((row_iso3 = '{a}' AND partner = '{b}') OR (row_iso3 = '{b}' AND partner = '{a}')){yr}"
        for lt in reader.dyadic_types():
            if kinds_l is not None and lt.id not in kinds_l:
                continue
            if time.monotonic() > deadline:
                truncated.append(lt.id)
                continue
            d = reader.dyadic(lt, where=where, cap=PAIR_CAP_PER_KIND)
            if d.skipped.get("row_cap_reached"):
                truncated.append(lt.id)
            links.extend((lk, iso_of(lk)) for lk in d.links)
        if kinds_l is None or "unga_vote_agreement" in kinds_l:
            for grain in ("decade", "year"):
                if time.monotonic() > deadline:
                    truncated.append(f"unga_vote_agreement:{grain}")
                    continue
                v = reader.votes(grain=grain, pair=(a, b), year_lo=from_year, year_hi=to_year, cap=PAIR_CAP_PER_KIND)
                if v.skipped.get("row_cap_reached"):
                    truncated.append(f"unga_vote_agreement:{grain}")
                links.extend((lk, iso_of(lk)) for lk in v.links)
            if detail_votes is not None and time.monotonic() <= deadline:
                lo, hi, issue = detail_votes
                vote_detail = {
                    "years": [lo, hi],
                    "issue": issue,
                    **reader.vote_detail(a, b, year_lo=lo, year_hi=hi, issue=issue),
                }
    labels = _labels(
        conn, {n for lk, _ in links for n in (lk.subject, lk.object, *lk.via)} | {f"iso3:{a}", f"iso3:{b}"}
    )
    kinds_out: dict[str, Any] = {}
    for lk, iso in sorted(
        links, key=lambda x: (x[0].link_type, x[0].temporal.first_year or -(10**9), x[0].temporal.label())
    ):
        k = kinds_out.setdefault(
            lk.link_type,
            {**_kind_meta(tax, lk.link_type), "count": 0, "first_year": None, "last_year": None, "links": []},
        )
        k["count"] += 1
        fy, ly = lk.temporal.first_year, lk.temporal.last_year
        if fy is not None:
            k["first_year"] = fy if k["first_year"] is None else min(k["first_year"], fy)
        if ly is not None:
            k["last_year"] = ly if k["last_year"] is None else max(k["last_year"], ly)
        k["links"].append(_link_json(lk, labels, iso))
    return {
        "a": a,
        "b": b,
        "from_year": from_year,
        "to_year": to_year,
        "store": _store_ref(m) if m else None,
        "atlas_snapshot": atlas_ref,
        "kinds": kinds_out,
        "kinds_without_links": sorted(
            k for k in (kinds_l or tax.link_types) if k not in kinds_out and tax.link(k).scope == "country_country"
        ),
        "vote_detail": vote_detail,
        "labels": labels,
        "truncated": sorted(set(truncated)),
        "notes": notes,
        "honesty": NOT_CAUSAL,
    }
