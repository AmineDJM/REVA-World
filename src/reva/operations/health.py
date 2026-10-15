"""Connector health vectors (appended to ops.connector_health, never updated).

The vector summarises the last ``window`` (default 20) connector runs and
acquisitions of a connector (all registered versions):

* ``success_ratio`` – mean item-level success over the run records written by
  ``reva.evidence.runner.record_health`` (published or unchanged / planned).
* ``freshness`` – time since the last *successful check* (a run with at least
  one published/unchanged item) expressed in units of the connector's declared
  cadence (``lag_cadences``). It is late only beyond ``LATE_CADENCES`` (2)
  expected cadences, so an annual/monthly source is never "stale" after a day.
  ``last_new_data_at`` (newest validated acquisition) is reported separately:
  unchanged data is not a freshness failure.
* ``schema_drift`` – per request item, whether the newest acquisition's schema
  fingerprints differ from those previously seen for the same item.
* ``completeness`` – item completeness of the latest run and the share of
  acquisitions in the window that passed validation (not quarantined).
* ``connector_state`` – for promoted template connectors
  (:func:`reva.autonomy.health.connector_state` of the newest version): ``active``,
  ``retired`` or ``template_changed``; ``template_changed`` (the reviewed template
  code changed since promotion, so the version is no longer loaded) makes the
  status ``quarantined``. ``None`` for built-in connectors.

Rows written here carry ``metrics.kind = 'health_vector'`` and are excluded
when reading run records.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, text

log = logging.getLogger("reva.health")

WINDOW = 20
LATE_CADENCES = 2.0
MIN_SUCCESS = 0.95


def _runs(conn: Any, cv_ids: list[Any], window: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            "SELECT metrics, status, recorded_at FROM ops.connector_health "
            "WHERE connector_version_id = ANY(:ids) AND COALESCE(metrics->>'kind', 'run') <> 'health_vector' "
            "ORDER BY recorded_at DESC LIMIT :n"
        ),
        {"ids": cv_ids, "n": window},
    ).mappings()
    return [dict(r) for r in rows]


def _acquisitions(conn: Any, cv_ids: list[Any], origin: str | None, window: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            """SELECT a.retrieved_at, a.recorded_at, a.origin,
                      a.response_metadata->>'item_key' AS item_key,
                      a.response_metadata->'schema_fingerprints' AS fps,
                      EXISTS (SELECT 1 FROM meta.artifact_location l
                               WHERE l.artifact_sha256 = a.raw_sha256 AND l.namespace = 'quarantine') AS quarantined
                 FROM evidence.acquisition a
                WHERE a.connector_version_id = ANY(:ids)
                  AND (CAST(:o AS text) IS NULL OR a.origin = CAST(:o AS text))
                ORDER BY a.recorded_at DESC LIMIT :n"""
        ),
        {"ids": cv_ids, "o": origin, "n": window},
    ).mappings()
    return [dict(r) for r in rows]


def _connector_state(conn: Any, connector_version_id: Any) -> str | None:
    """Promotion state of a template connector version (None for built-ins / unavailable)."""
    try:
        from reva.autonomy.health import connector_state

        with conn.begin_nested():
            return connector_state(conn, connector_version_id)
    except Exception as e:  # the health vector is still recorded; the state is reported unknown
        log.warning("connector state unavailable for %s: %s", connector_version_id, type(e).__name__)
        return None


def connector_health_vector(
    engine: Engine, connector_name: str, cadence_seconds: int, *, origin: str | None = None, window: int = WINDOW
) -> dict[str, Any] | None:
    """Compute the health vector; ``None`` if the connector is not registered."""
    with engine.connect() as conn:
        cvs = conn.execute(
            text("SELECT id FROM evidence.connector_version WHERE name = :n ORDER BY recorded_at DESC"),
            {"n": connector_name},
        ).scalars().all()
        if not cvs:
            return None
        now: datetime = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        runs = [r for r in _runs(conn, list(cvs), window)
                if origin is None or r["metrics"].get("origin") in (None, origin)]
        acqs = _acquisitions(conn, list(cvs), origin, window)
        last_new = conn.execute(
            text(
                """SELECT max(a.retrieved_at) FROM evidence.acquisition a
                    WHERE a.connector_version_id = ANY(:ids)
                      AND (CAST(:o AS text) IS NULL OR a.origin = CAST(:o AS text))
                      AND NOT EXISTS (SELECT 1 FROM meta.artifact_location l
                                       WHERE l.artifact_sha256 = a.raw_sha256 AND l.namespace = 'quarantine')"""
            ),
            {"ids": list(cvs), "o": origin},
        ).scalar_one()
        state = _connector_state(conn, cvs[0])

    ratios = [float(r["metrics"].get("request_success_ratio", 0.0)) for r in runs]
    success_ratio = sum(ratios) / len(ratios) if ratios else None
    ok_runs = [r for r in runs if float(r["metrics"].get("request_success_ratio", 0.0)) > 0]
    last_check = max((r["recorded_at"] for r in ok_runs), default=None)
    lag = (now - last_check).total_seconds() if last_check else None
    lag_cadences = lag / cadence_seconds if lag is not None else None

    by_item: dict[str, list[tuple[str, ...]]] = defaultdict(list)
    for a in acqs:  # newest first
        if not a["quarantined"]:
            by_item[a["item_key"] or "?"].append(tuple(sorted(a["fps"] or [])))
    drift_items = sorted(k for k, fps in by_item.items() if len(fps) > 1 and fps[0] not in fps[1:])
    validated = sum(1 for a in acqs if not a["quarantined"])
    latest = runs[0] if runs else None
    completeness_latest = float(latest["metrics"].get("request_success_ratio", 0.0)) if latest else None

    if not runs and not acqs:
        status = "unknown"
    elif state == "template_changed" or (latest is not None and latest["status"] == "quarantined"):
        status = "quarantined"
    elif ((success_ratio is not None and success_ratio < MIN_SUCCESS)
          or lag_cadences is None or lag_cadences > LATE_CADENCES or drift_items
          or (completeness_latest is not None and completeness_latest < MIN_SUCCESS)):
        status = "degraded"
    else:
        status = "healthy"
    return {
        "kind": "health_vector",
        "connector": connector_name,
        "window": window,
        "origin_filter": origin,
        "runs_considered": len(runs),
        "acquisitions_considered": len(acqs),
        "success_ratio": success_ratio,
        "freshness": {
            "cadence_seconds": cadence_seconds,
            "last_successful_check_at": last_check.isoformat() if last_check else None,
            "last_new_data_at": last_new.isoformat() if last_new else None,
            "lag_seconds": lag,
            "lag_cadences": lag_cadences,
            "late": lag_cadences is None or lag_cadences > LATE_CADENCES,
            "late_threshold_cadences": LATE_CADENCES,
        },
        "schema_drift": {"drift": bool(drift_items), "items": drift_items[:20],
                         "distinct_fingerprint_sets": len({f for fps in by_item.values() for f in fps})},
        "completeness": {
            "latest_run_item_completeness": completeness_latest,
            "validated_acquisition_ratio": validated / len(acqs) if acqs else None,
        },
        "connector_state": state,
        "status": status,
    }


def append_health(engine: Engine, connector_name: str, vector: dict[str, Any]) -> None:
    with engine.begin() as conn:
        cv = conn.execute(
            text("SELECT id FROM evidence.connector_version WHERE name = :n ORDER BY recorded_at DESC LIMIT 1"),
            {"n": connector_name},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO ops.connector_health(connector_version_id, metrics, status) "
                "VALUES (:cv, CAST(:m AS jsonb), :s)"
            ),
            {"cv": cv, "m": json.dumps(vector, default=str, allow_nan=False), "s": vector["status"]},
        )
