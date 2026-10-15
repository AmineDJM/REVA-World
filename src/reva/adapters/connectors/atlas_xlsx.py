"""Minimal, bounded, read-only XLSX reader (stdlib only; agent A-HISTECON).

The historical-economics providers (Maddison Project, Clio-Infra) publish Office
Open XML workbooks. The project has no spreadsheet dependency, so this module
reads the parts we need with ``zipfile`` + ``xml.etree`` and nothing else:

* shared strings and inline strings, numbers kept as the **exact cell text**
  (no float conversion: the source text is what gets stored as a decimal string);
* booleans and error cells (``#N/A`` …) are returned as typed markers, never as
  numbers;
* formulas are not evaluated: the cached value written by the producing
  application is returned (and :class:`Cell.formula` says it was a formula).

Safety bounds (raw bytes are untrusted): total uncompressed size, number of
members, per-sheet size and number of rows are capped; no external entity or
DTD processing (Python's expat does not fetch external entities, and a DTD in a
workbook part is refused outright).
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree as ET

NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
}
MAX_UNCOMPRESSED = 400 * 1024 * 1024
MAX_MEMBERS = 2000
MAX_ROWS = 2_000_000

_REF = re.compile(r"^([A-Z]{1,3})([0-9]+)$")


class XlsxError(ValueError):
    """The bytes are not a readable workbook within the bounds."""


@dataclass(frozen=True)
class Cell:
    ref: str
    kind: str  # "s" string | "n" number (exact text) | "b" bool | "e" error | "empty"
    text: str
    formula: bool = False


def col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def col_letters(idx: int) -> str:
    idx += 1
    out = ""
    while idx:
        idx, rem = divmod(idx - 1, 26)
        out = chr(65 + rem) + out
    return out


def _parse(data: bytes, part: str) -> ET.Element:
    head = data[:4096]
    if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
        raise XlsxError(f"workbook part {part} declares a DTD/entity (refused)")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise XlsxError(f"workbook part {part} is not well-formed XML: {exc}") from exc


class Workbook:
    def __init__(self, body: bytes) -> None:
        try:
            self._zip = zipfile.ZipFile(io.BytesIO(body))
        except zipfile.BadZipFile as exc:
            raise XlsxError("not a zip/xlsx container") from exc
        infos = self._zip.infolist()
        if len(infos) > MAX_MEMBERS:
            raise XlsxError("too many workbook members")
        if sum(i.file_size for i in infos) > MAX_UNCOMPRESSED:
            raise XlsxError("workbook expands beyond the uncompressed bound")
        self._names = {i.filename for i in infos}
        self._strings = self._shared_strings()
        self.sheets = self._sheet_targets()

    def _read(self, part: str) -> bytes:
        if part not in self._names:
            raise XlsxError(f"missing workbook part {part}")
        return self._zip.read(part)

    def _shared_strings(self) -> list[str]:
        part = "xl/sharedStrings.xml"
        if part not in self._names:
            return []
        root = _parse(self._read(part), part)
        out: list[str] = []
        for si in root.findall("m:si", NS):
            out.append("".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")))
        return out

    def _sheet_targets(self) -> dict[str, str]:
        wb = _parse(self._read("xl/workbook.xml"), "xl/workbook.xml")
        rels = _parse(self._read("xl/_rels/workbook.xml.rels"), "xl/_rels/workbook.xml.rels")
        targets = {r.get("Id"): r.get("Target", "") for r in rels.findall("pr:Relationship", NS)}
        out: dict[str, str] = {}
        for sh in wb.findall("m:sheets/m:sheet", NS):
            rid = sh.get(f"{{{NS['r']}}}id")
            tgt = targets.get(rid, "")
            tgt = tgt.lstrip("/")
            if not tgt.startswith("xl/"):
                tgt = "xl/" + tgt
            out[sh.get("name", "")] = tgt
        return out

    def rows(self, sheet: str) -> Iterator[tuple[int, dict[int, Cell]]]:
        """Yield (1-based row number, {0-based column: Cell}) for every non-empty row."""
        if sheet not in self.sheets:
            raise XlsxError(f"sheet {sheet!r} not in workbook (has {sorted(self.sheets)})")
        part = self.sheets[sheet]
        data = self._read(part)
        if b"<!DOCTYPE" in data[:4096] or b"<!ENTITY" in data[:4096]:
            raise XlsxError(f"sheet part {part} declares a DTD/entity (refused)")
        m = f"{{{NS['m']}}}"
        n_rows = 0
        for _ev, el in ET.iterparse(io.BytesIO(data), events=("end",)):
            if el.tag != m + "row":
                continue
            n_rows += 1
            if n_rows > MAX_ROWS:
                raise XlsxError("sheet exceeds the row bound")
            rnum = int(el.get("r", n_rows))
            cells: dict[int, Cell] = {}
            for c in el.findall(m + "c"):
                ref = c.get("r", "")
                mm = _REF.match(ref)
                if not mm:
                    continue
                cell = self._cell(c, ref)
                if cell.kind != "empty":
                    cells[col_index(mm.group(1))] = cell
            el.clear()
            if cells:
                yield rnum, cells

    def _cell(self, c: ET.Element, ref: str) -> Cell:
        m = f"{{{NS['m']}}}"
        t = c.get("t", "n")
        f = c.find(m + "f") is not None
        v = c.find(m + "v")
        if t == "inlineStr":
            is_ = c.find(m + "is")
            txt = "".join(x.text or "" for x in is_.iter(m + "t")) if is_ is not None else ""
            return Cell(ref, "s" if txt != "" else "empty", txt, f)
        if v is None or v.text is None:
            return Cell(ref, "empty", "", f)
        raw = v.text
        if t == "s":
            try:
                return Cell(ref, "s", self._strings[int(raw)], f)
            except (ValueError, IndexError) as exc:
                raise XlsxError(f"bad shared-string index in {ref}") from exc
        if t == "str":
            return Cell(ref, "s", raw, f)
        if t == "b":
            return Cell(ref, "b", raw, f)
        if t == "e":
            return Cell(ref, "e", raw, f)
        return Cell(ref, "n", raw.strip(), f)

    def table(self, sheet: str) -> list[list[Any]]:
        """Dense list-of-lists (strings; None for empty) — for small sheets only."""
        rows = list(self.rows(sheet))
        if not rows:
            return []
        width = max(max(c) for _, c in rows) + 1
        out: list[list[Any]] = []
        last = 0
        for rnum, cells in rows:
            while last + 1 < rnum:
                out.append([None] * width)
                last += 1
            out.append([cells[i].text if i in cells else None for i in range(width)])
            last = rnum
        return out
