"""The single atlas ingest entry point: any atlas source, budget-metered (invariant 30).

``run_atlas_ingest(settings, engine, store, source, dataset=None, mode="live"|"fixture")``:

1. resolves the source in the atlas registry (:mod:`reva.atlas.sources`);
2. **live**: reserves ``spec.planned_requests`` on the source's budget key before any request
   (``reserve_ingest``; a run that does not fit is refused with ``BudgetExhausted`` and
   nothing is fetched), builds the SafeHttpClient restricted to ``spec.allowed_hosts`` with
   the spec's per-host politeness, and wraps it in the ledger meter (``metered``: each request
   is covered by the reservation or topped up under the egress lock, else refused before it is
   sent; response bodies are capped to the reserved bytes); **fixture**: replays
   ``tests/fixtures/source/<name>/manifest.json`` (``FixtureHttp``, never charged, refused in
   production);
3. hands the module's ``atlas_ingest(run)`` an :class:`AtlasRun` (the metered client, archive
   and register helpers) — atlas modules never build an HTTP client themselves;
4. settles the ledger with the actual usage (``settle_metered``) whatever happened, and returns
   the module's summary plus the budget summary.

Entry points: ``uv run python -m reva.atlas.ingest <source> [--dataset D] [--fixture]``
(worker credentials), the worker job handler :func:`handle_atlas_ingest` (payload
``{"source": name, "dataset": optional, "mode": "live"|"fixture"}``) and :func:`list_sources`.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import polars as pl
from sqlalchemy import Engine

from reva.atlas.sources import AtlasSourceSpec, get_source
from reva.kernel.errors import PolicyDenied, ValidationProblem
from reva.ports.storage import ArtifactStore

ACTOR = "worker:atlas_ingest"


@dataclass
class AtlasRun:
    """What an atlas module's ``atlas_ingest(run)`` gets. Fetch only through ``http``."""

    spec: AtlasSourceSpec
    http: Any  # MeteredHttp(SafeHttpClient) live, FixtureHttp offline: ``await http.get(url, headers=..., max_bytes=...)``
    engine: Engine  # worker role
    store: ArtifactStore
    settings: Any
    origin: str  # "live" | "test_fixture"
    dataset: str | None = None  # restrict the run to one atlas dataset (None: all)
    actor: str = ACTOR
    is_cancelled: Callable[[], bool] = lambda: False
    archived: list[str] = field(default_factory=list)
    registered: list[dict[str, Any]] = field(default_factory=list)

    def wants(self, dataset: str) -> bool:
        if dataset not in self.spec.datasets:
            raise ValidationProblem("dataset not declared by the atlas source", dataset=dataset, source=self.spec.name)
        return self.dataset is None or self.dataset == dataset

    async def get(
        self,
        url: str,
        *,
        key: str,
        media_types: tuple[str, ...] = ("application/json",),
        headers: dict[str, str] | None = None,
        max_bytes: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """GET one item through the run's client and return a ``RawAcquisition`` (not yet archived)."""
        from reva.adapters.http.safe_client import request_fingerprint
        from reva.ports.connectors import FetchItem, RawAcquisition

        if self.is_cancelled():
            raise PolicyDenied("atlas ingest cancelled")
        cap = min(max_bytes or self.spec.max_response_bytes, self.spec.max_response_bytes)
        item = FetchItem(key=key, url=url, expected_media_types=media_types, max_bytes=cap, params=params or {})
        kw: dict[str, Any] = {"max_bytes": cap}
        if headers:
            kw["headers"] = headers
        r = await self.http.get(url, **kw)
        return RawAcquisition(
            item=item,
            body=r.body,
            media_type=r.media_type or media_types[0],
            retrieved_at=r.retrieved_at,
            response_metadata=r.response_metadata(),
            request_fingerprint=request_fingerprint("GET", url),
            origin=self.origin,  # type: ignore[arg-type]
        )

    def archive(self, raw: Any) -> str:
        """Archive raw bytes as an ``evidence.acquisition`` of this source's connector version."""
        from reva.evidence.ingest import publish_acquisition

        if raw.origin != self.origin:
            raise ValidationProblem("raw acquisition origin differs from the run origin", origin=raw.origin)
        summary = publish_acquisition(self.engine, self.store, self.spec, raw, [], [], actor=self.actor)  # type: ignore[arg-type]
        if summary.namespace == "quarantine":
            raise ValidationProblem("raw acquisition was quarantined", raw_sha256=summary.raw_sha256)
        self.archived.append(summary.raw_sha256)
        return str(summary.raw_sha256)

    def register(
        self,
        *,
        dataset: str,
        frame: pl.DataFrame,
        raw_sha256s: list[str],
        coverage_note: str | None = None,
        geo_scheme: str | None = None,
        rights: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``register_atlas_table`` with this source's atlas key, parser version and origin."""
        from reva.atlas.tables import register_atlas_table

        self.wants(dataset)
        out = register_atlas_table(
            self.engine,
            self.store,
            source=self.spec.atlas_source,
            dataset=dataset,
            frame=frame,
            raw_sha256s=raw_sha256s,
            parser_version=self.spec.parser_version,
            origin=self.origin,
            coverage_note=coverage_note,
            geo_scheme=geo_scheme,
            rights=rights
            or {"licence": self.spec.licence, "provider": self.spec.provider, "source_key": self.spec.source_key},
            actor=self.actor,
        )
        self.registered.append({k: out[k] for k in ("dataset_key", "dataset_id", "row_count", "created")})
        return out


def _live_client(spec: AtlasSourceSpec, settings: Any) -> Any:
    from reva.adapters.http.rate_limit import HostPolicy, RateLimiter
    from reva.adapters.http.safe_client import SafeHttpClient

    policy = HostPolicy(min_interval_seconds=spec.min_interval_seconds, max_concurrency=1)
    return SafeHttpClient(
        allowed_hosts=spec.allowed_hosts,
        timeout_seconds=max(float(settings.reva_http_timeout_seconds), 120.0),  # bulk files
        max_bytes=spec.max_response_bytes,
        contact_email=settings.reva_http_contact_email,
        rate_limiter=RateLimiter({h: policy for h in spec.allowed_hosts}, default=policy),
        proxy_mode=settings.reva_http_proxy_mode,
    )


async def run_atlas_ingest(
    settings: Any,
    engine: Engine,
    store: ArtifactStore,
    source: str,
    *,
    dataset: str | None = None,
    mode: str = "live",
    job_id: uuid.UUID | str | None = None,
    attempt: int = 1,
    http_factory: Callable[[AtlasSourceSpec, Any], Any] | None = None,
    is_cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, Any]:
    """Run one atlas source (see module doc). ``http_factory`` replaces the live client (tests)."""
    from reva.evidence.runner import FixtureHttp
    from reva.operations.budgets import BudgetExhausted, load_budget, metered, reserve_ingest, settle_metered

    entry = get_source(source)
    spec = entry.spec
    if dataset is not None and dataset not in spec.datasets:
        raise ValidationProblem("dataset not declared by the atlas source", dataset=dataset, known=list(spec.datasets))
    if mode not in ("live", "fixture"):
        raise ValidationProblem("atlas ingest mode must be live or fixture", mode=str(mode)[:32])
    if mode == "fixture" and getattr(settings, "reva_env", "") == "production":
        raise PolicyDenied("fixture ingestion is not allowed in production")
    import importlib

    module = importlib.import_module(entry.module)
    started = datetime.now(UTC)
    reservation = None
    if mode == "fixture":
        http: Any = FixtureHttp(spec.name)
    else:
        budget = load_budget()
        # Invariant 30: reserve before the first request; a run that does not fit is not started.
        reservation = reserve_ingest(
            engine,
            job_id=job_id or uuid.uuid4(),
            attempt=attempt,
            source_key=spec.budget_key,
            planned_requests=spec.planned_requests,
            budget=budget,
        )
        inner = (http_factory or _live_client)(spec, settings)
        http = metered(inner, engine, reservation, budget=budget)
    run = AtlasRun(
        spec=spec,
        http=http,
        engine=engine,
        store=store,
        settings=settings,
        origin="test_fixture" if mode == "fixture" else "live",
        dataset=dataset,
        is_cancelled=is_cancelled,
    )
    status, error, summary = "completed", None, None
    try:
        summary = await module.atlas_ingest(run)
    except BudgetExhausted as e:  # partial run: what was archived/registered stays, typed outcome
        status, error = "budget_exhausted", e.problem()
    finally:
        usage = settle_metered(engine, http) if reservation is not None else None
        close = getattr(http, "aclose", None)
        if close is not None:
            await close()
    return {
        "source": spec.name,
        "source_key": spec.source_key,
        "budget_key": spec.budget_key,
        "mode": mode,
        "dataset": dataset,
        "status": status,
        "error": error,
        "started_at": started.isoformat(),
        "archived_raw": len(run.archived),
        "registered": run.registered,
        "summary": summary,
        "budget": usage,
    }


def handle_atlas_ingest(payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Worker job handler (requested job kind ``atlas_ingest``; see docs/dev/v3_atlascore.md)."""
    return asyncio.run(
        run_atlas_ingest(
            ctx.settings,
            ctx.worker_engine,
            ctx.store,
            str(payload.get("source", "")),
            dataset=payload.get("dataset"),
            mode=str(payload.get("mode", "live")),
            job_id=ctx.job_id,
            attempt=ctx.attempt,
            is_cancelled=ctx.cancel_requested,
        )
    )


def list_sources() -> dict[str, Any]:
    from reva.atlas.sources import atlas_source_errors, atlas_sources

    return {
        "sources": {
            k: {**e.spec.model_dump(mode="json", exclude={"field_mappings"}), "module": e.module}
            for k, e in atlas_sources().items()
        },
        "errors": atlas_source_errors(),
    }


def main(argv: list[str] | None = None) -> None:  # pragma: no cover - operator entry point
    import argparse

    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres.engine import make_engine
    from reva.operations.budgets import BudgetExhausted
    from reva.settings import get_settings

    ap = argparse.ArgumentParser(description="Run one atlas source (budget-metered).")
    ap.add_argument("source", nargs="?", help="atlas source name (omit with --list)")
    ap.add_argument("--dataset")
    ap.add_argument("--fixture", action="store_true", help="replay recorded fixtures (test_fixture origin)")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args(argv)
    if args.list or not args.source:
        print(json.dumps(list_sources(), indent=1, default=str))
        return
    settings = get_settings()
    try:
        out = asyncio.run(
            run_atlas_ingest(
                settings,
                make_engine("worker", settings),
                get_store(),
                args.source,
                dataset=args.dataset,
                mode="fixture" if args.fixture else "live",
            )
        )
    except BudgetExhausted as e:
        print(json.dumps(e.problem(), indent=1, default=str))
        raise SystemExit(2) from e
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":  # pragma: no cover
    main()
