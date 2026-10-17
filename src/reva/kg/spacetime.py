"""Space-time helpers shared by the V4 agents (owner V4CORE): periods with precision, place ids and the place
hierarchy country → ADM1 → ADM2 read from evidenced ``located_in`` statements.

Periods
-------
:class:`Period` is one calendar period at a declared precision (``day``, ``month``, ``quarter``, ``year``,
``decade``, ``century``), astronomical year numbering (year 0 = 1 BCE, -43 = 44 BCE) like the KG and the atlas.

* :func:`parse_period` reads ``2024``, ``2024-03``, ``2024-03-15``, ``2024-Q1`` / ``2024Q1``, ``2020s``,
  ``-0043``, ``19C`` (19th century = 1801..1900), and the same with an explicit precision.
* :meth:`Period.bounds` gives inclusive astronomical ISO bounds (``"2024-01-01"``, ``"2024-03-31"``);
  :meth:`Period.atlas_bounds` the atlas half-open ``[start, end)`` Python dates (years ≥ 1 only: BCE periods go
  through ``reva.atlas.tables.frame_with_year_periods``); :meth:`Period.kg_times` the KG ``(start, end)``
  :class:`~reva.kg.model.KgTime` pair (a quarter is three months: start and end at month precision, since
  Wikibase has no quarter precision; a single day/month/year/decade/century is one point-in-time).
* A period is a *period*, never a point: comparisons use :meth:`overlaps` / :meth:`contains`.
* Precision is never invented: a value reported for a year is a year, not 1 January.

Places
------
Identifiers are the authorities' own (never REVA-made, never matched by name):

* country: ISO 3166-1 alpha-3 (``iso3:FRA`` node, or the Wikidata QID the KG holds for it);
* ADM1/ADM2 by ISO 3166-2 (``iso3166_2:FR-75``, ``iso3166_2:SA-04``) or geoBoundaries ids
  (``geoboundaries:<shapeID>`` with the shape's ISO3 and level, e.g. ``FRA-ADM1-…``).

:func:`place_chain` climbs evidenced ``located_in`` statements (family registry: P131, P17, ``place:located_in``
…) from any entity, returning each level with the statement that evidences it. The only structural inference is
the country of an ISO 3166-2 code (its first element *is* the ISO 3166-1 alpha-2 code by the standard's
definition); it is labelled ``basis: iso3166_2_code_prefix`` and never used to link ADM1 ↔ ADM2.
"""

from __future__ import annotations

import calendar
import json
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from reva.kg.model import KgProperty, KgStatement, KgTime

Precision = Literal["day", "month", "quarter", "year", "decade", "century"]
PRECISIONS: tuple[Precision, ...] = ("day", "month", "quarter", "year", "decade", "century")
#: Wikibase precision codes (KG ``*_precision`` columns).
WIKIBASE_PRECISION = {"century": 7, "decade": 8, "year": 9, "month": 10, "day": 11}
_PREC_FROM_WIKIBASE = {v: k for k, v in WIKIBASE_PRECISION.items()}

MIN_YEAR, MAX_YEAR = -999_999, 9_999


class PeriodError(ValueError):
    pass


def _fmt_year(y: int) -> str:
    return f"-{abs(y):04d}" if y < 0 else f"{y:04d}"


def _iso(y: int, m: int, d: int) -> str:
    return f"{_fmt_year(y)}-{m:02d}-{d:02d}"


def _days_in_month(y: int, m: int) -> int:
    # proleptic Gregorian, astronomical year (year 0 is a leap year like 2000)
    if m == 2:
        return 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28
    return calendar.monthrange(2001, m)[1]


@dataclass(frozen=True, order=True)
class Period:
    """A calendar period. ``year`` is astronomical; ``month``/``day`` are set only at the precision that has
    them; ``quarter`` (1-4) only for quarter precision. Decades start at a multiple of 10 (``2020`` = 2020-2029);
    centuries are the ordinal ones (``century=19`` = years 1801-1900; BCE centuries are not modelled)."""

    precision: Precision
    year: int
    month: int | None = None
    day: int | None = None
    quarter: int | None = None
    century: int | None = None

    def __post_init__(self) -> None:
        p = self.precision
        if p not in PRECISIONS:
            raise PeriodError(f"precision must be one of {PRECISIONS}")
        if not MIN_YEAR <= self.year <= MAX_YEAR:
            raise PeriodError(f"year {self.year} out of range")
        need_m = p in ("day", "month")
        if need_m != (self.month is not None) or (self.month is not None and not 1 <= self.month <= 12):
            raise PeriodError(f"month required (1-12) exactly for day/month precision, got {self.month}")
        if (p == "day") != (self.day is not None):
            raise PeriodError("day required exactly for day precision")
        if p == "day":
            assert self.month is not None and self.day is not None
            if not 1 <= self.day <= _days_in_month(self.year, self.month):
                raise PeriodError(f"invalid day {self.year}-{self.month}-{self.day}")
        if (p == "quarter") != (self.quarter is not None) or (self.quarter is not None and not 1 <= self.quarter <= 4):
            raise PeriodError("quarter (1-4) required exactly for quarter precision")
        if p == "decade" and self.year % 10:
            raise PeriodError("a decade starts at a multiple of 10 (2020 = 2020-2029)")
        if (p == "century") != (self.century is not None):
            raise PeriodError("century number required exactly for century precision")
        if p == "century":
            assert self.century is not None
            if self.century < 1 or self.year != (self.century - 1) * 100 + 1:
                raise PeriodError("century N starts in year (N-1)*100+1 (BCE centuries are not modelled)")

    # ------------------------------------------------------------------------------------------ bounds
    def bounds_ymd(self) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
        y, p = self.year, self.precision
        if p == "day":
            assert self.month and self.day
            return (y, self.month, self.day), (y, self.month, self.day)
        if p == "month":
            assert self.month
            return (y, self.month, 1), (y, self.month, _days_in_month(y, self.month))
        if p == "quarter":
            assert self.quarter
            m0 = 3 * (self.quarter - 1) + 1
            return (y, m0, 1), (y, m0 + 2, _days_in_month(y, m0 + 2))
        if p == "year":
            return (y, 1, 1), (y, 12, 31)
        if p == "decade":
            return (y, 1, 1), (y + 9, 12, 31)
        return (y, 1, 1), (y + 99, 12, 31)  # century

    def bounds(self) -> tuple[str, str]:
        """Inclusive bounds, ISO 8601 with astronomical years (``-0043-03-15``)."""
        a, b = self.bounds_ymd()
        return _iso(*a), _iso(*b)

    def atlas_bounds(self) -> tuple[date, date]:
        """Atlas half-open ``[period_start, period_end)`` dates (years 1-9998 only)."""
        (y0, m0, d0), (y1, m1, d1) = self.bounds_ymd()
        if y0 < 1 or y1 > 9998:
            raise PeriodError("Python dates cover years 1-9998: use reva.atlas.tables.frame_with_year_periods for BCE")
        end = date(y1, m1, d1)
        return date(y0, m0, d0), date.fromordinal(end.toordinal() + 1)

    def kg_times(self) -> tuple[KgTime, KgTime | None]:
        """``(start_or_point, end)`` for a :class:`~reva.kg.model.KgStatement`: a quarter is start + end at month
        precision; any other period is one point-in-time at its own Wikibase precision (end ``None``)."""
        if self.precision == "quarter":
            assert self.quarter
            m0 = 3 * (self.quarter - 1) + 1
            return (
                KgTime(f"{_fmt_year(self.year)}-{m0:02d}-01", WIKIBASE_PRECISION["month"]),
                KgTime(f"{_fmt_year(self.year)}-{m0 + 2:02d}-01", WIKIBASE_PRECISION["month"]),
            )
        (y, m, d), _ = self.bounds_ymd()
        if self.precision == "century":
            # Wikibase convention: a century is written with a year inside it (1801..1900 → 1900 for "19th")
            assert self.century is not None
            y = self.century * 100
        return KgTime(_iso(y, m, d), WIKIBASE_PRECISION[self.precision]), None

    # ------------------------------------------------------------------------------------------ relations
    def overlaps(self, other: Period) -> bool:
        a0, a1 = self.bounds_ymd()
        b0, b1 = other.bounds_ymd()
        return a0 <= b1 and b0 <= a1

    def contains(self, other: Period) -> bool:
        a0, a1 = self.bounds_ymd()
        b0, b1 = other.bounds_ymd()
        return a0 <= b0 and b1 <= a1

    def text(self) -> str:
        """Canonical text (parsed back by :func:`parse_period`)."""
        y = _fmt_year(self.year)
        if self.precision == "day":
            return f"{y}-{self.month:02d}-{self.day:02d}"
        if self.precision == "month":
            return f"{y}-{self.month:02d}"
        if self.precision == "quarter":
            return f"{y}-Q{self.quarter}"
        if self.precision == "decade":
            return f"{y}s"
        if self.precision == "century":
            return f"{self.century}C"
        return y

    def label_fr(self) -> str:
        mois = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
                "novembre", "décembre")  # fmt: skip
        y = self.year
        ys = f"{1 - y} av. J.-C." if y <= 0 else str(y)
        if self.precision == "day":
            assert self.month
            return f"{self.day} {mois[self.month - 1]} {ys}"
        if self.precision == "month":
            assert self.month
            return f"{mois[self.month - 1]} {ys}"
        if self.precision == "quarter":
            return f"{'1er' if self.quarter == 1 else f'{self.quarter}e'} trimestre {ys}"
        if self.precision == "decade":
            return f"années {ys}" if y > 0 else f"décennie {ys}"
        if self.precision == "century":
            return f"{_roman(self.century or 0)}e siècle"
        return ys


def _roman(n: int) -> str:
    vals = ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
            (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"))  # fmt: skip
    out = ""
    for v, s in vals:
        while n >= v:
            out, n = out + s, n - v
    return out


_Y = r"(-?\d{1,6})"
_PATTERNS: tuple[tuple[re.Pattern[str], Precision], ...] = (
    (re.compile(rf"^{_Y}-(\d{{2}})-(\d{{2}})$"), "day"),
    (re.compile(rf"^{_Y}-(\d{{2}})$"), "month"),
    (re.compile(rf"^{_Y}-?Q([1-4])$", re.I), "quarter"),
    (re.compile(rf"^{_Y}s$"), "decade"),
    (re.compile(r"^(\d{1,2})C$", re.I), "century"),
    (re.compile(rf"^{_Y}$"), "year"),
)


def parse_period(text: str, precision: Precision | None = None) -> Period:
    """Parse a period string (see module doc). With ``precision``, the text must have exactly that precision."""
    t = str(text).strip()
    for rx, prec in _PATTERNS:
        m = rx.match(t)
        if not m:
            continue
        if precision is not None and precision != prec:
            raise PeriodError(f"{text!r} has precision {prec}, expected {precision}")
        g = m.groups()
        if prec == "day":
            return Period("day", int(g[0]), month=int(g[1]), day=int(g[2]))
        if prec == "month":
            return Period("month", int(g[0]), month=int(g[1]))
        if prec == "quarter":
            return Period("quarter", int(g[0]), quarter=int(g[1]))
        if prec == "decade":
            return Period("decade", int(g[0]))
        if prec == "century":
            c = int(g[0])
            return Period("century", (c - 1) * 100 + 1, century=c)
        return Period("year", int(g[0]))
    raise PeriodError(f"unrecognised period {text!r}")


def period_from_kg_time(iso: str, wikibase_precision: int) -> Period | None:
    """The period a KG time (astronomical ISO + Wikibase precision) stands for; ``None`` for precisions this
    model does not cover (millennium and coarser, or sub-day)."""
    prec = _PREC_FROM_WIKIBASE.get(int(wikibase_precision))
    if prec is None:
        return None
    m = re.match(r"^(-?\d{1,7})-(\d{2})-(\d{2})", iso.lstrip("+"))
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if prec == "day":
        return Period("day", y, month=mo, day=d)
    if prec == "month":
        return Period("month", y, month=mo)
    if prec == "year":
        return Period("year", y)
    if prec == "decade":
        return Period("decade", y - (y % 10))
    if y <= 0:
        return None
    c = (y - 1) // 100 + 1  # Wikibase writes the 19th century as 1801..1900 (often 1900)
    return Period("century", (c - 1) * 100 + 1, century=c)


def iter_periods(first: Period, last: Period) -> Iterator[Period]:
    """Consecutive periods of one precision from ``first`` to ``last`` inclusive (day/month/quarter/year)."""
    if first.precision != last.precision:
        raise PeriodError("first and last must have the same precision")
    p = first
    n = 0
    while p <= last:
        yield p
        n += 1
        if n > 1_000_000:  # pragma: no cover
            raise PeriodError("too many periods")
        p = _next(p)


def _next(p: Period) -> Period:
    if p.precision == "year":
        return Period("year", p.year + 1)
    if p.precision == "quarter":
        assert p.quarter
        return Period("quarter", p.year + (p.quarter == 4), quarter=p.quarter % 4 + 1)
    if p.precision == "month":
        assert p.month
        return Period("month", p.year + (p.month == 12), month=p.month % 12 + 1)
    if p.precision == "day":
        assert p.month and p.day
        if p.day < _days_in_month(p.year, p.month):
            return Period("day", p.year, month=p.month, day=p.day + 1)
        if p.month < 12:
            return Period("day", p.year, month=p.month + 1, day=1)
        return Period("day", p.year + 1, month=1, day=1)
    if p.precision == "decade":
        return Period("decade", p.year + 10)
    assert p.century
    return Period("century", p.year + 100, century=p.century + 1)


# ---------------------------------------------------------------------------------------------- places
ISO3166_2_RE = re.compile(r"^([A-Z]{2})-([A-Z0-9]{1,3})$")
GEOBOUNDARIES_RE = re.compile(r"^([A-Z]{3})-ADM([0-5])-([A-Za-z0-9_.-]{1,64})$")
ISO3_RE = re.compile(r"^[A-Z]{3}$")

#: Dataset predicate for evidenced place containment (child → parent), mapped to the ``located_in`` family in
#: registry/kg/relations_v4.yaml. A loader registers it with :func:`reva.kg.model.register_properties`.
PLACE_LOCATED_IN = KgProperty(
    pid="place:located_in",
    value_kind="item",
    reva_predicate="place_located_in",
    label_en="located in the administrative unit (as the source states)",
    label_fr="situé dans l'unité administrative (selon la source)",
    inverse_name="place_contains",
    inverse_label_en="contains the administrative unit (as the source states)",
    inverse_label_fr="contient l'unité administrative (selon la source)",
)


@dataclass(frozen=True)
class PlaceId:
    """A place identifier as the authority publishes it."""

    scheme: Literal["iso3", "iso3166_2", "geoboundaries"]
    code: str
    level: int  # 0 country, 1 ADM1, 2 ADM2… (ISO 3166-2 codes: 1 unless the source states otherwise)
    country_iso2: str | None = None
    country_iso3: str | None = None

    @property
    def kg_id(self) -> str:
        """The KG node id (``iso3:FRA``, ``iso3166_2:FR-75``, ``geoboundaries:FRA-ADM1-…``)."""
        return f"{self.scheme}:{self.code}"


def parse_place(value: str, *, level: int | None = None) -> PlaceId:
    """Parse an ISO 3166-1 alpha-3, ISO 3166-2 or geoBoundaries id (bare or as a ``<scheme>:`` KG id).

    ISO 3166-2 codes do not carry their level (a French ``FR-75`` is a département, ``FR-IDF`` a region): pass
    ``level`` from the source; the default 1 is the ISO list's first-level assumption and is only a default."""
    v = str(value).strip()
    scheme_hint = None
    if ":" in v:
        scheme_hint, v = v.split(":", 1)
    if scheme_hint in (None, "iso3") and ISO3_RE.match(v):
        return PlaceId("iso3", v, 0, country_iso3=v)
    m = ISO3166_2_RE.match(v)
    if m and scheme_hint in (None, "iso3166_2"):
        return PlaceId("iso3166_2", v, 1 if level is None else level, country_iso2=m.group(1))
    m = GEOBOUNDARIES_RE.match(v)
    if m and scheme_hint in (None, "geoboundaries"):
        lv = int(m.group(2))
        if level is not None and level != lv:
            raise ValueError(f"geoBoundaries id {v} is ADM{lv}, not level {level}")
        return PlaceId("geoboundaries", v, lv, country_iso3=m.group(1))
    raise ValueError(f"not an ISO 3166-1 alpha-3, ISO 3166-2 or geoBoundaries id: {value!r}")


ISO3166_1_FILE = Path(__file__).resolve().parents[3] / "registry" / "reference" / "iso-codes_4.20.1_iso_3166-1.json"


@lru_cache(maxsize=1)
def iso2_to_iso3() -> dict[str, str]:
    """ISO 3166-1 alpha-2 → alpha-3 from the pinned Debian iso-codes mirror (the standard's own table)."""
    try:
        doc = json.loads(ISO3166_1_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # pragma: no cover - the registry file is part of the repo
        return {}
    return {str(e["alpha_2"]): str(e["alpha_3"]) for e in doc.get("3166-1", []) if e.get("alpha_2")}


def located_in_statement(
    child: PlaceId | str,
    parent: PlaceId | str,
    *,
    source_key: str,
    raw_sha256: str | None = None,
    statement_id: str | None = None,
    source_revision: str | None = None,
    valid: Period | None = None,
    qualifiers: Mapping[str, Any] | None = None,
) -> KgStatement:
    """A ``place:located_in`` statement child → parent, as a source states it (the source's own parent field,
    e.g. geoBoundaries ADM2 ``shapeGroup``/parent id, or an ISO 3166-2 subdivision's parent). Never built from
    name similarity; the caller passes the source's ids."""
    c = child.kg_id if isinstance(child, PlaceId) else str(child)
    p = parent.kg_id if isinstance(parent, PlaceId) else str(parent)
    start, end = (None, None)
    pit = None
    if valid is not None:
        t0, t1 = valid.kg_times()
        if t1 is None:
            pit = t0
        else:
            start, end = t0, t1
    return KgStatement(
        subject_qid=c,
        property_pid=PLACE_LOCATED_IN.pid,
        object_qid=p,
        start=start,
        end=end,
        point_in_time=pit,
        qualifiers=dict(qualifiers or {}),
        source_key=source_key,
        raw_sha256=raw_sha256,
        statement_id=statement_id,
        source_revision=source_revision,
    )


@dataclass(frozen=True)
class PlaceStep:
    qid: str
    level: int | None
    via: str | None  # property id of the evidencing statement (None for the start)
    statement_key: str | None
    basis: str  # "start" | "statement" | "iso3166_2_code_prefix"


#: Preferred properties when several located_in statements lead up from one node (administrative first).
ADMIN_ORDER = ("place:located_in", "P131", "P17")


def place_chain(
    start: str,
    edges: Iterable[Mapping[str, Any]],
    *,
    located_in_pids: Iterable[str] | None = None,
    max_depth: int = 6,
    country_by_iso2: Mapping[str, str] | None = None,
) -> list[PlaceStep]:
    """Climb from ``start`` to the top through evidenced located_in edges.

    ``edges``: statement dicts with ``subject_qid``, ``property_pid``, ``object_qid``, ``statement_key`` (the
    caller reads them from ``kg.statement_current``; optional ``ended``: the statement has an end date, so a
    current containment is preferred over a past one). At each level the parent is chosen by currency, then by
    property preference
    (:data:`ADMIN_ORDER`, then the other located_in pids sorted) and then by statement key, deterministically;
    a cycle stops the climb. When the chain stops at an ISO 3166-2 node without a stated parent and
    ``country_by_iso2`` maps its prefix, the country is appended with basis ``iso3166_2_code_prefix``.
    """
    pids = set(located_in_pids) if located_in_pids is not None else set(ADMIN_ORDER)
    order = {p: i for i, p in enumerate(ADMIN_ORDER)}
    up: dict[str, list[Mapping[str, Any]]] = {}
    for e in edges:
        if e.get("property_pid") in pids and e.get("object_qid"):
            up.setdefault(str(e["subject_qid"]), []).append(e)
    chain = [PlaceStep(start, _level_of(start), None, None, "start")]
    seen = {start}
    cur = start
    for _ in range(max_depth):
        cands = sorted(
            up.get(cur, []),
            key=lambda e: (
                bool(e.get("ended")),
                order.get(str(e["property_pid"]), 99),
                str(e["property_pid"]),
                str(e.get("statement_key")),
            ),
        )
        nxt = next((e for e in cands if str(e["object_qid"]) not in seen), None)
        if nxt is None:
            break
        cur = str(nxt["object_qid"])
        seen.add(cur)
        chain.append(PlaceStep(cur, _level_of(cur), str(nxt["property_pid"]), nxt.get("statement_key"), "statement"))
    last = chain[-1].qid
    if country_by_iso2 and last.startswith("iso3166_2:"):
        iso2 = last.split(":", 1)[1].split("-", 1)[0]
        country = country_by_iso2.get(iso2)
        if country and country not in seen:
            chain.append(PlaceStep(country, 0, None, None, "iso3166_2_code_prefix"))
    return chain


def _level_of(qid: str) -> int | None:
    """Level stated by the identifier itself: iso3 = 0, geoBoundaries = its ADM level; else unknown."""
    scheme = qid.split(":", 1)[0] if ":" in qid else None
    if scheme not in ("iso3", "geoboundaries"):
        return None
    try:
        return parse_place(qid).level
    except ValueError:
        return None


def chain_levels(chain: Sequence[PlaceStep]) -> dict[str, str]:
    """``{"country": qid, "adm1": qid, "adm2": qid}`` for the ids whose level the identifier itself states
    (iso3 / geoBoundaries); ISO 3166-2 nodes have no intrinsic level and are left out."""
    names = {0: "country", 1: "adm1", 2: "adm2", 3: "adm3"}
    out: dict[str, str] = {}
    for s in chain:
        if s.level is not None and s.level in names:
            out.setdefault(names[s.level], s.qid)
    return out
