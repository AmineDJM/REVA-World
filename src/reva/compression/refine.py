"""Refinement of a coarse table along an already supported dimension.

Scientific scope and limits
---------------------------
* Refinement may only add detail the source recipe actually supports. For a
  Eurostat ``demo_pjan`` recipe that means splitting age bands back into the
  recipe's leaf cells (``dimension="age"``: single years, or the recipe's
  age-band leaves where the source has nothing finer); recorded sex is already
  resolved in coarse tables.
* The split of a coarse cell's exact weight ``W`` across its leaves uses the
  recipe's own leaf counts ``c_i``: ``w_i = W · c_i / Σc``. Total weight per
  coarse cell, per sex and overall is conserved exactly. If ``W`` was not changed
  since coarsening, refined weights equal the recipe counts exactly.
* Observed-zero leaves receive no record. ``Y_OPEN`` and ``UNK`` stay explicit
  (no age is imputed inside them).
* Any other dimension (income, household, health, ...) is unsupported: there is
  no source evidence for the split, so ``UnsupportedRefinement`` is raised
  rather than fabricating detail.
"""

from __future__ import annotations

import time
from fractions import Fraction
from typing import Any

import pyarrow as pa

from reva.compression.certificates import (
    CompressionCertificate,
    ConservationCheck,
    conservation_checks,
    group_max_errors,
)
from reva.compression.stratified import CompressionResult
from reva.kernel.errors import CapabilityGap, IncompatibleSemantics, ValidationProblem
from reva.kernel.ids import logical_hash
from reva.population.materialise import (
    INT64_MAX,
    POPULATION_SCHEMA,
    SEMANTICS_NOTE,
    exact_weight_sums,
    table_metadata,
)
from reva.population.recipe import (
    FORBIDDEN_ATTRIBUTES,
    LeafCell,
    PopulationRecipe,
    fraction_decimal_str,
    fraction_str,
)

REQUIRED_COARSE_COLUMNS = (
    "cell_id",
    "age_lo",
    "age_hi",
    "includes_top_coded",
    "is_unknown_age",
    "sex_code",
    "weight_num",
    "weight_den",
)


class UnsupportedRefinement(CapabilityGap):
    """Refinement requested along a dimension the source recipe does not support."""

    code = "unsupported_refinement"


def _members(
    recipe: PopulationRecipe,
    sex: str,
    lo: int | None,
    hi: int | None,
    top: bool,
    unk: bool,
) -> list[LeafCell]:
    open_lo = recipe.open_age_lower_bound
    out = []
    for leaf in recipe.leaf_cells:
        if leaf.sex_code != sex:
            continue
        if unk:
            if leaf.is_unknown_age:
                out.append(leaf)
            continue
        if leaf.is_unknown_age:
            continue
        if leaf.is_top_coded:
            if top or (hi is None and lo is not None and open_lo is not None and lo <= open_lo):
                out.append(leaf)
            continue
        assert lo is not None
        leaf_lo = leaf.age_years if leaf.age_years is not None else leaf.age_lo
        leaf_hi = leaf.age_years if leaf.age_years is not None else leaf.age_hi
        if leaf_lo is None:
            raise ValidationProblem("recipe leaf without an age range", cell_id=leaf.cell_id)
        if leaf_lo >= lo and (hi is None or (leaf_hi is not None and leaf_hi <= hi)):
            out.append(leaf)
        elif leaf_lo <= (hi if hi is not None else leaf_lo) and (leaf_hi is None or leaf_hi >= lo):
            raise ValidationProblem(
                f"coarse cell [{lo}, {hi}] only partly covers the leaf {leaf.cell_id}",
                cell_id=leaf.cell_id,
            )
    return out


def refine(
    coarse_table: pa.Table,
    recipe: PopulationRecipe,
    dimension: str,
    *,
    task_scope: str = "age x recorded-sex weight totals",
    epsilon: str = "0",
) -> CompressionResult:
    """Split coarse cells along ``dimension`` using the recipe's leaf counts.

    Returns a table in the fine population schema (one record per nonzero
    leaf cell), the coarse→fine mapping, and a certificate whose loss metric is
    the maximum absolute deviation of refined leaf weights from recipe counts.
    """
    t0 = time.perf_counter()
    if dimension not in recipe.supported_attributes:
        sensitive = dimension.lower() in FORBIDDEN_ATTRIBUTES
        raise UnsupportedRefinement(
            f"refinement along {dimension!r} is unsupported: the recipe supports only "
            f"{list(recipe.supported_attributes)}; the requested new detail has no source "
            "evidence and would be fabricated"
            + ("; it is also a sensitive attribute that is never synthesised" if sensitive else ""),
            dimension=dimension,
            supported=list(recipe.supported_attributes),
            report={
                "new_detail": dimension,
                "status": "unsupported",
                "sensitive_attribute": sensitive,
                "reason": "no source cells cross-classify this dimension with age x sex",
            },
        )
    if dimension == "sex":
        raise ValidationProblem("coarse tables already resolve recorded sex; nothing to refine along 'sex'")
    missing_cols = [c for c in REQUIRED_COARSE_COLUMNS if c not in coarse_table.column_names]
    if missing_cols:
        raise ValidationProblem("not a coarse population table", missing_columns=missing_cols)
    md = table_metadata(coarse_table)
    digest = recipe.digest()
    if md.get("reva.recipe_digest") not in (None, digest):
        raise IncompatibleSemantics(
            "coarse table was derived from a different recipe",
            table_recipe_digest=md.get("reva.recipe_digest"),
            recipe_digest=digest,
        )

    rows = coarse_table.select(list(REQUIRED_COARSE_COLUMNS)).to_pylist()
    fine_w: dict[str, Fraction] = {}
    mapping: dict[str, list[str]] = {}
    coarse_in: dict[str, Fraction] = {}
    coarse_out: dict[str, Fraction] = {}
    for r in rows:
        if r["weight_den"] <= 0:
            raise ValidationProblem("weight_den must be positive", cell_id=r["cell_id"])
        w = Fraction(r["weight_num"], r["weight_den"])
        cid = r["cell_id"]
        coarse_in[cid] = coarse_in.get(cid, Fraction(0)) + w
        members = _members(
            recipe,
            r["sex_code"],
            r["age_lo"],
            r["age_hi"],
            r["includes_top_coded"],
            r["is_unknown_age"],
        )
        denom = sum(m.count for m in members)
        if denom == 0:
            if w != 0:
                raise ValidationProblem(
                    "coarse cell has weight but the recipe has no observed count to split it by",
                    cell_id=cid,
                )
            continue
        for m in members:
            if m.count == 0:
                continue
            part = w * m.count / denom
            fine_w[m.cell_id] = fine_w.get(m.cell_id, Fraction(0)) + part
            coarse_out[cid] = coarse_out.get(cid, Fraction(0)) + part
            if m.cell_id not in mapping.setdefault(cid, []):
                mapping[cid].append(m.cell_id)

    order = [leaf for leaf in recipe.leaf_cells if leaf.cell_id in fine_w]
    cols: dict[str, list[Any]] = {f.name: [] for f in POPULATION_SCHEMA}
    for i, leaf in enumerate(order):
        w = fine_w[leaf.cell_id]
        if w.numerator > INT64_MAX or w.denominator > INT64_MAX:
            raise ValidationProblem("refined weight not representable in int64")
        cols["record_id"].append(i)
        cols["cell_id"].append(leaf.cell_id)
        cols["age_code"].append(leaf.age_code)
        cols["age_years"].append(leaf.age_years)
        cols["is_top_coded"].append(leaf.is_top_coded)
        cols["is_unknown_age"].append(leaf.is_unknown_age)
        cols["sex_code"].append(leaf.sex_code)
        cols["weight"].append(w.numerator / w.denominator)
        cols["weight_num"].append(w.numerator)
        cols["weight_den"].append(w.denominator)

    frozen_mapping = {k: tuple(v) for k, v in sorted(mapping.items())}
    mapping_digest = logical_hash({"operation": "refine", "dimension": dimension, "mapping": frozen_mapping})
    new_md = {
        **{k: v for k, v in md.items() if k not in ("reva.age_bands",)},
        "reva.compression": "refine",
        "reva.refined_dimension": dimension,
        "reva.mapping_digest": mapping_digest,
        "reva.recipe_digest": digest,
        "reva.open_age_lower_bound": ("" if recipe.open_age_lower_bound is None else str(recipe.open_age_lower_bound)),
        "reva.semantics": SEMANTICS_NOTE,
    }
    out = pa.Table.from_pydict(cols, schema=POPULATION_SCHEMA.with_metadata(new_md))

    # --- certificate (recomputed from the output table)
    out_cells = {k[0]: v for k, v in exact_weight_sums(out, ["cell_id"]).items()}
    leaf_err = {leaf.cell_id: abs(out_cells.get(leaf.cell_id, Fraction(0)) - leaf.count) for leaf in recipe.leaf_cells}
    measured = max(leaf_err.values(), default=Fraction(0))
    per_cell_diff = max(
        (abs(coarse_out.get(c, Fraction(0)) - w) for c, w in coarse_in.items()),
        default=Fraction(0),
    )
    groups: dict[str, list[str]] = {}
    for leaf in recipe.leaf_cells:
        groups.setdefault(leaf.sex_code, []).append(leaf.cell_id)
    tails = {
        "top_coded": [leaf.cell_id for leaf in recipe.leaf_cells if leaf.is_top_coded],
        "unknown_age": [leaf.cell_id for leaf in recipe.leaf_cells if leaf.is_unknown_age],
    }
    cert = CompressionCertificate(
        task_scope=task_scope,
        operation="refine",
        reference=digest,
        mapping_digest=mapping_digest,
        loss_metric="max_abs_leaf_weight_error_vs_recipe_counts",
        epsilon=epsilon,
        measured_error=fraction_decimal_str(measured),
        within_epsilon=measured <= Fraction(epsilon),
        subgroup_errors=group_max_errors(leaf_err, groups),
        tail_errors=group_max_errors(leaf_err, tails),
        conservation=(
            *conservation_checks(coarse_table, out),
            ConservationCheck("per_coarse_cell_weight", per_cell_diff == 0, fraction_str(per_cell_diff)),
        ),
        runtime_seconds=time.perf_counter() - t0,
        expiry_conditions=(
            "Valid only with the referenced recipe; the within-band split uses its leaf counts.",
            "If coarse weights were re-calibrated, refined leaves inherit recipe proportions "
            "within each band (no new within-band evidence).",
            "Out of scope: any dimension other than age and recorded sex.",
        ),
        unsupported_detail=(
            "exact age within Y_OPEN",
            "age of UNK records",
            "any attribute beyond age and recorded sex",
        ),
    )
    return CompressionResult(table=out, mapping=frozen_mapping, mapping_digest=mapping_digest, certificate=cert)
