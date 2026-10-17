"""ECB Survey of Professional Forecasters -> vintage atlas table (V5, agent PRED-VINTAGE).

Source ``atlas.vintage_ecb_spf`` -> table ``vintage_ea.ecb_spf``: the ECB Data Portal SDMX API
(``data-api.ecb.europa.eu``, open, no key), dataflow ``ECB:SPF`` (structure ``ECB_FCT1``), euro area
(``U2``) **average of point forecasts** (``FCT_BREAKDOWN=POINT``, ``FCT_SOURCE=AVG``) for HICP
inflation, real GDP growth and the unemployment rate, quarterly survey rounds since 1999-Q1 (one CSV
request, ``format=csvdata``). Conventions in :mod:`.vintage_common`.

* ``vintage`` = the survey round (``TIME_PERIOD``, e.g. ``2008-Q1``). The publication day is not in the
  data: ``available_at`` = first instant of the quarter after the round (rule ``ecb_spf_round_quarter_end``).
* Reference period: calendar-year horizons (``FCT_HORIZON`` = ``2015``) -> that year; the longer-term
  horizon ``LT`` -> the target year the series title states verbatim ("5 calendar years ahead in SPF Q3 and
  Q4 rounds and 4 calendar years ahead in SPF Q1 and Q2 rounds"), checked against ``TITLE_COMPL``.
  Rolling horizons (``P12M``, ``P24M``, ``P9M``, ``P21M``, ``M3M``…) are relative to the latest
  data release of each round, whose date is not in the data: they are **not** loaded (counted).
* All cells are ``projection`` (survey expectations).
"""

from __future__ import annotations

import csv
import hashlib
import inspect
import io
import re
import sys
from datetime import datetime
from typing import Any

from reva.adapters.connectors.vintage_common import (
    Rows,
    coverage_line,
    decimal_or_none,
    first_of_next_month,
    indicator,
    vintage_flags,
    year_bounds,
)
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

NAME = "atlas.vintage_ecb_spf"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_vintage_ecb_spf/1"
SOURCE_KEY = "ecb.data_api"
ATLAS_SOURCE = "vintage_ea"
DS = "ecb_spf"
HOST = "data-api.ecb.europa.eu"
URL = f"https://{HOST}/service/data/SPF/Q.U2.HICP+RGDP+UNEM.POINT..Q.AVG?format=csvdata"
TOPICS = {"HICP": "hicp_inflation", "RGDP": "real_gdp_growth", "UNEM": "unemployment_rate"}
LT_RULE = "5 calendar years ahead in SPF Q3 and Q4 rounds and 4 calendar years ahead in SPF Q1 and Q2 rounds"
MAX_BYTES = 10_000_000
_ROUND = re.compile(r"^(\d{4})-Q([1-4])$")


class EcbSpfError(SourceResponseError):
    def __init__(self, msg: str) -> None:
        super().__init__(msg, retryable=False, kind="parse_error")


def code_digest() -> str:
    return "sha256:" + hashlib.sha256(inspect.getsource(sys.modules[__name__]).encode()).hexdigest()


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="European Central Bank, Survey of Professional Forecasters (ECB Data Portal)",
        licence="ECB statistics reuse: free of charge with source acknowledgement "
        "(https://www.ecb.europa.eu/stats/ecb_statistics/governance_and_quality_framework/html/usage_policy.en.html)",
        allowed_hosts=(HOST,),
        datasets=(DS,),
        dataset_ids=("ECB:SPF(1.0)",),
        cadence_seconds=30 * 86_400,
        planned_requests=2,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"atlas_tables": [f"{ATLAS_SOURCE}.{DS}"], "geo": "U2 (euro area, changing composition)"},
        notes="one csvdata request; calendar-year and longer-term horizons only",
    )


def rights() -> dict[str, Any]:
    return {
        "licence": "ECB statistics: reuse free of charge provided the source is acknowledged",
        "licence_url": "https://www.ecb.europa.eu/stats/ecb_statistics/governance_and_quality_framework/html/usage_policy.en.html",
        "attribution": "Source: European Central Bank, Survey of Professional Forecasters (ECB Data Portal)",
        "commercial_use": "permitted",
        "open_licence": None,
        "licence_checked": "2026-10-17",
    }


def round_available_at(year: int, quarter: int) -> datetime:
    return first_of_next_month(year, 3 * quarter)


def parse_csv(body: bytes, raw_sha: str, rows: Rows, stats: dict[str, int]) -> None:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise EcbSpfError(f"ECB SPF CSV is not UTF-8 ({e})") from e
    reader = csv.DictReader(io.StringIO(text))
    need = {
        "KEY",
        "REF_AREA",
        "FCT_TOPIC",
        "FCT_BREAKDOWN",
        "FCT_HORIZON",
        "FCT_SOURCE",
        "TIME_PERIOD",
        "OBS_VALUE",
        "TITLE_COMPL",
        "UNIT",
    }
    if not need <= set(reader.fieldnames or ()):
        raise EcbSpfError(f"ECB SPF CSV lacks columns {sorted(need - set(reader.fieldnames or ()))}")
    for i, r in enumerate(reader, start=2):
        if r["REF_AREA"] != "U2" or r["FCT_BREAKDOWN"] != "POINT" or r["FCT_SOURCE"] != "AVG":
            raise EcbSpfError(f"line {i}: unrequested series {r['KEY']}")
        topic = r["FCT_TOPIC"]
        if topic not in TOPICS:
            raise EcbSpfError(f"line {i}: unrequested topic {topic}")
        m = _ROUND.match(r["TIME_PERIOD"])
        if not m:
            raise EcbSpfError(f"line {i}: survey round {r['TIME_PERIOD']!r} is not YYYY-Qn")
        ry, rq = int(m.group(1)), int(m.group(2))
        h = r["FCT_HORIZON"]
        if re.fullmatch(r"\d{4}", h):
            ty = int(h)
        elif h == "LT":
            if LT_RULE not in r["TITLE_COMPL"]:
                raise EcbSpfError(f"line {i}: longer-term horizon title changed: {r['TITLE_COMPL'][:200]!r}")
            ty = ry + (5 if rq >= 3 else 4)
        else:
            stats["rolling_horizon_not_loaded"] = stats.get("rolling_horizon_not_loaded", 0) + 1
            continue
        val = decimal_or_none(r["OBS_VALUE"])
        if val is None:
            stats["no_value"] = stats.get("no_value", 0) + 1
            continue
        p0, p1 = year_bounds(ty)
        rows.add(
            source=ATLAS_SOURCE,
            dataset=DS,
            indicator=indicator(f"{topic}.{h}", r["TIME_PERIOD"]),
            provider_geo="U2",
            start=p0,
            end=p1,
            value=val,
            unit=r["UNIT"] or "not stated",
            flags=vintage_flags(
                family="ecb_spf",
                vintage=r["TIME_PERIOD"],
                available_at=round_available_at(ry, rq),
                basis="ecb_spf_round_quarter_end",
                variable=TOPICS[topic],
                provider_variable=topic,
                estimate_kind="projection",
                horizon=h,
                series_key=r["KEY"],
                statistic="average of point forecasts",
                obs_status=r.get("OBS_STATUS") or None,
                title=r["TITLE_COMPL"][:300],
            ),
            raw_sha256=raw_sha,
            selector={"line": i, "key": r["KEY"], "time_period": r["TIME_PERIOD"]},
        )
        stats["cells"] = stats.get("cells", 0) + 1


async def atlas_ingest(run: Any) -> dict[str, Any]:
    raw = await run.get(
        URL, key="spf:csvdata", media_types=("text/csv", "application/vnd.sdmx.data+csv"), max_bytes=MAX_BYTES
    )
    sha = run.archive(raw)
    rows, stats = Rows(), dict[str, int]()
    parse_csv(raw.body, sha, rows, stats)
    frame = rows.frame()
    note = (
        "ECB SPF euro-area average point forecasts (HICP, real GDP growth, unemployment), calendar-year and "
        f"longer-term ({LT_RULE}) horizons; available_at = first instant of the quarter after the round. "
        f"Coverage: {coverage_line(frame)}. Parse: {stats}."
    )[:4000]
    reg = run.register(dataset=DS, frame=frame, raw_sha256s=[sha], coverage_note=note, rights=rights())
    return {
        "tables": {
            DS: {
                "dataset_key": reg["dataset_key"],
                "rows": reg["row_count"],
                "created": reg["created"],
                "coverage": coverage_line(frame),
                "stats": stats,
            }
        },
        "requests": 1,
    }


__all__ = ["NAME", "atlas_ingest", "atlas_source", "parse_csv"]
