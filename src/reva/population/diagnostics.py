"""Weight and calibration diagnostics for a materialised population table.

Scientific scope and limits
---------------------------
* Diagnostics compare the table against the recipe's own leaf counts (age
  margin, recorded-sex margin, joint age × sex cells). They say nothing about
  accuracy relative to the true population beyond what the source reports.
* Weight concentration is reported with two named methods: the Gini
  coefficient over record weights (``gini_record_weights``) and the share of
  total weight held by the heaviest ceil(1%) of records
  (``top_1pct_record_weight_share``).
* Only the age × sex joint distribution is supported by the source. Every
  other association (income, household, health, ...) is listed as unsupported;
  the table carries no information about it.
"""

from __future__ import annotations

import math
from fractions import Fraction
from typing import Any

import numpy as np
import pyarrow as pa

from reva.population.materialise import exact_weight_sums
from reva.population.recipe import PopulationRecipe, fraction_decimal_str, fraction_str

DIAGNOSTICS_VERSION = "reva-population-diagnostics-1"

UNSUPPORTED_ASSOCIATIONS: tuple[dict[str, str], ...] = tuple(
    {
        "association": assoc,
        "status": "unsupported",
        "reason": "Eurostat demo_pjan reports only age x recorded sex; no source evidence.",
    }
    for assoc in (
        "age x sex x income",
        "age x sex x household",
        "age x sex x health",
        "age x sex x ethnicity",
        "age x sex x religion",
        "age x sex x sexual_orientation",
        "age x sex x political_opinion",
        "age x sex x gender_identity",
        "within-cell age detail (exact age inside Y_OPEN, age of UNK records)",
        "sub-national geography",
        "any attribute other than age and recorded sex",
    )
)


def _finite(x: float) -> float:
    if not math.isfinite(x):
        raise ValueError("non-finite diagnostic value")
    return float(x)


def _margin_errors(
    targets: dict[str, Fraction], estimates: dict[str, Fraction]
) -> dict[str, Any]:
    max_abs = Fraction(0)
    max_rel: Fraction | None = Fraction(0)
    worst: str | None = None
    undefined_rel: list[str] = []
    for key in sorted(set(targets) | set(estimates)):
        t = targets.get(key, Fraction(0))
        e = estimates.get(key, Fraction(0))
        err = abs(e - t)
        if err > max_abs:
            max_abs, worst = err, key
        if t > 0:
            rel = err / t
            if max_rel is not None and rel > max_rel:
                max_rel = rel
        elif err > 0:
            undefined_rel.append(key)
    return {
        "n_categories": len(set(targets) | set(estimates)),
        "max_abs_error": fraction_str(max_abs),
        "max_abs_error_decimal": fraction_decimal_str(max_abs),
        "max_rel_error": fraction_decimal_str(max_rel) if max_rel is not None else None,
        "worst_category": worst,
        "categories_with_undefined_relative_error": undefined_rel,
    }


def diagnose(table: pa.Table, recipe: PopulationRecipe) -> dict[str, Any]:
    """JSON-ready diagnostics: ESS, concentration, margin errors, support violations."""
    n = table.num_rows
    w = table.column("weight").to_numpy()
    if n == 0:
        raise ValueError("empty table")
    if not np.all(np.isfinite(w)) or np.any(w < 0):
        raise ValueError("weights must be finite and non-negative")
    s1 = float(np.sum(w))
    s2 = float(np.sum(w * w))
    ess = s1 * s1 / s2 if s2 > 0 else 0.0
    ws = np.sort(w)
    idx = np.arange(1, n + 1, dtype=np.float64)
    gini = float((2.0 * np.sum(idx * ws)) / (n * s1) - (n + 1.0) / n) if s1 > 0 else 0.0
    top_k = max(1, math.ceil(0.01 * n))
    top_share = float(np.sum(ws[-top_k:]) / s1) if s1 > 0 else 0.0

    joint_est = {k[0]: v for k, v in exact_weight_sums(table, ["cell_id"]).items()}
    sex_est = {k[0]: v for k, v in exact_weight_sums(table, ["sex_code"]).items()}
    age_est = {k[0]: v for k, v in exact_weight_sums(table, ["age_code"]).items()}
    joint_t: dict[str, Fraction] = {}
    sex_t: dict[str, Fraction] = {}
    age_t: dict[str, Fraction] = {}
    for c in recipe.leaf_cells:
        joint_t[c.cell_id] = Fraction(c.count)
        sex_t[c.sex_code] = sex_t.get(c.sex_code, Fraction(0)) + c.count
        age_t[c.age_code] = age_t.get(c.age_code, Fraction(0)) + c.count

    cell_col = table.column("cell_id").to_pylist()
    zeros = set(recipe.structural_zeros)
    known = {c.cell_id for c in recipe.leaf_cells}
    missing = {m.cell_id for m in recipe.missing_cells}
    in_zero = sum(1 for c in cell_col if c in zeros)
    in_missing = sum(1 for c in cell_col if c in missing)
    outside = sum(1 for c in cell_col if c not in known and c not in missing)
    total_exact = sum(joint_est.values(), Fraction(0))

    return {
        "version": DIAGNOSTICS_VERSION,
        "recipe_digest": recipe.digest(),
        "n_records": n,
        "total_weight": fraction_str(total_exact),
        "recipe_total": str(recipe.total_count),
        "effective_sample_size": _finite(ess),
        "effective_sample_size_ratio": _finite(ess / n),
        "max_weight_share": _finite(float(ws[-1]) / s1 if s1 > 0 else 0.0),
        "weight_concentration": {
            "gini_record_weights": _finite(gini),
            "top_1pct_record_weight_share": _finite(top_share),
            "top_1pct_records": top_k,
        },
        "calibration_error": {
            "age_margin": _margin_errors(age_t, age_est),
            "sex_margin": _margin_errors(sex_t, sex_est),
            "joint_age_sex": _margin_errors(joint_t, joint_est),
            "basis": "exact weight_num/weight_den sums vs recipe leaf counts",
        },
        "structural_zero_violations": {
            "records_in_observed_zero_cells": in_zero,
            "records_in_missing_cells": in_missing,
            "records_outside_recipe_cells": outside,
            "ok": in_zero == 0 and in_missing == 0 and outside == 0,
        },
        "supported_associations": ["age x recorded sex (joint, from source cells)"],
        "unsupported_associations": [dict(a) for a in UNSUPPORTED_ASSOCIATIONS],
        "source_consistency": {
            "inconsistent_checks": sum(1 for c in recipe.consistency if c.status == "inconsistent"),
            "not_computable_checks": sum(
                1 for c in recipe.consistency if c.status == "not_computable"
            ),
            "missing_leaf_cells": len(recipe.missing_cells),
        },
    }
