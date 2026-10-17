"""French display formatting for dossier values (exact value kept apart, never rounded away)."""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any

NBSP = " "  # narrow no-break space: French thousands separator

# Units as sources write them -> French display. Unknown units are shown as the source wrote them.
UNIT_FR: dict[str, str] = {
    "thousand barrels per day": "milliers de barils par jour",
    "dollars per barrel": "dollars par baril",
    "Dollars per Barrel": "dollars par baril",
    "deaths": "décès",
    "persons": "personnes",
    "years": "ans",
    "US dollars": "dollars US",
    "US dollars per person": "dollars US par habitant",
    "international dollars per person": "dollars internationaux par habitant",
    "% change": "%",
    "%": "%",
    "% of GDP": "% du PIB",
    "km2": "km²",
    "tonnes per person": "tonnes par habitant",
    "per 100,000 population": "pour 100 000 habitants",
    "persons per km2 of land area": "habitants par km²",
    "index (0-100)": "indice (0-100)",
    "index (0-1)": "indice (0-1)",
    "DALYs": "AVCI",
    "deaths per 100 000 population": "décès pour 100 000 habitants",
    "DALYs per 100 000 population": "AVCI pour 100 000 habitants",
    "per 1,000 population at risk": "pour 1 000 personnes exposées",
    "MW": "MW",
    "percent": "%",
    "log_points": "points log",
    "1": "",
}

SCALE_UNITS = {"US dollars", "persons", "deaths", "DALYs"}


def unit_fr(unit: str | None) -> str:
    if not unit:
        return ""
    return UNIT_FR.get(unit, unit)


def to_decimal(v: Any) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _group(int_part: str) -> str:
    neg = int_part.startswith("-")
    s = int_part.lstrip("-")
    out = []
    while len(s) > 3:
        out.insert(0, s[-3:])
        s = s[:-3]
    out.insert(0, s)
    return ("-" if neg else "") + NBSP.join(out)


def fr_decimal(d: Decimal, digits: int) -> str:
    q = Decimal(1).scaleb(-digits) if digits > 0 else Decimal(1)
    r = d.quantize(q)
    s = f"{r:f}"
    if "." in s:
        i, f = s.split(".")
        f = f.rstrip("0") if digits > 0 else ""
        return _group(i) + ("," + f if f else "")
    return _group(s)


def auto_digits(d: Decimal) -> int:
    a = abs(d)
    if a >= 1000:
        return 0
    if a >= 100:
        return 1
    if a >= 1:
        return 2
    if a == 0:
        return 0
    # keep 3 significant digits for small values
    return min(6, max(2, -int(math.floor(math.log10(float(a)))) + 2))


def fr_number(v: Any, digits: int | None = None, unit: str | None = None) -> str | None:
    """French display of a decimal value; large counts and dollar amounts get a scale word."""
    d = to_decimal(v)
    if d is None:
        return None
    if unit in SCALE_UNITS and abs(d) >= Decimal(1_000_000):
        for size, word in ((Decimal(10) ** 12, "billion(s)"), (Decimal(10) ** 9, "milliard(s)"), (Decimal(10) ** 6, "million(s)")):
            if abs(d) >= size:
                x = d / size
                return f"{fr_decimal(x, 1 if abs(x) < 100 else 0)} {word.replace('(s)', 's' if abs(x) >= 2 else '')}"
    return fr_decimal(d, auto_digits(d) if digits is None else digits)


def with_unit(text: str, unit: str | None) -> str:
    u = unit_fr(unit)
    if not u:
        return text
    if u == "%":
        return f"{text}{NBSP}%"
    if text.endswith(("millions", "milliards", "billions", "million", "milliard", "billion")):
        return f"{text} de {u}" if not u.startswith(("%", "d")) else f"{text} {u}"
    return f"{text} {u}"


MONTHS_FR = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]


def fr_period(p: str | None) -> str | None:
    """'2024' -> '2024'; '2026-09' -> 'sept. 2026'; '2027Q1' -> 'T1 2027'; ISO dates -> '18 mars 2014'."""
    if not p:
        return None
    p = str(p)
    if len(p) == 7 and p[4] == "-" and p[:4].lstrip("-").isdigit():
        m = int(p[5:7])
        return f"{MONTHS_FR[m - 1]} {p[:4]}" if 1 <= m <= 12 else p
    if len(p) == 6 and p[4] == "Q":
        return f"T{p[5]} {p[:4]}"
    if len(p) == 10 and p[4] == "-" and p[7] == "-":
        y, m, d = p.split("-")
        mi = int(m)
        if 1 <= mi <= 12:
            day = int(d)
            return f"{'1er' if day == 1 else day} {MONTHS_FR[mi - 1].rstrip('.') if mi in (3, 5, 6, 8) else MONTHS_FR[mi - 1]} {y}"
    return p


def plural(n: int, one: str, many: str) -> str:
    return one if abs(n) < 2 else many
