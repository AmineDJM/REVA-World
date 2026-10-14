"""Prospective forecast ledger (architecture §13, validation stage V5).

Forecasts are *frozen at issue time*: the issue manifest (model, pinned data
release, every point/interval, the in-window MASE scale and the issue
timestamp from the database clock) is serialised canonically, stored
content-addressed in the ``evaluation`` namespace, and its SHA-256 is recorded
in a ``model.evaluation`` row (``metrics.kind = 'prospective_issue'``) inside a
new evaluation-mode context. Nothing about an issued forecast can change
afterwards: the store verifies bytes against their address and the database
rows are immutable.

Scoring happens later and separately. :func:`score_issued_forecasts` re-reads
and re-verifies the frozen manifest, looks up realised values in a published
release, and refuses to score (``MissingInput``) while no target outcome has
been recorded. Outcomes that REVA already held at issue time are not counted as
prospective. Each scoring pass appends a new ``prospective_score`` evaluation;
partial scoring leaves the remaining targets pending.

Replay mode: issued forecasts are what REVA actually produced from what it had
published, so rows use ``operational_as_known`` and are distinguished by
``metrics.kind`` (``prospective_issue`` / ``prospective_score``). They are never
labelled as, or mixed with, revised-data retrospective results.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from reva.evaluation.tournament_service import (
    CANDIDATES,
    DEFAULT_COUNTRIES,
    DEFAULT_YEARS,
    SUPPORTED_CONCEPTS,
    annual_year_of,
    finite_json,
    load_release_panel,
    model_ref,
)
from reva.kernel.errors import CapabilityGap, IntegrityFailure, MissingInput, ValidationProblem
from reva.kernel.ids import canonical_bytes
from reva.models.applicability import (
    ApplicabilityRequest,
    check_applicability,
    descriptor_from_registry,
)
from reva.models.forecasting.base import ForecastModel, IneligibleReport, to_float
from reva.models.metrics import absolute_error, interval_covers, interval_width, mase_scale
from reva.ports.storage import ArtifactStore

ISSUE_SCHEMA = "reva-forecast-issue/1"
SCORE_SCHEMA = "reva-forecast-score/1"
LEDGER_LABEL = "prospective forecast — frozen at issue time; scored only after outcomes are recorded"
REPLAY_MODE = "operational_as_known"
DEFAULT_LEVEL = 0.8


@dataclass(frozen=True)
class Outcome:
    value: Decimal | float
    recorded_at: datetime | None = None
    release_id: str | None = None
    observation_id: str | None = None


@dataclass(frozen=True)
class IssueResult:
    evaluation_id: UUID
    context_id: UUID
    manifest_sha256: str
    issued_at: datetime
    n_forecasts: int
    target_years: tuple[int, ...]


@dataclass(frozen=True)
class ScoreResult:
    evaluation_id: UUID
    context_id: UUID
    manifest_sha256: str
    n_scored: int
    n_pending: int


# --------------------------------------------------------------------------- canonical round trip


def manifest_sha256(manifest: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical bytes (equals the content address in the store)."""
    return hashlib.sha256(canonical_bytes(manifest)).hexdigest()


def _decode(obj: Any) -> Any:
    """Inverse of the canonical encoding for floats/decimals/datetimes."""
    if isinstance(obj, dict):
        if len(obj) == 1:
            ((k, v),) = obj.items()
            if k == "$float":
                return float(v)
            if k == "$decimal":
                return Decimal(v)
            if k == "$datetime":
                return datetime.fromisoformat(v)
        return {k: _decode(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decode(v) for v in obj]
    return obj


def load_frozen_manifest(store: ArtifactStore, sha: str) -> dict[str, Any]:
    """Read and verify a frozen manifest; any byte change is an IntegrityFailure."""
    data = store.get("evaluation", sha)
    if hashlib.sha256(data).hexdigest() != sha:
        raise IntegrityFailure("frozen forecast manifest does not match its hash", sha256=sha)
    manifest = _decode(json.loads(data))
    if manifest_sha256(manifest) != sha:
        raise IntegrityFailure("frozen forecast manifest is not canonical", sha256=sha)
    return manifest


# --------------------------------------------------------------------------- pure freeze / score


def _descriptor(model: ForecastModel) -> Any:
    from reva.ontology.registry import load_registry

    for entry in load_registry().models:
        if entry["model_key"] == model.key and entry["version"] == model.version:
            return descriptor_from_registry(entry)
    raise CapabilityGap(f"model {model_ref(model)} is not registered")


def freeze_issue(
    series: Mapping[str, Sequence[tuple[int, Any]]],
    meta: Mapping[str, Mapping[str, Any]],
    model: ForecastModel,
    horizons: Sequence[int],
    *,
    issued_at: datetime,
    data_ref: Mapping[str, Any],
    level: float = DEFAULT_LEVEL,
    descriptor: Any | None = None,
) -> dict[str, Any]:
    """Pure: build the issue manifest. Out-of-envelope series are refused, not extrapolated."""
    if issued_at.tzinfo is None:
        raise ValidationProblem("issued_at must be timezone-aware")
    desc = descriptor or _descriptor(model)
    hs = sorted(set(int(h) for h in horizons))
    issued: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    for sid, obs in sorted(series.items()):
        m = meta.get(sid, {})
        report = check_applicability(
            desc, ApplicabilityRequest(geography=m.get("iso3"), aggregation=m.get("aggregation"), train=list(obs),
                                       horizons=hs),
        )
        if not report.applicable:
            refused.append({"series_id": sid, "reasons": list(report.reasons)})
            continue
        fitted = model.fit(list(obs))  # type: ignore[arg-type]
        if isinstance(fitted, IneligibleReport):
            refused.append({"series_id": sid, "reasons": [f"ineligible: {fitted.reason}"]})
            continue
        scale = mase_scale([to_float(v) for _, v in obs])
        issued.append({
            "series_id": sid,
            "concept_id": m.get("concept_id"),
            "iso3": m.get("iso3"),
            "unit_ref": m.get("unit_ref"),
            "origin_year": fitted.origin_year,
            "n_train": len(obs),
            "train_mase_scale": scale.value,
            "scale_flag": scale.flag,
            "forecasts": [
                {"target_year": fc.target_year, "horizon": fc.horizon, "point": fc.point,
                 "interval": list(fc.interval) if fc.interval else None, "interval_kind": fc.interval_kind,
                 "level": fc.level, "uncertainty": fc.uncertainty.model_dump(mode="json", exclude_none=True)}
                for fc in fitted.predict(hs, level=level)
            ],
        })
    if not issued:
        raise CapabilityGap("no series is within the model's applicability envelope; nothing issued",
                            refused=refused[:20])
    return finite_json({
        "schema": ISSUE_SCHEMA,
        "label": LEDGER_LABEL,
        "replay_mode": REPLAY_MODE,
        "issued_at": issued_at.isoformat(),
        "model": model_ref(model),
        "horizons": hs,
        "level": level,
        "data": dict(data_ref),
        "series": issued,
        "refused": refused,
        "statement": "These forecasts were issued at issued_at from the pinned release and are scored only "
        "against outcomes recorded later.",
    })


def score_issue(
    manifest: Mapping[str, Any],
    outcomes: Mapping[tuple[str, int], Outcome],
    *,
    scored_at: datetime,
    issue_sha256: str,
) -> dict[str, Any]:
    """Pure: score a frozen issue against realised outcomes.

    Refuses (``MissingInput``) when no target has an admissible outcome yet, and
    (``ValidationProblem``) when ``scored_at`` is not after the issue time.
    Outcomes REVA already recorded at or before issue are not prospective and
    are listed separately, never scored.
    """
    if manifest.get("schema") != ISSUE_SCHEMA:
        raise ValidationProblem("not a forecast issue manifest")
    issued_at = datetime.fromisoformat(str(manifest["issued_at"]))
    if scored_at <= issued_at:
        raise ValidationProblem("scoring must happen after the issue time")
    scores: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    not_prospective: list[dict[str, Any]] = []
    for s in manifest["series"]:
        sid = s["series_id"]
        scale = s.get("train_mase_scale")
        for fc in s["forecasts"]:
            key = (sid, int(fc["target_year"]))
            out = outcomes.get(key)
            cell = {"series_id": sid, "target_year": key[1], "horizon": fc["horizon"]}
            if out is None:
                pending.append(cell)
                continue
            if out.recorded_at is not None and out.recorded_at <= issued_at:
                not_prospective.append({**cell, "reason": "outcome recorded at or before issue time",
                                        "recorded_at": out.recorded_at.isoformat()})
                continue
            y = to_float(out.value)
            ae = absolute_error(float(fc["point"]), y)
            interval = tuple(fc["interval"]) if fc.get("interval") else None
            scores.append({
                **cell, "point": fc["point"], "outcome": y, "abs_error": ae,
                "mase_scaled_error": ae / scale if scale else None,
                "covered": interval_covers(interval, y) if interval else None,
                "interval_width": interval_width(interval) if interval else None,
                "outcome_release_id": out.release_id, "outcome_observation_id": out.observation_id,
                "outcome_recorded_at": out.recorded_at.isoformat() if out.recorded_at else None,
            })
    if not scores:
        raise MissingInput(
            "no outcome has been recorded yet for any issued target; refusing to score",
            pending=len(pending), not_prospective=len(not_prospective),
        )
    scaled = [r["mase_scaled_error"] for r in scores if r["mase_scaled_error"] is not None]
    cov = [r["covered"] for r in scores if r["covered"] is not None]
    return finite_json({
        "schema": SCORE_SCHEMA,
        "label": LEDGER_LABEL,
        "replay_mode": REPLAY_MODE,
        "issue_manifest_sha256": issue_sha256,
        "issued_at": issued_at.isoformat(),
        "scored_at": scored_at.isoformat(),
        "model": manifest["model"],
        "scores": scores,
        "pending": pending,
        "not_prospective": not_prospective,
        "summary": {
            "n_scored": len(scores),
            "n_pending": len(pending),
            "n_not_prospective": len(not_prospective),
            "mae": math.fsum(r["abs_error"] for r in scores) / len(scores),
            "mase": math.fsum(scaled) / len(scaled) if scaled else None,
            "coverage": sum(cov) / len(cov) if cov else None,
            "n_intervals": len(cov),
        },
    })


# --------------------------------------------------------------------------- DB wrappers


def _model(model_key: str) -> ForecastModel:
    cls = CANDIDATES.get(model_key)
    if cls is None:
        raise CapabilityGap(f"forecast model {model_key!r} is not available", available=sorted(CANDIDATES))
    m: ForecastModel = cls()
    return m


def _model_version_id(conn: Any, model: ForecastModel) -> str:
    row = conn.execute(
        text("SELECT id FROM model.version WHERE model_key = :k AND version = :v"),
        {"k": model.key, "v": model.version},
    ).scalar_one_or_none()
    if row is None:
        raise MissingInput(f"model version {model_ref(model)} is not registered")
    return str(row)


def _insert_eval(conn: Any, mv: str, ctx_id: UUID, sha: str, scope: Mapping[str, Any], metrics: Mapping[str, Any],
                 act: UUID) -> UUID:
    return UUID(str(conn.execute(
        text(
            """INSERT INTO model.evaluation(model_version_id, context_id, evaluation_manifest_sha256, replay_mode,
                 task_scope, metrics, provenance_activity_id)
               VALUES (:m, :c, :s, :rm, CAST(:ts AS jsonb), CAST(:mt AS jsonb), :a) RETURNING id"""
        ),
        {"m": mv, "c": ctx_id, "s": sha, "rm": REPLAY_MODE, "ts": json.dumps(finite_json(dict(scope))),
         "mt": json.dumps(finite_json(dict(metrics))), "a": act},
    ).scalar_one()))


def issue_forecasts(
    engine: Engine,
    store: ArtifactStore,
    *,
    model_key: str,
    environment: str,
    horizons: Sequence[int] = (1, 2, 3),
    concepts: Sequence[str] = SUPPORTED_CONCEPTS,
    countries: Sequence[str] = DEFAULT_COUNTRIES,
    years: tuple[int, int] = (DEFAULT_YEARS[0], 9999),
    release_id: UUID | None = None,
    actor: str = "worker:ledger",
    level: float = DEFAULT_LEVEL,
) -> IssueResult:
    """Freeze forecasts from a published release and record them in the ledger."""
    from reva.adapters.postgres import repositories as repo
    from reva.belief.snapshots import release_as_of, release_by_id
    from reva.kernel.context import Origin, assert_publishable

    model = _model(model_key)
    if any(c not in SUPPORTED_CONCEPTS for c in concepts):
        raise CapabilityGap(f"ledger concepts must be among {list(SUPPORTED_CONCEPTS)}")
    with engine.connect() as conn:
        release = release_by_id(conn, release_id) if release_id else release_as_of(conn, None)
        if release is None or release.get("sequence") is None:
            raise MissingInput("no published release to issue forecasts from")
        assert_publishable(Origin(release["origin"]), environment)
        panel = load_release_panel(conn, release, concepts=list(concepts), countries=list(countries), years=years)
        issued_at: datetime = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        mv = _model_version_id(conn, model)
    manifest = freeze_issue(panel.series, panel.meta, model, horizons, issued_at=issued_at,
                            data_ref={**panel.source, "panel_digest": panel.digest(), "dropped": panel.dropped},
                            level=level)
    targets = tuple(sorted({f["target_year"] for s in manifest["series"] for f in s["forecasts"]}))
    n = sum(len(s["forecasts"]) for s in manifest["series"])
    with engine.begin() as conn:
        sha = repo.store_json_artifact(conn, store, manifest, "evaluation", schema_version="forecast-issue/1")
        if sha != manifest_sha256(manifest):
            raise IntegrityFailure("stored issue manifest hash differs from its canonical hash")
        act = repo.simple_activity("evaluation.forecast_issue", "evaluation", actor,
                                   {"model": model_ref(model), "horizons": list(horizons), "label": LEDGER_LABEL})
        repo.record_activity(conn, act, inputs=[(release["manifest_sha256"], {"role": "issue_data_release"})],
                             outputs=[sha])
        ctx_id = repo.create_context(conn, mode="evaluation", origin=release["origin"], manifest_sha256=sha,
                                     knowledge_cutoff=release["knowledge_cutoff"],
                                     parent_context_id=UUID(str(release["context_id"])))
        scope = {"kind": "prospective_forecast_ledger", "task": "annual_point_forecast.positive_series",
                 "concepts": sorted(concepts), "countries": sorted(countries), "horizons": sorted(set(horizons))}
        eid = _insert_eval(conn, mv, ctx_id, sha, scope, {
            "kind": "prospective_issue", "status": "issued_unscored", "label": LEDGER_LABEL,
            "issued_at": issued_at.isoformat(), "issue_manifest_sha256": sha, "n_forecasts": n,
            "target_years": list(targets), "refused": len(manifest["refused"]),
        }, act.id)
        repo.emit_outbox(conn, "evaluation.forecast_issued",
                         {"evaluation_id": str(eid), "manifest_sha256": sha, "model": model_ref(model)})
    return IssueResult(eid, ctx_id, sha, issued_at, n, targets)


def release_outcomes(
    conn: Any, release: Mapping[str, Any], manifest: Mapping[str, Any]
) -> dict[tuple[str, int], Outcome]:
    """Realised values for the manifest's targets from one published release."""
    wanted = {(s["series_id"], int(f["target_year"])) for s in manifest["series"] for f in s["forecasts"]}
    concepts = sorted({s["concept_id"] for s in manifest["series"]})
    rows = conn.execute(
        text(
            """SELECT c.concept_id, s.spatial_spec, s.temporal_extent, e.value, i.observation_id,
                      o.recorded_at
               FROM belief.estimate e
               JOIN ontology.concept_version c ON c.id = e.concept_version_id
               JOIN world.support s ON s.id = e.support_id
               LEFT JOIN belief.input i ON i.estimate_id = e.id
               LEFT JOIN evidence.observation o ON o.id = i.observation_id
               WHERE e.release_id = :r AND c.concept_id = ANY(:cs)"""
        ),
        {"r": release["id"], "cs": concepts},
    ).mappings()
    out: dict[tuple[str, int], Outcome] = {}
    for r in rows:
        key_s = str((r["spatial_spec"] or {}).get("key", ""))
        iso3 = key_s.split(":", 1)[1] if ":" in key_s else key_s
        year = annual_year_of(r["temporal_extent"] or {})
        if year is None:
            continue
        k = (f"{iso3}:{r['concept_id']}", year)
        if k not in wanted or (r["value"] or {}).get("kind") != "decimal":
            continue
        out[k] = Outcome(Decimal(str(r["value"]["value"])), r["recorded_at"], str(release["id"]),
                         str(r["observation_id"]) if r["observation_id"] else None)
    return out


def score_issued_forecasts(
    engine: Engine,
    store: ArtifactStore,
    issue_evaluation_id: UUID,
    *,
    release_id: UUID | None = None,
    actor: str = "worker:ledger",
) -> ScoreResult:
    """Score a frozen issue against the latest (or given) published release.

    Raises ``MissingInput`` while no target outcome exists (nothing is written).
    """
    from reva.adapters.postgres import repositories as repo
    from reva.belief.snapshots import release_as_of, release_by_id

    with engine.connect() as conn:
        row = conn.execute(
            text("""SELECT e.id, e.model_version_id, e.context_id, e.evaluation_manifest_sha256, e.metrics,
                           e.task_scope, c.origin, c.knowledge_cutoff
                    FROM model.evaluation e JOIN meta.context c ON c.id = e.context_id WHERE e.id = :i"""),
            {"i": issue_evaluation_id},
        ).mappings().one_or_none()
        if row is None or (row["metrics"] or {}).get("kind") != "prospective_issue":
            raise ValidationProblem("not a prospective forecast issue", evaluation_id=str(issue_evaluation_id))
        sha = row["evaluation_manifest_sha256"]
        manifest = load_frozen_manifest(store, sha)
        release = release_by_id(conn, release_id) if release_id else release_as_of(conn, None, row["origin"])
        if release is None or release.get("sequence") is None:
            raise MissingInput("no published release with outcomes")
        if release["origin"] != row["origin"]:
            raise ValidationProblem("outcome release origin differs from the issue origin")
        outcomes = release_outcomes(conn, release, manifest)
        scored_at: datetime = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
    result = score_issue(manifest, outcomes, scored_at=scored_at, issue_sha256=sha)
    result["outcome_release"] = {"release_id": str(release["id"]), "manifest_sha256": release["manifest_sha256"],
                                 "published_at": release["published_at"].isoformat()}
    with engine.begin() as conn:
        score_sha = repo.store_json_artifact(conn, store, result, "evaluation", schema_version="forecast-score/1")
        act = repo.simple_activity("evaluation.forecast_score", "evaluation", actor,
                                   {"issue_evaluation_id": str(issue_evaluation_id), "label": LEDGER_LABEL})
        repo.record_activity(conn, act, inputs=[(sha, {"role": "frozen_issue"}),
                                                (release["manifest_sha256"], {"role": "outcome_release"})],
                             outputs=[score_sha])
        ctx_id = repo.create_context(conn, mode="evaluation", origin=row["origin"], manifest_sha256=score_sha,
                                     knowledge_cutoff=release["knowledge_cutoff"],
                                     parent_context_id=UUID(str(row["context_id"])))
        scope = {**dict(row["task_scope"]), "issue_evaluation_id": str(issue_evaluation_id)}
        summary = result["summary"]
        eid = _insert_eval(conn, str(row["model_version_id"]), ctx_id, score_sha, scope, {
            "kind": "prospective_score", "label": LEDGER_LABEL, "issue_manifest_sha256": sha,
            "scored_at": result["scored_at"], **summary,
        }, act.id)
    return ScoreResult(eid, ctx_id, score_sha, summary["n_scored"], summary["n_pending"])


__all__ = [
    "ISSUE_SCHEMA",
    "LEDGER_LABEL",
    "SCORE_SCHEMA",
    "IssueResult",
    "Outcome",
    "ScoreResult",
    "freeze_issue",
    "issue_forecasts",
    "load_frozen_manifest",
    "manifest_sha256",
    "release_outcomes",
    "score_issue",
    "score_issued_forecasts",
]
