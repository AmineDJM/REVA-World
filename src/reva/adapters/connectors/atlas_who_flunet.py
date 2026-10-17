"""WHO FluNet: weekly influenza laboratory surveillance by country -> atlas table ``who.flunet`` (V6, agent
PRED-HEALTH).

Source
------
WHO Global Influenza Surveillance and Response System (GISRS), FluNet, served by the WHO xMart public API (OData
v4): entity set ``VIW_FNT`` of ``https://xmart-api-public.who.int/FLUMART`` (the table behind the WHO FluNet
dashboard / "FluNet data download"). One row per country x ISO week x ``ORIGIN_SOURCE`` (``SENTINEL``,
``NONSENTINEL``, ``NOTDEFINED``): specimens received / processed, influenza positives by type and subtype,
negatives. Open, no account. Licence: WHO datasets are **CC BY 4.0** with WHO's additional terms (same terms as
``atlas.who_gho`` / ``atlas.who_ghe``), recorded in :data:`RIGHTS`; attribution "WHO FluNet (GISRS)".

Requests (one run): one provider-side slice per panel country (``$filter=COUNTRY_CODE eq '<ISO3>' and ISO_YEAR ge
<FIRST_YEAR>``, ``$select`` of :data:`SELECT` only), ~180-370 kB each, ~7 MB per run for the 30-country panel
(:data:`PANEL`). Paging (``@odata.nextLink``) is refused as drift (not observed for these slices).

What a cell is
--------------
* ``indicator`` = ``flunet.<column>|origin=<ORIGIN_SOURCE>``, column in :data:`COLUMNS` (verbatim FluNet column
  names, lower-cased): ``spec_processed_nb``, ``spec_received_nb``, ``inf_a``, ``inf_b``, ``inf_all``,
  ``inf_negative``.
* period = the ISO week ``[ISO_WEEKSTARTDATE, +7 days)`` (Monday to Monday); ``flags.iso_year`` / ``iso_week``
  verbatim; ``provider_geo`` = ``COUNTRY_CODE`` (ISO 3166-1 alpha-3 as published; geo scheme ``iso3``).
* ``value`` = the API's number exactly as served; a null is Missing (``flags.missing = not_reported``), never zero.
  FluNet leaves many columns null where a country does not report them; the forecasting layer
  (:mod:`reva.evaluation.health_registry`) derives positivity only from published numbers (see there).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from reva.adapters.connectors.atlas_healthedu_common import canon, fetch_archive_register, fit_flags
from reva.adapters.connectors.base import dec_str, json_decimal, module_digest
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError, ValidationProblem
from reva.ports.connectors import FetchItem

CONNECTOR_NAME = "atlas.who_flunet"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas-who-flunet-parse-1"
SOURCE_KEY = "who.flunet"
HOST = "xmart-api-public.who.int"
BASE = f"https://{HOST}/FLUMART/VIW_FNT"
FIRST_YEAR = 2015
MAX_ITEM_BYTES = 6_000_000  # one country slice was 170-370 kB on 2026-10-17

RIGHTS: dict[str, str] = {
    "licence": "CC BY 4.0 (WHO additional terms)",
    "licence_url": "https://creativecommons.org/licenses/by/4.0/",
    "terms_url": "https://data.who.int/redirect-pages/footer/learn-more/terms-and-conditions",
    "attribution": "World Health Organization, Global Influenza Surveillance and Response System (GISRS), FluNet "
    "(xMart FLUMART VIW_FNT)",
    "source_key": SOURCE_KEY,
}

#: Panel: long FluNet series in both hemispheres and the tropics (ISO 3166-1 alpha-3 as FluNet publishes them).
PANEL: tuple[str, ...] = (
    "ARG",
    "AUS",
    "BRA",
    "CAN",
    "CHL",
    "CHN",
    "COL",
    "DEU",
    "EGY",
    "ESP",
    "FRA",
    "GBR",
    "IND",
    "ITA",
    "JPN",
    "KEN",
    "KOR",
    "MAR",
    "MEX",
    "NLD",
    "NOR",
    "NZL",
    "PER",
    "POL",
    "RUS",
    "SWE",
    "THA",
    "USA",
    "ZAF",
    "IRN",
)
#: (FluNet column, unit) — numbers of specimens.
COLUMNS: tuple[tuple[str, str], ...] = (
    ("SPEC_PROCESSED_NB", "specimens processed"),
    ("SPEC_RECEIVED_NB", "specimens received"),
    ("INF_A", "specimens positive for influenza A"),
    ("INF_B", "specimens positive for influenza B"),
    ("INF_ALL", "specimens positive for influenza"),
    ("INF_NEGATIVE", "specimens negative for influenza"),
)
DIMS = (
    "COUNTRY_CODE",
    "COUNTRY_AREA_TERRITORY",
    "WHOREGION",
    "HEMISPHERE",
    "ISO_WEEKSTARTDATE",
    "ISO_YEAR",
    "ISO_WEEK",
    "ORIGIN_SOURCE",
)
SELECT: tuple[str, ...] = (*DIMS, *(c for c, _ in COLUMNS))
ORIGINS = ("SENTINEL", "NONSENTINEL", "NOTDEFINED")


class FluNetParseError(SourceResponseError):
    pass


def _err(msg: str) -> FluNetParseError:
    return FluNetParseError(msg, retryable=False, kind="schema_drift")


def _q(s: str) -> str:
    return quote(s, safe="'(),$=")


def country_url(iso3: str, first_year: int = FIRST_YEAR) -> str:
    flt = f"COUNTRY_CODE eq '{iso3}' and ISO_YEAR ge {first_year}"
    return f"{BASE}?$filter={_q(flt)}&$select={','.join(SELECT)}"


def parse_country(body: bytes, iso3: str) -> list[dict[str, Any]]:
    try:
        doc = json_decimal(body)
        rows = doc["value"]
    except (ValueError, KeyError, TypeError) as e:
        raise _err(f"{iso3}: not the expected OData JSON") from e
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise _err(f"{iso3}: 'value' is not a list of objects")
    if "@odata.nextLink" in doc:
        raise _err(f"{iso3}: response is paged (@odata.nextLink); paging is not implemented")
    seen: set[tuple[str, str]] = set()
    for i, r in enumerate(rows):
        missing = [c for c in SELECT if c not in r]
        if missing:
            raise _err(f"{iso3}: row {i} lacks {missing}")
        if r["COUNTRY_CODE"] != iso3:
            raise _err(f"{iso3}: row {i} is for {r['COUNTRY_CODE']!r}")
        if r["ORIGIN_SOURCE"] not in ORIGINS:
            raise _err(f"{iso3}: row {i} unknown ORIGIN_SOURCE {r['ORIGIN_SOURCE']!r}")
        try:
            start = date.fromisoformat(str(r["ISO_WEEKSTARTDATE"])[:10])
        except ValueError as e:
            raise _err(f"{iso3}: row {i} ISO_WEEKSTARTDATE {r['ISO_WEEKSTARTDATE']!r}") from e
        iy, iw, _ = start.isocalendar()
        if start.weekday() != 0 or (Decimal(iy), Decimal(iw)) != (r["ISO_YEAR"], r["ISO_WEEK"]):
            raise _err(f"{iso3}: row {i} week start {start} is not ISO {r['ISO_YEAR']}-W{r['ISO_WEEK']}")
        key = (start.isoformat(), str(r["ORIGIN_SOURCE"]))
        if key in seen:
            raise _err(f"{iso3}: duplicate row for {key}")
        seen.add(key)
        for c, _ in COLUMNS:
            v = r[c]
            if v is not None and (not isinstance(v, Decimal) or v < 0):
                raise _err(f"{iso3}: row {i} {c} is not a non-negative number")
    return rows


def atlas_rows(rows: Sequence[Mapping[str, Any]], *, iso3: str, raw_sha256: str, item_key: str) -> list[dict[str, Any]]:
    from reva.atlas.tables import missing_flags

    out: list[dict[str, Any]] = []
    for idx, r in enumerate(rows):
        start = date.fromisoformat(str(r["ISO_WEEKSTARTDATE"])[:10])
        origin = str(r["ORIGIN_SOURCE"])
        base = {
            "producer": "WHO GISRS FluNet",
            "iso_year": int(r["ISO_YEAR"]),
            "iso_week": int(r["ISO_WEEK"]),
            "origin_source": origin,
            "who_region": r["WHOREGION"],
            "hemisphere": r["HEMISPHERE"],
            "country_area_territory": r["COUNTRY_AREA_TERRITORY"],
        }
        for col, unit in COLUMNS:
            v = r[col]
            flags = dict(base)
            if v is None:
                flags = json.loads(missing_flags("not_reported", **flags))
            out.append(
                {
                    "source": "who",
                    "dataset": "flunet",
                    "indicator": f"flunet.{col.lower()}|origin={origin}",
                    "concept_id": None,
                    "iso3": None,
                    "provider_geo": iso3,
                    "entity_qid": None,
                    "period_start": start,
                    "period_end": start + timedelta(days=7),
                    "value": None if v is None else dec_str(v),
                    "value_text": None,
                    "unit": unit,
                    "flags": fit_flags(flags),
                    "raw_sha256": raw_sha256,
                    "selector": canon({"item": item_key, "index": idx, "column": col}),
                }
            )
    return out


class WhoFluNetConnector:
    """Run protocol of :func:`atlas_healthedu_common.fetch_archive_register`."""

    ATLAS_SOURCE = "who"
    ATLAS_DATASET = "flunet"
    GEO_SCHEME = "iso3"
    RIGHTS = RIGHTS

    def __init__(self, countries: Sequence[str] = PANEL, *, first_year: int = FIRST_YEAR) -> None:
        self.countries = tuple(countries)
        self.first_year = first_year

    def items(self) -> tuple[FetchItem, ...]:
        return tuple(
            FetchItem(
                key=f"flunet.country:{c}",
                url=country_url(c, self.first_year),
                max_bytes=MAX_ITEM_BYTES,
                params={"iso3": c, "first_year": self.first_year},
            )
            for c in self.countries
        )

    def follow_ups(self, item: FetchItem, body: bytes) -> tuple[FetchItem, ...]:
        return ()

    def check(self, item: FetchItem, body: bytes) -> dict[str, Any]:
        rows = parse_country(body, str(item.params["iso3"]))
        return {"rows": len(rows)}

    def atlas_frame(self, bodies: Mapping[str, bytes], raws: Mapping[str, str]) -> tuple[Any, str, dict[str, Any]]:
        import polars as pl

        from reva.atlas.tables import ATLAS_SCHEMA

        parts = []
        weeks: list[date] = []
        empty: list[str] = []
        for c in self.countries:
            key = f"flunet.country:{c}"
            rows = parse_country(bodies[key], c)
            if not rows:
                empty.append(c)
                continue
            weeks.extend(date.fromisoformat(str(r["ISO_WEEKSTARTDATE"])[:10]) for r in rows)
            parts.append(
                pl.DataFrame(atlas_rows(rows, iso3=c, raw_sha256=raws[key], item_key=key), schema=ATLAS_SCHEMA)
            )
        if not parts:
            raise ValidationProblem("FluNet returned no rows for any panel country: registration refused")
        frame = pl.concat(parts, how="vertical")
        note = (
            f"WHO FluNet (xMart FLUMART VIW_FNT), ISO weeks {min(weeks)}..{max(weeks)} (week starts), "
            f"{len(parts)} countries, per ORIGIN_SOURCE: specimens received/processed, influenza A/B/all positives, "
            "negatives (verbatim counts; nulls kept as Missing)."
        )
        return frame, note, {"countries": len(parts), "empty_countries": empty, "last_week_start": str(max(weeks))}


#: Fixture mode (``--fixture`` / tests) replays the captured subset in tests/fixtures/source/atlas.who_flunet.
FIXTURE_COUNTRIES = ("FRA", "AUS")


def connector_for(run: Any) -> WhoFluNetConnector:
    if run.origin == "test_fixture":
        return WhoFluNetConnector(FIXTURE_COUNTRIES)
    return WhoFluNetConnector()


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=WhoFluNetConnector.ATLAS_SOURCE,
        provider="World Health Organization (GISRS FluNet, WHO xMart public API)",
        licence=RIGHTS["licence"],
        allowed_hosts=(HOST,),
        datasets=(WhoFluNetConnector.ATLAS_DATASET,),
        dataset_ids=("FLUMART/VIW_FNT",),
        cadence_seconds=7 * 86400,
        planned_requests=len(PANEL),
        max_response_bytes=MAX_ITEM_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=module_digest(WhoFluNetConnector),
        field_mappings={"select": list(SELECT), "panel": list(PANEL), "first_iso_year": FIRST_YEAR},
        notes="One request per panel country (provider-side filter and column selection); a failed slice "
        "registers nothing.",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """ATLASCORE entry point (``uv run reva atlas ingest atlas.who_flunet``)."""
    return await fetch_archive_register(run, connector_for(run), dataset=WhoFluNetConnector.ATLAS_DATASET)


__all__ = [
    "CONNECTOR_NAME",
    "PANEL",
    "RIGHTS",
    "SOURCE_KEY",
    "WhoFluNetConnector",
    "atlas_ingest",
    "atlas_rows",
    "atlas_source",
    "country_url",
    "parse_country",
]
