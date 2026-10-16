"""Economic and financial crises as dated events and atlas series (V3, agent A-ECRISES).

Four expert-coded crisis datasets, one atlas source each (``atlas_source()`` returns four specs; the module
dispatches on ``run.spec.name``):

* **Laeven & Valencia (2020), "Systemic Banking Crises Database II"**, IMF Economic Review 68:307-361
  (``atlas.ecrises.laeven_valencia``): the authors' data file (Springer electronic supplementary material,
  ``41308_2020_107_MOESM1_ESM.xlsx``, 1970-2017): crisis start years (systemic banking, currency, sovereign
  debt crisis, sovereign debt restructuring), monthly start dates, banking-crisis resolution and outcomes
  (end year, output loss, fiscal costs, liquidity, NPLs, public debt), per-crisis details, and the yearly
  crisis-frequency table (incl. twin and triple crises). The IMF working-paper copy (WP/18/206 dataset
  ``.ashx``) is no longer served (HTTP 404, 2026-10-16).
* **Reinhart & Rogoff "Global Crises Data by Country"** (``atlas.ecrises.reinhart_rogoff``): Harvard
  Business School Behavioral Finance & Financial Stability project file ``20160923_global_crisis_data.xlsx``
  (70 countries, 1800-2016): banking / systemic / currency / inflation crisis dummies, domestic and external
  sovereign default, gold standard, independence, inflation, USD exchange rates and the provider's notes.
* **Jordà-Schularick-Taylor Macrohistory Database R6** (``atlas.ecrises.jst``): ``JSTdatasetR6.xlsx``
  (18 advanced economies, 1870-2020): ``crisisJST`` (systemic financial crisis start years) and every
  macro-financial series of the release. Licence CC BY-NC-SA 4.0 (checked on every run on the provider's
  licence page; another licence = refused).
* **ESRB European financial crises database** (``atlas.ecrises.esrb``): ESRB/ECB file
  ``esrb.fcdb20220120.en.xlsx`` (Lo Duca et al. 2017, ESRB Occasional Paper 13; updated): systemic crises
  and residual events of financial stress in EU/EEA countries with start month, end of crisis management,
  "back to normal" date, categories and the coded "accelerator" (ESRB intermediate macroprudential
  objective). No open BIS banking-crisis list exists (BIS SDMX dataflows checked 2026-10-16), so this
  official-sector database fills that slot.

What is produced per source (all values verbatim; nothing is imputed, blended or rescaled)
-------------------------------------------------------------------------------------------
1. **Atlas table** ``<source>.<dataset>`` (country-year cells exactly as published; empty, ``n/a``,
   ``...`` and spreadsheet error cells are omitted and counted).
2. **Crisis events** (:class:`CrisisEvent`): one per coded episode. Laeven-Valencia, JST and ESRB code
   episodes directly (start year / start month; LV and ESRB also an end). Reinhart-Rogoff code *country-years*:
   an RR event is a maximal run of consecutive years coded 1 (REVA's grouping, rule ``rr_consecutive_years``,
   stated on every such event). Events go to the KG (``kg.entity`` kind ``event``; statements P31 class,
   P17 country, P580/P582 start/end, ``ecrises:crisis_type``, ``ecrises:coded_date``) and to an evidence
   dataset ``ecrises.<source>.events`` (concept ``events.crisis_episodes``).
3. **Coded relations** (:class:`CodedRelation`), mapped per ``registry/causal/relations_ecrises.yaml``:
   * ESRB "Accelerator and motivation" names one or more of the five ESRB intermediate objectives
     (codebook, Annex A of ESRB OP 13) -> **causal claim** ``contributing_factor`` (tier T1) from the named
     vulnerability (condition entity) to the crisis, mechanism = the provider's text verbatim;
   * Laeven-Valencia twin / triple crises (definition in the data file's footnote 1/ and 2/: crises within
     ``[t-1, t+1]``) -> **context** ``same_period`` edges only (co-occurrence is not causation; the authors
     code no causal sequence). REVA applies the published definition to the published dates and checks the
     result against the authors' yearly twin/triple counts (``twin_check`` in the build report).
   * Nothing else in these datasets is coded as a relation (RR / JST notes and narratives, ESRB "domestic vs
     imported", LV sovereign restructurings are attributes, never edges).
   They are stored as an evidence dataset ``ecrises.<source>.coded_relations`` (concept
   ``causal.coded_relations``) for the causal loader (CAUSCORE); REVA asserts none of them.

Rights: none of the four is an unrestricted open licence (see ``RIGHTS``): JST is CC BY-NC-SA 4.0
(non-commercial, share-alike; flagged like CShapes and IPU), Laeven-Valencia is (c) IMF (IMF data terms:
reuse with attribution; flagged for maintainer review), Reinhart-Rogoff states no licence (HBS copyright;
flagged), ESRB allows free use with citation. Every atlas row carries the citation key in its flags.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any

import polars as pl

from reva.adapters.connectors.atlas_history_common import canon, decimal_text, frame
from reva.adapters.connectors.atlas_xlsx import Cell, Workbook, XlsxError, col_letters
from reva.kernel.errors import SourceResponseError

CONNECTOR_VERSION = "1.0.0"
KG_SOURCE_PREFIX = "ecrises"
ACTOR = "worker:atlas_ingest"
MAX_XLSX_BYTES = 20_000_000

# ------------------------------------------------------------------------------------------- sources
LV = "laeven_valencia"
RR = "reinhart_rogoff"
JST = "jst"
ESRB = "esrb"

LV_URL = (
    "https://static-content.springer.com/esm/art%3A10.1057%2Fs41308-020-00107-3/MediaObjects/"
    "41308_2020_107_MOESM1_ESM.xlsx"
)
RR_URL = (
    "https://www.hbs.edu/behavioral-finance-and-financial-stability/Documents/ChartData/MapCharts/"
    "20160923_global_crisis_data.xlsx"
)
JST_URL = "https://www.macrohistory.net/app/download/9834512569/JSTdatasetR6.xlsx?t=1763503850"
JST_LICENCE_URL = "https://www.macrohistory.net/database/licence-terms/"
JST_LICENCE_MARKER = "CREATIVE COMMONS ATTRIBUTION-NONCOMMERCIAL-SHAREALIKE 4.0 INTERNATIONAL"
ESRB_URL = "https://www.esrb.europa.eu/pub/fcdb/esrb.fcdb20220120.en.xlsx"

XLSX_MEDIA = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/octet-stream",
)

CITATIONS = {
    LV: "Laeven, L. and Valencia, F. (2020), 'Systemic Banking Crises Database II', IMF Economic Review 68, "
    "307-361, doi:10.1057/s41308-020-00107-3 (data file: electronic supplementary material 1). (c) IMF.",
    RR: "Reinhart, C. M. and Rogoff, K. S., 'This Time Is Different' crisis data: Global Crises Data by "
    "Country (Harvard Business School, Behavioral Finance & Financial Stability project; with C. Trebesch "
    "and V. Reinhart), file 20160923_global_crisis_data.xlsx.",
    JST: "Jordà, Ò., Schularick, M. and Taylor, A. M. (2017), 'Macrofinancial History and the New Business "
    "Cycle Facts', NBER Macroeconomics Annual 2016, vol. 31. Jordà-Schularick-Taylor Macrohistory Database "
    "R6 (macrohistory.net), CC BY-NC-SA 4.0.",
    ESRB: "Lo Duca, M. et al. (2017), 'A new database for financial crises in European countries', ESRB "
    "Occasional Paper 13; European financial crises database (ESRB/ECB), file esrb.fcdb20220120.en.xlsx. "
    "Source: European Systemic Risk Board.",
}

RIGHTS: dict[str, dict[str, Any]] = {
    LV: {
        "licence": "(c) International Monetary Fund 2020 - IMF data terms (reuse with attribution); "
        "not an open licence, pending maintainer review",
        "licence_url": "https://www.imf.org/en/About/copyright-and-terms",
        "attribution": CITATIONS[LV],
        "open_licence": False,
        "review_flag": "IMF copyright; the data file is published as supplementary material of a "
        "non-open-access article. IMF 'Special Terms for Statistical Data' allow reuse with attribution; "
        "whether they cover this research dataset is for a maintainer to confirm.",
    },
    RR: {
        "licence": "no licence stated by the provider (Copyright President & Fellows of Harvard College); "
        "data offered for download with citation - not an open licence, pending maintainer review",
        "licence_url": "https://www.hbs.edu/behavioral-finance-and-financial-stability/data/Pages/global.aspx",
        "attribution": CITATIONS[RR],
        "open_licence": False,
        "review_flag": "no reuse terms published; redistribution in REVA to be confirmed by a maintainer",
    },
    JST: {
        "licence": "CC BY-NC-SA 4.0",
        "licence_url": "https://creativecommons.org/licenses/by-nc-sa/4.0/",
        "attribution": CITATIONS[JST],
        "open_licence": False,
        "non_commercial": True,
        "share_alike": "derived tables, events and relations from JST are shared under CC BY-NC-SA 4.0",
        "restriction": "NON-COMMERCIAL. Exclude source 'jst' from any commercial deployment; the provider "
        "expressly forbids commercial data providers from integrating the data.",
    },
    ESRB: {
        "licence": "ESRB website copyright terms: free use with accurate reproduction and citation of the "
        "ESRB as the source",
        "licence_url": "https://www.esrb.europa.eu/services/disclaimer/html/index.en.html",
        "attribution": CITATIONS[ESRB],
        "open_licence": False,
        "review_flag": "free use with citation; documents bearing author names need permission for "
        "re-publication (the database is an ESRB data file, not an authored document)",
    },
}


@dataclass(frozen=True)
class SourceDef:
    key: str  # LV / RR / JST / ESRB
    name: str  # atlas connector name
    source_key: str  # registry/sources/*.yaml
    atlas_source: str
    dataset: str
    url: str
    hosts: tuple[str, ...]
    provider: str
    dataset_id: str
    parser_version: str
    planned_requests: int
    cadence_days: int


SOURCES: dict[str, SourceDef] = {
    LV: SourceDef(
        LV,
        "atlas.ecrises.laeven_valencia",
        "laeven_valencia.sbcd",
        "laeven_valencia",
        "sbcd",
        LV_URL,
        ("static-content.springer.com",),
        "Luc Laeven and Fabian Valencia (IMF), Systemic Banking Crises Database II",
        "doi:10.1057/s41308-020-00107-3 ESM1",
        "atlas_ecrises_lv/1",
        1,
        180,
    ),
    RR: SourceDef(
        RR,
        "atlas.ecrises.reinhart_rogoff",
        "reinhart_rogoff.bffs",
        "reinhart_rogoff",
        "global_crises",
        RR_URL,
        ("www.hbs.edu",),
        "Carmen M. Reinhart and Kenneth S. Rogoff; HBS Behavioral Finance & Financial Stability project",
        "20160923_global_crisis_data.xlsx",
        "atlas_ecrises_rr/1",
        1,
        180,
    ),
    JST: SourceDef(
        JST,
        "atlas.ecrises.jst",
        "jst.macrohistory",
        "jst",
        "macrohistory",
        JST_URL,
        ("www.macrohistory.net", "sfff5b3ac9317c4be.jimcontent.com"),
        "Jordà-Schularick-Taylor Macrohistory Database (MacroFinance & MacroHistory Lab, Univ. of Bonn)",
        "JSTdatasetR6",
        "atlas_ecrises_jst/1",
        2,
        180,
    ),
    ESRB: SourceDef(
        ESRB,
        "atlas.ecrises.esrb",
        "esrb.fcdb",
        "esrb",
        "fcdb",
        ESRB_URL,
        ("www.esrb.europa.eu",),
        "European Systemic Risk Board (ESRB/ECB), European financial crises database",
        "esrb.fcdb20220120",
        "atlas_ecrises_esrb/1",
        1,
        90,
    ),
}
BY_NAME = {s.name: s for s in SOURCES.values()}

# Wikidata classes of crisis events (labels/descriptions quoted from wbsearchentities, 2026-10-16;
# Q114380 from KGSCIECO's economy.economic_crises spec). Only these are used for P31; a coded type
# without a verified class gets no P31 (its ``ecrises:crisis_type`` statement still says what it is).
WIKIDATA_CLASSES = {
    "Q806729": "banking crisis - 'abrupt instability in the banking industry'",
    "Q1417912": "currency crisis - 'situation in which grave doubt exists as to whether a central bank has "
    "enough forex reserves to maintain an exchange rate'",
    "Q1092023": "sovereign default - 'failure or refusal of the government of a sovereign state to pay back "
    "its debt in full when due'",
    "Q1364290": "debt restructuring - 'process of renegotiation regarding an organization's, or sovereign "
    "entity's delinquent debts'",
    "Q114380": "financial crisis (registry/kg/queries/economy/economic_crises.yaml)",
}

#: crisis_type -> (English label, P31 class or None)
CRISIS_TYPES: dict[str, tuple[str, str | None]] = {
    "systemic_banking": ("systemic banking crisis", "Q806729"),
    "banking": ("banking crisis", "Q806729"),
    "currency": ("currency crisis", "Q1417912"),
    "sovereign_debt": ("sovereign debt crisis (default or restructuring)", "Q1092023"),
    "sovereign_debt_restructuring": ("sovereign debt restructuring", "Q1364290"),
    "external_default": ("sovereign external debt default or restructuring", "Q1092023"),
    "domestic_default": ("sovereign domestic debt default", "Q1092023"),
    "inflation": ("inflation crisis", None),
    "systemic_financial": ("systemic financial crisis", "Q114380"),
    "residual_financial_stress": ("episode of elevated financial stress (not systemic)", None),
}


class EcrisesError(SourceResponseError):
    """The provider file does not have the layout this parser was written for (schema drift)."""


def _err(msg: str, kind: str = "schema_drift") -> EcrisesError:
    return EcrisesError(msg, retryable=False, kind=kind)


# ------------------------------------------------------------------------------------------- helpers
def sha256_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


_FOOTNOTE = re.compile(r"\s+(\d+/)\s*$")
_YEAR = re.compile(r"^\d{4}$")


def split_footnote(text: str) -> tuple[str, str | None]:
    """``"Argentina 8/"`` -> ``("Argentina", "8/")``; surrounding whitespace removed."""
    t = text.strip()
    m = _FOOTNOTE.search(t)
    if m:
        return t[: m.start()].strip(), m.group(1)
    return t, None


def years_in_cell(text: str) -> list[int]:
    """``"1980, 1989,1995"`` -> ``[1980, 1989, 1995]``; refuses anything that is not a year list."""
    out = []
    for part in text.split(","):
        p = part.strip()
        if not p:
            continue
        if not _YEAR.match(p):
            raise ValueError(p)
        out.append(int(p))
    return out


def excel_serial_date(text: str) -> date | None:
    """Excel 1900-system serial day number -> date (``29281`` -> 1980-03-01); ``None`` if not a serial."""
    try:
        n = int(text)
    except ValueError:
        return None
    if not 3_000 <= n <= 80_000:  # below 3000 (1908) a number is a year typed as a number, not a date
        return None
    return date(1899, 12, 30) + timedelta(days=n)


def num(cell: Cell | None) -> str | None:
    """The cell's exact numeric text as a strict decimal string, or ``None``."""
    if cell is None or cell.kind != "n":
        return None
    return decimal_text(cell.text)


def yper(year: int) -> tuple[date, date]:
    return date(year, 1, 1), date(year + 1, 1, 1)


def ref(col: int, row: int) -> str:
    return f"{col_letters(col)}{row}"


_MON = {
    m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)
}


def month_token(tok: str) -> tuple[int, int] | None:
    """``"Mar-80"`` -> ``(1980, 3)`` (two-digit years: 70-99 -> 19xx, 00-69 -> 20xx; LV covers 1970-2017)."""
    m = re.fullmatch(r"([A-Za-z]{3})-(\d{2})", tok.strip())
    if not m or m.group(1).lower() not in _MON:
        return None
    yy = int(m.group(2))
    return (1900 + yy if yy >= 70 else 2000 + yy), _MON[m.group(1).lower()]


def ym_text(text: str) -> tuple[int, int] | None:
    """``"2007-12"`` (ESRB YYYY-MM) -> ``(2007, 12)``."""
    m = re.fullmatch(r"(\d{4})-(\d{2})", text.strip())
    if not m or not 1 <= int(m.group(2)) <= 12:
        return None
    return int(m.group(1)), int(m.group(2))


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def bounded_flags(fl: Mapping[str, Any], limit: int = 4000) -> str:
    """Canonical flags JSON within the atlas limit: the longest text values are shortened first (marked)."""
    out = dict(fl)
    text = canon(out)
    while len(text) > limit:
        k = max((k for k, v in out.items() if isinstance(v, str)), key=lambda k: len(out[k]))
        over = len(text) - limit + 40
        out[k] = out[k][: max(0, len(out[k]) - over)] + " [truncated]"
        out["flags_truncated"] = True
        text = canon(out)
    return text


def atlas_row(
    *,
    src: SourceDef,
    indicator: str,
    provider_geo: str,
    period: tuple[date, date],
    value: str | None = None,
    value_text: str | None = None,
    unit: str,
    flags: Mapping[str, Any],
    raw_sha: str,
    selector: Mapping[str, Any],
    concept_id: str | None = None,
) -> dict[str, Any]:
    if value is None and value_text is None:
        raise ValueError("an atlas row needs a value or a value_text")
    fl = {"citation": src.key, **dict(flags)}
    if value_text is not None:
        clean = _CONTROL.sub(" ", value_text)
        if clean != value_text:
            fl["control_characters_replaced_by_spaces"] = True  # line breaks etc. (atlas text has none)
        value_text = clean
        if len(value_text) > 4000:
            fl["value_text_truncated_from_chars"] = len(value_text)
            value_text = value_text[:4000]
    return {
        "source": src.atlas_source,
        "dataset": src.dataset,
        "indicator": indicator,
        "concept_id": concept_id,
        "iso3": None,
        "provider_geo": provider_geo,
        "entity_qid": None,
        "period_start": period[0],
        "period_end": period[1],
        "value": value,
        "value_text": value_text,
        "unit": unit,
        "flags": bounded_flags(fl),
        "raw_sha256": raw_sha,
        "selector": canon(selector),
    }


@dataclass
class CrisisEvent:
    """One coded crisis episode (an event of the REVA event model, not yet linked across datasets)."""

    eid: str
    source: str  # LV / RR / JST / ESRB
    crisis_type: str  # key of CRISIS_TYPES
    coded_as: str  # the provider's column / field / sheet, verbatim
    provider_geo: str
    iso3: str | None
    start: tuple[str, int]  # (ISO date text, Wikibase precision 9 year / 10 month / 11 day)
    end: tuple[str, int] | None = None
    end_kind: str | None = None  # what the provider's end means
    label_en: str = ""
    raw_sha256: str = ""
    selector: dict[str, Any] = field(default_factory=dict)
    attributes: dict[str, Any] = field(default_factory=dict)
    coded_dates: list[dict[str, Any]] = field(default_factory=list)  # {field, time, precision}
    derivation: str | None = None  # how REVA grouped provider codes into this episode, when it did

    @property
    def start_year(self) -> int:
        return int(self.start[0][:4])

    @property
    def end_year(self) -> int | None:
        return int(self.end[0][:4]) if self.end else None


@dataclass
class CodedRelation:
    """A relation the dataset codes (causal claim or context edge), with its asserting source."""

    rid: str
    family: str  # "causal" | "context"
    relation: str  # closed vocabulary (roadmap): contributing_factor / same_period ...
    subject: str  # cause (causal) / first event (context)
    object: str  # effect (causal) / second event (context)
    rule_id: str  # registry/causal/relations_ecrises.yaml rule
    coded_as: str
    tier: str = "T1"
    polarity: str | None = None
    mechanism: str | None = None
    qualifiers: dict[str, Any] = field(default_factory=dict)
    asserting_source: dict[str, Any] = field(default_factory=dict)


@dataclass
class Build:
    """Everything one source file yields."""

    src: SourceDef
    raw_sha256: str
    rows: list[dict[str, Any]]
    events: list[CrisisEvent]
    relations: list[CodedRelation]
    conditions: dict[str, dict[str, str]]  # condition entity id -> labels/description
    report: dict[str, Any]
    geo_scheme: str
    coverage_note: str

    def frame(self) -> pl.DataFrame:
        return frame(self.rows)


def _country(iso3: str | None, provider_geo: str) -> str:
    return iso3 or re.sub(r"[^A-Za-z0-9]+", "_", provider_geo).strip("_")[:40] or "unknown"


def _iso3(code: str, scheme: str) -> str | None:
    from reva.atlas.tables import country_iso3

    return country_iso3(code, scheme)


def _asserting(src: SourceDef, raw_sha: str, selector: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source_key": src.source_key,
        "dataset": f"{src.atlas_source}.{src.dataset}",
        "dataset_id": src.dataset_id,
        "raw_sha256": raw_sha,
        "selector": dict(selector),
        "citation": CITATIONS[src.key],
    }


def _event_label(ctype: str, country: str, when: str, src_label: str) -> str:
    return f"{CRISIS_TYPES[ctype][0].capitalize()} - {country}, {when} ({src_label})"


# =========================================================================================== Laeven-Valencia
LV_GEO_SCHEME = "laeven_valencia"
LV_YEARS_SHEET = "Crisis Years"
LV_MONTHLY_SHEET = "Monthly Crisis Dates"
LV_FREQ_SHEET = "Crisis Frequency"
LV_RES_SHEET = "Crisis Resolution and Outcomes"
LV_DETAIL_SHEET = "Additional Details-Bk Crises"
LV_SHEETS = (LV_YEARS_SHEET, LV_MONTHLY_SHEET, LV_FREQ_SHEET, LV_RES_SHEET, LV_DETAIL_SHEET)
#: Crisis Years columns: header text (as published) -> crisis type
LV_YEAR_COLUMNS = {
    1: ("Systemic Banking Crisis (starting date)", "systemic_banking"),
    2: ("Currency Crisis", "currency"),
    3: ("Sovereign Debt Crisis (year)", "sovereign_debt"),
    4: ("Sovereign Debt Restructuring (year)", "sovereign_debt_restructuring"),
}
LV_MONTHLY_COLUMNS = {1: "systemic_banking", 2: "sovereign_debt", 3: "currency"}
#: Resolution sheet: column -> (indicator, header prefix as published, unit)
LV_RES_COLUMNS = {
    3: ("output_loss", "Output loss", "% of trend real GDP (cumulative over [T, T+3])"),
    4: ("fiscal_cost_gross", "Fiscal Costs 2/", "% of GDP"),
    5: ("fiscal_cost_net", "Fiscal Costs, net", "% of GDP"),
    6: ("fiscal_cost_financial_assets", "Fiscal cost", "% of financial sector assets"),
    7: ("peak_liquidity", "Peak liquidity", "% of total deposits and liabilities to non-residents"),
    8: ("liquidity_support", "Liquidity support", "% of total deposits and liabilities to non-residents"),
    9: ("peak_npl", "Peak NPLs", "% of total loans"),
    10: ("public_debt_increase", "Increase in public debt", "% of GDP"),
}
LV_FREQ_COLUMNS = {
    1: ("Banking", "banking"),
    2: ("Currency", "currency"),
    3: ("Sovereign", "sovereign"),
    4: ("Twin  (banking-currency) 1/", "twin_banking_currency"),
    5: ("Twin  (banking-sovereign) 1/", "twin_banking_sovereign"),
    6: ("Twin  (currency-sovereign) 1/", "twin_currency_sovereign"),
    7: ("Triple  2/", "triple"),
}
LV_MISSING = {"...", "…", "n.a.", "n.a", ""}
LV_WORLD_GEO = "ALL COUNTRIES (Laeven-Valencia database)"
#: Additional-details rows whose cells are Excel dates (labels as published, stripped)
LV_DATE_ITEMS = {
    "Banking crisis start date",
    "Date when crisis became systemic",
    "Introduction",
    "Date of introduction",
    "Date of removal",
    "Date when it became extensive",
}


def sheet_comments(body: bytes, sheet_part: str) -> dict[str, str]:
    """Cell comments (notes) of one worksheet part: ``{"E412": "Carmen: Not a systemic banking crisis"}``.

    Read from the sheet's relationships (``xl/worksheets/_rels/<sheet>.rels``, type ``.../comments``); the comment
    text is kept verbatim (whitespace runs collapsed). Bounded; DTD/entity declarations refused."""
    import io
    import posixpath
    import zipfile
    from xml.etree import ElementTree as ET

    z = zipfile.ZipFile(io.BytesIO(body))
    names = set(z.namelist())
    rels = posixpath.join(posixpath.dirname(sheet_part), "_rels", posixpath.basename(sheet_part) + ".rels")
    if rels not in names:
        return {}

    def parse(part: str) -> Any:
        info = z.getinfo(part)
        if info.file_size > 20_000_000:
            raise _err(f"{part}: too large")
        data = z.read(part)
        if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
            raise _err(f"{part}: DTD/entity declaration refused", "unsafe_xml")
        return ET.fromstring(data)  # noqa: S314 - no DTD (checked above)

    out: dict[str, str] = {}
    pr = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    m = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    for rel in parse(rels).iter(pr + "Relationship"):
        if not rel.get("Type", "").endswith("/comments"):
            continue
        target = posixpath.normpath(posixpath.join(posixpath.dirname(sheet_part), rel.get("Target", "")))
        if target not in names:
            continue
        for c in parse(target).iter(m + "comment"):
            text = re.sub(r"\s+", " ", "".join(t.text or "" for t in c.iter(m + "t"))).strip()
            if c.get("ref") and text:
                out[c.get("ref", "")] = text[:1000]
    return out


def _sheet_rows(wb: Workbook, sheet: str) -> list[tuple[int, dict[int, Cell]]]:
    try:
        return list(wb.rows(sheet))
    except XlsxError as e:
        raise _err(f"workbook: {e}") from e


def _need_header(rows: Sequence[tuple[int, dict[int, Cell]]], expect: Mapping[int, str], what: str) -> None:
    if not rows or rows[0][0] != 1:
        raise _err(f"{what}: no header row")
    hdr = rows[0][1]
    for col, text in expect.items():
        got = hdr.get(col)
        if got is None or not got.text.strip().startswith(text.strip()):
            raise _err(f"{what}: column {col_letters(col)} header {got.text if got else None!r} != {text!r}")


def _footnotes(text: str) -> dict[str, str]:
    """``"1/ In percent of GDP. ...\\n2/ Fiscal costs ..."`` -> ``{"1/": "In percent of GDP. ...", ...}``."""
    parts = re.split(r"(?:(?<=\s)|^)(\d+/)\s", text)
    out: dict[str, str] = {}
    for i in range(1, len(parts) - 1, 2):
        out[parts[i]] = parts[i + 1].strip()
    return out


def parse_lv(body: bytes, raw_sha: str) -> Build:
    src = SOURCES[LV]
    try:
        wb = Workbook(body)
    except XlsxError as e:
        raise _err(f"Laeven-Valencia data file: {e}", "not_xlsx") from e
    missing = [s for s in LV_SHEETS if s not in wb.sheets]
    if missing:
        raise _err(f"Laeven-Valencia data file: sheets {missing} missing (has {sorted(wb.sheets)})")
    rows: list[dict[str, Any]] = []
    rep: dict[str, Any] = {"omitted_cells": Counter(), "unmapped_names": set()}
    events: dict[tuple[str, str, int], CrisisEvent] = {}

    def iso(name: str) -> str | None:
        got = _iso3(name, LV_GEO_SCHEME)
        if got is None:
            rep["unmapped_names"].add(name)
        return got

    def ckey(name: str) -> str:
        """Country identity across sheets (the sheets spell names differently: 'Congo, Dem. Rep. of' /
        'Congo, Dem Rep'): the crosswalk entity through the reviewed alias table, else the name itself."""
        return iso(name) or name

    # ---- Crisis Years (start years per type)
    yrows = _sheet_rows(wb, LV_YEARS_SHEET)
    _need_header(yrows, {0: "Country", **{c: h for c, (h, _) in LV_YEAR_COLUMNS.items()}}, LV_YEARS_SHEET)
    years_by: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for rnum, cells in yrows[1:]:
        if 0 not in cells:
            continue
        name, fn = split_footnote(cells[0].text)
        if not name or rnum == 2:
            continue
        for col, (header, ctype) in LV_YEAR_COLUMNS.items():
            c = cells.get(col)
            if c is None or c.text.strip() in LV_MISSING:
                if c is not None:
                    rep["omitted_cells"][f"{LV_YEARS_SHEET}:{c.text.strip() or 'empty'}"] += 1
                continue
            try:
                ys = years_in_cell(c.text)
            except ValueError as e:
                raise _err(f"{LV_YEARS_SHEET} {ref(col, rnum)}: {c.text!r} is not a year list") from e
            if len(set(ys)) != len(ys):
                raise _err(f"{LV_YEARS_SHEET} {ref(col, rnum)}: repeated year in {c.text!r}")
            for k, y in enumerate(ys):
                years_by[ckey(name)][ctype].append(y)
                sel = {"sheet": LV_YEARS_SHEET, "cell": ref(col, rnum), "item": k}
                fl: dict[str, Any] = {"column": header, "cell_text": c.text}
                if fn:
                    fl["country_footnote"] = fn
                rows.append(
                    atlas_row(
                        src=src,
                        indicator=f"crisis_start|type={ctype}",
                        provider_geo=name,
                        period=yper(y),
                        value="1",
                        unit="1 = year listed by the authors as the start of a crisis of this type",
                        flags=fl,
                        raw_sha=raw_sha,
                        selector=sel,
                    )
                )
                events[(ckey(name), ctype, y)] = CrisisEvent(
                    eid=f"{KG_SOURCE_PREFIX}:lv:{ctype}:{_country(iso(name), name)}:{y}",
                    source=LV,
                    crisis_type=ctype,
                    coded_as=f"{LV_YEARS_SHEET} / {header}",
                    provider_geo=name,
                    iso3=iso(name),
                    start=(f"{y:04d}-01-01", 9),
                    label_en=_event_label(ctype, name, str(y), "Laeven & Valencia 2020"),
                    raw_sha256=raw_sha,
                    selector=sel,
                )

    # ---- Monthly start dates (subset of episodes): attach to the year's event
    mrows = _sheet_rows(wb, LV_MONTHLY_SHEET)
    _need_header(
        mrows, {0: "Country", 1: "Banking crisis", 2: "Sovereign debt crisis", 3: "Currency crisis"}, LV_MONTHLY_SHEET
    )
    monthly_unmatched: list[str] = []
    for rnum, cells in mrows[1:]:
        if 0 not in cells or len(cells[0].text) > 60:
            continue
        name = split_footnote(cells[0].text)[0]
        canon_name = ckey(name)  # the monthly sheet writes some names without spaces ("UnitedKingdom")
        for col, ctype in LV_MONTHLY_COLUMNS.items():
            c = cells.get(col)
            if c is None or not c.text.strip():
                continue
            for k, tok in enumerate(t for t in c.text.split(",") if t.strip()):
                ym = month_token(tok)
                if ym is None and c.kind == "n" and (sd := excel_serial_date(tok.strip())) is not None:
                    ym = (sd.year, sd.month)  # one cell is a spreadsheet date (displayed as Mon-YY)
                if ym is None:
                    raise _err(f"{LV_MONTHLY_SHEET} {ref(col, rnum)}: {tok!r} is not Mon-YY")
                ev = events.get((canon_name, ctype, ym[0]))
                if ev is None:
                    monthly_unmatched.append(f"{name}:{ctype}:{tok.strip()}")
                    continue
                ev.start = (f"{ym[0]:04d}-{ym[1]:02d}-01", 10)
                ev.attributes["start_month_as_published"] = tok.strip()
                ev.coded_dates.append(
                    {
                        "field": f"{LV_MONTHLY_SHEET} / {mrows[0][1][col].text}",
                        "time": f"{ym[0]:04d}-{ym[1]:02d}-01",
                        "precision": 10,
                        "selector": {"sheet": LV_MONTHLY_SHEET, "cell": ref(col, rnum), "item": k},
                    }
                )
    rep["monthly_unmatched"] = monthly_unmatched

    # ---- Resolution and outcomes (banking crises): end year + outcome measures at the start year
    rrows = _sheet_rows(wb, LV_RES_SHEET)
    _need_header(
        rrows, {0: "Country", 1: "Start", 2: "End", **{c: h for c, (_, h, _) in LV_RES_COLUMNS.items()}}, LV_RES_SHEET
    )
    res_notes: dict[str, str] = {}
    for _rnum, cells in rrows[1:]:
        if 0 in cells and len(cells[0].text) > 80:
            res_notes = _footnotes(cells[0].text)
    seen_res: set[tuple[str, int]] = set()
    for rnum, cells in rrows[1:]:
        if 0 not in cells or 1 not in cells or len(cells[0].text) > 80:
            continue
        name, fn = split_footnote(cells[0].text)
        canon_name = ckey(name)
        start = cells[1].text.strip()
        if not _YEAR.match(start):
            raise _err(f"{LV_RES_SHEET} {ref(1, rnum)}: start {start!r} is not a year")
        y = int(start)
        if (canon_name, y) in seen_res:
            raise _err(f"{LV_RES_SHEET}: two rows for {name} {y}")
        seen_res.add((canon_name, y))
        base_fl: dict[str, Any] = {"episode_start": y}
        if fn:
            base_fl["country_footnote"] = fn
            if fn in res_notes:
                base_fl["country_footnote_text"] = res_notes[fn]
        sel_base = {"sheet": LV_RES_SHEET, "row": rnum}
        end_cell = cells.get(2)
        ev = events.get((canon_name, "systemic_banking", y))
        if ev is None:
            rep.setdefault("resolution_without_start", []).append(f"{name}:{y}")
        if end_cell is not None:
            end_txt, end_fn = split_footnote(end_cell.text)
            efl = {**base_fl, "end_as_published": end_cell.text}
            if end_fn:
                efl["end_footnote"] = end_fn
                efl["end_footnote_text"] = res_notes.get(end_fn)
            if _YEAR.match(end_txt):
                rows.append(
                    atlas_row(
                        src=src,
                        indicator="banking_crisis_end_year",
                        provider_geo=name,
                        period=yper(y),
                        value=end_txt,
                        unit="calendar year (end of the banking crisis as dated by the authors)",
                        flags=efl,
                        raw_sha=raw_sha,
                        selector={**sel_base, "cell": ref(2, rnum)},
                    )
                )
                if ev is not None:
                    ev.end = (f"{int(end_txt):04d}-01-01", 9)
                    ev.end_kind = "end year as dated by the authors" + (
                        f" (footnote {end_fn}: {res_notes.get(end_fn, '')})" if end_fn else ""
                    )
            elif end_txt.lower() == "ongoing":
                rows.append(
                    atlas_row(
                        src=src,
                        indicator="banking_crisis_end_year",
                        provider_geo=name,
                        period=yper(y),
                        value_text=end_txt,
                        unit="calendar year (end of the banking crisis as dated by the authors)",
                        flags=efl,
                        raw_sha=raw_sha,
                        selector={**sel_base, "cell": ref(2, rnum)},
                    )
                )
                if ev is not None:
                    ev.attributes["end_as_published"] = "ongoing"
            else:
                raise _err(f"{LV_RES_SHEET} {ref(2, rnum)}: end {end_cell.text!r} not understood")
        if ev is not None and fn == "8/":
            ev.attributes["borderline"] = True
            ev.attributes["borderline_note"] = res_notes.get("8/", "Borderline cases.")
        for col, (ind, _header, unit) in LV_RES_COLUMNS.items():
            c = cells.get(col)
            v = num(c)
            if v is None:
                rep["omitted_cells"][f"{LV_RES_SHEET}:{(c.text.strip() if c else '') or 'empty'}"] += 1
                continue
            hdr = rrows[0][1][col].text
            fl = {**base_fl, "column": hdr}
            m = re.search(r"(\d+/)", hdr)
            if m and m.group(1) in res_notes:
                fl["column_footnote_text"] = res_notes[m.group(1)]
            rows.append(
                atlas_row(
                    src=src,
                    indicator=ind,
                    provider_geo=name,
                    period=yper(y),
                    value=v,
                    unit=unit,
                    flags=fl,
                    raw_sha=raw_sha,
                    selector={**sel_base, "cell": ref(col, rnum)},
                )
            )
            if ev is not None:
                ev.attributes[ind] = v

    # ---- Additional details (one column per banking crisis; rows = items)
    drows = _sheet_rows(wb, LV_DETAIL_SHEET)
    if not drows or drows[0][1].get(0) is None or drows[0][1][0].text.strip() != "Country name":
        raise _err(f"{LV_DETAIL_SHEET}: first row must be 'Country name'")
    by_row = dict(drows)
    dhdr = by_row[1]
    start_row = next((r for r, c in drows if 0 in c and c[0].text.strip() == "Banking crisis start date"), None)
    if start_row is None:
        raise _err(f"{LV_DETAIL_SHEET}: no 'Banking crisis start date' row")
    crisis_cols: dict[int, tuple[str, str, int]] = {}  # col -> (country key, name as published, start year)
    for col, c in sorted(dhdr.items()):
        if col == 0:
            continue
        name = split_footnote(c.text)[0]
        sc = by_row[start_row].get(col)
        d = excel_serial_date(sc.text) if sc is not None and sc.kind == "n" else None
        if d is not None:
            y = d.year
        elif sc is not None and _YEAR.match(sc.text.strip()):
            y = int(sc.text.strip())
        else:
            raise _err(f"{LV_DETAIL_SHEET} {ref(col, start_row)}: no start date for {name}")
        canon_name = ckey(name)
        if (canon_name, "systemic_banking", y) not in events:
            rep.setdefault("details_without_start", []).append(f"{name}:{y}")
        crisis_cols[col] = (canon_name, name, y)
    if len({(n, y) for n, _nm, y in crisis_cols.values()}) != len(crisis_cols):
        raise _err(f"{LV_DETAIL_SHEET}: two crisis columns with the same country and start year")
    section = ""
    for rnum, cells in drows[1:]:
        label_cell = cells.get(0)
        if label_cell is None:
            continue
        label = re.sub(r"\s+", " ", label_cell.text).strip()
        if len(cells) == 1:  # a section heading ("Containment phase", "Liquidity support", ...)
            section = label
            continue
        for col, (ck, name, y) in crisis_cols.items():
            c = cells.get(col)
            if c is None or c.kind in ("empty", "e") or not c.text.strip():
                continue
            fl = {"item": label, "section": section or None, "episode_start": y}
            value: str | None = None
            vtext: str | None = None
            iso_date: str | None = None  # set only for a spreadsheet date cell
            if label in LV_DATE_ITEMS and c.kind == "n" and (dd := excel_serial_date(c.text)) is not None:
                vtext = iso_date = dd.isoformat()
                fl["excel_serial"] = c.text
                fl["date_note"] = "spreadsheet date cell; displayed as a date by the provider"
            elif c.kind == "n":
                value = decimal_text(c.text)
                if value is None:
                    rep["omitted_cells"][f"{LV_DETAIL_SHEET}:non_decimal"] += 1
                    continue
            else:
                vtext = c.text.strip()
            rows.append(
                atlas_row(
                    src=src,
                    indicator=f"detail|row={rnum}|item={label}",
                    provider_geo=name,
                    period=yper(y),
                    value=value,
                    value_text=vtext,
                    unit="as labelled by the authors (item text)",
                    flags=fl,
                    raw_sha=raw_sha,
                    selector={"sheet": LV_DETAIL_SHEET, "cell": ref(col, rnum)},
                )
            )
            ev = events.get((ck, "systemic_banking", y))
            if ev is None:
                continue
            if label == "Brief description of crisis" and vtext:
                ev.attributes["description"] = vtext[:4000]
            elif label == "Date when crisis became systemic" and iso_date:
                ev.coded_dates.append(
                    {
                        "field": f"{LV_DETAIL_SHEET} / {label}",
                        "time": iso_date,
                        "precision": 10 if iso_date.endswith("-01") else 11,  # first-of-month cells date a month
                        "selector": {"sheet": LV_DETAIL_SHEET, "cell": ref(col, rnum)},
                    }
                )
            elif label == "Banking crisis start date" and (iso_date or vtext):
                ev.attributes["start_date_as_published"] = iso_date or vtext
                if iso_date:
                    prec = (
                        10 if iso_date.endswith("-01") else 11
                    )  # the provider dates crises by month (1st-of-month cells)
                    if ev.start[1] < prec and ev.start[0][:4] == iso_date[:4]:
                        ev.start = (iso_date, prec)

    # ---- Crisis frequency (world counts per year) + footnotes defining twin / triple crises
    frows = _sheet_rows(wb, LV_FREQ_SHEET)
    _need_header(frows, {0: "Year", **{c: h for c, (h, _) in LV_FREQ_COLUMNS.items()}}, LV_FREQ_SHEET)
    freq_note = ""
    published: dict[str, dict[int, int]] = defaultdict(dict)
    published_total: dict[str, int] = {}
    for rnum, cells in frows[1:]:
        c0 = cells.get(0)
        if c0 is None:
            continue
        if c0.kind == "s" and c0.text.strip().startswith("1/"):
            freq_note = c0.text.strip()
            continue
        label = c0.text.strip()
        for col, (header, key) in LV_FREQ_COLUMNS.items():
            v = num(cells.get(col))
            if v is None:
                continue
            if label == "Total":
                published_total[key] = int(v)
                continue
            if not _YEAR.match(label):
                continue
            published[key][int(label)] = int(v)
            rows.append(
                atlas_row(
                    src=src,
                    indicator=f"crisis_count|type={key}",
                    provider_geo=LV_WORLD_GEO,
                    period=yper(int(label)),
                    value=v,
                    unit="number of crises starting in the year (all countries in the database)",
                    flags={"column": header, "definition": freq_note or None},
                    raw_sha=raw_sha,
                    selector={"sheet": LV_FREQ_SHEET, "cell": ref(col, rnum)},
                )
            )
    if not freq_note:
        raise _err(f"{LV_FREQ_SHEET}: the twin/triple footnote (1/, 2/) is missing")
    # the footnote row is read after the year rows: put the definition on every count row
    rows = [
        r
        if not r["indicator"].startswith("crisis_count|")
        else {**r, "flags": canon({**json.loads(r["flags"]), "definition": freq_note})}
        for r in rows
    ]
    rep["freq_definition"] = freq_note
    rep["published_totals"] = published_total
    # the authors' yearly counts vs the start years listed in Crisis Years (reported, never "fixed")
    rep["count_check"] = {}
    for key, ctypes in (
        ("banking", ("systemic_banking",)),
        ("currency", ("currency",)),
        ("sovereign", ("sovereign_debt",)),
    ):
        comp = Counter(e.start_year for e in events.values() if e.crisis_type in ctypes)
        pub = published.get(key, {})
        rep["count_check"][key] = {
            "published_total": published_total.get(key),
            "listed_total": sum(comp.values()),
            "years_differing": {
                y: [pub.get(y, 0), comp.get(y, 0)]
                for y in sorted(set(pub) | set(comp))
                if pub.get(y, 0) != comp.get(y, 0)
            },
        }

    evs = sorted(events.values(), key=lambda e: e.eid)
    relations, twin_check = lv_twin_relations(years_by, events, src, raw_sha, freq_note, published, published_total)
    rep["twin_check"] = twin_check
    rep["events_by_type"] = dict(Counter(e.crisis_type for e in evs))
    rep["omitted_cells"] = dict(rep["omitted_cells"])
    rep["unmapped_names"] = sorted(rep["unmapped_names"])
    note = (
        "Laeven & Valencia (2020) Systemic Banking Crises Database II, data file (1970-2017): crisis start years "
        "by type (Crisis Years), banking-crisis outcomes at the start year (Crisis Resolution and Outcomes), "
        "per-crisis details (Additional Details-Bk Crises; dates as ISO text), yearly world counts incl. twin and "
        "triple crises. Countries by name (geo scheme laeven_valencia); Yugoslavia, SFR kept unmapped. "
        "(c) IMF - not an open licence (see rights)."
    )
    return Build(src, raw_sha, rows, evs, relations, {}, rep, LV_GEO_SCHEME, note)


#: Twin / triple definitions of the data file (footnotes 1/ and 2/ of the Crisis Frequency sheet).
LV_PAIRS = {
    "twin_banking_currency": ("systemic_banking", "currency"),
    "twin_banking_sovereign": ("systemic_banking", "sovereign_debt"),
    "twin_currency_sovereign": ("currency", "sovereign_debt"),
}


def lv_twin_sets(by_type: Mapping[str, Sequence[int]]) -> dict[str, list[tuple[int, tuple[int, ...], tuple[int, ...]]]]:
    """Apply the authors' footnote definitions to one country's crisis years.

    Returns ``{kind: [(t, matching years of the second type, matching sovereign years for triples)]}``:
    triple = banking crisis in t, currency crisis in [t-1, t+1] and sovereign debt crisis in [t-1, t+1];
    twin (banking-X) = banking crisis in t and an X crisis in [t-1, t+1], excluding triple crises;
    twin (currency-sovereign) = currency crisis in t and a sovereign crisis in [t-1, t+1], excluding currency
    crises that belong to a triple crisis.
    """
    b = sorted(set(by_type.get("systemic_banking", ())))
    c = sorted(set(by_type.get("currency", ())))
    s = sorted(set(by_type.get("sovereign_debt", ())))

    def near(t: int, ys: Sequence[int]) -> tuple[int, ...]:
        return tuple(y for y in ys if abs(y - t) <= 1)

    out: dict[str, list[tuple[int, tuple[int, ...], tuple[int, ...]]]] = defaultdict(list)
    triple_currency: set[int] = set()
    for t in b:
        nc, ns = near(t, c), near(t, s)
        if nc and ns:
            out["triple"].append((t, nc, ns))
            triple_currency.update(nc)
        else:
            if nc:
                out["twin_banking_currency"].append((t, nc, ()))
            if ns:
                out["twin_banking_sovereign"].append((t, ns, ()))
    for t in c:
        ns = near(t, s)
        if ns and t not in triple_currency:
            out["twin_currency_sovereign"].append((t, ns, ()))
    return out


def lv_twin_relations(
    years_by: Mapping[str, Mapping[str, Sequence[int]]],
    events: Mapping[tuple[str, str, int], CrisisEvent],
    src: SourceDef,
    raw_sha: str,
    definition: str,
    published: Mapping[str, Mapping[int, int]],
    published_total: Mapping[str, int],
) -> tuple[list[CodedRelation], dict[str, Any]]:
    computed: dict[str, Counter[int]] = defaultdict(Counter)
    rels: list[CodedRelation] = []
    for name in sorted(years_by):
        sets = lv_twin_sets(years_by[name])
        for kind, items in sets.items():
            for t, second, sov in items:
                computed[kind][t] += 1
                pairs: list[tuple[str, str, int]] = []
                if kind == "triple":
                    pairs += [("systemic_banking", "currency", y) for y in second]
                    pairs += [("systemic_banking", "sovereign_debt", y) for y in sov]
                else:
                    first_t, second_t = LV_PAIRS[kind]
                    pairs += [(first_t, second_t, y) for y in second]
                first_type = "systemic_banking" if kind != "twin_currency_sovereign" else "currency"
                a = events[(name, first_type, t)]
                for _ft, st, y in pairs:
                    bev = events[(name, st, y)]
                    rels.append(
                        CodedRelation(
                            rid=f"{KG_SOURCE_PREFIX}:lv:{kind}:{a.eid.split(':', 3)[3]}:{st}:{y}",
                            family="context",
                            relation="same_period",
                            subject=a.eid,
                            object=bev.eid,
                            rule_id=f"lv2020_{kind}",
                            coded_as={
                                "triple": "Triple crisis (footnote 2/)",
                                "twin_banking_currency": "Twin (banking-currency) (footnote 1/)",
                                "twin_banking_sovereign": "Twin (banking-sovereign) (footnote 1/)",
                                "twin_currency_sovereign": "Twin (currency-sovereign) (footnote 1/)",
                            }[kind],
                            qualifiers={
                                "anchor_year": t,
                                "window": [t - 1, t + 1],
                                "second_year": y,
                                "definition": definition,
                                "derivation": "REVA applied the authors' published definition to the authors' "
                                "published crisis years; checked against the authors' yearly counts",
                            },
                            asserting_source=_asserting(src, raw_sha, {"sheet": LV_FREQ_SHEET, "footnote": "1/ 2/"}),
                        )
                    )
    check: dict[str, Any] = {}
    for kind in ("twin_banking_currency", "twin_banking_sovereign", "twin_currency_sovereign", "triple"):
        pub = dict(published.get(kind, {}))
        comp = dict(computed.get(kind, {}))
        diff = {
            y: [pub.get(y, 0), comp.get(y, 0)] for y in sorted(set(pub) | set(comp)) if pub.get(y, 0) != comp.get(y, 0)
        }
        check[kind] = {
            "published_total": published_total.get(kind),
            "computed_total": sum(comp.values()),
            "years_differing": diff,
            "matches": not diff and published_total.get(kind) == sum(comp.values()),
        }
    for r in rels:
        kind = r.rule_id.removeprefix("lv2020_")
        r.qualifiers["published_count_check"] = "matches" if check[kind]["matches"] else "differs (see build report)"
    return rels, check


# =========================================================================================== Reinhart-Rogoff
RR_SHEET = "Sheet1"
RR_GEO_SCHEME = "iso3"
#: column -> (indicator, header prefix as published, unit, kind) ; kind: dummy / number / text
RR_COLUMNS: dict[int, tuple[str, str, str, str]] = {
    4: ("banking_crisis", "Banking Crisis", "1 = crisis year, 0 = no crisis (RR coding)", "dummy"),
    5: ("banking_crisis_notes", "Banking_Crisis_Notes", "text", "text"),
    6: ("systemic_crisis", "Systemic Crisis", "1 = systemic crisis year, 0 = no (RR coding)", "dummy"),
    7: ("gold_standard", "Gold Standard", "1 = on the gold standard, 0 = not", "dummy"),
    8: ("exch_usd", "exch_usd", "national currency per US dollar", "number"),
    9: ("exch_usd_alt1", "exch_usd_alt1", "national currency per US dollar (alternative series 1)", "number"),
    10: ("exch_usd_alt2", "exch_usd_alt2", "national currency per US dollar (alternative series 2)", "number"),
    11: ("exch_usd_alt3", "exch_usd_alt3", "national currency per US dollar (alternative series 3)", "number"),
    16: ("domestic_debt_default", "Domestic_Debt_In_Default", "1 = domestic debt in default, 0 = not", "dummy"),
    17: ("domestic_debt_notes", "Domestic_Debt_ Notes/Sources", "text", "text"),
    18: (
        "external_default_1",
        "SOVEREIGN EXTERNAL DEBT 1:",
        "1 = in default/restructuring, 0 = not (series 1: excl. post-1975 official-creditor defaults)",
        "dummy",
    ),
    19: (
        "external_default_2",
        "SOVEREIGN EXTERNAL DEBT 2:",
        "1 = in default/restructuring, 0 = not (series 2: incl. post-1975 official-creditor defaults)",
        "dummy",
    ),
    20: ("external_default_notes", "Defaults_External_Notes", "text", "text"),
    21: ("gdp_weighted_default", "GDP_Weighted_default", "as published (GDP-weighted default indicator)", "number"),
    23: ("inflation", "Inflation, Annual percentages", "% per year (change of average consumer prices)", "number"),
    24: ("independence", "Independence", "1 = independent, 0 = not", "dummy"),
    25: (
        "currency_crisis",
        "Currency Crises",
        "1 = currency crisis year, 0 = no; code 2 as published (undocumented)",
        "dummy",
    ),
    26: ("inflation_crisis", "Inflation Crises", "1 = inflation crisis year, 0 = no", "dummy"),
}
#: per-country exchange-rate metadata columns (folded into the flags of the exch_usd* rows)
RR_EXCH_META = {12: "conversion_notes", 13: "national_currency", 14: "exch_primary_source_code", 15: "exch_sources"}
RR_SKIPPED = {22: "header '<' has no published definition"}
RR_MISSING = {"n/a", "na", "n.a.", ""}
#: RR dummies that become episodes (runs of consecutive years coded 1) -> crisis type
RR_EPISODES = {
    "banking_crisis": "banking",
    "currency_crisis": "currency",
    "inflation_crisis": "inflation",
    "domestic_debt_default": "domestic_default",
    "external_default_2": "external_default",
}


def parse_rr(body: bytes, raw_sha: str) -> Build:
    src = SOURCES[RR]
    try:
        wb = Workbook(body)
    except XlsxError as e:
        raise _err(f"Reinhart-Rogoff file: {e}", "not_xlsx") from e
    if RR_SHEET not in wb.sheets:
        raise _err(f"Reinhart-Rogoff file: sheet {RR_SHEET!r} missing (has {sorted(wb.sheets)})")
    rows_in = _sheet_rows(wb, RR_SHEET)
    expect = {0: "Case", 1: "CC3", 2: "Country", 3: "Year", **{c: h for c, (_, h, _, _) in RR_COLUMNS.items()}}
    _need_header(rows_in, expect, RR_SHEET)
    hdr = rows_in[0][1]
    rep: dict[str, Any] = {"omitted_cells": Counter(), "currency_code_2": [], "skipped_columns": RR_SKIPPED}
    comments = sheet_comments(body, wb.sheets[RR_SHEET])
    rep["cell_comments"] = len(comments)
    episode_comments: dict[tuple[str, int, str], str] = {}
    rows: list[dict[str, Any]] = []
    series: dict[tuple[str, str], dict[int, tuple[str, int]]] = defaultdict(dict)  # (cc3, ind) -> year -> (code,row)
    notes: dict[tuple[str, int], dict[str, str]] = defaultdict(dict)
    names: dict[str, str] = {}
    seen: set[tuple[str, int]] = set()
    for rnum, cells in rows_in[1:]:
        if rnum == 2 and all(c.text == "x" for k, c in cells.items()):
            continue  # marker row under the header ('x' in some columns; not data)
        cc = cells.get(1)
        yr = cells.get(3)
        if cc is None or yr is None or not _YEAR.match(yr.text.strip()):
            raise _err(f"{RR_SHEET} row {rnum}: no country code / year")
        geo = cc.text  # verbatim (one code carries a trailing space: 'HUN ')
        y = int(yr.text.strip())
        if (geo, y) in seen:
            raise _err(f"{RR_SHEET}: duplicate country-year {geo!r} {y}")
        seen.add((geo, y))
        names[geo] = cells[2].text.strip() if 2 in cells else geo
        exch_meta = {v: cells[k].text.strip() for k, v in RR_EXCH_META.items() if k in cells and cells[k].text.strip()}
        for col, (ind, _h, unit, kind) in RR_COLUMNS.items():
            c = cells.get(col)
            if c is None:
                continue
            t = c.text.strip()
            if c.kind == "e":
                rep["omitted_cells"][f"{ind}:spreadsheet_error:{t}"] += 1
                continue
            if t.lower() in RR_MISSING:
                rep["omitted_cells"][f"{ind}:{t or 'blank'}"] += 1
                continue
            sel = {"sheet": RR_SHEET, "cell": ref(col, rnum)}
            fl: dict[str, Any] = {"column": hdr[col].text}
            if ref(col, rnum) in comments:
                fl["provider_cell_comment"] = comments[ref(col, rnum)]
                episode_comments[(geo, y, ind)] = comments[ref(col, rnum)]
            if kind == "text":
                rows.append(
                    atlas_row(
                        src=src,
                        indicator=ind,
                        provider_geo=geo,
                        period=yper(y),
                        value_text=t,
                        unit=unit,
                        flags=fl,
                        raw_sha=raw_sha,
                        selector=sel,
                    )
                )
                notes[(geo, y)][ind] = t
                continue
            v = decimal_text(c.text) if c.kind == "n" else None
            if v is None:
                if kind == "number" or c.kind != "n":
                    # text in a numeric column (e.g. a footnote-like remark) is kept as text, never parsed
                    rows.append(
                        atlas_row(
                            src=src,
                            indicator=ind,
                            provider_geo=geo,
                            period=yper(y),
                            value_text=t,
                            unit=unit,
                            flags={**fl, "non_numeric_cell": True},
                            raw_sha=raw_sha,
                            selector=sel,
                        )
                    )
                    rep["omitted_cells"][f"{ind}:kept_as_text"] += 1
                    continue
                rep["omitted_cells"][f"{ind}:non_decimal"] += 1
                continue
            if c.formula:
                fl["formula_cached_value"] = True
            if ind.startswith("exch_usd"):
                fl.update(exch_meta)
            if ind == "currency_crisis" and v == "2":
                fl["undocumented_code"] = "value 2 is published without a definition in the file"
                rep["currency_code_2"].append(f"{geo.strip()}:{y}")
            if kind == "dummy" and v not in ("0", "1", "2"):
                raise _err(f"{RR_SHEET} {ref(col, rnum)}: dummy {ind} = {v!r}")
            rows.append(
                atlas_row(
                    src=src,
                    indicator=ind,
                    provider_geo=geo,
                    period=yper(y),
                    value=v,
                    unit=unit,
                    flags=fl,
                    raw_sha=raw_sha,
                    selector=sel,
                )
            )
            if kind == "dummy":
                series[(geo, ind)][y] = (v, rnum)
    rep["omitted_cells"] = dict(rep["omitted_cells"])
    events = rr_episodes(series, notes, names, src, raw_sha, rep)
    for ev in events:  # cell comments on the coded years of an episode (e.g. "Not a systemic banking crisis")
        ind = next(i for i, t in RR_EPISODES.items() if t == ev.crisis_type)
        got = [
            f"{y}: {episode_comments[(ev.provider_geo, y, ind)]}"
            for y in range(ev.start_year, (ev.end_year or ev.start_year) + 1)
            if (ev.provider_geo, y, ind) in episode_comments
        ]
        if got:
            ev.attributes["provider_cell_comments"] = got
    note = (
        "Reinhart-Rogoff Global Crises Data by Country (HBS BFFS file 20160923): 70 countries, 1800-2016, one row "
        "per country-year as published (CC3 codes; 'HUN ' verbatim). Crisis/default dummies, inflation, USD "
        "exchange rates (with the provider's per-country source notes in flags), notes as text. 'n/a', blank and "
        "spreadsheet-error cells omitted; column '<' (no definition) and the 'x' marker row skipped. No licence "
        "stated by the provider (see rights)."
    )
    return Build(src, raw_sha, rows, events, [], {}, rep, RR_GEO_SCHEME, note)


def runs(years: Iterable[int]) -> list[tuple[int, int]]:
    """Maximal runs of consecutive years: ``[1890, 1891, 1893]`` -> ``[(1890, 1891), (1893, 1893)]``."""
    out: list[tuple[int, int]] = []
    for y in sorted(set(years)):
        if out and y == out[-1][1] + 1:
            out[-1] = (out[-1][0], y)
        else:
            out.append((y, y))
    return out


def rr_episodes(
    series: Mapping[tuple[str, str], Mapping[int, tuple[str, int]]],
    notes: Mapping[tuple[str, int], Mapping[str, str]],
    names: Mapping[str, str],
    src: SourceDef,
    raw_sha: str,
    rep: dict[str, Any],
) -> list[CrisisEvent]:
    col_of = {ind: col for col, (ind, *_r) in RR_COLUMNS.items()}
    events: list[CrisisEvent] = []
    systemic_outside = 0
    for (geo, ind), by_year in sorted(series.items()):
        ctype = RR_EPISODES.get(ind)
        if ctype is None:
            continue
        ones = [y for y, (v, _r) in by_year.items() if v == "1"]
        cc3 = geo.strip()
        iso3 = _iso3(geo, RR_GEO_SCHEME)
        for a, b in runs(ones):
            rows_used = [by_year[y][1] for y in range(a, b + 1)]
            ev = CrisisEvent(
                eid=f"{KG_SOURCE_PREFIX}:rr:{ctype}:{cc3}:{a}",
                source=RR,
                crisis_type=ctype,
                coded_as=f"{RR_SHEET} / {RR_COLUMNS[col_of[ind]][1].rstrip(':')}",
                provider_geo=geo,
                iso3=iso3,
                start=(f"{a:04d}-01-01", 9),
                end=(f"{b:04d}-01-01", 9),
                end_kind="last consecutive year coded 1",
                label_en=_event_label(
                    ctype, names.get(geo, cc3), f"{a}" if a == b else f"{a}-{b}", "Reinhart & Rogoff"
                ),
                raw_sha256=raw_sha,
                selector={
                    "sheet": RR_SHEET,
                    "column": col_letters(col_of[ind]),
                    "rows": [min(rows_used), max(rows_used)],
                },
                derivation="rr_consecutive_years: a maximal run of consecutive country-years coded 1 in this "
                "column (REVA's grouping of the provider's yearly codes; the provider codes years, not episodes)",
            )
            if ind == "banking_crisis":
                sys_years = [
                    y for y in range(a, b + 1) if series.get((geo, "systemic_crisis"), {}).get(y, ("0", 0))[0] == "1"
                ]
                ev.attributes["systemic_crisis_years"] = sys_years
            if ind == "external_default_2":
                s1 = series.get((geo, "external_default_1"), {})
                ev.attributes["years_also_in_series_1"] = [y for y in range(a, b + 1) if s1.get(y, ("0", 0))[0] == "1"]
            note_key = {
                "banking_crisis": "banking_crisis_notes",
                "domestic_debt_default": "domestic_debt_notes",
                "external_default_2": "external_default_notes",
            }.get(ind)
            if note_key:
                texts = [t for t in dict.fromkeys(notes.get((geo, y), {}).get(note_key) for y in range(a, b + 1)) if t]
                if texts:
                    ev.attributes["provider_notes"] = [t[:2000] for t in texts[:5]]
            events.append(ev)
    for (geo, ind), by_year in series.items():
        if ind != "systemic_crisis":
            continue
        bank = series.get((geo, "banking_crisis"), {})
        systemic_outside += sum(1 for y, (v, _r) in by_year.items() if v == "1" and bank.get(y, ("0", 0))[0] != "1")
    rep["systemic_years_without_banking_crisis_code"] = systemic_outside
    rep["events_by_type"] = dict(Counter(e.crisis_type for e in events))
    return events


# =========================================================================================== JST Macrohistory
JST_SHEET = "Sheet1"
JST_GEO_SCHEME = "iso3"
#: Variable labels of the R6 documentation ("Variable names and descriptive labels in Stata", JST R6
#: documentation pp. 5-7), quoted; the unit column is the label's own unit wording.
JST_VARIABLES: dict[str, tuple[str, str]] = {
    "pop": ("Population", "persons (thousands as documented per country)"),
    "rgdpmad": ("Real GDP per capita (PPP, 1990 Int$, Maddison)", "1990 international dollars"),
    "rgdpbarro": ("Real GDP per capita (index, 2005=100)", "index, 2005=100"),
    "rconsbarro": ("Real consumption per capita (index, 2006=100)", "index, 2006=100"),
    "gdp": ("GDP (nominal, local currency)", "nominal, local currency"),
    "iy": ("Investment-to-GDP ratio", "ratio"),
    "cpi": ("Consumer prices (index, 1990=100)", "index, 1990=100"),
    "ca": ("Current account (nominal, local currency)", "nominal, local currency"),
    "imports": ("Imports (nominal, local currency)", "nominal, local currency"),
    "exports": ("Exports (nominal, local currency)", "nominal, local currency"),
    "narrowm": ("Narrow money (nominal, local currency)", "nominal, local currency"),
    "money": ("Broad money (nominal, local currency)", "nominal, local currency"),
    "stir": ("Short-term interest rate (nominal, percent per year)", "% per year"),
    "ltrate": ("Long-term interest rate (nominal, percent per year)", "% per year"),
    "hpnom": ("House prices (nominal index, 1990=100)", "index, 1990=100"),
    "unemp": ("Unemployment rate (percent)", "%"),
    "wage": ("Wages (index, 1990= 100)", "index, 1990=100"),
    "debtgdp": ("Public debt-to-GDP ratio", "ratio"),
    "revenue": ("Government revenues (nominal, local currency)", "nominal, local currency"),
    "expenditure": ("Government expenditure (nominal, local currency)", "nominal, local currency"),
    "xrusd": ("USD exchange rate (local currency/USD)", "local currency per US dollar"),
    "tloans": ("Total loans to non-financial private sector (nominal, local currency)", "nominal, local currency"),
    "tmort": ("Mortgage loans to non-financial private sector (nominal, local currency)", "nominal, local currency"),
    "thh": ("Total loans to households (nominal, local currency)", "nominal, local currency"),
    "tbus": ("Total loans to business (nominal, local currency)", "nominal, local currency"),
    "bdebt": ("Corporate debt (nominal, local currency)", "nominal, local currency"),
    "lev": ("Banks, capital ratio (in %)", "%"),
    "ltd": ("Banks, loans-to-deposits ratio (in %)", "%"),
    "noncore": ("Banks, noncore funding ratio (in %)", "%"),
    "crisisJST": (
        "Systemic financial crises (0-1 dummy); included since R5",
        "1 = first year of a systemic financial crisis, 0 = not",
    ),
    "crisisJST_old": (
        "Systemic financial crises (0-1 dummy); as coded in all prior releases (R1 - R4)",
        "1 = first year of a systemic financial crisis (R1-R4 coding), 0 = not",
    ),
    "peg": ("Peg dummy", "1 = peg, 0 = not"),
    "peg_strict": ("Strict peg dummy", "1 = strict peg, 0 = not"),
    "peg_type": ("Peg type (BASE, PEG, FLOAT)", "category"),
    "peg_base": ("Peg base (GBR, USA, DEU, HYBRID, NA)", "category"),
    "JSTtrilemmaIV": ("JST trilemma instrument (raw base rate changes)", "as published"),
    "eq_tr": ("Equity total return, nominal. r[t] = [[p[t] + d[t]] / p[t-1] ] - 1", "ratio (return)"),
    "housing_tr": ("Housing total return, nominal. r[t] = [[p[t] + d[t]] / p[t-1] ] - 1", "ratio (return)"),
    "bond_tr": ("Government bond total return, nominal. r[t] = [[p[t] + coupon[t]] / p[t-1] ] - 1", "ratio (return)"),
    "bill_rate": ("Bill rate, nominal. r[t] = coupon[t] / p[t-1]", "ratio (rate)"),
    "rent_ipolated": ("1 if housing rental yields interpolated e.g. wartime", "1 = interpolated"),
    "housing_capgain_ipolated": (
        "1 if housing capital gains and total returns interpolated e.g. wartime",
        "1 = interpolated",
    ),
    "housing_capgain": ("Housing capital gain, nominal. cg[t] = [ p[t] / p[t-1] ] - 1", "ratio (return)"),
    "housing_rent_rtn": ("Housing rental return. dp_rtn[t] = rent[t]/p[t-1]", "ratio (return)"),
    "housing_rent_yd": ("Housing rental yield. dp[t] = rent[t]/p[t]", "ratio (yield)"),
    "eq_capgain": ("Equity capital gain, nominal. cg[t] = [ p[t] / p[t-1] ] - 1", "ratio (return)"),
    "eq_dp": ("Equity dividend yield. dp[t] = dividend[t]/p[t]", "ratio (yield)"),
    "eq_capgain_interp": ("1 if equity cap. gain interpolated to cover exchange closure", "1 = interpolated"),
    "eq_tr_interp": ("1 if equity total return interpolated to cover exchange closure", "1 = interpolated"),
    "eq_dp_interp": ("1 if equity dividend interpolated or assumed zero to cover exchange closure", "1 = interpolated"),
    "bond_rate": ("Gov. bond rate, rate[t] = coupon[t] / p[t-1], or yield to maturity at t", "ratio (rate)"),
    "eq_div_rtn": ("Equity dividend return. dp_rtn[t] = dividend[t]/p[t-1]", "ratio (return)"),
    "capital_tr": ("Tot. rtn. on wealth, nominal. Wtd. avg. of housing, equity, bonds and bills", "ratio (return)"),
    "risky_tr": ("Tot. rtn. on risky assets, nominal. Wtd. avg. of housing and equity", "ratio (return)"),
    "safe_tr": ("Tot. rtn. on safe assets, nominal. Equally wtd. avg. of bonds and bills", "ratio (return)"),
}
JST_ID_COLUMNS = ("year", "country", "iso", "ifs")
JST_TEXT_VARIABLES = {"peg_type", "peg_base"}
JST_CRISIS_DEFINITION = (
    "The banking crisis variable (crisisJST) takes unity in the first year a country is experiencing a "
    "systemic banking crisis, and zero else. An episode of banking distress is coded as a systemic banking "
    "crisis if it is characterized by major bank failures, banking panics, substantial losses in the banking "
    "sector, significant recapitalization, and/or significant government intervention. (JST Financial Crisis "
    "Chronology, 25 February 2021, section 1)"
)


def check_jst_licence(body: bytes) -> None:
    text = re.sub(r"\s+", " ", re.sub(rb"<[^>]+>", b" ", body).decode("utf-8", "replace")).upper()
    if JST_LICENCE_MARKER not in text:
        raise _err("JST licence page no longer states CC BY-NC-SA 4.0: refused (licence check)", "licence_changed")


def parse_jst(body: bytes, raw_sha: str) -> Build:
    src = SOURCES[JST]
    try:
        wb = Workbook(body)
    except XlsxError as e:
        raise _err(f"JST file: {e}", "not_xlsx") from e
    if JST_SHEET not in wb.sheets:
        raise _err(f"JST file: sheet {JST_SHEET!r} missing")
    rows_in = _sheet_rows(wb, JST_SHEET)
    hdr = {k: c.text.strip() for k, c in rows_in[0][1].items()}
    cols = {v: k for k, v in hdr.items()}
    missing = [v for v in (*JST_ID_COLUMNS, "crisisJST") if v not in cols]
    if missing:
        raise _err(f"JST file: columns {missing} missing")
    unknown = sorted(set(hdr.values()) - set(JST_VARIABLES) - set(JST_ID_COLUMNS))
    if unknown:
        raise _err(f"JST file: undocumented columns {unknown} (update JST_VARIABLES from the documentation)")
    rows: list[dict[str, Any]] = []
    rep: dict[str, Any] = {"omitted_cells": Counter()}
    crisis: dict[tuple[str, int], tuple[int, str | None]] = {}
    names: dict[str, str] = {}
    seen: set[tuple[str, int]] = set()
    for rnum, cells in rows_in[1:]:
        iso_c, y_c = cells.get(cols["iso"]), cells.get(cols["year"])
        if iso_c is None or y_c is None or not _YEAR.match(y_c.text.strip()):
            raise _err(f"JST row {rnum}: no iso/year")
        geo, y = iso_c.text.strip(), int(y_c.text.strip())
        if (geo, y) in seen:
            raise _err(f"JST: duplicate {geo} {y}")
        seen.add((geo, y))
        names[geo] = cells[cols["country"]].text.strip() if cols["country"] in cells else geo
        for col, var in hdr.items():
            if var in JST_ID_COLUMNS:
                continue
            c = cells.get(col)
            if c is None:
                continue
            label, unit = JST_VARIABLES[var]
            sel = {"sheet": JST_SHEET, "cell": ref(col, rnum)}
            fl: dict[str, Any] = {"label": label, "release": "R6"}
            if var in JST_TEXT_VARIABLES:
                t = c.text.strip()
                if not t:
                    continue
                rows.append(
                    atlas_row(
                        src=src,
                        indicator=var,
                        provider_geo=geo,
                        period=yper(y),
                        value_text=t,
                        unit=unit,
                        flags=fl,
                        raw_sha=raw_sha,
                        selector=sel,
                    )
                )
                continue
            v = num(c)
            if v is None:
                rep["omitted_cells"][f"{var}:{c.kind}"] += 1
                continue
            if var in ("crisisJST", "crisisJST_old"):
                if v not in ("0", "1"):
                    raise _err(f"JST {ref(col, rnum)}: {var} = {v!r}")
                fl["definition"] = JST_CRISIS_DEFINITION
            rows.append(
                atlas_row(
                    src=src,
                    indicator=var,
                    provider_geo=geo,
                    period=yper(y),
                    value=v,
                    unit=unit,
                    flags=fl,
                    raw_sha=raw_sha,
                    selector=sel,
                )
            )
            if var == "crisisJST" and v == "1":
                old = cells.get(cols["crisisJST_old"]) if "crisisJST_old" in cols else None
                crisis[(geo, y)] = (rnum, num(old))
    events = []
    for (geo, y), (rnum, old_v) in sorted(crisis.items()):
        ev = CrisisEvent(
            eid=f"{KG_SOURCE_PREFIX}:jst:systemic_banking:{geo}:{y}",
            source=JST,
            crisis_type="systemic_banking",
            coded_as="crisisJST (JST Macrohistory R6)",
            provider_geo=geo,
            iso3=_iso3(geo, JST_GEO_SCHEME),
            start=(f"{y:04d}-01-01", 9),
            label_en=_event_label("systemic_banking", names.get(geo, geo), str(y), "Jordà-Schularick-Taylor R6"),
            raw_sha256=raw_sha,
            selector={"sheet": JST_SHEET, "cell": ref(cols["crisisJST"], rnum)},
            attributes={"crisisJST_old_same_year": old_v, "definition": JST_CRISIS_DEFINITION},
        )
        events.append(ev)
    rep["omitted_cells"] = dict(rep["omitted_cells"])
    rep["events"] = len(events)
    note = (
        "Jordà-Schularick-Taylor Macrohistory Database R6: 18 advanced economies, 1870-2020, every variable of the "
        "release as published (labels/units from the R6 documentation in flags/unit). crisisJST = first year of a "
        "systemic financial crisis. CC BY-NC-SA 4.0: NON-COMMERCIAL, share-alike (see rights)."
    )
    return Build(src, raw_sha, rows, events, [], {}, rep, JST_GEO_SCHEME, note)


# =========================================================================================== ESRB
ESRB_GEO_SCHEME = "esrb"
ESRB_SHEETS = {"Systemic crises": "systemic", "Residual events": "residual"}
#: field columns (header prefix as published) -> attribute key
ESRB_FIELDS = {
    0: ("Country", "country"),
    1: ("Event", "event"),
    2: ("Start date", "start_date"),
    3: ("End of crisis management date", "end_of_crisis_management"),
    4: ("System back", "back_to_normal"),
    5: ("Systemic crisis", "systemic_crisis"),
    6: ("Accelerator and motivation", "accelerator_and_motivation"),
    7: ("Brief desrciption of the identified event", "description"),
    8: ("Crisis management policies", "crisis_management_policies"),
    9: ("External support", "external_support"),
    10: ("Domestic vs imported", "domestic_vs_imported"),
    11: ("Date of the first default", "date_of_first_default"),
    12: ("Currency / BoP / Capital flow", "currency_bop_capital_flow"),
    13: ("Sovereign", "sovereign"),
    14: ("Banking", "banking"),
    15: ("Significant asset price correction", "significant_asset_price_correction"),
    16: ("Transition", "transition"),
    17: ("Macropru relevant", "macropru_relevant"),
    18: ("Macropru relevance explanation", "macropru_relevance_explanation"),
}
ESRB_DUMMIES = {5, 9, 10, 12, 13, 14, 15, 16, 17}
ESRB_CATEGORY_COLUMNS = {12, 13, 14, 15, 16}
ESRB_TEXT = {6, 7, 8, 11, 18}
ESRB_DOMESTIC = {"0": "domestic", "1": "imported (originated abroad)", "2": "both"}
#: The five ESRB intermediate macroprudential objectives (Recommendation ESRB/2013/1), which the codebook
#: (ESRB OP 13, Annex A, "Accelerator and motivation") says this field indicates. Patterns are matched
#: case-insensitively in the part of the field before "Motivation" only.
ESRB_OBJECTIVES: dict[str, tuple[str, str]] = {
    "excessive_credit_growth_and_leverage": ("excessive credit growth and leverage", r"excessive credit growth"),
    "mismatches_and_market_illiquidity": ("mismatches and market illiquidity", r"\bmismatches\b"),
    "exposure_concentration": ("exposure concentration", r"exposure concentration"),
    "misaligned_incentives": ("misaligned incentives", r"misaligned incentives"),
    "infrastructure_resilience": ("infrastructure resilience", r"infrastructure resilience"),
}
ESRB_ACCELERATOR_DEFINITION = (
    "Accelerator and motivation: Indicator of one or more of the five ESRB intermediate macroprudential "
    "objectives (excessive credit growth and leverage, mismatches and market illiquidity, exposure "
    "concentration, misaligned incentives, infrastructure resilience) - see the Recommendation of the ESRB of "
    "4 April 2013 on intermediate objectives and instruments of macroprudential policy (ESRB/2013/1) (OJ 2013/C "
    "170/01). Format: text. (ESRB Occasional Paper 13, Annex A)"
)
ESRB_NOTE_PREFIX = "Notes:"


def accelerator_objectives(text: str) -> list[str]:
    """Objective keys named in the accelerator part (before 'Motivation') of an ESRB field, in text order."""
    head = re.split(r"motivation\s*:", text, maxsplit=1, flags=re.IGNORECASE)[0].lower()
    found = []
    for key, (_label, pat) in ESRB_OBJECTIVES.items():
        m = re.search(pat, head)
        if m:
            found.append((m.start(), key))
    return [k for _p, k in sorted(found)]


def condition_id(key: str) -> str:
    return f"{KG_SOURCE_PREFIX}:condition:esrb_objective:{key}"


def parse_esrb(body: bytes, raw_sha: str) -> Build:
    src = SOURCES[ESRB]
    try:
        wb = Workbook(body)
    except XlsxError as e:
        raise _err(f"ESRB file: {e}", "not_xlsx") from e
    missing = [s for s in ESRB_SHEETS if s not in wb.sheets]
    if missing:
        raise _err(f"ESRB file: sheets {missing} missing (has {sorted(wb.sheets)})")
    rows: list[dict[str, Any]] = []
    events: list[CrisisEvent] = []
    relations: list[CodedRelation] = []
    conditions: dict[str, dict[str, str]] = {}
    rep: dict[str, Any] = {"omitted_cells": Counter(), "accelerator_not_coded": [], "notes": []}
    used_keys: set[tuple[str, str, int]] = set()
    for sheet, kind in ESRB_SHEETS.items():
        rows_in = _sheet_rows(wb, sheet)
        _need_header(rows_in, {k: h for k, (h, _) in ESRB_FIELDS.items()}, sheet)
        hdr = rows_in[0][1]
        for rnum, cells in rows_in[1:]:
            c0 = cells.get(0)
            if c0 is None:
                continue
            if c0.text.strip().startswith(ESRB_NOTE_PREFIX):
                rep["notes"].append(c0.text.strip())
                continue
            if rnum == 2 and 1 not in cells:  # legend row ("0= NO 1= YES")
                continue
            geo = c0.text.strip()
            start_c, end_c, normal_c = cells.get(2), cells.get(3), cells.get(4)
            st = ym_text(start_c.text) if start_c else None
            if st is None or start_c is None:
                raise _err(f"{sheet} {ref(2, rnum)}: start date {start_c.text if start_c else None!r} is not YYYY-MM")
            key = (kind, geo, st[0])
            if key in used_keys:
                raise _err(f"{sheet}: two events of {geo} start in {st[0]} (atlas cell would collide)")
            used_keys.add(key)
            ev_no = cells[1].text.strip() if 1 in cells else ""
            iso3 = _iso3(geo, ESRB_GEO_SCHEME)
            base = {"sheet": sheet, "row": rnum}
            period = yper(st[0])
            ind_prefix = f"{kind}"
            attrs: dict[str, Any] = {"event_number": ev_no, "sheet": sheet}
            rows.append(
                atlas_row(
                    src=src,
                    indicator=f"{ind_prefix}|event_start",
                    provider_geo=geo,
                    period=period,
                    value="1",
                    unit="1 = an event of this sheet starts in the year",
                    flags={"start_date": start_c.text.strip(), "event_number": ev_no},
                    raw_sha=raw_sha,
                    selector={**base, "cell": ref(2, rnum)},
                )
            )
            for col, (_header, akey) in ESRB_FIELDS.items():
                if col in (0, 1, 2):
                    continue
                c = cells.get(col)
                if c is None or not c.text.strip() or c.text.strip().lower() in ("n.a.", "n.a", "none.", "none"):
                    if c is not None and c.text.strip():
                        rep["omitted_cells"][f"{akey}:{c.text.strip()}"] += 1
                    continue
                t = c.text.strip()
                sel = {**base, "cell": ref(col, rnum)}
                fl = {"field": hdr[col].text.strip(), "event_number": ev_no}
                if col in ESRB_DUMMIES and c.kind == "n" and (v := decimal_text(c.text)) is not None:
                    rows.append(
                        atlas_row(
                            src=src,
                            indicator=f"{ind_prefix}|{akey}",
                            provider_geo=geo,
                            period=period,
                            value=v,
                            unit="0 = no, 1 = yes" if col != 10 else "0 = domestic, 1 = imported, 2 = both",
                            flags=fl,
                            raw_sha=raw_sha,
                            selector=sel,
                        )
                    )
                    attrs[akey] = ESRB_DOMESTIC.get(v, v) if col == 10 else v
                    continue
                if col in (3, 4) and (ym := ym_text(t)) is not None:
                    rows.append(
                        atlas_row(
                            src=src,
                            indicator=f"{ind_prefix}|{akey}",
                            provider_geo=geo,
                            period=period,
                            value_text=f"{ym[0]:04d}-{ym[1]:02d}",
                            unit="year-month (YYYY-MM)",
                            flags=fl,
                            raw_sha=raw_sha,
                            selector=sel,
                        )
                    )
                    attrs[akey] = f"{ym[0]:04d}-{ym[1]:02d}"
                    continue
                rows.append(
                    atlas_row(
                        src=src,
                        indicator=f"{ind_prefix}|{akey}",
                        provider_geo=geo,
                        period=period,
                        value_text=t,
                        unit="text",
                        flags=fl,
                        raw_sha=raw_sha,
                        selector=sel,
                    )
                )
                attrs[akey] = t[:4000]
            ctype = "systemic_financial" if kind == "systemic" else "residual_financial_stress"
            ev = CrisisEvent(
                eid=f"{KG_SOURCE_PREFIX}:esrb:{kind}:{_country(iso3, geo)}:{st[0]:04d}-{st[1]:02d}",
                source=ESRB,
                crisis_type=ctype,
                coded_as=f"{sheet} (ESRB European financial crises database)",
                provider_geo=geo,
                iso3=iso3,
                start=(f"{st[0]:04d}-{st[1]:02d}-01", 10),
                label_en=_event_label(
                    ctype, geo.rstrip("*"), f"{st[0]:04d}-{st[1]:02d}", "ESRB financial crises database"
                ),
                raw_sha256=raw_sha,
                selector={**base, "cell": ref(2, rnum)},
                attributes=attrs,
            )
            em = ym_text(end_c.text) if end_c is not None else None
            if em is not None:
                ev.end = (f"{em[0]:04d}-{em[1]:02d}-01", 10)
                ev.end_kind = "End of crisis management date (end of the acute phase; ESRB OP 13 section 2)"
            nm = ym_text(normal_c.text) if normal_c is not None else None
            if nm is not None:
                ev.coded_dates.append(
                    {
                        "field": hdr[4].text.strip(),
                        "time": f"{nm[0]:04d}-{nm[1]:02d}-01",
                        "precision": 10,
                        "selector": {**base, "cell": ref(4, rnum)},
                    }
                )
            fd = cells.get(11)
            if fd is not None and (fdm := ym_text(fd.text[:7])) is not None:
                ev.coded_dates.append(
                    {
                        "field": hdr[11].text.strip(),
                        "time": f"{fdm[0]:04d}-{fdm[1]:02d}-01",
                        "precision": 10,
                        "selector": {**base, "cell": ref(11, rnum)},
                        "text": fd.text.strip()[:500],
                    }
                )
            ev.attributes["categories"] = [
                ESRB_FIELDS[c][1] for c in sorted(ESRB_CATEGORY_COLUMNS) if attrs.get(ESRB_FIELDS[c][1]) == "1"
            ]
            events.append(ev)
            acc = cells.get(6)
            if acc is not None and acc.text.strip() and acc.text.strip().lower() not in ("n.a.", "none."):
                objs = accelerator_objectives(acc.text)
                if not objs:
                    rep["accelerator_not_coded"].append(f"{sheet}:{geo}:{st[0]}-{st[1]:02d}")
                for okey in objs:
                    cid = condition_id(okey)
                    conditions[cid] = {
                        "label_en": f"{ESRB_OBJECTIVES[okey][0]} (ESRB intermediate macroprudential objective)",
                        "description_en": "vulnerability category of Recommendation ESRB/2013/1, as coded in the ESRB "
                        "European financial crises database field 'Accelerator and motivation'",
                    }
                    relations.append(
                        CodedRelation(
                            rid=f"{KG_SOURCE_PREFIX}:esrb:accelerator:{ev.eid.split(':', 2)[2]}:{okey}",
                            family="causal",
                            relation="contributing_factor",
                            subject=cid,
                            object=ev.eid,
                            rule_id="esrb_fcdb_accelerator",
                            coded_as="Accelerator and motivation",
                            polarity="promotes",
                            mechanism=acc.text.strip()[:4000],
                            qualifiers={
                                "codebook_definition": ESRB_ACCELERATOR_DEFINITION,
                                "objective": ESRB_OBJECTIVES[okey][0],
                                "matched_in": "text before 'Motivation'",
                                "assessed_by": "national authorities (ESRB database contributors)",
                                "provider_geo": geo,
                                "event_sheet": sheet,
                            },
                            asserting_source=_asserting(src, raw_sha, {**base, "cell": ref(6, rnum)}),
                        )
                    )
    rep["omitted_cells"] = dict(rep["omitted_cells"])
    rep["events_by_type"] = dict(Counter(e.crisis_type for e in events))
    rep["claims"] = len(relations)
    note = (
        "ESRB European financial crises database (file esrb.fcdb20220120): systemic crises and residual events of "
        "financial stress in EU/EEA countries; one atlas cell per event field at the event's start year (indicator "
        "'<systemic|residual>|<field>'); dates as YYYY-MM text, dummies as published, texts verbatim. Country codes "
        "ISO 3166 alpha-2 ('UK*' -> GBR per the file's note). Free use with citation of the ESRB."
    )
    return Build(src, raw_sha, rows, events, relations, conditions, rep, ESRB_GEO_SCHEME, note)


PARSERS = {LV: parse_lv, RR: parse_rr, JST: parse_jst, ESRB: parse_esrb}


def parse(src_key: str, body: bytes) -> Build:
    return PARSERS[src_key](body, sha256_hex(body))


# =========================================================================================== KG / datasets
KG_CUSTOM_PROPERTIES = [
    {
        "pid": "ecrises:crisis_type",
        "value_kind": "string",
        "reva_predicate": "coded_crisis_type",
        "label_en": "crisis type as coded by the dataset",
        "label_fr": "type de crise codé par la base de données",
    },
    {
        "pid": "ecrises:same_crisis_window",
        "value_kind": "item",
        "reva_predicate": "same_crisis_window",
        "label_en": "in the same crisis window as (Laeven-Valencia twin/triple definition; co-occurrence, not causal)",
        "label_fr": "dans la même fenêtre de crise que (définition Laeven-Valencia ; coïncidence, non causale)",
        "inverse_name": "same_crisis_window_of",
        "inverse_label_en": "in the same crisis window as (co-occurrence, not causal)",
        "inverse_label_fr": "dans la même fenêtre de crise que (coïncidence, non causale)",
    },
    {
        "pid": "ecrises:coded_date",
        "value_kind": "time",
        "reva_predicate": "coded_date",
        "label_en": "other coded date of the crisis (field named in the qualifier)",
        "label_fr": "autre date codée de la crise (champ nommé dans le qualificatif)",
    },
]
KG_WIKIDATA_PIDS = ("P31", "P17", "P580", "P582")


def kg_records(build: Build, *, qid_for_iso3: Mapping[str, str]) -> dict[str, Any]:
    """Entities and statements (``reva.kg.model`` dataclasses) of a build's events and condition entities."""
    from reva.kg.model import KgEntity, KgStatement

    src = build.src
    ents: list[Any] = []
    sts: list[Any] = []
    refs = [{"stated_in": CITATIONS[src.key], "dataset_id": src.dataset_id}]

    def tval(iso: str, prec: int) -> dict[str, Any]:
        return {"type": "time", "time": iso, "precision": prec}

    for ev in build.events:
        desc = f"{CRISIS_TYPES[ev.crisis_type][0]} coded in {src.provider}"
        ents.append(
            KgEntity(qid=ev.eid, kind="event", labels={"en": ev.label_en[:500]}, descriptions={"en": desc[:500]})
        )
        ref_ev = [{**refs[0], **{k: v for k, v in ev.selector.items()}}]
        cls = CRISIS_TYPES[ev.crisis_type][1]
        if cls:
            sts.append(
                KgStatement(
                    subject_qid=ev.eid,
                    property_pid="P31",
                    object_qid=cls,
                    qualifiers={
                        "coded_as": ev.coded_as,
                        "class_mapping": "agent_mapped (registry: atlas_ecrises.WIKIDATA_CLASSES)",
                    },
                    statement_id=f"{ev.eid}:P31",
                    raw_sha256=ev.raw_sha256,
                    references=ref_ev,
                )
            )
        sts.append(
            KgStatement(
                subject_qid=ev.eid,
                property_pid="ecrises:crisis_type",
                value={"type": "string", "value": CRISIS_TYPES[ev.crisis_type][0]},
                qualifiers={"coded_as": ev.coded_as, "type_key": ev.crisis_type},
                statement_id=f"{ev.eid}:type",
                raw_sha256=ev.raw_sha256,
                references=ref_ev,
            )
        )
        q = qid_for_iso3.get(ev.iso3 or "")
        if q:
            sts.append(
                KgStatement(
                    subject_qid=ev.eid,
                    property_pid="P17",
                    object_qid=q,
                    qualifiers={"iso3": ev.iso3, "provider_geo": ev.provider_geo},
                    statement_id=f"{ev.eid}:P17",
                    raw_sha256=ev.raw_sha256,
                    references=ref_ev,
                )
            )
        quals: dict[str, Any] = {"coded_as": ev.coded_as}
        if ev.derivation:
            quals["derivation"] = ev.derivation
        sts.append(
            KgStatement(
                subject_qid=ev.eid,
                property_pid="P580",
                value=tval(*ev.start),
                qualifiers=quals,
                statement_id=f"{ev.eid}:P580",
                raw_sha256=ev.raw_sha256,
                references=ref_ev,
            )
        )
        if ev.end:
            sts.append(
                KgStatement(
                    subject_qid=ev.eid,
                    property_pid="P582",
                    value=tval(*ev.end),
                    qualifiers={**quals, "end_meaning": ev.end_kind or ""},
                    statement_id=f"{ev.eid}:P582",
                    raw_sha256=ev.raw_sha256,
                    references=ref_ev,
                )
            )
        for k, cd in enumerate(ev.coded_dates):
            sts.append(
                KgStatement(
                    subject_qid=ev.eid,
                    property_pid="ecrises:coded_date",
                    value=tval(cd["time"], cd["precision"]),
                    qualifiers={"field": cd["field"], **({"text": cd["text"]} if cd.get("text") else {})},
                    statement_id=f"{ev.eid}:date:{k}",
                    raw_sha256=ev.raw_sha256,
                    references=[{**refs[0], **cd.get("selector", {})}],
                )
            )
    for r in build.relations:
        if r.family != "context":
            continue  # causal claims go to kg.causal_claim (load_dataset_claims), never to KG predicates
        sts.append(
            KgStatement(
                subject_qid=r.subject,
                property_pid="ecrises:same_crisis_window",
                object_qid=r.object,
                qualifiers={"coded_as": r.coded_as, "rule_id": r.rule_id, "not_causal": True, **r.qualifiers},
                statement_id=r.rid,
                raw_sha256=build.raw_sha256,
                references=[{**refs[0], **r.asserting_source.get("selector", {})}],
            )
        )
    for cid, lab in sorted(build.conditions.items()):
        ents.append(
            KgEntity(
                qid=cid, kind="condition", labels={"en": lab["label_en"]}, descriptions={"en": lab["description_en"]}
            )
        )
    return {"entities": ents, "statements": sts}


def ensure_kg_properties(engine: Any) -> dict[str, Any]:
    from reva.adapters.connectors.wikidata import load_allowlist
    from reva.kg.harvest import kg_properties
    from reva.kg.model import KgProperty, register_properties

    props = [p for p in kg_properties(load_allowlist()) if p.pid in KG_WIKIDATA_PIDS]
    if len(props) != len(KG_WIKIDATA_PIDS):
        raise ValueError(f"{KG_WIKIDATA_PIDS} must all be in registry/kg/properties.yaml")
    props += [KgProperty(**p) for p in KG_CUSTOM_PROPERTIES]
    return register_properties(engine, props).__dict__


def events_table(build: Build) -> Any:
    import pyarrow as pa  # type: ignore[import-untyped]

    recs = [
        {
            "event_id": e.eid,
            "source_key": build.src.source_key,
            "crisis_type": e.crisis_type,
            "crisis_type_label": CRISIS_TYPES[e.crisis_type][0],
            "wikidata_class": CRISIS_TYPES[e.crisis_type][1],
            "coded_as": e.coded_as,
            "provider_geo": e.provider_geo,
            "iso3": e.iso3,
            "start_time": e.start[0],
            "start_precision": e.start[1],
            "end_time": e.end[0] if e.end else None,
            "end_precision": e.end[1] if e.end else None,
            "end_meaning": e.end_kind,
            "derivation": e.derivation,
            "label_en": e.label_en,
            "attributes": canon(e.attributes),
            "coded_dates": json.dumps(e.coded_dates, sort_keys=True, ensure_ascii=False),
            "selector": canon(e.selector),
            "raw_sha256": e.raw_sha256,
        }
        for e in build.events
    ]
    return pa.Table.from_pylist(recs, schema=_event_schema())


def _event_schema() -> Any:
    import pyarrow as pa

    s, i = pa.string(), pa.int32()
    return pa.schema(
        [
            ("event_id", s),
            ("source_key", s),
            ("crisis_type", s),
            ("crisis_type_label", s),
            ("wikidata_class", s),
            ("coded_as", s),
            ("provider_geo", s),
            ("iso3", s),
            ("start_time", s),
            ("start_precision", i),
            ("end_time", s),
            ("end_precision", i),
            ("end_meaning", s),
            ("derivation", s),
            ("label_en", s),
            ("attributes", s),
            ("coded_dates", s),
            ("selector", s),
            ("raw_sha256", s),
        ]
    )


def relations_table(build: Build) -> Any:
    import pyarrow as pa

    s = pa.string()
    schema = pa.schema(
        [
            ("relation_id", s),
            ("family", s),
            ("relation", s),
            ("subject_id", s),
            ("object_id", s),
            ("tier", s),
            ("rule_id", s),
            ("coded_as", s),
            ("polarity", s),
            ("mechanism", s),
            ("qualifiers", s),
            ("asserting_source", s),
            ("raw_sha256", s),
        ]
    )
    recs = [
        {
            "relation_id": r.rid,
            "family": r.family,
            "relation": r.relation,
            "subject_id": r.subject,
            "object_id": r.object,
            "tier": r.tier,
            "rule_id": r.rule_id,
            "coded_as": r.coded_as,
            "polarity": r.polarity,
            "mechanism": r.mechanism,
            "qualifiers": canon(r.qualifiers),
            "asserting_source": canon(r.asserting_source),
            "raw_sha256": r.asserting_source.get("raw_sha256"),
        }
        for r in build.relations
    ]
    return pa.Table.from_pylist(recs, schema=schema)


def register_side_datasets(run: Any, build: Build) -> dict[str, Any]:
    """Events and coded relations as evidence datasets (``register_dataset``, worker role)."""
    from sqlalchemy import text

    from reva.evidence.locks import take_evidence_write_lock
    from reva.simulation.datasets import register_dataset

    out: dict[str, Any] = {}
    with run.engine.begin() as conn:
        take_evidence_write_lock(conn)
        acq = conn.execute(
            text(
                "SELECT id::text FROM evidence.acquisition WHERE raw_sha256 = :s AND origin = :o "
                "ORDER BY recorded_at DESC, id DESC LIMIT 1"
            ),
            {"s": build.raw_sha256, "o": run.origin},
        ).scalar_one()
        for what, concept, table in (
            ("events", "events.crisis_episodes", events_table(build)),
            ("coded_relations", "causal.coded_relations", relations_table(build)),
        ):
            if table.num_rows == 0:
                continue
            rec = register_dataset(
                conn,
                run.store,
                dataset_key=f"ecrises.{build.src.atlas_source}.{what}",
                concept_id=concept,
                subject_key=f"ecrises.{build.src.atlas_source}",
                table=table,
                source_acquisition_ids=[acq],
                parser_version=build.src.parser_version,
                origin=run.origin,
                coverage={"rows": table.num_rows, "dataset_id": build.src.dataset_id},
                actor=run.actor,
                rights={**RIGHTS[build.src.key], "source_key": build.src.source_key},
            )
            out[what] = {"dataset_key": rec.dataset_key, "artifact_sha256": rec.artifact_sha256, "rows": table.num_rows}
    return out


def kg_available(engine: Any) -> bool:
    from sqlalchemy import text

    with engine.connect() as conn:
        return bool(conn.execute(text("SELECT to_regclass('kg.statement') IS NOT NULL")).scalar_one())


def load_kg(run: Any, build: Build, retrieved_at: Any) -> dict[str, Any]:
    from reva.kg.model import load_batch
    from reva.kg.query import country_qid_registry

    ensure_kg_properties(run.engine)
    kg = kg_records(build, qid_for_iso3=country_qid_registry())
    rep = load_batch(
        run.engine,
        run.store,
        entities=kg["entities"],
        statements=kg["statements"],
        source_key=build.src.source_key,
        raw_sha256s=[build.raw_sha256],
        retrieved_at=retrieved_at,
        origin=run.origin,
        parameters={"connector": build.src.name, "parser": build.src.parser_version},
    )
    return rep.as_dict()


#: Codebooks of the T1 claims (definitions quoted verbatim; registry/causal/relations_ecrises.yaml).
CODEBOOKS: dict[str, dict[str, Any]] = {
    ESRB: {
        "name": "ESRB European financial crises database - fields of the crises database (ESRB Occasional Paper "
        "No 13, Annex A)",
        "version": "OP13 (July 2017); data file fcdb20220120",
        "url": "https://www.esrb.europa.eu/pub/pdf/occasional/esrb.op13.en.pdf",
        "definitions": {"accelerator": ESRB_ACCELERATOR_DEFINITION},
    },
}
CLAIM_TERMS = {"esrb_fcdb_accelerator": "accelerator"}


def dataset_claims(build: Build) -> list[Any]:
    """The build's causal relations as ``reva.causal.model.DatasetClaim`` (T1, codebook term per rule)."""
    from reva.causal.model import DatasetClaim

    out = []
    for r in build.relations:
        if r.family != "causal":
            continue
        out.append(
            DatasetClaim(
                cause_id=r.subject,
                effect_id=r.object,
                relation=r.relation,
                selector={**r.asserting_source.get("selector", {}), "objective": r.qualifiers.get("objective")},
                raw_sha256=build.raw_sha256,
                codebook_term=CLAIM_TERMS[r.rule_id],
                source_claim_id=r.rid.split(":", 1)[1].replace("*", ""),
                polarity=r.polarity,
                mechanism=r.mechanism,
                scope={"place": [r.qualifiers.get("provider_geo")]} if r.qualifiers.get("provider_geo") else {},
                tier=r.tier,
            )
        )
    return out


def causal_available(engine: Any) -> bool:
    try:
        from reva.causal.model import load_dataset_claims  # noqa: F401
    except ImportError:
        return False
    from sqlalchemy import text

    with engine.connect() as conn:
        return bool(conn.execute(text("SELECT to_regclass('kg.causal_claim') IS NOT NULL")).scalar_one())


def deliver_claims(run: Any, build: Build, retrieved_at: Any) -> dict[str, Any]:
    """Load the build's T1 claims with CAUSCORE's ``load_dataset_claims`` (complete delivery), then grade.
    Without the causal schema the claims stay in the ``ecrises.<source>.coded_relations`` dataset only."""
    n = sum(1 for r in build.relations if r.family == "causal")
    if n == 0:
        return {"status": "no_coded_causal_relations"}
    if not causal_available(run.engine):
        return {
            "status": "causal_unavailable",
            "claims": n,
            "staged_in": f"ecrises.{build.src.atlas_source}.coded_relations",
        }
    from reva.causal.model import Codebook, load_dataset_claims

    rep = load_dataset_claims(
        run.engine,
        run.store,
        source_key=build.src.source_key,
        dataset=build.src.dataset,
        dataset_revision=build.src.dataset_id,
        codebook=Codebook(**CODEBOOKS[build.src.key]),
        raw_sha256s=[build.raw_sha256],
        retrieved_at=retrieved_at,
        origin=run.origin,
        claims=dataset_claims(build),
        complete=True,
        parameters={"connector": build.src.name, "relations_file": "registry/causal/relations_ecrises.yaml"},
    )
    out: dict[str, Any] = {"status": "loaded", "claims": n, "report": rep.as_dict()}
    try:
        from reva.causal.grader import grade_claims

        g = grade_claims(run.engine, origin=run.origin)
        out["grading"] = g.as_dict() if hasattr(g, "as_dict") else str(g)
    except ImportError:  # pragma: no cover - grader not deployed
        out["grading"] = "grader_unavailable"
    return out


# =========================================================================================== platform protocol
def atlas_source() -> list[Any]:
    from reva.adapters.connectors.base import module_digest
    from reva.atlas.sources import AtlasSourceSpec

    digest = module_digest(parse_lv)
    return [
        AtlasSourceSpec(
            name=s.name,
            version=CONNECTOR_VERSION,
            source_key=s.source_key,
            atlas_source=s.atlas_source,
            provider=s.provider,
            licence=RIGHTS[s.key]["licence"][:500],
            allowed_hosts=s.hosts,
            datasets=(s.dataset,),
            dataset_ids=(s.dataset_id,),
            cadence_seconds=s.cadence_days * 86400,
            planned_requests=s.planned_requests,
            max_response_bytes=MAX_XLSX_BYTES,
            min_interval_seconds=2.0,
            parser_version=s.parser_version,
            code_digest=digest,
            field_mappings={"file": s.url.rsplit("/", 1)[-1].split("?")[0]},
            notes=f"crisis dataset; events + coded relations; {RIGHTS[s.key]['licence'][:200]}",
        )
        for s in SOURCES.values()
    ]


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """Fetch the source's file (JST: licence page first), parse it fully, archive, register the atlas table,
    and -- when the database has the KG -- load the events and register the events / coded-relations datasets.
    Run option ``kg=false`` skips the KG and side datasets."""
    src = BY_NAME[run.spec.name]
    out: dict[str, Any] = {"source": src.name}
    if src.key == JST:
        lic = await run.get(JST_LICENCE_URL, key="jst:licence-terms", media_types=("text/html",), max_bytes=3_000_000)
        check_jst_licence(lic.body)
        out["licence_page_sha256"] = run.archive(lic)
    raw = await run.get(
        src.url, key=f"{src.atlas_source}:{src.dataset_id}", media_types=XLSX_MEDIA, max_bytes=MAX_XLSX_BYTES
    )
    build = PARSERS[src.key](raw.body, sha256_hex(raw.body))  # parse fully before anything is stored
    sha = run.archive(raw)
    if sha != build.raw_sha256:
        raise _err("archived raw sha256 differs from the parsed bytes", "integrity")
    reg = run.register(
        dataset=src.dataset,
        frame=build.frame(),
        raw_sha256s=[sha],
        coverage_note=build.coverage_note,
        geo_scheme=build.geo_scheme,
        rights={**RIGHTS[src.key], "provider": src.provider, "source_key": src.source_key},
    )
    out["atlas"] = {k: reg[k] for k in ("dataset_key", "artifact_sha256", "row_count", "created")}
    out["unmapped_provider_geo"] = reg["unmapped_provider_geo"][:100]
    out["events"] = len(build.events)
    out["relations"] = dict(Counter(f"{r.family}:{r.relation}" for r in build.relations))
    out["report"] = build.report
    if str(run.options.get("kg", "true")).lower() != "false" and kg_available(run.engine):
        out["kg"] = load_kg(run, build, raw.retrieved_at)
        out["datasets"] = register_side_datasets(run, build)
        out["causal"] = deliver_claims(run, build, raw.retrieved_at)
    return out


def describe_build(build: Build) -> dict[str, Any]:
    """Small JSON-able summary of a build (docs / tests)."""
    return {
        "rows": len(build.rows),
        "events": len(build.events),
        "relations": len(build.relations),
        "report": build.report,
        "sample_event": asdict(build.events[0]) if build.events else None,
    }
