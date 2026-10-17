"""WHO Global Health Estimates (GHE): deaths and DALYs by cause, country and year -> atlas table ``who.ghe``
(V4, agent V4HEALTH; extends A-HEALTHEDU's ``who.gho`` table, which carries GHE *headline* indicators only --
life expectancy, NCD mortality 30-70, suicide -- but no cause-of-death breakdown: GHO's own ``GHE_*`` indicators
stop at WHO regions and the 2012 vintage).

Source
------
WHO data portal back-end (xMart, OData v4), entity set ``GHE_FULL`` of ``https://xmart-api-public.who.int/DEX_CMS``:
the full GHE (country x year x age group x sex x cause), behind https://data.who.int "Global health estimates:
leading causes of death / DALYs". 183 countries, 2000 onwards (the latest year is read from the API, never
assumed), 226 causes in 6 levels (0 all causes, 1 three broad groups, 2 23 groups, 3-5 specific causes).
Licence: WHO datasets are **CC BY 4.0** with WHO's additional terms
(https://data.who.int/redirect-pages/footer/learn-more/terms-and-conditions), recorded in :data:`RIGHTS`
(same terms as ``atlas.who_gho``). The GHE methods report (PDF) is CC BY-NC-SA 3.0 IGO and is NOT fetched.

Requests (one run; the full table is 65.5 M rows, so only provider-side slices are requested):
``$apply=groupby`` for the year list, the country list and the cause list (code, title, level), then one request
per country with ``DIM_AGEGROUP_CODE eq 'TOTAL' and DIM_SEX_CODE eq 'TOTAL'`` (all ages, both sexes) and
``FLAG_LEVEL le 2 or DIM_YEAR_CODE eq <latest year>``: the 27 cause groups of levels 0-2 for every year plus all
226 causes for the latest year (~280 KB per country, ~51 MB per run). Other age/sex breakdowns and the
level 3-5 causes of earlier years wait for a bigger machine (docs/dev/v4_v4health.md).

What a cell is
--------------
* ``indicator`` = ``ghe.<measure>|sex=TOTAL|age=TOTAL|cause=<GHE cause code>``, measure in ``deaths`` (count),
  ``deaths_rate100k`` (per 100 000 population), ``dalys`` (count), ``dalys_rate100k``.
* ``value`` = the API's number exactly as served (decimal text); ``flags.low`` / ``flags.high`` = WHO's uncertainty
  bounds for the death counts (``VAL_DTHS_COUNT_LOW/HIGH``; DALY bounds not requested, see MEASURES; their level is not stated per row, ``interval_level:
  "not_stated_per_row"``); ``flags.cause_title`` / ``flags.cause_level`` from the cause list; ``flags.estimate_class
  = who_comparable_estimate``. A null value is Missing (``flags.missing = not_reported``), never zero; a 0.0 that WHO
  serves is WHO's estimate of zero and stays 0.
* GHE cause codes are WHO's own; they are **not** linked to Disease Ontology / Mondo / ICD classes here: the API
  publishes no ICD code per cause, and REVA never matches causes to diseases by name. The ICD-10 code lists of the
  GHE cause categories (methods report annex) are the evidenced bridge for V4LINK.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from reva.adapters.connectors.atlas_healthedu_common import canon, fetch_archive_register, fit_flags
from reva.adapters.connectors.base import dec_str, json_decimal, module_digest
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError, ValidationProblem
from reva.ports.connectors import FetchItem

CONNECTOR_NAME = "atlas.who_ghe"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas-who-ghe-parse-1"
SOURCE_KEY = "who.ghe"
HOST = "xmart-api-public.who.int"
BASE = f"https://{HOST}/DEX_CMS/GHE_FULL"
MAX_ITEM_BYTES = 8_000_000  # one country slice was ~280 kB on 2026-10-17
MAX_LEVEL_ALL_YEARS = 2
MIN_COUNTRIES_LIVE = 150

RIGHTS: dict[str, str] = {
    "licence": "CC BY 4.0 (WHO additional terms)",
    "licence_url": "https://creativecommons.org/licenses/by/4.0/",
    "terms_url": "https://data.who.int/redirect-pages/footer/learn-more/terms-and-conditions",
    "attribution": "World Health Organization, Global Health Estimates (GHE): deaths and DALYs by cause, "
    "country and year (WHO data portal)",
    "source_key": SOURCE_KEY,
}

#: (column, measure, unit, low column, high column)
MEASURES: tuple[tuple[str, str, str, str | None, str | None], ...] = (
    ("VAL_DTHS_COUNT_NUMERIC", "deaths", "deaths", "VAL_DTHS_COUNT_LOW", "VAL_DTHS_COUNT_HIGH"),
    ("VAL_DTHS_RATE100K_NUMERIC", "deaths_rate100k", "deaths per 100 000 population", None, None),
    # DALY uncertainty bounds (VAL_DALY_COUNT_LOW/HIGH) are not requested: they add ~15 % to the 50 MB run and the
    # V4 raw-archive cap is 150 MB per agent (docs/dev/v4_v4health.md, "finish on a bigger machine")
    ("VAL_DALY_COUNT_NUMERIC", "dalys", "disability-adjusted life years (DALYs)", None, None),
    ("VAL_DALY_RATE100K_NUMERIC", "dalys_rate100k", "DALYs per 100 000 population", None, None),
)
DIMS = ("DIM_COUNTRY_CODE", "DIM_YEAR_CODE", "DIM_GHECAUSE_CODE")
SELECT: tuple[str, ...] = (
    *DIMS,
    *dict.fromkeys(c for m in MEASURES for c in (m[0], m[3], m[4]) if c),
)
CLASSIFIED_BY = "V4HEALTH catalog, from WHO GHE methods (modelled, comparable across countries)"


class GheParseError(SourceResponseError):
    pass


def _err(msg: str) -> GheParseError:
    return GheParseError(msg, retryable=False, kind="schema_drift")


def _q(s: str) -> str:
    return quote(s, safe="'(),$=")


def groupby_url(cols: Sequence[str]) -> str:
    return f"{BASE}?$apply=" + _q(f"groupby(({','.join(cols)}))")


YEARS_URL = groupby_url(["DIM_YEAR_CODE"])
COUNTRIES_URL = groupby_url(["DIM_COUNTRY_CODE"])
CAUSES_URL = groupby_url(["DIM_GHECAUSE_CODE", "DIM_GHECAUSE_TITLE", "FLAG_LEVEL"])


def country_url(iso3: str, latest_year: int) -> str:
    flt = (
        f"DIM_COUNTRY_CODE eq '{iso3}' and DIM_AGEGROUP_CODE eq 'TOTAL' and DIM_SEX_CODE eq 'TOTAL' and "
        f"(FLAG_LEVEL le {MAX_LEVEL_ALL_YEARS} or DIM_YEAR_CODE eq {latest_year})"
    )
    return f"{BASE}?$filter={_q(flt)}&$select={','.join(SELECT)}"


def _values(body: bytes, what: str) -> list[dict[str, Any]]:
    try:
        doc = json_decimal(body)
        rows = doc["value"]
    except (ValueError, KeyError, TypeError) as e:
        raise _err(f"{what}: not the expected OData JSON") from e
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise _err(f"{what}: 'value' is not a list of objects")
    if "@odata.nextLink" in doc:
        raise _err(f"{what}: response is paged (@odata.nextLink); paging is not implemented")
    return rows


def parse_years(body: bytes) -> list[int]:
    out = []
    for r in _values(body, "year list"):
        y = r.get("DIM_YEAR_CODE")
        if not isinstance(y, Decimal) or y != int(y):
            raise _err(f"year list: non-integer year {y!r}")
        out.append(int(y))
    if not out:
        raise _err("year list is empty")
    return sorted(out)


def parse_countries(body: bytes) -> list[str]:
    out = []
    for r in _values(body, "country list"):
        c = r.get("DIM_COUNTRY_CODE")
        if not isinstance(c, str) or len(c) != 3 or not c.isalpha() or not c.isupper():
            raise _err(f"country list: unexpected code {c!r}")
        out.append(c)
    if not out:
        raise _err("country list is empty")
    return sorted(set(out))


def parse_causes(body: bytes) -> dict[int, tuple[str, int]]:
    out: dict[int, tuple[str, int]] = {}
    for r in _values(body, "cause list"):
        code, title, level = r.get("DIM_GHECAUSE_CODE"), r.get("DIM_GHECAUSE_TITLE"), r.get("FLAG_LEVEL")
        if not isinstance(code, Decimal) or not isinstance(title, str) or not isinstance(level, Decimal):
            raise _err(f"cause list: unexpected row {r!r}"[:200])
        c = int(code)
        if c in out and out[c] != (title, int(level)):
            raise _err(f"cause list: code {c} has two titles/levels")
        out[c] = (title, int(level))
    if 0 not in out:
        raise _err("cause list lacks 0 (All Causes)")
    return out


def parse_country(body: bytes, iso3: str) -> list[dict[str, Any]]:
    rows = _values(body, iso3)
    for i, r in enumerate(rows):
        missing = [c for c in SELECT if c not in r]
        if missing:
            raise _err(f"{iso3}: row {i} lacks {missing}")
        if r["DIM_COUNTRY_CODE"] != iso3:
            raise _err(f"{iso3}: row {i} is for {r['DIM_COUNTRY_CODE']!r}")
        for c in ("DIM_YEAR_CODE", "DIM_GHECAUSE_CODE"):
            if not isinstance(r[c], Decimal) or r[c] != int(r[c]):
                raise _err(f"{iso3}: row {i} {c} is not an integer")
        for m in MEASURES:
            for col in (m[0], m[3], m[4]):
                if col and r[col] is not None and not isinstance(r[col], Decimal):
                    raise _err(f"{iso3}: row {i} {col} is not a number")
    return rows


def _num(v: Any) -> str | None:
    return None if v is None else dec_str(v)


def atlas_rows(
    rows: Sequence[Mapping[str, Any]],
    causes: Mapping[int, tuple[str, int]],
    *,
    iso3: str,
    raw_sha256: str,
    item_key: str,
) -> list[dict[str, Any]]:
    from reva.atlas.tables import missing_flags, year_period

    out: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for idx, r in enumerate(rows):
        year, cause = int(r["DIM_YEAR_CODE"]), int(r["DIM_GHECAUSE_CODE"])
        if (year, cause) in seen:
            raise _err(f"{iso3}: duplicate row for year {year} cause {cause}")
        seen.add((year, cause))
        if cause not in causes:
            raise _err(f"{iso3}: cause {cause} is not in the cause list")
        title, level = causes[cause]
        start, end = year_period(year)
        for col, measure, unit, lo, hi in MEASURES:
            flags: dict[str, Any] = {
                "estimate_class": "who_comparable_estimate",
                "producer": "WHO Global Health Estimates (GHE)",
                "classified_by": CLASSIFIED_BY,
                "cause_title": title,
                "cause_level": level,
                "age": "TOTAL",
                "sex": "TOTAL",
            }
            if lo and hi and (r[lo] is not None or r[hi] is not None):
                flags["low"], flags["high"] = _num(r[lo]), _num(r[hi])
                flags["interval_level"] = "not_stated_per_row"
            value = _num(r[col])
            if value is None:
                flags = json.loads(missing_flags("not_reported", **flags))
            out.append(
                {
                    "source": "who",
                    "dataset": "ghe",
                    "indicator": f"ghe.{measure}|sex=TOTAL|age=TOTAL|cause={cause}",
                    "concept_id": CONCEPT_HINTS.get(f"ghe.{measure}|sex=TOTAL|age=TOTAL|cause={cause}"),
                    "iso3": None,
                    "provider_geo": iso3,
                    "entity_qid": None,
                    "period_start": start,
                    "period_end": end,
                    "value": value,
                    "value_text": None,
                    "unit": unit,
                    "flags": fit_flags(flags),
                    "raw_sha256": raw_sha256,
                    "selector": canon({"item": item_key, "year": year, "cause": cause, "column": col, "index": idx}),
                }
            )
    return out


class WhoGheConnector:
    """Run protocol of :func:`atlas_healthedu_common.fetch_archive_register` (items, follow-ups, checks, frame)."""

    ATLAS_SOURCE = "who"
    ATLAS_DATASET = "ghe"
    GEO_SCHEME = "iso3"
    RIGHTS = RIGHTS

    def __init__(self, countries: Sequence[str] | None = None, *, min_countries: int = 1) -> None:
        self.only = tuple(sorted(countries)) if countries else None
        self.min_countries = min_countries
        self.latest: int | None = None
        self.countries: list[str] = []

    def items(self) -> tuple[FetchItem, ...]:
        return (FetchItem(key="ghe.years", url=YEARS_URL, max_bytes=200_000, params={"kind": "years"}),)

    def follow_ups(self, item: FetchItem, body: bytes) -> tuple[FetchItem, ...]:
        kind = item.params.get("kind")
        if kind == "years":
            self.latest = parse_years(body)[-1]
            return (
                FetchItem(key="ghe.countries", url=COUNTRIES_URL, max_bytes=200_000, params={"kind": "countries"}),
                FetchItem(key="ghe.causes", url=CAUSES_URL, max_bytes=500_000, params={"kind": "causes"}),
            )
        if kind == "countries":
            listed = parse_countries(body)
            self.countries = [c for c in listed if self.only is None or c in self.only]
            assert self.latest is not None
            return tuple(
                FetchItem(
                    key=f"ghe.country:{c}",
                    url=country_url(c, self.latest),
                    max_bytes=MAX_ITEM_BYTES,
                    params={"kind": "country", "iso3": c, "latest_year": self.latest},
                )
                for c in self.countries
            )
        return ()

    def check(self, item: FetchItem, body: bytes) -> dict[str, Any]:
        kind = item.params.get("kind")
        if kind == "years":
            return {"years": len(parse_years(body))}
        if kind == "countries":
            return {"countries": len(parse_countries(body))}
        if kind == "causes":
            return {"causes": len(parse_causes(body))}
        rows = parse_country(body, str(item.params["iso3"]))
        if not rows:
            raise _err(f"{item.params['iso3']}: no rows")
        return {"rows": len(rows)}

    def atlas_frame(self, bodies: Mapping[str, bytes], raws: Mapping[str, str]) -> tuple[Any, str, dict[str, Any]]:
        import polars as pl

        from reva.atlas.tables import ATLAS_SCHEMA

        if len(self.countries) < self.min_countries:
            raise ValidationProblem(
                "WHO GHE country list too short: registration refused", countries=len(self.countries)
            )
        causes = parse_causes(bodies["ghe.causes"])
        parts = []
        years: set[int] = set()
        for c in self.countries:
            key = f"ghe.country:{c}"
            rows = parse_country(bodies[key], c)
            years.update(int(r["DIM_YEAR_CODE"]) for r in rows)
            parts.append(
                pl.DataFrame(atlas_rows(rows, causes, iso3=c, raw_sha256=raws[key], item_key=key), schema=ATLAS_SCHEMA)
            )
        frame = pl.concat(parts, how="vertical")
        note = (
            f"WHO Global Health Estimates (xMart GHE_FULL), all ages, both sexes: cause levels 0-{MAX_LEVEL_ALL_YEARS} "
            f"for {min(years)}-{max(years)} and all {len(causes)} causes for {self.latest}; {len(self.countries)} "
            "countries; deaths, deaths per 100 000, DALYs, DALYs per 100 000 with WHO uncertainty bounds."
        )
        return frame, note, {"latest_year": self.latest, "countries": len(self.countries), "causes": len(causes)}


#: Fixture mode (``--fixture`` / tests) replays the captured subset in tests/fixtures/source/atlas.who_ghe.
FIXTURE_COUNTRIES = ("FRA", "NGA")


def connector_for(run: Any) -> WhoGheConnector:
    if run.origin == "test_fixture":
        return WhoGheConnector(FIXTURE_COUNTRIES)
    return WhoGheConnector(min_countries=MIN_COUNTRIES_LIVE)


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=WhoGheConnector.ATLAS_SOURCE,
        provider="World Health Organization (Global Health Estimates, WHO data portal xMart API)",
        licence=RIGHTS["licence"],
        allowed_hosts=(HOST,),
        datasets=(WhoGheConnector.ATLAS_DATASET,),
        dataset_ids=("DEX_CMS/GHE_FULL",),
        cadence_seconds=90 * 86400,
        planned_requests=3 + 190,
        max_response_bytes=MAX_ITEM_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=module_digest(WhoGheConnector),
        field_mappings={
            "select": list(SELECT),
            "measures": {m[1]: m[0] for m in MEASURES},
            "slice": "DIM_AGEGROUP_CODE='TOTAL', DIM_SEX_CODE='TOTAL', FLAG_LEVEL<=2 all years + all levels latest year",
        },
        notes="One request per country (provider-side filter), plus year/country/cause lists; partial run registers "
        "nothing.",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """ATLASCORE entry point (``uv run reva atlas ingest atlas.who_ghe``)."""
    return await fetch_archive_register(run, connector_for(run), dataset=WhoGheConnector.ATLAS_DATASET)


#: Folded indicator -> catalog concept (registry/atlas/catalog/health_body.yaml).
CONCEPT_HINTS: dict[str, str] = {}


def _load_hints() -> None:
    from reva.ontology.registry import REGISTRY_ROOT

    path = REGISTRY_ROOT / "atlas" / "catalog" / "health_body.yaml"
    if not path.exists():
        return
    import yaml

    doc = yaml.safe_load(path.read_text())
    prefix = "who.ghe."
    for c in doc.get("concepts") or []:
        for s in c.get("source_order") or []:
            if isinstance(s, str) and s.startswith(prefix):
                CONCEPT_HINTS.setdefault(s[len(prefix) :], c["id"])


_load_hints()

__all__ = [
    "CONNECTOR_NAME",
    "RIGHTS",
    "SOURCE_KEY",
    "WhoGheConnector",
    "atlas_ingest",
    "atlas_rows",
    "atlas_source",
    "country_url",
    "parse_causes",
    "parse_country",
]
