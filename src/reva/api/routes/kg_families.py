"""Universal relation families over the knowledge graph (REVA V4, agent V4CORE).

* ``GET /v1/kg/families`` — the family registry (``registry/kg/relations_v4.yaml`` + ``relations_v4.d/*.yaml``):
  families with fr/en labels and inverse labels, and every source property mapped to them.
* ``GET /v1/kg/entities/{qid}/families`` — the neighbours of ANY entity (compound, taxon, company, region…)
  grouped by family, each with its statement provenance, the source's own property, share (Missing = null,
  never 0) and the licence of the raw artifact; literal values (identifiers, quantities, dates, coordinates);
  entity-valued properties no family maps yet (counted, not hidden).
* ``GET /v1/kg/entities/{qid}/places`` — the evidenced ``located_in`` chain (ADM2 → ADM1 → country) with
  coordinates.
* ``GET /v1/kg/families/path`` — shortest connections whose every hop reads, in path order, as one of the
  families asked for (``part_of`` is not ``has_part``).

No family is causal: causal claims are served by ``/v1/causal`` as attributed claims. Read-only, ``read`` scope.
The country↔country links store (``/v1/links``) is unchanged.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field

from reva.api import schemas as s
from reva.api.auth import require_scope
from reva.api.deps import Timer, read_conn
from reva.api.routes.kg import (
    ORIGIN_QUERY,
    PATH_WARNING,
    QID_PATTERN,
    TIME_PATTERN,
    KgPathResult,
    KgRef,
    KgStatementOut,
    OriginParam,
    QidPath,
    _community,
    _derived,
    _envelope_kwargs,
    _known_qid,
    _origin,
    _window,
)
from reva.kernel.errors import ValidationProblem
from reva.kg import family_query as fq
from reva.kg import query as kq

router = APIRouter(prefix="/v1/kg", tags=["knowledge-graph"], dependencies=[Depends(require_scope("read"))])

FAMILY_PATTERN = r"^[a-z][a-z_]{1,31}$"
FAMILY_WARNING = s.ApiWarning(code="families_not_causal", message=fq.FAMILY_NOTE)


# ---------------------------------------------------------------------------------------------- models
class FamilyLabels(s.ApiModel):
    en: str
    fr: str


class FamilyMappingOut(s.ApiModel):
    property: str
    family: str
    direction: Literal["forward", "reverse"]
    subtype: str | None
    source: str | None
    share_qualifier: str | None
    share_scale: str | None
    origin: str = Field(
        description="File the mapping is declared in (relations_v4.yaml or relations_v4.d/<agent>.yaml)"
    )
    agent: str
    note: str | None


class FamilyOut(s.ApiModel):
    id: str
    label: FamilyLabels
    inverse_label: FamilyLabels
    inverse_family: str | None
    symmetric: bool
    group: str
    path_cost: float
    share_allowed: bool
    causal: Literal[False] = Field(description="Always false: no relation family is causal")
    description: FamilyLabels
    mappings: int


class FamilyRegistryOut(s.ApiModel):
    families: list[FamilyOut]
    mappings: list[FamilyMappingOut]
    files: list[str]
    note: str


class FamilyRegistryResponse(s.Envelope):
    data: FamilyRegistryOut


class FamilyReading(s.ApiModel):
    label: FamilyLabels = Field(description="Relation read from the entity to the neighbour")
    inverse: bool = Field(description="True when the label is the family's inverse reading")


class FamilyMappingRef(s.ApiModel):
    origin: str
    agent: str
    direction: Literal["forward", "reverse"]


class FamilyShare(s.ApiModel):
    value: str = Field(description="Share as the source gives it (decimal string)")
    fraction: str | None = Field(description="Normalised fraction 0..1 for dimensionless scales; null otherwise")
    unit: str | None
    scale: str
    qualifier: str


class AtlasHint(s.ApiModel):
    kind: Literal["concept", "table"] = Field(
        description="concept: object atlas:<catalog concept> (GET /v1/atlas/series); table: object "
        "atlas:<source>.<dataset> table entity (GET /v1/atlas/tables/{source}/{dataset}/cells)"
    )
    concept: str | None = Field(description="Atlas catalog concept (kind concept); null for a table entity")
    iso3: str | None
    table: str | None = Field(description="Atlas table key <source>.<dataset> when the statement names one")
    source: str | None = Field(None, description="kind table: atlas source key")
    dataset: str | None = Field(None, description="kind table: atlas dataset key")
    provider_geo: str | None = Field(
        None, description="kind table: the provider's geography code of the subject's cells (regions are not countries)"
    )
    indicators: list[str] = Field(default_factory=list, description="kind table: exact indicator ids of the subject")
    indicator_contains: str | None = Field(
        None, description="kind table: indicator substring selecting the subject's cells (e.g. classif1=OCU_ISCO08_2)"
    )


class ValueRights(s.ApiModel):
    licence: str
    licence_url: str | None
    attribution: str | None
    commercial_use: str
    non_commercial: bool
    share_alike: bool
    no_derivatives: bool
    open_licence: bool | None
    restricted: bool
    basis: str
    restriction: str | None = None
    required_notice: str | None = Field(
        None, description="Notice the provider's terms require next to any presentation of the value (verbatim)"
    )


class FamilyNeighbour(s.ApiModel):
    neighbour: KgRef
    reading: FamilyReading
    subtype: str | None
    mapping: FamilyMappingRef
    share: FamilyShare | None = Field(description="Only when the mapping declares a share; null = Missing, never 0")
    atlas: AtlasHint | None = Field(description="measured_by only: the atlas series to read")
    rights: ValueRights | None = Field(description="Licence of the raw artifact the statement was parsed from")
    statement: KgStatementOut


class FamilyPropertyCount(s.ApiModel):
    pid: str
    count: int
    label_fr: str | None
    label_en: str | None


class FamilyGroup(s.ApiModel):
    family: str
    label: FamilyLabels
    inverse_label: FamilyLabels
    group: str
    symmetric: bool
    count: int
    count_is_lower_bound: bool
    shown: int
    truncated: bool
    properties: list[FamilyPropertyCount]
    neighbours: list[FamilyNeighbour]
    causal: Literal[False]


class UnmappedProperty(s.ApiModel):
    pid: str
    direction: Literal["out", "in"]
    count: int
    label_fr: str | None
    label_en: str | None
    causal_assertion: bool = Field(description="A source-asserted causal property (read it through /v1/causal)")


class LiteralStatement(KgStatementOut):
    rights: ValueRights | None


class RegistryInfo(s.ApiModel):
    families: int
    mappings: int
    files: list[str]


class EntityFamilies(s.ApiModel):
    qid: str
    label: str
    labels: dict[str, str]
    kind: str | None
    families: list[FamilyGroup]
    unmapped: list[UnmappedProperty]
    literals: list[LiteralStatement]
    literals_truncated: bool
    scan_capped: bool
    registry: RegistryInfo
    notes: list[str]


class EntityFamiliesResponse(s.Envelope):
    data: EntityFamilies


class Coordinates(s.ApiModel):
    latitude: float
    longitude: float
    statement_key: str
    source_key: str


class PlaceLevel(s.ApiModel):
    entity: KgRef
    level: int | None = Field(description="0 country, 1 ADM1, 2 ADM2 when the identifier states it; else null")
    via: str | None
    statement_key: str | None
    basis: Literal["start", "statement", "iso3166_2_code_prefix"]
    iso3: str | None
    coordinates: Coordinates | None


class EntityPlaces(s.ApiModel):
    qid: str
    label: str
    chain: list[PlaceLevel]
    levels: dict[str, str]
    country_iso3: str | None
    coordinates: Coordinates | None
    located_in_properties: list[str]
    note: str


class EntityPlacesResponse(s.Envelope):
    data: EntityPlaces


class FamilyPathResult(KgPathResult):
    families: list[str] | None = None


class FamilyPathResponse(s.Envelope):
    data: FamilyPathResult


# ---------------------------------------------------------------------------------------------- routes
@router.get("/families", response_model=FamilyRegistryResponse, responses={422: {"model": s.Problem}})
def list_families(request: Request) -> FamilyRegistryResponse:
    t = Timer()
    _ = request
    reg = fq.registry()
    counts = reg.summary()["per_family"]
    fams = [FamilyOut(**f.as_dict(), mappings=counts.get(fid, 0)) for fid, f in reg.families.items()]
    maps = [
        FamilyMappingOut(
            property=m.property,
            family=m.family,
            direction=m.direction,
            subtype=m.subtype,
            source=m.source,
            share_qualifier=m.share_qualifier,
            share_scale=m.share_scale,
            origin=m.origin,
            agent=m.agent,
            note=m.note,
        )
        for m in sorted(reg.mappings.values(), key=lambda m: (m.family, m.property))
    ]
    return FamilyRegistryResponse(
        data=FamilyRegistryOut(families=fams, mappings=maps, files=list(reg.files), note=fq.FAMILY_NOTE),
        context_mode="registry",
        origin="none",
        modality="metadata",
        warnings=[FAMILY_WARNING],
        cost=t.cost(len(maps)),
    )


@router.get(
    "/entities/{qid}/families",
    response_model=EntityFamiliesResponse,
    responses={404: {"model": s.Problem}, 422: {"model": s.Problem}},
)
def get_entity_families(
    request: Request,
    qid: str = QidPath,
    family: list[str] | None = Query(None, max_length=30, description="Only these families (default: all)"),
    per_family: int = Query(50, ge=1, le=fq.MAX_PER_FAMILY, description="Neighbours shown per family"),
    valid_from: str | None = Query(None, pattern=TIME_PATTERN),
    valid_to: str | None = Query(None, pattern=TIME_PATTERN),
    include_undated: bool = True,
    include_literals: bool = Query(True, description="Also return literal values (identifiers, quantities, dates)"),
    origin: OriginParam | None = ORIGIN_QUERY,
) -> EntityFamiliesResponse:
    t = Timer()
    _families(family)
    w = _window(valid_from, valid_to, include_undated)
    with read_conn(request) as conn:
        _known_qid(conn, qid)
        res = fq.entity_families(
            conn,
            qid,
            families=family,
            per_family=per_family,
            time_window=w,
            origin=_origin(request, origin),
            include_literals=include_literals,
        )
    sts = [n["statement"] for g in res["families"] for n in g["neighbours"]] + list(res["literals"])
    env = _envelope_kwargs(res["origins"], _community(sts), t, len(sts), derived=_derived(sts))
    env["warnings"].insert(0, FAMILY_WARNING)
    if res["scan_capped"]:
        env["warnings"].append(
            s.ApiWarning(
                code="truncated", message=f"more than {fq.MAX_SCAN} family statements: counts are lower bounds"
            )
        )
    restricted = sorted(
        {
            (x.get("rights") or {}).get("licence") or ""
            for x in [*(n for g in res["families"] for n in g["neighbours"]), *res["literals"]]
            if (x.get("rights") or {}).get("restricted")
        }
    )
    if restricted:
        env["warnings"].append(
            s.ApiWarning(code="restricted_licence", message="Restricted licence(s): " + "; ".join(restricted)[:400])
        )
    data = {k: v for k, v in res.items() if k != "origins"}
    return EntityFamiliesResponse(data=EntityFamilies.model_validate(data), **env)


@router.get(
    "/entities/{qid}/places",
    response_model=EntityPlacesResponse,
    responses={404: {"model": s.Problem}, 422: {"model": s.Problem}},
)
def get_entity_places(
    request: Request, qid: str = QidPath, origin: OriginParam | None = ORIGIN_QUERY
) -> EntityPlacesResponse:
    t = Timer()
    with read_conn(request) as conn:
        _known_qid(conn, qid)
        res = fq.entity_places(conn, qid, origin=_origin(request, origin))
    origins: list[str] = []
    return EntityPlacesResponse(
        data=EntityPlaces.model_validate(res), **_envelope_kwargs(origins, False, t, len(res["chain"]))
    )


@router.get(
    "/families/path",
    response_model=FamilyPathResponse,
    response_model_by_alias=True,
    responses={404: {"model": s.Problem}, 422: {"model": s.Problem}},
)
def get_family_path(
    request: Request,
    from_: str = Query(..., alias="from", pattern=QID_PATTERN, max_length=161),
    to: str = Query(..., pattern=QID_PATTERN, max_length=161),
    family: list[str] = Query(..., min_length=1, max_length=30, description="Allowed families (in reading direction)"),
    max_hops: int = Query(4, ge=1, le=kq.MAX_HOPS),
    valid_from: str | None = Query(None, pattern=TIME_PATTERN),
    valid_to: str | None = Query(None, pattern=TIME_PATTERN),
    include_undated: bool = True,
    hub_limit: int = Query(kq.DEFAULT_HUB_LIMIT, ge=1, le=kq.MAX_HUB_LIMIT),
    allow_hub: list[str] | None = Query(None, max_length=20),
    max_paths: int = Query(5, ge=1, le=kq.MAX_PATHS),
    origin: OriginParam | None = ORIGIN_QUERY,
) -> FamilyPathResponse:
    t = Timer()
    _families(family)
    if from_ == to:
        raise ValidationProblem("from and to are the same entity")
    w = _window(valid_from, valid_to, include_undated)
    with read_conn(request) as conn:
        _known_qid(conn, from_)
        _known_qid(conn, to)
        res = fq.family_paths(
            conn,
            from_,
            to,
            families=family,
            max_hops=max_hops,
            time_window=w,
            hub_limit=hub_limit,
            allow_hubs=allow_hub or (),
            max_paths=max_paths,
            origin=_origin(request, origin),
        )
    hops = [h for p in res["paths"] for h in p["hops"]]
    env = _envelope_kwargs(
        sorted({h["provenance"]["origin"] for h in hops}),
        _community(hops),
        t,
        len(hops),
        derived=_derived(hops),
        edges_scanned=res["search"]["edges_scanned"],
        levels_expanded=res["search"]["levels_expanded"],
    )
    env["warnings"][:0] = [PATH_WARNING, FAMILY_WARNING]
    if res["search"]["truncated"]:
        env["warnings"].append(
            s.ApiWarning(code="truncated", message="; ".join(res["search"]["truncation_reasons"])[:500])
        )
    return FamilyPathResponse(data=FamilyPathResult.model_validate(res), **env)


def _families(fams: list[str] | None) -> None:
    import re

    for f in fams or ():
        if not re.match(FAMILY_PATTERN, f):
            raise ValidationProblem("invalid relation family id", family=f[:64])
