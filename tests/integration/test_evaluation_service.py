"""Evaluation service (tournament job) and prospective forecast ledger against a real database.

Offline: the World Bank WDI captured fixture is ingested through the real
connector path (``FixtureHttp``) and published as a ``test_fixture`` release.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from reva.adapters.connectors.registry import builtin_connectors
from reva.belief.publication import publish_release
from reva.evaluation import ledger
from reva.evaluation.tournament_service import GDP, LABEL, POP, REPLAY_MODE, handle_evaluation
from reva.evidence.runner import FixtureHttp, run_connector
from reva.kernel.context import Origin
from reva.kernel.errors import CapabilityGap, IntegrityFailure, MissingInput, ValidationProblem
from reva.kernel.ids import canonical_bytes
from reva.operations.context import JobContext

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def wdi_release(db):
    c = builtin_connectors()["worldbank.wdi.indicators"]
    asyncio.run(run_connector(c, FixtureHttp(c.describe().name), db.store, db.worker, persist_cursor=False))
    with db.owner.connect() as conn:
        cutoff = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
    rel = publish_release(db.worker, db.store, cutoff=cutoff, origin=Origin.test_fixture, environment="test",
                          concepts=[POP, GDP])
    return rel


def claim_job(db, payload: dict) -> JobContext:
    """Insert an evaluation job and claim it by hand (fencing token + lease)."""
    with db.owner.begin() as conn:
        jid = conn.execute(
            text("INSERT INTO ops.job(job_type, payload, idempotency_key) "
                 "VALUES ('evaluation', CAST(:p AS jsonb), :k) RETURNING id"),
            {"p": json.dumps(payload, default=str), "k": f"test-eval-{uuid.uuid4()}"},
        ).scalar_one()
        token = conn.execute(
            text("""UPDATE ops.job SET status = 'running', worker_id = 'test-worker-L',
                      lease_until = clock_timestamp() + interval '15 minutes',
                      fencing_token = nextval('ops.fencing_seq'), attempts = attempts + 1
                    WHERE id = :j RETURNING fencing_token"""),
            {"j": jid},
        ).scalar_one()
    return JobContext(job_id=jid, job_type="evaluation", worker_id="test-worker-L", fencing_token=int(token),
                      attempt=1, settings=db.settings, worker_engine=db.worker, sim_engine=db.sim, store=db.store)


def run_job(db, payload: dict) -> dict:
    ctx = claim_job(db, payload)
    t0 = time.perf_counter()
    result = handle_evaluation(payload, ctx)
    result["_wall_seconds"] = time.perf_counter() - t0
    assert ctx.completed_in_handler
    with db.owner.connect() as conn:
        assert conn.execute(text("SELECT status FROM ops.job WHERE id = :j"), {"j": ctx.job_id}).scalar_one() \
            == "succeeded"
    return result


def manifest_of(db, sha: str) -> dict:
    return ledger.decode_canonical(json.loads(db.store.get("evaluation", sha)))


def test_full_panel_stores_evaluations_promotions_and_label(db, wdi_release):
    res = run_job(db, {"release_id": str(wdi_release.release_id)})
    print(f"\nfull-panel tournament runtime {res['tournament_runtime_seconds']}s "
          f"(handler wall {res['_wall_seconds']:.2f}s); decisions {res['decisions']}; "
          f"status {res['evaluation_status']}; holdout {res['holdout']['status']}")
    assert res["label"] == LABEL and res["replay_mode"] == REPLAY_MODE
    assert res["series"] == 8  # 4 countries x (population, GDP)

    with db.owner.connect() as conn:
        ctx_row = conn.execute(text("SELECT mode, origin, parent_context_id FROM meta.context WHERE id = :c"),
                               {"c": res["context_id"]}).mappings().one()
        evals = conn.execute(
            text("""SELECT v.model_key, e.replay_mode, e.task_scope, e.metrics, e.evaluation_manifest_sha256
                    FROM model.evaluation e JOIN model.version v ON v.id = e.model_version_id
                    WHERE e.context_id = :c"""),
            {"c": res["context_id"]},
        ).mappings().all()
        promos = conn.execute(
            text("""SELECT v.model_key, p.decision, p.reviewer, p.reasons, p.policy_version, p.task_scope
                    FROM model.promotion p JOIN model.version v ON v.id = p.model_version_id
                    JOIN model.evaluation e ON e.id = p.evaluation_id WHERE e.context_id = :c"""),
            {"c": res["context_id"]},
        ).mappings().all()
        loc = conn.execute(text("SELECT namespace FROM meta.artifact_location WHERE artifact_sha256 = :s"),
                           {"s": res["manifest_sha256"]}).scalar_one()
    assert ctx_row["mode"] == "evaluation" and ctx_row["origin"] == "test_fixture"
    assert str(ctx_row["parent_context_id"]) == str(wdi_release.context_id)
    assert loc == "evaluation"

    dev = [e for e in evals if e["task_scope"]["period"] == "development"]
    assert {e["model_key"] for e in dev} == {"forecast.last_value", "forecast.log_drift", "forecast.local_level_log"}
    for e in evals:
        assert e["replay_mode"] == "revised_data_retrospective"
        assert e["metrics"]["label"] == LABEL
        assert e["evaluation_manifest_sha256"] == res["manifest_sha256"]
    naive = next(e for e in dev if e["model_key"] == "forecast.last_value")
    assert naive["metrics"]["is_naive_baseline"] is True and naive["metrics"]["n_predictions"] > 0
    assert naive["metrics"]["paired"]["mase"] is not None and naive["metrics"]["paired"]["n_common_cases"] > 0
    assert naive["metrics"]["unpaired"]["is_baseline"] is True
    assert "series_id=DZA:" + POP in naive["metrics"]["subgroups"]

    assert len(promos) == 2
    for p in promos:
        assert p["reviewer"] == "gate-evaluator:tournament"
        assert p["decision"] in ("promote", "keep_incumbent", "reject")
        assert p["reasons"][0] == LABEL and len(p["reasons"]) >= 2
        assert p["policy_version"] == "promotion-1.0.0"
        assert p["task_scope"]["task_key"] == res["task_key"]

    m = manifest_of(db, res["manifest_sha256"])
    for ref, d in m["final_decisions"].items():
        print(f"  {ref}: {d['decision']} n_pairs={d['n_pairs']} reasons={d['reasons'][1:]}")
    print("  paired dev MASE:", {k: v["mase"] for k, v in m["development"]["paired_scores"]["metrics"].items()},
          "n_common", m["development"]["paired_scores"]["n_common_cases"])
    print("  unpaired dev MASE:", {k: v["mase"] for k, v in m["development"]["scores_unpaired"].items()})
    assert m["label"] == LABEL and m["replay_mode"] == REPLAY_MODE
    assert {"DZA:" + POP, "DZA:" + GDP, "FRA:" + POP, "CHN:" + GDP, "USA:" + GDP} <= set(m["inputs"]["series"])
    assert m["inputs"]["source"]["release_id"] == str(wdi_release.release_id)
    assert m["seed"] == 20261014 and m["policy"]["policy_version"] == "promotion-1.0.0"
    assert m["policy"]["min_origin_series_pairs"] == 30
    assert m["naive_baseline"].startswith("forecast.last_value@")
    rdf = m["researcher_degrees_of_freedom"]
    assert rdf["hyperparameter_tuning_budget"] == 0 and "prior_development_runs_on_this_task" in rdf
    hold = m["locked_holdout"]
    assert hold["years"] == [2023, 2024] and hold["used_for_challenger_selection"] is False
    assert m["holdout_usage_count"] == hold["access_count_after"]
    # The challenger selection never saw the holdout years.
    preds = m["development"]["predictions"]
    assert preds and max(p["target_year"] for p in preds) <= 2022
    assert m["development"]["targets"] and m["development"]["scores_unpaired"]["forecast.last_value"]["is_baseline"]
    paired = m["development"]["paired_scores"]
    assert set(paired["metrics"]) == {"forecast.last_value", "forecast.log_drift", "forecast.local_level_log"}
    assert 0 < paired["n_common_cases"] <= min(v["n_scaled"] for v in m["development"]["scores_unpaired"].values())
    assert m["development"]["ineligible"]  # local level needs >= 6 points: early origins are ineligible
    assert m["execution"]["tournament_runtime_seconds"] > 0
    if hold["status"] == "scored":
        for ref in hold["scores"]:
            assert all(p["target_year"] in (2023, 2024) for p in hold["scores"][ref]["predictions"])


def test_insufficient_evidence_keeps_incumbent(db, wdi_release):
    res = run_job(db, {"release_id": str(wdi_release.release_id), "countries": ["DZA"], "concepts": [POP],
                       "years": [2015, 2024]})
    assert res["evaluation_status"] == "insufficient_evaluation"
    assert set(res["decisions"].values()) == {"keep_incumbent"}
    assert res["incumbent"].startswith("forecast.last_value@")
    assert res["holdout"]["status"] == "locked_unopened"  # the holdout is not spent on a non-candidate
    m = manifest_of(db, res["manifest_sha256"])
    for d in m["final_decisions"].values():
        assert any(r.startswith("insufficient_evaluation") for r in d["reasons"])
    assert m["holdout_usage_count"] == {k: 0 for k in m["holdout_usage_count"]}
    with db.owner.connect() as conn:
        n_hold = conn.execute(
            text("SELECT count(*) FROM model.evaluation WHERE context_id = :c "
                 "AND task_scope->>'period' = 'locked_holdout'"), {"c": res["context_id"]}).scalar_one()
    assert n_hold == 0


def test_locked_holdout_reuse_is_blocked(db, wdi_release):
    payload = {"release_id": str(wdi_release.release_id), "countries": ["FRA", "USA"], "years": [2000, 2020],
               "holdout_mode": "final_report"}
    first = run_job(db, payload)
    assert first["holdout"]["status"] == "scored"
    assert first["holdout"]["years"] == [2019, 2020]
    assert set(first["holdout"]["access_count"].values()) == {1}
    assert len(first["holdout_evaluation_ids"]) == 3

    second = run_job(db, payload)
    assert second["holdout"]["status"] == "blocked"
    assert second["holdout_evaluation_ids"] == {}
    assert set(second["holdout"]["blocked"].values()) == {1}  # each model version already used it once
    m = manifest_of(db, second["manifest_sha256"])
    assert m["locked_holdout"]["access_count_before"] == {k: 1 for k in m["locked_holdout"]["access_count_before"]}
    assert m["holdout_usage_count"] == m["locked_holdout"]["access_count_before"]  # unchanged: nothing scored
    assert m["researcher_degrees_of_freedom"]["prior_development_runs_on_this_task"] == 1
    for d in m["final_decisions"].values():
        assert d["decision"] != "promote"
        if d["development_decision"] == "promote":
            assert any(r.startswith("locked_holdout_reuse_blocked") for r in d["reasons"])
    with db.owner.connect() as conn:
        audits = conn.execute(
            text("SELECT count(*) FROM ops.audit WHERE action = 'evaluation.holdout_reuse_blocked' "
                 "AND details->>'manifest_sha256' = :s"), {"s": second["manifest_sha256"]}).scalar_one()
    assert audits == 1


def test_observation_source_and_payload_validation(db, wdi_release):
    res = run_job(db, {"source": "observations", "origin": "test_fixture", "countries": ["DZA", "CHN"],
                       "concepts": [GDP], "years": [2000, 2024]})
    m = manifest_of(db, res["manifest_sha256"])
    assert m["inputs"]["source"]["kind"] == "observations_latest_revision"
    assert set(m["inputs"]["series"]) == {"DZA:" + GDP, "CHN:" + GDP}
    assert m["label"] == LABEL

    ctx = claim_job(db, {})
    with pytest.raises(ValidationProblem):
        handle_evaluation({"policy_override": {"min_origin_series_pairs": 1}}, ctx)
    with pytest.raises(CapabilityGap):
        handle_evaluation({"concepts": ["trade.hs2709.import_value.world_total"]}, ctx)
    with pytest.raises(MissingInput):
        handle_evaluation({"release_id": str(wdi_release.release_id), "countries": ["ZZZ"]}, ctx)


# --------------------------------------------------------------------------- prospective ledger


def test_ledger_freezes_and_refuses_to_score_before_outcomes(db, wdi_release):
    issued = ledger.issue_forecasts(db.worker, db.store, model_key="forecast.log_drift", environment="test",
                                    horizons=(1, 2), release_id=wdi_release.release_id,
                                    countries=["DZA", "FRA"])
    assert issued.target_years == (2025, 2026)
    assert issued.n_forecasts == 8  # 2 countries x 2 concepts x 2 horizons
    frozen = ledger.load_frozen_manifest(db.store, issued.manifest_sha256)
    assert ledger.manifest_sha256(frozen) == issued.manifest_sha256
    assert frozen["label"] == ledger.LEDGER_LABEL and frozen["model"] == "forecast.log_drift@1.0.0"
    with db.owner.connect() as conn:
        row = conn.execute(text("SELECT replay_mode, metrics, context_id FROM model.evaluation WHERE id = :i"),
                           {"i": issued.evaluation_id}).mappings().one()
        mode = conn.execute(text("SELECT mode FROM meta.context WHERE id = :c"),
                            {"c": row["context_id"]}).scalar_one()
    assert mode == "evaluation"
    assert row["replay_mode"] == "operational_as_known"
    assert row["metrics"]["kind"] == "prospective_issue"
    assert row["metrics"]["issue_manifest_sha256"] == issued.manifest_sha256

    # No 2025/2026 outcome exists yet: scoring is refused and nothing is written.
    with db.owner.connect() as conn:
        before = conn.execute(text("SELECT count(*) FROM model.evaluation")).scalar_one()
    with pytest.raises(MissingInput):
        ledger.score_issued_forecasts(db.worker, db.store, issued.evaluation_id)
    with db.owner.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM model.evaluation")).scalar_one() == before

    # Once outcomes are recorded later, the frozen forecasts are scored (pure scorer).
    sid = "DZA:" + POP
    later = issued.issued_at + timedelta(days=400)
    out = {(sid, 2025): ledger.Outcome(Decimal("47000000"), recorded_at=later)}
    scored = ledger.score_issue(frozen, out, scored_at=later + timedelta(seconds=1),
                                issue_sha256=issued.manifest_sha256)
    assert scored["summary"]["n_scored"] == 1 and scored["summary"]["n_pending"] == 7
    assert scored["scores"][0]["covered"] in (True, False)
    # An outcome REVA already held at issue time is not a prospective score.
    stale = {(sid, 2025): ledger.Outcome(Decimal("47000000"), recorded_at=issued.issued_at)}
    with pytest.raises(MissingInput):
        ledger.score_issue(frozen, stale, scored_at=later, issue_sha256=issued.manifest_sha256)
    with pytest.raises(ValidationProblem):
        ledger.score_issue(frozen, out, scored_at=issued.issued_at, issue_sha256=issued.manifest_sha256)


def test_ledger_tamper_detection_and_envelope(db, wdi_release):
    issued = ledger.issue_forecasts(db.worker, db.store, model_key="forecast.last_value", environment="test",
                                    horizons=(1,), release_id=wdi_release.release_id, countries=["CHN"])
    frozen = ledger.load_frozen_manifest(db.store, issued.manifest_sha256)
    tampered = dict(frozen, issued_at="1999-01-01T00:00:00+00:00")
    assert ledger.manifest_sha256(tampered) != issued.manifest_sha256

    class TamperedStore:
        def get(self, namespace: str, sha256: str) -> bytes:
            return canonical_bytes(tampered)

    with pytest.raises(IntegrityFailure):
        ledger.load_frozen_manifest(TamperedStore(), issued.manifest_sha256)  # type: ignore[arg-type]
    # Horizons beyond the evaluated envelope (max 3 years) are refused, not extrapolated.
    with pytest.raises(CapabilityGap):
        ledger.issue_forecasts(db.worker, db.store, model_key="forecast.log_drift", environment="test",
                               horizons=(5,), release_id=wdi_release.release_id, countries=["FRA"])
