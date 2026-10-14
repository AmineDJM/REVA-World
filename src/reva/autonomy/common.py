"""Shared autonomy plumbing: artifact I/O, typed blocked results and budgets.

Artifacts are immutable, content-addressed objects in the ArtifactStore. An
``ArtifactRef`` is only a pointer; registration in ``meta.artifact`` happens in
the transaction that references it (proposal, gate result, report).
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections.abc import Coroutine, Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection

from reva.adapters.postgres import repositories as repo
from reva.kernel.errors import IntegrityFailure, RevaError
from reva.kernel.ids import sha256_bytes
from reva.ports.autonomy import ArtifactRef, Finding, ResourceBudget, ScopedContext, ValidationReport
from reva.ports.storage import ArtifactStore

AUTONOMY_NAMESPACE = uuid.UUID("0b8d3a52-6c1e-5f0e-9a43-2f6c1d7e8a10")


class AutonomyBlocked(RevaError):
    """An autonomy action was refused (kill switch, disabled mode, budget, backlog)."""

    code = "autonomy_blocked"
    http_status = 423


def stable_id(*parts: str) -> uuid.UUID:
    return uuid.uuid5(AUTONOMY_NAMESPACE, "\x1f".join(parts))


def now_utc() -> datetime:
    return datetime.now(UTC)


def plain_json_bytes(payload: Any) -> bytes:
    """Deterministic plain JSON (sorted keys, no NaN); decimals/dates as strings."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False, default=_default).encode("utf-8")


def _default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, uuid.UUID):
        return str(o)
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    return str(o)


class ArtifactIO:
    """JSON/bytes artifacts in the content-addressed store, addressed by ArtifactRef."""

    def __init__(self, store: ArtifactStore) -> None:
        self.store = store

    def put_bytes(self, data: bytes, namespace: str, media_type: str) -> ArtifactRef:
        obj = self.store.put_bytes(data, namespace, media_type)
        return ArtifactRef(sha256=obj.sha256, namespace=obj.namespace, media_type=media_type)

    def put_json(self, payload: Any, namespace: str = "models") -> ArtifactRef:
        return self.put_bytes(plain_json_bytes(payload), namespace, "application/json")

    def get_bytes(self, ref: ArtifactRef) -> bytes:
        data = self.store.get(ref.namespace, ref.sha256)
        if sha256_bytes(data) != ref.sha256:
            raise IntegrityFailure("artifact bytes do not match their reference", sha256=ref.sha256)
        return data

    def get_json(self, ref: ArtifactRef) -> Any:
        return json.loads(self.get_bytes(ref))

    def register(self, conn: Connection, ref: ArtifactRef, schema_version: str,
                 rights: Mapping[str, Any] | None = None) -> str:
        """Record the (already stored) artifact in meta.artifact inside the caller's transaction."""
        obj = self.store.put_bytes(self.get_bytes(ref), ref.namespace, ref.media_type)  # idempotent
        return repo.register_artifact(conn, obj, schema_version=schema_version,
                                      rights=rights or {"licence": "internal-derived"})


def default_budget(**over: Any) -> ResourceBudget:
    base: dict[str, Any] = {"max_requests": 5, "max_bytes": 8_000_000, "max_wall_seconds": 120,
                            "max_cpu_seconds": 60, "max_memory_bytes": 1_073_741_824, "max_cost_usd": "0"}
    base.update(over)
    return ResourceBudget(**base)


def report(outcome: str, findings: list[Finding], evidence: ArtifactRef, evaluator: str,
           usage: Mapping[str, Any] | None = None, report_id: uuid.UUID | None = None) -> ValidationReport:
    return ValidationReport(
        report_id=report_id or uuid.uuid4(),
        outcome=outcome,  # type: ignore[arg-type]
        findings=tuple(findings),
        evidence_manifest=evidence,
        evaluator_version=evaluator,
        resource_usage=dict(usage or {}),
    )


def outcome_of(findings: list[Finding]) -> str:
    return "fail" if any(f.severity == "blocker" for f in findings) else "pass"


def report_json(r: ValidationReport) -> dict[str, Any]:
    return {
        "report_id": str(r.report_id),
        "outcome": r.outcome,
        "findings": [{"code": f.code, "severity": f.severity, "message": f.message,
                      "evidence": [e.sha256 for e in f.evidence]} for f in r.findings],
        "evidence_manifest": r.evidence_manifest.sha256,
        "evaluator_version": r.evaluator_version,
        "resource_usage": dict(r.resource_usage),
    }


def ref_json(ref: ArtifactRef | None) -> dict[str, str] | None:
    if ref is None:
        return None
    return {"sha256": ref.sha256, "namespace": ref.namespace, "media_type": ref.media_type}


def ref_from(d: Mapping[str, Any]) -> ArtifactRef:
    return ArtifactRef(sha256=str(d["sha256"]), namespace=str(d["namespace"]), media_type=str(d["media_type"]))


def make_context(io: ArtifactIO, mode: str, cutoff: datetime, ontology_manifest: Any,
                 context_id: uuid.UUID | None = None) -> ScopedContext:
    ref = io.put_json(ontology_manifest, "models")
    return ScopedContext(context_id=context_id or uuid.uuid4(), mode=mode,  # type: ignore[arg-type]
                         knowledge_cutoff=cutoff, ontology_manifest=ref)


def run_sync[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine from sync code, also when called inside a running event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as e:
            box["error"] = e

    t = threading.Thread(target=target, name="reva-autonomy-async")
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box["value"]  # type: ignore[no-any-return]
