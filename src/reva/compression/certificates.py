"""Compression certificates: what a coarsen/refine step preserved and what it lost.

A certificate is valid only for the stated task scope and reference recipe. It
certifies exact conservation of weight totals (checked with Fraction
arithmetic) and the measured value of a named loss metric; it does not certify
anything about attributes or associations outside the age × recorded-sex
support of the reference recipe.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from typing import Any, Literal

import pyarrow as pa

from reva.kernel.ids import logical_hash
from reva.population.materialise import exact_total_weight, exact_weight_sums
from reva.population.recipe import fraction_decimal_str, fraction_str

CERTIFICATE_VERSION = "reva-compression-certificate-1"


@dataclass(frozen=True)
class ConservationCheck:
    """Exact weight conservation. ``max_abs_diff`` is an exact fraction string."""

    name: str
    conserved: bool
    max_abs_diff: str


@dataclass(frozen=True)
class CompressionCertificate:
    task_scope: str
    operation: Literal["coarsen", "refine", "stratify"]
    reference: str  # recipe digest (or "unknown" if not recorded)
    mapping_digest: str
    loss_metric: str
    epsilon: str  # decimal string
    measured_error: str  # decimal string
    within_epsilon: bool
    subgroup_errors: dict[str, str]  # per recorded sex, decimal strings
    tail_errors: dict[str, str]  # top-coded / unknown-age groups
    conservation: tuple[ConservationCheck, ...]
    runtime_seconds: float
    expiry_conditions: tuple[str, ...]
    unsupported_detail: tuple[str, ...] = ()
    version: str = field(default=CERTIFICATE_VERSION)

    @property
    def conserved(self) -> bool:
        return all(c.conserved for c in self.conservation)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["conservation"] = [asdict(c) for c in self.conservation]
        d["expiry_conditions"] = list(self.expiry_conditions)
        d["unsupported_detail"] = list(self.unsupported_detail)
        d["conserved"] = self.conserved
        return d

    def digest(self) -> str:
        """Logical hash excluding the (non-deterministic) runtime."""
        d = self.to_dict()
        d.pop("runtime_seconds")
        return logical_hash(d)


def group_max_errors(
    errors: Mapping[str, Fraction], groups: Mapping[str, Sequence[str]]
) -> dict[str, str]:
    """Loss metric restricted to each subgroup: max per-cell error, as decimal strings."""
    return {
        g: fraction_decimal_str(max((errors[c] for c in cells if c in errors), default=Fraction(0)))
        for g, cells in sorted(groups.items())
    }


def conservation_checks(before: pa.Table, after: pa.Table) -> tuple[ConservationCheck, ...]:
    """Exact total and per-recorded-sex weight conservation between two tables."""
    tb, ta = exact_total_weight(before), exact_total_weight(after)
    sb = {k[0]: v for k, v in exact_weight_sums(before, ["sex_code"]).items()}
    sa = {k[0]: v for k, v in exact_weight_sums(after, ["sex_code"]).items()}
    sex_diff = max(
        (abs(sa.get(s, Fraction(0)) - sb.get(s, Fraction(0))) for s in set(sa) | set(sb)),
        default=Fraction(0),
    )
    return (
        ConservationCheck("total_weight", tb == ta, fraction_str(abs(ta - tb))),
        ConservationCheck("per_sex_total_weight", sex_diff == 0, fraction_str(sex_diff)),
    )
