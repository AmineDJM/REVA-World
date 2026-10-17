"""Political leaders: Archigos 4.1 (1875-2015) and the REIGN-format leader list (1945-2026) (agent A-COUPS).

Sources and access (checked 2026-10-16)
---------------------------------------
* **Archigos 4.1** (Goemans, Gleditsch & Chiozza; updated to 2015-12-31): ``Archigos_4.1_stata14.dta`` on
  ``www.rochester.edu/college/faculty/hgoemans/`` (Stata 118 file, 3,409 leader spells, 2.9 MB; served without a
  content type). **No licence stated**: the authors "would appreciate it if users would cite" Goemans, Gleditsch &
  Chiozza (2009), Journal of Peace Research 46(2). Free download, no registration. Unit = leader spell of the
  *effective* ruler; entry (Regular / Irregular / Foreign Imposition / Unknown), exit and the finer ``exitcode``
  (codebook pp. 2-3), post-tenure fate, birth/death years, DBpedia URI.
* **REIGN-format leader list** (``leaderlist.xlsx`` linked on ``jonathanmpowell.com/coups/``, updated
  2026-04-14 per its ``updated`` cell): same columns as REIGN's ``leader_list_8_21.csv`` (Bell, Besaw & Frank,
  One Earth Future; REIGN stopped publishing in August 2021), leaders 1945-2026, start day, end month. No
  manner of entry/exit. **No licence stated** (REIGN asked for a citation; the hosting page states none).
  Kept separate from Archigos (different ids, different end precision); never merged by name.

Outputs
-------
* atlas ``archigos.leaders_cy`` -- **computed**, exact counts per COW state x year: ``LEADERS_IN_OFFICE``
  (spells overlapping the year), ``LEADER_ENTRIES`` / ``LEADER_ENTRIES|entry=<manner>``, ``LEADER_EXITS`` /
  ``LEADER_EXITS|exit=<manner>`` / ``LEADER_EXITS|exitcode=<code>`` (the provider labels verbatim in flags;
  "Still in Office" is censoring at 2015-12-31, not an exit). Non-zero cells only.
* atlas ``reign.leaders_cy`` -- ``LEADERS_IN_OFFICE``, ``LEADER_ENTRIES`` from the leader list.
* KG (``archigos.leaders``): leaders ``archigos:leader:<leadid>`` (kind person), one ``archigos:leader_of``
  statement per spell (start/end day precision, entry/exit/exitcode/fate as qualifiers, verbatim), birth year,
  death date, the DBpedia/English-Wikipedia title Archigos gives; irregular and foreign removals as events
  ``archigos:exit:<obsid>`` (``P585`` day, ``archigos:removal_of``, ``archigos:removed_from``,
  ``archigos:exit_manner``).
* KG (``reign.leaderlist``): ``reign:leader:<leadid>`` + ``reign:leader_of`` spells.
* **No causal claim** (connector 1.1.0, AUDFIX audit M5): Archigos codes ONE fact per removal -- the exit and its
  ``exitcode`` -- not a coup followed by a removal. Until 1.0.0 REVA split it into a coup event
  ``archigos:coup:<obsid>`` and a ``coded_outcome`` claim coup -> removal (327 claims, dated by copying the exit
  day, labelling "other government actors" removals as coups and quoting the "without foreign support" passage for
  removals with foreign support). Now the removal event ``archigos:exit:<obsid>`` carries the exitcode verbatim,
  ``removed_by`` / ``foreign_support`` (the two parts of that label, verbatim) and the codebook passage *of that
  exact exitcode* when the codebook has one (only "Removed by Military, without Foreign Support" -- "the classic
  military coup" -- and "Removed by Other Government Actors, without Foreign Support"); the claims are retracted
  through the loader (empty complete delivery), the ``archigos:coup:*`` statements are retracted and their entities
  re-emitted as ``superseded_record``.
* **Codebook** (audit M4): ``Archigos_4.1.pdf`` is fetched, archived and compared with its pinned sha256; the quoted
  passages carry the archived sha256 and the physical PDF page.
* **Context link to Powell & Thyne** (P&T list fetched in the same run): when a *successful* P&T coup has the same
  COW state code and the same calendar day as an Archigos removal by the military or other government actors, a
  context statement ``coup:same_state_and_day_as`` links the P&T event to the Archigos removal event -- never
  ``same_as``, never causal, never by name or by near dates; near misses (1-31 days apart) are counted in the
  summary, not linked. P&T do not code leaders.
* **Wikidata ``same_as``** (run option ``wikidata=1``): Archigos's DBpedia URI is an English-Wikipedia title;
  one WDQS query per 250 titles maps the exact article IRI (sitelink) to its item and reads P31/P569. A link is
  kept only when the item is a human (Q5) and, when both are known, Wikidata's birth year equals Archigos's
  ``yrborn``; otherwise the reason is counted. Never by name.
"""

from __future__ import annotations

import hashlib
import re
import struct
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import quote, unquote

from reva.adapters.connectors.atlas_coups import COUP_PROPERTIES as _COUP_PROPS
from reva.adapters.connectors.atlas_coups import HOST as PT_HOST
from reva.adapters.connectors.atlas_coups import PAGE_URL as PT_PAGE_URL
from reva.adapters.connectors.atlas_coups import (
    CoupEvent,
    csv_file_name,
    fetch_coup_list,
    load_kg_records,
    parse_csv,
    wikidata_time_properties,
)
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
    load_qid_for_iso3,
)
from reva.kernel.errors import SourceResponseError


class LeaderParseError(SourceResponseError):
    pass


def _drift(msg: str) -> LeaderParseError:
    return LeaderParseError(msg, retryable=False, kind="schema_drift")


# ============================================================================= Stata .dta reader (117/118/119)
#: Bounded, read-only reader of Stata's XML-tagged binary format (releases 117-119): fixed-width strings, strL,
#: byte/int/long/float/double with Stata missing values -> None. No value labels needed here (Archigos stores
#: categories as strings). Raw bytes are untrusted: every offset and size is checked.
DTA_MAX_ROWS = 500_000
DTA_MAX_VARS = 2_000
_MISSING_INT = {65530: 100, 65529: 32740, 65528: 2_147_483_620}  # values above are missing (.  .a - .z)
_FLOAT_MISSING = 2.0**127
_DOUBLE_MISSING = 2.0**1023


@dataclass
class DtaFrame:
    release: int
    nobs: int
    names: list[str]
    types: list[int]
    data_label: str
    columns: dict[str, list[Any]]
    #: (row index, variable) of strings that were not valid UTF-8 and were decoded as Windows-1252
    encoding_fallbacks: list[tuple[int, str]] = field(default_factory=list)


def _expect(buf: bytes, pos: int, tag: bytes) -> int:
    if buf[pos : pos + len(tag)] != tag:
        raise _drift(f"dta: expected {tag!r} at byte {pos}")
    return pos + len(tag)


def _cstr(raw: bytes) -> bytes:
    i = raw.find(b"\x00")
    return raw if i < 0 else raw[:i]


def read_dta(body: bytes, *, max_rows: int = DTA_MAX_ROWS) -> DtaFrame:
    """Parse a Stata 117/118/119 ``.dta`` file (see module section comment)."""
    p = _expect(body, 0, b"<stata_dta><header><release>")
    try:
        release = int(body[p : p + 3])
    except ValueError as e:
        raise _drift("dta: unreadable release") from e
    if release not in (117, 118, 119):
        raise _drift(f"dta: release {release} not supported (117-119)")
    p = _expect(body, p + 3, b"</release><byteorder>")
    bo = body[p : p + 3]
    if bo not in (b"LSF", b"MSF"):
        raise _drift("dta: byte order")
    e = "<" if bo == b"LSF" else ">"
    p = _expect(body, p + 3, b"</byteorder><K>")
    kfmt = "I" if release == 119 else "H"
    (k,) = struct.unpack_from(e + kfmt, body, p)
    p = _expect(body, p + struct.calcsize(kfmt), b"</K><N>")
    nfmt = "I" if release == 117 else "Q"
    (n,) = struct.unpack_from(e + nfmt, body, p)
    p = _expect(body, p + struct.calcsize(nfmt), b"</N><label>")
    lfmt = "B" if release == 117 else "H"
    (ll,) = struct.unpack_from(e + lfmt, body, p)
    p += struct.calcsize(lfmt)
    data_label = body[p : p + ll].decode("utf-8", errors="replace")
    p = _expect(body, p + ll, b"</label><timestamp>")
    tl = body[p]
    p = _expect(body, p + 1 + tl, b"</timestamp></header><map>")
    if not (0 < k <= DTA_MAX_VARS) or n > max_rows:
        raise _drift(f"dta: {k} variables / {n} rows outside bounds")
    offsets = struct.unpack_from(e + "14Q", body, p)
    if list(offsets) != sorted(offsets) or offsets[-1] > len(body):
        raise _drift("dta: section map not increasing or beyond the file")
    w_name, w_fmt, w_lbl = (33, 49, 81) if release == 117 else (129, 57, 321)
    q = _expect(body, offsets[2], b"<variable_types>")
    types = list(struct.unpack_from(e + f"{k}H", body, q))
    q = _expect(body, offsets[3], b"<varnames>")
    names = []
    for i in range(k):
        raw = _cstr(body[q + i * w_name : q + (i + 1) * w_name])
        names.append(raw.decode("utf-8" if release >= 118 else "latin-1"))
    if len(set(names)) != k:
        raise _drift("dta: duplicate variable names")
    del w_fmt, w_lbl  # formats / variable labels are not needed
    # row layout
    codes: list[str] = []
    for t in types:
        if 1 <= t <= 2045:
            codes.append(f"{t}s")
        elif t == 32768:
            codes.append("8s")
        elif t in (65526, 65527, 65528, 65529, 65530):
            codes.append({65526: "d", 65527: "f", 65528: "i", 65529: "h", 65530: "b"}[t])
        else:
            raise _drift(f"dta: unknown variable type {t}")
    row = struct.Struct(e + "".join(codes))
    d0 = _expect(body, offsets[9], b"<data>")
    d1 = d0 + row.size * n
    if body[d1 : d1 + 7] != b"</data>":
        raise _drift("dta: data section size does not match N x row width (truncated?)")
    strls = _read_strls(body, offsets[10], release, e) if 32768 in types else {}
    cols: dict[str, list[Any]] = {nm: [] for nm in names}
    fallbacks: list[tuple[int, str]] = []
    for ri, vals in enumerate(row.iter_unpack(body[d0:d1])):
        for nm, t, v in zip(names, types, vals, strict=True):
            if t <= 2045:
                cols[nm].append(_decode(_cstr(v), ri, nm, fallbacks, release))
            elif t == 32768:
                if release == 117:
                    vv, oo = struct.unpack(e + "II", v)
                elif release == 118:
                    vv, oo = (
                        int.from_bytes(v[:2], "little" if e == "<" else "big"),
                        int.from_bytes(v[2:], "little" if e == "<" else "big"),
                    )
                else:
                    vv, oo = (
                        int.from_bytes(v[:3], "little" if e == "<" else "big"),
                        int.from_bytes(v[3:], "little" if e == "<" else "big"),
                    )
                if (vv, oo) == (0, 0):
                    cols[nm].append("")
                elif (vv, oo) not in strls:
                    raise _drift(f"dta: strL ({vv},{oo}) missing")
                else:
                    cols[nm].append(_decode(strls[(vv, oo)], ri, nm, fallbacks, release))
            elif t in _MISSING_INT:
                cols[nm].append(None if v > _MISSING_INT[t] else v)
            elif t == 65527:
                cols[nm].append(None if abs(v) >= _FLOAT_MISSING or v != v else v)
            else:
                cols[nm].append(None if abs(v) >= _DOUBLE_MISSING or v != v else v)
    return DtaFrame(release, n, names, types, data_label, cols, fallbacks)


def _decode(raw: bytes, ri: int, nm: str, fallbacks: list[tuple[int, str]], release: int) -> str:
    if release == 117:
        return raw.decode("latin-1")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        fallbacks.append((ri, nm))
        return raw.decode("cp1252", errors="replace")


def _read_strls(body: bytes, off: int, release: int, e: str) -> dict[tuple[int, int], bytes]:
    p = _expect(body, off, b"<strls>")
    out: dict[tuple[int, int], bytes] = {}
    ofmt = "I" if release == 117 else "Q"
    head = struct.Struct(e + "I" + ofmt + "BI")
    while body[p : p + 3] == b"GSO":
        v, o, t, ln = head.unpack_from(body, p + 3)
        p += 3 + head.size
        if p + ln > len(body):
            raise _drift("dta: strL beyond the file")
        data = body[p : p + ln]
        out[(v, o)] = data[:-1] if t == 130 and data.endswith(b"\x00") else data
        p += ln
    _expect(body, p, b"</strls>")
    return out


# ============================================================================= Archigos 4.1
ARCHIGOS_SOURCE_KEY = "archigos.leaders"
ARCHIGOS_NAME = "atlas.archigos"
ARCHIGOS_VERSION_CONNECTOR = "1.1.1"  # 1.1.0 (AUDFIX M3/M4/M5): no coup -> removal claim; codebook archived;
# 1.1.1: the KG source revision names the REVA representation (changed qualifiers are new revisions, not conflicts)
ARCHIGOS_PARSER = "atlas-archigos-1"
ARCHIGOS_RELEASE = "4.1"
ARCHIGOS_HOST = "www.rochester.edu"
ARCHIGOS_URL = f"https://{ARCHIGOS_HOST}/college/faculty/hgoemans/Archigos_4.1_stata14.dta"
ARCHIGOS_FILE = "Archigos_4.1_stata14.dta"
ARCHIGOS_CODEBOOK_URL = f"https://{ARCHIGOS_HOST}/college/faculty/hgoemans/Archigos_4.1.pdf"
#: sha256 of the codebook PDF the definitions below were copied from (fetched 2026-10-16, 3,266,494 bytes)
ARCHIGOS_CODEBOOK_SHA256 = "6be0ec5c9dbd5510d3a4d02d276ec5094dbc942147432349db55b35b520abcdf"
ARCHIGOS_MIN_ROWS = 3_300
WDQS_HOST = "query.wikidata.org"
WDQS_URL = f"https://{WDQS_HOST}/sparql"
WDQS_CHUNK = 250  # titles per POST: the body stays well under the 64 KiB POST limit

ARCHIGOS_RIGHTS: dict[str, str] = {
    "licence": "No licence stated (checked 2026-10-16): free public download without registration; the authors ask "
    "users to cite Goemans, Gleditsch & Chiozza (2009). No redistribution or commercial-use grant.",
    "licence_url": "https://www.rochester.edu/college/faculty/hgoemans/data.htm",
    "attribution": "Goemans, Henk E., Kristian Skrede Gleditsch & Giacomo Chiozza (2009). Introducing Archigos: A "
    "Dataset of Political Leaders. Journal of Peace Research 46(2): 269-283. Archigos version 4.1 (2016).",
    "commercial_use": "not_granted",
    "redistribution": "not_granted",
}

ARCHIGOS_COLUMNS = (
    "obsid",
    "leadid",
    "ccode",
    "idacr",
    "leader",
    "startdate",
    "enddate",
    "entry",
    "exit",
    "exitcode",
    "prevtimesinoffice",
    "posttenurefate",
    "gender",
    "yrborn",
    "yrdied",
    "deathdate",
    "dbpediauri",
)
ENTRY_VALUES = frozenset({"Regular", "Irregular", "Foreign Imposition", "Unknown"})
EXIT_VALUES = frozenset(
    {
        "Regular",
        "Irregular",
        "Natural Death",
        "Still in Office",
        "Foreign",
        "Retired Due to Ill Health",
        "Suicide",
        "Unknown",
    }
)
EXITCODE_VALUES = frozenset(
    {
        "Regular",
        "Still in Office",
        "Unknown",
        "Irregular, Other",
        "Assassination by Unsupported Individual",
        "Popular Protest, with Foreign Support",
        "Popular Protest, without Foreign Support",
        "Removed by Military, with Foreign Support",
        "Removed by Military, without Foreign Support",
        "Removed by Other Government Actors, with Foreign Support",
        "Removed by Other Government Actors, without Foreign Support",
        "Removed by Rebels, with Foreign Support",
        "Removed by Rebels, without Foreign Support",
        "Removed in Military Power Struggle Short of Coup",
        "Removed through Threat of Foreign Force",
    }
)
FATE_VALUES = frozenset(
    {
        "OK",
        "Exile",
        "Imprisonment",
        "Death",
        "Suicide",
        "Leader Still in Office",
        "Missing: Natural Death within Six Months of Losing Office",
        "Missing: No Information Found",
        "Missing: Left Office after 2014-12-31",
    }
)
#: exitcodes whose removal Archigos attributes to actors inside the state apparatus (the military -- "the classic
#: military coup" -- or other government actors): the only ones linked to a P&T coup event
COUP_LINK_EXITCODES = {
    "Removed by Military, without Foreign Support": "exitcode_military",
    "Removed by Military, with Foreign Support": "exitcode_military",
    "Removed by Other Government Actors, without Foreign Support": "exitcode_other_government_actors",
    "Removed by Other Government Actors, with Foreign Support": "exitcode_other_government_actors",
}
REMOVAL_EXITS = frozenset({"Irregular", "Foreign"})
CENSOR_DATE = date(2015, 12, 31)

#: Verbatim passages of the Archigos 4.1 codebook (PDF sha256 above; physical pages in CODEBOOK_PAGES).
CODEBOOK_DEFINITIONS: dict[str, str] = {
    "exit_irregular": "Removal from office is coded as Irregular when the leader was removed in contravention of "
    "explicit rules and established conventions. Most Irregular removals from office are done by domestic forces "
    "(see below for more). Irregular removal from office is overwhelmingly the result of the threat or use of "
    "force as exemplified in coups, (popular) revolts and assassinations.",
    "exitcode": "The variable EXITCODE records in more detail how the leader lost power. We offer a finer coding "
    "scheme for all leaders who were coded EXIT==Irregular or EXIT==Foreign, in other words, all leaders who were "
    "removed irregularly or deposed by a foreign state.",
    "exitcode_military": "the great majority of irregular removals (277) are the result of action by domestic "
    "military actors without foreign support, the classic “military coup.”",
    "exitcode_other_government_actors": "Next in order of frequency are the 42 leaders who were removed by other "
    "government actors, without direct foreign support;",
}
#: physical page(s) of each passage in the pinned codebook PDF (checked by tests/unit/test_audfix_codebooks.py)
CODEBOOK_PAGES: dict[str, list[int]] = {
    "exit_irregular": [9],
    "exitcode": [9],
    "exitcode_military": [10],
    "exitcode_other_government_actors": [10],
}
#: the exitcodes the codebook describes in words (its own label, not the "with Foreign Support" variants): M5
EXITCODE_PASSAGE = {
    "Removed by Military, without Foreign Support": "exitcode_military",
    "Removed by Other Government Actors, without Foreign Support": "exitcode_other_government_actors",
}
CONTEXT_METHOD = (
    "context only (not identity, not causal): an Archigos irregular removal by the military or other government "
    "actors and a successful Powell & Thyne coup share the Correlates of War state code and the calendar day "
    "(Archigos enddate == P&T event day)"
)
SUPERSEDED_NOTE = (
    "superseded record (Archigos connector 1.1.0): until 1.0.0 REVA split one Archigos exitcode into a coup event "
    "and a removal; Archigos codes a single removal, see the removal event"
)


def archigos_codebook() -> Any:
    from reva.adapters.connectors.causdata_common import PinnedCodebook

    return PinnedCodebook(
        key="archigos_codebook",
        title=f"Archigos {ARCHIGOS_RELEASE} codebook (Archigos_4.1.pdf)",
        url=ARCHIGOS_CODEBOOK_URL,
        sha256=ARCHIGOS_CODEBOOK_SHA256,
        media_types=("application/pdf", "application/octet-stream"),
        max_bytes=5_000_000,
        extraction="poppler pdftotext -layout, whitespace normalised",
        quotes={k: {"pages": v} for k, v in CODEBOOK_PAGES.items()},
    )


def exit_detail(exitcode: str) -> dict[str, Any]:
    """The two parts of an Archigos exitcode label, verbatim (``Removed by <who>, with[out] Foreign Support``)."""
    out: dict[str, Any] = {}
    m = re.fullmatch(r"(Removed by .+?|Popular Protest), (with|without) Foreign Support", exitcode)
    if m:
        out["removed_by"] = m.group(1)
        out["foreign_support"] = m.group(2) == "with"
    return out


@dataclass(frozen=True)
class Spell:
    row: int  # 0-based row in the .dta
    obsid: str
    leadid: str
    ccode: int
    idacr: str
    leader: str
    start: date
    end: date
    entry: str
    exit: str
    exitcode: str
    prevtimes: int | None
    fate: str
    yrborn: int | None
    yrdied: int | None
    deathdate: date | None
    enwiki_title: str | None

    @property
    def censored(self) -> bool:
        return self.exit == "Still in Office"

    @property
    def leader_id(self) -> str:
        return f"archigos:leader:{self.leadid}"

    @property
    def exit_event_id(self) -> str:
        return f"archigos:exit:{self.obsid}"


def _date_or_none(s: str | None, what: str) -> date | None:
    s = (s or "").strip()
    if s in ("", "NA", "."):
        return None
    try:
        return date.fromisoformat(s)
    except ValueError as e:
        raise _drift(f"archigos: {what} {s!r} is not YYYY-MM-DD") from e


_PROOFPOINT_RE = re.compile(r"[?&]u=([^&]+)")
DBPEDIA_PREFIX = "http://dbpedia.org/resource/"


def dbpedia_title(uri: str | None) -> str | None:
    """English-Wikipedia title from Archigos's ``dbpediauri`` (a Proofpoint URL-Defense v2 wrapper of a DBpedia
    resource URI in 4.1): ``-XX`` -> ``%XX``, ``_`` -> ``/``, percent-decoding twice. None when absent."""
    s = (uri or "").strip()
    if s in ("", "NA"):
        return None
    if "urldefense.proofpoint.com/v2/url" in s:
        m = _PROOFPOINT_RE.search(s)
        if not m:
            raise _drift("archigos: unreadable URL-Defense wrapper")
        s = unquote(re.sub(r"-([0-9A-F]{2})", r"%\1", m.group(1)).replace("_", "/"))
    s = s.replace("https://dbpedia.org/", "http://dbpedia.org/")
    if not s.startswith(DBPEDIA_PREFIX) or len(s) == len(DBPEDIA_PREFIX):
        raise _drift(f"archigos: not a DBpedia resource URI: {s[:80]!r}")
    return unquote(s[len(DBPEDIA_PREFIX) :]).replace("_", " ")


def _year(v: Any) -> int | None:
    """Archigos year codes: -999 = missing (codebook p. 4), -777 = alive / presumed alive (the codebook says NA)."""
    return None if v is None or int(v) < 0 else int(v)


def parse_archigos(body: bytes, *, strict: bool = True) -> tuple[list[Spell], dict[str, Any]]:
    """Every row checked against the codebook categories; returns spells and a parse report."""
    fr = read_dta(body)
    miss = [c for c in ARCHIGOS_COLUMNS if c not in fr.columns]
    if miss:
        raise _drift(f"archigos: columns missing {miss}")
    if strict and fr.nobs < ARCHIGOS_MIN_ROWS:
        raise LeaderParseError(f"archigos: {fr.nobs} rows (truncated?)", retryable=False, kind="incomplete")
    c = fr.columns
    spells: list[Spell] = []
    seen: set[str] = set()
    for i in range(fr.nobs):
        entry, ex, code, fate = c["entry"][i], c["exit"][i], c["exitcode"][i], c["posttenurefate"][i]
        bad = [
            (n, v)
            for n, v, allowed in (
                ("entry", entry, ENTRY_VALUES),
                ("exit", ex, EXIT_VALUES),
                ("exitcode", code, EXITCODE_VALUES),
                ("posttenurefate", fate, FATE_VALUES),
            )
            if v not in allowed
        ]
        if bad:
            raise _drift(f"archigos row {i}: categories outside the codebook {bad}")
        obsid = c["obsid"][i]
        if not obsid or obsid in seen or not re.match(r"^[A-Za-z0-9_.:-]{1,64}$", obsid):
            raise _drift(f"archigos row {i}: obsid {obsid!r} missing, duplicated or not a key")
        seen.add(obsid)
        leadid = c["leadid"][i]
        if not re.match(r"^[A-Za-z0-9-]{8,64}$", leadid or ""):
            raise _drift(f"archigos row {i}: leadid {leadid!r}")
        start = _date_or_none(c["startdate"][i], "startdate")
        end = _date_or_none(c["enddate"][i], "enddate")
        if start is None or end is None or end < start:
            raise _drift(f"archigos row {i}: start/end {c['startdate'][i]!r}/{c['enddate'][i]!r}")
        cc = c["ccode"][i]
        if cc is None:
            raise _drift(f"archigos row {i}: ccode missing")
        spells.append(
            Spell(
                row=i,
                obsid=obsid,
                leadid=leadid,
                ccode=int(cc),
                idacr=c["idacr"][i],
                leader=c["leader"][i].strip(),
                start=start,
                end=end,
                entry=entry,
                exit=ex,
                exitcode=code,
                prevtimes=c["prevtimesinoffice"][i],
                fate=fate,
                yrborn=_year(c["yrborn"][i]),
                yrdied=_year(c["yrdied"][i]),
                deathdate=_date_or_none(c["deathdate"][i], "deathdate"),
                enwiki_title=dbpedia_title(c["dbpediauri"][i]),
            )
        )
    report = {
        "release": fr.release,
        "rows": fr.nobs,
        "leaders": len({s.leadid for s in spells}),
        "data_label": fr.data_label,
        "encoding_fallbacks": [{"row": r, "variable": v} for r, v in fr.encoding_fallbacks],
        "censored_spells": sum(s.censored for s in spells),
        "yrdied_codes": dict(Counter(str(v) for v in c["yrdied"] if v is not None and int(v) < 0)),
        "yrborn_codes": dict(Counter(str(v) for v in c["yrborn"] if v is not None and int(v) < 0)),
        "with_enwiki_title": sum(s.enwiki_title is not None for s in spells),
    }
    return spells, report


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def archigos_cy_rows(
    spells: Sequence[Spell], *, raw_sha256: str, crosswalk: StateCodeCrosswalk, file: str = ARCHIGOS_FILE
) -> list[dict[str, Any]]:
    from reva.atlas.tables import json_obj, year_period

    cells: dict[tuple[str, int, int], list[Spell]] = defaultdict(list)
    labels: dict[str, tuple[str, str]] = {}
    for s in spells:
        for y in range(s.start.year, s.end.year + 1):
            cells[("LEADERS_IN_OFFICE", s.ccode, y)].append(s)
        cells[("LEADER_ENTRIES", s.ccode, s.start.year)].append(s)
        ind = f"LEADER_ENTRIES|entry={_slug(s.entry)}"
        labels[ind] = ("entry", s.entry)
        cells[(ind, s.ccode, s.start.year)].append(s)
        if s.censored:
            continue
        cells[("LEADER_EXITS", s.ccode, s.end.year)].append(s)
        ind = f"LEADER_EXITS|exit={_slug(s.exit)}"
        labels[ind] = ("exit", s.exit)
        cells[(ind, s.ccode, s.end.year)].append(s)
        if s.exit in REMOVAL_EXITS:
            ind = f"LEADER_EXITS|exitcode={_slug(s.exitcode)}"
            labels[ind] = ("exitcode", s.exitcode)
            cells[(ind, s.ccode, s.end.year)].append(s)
    methods = {
        "LEADERS_IN_OFFICE": "count of Archigos leader spells whose [startdate, enddate] overlaps the calendar year",
        "LEADER_ENTRIES": "count of Archigos leader spells starting in the year",
        "LEADER_EXITS": "count of Archigos leader spells ending in the year (exit other than 'Still in Office')",
    }
    out = []
    for (ind, cc, year), ss in sorted(cells.items()):
        res = crosswalk.resolve("cow", cc, date(year, 7, 1))
        base = ind.split("|")[0]
        flags: dict[str, Any] = {
            "computed_from": f"Archigos {ARCHIGOS_RELEASE}",
            "method": methods[base],
            "obsids": [s.obsid for s in ss][:60],
            "idacr": sorted({s.idacr for s in ss}),
            "code_resolution": res.reason,
        }
        if ind in labels:
            flags["archigos_variable"], flags["archigos_value"] = labels[ind]
        if len(ss) > 60:
            flags["obsids_total"] = len(ss)
        if res.iso3 is None and res.label:
            flags["historical_state"] = res.label
        if base == "LEADERS_IN_OFFICE" and year == CENSOR_DATE.year and any(s.censored for s in ss):
            flags["censored"] = "spells 'Still in Office' end at 2015-12-31 (Archigos coverage end)"
        start, end = year_period(year)
        out.append(
            {
                "source": "archigos",
                "dataset": "leaders_cy",
                "indicator": ind,
                "concept_id": None,
                "iso3": res.iso3,
                "provider_geo": f"cow:{cc}",
                "entity_qid": res.qid,
                "period_start": start,
                "period_end": end,
                "value": str(len(ss)),
                "value_text": None,
                "unit": "leaders",
                "flags": json_obj(flags),
                "raw_sha256": raw_sha256,
                "selector": json_obj(file=file, rows=[s.row for s in ss][:200], rows_total=len(ss)),
            }
        )
    return out


# ----------------------------------------------------------------------------- coup join


@dataclass(frozen=True)
class CoupRemoval:
    """A leader removal Archigos codes as done by the military or other government actors (exit Irregular): the
    population of the Powell & Thyne context link (not an event of its own, not a claim)."""

    spell: Spell
    codebook_term: str
    #: a successful P&T coup in the same COW state on the same day (context edge only)
    pt_event: CoupEvent | None = None

    @property
    def event_id(self) -> str:
        """The superseded 1.0.0 coup record id (re-emitted as ``superseded_record`` only)."""
        return f"archigos:coup:{self.spell.obsid}"

    @property
    def perpetrator(self) -> str:
        return "military" if "Military" in self.spell.exitcode else "other_government_actors"

    @property
    def foreign_support(self) -> bool:
        return "with Foreign Support" in self.spell.exitcode


def coup_removals(spells: Sequence[Spell], events: Sequence[CoupEvent]) -> tuple[list[CoupRemoval], dict[str, Any]]:
    """Archigos coup-type removals (claims rest on Archigos alone) + the same-state-same-day P&T context match."""
    succ = {(e.ccode, e.day): e for e in events if e.coup == "2"}
    by_code: dict[int, list[date]] = defaultdict(list)
    for cc, d in succ:
        by_code[cc].append(d)
    out: list[CoupRemoval] = []
    near: Counter[str] = Counter()
    same_day_other: Counter[str] = Counter()
    for s in spells:
        hit = succ.get((s.ccode, s.end))
        if s.exit == "Irregular" and s.exitcode in COUP_LINK_EXITCODES:
            out.append(CoupRemoval(s, COUP_LINK_EXITCODES[s.exitcode], hit))
            if hit is None and events and s.end.year >= 1950:
                ds = [abs((s.end - d).days) for d in by_code.get(s.ccode, [])]
                m = min(ds) if ds else None
                bucket = (
                    "no_pt_coup_in_state" if m is None else "1_day" if m == 1 else "2_7_days" if m <= 7
                    else "8_31_days" if m <= 31 else "more_than_31_days"
                )  # fmt: skip
                near[bucket] += 1
        elif hit is not None and not s.censored:
            same_day_other[f"{s.exit} / {s.exitcode}"] += 1
    matched = [r for r in out if r.pt_event is not None]
    per_event = Counter(r.pt_event.event_id for r in matched if r.pt_event)
    report = {
        "coup_removals": len(out),
        "coup_removals_since_1950": sum(r.spell.end.year >= 1950 for r in out),
        "by_exitcode": dict(Counter(r.spell.exitcode for r in out)),
        "same_state_and_day_as_successful_pt_coup": len(matched),
        "not_matched_since_1950_by_nearest_pt_coup": dict(near),
        "successful_pt_coup_same_day_other_archigos_exit": dict(same_day_other),
        "successful_pt_coups": len(succ),
        "successful_pt_coups_until_2015": sum(1 for (_c, d) in succ if d <= CENSOR_DATE),
        "pt_events_with_several_matches": {k: v for k, v in per_event.items() if v > 1},
        "context_method": CONTEXT_METHOD,
    }
    return out, report


# ----------------------------------------------------------------------------- KG

LEADER_PROPERTIES: dict[str, tuple[str, str, str, str, str | None, str | None, str | None]] = {
    "archigos:leader_of": (
        "item",
        "archigos_leader_of",
        "effective leader of (Archigos spell)",
        "dirigeant effectif de (Archigos)",
        "archigos_effective_leader",
        "effective leader (Archigos)",
        "dirigeant effectif (Archigos)",
    ),
    "archigos:birth_year": (
        "time",
        "archigos_birth_year",
        "year of birth (Archigos)",
        "année de naissance (Archigos)",
        None,
        None,
        None,
    ),
    "archigos:death_date": (
        "time",
        "archigos_death_date",
        "date of death (Archigos)",
        "date de décès (Archigos)",
        None,
        None,
        None,
    ),
    "archigos:enwiki_title": (
        "string",
        "archigos_enwiki_title",
        "English Wikipedia title given by Archigos (DBpedia URI)",
        "titre Wikipédia anglais donné par Archigos (URI DBpedia)",
        None,
        None,
        None,
    ),
    "archigos:same_as": (
        "item",
        "archigos_same_as",
        "same person as Wikidata item (identifier-evidenced)",
        "même personne que l'élément Wikidata (preuve par identifiant)",
        "archigos_same_as_leader",
        "Archigos leader record",
        "fiche de dirigeant Archigos",
    ),
    "archigos:removal_of": (
        "item",
        "archigos_removal_of",
        "removal from office of (Archigos)",
        "éviction du pouvoir de (Archigos)",
        "archigos_removed_in",
        "removed from office in (Archigos)",
        "évincé lors de (Archigos)",
    ),
    "archigos:removed_from": (
        "item",
        "archigos_removed_from",
        "leader removed from the government of (Archigos)",
        "dirigeant évincé du gouvernement de (Archigos)",
        "archigos_removals_in",
        "leader removals in (Archigos)",
        "évictions de dirigeants (Archigos)",
    ),
    "archigos:exit_manner": (
        "string",
        "archigos_exit_manner",
        "manner of exit as coded (Archigos)",
        "mode de sortie du pouvoir tel que codé (Archigos)",
        None,
        None,
        None,
    ),
    "reign:leader_of": (
        "item",
        "reign_leader_of",
        "leader of (REIGN-format leader list)",
        "dirigeant de (liste de dirigeants REIGN)",
        "reign_leader",
        "leader (REIGN-format list)",
        "dirigeant (liste REIGN)",
    ),
}


def leader_kg_properties() -> list[Any]:
    from reva.kg.model import KgProperty

    props = {**LEADER_PROPERTIES, **_COUP_PROPS}
    return [
        KgProperty(
            pid=p,
            value_kind=k,
            reva_predicate=rp,
            label_en=le,
            label_fr=lf,
            inverse_name=inv,
            inverse_label_en=ile,
            inverse_label_fr=ilf,
        )
        for p, (k, rp, le, lf, inv, ile, ilf) in props.items()
    ] + wikidata_time_properties()


def archigos_kg_records(
    spells: Sequence[Spell],
    *,
    raw_sha256: str,
    resolver: StateEntityResolver,
    removals: Sequence[CoupRemoval] = (),
    pt_sha256: str | None = None,
    pt_file: str | None = None,
    same_as: Mapping[str, Mapping[str, Any]] | None = None,
    codebook_fetch: Mapping[str, Any] | None = None,
) -> tuple[list[KgEntity], list[KgStatement]]:
    from reva.adapters.connectors.causdata_common import codebook_sources

    cb_src = codebook_sources(archigos_codebook(), codebook_fetch)
    ents: dict[str, KgEntity] = {}
    sts: list[KgStatement] = []
    by_leader: dict[str, list[Spell]] = defaultdict(list)
    for s in spells:
        by_leader[s.leadid].append(s)
    for leadid, ss in by_leader.items():
        first = min(ss, key=lambda x: x.start)
        lid = first.leader_id
        names = sorted({x.leader for x in ss})
        ents[lid] = KgEntity(
            qid=lid,
            kind="person",
            labels={"en": first.leader},
            descriptions={
                "en": f"political leader as coded by Archigos {ARCHIGOS_RELEASE} ({first.idacr}, from "
                f"{first.start.year})"
                + (f"; also listed as {', '.join(n for n in names if n != first.leader)}" if len(names) > 1 else "")
            },
        )
        sel = {"file": ARCHIGOS_FILE, "row": first.row, "obsid": first.obsid}
        if first.yrborn is not None:
            sts.append(
                KgStatement(
                    key=f"{lid}:born",
                    subject=lid,
                    property="archigos:birth_year",
                    value={"type": "time", "time": f"{first.yrborn:04d}", "precision": 9},
                    raw_sha256=raw_sha256,
                    selector={**sel, "column": "yrborn"},
                )
            )
        if first.deathdate is not None:
            sts.append(
                KgStatement(
                    key=f"{lid}:died",
                    subject=lid,
                    property="archigos:death_date",
                    value={"type": "time", "time": first.deathdate.isoformat(), "precision": 11},
                    raw_sha256=raw_sha256,
                    selector={**sel, "column": "deathdate"},
                )
            )
        elif first.yrdied is not None:
            sts.append(
                KgStatement(
                    key=f"{lid}:died",
                    subject=lid,
                    property="archigos:death_date",
                    value={"type": "time", "time": f"{first.yrdied:04d}", "precision": 9},
                    raw_sha256=raw_sha256,
                    selector={**sel, "column": "yrdied"},
                )
            )
        titles = sorted({x.enwiki_title for x in ss if x.enwiki_title})
        for t in titles:
            sts.append(
                KgStatement(
                    key=f"{lid}:enwiki:{hashlib.sha256(t.encode()).hexdigest()[:12]}",
                    subject=lid,
                    property="archigos:enwiki_title",
                    value={"type": "string", "value": t},
                    qualifiers={"dbpedia_uri": DBPEDIA_PREFIX + t.replace(" ", "_")},
                    raw_sha256=raw_sha256,
                    selector={**sel, "column": "dbpediauri"},
                )
            )
        hit = (same_as or {}).get(leadid)
        if hit is not None:  # the statement names the WDQS response that evidences it
            sts.append(
                KgStatement(
                    key=f"{lid}:same_as",
                    subject=lid,
                    property="archigos:same_as",
                    object_qid=hit["qid"],
                    qualifiers=dict(hit["evidence"]),
                    raw_sha256=hit["evidence"]["wdqs_raw_sha256"],
                    selector=hit["selector"],
                )
            )
    for s in spells:
        lid = s.leader_id
        state = resolver.entity("cow", s.ccode, s.start, s.idacr)
        res = resolver.crosswalk.resolve("cow", s.ccode, s.start)
        q: dict[str, Any] = {
            "obsid": s.obsid,
            "entry": s.entry,
            "exit": s.exit,
            "exitcode": s.exitcode,
            "posttenurefate": s.fate,
            "idacr": s.idacr,
            "cow_code": s.ccode,
            "code_resolution": res.reason,
            "archigos_release": ARCHIGOS_RELEASE,
        }
        if s.prevtimes is not None:
            q["prevtimesinoffice"] = s.prevtimes
        if s.censored:
            q["still_in_office_at"] = s.end.isoformat()
        end_state = resolver.crosswalk.resolve("cow", s.ccode, s.end)
        if (end_state.iso3, end_state.qid) != (res.iso3, res.qid):
            q["state_at_end"] = end_state.iso3 or end_state.qid or end_state.label
        sel = {"file": ARCHIGOS_FILE, "row": s.row, "obsid": s.obsid}
        sts.append(
            KgStatement(
                key=f"spell:{s.obsid}",
                subject=lid,
                property="archigos:leader_of",
                object_qid=state,
                start=s.start.isoformat(),
                start_precision=11,
                end=None if s.censored else s.end.isoformat(),
                end_precision=None if s.censored else 11,
                qualifiers=q,
                raw_sha256=raw_sha256,
                selector=sel,
            )
        )
        if s.exit in REMOVAL_EXITS:
            xid = s.exit_event_id
            ents[xid] = KgEntity(
                qid=xid,
                kind="event",
                labels={
                    "en": f"{'Irregular removal' if s.exit == 'Irregular' else 'Removal by a foreign state'} of "
                    f"{s.leader} ({s.idacr}), {s.end.isoformat()}",
                    "fr": f"{'Éviction irrégulière' if s.exit == 'Irregular' else 'Éviction par un État étranger'}"
                    f" de {s.leader} ({s.idacr}), {s.end.isoformat()}",
                },
                descriptions={"en": f"leader removal as coded by Archigos {ARCHIGOS_RELEASE}: {s.exitcode}"},
            )
            xq: dict[str, Any] = {
                "obsid": s.obsid,
                "exit": s.exit,
                "exitcode": s.exitcode,
                "posttenurefate": s.fate,
                **exit_detail(s.exitcode),
            }
            day = s.end.isoformat()
            sts += [
                KgStatement(
                    key=f"exit:{s.obsid}:of",
                    subject=xid,
                    property="archigos:removal_of",
                    object_qid=lid,
                    point_in_time=day,
                    pit_precision=11,
                    qualifiers=xq,
                    raw_sha256=raw_sha256,
                    selector=sel,
                ),
                KgStatement(
                    key=f"exit:{s.obsid}:from",
                    subject=xid,
                    property="archigos:removed_from",
                    object_qid=resolver.entity("cow", s.ccode, s.end, s.idacr),
                    point_in_time=day,
                    pit_precision=11,
                    qualifiers=xq,
                    raw_sha256=raw_sha256,
                    selector=sel,
                ),
                KgStatement(
                    key=f"exit:{s.obsid}:date",
                    subject=xid,
                    property="P585",
                    value={"type": "time", "time": day, "precision": 11},
                    qualifiers=xq,
                    raw_sha256=raw_sha256,
                    selector=sel,
                ),
                KgStatement(
                    key=f"exit:{s.obsid}:manner",
                    subject=xid,
                    property="archigos:exit_manner",
                    value={"type": "string", "value": s.exitcode},
                    qualifiers=manner_qualifiers(xq, s.exitcode, cb_src),
                    raw_sha256=raw_sha256,
                    selector=sel,
                ),
            ]
    for r in removals:
        s = r.spell
        # the 1.0.0 coup record: re-emitted as superseded (its statements are retracted at load), never an event
        ents[r.event_id] = KgEntity(
            qid=r.event_id,
            kind="superseded_record",
            labels={"en": f"Superseded record: Archigos removal {s.obsid} (see {s.exit_event_id})"},
            descriptions={"en": SUPERSEDED_NOTE},
        )
        e = r.pt_event
        if e is not None:
            day = s.end.isoformat()
            sel = {
                "file": ARCHIGOS_FILE,
                "row": s.row,
                "obsid": s.obsid,
                "columns": ["ccode", "enddate", "exit", "exitcode"],
            }
            sts.append(
                KgStatement(key=f"exit:{s.obsid}:pt", subject=s.exit_event_id, property="coup:same_state_and_day_as",
                            object_qid=e.event_id, point_in_time=day, pit_precision=11,
                            qualifiers={"method": CONTEXT_METHOD, "exitcode": s.exitcode, "pt_dataset_version": e.version,
                                        "pt_selector": {"file": pt_file, "row": e.row}, "pt_raw_sha256": pt_sha256,
                                        "pt_outcome": e.outcome},
                            raw_sha256=raw_sha256, selector=sel)
            )  # fmt: skip
    return list(ents.values()), sts


# ----------------------------------------------------------------------------- coded relations (CAUSCORE T1)


def manner_qualifiers(xq: Mapping[str, Any], exitcode: str, cb_src: Mapping[str, Any]) -> dict[str, Any]:
    """Qualifiers of the exit-manner statement: the codebook passage of THIS exitcode when the codebook has one (else
    the general EXITCODE passage, said so), with where it sits in the archived codebook (audit M5 / M4)."""
    term = EXITCODE_PASSAGE.get(exitcode)
    q = dict(xq)
    q["codebook_definition"] = CODEBOOK_DEFINITIONS[term or "exitcode"]
    q["codebook_definition_scope"] = "this exitcode" if term else "the EXITCODE variable (no passage for this value)"
    src = cb_src.get(term or "exitcode")
    if src:
        q["codebook_quote_source"] = dict(src)
    return q


def codebook(fetched: Mapping[str, Any] | None = None) -> dict[str, Any]:
    from reva.adapters.connectors.causdata_common import codebook_sources

    out = {
        "name": f"Archigos: A Data Set on Leaders 1875-2015, Version {ARCHIGOS_RELEASE} (codebook)",
        "version": f"{ARCHIGOS_RELEASE} (Archigos_4.1.pdf sha256 {ARCHIGOS_CODEBOOK_SHA256[:12]})",
        "url": ARCHIGOS_CODEBOOK_URL,
        "definitions": dict(CODEBOOK_DEFINITIONS),
    }
    src = codebook_sources(archigos_codebook(), fetched)
    if src:
        out["sources"] = src
    return out


def load_claims(
    engine: Any,
    store: Any,
    claims: Sequence[Mapping[str, Any]],
    *,
    raw_sha256s: Sequence[str],
    retrieved_at: Any,
    origin: str,
) -> dict[str, Any]:
    """Load through CAUSCORE's ``reva.causal.load_dataset_claims`` when it and its tables exist. Since 1.1.0 the
    delivery is empty and complete: it retracts the coup -> removal claims of 1.0.0 (audit M5)."""
    try:
        from reva.causal import Codebook, DatasetClaim, load_dataset_claims
    except ImportError:
        return {"status": "skipped", "reason": "reva.causal (CAUSCORE) not available"}
    from sqlalchemy import text

    with engine.connect() as conn:
        if not conn.execute(text("SELECT to_regclass('kg.causal_claim') IS NOT NULL")).scalar_one():
            return {"status": "skipped", "reason": "kg.causal_claim missing (migration 0015_causal)"}
    rep = load_dataset_claims(
        engine,
        store,
        source_key=ARCHIGOS_SOURCE_KEY,
        dataset="coup_removals",
        dataset_revision=f"archigos-{ARCHIGOS_RELEASE}",
        codebook=Codebook(**codebook()),
        raw_sha256s=list(raw_sha256s),
        retrieved_at=retrieved_at,
        origin=origin,
        claims=[DatasetClaim(**c) for c in claims],
        complete=True,
    )
    return {"status": "loaded", **rep.as_dict()}


# ----------------------------------------------------------------------------- Wikidata same_as (option)


def wiki_iri(title: str) -> str:
    """English-Wikipedia article IRI as WDQS stores sitelinks (MediaWiki ``wfUrlencode`` of the title)."""
    t = title.replace(" ", "_")
    return "https://en.wikipedia.org/wiki/" + quote(t, safe=";:@$!*(),/~")


def wdqs_query(titles: Sequence[str]) -> str:
    vals = " ".join(f"<{wiki_iri(t)}>" for t in titles)
    return (
        "SELECT ?article ?item ?human ?birth WHERE { VALUES ?article { " + vals + " } "
        "?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> . "
        "BIND(EXISTS { ?item wdt:P31 wd:Q5 } AS ?human) "
        "OPTIONAL { ?item wdt:P569 ?birth } }"
    )


def _wdqs_year(v: str | None) -> int | None:
    """Year of an ``xsd:dateTime`` literal from WDQS (``1822-04-27T00:00:00Z``; CE years only)."""
    m = re.match(r"^\+?(\d{4,})-\d{2}-\d{2}T", v or "")
    return int(m.group(1)) if m else None


def resolve_same_as(
    spells: Sequence[Spell], responses: Sequence[tuple[bytes, str]]
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """leadid -> {qid, evidence, selector} from WDQS JSON responses ``(body, raw_sha256)`` (fail closed)."""
    import json

    by_iri: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sha_of: dict[str, str] = {}
    for body, sha in responses:
        try:
            doc = json.loads(body)
            rows = doc["results"]["bindings"]
        except (ValueError, KeyError, TypeError) as e:
            raise _drift("WDQS: not a SPARQL JSON result") from e
        for b in rows:
            iri = b.get("article", {}).get("value")
            item = b.get("item", {}).get("value", "")
            if iri and item.startswith("http://www.wikidata.org/entity/Q"):
                by_iri[iri].append(
                    {
                        "qid": item.rsplit("/", 1)[-1],
                        "human": b.get("human", {}).get("value") == "true",
                        "birth": b.get("birth", {}).get("value"),
                    }
                )
                sha_of[iri] = sha
    out: dict[str, dict[str, Any]] = {}
    reasons: Counter[str] = Counter()
    leaders: dict[str, Spell] = {}
    for s in spells:
        if s.enwiki_title and s.leadid not in leaders:
            leaders[s.leadid] = s
    titles_per_leader = defaultdict(set)
    for s in spells:
        if s.enwiki_title:
            titles_per_leader[s.leadid].add(s.enwiki_title)
    for leadid, s in leaders.items():
        if len(titles_per_leader[leadid]) > 1:
            reasons["several_titles_for_one_leader"] += 1
            continue
        iri = wiki_iri(s.enwiki_title or "")
        hits = by_iri.get(iri, [])
        qids = {h["qid"] for h in hits}
        if not qids:
            reasons["title_not_a_sitelink (redirect, renamed or missing)"] += 1
            continue
        if len(qids) > 1:
            reasons["title_on_several_items"] += 1
            continue
        if not any(h["human"] for h in hits):
            reasons["item_not_instance_of_human_Q5"] += 1
            continue
        births = sorted({y for h in hits if (y := _wdqs_year(h["birth"])) is not None})
        if s.yrborn is not None and births and s.yrborn not in births:
            reasons["birth_year_disagrees"] += 1
            continue
        qid = qids.pop()
        out[leadid] = {
            "qid": qid,
            "selector": {"query": "enwiki sitelink + P31/P569 (WDQS)", "article": iri},
            "evidence": {
                "method": "Archigos dbpediauri -> English Wikipedia title -> Wikidata sitelink (exact IRI)",
                "enwiki_title": s.enwiki_title,
                "instance_of_human": True,
                "birth_year_check": (
                    "equal" if s.yrborn is not None and births else "not_checked (year missing on one side)"
                ),
                "archigos_yrborn": s.yrborn,
                "wikidata_birth_years": births,
                "wdqs_raw_sha256": sha_of[iri],
            },
        }
        reasons["linked"] += 1
    return out, dict(reasons)


async def fetch_same_as(run: Any, spells: Sequence[Spell]) -> list[tuple[Any, bytes]]:
    """WDQS POST queries (``WDQS_CHUNK`` titles each), under the machine-wide Wikidata lock when live."""
    from urllib.parse import urlencode

    from reva.adapters.connectors.wikidata import MachineLock, user_agent

    titles = sorted({s.enwiki_title for s in spells if s.enwiki_title})
    out = []
    for i in range(0, len(titles), WDQS_CHUNK):
        q = wdqs_query(titles[i : i + WDQS_CHUNK])
        body = urlencode({"query": q, "format": "json"}).encode()
        kw = dict(
            key=f"wdqs:archigos_titles:{i}",
            content=body,
            content_type="application/x-www-form-urlencoded",
            media_types=("application/sparql-results+json", "application/json"),
            headers={"User-Agent": user_agent(), "Accept": "application/sparql-results+json"},
            max_bytes=10_000_000,
        )
        if run.origin == "live":
            async with MachineLock().slot("wdqs") as slot:
                try:
                    raw = await run.post(WDQS_URL, **kw)
                except SourceResponseError as e:
                    if e.details.get("status") in (429, 503):
                        slot.cooldown(600.0)  # every process backs off; this run gives up on same_as
                    raise
        else:
            raw = await run.post(WDQS_URL, **kw)
        out.append((raw, body))
    return out


# ============================================================================= REIGN-format leader list
LEADERLIST_SOURCE_KEY = "reign.leaderlist"
LEADERLIST_NAME = "atlas.reign_leaders"
LEADERLIST_VERSION_CONNECTOR = "1.0.0"
LEADERLIST_PARSER = "atlas-reign-leaderlist-1"
LEADERLIST_LINK_RE = re.compile(
    r"https://jonathanmpowell\.com/wp-content/uploads/(\d{4})/(\d{2})/leaderlist(?:-\d{1,2})?\.xlsx"
)
LEADERLIST_COLUMNS = (
    "stateabb",
    "ccode",
    "leadid",
    "leader",
    "sdate",
    "smonth",
    "syear",
    "emonth",
    "eyear",
    "birthyear",
    "gender",
    "militarycareer",
)
LEADERLIST_MIN_ROWS = 2_500
LEADERLIST_RIGHTS: dict[str, str] = {
    "licence": "No licence stated (checked 2026-10-16): REIGN-format leader list published without terms next to "
    "the Powell & Thyne coup list; REIGN asked users to cite Bell, Besaw & Frank (2021). No redistribution or "
    "commercial-use grant.",
    "licence_url": PT_PAGE_URL,
    "attribution": "Bell, Curtis, Clayton Besaw & Matthew Frank (2021). The Rulers, Elections, and Irregular "
    "Governance (REIGN) Dataset. One Earth Future; leader list as updated on jonathanmpowell.com/coups",
    "commercial_use": "not_granted",
    "redistribution": "not_granted",
}


@dataclass(frozen=True)
class ListSpell:
    row: int  # 1-based worksheet row
    stateabb: str
    ccode: int
    leadid: str | None
    leader: str
    start: date
    end_month: tuple[int, int | None] | None  # (year, month or None if invalid); None = in office at the update
    birthyear: int | None
    militarycareer: str | None
    #: provider defect noted on the row (e.g. an end month without an end year: the end is then unknown)
    defect: str | None = None
    start_precision: int = 11

    @property
    def leader_id(self) -> str:
        if self.leadid:
            return "reign:leader:" + re.sub(r"[^A-Za-z0-9_.-]", "_", self.leadid)
        return f"reign:leader:c{self.ccode}-{self.start.isoformat()}"


def _int_cell(v: Any, what: str, row: int, *, optional: bool = False) -> int | None:
    s = ("" if v is None else str(v)).strip()
    if s == "" and optional:
        return None
    if not re.match(r"^-?\d+(\.0+)?$", s):
        raise _drift(f"leader list row {row}: {what} {s!r} is not an integer")
    return int(float(s))


def _missing99(v: int | None) -> int | None:
    return None if v == -99 else v  # REIGN missing code


def _mil(v: Any) -> str | None:
    s = ("" if v is None else str(v)).strip()
    return None if s in ("", "-99") else s


def parse_leaderlist(body: bytes, *, strict: bool = True) -> tuple[list[ListSpell], dict[str, Any]]:
    from reva.adapters.connectors.atlas_xlsx import Workbook, XlsxError

    try:
        wb = Workbook(body)
        sheets = list(wb._sheet_targets())
        table = wb.table(sheets[0])
    except (XlsxError, KeyError, IndexError) as e:
        raise _drift("leader list: unreadable workbook") from e
    header = [str(h or "").strip() for h in table[0]]
    if tuple(header[: len(LEADERLIST_COLUMNS)]) != LEADERLIST_COLUMNS:
        raise _drift(f"leader list: header {header}")
    out: list[ListSpell] = []
    updated = None
    if "updated" in header:  # one cell (an Excel 1900-system date serial) in the first data row
        col = header.index("updated")
        serials = [r[col] for r in table[1:] if len(r) > col and r[col] not in (None, "")]
        if serials and re.match(r"^\d{5}(\.0+)?$", str(serials[0])):
            from datetime import timedelta

            updated = date(1899, 12, 30) + timedelta(days=int(float(serials[0])))
    keys: Counter[str] = Counter()
    for i, r in enumerate(table[1:], start=2):
        cells = dict(zip(header, list(r) + [None] * (len(header) - len(r)), strict=False))
        if not any(v not in (None, "") for v in cells.values()):
            continue
        cc = _int_cell(cells["ccode"], "ccode", i)
        sy, sm, sd = (_int_cell(cells[k], k, i) for k in ("syear", "smonth", "sdate"))
        defect = None
        start_precision = 11
        try:
            start = date(sy, sm, sd)  # type: ignore[arg-type]
        except ValueError:  # e.g. ISL 2024 sdate 12 / smonth 21: not corrected, the year is kept
            start, start_precision = date(sy, 1, 1), 9  # type: ignore[arg-type]
            defect = f"invalid start day/month {sd}/{sm}: start kept at year precision"
        ey = _int_cell(cells["eyear"], "eyear", i, optional=True)
        em = _int_cell(cells["emonth"], "emonth", i, optional=True)
        if ey is None and em is not None:  # one 2026 row (MNE, Spajic): end month 6 without a year
            defect, em = f"end month {em} without an end year: end unknown, treated as in office", None
        if ey is not None and (em is None or not 1 <= em <= 12):  # e.g. emonth 20: not corrected
            defect, em = f"invalid end month {em}: end kept at year precision", None
        if ey is not None and (ey, em or 12) < (start.year, start.month):
            raise _drift(f"leader list row {i}: ends before it starts")
        name = str(cells["leader"] or "").strip()
        if not name:
            raise _drift(f"leader list row {i}: no leader name")
        lid = (str(cells["leadid"]).strip() or None) if cells["leadid"] is not None else None
        sp = ListSpell(
            i,
            str(cells["stateabb"] or "").strip(),
            cc,
            lid,
            name,
            start,  # type: ignore[arg-type]
            None if ey is None else (ey, em),  # type: ignore[arg-type]
            _missing99(_int_cell(cells["birthyear"], "birthyear", i, optional=True)),
            _mil(cells["militarycareer"]),
            defect,
            start_precision,
        )
        keys[f"{sp.leader_id}|{sp.start}"] += 1
        out.append(sp)
    dups = [k for k, n in keys.items() if n > 1]
    if dups:
        raise _drift(f"leader list: duplicated spells {dups[:5]}")
    if strict and len(out) < LEADERLIST_MIN_ROWS:
        raise LeaderParseError(f"leader list: {len(out)} rows (truncated?)", retryable=False, kind="incomplete")
    return out, {
        "rows": len(out),
        "updated": updated.isoformat() if updated else None,
        "open_spells": sum(s.end_month is None for s in out),
        "without_leadid": sum(s.leadid is None for s in out),
        "provider_defects": [{"row": s.row, "defect": s.defect} for s in out if s.defect],
    }


def leaderlist_cy_rows(
    spells: Sequence[ListSpell], *, raw_sha256: str, crosswalk: StateCodeCrosswalk, file: str, through_year: int
) -> list[dict[str, Any]]:
    from reva.atlas.tables import json_obj, year_period

    cells: dict[tuple[str, int, int], list[ListSpell]] = defaultdict(list)
    for s in spells:
        last = s.end_month[0] if s.end_month else through_year
        for y in range(s.start.year, last + 1):
            cells[("LEADERS_IN_OFFICE", s.ccode, y)].append(s)
        cells[("LEADER_ENTRIES", s.ccode, s.start.year)].append(s)
    methods = {
        "LEADERS_IN_OFFICE": "count of leader-list spells overlapping the calendar year (start day .. end month; "
        f"spells without an end counted through {through_year}, the list's update year)",
        "LEADER_ENTRIES": "count of leader-list spells starting in the year",
    }
    out = []
    for (ind, cc, year), ss in sorted(cells.items()):
        res = crosswalk.resolve("cow", cc, date(year, 7, 1))
        flags: dict[str, Any] = {
            "computed_from": "REIGN-format leader list (jonathanmpowell.com)",
            "method": methods[ind],
            "stateabb": sorted({s.stateabb for s in ss}),
            "rows": [s.row for s in ss][:60],
            "code_resolution": res.reason,
        }
        if res.iso3 is None and res.label:
            flags["historical_state"] = res.label
        if any(s.defect for s in ss):
            flags["provider_defect_rows"] = [{"row": s.row, "defect": s.defect} for s in ss if s.defect]
        if ind == "LEADERS_IN_OFFICE" and any(s.end_month is None for s in ss):
            flags["open_ended"] = "includes leaders without an end date (in office at the list's update)"
        start, end = year_period(year)
        out.append(
            {
                "source": "reign",
                "dataset": "leaders_cy",
                "indicator": ind,
                "concept_id": None,
                "iso3": res.iso3,
                "provider_geo": f"cow:{cc}",
                "entity_qid": res.qid,
                "period_start": start,
                "period_end": end,
                "value": str(len(ss)),
                "value_text": None,
                "unit": "leaders",
                "flags": json_obj(flags),
                "raw_sha256": raw_sha256,
                "selector": json_obj(file=file, rows=[s.row for s in ss][:200], rows_total=len(ss)),
            }
        )
    return out


def leaderlist_kg_records(
    spells: Sequence[ListSpell], *, raw_sha256: str, resolver: StateEntityResolver, file: str, updated: str | None
) -> tuple[list[KgEntity], list[KgStatement]]:
    ents: dict[str, KgEntity] = {}
    sts: list[KgStatement] = []
    for s in spells:
        lid = s.leader_id
        if lid not in ents:
            ents[lid] = KgEntity(
                qid=lid,
                kind="person",
                labels={"en": s.leader},
                descriptions={"en": f"political leader in the REIGN-format leader list ({s.stateabb})"},
            )
        q: dict[str, Any] = {
            "stateabb": s.stateabb,
            "cow_code": s.ccode,
            "leadid": s.leadid,
            "end_precision": "month (the list gives end month and year only)",
        }
        if s.birthyear is not None:
            q["birthyear"] = s.birthyear
        if s.militarycareer is not None:
            q["militarycareer"] = s.militarycareer
        if s.end_month is None:
            q["in_office_at_list_update"] = updated
        if s.defect:
            q["provider_defect"] = s.defect
        end = (
            None
            if s.end_month is None
            else f"{s.end_month[0]:04d}" + ("" if s.end_month[1] is None else f"-{s.end_month[1]:02d}")
        )
        sts.append(
            KgStatement(
                key=f"spell:{s.row}:{lid}:{s.start.isoformat()}",
                subject=lid,
                property="reign:leader_of",
                object_qid=resolver.entity("cow", s.ccode, s.start, s.stateabb),
                start=s.start.isoformat() if s.start_precision == 11 else f"{s.start.year:04d}",
                start_precision=s.start_precision,
                end=end,
                end_precision=None if end is None else (10 if len(end) > 4 else 9),
                qualifiers=q,
                raw_sha256=raw_sha256,
                selector={"file": file, "row": s.row},
            )
        )
    return list(ents.values()), sts


# ============================================================================= specs, build, ingest


def atlas_source() -> list[Any]:
    from reva.atlas.sources import AtlasSourceSpec

    return [
        AtlasSourceSpec(
            name=ARCHIGOS_NAME,
            version=ARCHIGOS_VERSION_CONNECTOR,
            source_key=ARCHIGOS_SOURCE_KEY,
            budget_key=ARCHIGOS_SOURCE_KEY,
            atlas_source="archigos",
            provider="Henk Goemans, Kristian Skrede Gleditsch & Giacomo Chiozza (Archigos)",
            licence=ARCHIGOS_RIGHTS["licence"],
            allowed_hosts=(ARCHIGOS_HOST, PT_HOST, "cran.r-project.org", WDQS_HOST),
            datasets=("leaders_cy",),
            dataset_ids=(
                f"archigos-{ARCHIGOS_RELEASE}",
                "powell-thyne-coups:latest",
                f"countrycode-{COUNTRYCODE_VERSION}",
            ),
            cadence_seconds=90 * 86400,
            planned_requests=11,  # + the codebook PDF (AUDFIX)
            max_response_bytes=10_000_000,
            min_interval_seconds=2.0,
            parser_version=ARCHIGOS_PARSER,
            field_mappings={
                "geo": "Correlates of War ccode",
                "time": "leader spells, day precision",
                "kg": "archigos:leader:<leadid>, archigos:exit:<obsid> (one removal event, exitcode verbatim; no claim)",
            },
            notes="Also fetches the Powell & Thyne coup list (exact coup join) and countrycode (CRAN); option "
            "wikidata=1 adds WDQS sitelink lookups (machine-wide Wikidata lock) for same_as.",
        ),
        AtlasSourceSpec(
            name=LEADERLIST_NAME,
            version=LEADERLIST_VERSION_CONNECTOR,
            source_key=LEADERLIST_SOURCE_KEY,
            budget_key=LEADERLIST_SOURCE_KEY,
            atlas_source="reign",
            provider="REIGN (Bell, Besaw & Frank, One Earth Future) leader list, as updated on jonathanmpowell.com",
            licence=LEADERLIST_RIGHTS["licence"],
            allowed_hosts=(PT_HOST, "cran.r-project.org"),
            datasets=("leaders_cy",),
            dataset_ids=("reign-leaderlist:latest", f"countrycode-{COUNTRYCODE_VERSION}"),
            cadence_seconds=30 * 86400,
            planned_requests=4,
            max_response_bytes=5_000_000,
            min_interval_seconds=2.0,
            parser_version=LEADERLIST_PARSER,
            field_mappings={
                "geo": "Correlates of War ccode",
                "time": "start day, end month",
                "kg": "reign:leader:<leadid>",
            },
            notes="The coups page is read for the newest leaderlist.xlsx link.",
        ),
    ]


def build_archigos(
    dta_body: bytes,
    *,
    crosswalk: StateCodeCrosswalk,
    qid_for_iso3: Mapping[str, str],
    pt_csv: bytes | None = None,
    pt_file: str | None = None,
    countrycode_sha256: str | None = None,
    same_as_responses: Sequence[tuple[bytes, str]] = (),
    strict: bool = True,
    codebook_fetch: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    sha = hashlib.sha256(dta_body).hexdigest()
    spells, report = parse_archigos(dta_body, strict=strict)
    events = parse_csv(pt_csv, strict=strict) if pt_csv is not None else []
    pt_sha = hashlib.sha256(pt_csv).hexdigest() if pt_csv is not None else None
    removals, join_report = coup_removals(spells, events)
    same_as, same_as_report = resolve_same_as(spells, same_as_responses) if same_as_responses else ({}, {})
    resolver = StateEntityResolver(crosswalk, qid_for_iso3)
    ents, sts = archigos_kg_records(
        spells,
        raw_sha256=sha,
        resolver=resolver,
        removals=removals,
        pt_sha256=pt_sha,
        pt_file=pt_file,
        same_as=same_as,
        codebook_fetch=codebook_fetch,
    )
    claims: list[dict[str, Any]] = []  # Archigos codes one removal, not a cause and an effect (audit M5)
    ents = ents + resolver.code_entities()
    if countrycode_sha256:
        sts = sts + code_correspondence(resolver, raw_sha256=countrycode_sha256)
    return {
        "sha256": sha,
        "pt_sha256": pt_sha,
        "spells": spells,
        "report": report,
        "removals": removals,
        "join": join_report,
        "same_as": same_as,
        "same_as_report": same_as_report,
        "leaders_cy": archigos_cy_rows(spells, raw_sha256=sha, crosswalk=crosswalk),
        "entities": ents,
        "statements": sts,
        "claims": claims,
    }


def build_leaderlist(
    xlsx_body: bytes,
    *,
    file: str,
    crosswalk: StateCodeCrosswalk,
    qid_for_iso3: Mapping[str, str],
    countrycode_sha256: str | None = None,
    strict: bool = True,
    through_year: int | None = None,
) -> dict[str, Any]:
    sha = hashlib.sha256(xlsx_body).hexdigest()
    spells, report = parse_leaderlist(xlsx_body, strict=strict)
    upd = report["updated"]
    through = through_year or (int(upd[:4]) if upd else max(s.start.year for s in spells))
    resolver = StateEntityResolver(crosswalk, qid_for_iso3)
    ents, sts = leaderlist_kg_records(spells, raw_sha256=sha, resolver=resolver, file=file, updated=upd)
    ents = ents + resolver.code_entities()
    if countrycode_sha256:
        sts = sts + code_correspondence(resolver, raw_sha256=countrycode_sha256)
    return {
        "sha256": sha,
        "spells": spells,
        "report": report,
        "leaders_cy": leaderlist_cy_rows(spells, raw_sha256=sha, crosswalk=crosswalk, file=file, through_year=through),
        "entities": ents,
        "statements": sts,
    }


def _summ(r: Mapping[str, Any]) -> dict[str, Any]:
    return {k: r[k] for k in ("dataset_key", "row_count", "created", "unmapped_provider_geo")}


async def atlas_ingest(run: Any) -> dict[str, Any]:
    if run.spec.name == LEADERLIST_NAME:
        return await _ingest_leaderlist(run)
    return await _ingest_archigos(run)


async def _ingest_archigos(run: Any) -> dict[str, Any]:
    crosswalk, cc_sha = await fetch_countrycode(run)
    dta = await run.get(
        ARCHIGOS_URL,
        key=f"archigos:{ARCHIGOS_RELEASE}",
        media_types=("application/x-stata-dta", "application/octet-stream"),
        max_bytes=10_000_000,
    )
    pt_error = None
    try:  # context edges only: the run goes on without them when the P&T host refuses the platform client
        page_raw, csv_raw, page = await fetch_coup_list(run)
    except SourceResponseError as e:
        page_raw = csv_raw = page = None
        pt_error = {"status": "powell_thyne_unavailable", "error": str(e)[:300], "details": dict(e.details)}
    pt_file = csv_file_name(page.csv_url) if page else None
    # parse before any further request or archive (nothing archived on drift)
    spells, _ = parse_archigos(dta.body, strict=run.origin == "live")
    wd: list[tuple[Any, bytes]] = []
    wd_error = None
    if str(run.options.get("wikidata", "")) in ("1", "true", "yes"):
        try:
            wd = await fetch_same_as(run, spells)
        except SourceResponseError as e:  # WDQS throttled or down: no same_as this run, everything else goes on
            wd, wd_error = [], {"status": "wdqs_unavailable", "error": str(e)[:300], "details": dict(e.details)}
    sha = run.archive(dta)
    from reva.adapters.connectors.causdata_common import fetch_codebook

    book = await fetch_codebook(run, archigos_codebook())  # archived as evidence (audit M4)
    out = build_archigos(
        dta.body,
        crosswalk=crosswalk,
        qid_for_iso3=load_qid_for_iso3(),
        pt_csv=csv_raw.body if csv_raw else None,
        pt_file=pt_file,
        countrycode_sha256=cc_sha,
        same_as_responses=[(raw.body, hashlib.sha256(raw.body).hexdigest()) for raw, _b in wd],
        strict=run.origin == "live",
        codebook_fetch=book,
    )
    pt_sha = None
    if page_raw is not None and csv_raw is not None:
        run.archive(page_raw)
        pt_sha = run.archive(csv_raw)
    wd_shas = [run.archive(raw) for raw, _b in wd]
    res: dict[str, Any] = {
        "archigos": out["report"],
        "join": {**out["join"], **({"powell_thyne": pt_error} if pt_error else {})},
        "same_as": wd_error or out["same_as_report"],
        "atlas": {},
        "pt_csv": page.csv_url if page else None,
        "codebook": {k: book.get(k) for k in ("sha256", "pinned_sha256", "matches", "archived", "status")},
    }
    if run.wants("leaders_cy"):
        r = run.register(
            dataset="leaders_cy",
            frame=atlas_frame(out["leaders_cy"]),
            raw_sha256s=[sha],
            rights=ARCHIGOS_RIGHTS,
            coverage_note=f"COMPUTED from Archigos {ARCHIGOS_RELEASE} leader spells (1840-2015; spells still in office "
            "are censored at 2015-12-31); exact counts per COW state-year, non-zero cells only",
        )
        res["atlas"]["leaders_cy"] = _summ(r)
    res["kg_counts"] = {
        "entities": len(out["entities"]),
        "statements": len(out["statements"]),
        "claims": len(out["claims"]),
    }
    if run.dataset is None and kg_available(run.engine):
        raws = sorted({sha, cc_sha, *wd_shas} | ({pt_sha} if pt_sha else set()))
        res["kg"] = load_kg_records(
            run.engine,
            run.store,
            out["entities"],
            out["statements"],
            properties=leader_kg_properties() + _correspondence_property(),
            source_key=ARCHIGOS_SOURCE_KEY,
            source_revision=f"archigos-{ARCHIGOS_RELEASE}+reva-{ARCHIGOS_VERSION_CONNECTOR}",
            raw_sha256s=raws,
            retrieved_at=dta.retrieved_at,
            origin=run.origin,
        )
        from reva.adapters.connectors.causdata_common import retract_statements

        # the 1.0.0 coup records (archigos:coup:*) are no longer emitted: retract their statements (append-only)
        res["kg_retracted"] = retract_statements(
            run.engine,
            run.store,
            source_key=ARCHIGOS_SOURCE_KEY,
            origin=run.origin,
            like_patterns=[f"{ARCHIGOS_SOURCE_KEY}:coup:%"],
            keep=(),
            raw_sha256s=[sha],
            retrieved_at=dta.retrieved_at,
            parameters={"connector": ARCHIGOS_NAME, "reason": "AUDFIX M5: coup records superseded"},
        )
        res["causal"] = load_claims(
            run.engine,
            run.store,
            out["claims"],
            raw_sha256s=[sha],  # Archigos alone asserts every claim
            retrieved_at=dta.retrieved_at,
            origin=run.origin,
        )
    return res


async def _ingest_leaderlist(run: Any) -> dict[str, Any]:
    from reva.adapters.connectors.atlas_coups import MAX_PAGE_BYTES

    crosswalk, cc_sha = await fetch_countrycode(run)
    page_raw = await run.get(
        PT_PAGE_URL, key="powell_thyne:coups_page", media_types=("text/html",), max_bytes=MAX_PAGE_BYTES
    )
    links = sorted({m.group(0) for m in LEADERLIST_LINK_RE.finditer(page_raw.body.decode("utf-8", errors="replace"))})
    if not links:
        raise _drift("no leaderlist.xlsx link on the coups page")
    url = max(links, key=lambda u: (LEADERLIST_LINK_RE.match(u).groups(), u))  # type: ignore[union-attr]
    xlsx = await run.get(
        url,
        key="reign:leaderlist",
        max_bytes=5_000_000,
        media_types=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/octet-stream"),
    )
    file = url.rsplit("/", 1)[-1]
    out = build_leaderlist(
        xlsx.body,
        file=file,
        crosswalk=crosswalk,
        qid_for_iso3=load_qid_for_iso3(),
        countrycode_sha256=cc_sha,
        strict=run.origin == "live",
    )
    run.archive(page_raw)
    sha = run.archive(xlsx)
    res: dict[str, Any] = {"leaderlist": out["report"], "url": url, "atlas": {}}
    if run.wants("leaders_cy"):
        r = run.register(
            dataset="leaders_cy",
            frame=atlas_frame(out["leaders_cy"]),
            raw_sha256s=[sha],
            rights=LEADERLIST_RIGHTS,
            coverage_note=f"COMPUTED from the REIGN-format leader list ({file}, updated "
            f"{out['report']['updated']}); exact counts per COW state-year, non-zero cells only",
        )
        res["atlas"]["leaders_cy"] = _summ(r)
    res["kg_counts"] = {"entities": len(out["entities"]), "statements": len(out["statements"])}
    if run.dataset is None and kg_available(run.engine):
        res["kg"] = load_kg_records(
            run.engine,
            run.store,
            out["entities"],
            out["statements"],
            properties=leader_kg_properties() + _correspondence_property(),
            source_key=LEADERLIST_SOURCE_KEY,
            source_revision=f"leaderlist-{out['report']['updated']}",
            raw_sha256s=sorted({sha, cc_sha}),
            retrieved_at=xlsx.retrieved_at,
            origin=run.origin,
        )
    return res


def _correspondence_property() -> list[Any]:
    from reva.adapters.connectors.atlas_geopol_common import kg_properties as geopol_props

    return [p for p in geopol_props() if p.pid == "geopol:corresponds_to_iso3"]
