"""World knowledge graph: the Python interface other agents code against, and the bulk loader.

Data model (storage: migration ``0009_kg``)
-------------------------------------------
* ``KgEntity`` — an entity snapshot: ``qid`` (a Wikidata Q-id such as ``Q142``, or a provider-scoped key such
  as ``ucdp:conflict:333`` — never a bare provider code), ``kind`` (``country``, ``empire``, ``person``,
  ``war``, ``treaty``, ``language``...), labels / descriptions per language, aliases, sitelinks count.
* ``KgStatement`` — ``subject_qid --property_pid--> object_qid`` *or* a typed literal ``value`` (a dict with a
  ``type`` key; quantities carry decimal *strings*, never floats), with temporal qualifiers ``start``, ``end``
  and ``point_in_time`` (``KgTime``: time string + Wikibase precision), other ``qualifiers``, ``rank``
  (``preferred`` / ``normal``; ``deprecated`` statements are never stored), the statement's own
  ``references``, and provenance (``source_key``, ``source_revision``, ``statement_id``, ``raw_sha256``).
* ``KgProperty`` — the allow-list: statements may only use registered properties.

Time convention
---------------
``KgTime.iso`` uses **astronomical year numbering** (ISO 8601 / XSD 1.1: year ``0`` is 1 BCE, ``-0043`` is 44
BCE), which is what the Wikidata Query Service returns for ``xsd:dateTime`` values. The Wikibase *JSON* API
(``wbgetentities``) uses historical numbering (``-0044`` is 44 BCE, no year 0): convert with
``wikibase_json_time``. ``precision`` is the Wikibase precision (0 = billion years ... 9 = year, 10 = month,
11 = day; 12-14 are treated as day). Derived ``valid_lo``/``valid_hi`` dates (used only for filtering and
sorting) span the whole period at that precision (e.g. year precision 1975 -> 1975-01-01..1975-12-31);
decade precision spans the decade; century precision and coarser are *widened by one unit on each side*
because sources disagree on which year stands for a century. Julian-calendar times (``calendar="julian"``,
precision >= year) are converted to proleptic Gregorian for the derived dates; the raw string is kept.

Loading
-------
``load_batch(engine, store, entities=..., statements=..., source_key=..., raw_sha256s=..., retrieved_at=...,
origin=...)`` writes one ``kg.load`` provenance row and the batch in one transaction (bulk COPY into temp
staging tables, then ``INSERT ... SELECT``). It is bounded (``MAX_ENTITIES_PER_BATCH``,
``MAX_STATEMENTS_PER_BATCH``: callers split larger harvests) and idempotent: a statement is identified by
``(statement key, origin, source revision)`` where the key is ``<source_key>:<statement_id>`` or, without a
source statement id, ``<source_key>:h:<hash of its semantic content>``. Re-loading the same batch inserts
nothing; a new source revision with changed content appends a new row (the current view shows the latest);
a new revision with *unchanged* content is not re-stored (the stored row keeps the revision and retrieval time
at which that content was first recorded); a batch retrieved *before* the stored latest row is reported as
``stale`` and not stored; the same key and revision with different content is a ``conflicting`` source and is
not stored. Deprecated-rank statements are rejected; if an earlier version of the same statement is stored,
a retraction (``deprecated_at_source``) removes it from the current view. ``retract_keys`` records
statements absent from the delivery; their reason is derived, never assumed (audit2 N1, migration
``0018_retraction_reasons``): ``reclassified_by_reva`` when the statement's raw artifact is in this delivery (same
bytes: the source still has it, the connector no longer emits it), else ``retract_reason`` when the caller verified
it (``removed_at_source``), else ``absent_from_delivery``; ``detail`` records the basis. Nothing is ever updated
or deleted.

Reinstatement (review V3, KG-1; migration ``0011_kg_fixes``): a source without revision ids
(``source_revision`` NULL) that re-asserts a statement whose latest stored row is retracted — by a
retraction recorded from a delivery retrieved *before* this one — stores a new row with
``reinstates_row_id`` = that retracted row (report: ``statements_reinstated``); the statement is current
again. A revisioned source re-asserting it under a new revision is an ordinary ``revision``.

Raw artifacts: every ``raw_sha256s`` entry must be archived. If it is not yet a verified ``meta.artifact``
but the bytes are in ``store`` (namespace ``raw``), the loader registers it; otherwise the batch is refused.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import lru_cache
from typing import Any, Literal

from sqlalchemy import Connection, Engine, text

from reva.kernel.errors import IntegrityFailure, ValidationProblem

LOADER_VERSION = "reva.kg.model/1"
HASH_VERSION = "reva-kg-1"

QID_RE = re.compile(r"^(Q[1-9][0-9]{0,11}|[a-z][a-z0-9_]{1,31}:[A-Za-z0-9_.:-]{1,128})$")
PID_RE = re.compile(r"^(P[1-9][0-9]{0,9}|[a-z][a-z0-9_]{1,31}:[a-z][a-z0-9_]{0,63})$")
SOURCE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,99}$")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
LANG_RE = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{1,8}){0,3}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
VALUE_TYPE_RE = re.compile(r"^[a-z][a-z_]{0,31}$")
DECIMAL_RE = re.compile(r"^[+-]?[0-9]{1,40}(\.[0-9]{1,40})?$")
TIME_RE = re.compile(
    r"^([+-]?)([0-9]{1,16})(?:-([0-9]{1,2})(?:-([0-9]{1,2})"
    r"(?:T([0-9]{2}):([0-9]{2})(?::([0-9]{2})(?:\.[0-9]+)?)?)?(Z|[+-][0-9]{2}:?[0-9]{2})?)?)?$"
)

RANKS = ("preferred", "normal")
ORIGINS = ("live", "test_fixture")
VALUE_KINDS = (
    "item",
    "time",
    "quantity",
    "string",
    "monolingualtext",
    "external_id",
    "globe_coordinate",
    "url",
    "other",
)
#: Properties whose statements a source *asserts* as causal: Wikidata has cause (P828), has effect (P1542),
#: has immediate cause (P1478), has contributing factor (P1479), immediate cause of (P1536), contributing
#: factor of (P1537) — the same set WDHARVEST's allow-list marks causal. Only these may be worded causally,
#: and then as "asserted by <source>", never as REVA's conclusion.
CAUSAL_PIDS = frozenset({"P828", "P1542", "P1478", "P1479", "P1536", "P1537"})

MAX_ENTITIES_PER_BATCH = 50_000
MAX_STATEMENTS_PER_BATCH = 200_000
MAX_RETRACTIONS_PER_BATCH = 50_000
MAX_LABEL_CHARS = 500
MAX_STATEMENT_JSON_BYTES = 65_536
MAX_REPORTED_REJECTIONS = 1_000

#: PostgreSQL ``date`` range in astronomical years (4713 BC .. 5874897 AD).
_PG_MIN_YEAR = -4712
_PG_MAX_YEAR = 5_874_897


# ---------------------------------------------------------------------------------------------- dataclasses
@dataclass(frozen=True, slots=True)
class KgTime:
    """A time string (astronomical years, see module doc) with its Wikibase precision (0-14)."""

    iso: str
    precision: int
    calendar: Literal["gregorian", "julian"] = "gregorian"


@dataclass(frozen=True, slots=True)
class KgProperty:
    """An allow-listed property and its naming (``reva_predicate`` e.g. ``official_language``)."""

    pid: str
    value_kind: str
    reva_predicate: str
    label_en: str
    label_fr: str | None = None
    inverse_name: str | None = None
    inverse_label_en: str | None = None
    inverse_label_fr: str | None = None


@dataclass(frozen=True, slots=True)
class KgEntity:
    qid: str
    kind: str
    labels: Mapping[str, str]
    descriptions: Mapping[str, str] = field(default_factory=dict)
    aliases: Mapping[str, Sequence[str]] = field(default_factory=dict)
    sitelinks: int | None = None
    source_revision: str | None = None


@dataclass(frozen=True, slots=True)
class KgStatement:
    subject_qid: str
    property_pid: str
    object_qid: str | None = None
    value: Mapping[str, Any] | None = None
    start: KgTime | None = None
    end: KgTime | None = None
    point_in_time: KgTime | None = None
    qualifiers: Mapping[str, Any] = field(default_factory=dict)
    rank: str = "normal"
    references: Sequence[Mapping[str, Any]] = ()
    #: Must equal the batch ``source_key`` (empty: the batch's).
    source_key: str = ""
    source_revision: str | None = None
    #: The source's own statement id (Wikidata statement GUID). Without one, identity is a content hash.
    statement_id: str | None = None
    #: The raw artifact this statement was parsed from (must be one of the batch's ``raw_sha256s``; when the
    #: batch has exactly one raw artifact it is used by default).
    raw_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class Rejection:
    kind: Literal["entity", "statement", "retraction"]
    index: int
    reason: str
    key: str | None = None


@dataclass
class LoadReport:
    load_id: int | None
    source_key: str
    origin: str
    entities_inserted: int = 0
    entities_unchanged: int = 0
    entities_stale: int = 0
    entities_conflicting: int = 0
    labels_inserted: int = 0
    labels_skipped: int = 0
    statements_inserted: int = 0
    statements_new_revision: int = 0
    statements_unchanged: int = 0
    statements_reinstated: int = 0
    statements_stale: int = 0
    statements_conflicting: int = 0
    retractions_inserted: int = 0
    rejected_counts: dict[str, int] = field(default_factory=dict)
    rejected: list[Rejection] = field(default_factory=list)
    elapsed_s: float = 0.0

    @property
    def rejected_total(self) -> int:
        return sum(self.rejected_counts.values())

    def as_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "rejected"}
        d["rejected_sample"] = [_rej_dict(r) for r in self.rejected[:50]]
        return d


def _rej_dict(r: Rejection) -> dict[str, Any]:
    return {"kind": r.kind, "index": r.index, "reason": r.reason, "key": r.key}


class _Reject(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------------------------- hashing
_ENCODER = json.JSONEncoder(sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _json(obj: Any) -> str:
    """Canonical JSON (sorted keys, compact, no NaN); one shared encoder (building one per call costs more
    than encoding a small statement)."""
    return _ENCODER.encode(obj)


def content_hash(obj: Any) -> str:
    h = hashlib.sha256()
    h.update(HASH_VERSION.encode("ascii"))
    h.update(b"\x00")
    h.update(_json(obj).encode("utf-8"))
    return h.hexdigest()


# ---------------------------------------------------------------------------------------------- time
def _is_leap(y: int, calendar: str) -> bool:
    if calendar == "julian":
        return y % 4 == 0
    return y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)


def _days_in_month(y: int, m: int, calendar: str) -> int:
    if m == 2:
        return 29 if _is_leap(y, calendar) else 28
    return 30 if m in (4, 6, 9, 11) else 31


def _julian_to_gregorian(y: int, m: int, d: int) -> tuple[int, int, int]:
    """Julian calendar date -> proleptic Gregorian (astronomical years, via the Julian day number)."""
    a = (14 - m) // 12
    yy = y + 4800 - a
    mm = m + 12 * a - 3
    jdn = d + (153 * mm + 2) // 5 + 365 * yy + yy // 4 - 32083
    a = jdn + 32044
    b = (4 * a + 3) // 146097
    c = a - 146097 * b // 4
    dd = (4 * c + 3) // 1461
    e = c - 1461 * dd // 4
    mm = (5 * e + 2) // 153
    day = e - (153 * mm + 2) // 5 + 1
    month = mm + 3 - 12 * (mm // 10)
    year = 100 * b + dd - 4800 + mm // 10
    return year, month, day


def _pg_date(ymd: tuple[int, int, int]) -> str | None:
    y, m, d = ymd
    if y < _PG_MIN_YEAR or y > _PG_MAX_YEAR:
        return None
    if y >= 1:
        return f"{y:04d}-{m:02d}-{d:02d}"
    return f"{1 - y:04d}-{m:02d}-{d:02d} BC"


def parse_time(t: KgTime) -> tuple[str | None, str | None]:
    """Validate ``t`` and return its derived inclusive bounds as PostgreSQL date literals.

    Returns ``(None, None)`` when the period lies outside the PostgreSQL date range (e.g. geological time):
    the statement is kept with its raw time but counts as undated for filters. Raises ``_Reject`` for an
    invalid time.
    """
    if not isinstance(t, KgTime):
        raise _Reject("invalid_time")
    if t.calendar not in ("gregorian", "julian"):
        raise _Reject("invalid_time_calendar")
    if type(t.precision) is not int or not 0 <= t.precision <= 14:
        raise _Reject("invalid_time_precision")
    if not isinstance(t.iso, str) or len(t.iso) > 64:
        raise _Reject("invalid_time")
    r = _bounds(t.iso, t.precision, t.calendar)
    if isinstance(r, str):
        raise _Reject(r)
    return r


@lru_cache(maxsize=65_536)
def _bounds(iso: str, precision: int, cal: str) -> tuple[str | None, str | None] | str:
    """Cached core of ``parse_time`` (dates repeat a lot in real data); returns a reject reason string."""
    try:
        return _bounds_uncached(iso, precision, cal)
    except _Reject as e:
        return e.reason


def _bounds_uncached(iso: str, precision: int, cal: str) -> tuple[str | None, str | None]:
    mt = TIME_RE.match(iso.strip())
    if not mt:
        raise _Reject("invalid_time")
    y = int(mt.group(2)) * (-1 if mt.group(1) == "-" else 1)
    mo = int(mt.group(3) or 0)
    d = int(mt.group(4) or 0)
    p = min(precision, 11)
    if mo > 12 or d > 31:
        raise _Reject("invalid_time")
    if p >= 10 and mo == 0:
        raise _Reject("invalid_time_precision")
    if p >= 11:
        if d == 0 or d > _days_in_month(y, mo, cal):
            raise _Reject("invalid_time")
        lo = hi = (y, mo, d)
    elif p == 10:
        lo, hi = (y, mo, 1), (y, mo, _days_in_month(y, mo, cal))
    elif p == 9:
        lo, hi = (y, 1, 1), (y, 12, 31)
    elif p == 8:
        y0 = (y // 10) * 10
        lo, hi = (y0, 1, 1), (y0 + 9, 12, 31)
    else:
        unit = 10 ** (9 - p)
        lo, hi = (y - unit, 1, 1), (y + unit, 12, 31)
    if cal == "julian" and p >= 9:
        lo, hi = _julian_to_gregorian(*lo), _julian_to_gregorian(*hi)
    a, b = _pg_date(lo), _pg_date(hi)
    if a is None or b is None:
        return None, None
    return a, b


def _sort_key(pg: str | None) -> tuple[int, int, int] | None:
    if pg is None:
        return None
    bc = pg.endswith(" BC")
    y, m, d = (int(x) for x in pg.removesuffix(" BC").split("-"))
    return (1 - y if bc else y, m, d)


def wikibase_json_time(value: Mapping[str, Any]) -> KgTime:
    """Convert a Wikibase JSON time datavalue (``{"time": "-0044-03-15T00:00:00Z", "precision": 11,
    "calendarmodel": ".../Q1985786"}``, historical year numbering) to a ``KgTime`` (astronomical years)."""
    raw = str(value["time"])
    mt = TIME_RE.match(raw)
    if not mt:
        raise ValueError(f"not a Wikibase time: {raw[:64]!r}")
    year = int(mt.group(2))
    if mt.group(1) == "-" and year > 0:
        astro = 1 - year  # 1 BCE -> 0, 44 BCE -> -43
        raw = ("-" if astro < 0 else "+") + f"{abs(astro):04d}" + raw[len(mt.group(1)) + len(mt.group(2)) :]
    cal = "julian" if str(value.get("calendarmodel", "")).endswith("/Q1985786") else "gregorian"
    return KgTime(raw, int(value["precision"]), cal)  # type: ignore[arg-type]


def coerce_time(x: Any) -> KgTime | None:
    """Accept a ``KgTime``, ``(iso, precision)``/``(iso, precision, calendar)`` or a dict with ``time``/``iso``
    and ``precision`` (and optional ``calendar``)."""
    if x is None or isinstance(x, KgTime):
        return x
    if isinstance(x, (tuple, list)) and len(x) in (2, 3):
        return KgTime(str(x[0]), x[1], *(x[2:]))
    if isinstance(x, Mapping):
        return KgTime(str(x.get("iso", x.get("time"))), x.get("precision"), x.get("calendar", "gregorian"))  # type: ignore[arg-type]
    raise _Reject("invalid_time")


# ---------------------------------------------------------------------------------------------- normalisation
def statement_key(st: KgStatement, source_key: str) -> str:
    """The identity of a statement across revisions (see module doc)."""
    if st.statement_id:
        return f"{source_key}:{st.statement_id}"
    return f"{source_key}:h:{_sha(_semantic_json(st))[:40]}"


def _time_tuple(t: Any) -> list[Any] | None:
    if t is None:
        return None
    t = coerce_time(t)
    return [t.iso, t.precision, t.calendar]  # type: ignore[union-attr]


def _semantic_json(st: KgStatement) -> str:
    """Canonical JSON of what a statement says (subject, property, object/value, times, qualifiers): the
    basis of the content-derived key; rank and references are not part of it (a change in them is a new
    revision of the same statement)."""
    return _json(
        [
            st.subject_qid,
            st.property_pid,
            st.object_qid,
            st.value,
            _time_tuple(st.start),
            _time_tuple(st.end),
            _time_tuple(st.point_in_time),
            st.qualifiers,
        ]
    )


_HASH_PREFIX = HASH_VERSION.encode("ascii") + b"\x00"


def _sha(text_: str) -> str:
    return hashlib.sha256(_HASH_PREFIX + text_.encode("utf-8")).hexdigest()


def _check_value(v: Mapping[str, Any]) -> tuple[str | None, str | None]:
    if not isinstance(v, Mapping) or not isinstance(v.get("type"), str) or not VALUE_TYPE_RE.match(v["type"]):
        raise _Reject("invalid_value")
    if v["type"] == "quantity":
        for k in ("amount", "upperBound", "lowerBound"):
            if k in v and not (isinstance(v[k], str) and DECIMAL_RE.match(v[k])):
                raise _Reject("quantity_not_decimal_string")
        if "amount" not in v:
            raise _Reject("invalid_value")
    if v["type"] == "time":
        return parse_time(KgTime(str(v.get("time")), v.get("precision"), v.get("calendar", "gregorian")))  # type: ignore[arg-type]
    return None, None


@dataclass(slots=True)
class _StRow:
    idx: int
    key: str
    st: KgStatement
    content: str
    raw: str | None
    lo: str | None
    hi: str | None
    vlo: str | None
    vhi: str | None
    cals: dict[str, str] | None


def _normalise_statement(i: int, st: KgStatement, source_key: str, raws: set[str], props: Mapping[str, str]) -> _StRow:
    if not isinstance(st, KgStatement):
        raise _Reject("not_a_statement")
    if st.source_key and st.source_key != source_key:
        raise _Reject("source_key_mismatch")
    if st.rank == "deprecated":
        raise _Reject("deprecated_rank")
    if st.rank not in RANKS:
        raise _Reject("invalid_rank")
    if not isinstance(st.subject_qid, str) or not QID_RE.match(st.subject_qid):
        raise _Reject("invalid_subject_qid")
    if not isinstance(st.property_pid, str) or not PID_RE.match(st.property_pid):
        raise _Reject("invalid_property_pid")
    kind = props.get(st.property_pid)
    if kind is None:
        raise _Reject("unknown_property")
    if (st.object_qid is None) == (st.value is None):
        raise _Reject("object_xor_value")
    if st.object_qid is not None and (not isinstance(st.object_qid, str) or not QID_RE.match(st.object_qid)):
        raise _Reject("invalid_object_qid")
    vlo = vhi = None
    if st.value is not None:
        vlo, vhi = _check_value(st.value)
        if kind == "item" and st.value.get("type") not in ("somevalue", "novalue"):
            raise _Reject("property_kind_mismatch")
    elif kind != "item":
        raise _Reject("property_kind_mismatch")
    if st.statement_id is not None and not (isinstance(st.statement_id, str) and 1 <= len(st.statement_id) <= 200):
        raise _Reject("invalid_statement_id")
    if st.source_revision is not None and not (
        isinstance(st.source_revision, str) and 1 <= len(st.source_revision) <= 100
    ):
        raise _Reject("invalid_source_revision")
    if type(st.qualifiers) is not dict and not isinstance(st.qualifiers, Mapping):
        raise _Reject("invalid_qualifiers")
    if st.references and (
        not isinstance(st.references, (list, tuple)) or not all(isinstance(r, Mapping) for r in st.references)
    ):
        raise _Reject("invalid_references")
    raw = st.raw_sha256
    if raw is None and len(raws) == 1:
        raw = next(iter(raws))
    if raw is not None and raw not in raws:
        raise _Reject("raw_sha256_not_in_batch")
    bounds: dict[str, tuple[str | None, str | None]] = {}
    cals: dict[str, str] = {}
    if st.start is not None or st.end is not None or st.point_in_time is not None:
        for name in ("start", "end", "point_in_time"):
            t = getattr(st, name)
            if t is None:
                continue
            if not isinstance(t, KgTime):
                st = replace(st, **{name: coerce_time(t)})
                t = getattr(st, name)
            bounds[name] = parse_time(t)
            if t.calendar != "gregorian":
                cals[name] = t.calendar
    # Derived validity: start (or the point in time) .. end (or the point in time); a missing side is open.
    lo_src = bounds.get("start") or bounds.get("point_in_time")
    hi_src = bounds.get("end") or bounds.get("point_in_time")
    lo = lo_src[0] if lo_src else None
    hi = hi_src[1] if hi_src else None
    if lo is not None and hi is not None and _sort_key(lo) > _sort_key(hi):  # type: ignore[operator]
        raise _Reject("end_before_start")
    try:
        sem = _semantic_json(st)
        refs_json = _json(list(st.references)) if st.references else "[]"
    except (TypeError, ValueError) as e:
        raise _Reject("not_json_serialisable") from e
    if len(sem) + len(refs_json) > MAX_STATEMENT_JSON_BYTES:
        raise _Reject("too_large")
    key = f"{source_key}:{st.statement_id}" if st.statement_id else f"{source_key}:h:{_sha(sem)[:40]}"
    if len(key) > 300:
        raise _Reject("statement_key_too_long")
    content = _sha(f"{sem}\x1f{st.rank}\x1f{refs_json}")
    return _StRow(i, key, st, content, raw, lo, hi, vlo, vhi, cals or None)


@dataclass(slots=True)
class _EntRow:
    idx: int
    e: KgEntity
    labels: list[tuple[str, bool, str]]
    content: str
    descriptions: dict[str, str]
    aliases: dict[str, list[str]]


def _normalise_entity(i: int, e: KgEntity) -> tuple[_EntRow, int]:
    if not isinstance(e, KgEntity):
        raise _Reject("not_an_entity")
    if not isinstance(e.qid, str) or not QID_RE.match(e.qid):
        raise _Reject("invalid_qid")
    if not isinstance(e.kind, str) or not NAME_RE.match(e.kind):
        raise _Reject("invalid_kind")
    if e.sitelinks is not None and (
        not isinstance(e.sitelinks, int) or isinstance(e.sitelinks, bool) or e.sitelinks < 0
    ):
        raise _Reject("invalid_sitelinks")
    if e.source_revision is not None and not (
        isinstance(e.source_revision, str) and 1 <= len(e.source_revision) <= 100
    ):
        raise _Reject("invalid_source_revision")
    skipped = 0
    labels: dict[str, str] = {}
    for lang, lab in (e.labels or {}).items():
        lang = str(lang).lower()
        lab = lab.strip() if isinstance(lab, str) else ""
        if not LANG_RE.match(lang) or not lab or len(lab) > MAX_LABEL_CHARS:
            skipped += 1
            continue
        labels[lang] = lab
    descs: dict[str, str] = {}
    for lang, dsc in (e.descriptions or {}).items():
        if isinstance(dsc, str) and dsc.strip() and LANG_RE.match(str(lang).lower()) and len(dsc) <= 2000:
            descs[str(lang).lower()] = dsc.strip()
    aliases: dict[str, list[str]] = {}
    for lang, al in (e.aliases or {}).items():
        lang = str(lang).lower()
        vals = [al] if isinstance(al, str) else list(al or ())
        ok = [a.strip() for a in vals if isinstance(a, str) and a.strip() and len(a.strip()) <= MAX_LABEL_CHARS]
        skipped += len(vals) - len(ok)
        good = sorted(set(ok))
        if LANG_RE.match(lang) and good:
            aliases[lang] = good
        elif good:
            skipped += len(good)
    rows = [(lang, False, lab) for lang, lab in sorted(labels.items())]
    rows += [(lang, True, a) for lang, al in sorted(aliases.items()) for a in al if a != labels.get(lang)]
    content = content_hash(
        {"kind": e.kind, "labels": labels, "descriptions": descs, "aliases": aliases, "sitelinks": e.sitelinks}
    )
    return _EntRow(i, e, rows, content, descs, aliases), skipped


# ---------------------------------------------------------------------------------------------- properties
@dataclass
class PropertyReport:
    properties_inserted: int = 0
    versions_inserted: int = 0
    unchanged: int = 0


def _property_definition(p: KgProperty) -> dict[str, Any]:
    return {
        "reva_predicate": p.reva_predicate,
        "inverse_name": p.inverse_name,
        "label_en": p.label_en,
        "label_fr": p.label_fr,
        "inverse_label_en": p.inverse_label_en,
        "inverse_label_fr": p.inverse_label_fr,
    }


def register_properties(engine: Engine, properties: Iterable[KgProperty]) -> PropertyReport:
    """Add properties to the allow-list (append-only: a changed name/label is a new version; the value kind of a
    registered property never changes)."""
    props = list(properties)
    for p in props:
        if not PID_RE.match(p.pid) or p.value_kind not in VALUE_KINDS or not NAME_RE.match(p.reva_predicate):
            raise ValidationProblem("invalid property definition", pid=str(p.pid)[:64])
        if p.inverse_name is not None and not NAME_RE.match(p.inverse_name):
            raise ValidationProblem("invalid inverse name", pid=p.pid)
        if not p.label_en or len(p.label_en) > 200:
            raise ValidationProblem("property label_en required (max 200 chars)", pid=p.pid)
    rep = PropertyReport()
    with engine.begin() as conn:
        for p in props:
            r = conn.execute(
                text("""INSERT INTO kg.property(pid, value_kind) VALUES (:p, :k)
                        ON CONFLICT (pid) DO NOTHING RETURNING pid"""),
                {"p": p.pid, "k": p.value_kind},
            ).scalar_one_or_none()
            rep.properties_inserted += r is not None
            kind = conn.execute(text("SELECT value_kind FROM kg.property WHERE pid = :p"), {"p": p.pid}).scalar_one()
            if kind != p.value_kind:
                raise ValidationProblem(
                    "a registered property's value kind cannot change",
                    pid=p.pid,
                    registered=kind,
                    requested=p.value_kind,
                )
            sha = content_hash(_property_definition(p))
            cur = conn.execute(
                text("SELECT definition_sha256 FROM kg.property_current WHERE pid = :p"), {"p": p.pid}
            ).scalar_one_or_none()
            if cur == sha:
                rep.unchanged += 1
                continue
            conn.execute(
                text("""INSERT INTO kg.property_version(pid, reva_predicate, inverse_name, label_en, label_fr,
                            inverse_label_en, inverse_label_fr, definition_sha256)
                        VALUES (:p, :rp, :inv, :le, :lf, :ile, :ilf, :sha)
                        ON CONFLICT (pid, definition_sha256) DO NOTHING"""),
                {
                    "p": p.pid,
                    "rp": p.reva_predicate,
                    "inv": p.inverse_name,
                    "le": p.label_en,
                    "lf": p.label_fr,
                    "ile": p.inverse_label_en,
                    "ilf": p.inverse_label_fr,
                    "sha": sha,
                },
            )
            rep.versions_inserted += 1
    return rep


# ---------------------------------------------------------------------------------------------- loader
def _ensure_raw_artifacts(conn: Connection, store: Any, raws: Sequence[str], source_key: str) -> None:
    from reva.adapters.postgres.repositories import register_artifact
    from reva.ports.storage import StoredObject

    for sha in raws:
        if conn.execute(text("SELECT meta.artifact_is_verified(:s)"), {"s": sha}).scalar_one():
            continue
        head = store.head("raw", sha) if store is not None else None
        if head is None:
            raise IntegrityFailure(
                "raw artifact is not archived (neither registered nor in the object store)", sha256=sha
            )
        register_artifact(
            conn,
            StoredObject(sha, head.byte_size, "raw", head.storage_uri, "application/octet-stream"),
            schema_version="kg-raw/1",
            rights={"source_key": source_key, "licence": "as declared by the source registry entry"},
        )


#: Rows per staging statement. A chunk travels as ONE jsonb parameter (an array of row arrays) unpacked
#: server-side with jsonb_array_elements: runtime roles have no TEMP privilege (0007 revokes it from PUBLIC
#: and this module does not widen it), and one jsonb value is much cheaper to bind than per-column arrays.
STAGE_CHUNK = 25_000


def _rows_json(rows: Iterable[Sequence[Any]]) -> str:
    return json.dumps(list(rows), separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _st_tuple(r: _StRow) -> list[Any]:
    st = r.st
    s, e, p = st.start, st.end, st.point_in_time
    return [
        r.idx,
        r.key,
        st.statement_id,
        r.content,
        st.subject_qid,
        st.property_pid,
        st.object_qid,
        st.value,
        s.iso if s else None,
        s.precision if s else None,
        e.iso if e else None,
        e.precision if e else None,
        p.iso if p else None,
        p.precision if p else None,
        r.cals,
        r.lo,
        r.hi,
        r.vlo,
        r.vhi,
        st.qualifiers,
        st.rank,
        list(st.references),
        st.source_revision,
        r.raw,
    ]


#: jsonb_array_elements(...) AS x -> typed columns (positions match _st_tuple).
_ST_SELECT = """(x->>0)::int AS idx, x->>1 AS statement_key, x->>2 AS statement_id, x->>3 AS content_sha256,
  x->>4 AS subject_qid, x->>5 AS property_pid, x->>6 AS object_qid, NULLIF(x->7, 'null'::jsonb) AS value,
  x->>8 AS start_time, (x->>9)::smallint AS start_precision, x->>10 AS end_time, (x->>11)::smallint AS end_precision,
  x->>12 AS pit_time, (x->>13)::smallint AS pit_precision, NULLIF(x->14, 'null'::jsonb) AS time_calendars,
  (x->>15)::date AS valid_lo, (x->>16)::date AS valid_hi, (x->>17)::date AS value_time_lo,
  (x->>18)::date AS value_time_hi, x->19 AS qualifiers, x->>20 AS rank, x->21 AS refs, x->>22 AS source_revision,
  x->>23 AS raw_sha256"""


def _check_batch_params(source_key: str, raw_sha256s: Sequence[str], retrieved_at: datetime, origin: str) -> list[str]:
    if not isinstance(source_key, str) or not SOURCE_KEY_RE.match(source_key):
        raise ValidationProblem("invalid source_key", source_key=str(source_key)[:100])
    if origin not in ORIGINS:
        raise ValidationProblem("origin must be 'live' or 'test_fixture'", origin=str(origin)[:32])
    raws = list(raw_sha256s)
    if (
        not raws
        or len(raws) > 10_000
        or len(set(raws)) != len(raws)
        or not all(isinstance(s, str) and SHA_RE.match(s) for s in raws)
    ):
        raise ValidationProblem("raw_sha256s must be 1..10000 distinct lowercase sha256 hex digests")
    if not isinstance(retrieved_at, datetime) or retrieved_at.tzinfo is None:
        raise ValidationProblem("retrieved_at must be a timezone-aware datetime")
    return raws


def load_batch(
    engine: Engine,
    store: Any,
    *,
    entities: Iterable[KgEntity] = (),
    statements: Iterable[KgStatement] = (),
    source_key: str,
    raw_sha256s: Sequence[str],
    retrieved_at: datetime,
    origin: str,
    retract_keys: Iterable[str] = (),
    parameters: Mapping[str, Any] | None = None,
    retract_reason: str | None = None,
) -> LoadReport:
    """Load one bounded batch in one transaction (see the module docstring for the semantics).

    ``retract_reason``: ``removed_at_source`` only when the caller verified that the source no longer has the
    ``retract_keys`` statements; by default the reason is derived (``reclassified_by_reva`` / ``absent_from_delivery``)."""
    if retract_reason not in (None, "removed_at_source"):
        raise ValidationProblem("retract_reason must be None (derived) or removed_at_source (verified by the caller)")
    t0 = time.perf_counter()
    raws = _check_batch_params(source_key, raw_sha256s, retrieved_at, origin)
    ents = list(entities)
    sts = list(statements)
    retract = list(retract_keys)
    if (
        len(ents) > MAX_ENTITIES_PER_BATCH
        or len(sts) > MAX_STATEMENTS_PER_BATCH
        or len(retract) > MAX_RETRACTIONS_PER_BATCH
    ):
        raise ValidationProblem(
            "batch too large: split it",
            max_entities=MAX_ENTITIES_PER_BATCH,
            max_statements=MAX_STATEMENTS_PER_BATCH,
            max_retractions=MAX_RETRACTIONS_PER_BATCH,
        )
    rep = LoadReport(load_id=None, source_key=source_key, origin=origin)
    counts: Counter[str] = Counter()

    def reject(kind: str, i: int, reason: str, key: str | None = None) -> None:
        counts[reason] += 1
        if len(rep.rejected) < MAX_REPORTED_REJECTIONS:
            rep.rejected.append(Rejection(kind, i, reason, key))  # type: ignore[arg-type]

    # -- entities (validated before the transaction)
    ent_rows: list[_EntRow] = []
    seen_q: dict[str, str] = {}
    for i, e in enumerate(ents):
        try:
            row, skipped = _normalise_entity(i, e)
        except _Reject as r:
            reject("entity", i, r.reason, getattr(e, "qid", None))
            continue
        prev = seen_q.get(row.e.qid)
        if prev is not None:
            if prev != row.content:
                reject("entity", i, "conflict_in_batch", row.e.qid)
            continue
        seen_q[row.e.qid] = row.content
        rep.labels_skipped += skipped
        ent_rows.append(row)

    retract_rows: list[tuple[str, str, str | None]] = [
        (k, retract_reason or "absent_from_delivery", None) for k in retract
    ]
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": "reva:kg:load:" + source_key})
        # Bulk statements: JIT compilation costs ~1 s per staging statement and buys nothing here.
        conn.exec_driver_sql("SET LOCAL jit = off")
        conn.exec_driver_sql("SET LOCAL work_mem = '64MB'")
        props = dict(
            conn.execute(
                text("SELECT pid, value_kind FROM kg.property WHERE pid = ANY(:p)"),
                {
                    "p": sorted(
                        {s.property_pid for s in sts if isinstance(s, KgStatement) and isinstance(s.property_pid, str)}
                    )
                },
            ).all()
        )
        _ensure_raw_artifacts(conn, store, raws, source_key)
        load = conn.execute(
            text("""INSERT INTO kg.load(source_key, origin, raw_sha256s, retrieved_at, loader_version, parameters)
                    VALUES (:sk, :o, :raws, :ret, :lv, CAST(:par AS jsonb)) RETURNING id"""),
            {
                "sk": source_key,
                "o": origin,
                "raws": raws,
                "ret": retrieved_at,
                "lv": LOADER_VERSION,
                "par": _json(dict(parameters or {})),
            },
        ).scalar_one()
        rep.load_id = int(load)
        bind = {"load": rep.load_id, "o": origin, "sk": source_key, "ret": retrieved_at}
        if ent_rows:
            _load_entities(conn, ent_rows, bind, rep)

        # Statements: normalise chunk k+1 in this thread while one helper thread has the database execute
        # chunk k on this transaction's connection (strictly one user of the connection at a time; psycopg
        # releases the GIL while it waits for the server). A fixed single helper thread, not a pool sized from
        # the host CPU count.
        seen_k: dict[tuple[str, str | None], str] = {}
        raw_set = set(raws)
        chunk: list[_StRow] = []
        pending: Future[None] | None = None
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="reva-kg-load") as helper:
            for i, st in enumerate(sts):
                try:
                    row = _normalise_statement(i, st, source_key, raw_set, props)
                except _Reject as r:
                    k: str | None = None
                    if r.reason == "deprecated_rank" and isinstance(st, KgStatement):
                        try:
                            k = statement_key(st, source_key)
                            retract_rows.append((k, "deprecated_at_source", st.source_revision))
                        except (TypeError, ValueError):
                            k = None
                    reject("statement", i, r.reason, k)
                    continue
                ident = (row.key, row.st.source_revision)
                prev = seen_k.get(ident)
                if prev is not None:
                    if prev != row.content:
                        reject("statement", i, "conflict_in_batch", row.key)
                    continue
                seen_k[ident] = row.content
                chunk.append(row)
                if len(chunk) >= STAGE_CHUNK:
                    if pending is not None:
                        pending.result()
                    pending = helper.submit(_load_statements, conn, chunk, bind, rep)
                    chunk = []
            if pending is not None:
                pending.result()
        if chunk:
            _load_statements(conn, chunk, bind, rep)
        if retract_rows:
            _load_retractions(conn, retract_rows, {**bind, "raws": raws}, rep, source_key)

    for reason, n in counts.items():
        rep.rejected_counts[reason] = rep.rejected_counts.get(reason, 0) + n
    rep.elapsed_s = round(time.perf_counter() - t0, 4)
    return rep


def _chunks(rows: Sequence[Any]) -> Iterable[Sequence[Any]]:
    for i in range(0, len(rows), STAGE_CHUNK):
        yield rows[i : i + STAGE_CHUNK]


def _load_entities(conn: Connection, rows: list[_EntRow], bind: dict[str, Any], rep: LoadReport) -> None:
    for chunk in _chunks(rows):
        ents = _rows_json(
            [r.idx, r.e.qid, r.e.kind, r.descriptions, r.aliases, r.e.sitelinks, r.content, r.e.source_revision]
            for r in chunk
        )
        labs = _rows_json([r.e.qid, lang, al, lab] for r in chunk for (lang, al, lab) in r.labels)
        res = conn.execute(
            text("""WITH se AS (
          SELECT (x->>0)::int AS idx, x->>1 AS qid, x->>2 AS kind, x->3 AS descriptions, x->4 AS aliases,
                 (x->>5)::int AS sitelinks, x->>6 AS content_sha256, x->>7 AS source_revision
            FROM jsonb_array_elements(CAST(:ents AS jsonb)) x),
        sl AS (
          SELECT x->>0 AS qid, x->>1 AS lang, (x->>2)::boolean AS is_alias, x->>3 AS label
            FROM jsonb_array_elements(CAST(:labs AS jsonb)) x),
        cls AS (
          SELECT se.*, CASE
              WHEN ex.found THEN 'unchanged'
              WHEN cf.found THEN 'conflicting'
              WHEN lt.id IS NULL THEN 'new'
              WHEN lt.content_sha256 = se.content_sha256 THEN 'unchanged'
              WHEN lt.retrieved_at > :ret THEN 'stale'
              ELSE 'revision' END AS cls
          FROM se
          LEFT JOIN LATERAL (SELECT e.id, e.content_sha256, e.retrieved_at FROM kg.entity e
                              WHERE e.qid = se.qid AND e.origin = :o ORDER BY e.id DESC LIMIT 1) lt ON true
          LEFT JOIN LATERAL (SELECT true AS found FROM kg.entity e
                              WHERE lt.id IS NOT NULL AND e.qid = se.qid AND e.origin = :o
                                AND e.content_sha256 = se.content_sha256
                                AND e.source_revision IS NOT DISTINCT FROM se.source_revision LIMIT 1) ex ON true
          LEFT JOIN LATERAL (SELECT true AS found FROM kg.entity e
                              WHERE lt.id IS NOT NULL AND e.qid = se.qid AND e.origin = :o
                                AND se.source_revision IS NOT NULL AND e.source_revision = se.source_revision
                                AND e.content_sha256 <> se.content_sha256 LIMIT 1) cf ON true),
        ins AS (
          INSERT INTO kg.entity(load_id, qid, origin, kind, descriptions, aliases, sitelinks, content_sha256,
                                source_key, source_revision, retrieved_at)
          SELECT :load, c.qid, :o, c.kind, c.descriptions, c.aliases, c.sitelinks, c.content_sha256, :sk,
                 c.source_revision, :ret
            FROM cls c WHERE c.cls IN ('new','revision') ORDER BY c.idx
          RETURNING id, qid),
        lab AS (
          INSERT INTO kg.entity_label(entity_id, qid, origin, lang, is_alias, label)
          SELECT ins.id, ins.qid, :o, sl.lang, sl.is_alias, sl.label FROM ins JOIN sl ON sl.qid = ins.qid
          RETURNING 1)
        SELECT cls, count(*) FROM cls GROUP BY cls
        UNION ALL SELECT '_inserted', count(*) FROM ins
        UNION ALL SELECT '_labels', count(*) FROM lab"""),
            {**bind, "ents": ents, "labs": labs},
        ).all()
        for cls, n in res:
            n = int(n)
            if cls == "_inserted":
                rep.entities_inserted += n
            elif cls == "_labels":
                rep.labels_inserted += n
            elif cls == "unchanged":
                rep.entities_unchanged += n
            elif cls == "stale":
                rep.entities_stale += n
            elif cls == "conflicting":
                rep.entities_conflicting += n


def _load_statements(conn: Connection, rows: list[_StRow], bind: dict[str, Any], rep: LoadReport) -> None:
    for chunk in _chunks(rows):
        res = conn.execute(
            text(f"""WITH st AS (SELECT {_ST_SELECT} FROM jsonb_array_elements(CAST(:rows AS jsonb)) x),
        cls AS (
          SELECT st.*, lt.id AS lt_id, CASE
              WHEN lt.id IS NULL THEN 'new'
              WHEN lt.retracted AND st.source_revision IS NULL AND lt.retraction_retrieved_at < :ret
                   AND lt.retrieved_at <= :ret THEN 'reinstated'
              WHEN ex.content_sha256 IS NOT NULL AND ex.content_sha256 = st.content_sha256 THEN 'unchanged'
              WHEN ex.content_sha256 IS NOT NULL THEN 'conflicting'
              WHEN lt.retrieved_at > :ret THEN 'stale'
              WHEN lt.content_sha256 = st.content_sha256 AND NOT lt.retracted THEN 'unchanged'
              ELSE 'revision' END AS cls
          FROM st
          LEFT JOIN LATERAL (SELECT s.id, s.content_sha256, s.retrieved_at, rl.retrieved_at IS NOT NULL AS retracted,
                                    rl.retrieved_at AS retraction_retrieved_at
                               FROM kg.statement s
                               LEFT JOIN kg.statement_retraction r ON r.statement_row_id = s.id
                               LEFT JOIN kg.load rl ON rl.id = r.load_id
                              WHERE s.statement_key = st.statement_key AND s.origin = :o
                              ORDER BY s.id DESC LIMIT 1) lt ON true
          LEFT JOIN LATERAL (SELECT s.content_sha256 FROM kg.statement s
                              WHERE lt.id IS NOT NULL AND s.statement_key = st.statement_key AND s.origin = :o
                                AND s.source_revision IS NOT DISTINCT FROM st.source_revision LIMIT 1) ex ON true),
        ins AS (
          INSERT INTO kg.statement(load_id, statement_key, statement_id, origin, content_sha256, subject_qid,
                property_pid, object_qid, value, start_time, start_precision, end_time, end_precision, pit_time,
                pit_precision, time_calendars, valid_lo, valid_hi, value_time_lo, value_time_hi, qualifiers, rank,
                refs, source_key, source_revision, raw_sha256, retrieved_at, reinstates_row_id)
          SELECT :load, c.statement_key, c.statement_id, :o, c.content_sha256, c.subject_qid, c.property_pid,
                 c.object_qid, c.value, c.start_time, c.start_precision, c.end_time, c.end_precision, c.pit_time,
                 c.pit_precision, c.time_calendars, c.valid_lo, c.valid_hi, c.value_time_lo, c.value_time_hi,
                 c.qualifiers, c.rank, c.refs, :sk, c.source_revision, c.raw_sha256, :ret,
                 CASE WHEN c.cls = 'reinstated' THEN c.lt_id END
            FROM cls c WHERE c.cls IN ('new','revision','reinstated') ORDER BY c.idx
          RETURNING 1)
        SELECT cls, count(*) FROM cls GROUP BY cls UNION ALL SELECT '_inserted', count(*) FROM ins"""),
            {**bind, "rows": _rows_json(_st_tuple(r) for r in chunk)},
        ).all()
        for cls, n in res:
            n = int(n)
            if cls == "_inserted":
                rep.statements_inserted += n
            elif cls == "revision":
                rep.statements_new_revision += n
            elif cls == "reinstated":
                rep.statements_reinstated += n
            elif cls == "unchanged":
                rep.statements_unchanged += n
            elif cls == "stale":
                rep.statements_stale += n
            elif cls == "conflicting":
                rep.statements_conflicting += n


def _load_retractions(
    conn: Connection, rows: list[tuple[str, str, str | None]], bind: dict[str, Any], rep: LoadReport, source_key: str
) -> None:
    def ok(k: object, rev: object) -> bool:
        return (
            isinstance(k, str)
            and k.startswith(source_key + ":")
            and len(k) <= 300
            and (rev is None or (isinstance(rev, str) and 1 <= len(rev) <= 100))
        )

    good = [list(r) for r in rows if ok(r[0], r[2])]
    for i, (k, _why, rev) in enumerate(rows):
        if not ok(k, rev):
            rep.rejected_counts["invalid_statement_key"] = rep.rejected_counts.get("invalid_statement_key", 0) + 1
            if len(rep.rejected) < MAX_REPORTED_REJECTIONS:
                rep.rejected.append(Rejection("retraction", i, "invalid_statement_key", str(k)[:100]))
    for chunk in _chunks(good):
        n = conn.execute(
            text("""INSERT INTO kg.statement_retraction(load_id, statement_row_id, statement_key, origin, reason,
                                                        source_revision, detail)
        SELECT DISTINCT ON (lt.id) :load, lt.id, lt.statement_key, :o, d.reason, sr.source_revision,
               jsonb_build_object('basis', d.basis, 'raw_sha256', lt.raw_sha256,
                                  'source_revision_from', lt.source_revision, 'source_revision_to', sr.source_revision)
          FROM (SELECT x->>0 AS statement_key, x->>1 AS reason, x->>2 AS source_revision
                  FROM jsonb_array_elements(CAST(:rows AS jsonb)) x) sr
          CROSS JOIN LATERAL (SELECT s.id, s.statement_key, s.retrieved_at, s.raw_sha256, s.source_revision
                                FROM kg.statement s
                               WHERE s.statement_key = sr.statement_key AND s.origin = :o
                               ORDER BY s.id DESC LIMIT 1) lt
          CROSS JOIN LATERAL (SELECT CASE
              WHEN sr.reason = 'deprecated_at_source' THEN 'deprecated_at_source'
              WHEN lt.raw_sha256 = ANY(CAST(:raws AS text[])) THEN 'reclassified_by_reva'
              ELSE sr.reason END AS reason,
            CASE
              WHEN sr.reason = 'deprecated_at_source' THEN 'deprecated_rank_in_delivery'
              WHEN lt.raw_sha256 = ANY(CAST(:raws AS text[])) THEN 'raw_unchanged'
              WHEN sr.reason = 'removed_at_source' THEN 'removal_verified_by_caller'
              ELSE 'raw_changed_row_not_checked' END AS basis) d
         WHERE lt.retrieved_at <= :ret
           AND NOT EXISTS (SELECT 1 FROM kg.statement_retraction r WHERE r.statement_row_id = lt.id)
         ORDER BY lt.id"""),
            {**bind, "rows": _rows_json(chunk)},
        ).rowcount
        rep.retractions_inserted += int(n)
