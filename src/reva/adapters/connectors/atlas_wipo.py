"""WIPO IP statistics bulk files and Global Innovation Index -> atlas tables (V3, agent A-SCI).

Sources (checked 2026-10-15)
----------------------------
* **WIPO IP Statistics, "Complete data sets for Indicators by class and by technology"**
  (https://www.wipo.int/en/web/ip-statistics, section "Additional IP data": "datasets too large to be
  downloaded from the IP Statistics Data Center"). Three ZIPs of CSVs ``year,office,origin,<id>,count``:

  ======================================================  ===================================  ==============
  file                                                    dimension                            atlas dataset
  ======================================================  ===================================  ==============
  ``dc_indicator_patent_4_publication_by_technology.csv`` 35 WIPO technology fields (+0)        ``patent_pub_tech``
  ``dc_indicator_patent_5_grant_by_technology.csv``       35 WIPO technology fields (+0)        ``patent_grant_tech``
  ``dc_indicator_trademark_3_application_by_class.csv``   45 Nice classes (+0)                  ``tm_appl_class``
  ``dc_indicator_trademark_4_registration_by_class.csv``  45 Nice classes (+0)                  ``tm_reg_class``
  ``dc_indicator_design_3_application_by_class.csv``      32 Locarno classes (+0)               ``design_appl_class``
  ``dc_indicator_design_4_registration_by_class.csv``     32 Locarno classes (+0)               ``design_reg_class``
  ======================================================  ===================================  ==============

  The CSVs are dated 2023-12-06 (data years 2000/2004 .. 2022): WIPO has not refreshed these bulk
  files since; newer years exist only in the interactive Data Center, which has no documented open
  bulk API (not used). ``office`` is the receiving IP office (ISO 3166 alpha-2 or a regional office
  code: ``EP``, ``EA``, ``AP``, ``OA``, ``GC``, ``BX``…), ``origin`` the applicant's country of origin
  (WIPO: residence of the first-named applicant). Id ``0`` is "Unknown" (the field-name table in the
  patent ZIP says so for technology; assumed alike for classes and flagged). The code ``**`` appears as
  office and as origin and is **not documented** in the download: kept verbatim, flagged
  ``code_undocumented``, never mapped to a country.

  Atlas cells: ``provider_geo`` = origin code verbatim (``iso2`` scheme; historical codes such as ``YU``,
  ``CS``, ``SU`` and ``**`` stay unmapped), indicator = ``<measure>|office=<office>|<tech|class>=<id>``,
  value = the reported count. Nothing is summed: technology counts may overlap and trademark/design
  counts are per class (WIPO "class counts"), so totals across ids or offices are **not** the number of
  filings. Total filings by origin (resident + abroad) are not in these files; the World Bank WDI
  series ``IP.PAT.RESD``/``IP.PAT.NRES``/``IP.TMK.*``/``IP.IDS.*`` (WIPO as source, by office) are A-WB's.

* **Global Innovation Index 2025** data workbook (``wipo-pub-2000-2025-tech1.xlsx``, linked as
  "Download Data" from https://www.wipo.int/en/web/global-innovation-index/2025/index). Only the
  WIPO-computed composite levels (Index, Sub-Index, Pillar, Sub-Pillar: score and rank) are kept;
  the 78 indicator-level values come from third parties (S&P Global, ITU, UNESCO…) with their own
  terms and are not ingested. Period = the edition year (data years of components vary).

Rights: WIPO's Open Access Policy — "new WIPO online publications and other online content are issued
under an Attribution 4.0 International CC license (CC BY 4.0)" except content published under more
restrictive terms (https://www.wipo.int/tools/en/disclaim.html). The downloads carry no file-level
licence; this site-level statement is recorded as ``licence_basis``.
"""

from __future__ import annotations

import csv
import io
import re
import sys
import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from xml.etree import ElementTree as ET

from reva.adapters.connectors.atlas_sci_common import atlas_spec, canon, code_digest
from reva.adapters.connectors.base import to_raw
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import (
    ConnectorSpec,
    Cursor,
    FetchBudget,
    FetchItem,
    FetchPlan,
    ObservationBatch,
    RawAcquisition,
)

CONNECTOR_IPSTATS = "atlas.wipo_ipstats"
CONNECTOR_GII = "atlas.wipo_gii"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas-wipo-parse-1"
SOURCE_KEY_IPSTATS = "wipo.ipstats_bulk"
SOURCE_KEY_GII = "wipo.gii"
ATLAS_SOURCE = "wipo"
HOST = "www.wipo.int"
MAX_ZIP_BYTES = 40_000_000
MAX_XLSX_BYTES = 10_000_000
MAX_MEMBER_BYTES = 200_000_000  # uncompressed cap per ZIP member (decompression-bomb guard)

RIGHTS: dict[str, str] = {
    "licence": "CC-BY-4.0",
    "licence_basis": "WIPO Open Access Policy (site-level; no file-level licence in the download)",
    "licence_url": "https://www.wipo.int/tools/en/disclaim.html",
    "attribution": "World Intellectual Property Organization (WIPO), IP Statistics Data Center",
}
GII_RIGHTS: dict[str, str] = {
    **RIGHTS,
    "attribution": "WIPO, Global Innovation Index 2025 (composite scores and ranks only)",
    "third_party_excluded": "indicator-level values from third-party sources are not ingested",
}


@dataclass(frozen=True)
class BulkFile:
    zip_key: str
    member: str
    dataset: str
    measure: str  # indicator prefix
    id_column: str  # tec_id | class_id
    id_name: str  # tech | class
    unit: str
    first_year: int


ZIPS: dict[str, str] = {
    "patent": f"https://{HOST}/documents/d/ip-statistics/wipo-data-patent-indicators.zip",
    "trademark": f"https://{HOST}/documents/d/ip-statistics/wipo-data-trademark-indicators.zip",
    "design": f"https://{HOST}/documents/d/ip-statistics/docs-en-wipo-data-design-indicators.zip",
}
GII_URL = f"https://{HOST}/edocs/pubdocs/en/wipo-pub-2000-2025-tech1.xlsx"
GII_EDITION = 2025

BULK_FILES: tuple[BulkFile, ...] = (
    BulkFile(
        "patent",
        "dc_indicator_patent_4_publication_by_technology.csv",
        "patent_pub_tech",
        "patent_publications",
        "tec_id",
        "tech",
        "patent publications",
        2000,
    ),
    BulkFile(
        "patent",
        "dc_indicator_patent_5_grant_by_technology.csv",
        "patent_grant_tech",
        "patent_grants",
        "tec_id",
        "tech",
        "patent grants",
        2000,
    ),
    BulkFile(
        "trademark",
        "dc_indicator_trademark_3_application_by_class.csv",
        "tm_appl_class",
        "trademark_application_class_count",
        "class_id",
        "class",
        "class count (applications)",
        2004,
    ),
    BulkFile(
        "trademark",
        "dc_indicator_trademark_4_registration_by_class.csv",
        "tm_reg_class",
        "trademark_registration_class_count",
        "class_id",
        "class",
        "class count (registrations)",
        2004,
    ),
    BulkFile(
        "design",
        "dc_indicator_design_3_application_by_class.csv",
        "design_appl_class",
        "design_application_class_count",
        "class_id",
        "class",
        "class count (applications)",
        2000,
    ),
    BulkFile(
        "design",
        "dc_indicator_design_4_registration_by_class.csv",
        "design_reg_class",
        "design_registration_class_count",
        "class_id",
        "class",
        "class count (registrations)",
        2000,
    ),
)
TECH_NAMES_MEMBER = "patent_technology_field_names.xlsx"
UNDOCUMENTED_CODES = frozenset({"**"})
#: Origin codes are ISO 3166 alpha-2 (historical AN/CS/DD/YU and "**" stay unmapped).
GEO_SCHEME = "iso2"
_CODE = re.compile(r"^(\*\*|[A-Z][A-Z0-9])$")
_INT = re.compile(r"^(0|[1-9][0-9]*)$")


class WipoParseError(SourceResponseError):
    pass


def _err(msg: str, kind: str = "schema_drift") -> WipoParseError:
    return WipoParseError(msg, retryable=False, kind=kind)


def _module() -> Any:
    return sys.modules[__name__]


# ----------------------------------------------------------------------------- connectors
class _Base:
    name: str
    source_key: str
    dataset_ids: tuple[str, ...]

    def _spec(self, **kw: Any) -> ConnectorSpec:
        return atlas_spec(
            name=self.name,
            version=CONNECTOR_VERSION,
            source_key=self.source_key,
            provider="World Intellectual Property Organization (WIPO)",
            licence="CC-BY-4.0",
            allowed_hosts=(HOST,),
            dataset_ids=self.dataset_ids,
            geography=("*",),
            lineage_family="wipo",
            cadence_seconds=30 * 86400,
            release_aware=False,
            revision_strategy="full file per run; new bytes -> new atlas table version",
            max_pages=1,
            parser_version=PARSER_VERSION,
            code_digest=code_digest(_module()),
            **kw,
        )

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition:
        result = await http.get(item.url, max_bytes=item.max_bytes)
        return to_raw(item, result, getattr(http, "origin", "live"))

    def parse(self, raw: RawAcquisition) -> Iterator[ObservationBatch]:
        return iter(())  # atlas connector: no evidence observations


class WipoIpStatsConnector(_Base):
    name = CONNECTOR_IPSTATS
    source_key = SOURCE_KEY_IPSTATS
    dataset_ids = tuple(b.dataset for b in BULK_FILES)

    def describe(self) -> ConnectorSpec:
        return self._spec(
            field_mappings={"atlas": [f"{ATLAS_SOURCE}.{b.dataset}" for b in BULK_FILES]},
            units={b.dataset: b.unit for b in BULK_FILES},
            time_semantics="calendar year of publication/grant/application as reported by the office",
            max_response_bytes=MAX_ZIP_BYTES,
        )

    def plan(self, cursor: Cursor | None = None, budget: FetchBudget | None = None) -> FetchPlan:
        return FetchPlan(
            connector=self.name,
            items=tuple(
                FetchItem(
                    key=f"zip:{k}",
                    url=u,
                    expected_media_types=("application/zip",),
                    max_bytes=MAX_ZIP_BYTES,
                    params={"zip": k},
                )
                for k, u in ZIPS.items()
            ),
            pagination="none (whole files)",
            availability_expectation="bulk files dated 2023-12; WIPO refreshes them irregularly",
        )


class WipoGiiConnector(_Base):
    name = CONNECTOR_GII
    source_key = SOURCE_KEY_GII
    dataset_ids = (f"gii_{GII_EDITION}",)

    def describe(self) -> ConnectorSpec:
        return self._spec(
            field_mappings={
                "atlas": f"{ATLAS_SOURCE}.gii_{GII_EDITION}",
                "levels": ["Index", "SubIndex", "Pillar", "SubPillar"],
            },
            units={"score": "score 0-100", "rank": "rank"},
            time_semantics="GII edition year; component data years vary",
            max_response_bytes=MAX_XLSX_BYTES,
        )

    def plan(self, cursor: Cursor | None = None, budget: FetchBudget | None = None) -> FetchPlan:
        return FetchPlan(
            connector=self.name,
            items=(
                FetchItem(
                    key=f"gii:{GII_EDITION}",
                    url=GII_URL,
                    expected_media_types=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",),
                    max_bytes=MAX_XLSX_BYTES,
                ),
            ),
            pagination="none (one workbook)",
            availability_expectation="one edition per year (September)",
        )


# ----------------------------------------------------------------------------- zip helpers
def open_zip(body: bytes) -> zipfile.ZipFile:
    try:
        zf = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as e:
        raise _err(f"not a ZIP archive ({e})") from None
    for info in zf.infolist():
        if info.file_size > MAX_MEMBER_BYTES:
            raise _err(f"ZIP member {info.filename!r} expands to {info.file_size} bytes (> cap)", kind="oversized")
    return zf


def bulk_members(zip_key: str) -> tuple[BulkFile, ...]:
    return tuple(b for b in BULK_FILES if b.zip_key == zip_key)


def iter_bulk_csv(zf: zipfile.ZipFile, bf: BulkFile) -> Iterator[tuple[int, int, str, str, str, str]]:
    """``(row_number, year, office, origin, id, count)``; row_number is 1-based over data rows."""
    try:
        raw = zf.read(bf.member)
    except KeyError:
        raise _err(f"{bf.member} missing from the {bf.zip_key} ZIP") from None
    text = raw.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text, newline=""))
    header = [h.strip() for h in next(reader, [])]
    want = ["year", "office", "origin", bf.id_column, "count"]
    if header != want:
        raise _err(f"{bf.member}: header {header} != {want}")
    for n, rec in enumerate(reader, start=1):
        if not rec:
            continue
        if len(rec) != 5:
            raise _err(f"{bf.member} row {n}: {len(rec)} fields")
        year, office, origin, ident, count = (x.strip() for x in rec)
        if not (year.isdigit() and len(year) == 4):
            raise _err(f"{bf.member} row {n}: year {year!r}")
        if not _CODE.match(office) or not _CODE.match(origin):
            raise _err(f"{bf.member} row {n}: office/origin code {office!r}/{origin!r}")
        if not _INT.match(ident) or not _INT.match(count):
            raise _err(f"{bf.member} row {n}: id/count {ident!r}/{count!r}")
        yield n, int(year), office, origin, ident, count


def bulk_frame(zf: zipfile.ZipFile, bf: BulkFile, *, raw_sha256: str, retrieved_at: datetime) -> Any:
    """Atlas frame for one CSV (columnar build; millions of rows)."""
    import polars as pl

    from reva.atlas.tables import ATLAS_SCHEMA

    rows, years, offices, origins, ids, counts = [], [], [], [], [], []
    for n, y, off, org, ident, cnt in iter_bulk_csv(zf, bf):
        rows.append(n)
        years.append(y)
        offices.append(off)
        origins.append(org)
        ids.append(ident)
        counts.append(cnt)
    if not rows:
        raise _err(f"{bf.member}: no data rows", kind="incomplete")
    base_flags = {"retrieved_at": retrieved_at.isoformat(), "licence": RIGHTS["licence"], "file_dated": "2023-12-06"}
    flag_variants = {}
    for und_office in (False, True):
        for und_origin in (False, True):
            for unknown_id in (False, True):
                f: dict[str, Any] = dict(base_flags)
                und = [w for w, b in (("office", und_office), ("origin", und_origin)) if b]
                if und:
                    f["code_undocumented"] = {"code": "**", "in": und}
                if unknown_id:
                    f["id_0"] = (
                        "Unknown (WIPO field-name table)"
                        if bf.id_name == "tech"
                        else "Unknown (assumed as for technology)"
                    )
                if bf.id_name == "class":
                    f["class_count"] = "counts per class; one filing may designate several classes"
                flag_variants[(und_office, und_origin, unknown_id)] = canon(f)
    df = pl.DataFrame(
        {"n": rows, "year": years, "office": offices, "origin": origins, "id": ids, "count": counts},
        schema={
            "n": pl.Int64,
            "year": pl.Int32,
            "office": pl.String,
            "origin": pl.String,
            "id": pl.String,
            "count": pl.String,
        },
    )
    key = (
        pl.col("office").eq("**").cast(pl.String)
        + "|"
        + pl.col("origin").eq("**").cast(pl.String)
        + "|"
        + pl.col("id").eq("0").cast(pl.String)
    )
    fmap = {f"{str(a).lower()}|{str(b).lower()}|{str(c).lower()}": v for (a, b, c), v in flag_variants.items()}
    out = df.select(
        pl.lit(ATLAS_SOURCE).alias("source"),
        pl.lit(bf.dataset).alias("dataset"),
        (pl.lit(f"{bf.measure}|office=") + pl.col("office") + pl.lit(f"|{bf.id_name}=") + pl.col("id")).alias(
            "indicator"
        ),
        pl.lit(None, dtype=pl.String).alias("concept_id"),
        pl.lit(None, dtype=pl.String).alias("iso3"),
        pl.col("origin").alias("provider_geo"),
        pl.lit(None, dtype=pl.String).alias("entity_qid"),
        pl.date(pl.col("year"), 1, 1).alias("period_start"),
        pl.date(pl.col("year") + 1, 1, 1).alias("period_end"),
        pl.col("count").alias("value"),
        pl.lit(None, dtype=pl.String).alias("value_text"),
        pl.lit(bf.unit).alias("unit"),
        key.replace_strict(fmap, return_dtype=pl.String).alias("flags"),
        pl.lit(raw_sha256).alias("raw_sha256"),
        (pl.lit('{"file":"' + bf.member + '","row":') + pl.col("n").cast(pl.String) + pl.lit("}")).alias("selector"),
    )
    return out.select([pl.col(c).cast(t) for c, t in ATLAS_SCHEMA.items()])


def tech_field_names(zf: zipfile.ZipFile) -> dict[str, dict[str, str]]:
    """``{id: {"en": ..., "fr": ...}}`` from the patent ZIP's field-name workbook."""
    sheets = read_xlsx(zf.read(TECH_NAMES_MEMBER))
    rows = next(iter(sheets.values()))
    header = rows[0]
    if header[:3] != ["class_id", "field_en", "field_fr"]:
        raise _err(f"{TECH_NAMES_MEMBER}: header {header[:3]}")
    return {r[0]: {"en": (r[1] or "").strip(), "fr": (r[2] or "").strip()} for r in rows[1:] if r and r[0] is not None}


# ----------------------------------------------------------------------------- minimal xlsx reader
_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_CELL_REF = re.compile(r"^([A-Z]{1,3})([0-9]+)$")


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


_FORBIDDEN_XML = (b"<!DOCTYPE", b"<!ENTITY", b"<!doctype", b"<!entity")


def _xml(data: bytes) -> ET.Element:
    """Parse one workbook part; DTDs/entity declarations (never present in xlsx) are refused first."""
    if any(t in data for t in _FORBIDDEN_XML):
        raise _err("xlsx part declares a DTD/entities (refused)", kind="unsafe_xml")
    try:
        return ET.fromstring(data)  # noqa: S314 - DTDs/entities refused above; no external resolution in ET
    except ET.ParseError as e:
        raise _err(f"xlsx part is not well-formed XML ({e})") from None


def read_xlsx(body: bytes, sheets: Sequence[str] | None = None) -> dict[str, list[list[str | None]]]:
    """Cell *text as stored* per sheet (numbers keep their stored decimal text; no float round-trip).

    Supports shared strings, inline strings, booleans and numbers (what spreadsheet exports use);
    formulas yield their cached value. Raises :class:`WipoParseError` on anything else.
    """
    zf = open_zip(body)
    try:
        wb = _xml(zf.read("xl/workbook.xml"))
        rels = _xml(zf.read("xl/_rels/workbook.xml.rels"))
    except KeyError as e:
        raise _err(f"xlsx part missing: {e}") from None
    target = {r.get("Id"): r.get("Target") for r in rels}
    shared: list[str] = []
    if "xl/sharedStrings.xml" in zf.namelist():
        for si in _xml(zf.read("xl/sharedStrings.xml")).findall("m:si", _NS):
            shared.append("".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t")))
    out: dict[str, list[list[str | None]]] = {}
    sheets_el = wb.find("m:sheets", _NS)
    for sh in list(sheets_el) if sheets_el is not None else []:
        name = sh.get("name") or ""
        if sheets is not None and name not in sheets:
            continue
        path = target.get(sh.get(_REL_NS))
        if not path:
            raise _err(f"xlsx: sheet {name!r} has no target")
        path = path.lstrip("/")
        path = path if path.startswith("xl/") else f"xl/{path}"
        rows: list[list[str | None]] = []
        for row in _xml(zf.read(path)).iterfind(".//m:sheetData/m:row", _NS):
            vals: list[str | None] = []
            for c in row.findall("m:c", _NS):
                m = _CELL_REF.match(c.get("r") or "")
                idx = _col_index(m.group(1)) if m else len(vals)
                while len(vals) < idx:
                    vals.append(None)
                t = c.get("t")
                v = c.find("m:v", _NS)
                if t == "s":
                    val: str | None = shared[int(v.text)] if v is not None and v.text is not None else None
                elif t == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter(f"{{{_NS['m']}}}t"))
                elif t in (None, "n", "str", "b", "e"):
                    val = v.text if v is not None else None
                    if t == "e":
                        val = None
                else:
                    raise _err(f"xlsx: unsupported cell type {t!r}")
                vals.append(val)
            rows.append(vals)
        out[name] = rows
    return out


# ----------------------------------------------------------------------------- GII
GII_LEVELS = ("Index", "SubIndex", "Pillar", "SubPillar")
_DEC = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]{1,3})?$")


def _dec(s: str | None) -> str | None:
    if s is None or s == "":
        return None
    s = s.strip()
    if not _DEC.match(s):
        raise _err(f"GII: value {s!r} is not a decimal")
    return s


def gii_concept_id(num: str, measure: str) -> str:
    return "sci.gii." + ("index" if num == "GII" else num.lower().replace(".", "_")) + "." + measure


def gii_rows(body: bytes, *, raw_sha256: str, retrieved_at: datetime) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    sh = read_xlsx(body, sheets=("Index Structure", "Data"))
    if set(sh) != {"Index Structure", "Data"}:
        raise _err(f"GII workbook sheets {sorted(sh)}")
    struct = sh["Index Structure"]
    if struct[0][:3] != ["NUM", "NAME", "LEVEL"]:
        raise _err(f"GII Index Structure header {struct[0][:3]}")
    levels: dict[str, tuple[str, str]] = {}
    for r in struct[1:]:
        if len(r) >= 3 and r[2]:
            levels[r[0] or "GII"] = (r[2], r[1] or "")
    data = sh["Data"]
    hdr = data[0]
    want = [
        "ISO3",
        "ECONOMY_NAME",
        "NUM",
        "NAME",
        "DATAYR",
        "VALUE_SCREEN",
        "SCORE",
        "RANK",
        "SW_OVERALL",
        "SW_INCGRP",
        "OUTDATED",
        "DMC",
    ]
    if hdr[: len(want)] != want:
        raise _err(f"GII Data header {hdr}")
    period = (date(GII_EDITION, 1, 1), date(GII_EDITION + 1, 1, 1))
    rows: list[dict[str, Any]] = []
    economies: set[str] = set()
    for i, r in enumerate(data[1:], start=2):
        r = r + [None] * (len(want) - len(r))
        iso, num = r[0], r[2] or "GII"
        level = levels.get(num, ("", ""))[0]
        if level not in GII_LEVELS or not iso:
            continue
        economies.add(iso)
        base = {
            "edition": f"GII {GII_EDITION}",
            "level": level,
            "name_en": r[3],
            "period_semantics": "edition year; component data years vary",
            "retrieved_at": retrieved_at.isoformat(),
            "licence": RIGHTS["licence"],
        }
        if r[11]:
            base["dmc_not_met"] = r[11]  # data minimum coverage requirement not met
        for measure, col, unit in (("score", 6, "score 0-100"), ("rank", 7, "rank")):
            val = _dec(r[col])
            if val is None:
                continue
            if measure == "rank" and "." in val:
                val = val.rstrip("0").rstrip(".") if val.endswith(".0") else val
            rows.append(
                {
                    "source": ATLAS_SOURCE,
                    "dataset": f"gii_{GII_EDITION}",
                    "indicator": f"{num}|measure={measure}",
                    "concept_id": gii_concept_id(num, measure) if level != "SubPillar" else None,
                    "iso3": None,
                    "provider_geo": iso,
                    "entity_qid": None,
                    "period_start": period[0],
                    "period_end": period[1],
                    "value": val,
                    "value_text": None,
                    "unit": unit,
                    "flags": canon(base),
                    "raw_sha256": raw_sha256,
                    "selector": canon(
                        {
                            "file": "wipo-pub-2000-2025-tech1.xlsx",
                            "sheet": "Data",
                            "row": i,
                            "column": "SCORE" if measure == "score" else "RANK",
                        }
                    ),
                }
            )
    return rows, {"economies": len(economies), "levels": {k: v for k, v in levels.items() if v[0] in GII_LEVELS}}


__all__ = [
    "BULK_FILES",
    "CONNECTOR_GII",
    "CONNECTOR_IPSTATS",
    "GEO_SCHEME",
    "GII_URL",
    "ZIPS",
    "WipoGiiConnector",
    "WipoIpStatsConnector",
    "bulk_frame",
    "bulk_members",
    "gii_rows",
    "iter_bulk_csv",
    "open_zip",
    "read_xlsx",
    "tech_field_names",
]
