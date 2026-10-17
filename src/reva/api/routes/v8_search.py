"""V8 universal search (agent V8API; contract ``docs/dev/v8_api_interface.md`` §1).

``GET /v1/v8/search?q=&kinds=&limit=&lang=`` — one ranked list over KG records of every kind (labels, aliases,
authority ids), atlas indicators and tables, countries and regions, forecast questions and families, reference
decisions, effect estimates and sources, from the prebuilt index (``uv run reva v8 index build``).
``GET /v1/v8/search/status`` — what the index holds and when it was built.

Read-only (``read`` scope). Search is lookup, never identity: two records with the same name stay two hits; the
only merge is the declared ISO3 → Wikidata QID map of the countries.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field

from reva.api import schemas as s
from reva.api.auth import require_scope
from reva.api.deps import Timer
from reva.api.errors import ApiProblem
from reva.v8.search.build import SEARCH_KINDS
from reva.v8.search.index import IndexMissing, get_index

router = APIRouter(prefix="/v1/v8/search", tags=["v8-search"], dependencies=[Depends(require_scope("read"))])

LOOKUP_WARNING = s.ApiWarning(
    code="lookup_not_identity",
    message="Search matches labels and identifiers of each source's own records; two hits with the same name are "
    "not the same thing (no record is merged by name).",
)


class SearchHit(s.ApiModel):
    id: str = Field(description="The record's own id in its source (ISO3 for kind=country)")
    kind: str = Field(description="Search kind: " + ", ".join(SEARCH_KINDS))
    label: str
    sublabel: str = Field(description="French: kind · codes · description / status")
    href: str = Field(description="V8 deep link (v8Links scheme of ui/src/v8/contract.ts)")
    score: float = Field(description="0..1, comparable within one response only")
    group: str = Field(description="French group label for the palette")
    match: Literal["id", "exact", "prefix", "word", "trigram"]
    matched: str = Field(description="The label, alias or code that matched")
    entity_kind: str | None = Field(None, description="KG kind for KG records")
    source: str | None = None
    origin: Literal["live", "test_fixture"] = "live"
    extra: dict[str, Any] = Field(default_factory=dict)


class SearchGroup(s.ApiModel):
    group: str
    count: int


class SearchIndexInfo(s.ApiModel):
    built_at: str | None
    version: str | None
    documents: int | None


class SearchData(s.ApiModel):
    query: str
    normalized: str
    hits: list[SearchHit]
    groups: list[SearchGroup]
    truncated: bool
    index: SearchIndexInfo


class SearchResponse(s.Envelope):
    data: SearchData


class SearchStatus(s.ApiModel):
    built: bool
    built_at: str | None = None
    version: str | None = None
    documents: int | None = None
    names: int | None = None
    codes: int | None = None
    kinds: dict[str, int] = Field(default_factory=dict)
    errors: list[dict[str, str]] = Field(default_factory=list)
    origin: str | None = None
    path_bytes: int | None = None
    popular_names_in_memory: int | None = None


class SearchStatusResponse(s.Envelope):
    data: SearchStatus


def _index() -> Any:
    try:
        return get_index()
    except IndexMissing:
        raise ApiProblem(
            503,
            "search_index_missing",
            "The V8 search index has not been built on this server (run `uv run reva v8 index build`).",
        ) from None


def _origin(request: Request) -> str | None:
    prod = getattr(getattr(request.app.state, "settings", None), "reva_env", "") == "production"
    return "live" if prod else None


@router.get("", response_model=SearchResponse, responses={503: {"model": s.Problem}, 422: {"model": s.Problem}})
def search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=200, description="Text, an id or a code"),
    kinds: str | None = Query(
        None, max_length=200, pattern=r"^[a-z_]+(,[a-z_]+)*$", description="Comma list of search kinds"
    ),
    limit: int = Query(20, ge=1, le=100),
    lang: Literal["fr", "en"] = Query("fr"),
) -> SearchResponse:
    t = Timer()
    kind_list = [k for k in (kinds or "").split(",") if k]
    bad = [k for k in kind_list if k not in SEARCH_KINDS]
    if bad:
        raise ApiProblem(422, "validation_error", f"unknown search kinds: {', '.join(bad)}", allowed=list(SEARCH_KINDS))
    idx = _index()
    res = idx.search(q, kinds=kind_list or None, limit=limit, lang=lang, origin=_origin(request))
    st = idx.meta
    data = SearchData(
        **res,
        index=SearchIndexInfo(built_at=st.get("built_at"), version=st.get("version"), documents=st.get("documents")),
    )
    origins = {h["origin"] for h in res["hits"]}
    origin = "none" if not origins else ("mixed" if len(origins) > 1 else origins.pop())
    warnings = [LOOKUP_WARNING]
    if origin in ("test_fixture", "mixed"):
        warnings.append(s.ApiWarning(code="fixture_data", message="Contains test_fixture-origin records."))
    env: dict[str, Any] = {
        "context_mode": "registry",
        "origin": origin,
        "modality": "metadata",
        "warnings": warnings,
        "cost": t.cost(len(res["hits"])),
    }
    return SearchResponse(data=data, **env)


@router.get("/status", response_model=SearchStatusResponse)
def status() -> SearchStatusResponse:
    t = Timer()
    try:
        st = SearchStatus(**get_index().status())
    except IndexMissing:
        st = SearchStatus(built=False)
    env: dict[str, Any] = {
        "context_mode": "registry",
        "origin": "none",
        "modality": "metadata",
        "cost": t.cost(st.documents or 0),
    }
    return SearchStatusResponse(data=st, **env)
