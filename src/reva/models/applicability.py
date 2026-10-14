"""Applicability descriptors and checks.

A model version declares the envelope it was built and evaluated for:
geographies, a time window, a maximum horizon, the supports (temporal
resolution and aggregation) it understands, admissible interventions, minimum
training length and whether it needs a strictly positive series. Requests
outside that envelope produce ``ApplicabilityReport(applicable=False,
reasons=...)``: the model is not run, and nothing is silently extrapolated.

Capability levels L0-L4 are described here as well. L3 (uncertainty
propagation with justified parameter distributions) and L4 (research
execution) are reserved: they are always reported unavailable until they are
actually implemented.

Pure module: no database, HTTP or adapter imports.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from reva.models.ports import ApplicabilityReport, CapabilityLevel

ANY = "*"


# --------------------------------------------------------------------------- capability levels


@dataclass(frozen=True)
class CapabilityDescriptor:
    level: CapabilityLevel
    name: str
    description: str
    implemented: bool
    unavailable_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "name": self.name,
            "description": self.description,
            "implemented": self.implemented,
            "unavailable_reason": self.unavailable_reason,
        }


CAPABILITY_LEVELS: Mapping[CapabilityLevel, CapabilityDescriptor] = {
    CapabilityLevel.L0: CapabilityDescriptor(
        CapabilityLevel.L0,
        "exact_spec_cache",
        "Exact-specification cache lookup of a previously computed, digest-pinned result.",
        True,
    ),
    CapabilityLevel.L1: CapabilityDescriptor(
        CapabilityLevel.L1,
        "deterministic",
        "Deterministic evaluation of the registered model at pinned inputs.",
        True,
    ),
    CapabilityLevel.L2: CapabilityDescriptor(
        CapabilityLevel.L2,
        "labelled_sensitivity",
        "Labelled sensitivity over registered assumption envelopes; no probability interpretation.",
        True,
    ),
    CapabilityLevel.L3: CapabilityDescriptor(
        CapabilityLevel.L3,
        "uncertainty_propagation",
        "Propagation of justified parameter/input distributions.",
        False,
        "reserved: requires justified parameter distributions and validated propagation (not implemented)",
    ),
    CapabilityLevel.L4: CapabilityDescriptor(
        CapabilityLevel.L4,
        "research_execution",
        "Research-grade execution (structural estimation, agent-based or coupled feedback runs).",
        False,
        "reserved: requires research execution with a convergence/validation contract (not implemented)",
    ),
}


def capability_status(level: str | CapabilityLevel, supported: Iterable[str | CapabilityLevel]) -> ApplicabilityReport:
    """Is ``level`` available for a model that declares ``supported`` levels?

    L3/L4 are unavailable regardless of what a model declares.
    """
    try:
        lv = CapabilityLevel(str(level.value if isinstance(level, CapabilityLevel) else level))
    except ValueError:
        return ApplicabilityReport(False, (f"capability: unknown level {level!r}",), checked=("capability",))
    desc = CAPABILITY_LEVELS[lv]
    if not desc.implemented:
        return ApplicabilityReport(False, (f"capability: {lv.value} {desc.unavailable_reason}",),
                                   checked=("capability",))
    sup = {CapabilityLevel(str(s.value if isinstance(s, CapabilityLevel) else s)) for s in supported}
    if lv not in sup:
        return ApplicabilityReport(
            False,
            (f"capability: {lv.value} is not declared by this model (supported: {sorted(s.value for s in sup)})",),
            checked=("capability",),
        )
    return ApplicabilityReport(True, checked=("capability",))


# --------------------------------------------------------------------------- descriptors


@dataclass(frozen=True)
class ApplicabilityDescriptor:
    """Declared envelope of a model version. ``"*"`` means unrestricted."""

    model_ref: str
    geographies: frozenset[str] | str = ANY
    time_window: tuple[int, int] | None = None  # inclusive years of admissible *data and targets*
    max_horizon: int | None = None  # in ``horizon_unit`` steps from the origin
    horizon_unit: str = "year"
    temporal_resolutions: frozenset[str] = frozenset({"annual"})
    aggregations: frozenset[str] | str = ANY  # kernel Support.aggregation values
    admissible_interventions: frozenset[str] = frozenset()
    min_train: int = 1
    requires_positive: bool = False
    requires_consecutive: bool = False
    capability_levels: tuple[CapabilityLevel, ...] = (CapabilityLevel.L1,)
    notes: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_ref": self.model_ref,
            "geographies": self.geographies if isinstance(self.geographies, str) else sorted(self.geographies),
            "time_window": list(self.time_window) if self.time_window else None,
            "max_horizon": self.max_horizon,
            "horizon_unit": self.horizon_unit,
            "temporal_resolutions": sorted(self.temporal_resolutions),
            "aggregations": self.aggregations if isinstance(self.aggregations, str) else sorted(self.aggregations),
            "admissible_interventions": sorted(self.admissible_interventions),
            "min_train": self.min_train,
            "requires_positive": self.requires_positive,
            "requires_consecutive": self.requires_consecutive,
            "capability_levels": [c.value for c in self.capability_levels],
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class ApplicabilityRequest:
    """What a caller wants to do with the model."""

    geography: str | None = None
    temporal_resolution: str = "annual"
    aggregation: str | None = None
    train: Sequence[tuple[int, Any]] = ()  # (year, value) training observations
    target_years: Sequence[int] = ()
    horizons: Sequence[int] = ()
    intervention: str | None = None
    capability_level: str | CapabilityLevel | None = None


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, Decimal):
        return float(v) if v.is_finite() else math.nan
    if isinstance(v, (int, float)):
        return float(v)
    return None


def check_applicability(desc: ApplicabilityDescriptor, req: ApplicabilityRequest) -> ApplicabilityReport:
    """Check ``req`` against ``desc``. All failing checks are reported."""
    reasons: list[str] = []
    warnings: list[str] = []
    checked: list[str] = []

    checked.append("geography")
    if req.geography is not None and desc.geographies != ANY and req.geography not in desc.geographies:
        reasons.append(f"geography: {req.geography!r} is outside the declared envelope")

    checked.append("support")
    if req.temporal_resolution not in desc.temporal_resolutions:
        reasons.append(
            f"support: temporal resolution {req.temporal_resolution!r} not in {sorted(desc.temporal_resolutions)}"
        )
    if req.aggregation is not None and desc.aggregations != ANY and req.aggregation not in desc.aggregations:
        reasons.append(f"support: aggregation {req.aggregation!r} is outside the declared envelope")

    years = [int(y) for y, _ in req.train]
    checked.append("training")
    if len(years) < desc.min_train:
        reasons.append(f"training: {len(years)} observations < min_train {desc.min_train}")
    if desc.requires_consecutive and any(b - a != 1 for a, b in itertools.pairwise(years)):
        reasons.append("training: non-consecutive years (gaps are not imputed)")
    values = [_num(v) for _, v in req.train]
    if any(v is None for v in values):
        reasons.append("training: non-numeric or missing values (never replaced with zero)")
    elif any(not math.isfinite(v) for v in values if v is not None):
        reasons.append("training: non-finite values")
    if desc.requires_positive:
        checked.append("positivity")
        bad = [y for (y, _), v in zip(req.train, values, strict=True) if v is not None and v <= 0.0]
        if bad:
            reasons.append(
                f"positivity: strictly positive series required (log transform); non-positive at {bad[:5]} "
                "(values are never clipped)"
            )

    checked.append("time_window")
    if desc.time_window is not None:
        lo, hi = desc.time_window
        outside = sorted({y for y in [*years, *req.target_years] if y < lo or y > hi})
        if outside:
            reasons.append(f"time_window: years {outside[:5]} outside declared window [{lo}, {hi}]")

    checked.append("horizon")
    last = max(years) if years else None
    horizons = list(req.horizons)
    if last is not None:
        for t in req.target_years:
            if t <= last:
                reasons.append(f"horizon: target year {t} is not after the last training year {last}")
            else:
                horizons.append(t - last)
    if any(h < 1 for h in horizons):
        reasons.append("horizon: horizons must be >= 1")
    if desc.max_horizon is not None and horizons and max(horizons) > desc.max_horizon:
        reasons.append(
            f"horizon: {max(horizons)} {desc.horizon_unit}(s) exceeds the evaluated maximum {desc.max_horizon} "
            "(no silent extrapolation)"
        )

    checked.append("intervention")
    if req.intervention is not None and req.intervention not in desc.admissible_interventions:
        reasons.append(f"intervention: {req.intervention!r} is not admissible for this model")

    if req.capability_level is not None:
        cap = capability_status(req.capability_level, desc.capability_levels)
        checked.extend(cap.checked)
        reasons.extend(cap.reasons)

    return ApplicabilityReport(
        applicable=not reasons,
        reasons=tuple(reasons),
        warnings=tuple(warnings),
        checked=tuple(dict.fromkeys(checked)),
        model_ref=desc.model_ref,
    )


# --------------------------------------------------------------------------- registry descriptors

FORECAST_MAX_HORIZON = 3
"""Evaluated horizon range of the V1 tournament; forecasts beyond it are out of envelope."""


def descriptor_from_registry(entry: Mapping[str, Any]) -> ApplicabilityDescriptor:
    """Build a descriptor from a ``registry/models/methods.yaml`` model entry.

    Forecast models get the V1 tournament envelope (annual resolution, horizons
    1..3, consecutive training years). Scenario models carry their declared
    reference years, one-period horizon and fidelity levels.
    """
    ap = dict(entry.get("applicability") or {})
    ref = f"{entry['model_key']}@{entry['version']}"
    kind = entry.get("kind")
    if kind == "forecast":
        requires_positive = bool(ap.get("requires_positive", False))
        return ApplicabilityDescriptor(
            model_ref=ref,
            temporal_resolutions=frozenset({"annual"}),
            aggregations=frozenset({"point_in_time_stock", "period_average_stock", "period_flow_total"}),
            max_horizon=int(ap.get("max_horizon", FORECAST_MAX_HORIZON)),
            min_train=int(ap.get("min_train", 1)),
            requires_positive=requires_positive,
            requires_consecutive=requires_positive,  # log models need consecutive years
            capability_levels=(CapabilityLevel.L1,),
            notes=("univariate annual forecast of the future released statistic",),
        )
    fid = [CapabilityLevel(x) for x in ap.get("fidelity_supported", ["L1"])]
    years = ap.get("reference_years")
    horizon = ap.get("horizon") or {}
    return ApplicabilityDescriptor(
        model_ref=ref,
        time_window=(int(years[0]), int(years[1])) if years else None,
        max_horizon=int(horizon["periods"]) if "periods" in horizon else None,
        horizon_unit=str(horizon.get("unit", "year")),
        temporal_resolutions=frozenset({"annual"}),
        admissible_interventions=frozenset(ap.get("admissible_interventions", [])),
        min_train=int(ap.get("min_train", 0)),
        requires_positive=bool(ap.get("requires_positive", False)),
        capability_levels=tuple(fid),
    )


__all__ = [
    "ANY",
    "CAPABILITY_LEVELS",
    "FORECAST_MAX_HORIZON",
    "ApplicabilityDescriptor",
    "ApplicabilityReport",
    "ApplicabilityRequest",
    "CapabilityDescriptor",
    "capability_status",
    "check_applicability",
    "descriptor_from_registry",
]
