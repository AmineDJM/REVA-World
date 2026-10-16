"""Temporal check of causal claims with date precision bounds (astronomical years).

Temporal precedence is necessary, not sufficient: the check only *flags*; it never creates, drops or corrects a
claim. An event's start is P580 (start time), else P585 (point in time), else P571 (inception); its end is P582,
else P585, else P576 (dissolved). Each date is the *window* of its precision as derived by the KG
(``reva.kg.model.parse_time``: day; month; year 1929 -> 1929-01-01..1929-12-31; decade; century and coarser
widened one unit each side). Several values of the same property (preferred rank first) are joined into their
covering window: REVA does not pick one.

* ``consistent``   — the cause's start window ends before the effect's start window begins;
* ``inconsistent`` — the cause's start window begins after the effect's start window ends (beyond precision);
* ``overlapping``  — the windows overlap: the order cannot be decided at this precision;
* ``undetermined`` — a start is missing (or outside the PostgreSQL date range, e.g. geological time).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from reva.kg.model import KgTime, _Reject, parse_time
from reva.kg.query import astro_iso, date_key

TemporalCheck = Literal["consistent", "overlapping", "inconsistent", "undetermined"]
START_PIDS = ("P580", "P585", "P571")
END_PIDS = ("P582", "P585", "P576")
DATE_PIDS = tuple(dict.fromkeys(START_PIDS + END_PIDS))
YMD = tuple[int, int, int]


@dataclass(frozen=True)
class Window:
    """An inclusive date window (astronomical (year, month, day) tuples) and where it came from."""

    lo: YMD
    hi: YMD
    basis: str  # property id the window was read from
    precision: int | None
    times: tuple[str, ...] = ()  # the source time strings (astronomical)

    def as_dict(self) -> dict[str, Any]:
        return {
            "from": _iso(self.lo),
            "to": _iso(self.hi),
            "basis": self.basis,
            "precision": self.precision,
            "times": list(self.times),
        }


def _iso(t: YMD) -> str:
    y, m, d = t
    year = f"-{-y:04d}" if y < 0 else (f"+{y}" if y > 9999 else f"{y:04d}")
    return f"{year}-{m:02d}-{d:02d}"


def window_of_time(iso: str, precision: int, basis: str = "P585", calendar: str = "gregorian") -> Window | None:
    """Window of one time string at its Wikibase precision (``None`` outside the supported range)."""
    try:
        lo, hi = parse_time(KgTime(iso, precision, calendar))  # type: ignore[arg-type]
    except _Reject:
        return None
    if lo is None or hi is None:
        return None
    return Window(date_key(lo), date_key(hi), basis, precision, (iso,))


@dataclass(frozen=True)
class DateValue:
    """One dated statement of an event: property, rank, derived bounds (PostgreSQL date literals), precision."""

    pid: str
    lo: str | None
    hi: str | None
    precision: int | None
    time: str | None = None
    rank: str = "normal"


def _join(vals: Sequence[DateValue], pid: str) -> Window | None:
    usable = [v for v in vals if v.pid == pid and v.lo is not None and v.hi is not None]
    if not usable:
        return None
    pref = [v for v in usable if v.rank == "preferred"]
    use = pref or usable
    lo = min(date_key(v.lo) for v in use)  # type: ignore[arg-type]
    hi = max(date_key(v.hi) for v in use)  # type: ignore[arg-type]
    precs = {v.precision for v in use}
    return Window(lo, hi, pid, precs.pop() if len(precs) == 1 else None, tuple(sorted({v.time for v in use if v.time})))


@dataclass(frozen=True)
class EventDates:
    start: Window | None
    end: Window | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.as_dict() if self.start else None,
            "end": self.end.as_dict() if self.end else None,
        }


def event_dates(values: Iterable[DateValue]) -> EventDates:
    vals = list(values)
    start = next((w for w in (_join(vals, p) for p in START_PIDS) if w is not None), None)
    end = next((w for w in (_join(vals, p) for p in END_PIDS) if w is not None), None)
    return EventDates(start, end)


def check(cause_start: Window | None, effect_start: Window | None) -> TemporalCheck:
    if cause_start is None or effect_start is None:
        return "undetermined"
    if cause_start.hi < effect_start.lo:
        return "consistent"
    if cause_start.lo > effect_start.hi:
        return "inconsistent"
    return "overlapping"


def temporal_detail(cause: EventDates | None, effect: EventDates | None) -> tuple[TemporalCheck, dict[str, Any]]:
    cs = cause.start if cause else None
    es = effect.start if effect else None
    result = check(cs, es)
    return result, {
        "check": result,
        "cause_start": cs.as_dict() if cs else None,
        "effect_start": es.as_dict() if es else None,
        "rule": "cause start window vs effect start window, at date precision",
    }


def dates_from_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, EventDates]:
    """``{entity: EventDates}`` from rows with ``subject_qid, property_pid, rank, lo, hi, precision, time``
    (``lo``/``hi`` as PostgreSQL date literals, e.g. from ``to_char(..., 'YYYY-MM-DD BC')``)."""
    by: dict[str, list[DateValue]] = {}
    for r in rows:
        by.setdefault(r["subject_qid"], []).append(
            DateValue(r["property_pid"], r["lo"], r["hi"], r.get("precision"), r.get("time"), r.get("rank", "normal"))
        )
    return {q: event_dates(v) for q, v in by.items()}


__all__ = [
    "DATE_PIDS",
    "DateValue",
    "EventDates",
    "TemporalCheck",
    "Window",
    "astro_iso",
    "check",
    "dates_from_rows",
    "event_dates",
    "temporal_detail",
    "window_of_time",
]
