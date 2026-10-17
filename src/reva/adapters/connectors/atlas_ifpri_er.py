"""IFPRI Food Export Restrictions Tracker (public workbooks) -> KG events + atlas table (V6, agent V6AGRI).

Source (researched 2026-10-17): the Food Security Portal tool page "Food and Fertilizer Export Restrictions
Tracker" ``https://www.foodsecurityportal.org/node/25`` (archived each run: it carries the licence statements) and
its two download links "Download data for COVID-19" / "Download data for Ukraine Crisis":

* ``.../2022-04/Export Restrictions Food Products (IFPRI) - PublicVersion_IFPRI_ExpRestrictions_Covid19_0.xlsx``
  (cell A1 "(c) IFPRI 2021", B1 "License: ODC-By", C1 citation "Laborde D. & M. Parent, (2020). Food export
  restrictions in the era of Covid-19. Last update: 03-23-2022");
* ``.../2022-04/Export Restrictions Food Products (IFPRI) - PublicVersion_IFPRI_ExpRestrictions_UKRRUS.xlsx``
  ("(c) IFPRI 2022", "License: ODC-By", "Laborde D. & Mamun A. (2022). Food Export restrictions during the
  Ukraine-Russia crisis. Last update: <Excel date>").

The page's "csv format here" link (a published Google Sheet) answers ``410 Gone`` and the interactive Shiny app
has no download URL usable without a browser session: the measures after April 2022 (India's 2022 wheat and 2023
rice measures among them) are **not** in these open files (owner decision, docs/dev/v6_agri.md).

Licence: each workbook states "License: ODC-By" (Open Data Commons Attribution License); the page says "The Food
Export Restrictions Tracker data is made available under the Open Data Commons Attribution License" and "The Food
Export Restrictions Tracker is licensed under a Creative Commons Attribution 4.0 International License". The cell
of the workbook is checked on every run (a different licence cell refuses the parse).

KG (``guarded_kg_load``): one entity per measure ``ifpri_er:<tracker>:<Code>`` (``covid19`` / ``ukrrus``), kind
``export_restriction`` (event type in ``registry/events/taxonomy.yaml``), with ``ifpri_er:imposed_by`` -> country
(Country_Alpha3, an ISO 3166-1 alpha-3 code published by IFPRI), P580 start (StartingDate), P582 end (EndDate:
the column's definition is missing from the workbook's definition row, so the end is "as coded"), announcement and
last-update dates, category, products, description, status, sources, WTO notification code, exempted countries
(text), and ``ifpri_er:covers_hs_heading`` -> ``hs2017:<HS4>`` (HS edition not stated by IFPRI: HS 2017 assumed
and said so on every statement; a numeric cell of 3 digits is the spreadsheet's rendering of a heading of
chapters 01-09 and is zero-padded, flagged). Dates are Excel serial numbers (1900 date system, checked).

Atlas table ``ifpri_er.measures``: the tracker's numeric fields per measure (shares of the country's food exports
and of world food exports in kcal and USD, number of HS4 products with a >= 5 % world share, calories, dollars),
``indicator`` = ``<column>|measure=<tracker>:<Code>``, ``provider_geo`` = Country_Alpha3 (scheme ``iso3``), period =
[StartingDate, EndDate + 1 day) (or the start day alone when no end is coded, flagged). Columns whose meaning the
workbook does not define (Stock, Production, Use, share*, TDollars) are not loaded.
"""

from __future__ import annotations

import html
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from urllib.parse import quote

from reva.adapters.connectors.agri_common import canon, guarded_kg_load, module_digest
from reva.adapters.connectors.atlas_geopol_common import KgEntity, KgStatement, load_qid_for_iso3
from reva.adapters.connectors.vintage_common import DECIMAL_RE, Rows, decimal_or_none
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.ifpri_er"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas_ifpri_er/1"
SOURCE_KEY = "ifpri.export_restrictions"
SOURCE = "ifpri_er"
DATASET = "measures"
HOST = "www.foodsecurityportal.org"
PAGE_URL = f"https://{HOST}/node/25"
FILE_BASE = f"https://{HOST}/sites/default/files/2022-04/"
XLSX_MEDIA = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/octet-stream",
    "application/zip",
)
LICENCE = "ODC-By (Open Data Commons Attribution License), as stated in each IFPRI workbook"
KG_PIDS = (
    "ifpri_er:imposed_by",
    "ifpri_er:covers_hs_heading",
    "ifpri_er:hs4_as_stated",
    "ifpri_er:measure_category",
    "ifpri_er:products",
    "ifpri_er:description",
    "ifpri_er:announced",
    "ifpri_er:last_update",
    "ifpri_er:verification_status",
    "ifpri_er:source_url",
    "ifpri_er:wto_notified",
    "ifpri_er:protected_countries",
)
WIKIDATA_PIDS = ("P580", "P582")
HEADER = (
    "Code",
    "LastUpdate",
    "AnnouncementDate",
    "StartingDate",
    "EndDate",
    "Country_Alpha3",
    "Country_Label",
    "Products",
    "HS4",
    "Category",
    "Description",
    "Additional Comments",
    "Status",
    "Source",
    "Source2",
    "ProtectedCountries",
    "WTOcountry",
    "WTOnotification",
    "Share_of_exports_Kcal",
    "Share_of_exports_USD",
    "Share_in_World_Kcal",
    "Share_in_World_USD",
    "Number_Products_5pct",
    "Calories",
    "Dollars",
)
#: numeric columns loaded into the atlas: column -> unit (the definitions are quoted from the workbook).
METRICS = {
    "Share_of_exports_Kcal": "share (fraction, as published)",
    "Share_of_exports_USD": "share (fraction, as published)",
    "Share_in_World_Kcal": "share (fraction, as published)",
    "Share_in_World_USD": "share (fraction, as published)",
    "Number_Products_5pct": "count of HS4 products",
    "Calories": "billion kcal per year",
    "Dollars": "million USD per year",
}
HS_VERSION_BASIS = (
    "HS edition not stated by IFPRI; REVA links the code as stated to the HS 2017 heading (assumption: the tracker "
    "was compiled in 2020-2022)"
)
PAD_NOTE = "numeric 3-digit cell zero-padded to 4 digits by REVA (spreadsheet number rendering drops the leading 0)"
EXCEL_EPOCH = date(1899, 12, 30)
_HS4 = re.compile(r"^[0-9]{4}$")


@dataclass(frozen=True)
class Tracker:
    tag: str
    file: str
    title: str

    @property
    def url(self) -> str:
        return FILE_BASE + quote(self.file)


TRACKERS: tuple[Tracker, ...] = (
    Tracker(
        "covid19",
        "Export Restrictions Food Products (IFPRI) - PublicVersion_IFPRI_ExpRestrictions_Covid19_0.xlsx",
        "IFPRI Food export restrictions in the era of Covid-19 (Laborde & Parent 2020)",
    ),
    Tracker(
        "ukrrus",
        "Export Restrictions Food Products (IFPRI) - PublicVersion_IFPRI_ExpRestrictions_UKRRUS.xlsx",
        "IFPRI Food export restrictions during the Ukraine-Russia crisis (Laborde & Mamun 2022)",
    ),
)


class IfpriError(SourceResponseError):
    def __init__(self, msg: str, kind: str = "schema_drift") -> None:
        super().__init__(msg, retryable=False, kind=kind)


@dataclass
class Measure:
    tracker: str
    row: int
    code: str
    values: dict[str, str]
    kinds: dict[str, str]

    @property
    def node(self) -> str:
        return f"ifpri_er:{self.tracker}:{self.code}"


@dataclass
class WorkbookReport:
    tracker: str
    sheet: str = ""
    licence_cell: str = ""
    citation_cell: str = ""
    definitions: dict[str, str] = field(default_factory=dict)
    measures: int = 0
    categories: dict[str, int] = field(default_factory=dict)
    countries: dict[str, int] = field(default_factory=dict)


# --------------------------------------------------------------------------- page facts
def page_facts(body: bytes) -> dict[str, str]:
    """Licence sentences quoted from the archived tool page (empty string when one is no longer there)."""
    t = body.decode("utf-8", errors="replace")
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", t, flags=re.S)
    t = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t)))
    out = {}
    for key, pat in (
        (
            "odc_by",
            r"The Food Export Restrictions Tracker data is made available under the Open Data Commons Attribution License",
        ),
        (
            "cc_by",
            r"The Food Export Restrictions Tracker is licensed under a Creative Commons Attribution 4\.0 International License",
        ),
        ("last_update", r"Last Update: [0-9/]+ \| Next Update: [0-9/]+"),
        ("legacy", r"This unmaintained legacy tool is no longer updated[^.]*\."),
    ):
        m = re.search(pat, t)
        out[key] = m.group(0) if m else ""
    return out


# --------------------------------------------------------------------------- workbook
def excel_date(text: str | None) -> date | None:
    if text is None or not str(text).strip():
        return None
    s = str(text).strip()
    if not re.match(r"^[0-9]{4,5}(\.0+)?$", s):
        raise IfpriError(f"unexpected date cell {s[:20]!r} (expected an Excel serial number)")
    n = int(float(s))
    if n < 61:
        raise IfpriError(f"Excel serial {n} is before 1900-03-01 (1900 leap-year bug range)")
    return EXCEL_EPOCH + timedelta(days=n)


def parse_workbook(body: bytes, tr: Tracker) -> tuple[list[Measure], WorkbookReport]:
    from reva.adapters.connectors.atlas_xlsx import Workbook, XlsxError

    try:
        wb = Workbook(body)
    except XlsxError as e:
        raise IfpriError(f"{tr.file}: {e}") from e
    if b'date1904="1"' in wb._read("xl/workbook.xml") or b'date1904="true"' in wb._read("xl/workbook.xml"):
        raise IfpriError(f"{tr.file}: workbook uses the 1904 date system")
    if len(wb.sheets) != 1:
        raise IfpriError(f"{tr.file}: expected one sheet, found {sorted(wb.sheets)}")
    sheet = next(iter(wb.sheets))
    rows = list(wb.rows(sheet))
    rep = WorkbookReport(tracker=tr.tag, sheet=sheet)
    by_num = dict(rows)
    first = by_num.get(1, {})
    rep.licence_cell = first[1].text if 1 in first else ""
    rep.citation_cell = first[2].text if 2 in first else ""
    if rep.licence_cell.strip() != "License: ODC-By":
        raise IfpriError(
            f"{tr.file}: licence cell B1 is {rep.licence_cell[:60]!r}, not 'License: ODC-By'", kind="licence"
        )
    defs, head = by_num.get(3, {}), by_num.get(4, {})
    header = tuple(head[i].text.strip() if i in head else "" for i in range(len(HEADER)))
    if header != HEADER:
        raise IfpriError(f"{tr.file}: header changed: {header}")
    for i, col in enumerate(HEADER):
        if col == "EndDate":
            continue  # the definition row has no EndDate entry (its cell defines a 'Year' column): not quoted
        rep.definitions[col] = defs[i].text.strip() if i in defs else ""
    out: list[Measure] = []
    seen: set[str] = set()
    for rnum, cells in rows:
        if rnum <= 4:
            continue
        vals = {col: (cells[i].text.strip() if i in cells else "") for i, col in enumerate(HEADER)}
        kinds = {col: (cells[i].kind if i in cells else "empty") for i, col in enumerate(HEADER)}
        if not any(vals.values()):
            continue
        code = vals["Code"]
        if not re.match(r"^[0-9]{1,6}$", code):
            raise IfpriError(f"{tr.file}: row {rnum}: Code {code!r} is not a number")
        if code in seen:
            raise IfpriError(f"{tr.file}: duplicate Code {code}")
        seen.add(code)
        if not re.match(r"^[A-Z]{3}$", vals["Country_Alpha3"]):
            raise IfpriError(f"{tr.file}: row {rnum}: Country_Alpha3 {vals['Country_Alpha3']!r}")
        out.append(Measure(tr.tag, rnum, code, vals, kinds))
    rep.measures = len(out)
    rep.categories = dict(Counter(m.values["Category"] for m in out))
    rep.countries = dict(Counter(m.values["Country_Alpha3"] for m in out))
    if not out:
        raise IfpriError(f"{tr.file}: no measure")
    return out, rep


def hs_codes(m: Measure) -> tuple[list[tuple[str, str | None]], list[str]]:
    """(code, padding note) pairs for the HS4 cell, and the malformed parts (kept as text only)."""
    raw = m.values["HS4"]
    if not raw:
        return [], []
    ok: list[tuple[str, str | None]] = []
    bad: list[str] = []
    numeric = m.kinds["HS4"] == "n"
    for part in re.split(r"[;,]", raw):
        p = part.strip()
        if not p:
            continue
        if _HS4.match(p):
            ok.append((p, None))
        elif numeric and re.match(r"^[1-9][0-9]{2}$", p):
            ok.append(("0" + p, PAD_NOTE))
        else:
            bad.append(p)
    return ok, bad


# --------------------------------------------------------------------------- KG
def _t(d: date) -> dict[str, Any]:
    return {"type": "time", "time": d.isoformat(), "precision": 11}


def _s(v: str) -> dict[str, Any]:
    return {"type": "string", "value": v[:4000]}


def kg_records(
    measures: list[Measure], trackers: dict[str, Tracker], shas: dict[str, str], qids: dict[str, str]
) -> tuple[list[KgEntity], list[KgStatement], dict[str, Any]]:
    ents: list[KgEntity] = []
    sts: list[KgStatement] = []
    stats: Counter[str] = Counter()
    for m in measures:
        v, tr, sha = m.values, trackers[m.tracker], shas[m.tracker]
        sel = {"file": tr.file, "row": m.row}
        start, end = excel_date(v["StartingDate"]), excel_date(v["EndDate"])
        announced, updated = excel_date(v["AnnouncementDate"]), excel_date(v["LastUpdate"])
        when = start.isoformat() if start else "undated"
        ents.append(
            KgEntity(
                qid=m.node,
                kind="export_restriction",
                labels={
                    "en": f"{v['Category'] or 'Measure'} on exports of {v['Products'][:120]} by {v['Country_Label']} "
                    f"from {when} (IFPRI {m.tracker} {m.code})"
                },
                descriptions={"en": f"Food export restriction as coded in the {tr.title} workbook (ODC-By)"},
            )
        )

        def add(
            key: str, prop: str, *, _m: Measure = m, _sha: str = sha, _sel: dict[str, Any] = sel, **kw: Any
        ) -> None:
            sts.append(
                KgStatement(
                    key=f"{_m.tracker}:{_m.code}:{key}",
                    subject=_m.node,
                    property=prop,
                    raw_sha256=_sha,
                    selector=_sel,
                    **kw,
                )
            )

        iso = v["Country_Alpha3"]
        node = qids.get(iso) or f"iso3:{iso}"
        stats["imposed_by_qid" if iso in qids else "imposed_by_iso3_node"] += 1
        tkw: dict[str, Any] = {}
        tq: dict[str, Any] = {}
        if start:
            tkw.update(start=start.isoformat(), start_precision=11)
        if end and start and end < start:
            tq = {"coded_end": end.isoformat(), "date_inconsistency": "coded end precedes coded start (kept as coded)"}
        elif end:
            tkw.update(end=end.isoformat(), end_precision=11)
        add(
            "imposed_by",
            "ifpri_er:imposed_by",
            object_qid=node,
            qualifiers={"country_alpha3": iso, "country_label": v["Country_Label"], **tq},
            **tkw,
        )
        if start:
            add("start", "P580", value=_t(start), qualifiers={"column": "StartingDate"})
        if end:
            add(
                "end",
                "P582",
                value=_t(end),
                qualifiers={
                    "column": "EndDate",
                    "definition": "not given in the workbook's definition row (end date as coded)",
                },
            )
        if announced:
            add("announced", "ifpri_er:announced", value=_t(announced))
        if updated:
            add("last_update", "ifpri_er:last_update", value=_t(updated))
        for col, prop in (
            ("Category", "ifpri_er:measure_category"),
            ("Products", "ifpri_er:products"),
            ("Status", "ifpri_er:verification_status"),
            ("WTOnotification", "ifpri_er:wto_notified"),
            ("ProtectedCountries", "ifpri_er:protected_countries"),
        ):
            if v[col]:
                add(col.lower(), prop, value=_s(v[col]))
        if v["Description"]:
            dq: dict[str, Any] = {"truncated": True} if len(v["Description"]) > 4000 else {}
            add("description", "ifpri_er:description", value=_s(v["Description"]), qualifiers=dq)
        for col in ("Source", "Source2"):
            for i, url in enumerate(u.strip() for u in v[col].split(";") if u.strip()):
                add(f"{col.lower()}:{i}", "ifpri_er:source_url", value=_s(url), qualifiers={"column": col})
        if v["HS4"]:
            add("hs4", "ifpri_er:hs4_as_stated", value=_s(v["HS4"]))
            ok, bad = hs_codes(m)
            stats["hs_malformed"] += len(bad)
            for code, pad in ok:
                hq: dict[str, Any] = {
                    "hs_code_as_stated": code if pad is None else code[1:],
                    "hs_version_basis": HS_VERSION_BASIS,
                }
                if pad:
                    hq["hs_code_repair"] = pad
                    stats["hs_zero_padded"] += 1
                add(f"hs:{code}", "ifpri_er:covers_hs_heading", object_qid=f"hs2017:{code}", qualifiers=hq)
                stats["hs_links"] += 1
    stats["entities"] = len(ents)
    stats["statements"] = len(sts)
    return ents, sts, dict(stats)


# --------------------------------------------------------------------------- atlas
def atlas_rows(
    measures: list[Measure], trackers: dict[str, Tracker], shas: dict[str, str], definitions: dict[str, dict[str, str]]
) -> Any:
    rows = Rows()
    for m in measures:
        v, tr = m.values, trackers[m.tracker]
        start, end = excel_date(v["StartingDate"]), excel_date(v["EndDate"])
        if start is None:
            continue
        if end is not None and end >= start:
            p0, p1, basis = start, end + timedelta(days=1), "StartingDate .. EndDate (inclusive, as coded)"
        else:
            p0, p1, basis = start, start + timedelta(days=1), "start day only (no usable EndDate coded)"
        for col, unit in METRICS.items():
            raw = v[col]
            if not raw:
                continue
            val = decimal_or_none(raw)
            if val is None or not DECIMAL_RE.match(val):
                raise IfpriError(f"{tr.file}: row {m.row} {col}: {raw[:30]!r} is not a number")
            rows.add(
                source=SOURCE,
                dataset=DATASET,
                indicator=f"{col}|measure={m.tracker}:{m.code}",
                provider_geo=v["Country_Alpha3"],
                start=p0,
                end=p1,
                value=val,
                unit=unit,
                flags=canon(
                    {
                        "definition": definitions[m.tracker].get(col, ""),
                        "measure": m.node,
                        "category": v["Category"],
                        "products": v["Products"][:300],
                        "hs4_as_stated": v["HS4"],
                        "status": v["Status"],
                        "period_basis": basis,
                    }
                ),
                raw_sha256=shas[m.tracker],
                selector={"file": tr.file, "row": m.row, "column": col},
            )
    return rows.frame()


def rights(facts: dict[str, str]) -> dict[str, Any]:
    return {
        "licence": "ODC-By 1.0 (Open Data Commons Attribution License)",
        "licence_url": "https://opendatacommons.org/licenses/by/1-0/",
        "attribution": "Laborde D., Mamun A., Parent M. (IFPRI), Food Export Restrictions Tracker (COVID-19 and "
        "Ukraine-crisis public workbooks)",
        "provider": "International Food Policy Research Institute (IFPRI), Food Security Portal",
        "licence_statement_page": facts.get("odc_by") or None,
        "licence_statement_page_cc": facts.get("cc_by") or None,
        "licence_checked": "2026-10-17",
        "source_key": SOURCE_KEY,
    }


def code_digest() -> str:
    return module_digest(sys.modules[__name__])


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        atlas_source=SOURCE,
        provider="International Food Policy Research Institute (IFPRI), Food Export Restrictions Tracker",
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=(DATASET,),
        dataset_ids=tuple(t.tag for t in TRACKERS),
        cadence_seconds=90 * 86_400,
        planned_requests=1 + len(TRACKERS),
        max_response_bytes=5_000_000,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={
            "atlas_tables": [f"{SOURCE}.{DATASET}"],
            "kg": list(KG_PIDS + WIKIDATA_PIDS),
            "entities": ["ifpri_er:<tracker>:<Code>"],
        },
        notes="Tool page + two ODC-By workbooks (~60 kB); KG export_restriction events + per-measure atlas metrics.",
    )


async def atlas_ingest(run: Any, *, load_kg: bool = True) -> dict[str, Any]:
    if not run.wants(DATASET):
        return {"tables": {}}
    page = await run.get(PAGE_URL, key="page:tracker", media_types=("text/html",), max_bytes=3_000_000)
    page_sha = run.archive(page)
    facts = page_facts(page.body)
    trackers = {t.tag: t for t in TRACKERS}
    shas: dict[str, str] = {}
    measures: list[Measure] = []
    reports: dict[str, Any] = {}
    defs: dict[str, dict[str, str]] = {}
    retrieved = page.retrieved_at
    for t in TRACKERS:
        raw = await run.get(t.url, key=f"xlsx:{t.tag}", media_types=XLSX_MEDIA, max_bytes=5_000_000)
        shas[t.tag] = run.archive(raw)
        ms, rep = parse_workbook(raw.body, t)
        measures.extend(ms)
        defs[t.tag] = rep.definitions
        reports[t.tag] = rep.__dict__
        retrieved = max(retrieved, raw.retrieved_at)
    frame = atlas_rows(measures, trackers, shas, defs)
    note = (
        f"IFPRI Food Export Restrictions Tracker public workbooks ({PAGE_URL}): "
        + "; ".join(
            f"{t.tag}: {reports[t.tag]['measures']} measures, '{reports[t.tag]['citation_cell']}'" for t in TRACKERS
        )
        + f". Licence cell 'License: ODC-By' checked; page: '{facts.get('odc_by')}'. Numeric fields per measure with "
        "IFPRI's column definitions quoted in flags; period = StartingDate..EndDate as coded. Measures after April "
        "2022 are not in the open files. The KG holds one export_restriction event per measure."
    )[:4000]
    reg = run.register(
        dataset=DATASET,
        frame=frame,
        raw_sha256s=sorted(set(shas.values())),
        coverage_note=note,
        geo_scheme="iso3",
        rights=rights(facts),
    )
    out: dict[str, Any] = {
        "tables": {DATASET: {"dataset_key": reg["dataset_key"], "rows": reg["row_count"], "created": reg["created"]}},
        "workbooks": {k: {kk: vv for kk, vv in r.items() if kk != "definitions"} for k, r in reports.items()},
        "page": {"sha256": page_sha, **facts},
    }
    if load_kg:
        ents, sts, stats = kg_records(measures, trackers, shas, load_qid_for_iso3())
        out["kg_stats"] = stats
        out["kg"] = guarded_kg_load(
            run.engine,
            run.store,
            pids=KG_PIDS,
            wikidata_pids=WIKIDATA_PIDS,
            entities=ents,
            statements=sts,
            source_key=SOURCE_KEY,
            source_revision=f"ifpri_er@{'+'.join(sorted(s[:12] for s in shas.values()))}",
            raw_sha256s=sorted(set(shas.values())),
            retrieved_at=retrieved,
            origin=run.origin,
        )
    return out


__all__ = [
    "CONNECTOR_NAME",
    "TRACKERS",
    "IfpriError",
    "atlas_ingest",
    "atlas_source",
    "excel_date",
    "hs_codes",
    "kg_records",
    "page_facts",
    "parse_workbook",
]
