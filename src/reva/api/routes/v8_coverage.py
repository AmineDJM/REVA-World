"""V8 Couverture (agent V8COUVERTURE): what REVA has loaded and what is missing, per domain x place x period.

Aggregates are precomputed by ``uv run reva v8 coverage refresh`` (read-only on the DB; writes
``var/v8/coverage/coverage.json``); these routes only shape them and add two cheap live reads: atlas tables registered
since the aggregates were computed (staleness) and the connector acquisitions that complete queued loads. Counts say
what is *stored*; absence of a source is not absence of the phenomenon (every response carries that note).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from reva.api.auth import require_scope
from reva.api.deps import read_conn
from reva.api.errors import NotFound
from reva.v8.coverage import service as svc
from reva.v8.coverage.pending import PendingRegistry

router = APIRouter(prefix="/v1/v8/coverage", tags=["v8-coverage"], dependencies=[Depends(require_scope("read"))])


class _M(BaseModel):
    model_config = ConfigDict(extra="allow")


class CoverageMeta(_M):
    computed_at: str
    today: str
    origin: str
    atlas_tables: int
    atlas_rows: int
    kg_entities: int
    kg_statements: int
    kg_computed: bool
    places: int
    stale: dict[str, Any] | None = Field(None, description="Atlas tables registered since the aggregates were computed")
    note: str
    refresh_command: str


class MatrixColumn(_M):
    id: str
    label: str
    region: str
    countries: int


class DomainSummary(_M):
    tables: int
    rows: int
    countries: int
    latest_year: int | None
    last_loaded_at: str | None
    licences: dict[str, int]
    kg_entities: int
    catalog: dict[str, int] | None
    pending_loads: int
    pending_decisions: int


class MatrixRow(_M):
    id: str
    label: str
    summary: DomainSummary
    cells: dict[str, dict[str, Any]] = Field(
        description="column id -> counts; a column absent from `cells` has no stored row and no KG entity "
        "(not covered — which is not the same as 'zero')"
    )


class CoverageMatrix(_M):
    level: str
    meta: CoverageMeta
    columns: list[MatrixColumn]
    rows: list[MatrixRow]


class DomainDetail(_M):
    id: str
    label: str
    summary: DomainSummary
    tables: list[dict[str, Any]]
    countries: list[dict[str, Any]]
    countries_without_data: list[dict[str, Any]]
    kg_kinds: list[dict[str, Any]]
    hist: dict[str, int]
    connectors: list[dict[str, Any]]
    pending: list[dict[str, Any]]
    meta: CoverageMeta


class CountryDetail(_M):
    iso3: str
    label: str
    region: str
    subregion: str
    domains: list[dict[str, Any]]
    domains_without_data: list[str]
    tables: list[dict[str, Any]]
    kg_kinds: list[dict[str, Any]]
    meta: CoverageMeta


class Freshness(_M):
    meta: CoverageMeta
    domains: list[dict[str, Any]]
    never_loaded: list[dict[str, Any]]


class PendingGroup(_M):
    kind: str
    label: str
    items: list[dict[str, Any]]


class PendingView(_M):
    type: str
    updated: str
    checked_at: str | None
    counts: dict[str, int]
    disk_mb_open_loads: float
    disk_mb_unknown: int
    groups: list[PendingGroup]


def _cov() -> svc.Coverage:
    try:
        return svc.load()
    except svc.CoverageNotComputed as e:
        raise NotFound(
            "coverage aggregates not computed yet: run `uv run reva v8 coverage refresh`",
            code_hint="coverage_not_computed",
        ) from e


def _pending() -> PendingRegistry:
    reg = svc.safe_pending()
    if reg is None:
        raise NotFound("registry/coverage/pending.yaml is invalid (run `uv run reva v8 coverage check-pending`)")
    return reg


@router.get("/matrix", response_model=CoverageMatrix, summary="Domain x place coverage matrix (page bundle)")
def coverage_matrix(request: Request, level: str = Query("region", pattern="^(region|subregion|country)$")) -> Any:
    c = _cov()
    with read_conn(request) as conn:
        stale = svc.staleness(conn, c)
    return svc.matrix(c, level, svc.safe_pending(), stale)


@router.get("/domains/{domain}", response_model=DomainDetail, summary="One domain: tables, countries, KG kinds, gaps")
def coverage_domain(request: Request, domain: str = Path(pattern="^[a-z_]{2,40}$")) -> Any:
    c = _cov()
    if domain not in c.domain_ids:
        raise NotFound("unknown coverage domain", domain=domain, known=c.domain_ids)
    reg = svc.safe_pending()
    with read_conn(request) as conn:
        obs = svc.observations(conn, reg, c.doc["origin"]) if reg else {}
        stale = svc.staleness(conn, c)
    out = svc.domain_detail(c, domain, reg, obs)
    out["meta"]["stale"] = stale
    return out


@router.get("/countries/{iso3}", response_model=CountryDetail, summary="One country: coverage profile per domain")
def coverage_country(request: Request, iso3: str = Path(pattern="^[A-Z]{3}$")) -> Any:
    c = _cov()
    if iso3 not in c.places:
        raise NotFound("unknown country key (registry/entities/country_crosswalk.json)", iso3=iso3)
    out = svc.country_detail(c, iso3, svc.safe_pending())
    with read_conn(request) as conn:
        out["meta"]["stale"] = svc.staleness(conn, c)
    return out


@router.get("/freshness", response_model=Freshness, summary="Latest period and last load per table, by domain")
def coverage_freshness() -> Any:
    return svc.freshness(_cov())


@router.get("/pending", response_model=PendingView, summary="Queued loads, with a live check against the database")
def coverage_pending(request: Request) -> Any:
    reg = _pending()
    with read_conn(request) as conn:
        obs = svc.observations(conn, reg)
    return svc.pending_view(reg, obs, "load")


@router.get("/decisions", response_model=PendingView, summary="Owner decisions (licences, keys, permissions, ...)")
def coverage_decisions(request: Request) -> Any:
    reg = _pending()
    with read_conn(request) as conn:
        obs = svc.observations(conn, reg)
    return svc.pending_view(reg, obs, "decision")
