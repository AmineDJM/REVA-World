"""Interconnections (V3, agent LINKS): typed links of a country and explained paths between two entities.

Read-only (``read`` scope) over ``kg.*`` through :mod:`reva.links`. A path is a chain of "connected via"
hops, each citing its statement(s) or atlas row(s); it is never a causal explanation. Causal wording only
appears for has-cause / has-effect statements and is attributed to the source that asserts it. Every
link has evidence; a link type whose source is not delivered yet simply has no links (its status says so).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path, Query, Request

from reva.api import schemas as s
from reva.api.auth import require_scope
from reva.api.deps import Timer, origin_of, origin_warnings, read_conn
from reva.api.errors import NotFound
from reva.kernel.errors import ValidationProblem
from reva.links import service
from reva.links.paths import DISCLAIMER, MAX_HOPS_LIMIT, MAX_K, MAX_WAYPOINTS, PathQueryError

router = APIRouter(prefix="/v1/links", tags=["links"], dependencies=[Depends(require_scope("read"))])

NODE_PATTERN = r"^(Q[1-9][0-9]{0,11}|[A-Z]{3}|[a-z][a-z0-9_]{1,31}:[A-Za-z0-9_.:-]{1,128})$"
KIND_PATTERN = r"^((statement|link):)?([A-Za-z][A-Za-z0-9_]{0,63})$"
PATH_WARNING = s.ApiWarning(code="path_not_causal", message=DISCLAIMER["en"])
COMMUNITY_WARNING = s.ApiWarning(
    code="community_edited", message="Wikidata statements are community-edited and are not verified by REVA."
)


class LinksPathResponse(s.Envelope):
    data: dict[str, Any]


class CountryLinksResponse(s.Envelope):
    data: dict[str, Any]


def _origins(evidence: list[dict[str, Any]]) -> list[str]:
    return sorted({e.get("origin") or "live" for e in evidence})


def _envelope(origins: list[str], community: bool, t: Timer, rows: int, **detail: Any) -> dict[str, Any]:
    origin = origin_of(origins)
    warnings = ([COMMUNITY_WARNING] if community else []) + origin_warnings(origin)
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
) -> LinksPathResponse:
    import re

    t = Timer()
    for x in (*(kind or ()), *(exclude_kind or ())):
        if not re.match(KIND_PATTERN, x):
            raise ValidationProblem("invalid kind", kind=x[:64])
    for x in via or ():
        if not re.match(NODE_PATTERN, x):
            raise ValidationProblem("invalid waypoint", via=x[:64])
    if via and len(via) > MAX_WAYPOINTS:
        raise ValidationProblem(f"at most {MAX_WAYPOINTS} waypoints")
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
            )
    except service.UnknownCountry as e:
        raise NotFound("country not found in the knowledge graph (no ISO 3166 alpha-3 statement)", iso3=str(e)) from e
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
        env["warnings"].append(s.ApiWarning(code="truncated", message="search bounded: a connection may exist beyond"))
    return LinksPathResponse(data=data, **env)


@router.get(
    "/{ref}",
    response_model=CountryLinksResponse,
    responses={404: {"model": s.Problem}, 422: {"model": s.Problem}},
)
def get_country_links(
    request: Request,
    ref: str = Path(..., pattern=NODE_PATTERN, max_length=161, description="ISO 3166 alpha-3 or QID"),
    link_type: list[str] | None = Query(None, max_length=30),
    year: int | None = Query(None, ge=-5000, le=3000, description="Only links valid in that year"),
) -> CountryLinksResponse:
    t = Timer()
    try:
        with read_conn(request) as conn:
            data = service.links_for_country(conn, ref, link_types=link_type, year=year)
    except service.UnknownCountry as e:
        raise NotFound("country not found in the knowledge graph (no ISO 3166 alpha-3 statement)", iso3=str(e)) from e
    evidence = [e for lk in data["links"] for e in lk["evidence"]]
    env = _envelope(
        _origins(evidence),
        any(str(e.get("source", "")).startswith("wikidata") for e in evidence),
        t,
        len(data["links"]),
    )
    return CountryLinksResponse(data=data, **env)
