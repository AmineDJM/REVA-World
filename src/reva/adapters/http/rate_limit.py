"""Provider-aware request pacing: per-host minimum interval and concurrency."""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field


@dataclass
class HostPolicy:
    min_interval_seconds: float = 1.0
    max_concurrency: int = 1
    max_retry_after_seconds: float = 120.0


@dataclass
class _HostState:
    sem: asyncio.Semaphore
    last: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class RateLimiter:
    def __init__(self, policies: dict[str, HostPolicy] | None = None, default: HostPolicy | None = None):
        self.policies = policies or {}
        self.default = default or HostPolicy()
        self._state: dict[str, _HostState] = {}

    def policy(self, host: str) -> HostPolicy:
        return self.policies.get(host, self.default)

    def _st(self, host: str) -> _HostState:
        st = self._state.get(host)
        if st is None:
            st = _HostState(sem=asyncio.Semaphore(self.policy(host).max_concurrency))
            self._state[host] = st
        return st

    async def acquire(self, host: str) -> None:
        st = self._st(host)
        await st.sem.acquire()
        async with st.lock:
            wait = st.last + self.policy(host).min_interval_seconds - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            st.last = time.monotonic()

    def release(self, host: str) -> None:
        self._st(host).sem.release()

    def adapt_from_headers(self, host: str, headers: dict[str, str]) -> None:
        """Honour provider concurrency/interval hints (e.g. Crossref X-Rate-Limit-*)."""
        limit = headers.get("x-rate-limit-limit")
        interval = headers.get("x-rate-limit-interval")
        if limit and interval and interval.endswith("s"):
            try:
                per = float(interval[:-1]) / max(1, int(limit))
                pol = self.policy(host)
                self.policies[host] = HostPolicy(
                    max(pol.min_interval_seconds, per), pol.max_concurrency, pol.max_retry_after_seconds
                )
            except ValueError:
                pass
        conc = headers.get("x-concurrency-limit")
        if conc and conc.isdigit():
            pol = self.policy(host)
            self.policies[host] = HostPolicy(
                pol.min_interval_seconds, max(1, min(pol.max_concurrency, int(conc))), pol.max_retry_after_seconds
            )


def backoff_seconds(attempt: int, base: float = 1.0, cap: float = 60.0) -> float:
    """Exponential backoff with full jitter."""
    return random.uniform(0, min(cap, base * (2**attempt)))
