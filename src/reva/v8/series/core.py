"""Pure helpers of the V8 series service: indicator parsing, period precision and codes, transform rules.

No I/O here (unit-tested without a database or an atlas snapshot).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

# --------------------------------------------------------------------------- indicators

Dims = tuple[tuple[str, str], ...]


def parse_indicator(indicator: str) -> tuple[str, Dims]:
    """``"UNE_2EAP_SEX_AGE_RT|sex=SEX_F|classif1=AGE_Y15-24"`` -> (``"UNE_2EAP_SEX_AGE_RT"``, ``(("sex", ...), ...)``).

    The measure is every leading token without ``=`` (FAO ``"2511|5123"`` keeps both codes); the filters are the
    ``key=value`` tokens in order. A positional token after a filter is kept as a filter ``_<position>``."""
    parts = indicator.split("|")
    measure: list[str] = []
    dims: list[tuple[str, str]] = []
    for i, p in enumerate(parts):
        if "=" in p and p.split("=", 1)[0]:
            k, v = p.split("=", 1)
            dims.append((k, v))
        elif not dims:
            measure.append(p)
        else:
            dims.append((f"_{i}", p))
    return "|".join(measure), tuple(dims)


def series_key(source: str, dataset: str, indicator: str) -> str:
    return f"{source}.{dataset}.{indicator}"


def split_series_key(key: str) -> tuple[str, str, str] | None:
    """``"wb.wdi_econ.NY.GDP.PCAP.CD"`` -> (``wb``, ``wdi_econ``, ``NY.GDP.PCAP.CD``); source and dataset hold no dot."""
    parts = key.split(".", 2)
    if len(parts) != 3 or not all(parts):
        return None
    return parts[0], parts[1], parts[2]


#: Dimension values that usually mean "all" (preferred when a filter is left open).
TOTAL_HINTS = ("_t", "t", "total", "sex_t", "sex_btsx", "all", "_z", "ensemble", "both", "tot", "_tot")


def default_score(dims: Dims) -> tuple[int, str]:
    """Sort key choosing the default variant of a measure: most dimensions at a "total" value first."""
    totals = 0
    for k, v in dims:
        lv = v.lower()
        if lv in TOTAL_HINTS or lv.endswith("_t") or lv.endswith("_total") or "total" in lv or "aggregate_total" in lv:
            totals += 1
        elif k.startswith("proj") or k == "variant":
            totals -= 2
    return (-totals, "|".join(f"{k}={v}" for k, v in dims))


# --------------------------------------------------------------------------- text search

_WS = re.compile(r"[^a-z0-9%°$€]+")


def fold(text: str | None) -> str:
    """Lower-case, accents removed, punctuation as spaces (search normalisation)."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return " ".join(_WS.split(t)).strip()


def tokens(query: str) -> list[str]:
    return [t for t in fold(query).split(" ") if t]


# --------------------------------------------------------------------------- periods

PRECISIONS = ("year", "semester", "quarter", "month", "day")
PRECISION_FR = {"year": "année", "semester": "semestre", "quarter": "trimestre", "month": "mois", "day": "jour"}

#: SQL (DuckDB) span class of a cell ``x`` that the year view does not attach (``x.year IS NULL``) — or every
#: cell for a sub-annual precision. The year precision uses the atlas year attachment (``x.year``).
SPAN_SQL = (
    "CASE WHEN datediff('day', x.period_start, x.period_end) BETWEEN 28 AND 31 THEN 'month' "
    "WHEN datediff('day', x.period_start, x.period_end) BETWEEN 89 AND 92 THEN 'quarter' "
    "WHEN datediff('day', x.period_start, x.period_end) BETWEEN 181 AND 184 THEN 'semester' "
    "WHEN datediff('day', x.period_start, x.period_end) <= 1 THEN 'day' ELSE 'other' END"
)

_YEAR = re.compile(r"^-?\d{1,5}$")
_MONTH = re.compile(r"^(-?\d{1,5})-(0[1-9]|1[0-2])$")
_QUARTER = re.compile(r"^(-?\d{1,5})-Q([1-4])$")
_SEMESTER = re.compile(r"^(-?\d{1,5})-S([12])$")
_DAY = re.compile(r"^(-?\d{1,5})-(\d{2})-(\d{2})$")


def _ymd(iso: str) -> tuple[int, int, int]:
    neg = iso.startswith("-")
    y, m, d = (iso[1:] if neg else iso).split("-")[:3]
    return (-int(y) if neg else int(y)), int(m), int(d)


def period_code(precision: str, period_start: str, year: int | None = None) -> str:
    """Display/URL code of a cell's period at ``precision`` (``2020``, ``2020-S1``, ``2020-Q3``, ``2020-07``,
    ``2020-07-14``). A sub-annual period that does not start on its calendar boundary keeps its ISO start date."""
    y, m, d = _ymd(period_start)
    if precision == "year":
        return str(year if year is not None else y)
    if precision == "month" and d == 1:
        return f"{y}-{m:02d}"
    if precision == "quarter" and d == 1 and m in (1, 4, 7, 10):
        return f"{y}-Q{(m - 1) // 3 + 1}"
    if precision == "semester" and d == 1 and m in (1, 7):
        return f"{y}-S{1 if m == 1 else 2}"
    return period_start[:11] if period_start.startswith("-") else period_start[:10]


def period_label_fr(precision: str, code: str) -> str:
    months = ("janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc.")
    if m := _MONTH.match(code):
        return f"{months[int(m.group(2)) - 1]} {m.group(1)}"
    if m := _QUARTER.match(code):
        return f"T{m.group(2)} {m.group(1)}"
    if m := _SEMESTER.match(code):
        return f"S{m.group(2)} {m.group(1)}"
    if m := _DAY.match(code):
        return f"{int(m.group(3))} {months[int(m.group(2)) - 1]} {m.group(1)}"
    return code


@dataclass(frozen=True)
class PeriodSel:
    precision: str
    year: int | None = None  # year precision
    start: date | None = None  # sub-annual: exact period start (positive years only)


def parse_period(precision: str, code: str) -> PeriodSel | None:
    """The selection a period code stands for at ``precision`` (``None`` when the code does not fit)."""
    if precision == "year":
        return PeriodSel("year", year=int(code)) if _YEAR.match(code) else None
    try:
        if (m := _MONTH.match(code)) and precision == "month":
            return PeriodSel(precision, start=date(int(m.group(1)), int(m.group(2)), 1))
        if (m := _QUARTER.match(code)) and precision == "quarter":
            return PeriodSel(precision, start=date(int(m.group(1)), 3 * int(m.group(2)) - 2, 1))
        if (m := _SEMESTER.match(code)) and precision == "semester":
            return PeriodSel(precision, start=date(int(m.group(1)), 1 if m.group(2) == "1" else 7, 1))
        if m := _DAY.match(code):
            return PeriodSel(precision, start=date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
    except ValueError:
        return None
    return None


def default_precision(counts: dict[str, int]) -> str | None:
    """The precision a series is shown at by default: the year view when it holds at least half of the value cells
    (annual series, and annual series with a few sub-annual cells), else the most populated sub-annual one."""
    total = sum(counts.values())
    if total == 0:
        return None
    if counts.get("year", 0) * 2 >= total:
        return "year"
    sub = [(counts.get(p, 0), p) for p in PRECISIONS[1:] if counts.get(p, 0) > 0]
    if not sub:
        return "year" if counts.get("year") else None
    return max(sub)[1]


# --------------------------------------------------------------------------- units and transforms

_PERCENT = re.compile(r"%|percent|pourcent|\bpct\b|\bpp\b|points? de pourcentage|per ?cent", re.I)
_INTENSIVE = re.compile(
    r"per |/|par |index|indice|ratio|share|score|rank|rang|°|degree|\byears?\b|\bans?\b|density|rate|taux|"
    r"\bper\b|capita|habitant|inhabitant|elasticit|coefficient|gini|\b1\b|^1$|scale|level|niveau",
    re.I,
)
_EXTENSIVE = re.compile(
    r"usd|us\$|\$|eur|€|dollar|lcu|currency|monnaie|national currency|ncu|xdc|million|billion|thousand|milliers|"
    r"persons?|people|personnes|inhabitants|population|number|nombre|count|head|tonnes?|tons?|\bt\b|kg|kilogram|"
    r"barrels?|bbl|twh|gwh|kwh|mwh|joules?|pj|ej|btu|quad|mtoe|ktoe|toe|cubic|m3|bcm|bcf|litres?|liters?|hectares?|"
    r"\bha\b|km2|km²|sq km|square|tonnes co2|mtco2|co2",
    re.I,
)


def unit_kind(unit: str | None) -> str:
    """``percent`` | ``intensive`` (rate, index, per-unit, score…) | ``extensive`` (amounts, counts, masses,
    energy…) | ``unknown``. Conservative: a per-capita computation is offered only for ``extensive`` units."""
    u = (unit or "").strip()
    if not u:
        return "unknown"
    if _PERCENT.search(u):
        return "percent"
    if _INTENSIVE.search(u + " "):
        return "intensive"
    if _EXTENSIVE.search(u):
        return "extensive"
    return "unknown"


@dataclass(frozen=True)
class TransformRule:
    ok: bool
    reason: str | None = None


_PER_CAPITA = re.compile(
    r"per ?capita|par habitant|per (?:person|inhabitant|head|1 ?000|100|10 ?000|100 ?000|million)|pcap|_pc\b|\.pc\b|"
    r"per_capita|percap|density|densit|/\s*(?:capita|hab|person|km)",
    re.I,
)


def transform_rules(
    *,
    unit: str | None,
    precision: str | None,
    numeric: bool,
    min_value: float | None,
    countries: bool,
    context: str = "",
) -> dict[str, TransformRule]:
    """Which Comparer transforms the inputs allow (French reasons when not).

    * ``index`` (base 100): numeric, strictly positive values, not a percentage (an index of a rate misleads).
    * ``yoy`` (variation sur un an, %): numeric, regular precision (year/semester/quarter/month), strictly positive,
      not already a percentage or rate.
    * ``percap`` (par habitant, REVA computation): annual numeric amounts or counts (extensive unit) of ISO3
      countries, divided by the atlas population of the same country and year; never for a series whose label,
      code or unit (``context``) says it is already per capita, per 1000, per km²…"""
    kind = unit_kind(unit)
    out: dict[str, TransformRule] = {}
    if not numeric:
        nope = TransformRule(False, "valeurs non numériques")
        return {"index": nope, "yoy": nope, "percap": nope}
    positive = min_value is not None and min_value > 0
    if kind == "percent":
        out["index"] = TransformRule(False, "unité en pourcentage : un indice base 100 d’un taux induirait en erreur")
    elif not positive:
        out["index"] = TransformRule(False, "valeurs nulles ou négatives : pas d’indice base 100")
    else:
        out["index"] = TransformRule(True)
    if precision not in ("year", "semester", "quarter", "month"):
        out["yoy"] = TransformRule(False, "précision irrégulière : pas de variation sur un an")
    elif kind == "percent" or (kind == "intensive" and re.search(r"rate|taux", unit or "", re.I)):
        out["yoy"] = TransformRule(False, "déjà un taux : sa variation en % n’a pas de sens")
    elif not positive:
        out["yoy"] = TransformRule(False, "valeurs nulles ou négatives : variation en % non définie")
    else:
        out["yoy"] = TransformRule(True)
    if precision != "year":
        out["percap"] = TransformRule(False, "population annuelle seulement : série non annuelle")
    elif not countries:
        out["percap"] = TransformRule(False, "lieux qui ne sont pas des pays : pas de population atlas")
    elif _PER_CAPITA.search(f"{context} {unit or ''}"):
        out["percap"] = TransformRule(False, "déjà rapporté à la population (ou à une surface) : pas de second calcul")
    elif kind != "extensive":
        out["percap"] = TransformRule(
            False, f"unité « {unit or '?'} » non additive ou inconnue : pas de calcul par habitant"
        )
    else:
        out["percap"] = TransformRule(True)
    return out


# --------------------------------------------------------------------------- choropleth scale hint


def scale_hint(values: Sequence[float]) -> str:
    """``diverging`` (both signs), ``log`` (strictly positive, right-skewed and spanning >= 2 orders of magnitude),
    ``linear`` (few distinct values, or a near-symmetric spread) or ``quantile`` (other skewed distributions).
    The UI may override it."""
    v = sorted(values)
    if not v:
        return "quantile"
    lo, hi = v[0], v[-1]
    if lo < 0 < hi:
        return "diverging"
    n = len(v)
    if n < 8 or len(set(v)) <= 6:
        return "linear"
    spread = hi - lo
    if spread <= 0:
        return "linear"
    mean = sum(v) / n
    med = v[n // 2]
    skew = (mean - med) / spread
    if lo > 0 and hi / lo >= 100 and skew > 0.03:
        return "log"
    return "linear" if abs(skew) < 0.02 else "quantile"
