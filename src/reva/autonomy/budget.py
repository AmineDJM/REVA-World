"""Daily request/byte budgets for autonomy discovery (review R4-08, invariant 30).

``registry/policies/budgets.yaml`` (digest-pinned) is enforced through the shared
``ops.budget_ledger`` by :func:`reva.operations.budgets.reserve_egress`, on the
canonical accounts (:func:`reva.operations.budgets.account_key`)

* ``autonomy:discovery:{source_key}:requests``
* ``autonomy:discovery:{source_key}:bytes``

The per-source limit (``per_source_daily_requests``) and the daily totals
(``daily.http_requests_total`` / ``http_bytes_total``) are shared with ingestion:
what every egress account (ingestion ``source:{source_key}:*`` and autonomy)
already used today is subtracted before reserving, and ingestion and discovery
reservations are serialised by the *same* advisory lock, so together they can
never exceed a shared limit. A limit of ``0`` means no request at all, and a
source without a per-source limit is refused (fail closed). Each job reserves
before its first request (its hard caps are the reserved amounts) and records
what it consumed after the run; an unsettled reservation keeps counting (fail
closed). Fixture replays make no network requests and are not charged.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine

from reva.operations.budgets import BudgetExhausted, MeteredHttp, Reservation, consume, load_budget, reserve_egress

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


def reserve_discovery(engine: Engine, source_key: str, *, job_key: str | None = None,
                      wanted_requests: int = DEFAULT_MAX_REQUESTS_PER_JOB, wanted_bytes: int = 8_000_000,
                      policy: Any = None) -> DiscoveryReservation:
    """Reserve today's discovery allowance or raise ``BudgetExhausted`` (nothing is fetched)."""
    g = reserve_egress(engine, purpose="discovery", source_key=source_key,
                       operation_id=f"discovery:{job_key or uuid.uuid4()}", want_requests=wanted_requests,
                       want_bytes=wanted_bytes, budget=load_budget(policy))
    return DiscoveryReservation(source_key, g.requests_operation_id, g.bytes_operation_id, g.requests, g.bytes)


def settle_discovery(engine: Engine, r: DiscoveryReservation, *, requests: int, nbytes: int) -> None:
    consume(engine, r.requests_operation_id, requests)
    consume(engine, r.bytes_operation_id, nbytes)


class DiscoveryMeteredHttp(MeteredHttp):
    """One shared meter for a whole live discovery run (R6-09).

    The catalogue search, the inspection *and* the validator's live sample all go
    through this one client, so every provider GET is counted once and settled, and
    the run as a whole is hard-capped at its reservation (no in-run top-up): it can
    never make more requests or read more bytes than the ledger reserved.
    """

    def __init__(self, inner: Any, engine: Engine, r: DiscoveryReservation) -> None:
        res = Reservation(r.source_key, r.requests_operation_id, r.bytes_operation_id, r.max_requests,
                          r.max_requests, r.max_bytes, "budgets")
        super().__init__(inner, engine, res, request_chunk=1)
        self.reserved_bytes = r.max_bytes
        self.hard_requests = r.max_requests
        self.hard_bytes = r.max_bytes

    async def get(self, url: str, **kw: Any) -> Any:
        if self.requests >= self.hard_requests:
            raise self._refuse("requests")
        remaining = self.hard_bytes - self.bytes
        if remaining <= 0:
            raise self._refuse("bytes")
        cap = int(kw.get("max_bytes") or remaining)
        return await super().get(url, **{**kw, "max_bytes": min(cap, remaining)})


def metered_discovery(http: Any, engine: Engine, reservation: DiscoveryReservation | None) -> Any:
    """Wrap a live discovery client in the shared meter; fixture replays are returned unchanged."""
    if reservation is None or getattr(http, "origin", "live") == "test_fixture":
        return http
    return DiscoveryMeteredHttp(http, engine, reservation)


__all__ = ["BudgetExhausted", "DiscoveryMeteredHttp", "DiscoveryReservation", "metered_discovery",
           "reserve_discovery", "settle_discovery"]
