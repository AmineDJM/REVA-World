"""Structured logs with bounded correlation fields, secret redaction, and the
bounded-cardinality Prometheus registry."""

from __future__ import annotations

import io
import json
import logging

import pytest

from reva.operations import telemetry as tm


def test_json_logs_carry_correlation_and_redact_secrets(monkeypatch) -> None:
    monkeypatch.setenv("REVA_WORKER_DB_PASSWORD", "sup3r-secret-pw")
    monkeypatch.setenv("REVA_S3_SECRET_ACCESS_KEY", "AKIA-very-secret")
    stream = io.StringIO()
    handler = tm.configure_logging("INFO", stream=stream)
    try:
        log = logging.getLogger("reva.test.telemetry")
        with tm.log_context(job_id="job-123", release_id="rel-9", user_email="x@example.com"):
            log.info("connecting with %s and pw sup3r-secret-pw key=AKIA-very-secret",
                     "postgresql+psycopg://reva_worker:hunter22@db:5432/reva", extra={"run_id": "run-1"})
    finally:
        logging.getLogger().removeHandler(handler)
    rec = json.loads(stream.getvalue().strip().splitlines()[-1])
    assert rec["job_id"] == "job-123" and rec["release_id"] == "rel-9" and rec["run_id"] == "run-1"
    assert "user_email" not in rec  # only the fixed correlation fields
    for secret in ("sup3r-secret-pw", "AKIA-very-secret", "hunter22"):
        assert secret not in rec["msg"]
    assert "reva_worker:***@db" in rec["msg"]


def test_secret_env_names() -> None:
    for name in ("REVA_API_DB_PASSWORD", "REVA_SESSION_SECRET", "REVA_COMTRADE_API_KEY", "DATABASE_URL",
                 "REVA_MIGRATION_DATABASE_URL", "REVA_S3_ACCESS_KEY_ID"):
        assert tm.is_secret_env_name(name), name
    for name in ("REVA_ENV", "OMP_NUM_THREADS", "REVA_DB_HOST"):
        assert not tm.is_secret_env_name(name), name


def test_metrics_have_bounded_labels_and_render() -> None:
    reg = tm.Registry()
    with pytest.raises(ValueError):
        reg.counter("reva_bad_total", "entity ids are not labels", ("entity_id",))
    c = reg.counter("reva_test_requests_total", "t", ("connector", "status"))
    for i in range(tm.MAX_LABEL_VALUES + 10):
        c.inc(connector=f"conn.{i}", status="published")
    c.inc(connector="https://evil.example/?q=1", status="weird-status")
    text_ = reg.render()
    connectors = {line.split('connector="')[1].split('"')[0] for line in text_.splitlines()
                  if line.startswith("reva_test_requests_total{")}
    assert len(connectors) <= tm.MAX_LABEL_VALUES + 1 and "other" in connectors
    assert 'status="other"' in text_
    h = reg.histogram("reva_test_seconds", "t", ("job_type",), buckets=(0.1, 1))
    h.observe(0.05, job_type="ingest")
    h.observe(5, job_type="not-a-job-type")
    out = reg.render()
    assert 'reva_test_seconds_bucket{job_type="ingest",le="0.1"} 1' in out
    assert 'reva_test_seconds_count{job_type="other"} 1' in out
    assert "# TYPE reva_test_seconds histogram" in out


def test_default_registry_exposes_required_series() -> None:
    out = tm.REGISTRY.render()
    for name in ("reva_jobs_claimed_total", "reva_jobs_completed_total", "reva_jobs_failed_total",
                 "reva_queue_oldest_age_seconds", "reva_job_lease_expiries_total", "reva_ingestion_requests_total",
                 "reva_ingestion_bytes_total", "reva_simulation_duration_seconds", "reva_simulation_max_rss_bytes"):
        assert f"# TYPE {name} " in out
