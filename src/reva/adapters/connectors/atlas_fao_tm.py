"""FAOSTAT detailed trade matrix (bilateral), bounded to staples -> atlas table (V6, agent V6AGRI).

Source (researched 2026-10-17): FAOSTAT bulk downloads, domain TM "Trade: Detailed trade matrix". The whole-world
file ``Trade_DetailedTradeMatrix_E_All_Data_(Normalized).zip`` is 420 MB (over the V6 raw-archive bound), so this
connector reads the **regional** file ``https://bulks-faostat.fao.org/production/Trade_DetailedTradeMatrix_E_Asia.zip``
(112.5 MB, FAO's own split by *reporter* region; DateUpdate 2025-12-23) through two HTTP byte ranges, like
``atlas_ecoregions`` / ``atlas_wid``:

1. the archive tail (64 KiB: central directory + FAO's small code lists ``*_Elements.csv``, ``*_Flags.csv``,
   ``*_ItemCodes.csv``, ``*_ReporterCountries.csv``, ``*_PartnerCountries.csv``);
2. the wide member ``Trade_DetailedTradeMatrix_E_Asia.csv`` only (57.8 MB compressed, 554 MB inflated): the
   ``_NOFLAG`` copy is never downloaded. The member is inflated as a stream (CRC-32 and size checked against the
   central directory) and filtered to :data:`ITEMS`; nothing else of the 554 MB is kept.

Scope: reporters = the countries FAO files under Asia (India, China, Thailand, Viet Nam, Pakistan, Myanmar,
Cambodia, Japan, Indonesia, the Philippines, Bangladesh, Saudi Arabia, Iran, Türkiye ...), all partners (any
region), items wheat 15, wheat flour 16, rice 27 (paddy) / 28 (husked) / 29 (milled, husked) / 30 (paddy, milled
equivalent) / 31 (milled) / 32 (broken), maize 56, soya beans 236, soya bean oil 237, soya bean cake 238; every
element present (import / export quantity and value), years 1986-2024. Exporters outside Asia (United States,
Brazil, Argentina, Russia, Ukraine, EU) appear only as partners of Asian reporters (mirror flows); their own
reports are in the other regional files (Americas 62.8 MB, Europe 189 MB, Africa 49.8 MB, Oceania 14.1 MB): to
add on a bigger machine (``REGION`` option, same parser).

Cells: ``indicator`` = ``<item code>|<element code>|partner=<FAO partner area code>`` (FAOSTAT codes);
``provider_geo`` = FAO reporter area code; ``iso3`` of the reporter from the row's own M49 code through scheme
``fao_m49`` (``atlas_fao.fao_area_iso3``), the partner's M49 / iso3 in ``flags`` (same rule); aggregates and
"unspecified" partners keep their FAO code only. Values verbatim (strict decimal check), FAO flag letter verbatim
with its meaning from the archived ``*_Flags.csv``; empty values produce no cell (never a zero). The item's CPC
code as FAO publishes it (``Item Code (CPC)``) is kept in ``flags.item_cpc``.

Licence: FAO Statistical Database terms of use, CC BY 4.0 (as ``atlas_fao``). Reported trade is what the
*reporter* declared; partner-side (mirror) flows are not reconciled by REVA.
"""

from __future__ import annotations

import csv
import io
import sys
import zlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from reva.adapters.connectors.agri_common import canon, module_digest
from reva.adapters.connectors.vintage_common import DECIMAL_RE, Rows
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.fao_tm"
# 1.0.1: 1.0.0 was registered in the dev DB by a concurrent `reva seed` from a pre-`ruff format` working copy (other digest).
CONNECTOR_VERSION = "1.0.1"
PARSER_VERSION = "atlas_fao_tm/1"
SOURCE_KEY = "fao.faostat_tm"
SOURCE = "fao_tm"
DATASET = "tm_staples_asia"
REGION = "Asia"
HOST = "bulks-faostat.fao.org"
ARCHIVE_URL = f"https://{HOST}/production/Trade_DetailedTradeMatrix_E_{REGION}.zip"
WIDE_MEMBER = f"Trade_DetailedTradeMatrix_E_{REGION}.csv"
CODE_MEMBERS = {
    "flags": "Trade_DetailedTradeMatrix_E_Flags.csv",
    "items": "Trade_DetailedTradeMatrix_E_ItemCodes.csv",
    "elements": "Trade_DetailedTradeMatrix_E_Elements.csv",
    "reporters": "Trade_DetailedTradeMatrix_E_ReporterCountries.csv",
    "partners": "Trade_DetailedTradeMatrix_E_PartnerCountries.csv",
}
TAIL_BYTES = 65_536
MAX_WIDE_COMPRESSED = 80_000_000
MAX_WIDE_INFLATED = 1_500_000_000
CHUNK = 4 * 1024 * 1024
LICENCE = "CC BY 4.0 (FAO Statistical Database terms of use)"
TERMS_URL = "https://www.fao.org/contact-us/terms/db-terms-of-use/en"
#: FAOSTAT item codes (TM ItemCodes.csv): wheat & flour, rice (all forms), maize, soya beans / oil / cake.
ITEMS = frozenset({"15", "16", "27", "28", "29", "30", "31", "32", "56", "236", "237", "238"})
FIXED = (
    "Reporter Country Code",
    "Reporter Country Code (M49)",
    "Reporter Countries",
    "Partner Country Code",
    "Partner Country Code (M49)",
    "Partner Countries",
    "Item Code",
    "Item Code (CPC)",
    "Item",
    "Element Code",
    "Element",
    "Unit",
)


class TmError(SourceResponseError):
    def __init__(self, msg: str, kind: str = "schema_drift") -> None:
        super().__init__(msg, retryable=False, kind=kind)


@dataclass
class TmReport:
    member: str = WIDE_MEMBER
    inflated_bytes: int = 0
    lines: int = 0
    rows_in_scope: int = 0
    cells: int = 0
    empty_values: int = 0
    years: list[int] = field(default_factory=list)
    flags: dict[str, int] = field(default_factory=dict)
    items: dict[str, str] = field(default_factory=dict)
    elements: dict[str, str] = field(default_factory=dict)
    reporters: dict[str, str] = field(default_factory=dict)
    unmapped_reporters: list[str] = field(default_factory=list)
    unmapped_partners: list[str] = field(default_factory=list)


def _csv_rows(data: bytes) -> list[list[str]]:
    text = data.decode("utf-8-sig", errors="strict")
    return [[c.strip() for c in r] for r in csv.reader(io.StringIO(text, newline="")) if r]


def code_lists(members: dict[str, bytes]) -> dict[str, Any]:
    """FAO's code lists from the archive (flag meanings, item names + CPC, elements, area definitions)."""
    flags = {r[0]: r[1] for r in _csv_rows(members["flags"])[1:] if len(r) >= 2}
    items = {r[0]: (r[1].lstrip("'"), r[2]) for r in _csv_rows(members["items"])[1:] if len(r) >= 3}
    missing = sorted(ITEMS - set(items))
    if missing:
        raise TmError(f"items {missing} are not in {CODE_MEMBERS['items']} any more")
    reporters = {r[0]: (r[1].lstrip("'"), r[2]) for r in _csv_rows(members["reporters"])[1:] if len(r) >= 3}
    partners = {r[0]: (r[1].lstrip("'"), r[2]) for r in _csv_rows(members["partners"])[1:] if len(r) >= 3}
    return {"flags": flags, "items": items, "reporters": reporters, "partners": partners}


def iter_lines(body: bytes, served_start: int, member: Any) -> Any:
    """Yield the member's lines (bytes, without line end) by streaming inflate; CRC-32 / size checked at the end."""
    import struct

    if served_start != member.offset:
        raise TmError(f"{member.name}: range starts at {served_start}, member at {member.offset}")
    if body[:4] != b"PK\x03\x04" or len(body) < 30:
        raise TmError(f"{member.name}: no local file header at the member offset")
    (_s, _v, flags, method, _t, _d, _crc, _cs, _us, fnl, exl) = struct.unpack("<IHHHHHIIIHH", body[:30])
    name = body[30 : 30 + fnl].decode("utf-8", "strict")
    if name != member.name or method != 8 or flags & 0x1:
        raise TmError(f"{member.name}: local header {name!r} method {method} flags {flags}")
    start = 30 + fnl + exl
    data = memoryview(body)[start : start + member.csize]
    if len(data) != member.csize:
        raise TmError(f"{member.name}: range holds {len(data)} of {member.csize} compressed bytes")
    if member.usize > MAX_WIDE_INFLATED:
        raise TmError(f"{member.name}: {member.usize} bytes inflated exceeds the bound", kind="too_large")
    d = zlib.decompressobj(-15)
    crc, total, carry = 0, 0, b""
    for i in range(0, len(data), CHUNK):
        try:
            out = d.decompress(bytes(data[i : i + CHUNK]))
        except zlib.error as exc:
            raise TmError(f"{member.name}: corrupt deflate stream ({exc})") from exc
        crc, total = zlib.crc32(out, crc), total + len(out)
        if total > MAX_WIDE_INFLATED:
            raise TmError(f"{member.name}: inflates beyond the bound", kind="too_large")
        buf = carry + out
        parts = buf.split(b"\n")
        carry = parts.pop()
        for p in parts:
            yield p.rstrip(b"\r")
    out = d.flush()
    crc, total = zlib.crc32(out, crc), total + len(out)
    buf = carry + out
    for p in buf.split(b"\n"):
        if p.strip():
            yield p.rstrip(b"\r")
    if total != member.usize or (crc & 0xFFFFFFFF) != member.crc:
        raise TmError(f"{member.name}: size/CRC-32 mismatch with the central directory")


def parse_wide(lines: Any, codes: dict[str, Any], raw_sha256: str, rep: TmReport) -> Any:
    """Wide CSV lines -> atlas frame (one cell per non-empty year value of an in-scope row)."""
    from reva.adapters.connectors.atlas_fao import fao_area_iso3

    it = iter(lines)
    header = next(it, None)
    if header is None:
        raise TmError(f"{WIDE_MEMBER}: empty member")
    head = next(csv.reader([header.decode("utf-8-sig")]))
    if tuple(head[: len(FIXED)]) != FIXED:
        raise TmError(f"{WIDE_MEMBER}: header changed: {head[: len(FIXED)]}")
    years: list[tuple[int, int, int]] = []  # (year, value column, flag column)
    for j in range(len(FIXED), len(head), 2):
        ycol, fcol = head[j], head[j + 1] if j + 1 < len(head) else ""
        if not (ycol.startswith("Y") and ycol[1:].isdigit() and fcol == f"{ycol}F"):
            raise TmError(f"{WIDE_MEMBER}: unexpected year columns {ycol!r} {fcol!r}")
        years.append((int(ycol[1:]), j, j + 1))
    rep.years = [y for y, _, _ in years]
    needles = tuple(f'"{c}","\''.encode() for c in sorted(ITEMS))
    meanings: dict[str, str] = codes["flags"]
    rows = Rows()
    flag_count: Counter[str] = Counter()
    partner_iso: dict[str, str | None] = {}
    unm_r: set[str] = set()
    unm_p: set[str] = set()
    for n, line in enumerate(it, start=2):
        rep.lines += 1
        if not any(nd in line for nd in needles):
            continue
        r = next(csv.reader([line.decode("utf-8")]))
        if len(r) != len(head):
            raise TmError(f"{WIDE_MEMBER}: line {n} has {len(r)} fields for {len(head)}")
        if r[6] not in ITEMS:
            continue
        rep.rows_in_scope += 1
        rcode, rm49, rname, pcode, pm49, pname, item, cpc, iname, elem, ename, unit = (c.strip() for c in r[:12])
        rm49, pm49, cpc = rm49.lstrip("'"), pm49.lstrip("'"), cpc.lstrip("'")
        rep.items.setdefault(item, iname)
        rep.elements.setdefault(elem, f"{ename} ({unit})")
        rep.reporters.setdefault(rcode, rname)
        riso = fao_area_iso3(rm49)
        if riso is None:
            unm_r.add(f"{rcode} {rname}")
        if pcode not in partner_iso:
            partner_iso[pcode] = fao_area_iso3(pm49)
            if partner_iso[pcode] is None:
                unm_p.add(f"{pcode} {pname}")
        for y, vj, fj in years:
            v = r[vj].strip()
            if not v:
                rep.empty_values += 1 if r[fj].strip() else 0
                continue
            if not DECIMAL_RE.match(v):
                raise TmError(f"{WIDE_MEMBER}: line {n} {y}: value {v[:30]!r} is not a number")
            fl = r[fj].strip()
            flag_count[fl] += 1
            fobj: dict[str, Any] = {
                "flag": fl,
                "flag_meaning": meanings.get(fl, "no flag in the file" if not fl else "not listed in Flags.csv"),
                "item": iname,
                "item_cpc": cpc,
                "element": ename,
                "reporter": rname,
                "reporter_m49": rm49,
                "partner": pname,
                "partner_m49": pm49,
                "partner_iso3": partner_iso[pcode],
                "flow_basis": "as declared by the reporter (mirror flows not reconciled)",
                "regional_file": REGION,
            }
            if fl == "E":
                fobj["estimate_kind"] = "estimated"
            elif fl == "I":
                fobj["estimate_kind"] = "imputed"
            rows.add(
                source=SOURCE,
                dataset=DATASET,
                indicator=f"{item}|{elem}|partner={pcode}",
                provider_geo=rcode,
                iso3=riso,
                start=date(y, 1, 1),
                end=date(y + 1, 1, 1),
                value=v,
                unit=unit or "not stated by FAOSTAT",
                flags=canon(fobj),
                raw_sha256=raw_sha256,
                selector={"zip": ARCHIVE_URL.rsplit("/", 1)[1], "file": WIDE_MEMBER, "row": n, "column": f"Y{y}"},
            )
    rep.cells = len(rows)
    rep.flags = dict(flag_count)
    rep.unmapped_reporters = sorted(unm_r)
    rep.unmapped_partners = sorted(unm_p)
    if not rep.cells:
        raise TmError(f"{WIDE_MEMBER}: no cell in scope")
    return rows.frame()


def rights() -> dict[str, Any]:
    return {
        "licence": "CC-BY-4.0",
        "licence_url": TERMS_URL,
        "attribution": "FAO. FAOSTAT: Trade - Detailed trade matrix. Licence: CC-BY-4.0.",
        "provider": "FAO (FAOSTAT)",
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
        provider="Food and Agriculture Organization of the United Nations (FAO), FAOSTAT detailed trade matrix",
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=(DATASET,),
        dataset_ids=("TM",),
        cadence_seconds=90 * 86_400,
        planned_requests=2,
        max_response_bytes=MAX_WIDE_COMPRESSED,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"atlas_tables": [f"{SOURCE}.{DATASET}"], "items": sorted(ITEMS, key=int), "region": REGION},
        notes="Two byte ranges of the FAOSTAT TM Asia zip (tail + wide member, ~58 MB); staples only.",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    from reva.adapters.connectors.atlas_wid import content_range, inflate_member, parse_central_directory

    if not run.wants(DATASET):
        return {"tables": {}}
    tail = await run.get_range(ARCHIVE_URL, key="tm:tail", suffix=TAIL_BYTES)
    t_start, _e, size = content_range(tail)
    etag = (tail.response_metadata.get("headers") or {}).get("etag")
    members, implied = parse_central_directory(tail.body, t_start)
    if implied != size:
        raise TmError(f"central directory implies {implied} bytes, server says {size}")
    by_name = {m.name: m for m in members}
    small: dict[str, bytes] = {}
    for key, name in CODE_MEMBERS.items():
        m = by_name.get(name)
        if m is None:
            raise TmError(f"{name} is not in the archive")
        if m.offset < t_start:
            raise TmError(f"{name} is outside the fetched tail (increase TAIL_BYTES)")
        lo = m.offset - t_start
        small[key] = inflate_member(tail.body[lo:], m.offset, m)
    codes = code_lists(small)
    wide = by_name.get(WIDE_MEMBER)
    if wide is None:
        raise TmError(f"{WIDE_MEMBER} is not in the archive ({sorted(by_name)})")
    if wide.csize > MAX_WIDE_COMPRESSED - 4096:
        raise TmError(f"{WIDE_MEMBER} is {wide.csize} bytes compressed (cap {MAX_WIDE_COMPRESSED})", kind="too_large")
    hi = wide.offset + 30 + wide.name_len + wide.extra_len + 1024 + wide.csize - 1
    part = await run.get_range(
        ARCHIVE_URL,
        key="tm:wide",
        start=wide.offset,
        end=min(hi, size - 1),
        if_range=etag,
        params={"member": WIDE_MEMBER},
    )
    served, _e2, _s2 = content_range(part)
    tail_sha, wide_sha = run.archive(tail), run.archive(part)
    rep = TmReport(inflated_bytes=wide.usize)
    frame = parse_wide(iter_lines(part.body, served, wide), codes, wide_sha, rep)
    lm = (part.response_metadata.get("headers") or {}).get("last-modified")
    del part
    note = (
        f"FAOSTAT TM 'Detailed trade matrix', regional file {ARCHIVE_URL} (reporters in FAO's Asia region; ETag "
        f"{etag}, Last-Modified {lm}); member {WIDE_MEMBER} read by byte range and stream-inflated (CRC checked). "
        f"Items {sorted(ITEMS, key=int)}; {rep.cells} cells from {rep.rows_in_scope} rows in scope ({rep.lines} lines); "
        f"years {min(rep.years)}-{max(rep.years)}; flags {rep.flags}. Reporter-declared flows; partner = FAO area code "
        f"in the indicator (M49/iso3 in flags). Unmapped reporters {rep.unmapped_reporters}; {len(rep.unmapped_partners)}"
        f" unmapped partner areas (aggregates/historical/unspecified). Licence CC BY 4.0, cite: FAO. FAOSTAT: Trade - "
        f"Detailed trade matrix. {TERMS_URL}"
    )[:4000]
    reg = run.register(
        dataset=DATASET, frame=frame, raw_sha256s=[wide_sha, tail_sha], coverage_note=note, rights=rights()
    )
    return {
        "tables": {
            DATASET: {
                "dataset_key": reg["dataset_key"],
                "rows": reg["row_count"],
                "created": reg["created"],
                "countries": reg.get("coverage", {}).get("countries"),
            }
        },
        "report": {k: v for k, v in rep.__dict__.items() if k not in ("reporters",)},
        "archive_bytes": size,
        "wide_compressed_bytes": wide.csize,
    }


__all__ = [
    "CONNECTOR_NAME",
    "ITEMS",
    "TmError",
    "atlas_ingest",
    "atlas_source",
    "code_lists",
    "iter_lines",
    "parse_wide",
]
