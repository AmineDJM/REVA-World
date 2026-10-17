"""Time use (SDG 5.4.1, unpaid domestic chores and care work) -> atlas table ``un_sdg.sdg_timeuse`` and KG
``survey_wave`` entities (V4, agent V4MIND).

Source: UN Statistics Division, SDG Global Database API v5 (``unstats.un.org``; budget key ``un.sdg`` shared with
A-UN; licence: UNdata terms of use, free use with attribution), series (descriptions verbatim):

* ``SL_DOM_TSPD`` "Proportion of time spent on unpaid domestic chores and care work, by sex, age and location (%)"
* ``SL_DOM_TSPDCW`` "Proportion of time spent on unpaid care work, by sex, age and location (%)"
* ``SL_DOM_TSPDDC`` "Proportion of time spent on unpaid domestic chores, by sex, age and location (%)"

A-UN's ``un_sdg.sdg_headline`` table does not hold these series (checked 2026-10-17). Each series is one page of at
most 10,000 records (the API reports ``totalElements``; a series whose record count does not match is left out
and reported). Cells: every dimension (Sex, Age, Location, Reporting Type) folded into the indicator, verbatim
(``SL_DOM_TSPD|Age=15+|Location=ALLAREA|Reporting Type=G|Sex=FEMALE``); value = the API's ``value`` text (strict
decimal); flags keep the ``Nature`` attribute (C country data, CA country adjusted, E estimated, N non-relevant),
the provider ``source`` text verbatim (it names the national time-use survey) and the footnotes. The period is
the year the API gives (``timePeriodStart``): the survey reference year.

Time-use OECD alternative: OECD ``DSD_TIME_USE@DF_TIME_USE`` (minutes per day by activity) has **no time
dimension** (no survey year in the data, checked 2026-10-17), so it cannot be placed in time and is not loaded.

KG: one ``survey_wave`` entity per (geography, year, source text) -- ``unsd_tus:<M49>:<year>:<sha256(text)[:10]>``,
label = the provider's source text verbatim (it is how UNSD names the survey) -- with
``unsd_tus:survey_in`` -> the country's Wikidata QID (reviewed ``registry/entities/country_qids.json``),
``unsd_tus:reference_year`` and ``unsd_tus:data_source_for`` -> ``atlas:un_sdg.sdg_timeuse``. Two different source
texts for one geography-year give two entities (never merged). Proportions of a day are what surveys measured; they
say nothing about why time is spent so.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from reva.adapters.connectors import atlas_mind_common as mc
from reva.adapters.connectors.atlas_fiscal_common import strict_decimal
from reva.adapters.connectors.atlas_society_common import (
    RowBuffer,
    canon,
    concept_hints,
    err,
    fetch_archive,
    year_period,
)
from reva.adapters.connectors.base import module_digest
from reva.atlas.sources import AtlasSourceSpec
from reva.ports.connectors import FetchItem

CONNECTOR_NAME = "atlas.un_sdg_timeuse"
CONNECTOR_VERSION = "1.0.1"  # 1.0.0 was registered in the dev DB from an in-progress copy of this module
PARSER_VERSION = "atlas_mind_timeuse/1"
SOURCE = "un_sdg"
DATASET = "sdg_timeuse"
SOURCE_KEY = "un.sdg"
HOST = "unstats.un.org"
API = "https://unstats.un.org/sdgs/UNSDGAPIV5/v1/sdg/"
PAGE_SIZE = 10000
MAX_BYTES = 30_000_000
CATALOG_FILE = "mind.yaml"
KG_SOURCE_KEY = "unsd.sdg_timeuse"
SERIES: dict[str, str] = {
    "SL_DOM_TSPD": "Proportion of time spent on unpaid domestic chores and care work, by sex, age and location (%)",
    "SL_DOM_TSPDCW": "Proportion of time spent on unpaid care work, by sex, age and location (%)",
    "SL_DOM_TSPDDC": "Proportion of time spent on unpaid domestic chores, by sex, age and location (%)",
}
#: M49 codes of the recorded offline fixtures (Albania, France, Mexico).
FIXTURE_AREAS = ("8", "250", "484")
RIGHTS: dict[str, Any] = {
    "licence": "UNdata terms of use (free use with attribution)",
    "licence_url": "https://data.un.org/Host.aspx?Content=UNdataUse",
    "attribution": "United Nations Statistics Division, SDG Global Database (unstats.un.org/sdgs/dataportal)",
    "source_key": SOURCE_KEY,
    "agent": "V4MIND",
}


def data_url(series: str, areas: tuple[str, ...] | None = None) -> str:
    url = f"{API}Series/Data?seriesCode={series}&pageSize={PAGE_SIZE}&page=1"
    return url + "".join(f"&areaCode={a}" for a in areas or ())


def items(areas: tuple[str, ...] | None = None) -> tuple[FetchItem, ...]:
    return tuple(FetchItem(key=f"sdg:{s}", url=data_url(s, areas), max_bytes=MAX_BYTES) for s in SERIES)


def _doc(body: bytes, series: str) -> dict[str, Any]:
    if body.lstrip()[:1] == b"<":
        raise err(f"SDG {series}: an HTML/XML page was served instead of JSON")
    try:
        doc = json.loads(body)
    except ValueError as e:
        raise err(f"SDG {series}: malformed JSON ({e})") from e
    if not isinstance(doc, dict) or not isinstance(doc.get("data"), list):
        raise err(f"SDG {series}: response is not an observation page")
    return doc


def parse(bodies: dict[str, bytes], shas: dict[str, str]) -> tuple[Any, dict[str, Any], list[dict[str, Any]]]:
    """All series -> one atlas frame, a report, and the survey-wave records (for the KG)."""
    from reva.adapters.connectors.atlas_un import m49_code

    buf = RowBuffer(SOURCE, DATASET)
    hints = concept_hints(CATALOG_FILE, SOURCE, DATASET)
    rep: dict[str, Any] = {"series": {}, "left_out": []}
    stats: Counter[str] = Counter()
    waves: dict[str, dict[str, Any]] = {}
    for code, desc in SERIES.items():
        key = f"sdg:{code}"
        if key not in bodies:
            rep["left_out"].append({"series": code, "reason": "not fetched"})
            continue
        doc = _doc(bodies[key], code)
        total, pages = doc.get("totalElements"), doc.get("totalPages")
        if pages != 1 or total != len(doc["data"]):
            rep["left_out"].append(
                {"series": code, "reason": f"{total} records in {pages} pages, {len(doc['data'])} read"}
            )
            continue
        start = len(buf)
        seen: set[tuple[str, str, int]] = set()
        for i, r in enumerate(doc["data"]):
            if r.get("series") != code:
                raise err(f"SDG {code} record {i}: series {r.get('series')!r}")
            if r.get("seriesDescription") != desc:
                raise err(f"SDG {code}: description changed to {r.get('seriesDescription')!r}")
            dims = r.get("dimensions") or {}
            geo = m49_code(r.get("geoAreaCode"))
            t = r.get("timePeriodStart")
            if not isinstance(t, (int, float)) or t != int(t) or not 1900 <= t <= 2100:
                raise err(f"SDG {code} record {i}: timePeriodStart {t!r} is not a calendar year")
            year = int(t)
            indicator = code + "".join(f"|{k}={dims[k]}" for k in sorted(dims))
            if (indicator, geo, year) in seen:
                raise err(f"SDG {code}: duplicate cell {indicator} {geo} {year}")
            seen.add((indicator, geo, year))
            attrs = r.get("attributes") or {}
            if attrs.get("Units") != "PERCENT":
                raise err(f"SDG {code} record {i}: unit {attrs.get('Units')!r}")
            raw = r.get("value")
            value = None if raw in (None, "", "NaN") else strict_decimal(str(raw))
            src = str(r.get("source") or "").strip()
            flags: dict[str, Any] = {
                "series_description": desc,
                "sdg_indicator": "5.4.1",
                "geo_label": r.get("geoAreaName"),
                "nature": attrs.get("Nature"),
                "provider_source": src[:600] or None,
                "footnotes": [str(x)[:500] for x in (r.get("footnotes") or []) if x][:5] or None,
                "time_detail": r.get("time_detail"),
            }
            flags = {k: v for k, v in flags.items() if v is not None}
            if value is None:
                flags["missing"] = "not_reported"
                stats["missing_values"] += 1
            buf.add(
                indicator=indicator,
                provider_geo=geo,
                period=year_period(year),
                value=value,
                unit="% of a day (24 hours)",
                flags=canon(flags),
                raw_sha256=shas[key],
                selector=canon(item=key, record=i, series=code),
                concept_id=hints.get(indicator),
            )
            if src:
                wk = f"unsd_tus:{geo}:{year}:{mc.short_hash(src)}"
                w = waves.setdefault(
                    wk, {"qid": wk, "geo": geo, "geo_label": r.get("geoAreaName"), "year": year, "source": src,
                         "series": set(), "raw_sha256": shas[key], "record": i, "item": key},
                )  # fmt: skip
                w["series"].add(code)
        rep["series"][code] = {"records": len(doc["data"]), "cells": len(buf) - start}
    if len(buf) == 0:
        raise err("no complete SDG time-use series")
    rep.update(dict(stats))
    rep["survey_waves"] = len(waves)
    return buf.frame(), rep, list(waves.values())


def kg_records(waves: list[dict[str, Any]]) -> tuple[list[Any], list[Any], dict[str, Any]]:
    from reva.atlas.tables import country_iso3
    from reva.kg.model import KgEntity, KgStatement

    qids = mc.country_qids()
    table = mc.atlas_table_entity(SOURCE, DATASET, "UN SDG 5.4.1 time spent on unpaid domestic chores and care work")
    ents: list[Any] = [table]
    sts: list[Any] = []
    unmapped: list[str] = []
    for w in waves:
        ref = {"raw_sha256": w["raw_sha256"], "selector": {"item": w["item"], "record": w["record"]}}
        ents.append(
            KgEntity(
                qid=w["qid"],
                kind="survey_wave",
                labels={"en": w["source"][:480]},
                descriptions={"en": f"time-use survey round, {w['geo_label']} {w['year']} (as named by UNSD)"},
            )
        )
        common: dict[str, Any] = {"source_key": KG_SOURCE_KEY, "references": (ref,), "raw_sha256": w["raw_sha256"]}
        sts.append(KgStatement(subject_qid=w["qid"], property_pid="unsd_tus:source_text",
                               value={"type": "string", "value": w["source"][:2000]}, **common))  # fmt: skip
        sts.append(KgStatement(subject_qid=w["qid"], property_pid="unsd_tus:reference_year",
                               value={"type": "time", "time": mc.year_time(w["year"]).iso, "precision": 9},
                               **common))  # fmt: skip
        sts.append(KgStatement(subject_qid=w["qid"], property_pid="unsd_tus:data_source_for", object_qid=table.qid,
                               qualifiers={"series": sorted(w["series"])}, **common))  # fmt: skip
        iso3 = country_iso3(w["geo"], "m49")
        qid = qids.get(iso3) if iso3 else None
        if qid is None:
            unmapped.append(f"{w['geo']} {w['geo_label']}")
            continue
        sts.append(KgStatement(subject_qid=w["qid"], property_pid="unsd_tus:survey_in", object_qid=qid,
                               qualifiers={"m49": w["geo"], "iso3": iso3}, point_in_time=mc.year_time(w["year"]),
                               **common))  # fmt: skip
    return ents, sts, {"entities": len(ents), "statements": len(sts), "geo_without_qid": sorted(set(unmapped))}


def atlas_source() -> AtlasSourceSpec:
    import sys

    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=SOURCE,
        provider="UN Statistics Division (SDG Global Database, indicator 5.4.1)",
        licence=RIGHTS["licence"],
        allowed_hosts=(HOST,),
        datasets=(DATASET,),
        dataset_ids=tuple(SERIES),
        cadence_seconds=180 * 86400,
        planned_requests=len(SERIES),
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=module_digest(sys.modules[__name__].parse),
        field_mappings={"series": SERIES, "geo_scheme": "m49", "kg_source_key": KG_SOURCE_KEY},
        notes="One page (pageSize 10000) per series, ~3 MB each; survey-wave KG entities from the source texts.",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    if not run.wants(DATASET):
        return {"skipped": True}
    live = run.origin == "live"
    got = await fetch_archive(run, items(None if live else FIXTURE_AREAS))
    frame, rep, waves = parse(got.bodies, got.shas)
    note = (
        "UN SDG Global Database API v5, indicator 5.4.1 series "
        + ", ".join(f"{k} '{v}'" for k, v in SERIES.items())
        + "; every dimension folded into the indicator; period = survey reference year; provider source text "
        "(national time-use survey) in flags.provider_source. Left out: "
        + (json.dumps(rep["left_out"]) if rep["left_out"] else "none")
        + "."
        + ("" if live else f" Fixture subset M49 {','.join(FIXTURE_AREAS)}.")
    )
    reg = run.register(
        dataset=DATASET, frame=frame, raw_sha256s=sorted(set(got.shas.values())), coverage_note=note,
        geo_scheme="m49", rights=RIGHTS,
    )  # fmt: skip
    out: dict[str, Any] = {"parse": rep, "atlas": reg}
    if mc.kg_available(run.engine):
        ents, sts, krep = kg_records(waves)
        retrieved = max((datetime.fromisoformat(m["retrieved_at"]) for m in got.meta.values() if m.get("retrieved_at")),
                        default=datetime.now(UTC))  # fmt: skip
        out["kg"] = {**krep, **mc.load(run.engine, run.store, entities=ents, statements=sts, source_key=KG_SOURCE_KEY,
                                       raw_sha256s=sorted(set(got.shas.values())), retrieved_at=retrieved,
                                       origin=run.origin)}  # fmt: skip
    return out


__all__ = ["CONNECTOR_NAME", "DATASET", "SERIES", "atlas_ingest", "atlas_source", "items", "kg_records", "parse"]
