"""Simulation engine: fidelity dispatch, deterministic execution and output digests.

L0  exact-spec cache lookup: the same full specification (excluding only the
    requested fidelity), same pinned baseline and same model manifest reuse a prior
    succeeded L1/L2 outcome; otherwise the run is a ``cache_miss`` — never a
    computed answer.
L1  deterministic fixed-quantity crude border-value exposure.
L2  labelled elasticity sensitivity over the declared grid; the per-country
    min/max across the grid is a ``sensitivity_range`` (no coverage level).
L3/L4 reserved: CapabilityGap.

The engine only reads (pinned release, prior runs). Persistence lives in
``reva.simulation.results``.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from reva.kernel.errors import CapabilityGap, RevaError
from reva.kernel.ids import logical_hash
from reva.kernel.uncertainty import UncertaintyKind, UncertaintySpec, UncertaintyStatus
from reva.kernel.values import decimal_str
from reva.models.economic import crude_exposure as ce
from reva.simulation import rng
from reva.simulation.compile import CompiledScenario, compile_scenario
from reva.simulation.spec import RESERVED_FIDELITY, ScenarioSpec

ENGINE_VERSION = "reva.simulation.engine@1.0.0"
SENSITIVITY_METHOD = "reva.simulation.l2_elasticity_grid_minmax@1.0.0"
_IMPL_FILES = (
    Path(ce.__file__),
    Path(__file__),
    Path(__file__).with_name("compile.py"),
    Path(__file__).with_name("coupling.py"),
)


@lru_cache(maxsize=1)
def implementation_digest() -> str:
    """Hash of the model + engine sources: any change invalidates the L0 cache."""
    h = hashlib.sha256()
    for p in _IMPL_FILES:
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return f"sha256:{h.hexdigest()}"


def model_manifest(conn: Connection) -> dict[str, Any]:
    """Deterministic model manifest for economic.crude_exposure@1.0.0."""
    row = conn.execute(
        text("SELECT spec_sha256, approval_status, applicability FROM model.version WHERE model_key=:k AND version=:v"),
        {"k": ce.MODEL_KEY, "v": ce.MODEL_VERSION},
    ).mappings().one_or_none()
    if row is None:
        raise CapabilityGap(f"model {ce.MODEL_KEY}@{ce.MODEL_VERSION} is not registered",
                            capability="model.registry")
    if row["approval_status"] != "approved":
        raise CapabilityGap(f"model {ce.MODEL_KEY}@{ce.MODEL_VERSION} is not approved",
                            capability="model.approval")
    return {
        "schema": "reva-model-manifest/1",
        "models": [{
            "model_key": ce.MODEL_KEY,
            "version": ce.MODEL_VERSION,
            "spec_sha256": row["spec_sha256"],
            "implementation": "reva.models.economic.crude_exposure",
            "implementation_digest": implementation_digest(),
            "decimal_precision": ce.PRECISION,
        }],
        "engine": ENGINE_VERSION,
    }


def cache_key(spec_json: dict[str, Any]) -> str:
    """Exact specification identity, excluding only the requested fidelity level."""
    return logical_hash({k: v for k, v in spec_json.items() if k != "fidelity"})


@dataclass
class ResultRow:
    output_key: str
    iso3: str
    subject_id: UUID
    variant: str
    value: dict[str, Any]
    uncertainty: dict[str, Any]
    applicability: dict[str, Any]

    def digest_view(self) -> dict[str, Any]:
        return {"output_key": self.output_key, "subject_id": str(self.subject_id), "variant": self.variant,
                "value": self.value}


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
    compiled: CompiledScenario | None = None
    release: dict[str, Any] | None = None
    seed_plan: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)

    @property
    def outcome_status(self) -> str:
        """sim.run_outcome status (the schema has no cache_miss state; see problems)."""
        return "unsupported" if self.status == "cache_miss" else self.status


def output_digest(rows: list[ResultRow]) -> str:
    return logical_hash(sorted((r.digest_view() for r in rows),
                               key=lambda d: (d["output_key"], d["subject_id"], d["variant"])))


def _value(output_key: str, d: Decimal) -> dict[str, Any]:
    unit = ce.OUTPUT_UNITS[output_key]
    if unit == ce.USD:
        basis = {"currency": "USD", "price_basis": "nominal", "scale_factor": "1",
                 "notes": "change in border value over one annual accounting period"}
    else:
        basis = {"scale_factor": "1", "notes": "percent of same-year baseline nominal GDP (current USD)"}
    return {"kind": "decimal", "value": decimal_str(d), "unit_ref": unit, "basis": basis}


def _unknown_uncertainty() -> dict[str, Any]:
    return UncertaintySpec.unknown(
        "Unquantified: inputs carry no published uncertainty and no error model is introduced; the value is "
        "conditional on the stated scenario assumptions"
    ).model_dump(mode="json")


def _range_uncertainty(lo: Decimal, hi: Decimal, grid: list[dict[str, Any]]) -> dict[str, Any]:
    spec = UncertaintySpec(
        status=UncertaintyStatus.quantified,
        kind=UncertaintyKind.sensitivity_range,
        description=("Elasticity sensitivity range: min/max of this output across the labelled scenario-"
                     "assumption grid. Not a confidence, credible or prediction interval; no coverage level."),
        method_version=SENSITIVITY_METHOD,
        lower=decimal_str(lo),
        upper=decimal_str(hi),
        conditioning=tuple(f"import_elasticity={g['import_elasticity']};export_elasticity={g['export_elasticity']}"
                           f";basis={g['basis']}" for g in grid),
    )
    return spec.model_dump(mode="json")


def _applicability(spec: ScenarioSpec, country: Any, variant: str, assumptions: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "applicable",
        "modality": "assumption_conditioned_scenario",
        "model": f"{ce.MODEL_KEY}@{ce.MODEL_VERSION}",
        "country": country.iso3,
        "reference_year": spec.reference_year,
        "horizon": spec.horizon.model_dump(mode="json"),
        "variant": variant,
        "variant_assumptions": assumptions,
        "inputs": country.input_refs(),
        "source_flags": ce.explain(country.inputs)["source_flags"],
        "not_claimed": ce.NOT_CLAIMED,
    }


def compute(compiled: CompiledScenario, fidelity: str) -> list[ResultRow]:
    """Deterministic L1 or L2 evaluation over ready countries (sorted, scheduling-independent)."""
    spec = compiled.spec
    ch = compiled.change
    grid = [g.model_dump(mode="json") for g in spec.assumptions.elasticity_grid]
    rows: list[ResultRow] = []
    wanted = list(spec.outputs)
    for c in sorted(compiled.countries, key=lambda c: c.iso3):
        if c.status != "ready" or c.values is None:
            continue
        m, x, g = c.values
        l1 = ce.exposure_l1(m, x, g, ch.import_multiplier, ch.export_multiplier)
        if fidelity == "L1":
            for k in wanted:
                rows.append(ResultRow(k, c.iso3, c.subject_id, "L1", _value(k, getattr(l1, k)),
                                      _unknown_uncertainty(),
                                      _applicability(spec, c, "L1", {"quantity_response": "fixed"})))
            continue
        variants = [
            ce.exposure_l2(m, x, g, ch.import_multiplier, ch.export_multiplier,
                           Decimal(repr(p["import_elasticity"])), Decimal(repr(p["export_elasticity"])))
            for p in grid
        ]
        for k in wanted:
            lo, hi = ce.sensitivity_range(variants, k)
            # The fixed-quantity case (the L1 special case) carries the grid's sensitivity range.
            rows.append(ResultRow(k, c.iso3, c.subject_id, "L1", _value(k, getattr(l1, k)),
                                  _range_uncertainty(lo, hi, grid),
                                  _applicability(spec, c, "L1", {"quantity_response": "fixed",
                                                                 "uncertainty": "sensitivity_range over grid"})))
            for p, r in zip(grid, variants, strict=True):
                if r.variant in {row.variant for row in rows if row.output_key == k and row.iso3 == c.iso3}:
                    continue  # duplicate grid points are evaluated once
                rows.append(ResultRow(k, c.iso3, c.subject_id, r.variant, _value(k, getattr(r, k)),
                                      _unknown_uncertainty(),
                                      _applicability(spec, c, r.variant, {"quantity_response": "elasticity",
                                                                          **p})))
    return rows


def _base_plan(scenario: dict[str, Any], fidelity: str) -> RunPlan:
    spec_json = dict(scenario["spec"])
    return RunPlan(scenario_id=UUID(str(scenario["id"])), fidelity=fidelity, spec_json=spec_json,
                   spec_digest=str(scenario["spec_digest"]), status="unsupported",
                   cache_key=cache_key(spec_json))


def plan_run(conn: Connection, scenario: dict[str, Any], fidelity: str, manifest: dict[str, Any]) -> RunPlan:
    """Validate, compile and execute (or look up) a run. Never raises for request problems:
    they are recorded on the plan as unsupported / missing_input outcomes."""
    from reva.simulation.spec import validate_scenario_spec

    t0 = time.perf_counter()
    plan = _base_plan(scenario, fidelity)
    plan.seed_plan = rng.seed_plan(int(plan.spec_json.get("seed", 0)), f"{ce.MODEL_KEY}@{ce.MODEL_VERSION}",
                                   [str(c) for c in plan.spec_json.get("countries", [])], 0)
    try:
        if fidelity in RESERVED_FIDELITY:
            raise CapabilityGap(
                f"fidelity {fidelity} is reserved and unavailable in V1",
                capability=f"fidelity.{fidelity}",
                reason="L3 requires justified parameter distributions; L4 requires research execution",
            )
        if fidelity not in ("L0", "L1", "L2"):
            raise CapabilityGap(f"unknown fidelity {fidelity!r}", capability="fidelity")
        spec = validate_scenario_spec({**plan.spec_json, "fidelity": fidelity})
        if fidelity == "L0":
            _lookup(conn, plan, manifest)
        else:
            compiled = compile_scenario(conn, spec)
            plan.compiled = compiled
            plan.release = compiled.release
            plan.applicability = {
                "explanation": ce.explain(None, fidelity=fidelity),
                "intervention": compiled.change.as_json(),
                "countries": {c.iso3: {"status": c.status, "problems": c.problems} for c in compiled.countries},
                "schedule": compiled.schedule,
                "baseline_inputs": compiled.baseline_inputs(),
                "baseline_digest": compiled.baseline_digest(),
            }
            if compiled.missing:
                plan.status = "missing_input"
                plan.problems = compiled.missing + compiled.incompatible
            elif compiled.incompatible:
                plan.status = "unsupported"
                plan.problems = compiled.incompatible
            else:
                plan.results = compute(compiled, fidelity)
                plan.status = "succeeded"
                plan.output_digest = output_digest(plan.results)
    except RevaError as e:
        plan.status = "unsupported"
        plan.problems = [e.problem()]
        plan.results, plan.cache_hit_of, plan.output_digest = [], None, None
        plan.applicability.setdefault("explanation", ce.explain(None, fidelity=fidelity))
    plan.cost = {
        "compute_seconds": round(time.perf_counter() - t0, 6),
        "countries": len(plan.spec_json.get("countries", [])),
        "grid_points": len(plan.spec_json.get("assumptions", {}).get("elasticity_grid", [])),
        "result_rows": len(plan.results),
        "random_draws": 0,
    }
    return plan


def _lookup(conn: Connection, plan: RunPlan, manifest: dict[str, Any]) -> None:
    """L0: reuse a prior succeeded computed outcome for the exact same specification."""
    mm = logical_hash(manifest)
    row = conn.execute(
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
    ).mappings().one_or_none()
    plan.applicability = {"explanation": ce.explain(None, fidelity="L0"),
                          "l0": "exact-specification cache lookup; no computation"}
    if row is None:
        plan.status = "cache_miss"
        plan.problems = [{"code": "cache_miss",
                          "detail": "no prior succeeded run with the same full specification, baseline and model "
                                    "manifest; L0 does not compute an answer (request L1 or L2)"}]
        return
    prior = UUID(str(row["id"]))
    res = conn.execute(
        text("""SELECT r.output_key, r.subject_id, r.variant, r.value, r.uncertainty, r.applicability,
                       e.label FROM sim.result r LEFT JOIN world.entity e ON e.id = r.subject_id
                WHERE r.run_id = :r ORDER BY r.output_key, r.subject_id, r.variant"""),
        {"r": prior},
    ).mappings().all()
    plan.results = [
        ResultRow(r["output_key"], str(r["applicability"].get("country", r["label"])), UUID(str(r["subject_id"])),
                  r["variant"], dict(r["value"]), dict(r["uncertainty"]),
                  {**dict(r["applicability"]), "cache_hit_of": str(prior)})
        for r in res
    ]
    plan.cache_hit_of = prior
    plan.status = "succeeded"
    plan.output_digest = output_digest(plan.results)
    if plan.output_digest != row["output_digest"]:
        raise CapabilityGap("cached outputs do not reproduce the recorded output digest",
                            capability="cache.integrity")
    plan.applicability["cache_hit_of"] = str(prior)
