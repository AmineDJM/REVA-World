"""USGS National Minerals Information Center data releases -> atlas tables (V3, agent A-RESOURCES).

Who produces and who holds which mineral, for every country: USGS *Mineral Commodity Summaries* (world mine
production and reserves by country for ~90 non-fuel commodities, plus U.S. salient statistics) and the
*Minerals Yearbook, volume III (Area Reports — International)* production tables (every commodity a country
produces, 5 years per edition).

Source (researched 2026-10-15): USGS ScienceBase data releases (machine-readable CSV + FGDC metadata), one
ScienceBase item per release. The connector fetches the item JSON
(``https://www.sciencebase.gov/catalog/item/<id>?format=json``), takes the CSV and metadata URLs *from the
item's own file list* (by exact file name), archives all three and parses the CSV:

=========  ==========================  ==========================================================================
dataset    ScienceBase item            content
=========  ==========================  ==========================================================================
mcs2026    69837e43b66b01367d7ec7c7    MCS 2026 data release (doi:10.5066/P1WKQ63T), ``MCS2026_Commodities_Data.csv``:
                                       world production / reserves / capacity by country (2024, 2025), U.S.
                                       salient statistics 2021-2025, U.S. import sources 2021-24 (partner shares)
myb2024    6a8490171ba49b3133cd8055    2024 Minerals Yearbook vol. III (doi:10.5066/P1KEQASH),
                                       ``2024MYBv3_Production_table.csv``: production 2020-2024, ~165 countries
myb2019    67febb3cd4be02350c2609bc    2019 Minerals Yearbook vol. III table 1 (doi:10.5066/P13HTKV3),
                                       ``2019_MYBviii_Table1_08-21-2025.csv``: production 2015-2019, ~170 countries
=========  ==========================  ==========================================================================

Licence: U.S. public domain; the ScienceBase items / FGDC metadata mark the data CC0 1.0 ("These data are in
the public domain and do not have any use constraints"). The parse fails closed when neither the archived item
JSON nor the archived metadata says CC0 / public domain any more.

Parsing — values exactly as published, never rescaled:

* ``mcs2026``: ``indicator`` = ``<Commodity>|<Section>|<Statistics>|<Statistics_detail>|unit=<Unit>`` (verbatim
  labels; the MCS release has no codes). Thousands separators are removed from numbers (formatting only).
  The value codes are read from the archived FGDC metadata (``XX``, ``W``, ``s``, ``NA``, ``E``, ``—``):
  ``W`` (withheld) and ``NA`` / ``XX`` produce a value-less cell with ``flags.missing`` (never a zero);
  ``—`` is USGS's own "zero" and becomes ``0`` with ``flags.provider_code``; ``s`` (small) and ``E`` (net
  exporter) and qualitative entries (``Large``, ``>2,000,000``, ``330 - 390``) go to ``value_text``.
  ``Notes`` are kept verbatim in flags; "Estimated." -> ``flags.estimate_kind = "estimated"``. The "Import
  Sources" section lists the *partner* country in the Country column (share of U.S. imports 2021-24):
  ``provider_geo`` is that partner, ``flags.reporter = "United States"``.
* ``myb2024`` (long CSV): ``indicator`` = levels 1-5, descriptor and commodity unit joined by ``|`` plus
  ``|unit=<Unit>``; ``Value Notes = Estimated`` -> ``estimate_kind``; ``-999`` ("Value is not known or not
  available", archived metadata) -> value-less cell flagged ``not_available``. Rows that share every key column
  are told apart by their ``Other Notes`` (Ghana: large-scale vs artisanal gold) or, when nothing distinguishes
  them, by an ordinal flagged ``ambiguous_duplicate`` (North Korea graphite).
* ``myb2019`` (wide CSV, one column per year): ``indicator`` = ``<Commodity>|<Commodity Product>|<Commodity
  Type>|unit=<Units>``; per-year footnotes verbatim ("Revised." -> ``flags.revised``, "Estimated." ->
  ``estimate_kind``); ``-999`` as above.

Countries are names: mapped through scheme ``usgs_name`` (``registry/atlas/geo_aliases/a_resources.yaml``);
"World total", "Other countries" and combined rows stay unmapped (kept, listed in each manifest).
"""

from __future__ import annotations

import csv
import hashlib
import inspect
import io
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import polars as pl

from reva.atlas.sources import AtlasSourceSpec
from reva.atlas.tables import ATLAS_SCHEMA, country_iso3
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.usgs_minerals"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas_usgs/1"
SOURCE_KEY = "usgs.sciencebase"
SOURCE = "usgs"
HOST = "www.sciencebase.gov"
GEO_SCHEME = "usgs_name"
LICENCE = "U.S. public domain; ScienceBase data releases marked CC0 1.0 (USGS National Minerals Information Center)"
ITEM_URL = "https://" + HOST + "/catalog/item/{item_id}?format=json"
CSV_MEDIA = ("text/csv", "application/octet-stream", "text/plain")
XML_MEDIA = ("application/fgdc+xml", "application/xml", "text/xml")
MAX_NOTE = 1000
DECIMAL_RE = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")
THOUSANDS_RE = re.compile(r"^-?[1-9][0-9]{0,2}(,[0-9]{3})+(\.[0-9]+)?$")
YEAR_RE = re.compile(r"^([0-9]{4})(?:\s*[–-]\s*([0-9]{2,4}))?$")
NOT_KNOWN = "-999"


class UsgsParseError(SourceResponseError):
    def __init__(self, msg: str) -> None:
        super().__init__(msg, retryable=False, kind="parse_error")


@dataclass(frozen=True)
class Release:
    """One ScienceBase data release = one atlas table."""

    dataset: str
    item_id: str
    data_file: str
    meta_file: str
    kind: str  # mcs | myb_long | myb_wide
    title: str
    doi: str
    max_bytes: int = 20_000_000

    @property
    def item_url(self) -> str:
        return ITEM_URL.format(item_id=self.item_id)


RELEASES: tuple[Release, ...] = (
    Release(
        "mcs2026",
        "69837e43b66b01367d7ec7c7",
        "MCS2026_Commodities_Data.csv",
        "MCS2026_Commodities_Data.xml",
        "mcs",
        "Mineral Commodity Summaries 2026 Data Release - Commodity Salient U.S. and World Statistics",
        "https://doi.org/10.5066/P1WKQ63T",
    ),
    Release(
        "myb2024",
        "6a8490171ba49b3133cd8055",
        "2024MYBv3_Production_table.csv",
        "2024_MYBv3_metadata.xml",
        "myb_long",
        "2024 Minerals Yearbook, volume III, Area Reports — International: global production (2020-2024)",
        "https://doi.org/10.5066/P1KEQASH",
    ),
    Release(
        "myb2019",
        "67febb3cd4be02350c2609bc",
        "2019_MYBviii_Table1_08-21-2025.csv",
        "2019_MYBviii_table1_08-21-2025.xml",
        "myb_wide",
        "Minerals Yearbook, volume III, Area Reports — International: 2019 country chapters, table 1 (2015-2019)",
        "https://doi.org/10.5066/P13HTKV3",
    ),
)
RELEASES_BY_DATASET = {r.dataset: r for r in RELEASES}


# --------------------------------------------------------------------------- spec
def code_digest() -> str:
    return "sha256:" + hashlib.sha256(inspect.getsource(sys.modules[__name__]).encode()).hexdigest()


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        atlas_source=SOURCE,
        provider="U.S. Geological Survey, National Minerals Information Center (ScienceBase data releases)",
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=tuple(r.dataset for r in RELEASES),
        dataset_ids=tuple(r.doi for r in RELEASES),
        cadence_seconds=30 * 86_400,
        planned_requests=3 * len(RELEASES),
        max_response_bytes=max(r.max_bytes for r in RELEASES),
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"atlas_tables": [f"{SOURCE}.{r.dataset}" for r in RELEASES], "geo_scheme": GEO_SCHEME},
        notes="USGS MCS + Minerals Yearbook vol. III data releases on ScienceBase (item JSON, CSV, FGDC metadata)",
    )


# --------------------------------------------------------------------------- item + metadata
def parse_item(body: bytes, release: Release) -> dict[str, Any]:
    """ScienceBase item JSON -> {title, rights, citation, files {name: {url, size, contentType}}}."""
    try:
        doc = json.loads(body)
        files = {f["name"]: f for f in doc.get("files") or []}
    except (ValueError, KeyError, TypeError) as e:
        raise UsgsParseError(f"ScienceBase item {release.item_id}: unexpected JSON ({e})") from e
    if doc.get("id") != release.item_id:
        raise UsgsParseError(f"ScienceBase item id {doc.get('id')!r} != {release.item_id}")
    for name in (release.data_file, release.meta_file):
        if name not in files or not str(files[name].get("url", "")).startswith(f"https://{HOST}/"):
            raise UsgsParseError(f"ScienceBase item {release.item_id} has no file {name!r} on {HOST}")
    return {
        "title": doc.get("title"),
        "rights": doc.get("rights"),
        "citation": doc.get("citation"),
        "dates": doc.get("dates"),
        "files": {n: {k: f.get(k) for k in ("url", "size", "contentType")} for n, f in files.items()},
    }


def _strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def parse_metadata(body: bytes) -> dict[str, Any]:
    """FGDC metadata -> {use_constraints, value_codes {code: meaning}} (value attributes only)."""
    text = body.decode("utf-8", errors="replace")
    use = re.search(r"<useconst>(.*?)</useconst>", text, re.S)
    codes: dict[str, str] = {}
    for m in re.finditer(r"<attr>(.*?)</attr>", text, re.S):
        a = m.group(1)
        lab = re.search(r"<attrlabl>(.*?)</attrlabl>", a, re.S)
        if not lab or not re.match(r"^(Value|[0-9]{4} Production)$", lab.group(1).strip()):
            continue
        for e in re.finditer(r"<edomv>(.*?)</edomv>\s*<edomvd>(.*?)</edomvd>", a, re.S):
            vs, desc = e.group(1).strip(), e.group(2).strip()
            listed = [v.strip() for v in vs.split(",")]
            lines = [ln.strip() for ln in desc.splitlines() if ln.strip()]
            per = {}
            for ln in lines:
                mm = re.match(r"^(\S+)\s+-\s+(.*)$", ln)
                if mm and mm.group(1) in listed:
                    per[mm.group(1)] = mm.group(2).strip()
            if len(listed) == 1:
                codes.setdefault(listed[0], per.get(listed[0], _strip_tags(desc)))
            else:
                for v in listed:
                    if v in per:
                        codes.setdefault(v, per[v])
    return {"use_constraints": _strip_tags(use.group(1)) if use else None, "value_codes": codes}


def licence_ok(item: dict[str, Any], meta: dict[str, Any]) -> bool:
    blob = f"{item.get('rights') or ''} {meta.get('use_constraints') or ''}".lower()
    return any(k in blob for k in ("cc0", "creative commons zero", "public domain"))


# --------------------------------------------------------------------------- csv helpers
def decode_csv(body: bytes) -> tuple[str, str]:
    try:
        return body.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        return body.decode("cp1252"), "cp1252"


def _records(text: str) -> tuple[list[str], list[dict[str, str]]]:
    rd = csv.DictReader(io.StringIO(text, newline=""))
    rows = [{k: (v or "").strip() for k, v in r.items() if k is not None} for r in rd]
    return [h.strip() for h in (rd.fieldnames or [])], rows


def _number(v: str) -> str | None:
    """Strict decimal string of a published number (thousands commas removed); None if not a number."""
    if DECIMAL_RE.match(v):
        return v
    if THOUSANDS_RE.match(v):
        return v.replace(",", "")
    return None


def _period(year: str) -> tuple[date, date]:
    m = YEAR_RE.match(year)
    if not m:
        raise UsgsParseError(f"unexpected Year {year!r}")
    y0 = int(m.group(1))
    if m.group(2) is None:
        return date(y0, 1, 1), date(y0 + 1, 1, 1)
    y1s = m.group(2)
    y1 = int(y1s) if len(y1s) == 4 else (y0 // 100) * 100 + int(y1s)
    if y1 < y0:
        raise UsgsParseError(f"Year range {year!r} ends before it starts")
    return date(y0, 1, 1), date(y1 + 1, 1, 1)


def _note(s: str) -> str:
    return s[:MAX_NOTE]


def bounded_indicator(full: str) -> str:
    """The indicator code: the verbatim label path, or (beyond the atlas 200-character limit) its first 186
    characters + ``~h=`` + 10 hex digits of its sha256 (deterministic; the full path is kept in flags)."""
    if len(full) <= 200:
        return full
    return full[:186] + "~h=" + hashlib.sha256(full.encode()).hexdigest()[:10]


def _flags(obj: dict[str, Any]) -> str:
    return json.dumps({k: v for k, v in obj.items() if v not in (None, "")}, sort_keys=True, ensure_ascii=False)


def disambiguate(inds: list[str], geos: list[str], periods: list[str]) -> set[int]:
    """Rows that still share (indicator, country, period) get ``|dup=<n>`` (n = order of appearance in the
    file within that period), applied to every period of the clashing (indicator, country) series so the
    series stays aligned. Changes ``inds`` in place; returns the affected row positions (flagged)."""
    key = Counter(zip(inds, geos, periods, strict=True))
    clashing = {(ind, g) for (ind, g, _p), n in key.items() if n > 1}
    ordinal: dict[tuple[str, str, str], int] = defaultdict(int)
    hit: set[int] = set()
    for i, (ind, g, p) in enumerate(zip(list(inds), geos, periods, strict=True)):
        if (ind, g) in clashing:
            ordinal[(ind, g, p)] += 1
            inds[i] = f"{ind}|dup={ordinal[(ind, g, p)]}"
            hit.add(i)
    return hit


@dataclass
class Report:
    dataset: str
    encoding: str = "utf-8"
    records: int = 0
    cells: int = 0
    numeric: int = 0
    text_cells: int = 0
    missing_cells: dict[str, int] = field(default_factory=dict)
    zero_dash: int = 0
    estimated: int = 0
    revised: int = 0
    ambiguous_duplicates: int = 0
    indicators: int = 0
    geos: dict[str, int] = field(default_factory=dict)
    years: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _frame(rows: list[dict[str, Any]], release: Release, raw_sha: str) -> pl.DataFrame:
    df = pl.DataFrame(
        {
            "source": [SOURCE] * len(rows),
            "dataset": [release.dataset] * len(rows),
            "indicator": [r["indicator"] for r in rows],
            "concept_id": [None] * len(rows),
            "iso3": [country_iso3(r["geo"], GEO_SCHEME) for r in rows],
            "provider_geo": [r["geo"] for r in rows],
            "entity_qid": [None] * len(rows),
            "period_start": [r["start"] for r in rows],
            "period_end": [r["end"] for r in rows],
            "value": [r.get("value") for r in rows],
            "value_text": [r.get("value_text") for r in rows],
            "unit": [r["unit"] for r in rows],
            "flags": [r["flags"] for r in rows],
            "raw_sha256": [raw_sha] * len(rows),
            "selector": [r["selector"] for r in rows],
        },
        schema=dict(ATLAS_SCHEMA),
    )
    dup = df.select(pl.struct("indicator", "provider_geo", "period_start", "period_end").is_duplicated().sum()).item()
    if dup:
        ex = df.filter(pl.struct("indicator", "provider_geo", "period_start", "period_end").is_duplicated()).head(3)
        raise UsgsParseError(f"{release.dataset}: {dup} duplicate (indicator, country, period) cells, e.g. {ex.rows()}")
    return df


def _value_cell(raw: str, codes: dict[str, str], rep: Report) -> dict[str, Any]:
    """MCS value -> {value | value_text | missing flags}."""
    num = _number(raw)
    if num is not None:
        rep.numeric += 1
        return {"value": num}
    meaning = codes.get(raw)
    if raw == "—":  # USGS: "— - zero"
        if meaning is None or "zero" not in meaning.lower():
            raise UsgsParseError("metadata no longer defines '—' as zero")
        rep.zero_dash += 1
        rep.numeric += 1
        return {"value": "0", "f": {"provider_code": raw, "provider_meaning": meaning}}
    if raw in ("W", "NA", "XX") or raw == "":
        reason = {"W": "confidential", "NA": "not_available", "XX": "not_applicable", "": "not_reported"}[raw]
        rep.missing_cells[reason] = rep.missing_cells.get(reason, 0) + 1
        f: dict[str, Any] = {"missing": reason}
        if raw:
            f.update(provider_code=raw, provider_meaning=meaning or "not defined in the archived metadata")
        return {"f": f}
    rep.text_cells += 1
    f = {"provider_code": raw, "provider_meaning": meaning} if meaning else {"qualitative": True}
    return {"value_text": raw[:4000], "f": f}


# --------------------------------------------------------------------------- MCS
MCS_COLUMNS = ("MCS chapter", "Section", "Commodity", "Country", "Statistics", "Statistics_detail", "Unit", "Year")


def parse_mcs(release: Release, body: bytes, raw_sha: str, codes: dict[str, str]) -> tuple[pl.DataFrame, Report]:
    text, enc = decode_csv(body)
    head, recs = _records(text)
    for c in (*MCS_COLUMNS, "Value", "Notes"):
        if c not in head:
            raise UsgsParseError(f"{release.data_file}: missing column {c!r} (header {head})")
    rep = Report(release.dataset, encoding=enc, records=len(recs))
    fulls = [
        f"{r['Commodity']}|{r['Section']}|{r['Statistics']}|{r['Statistics_detail']}|unit={r['Unit'] or 'not stated by USGS'}"
        for r in recs
    ]
    # e.g. Mica (Natural): the scrap-and-flake and the sheet salient-statistics blocks carry identical labels
    ambiguous = disambiguate(fulls, [r["Country"] for r in recs], [r["Year"] for r in recs])
    rep.ambiguous_duplicates = len(ambiguous)
    rows: list[dict[str, Any]] = []
    for i, (r, full) in enumerate(zip(recs, fulls, strict=True), start=1):
        unit = r["Unit"] or "not stated by USGS"
        ind = bounded_indicator(full)
        start, end = _period(r["Year"])
        cell = _value_cell(r["Value"], codes, rep)
        notes = r.get("Notes", "")
        f: dict[str, Any] = dict(cell.get("f") or {})
        if ind != full:
            f["indicator_full"] = full
        if i - 1 in ambiguous:
            f["ambiguous_duplicate"] = (
                "several rows share every key column (labels identical in the USGS file); ordinal in file order"
            )
        f["notes"] = _note(notes)
        if notes.startswith("Estimated"):
            f["estimate_kind"] = "estimated"
            rep.estimated += 1
        if "Revised" in notes or "revised" in notes:
            f["revised_note"] = True
            rep.revised += 1
        f["mcs_chapter"] = r["MCS chapter"]
        f["critical_mineral_2025"] = r.get("Is critical mineral 2025", "")
        f["other_notes"] = _note(r.get("Other notes", ""))
        if not r["Unit"]:
            f["unit_note"] = "Unit column empty in the USGS file"
        if r["Section"].startswith("Import Sources"):
            f["reporter"] = "United States"
            f["partner_role"] = "provider_geo is the source (exporting) country of U.S. imports"
        rows.append(
            {
                "indicator": ind,
                "geo": r["Country"] or "not stated",
                "start": start,
                "end": end,
                "value": cell.get("value"),
                "value_text": cell.get("value_text"),
                "unit": unit[:100],
                "flags": _flags(f),
                "selector": json.dumps({"file": release.data_file, "record": i}, sort_keys=True),
            }
        )
    return _finish(rows, release, raw_sha, rep)


# --------------------------------------------------------------------------- MYB 2024 (long)
MYB_LEVELS = (
    "Level 1 (Commodity Group)",
    "Level 2 (Commodity)",
    "Level 3 (Type)",
    "Level 4 (Phase)",
    "Level 5 (Form)",
    "Descriptor",
    "Commodity Unit",
)


def _missing_or_value(raw: str, codes: dict[str, str], rep: Report) -> tuple[str | None, dict[str, Any]]:
    if raw == NOT_KNOWN:
        rep.missing_cells["not_available"] = rep.missing_cells.get("not_available", 0) + 1
        return None, {
            "missing": "not_available",
            "provider_code": raw,
            "provider_meaning": codes.get(raw, "not defined in the archived metadata"),
        }
    num = _number(raw)
    if num is None or num.startswith("-"):
        raise UsgsParseError(f"unexpected production value {raw!r}")
    rep.numeric += 1
    return num, {}


def parse_myb_long(release: Release, body: bytes, raw_sha: str, codes: dict[str, str]) -> tuple[pl.DataFrame, Report]:
    text, enc = decode_csv(body)
    head, recs = _records(text)
    need = (*MYB_LEVELS, "ObjectID", "Country (Short Form) or Locality", "Time Period", "Timeframe", "Value", "Unit")
    for c in need:
        if c not in head:
            raise UsgsParseError(f"{release.data_file}: missing column {c!r}")
    rep = Report(release.dataset, encoding=enc, records=len(recs))
    base: list[str] = []
    for r in recs:
        if r["Timeframe"] != "Annual" or r.get("Data Type", "Production") != "Production":
            raise UsgsParseError(f"{release.data_file}: unexpected Timeframe/Data Type in ObjectID {r['ObjectID']}")
        base.append("|".join(r[c] for c in MYB_LEVELS) + f"|unit={r['Unit']}")
    # disambiguate rows that share every key column: first by Other Notes, then by ordinal (flagged)
    key = Counter((b, r["Country (Short Form) or Locality"], r["Time Period"]) for b, r in zip(base, recs, strict=True))
    clash = {(b, g) for (b, g, _y), n in key.items() if n > 1}
    inds: list[str] = []
    for b, r in zip(base, recs, strict=True):
        g = r["Country (Short Form) or Locality"]
        note = r.get("Other Notes", "")
        inds.append(f"{b}|note={note}" if (b, g) in clash and note else b)
    geo_of = [r["Country (Short Form) or Locality"] for r in recs]
    ambiguous = disambiguate(inds, geo_of, [r["Time Period"] for r in recs])
    rep.ambiguous_duplicates = len(ambiguous)
    rows: list[dict[str, Any]] = []
    for i, (full, r) in enumerate(zip(inds, recs, strict=True)):
        ind = bounded_indicator(full)
        start, end = _period(r["Time Period"])
        value, f = _missing_or_value(r["Value"], codes, rep)
        if ind != full:
            f["indicator_full"] = full
        if r.get("Value Notes") == "Estimated":
            f["estimate_kind"] = "estimated"
            rep.estimated += 1
        elif r.get("Value Notes"):
            f["value_notes"] = r["Value Notes"]
        if i in ambiguous:
            f["ambiguous_duplicate"] = (
                "several rows share every key column and carry no distinguishing note; ordinal in file order"
            )
        f.update(
            other_notes=_note(r.get("Other Notes", "")),
            source_1=_note(r.get("Source 1", "")),
            source_2=_note(r.get("Source 2", "")),
            currentness_date=r.get("Currentness Date", ""),
            report=r.get("Country Report Title", ""),
            commodity_unit_classification=r.get("Commodity Unit Classification", ""),
        )
        rows.append(
            {
                "indicator": ind,
                "geo": r["Country (Short Form) or Locality"],
                "start": start,
                "end": end,
                "value": value,
                "value_text": None,
                "unit": r["Unit"][:100] or "not stated by USGS",
                "flags": _flags(f),
                "selector": json.dumps(
                    {"file": release.data_file, "record": i + 1, "ObjectID": r["ObjectID"]}, sort_keys=True
                ),
            }
        )
    return _finish(rows, release, raw_sha, rep)


# --------------------------------------------------------------------------- MYB 2019 (wide)
def parse_myb_wide(release: Release, body: bytes, raw_sha: str, codes: dict[str, str]) -> tuple[pl.DataFrame, Report]:
    text, enc = decode_csv(body)
    head, recs = _records(text)
    years = sorted(int(m.group(1)) for h in head if (m := re.match(r"^([0-9]{4}) Production$", h)))
    if not years:
        raise UsgsParseError(f"{release.data_file}: no '<year> Production' columns")
    for c in ("Country", "Commodity", "Commodity Product", "Commodity Type", "Units", *(f"{y} Footnotes" for y in years)):
        if c not in head:
            raise UsgsParseError(f"{release.data_file}: missing column {c!r}")
    rep = Report(release.dataset, encoding=enc, records=len(recs))
    rows: list[dict[str, Any]] = []
    for i, r in enumerate(recs, start=1):
        full = f"{r['Commodity']}|{r['Commodity Product']}|{r['Commodity Type']}|unit={r['Units']}"
        ind = bounded_indicator(full)
        for y in years:
            raw = r[f"{y} Production"]
            if raw == "":
                continue
            value, f = _missing_or_value(raw, codes, rep)
            if ind != full:
                f["indicator_full"] = full
            foot = r[f"{y} Footnotes"]
            if foot:
                f["footnote"] = _note(foot)
                if "Estimated" in foot:
                    f["estimate_kind"] = "estimated"
                    rep.estimated += 1
                if "Revised" in foot:
                    f["revised"] = True
                    rep.revised += 1
                if "Preliminary" in foot:
                    f["preliminary"] = True
            f.update(
                commodity_footnotes=_note(r.get("Commodity Footnotes", "")),
                chapter=r.get("Chapter Title", ""),
                source_publication_date=r.get("Source Publication Date", ""),
            )
            rows.append(
                {
                    "indicator": ind,
                    "geo": r["Country"],
                    "start": date(y, 1, 1),
                    "end": date(y + 1, 1, 1),
                    "value": value,
                    "value_text": None,
                    "unit": r["Units"][:100] or "not stated by USGS",
                    "flags": _flags(f),
                    "selector": json.dumps(
                        {"file": release.data_file, "record": i, "column": f"{y} Production"}, sort_keys=True
                    ),
                }
            )
    return _finish(rows, release, raw_sha, rep)


def _finish(rows: list[dict[str, Any]], release: Release, raw_sha: str, rep: Report) -> tuple[pl.DataFrame, Report]:
    if not rows:
        raise UsgsParseError(f"{release.data_file}: no cell")
    df = _frame(rows, release, raw_sha)
    rep.cells = df.height
    rep.indicators = df["indicator"].n_unique()
    rep.geos = dict(Counter(df["provider_geo"].to_list()))
    rep.years = [int(df["period_start"].min().year), int(df["period_start"].max().year)]  # type: ignore[union-attr]
    return df, rep


PARSERS = {"mcs": parse_mcs, "myb_long": parse_myb_long, "myb_wide": parse_myb_wide}


def parse_release(
    release: Release, body: bytes, raw_sha: str, meta: dict[str, Any]
) -> tuple[pl.DataFrame, Report]:
    return PARSERS[release.kind](release, body, raw_sha, dict(meta.get("value_codes") or {}))


def coverage_note(release: Release, rep: Report, item: dict[str, Any], meta: dict[str, Any]) -> str:
    unm = sorted(g for g in rep.geos if country_iso3(g, GEO_SCHEME) is None)
    parts = [
        f"USGS ScienceBase item {release.item_id} '{item.get('title')}' ({release.doi}), file {release.data_file}"
        f" ({rep.encoding}); {rep.records} records -> {rep.cells} cells, {rep.indicators} indicators,"
        f" years {rep.years[0]}-{rep.years[1]}.",
        f"Value codes from the archived FGDC metadata: {json.dumps(meta.get('value_codes'), ensure_ascii=False)}.",
        f"Value-less cells (flags.missing, never zero): {rep.missing_cells}; '—' zero cells {rep.zero_dash};"
        f" value_text cells {rep.text_cells}; estimated {rep.estimated}; revised {rep.revised};"
        f" ambiguous duplicates {rep.ambiguous_duplicates}.",
        f"Unmapped geographies (kept): {unm}.",
        "Licence: U.S. public domain / CC0 1.0. Cite: " + str(item.get("citation") or release.doi),
    ]
    return " ".join(parts)[:4000]


def rights(release: Release, item: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "licence": "CC0-1.0 (U.S. public domain)",
        "licence_text": (meta.get("use_constraints") or item.get("rights") or "")[:600],
        "attribution": str(item.get("citation") or release.doi),
        "provider": "U.S. Geological Survey, National Minerals Information Center",
        "licence_checked": "2026-10-15",
        "source_key": SOURCE_KEY,
    }


# --------------------------------------------------------------------------- ingest
async def atlas_ingest(run: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"tables": {}}
    for rel in RELEASES:
        if not run.wants(rel.dataset):
            continue
        raw_item = await run.get(rel.item_url, key=f"item:{rel.item_id}", media_types=("application/json",))
        item_sha = run.archive(raw_item)
        item = parse_item(raw_item.body, rel)
        raw_meta = await run.get(
            item["files"][rel.meta_file]["url"], key=f"meta:{rel.meta_file}", media_types=XML_MEDIA, max_bytes=2_000_000
        )
        meta_sha = run.archive(raw_meta)
        meta = parse_metadata(raw_meta.body)
        if not licence_ok(item, meta):
            raise UsgsParseError(f"{rel.dataset}: archived item/metadata no longer state CC0 / public domain")
        raw = await run.get(
            item["files"][rel.data_file]["url"],
            key=f"data:{rel.data_file}",
            media_types=CSV_MEDIA,
            max_bytes=rel.max_bytes,
            params={"item": rel.item_id, "file": rel.data_file},
        )
        sha = run.archive(raw)
        frame, rep = parse_release(rel, raw.body, sha, meta)
        reg = run.register(
            dataset=rel.dataset,
            frame=frame,
            raw_sha256s=[sha, item_sha, meta_sha],
            geo_scheme=GEO_SCHEME,
            coverage_note=coverage_note(rel, rep, item, meta),
            rights=rights(rel, item, meta),
        )
        out["tables"][rel.dataset] = {
            "dataset_key": reg["dataset_key"],
            "rows": reg["row_count"],
            "countries": reg["coverage"]["countries"],
            "created": reg["created"],
            "report": {k: v for k, v in rep.as_dict().items() if k != "geos"},
            "unmapped": [u["provider_geo"] for u in reg.get("unmapped_provider_geo", [])],
        }
    return out


__all__ = [
    "CONNECTOR_NAME",
    "RELEASES",
    "RELEASES_BY_DATASET",
    "Release",
    "UsgsParseError",
    "atlas_ingest",
    "atlas_source",
    "parse_item",
    "parse_metadata",
    "parse_release",
]
