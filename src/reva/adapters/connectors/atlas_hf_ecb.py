"""ECB daily series -> atlas tables (V5, agent PRED-HF): euro reference FX rates, €STR, key ECB interest rates.

=============  =========================================================================================  ==========
dataset        provider file                                                                              rows
=============  =========================================================================================  ==========
``fx_ref``     ``https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip`` (member                     ~190 k
               ``eurofxref-hist.csv``): every euro foreign exchange reference rate since 1999-01-04, one
               column per currency (units of currency per 1 euro); ``N/A`` = not quoted that day -> no row
``estr``       ECB Data Portal SDMX API ``EST/B.EU000A2X2A25.WT+R25+R75+TT+NT+NB+VL`` (SDMX-CSV): the euro    ~12.6 k
               short-term rate (WT, volume-weighted trimmed mean), its 25th/75th volume percentiles, total
               volume, number of transactions, number of active banks, share of the 5 largest banks
``key_rates``  ECB Data Portal ``FM/D.U2.EUR.4F.KR.DFR+MRR_FR+MRR_MBR+MLFR.LEV``: deposit facility, main      ~30 k
               refinancing (fixed rate / minimum bid rate) and marginal lending facility rates, the level in
               force on each calendar day ("date of changes" series)
=============  =========================================================================================  ==========

Indicators: ``fx_ref`` -> ``EUR_REF|currency=<ISO 4217 code as the column header>`` (unit ``<code> per EUR``:
the ECB page states "All currencies quoted against the euro (base currency)"); the SDMX tables use the ECB series
``KEY`` verbatim (``EST.B.EU000A2X2A25.WT``). Units of the SDMX tables are the ECB ``CL_UNIT`` labels with their
code (``Percent per annum (PCPA)``); ``UNIT_MULT`` is kept in ``flags.unit_mult`` and never applied (``TT`` is in
millions of euro). ``provider_geo`` = ``U2`` (euro area, changing composition, the ECB area code of these series;
not a country: ``iso3`` null by design).

Publication / revisions (quoted in :data:`RIGHTS` and checked against the archived ECB pages at each run):
reference rates are published around 16:00 CET on the same TARGET working day ("for information purposes only");
the €STR for trading day D is published at 08:00 CET on the next TARGET2 business day and revised the same day
only for errors above 2 basis points (ECB "€STR methodology and policies", PDF, quoted from the 2026-10-17 copy;
not machine-checked); the key-rate series carry the level in force per calendar day.

Licence: ECB website copyright notice — free use provided the information appears accurately and the ECB is
cited as the source (modification must be stated). "The ECB does not charge for the €STR or license its use."
"""

from __future__ import annotations

import csv
import io
import sys
import zipfile
from collections import Counter
from collections.abc import Mapping
from datetime import date
from typing import Any

from reva.adapters.connectors import hf_daily_common as hc
from reva.atlas.sources import AtlasSourceSpec

NAME = "atlas.ecb_daily"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_hf_ecb/1"
SOURCE_KEY = "ecb.stats"
ATLAS_SOURCE = "ecb"
DATASETS = ("fx_ref", "estr", "key_rates")
HOSTS = ("www.ecb.europa.eu", "data-api.ecb.europa.eu")
GEO = "U2"
API = "https://data-api.ecb.europa.eu/service/data"
ZIP_MEDIA = ("application/zip", "application/x-zip-compressed", "application/octet-stream")
CSV_MEDIA = ("text/csv", "application/vnd.sdmx.data+csv")
HTML_MEDIA = ("text/html",)
FX_MEMBER = "eurofxref-hist.csv"
ESTR_KEY = "B.EU000A2X2A25.WT+R25+R75+TT+NT+NB+VL"
KR_KEY = "D.U2.EUR.4F.KR.DFR+MRR_FR+MRR_MBR+MLFR.LEV"
DISCLAIMER_URL = "https://www.ecb.europa.eu/services/using-our-site/disclaimer/html/index.en.html"

FILES: tuple[hc.FileSpec, ...] = (
    hc.FileSpec("fx_zip", "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip", ("fx_ref",), ZIP_MEDIA,
                5_000_000),
    hc.FileSpec("fx_page", "https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/"
                "html/index.en.html", ("fx_ref",), HTML_MEDIA, 2_000_000, role="policy"),
    hc.FileSpec("estr_csv", f"{API}/EST/{ESTR_KEY}?format=csvdata", ("estr",), CSV_MEDIA, 30_000_000),
    hc.FileSpec("estr_page", "https://www.ecb.europa.eu/stats/financial_markets_and_interest_rates/"
                "euro_short-term_rate/html/index.en.html", ("estr",), HTML_MEDIA, 2_000_000, role="policy"),
    hc.FileSpec("kr_csv", f"{API}/FM/{KR_KEY}?format=csvdata", ("key_rates",), CSV_MEDIA, 60_000_000),
    hc.FileSpec("disclaimer", DISCLAIMER_URL, DATASETS, HTML_MEDIA, 2_000_000, role="policy"),
)  # fmt: skip

LICENCE = (
    "ECB website copyright: free use provided the information appears accurately and the ECB is cited as the source"
    " (modifications stated explicitly)"
)
_COPYRIGHT_Q = (
    "disclaimer",
    "users of this website may make free use of the information obtained directly from it subject to the following "
    "conditions: When such information is distributed or reproduced, it must appear accurately and the ECB must be "
    "cited as the source.",
)
_MODIFIED_Q = (
    "disclaimer",
    "If the information is modified by the user (e.g. by seasonal adjustment of statistical data or calculation of "
    "growth rates) this must be stated explicitly.",
)
_BASE = {
    "licence": LICENCE,
    "licence_url": DISCLAIMER_URL,
    "attribution": "Source: European Central Bank (ECB)",
    "licence_checked": "2026-10-17",
    "provider": "European Central Bank",
}
RIGHTS: dict[str, dict[str, Any]] = {
    "fx_ref": {
        **_BASE,
        "terms_quote": _COPYRIGHT_Q[1],
        "publication": "same TARGET working day around 16:00 CET (concertation around 14:10 CET)",
        "pub_lag": "same_day",
        "revision_policy": "no revision policy stated on the reference-rate page; value as fetched (as_of)",
        "policy_quotes": {
            "fx_publication": ("fx_page", "The reference rates are usually updated at around 16:00 CET every working "
                               "day, except on TARGET closing days."),
            "fx_concertation": ("fx_page", "They are based on the daily concertation procedure between central banks "
                                "across Europe, which normally takes place around 14:10 CET."),
            "fx_information_only": ("fx_page", "The reference rates are published for information purposes only. Using "
                                    "the rates for transaction purposes is strongly discouraged."),
            "fx_base_currency": ("fx_page", "All currencies quoted against the euro (base currency)"),
            "copyright": _COPYRIGHT_Q,
            "modified": _MODIFIED_Q,
        },
    },
    "estr": {
        **_BASE,
        "terms_quote": "The ECB does not charge for the €STR or license its use. " + _COPYRIGHT_Q[1],
        "publication": "08:00 CET on the next TARGET2 business day after the trading day (period = trading day)",
        "pub_lag": "next_business_day",
        "revision_policy": "revised and republished the same day (no later than 11:00 CET) only for errors above 2 bp",
        "revision_quote": "If errors are detected following standard publication that affect the published €STR by "
        "more than 2 basis points, the €STR is revised and republished on the same day, no later than 11:00 CET. "
        "(ECB, Euro short-term rate (€STR) methodology and policies, PDF "
        "https://www.ecb.europa.eu/stats/euro-short-term-rates/interest_rate_benchmarks/WG_euro_risk-free_rates/"
        "shared/pdf/ecb.ESTER_methodology_and_policies.en.pdf, read 2026-10-17, not archived by this connector)",
        "policy_quotes": {
            "estr_publication": ("estr_page", "The euro short-term rate (€STR) is published on each TARGET2 business day "
                                 "based on transactions conducted and settled on the previous TARGET2 business day."),
            "estr_licence": ("estr_page", "The ECB does not charge for the €STR or license its use."),
            "copyright": _COPYRIGHT_Q,
        },
    },
    "key_rates": {
        **_BASE,
        "terms_quote": _COPYRIGHT_Q[1],
        "publication": "level in force on each calendar day (ECB series 'date of changes'); decisions announced by "
        "the ECB before their effective date",
        "pub_lag": "not_stated",
        "revision_policy": "not stated by provider; value as fetched (as_of)",
        "policy_quotes": {"copyright": _COPYRIGHT_Q, "modified": _MODIFIED_Q},
    },
}

#: ECB codelist labels (``data-api.ecb.europa.eu/service/codelist/ECB/CL_UNIT`` and ``CL_UNIT_MULT``, read
#: 2026-10-17). A code the data uses that is not listed fails the table (a unit is never guessed).
UNIT_LABELS = {"PC": "Percent", "PCPA": "Percent per annum", "EUR": "Euro", "_Z": "Not applicable"}
UNIT_MULT_LABELS = {"0": "Units", "6": "Millions"}
#: ECB observation status codes seen in these series (CL_OBS_STATUS); others are kept verbatim and flagged.
OBS_STATUS_NORMAL = "A"


def code_digest() -> str:
    return hc.code_digest(sys.modules[__name__])


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="European Central Bank (euro reference rates file, ECB Data Portal SDMX API)",
        licence=LICENCE,
        allowed_hosts=HOSTS,
        datasets=DATASETS,
        dataset_ids=("ECB:eurofxref-hist", f"ECB:EST/{ESTR_KEY}", f"ECB:FM/{KR_KEY}"),
        cadence_seconds=hc.DAY,
        planned_requests=len(FILES),
        max_response_bytes=60_000_000,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"geo": "provider_geo U2 (euro area), iso3 null", "tables": [f"ecb.{d}" for d in DATASETS]},
        notes="daily: euro reference FX rates (zip), €STR and key ECB rates (SDMX-CSV); ~15 MB per full run",
    )


# --------------------------------------------------------------------------- parsers
def parse_fx(f: hc.Fetched, rows: hc.DailyRows, report: dict[str, Any]) -> None:
    try:
        z = zipfile.ZipFile(io.BytesIO(f.body))
    except zipfile.BadZipFile as e:
        raise hc.err(f"fx_ref: not a zip ({e})") from e
    if z.namelist() != [FX_MEMBER]:
        raise hc.err(f"fx_ref: unexpected zip members {z.namelist()[:5]}")
    info = z.getinfo(FX_MEMBER)
    if info.file_size > 50_000_000:
        raise hc.err("fx_ref: member too large", kind="too_large")
    text = z.read(FX_MEMBER).decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header or header[0] != "Date":
        raise hc.err("fx_ref: header must start with Date")
    cur = [h.strip() for h in header[1:]]
    while cur and cur[-1] == "":
        cur.pop()  # the file ends every line with a comma
    if not cur or any(len(c) != 3 or not c.isalpha() or not c.isupper() for c in cur):
        raise hc.err(f"fx_ref: currency headers are not ISO 4217 codes: {cur[:10]}")
    rights = RIGHTS["fx_ref"]
    flags = rows.flags(**hc.base_flags(rights, f, base_currency="EUR"))
    flags_norm = rows.flags(**hc.base_flags(rights, f, base_currency="EUR", value_normalised=True))
    na = Counter()
    for i, rec in enumerate(reader, start=2):
        if not rec or not rec[0].strip():
            continue
        try:
            d = date.fromisoformat(rec[0].strip())
        except ValueError as e:
            raise hc.err(f"fx_ref: line {i}: bad date {rec[0]!r}") from e
        for j, c in enumerate(cur, start=1):
            cell = rec[j].strip() if j < len(rec) else ""
            v, normalised = hc.number(cell)
            if v is None:
                na[cell or "<empty>"] += 1
                continue
            rows.add(indicator=f"EUR_REF|currency={c}", provider_geo=GEO, day=d, value=v, unit=f"{c} per EUR",
                     flags=flags_norm if normalised else flags, raw_sha256=f.sha256,
                     selector={"file": FX_MEMBER, "line": i, "column": c})  # fmt: skip
    report["fx_ref"] = {"currencies": len(cur), "not_quoted_cells": dict(na)}


def parse_sdmx_csv(ds: str, f: hc.Fetched, rows: hc.DailyRows, report: dict[str, Any]) -> None:
    """ECB Data Portal SDMX-CSV (``format=csvdata``): one row per observation with series attributes."""
    reader = csv.DictReader(io.StringIO(f.text("utf-8-sig")))
    need = {"KEY", "TIME_PERIOD", "OBS_VALUE", "OBS_STATUS", "TITLE", "UNIT_MULT"}
    if not reader.fieldnames or not need <= set(reader.fieldnames):
        raise hc.err(f"{ds}: SDMX-CSV columns missing: {sorted(need - set(reader.fieldnames or ()))}")
    unit_col = "UNIT_MEASURE" if "UNIT_MEASURE" in reader.fieldnames else "UNIT"
    if unit_col not in reader.fieldnames:
        raise hc.err(f"{ds}: no unit column")
    rights = RIGHTS[ds]
    empty = Counter()
    series = Counter()
    for i, r in enumerate(reader, start=2):
        key = r["KEY"].strip()
        if r.get("FREQ") not in ("D", "B"):
            raise hc.err(f"{ds}: line {i}: frequency {r.get('FREQ')!r} is not daily/business-daily")
        try:
            d = date.fromisoformat(r["TIME_PERIOD"].strip())
        except ValueError as e:
            raise hc.err(f"{ds}: line {i}: bad TIME_PERIOD {r['TIME_PERIOD']!r}") from e
        v, normalised = hc.number(r["OBS_VALUE"])
        if v is None:
            empty[key] += 1
            continue
        ucode = r[unit_col].strip()
        mult = r["UNIT_MULT"].strip()
        if ucode not in UNIT_LABELS or mult not in UNIT_MULT_LABELS:
            raise hc.err(f"{ds}: unit {ucode!r} / multiplier {mult!r} not in the quoted ECB codelists", kind="unit")
        status = r["OBS_STATUS"].strip()
        conf = (r.get("CONF_STATUS") or r.get("OBS_CONF") or "").strip()
        extra: dict[str, Any] = {
            "series_title": r["TITLE"].strip(),
            "unit_mult": f"{mult}: {UNIT_MULT_LABELS[mult]}",
            "frequency": r["FREQ"],
        }
        if status != OBS_STATUS_NORMAL:
            extra["obs_status"] = status
        if conf and conf != "F":
            extra["confidentiality"] = conf
        if normalised:
            extra["value_normalised"] = True
        rows.add(indicator=key, provider_geo=GEO, day=d, value=v, unit=f"{UNIT_LABELS[ucode]} ({ucode})",
                 flags=rows.flags(**hc.base_flags(rights, f, **extra)), raw_sha256=f.sha256,
                 selector={"file": f.key, "line": i, "key": key})  # fmt: skip
        series[key] += 1
    report[ds] = {"series": dict(series), "empty_values": dict(empty)}


def build(files: Mapping[str, hc.Fetched], wanted: tuple[str, ...] = DATASETS) -> tuple[dict[str, hc.TableBuild], dict]:
    out = {ds: hc.TableBuild(hc.DailyRows(ATLAS_SOURCE, ds)) for ds in wanted}
    report: dict[str, Any] = {}
    if "fx_ref" in wanted:
        parse_fx(files["fx_zip"], out["fx_ref"].rows, report)
    if "estr" in wanted:
        parse_sdmx_csv("estr", files["estr_csv"], out["estr"].rows, report)
    if "key_rates" in wanted:
        parse_sdmx_csv("key_rates", files["kr_csv"], out["key_rates"].rows, report)
    report["policy_quotes_found"] = hc.check_policy_pages(files, FILES, RIGHTS, wanted)
    return out, report


NOTES = {
    "fx_ref": "ECB euro foreign exchange reference rates, daily since 1999-01-04 (units of currency per 1 EUR); N/A "
    "cells (currency not quoted, e.g. legacy currencies after euro adoption) are no row. provider_geo U2 = euro area, "
    "not a country. Reference rates are for information purposes only (ECB). flags.as_of = fetch date.",
    "estr": "Euro short-term rate (€STR) and its published statistics, one row per trading day (period = trading day; "
    "published 08:00 CET the next TARGET2 business day). TT is in millions of euro (flags.unit_mult, not applied).",
    "key_rates": "Key ECB interest rates: level in force on each calendar day (ECB 'date of changes' series). "
    "MRR_FR fixed-rate tenders and MRR_MBR variable-rate minimum bid rate cover different periods.",
}


async def atlas_ingest(run: Any) -> dict[str, Any]:
    wanted = tuple(d for d in DATASETS if run.wants(d))
    files = await hc.fetch_all(run, FILES, wanted)
    builds, report = build(files, wanted)
    tables = hc.register_tables(run, builds, rights=RIGHTS, notes=NOTES, source_key=SOURCE_KEY)
    return {"ok": True, "tables": tables, "report": report}


__all__ = ["DATASETS", "FILES", "NAME", "RIGHTS", "atlas_ingest", "atlas_source", "build"]
