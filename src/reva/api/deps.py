"""Shared request dependencies and envelope helpers."""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import Header, Query, Request
from sqlalchemy import Connection, Engine

from reva.api import schemas as s
from reva.api.errors import ApiProblem

DEFAULT_PAGE = 100
MAX_PAGE = 1000
_IDEMPOTENCY_RE = re.compile(r"^[\x21-\x7e]{8,200}$")


def get_engine(request: Request) -> Engine:
    return request.app.state.engine()  # type: ignore[no-any-return]


@contextmanager
def read_conn(request: Request) -> Iterator[Connection]:
    """Read-only transaction on the reva_api role."""
    with get_engine(request).connect() as conn:
        with conn.begin():
            yield conn


def page_limit(limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE, description="Page size (max 1000)")) -> int:
    return limit


def encode_cursor(values: Iterable[Any]) -> str:
    raw = json.dumps([str(v) if isinstance(v, (UUID, datetime)) else v for v in values], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _check_cursor_value(kind: str, v: Any) -> Any:
    if kind == "t":
        if not isinstance(v, str):
            raise ValueError
        ts = datetime.fromisoformat(v)
        if ts.tzinfo is None:
            raise ValueError
        return v
    if kind == "u":
        return str(UUID(str(v))) if isinstance(v, str) else _fail()
    if kind == "s":
        return v if isinstance(v, str) else _fail()
    if kind == "i":
        return v if isinstance(v, int) and not isinstance(v, bool) else _fail()
    raise AssertionError(kind)


def _fail() -> Any:
    raise ValueError


def decode_cursor(cursor: str | None, kinds: str) -> list[Any] | None:
    """Decode an opaque keyset cursor whose elements have the given kinds.

    ``kinds`` has one letter per element: ``t`` aware ISO timestamp, ``u`` UUID, ``s`` text, ``i`` integer.
    Anything else (wrong arity or element type) is a 400 ``invalid_cursor``, never a database error.
    """
    if cursor is None:
        return None
    try:
        pad = "=" * (-len(cursor) % 4)
        vals = json.loads(base64.urlsafe_b64decode(cursor + pad).decode())
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise ApiProblem(400, "invalid_cursor", "cursor is not valid for this endpoint") from None
    if not isinstance(vals, list) or len(vals) != len(kinds):
        raise ApiProblem(400, "invalid_cursor", "cursor is not valid for this endpoint")
    try:
        return [_check_cursor_value(k, v) for k, v in zip(kinds, vals, strict=True)]
    except (ValueError, TypeError):
        raise ApiProblem(400, "invalid_cursor", "cursor is not valid for this endpoint") from None


def page(rows: list[Any], limit: int, key: Any) -> tuple[list[Any], str | None]:
    """Rows were fetched with limit+1; return the page and the next cursor."""
    if len(rows) > limit:
        rows = rows[:limit]
        return rows, encode_cursor(key(rows[-1]))
    return rows, None


def idempotency_key(
    key: str | None = Header(
        None,
        alias="Idempotency-Key",
        description="Required on POSTs that start work: 8-200 printable ASCII characters",
    ),
) -> str:
    if key is None:
        raise ApiProblem(400, "idempotency_key_required", "Idempotency-Key header is required for this request")
    if not _IDEMPOTENCY_RE.match(key):
        raise ApiProblem(400, "invalid_idempotency_key", "Idempotency-Key must be 8-200 printable ASCII characters")
    return key


def require_aware(ts: datetime | None, name: str) -> datetime | None:
    if ts is not None and ts.tzinfo is None:
        raise ApiProblem(422, "validation_error", f"{name} must include a timezone offset (UTC recommended)")
    return ts


class Timer:
    def __init__(self) -> None:
        self.t0 = time.perf_counter()

    def cost(self, rows: int, **detail: Any) -> s.CostReport:
        return s.CostReport(wall_time_ms=round((time.perf_counter() - self.t0) * 1000, 3), rows=rows,
                            detail=detail or None)


SCENARIO_BANNER = s.ApiWarning(
    code="scenario_context",
    message="Assumption-conditioned scenario output in a simulation context: not a forecast, not an observation "
    "and never accepted as real-world evidence.",
)

FIXTURE_WARNING = s.ApiWarning(
    code="fixture_data",
    message="Contains test_fixture-origin data (captured provider responses replayed for testing); "
    "not a live production context.",
)


def origin_of(origins: Iterable[str | None]) -> str:
    vals = {o for o in origins if o}
    if not vals:
        return "none"
    if len(vals) > 1:
        return "mixed"
    return vals.pop()


def origin_warnings(origin: str) -> list[s.ApiWarning]:
    return [FIXTURE_WARNING] if origin in ("test_fixture", "mixed") else []


def release_ref(row: dict[str, Any] | None) -> s.BaselineReleaseRef | None:
    if row is None:
        return None
    return s.BaselineReleaseRef(
        id=row["id"],
        sequence=row.get("sequence"),
        knowledge_cutoff=row["knowledge_cutoff"],
        published_at=row.get("published_at"),
        origin=row["origin"],
        method=row.get("method"),
    )


def uncertainty_summary(records: Iterable[dict[str, Any]]) -> s.UncertaintySummary:
    recs = [r for r in records if r]
    if not recs:
        return s.UncertaintySummary(status="not_applicable", note="no rows")
    statuses = {r.get("status") for r in recs}
    kinds = {r.get("kind") for r in recs}
    levels = {r.get("level") for r in recs}
    deps = {r.get("dependence_group") for r in recs}
    if len(statuses) == 1:
        status = statuses.pop()
    else:
        status = "mixed"
    return s.UncertaintySummary(
        status=status,
        kind=kinds.pop() if len(kinds) == 1 else "mixed",
        level=levels.pop() if len(levels) == 1 else None,
        dependence=deps.pop() if len(deps) == 1 else "mixed",
        note=None if status != "unquantified" else "Source reports no uncertainty; unknown is not zero",
    )


def units_of(values: Iterable[dict[str, Any]]) -> list[str]:
    return sorted({v["unit_ref"] for v in values if isinstance(v, dict) and v.get("unit_ref")})


def period_label(temporal: dict[str, Any] | None) -> str | None:
    if not temporal:
        return None
    return temporal.get("label") or temporal.get("start")


def support_summary(row: dict[str, Any], prefix: str = "") -> s.SupportSummary:
    temporal = row[f"{prefix}temporal_extent"]
    return s.SupportSummary(
        id=row[f"{prefix}support_id"],
        temporal=temporal,
        spatial=row[f"{prefix}spatial_spec"],
        population=row[f"{prefix}population_spec"],
        aggregation=row[f"{prefix}aggregation"],
        period_label=period_label(temporal),
    )


def link(kind: str, ident: Any) -> s.ProvenanceLink:
    return s.ProvenanceLink(kind=kind, id=str(ident), href=f"/v1/provenance/{kind}/{ident}")  # type: ignore[arg-type]


def freshness(retrieved: Iterable[datetime | None], recorded: Iterable[datetime | None]) -> s.Freshness:
    r1 = [x for x in retrieved if x]
    r2 = [x for x in recorded if x]
    return s.Freshness(latest_retrieved_at=max(r1) if r1 else None, latest_recorded_at=max(r2) if r2 else None)
