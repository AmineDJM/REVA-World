"""Shared plumbing of the society & culture atlas sources (agent A-SOCIETY, V3 atlas).

Used by ``atlas_society_religion`` (RCS-Dem 2.0, COW World Religion Project), ``atlas_society_frac``
(HIEF, SSCP fractionalisation), ``atlas_society_justice`` (CDPD death penalty, WHO homicide estimates)
and ``atlas_society_gov`` (Transparency International CPI, OECD SIGI / GID-DB legal indicators, ParlGov
elections). Every module runs through ATLASCORE's single entry point (:mod:`reva.atlas.ingest`): it
fetches only through ``run.get`` (budget-metered SafeHttpClient restricted to the spec's hosts, or the
fixture replay), archives every response with ``run.archive`` **before** parsing, and registers atlas
tables with ``run.register``. No module here builds an HTTP client.

Helpers
-------
* :func:`fetch_archive` -- fetch a list of :class:`~reva.ports.connectors.FetchItem` sequentially,
  archive each response at once, return the bodies read back from the artifact store and their sha256.
* :func:`check_dataverse_record` -- a Harvard Dataverse dataset record (JSON API): the licence must be
  one of the expected names and every payload must match the MD5 the record publishes for its file
  (``format=original`` downloads are checked against the original upload's MD5).
* :class:`XlsxStream` -- bounded, streaming, read-only XLSX reader that accepts both the transitional
  (``schemas.openxmlformats.org``) and the *strict* (``purl.oclc.org/ooxml``) SpreadsheetML namespaces
  (the TI CPI workbook is strict OOXML). Numbers are returned as the exact cell text (no float
  conversion), formulas are not evaluated, DTD/entity declarations are refused.
* :class:`RowBuffer` -- columnar builder of the ATLASCORE long schema (cheaper than dicts).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from xml.etree import ElementTree as ET

from reva.adapters.connectors.atlas_history_common import decimal_text, year_period
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import FetchItem

DATAVERSE = "dataverse.harvard.edu"
S3_HOST = "dvn-cloud-iqss.s3.amazonaws.com"  # Dataverse /api/access answers 303 -> signed S3 URL

MAX_XLSX_UNCOMPRESSED = 400 * 1024 * 1024
MAX_XLSX_MEMBERS = 4000
MAX_XLSX_ROWS = 2_000_000
_REF = re.compile(r"^([A-Z]{1,3})([0-9]+)$")


class SocietyParseError(SourceResponseError):
    """Provider response drift (structure, licence, checksum) -- nothing is registered."""


def err(msg: str, kind: str = "schema_drift") -> SocietyParseError:
    return SocietyParseError(msg[:2000], retryable=False, kind=kind)


def canon(obj: Mapping[str, Any] | None = None, **kw: Any) -> str:
    """Canonical JSON object text (``flags`` / ``selector`` columns)."""
    return json.dumps({**dict(obj or {}), **kw}, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def num(text: str | None) -> str | None:
    """Provider numeric text as a strict decimal string (lossless normalisations only) or ``None``."""
    return decimal_text(text)


def day_period(d: date) -> tuple[date, date]:
    from datetime import timedelta

    return d, d + timedelta(days=1)


# ----------------------------------------------------------------------------- fetching
@dataclass
class Fetched:
    bodies: dict[str, bytes] = field(default_factory=dict)
    shas: dict[str, str] = field(default_factory=dict)
    meta: dict[str, dict[str, Any]] = field(default_factory=dict)


async def fetch_archive(run: Any, items: Sequence[FetchItem]) -> Fetched:
    """Fetch every item through ``run.get`` and archive it at once (``run.archive``).

    The returned bodies are read back from the artifact store (the archived bytes are what gets
    parsed). Structural checks happen afterwards; a failed check raises and registers nothing, the
    raw bytes stay archived as the record of what the provider served."""
    out = Fetched()
    for it in items:
        raw = await run.get(
            it.url, key=it.key, media_types=it.expected_media_types, max_bytes=it.max_bytes, params=dict(it.params)
        )
        sha = run.archive(raw)
        out.shas[it.key] = sha
        out.bodies[it.key] = run.store.get("raw", sha)
        md = dict(raw.response_metadata or {})
        out.meta[it.key] = {
            "url": it.url,
            "final_url": str(md.get("final_url", "")).split("?", 1)[0][:300] or None,  # no signed query
            "retrieved_at": raw.retrieved_at.isoformat() if raw.retrieved_at else None,
            "bytes": len(raw.body),
        }
    return out


# ----------------------------------------------------------------------------- dataverse
def dataverse_record_url(doi: str) -> str:
    return f"https://{DATAVERSE}/api/datasets/:persistentId/?persistentId={doi}"


def dataverse_file_url(file_id: int, *, original: bool = False) -> str:
    return f"https://{DATAVERSE}/api/access/datafile/{file_id}" + ("?format=original" if original else "")


def check_dataverse_record(
    body: bytes, *, doi: str, files: Mapping[int, tuple[str, bytes]], licences: Sequence[str]
) -> dict[str, Any]:
    """Licence and file integrity from the Dataverse dataset record (``latestVersion``).

    ``files`` maps a Dataverse file id to (expected file label, payload bytes). The payload must
    match the record's MD5 (for ingested tabular files the record's MD5 is the original upload's,
    which is what ``format=original`` serves). A different licence, a file no longer in the latest
    version, or a checksum mismatch raises :class:`SocietyParseError` (nothing registered)."""
    try:
        doc = json.loads(body)
        v = doc["data"]["latestVersion"]
    except (ValueError, KeyError, TypeError) as e:
        raise err(f"{doi}: Dataverse record is not the expected JSON ({e})") from e
    lic = (v.get("license") or {}).get("name") if isinstance(v.get("license"), dict) else v.get("license")
    if lic not in licences:
        raise err(f"{doi}: dataset licence is {lic!r}, expected one of {list(licences)}", kind="licence")
    recs = {f["dataFile"]["id"]: f["dataFile"] for f in v.get("files", []) if isinstance(f, dict) and "dataFile" in f}
    checked = []
    for fid, (label, payload) in files.items():
        df = recs.get(fid)
        if df is None:
            raise err(f"{doi}: file {fid} ({label}) is not in the latest dataset version", kind="stale_release")
        names = {df.get("filename"), df.get("originalFileName")}
        if label not in names:
            raise err(f"{doi}: file {fid} is now {sorted(n for n in names if n)}, expected {label!r}")
        want = (df.get("checksum") or {}).get("value")
        got = hashlib.md5(payload, usedforsecurity=False).hexdigest()
        if not want or want != got:
            raise err(f"{doi}: {label}: MD5 {got} does not match the Dataverse record ({want})", kind="integrity")
        checked.append({"file_id": fid, "label": label, "md5": want})
    return {
        "doi": doi,
        "version": f"{v.get('versionNumber')}.{v.get('versionMinorNumber')}",
        "release_time": v.get("releaseTime"),
        "licence": lic,
        "licence_uri": (v.get("license") or {}).get("uri") if isinstance(v.get("license"), dict) else None,
        "files": checked,
    }


# ----------------------------------------------------------------------------- CSV
def read_csv(body: bytes, name: str, *, required: Sequence[str] = (), encoding: str = "utf-8") -> list[dict[str, str]]:
    try:
        text = body.decode(encoding)
    except UnicodeDecodeError as e:
        raise err(f"{name}: not {encoding} text ({e})") from e
    text = text.removeprefix("﻿")
    if text.lstrip().startswith("<"):
        raise err(f"{name}: an HTML/XML page was served instead of CSV")
    reader = csv.DictReader(io.StringIO(text))
    header = reader.fieldnames or []
    missing = [c for c in required if c not in header]
    if missing:
        raise err(f"{name}: columns {missing} missing (header {header[:30]})")
    return list(reader)


# ----------------------------------------------------------------------------- XLSX
@dataclass(frozen=True)
class Cell:
    kind: str  # "s" string | "n" number (exact text) | "b" bool | "e" error
    text: str


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def col_letters(idx: int) -> str:
    out, n = "", idx + 1
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def _xml(data: bytes, part: str) -> ET.Element:
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise err(f"workbook part {part} declares a DTD/entity (refused)")
    try:
        return ET.fromstring(data)  # noqa: S314 - DTD/entities refused above; expat fetches nothing
    except ET.ParseError as e:
        raise err(f"workbook part {part} is not well-formed XML: {e}") from e


class XlsxStream:
    """Streaming XLSX reader (transitional or strict OOXML), bounded, values as exact cell text."""

    def __init__(self, body: bytes) -> None:
        try:
            self._zip = zipfile.ZipFile(io.BytesIO(body))
        except zipfile.BadZipFile as e:
            raise err("not a zip/xlsx container") from e
        infos = self._zip.infolist()
        if len(infos) > MAX_XLSX_MEMBERS or sum(i.file_size for i in infos) > MAX_XLSX_UNCOMPRESSED:
            raise err("workbook exceeds the member/size bounds")
        self._names = {i.filename for i in infos}
        self._strings = self._shared_strings()
        self.sheets = self._sheet_targets()

    def _read(self, part: str) -> bytes:
        if part not in self._names:
            raise err(f"missing workbook part {part}")
        return self._zip.read(part)

    def _shared_strings(self) -> list[str]:
        part = "xl/sharedStrings.xml"
        if part not in self._names:
            return []
        root = _xml(self._read(part), part)
        return ["".join(t.text or "" for t in si.iter() if _local(t.tag) == "t") for si in root if _local(si.tag) == "si"]

    def _sheet_targets(self) -> dict[str, str]:
        wb = _xml(self._read("xl/workbook.xml"), "xl/workbook.xml")
        rels = _xml(self._read("xl/_rels/workbook.xml.rels"), "xl/_rels/workbook.xml.rels")
        targets = {r.get("Id"): r.get("Target", "") for r in rels if _local(r.tag) == "Relationship"}
        out: dict[str, str] = {}
        for sh in wb.iter():
            if _local(sh.tag) != "sheet":
                continue
            rid = next((v for k, v in sh.attrib.items() if _local(k) == "id"), None)
            tgt = targets.get(rid, "").lstrip("/")
            if not tgt.startswith("xl/"):
                tgt = "xl/" + tgt
            out[sh.get("name", "")] = tgt
        return out

    def rows(self, sheet: str) -> Iterator[tuple[int, dict[int, Cell]]]:
        """Yield (1-based row number, {0-based column: Cell}) for every row with a non-empty cell."""
        if sheet not in self.sheets:
            raise err(f"sheet {sheet!r} not in workbook (has {sorted(self.sheets)})", kind="schema_drift")
        part = self.sheets[sheet]
        if part not in self._names:
            raise err(f"missing workbook part {part}")
        with self._zip.open(part) as fh:
            head = fh.read(4096)
        if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
            raise err(f"sheet part {part} declares a DTD/entity (refused)")
        n = 0
        with self._zip.open(part) as fh:
            for _ev, el in ET.iterparse(fh, events=("end",)):  # noqa: S314 - no DTD (checked above)
                if _local(el.tag) != "row":
                    continue
                n += 1
                if n > MAX_XLSX_ROWS:
                    raise err("sheet exceeds the row bound")
                rnum = int(el.get("r", n))
                cells: dict[int, Cell] = {}
                for c in el:
                    if _local(c.tag) != "c":
                        continue
                    m = _REF.match(c.get("r", ""))
                    if not m:
                        continue
                    cell = self._cell(c)
                    if cell is not None:
                        cells[col_index(m.group(1))] = cell
                el.clear()
                if cells:
                    yield rnum, cells

    def _cell(self, c: ET.Element) -> Cell | None:
        t = c.get("t", "n")
        v = next((x for x in c if _local(x.tag) == "v"), None)
        if t == "inlineStr":
            is_ = next((x for x in c if _local(x.tag) == "is"), None)
            txt = "".join(x.text or "" for x in is_.iter() if _local(x.tag) == "t") if is_ is not None else ""
            return Cell("s", txt) if txt != "" else None
        if v is None or v.text is None:
            return None
        raw = v.text
        if t == "s":
            try:
                txt = self._strings[int(raw)]
            except (ValueError, IndexError) as e:
                raise err(f"bad shared-string index {raw!r}") from e
            return Cell("s", txt) if txt != "" else None
        if t == "str":
            return Cell("s", raw) if raw != "" else None
        if t in ("b", "e"):
            return Cell(t, raw)
        return Cell("n", raw.strip())


def header_map(cells: Mapping[int, Cell]) -> dict[str, int]:
    return {c.text.strip(): i for i, c in cells.items() if c.kind == "s" and c.text.strip()}


# ----------------------------------------------------------------------------- rows
class RowBuffer:
    """Columnar builder of the ATLASCORE long schema for one table."""

    def __init__(self, source: str, dataset: str) -> None:
        self.source, self.dataset = source, dataset
        self.cols: dict[str, list[Any]] = {
            k: []
            for k in (
                "indicator",
                "concept_id",
                "iso3",
                "provider_geo",
                "entity_qid",
                "period_start",
                "period_end",
                "value",
                "value_text",
                "unit",
                "flags",
                "raw_sha256",
                "selector",
            )
        }

    def add(
        self,
        *,
        indicator: str,
        provider_geo: str,
        period: tuple[date, date],
        value: str | None,
        unit: str,
        flags: str,
        raw_sha256: str,
        selector: str,
        value_text: str | None = None,
        iso3: str | None = None,
        concept_id: str | None = None,
        entity_qid: str | None = None,
    ) -> None:
        """``flags`` / ``selector`` are canonical JSON text (:func:`canon`; callers reuse flag texts)."""
        if value is None and value_text is None and '"missing"' not in flags:
            raise ValueError("a value-less row must carry flags.missing")
        c = self.cols
        c["indicator"].append(indicator)
        c["concept_id"].append(concept_id)
        c["iso3"].append(iso3)
        c["provider_geo"].append(provider_geo)
        c["entity_qid"].append(entity_qid)
        c["period_start"].append(period[0])
        c["period_end"].append(period[1])
        c["value"].append(value)
        c["value_text"].append(value_text)
        c["unit"].append(unit)
        c["flags"].append(flags)
        c["raw_sha256"].append(raw_sha256)
        c["selector"].append(selector)

    def __len__(self) -> int:
        return len(self.cols["indicator"])

    def frame(self) -> Any:
        import polars as pl

        from reva.atlas.tables import ATLAS_SCHEMA

        n = len(self)
        data = {"source": [self.source] * n, "dataset": [self.dataset] * n, **self.cols}
        return pl.DataFrame(data, schema=ATLAS_SCHEMA).select(ATLAS_SCHEMA.names())


def concept_hints(catalog_file: str, source: str, dataset: str) -> dict[str, str]:
    """Indicator -> concept id for one table, from ``registry/atlas/catalog/<catalog_file>`` (first wins)."""
    from reva.ontology.registry import REGISTRY_ROOT

    path = REGISTRY_ROOT / "atlas" / "catalog" / catalog_file
    if not path.exists():
        return {}
    import yaml

    doc = yaml.safe_load(path.read_text()) or {}
    prefix = f"{source}.{dataset}."
    out: dict[str, str] = {}
    for c in doc.get("concepts") or []:
        for s in c.get("source_order") or []:
            key = s if isinstance(s, str) else f"{s['source']}.{s['dataset']}.{s['indicator']}"
            if key.startswith(prefix):
                out.setdefault(key[len(prefix) :], c["id"])
    return out


def crosswalk_key(code: str | None) -> str | None:
    """``code`` when it is a crosswalk entity key (ISO3, XKX, CHI), else ``None``."""
    from reva.atlas.tables import known_country_keys

    if code is None:
        return None
    c = code.strip().upper()
    return c if c in known_country_keys() else None


def sha256_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


__all__ = [
    "DATAVERSE",
    "S3_HOST",
    "Cell",
    "Fetched",
    "RowBuffer",
    "SocietyParseError",
    "XlsxStream",
    "canon",
    "check_dataverse_record",
    "col_letters",
    "concept_hints",
    "crosswalk_key",
    "dataverse_file_url",
    "dataverse_record_url",
    "day_period",
    "err",
    "fetch_archive",
    "header_map",
    "num",
    "read_csv",
    "sha256_hex",
    "year_period",
]
