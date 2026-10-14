"""Semantic compatibility checks between concepts, supports and value bases.

Two numbers are only "the same thing" if their statistical universe, temporal
support (a 1 January point is not a midyear estimate), spatial scheme,
aggregation, scale type, unit, currency/price basis, valuation (CIF is not FOB)
and classification revision agree. These checks return a typed
:class:`CompatibilityReport`; they never coerce. Missing semantic information
(no unit, no support, no basis for a currency amount) is a *blocker*: the
answer is "cannot be determined", which is never treated as compatible.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from reva.kernel.errors import IncompatibleSemantics, MissingInput
from reva.kernel.records import Concept, ScaleType, SemanticProfile
from reva.kernel.support import Support
from reva.kernel.values import MeasurementBasis
from reva.ontology.units import (
    QuantityKind,
    canonical_unit_ref,
    conversion_factor,
    is_known_unit,
    unit_definition,
)

Purpose = Literal["identity", "aggregate", "combine"]
Severity = Literal["incompatible", "blocker", "note"]

ADDITIVE_SCALE_TYPES = frozenset({ScaleType.stock, ScaleType.flow, ScaleType.extensive})
# Operation names in ``permitted_transformations`` that license summation.
ADDITIVE_OPERATIONS = frozenset({"sum", "sum_disjoint_leaf_cells", "sum_disjoint_entities"})


class Verdict(StrEnum):
    compatible = "compatible"
    incompatible = "incompatible"
    blocked = "blocked"


class Finding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    aspect: str
    code: str
    severity: Severity
    message: str
    left: str | None = None
    right: str | None = None


class CompatibilityReport(BaseModel):
    """Typed outcome. ``bool(report)`` is True only for ``compatible``."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    purpose: str
    left: str
    right: str
    verdict: Verdict
    findings: tuple[Finding, ...] = ()

    @property
    def compatible(self) -> bool:
        return self.verdict is Verdict.compatible

    def __bool__(self) -> bool:
        return self.compatible

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(f.message for f in self.findings if f.severity != "note")

    @property
    def codes(self) -> frozenset[str]:
        return frozenset(f.code for f in self.findings if f.severity != "note")

    def require(self) -> None:
        """Raise unless compatible: blocked -> MissingInput, incompatible -> IncompatibleSemantics."""
        if self.verdict is Verdict.compatible:
            return
        detail = {"purpose": self.purpose, "left": self.left, "right": self.right,
                  "findings": [f.model_dump() for f in self.findings]}
        msg = f"{self.purpose} of {self.left} and {self.right}: " + "; ".join(self.reasons)
        if self.verdict is Verdict.blocked:
            raise MissingInput(msg, **detail)
        raise IncompatibleSemantics(msg, **detail)


def _report(purpose: str, left: str, right: str, findings: list[Finding]) -> CompatibilityReport:
    sev = {f.severity for f in findings}
    if "incompatible" in sev:
        verdict = Verdict.incompatible
    elif "blocker" in sev:
        verdict = Verdict.blocked
    else:
        verdict = Verdict.compatible
    return CompatibilityReport(purpose=purpose, left=left, right=right, verdict=verdict, findings=tuple(findings))


# --------------------------------------------------------------------------- semantic sides


@dataclass(frozen=True)
class SemanticSide:
    """Everything known about one operand.

    ``unit_ref``/``basis`` come from the value when available and override the
    profile; ``classification`` is ``"<system>:<revision>"`` (e.g. ``HS:H5``)
    and may also be given as ``support.resolution['classification']``.
    """

    ref: str
    profile: SemanticProfile | None
    support: Support | None = None
    basis: MeasurementBasis | None = None
    unit_ref: str | None = None
    classification: str | None = None

    @classmethod
    def from_concept(cls, concept: Concept, **kw: Any) -> SemanticSide:
        return cls(ref=f"{concept.concept_id}@{concept.version}", profile=concept.semantic_profile, **kw)

    @property
    def effective_unit(self) -> str | None:
        return self.unit_ref or (self.profile.unit_ref if self.profile else None)

    @property
    def effective_classification(self) -> str | None:
        if self.classification:
            return self.classification
        if self.support is not None:
            return self.support.resolution.get("classification")
        return None


def _as_side(x: SemanticSide | Concept) -> SemanticSide:
    return x if isinstance(x, SemanticSide) else SemanticSide.from_concept(x)


# --------------------------------------------------------------------------- temporal support

_POINT = re.compile(r"^point:(\d{2})-(\d{2})\s*(?:\((?P<q>[^)]*)\))?\s*$")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December")


@dataclass(frozen=True)
class TemporalSupportSpec:
    kind: Literal["point", "period", "unparsed"]
    anchor: str | None  # MM-DD for points
    qualifier: str | None
    raw: str

    def describe(self) -> str:
        if self.kind == "point" and self.anchor:
            m, d = (int(x) for x in self.anchor.split("-"))
            base = f"point on {d} {_MONTHS[m - 1]}"
            return f"{base} ({self.qualifier})" if self.qualifier else base
        return self.raw


def parse_temporal_support(raw: str) -> TemporalSupportSpec:
    m = _POINT.match(raw.strip())
    if m:
        return TemporalSupportSpec("point", f"{m.group(1)}-{m.group(2)}", m.group("q"), raw)
    if raw.startswith(("annual_", "period_", "monthly_", "quarterly_")):
        return TemporalSupportSpec("period", None, None, raw)
    return TemporalSupportSpec("unparsed", None, None, raw)


def _temporal_finding(a: str, b: str) -> Finding | None:
    if a == b:
        return None
    pa, pb = parse_temporal_support(a), parse_temporal_support(b)
    if pa.kind == pb.kind == "point" and pa.anchor == pb.anchor:
        # same date; differing qualifier text is still a declared difference in method
        return Finding(aspect="temporal_support", code="temporal_support_qualifier_mismatch", severity="incompatible",
                       message=f"temporal support differs: {pa.describe()} vs {pb.describe()}", left=a, right=b)
    if pa.kind == pb.kind == "point":
        code, what = "temporal_point_mismatch", "different reference points"
    elif {pa.kind, pb.kind} == {"point", "period"}:
        code, what = "temporal_point_vs_period", "point-in-time vs period statistic"
    else:
        code, what = "temporal_support_mismatch", "different temporal supports"
    return Finding(aspect="temporal_support", code=code, severity="incompatible",
                   message=f"{what}: {pa.describe()} vs {pb.describe()}", left=a, right=b)


# --------------------------------------------------------------------------- valuation


def normalise_valuation(v: str | None) -> str | None:
    """``CIF``/``FOB`` when declared exactly; otherwise a tagged raw string.

    A provider's "primaryValue (imports usually CIF)" is *not* declared CIF.
    """
    if v is None:
        return None
    s = v.strip()
    if s.upper() in ("CIF", "FOB"):
        return s.upper()
    return f"undeclared:{s}"


def _valuation_finding(a: str | None, b: str | None, where: str) -> Finding | None:
    na, nb = normalise_valuation(a), normalise_valuation(b)
    if na == nb:
        if na is not None and na.startswith("undeclared:") and "not determinable" in na:
            return Finding(aspect="valuation", code="valuation_undetermined", severity="blocker",
                           message=f"{where} valuation is undetermined on both sides ({a})", left=a, right=b)
        return None
    if {na, nb} == {"CIF", "FOB"}:
        return Finding(aspect="valuation", code="valuation_cif_vs_fob", severity="incompatible",
                       message=f"{where} valuation CIF vs FOB: CIF includes insurance and freight to the importer's "
                               "border, FOB does not; they are distinct measures", left=a, right=b)
    if na is None or nb is None:
        return Finding(aspect="valuation", code="valuation_missing", severity="blocker",
                       message=f"{where} valuation declared on one side only ({a!r} vs {b!r})", left=a, right=b)
    return Finding(aspect="valuation", code="valuation_mismatch", severity="incompatible",
                   message=f"{where} valuation differs: {a!r} vs {b!r}", left=a, right=b)


# --------------------------------------------------------------------------- core checks


def _neq(aspect: str, code: str, a: Any, b: Any, what: str) -> Finding | None:
    if a == b:
        return None
    return Finding(aspect=aspect, code=code, severity="incompatible", message=f"{what} differs: {a!r} vs {b!r}",
                   left=None if a is None else str(a), right=None if b is None else str(b))


def _profile_findings(a: SemanticProfile, b: SemanticProfile, purpose: Purpose) -> list[Finding]:
    out: list[Finding | None] = [
        _neq("population_universe", "population_universe_mismatch", a.population_universe, b.population_universe,
             "statistical/population universe"),
        _neq("denominator", "denominator_mismatch", a.denominator, b.denominator, "denominator"),
        _temporal_finding(a.temporal_support, b.temporal_support),
        _neq("spatial_support", "spatial_support_mismatch", a.spatial_support, b.spatial_support, "spatial support"),
        _neq("aggregation", "aggregation_mismatch", a.aggregation, b.aggregation, "aggregation"),
        _neq("scale_type", "scale_type_mismatch", a.scale_type.value, b.scale_type.value, "scale type"),
        _valuation_finding(a.valuation, b.valuation, "concept"),
    ]
    if a.measurement_method != b.measurement_method:
        sev: Severity = "incompatible" if purpose == "identity" else "note"
        out.append(Finding(aspect="measurement_method", code="measurement_method_mismatch", severity=sev,
                           message=f"measurement method differs: {a.measurement_method!r} vs {b.measurement_method!r}",
                           left=a.measurement_method, right=b.measurement_method))
    if purpose == "aggregate":
        for p in (a, b):
            if p.scale_type not in ADDITIVE_SCALE_TYPES:
                out.append(Finding(aspect="scale_type", code="non_additive_scale_type", severity="incompatible",
                                   message=f"{p.scale_type.value} quantities cannot be summed"))
                break
        for p in (a, b):
            if not ADDITIVE_OPERATIONS.intersection(p.permitted_transformations):
                out.append(Finding(
                    aspect="permitted_operations", code="aggregation_not_permitted", severity="incompatible",
                    message=f"concept permits only {list(p.permitted_transformations)}; no additive operation "
                            f"({sorted(ADDITIVE_OPERATIONS)}) is approved"))
                break
    return [f for f in out if f is not None]


def _unit_findings(a: SemanticSide, b: SemanticSide) -> tuple[list[Finding], bool]:
    """Returns (findings, is_currency)."""
    ua, ub = a.effective_unit, b.effective_unit
    out: list[Finding] = []
    categorical = all(s.profile is not None and s.profile.scale_type is ScaleType.categorical for s in (a, b))
    if categorical and ua is None and ub is None:
        return out, False
    for side, u in ((a, ua), (b, ub)):
        if u is None:
            out.append(Finding(aspect="unit", code="unit_missing", severity="blocker",
                               message=f"{side.ref} has no unit_ref; a unit is never inferred"))
        elif not is_known_unit(u):
            out.append(Finding(aspect="unit", code="unit_unknown", severity="blocker",
                               message=f"{side.ref} unit_ref {u!r} is not registered"))
    if out:
        return out, False
    ca, cb = canonical_unit_ref(ua), canonical_unit_ref(ub)
    is_currency = QuantityKind.currency_amount in (unit_definition(ca).kind, unit_definition(cb).kind)
    if ca != cb:
        try:
            conversion_factor(ca, cb)
            note = "convertible, but a conversion is a transform, not identity"
        except IncompatibleSemantics as e:
            note = e.message
        out.append(Finding(aspect="unit", code="unit_mismatch", severity="incompatible",
                           message=f"unit differs: {ca} vs {cb} ({note})", left=ca, right=cb))
    return out, is_currency


def _currency_profile_findings(a: SemanticSide, b: SemanticSide) -> list[Finding]:
    out: list[Finding] = []
    pa, pb = a.profile, b.profile
    assert pa is not None and pb is not None
    for side, p in ((a, pa), (b, pb)):
        basis = side.basis
        if p.currency_basis is None and (basis is None or basis.currency is None):
            out.append(Finding(aspect="currency_basis", code="currency_basis_missing", severity="blocker",
                               message=f"{side.ref}: currency amount without declared currency"))
        if p.price_basis is None and (basis is None or basis.price_basis is None):
            out.append(Finding(aspect="price_basis", code="price_basis_missing", severity="blocker",
                               message=f"{side.ref}: currency amount without nominal/real/PPP price basis"))
    if not out:
        for f in (
            _neq("currency_basis", "currency_basis_mismatch", pa.currency_basis, pb.currency_basis, "currency"),
            _neq("price_basis", "price_basis_mismatch", pa.price_basis, pb.price_basis, "price basis"),
        ):
            if f is not None:
                out.append(f)
    return out


def basis_findings(a: MeasurementBasis | None, b: MeasurementBasis | None, *, currency: bool,
                   left: str = "left", right: str = "right") -> list[Finding]:
    """Compare value-level measurement bases (currency, prices, FX, valuation, scale)."""
    out: list[Finding] = []
    if a is None or b is None:
        if currency:
            missing = [n for n, x in ((left, a), (right, b)) if x is None]
            out.append(Finding(aspect="basis", code="currency_basis_missing", severity="blocker",
                               message=f"currency amount without measurement basis: {', '.join(missing)}"))
        return out
    checks = (
        ("currency", "currency_mismatch", "currency"),
        ("price_basis", "price_basis_mismatch", "price basis"),
        ("price_reference_year", "price_reference_year_mismatch", "price reference year"),
        ("exchange_rate_transform", "exchange_rate_transform_mismatch", "exchange-rate transformation"),
        ("scale_factor", "scale_factor_mismatch", "scale factor"),
    )
    for attr, code, what in checks:
        f = _neq(attr, code, getattr(a, attr), getattr(b, attr), what)
        if f is not None:
            out.append(f)
    if currency:
        for name, x in ((left, a), (right, b)):
            if x.currency is None or x.price_basis is None:
                out.append(Finding(aspect="basis", code="currency_basis_incomplete", severity="blocker",
                                   message=f"{name}: currency amount lacks currency or price basis"))
            elif x.price_basis in ("real", "ppp") and x.price_reference_year is None:
                out.append(Finding(aspect="basis", code="price_reference_year_missing", severity="blocker",
                                   message=f"{name}: {x.price_basis} amount without price reference year"))
    v = _valuation_finding(a.valuation, b.valuation, "value")
    if v is not None:
        out.append(v)
    return out


def _support_findings(a: Support, b: Support, purpose: Purpose) -> list[Finding]:
    out: list[Finding | None] = [
        _neq("support.aggregation", "support_aggregation_mismatch", a.aggregation, b.aggregation, "support aggregation"),
        _neq("support.population", "support_population_mismatch", a.population.universe, b.population.universe,
             "support population universe"),
        _neq("support.spatial", "spatial_scheme_mismatch", a.spatial.scheme, b.spatial.scheme, "spatial scheme"),
        _neq("support.temporal", "calendar_mismatch", a.temporal.calendar.value, b.temporal.calendar.value,
             "calendar"),
    ]
    ta, tb = a.temporal, b.temporal
    if (ta.kind, ta.start, ta.end) != (tb.kind, tb.start, tb.end):
        out.append(Finding(
            aspect="support.temporal", code="temporal_extent_mismatch", severity="incompatible",
            message=f"temporal extent differs: {ta.kind} {ta.start}..{ta.end} vs {tb.kind} {tb.start}..{tb.end}",
            left=f"{ta.kind}:{ta.start}:{ta.end}", right=f"{tb.kind}:{tb.start}:{tb.end}"))
    if ta.start_uncertainty or ta.end_uncertainty or tb.start_uncertainty or tb.end_uncertainty:
        out.append(Finding(aspect="support.temporal", code="temporal_bounds_uncertain", severity="blocker",
                           message="uncertain temporal bounds: equality of supports cannot be established"))
    if purpose == "identity":
        out.append(_neq("support.spatial", "spatial_unit_mismatch", a.spatial.key, b.spatial.key, "spatial unit"))
    return [f for f in out if f is not None]


def check_support_against_profile(profile: SemanticProfile, support: Support) -> list[Finding]:
    """A support must instantiate its concept's declared support."""
    out: list[Finding] = []
    if support.aggregation != profile.aggregation:
        out.append(Finding(aspect="support.aggregation", code="support_profile_aggregation_mismatch",
                           severity="incompatible",
                           message=f"support aggregation {support.aggregation} contradicts concept {profile.aggregation}"))
    if profile.population_universe != "not_applicable" and support.population.universe != profile.population_universe:
        out.append(Finding(aspect="support.population", code="support_profile_universe_mismatch",
                           severity="incompatible",
                           message=f"support universe {support.population.universe} contradicts concept "
                                   f"{profile.population_universe}"))
    spec = parse_temporal_support(profile.temporal_support)
    t = support.temporal
    if spec.kind == "point":
        if t.kind != "instant" or t.start is None or t.start.strftime("%m-%d") != spec.anchor:
            out.append(Finding(aspect="support.temporal", code="support_profile_temporal_mismatch",
                               severity="incompatible",
                               message=f"concept requires {spec.describe()}, support is {t.kind} starting {t.start}"))
    elif spec.kind == "period" and t.kind != "interval":
        out.append(Finding(aspect="support.temporal", code="support_profile_temporal_mismatch",
                           severity="incompatible", message=f"concept requires a period ({spec.raw}), support is an "
                                                            "instant"))
    return out


def check_compatibility(a: SemanticSide | Concept, b: SemanticSide | Concept,
                        purpose: Purpose = "identity", *, require_support: bool = False) -> CompatibilityReport:
    """Full semantic comparison of two operands for a stated purpose."""
    sa, sb = _as_side(a), _as_side(b)
    findings: list[Finding] = []
    if sa.profile is None or sb.profile is None:
        missing = [s.ref for s in (sa, sb) if s.profile is None]
        findings.append(Finding(aspect="semantic_profile", code="semantic_profile_missing", severity="blocker",
                                message=f"no semantic profile for {', '.join(missing)}"))
        return _report(purpose, sa.ref, sb.ref, findings)
    findings.extend(_profile_findings(sa.profile, sb.profile, purpose))
    unit_f, is_currency = _unit_findings(sa, sb)
    findings.extend(unit_f)
    if is_currency:
        findings.extend(_currency_profile_findings(sa, sb))
    if sa.basis is not None or sb.basis is not None or (is_currency and require_support):
        findings.extend(basis_findings(sa.basis, sb.basis, currency=is_currency, left=sa.ref, right=sb.ref))
    if sa.support is not None and sb.support is not None:
        findings.extend(_support_findings(sa.support, sb.support, purpose))
        for s in (sa, sb):
            assert s.support is not None and s.profile is not None
            findings.extend(check_support_against_profile(s.profile, s.support))
    elif require_support:
        missing = [s.ref for s in (sa, sb) if s.support is None]
        findings.append(Finding(aspect="support", code="support_missing", severity="blocker",
                                message=f"support missing for {', '.join(missing)}"))
    ca, cb = sa.effective_classification, sb.effective_classification
    if ca != cb:
        if ca is None or cb is None:
            findings.append(Finding(aspect="classification", code="classification_missing", severity="blocker",
                                    message=f"classification revision declared on one side only ({ca!r} vs {cb!r})"))
        else:
            findings.append(Finding(aspect="classification", code="classification_revision_mismatch",
                                    severity="incompatible",
                                    message=f"classification/revision differs: {ca} vs {cb}", left=ca, right=cb))
    return _report(purpose, sa.ref, sb.ref, findings)


def compare_concepts(a: Concept, b: Concept) -> CompatibilityReport:
    """Are two concept versions the same quantity (identity purpose)?"""
    return check_compatibility(a, b, "identity")


def can_aggregate(a: SemanticSide | Concept, b: SemanticSide | Concept) -> CompatibilityReport:
    """May two values be summed into one total?

    Requires identical statistical universe, support date/period, aggregation,
    additive scale type, an approved additive operation, unit, currency/price
    basis, valuation and classification revision. Supports are mandatory.
    """
    return check_compatibility(a, b, "aggregate", require_support=True)


# --------------------------------------------------------------------------- cross-country combination


@dataclass(frozen=True)
class CountryValue:
    """One country's value to be combined (compared, ranked, ratioed or pooled)."""

    entity_key: str
    side: SemanticSide
    reference_year: int | None = None

    @property
    def effective_reference_year(self) -> int | None:
        if self.reference_year is not None:
            return self.reference_year
        if self.side.support is not None:
            return self.side.support.temporal.reference_year()
        return None


def can_combine_countries(items: Sequence[CountryValue]) -> CompatibilityReport:
    """Refuse silent cross-country combination of non-comparable values.

    All items must share the concept, the reference year and the full value
    basis (currency, nominal/real/PPP, price reference year, exchange-rate
    transformation, valuation); a currency amount without a basis or a value
    without reference year blocks the combination.
    """
    label = ",".join(i.entity_key for i in items)
    findings: list[Finding] = []
    if len(items) < 2:
        findings.append(Finding(aspect="input", code="too_few_items", severity="blocker",
                                message="combination needs at least two country values"))
        return _report("combine", label, "", findings)
    keys = [i.entity_key for i in items]
    if len(set(keys)) != len(keys):
        findings.append(Finding(aspect="entity", code="duplicate_entity", severity="incompatible",
                                message="the same country appears more than once"))
    years = {i.entity_key: i.effective_reference_year for i in items}
    for k, y in years.items():
        if y is None:
            findings.append(Finding(aspect="reference_year", code="reference_year_missing", severity="blocker",
                                    message=f"{k}: reference year unknown"))
    known_years = {y for y in years.values() if y is not None}
    if len(known_years) > 1:
        findings.append(Finding(
            aspect="reference_year", code="reference_year_mismatch", severity="incompatible",
            message="different reference years cannot be combined silently: "
                    + ", ".join(f"{k}={y}" for k, y in sorted(years.items()))))
    first = items[0]
    for other in items[1:]:
        rep = check_compatibility(first.side, other.side, "combine")
        for f in rep.findings:
            if f.code == "temporal_extent_mismatch" and len(known_years) > 1:
                continue  # already reported as reference_year_mismatch
            tagged = f.model_copy(update={"message": f"{first.entity_key} vs {other.entity_key}: {f.message}"})
            findings.append(tagged)
        if first.side.ref != other.side.ref:
            findings.append(Finding(aspect="concept", code="concept_mismatch", severity="incompatible",
                                    message=f"{first.entity_key} uses {first.side.ref}, {other.entity_key} uses "
                                            f"{other.side.ref}", left=first.side.ref, right=other.side.ref))
    for i in items:
        u = i.side.effective_unit
        if u and is_known_unit(u) and unit_definition(u).kind is QuantityKind.currency_amount and i.side.basis is None:
            findings.append(Finding(aspect="basis", code="currency_basis_missing", severity="blocker",
                                    message=f"{i.entity_key}: currency amount without value basis (GDP basis "
                                            "unknown: nominal/real/PPP, price year, exchange-rate transform)"))
    return _report("combine", label, "", _dedupe(findings))


def _dedupe(findings: list[Finding]) -> list[Finding]:
    seen: set[tuple[str, str]] = set()
    out: list[Finding] = []
    for f in findings:
        key = (f.code, f.message)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out
