"""Ingestion budgets (invariant 30): ``registry/policies/budgets.yaml`` enforced on
every *live* ingestion run, whether scheduled or API-submitted.

Accounting lives in ``ops.budget_ledger`` (one row per run attempt and unit):

* ``source:{source_key}:requests`` — HTTP requests, limited per source by
  ``per_source_daily_requests`` and in total by ``daily.http_requests_total``;
* ``source:{source_key}:bytes`` — raw bytes acquired, limited in total by
  ``daily.http_bytes_total``.

Protocol: :func:`reserve_ingest` runs *before* any request, in one short
transaction serialised by an advisory transaction lock. It plans the run (the
connector's own ``plan``), and refuses to start it (``BudgetExhausted``, job code
``budget_exhausted``, never a success) when the planned requests would exceed
the remaining per-source or total daily request budget, or when the daily byte
budget is already used up. Otherwise it records the reservation and returns the
remaining allowances, which the run is capped to (``FetchBudget``). After the run
:func:`settle` records what was consumed. A reservation that is never settled
(crash) keeps counting with its reserved amount — budgets fail closed.

Days are UTC calendar days on the database clock. Fixture runs make no network
requests and are not charged.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from reva.kernel.errors import RevaError

log = logging.getLogger("reva.budgets")

BUDGET_LOCK_KEY = "reva:budget:ingest"
DAY_START_SQL = "(date_trunc('day', clock_timestamp() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"


class BudgetExhausted(RevaError):
    """The run would exceed a daily budget; it was not started."""

    code = "budget_exhausted"
    http_status = 429


@dataclass(frozen=True)
class IngestBudget:
    policy_version: str
    policy_sha256: str
    total_requests: int
    total_bytes: int
    per_source_requests: Mapping[str, int]


@dataclass(frozen=True)
class Reservation:
    source_key: str
    requests_operation_id: str
    bytes_operation_id: str
    planned_requests: int
    max_requests: int
    max_bytes: int
    policy_version: str


def load_budget(policy: Any = None) -> IngestBudget:
    """The pinned budgets policy (integrity-checked; a tampered file fails closed)."""
    if policy is None:
        from reva.autonomy.policy import budgets_policy

        policy = budgets_policy()
    content = policy.content
    daily = content.get("daily") or {}
    return IngestBudget(
        policy_version=str(content.get("policy_version", "unversioned")),
        policy_sha256=str(getattr(policy, "sha256", "")),
        total_requests=int(daily["http_requests_total"]),
        total_bytes=int(daily["http_bytes_total"]),
        per_source_requests={str(k): int(v) for k, v in (content.get("per_source_daily_requests") or {}).items()},
    )


def usage_today(conn: Connection, source_key: str) -> dict[str, int]:
    """Today's (UTC) usage: consumed amounts, or the reservation while a run is unsettled."""
    row = conn.execute(
        text(f"""SELECT
              COALESCE(sum(COALESCE(consumed, reserved)) FILTER (WHERE account_key = :src), 0) AS src,
              COALESCE(sum(COALESCE(consumed, reserved)) FILTER (WHERE unit = 'requests'), 0) AS req,
              COALESCE(sum(COALESCE(consumed, reserved)) FILTER (WHERE unit = 'bytes'), 0) AS byt
            FROM ops.budget_ledger
           WHERE account_key LIKE 'source:%' AND recorded_at >= {DAY_START_SQL}"""),
        {"src": f"source:{source_key}:requests"},
    ).one()
    return {"source_requests": int(row.src), "total_requests": int(row.req), "total_bytes": int(row.byt)}


def reserve_ingest(engine: Engine, *, job_id: UUID | str, attempt: int, source_key: str, planned_requests: int,
                   budget: IngestBudget | None = None) -> Reservation:
    """Reserve ``planned_requests`` for this run or raise :class:`BudgetExhausted`."""
    b = budget or load_budget()
    per_source = b.per_source_requests.get(source_key)
    if per_source is None:
        log.warning("no per-source daily request budget for %s; only the total budget applies", source_key)
    op = f"ingest:{job_id}:{attempt}"
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": BUDGET_LOCK_KEY})
        used = usage_today(conn, source_key)
        remaining_source = (per_source - used["source_requests"]) if per_source is not None else None
        remaining_total = b.total_requests - used["total_requests"]
        remaining_bytes = b.total_bytes - used["total_bytes"]
        limits = {"source_key": source_key, "planned_requests": planned_requests, "policy": b.policy_version,
                  "per_source_daily_requests": per_source, "used_source_requests": used["source_requests"],
                  "daily_requests_total": b.total_requests, "used_total_requests": used["total_requests"],
                  "daily_bytes_total": b.total_bytes, "used_total_bytes": used["total_bytes"]}
        if remaining_source is not None and planned_requests > remaining_source:
            raise BudgetExhausted(
                f"daily request budget for {source_key} would be exceeded "
                f"({used['source_requests']} used + {planned_requests} planned > {per_source}); run not started",
                **limits)
        if planned_requests > remaining_total:
            raise BudgetExhausted(
                f"total daily request budget would be exceeded ({used['total_requests']} used + "
                f"{planned_requests} planned > {b.total_requests}); run not started", **limits)
        if remaining_bytes <= 0:
            raise BudgetExhausted(f"total daily byte budget exhausted ({used['total_bytes']} >= {b.total_bytes}); "
                                  "run not started", **limits)
        ins = text("INSERT INTO ops.budget_ledger(account_key, operation_id, unit, reserved) VALUES (:a, :o, :u, :r) "
                   "ON CONFLICT (operation_id) DO NOTHING")
        conn.execute(ins, {"a": f"source:{source_key}:requests", "o": f"{op}:requests", "u": "requests",
                           "r": planned_requests})
        conn.execute(ins, {"a": f"source:{source_key}:bytes", "o": f"{op}:bytes", "u": "bytes", "r": 0})
    # Caps for this run (its own reservation included): requests left today before it started.
    caps = [remaining_total] + ([remaining_source] if remaining_source is not None else [])
    return Reservation(source_key, f"{op}:requests", f"{op}:bytes", planned_requests, max(0, min(caps)),
                       max(0, remaining_bytes), b.policy_version)


def settle(engine: Engine, reservation: Reservation, *, requests: int, nbytes: int) -> None:
    """Record what the run consumed (replaces the reservation in the daily sums)."""
    with engine.begin() as conn:
        upd = text("UPDATE ops.budget_ledger SET consumed = :c WHERE operation_id = :o")
        conn.execute(upd, {"c": max(0, int(requests)), "o": reservation.requests_operation_id})
        conn.execute(upd, {"c": max(0, int(nbytes)), "o": reservation.bytes_operation_id})
