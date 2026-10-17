"""UN Statistics Division classifications -> KG classification codes, hierarchies and concordances (V4, agent V4LINK).

Why: a BACI trade flow is coded in HS (``atlas.baci`` ``hs92_pair_hs2``: ``X|partner=<j>|hs2=27``), production and
companies in ISIC / NACE, products in CPC, long trade series in SITC. The UNSD correspondence tables are the
published bridges between them, so that "country exports HS 27 -> HS 2709 crude petroleum -> CPC 12010 -> ISIC
0610 extraction of crude petroleum -> the companies and facilities classified there" is a chain of sourced hops.

Source (all from https://unstats.un.org/unsd/classifications/Econ, no account; fetched through ``run.get``,
budget key ``unsd.classifications``, every file archived raw):

======================  ==========================================================================  ============
key                     file                                                                        used for
======================  ==========================================================================  ============
``hs_codes``            ``download/In Text/HSCodeandDescription.xlsx`` (sheets HS22, HS17, HS92)     HS codes
``sitc_codes``          ``Download/In Text/SITCCodeandDescription.xlsx`` (sheet SITC4)              SITC Rev.4
``cpc21_structure``     ``Download/In Text/CPC_Ver_2_1_english_structure.txt``                      CPC 2.1
``isic4_structure``     ``Download/In Text/ISIC_Rev_4_english_structure.Txt``                       ISIC Rev.4
``hs2017_cpc21``        ``tables/CPC/CPCv21_HS2017/CPC21-HS2017.csv``                               HS17 -> CPC
``cpc21_isic4``         ``tables/CPC/CPCv21_ISIC4/cpc21-isic4.txt`` (+ its ``readme.txt``)          CPC -> ISIC
``hs2022_hs2017``       ``tables/HS2022toHS2017ConversionAndCorrelationTables.xlsx``                HS22 -> HS17
``hs2022_hs1992``       ``tables/HS2022toHS1992ConversionAndCorrelationTables.xlsx``                HS22 -> HS92
``hs2022_sitc4``        ``tables/HS2022toSITC4ConversionAndCorrelationTables.xlsx``                 HS22 -> SITC4
======================  ==========================================================================  ============

KG (source key ``unsd.classifications``; predicates in :mod:`reva.adapters.connectors.v4link_common`):

* entities ``hs2022:<code>``, ``hs2017:<code>``, ``hs1992:<code>`` (kind ``hs_code``: 2-digit chapters, 4-digit
  headings, 6-digit subheadings), ``cpc21:<code>`` (``cpc_code``: sections .. subclasses), ``isic4:<code>``
  (``isic_code``: sections A-U, divisions, groups, classes), ``sitc4:<code>`` (``sitc_code``); English label =
  the description verbatim; no other language, nothing translated.
* ``v4link:class_parent`` child -> parent: HS and SITC from the file's own ``Parent Code`` column; CPC and ISIC from
  the classification's code structure as published (CPC 2.1: each level adds one digit to its parent's code,
  section 1 digit .. subclass 5 digits; ISIC Rev.4: group = first 3 digits of a class, division = first 2 digits
  of a group, and a division belongs to the section row that precedes it in the structure file). A computed parent
  that is not itself a row of the file is reported (``parent_missing``), never invented.
* ``v4link:corresponds_to`` from the first code of a row to the second, one statement per distinct pair of a table,
  qualifiers verbatim: the CPC tables' ``partial`` flags (columns ``HS partial`` / ``CPC partial``,
  ``CPC21partial`` / ``ISIC4partial``; the files do not define them -- UNSD's convention is 1 = only part of the
  code is covered by the correspondence) and the HS tables' ``Relationship`` (``1:1``, ``1:n``, ``n:1``, ``n:n``)
  from the *Correlations* sheet, plus ``unsd_conversion: true`` when the pair is also the row of UNSD's one-way
  *Conversions* sheet. HS codes written ``2709.00`` in the CPC table are the same 6-digit code without the dot.
  A row whose code is not in the loaded structure of its classification is reported (``code_not_in_structure``)
  and not loaded. No weights, no shares, no inference across tables (a chain HS -> CPC -> ISIC is two hops, each
  with its own table).

Licence: UNSD publishes these files freely on its site under the United Nations terms of use
(https://www.un.org/en/about-us/terms-of-use). The HS descriptions reproduce the Harmonized System nomenclature,
(c) World Customs Organization: UNSD states no separate licence for redistributing them, so every HS entity and
statement is flagged ``redistribution: unverified (HS nomenclature (c) WCO)``. Codes and correspondences are
facts of the classification.
"""

from __future__ import annotations

import csv
import hashlib
import inspect
import io
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from reva.adapters.connectors import v4link_common as common
from reva.adapters.connectors.v4link_common import P_CLASS_PARENT, P_CORRESPONDS
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

NAME = "atlas.unsd_classif"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_unsd_classif/1"
SOURCE_KEY = "unsd.classifications"
ATLAS_SOURCE = "unsd_classif"
DATASET = "classifications"
HOST = "unstats.un.org"
BASE = f"https://{HOST}/unsd/classifications/Econ/"
DAY = 86400
MAX_BYTES = 8_000_000
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
TEXT = ("text/plain", "text/csv", "application/octet-stream")

PROVIDER = "United Nations Statistics Division (UNSD), Classifications on economic statistics"
LICENCE = (
    "UNSD files, free download under the United Nations terms of use (https://www.un.org/en/about-us/terms-of-use); "
    "HS descriptions reproduce the Harmonized System nomenclature (c) World Customs Organization -- redistribution "
    "licence not stated (flagged)"
)
HS_FLAG = "unverified (HS nomenclature (c) WCO; UNSD states no redistribution licence)"


@dataclass(frozen=True)
class SourceFile:
    key: str
    path: str  # relative to BASE, URL-encoded
    kind: str  # xlsx | text

    @property
    def url(self) -> str:
        return BASE + self.path


FILES: tuple[SourceFile, ...] = (
    SourceFile("hs_codes", "download/In%20Text/HSCodeandDescription.xlsx", "xlsx"),
    SourceFile("sitc_codes", "Download/In%20Text/SITCCodeandDescription.xlsx", "xlsx"),
    SourceFile("cpc21_structure", "Download/In%20Text/CPC_Ver_2_1_english_structure.txt", "text"),
    SourceFile("isic4_structure", "Download/In%20Text/ISIC_Rev_4_english_structure.Txt", "text"),
    SourceFile("hs2017_cpc21", "tables/CPC/CPCv21_HS2017/CPC21-HS2017.csv", "text"),
    SourceFile("cpc21_isic4", "tables/CPC/CPCv21_ISIC4/cpc21-isic4.txt", "text"),
    SourceFile("cpc21_isic4_readme", "tables/CPC/CPCv21_ISIC4/readme.txt", "text"),
    SourceFile("hs2022_hs2017", "tables/HS2022toHS2017ConversionAndCorrelationTables.xlsx", "xlsx"),
    SourceFile("hs2022_hs1992", "tables/HS2022toHS1992ConversionAndCorrelationTables.xlsx", "xlsx"),
    SourceFile("hs2022_sitc4", "tables/HS2022toSITC4ConversionAndCorrelationTables.xlsx", "xlsx"),
)

#: HS sheets of HSCodeandDescription.xlsx loaded as KG schemes (BACI uses HS 1992; CPC 2.1 is tied to HS 2017).
HS_SHEETS = {"HS22": ("hs2022", "HS 2022"), "HS17": ("hs2017", "HS 2017"), "HS92": ("hs1992", "HS 1992")}
SITC_SHEETS = {"SITC4": ("sitc4", "SITC Rev.4")}
SCHEME_LABEL = {
    "hs2022": "HS 2022",
    "hs2017": "HS 2017",
    "hs1992": "HS 1992",
    "cpc21": "CPC Ver. 2.1",
    "isic4": "ISIC Rev.4",
    "sitc4": "SITC Rev.4",
}
HS_LEVEL = {2: "chapter", 4: "heading", 6: "subheading"}
CPC_LEVEL = {1: "section", 2: "division", 3: "group", 4: "class", 5: "subclass"}
ISIC_LEVEL = {1: "section", 2: "division", 3: "group", 4: "class"}
SITC_LEVEL = {1: "section", 2: "division", 3: "group", 4: "subgroup", 5: "basic heading"}

HS_RE = re.compile(r"^[0-9]{2}([0-9]{2}([0-9]{2})?)?$")
CPC_RE = re.compile(r"^[0-9]{1,5}$")
ISIC_RE = re.compile(r"^([A-U]|[0-9]{2,4})$")
SITC_RE = re.compile(r"^[0-9]{1,5}$")


@dataclass(frozen=True)
class Concordance:
    """A correspondence table: (key, from scheme, to scheme, kind)."""

    key: str
    table: str
    left: str
    right: str


CONCORDANCES: tuple[Concordance, ...] = (
    Concordance("hs2017_cpc21", "CPC21-HS2017", "hs2017", "cpc21"),
    Concordance("cpc21_isic4", "cpc21-isic4", "cpc21", "isic4"),
    Concordance("hs2022_hs2017", "HS2022-HS2017 Correlations", "hs2022", "hs2017"),
    Concordance("hs2022_hs1992", "HS2022-HS1992 Correlations", "hs2022", "hs1992"),
    Concordance("hs2022_sitc4", "Correlation HS22-SITC4", "hs2022", "sitc4"),
)


class ClassifError(SourceResponseError):
    pass


def _err(msg: str, kind: str = "schema_drift") -> ClassifError:
    return ClassifError(msg[:2000], retryable=False, kind=kind)


def code_digest() -> str:
    h = hashlib.sha256()
    h.update(inspect.getsource(inspect.getmodule(code_digest)).encode())  # type: ignore[arg-type]
    h.update(inspect.getsource(common).encode())
    return "sha256:" + h.hexdigest()


# ============================================================================================== parsing
@dataclass(frozen=True)
class Code:
    scheme: str
    code: str
    title: str
    parent: str | None
    level: str
    selector: Mapping[str, Any]


@dataclass(frozen=True)
class Pair:
    left: str  # KG id
    right: str
    quals: Mapping[str, Any]
    selector: Mapping[str, Any]


@dataclass
class Parsed:
    codes: dict[str, Code] = field(default_factory=dict)  # KG id -> Code
    pairs: dict[str, list[Pair]] = field(default_factory=dict)  # concordance key -> pairs
    report: dict[str, Any] = field(default_factory=dict)


def decode_text(body: bytes) -> str:
    """UTF-8 when valid, else Windows-1252 (the CPC 2.1 structure file is not UTF-8)."""
    try:
        return body.decode("utf-8-sig")
    except UnicodeDecodeError:
        return body.decode("cp1252")


def csv_rows(body: bytes, required: Sequence[str]) -> list[tuple[int, dict[str, str]]]:
    rd = csv.DictReader(io.StringIO(decode_text(body)))
    head = [h.strip() for h in (rd.fieldnames or [])]
    missing = [c for c in required if c not in head]
    if missing:
        raise _err(f"columns {missing} missing (header {head})")
    rd.fieldnames = head
    return [(i + 2, {k: (v or "").strip() for k, v in r.items() if k}) for i, r in enumerate(rd)]


def _cell_text(cells: Mapping[int, Any], i: int) -> str:
    c = cells.get(i)
    return "" if c is None else str(c.text).strip()


def xlsx_table(body: bytes, sheet: str, header: Sequence[str]) -> list[tuple[int, list[str], bool]]:
    """Rows after the header row whose first cells equal ``header`` (case/space-insensitive): (row number, cell
    texts, whether a cell of the row is typed numeric -- a code typed as a number may have lost a leading zero)."""
    from reva.adapters.connectors.atlas_society_common import XlsxStream

    x = XlsxStream(body)
    want = [h.strip().lower() for h in header]
    out: list[tuple[int, list[str], bool]] = []
    found = False
    for rnum, cells in x.rows(sheet):
        vals = [_cell_text(cells, i) for i in range(len(header))]
        if not found:
            found = [v.lower() for v in vals] == want
            continue
        if any(vals):
            numeric = any(getattr(cells.get(i), "kind", "s") == "n" for i in range(len(header)))
            out.append((rnum, vals, numeric))
    if not found:
        raise _err(f"sheet {sheet}: header {list(header)} not found")
    return out


def parse_coded_sheet(
    parsed: Parsed, body: bytes, *, sheet: str, scheme: str, code_re: re.Pattern[str], levels: Mapping[int, str]
) -> None:
    """HS / SITC sheets: Classification, Code, Description, Parent Code, Level, IsBasicLevel."""
    st: Counter[str] = Counter()
    rows = xlsx_table(body, sheet, ("Classification", "Code", "Description", "Parent Code", "Level"))
    for rnum, vals, numeric in rows:
        code, title, parent = vals[1], vals[2], vals[3]
        if code.upper() == "TOTAL":
            st["total_row"] += 1
            continue
        if numeric:
            st["numeric_cells"] += 1  # reported; a code that lost a leading zero fails the length check below
        if not code_re.match(code) or len(code) not in levels:
            st["bad_code"] += 1
            continue
        if not title:
            st["no_title"] += 1
            continue
        par = None if parent.upper() == "TOTAL" else parent
        if par is not None and not code_re.match(par):
            st["bad_parent"] += 1
            par = None
        kid = f"{scheme}:{code}"
        if kid in parsed.codes:
            st["duplicate_code"] += 1
            continue
        parsed.codes[kid] = Code(scheme, code, title, par, levels[len(code)], {"sheet": sheet, "row": rnum})
        st["codes"] += 1
    for c in list(parsed.codes.values()):
        if c.scheme == scheme and c.parent is not None and f"{scheme}:{c.parent}" not in parsed.codes:
            st["parent_missing"] += 1
    parsed.report[f"{scheme}_structure"] = dict(st)


def parse_cpc(parsed: Parsed, body: bytes) -> None:
    st: Counter[str] = Counter()
    for rnum, r in csv_rows(body, ("CPC21code", "CPC21title")):
        code, title = r["CPC21code"], r["CPC21title"]
        if not CPC_RE.match(code) or not title:
            st["bad_row"] += 1
            continue
        kid = f"cpc21:{code}"
        if kid in parsed.codes:
            st["duplicate_code"] += 1
            continue
        parsed.codes[kid] = Code(
            "cpc21", code, title, code[:-1] or None, CPC_LEVEL[len(code)], {"file": "cpc21_structure", "row": rnum}
        )
        st["codes"] += 1
    for c in [c for c in parsed.codes.values() if c.scheme == "cpc21" and c.parent]:
        if f"cpc21:{c.parent}" not in parsed.codes:
            st["parent_missing"] += 1
    parsed.report["cpc21_structure"] = dict(st)


def parse_isic(parsed: Parsed, body: bytes) -> None:
    st: Counter[str] = Counter()
    section: str | None = None
    for rnum, r in csv_rows(body, ("Code", "Description")):
        code, title = r["Code"], r["Description"]
        if not ISIC_RE.match(code) or not title:
            st["bad_row"] += 1
            continue
        if code.isalpha():
            section, parent, level = code, None, "section"
        elif len(code) == 2:
            if section is None:
                st["division_before_any_section"] += 1
                continue
            parent, level = section, "division"
        else:
            parent, level = code[:-1], ISIC_LEVEL[len(code)]
        kid = f"isic4:{code}"
        if kid in parsed.codes:
            st["duplicate_code"] += 1
            continue
        parsed.codes[kid] = Code("isic4", code, title, parent, level, {"file": "isic4_structure", "row": rnum})
        st["codes"] += 1
    for c in [c for c in parsed.codes.values() if c.scheme == "isic4" and c.parent]:
        if f"isic4:{c.parent}" not in parsed.codes:
            st["parent_missing"] += 1
    parsed.report["isic4_structure"] = dict(st)


def _add_pairs(
    parsed: Parsed, conc: Concordance, rows: Iterable[tuple[str, str, Mapping[str, Any], Mapping[str, Any]]]
) -> None:
    st: Counter[str] = Counter()
    seen: dict[tuple[str, str], int] = {}
    out: list[Pair] = []
    missing: list[str] = []
    for a, b, quals, sel in rows:
        if "n/a" in (a.lower(), b.lower()):
            st["not_applicable_stated"] += 1  # the table states no correspondent ("n/a"): no statement
            continue
        left, right = f"{conc.left}:{a}", f"{conc.right}:{b}"
        bad = [x for x in (left, right) if x not in parsed.codes]
        if bad:
            st["code_not_in_structure"] += 1
            missing.extend(bad)
            continue
        if (left, right) in seen:
            st["duplicate_pair"] += 1
            continue
        seen[(left, right)] = len(out)
        out.append(Pair(left, right, {"table": conc.table, **quals}, sel))
        st["pairs"] += 1
    parsed.pairs[conc.key] = out
    parsed.report[conc.key] = {**dict(st), "missing_codes_sample": sorted(set(missing))[:20]}


def parse_cpc_tables(parsed: Parsed, hs_cpc: bytes, cpc_isic: bytes) -> None:
    conc = {c.key: c for c in CONCORDANCES}
    rows1 = []
    for rnum, r in csv_rows(hs_cpc, ("HS 2017", "HS partial", "CPC Ver. 2.1", "CPC partial")):
        hs = r["HS 2017"].replace(".", "")
        rows1.append(
            (
                hs,
                r["CPC Ver. 2.1"],
                {"hs_partial": r["HS partial"], "cpc_partial": r["CPC partial"], "hs_code_as_stated": r["HS 2017"]},
                {"file": "hs2017_cpc21", "row": rnum},
            )
        )
    _add_pairs(parsed, conc["hs2017_cpc21"], rows1)
    rows2 = []
    for rnum, r in csv_rows(cpc_isic, ("CPC21code", "CPC21partial", "ISIC4code", "ISIC4partial")):
        rows2.append(
            (
                r["CPC21code"],
                r["ISIC4code"],
                {"cpc_partial": r["CPC21partial"], "isic_partial": r["ISIC4partial"]},
                {"file": "cpc21_isic4", "row": rnum},
            )
        )
    _add_pairs(parsed, conc["cpc21_isic4"], rows2)


def parse_hs_correlation(parsed: Parsed, body: bytes, conc: Concordance, right_header: str) -> None:
    """``... Correlations`` sheet (code, code, Relationship) + the one-way ``Conversions`` sheet as a flag."""
    from reva.adapters.connectors.atlas_society_common import XlsxStream

    sheets = list(XlsxStream(body).sheets)
    corr = [s for s in sheets if "correlation" in s.lower()]
    conv = [s for s in sheets if "conversion" in s.lower()]
    if len(corr) != 1 or len(conv) != 1:
        raise _err(f"{conc.key}: expected one correlation and one conversion sheet, got {sheets}")
    left_header = "HS 2022" if conc.key != "hs2022_hs2017" else "HS2022"
    crow = xlsx_table(body, corr[0], (left_header, right_header, "Relationship"))
    conv_left = "From HS 2022" if conc.key != "hs2022_hs2017" else "From HS2022"
    vrow = xlsx_table(body, conv[0], (conv_left, _conv_right(conc)))
    conversions = {(v[0], v[1]) for _r, v, _n in vrow}
    numeric = sum(1 for *_x, n in crow if n)
    rows = []
    for rnum, v, _n in crow:
        rows.append(
            (
                v[0],
                v[1],
                {"relationship": v[2], **({"unsd_conversion": True} if (v[0], v[1]) in conversions else {})},
                {"file": conc.key, "sheet": corr[0], "row": rnum},
            )
        )
    _add_pairs(parsed, conc, rows)
    parsed.report[conc.key]["conversion_rows"] = len(vrow)
    parsed.report[conc.key]["numeric_cells"] = numeric


def _conv_right(conc: Concordance) -> str:
    return {"hs2022_hs2017": "To HS2017", "hs2022_hs1992": "From HS 1992", "hs2022_sitc4": "To SITC Rev. 4"}[conc.key]


def parse_all(bodies: Mapping[str, bytes]) -> Parsed:
    p = Parsed()
    for sheet, (scheme, _lab) in HS_SHEETS.items():
        parse_coded_sheet(p, bodies["hs_codes"], sheet=sheet, scheme=scheme, code_re=HS_RE, levels=HS_LEVEL)
    for sheet, (scheme, _lab) in SITC_SHEETS.items():
        parse_coded_sheet(p, bodies["sitc_codes"], sheet=sheet, scheme=scheme, code_re=SITC_RE, levels=SITC_LEVEL)
    parse_cpc(p, bodies["cpc21_structure"])
    parse_isic(p, bodies["isic4_structure"])
    parse_cpc_tables(p, bodies["hs2017_cpc21"], bodies["cpc21_isic4"])
    conc = {c.key: c for c in CONCORDANCES}
    parse_hs_correlation(p, bodies["hs2022_hs2017"], conc["hs2022_hs2017"], "HS2017")
    parse_hs_correlation(p, bodies["hs2022_hs1992"], conc["hs2022_hs1992"], "HS 1992")
    parse_hs_correlation(p, bodies["hs2022_sitc4"], conc["hs2022_sitc4"], "SITC Rev. 4")
    return p


# ============================================================================================== KG build
KIND = {
    "hs2022": "hs_code",
    "hs2017": "hs_code",
    "hs1992": "hs_code",
    "cpc21": "cpc_code",
    "isic4": "isic_code",
    "sitc4": "sitc_code",
}
LEVEL_FR = {
    "chapter": "chapitre",
    "heading": "position",
    "subheading": "sous-position",
    "section": "section",
    "division": "division",
    "group": "groupe",
    "class": "classe",
    "subclass": "sous-classe",
    "subgroup": "sous-groupe",
    "basic heading": "rubrique de base",
}


def build_kg(
    parsed: Parsed, *, raw: Mapping[str, str], revisions: Mapping[str, str]
) -> tuple[list[Any], list[Any], dict[str, Any]]:
    """Entities + statements. ``raw``: file key -> archived sha256; ``revisions``: file key -> source revision."""
    from reva.kg.model import KgEntity, KgStatement

    ents: list[Any] = []
    sts: list[Any] = []
    rep: Counter[str] = Counter()

    def file_of(c: Code) -> str:
        if c.scheme.startswith("hs"):
            return "hs_codes"
        if c.scheme == "sitc4":
            return "sitc_codes"
        return f"{c.scheme}_structure"

    for kid in sorted(parsed.codes):
        c = parsed.codes[kid]
        f = file_of(c)
        lab = SCHEME_LABEL[c.scheme]
        ents.append(
            KgEntity(
                qid=kid,
                kind=KIND[c.scheme],
                labels={"en": c.title[:500]},
                descriptions={
                    "en": f"{lab} {c.level} {c.code} (UNSD classification file)",
                    "fr": f"{lab} {LEVEL_FR.get(c.level, c.level)} {c.code} (fichier de classification UNSD)",
                },
                source_revision=revisions[f],
            )
        )
        rep[f"entities_{c.scheme}"] += 1
        if c.parent:
            par = f"{c.scheme}:{c.parent}"
            if par not in parsed.codes:
                rep["parent_missing"] += 1
                continue
            quals: dict[str, Any] = {"scheme": c.scheme, "level": c.level}
            if c.scheme.startswith("hs"):
                quals["redistribution"] = HS_FLAG
            sts.append(
                KgStatement(
                    subject_qid=kid,
                    property_pid=P_CLASS_PARENT,
                    object_qid=par,
                    qualifiers=quals,
                    references=[{"stated_in": f"UNSD {lab}", "selector": {"file": f, **c.selector}}],
                    statement_id=f"parent:{kid}",
                    source_revision=revisions[f],
                    raw_sha256=raw[f],
                )
            )
            rep["class_parent"] += 1
    for conc in CONCORDANCES:
        for p in parsed.pairs.get(conc.key, []):
            quals = dict(p.quals)
            if conc.left.startswith("hs") or conc.right.startswith("hs"):
                quals["redistribution"] = HS_FLAG
            sts.append(
                KgStatement(
                    subject_qid=p.left,
                    property_pid=P_CORRESPONDS,
                    object_qid=p.right,
                    qualifiers=quals,
                    references=[{"stated_in": f"UNSD correspondence table {conc.table}", "selector": dict(p.selector)}],
                    statement_id=f"concord:{conc.key}:{p.left}={p.right}",
                    source_revision=revisions[conc.key],
                    raw_sha256=raw[conc.key],
                )
            )
            rep[f"corresponds_{conc.key}"] += 1
    return ents, sts, dict(rep)


# ============================================================================================== atlas protocol
def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider=PROVIDER,
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=(DATASET,),
        dataset_ids=tuple(f.key for f in FILES),
        cadence_seconds=90 * DAY,
        planned_requests=len(FILES),
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={
            "entities": "hs2022/hs2017/hs1992 (HSCodeandDescription.xlsx), sitc4, cpc21, isic4 structure files",
            "v4link:class_parent": "Parent Code column (HS, SITC); code structure (CPC, ISIC)",
            "v4link:corresponds_to": ", ".join(c.table for c in CONCORDANCES),
        },
        notes="KG only (no atlas table): classification codes, hierarchies and UNSD concordances (V4LINK).",
    )


def _revision(raw: Any, key: str) -> str:
    headers = {k.lower(): v for k, v in ((raw.response_metadata or {}).get("headers") or {}).items()}
    lm = headers.get("last-modified")
    if lm:
        try:
            return f"{key}@{datetime.strptime(lm, '%a, %d %b %Y %H:%M:%S %Z').date().isoformat()}"[:100]
        except ValueError:
            pass
    return f"{key}@retrieved:{raw.retrieved_at.date().isoformat()}"[:100]


async def atlas_ingest(run: Any) -> dict[str, Any]:
    if not run.wants(DATASET):
        return {"skipped": True}
    bodies: dict[str, bytes] = {}
    raw: dict[str, str] = {}
    revisions: dict[str, str] = {}
    sizes: dict[str, int] = {}
    retrieved: list[datetime] = []
    for f in FILES:
        acq = await run.get(f.url, key=f.key, media_types=(XLSX,) if f.kind == "xlsx" else TEXT, max_bytes=MAX_BYTES)
        if f.kind == "xlsx" and not acq.body.startswith(b"PK"):
            raise _err(f"{f.key}: not an xlsx container")
        raw[f.key] = run.archive(acq)
        bodies[f.key] = acq.body
        revisions[f.key] = _revision(acq, f.key)
        sizes[f.key] = len(acq.body)
        retrieved.append(acq.retrieved_at)
    parsed = parse_all(bodies)
    ents, sts, build = build_kg(parsed, raw=raw, revisions=revisions)
    kg = common.load_kg(
        run.engine,
        run.store,
        entities=ents,
        statements=sts,
        source_key=SOURCE_KEY,
        raw_sha256s=sorted(raw.values()),
        retrieved_at=max(retrieved),
        origin=run.origin,
        parameters={"connector": NAME, "parser": PARSER_VERSION, "files": {k: raw[k] for k in sorted(raw)}},
        retract_prefix="",
    )
    return {
        "files": {k: {"raw_sha256": raw[k], "bytes": sizes[k], "revision": revisions[k]} for k in raw},
        "parse": parsed.report,
        "build": build,
        "kg": kg,
    }


def scheme_counts(parsed: Parsed) -> dict[str, int]:
    c: dict[str, int] = defaultdict(int)
    for v in parsed.codes.values():
        c[v.scheme] += 1
    return dict(c)
