"""Sanctions between countries as dated, coded episodes, country-pair rows and regime snapshots (agent A-SANCTIONS).

Sources (``docs/dev/v3_a_sanctions.md`` has the licence/access check of every candidate)
-----------------------------------------------------------------------------------------
* **TIES 4.1** -- Threat and Imposition of Economic Sanctions, 1945-2005 (Morgan, Bapat & Kobayashi 2014,
  CMPS 31(5)), ``TIESv4-1.xls`` from ``sanctions.web.unc.edu`` (public download, no form; no licence
  stated, citation requested -> ``open_licence: false``). One row per case (sender(s) -> one target):
  threat / imposition stages with dates, issues (sender objectives as coded), threatened and imposed
  sanction types, institution codes and the coded **final outcome**. The five corrections of
  ``TIESCorrection.txt`` are already in v4.1 (checked 2026-10-16 against the file; see the doc).
* **EUSANCT 1.0** (case level) -- sanction threats and impositions by the EU, the US and the UN,
  1989-2015 (Weber & Schneider 2020, CMPS), RADAR/KonDATA dataset DOI 10.48606/10, **CC BY 4.0**.
  Per-sender (EU/US/UN) start/end and imposition dates, issues, sanction types, coded final outcome,
  threat/sanction success and the Hufbauer-Schott-Elliott (HSE) policy result x sanction contribution.
  RADAR serves the dataset as one tar (64.8 MB, generated on request: header mtimes change, so each
  download has its own sha256); only the case-level CSV member is parsed.
* **EU Sanctions Map** (``www.sanctionsmap.eu/api/v1/regime``) -- the regimes in force in the EU on the
  retrieval day (EU autonomous, UN, "UN and EU"): country the regime is listed under (not necessarily
  the target State), measure types, legal-act references, last amendment and expiry dates. Regime-level
  only: designation lists (``/api/v1/fsd``, persons/entities) are **never** requested. No licence is
  stated on the site (disclaimer only); the facts are references to EU/UN official acts.
* **US OFAC "Sanctions Programs and Country Information"** page -- the list of active programs and the
  date each was last updated (US Government work, public domain, 17 U.S.C. 105). Program level only:
  the SDN / consolidated lists (individuals and entities) are **never** requested. A program is tied to a
  country only when its title names exactly one country (``registry/atlas/geo_aliases/a_sanctions.yaml``,
  scheme ``ofac_program``, each alias citing the title).

Not used: the Global Sanctions Data Base (data only through a request form asking name, e-mail and
affiliation, with a no-redistribution clause); the UN Security Council consolidated list (individual- and
entity-level designations: personal data; UN regimes come through the EU map's UN regimes with their
S/RES references); the UN SC sanctions pages (HTTP 202 empty bot challenge on 2026-10-16).

Outputs
-------
* Atlas tables (computed counts, flagged ``computed_from``; a missing row is "no coded case", never 0):
  ``ties.dyad_year`` / ``eusanct.dyad_year`` -- row = sender, ``|partner=<target ISO3>`` (or
  ``|partner_code=cow:<n>`` when the target does not map), ``flags.partner_iso3``:
  ``CASES_STARTED``, ``CASES_ACTIVE``, ``IMPOSITIONS_ACTIVE`` (distinct case ids, case ids in flags);
  ``ties.country_year`` / ``eusanct.country_year``: ``TARGET_CASES_STARTED``, ``TARGET_CASES_ACTIVE``,
  ``TARGET_IMPOSITIONS_ACTIVE``, ``SENDER_CASES_STARTED``;
  ``eusanctionsmap.regimes`` (``REGIME|regime=<id>`` value_text = adopted by; ``REGIMES_LISTED`` count);
  ``ofac.programs`` (``PROGRAM|program=<slug>``; row USA ``ACTIVE_PROGRAMS|partner=<ISO3>``).
* KG (when the ``kg`` schema exists): one ``sanction_episode`` entity per TIES / EUSANCT case with
  senders, target, stage dates, issues, types, outcome (all *context* statements, none causal); one
  ``coded_outcome`` entity per coded outcome; EUSANCT ``same_case_as`` TIES only on a shared case id +
  EUSANCT's own ``datasource`` naming TIES + the same target code; ``sanction_regime`` /
  ``sanction_program`` entities.
* T1 causal claims (``kg.causal_claim`` through CAUSCORE's ``reva.causal.load_dataset_claims``, codebook
  definitions quoted verbatim, :func:`causal_claim_records` / :func:`deliver_claims`): ``coded_outcome``
  case -> its coded final outcome (TIES variable 39, EUSANCT variable 30) and, EUSANCT only,
  ``contributing_factor`` (``promotes``) case -> the sender-goals record where the HSE success score is >= 9
  (variable 36: "implies that the sanction made a substantial contribution to the goals of the sender").
  **No ``reaction_to`` / ``precipitated`` claim is produced**: neither codebook codes a triggering event
  (TIES/EUSANCT issue categories name a *kind* of target behaviour, never a dated event), and the TIES
  threat -> imposition sequence is two stages of one case (context, not causation).

Free-text fields that may name people (TIES ``threatid*`` coder notes, EUSANCT ``*_id``, ``*_other``,
``otherissue``, ``comments``, ``link``) are not parsed into any output.
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import math
import re
import struct
import tarfile
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from reva.adapters.connectors.atlas_geopol_common import (
    COUNTRYCODE_VERSION,
    KgEntity,
    KgStatement,
    StateCodeCrosswalk,
    StateEntityResolver,
    atlas_frame,
    code_correspondence,
    fetch_countrycode,
    kg_available,
    load_kg,
    load_qid_for_iso3,
)
from reva.kernel.errors import SourceResponseError

PARSER_VERSION = "atlas-sanctions-parse-1"
CONNECTOR_VERSION = "1.0.0"


class SanctionsParseError(SourceResponseError):
    pass


def _fail(msg: str, kind: str = "schema_drift") -> SanctionsParseError:
    return SanctionsParseError(msg, retryable=False, kind=kind)


# =====================================================================================================
# Minimal, bounded reader of a legacy Excel 97-2003 (.xls, BIFF8) workbook: first worksheet, cell text.
# (No xls reader is a project dependency; validated cell-for-cell against xlrd on TIES 4.1 and the
# EUSANCT case-level workbook: 0 differences.)
# =====================================================================================================

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ENDOFCHAIN, _FREESECT = 0xFFFFFFFE, 0xFFFFFFFF
MAX_XLS_BYTES = 20_000_000
MAX_XLS_CELLS = 2_000_000


def _ole_stream(data: bytes, names: tuple[str, ...] = ("Workbook", "Book")) -> bytes:
    if len(data) < 512 or data[:8] != _OLE_MAGIC:
        raise _fail("not an OLE2 compound file (.xls)")
    if len(data) > MAX_XLS_BYTES:
        raise _fail("workbook too large", "too_large")
    ssz = 1 << struct.unpack_from("<H", data, 0x1E)[0]
    if ssz not in (512, 4096):
        raise _fail(f"unsupported OLE sector size {ssz}")
    n_fat, first_dir = struct.unpack_from("<II", data, 0x2C)
    mini_cutoff = struct.unpack_from("<I", data, 0x38)[0]
    first_difat, n_difat = struct.unpack_from("<II", data, 0x44)
    nsec = (len(data) - 512) // ssz

    def sector(i: int) -> bytes:
        if i >= nsec:
            raise _fail("OLE sector index out of range")
        off = 512 + i * ssz
        return data[off : off + ssz]

    difat = list(struct.unpack_from("<109I", data, 0x4C))
    s, seen = first_difat, 0
    while s not in (_ENDOFCHAIN, _FREESECT) and seen < n_difat:
        vals = struct.unpack(f"<{ssz // 4}I", sector(s))
        difat.extend(vals[:-1])
        s, seen = vals[-1], seen + 1
    fat: list[int] = []
    for fs in [x for x in difat if x not in (_FREESECT, _ENDOFCHAIN)][:n_fat]:
        fat.extend(struct.unpack(f"<{ssz // 4}I", sector(fs)))

    def chain(start: int) -> bytes:
        out, cur, steps = [], start, 0
        while cur not in (_ENDOFCHAIN, _FREESECT):
            if cur >= len(fat) or steps > nsec:
                raise _fail("broken OLE sector chain")
            out.append(sector(cur))
            cur, steps = fat[cur], steps + 1
        return b"".join(out)

    directory = chain(first_dir)
    for off in range(0, len(directory) - 127, 128):
        e = directory[off : off + 128]
        nlen = struct.unpack_from("<H", e, 64)[0]
        name = e[: max(0, nlen - 2)].decode("utf-16-le", "replace")
        start, size = struct.unpack_from("<II", e, 116)
        if e[66] == 2 and name in names:
            if size < mini_cutoff:
                raise _fail("workbook stream stored in the OLE mini stream (unsupported)")
            body = chain(start)
            if len(body) < size:
                raise _fail("truncated workbook stream", "incomplete")
            return body[:size]
    raise _fail("no Workbook stream in the .xls file")


def _records(stream: bytes, start: int = 0) -> Iterator[tuple[int, bytes]]:
    pos, n = start, len(stream)
    while pos + 4 <= n:
        rtype, rlen = struct.unpack_from("<HH", stream, pos)
        yield rtype, stream[pos + 4 : pos + 4 + rlen]
        pos += 4 + rlen


def _rk(v: int) -> float:
    if v & 2:
        num = float((v if v < 0x80000000 else v - (1 << 32)) >> 2)
    else:
        num = struct.unpack("<d", struct.pack("<Q", (v & 0xFFFFFFFC) << 32))[0]
    return num / 100.0 if v & 1 else num


def _num_text(x: float) -> str:
    if math.isnan(x) or math.isinf(x):
        raise _fail("non-finite number cell")
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))
    return repr(x)


def _sst(chunks: list[bytes]) -> list[str]:
    """Shared strings of an SST record + its CONTINUE records (BIFF8: a string's characters may continue in
    the next record, which then starts with a 1-byte option flag; headers never straddle records)."""
    ci, pos = 0, 8
    total = struct.unpack_from("<I", chunks[0], 4)[0]
    out: list[str] = []

    def take(k: int) -> bytes:
        nonlocal ci, pos
        if pos >= len(chunks[ci]):
            ci, pos = ci + 1, 0
            if ci >= len(chunks):
                raise _fail("truncated SST", "incomplete")
        b = chunks[ci][pos : pos + k]
        if len(b) != k:
            raise _fail("SST field split across records")
        pos += k
        return b

    for _ in range(total):
        nchar = struct.unpack("<H", take(2))[0]
        flags = take(1)[0]
        nrich = struct.unpack("<H", take(2))[0] if flags & 0x08 else 0
        ext = struct.unpack("<I", take(4))[0] if flags & 0x04 else 0
        wide = flags & 0x01
        parts: list[str] = []
        left = nchar
        while left > 0:
            per = 2 if wide else 1
            k = min(left, (len(chunks[ci]) - pos) // per)
            raw = chunks[ci][pos : pos + k * per]
            parts.append(raw.decode("utf-16-le") if wide else raw.decode("latin-1"))
            pos += k * per
            left -= k
            if left > 0:
                ci, pos = ci + 1, 1
                if ci >= len(chunks):
                    raise _fail("truncated SST string", "incomplete")
                wide = chunks[ci][0] & 0x01
        skip = 4 * nrich + ext
        while skip > 0:
            if pos >= len(chunks[ci]):
                ci, pos = ci + 1, 0
                if ci >= len(chunks):
                    raise _fail("truncated SST formatting runs", "incomplete")
            k = min(skip, len(chunks[ci]) - pos)
            pos += k
            skip -= k
        out.append("".join(parts))
    return out


def _xl_string(b: bytes, off: int, len_bytes: int = 2) -> str:
    nchar = struct.unpack_from("<H", b, off)[0] if len_bytes == 2 else b[off]
    p = off + len_bytes
    flags = b[p]
    p += 1
    if len_bytes == 2:
        if flags & 0x08:
            p += 2
        if flags & 0x04:
            p += 4
    return b[p : p + nchar * 2].decode("utf-16-le") if flags & 1 else b[p : p + nchar].decode("latin-1")


def read_xls_first_sheet(data: bytes) -> tuple[str, list[list[str | None]]]:
    """(sheet name, rows of cell text) of the first worksheet. Numbers are formatted without a trailing
    ``.0`` when integral; empty cells are ``None``; formulas give their cached result (never evaluated)."""
    stream = _ole_stream(data)
    sst: list[str] = []
    sheets: list[tuple[int, str]] = []
    sst_chunks: list[bytes] | None = None
    for rtype, body in _records(stream):
        if sst_chunks is not None:
            if rtype == 0x003C:  # CONTINUE
                sst_chunks.append(body)
                continue
            sst, sst_chunks = _sst(sst_chunks), None
        if rtype == 0x0085 and body[5] == 0:  # BOUNDSHEET8, worksheet
            sheets.append((struct.unpack_from("<I", body, 0)[0], _xl_string(body, 6, 1)))
        elif rtype == 0x00FC:  # SST
            sst_chunks = [body]
        elif rtype == 0x000A:  # EOF of the globals substream
            break
    if sst_chunks is not None:
        sst = _sst(sst_chunks)
    if not sheets:
        raise _fail("no worksheet in the .xls file")
    cells: dict[tuple[int, int], str] = {}
    pending: tuple[int, int] | None = None
    for rtype, b in _records(stream, sheets[0][0]):
        if rtype == 0x000A:
            break
        if len(cells) > MAX_XLS_CELLS:
            raise _fail("too many cells", "too_large")
        if rtype == 0x00FD:  # LABELSST
            r, c, _xf, i = struct.unpack_from("<HHHI", b, 0)
            if i >= len(sst):
                raise _fail("SST index out of range")
            cells[(r, c)] = sst[i]
        elif rtype == 0x0203:  # NUMBER
            r, c, _xf = struct.unpack_from("<HHH", b, 0)
            cells[(r, c)] = _num_text(struct.unpack_from("<d", b, 6)[0])
        elif rtype == 0x027E:  # RK
            r, c, _xf, v = struct.unpack_from("<HHHI", b, 0)
            cells[(r, c)] = _num_text(_rk(v))
        elif rtype == 0x00BD:  # MULRK
            r, c0 = struct.unpack_from("<HH", b, 0)
            c1 = struct.unpack_from("<H", b, len(b) - 2)[0]
            for k in range(c1 - c0 + 1):
                cells[(r, c0 + k)] = _num_text(_rk(struct.unpack_from("<I", b, 4 + 6 * k + 2)[0]))
        elif rtype == 0x0204:  # LABEL
            r, c, _xf = struct.unpack_from("<HHH", b, 0)
            cells[(r, c)] = _xl_string(b, 6)
        elif rtype == 0x0205:  # BOOLERR
            r, c, _xf, v, is_err = struct.unpack_from("<HHHBB", b, 0)
            if not is_err:
                cells[(r, c)] = "TRUE" if v else "FALSE"
        elif rtype == 0x0006:  # FORMULA (cached result)
            r, c, _xf = struct.unpack_from("<HHH", b, 0)
            res = b[6:14]
            if res[6:8] == b"\xff\xff":
                if res[0] == 0:
                    pending = (r, c)
                elif res[0] == 1:
                    cells[(r, c)] = "TRUE" if res[2] else "FALSE"
            else:
                cells[(r, c)] = _num_text(struct.unpack("<d", res)[0])
        elif rtype == 0x0207 and pending is not None:  # STRING (formula string result)
            cells[pending] = _xl_string(b, 0)
            pending = None
    if not cells:
        return sheets[0][1], []
    nrows = max(r for r, _ in cells) + 1
    ncols = max(c for _, c in cells) + 1
    out: list[list[str | None]] = [[None] * ncols for _ in range(nrows)]
    for (r, c), v in cells.items():
        out[r][c] = v if v.strip() != "" else None
    return sheets[0][1], out


# =====================================================================================================
# Codebook tables (verbatim category names; the doc quotes the definitions)
# =====================================================================================================

#: TIES 4 users' manual (June 2013), variable 12 "Issue".
TIES_ISSUES = {
    1: "Contain Political Influence",
    2: "Contain Military Behavior",
    3: "Destabilize Regime",
    4: "Release Citizens, Property, or Material",
    5: "Solve Territorial Dispute",
    6: "Deny Strategic Materials",
    7: "Retaliate for Alliance or Alignment Choice",
    8: "Improve Human Rights",
    9: "End Weapons/Materials Proliferation",
    10: "Terminate Support of Non-State Actors",
    11: "Deter or Punish Drug Trafficking Practices",
    12: "Improve Environmental Policies",
    13: "Trade Practices",
    14: "Implement Economic Reform",
    15: "Other",
}
#: TIES variable 16 "Sanction Type Threatened".
TIES_THREAT_TYPES = {
    1: "Unspecific",
    2: "Total Economic Embargo",
    3: "Partial Economic Embargo",
    4: "Import Restriction",
    5: "Export Restriction",
    6: "Blockade",
    7: "Asset Freeze",
    8: "Termination of Foreign Aid",
    9: "Travel Ban",
    10: "Suspension of Economic Agreement/Protocol",
}
#: TIES variable 30 "Sanction Type" (imposed).
TIES_IMPOSED_TYPES = {
    1: "Total Economic Embargo",
    2: "Partial Economic Embargo",
    3: "Import Restriction",
    4: "Export Restriction",
    5: "Blockade",
    6: "Asset Freeze",
    7: "Termination of Foreign Aid",
    8: "Travel Ban",
    9: "Suspension of Economic Agreement",
    10: "Other",
}
#: TIES variable 39 "Final Outcome" (stage: threat / imposition).
TIES_OUTCOMES = {
    1: ("Partial Acquiescence by Target to threat", "threat"),
    2: ("Complete Acquiescence by Target to threat", "threat"),
    3: ("Capitulation by the Sender(s) in threat stage", "threat"),
    4: ("Stalemate in the Threat Stage", "threat"),
    5: ("Negotiated Settlement", "threat"),
    6: ("Partial Acquiescence by the Target State following sanctions imposition", "imposition"),
    7: ("Total Acquiescence by Target State following sanctions imposition", "imposition"),
    8: ("Capitulation by Sender after Imposition", "imposition"),
    9: ("Stalemate after Sanctions Imposition", "imposition"),
    10: ("Negotiated Settlement following sanctions imposition", "imposition"),
}
TIES_COSTS = {1: "Minor", 2: "Major", 3: "Severe"}

#: EUSANCT case-level codebook v2.0 (March 2020), variable 15 "Issue".
EUSANCT_ISSUES = {
    1: "Contain Political Influence",
    2: "Contain Military Behaviour",
    3: "Leadership Change",
    4: "Release Citizens, Property, or Material",
    5: "Solve Territorial Dispute",
    6: "Deny Strategic Materials",
    7: "Retaliate for Alliance or Alignment Choice",
    8: "Improve Human Rights",
    9: "End Weapons/Materials Proliferation",
    10: "Terminate Support of Non-State Actors",
    11: "Deter or Punish Drug Trafficking Practices and Corruption",
    12: "Violation of Constitutional Order",
    13: "Fraud Elections",
    14: "Other",
}
#: EUSANCT variables 19 (threatened, 1-13) and 24 (imposed, 1-13 incl. 7b; 13 = Other when imposed).
EUSANCT_TYPES = {
    "1": "Total Economic Embargo",
    "2": "Partial Economic Embargo",
    "3": "Import Restriction",
    "4": "Export Restriction",
    "5": "Blockade",
    "6": "Major Financial Sanction",
    "7": "Targeted Financial Sanction",
    "7b": "Targeted Financial Sanction not only against individuals but against whole entities or companies",
    "8": "Termination of Foreign Aid",
    "9": "Travel Ban",
    "10": "Suspension of Economic Agreement and Exclusion from Regional or International Organizations",
    "11": "Diplomatic Sanction",
    "12": "Arms Embargo",
}
EUSANCT_TYPE_13 = {"threat": "Unspecific", "imposition": "Other"}
#: EUSANCT variable 30 "Final Outcome".
EUSANCT_OUTCOMES = {
    1: ("Partial Acquiescence by Target to Threat", "threat"),
    2: ("Complete Acquiescence by Target to Threat", "threat"),
    3: ("Capitulation by the Sender in Threat Stage", "threat"),
    4: ("Stalemate in the Threat Stage", "threat"),
    5: ("Negotiated Settlement", "threat"),
    6: ("Partial Acquiescence by the Target State Following Sanction Imposition", "imposition"),
    7: ("Total Acquiescence by Target State Following Sanction Imposition", "imposition"),
    8: ("Capitulation by the Sender after Imposition", "imposition"),
    9: ("Stalemate after Sanction Imposition", "imposition"),
    10: ("Negotiated Settlement Following Sanction Imposition", "imposition"),
    11: ("Sanction No Longer Applicable", "other"),
}
EUSANCT_MULTI = {1: "Unilateral Sanction", 2: "Multilateral Sanction", 3: "Universal Sanction"}
#: EUSANCT variables 34-36, after Hufbauer, Schott & Elliott (2009: 49-50).
HSE_POLICY_RESULT = {
    1: "Failed Outcome",
    2: "Unclear but Possibly Positive Outcome",
    3: "Positive Outcome",
    4: "Successful Outcome",
}
HSE_CONTRIBUTION = {
    1: "Negative Contribution",
    2: "Little or No Contribution",
    3: "Substantial Contribution",
    4: "Decisive Contribution",
}

#: REVA grouping of the codebooks' sanction-type categories into families (a lookup, documented in
#: docs/dev/v3_a_sanctions.md; the provider category is always kept next to it).
TIES_THREAT_FAMILY = {
    1: "unspecific",
    2: "trade",
    3: "trade",
    4: "trade",
    5: "trade",
    6: "trade",
    7: "financial",
    8: "aid",
    9: "travel",
    10: "economic_agreement",
}
TIES_IMPOSED_FAMILY = {
    1: "trade",
    2: "trade",
    3: "trade",
    4: "trade",
    5: "trade",
    6: "financial",
    7: "aid",
    8: "travel",
    9: "economic_agreement",
    10: "other",
}
EUSANCT_FAMILY = {
    "1": "trade",
    "2": "trade",
    "3": "trade",
    "4": "trade",
    "5": "trade",
    "6": "financial",
    "7": "financial",
    "7b": "financial",
    "8": "aid",
    "9": "travel",
    "10": "economic_agreement",
    "11": "diplomatic",
    "12": "arms",
}

ORG_EU = "sanctions:org:eu"
ORG_UN = "sanctions:org:un"
ORG_LABELS = {
    ORG_EU: (
        "European Union / European Communities as a collective sanctions sender",
        "EEC/EU acting collectively as coded by TIES (sender/target code 1000) and EUSANCT (sender 'EU': the "
        "European Union and/or the European Community prior to 1993)",
    ),
    ORG_UN: ("United Nations as a sanctions sender", "United Nations as coded by EUSANCT (sender 'UN')"),
}
#: TIES / EUSANCT use the non-COW code 1000 for the EEC/EU acting collectively (TIES manual, variable 5).
EEC_EU_CODE = 1000


# =====================================================================================================
# Small parsing helpers
# =====================================================================================================


def _int(v: Any) -> int | None:
    s = "" if v is None else str(v).strip()
    if s in ("", ".", "NA"):
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    if f != int(f):
        return None
    return int(f)


def _codes(v: Any) -> list[str]:
    """Multi-valued code cell ("2 7", "8;12", "3") -> code strings in source order, unique."""
    s = "" if v is None else str(v).strip()
    if s in ("", "."):
        return []
    out: list[str] = []
    for part in re.split(r"[\s;,]+", s):
        p = part.strip()
        if p.endswith(".0"):
            p = p[:-2]
        if p and p != "." and p not in out:
            out.append(p)
    return out


@dataclass(frozen=True)
class Day:
    """A date with Wikibase precision (11 day, 10 month, 9 year)."""

    year: int
    month: int = 1
    day: int = 1
    precision: int = 9

    @property
    def iso(self) -> str:
        return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"

    @property
    def mid(self) -> date:
        """The date used to resolve a state code (the day itself, 1 July for a year)."""
        if self.precision == 9:
            return date(self.year, 7, 1)
        if self.precision == 10:
            return date(self.year, self.month, 15)
        return date(self.year, self.month, self.day)


def _window(d: Day) -> tuple[str, str]:
    """(first day, last day) of a date at its precision, ISO strings."""
    if d.precision == 9:
        return f"{d.year:04d}-01-01", f"{d.year:04d}-12-31"
    if d.precision == 10:
        return f"{d.year:04d}-{d.month:02d}-01", f"{d.year:04d}-{d.month:02d}-31"
    return d.iso, d.iso


def end_before_start(start: Day | None, end: Day | None) -> bool:
    """The coded end lies entirely before the coded start (beyond date precision): a source inconsistency, kept
    and flagged, never corrected."""
    return start is not None and end is not None and _window(end)[1] < _window(start)[0]


def _ymd(y: Any, m: Any = None, d: Any = None) -> Day | None:
    yi, mi, di = _int(y), _int(m), _int(d)
    if yi is None or not (1800 <= yi <= 2100):
        return None
    if mi is None or not (1 <= mi <= 12):
        return Day(yi)
    if di is None:
        return Day(yi, mi, 1, 10)
    try:
        date(yi, mi, di)
    except ValueError:
        return Day(yi, mi, 1, 10)
    return Day(yi, mi, di, 11)


_MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}


def _stata_date(v: Any, year: Any = None) -> Day | None:
    """EUSANCT dates are Stata ``%td`` text (``30jun1950``); fall back on the year column."""
    s = "" if v is None else str(v).strip().lower()
    m = re.fullmatch(r"(\d{1,2})([a-z]{3})(\d{4})", s)
    if m and m.group(2) in _MONTHS:
        return _ymd(m.group(3), _MONTHS[m.group(2)], m.group(1))
    return _ymd(year)


# =====================================================================================================
# Cases (one neutral record per TIES / EUSANCT case)
# =====================================================================================================


@dataclass
class SenderSpell:
    """One sender's part in a case. ``ref`` = ("cow", code) or ("org", "EU"/"UN")."""

    ref: tuple[str, Any]
    start: Day | None
    end: Day | None
    end_basis: str  # coded | ongoing_as_of | ongoing_at_end_of_coverage | no_end_coded
    imposition_start: Day | None
    primary: bool = False
    threat: bool | None = None
    imposition: bool | None = None
    threat_types: list[str] = field(default_factory=list)
    imposed_types: list[str] = field(default_factory=list)
    label: str | None = None  # EU / US / UN for EUSANCT


@dataclass
class Case:
    dataset: str  # ties | eusanct
    case_id: str
    target: int | None
    target_name: str | None
    start: Day | None
    end: Day | None
    ongoing_as_of: Day | None
    end_basis: str
    senders: list[SenderSpell]
    threat: bool | None
    imposition: bool | None
    imposition_start: Day | None
    issues: list[int]
    threat_types: list[str]
    imposed_types: list[str]
    institutions: list[str]
    outcome: int | None
    extra: dict[str, Any]
    row: int  # 1-based line/row in the raw file (header = 1)


def _flag(v: Any) -> bool | None:
    i = _int(v)
    return None if i is None else bool(i)


TIES_REQUIRED = (
    "caseid",
    "startmonth",
    "startday",
    "startyear",
    "endmonth",
    "endday",
    "endyear",
    "ongoingasofmonth",
    "ongoingasofday",
    "ongoingasofyear",
    "sender1",
    "sender2",
    "sender3",
    "sender4",
    "sender5",
    "primarysender",
    "targetstate",
    "institution",
    "institutionid",
    "issue1",
    "issue2",
    "issue3",
    "threat",
    "sanctiontypethreat",
    "imposition",
    "sancimpositionstartmonth",
    "sancimpositionstartday",
    "sancimpositionstartyear",
    "sanctiontype",
    "targetcosts",
    "sendercosts",
    "finaloutcome",
    "settlementnaturesender",
    "settlementnaturetarget",
)
TIES_MIN_CASES = 1400


def parse_ties(body: bytes, *, strict: bool = True) -> tuple[str, list[Case]]:
    sheet, rows = read_xls_first_sheet(body)
    if not rows:
        raise _fail("TIES workbook is empty", "incomplete")
    hdr = [(h or "").strip() for h in rows[0]]
    missing = [c for c in TIES_REQUIRED if c not in hdr]
    if missing:
        raise _fail(f"TIES: missing columns {missing}")
    ix = {h: i for i, h in enumerate(hdr) if h}
    cases: list[Case] = []
    seen: set[str] = set()
    for r_i, raw in enumerate(rows[1:], start=2):
        r = {h: (raw[i] if i < len(raw) else None) for h, i in ix.items()}
        cid = _int(r["caseid"])
        if cid is None:
            if any(v not in (None, "") for v in raw):
                raise _fail(f"TIES row {r_i}: no caseid")
            continue
        case_id = str(cid)
        if case_id in seen:
            raise _fail(f"TIES: duplicate caseid {case_id}")
        seen.add(case_id)
        start = _ymd(r["startyear"], r["startmonth"], r["startday"])
        end = _ymd(r["endyear"], r["endmonth"], r["endday"])
        ongoing = _ymd(r["ongoingasofyear"], r["ongoingasofmonth"], r["ongoingasofday"])
        end_basis = "coded" if end else ("ongoing_as_of" if ongoing else "no_end_coded")
        if end_before_start(start, end):
            end_basis = "coded_end_before_start"
        primary = _int(r["primarysender"])
        sender_codes: list[int] = []
        for k in ("sender1", "sender2", "sender3", "sender4", "sender5", "primarysender"):
            c = _int(r[k])
            if c is not None and c not in sender_codes:
                sender_codes.append(c)
        imp_start = _ymd(r["sancimpositionstartyear"], r["sancimpositionstartmonth"], r["sancimpositionstartday"])
        spells = [
            SenderSpell(
                ref=("cow", c),
                start=start,
                end=end or ongoing,
                end_basis=end_basis,
                imposition_start=imp_start if _flag(r["imposition"]) else None,
                primary=(c == primary),
            )
            for c in sender_codes
        ]
        extra = {
            k: _int(r[k])
            for k in ("targetcosts", "sendercosts", "settlementnaturesender", "settlementnaturetarget", "institution")
            if _int(r[k]) is not None
        }
        cases.append(
            Case(
                dataset="ties",
                case_id=case_id,
                target=_int(r["targetstate"]),
                target_name=None,
                start=start,
                end=end,
                ongoing_as_of=ongoing,
                end_basis=end_basis,
                senders=spells,
                threat=_flag(r["threat"]),
                imposition=_flag(r["imposition"]),
                imposition_start=imp_start if _flag(r["imposition"]) else None,
                issues=[i for i in (_int(r[k]) for k in ("issue1", "issue2", "issue3")) if i is not None],
                threat_types=_codes(r["sanctiontypethreat"]),
                imposed_types=_codes(r["sanctiontype"]),
                institutions=_codes(r["institutionid"]),
                outcome=_int(r["finaloutcome"]),
                extra=extra,
                row=r_i,
            )
        )
    if strict and len(cases) < TIES_MIN_CASES:
        raise _fail(f"TIES: {len(cases)} cases < expected {TIES_MIN_CASES} (truncated?)", "incomplete")
    return sheet, cases


EUSANCT_MEMBER = "EUSANCT_Dataset_Case-level.csv"
EUSANCT_MIN_CASES = 320
EUSANCT_LAST_YEAR = 2015  # "The EUSANCT Dataset covers the period from 1989 to 2015" (codebook, introduction)
EUSANCT_REQUIRED = (
    "caseid",
    "targetstate",
    "targetstate_name",
    "EU",
    "US",
    "UN",
    "sender_combination",
    "startyearEU",
    "startdateEU",
    "startyearUS",
    "startdateUS",
    "startyearUN",
    "startdateUN",
    "endyearEU",
    "enddateEU",
    "endyearUS",
    "enddateUS",
    "endyearUN",
    "enddateUN",
    "sender2",
    "sender3",
    "sender4",
    "sender5",
    "institution2",
    "institution3",
    "multi",
    "issue1",
    "issue2",
    "issue3",
    "threatEU",
    "threatEU_type",
    "threatUS",
    "threatUS_type",
    "threatUN",
    "threatUN_type",
    "impositionEU",
    "impositionEU_year",
    "impositionEU_start",
    "impositionEU_type",
    "impositionUS",
    "impositionUS_year",
    "impositionUS_start",
    "impositionUS_type",
    "impositionUN",
    "impositionUN_year",
    "impositionUN_start",
    "impositionUN_type",
    "finaloutcome",
    "threat",
    "threat_success",
    "sanction",
    "sanctions_success",
    "success",
    "policyresult",
    "sanctioncontribution",
    "HSEscore",
    "datasource",
)


def eusanct_csv_from_tar(body: bytes) -> tuple[str, bytes]:
    """(member path, bytes) of the case-level CSV inside the RADAR tar (bounded, regular files only)."""
    try:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:") as tf:
            for m in tf:
                if m.isfile() and m.name.rsplit("/", 1)[-1] == EUSANCT_MEMBER:
                    if m.size > 20_000_000:
                        raise _fail("EUSANCT case-level CSV too large", "too_large")
                    f = tf.extractfile(m)
                    if f is None:
                        break
                    return m.name, f.read()
    except tarfile.TarError as e:
        raise _fail(f"EUSANCT archive is not a readable tar: {e}") from e
    raise _fail(f"EUSANCT archive has no {EUSANCT_MEMBER}")


def parse_eusanct(csv_bytes: bytes, *, strict: bool = True) -> list[Case]:
    try:
        text_ = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise _fail("EUSANCT CSV is not UTF-8") from e
    reader = csv.DictReader(io.StringIO(text_, newline=""), delimiter=";")
    missing = [c for c in EUSANCT_REQUIRED if c not in (reader.fieldnames or [])]
    if missing:
        raise _fail(f"EUSANCT: missing columns {missing}")
    cases: list[Case] = []
    seen: set[str] = set()
    for line, r in enumerate(reader, start=2):
        cid = _int(r["caseid"])
        if cid is None:
            continue
        case_id = str(cid)
        if case_id in seen:
            raise _fail(f"EUSANCT: duplicate caseid {case_id}")
        seen.add(case_id)
        spells: list[SenderSpell] = []
        any_ongoing = False
        for s in ("EU", "US", "UN"):
            if _flag(r[s]) is not True:
                continue
            start = _stata_date(r[f"startdate{s}"], r[f"startyear{s}"])
            end_raw = (r[f"enddate{s}"] or "").strip().lower()
            if end_raw == "ongoing":
                end, basis = None, "ongoing_at_end_of_coverage"
                any_ongoing = True
            else:
                end = _stata_date(r[f"enddate{s}"], r[f"endyear{s}"])
                basis = "coded" if end else "no_end_coded"
                if end_before_start(start, end):
                    basis = "coded_end_before_start"
            imposed = _flag(r[f"imposition{s}"]) is True
            spells.append(
                SenderSpell(
                    ref=("cow", 2) if s == "US" else ("org", s),
                    start=start,
                    end=end,
                    end_basis=basis,
                    imposition_start=_stata_date(r[f"imposition{s}_start"], r[f"imposition{s}_year"])
                    if imposed
                    else None,
                    threat=_flag(r[f"threat{s}"]),
                    imposition=_flag(r[f"imposition{s}"]),
                    threat_types=_codes(r[f"threat{s}_type"]),
                    imposed_types=_codes(r[f"imposition{s}_type"]),
                    label=s,
                )
            )
        starts = [s.start for s in spells if s.start]
        ends = [s.end for s in spells if s.end]
        c_start = min(starts, key=lambda d: d.iso) if starts else None
        c_end = None if any_ongoing or not ends or len(ends) < len(spells) else max(ends, key=lambda d: d.iso)
        basis = "coded" if c_end else ("ongoing_at_end_of_coverage" if any_ongoing else "no_end_coded")
        if end_before_start(c_start, c_end):
            basis = "coded_end_before_start"
        for k in ("sender2", "sender3", "sender4", "sender5"):
            c = _int(r[k])
            if c is not None and all(sp.ref != ("cow", c) for sp in spells):
                # additional sender States: EUSANCT gives them no dates of their own -> the case's span
                spells.append(
                    SenderSpell(ref=("cow", c), start=c_start, end=c_end, end_basis=basis, imposition_start=None)
                )
        imp_starts = [s.imposition_start for s in spells if s.imposition_start]
        extra: dict[str, Any] = {
            "sender_combination": r["sender_combination"],
            "datasource": r["datasource"],
        }
        for k in (
            "multi",
            "threat_success",
            "sanctions_success",
            "success",
            "policyresult",
            "sanctioncontribution",
            "HSEscore",
        ):
            if _int(r[k]) is not None:
                extra[k] = _int(r[k])
        cases.append(
            Case(
                dataset="eusanct",
                case_id=case_id,
                target=_int(r["targetstate"]),
                target_name=(r["targetstate_name"] or "").strip() or None,
                start=c_start,
                end=c_end,
                ongoing_as_of=None,
                end_basis=basis,
                senders=spells,
                threat=_flag(r["threat"]),
                imposition=_flag(r["sanction"]),
                imposition_start=min(imp_starts, key=lambda d: d.iso) if imp_starts else None,
                issues=[i for i in (_int(r[k]) for k in ("issue1", "issue2", "issue3")) if i is not None],
                threat_types=[],
                imposed_types=[],
                institutions=[c for c in (_codes(r["institution2"]) + _codes(r["institution3"]))],
                outcome=_int(r["finaloutcome"]),
                extra=extra,
                row=line,
            )
        )
    if strict and len(cases) < EUSANCT_MIN_CASES:
        raise _fail(f"EUSANCT: {len(cases)} cases < expected {EUSANCT_MIN_CASES} (truncated?)", "incomplete")
    return cases


# =====================================================================================================
# Atlas rows: dyad-year and country-year counts (computed from the case rows)
# =====================================================================================================

DYAD_INDICATORS = {
    "CASES_STARTED": "cases whose sender spell starts in the year (TIES: case start = first threat or imposition)",
    "CASES_ACTIVE": "cases active in the year, from the sender's start year to its end year (end basis in flags)",
    "IMPOSITIONS_ACTIVE": "cases with sanctions imposed by the sender, from the imposition start year to the end year",
}
COUNTRY_INDICATORS = {
    "TARGET_CASES_STARTED": "cases targeting the country that start in the year",
    "TARGET_CASES_ACTIVE": "cases targeting the country active in the year (case start .. case end/end basis)",
    "TARGET_IMPOSITIONS_ACTIVE": "cases targeting the country with sanctions imposed, imposition start .. case end",
    "SENDER_CASES_STARTED": "cases in which the country is a sender State, by the year its sender spell starts",
}
END_BASIS_NOTE = {
    "coded": "end date coded by the dataset",
    "ongoing_as_of": "no end coded; active through the TIES 'ongoing as of' (last reported incident) year",
    "ongoing_at_end_of_coverage": "coded 'ongoing'; active through the last coverage year of EUSANCT (2015)",
    "no_end_coded": "no end coded and no ongoing date: counted in its start year only",
    "coded_end_before_start": "the coded end precedes the coded start (source inconsistency, kept and flagged): "
    "counted in its start year only",
}
DATASET_META = {
    "ties": {"file": "TIESv4-1.xls", "name": "TIES 4.1 (Morgan, Bapat & Kobayashi 2014)"},
    "eusanct": {"file": EUSANCT_MEMBER, "name": "EUSANCT 1.0 case level (Weber & Schneider 2020)"},
}


def _span(start: Day | None, end: Day | None, basis: str, ongoing: Day | None, last_year: int) -> list[int]:
    if start is None:
        return []
    if basis == "coded_end_before_start":
        stop = start.year
    elif end is not None:
        stop = end.year
    elif basis == "ongoing_as_of" and ongoing is not None:
        stop = ongoing.year
    elif basis == "ongoing_at_end_of_coverage":
        stop = max(last_year, start.year)
    else:
        stop = start.year
    return list(range(start.year, max(stop, start.year) + 1))


class _Acc:
    """(indicator, provider_geo, year) -> case ids / rows / partner and resolution metadata."""

    def __init__(self) -> None:
        self.cases: dict[tuple[str, str, int], set[str]] = defaultdict(set)
        self.rows: dict[tuple[str, str, int], list[int]] = defaultdict(list)
        self.meta: dict[tuple[str, str, int], dict[str, Any]] = {}
        self.basis: dict[tuple[str, str, int], Counter[str]] = defaultdict(Counter)

    def add(self, key: tuple[str, str, int], case: Case, basis: str, meta: dict[str, Any]) -> None:
        if case.case_id not in self.cases[key]:
            self.rows[key].append(case.row)
            self.basis[key][basis] += 1
        self.cases[key].add(case.case_id)
        m = self.meta.setdefault(key, dict(meta))
        for k in ("partner_code", "row_code"):
            if k in meta and meta[k] not in str(m.get(k, "")).split(","):
                m[k] = ",".join(sorted({*str(m.get(k, "")).split(","), meta[k]} - {""}))


def _resolve(crosswalk: StateCodeCrosswalk, ref: tuple[str, Any], when: date) -> tuple[str | None, str, dict[str, Any]]:
    """(iso3, provider_geo, flags) of a sender/target reference at a date."""
    kind, code = ref
    if kind == "org":
        return None, f"org:{code}", {"sender_label": ORG_LABELS[ORG_EU if code == "EU" else ORG_UN][0]}
    if code == EEC_EU_CODE:
        return None, f"cow:{code}", {"sender_label": ORG_LABELS[ORG_EU][0] + " (code 1000)"}
    res = crosswalk.resolve("cow", int(code), when)
    f: dict[str, Any] = {"code_resolution": res.reason}
    if res.iso3 is None and res.label:
        f["historical_state"] = res.label
    if res.iso3 is None and res.qid:
        f["qid"] = res.qid  # verified historical-state QID (registry/entities/historical_states_qids.json)
    return res.iso3, f"cow:{code}", f


def case_atlas_rows(
    cases: Sequence[Case], *, raw_sha256: str, crosswalk: StateCodeCrosswalk, last_year: int, file: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(dyad_year rows, country_year rows) for one dataset."""
    from reva.atlas.tables import json_obj, year_period

    dataset = cases[0].dataset if cases else "ties"
    dyad, ctry = _Acc(), _Acc()
    for c in cases:
        tgt_ref = ("cow", c.target) if c.target is not None else None
        # country-year (target role)
        if tgt_ref is not None and c.start is not None:
            for ind, years in (
                ("TARGET_CASES_STARTED", [c.start.year]),
                ("TARGET_CASES_ACTIVE", _span(c.start, c.end, c.end_basis, c.ongoing_as_of, last_year)),
                (
                    "TARGET_IMPOSITIONS_ACTIVE",
                    _span(c.imposition_start, c.end, c.end_basis, c.ongoing_as_of, last_year)
                    if c.imposition_start
                    else [],
                ),
            ):
                for y in years:
                    iso, geo, f = _resolve(crosswalk, tgt_ref, date(y, 7, 1))
                    ctry.add((ind, geo, y), c, c.end_basis, {"iso3": iso, "entity_qid": f.pop("qid", None), **f})
        for sp in c.senders:
            if sp.start is None:
                continue
            if sp.ref[0] == "cow" and sp.ref[1] != EEC_EU_CODE:
                iso, geo, f = _resolve(crosswalk, sp.ref, date(sp.start.year, 7, 1))
                ctry.add(
                    ("SENDER_CASES_STARTED", geo, sp.start.year),
                    c,
                    sp.end_basis,
                    {"iso3": iso, "entity_qid": f.pop("qid", None), **f},
                )
            if tgt_ref is None:
                continue
            spans = {
                "CASES_STARTED": [sp.start.year],
                "CASES_ACTIVE": _span(sp.start, sp.end, sp.end_basis, c.ongoing_as_of, last_year),
                "IMPOSITIONS_ACTIVE": _span(sp.imposition_start, sp.end, sp.end_basis, c.ongoing_as_of, last_year)
                if sp.imposition_start
                else [],
            }
            for ind, years in spans.items():
                for y in years:
                    when = date(y, 7, 1)
                    iso, geo, f = _resolve(crosswalk, sp.ref, when)
                    p_iso, p_geo, pf = _resolve(crosswalk, tgt_ref, when)
                    p_code = p_geo
                    dim = f"partner={p_iso}" if p_iso else f"partner_code={p_code}"
                    meta = {
                        "iso3": iso,
                        "entity_qid": f.pop("qid", None),
                        **{f"row_{k}": v for k, v in f.items()},
                        "partner_iso3": p_iso,
                        "counterpart_iso3": p_iso,
                        "partner_code": p_code,
                        **{f"partner_{k}": v for k, v in pf.items()},
                    }
                    if sp.label:
                        meta["sender"] = sp.label
                    meta["sender_dates"] = (
                        "own dates of this sender"
                        if sp.label
                        else "the case's dates (the dataset codes no dates per additional sender)"
                    )
                    dyad.add((f"{ind}|{dim}", geo, y), c, sp.end_basis, meta)

    def emit(acc: _Acc, defs: Mapping[str, str], table: str) -> list[dict[str, Any]]:
        out = []
        for key in sorted(acc.cases, key=lambda k: (k[0], k[1], k[2])):
            ind, geo, y = key
            meta = dict(acc.meta[key])
            iso = meta.pop("iso3")
            qid = meta.pop("entity_qid", None)
            ids = sorted(acc.cases[key])
            start, end = year_period(y)
            flags: dict[str, Any] = {
                "computed_from": DATASET_META[dataset]["name"],
                "method": "count of distinct case ids; " + defs[ind.split("|", 1)[0]],
                "case_ids": ids[:100],
                "cases_total": len(ids),
                "end_basis": dict(sorted(acc.basis[key].items())),
                **{k: v for k, v in meta.items() if v is not None},
            }
            if "partner_iso3" in meta:
                flags["partner_iso3"] = meta["partner_iso3"]
                flags["counterpart_iso3"] = meta["partner_iso3"]
            rows = acc.rows[key]
            out.append(
                {
                    "source": dataset,
                    "dataset": table,
                    "indicator": ind,
                    "concept_id": None,
                    "iso3": iso,
                    "provider_geo": geo,
                    "entity_qid": qid,
                    "period_start": start,
                    "period_end": end,
                    "value": str(len(ids)),
                    "value_text": None,
                    "unit": "cases",
                    "flags": fit_flags(flags),
                    "raw_sha256": raw_sha256,
                    "selector": json_obj(file=file, rows=sorted(rows)[:50], rows_total=len(rows), year=y),
                }
            )
        return out

    return emit(dyad, DYAD_INDICATORS, "dyad_year"), emit(ctry, COUNTRY_INDICATORS, "country_year")


# =====================================================================================================
# KG records (events, participants, coded outcomes) and the causal-claim view for CAUSCORE
# =====================================================================================================

#: Dataset predicates: pid -> (value kind, reva predicate, en label, fr label, inverse name, inverse en label).
SANCTIONS_PROPERTIES: dict[str, tuple[str, str, str, str, str | None, str | None]] = {}


def _prop(ns: str, name: str, kind: str, en: str, fr: str, inv: str | None = None, inv_en: str | None = None) -> None:
    tag = {"ties": "TIES", "eusanct": "EUSANCT", "eusanctionsmap": "EU Sanctions Map", "ofac": "OFAC"}[ns]
    SANCTIONS_PROPERTIES[f"{ns}:{name}"] = (
        kind,
        f"{ns}_{name}",
        f"{en} ({tag})",
        f"{fr} ({tag})",
        f"{ns}_{inv}" if inv else None,
        f"{inv_en} ({tag})" if inv_en else None,
    )


for _ns in ("ties", "eusanct"):
    _prop(_ns, "sender", "item", "sanctions sender", "émetteur de sanctions", "sender_in", "sender in sanctions case")
    _prop(_ns, "target", "item", "sanctions target", "cible de sanctions", "targeted_in", "targeted in sanctions case")
    _prop(_ns, "threat_issued", "string", "sanctions threat issued", "menace de sanctions émise")
    _prop(_ns, "sanctions_imposed", "string", "sanctions imposed", "sanctions imposées")
    _prop(_ns, "imposition_start", "time", "sanctions imposition start", "début de l'imposition des sanctions")
    _prop(_ns, "issue", "string", "issue (sender objective as coded)", "enjeu (objectif de l'émetteur, codé)")
    _prop(_ns, "sanction_type_threatened", "string", "sanction type threatened", "type de sanction menacé")
    _prop(_ns, "sanction_type_imposed", "string", "sanction type imposed", "type de sanction imposé")
    _prop(_ns, "institution_igo_code", "string", "institution (COW IGO code)", "institution (code OIG COW)")
    _prop(_ns, "final_outcome", "string", "final outcome as coded", "issue finale codée")
    _prop(
        _ns,
        "outcome_record",
        "item",
        "has outcome record",
        "a pour fiche d'issue",
        "outcome_record_of",
        "outcome record of",
    )
_prop("ties", "ongoing_as_of", "time", "case ongoing as of (last reported incident)", "en cours au (dernier incident)")
_prop("eusanct", "multilateralism", "string", "unilateral / multilateral / universal", "unilatérale / multilatérale")
_prop("eusanct", "hse_policy_result", "string", "HSE policy result", "résultat politique (HSE)")
_prop(
    "eusanct",
    "policy_result_record",
    "item",
    "has sender-goals record (HSE policy result)",
    "a pour fiche d'objectifs de l'émetteur (HSE)",
    "policy_result_record_of",
    "sender-goals record of",
)
_prop(
    "eusanct",
    "hse_sanction_contribution",
    "string",
    "HSE sanction contribution (as coded)",
    "contribution de la sanction selon HSE (codée)",
)
_prop(
    "eusanct",
    "same_case_as",
    "item",
    "same sanctions case as",
    "même épisode de sanctions que",
    "same_case_as",
    "same sanctions case as",
)
_prop(
    "eusanctionsmap",
    "listed_under_country",
    "item",
    "regime listed under country",
    "régime classé sous le pays",
    "country_of_regime",
    "country of sanctions regime",
)
_prop("eusanctionsmap", "adopted_by", "string", "adopted by", "adopté par")
_prop("eusanctionsmap", "measure_type", "string", "restrictive measure type", "type de mesure restrictive")
_prop("eusanctionsmap", "legal_act", "string", "legal act", "acte juridique")
_prop("eusanctionsmap", "thematic_programme", "string", "thematic programme code", "code de programme thématique")
_prop("eusanctionsmap", "last_amended", "time", "last legal update", "dernière mise à jour juridique")
_prop("eusanctionsmap", "expires", "time", "expiry date", "date d'expiration")
_prop("eusanctionsmap", "listed_in_force", "time", "listed as in force", "listé comme en vigueur")
_prop(
    "ofac",
    "program_country",
    "item",
    "program names country",
    "programme nommant le pays",
    "named_in_program",
    "named in OFAC sanctions program",
)
_prop("ofac", "imposed_by", "item", "program administered for", "programme administré pour")
_prop("ofac", "last_updated", "time", "program last updated", "dernière mise à jour du programme")
_prop("ofac", "listed_active", "time", "listed as an active program", "listé comme programme actif")


#: Wikidata properties used on dataset entities, registered with WDHARVEST's own definitions (as atlas_cow does).
KG_WIKIDATA_PIDS = ("P580", "P582", "P585")


def kg_properties() -> list[Any]:
    from reva.kg.model import KgProperty

    return [
        KgProperty(
            pid=p, value_kind=k, reva_predicate=rp, label_en=le, label_fr=lf, inverse_name=inv, inverse_label_en=ile
        )
        for p, (k, rp, le, lf, inv, ile) in SANCTIONS_PROPERTIES.items()
    ]


def _t(d: Day) -> dict[str, Any]:
    return {"type": "time", "time": d.iso, "precision": d.precision}


def _s(v: str) -> dict[str, Any]:
    return {"type": "string", "value": v}


def _yes(b: bool | None) -> str | None:
    return None if b is None else ("yes" if b else "no")


def _outcome_meta(c: Case) -> tuple[str, str] | None:
    table = TIES_OUTCOMES if c.dataset == "ties" else EUSANCT_OUTCOMES
    return table.get(c.outcome) if c.outcome is not None else None


def _sender_entity(sp: SenderSpell, resolver: StateEntityResolver, when: date) -> str:
    kind, code = sp.ref
    if kind == "org":
        return ORG_EU if code == "EU" else ORG_UN
    if code == EEC_EU_CODE:
        return ORG_EU
    return resolver.entity("cow", int(code), when)


def _case_label(c: Case, resolver: StateEntityResolver, target_entity: str | None) -> str:
    tag = "TIES" if c.dataset == "ties" else "EUSANCT"
    tgt = c.target_name or (
        "EEC/EU"
        if c.target == EEC_EU_CODE
        else (resolver.crosswalk.resolve("cow", c.target, c.start.mid if c.start else date(2000, 7, 1)).label or "")
        if c.target is not None
        else ""
    )
    when = c.start.year if c.start else "undated"
    tgt_s = f"{tgt} (COW {c.target})" if tgt else f"COW {c.target}"
    return f"Sanctions case {c.case_id} ({tag}): target {tgt_s}, from {when}"


def _interval(start: Day | None, end: Day | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """(statement time kwargs, extra qualifiers). An end coded before the start is not put on the interval (the
    KG refuses such intervals); it is kept as a qualifier and in its own P582 statement, flagged, never fixed."""
    kw: dict[str, Any] = {
        "start": start.iso if start else None,
        "start_precision": start.precision if start else None,
    }
    if end_before_start(start, end) and end is not None:
        return kw, {
            "coded_end": end.iso,
            "coded_end_precision": end.precision,
            "date_inconsistency": "coded end precedes coded start (source data, kept as coded)",
        }
    kw.update(end=end.iso if end else None, end_precision=end.precision if end else None)
    return kw, {}


class _Adder:
    """Appends statements of one subject (key prefix, raw sha, default selector) to a list."""

    def __init__(self, out: list[KgStatement], prefix: str, subject: str, raw_sha256: str, selector: Mapping[str, Any]):
        self.out, self.prefix, self.subject, self.sha, self.sel = out, prefix, subject, raw_sha256, dict(selector)

    def __call__(self, key: str, prop: str, **kw: Any) -> None:
        kw.setdefault("selector", self.sel)
        self.out.append(
            KgStatement(key=f"{self.prefix}:{key}", subject=self.subject, property=prop, raw_sha256=self.sha, **kw)
        )


def case_kg_records(
    cases: Sequence[Case], *, raw_sha256: str, resolver: StateEntityResolver, file: str
) -> tuple[list[KgEntity], list[KgStatement]]:
    ents: list[KgEntity] = []
    stmts: list[KgStatement] = []
    used_orgs: set[str] = set()
    for c in cases:
        ns = c.dataset
        subj = f"{ns}:case:{c.case_id}"
        sel = {"file": file, "row": c.row}
        when = c.start.mid if c.start else date(2000, 7, 1)
        tgt_entity = None
        if c.target is not None:
            tgt_entity = (
                ORG_EU
                if c.target == EEC_EU_CODE
                else resolver.entity("cow", c.target, when, provider_label=c.target_name)
            )
        ents.append(
            KgEntity(
                qid=subj,
                kind="sanction_episode",
                labels={"en": _case_label(c, resolver, tgt_entity)},
                descriptions={
                    "en": f"Economic sanctions case (threat and/or imposition) as coded by {DATASET_META[ns]['name']}"
                },
            )
        )

        st = _Adder(stmts, c.case_id, subj, raw_sha256, sel)

        end = c.end
        if tgt_entity:
            if tgt_entity == ORG_EU:
                used_orgs.add(ORG_EU)
            tkw, tq = _interval(c.start, c.end)
            st(
                "target",
                f"{ns}:target",
                object_qid=tgt_entity,
                qualifiers={"cow_code": c.target, "end_basis": c.end_basis, **tq},
                **tkw,
            )
        for sp in c.senders:
            obj = _sender_entity(sp, resolver, sp.start.mid if sp.start else when)
            if obj.startswith("sanctions:org:"):
                used_orgs.add(obj)
            q: dict[str, Any] = {"end_basis": sp.end_basis}
            if sp.ref[0] == "cow":
                q["cow_code"] = sp.ref[1]
            if ns == "ties":
                q["primary_sender"] = sp.primary
            if sp.label:
                q["sender"] = sp.label
                q["threat"] = _yes(sp.threat)
                q["imposition"] = _yes(sp.imposition)
            skw, sq = _interval(sp.start, sp.end)
            q.update(sq)
            st(
                f"sender:{sp.label or sp.ref[1]}",
                f"{ns}:sender",
                object_qid=obj,
                qualifiers={k: v for k, v in q.items() if v is not None},
                **skw,
            )
            if sp.label and sp.imposition_start:
                st(
                    f"imposition_start:{sp.label}",
                    f"{ns}:imposition_start",
                    value=_t(sp.imposition_start),
                    qualifiers={"sender": sp.label},
                )
            for kind_, codes in (("threatened", sp.threat_types), ("imposed", sp.imposed_types)):
                for code in codes:
                    lab = EUSANCT_TYPES.get(code) or (
                        EUSANCT_TYPE_13["threat" if kind_ == "threatened" else "imposition"] if code == "13" else None
                    )
                    if lab is None:
                        continue
                    st(
                        f"type_{kind_}:{sp.label}:{code}",
                        f"{ns}:sanction_type_{kind_}",
                        value=_s(lab),
                        qualifiers={
                            "sender": sp.label,
                            "code": code,
                            "family": EUSANCT_FAMILY.get(code, "other"),
                            "family_basis": "REVA grouping of the codebook category",
                        },
                    )
        if c.start:
            # P580/P582 (Wikidata start/end time, WDHARVEST's definitions): the dates the event view and the causal
            # grader read
            st(
                "P580",
                "P580",
                value=_t(c.start),
                qualifiers={
                    "definition": "case begins with the first threat, or with an imposition without prior threat"
                },
            )
        if c.end:
            st("P582", "P582", value=_t(c.end), qualifiers={"definition": "case end as coded by the dataset"})
        if c.ongoing_as_of:
            st("ongoing_as_of", "ties:ongoing_as_of", value=_t(c.ongoing_as_of))
        if c.threat is not None:
            st("threat_issued", f"{ns}:threat_issued", value=_s(_yes(c.threat) or ""))
        if c.imposition is not None:
            st("sanctions_imposed", f"{ns}:sanctions_imposed", value=_s(_yes(c.imposition) or ""))
        if ns == "ties" and c.imposition_start:
            st(
                "imposition_start",
                "ties:imposition_start",
                value=_t(c.imposition_start),
                qualifiers={
                    "stage_sequence": "threat stage precedes imposition within the same case (context, not "
                    "a causal claim)"
                    if c.threat
                    else "imposition without prior threat"
                },
            )
        issues = TIES_ISSUES if ns == "ties" else EUSANCT_ISSUES
        for i in c.issues:
            if i in issues:
                st(f"issue:{i}", f"{ns}:issue", value=_s(issues[i]), qualifiers={"code": i})
        if ns == "ties":
            for kind_, codes, table, fam in (
                ("threatened", c.threat_types, TIES_THREAT_TYPES, TIES_THREAT_FAMILY),
                ("imposed", c.imposed_types, TIES_IMPOSED_TYPES, TIES_IMPOSED_FAMILY),
            ):
                for code in codes:
                    k = _int(code)
                    if k in table:
                        st(
                            f"type_{kind_}:{k}",
                            f"ties:sanction_type_{kind_}",
                            value=_s(table[k]),
                            qualifiers={
                                "code": k,
                                "family": fam[k],
                                "family_basis": "REVA grouping of the codebook category",
                            },
                        )
        for code in c.institutions:
            st(f"institution:{code}", f"{ns}:institution_igo_code", value=_s(code))
        if ns == "eusanct" and c.extra.get("multi") in EUSANCT_MULTI:
            st(
                "multilateralism",
                "eusanct:multilateralism",
                value=_s(EUSANCT_MULTI[c.extra["multi"]]),
                qualifiers={"code": c.extra["multi"]},
            )
        if ns == "eusanct" and "TIES" in str(c.extra.get("datasource", "")).split("/"):
            pass  # same_case_as is added by link_eusanct_to_ties (needs the TIES case list)
        om = _outcome_meta(c)
        if om:
            label, stage = om
            out_id = f"{ns}:outcome:{c.case_id}"
            ents.append(
                KgEntity(
                    qid=out_id,
                    kind="coded_outcome",
                    labels={"en": f"Outcome of sanctions case {c.case_id} ({ns.upper()}): {label}"},
                    descriptions={"en": f"Final outcome category coded by {DATASET_META[ns]['name']}"},
                )
            )
            st("final_outcome", f"{ns}:final_outcome", value=_s(label), qualifiers={"code": c.outcome, "stage": stage})
            q = {
                "codebook_variable": "finaloutcome",
                "claim": "the case -> outcome relation itself is a T1 coded_outcome claim in kg.causal_claim "
                "(reva.causal.load_dataset_claims); this statement is context",
                "code": c.outcome,
                "label": label,
                "stage": stage,
            }
            if ns == "ties":
                for ck, lab in (("targetcosts", "target_costs"), ("sendercosts", "sender_costs")):
                    if c.extra.get(ck) in TIES_COSTS:
                        q[lab] = TIES_COSTS[c.extra[ck]]
                for ck in ("settlementnaturesender", "settlementnaturetarget"):
                    if ck in c.extra:
                        q[ck] = c.extra[ck]
                if "settlementnaturesender" in c.extra or "settlementnaturetarget" in c.extra:
                    q["settlement_caveat"] = (
                        "TIES manual: reliability tests indicate the settlement measures are somewhat questionable"
                    )
            else:
                for ck in ("threat_success", "sanctions_success", "success"):
                    if ck in c.extra:
                        q[ck] = c.extra[ck]
            st(
                "outcome_record",
                f"{ns}:outcome_record",
                object_qid=out_id,
                qualifiers=q,
                start=end.iso if end else None,
                start_precision=end.precision if end else None,
            )
            if c.end:  # the outcome is the nature of the case's termination: dated at the coded case end only
                stmts.append(
                    KgStatement(
                        key=f"{c.case_id}:outcome:P585",
                        subject=out_id,
                        property="P585",
                        value=_t(c.end),
                        qualifiers={"basis": "coded case end (the outcome is the nature of the termination)"},
                        raw_sha256=raw_sha256,
                        selector=sel,
                    )
                )
            if ns == "eusanct":
                pr, sc = c.extra.get("policyresult"), c.extra.get("sanctioncontribution")
                if pr in HSE_POLICY_RESULT:
                    st(
                        "hse_policy_result",
                        "eusanct:hse_policy_result",
                        value=_s(HSE_POLICY_RESULT[pr]),
                        qualifiers={"code": pr, "hse_score": c.extra.get("HSEscore")},
                    )
                    goal_id = f"eusanct:policy_result:{c.case_id}"
                    ents.append(
                        KgEntity(
                            qid=goal_id,
                            kind="coded_outcome",
                            labels={
                                "en": f"Sender goals in sanctions case {c.case_id} (EUSANCT, HSE policy result): "
                                f"{HSE_POLICY_RESULT[pr]}"
                            },
                            descriptions={
                                "en": "Degree to which the goals stated by the sender were realized, as "
                                "coded by EUSANCT after Hufbauer, Schott & Elliott (2009)"
                            },
                        )
                    )
                    st(
                        "policy_result_record",
                        "eusanct:policy_result_record",
                        object_qid=goal_id,
                        qualifiers={"code": pr, "label": HSE_POLICY_RESULT[pr], "hse_score": c.extra.get("HSEscore")},
                    )
                if sc in HSE_CONTRIBUTION:
                    st(
                        "hse_sanction_contribution",
                        "eusanct:hse_sanction_contribution",
                        value=_s(HSE_CONTRIBUTION[sc]),
                        qualifiers={
                            "code": sc,
                            "policy_result": HSE_POLICY_RESULT.get(pr) if isinstance(pr, int) else None,
                            "hse_score": c.extra.get("HSEscore"),
                            "hse_threshold": "a score of 9 or higher implies that the sanction made a substantial "
                            "contribution to the goals of the sender (codebook variable 36)",
                        },
                    )
    for org in sorted(used_orgs):
        lab, desc = ORG_LABELS[org]
        ents.append(KgEntity(qid=org, kind="organization", labels={"en": lab}, descriptions={"en": desc}))
    return ents, stmts


def link_eusanct_to_ties(
    eus: Sequence[Case], ties: Sequence[Case], *, raw_sha256: str, file: str
) -> tuple[list[KgStatement], Counter[str]]:
    """``eusanct:same_case_as`` only with evidence: identical case id, EUSANCT ``datasource`` naming TIES and
    the same target COW code. Everything else is counted, never linked by dates or names."""
    by_id = {c.case_id: c for c in ties}
    out: list[KgStatement] = []
    report: Counter[str] = Counter()
    for c in eus:
        from_ties = "TIES" in str(c.extra.get("datasource", "")).split("/")
        t = by_id.get(c.case_id)
        if not from_ties:
            report["datasource_not_ties"] += 1
            continue
        if t is None:
            report["ties_named_but_case_id_absent"] += 1
            continue
        if t.target != c.target:
            report["case_id_shared_but_target_differs"] += 1
            continue
        report["linked"] += 1
        out.append(
            KgStatement(
                key=f"{c.case_id}:same_case_as:ties",
                subject=f"eusanct:case:{c.case_id}",
                property="eusanct:same_case_as",
                object_qid=f"ties:case:{c.case_id}",
                qualifiers={
                    "evidence": "identical case id; EUSANCT datasource names TIES; identical target COW code",
                    "datasource": c.extra.get("datasource"),
                },
                raw_sha256=raw_sha256,
                selector={"file": file, "row": c.row, "columns": ["caseid", "targetstate", "datasource"]},
            )
        )
    return out, report


#: Codebook definitions the T1 claims rest on, quoted verbatim from the pinned PDFs (pdftotext -layout,
#: whitespace normalised, page-number lines dropped; in the TIES manual the PDF's ligature glyphs -- "…" for "fi",
#: "¤" for "ff" -- are restored to the letters they stand for).
TIES_CODEBOOK = {
    "name": "Threat and Imposition of Sanctions (TIES) Data 4.0 Users' Manual, Case Level Data "
    "(Morgan, Bapat & Kobayashi)",
    "version": "manual updated June 2013 (tiesusersmanualv4.pdf sha256 "
    "862fe788f0bf24a64d434e0b4e1621c2b7883bf28e9c18366c33a08c8d269540); data TIESv4-1.xls",
    "url": "https://sanctions.web.unc.edu/wp-content/uploads/sites/18834/2021/04/tiesusersmanualv4.pdf",
    "definitions": {
        "finaloutcome": "39. Final Outcome: This variable is designed to capture the nature of the termination of the "
        "sanctions incident. The coder should choose from among the categories listed. If the outcome does not "
        "match the categories listed, the coder should code the variable as other and fill in the outcome in the "
        "blank field at the bottom. The categories are: 1. Partial Acquiescence by Target to threat: The target "
        "state agrees to some of the demands of the sender(s) and adjusts only these behaviors accordingly while "
        "continuing the engage in some of the offensive behavior prior to imposition of sanctions. 2. Complete "
        "Acquiescence by Target to threat: The target state agrees to all of the demands of the sender(s) and "
        "adjusts its behavior accordingly prior to the imposition of sanctions 3. Capitulation by the Sender(s) in "
        "threat stage: Although the sender threatens sanctions, the sender refuses to impose sanctions despite the "
        "refusal of the target to alter its behavior. 4. Stalemate in the Threat Stage: Although the issue remains "
        "unresolved following the sender’s threat, the target does not alter its behavior and the sender does not "
        "impose sanctions. 5. Negotiated Settlement: The target state agrees to alter some of its behavior in "
        "exchange for actions taken by the sender(s) prior to the imposition of sanctions. The sender(s) must "
        "perform some action in exchange for the target state’s compliance for this variable to be coded as 1. 6. "
        "Partial Acquiescence by the Target State following sanctions imposition: The target state agrees to some "
        "of the demands of the sender(s) and adjusts only these behaviors accordingly while continuing the engage "
        "in some of the offensive behavior after sanctions are imposed. 7. Total Acquiescence by Target State "
        "following sanctions imposition: The target state agrees to all of the demands of the sender(s) and "
        "adjusts its behavior accordingly after sanctions are imposed. 8. Capitulation by Sender after "
        "Imposition: The sender state removes sanctions despite the refusal of the target to alter its behavior. "
        "9. Stalemate after Sanctions Imposition: Sanctions are imposed by the sender(s), but the target does not "
        "alter its behavior. 10. Negotiated Settlement following sanctions imposition: The target state agrees to "
        "alter some of its behavior in exchange for actions taken by the sender(s) following the imposition of "
        "sanctions. The sender(s) must perform some action in exchange for the target state’s compliance for this "
        "variable to be coded as 1.",
    },
}
EUSANCT_CODEBOOK = {
    "name": "EUSANCT Dataset – Codebook: Case-level Dataset (Weber & Schneider)",
    "version": "2.0, March 2020 (Codebook_EUSANCT_Case-level-Dataset_01.pdf sha256 "
    "8d8da4c58a8a7ab1d4ef2268c25c239ccea52d44c7fd12e7b53ac4a2ea8696f7, inside the RADAR tar of doi:10.48606/10)",
    "url": "https://doi.org/10.48606/10",
    "definitions": {
        "finaloutcome": "30. Final Outcome: This variable is designed to capture the nature of the termination of the "
        "sanction incident. The coder should choose from among the categories listed. If the outcome does not "
        "match the categories listed, the coder should code the variable as “Other” and fill in the outcome in the "
        "blank field at the bottom. The categories are: 1. Partial Acquiescence by Target to Threat: The target "
        "state agrees to some of the demands of the sender and adjusts only these behaviours accordingly while "
        "continuing the engage in some of the offensive behaviour prior to imposition of the sanction. 2. "
        "Complete Acquiescence by Target to Threat: The target state agrees to all of the demands of the sender and "
        "adjusts its behaviour accordingly prior to the imposition of the sanction. 3. Capitulation by the Sender "
        "in Threat Stage: Although the sender threatens a sanction, the sender refuses to impose a sanction despite "
        "the refusal of the target to alter its behaviour. 4. Stalemate in the Threat Stage: Although the issue "
        "remains unresolved following the threats of the sender, the target does not alter its behaviour and the "
        "sender does not impose a sanction. 5. Negotiated Settlement: The target state agrees to alter some of its "
        "behaviour in exchange for actions taken by the sender prior to the imposition of a sanction. The sender "
        "must perform some action in exchange for the target state’s compliance for this variable to be coded as "
        "5. 6. Partial Acquiescence by the Target State Following Sanction Imposition: The target state agrees to "
        "some of the demands of the sender and adjusts only these behaviours accordingly while continuing the "
        "engage in some of the offensive behaviour after a sanction is imposed. 7. Total Acquiescence by Target "
        "State Following Sanction Imposition: The target state agrees to all of the demands of the sender and "
        "adjusts its behaviour accordingly after the sanction is imposed. 8. Capitulation by the Sender after "
        "Imposition: The sender state removes the sanction despite the refusal of the target to alter its "
        "behaviour. 9. Stalemate after Sanction Imposition: A sanction is imposed by the sender, but the target "
        "does not alter its behaviour. 10. Negotiated Settlement Following Sanction Imposition: The target state "
        "agrees to alter some of its behaviour in exchange for actions taken by the sender following the "
        "imposition of the sanction. The sender must perform some action in exchange for the target state’s "
        "compliance for this variable to be coded as 10. 11. Sanction No Longer Applicable: Circumstances have "
        "changed and the initial reasons for a sanction no longer exist. Example: State collapses, death of "
        "sanctioned persons, etc.",
        "sanctioncontribution": "35. “Sanction Contribution:” This variable assesses the sanction’s contribution to "
        "the achievement of the goals of the sender. 1. “Negative Contribution” 2. “Little or No Contribution” 3. "
        "“Substantial Contribution” 4. “Decisive Contribution” 36. HSE “Success Score:” Multiply the scores for "
        "Policy Result and Sanction Contribution. A score of 9 or higher implies that the sanction made a "
        "substantial contribution to the goals of the sender.",
    },
}
CLAIM_DATASET = {"ties": "ties_case", "eusanct": "eusanct_case"}


def causal_claim_records(
    cases: Sequence[Case], *, source_key: str, dataset_revision: str, raw_sha256: str, file: str
) -> list[dict[str, Any]]:
    """Neutral claim records for CAUSCORE (``kg.causal_claim``): only what the codebooks code.

    * ``coded_outcome`` (T1): case -> its coded final outcome (every case with an outcome code);
    * ``contributing_factor`` (T1, EUSANCT only, ``promotes``): case -> its sender-goals record
      (``eusanct:policy_result:<id>``) when the HSE success score is >= 9 -- the codebook's own threshold for "the
      sanction made a substantial contribution to the goals of the sender" (= contribution >= 3 and policy
      result >= 3). Lower scores and "negative contribution" give no claim (the codebook names no effect for
      them).
    No ``reaction_to`` / ``immediate_cause``: no triggering event is coded."""
    out: list[dict[str, Any]] = []
    for c in cases:
        om = _outcome_meta(c)
        if not om:
            continue
        label, stage = om
        cause, effect = f"{c.dataset}:case:{c.case_id}", f"{c.dataset}:outcome:{c.case_id}"
        base = {
            "cause": cause,
            "effect": effect,
            "cause_time": {"start": c.start.iso, "precision": c.start.precision} if c.start else None,
            "effect_time": {"point": c.end.iso, "precision": c.end.precision} if c.end else None,
            "asserting_source": {
                "source_key": source_key,
                "dataset_revision": dataset_revision,
                "raw_sha256": raw_sha256,
                "selector": {"file": file, "row": c.row},
            },
            "source_tier": "T1",
            "mechanism": None,
            "scope": {"target_cow_code": c.target},
        }
        out.append(
            {
                **base,
                "claim_key": f"{c.dataset}:{c.case_id}:coded_outcome",
                "source_claim_id": f"{c.case_id}:coded_outcome",
                "codebook_term": "finaloutcome",
                "selector": {"file": file, "row": c.row, "columns": ["caseid", "finaloutcome"]},
                "relation": "coded_outcome",
                "polarity": None,
                "statement_key": f"{source_key}:{c.case_id}:outcome_record",
                "coded": {"variable": "finaloutcome", "code": c.outcome, "label": label, "stage": stage},
            }
        )
        sc = c.extra.get("sanctioncontribution") if c.dataset == "eusanct" else None
        pr = c.extra.get("policyresult") if c.dataset == "eusanct" else None
        score = c.extra.get("HSEscore")
        # HSE success score >= 9 (codebook variable 36: "implies that the sanction made a substantial contribution
        # to the goals of the sender"); with integer scores 1-4 this is exactly contribution >= 3 and result >= 3
        if sc in (3, 4) and pr in (3, 4) and isinstance(score, int) and score >= 9:
            out.append(
                {
                    **base,
                    "effect": f"eusanct:policy_result:{c.case_id}",
                    "claim_key": f"{c.dataset}:{c.case_id}:hse_contribution",
                    "source_claim_id": f"{c.case_id}:hse_contribution",
                    "codebook_term": "sanctioncontribution",
                    "selector": {
                        "file": file,
                        "row": c.row,
                        "columns": ["caseid", "sanctioncontribution", "policyresult", "HSEscore"],
                    },
                    "relation": "contributing_factor",
                    "polarity": "promotes",
                    "statement_key": f"{source_key}:{c.case_id}:hse_sanction_contribution",
                    "coded": {
                        "variable": "sanctioncontribution",
                        "code": sc,
                        "label": HSE_CONTRIBUTION[sc],
                        "policy_result": HSE_POLICY_RESULT[pr],
                        "hse_score": c.extra.get("HSEscore"),
                    },
                }
            )
    return out


def _causal_api() -> dict[str, Any] | None:
    """CAUSCORE's T1 loader (``reva.causal``; ``reva.causal.model`` while the package re-exports nothing)."""
    import importlib

    for mod in ("reva.causal", "reva.causal.model"):
        try:
            m = importlib.import_module(mod)
        except ImportError:
            continue
        if all(hasattr(m, n) for n in ("Codebook", "DatasetClaim", "load_dataset_claims")):
            grade = getattr(m, "grade_claims", None) or getattr(
                importlib.import_module("reva.causal"), "grade_claims", None
            )
            return {
                "Codebook": m.Codebook,
                "DatasetClaim": m.DatasetClaim,
                "load": m.load_dataset_claims,
                "grade": grade,
            }
    return None


def deliver_claims(
    run: Any, records: Sequence[Mapping[str, Any]], *, dataset: str, revision: str, raw_sha256: str, retrieved_at: Any
) -> dict[str, Any]:
    """Load the T1 claims through ``reva.causal.load_dataset_claims`` (one complete delivery per dataset) when the
    causal schema and loader exist; otherwise report them as not delivered (counts only, never silent)."""
    from sqlalchemy import text

    api = _causal_api()
    if api is None:
        return {"status": "causal_loader_unavailable", "claims": len(records)}
    with run.engine.connect() as conn:
        if not conn.execute(text("SELECT to_regclass('kg.causal_claim') IS NOT NULL")).scalar_one():
            return {"status": "causal_schema_unavailable", "claims": len(records)}
    cb = TIES_CODEBOOK if dataset == "ties" else EUSANCT_CODEBOOK
    claims = [
        api["DatasetClaim"](
            cause_id=r["cause"],
            effect_id=r["effect"],
            relation=r["relation"],
            selector=r["selector"],
            raw_sha256=raw_sha256,
            codebook_term=r["codebook_term"],
            source_claim_id=r["source_claim_id"],
            polarity=r["polarity"],
            tier="T1",
        )
        for r in records
    ]
    rep = api["load"](
        run.engine,
        run.store,
        source_key=run.spec.source_key,
        dataset=CLAIM_DATASET[dataset],
        dataset_revision=revision,
        codebook=api["Codebook"](**cb),
        raw_sha256s=[raw_sha256],
        retrieved_at=retrieved_at,
        origin=run.origin,
        claims=claims,
        complete=True,
    )
    out: dict[str, Any] = {
        "status": "loaded",
        "claims": len(claims),
        "report": rep.as_dict() if hasattr(rep, "as_dict") else dict(rep),
    }
    if api["grade"] is not None:
        try:
            g = api["grade"](run.engine, origin=run.origin)
            out["grading"] = g.as_dict() if hasattr(g, "as_dict") else str(g)
        except Exception as e:  # grading is the worker's job too: report, do not lose the loaded claims
            out["grading"] = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
    else:
        out["grading"] = "left to the worker (reva.causal.grade_claims not available)"
    return out


# =====================================================================================================
# EU Sanctions Map (regime level) and OFAC programs (program level)
# =====================================================================================================

EU_MAP_MIN_REGIMES = 40
_BRUSSELS = "Europe/Brussels"


def _local_day(ts: Any) -> str | None:
    """Unix timestamps of the map are local midnights in Brussels (22:00/23:00 UTC the day before)."""
    from zoneinfo import ZoneInfo

    if ts in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=ZoneInfo(_BRUSSELS)).date().isoformat()
    except (TypeError, ValueError, OverflowError) as e:
        raise _fail(f"EU map: bad timestamp {ts!r}") from e


@dataclass
class Regime:
    id: int
    index: int  # position in the JSON data array
    specification: str
    adopted_by: str | None
    country_iso2: str | None
    country_title: str | None
    programmes: list[str]
    thematic: bool
    special: int | None
    measures: list[str]
    legal_acts: list[dict[str, str]]
    last_amended: str | None
    expires: str | None


def _data(o: Any) -> Any:
    return o.get("data") if isinstance(o, Mapping) else None


def parse_eu_map(body: bytes, *, strict: bool = True) -> list[Regime]:
    try:
        doc = json.loads(body)
    except ValueError as e:
        raise _fail("EU map: not JSON") from e
    items = doc.get("data") if isinstance(doc, Mapping) else None
    if not isinstance(items, list):
        raise _fail("EU map: no data array")
    out = []
    for i, r in enumerate(items):
        if not isinstance(r, Mapping) or not isinstance(r.get("id"), int) or not r.get("specification"):
            raise _fail(f"EU map regime {i}: no id/specification")
        ab, ctry = _data(r.get("adopted_by")), _data(r.get("country"))
        measures = sorted(
            {
                str(_data(m.get("type")).get("title"))
                for m in (_data(r.get("measures")) or [])
                if isinstance(_data(m.get("type")), Mapping) and _data(m.get("type")).get("title")
            }
        )
        acts = []
        for a in _data(r.get("legal_acts")) or []:
            t = _data(a.get("type")) or {}
            acts.append(
                {
                    "number": str(a.get("number") or "").strip(),
                    "type": str(t.get("title") or ""),
                    "url": str(a.get("url") or ""),
                }
            )
        out.append(
            Regime(
                id=r["id"],
                index=i,
                specification=re.sub(r"\s+", " ", str(r["specification"])).strip(),
                adopted_by=str(ab.get("title")) if isinstance(ab, Mapping) and ab.get("title") else None,
                country_iso2=str(ctry.get("code")).upper() if isinstance(ctry, Mapping) and ctry.get("code") else None,
                country_title=str(ctry.get("title")) if isinstance(ctry, Mapping) and ctry.get("title") else None,
                programmes=[str(p) for p in (r.get("programme") or [])],
                thematic=r.get("type") == 1,
                special=r.get("special") if isinstance(r.get("special"), int) else None,
                measures=measures,
                legal_acts=acts,
                last_amended=_local_day(r.get("amendment")),
                expires=_local_day(r.get("expiration")),
            )
        )
    if strict and len(out) < EU_MAP_MIN_REGIMES:
        raise _fail(f"EU map: {len(out)} regimes < expected {EU_MAP_MIN_REGIMES}", "incomplete")
    return out


MAX_FLAGS_CHARS = 3900  # atlas flags are <= 4000 characters (reva.atlas.tables.MAX_TEXT)


def fit_flags(flags: dict[str, Any]) -> str:
    """JSON flags within the atlas text limit: the longest list is shortened (``<key>_total`` keeps its full
    length; the KG statements keep every item) until the object fits."""
    from reva.atlas.tables import json_obj

    f = dict(flags)
    while len(out := json_obj(f)) > MAX_FLAGS_CHARS:
        lists = [(len(json.dumps(v)), k) for k, v in f.items() if isinstance(v, list) and len(v) > 1]
        if not lists:
            raise _fail("flags too long for an atlas row", "too_large")
        _, k = max(lists)
        f.setdefault(f"{k}_total", len(f[k]))
        f[k] = f[k][: max(1, len(f[k]) // 2)]
    return out


def eu_map_rows(regimes: Sequence[Regime], *, raw_sha256: str, day: date) -> list[dict[str, Any]]:
    from reva.atlas.tables import country_iso3, json_obj

    out: list[dict[str, Any]] = []
    per: dict[str, list[Regime]] = defaultdict(list)
    end = day + timedelta(days=1)
    for g in regimes:
        iso = country_iso3(g.country_iso2, "iso2") if g.country_iso2 else None
        geo = g.country_iso2 or ("thematic:" + "+".join(g.programmes or [str(g.id)]))
        flags = {
            "specification": g.specification,
            "regime_kind": "thematic" if g.thematic else "country",
            "listed_under_country": g.country_title,
            "measure_types": g.measures,
            "legal_acts": [a["number"] for a in g.legal_acts],
            "last_amended": g.last_amended,
            "expires": g.expires,
            "programme_codes": g.programmes,
            "provider_special": g.special,
            "semantics": "regime in force in the EU on the retrieval day, listed under this country on the EU "
            "Sanctions Map (the regime may target persons/entities linked to the country, not the State itself)",
        }
        out.append(
            {
                "source": "eusanctionsmap",
                "dataset": "regimes",
                "indicator": f"REGIME|regime={g.id}",
                "concept_id": None,
                "iso3": iso,
                "provider_geo": geo,
                "entity_qid": None,
                "period_start": day,
                "period_end": end,
                "value": None,
                "value_text": g.adopted_by or "not stated",
                "unit": "adopted by",
                "flags": fit_flags({k: v for k, v in flags.items() if v not in (None, [], "")}),
                "raw_sha256": raw_sha256,
                "selector": json_obj(file="api/v1/regime", path=f"$.data[{g.index}]", regime_id=g.id),
            }
        )
        if g.country_iso2 and not g.thematic:
            per[g.country_iso2].append(g)
    for iso2, gs in sorted(per.items()):
        out.append(
            {
                "source": "eusanctionsmap",
                "dataset": "regimes",
                "indicator": "REGIMES_LISTED",
                "concept_id": None,
                "iso3": country_iso3(iso2, "iso2"),
                "provider_geo": iso2,
                "entity_qid": None,
                "period_start": day,
                "period_end": end,
                "value": str(len(gs)),
                "value_text": None,
                "unit": "regimes",
                "flags": json_obj(
                    computed_from="EU Sanctions Map regime list",
                    method="count of country regimes listed under the country on the retrieval day",
                    regime_ids=sorted(g.id for g in gs),
                    adopted_by=dict(sorted(Counter(g.adopted_by or "not stated" for g in gs).items())),
                ),
                "raw_sha256": raw_sha256,
                "selector": json_obj(file="api/v1/regime", paths=[f"$.data[{g.index}]" for g in gs]),
            }
        )
    return out


def eu_map_kg(
    regimes: Sequence[Regime], *, raw_sha256: str, day: date, qid_for_iso3: Mapping[str, str]
) -> tuple[list[KgEntity], list[KgStatement]]:
    from reva.atlas.tables import country_iso3

    ents: list[KgEntity] = []
    stmts: list[KgStatement] = []
    for g in regimes:
        subj = f"eusanctionsmap:regime:{g.id}"
        sel = {"file": "api/v1/regime", "path": f"$.data[{g.index}]"}
        ents.append(
            KgEntity(
                qid=subj,
                kind="sanction_regime",
                labels={"en": f"{g.specification} (EU Sanctions Map)"},
                descriptions={
                    "en": "Sanctions regime in force in the EU (EU Sanctions Map, regime "
                    f"{g.id}; adopted by {g.adopted_by or 'not stated'})"
                },
            )
        )

        st = _Adder(stmts, str(g.id), subj, raw_sha256, sel)

        st(
            "listed_in_force",
            "eusanctionsmap:listed_in_force",
            value={"type": "time", "time": day.isoformat(), "precision": 11},
            qualifiers={"basis": "listed by the EU Sanctions Map on the retrieval day"},
        )
        iso = country_iso3(g.country_iso2, "iso2") if g.country_iso2 else None
        if iso and iso in qid_for_iso3:
            st(
                "country",
                "eusanctionsmap:listed_under_country",
                object_qid=qid_for_iso3[iso],
                qualifiers={"iso2": g.country_iso2, "iso3": iso},
            )
        if g.adopted_by:
            st("adopted_by", "eusanctionsmap:adopted_by", value=_s(g.adopted_by))
        for m in g.measures:
            st(f"measure:{hashlib.sha256(m.encode()).hexdigest()[:12]}", "eusanctionsmap:measure_type", value=_s(m))
        for a in g.legal_acts:
            if a["number"]:
                st(
                    f"act:{hashlib.sha256(a['number'].encode()).hexdigest()[:12]}",
                    "eusanctionsmap:legal_act",
                    value=_s(a["number"]),
                    qualifiers={k: v for k, v in (("type", a["type"]), ("url", a["url"])) if v},
                )
        for p in g.programmes if g.thematic else []:
            st(f"programme:{p}", "eusanctionsmap:thematic_programme", value=_s(p))
        if g.last_amended:
            st(
                "last_amended",
                "eusanctionsmap:last_amended",
                value={"type": "time", "time": g.last_amended, "precision": 11},
                qualifiers={"timezone": _BRUSSELS},
            )
        if g.expires:
            st(
                "expires",
                "eusanctionsmap:expires",
                value={"type": "time", "time": g.expires, "precision": 11},
                qualifiers={"timezone": _BRUSSELS},
            )
    return ents, stmts


OFAC_MIN_PROGRAMS = 25
_OFAC_ROW = re.compile(
    r'<a href="/sanctions-programs-and-country-information/([a-z0-9-]+)"[^>]*>(.*?)</a>.*?'
    r'<time datetime="(\d{4}-\d{2}-\d{2})T[^"]*"',
    re.S,
)


@dataclass
class Program:
    slug: str
    title: str
    last_updated: str
    index: int


def parse_ofac(body: bytes, *, strict: bool = True) -> list[Program]:
    try:
        page = body.decode("utf-8")
    except UnicodeDecodeError as e:
        raise _fail("OFAC page is not UTF-8") from e
    start = page.find("Active Sanctions Programs")
    end_ = page.find("</table>", start)
    if start < 0 or end_ < 0:
        raise _fail("OFAC page: no 'Active Sanctions Programs' table")
    out: list[Program] = []
    for i, tr in enumerate(re.split(r"<tr\b", page[start:end_])[1:]):
        m = _OFAC_ROW.search(tr)
        if not m:
            continue
        title = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", m.group(2)))).strip()
        out.append(Program(slug=m.group(1), title=title, last_updated=m.group(3), index=i))
    if len({p.slug for p in out}) != len(out):
        raise _fail("OFAC page: duplicate program slugs")
    if strict and len(out) < OFAC_MIN_PROGRAMS:
        raise _fail(f"OFAC page: {len(out)} programs < expected {OFAC_MIN_PROGRAMS}", "incomplete")
    return out


def ofac_rows(programs: Sequence[Program], *, raw_sha256: str, day: date) -> list[dict[str, Any]]:
    from reva.atlas.tables import country_iso3, json_obj

    out, per = [], defaultdict(list)
    end = day + timedelta(days=1)
    for p in programs:
        iso = country_iso3(p.slug, "ofac_program")
        sel = json_obj(
            file="sanctions-programs-and-country-information",
            table="Active Sanctions Programs",
            row=p.index + 1,
            program=p.slug,
        )
        flags = {
            "program_title": p.title,
            "program_last_updated": p.last_updated,
            "mapping": "title names exactly this country (registry/atlas/geo_aliases/a_sanctions.yaml)"
            if iso
            else "not tied to one country (thematic, regional or multi-country program)",
            "semantics": "program listed as active by OFAC on the retrieval day",
        }
        out.append(
            {
                "source": "ofac",
                "dataset": "programs",
                "indicator": f"PROGRAM|program={p.slug}",
                "concept_id": None,
                "iso3": iso,
                # OFAC has no geography code: the country the title names (alias table), else a fixed marker
                "provider_geo": iso or "program_without_single_country",
                "entity_qid": None,
                "period_start": day,
                "period_end": end,
                "value": None,
                "value_text": p.title,
                "unit": "program",
                "flags": json_obj(flags),
                "raw_sha256": raw_sha256,
                "selector": sel,
            }
        )
        if iso:
            per[iso].append(p)
    for iso, ps in sorted(per.items()):
        out.append(
            {
                "source": "ofac",
                "dataset": "programs",
                "indicator": f"ACTIVE_PROGRAMS|partner={iso}",
                "concept_id": None,
                "iso3": "USA",
                "provider_geo": "USA",
                "entity_qid": None,
                "period_start": day,
                "period_end": end,
                "value": str(len(ps)),
                "value_text": None,
                "unit": "programs",
                "flags": json_obj(
                    computed_from="OFAC Sanctions Programs and Country Information (active programs)",
                    method="count of active OFAC programs whose title names the partner country",
                    programs=[p.slug for p in ps],
                    partner_iso3=iso,
                    counterpart_iso3=iso,
                ),
                "raw_sha256": raw_sha256,
                "selector": json_obj(file="sanctions-programs-and-country-information", rows=[p.index + 1 for p in ps]),
            }
        )
    return out


def ofac_kg(
    programs: Sequence[Program], *, raw_sha256: str, day: date, qid_for_iso3: Mapping[str, str]
) -> tuple[list[KgEntity], list[KgStatement]]:
    from reva.atlas.tables import country_iso3

    ents: list[KgEntity] = []
    stmts: list[KgStatement] = []
    usa = qid_for_iso3.get("USA")
    for p in programs:
        subj = f"ofac:program:{p.slug}"
        sel = {"file": "sanctions-programs-and-country-information", "row": p.index + 1}
        ents.append(
            KgEntity(
                qid=subj,
                kind="sanction_program",
                labels={"en": f"{p.title} (US OFAC program)"},
                descriptions={"en": "US Treasury OFAC sanctions program (program level)"},
            )
        )

        st = _Adder(stmts, p.slug, subj, raw_sha256, sel)

        st("listed_active", "ofac:listed_active", value={"type": "time", "time": day.isoformat(), "precision": 11})
        st("last_updated", "ofac:last_updated", value={"type": "time", "time": p.last_updated, "precision": 11})
        if usa:
            st("imposed_by", "ofac:imposed_by", object_qid=usa)
        iso = country_iso3(p.slug, "ofac_program")
        if iso and iso in qid_for_iso3:
            st(
                "country",
                "ofac:program_country",
                object_qid=qid_for_iso3[iso],
                qualifiers={"basis": f"program title '{p.title}' names the country", "iso3": iso},
            )
    return ents, stmts


# =====================================================================================================
# Atlas sources and ingest
# =====================================================================================================

TIES_URL = "https://sanctions.web.unc.edu/wp-content/uploads/sites/18834/2021/04/TIESv4-1.xls"
EUSANCT_URL = "https://kondata.uni-konstanz.de/radar/de/dataset/lCfLfgbpdRKseQvp/datasetBinaryTreeStream"
EU_MAP_URL = "https://www.sanctionsmap.eu/api/v1/regime"
OFAC_URL = "https://ofac.treasury.gov/sanctions-programs-and-country-information"

RIGHTS: dict[str, dict[str, Any]] = {
    "atlas.ties": {
        "licence": "no licence stated (public academic download; citation requested)",
        "open_licence": False,
        "attribution": "Morgan, T. Clifton, Navin Bapat, and Yoshiharu Kobayashi. 2014. 'The Threat and Imposition "
        "of Sanctions: Updating the TIES dataset.' Conflict Management and Peace Science 31(5): 541-558. TIES 4.1.",
        "licence_evidence": "sanctions.web.unc.edu (read 2026-10-16): 'Please use the following citation when using "
        "TIES'; no licence or terms of use published",
    },
    "atlas.eusanct": {
        "licence": "CC BY 4.0",
        "licence_url": "https://creativecommons.org/licenses/by/4.0/",
        "open_licence": True,
        "attribution": "Weber, Patrick M.; Schneider, Gerald (2021): EUSANCT: Does Supranational Coercion Work? "
        "Universität Konstanz. DOI 10.48606/10. Weber & Schneider (2020), CMPS, doi 10.1177/0738894220948729.",
        "licence_evidence": "KonDATA record 10.48606/10 (read 2026-10-16): 'This work is licensed under CC BY 4.0'",
    },
    "atlas.eu_sanctionsmap": {
        "licence": "no licence stated (site disclaimer only); regime-level facts referencing EU/UN official acts",
        "open_licence": False,
        "attribution": "EU Sanctions Map (www.sanctionsmap.eu); legal acts: Official Journal of the EU / UN Security "
        "Council resolutions",
        "licence_evidence": "www.sanctionsmap.eu/api/v1/data articles 'disclaimer' (read 2026-10-16): no licence; "
        "'Only the legal acts published in the Official Journal of the European Union are authentic'",
    },
    "atlas.ofac_programs": {
        "licence": "public domain (US Government work, 17 U.S.C. 105)",
        "open_licence": True,
        "attribution": "U.S. Department of the Treasury, Office of Foreign Assets Control, 'Sanctions Programs and "
        "Country Information'",
    },
}


def atlas_source() -> list[Any]:
    from reva.atlas.sources import AtlasSourceSpec

    common: dict[str, Any] = {
        "version": CONNECTOR_VERSION,
        "parser_version": PARSER_VERSION,
        "min_interval_seconds": 2.0,
    }
    return [
        AtlasSourceSpec(
            name="atlas.ties",
            source_key="ties.unc",
            atlas_source="ties",
            provider="Threat and Imposition of Economic Sanctions (TIES) project, UNC Chapel Hill",
            licence=RIGHTS["atlas.ties"]["licence"],
            allowed_hosts=("sanctions.web.unc.edu", "cran.r-project.org"),
            datasets=("dyad_year", "country_year"),
            dataset_ids=("TIESv4-1.xls", f"countrycode-{COUNTRYCODE_VERSION}"),
            cadence_seconds=90 * 86400,
            planned_requests=4,
            max_response_bytes=10_000_000,
            field_mappings={
                "geo": "COW state codes (1000 = EEC/EU collectively)",
                "time": "case dates",
                "kg": "ties:case:<caseid>, ties:outcome:<caseid>",
            },
            notes="TIES 4.1 case-level workbook (BIFF8 .xls read by a bounded built-in reader); countrycode from "
            "CRAN for COW -> ISO3.",
            **common,
        ),
        AtlasSourceSpec(
            name="atlas.eusanct",
            source_key="eusanct.kondata",
            atlas_source="eusanct",
            provider="EUSANCT dataset (Weber & Schneider), University of Konstanz (KonDATA/RADAR)",
            licence="CC BY 4.0",
            allowed_hosts=("kondata.uni-konstanz.de", "cran.r-project.org"),
            datasets=("dyad_year", "country_year"),
            dataset_ids=("doi:10.48606/10", EUSANCT_MEMBER, f"countrycode-{COUNTRYCODE_VERSION}"),
            cadence_seconds=180 * 86400,
            planned_requests=4,
            max_response_bytes=100_000_000,
            field_mappings={
                "geo": "COW state codes; senders EU/US/UN",
                "time": "per-sender dates (Stata %td)",
                "kg": "eusanct:case:<caseid>, eusanct:outcome:<caseid>",
            },
            notes="RADAR dataset tar (64.8 MB, generated on request); the case-level CSV member is parsed.",
            **common,
        ),
        AtlasSourceSpec(
            name="atlas.eu_sanctionsmap",
            source_key="eu.sanctionsmap",
            atlas_source="eusanctionsmap",
            provider="EU Sanctions Map",
            licence=RIGHTS["atlas.eu_sanctionsmap"]["licence"],
            allowed_hosts=("www.sanctionsmap.eu",),
            datasets=("regimes",),
            dataset_ids=("api/v1/regime",),
            cadence_seconds=7 * 86400,
            planned_requests=2,
            max_response_bytes=10_000_000,
            field_mappings={
                "geo": "ISO 3166-1 alpha-2 country of the regime",
                "time": "retrieval day",
                "kg": "eusanctionsmap:regime:<id>",
            },
            notes="Regime-level JSON only; designation lists (persons/entities) are never requested.",
            **common,
        ),
        AtlasSourceSpec(
            name="atlas.ofac_programs",
            source_key="ofac.programs",
            atlas_source="ofac",
            provider="U.S. Department of the Treasury, Office of Foreign Assets Control (OFAC)",
            licence=RIGHTS["atlas.ofac_programs"]["licence"],
            allowed_hosts=("ofac.treasury.gov",),
            datasets=("programs",),
            dataset_ids=("sanctions-programs-and-country-information",),
            cadence_seconds=7 * 86400,
            planned_requests=2,
            max_response_bytes=5_000_000,
            field_mappings={
                "geo": "program slug -> ISO3 when the title names one country (scheme ofac_program)",
                "time": "retrieval day",
                "kg": "ofac:program:<slug>",
            },
            notes="Program list page only; the SDN / consolidated lists are never requested.",
            **common,
        ),
    ]


def _countries_for_kg() -> dict[str, str]:
    return load_qid_for_iso3()


def build_cases(dataset: str, raw_body: bytes, *, strict: bool = True) -> tuple[list[Case], str]:
    """(cases, file label used in selectors) from a raw TIES workbook or EUSANCT tar."""
    if dataset == "ties":
        sheet, cases = parse_ties(raw_body, strict=strict)
        return cases, f"{DATASET_META['ties']['file']}#{sheet}"
    member, csv_bytes = eusanct_csv_from_tar(raw_body)
    return parse_eusanct(csv_bytes, strict=strict), member.rsplit("/", 1)[-1]


def _summ(r: Mapping[str, Any]) -> dict[str, Any]:
    return {k: r[k] for k in ("dataset_key", "row_count", "created", "unmapped_provider_geo")}


async def _ingest_cases(run: Any, dataset: str) -> dict[str, Any]:
    crosswalk, cc_sha = await fetch_countrycode(run)
    if dataset == "ties":
        raw = await run.get(
            TIES_URL,
            key="ties:4.1",
            media_types=("application/vnd.ms-excel", "application/octet-stream"),
            max_bytes=10_000_000,
        )
        last_year, revision = 2005, "ties-4.1"
    else:
        raw = await run.get(
            EUSANCT_URL,
            key="eusanct:10.48606/10",
            media_types=("application/tar", "application/x-tar", "application/octet-stream"),
            max_bytes=100_000_000,
        )
        last_year, revision = EUSANCT_LAST_YEAR, "eusanct-1.0"
    strict = run.origin == "live"
    cases, file = build_cases(dataset, raw.body, strict=strict)
    sha = run.archive(raw)  # parsed first: nothing archived on schema drift
    dyad, ctry = case_atlas_rows(cases, raw_sha256=sha, crosswalk=crosswalk, last_year=last_year, file=file)
    rights = RIGHTS[run.spec.name]
    res: dict[str, Any] = {"cases": len(cases), "atlas": {}}
    note = {
        "ties": "COMPUTED from TIES 4.1 (1945-2005) case rows",
        "eusanct": "COMPUTED from EUSANCT 1.0 case level (EU/US/UN senders, 1989-2015) case rows",
    }[dataset]
    for table, rows, what in (
        ("dyad_year", dyad, "sender -> target (|partner=ISO3, flags.partner_iso3), distinct cases per year"),
        ("country_year", ctry, "per country and year, target/sender roles"),
    ):
        if run.wants(table):
            r = run.register(
                dataset=table,
                frame=atlas_frame(rows),
                raw_sha256s=[sha],
                rights=rights,
                coverage_note=f"{note}: {what}; a missing row = no coded case, never 0",
            )
            res["atlas"][table] = _summ(r)
    resolver = StateEntityResolver(crosswalk, _countries_for_kg())
    ents, stmts = case_kg_records(cases, raw_sha256=sha, resolver=resolver, file=file)
    if dataset == "eusanct":
        # same-case links need the TIES case list: read from the archived TIES raw when this database holds it
        ties_cases = _archived_ties_cases(run)
        if ties_cases is not None:
            links, rep = link_eusanct_to_ties(cases, ties_cases, raw_sha256=sha, file=file)
            stmts += links
            res["same_case_as"] = dict(rep)
        else:
            res["same_case_as"] = {"skipped": "no archived TIES workbook in this database (run atlas.ties first)"}
    ents += resolver.code_entities()
    stmts += code_correspondence(resolver, raw_sha256=cc_sha)
    claims = causal_claim_records(
        cases, source_key=run.spec.source_key, dataset_revision=revision, raw_sha256=sha, file=file
    )
    res["causal_claims"] = dict(Counter(c["relation"] for c in claims))
    res["kg_counts"] = {"entities": len(ents), "statements": len(stmts)}
    if run.dataset is None and kg_available(run.engine):
        res["kg"] = _load(run, ents, stmts, revision, [sha, cc_sha], raw.retrieved_at)
        res["claims_delivery"] = deliver_claims(
            run, claims, dataset=dataset, revision=revision, raw_sha256=sha, retrieved_at=raw.retrieved_at
        )
    res["unresolved_state_codes"] = {k: sorted(v)[:5] for k, v in resolver.unresolved.items()}
    return res


def _archived_ties_cases(run: Any) -> list[Case] | None:
    """TIES cases from the latest archived TIES workbook (same origin) in this database, if any."""
    from sqlalchemy import text

    try:
        with run.engine.connect() as conn:
            row = conn.execute(
                text(
                    """SELECT a.raw_sha256 FROM evidence.acquisition a
                         JOIN evidence.connector_version v ON v.id = a.connector_version_id
                        WHERE v.name = 'atlas.ties' AND a.origin = :o
                        ORDER BY a.retrieved_at DESC, a.recorded_at DESC LIMIT 1"""
                ),
                {"o": run.origin},
            ).first()
        if row is None:
            return None
        body = run.store.get("raw", row[0])
        return parse_ties(body, strict=False)[1]
    except Exception:  # optional enrichment: no link rather than a failed run
        return None


def _load(
    run: Any, ents: list[KgEntity], stmts: list[KgStatement], revision: str, shas: list[str], retrieved_at: Any
) -> dict[str, Any]:
    from reva.adapters.connectors.wikidata import load_allowlist
    from reva.kg.harvest import kg_properties as wikidata_properties
    from reva.kg.model import register_properties

    wd = [p for p in wikidata_properties(load_allowlist()) if p.pid in KG_WIKIDATA_PIDS]
    if len(wd) != len(KG_WIKIDATA_PIDS):
        raise ValueError(f"{KG_WIKIDATA_PIDS} must all be in registry/kg/properties.yaml")
    register_properties(run.engine, kg_properties() + wd)
    return load_kg(
        run.engine,
        run.store,
        ents,
        stmts,
        source_key=run.spec.source_key,
        source_revision=revision,
        raw_sha256s=sorted(set(shas)),
        retrieved_at=retrieved_at,
        origin=run.origin,
    )


async def _ingest_snapshot(run: Any) -> dict[str, Any]:
    eu = run.spec.name == "atlas.eu_sanctionsmap"
    url = EU_MAP_URL if eu else OFAC_URL
    raw = await run.get(
        url,
        key="regimes" if eu else "programs",
        media_types=("application/json",) if eu else ("text/html",),
        max_bytes=10_000_000,
    )
    strict = run.origin == "live"
    items: Sequence[Any] = parse_eu_map(raw.body, strict=strict) if eu else parse_ofac(raw.body, strict=strict)
    sha = run.archive(raw)
    day = raw.retrieved_at.astimezone(UTC).date() if isinstance(raw.retrieved_at, datetime) else date.today()
    qids = _countries_for_kg()
    if eu:
        rows = eu_map_rows(items, raw_sha256=sha, day=day)
        ents, stmts = eu_map_kg(items, raw_sha256=sha, day=day, qid_for_iso3=qids)
        table, note = "regimes", "EU Sanctions Map regimes in force on the retrieval day (regime level only)"
    else:
        rows = ofac_rows(items, raw_sha256=sha, day=day)
        ents, stmts = ofac_kg(items, raw_sha256=sha, day=day, qid_for_iso3=qids)
        table, note = "programs", "OFAC active sanctions programs on the retrieval day (program level only)"
    res: dict[str, Any] = {"items": len(items), "atlas": {}}
    if run.wants(table):
        r = run.register(
            dataset=table, frame=atlas_frame(rows), raw_sha256s=[sha], rights=RIGHTS[run.spec.name], coverage_note=note
        )
        res["atlas"][table] = _summ(r)
    res["kg_counts"] = {"entities": len(ents), "statements": len(stmts)}
    if run.dataset is None and kg_available(run.engine):
        res["kg"] = _load(run, ents, stmts, f"{run.spec.source_key}@{day.isoformat()}", [sha], raw.retrieved_at)
    return res


async def atlas_ingest(run: Any) -> dict[str, Any]:
    name = run.spec.name
    if name == "atlas.ties":
        return await _ingest_cases(run, "ties")
    if name == "atlas.eusanct":
        return await _ingest_cases(run, "eusanct")
    if name in ("atlas.eu_sanctionsmap", "atlas.ofac_programs"):
        return await _ingest_snapshot(run)
    raise ValueError(f"unknown sanctions source {name}")
