"""Daily request/byte budgets for autonomy discovery (review R4-08).

``registry/policies/budgets.yaml`` (digest-pinned) is enforced through the shared
``ops.budget_ledger`` using the generic helpers in ``reva.operations.budgets``
(``reserve`` / ``consume``), on accounts

* ``autonomy:discovery:{source_key}:requests``
* ``autonomy:discovery:{source_key}:bytes``

The per-source limit (``per_source_daily_requests``) and the daily totals
(``daily.http_requests_total`` / ``http_bytes_total``) are shared with ingestion:
what ingestion (``source:{source_key}:*`` accounts) and other autonomy accounts
already used today is subtracted before reserving. A limit of ``0`` means no
request at all, and a source without a per-source limit is refused (fail closed).
Each job reserves before its first request and records what it consumed after
the run; an unsettled reservation keeps counting (fail closed). Fixture replays
make no network requests and are not charged.

Known limitation: ingestion and autonomy reservations are serialised by different
advisory locks, so two concurrent runs can overshoot a shared limit by at most one
run's reservation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, text

from reva.operations.budgets import (
    DAY_START_SQL,
    BudgetExhausted,
    consume,
    load_budget,
    reserve,
    usage_today,
    used_today,
)

DEFAULT_MAX_REQUESTS_PER_JOB = 5


@dataclass(frozen=True)
class DiscoveryReservation:
    source_key: str
    requests_operation_id: str
    bytes_operation_id: str
    max_requests: int
    max_bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {"source_key": self.source_key, "max_requests": self.max_requests, "max_bytes": self.max_bytes,
                "requests_operation_id": self.requests_operation_id}


def _autonomy_totals(conn: Any) -> tuple[int, int]:
    row = conn.execute(text(
        f"""SELECT COALESCE(sum(COALESCE(consumed, reserved)) FILTER (WHERE unit = 'requests'), 0) AS req,
                   COALESCE(sum(COALESCE(consumed, reserved)) FILTER (WHERE unit = 'bytes'), 0) AS byt
              FROM ops.budget_ledger
             WHERE account_key LIKE 'autonomy:%' AND recorded_at >= {DAY_START_SQL}""")).one()
    return int(row.req), int(row.byt)


def reserve_discovery(engine: Engine, source_key: str, *, job_key: str | None = None,
                      wanted_requests: int = DEFAULT_MAX_REQUESTS_PER_JOB, wanted_bytes: int = 8_000_000,
                      policy: Any = None) -> DiscoveryReservation:
    """Reserve today's discovery allowance or raise ``BudgetExhausted`` (nothing is fetched)."""
    b = load_budget(policy)
    per_source = b.per_source_requests.get(source_key)
    if per_source is None:
        raise BudgetExhausted(f"no per-source daily request budget for {source_key}: autonomy refuses (fail closed)",
                              source_key=source_key)
    req_key = f"autonomy:discovery:{source_key}:requests"
    byte_key = f"autonomy:discovery:{source_key}:bytes"
    with engine.connect() as conn:
        ingest = usage_today(conn, source_key)
        auto_req, auto_bytes = _autonomy_totals(conn)
        own_req = used_today(conn, req_key)
        own_bytes = used_today(conn, byte_key)
    remaining_source = per_source - ingest["source_requests"] - own_req
    remaining_total = b.total_requests - ingest["total_requests"] - auto_req
    avail = min(remaining_source, remaining_total)
    limits = {"source_key": source_key, "per_source_daily_requests": per_source,
              "daily_requests_total": b.total_requests, "policy": b.policy_version}
    if avail <= 0 or wanted_requests <= 0:
        raise BudgetExhausted(f"daily request budget for autonomy discovery on {source_key} is exhausted "
                              f"(limit {per_source}/day, total {b.total_requests}/day)", **limits)
    remaining_bytes = b.total_bytes - ingest["total_bytes"] - auto_bytes
    if remaining_bytes <= 0:
        raise BudgetExhausted("daily byte budget exhausted; discovery not started", **limits)
    op = f"discovery:{job_key or uuid.uuid4()}"
    n = min(wanted_requests, avail)
    reserve(engine, account_key=req_key, operation_id=f"{op}:requests", unit="requests", amount=n,
            daily_limit=own_req + avail)
    nbytes = min(wanted_bytes, remaining_bytes)
    reserve(engine, account_key=byte_key, operation_id=f"{op}:bytes", unit="bytes", amount=nbytes,
            daily_limit=own_bytes + remaining_bytes)
    return DiscoveryReservation(source_key, f"{op}:requests", f"{op}:bytes", n, nbytes)


def settle_discovery(engine: Engine, r: DiscoveryReservation, *, requests: int, nbytes: int) -> None:
    consume(engine, r.requests_operation_id, requests)
    consume(engine, r.bytes_operation_id, nbytes)


__all__ = ["BudgetExhausted", "DiscoveryReservation", "reserve_discovery", "settle_discovery"]
