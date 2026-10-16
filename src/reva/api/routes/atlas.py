"""Atlas (V3): concepts, country dossiers, series, maps and coverage over a pinned atlas snapshot.

Every data answer is computed from one recorded atlas snapshot (``reva.atlas.snapshot``) and
names it (``snapshot``: manifest sha256, origin, knowledge cutoff). Cells are what a source
reports: the best-available view picks by the concept's declared source order and always names
the source, its flags and the raw artifact + selector; nothing is blended, imputed or zero-filled.
Catalog concepts are provider-declared, not human-reviewed (``review_status``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from reva.api import schemas as s
from reva.api.auth import require_scope
from reva.api.deps import origin_warnings, read_conn
from reva.api.errors import NotFound
from reva.atlas.catalog import Catalog, cached_catalog
from reva.atlas.query import AtlasQuery, query_for_request
from reva.atlas.source_order import HeadlineRegistry, cached_headlines
from reva.atlas.tables import geo_index

router = APIRouter(prefix="/v1/atlas", tags=["atlas"], dependencies=[Depends(require_scope("read"))])

ORIGIN_Q = Query(
    None,
    pattern="^(live|test_fixture)$",
    description="Snapshot origin (default: live in production, else the latest snapshot of any origin)",
)
SNAPSHOT_Q = Query(
    None,
    pattern="^[0-9a-f]{64}$",
    description="A recorded atlas snapshot (manifest sha256). The latest snapshot is always readable; an older one "
    "only when its query file already exists (409 snapshot_not_materialised otherwise: query files are built by "
    "the worker, never inside a request)",
)
PROJECTIONS_Q = Query(
    "exclude",
    pattern="^(exclude|include)$",
    description="exclude (default): projections, nowcasts and extrapolations (provider flags or a documented "
    "registry rule, e.g. WDI values after the UN WPP estimate horizon) never answer; include: they answer where "
    "no actual value exists, flagged (projection: true, flags.estimate_kind)",
)
CELL_NOTE = (
    "Atlas cells are what each source reports. The best-available value is the first source in the concept's "
    "declared source order that reports the cell; no blending, imputation or zero-filling. Concepts are "
    "provider-declared (not human-reviewed)."
)


class _M(BaseModel):
    model_config = ConfigDict(extra="allow", json_schema_serialization_defaults_required=True)


class AtlasSnapshotRef(_M):
    snapshot_sha256: str
    origin: str
    knowledge_cutoff: datetime
    tables: int
    rows: int


class AtlasRights(_M):
    licence: str = Field(description="Licence or terms as recorded with the source table (e.g. CC-BY-NC-SA-4.0)")
    licence_url: str | None = None
    attribution: str | None = None
    commercial_use: str = Field(description="permitted | not_permitted | restricted | unknown")
    non_commercial: bool
    share_alike: bool
    no_derivatives: bool
    open_licence: bool | None
    restricted: bool = Field(description="Any non-commercial, share-alike, no-derivatives or not-open term")
    basis: str = Field(description="table_artifact (recorded with the table) | atlas_source_spec (source default)")
    restriction: str | None = None


class AtlasLicence(AtlasRights):
    source: str


class AtlasProvenance(_M):
    raw_sha256: str = Field(description="sha256 of the archived raw artifact the cell was parsed from")
    selector: dict[str, Any] = Field(description="Locator of the cell inside the raw artifact")
    table_artifact_sha256: str
    dataset_key: str | None
    dataset_id: str | None


class AtlasCell(_M):
    iso3: str | None = None
    year: int | None = None
    value: str | None = Field(description="Exact decimal string as reported; null when the cell is textual or missing")
    value_text: str | None
    unit: str
    source: str
    dataset: str
    indicator: str
    series: str = Field(description="<source>.<dataset>.<indicator>")
    provider_geo: str
    entity_qid: str | None
    period_start: str = Field(
        description="ISO 8601 date, proleptic Gregorian, astronomical years (year 0 = 1 BCE; -9999-01-01 = 10000 BCE)"
    )
    period_end: str = Field(description="Exclusive end (half-open period), same convention as period_start")
    period_basis: str | None = Field(
        None, description="calendar_year | sub_annual | twelve_month_window | multi_year (the cell's true period)"
    )
    flags: dict[str, Any]
    provenance: AtlasProvenance
    rights: AtlasRights | None = Field(None, description="Licence and terms of the source table of this cell")
    source_rank: int | None = Field(None, description="Position of the series in the concept's source order")
    sources_reporting: int | None = Field(None, description="How many declared sources report this cell")


class AtlasConcept(_M):
    id: str
    labels: dict[str, str]
    definition: str
    unit: str
    domain: str
    subdomain: str | None
    theme: str | None = Field(None, description="REVA theme (registry/atlas/themes.yaml)")
    value_kind: str
    source_order: list[str]
    provider_declared: bool
    review_status: str
    notes: str | None
    tags: list[str]
    catalog_file: str


class AtlasConceptList(_M):
    data: list[AtlasConcept]
    total: int
    domains: dict[str, Any]
    themes: dict[str, dict[str, str]] = {}
    unthemed: list[str] = []
    catalog_errors: list[dict[str, str]]
    warnings: list[s.ApiWarning] = []


class AtlasSeriesPoint(_M):
    year: int
    value: str | None
    value_text: str | None
    unit: str | None = Field(None, description="The cell's own unit (a series can switch currency or unit)")
    period_start: str | None = None
    period_end: str | None = None
    period_basis: str | None = None
    series: str
    flags: dict[str, Any]


class AtlasDossierConcept(_M):
    concept_id: str
    labels: dict[str, str]
    unit: str
    subdomain: str | None
    theme: str | None = None
    latest: AtlasCell
    years: int
    year_min: int
    year_max: int
    sources_used: list[str]
    licences: list[AtlasLicence] = []
    series: list[AtlasSeriesPoint] | None = None


class AtlasDossierDomain(_M):
    domain: str
    concepts: list[AtlasDossierConcept]
    without_data: list[str]


class AtlasConsulted(_M):
    series: str = Field(description="<source>.<dataset>.<indicator>, or 'derived: <expression>'")
    source_rank: int
    status: str = Field(
        description="answered | also_reported | no_value | projection_only | projection_upstream | nowcast | "
        "extrapolated (a registry cell rule marks the cell) | not_equivalent_for_country | unit_mismatch | "
        "series_not_in_snapshot"
    )
    reason: str | None = Field(None, description="Why the series is not equivalent for this country")
    rule: str | None = Field(None, description="The registry cell rule that marks the cell")


class AtlasAnsweredBy(_M):
    series: str
    source: str
    dataset: str
    indicator: str
    source_rank: int
    justification: str | None


class AtlasHeadlineCell(_M):
    concept_id: str
    iso3: str
    year: int
    value: str = Field(description="Exact decimal string in the concept unit (reported value x declared exact scale)")
    unit: str
    method: str = Field(description="reported (one listed series answered) or derived (exact formula of two inputs)")
    projection: bool = Field(
        False, description="A projection, nowcast or extrapolation (only with projections=include)"
    )
    source_break: dict[str, Any] | None = Field(
        None,
        description="Set where the answering series changes within this country's series: from, to, from_year, "
        "year and the overlap year's values of both series (never read the step as a change)",
    )
    answered_by: AtlasAnsweredBy | None
    reported: dict[str, Any] | None = Field(description="The value and unit exactly as the answering source reports")
    provider_geo: str | None
    period_start: str
    period_end: str
    period_basis: str | None = None
    flags: dict[str, Any]
    provenance: AtlasProvenance | None
    rights: AtlasRights | None = Field(None, description="Licence and terms of the answering source")
    consulted: list[AtlasConsulted] = Field(description="Every listed series in order, with what it had for this cell")
    derived: dict[str, Any] | None = Field(None, description="formula, expression, precision and the two input cells")


class AtlasHeadlineDossierConcept(_M):
    concept_id: str
    theme: str
    labels: dict[str, str]
    unit: str
    latest: AtlasHeadlineCell
    years: int
    year_min: int
    year_max: int
    sources_used: list[str]
    source_breaks: list[dict[str, Any]] = []
    licences: list[AtlasLicence] = []
    series: list[dict[str, Any]] | None = None


class AtlasHeadlineMissing(_M):
    concept_id: str
    theme: str
    consulted: list[AtlasConsulted]


class AtlasHeadlineDossier(_M):
    concepts: list[AtlasHeadlineDossierConcept]
    without_data: list[AtlasHeadlineMissing]
    projections: str = "exclude"
    licences: list[AtlasLicence] = []


class AtlasDossier(_M):
    iso3: str
    label: str | None
    headline: AtlasHeadlineDossier | None = Field(
        None, description="REVA headline concepts: cross-source best value per year, naming who answered"
    )
    domains: list[AtlasDossierDomain]
    concepts_with_data: int
    concepts_without_data: int
    non_annual_cells: int
    licences: list[AtlasLicence] = Field([], description="Licence of every source used, restricted first")


class AtlasDossierResponse(_M):
    data: AtlasDossier
    snapshot: AtlasSnapshotRef
    notes: list[str]
    catalog_errors: list[dict[str, str]]
    warnings: list[s.ApiWarning] = []


class AtlasSourceCells(_M):
    series: str
    source: str
    dataset: str
    indicator: str
    source_rank: int | None
    in_source_order: bool
    cells: list[AtlasCell]


class AtlasSeries(_M):
    concept: AtlasConcept
    iso3: str
    best: list[AtlasCell]
    per_source: list[AtlasSourceCells]
    sources_without_data: list[str]
    licences: list[AtlasLicence] = []


class AtlasSeriesResponse(_M):
    data: AtlasSeries
    snapshot: AtlasSnapshotRef
    notes: list[str]
    warnings: list[s.ApiWarning] = []


class AtlasMap(_M):
    concept: AtlasConcept
    year: int
    values: list[AtlasCell]
    countries_with_value: int
    countries_without_value: list[str]
    countries_with_other_period_cells: list[str] = Field(
        [], description="Countries with cells overlapping the year that the year view does not attach (never absent)"
    )
    by_source: dict[str, int]
    licences: list[AtlasLicence] = []


class AtlasMapResponse(_M):
    data: AtlasMap
    snapshot: AtlasSnapshotRef
    notes: list[str]
    warnings: list[s.ApiWarning] = []


class AtlasSourceCoverage(_M):
    series: str
    source_rank: int
    countries: int
    cells: int
    year_min: int | None
    year_max: int | None


class AtlasConceptCoverage(_M):
    concept_id: str
    domain: str
    theme: str | None = None
    countries: int
    cells: int
    year_min: int | None
    year_max: int | None
    by_source: list[AtlasSourceCoverage]


class AtlasCountryCoverage(_M):
    iso3: str
    years: int
    year_min: int | None
    year_max: int | None
    best_sources_used: list[str]


class AtlasCoverageResponse(_M):
    data: list[AtlasConceptCoverage]
    by_country: list[AtlasCountryCoverage] | None = None
    snapshot: AtlasSnapshotRef
    countries_total: int
    catalog_errors: list[dict[str, str]]
    warnings: list[s.ApiWarning] = []


class AtlasHeadlineConcept(_M):
    id: str
    theme: str
    labels: dict[str, str]
    definition: str
    unit: str
    projections: str
    series: list[dict[str, Any]]
    derived: dict[str, Any] | None
    caveats: str | None
    not_equivalent: list[dict[str, str]]
    related_catalog_concepts: list[str]
    review_status: str
    file: str


class AtlasHeadlineConceptList(_M):
    data: list[AtlasHeadlineConcept]
    total: int
    themes: dict[str, dict[str, str]]
    errors: list[dict[str, str]]
    notes: list[str]


class AtlasHeadlineMapMissing(_M):
    iso3: str
    consulted: list[AtlasConsulted]


class AtlasHeadlineMap(_M):
    concept: AtlasHeadlineConcept
    year: int
    values: list[AtlasHeadlineCell]
    countries_with_value: int
    countries_without_value: list[AtlasHeadlineMapMissing]
    countries_with_other_period_cells: list[str] = []
    by_source: dict[str, int]
    projections: str = "exclude"
    projection_values: int = 0
    licences: list[AtlasLicence] = []


class AtlasHeadlineMapResponse(_M):
    data: AtlasHeadlineMap
    snapshot: AtlasSnapshotRef
    notes: list[str]
    warnings: list[s.ApiWarning] = []


class AtlasHeadlineSeries(_M):
    concept: AtlasHeadlineConcept
    iso3: str
    best: list[AtlasHeadlineCell]
    by_series: list[dict[str, Any]]
    derived_years: int
    sources_without_data: list[str]
    projections: str = "exclude"
    source_breaks: list[dict[str, Any]] = Field(
        [], description="Every change of answering series in this series (from, to, years, overlap values)"
    )
    projection_years: list[int] = []
    licences: list[AtlasLicence] = []


class AtlasHeadlineSeriesResponse(_M):
    data: AtlasHeadlineSeries
    snapshot: AtlasSnapshotRef
    notes: list[str]
    warnings: list[s.ApiWarning] = []


class AtlasHeadlineCoverage(_M):
    concept_id: str
    theme: str
    countries: int
    cells: int
    year_min: int | None
    year_max: int | None
    derived_cells: int
    latest_year_answered_by: dict[str, int]
    countries_answered_by: dict[str, int]
    series_in_snapshot: list[str]
    series_not_in_snapshot: list[str]
    countries_without_value: list[str]


class AtlasHeadlineCoverageResponse(_M):
    data: list[AtlasHeadlineCoverage]
    snapshot: AtlasSnapshotRef
    countries_total: int
    notes: list[str]
    warnings: list[s.ApiWarning] = []


HEADLINE_NOTE = (
    "REVA headline concepts list, per concept, provider series that define the same quantity the same way "
    "(each with a written justification; non-equivalent series are listed with the reason and never answer). "
    "Per country and calendar year the first listed series reporting a value answers (projections excluded "
    "unless the concept allows them); 'consulted' names every listed series and what it had. A 'derived' value "
    "is an exact formula of two cited inputs of the same country and year, used only when no listed series "
    "reports the cell. The equivalences are agent-declared, not human-reviewed. A listed series may be declared "
    "not equivalent for some countries (e.g. another territorial scope) and never answers for them; cells a "
    "documented registry rule marks as an upstream projection, nowcast or extrapolation (e.g. WDI values after "
    "the UN WPP estimate horizon) are excluded unless projections=include. Where the answering series changes "
    "inside a country's series, the cell carries source_break with both series' values of the overlap year."
)


# --------------------------------------------------------------------------- helpers
def _store(request: Request) -> Any:
    st = getattr(request.app.state, "atlas_store", None)
    if st is None:
        from reva.adapters.objects.factory import build_store

        st = build_store(request.app.state.settings)
        request.app.state.atlas_store = st
    return st


def _query(request: Request, origin: str | None, snapshot: str | None) -> AtlasQuery:
    # ATLAS-5: in production an unspecified origin means live (a fixture snapshot never shadows it)
    if origin is None and snapshot is None and getattr(request.app.state.settings, "reva_env", "") == "production":
        origin = "live"
    # SEC-3: only the latest snapshot's query file may be built here; another snapshot must already exist
    with read_conn(request) as conn:
        return query_for_request(conn, _store(request), origin=origin, sha256=snapshot)


def _licence_warnings(licences: list[dict[str, Any]]) -> list[s.ApiWarning]:
    """LIC-1: one warning naming every restricted licence among the sources of the answer."""
    bad = [x for x in licences if x.get("restricted")]
    if not bad:
        return []
    parts = []
    for x in bad:
        terms = [
            t
            for t, on in (
                ("non-commercial", x.get("non_commercial")),
                ("share-alike", x.get("share_alike")),
                ("no derivatives", x.get("no_derivatives")),
                ("no charging for access", x.get("commercial_use") == "restricted"),
                ("not an open licence", x.get("open_licence") is False),
            )
            if on
        ]
        parts.append(f"{x['source']}: {x['licence']} ({', '.join(terms)})")
    return [
        s.ApiWarning(
            code="licence_restricted",
            message=(
                "Some values come from sources under restricted terms; reuse must respect them: " + "; ".join(parts)
            )[:1000],
        )
    ]


def _snap(q: AtlasQuery) -> AtlasSnapshotRef:
    return AtlasSnapshotRef(**q.snapshot.ref())


def _catalog() -> Catalog:
    return cached_catalog()


def _headlines() -> HeadlineRegistry:
    return cached_headlines()


def _headline(reg: HeadlineRegistry, concept: str) -> str:
    if concept not in reg.concepts:
        raise NotFound("unknown headline concept", concept=concept[:200])
    return concept


def _catalog_warnings(cat: Catalog) -> list[s.ApiWarning]:
    if not cat.errors:
        return []
    files = sorted({e["file"] for e in cat.errors})
    return [
        s.ApiWarning(code="atlas_catalog_invalid_files", message=f"Skipped invalid catalog files: {', '.join(files)}")
    ]


def _country(iso3: str) -> tuple[str, str | None]:
    key = iso3.upper()
    idx = geo_index()
    if key not in idx.keys:
        raise NotFound("country not in the crosswalk", iso3=iso3[:8])
    return key, _labels().get(key)


_LABELS: dict[str, str] = {}


def _labels() -> dict[str, str]:
    if not _LABELS:
        import json

        from reva.atlas.tables import CROSSWALK_PATH

        _LABELS.update({e["key"]: e["label"] for e in json.loads(CROSSWALK_PATH.read_text())["entities"]})
    return _LABELS


def _concept(cat: Catalog, concept: str) -> str:
    if concept not in cat.concepts:
        raise NotFound("unknown atlas concept", concept=concept[:200])
    return concept


# --------------------------------------------------------------------------- routes
@router.get("/concepts", response_model=AtlasConceptList)
def list_concepts(
    domain: str | None = Query(None, pattern="^[a-z][a-z0-9_]{0,39}$"),
    subdomain: str | None = Query(None, pattern="^[a-z][a-z0-9_]{0,59}$"),
    q: str | None = Query(
        None, min_length=1, max_length=100, description="Case-insensitive search (id, labels, definition)"
    ),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    theme: str | None = Query(None, pattern="^[a-z][a-z0-9_]{0,39}$", description="REVA theme"),
) -> AtlasConceptList:
    cat = _catalog()
    hits = cat.search(q, domain, subdomain, theme)
    return AtlasConceptList(
        data=[AtlasConcept(**c.as_dict()) for c in hits[offset : offset + limit]],
        total=len(hits),
        domains=cat.domains,
        themes=cat.themes,
        unthemed=cat.unthemed,
        catalog_errors=cat.errors,
        warnings=_catalog_warnings(cat),
    )


@router.get("/countries/{iso3}", response_model=AtlasDossierResponse)
def country_dossier(
    request: Request,
    iso3: str = Path(..., pattern="^[A-Za-z]{3}$"),
    domain: str | None = Query(None, pattern="^[a-z][a-z0-9_]{0,39}$"),
    series: bool = Query(True, description="Include annual best-available series per concept"),
    origin: str | None = ORIGIN_Q,
    snapshot: str | None = SNAPSHOT_Q,
    theme: str | None = Query(None, pattern="^[a-z][a-z0-9_]{0,39}$", description="REVA theme"),
    headline: bool = Query(True, description="Include the REVA headline concepts (cross-source best value)"),
    projections: str = PROJECTIONS_Q,
) -> AtlasDossierResponse:
    key, label = _country(iso3)
    cat = _catalog()
    q = _query(request, origin, snapshot)
    d = q.dossier(cat, key, domain=domain, include_series=series, theme=theme)
    h = (
        q.headline_dossier(_headlines(), key, theme=theme, include_series=series, projections=projections)
        if headline
        else None
    )
    lic = list(d.get("licences") or []) + list((h or {}).get("licences") or [])
    return AtlasDossierResponse(
        data=AtlasDossier(label=label, headline=AtlasHeadlineDossier(**h) if h is not None else None, **d),
        snapshot=_snap(q),
        notes=[CELL_NOTE, HEADLINE_NOTE] if headline else [CELL_NOTE],
        catalog_errors=cat.errors,
        warnings=origin_warnings(q.snapshot.origin) + _catalog_warnings(cat) + _licence_warnings(lic),
    )


@router.get("/series", response_model=AtlasSeriesResponse)
def concept_series(
    request: Request,
    concept: str = Query(..., min_length=1, max_length=200),
    iso3: str = Query(..., pattern="^[A-Za-z]{3}$"),
    origin: str | None = ORIGIN_Q,
    snapshot: str | None = SNAPSHOT_Q,
) -> AtlasSeriesResponse:
    key, _ = _country(iso3)
    cat = _catalog()
    _concept(cat, concept)
    q = _query(request, origin, snapshot)
    data = q.series(cat, concept, key)
    return AtlasSeriesResponse(
        data=AtlasSeries(**data),
        snapshot=_snap(q),
        notes=[CELL_NOTE],
        warnings=origin_warnings(q.snapshot.origin) + _licence_warnings(data["licences"]),
    )


@router.get("/map", response_model=AtlasMapResponse)
def concept_map(
    request: Request,
    concept: str = Query(..., min_length=1, max_length=200),
    year: int = Query(..., ge=-10000, le=3000),
    origin: str | None = ORIGIN_Q,
    snapshot: str | None = SNAPSHOT_Q,
) -> AtlasMapResponse:
    cat = _catalog()
    _concept(cat, concept)
    q = _query(request, origin, snapshot)
    data = q.map_values(cat, concept, year)
    return AtlasMapResponse(
        data=AtlasMap(**data),
        snapshot=_snap(q),
        notes=[CELL_NOTE, "countries_without_value: no declared source reports the cell (absent, not zero)."],
        warnings=origin_warnings(q.snapshot.origin) + _licence_warnings(data["licences"]),
    )


@router.get("/coverage", response_model=AtlasCoverageResponse)
def coverage(
    request: Request,
    domain: str | None = Query(None, pattern="^[a-z][a-z0-9_]{0,39}$"),
    concept: str | None = Query(None, min_length=1, max_length=200, description="Adds the per-country matrix"),
    origin: str | None = ORIGIN_Q,
    snapshot: str | None = SNAPSHOT_Q,
    theme: str | None = Query(None, pattern="^[a-z][a-z0-9_]{0,39}$", description="REVA theme"),
) -> AtlasCoverageResponse:
    cat = _catalog()
    if concept is not None:
        _concept(cat, concept)
    q = _query(request, origin, snapshot)
    rows = q.coverage(cat, domain, theme)
    if concept is not None:
        rows = [r for r in rows if r["concept_id"] == concept] or [
            r for r in q.coverage(cat, cat.concepts[concept].domain) if r["concept_id"] == concept
        ]
    return AtlasCoverageResponse(
        data=[AtlasConceptCoverage(**r) for r in rows],
        by_country=[AtlasCountryCoverage(**r) for r in q.coverage_by_country(cat, concept)] if concept else None,
        snapshot=_snap(q),
        countries_total=len(geo_index().keys),
        catalog_errors=cat.errors,
        warnings=origin_warnings(q.snapshot.origin) + _catalog_warnings(cat),
    )


# --------------------------------------------------------------------------- headline concepts (ATLASOPS)
@router.get("/headline/concepts", response_model=AtlasHeadlineConceptList)
def headline_concepts(
    theme: str | None = Query(None, pattern="^[a-z][a-z0-9_]{0,39}$", description="REVA theme"),
) -> AtlasHeadlineConceptList:
    """REVA headline concepts: the declared cross-source equivalences (series, justifications, exclusions)."""
    reg = _headlines()
    hits = sorted(reg.by_theme(theme), key=lambda c: (c.theme, c.id))
    return AtlasHeadlineConceptList(
        data=[AtlasHeadlineConcept(**c.as_dict()) for c in hits],
        total=len(hits),
        themes=_catalog().themes,
        errors=reg.errors,
        notes=[HEADLINE_NOTE],
    )


@router.get("/headline/map", response_model=AtlasHeadlineMapResponse)
def headline_map(
    request: Request,
    concept: str = Query(..., min_length=1, max_length=200),
    year: int = Query(..., ge=-10000, le=3000),
    origin: str | None = ORIGIN_Q,
    snapshot: str | None = SNAPSHOT_Q,
    projections: str = PROJECTIONS_Q,
) -> AtlasHeadlineMapResponse:
    """Best value of a headline concept for every country in one year, with the answering source per
    country and, for countries without a value, every series consulted and what it had."""
    reg = _headlines()
    _headline(reg, concept)
    q = _query(request, origin, snapshot)
    data = q.headline_map(reg, concept, year, projections=projections)
    return AtlasHeadlineMapResponse(
        data=AtlasHeadlineMap(**data),
        snapshot=_snap(q),
        notes=[HEADLINE_NOTE],
        warnings=origin_warnings(q.snapshot.origin) + _licence_warnings(data["licences"]),
    )


@router.get("/headline/series", response_model=AtlasHeadlineSeriesResponse)
def headline_series(
    request: Request,
    concept: str = Query(..., min_length=1, max_length=200),
    iso3: str = Query(..., pattern="^[A-Za-z]{3}$"),
    origin: str | None = ORIGIN_Q,
    snapshot: str | None = SNAPSHOT_Q,
    projections: str = PROJECTIONS_Q,
) -> AtlasHeadlineSeriesResponse:
    """Best annual series of a headline concept for one country (source named per year; every change of
    answering series flagged ``source_break``)."""
    key, _ = _country(iso3)
    reg = _headlines()
    _headline(reg, concept)
    q = _query(request, origin, snapshot)
    data = q.headline_series(reg, concept, key, projections=projections)
    warnings = origin_warnings(q.snapshot.origin) + _licence_warnings(data["licences"])
    if data["source_breaks"]:
        warnings.append(
            s.ApiWarning(
                code="source_break",
                message="The answering series changes within this series at "
                + ", ".join(f"{b['year']} ({b['from']} -> {b['to']})" for b in data["source_breaks"])[:800]
                + "; values on either side come from different sources.",
            )
        )
    return AtlasHeadlineSeriesResponse(
        data=AtlasHeadlineSeries(**data),
        snapshot=_snap(q),
        notes=[HEADLINE_NOTE],
        warnings=warnings,
    )


@router.get("/headline/coverage", response_model=AtlasHeadlineCoverageResponse)
def headline_coverage(
    request: Request,
    theme: str | None = Query(None, pattern="^[a-z][a-z0-9_]{0,39}$", description="REVA theme"),
    origin: str | None = ORIGIN_Q,
    snapshot: str | None = SNAPSHOT_Q,
) -> AtlasHeadlineCoverageResponse:
    """Per headline concept: countries with a value, who answered (latest year), missing countries."""
    reg = _headlines()
    q = _query(request, origin, snapshot)
    return AtlasHeadlineCoverageResponse(
        data=[AtlasHeadlineCoverage(**r) for r in q.headline_coverage(reg, theme)],
        snapshot=_snap(q),
        countries_total=len(geo_index().keys),
        notes=[HEADLINE_NOTE],
        warnings=origin_warnings(q.snapshot.origin),
    )
