"""Deterministic materialisation of weighted representative records from a recipe.

Scientific scope and limits
---------------------------
* Records are *weighted representatives* of age × recorded-sex cells. They are
  not linked to, sampled from, or descriptive of real individuals; no
  attribute beyond the cell's age code and recorded sex is implied.
* Allocation (at most ``MAX_RECORDS``): every nonzero cell receives at least one
  record; the remaining records are distributed proportionally to counts by the
  largest-remainder method using exact integer arithmetic. Ties in the
  remainder are broken by a seed-derived stable key (SHA-256 of seed and
  cell id), then by canonical cell order. The seed therefore only matters where
  remainders tie; per-cell weight totals never depend on it.
* Each record in cell *c* with *k_c* records carries the exact weight
  ``count_c / k_c`` (stored as reduced ``weight_num``/``weight_den``), so the exact
  weight sum per cell equals the source count. The float ``weight`` column is a
  convenience rounding of that fraction.
* Observed-zero cells receive no records; missing cells are not in the recipe.
* Within-cell structure (exact ages inside ``Y_OPEN`` or inside an age-band leaf
  such as ``Y_GE85``) is not represented: such records have ``age_years`` null
  and keep the source age code.
* A recipe whose totals gate is blocked (leaves do not reproduce the reported
  ``TOTAL``) is refused with ``InconsistentSourceTotals``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from reva.kernel.errors import CapabilityGap, ValidationProblem
from reva.kernel.ids import canonical_bytes, logical_hash
from reva.population.recipe import InconsistentSourceTotals, LeafCell, PopulationRecipe, fraction_str

MAX_RECORDS = 100_000
ALLOCATION_METHOD = "reva-largest-remainder-min1-v1"
TABLE_HASH_VERSION = "reva-population-rows-1"
INT64_MAX = np.iinfo(np.int64).max
SEMANTICS_NOTE = (
    "Weighted representative records of age x recorded-sex cells; not linked to real individuals."
)

POPULATION_SCHEMA = pa.schema(
    [
        pa.field("record_id", pa.int64(), nullable=False),
        pa.field("cell_id", pa.string(), nullable=False),
        pa.field("age_code", pa.string(), nullable=False),
        pa.field("age_years", pa.int16(), nullable=True),
        pa.field("is_top_coded", pa.bool_(), nullable=False),
        pa.field("is_unknown_age", pa.bool_(), nullable=False),
        pa.field("sex_code", pa.string(), nullable=False),
        pa.field("weight", pa.float64(), nullable=False),
        pa.field("weight_num", pa.int64(), nullable=False),
        pa.field("weight_den", pa.int64(), nullable=False),
    ]
)


class ResolutionError(CapabilityGap):
    """The requested record count cannot represent the recipe at its resolution."""

    code = "population_resolution_infeasible"


@dataclass(frozen=True)
class CellAllocation:
    cell_id: str
    count: int
    records: int

    @property
    def weight(self) -> Fraction:
        """Exact per-record weight ``count / records``."""
        return Fraction(self.count, self.records)


@dataclass(frozen=True)
class Allocation:
    recipe_digest: str
    n_records: int
    seed: int
    cells: tuple[CellAllocation, ...]
    method: str = ALLOCATION_METHOD

    def records_by_cell(self) -> dict[str, int]:
        return {c.cell_id: c.records for c in self.cells}

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "recipe_digest": self.recipe_digest,
            "n_records": self.n_records,
            "seed": self.seed,
            "cells": [
                {"cell_id": c.cell_id, "count": c.count, "records": c.records} for c in self.cells
            ],
        }


# --------------------------------------------------------------------------- coarsening hints


def standard_age_bands(
    width: int, open_age_lower_bound: int | None, max_single_age: int
) -> list[tuple[str, int, int | None]]:
    """Eurostat-style bands of ``width`` years with an open-ended last band.

    The open band starts at or below ``open_age_lower_bound`` (the source's
    ``Y_OPEN`` lower bound, or the lower bound of an open band leaf) so the
    top group is never split. ``max_single_age`` is the highest finite age
    covered when there is no open top group.
    """
    top = open_age_lower_bound if open_age_lower_bound is not None else max_single_age + 1
    last = max((top // width) * width, 0)
    bands: list[tuple[str, int, int | None]] = []
    for lo in range(0, last, width):
        hi = lo + width - 1
        label = f"Y_LT{hi + 1}" if lo == 0 else f"Y{lo}-{hi}"
        bands.append((label, lo, hi))
    bands.append((f"Y_GE{last}", last, None))
    return bands


def band_for_leaf(
    leaf: LeafCell, bands: Sequence[tuple[str, int, int | None]], open_age_lower_bound: int | None
) -> str | None:
    """Label of the band that wholly contains ``leaf``'s age range, or ``None``.

    ``UNK`` maps to ``"UNK"``. ``Y_OPEN`` maps to an open-ended band starting at or
    below its lower bound; with an unknown bound it maps nowhere (refused).
    """
    if leaf.is_unknown_age:
        return "UNK"
    if leaf.is_top_coded:
        lo_leaf: int | None = open_age_lower_bound
        hi_leaf: int | None = None
    else:
        lo_leaf, hi_leaf = leaf.age_lo, leaf.age_hi
        if lo_leaf is None and leaf.age_years is not None:
            lo_leaf = hi_leaf = leaf.age_years
    if lo_leaf is None:
        return None
    for label, lo, hi in bands:
        if lo <= lo_leaf and (hi is None or (hi_leaf is not None and hi_leaf <= hi)):
            return label
    return None


def _band_cell_count(recipe: PopulationRecipe, bands: Sequence[tuple[str, int, int | None]]) -> int | None:
    """Nonzero coarse cells under ``bands``; ``None`` if some leaf would be split."""
    nonzero: set[tuple[str, str]] = set()
    for c in recipe.nonzero_cells:
        label = band_for_leaf(c, bands, recipe.open_age_lower_bound)
        if label is None:
            return None
        nonzero.add((c.sex_code, label))
    return len(nonzero)


def _open_top(recipe: PopulationRecipe) -> tuple[int | None, bool]:
    """(lower bound of the open-ended top leaf, whether that bound is known)."""
    has_open = False
    for c in recipe.leaf_cells:
        if c.is_top_coded:
            return recipe.open_age_lower_bound, recipe.open_age_lower_bound is not None
        if c.age_hi is None and c.age_lo is not None:
            has_open = True
            return c.age_lo, True
    return None, not has_open


def suggest_coarsening(recipe: PopulationRecipe, n_records: int) -> list[dict[str, Any]]:
    """Coarser age schemes (sex kept) with the minimum record count each needs.

    A scheme that would split a leaf (an age band leaf, or ``Y_OPEN`` whose
    lower bound is unknown) is reported as not applicable rather than guessed.
    """
    finite = [c.age_hi if c.age_hi is not None else c.age_years for c in recipe.leaf_cells]
    finite_his = [h for h in finite if h is not None]
    max_finite = max(finite_his) if finite_his else 0
    open_lo, open_known = _open_top(recipe)
    schemes: list[tuple[str, list[tuple[str, int, int | None]]]] = [
        (f"{w}_year_age_bands", standard_age_bands(w, open_lo, max_finite)) for w in (5, 10, 20)
    ]
    if open_lo is None or open_lo >= 65:
        broad: list[tuple[str, int, int | None]] = [
            ("Y_LT15", 0, 14),
            ("Y15-64", 15, 64),
            ("Y_GE65", 65, None),
        ]
        schemes.append(("broad_age_groups", broad))
    out = []
    for name, bands in schemes:
        m = _band_cell_count(recipe, bands) if open_known else None
        entry: dict[str, Any] = {
            "scheme": name,
            "age_bands": [[label, lo, hi] for label, lo, hi in bands],
            "minimum_feasible_records": m,
            "feasible_for_requested": m is not None and m <= n_records,
            "how": "materialise at >= the recipe minimum, then compression.coarsen(...)",
        }
        if m is None:
            entry["not_applicable_reason"] = (
                "Y_OPEN lower bound is unknown; an open band cannot be placed without guessing"
                if not open_known
                else "the scheme would split an age-band leaf of the recipe"
            )
        out.append(entry)
    return out


# --------------------------------------------------------------------------- allocation


def _tie_key(seed: int, cell_id: str) -> bytes:
    return hashlib.sha256(f"{seed}\x1f{cell_id}".encode()).digest()


def allocate(recipe: PopulationRecipe, n_records: int) -> Allocation:
    """Allocate ``n_records`` representatives to the recipe's nonzero leaf cells."""
    if isinstance(n_records, bool) or not isinstance(n_records, int) or n_records <= 0:
        raise ValidationProblem("n_records must be a positive int", n_records=n_records)
    if n_records > MAX_RECORDS:
        raise ResolutionError(
            f"n_records={n_records} exceeds the supported maximum of {MAX_RECORDS}",
            requested=n_records,
            maximum_records=MAX_RECORDS,
        )
    if recipe.is_blocked:
        failing = [c.to_dict() for c in recipe.total_checks if c.status == "inconsistent"]
        raise InconsistentSourceTotals(
            "recipe is blocked: its leaf cells do not reproduce the reported TOTAL and no "
            "documented explanation was recorded; refusing to materialise",
            failing_checks=failing,
        )
    nonzero: list[LeafCell] = list(recipe.nonzero_cells)
    m = len(nonzero)
    if m == 0:
        raise ValidationProblem("recipe has no nonzero cells to represent")
    if n_records < m:
        suggestions = suggest_coarsening(recipe, n_records)
        feasible = [s["scheme"] for s in suggestions if s["feasible_for_requested"]]
        raise ResolutionError(
            f"n_records={n_records} is smaller than the {m} nonzero age x sex cells; every "
            "nonzero cell needs at least one representative. Coarsen the age resolution "
            f"(e.g. {feasible[0] if feasible else '5_year_age_bands'}) or request >= {m}.",
            requested=n_records,
            minimum_feasible_records=m,
            suggested_coarsening=suggestions,
        )
    total = sum(c.count for c in nonzero)
    remainder = n_records - m
    base: list[int] = []
    rems: list[int] = []
    for c in nonzero:
        q, r = divmod(remainder * c.count, total)
        base.append(1 + q)
        rems.append(r)
    leftover = n_records - sum(base)
    order = sorted(range(m), key=lambda i: (-rems[i], _tie_key(recipe.seed, nonzero[i].cell_id), i))
    for i in order[:leftover]:
        base[i] += 1
    cells = tuple(
        CellAllocation(cell_id=c.cell_id, count=c.count, records=k)
        for c, k in zip(nonzero, base, strict=True)
    )
    assert sum(base) == n_records
    return Allocation(
        recipe_digest=recipe.digest(), n_records=n_records, seed=recipe.seed, cells=cells
    )


# --------------------------------------------------------------------------- table


def materialise(recipe: PopulationRecipe, n_records: int) -> pa.Table:
    """Build the deterministic weighted-representative table (see module docstring)."""
    alloc = allocate(recipe, n_records)
    leaves = [recipe.leaf(c.cell_id) for c in alloc.cells]
    ks = np.array([c.records for c in alloc.cells], dtype=np.int64)
    nums, dens = [], []
    for c in alloc.cells:
        w = c.weight
        if w.numerator > INT64_MAX or w.denominator > INT64_MAX:
            raise ValidationProblem("weight not representable in int64", cell_id=c.cell_id)
        nums.append(w.numerator)
        dens.append(w.denominator)
    num_arr = np.repeat(np.array(nums, dtype=np.int64), ks)
    den_arr = np.repeat(np.array(dens, dtype=np.int64), ks)

    def rep(values: list[Any], dtype: Any = object) -> np.ndarray:
        return np.repeat(np.array(values, dtype=dtype), ks)

    ages = rep([lf.age_years if lf.age_years is not None else 0 for lf in leaves], np.int16)
    age_null = rep([lf.age_years is None for lf in leaves], bool)
    columns = [
        pa.array(np.arange(n_records, dtype=np.int64)),
        pa.array(rep([lf.cell_id for lf in leaves]), pa.string()),
        pa.array(rep([lf.age_code for lf in leaves]), pa.string()),
        pa.array(ages, pa.int16(), mask=age_null),
        pa.array(rep([lf.is_top_coded for lf in leaves], bool)),
        pa.array(rep([lf.is_unknown_age for lf in leaves], bool)),
        pa.array(rep([lf.sex_code for lf in leaves]), pa.string()),
        pa.array(rep([n / d for n, d in zip(nums, dens, strict=True)], np.float64)),
        pa.array(num_arr),
        pa.array(den_arr),
    ]
    metadata = {
        "reva.recipe_digest": alloc.recipe_digest,
        "reva.allocation_method": alloc.method,
        "reva.seed": str(recipe.seed),
        "reva.n_records": str(n_records),
        "reva.open_age_lower_bound": (
            "" if recipe.open_age_lower_bound is None else str(recipe.open_age_lower_bound)
        ),
        "reva.geography": recipe.geography,
        "reva.reference_date": recipe.reference_date.isoformat(),
        "reva.semantics": SEMANTICS_NOTE,
    }
    return pa.Table.from_arrays(columns, schema=POPULATION_SCHEMA.with_metadata(metadata))


def table_metadata(table: pa.Table) -> dict[str, str]:
    md = table.schema.metadata or {}
    return {k.decode(): v.decode() for k, v in md.items()}


def exact_weight_sums(table: pa.Table, keys: Sequence[str]) -> dict[tuple[Any, ...], Fraction]:
    """Exact ``Σ weight_num/weight_den`` grouped by ``keys`` (Fraction arithmetic)."""
    keys = list(keys)
    if table.num_rows == 0:
        return {}
    grouped = table.group_by([*keys, "weight_num", "weight_den"], use_threads=False).aggregate(
        [("weight_num", "count")]
    )
    cols = {name: grouped.column(name).to_pylist() for name in grouped.column_names}
    out: dict[tuple[Any, ...], Fraction] = {}
    counts = cols["weight_num_count"]
    for i in range(grouped.num_rows):
        key = tuple(cols[k][i] for k in keys)
        den = cols["weight_den"][i]
        if den <= 0:
            raise ValidationProblem("weight_den must be positive")
        out[key] = out.get(key, Fraction(0)) + Fraction(cols["weight_num"][i], den) * counts[i]
    return out


def exact_total_weight(table: pa.Table) -> Fraction:
    return sum(exact_weight_sums(table, []).values(), Fraction(0))


def canonical_summary(table: pa.Table) -> dict[str, Any]:
    """Order-independent summary: per-cell record counts and exact weight totals."""
    sums = exact_weight_sums(table, ["cell_id"])
    counts_tbl = table.group_by(["cell_id"], use_threads=False).aggregate([("record_id", "count")])
    counts = dict(
        zip(
            counts_tbl.column("cell_id").to_pylist(),
            counts_tbl.column("record_id_count").to_pylist(),
            strict=True,
        )
    )
    return {
        "version": TABLE_HASH_VERSION,
        "n_records": table.num_rows,
        "recipe_digest": table_metadata(table).get("reva.recipe_digest"),
        "cells": {
            cid: {"records": counts[cid], "weight_sum": fraction_str(sums[(cid,)])}
            for cid in sorted(counts)
        },
        "total_weight": fraction_str(sum(sums.values(), Fraction(0))),
    }


def summary_logical_hash(table: pa.Table) -> str:
    return logical_hash(canonical_summary(table))


def table_logical_hash(table: pa.Table) -> str:
    """SHA-256 over the canonically encoded, sorted rows (independent of Parquet bytes)."""
    names = table.column_names
    pylists = [table.column(n).to_pylist() for n in names]
    encoded = sorted(canonical_bytes(list(row)) for row in zip(*pylists, strict=True))
    h = hashlib.sha256()
    h.update(TABLE_HASH_VERSION.encode("ascii"))
    h.update(b"\x00")
    h.update(canonical_bytes({"columns": names, "types": [str(t) for t in table.schema.types]}))
    for row in encoded:
        h.update(len(row).to_bytes(8, "big"))
        h.update(row)
    return h.hexdigest()


def to_parquet_bytes(table: pa.Table) -> bytes:
    """Parquet (zstd level 3, fixed writer settings) for byte-stable output per pyarrow version."""
    sink = pa.BufferOutputStream()
    pq.write_table(
        table,
        sink,
        compression="zstd",
        compression_level=3,
        use_dictionary=True,
        write_statistics=True,
        version="2.6",
        data_page_version="1.0",
        row_group_size=1 << 20,
    )
    return bytes(sink.getvalue().to_pybytes())
