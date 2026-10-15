"""OpenAlex scholarly works per country, year and domain -> atlas tables (V3, agent A-SCI).

Source
------
OpenAlex (OurResearch), ``https://api.openalex.org`` — data **CC0** (https://openalex.org/about,
"all OpenAlex data is CC0"). Only ``group_by`` aggregate endpoints are used, never per-work
downloads: one request returns the number of works per country for one filter.

* ``/works?filter=publication_year:<Y>,type:article|review&group_by=authorships.countries``
  -> dataset ``works_country_year``: works (articles and reviews) published in year ``Y`` with at
  least one authorship affiliated to an institution in the country (**full counting**: a work with
  authors in France and Japan counts once for each; country counts do not sum to the world total).
* the same with ``primary_topic.domain.id:<D>`` for OpenAlex's four domains (1 Life Sciences,
  2 Social Sciences, 3 Physical Sciences, 4 Health Sciences) -> dataset ``works_country_domain``.
* Optionally (``fields=True``) the 26 OpenAlex fields (``primary_topic.field.id``) for a few years
  -> dataset ``works_country_field`` (request-hungry; off by default).

Every response's ``meta.count`` (all works matching the filter, any country or none) is kept as
an unmapped ``provider_geo = "_all_works"`` row, and the ``unknown`` group (works with no country
attributed) as ``provider_geo = "unknown"``, so country shares and the coverage gap stay visible.

Caveats carried in flags: OpenAlex is a live index (counts change as records are added, merged or
re-attributed: the retrieval date is recorded on every row); affiliation-country coverage is weak
before ~1990 and uneven across countries; ``authorships.countries`` comes from OpenAlex's
institution matching (and, for unmatched raw affiliations, its own country inference).

Access: since 2025 OpenAlex meters requests; without an API key a request draws on a small free
daily budget shared by everyone behind the same IP address (exhausted on 2026-10-15: HTTP 429
"Insufficient budget"). The connector takes an optional key (``api_key``, sent as the ``api_key``
query parameter, which the safe client and fixtures redact) and a ``mailto`` contact (the
configured ``REVA_HTTP_CONTACT_EMAIL``). A 429 is never retried in a loop: the run stops and
reports the remaining items as blocked.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import quote, urlencode

from reva.adapters.connectors.atlas_sci_common import atlas_frame, atlas_spec, canon, code_digest
from reva.adapters.http.safe_client import request_fingerprint
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import (
    ConnectorSpec,
    Cursor,
    FetchBudget,
    FetchItem,
    FetchPlan,
    ObservationBatch,
    RawAcquisition,
)

CONNECTOR_NAME = "atlas.openalex"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas-openalex-parse-1"
SOURCE_KEY = "openalex.api"
ATLAS_SOURCE = "openalex"
HOST = "api.openalex.org"
MAX_BYTES = 2_000_000
PER_PAGE = 200
WORK_TYPES = "article|review"
YEARS = (1990, 2025)
DOMAIN_YEARS = (2000, 2025)
FIELD_YEARS = (2000, 2010, 2020, 2024)
DOMAINS: dict[str, str] = {
    "1": "Life Sciences",
    "2": "Social Sciences",
    "3": "Physical Sciences",
    "4": "Health Sciences",
}
#: OpenAlex fields (ASJC-based, ids 11-36).
FIELDS: dict[str, str] = {
    "11": "Agricultural and Biological Sciences",
    "12": "Arts and Humanities",
    "13": "Biochemistry, Genetics and Molecular Biology",
    "14": "Business, Management and Accounting",
    "15": "Chemical Engineering",
    "16": "Chemistry",
    "17": "Computer Science",
    "18": "Decision Sciences",
    "19": "Earth and Planetary Sciences",
    "20": "Economics, Econometrics and Finance",
    "21": "Energy",
    "22": "Engineering",
    "23": "Environmental Science",
    "24": "Immunology and Microbiology",
    "25": "Materials Science",
    "26": "Mathematics",
    "27": "Medicine",
    "28": "Neuroscience",
    "29": "Nursing",
    "30": "Pharmacology, Toxicology and Pharmaceutics",
    "31": "Physics and Astronomy",
    "32": "Psychology",
    "33": "Social Sciences",
    "34": "Veterinary",
    "35": "Dentistry",
    "36": "Health Professions",
}
DATASETS = ("works_country_year", "works_country_domain", "works_country_field")
SECRET_PARAMS = ("api_key", "subscription-key")

RIGHTS: dict[str, str] = {
    "licence": "CC0-1.0",
    "licence_url": "https://openalex.org/about",
    "attribution": "OpenAlex (OurResearch), CC0",
}


class OpenAlexError(SourceResponseError):
    pass


def _err(msg: str, kind: str = "schema_drift") -> OpenAlexError:
    return OpenAlexError(msg, retryable=False, kind=kind)


@dataclass(frozen=True)
class Query:
    dataset: str
    year: int
    dim: str | None = None  # "domain" | "field"
    dim_id: str | None = None

    @property
    def indicator(self) -> str:
        base = f"works|type={WORK_TYPES.replace('|', ',')}"
        return base if self.dim is None else f"{base}|{self.dim}={self.dim_id}"

    @property
    def filter(self) -> str:
        f = f"publication_year:{self.year},type:{WORK_TYPES}"
        if self.dim == "domain":
            f += f",primary_topic.domain.id:{self.dim_id}"
        elif self.dim == "field":
            f += f",primary_topic.field.id:{self.dim_id}"
        return f

    @property
    def key(self) -> str:
        return f"{self.dataset}:{self.year}" + (f":{self.dim}={self.dim_id}" if self.dim else "")


def queries(
    *,
    years: tuple[int, int] = YEARS,
    domain_years: tuple[int, int] | None = DOMAIN_YEARS,
    field_years: Sequence[int] | None = None,
) -> list[Query]:
    out = [Query("works_country_year", y) for y in range(years[0], years[1] + 1)]
    if domain_years:
        out += [
            Query("works_country_domain", y, "domain", d)
            for y in range(domain_years[0], domain_years[1] + 1)
            for d in DOMAINS
        ]
    if field_years:
        out += [Query("works_country_field", y, "field", f) for y in field_years for f in FIELDS]
    return out


def query_url(q: Query, *, cursor: str = "*", mailto: str | None = None, api_key: str | None = None) -> str:
    params: dict[str, str] = {
        "filter": q.filter,
        "group_by": "authorships.countries",
        "per_page": str(PER_PAGE),
        "cursor": cursor,
    }
    if mailto:
        params["mailto"] = mailto
    if api_key:
        params["api_key"] = api_key
    return f"https://{HOST}/works?" + urlencode(params, safe=":,|*", quote_via=quote)


class OpenAlexAtlasConnector:
    """Atlas bulk connector (group_by aggregates; archive raw -> atlas tables)."""

    def __init__(
        self,
        *,
        mailto: str | None = None,
        api_key: str | None = None,
        years: tuple[int, int] = YEARS,
        domain_years: tuple[int, int] | None = DOMAIN_YEARS,
        field_years: Sequence[int] | None = None,
    ) -> None:
        self.mailto = mailto
        self._api_key = api_key
        self.queries = queries(years=years, domain_years=domain_years, field_years=field_years)

    def describe(self) -> ConnectorSpec:
        return atlas_spec(
            name=CONNECTOR_NAME,
            version=CONNECTOR_VERSION,
            source_key=SOURCE_KEY,
            provider="OpenAlex (OurResearch)",
            licence="CC0-1.0",
            allowed_hosts=(HOST,),
            dataset_ids=DATASETS,
            field_mappings={
                "atlas": [f"{ATLAS_SOURCE}.{d}" for d in DATASETS],
                "group_by": "authorships.countries",
                "work_types": WORK_TYPES,
            },
            units={"works": "works (full counting by country)"},
            geography=("*",),
            time_semantics="publication_year of the work; counts as indexed at retrieval time",
            lineage_family="openalex",
            cadence_seconds=30 * 86400,
            release_aware=False,
            authentication_secret_ref="env:OPENALEX_API_KEY (optional)",
            revision_strategy="full re-query per run; new bytes -> new atlas table version",
            max_response_bytes=MAX_BYTES,
            max_pages=5,
            parser_version=PARSER_VERSION,
            code_digest=code_digest(sys.modules[__name__]),
        )

    def item(self, q: Query, cursor: str = "*", page: int = 1) -> FetchItem:
        return FetchItem(
            key=f"{q.key}:p{page}",
            url=query_url(q, cursor=cursor, mailto=self.mailto, api_key=self._api_key),
            max_bytes=MAX_BYTES,
            params={"dataset": q.dataset, "year": q.year, "dim": q.dim, "dim_id": q.dim_id, "page": page},
        )

    def plan(self, cursor: Cursor | None = None, budget: FetchBudget | None = None) -> FetchPlan:
        items = tuple(self.item(q) for q in self.queries)
        limit = budget.max_requests if budget else len(items)
        return FetchPlan(
            connector=CONNECTOR_NAME,
            items=items[:limit],
            pagination="cursor paging of group_by results (per_page 200); a follow-up page is planned from meta.next_cursor",
            availability_expectation="live index; counts drift daily",
            budget_exhausted=len(items) > limit,
        )

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition:
        result = await http.get(item.url, max_bytes=item.max_bytes)
        return RawAcquisition(
            item=item,
            body=result.body,
            media_type=result.media_type or "application/json",
            retrieved_at=result.retrieved_at,
            response_metadata=result.response_metadata(),
            request_fingerprint=request_fingerprint("GET", item.url, secret_params=SECRET_PARAMS),
            origin=getattr(http, "origin", "live"),
        )

    def parse(self, raw: RawAcquisition) -> Iterator[ObservationBatch]:
        return iter(())  # atlas connector: no evidence observations


# ----------------------------------------------------------------------------- parsing
@dataclass(frozen=True)
class GroupPage:
    total_count: int  # meta.count: works matching the filter
    groups: tuple[tuple[str, str | None, int, int], ...]  # (code, display name, count, position)
    next_cursor: str | None
    groups_count: int | None


def parse_group_page(body: bytes) -> GroupPage:
    try:
        doc = json.loads(body)
    except ValueError as e:
        raise _err(f"not JSON ({e})") from None
    if not isinstance(doc, Mapping):
        raise _err("expected a JSON object")
    if "error" in doc and "group_by" not in doc:
        raise _err(
            f"OpenAlex error: {str(doc.get('error'))[:200]}: {str(doc.get('message'))[:300]}", kind="provider_error"
        )
    meta, groups = doc.get("meta"), doc.get("group_by")
    if not isinstance(meta, Mapping) or not isinstance(groups, list):
        raise _err("expected 'meta' and a 'group_by' list")
    total = meta.get("count")
    if not isinstance(total, int) or total < 0:
        raise _err(f"meta.count {total!r}")
    out = []
    for i, g in enumerate(groups):
        if not isinstance(g, Mapping) or not isinstance(g.get("count"), int) or g["count"] < 0:
            raise _err(f"group {i}: bad count")
        key = str(g.get("key") or "")
        code = key.rstrip("/").rsplit("/", 1)[-1] if key else ""
        if not code:
            raise _err(f"group {i}: empty key")
        code = "unknown" if code.lower() == "unknown" else code.upper()
        out.append((code, g.get("key_display_name"), int(g["count"]), i))
    nc = meta.get("next_cursor")
    gc = meta.get("groups_count")
    return GroupPage(total, tuple(out), nc if isinstance(nc, str) and nc else None, gc if isinstance(gc, int) else None)


def atlas_rows(
    q: Query, pages: Sequence[tuple[GroupPage, str, int]], *, retrieved_at: datetime
) -> list[dict[str, Any]]:
    """Rows for one query from its pages ``(page, raw_sha256, page_number)``; ``iso3`` is left
    null and filled by ``register_atlas_table(..., geo_scheme=GEO_SCHEME)``."""
    if not pages:
        return []
    period = (date(q.year, 1, 1), date(q.year + 1, 1, 1))
    flags = {
        "counting": "full counting by affiliation country (a work counts once per country)",
        "work_types": WORK_TYPES.replace("|", ","),
        "live_index": "counts as indexed at retrieval; OpenAlex revises records continually",
        "retrieved_at": retrieved_at.isoformat(),
        "licence": RIGHTS["licence"],
    }
    if q.year < 1990:
        flags["coverage_warning"] = "affiliation-country coverage is sparse before ~1990"
    if q.dim == "domain":
        flags["domain"] = DOMAINS.get(q.dim_id or "", str(q.dim_id))
    if q.dim == "field":
        flags["field"] = FIELDS.get(q.dim_id or "", str(q.dim_id))
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    first, first_sha, _ = pages[0]
    rows.append(
        _row(
            q,
            "_all_works",
            None,
            period,
            first.total_count,
            first_sha,
            {"filter": q.filter, "page": 1, "path": "meta.count"},
            {**flags, "meaning": "all works matching the filter (any country or none)"},
        )
    )
    for page, sha, pno in pages:
        for code, name, count, pos in page.groups:
            if code in seen:
                raise _err(f"{q.key}: group {code} repeated across pages")
            seen.add(code)
            f = dict(flags)
            if name:
                f["display_name"] = name
            if code == "unknown":
                f["meaning"] = "works with no affiliation country attributed"
            rows.append(
                _row(
                    q,
                    code,
                    None,
                    period,
                    count,
                    sha,
                    {"filter": q.filter, "page": pno, "path": f"group_by[{pos}]", "key": code},
                    f,
                )
            )
    return rows


def _row(
    q: Query,
    geo: str,
    iso3: str | None,
    period: tuple[date, date],
    n: int,
    sha: str,
    selector: Mapping[str, Any],
    flags: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "source": ATLAS_SOURCE,
        "dataset": q.dataset,
        "indicator": q.indicator,
        "concept_id": "sci.openalex.works" + ("" if q.dim is None else f".{q.dim}_{q.dim_id}"),
        "iso3": iso3,
        "provider_geo": geo,
        "entity_qid": None,
        "period_start": period[0],
        "period_end": period[1],
        "value": str(n),
        "value_text": None,
        "unit": "works",
        "flags": canon(flags),
        "raw_sha256": sha,
        "selector": canon(selector),
    }


#: geo scheme for OpenAlex country codes (ISO 3166 alpha-2).
GEO_SCHEME = "iso2"


def frame(rows: Sequence[Mapping[str, Any]]) -> Any:
    return atlas_frame(rows)


__all__ = [
    "CONNECTOR_NAME",
    "DOMAINS",
    "FIELDS",
    "GEO_SCHEME",
    "GroupPage",
    "OpenAlexAtlasConnector",
    "Query",
    "atlas_rows",
    "frame",
    "parse_group_page",
    "queries",
    "query_url",
]
