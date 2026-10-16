"""OpenAlex scholarly works per country, year and domain -> atlas tables (V3, agent A-SCI).

Source
------
OpenAlex (OurResearch), ``https://api.openalex.org`` — data **CC0** (https://openalex.org/about,
"all OpenAlex data is CC0"). Only ``group_by`` aggregate endpoints are used, never per-work
downloads: one request returns the number of works per country for one filter.

* ``/works?filter=publication_year:<Y>,type:article|review&group_by=authorships.countries``
  (years 1990-2025) -> dataset ``works_country_year``: works (articles and reviews) published in year ``Y`` with at
  least one authorship affiliated to an institution in the country (**full counting**: a work with
  authors in France and Japan counts once for each; country counts do not sum to the world total).
* the same with ``primary_topic.domain.id:<D>`` for OpenAlex's four domains (1 Life Sciences,
  2 Social Sciences, 3 Physical Sciences, 4 Health Sciences), years 2015-2025 -> dataset
  ``works_country_domain``.
* Optionally (``field_years=...``) the 26 OpenAlex fields (``primary_topic.field.id``) for a few years
  -> dataset ``works_country_field`` (request-hungry; off by default).

Every response's ``meta.count`` (all works matching the filter, any country or none) is kept as
an unmapped ``provider_geo = "_all_works"`` row (the denominator for shares); an ``unknown`` group,
if OpenAlex returns one, is kept unmapped as ``provider_geo = "unknown"``.

A group_by page holds at most 200 groups and OpenAlex refuses cursor paging without an API key
(HTTP 401, observed 2026-10-16), so the countries beyond the 200 largest come from *complement*
queries (see :func:`fetch_query`); about 3 requests per query, 230 for the default plan.

Caveats carried in flags: OpenAlex is a live index (counts change as records are added, merged or
re-attributed: the retrieval date is recorded on every row); affiliation-country coverage is weak
before ~1990 and uneven across countries; ``authorships.countries`` comes from OpenAlex's
institution matching (and, for unmatched raw affiliations, its own country inference).

Access: since 2025 OpenAlex meters requests; without an API key a request draws on a small free
daily budget shared by everyone behind the same IP address (exhausted on 2026-10-15: HTTP 429
"Insufficient budget"). The connector takes an optional key (``OPENALEX_API_KEY`` from the environment, sent as the
``api_key`` query parameter, which the safe client and fixtures redact); the contact address goes in
the budget-metered client's User-Agent (``mailto:``, from ``REVA_HTTP_CONTACT_EMAIL``), not in the
URL. A 429 stops the run: the remaining queries are reported ``not_attempted``. Fetching goes only
through ``run.get`` (``reva.atlas.ingest``: budget-metered live client or fixture replay).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote, urlencode

from reva.adapters.connectors.atlas_sci_common import atlas_frame, canon, code_digest, trim_registration
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.openalex"
CONNECTOR_VERSION = "1.2.0"
PARSER_VERSION = "atlas-openalex-parse-2"
SOURCE_KEY = "openalex.api"
ATLAS_SOURCE = "openalex"
HOST = "api.openalex.org"
MAX_BYTES = 2_000_000
PER_PAGE = 200
WORK_TYPES = "article|review"
YEARS = (1990, 2025)
DOMAIN_YEARS = (2015, 2025)
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


def query_filter(q: Query, countries: Sequence[str] | None = None) -> str:
    """The query's filter, optionally restricted to works with an author in ``countries`` (ISO alpha-2)."""
    return q.filter + (f",authorships.countries:{'|'.join(countries)}" if countries else "")


def query_url(
    q: Query, *, cursor: str | None = None, api_key: str | None = None, countries: Sequence[str] | None = None
) -> str:
    """Without ``cursor`` OpenAlex returns the 200 largest groups (count descending); with one (``*`` to
    start, API key required past the first page) it pages through every group in key order."""
    params: dict[str, str] = {
        "filter": query_filter(q, countries),
        "group_by": "authorships.countries",
        "per_page": str(PER_PAGE),
    }
    if cursor is not None:
        params["cursor"] = cursor
    if api_key:
        params["api_key"] = api_key
    return f"https://{HOST}/works?" + urlencode(params, safe=":,|*", quote_via=quote)


#: Planned requests for one default run (36 + 44 queries, ~3 requests each without an API key).
PLANNED_REQUESTS = 300
#: Fixture runs replay a small recorded plan (real captures of these queries).
FIXTURE_YEARS = (2020, 2020)


def atlas_source() -> Any:
    """The atlas source spec (``reva.atlas.sources`` protocol)."""
    from reva.atlas.sources import AtlasSourceSpec

    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="OpenAlex (OurResearch)",
        licence="CC0-1.0",
        allowed_hosts=(HOST,),
        datasets=DATASETS,
        dataset_ids=("works group_by authorships.countries",),
        cadence_seconds=30 * 86400,
        planned_requests=PLANNED_REQUESTS,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=0.25,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(sys.modules[__name__]),
        field_mappings={
            "group_by": "authorships.countries",
            "work_types": WORK_TYPES,
            "years": list(YEARS),
            "domain_years": list(DOMAIN_YEARS),
        },
        notes="optional API key from the environment (OPENALEX_API_KEY, sent as api_key, redacted)",
    )


async def atlas_ingest(
    run: Any,
    *,
    years: tuple[int, int] | None = None,
    domain_years: tuple[int, int] | None = None,
    field_years: Sequence[int] | None = None,
    max_requests: int | None = None,
) -> dict[str, Any]:
    """Query every planned group_by; register one table per dataset with the queries that completed.

    A query whose pages did not all succeed contributes no rows (a partial country list would read
    as absences) and is listed under ``incomplete``; a provider 429/401/403 stops the run (the rest
    is reported ``not_attempted``)."""
    import os

    from reva.kernel.errors import RevaError

    fixture = run.origin == "test_fixture"
    plan = queries(
        years=years or (FIXTURE_YEARS if fixture else YEARS),
        domain_years=domain_years or (FIXTURE_YEARS if fixture else DOMAIN_YEARS),
        field_years=field_years,
    )
    plan = [q for q in plan if run.wants(q.dataset)]
    api_key = None if fixture else (os.environ.get("OPENALEX_API_KEY") or None)
    limit = max_requests or run.spec.planned_requests
    rows: dict[str, list[dict[str, Any]]] = {}
    shas: dict[str, list[str]] = {}
    incomplete: list[dict[str, Any]] = []
    requests = 0
    stopped: str | None = None
    for q in plan:
        if stopped:
            incomplete.append({"query": q.key, "status": "not_attempted", "reason": stopped})
            continue
        state = {"requests": requests}
        try:
            fetched = await fetch_query(run, q, api_key=api_key, limit=limit, state=state)
        except _StopRun:
            requests = state["requests"]
            stopped = "request limit for this run reached"
            incomplete.append({"query": q.key, "status": "not_attempted", "reason": stopped})
            continue
        except RevaError as e:
            requests = state["requests"]
            status = e.details.get("status")
            incomplete.append({"query": q.key, "status": "failed", "http_status": status, "error": e.message[:300]})
            if status in (429, 401, 403):
                stopped = f"provider refused (HTTP {status}); no further requests this run"
            continue
        requests = state["requests"]
        retrieved_at = datetime.now(UTC)
        rows.setdefault(q.dataset, []).extend(atlas_rows(q, fetched, retrieved_at=retrieved_at))
        shas.setdefault(q.dataset, []).extend(f.sha for f in fetched)
    tables = {}
    for ds, rs in rows.items():
        reg = run.register(
            dataset=ds,
            frame=atlas_frame(rs),
            raw_sha256s=sorted(set(shas[ds])),
            geo_scheme=GEO_SCHEME,
            coverage_note=(
                "OpenAlex group_by authorships.countries (articles+reviews, full counting) as indexed at "
                "retrieval; '_all_works' and 'unknown' rows kept unmapped."
            ),
            rights=RIGHTS,
        )
        tables[ds] = trim_registration(reg)
    return {"requests": requests, "queries": len(plan), "tables": tables, "incomplete": incomplete, "stopped": stopped}


class _StopRun(Exception):
    pass


MAX_PAGES = 5
#: Country codes per complement query (keeps the response under 200 groups; halved when it is not).
CHUNK = 25


@dataclass(frozen=True)
class Fetched:
    page: GroupPage
    sha: str
    pno: int
    filter: str
    keep: frozenset[str] | None = None  # complement queries: only these groups are taken from the page
    unresolved: tuple[str, ...] = ()  # countries whose count could not be established (first page only)


def _maybe_truncated(page: GroupPage) -> bool:
    # OpenAlex caps a group_by page at 200 groups and (without a cursor) gives no truncation signal.
    return len(page.groups) >= PER_PAGE


async def fetch_query(run: Any, q: Query, *, api_key: str | None, limit: int, state: dict[str, int]) -> list[Fetched]:
    """All country groups of one query.

    The first request returns the 200 largest groups (count descending). If it holds fewer, it is
    complete. Otherwise: with an API key, cursor paging (key order) lists every group; without one
    (OpenAlex refuses cursor paging: HTTP 401) the remaining crosswalk countries come from
    *complement* queries restricted to works with an author in a chunk of them. A country's count is
    the same under that restriction (every work counted for it has an author there), so a chunk
    country present in a response is exact; one absent from a response with fewer than 200 groups
    has no work; one absent from a full (possibly truncated) response is re-queried in smaller
    chunks, and alone it is reported ``unresolved`` (flagged on the ``_all_works`` row)."""
    from reva.atlas.tables import geo_index

    async def get(url: str, key: str) -> tuple[GroupPage, str]:
        if state["requests"] >= limit:
            raise _StopRun
        state["requests"] += 1
        raw = await run.get(url, key=key, max_bytes=MAX_BYTES)
        return parse_group_page(raw.body), run.archive(raw)

    page, sha = await get(query_url(q, api_key=api_key), f"{q.key}:top")
    if not _maybe_truncated(page):
        return [Fetched(page, sha, 1, q.filter)]
    if api_key:
        out: list[Fetched] = []
        cursor: str | None = "*"
        pno = 0
        while cursor:
            if pno >= MAX_PAGES:
                raise _err(f"{q.key}: more group pages than {MAX_PAGES}", kind="too_many_pages")
            pno += 1
            pg, sh = await get(query_url(q, cursor=cursor, api_key=api_key), f"{q.key}:p{pno}")
            out.append(Fetched(pg, sh, pno, q.filter))
            cursor = pg.next_cursor if pg.groups else None
        # meta.count (all works) is the same on every page; the first cursor page carries it
        return out
    first = Fetched(page, sha, 1, q.filter)
    out = [first]
    seen = {c for c, *_ in page.groups}
    todo = sorted(set(geo_index().maps[GEO_SCHEME]) - seen)
    queue = [todo[i : i + CHUNK] for i in range(0, len(todo), CHUNK)]
    unresolved: list[str] = []
    n = 0
    while queue:
        chunk = queue.pop(0)
        n += 1
        pg, sh = await get(query_url(q, countries=chunk), f"{q.key}:c{n}")
        present = {c for c, *_ in pg.groups} & set(chunk)
        out.append(Fetched(pg, sh, n + 1, query_filter(q, chunk), frozenset(present)))
        missing = [c for c in chunk if c not in present]
        if missing and _maybe_truncated(pg):
            if len(missing) == 1 and len(chunk) == 1:
                unresolved.append(missing[0])
            else:
                half = max(1, len(missing) // 2)
                queue[:0] = [missing[:half], missing[half:]] if len(missing) > 1 else [missing]
    if unresolved:
        out[0] = Fetched(first.page, first.sha, 1, first.filter, None, tuple(unresolved))
    return out


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


def atlas_rows(q: Query, pages: Sequence[Fetched], *, retrieved_at: datetime) -> list[dict[str, Any]]:
    """Rows for one query from its fetched pages (first page, cursor pages or complement queries);
    ``iso3`` is left null and filled by ``register_atlas_table(..., geo_scheme=GEO_SCHEME)``."""
    if not pages:
        return []
    if pages[0].keep is not None:
        raise _err(f"{q.key}: the first fetched page must be the unrestricted query")
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
    first, first_sha = pages[0].page, pages[0].sha
    rows.append(
        _row(
            q,
            "_all_works",
            None,
            period,
            first.total_count,
            first_sha,
            {"filter": q.filter, "page": 1, "path": "meta.count"},
            {
                **flags,
                "meaning": "all works matching the filter (any country or none)",
                **({"unresolved_countries": list(pages[0].unresolved)} if pages[0].unresolved else {}),
            },
        )
    )
    for fp in pages:
        for code, name, count, pos in fp.page.groups:
            if fp.keep is not None and code not in fp.keep:
                continue  # a co-author country of a complement query: counted from its own response
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
                    fp.sha,
                    {"filter": fp.filter, "page": fp.pno, "path": f"group_by[{pos}]", "key": code},
                    f if fp.keep is None else {**f, "via": "complement query (works with an author in the chunk)"},
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
GEO_SCHEME = "openalex"  # ISO alpha-2 + XK (Kosovo), registry/atlas/geo_aliases/a_sci.yaml


def frame(rows: Sequence[Mapping[str, Any]]) -> Any:
    return atlas_frame(rows)


__all__ = [
    "CONNECTOR_NAME",
    "DOMAINS",
    "FIELDS",
    "GEO_SCHEME",
    "Fetched",
    "GroupPage",
    "Query",
    "atlas_ingest",
    "atlas_rows",
    "atlas_source",
    "fetch_query",
    "frame",
    "parse_group_page",
    "queries",
    "query_url",
]
