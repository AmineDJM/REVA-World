"""ConnectorHealthMonitor V1: a health *vector* from run history.

Dimensions (architecture §10.3):

* ``request_success`` — success ratio over the last 20 scheduled runs;
* ``freshness`` — delay of the provider's latest release relative to the
  provider's own cadence (expected next release = last provider update +
  cadence + grace), **not** days since our last ingestion;
* ``semantic_drift`` — schema fingerprints (key-order independent, dimension/
  unit preserving) of recent runs vs the accepted fingerprint;
* ``completeness`` — received vs expected keys;
* ``revision_anomalies`` — revisions per run vs a robust baseline;
* ``provenance`` — share of runs with a raw artifact hash;
* ``latency`` — p50/p95 seconds.

The summary ``state`` is operational only.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from reva.autonomy.common import ArtifactIO
from reva.ports.autonomy import JSON, ArtifactRef, HealthReport

WINDOW = 20
SUCCESS = ("success", "published", "unchanged")


def _ts(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = min(len(s) - 1, max(0, round(q * (len(s) - 1))))
    return s[k]


def health_vector(runs: list[Mapping[str, Any]], schedule: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    runs = sorted(runs, key=lambda r: _ts(r.get("started_at")) or now)
    recent = runs[-WINDOW:]
    ok = [r for r in recent if r.get("outcome") in SUCCESS]
    dims: dict[str, Any] = {"window_runs": len(recent)}
    dims["request_success"] = {"ratio": (len(ok) / len(recent)) if recent else None, "successes": len(ok),
                               "runs": len(recent)}
    cadence = int(schedule.get("cadence_seconds") or 0)
    grace = int(schedule.get("grace_seconds") or 0)
    provider_updates = [t for t in (_ts(r.get("source_last_update")) for r in ok) if t is not None]
    last_update = max(provider_updates) if provider_updates else None
    if cadence <= 0:
        dims["freshness"] = {"state": "unknown", "reason": "no provider cadence declared"}
    elif last_update is None:
        dims["freshness"] = {"state": "unknown", "reason": "no provider release timestamp observed"}
    else:
        expected_next = last_update + timedelta(seconds=cadence + grace)
        overdue = (now - expected_next).total_seconds()
        dims["freshness"] = {"state": "overdue" if overdue > 0 else "on_schedule",
                             "provider_last_update": last_update.isoformat(),
                             "expected_next_release_by": expected_next.isoformat(),
                             "overdue_seconds": max(0.0, overdue), "basis": "provider_cadence"}
    accepted = schedule.get("accepted_fingerprint") or (ok[0].get("schema_fingerprint") if ok else None)
    fps = [r.get("schema_fingerprint") for r in ok if r.get("schema_fingerprint")]
    latest_fp = fps[-1] if fps else None
    dims["semantic_drift"] = {"accepted_fingerprint": accepted, "latest_fingerprint": latest_fp,
                              "drift": bool(accepted and latest_fp and latest_fp != accepted),
                              "distinct_fingerprints": len(set(fps))}
    comp = [(int(r.get("received_count") or 0), int(r.get("expected_count") or 0)) for r in ok
            if r.get("expected_count")]
    dims["completeness"] = {"latest": (comp[-1][0] / comp[-1][1]) if comp else None,
                            "mean": (sum(a / b for a, b in comp) / len(comp)) if comp else None}
    revs = [int(r.get("revisions") or 0) for r in ok]
    base = statistics.median(revs[:-1]) if len(revs) > 1 else 0
    anomaly = bool(revs) and revs[-1] > max(10, 3 * base)
    dims["revision_anomalies"] = {"latest": revs[-1] if revs else None, "baseline_median": base,
                                  "anomaly": anomaly}
    dims["provenance"] = {"raw_hash_ratio": (sum(1 for r in recent if r.get("raw_sha256")) / len(recent))
                          if recent else None}
    lat = [float(r["latency_seconds"]) for r in recent if r.get("latency_seconds") is not None]
    dims["latency"] = {"p50": _pct(lat, 0.5), "p95": _pct(lat, 0.95)}
    latest = recent[-1] if recent else None
    if not recent:
        state = "unknown"
    elif dims["semantic_drift"]["drift"] or (latest and latest.get("outcome") == "quarantined"):
        state = "quarantined"
    elif ((dims["request_success"]["ratio"] or 0) < 0.9 or dims["freshness"].get("state") == "overdue"
          or (dims["completeness"]["latest"] is not None and dims["completeness"]["latest"] < 1.0) or anomaly):
        state = "degraded"
    else:
        state = "healthy"
    dims["state"] = state
    return dims


class RunHistoryHealthMonitor:
    def __init__(self, io_: ArtifactIO) -> None:
        self.io = io_

    def assess(self, connector_version_id: UUID, run_history: ArtifactRef, expected_schedule: Mapping[str, JSON],
               now: datetime) -> HealthReport:
        history = self.io.get_json(run_history)
        runs = history.get("runs", []) if isinstance(history, dict) else list(history)
        dims = health_vector(runs, expected_schedule, now)
        ev = self.io.put_json({"schema": "reva-connector-health/1", "connector_version_id": str(connector_version_id),
                               "run_history": run_history.sha256, "schedule": dict(expected_schedule),
                               "observed_at": now.isoformat(), "dimensions": dims}, "evaluation")
        return HealthReport(connector_version_id=connector_version_id, observed_at=now,
                            state=dims["state"], dimensions=dims, evidence=ev)


def run_history_from_db(conn: Connection, connector_version_id: UUID, limit: int = WINDOW) -> dict[str, Any]:
    """Run history from ops.connector_health + the acquisitions recorded in the same window."""
    rows = conn.execute(text(
        """SELECT recorded_at, status, metrics FROM ops.connector_health WHERE connector_version_id = :cv
           ORDER BY recorded_at DESC LIMIT :n"""), {"cv": connector_version_id, "n": limit}).mappings().all()
    acq = conn.execute(text(
        """SELECT retrieved_at, raw_sha256, response_metadata FROM evidence.acquisition
           WHERE connector_version_id = :cv ORDER BY retrieved_at DESC LIMIT :n"""),
        {"cv": connector_version_id, "n": limit}).mappings().all()
    runs = []
    for i, r in enumerate(reversed(rows)):
        m = dict(r["metrics"])
        a = acq[len(rows) - 1 - i] if len(acq) == len(rows) else None
        meta = (a["response_metadata"] if a else {}) or {}
        bm = (meta.get("batch_metadata") or [{}])[0] if meta.get("batch_metadata") else {}
        quality = (meta.get("quality") or [{}])[0] if meta.get("quality") else {}
        runs.append({
            "started_at": r["recorded_at"].isoformat(),
            "outcome": {"healthy": "success", "degraded": "failed"}.get(r["status"], r["status"]),
            "latency_seconds": meta.get("elapsed_seconds"),
            "schema_fingerprint": (meta.get("schema_fingerprints") or [None])[0],
            "expected_count": quality.get("expected_count"), "received_count": quality.get("received_count"),
            "revisions": m.get("inserted", 0),
            "source_last_update": (bm.get("annotations") or {}).get("UPDATE_DATA") or bm.get("dataset_updated"),
            "raw_sha256": a["raw_sha256"] if a else None,
        })
    return {"schema": "reva-run-history/1", "connector_version_id": str(connector_version_id), "runs": runs}
