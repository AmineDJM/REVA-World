"""Coarsening of a materialised population table into age-band × recorded-sex cells.

Scientific scope and limits
---------------------------
* Coarsening only merges existing age leaves (single years, or age-band leaves
  such as ``Y_GE85`` when the source has nothing finer) into bands; it keeps
  recorded sex unchanged. ``UNK`` (unknown age) is never merged into a band, an
  age-band leaf must lie wholly inside one target band, and the top-coded
  ``Y_OPEN`` group may only join an open-ended band that starts at or below its
  lower bound (it is never split or assigned an age). An unknown ``Y_OPEN``
  lower bound refuses the operation rather than guessing.
* Each coarse cell becomes one representative record whose exact weight
  (``weight_num``/``weight_den``) is the exact Fraction sum of its fine records'
  weights, so totals per coarse cell, per sex and overall are conserved exactly.
* Single-year detail inside a band is lost. It can be restored only along the
  already-supported age dimension using the source recipe's leaf counts
  (see ``reva.compression.refine``).
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

import pyarrow as pa

from reva.compression.certificates import (
    CompressionCertificate,
    ConservationCheck,
    conservation_checks,
    group_max_errors,
)
from reva.kernel.errors import ValidationProblem
from reva.kernel.ids import logical_hash
from reva.population.materialise import INT64_MAX, exact_weight_sums, table_metadata
from reva.population.recipe import fraction_decimal_str, fraction_str, parse_age_code

UNK_LABEL = "UNK"
SEX_ORDER = ("F", "M")

COARSE_SCHEMA = pa.schema(
    [
        pa.field("record_id", pa.int64(), nullable=False),
        pa.field("cell_id", pa.string(), nullable=False),
        pa.field("age_band", pa.string(), nullable=False),
        pa.field("age_lo", pa.int16(), nullable=True),
        pa.field("age_hi", pa.int16(), nullable=True),
        pa.field("includes_top_coded", pa.bool_(), nullable=False),
        pa.field("is_unknown_age", pa.bool_(), nullable=False),
        pa.field("sex_code", pa.string(), nullable=False),
        pa.field("weight", pa.float64(), nullable=False),
        pa.field("weight_num", pa.int64(), nullable=False),
        pa.field("weight_den", pa.int64(), nullable=False),
        pa.field("n_fine_records", pa.int64(), nullable=False),
        pa.field("n_fine_cells", pa.int64(), nullable=False),
    ]
)


@dataclass(frozen=True)
class AgeBand:
    label: str
    lo: int
    hi: int | None

    def contains(self, age: int) -> bool:
        return age >= self.lo and (self.hi is None or age <= self.hi)


@dataclass(frozen=True)
class CompressionResult:
    """Output table, coarse→fine (or fine→coarse) cell mapping, and certificate."""

    table: pa.Table
    mapping: Mapping[str, tuple[str, ...]]
    mapping_digest: str
    certificate: CompressionCertificate


def validate_bands(age_bands: Sequence[tuple[str, int, int | None]]) -> tuple[AgeBand, ...]:
    """Bands must be uniquely labelled, non-overlapping, and only the last may be open."""
    bands = sorted((AgeBand(str(lab), int(lo), hi) for lab, lo, hi in age_bands), key=lambda b: b.lo)
    if not bands:
        raise ValidationProblem("at least one age band is required")
    labels = [b.label for b in bands]
    if len(set(labels)) != len(labels) or UNK_LABEL in labels:
        raise ValidationProblem("band labels must be unique and not 'UNK'", labels=labels)
    for i, b in enumerate(bands):
        if b.lo < 0 or (b.hi is not None and b.hi < b.lo):
            raise ValidationProblem(f"invalid band {b.label!r}")
        if b.hi is None and i != len(bands) - 1:
            raise ValidationProblem("only the last (highest) band may be open-ended")
        prev_hi = bands[i - 1].hi if i else None
        if i and (prev_hi is None or prev_hi >= b.lo):
            raise ValidationProblem(f"bands overlap at {b.label!r}")
    return tuple(bands)


def _check_int64(f: Fraction, what: str) -> None:
    if f.numerator > INT64_MAX or f.denominator > INT64_MAX or f.numerator < 0:
        raise ValidationProblem(f"exact weight of {what} not representable in int64")


def coarsen(
    table: pa.Table,
    age_bands: Sequence[tuple[str, int, int | None]],
    *,
    open_age_lower_bound: int | None = None,
    task_scope: str = "age x recorded-sex weight totals",
    epsilon: str = "0",
) -> CompressionResult:
    """Aggregate a fine population table into age band × recorded sex cells.

    ``age_bands`` is a sequence of ``(label, lo, hi_or_None)`` with inclusive
    bounds. The Y_OPEN lower bound comes from ``open_age_lower_bound`` or the
    table metadata written by ``materialise``.
    """
    t0 = time.perf_counter()
    bands = validate_bands(age_bands)
    md = table_metadata(table)
    if open_age_lower_bound is None and md.get("reva.open_age_lower_bound"):
        open_age_lower_bound = int(md["reva.open_age_lower_bound"])

    keys = ["cell_id", "sex_code", "age_code", "age_years", "is_top_coded", "is_unknown_age"]
    fine = exact_weight_sums(table, keys)
    rec_counts_tbl = table.group_by(["cell_id"], use_threads=False).aggregate([("record_id", "count")])
    rec_counts = dict(
        zip(
            rec_counts_tbl.column("cell_id").to_pylist(),
            rec_counts_tbl.column("record_id_count").to_pylist(),
            strict=True,
        )
    )

    coarse_w: dict[tuple[str, str], Fraction] = {}
    coarse_fine: dict[tuple[str, str], list[tuple[int, str]]] = {}
    band_by_label = {b.label: b for b in bands}
    band_has_top: set[str] = set()
    for (cid, sex, _age_code, age_years, top, unk), w in fine.items():
        if unk:
            label, order = UNK_LABEL, 10**6 + 1
        elif top:
            open_band = bands[-1]
            if open_band.hi is not None:
                raise ValidationProblem("top-coded (Y_OPEN) records require an open-ended last band", cell_id=cid)
            if open_age_lower_bound is None:
                raise ValidationProblem("Y_OPEN lower bound unknown; pass open_age_lower_bound", cell_id=cid)
            if open_band.lo > open_age_lower_bound:
                raise ValidationProblem(
                    f"open band {open_band.label!r} starts above the Y_OPEN lower bound "
                    f"{open_age_lower_bound}; the top-coded group cannot be split",
                    cell_id=cid,
                )
            label, order = open_band.label, 10**6
            band_has_top.add(label)
        elif age_years is None:
            # an age-band leaf (e.g. Y_GE85, Y85-89): it must fit wholly inside one band
            leaf_iv = parse_age_code(_age_code).interval
            if leaf_iv is None:
                raise ValidationProblem(
                    "record without age_years that is neither UNK, Y_OPEN nor an age band",
                    cell_id=cid,
                )
            lo, hi = leaf_iv
            hit = [b for b in bands if b.lo <= lo and (b.hi is None or (hi is not None and hi <= b.hi))]
            if not hit:
                raise ValidationProblem(
                    f"age-band leaf {_age_code!r} is not wholly inside any target band; "
                    "it cannot be split without inventing within-band detail",
                    cell_id=cid,
                )
            label, order = hit[0].label, lo
        else:
            hit = [b for b in bands if b.contains(age_years)]
            if not hit:
                raise ValidationProblem(f"age {age_years} not covered by any band", cell_id=cid)
            label, order = hit[0].label, age_years
        key = (sex, label)
        coarse_w[key] = coarse_w.get(key, Fraction(0)) + w
        coarse_fine.setdefault(key, []).append((order, cid))

    band_order = {b.label: i for i, b in enumerate(bands)} | {UNK_LABEL: len(bands)}
    sex_rank = {s: i for i, s in enumerate(SEX_ORDER)}
    ordered = sorted(coarse_w, key=lambda k: (sex_rank.get(k[0], 99), k[0], band_order[k[1]]))

    rows: dict[str, list[Any]] = {f.name: [] for f in COARSE_SCHEMA}
    mapping: dict[str, tuple[str, ...]] = {}
    for i, (sex, label) in enumerate(ordered):
        w = coarse_w[(sex, label)]
        _check_int64(w, f"{sex}|{label}")
        fine_ids = tuple(c for _, c in sorted(coarse_fine[(sex, label)]))
        cid = f"{sex}|{label}"
        mapping[cid] = fine_ids
        band = band_by_label.get(label)
        rows["record_id"].append(i)
        rows["cell_id"].append(cid)
        rows["age_band"].append(label)
        rows["age_lo"].append(band.lo if band else None)
        rows["age_hi"].append(band.hi if band else None)
        rows["includes_top_coded"].append(label in band_has_top)
        rows["is_unknown_age"].append(label == UNK_LABEL)
        rows["sex_code"].append(sex)
        rows["weight"].append(w.numerator / w.denominator)
        rows["weight_num"].append(w.numerator)
        rows["weight_den"].append(w.denominator)
        rows["n_fine_records"].append(sum(rec_counts[c] for c in fine_ids))
        rows["n_fine_cells"].append(len(fine_ids))

    mapping_digest = logical_hash({"operation": "coarsen", "mapping": mapping})
    new_md = {
        **md,
        "reva.compression": "coarsen",
        "reva.mapping_digest": mapping_digest,
        "reva.age_bands": str([[b.label, b.lo, b.hi] for b in bands]),
    }
    out = pa.Table.from_pydict(rows, schema=COARSE_SCHEMA.with_metadata(new_md))

    # --- certificate: recompute from the output table, independently of coarse_w
    out_cells = {k[0]: v for k, v in exact_weight_sums(out, ["cell_id"]).items()}
    in_cells = {k[0]: v for k, v in exact_weight_sums(table, ["cell_id"]).items()}
    cell_err = {c: abs(out_cells[c] - sum((in_cells[f] for f in fs), Fraction(0))) for c, fs in mapping.items()}
    measured = max(cell_err.values(), default=Fraction(0))
    sex_of = {c: c.split("|", 1)[0] for c in mapping}
    groups: dict[str, list[str]] = {}
    for c, s in sex_of.items():
        groups.setdefault(s, []).append(c)
    tails = {"unknown_age": [c for c in mapping if c.endswith(f"|{UNK_LABEL}")]}
    if bands[-1].hi is None:
        tails[f"open_band_{bands[-1].label}_incl_top_coded"] = [c for c in mapping if c.endswith(f"|{bands[-1].label}")]
    cert = CompressionCertificate(
        task_scope=task_scope,
        operation="coarsen",
        reference=md.get("reva.recipe_digest", "unknown"),
        mapping_digest=mapping_digest,
        loss_metric="max_abs_coarse_cell_weight_error_exact",
        epsilon=epsilon,
        measured_error=fraction_decimal_str(measured),
        within_epsilon=measured <= Fraction(epsilon),
        subgroup_errors=group_max_errors(cell_err, groups),
        tail_errors=group_max_errors(cell_err, tails),
        conservation=(
            *conservation_checks(table, out),
            ConservationCheck("per_coarse_cell_weight", measured == 0, fraction_str(measured)),
        ),
        runtime_seconds=time.perf_counter() - t0,
        expiry_conditions=(
            "Valid only for the input table derived from the referenced recipe digest.",
            "Invalid if weights are re-calibrated or records are added/removed after coarsening.",
            "Single-year age detail within bands is not represented; only refine() along age "
            "with the same recipe may restore it.",
            "Out of scope: any attribute or association beyond age band x recorded sex.",
        ),
        unsupported_detail=(
            "single-year age within a band",
            "exact age within Y_OPEN",
            "age of UNK records",
        ),
    )
    return CompressionResult(table=out, mapping=mapping, mapping_digest=mapping_digest, certificate=cert)
