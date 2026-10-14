"""Generic iterative proportional fitting (raking) to categorical margins.

Scientific scope and limits
---------------------------
* Raking adjusts record weights multiplicatively so that weighted category
  totals match each supplied margin. It can only redistribute weight among
  records that already exist: it cannot create support for a category without
  records, and it encodes no association beyond what the starting weights
  carry (IPF preserves the odds-ratio structure of the seed weights).
* Margins are checked before fitting. Disagreeing margin totals, negative or
  non-finite targets, a positive target for a category with no positively
  weighted record, or records in a category without any target make the
  problem infeasible: the result has ``feasible=False`` and no weights. Targets
  are never silently altered or renormalised.
* Convergence is measured as the maximum absolute deviation of any margin
  category divided by the common margin total (``relative_tolerance``). If the
  pre-checks pass but IPF fails to converge, the margins may still be
  jointly incompatible with the record support (zero pattern); this is reported
  as ``converged=False``.
* Calibrated weights are floats; exact Fraction weights are not preserved.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from reva.kernel.errors import ValidationProblem

CALIBRATION_METHOD = "reva-ipf-raking-v1"


@dataclass(frozen=True)
class CalibrationResult:
    feasible: bool
    converged: bool
    iterations: int
    max_abs_deviation: float | None
    max_rel_deviation: float | None
    weights: np.ndarray | None = field(repr=False, compare=False)
    problems: tuple[dict[str, Any], ...] = ()
    targets: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    method: str = CALIBRATION_METHOD

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready report (weights summarised, not embedded)."""
        out: dict[str, Any] = {
            "method": self.method,
            "feasible": self.feasible,
            "converged": self.converged,
            "iterations": self.iterations,
            "max_abs_deviation": self.max_abs_deviation,
            "max_rel_deviation": self.max_rel_deviation,
            "problems": [dict(p) for p in self.problems],
            "targets": {k: dict(v) for k, v in self.targets.items()},
        }
        if self.weights is not None:
            out["weights_summary"] = {
                "n": int(self.weights.size),
                "sum": float(np.sum(self.weights)),
                "min": float(np.min(self.weights)) if self.weights.size else None,
                "max": float(np.max(self.weights)) if self.weights.size else None,
            }
        return out


def _as_float(x: Any) -> float:
    return float(x)


def rake(
    weights: Sequence[float] | np.ndarray,
    categories: Mapping[str, Sequence[str]],
    margins: Mapping[str, Mapping[str, Any]],
    *,
    max_iterations: int = 200,
    relative_tolerance: float = 1e-10,
) -> CalibrationResult:
    """Rake ``weights`` so each dimension's weighted totals match ``margins[dim]``.

    ``categories[dim][i]`` is record *i*'s category in dimension ``dim``. Every
    dimension in ``margins`` must have a category column and vice versa.
    """
    w0 = np.asarray(weights, dtype=np.float64)
    n = w0.size
    if w0.ndim != 1:
        raise ValidationProblem("weights must be one-dimensional")
    if not np.all(np.isfinite(w0)) or np.any(w0 < 0):
        raise ValidationProblem("weights must be finite and non-negative")
    if set(categories) != set(margins):
        raise ValidationProblem(
            "categories and margins must cover the same dimensions",
            categories=sorted(categories),
            margins=sorted(margins),
        )
    if not margins:
        raise ValidationProblem("at least one margin is required")
    targets = {d: {str(k): _as_float(v) for k, v in margins[d].items()} for d in sorted(margins)}
    problems: list[dict[str, Any]] = []

    # --- feasibility pre-checks (no target is ever modified)
    codes: dict[str, np.ndarray] = {}
    tvec: dict[str, np.ndarray] = {}
    for d, tg in targets.items():
        labels = [str(x) for x in categories[d]]
        if len(labels) != n:
            raise ValidationProblem(f"category column {d!r} has wrong length")
        for k, v in tg.items():
            if not math.isfinite(v) or v < 0:
                problems.append(
                    {"kind": "invalid_target", "dimension": d, "category": k, "target": repr(v)}
                )
        order = sorted(tg)
        pos = {k: i for i, k in enumerate(order)}
        unknown = sorted({lab for lab in labels if lab not in pos})
        if unknown:
            problems.append(
                {
                    "kind": "records_in_category_without_target",
                    "dimension": d,
                    "categories": unknown,
                }
            )
            continue
        code = np.fromiter((pos[lab] for lab in labels), dtype=np.int64, count=n)
        support = np.bincount(code, weights=(w0 > 0).astype(np.float64), minlength=len(order))
        for k in order:
            if tg[k] > 0 and support[pos[k]] == 0:
                problems.append(
                    {"kind": "positive_target_without_support", "dimension": d, "category": k}
                )
        codes[d] = code
        tvec[d] = np.array([tg[k] for k in order], dtype=np.float64)

    totals = {d: math.fsum(tg.values()) for d, tg in targets.items()}
    ref_total = next(iter(totals.values()))
    scale = max(abs(ref_total), 1.0)
    for d, t in totals.items():
        if abs(t - ref_total) > relative_tolerance * scale:
            problems.append(
                {
                    "kind": "margin_totals_disagree",
                    "dimension": d,
                    "total": repr(t),
                    "reference_total": repr(ref_total),
                }
            )
    if problems:
        return CalibrationResult(
            feasible=False,
            converged=False,
            iterations=0,
            max_abs_deviation=None,
            max_rel_deviation=None,
            weights=None,
            problems=tuple(problems),
            targets=targets,
        )

    # --- IPF
    w = w0.copy()
    dims = list(tvec)
    max_dev = math.inf
    it = 0
    while it < max_iterations:
        it += 1
        for d in dims:
            current = np.bincount(codes[d], weights=w, minlength=tvec[d].size)
            with np.errstate(divide="ignore", invalid="ignore"):
                factor = np.where(current > 0, tvec[d] / current, 1.0)
            w = w * factor[codes[d]]
        max_dev = max(
            float(
                np.max(np.abs(np.bincount(codes[d], weights=w, minlength=tvec[d].size) - tvec[d]))
            )
            for d in dims
        )
        if max_dev <= relative_tolerance * scale:
            break
    converged = max_dev <= relative_tolerance * scale
    if not converged:
        problems.append(
            {
                "kind": "not_converged",
                "detail": "margins may be jointly incompatible with the record support "
                "(zero pattern); targets were not altered",
            }
        )
    return CalibrationResult(
        feasible=True,
        converged=converged,
        iterations=it,
        max_abs_deviation=max_dev,
        max_rel_deviation=max_dev / scale,
        weights=w,
        problems=tuple(problems),
        targets=targets,
    )
