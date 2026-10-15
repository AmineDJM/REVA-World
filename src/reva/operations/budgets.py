"""Daily egress budgets (invariant 30): ``registry/policies/budgets.yaml`` enforced on
every *live* provider request path through one persistent ledger, ``ops.budget_ledger``.

Keys
----
Budgets are per **source** (``registry/sources/*.yaml`` ``source_key``), not per
connector name: a connector is charged to its descriptor's ``source_key``
(:func:`budget_source_key`; e.g. connector ``worldbank.wdi.indicators`` →
source ``worldbank.wdi``). Ledger accounts are built by one function,
:func:`account_key`:

* ingestion (CLI ``reva ingest --live``, worker ``ingest`` jobs, scheduled ingests):
  ``source:{source_key}:{unit}``;
* autonomy discovery: ``autonomy:discovery:{source_key}:{unit}``.

``unit`` is ``requests`` (logical provider GETs) or ``bytes`` (response bytes).
The per-source limit (``per_source_daily_requests``) counts the request accounts
of *every* purpose for that source; the daily totals (``daily.http_requests_total``,
``daily.http_bytes_total``) count every egress account. A source without a
per-source limit is refused (fail closed) and a limit of ``0`` means no request.

Protocol
--------
Every egress reservation (ingest, discovery, and the in-run top-ups below) runs
in one short transaction serialised by the **single** advisory transaction lock
:data:`BUDGET_LOCK_KEY`, so concurrent reservations from different paths can
never together exceed a shared limit. :func:`reserve_ingest` runs *before* the
first request and refuses to start a run (:class:`BudgetExhausted`, job code
``budget_exhausted``, never a success) when the planned requests do not fit the
remaining per-source or total request budget or the byte budget is used up.

During the run, :func:`metered` wraps the live HTTP client: before each request
it makes sure the ledger already holds a reservation covering it (topping the
reservation up under the same lock, or refusing the request with
``BudgetExhausted`` without calling the client), and caps the response size to
the reserved bytes. The ledger reservation is therefore always >= what was
actually requested, whatever the connector's follow-ups do. After the run
:func:`settle` / :func:`settle_metered` records the actual usage. A reservation
that is never settled (crash) keeps counting with its reserved amount — budgets
fail closed.

Days are UTC calendar days on the **database** clock (``clock_timestamp()``,
independent of the session time zone); a row counts for the day it was reserved.
Fixture replays make no network requests and are never charged.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from reva.kernel.errors import RevaError

log = logging.getLogger("reva.budgets")

# One lock for every egress reservation (ingest, discovery, in-run top-ups): the
# per-source and total limits are shared by all of them.
BUDGET_LOCK_KEY = "reva:budget:egress"
DAY_START_SQL = "(date_trunc('day', clock_timestamp() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"

# purpose -> account prefix (the only egress accounts the ledger knows)
EGRESS_PURPOSES: Mapping[str, str] = {"ingest": "source", "discovery": "autonomy:discovery"}
UNITS = ("requests", "bytes")
_EGRESS_SQL = " OR ".join(f"starts_with(account_key, '{p}:')" for p in EGRESS_PURPOSES.values())
DEFAULT_REQUEST_BYTES = 52_428_800  # per-request byte cap when neither the caller nor the client says


class BudgetExhausted(RevaError):
    """The run (or request) would exceed a daily budget; it was not started."""

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


# --------------------------------------------------------------------------- keys
def account_key(source_key: str, unit: str, purpose: str = "ingest") -> str:
    """The canonical ledger account for ``purpose`` traffic to ``source_key``."""
    if purpose not in EGRESS_PURPOSES:
        raise ValueError(f"unknown egress purpose {purpose!r}")
    if unit not in UNITS:
        raise ValueError(f"unknown budget unit {unit!r}")
    if not source_key or ":" in source_key:
        raise ValueError("source_key must be a non-empty registered source key")
    return f"{EGRESS_PURPOSES[purpose]}:{source_key}:{unit}"


def source_accounts(source_key: str, unit: str = "requests") -> list[str]:
    """Every purpose's account for ``source_key`` (they share the per-source limit)."""
    return [account_key(source_key, unit, p) for p in EGRESS_PURPOSES]


def budget_source_key(connector_or_spec: Any) -> str:
    """The budget key of a connector: its descriptor's registered ``source_key``."""
    spec = connector_or_spec.describe() if hasattr(connector_or_spec, "describe") else connector_or_spec
    key = getattr(spec, "source_key", None)
    if not isinstance(key, str) or not key:
        raise BudgetExhausted(
            "connector declares no source_key: no budget applies, refused (fail closed)",
            reason="unknown_source",
            connector=str(getattr(spec, "name", ""))[:128],
        )
    return key


def source_limit(b: IngestBudget, source_key: str) -> int:
    """The per-source daily request limit; unknown sources and limit 0 are refused."""
    limit = b.per_source_requests.get(source_key)
    if limit is None:
        raise BudgetExhausted(
            f"no per-source daily request budget for {source_key}: refused (fail closed)",
            reason="unknown_source",
            source_key=source_key,
            policy=b.policy_version,
        )
    if limit <= 0:
        raise BudgetExhausted(
            f"the daily request budget for {source_key} is 0: no requests allowed",
            reason="zero_limit",
            source_key=source_key,
            per_source_daily_requests=limit,
            policy=b.policy_version,
        )
    return limit


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


# --------------------------------------------------------------------------- usage
def _lock(conn: Connection, key: str = BUDGET_LOCK_KEY) -> None:
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": key})


def usage_today(conn: Connection, source_key: str) -> dict[str, int]:
    """Today's (UTC, DB clock) egress usage; unsettled rows count their reservation.

    ``source_requests`` sums the request accounts of every purpose for this source;
    the totals sum every egress account.
    """
    row = conn.execute(
        text(f"""SELECT
              COALESCE(sum(COALESCE(consumed, reserved)) FILTER (WHERE account_key = ANY(:src)), 0) AS src,
              COALESCE(sum(COALESCE(consumed, reserved)) FILTER (WHERE unit = 'requests'), 0) AS req,
              COALESCE(sum(COALESCE(consumed, reserved)) FILTER (WHERE unit = 'bytes'), 0) AS byt
            FROM ops.budget_ledger
           WHERE ({_EGRESS_SQL}) AND recorded_at >= {DAY_START_SQL}"""),
        {"src": source_accounts(source_key)},
    ).one()
    return {"source_requests": int(row.src), "total_requests": int(row.req), "total_bytes": int(row.byt)}


def _remaining(b: IngestBudget, limit: int, used: Mapping[str, int]) -> tuple[int, int, int, int]:
    rem_source = limit - used["source_requests"]
    rem_total = b.total_requests - used["total_requests"]
    return rem_source, rem_total, min(rem_source, rem_total), b.total_bytes - used["total_bytes"]


_INSERT = text(
    "INSERT INTO ops.budget_ledger(account_key, operation_id, unit, reserved) VALUES (:a, :o, :u, :r) "
    "ON CONFLICT (operation_id) DO NOTHING"
)


# --------------------------------------------------------------------------- ingestion
def reserve_ingest(
    engine: Engine,
    *,
    job_id: UUID | str,
    attempt: int,
    source_key: str,
    planned_requests: int,
    budget: IngestBudget | None = None,
) -> Reservation:
    """Reserve ``planned_requests`` for this live run or raise :class:`BudgetExhausted`.

    Nothing is recorded when the run is refused. ``max_requests`` / ``max_bytes`` of
    the result are what was left today before the run (its caps); requests beyond
    the reservation are topped up (or refused) per request by :func:`metered`.
    """
    b = budget or load_budget()
    per_source = source_limit(b, source_key)
    if planned_requests < 0:
        raise ValueError("planned_requests must be >= 0")
    op = f"ingest:{job_id}:{attempt}"
    with engine.begin() as conn:
        _lock(conn)
        used = usage_today(conn, source_key)
        remaining_source, remaining_total, remaining, remaining_bytes = _remaining(b, per_source, used)
        limits = {
            "source_key": source_key,
            "planned_requests": planned_requests,
            "policy": b.policy_version,
            "per_source_daily_requests": per_source,
            "used_source_requests": used["source_requests"],
            "daily_requests_total": b.total_requests,
            "used_total_requests": used["total_requests"],
            "daily_bytes_total": b.total_bytes,
            "used_total_bytes": used["total_bytes"],
        }
        if remaining_source <= 0 or planned_requests > remaining_source:
            raise BudgetExhausted(
                f"daily request budget for {source_key} would be exceeded "
                f"({used['source_requests']} used + {planned_requests} planned > {per_source}); run not started",
                reason="source_limit",
                **limits,
            )
        if remaining_total <= 0 or planned_requests > remaining_total:
            raise BudgetExhausted(
                f"total daily request budget would be exceeded ({used['total_requests']} used + "
                f"{planned_requests} planned > {b.total_requests}); run not started",
                reason="total_limit",
                **limits,
            )
        if remaining_bytes <= 0:
            raise BudgetExhausted(
                f"total daily byte budget exhausted ({used['total_bytes']} >= {b.total_bytes}); run not started",
                reason="byte_limit",
                **limits,
            )
        conn.execute(
            _INSERT,
            {"a": account_key(source_key, "requests"), "o": f"{op}:requests", "u": "requests", "r": planned_requests},
        )
        conn.execute(_INSERT, {"a": account_key(source_key, "bytes"), "o": f"{op}:bytes", "u": "bytes", "r": 0})
    return Reservation(
        source_key,
        f"{op}:requests",
        f"{op}:bytes",
        planned_requests,
        max(0, remaining),
        max(0, remaining_bytes),
        b.policy_version,
    )


def top_up(
    engine: Engine, reservation: Reservation, *, unit: str, want: int, budget: IngestBudget | None = None
) -> int:
    """Grow a live reservation by up to ``want`` units (under the egress lock).

    Returns the units granted (``0`` when the daily budget is used up). The ledger
    reservation only grows by what fits the remaining per-source/total budget.
    """
    if want <= 0:
        return 0
    b = budget or load_budget()
    per_source = source_limit(b, reservation.source_key)
    op = reservation.requests_operation_id if unit == "requests" else reservation.bytes_operation_id
    with engine.begin() as conn:
        _lock(conn)
        used = usage_today(conn, reservation.source_key)
        _, _, remaining, remaining_bytes = _remaining(b, per_source, used)
        grant = max(0, min(want, remaining if unit == "requests" else remaining_bytes))
        if grant:
            n = conn.execute(
                text(
                    "UPDATE ops.budget_ledger SET reserved = reserved + :g WHERE operation_id = :o AND consumed IS NULL"
                ),
                {"g": grant, "o": op},
            ).rowcount
            if n != 1:  # settled or unknown reservation: nothing more may be charged to it
                return 0
    return grant


class MeteredHttp:
    """Live HTTP wrapper enforcing the persistent daily budget per request.

    Before each request the ledger reservation must already cover it: the wrapper
    tops it up under the egress lock or refuses with :class:`BudgetExhausted`
    *without* calling the wrapped client. Response bodies are capped to the
    reserved bytes. ``requests`` / ``bytes`` are the actual usage for :func:`settle`.
    """

    def __init__(
        self,
        inner: Any,
        engine: Engine,
        reservation: Reservation,
        *,
        budget: IngestBudget | None = None,
        request_chunk: int = 10,
    ) -> None:
        self.inner = inner
        self.engine = engine
        self.reservation = reservation
        self.budget = budget
        self.request_chunk = max(1, request_chunk)
        self.reserved_requests = reservation.planned_requests
        self.reserved_bytes = 0
        self.requests = 0
        self.bytes = 0
        self.refused = 0

    @property
    def origin(self) -> str:
        return str(getattr(self.inner, "origin", "live"))

    def __getattr__(self, name: str) -> Any:  # write_manifest, captured, ... of the wrapped client
        return getattr(self.inner, name)

    def _refuse(self, unit: str) -> BudgetExhausted:
        self.refused += 1
        return BudgetExhausted(
            f"daily {unit} budget for {self.reservation.source_key} is used up: request not made",
            reason=f"in_run_{unit}_limit",
            source_key=self.reservation.source_key,
            policy=self.reservation.policy_version,
        )

    async def get(self, url: str, **kw: Any) -> Any:
        if self.requests + 1 > self.reserved_requests:
            granted = await asyncio.to_thread(
                top_up, self.engine, self.reservation, unit="requests", want=self.request_chunk, budget=self.budget
            )
            self.reserved_requests += granted
            if self.requests + 1 > self.reserved_requests:
                raise self._refuse("requests")
        per_request = int(kw.get("max_bytes") or getattr(self.inner, "max_bytes", 0) or DEFAULT_REQUEST_BYTES)
        headroom = self.reserved_bytes - self.bytes
        if headroom < per_request:
            granted = await asyncio.to_thread(
                top_up, self.engine, self.reservation, unit="bytes", want=per_request - headroom, budget=self.budget
            )
            self.reserved_bytes += granted
            headroom += granted
            if headroom <= 0:
                raise self._refuse("bytes")
        self.requests += 1
        result = await self.inner.get(url, **{**kw, "max_bytes": min(per_request, headroom)})
        self.bytes += len(getattr(result, "body", b"") or b"")
        return result

    async def aclose(self) -> None:
        await self.inner.aclose()


def metered(http: Any, engine: Engine, reservation: Reservation | None, *, budget: IngestBudget | None = None) -> Any:
    """Wrap a live client in :class:`MeteredHttp`; fixture clients (never charged) are returned as is."""
    if reservation is None or getattr(http, "origin", "live") == "test_fixture":
        return http
    return MeteredHttp(http, engine, reservation, budget=budget)


def settle(engine: Engine, reservation: Reservation, *, requests: int, nbytes: int) -> None:
    """Record what the run consumed (replaces the reservation in the daily sums)."""
    consume(engine, reservation.requests_operation_id, requests)
    consume(engine, reservation.bytes_operation_id, nbytes)


def settle_metered(engine: Engine, http: Any) -> dict[str, Any] | None:
    """Settle a :class:`MeteredHttp` run with its actual usage; returns the budget summary."""
    if not isinstance(http, MeteredHttp):
        return None
    r = http.reservation
    settle(engine, r, requests=http.requests, nbytes=http.bytes)
    return {
        "policy": r.policy_version,
        "source_key": r.source_key,
        "reserved_requests": r.planned_requests,
        "consumed_requests": http.requests,
        "consumed_bytes": http.bytes,
        "refused_requests": http.refused,
    }


# --------------------------------------------------------------------------- shared egress reservation
@dataclass(frozen=True)
class EgressGrant:
    requests_operation_id: str
    bytes_operation_id: str
    requests: int
    bytes: int


def reserve_egress(
    engine: Engine,
    *,
    purpose: str,
    source_key: str,
    operation_id: str,
    want_requests: int,
    want_bytes: int,
    budget: IngestBudget | None = None,
) -> EgressGrant:
    """Reserve up to ``want_requests``/``want_bytes`` for ``purpose`` traffic (all-or-refuse on zero).

    Used by autonomy discovery. Serialised by the same lock as ingestion; refuses
    (nothing recorded) when no request or no byte is left for today.
    """
    b = budget or load_budget()
    per_source = source_limit(b, source_key)
    limits = {
        "source_key": source_key,
        "purpose": purpose,
        "per_source_daily_requests": per_source,
        "daily_requests_total": b.total_requests,
        "policy": b.policy_version,
    }
    if want_requests <= 0:
        raise BudgetExhausted(f"no requests requested for {purpose} on {source_key}", reason="zero_request", **limits)
    req_op, byte_op = f"{operation_id}:requests", f"{operation_id}:bytes"
    with engine.begin() as conn:
        _lock(conn)
        used = usage_today(conn, source_key)
        _, _, remaining, remaining_bytes = _remaining(b, per_source, used)
        if remaining <= 0:
            raise BudgetExhausted(
                f"daily request budget for {purpose} on {source_key} is exhausted "
                f"(limit {per_source}/day, total {b.total_requests}/day)",
                reason="source_limit",
                used_source_requests=used["source_requests"],
                **limits,
            )
        if remaining_bytes <= 0:
            raise BudgetExhausted(f"daily byte budget exhausted; {purpose} not started", reason="byte_limit", **limits)
        n, nb = min(want_requests, remaining), min(max(0, want_bytes), remaining_bytes)
        conn.execute(_INSERT, {"a": account_key(source_key, "requests", purpose), "o": req_op, "u": "requests", "r": n})
        conn.execute(_INSERT, {"a": account_key(source_key, "bytes", purpose), "o": byte_op, "u": "bytes", "r": nb})
    return EgressGrant(req_op, byte_op, n, nb)


# --------------------------------------------------------------------------- generic ledger helpers
# Reusable for any daily budget account:
#   remaining = reserve(engine, account_key="...", operation_id=f"...:{job}:{n}",
#                       unit="requests", amount=planned, daily_limit=limit)
#   ... do the work, at most ``remaining`` units ...
#   consume(engine, operation_id, actually_used)
def used_today(conn: Connection, account_key: str) -> int:
    """Units charged to ``account_key`` today (UTC, DB clock); unsettled rows count their reservation."""
    return int(
        conn.execute(
            text(
                f"SELECT COALESCE(sum(COALESCE(consumed, reserved)), 0) FROM ops.budget_ledger "
                f"WHERE account_key = :a AND recorded_at >= {DAY_START_SQL}"
            ),
            {"a": account_key},
        ).scalar_one()
    )


def _lock_key_for(account: str) -> str:
    """Egress accounts share the egress lock; any other account is serialised on its own key."""
    if any(account.startswith(f"{p}:") for p in EGRESS_PURPOSES.values()):
        return BUDGET_LOCK_KEY
    return f"reva:budget:{account}"


def reserve(engine: Engine, *, account_key: str, operation_id: str, unit: str, amount: int, daily_limit: int) -> int:
    """Atomically reserve ``amount`` units against ``daily_limit`` for ``account_key``.

    Serialised by an advisory transaction lock (the egress lock for egress
    accounts). Raises :class:`BudgetExhausted` (nothing recorded) when
    ``used + amount`` would exceed the limit; otherwise records the reservation
    (idempotent per ``operation_id``) and returns the units that were left before
    it (the caller's hard cap).
    """
    if amount < 0 or daily_limit < 0:
        raise ValueError("amount and daily_limit must be >= 0")
    with engine.begin() as conn:
        _lock(conn, _lock_key_for(account_key))
        used = used_today(conn, account_key)
        if used + amount > daily_limit:
            raise BudgetExhausted(
                f"daily budget for {account_key} would be exceeded ({used} used + {amount} requested > {daily_limit})",
                account_key=account_key,
                used=used,
                requested=amount,
                daily_limit=daily_limit,
                unit=unit,
            )
        conn.execute(_INSERT, {"a": account_key, "o": operation_id, "u": unit, "r": amount})
    return daily_limit - used


def consume(engine: Engine, operation_id: str, amount: int) -> None:
    """Record the units an operation actually consumed (replaces its reservation)."""
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE ops.budget_ledger SET consumed = :c WHERE operation_id = :o"),
            {"c": max(0, int(amount)), "o": operation_id},
        )
