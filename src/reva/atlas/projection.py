"""Is an atlas cell an observation? The single projection vocabulary (FIX5, QA-DATA D1/D2).

Rules and vocabulary live in ``registry/atlas/projection_kinds.yaml`` (read it for the full statement). Every reader
uses this module: :data:`PROJECTION_SQL` / :func:`projection_sql` in SQL (atlas query layer, QA), :func:`is_projection`
in Python (decision-engine baseline, forecast registries, energy backtests, NLQA). A cell is not an observation when
its ``estimate_kind`` is a projection kind or a kind listed in no class (fail-safe), a projection flag is set, or its
period starts after a release date it carries (``series_last_updated``) or after the date its table was recorded
(fetch) — no provider observes a period that has not begun. Nothing is rewritten: the rule is applied at read time.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml

PROJECTION_KINDS_FILE = Path(__file__).resolve().parents[3] / "registry" / "atlas" / "projection_kinds.yaml"
KindClass = Literal["observation", "estimate", "projection", "unknown"]
CLASSES: tuple[str, ...] = ("observation", "estimate", "projection")
_SQL_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
_ISO = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})")


@dataclass(frozen=True)
class ProjectionVocabulary:
    classes: Mapping[str, tuple[str, ...]]
    truthy_flags: tuple[str, ...]
    true_flags: tuple[str, ...]
    release_date_flags: tuple[str, ...]
    date_rule_exempt_kinds: tuple[str, ...]
    date_rule_exempt_tables: tuple[str, ...]

    @property
    def projection_kinds(self) -> tuple[str, ...]:
        return self.classes["projection"]

    @property
    def listed_non_projection(self) -> tuple[str, ...]:
        return self.classes["observation"] + self.classes["estimate"]


@lru_cache(maxsize=1)
def vocabulary(path: Path = PROJECTION_KINDS_FILE) -> ProjectionVocabulary:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    cls = {c: tuple(str(k) for k in (doc["classes"].get(c) or ())) for c in CLASSES}
    seen: dict[str, str] = {}
    for c, kinds in cls.items():
        for k in kinds:
            if k in seen:
                raise ValueError(f"{path.name}: estimate_kind {k!r} listed in {seen[k]} and {c}")
            if not _SQL_IDENT.match(k):
                raise ValueError(f"{path.name}: estimate_kind {k!r} is not a plain identifier")
            seen[k] = c
    flags = [*(doc.get("truthy_flags") or ()), *(doc.get("true_flags") or ()), *(doc.get("release_date_flags") or ())]
    for f in flags:
        if not _SQL_IDENT.match(str(f)):
            raise ValueError(f"{path.name}: flag {f!r} is not a plain identifier")
    return ProjectionVocabulary(
        classes=cls,
        truthy_flags=tuple(doc.get("truthy_flags") or ()),
        true_flags=tuple(doc.get("true_flags") or ()),
        release_date_flags=tuple(doc.get("release_date_flags") or ()),
        date_rule_exempt_kinds=tuple(doc.get("date_rule_exempt_kinds") or ()),
        date_rule_exempt_tables=tuple(doc.get("date_rule_exempt_tables") or ()),
    )


def kind_class(kind: Any) -> KindClass | None:
    """``observation`` | ``estimate`` | ``projection`` | ``unknown`` (set but unlisted); ``None`` when no kind."""
    k = str(kind or "").strip()
    if not k:
        return None
    for c, kinds in vocabulary().classes.items():
        if k in kinds:
            return c  # type: ignore[return-value]
    return "unknown"


PROJECTION_KINDS: tuple[str, ...] = vocabulary().projection_kinds


def _truthy(v: Any) -> bool:
    if v is None or v is False:
        return False
    return str(v).strip().lower() not in ("", "false", "0", "no", "none")


def _as_date(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, date):
        return v
    m = _ISO.match(str(v))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def flags_say_projection(flags: Mapping[str, Any] | None) -> bool:
    """Rules 1-3 (the cell's own kind and projection flags)."""
    f = flags or {}
    v = vocabulary()
    if kind_class(f.get("estimate_kind")) in ("projection", "unknown"):
        return True
    if any(_truthy(f.get(k)) for k in v.truthy_flags):
        return True
    return any(str(f.get(k) or "").strip().lower() == "true" for k in v.true_flags)


def is_projection(
    flags: Mapping[str, Any] | None,
    *,
    period_start: Any = None,
    recorded: Any = None,
    table: str | None = None,
    as_of: date | None = None,
) -> bool:
    """True when the cell is not an observation (rules 1-5 of ``projection_kinds.yaml``).

    ``period_start``: the cell's period start (date or ISO text); ``recorded``: the date its table was recorded
    (fetched), else ``as_of`` (default: today) bounds the date rule; ``table``: ``"<source>.<dataset>"``."""
    f = flags or {}
    if flags_say_projection(f):
        return True
    start = _as_date(period_start)
    if start is None:
        return False
    v = vocabulary()
    if str(f.get("estimate_kind") or "") in v.date_rule_exempt_kinds or (table and table in v.date_rule_exempt_tables):
        return False
    for k in v.release_date_flags:
        rel = _as_date(f.get(k))
        if rel is not None and start > rel:
            return True
    bound = _as_date(recorded) or as_of or date.today()
    return start > bound


def estimate_status(flags: Mapping[str, Any] | None, **kw: Any) -> str | None:
    """``projection`` | ``estimate`` | ``None`` (as reported)."""
    if is_projection(flags, **kw):
        return "projection"
    return "estimate" if kind_class((flags or {}).get("estimate_kind")) == "estimate" else None


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join("'" + k + "'" for k in values) + ")" if values else "('')"


def projection_sql(
    alias: str = "x", *, period: bool = False, recorded_sql: str | None = None, table_exempt_sql: str | None = None
) -> str:
    """SQL (DuckDB) boolean: the cell ``alias`` is not an observation.

    ``period=False``: rules 1-3 on ``alias.flags`` only. ``period=True`` adds rule 4 (release date in the flags,
    needs ``alias.period_start`` DATE) and, with ``recorded_sql`` (a DATE expression: the table's record date; NULL
    turns the rule off for that row), rule 5. ``table_exempt_sql``: a boolean SQL expression true for exempt tables.
    """
    v = vocabulary()
    fl = f"{alias}.flags"
    ek = f"coalesce(json_extract_string({fl}, '$.estimate_kind'), '')"
    parts = [
        f"{ek} IN {_in(v.projection_kinds)}",
        f"({ek} <> '' AND {ek} NOT IN {_in(v.listed_non_projection)})",
    ]
    parts += [f"coalesce(json_extract_string({fl}, '$.{k}'), 'false') NOT IN ('false', '')" for k in v.truthy_flags]
    parts += [f"coalesce(json_extract_string({fl}, '$.{k}'), 'false') = 'true'" for k in v.true_flags]
    if period:
        dated = [
            f"coalesce(TRY_CAST(substr(json_extract_string({fl}, '$.{k}'), 1, 10) AS DATE) < {alias}.period_start, "
            "false)"
            for k in v.release_date_flags
        ]
        if recorded_sql is not None:
            dated.append(f"coalesce({alias}.period_start > {recorded_sql}, false)")
        if dated:
            exempt = f"{ek} IN {_in(v.date_rule_exempt_kinds)}"
            if table_exempt_sql:
                exempt = f"({exempt} OR {table_exempt_sql})"
            parts.append(f"(NOT {exempt} AND ({' OR '.join(dated)}))")
    return "(" + " OR ".join(parts) + ")"


#: Rules 1-3 on ``x.flags`` (no period columns needed).
PROJECTION_SQL = projection_sql("x")
