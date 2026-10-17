"""Human mobility layer (agent V6MIGR): asylum flows, monthly EU asylum applicants, migrant deaths, internal displacement.

Extends -- never duplicates -- what is loaded already: UN DESA International Migrant Stock 2024 (``un_desa.ims2024``) and
the UNHCR population stocks (``unhcr.pop_bilateral`` / ``pop_by_asylum`` / ``pop_by_origin``) of :mod:`atlas_un`, the
IDMC disaster events and their attributed displacements (KG + causal claims) of :mod:`atlas_idmc`, and the World Bank
remittance inflows / outflows of WDI (``wb.wdi_econ`` ``BX.TRF.PWKR.CD.DT`` / ``BM.TRF.PWKR.CD.DT``).

=========================  =========================  ===========================================================
connector                  source_key                 atlas tables (``<source>.<dataset>``)
=========================  =========================  ===========================================================
``atlas.unhcr_asylum``     ``unhcr.asylum``           ``unhcr.asylum_applications`` (claims lodged, origin x asylum x
                                                      year x authority x application type x stage x cases/persons,
                                                      2000-), ``unhcr.asylum_decisions`` (recognised, complementary
                                                      protection, rejected, otherwise closed, total)
``atlas.eurostat_asylum``  ``eurostat.dissemination``  ``eurostat.asylum_applicants_m`` (Eurostat ``migr_asyappctzm``:
                                                      asylum applicants by citizenship x reporting country x month,
                                                      2008-01-, all applicants and first-time applicants, both sexes,
                                                      all ages)
``atlas.iom_mmp``          ``iom.missing_migrants``    ``iom.mmp_monthly`` (IOM Missing Migrants Project: dead and
                                                      missing migrants and incident records by country of incident /
                                                      MMP region x month x cause of death, 2014-)
``atlas.idmc_national``    ``idmc.gidd``              ``idmc.gidd_national`` (IDMC GIDD: conflict / disaster internal
                                                      displacements and IDP stocks by country x year)
=========================  =========================  ===========================================================

Honesty rules applied here
--------------------------
* Values as reported. UNHCR's asylum download (``download=true``, a ZIP of CSV) writes the cells the JSON API renders
  as the *string* ``"0"`` (no figure) as ``0``: for every year checked (2024 in full, and all-years totals) the CSV
  ``0`` cells are exactly the JSON string-``"0"`` cells, so a ``0`` is omitted and counted, never stored as zero. The
  download is checked against the JSON API's own record count and column totals (one ``limit=1`` request per
  endpoint); a mismatch refuses the table.
* Bilateral tables put the reporting side in ``provider_geo`` (UNHCR: country of asylum; Eurostat: reporting country)
  and fold the counterpart into the indicator (``applied|origin=AFG|...``, ``asylum_applicants|...|citizen=SY``);
  ``flags`` carry the counterpart's provider code, crosswalk key (or null) and label (UNHCR) for the LINKS layer.
* Eurostat cells ``:`` (not available) are not stored (counted by flag); reported zeros are kept; Eurostat flags are
  kept with the label of Eurostat's OBS_FLAG code list (archived with the run).
* IOM MMP publishes one record per incident. ``iom.mmp_monthly`` cells are **sums / counts of provider records
  computed by REVA** (``flags.aggregation``), per country of incident (``provider_geo`` = MMP's country name verbatim,
  mapped only through the exact-name aliases of ``registry/atlas/geo_aliases/v6migr.yaml``, scheme ``iom_mmp``) or MMP
  region (``mmp_region:<name>``, unmapped), per month of MMP's ``reported_date`` (codebook: "Estimated date of death";
  "cumulative total" records carry the latest date of their range) and per MMP ``cause_death`` category (cause of
  *death*, not of migration: never a causal claim). Every record is counted as published (fully identical records are
  listed in the coverage note, not dropped); the selector lists the CSV rows of each cell.
* IDMC national figures: both the rounded and the raw (not rounded) columns are kept as separate indicators; the
  README definitions quoted in the catalog must be present verbatim in the archived workbook, else the table is
  refused. IDMC codes the trigger (conflict and violence / disasters) of each figure; REVA stores it as a dimension and
  makes **no new causal claim** (IDMC's event-level disaster claims are :mod:`atlas_idmc`'s). Licence CC BY-NC 4.0 per
  the workbook README (non-commercial; HDX's record says CC BY: the more restrictive statement applies).
* Not loaded (see ``docs/dev/v6_migration.md``): the World Bank/KNOMAD bilateral remittance matrix (KNOMAD closed in
  2024; its files no longer resolve on knomad.org and the CGD 2024 re-run answers 403 to non-browser clients; both are
  *modelled allocations* of aggregate inflows over migrant stocks, not observed flows) -- an owner decision.

Live run: ``uv run reva atlas ingest atlas.unhcr_asylum`` (also ``atlas.eurostat_asylum``, ``atlas.iom_mmp``,
``atlas.idmc_national``); fixtures: ``--fixture``.
"""

from __future__ import annotations

import csv
import hashlib
import inspect
import io
import json
import re
import sys
import zipfile
import zlib
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from xml.etree import ElementTree as ET

import polars as pl

from reva.kernel.errors import SourceResponseError

VERSION = "1.0.0"
PARSER_VERSION = "atlas_migration/1"
MAX_TEXT = 4000


def _err(msg: str, kind: str = "schema_drift", retryable: bool = False, **details: Any) -> SourceResponseError:
    return SourceResponseError(msg, retryable=retryable, kind=kind, **details)


def canon(obj: Mapping[str, Any]) -> str:
    return json.dumps(dict(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


_STRICT = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")
_THOUSANDS = re.compile(r"^[1-9][0-9]{0,2}(,[0-9]{3})+$")


def count_text(text: str, what: str) -> tuple[str, str | None]:
    """A provider count as a strict decimal: ``("1022", "1,022")`` for a thousands-separated spelling (the original
    text returned to be kept), ``("12", None)`` when already strict. Anything else is refused (schema drift)."""
    t = text.strip()
    if _STRICT.match(t) and not t.startswith("-"):
        return t, None
    if _THOUSANDS.match(t):
        return t.replace(",", ""), t
    raise _err(f"{what}: {t[:30]!r} is not a count")


def module_digest() -> str:
    return "sha256:" + hashlib.sha256(inspect.getsource(sys.modules[__name__]).encode()).hexdigest()


@dataclass
class Rows:
    """Column buffers of one atlas table (``reva.atlas.tables.ATLAS_SCHEMA``)."""

    source: str
    dataset: str
    cols: dict[str, list[Any]] = field(
        default_factory=lambda: {
            c: []
            for c in (
                "indicator",
                "concept_id",
                "iso3",
                "provider_geo",
                "period_start",
                "period_end",
                "value",
                "unit",
                "flags",
                "raw_sha256",
                "selector",
            )
        }
    )

    def add(
        self,
        *,
        indicator: str,
        provider_geo: str,
        iso3: str | None,
        period: tuple[date, date],
        value: str,
        unit: str,
        flags: Mapping[str, Any] | str,
        raw_sha256: str,
        selector: Mapping[str, Any] | str,
        concept_id: str | None = None,
    ) -> None:
        if len(indicator) > 200:
            raise _err("indicator longer than 200 characters", indicator=indicator[:80])
        c = self.cols
        c["indicator"].append(indicator)
        c["concept_id"].append(concept_id)
        c["iso3"].append(iso3)
        c["provider_geo"].append(provider_geo)
        c["period_start"].append(period[0])
        c["period_end"].append(period[1])
        c["value"].append(value)
        c["unit"].append(unit)
        c["flags"].append(flags if isinstance(flags, str) else canon(flags))
        c["raw_sha256"].append(raw_sha256)
        c["selector"].append(selector if isinstance(selector, str) else canon(selector))

    def __len__(self) -> int:
        return len(self.cols["indicator"])

    def frame(self) -> pl.DataFrame:
        from reva.atlas.tables import ATLAS_SCHEMA

        n = len(self)
        df = pl.DataFrame(
            {
                "source": [self.source] * n,
                "dataset": [self.dataset] * n,
                **{k: v for k, v in self.cols.items()},
                "entity_qid": [None] * n,
                "value_text": [None] * n,
            },
            schema={
                "source": pl.String,
                "dataset": pl.String,
                "indicator": pl.String,
                "concept_id": pl.String,
                "iso3": pl.String,
                "provider_geo": pl.String,
                "period_start": pl.Date,
                "period_end": pl.Date,
                "value": pl.String,
                "unit": pl.String,
                "flags": pl.String,
                "raw_sha256": pl.String,
                "selector": pl.String,
                "entity_qid": pl.String,
                "value_text": pl.String,
            },
        ).select(ATLAS_SCHEMA.names())
        dup = df.select(pl.struct("indicator", "provider_geo", "period_start", "period_end").is_duplicated().sum()).item()
        if dup:
            raise _err(f"{self.source}.{self.dataset}: {dup} duplicate cells")
        return df


def year_period(y: int) -> tuple[date, date]:
    return date(y, 1, 1), date(y + 1, 1, 1)


def month_period(y: int, m: int) -> tuple[date, date]:
    return date(y, m, 1), (date(y, m + 1, 1) if m < 12 else date(y + 1, 1, 1))


def _iso3(code: str | None, scheme: str) -> str | None:
    from reva.atlas.tables import country_iso3

    return country_iso3(code, scheme) if code else None


# ============================================================================= UNHCR asylum
UNHCR_HOST = "api.unhcr.org"
UNHCR_API = f"https://{UNHCR_HOST}/population/v1/"
UNHCR_SOURCE_KEY = "unhcr.asylum"
UNHCR_LICENCE = "CC BY 4.0 (UNHCR Refugee Data Finder; pending maintainer review, see registry/sources/unhcr_asylum.yaml)"
UNHCR_RIGHTS: dict[str, Any] = {
    "licence": UNHCR_LICENCE,
    "licence_url": "https://creativecommons.org/licenses/by/4.0/",
    "attribution": "UNHCR Refugee Data Finder, https://www.unhcr.org/refugee-statistics/",
    "source_key": UNHCR_SOURCE_KEY,
}
#: field definitions quoted verbatim from the API documentation (https://api.unhcr.org/docs/refugee-statistics.html,
#: read 2026-10-17); a code not listed there keeps its code and "not labelled in the API documentation"
UNHCR_DEFINITIONS = {
    "procedure_type": "The procedure type describes the authority with whom the asylum claim was lodged. It  can be "
    "one of: **G – Government, J - Joint, U – UNHCR**.",
    "app_type": "The application type describes the stage of the procedure.  Common values are N (New), R (Repeat) "
    "and A (Appeal).  For the full list see the data content description on refugee-statistics. ",
    "dec_level": "The decision level provides more detail of the stage of the procedure.  Common values are NA (New "
    "applications), RA (repeat/reopened applications) and AR (administrative review).  For the full list, see the "
    "data content description on refugee-statistics.",
    "app_pc": "The data type describes whether the data provided reflects cases or persons.  The most common situation "
    "is for the data on asylum applications lodged to reflect persons, but in certain situations (e.g USA), the data "
    "provided by governments is at the level of cases.  A case can contain information on one or more persons. See "
    "also the data content description on refugee-statistics. ",
    "applied": "The total number of asylum claims for the given dataset.",
    "dec_recognized": "The number of asylum applicants recognised as refugees under UNHCR’s mandate.",
    "dec_other": "The number of asylum applicants recognised as refugees under other forms of complementary protection.",
    "dec_rejected": "The number of asylum applicants whose asylum claims have been rejected.",
    "dec_closed": "The number of asylum applicants whose asylum claims have been otherwise closed.",
    "dec_total": "The total number of asylum applicants for which a decision on their asylum claim has been made in "
    "the given reference period.",
}
AUTHORITY_LABELS = {"G": "Government", "J": "Joint", "U": "UNHCR"}
APP_TYPE_LABELS = {"N": "New", "R": "Repeat", "A": "Appeal"}
STAGE_LABELS = {"NA": "New applications", "RA": "repeat/reopened applications", "AR": "administrative review"}
COUNT_UNITS = {"P": "persons", "C": "cases"}


@dataclass(frozen=True)
class UnhcrEndpoint:
    dataset: str
    endpoint: str
    csv_name: str
    #: CSV column -> (atlas indicator name, JSON field of the API's ``total``)
    values: tuple[tuple[str, str, str], ...]
    dims: tuple[tuple[str, str], ...]  # (CSV column, indicator dimension)
    time_reference: str

    def download_url(self) -> str:
        return f"{UNHCR_API}{self.endpoint}/?limit=1000000&coo_all=true&coa_all=true&download=true"

    def check_url(self) -> str:
        return f"{UNHCR_API}{self.endpoint}/?limit=1&coo_all=true&coa_all=true"


UNHCR_ENDPOINTS = (
    UnhcrEndpoint(
        "asylum_applications",
        "asylum-applications",
        "asylum-applications.csv",
        (("applied", "applied", "applied"),),
        (("Authority", "authority"), ("Application type", "app_type"), ("Stage of procedure", "stage"),
         ("Cases / Persons", "count")),
        "asylum claims lodged during the calendar year (flow)",
    ),
    UnhcrEndpoint(
        "asylum_decisions",
        "asylum-decisions",
        "asylum-decisions.csv",
        (
            ("Recognized decisions", "dec_recognized", "dec_recognized"),
            ("Complementary protection", "dec_other", "dec_other"),
            ("Rejected decisions", "dec_rejected", "dec_rejected"),
            ("Otherwise closed", "dec_closed", "dec_closed"),
            ("Total decisions", "dec_total", "dec_total"),
        ),
        (("Authority", "authority"), ("Stage of procedure", "stage"), ("Cases / Persons", "count")),
        "decisions taken during the calendar year (flow)",
    ),
)
UNHCR_ID_COLUMNS = ("Year", "Country of origin", "Country of origin (ISO)", "Country of asylum", "Country of asylum (ISO)")


def read_zip_member(body: bytes, name: str, limit: int = 200_000_000) -> bytes:
    try:
        z = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as e:
        raise _err("UNHCR download is not a ZIP archive", kind="decoding_error") from e
    infos = {i.filename: i for i in z.infolist()}
    if name not in infos:
        raise _err(f"UNHCR download without {name}", members=sorted(infos))
    if infos[name].file_size > limit:
        raise _err(f"{name}: {infos[name].file_size} bytes exceed the cap", kind="decompression_limit")
    return z.read(name)


def unhcr_check_totals(body: bytes, ep: UnhcrEndpoint) -> tuple[int, dict[str, int]]:
    """``(record count, column totals)`` of the JSON API answer to ``check_url`` (``maxPages`` at ``limit=1``)."""
    try:
        doc = json.loads(body.decode("utf-8"))
        n = int(doc["maxPages"])
        totals = {k: int(v) for k, v in (doc.get("total") or {}).items()}
    except (UnicodeDecodeError, ValueError, KeyError, TypeError) as e:
        raise _err(f"UNHCR {ep.endpoint}: check response is not the expected JSON") from e
    return n, totals


def build_unhcr_asylum(
    ep: UnhcrEndpoint, body: bytes, sha: str, check: tuple[int, dict[str, int]] | None
) -> tuple[pl.DataFrame, dict[str, Any]]:
    """One UNHCR asylum download (ZIP of CSV) -> atlas frame + report. ``check`` = the JSON API's (records, totals)."""
    text = read_zip_member(body, ep.csv_name).decode("utf-8-sig")
    rdr = csv.reader(io.StringIO(text, newline=""))
    header = next(rdr, None)
    want = [*UNHCR_ID_COLUMNS, *(c for c, _ in ep.dims), *(c for c, _, _ in ep.values)]
    if header is None or [h.strip() for h in header] != want:
        raise _err(f"UNHCR {ep.endpoint}: CSV header changed", header=header, expected=want)
    rows = Rows("unhcr", ep.dataset)
    stats: Counter[str] = Counter()
    sums: Counter[str] = Counter()
    years: set[int] = set()
    unlabelled: dict[str, set[str]] = defaultdict(set)
    records = 0
    seen: set[tuple[str, ...]] = set()
    for rnum, rec in enumerate(rdr, start=2):
        if not any(x.strip() for x in rec):
            continue
        if len(rec) != len(want):
            raise _err(f"UNHCR {ep.endpoint} row {rnum}: {len(rec)} fields")
        records += 1
        r = dict(zip(want, (x.strip() for x in rec), strict=True))
        try:
            year = int(r["Year"])
        except ValueError as e:
            raise _err(f"UNHCR {ep.endpoint} row {rnum}: year {r['Year']!r}") from e
        years.add(year)
        coo, coa = r["Country of origin (ISO)"], r["Country of asylum (ISO)"]
        if not coo or not coa:
            raise _err(f"UNHCR {ep.endpoint} row {rnum}: origin or asylum code missing")
        dims = [(d, r[c]) for c, d in ep.dims]
        key = (str(year), coo, coa, *(v for _, v in dims))
        if key in seen:
            raise _err(f"UNHCR {ep.endpoint}: duplicate record {key}")
        seen.add(key)
        for d, v in dims:
            lab = {"authority": AUTHORITY_LABELS, "app_type": APP_TYPE_LABELS, "stage": STAGE_LABELS}.get(d)
            if lab is not None and v not in lab:
                unlabelled[d].add(v)
        count = dict(dims)["count"]
        if count not in COUNT_UNITS:
            raise _err(f"UNHCR {ep.endpoint} row {rnum}: unknown Cases / Persons code {count!r}")
        iso3 = _iso3(coa, "iso3")
        for col, name, total_key in ep.values:
            raw = r[col]
            if raw == "":
                stats[f"{name}_empty_omitted"] += 1
                continue
            value, spelled = count_text(raw, f"UNHCR {ep.endpoint} row {rnum} {col}")
            sums[total_key] += int(value)
            if value == "0":
                stats[f"{name}_no_figure_omitted"] += 1  # the API's string "0": no figure, never a zero
                continue
            flags: dict[str, Any] = {
                "time_reference": ep.time_reference,
                "geo_label": r["Country of asylum"],
                "counterpart_role": "origin",
                "counterpart_provider_geo": coo,
                "counterpart_iso3": _iso3(coo, "iso3"),
                "counterpart_label": r["Country of origin"],
            }
            if spelled:
                flags["provider_text"] = spelled
            rows.add(
                indicator=f"{name}|origin={coo}" + "".join(f"|{d}={v}" for d, v in dims),
                provider_geo=coa,
                iso3=iso3,
                period=year_period(year),
                value=value,
                unit=COUNT_UNITS[count],
                flags=flags,
                raw_sha256=sha,
                selector={"file": ep.csv_name, "row": rnum, "column": col},
            )
    if check is not None:
        n, totals = check
        bad = {k: (sums.get(k, 0), v) for k, v in totals.items() if sums.get(k, 0) != v}
        if n != records or bad:
            raise _err(
                f"UNHCR {ep.endpoint}: download disagrees with the API (records {records} vs {n})",
                kind="incomplete",
                totals=bad,
            )
    if not len(rows):
        raise _err(f"UNHCR {ep.endpoint}: no cell with a figure")
    report = {
        "records": records,
        "cells": len(rows),
        "years": [min(years), max(years)],
        "checked_against_api": check is not None,
        "column_totals": dict(sums),
        "unlabelled_codes": {k: sorted(v) for k, v in sorted(unlabelled.items())},
        **dict(stats),
    }
    return rows.frame(), report


def unhcr_note(ep: UnhcrEndpoint, rep: Mapping[str, Any]) -> str:
    defs = "; ".join(
        f"{k}: '{UNHCR_DEFINITIONS[k]}'"
        for k in ("procedure_type", "app_type", "dec_level", "app_pc", *(n for _, n, _ in ep.values))
        if k in UNHCR_DEFINITIONS
    )
    return (
        f"UNHCR {ep.endpoint} (API download {ep.download_url()}), {rep['records']} records, years "
        f"{rep['years'][0]}-{rep['years'][1]}; provider_geo = country of asylum ISO code, origin folded into the "
        "indicator (UNK unknown, XXA stateless, TIB Tibetan kept unmapped). Dimensions: authority = procedure_type, "
        "app_type, stage = decision level, count = cases (C) or persons (P), the unit says which. Cells written 0 (the "
        "API's string '0', no figure) are omitted, never stored as zero; download checked against the API's record "
        f"count and totals: {rep['checked_against_api']}. Codes without a documented label: {rep['unlabelled_codes']}. "
        f"Definitions (API documentation, verbatim): {defs}. Licence: {UNHCR_LICENCE}."
    )[:MAX_TEXT]


# ============================================================================= Eurostat monthly asylum applicants
ES_HOST = "ec.europa.eu"
ES_DATASET_CODE = "migr_asyappctzm"
ES_TSV_URL = (
    f"https://{ES_HOST}/eurostat/api/dissemination/sdmx/2.1/data/{ES_DATASET_CODE}?format=TSV&compressed=true"
)
ES_FLAGS_URL = f"https://{ES_HOST}/eurostat/api/dissemination/sdmx/2.1/codelist/ESTAT/OBS_FLAG/latest"
ES_DATASET = "asylum_applicants_m"
ES_KEY_HEADER = "freq,unit,citizen,sex,applicant,age,geo\\TIME_PERIOD"
ES_KEEP = {"freq": {"M"}, "unit": {"PER"}, "sex": {"T"}, "age": {"TOTAL"}, "applicant": {"TOTAL", "FRST"}}
ES_LICENCE = "Eurostat reuse policy (Commission Decision 2011/833/EU; CC BY 4.0), acknowledgement required"
ES_RIGHTS: dict[str, Any] = {
    "licence": ES_LICENCE,
    "licence_url": "https://ec.europa.eu/eurostat/about-us/policies/copyright",
    "attribution": f"Source: Eurostat ({ES_DATASET_CODE})",
    "source_key": "eurostat.dissemination",
}
ES_APPLICANT_LABELS = {"TOTAL": "Total", "FRST": "First time applicant"}  # Eurostat code list APPLICANT
ES_MAX_UNCOMPRESSED = 1_500_000_000


def gunzip_lines(body: bytes, limit: int = ES_MAX_UNCOMPRESSED) -> Iterator[str]:
    """Lines of a gzip body, decompressed incrementally (bounded, truncation refused)."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    total, buf, pos = 0, b"", 0
    step = 1 << 20
    while pos < len(body) or d.unconsumed_tail:
        chunk = d.decompress(d.unconsumed_tail or body[pos : pos + step], step)
        if not d.unconsumed_tail:
            pos += step
        total += len(chunk)
        if total > limit:
            raise _err(f"decompressed body exceeds {limit} bytes", kind="decompression_limit")
        buf += chunk
        *lines, buf = buf.split(b"\n")
        for ln in lines:
            yield ln.decode("utf-8")
        if d.eof:
            break
    if not d.eof:
        raise _err("truncated gzip body", kind="decoding_error", retryable=True)
    buf += d.flush()
    if buf:
        yield buf.decode("utf-8")


def parse_obs_flags(body: bytes) -> dict[str, str]:
    """Eurostat OBS_FLAG code list (SDMX 2.1 structure XML) -> {code: English name}."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise _err("Eurostat OBS_FLAG code list is not XML") from e
    s = "{http://www.sdmx.org/resources/sdmxml/schemas/v2_1/structure}"
    c = "{http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common}"
    lang = "{http://www.w3.org/XML/1998/namespace}lang"
    out = {}
    for code in root.iter(f"{s}Code"):
        names = [n.text or "" for n in code.findall(f"{c}Name") if n.get(lang) == "en"]
        if code.get("id") and names:
            out[str(code.get("id"))] = names[0]
    if not out:
        raise _err("Eurostat OBS_FLAG code list holds no code")
    return out


_ES_CELL = re.compile(r"^(?P<v>:|-?[0-9]+(\.[0-9]+)?)(\s+(?P<f>[a-z]+))?$")


def build_eurostat_asylum(body: bytes, sha: str, flag_labels: Mapping[str, str]) -> tuple[pl.DataFrame, dict[str, Any]]:
    lines = gunzip_lines(body)
    header = next(lines, "").rstrip("\r").split("\t")
    if not header or header[0].strip() != ES_KEY_HEADER:
        raise _err(f"Eurostat {ES_DATASET_CODE}: TSV key header changed", header=header[:1])
    months: list[tuple[str, tuple[date, date]]] = []
    for h in header[1:]:
        t = h.strip()
        m = re.fullmatch(r"(\d{4})-(\d{2})", t)
        if not m:
            raise _err(f"Eurostat {ES_DATASET_CODE}: period column {t!r} is not monthly")
        months.append((t, month_period(int(m.group(1)), int(m.group(2)))))
    keys = ES_KEY_HEADER.split("\\")[0].split(",")
    rows = Rows("eurostat", ES_DATASET)
    stats: Counter[str] = Counter()
    flagged: Counter[str] = Counter()
    geos: set[str] = set()
    citizens: set[str] = set()
    zeros = 0
    series = 0
    flags_cache: dict[tuple[str, str], str] = {}
    for lnum, line in enumerate(lines, start=2):
        if not line.strip():
            continue
        parts = line.rstrip("\r").split("\t")
        k = dict(zip(keys, parts[0].split(","), strict=True))
        if any(k[d] not in allowed for d, allowed in ES_KEEP.items()):
            stats["series_outside_selection"] += 1
            continue
        if len(parts) != len(months) + 1:
            raise _err(f"Eurostat {ES_DATASET_CODE} line {lnum}: {len(parts) - 1} periods, expected {len(months)}")
        series += 1
        geo, cit, app = k["geo"], k["citizen"], k["applicant"]
        geos.add(geo)
        citizens.add(cit)
        iso3 = _iso3(geo, "eurostat")
        indicator = f"asylum_applicants|applicant={app}|citizen={cit}"
        for (period_code, period), cell in zip(months, parts[1:], strict=True):
            m = _ES_CELL.match(cell.strip())
            if not m:
                raise _err(f"Eurostat {ES_DATASET_CODE} line {lnum} {period_code}: cell {cell.strip()[:20]!r}")
            v, f = m.group("v"), m.group("f") or ""
            if v == ":":
                stats[f"not_available{('_' + f) if f else ''}"] += 1
                continue
            if v == "0":
                zeros += 1
            fl_key = (cit, f)
            fl = flags_cache.get(fl_key)
            if fl is None:
                fd: dict[str, Any] = {
                    "counterpart_role": "citizenship",
                    "counterpart_provider_geo": cit,
                    "counterpart_iso3": _iso3(cit, "eurostat"),
                }
                if f:
                    fd["provider_flag"] = f
                    fd["provider_flag_label"] = flag_labels.get(f, "(code not in the OBS_FLAG code list)")
                fl = flags_cache[fl_key] = canon(fd)
            if f:
                flagged[f] += 1
            rows.add(
                indicator=indicator,
                provider_geo=geo,
                iso3=iso3,
                period=period,
                value=v,
                unit="persons",
                flags=fl,
                raw_sha256=sha,
                selector=f'{{"line":{lnum},"period":"{period_code}"}}',
                concept_id=_es_concept(app, cit),
            )
    if not len(rows):
        raise _err(f"Eurostat {ES_DATASET_CODE}: no cell in the selection")
    rep = {
        "series": series,
        "cells": len(rows),
        "reported_zeros": zeros,
        "flagged": dict(flagged),
        "no_value": dict(stats),
        "periods": [months[0][0], months[-1][0]],
        "reporting_geos": len(geos),
        "citizenships": len(citizens),
        "unmapped_citizen_codes": sorted(c for c in citizens if _iso3(c, "eurostat") is None),
    }
    return rows.frame(), rep


def _es_concept(app: str, cit: str) -> str | None:
    if cit != "TOTAL":
        return None
    return {"TOTAL": "migration.eurostat.asylum_applicants_monthly", "FRST": "migration.eurostat.first_time_asylum_applicants_monthly"}.get(app)


def eurostat_note(rep: Mapping[str, Any]) -> str:
    return (
        f"Eurostat {ES_DATASET_CODE} 'Asylum applicants by type, citizenship, sex and age - monthly data' (bulk TSV "
        f"{ES_TSV_URL}); selection unit=PER (persons), sex=T, age=TOTAL, applicant TOTAL / FRST "
        f"({ES_APPLICANT_LABELS}); {rep['series']} series, {rep['cells']} cells, months {rep['periods']}. provider_geo = "
        "reporting country (Eurostat code, scheme eurostat; EU27_2020 kept unmapped), citizenship folded into the "
        "indicator and repeated in flags.counterpart_* (aggregates such as TOTAL, EXT_EU27_2020, STLS stateless, UNK "
        f"kept with counterpart_iso3 null: {rep['unmapped_citizen_codes'][:40]}). Cells ':' are not stored "
        f"({rep['no_value']}); reported zeros kept ({rep['reported_zeros']}); Eurostat flags kept with their OBS_FLAG "
        f"label ({rep['flagged']}). Licence: {ES_LICENCE}."
    )[:MAX_TEXT]


# ============================================================================= IOM Missing Migrants Project
HDX_HOST = "data.humdata.org"
S3_HOST = "s3.us-east-1.amazonaws.com"  # HDX resource downloads answer 302 -> its S3 file store
MMP_PACKAGE = "missing-migrants-project-data"
MMP_PACKAGE_URL = f"https://{HDX_HOST}/api/3/action/package_show?id={MMP_PACKAGE}"
MMP_RESOURCE = "iom-missing-migrants-project-data.csv"
MMP_ORG = "iom-missing-migrants-project"
MMP_SOURCE_KEY = "iom.missing_migrants"
MMP_DATASET = "mmp_monthly"
MMP_SCHEME = "iom_mmp"
MMP_LICENCE = "CC BY-IGO (HDX dataset record of the IOM Missing Migrants Project; pending maintainer review)"
MMP_RIGHTS: dict[str, Any] = {
    "licence": MMP_LICENCE,
    "licence_url": "https://creativecommons.org/licenses/by/3.0/igo/",
    "attribution": "IOM Missing Migrants Project, https://missingmigrants.iom.int (via HDX)",
    "source_key": MMP_SOURCE_KEY,
}
MMP_COLUMNS = ("web_id", "region", "reported_date", "total_dead_and_missing", "cause_death", "country_of_incident")
MMP_AGG_DEAD = "sum of total_dead_and_missing over the MMP incident records of this cell (computed by REVA)"
MMP_AGG_COUNT = "count of MMP incident records of this cell (computed by REVA)"


def hdx_resource_url(body: bytes, package: str, org: str, resource: str) -> tuple[str, dict[str, Any]]:
    try:
        res = json.loads(body.decode("utf-8"))["result"]
        porg = res["organization"]["name"]
    except (UnicodeDecodeError, ValueError, KeyError, TypeError) as e:
        raise _err("HDX package_show: not a package record") from e
    if res.get("name") != package or porg != org:
        raise _err(f"HDX package_show: unexpected package/organisation {res.get('name')!r} / {porg!r}")
    hits = [r for r in res.get("resources") or [] if r.get("name") == resource]
    if len(hits) != 1 or not str(hits[0].get("url", "")).startswith(f"https://{HDX_HOST}/"):
        raise _err(f"HDX package_show: resource {resource} not found once with an HDX URL")
    meta = {
        "package": package,
        "license_id": res.get("license_id"),
        "license_title": res.get("license_title"),
        "package_last_modified": res.get("last_modified"),
        "resource_last_modified": hits[0].get("last_modified"),
        "resource_url": hits[0]["url"],
        "caveats": (res.get("caveats") or "")[:1000],
    }
    return str(hits[0]["url"]), meta


def build_mmp(body: bytes, sha: str) -> tuple[pl.DataFrame, dict[str, Any]]:
    text = body.decode("utf-8-sig")
    rdr = csv.DictReader(io.StringIO(text, newline=""))
    missing = [c for c in MMP_COLUMNS if c not in (rdr.fieldnames or [])]
    if missing:
        raise _err("MMP CSV: columns missing", missing=missing)
    #: (geo kind, provider geo, year, month, cause) -> [csv rows], sum, records without a total
    cells: dict[tuple[str, str, int, int, str], list[Any]] = {}
    stats: Counter[str] = Counter()
    seen_rows: dict[tuple[str, ...], int] = {}
    identical: list[list[int]] = []
    respelled: dict[int, str] = {}
    for rnum, r in enumerate(rdr, start=2):
        rec = tuple((r.get(k) or "") for k in rdr.fieldnames or ())
        if rec in seen_rows:
            identical.append([seen_rows[rec], rnum])
        else:
            seen_rows[rec] = rnum
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", (r["reported_date"] or "").strip())
        if not m:
            stats["records_without_reported_date"] += 1
            continue
        y, mo = int(m.group(1)), int(m.group(2))
        tdm_raw = (r["total_dead_and_missing"] or "").strip()
        tdm: int | None = None
        if tdm_raw:
            v, spelled = count_text(tdm_raw, f"MMP row {rnum} total_dead_and_missing")
            tdm = int(v)
            if spelled:
                respelled[rnum] = spelled
        else:
            stats["records_without_total"] += 1
        cause = (r["cause_death"] or "").strip() or "(not given)"
        country = (r["country_of_incident"] or "").strip()
        region = (r["region"] or "").strip()
        geos = []
        if country:
            geos.append(("country_of_incident", country))
        else:
            stats["records_without_country"] += 1
        if region:
            geos.append(("region_of_incident", f"mmp_region:{region}"))
        else:
            stats["records_without_region"] += 1
        for kind, geo in geos:
            for c in (cause, "ALL"):
                cell = cells.setdefault((kind, geo, y, mo, c), [[], 0, 0])
                cell[0].append(rnum)
                if tdm is None:
                    cell[2] += 1
                else:
                    cell[1] += tdm
    rows = Rows("iom", MMP_DATASET)
    geo_iso: dict[str, str | None] = {}
    for (kind, geo, y, mo, cause), (rnums, total, no_total) in sorted(cells.items()):
        if geo not in geo_iso:
            geo_iso[geo] = _iso3(geo, MMP_SCHEME)
        base = {"geo_kind": kind, "cause_scope": "all causes" if cause == "ALL" else "MMP cause_death category"}
        sel = {"file": MMP_RESOURCE, "rows": rnums}
        notes = [respelled[n] for n in rnums if n in respelled]
        dead_flags: dict[str, Any] = {**base, "aggregation": MMP_AGG_DEAD, "records": len(rnums)}
        if no_total:
            dead_flags["records_without_total"] = no_total
        if notes:
            dead_flags["provider_text_respelled"] = notes
        concept = "migration.iom.mmp_dead_and_missing_monthly" if cause == "ALL" and kind == "country_of_incident" else None
        if len(rnums) > no_total:  # at least one record with a total: the sum is a reported quantity
            rows.add(
                indicator=f"dead_and_missing|cause={cause}",
                provider_geo=geo,
                iso3=geo_iso[geo],
                period=month_period(y, mo),
                value=str(total),
                unit="persons (dead and missing)",
                flags=dead_flags,
                raw_sha256=sha,
                selector={**sel, "column": "total_dead_and_missing"},
                concept_id=concept,
            )
        rows.add(
            indicator=f"incident_records|cause={cause}",
            provider_geo=geo,
            iso3=geo_iso[geo],
            period=month_period(y, mo),
            value=str(len(rnums)),
            unit="incident records",
            flags={**base, "aggregation": MMP_AGG_COUNT},
            raw_sha256=sha,
            selector={**sel, "column": "web_id"},
        )
    if not len(rows):
        raise _err("MMP CSV: no record with a date")
    unmapped = sorted(g for g, i in geo_iso.items() if i is None and not g.startswith("mmp_region:"))
    rep = {
        "records": len(seen_rows) + len(identical),
        "cells": len(rows),
        "fully_identical_records": identical,
        "unmapped_country_names": unmapped,
        "respelled_totals": {str(k): v for k, v in respelled.items()},
        **dict(stats),
    }
    return rows.frame(), rep


def mmp_note(rep: Mapping[str, Any], meta: Mapping[str, Any]) -> str:
    return (
        f"IOM Missing Migrants Project incident records (HDX {MMP_PACKAGE}, resource {MMP_RESOURCE}, last modified "
        f"{meta.get('resource_last_modified')}): {rep['records']} records -> {rep['cells']} cells. Every cell is a sum "
        "(dead_and_missing) or a count (incident_records) of provider records computed by REVA, per country of incident "
        "(MMP name verbatim, exact-name aliases scheme iom_mmp) or MMP region (mmp_region:<name>, unmapped), per month of "
        "reported_date (codebook: 'Estimated date of death. In cases where the exact date of death is not known, this "
        "variable indicates the date in which the body or bodies were found.'; a 'cumulative total' record carries the "
        "latest date of its range) and per cause_death category (cause of death, not of migration) or cause=ALL. "
        "Records without total_dead_and_missing are counted in flags.records_without_total, never as zero. Fully "
        f"identical records counted as published: {rep['fully_identical_records'][:20]}. Unmapped country names: "
        f"{rep['unmapped_country_names']}. HDX caveat: {meta.get('caveats', '')[:400]} Licence: {MMP_LICENCE}."
    )[:MAX_TEXT]


# ============================================================================= IDMC national figures
IDMC_PACKAGE = "idmc_internal_displacement_conflict-violence_disasters"
IDMC_PACKAGE_URL = f"https://{HDX_HOST}/api/3/action/package_show?id={IDMC_PACKAGE}"
IDMC_ORG = "international-displacement-monitoring-centre-idmc"
IDMC_RESOURCE = "IDMC_Internal_Displacement_Conflict-Violence_Disasters.xlsx"
IDMC_DATASET = "gidd_national"
IDMC_SHEET = "1_Displacement_data"
IDMC_LICENCE = (
    "CC BY-NC 4.0 (stated in the workbook README: non-commercial; HDX's dataset record says CC BY - the more "
    "restrictive provider statement applies; maintainer review pending)"
)
IDMC_RIGHTS: dict[str, Any] = {
    "licence": IDMC_LICENCE,
    "licence_url": "https://creativecommons.org/licenses/by-nc/4.0/",
    "attribution": "Internal Displacement Monitoring Centre. Global Internal Displacement Database. IDMC. Available "
    "at: https://www.internal-displacement.org/database/displacement-data/ (via HDX)",
    "non_commercial": True,
    "source_key": "idmc.gidd",
}
#: workbook column -> (indicator, unit, time reference, catalog concept); the README defines every column (quoted
#: verbatim in IDMC_README_DEFINITIONS and in registry/atlas/catalog/migration.yaml)
IDMC_COLUMNS: dict[str, tuple[str, str, str, str | None]] = {
    "Conflict Stock Displacement": ("conflict_idps|rounded=yes", "people", "end of the reporting year", None),
    "Conflict Stock Displacement (Raw)": (
        "conflict_idps|rounded=no", "people", "end of the reporting year", "migration.idmc.conflict_idps"),
    "Conflict Internal Displacements": (
        "conflict_internal_displacements|rounded=yes", "internal displacements (flows)", "reporting year", None),
    "Conflict Internal Displacements (Raw)": (
        "conflict_internal_displacements|rounded=no", "internal displacements (flows)", "reporting year",
        "migration.idmc.conflict_internal_displacements"),
    "Disaster Internal Displacements": (
        "disaster_internal_displacements|rounded=yes", "internal displacements (flows)", "reporting year", None),
    "Disaster Internal Displacements (Raw)": (
        "disaster_internal_displacements|rounded=no", "internal displacements (flows)", "reporting year",
        "migration.idmc.disaster_internal_displacements"),
    "Disaster Stock Displacement": ("disaster_idps|rounded=yes", "people", "end of the reporting year", None),
    "Disaster Stock Displacement (Raw)": (
        "disaster_idps|rounded=no", "people", "end of the reporting year", "migration.idmc.disaster_idps"),
}
IDMC_README_DEFINITIONS = (
    "Conflict Stock Displacement: Total number of IDPs (rounded figures at the national level), as a result of conflict "
    "and violence as of the end of the reporting year. Units are recorded as 'People'.",
    "Conflict Stock Displacement (Raw): Total number of IDPs (not rounded), as a result of conflict and violence as of "
    "the end of the reporting year. Units are recorded as 'People'.",
    "Conflict Internal Displacements (Raw): Total number of internal displacements reported (not rounded), as a result "
    "of conflict and violence over the reporting year. Units are recorded as 'internal displacement flows' or "
    "'internal displacement movements'.",
    "Disaster Internal Displacements (Raw): Total number of internal displacements reported (not rounded), as a result "
    "of disasters over the reporting year. Units are recorded as 'internal displacement flows' or 'internal "
    "displacement movements'.",
    "Disaster Stock Displacement (Raw): Total number of IDPs (not rounded), as a result of disasters as of the end of "
    "the reporting year. Units are recorded as 'People'.",
    "Internal Displacements (flows): This metric represents the number of internal displacements, or internal "
    "displacement population flows, reported from January 1st to December 31st of a reporting year.This figure may "
    "include individuals who are displaced multiple times during the year by different events.",
)
IDMC_LICENCE_TEXT = "This content is licensed under CC BY-NC."


def build_idmc_national(body: bytes, sha: str) -> tuple[pl.DataFrame, dict[str, Any]]:
    from reva.adapters.connectors.atlas_un import Xlsx

    try:
        xl = Xlsx(body)
    except Exception as e:
        raise _err("IDMC workbook unreadable", kind="decoding_error") from e
    if IDMC_SHEET not in xl.sheets or "README" not in xl.sheets:
        raise _err("IDMC workbook sheets changed", sheets=sorted(xl.sheets))
    readme = "\n".join(v for _r, cells in xl.rows("README") for v in cells.values())
    missing_defs = [d[:60] for d in IDMC_README_DEFINITIONS if d not in readme]
    if missing_defs:
        raise _err("IDMC README definitions differ from the quoted ones (refused)", missing=missing_defs)
    if IDMC_LICENCE_TEXT not in readme:
        raise _err("IDMC README licence statement changed (refused until reviewed)")
    rows = Rows("idmc", IDMC_DATASET)
    header: dict[str, str] | None = None
    years: set[int] = set()
    respelled = 0
    for rnum, cells in xl.rows(IDMC_SHEET):
        if header is None:
            header = {v.strip(): k for k, v in cells.items()}
            want = {"ISO3", "Name", "Year", *IDMC_COLUMNS}
            if set(header) != want:
                raise _err("IDMC 1_Displacement_data header changed", header=sorted(header))
            continue
        iso, name, ytxt = (cells.get(header[c], "").strip() for c in ("ISO3", "Name", "Year"))
        if not iso or not re.fullmatch(r"\d{4}", ytxt):
            raise _err(f"IDMC row {rnum}: ISO3/Year missing or malformed")
        year = int(ytxt)
        years.add(year)
        for col, (ind, unit, tref, concept) in IDMC_COLUMNS.items():
            raw = cells.get(header[col], "").strip()
            if not raw:
                continue
            value, spelled = count_text(raw, f"IDMC row {rnum} {col}")
            flags: dict[str, Any] = {"geo_label": name, "time_reference": tref, "provider_column": col}
            if spelled:
                flags["provider_text"] = spelled
                respelled += 1
            rows.add(
                indicator=ind,
                provider_geo=iso,
                iso3=_iso3(iso, "iso3"),
                period=year_period(year),
                value=value,
                unit=unit,
                flags=flags,
                raw_sha256=sha,
                selector={"sheet": IDMC_SHEET, "cell": f"{header[col]}{rnum}"},
                concept_id=concept,
            )
    if not len(rows):
        raise _err("IDMC workbook: no figure")
    return rows.frame(), {"cells": len(rows), "years": [min(years), max(years)], "respelled": respelled}


def idmc_note(rep: Mapping[str, Any], meta: Mapping[str, Any]) -> str:
    return (
        f"IDMC Global Internal Displacement Database, national figures (HDX {IDMC_PACKAGE}, resource {IDMC_RESOURCE}, "
        f"last modified {meta.get('resource_last_modified')}), sheet {IDMC_SHEET}: {rep['cells']} cells, years "
        f"{rep['years'][0]}-{rep['years'][1]}. Rounded and raw (not rounded) columns are separate indicators "
        "(|rounded=yes / |rounded=no); stocks are IDPs at the end of the reporting year, flows are internal "
        "displacements over the year (may count a person several times). The trigger (conflict and violence / "
        "disasters) is IDMC's coding, stored as part of the indicator; REVA makes no causal claim from it. AB9 (Abyei "
        f"Area) kept unmapped. README definitions verified verbatim. Licence: {IDMC_LICENCE}."
    )[:MAX_TEXT]


# ============================================================================= specs + ingest
@dataclass(frozen=True)
class SpecDef:
    name: str
    source_key: str
    atlas_source: str
    provider: str
    licence: str
    hosts: tuple[str, ...]
    datasets: tuple[str, ...]
    dataset_ids: tuple[str, ...]
    planned_requests: int
    max_bytes: int
    cadence_days: int
    notes: str


SPECS = (
    SpecDef(
        "atlas.unhcr_asylum",
        UNHCR_SOURCE_KEY,
        "unhcr",
        "UNHCR Refugee Population Statistics Database (API v1: asylum applications and decisions)",
        UNHCR_LICENCE,
        (UNHCR_HOST,),
        tuple(e.dataset for e in UNHCR_ENDPOINTS),
        tuple(e.endpoint for e in UNHCR_ENDPOINTS),
        4,
        30_000_000,
        180,
        "Per endpoint: one ZIP download of every year (~0.7-1.0 MB) + one limit=1 JSON request (record count and "
        "totals used to verify the download).",
    ),
    SpecDef(
        "atlas.eurostat_asylum",
        "eurostat.dissemination",
        "eurostat",
        "Eurostat (SDMX 2.1 dissemination API): asylum applicants by citizenship, monthly (migr_asyappctzm)",
        ES_LICENCE,
        (ES_HOST,),
        (ES_DATASET,),
        (ES_DATASET_CODE,),
        2,
        60_000_000,
        30,
        "One gzip TSV of the whole dataset (~10 MB, ~470 MB uncompressed, streamed) + the OBS_FLAG code list.",
    ),
    SpecDef(
        "atlas.iom_mmp",
        MMP_SOURCE_KEY,
        "iom",
        "IOM Missing Migrants Project (via the Humanitarian Data Exchange, HDX)",
        MMP_LICENCE,
        (HDX_HOST, S3_HOST),
        (MMP_DATASET,),
        (f"hdx:{MMP_PACKAGE}", MMP_RESOURCE),
        3,
        60_000_000,
        30,
        "HDX package_show + the CSV resource (~7.4 MB; HDX redirects to its S3 file store).",
    ),
    SpecDef(
        "atlas.idmc_national",
        "idmc.gidd",
        "idmc",
        "Internal Displacement Monitoring Centre (IDMC), national figures, via the Humanitarian Data Exchange (HDX)",
        IDMC_LICENCE,
        (HDX_HOST, S3_HOST),
        (IDMC_DATASET,),
        (f"hdx:{IDMC_PACKAGE}", IDMC_RESOURCE),
        3,
        20_000_000,
        90,
        "HDX package_show + the national workbook (~0.5 MB). The disaster-event workbook of the same package is "
        "atlas.idmc's (KG + causal claims).",
    ),
)
SPECS_BY_NAME = {s.name: s for s in SPECS}


def atlas_source() -> list[Any]:
    from reva.atlas.sources import AtlasSourceSpec

    digest = module_digest()
    return [
        AtlasSourceSpec(
            name=s.name,
            version=VERSION,
            source_key=s.source_key,
            budget_key=s.source_key,
            atlas_source=s.atlas_source,
            provider=s.provider,
            licence=s.licence,
            allowed_hosts=s.hosts,
            datasets=s.datasets,
            dataset_ids=s.dataset_ids,
            cadence_seconds=s.cadence_days * 86400,
            planned_requests=s.planned_requests,
            max_response_bytes=s.max_bytes,
            min_interval_seconds=1.0,
            parser_version=PARSER_VERSION,
            code_digest=digest,
            notes=s.notes,
        )
        for s in SPECS
    ]


def _table_summary(reg: Mapping[str, Any]) -> dict[str, Any]:
    cov = reg.get("coverage") or {}
    return {
        **{k: reg.get(k) for k in ("dataset_key", "artifact_sha256", "row_count", "created")},
        "countries": cov.get("countries"),
        "unmapped_provider_geo": len(reg.get("unmapped_provider_geo") or []),
    }


async def _ingest_unhcr(run: Any, out: dict[str, Any]) -> None:
    for ep in UNHCR_ENDPOINTS:
        if not run.wants(ep.dataset):
            continue
        chk = await run.get(ep.check_url(), key=f"unhcr:{ep.endpoint}:check", max_bytes=1_000_000)
        raw = await run.get(
            ep.download_url(),
            key=f"unhcr:{ep.endpoint}:download",
            media_types=("application/zip",),
            max_bytes=30_000_000,
        )
        sha_c, sha = run.archive(chk), run.archive(raw)
        frame, rep = build_unhcr_asylum(ep, raw.body, sha, unhcr_check_totals(chk.body, ep))
        reg = run.register(
            dataset=ep.dataset,
            frame=frame,
            raw_sha256s=[sha, sha_c],
            coverage_note=unhcr_note(ep, rep),
            geo_scheme="iso3",
            rights=dict(UNHCR_RIGHTS),
        )
        out["tables"].append(_table_summary(reg))
        out["reports"][ep.dataset] = rep


async def _ingest_eurostat(run: Any, out: dict[str, Any]) -> None:
    if not run.wants(ES_DATASET):
        return
    fl = await run.get(ES_FLAGS_URL, key="eurostat:codelist:OBS_FLAG", media_types=(
        "application/vnd.sdmx.structure+xml", "application/xml", "text/xml"), max_bytes=2_000_000)
    raw = await run.get(
        ES_TSV_URL,
        key=f"eurostat:{ES_DATASET_CODE}:tsv",
        media_types=("text/tab-separated-values", "application/gzip", "application/octet-stream"),
        max_bytes=60_000_000,
    )
    sha_f, sha = run.archive(fl), run.archive(raw)
    frame, rep = build_eurostat_asylum(raw.body, sha, parse_obs_flags(fl.body))
    reg = run.register(
        dataset=ES_DATASET,
        frame=frame,
        raw_sha256s=[sha, sha_f],
        coverage_note=eurostat_note(rep),
        geo_scheme="eurostat",
        rights=dict(ES_RIGHTS),
    )
    out["tables"].append(_table_summary(reg))
    out["reports"][ES_DATASET] = rep


async def _ingest_hdx(run: Any, out: dict[str, Any], kind: str) -> None:
    pkg_url, package, org, resource, dataset = (
        (MMP_PACKAGE_URL, MMP_PACKAGE, MMP_ORG, MMP_RESOURCE, MMP_DATASET)
        if kind == "mmp"
        else (IDMC_PACKAGE_URL, IDMC_PACKAGE, IDMC_ORG, IDMC_RESOURCE, IDMC_DATASET)
    )
    if not run.wants(dataset):
        return
    pkg = await run.get(pkg_url, key=f"hdx:{package}:package_show", max_bytes=2_000_000)
    url, meta = hdx_resource_url(pkg.body, package, org, resource)
    media = (
        ("text/csv", "application/octet-stream", "binary/octet-stream")
        if kind == "mmp"
        else ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/octet-stream",
              "binary/octet-stream")
    )
    raw = await run.get(url, key=f"hdx:{package}:{resource}", media_types=media, max_bytes=60_000_000)
    sha_p, sha = run.archive(pkg), run.archive(raw)
    if kind == "mmp":
        frame, rep = build_mmp(raw.body, sha)
        note, rights, scheme = mmp_note(rep, meta), MMP_RIGHTS, MMP_SCHEME
    else:
        frame, rep = build_idmc_national(raw.body, sha)
        note, rights, scheme = idmc_note(rep, meta), IDMC_RIGHTS, "iso3"
    reg = run.register(
        dataset=dataset,
        frame=frame,
        raw_sha256s=[sha, sha_p],
        coverage_note=note,
        geo_scheme=scheme,
        rights={**rights, "hdx": {k: meta.get(k) for k in ("license_id", "license_title", "resource_last_modified")}},
    )
    out["tables"].append(_table_summary(reg))
    out["reports"][dataset] = {k: v for k, v in rep.items() if k != "fully_identical_records"} | (
        {"fully_identical_records": len(rep["fully_identical_records"])} if "fully_identical_records" in rep else {}
    )
    out["hdx"] = meta


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """Fetch (``run.get``) -> archive (``run.archive``) -> parse -> register (``run.register``) the wanted tables of
    ``run.spec``. Any provider error refuses that table (typed error in the report); budget refusals propagate."""
    from reva.operations.budgets import BudgetExhausted

    out: dict[str, Any] = {"connector": run.spec.name, "origin": run.origin, "tables": [], "reports": {}, "errors": []}
    try:
        if run.spec.name == "atlas.unhcr_asylum":
            await _ingest_unhcr(run, out)
        elif run.spec.name == "atlas.eurostat_asylum":
            await _ingest_eurostat(run, out)
        elif run.spec.name == "atlas.iom_mmp":
            await _ingest_hdx(run, out, "mmp")
        elif run.spec.name == "atlas.idmc_national":
            await _ingest_hdx(run, out, "idmc")
        else:  # pragma: no cover - discovery only lists SPECS
            raise ValueError(run.spec.name)
    except BudgetExhausted:
        raise
    except SourceResponseError as e:
        out["errors"].append({"error": type(e).__name__, "kind": getattr(e, "kind", None), "message": str(e)[:500]})
    out["ok"] = not out["errors"]
    return out


__all__ = [
    "SPECS",
    "UNHCR_ENDPOINTS",
    "atlas_ingest",
    "atlas_source",
    "build_eurostat_asylum",
    "build_idmc_national",
    "build_mmp",
    "build_unhcr_asylum",
]
