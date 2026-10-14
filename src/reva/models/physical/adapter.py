"""P8 physical-model ports: field-to-region aggregation and external-model conformance.

* :class:`FieldToRegionAggregator` — the protocol a conservative regridding /
  zonal-statistics component must implement. Regridding is a versioned model
  component with its own numerical error (architecture §16.2), so results carry
  method, version and an error/coverage statement. The only implementation in
  this build is the verification helper in :mod:`reva.models.physical.verification`
  (rectangular regions, analytic fields); real-dataset aggregation is reserved
  (``physical.field_to_region``).
* :class:`ExternalModelPort` — a BMI-style lifecycle (initialize / update /
  get_value / finalize, plus time and variable metadata) for mature external
  models (FMI co-simulation maps onto the same lifecycle). No external model is
  integrated; :func:`check_lifecycle_conformance` is a real, pure checker that
  exercises an in-process object against the contract (verification only).

No DB/HTTP imports (import-linter contract for ``reva.models``).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

import numpy as np

from reva.capabilities import require
from reva.models.physical.fields import FieldAsset, FieldMetadata

# --------------------------------------------------------------------------- aggregation


@dataclass(frozen=True)
class RegionGeometryRef:
    """A versioned region geometry (e.g. a boundary release) referenced by content hash."""

    region_id: str
    geometry_sha256: str
    boundary_version: str
    crs: str


@dataclass(frozen=True)
class RegionStatistic:
    region_id: str
    value: float | None
    unit: str
    statistic: Literal["area_weighted_mean", "area_integral", "population_weighted_mean"]
    coverage_fraction: float
    missing_fraction: float
    numerical_error_bound: float | None
    """Bound on regridding/aggregation error in ``unit``; ``None`` = not estimated (must be disclosed)."""


@dataclass(frozen=True)
class AggregationResult:
    method: str
    method_version: str
    field: FieldMetadata
    statistics: tuple[RegionStatistic, ...]
    conserved_total_relative_error: float | None
    verification_only: bool = False


@runtime_checkable
class FieldToRegionAggregator(Protocol):
    """Conservative aggregation of a gridded field to versioned region geometries."""

    method: str
    method_version: str

    def aggregate(self, asset: FieldAsset, regions: Sequence[RegionGeometryRef], *,
                  statistic: Literal["area_weighted_mean", "area_integral", "population_weighted_mean"],
                  ) -> AggregationResult: ...


def aggregate_field_to_regions(asset: FieldAsset, regions: Sequence[RegionGeometryRef]) -> AggregationResult:
    """Reserved entry point for real data: raises ``CapabilityGap`` (physical.field_to_region)."""
    require("physical.field_to_region", asset_id=asset.asset_id, regions=len(regions))
    raise AssertionError("unreachable: physical.field_to_region is not implemented")  # pragma: no cover


# --------------------------------------------------------------------------- external models (BMI-style)


@runtime_checkable
class ExternalModelPort(Protocol):
    """BMI-style lifecycle contract for an external model component.

    Times are floats in ``get_time_units()`` (a CF-style string such as
    ``"days since 2000-01-01"``) on the calendar named by ``get_calendar()``.
    """

    def initialize(self, config: Mapping[str, Any]) -> None: ...
    def update(self) -> None: ...
    def update_until(self, time: float) -> None: ...
    def finalize(self) -> None: ...

    def get_component_name(self) -> str: ...
    def get_input_var_names(self) -> tuple[str, ...]: ...
    def get_output_var_names(self) -> tuple[str, ...]: ...
    def get_var_units(self, name: str) -> str: ...
    def get_var_support(self, name: str) -> str: ...

    def get_start_time(self) -> float: ...
    def get_end_time(self) -> float: ...
    def get_current_time(self) -> float: ...
    def get_time_step(self) -> float: ...
    def get_time_units(self) -> str: ...
    def get_calendar(self) -> str: ...

    def get_value(self, name: str) -> np.ndarray: ...
    def set_value(self, name: str, value: np.ndarray) -> None: ...


@dataclass(frozen=True)
class ConformanceFinding:
    code: str
    severity: Literal["info", "warning", "blocker"]
    message: str


@dataclass(frozen=True)
class ConformanceReport:
    component: str | None
    outcome: Literal["pass", "fail"]
    findings: tuple[ConformanceFinding, ...]
    steps_executed: int
    verification_only: Literal[True] = True
    contract: str = "reva.external_model_port/1 (BMI-style)"
    variables: Mapping[str, str] = field(default_factory=dict)


_REQUIRED_METHODS: tuple[str, ...] = (
    "initialize", "update", "update_until", "finalize",
    "get_component_name", "get_input_var_names", "get_output_var_names", "get_var_units", "get_var_support",
    "get_start_time", "get_end_time", "get_current_time", "get_time_step", "get_time_units", "get_calendar",
    "get_value", "set_value",
)


def check_lifecycle_conformance(model: object, config: Mapping[str, Any], *, max_steps: int = 3) -> ConformanceReport:
    """Exercise ``model`` through initialize → update (≤ max_steps) → get_value → finalize.

    Checks: required methods present; every variable declares non-empty units and support;
    start ≤ current ≤ end; positive finite time step; time strictly increases on update and
    never passes end time; outputs are finite numeric arrays. Any exception is a blocker.
    VERIFICATION ONLY: passing this does not integrate the model into simulations.
    """
    findings: list[ConformanceFinding] = []
    missing = [m for m in _REQUIRED_METHODS if not callable(getattr(model, m, None))]
    if missing:
        return ConformanceReport(None, "fail", (ConformanceFinding(
            "missing_methods", "blocker", f"missing port methods: {sorted(missing)}"),), 0)

    m: Any = model
    steps = 0
    variables: dict[str, str] = {}
    name: str | None = None
    initialized = False
    try:
        m.initialize(config)
        initialized = True
        name = str(m.get_component_name())
        t0, t_end, dt = float(m.get_start_time()), float(m.get_end_time()), float(m.get_time_step())
        if not (math.isfinite(dt) and dt > 0):
            findings.append(ConformanceFinding("bad_time_step", "blocker", f"time step {dt} not positive/finite"))
        if not (math.isfinite(t0) and math.isfinite(t_end) and t0 <= t_end):
            findings.append(ConformanceFinding("bad_time_bounds", "blocker", f"start {t0} / end {t_end} invalid"))
        if not str(m.get_time_units()).strip():
            findings.append(ConformanceFinding("time_units_missing", "blocker", "time units are not declared"))
        if not str(m.get_calendar()).strip():
            findings.append(ConformanceFinding("calendar_missing", "blocker", "calendar is not declared"))
        outputs = tuple(m.get_output_var_names())
        for var in (*tuple(m.get_input_var_names()), *outputs):
            unit = str(m.get_var_units(var) or "")
            support = str(m.get_var_support(var) or "")
            if not unit.strip():
                findings.append(ConformanceFinding("units_missing", "blocker", f"variable {var!r} has no units"))
            if not support.strip():
                findings.append(ConformanceFinding("support_missing", "blocker", f"variable {var!r} has no support"))
            variables[var] = unit
        if not outputs:
            findings.append(ConformanceFinding("no_outputs", "blocker", "model declares no output variables"))
        prev = float(m.get_current_time())
        if abs(prev - t0) > 1e-12 * max(1.0, abs(t0)):
            findings.append(ConformanceFinding("start_time_mismatch", "warning",
                                               f"current time {prev} differs from start time {t0} after initialize"))
        while steps < max_steps and prev < t_end:
            m.update()
            steps += 1
            now = float(m.get_current_time())
            if not now > prev:
                findings.append(ConformanceFinding("time_not_increasing", "blocker",
                                                   f"time did not increase on update ({prev} -> {now})"))
                break
            if now > t_end + 1e-9 * max(1.0, abs(t_end)):
                findings.append(ConformanceFinding("time_past_end", "blocker", f"time {now} passed end {t_end}"))
                break
            prev = now
        for var in outputs:
            arr = np.asarray(m.get_value(var))
            if arr.dtype.kind not in "iuf":
                findings.append(ConformanceFinding("non_numeric_output", "blocker", f"{var!r} is not numeric"))
            elif not np.all(np.isfinite(arr)):
                findings.append(ConformanceFinding("non_finite_output", "blocker",
                                                   f"{var!r} contains non-finite values; missingness must be typed"))
    except Exception as exc:  # the port is untrusted: any failure is a finding, not a crash
        findings.append(ConformanceFinding("lifecycle_exception", "blocker", f"{type(exc).__name__}: {exc}"))
    finally:
        if initialized:
            try:
                m.finalize()
            except Exception as exc:
                findings.append(ConformanceFinding("finalize_exception", "blocker", f"{type(exc).__name__}: {exc}"))
    outcome: Literal["pass", "fail"] = "fail" if any(f.severity == "blocker" for f in findings) else "pass"
    return ConformanceReport(name, outcome, tuple(findings), steps, variables=variables)


def couple_external_model(model: ExternalModelPort, *, scenario_digest: str) -> None:
    """Reserved: coupling an external model into a simulation raises ``CapabilityGap``."""
    require("physical.external_model", scenario_digest=scenario_digest)
    raise AssertionError("unreachable: physical.external_model is not implemented")  # pragma: no cover
