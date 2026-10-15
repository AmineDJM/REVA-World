"""Simulation engine: model dispatch, fidelity dispatch, deterministic execution, digests.

The engine is model-agnostic: the scenario spec's ``model_key`` selects a plugin
from :mod:`reva.simulation.plugins` (crude exposure = plugin #1,
:mod:`reva.simulation.crude_plugin`).

L0  exact-spec cache lookup (platform, any model): the same full specification
    (excluding only the requested fidelity), same pinned baseline and same model
    manifest reuse a prior succeeded L1/L2 outcome; otherwise the run is a
    ``cache_miss`` — never a computed answer.
L1/L2  computed by the plugin, if it declares the fidelity.
L3/L4  reserved: CapabilityGap.

The engine only reads (pinned release, pinned datasets, prior runs). Persistence
lives in ``reva.simulation.results``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from reva.kernel.errors import CapabilityGap, MissingInput, RevaError
from reva.kernel.ids import logical_hash
from reva.models.economic import crude_exposure as ce
from reva.ports.storage import ArtifactStore
from reva.simulation import plugins, rng
from reva.simulation.compile import CompiledScenario, compile_run
from reva.simulation.crude_plugin import (  # noqa: F401  (V1 names, re-exported)
    FIXED_POINT_CONDITION,
    SENSITIVITY_METHOD,
    SENSITIVITY_STATEMENT,
    SENSITIVITY_VARIANT,
    compute,
    implementation_digest,
)
from reva.simulation.plugins import ResultRow
from reva.simulation.spec import RESERVED_FIDELITY

ENGINE_VERSION = "reva.simulation.engine@1.0.0"
DEFAULT_MODEL_KEY = ce.MODEL_KEY
#: Approval states the engine runs. ``candidate`` runs only for a plugin whose manifest
#: declares ``review_status: maintainer_review_pending`` (V2 experimental models) and is
#: labelled as such on the model manifest and every run.
EXPERIMENTAL_REVIEW_STATUS = "maintainer_review_pending"
EXPERIMENTAL_LABEL = "experimental model, not yet reviewed"

__all__ = [
    "ENGINE_VERSION",
    "ResultRow",
    "RunPlan",
    "cache_key",
    "compute",
    "model_manifest",
    "output_digest",
    "plan_run",
]


def model_manifest(conn: Connection, model_key: str | None = None, model_version: str | None = None) -> dict[str, Any]:
    """Deterministic model manifest for the scenario's model (default: the crude model).

    The registry row (``model.version``) must exist and be approved — or be a
    ``candidate`` whose plugin declares itself experimental (review pending), in which
    case the manifest says so. The plugin's own manifest (implementation digest,
    precision, review status) is merged into the model entry.
    """
    key = model_key or DEFAULT_MODEL_KEY
    plugin = plugins.get(key, model_version)
    version = model_version or plugin.model_version
    if version != plugin.model_version:
        raise CapabilityGap(
            f"model {key}@{version} is not available (registered plugin: {plugin.model_version})",
            capability=f"model.{key}",
        )
    row = (
        conn.execute(
            text(
                "SELECT spec_sha256, approval_status, applicability FROM model.version WHERE model_key=:k AND version=:v"
            ),
            {"k": key, "v": version},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise CapabilityGap(f"model {key}@{version} is not registered", capability="model.registry")
    own = dict(plugin.manifest())
    experimental = row["approval_status"] == "candidate" and own.get("review_status") == EXPERIMENTAL_REVIEW_STATUS
    if row["approval_status"] != "approved" and not experimental:
        raise CapabilityGap(f"model {key}@{version} is not approved", capability="model.approval")
    entry: dict[str, Any] = {"model_key": key, "version": version, "spec_sha256": row["spec_sha256"], **own}
    if experimental:
        entry.update(approval_status="candidate", experimental=True, label=EXPERIMENTAL_LABEL)
    return {"schema": "reva-model-manifest/1", "models": [entry], "engine": ENGINE_VERSION}


def cache_key(spec_json: dict[str, Any]) -> str:
    """Exact specification identity, excluding only the requested fidelity level."""
    return logical_hash({k: v for k, v in spec_json.items() if k != "fidelity"})


@dataclass
class RunPlan:
    """Everything needed to publish one run (computed or recorded as not computed)."""

    scenario_id: UUID
    fidelity: str
    spec_json: dict[str, Any]
    spec_digest: str
    status: str  # succeeded | missing_input | unsupported | cache_miss
    problems: list[dict[str, Any]] = field(default_factory=list)
    results: list[ResultRow] = field(default_factory=list)
    applicability: dict[str, Any] = field(default_factory=dict)
    cache_key: str | None = None
    cache_hit_of: UUID | None = None
    output_digest: str | None = None
    compiled: CompiledScenario | Any | None = None
    release: dict[str, Any] | None = None
    seed_plan: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)
    input_provenance: dict[str, Any] | None = None

    @property
    def outcome_status(self) -> str:
        """sim.run_outcome status (the schema has no cache_miss state; see problems)."""
        return "unsupported" if self.status == "cache_miss" else self.status


def output_digest(rows: list[ResultRow]) -> str:
    """Logical hash of the sorted digest views (single-period views are the V1 views)."""
    return logical_hash(
        sorted(
            (r.digest_view() for r in rows),
            key=lambda d: (d["output_key"], d["subject_id"], d["variant"], d.get("period", [])),
        )
    )


def _plugin_or_none(model_key: Any, model_version: Any = None) -> plugins.SimulationModel | None:
    try:
        return plugins.get(str(model_key), None if model_version is None else str(model_version))
    except CapabilityGap:
        return None


def _explanation(model: plugins.SimulationModel | None, fidelity: str, model_key: Any) -> dict[str, Any]:
    hook = plugins.optional_hook(model, "explanation") if model is not None else None
    if hook is not None:
        return dict(hook(fidelity))
    return {"model": f"{model_key}", "fidelity": fidelity}


def _seed_subjects(model: plugins.SimulationModel | None, spec_json: dict[str, Any]) -> list[str]:
    hook = plugins.optional_hook(model, "seed_subjects") if model is not None else None
    if hook is not None:
        try:
            return [str(s) for s in hook(spec_json)]
        except RevaError:
            pass
    return [str(c) for c in spec_json.get("countries", [])]


def plan_run(
    conn: Connection,
    scenario: dict[str, Any],
    fidelity: str,
    manifest: dict[str, Any],
    store: ArtifactStore | None = None,
) -> RunPlan:
    """Validate, compile and execute (or look up) a run. Never raises for request problems:
    they are recorded on the plan as unsupported / missing_input outcomes."""
    from reva.simulation.spec import validate_scenario_spec

    t0 = time.perf_counter()
    spec_json = dict(scenario["spec"])
    plan = RunPlan(
        scenario_id=UUID(str(scenario["id"])),
        fidelity=fidelity,
        spec_json=spec_json,
        spec_digest=str(scenario["spec_digest"]),
        status="unsupported",
        cache_key=cache_key(spec_json),
    )
    model_key = spec_json.get("model_key", DEFAULT_MODEL_KEY)
    model = _plugin_or_none(model_key, spec_json.get("model_version"))
    model_id = (
        f"{model.model_key}@{model.model_version}"
        if model is not None
        else f"{model_key}@{spec_json.get('model_version')}"
    )
    plan.seed_plan = rng.seed_plan(int(spec_json.get("seed", 0)), model_id, _seed_subjects(model, spec_json), 0)
    try:
        if fidelity in RESERVED_FIDELITY:
            raise CapabilityGap(
                f"fidelity {fidelity} is reserved and unavailable in V1",
                capability=f"fidelity.{fidelity}",
                reason="L3 requires justified parameter distributions; L4 requires research execution",
            )
        if fidelity not in ("L0", "L1", "L2"):
            raise CapabilityGap(f"unknown fidelity {fidelity!r}", capability="fidelity")
        spec = validate_scenario_spec({**spec_json, "fidelity": fidelity})
        if model is None:
            model = plugins.get(str(model_key), spec_json.get("model_version"))  # structured CapabilityGap
        if fidelity == "L0":
            _lookup(conn, plan, manifest, model)
        else:
            if fidelity not in model.fidelities:
                raise CapabilityGap(
                    f"model {model_id} does not compute fidelity {fidelity}",
                    capability=f"fidelity.{fidelity}",
                    model=model_id,
                    supported=sorted({"L0", *model.fidelities}),
                )
            _, compiled, inputs = compile_run(conn, spec.as_json(), store)
            plan.compiled = compiled
            plan.release = dict(inputs.release)
            plan.input_provenance = inputs.provenance() if inputs.datasets else None
            hook = plugins.optional_hook(model, "run_applicability")
            plan.applicability = (
                dict(hook(compiled, fidelity)) if hook else {"explanation": _explanation(model, fidelity, model_key)}
            )
            plan.applicability.setdefault("explanation", _explanation(model, fidelity, model_key))
            if plan.input_provenance is not None:
                plan.applicability["input_provenance"] = plan.input_provenance
            if manifest.get("models") and manifest["models"][0].get("experimental"):
                plan.applicability["model_label"] = EXPERIMENTAL_LABEL
            own_missing = getattr(compiled, "missing", None)
            if own_missing is None:
                # The plugin does not report missing inputs itself: the platform refuses
                # before compute whenever any declared input is missing.
                missing = inputs.missing_problems()
                incompatible: list[dict[str, Any]] = []
            else:
                missing = list(own_missing)
                incompatible = list(getattr(compiled, "incompatible", []) or [])
            if missing:
                plan.status = "missing_input"
                plan.problems = missing + incompatible
            elif incompatible:
                plan.status = "unsupported"
                plan.problems = incompatible
            else:
                plan.results = model.compute(compiled, fidelity)
                plan.status = "succeeded"
                plan.output_digest = output_digest(plan.results)
    except MissingInput as e:
        plan.status = "missing_input"
        plan.problems = [{"code": "missing_input", **m} for m in e.details.get("missing", [])] or [e.problem()]
        plan.results, plan.cache_hit_of, plan.output_digest = [], None, None
        plan.applicability.setdefault("explanation", _explanation(model, fidelity, model_key))
    except RevaError as e:
        plan.status = "unsupported"
        plan.problems = [e.problem()]
        plan.results, plan.cache_hit_of, plan.output_digest = [], None, None
        plan.applicability.setdefault("explanation", _explanation(model, fidelity, model_key))
    cost_hook = plugins.optional_hook(model, "cost") if model is not None else None
    plan.cost = {
        "compute_seconds": round(time.perf_counter() - t0, 6),
        "countries": len(spec_json.get("countries", [])),
        **(dict(cost_hook(spec_json)) if cost_hook else {}),
        "result_rows": len(plan.results),
        "random_draws": 0,
    }
    return plan


def _period(row: dict[str, Any]) -> tuple[Any, Any] | None:
    from datetime import date

    s, e = row.get("period_start"), row.get("period_end")
    if s is None and e is None:
        return None
    return (date.fromisoformat(str(s)), date.fromisoformat(str(e)))


def _lookup(conn: Connection, plan: RunPlan, manifest: dict[str, Any], model: plugins.SimulationModel) -> None:
    """L0: reuse a prior succeeded computed outcome for the exact same specification."""
    mm = logical_hash(manifest)
    row = (
        conn.execute(
            text(
                """SELECT r.id, o.output_digest FROM sim.run r JOIN sim.run_outcome o ON o.run_id = r.id
               JOIN sim.scenario s ON s.id = r.scenario_id
               WHERE o.status = 'succeeded' AND o.cache_hit_of IS NULL AND r.fidelity IN ('L1','L2')
                 AND r.execution_profile->>'cache_key' = :k
                 AND r.execution_profile->>'model_manifest_logical_sha256' = :mm
                 AND s.baseline_release_id = CAST(:b AS uuid)
               ORDER BY o.recorded_at, r.id LIMIT 1"""
            ),
            {"k": plan.cache_key, "mm": mm, "b": plan.spec_json["baseline_release_id"]},
        )
        .mappings()
        .one_or_none()
    )
    plan.applicability = {
        "explanation": _explanation(model, "L0", model.model_key),
        "l0": "exact-specification cache lookup; no computation",
    }
    if row is None:
        plan.status = "cache_miss"
        plan.problems = [
            {
                "code": "cache_miss",
                "detail": "no prior succeeded run with the same full specification, baseline and model "
                "manifest; L0 does not compute an answer (request L1 or L2)",
            }
        ]
        return
    prior = UUID(str(row["id"]))
    # to_jsonb(r) reads the V2 period columns when present (migration 0008) and yields
    # NULL on a pre-0008 schema, so the lookup works on both.
    res = (
        conn.execute(
            text("""SELECT r.output_key, r.subject_id, r.variant, r.value, r.uncertainty, r.applicability,
                       to_jsonb(r)->>'period_start' AS period_start, to_jsonb(r)->>'period_end' AS period_end,
                       e.label FROM sim.result r LEFT JOIN world.entity e ON e.id = r.subject_id
                WHERE r.run_id = :r ORDER BY r.output_key, r.subject_id, r.variant,
                      to_jsonb(r)->>'period_start'"""),
            {"r": prior},
        )
        .mappings()
        .all()
    )
    plan.results = [
        ResultRow(
            r["output_key"],
            str(r["applicability"].get("country", r["label"])),
            UUID(str(r["subject_id"])),
            r["variant"],
            dict(r["value"]),
            dict(r["uncertainty"]),
            {**dict(r["applicability"]), "cache_hit_of": str(prior)},
            _period(dict(r)),
        )
        for r in res
    ]
    plan.cache_hit_of = prior
    plan.status = "succeeded"
    plan.output_digest = output_digest(plan.results)
    if plan.output_digest != row["output_digest"]:
        raise CapabilityGap("cached outputs do not reproduce the recorded output digest", capability="cache.integrity")
    plan.applicability["cache_hit_of"] = str(prior)
