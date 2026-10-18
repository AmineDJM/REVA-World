"""V8 series (agent V8MONDE): catalog search, map slices, place series, multi-series and vintages.

Read-only over the pinned atlas snapshot (the atlas query layer; same snapshot rules as /v1/atlas: the latest
snapshot, or an older one whose query file exists). Every value is the exact decimal string a source reports, with
its source series, period, flags, raw artifact and licence; a place without a value is listed with its typed
reason (never zero); projections are flagged and never answer unless asked.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from reva.api import schemas as s
from reva.api.auth import require_scope
from reva.api.deps import origin_warnings, read_conn
from reva.api.routes.atlas import ORIGIN_Q, SNAPSHOT_Q, _catalog, _headlines, _licence_warnings, _query, _store
from reva.atlas.query import AtlasQuery
from reva.kernel.errors import ValidationProblem
from reva.v8.series import service as svc
from reva.v8.series.core import fold, parse_indicator
from reva.v8.series.index import SeriesIndex, index_for
from reva.v8.series.vintages import has_vintages, vintages_for

router = APIRouter(prefix="/v1/v8/series", tags=["v8"], dependencies=[Depends(require_scope("read"))])

NOTE = (
    "Atlas cells as each source reports them (exact decimal strings), one series per answer: no blending, imputation "
    "or zero-filling. A place without a value carries its reason. Projections (atlas projection rules) are flagged and "
    "excluded unless projections=include|only."
)


class _M(BaseModel):
    model_config = ConfigDict(extra="allow")


class SnapshotRef(_M):
    snapshot_sha256: str
    origin: str
    knowledge_cutoff: str


class CatalogHit(_M):
    kind: str = Field(description="concept | table | measure")
    id: str
    label: str
    sublabel: str | None = None
    table: str | None = None
    measure: str | None = None
    filters: list[dict[str, str]] = []
    series: str | None = None
    concept: str | None = None
    unit: str | None = None
    theme: str | None = None
    in_snapshot: bool = True


class CatalogResponse(_M):
    query: str
    hits: list[CatalogHit]
    concepts_total: int
    themes: dict[str, Any]
    snapshot: SnapshotRef
    took_ms: float


class TableMeasure(_M):
    measure: str
    variants: int
    label: str | None = None
    default_series: str


class TableResponse(_M):
    table: str
    measures: list[TableMeasure]
    measures_total: int
    snapshot: SnapshotRef


class MeasureResponse(_M):
    table: str
    measure: str
    facets: list[dict[str, Any]]
    variants: int
    resolved: dict[str, Any]
    snapshot: SnapshotRef


class MapResponse(_M):
    data: dict[str, Any]
    snapshot: SnapshotRef
    notes: list[str]
    warnings: list[s.ApiWarning] = []
    took_ms: float


class PlaceResponse(_M):
    data: dict[str, Any]
    snapshot: SnapshotRef
    notes: list[str]
    warnings: list[s.ApiWarning] = []
    took_ms: float


class MultiResponse(_M):
    data: dict[str, Any]
    snapshot: SnapshotRef
    notes: list[str]
    warnings: list[s.ApiWarning] = []
    took_ms: float


class VintagesResponse(_M):
    data: dict[str, Any]
    snapshot: SnapshotRef
    notes: list[str]
    took_ms: float


class PlacesResponse(_M):
    data: list[dict[str, Any]]


# --------------------------------------------------------------------------- helpers


#: (app, origin, snapshot) -> (AtlasQuery, monotonic expiry). Resolving the snapshot reads the latest snapshot ref,
#: re-verifies its manifest and the rights of every pinned table (~100 ms per request); a slider or an animation
#: asks for dozens of slices a minute, so the resolution is reused for SNAPSHOT_TTL seconds (every answer still
#: names its snapshot; a newer snapshot is picked up after the TTL).
SNAPSHOT_TTL = 30.0
_RESOLVED: dict[tuple[int, str | None, str | None], tuple[AtlasQuery, float]] = {}


def _ctx(request: Request, origin: str | None, snapshot: str | None) -> tuple[AtlasQuery, SeriesIndex]:
    key = (id(request.app), origin, snapshot)
    hit = _RESOLVED.get(key)
    now = time.monotonic()
    if hit is not None and hit[1] > now:
        q = hit[0]
    else:
        q = _query(request, origin, snapshot)
        _RESOLVED[key] = (q, now + SNAPSHOT_TTL)
    return q, index_for(q, _catalog())


def _snap(q: AtlasQuery) -> SnapshotRef:
    ref = q.snapshot.ref()
    return SnapshotRef(
        snapshot_sha256=ref["snapshot_sha256"], origin=ref["origin"], knowledge_cutoff=str(ref["knowledge_cutoff"])
    )


def _filters(f: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in f or []:
        if "=" not in item:
            raise ValidationProblem("a filter is key=value", filter=item[:80])
        k, v = item.split("=", 1)
        out[k] = v
    return out


def _cache(response: Response) -> None:
    response.headers["Cache-Control"] = "private, max-age=300"


SERIES_Q = Query(None, min_length=3, max_length=400, description="Atlas series <source>.<dataset>.<indicator>")
TABLE_Q = Query(None, pattern=r"^[a-z0-9][a-z0-9_-]{0,23}\.[a-z0-9][a-z0-9_-]{0,38}$", description="<source>.<dataset>")
MEASURE_Q = Query(None, max_length=300, description="Measure (indicator before its key=value filters)")
FILTER_Q = Query(None, max_length=12, description="Filters key=value (repeatable); open keys take their default")


# --------------------------------------------------------------------------- routes


@router.get("/catalog", response_model=CatalogResponse)
def catalog(
    request: Request,
    response: Response,
    q: str = Query("", max_length=120, description="Search text (labels fr/en, concept id, unit, theme, source, code)"),
    theme: str | None = Query(None, pattern="^[a-z][a-z0-9_]{0,39}$"),
    limit: int = Query(30, ge=1, le=100),
    origin: str | None = ORIGIN_Q,
    snapshot: str | None = SNAPSHOT_Q,
) -> CatalogResponse:
    """Atlas indicators matching a text: catalog concepts (labels, unit, theme, sources; the first source of each
    concept present in the snapshot), tables and raw measure codes."""
    t0 = time.perf_counter()
    aq, idx = _ctx(request, origin, snapshot)
    res = idx.search(q, theme=theme, limit=limit)
    hits: list[CatalogHit] = []
    for h in res["concepts"]:
        c = h.concept
        prim = idx.primary_series(c)
        meas: str | None = None
        dims: tuple[tuple[str, str], ...] = ()
        if prim:
            meas, dims = parse_indicator(prim[2])
        hits.append(
            CatalogHit(
                kind="concept",
                id=c.id,
                label=c.labels.get("fr") or c.labels.get("en") or c.id,
                sublabel=f"{c.unit} · {prim[0]}.{prim[1]}" if prim else f"{c.unit} · non chargé dans cet instantané",
                table=f"{prim[0]}.{prim[1]}" if prim else None,
                measure=meas,
                filters=[{"key": k, "value": v} for k, v in dims],
                series=f"{prim[0]}.{prim[1]}.{prim[2]}" if prim else None,
                concept=c.id,
                unit=c.unit,
                theme=c.theme,
                in_snapshot=prim is not None,
            )
        )
    for t in res["tables"]:
        hits.append(
            CatalogHit(kind="table", id=t.key, label=t.key, sublabel=f"table · {len(t.measures)} mesures", table=t.key)
        )
    for m in res["measures"]:
        v = m.default()
        series = f"{m.table}.{v.indicator}"
        concept = idx.concept_for(series)
        hits.append(
            CatalogHit(
                kind="measure",
                id=f"{m.table}:{m.measure}",
                label=((concept.labels.get("fr") or concept.labels.get("en")) if concept else None) or m.measure,
                sublabel=f"{m.table} · {m.measure}"
                + (f" · {len(m.variants)} variantes" if len(m.variants) > 1 else ""),
                table=m.table,
                measure=m.measure,
                filters=[{"key": k, "value": val} for k, val in v.dims],
                series=series,
                concept=concept.id if concept else None,
                unit=concept.unit if concept else None,
            )
        )
    _cache(response)
    return CatalogResponse(
        query=q,
        hits=hits,
        concepts_total=res["concepts_total"],
        themes=_catalog().themes,
        snapshot=_snap(aq),
        took_ms=round((time.perf_counter() - t0) * 1000, 1),
    )


@router.get("/table", response_model=TableResponse)
def table(
    request: Request,
    response: Response,
    table: str = Query(..., pattern=r"^[a-z0-9][a-z0-9_-]{0,23}\.[a-z0-9][a-z0-9_-]{0,38}$"),
    q: str = Query("", max_length=120),
    limit: int = Query(200, ge=1, le=1000),
    origin: str | None = ORIGIN_Q,
    snapshot: str | None = SNAPSHOT_Q,
) -> TableResponse:
    """Measures of one atlas table (labels from catalog concepts when a concept reads the series)."""
    aq, idx = _ctx(request, origin, snapshot)
    t = idx.tables.get(table)
    if t is None:
        raise ValidationProblem("table not in this atlas snapshot", table=table)
    needle = fold(q)
    ms = [m for m in t.measures.values() if not needle or needle in m.folded]
    ms.sort(key=lambda m: m.measure)
    out = []
    for m in ms[:limit]:
        v = m.default()
        c = idx.concept_for(f"{table}.{v.indicator}")
        out.append(
            TableMeasure(
                measure=m.measure,
                variants=len(m.variants),
                label=(c.labels.get("fr") or c.labels.get("en")) if c else None,
                default_series=f"{table}.{v.indicator}",
            )
        )
    _cache(response)
    return TableResponse(table=table, measures=out, measures_total=len(ms), snapshot=_snap(aq))


@router.get("/measure", response_model=MeasureResponse)
def measure(
    request: Request,
    response: Response,
    table: str = Query(..., pattern=r"^[a-z0-9][a-z0-9_-]{0,23}\.[a-z0-9][a-z0-9_-]{0,38}$"),
    measure: str = Query(..., max_length=300),
    f: list[str] | None = FILTER_Q,
    origin: str | None = ORIGIN_Q,
    snapshot: str | None = SNAPSHOT_Q,
) -> MeasureResponse:
    """Filter facets of one measure and the series the given filters resolve to."""
    aq, idx = _ctx(request, origin, snapshot)
    m = idx.measure(table, measure)
    if m is None:
        raise ValidationProblem("unknown table/measure in this snapshot", table=table, measure=measure[:120])
    _sid, src, ds, ind = svc.resolve(idx, table=table, measure=measure, filters=_filters(f))
    _cache(response)
    return MeasureResponse(
        table=table,
        measure=measure,
        facets=m.facets(),
        variants=len(m.variants),
        resolved={"series": f"{src}.{ds}.{ind}", "indicator": ind},
        snapshot=_snap(aq),
    )


@router.get("/map", response_model=MapResponse)
def map_slice(
    request: Request,
    response: Response,
    series: str | None = SERIES_Q,
    table: str | None = TABLE_Q,
    measure: str | None = MEASURE_Q,
    f: list[str] | None = FILTER_Q,
    period: str | None = Query(None, pattern=r"^-?\d{1,5}(-(Q[1-4]|S[12]|\d{2}(-\d{2})?))?$"),
    precision: str | None = Query(None, pattern="^(year|semester|quarter|month|day)$"),
    level: str = Query("adm0", pattern="^(adm0|adm1)$"),
    projections: str = Query("exclude", pattern="^(exclude|include|only)$"),
    origin: str | None = ORIGIN_Q,
    snapshot: str | None = SNAPSHOT_Q,
) -> MapResponse:
    """One series, one period, every place: value cells (with source, period, flags, raw artifact, licence), the
    reason of every country without a value, the periods of the series at its precision and a scale hint."""
    t0 = time.perf_counter()
    aq, idx = _ctx(request, origin, snapshot)
    sid, *_ = svc.resolve(idx, series=series, table=table, measure=measure, filters=_filters(f))
    data = svc.map_slice(aq, idx, sid, precision=precision, period=period, level=level, projections=projections)
    _cache(response)
    warnings = origin_warnings(aq.snapshot.origin) + _licence_warnings(data["licences"])
    if level == "adm1":
        warnings.append(
            s.ApiWarning(
                code="no_subnational_geometry",
                message="REVA serves no sub-national display geometry: ADM1/NUTS values are listed, not mapped.",
            )
        )
    return MapResponse(
        data=data,
        snapshot=_snap(aq),
        notes=[NOTE],
        warnings=warnings,
        took_ms=round((time.perf_counter() - t0) * 1000, 1),
    )


@router.get("/place", response_model=PlaceResponse)
def place(
    request: Request,
    response: Response,
    geo: str = Query(..., pattern=r"^[A-Za-z0-9_.-]{1,40}$", description="ISO3 or provider geography"),
    series: str | None = SERIES_Q,
    table: str | None = TABLE_Q,
    measure: str | None = MEASURE_Q,
    f: list[str] | None = FILTER_Q,
    precision: str | None = Query(None, pattern="^(year|semester|quarter|month|day)$"),
    origin: str | None = ORIGIN_Q,
    snapshot: str | None = SNAPSHOT_Q,
) -> PlaceResponse:
    """One place's cells of one series for every period (projections flagged)."""
    t0 = time.perf_counter()
    aq, idx = _ctx(request, origin, snapshot)
    sid, *_ = svc.resolve(idx, series=series, table=table, measure=measure, filters=_filters(f))
    g = geo.upper() if len(geo) == 3 and geo.isalpha() else geo
    data = svc.place_series(aq, idx, sid, g, precision=precision)
    _cache(response)
    return PlaceResponse(
        data=data,
        snapshot=_snap(aq),
        notes=[NOTE],
        warnings=origin_warnings(aq.snapshot.origin) + _licence_warnings(data["licences"]),
        took_ms=round((time.perf_counter() - t0) * 1000, 1),
    )


@router.get("/multi", response_model=MultiResponse)
def multi(
    request: Request,
    response: Response,
    s_: list[str] = Query(..., alias="s", min_length=1, max_length=6, description="Series keys (1-6)"),
    g: list[str] = Query(..., min_length=1, max_length=12, description="ISO3 countries (1-12)"),
    population: bool = Query(False, description="Add the atlas headline population of the countries (per capita)"),
    origin: str | None = ORIGIN_Q,
    snapshot: str | None = SNAPSHOT_Q,
) -> MultiResponse:
    """N series x M countries aligned by period, with the transforms the inputs allow (index 100, year-on-year,
    per capita) and, on request, the population series a per-capita computation divides by."""
    t0 = time.perf_counter()
    aq, idx = _ctx(request, origin, snapshot)
    sids = []
    for key in s_:
        sid, *_ = svc.resolve(idx, series=key)
        if sid not in sids:
            sids.append(sid)
    geos = []
    for x in g:
        for part in x.split(","):
            p = part.strip().upper()
            if len(p) == 3 and p.isalpha() and p not in geos:
                geos.append(p)
    data = dict(svc.multi_series(aq, idx, sids, geos[:12], population=population, headlines=_headlines()))
    data["series"] = [{**ser, "vintages_available": has_vintages(ser["meta"]["table"])} for ser in data["series"]]
    _cache(response)
    lic = list(data["licences"]) + list((data.get("population") or {}).get("licences") or [])
    return MultiResponse(
        data=data,
        snapshot=_snap(aq),
        notes=[NOTE],
        warnings=origin_warnings(aq.snapshot.origin) + _licence_warnings(lic),
        took_ms=round((time.perf_counter() - t0) * 1000, 1),
    )


@router.get("/vintages", response_model=VintagesResponse)
def vintages(
    request: Request,
    response: Response,
    series: str = Query(..., min_length=3, max_length=400),
    g: list[str] = Query(..., min_length=1, max_length=12),
    origin: str | None = ORIGIN_Q,
    snapshot: str | None = SNAPSHOT_Q,
) -> VintagesResponse:
    """Published vintages of a series for some countries (as first published; each vintage as published)."""
    t0 = time.perf_counter()
    aq, idx = _ctx(request, origin, snapshot)
    _sid, src, ds, ind = svc.resolve(idx, series=series)
    meas, _dims = parse_indicator(ind)
    isos = [p.strip().upper() for x in g for p in x.split(",") if len(p.strip()) == 3]
    with read_conn(request) as conn:
        data = vintages_for(conn, _store(request), f"{src}.{ds}", meas, isos)
    _cache(response)
    return VintagesResponse(
        data=data,
        snapshot=_snap(aq),
        notes=[
            "Millésimes tels que publiés par le fournisseur ; les projections d’un millésime restent des projections."
        ],
        took_ms=round((time.perf_counter() - t0) * 1000, 1),
    )


@router.get("/places", response_model=PlacesResponse)
def places(response: Response) -> PlacesResponse:
    """The country referential (ISO3, ISO2, labels, UN region) used by Monde and Comparer."""
    _cache(response)
    return PlacesResponse(data=svc.places())
