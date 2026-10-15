"""Connector-repair hook-ups (architecture §10.3) through the operations layer.

* the ``connector_repair`` job type is registered and an
  ``evidence.acquisition_quarantined`` outbox event enqueues one idempotent job;
* belief publication withholds paused concepts (``source_schema_quarantine``),
  keeps them dirty, and the read model serves the last accepted release as stale;
* a resume re-enqueues the publication; a pause waits for the publication lock;
* run records carry the structured HTTP status; a persistent connector-wide
  auth failure disables the connector's ingest schedule (audited);
* releases pin their relations (``world.relation.release_id``), readable through
  ``GraphContext.release`` and ``GET /v1/relations?release_id=``;
* the operational health vector reports a promoted connector's ``connector_state``.

Offline: fixtures and synthetic derivations of the captured World Bank / Comtrade bank.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from reva.adapters.connectors.comtrade import ComtradeConnector
from reva.adapters.connectors.worldbank import WorldBankConnector
from reva.adapters.http.safe_client import request_fingerprint
from reva.adapters.postgres import repositories as repo
from reva.autonomy import repair as R
from reva.autonomy.common import stable_id
from reva.belief.publication import publish_release
from reva.evidence.runner import FixtureHttp, RunReport, post_publish_for, record_health, run_connector
from reva.kernel.context import Origin
from reva.kernel.errors import SourceResponseError, ValidationProblem
from reva.operations import health as ops_health
from reva.operations import invalidation
from reva.operations.jobs import (
    HANDLERS,
    after_run_failures,
    dirty_belief_nodes,
    enqueue_connector_repair,
    repair_idempotency_key,
    resolve_handler,
)
from reva.world import queries as wq
from reva.world.queries import GraphContext

pytestmark = pytest.mark.db

REPAIR = Path(__file__).parents[1] / "fixtures" / "repair"
CASES = {c["case"]: c for c in json.loads((REPAIR / "manifest.json").read_text())}
WB = "worldbank.wdi.indicators"
CT = "un.comtrade.public_exact"
POP = "demography.population.wdi_midyear_total"


class StubHttp:
    """Captured fixtures, drifted bodies, and provider failures raised as SafeHttpClient raises them."""

    origin = "test_fixture"

    def __init__(
        self,
        connector: str,
        *,
        overrides: dict[str, bytes] | None = None,
        status_for: Any = None,
        message: str | None = None,
    ) -> None:
        self.base = FixtureHttp(
            connector, overrides={request_fingerprint("GET", u): b for u, b in (overrides or {}).items()}
        )
        self.status_for = status_for or (lambda url: None)
        self.message = message

    async def get(self, url: str, **kw: Any) -> Any:
        st = self.status_for(url)
        if st is not None:
            raise SourceResponseError(
                self.message or f"HTTP {st} from {urlsplit(url).hostname}", retryable=False, status=st, attempts=1
            )
        return await self.base.get(url, **kw)

    async def aclose(self) -> None:
        return None


def _unique_renamed_body() -> bytes:
    """The wb_renamed_field drift with one value perturbed, so this module's acquisition is new
    (the session database may already hold the byte-identical drift from another module)."""
    body = json.loads((REPAIR / CASES["wb_renamed_field"]["file"]).read_bytes())
    body[1][0]["value"] = int(body[1][0]["value"]) + 1 + uuid.uuid4().int % 997
    return json.dumps(body).encode()


@pytest.fixture(scope="module", autouse=True)
def _cleanup(db) -> Iterator[None]:
    yield
    with db.owner.begin() as c:
        c.execute(text("DELETE FROM ops.dirty_node WHERE node_key LIKE :p"), {"p": R.PAUSE_PREFIX + "%"})
        c.execute(text("DELETE FROM ops.schedule WHERE schedule_key LIKE 'test.repair_hookup.%'"))
        # Never leave repair jobs for a later worker test to claim.
        c.execute(
            text(
                "UPDATE ops.job SET status = 'cancelled', finished_at = clock_timestamp() "
                "WHERE job_type = 'connector_repair' AND status = 'queued'"
            )
        )
    with db.api.begin() as c:
        for pid in c.execute(
            text(
                "SELECT p.id FROM ops.proposal p WHERE p.author = :a AND NOT EXISTS "
                "(SELECT 1 FROM ops.proposal_review r WHERE r.proposal_id = p.id)"
            ),
            {"a": R.AUTHOR},
        ).scalars():
            c.execute(
                text("SELECT ops.review_proposal(:p, 'reject', 'human:test-teardown', :m)"),
                {"p": pid, "m": "test proposal closed at module teardown"},
            )


def _job(db, key: str) -> dict[str, Any] | None:
    with db.owner.connect() as c:
        row = (
            c.execute(text("SELECT id, job_type, payload, status FROM ops.job WHERE idempotency_key = :k"), {"k": key})
            .mappings()
            .one_or_none()
        )
    return dict(row) if row else None


def _manifest(db, sha: str) -> dict[str, Any]:
    return json.loads(db.store.get("belief", sha))


# --------------------------------------------------------------------------- 1. registration + enqueue
def test_connector_repair_job_type_is_registered() -> None:
    assert HANDLERS["connector_repair"] == "reva.autonomy.repair:handle_connector_repair"
    assert resolve_handler("connector_repair") is R.handle_connector_repair


def test_enqueue_requires_exactly_one_trigger(db) -> None:
    with db.worker.begin() as c, pytest.raises(ValidationProblem):
        enqueue_connector_repair(c, WB, "test_fixture", created_by="test")
    k1 = repair_idempotency_key(WB, acquisition_id="a")
    assert k1 == repair_idempotency_key(WB, acquisition_id="a") != repair_idempotency_key(WB, acquisition_id="b")
    assert k1 != repair_idempotency_key(CT, acquisition_id="a")


# --------------------------------------------------------------------------- 1-3. quarantine -> pause -> release
async def test_quarantine_to_repair_to_withheld_release_to_stale_read_to_resume(db, gate_service) -> None:
    wb = WorldBankConnector()
    # A baseline release that publishes POP (and GDP) from the captured bank.
    rep0 = await run_connector(wb, FixtureHttp(WB), db.store, db.worker, persist_cursor=False)
    assert not rep0.errors
    r0 = publish_release(db.worker, db.store, origin=Origin.test_fixture, environment="test")
    with db.api.connect() as c:
        base = wq.release_estimates(c, release_id=r0.release_id, concept_id=POP)
    assert base.estimates and not any(e["stale"] for e in base.estimates)

    # Drift: the reviewed parser quarantines the renamed-field response.
    url = CASES["wb_renamed_field"]["url"]
    rep = await run_connector(
        wb, StubHttp(WB, overrides={url: _unique_renamed_body()}), db.store, db.worker, persist_cursor=False
    )
    assert {o.item_key: o.status for o in rep.outcomes}["series:SP.POP.TOTL:p1"] == "quarantined"
    with db.owner.connect() as c:
        acq = c.execute(
            text(
                "SELECT payload->>'acquisition_id' FROM ops.outbox WHERE event_type = 'evidence.acquisition_quarantined' "
                "ORDER BY id DESC LIMIT 1"
            )
        ).scalar_one()

    # The operations dispatcher turns the outbox event into one idempotent repair job.
    report = invalidation.process_outbox(db.worker, environment="test")
    key = repair_idempotency_key(WB, acquisition_id=acq)
    job = _job(db, key)
    assert job is not None and job["job_type"] == "connector_repair" and str(job["id"]) in report.repair_jobs
    assert job["payload"] == {"connector": WB, "origin": "test_fixture", "acquisition_id": acq}
    with db.worker.begin() as c:
        again = enqueue_connector_repair(c, WB, "test_fixture", acquisition_id=acq, created_by="test")
    assert again.job_id == job["id"] and not again.created

    # The handler (as the worker would run it) files the proposal and pauses POP.
    ctx = SimpleNamespace(settings=db.settings, worker_engine=db.worker, store=db.store)
    out = R.handle_connector_repair(dict(job["payload"]), ctx)
    assert out["status"] == "gate_needs_review" and out["classification"] == R.RENAME, out
    assert out["paused_concepts"] == [POP]
    proposal_id = out["proposal_id"]
    with db.owner.connect() as c:
        assert R.is_publication_paused(c, POP)
        pause_started = c.execute(
            text("SELECT updated_at FROM ops.dirty_node WHERE node_key = :k"), {"k": R.PAUSE_PREFIX + POP}
        ).scalar_one()

    # 2. Publication withholds POP (reason source_schema_quarantine) but publishes the rest.
    r1 = publish_release(db.worker, db.store, origin=Origin.test_fixture, environment="test")
    m1 = _manifest(db, r1.manifest_sha256)
    paused = [w for w in m1["withheld_estimands"] if w["reason"] == "source_schema_quarantine"]
    assert paused and {w["concept_id"] for w in paused} == {POP}
    assert all(w["details"]["proposal_id"] == proposal_id for w in paused)
    assert all(w["details"]["last_accepted_release"]["release_id"] for w in paused)
    assert m1["publication_pauses"][0]["concept_id"] == POP
    with db.owner.connect() as c:
        pop_in_r1 = c.execute(
            text(
                "SELECT count(*) FROM belief.estimate e JOIN ontology.concept_version cv ON cv.id = e.concept_version_id "
                "WHERE e.release_id = :r AND cv.concept_id = :c"
            ),
            {"r": r1.release_id, "c": POP},
        ).scalar_one()
        params = c.execute(
            text(
                "SELECT a.parameters FROM belief.release r JOIN meta.activity a "
                "ON a.id = r.provenance_activity_id WHERE r.id = :r"
            ),
            {"r": r1.release_id},
        ).scalar_one()
        # POP stays dirty for the next release, but does not count as publishable work while paused.
        reason = c.execute(
            text("SELECT reason FROM ops.dirty_node WHERE node_key = :k"), {"k": f"belief:{POP}"}
        ).scalar_one()
        dirty_while_paused = dirty_belief_nodes(c)
    assert pop_in_r1 == 0 and r1.estimates > 0
    assert params["withheld_paused"][0]["proposal_id"] == proposal_id
    assert reason.startswith("upstream_changed")
    assert dirty_while_paused == 0
    # A release with only paused estimands is refused, typed.
    with pytest.raises(ValidationProblem, match="paused"):
        publish_release(db.worker, db.store, origin=Origin.test_fixture, environment="test", concepts=[POP])

    # 3. Read model (reva_api role): POP served from the last accepted release, marked stale.
    with db.api.connect() as c:
        cur = wq.release_estimates(c, origin="test_fixture", concept_id=POP)
        other = wq.release_estimates(c, release_id=r1.release_id)
        before_pause = wq.release_estimates(
            c, as_of=pause_started - timedelta(microseconds=1), origin="test_fixture", concept_id=POP
        )
    assert cur.release["id"] == r1.release_id
    assert cur.estimates and all(
        e["stale"]
        and e["stale_reason"] == "source_schema_quarantine"
        and e["proposal_id"] == proposal_id
        and e["served_as"] == "last_accepted_release"
        for e in cur.estimates
    )
    assert {e["served_from_release_id"] for e in cur.estimates} == {r0.release_id}
    assert cur.stale_concepts[POP]["withheld_in_release"] is True
    assert any(not e["stale"] for e in other.estimates if e["concept_id"] != POP)
    # As of just before the pause, the then-current release was not stale.
    assert before_pause.release is not None and before_pause.release["sequence"] < r1.sequence
    assert before_pause.estimates and not any(e["stale"] for e in before_pause.estimates)

    # Claim comparison (read model) carries the same marks.
    est = cur.estimates[0]
    with db.api.connect() as c:
        cmp = wq.compare_claims(c, est["subject_id"], POP, est["support_id"], origin="test_fixture")
    assert cmp.estimates and cmp.estimates[0]["stale"] and cmp.estimates[0]["proposal_id"] == proposal_id
    assert any("paused" in n for n in cmp.notes)

    # Resume (named human) -> the outbox re-enqueues the publication -> POP published again.
    with db.api.begin() as c:  # the human decision is recorded through the API principal
        review = c.execute(
            text("SELECT ops.review_proposal(:p, 'approve', 'human:maintainer', 'mapping v2')"), {"p": proposal_id}
        ).scalar_one()
    assert R.resume_belief_publication(
        db.worker, POP, actor="human:maintainer", reason="mapping v2 reviewed", review_id=review
    )
    report = invalidation.process_outbox(db.worker, environment="test")
    assert report.publish_jobs, "a resumed concept must re-enqueue its publication"
    r2 = publish_release(db.worker, db.store, origin=Origin.test_fixture, environment="test")
    with db.api.connect() as c:
        now = wq.release_estimates(c, release_id=r2.release_id, concept_id=POP)
        dirty_after = c.execute(
            text("SELECT 1 FROM ops.dirty_node WHERE node_key = :k"), {"k": f"belief:{POP}"}
        ).scalar_one_or_none()
    assert now.estimates and not any(e["stale"] for e in now.estimates)
    assert {e["served_from_release_id"] for e in now.estimates} == {r2.release_id}
    assert dirty_after is None


def test_pause_waits_for_the_publication_lock(db) -> None:
    """A pause cannot commit between a release's pause read and its commit."""
    probe = f"test.repair_hookup.lock_probe.{uuid.uuid4().hex[:8]}"
    with db.worker.begin() as holder:
        repo.take_publication_lock(holder)
        with db.worker.connect() as other, pytest.raises(DBAPIError):
            other.execute(text("SET lock_timeout = '300ms'"))
            R.pause_belief_publication(
                other,
                [probe],
                connector=WB,
                classification=R.RENAME,
                proposal_id=stable_id("lock-probe"),
                origin="test_fixture",
                item_key=None,
                acquisition_id=None,
                actor=R.AUTHOR,
            )
    with db.owner.connect() as c:
        assert not R.is_publication_paused(c, probe)


# --------------------------------------------------------------------------- 4. structured status, auth
def test_run_records_carry_structured_http_status(db) -> None:
    ct = ComtradeConnector()
    spec = ct.describe()
    report = RunReport(spec.name)
    report.errors.append({"item": "x", "code": "source_response_error", "error": "denied by provider", "status": 403})
    report.errors.append({"item": "y", "code": "source_response_error", "error": "HTTP 401 from host"})  # old row
    record_health(db.worker, spec.name, spec.version, report, origin="test_fixture")
    with db.worker.connect() as c:
        runs = R.failure_history(c, spec.name, origin="test_fixture", window=1)
    sigs = {s.item_key: s for s in runs[-1].signals}
    assert sigs["x"].status == 403 and sigs["x"].kind == "http_status"  # no "HTTP nnn" in the message
    assert sigs["y"].status == 401  # message fallback for rows without the structured field
    assert R._structured_status(True) is None and R._structured_status("404") == 404


async def test_persistent_auth_failure_disables_the_ingest_schedule(db) -> None:
    ct = ComtradeConnector()
    fixture_key = f"test.repair_hookup.ct_fixture.{uuid.uuid4().hex[:6]}"
    live_key = f"test.repair_hookup.ct_live.{uuid.uuid4().hex[:6]}"
    with db.owner.begin() as c:
        for k, mode in ((fixture_key, "fixture"), (live_key, "live")):
            c.execute(
                text(
                    "INSERT INTO ops.schedule(schedule_key, job_type, parameters, cadence_spec, next_due_at) "
                    "VALUES (:k, 'ingest', CAST(:p AS jsonb), '{\"interval_seconds\": 86400}'::jsonb, "
                    "clock_timestamp() + interval '30 days')"
                ),
                {"k": k, "p": json.dumps({"connector": CT, "mode": mode})},
            )
    # Message without "HTTP nnn": only the structured status identifies the auth failure.
    http = StubHttp(CT, status_for=lambda u: 401, message="provider refused the request")
    for _ in range(R.PERSISTENT_RUNS):
        rep = await run_connector(ct, http, db.store, db.worker, persist_cursor=False)
        assert rep.errors and all(e.get("status") == 401 for e in rep.errors if e.get("code"))
    out = after_run_failures(db.worker, CT, "test_fixture", actor="worker:test-hookup", environment="test")
    assert out["streak"]["kind"] == R.AUTH and out["streak"]["connector_wide"] is True
    # (>= : this session's earlier auth-failing run records may extend the same streak)
    assert out["streak"]["runs"] >= R.PERSISTENT_RUNS and 401 in out["streak"]["statuses"]
    assert fixture_key in out["schedules_disabled"] and live_key not in out["schedules_disabled"]
    with db.owner.connect() as c:
        enabled = dict(
            c.execute(
                text("SELECT schedule_key, enabled FROM ops.schedule WHERE schedule_key IN (:a, :b)"),
                {"a": fixture_key, "b": live_key},
            ).all()
        )
        audit = (
            c.execute(
                text(
                    "SELECT actor, details FROM ops.audit WHERE action = 'schedule.disabled_auth_failure' "
                    "AND target->>'schedule_key' = :k"
                ),
                {"k": fixture_key},
            )
            .mappings()
            .all()
        )
    assert enabled == {fixture_key: False, live_key: True}
    assert len(audit) == 1 and audit[0]["actor"] == "worker:test-hookup"
    assert 401 in audit[0]["details"]["statuses"]
    # One repair job per streak (idempotent across repeated follow-ups); no second audit row.
    job = _job(
        db,
        repair_idempotency_key(CT, trigger=f"failure_streak:{R.AUTH}:{out['streak']['since']}", origin="test_fixture"),
    )
    assert job is not None and str(job["id"]) == out["repair_job_id"]
    again = after_run_failures(db.worker, CT, "test_fixture", actor="worker:test-hookup", environment="test")
    assert again["repair_job_id"] == out["repair_job_id"] and again["schedules_disabled"] == []


# --------------------------------------------------------------------------- release-pinned relations
async def test_published_release_pins_its_relations_and_the_api_serves_them(db) -> None:
    from fastapi.testclient import TestClient

    from reva.api.app import create_app
    from reva.api.auth import hash_password
    from reva.settings import Settings

    ct = ComtradeConnector()
    rep = await run_connector(
        ct,
        FixtureHttp(CT),
        db.store,
        db.worker,
        persist_cursor=False,
        post_publish=post_publish_for(ct.describe().template),
    )
    assert not rep.errors
    rel = publish_release(db.worker, db.store, origin=Origin.test_fixture, environment="test")
    with db.owner.connect() as c:
        n_rel, n_part, n_src_part = c.execute(
            text(
                """SELECT count(DISTINCT r.id), count(p.*),
                      (SELECT count(*) FROM world.relation_participant sp
                        WHERE sp.relation_id IN (SELECT CAST(component->'derived_from'->>'relation_id' AS uuid)
                                                   FROM world.relation WHERE release_id = :r))
                 FROM world.relation r LEFT JOIN world.relation_participant p ON p.relation_id = r.id
                WHERE r.release_id = :r"""
            ),
            {"r": rel.release_id},
        ).one()
        expected = c.execute(
            text(
                """SELECT count(*) FROM belief.estimate e JOIN belief.input i ON i.estimate_id = e.id
                 JOIN world.relation r ON r.evidence_observation_id = i.observation_id
                WHERE e.release_id = :r"""
            ),
            {"r": rel.release_id},
        ).scalar_one()
        sample = c.execute(
            text("SELECT r.layer, r.component FROM world.relation r WHERE r.release_id = :r LIMIT 1"),
            {"r": rel.release_id},
        ).one()
    assert n_rel > 0 and n_rel == expected and n_part == n_src_part
    assert set(sample.component["derived_from"]) == {"relation_id", "evidence_observation_id", "estimate_id"}
    assert sample.component["flow_magnitude"]["value"]["kind"] in ("decimal", "missing")

    with db.api.connect() as c:
        es = wq.extract_subgraph(c, context=GraphContext.release(rel.release_id), max_edges=500)
    assert es.edges and all(
        e.provenance.kind == "release" and e.provenance.release_id == rel.release_id for e in es.edges
    )
    assert all(e.flow_magnitude["status"] == "selected" for e in es.edges)

    password = "correct horse battery staple"
    data = db.settings.model_dump()
    data.update(
        reva_admin_password_hash=SecretStr(hash_password(password)),
        reva_session_secret=SecretStr("s" * 48),
        reva_allowed_hosts="testserver,localhost",
        reva_cors_origins="",
        reva_env="test",
    )
    app = create_app(Settings.model_validate(data))
    try:
        with TestClient(app) as client:
            r = client.post("/v1/session", json={"username": "admin", "password": password})
            assert r.status_code == 200, r.text
            r = client.get("/v1/relations", params={"release_id": str(rel.release_id), "max_edges": 100})
            assert r.status_code == 200, r.text
            body = r.json()
    finally:
        app.state.engine.dispose()
    rels = body["data"]["relations"]
    assert rels and all(x["release_id"] == str(rel.release_id) for x in rels)
    assert body["context_mode"] == "belief" and body["baseline_release"]["id"] == str(rel.release_id)


# --------------------------------------------------------------------------- health vector connector_state
def test_health_vector_reports_template_changed_as_quarantined(db, monkeypatch) -> None:
    spec = WorldBankConnector().describe()
    vec = ops_health.connector_health_vector(db.worker, spec.name, spec.cadence_seconds)
    assert vec is not None and vec["connector_state"] is None  # built-in: no promotion state
    import reva.autonomy.health as ah

    monkeypatch.setattr(ah, "connector_state", lambda conn, cv: "template_changed")
    vec = ops_health.connector_health_vector(db.worker, spec.name, spec.cadence_seconds)
    assert vec is not None and vec["connector_state"] == "template_changed" and vec["status"] == "quarantined"
