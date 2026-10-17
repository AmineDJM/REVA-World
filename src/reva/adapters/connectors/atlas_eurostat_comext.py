"""Eurostat Comext monthly EU trade (HS chapters, partners) -> atlas tables (V6, agent V6TRADEHF).

Source: Eurostat Comext dissemination SDMX 2.1 API (``https://ec.europa.eu/eurostat/api/comext/dissemination``,
open, no key), dataset ``DS-045409`` "EU trade since 1988 by HS2-4-6 and CN8", tab-separated output
(``format=TSV``: one row per series, one column per month; ~11 bytes per value against ~85 in SDMX-CSV).
Reporters: the 27 EU member states (Comext reporter codes are ISO 3166 alpha-2, Greece ``GR``).

================  ====================================================================================
dataset           content (monthly, as published; never summed or averaged)
================  ====================================================================================
comext_hs2_m      reporter x partner aggregate (``WORLD``, ``EXT_EU27_2020``, ``INT_EU27_2020``) x product
                  (``TOTAL`` and every 2-digit chapter of the code list) x flow (1 import, 2 export) x
                  ``VALUE_IN_EUROS`` and ``QUANTITY_IN_100KG``; from ``hs2_start`` (default 2022-01)
comext_partner_m  reporter x every partner of the code list x product ``TOTAL`` x flow x ``VALUE_IN_EUROS``;
                  from ``partner_start`` (default 2024-01)
================  ====================================================================================

Requests: the partner code list ``CXT_FREE_ISO`` (labels, archived), then per reporter four HS-chapter groups
(the API answers 25-chapter requests in seconds; a 98-chapter request took 128 s or was reset on 2026-10-17,
and multi-reporter requests are queued asynchronously, which this connector does not follow) and one partner
request. A failed request is retried once; a reporter whose requests still fail is listed in the coverage note
(the table is then incomplete, never padded).

Honesty rules:

* Values verbatim as strict decimals; ``:`` (not available) -> no cell, counted; a flag after the value (``p``,
  ``e``, ``c`` ...) is kept in ``flags.provider_flag``.
* Months as ``[first day, first day of next month)``; ``flags.frequency = "M"``; seasonal adjustment is not
  stated by the provider (``flags.seasonal_adjustment = "not_stated_by_provider"``).
* Products are linked by code only: a 2-digit chapter ``01``..``97`` in a period from 2022-01 names the KG entity
  ``hs2022:<CC>`` (``flags.hs_chapter_kg``; the Combined Nomenclature of 2022 onwards is built on HS 2022, whose
  chapters it keeps). Chapter ``99`` (CN special codes) and ``TOTAL`` name no HS entity. No product labels are
  copied (the KG holds the UNSD HS labels).
* Partner aggregates (``WORLD``, ``EXT_EU27_2020``, ``Q*`` ...) stay unmapped (``partner_iso3`` null); labels from
  the archived code list.
* Units from the indicator code: ``VALUE_IN_EUROS`` -> ``euro``, ``QUANTITY_IN_100KG`` -> ``100 kg``.
* Reports of the 27 reporters are not the EU aggregate; extra-EU totals are Eurostat's ``EXT_EU27_2020`` partner.

    uv run reva atlas ingest atlas.eurostat_comext [--dataset comext_hs2_m] [--option hs2_start=2022-01] [--fixture]
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import polars as pl

from reva.adapters.connectors import atlas_fiscal_common as fc
from reva.adapters.connectors import atlas_imf as imf
from reva.atlas.sources import AtlasSourceSpec
from reva.atlas.tables import ATLAS_SCHEMA, country_iso3
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.eurostat_comext"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas_eurostat_comext/1"
SOURCE = "eurostat"
SOURCE_KEY = "eurostat.comext"
HOST = "ec.europa.eu"
BASE = f"https://{HOST}/eurostat/api/comext/dissemination/sdmx/2.1"
DATAFLOW = "DS-045409"
DATAFLOW_TITLE = "EU trade since 1988 by HS2-4-6 and CN8"
GEO_SCHEME = "comext"
MAX_BYTES = 10_000_000
LICENCE = "Eurostat reuse policy (Commission Decision 2011/833/EU; CC BY 4.0), acknowledgement required"
RIGHTS: dict[str, Any] = {
    "licence": LICENCE,
    "licence_url": "https://ec.europa.eu/eurostat/about-us/policies/copyright",
    "attribution": f"Source: Eurostat Comext ({DATAFLOW})",
    "licence_checked": "2026-10-17",
    "source_key": SOURCE_KEY,
}

#: the 27 EU member states as Comext reporters (code list CXT_FREE_ISO 'EU27_2020' label, quoted 2026-10-17:
#: "European Union - 27 countries (AT, BE, BG, CY, CZ, DE, DK, EE, ES, FI, FR, GR, HR, HU, IE, IT, LT, LU, LV,
#: MT, NL, PL, PT, RO, SE, SI, SK)")
REPORTERS = (
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU",
    "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK",
)  # fmt: skip
PARTNER_AGGREGATES = ("WORLD", "EXT_EU27_2020", "INT_EU27_2020")
#: 2-digit chapters accepted by the DS-045409 key (the API refuses 77 and 98: "not allowed" on 2026-10-17)
CHAPTERS = tuple(f"{i:02d}" for i in range(1, 100) if i not in (77, 98))
CHAPTER_GROUPS: tuple[tuple[str, ...], ...] = tuple(
    (("TOTAL",) if i == 0 else ()) + CHAPTERS[i : i + 25] for i in range(0, len(CHAPTERS), 25)
)
FLOWS = {"1": "IMPORT", "2": "EXPORT"}  # code list CXT_EU_FLUX (quoted)
UNITS = {"VALUE_IN_EUROS": "euro", "QUANTITY_IN_100KG": "100 kg"}
HS2_INDICATORS = ("VALUE_IN_EUROS", "QUANTITY_IN_100KG")
PARTNER_INDICATORS = ("VALUE_IN_EUROS",)
DIMS = ("freq", "reporter", "partner", "product", "flow", "indicators")
HS2_START = "2022-01"
PARTNER_START = "2024-01"
HS_KG_FROM = date(2022, 1, 1)
DATASETS = ("comext_hs2_m", "comext_partner_m")
TITLES = {
    "comext_hs2_m": "EU member states' monthly goods trade by HS chapter with the World / extra-EU / intra-EU",
    "comext_partner_m": "EU member states' monthly goods trade (all products) by partner country",
}
FIXTURE_REPORTERS = ("FR", "GR")
FIXTURE_START = "2026-03"
_MONTH = re.compile(r"^([0-9]{4})-(0[1-9]|1[0-2])$")
_NUMBER = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")


class ComextError(SourceResponseError):
    def __init__(self, msg: str, **ctx: Any) -> None:
        super().__init__(msg, retryable=False, kind="parse_error", **ctx)


def check_month(v: Any, name: str) -> str:
    s = str(v).strip()
    if not _MONTH.match(s):
        raise ComextError("run option must be YYYY-MM", option=name, value=s[:20])
    return s


def month(text: str) -> tuple[date, date]:
    m = _MONTH.match(text.strip())
    if not m:
        raise ComextError("unsupported Comext period", period=text[:20])
    y, mo = int(m.group(1)), int(m.group(2))
    return date(y, mo, 1), (date(y, mo + 1, 1) if mo < 12 else date(y + 1, 1, 1))


def data_url(
    reporter: str, partners: Sequence[str], products: Sequence[str], indicators: Sequence[str], start: str
) -> str:
    key = f"M.{reporter}.{'+'.join(partners)}.{'+'.join(products)}.{'+'.join(FLOWS)}.{'+'.join(indicators)}"
    return f"{BASE}/data/{DATAFLOW}/{key}?startPeriod={start}&format=TSV"


def codelist_url() -> str:
    return f"{BASE}/codelist/ESTAT/CXT_FREE_ISO/latest"


def requests_for(reporter: str, hs2_start: str, partner_start: str) -> list[tuple[str, str, str]]:
    """``(dataset, item key, url)`` of one reporter."""
    out = [
        (
            "comext_hs2_m",
            f"hs2:{reporter}:{g[1] if g[0] == 'TOTAL' else g[0]}-{g[-1]}",
            data_url(reporter, PARTNER_AGGREGATES, g, HS2_INDICATORS, hs2_start),
        )
        for g in CHAPTER_GROUPS
    ]
    out.append(
        (
            "comext_partner_m",
            f"partner:{reporter}",
            data_url(reporter, (), ("TOTAL",), PARTNER_INDICATORS, partner_start),
        )
    )
    return out


@dataclass
class ComextReport:
    dataset: str
    series: int = 0
    cells: int = 0
    not_available: Counter[str] = field(default_factory=Counter)
    flags: Counter[str] = field(default_factory=Counter)
    zeros: int = 0
    reporters: set[str] = field(default_factory=set)
    partners: set[str] = field(default_factory=set)
    products: set[str] = field(default_factory=set)
    periods: tuple[str, str] | None = None
    requests: int = 0
    failed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "requests": self.requests,
            "series": self.series,
            "cells": self.cells,
            "not_available_by_flag": dict(self.not_available),
            "provider_flags": dict(self.flags),
            "reported_zeros": self.zeros,
            "reporters": sorted(self.reporters),
            "partners": len(self.partners),
            "unmapped_partners": sorted(p for p in self.partners if country_iso3(p, GEO_SCHEME) is None),
            "products": len(self.products),
            "periods": list(self.periods) if self.periods else None,
            "failed_requests": self.failed,
        }


def _token(tok: str) -> tuple[str | None, str]:
    """A TSV cell -> (strict decimal or None, provider flag)."""
    t = tok.strip()
    if not t:
        return None, ""
    if t.startswith(":"):
        return None, t[1:].strip()
    num, _, flag = t.partition(" ")
    if not _NUMBER.match(num):
        raise ComextError("Comext TSV: value is not a decimal", token=t[:30])
    return fc.strict_decimal(num), flag.strip()


def parse_tsv(
    body: bytes,
    sha: str,
    label: str,
    rep: ComextReport,
    cols: dict[str, list[Any]],
    partner_labels: Mapping[str, str],
    *,
    expect: Mapping[str, Sequence[str]],
) -> None:
    """Append the cells of one Comext TSV response to ``cols``. ``expect``: allowed codes per dimension."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ComextError("Comext TSV: not UTF-8") from e
    if text.lstrip().startswith("<"):
        raise ComextError("Comext: XML instead of TSV (provider fault or asynchronous queue)", head=text[:200])
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ComextError("Comext TSV: empty body")
    head = lines[0].split("\t")
    dims = head[0].split("\\")[0].split(",")
    if tuple(dims) != DIMS or not head[0].endswith("\\TIME_PERIOD"):
        raise ComextError("Comext TSV: unexpected header", header=head[0][:120])
    periods = [h.strip() for h in head[1:]]
    spans = [month(p) for p in periods]
    rep.requests += 1
    for ln in lines[1:]:
        parts = ln.split("\t")
        key = dict(zip(DIMS, parts[0].split(","), strict=True))
        if len(parts) != len(periods) + 1:
            raise ComextError("Comext TSV: ragged row", series=parts[0][:80])
        if key["freq"] != "M":
            raise ComextError("Comext TSV: unrequested frequency", freq=key["freq"][:4])
        for d, allowed in expect.items():
            if allowed and key[d] not in allowed:
                raise ComextError("Comext TSV: unrequested code", dimension=d, code=key[d][:40])
        rep.series += 1
        p, prod, flow, ind = key["partner"], key["product"], key["flow"], key["indicators"]
        piso = country_iso3(p, GEO_SCHEME)
        indicator = f"{ind}|flow={flow}|partner={p}|product={prod}"
        unit = UNITS[ind]
        series_key = parts[0]
        for (start, end), tp, tok in zip(spans, periods, parts[1:], strict=True):
            val, flag = _token(tok)
            if val is None:
                rep.not_available[flag or "(none)"] += 1
                continue
            flags: dict[str, Any] = {
                "frequency": "M",
                "seasonal_adjustment": "not_stated_by_provider",
                "flow": flow,
                "flow_label": FLOWS[flow],
                "partner": p,
                "partner_label": partner_labels.get(p, "(not in the archived code list)"),
                "partner_iso3": piso,
                "product": prod,
            }
            if piso is None:
                flags["partner_unmapped"] = True
            if prod.isdigit() and int(prod) <= 97 and start >= HS_KG_FROM:
                flags["hs_chapter_kg"] = f"hs2022:{prod}"
            if flag:
                flags["provider_flag"] = flag
                rep.flags[flag] += 1
                if "e" in flag:
                    flags["estimate_kind"] = "estimate"
                elif "p" in flag:
                    flags["estimate_kind"] = "provisional"
            if float(val) == 0:
                rep.zeros += 1
            rep.reporters.add(key["reporter"])
            rep.partners.add(p)
            rep.products.add(prod)
            cols["indicator"].append(indicator)
            cols["provider_geo"].append(key["reporter"])
            cols["period_start"].append(start)
            cols["period_end"].append(end)
            cols["value"].append(val)
            cols["unit"].append(unit)
            cols["flags"].append(fc.canon(flags))
            cols["raw_sha256"].append(sha)
            cols["selector"].append(
                fc.canon({"file": label, "dataset": DATAFLOW, "series": series_key, "time_period": tp})
            )


def frame(cols: Mapping[str, list[Any]], dataset: str, rep: ComextReport) -> pl.DataFrame:
    if not cols["indicator"]:
        raise ComextError("Comext: no cell with a value", dataset=dataset)
    rep.cells = len(cols["indicator"])
    starts = cols["period_start"]
    rep.periods = (min(starts).isoformat(), max(starts).isoformat())
    df = pl.DataFrame(
        dict(cols),
        schema={
            "indicator": pl.String,
            "provider_geo": pl.String,
            "period_start": pl.Date,
            "period_end": pl.Date,
            "value": pl.String,
            "unit": pl.String,
            "flags": pl.String,
            "raw_sha256": pl.String,
            "selector": pl.String,
        },
    ).select(
        pl.lit(SOURCE).alias("source"),
        pl.lit(dataset).alias("dataset"),
        "indicator",
        pl.lit(None, dtype=pl.String).alias("concept_id"),
        pl.lit(None, dtype=pl.String).alias("iso3"),
        "provider_geo",
        pl.lit(None, dtype=pl.String).alias("entity_qid"),
        "period_start",
        "period_end",
        "value",
        pl.lit(None, dtype=pl.String).alias("value_text"),
        "unit",
        "flags",
        "raw_sha256",
        "selector",
    )
    df = df.cast(dict(ATLAS_SCHEMA))  # type: ignore[arg-type]
    dup = df.select(pl.struct("indicator", "provider_geo", "period_start", "period_end").is_duplicated().sum()).item()
    if dup:
        raise ComextError(f"Comext: {dup} duplicate cells", dataset=dataset)
    return df


def coverage_note(dataset: str, rep: ComextReport, start: str) -> str:
    d = rep.as_dict()
    missing = sorted(set(REPORTERS) - rep.reporters)
    return (
        f"{TITLES[dataset]}. Eurostat Comext {DATAFLOW} '{DATAFLOW_TITLE}' (SDMX 2.1 TSV); startPeriod {start};"
        f" {rep.requests} response(s); {rep.series} series, {rep.cells} cells (values ':' are not cells:"
        f" {dict(rep.not_available)}); reporters {len(rep.reporters)}, partners {len(rep.partners)}, products"
        f" {len(rep.products)}; periods {d['periods']}; provider flags {dict(rep.flags)}. Units from the indicator"
        " code (VALUE_IN_EUROS euro, QUANTITY_IN_100KG 100 kg). Seasonal adjustment not stated by the provider."
        " HS chapters 01..97 from 2022-01 link to hs2022:<CC> by code (flags.hs_chapter_kg)."
        + (
            f" INCOMPLETE: failed requests {', '.join(rep.failed)}; reporters without cells: {', '.join(missing)}."
            if rep.failed or missing
            else ""
        )
        + f" Licence: {LICENCE}."
    )[:4000]


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=SOURCE,
        provider="Eurostat Comext (dissemination SDMX 2.1 API), DS-045409 monthly EU trade",
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=DATASETS,
        dataset_ids=(DATAFLOW,),
        cadence_seconds=30 * 86_400,
        planned_requests=1 + len(REPORTERS) * (len(CHAPTER_GROUPS) + 1),
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=fc.module_digest(imf, sys.modules[__name__]),
        field_mappings={
            "geo_scheme": GEO_SCHEME,
            "tables": {d: f"{SOURCE}.{d}" for d in DATASETS},
            "time_semantics": "calendar months as published",
            "indicator": "<indicators>|flow=<1|2>|partner=<code>|product=<TOTAL|CC>",
            "kg_products": "product=<CC> (01..97, periods >= 2022-01) -> hs2022:<CC>",
        },
        notes="1 code list + 27 reporters x (4 HS-chapter groups + 1 partner request); ~25 MB measured 2026-10-17.",
    )


async def _fetch(run: Any, url: str, key: str, media: tuple[str, ...]) -> tuple[bytes, str]:
    raw = await run.get(
        url, key=key, media_types=media, max_bytes=MAX_BYTES, params={"kind": "comext", "dataset": DATAFLOW}
    )
    if not raw.body:
        raise SourceResponseError("Comext: empty response body", retryable=True)
    return raw.body, run.archive(raw)


async def atlas_ingest(run: Any) -> dict[str, Any]:
    from reva.operations.budgets import BudgetExhausted

    fixture = run.origin == "test_fixture"
    opts = run.options or {}
    hs2_start = FIXTURE_START if fixture else check_month(opts.get("hs2_start", HS2_START), "hs2_start")
    partner_start = FIXTURE_START if fixture else check_month(opts.get("partner_start", PARTNER_START), "partner_start")
    reporters = FIXTURE_REPORTERS if fixture else REPORTERS
    out: dict[str, Any] = {"fetched": [], "errors": [], "tables": [], "reports": {}}
    try:
        body, cl_sha = await _fetch(
            run, codelist_url(), "codelist:CXT_FREE_ISO", ("application/vnd.sdmx.structure+xml", "application/xml")
        )
        out["fetched"].append({"item": "codelist:CXT_FREE_ISO", "bytes": len(body), "sha256": cl_sha})
        partner_labels = imf.parse_codelist(body)
    except BudgetExhausted:
        raise
    except Exception as e:
        out["errors"].append({"item": "codelist", "error": type(e).__name__, "message": str(e)[:300]})
        out["ok"] = False
        return out
    reps = {d: ComextReport(d) for d in DATASETS}
    cols: dict[str, dict[str, list[Any]]] = {d: {c: [] for c in fc._COLS} for d in DATASETS}
    shas: dict[str, list[str]] = {d: [cl_sha] for d in DATASETS}
    expect = {
        "comext_hs2_m": {
            "partner": PARTNER_AGGREGATES,
            "flow": tuple(FLOWS),
            "indicators": HS2_INDICATORS,
            "product": ("TOTAL", *CHAPTERS),
        },
        "comext_partner_m": {"flow": tuple(FLOWS), "indicators": PARTNER_INDICATORS, "product": ("TOTAL",)},
    }
    for r in reporters:
        for dataset, key, url in requests_for(r, hs2_start, partner_start):
            if not run.wants(dataset):
                continue
            last: Exception | None = None
            for _attempt in range(1 if fixture else 2):
                try:
                    body, sha = await _fetch(run, url, key, ("text/tab-separated-values", "text/plain"))
                    out["fetched"].append({"item": key, "bytes": len(body), "sha256": sha})
                    parse_tsv(
                        body,
                        sha,
                        f"{DATAFLOW}_{key.replace(':', '_')}.tsv",
                        reps[dataset],
                        cols[dataset],
                        partner_labels,
                        expect={**expect[dataset], "reporter": (r,)},
                    )
                    shas[dataset].append(sha)
                    last = None
                    break
                except BudgetExhausted:
                    raise
                except Exception as e:
                    last = e
            if last is not None:
                reps[dataset].failed.append(key)
                out["errors"].append(
                    {"dataset": dataset, "item": key, "error": type(last).__name__, "message": str(last)[:300]}
                )
    for dataset in DATASETS:
        if not run.wants(dataset) or len(shas[dataset]) < 2:
            continue
        rep = reps[dataset]
        try:
            df = frame(cols[dataset], dataset, rep)
            reg = run.register(
                dataset=dataset,
                frame=df,
                raw_sha256s=shas[dataset],
                coverage_note=coverage_note(dataset, rep, hs2_start if dataset == "comext_hs2_m" else partner_start),
                geo_scheme=GEO_SCHEME,
                rights=dict(RIGHTS),
            )
        except BudgetExhausted:
            raise
        except Exception as e:
            details = getattr(e, "details", None)
            out["errors"].append(
                {
                    "dataset": dataset,
                    "stage": "parse/register",
                    "error": type(e).__name__,
                    "message": str(e)[:500],
                    "details": json.loads(json.dumps(details, default=str)) if details else None,
                }
            )
            continue
        out["reports"][dataset] = rep.as_dict()
        out["tables"].append(
            {
                "table": f"{SOURCE}.{dataset}",
                "dataset_key": reg.get("dataset_key"),
                "created": reg.get("created"),
                "rows": reg.get("row_count"),
                "countries": (reg.get("coverage") or {}).get("countries"),
                "artifact_sha256": reg.get("artifact_sha256"),
            }
        )
    out["ok"] = not out["errors"]
    return out


__all__ = [
    "CHAPTER_GROUPS",
    "CONNECTOR_NAME",
    "DATASETS",
    "atlas_ingest",
    "atlas_source",
    "data_url",
    "parse_tsv",
    "requests_for",
]
