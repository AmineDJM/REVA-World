"""Gapminder long series (population, life expectancy, GDP per capita since 1800) -> atlas table
``gapminder.fasttrack`` (V3, agent A-HISTECON).

Source
------
Gapminder "Fast Track" DDF dataset (``github.com/open-numbers/ddf--gapminder--fasttrack``, served by
``raw.githubusercontent.com``), the dataset behind gapminder.org's own tools. Licence **CC BY 4.0**
("All Gapminder material linking here are freely available under the Creative Commons Attribution 4.0
International license", gapminder.org/free-material, checked 2026-10-15). Attribution text required by
Gapminder: "Based on free material from GAPMINDER.ORG, CC-BY LICENSE".

Five requests per run: ``datapackage.json`` (dataset revision: ``created``), ``ddf--concepts.csv``
(series version and documentation URL), ``ddf--entities--geo--country.csv`` (Gapminder geo code ->
ISO 3166 alpha-3, the provider's own crosswalk) and the three datapoint CSVs ``pop``, ``lex``,
``gdp_pcap`` (by country and year).

Estimates vs projections (provider documentation, quoted in flags)
------------------------------------------------------------------
The Fast Track series run to 2100. The years after the provider's last *estimate* year are UN
projections (``pop``), UN-forecast extensions (``lex``) or an explicit "IF-scenario" (``gdp_pcap``);
they are **not** loaded (counted in the report as ``excluded_projection_cells``). The cut-off and the
segment notes come from the documentation page of each series (``SERIES``); when the concept version
in ``ddf--concepts.csv`` differs from the documented version, rows carry
``documentation_version_mismatch`` and the documented cut-off is still applied (conservative).
Historical segments are composite reconstructions by Gapminder (Maddison/Clio-Infra-based, adjusted
to meet the modern series): every row carries its segment note; Gapminder values are therefore not an
independent confirmation of Maddison or Clio-Infra values for the same country-year.
"""

from __future__ import annotations

import csv
import io
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from reva.adapters.connectors.atlas_history_common import (
    DAY,
    AtlasBuild,
    canon,
    code_digest,
    decimal_text,
    fetch_plain,
    frame,
    historical_qid,
    ingest_with_run,
    sha256_hex,
    source_spec,
    year_period,
)
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import FetchItem, RawAcquisition

NAME = "atlas.gapminder.fasttrack"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_gapminder/1"
SOURCE_KEY = "gapminder.fasttrack"
ATLAS_SOURCE = "gapminder"
ATLAS_DATASET = "fasttrack"
HOST = "raw.githubusercontent.com"
BASE = f"https://{HOST}/open-numbers/ddf--gapminder--fasttrack/master"
MAX_BYTES = 8_000_000

RIGHTS: dict[str, str] = {
    "licence": "CC BY 4.0",
    "licence_url": "https://creativecommons.org/licenses/by/4.0/",
    "attribution": "Based on free material from GAPMINDER.ORG, CC-BY LICENSE",
    "terms_url": "https://www.gapminder.org/free-material/",
}

#: Documented series (gapminder.org/data/documentation, read 2026-10-15). ``last_estimate_year``: later
#: years are projections / scenario and are not loaded.
SERIES: dict[str, dict[str, Any]] = {
    "pop": {
        "unit": "persons",
        "concept_id": "history.population_persons",
        "documented_version": "v8",
        "doc_url": "https://www.gapminder.org/data/documentation/gd003/",
        "last_estimate_year": 2023,
        "segments": [
            (
                1800,
                1949,
                "Clio Infra population v2015 where available, otherwise Gapminder v3 (Maddison-based); "
                "small adjustments before 1950 to meet the UN series",
            ),
            (1950, 2023, "UN World Population Prospects 2024 (estimates)"),
        ],
        "excluded_note": "2024-2100: UN WPP 2024 medium-variant projection (not loaded)",
    },
    "lex": {
        "unit": "years",
        "concept_id": "history.life_expectancy_at_birth",
        "documented_version": "v14",
        "doc_url": "https://www.gapminder.org/data/documentation/gd004/",
        "last_estimate_year": 2019,
        "segments": [
            (
                1800,
                1949,
                "Gapminder v7 (about 100 sources compiled by K. Johansson and M. Lindgren, incl. "
                "disaster estimates), adjusted to meet the IHME series",
            ),
            (1950, 2019, "IHME Global Burden of Disease 2019 life tables"),
        ],
        "excluded_note": "2020-2100: IHME series extended with UN WPP forecast change rates (not loaded)",
    },
    "gdp_pcap": {
        "unit": "international $, constant 2021 prices (PPP)",
        "concept_id": "history.gdp_per_capita_ppp_2021",
        "documented_version": None,  # the documentation page does not state a version number
        "doc_url": "https://www.gapminder.org/data/documentation/gd001/",
        "last_estimate_year": 2024,
        "segments": [
            (
                1800,
                1989,
                "historic series (Maddison Project 2023 and Gapminder's own estimates) shifted to "
                "PPP 2021 and blended into the modern series over 70 years",
            ),
            (1990, 2024, "World Bank WDI GDP per capita, PPP (constant 2021 international $)"),
        ],
        "excluded_note": "2025-2100: Gapminder 'IF-scenario' (IMF WEO / Atlas of Economic Complexity growth "
        "rates, then a growth model) -- not data, not loaded",
    },
}
FILES = {
    "datapackage": "datapackage.json",
    "concepts": "ddf--concepts.csv",
    "geo": "ddf--entities--geo--country.csv",
    **{f"dp.{k}": f"countries_etc_datapoints/ddf--datapoints--{k}--by--country--time.csv" for k in SERIES},
}
#: Gapminder geos without an ISO alpha-3 in the provider's entity file whose *name* is exactly a crosswalk
#: entity label (agent mapping, evidence = provider entity row; ISO-less in both lists).
NAME_ALIASES = {"kos": ("XKX", "Kosovo"), "chanisl": ("CHI", "Channel Islands")}


class GapminderParseError(SourceResponseError):
    pass


def _err(msg: str) -> GapminderParseError:
    return GapminderParseError(msg, retryable=False, kind="schema_drift")


def _csv(body: bytes, what: str) -> list[dict[str, str]]:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise _err(f"{what} is not UTF-8") from e
    return list(csv.DictReader(io.StringIO(text)))


def parse_concepts(body: bytes) -> dict[str, dict[str, str]]:
    rows = _csv(body, "ddf--concepts.csv")
    if not rows or "concept" not in rows[0]:
        raise _err("ddf--concepts.csv has no 'concept' column")
    return {r["concept"]: r for r in rows if r.get("concept") in SERIES}


def parse_geo(body: bytes) -> dict[str, dict[str, str]]:
    rows = _csv(body, "geo entities")
    if not rows or not {"country", "name", "iso3166_1_alpha3"} <= set(rows[0]):
        raise _err("geo entity file lacks country/name/iso3166_1_alpha3")
    return {r["country"]: {"name": r["name"], "alpha3": r["iso3166_1_alpha3"] or ""} for r in rows}


def geo_iso3(code: str, geo: Mapping[str, Mapping[str, str]]) -> tuple[str | None, str]:
    """(crosswalk key or None, how it was mapped)."""
    from reva.atlas.tables import country_iso3

    ent = geo.get(code)
    if ent and ent["alpha3"]:
        k = country_iso3(ent["alpha3"], "iso3")
        return k, "provider_iso3166_alpha3" if k else "provider_alpha3_not_in_crosswalk"
    if code in NAME_ALIASES and ent and ent["name"] == NAME_ALIASES[code][1]:
        return NAME_ALIASES[code][0], "agent_alias_exact_name"
    return None, "no_iso_code"


def parse_datapoints(body: bytes, concept: str) -> list[dict[str, Any]]:
    rows = _csv(body, f"datapoints {concept}")
    if not rows or list(rows[0]) != ["country", "time", concept]:
        raise _err(f"datapoints {concept}: header {list(rows[0]) if rows else None}")
    out = []
    for i, r in enumerate(rows, start=2):  # line 1 is the header
        t = r["time"].strip()
        if not t.isdigit():
            raise _err(f"datapoints {concept} line {i}: time {t!r}")
        raw = r[concept]
        if raw is None or raw.strip() == "":
            continue
        v = decimal_text(raw)
        if v is None:
            raise _err(f"datapoints {concept} line {i}: value {raw[:20]!r} is not a number")
        out.append({"geo": r["country"], "year": int(t), "value": v, "line": i})
    return out


def _segment(spec: Mapping[str, Any], year: int) -> str | None:
    for lo, hi, note in spec["segments"]:
        if lo <= year <= hi:
            return note
    return None


class GapminderConnector:
    """Gapminder Fast Track: pop, lex, gdp_pcap by country and year (estimates only)."""

    def describe(self) -> Any:
        return atlas_source()

    def fetch_items(self) -> tuple[FetchItem, ...]:
        return tuple(
            FetchItem(
                key=k,
                url=f"{BASE}/{path}",
                max_bytes=MAX_BYTES,
                expected_media_types=("text/plain", "text/csv", "application/json"),
            )
            for k, path in FILES.items()
        )

    def follow_up(self, raw: RawAcquisition) -> tuple[FetchItem, ...]:
        return ()

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition:
        return await fetch_plain(item, http)

    def build(self, raws: Sequence[RawAcquisition]) -> list[AtlasBuild]:
        by = {r.item.key: r for r in raws}
        if set(by) != set(FILES):
            raise _err(f"expected {sorted(FILES)}, got {sorted(by)}")
        try:
            dp = json.loads(by["datapackage"].body)
        except ValueError as e:
            raise _err("datapackage.json is not JSON") from e
        revision = str(dp.get("created") or "")[:100] or None
        concepts = parse_concepts(by["concepts"].body)
        geo = parse_geo(by["geo"].body)
        rows: list[dict[str, Any]] = []
        used = [sha256_hex(by[k].body) for k in ("datapackage", "concepts", "geo")]
        report: dict[str, Any] = {"revision": revision, "series": {}, "unmapped_geos": {}}
        for concept, spec in SERIES.items():
            meta = concepts.get(concept)
            if meta is None:
                raise _err(f"concept {concept} missing from ddf--concepts.csv")
            raw = by[f"dp.{concept}"]
            sha = sha256_hex(raw.body)
            used.append(sha)
            points = parse_datapoints(raw.body, concept)
            version = meta.get("version") or None
            mismatch = spec["documented_version"] is not None and version != spec["documented_version"]
            excluded = 0
            kept_geo: Counter[str] = Counter()
            for p in points:
                if p["year"] > spec["last_estimate_year"]:
                    excluded += 1
                    continue
                iso3, how = geo_iso3(p["geo"], geo)
                flags: dict[str, Any] = {
                    "series_version": version,
                    "series_updated": meta.get("updated") or None,
                    "doc_url": spec["doc_url"],
                    "segment": _segment(spec, p["year"]),
                    "provider_name": (geo.get(p["geo"]) or {}).get("name"),
                    "geo_mapping": how,
                    "licence": RIGHTS["licence"],
                    "composite_reconstruction": p["year"] < spec["segments"][-1][0],
                }
                if revision:
                    flags["dataset_created"] = revision
                if mismatch:
                    flags["documentation_version_mismatch"] = {
                        "documented": spec["documented_version"],
                        "data": version,
                        "applied": f"documented cut-off {spec['last_estimate_year']} and segments of the documented version",
                    }
                q = None if iso3 else historical_qid("gapminder", p["geo"])
                if q is not None:
                    flags["entity_mapping"] = {
                        "qid": q["qid"],
                        "review": q.get("review_status", "agent_mapped"),
                        "file": "registry/entities/historical_states_qids.json",
                    }
                if iso3 is None:
                    report["unmapped_geos"][p["geo"]] = (geo.get(p["geo"]) or {}).get("name")
                kept_geo[p["geo"]] += 1
                start, end = year_period(p["year"])
                rows.append(
                    {
                        "source": ATLAS_SOURCE,
                        "dataset": ATLAS_DATASET,
                        "indicator": concept,
                        "concept_id": spec["concept_id"],
                        "iso3": iso3,
                        "provider_geo": p["geo"][:64],
                        "entity_qid": q["qid"] if q else None,
                        "period_start": start,
                        "period_end": end,
                        "value": p["value"],
                        "value_text": None,
                        "unit": spec["unit"],
                        "flags": canon(flags),
                        "raw_sha256": sha,
                        "selector": canon(file=FILES[f"dp.{concept}"], line=p["line"], column=concept),
                    }
                )
            years = [p["year"] for p in points if p["year"] <= spec["last_estimate_year"]]
            report["series"][concept] = {
                "version": version,
                "documented_version": spec["documented_version"],
                "version_mismatch": mismatch,
                "cells": len(years),
                "geos": len(kept_geo),
                "years": [min(years), max(years)] if years else None,
                "excluded_projection_cells": excluded,
                "excluded_note": spec["excluded_note"],
            }
        note = (
            "Gapminder Fast Track (CC BY 4.0): pop 1800-2023, lex 1800-2019, gdp_pcap 1800-2024 per country "
            "(projection / IF-scenario years excluded); composite reconstructions before the modern segment, "
            "segment noted on every row."
        )
        return [
            AtlasBuild(
                ATLAS_SOURCE,
                ATLAS_DATASET,
                frame(rows),
                sorted(set(used)),
                PARSER_VERSION,
                note,
                geo_scheme=None,
                rights=dict(RIGHTS),
                report=report,
            )
        ]


def atlas_source() -> Any:
    """The atlas platform declaration (``reva.atlas.sources``)."""
    return source_spec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        datasets=(ATLAS_DATASET,),
        provider="Gapminder Foundation (Fast Track DDF dataset, open-numbers)",
        licence=RIGHTS["licence"],
        allowed_hosts=(HOST,),
        dataset_ids=("ddf--gapminder--fasttrack",),
        field_mappings={k: FILES[f"dp.{k}"] for k in SERIES},
        units={k: v["unit"] for k, v in SERIES.items()},
        geography=("Gapminder geo codes, mapped through the provider's ISO 3166 alpha-3 column",),
        time_semantics="calendar year; projection/scenario years after the documented last estimate excluded",
        lineage_family="gapminder",
        cadence_seconds=30 * DAY,
        revision_strategy="datapackage.json 'created' + concept versions; changed bytes -> new atlas table version",
        planned_requests=len(FILES),
        max_response_bytes=MAX_BYTES,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    out = await ingest_with_run(run, GapminderConnector())
    return {k: v for k, v in out.items() if not k.startswith("_")}
