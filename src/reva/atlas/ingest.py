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
    options: dict[str, Any] = field(default_factory=dict)  # source-specific run options (validated, small)
    is_cancelled: Callable[[], bool] = lambda: False
    archived: list[str] = field(default_factory=list)
    registered: list[dict[str, Any]] = field(default_factory=list)

    def wants(self, dataset: str) -> bool:
        if dataset not in self.spec.datasets:
            raise ValidationProblem("dataset not declared by the atlas source", dataset=dataset, source=self.spec.name)
        return self.dataset is None or self.dataset == dataset

    @classmethod
    def offline(cls, spec: AtlasSourceSpec, http: Any, origin: str | None = None) -> AtlasRun:
        """A run that can only fetch (parser tests): no engine/store, so ``archive``/``register`` fail."""
        return cls(
            spec=spec,
            http=http,
            engine=None,  # type: ignore[arg-type]
            store=None,  # type: ignore[arg-type]
            settings=None,
            origin=origin or str(getattr(http, "origin", "test_fixture")),
        )

    def _cap(self, max_bytes: int | None) -> int:
        return min(max_bytes or self.spec.max_response_bytes, self.spec.max_response_bytes)

    def _raw(
        self,
        r: Any,
        *,
        url: str,
        key: str,
        media_types: tuple[str, ...],
        cap: int,
        params: dict[str, Any] | None,
        fingerprint: str,
        extra_headers: dict[str, str] | None = None,
        request: dict[str, Any] | None = None,
    ) -> Any:
        from reva.ports.connectors import FetchItem, RawAcquisition

        item = FetchItem(key=key, url=url, expected_media_types=media_types, max_bytes=cap, params=params or {})
        meta = r.response_metadata()
        if extra_headers:
            meta["headers"] = {**meta.get("headers", {}), **extra_headers}
        if request:
            meta["request"] = request  # POST: the request body is archived with the response
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

    async def post(
        self,
        url: str,
        *,
        key: str,
        json_body: Any = None,
        content: bytes | None = None,
        content_type: str | None = None,
        media_types: tuple[str, ...] = ("application/json",),
        headers: dict[str, str] | None = None,
        max_bytes: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """POST one query (PxWeb and other POST-only APIs) and return a ``RawAcquisition``.

        Same guarantees as :meth:`get`: the source's allow-listed hosts, politeness and size caps
        (``reva.atlas.http_post.safe_post`` through the run's SafeHttpClient, never a new client),
        metered on the run's budget reservation exactly like a GET (one request; response and request
        bytes counted). Fixture mode replays a capture keyed by URL + request-body sha256. The request
        body (<= 64 KiB; canonical JSON for ``json_body``) is archived with the response in
        ``response_metadata.request`` (method, content type, body text, sha256), and the acquisition's
        fingerprint covers it, so two different queries to one URL are two acquisitions."""
        import base64
        import hashlib

        from reva.adapters.http.safe_client import redact_url

        if self.is_cancelled():
            raise PolicyDenied("atlas ingest cancelled")
        if (json_body is None) == (content is None):
            raise ValidationProblem("POST needs exactly one of json_body or content")
        body = (
            json.dumps(json_body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            if json_body is not None
            else bytes(content or b"")
        )
        if len(body) > POST_BODY_MAX:
            raise ValidationProblem("POST body too large to archive with the response", bytes=len(body))
        ctype = content_type or ("application/json" if json_body is not None else "application/octet-stream")
        send = {"Content-Type": ctype, **(headers or {})}
        cap = self._cap(max_bytes)
        body_sha = hashlib.sha256(body).hexdigest()
        fp = hashlib.sha256(f"POST {redact_url(url)} body:{body_sha}".encode()).hexdigest()
        r = await self._post(url, body, send, cap, fp)
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        request = {
            "method": "POST",
            "content_type": ctype,
            "body_sha256": body_sha,
            "body_bytes": len(body),
            "body": text,
            "body_base64": None if text is not None else base64.b64encode(body).decode(),
        }
        return self._raw(
            r,
            url=url,
            key=key,
            media_types=media_types,
            cap=cap,
            params={**(params or {}), "method": "POST", "request_body_sha256": body_sha},
            fingerprint=fp,
            request=request,
        )

    async def _post(self, url: str, body: bytes, headers: dict[str, str], cap: int, fp: str) -> Any:
        import asyncio as _asyncio

        from reva.atlas.http_post import safe_post
        from reva.operations.budgets import MeteredHttp, top_up

        http = self.http
        if isinstance(http, MeteredHttp):  # live: the same ledger accounting as MeteredHttp.get
            if http.requests + 1 > http.reserved_requests:
                granted = await _asyncio.to_thread(
                    top_up, http.engine, http.reservation, unit="requests", want=http.request_chunk, budget=http.budget
                )
                http.reserved_requests += granted
                if http.requests + 1 > http.reserved_requests:
                    raise http._refuse("requests")
            need = cap + len(body)
            headroom = http.reserved_bytes - http.bytes
            if headroom < need:
                granted = await _asyncio.to_thread(
                    top_up, http.engine, http.reservation, unit="bytes", want=need - headroom, budget=http.budget
                )
                http.reserved_bytes += granted
                headroom += granted
                if headroom <= len(body):
                    raise http._refuse("bytes")
            http.requests += 1
            http.bytes += len(body)  # request bytes count against the egress budget too
            r = await safe_post(
                http.inner, url, content=body, headers=headers, max_bytes=min(cap, headroom - len(body))
            )
            http.bytes += len(r.body)
            return r
        entries = getattr(http, "entries", None)
        if getattr(http, "origin", "") == "test_fixture" and isinstance(entries, dict):
            return _fixture_post(http, url, fp)  # FixtureHttp capture keyed by URL + body sha256
        poster = getattr(http, "post", None)
        if poster is not None and getattr(http, "origin", "") == "test_fixture":
            return await poster(url, content=body, headers=headers, max_bytes=cap)
        if self.engine is None:  # AtlasRun.offline with a live client (live layout tests): nothing archived
            return await safe_post(http, url, content=body, headers=headers, max_bytes=cap)
        raise PolicyDenied("live atlas POST requires the metered client of run_atlas_ingest")

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


POST_BODY_MAX = 65_536  # a POST query is archived in full with its response


def _fixture_post(http: Any, url: str, fp: str) -> Any:
    """A captured POST response from a FixtureHttp manifest (entry ``request_fingerprint`` = the POST
    fingerprint of ``AtlasRun.post``: sha256 of ``POST <url> body:<body sha256>``)."""
    import hashlib

    from reva.adapters.http.safe_client import HttpResult
    from reva.kernel.errors import SourceResponseError

    e = http.entries.get(fp)
    if e is None:
        raise SourceResponseError("no captured fixture for this POST query", retryable=False, kind="fixture_missing")
    body = (http.dir / e["file"]).read_bytes()
    if hashlib.sha256(body).hexdigest() != e["sha256"]:
        raise SourceResponseError("fixture bytes do not match manifest hash", retryable=False)
    return HttpResult(
        url,
        url,
        int(e.get("status", 200)),
        dict(e.get("headers") or {}),
        body,
        datetime.fromisoformat(e.get("retrieved_at", "2026-10-16T00:00:00+00:00")),
        0.0,
        (),
        1,
    )


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


#: Non-standard ``Content-Encoding`` labels some atlas providers send for an unencoded body
#: (www.peaceagreements.org sends ``none``). Read as ``identity``: no decoding, same byte bound.
#: This lives here, not in ``safe_client.py``: that file is in the digest-pinned autonomy template closure
#: (``registry/policies/promotion.yaml`` ``template_sha256``), so the safe client itself still refuses ``none``.
IDENTITY_CODING_ALIASES = frozenset({"none"})
#: Header recording the label the provider actually sent, kept with the archived response headers.
ORIGINAL_CODING_HEADER = "x-reva-original-content-encoding"


async def _identity_coding_alias(response: Any) -> None:
    """httpx response hook: a lone alias label in ``IDENTITY_CODING_ALIASES`` becomes ``identity``."""
    values = response.headers.get_list("content-encoding")
    codings = [c.strip().lower() for v in values for c in v.split(",") if c.strip()]
    if len(codings) == 1 and codings[0] in IDENTITY_CODING_ALIASES:
        response.headers[ORIGINAL_CODING_HEADER] = codings[0]
        response.headers["content-encoding"] = "identity"


def _live_client(spec: AtlasSourceSpec, settings: Any) -> Any:
    from reva.adapters.http.rate_limit import HostPolicy, RateLimiter
    from reva.adapters.http.safe_client import SafeHttpClient

    policy = HostPolicy(min_interval_seconds=spec.min_interval_seconds, max_concurrency=1)
    client = SafeHttpClient(
        allowed_hosts=spec.allowed_hosts,
        timeout_seconds=max(float(settings.reva_http_timeout_seconds), 120.0),  # bulk files
        max_bytes=spec.max_response_bytes,
        contact_email=settings.reva_http_contact_email,
        rate_limiter=RateLimiter({h: policy for h in spec.allowed_hosts}, default=policy),
        proxy_mode=settings.reva_http_proxy_mode,
    )
    client._client.event_hooks = {"request": [], "response": [_identity_coding_alias]}
    return client


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
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one atlas source (see module doc). ``http_factory`` replaces the live client (tests).

    ``options`` are source-specific run options (e.g. WID ``countries``), JSON scalars or lists
    of strings, recorded in the result. The source's connector version must be registered with
    the code it runs (``code_digest``): changed code under the same version is refused before any
    request, so archived acquisitions always name the code that produced them."""
    from reva.operations.budgets import BudgetExhausted, load_budget, metered, reserve_ingest, settle_metered

    entry = get_source(source)
    spec = entry.spec
    if dataset is not None and dataset not in spec.datasets:
        raise ValidationProblem("dataset not declared by the atlas source", dataset=dataset, known=list(spec.datasets))
    if mode not in ("live", "fixture"):
        raise ValidationProblem("atlas ingest mode must be live or fixture", mode=str(mode)[:32])
    if mode == "fixture" and getattr(settings, "reva_env", "") == "production":
        raise PolicyDenied("fixture ingestion is not allowed in production")
    opts = _check_options(options)
    _check_registered(engine, spec)
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
        options=opts,
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
        "options": opts,
        "status": status,
        "error": error,
        "started_at": started.isoformat(),
        "archived_raw": len(run.archived),
        "registered": run.registered,
        "summary": summary,
        "budget": usage,
    }


def _check_options(options: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (options or {}).items():
        if not isinstance(k, str) or not k.isidentifier() or len(k) > 40:
            raise ValidationProblem("invalid atlas run option name", option=str(k)[:40])
        ok = isinstance(v, (str, int, float, bool)) or (
            isinstance(v, list) and len(v) <= 1000 and all(isinstance(x, str) and len(x) <= 64 for x in v)
        )
        if not ok or (isinstance(v, str) and len(v) > 4000):
            raise ValidationProblem("atlas run option values are scalars or short string lists", option=k)
        out[k] = v
    return out


def _check_registered(engine: Engine, spec: AtlasSourceSpec) -> None:
    from reva.adapters.postgres import repositories as repo

    with engine.connect() as conn:
        row = repo.connector_version_row(conn, spec.name, spec.version)
    if row is None:
        raise ValidationProblem(
            f"atlas connector {spec.name}@{spec.version} is not registered (seed or ensure_registered)",
            connector=spec.name,
            version=spec.version,
        )
    if (row.get("code_digest") or "") != (spec.code_digest or ""):
        raise ValidationProblem(
            f"atlas connector {spec.name}@{spec.version} is registered with different code; bump the version",
            connector=spec.name,
            version=spec.version,
        )


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
            options=payload.get("options") or None,
        )
    )


def parse_options(items: list[str] | None) -> dict[str, Any]:
    """``["countries=FR,DE", "x=1"]`` -> ``{"countries": "FR,DE", "x": "1"}`` (CLI ``--option``)."""
    out: dict[str, Any] = {}
    for it in items or []:
        k, sep, v = it.partition("=")
        if not sep or not k.strip():
            raise ValidationProblem("run option must be KEY=VALUE", option=it[:80])
        out[k.strip()] = v.strip()
    return out


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
    ap.add_argument("--option", action="append", default=[], metavar="KEY=VALUE", help="source run option")
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
                options=parse_options(args.option),
            )
        )
    except BudgetExhausted as e:
        print(json.dumps(e.problem(), indent=1, default=str))
        raise SystemExit(2) from e
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":  # pragma: no cover
    main()
