"""Unit registry for REVA unit references, backed by Pint dimensional analysis.

Scientific rules enforced here
------------------------------
* A unit is only ever taken from an explicit ``unit_ref``. There is deliberately
  no function that guesses a unit from a field or column name; an observation
  whose ``unit_ref`` is missing or unknown is rejected by
  :func:`validate_observation_units`.
* Currency amounts are their own dimension per currency (``[currency_USD]``,
  ``[currency_EUR]``). Pint therefore cannot convert them to anything else, and
  :func:`convert` refuses even a same-dimension call between different
  currencies. The only way to change currency, price basis or price reference
  year is a versioned :class:`CurrencyTransform` that carries its basis,
  exchange-rate transformation, per-period factors and evidence.
* "USD" alone is not a complete economic unit: a currency amount must carry a
  :class:`~reva.kernel.values.MeasurementBasis` with currency and price basis
  (and a price reference year for real/PPP values).
* Mass and volume are different dimensions. kg <-> barrel is refused
  (:class:`~reva.kernel.errors.IncompatibleSemantics`) unless an explicit
  :class:`DensityConvention` (density *and* named, versioned convention) is
  supplied to :func:`mass_to_volume` / :func:`volume_to_mass`.
* Dimensionless units carry a semantic denominator: a percentage of baseline
  GDP is not interchangeable with a bare ratio even though Pint would allow it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from enum import StrEnum
from functools import lru_cache
from typing import Any, Literal

import pint
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from reva.kernel.errors import IncompatibleSemantics, MissingInput, ValidationProblem
from reva.kernel.values import MeasurementBasis, ScalarDecimal, decimal_str

UNIT_REGISTRY_VERSION = "reva-units-1"
_PRECISION = 50


class UnknownUnit(ValidationProblem):
    """A unit reference that is missing or not registered."""

    code = "unknown_unit"


class QuantityKind(StrEnum):
    currency_amount = "currency_amount"
    count = "count"
    mass = "mass"
    volume = "volume"
    dimensionless = "dimensionless"


@dataclass(frozen=True)
class UnitDefinition:
    """One REVA unit reference and its Pint expression.

    ``denominator`` names the semantic reference of a dimensionless unit
    (``None`` for a bare ratio). Two dimensionless units with different
    denominators are never converted into each other.
    """

    ref: str
    pint_unit: str
    kind: QuantityKind
    label: str
    currency: str | None = None
    denominator: str | None = None


UNIT_DEFINITIONS: tuple[UnitDefinition, ...] = (
    UnitDefinition("unit.usd", "reva_USD", QuantityKind.currency_amount, "US dollar amount", currency="USD"),
    UnitDefinition("unit.eur", "reva_EUR", QuantityKind.currency_amount, "Euro amount", currency="EUR"),
    UnitDefinition("unit.persons", "reva_person", QuantityKind.count, "Persons"),
    UnitDefinition("unit.kg", "kilogram", QuantityKind.mass, "Kilogram"),
    UnitDefinition("unit.tonne", "metric_ton", QuantityKind.mass, "Metric tonne"),
    UnitDefinition("unit.barrel", "oil_barrel", QuantityKind.volume, "Oil barrel (42 US gallons)"),
    UnitDefinition("unit.m3", "meter ** 3", QuantityKind.volume, "Cubic metre"),
    UnitDefinition("unit.ratio", "dimensionless", QuantityKind.dimensionless, "Ratio (bare, no denominator)"),
    UnitDefinition("unit.percent", "percent", QuantityKind.dimensionless, "Percent (bare, no denominator)"),
    UnitDefinition(
        "unit.ratio_to_baseline_gdp",
        "dimensionless",
        QuantityKind.dimensionless,
        "Ratio to baseline GDP",
        denominator="baseline_gdp",
    ),
    UnitDefinition(
        "unit.percent_of_baseline_gdp",
        "percent",
        QuantityKind.dimensionless,
        "Percent of baseline GDP",
        denominator="baseline_gdp",
    ),
)

# Explicit, enumerated short forms already used in the model registry. This is
# a lookup of declared spellings, not inference from field names.
UNIT_ALIASES: Mapping[str, str] = {
    "percent_of_baseline_gdp": "unit.percent_of_baseline_gdp",
    "ratio": "unit.ratio",
}

_BY_REF: dict[str, UnitDefinition] = {d.ref: d for d in UNIT_DEFINITIONS}


@lru_cache(maxsize=1)
def unit_registry() -> pint.UnitRegistry:  # type: ignore[type-arg]
    """The Pint registry with REVA's extra base dimensions (Decimal magnitudes)."""
    ureg: pint.UnitRegistry = pint.UnitRegistry(non_int_type=Decimal)  # type: ignore[type-arg]
    ureg.define("reva_person = [reva_population]")
    for d in UNIT_DEFINITIONS:
        if d.kind is QuantityKind.currency_amount:
            assert d.currency is not None
            # One base dimension per currency: never inter-convertible in Pint.
            ureg.define(f"reva_{d.currency} = [currency_{d.currency}]")
    return ureg


def is_known_unit(ref: str | None) -> bool:
    if not ref:
        return False
    return ref in _BY_REF or ref in UNIT_ALIASES


def canonical_unit_ref(ref: str | None) -> str:
    """Canonical ``unit.*`` reference; raises :class:`UnknownUnit` otherwise."""
    if ref is None or not str(ref).strip():
        raise UnknownUnit("unit_ref is missing; a unit is never inferred", unit_ref=ref)
    if ref in _BY_REF:
        return ref
    if ref in UNIT_ALIASES:
        return UNIT_ALIASES[ref]
    raise UnknownUnit(f"unit_ref {ref!r} is not registered in {UNIT_REGISTRY_VERSION}", unit_ref=ref)


def unit_definition(ref: str | None) -> UnitDefinition:
    return _BY_REF[canonical_unit_ref(ref)]


def is_currency_unit(ref: str | None) -> bool:
    return is_known_unit(ref) and unit_definition(ref).kind is QuantityKind.currency_amount


def dimensionality(ref: str) -> str:
    d = unit_definition(ref)
    return str(unit_registry().Unit(d.pint_unit).dimensionality)


def _dec(value: Decimal | str | int) -> Decimal:
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation as e:
        raise ValidationProblem(f"not a decimal: {value!r}") from e
    if not d.is_finite():
        raise ValidationProblem("non-finite values are forbidden")
    return d


def conversion_factor(src: str, dst: str) -> Decimal:
    """Exact multiplicative factor ``src -> dst`` or a typed refusal.

    Refusals (:class:`IncompatibleSemantics`): any currency change, mass<->volume,
    dimensionless units with different semantic denominators, and anything Pint
    finds dimensionally inconsistent.
    """
    a, b = unit_definition(src), unit_definition(dst)
    if a.ref == b.ref:
        return Decimal(1)
    if QuantityKind.currency_amount in (a.kind, b.kind):
        raise IncompatibleSemantics(
            f"{a.ref} -> {b.ref}: currency amounts convert only through a versioned CurrencyTransform "
            "carrying currency, price basis, price reference year and exchange-rate transformation",
            src=a.ref,
            dst=b.ref,
        )
    if {a.kind, b.kind} == {QuantityKind.mass, QuantityKind.volume}:
        raise IncompatibleSemantics(
            f"{a.ref} -> {b.ref}: mass and volume cannot be interchanged without an explicit density "
            "and named convention (use mass_to_volume/volume_to_mass with a DensityConvention)",
            src=a.ref,
            dst=b.ref,
        )
    if a.kind is QuantityKind.dimensionless and a.denominator != b.denominator:
        raise IncompatibleSemantics(
            f"{a.ref} -> {b.ref}: dimensionless units with different denominators "
            f"({a.denominator!r} vs {b.denominator!r}) are not interchangeable",
            src=a.ref,
            dst=b.ref,
        )
    ureg = unit_registry()
    try:
        q = ureg.Quantity(Decimal(1), a.pint_unit).to(b.pint_unit)
    except pint.DimensionalityError as e:
        raise IncompatibleSemantics(f"{a.ref} -> {b.ref}: dimensionally incompatible ({e})",
                                    src=a.ref, dst=b.ref) from e
    return _dec(q.magnitude)


def convert(value: Decimal | str | int, src: str, dst: str) -> Decimal:
    """Convert a magnitude between compatible physical units (exact Decimal)."""
    factor = conversion_factor(src, dst)
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        return _dec(value) * factor


# --------------------------------------------------------------------------- mass <-> volume


class DensityConvention(BaseModel):
    """An explicit density with a named, versioned convention.

    Example: a reviewed API-gravity-derived density for a named crude stream at
    a stated reference temperature. REVA ships no default: the caller must
    provide one with its evidence.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    convention_key: str = Field(min_length=1)
    version: str = Field(min_length=1)
    density_kg_per_m3: str
    reference_temperature_c: str | None = None
    evidence_ref: str = Field(min_length=1)

    @field_validator("density_kg_per_m3")
    @classmethod
    def _positive(cls, v: str) -> str:
        if _dec(v) <= 0:
            raise ValueError("density must be positive")
        return v


def _require_kind(ref: str, kind: QuantityKind) -> UnitDefinition:
    d = unit_definition(ref)
    if d.kind is not kind:
        raise IncompatibleSemantics(f"{ref} is a {d.kind.value} unit, expected {kind.value}", unit_ref=ref)
    return d


def mass_to_volume(value: Decimal | str, mass_unit: str, volume_unit: str,
                   convention: DensityConvention | None) -> Decimal:
    if convention is None:
        raise IncompatibleSemantics(
            "mass -> volume requires an explicit DensityConvention (density and convention)",
            src=mass_unit, dst=volume_unit,
        )
    _require_kind(mass_unit, QuantityKind.mass)
    _require_kind(volume_unit, QuantityKind.volume)
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        kg = convert(value, mass_unit, "unit.kg")
        m3 = kg / _dec(convention.density_kg_per_m3)
        return convert(m3, "unit.m3", volume_unit)


def volume_to_mass(value: Decimal | str, volume_unit: str, mass_unit: str,
                   convention: DensityConvention | None) -> Decimal:
    if convention is None:
        raise IncompatibleSemantics(
            "volume -> mass requires an explicit DensityConvention (density and convention)",
            src=volume_unit, dst=mass_unit,
        )
    _require_kind(volume_unit, QuantityKind.volume)
    _require_kind(mass_unit, QuantityKind.mass)
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        m3 = convert(value, volume_unit, "unit.m3")
        kg = m3 * _dec(convention.density_kg_per_m3)
        return convert(kg, "unit.kg", mass_unit)


# --------------------------------------------------------------------------- currency basis

Severity = Literal["error", "warning"]


class UnitIssue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    code: str
    severity: Severity
    message: str
    path: str = ""


class UnitCheckReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    issues: tuple[UnitIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    @property
    def errors(self) -> tuple[UnitIssue, ...]:
        return tuple(i for i in self.issues if i.severity == "error")

    def require(self) -> None:
        if not self.ok:
            raise ValidationProblem(
                "; ".join(f"{i.path or 'value'}: {i.message}" for i in self.errors),
                issues=[i.model_dump() for i in self.errors],
            )


def _basis(obj: MeasurementBasis | Mapping[str, Any] | None) -> MeasurementBasis | None:
    if obj is None or isinstance(obj, MeasurementBasis):
        return obj
    return MeasurementBasis.model_validate(dict(obj))


def currency_basis_issues(unit_ref: str, basis: MeasurementBasis | Mapping[str, Any] | None,
                          path: str = "") -> tuple[UnitIssue, ...]:
    """Issues that make a currency amount economically incomplete.

    Errors: no basis, no/mismatching currency, no price basis, real/PPP without
    price reference year. Warning: no exchange-rate transformation recorded.
    """
    d = unit_definition(unit_ref)
    if d.kind is not QuantityKind.currency_amount:
        return ()
    try:
        b = _basis(basis)
    except ValueError as e:
        return (UnitIssue(code="basis_invalid", severity="error", message=str(e), path=path),)
    out: list[UnitIssue] = []
    if b is None:
        return (UnitIssue(code="currency_basis_missing", severity="error", path=path,
                          message=f"{d.ref} amount without a measurement basis; currency alone is incomplete"),)
    if not b.currency:
        out.append(UnitIssue(code="currency_missing", severity="error", path=path,
                             message="currency amount without ISO 4217 currency in its basis"))
    elif b.currency != d.currency:
        out.append(UnitIssue(code="currency_unit_mismatch", severity="error", path=path,
                             message=f"basis currency {b.currency} does not match unit {d.ref}"))
    if b.price_basis is None:
        out.append(UnitIssue(code="price_basis_missing", severity="error", path=path,
                             message="currency amount without nominal/real/PPP price basis"))
    elif b.price_basis in ("real", "ppp") and b.price_reference_year is None:
        out.append(UnitIssue(code="price_reference_year_missing", severity="error", path=path,
                             message=f"{b.price_basis} amount without a price reference year"))
    if not b.exchange_rate_transform:
        out.append(UnitIssue(code="exchange_rate_transform_unrecorded", severity="warning", path=path,
                             message="exchange-rate transformation not recorded; cross-source combination "
                                     "must treat the conversion basis as unknown"))
    return tuple(out)


_UNIT_VALUE_KINDS = frozenset({"decimal", "float", "array"})


def validate_observation_units(observation: Mapping[str, Any], *, expected_unit_ref: str | None = None
                               ) -> UnitCheckReport:
    """Reject numeric observations whose ``unit_ref`` is missing or unknown.

    ``observation`` is either a normalised observation (with a ``value`` key)
    or a value dict itself. ``expected_unit_ref`` is the concept profile's unit;
    when given, the observation must declare exactly that unit. Missing values
    carry no unit and pass. A unit is never derived from any field name.
    """
    if "kind" in observation:
        value: Any = observation
        path = ""
    elif "value" in observation:
        value, path = observation["value"], "value"
    else:
        return UnitCheckReport(issues=(UnitIssue(code="value_missing", severity="error", path="value",
                                                 message="observation has no value"),))
    if not isinstance(value, Mapping):
        return UnitCheckReport(issues=(UnitIssue(code="value_invalid", severity="error", path=path,
                                                 message="value must be an object"),))
    kind = value.get("kind")
    if kind not in _UNIT_VALUE_KINDS:
        return UnitCheckReport()
    ref = value.get("unit_ref")
    p = f"{path}.unit_ref" if path else "unit_ref"
    if ref is None or (isinstance(ref, str) and not ref.strip()):
        return UnitCheckReport(issues=(UnitIssue(code="unit_ref_missing", severity="error", path=p,
                                                 message="numeric value without unit_ref"),))
    if not isinstance(ref, str) or not is_known_unit(ref):
        return UnitCheckReport(issues=(UnitIssue(code="unit_ref_unknown", severity="error", path=p,
                                                 message=f"unit_ref {ref!r} is not registered"),))
    issues: list[UnitIssue] = []
    if expected_unit_ref is not None:
        if not is_known_unit(expected_unit_ref):
            issues.append(UnitIssue(code="expected_unit_unknown", severity="error", path=p,
                                    message=f"concept unit {expected_unit_ref!r} is not registered"))
        elif canonical_unit_ref(ref) != canonical_unit_ref(expected_unit_ref):
            issues.append(UnitIssue(code="unit_ref_mismatch", severity="error", path=p,
                                    message=f"value unit {ref} differs from concept unit {expected_unit_ref}"))
    bpath = f"{path}.basis" if path else "basis"
    issues.extend(currency_basis_issues(ref, value.get("basis"), bpath))
    return UnitCheckReport(issues=tuple(issues))


# --------------------------------------------------------------------------- currency transforms

PriceBasis = Literal["nominal", "real", "ppp"]


class CurrencyBasisSpec(BaseModel):
    """The currency/price side of a transform. ``price_reference_year=None`` with
    ``nominal`` means current prices of each value's own period."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    price_basis: PriceBasis
    price_reference_year: int | None = None

    @model_validator(mode="after")
    def _ref_year(self) -> CurrencyBasisSpec:
        if self.price_basis in ("real", "ppp") and self.price_reference_year is None:
            raise ValueError(f"{self.price_basis} basis requires a price reference year")
        return self


class CurrencyTransform(BaseModel):
    """A versioned, evidence-backed currency/price-basis transformation.

    ``factors`` maps a value's reference-period year to the multiplicative
    factor (target per source unit). Factors are data supplied with evidence;
    a missing year is a missing input, never interpolated.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    transform_key: str = Field(min_length=1)
    version: str = Field(min_length=1)
    source: CurrencyBasisSpec
    target: CurrencyBasisSpec
    exchange_rate_transform: str = Field(min_length=1)
    factors: dict[int, str]
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    approval: Literal["approved", "candidate", "rejected"]

    @field_validator("factors")
    @classmethod
    def _factors(cls, v: dict[int, str]) -> dict[int, str]:
        if not v:
            raise ValueError("a currency transform needs at least one period factor")
        for year, f in v.items():
            if _dec(f) <= 0:
                raise ValueError(f"factor for {year} must be positive")
        return v

    @model_validator(mode="after")
    def _changes_something(self) -> CurrencyTransform:
        if self.source == self.target:
            raise ValueError("transform source and target bases are identical")
        return self


def _unit_for_currency(currency: str) -> str:
    for d in UNIT_DEFINITIONS:
        if d.currency == currency:
            return d.ref
    raise UnknownUnit(f"no registered unit for currency {currency}", currency=currency)


def apply_currency_transform(value: ScalarDecimal, period_year: int, transform: CurrencyTransform) -> ScalarDecimal:
    """Re-express a currency amount on another basis through an approved transform."""
    if transform.approval != "approved":
        raise IncompatibleSemantics(
            f"currency transform {transform.transform_key}@{transform.version} is {transform.approval}, not approved"
        )
    errors = [i for i in currency_basis_issues(value.unit_ref, value.basis) if i.severity == "error"]
    if errors:
        raise IncompatibleSemantics("source amount has an incomplete currency basis: "
                                    + "; ".join(i.message for i in errors))
    b = value.basis
    src = transform.source
    if (b.currency, b.price_basis) != (src.currency, src.price_basis) or (
        src.price_reference_year is not None and b.price_reference_year != src.price_reference_year
    ):
        raise IncompatibleSemantics(
            f"value basis ({b.currency}, {b.price_basis}, {b.price_reference_year}) does not match transform "
            f"source ({src.currency}, {src.price_basis}, {src.price_reference_year})"
        )
    if b.scale_factor != "1":
        raise IncompatibleSemantics("scaled values must be rescaled to scale_factor 1 before transformation")
    factor = transform.factors.get(period_year)
    if factor is None:
        raise MissingInput(
            f"transform {transform.transform_key}@{transform.version} has no factor for {period_year}",
            input="currency_factor", reason="unavailable",
        )
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        out = value.as_decimal() * _dec(factor)
    tgt = transform.target
    new_basis = b.model_copy(update={
        "currency": tgt.currency,
        "price_basis": tgt.price_basis,
        "price_reference_year": tgt.price_reference_year,
        "exchange_rate_transform": (
            f"{transform.transform_key}@{transform.version}: {transform.exchange_rate_transform}"
        ),
    })
    return ScalarDecimal(value=decimal_str(out), unit_ref=_unit_for_currency(tgt.currency), basis=new_basis)
