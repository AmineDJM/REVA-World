"""Open gap-fill sources for minimum and average wages (V3, agent A-ILO).

ILOSTAT (:mod:`atlas_ilo`) is the first source for minimum wages and mean earnings. These
connectors add *separate* atlas tables from other open providers; the catalog
(``registry/atlas/catalog/labour.yaml``) lists them after ILOSTAT in ``source_order``. Nothing is
estimated, converted or blended: every cell is what the provider reports, with its own unit.

Eurostat ``earn_mw_cur`` — monthly national minimum wages (``source = "eurostat"``)
    Semi-annual (S1 = January, S2 = July values; period ``[YYYY-01-01, YYYY-07-01)`` /
    ``[YYYY-07-01, YYYY+1-01-01)``), currencies ``EUR``, ``NAC`` (national currency) and ``PPS``
    (EU purchasing power standard). Eurostat publishes rows for countries **without** a national
    minimum wage with flag ``m`` = "missing value; data cannot exist" (Austria, Denmark, Finland,
    Italy, Sweden, Norway, Iceland, Switzerland; Germany before 2015, ...). Those rows are kept as
    value-less cells with ``flags.missing = "not_applicable"`` and the provider flag and label: the
    provider's own statement that no national minimum wage exists, never a zero. Flag labels come
    from the archived ``ESTAT/OBS_FLAG`` code list. Licence: Eurostat reuse policy (CC BY 4.0
    equivalent, acknowledgement required).

OECD earnings — ``DSD_EARNINGS@MW_CURP`` (statutory minimum wages, current prices, national
    currency units; annual/monthly/weekly/daily/hourly pay periods) and ``DSD_EARNINGS@AV_AN_WAGE``
    (average annual wages per full-time equivalent employee: national currency current/constant
    prices and US dollars PPP, constant prices) (``source = "oecd"``). Licence: OECD Terms and
    Conditions; the OECD "open by default" policy makes CC BY 4.0 the default since 2024-07-01 (the
    dataset-level licence was not separately verified: oecd.org answered 403 to the check). National-currency rows carry the ISO 4217 code the OECD declares in
    ``UNIT_MEASURE``; the indicator folds it as ``unit=NCU`` so a series name does not differ per
    country, and the cell's ``unit``/``flags.currency`` keep the actual code. OECD's own conversions
    (``USD_PPP``, ``USD_EXC``) keep their code in the indicator.

Searched and not implemented (see ``docs/dev/v3_a_ilo.md``): WageIndicator (terms forbid
non-private use and AI processing), UNECE statistical database (no explicit open licence found;
its PXWeb API needs POST, which ``SafeHttpClient`` does not offer).
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any
from xml.etree import ElementTree as ET

import polars as pl

from reva.adapters.connectors import atlas_labour_common as common
from reva.adapters.connectors.atlas_labour_common import AtlasTablePlan, canon, strict_decimal
from reva.atlas.tables import ATLAS_SCHEMA
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import ConnectorSpec, FetchItem, RawAcquisition


def _module() -> Any:
    import sys

    return sys.modules[__name__]


def _sha(raw: RawAcquisition) -> str:
    if raw.raw_sha256:
        return raw.raw_sha256
    import hashlib

    return hashlib.sha256(raw.body).hexdigest()


def _frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    return pl.DataFrame({c: [r.get(c) for r in rows] for c in ATLAS_SCHEMA.names()}, schema=ATLAS_SCHEMA)


def _csv_rows(body: bytes, what: str) -> tuple[list[str], list[list[str]]]:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise SourceResponseError(f"{what}: not UTF-8", retryable=False) from e
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2:
        raise SourceResponseError(f"{what}: no data rows", retryable=False)
    return rows[0], rows[1:]


# =========================================================================== Eurostat earn_mw_cur
EUROSTAT_HOST = "ec.europa.eu"
EUROSTAT_BASE = f"https://{EUROSTAT_HOST}/eurostat/api/dissemination/sdmx/2.1"
EUROSTAT_SOURCE_KEY = "eurostat.dissemination"
EUROSTAT_RIGHTS = {
    "licence": "Eurostat reuse policy (CC BY 4.0 equivalent; acknowledgement required)",
    "licence_url": "https://ec.europa.eu/eurostat/about-us/policies/copyright",
    "attribution": "Source: Eurostat (earn_mw_cur)",
}
#: Eurostat currency codes of earn_mw_cur (Eurostat CURRENCY code list labels).
EUROSTAT_CURRENCY_UNITS = {
    "EUR": "EUR per month",
    "NAC": "national currency per month (Eurostat NAC)",
    "PPS": "PPS per month (EU purchasing power standard)",
}
_SEMESTER = re.compile(r"^([12][0-9]{3})-S([12])$")


def eurostat_mw_url(geos: Sequence[str] | None = None) -> str:
    key = f"S..{'+'.join(geos)}" if geos else ""
    return f"{EUROSTAT_BASE}/data/earn_mw_cur/{key}?format=SDMX-CSV&compressed=false"


EUROSTAT_FLAG_URL = f"{EUROSTAT_BASE}/codelist/ESTAT/OBS_FLAG"


def parse_sdmx_codelist(body: bytes) -> dict[str, str]:
    """``code -> English name`` of an SDMX 2.1 structure message holding one code list."""
    head = body[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in body.upper():
        raise SourceResponseError("SDMX code list with a DTD/entity declaration refused", retryable=False)
    try:
        # DTD/entity declarations are refused above; expat never fetches external entities.
        root = ET.fromstring(body)  # noqa: S314
    except ET.ParseError as e:
        raise SourceResponseError("SDMX code list is not well-formed XML", retryable=False) from e
    s = "{http://www.sdmx.org/resources/sdmxml/schemas/v2_1/structure}"
    c = "{http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common}"
    out: dict[str, str] = {}
    for code in root.iter(f"{s}Code"):
        cid = code.get("id")
        for name in code.findall(f"{c}Name"):
            if name.get("{http://www.w3.org/XML/1998/namespace}lang") == "en" and cid:
                out[cid] = (name.text or "").strip()
    if not out:
        raise SourceResponseError("SDMX code list has no codes", retryable=False)
    return out


def semester_period(text: str) -> tuple[date, date]:
    m = _SEMESTER.match(text)
    if not m:
        raise SourceResponseError("earn_mw_cur: unexpected TIME_PERIOD", value=text[:20], retryable=False)
    y, s = int(m.group(1)), int(m.group(2))
    return (date(y, 1, 1), date(y, 7, 1)) if s == 1 else (date(y, 7, 1), date(y + 1, 1, 1))


def parse_eurostat_mw(body: bytes, raw_sha: str, flag_labels: Mapping[str, str]) -> pl.DataFrame:
    header, rows = _csv_rows(body, "Eurostat earn_mw_cur")
    need = ("freq", "currency", "geo", "TIME_PERIOD", "OBS_VALUE", "OBS_FLAG")
    miss = [c for c in need if c not in header]
    if miss:
        raise SourceResponseError("earn_mw_cur: missing columns", missing=miss, retryable=False)
    ix = {c: header.index(c) for c in header}
    out = []
    for n, r in enumerate(rows, start=1):
        if len(r) != len(header):
            raise SourceResponseError("earn_mw_cur: ragged CSV row", row=n, retryable=False)
        freq, cur, geo, tp = r[ix["freq"]], r[ix["currency"]], r[ix["geo"]], r[ix["TIME_PERIOD"]]
        if freq != "S":
            raise SourceResponseError("earn_mw_cur: unexpected frequency", freq=freq[:5], retryable=False)
        if cur not in EUROSTAT_CURRENCY_UNITS:
            raise SourceResponseError("earn_mw_cur: unknown currency code", currency=cur[:10], retryable=False)
        start, end = semester_period(tp)
        value = strict_decimal(r[ix["OBS_VALUE"]])
        flag = r[ix["OBS_FLAG"]].strip()
        flags: dict[str, Any] = {"pay": "gross monthly national minimum wage (Eurostat earn_mw_cur)"}
        if "LAST UPDATE" in ix:
            flags["last_update"] = r[ix["LAST UPDATE"]]
        if flag:
            flags["provider_flag"] = flag
            flags["provider_flag_label"] = flag_labels.get(flag, "(code not in ESTAT/OBS_FLAG)")
        if value is None:
            # 'm' (and its combinations): "missing value; data cannot exist" -> no national minimum wage
            flags["missing"] = "not_applicable" if flag.endswith("m") else "not_reported"
        out.append(
            {
                "source": "eurostat",
                "dataset": "earn_mw_cur",
                "indicator": f"earn_mw_cur|currency={cur}",
                "provider_geo": geo,
                "period_start": start,
                "period_end": end,
                "value": value,
                "unit": EUROSTAT_CURRENCY_UNITS[cur],
                "flags": canon(flags),
                "raw_sha256": raw_sha,
                "selector": canon({"file": "earn_mw_cur.csv", "row": n}),
            }
        )
    return _frame(out)


class EurostatMinimumWageConnector:
    parser_version = "atlas-eurostat-earn-mw-parse-1"

    def __init__(self, geos: Sequence[str] | None = None) -> None:
        self.geos = tuple(geos) if geos else None

    def describe(self) -> ConnectorSpec:
        return common.atlas_spec(
            name="atlas.eurostat.earn_mw_cur",
            version="1.0.0",
            source_key=EUROSTAT_SOURCE_KEY,
            provider="Eurostat",
            licence=EUROSTAT_RIGHTS["licence"],
            allowed_hosts=(EUROSTAT_HOST,),
            dataset_ids=("earn_mw_cur",),
            field_mappings={
                "atlas_source": "eurostat",
                "tables": {"earn_mw_cur": "eurostat.earn_mw_cur"},
                "geo_scheme": "eurostat",
            },
            units=EUROSTAT_CURRENCY_UNITS,
            geography=self.geos or ("all earn_mw_cur geographies",),
            time_semantics="semi-annual: S1 -> [Y-01-01, Y-07-01), S2 -> [Y-07-01, Y+1-01-01)",
            lineage_family="eurostat",
            cadence_seconds=30 * 86400,
            release_aware=False,
            revision_strategy="whole-dataset re-download; changed content = new atlas table version",
            max_response_bytes=5_000_000,
            max_pages=1,
            parser_version=self.parser_version,
            code_digest=common.code_digest(common, _module()),
        )

    def plan(self) -> list[FetchItem]:
        return [
            FetchItem(
                key="codelist:OBS_FLAG",
                url=EUROSTAT_FLAG_URL,
                max_bytes=1_000_000,
                expected_media_types=("application/vnd.sdmx.structure+xml", "application/xml", "text/xml"),
            ),
            FetchItem(
                key="data:earn_mw_cur",
                url=eurostat_mw_url(self.geos),
                max_bytes=5_000_000,
                expected_media_types=("application/vnd.sdmx.data+csv", "text/csv", "application/octet-stream"),
            ),
        ]

    def build_tables(self, raws: Mapping[str, RawAcquisition]) -> list[AtlasTablePlan]:
        if "codelist:OBS_FLAG" not in raws or "data:earn_mw_cur" not in raws:
            raise SourceResponseError("earn_mw_cur: data or flag code list missing; no table built", retryable=False)
        labels = parse_sdmx_codelist(raws["codelist:OBS_FLAG"].body)
        raw = raws["data:earn_mw_cur"]
        frame = parse_eurostat_mw(raw.body, _sha(raw), labels)
        note = (
            "Eurostat earn_mw_cur: monthly national minimum wages, semi-annual (January/July). "
            "Rows flagged 'm' (data cannot exist) = no national minimum wage; kept as value-less cells."
        )
        if self.geos:
            note += f" Restricted request (geo={'+'.join(self.geos)})."
        return [
            AtlasTablePlan(
                "eurostat",
                "earn_mw_cur",
                frame,
                (_sha(raw), _sha(raws["codelist:OBS_FLAG"])),
                "eurostat",
                note,
                EUROSTAT_RIGHTS,
            )
        ]


# =========================================================================== OECD earnings
OECD_HOST = "sdmx.oecd.org"
OECD_BASE = f"https://{OECD_HOST}/public/rest/data"
OECD_SOURCE_KEY = "oecd.sdmx"
OECD_RIGHTS = {
    "licence": "OECD Terms and Conditions; CC BY 4.0 is the OECD default since 2024-07-01 (dataset-level "
    "licence not separately verified)",
    "licence_url": "https://www.oecd.org/en/about/oecd-open-by-default-policy.html",
    "terms_url": "https://www.oecd.org/en/about/terms-conditions.html",
    "attribution": "OECD Data Explorer, Earnings database (OECD.ELS.SAE)",
}
OECD_FLOWS: dict[str, str] = {
    # flow id -> atlas dataset key
    "DSD_EARNINGS@MW_CURP": "earnings_mw_curp",
    "DSD_EARNINGS@AV_AN_WAGE": "earnings_av_an_wage",
}
OECD_CONVERTED_UNITS = {"USD_PPP", "USD_EXC", "EUR_PPP", "PPP"}
_PERIOD_WORD = {"A": "year", "M": "month", "W": "week", "D": "day", "H": "hour"}
_ISO4217 = re.compile(r"^[A-Z]{3}$")


def oecd_url(flow: str, areas: Sequence[str] | None = None) -> str:
    key = ("+".join(areas) + "......") if areas else "all"
    return f"{OECD_BASE}/OECD.ELS.SAE,{flow},1.0/{key}?format=csvfilewithlabels"


def parse_oecd_earnings(body: bytes, raw_sha: str, flow: str) -> pl.DataFrame:
    header, rows = _csv_rows(body, f"OECD {flow}")
    need = (
        "REF_AREA",
        "MEASURE",
        "UNIT_MEASURE",
        "PAY_PERIOD",
        "PRICE_BASE",
        "AGGREGATION_OPERATION",
        "SEX",
        "TIME_PERIOD",
        "OBS_VALUE",
        "OBS_STATUS",
        "UNIT_MULT",
        "BASE_PER",
        "STRUCTURE_ID",
    )
    miss = [c for c in need if c not in header]
    if miss:
        raise SourceResponseError(f"OECD {flow}: missing columns", missing=miss, retryable=False)
    ix = {c: i for i, c in enumerate(header)}  # first occurrence (code columns precede label columns)

    def lab(r: list[str], code_col: str) -> str:
        i = ix[code_col] + 1
        return r[i] if i < len(r) else ""

    dataset = OECD_FLOWS[flow]
    out = []
    for n, r in enumerate(rows, start=1):
        if len(r) != len(header):
            raise SourceResponseError(f"OECD {flow}: ragged CSV row", row=n, retryable=False)
        if flow not in r[ix["STRUCTURE_ID"]]:
            raise SourceResponseError(f"OECD {flow}: row of another structure", row=n, retryable=False)
        tp = r[ix["TIME_PERIOD"]]
        if tp == "" and r[ix["OBS_VALUE"]].strip() == "":
            continue  # series-attribute row without a period or value: not a cell
        if not re.match(r"^[12][0-9]{3}$", tp):
            raise SourceResponseError(f"OECD {flow}: non-annual TIME_PERIOD", value=tp[:12], retryable=False)
        y = int(tp)
        unit_code = r[ix["UNIT_MEASURE"]]
        pay = r[ix["PAY_PERIOD"]]
        price = r[ix["PRICE_BASE"]]
        agg = r[ix["AGGREGATION_OPERATION"]]
        sex = r[ix["SEX"]]
        base_per = r[ix["BASE_PER"]].strip()
        mult = r[ix["UNIT_MULT"]].strip()
        ncu = bool(_ISO4217.match(unit_code)) and unit_code not in OECD_CONVERTED_UNITS
        ind = f"{r[ix['MEASURE']]}|unit={'NCU' if ncu else unit_code}|pay_period={pay}|price_base={price}"
        if agg and agg != "_Z":
            ind += f"|agg={agg}"
        if sex and sex != "_Z":
            ind += f"|sex={sex}"
        unit = f"{unit_code} per {_PERIOD_WORD.get(pay, lab(r, 'PAY_PERIOD').lower() or pay)}"
        unit += f", {lab(r, 'PRICE_BASE').lower() or price}"
        if base_per:
            unit += f" (base {base_per})"
        if mult not in ("", "0"):
            unit += f" x10^{mult}"
        flags: dict[str, Any] = {
            "measure_label": lab(r, "MEASURE"),
            "unit_label": lab(r, "UNIT_MEASURE"),
            "pay_period_label": lab(r, "PAY_PERIOD"),
            "price_base_label": lab(r, "PRICE_BASE"),
        }
        if ncu:
            flags["currency"] = unit_code
            flags["currency_basis"] = "national currency units as declared by the OECD"
        else:
            flags["currency_basis"] = f"converted by the OECD ({lab(r, 'UNIT_MEASURE')})"
        if base_per:
            flags["base_period"] = base_per
        if mult == "":
            flags["unit_multiplier"] = "not stated by provider"
        st = r[ix["OBS_STATUS"]].strip()
        if st:
            flags["provider_flag"] = st
            flags["provider_flag_label"] = lab(r, "OBS_STATUS")
        value = strict_decimal(r[ix["OBS_VALUE"]])
        if value is None:
            flags["missing"] = "not_reported"
        out.append(
            {
                "source": "oecd",
                "dataset": dataset,
                "indicator": ind,
                "provider_geo": r[ix["REF_AREA"]],
                "period_start": date(y, 1, 1),
                "period_end": date(y + 1, 1, 1),
                "value": value,
                "unit": unit[:100],
                "flags": canon(flags),
                "raw_sha256": raw_sha,
                "selector": canon({"file": f"{flow}.csv", "row": n}),
            }
        )
    return _frame(out)


class OecdEarningsConnector:
    parser_version = "atlas-oecd-earnings-parse-1"

    def __init__(self, areas: Sequence[str] | None = None, flows: Sequence[str] = tuple(OECD_FLOWS)) -> None:
        self.areas = tuple(areas) if areas else None
        self.flows = tuple(flows)

    def describe(self) -> ConnectorSpec:
        return common.atlas_spec(
            name="atlas.oecd.earnings",
            version="1.0.0",
            source_key=OECD_SOURCE_KEY,
            provider="OECD (Directorate for Employment, Labour and Social Affairs)",
            licence=OECD_RIGHTS["licence"],
            allowed_hosts=(OECD_HOST,),
            dataset_ids=self.flows,
            field_mappings={
                "atlas_source": "oecd",
                "tables": {f: f"oecd.{OECD_FLOWS[f]}" for f in self.flows},
                "geo_scheme": "iso3",
            },
            units={"MW_CURP": "national currency units per pay period", "AV_AN_WAGE": "per year (unit per row)"},
            geography=self.areas or ("all OECD reference areas",),
            time_semantics="annual -> [Y-01-01, Y+1-01-01)",
            lineage_family="oecd",
            cadence_seconds=30 * 86400,
            release_aware=False,
            revision_strategy="whole-dataflow re-download; changed content = new atlas table version",
            max_response_bytes=10_000_000,
            max_pages=1,
            parser_version=self.parser_version,
            code_digest=common.code_digest(common, _module()),
        )

    def plan(self) -> list[FetchItem]:
        return [
            FetchItem(
                key=f"data:{f}",
                url=oecd_url(f, self.areas),
                max_bytes=10_000_000,
                required=False,
                expected_media_types=("application/vnd.sdmx.data+csv", "text/csv"),
            )
            for f in self.flows
        ]

    def build_tables(self, raws: Mapping[str, RawAcquisition]) -> list[AtlasTablePlan]:
        plans = []
        for f in self.flows:
            raw = raws.get(f"data:{f}")
            if raw is None:
                continue
            frame = parse_oecd_earnings(raw.body, _sha(raw), f)
            note = f"OECD OECD.ELS.SAE {f} (csvfilewithlabels)."
            if self.areas:
                note += f" Restricted request (REF_AREA={'+'.join(self.areas)})."
            plans.append(AtlasTablePlan("oecd", OECD_FLOWS[f], frame, (_sha(raw),), "iso3", note, OECD_RIGHTS))
        return plans


__all__ = [
    "EUROSTAT_RIGHTS",
    "OECD_FLOWS",
    "OECD_RIGHTS",
    "EurostatMinimumWageConnector",
    "OecdEarningsConnector",
    "parse_eurostat_mw",
    "parse_oecd_earnings",
    "parse_sdmx_codelist",
    "semester_period",
]
