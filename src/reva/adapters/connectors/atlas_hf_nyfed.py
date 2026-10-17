"""New York Fed reference rates (EFFR, OBFR, SOFR) -> atlas table (V5, agent PRED-HF).

Source: the New York Fed Markets Data API (open, no key), full history per rate:
``https://markets.newyorkfed.org/api/rates/unsecured/effr/last/0.json`` (EFFR, since 2000-07-03),
``…/unsecured/obfr/last/0.json`` (OBFR, since 2016-03-01), ``…/secured/sofr/last/0.json`` (SOFR, since 2018-04-02).

Table ``nyfed.rates_d``: one row per (rate, published field, ``effectiveDate``); indicator
``<TYPE>|field=<API field>`` (``EFFR|field=percentRate``, ``EFFR|field=targetRateFrom``, ``SOFR|field=volumeInBillions``
…); numbers are kept as the JSON number text (``parse_float=str``). Units follow the API field names: ``percent*``,
``targetRate*``, ``intraDay*`` and ``stdDeviation`` -> ``percent``; ``volumeInBillions`` -> ``billions of US
dollars`` (``flags.unit_basis``). ``provider_geo`` = ``US`` (``iso3`` USA through the ISO alpha-2 crosswalk).

Publication and revisions (quoted, checked against the archived NY Fed page): EFFR/OBFR are published at about
9:00 a.m. ET and SOFR at about 8:00 a.m. ET on the business day after the effective date; a same-day revision at
about 2:30 p.m. ET happens only when the change exceeds one basis point and is footnoted. The API's
``revisionIndicator`` (``"r"`` = revised) is kept verbatim in ``flags.revision_indicator`` and ``footnoteId`` in
``flags.footnote_id``: these are the only daily series in the atlas with an explicit per-value revision mark.

Licence: New York Fed Terms of Use — reference-rate data may be used and distributed with the prescribed notice
and disclaimer (``RIGHTS["rates_d"]["required_notice"]``); SOFR is calculated from data licensed to the New York Fed
by DTCC Solutions LLC (flagged on SOFR rows).
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from collections.abc import Mapping
from datetime import date
from typing import Any

from reva.adapters.connectors import hf_daily_common as hc
from reva.atlas.sources import AtlasSourceSpec

NAME = "atlas.nyfed_rates"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_hf_nyfed/1"
SOURCE_KEY = "nyfed.markets"
ATLAS_SOURCE = "nyfed"
DATASETS = ("rates_d",)
API_HOST = "markets.newyorkfed.org"
WEB_HOST = "www.newyorkfed.org"
GEO = "US"
GEO_SCHEME = "iso2"
API = f"https://{API_HOST}/api/rates"
RATES = {"EFFR": "unsecured/effr", "OBFR": "unsecured/obfr", "SOFR": "secured/sofr"}
TERMS_URL = f"https://{WEB_HOST}/privacy/termsofuse"
INFO_URL = f"https://{WEB_HOST}/markets/reference-rates/additional-information-about-reference-rates"
JSON_MEDIA = ("application/json",)

FILES: tuple[hc.FileSpec, ...] = (
    *(hc.FileSpec(f"api_{t.lower()}", f"{API}/{p}/last/0.json", DATASETS, JSON_MEDIA, 10_000_000)
      for t, p in RATES.items()),
    hc.FileSpec("terms", TERMS_URL, DATASETS, ("text/html",), 3_000_000, role="policy"),
    hc.FileSpec("info", INFO_URL, DATASETS, ("text/html",), 3_000_000, role="policy"),
)  # fmt: skip

#: API fields -> unit (by field name); a numeric field not listed fails the table (never guessed)
FIELD_UNITS = {
    "percentRate": "percent",
    "percentPercentile1": "percent",
    "percentPercentile25": "percent",
    "percentPercentile75": "percent",
    "percentPercentile99": "percent",
    "targetRateFrom": "percent",
    "targetRateTo": "percent",
    "intraDayHigh": "percent",
    "intraDayLow": "percent",
    "stdDeviation": "percent",
    "volumeInBillions": "billions of US dollars",
}
NON_VALUE_FIELDS = {"effectiveDate", "type", "revisionIndicator", "footnoteId"}
#: provider text for a value not available (no row, counted; never zero)
NOT_AVAILABLE = frozenset({"NA", ""})

LICENCE = (
    "New York Fed Terms of Use: reference-rate data may be used and distributed with the prescribed notice and "
    "disclaimer"
)
_Q_NOTICE = (
    "If you use or distribute reference rate data or related information posted to the website, you must include the "
    "following notice and disclaimer with your presentation of that data or information:"
)
_Q_DTCC = (
    "The Secured Overnight Financing Rate (SOFR) Data and Broad General Collateral Rate (BGCR) Data are calculated "
    "using data provided under a license granted to the New York Fed by DTCC Solutions LLC"
)
_Q_EFFR_PUB = "The EFFR and the OBFR will be published at approximately 9:00 a.m. ET each business day"
_Q_EFFR_REV = (
    "These revisions would only take place if the change in the published rate exceeds one basis point. Any time a "
    "rate were to be revised, a footnote would indicate the revision."
)
_Q_SOFR_REV = (
    "Rate revisions will only occur if the change in the rate exceeds one basis point and only on the same day as "
    "initial publication."
)
REQUIRED_NOTICE = (
    "The EFFR, OBFR and SOFR data are subject to the Terms of Use posted at newyorkfed.org. The New York Fed is not "
    "responsible for publication of the EFFR, OBFR and SOFR data by REVA World, does not sanction or endorse any "
    "particular republication, and has no liability for your use."
)
RIGHTS: dict[str, dict[str, Any]] = {
    "rates_d": {
        "licence": LICENCE,
        "licence_url": TERMS_URL,
        "terms_quote": _Q_NOTICE,
        "required_notice": REQUIRED_NOTICE,
        "attribution": "Source: Federal Reserve Bank of New York",
        "licence_checked": "2026-10-17",
        "provider": "Federal Reserve Bank of New York",
        "publication": "business day after the effective date: EFFR/OBFR ~9:00 a.m. ET, SOFR ~8:00 a.m. ET",
        "pub_lag": "next_business_day",
        "revision_policy": "same-day revision ~2:30 p.m. ET only if > 1 bp, footnoted (API revisionIndicator 'r')",
        "policy_quotes": {
            "notice": ("terms", _Q_NOTICE),
            "dtcc": ("terms", _Q_DTCC),
            "effr_publication": ("info", _Q_EFFR_PUB),
            "effr_revisions": ("info", _Q_EFFR_REV),
            "sofr_revisions": ("info", _Q_SOFR_REV),
        },
    }
}
SOFR_NOTE = "SOFR is calculated using data licensed to the New York Fed by DTCC Solutions LLC (NY Fed Terms of Use)"


def code_digest() -> str:
    return hc.code_digest(sys.modules[__name__])


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="Federal Reserve Bank of New York (Markets Data API reference rates)",
        licence=LICENCE,
        allowed_hosts=(API_HOST, WEB_HOST),
        datasets=DATASETS,
        dataset_ids=tuple(f"NYFED:{t}" for t in RATES),
        cadence_seconds=hc.DAY,
        planned_requests=len(FILES),
        max_response_bytes=10_000_000,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"geo_scheme": GEO_SCHEME, "tables": ["nyfed.rates_d"]},
        notes="three full-history JSON files (~2.7 MB) + terms and methodology pages per full run",
    )


def parse_rate(rtype: str, f: hc.Fetched, rows: hc.DailyRows, report: dict[str, Any]) -> None:
    try:
        doc = json.loads(f.body, parse_float=str, parse_int=str)
    except ValueError as e:
        raise hc.err(f"{rtype}: not JSON ({e})") from e
    recs = doc.get("refRates") if isinstance(doc, dict) else None
    if not isinstance(recs, list) or not recs:
        raise hc.err(f"{rtype}: no refRates list", kind="incomplete")
    rights = RIGHTS["rates_d"]
    n: Counter[str] = Counter()
    na: Counter[str] = Counter()
    revised = 0
    for i, r in enumerate(recs):
        if not isinstance(r, dict) or r.get("type") != rtype:
            raise hc.err(f"{rtype}: record {i} has type {r.get('type') if isinstance(r, dict) else r!r}")
        try:
            d = date.fromisoformat(str(r["effectiveDate"]))
        except (KeyError, ValueError) as e:
            raise hc.err(f"{rtype}: record {i}: bad effectiveDate") from e
        unknown = set(r) - set(FIELD_UNITS) - NON_VALUE_FIELDS
        if unknown:
            raise hc.err(f"{rtype}: record {i}: fields without a known unit {sorted(unknown)}", kind="unit")
        rev = str(r.get("revisionIndicator") or "").strip()
        revised += bool(rev)
        fl = rows.flags(**hc.base_flags(rights, f, revision_indicator=rev or None,
                                        footnote_id=str(r["footnoteId"]) if r.get("footnoteId") else None,
                                        unit_basis="NY Fed API field name", required_notice=REQUIRED_NOTICE,
                                        data_licence_note=SOFR_NOTE if rtype == "SOFR" else None))  # fmt: skip
        for field, unit in FIELD_UNITS.items():
            if field not in r or r[field] is None:
                continue
            if str(r[field]).strip() in NOT_AVAILABLE:
                na[f"{field}={r[field]}"] += 1
                continue
            v, normalised = hc.number(str(r[field]))
            if v is None:
                raise hc.err(f"{rtype}: record {i}: {field} = {r[field]!r} is not a number")
            rows.add(indicator=f"{rtype}|field={field}", provider_geo=GEO, day=d, value=v, unit=unit,
                     flags=fl if not normalised else rows.flags(**{**json.loads(fl), "value_normalised": True}),
                     raw_sha256=f.sha256, selector={"file": f.key, "index": i, "field": field})  # fmt: skip
            n[field] += 1
    report.setdefault("rates", {})[rtype] = {
        "records": len(recs),
        "revised_records": revised,
        "cells": dict(n),
        "not_available": dict(na),
    }


def build(
    files: Mapping[str, hc.Fetched], wanted: tuple[str, ...] = DATASETS
) -> tuple[dict[str, hc.TableBuild], dict[str, Any]]:
    out = {ds: hc.TableBuild(hc.DailyRows(ATLAS_SOURCE, ds)) for ds in wanted}
    report: dict[str, Any] = {}
    if "rates_d" in wanted:
        for t in RATES:
            parse_rate(t, files[f"api_{t.lower()}"], out["rates_d"].rows, report)
    report["policy_quotes_found"] = hc.check_policy_pages(files, FILES, RIGHTS, wanted)
    return out, report


NOTES = {
    "rates_d": "New York Fed reference rates EFFR (2000-), OBFR (2016-), SOFR (2018-): rate, volume-weighted "
    "percentiles, volume, FOMC target range (EFFR; a single target before the range), intraday high/low and standard "
    "deviation (older EFFR). One row per published field and effective date. flags.revision_indicator = API 'r' "
    "(same-day revision). Notice required by the NY Fed Terms of Use in flags.required_notice. provider_geo US.",
}


async def atlas_ingest(run: Any) -> dict[str, Any]:
    wanted = tuple(d for d in DATASETS if run.wants(d))
    files = await hc.fetch_all(run, FILES, wanted)
    builds, report = build(files, wanted)
    tables = hc.register_tables(run, builds, rights=RIGHTS, notes=NOTES, source_key=SOURCE_KEY, geo_scheme=GEO_SCHEME)
    return {"ok": True, "tables": tables, "report": report}


__all__ = ["DATASETS", "FILES", "NAME", "RATES", "RIGHTS", "atlas_ingest", "atlas_source", "build"]
