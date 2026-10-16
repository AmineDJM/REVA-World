"""Shared plumbing of the GAPHUNT gap-fill atlas connectors (V3, agent GAPHUNT).

GAPHUNT adds official national / regional statistical offices for countries and territories that
the multilateral sources leave empty (``docs/dev/v3_gaps.md``). Each office is one module
(``atlas_gap_<office>.py``) declaring ``atlas_source()`` and ``async atlas_ingest(run)``; this
module only

* fetches the module's items through the run (``run.get``: the budget-metered live client or the
  fixture client, never a client built here), refuses empty bodies, archives every raw response
  with ``run.archive`` before anything is parsed, and registers the parsed tables with
  ``run.register`` (:func:`ingest_items`);
* exact helpers: strict decimal text, canonical JSON, calendar periods, the atlas frame builder.

Nothing here interprets a provider value: no conversion, no scaling, no imputation. Every failure
is typed; a table that does not validate is not registered and the summary says so. A budget
refusal always propagates (the platform ends the run as ``budget_exhausted``).
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import polars as pl

from reva.atlas.tables import ATLAS_SCHEMA
from reva.kernel.errors import SourceResponseError, ValidationProblem

_STRICT_DECIMAL = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]{1,3})?$")
_YEAR = re.compile(r"^([12][0-9]{3})$")
_MONTH = re.compile(r"^([12][0-9]{3})-(0[1-9]|1[0-2])$")


def code_digest(*modules: Any) -> str:
    """sha256 over the source of this module and the given connector modules (code identity)."""
    h = hashlib.sha256()
    for m in (inspect.getmodule(code_digest), *modules):
        h.update(inspect.getsource(m).encode())
    return "sha256:" + h.hexdigest()


def canon(obj: Mapping[str, Any]) -> str:
    """Canonical JSON object text (flags / selector columns)."""
    return json.dumps(dict(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def strict_decimal(value: Any) -> str | None:
    """The provider's number as a strict decimal string; ``None`` for an empty cell.

    Text that already is a strict decimal is kept verbatim; JSON numbers are spelled exactly through
    ``Decimal(str(x))`` (the shortest repr of the served binary float, never re-rounded). Anything
    non-numeric (``NaN``, ``1,000``, ``..``) raises ``ValidationProblem``: a value is never guessed."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValidationProblem("boolean is not a numeric cell value")
    text = str(value).strip()
    if text == "":
        return None
    if _STRICT_DECIMAL.match(text):
        return text
    try:
        d = Decimal(text)
    except InvalidOperation as e:
        raise ValidationProblem("non-numeric provider value", value=text[:40]) from e
    if not d.is_finite():
        raise ValidationProblem("non-finite provider value", value=text[:40])
    out = format(d, "f")
    if "." in out:
        out = out.rstrip("0").rstrip(".")
    if out in ("-0", ""):
        out = "0"
    if not _STRICT_DECIMAL.match(out):  # pragma: no cover - format(Decimal, "f") is always strict
        raise ValidationProblem("could not spell provider value as a strict decimal", value=text[:40])
    return out


def year_period(year: int) -> tuple[date, date]:
    return date(year, 1, 1), date(year + 1, 1, 1)


def period_of(text: str) -> tuple[date, date, str]:
    """``YYYY`` -> calendar year, ``YYYY-MM`` -> calendar month; anything else is refused."""
    t = str(text).strip()
    m = _YEAR.match(t)
    if m:
        s, e = year_period(int(m.group(1)))
        return s, e, "year"
    m = _MONTH.match(t)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        return date(y, mo, 1), (date(y + 1, 1, 1) if mo == 12 else date(y, mo + 1, 1)), "month"
    raise SourceResponseError("unexpected time period", value=t[:20], retryable=False)


def frame(rows: Sequence[Mapping[str, Any]]) -> pl.DataFrame:
    """Rows -> polars frame in ATLAS_SCHEMA (missing optional columns are null)."""
    return pl.DataFrame({c: [r.get(c) for r in rows] for c in ATLAS_SCHEMA.names()}, schema=ATLAS_SCHEMA)


def dedupe_check(rows: Sequence[Mapping[str, Any]], what: str) -> None:
    """Refuse two cells with the same (indicator, provider_geo, period): never pick one silently."""
    seen: dict[tuple[Any, ...], int] = {}
    for i, r in enumerate(rows):
        k = (r["indicator"], r["provider_geo"], r["period_start"], r["period_end"])
        if k in seen:
            raise SourceResponseError(
                f"{what}: duplicate cell", indicator=str(k[0])[:120], geo=str(k[1]), row=i, retryable=False
            )
        seen[k] = i


@dataclass(frozen=True)
class GapItem:
    """One GET of a gap-fill connector."""

    key: str
    url: str
    datasets: tuple[str, ...]
    media_types: tuple[str, ...] = ("application/json",)
    headers: Mapping[str, str] = field(default_factory=dict)
    max_bytes: int | None = None
    required: bool = True


@dataclass(frozen=True)
class GapTable:
    """One atlas table to register (``run.register``)."""

    dataset: str
    frame: pl.DataFrame
    raw_sha256s: tuple[str, ...]
    geo_scheme: str | None
    coverage_note: str
    rights: Mapping[str, Any]


@dataclass
class Fetched:
    body: bytes
    sha256: str
    url: str
    retrieved_at: str


async def ingest_items(
    run: Any,
    items: Sequence[GapItem],
    build: Callable[[Mapping[str, Fetched]], list[GapTable]],
) -> dict[str, Any]:
    """Fetch (``run.get``) -> archive (``run.archive``) -> parse (``build``) -> ``run.register``.

    Items whose datasets the run does not want are not requested. A failed required item stops
    table building (nothing partial is registered); a failed optional item is reported and the
    tables needing it are simply absent (``build`` decides from the fetched keys)."""
    from reva.operations.budgets import BudgetExhausted

    fetched: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    got: dict[str, Fetched] = {}
    for it in items:
        if not any(run.wants(d) for d in it.datasets):
            continue
        try:
            raw = await run.get(
                it.url,
                key=it.key,
                media_types=it.media_types,
                headers=dict(it.headers) or None,
                max_bytes=it.max_bytes,
            )
            if not raw.body or not raw.body.strip():
                raise SourceResponseError("empty response body", item=it.key, retryable=True)
        except BudgetExhausted:
            raise
        except Exception as e:  # typed provider errors are reported per item
            errors.append({"item": it.key, "error": type(e).__name__, "message": str(e)[:300], "required": it.required})
            continue
        sha = run.archive(raw)
        got[it.key] = Fetched(raw.body, sha, it.url, str(raw.retrieved_at))
        fetched.append({"item": it.key, "bytes": len(raw.body), "sha256": sha})
    tables: list[dict[str, Any]] = []
    if any(e["required"] for e in errors):
        return {"fetched": fetched, "errors": errors, "tables": tables, "ok": False}
    try:
        plans = build(got)
    except Exception as e:
        errors.append({"stage": "parse", "error": type(e).__name__, "message": str(e)[:500]})
        return {"fetched": fetched, "errors": errors, "tables": tables, "ok": False}
    for p in plans:
        if not run.wants(p.dataset):
            continue
        try:
            out = run.register(
                dataset=p.dataset,
                frame=p.frame,
                raw_sha256s=list(p.raw_sha256s),
                coverage_note=p.coverage_note,
                geo_scheme=p.geo_scheme,
                rights=dict(p.rights),
            )
        except Exception as e:
            errors.append({"stage": "register", "dataset": p.dataset, "error": type(e).__name__, "message": str(e)[:500]})
            continue
        cov = out.get("coverage") or {}
        tables.append(
            {
                "dataset_key": out["dataset_key"],
                "rows": out["row_count"],
                "created": out["created"],
                "countries": cov.get("countries"),
                "unmapped_provider_geo": out.get("unmapped_provider_geo"),
            }
        )
    return {"fetched": fetched, "errors": errors, "tables": tables, "ok": not errors}


__all__ = [
    "Fetched",
    "GapItem",
    "GapTable",
    "canon",
    "code_digest",
    "dedupe_check",
    "frame",
    "ingest_items",
    "period_of",
    "strict_decimal",
    "year_period",
]
