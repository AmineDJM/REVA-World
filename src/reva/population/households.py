"""Household recipe and weighted representative households from public margins (P10, V2 agent PEN).

Scientific scope and limits
---------------------------
* Source: Eurostat ``lfst_hhnhtych`` (LFS private households, thousands) for one country and
  year - household composition (one adult ``A1``, couple ``CPL``, other ``OTH``, each with or
  without resident children; ``NRP`` no response) x number of resident children (0, 1, 2, 3+).
  The leaf cells are the joint composition x children cells published by the source; that
  joint distribution is the only association used.
* **Structural zeros** are cells impossible by definition: a household "without resident
  children" with 1+ children, and one "with resident children" with 0 children. If the
  source reports a non-zero value for such a cell the recipe is refused
  (``InconsistentHouseholdSource``); a missing value there is a structural zero, not Missing.
* Any other missing leaf (Eurostat ``:``, e.g. a suppressed low-reliability cell) stays
  Missing: it is excluded and listed, never imputed, and the totals gate reports the
  unexplained remainder.
* **Household size** of a cell follows from the category definitions only: exact for one
  adult / couple with 0, 1 or 2 children; a lower bound for 3+ children, "other" households
  (at least two adults, upper size unknown) and no-response households. No size is drawn or
  imputed inside an interval; records carry ``size_lo``/``size_hi`` (``None`` = open).
* **Totals gate.** The leaves must reproduce the published ``TOTAL x TOTAL`` within the
  published rounding (each cell is rounded to 0.1 thousand: tolerance 50 households per
  leaf). Otherwise the recipe is refused unless an explanation is recorded.
* **Held-out check.** EU-SILC ``ilc_lvph03`` (distribution of households by size) and
  ``ilc_lvph01`` (average size) - a different survey, never used for synthesis - are compared
  with the size *intervals* implied by the LFS cells: per size class the SILC share must lie
  inside the LFS-implied [lower, upper] share; misses are reported with their distance.
* Records are weighted representatives of household cells. They are **not linked to
  persons**: no member ages, sexes or other attributes exist (the person recipe stays
  age x recorded sex only; no joint person x household data is used). Income, health and
  every other attribute are unsupported.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from typing import Any

import numpy as np
import pyarrow as pa

from reva.kernel.errors import CapabilityGap, ValidationProblem
from reva.kernel.ids import logical_hash
from reva.population.recipe import fraction_decimal_str, fraction_str

HOUSEHOLD_METHOD_VERSION = "reva-household-recipe-1"
HOUSEHOLD_ALLOCATION = "reva-largest-remainder-min1-v1"
HOUSEHOLD_DIAGNOSTICS_VERSION = "reva-household-diagnostics-1"
MAX_HOUSEHOLD_RECORDS = 100_000
SOURCE_TABLE = "lfst_hhnhtych"
LEAF_COMPOSITIONS = ("A1_NCH", "A1_CH", "CPL_NCH", "CPL_CH", "OTH_NCH", "OTH_CH", "NRP")
CHILD_CODES = ("0", "1", "2", "GE3")
TOTAL_TOLERANCE_PER_LEAF = 50  # households: each published cell is rounded to 0.1 thousand
SIZE_CLASSES = ("1", "2", "3", "4", "5", "GE6")
SUPPORTED_ATTRIBUTES = ("household_composition", "number_of_resident_children", "household_size_interval")
UNSUPPORTED_ASSOCIATIONS = (
    "household x member age or recorded sex (no joint person-household source)",
    "household x income, tenure, health, ethnicity or any sensitive attribute",
    "exact size inside an open size interval (3+ children, other and no-response households)",
    "sub-national geography",
    "linkage of synthetic households to synthetic persons",
)

COMPOSITION_LABELS = {
    "A1_NCH": "one adult without resident children",
    "A1_CH": "one adult with resident children",
    "CPL_NCH": "couple without resident children",
    "CPL_CH": "couple with resident children",
    "OTH_NCH": "other household type without resident children",
    "OTH_CH": "other household type with resident children",
    "NRP": "no response (composition unknown)",
}


class InconsistentHouseholdSource(ValidationProblem):
    code = "household_source_inconsistent"


class HouseholdResolutionError(CapabilityGap):
    code = "household_resolution_infeasible"


def child_count(code: str) -> tuple[int, bool]:
    """(number of children, open) for an ``n_child`` code."""
    if code == "GE3":
        return 3, True
    if code in ("0", "1", "2"):
        return int(code), False
    raise ValidationProblem(f"unknown n_child code {code!r}")


def is_structural_zero(comp: str, n_child: str) -> bool:
    if comp.endswith("_NCH"):
        return n_child != "0"
    if comp.endswith("_CH"):
        return n_child == "0"
    return False


def size_bounds(comp: str, n_child: str) -> tuple[int, int | None, int, int | None]:
    """(size_lo, size_hi, adults_lo, adults_hi) from the category definitions (None = open)."""
    k, open_k = child_count(n_child)
    if comp.startswith("A1"):
        adults_lo, adults_hi = 1, 1
    elif comp.startswith("CPL"):
        adults_lo, adults_hi = 2, 2
    elif comp.startswith("OTH"):
        adults_lo, adults_hi = 2, None
    elif comp == "NRP":
        adults_lo, adults_hi = 0, None
    else:
        raise ValidationProblem(f"unknown composition {comp!r}")
    lo = max(1, adults_lo + k)
    hi = None if (open_k or adults_hi is None) else adults_hi + k
    return lo, hi, adults_lo, adults_hi


@dataclass(frozen=True)
class HouseholdCell:
    cell_id: str
    composition: str
    n_child: str
    households: int  # exact count (thousands x 1000)
    size_lo: int
    size_hi: int | None
    adults_lo: int
    adults_hi: int | None
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell_id": self.cell_id,
            "composition": self.composition,
            "composition_label": COMPOSITION_LABELS[self.composition],
            "n_child": self.n_child,
            "households": self.households,
            "size_lo": self.size_lo,
            "size_hi": self.size_hi,
            "adults_lo": self.adults_lo,
            "adults_hi": self.adults_hi,
            "status": self.status,
        }


@dataclass(frozen=True, kw_only=True)
class HouseholdRecipe:
    geography: str
    year: int
    source_ref: Mapping[str, Any]
    cells: tuple[HouseholdCell, ...]
    structural_zeros: tuple[str, ...]
    observed_zero_cells: tuple[str, ...]
    missing_cells: tuple[dict[str, Any], ...]
    reported_total: int | None
    totals_check: Mapping[str, Any]
    seed: int
    accepted_inconsistency: str | None = None
    method_version: str = HOUSEHOLD_METHOD_VERSION

    @property
    def total_households(self) -> int:
        return sum(c.households for c in self.cells)

    def persons_bounds(self) -> tuple[int, int | None]:
        lo = sum(c.households * c.size_lo for c in self.cells)
        if any(c.size_hi is None for c in self.cells if c.households):
            return lo, None
        return lo, sum(c.households * (c.size_hi or 0) for c in self.cells)

    def to_dict(self) -> dict[str, Any]:
        lo, hi = self.persons_bounds()
        return {
            "method_version": self.method_version,
            "geography": self.geography,
            "year": self.year,
            "source_ref": dict(self.source_ref),
            "supported_attributes": list(SUPPORTED_ATTRIBUTES),
            "cells": [c.to_dict() for c in self.cells],
            "structural_zeros": list(self.structural_zeros),
            "observed_zero_cells": list(self.observed_zero_cells),
            "missing_cells": list(self.missing_cells),
            "reported_total": self.reported_total,
            "total_households": self.total_households,
            "totals_check": dict(self.totals_check),
            "accepted_inconsistency": self.accepted_inconsistency,
            "persons_in_households_bounds": {"lower": lo, "upper": hi, "kind": "bounds"},
            "seed": self.seed,
        }

    def digest(self) -> str:
        return logical_hash(self.to_dict())


def _households(value: str) -> int:
    d = Decimal(value) * 1000
    if d != d.to_integral_value() or d < 0:
        raise ValidationProblem(f"household count {value} thousand is not a non-negative whole number of households")
    return int(d)


def build_household_recipe(
    rows: Sequence[Mapping[str, Any]],
    *,
    geography: str,
    year: int,
    source_ref: Mapping[str, Any],
    seed: int,
    inconsistency_explanation: str | None = None,
) -> HouseholdRecipe:
    """Rows: the ``lfst_hhnhtych`` dataset-input table of one country (dim1 = phhcomp, dim2 = n_child)."""
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValidationProblem("seed must be a non-negative int")
    by: dict[tuple[str, str], Mapping[str, Any]] = {}
    r: Mapping[str, Any] | None
    for r in rows:
        if r.get("year") != year:
            continue
        if r.get("dim1_name") != "phhcomp" or r.get("dim2_name") != "n_child":
            raise ValidationProblem("rows are not lfst_hhnhtych dataset rows (phhcomp x n_child)")
        key = (str(r["dim1"]), str(r["dim2"]))
        if key in by:
            raise ValidationProblem(f"duplicate cell {key}")
        by[key] = r
    if not by:
        raise ValidationProblem(f"no {SOURCE_TABLE} rows for {geography} {year}")
    cells: list[HouseholdCell] = []
    structural: list[str] = []
    missing: list[dict[str, Any]] = []
    for comp in LEAF_COMPOSITIONS:
        for k in CHILD_CODES:
            cid = f"{comp}|{k}"
            r = by.get((comp, k))
            v = None if r is None else r.get("value")
            if is_structural_zero(comp, k):
                if v is not None and Decimal(v) != 0:
                    raise InconsistentHouseholdSource(
                        f"{geography} {year}: {cid} is impossible by definition but the source reports {v} thousand",
                        cell=cid,
                        value=v,
                    )
                structural.append(cid)
                continue
            if v is None:
                missing.append(
                    {
                        "cell_id": cid,
                        "reason": (r or {}).get("missing_reason") or "absent_from_source",
                        "status": (r or {}).get("status", ""),
                    }
                )
                continue
            assert r is not None
            lo, hi, alo, ahi = size_bounds(comp, k)
            cells.append(HouseholdCell(cid, comp, k, _households(v), lo, hi, alo, ahi, str(r.get("status") or "")))
    tot_row = by.get(("TOTAL", "TOTAL"))
    reported = _households(tot_row["value"]) if tot_row and tot_row.get("value") is not None else None
    leaf_sum = sum(c.households for c in cells)
    tolerance = TOTAL_TOLERANCE_PER_LEAF * (len(cells) + 1)
    if reported is None:
        check: dict[str, Any] = {"status": "not_reported"}
    else:
        diff = leaf_sum - reported
        if abs(diff) <= tolerance:
            # with missing leaves this means they hold at most the rounding tolerance together
            status = "consistent_within_rounding"
        elif missing and diff < 0:
            status = "not_computable_missing_leaves"
        else:
            status = "inconsistent"
        check = {
            "status": status,
            "reported": reported,
            "leaf_sum": leaf_sum,
            "difference": diff,
            "rounding_tolerance": tolerance,
            "unexplained_remainder_if_missing": (reported - leaf_sum) if missing else None,
        }
        if status == "inconsistent" and inconsistency_explanation is None:
            raise InconsistentHouseholdSource(
                f"{geography} {year}: household leaves sum to {leaf_sum} but the source TOTAL is {reported} "
                f"(tolerance {tolerance}); record an explanation to proceed",
                totals_check=check,
            )
    if not cells:
        raise ValidationProblem("no observed household leaf cells")
    return HouseholdRecipe(
        geography=geography,
        year=year,
        source_ref=source_ref,
        cells=tuple(cells),
        structural_zeros=tuple(structural),
        observed_zero_cells=tuple(c.cell_id for c in cells if c.households == 0),
        missing_cells=tuple(missing),
        reported_total=reported,
        totals_check=check,
        seed=seed,
        accepted_inconsistency=inconsistency_explanation if check.get("status") == "inconsistent" else None,
    )


# --------------------------------------------------------------------------- materialisation
def _tie_key(seed: int, cell_id: str) -> bytes:
    return hashlib.sha256(f"{seed}|{cell_id}".encode()).digest()


def allocate_households(recipe: HouseholdRecipe, n_records: int) -> dict[str, int]:
    """Largest-remainder allocation, at least one record per nonzero cell (exact integer arithmetic)."""
    nz = [c for c in recipe.cells if c.households > 0]
    if not 1 <= n_records <= MAX_HOUSEHOLD_RECORDS:
        raise ValidationProblem(f"n_records must be in [1, {MAX_HOUSEHOLD_RECORDS}]")
    if n_records < len(nz):
        raise HouseholdResolutionError(
            f"{n_records} records cannot represent {len(nz)} nonzero household cells", required_min=len(nz)
        )
    total = sum(c.households for c in nz)
    extra = n_records - len(nz)
    base = {c.cell_id: 1 + (extra * c.households) // total for c in nz}
    rem = sorted(nz, key=lambda c: (-((extra * c.households) % total), _tie_key(recipe.seed, c.cell_id), c.cell_id))
    left = n_records - sum(base.values())
    for c in rem[:left]:
        base[c.cell_id] += 1
    return base


HOUSEHOLD_SCHEMA = pa.schema(
    [
        pa.field("household_id", pa.int64(), nullable=False),
        pa.field("cell_id", pa.string(), nullable=False),
        pa.field("composition", pa.string(), nullable=False),
        pa.field("n_child", pa.string(), nullable=False),
        pa.field("size_lo", pa.int16(), nullable=False),
        pa.field("size_hi", pa.int16(), nullable=True),
        pa.field("adults_lo", pa.int16(), nullable=False),
        pa.field("adults_hi", pa.int16(), nullable=True),
        pa.field("weight", pa.float64(), nullable=False),
        pa.field("weight_num", pa.int64(), nullable=False),
        pa.field("weight_den", pa.int64(), nullable=False),
    ]
)


def materialise_households(recipe: HouseholdRecipe, n_records: int) -> pa.Table:
    if recipe.totals_check.get("status") == "inconsistent" and not recipe.accepted_inconsistency:
        raise InconsistentHouseholdSource("recipe totals gate is blocked")
    alloc = allocate_households(recipe, n_records)
    cols: dict[str, list[Any]] = {f.name: [] for f in HOUSEHOLD_SCHEMA}
    hid = 0
    for c in recipe.cells:
        k = alloc.get(c.cell_id, 0)
        if k == 0:
            continue
        w = Fraction(c.households, k)
        for _ in range(k):
            cols["household_id"].append(hid)
            cols["cell_id"].append(c.cell_id)
            cols["composition"].append(c.composition)
            cols["n_child"].append(c.n_child)
            cols["size_lo"].append(c.size_lo)
            cols["size_hi"].append(c.size_hi)
            cols["adults_lo"].append(c.adults_lo)
            cols["adults_hi"].append(c.adults_hi)
            cols["weight"].append(w.numerator / w.denominator)
            cols["weight_num"].append(w.numerator)
            cols["weight_den"].append(w.denominator)
            hid += 1
    return pa.table(cols, schema=HOUSEHOLD_SCHEMA)


def exact_sums(table: pa.Table, key: str) -> dict[str, Fraction]:
    out: dict[str, Fraction] = {}
    for k, n, d in zip(
        table.column(key).to_pylist(),
        table.column("weight_num").to_pylist(),
        table.column("weight_den").to_pylist(),
        strict=True,
    ):
        out[k] = out.get(k, Fraction(0)) + Fraction(n, d)
    return out


# --------------------------------------------------------------------------- diagnostics
def _margin(targets: Mapping[str, Fraction], est: Mapping[str, Fraction]) -> dict[str, Any]:
    worst, max_abs = None, Fraction(0)
    for k in sorted(set(targets) | set(est)):
        e = abs(est.get(k, Fraction(0)) - targets.get(k, Fraction(0)))
        if e > max_abs:
            worst, max_abs = k, e
    return {
        "n_categories": len(set(targets) | set(est)),
        "max_abs_error": fraction_str(max_abs),
        "worst_category": worst,
    }


def size_share_bounds(recipe: HouseholdRecipe) -> dict[str, tuple[Fraction, Fraction]]:
    """Per SILC size class, the [lower, upper] share of households the LFS cells allow."""
    total = Fraction(recipe.total_households)
    out = {}
    for cls in SIZE_CLASSES:
        lo_s = 6 if cls == "GE6" else int(cls)
        hi_s = None if cls == "GE6" else int(cls)
        certain = possible = 0
        for c in recipe.cells:
            c_hi = c.size_hi
            within_lo = c.size_lo >= lo_s and (hi_s is None or (c_hi is not None and c_hi <= hi_s))
            overlaps = (hi_s is None or c.size_lo <= hi_s) and (c_hi is None or c_hi >= lo_s)
            if within_lo:
                certain += c.households
            if overlaps:
                possible += c.households
        out[cls] = (Fraction(certain) / total, Fraction(possible) / total)
    return out


def held_out_silc(
    recipe: HouseholdRecipe, size_rows: Sequence[Mapping[str, Any]], avg_rows: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Score the LFS-implied size intervals against EU-SILC (held out: never used for synthesis)."""
    shares = {r["dim1"]: r for r in size_rows if r.get("year") == recipe.year}
    bounds = size_share_bounds(recipe)
    classes = []
    n_ok = n_scored = 0
    for cls in SIZE_CLASSES:
        lo, hi = bounds[cls]
        r = shares.get(cls)
        entry: dict[str, Any] = {
            "size_class": cls,
            "lfs_implied_share_lower": fraction_decimal_str(lo, 6),
            "lfs_implied_share_upper": fraction_decimal_str(hi, 6),
        }
        if r is None or r.get("value") is None:
            entry["silc_share"] = None
            entry["status"] = "not_scored_missing"
        else:
            s = Fraction(Decimal(r["value"])) / 100
            entry["silc_share"] = fraction_decimal_str(s, 6)
            entry["silc_flag"] = r.get("status", "")
            dist = Fraction(0) if lo <= s <= hi else min(abs(s - lo), abs(s - hi))
            entry["distance_outside_interval"] = fraction_decimal_str(dist, 6)
            entry["status"] = "inside" if dist == 0 else "outside"
            n_scored += 1
            n_ok += dist == 0
        classes.append(entry)
    avg = next((r for r in avg_rows if r.get("year") == recipe.year and r.get("value") is not None), None)
    lo_p, hi_p = recipe.persons_bounds()
    avg_entry: dict[str, Any] = {
        "lfs_implied_average_lower": fraction_decimal_str(Fraction(lo_p, recipe.total_households), 6),
        "lfs_implied_average_upper": None
        if hi_p is None
        else fraction_decimal_str(Fraction(hi_p, recipe.total_households), 6),
    }
    if avg is not None:
        a = Fraction(Decimal(avg["value"]))
        avg_entry["silc_average"] = avg["value"]
        avg_entry["status"] = (
            "inside"
            if a >= Fraction(lo_p, recipe.total_households)
            and (hi_p is None or a <= Fraction(hi_p, recipe.total_households))
            else "outside"
        )
    return {
        "source": "EU-SILC ilc_lvph03 / ilc_lvph01 (different survey; held out)",
        "size_classes": classes,
        "scored": n_scored,
        "inside": n_ok,
        "average_size": avg_entry,
        "note": (
            "The LFS and SILC household universes and sampling differ; an 'outside' class is a "
            "discrepancy between sources, reported, never corrected."
        ),
    }


def diagnose_households(
    table: pa.Table, recipe: HouseholdRecipe, held_out: Mapping[str, Any] | None = None, person_total: int | None = None
) -> dict[str, Any]:
    n = table.num_rows
    if n == 0:
        raise ValueError("empty table")
    w = table.column("weight").to_numpy()
    if not np.all(np.isfinite(w)) or np.any(w < 0):
        raise ValueError("weights must be finite and non-negative")
    s1, s2 = float(w.sum()), float((w * w).sum())
    ws = np.sort(w)
    idx = np.arange(1, n + 1, dtype=np.float64)
    gini = float((2.0 * np.sum(idx * ws)) / (n * s1) - (n + 1.0) / n)
    top_k = max(1, math.ceil(0.01 * n))
    joint = exact_sums(table, "cell_id")
    comp = exact_sums(table, "composition")
    kids = exact_sums(table, "n_child")
    jt = {c.cell_id: Fraction(c.households) for c in recipe.cells}
    ct: dict[str, Fraction] = {}
    kt: dict[str, Fraction] = {}
    for c in recipe.cells:
        ct[c.composition] = ct.get(c.composition, Fraction(0)) + c.households
        kt[c.n_child] = kt.get(c.n_child, Fraction(0)) + c.households
    cells = table.column("cell_id").to_pylist()
    in_struct = sum(1 for c in cells if c in set(recipe.structural_zeros))
    in_zero = sum(1 for c in cells if c in set(recipe.observed_zero_cells))
    known = {c.cell_id for c in recipe.cells}
    outside = sum(1 for c in cells if c not in known)
    lo_p, hi_p = recipe.persons_bounds()
    out = {
        "version": HOUSEHOLD_DIAGNOSTICS_VERSION,
        "recipe_digest": recipe.digest(),
        "n_records": n,
        "total_weight": fraction_str(sum(joint.values(), Fraction(0))),
        "recipe_total_households": recipe.total_households,
        "effective_sample_size": s1 * s1 / s2,
        "weight_concentration": {
            "gini_record_weights": gini,
            "top_1pct_record_weight_share": float(np.sum(ws[-top_k:]) / s1),
        },
        "calibration_error": {
            "joint_composition_children": _margin(jt, joint),
            "composition_margin": _margin(ct, comp),
            "children_margin": _margin(kt, kids),
            "basis": "exact weight_num/weight_den sums vs recipe cell counts",
        },
        "support_violations": {
            "records_in_structural_zero_cells": in_struct,
            "records_in_observed_zero_cells": in_zero,
            "records_outside_recipe_cells": outside,
            "ok": in_struct == 0 and in_zero == 0 and outside == 0,
        },
        "structural_constraints": {
            "structural_zero_cells": list(recipe.structural_zeros),
            "size_rule": "size and adult counts follow from category definitions (exact or lower bounds)",
        },
        "totals_check": dict(recipe.totals_check),
        "missing_cells": list(recipe.missing_cells),
        "persons_in_households_bounds": {
            "lower": lo_p,
            "upper": hi_p,
            "kind": "bounds",
            "note": "open intervals (3+ children, other, no response) are not imputed",
        },
        "supported_associations": ["household composition x number of resident children (joint, source cells)"],
        "unsupported_associations": [{"association": a, "status": "unsupported"} for a in UNSUPPORTED_ASSOCIATIONS],
    }
    if held_out is not None:
        out["held_out"] = dict(held_out)
    if person_total is not None:
        out["person_population_check"] = {
            "person_recipe_total": person_total,
            "persons_in_households_lower_bound": lo_p,
            "status": "consistent" if lo_p <= person_total else "inconsistent",
            "note": (
                "persons in private households (lower bound) must not exceed the resident population; "
                "the two sources differ in universe (private households vs usually resident population)"
            ),
        }
    return out


@dataclass
class HouseholdSynthesis:
    recipe: HouseholdRecipe
    table: pa.Table
    diagnostics: dict[str, Any] = field(default_factory=dict)


def synthesise_households(
    rows: Sequence[Mapping[str, Any]],
    *,
    geography: str,
    year: int,
    seed: int,
    n_records: int,
    source_ref: Mapping[str, Any],
    size_rows: Sequence[Mapping[str, Any]] = (),
    avg_rows: Sequence[Mapping[str, Any]] = (),
    person_total: int | None = None,
    inconsistency_explanation: str | None = None,
) -> HouseholdSynthesis:
    recipe = build_household_recipe(
        rows,
        geography=geography,
        year=year,
        source_ref=source_ref,
        seed=seed,
        inconsistency_explanation=inconsistency_explanation,
    )
    table = materialise_households(recipe, n_records)
    held = held_out_silc(recipe, size_rows, avg_rows) if size_rows else None
    return HouseholdSynthesis(recipe, table, diagnose_households(table, recipe, held, person_total))
