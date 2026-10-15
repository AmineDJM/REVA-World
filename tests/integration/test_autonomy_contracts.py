"""Contract tests for the V1 autonomy implementations (one per protocol not covered elsewhere)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from reva.autonomy.common import ArtifactIO, default_budget, make_context
from reva.ports import autonomy as ports

pytestmark = pytest.mark.db
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


@pytest.fixture()
def io(db) -> ArtifactIO:
    return ArtifactIO(db.store)


def _ctx(io, mode="evidence", cutoff=None):
    return make_context(io, mode, cutoff or datetime.now(UTC) + timedelta(seconds=1), {"t": 1})


def _gap(io, **over) -> ports.KnowledgeGap:
    base = dict(gap_id=uuid.uuid4(), requirement_id="R-POP-JAN-AGESEX",
                concept_version="demography.population.jan1_age_sex@1.0.0", geography=("FRA",),
                temporal_support={"years": [2023]}, reason="unsupported", priority=1.0, priority_method="t",
                context=_ctx(io))
    base.update(over)
    return ports.KnowledgeGap(**base)


def test_protocol_conformance():
    """Every normative protocol has a concrete class with the protocol's methods."""
    from reva.autonomy import (
        challenger,
        discovery,
        evidence,
        gaps,
        gate,
        generator,
        health,
        mapping,
        models,
        quality,
        sandbox,
        validator,
        voi,
    )

    impl = {
        ports.SourceDiscoverer: discovery.EurostatCatalogueDiscoverer,
        ports.OntologyMapper: mapping.RegistryOntologyMapper,
        ports.ConnectorGenerator: generator.DeclarativeConnectorGenerator,
        ports.ConnectorValidator: validator.JsonStatConnectorValidator,
        ports.ConnectorHealthMonitor: health.RunHistoryHealthMonitor,
        ports.EvidenceExtractor: evidence.StructuredCardImporter,
        ports.DataQualityEvaluator: quality.DimensionRuleQualityEvaluator,
        ports.ModelRegistry: models.PostgresModelRegistry,
        ports.ModelEvaluator: models.TournamentModelEvaluator,
        ports.BacktestRunner: models.TournamentBacktestRunner,
        ports.KnowledgeGapDetector: gaps.CatalogueGapDetector,
        ports.ValueOfInformationEstimator: voi.GapValueEstimator,
        ports.ModelChallenger: challenger.BoundedModelChallenger,
        ports.SandboxRunner: sandbox.TrustedTemplateSandbox,
        ports.PromotionGate: gate.PolicyPromotionGate,
    }
    import inspect

    for proto, cls in impl.items():
        for name, member in inspect.getmembers(proto, inspect.isfunction):
            if name.startswith("_"):
                continue
            ours = getattr(cls, name)
            assert inspect.iscoroutinefunction(ours) == inspect.iscoroutinefunction(member), (cls, name)
            assert list(inspect.signature(ours).parameters) == list(inspect.signature(member).parameters), (cls, name)


# --------------------------------------------------------------------------- VOI
def test_voi_without_decision_spec_is_uncertainty_reduction_proxy(io):
    from reva.autonomy.voi import GapValueEstimator

    gap = _gap(io, priority=1.0)
    plan = io.put_json({"expected_coverage_gain": 1.0}, "evaluation")
    v = GapValueEstimator(io).estimate(gap, plan, None, default_budget())
    assert v.kind == "uncertainty_reduction_proxy"
    assert v.unit == "dimensionless_proxy" and v.unit not in ("USD", "EUR")
    assert float(v.value) == pytest.approx(0.5)  # relevance 1/2 x full sd reduction
    a = io.get_json(v.assumptions)
    assert a["not_money"] is True and "default_rule" in a
    # no inputs at all -> unsupported, never a made-up number
    v2 = GapValueEstimator(io).estimate(gap, io.put_json({}, "evaluation"), None, default_budget())
    assert v2.kind == "unsupported" and v2.value is None


def test_voi_with_decision_spec_is_exact_evsi(io):
    from reva.autonomy.voi import GapValueEstimator
    from reva.evaluation.voi import DecisionProblem, evsi_exact

    spec = {"prior": 0.3, "utility": [[0, 0], [-10, 40]], "sensitivity": 0.9, "specificity": 0.8, "cost": 1.0,
            "utility_unit": "utility"}
    v = GapValueEstimator(io).estimate(_gap(io), io.put_json({}, "evaluation"), io.put_json(spec, "evaluation"),
                                       default_budget())
    expected = evsi_exact(DecisionProblem(prior=0.3, utility=((0, 0), (-10, 40)), sensitivity=0.9,
                                          specificity=0.8, cost=1.0))
    assert v.kind == "decision_evsi" and float(v.value) == pytest.approx(expected) and v.unit == "utility"
    bad = GapValueEstimator(io).estimate(_gap(io), io.put_json({}, "evaluation"),
                                         io.put_json({"prior": 2}, "evaluation"), default_budget())
    assert bad.kind == "unsupported"


# --------------------------------------------------------------------------- gaps
def test_gap_classification_rules(io):
    from reva.autonomy.gaps import CatalogueGapDetector
    from reva.ontology.registry import load_registry

    reqs = load_registry().requirements
    snap = {"schema": "reva-coverage-snapshot/1", "eligible": {
        "trade.hs2709.import_value.world_total": ["FRA", "DZA", "CHN", "USA"],
        "demography.population.wdi_midyear_total": ["FRA"]},
        "cells": [
            *[{"concept_id": "trade.hs2709.import_value.world_total", "geo": "DZA", "year": y, "status": "missing",
               "missing_reasons": ["unavailable"], "latest_recorded_at": None} for y in range(2020, 2025)],
            *[{"concept_id": "trade.hs2709.import_value.world_total", "geo": "FRA", "year": y, "status": "value",
               "missing_reasons": [], "latest_recorded_at": (NOW - timedelta(days=10)).isoformat()}
              for y in range(2020, 2025)],
            *[{"concept_id": "demography.population.wdi_midyear_total", "geo": "FRA", "year": y,
               "status": "value", "missing_reasons": [],
               "latest_recorded_at": (NOW - timedelta(days=1000)).isoformat()} for y in range(2000, 2025)]]}
    gaps = CatalogueGapDetector(io).detect(io.put_json(reqs, "models"), io.put_json(snap, "evaluation"),
                                           _ctx(io, cutoff=NOW))
    by = {(g.requirement_id, g.geography[0]): g for g in gaps}
    dza = by[("R-CRUDE-M", "DZA")]
    assert dza.reason.startswith("missing") and dza.temporal_support["years"] == [2020, 2021, 2022, 2023, 2024]
    assert "typed Missing" in dza.reason
    assert ("R-CRUDE-M", "FRA") not in by
    assert by[("R-POP-JAN-AGESEX", "FRA")].reason.startswith("unsupported")
    assert by[("R-POP-WDI", "FRA")].reason.startswith("stale")
    assert by[("R-POP-WDI", "DZA")].reason.startswith("unsupported")
    assert all(g.priority_method.startswith("requirement_weight") for g in gaps)


async def test_gap_detector_against_database_finds_dza_crude_missing(db, io):
    from reva.adapters.connectors.registry import builtin_connectors
    from reva.autonomy.gaps import CatalogueGapDetector, build_coverage_snapshot
    from reva.evidence.runner import FixtureHttp, run_connector
    from reva.ontology.registry import load_registry

    c = builtin_connectors()["un.comtrade.public_exact"]
    await run_connector(c, FixtureHttp(c.describe().name), db.store, db.worker, persist_cursor=False)
    cutoff = datetime.now(UTC) + timedelta(seconds=1)
    with db.worker.connect() as conn:
        snap = build_coverage_snapshot(conn, cutoff, ["test_fixture"])
    gaps = CatalogueGapDetector(io).detect(io.put_json(load_registry().requirements, "models"),
                                           io.put_json(snap, "evaluation"), _ctx(io, cutoff=cutoff))
    by = {(g.requirement_id, g.geography[0]): g for g in gaps}
    # expected from the captured provider bytes themselves: DZA world-total rows absent
    from reva.evidence.runner import FIXTURE_ROOT

    d = FIXTURE_ROOT / "un.comtrade.public_exact"
    empty: dict[str, list[int]] = {"M": [], "X": []}
    for e in json.loads((d / "manifest.json").read_text()):
        u = e["url"]
        if "reporterCode=12&" in u and "partnerCode=0" in u:
            year = int(u.split("period=")[1].split("&")[0])
            if not json.loads((d / e["file"]).read_text())["data"]:
                empty["M" if "flowCode=M" in u else "X"].append(year)
    assert len(empty["M"]) >= 4 and len(empty["X"]) >= 4
    for rid, flow in (("R-CRUDE-M", "M"), ("R-CRUDE-X", "X")):
        g = by[(rid, "DZA")]
        assert g.reason.startswith("missing") and "typed Missing" in g.reason
        assert g.temporal_support["years"] == sorted(empty[flow])
        assert (rid, "FRA") not in by  # France 2020-2024 exist in the captured preview


def test_handle_gaps_writes_report_and_outbox(db, io):
    from reva.autonomy.jobs import handle_gaps
    from reva.operations.context import JobContext

    ctx = JobContext(job_id=None, job_type="gaps", worker_id="t", fencing_token=0, attempt=1,  # type: ignore[arg-type]
                     settings=db.settings, worker_engine=db.worker, sim_engine=None, store=db.store)
    out = handle_gaps({"origin": "test_fixture"}, ctx)
    assert out["status"] == "ok" and out["gaps"] > 0
    doc = io.get_json(ports.ArtifactRef(out["artifact_sha256"], "evaluation", "application/json"))
    assert doc["coverage"]["statement"].startswith("coverage of this finite requirement catalogue")
    assert all(g["value_estimate"]["kind"] in ("uncertainty_reduction_proxy", "unsupported") for g in doc["gaps"])
    with db.owner.connect() as conn:
        ev = conn.execute(text("SELECT payload FROM ops.outbox WHERE event_type='autonomy.gap_report' "
                               "ORDER BY id DESC LIMIT 1")).scalar_one()
        assert ev["artifact_sha256"] == out["artifact_sha256"]
        assert conn.execute(text("SELECT 1 FROM meta.artifact WHERE sha256=:s"),
                            {"s": out["artifact_sha256"]}).scalar_one() == 1


# --------------------------------------------------------------------------- health
def _runs(n=20, fail=0, fp="a", last_fp=None, update="2026-03-01T00:00:00+00:00", revisions=1):
    runs = []
    for i in range(n):
        runs.append({"started_at": (NOW - timedelta(days=7 * (n - i))).isoformat(),
                     "outcome": "failed" if i < fail else "success", "latency_seconds": 1.0 + i / 10,
                     "schema_fingerprint": last_fp if (last_fp and i == n - 1) else fp,
                     "expected_count": 309, "received_count": 309, "revisions": revisions,
                     "source_last_update": update, "raw_sha256": "x" * 64})
    return {"runs": runs}


def test_health_vector(io):
    from reva.autonomy.health import RunHistoryHealthMonitor

    mon = RunHistoryHealthMonitor(io)
    sched = {"cadence_seconds": 365 * 86400, "grace_seconds": 30 * 86400}
    h = mon.assess(uuid.uuid4(), io.put_json(_runs(30, fail=0), "evaluation"), sched, NOW)
    assert h.state == "healthy" and h.dimensions["window_runs"] == 20
    assert h.dimensions["freshness"]["state"] == "on_schedule"
    assert h.dimensions["freshness"]["basis"] == "provider_cadence"
    # 3 failures among the last 20 scheduled runs
    h2 = mon.assess(uuid.uuid4(), io.put_json(_runs(20, fail=3), "evaluation"), sched, NOW)
    assert h2.dimensions["request_success"]["ratio"] == pytest.approx(17 / 20) and h2.state == "degraded"
    # provider release overdue relative to its own cadence (not days since our ingestion)
    h3 = mon.assess(uuid.uuid4(), io.put_json(_runs(update="2024-01-01T00:00:00+00:00"), "evaluation"), sched, NOW)
    assert h3.dimensions["freshness"]["state"] == "overdue" and h3.state == "degraded"
    h4 = mon.assess(uuid.uuid4(), io.put_json(_runs(last_fp="b"), "evaluation"), sched, NOW)
    assert h4.dimensions["semantic_drift"]["drift"] and h4.state == "quarantined"
    assert mon.assess(uuid.uuid4(), io.put_json({"runs": []}, "evaluation"), sched, NOW).state == "unknown"


def test_schema_fingerprint_ignores_key_order_but_not_dimension_order():
    from reva.adapters.connectors.eurostat_template import structure_fingerprint

    doc = {"version": "2.0", "id": ["sex", "age"], "size": [1, 1], "label": "x",
           "dimension": {"sex": {"category": {"index": {"T": 0}}}, "age": {"category": {"index": {"Y1": 0}}}}}
    reordered = json.loads(json.dumps(dict(reversed(list(doc.items())))))
    assert structure_fingerprint(doc) == structure_fingerprint(reordered)
    swapped = {**doc, "id": ["age", "sex"]}
    assert structure_fingerprint(doc) != structure_fingerprint(swapped)


# --------------------------------------------------------------------------- data quality
def _batch(io):
    from reva.adapters.connectors.eurostat_template import EurostatJsonStatConnector
    from reva.autonomy.common import run_sync
    from reva.autonomy.fixtures import BankHttp, bank_from_dir
    from reva.ports.connectors import FetchBudget

    bank = bank_from_dir(io, "eurostat.demo_pjan.fr")
    manifest = _manifest(io, bank)
    conn = EurostatJsonStatConnector(manifest)
    item = conn.plan(None, FetchBudget()).items[0]
    raw = run_sync(conn.fetch(item, BankHttp(io, bank)))
    return manifest, next(conn.parse(raw))


def _manifest(io, bank):
    from reva.autonomy.common import run_sync
    from reva.autonomy.discovery import EurostatCatalogueDiscoverer
    from reva.autonomy.fixtures import BankHttp
    from reva.autonomy.generator import DeclarativeConnectorGenerator
    from reva.autonomy.mapping import RegistryOntologyMapper

    gap = _gap(io)
    cands = run_sync(EurostatCatalogueDiscoverer(io, BankHttp(io, bank)).discover(gap, default_budget()))
    m = RegistryOntologyMapper(io).assess(cands[0], gap.context)[0]
    return io.get_json(DeclarativeConnectorGenerator(io).generate(cands[0], m, ("eurostat_jsonstat",)).manifest)


def test_data_quality_dimension_rules(io):
    from reva.autonomy.quality import DimensionRuleQualityEvaluator, contract_for

    manifest, batch = _batch(io)
    b = io.put_json(batch.model_dump(mode="json"), "normalised")
    contract = contract_for(manifest)
    ok = DimensionRuleQualityEvaluator(io).evaluate(b, io.put_json(contract, "models"))
    assert ok.outcome == "pass", ok.findings
    detail = io.get_json(ok.evidence_manifest)["detail"]
    assert detail["cell_types"] == {"leaf": 204, "subtotal": 104, "total": 1}
    assert detail["dimensions"]["age"]["categories_seen"] == 103
    wrong = {**contract, "unit_ref": "unit.thousand_persons", "temporal_support": "point:07-01"}
    bad = DimensionRuleQualityEvaluator(io).evaluate(b, io.put_json(wrong, "models"))
    codes = {f.code for f in bad.findings if f.severity == "blocker"}
    assert bad.outcome == "fail" and {"unit_mismatch", "temporal_support_mismatch"} <= codes


# --------------------------------------------------------------------------- models
def _model_manifest(io, key):
    return io.put_json({"model_key": key, "version": "9.0.0", "kind": "forecast", "input_ports": [],
                        "output_ports": [{"name": "point_forecast"}], "assumptions": ["test"],
                        "applicability": {"concepts": "*"}}, "models")


def _model_proposal(db, io, spec_sha: str) -> ports.PlatformChangeProposal:
    from reva.autonomy.policy import promotion_policy
    from reva.autonomy.proposals import ProposalStore, base_commit

    change = io.put_json({"schema": "reva-model-registration/1", "model_spec_sha256": spec_sha}, "models")
    p = ports.PlatformChangeProposal(uuid.uuid4(), "agent:model-challenger", 2, (), base_commit(), change,
                                     ("new_model",), True, False,
                                     ports.ValueEstimate("unsupported", None, None, {}, change, "t"),
                                     promotion_policy().version)
    ProposalStore(db.worker, io).persist(p, proposal_type="model_registration", scope={}, risk={})
    return p


def _review(db, proposal_id, decision="approve", reviewer="human:maintainer"):
    with db.api.begin() as conn:  # reviews are written only through ops.review_proposal (reva_api)
        return conn.execute(text("SELECT ops.review_proposal(:p, :d, :r, 'test')"),
                            {"p": proposal_id, "d": decision, "r": reviewer}).scalar_one()


def test_model_registry_requires_approval_and_is_immutable(db, io):
    from reva.autonomy.gate import PolicyPromotionGate
    from reva.autonomy.models import PostgresModelRegistry
    from reva.autonomy.policy import promotion_policy
    from reva.kernel.errors import ImmutableRecordViolation, PolicyDenied

    reg = PostgresModelRegistry(db.worker, io)
    key = f"forecast.test_{uuid.uuid4().hex[:8]}"
    man = _model_manifest(io, key)
    desc = ports.ModelDescriptor(uuid.uuid4(), key, "9.0.0", man, {"concepts": "*"})
    p = _model_proposal(db, io, man.sha256)
    review = PolicyPromotionGate(db.gate, io).decide(p, [], promotion_policy().ref())
    assert review.outcome == "needs_review"  # new_model is T2
    with pytest.raises(PolicyDenied):
        reg.register(desc, review)
    # (R4-05) fabricated decisions are refused: no recorded approval behind them
    for who in ("human:maintainer", "human:nobody", "gate-evaluator:promotion"):
        fake = ports.GateDecision(uuid.uuid4(), "promote", p.proposal_id, "made-up", (), ("trust me",), who, None)
        with pytest.raises(PolicyDenied):
            reg.register(desc, fake)
    with pytest.raises(PolicyDenied):
        reg.register(desc, ports.GateDecision(uuid.uuid4(), "promote", uuid.uuid4(), "p", (), (), "agent:x", None))
    # a recorded human approval of a proposal that names this spec is accepted
    rid = _review(db, p.proposal_id)
    approved = ports.GateDecision(rid, "promote", p.proposal_id, promotion_policy().version, (),
                                  ("named review",), "human:maintainer", None)
    with pytest.raises(PolicyDenied):  # right reviewer recorded, but another person claims it
        reg.register(desc, ports.GateDecision(rid, "promote", p.proposal_id, "x", (), (), "human:other", None))
    got = reg.register(desc, approved)
    assert reg.register(desc, approved).model_version_id == got.model_version_id  # idempotent
    # an approval for a proposal naming another spec does not cover this one
    other_key = f"forecast.test_{uuid.uuid4().hex[:8]}"
    other_desc = ports.ModelDescriptor(uuid.uuid4(), other_key, "9.0.0", _model_manifest(io, other_key), {})
    with pytest.raises(PolicyDenied):
        reg.register(other_desc, approved)
    # a T0 gate pass is never a model approval
    from reva.autonomy.proposals import ProposalStore, base_commit

    t0 = ports.PlatformChangeProposal(uuid.uuid4(), "agent:test", 0, (), base_commit(),
                                      io.put_json({"model_spec_sha256": man.sha256}, "models"), ("report",), False,
                                      False, ports.ValueEstimate("unsupported", None, None, {}, man, "t"),
                                      promotion_policy().version)
    ProposalStore(db.worker, io).persist(t0, proposal_type="t", scope={}, risk={})
    t0_pass = PolicyPromotionGate(db.gate, io).decide(t0, [], promotion_policy().ref())
    assert t0_pass.outcome == "promote"
    key2 = f"forecast.test_{uuid.uuid4().hex[:8]}"
    with pytest.raises(PolicyDenied):
        reg.register(ports.ModelDescriptor(uuid.uuid4(), key2, "9.0.0", man, {}), t0_pass)
    different = io.put_json({"different": True, "kind": "forecast", "input_ports": [], "output_ports": [],
                             "assumptions": []}, "models")
    p2 = _model_proposal(db, io, different.sha256)
    rid2 = _review(db, p2.proposal_id)
    with pytest.raises(ImmutableRecordViolation):
        reg.register(ports.ModelDescriptor(uuid.uuid4(), key, "9.0.0", different, {}),
                     ports.GateDecision(rid2, "promote", p2.proposal_id, "x", (), (), "human:maintainer", None))
    eligible = reg.eligible({"kind": "forecast"}, _ctx(io, cutoff=datetime.now(UTC) + timedelta(seconds=5)))
    assert key in {d.model_key for d in eligible} and "forecast.last_value" in {d.model_key for d in eligible}
    past = reg.eligible({"kind": "forecast"}, _ctx(io, cutoff=datetime(2000, 1, 1, tzinfo=UTC)))
    assert past == []


def test_model_evaluator_proper_metrics(io):
    from reva.autonomy.models import TournamentModelEvaluator

    # >= 3 consecutive training years are needed for a defined MASE scale (here 4: 1998-2001, scale 2)
    targets = io.put_json({"series": {"a": [[1998, 6], [1999, 8], [2000, 10], [2001, 12], [2002, 14], [2003, 16]]}},
                          "evaluation")
    preds = io.put_json({"records": [
        {"series_id": "a", "origin": 2001, "horizon": 1, "target_year": 2002, "point": 12,
         "interval": [11, 15], "interval_kind": "prediction_interval"},
        {"series_id": "a", "origin": 2001, "horizon": 2, "target_year": 2003, "point": 12, "interval": None},
    ]}, "evaluation")
    model = ports.ModelDescriptor(uuid.uuid4(), "forecast.last_value", "1.0.0",
                                  ports.ArtifactRef("0" * 64, "models", "application/json"), {})
    spec = ports.EvaluationSpec(uuid.uuid4(), io.put_json({}, "models"), "revised_data_retrospective",
                                _ctx(io, "evaluation"))
    r = TournamentModelEvaluator(io).evaluate(model, preds, targets, spec)
    m = r.metrics["overall"]
    assert m["mae"] == pytest.approx(3.0) and m["mase"] == pytest.approx(1.5) and m["coverage"] == 1.0
    assert set(r.metrics["subgroups"]["horizon"]) == {"1", "2"} and m["n_undefined_scale"] == 0
    short = io.put_json({"series": {"a": [[2000, 10], [2001, 12], [2002, 14], [2003, 16]]}}, "evaluation")
    m2 = TournamentModelEvaluator(io).evaluate(model, preds, short, spec).metrics["overall"]
    assert m2["mase"] is None and m2["n_undefined_scale"] == 2 and m2["mae"] == pytest.approx(3.0)
    leaky = io.put_json({"records": [{"series_id": "a", "origin": 2003, "horizon": 1, "target_year": 2003,
                                      "point": 1}]}, "evaluation")
    r2 = TournamentModelEvaluator(io).evaluate(model, leaky, targets, spec)
    assert "future_information" in {f.code for f in r2.findings}


async def test_backtest_runner_adapts_tournament_and_blocks_unsupported_modes(io):
    from reva.autonomy.models import TournamentBacktestRunner

    panel = {f"s{i}": [[2000 + t, 100.0 * (1.02 + 0.01 * i) ** t] for t in range(15)] for i in range(3)}
    spec_ref = io.put_json({"panel": panel, "horizons": [1, 2], "seed": 7, "incumbent": "forecast.last_value"},
                           "models")
    model = ports.ModelDescriptor(uuid.uuid4(), "forecast.log_drift", "1.0.0",
                                  ports.ArtifactRef("0" * 64, "models", "application/json"), {})
    ok = await TournamentBacktestRunner(io).run(
        model, ports.EvaluationSpec(uuid.uuid4(), spec_ref, "revised_data_retrospective", _ctx(io, "evaluation")),
        default_budget())
    assert ok.metrics["n_predictions"] > 0 and ok.metrics["mase"] is not None
    assert ok.metrics["decision"]["decision"] in ("promote", "keep_incumbent")
    blocked = await TournamentBacktestRunner(io).run(
        model, ports.EvaluationSpec(uuid.uuid4(), spec_ref, "operational_as_known", _ctx(io, "evaluation")),
        default_budget())
    assert blocked.metrics["status"] == "blocked"
    assert {f.code for f in blocked.findings} == {"unsupported_replay_mode"}


def test_challenger_proposals_are_bounded_t2_and_need_review(db, io):
    from reva.autonomy.challenger import BoundedModelChallenger
    from reva.autonomy.gate import PolicyPromotionGate
    from reva.autonomy.policy import promotion_policy
    from reva.autonomy.proposals import ProposalStore

    inc = ports.ModelDescriptor(uuid.uuid4(), "forecast.last_value", "1.0.0",
                                ports.ArtifactRef("0" * 64, "models", "application/json"), {})
    task = {"concept": "demography.population.wdi_midyear_total", "holdout_id": f"h-{uuid.uuid4()}"}
    props = BoundedModelChallenger(io).propose(task, inc, _ctx(io), default_budget())
    assert {io.get_json(p.patch_or_manifest)["challenger"] for p in props} == {"forecast.log_drift",
                                                                                "forecast.local_level_log"}
    assert all(p.permission_tier == 2 and p.author == "agent:model-challenger" for p in props)
    tested = {**task, "tested": [["forecast.log_drift", "forecast.last_value", task["holdout_id"]]]}
    again = BoundedModelChallenger(io).propose(tested, inc, _ctx(io), default_budget())
    assert [io.get_json(p.patch_or_manifest)["challenger"] for p in again] == ["forecast.local_level_log"]
    ProposalStore(db.worker, io).persist(props[0], proposal_type="model_challenge", scope=task, risk={})
    d = PolicyPromotionGate(db.gate, io).decide(props[0], [], promotion_policy().ref())
    assert d.outcome == "needs_review"


async def test_health_from_recorded_runs(db, io):
    from reva.adapters.connectors.registry import builtin_connectors
    from reva.autonomy.health import RunHistoryHealthMonitor, run_history_from_db
    from reva.evidence.runner import FixtureHttp, run_connector

    c = builtin_connectors()["worldbank.wdi.indicators"]
    await run_connector(c, FixtureHttp(c.describe().name), db.store, db.worker, persist_cursor=False)
    with db.worker.connect() as conn:
        cv = conn.execute(text("SELECT id FROM evidence.connector_version WHERE name = :n AND version = :v"),
                          {"n": c.describe().name, "v": c.describe().version}).scalar_one()
        hist = run_history_from_db(conn, cv)
    assert hist["runs"]
    h = RunHistoryHealthMonitor(io).assess(cv, io.put_json(hist, "evaluation"),
                                           {"cadence_seconds": 30 * 86400}, datetime.now(UTC))
    assert h.state in ("healthy", "degraded", "quarantined", "unknown")
    assert set(h.dimensions) >= {"request_success", "freshness", "semantic_drift", "completeness",
                                 "revision_anomalies", "provenance", "latency"}
