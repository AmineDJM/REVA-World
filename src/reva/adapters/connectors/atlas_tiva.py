"""OECD Trade in Value Added (TiVA, 2025 edition) -> atlas supply-chain tables (V4, agent V4INDUSTRY).

Source
------
OECD TiVA 2025 edition, derived from the OECD Inter-Country Input-Output (ICIO) tables: "a collection of measures
that provide insights into global production networks and supply chains beyond conventional trade statistics. TiVA
tracks the origins of value added in exports, imports and final demand" -- 80 economies (+ regional aggregates),
50 economic activities based on ISIC Rev.4 (+ industry groups), 1995-2022 (dataflow descriptions, OECD SDMX,
retrieved 2026-10-17). Access: OECD SDMX REST, ``https://sdmx.oecd.org/sti-public/rest`` (the TiVA dataflows are
external references of ``sdmx.oecd.org/public``), SDMX-CSV, no key. Values are **model-based estimates** (ICIO):
every cell says so (``flags.estimate = "oecd_icio_model_based"``). Licence: OECD content published from 2024-07-01
defaults to CC BY 4.0; no dataset-level statement was found (``licence_status =
oecd_default_cc_by_4.0_dataset_statement_not_verified``, as for BaTIS).

The full ICIO tables (multi-GB) are not downloaded: only these compact indicators.

Atlas tables (``source = oecd``)
--------------------------------
``tiva_principal`` (dataflow ``DSD_TIVA_MAINLV@DF_MAINLV`` 1.1, "Principal Indicators, levels")
    indicator ``<MEASURE>|activity=<ACTIVITY>`` for the measures of :data:`MEASURES` (production, value added,
    gross exports, domestic / foreign value added in gross exports, domestic value added in foreign final demand,
    foreign value added in domestic final demand), partner ``W`` (world), activities ``_T`` (total) and the 20 ISIC
    Rev.4 sections A..T as TiVA codes them (TiVA has no section U), years ``start_year`` (default 2010) .. last. Unit: million US dollars
    (``UNIT_MULT`` 6, verbatim per row).
``tiva_fdva`` (dataflow ``DSD_TIVA_FDVA@DF_FDVA`` 1.1, "Origin of value added in final demand")
    "estimates of final demand in country c for industry i final goods and services, broken down by the value added
    originating from source industry j in source country s". Geography = the **value-added source** economy s;
    indicator ``FDVA|fd_area=<c>|va_activity=<j>`` with final-demand activity i = ``_T``: (a) j = ``_T`` for every
    year from ``start_year``; (b) j = each ISIC section for the last year only. This is the country x country (x
    industry) value-added supply matrix ("value added from s embodied in what c's residents and governments
    finally consume or invest"); it is an aggregate flow estimated from input-output tables, **not** a supplier
    relation between firms and not a causal statement.

Every cell keeps the provider codes verbatim (aggregates such as ``W``, ``OECD``, ``EU27_2020``, ``WXD`` stay unmapped
geographies), the activity and measure labels from the OECD code lists (archived in the same run), and the ISIC
Rev.4 section letter when the TiVA activity code *is* a single section code (``flags.isic4_section``), so the series
can be read against the ``isic4:<section>`` industries of the KG. An empty ``OBS_VALUE`` is omitted (counted), never 0.
"""

from __future__ import annotations

import csv
import io
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from reva.adapters.connectors.atlas_industry_common import atlas_frame, canon, code_digest, trim_registration
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.oecd_tiva"
# 1.0.1: 1.0.0 was registered in the dev DB from a WIP snapshot of earlier code.
CONNECTOR_VERSION = "1.0.1"
PARSER_VERSION = "atlas-tiva-1"
SOURCE_KEY = "oecd.sdmx"
ATLAS_SOURCE = "oecd"
DS_MAIN = "tiva_principal"
DS_FDVA = "tiva_fdva"
HOST = "sdmx.oecd.org"
BASE = f"https://{HOST}/sti-public/rest"
AGENCY = "OECD.STI.PIE"
FLOW_MAIN = f"{AGENCY},DSD_TIVA_MAINLV@DF_MAINLV,1.1"
FLOW_FDVA = f"{AGENCY},DSD_TIVA_FDVA@DF_FDVA,1.1"
EDITION = "TiVA 2025 edition"
MEASURES: tuple[str, ...] = ("PROD", "VALU", "EXGR", "EXGR_DVA", "EXGR_FVA", "FFD_DVA", "DFD_FVA")
SECTIONS: tuple[str, ...] = tuple("ABCDEFGHIJKLMNOPQRST")
ACTIVITIES: tuple[str, ...] = ("_T", *SECTIONS)
START_YEAR = 2010
MAX_BYTES = 45_000_000
CSV_MEDIA = ("application/vnd.sdmx.data+csv", "text/csv", "application/octet-stream")
XML_MEDIA = ("application/xml", "text/xml", "application/vnd.sdmx.structure+xml")
UNIT = "million US dollars (current)"
RIGHTS: dict[str, str] = {
    "licence": "CC-BY-4.0 (OECD open-by-default policy; dataset-level statement not verified)",
    "licence_url": "https://www.oecd.org/en/about/oecd-open-by-default-policy.html",
    "licence_status": "oecd_default_cc_by_4.0_dataset_statement_not_verified",
    "terms_url": "https://www.oecd.org/en/about/terms-conditions.html",
    "attribution": "OECD (2025), Trade in Value Added (TiVA) database, 2025 edition, sdmx.oecd.org",
}
MAIN_COLS = ("MEASURE", "REF_AREA", "ACTIVITY", "COUNTERPART_AREA", "UNIT_MEASURE", "FREQ", "TIME_PERIOD", "OBS_VALUE")
FDVA_COLS = (
    "MEASURE",
    "VALUE_ADDED_SOURCE_AREA",
    "VALUE_ADDED_SOURCE_ACTIVITY",
    "FINAL_DEMAND_AREA",
    "FINAL_DEMAND_ACTIVITY",
    "UNIT_MEASURE",
    "FREQ",
    "TIME_PERIOD",
    "OBS_VALUE",
)
_DEC = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]{1,3})?$")


class TivaParseError(SourceResponseError):
    pass


def _err(msg: str, kind: str = "schema_drift") -> TivaParseError:
    return TivaParseError(msg, retryable=False, kind=kind)


# ============================================================================================== URLs
def constraint_url(flow: str) -> str:
    return f"{BASE}/availableconstraint/{flow}/all"


def codelist_url(agency: str, cl: str) -> str:
    return f"{BASE}/codelist/{agency}/{cl}"


def main_url(measure: str, *, areas: Sequence[str] = (), start: int, end: int | None = None) -> str:
    key = f"{measure}.{'+'.join(areas)}.{'+'.join(ACTIVITIES)}.W.USD.A"
    tail = f"&endPeriod={end}" if end else ""
    return f"{BASE}/data/{FLOW_MAIN}/{key}?startPeriod={start}{tail}&format=csvfile"


def fdva_url(*, va_activities: Sequence[str], areas: Sequence[str] = (), start: int, end: int | None = None) -> str:
    a = "+".join(areas)
    key = f"FDVA.{a}.{'+'.join(va_activities)}.{a}._T.USD.A"
    tail = f"&endPeriod={end}" if end else ""
    return f"{BASE}/data/{FLOW_FDVA}/{key}?startPeriod={start}{tail}&format=csvfile"


# ============================================================================================== parsing
def parse_time_range(body: bytes) -> tuple[int, int]:
    """(first year, last year) of an SDMX available-constraint answer."""
    s = body.decode("utf-8", "replace")
    m0 = re.search(r"<common:StartPeriod[^>]*>(\d{4})", s)
    m1 = re.search(r"<common:EndPeriod[^>]*>(\d{4})", s)
    if not (m0 and m1):
        raise _err("available constraint: no TIME_PERIOD range")
    return int(m0.group(1)), int(m1.group(1))


def parse_codelist(body: bytes) -> dict[str, str]:
    """SDMX-ML codelist -> {code: English name}."""
    s = body.decode("utf-8", "replace")
    out: dict[str, str] = {}
    for m in re.finditer(r'<structure:Code id="([^"]+)"[^>]*>(.*?)</structure:Code>', s, re.S):
        n = re.search(r'<common:Name xml:lang="en">(.*?)</common:Name>', m.group(2), re.S)
        if n:
            out[m.group(1)] = _unescape(n.group(1).strip())
    if not out:
        raise _err("codelist: no codes")
    return out


def _unescape(t: str) -> str:
    import html

    return html.unescape(t)


def parse_csv(body: bytes, cols: Sequence[str]) -> tuple[list[dict[str, str]], Counter[str]]:
    """SDMX-CSV rows (``format=csvfile``) with the expected dimension columns; empty OBS_VALUE rows dropped."""
    text = body.decode("utf-8-sig", "strict")
    rd = csv.DictReader(io.StringIO(text))
    hdr = rd.fieldnames or []
    missing = [c for c in cols if c not in hdr]
    if missing:
        if text.lstrip().startswith("<"):
            raise _err("answer is XML, not SDMX-CSV (queued or error envelope)", kind="not_csv")
        raise _err(f"SDMX-CSV header lacks {missing} (got {hdr})")
    out: list[dict[str, str]] = []
    skipped: Counter[str] = Counter()
    for r in rd:
        v = (r.get("OBS_VALUE") or "").strip()
        if not v:
            skipped["empty_obs_value"] += 1
            continue
        if not _DEC.match(v):
            raise _err(f"OBS_VALUE not a decimal: {v!r}")
        if r.get("FREQ") != "A" or not re.match(r"^\d{4}$", r.get("TIME_PERIOD") or ""):
            raise _err(f"unexpected FREQ/TIME_PERIOD {r.get('FREQ')}/{r.get('TIME_PERIOD')}")
        out.append({k: (r.get(k) or "").strip() for k in (*cols, "UNIT_MULT", "DATAFLOW")})
    return out, skipped


# ============================================================================================== rows
def _isic_section(code: str) -> str | None:
    return code if len(code) == 1 and code in SECTIONS else None


def _area_flags(code: str, key: str) -> dict[str, Any]:
    from reva.adapters.connectors.atlas_industry_common import _geo

    idx = _geo()
    iso = idx.lookup(code, idx.scheme("iso3"))
    return {f"{key}_iso3": iso} if iso else {f"{key}_unmapped": code}


def main_rows(
    recs: Sequence[Mapping[str, str]], *, raw_sha256: str, request_url: str, measures: Mapping[str, str],
    activities: Mapping[str, str],
) -> list[dict[str, Any]]:  # fmt: skip
    rows = []
    for r in recs:
        if r["COUNTERPART_AREA"] != "W" or r["UNIT_MEASURE"] != "USD":
            raise _err(f"unexpected partner/unit {r['COUNTERPART_AREA']}/{r['UNIT_MEASURE']}")
        y = int(r["TIME_PERIOD"])
        flags: dict[str, Any] = {
            "estimate": "oecd_icio_model_based",
            "edition": EDITION,
            "measure_label": measures.get(r["MEASURE"]),
            "activity_label": activities.get(r["ACTIVITY"]),
            "unit_mult": r.get("UNIT_MULT"),
            "partner": "W",
            "licence_status": RIGHTS["licence_status"],
        }
        sec = _isic_section(r["ACTIVITY"])
        if sec:
            flags["isic4_section"] = sec
        rows.append(
            {
                "source": ATLAS_SOURCE,
                "dataset": DS_MAIN,
                "indicator": f"{r['MEASURE']}|activity={r['ACTIVITY']}",
                "concept_id": f"industry.tiva.{r['MEASURE'].lower()}",
                "iso3": None,
                "provider_geo": r["REF_AREA"],
                "entity_qid": None,
                "period_start": date(y, 1, 1),
                "period_end": date(y + 1, 1, 1),
                "value": r["OBS_VALUE"],
                "value_text": None,
                "unit": UNIT if r.get("UNIT_MULT") == "6" else f"US dollars x 10^{r.get('UNIT_MULT')}",
                "flags": canon(flags),
                "raw_sha256": raw_sha256,
                "selector": canon(
                    {
                        "url": request_url,
                        "key": {k: r[k] for k in ("MEASURE", "REF_AREA", "ACTIVITY", "COUNTERPART_AREA")},
                        "TIME_PERIOD": r["TIME_PERIOD"],
                    }
                ),
            }
        )
    return rows


def fdva_rows(
    recs: Sequence[Mapping[str, str]], *, raw_sha256: str, request_url: str, activities: Mapping[str, str]
) -> list[dict[str, Any]]:
    rows = []
    for r in recs:
        if r["FINAL_DEMAND_ACTIVITY"] != "_T" or r["UNIT_MEASURE"] != "USD" or r["MEASURE"] != "FDVA":
            raise _err(f"unexpected FDVA key {r}")
        y = int(r["TIME_PERIOD"])
        j = r["VALUE_ADDED_SOURCE_ACTIVITY"]
        flags: dict[str, Any] = {
            "estimate": "oecd_icio_model_based",
            "edition": EDITION,
            "reading": "value added originating in the geography (source s, activity j) embodied in final demand of fd_area c",
            "va_activity_label": activities.get(j),
            "final_demand_activity": "_T",
            "unit_mult": r.get("UNIT_MULT"),
            "licence_status": RIGHTS["licence_status"],
            **_area_flags(r["FINAL_DEMAND_AREA"], "fd_area"),
        }
        sec = _isic_section(j)
        if sec:
            flags["isic4_section"] = sec
        rows.append(
            {
                "source": ATLAS_SOURCE,
                "dataset": DS_FDVA,
                "indicator": f"FDVA|fd_area={r['FINAL_DEMAND_AREA']}|va_activity={j}",
                "concept_id": "industry.tiva.fdva_bilateral",
                "iso3": None,
                "provider_geo": r["VALUE_ADDED_SOURCE_AREA"],
                "entity_qid": None,
                "period_start": date(y, 1, 1),
                "period_end": date(y + 1, 1, 1),
                "value": r["OBS_VALUE"],
                "value_text": None,
                "unit": UNIT if r.get("UNIT_MULT") == "6" else f"US dollars x 10^{r.get('UNIT_MULT')}",
                "flags": canon(flags),
                "raw_sha256": raw_sha256,
                "selector": canon(
                    {
                        "url": request_url,
                        "key": {k: r[k] for k in FDVA_COLS[:5]},
                        "TIME_PERIOD": r["TIME_PERIOD"],
                    }
                ),
            }
        )
    return rows


# ============================================================================================== connector
def _opt_areas(run: Any) -> list[str]:
    v = (run.options or {}).get("areas")
    if v is None:
        return []
    items = v if isinstance(v, list) else str(v).split(",")
    return [x.strip().upper() for x in items if x.strip()]


async def _labels(run: Any) -> tuple[dict[str, str], dict[str, str], list[str]]:
    shas = []
    m = await run.get(codelist_url(AGENCY, "CL_TIVA_MEASURE"), key="cl:measure", media_types=XML_MEDIA)
    shas.append(run.archive(m))
    a = await run.get(codelist_url("OECD", "CL_ACTIVITY_ISIC4"), key="cl:activity", media_types=XML_MEDIA)
    shas.append(run.archive(a))
    return parse_codelist(m.body), parse_codelist(a.body), shas


async def atlas_ingest(run: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not (run.wants(DS_MAIN) or run.wants(DS_FDVA)):
        return {"skipped": True}
    areas = _opt_areas(run)
    start = int((run.options or {}).get("start_year", START_YEAR))
    measures, activities, label_shas = await _labels(run)
    cons = await run.get(constraint_url(FLOW_MAIN), key="constraint:main", media_types=XML_MEDIA)
    cons_sha = run.archive(cons)
    first, last = parse_time_range(cons.body)
    end = int((run.options or {}).get("end_year", last))
    out["edition_years"] = [first, last]
    if run.wants(DS_MAIN):
        rows: list[dict[str, Any]] = []
        shas = [*label_shas, cons_sha]
        skipped: Counter[str] = Counter()
        for meas in MEASURES:
            u = main_url(meas, areas=areas, start=start, end=end)
            raw = await run.get(u, key=f"main:{meas}", media_types=CSV_MEDIA)
            sha = run.archive(raw)
            shas.append(sha)
            recs, sk = parse_csv(raw.body, MAIN_COLS)
            skipped.update(sk)
            rows += main_rows(recs, raw_sha256=sha, request_url=u, measures=measures, activities=activities)
        reg = run.register(
            dataset=DS_MAIN,
            frame=atlas_frame(rows),
            raw_sha256s=shas,
            geo_scheme="iso3",
            coverage_note=(
                f"OECD {EDITION} principal indicators (levels, partner World) for {len(MEASURES)} measures x total + "
                f"20 ISIC Rev.4 sections (A..T), {start}-{end}; model-based ICIO estimates, million USD."
            ),
            rights=RIGHTS,
        )
        out[DS_MAIN] = {"rows": len(rows), "skipped": dict(skipped), "atlas": trim_registration(reg)}
    if run.wants(DS_FDVA):
        rows = []
        shas = [*label_shas, cons_sha]
        skipped = Counter()
        for acts, s0 in ((["_T"], start), (list(SECTIONS), end)):
            u = fdva_url(va_activities=acts, areas=areas, start=s0, end=end)
            raw = await run.get(u, key=f"fdva:{acts[0]}:{s0}", media_types=CSV_MEDIA)
            sha = run.archive(raw)
            shas.append(sha)
            recs, sk = parse_csv(raw.body, FDVA_COLS)
            skipped.update(sk)
            rows += fdva_rows(recs, raw_sha256=sha, request_url=u, activities=activities)
        reg = run.register(
            dataset=DS_FDVA,
            frame=atlas_frame(rows),
            raw_sha256s=shas,
            geo_scheme="iso3",
            coverage_note=(
                f"OECD {EDITION} origin of value added in final demand: value added of source economy s (geography) "
                f"embodied in final demand of fd_area c, all source activities {start}-{end} and by ISIC section for "
                f"{end}; model-based ICIO estimates, million USD. Aggregate flows, not firm-level supplier relations."
            ),
            rights=RIGHTS,
        )
        out[DS_FDVA] = {"rows": len(rows), "skipped": dict(skipped), "atlas": trim_registration(reg)}
    return out


def atlas_source() -> Any:
    from reva.atlas.sources import AtlasSourceSpec

    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="OECD - Trade in Value Added (TiVA) 2025 edition (ICIO-based indicators), SDMX REST",
        licence=f"{RIGHTS['licence']} ({RIGHTS['licence_url']}); attribution: {RIGHTS['attribution']}",
        allowed_hosts=(HOST,),
        datasets=(DS_MAIN, DS_FDVA),
        dataset_ids=("DSD_TIVA_MAINLV@DF_MAINLV 1.1", "DSD_TIVA_FDVA@DF_FDVA 1.1"),
        cadence_seconds=90 * 86400,
        planned_requests=3 + len(MEASURES) + 2,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(sys.modules[__name__]),
        field_mappings={
            "measures": list(MEASURES),
            "activities": list(ACTIVITIES),
            "start_year": START_YEAR,
            "flows": [FLOW_MAIN, FLOW_FDVA],
        },
        notes="Compact TiVA indicators only (full ICIO tables not downloaded); values are model-based estimates.",
    )


__all__ = [
    "CONNECTOR_NAME",
    "MEASURES",
    "atlas_ingest",
    "atlas_source",
    "codelist_url",
    "constraint_url",
    "fdva_rows",
    "fdva_url",
    "main_rows",
    "main_url",
    "parse_codelist",
    "parse_csv",
    "parse_time_range",
]
