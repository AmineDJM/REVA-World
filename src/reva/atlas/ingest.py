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

    @classmethod
    def offline(cls, spec: AtlasSourceSpec, http: Any) -> AtlasRun:
        """A run that can only fetch (parser tests): no engine/store, so ``archive``/``register`` fail."""
        return cls(
            spec=spec,
            http=http,
            engine=None,  # type: ignore[arg-type]
            store=None,  # type: ignore[arg-type]
            settings=None,
            origin=str(getattr(http, "origin", "test_fixture")),
        )

    def _cap(self, max_bytes: int | None) -> int:
        return min(max_bytes or self.spec.max_response_bytes, self.spec.max_response_bytes)

    def _raw(
        self, r: Any, *, url: str, key: str, media_types: tuple[str, ...], cap: int, params: dict[str, Any] | None,
        fingerprint: str, extra_headers: dict[str, str] | None = None,
    ) -> Any:
        from reva.ports.connectors import FetchItem, RawAcquisition

        item = FetchItem(key=key, url=url, expected_media_types=media_types, max_bytes=cap, params=params or {})
        meta = r.response_metadata()
        if extra_headers:
            meta["headers"] = {**meta.get("headers", {}), **extra_headers}
        return RawAcquisition(
            item=item,
            body=r.body,
            media_type=r.media_type or media_types[0],
            retrieved_at=r.retrieved_at,
            response_metadata=meta,
            request_fingerprint=fingerprint,
            origin=self.origin,  # type: ignore[arg-type]
        )

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
        """GET one item through the run's client and return a ``RawAcquisition`` (not yet archived).

        ``params`` are kept on the acquisition's item (connector bookkeeping, e.g. which variable a
        metadata document describes); they are not sent."""
        from reva.adapters.http.safe_client import SECRET_QUERY_PARAMS, request_fingerprint

        if self.is_cancelled():
            raise PolicyDenied("atlas ingest cancelled")
        cap = self._cap(max_bytes)
        kw: dict[str, Any] = {"max_bytes": cap}
        if headers:
            kw["headers"] = headers
        r = await self.http.get(url, **kw)
        fp = request_fingerprint("GET", url, secret_params=tuple(SECRET_QUERY_PARAMS))
        return self._raw(r, url=url, key=key, media_types=media_types, cap=cap, params=params, fingerprint=fp)

    async def get_range(
        self,
        url: str,
        *,
        key: str,
        start: int | None = None,
        end: int | None = None,
        suffix: int | None = None,
        if_range: str | None = None,
        media_types: tuple[str, ...] = ("application/octet-stream",),
        params: dict[str, Any] | None = None,
    ) -> Any:
        """GET one HTTP byte range (``start``..``end`` inclusive, or the last ``suffix`` bytes).

        Metered like :meth:`get` (one request, the bytes served). Only ``206 Partial Content`` is
        accepted; ``if_range`` (an ETag) makes a changed resource answer 200 with the whole file,
        which is refused (status) or capped (size), so ranges of two versions never mix. The
        acquisition's fingerprint includes the range and its metadata keeps ``content-range``."""
        import hashlib

        from reva.adapters.http.safe_client import redact_url

        if self.is_cancelled():
            raise PolicyDenied("atlas ingest cancelled")
        if suffix is not None:
            if start is not None or end is not None or suffix <= 0:
                raise ValidationProblem("byte range: give either suffix > 0 or start <= end")
            spec, size = f"-{suffix}", suffix
        else:
            if start is None or end is None or start < 0 or end < start:
                raise ValidationProblem("byte range: give either suffix > 0 or start <= end", start=start, end=end)
            spec, size = f"{start}-{end}", end - start + 1
        cap = self._cap(size)
        if cap < size:
            raise ValidationProblem("byte range larger than the source's max_response_bytes", bytes=size, cap=cap)
        headers = {"Range": f"bytes={spec}"}
        if if_range:
            headers["If-Range"] = if_range
        r = await self.http.get(url, headers=headers, max_bytes=cap, accept_status=(206,))
        if r.status != 206:
            raise ValidationProblem("byte range request was not answered with 206", status=r.status)
        fp = hashlib.sha256(f"GET {redact_url(url)} Range: bytes={spec}".encode()).hexdigest()
        return self._raw(
            r,
            url=url,
            key=key,
            media_types=media_types,
            cap=cap,
            params={**(params or {}), "range": spec, **({"if_range": if_range} if if_range else {})},
            fingerprint=fp,
            extra_headers={"content-range": r.headers.get("content-range", "")},
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


class ArchiveRangeFixtureHttp:
    """Fixture client for sources read by HTTP byte ranges of one archive (e.g. WID).

    ``tests/fixtures/source/<name>/manifest.json`` is then an object
    ``{"kind": "byte_range_archive", "url", "file", "sha256", "etag", "retrieved_at"}``: the
    file is served like the provider does (206 + ``Content-Range`` + ``ETag``); a request whose
    ``If-Range`` differs from the ETag is refused like a changed archive. Never charged."""

    origin = "test_fixture"

    def __init__(self, connector_name: str, root: Any = None, etag: str | None = None) -> None:
        import hashlib
        from pathlib import Path

        from reva.evidence.runner import FIXTURE_ROOT

        d = Path(root or FIXTURE_ROOT) / connector_name
        man = json.loads((d / "manifest.json").read_text())
        if not isinstance(man, dict) or man.get("kind") != "byte_range_archive":
            raise ValidationProblem("not a byte-range archive fixture", connector=connector_name)
        self.url, self.etag = str(man["url"]), str(man["etag"])
        self.serve_etag = etag or self.etag
        self.body = (d / man["file"]).read_bytes()
        if hashlib.sha256(self.body).hexdigest() != man["sha256"]:
            raise ValidationProblem("fixture bytes do not match the manifest hash", connector=connector_name)
        self.retrieved_at = datetime.fromisoformat(man.get("retrieved_at", "2026-10-15T00:00:00+00:00"))
        self.requests: list[dict[str, str]] = []

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None, max_bytes: int | None = None, **_: Any
    ) -> Any:
        from reva.adapters.http.safe_client import HttpResult
        from reva.kernel.errors import SourceResponseError

        h = dict(headers or {})
        self.requests.append(h)
        if url != self.url:
            raise SourceResponseError("no captured fixture for this URL", retryable=False, kind="fixture_missing")
        if "If-Range" in h and h["If-Range"] != self.serve_etag:
            raise SourceResponseError("HTTP 200 (If-Range mismatch: whole file) refused", retryable=False)
        spec = h.get("Range", "").removeprefix("bytes=")
        n = len(self.body)
        if not spec:
            raise SourceResponseError("byte-range fixture requires a Range header", retryable=False)
        if spec.startswith("-"):
            a, b = max(0, n - int(spec[1:])), n - 1
        else:
            a, b = (int(x) for x in spec.split("-"))
            b = min(b, n - 1)
        chunk = self.body[a : b + 1]
        if max_bytes is not None and len(chunk) > max_bytes:
            raise SourceResponseError("fixture range larger than max_bytes", retryable=False)
        return HttpResult(
            url,
            url,
            206,
            {"content-range": f"bytes {a}-{b}/{n}", "etag": self.serve_etag, "content-type": "application/zip"},
            chunk,
            self.retrieved_at,
            0.0,
            (),
            1,
        )

    async def aclose(self) -> None:
        return None


def fixture_http(connector_name: str) -> Any:
    """The fixture client of an atlas source: captured responses, or a byte-range archive."""
    from reva.evidence.runner import FIXTURE_ROOT, FixtureHttp

    man = FIXTURE_ROOT / connector_name / "manifest.json"
    if man.exists():
        doc = json.loads(man.read_text())
        if isinstance(doc, dict) and doc.get("kind") == "byte_range_archive":
            return ArchiveRangeFixtureHttp(connector_name)
    return FixtureHttp(connector_name)


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
        http: Any = fixture_http(spec.name)
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
