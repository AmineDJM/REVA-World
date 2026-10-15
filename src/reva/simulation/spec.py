"""ScenarioSpec: JSON Schema validation followed by mandatory scope checks.

Version 2.0.0 is a discriminated union on ``model_key`` built from the registered
model plugins (:mod:`reva.simulation.scenario_schema`); crude-exposure specs with
``schema_version`` 1.0.0 (the V1 appendix) validate exactly as before and produce
the same canonical bytes, digests and L0 cache keys.

Contract bounds are implementation/applicability limits, not empirical claims
about plausible economics. The example's fixture baseline UUID is not a real
release; a real published baseline must be resolved before execution.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

import jsonschema
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from reva.kernel.errors import CapabilityGap, ValidationProblem
from reva.kernel.ids import logical_hash

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
FIXTURE_BASELINE = "00000000-0000-4000-8000-000000000001"
SUPPORTED_FIDELITY = ("L0", "L1", "L2")
RESERVED_FIDELITY = ("L3", "L4")
IMPORT_ELASTICITY_ENVELOPE = (-2.0, 1.0)
EXPORT_ELASTICITY_ENVELOPE = (-1.0, 2.0)


#: RFC 3339 ``date-time`` (section 5.6): full-date "T" full-time with an explicit offset.
_RFC3339 = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(\.\d{1,9})?([Zz]|[+-]\d{2}:\d{2})$",
    re.ASCII,
)
#: Tolerated clock skew for a knowledge cutoff "now".
CUTOFF_FUTURE_SKEW = timedelta(seconds=60)


def parse_knowledge_cutoff(value: object, *, now: datetime | None = None) -> datetime:
    """Strict RFC 3339 parse of a knowledge cutoff (independent of jsonschema format plugins).

    Rejects anything that is not an RFC 3339 date-time with an explicit offset
    (``infinity``, ``yesterday``, ``2026-99-99``, naive or date-only values), and
    cutoffs more than 60 s in the future. Returns an aware UTC datetime.
    """
    if not isinstance(value, str):
        raise ValidationProblem("knowledge_cutoff must be an RFC 3339 date-time string")
    m = _RFC3339.match(value)
    if m is None:
        raise ValidationProblem(
            "knowledge_cutoff is not an RFC 3339 date-time with an explicit offset",
            errors=[{"path": "/knowledge_cutoff", "message": "invalid date-time"}],
        )
    text = value[:-1] + "+00:00" if value[-1] in "Zz" else value
    frac = m.group(7)
    if frac and len(frac) > 7:  # fromisoformat accepts at most microseconds; truncate extra digits
        text = text.replace(frac, frac[:7], 1)
    try:
        parsed = datetime.fromisoformat(text.replace("t", "T", 1))
    except ValueError as e:
        raise ValidationProblem(
            "knowledge_cutoff is not a valid calendar date-time",
            errors=[{"path": "/knowledge_cutoff", "message": "invalid date-time"}],
        ) from e
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationProblem("knowledge_cutoff must carry an explicit UTC offset")
    utc = parsed.astimezone(UTC)
    current = now or datetime.now(UTC)
    if utc > current + CUTOFF_FUTURE_SKEW:
        raise ValidationProblem(
            "knowledge_cutoff lies in the future", errors=[{"path": "/knowledge_cutoff", "message": "future cutoff"}]
        )
    return utc


CRUDE_MODEL_KEY = "economic.crude_exposure"


@lru_cache(maxsize=8)
def _union_schema(_keys: tuple[Any, ...]) -> dict[str, Any]:
    from reva.simulation.plugins import REGISTRY
    from reva.simulation.scenario_schema import build_schema

    return build_schema(dict(REGISTRY))


def scenario_schema() -> dict[str, Any]:
    """The v2.0.0 union over the registered plugins (``contracts/scenario.schema.json``
    is its exported copy; the contract test keeps them equal)."""
    from reva.simulation.plugins import REGISTRY, VERSIONS, load_all

    load_all()
    return _union_schema(tuple(sorted((k, *sorted(VERSIONS.get(k, {}))) for k in REGISTRY)))


@lru_cache(maxsize=1)
def scenario_schema_v1() -> dict[str, Any]:
    """The frozen V1 appendix (crude model only)."""
    return json.loads((CONTRACTS / "scenario.v1.schema.json").read_text())


class ElasticityPoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    import_elasticity: float
    export_elasticity: float
    basis: Literal["scenario_assumption"]


class Intervention(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["set_exogenous_border_price_multiplier"]
    import_multiplier: float = Field(gt=0, le=3)
    export_multiplier: float = Field(gt=0, le=3)
    unit: Literal["ratio"]


class Assumptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    quantity_response: Literal["fixed", "elasticity_sensitivity"]
    elasticity_grid: tuple[ElasticityPoint, ...]
    no_general_equilibrium: Literal[True]


class Horizon(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    periods: Literal[1]
    unit: Literal["annual_accounting_period"]


class ScenarioSpec(BaseModel):
    """The crude-exposure spec (V1 shape; ``schema_version`` 1.0.0 or 2.0.0, same fields)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["1.0.0", "2.0.0"]
    title: str
    baseline_release_id: str
    knowledge_cutoff: str
    model_key: Literal["economic.crude_exposure"]
    model_version: Literal["1.0.0"]
    # V2 step 1 (ALLC): registry country keys (ISO 3166 alpha-3, or a World Bank economy code
    # for CHI/XKX) or exactly ["ALL"]; the JSON Schema keeps 1.0.0 specs on the V1 four
    # countries and the "fail" policy. "ALL" is resolved at compile against the pinned release.
    countries: tuple[Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")], ...]
    reference_year: int
    horizon: Horizon
    intervention: Intervention
    assumptions: Assumptions
    fidelity: Literal["L0", "L1", "L2"]
    missing_input_policy: Literal["fail", "skip_missing_countries"]
    outputs: tuple[str, ...]
    seed: int

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def canonical_bytes(self) -> bytes:
        """Plain sorted JSON; the DB verifies these bytes encode the stored spec."""
        return json.dumps(self.as_json(), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()

    def digest(self) -> str:
        return logical_hash(self.as_json())


class GenericScenarioSpec:
    """A validated spec of a non-crude model: the common fields as attributes, the
    model-specific parts as plain JSON (validated by the schema branch and the
    plugin's ``validate``). Same identity methods as :class:`ScenarioSpec`."""

    __slots__ = ("_data",)

    def __init__(self, data: dict[str, Any]) -> None:
        object.__setattr__(self, "_data", json.loads(json.dumps(data, allow_nan=False)))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("GenericScenarioSpec is immutable")

    def __getattr__(self, name: str) -> Any:
        try:
            return self._data[name]
        except KeyError as e:
            raise AttributeError(name) from e

    def as_json(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._data))

    def canonical_bytes(self) -> bytes:
        return json.dumps(self._data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()

    def digest(self) -> str:
        return logical_hash(self._data)


def _schema_errors(data: Any) -> list[Any]:
    validator = jsonschema.Draft202012Validator(scenario_schema(), format_checker=jsonschema.FormatChecker())
    return sorted(validator.iter_errors(data), key=lambda e: list(e.path))


def _schema_problem(errors: list[Any], version: str) -> ValidationProblem:
    return ValidationProblem(
        f"scenario does not satisfy ScenarioSpec {version}",
        errors=[{"path": "/" + "/".join(map(str, e.path)), "message": e.message} for e in errors[:20]],
    )


def _validate_generic(
    data: dict[str, Any], model: Any, *, allow_fixture_baseline: bool, now: datetime | None
) -> GenericScenarioSpec:
    errors = _schema_errors(data)
    if errors:
        raise _schema_problem(errors, "v2")
    policies = getattr(model, "missing_input_policies", frozenset({"fail"}))
    if data["missing_input_policy"] not in policies:
        raise CapabilityGap(
            f"missing_input_policy {data['missing_input_policy']!r} is not supported by {model.model_key}",
            capability=f"scenario.{data['missing_input_policy']}",
            supported=sorted(policies),
        )
    if data["fidelity"] != "L0" and data["fidelity"] not in model.fidelities:
        raise CapabilityGap(
            f"model {model.model_key} does not compute fidelity {data['fidelity']}",
            capability=f"fidelity.{data['fidelity']}",
            supported=sorted({"L0", *model.fidelities}),
        )
    parse_knowledge_cutoff(data["knowledge_cutoff"], now=now)
    if data["baseline_release_id"] == FIXTURE_BASELINE and not allow_fixture_baseline:
        raise ValidationProblem(
            "baseline_release_id is the illustrative fixture UUID; resolve a real published baseline release"
        )
    model.validate(data)
    return GenericScenarioSpec(data)


MACRO_OUTPUTS = frozenset({"gdp_loss", "unemployment", "consumer_inflation", "household_welfare"})


def _check_macro_outputs(data: Any) -> None:
    """Honesty rule: no macro outputs unless a listed model computes them (none does)."""
    outputs = data.get("outputs", []) if isinstance(data, dict) else []
    unsupported = MACRO_OUTPUTS & {o for o in outputs if isinstance(o, str)} if isinstance(outputs, list) else set()
    if unsupported:
        from reva.capabilities import require

        require("outputs.macroeconomic", requested=sorted(unsupported))
        raise CapabilityGap(
            f"outputs {sorted(unsupported)} are not produced by an accounting exposure model",
            capability="outputs.macroeconomic",
        )


def validate_scenario_spec(
    data: dict[str, Any], *, allow_fixture_baseline: bool = False, now: datetime | None = None
) -> ScenarioSpec | GenericScenarioSpec:
    """JSON Schema validation, then scope checks (still required after schema).

    Crude-exposure specs (``model_key`` absent or ``economic.crude_exposure``) take
    the V1 path unchanged and return a :class:`ScenarioSpec`; other models validate
    against their union branch and the plugin's ``validate`` and return a
    :class:`GenericScenarioSpec`. An unknown ``model_key`` is a ``CapabilityGap``.

    ``knowledge_cutoff`` is parsed explicitly (``parse_knowledge_cutoff``): the
    schema's ``format: date-time`` is only enforced by jsonschema when the
    optional ``rfc3339-validator`` plugin is installed.
    """
    from reva.capabilities import require

    if isinstance(data, dict) and data.get("fidelity") in RESERVED_FIDELITY:
        require(f"fidelity.{data['fidelity']}")  # raises a structured CapabilityGap
    model_key = data.get("model_key", CRUDE_MODEL_KEY) if isinstance(data, dict) else CRUDE_MODEL_KEY
    if isinstance(model_key, str) and model_key != CRUDE_MODEL_KEY:
        from reva.simulation import plugins

        _check_macro_outputs(data)
        model = plugins.get(model_key)
        version = data.get("model_version")
        if isinstance(version, str) and version != model.model_version and version in plugins.versions_of(model_key):
            model = plugins.get(model_key, version)  # another registered version (V3 SIMWORLD)
        return _validate_generic(data, model, allow_fixture_baseline=allow_fixture_baseline, now=now)
    if isinstance(data, dict) and isinstance(data.get("horizon"), dict) and data["horizon"].get("periods") != 1:
        require("horizon.multi_period", requested_periods=data["horizon"].get("periods"))
    _check_macro_outputs(data)
    errors = _schema_errors(data)
    if errors:
        raise _schema_problem(
            errors, "v1" if isinstance(data, dict) and data.get("schema_version") == "1.0.0" else "v2"
        )
    spec = ScenarioSpec.model_validate(data)
    parse_knowledge_cutoff(spec.knowledge_cutoff, now=now)
    if spec.baseline_release_id == FIXTURE_BASELINE and not allow_fixture_baseline:
        raise ValidationProblem(
            "baseline_release_id is the illustrative fixture UUID; resolve a real published baseline release"
        )
    for p in spec.assumptions.elasticity_grid:
        if not IMPORT_ELASTICITY_ENVELOPE[0] <= p.import_elasticity <= IMPORT_ELASTICITY_ENVELOPE[1]:
            raise ValidationProblem("import elasticity outside registered envelope")
        if not EXPORT_ELASTICITY_ENVELOPE[0] <= p.export_elasticity <= EXPORT_ELASTICITY_ENVELOPE[1]:
            raise ValidationProblem("export elasticity outside registered envelope")
    if spec.fidelity == "L1" and (spec.assumptions.quantity_response != "fixed" or spec.assumptions.elasticity_grid):
        raise ValidationProblem("L1 requires fixed quantities and an empty elasticity grid")
    if spec.fidelity == "L2" and (
        spec.assumptions.quantity_response != "elasticity_sensitivity" or not spec.assumptions.elasticity_grid
    ):
        raise ValidationProblem("L2 requires an elasticity_sensitivity grid")
    if not 2020 <= spec.reference_year <= 2024:
        raise CapabilityGap("reference year outside the model's registered applicability (2020-2024)")
    return spec
