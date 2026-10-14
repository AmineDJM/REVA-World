"""ModelRegistry, ModelEvaluator and BacktestRunner V1.

* ``PostgresModelRegistry`` wraps the immutable ``model.version`` table (append
  only; an existing key/version with different content is refused). Registration
  requires a ``promote`` decision from a gate evaluator or a named human.
* ``TournamentModelEvaluator`` computes proper metrics (MAE, MASE with a
  training-window scale, prediction-interval coverage) overall and by subgroup
  (series, horizon), with resource counts, using agent B's ``reva.models.metrics``.
* ``TournamentBacktestRunner`` is a thin adapter over agent B's rolling-origin
  tournament (cutoff-scoped series views). Replay modes the tournament cannot
  honour (strict vintage replay, analytic verification) return a typed blocked
  report — never a fabricated score.
"""

from __future__ import annotations

import json
import time
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import Engine, text

from reva.adapters.postgres import repositories as repo
from reva.autonomy.common import ArtifactIO
from reva.kernel.errors import ImmutableRecordViolation, PolicyDenied
from reva.ports.autonomy import (
    JSON,
    ArtifactRef,
    EvaluationReport,
    EvaluationSpec,
    Finding,
    GateDecision,
    ModelDescriptor,
    ResourceBudget,
    ScopedContext,
)

SUPPORTED_BACKTEST_MODES = ("revised_data_retrospective",)


def _f(code: str, severity: str, message: str) -> Finding:
    return Finding(code=code, severity=severity, message=message, evidence=())  # type: ignore[arg-type]


def forecast_models() -> dict[str, Any]:
    from reva.models.forecasting import LastValue, LocalLevelLog, LogDrift

    return {m.key: m for m in (LastValue(), LogDrift(), LocalLevelLog())}


class PostgresModelRegistry:
    def __init__(self, engine: Engine, io_: ArtifactIO) -> None:
        self.engine = engine
        self.io = io_

    def register(self, descriptor: ModelDescriptor, approval: GateDecision) -> ModelDescriptor:
        if approval.outcome != "promote":
            raise PolicyDenied(f"model registration needs a promote decision, got {approval.outcome}")
        if not approval.reviewer_identity.startswith(("gate-evaluator:", "human:")):
            raise PolicyDenied("approval must come from a gate evaluator or a named human reviewer")
        spec = self.io.get_json(descriptor.manifest)
        for k in ("kind", "input_ports", "output_ports", "assumptions"):
            if k not in spec:
                raise PolicyDenied(f"model manifest lacks {k}")
        with self.engine.begin() as conn:
            row = conn.execute(text("SELECT id, spec_sha256 FROM model.version WHERE model_key=:k AND version=:v"),
                               {"k": descriptor.model_key, "v": descriptor.version}).mappings().one_or_none()
            if row is not None:
                if row["spec_sha256"] != descriptor.manifest.sha256:
                    raise ImmutableRecordViolation("model version exists with different content; bump the version")
                return ModelDescriptor(uuid.UUID(str(row["id"])), descriptor.model_key, descriptor.version,
                                       descriptor.manifest, descriptor.applicability)
            self.io.register(conn, descriptor.manifest, "model-spec/1")
            act = repo.simple_activity("model.registration", "review", approval.reviewer_identity,
                                       {"model_key": descriptor.model_key, "version": descriptor.version,
                                        "gate_decision": str(approval.decision_id),
                                        "policy_version": approval.policy_version})
            repo.record_activity(conn, act, outputs=[descriptor.manifest.sha256])
            from reva.operations.environment import code_digest

            mid = conn.execute(text(
                """INSERT INTO model.version(model_key, version, kind, spec_sha256, code_digest, input_ports,
                     output_ports, applicability, assumptions, approval_status, provenance_activity_id)
                   VALUES (:k, :v, :kind, :sha, :cd, CAST(:ip AS jsonb), CAST(:op AS jsonb), CAST(:ap AS jsonb),
                           CAST(:as AS jsonb), 'approved', :a) RETURNING id"""),
                {"k": descriptor.model_key, "v": descriptor.version, "kind": spec["kind"],
                 "sha": descriptor.manifest.sha256, "cd": code_digest(), "ip": json.dumps(spec["input_ports"]),
                 "op": json.dumps(spec["output_ports"]), "ap": json.dumps(dict(descriptor.applicability)),
                 "as": json.dumps(spec["assumptions"]), "a": act.id}).scalar_one()
        return ModelDescriptor(uuid.UUID(str(mid)), descriptor.model_key, descriptor.version, descriptor.manifest,
                               descriptor.applicability)

    def eligible(self, task: Mapping[str, JSON], context: ScopedContext) -> list[ModelDescriptor]:
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                """SELECT id, model_key, version, kind, spec_sha256, applicability FROM model.version
                   WHERE approval_status = 'approved' AND recorded_at <= :cut
                     AND (CAST(:kind AS text) IS NULL OR kind = CAST(:kind AS text))
                   ORDER BY model_key, version"""),
                {"cut": context.knowledge_cutoff, "kind": task.get("kind")}).mappings().all()
        out = []
        concept = task.get("concept")
        for r in rows:
            ap = dict(r["applicability"] or {})
            concepts = ap.get("concepts")
            if concept and concepts not in (None, "*") and concept not in (concepts or []):
                continue
            out.append(ModelDescriptor(uuid.UUID(str(r["id"])), r["model_key"], r["version"],
                                       ArtifactRef(r["spec_sha256"], "models", "application/json"), ap))
        return out


def _metrics(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    from reva.models.metrics import interval_covers

    n = len(records)
    ae = [r["abs_error"] for r in records]
    scaled = [r["scaled"] for r in records if r["scaled"] is not None]
    ints = [r for r in records if r.get("interval")]
    return {
        "n": n,
        "mae": (sum(ae) / n) if n else None,
        "mase": (sum(scaled) / len(scaled)) if scaled else None,
        "n_scaled": len(scaled),
        "n_undefined_scale": n - len(scaled),
        "coverage": (sum(1 for r in ints if interval_covers(tuple(r["interval"]), r["target"])) / len(ints))
        if ints else None,
        "n_intervals": len(ints),
    }


class TournamentModelEvaluator:
    VERSION = "model-evaluator:proper-metrics@1"

    def __init__(self, io_: ArtifactIO) -> None:
        self.io = io_

    def evaluate(self, model: ModelDescriptor, predictions: ArtifactRef, targets: ArtifactRef,
                 specification: EvaluationSpec) -> EvaluationReport:
        from reva.models.metrics import absolute_error, mase_scale

        started = time.monotonic()
        preds = self.io.get_json(predictions).get("records", [])
        series = {k: {int(y): float(v) for y, v in pts} for k, pts in self.io.get_json(targets)["series"].items()}
        findings: list[Finding] = []
        scored: list[dict[str, Any]] = []
        missing_target = leak = bad_interval = 0
        for p in preds:
            s = series.get(p["series_id"], {})
            if int(p["origin"]) >= int(p["target_year"]):
                leak += 1
                continue
            if p.get("interval") and p.get("interval_kind") != "prediction_interval":
                bad_interval += 1
                continue
            y = s.get(int(p["target_year"]))
            if y is None:
                missing_target += 1
                continue
            train = [v for yr, v in sorted(s.items()) if yr <= int(p["origin"])]
            scale = mase_scale(train) if len(train) >= 1 else None
            err = absolute_error(float(p["point"]), y)
            scored.append({"series_id": p["series_id"], "horizon": int(p["horizon"]), "target": y, "abs_error": err,
                           "scaled": (err / scale.value) if scale is not None and scale.value else None,
                           "interval": p.get("interval")})
        if leak:
            findings.append(_f("future_information", "blocker", f"{leak} predictions with origin >= target year"))
        if bad_interval:
            findings.append(_f("interval_type", "blocker",
                               f"{bad_interval} intervals are not prediction intervals; coverage undefined"))
        if missing_target:
            findings.append(_f("missing_targets", "warning", f"{missing_target} predictions have no target value"))
        if not scored:
            findings.append(_f("no_scored_predictions", "blocker", "nothing could be scored"))
        by_series: dict[str, list[dict[str, Any]]] = defaultdict(list)
        by_h: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for r in scored:
            by_series[r["series_id"]].append(r)
            by_h[r["horizon"]].append(r)
        metrics: dict[str, Any] = {
            "overall": _metrics(scored),
            "subgroups": {"series": {k: _metrics(v) for k, v in sorted(by_series.items())},
                          "horizon": {str(k): _metrics(v) for k, v in sorted(by_h.items())}},
            "resources": {"predictions": len(preds), "scored": len(scored),
                          "wall_seconds": round(time.monotonic() - started, 4)},
            "evaluator": self.VERSION, "mode": specification.mode,
        }
        prov = self.io.put_json({"schema": "reva-evaluation-provenance/1", "evaluator": self.VERSION,
                                 "model": model.model_key, "version": model.version,
                                 "spec": specification.manifest.sha256,
                                 "context_id": str(specification.context.context_id),
                                 "cutoff": specification.context.knowledge_cutoff.isoformat()}, "evaluation")
        return EvaluationReport(uuid.uuid4(), model, metrics, predictions, targets, prov, tuple(findings))


class TournamentBacktestRunner:
    VERSION = "backtest-runner:tournament-adapter@1"

    def __init__(self, io_: ArtifactIO, policy: Mapping[str, Any] | None = None) -> None:
        self.io = io_
        self.policy = policy

    def _blocked(self, model: ModelDescriptor, spec: EvaluationSpec, code: str, msg: str) -> EvaluationReport:
        empty = self.io.put_json({"records": []}, "evaluation")
        prov = self.io.put_json({"schema": "reva-backtest-provenance/1", "runner": self.VERSION, "blocked": msg,
                                 "spec": spec.manifest.sha256}, "evaluation")
        return EvaluationReport(uuid.uuid4(), model, {"status": "blocked", "reason": msg}, empty, empty, prov,
                                (_f(code, "blocker", msg),))

    async def run(self, model: ModelDescriptor, specification: EvaluationSpec,
                  budget: ResourceBudget) -> EvaluationReport:
        try:
            from reva.models.tournament import run_tournament
        except ImportError:  # pragma: no cover - agent B's module is present in this repository
            return self._blocked(model, specification, "tournament_unavailable", "tournament module not available")
        if specification.mode not in SUPPORTED_BACKTEST_MODES:
            return self._blocked(model, specification, "unsupported_replay_mode",
                                 f"replay mode {specification.mode} needs vintage replay (agent H1); "
                                 "this adapter supports revised_data_retrospective only")
        models = forecast_models()
        if model.model_key not in models:
            return self._blocked(model, specification, "unsupported_model",
                                 f"{model.model_key} is not a registered annual forecaster")
        spec = self.io.get_json(specification.manifest)
        panel = {k: [(int(y), float(v)) for y, v in pts] for k, pts in spec["panel"].items()}
        if len(panel) > 1000 or sum(len(v) for v in panel.values()) > 100_000:
            return self._blocked(model, specification, "budget_exceeded", "panel exceeds backtest budget")
        incumbent = spec.get("incumbent", "forecast.last_value")
        cands = [models[k] for k in sorted({model.model_key, incumbent, *spec.get("candidates", [])}) if k in models]
        started = time.monotonic()
        res = run_tournament(panel, cands, incumbent_key=incumbent, horizons=tuple(spec.get("horizons", (1, 2, 3))),
                             replay_mode=specification.mode, seed=int(spec.get("seed", 0)),
                             policy=self.policy or spec.get("policy", {}))
        wall = time.monotonic() - started
        recs = [r for r in res.records if r.model == model.model_key]
        preds = self.io.put_json({"records": [
            {"series_id": r.series_id, "origin": r.origin, "horizon": r.horizon, "target_year": r.target_year,
             "point": r.point, "interval": list(r.interval) if r.interval else None,
             "interval_kind": r.interval_kind} for r in recs]}, "evaluation")
        targets = self.io.put_json({"series": {k: [[y, v] for y, v in pts] for k, pts in panel.items()}},
                                   "evaluation")
        m = res.metrics[model.model_key]
        dec = res.decisions.get(model.model_key)
        metrics = {
            "mae": m.mae, "mase": m.mase, "n_predictions": m.n_predictions, "coverage": m.coverage,
            "n_intervals": m.n_intervals, "is_baseline": m.is_baseline, "incumbent": res.incumbent,
            "decision": None if dec is None else {"decision": dec.decision, "reasons": list(dec.reasons),
                                                  "n_pairs": dec.n_pairs},
            "replay_mode": res.replay_mode, "method_version": res.method_version,
            "resources": {"wall_seconds": round(wall, 4), "records": len(res.records)},
        }
        prov = self.io.put_json({"schema": "reva-backtest-provenance/1", "runner": self.VERSION,
                                 "tournament": res.method_version, "seed": res.seed, "policy": dict(res.policy),
                                 "spec": specification.manifest.sha256,
                                 "cutoff": specification.context.knowledge_cutoff.isoformat()}, "evaluation")
        findings = tuple(_f("ineligible", "info", f"{i.series_id}@{i.origin}: {i.report.reason}")
                         for i in res.ineligible if i.report.model_key == model.model_key)[:20]
        return EvaluationReport(uuid.uuid4(), model, metrics, preds, targets, prov, findings)
