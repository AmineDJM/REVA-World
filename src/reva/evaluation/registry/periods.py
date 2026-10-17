"""Period indices for regular-frequency questions (monthly ``M``, quarterly ``Q``, annual ``A``).

index: ``M`` = year*12 + month-1, ``Q`` = year*4 + quarter-1, ``A`` = year. Labels: ``2027-01``, ``2027-Q1``,
``2027``. Periods are half-open UTC date intervals ``[start, end)`` like atlas cells.
"""

from __future__ import annotations

import re
from datetime import date

SEASON = {"M": 12, "Q": 4, "A": 1}
_LABEL = re.compile(r"^(-?\d{1,4})(?:-(\d{2})|-Q([1-4]))?$")


def season_of(freq: str) -> int:
    if freq not in SEASON:
        raise ValueError(f"unknown frequency {freq!r}")
    return SEASON[freq]


def index_of(freq: str, d: date) -> int:
    """Index of the period containing ``d``."""
    if freq == "M":
        return d.year * 12 + d.month - 1
    if freq == "Q":
        return d.year * 4 + (d.month - 1) // 3
    if freq == "A":
        return d.year
    raise ValueError(f"unknown frequency {freq!r}")


def start_of(freq: str, idx: int) -> date:
    if freq == "M":
        return date(idx // 12, idx % 12 + 1, 1)
    if freq == "Q":
        return date(idx // 4, (idx % 4) * 3 + 1, 1)
    if freq == "A":
        return date(idx, 1, 1)
    raise ValueError(f"unknown frequency {freq!r}")


def end_of(freq: str, idx: int) -> date:
    return start_of(freq, idx + 1)


def label_of(freq: str, idx: int) -> str:
    if freq == "M":
        return f"{idx // 12:04d}-{idx % 12 + 1:02d}"
    if freq == "Q":
        return f"{idx // 4:04d}-Q{idx % 4 + 1}"
    if freq == "A":
        return f"{idx:04d}"
    raise ValueError(f"unknown frequency {freq!r}")


def parse_label(freq: str, label: str) -> int:
    m = _LABEL.match(label)
    if not m:
        raise ValueError(f"bad period label {label!r}")
    y = int(m.group(1))
    if freq == "M" and m.group(2):
        return y * 12 + int(m.group(2)) - 1
    if freq == "Q" and m.group(3):
        return y * 4 + int(m.group(3)) - 1
    if freq == "A" and not m.group(2) and not m.group(3):
        return y
    raise ValueError(f"period label {label!r} does not match frequency {freq!r}")


def classify(start: date, end: date) -> tuple[str, int] | None:
    """(frequency, index) of an atlas cell window, or None when it is not a calendar month/quarter/year
    (e.g. a fiscal year or a daily cell)."""
    if start.day != 1:
        return None
    days = (end - start).days
    if 28 <= days <= 31 and end == start_of("M", index_of("M", start) + 1):
        return "M", index_of("M", start)
    if 90 <= days <= 92 and start.month in (1, 4, 7, 10) and end == start_of("Q", index_of("Q", start) + 1):
        return "Q", index_of("Q", start)
    if days in (365, 366) and start.month == 1 and end == date(start.year + 1, 1, 1):
        return "A", start.year
    return None


def cycle_of(d: date) -> str:
    """The registry's quarterly issue cycle containing ``d`` (``2026-Q4``)."""
    return label_of("Q", index_of("Q", d))


def cycle_anchor(cycle: str) -> date:
    return start_of("Q", parse_label("Q", cycle))
