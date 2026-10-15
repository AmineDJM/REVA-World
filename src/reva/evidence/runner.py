"""Run a connector end to end: plan -> fetch -> parse -> validate -> publish.

The cursor advances only after durable publication of all required items.
Live runs may record captured fixtures (URL, hash, retrieval time, licence).
Fixture replays carry origin=test_fixture and can never enter a live release.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import Engine, text

from reva.adapters.http.safe_client import (
    SECRET_QUERY_PARAMS,
    HttpResult,
    SafeHttpClient,
    redact_url,
    request_fingerprint,
)
from reva.evidence.ingest import publish_acquisition
from reva.kernel.errors import RevaError, SourceResponseError
from reva.kernel.ids import sha256_bytes
from reva.ports.connectors import Cursor, FetchBudget, FetchOutcome, Finding, QualityReport
from reva.ports.storage import ArtifactStore

log = logging.getLogger("reva.runner")
FIXTURE_ROOT = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "source"


def _fixture_fingerprint(url: str) -> str:
    """Fixture key: secret query parameters are excluded, so captures never depend on a key.
    (Identical to the plain fingerprint for URLs without secret parameters.)"""
    return request_fingerprint("GET", url, secret_params=tuple(SECRET_QUERY_PARAMS))


class FixtureHttp:
    """Serves captured provider responses; marks everything test_fixture."""

    origin = "test_fixture"

    def __init__(self, connector_name: str, root: Path = FIXTURE_ROOT, overrides: dict[str, bytes] | None = None):
        self.dir = root / connector_name
        manifest = self.dir / "manifest.json"
        self.entries: dict[str, dict[str, Any]] = {}
        if manifest.exists():
            for e in json.loads(manifest.read_text()):
                self.entries[e["request_fingerprint"]] = e
        self.overrides = overrides or {}

    async def get(self, url: str, **kw: Any) -> HttpResult:
        fp = _fixture_fingerprint(url)
        if fp in self.overrides:
            body = self.overrides[fp]
            status, headers = 200, {"content-type": "application/json"}
        else:
            e = self.entries.get(fp)
            if e is None:
                raise SourceResponseError(f"no captured fixture for {url}", retryable=False, kind="fixture_missing")
            body = (self.dir / e["file"]).read_bytes()
            if sha256_bytes(body) != e["sha256"]:
                raise SourceResponseError("fixture bytes do not match manifest hash", retryable=False)
            status, headers = e["status"], e["headers"]
        return HttpResult(url, url, status, dict(headers), body, datetime.fromisoformat(
            self.entries.get(fp, {}).get("retrieved_at", "2026-10-14T00:00:00+00:00")), 0.0, (), 1)

    async def aclose(self) -> None:
        return None


class RecordingHttp:
    """Live client that captures responses as fixtures with rights metadata."""

    origin = "live"

    def __init__(self, client: SafeHttpClient, connector_name: str, licence: str, root: Path = FIXTURE_ROOT):
        self.client = client
        self.dir = root / connector_name
        self.licence = licence
        self.captured: list[dict[str, Any]] = []

    async def get(self, url: str, **kw: Any) -> HttpResult:
        r = await self.client.get(url, **kw)
        sha = sha256_bytes(r.body)
        ext = ".zip" if r.media_type in ("application/zip", "application/octet-stream") else ".json"
        self.dir.mkdir(parents=True, exist_ok=True)
        fp = _fixture_fingerprint(url)
        fname = f"{fp[:16]}{ext}"
        (self.dir / fname).write_bytes(r.body)
        self.captured.append(
            {
                "request_fingerprint": fp,
                "url": redact_url(url),  # secret query values (subscription-key, token, ...) never persisted
                "file": fname,
                "sha256": sha,
                "status": r.status,
                "headers": {k: v for k, v in r.headers.items() if k in ("content-type", "etag", "last-modified")},
                "retrieved_at": r.retrieved_at.isoformat(),
                "licence": self.licence,
            }
        )
        return r

    def write_manifest(self) -> None:
        if not self.captured:
            return
        path = self.dir / "manifest.json"
        existing = json.loads(path.read_text()) if path.exists() else []
        merged = {e["request_fingerprint"]: e for e in existing}
        merged.update({e["request_fingerprint"]: e for e in self.captured})
        path.write_text(json.dumps(sorted(merged.values(), key=lambda e: e["url"]), indent=2))

    async def aclose(self) -> None:
        await self.client.aclose()


@dataclass
class RunReport:
    connector: str
    outcomes: list[FetchOutcome] = field(default_factory=list)
    quality: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    inserted: int = 0
    unchanged: int = 0
    relations: int = 0
    cursor: dict[str, Any] = field(default_factory=dict)
    cursor_advanced: bool = False
    errors: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "connector": self.connector,
            "outcomes": [o.model_dump() for o in self.outcomes],
            "quality": self.quality,
            "inserted": self.inserted,
            "unchanged": self.unchanged,
            "relations": self.relations,
            "cursor": self.cursor,
            "cursor_advanced": self.cursor_advanced,
            "errors": self.errors,
        }


def load_cursor(engine: Engine, name: str) -> Cursor | None:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT cursor FROM ops.connector_cursor WHERE connector_name=:n"), {"n": name}
        ).scalar_one_or_none()
    return Cursor(state=row) if row else None


def save_cursor(engine: Engine, name: str, cursor: Cursor) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO ops.connector_cursor(connector_name, cursor) VALUES (:n, CAST(:c AS jsonb))
                   ON CONFLICT (connector_name)
                   DO UPDATE SET cursor = EXCLUDED.cursor, updated_at = clock_timestamp()"""
            ),
            {"n": name, "c": json.dumps(cursor.state)},
        )


def record_health(engine: Engine, spec_name: str, spec_version: str, report: RunReport, origin: str = "live") -> None:
    ok = sum(1 for o in report.outcomes if o.status in ("published", "unchanged"))
    total = max(1, len(report.outcomes))
    quarantined = sum(1 for o in report.outcomes if o.status == "quarantined")
    status = "healthy" if ok == total else ("quarantined" if quarantined else "degraded")
    with engine.begin() as conn:
        cv = conn.execute(
            text("SELECT id FROM evidence.connector_version WHERE name=:n AND version=:v"),
            {"n": spec_name, "v": spec_version},
        ).scalar_one_or_none()
        if cv is None:
            return
        conn.execute(
            text(
                "INSERT INTO ops.connector_health(connector_version_id, metrics, status) "
                "VALUES (:cv, CAST(:m AS jsonb), :s)"
            ),
            {
                "cv": cv,
                "m": json.dumps(
                    {
                        "origin": origin,
                        "request_success_ratio": ok / total,
                        "items": total,
                        "quarantined": quarantined,
                        "inserted": report.inserted,
                        "unchanged": report.unchanged,
                        "errors": report.errors[:10],
                    }
                ),
                "s": status,
            },
        )


def _bind_post_publish(post_publish: Any, store: ArtifactStore, raw: Any, batches: list[Any]) -> Any:
    if post_publish is None:
        return None

    def step(conn: Any, summary: Any) -> None:
        post_publish(conn, store, raw, batches, summary)

    return step


async def run_connector(
    connector: Any,
    http: Any,
    store: ArtifactStore,
    engine: Engine,
    *,
    budget: FetchBudget | None = None,
    persist_cursor: bool = True,
    post_publish: Any = None,
    fence: Any = None,
) -> RunReport:
    spec = connector.describe()
    budget = budget or FetchBudget()
    cursor = load_cursor(engine, spec.name) if persist_cursor else None
    plan = connector.plan(cursor, budget)
    report = RunReport(spec.name)
    queue = list(plan.items)
    requests = 0
    while queue:
        item = queue.pop(0)
        requests += 1
        if requests > budget.max_requests:
            report.errors.append({"item": item.key, "error": "request budget exhausted"})
            report.outcomes.append(FetchOutcome(item_key=item.key, status="failed", detail="budget"))
            continue
        dep = item.params.get("depends_on")
        if dep and any(o.item_key == dep and o.status in ("quarantined", "failed") for o in report.outcomes):
            report.errors.append({"item": item.key, "error": f"blocked: dependency {dep} failed validation"})
            report.outcomes.append(FetchOutcome(item_key=item.key, status="failed", detail=f"blocked by {dep}"))
            continue
        try:
            raw = await connector.fetch(item, http)
            try:
                batches = list(connector.parse(raw))
                reports: list[QualityReport] = [connector.validate(b) for b in batches]
            except SourceResponseError as e:
                batches, reports = [], [
                    QualityReport(outcome="quarantine", findings=(Finding(
                        code=str(e.details.get("kind", "parse_error")), severity="blocker", message=e.message),))
                ]
            except (KeyError, IndexError, TypeError, ValueError, AttributeError, ArithmeticError) as e:
                # Row-level schema drift (a missing/renamed field, an unexpected type) in
                # an otherwise well-formed response: quarantine this item, keep the run going.
                log.warning("connector %s item %s: row-level schema drift: %s", spec.name, item.key,
                            type(e).__name__)
                batches, reports = [], [
                    QualityReport(outcome="quarantine", findings=(Finding(
                        code="row_schema_drift", severity="blocker",
                        message=f"parser failed on a row ({type(e).__name__}: {str(e)[:200]})"),))
                ]
            for b in batches:
                queue.extend(b.follow_up)
            report.quality[item.key] = [r.model_dump(mode="json") for r in reports]
            if raw.response_metadata.get("status") == 304:
                report.outcomes.append(FetchOutcome(item_key=item.key, status="unchanged", detail="304"))
                continue
            step = _bind_post_publish(post_publish, store, raw, batches)
            # Post-publish steps run inside the acquisition transaction (R-14): a crash
            # rolls the acquisition back, so the retry is not a duplicate and re-runs them.
            summary = publish_acquisition(engine, store, spec, raw, batches, reports, fence=fence,
                                          post_publish=step)
            report.inserted += summary.inserted
            report.unchanged += summary.unchanged
            report.relations += summary.relations
            status: Literal["published", "unchanged", "quarantined", "failed"] = (
                "quarantined" if summary.namespace == "quarantine" else (
                    "published" if (summary.inserted or not summary.duplicate_acquisition) else "unchanged"))
            report.outcomes.append(FetchOutcome(item_key=item.key, status=status, raw_sha256=summary.raw_sha256))
        except RevaError as e:
            log.warning("connector %s item %s failed: %s", spec.name, item.key, e.message)
            err: dict[str, Any] = {"item": item.key, "code": e.code, "error": e.message}
            http_status = e.details.get("status")
            if isinstance(http_status, int) and not isinstance(http_status, bool) and 100 <= http_status <= 599:
                err["status"] = http_status  # structured HTTP status (repair/health read it, not the message)
            report.errors.append(err)
            report.outcomes.append(FetchOutcome(item_key=item.key, status="failed", detail=e.message[:500]))
    new_cursor = connector.next_cursor(plan, report.outcomes)
    report.cursor = new_cursor.state
    if persist_cursor and new_cursor.state.get("complete"):
        save_cursor(engine, spec.name, new_cursor)
        report.cursor_advanced = True
    record_health(engine, spec.name, spec.version, report, origin=getattr(http, "origin", "live"))
    return report


def make_live_client(spec: Any, settings: Any) -> SafeHttpClient:
    from reva.adapters.http.rate_limit import HostPolicy, RateLimiter

    policies = {
        # Live check 2026-10-14: 429 "Try again in 2|3 seconds" at 1.5 s spacing.
        "comtradeapi.un.org": HostPolicy(min_interval_seconds=3.0, max_concurrency=1),
        "api.worldbank.org": HostPolicy(min_interval_seconds=0.5, max_concurrency=1),
        "api.crossref.org": HostPolicy(min_interval_seconds=1.0, max_concurrency=1),
        "ec.europa.eu": HostPolicy(min_interval_seconds=1.0, max_concurrency=1),
        "naciscdn.org": HostPolicy(min_interval_seconds=1.0, max_concurrency=1),
    }
    return SafeHttpClient(
        allowed_hosts=spec.allowed_hosts,
        timeout_seconds=settings.reva_http_timeout_seconds,
        max_bytes=min(settings.reva_http_max_bytes, spec.max_response_bytes),
        contact_email=settings.reva_http_contact_email,
        rate_limiter=RateLimiter(policies),
        proxy_mode=settings.reva_http_proxy_mode,
    )


def outcomes_ok(outcomes: Sequence[FetchOutcome]) -> bool:
    return all(o.status in ("published", "unchanged") for o in outcomes)


def post_publish_for(template: str) -> Any:
    """Post-publish step for a connector template.

    Contract: ``fn(conn, store, raw, batches, summary)``; it runs inside the
    acquisition publication transaction (only for newly published raw bytes) and
    must not commit.
    """
    if template == "naturalearth_asset":
        from reva.world.space import publish_display_geometries

        return publish_display_geometries
    if template == "crossref_doi":
        from reva.evidence.cards import publish_study

        return publish_study
    return None
