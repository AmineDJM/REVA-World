"""Interconnections (V3, agent LINKS): typed links of a country and explained paths between two entities.

Read-only (``read`` scope) over ``kg.*`` through :mod:`reva.links`. A path is a chain of "connected via"
hops, each citing its statement(s) or atlas row(s); it is never a causal explanation. Causal wording only
appears for has-cause / has-effect statements and is attributed to the source that asserts it. Every
link has evidence; a link type whose source is not delivered yet simply has no links (its status says so).
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, Path, Query, Request

from reva.api import schemas as s
from reva.api.auth import require_scope
from reva.api.deps import Timer, origin_of, origin_warnings, read_conn
from reva.api.errors import NotFound
from reva.kernel.errors import ValidationProblem
from reva.links import service
from reva.links.paths import DISCLAIMER, MAX_HOPS_LIMIT, MAX_K, MAX_STEPS, MAX_WAYPOINTS, PathQueryError

router = APIRouter(prefix="/v1/links", tags=["links"], dependencies=[Depends(require_scope("read"))])

NODE_PATTERN = r"^(Q[1-9][0-9]{0,11}|[A-Z]{3}|[a-z][a-z0-9_]{1,31}:[A-Za-z0-9_.:-]{1,128})$"
KIND_PATTERN = r"^((statement|link):)?([A-Za-z][A-Za-z0-9_]{0,63}|[a-z][a-z0-9_]{0,31}:[a-z][a-z0-9_]{0,63})$"
#: non-transit properties: Wikidata property or namespaced dataset predicate (objects never passed through)
PROP_PATTERN = r"^(P[1-9][0-9]{0,8}|[a-z][a-z0-9_]{0,31}:[a-z][a-z0-9_]{0,63})$"
MAX_UNORDERED_WAYPOINTS = 8
MAX_KINDS_PER_STEP = 16
MAX_NON_TRANSIT = 16
PATH_WARNING = s.ApiWarning(code="path_not_causal", message=DISCLAIMER["en"])
COMMUNITY_WARNING = s.ApiWarning(
    code="community_edited", message="Wikidata statements are community-edited and are not verified by REVA."
)


class LinksPathResponse(s.Envelope):
    data: dict[str, Any]


class CountryLinksResponse(s.Envelope):
    data: dict[str, Any]


class PairLinksResponse(s.Envelope):
    data: dict[str, Any]


ISO3_PATTERN = r"^[A-Z]{3}$"
FACET_PATTERN = r"^[a-z][a-z0-9_]{0,31}=[A-Za-z0-9_.-]{1,32}$"
LINK_KIND_PATTERN = r"^[a-z][a-z0-9_]{1,63}$"
DERIVED_WARNING = s.ApiWarning(
    code="derived_index",
    message="unga_vote_agreement is computed by REVA (share of identical votes, formula and counts in basis); "
    "it is not an alliance, friendship or alignment of interests.",
)
LINK_WARNING = s.ApiWarning(
    code="link_not_causal",
    message="A link says 'connected via', never 'because'; missing data gives no link (never a zero-weight link).",
)


def _production(request: Request) -> bool:
    return getattr(request.app.state.settings, "reva_env", "") == "production"


def _atlas_factory(request: Request) -> Any:
    """snapshot sha (None = latest; live only in production) -> AtlasQuery, or None when no snapshot exists."""

    def make(sha: str | None) -> Any:
        from reva.atlas.query import query_for
        from reva.atlas.snapshot import SnapshotUnavailable, resolve_snapshot

        st = getattr(request.app.state, "atlas_store", None)
        if st is None:
            from reva.adapters.objects.factory import build_store

            st = build_store(request.app.state.settings)
            request.app.state.atlas_store = st
        origin = "live" if sha is None and _production(request) else None
        try:
            with read_conn(request) as conn:
                snap = resolve_snapshot(conn, st, origin=origin, sha256=sha)
        except SnapshotUnavailable:
            return None
        return query_for(snap, st)

    return make


def _origins(evidence: list[dict[str, Any]]) -> list[str]:
    """Origins of the evidence; an item without a recorded origin is ``unknown`` (never assumed live)."""
    return sorted({e.get("origin") or "unknown" for e in evidence})


def _envelope(origins: list[str], community: bool, t: Timer, rows: int, **detail: Any) -> dict[str, Any]:
    known = [o for o in origins if o != "unknown"]
    origin = origin_of(known)
    warnings = ([COMMUNITY_WARNING] if community else []) + origin_warnings(origin)
    if "unknown" in origins:
        # LINKS-3: evidence without a recorded origin is not labelled live
        origin = "mixed" if known else "none"
        warnings.append(
            s.ApiWarning(
                code="origin_unknown",
                message="Some evidence has no recorded origin (live or test fixture); it is not asserted to be live.",
            )
        )
    return {
        "context_mode": "evidence",
        "origin": origin,
        "modality": "reported_observation",
        "warnings": warnings,
        "cost": t.cost(rows, **detail),
    }


@router.get(
    "/path",
    response_model=LinksPathResponse,
    responses={404: {"model": s.Problem}, 422: {"model": s.Problem}},
)
def get_links_path(
    request: Request,
    from_: str = Query(..., alias="from", pattern=NODE_PATTERN, max_length=161, description="QID or ISO3"),
    to: str = Query(..., pattern=NODE_PATTERN, max_length=161, description="QID or ISO3"),
    via: list[str] | None = Query(
        None, max_length=MAX_WAYPOINTS, description="Waypoints (QID or ISO3): one path forced through them, in order"
    ),
    k: int = Query(3, ge=1, le=MAX_K, description="Number of paths (k shortest simple paths)"),
    max_hops: int = Query(4, ge=1, le=MAX_HOPS_LIMIT),
    as_of: int | None = Query(None, ge=-5000, le=3000, description="Only hops valid in that year"),
    from_year: int | None = Query(None, ge=-5000, le=3000),
    to_year: int | None = Query(None, ge=-5000, le=3000),
    kind: list[str] | None = Query(None, max_length=50, description="Allowed hop kinds: P37, link:co_member..."),
    exclude_kind: list[str] | None = Query(None, max_length=50),
    include_undated: bool = True,
    include_blocked_hubs: bool = False,
    steps: list[str] | None = Query(
        None,
        max_length=MAX_STEPS,
        description="Ordered hop-type steps, one parameter per step; each a comma-separated set of hop kinds "
        "(P945,link:co_member). The path matches the steps in order, each by a distinct hop.",
    ),
    via_any: list[str] | None = Query(
        None, max_length=MAX_UNORDERED_WAYPOINTS, description="The path passes through at least one of these nodes"
    ),
    via_all: list[str] | None = Query(
        None, max_length=MAX_UNORDERED_WAYPOINTS, description="The path passes through all of these nodes (any order)"
    ),
    non_transit: list[str] | None = Query(
        None,
        max_length=MAX_NON_TRANSIT,
        description="Properties whose objects may end a path but are never passed through (P37, P2936, P30, P2348)",
    ),
) -> LinksPathResponse:
    t = Timer()
    for x in (*(kind or ()), *(exclude_kind or ())):
        if not re.match(KIND_PATTERN, x):
            raise ValidationProblem("invalid kind", kind=x[:64])
    for x in (*(via or ()), *(via_any or ()), *(via_all or ())):
        if not re.match(NODE_PATTERN, x):
            raise ValidationProblem("invalid waypoint", via=x[:64])
    if via and len(via) > MAX_WAYPOINTS:
        raise ValidationProblem(f"at most {MAX_WAYPOINTS} waypoints")
    for name, vals, cap in (
        ("via_any", via_any, MAX_UNORDERED_WAYPOINTS),
        ("via_all", via_all, MAX_UNORDERED_WAYPOINTS),
        ("non_transit", non_transit, MAX_NON_TRANSIT),
        ("steps", steps, MAX_STEPS),
    ):
        if vals and len(vals) > cap:
            raise ValidationProblem(f"at most {cap} {name} values")
    for x in non_transit or ():
        if not re.match(PROP_PATTERN, x):
            raise ValidationProblem("invalid non-transit property", non_transit=x[:64])
    step_sets: list[frozenset[str]] = []
    for st in steps or ():
        kinds_of_step = [x.strip() for x in st.split(",")]
        if not all(kinds_of_step) or len(kinds_of_step) > MAX_KINDS_PER_STEP:
            raise ValidationProblem(f"each step names 1..{MAX_KINDS_PER_STEP} hop kinds", step=st[:64])
        for x in kinds_of_step:
            if not re.match(KIND_PATTERN, x):
                raise ValidationProblem("invalid step kind", kind=x[:64])
        step_sets.append(frozenset(kinds_of_step))
    if len(step_sets) > max_hops:
        raise ValidationProblem("no more ordered steps than max_hops", steps=len(step_sets), max_hops=max_hops)
    if via and (step_sets or via_any or via_all):
        raise ValidationProblem("ordered via waypoints cannot be combined with steps, via_any or via_all")
    window = None
    if from_year is not None or to_year is not None:
        if from_year is None or to_year is None:
            raise ValidationProblem("from_year and to_year go together")
        window = (from_year, to_year)
    try:
        with read_conn(request) as conn:
            res = service.path_between(
                conn,
                from_,
                to,
                via=via or (),
                k=k,
                max_hops=max_hops,
                as_of=as_of,
                window=window,
                kinds=frozenset(kind) if kind else None,
                exclude_kinds=frozenset(exclude_kind or ()),
                include_undated=include_undated,
                include_blocked_hubs=include_blocked_hubs,
                steps=tuple(step_sets),
                via_any=frozenset(via_any or ()),
                via_all=frozenset(via_all or ()),
                non_transit_props=frozenset(non_transit or ()),
            )
    except service.UnknownCountry as e:
        raise NotFound("country not found in the knowledge graph (no ISO 3166 alpha-3 statement)", iso3=str(e)) from e
    except service.AmbiguousCountry as e:
        raise _ambiguous(e) from e
    except PathQueryError as e:
        raise ValidationProblem(str(e)) from e
    if res.reason == "unknown_node":
        raise NotFound("entity not found in the knowledge graph", qid=res.query.source)
    data = res.to_json()
    evidence = [e for p in data["paths"] for h in p["hops"] for e in h["evidence"]]
    env = _envelope(
        _origins(evidence),
        any(str(e.get("source", "")).startswith("wikidata") for e in evidence),
        t,
        len(evidence),
        explored_nodes=res.explored_nodes,
    )
    env["warnings"].insert(0, PATH_WARNING)
    if res.truncated:
        reasons = "; ".join(res.truncation_reasons)
        env["warnings"].append(
            s.ApiWarning(
                code="truncated",
                message=("search bounded: a connection may exist beyond" + (f" ({reasons})" if reasons else ""))[:500],
            )
        )
    return LinksPathResponse(data=data, **env)


@router.get(
    "/pair",
    response_model=PairLinksResponse,
    responses={404: {"model": s.Problem}, 422: {"model": s.Problem}},
)
def get_pair_links(
    request: Request,
    a: str = Query(..., pattern=ISO3_PATTERN, description="ISO 3166 alpha-3 (crosswalk entity)"),
    b: str = Query(..., pattern=ISO3_PATTERN, description="ISO 3166 alpha-3 (crosswalk entity)"),
    kind: list[str] | None = Query(None, max_length=30, description="Link types (default: all)"),
    from_year: int | None = Query(None, ge=-5000, le=3000),
    to_year: int | None = Query(None, ge=-5000, le=3000),
    votes_detail_from: int | None = Query(
        None, ge=1945, le=2100, description="With votes_detail_to: per roll call positions of a and b (<= 200)"
    ),
    votes_detail_to: int | None = Query(None, ge=1945, le=2100),
    votes_detail_issue: str = Query("all", pattern="^(all|co|hr|me|nu|di|ec)$"),
) -> PairLinksResponse:
    """Every link between two countries over time (KG, datasets, atlas flows, vote agreement by year and decade),
    each with its evidence. Bounded: rows per kind, total time budget (truncation is reported)."""
    t = Timer()
    for k in kind or ():
        if not re.match(LINK_KIND_PATTERN, k):
            raise ValidationProblem("invalid kind", kind=k[:64])
    if a == b:
        raise ValidationProblem("a and b must differ")
    if from_year is not None and to_year is not None and from_year > to_year:
        raise ValidationProblem("from_year must be <= to_year")
    detail = None
    if (votes_detail_from is None) != (votes_detail_to is None):
        raise ValidationProblem("votes_detail_from and votes_detail_to go together")
    if votes_detail_from is not None and votes_detail_to is not None:
        if not 0 <= votes_detail_to - votes_detail_from <= 10:
            raise ValidationProblem("votes detail spans at most 11 years")
        detail = (votes_detail_from, votes_detail_to, votes_detail_issue)
    try:
        with read_conn(request) as conn:
            data = service.pair_view(
                conn,
                a,
                b,
                kinds=kind,
                from_year=from_year,
                to_year=to_year,
                detail_votes=detail,
                atlas=_atlas_factory(request),
                live_only=_production(request),
            )
    except service.UnknownCountry as e:
        raise NotFound("not a crosswalk country", iso3=str(e)) from e
    except service.UnknownLinkKind as e:
        raise ValidationProblem("unknown link type", kind=str(e)) from e
    evidence = [e for k in data["kinds"].values() for lk in k["links"] for e in lk["evidence"]]
    env = _envelope(
        _origins(evidence),
        any(str(e.get("source", "")).startswith("wikidata") for e in evidence),
        t,
        sum(k["count"] for k in data["kinds"].values()),
    )
    env["warnings"].insert(0, LINK_WARNING)
    if "unga_vote_agreement" in data["kinds"]:
        env["warnings"].append(DERIVED_WARNING)
    if data["truncated"]:
        env["warnings"].append(
            s.ApiWarning(code="truncated", message=("bounded read: " + ", ".join(data["truncated"]))[:500])
        )
    return PairLinksResponse(data=data, **env)


def _ambiguous(e: service.AmbiguousCountry) -> ValidationProblem:
    return ValidationProblem(
        "ambiguous ISO 3166 alpha-3 code: several knowledge-graph items hold it and the reviewed registry "
        "(registry/entities/country_qids.json) does not decide; give a QID",
        iso3=e.iso3,
        candidates=e.candidates[:10],
    )


@router.get(
    "/{ref}",
    response_model=CountryLinksResponse,
    responses={404: {"model": s.Problem}, 422: {"model": s.Problem}},
)
def get_country_links(
    request: Request,
    ref: str = Path(..., pattern=NODE_PATTERN, max_length=161, description="ISO 3166 alpha-3 or QID"),
    link_type: list[str] | None = Query(None, max_length=30, description="Link types (alias of kind)"),
    kind: list[str] | None = Query(None, max_length=30, description="Link types (default: all)"),
    year: int | None = Query(None, ge=-5000, le=3000, description="Only links valid in that year"),
    facet: str | None = Query(
        None, pattern=FACET_PATTERN, description="issue=co (vote agreement), section=XVI (BACI); defaults all/TOTAL"
    ),
    limit: int = Query(5, ge=1, le=service.MAX_LIMIT, description="Top-N per kind, direction and facet"),
) -> CountryLinksResponse:
    """Top-N links per kind of one country (latest period per kind unless ``year``), from the materialised
    link store of the latest atlas snapshot + KG watermark; derived on demand when no store exists."""
    t = Timer()
    kinds = sorted(set(link_type or ()) | set(kind or ())) or None
    for k in kinds or ():
        if not re.match(LINK_KIND_PATTERN, k):
            raise ValidationProblem("invalid kind", kind=k[:64])
    try:
        with read_conn(request) as conn:
            data = service.country_view(
                conn,
                ref,
                kinds=kinds,
                year=year,
                facet=facet,
                limit=limit,
                atlas=_atlas_factory(request),
                live_only=_production(request),
            )
    except service.UnknownCountry as e:
        raise NotFound("country not found in the knowledge graph (no ISO 3166 alpha-3 statement)", iso3=str(e)) from e
    except service.AmbiguousCountry as e:
        raise _ambiguous(e) from e
    except service.UnknownLinkKind as e:
        raise ValidationProblem("unknown link type", kind=str(e)) from e
    evidence = [e for lk in data["links"] for e in lk["evidence"]]
    env = _envelope(
        _origins(evidence),
        any(str(e.get("source", "")).startswith("wikidata") for e in evidence),
        t,
        len(data["links"]),
    )
    env["warnings"].insert(0, LINK_WARNING)
    if "unga_vote_agreement" in data.get("by_kind", {}):
        env["warnings"].append(DERIVED_WARNING)
    if data.get("truncated"):
        env["warnings"].append(
            s.ApiWarning(
                code="truncated",
                message=f"neighbourhood read bounded at {len(data.get('truncated_nodes') or [])} node(s): "
                "links beyond the read caps are not listed",
            )
        )
    return CountryLinksResponse(data=data, **env)
