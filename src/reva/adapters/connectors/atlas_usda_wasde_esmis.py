"""USDA WASDE reports as published, 1998 -> now, from the USDA ESMIS archive: headline crop cells (V7, BEAT-AGRI).

Source ``atlas.usda_wasde_esmis`` -> table ``usda_wasde_esmis.headline``. The Economics, Statistics and Market
Information System (ESMIS, National Agricultural Library / Cornell archive) lists every World Agricultural Supply
and Demand Estimates report with its release date and the files published for it
(``https://esmis.nal.usda.gov/publication/world-agricultural-supply-and-demand-estimates?page=<n>``). This connector
complements ``atlas.usda_wasde`` (OCE CSV, 2021 -> now; the 2010-2020 zips answer 403) with the history needed to
evaluate WASDE's projections at every release date.

* **Which reports.** Every release listed with a release date in ``[first, last]`` (defaults 1998-01-01 -> today;
  the listing is read newest first until a page reaches ``first``; option ``pages`` = explicit page numbers).
  Per release one report file: the XML (``wasde-MM-DD-YYYY.xml`` / ``wasdeMMYY[vN].xml``; 2018-10..2021-07 the
  archive names it ``latest.xml`` or ``WASDE-<n>-<Month>-<DD>-<YYYY>.xml``) for releases from
  :data:`XML_FROM` (2010-07, the first XML), else the plain-text report (``.txt``). Announcements, ``.doc``,
  revision workbooks and ``ORIG``/``R1`` copies are never read. A release without such a file (or whose file does
  not hold the tables) is recorded with its reason, never filled.
* **Which cells (small, aggregated).** Only the headline lines needed by the V7 crop track, each as WASDE printed
  it in that report: United States corn, soybeans, wheat and rice (total) — yield per harvested acre, production,
  ending stocks (``U.S. ... Supply and Use`` tables) — and World wheat, corn, soybeans, rice (milled) —
  production, ending stocks (``World ... Supply and Use`` tables, row "World"). For the projection marketing year
  only the CURRENT report month's column is read (the previous month's column of the same table is the previous
  report, read from that report).
* **Identity.** ``indicator`` = ``wasde_esmis:<series>|v=<release date>`` (series e.g. ``us.corn.production``,
  ``world.rice.ending_stocks``); ``provider_geo`` = ``United States`` / ``World`` (WASDE's own row/table labels;
  never mapped to a country code). Period = the marketing year as its first calendar year (``2015/16`` ->
  2015, the convention of ``atlas.usda_wasde`` and ``atlas.usda_psd``). Every label (table title, row label,
  marketing-year label, month label) is kept verbatim in ``flags``.
* **Projection vs history.** ``Proj.``/``Projected``/``Projections`` -> ``estimate_kind = projection``; ``Est.``/
  ``Estimated`` -> ``estimate``; unlabelled earlier years -> ``published_history``. Each is the value AS PRINTED in
  that report (later reports revise it; the report date is part of the cell's identity).
* **Availability.** ``available_at`` = the day after the release date that ESMIS states for the release (00:00 UTC;
  WASDE is released at noon Eastern time). The archived files of old releases carry the ESMIS migration date as
  ``Last-Modified`` (e.g. 2018), which proves nothing about the publication day; the WASDE text/PDF was public on
  the release day, and the XML re-issues the same numbers (verified on the overlap months in the tests).

Licence: U.S. Government work (USDA World Agricultural Outlook Board), public domain in the United States
(17 U.S.C. 105); attribution kept.
"""

from __future__ import annotations

import html
import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from reva.adapters.connectors.agri_common import canon, module_digest
from reva.adapters.connectors.vintage_common import Rows, year_bounds
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

NAME = "atlas.usda_wasde_esmis"
VERSION = "1.0.2"
PARSER_VERSION = "atlas_usda_wasde_esmis/1"
SOURCE_KEY = "usda.wasde_esmis"
ATLAS_SOURCE = "usda_wasde_esmis"
DS = "headline"
HOST = "esmis.nal.usda.gov"
BASE = f"https://{HOST}"
LISTING_URL = f"{BASE}/publication/world-agricultural-supply-and-demand-estimates?page={{page}}"
LICENCE = "U.S. Government work (USDA World Agricultural Outlook Board, WASDE): public domain in the United States"
ATTRIBUTION = "USDA, World Agricultural Supply and Demand Estimates (WASDE), World Agricultural Outlook Board"
FIRST_RELEASE = date(1998, 1, 1)
XML_FROM = date(2010, 7, 1)
MAX_PAGES = 90
MAX_FILE_BYTES = 8_000_000
HTML_MEDIA = ("text/html",)
XML_MEDIA = ("application/xml", "text/xml", "application/octet-stream")
TXT_MEDIA = ("text/plain", "application/octet-stream")
AVAILABILITY_RULE = "esmis_release_date_plus_one_day"

CROPS = ("corn", "soybeans", "wheat", "rice")
US_ATTRS = ("yield", "production", "ending_stocks")
WORLD_ATTRS = ("production", "ending_stocks")
SERIES: tuple[str, ...] = tuple(f"us.{c}.{a}" for c in CROPS for a in US_ATTRS) + tuple(
    f"world.{c}.{a}" for c in CROPS for a in WORLD_ATTRS
)
UNITS: dict[str, str] = {
    "us.corn.yield": "bushels per harvested acre",
    "us.corn.production": "million bushels",
    "us.corn.ending_stocks": "million bushels",
    "us.soybeans.yield": "bushels per harvested acre",
    "us.soybeans.production": "million bushels",
    "us.soybeans.ending_stocks": "million bushels",
    "us.wheat.yield": "bushels per harvested acre",
    "us.wheat.production": "million bushels",
    "us.wheat.ending_stocks": "million bushels",
    "us.rice.yield": "pounds per harvested acre",
    "us.rice.production": "million hundredweight (rough equivalent)",
    "us.rice.ending_stocks": "million hundredweight (rough equivalent)",
    **{f"world.{c}.{a}": "million metric tons" for c in CROPS for a in WORLD_ATTRS},
}
UNITS["world.rice.production"] = "million metric tons (milled basis)"
UNITS["world.rice.ending_stocks"] = "million metric tons (milled basis)"

#: U.S. tables: (crop, title pattern, XML matrix ordinal in document order, text section start pattern)
US_TABLES: tuple[tuple[str, str, int, str | None], ...] = (
    ("wheat", r"^U\.\s?S\. Wheat Supply and Use", 0, None),
    ("corn", r"^U\.\s?S\. Feed Grains? and Corn Supply and Use", 1, r"^CORN\b"),
    ("soybeans", r"^U\.\s?S\. Soybeans and Products Supply and Use", 0, r"^SOYBEANS\b"),
    ("rice", r"^U\.\s?S\. Rice Supply and Use", 0, r"^TOTAL\b"),
)
WORLD_TABLES: tuple[tuple[str, str], ...] = (
    ("wheat", r"^World Wheat Supply and Use"),
    ("corn", r"^World Corn Supply and Use"),
    ("soybeans", r"^World Soybean Supply and Use"),
    ("rice", r"^World Rice Supply and Use \(Milled Basis\)"),
)
#: normalised row label -> attribute (U.S. tables)
US_LABELS = {
    "yield per harvested acre": "yield",
    "production": "production",
    "ending stocks": "ending_stocks",
    "ending stocks, total": "ending_stocks",
}
MONTHS = {
    m: i + 1
    for i, ms in enumerate(
        (
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        )
    )
    for m in ms
}
_FOOT = re.compile(r"_?\b\d{1,2}/")
_NUM = re.compile(r"^-?\d[\d,]*(\.\d+)?$|^-?\.\d+$")
_MY = re.compile(r"(\d{4})/(\d{2})")
_REPORT_FILE = re.compile(
    r"^(wasde(-\d{2}-\d{2}-\d{4}|\d{4}(v\d)?)|latest|wasde-\d+-[a-z]+-\d{1,2}-\d{4})\.(xml|txt)$", re.IGNORECASE
)
_ROW = re.compile(r"<tr\b.*?</tr>", re.DOTALL)
_TIME = re.compile(r'<time datetime="(\d{4}-\d{2}-\d{2})T')
_HREF = re.compile(r'href="(/sites/default/release-files/[^"]+)"')
_VIEW = re.compile(r'href="(/publication/world-agricultural-supply-and-demand-estimates/[0-9-]+)"')


class EsmisError(SourceResponseError):
    def __init__(self, msg: str, kind: str = "schema_drift") -> None:
        super().__init__(msg, retryable=False, kind=kind)


@dataclass(frozen=True)
class Release:
    release: date
    files: tuple[str, ...]
    view: str | None = None

    def report_file(self) -> str | None:
        """The report file to parse (XML from :data:`XML_FROM`, else text; see the module doc)."""
        cands = [f for f in self.files if _REPORT_FILE.match(f.rsplit("/", 1)[-1])]
        xml = sorted(f for f in cands if f.lower().endswith(".xml"))
        txt = sorted(f for f in cands if f.lower().endswith(".txt"))
        if self.release >= XML_FROM and xml:
            return xml[0]
        if txt:
            return txt[0]
        return xml[0] if xml else None


@dataclass(frozen=True)
class Cell:
    series: str
    my: int
    value: str
    status: str  # projection | estimate | published_history
    labels: Mapping[str, str]


@dataclass
class ReportParse:
    report_month: int | None = None
    report_year: int | None = None
    cells: list[Cell] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- listing


def parse_listing(body: bytes) -> list[Release]:
    """Releases on one ESMIS listing page (release date as ESMIS states it, file links of that row)."""
    text = body.decode("utf-8", errors="replace")
    out: list[Release] = []
    for row in _ROW.findall(text):
        t = _TIME.search(row)
        files = tuple(dict.fromkeys(html.unescape(h) for h in _HREF.findall(row)))
        if not t or not files:
            continue
        v = _VIEW.search(row)
        out.append(Release(date.fromisoformat(t.group(1)), files, v.group(1) if v else None))
    return out


# --------------------------------------------------------------------------- helpers


def norm_label(s: str) -> str:
    s = s.replace("\r", " ").replace("\n", " ")
    s = _FOOT.sub(" ", s)
    s = s.replace("*", " ")
    return re.sub(r"\s+", " ", s).strip().rstrip(":").strip().lower()


def number(tok: str) -> str | None:
    """A WASDE printed number as a canonical decimal string (commas removed), or ``None``."""
    t = tok.strip().replace("*", "").strip()
    if not t or not _NUM.match(t):
        return None
    t = t.replace(",", "")
    neg = t.startswith("-")
    t = t.lstrip("-")
    if t.startswith("."):
        t = "0" + t
    if "." in t:
        t = t.rstrip("0").rstrip(".")
    t = t.lstrip("0") or "0"
    if t.startswith("."):
        t = "0" + t
    return ("-" + t) if neg and t != "0" else t


def my_of(label: str) -> tuple[int, str]:
    """(first year, status) of a marketing-year label such as ``2014/15 Est.`` or ``2015/16 (Projected)``."""
    m = _MY.search(label)
    if not m:
        raise EsmisError(f"no marketing year in {label!r}")
    y = int(m.group(1))
    if int(m.group(2)) != (y + 1) % 100:
        raise EsmisError(f"unexpected marketing year {label!r}")
    low = label.lower()
    if "proj" in low:
        return y, "projection"
    if "est" in low:
        return y, "estimate"
    return y, "published_history"


def month_of(label: str) -> int | None:
    return MONTHS.get(label.strip().rstrip(".").lower())


def _numbers(rest: str) -> list[str]:
    rest = _FOOT.sub(" ", rest).replace("*", " ")
    out = []
    for tok in rest.split():
        v = number(tok)
        if v is None:
            raise EsmisError(f"unexpected token {tok!r} in {rest.strip()[:60]!r}")
        out.append(v)
    return out


# --------------------------------------------------------------------------- XML (2010-07 -> now)


def _xml_cells(e: ET.Element, ctx: dict[str, str]) -> Iterator[tuple[dict[str, str], str]]:
    a = e.attrib
    ctx2 = dict(ctx)
    for k, v in a.items():
        if not k.startswith("cell_value") and not k.startswith("Textbox"):
            ctx2[re.sub(r"\d+$", "", k)] = v
    for k, v in a.items():
        if k.startswith("cell_value"):
            yield ctx2, v
    for c in e:
        yield from _xml_cells(c, ctx2)


def parse_xml(body: bytes) -> ReportParse:
    """Headline cells of one WASDE XML report."""
    head = body[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in body.upper():
        raise EsmisError("XML with a DTD / entity declaration refused")
    try:
        root = ET.fromstring(body)  # noqa: S314 - DTD/entity declarations refused above; expat fetches nothing
    except ET.ParseError as e:
        raise EsmisError(f"XML does not parse: {e}") from e
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]  # some 2010 reports declare a default namespace (xmlns="wasde")
    rep = ReportParse()
    found: dict[tuple[str, int], Cell] = {}
    for sr in root:
        r = sr.find("Report")
        if r is None:
            continue
        title = re.sub(r"\s+", " ", r.attrib.get("sub_report_title") or "").strip()
        rm = r.attrib.get("Report_Month") or ""
        mm = re.match(r"([A-Za-z]+)\s+(\d{4})$", rm.strip())
        if mm and month_of(mm.group(1)):
            rep.report_month, rep.report_year = month_of(mm.group(1)), int(mm.group(2))
        for crop, pat, ordinal, _sec in US_TABLES:
            if not re.match(pat, title):
                continue
            mats = [c for c in r if c.tag.startswith("matrix")]
            if len(mats) <= ordinal:
                raise EsmisError(f"{title!r}: {len(mats)} matrices, ordinal {ordinal} expected")
            for ctx, v in _xml_cells(mats[ordinal], {}):
                attr = US_LABELS.get(norm_label(ctx.get("attribute", "")))
                if attr is None:
                    continue
                _xml_add(found, f"us.{crop}.{attr}", ctx, ctx.get("market_year", ""), v, title, rep)
        for crop, pat in WORLD_TABLES:
            if not re.match(pat, title):
                continue
            for ctx, v in _xml_cells(r, {}):
                if norm_label(ctx.get("region", "")) != "world":
                    continue
                attr = {"production": "production", "ending stocks": "ending_stocks"}.get(
                    norm_label(ctx.get("attribute", ""))
                )
                if attr is None:
                    continue
                my_label = ctx.get("region_header") or ctx.get("market_year") or ""
                _xml_add(found, f"world.{crop}.{attr}", ctx, my_label, v, title, rep)
    if rep.report_month is None:
        raise EsmisError("XML: no Report_Month")
    rep.cells = positional_status(found)
    return rep


def positional_status(found: Mapping[tuple[str, int], Cell]) -> list[Cell]:
    """Status by position (WASDE prints, per series, the projection year last and the estimate year before it):
    the latest marketing year of a series in a report = ``projection``, the one before = ``estimate``, earlier =
    ``published_history``. The printed label (``Proj.``/``Est.``; absent in some 2010-2011 XML world tables) is
    kept in ``labels.status_label``; a printed label contradicting the position is a layout surprise."""
    by: dict[str, list[int]] = {}
    for s, y in found:
        by.setdefault(s, []).append(y)
    out = []
    for (s, y), c in sorted(found.items()):
        ys = sorted(by[s])
        pos = "projection" if y == ys[-1] else "estimate" if len(ys) > 1 and y == ys[-2] else "published_history"
        if c.status != "published_history" and c.status != pos:
            raise EsmisError(f"{s} {y}: printed status {c.status} but position says {pos}")
        out.append(Cell(c.series, c.my, c.value, pos, {**c.labels, "status_label": c.status}))
    return out


def _xml_add(
    found: dict[tuple[str, int], Cell],
    series: str,
    ctx: Mapping[str, str],
    my_label: str,
    raw: str,
    title: str,
    rep: ReportParse,
) -> None:
    y, status = my_of(my_label)
    fm = (ctx.get("forecast_month") or "").strip()
    if fm:
        m = month_of(fm)
        if m is None:
            raise EsmisError(f"{title!r}: unknown forecast month {fm!r}")
        if m != rep.report_month:
            return  # the previous report's column
    v = number(raw)
    if v is None:
        if raw.strip().upper() in ("NA", ""):
            rep.missing.append(f"{series}|{y}")
            return
        raise EsmisError(f"{title!r}: value {raw!r} is not a number")
    key = (series, y)
    if key in found and found[key].value != v:
        raise EsmisError(f"{title!r}: two values for {series} {y}: {found[key].value} / {v}")
    found[key] = Cell(
        series,
        y,
        v,
        status,
        {
            "table": title,
            "row": re.sub(r"\s+", " ", ctx.get("attribute", "")).strip(),
            "market_year": my_label.strip(),
            "month": fm,
            "region": re.sub(r"\s+", " ", ctx.get("region", "")).strip(),
        },
    )


# --------------------------------------------------------------------------- text (1998 -> 2010-06)


_TITLE = re.compile(r"^\s*((?:U\.\s?S\.|World\b).*Supply and Use.*)$")


def _segments(lines: list[str]) -> list[tuple[str, list[str]]]:
    """(title, lines) of each table; a "(Cont'd.)" page is appended to its table."""
    idx = [(i, _TITLE.match(ln).group(1).strip()) for i, ln in enumerate(lines) if _TITLE.match(ln) and ":" not in ln]  # type: ignore[union-attr]
    out: list[tuple[str, list[str]]] = []
    for n, (i, t) in enumerate(idx):
        end = idx[n + 1][0] if n + 1 < len(idx) else len(lines)
        base = re.sub(r"\s*\(Cont'?d\.?\)\s*", " ", t).strip()
        base = re.sub(r"\s+", " ", base)
        if out and "cont" in t.lower() and out[-1][0] == base:
            out[-1][1].extend(lines[i + 1 : end])
        else:
            out.append((base, lines[i + 1 : end]))
    return out


def parse_txt(body: bytes) -> ReportParse:
    """Headline cells of one WASDE plain-text report (the 1998-2010 layout with ``:`` column separators)."""
    text = body.decode("cp1252", errors="replace").replace("\r\n", "\n").replace("\x0c", "\n")
    lines = text.split("\n")
    rep = ReportParse()
    found: dict[tuple[str, int], Cell] = {}
    for title, seg in _segments(lines):
        for crop, pat, _ordinal, sec in US_TABLES:
            if re.match(pat, title):
                _txt_us(found, rep, crop, title, seg, sec)
        for crop, pat in WORLD_TABLES:
            if re.match(pat, title):
                _txt_world(found, rep, crop, title, seg)
    rep.cells = positional_status(found)
    return rep


def _txt_us(
    found: dict[tuple[str, int], Cell], rep: ReportParse, crop: str, title: str, seg: list[str], sec: str | None
) -> None:
    proj_my = hist = months = None
    for i, ln in enumerate(seg[:12]):
        m = re.search(r"(\d{4}/\d{2})\s+Projections", ln)
        if m:
            proj_my = m.group(1)
        if re.search(r"^\s*Item\b", ln):
            hist = re.findall(r"\d{4}/\d{2}", ln)
            months = [w for w in re.split(r"[\s:]+", seg[i + 1]) if month_of(w)]
    if proj_my is None or not hist or len(hist) != 2 or not months:
        raise EsmisError(f"{title!r}: header not understood (projection {proj_my}, years {hist}, months {months})")
    cur = month_of(months[-1])
    if rep.report_month is None:
        rep.report_month = cur
    elif cur != rep.report_month:
        raise EsmisError(f"{title!r}: report month {months[-1]} differs from {rep.report_month}")
    start = 0
    if sec:
        start = next((i for i, ln in enumerate(seg) if re.match(sec, ln.strip())), -1)
        if start < 0:
            raise EsmisError(f"{title!r}: section {sec} not found")
    prev = ""
    got: set[str] = set()
    for ln in seg[start:]:
        if ":" not in ln or ln.strip().startswith("="):
            continue
        lab, rest = ln.split(":", 1)
        nl = norm_label(lab)
        if nl == "acre" and prev == "yield per harvested":
            nl = "yield per harvested acre"
        if nl:
            prev = nl
        attr = US_LABELS.get(nl)
        if attr is None or attr in got:
            continue
        nums = _numbers(rest)
        if not nums:
            continue
        if len(nums) != 2 + len(months):
            raise EsmisError(f"{title!r}: row {lab.strip()!r} has {len(nums)} values for {2 + len(months)} columns")
        got.add(attr)
        series = f"us.{crop}.{attr}"
        cols = [(hist[0], nums[0], ""), (hist[1] + " Est.", nums[1], ""), (proj_my + " Proj.", nums[-1], months[-1])]
        for my_label, v, mon in cols:
            y, status = my_of(my_label)
            _txt_put(
                found, series, y, v, status, {"table": title, "row": lab.strip(), "market_year": my_label, "month": mon}
            )
    missing = {a for a in US_ATTRS} - got
    if missing:
        raise EsmisError(f"{title!r}: rows not found {sorted(missing)}")


def _txt_world(found: dict[tuple[str, int], Cell], rep: ReportParse, crop: str, title: str, seg: list[str]) -> None:
    block: tuple[int, str, str] | None = None
    i = 0
    seen = 0
    while i < len(seg):
        ln = seg[i]
        if ":" in ln:
            lab, rest = ln.split(":", 1)
            r = rest.strip()
            if not lab.strip() and _MY.search(r) and not re.search(r"\d\s+\d", _MY.sub("", r)):
                y, status = my_of(r)
                block = (y, status, r)
            elif norm_label(lab).split()[:1] == ["world"] and block is not None and len(norm_label(lab).split()) <= 2:
                parts = norm_label(lab).split()
                inline_month = month_of(parts[1]) if len(parts) == 2 else None
                if len(parts) == 2 and inline_month is None:
                    i += 1
                    continue  # e.g. "World less China"
                if inline_month is not None and inline_month != rep.report_month:
                    i += 1
                    continue  # previous report's month on the same label
                nums = _numbers(rest)
                label_row = lab.strip()
                mon = parts[1] if inline_month is not None else ""
                if not nums:  # projection block: month sub-rows follow
                    j = i + 1
                    rows: dict[int, tuple[str, list[str]]] = {}
                    while j < len(seg) and ":" in seg[j]:
                        sl, sr_ = seg[j].split(":", 1)
                        mo = month_of(sl.strip())
                        if mo is None:
                            break
                        rows[mo] = (sl.strip(), _numbers(sr_))
                        j += 1
                    if rep.report_month not in rows:
                        raise EsmisError(f"{title!r}: no {rep.report_month} row under World ({sorted(rows)})")
                    mon, nums = rows[rep.report_month]
                if len(nums) < 6:
                    raise EsmisError(f"{title!r}: World row with {len(nums)} values")
                y, status, my_label = block
                labels = {"table": title, "row": label_row, "market_year": my_label, "month": mon}
                _txt_put(found, f"world.{crop}.production", y, nums[1], status, labels)
                _txt_put(found, f"world.{crop}.ending_stocks", y, nums[-1], status, labels)
                seen += 1
        i += 1
    if not seen:
        raise EsmisError(f"{title!r}: no World row")


def _txt_put(
    found: dict[tuple[str, int], Cell], series: str, y: int, v: str, status: str, labels: Mapping[str, str]
) -> None:
    key = (series, y)
    if key in found and found[key].value != v:
        raise EsmisError(f"two values for {series} {y}: {found[key].value} / {v}")
    found[key] = Cell(series, y, v, status, dict(labels))


# --------------------------------------------------------------------------- frame


def availability(release: date) -> datetime:
    return datetime(release.year, release.month, release.day, tzinfo=UTC) + timedelta(days=1)


def add_cells(rows: Rows, rel: Release, file: str, fmt: str, rep: ReportParse, raw_sha256: str) -> int:
    avail = availability(rel.release)
    n = 0
    for c in rep.cells:
        s, e = year_bounds(c.my)
        flags = {
            "series": c.series,
            "estimate_kind": c.status,
            "vintage": rel.release.isoformat(),
            "release_date": rel.release.isoformat(),
            "report_month": rep.report_month,
            "available_at": avail.isoformat().replace("+00:00", "Z"),
            "availability_rule": AVAILABILITY_RULE,
            "marketing_year_first_year": c.my,
            "period_basis": "marketing year as its first calendar year (local marketing-year months not stated)",
            "format": fmt,
            "file": file,
            "labels_verbatim": dict(c.labels),
            "geography": "WASDE table/row label (not mapped to a country code)",
            "licence": "public domain (U.S. Government work)",
        }
        rows.add(
            source=ATLAS_SOURCE,
            dataset=DS,
            indicator=f"wasde_esmis:{c.series}|v={rel.release.isoformat()}",
            provider_geo="World" if c.series.startswith("world.") else "United States",
            start=s,
            end=e,
            value=c.value,
            unit=UNITS[c.series],
            flags=canon(flags),
            raw_sha256=raw_sha256,
            selector={"file": file, "series": c.series, "market_year": c.my},
        )
        n += 1
    return n


def rights() -> dict[str, Any]:
    return {
        "licence": "Public domain (U.S. Government work, 17 U.S.C. 105)",
        "licence_url": "https://www.usda.gov/policies-and-links",
        "attribution": ATTRIBUTION,
        "provider": "USDA World Agricultural Outlook Board (via ESMIS, National Agricultural Library)",
        "licence_checked": "2026-10-17",
        "source_key": SOURCE_KEY,
    }


def code_digest() -> str:
    return module_digest(sys.modules[__name__])


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="USDA, World Agricultural Supply and Demand Estimates (ESMIS archive of each report as published)",
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=(DS,),
        dataset_ids=("world-agricultural-supply-and-demand-estimates",),
        cadence_seconds=30 * 86_400,
        planned_requests=420,
        max_response_bytes=MAX_FILE_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"series": "wasde_esmis:<series>|v=<release date>", "geo": "United States / World"},
        notes="ESMIS listing pages + one report file per release (TXT to 2010-06, XML from 2010-07); headline cells.",
    )


def _date_opt(v: Any, what: str, default: date) -> date:
    if not v:
        return default
    s = str(v)
    try:
        return date.fromisoformat(s if len(s) == 10 else s + "-01")
    except ValueError as e:
        raise EsmisError(f"run option {what} must be YYYY-MM[-DD], got {v!r}", kind="bad_option") from e


async def atlas_ingest(run: Any) -> dict[str, Any]:
    if not run.wants(DS):
        return {"tables": {}}
    first = _date_opt(run.options.get("first"), "first", FIRST_RELEASE)
    last = _date_opt(run.options.get("last"), "last", datetime.now(UTC).date())
    max_pages = int(run.options.get("max_pages") or MAX_PAGES)
    explicit = [int(x) for x in str(run.options.get("pages") or "").split(",") if x.strip()]
    shas: list[str] = []
    releases: dict[date, Release] = {}
    pages = 0
    for page in explicit or range(max_pages):
        try:
            raw = await run.get(LISTING_URL.format(page=page), key=f"esmis:wasde:page={page}", media_types=HTML_MEDIA)
        except SourceResponseError as e:
            if run.origin == "test_fixture" and (e.details or {}).get("kind") == "fixture_missing":
                break
            raise
        shas.append(run.archive(raw))
        pages += 1
        got = parse_listing(raw.body)
        if not got:
            break
        for r in got:
            prev = releases.get(r.release)
            releases[r.release] = (
                r if prev is None else Release(r.release, tuple(dict.fromkeys(prev.files + r.files)), prev.view)
            )
        if not explicit and min(r.release for r in got) < first:
            break
    rows = Rows()
    per: list[dict[str, Any]] = []
    for d in sorted(releases):
        if not (first <= d <= last):
            continue
        rel = releases[d]
        f = rel.report_file()
        rec: dict[str, Any] = {"release": d.isoformat(), "file": f, "files_listed": len(rel.files)}
        if f is None:
            per.append(rec | {"status": "no_report_file", "listed": [x.rsplit("/", 1)[-1] for x in rel.files]})
            continue
        fmt = "xml" if f.lower().endswith(".xml") else "txt"
        try:
            raw = await run.get(
                BASE + f, key=f"esmis:wasde:{d.isoformat()}", media_types=XML_MEDIA if fmt == "xml" else TXT_MEDIA,
                max_bytes=MAX_FILE_BYTES, params={"release": d.isoformat()},
            )  # fmt: skip
        except SourceResponseError as e:
            if run.origin == "test_fixture" and (e.details or {}).get("kind") == "fixture_missing":
                per.append(rec | {"status": "not_captured"})
                continue
            raise
        sha = run.archive(raw)
        shas.append(sha)
        try:
            rep = parse_xml(raw.body) if fmt == "xml" else parse_txt(raw.body)
        except EsmisError as e:
            per.append(rec | {"status": "not_parsed", "reason": str(e)[:300], "sha256": sha})
            continue
        n = add_cells(rows, rel, f.rsplit("/", 1)[-1], fmt, rep, sha)
        series = {c.series for c in rep.cells}
        per.append(
            rec
            | {
                "status": "parsed" if series == set(SERIES) else "parsed_partial",
                "format": fmt,
                "cells": n,
                "report_month": rep.report_month,
                "series_missing": sorted(set(SERIES) - series),
                "na_cells": rep.missing,
                "sha256": sha,
            }
        )
        del raw
    frame = rows.frame()
    if frame.height == 0:
        raise EsmisError(f"no WASDE cell parsed for {first}..{last}", kind="no_data")
    status: dict[str, int] = {}
    for p in per:
        status[p["status"]] = status.get(p["status"], 0) + 1
    note = (
        f"USDA WASDE reports as published (ESMIS archive, {LISTING_URL.format(page='N')}): {pages} listing pages, "
        f"{len(per)} releases {first}..{last} ({status}). Headline cells only: U.S. corn/soybeans/wheat/rice yield, "
        "production, ending stocks; World wheat/corn/soybeans/rice production, ending stocks; for the projection "
        "year the current month's column. Indicator wasde_esmis:<series>|v=<release date>; period = marketing year "
        "(first year); available_at = release date + 1 day (00:00 UTC). TXT reports to 2010-06, XML from 2010-07. "
        f"Public domain; cite {ATTRIBUTION}."
    )[:4000]
    reg = run.register(dataset=DS, frame=frame, raw_sha256s=sorted(set(shas)), coverage_note=note, rights=rights())
    return {
        "tables": {DS: {"dataset_key": reg["dataset_key"], "rows": reg["row_count"], "created": reg["created"]}},
        "pages": pages,
        "releases": per,
        "status_counts": status,
    }


__all__ = [
    "NAME",
    "SERIES",
    "EsmisError",
    "Release",
    "atlas_ingest",
    "atlas_source",
    "availability",
    "parse_listing",
    "parse_txt",
    "parse_xml",
]
