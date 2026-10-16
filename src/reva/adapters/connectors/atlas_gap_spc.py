"""Pacific Community (SPC) Pacific Data Hub .Stat — gap fill for 22 Pacific islands (V3, GAPHUNT).

Source: the SPC .Stat Suite SDMX REST API ``https://stats-sdmx-disseminate.pacificdata.org/rest``
(no key, GET). Four dataflows, one request each, all 22 Pacific Island Countries and Territories
(PICTs) — American Samoa, Cook Islands, Fiji, French Polynesia, Guam, Kiribati, Marshall Islands,
Micronesia, Nauru, New Caledonia, Niue, Northern Mariana Islands, Palau, Papua New Guinea,
Pitcairn, Samoa, Solomon Islands, Tokelau, Tonga, Tuvalu, Vanuatu, Wallis and Futuna:

``pocket``   ``SPC:DF_POCKET(3.1)`` "Pocket summary (latest available value per year)": mid-year
             population, land area, density, life expectancy (M/F), vital rates, GDP (national
             currency and USD), GDP per capita USD, inflation, merchandise trade, government
             revenue/expenditure % GDP, EEZ area.
``keyfacts`` ``SPC:DF_KEYFACTS(1.0)`` "About PICTs": capital, languages, currency, government
             status (texts, verbatim).
``emprates`` ``SPC:DF_EMPRATES(1.0)`` unemployment rate, national, both sexes, all ages, all
             disability statuses (the headline cell only; other breakdowns stay at SPC).
``digital``  ``SPC:DF_DIGITAL_DEVELOPMENT(1.0)`` indicator DD024 "Individuals using the Internet,
             total (%)" (ITU indicator re-published by SPC).

Honesty rules:

* The pocket summary repeats a territory's **latest known value** in later years: its
  ``DATA_YEAR`` attribute names the year the value is for. Only cells whose ``DATA_YEAR`` equals the
  ``TIME_PERIOD`` are kept; the repeats are omitted and counted (``coverage_note``), never stored as
  observations of a later year. Cells with ``OBS_STATUS = O`` (missing value) are omitted too.
* Values are stored as published with their unit multiplier spelled out in ``unit`` (``thousand
  USD``); never rescaled. ``OBS_STATUS`` (E estimated, P provisional, R revised), ``DATA_SOURCE`` and
  ``OBS_COMMENT`` (e.g. SPC's own currency conversion "Converted 0.7041 NZD/USD") are kept in flags.
* Geography: ``GEO_PICT`` codes are ISO 3166-1 alpha-2 (scheme ``iso2``); every code maps.
* Licence: the Pacific Data Hub catalogue lists the PDH.Stat pocket summary as "Other (Open)"; the
  site terms (pacificdata.org/terms-use) say each dataset carries its own licence. The terms page
  answered 403 to automated reads, so the exact licence text is **pending maintainer review**.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Mapping
from typing import Any

from reva.adapters.connectors import atlas_gap_common as common
from reva.adapters.connectors.atlas_gap_common import Fetched, GapItem, GapTable, canon, strict_decimal
from reva.atlas.sources import AtlasSourceSpec
from reva.atlas.tables import country_iso3
from reva.kernel.errors import SourceResponseError

NAME = "atlas.gap_spc"
SOURCE_KEY = "spc.pdh_stat"
HOST = "stats-sdmx-disseminate.pacificdata.org"
BASE = f"https://{HOST}/rest/data"
ATLAS_SOURCE = "spc"
PARSER_VERSION = "atlas_gap_spc/1"
CSV_ACCEPT = "application/vnd.sdmx.data+csv;version=1.0.0;labels=both"
RIGHTS = {
    "licence": "Pacific Data Hub: 'Other (Open)' per the PDH catalogue entry of the dataflow; per-dataset licence, pending maintainer review",
    "licence_url": "https://pacificdata.org/terms-use",
    "attribution": "Pacific Community (SPC), Pacific Data Hub .Stat (PDH.Stat)",
}
#: dataset -> (dataflow, version, live key, fixture key)
FLOWS: dict[str, tuple[str, str, str, str]] = {
    "pocket": ("DF_POCKET", "3.1", "all", "A.NU+TK+WF."),
    "keyfacts": ("DF_KEYFACTS", "1.0", "all", "A.NU+TK+PN+WF+CK."),
    "emprates": ("DF_EMPRATES", "1.0", "A..UNEMPRATE._T._T._T._T", "A.NU+CK+TO.UNEMPRATE._T._T._T._T"),
    "digital": ("DF_DIGITAL_DEVELOPMENT", "1.0", "A..DD024", "A.NU+TO+WF.DD024"),
}
#: columns whose cells are "CODE: label" (SDMX-CSV 1.0 labels=both); other columns are free text
CODED = frozenset(
    {
        "FREQ",
        "GEO_PICT",
        "INDICATOR",
        "UNIT_MEASURE",
        "UNIT_MULT",
        "OBS_STATUS",
        "SEX",
        "AGE",
        "URBANIZATION",
        "DISABILITY",
        "CONF_STATUS",
    }
)
_CODE = re.compile(r"^([A-Za-z0-9_]+): (.*)$", re.S)
_YEAR = re.compile(r"^[12][0-9]{3}$")
MULT_WORD = {"0": "", "3": "thousand ", "6": "million ", "9": "billion "}
TEXT_INDICATORS = frozenset({"GOV", "LAN", "CUR", "CAP", "SPC"})


def url(dataset: str, *, fixture: bool = False) -> str:
    flow, ver, key, fkey = FLOWS[dataset]
    return f"{BASE}/SPC,{flow},{ver}/{fkey if fixture else key}"


def items(*, fixture: bool = False) -> list[GapItem]:
    return [
        GapItem(
            key=f"data:{ds}",
            url=url(ds, fixture=fixture),
            datasets=(ds,),
            media_types=("application/vnd.sdmx.data+csv", "text/csv"),
            headers={"Accept": CSV_ACCEPT},
            max_bytes=30_000_000,
        )
        for ds in FLOWS
    ]


def read_sdmx_csv(body: bytes, what: str) -> list[dict[str, tuple[str, str]]]:
    """SDMX-CSV 1.0 with ``labels=both`` -> rows of ``{column id: (code or text, label)}``."""
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise SourceResponseError(f"{what}: not UTF-8", retryable=False) from e
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2:
        raise SourceResponseError(f"{what}: no data rows", retryable=False)
    header = [h.split(": ", 1)[0].strip() for h in rows[0]]
    if "DATAFLOW" not in header or "OBS_VALUE" not in header or "TIME_PERIOD" not in header:
        raise SourceResponseError(f"{what}: not an SDMX-CSV 1.0 data message", columns=header[:12], retryable=False)
    out = []
    for n, r in enumerate(rows[1:], start=1):
        if len(r) != len(header):
            raise SourceResponseError(f"{what}: ragged CSV row", row=n, retryable=False)
        rec: dict[str, tuple[str, str]] = {}
        for col, cell in zip(header, r, strict=True):
            if col in CODED and cell:
                m = _CODE.match(cell)
                if not m:
                    raise SourceResponseError(f"{what}: coded cell without code", column=col, row=n, retryable=False)
                rec[col] = (m.group(1), m.group(2))
            else:
                rec[col] = (cell, "")
        out.append(rec)
    return out


def _unit(rec: Mapping[str, tuple[str, str]]) -> tuple[str, str]:
    code, label = rec.get("UNIT_MEASURE", ("", ""))
    mult = rec.get("UNIT_MULT", ("", ""))[0]
    if mult not in ("", *MULT_WORD):
        raise SourceResponseError("SPC: unexpected unit multiplier", mult=mult[:5], retryable=False)
    base = label or code or "(unit not stated)"
    return (MULT_WORD.get(mult, "") + base).strip(), mult


def parse(dataset: str, f: Fetched) -> tuple[list[dict[str, Any]], dict[str, int]]:
    flow = FLOWS[dataset][0]
    recs = read_sdmx_csv(f.body, f"SPC {flow}")
    rows: list[dict[str, Any]] = []
    stats = {"rows_read": len(recs), "carried_forward_omitted": 0, "missing_status_omitted": 0, "empty_omitted": 0}
    for n, rec in enumerate(recs, start=1):
        geo = rec["GEO_PICT"][0]
        ind, ind_label = rec["INDICATOR"]
        tp = rec["TIME_PERIOD"][0]
        if rec.get("FREQ", ("A", ""))[0] != "A" or not _YEAR.match(tp):
            raise SourceResponseError(f"SPC {flow}: non-annual cell", period=tp[:12], retryable=False)
        status = rec.get("OBS_STATUS", ("", ""))
        if status[0] == "O":
            stats["missing_status_omitted"] += 1
            continue
        data_year = rec.get("DATA_YEAR", ("", ""))[0]
        if data_year and data_year != tp:
            stats["carried_forward_omitted"] += 1
            continue
        raw_value = rec["OBS_VALUE"][0]
        value = value_text = None
        if ind in TEXT_INDICATORS:
            value_text = raw_value.strip()[:4000] or None
        else:
            value = strict_decimal(raw_value)
        if value is None and value_text is None:
            stats["empty_omitted"] += 1
            continue
        unit, mult = _unit(rec)
        indicator = ind
        for dim in ("SEX", "AGE", "URBANIZATION", "DISABILITY"):
            if dim in rec:
                indicator += f"|{dim.lower()}={rec[dim][0]}"
        flags: dict[str, Any] = {"indicator_label": ind_label, "geo_label": rec["GEO_PICT"][1]}
        if status[0]:
            flags["obs_status"] = status[0]
            flags["obs_status_label"] = status[1]
            if status[0] == "E":
                flags["estimate_kind"] = "estimate"
        if mult:
            flags["unit_mult"] = mult
        for attr in ("DATA_SOURCE", "OBS_COMMENT", "CONF_STATUS"):
            v = rec.get(attr, ("", ""))
            if v[0]:
                flags[attr.lower()] = v[0][:1000] if attr != "CONF_STATUS" else f"{v[0]}: {v[1]}"
        if data_year:
            flags["data_year"] = data_year
        if dataset == "pocket":
            flags["provider_note"] = "PDH.Stat pocket summary: latest known value per year; only cells whose DATA_YEAR is the cell year are kept"
        start, end = common.year_period(int(tp))
        rows.append(
            {
                "source": ATLAS_SOURCE,
                "dataset": dataset,
                "indicator": indicator,
                "iso3": country_iso3(geo, "iso2"),
                "provider_geo": geo,
                "period_start": start,
                "period_end": end,
                "value": value,
                "value_text": value_text,
                "unit": unit if value is not None else "text",
                "flags": canon(flags),
                "raw_sha256": f.sha256,
                "selector": canon({"dataflow": flow, "row": n, "indicator": ind, "geo": geo, "time": tp}),
            }
        )
    common.dedupe_check(rows, f"SPC {flow}")
    return rows, stats


def build(got: Mapping[str, Fetched]) -> list[GapTable]:
    out = []
    for ds, (flow, ver, _k, _f) in FLOWS.items():
        f = got.get(f"data:{ds}")
        if f is None:
            continue
        rows, st = parse(ds, f)
        note = (
            f"SPC PDH.Stat {flow}({ver}), SDMX-CSV; {len(rows)} cells kept of {st['rows_read']} rows; "
            f"omitted: {st['carried_forward_omitted']} carried-forward repeats (DATA_YEAR != year), "
            f"{st['missing_status_omitted']} OBS_STATUS=O, {st['empty_omitted']} empty. Request: {f.url}"
        )
        out.append(GapTable(ds, common.frame(rows), (f.sha256,), "iso2", note, RIGHTS))
    return out


def _module() -> Any:
    import sys

    return sys.modules[__name__]


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version="1.0.0",
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="Pacific Community (SPC) — Pacific Data Hub .Stat",
        licence=f"{RIGHTS['licence']} ({RIGHTS['licence_url']})",
        allowed_hosts=(HOST,),
        datasets=tuple(FLOWS),
        dataset_ids=tuple(f"SPC:{f}({v})" for f, v, _k, _x in FLOWS.values()),
        cadence_seconds=30 * 86400,
        planned_requests=len(FLOWS),
        max_response_bytes=30_000_000,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=common.code_digest(_module()),
        field_mappings={"geo_scheme": "iso2"},
        notes="22 Pacific island countries and territories; carried-forward pocket-summary repeats omitted.",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    return await common.ingest_items(run, items(fixture=run.origin == "test_fixture"), build)


__all__ = ["FLOWS", "NAME", "atlas_ingest", "atlas_source", "build", "items", "parse", "read_sdmx_csv", "url"]
