"""Outbox-driven incremental invalidation on a fresh database: DAG descendants,
coupled components (cycles) invalidated as a whole, materiality skips that
accumulate an error budget and force a refresh, and idempotent publish_beliefs
enqueueing that never re-dirties nodes covered by a published release."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text

from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.adapters.postgres.engine import make_engine
from reva.operations import invalidation
from reva.operations.worker import Worker
from reva.settings import Settings

pytestmark = pytest.mark.db
REPO = Path(__file__).resolve().parents[2]


@dataclass
class FreshDB:
    settings: Settings
    owner: Engine
    worker: Engine
    sim: Engine
    store: FilesystemArtifactStore


def fresh_database(tmp_path_factory: pytest.TempPathFactory, label: str) -> Iterator[FreshDB]:
    base = Settings(_env_file=REPO / ".env")  # type: ignore[call-arg]
    if base.reva_migration_database_url is None:
        pytest.skip("REVA_MIGRATION_DATABASE_URL not configured")
    admin = create_engine(base.migration_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("PostgreSQL not reachable")
    name = f"reva_test_{label}_{uuid.uuid4().hex[:8]}"
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    root = tmp_path_factory.mktemp(f"objects_{label}")
    data = base.model_dump()
    data.update(reva_migration_database_url=base.migration_url().set(database=name).render_as_string(
        hide_password=False), reva_db_name=name, reva_env="test", reva_object_root=root)
    settings = Settings.model_validate(data)
    from reva.cli.bootstrap import bootstrap
    from reva.cli.seed import seed

    for attempt in range(3):
        try:
            bootstrap(settings)
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1)
    store = FilesystemArtifactStore(root, settings.reva_s3_prefix)
    pooled = settings.model_copy(update={"reva_db_pool_size": 4, "reva_db_max_overflow": 0})
    d = FreshDB(settings, make_engine("migration", settings), make_engine("worker", pooled),
                make_engine("sim", settings), store)
    seed(d.worker, store)
    try:
        yield d
    finally:
        for e in (d.owner, d.worker, d.sim):
            e.dispose()
        if not os.environ.get("REVA_KEEP_TEST_DB"):
            with admin.connect() as c:
                c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="module")
def idb(tmp_path_factory: pytest.TempPathFactory) -> Iterator[FreshDB]:
    yield from fresh_database(tmp_path_factory, "inval")


def _deps(d: FreshDB, edges: list[tuple[str, str]], policy: dict | None = None) -> None:
    with d.owner.begin() as c:
        for up, down in edges:
            c.execute(text("INSERT INTO ops.dependency(upstream_key, downstream_key, version, approximation_policy) "
                           "VALUES (:u, :d, '1', CAST(:p AS jsonb))"),
                      {"u": up, "d": down, "p": json.dumps(policy or {"kind": "exact"})})


def _event(d: FreshDB, payload: dict, created_at_sql: str = "clock_timestamp()") -> int:
    with d.worker.begin() as c:
        return int(c.execute(text(f"INSERT INTO ops.outbox(event_type, payload, created_at) VALUES "
                                  f"('evidence.observations_changed', CAST(:p AS jsonb), {created_at_sql}) "
                                  "RETURNING id"), {"p": json.dumps(payload)}).scalar_one())


def _dirty(d: FreshDB) -> dict[str, tuple[str, dict]]:
    with d.owner.connect() as c:
        return {r.node_key: (r.reason, r.accumulated_error_bound)
                for r in c.execute(text("SELECT node_key, reason, accumulated_error_bound FROM ops.dirty_node"))}


def _reset(d: FreshDB) -> None:
    with d.owner.begin() as c:
        c.execute(text("DELETE FROM ops.dirty_node"))
        c.execute(text("UPDATE ops.outbox SET processed_at = clock_timestamp() WHERE processed_at IS NULL"))


def test_tarjan_components() -> None:
    adj = {"a": ["b"], "b": ["c"], "c": ["a", "d"], "d": [], "e": ["d"]}
    comps = invalidation.strongly_connected_components(set("abcde"), adj)
    assert sorted(map(tuple, comps)) == [("a", "b", "c"), ("d",), ("e",)]


def test_dag_descendants_only(idb: FreshDB) -> None:
    _reset(idb)
    _deps(idb, [("belief:t.dag.x", "model:dag.a"), ("model:dag.a", "scenario:dag.s1"), ("model:dag.a", "card:dag.c"),
                ("belief:t.dag.y", "model:dag.b"), ("observation:t.dag.x:FRA", "model:dag.f")])
    eid = _event(idb, {"keys": [["t.dag.x", "FRA"]]})
    rep = invalidation.process_outbox(idb.worker)
    dirty = _dirty(idb)
    assert {"belief:t.dag.x", "observation:t.dag.x:FRA", "model:dag.a", "scenario:dag.s1", "card:dag.c",
            "model:dag.f"} <= set(dirty)
    assert "model:dag.b" not in dirty and "belief:t.dag.y" not in dirty
    assert rep.events >= 1 and rep.publish_jobs == []  # no acquisition origin -> nothing to publish
    with idb.owner.connect() as c:
        assert c.execute(text("SELECT processed_at IS NOT NULL FROM ops.outbox WHERE id = :i"), {"i": eid}).scalar()
        seq = c.execute(text("SELECT since_sequence FROM ops.dirty_node WHERE node_key = 'model:dag.a'")).scalar()
        assert seq == eid
    assert invalidation.process_outbox(idb.worker).events == 0  # processed exactly once


def test_cycle_invalidates_coupled_component_as_a_whole(idb: FreshDB) -> None:
    _reset(idb)
    _deps(idb, [("belief:t.cyc", "model:cyc.p"), ("model:cyc.p", "model:cyc.q"), ("model:cyc.q", "model:cyc.u"),
                ("model:cyc.u", "model:cyc.p"), ("model:cyc.q", "model:cyc.r"), ("model:cyc.s", "model:cyc.p")])
    _event(idb, {"keys": [["t.cyc", "CHN"]]})
    rep = invalidation.process_outbox(idb.worker)
    dirty = _dirty(idb)
    assert {"model:cyc.p", "model:cyc.q", "model:cyc.u", "model:cyc.r"} <= set(dirty)
    assert "model:cyc.s" not in dirty  # upstream of the cycle is unaffected
    assert dirty["model:cyc.q"][0] == "coupled_component" and dirty["model:cyc.u"][0] == "coupled_component"
    assert ["model:cyc.p", "model:cyc.q", "model:cyc.u"] in rep.coupled_components


def test_materiality_skips_accumulate_error_budget_then_force_refresh(idb: FreshDB) -> None:
    _reset(idb)
    _deps(idb, [("belief:t.mat", "model:mat.m")],
          {"kind": "materiality", "threshold": "0.01", "error_budget": "0.025", "metric": "abs_relative_change"})
    reasons = []
    for _ in range(3):
        _event(idb, {"keys": [["t.mat", "USA"]], "change_bounds": {"belief:t.mat": "0.01"}})
        invalidation.process_outbox(idb.worker)
        reasons.append(_dirty(idb)["model:mat.m"])
    assert [r for r, _ in reasons] == ["materiality_skip", "materiality_skip", "error_budget_exhausted"]
    assert [b["value"] for _, b in reasons] == ["0.01", "0.02", "0.03"]
    assert reasons[-1][1]["budget"] == "0.025" and reasons[-1][1]["exhausted"] is True
    # an unquantified change is never skipped
    _reset(idb)
    _event(idb, {"keys": [["t.mat", "USA"]]})
    invalidation.process_outbox(idb.worker)
    assert _dirty(idb)["model:mat.m"][0] == "upstream_changed"
    # a change above the threshold is never skipped
    _reset(idb)
    _event(idb, {"keys": [["t.mat", "USA"]], "change_bounds": {"belief:t.mat": "0.5"}})
    invalidation.process_outbox(idb.worker)
    assert _dirty(idb)["model:mat.m"][0] == "upstream_changed"


def test_ingest_events_enqueue_one_publication_and_releases_are_not_redirtied(idb: FreshDB) -> None:
    import asyncio

    from reva.adapters.connectors.registry import builtin_connectors
    from reva.evidence.runner import FixtureHttp, run_connector

    _reset(idb)
    with idb.owner.begin() as c:
        c.execute(text("UPDATE ops.job SET status = 'cancelled', worker_id = NULL, lease_until = NULL "
                       "WHERE status IN ('queued','running','cancel_requested')"))
    conn_ = builtin_connectors()["worldbank.wdi.indicators"]
    rep = asyncio.run(run_connector(conn_, FixtureHttp(conn_.describe().name), idb.store, idb.worker,
                                    persist_cursor=False))
    assert rep.inserted > 0
    r1 = invalidation.process_outbox(idb.worker, environment="test")
    assert len(r1.publish_jobs) == 1 and any(k.startswith("belief:") for k in r1.dirty)
    assert invalidation.process_outbox(idb.worker).publish_jobs == []  # nothing new, no duplicate job
    w = Worker(idb.settings, idb.worker, idb.store, idb.sim, scheduler=False, invalidation=False,
               lease_seconds=30, heartbeat_seconds=5, job_types=["publish_beliefs"])
    job = w.claim_one()
    assert job is not None and str(job.id) == r1.publish_jobs[0]
    assert w.execute(job).status == "succeeded"
    with idb.owner.connect() as c:
        acq = c.execute(text("SELECT id FROM evidence.acquisition LIMIT 1")).scalar()
        assert c.execute(text("SELECT count(*) FROM belief.release")).scalar() == 1
    # A late-processed event that the release already covers does not re-dirty beliefs.
    _event(idb, {"acquisition_id": str(acq), "keys": [["wdi.population", "FRA"]]},
           "clock_timestamp() - interval '1 hour'")
    r2 = invalidation.process_outbox(idb.worker, environment="test")
    assert not any(k.startswith("belief:") for k in _dirty(idb)) and r2.publish_jobs == []
    # In production, fixture-origin events never enqueue a publication.
    _event(idb, {"acquisition_id": str(acq), "keys": [["wdi.population", "FRA"]]})
    r3 = invalidation.process_outbox(idb.worker, environment="production")
    assert r3.publish_jobs == [] and "belief:wdi.population" in _dirty(idb)


# --------------------------------------------------------------------------- R2-02 per-event isolation
def test_failing_event_is_isolated_retried_with_backoff_and_surfaced(idb: FreshDB, monkeypatch) -> None:
    _reset(idb)
    bad = _event(idb, {"keys": [["concept.bad", "FRA"]]})
    good = _event(idb, {"keys": [["concept.good", "FRA"]]})
    real = invalidation._process_observation_event

    def flaky(conn, ev, *a, **k):
        if int(ev.id) == bad:
            conn.execute(text("SELECT 1/0"))  # a real DB error inside the event's savepoint
        return real(conn, ev, *a, **k)

    monkeypatch.setattr(invalidation, "_process_observation_event", flaky)
    rep = invalidation.process_outbox(idb.worker)
    assert rep.failed_events == [bad] and "belief:concept.good" in rep.dirty
    with idb.owner.connect() as c:
        processed = dict(c.execute(text("SELECT id, processed_at IS NOT NULL FROM ops.outbox WHERE id = ANY(:i)"),
                                   {"i": [bad, good]}).all())
        audit = c.execute(text("SELECT details FROM ops.audit WHERE action = 'outbox.event_failed' "
                               "AND (target->>'outbox_id')::bigint = :i"), {"i": bad}).scalars().all()
    assert processed == {bad: False, good: True}
    assert len(audit) == 1 and audit[0]["failures"] == 1 and audit[0]["exception"] == "DataError"
    assert "belief:concept.bad" not in _dirty(idb)
    from reva.operations.telemetry import OUTBOX_FAILING_EVENTS

    assert "reva_outbox_failing_events 1" in "\n".join(OUTBOX_FAILING_EVENTS.render())
    # In backoff: the event is not even selected, newer events still flow.
    newer = _event(idb, {"keys": [["concept.newer", "FRA"]]})
    rep = invalidation.process_outbox(idb.worker)
    assert rep.events == 1 and rep.failed_events == [] and "belief:concept.newer" in rep.dirty
    # After the backoff (shortened here) it is retried; once healthy it is processed.
    monkeypatch.setattr(invalidation, "OUTBOX_BACKOFF_BASE", 0.001)
    monkeypatch.setattr(invalidation, "_process_observation_event", real)
    rep = invalidation.process_outbox(idb.worker)
    assert rep.events == 1 and rep.failed_events == [] and "belief:concept.bad" in rep.dirty
    with idb.owner.connect() as c:
        assert c.execute(text("SELECT processed_at IS NOT NULL FROM ops.outbox WHERE id = :i"), {"i": bad}).scalar()
    assert newer > good


def test_publish_enqueue_failure_never_aborts_the_batch(idb: FreshDB, monkeypatch) -> None:
    import reva.operations.jobs as jobs_mod

    _reset(idb)
    with idb.owner.begin() as c:
        acq = c.execute(text("SELECT id FROM evidence.acquisition WHERE origin = 'test_fixture' LIMIT 1")).scalar()
        c.execute(text("UPDATE ops.job SET status = 'cancelled', worker_id = NULL, lease_until = NULL "
                       "WHERE status IN ('queued','running','cancel_requested')"))
    if acq is None:
        pytest.skip("needs the fixture acquisition created by the ingest test above")
    ev = _event(idb, {"acquisition_id": str(acq), "keys": [["concept.pubfail", "FRA"]]})
    real = jobs_mod.enqueue_publish_beliefs

    def broken(conn, *a, **k):
        conn.execute(text("SELECT 1/0"))
        return real(conn, *a, **k)

    monkeypatch.setattr(jobs_mod, "enqueue_publish_beliefs", broken)
    rep = invalidation.process_outbox(idb.worker, environment="test")
    assert rep.publish_jobs == [] and rep.publish_requests_deferred == ["test_fixture"]
    assert "belief:concept.pubfail" in _dirty(idb)
    with idb.owner.connect() as c:
        assert c.execute(text("SELECT processed_at IS NOT NULL FROM ops.outbox WHERE id = :i"), {"i": ev}).scalar()
        req = c.execute(text("SELECT payload FROM ops.outbox WHERE event_type = 'beliefs.publish_requested' "
                             "AND processed_at IS NULL")).scalars().all()
    assert len(req) == 1 and req[0]["origin"] == "test_fixture"
    # The deferred request goes through the normal per-event retry path once enqueue works again.
    monkeypatch.setattr(jobs_mod, "enqueue_publish_beliefs", real)
    rep = invalidation.process_outbox(idb.worker, environment="test")
    assert len(rep.publish_jobs) == 1
    with idb.owner.connect() as c:
        assert c.execute(text("SELECT job_type FROM ops.job WHERE id = :j"),
                         {"j": rep.publish_jobs[0]}).scalar() == "publish_beliefs"
        assert c.execute(text("SELECT count(*) FROM ops.outbox WHERE event_type = 'beliefs.publish_requested' "
                              "AND processed_at IS NULL")).scalar() == 0
