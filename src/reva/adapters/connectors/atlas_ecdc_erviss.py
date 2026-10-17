"""ECDC European Respiratory Virus Surveillance Summary (ERVISS): weekly ILI/ARI consultation rates and sentinel
influenza positivity by EU/EEA country -> atlas tables ``ecdc.erviss_ili`` and ``ecdc.erviss_sentinel`` (V6, agent
PRED-HEALTH).

Source: the ERVISS weekly data repository ``EU-ECDC/Respiratory_viruses_weekly_data`` (GitHub, raw files):
``data/ILIARIRates.csv`` (columns ``survtype,countryname,yearweek,indicator,age,value``) and
``data/sentinelTestsDetectionsPositivity.csv`` (``survtype,countryname,yearweek,pathogen,pathogentype,
pathogensubtype,indicator,age,value``), updated weekly (2022-W25 onwards). Kept: all-ages (``age = total``)
``ILIconsultationrate`` / ``ARIconsultationrate`` (primary care syndromic) and the influenza totals
(``pathogen = pathogentype = Influenza``, ``pathogensubtype = total``) ``tests`` / ``detections`` /
``positivity`` (primary care sentinel). Values verbatim.

Licence: the repository carries no LICENSE/README file reachable from this environment (raw README 404; github.com
HTML and ecdc.europa.eu answer 403 to non-browser clients), so the reuse terms could not be archived. ECDC's general
copyright notice (reproduction authorised with acknowledgement) is **not verified here**: every cell is flagged
``licence_review`` and the owner decides before any redistribution (docs/dev/v6_pred_health.md).

Geography: ``provider_geo`` = ``countryname`` verbatim (e.g. ``Czechia``). ERVISS publishes no country code, and REVA
never maps a name to a code by similarity, so ``iso3`` stays empty (no geo scheme).
Period: ``yearweek`` ``YYYY-Www`` = the ISO week ``[Monday, +7 days)``. The consultation-rate unit is not stated
in the file (ERVISS documents rates per 100 000 population; not archived), so the unit says so.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from reva.adapters.connectors.atlas_healthedu_common import canon, fetch_archive_register, fit_flags
from reva.adapters.connectors.base import dec_str, module_digest
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import FetchItem

CONNECTOR_NAME = "atlas.ecdc_erviss"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas-ecdc-erviss-parse-1"
SOURCE_KEY = "ecdc.erviss"
HOST = "raw.githubusercontent.com"
BASE = f"https://{HOST}/EU-ECDC/Respiratory_viruses_weekly_data/main/data"
ILI_URL = f"{BASE}/ILIARIRates.csv"
SENTINEL_URL = f"{BASE}/sentinelTestsDetectionsPositivity.csv"
MAX_ITEM_BYTES = 30_000_000
RIGHTS: dict[str, str] = {
    "licence": "licence_review: ECDC reuse terms not archived (repository has no reachable LICENSE)",
    "attribution": "European Centre for Disease Prevention and Control (ECDC), European Respiratory Virus "
    "Surveillance Summary (ERVISS), EU-ECDC/Respiratory_viruses_weekly_data",
    "licence_review": "owner decision: verify ECDC copyright/reuse notice before redistribution",
    "source_key": SOURCE_KEY,
}
_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")
ILI_INDICATORS = ("ILIconsultationrate", "ARIconsultationrate")
SENTINEL_INDICATORS = {"tests": "specimens tested", "detections": "influenza detections", "positivity": "% positive"}
RATE_UNIT = "consultation rate as published by ERVISS (unit not stated in the file)"


class ErvissParseError(SourceResponseError):
    pass


def _err(msg: str) -> ErvissParseError:
    return ErvissParseError(msg, retryable=False, kind="schema_drift")


def iso_week_start(yearweek: str) -> date:
    m = _WEEK_RE.match(yearweek)
    if not m:
        raise _err(f"yearweek {yearweek!r} is not YYYY-Www")
    try:
        return date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
    except ValueError as e:
        raise _err(f"yearweek {yearweek!r} is not an ISO week") from e


def _rows(body: bytes, need: Sequence[str]) -> list[dict[str, str]]:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise _err("ERVISS CSV is not UTF-8") from e
    rd = csv.DictReader(io.StringIO(text))
    if list(rd.fieldnames or ()) != list(need):
        raise _err(f"ERVISS CSV columns {rd.fieldnames} != {list(need)}")
    return list(rd)


def _value(v: str, line: int) -> str | None:
    s = (v or "").strip()
    if s in ("", "NA"):
        return None
    try:
        d = Decimal(s)
    except InvalidOperation as e:
        raise _err(f"line {line}: value {s!r} is not a number") from e
    if not d.is_finite() or d < 0:
        raise _err(f"line {line}: value {s!r}")
    return dec_str(d)


def parse_ili(body: bytes) -> list[dict[str, Any]]:
    out = []
    seen: set[tuple[str, str, str, str]] = set()
    for i, r in enumerate(_rows(body, ("survtype", "countryname", "yearweek", "indicator", "age", "value")), start=2):
        if r["age"] != "total" or r["indicator"] not in ILI_INDICATORS:
            continue
        k = (r["survtype"], r["countryname"], r["yearweek"], r["indicator"])
        if k in seen:
            raise _err(f"line {i}: duplicate {k}")
        seen.add(k)
        out.append({**r, "start": iso_week_start(r["yearweek"]), "num": _value(r["value"], i), "line": i})
    if not out:
        raise _err("no all-ages ILI/ARI rows")
    return out


def parse_sentinel(body: bytes) -> list[dict[str, Any]]:
    cols = (
        "survtype",
        "countryname",
        "yearweek",
        "pathogen",
        "pathogentype",
        "pathogensubtype",
        "indicator",
        "age",
        "value",
    )
    out = []
    seen: set[tuple[str, str, str, str]] = set()
    for i, r in enumerate(_rows(body, cols), start=2):
        if not (
            r["pathogen"] == "Influenza"
            and r["pathogentype"] == "Influenza"
            and r["pathogensubtype"] == "total"
            and r["age"] == "total"
            and r["indicator"] in SENTINEL_INDICATORS
        ):
            continue
        k = (r["survtype"], r["countryname"], r["yearweek"], r["indicator"])
        if k in seen:
            raise _err(f"line {i}: duplicate {k}")
        seen.add(k)
        out.append({**r, "start": iso_week_start(r["yearweek"]), "num": _value(r["value"], i), "line": i})
    if not out:
        raise _err("no influenza total rows")
    return out


def _cell(dataset: str, indicator: str, r: Mapping[str, Any], unit: str, raw: str, item: str) -> dict[str, Any]:
    import json

    from reva.atlas.tables import missing_flags

    flags: dict[str, Any] = {
        "producer": "ECDC ERVISS",
        "survtype": r["survtype"],
        "yearweek": r["yearweek"],
        "licence_review": True,
    }
    if r["num"] is None:
        flags = json.loads(missing_flags("not_reported", **flags))
    return {
        "source": "ecdc",
        "dataset": dataset,
        "indicator": indicator,
        "concept_id": None,
        "iso3": None,
        "provider_geo": r["countryname"],
        "entity_qid": None,
        "period_start": r["start"],
        "period_end": r["start"] + timedelta(days=7),
        "value": r["num"],
        "value_text": None,
        "unit": unit,
        "flags": fit_flags(flags),
        "raw_sha256": raw,
        "selector": canon({"item": item, "line": r["line"]}),
    }


class _Erviss:
    ATLAS_SOURCE = "ecdc"
    GEO_SCHEME = None
    RIGHTS = RIGHTS

    def follow_ups(self, item: FetchItem, body: bytes) -> tuple[FetchItem, ...]:
        return ()


class IliDataset(_Erviss):
    def items(self) -> tuple[FetchItem, ...]:
        return (FetchItem(key="erviss.ili", url=ILI_URL, max_bytes=MAX_ITEM_BYTES),)

    def check(self, item: FetchItem, body: bytes) -> dict[str, Any]:
        return {"rows": len(parse_ili(body))}

    def atlas_frame(self, bodies: Mapping[str, bytes], raws: Mapping[str, str]) -> tuple[Any, str, dict[str, Any]]:
        import polars as pl

        from reva.atlas.tables import ATLAS_SCHEMA

        rows = parse_ili(bodies["erviss.ili"])
        cells = [
            _cell(
                "erviss_ili",
                f"{r['indicator']}|survtype={r['survtype']}|age=total",
                r,
                RATE_UNIT,
                raws["erviss.ili"],
                "erviss.ili",
            )
            for r in rows
        ]
        last = max(r["start"] for r in rows)
        note = f"ERVISS ILIARIRates.csv, all ages: ILI/ARI consultation rates by country, ISO weeks to {last} (week start)."
        return pl.DataFrame(cells, schema=ATLAS_SCHEMA), note, {"last_week_start": last.isoformat()}


class SentinelDataset(_Erviss):
    def items(self) -> tuple[FetchItem, ...]:
        return (FetchItem(key="erviss.sentinel", url=SENTINEL_URL, max_bytes=MAX_ITEM_BYTES),)

    def check(self, item: FetchItem, body: bytes) -> dict[str, Any]:
        return {"rows": len(parse_sentinel(body))}

    def atlas_frame(self, bodies: Mapping[str, bytes], raws: Mapping[str, str]) -> tuple[Any, str, dict[str, Any]]:
        import polars as pl

        from reva.atlas.tables import ATLAS_SCHEMA

        rows = parse_sentinel(bodies["erviss.sentinel"])
        raw = raws["erviss.sentinel"]
        cells = [
            _cell(
                "erviss_sentinel",
                f"influenza.{r['indicator']}|survtype={r['survtype']}|age=total",
                r,
                SENTINEL_INDICATORS[r["indicator"]],
                raw,
                "erviss.sentinel",
            )
            for r in rows
        ]
        last = max(r["start"] for r in rows)
        note = f"ERVISS sentinelTestsDetectionsPositivity.csv: influenza tests, detections, positivity (all ages), to {last}."
        return pl.DataFrame(cells, schema=ATLAS_SCHEMA), note, {"last_week_start": last.isoformat()}


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source="ecdc",
        provider="European Centre for Disease Prevention and Control (ERVISS weekly data, GitHub raw files)",
        licence=RIGHTS["licence"],
        allowed_hosts=(HOST,),
        datasets=("erviss_ili", "erviss_sentinel"),
        dataset_ids=("data/ILIARIRates.csv", "data/sentinelTestsDetectionsPositivity.csv"),
        cadence_seconds=7 * 86400,
        planned_requests=2,
        max_response_bytes=MAX_ITEM_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=module_digest(_Erviss),
        field_mappings={"ili_indicators": list(ILI_INDICATORS), "sentinel_indicators": sorted(SENTINEL_INDICATORS)},
        notes="Two bulk CSV files (~2.5 MB + ~4.1 MB); each dataset registered only if its file parses.",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """ATLASCORE entry point (``uv run reva atlas ingest atlas.ecdc_erviss``)."""
    out: dict[str, Any] = {}
    for ds, obj in (("erviss_ili", IliDataset()), ("erviss_sentinel", SentinelDataset())):
        if run.wants(ds):
            out[ds] = await fetch_archive_register(run, obj, dataset=ds)
    return out


__all__ = ["CONNECTOR_NAME", "RIGHTS", "atlas_ingest", "atlas_source", "iso_week_start", "parse_ili", "parse_sentinel"]
