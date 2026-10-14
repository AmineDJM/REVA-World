"""Population recipe: the disjoint age × recorded-sex leaf partition of one source table.

Scientific scope and limits
---------------------------
* Input is one geography/reference date of Eurostat ``demo_pjan`` (population on
  1 January by single year of age and sex). The only supported attributes are
  ``age`` and ``sex``; the joint age × sex distribution is the only association
  the source supports. Nothing else (income, household, health, ...) can be
  derived from it and requests for such attributes are rejected.
* ``sex`` is the *recorded sex* as published by the source (M/F). It is not
  gender identity and is not re-interpreted.
* Only the disjoint leaf partition {M, F} × {Y_LT1, Y1..Yn, Y_OPEN, UNK} is
  counted. Totals (sex ``T``, age ``TOTAL``) and age-band subtotals are never
  summed together with leaves; they are retained only for consistency checks,
  which are reported and never used to alter source counts.
* ``Y_OPEN`` (open-ended top group) and ``UNK`` (unknown age) stay explicit
  categories: no age is imputed for them.
* A count of 0 is an *observed* zero. A missing count (Eurostat ``:``) or a leaf
  absent from the source is Missing — never zero.
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

RECIPE_METHOD_VERSION = "reva-population-recipe-1"
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
    open-ended. For ``top_coded`` the lower bound is only known from context
    (one above the highest single year in the table), so both are ``None``.
    """

    code: str
    kind: AgeKind
    lo: int | None = None
    hi: int | None = None

    @property
    def is_leaf(self) -> bool:
        return self.kind in ("single", "top_coded", "unknown")


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


def _age_sort_key(code: str) -> tuple[int, int]:
    a = parse_age_code(code)
    if a.kind == "single":
        assert a.lo is not None
        return (0, a.lo)
    if a.kind == "top_coded":
        return (1, 0)
    return (2, 0)


def cell_id_for(sex_code: str, age_code: str) -> str:
    return f"{sex_code}|{age_code}"


# --------------------------------------------------------------------------- errors


class UnsupportedAttribute(CapabilityGap):
    """A request for an attribute the source does not support (or must never synthesise)."""

    code = "unsupported_population_attribute"


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
                raise ValidationProblem(
                    "count must be an int or None", source_key=self.source_key
                )
            if self.count < 0:
                raise ValidationProblem(
                    "negative population count", source_key=self.source_key, count=self.count
                )
        object.__setattr__(self, "flags", tuple(self.flags))


@dataclass(frozen=True)
class LeafCell:
    """A disjoint leaf of the age × recorded-sex partition with an observed count."""

    cell_id: str
    age_code: str
    sex_code: str
    age_years: int | None
    is_top_coded: bool
    is_unknown_age: bool
    count: int
    flags: tuple[str, ...]
    source_key: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell_id": self.cell_id,
            "age_code": self.age_code,
            "sex_code": self.sex_code,
            "age_years": self.age_years,
            "is_top_coded": self.is_top_coded,
            "is_unknown_age": self.is_unknown_age,
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
    ``|difference| / reported`` as a decimal string. Checks are diagnostics only:
    a disagreement never changes any source count.
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


@dataclass(frozen=True)
class PopulationRecipe:
    """Immutable description of the leaf partition to be materialised.

    ``leaf_cells`` are in canonical order: recorded sex (F, M), then age
    (Y_LT1, Y1..Yn, Y_OPEN, UNK). ``structural_zeros`` lists leaf cells with an
    observed count of 0; these receive no records. ``missing_cells`` are
    excluded from ``leaf_cells`` and never treated as zero.
    """

    geography: str
    reference_date: date
    source_ref: Mapping[str, Any]
    leaf_cells: tuple[LeafCell, ...]
    excluded_cells: tuple[ExcludedCell, ...]
    consistency: tuple[ConsistencyCheck, ...]
    structural_zeros: tuple[str, ...]
    missing_cells: tuple[MissingCell, ...]
    seed: int
    open_age_lower_bound: int | None
    supported_attributes: tuple[str, ...] = SUPPORTED_ATTRIBUTES
    sex_definition: str = RECORDED_SEX_DEFINITION
    method_version: str = RECIPE_METHOD_VERSION
    _index: dict[str, LeafCell] = field(default_factory=dict, repr=False, compare=False)

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
    def flag_summary(self) -> dict[str, int]:
        """Number of included leaf cells carrying each source status flag."""
        out: dict[str, int] = {}
        for c in self.leaf_cells:
            for f in c.flags:
                out[f] = out.get(f, 0) + 1
        return dict(sorted(out.items()))

    @property
    def is_internally_consistent(self) -> bool:
        return all(c.status != "inconsistent" for c in self.consistency)

    def to_dict(self) -> dict[str, Any]:
        return {
            "method_version": self.method_version,
            "geography": self.geography,
            "reference_date": self.reference_date,
            "source_ref": dict(self.source_ref),
            "supported_attributes": list(self.supported_attributes),
            "sex_definition": self.sex_definition,
            "open_age_lower_bound": self.open_age_lower_bound,
            "seed": self.seed,
            "leaf_cells": [c.to_dict() for c in self.leaf_cells],
            "excluded_cells": [c.to_dict() for c in self.excluded_cells],
            "consistency": [c.to_dict() for c in self.consistency],
            "structural_zeros": list(self.structural_zeros),
            "missing_cells": [c.to_dict() for c in self.missing_cells],
            "total_count": self.total_count,
            "flag_summary": self.flag_summary,
        }

    def digest(self) -> str:
        """Kernel ``logical_hash`` of the canonical recipe description."""
        return logical_hash(self.to_dict())


# --------------------------------------------------------------------------- builder


def _relative(diff: int, reported: int) -> str | None:
    if reported == 0:
        return None
    return fraction_decimal_str(Fraction(abs(diff), reported))


def _leaf_universe(age_codes: Iterable[str]) -> list[str]:
    """All leaf age codes implied by the source: contiguous singles plus explicit extras."""
    parsed = [parse_age_code(a) for a in set(age_codes)]
    singles = [a.lo for a in parsed if a.kind == "single" and a.lo is not None]
    universe: set[str] = set()
    if singles:
        top = max(singles)
        universe.add("Y_LT1")
        universe.update(f"Y{k}" for k in range(1, top + 1))
    universe.update(a.code for a in parsed if a.kind in ("top_coded", "unknown"))
    return sorted(universe, key=_age_sort_key)


def _excluded_reason(sex: str, age: AgeCode) -> str:
    if sex == TOTAL_SEX and age.kind == "total":
        return "grand_total"
    if age.kind == "total":
        return "age_total"
    if age.kind == "band":
        return "age_band_subtotal" if sex != TOTAL_SEX else "age_band_subtotal_both_sexes"
    return "sex_total"


def _band_members(
    age: AgeCode, universe: Sequence[str], open_lo: int | None
) -> tuple[list[str] | None, str]:
    """Leaf age codes contained in an aggregate age code, or ``None`` if not computable."""
    if age.kind == "total":
        return list(universe), ""
    if age.kind != "band":
        return [age.code], ""
    assert age.lo is not None
    members: list[str] = []
    for code in universe:
        a = parse_age_code(code)
        if a.kind == "single":
            assert a.lo is not None
            if a.lo >= age.lo and (age.hi is None or a.lo <= age.hi):
                members.append(code)
        elif a.kind == "top_coded":
            if open_lo is None:
                return None, "lower bound of the open group Y_OPEN is unknown"
            if age.hi is None and age.lo <= open_lo:
                members.append(code)
            elif age.hi is None or age.hi >= open_lo:
                return None, f"band {age.code} only partly overlaps the open group Y_OPEN"
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
) -> PopulationRecipe:
    """Select the disjoint age × recorded-sex leaf partition and audit it against totals.

    Raises ``UnsupportedAttribute`` for any attribute beyond age/sex,
    ``ValidationProblem`` for unrecognised codes, duplicate cells, or (unless
    ``allow_missing``) any missing leaf count. With ``allow_missing=True``
    missing leaves are excluded and listed in ``missing_cells``; the recipe then
    describes only the observed part of the population.
    """
    check_attributes(attributes)
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValidationProblem("seed must be a non-negative int", seed=seed)
    if isinstance(reference_date, str):
        reference_date = date.fromisoformat(reference_date)
    if not geography:
        raise ValidationProblem("geography is required")

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

    leaf_codes = [a for (_, a) in by_key if parse_age_code(a).is_leaf]
    universe = _leaf_universe(leaf_codes)
    if not universe:
        raise ValidationProblem("source contains no leaf age categories")
    singles = [parse_age_code(a).lo for a in universe if parse_age_code(a).kind == "single"]
    open_lo = (max(s for s in singles if s is not None) + 1) if "Y_OPEN" in universe else None

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

    excluded: list[ExcludedCell] = []
    checks: list[ConsistencyCheck] = []
    for (sex, age_code), src in sorted(
        by_key.items(), key=lambda kv: (kv[0][0], kv[0][1])
    ):
        a = parse_age_code(age_code)
        if sex != TOTAL_SEX and a.is_leaf:
            continue
        excluded.append(
            ExcludedCell(
                source_key=src.source_key,
                age_code=age_code,
                sex_code=sex,
                count=src.count,
                flags=src.flags,
                reason=_excluded_reason(sex, a),
            )
        )
        checks.append(_check(src, a, universe, open_lo, leaf_counts))

    return PopulationRecipe(
        geography=geography,
        reference_date=reference_date,
        source_ref=source_ref,
        leaf_cells=tuple(leaves),
        excluded_cells=tuple(excluded),
        consistency=tuple(checks),
        structural_zeros=tuple(c.cell_id for c in leaves if c.count == 0),
        missing_cells=tuple(missing),
        seed=seed,
        open_age_lower_bound=open_lo,
    )


def _check(
    src: SourceCell,
    age: AgeCode,
    universe: Sequence[str],
    open_lo: int | None,
    leaf_counts: Mapping[tuple[str, str], int | None],
) -> ConsistencyCheck:
    sexes = LEAF_SEXES if src.sex_code == TOTAL_SEX else (src.sex_code,)
    members, note = _band_members(age, universe, open_lo)

    def nc(n: str, leaf_sum: int | None = None) -> ConsistencyCheck:
        return ConsistencyCheck(
            src.source_key, src.sex_code, age.code, src.count, leaf_sum, None, None, None,
            "not_computable", n,
        )

    if members is None:
        return nc(note)
    missing = [
        cell_id_for(s, m) for s in sexes for m in members if leaf_counts.get((s, m)) is None
    ]
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
