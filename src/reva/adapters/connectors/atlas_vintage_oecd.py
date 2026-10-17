"""OECD real-time database (MEI original release data and revisions) -> vintage atlas table
(V5, agent PRED-VINTAGE).

Source ``atlas.vintage_oecd_rt`` -> table ``vintage_oecd.stes_revisions``: OECD Data Explorer SDMX
(``sdmx.oecd.org``, open, no key), dataflow ``OECD.SDD.STES:DSD_STES_REVISIONS@DF_STES_REVISIONS(4.0)``
"Short-term economic statistics revisions" — OECD: "Access monthly data snapshots of published short term
economic statistics and observe the revisions made to key economic variables." Conventions in
:mod:`.vintage_common`.

Scope (disk budget): the G7 (USA, JPN, DEU, FRA, GBR, ITA, CAN); GDP volume (``B1GQ_Q``, quarterly), consumer
prices (``CP``, monthly index) and unemployment (``UNEMP``, monthly rate); the **quarterly** editions
(February, May, August, November) from 1999 on; for each edition year ``Y`` the request asks
``startPeriod = Y-3``, so every edition carries its last ~3-4 years of history (enough for first releases
and early revisions; the deep history of each edition is not loaded). One CSV request per edition year.

* ``vintage`` = ``EDITION`` (``200802``); ``available_at`` = first instant of the month after the edition
  month (rule ``oecd_mei_edition_month_end``).
* ``indicator`` = ``<MEASURE>.<FREQ>.<UNIT_MEASURE>.<ACTIVITY>|v=<EDITION>``; value = ``OBS_VALUE`` verbatim
  (``UNIT_MULT`` kept in ``unit``; never rescaled); ``OBS_STATUS`` in flags. ``published_history``.
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
    month_bounds,
    quarter_bounds,
    vintage_flags,
)
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

NAME = "atlas.vintage_oecd_rt"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_vintage_oecd/1"
SOURCE_KEY = "oecd.sdmx"
ATLAS_SOURCE = "vintage_oecd"
DS = "stes_revisions"
HOST = "sdmx.oecd.org"
FLOW = "OECD.SDD.STES,DSD_STES_REVISIONS@DF_STES_REVISIONS,4.0"
AREAS = ("USA", "JPN", "DEU", "FRA", "GBR", "ITA", "CAN")
MEASURES = {"B1GQ_Q": "real_gdp_level", "CP": "cpi_level", "UNEMP": "unemployment_rate"}
EDITION_MONTHS = (2, 5, 8, 11)
FIRST_YEAR, LAST_YEAR = 1999, 2026
HISTORY_YEARS = 3
FIXTURE_YEARS = (2008,)
ACCEPT = "application/vnd.sdmx.data+csv; charset=utf-8"
MAX_BYTES = 8_000_000
_Q = re.compile(r"^(\d{4})-Q([1-4])$")
_M = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_ED = re.compile(r"^(\d{4})(0[1-9]|1[0-2])$")


class OecdRtError(SourceResponseError):
    def __init__(self, msg: str) -> None:
        super().__init__(msg, retryable=False, kind="parse_error")


def code_digest() -> str:
    return "sha256:" + hashlib.sha256(inspect.getsource(sys.modules[__name__]).encode()).hexdigest()


def years() -> tuple[int, ...]:
    return tuple(range(FIRST_YEAR, LAST_YEAR + 1))


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="OECD, Short-term economic statistics revisions (MEI original release data and revisions)",
        licence="OECD Terms and Conditions (CC BY 4.0 default since 2024-07-01; dataset-level licence not "
        "separately verified)",
        allowed_hosts=(HOST,),
        datasets=(DS,),
        dataset_ids=("OECD.SDD.STES:DSD_STES_REVISIONS@DF_STES_REVISIONS(4.0)",),
        cadence_seconds=30 * 86_400,
        planned_requests=len(years()) + 2,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=3.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={
            "atlas_tables": [f"{ATLAS_SOURCE}.{DS}"],
            "geo_scheme": "iso3",
            "areas": list(AREAS),
            "measures": sorted(MEASURES),
        },
        notes="one CSV request per edition year (quarterly editions, history window Y-3..)",
    )


def rights() -> dict[str, Any]:
    return {
        "licence": "OECD Terms and Conditions (CC BY 4.0 default since 2024-07-01)",
        "licence_url": "https://www.oecd.org/en/about/terms-conditions.html",
        "attribution": "OECD (2026), Short-term economic statistics revisions (OECD Data Explorer)",
        "open_licence": None,
        "restriction": "dataset-level licence not separately verified",
        "licence_checked": "2026-10-17",
    }


def url(year: int) -> str:
    eds = "+".join(f"{year}{m:02d}" for m in EDITION_MONTHS)
    key = f"{'+'.join(AREAS)}.Q+M.{'+'.join(MEASURES)}...{eds}"
    return f"https://{HOST}/public/rest/data/{FLOW}/{key}?startPeriod={year - HISTORY_YEARS}"


def edition_available_at(edition: str) -> datetime:
    m = _ED.match(edition)
    if not m:
        raise OecdRtError(f"edition {edition!r} is not YYYYMM")
    return first_of_next_month(int(m.group(1)), int(m.group(2)))


def parse_csv(body: bytes, raw_sha: str, rows: Rows, stats: dict[str, int], *, year: int | None = None) -> None:
    text = body.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    need = {
        "REF_AREA",
        "FREQ",
        "MEASURE",
        "UNIT_MEASURE",
        "ACTIVITY",
        "EDITION",
        "TIME_PERIOD",
        "OBS_VALUE",
        "OBS_STATUS",
        "UNIT_MULT",
    }
    if not need <= set(reader.fieldnames or ()):
        raise OecdRtError(f"OECD CSV lacks columns {sorted(need - set(reader.fieldnames or ()))}")
    for i, r in enumerate(reader, start=2):
        meas, area, ed = r["MEASURE"], r["REF_AREA"], r["EDITION"]
        if meas not in MEASURES or area not in AREAS:
            raise OecdRtError(f"line {i}: unrequested series {area}/{meas}")
        if year is not None and not ed.startswith(str(year)):
            raise OecdRtError(f"line {i}: edition {ed} outside the requested year {year}")
        tp = r["TIME_PERIOD"]
        if r["FREQ"] == "Q" and (m := _Q.match(tp)):
            s, e = quarter_bounds(int(m.group(1)), int(m.group(2)))
        elif r["FREQ"] == "M" and (m := _M.match(tp)):
            s, e = month_bounds(int(m.group(1)), int(m.group(2)))
        else:
            raise OecdRtError(f"line {i}: period {tp!r} does not match frequency {r['FREQ']}")
        val = decimal_or_none(r["OBS_VALUE"])
        if val is None:
            stats["no_value"] = stats.get("no_value", 0) + 1
            continue
        pv = f"{meas}.{r['FREQ']}.{r['UNIT_MEASURE']}.{r['ACTIVITY']}"
        rows.add(
            source=ATLAS_SOURCE,
            dataset=DS,
            indicator=indicator(pv, ed),
            provider_geo=area,
            iso3=area,
            start=s,
            end=e,
            value=val,
            unit=f"{r['UNIT_MEASURE']} (UNIT_MULT {r['UNIT_MULT']})",
            flags=vintage_flags(
                family="oecd_mei_rt",
                vintage=ed,
                available_at=edition_available_at(ed),
                basis="oecd_mei_edition_month_end",
                variable=MEASURES[meas],
                provider_variable=pv,
                estimate_kind="published_history",
                obs_status=r["OBS_STATUS"] or None,
                base_per=r.get("BASE_PER") or None,
            ),
            raw_sha256=raw_sha,
            selector={"line": i, "edition": ed, "series": f"{area}.{pv}", "time_period": tp},
        )
        stats["cells"] = stats.get("cells", 0) + 1


async def atlas_ingest(run: Any) -> dict[str, Any]:
    fixture = run.origin == "test_fixture"
    rows, stats, raws = Rows(), dict[str, int](), []
    out: dict[str, Any] = {"tables": {}, "failures": [], "requests": 0}
    for y in FIXTURE_YEARS if fixture else years():
        try:
            raw = await run.get(
                url(y),
                key=f"stes_rev:{y}",
                media_types=("application/vnd.sdmx.data+csv", "text/csv"),
                headers={"Accept": ACCEPT},
                max_bytes=MAX_BYTES,
                params={"edition_year": y},
            )
            out["requests"] += 1
            sha = run.archive(raw)
            raws.append(sha)
            parse_csv(raw.body, sha, rows, stats, year=y)
        except SourceResponseError as e:
            out["failures"].append({"edition_year": y, "error": str(e)[:300]})
    if not len(rows):
        out["status"] = "no_cells"
        return out
    frame = rows.frame()
    note = (
        "OECD STES revisions (MEI original release data and revisions), G7, GDP volume / CPI / unemployment, "
        f"quarterly editions, history window edition year - {HISTORY_YEARS}; available_at = first instant of the "
        f"month after the edition. Coverage: {coverage_line(frame)}. Parse: {stats}. Failures: {out['failures']}."
    )[:4000]
    reg = run.register(
        dataset=DS, frame=frame, raw_sha256s=raws, coverage_note=note, geo_scheme="iso3", rights=rights()
    )
    out["tables"][DS] = {
        "dataset_key": reg["dataset_key"],
        "rows": reg["row_count"],
        "created": reg["created"],
        "coverage": coverage_line(frame),
        "stats": stats,
    }
    return out


__all__ = ["NAME", "atlas_ingest", "atlas_source", "parse_csv", "url"]
