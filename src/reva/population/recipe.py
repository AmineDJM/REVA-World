"""Population recipe: the disjoint age × recorded-sex leaf partition of one source table.

Scientific scope and limits
---------------------------
* Input is one geography/reference date of a Eurostat ``demo_pjan``-like table
  (population on 1 January by age and sex). The only supported attributes are
  ``age`` and ``sex``; the joint age × sex distribution is the only association
  the source supports. Nothing else (income, household, health, ...) can be
  derived from it and requests for such attributes are rejected.
* ``sex`` is the *recorded sex* as published by the source (M/F). It is not
  gender identity and is not re-interpreted.
* **Leaf partition.** The leaves are the *finest disjoint cover* of the age
  range the source covers with an observed (M/F) count, built only from source
  age codes: single years (``Y_LT1``, ``Yk``) where they are observed, an age
  band (``Ya-b``, ``Y_LTn``, ``Y_GEn``, ``Y_GTn``) where no finer observed cells
  tile its range, ``Y_OPEN`` as the open top group when it is observed, plus
  ``UNK``. Among all disjoint covers of that range the one with the most cells
  is chosen (deterministic dynamic programme), so every covered age is counted
  exactly once and overlapping codes are never both counted. Codes that
  overlap without any disjoint cover raise ``ValidationProblem``. Codes that
  are not leaves (totals, sex ``T``, subtotal bands, cells finer than a leaf
  band) are kept only for consistency checks, which never alter source counts.
* **Totals gate.** The leaves must reproduce each reported ``TOTAL`` (per
  recorded sex and both sexes) exactly. If they do not, ``build_recipe`` raises
  ``InconsistentSourceTotals`` unless the caller records a documented
  explanation (``inconsistency_explanation``); ``materialise`` refuses any
  recipe whose totals gate is blocked. Population is never silently dropped.
* ``Y_OPEN`` (open-ended top group) and ``UNK`` (unknown age) stay explicit
  categories: no age is imputed for them. The ``Y_OPEN`` lower bound is either
  declared by the caller (dataset metadata) or inferred as one above the
  highest age cell *with an observed count*; if any cell above that age is
  missing the bound is ambiguous and recorded as unknown (``None``), and
  coarsening that depends on it is refused downstream.
* A count of 0 is an *observed* zero (``observed_zero_cells``). Structural
  zeros (cells impossible by definition, ``structural_zeros``) do not exist
  for age × recorded sex. A missing count (Eurostat ``:``) or a leaf absent
  from the source is Missing — never zero.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Any, Literal

from reva.kernel.errors import CapabilityGap, ValidationProblem
from reva.kernel.ids import logical_hash
from reva.kernel.values import decimal_str

RECIPE_METHOD_VERSION = "reva-population-recipe-2"
PARTITION_RULE = (
    "finest disjoint cover of the observed age range by source age codes "
    "(max number of cells), plus Y_OPEN (open top group) and UNK; totals gate on TOTAL"
)
SUPPORTED_ATTRIBUTES: tuple[str, ...] = ("age", "sex")
FORBIDDEN_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "income",
        "religion",
        "sexual_orientation",
        "political_opinion",
        "health",
        "ethnicity",
        "household",
        "gender",
        "gender_identity",
        "race",
        "disability",
        "biometric",
        "genetic",
        "trade_union_membership",
    }
)
RECORDED_SEX_DEFINITION = (
    "Recorded sex as published by the source (Eurostat demo_pjan: M, F); "
    "not gender identity and not re-coded."
)
LEAF_SEXES: tuple[str, ...] = ("F", "M")
TOTAL_SEX = "T"
DECIMAL_PRECISION = 18

_SINGLE = re.compile(r"^Y([1-9]\d*)$")
_LT = re.compile(r"^Y_LT([1-9]\d*)$")
_RANGE = re.compile(r"^Y(\d+)-(\d+)$")
_GE = re.compile(r"^Y_GE(\d+)$")
_GT = re.compile(r"^Y_GT(\d+)$")

AgeKind = Literal["single", "top_coded", "unknown", "total", "band"]
Interval = tuple[int, int | None]  # inclusive completed-year bounds; hi=None is open-ended


# --------------------------------------------------------------------------- helpers


def fraction_str(value: Fraction) -> str:
    """Exact string for a Fraction: ``"p"`` for integers, ``"p/q"`` otherwise."""
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def fraction_decimal_str(value: Fraction, precision: int = DECIMAL_PRECISION) -> str:
    """Decimal string of a Fraction rounded to ``precision`` significant digits."""
    with localcontext() as ctx:
        ctx.prec = precision
        return decimal_str(Decimal(value.numerator) / Decimal(value.denominator))


@dataclass(frozen=True)
class AgeCode:
    """Parsed Eurostat age code.

    ``lo``/``hi`` are inclusive completed-year bounds; ``hi=None`` means
    open-ended. For ``top_coded`` (``Y_OPEN``) the lower bound is only known from
    context or metadata, so both are ``None`` here.
    """

    code: str
    kind: AgeKind
    lo: int | None = None
    hi: int | None = None

    @property
    def is_leaf(self) -> bool:
        """True for codes that are leaves in a single-year table (singles, Y_OPEN, UNK).

        Whether a *band* is a leaf depends on the other codes of the table; see
        ``build_recipe``.
        """
        return self.kind in ("single", "top_coded", "unknown")

    @property
    def interval(self) -> Interval | None:
        """``(lo, hi)`` for singles and bands; ``None`` for Y_OPEN, UNK and TOTAL."""
        if self.kind in ("single", "band"):
            assert self.lo is not None
            return (self.lo, self.hi)
        return None


def parse_age_code(code: str) -> AgeCode:
    """Classify a Eurostat age code; unrecognised codes raise ``ValidationProblem``."""
    if code == "TOTAL":
        return AgeCode(code, "total")
    if code == "UNK":
        return AgeCode(code, "unknown")
    if code == "Y_OPEN":
        return AgeCode(code, "top_coded")
    if code == "Y_LT1":
        return AgeCode(code, "single", 0, 0)
    if m := _SINGLE.match(code):
        k = int(m.group(1))
        return AgeCode(code, "single", k, k)
    if m := _LT.match(code):
        return AgeCode(code, "band", 0, int(m.group(1)) - 1)
    if m := _RANGE.match(code):
        lo, hi = int(m.group(1)), int(m.group(2))
        if hi < lo:
            raise ValidationProblem(f"age band with hi < lo: {code!r}", age_code=code)
        return AgeCode(code, "band", lo, hi)
    if m := _GE.match(code):
        return AgeCode(code, "band", int(m.group(1)), None)
    if m := _GT.match(code):
        return AgeCode(code, "band", int(m.group(1)) + 1, None)
    raise ValidationProblem(f"unrecognised Eurostat age code {code!r}", age_code=code)


def single_age_code(age: int) -> str:
    """Eurostat code of a single completed year (``Y_LT1`` for 0)."""
    return "Y_LT1" if age == 0 else f"Y{age}"


def _age_sort_key(code: str) -> tuple[int, int, int]:
    """Canonical order: finite cells by lower bound, then open-ended, then UNK."""
    a = parse_age_code(code)
    iv = a.interval
    if iv is not None:
        lo, hi = iv
        return (0, lo, hi) if hi is not None else (1, lo, 0)
    if a.kind == "top_coded":
        return (1, 10**9, 0)
    return (2, 0, 0)


def _contains(outer: Interval, inner: Interval) -> bool:
    if inner[0] < outer[0]:
        return False
    if outer[1] is None:
        return True
    return inner[1] is not None and inner[1] <= outer[1]


def _overlaps(a: Interval, b: Interval) -> bool:
    return (a[1] is None or b[0] <= a[1]) and (b[1] is None or a[0] <= b[1])


def cell_id_for(sex_code: str, age_code: str) -> str:
    return f"{sex_code}|{age_code}"


# --------------------------------------------------------------------------- errors


class UnsupportedAttribute(CapabilityGap):
    """A request for an attribute the source does not support (or must never synthesise)."""

    code = "unsupported_population_attribute"


class InconsistentSourceTotals(ValidationProblem):
    """The leaf partition does not reproduce a reported ``TOTAL`` and no explanation was given."""

    code = "population_source_totals_inconsistent"


def check_attributes(attributes: Iterable[str]) -> tuple[str, ...]:
    """Validate requested attributes; anything beyond age/sex raises ``UnsupportedAttribute``."""
    requested = tuple(attributes)
    extra = [a for a in requested if a not in SUPPORTED_ATTRIBUTES]
    if extra:
        forbidden = sorted(a for a in extra if a.lower() in FORBIDDEN_ATTRIBUTES)
        raise UnsupportedAttribute(
            "only age and recorded sex are supported by Eurostat demo_pjan; requested "
            f"attributes {sorted(extra)} are unsupported"
            + (
                f" and {forbidden} are sensitive attributes that are never synthesised"
                if forbidden
                else ""
            ),
            requested=sorted(extra),
            forbidden=forbidden,
            supported=list(SUPPORTED_ATTRIBUTES),
        )
    if len(set(requested)) != len(requested):
        raise ValidationProblem("duplicate attributes requested", requested=list(requested))
    return requested


# --------------------------------------------------------------------------- records


@dataclass(frozen=True)
class SourceCell:
    """One published source cell. ``count=None`` means missing (Eurostat ``:``)."""

    age_code: str
    sex_code: str
    count: int | None
    flags: tuple[str, ...]
    source_key: str

    def __post_init__(self) -> None:
        if self.count is not None:
            if isinstance(self.count, bool) or not isinstance(self.count, int):
                raise ValidationProblem("count must be an int or None", source_key=self.source_key)
            if self.count < 0:
                raise ValidationProblem(
                    "negative population count", source_key=self.source_key, count=self.count
                )
        object.__setattr__(self, "flags", tuple(self.flags))


@dataclass(frozen=True)
class LeafCell:
    """A disjoint leaf of the age × recorded-sex partition with an observed count.

    ``age_years`` is set only for single-year leaves. ``age_lo``/``age_hi`` are
    the inclusive bounds of any age leaf (``age_hi=None`` = open-ended; both
    ``None`` for ``UNK`` and for ``Y_OPEN`` with an unknown lower bound).
    """

    cell_id: str
    age_code: str
    sex_code: str
    age_years: int | None
    is_top_coded: bool
    is_unknown_age: bool
    count: int
    flags: tuple[str, ...]
    source_key: str
    age_lo: int | None = None
    age_hi: int | None = None

    @property
    def is_age_band(self) -> bool:
        """A multi-year band leaf (e.g. ``Y_GE85`` with no finer cells beneath it)."""
        return self.age_years is None and not self.is_top_coded and not self.is_unknown_age

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell_id": self.cell_id,
            "age_code": self.age_code,
            "sex_code": self.sex_code,
            "age_years": self.age_years,
            "age_lo": self.age_lo,
            "age_hi": self.age_hi,
            "is_top_coded": self.is_top_coded,
            "is_unknown_age": self.is_unknown_age,
            "is_age_band": self.is_age_band,
            "count": self.count,
            "flags": list(self.flags),
            "source_key": self.source_key,
        }


@dataclass(frozen=True)
class ExcludedCell:
    """A total/subtotal kept out of the leaf partition (used only for consistency checks)."""

    source_key: str
    age_code: str
    sex_code: str
    count: int | None
    flags: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_key": self.source_key,
            "age_code": self.age_code,
            "sex_code": self.sex_code,
            "count": self.count,
            "flags": list(self.flags),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class MissingCell:
    """A leaf whose count is missing in the source or absent from it. Never zero."""

    cell_id: str
    age_code: str
    sex_code: str
    reason: Literal["count_missing_in_source", "absent_from_source"]
    flags: tuple[str, ...]
    source_key: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell_id": self.cell_id,
            "age_code": self.age_code,
            "sex_code": self.sex_code,
            "reason": self.reason,
            "flags": list(self.flags),
            "source_key": self.source_key,
        }


@dataclass(frozen=True)
class ConsistencyCheck:
    """Reported aggregate vs the sum of the leaves it should contain.

    ``difference = leaf_sum - reported``. ``relative_difference`` is
    ``|difference| / reported`` as a decimal string. Checks never change any
    source count; a failing ``TOTAL`` check blocks the recipe (see module doc).
    """

    source_key: str
    sex_code: str
    age_code: str
    reported: int | None
    leaf_sum: int | None
    difference: int | None
    absolute_difference: int | None
    relative_difference: str | None
    status: Literal["consistent", "inconsistent", "not_computable"]
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_key": self.source_key,
            "sex_code": self.sex_code,
            "age_code": self.age_code,
            "reported": self.reported,
            "leaf_sum": self.leaf_sum,
            "difference": self.difference,
            "absolute_difference": self.absolute_difference,
            "relative_difference": self.relative_difference,
            "status": self.status,
            "note": self.note,
        }


TotalsStatus = Literal[
    "consistent", "inconsistent_accepted", "blocked", "not_computable", "not_reported"
]


@dataclass(frozen=True, kw_only=True)
class PopulationRecipe:
    """Immutable description of the leaf partition to be materialised.

    ``leaf_cells`` are in canonical order: recorded sex (F, M), then age (finite
    cells by lower bound, open-ended cells, UNK). ``observed_zero_cells`` lists
    leaf cells with an observed count of 0; these receive no records.
    ``structural_zeros`` lists cells impossible by definition (none for age ×
    recorded sex). ``missing_cells`` are excluded from ``leaf_cells`` and never
    treated as zero.
    """

    geography: str
    reference_date: date
    source_ref: Mapping[str, Any]
    leaf_cells: tuple[LeafCell, ...]
    excluded_cells: tuple[ExcludedCell, ...]
    consistency: tuple[ConsistencyCheck, ...]
    observed_zero_cells: tuple[str, ...]
    missing_cells: tuple[MissingCell, ...]
    seed: int
    open_age_lower_bound: int | None
    open_age_lower_bound_basis: str = "not_applicable"
    structural_zeros: tuple[str, ...] = ()
    accepted_inconsistency: str | None = None
    supported_attributes: tuple[str, ...] = SUPPORTED_ATTRIBUTES
    sex_definition: str = RECORDED_SEX_DEFINITION
    method_version: str = RECIPE_METHOD_VERSION
    _index: dict[str, LeafCell] = field(default_factory=dict, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_ref", dict(self.source_ref))
        object.__setattr__(self, "_index", {c.cell_id: c for c in self.leaf_cells})

    # -- accessors
    def leaf(self, cell_id: str) -> LeafCell:
        return self._index[cell_id]

    @property
    def total_count(self) -> int:
        """Sum of included leaf counts (never includes totals or subtotals)."""
        return sum(c.count for c in self.leaf_cells)

    @property
    def nonzero_cells(self) -> tuple[LeafCell, ...]:
        return tuple(c for c in self.leaf_cells if c.count > 0)

    @property
    def leaf_age_codes(self) -> tuple[str, ...]:
        """Distinct leaf age codes of the partition in canonical order."""
        seen: dict[str, None] = {}
        for c in self.leaf_cells:
            seen.setdefault(c.age_code, None)
        for m in self.missing_cells:
            seen.setdefault(m.age_code, None)
        return tuple(sorted(seen, key=_age_sort_key))

    @property
    def flag_summary(self) -> dict[str, int]:
        """Number of included leaf cells carrying each source status flag."""
        out: dict[str, int] = {}
        for c in self.leaf_cells:
            for f in c.flags:
                out[f] = out.get(f, 0) + 1
        return dict(sorted(out.items()))

    @property
    def total_checks(self) -> tuple[ConsistencyCheck, ...]:
        """Checks of reported ``TOTAL`` cells (per recorded sex and both sexes)."""
        return tuple(c for c in self.consistency if c.age_code == "TOTAL")

    @property
    def totals_status(self) -> TotalsStatus:
        """Outcome of the totals gate (see module docstring)."""
        checks = self.total_checks
        if not checks:
            return "not_reported"
        if any(c.status == "inconsistent" for c in checks):
            return "inconsistent_accepted" if self.accepted_inconsistency else "blocked"
        if any(c.status == "not_computable" for c in checks):
            return "not_computable"
        return "consistent"

    @property
    def is_blocked(self) -> bool:
        """True when the leaves fail to reproduce a reported TOTAL without an explanation."""
        return self.totals_status == "blocked"

    @property
    def is_internally_consistent(self) -> bool:
        return all(c.status != "inconsistent" for c in self.consistency)

    def to_dict(self) -> dict[str, Any]:
        return {
            "method_version": self.method_version,
            "partition_rule": PARTITION_RULE,
            "geography": self.geography,
            "reference_date": self.reference_date,
            "source_ref": dict(self.source_ref),
            "supported_attributes": list(self.supported_attributes),
            "sex_definition": self.sex_definition,
            "open_age_lower_bound": self.open_age_lower_bound,
            "open_age_lower_bound_basis": self.open_age_lower_bound_basis,
            "seed": self.seed,
            "leaf_age_codes": list(self.leaf_age_codes),
            "leaf_cells": [c.to_dict() for c in self.leaf_cells],
            "excluded_cells": [c.to_dict() for c in self.excluded_cells],
            "consistency": [c.to_dict() for c in self.consistency],
            "totals_status": self.totals_status,
            "accepted_inconsistency": self.accepted_inconsistency,
            "observed_zero_cells": list(self.observed_zero_cells),
            "structural_zeros": list(self.structural_zeros),
            "missing_cells": [c.to_dict() for c in self.missing_cells],
            "total_count": self.total_count,
            "flag_summary": self.flag_summary,
        }

    def digest(self) -> str:
        """Kernel ``logical_hash`` of the canonical recipe description."""
        return logical_hash(self.to_dict())


# --------------------------------------------------------------------------- partition


def _relative(diff: int, reported: int) -> str | None:
    if reported == 0:
        return None
    return fraction_decimal_str(Fraction(abs(diff), reported))


def finest_disjoint_cover(candidates: Mapping[str, Interval]) -> tuple[str, ...] | None:
    """Disjoint subset of ``candidates`` whose union equals the union of all of them.

    Among all such subsets the one with the most members (the finest) is
    returned; ties are broken by code order, so the result is deterministic.
    Returns ``None`` when no disjoint subset covers the union (partially
    overlapping codes). Gaps in the union are allowed.
    """
    if not candidates:
        return ()
    starts: dict[int, list[str]] = {}
    for code, (lo, _hi) in candidates.items():
        starts.setdefault(lo, []).append(code)
    positions = sorted(starts)
    memo: dict[int, tuple[int, tuple[str, ...]] | None] = {}

    def covered(x: int) -> bool:
        return any(_overlaps((x, x), iv) for iv in candidates.values())

    def best(x: int) -> tuple[int, tuple[str, ...]] | None:
        if x in memo:
            return memo[x]
        res: tuple[int, tuple[str, ...]] | None
        if not covered(x):
            nxt = next((s for s in positions if s > x), None)
            res = (0, ()) if nxt is None else best(nxt)
        else:
            res = None
            for code in sorted(starts.get(x, ())):
                hi = candidates[code][1]
                rest = (0, ()) if hi is None else best(hi + 1)
                if rest is None:
                    continue
                option = (rest[0] + 1, (code, *rest[1]))
                if res is None or option[0] > res[0]:
                    res = option
        memo[x] = res
        return res

    out = best(positions[0])
    return None if out is None else out[1]


def _greedy_disjoint(candidates: Mapping[str, Interval]) -> tuple[str, ...]:
    """Fallback for reporting missing cells only: narrowest-first disjoint selection."""

    def width(iv: Interval) -> float:
        return float("inf") if iv[1] is None else iv[1] - iv[0]

    picked: list[str] = []
    for code in sorted(candidates, key=lambda c: (width(candidates[c]), candidates[c][0], c)):
        if all(not _overlaps(candidates[code], candidates[p]) for p in picked):
            picked.append(code)
    return tuple(picked)


@dataclass(frozen=True)
class _Partition:
    observed: tuple[str, ...]  # leaf age codes with an observed count for some leaf sex
    missing: tuple[str, ...]  # leaf age codes without any observed count (missing/absent)
    open_lo: int | None
    open_basis: str
    open_min: int | None  # lowest age Y_OPEN can start at, given disjointness from observed cells

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(sorted({*self.observed, *self.missing}, key=_age_sort_key))


def _select_partition(
    by_key: Mapping[tuple[str, str], SourceCell], declared_open_lo: int | None
) -> _Partition:
    present = {a for (_s, a) in by_key}
    observed_codes = {
        a
        for (s, a), c in by_key.items()
        if s in LEAF_SEXES and c.count is not None and parse_age_code(a).kind != "total"
    }
    open_observed = "Y_OPEN" in observed_codes

    # 1. finest disjoint cover of the observed age range (Y_OPEN, when observed, is the top)
    obs_cands: dict[str, Interval] = {}
    for a in observed_codes:
        iv = parse_age_code(a).interval
        if iv is not None and (iv[1] is not None or not open_observed):
            obs_cands[a] = iv
    chosen = finest_disjoint_cover(obs_cands)
    if chosen is None:
        raise ValidationProblem(
            "observed age codes overlap without any disjoint cover; the leaf partition is "
            "ambiguous and would double-count or drop population",
            age_codes=sorted(obs_cands, key=_age_sort_key),
        )
    chosen_ivs = [obs_cands[a] for a in chosen]
    top_is_band = any(iv[1] is None for iv in chosen_ivs)
    open_is_leaf = "Y_OPEN" in present and not top_is_band
    observed_leaves = [*chosen]
    if open_observed and open_is_leaf:
        observed_leaves.append("Y_OPEN")
    if "UNK" in observed_codes:
        observed_leaves.append("UNK")

    # 2. ages the source names but does not observe: missing leaves (never zero)
    def uncovered(iv: Interval) -> bool:
        return all(not _overlaps(iv, c) for c in chosen_ivs)

    miss_cands: dict[str, Interval] = {}
    finite_hi = [iv[1] for a in present if (iv := parse_age_code(a).interval) and iv[1] is not None]
    for a in present - observed_codes:
        iv = parse_age_code(a).interval
        if iv is None or (iv[1] is None and open_is_leaf) or not uncovered(iv):
            continue
        miss_cands[a] = iv
    if finite_hi:
        for age in range(0, max(finite_hi) + 1):
            iv = (age, age)
            if uncovered(iv) and all(not _overlaps(iv, m) for m in miss_cands.values()):
                miss_cands[single_age_code(age)] = iv
    missing_cover = finest_disjoint_cover(miss_cands)
    missing_leaves = list(missing_cover if missing_cover is not None else _greedy_disjoint(miss_cands))
    if "Y_OPEN" in present and open_is_leaf and not open_observed:
        missing_leaves.append("Y_OPEN")
    if "UNK" in present and "UNK" not in observed_codes:
        missing_leaves.append("UNK")

    # 3. lower bound of Y_OPEN
    observed_finite = [iv[1] for iv in chosen_ivs if iv[1] is not None]
    open_min = max(observed_finite) + 1 if observed_finite else None
    all_finite_leaf_hi = [
        iv[1]
        for a in (*chosen, *missing_leaves)
        if (iv := parse_age_code(a).interval) and iv[1] is not None
    ]
    if not open_is_leaf:
        # no Y_OPEN, or Y_OPEN lies inside an open band leaf (e.g. Y_GE85): no bound needed
        open_lo, basis = None, "not_applicable: no Y_OPEN leaf in the partition"
    elif declared_open_lo is not None:
        if isinstance(declared_open_lo, bool) or not isinstance(declared_open_lo, int):
            raise ValidationProblem("open_age_lower_bound must be an int")
        if all_finite_leaf_hi and declared_open_lo <= max(all_finite_leaf_hi):
            raise ValidationProblem(
                f"declared Y_OPEN lower bound {declared_open_lo} overlaps finite age cells "
                f"up to {max(all_finite_leaf_hi)}",
                open_age_lower_bound=declared_open_lo,
            )
        open_lo, basis = declared_open_lo, "declared by caller (dataset metadata)"
    elif open_min is None:
        open_lo, basis = None, "unknown: no finite age cell has an observed count"
    else:
        above = sorted(
            (
                a
                for a in missing_leaves
                if (iv := parse_age_code(a).interval) is not None and iv[0] >= open_min
            ),
            key=_age_sort_key,
        )
        if above:
            open_lo = None
            basis = (
                f"unknown: cells above the highest observed age {open_min - 1} are missing "
                f"({', '.join(above[:5])}{', ...' if len(above) > 5 else ''}); Y_OPEN may start "
                f"at {open_min} or higher"
            )
        else:
            open_lo = open_min
            basis = (
                "inferred: one above the highest age cell with an observed count "
                "(source age cells are disjoint)"
            )
    return _Partition(
        observed=tuple(sorted(observed_leaves, key=_age_sort_key)),
        missing=tuple(sorted(missing_leaves, key=_age_sort_key)),
        open_lo=open_lo,
        open_basis=basis,
        open_min=open_min,
    )


def _leaf_interval(code: str, open_lo: int | None) -> Interval | None:
    if code == "Y_OPEN":
        return None if open_lo is None else (open_lo, None)
    return parse_age_code(code).interval


def _excluded_reason(sex: str, age: AgeCode, leaf_codes: Sequence[str]) -> str:
    if sex == TOTAL_SEX and age.kind == "total":
        return "grand_total"
    if age.kind == "total":
        return "age_total"
    if sex == TOTAL_SEX and age.code in leaf_codes:
        return "sex_total"
    if age.kind == "band":
        return "age_band_subtotal" if sex != TOTAL_SEX else "age_band_subtotal_both_sexes"
    if sex == TOTAL_SEX:
        return "sex_total"
    return "finer_than_leaf_partition"


def _aggregate_members(
    age: AgeCode, part: _Partition
) -> tuple[list[str] | None, str]:
    """Leaf age codes contained in an aggregate age code, or ``None`` if not computable."""
    codes = part.codes
    if age.kind == "total":
        return list(codes), ""
    if age.code in codes:
        return [age.code], ""
    iv = age.interval
    if iv is None:
        return None, f"{age.code} is not a leaf of this partition (it lies inside a band leaf)"
    members: list[str] = []
    for code in codes:
        if code == "UNK":
            continue
        liv = _leaf_interval(code, part.open_lo)
        if liv is None:  # Y_OPEN with unknown lower bound
            if iv[1] is not None and part.open_min is not None and iv[1] < part.open_min:
                continue
            return None, "lower bound of the open group Y_OPEN is unknown"
        if _contains(iv, liv):
            members.append(code)
        elif _overlaps(iv, liv):
            return None, f"{age.code} only partly overlaps the leaf {code}"
    return members, ""


def build_recipe(
    cells: Iterable[SourceCell],
    *,
    geography: str,
    reference_date: date | str,
    source_ref: Mapping[str, Any],
    seed: int,
    attributes: Sequence[str] = SUPPORTED_ATTRIBUTES,
    allow_missing: bool = False,
    open_age_lower_bound: int | None = None,
    inconsistency_explanation: str | None = None,
) -> PopulationRecipe:
    """Select the disjoint age × recorded-sex leaf partition and audit it against totals.

    Raises ``UnsupportedAttribute`` for any attribute beyond age/sex,
    ``ValidationProblem`` for unrecognised codes, duplicate cells, overlapping
    codes without a disjoint cover, or (unless ``allow_missing``) any missing leaf
    count, and ``InconsistentSourceTotals`` when the leaves do not reproduce a
    reported ``TOTAL`` and no ``inconsistency_explanation`` is recorded. With
    ``allow_missing=True`` missing leaves are excluded and listed in
    ``missing_cells``; the recipe then describes only the observed part of the
    population. ``open_age_lower_bound`` declares the ``Y_OPEN`` lower bound from
    dataset metadata instead of inferring it.
    """
    check_attributes(attributes)
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValidationProblem("seed must be a non-negative int", seed=seed)
    if isinstance(reference_date, str):
        reference_date = date.fromisoformat(reference_date)
    if not geography:
        raise ValidationProblem("geography is required")
    if inconsistency_explanation is not None and not inconsistency_explanation.strip():
        raise ValidationProblem("inconsistency_explanation must be a non-empty explanation")

    by_key: dict[tuple[str, str], SourceCell] = {}
    for cell in cells:
        if cell.sex_code not in (*LEAF_SEXES, TOTAL_SEX):
            raise ValidationProblem(
                f"unsupported sex code {cell.sex_code!r}; expected M, F or T",
                source_key=cell.source_key,
            )
        parse_age_code(cell.age_code)
        key = (cell.sex_code, cell.age_code)
        if key in by_key:
            raise ValidationProblem(
                "duplicate source cell", sex_code=cell.sex_code, age_code=cell.age_code
            )
        by_key[key] = cell
    if not by_key:
        raise ValidationProblem("no source cells supplied")

    part = _select_partition(by_key, open_age_lower_bound)
    universe = part.codes
    if not universe:
        raise ValidationProblem("source contains no leaf age categories")

    leaves: list[LeafCell] = []
    missing: list[MissingCell] = []
    leaf_counts: dict[tuple[str, str], int | None] = {}
    for sex in LEAF_SEXES:
        for age_code in universe:
            a = parse_age_code(age_code)
            cid = cell_id_for(sex, age_code)
            src = by_key.get((sex, age_code))
            if src is None or src.count is None:
                missing.append(
                    MissingCell(
                        cell_id=cid,
                        age_code=age_code,
                        sex_code=sex,
                        reason="absent_from_source" if src is None else "count_missing_in_source",
                        flags=src.flags if src else (),
                        source_key=src.source_key if src else None,
                    )
                )
                leaf_counts[(sex, age_code)] = None
                continue
            leaf_counts[(sex, age_code)] = src.count
            iv = _leaf_interval(age_code, part.open_lo)
            leaves.append(
                LeafCell(
                    cell_id=cid,
                    age_code=age_code,
                    sex_code=sex,
                    age_years=a.lo if a.kind == "single" else None,
                    is_top_coded=a.kind == "top_coded",
                    is_unknown_age=a.kind == "unknown",
                    count=src.count,
                    flags=src.flags,
                    source_key=src.source_key,
                    age_lo=iv[0] if iv else None,
                    age_hi=iv[1] if iv else None,
                )
            )
    if missing and not allow_missing:
        raise ValidationProblem(
            f"{len(missing)} leaf cell(s) are missing; missing counts are never treated as zero "
            "(pass allow_missing=True to exclude and report them)",
            missing_cells=[m.to_dict() for m in missing],
        )
    if not leaves:
        raise ValidationProblem("no observed leaf cells")

    leaf_set = set(universe)
    excluded: list[ExcludedCell] = []
    checks: list[ConsistencyCheck] = []
    for (sex, age_code), src in sorted(by_key.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        a = parse_age_code(age_code)
        if sex != TOTAL_SEX and age_code in leaf_set:
            continue
        excluded.append(
            ExcludedCell(
                source_key=src.source_key,
                age_code=age_code,
                sex_code=sex,
                count=src.count,
                flags=src.flags,
                reason=_excluded_reason(sex, a, universe),
            )
        )
        checks.append(_check(src, a, part, leaf_counts))

    failing = [c for c in checks if c.age_code == "TOTAL" and c.status == "inconsistent"]
    if failing and inconsistency_explanation is None:
        raise InconsistentSourceTotals(
            "the disjoint leaf partition does not reproduce the reported TOTAL "
            f"({', '.join(f'{c.sex_code}|TOTAL: leaves {c.leaf_sum} vs reported {c.reported}' for c in failing)}); "
            "population would be lost or double-counted. Record a documented "
            "inconsistency_explanation to proceed.",
            failing_checks=[c.to_dict() for c in failing],
            leaf_age_codes=list(universe),
            open_age_lower_bound=part.open_lo,
        )

    return PopulationRecipe(
        geography=geography,
        reference_date=reference_date,
        source_ref=source_ref,
        leaf_cells=tuple(leaves),
        excluded_cells=tuple(excluded),
        consistency=tuple(checks),
        observed_zero_cells=tuple(c.cell_id for c in leaves if c.count == 0),
        missing_cells=tuple(missing),
        seed=seed,
        open_age_lower_bound=part.open_lo,
        open_age_lower_bound_basis=part.open_basis,
        accepted_inconsistency=inconsistency_explanation if failing else None,
    )


def _check(
    src: SourceCell,
    age: AgeCode,
    part: _Partition,
    leaf_counts: Mapping[tuple[str, str], int | None],
) -> ConsistencyCheck:
    sexes = LEAF_SEXES if src.sex_code == TOTAL_SEX else (src.sex_code,)
    members, note = _aggregate_members(age, part)

    def nc(n: str, leaf_sum: int | None = None) -> ConsistencyCheck:
        return ConsistencyCheck(
            src.source_key,
            src.sex_code,
            age.code,
            src.count,
            leaf_sum,
            None,
            None,
            None,
            "not_computable",
            n,
        )

    if members is None:
        return nc(note)
    missing = [cell_id_for(s, m) for s in sexes for m in members if leaf_counts.get((s, m)) is None]
    if missing:
        return nc(f"leaf cells missing: {', '.join(missing)}")
    leaf_sum = sum(leaf_counts[(s, m)] or 0 for s in sexes for m in members)
    if src.count is None:
        return nc("reported aggregate is missing in source", leaf_sum)
    diff = leaf_sum - src.count
    return ConsistencyCheck(
        source_key=src.source_key,
        sex_code=src.sex_code,
        age_code=age.code,
        reported=src.count,
        leaf_sum=leaf_sum,
        difference=diff,
        absolute_difference=abs(diff),
        relative_difference=_relative(diff, src.count) if diff else "0",
        status="consistent" if diff == 0 else "inconsistent",
        note="" if src.count or not diff else "reported aggregate is 0; relative undefined",
    )
