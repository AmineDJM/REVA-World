"""COW Territorial Change v6 (1816-2018) and the COW political-entities list -> atlas tables + KG (agent A-COLONIAL).

Source
------
Correlates of War Project, *Territorial Change, 1816-2018 (v6)*, hosted by Steven V. Miller (Clemson University),
``https://correlatesofwar.org/wp-content/uploads/terr-changes-v6.zip`` (one request): ``tc2018.csv`` (the data),
``tcmanual.pdf`` (coding manual, sha256 pinned in ``registry/causal/relations_colonial.yaml``; the value labels below
are quoted from it) and ``Entities.pdf`` ("The list of political entities (colonies, dependencies, and
possessions)": per COW entity number the spells Begin Year - End Year with their political status, e.g. ``540 Angola
1816 1975 Became colony of 235``). Citation: Tir, Schafer, Diehl & Goertz (1998), "Territorial Changes, 1816-1996:
Procedures and Data", *Conflict Management and Peace Science* 16:89-97. **Terms** (COW FAQ): cite; users cannot charge
for access -- not an open licence (``registry/sources/cow_terrchange.yaml``). The countrycode 1.9.0 tarball (CRAN) is
fetched and archived too (COW-code crosswalk provenance, as in ``atlas_cow``).

``Entities.pdf`` is a printed spreadsheet; it is read with a small bounded PDF text reader (:func:`pdf_text_rows`:
FlateDecode streams, object streams, page tree, one WinAnsi TrueType font; text positions from ``Tm``/``Td``/``TD``/
``T*`` and the font's glyph widths; cells assigned to the five printed columns by x position; a name or status
printed on two lines is joined). Every parsed row keeps ``{page, row}``; a row the reader cannot assign completely is
kept with what was read and counted, never guessed.

Outputs
-------
* atlas ``cowtc.changes_cy`` (**computed**: distinct territorial changes per state-year, as gainer / loser, by
  procedure, independence, military conflict; flags list the change numbers);
* atlas ``cowtc.entity_status`` (the entity spells as listed: indicator ``status|relation=<r>|counterpart=<code>``,
  value_text = counterpart, period = [Begin Year, End Year + 1));
* KG (source key ``cow.terrchange``): ``cowtc:<number>`` events (``P585``; gainer, loser, territory, procedure,
  independence, military conflict, area, population, ...), ``cowent:<code>`` territories with their status spells
  (``cowent:colony_of`` / ``protectorate_of`` / ``mandated_to`` / ``possession_of`` / ``leased_to`` / ``occupied_by``
  / ``claimed_by`` / ``part_of`` / ``neutral_zone_of``), ``geopol:corresponds_to_iso3`` for entity codes the
  crosswalk (or an exact-name alias with evidence) maps, and ``colonial:independence_key`` on independences (the
  value ICOW's independence events carry too: EVENTS' same_as rule can link them).

No causal claim: ``procedur = 1`` (conquest) and ``conflict = 1`` do not identify a war or dispute
(``registry/causal/relations_colonial.yaml``).
"""

from __future__ import annotations

import re
import zlib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from reva.adapters.connectors.atlas_colonial import (
    CodedTime,
    Kg,
    StateEntityResolver,
    check_document,
    code_entities_and_links,
    drift,
    independence_key,
    int_code,
    load_colonial_kg,
    member,
    read_delimited,
    zip_members,
)
from reva.adapters.connectors.atlas_geopol_common import AtlasRows, KgEntity, KgStatement, StateCodeCrosswalk

CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas_terrchange/1"
SOURCE_KEY = "cow.terrchange"
TC_URL = "https://correlatesofwar.org/wp-content/uploads/terr-changes-v6.zip"
TC_REVISION = "cow-territorial-change-v6"
TC_MEMBER = "tc2018.csv"
ENTITIES_MEMBER = "Entities.pdf"
MANUAL_MEMBER = "tcmanual.pdf"
LICENCE = "COW terms of use (not an open licence): data must be cited; users cannot charge for access"
CITATION = (
    "Tir, Jaroslav, Philip Schafer, Paul Diehl, and Gary Goertz. 1998. 'Territorial Changes, 1816-1996: Procedures "
    "and Data', Conflict Management and Peace Science 16:89-97. Territorial Change data v6 (1816-2018)."
)

_TC_COLS = (
    *("year", "month", "gainer", "gaintype", "procedur", "entity", "contgain", "area", "pop", "portion", "loser"),
    *("losetype", "contlose", "entry", "exit", "number", "indep", "conflict", "version"),
)

#: Coding manual (tcmanual.pdf), "Process of Territorial Change": code -> (slug, label, verbatim definition).
PROCEDURES: dict[int, tuple[str, str, str]] = {
    1: ("conquest", "Conquest", "Conquest occurs when armed force is the primary agent of the territorial change."),
    2: ("annexation", "Annexation", "Annexation occurs when one political entity unilaterally extends its sovereignty over another political entity when the primary agent of change is diplomacy, although the implied threat of force may be involved."),
    3: ("cession", "Cession", "Cession occurs when part of a political entity is yielded to another entity through plebiscite, purchase, compensating agreement, or as a consequence of hostilities. If the cession was total, then the change is coded as a \"conquest.\""),
    4: ("secession", "Secession", "Secession refers to the dissolution of an existing end as a result of one or more parts of the entity leaving it in order to establish themselves as new independent entities."),
    5: ("unification", "Unification", "Unification refers to the formation of a new political entity out of two or more pre-existing entities."),
    6: ("mandated_territory", "Mandated territory", "A Mandated Territory is a territorial unit that is placed under the control of another political entity by the League of Nations or the United Nations."),
    -9: ("not_applicable", "Not applicable", "This variable may not always be applicable, most frequently for independences, in which case it is coded \"-9\"."),
}  # fmt: skip
TERRITORY_TYPE = {0: "dependent territory", 1: "homeland territory"}
INDEPENDENCE_DEF = (
    "Independence refers to the termination of colonial rule over a dependency. This termination must involve the "
    "former dependency's attaining effective control over its own foreign affairs and armed forces as wed as "
    "achieving some measure of diplomatic recognition."
)
CONFLICT_DEF = (
    "This is coded if there was military conflict between organized forces of both sides. Unorganized violence, such "
    "as riots, is not so classified. This variable is coded \"1\" for military conflict and \"0\" for no military "
    "conflict."
)
PORTION = {0: "part of the unit", 1: "entire unit", -9: "could not be determined"}
CONTIGUITY = {1: "contiguous", 0: "non-contiguous", -9: "independence case (contiguous to itself)"}
#: Manual, Appendix A: "In the COW Entities list, 1 is the UN" and "the League is 0".
SPECIAL_CODES = {0: "League of Nations", 1: "United Nations"}

#: Entities.pdf "Ending Political Status" phrases -> relation slug (KG predicate cowent:<slug>).
STATUS_RELATIONS = {
    "Became colony of": "colony_of",
    "Became protectorate of": "protectorate_of",
    "Mandated to": "mandated_to",
    "Became possession of": "possession_of",
    "Leased to": "leased_to",
    "Occupied by": "occupied_by",
    "Claimed by": "claimed_by",
    "Became part of": "part_of",
    "Became neutral or demilitarized zone of": "neutral_zone_of",
}
ENTITY_LIST_LAST_YEAR = 1993

TC_PROPERTIES: dict[str, tuple[str, str, str, str, str | None, str | None]] = {
    "cowtc:gainer": ("item", "cowtc_gainer", "gaining side (COW Territorial Change)", "bénéficiaire (COW Territorial Change)", "cowtc_gained_in", "gained territory in (COW TC)"),
    "cowtc:loser": ("item", "cowtc_loser", "losing side (COW Territorial Change)", "perdant (COW Territorial Change)", "cowtc_lost_in", "lost territory in (COW TC)"),
    "cowtc:territory": ("item", "cowtc_territory", "entity exchanged (COW Territorial Change)", "entité échangée (COW Territorial Change)", "cowtc_exchanged_in", "exchanged in (COW TC)"),
    "cowtc:procedure": ("string", "cowtc_procedure", "procedure of territorial change (COW TC, coded)", "procédure du changement territorial (COW TC, codé)", None, None),
    "cowtc:independence": ("string", "cowtc_independence", "independence (COW TC indep, coded)", "indépendance (COW TC indep, codé)", None, None),
    "cowtc:military_conflict": ("string", "cowtc_military_conflict", "military conflict (COW TC conflict, coded)", "conflit militaire (COW TC conflict, codé)", None, None),
    "cowtc:area_km2": ("quantity", "cowtc_area_km2", "area of unit exchanged, km2 (COW TC)", "superficie de l'unité échangée, km2 (COW TC)", None, None),
    "cowtc:population": ("quantity", "cowtc_population", "population of unit exchanged (COW TC)", "population de l'unité échangée (COW TC)", None, None),
    "cowtc:portion": ("string", "cowtc_portion", "portion of unit exchanged (COW TC)", "part de l'unité échangée (COW TC)", None, None),
    "cowtc:gainer_territory_type": ("string", "cowtc_gainer_territory_type", "type of territory for the gaining side (COW TC)", "type de territoire pour le bénéficiaire (COW TC)", None, None),
    "cowtc:loser_territory_type": ("string", "cowtc_loser_territory_type", "type of territory for the losing side (COW TC)", "type de territoire pour le perdant (COW TC)", None, None),
    "cowtc:gainer_contiguity": ("string", "cowtc_gainer_contiguity", "contiguity of unit to the gaining side (COW TC)", "contiguïté avec le bénéficiaire (COW TC)", None, None),
    "cowtc:loser_contiguity": ("string", "cowtc_loser_contiguity", "contiguity of unit to the losing side (COW TC)", "contiguïté avec le perdant (COW TC)", None, None),
    "cowtc:system_entry": ("string", "cowtc_system_entry", "system entry with the change (COW TC entry)", "entrée dans le système (COW TC)", None, None),
    "cowtc:system_exit": ("string", "cowtc_system_exit", "system exit with the change (COW TC exit)", "sortie du système (COW TC)", None, None),
    "cowent:colony_of": ("item", "cowent_colony_of", "colony of (COW entities list)", "colonie de (liste des entités COW)", "cowent_has_colony", "has colony (COW entities list)"),
    "cowent:protectorate_of": ("item", "cowent_protectorate_of", "protectorate of (COW entities list)", "protectorat de (liste des entités COW)", "cowent_has_protectorate", "has protectorate (COW entities list)"),
    "cowent:mandated_to": ("item", "cowent_mandated_to", "mandated to (COW entities list)", "mandat confié à (liste des entités COW)", "cowent_holds_mandate", "holds mandate over (COW entities list)"),
    "cowent:possession_of": ("item", "cowent_possession_of", "possession of (COW entities list)", "possession de (liste des entités COW)", "cowent_has_possession", "has possession (COW entities list)"),
    "cowent:leased_to": ("item", "cowent_leased_to", "leased to (COW entities list)", "cédé à bail à (liste des entités COW)", "cowent_leases", "leases (COW entities list)"),
    "cowent:occupied_by": ("item", "cowent_occupied_by", "occupied by (COW entities list)", "occupé par (liste des entités COW)", "cowent_occupies", "occupies (COW entities list)"),
    "cowent:claimed_by": ("item", "cowent_claimed_by", "claimed by (COW entities list)", "revendiqué par (liste des entités COW)", "cowent_claims", "claims (COW entities list)"),
    "cowent:part_of": ("item", "cowent_part_of", "part of (COW entities list)", "partie de (liste des entités COW)", "cowent_has_part", "has part (COW entities list)"),
    "cowent:neutral_zone_of": ("item", "cowent_neutral_zone_of", "neutral or demilitarized zone of (COW entities list)", "zone neutre ou démilitarisée de (liste des entités COW)", "cowent_has_neutral_zone", "has neutral zone (COW entities list)"),
    "cowent:status_unreadable": ("item", "cowent_status_unreadable", "status not printed (COW entities list; counterpart only)", "statut non imprimé (liste des entités COW)", None, None),
}  # fmt: skip


# =============================================================================== minimal PDF text reader

_TOK = re.compile(rb"\((?:\\.|[^\\)])*\)|\[|\]|<<|>>|/[^\s/\[\]()<>]+|-?\d*\.\d+|-?\d+|[A-Za-z*'\"]+", re.S)
_NUM = re.compile(rb"-?\d*\.?\d+")
_ESC = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12, 40: 40, 41: 41, 92: 92}
PDF_MAX_STREAM = 8_000_000
PDF_MAX_OBJECTS = 20_000


def _inflate(raw: bytes) -> bytes:
    d = zlib.decompressobj()
    out = d.decompress(raw, PDF_MAX_STREAM)
    if d.unconsumed_tail:
        raise drift("PDF stream exceeds the decompression bound", "too_large")
    return out


def _unescape(s: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(s):
        c = s[i]
        if c == 0x5C and i + 1 < len(s):
            n = s[i + 1]
            if n in _ESC:
                out.append(_ESC[n])
                i += 2
                continue
            if 48 <= n <= 55:
                j, v = i + 1, 0
                while j < len(s) and j < i + 4 and 48 <= s[j] <= 55:
                    v = v * 8 + s[j] - 48
                    j += 1
                out.append(v & 255)
                i = j
                continue
            if n in (10, 13):  # line continuation
                i += 2
                continue
            i += 1
            continue
        out.append(c)
        i += 1
    return bytes(out)


def _pdf_objects(b: bytes) -> tuple[dict[int, bytes], dict[int, bytes]]:
    """Object dictionaries and decoded streams (FlateDecode), including objects packed in object streams."""
    objs: dict[int, bytes] = {}
    streams: dict[int, bytes] = {}
    for m in re.finditer(rb"(?<![0-9])(\d+) 0 obj\s*(.*?)endobj", b, re.S):
        if len(objs) > PDF_MAX_OBJECTS:
            raise drift("PDF has too many objects", "too_large")
        n, body = int(m.group(1)), m.group(2)
        sm = re.match(rb"(<<.*?>>)\s*stream\r?\n", body, re.S)
        if not sm:
            objs[n] = body
            continue
        d = sm.group(1)
        lm = re.search(rb"/Length (\d+)(?! 0 R)", d)
        raw = body[sm.end() :]
        raw = raw[: int(lm.group(1))] if lm else raw.rsplit(b"endstream", 1)[0]
        data = _inflate(raw) if b"FlateDecode" in d else raw
        objs[n], streams[n] = d, data
        if b"/ObjStm" in d:
            first = int(re.search(rb"/First (\d+)", d).group(1))  # type: ignore[union-attr]
            count = int(re.search(rb"/N (\d+)", d).group(1))  # type: ignore[union-attr]
            hdr = data[:first].split()
            offs = [(int(hdr[2 * i]), int(hdr[2 * i + 1])) for i in range(count)]
            for i, (on, off) in enumerate(offs):
                end = offs[i + 1][1] if i + 1 < count else len(data) - first
                objs[on] = data[first + off : first + end]
    return objs, streams


def _ref(d: bytes, key: bytes) -> int | None:
    m = re.search(rb"/" + key + rb"\s*(\d+) 0 R", d)
    return int(m.group(1)) if m else None


@dataclass(frozen=True)
class Piece:
    page: int
    y: float
    x0: float
    x1: float
    text: str


def pdf_pieces(b: bytes) -> list[Piece]:
    """Positioned text pieces of a simple PDF (one simple font with a /Widths array; no rotation)."""
    objs, streams = _pdf_objects(b)
    cat = next((v for v in objs.values() if b"/Type/Catalog" in v.replace(b" /", b"/")), None)
    if cat is None:
        raise drift("PDF: no catalog")
    pages: list[int] = []

    def walk(n: int, depth: int = 0) -> None:
        if depth > 20:
            raise drift("PDF: page tree too deep")
        d = objs.get(n, b"")
        kids = re.search(rb"/Kids\s*\[(.*?)\]", d, re.S)
        if kids:
            for k in re.findall(rb"(\d+) 0 R", kids.group(1)):
                walk(int(k), depth + 1)
        elif b"/Contents" in d:
            pages.append(n)

    root = _ref(cat, b"Pages")
    if root is None:
        raise drift("PDF: no page tree")
    walk(root)
    font = next((v for v in objs.values() if b"/Widths" in v and b"/FirstChar" in v), None)
    if font is None:
        raise drift("PDF: no simple font with widths")
    first = int(re.search(rb"/FirstChar (\d+)", font).group(1))  # type: ignore[union-attr]
    widths = [float(x) for x in re.search(rb"/Widths\s*\[(.*?)\]", font, re.S).group(1).split()]  # type: ignore[union-attr]

    def width(ch: int) -> float:
        i = ch - first
        return widths[i] if 0 <= i < len(widths) and widths[i] > 0 else 556.0

    out: list[Piece] = []
    for pno, pn in enumerate(pages, 1):
        cref = _ref(objs[pn], b"Contents")
        if cref is None or cref not in streams:
            raise drift(f"PDF page {pno}: no content stream")
        stack: list[bytes] = []
        tm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
        lm = tm[:]
        tl, fs, tx = 0.0, 1.0, 0.0
        for t in _TOK.findall(streams[cref]):
            if t[:1] == b"(" or t in (b"[", b"]", b"<<", b">>") or t[:1] == b"/" or _NUM.fullmatch(t):
                stack.append(t)
                continue
            if t == b"BT":
                tm, lm, tx = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0], [1.0, 0.0, 0.0, 1.0, 0.0, 0.0], 0.0
            elif t == b"Tf" and len(stack) >= 1:
                fs = float(stack[-1])
            elif t == b"TL" and stack:
                tl = float(stack[-1])
            elif t == b"Tm" and len(stack) >= 6:
                tm = [float(x) for x in stack[-6:]]
                lm, tx = tm[:], 0.0
            elif t in (b"Td", b"TD") and len(stack) >= 2:
                dx, dy = float(stack[-2]), float(stack[-1])
                if t == b"TD":
                    tl = -dy
                lm = [lm[0], lm[1], lm[2], lm[3], lm[0] * dx + lm[2] * dy + lm[4], lm[1] * dx + lm[3] * dy + lm[5]]
                tm, tx = lm[:], 0.0
            elif t == b"T*":
                lm = [lm[0], lm[1], lm[2], lm[3], lm[2] * (-tl) + lm[4], lm[3] * (-tl) + lm[5]]
                tm, tx = lm[:], 0.0
            elif t in (b"Tj", b"TJ"):
                if t == b"Tj":
                    items = stack[-1:]
                else:
                    i = len(stack) - 1
                    while i >= 0 and stack[i] != b"[":
                        i -= 1
                    items = [x for x in stack[i + 1 :] if x != b"]"]
                for it in items:
                    if it[:1] == b"(":
                        s = _unescape(it[1:-1])
                        x0 = tm[0] * tx + tm[4]
                        tx += sum(width(c) for c in s) / 1000.0 * fs
                        out.append(Piece(pno, tm[5], x0, tm[0] * tx + tm[4], s.decode("cp1252", errors="replace")))
                    elif _NUM.fullmatch(it):
                        tx -= float(it) / 1000.0 * fs
            stack = []
    return out


#: Printed columns of Entities.pdf (x of the left edge, PDF points): number < 80, name < 300, begin < 355, end < 385.
ENTITY_COLUMNS = (("num", 80.0), ("name", 300.0), ("begin", 355.0), ("end", 385.0), ("status", 1e9))


def pdf_text_rows(b: bytes) -> list[dict[str, Any]]:
    """Rows of the Entities list: ``{"page", "row", "num", "name", "begin", "end", "status"}`` (cells as printed).

    Pieces on one baseline (within 0.3 pt) form a line; abutting pieces (gap < 0.6 pt: clipped words) are glued; a
    line without an entity number continues the next numbered line (a cell printed on two lines)."""
    pieces = pdf_pieces(b)
    by_page: dict[int, list[Piece]] = defaultdict(list)
    for p in pieces:
        by_page[p.page].append(p)
    out: list[dict[str, Any]] = []
    for pno in sorted(by_page):
        ps = sorted(by_page[pno], key=lambda p: -p.y)
        groups: list[tuple[float, list[Piece]]] = []
        for p in ps:
            if groups and abs(groups[-1][0] - p.y) <= 0.3:
                groups[-1][1].append(p)
            else:
                groups.append((p.y, [p]))
        pending: dict[str, str] = {}
        row_no = 0
        for _, g in groups:
            g.sort(key=lambda p: p.x0)
            cells: list[list[Any]] = []
            for p in g:
                if cells and abs(p.x0 - cells[-1][2]) < 0.6:
                    cells[-1][0] += p.text
                    cells[-1][2] = p.x1
                else:
                    cells.append([p.text, p.x0, p.x1])
            cols: dict[str, str] = {}
            for text, x0, _ in cells:
                col = next(name for name, lim in ENTITY_COLUMNS if x0 < lim)
                cols[col] = cols.get(col, "") + text
            if cols.get("num", "").strip() in ("Entity", "Number") or cols.get("name", "").strip() == "Name":
                continue
            if not cols.get("num", "").strip():
                for k, v in cols.items():
                    pending[k] = pending.get(k, "") + v
                continue
            for k, v in pending.items():
                cols[k] = v + cols.get(k, "")
            pending = {}
            row_no += 1
            out.append(
                {
                    "page": pno,
                    "row": row_no,
                    **{k: re.sub(r"\s+", " ", cols.get(k, "")).strip() for k, _ in ENTITY_COLUMNS},
                }
            )
        if pending:
            out.append({"page": pno, "row": row_no + 1, "num": "", "dangling": pending})
    return out


# =============================================================================== entities list


@dataclass(frozen=True)
class EntitySpell:
    page: int
    row: int
    code: int
    name: str
    begin: int | None
    end: int | None
    relation: str  # slug, or "status_unreadable"
    status_text: str
    counterpart: int | None
    raw_begin: str
    raw_end: str


def _year_or_none(s: str) -> int | None:
    return int(s) if s.isdigit() and 1000 <= int(s) <= 2100 else None


def parse_entities(pdf: bytes) -> tuple[list[EntitySpell], dict[str, Any]]:
    rows = pdf_text_rows(pdf)
    spells: list[EntitySpell] = []
    report: dict[str, Any] = {"rows": len(rows), "dangling": [], "unparsed_status": [], "unreadable_years": []}
    for r in rows:
        if "dangling" in r or not r["num"].isdigit():
            report["dangling"].append(r)
            continue
        status = r["status"]
        m = re.fullmatch(r"(.*?)\s*(-?\d+)", status)
        rel, cp = "status_unreadable", None
        if m:
            cp = int(m.group(2))
            phrase = m.group(1).strip()
            if phrase in STATUS_RELATIONS:
                rel = STATUS_RELATIONS[phrase]
            elif phrase:
                report["unparsed_status"].append({"page": r["page"], "row": r["row"], "status": status})
                rel = "status_unreadable"
        else:
            report["unparsed_status"].append({"page": r["page"], "row": r["row"], "status": status})
        if (r["begin"] and _year_or_none(r["begin"]) is None) or (r["end"] and _year_or_none(r["end"]) is None):
            report["unreadable_years"].append({"page": r["page"], "row": r["row"], "begin": r["begin"], "end": r["end"]})
        spells.append(
            EntitySpell(
                page=r["page"],
                row=r["row"],
                code=int(r["num"]),
                name=r["name"],
                begin=_year_or_none(r["begin"]),
                end=_year_or_none(r["end"]),
                relation=rel,
                status_text=status,
                counterpart=cp,
                raw_begin=r["begin"],
                raw_end=r["end"],
            )
        )
    if len(spells) < 1000:
        raise drift(f"Entities.pdf: only {len(spells)} rows read (layout changed?)", "incomplete")
    names: dict[int, list[str]] = defaultdict(list)
    for s in spells:
        if s.name and s.name not in names[s.code]:
            names[s.code].append(s.name)
    report["codes"] = len(names)
    report["codes_with_several_names"] = {k: v for k, v in sorted(names.items()) if len(v) > 1}
    report["relations"] = dict(sorted(_count(s.relation for s in spells).items()))
    return spells, report


def _count(it: Any) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for x in it:
        out[x] += 1
    return dict(out)


def entity_names(spells: Sequence[EntitySpell]) -> dict[int, str]:
    names: dict[int, list[str]] = defaultdict(list)
    for s in spells:
        if s.name and s.name not in names[s.code]:
            names[s.code].append(s.name)
    return {k: " / ".join(v) for k, v in names.items()}


# =============================================================================== territorial changes


@dataclass(frozen=True)
class TcRow:
    line: int
    number: int
    when: CodedTime
    gainer: int | None
    loser: int | None
    entity: int | None
    procedure: int
    gaintype: int | None
    losetype: int | None
    contgain: int | None
    contlose: int | None
    area: str | None
    pop: str | None
    portion: int | None
    entry: int | None
    exit: int | None
    indep: int
    conflict: int
    version: str


def _decimal(v: str) -> str | None:
    s = v.strip()
    if s in ("", ".", "-9"):
        return None
    if not re.fullmatch(r"\d+(\.\d+)?", s):
        raise drift(f"{TC_MEMBER}: not a decimal: {s!r}")
    s = s.rstrip("0").rstrip(".") if "." in s else s
    return s or "0"


def parse_tc(body: bytes) -> list[TcRow]:
    t = read_delimited(body, name=TC_MEMBER, columns=_TC_COLS, delimiter=",", encoding="utf-8", min_rows=500)
    out: list[TcRow] = []
    seen: set[int] = set()
    miss = ("", ".")
    for line, r in t:
        w = f"{TC_MEMBER} line {line}"
        num = int_code(r["number"], what=f"{w} number", missing=miss)
        year = int_code(r["year"], what=f"{w} year", missing=miss)
        month = int_code(r["month"], what=f"{w} month", missing=(*miss, "-9"))
        proc = int_code(r["procedur"], what=f"{w} procedur", missing=miss)
        indep = int_code(r["indep"], what=f"{w} indep", missing=miss)
        conflict = int_code(r["conflict"], what=f"{w} conflict", missing=miss)
        if num is None or year is None or num in seen:
            raise drift(f"{w}: number/year missing or number repeated")
        if proc not in PROCEDURES or indep not in (0, 1) or conflict not in (0, 1):
            raise drift(f"{w}: procedur/indep/conflict outside the manual")
        if month is not None and not 1 <= month <= 12:
            raise drift(f"{w}: month {month}")
        seen.add(num)

        def code(col: str) -> int | None:
            return int_code(r[col], what=f"{w} {col}", missing=(*miss, "-9"))  # noqa: B023

        out.append(
            TcRow(
                line=line,
                number=num,
                when=CodedTime(year, month),
                gainer=code("gainer"),
                loser=code("loser"),
                entity=code("entity"),
                procedure=proc,
                gaintype=code("gaintype"),
                losetype=code("losetype"),
                contgain=int_code(r["contgain"], what=f"{w} contgain", missing=miss),
                contlose=int_code(r["contlose"], what=f"{w} contlose", missing=miss),
                area=_decimal(r["area"]),
                pop=_decimal(r["pop"]),
                portion=int_code(r["portion"], what=f"{w} portion", missing=miss),
                entry=int_code(r["entry"], what=f"{w} entry", missing=miss),
                exit=int_code(r["exit"], what=f"{w} exit", missing=miss),
                indep=indep,
                conflict=conflict,
                version=r["version"].strip(),
            )
        )
    return out


class EntityResolver:
    """COW code at a date -> KG entity: a state (QID / ``cow:<code>`` through KGGEOPOL's resolver) when the crosswalk
    knows the code as a state at that date, else the COW political entity ``cowent:<code>``."""

    def __init__(self, resolver: StateEntityResolver, names: Mapping[int, str], kg: Kg) -> None:
        self.r = resolver
        self.names = dict(names)
        self.kg = kg

    def territory(self, code: int) -> str:
        eid = f"cowent:{code}"
        label = SPECIAL_CODES.get(code) or self.names.get(code) or f"COW political entity {code}"
        self.kg.entity(eid, "territory", f"{label} (COW entity {code})", "COW political entity (Territorial Change v6 Entities list)")  # fmt: skip
        return eid

    def party(self, code: int, when: date) -> str:
        if code in SPECIAL_CODES:
            return self.territory(code)
        res = self.r.crosswalk.resolve("cow", code, when)
        if res.reason == "unmapped":
            return self.territory(code)
        return self.r.entity("cow", code, when, self.names.get(code))


def tc_kg(rows: Sequence[TcRow], *, raw_sha256: str, resolver: StateEntityResolver, names: Mapping[int, str]) -> Kg:
    kg = Kg("cowtc", raw_sha256, TC_MEMBER)
    er = EntityResolver(resolver, names, kg)
    for r in rows:
        sel = {"line": r.line}
        ev = f"cowtc:{r.number}"
        k = str(r.number)
        when = r.when.start
        terr = names.get(r.entity, f"entity {r.entity}") if r.entity is not None else "unknown entity"
        kg.entity(ev, "event", f"Territorial change {r.number}: {terr} ({r.when.text})", "COW Territorial Change v6 case")
        etype = "independence" if r.indep == 1 else "territorial_change"
        kg.string(f"{k}:type_of_event", ev, "colonial:event_type", etype, sel={**sel, "column": "indep"})
        kg.point_in_time(f"{k}:date", ev, r.when, sel={**sel, "column": ["year", "month"]},
                         qualifiers={"as_coded": r.when.text, "rule": "date of the treaty, else of the end of action / plebiscite / annexation act (manual)"})  # fmt: skip
        if r.gainer is not None:
            kg.item(f"{k}:gainer", ev, "cowtc:gainer", er.party(r.gainer, when), sel={**sel, "column": "gainer"},
                    qualifiers={"code": r.gainer})  # fmt: skip
        if r.loser is not None:
            kg.item(f"{k}:loser", ev, "cowtc:loser", er.party(r.loser, when), sel={**sel, "column": "loser"},
                    qualifiers={"code": r.loser})  # fmt: skip
        if r.entity is not None:
            kg.item(f"{k}:territory", ev, "cowtc:territory", er.territory(r.entity), sel={**sel, "column": "entity"},
                    qualifiers={"code": r.entity})  # fmt: skip
        slug_, label, definition = PROCEDURES[r.procedure]
        kg.string(f"{k}:procedure", ev, "cowtc:procedure", label, sel={**sel, "column": "procedur"},
                  qualifiers={"code": r.procedure, "definition": definition})  # fmt: skip
        kg.string(f"{k}:independence", ev, "cowtc:independence", "independence" if r.indep else "not an independence",
                  sel={**sel, "column": "indep"}, qualifiers={"code": r.indep, "definition": INDEPENDENCE_DEF})  # fmt: skip
        kg.string(f"{k}:conflict", ev, "cowtc:military_conflict", "military conflict" if r.conflict else "no military conflict",
                  sel={**sel, "column": "conflict"}, qualifiers={"code": r.conflict, "definition": CONFLICT_DEF,
                  "not_a_causal_claim": "the conflict is not identified (registry/causal/relations_colonial.yaml)"})  # fmt: skip
        if r.area is not None:
            kg.s(f"{k}:area", ev, "cowtc:area_km2", sel={**sel, "column": "area"},
                 value={"type": "quantity", "amount": r.area, "unit": "km2"},
                 qualifiers={"note": "manual: 'A few unaccounted for cases were small in size, for which one square kilometer is entered as the area'"} if r.area == "1" else {})  # fmt: skip
        if r.pop is not None:
            kg.s(f"{k}:pop", ev, "cowtc:population", sel={**sel, "column": "pop"},
                 value={"type": "quantity", "amount": r.pop, "unit": "persons"})  # fmt: skip
        if r.portion in PORTION:
            kg.string(f"{k}:portion", ev, "cowtc:portion", PORTION[r.portion], sel={**sel, "column": "portion"})
        for prop, v, col in (("cowtc:gainer_territory_type", r.gaintype, "gaintype"), ("cowtc:loser_territory_type", r.losetype, "losetype")):  # fmt: skip
            if v in TERRITORY_TYPE:
                kg.string(f"{k}:{col}", ev, prop, TERRITORY_TYPE[v], sel={**sel, "column": col}, qualifiers={"code": v})
        for prop, v, col in (("cowtc:gainer_contiguity", r.contgain, "contgain"), ("cowtc:loser_contiguity", r.contlose, "contlose")):  # fmt: skip
            if v in CONTIGUITY:
                kg.string(f"{k}:{col}", ev, prop, CONTIGUITY[v], sel={**sel, "column": col}, qualifiers={"code": v})
        for prop, v, col in (("cowtc:system_entry", r.entry, "entry"), ("cowtc:system_exit", r.exit, "exit")):
            if v == 1:
                kg.string(f"{k}:{col}", ev, prop, "yes", sel={**sel, "column": col}, qualifiers={"code": v})
        if r.indep == 1 and r.gainer is not None and r.loser is not None and r.gainer > 1 and r.loser > 1:
            kg.string(f"{k}:independence_key", ev, "colonial:independence_key", independence_key(r.gainer, r.loser),
                      sel={**sel, "column": ["gainer", "loser", "indep"]},
                      qualifiers={"method": "shared COW codes (state = gainer, former ruler = loser); date-free"})  # fmt: skip
    return kg


def entities_kg(
    spells: Sequence[EntitySpell],
    *,
    raw_sha256: str,
    resolver: StateEntityResolver,
    names: Mapping[int, str],
    alias_iso3: Mapping[int, str],
) -> Kg:
    """Status spells of the COW political entities, plus the dated ``geopol:corresponds_to_iso3`` of each entity
    code the crosswalk maps (state codes) or an exact-name alias maps (``registry/atlas/geo_aliases/a_colonial.yaml``)."""
    kg = Kg("cowent", raw_sha256, ENTITIES_MEMBER)
    er = EntityResolver(resolver, names, kg)
    for s in spells:
        subj = er.territory(s.code)
        sel = {"page": s.page, "row": s.row}
        kw: dict[str, Any] = {}
        if s.begin is not None:
            kw |= {"start": f"{s.begin:04d}-01-01", "start_precision": 9}
        if s.end is not None and (s.begin is None or s.end >= s.begin):
            kw |= {"end": f"{s.end:04d}-01-01", "end_precision": 9}
        when = date(s.begin or s.end or 1900, 7, 1)
        qual: dict[str, Any] = {"status_as_printed": s.status_text, "begin_as_printed": s.raw_begin, "end_as_printed": s.raw_end}
        if s.end == ENTITY_LIST_LAST_YEAR:
            qual["end_note"] = "1993 is the latest End Year in the list; the list does not say whether it marks its coverage end"
        if s.begin is not None and s.end is not None and s.end < s.begin:
            qual["inconsistent"] = "End Year before Begin Year (end not stated)"
        if s.counterpart is None:
            continue
        kg.item(f"{s.code}:p{s.page}r{s.row}", subj, f"cowent:{s.relation}", er.party(s.counterpart, when), sel=sel,
                qualifiers={**qual, "counterpart_code": s.counterpart}, **kw)  # fmt: skip
    # dated correspondences
    seen: set[str] = set()
    for s in spells:
        if s.begin is None:
            continue
        end = s.end if s.end is not None and s.end >= s.begin else s.begin
        iso3, how = None, None
        res = resolver.crosswalk.resolve("cow", s.code, date(end, 7, 1))
        if res.iso3:
            iso3, how = res.iso3, f"COW code {s.code} via atlas_geopol_common.StateCodeCrosswalk ({res.reason})"
        elif s.code in alias_iso3:
            iso3, how = alias_iso3[s.code], "exact name alias, registry/atlas/geo_aliases/a_colonial.yaml scheme cowent"
        if iso3 is None:
            continue
        key = f"{s.code}:iso3:{iso3}:p{s.page}r{s.row}"
        if key in seen:
            continue
        seen.add(key)
        kg.s(
            key,
            f"cowent:{s.code}",
            "geopol:corresponds_to_iso3",
            sel={"page": s.page, "row": s.row},
            value={"type": "string", "value": iso3},
            start=f"{s.begin:04d}-01-01",
            start_precision=9,
            end=f"{end:04d}-12-31",
            end_precision=9,
            qualifiers={
                "method": how,
                "iso3_meaning": "the present-day crosswalk entity this COW entity code denotes; borders may differ",
            },
        )
    return kg


# =============================================================================== atlas tables


def _resolve(cw: StateCodeCrosswalk, code: int, when: date) -> tuple[str | None, str | None, str]:
    r = cw.resolve("cow", code, when)
    return r.iso3, r.qid, r.reason


def changes_cy_atlas(rows: Sequence[TcRow], *, raw_sha256: str, crosswalk: StateCodeCrosswalk) -> AtlasRows:
    """COMPUTED counts of distinct territorial changes per (state, year): as gainer / loser, by procedure,
    independence, military conflict. Only (state, year) pairs with at least one change are listed."""
    from reva.atlas.tables import json_obj, year_period

    cells: dict[tuple[str, str, int], set[int]] = defaultdict(set)
    lines: dict[tuple[str, str, int], set[int]] = defaultdict(set)
    codes: dict[tuple[str, int], set[int]] = defaultdict(set)
    meta: dict[tuple[str, int], tuple[str | None, str | None, str]] = {}
    for r in rows:
        y = r.when.year
        for role, code in (("gained", r.gainer), ("lost", r.loser)):
            if code is None:
                continue
            iso3, qid, reason = _resolve(crosswalk, code, date(y, 7, 1))
            geo = f"iso3:{iso3}" if iso3 else f"cow:{code}"  # grouping key: codes of one country are counted together
            meta[(geo, y)] = (iso3, qid, reason)
            codes[(geo, y)].add(code)
            inds = [role, f"{role}|procedure={PROCEDURES[r.procedure][0]}"]
            if r.indep:
                inds.append(f"{role}|independence=1")
            if r.conflict:
                inds.append(f"{role}|military_conflict=1")
            for ind in inds:
                cells[(ind, geo, y)].add(r.number)
                lines[(ind, geo, y)].add(r.line)
    out = AtlasRows()
    methods = {
        "gained": "distinct COW TC cases with this state as gainer, dated in the year",
        "lost": "distinct COW TC cases with this state as loser, dated in the year",
    }
    for (ind, geo, y), nums in sorted(cells.items()):
        iso3, qid, reason = meta[(geo, y)]
        p0, p1 = year_period(y)
        role = ind.split("|", 1)[0]
        fl: dict[str, Any] = {
            "computed_from": "cowtc tc2018.csv",
            "method": methods[role] + ("; " + ind.split("|", 1)[1] if "|" in ind else ""),
            "tc_numbers": sorted(nums),
            "cow_codes": sorted(codes[(geo, y)]),
            "code_resolution": reason,
        }
        if "procedure=" in ind:
            code = next(c for c, v in PROCEDURES.items() if f"procedure={v[0]}" in ind)
            fl["procedure_definition"] = PROCEDURES[code][2]
        if iso3 is None and qid:
            fl["historical_state_qid"] = qid
        out.add(
            {
                "source": "cowtc",
                "dataset": "changes_cy",
                "indicator": ind,
                "concept_id": None,
                "iso3": iso3,
                "provider_geo": "+".join(f"cow:{c}" for c in sorted(codes[(geo, y)])),
                "entity_qid": qid,
                "period_start": p0,
                "period_end": p1,
                "value": str(len(nums)),
                "value_text": None,
                "unit": "count",
                "flags": json_obj(fl),
                "raw_sha256": raw_sha256,
                "selector": json_obj(file=TC_MEMBER, lines=sorted(lines[(ind, geo, y)])[:50]),
            }
        )
    return out


def entity_status_atlas(
    spells: Sequence[EntitySpell],
    *,
    raw_sha256: str,
    crosswalk: StateCodeCrosswalk,
    names: Mapping[int, str],
    alias_iso3: Mapping[int, str],
) -> tuple[AtlasRows, dict[str, Any]]:
    """The entities list as printed: one cell per spell (value_text = counterpart). Spells without a readable Begin
    Year have no period and are only in the KG; overlapping identical cells are reported, not merged."""
    from reva.atlas.tables import json_obj

    out = AtlasRows()
    report: dict[str, Any] = {"no_period": [], "duplicate_cells": []}
    seen: set[tuple[str, str, date, date]] = set()
    for s in spells:
        if s.counterpart is None:
            report["no_period"].append({"page": s.page, "row": s.row, "reason": "no counterpart printed"})
            continue
        y0 = s.begin if s.begin is not None else s.end
        if y0 is None:
            report["no_period"].append({"page": s.page, "row": s.row, "code": s.code, "status": s.status_text})
            continue
        y1 = s.end if s.end is not None and s.end >= y0 else y0
        p0, p1 = date(y0, 1, 1), date(y1 + 1, 1, 1)
        res = crosswalk.resolve("cow", s.code, date(y1, 7, 1))
        iso3, how = res.iso3, res.reason
        if iso3 is None and s.code in alias_iso3:
            iso3, how = alias_iso3[s.code], "exact_name_alias"
        cp_label = SPECIAL_CODES.get(s.counterpart) or names.get(s.counterpart)
        cp_res = crosswalk.resolve("cow", s.counterpart, date(y0, 7, 1))
        if cp_label is None:
            cp_label = cp_res.label or f"COW {s.counterpart}"
        ind = f"status|relation={s.relation}|counterpart={s.counterpart}"
        key = (ind, f"cowent:{s.code}", p0, p1)
        if key in seen:
            report["duplicate_cells"].append({"page": s.page, "row": s.row, "code": s.code, "status": s.status_text})
            continue
        seen.add(key)
        fl: dict[str, Any] = {
            "entity_name": s.name,
            "status_as_printed": s.status_text,
            "begin_as_printed": s.raw_begin,
            "end_as_printed": s.raw_end,
            "counterpart_code": s.counterpart,
            "counterpart_iso3": cp_res.iso3,
            "code_resolution": how,
        }
        if iso3:
            fl["iso3_meaning"] = "the present-day crosswalk entity this COW entity code denotes; borders may differ"
        if s.begin is None:
            fl["period_meaning"] = "Begin Year not printed: period = End Year only"
        if s.end is None:
            fl["period_meaning"] = "End Year not printed: period = Begin Year only"
        if s.end == ENTITY_LIST_LAST_YEAR:
            fl["end_note"] = "1993 is the latest End Year in the list; the list does not say whether it marks its coverage end"
        out.add(
            {
                "source": "cowtc",
                "dataset": "entity_status",
                "indicator": ind,
                "concept_id": None,
                "iso3": iso3,
                "provider_geo": f"cowent:{s.code}",
                "entity_qid": res.qid,
                "period_start": p0,
                "period_end": p1,
                "value": None,
                "value_text": f"cow:{s.counterpart} {cp_label}",
                "unit": "category",
                "flags": json_obj(fl),
                "raw_sha256": raw_sha256,
                "selector": json_obj(file=ENTITIES_MEMBER, page=s.page, row=s.row),
            }
        )
    return out, report


def cowent_aliases() -> dict[int, str]:
    """``cowent:<code>`` -> crosswalk key from the geo scheme ``cowent`` (exact-name aliases, with evidence)."""
    from reva.atlas.tables import geo_index

    idx = geo_index()
    if "cowent" not in idx.maps:
        return {}
    out = {}
    for k, v in idx.maps["cowent"].items():
        m = re.fullmatch(r"COWENT:(\d+)", k)
        if m:
            out[int(m.group(1))] = v
    return out


def dedupe_iso3(frame_rows: AtlasRows) -> tuple[AtlasRows, list[dict[str, Any]]]:
    """Two provider geographies mapping to one country for the same indicator and period would make REVA choose:
    such rows keep ``iso3 = null`` (reported)."""
    groups: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    cols = frame_rows.cols
    for i in range(len(frame_rows)):
        if cols["iso3"][i] is not None:
            groups[(cols["indicator"][i], cols["iso3"][i], cols["period_start"][i], cols["period_end"][i])].append(i)
    cleared = []
    for key, idxs in groups.items():
        if len({cols["provider_geo"][i] for i in idxs}) > 1:
            for i in idxs:
                cleared.append({"provider_geo": cols["provider_geo"][i], "iso3": cols["iso3"][i], "indicator": key[0]})
                cols["iso3"][i] = None
    return frame_rows, cleared


# =============================================================================== source spec + ingest


def atlas_source() -> Any:
    from reva.atlas.sources import AtlasSourceSpec

    return AtlasSourceSpec(
        name="atlas.cowtc",
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        atlas_source="cowtc",
        provider="Correlates of War Project (Territorial Change v6, hosted by Steven V. Miller, Clemson University)",
        licence=LICENCE,
        allowed_hosts=("correlatesofwar.org", "cran.r-project.org"),
        datasets=("changes_cy", "entity_status"),
        dataset_ids=("COW Territorial Change v6 (tc2018.csv)", "COW Entities list (Entities.pdf)"),
        cadence_seconds=180 * 86400,
        planned_requests=4,
        max_response_bytes=10_000_000,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        notes="terr-changes-v6.zip + countrycode 1.9.0 tarball: 2 requests",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    from reva.adapters.connectors.atlas_colonial import _kg_ready
    from reva.adapters.connectors.atlas_geopol_common import fetch_countrycode, load_qid_for_iso3

    want_cy, want_ent = run.wants("changes_cy"), run.wants("entity_status")
    media = ("application/zip", "application/x-zip-compressed", "application/octet-stream")
    raw = await run.get(TC_URL, key="cowtc:terr-changes-v6", media_types=media, max_bytes=5_000_000)
    mem = zip_members(raw.body, name="terr-changes-v6.zip")
    _, manual = member(mem, MANUAL_MEMBER, name="terr-changes-v6.zip")
    docs = [check_document("cowtc_manual", manual)]
    _, pdf = member(mem, ENTITIES_MEMBER, name="terr-changes-v6.zip")
    docs.append(check_document("cowtc_entities", pdf))
    _, csv_body = member(mem, TC_MEMBER, name="terr-changes-v6.zip")
    rows = parse_tc(csv_body)
    spells, ent_report = parse_entities(pdf)
    names = entity_names(spells)
    cw, cc_sha = await fetch_countrycode(run)
    sha = run.archive(raw)
    alias = cowent_aliases()
    out: dict[str, Any] = {
        "documents": docs,
        "changes": len(rows),
        "entities_report": {k: v for k, v in ent_report.items() if k not in ("dangling",)}
        | {"dangling": ent_report["dangling"][:20]},
        "atlas": {},
        "claims": {"delivered": 0, "reason": "no COW TC variable identifies a cause event (relations_colonial.yaml)"},
    }
    rights = {"licence": LICENCE, "provider": "Correlates of War Project", "citation": CITATION, "source_key": SOURCE_KEY}
    if want_cy:
        cy, cleared = dedupe_iso3(changes_cy_atlas(rows, raw_sha256=sha, crosswalk=cw))
        reg = run.register(
            dataset="changes_cy",
            frame=cy.frame(),
            raw_sha256s=[sha],
            coverage_note=f"COMPUTED counts from COW Territorial Change v6: {len(rows)} changes 1816-2018",
            rights=rights,
        )
        out["atlas"]["changes_cy"] = {k: reg[k] for k in ("dataset_key", "row_count", "created")} | {
            "iso3_cleared": cleared[:20]
        }
    if want_ent:
        es, es_report = entity_status_atlas(spells, raw_sha256=sha, crosswalk=cw, names=names, alias_iso3=alias)
        es, cleared = dedupe_iso3(es)
        reg = run.register(
            dataset="entity_status",
            frame=es.frame(),
            raw_sha256s=[sha],
            coverage_note=(
                f"COW Entities list (Entities.pdf in terr-changes-v6.zip): {len(spells)} status spells of "
                f"{len(names)} political entities, 1816-1993, as printed"
            ),
            rights=rights,
        )
        out["atlas"]["entity_status"] = {k: reg[k] for k in ("dataset_key", "row_count", "created")} | {
            "iso3_cleared": cleared[:20],
            "no_period": len(es_report["no_period"]),
            "duplicate_cells": es_report["duplicate_cells"][:20],
        }
    if _kg_ready(run):
        qids = load_qid_for_iso3()
        resolver = StateEntityResolver(cw, qids)
        k1 = tc_kg(rows, raw_sha256=sha, resolver=resolver, names=names)
        k2 = entities_kg(spells, raw_sha256=sha, resolver=resolver, names=names, alias_iso3=alias)
        code_ents, code_links = code_entities_and_links(resolver, raw_sha256=cc_sha, prefix="cowtc")
        ents: dict[str, KgEntity] = {**k1.entities, **k2.entities}
        for e in code_ents:
            ents.setdefault(e.qid, e)
        statements: list[KgStatement] = [*k1.statements, *k2.statements, *code_links]
        out["kg"] = load_colonial_kg(
            run.engine,
            run.store,
            list(ents.values()),
            statements,
            source_key=SOURCE_KEY,
            source_revision=TC_REVISION,
            raw_sha256s=[sha, cc_sha],
            retrieved_at=raw.retrieved_at,
            origin=run.origin,
            extra_properties=TC_PROPERTIES,
        )
        out["kg_counts"] = {"entities": len(ents), "statements": len(statements)}
    return out
