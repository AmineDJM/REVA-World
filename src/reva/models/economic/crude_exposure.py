"""economic.crude_exposure 1.0.0 — one-period crude-oil border-value exposure accounting.

This is an assumption-conditioned border-value stress test, not a forecast.

L1 (fixed quantities), for price multipliers p_M, p_X > 0::

    ΔM    = M × (p_M − 1)
    ΔX    = X × (p_X − 1)
    Δnet  = ΔM − ΔX
    exposure_percent_baseline_gdp = 100 × Δnet / G

L2 (labelled elasticity sensitivity, assumed quantity elasticities ε)::

    M' = M × p_M^(1 + ε_M)        ΔM = M' − M = M × (p_M^(1 + ε_M) − 1)
    X' = X × p_X^(1 + ε_X)        ΔX = X' − X = X × (p_X^(1 + ε_X) − 1)

The implementation evaluates ΔM as ``M × (p^(1+ε) − 1)`` and special-cases the
exponent 1 so that ε = 0 executes exactly the L1 expression: the zero-elasticity
L2 case is bit-for-bit identical to L1, not merely close.

All arithmetic uses ``decimal`` in a 50-digit context. Integral exponents use exact
Decimal integer powers; non-integral exponents use exp((1 + ε)·ln p), which the
standard library rounds correctly in that context. Inputs are never silently
zero: a missing value raises :class:`MissingInput`.

M and X are same-year source-reported border values in current USD (imports
commonly CIF, exports FOB; both bases stay explicit). G is nominal GDP in current
USD for the same year. Negative net cost indicates net receipt exposure under the
stated assumptions. Outputs are not GDP loss, welfare change, fiscal revenue,
consumer inflation, an oil demand elasticity or a balance-of-payments forecast.

Pure module: imports only the standard library and the kernel.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Context, Decimal, localcontext
from typing import Any

from reva.kernel.errors import CapabilityGap, IncompatibleSemantics, MissingInput, ValidationProblem
from reva.kernel.values import Missing, MissingReason, decimal_str, parse_value

MODEL_KEY = "economic.crude_exposure"
MODEL_VERSION = "1.0.0"
PRECISION = 50
_CTX = Context(prec=PRECISION)

OUTPUT_KEYS: tuple[str, ...] = (
    "import_value_change_usd",
    "export_value_change_usd",
    "net_external_cost_change_usd",
    "exposure_percent_baseline_gdp",
)
USD = "unit.usd"
PERCENT_OF_BASELINE_GDP = "unit.percent_of_baseline_gdp"
OUTPUT_UNITS: dict[str, str] = {
    "import_value_change_usd": USD,
    "export_value_change_usd": USD,
    "net_external_cost_change_usd": USD,
    "exposure_percent_baseline_gdp": PERCENT_OF_BASELINE_GDP,
}

INPUT_CONCEPTS: dict[str, str] = {
    "M": "trade.hs2709.import_value.world_total",
    "X": "trade.hs2709.export_value.world_total",
    "G": "economics.gdp.nominal_current_usd",
}
INPUT_LABELS: dict[str, str] = {
    "M": "crude (HS 2709) import border value from World",
    "X": "crude (HS 2709) export border value to World",
    "G": "nominal GDP, current USD",
}

# Registered applicability envelope (implementation limits, not empirical claims).
MULTIPLIER_MAX = Decimal(3)
IMPORT_ELASTICITY_ENVELOPE = (Decimal(-2), Decimal(1))
EXPORT_ELASTICITY_ENVELOPE = (Decimal(-1), Decimal(2))
SUPPORTED_HORIZON = (1, "annual_accounting_period")
NOMINAL_PRICE_BASES = frozenset({"nominal", "nominal_current"})

# Pure-data port descriptor used by the coupling layer (concept, unit, basis, aggregation).
DESCRIPTOR: dict[str, Any] = {
    "model_key": MODEL_KEY,
    "version": MODEL_VERSION,
    "input_ports": [
        {"name": n, "concept": c, "unit": USD, "basis": "nominal_current_usd",
         "aggregation": "period_flow_total", "temporal_support": "calendar_year"}
        for n, c in INPUT_CONCEPTS.items()
    ],
    "output_ports": [
        {"name": k, "concept": f"scenario.{MODEL_KEY}.{k}", "unit": u,
         "basis": "nominal_current_usd" if u == USD else "ratio_to_baseline_gdp",
         "aggregation": "period_flow_total", "temporal_support": "calendar_year"}
        for k, u in OUTPUT_UNITS.items()
    ],
}

NOT_CLAIMED = (
    "This is an assumption-conditioned border-value accounting exposure for one annual accounting "
    "period. It is not a GDP loss, welfare change, fiscal revenue, consumer inflation, unemployment, "
    "oil demand elasticity or balance-of-payments forecast, and it has no general-equilibrium closure."
)


def dstr(d: Decimal) -> str:
    """Plain decimal string at full model precision.

    ``kernel.values.decimal_str`` normalises in the *thread* context (28 digits by
    default), which would silently round 50-digit results; format inside the model context.
    """
    with localcontext(_CTX):
        return decimal_str(d)


def _dec(x: Any, what: str) -> Decimal:
    """Exact Decimal conversion; floats go through their shortest repr (1.25 -> '1.25')."""
    if isinstance(x, bool):
        raise ValidationProblem(f"{what} must be numeric, not boolean")
    if isinstance(x, Decimal):
        d = x
    elif isinstance(x, int):
        d = Decimal(x)
    elif isinstance(x, float):
        d = Decimal(repr(x))
    elif isinstance(x, str):
        d = Decimal(x)
    else:
        raise ValidationProblem(f"{what} must be numeric (got {type(x).__name__})")
    if not d.is_finite():
        raise ValidationProblem(f"{what} must be finite")
    return d


def _power(p: Decimal, exponent: Decimal) -> Decimal:
    """p ** exponent in the model context; exponent 1 returns p unchanged (exact L1 case)."""
    with localcontext(_CTX):
        if exponent == 1:
            return p
        if exponent == exponent.to_integral_value():
            return p ** int(exponent)
        return (exponent * p.ln()).exp()


def _check_multiplier(p: Decimal, what: str) -> None:
    if p <= 0:
        raise ValidationProblem(f"{what} must be > 0 (got {p})", input=what)
    if p > MULTIPLIER_MAX:
        raise CapabilityGap(f"{what} {p} is outside the registered multiplier envelope (0, 3]",
                            capability="multiplier_envelope")


def _check_elasticity(e: Decimal, envelope: tuple[Decimal, Decimal], what: str) -> None:
    if not envelope[0] <= e <= envelope[1]:
        raise CapabilityGap(f"{what} {e} is outside the registered envelope [{envelope[0]}, {envelope[1]}]",
                            capability="elasticity_envelope")


@dataclass(frozen=True)
class ExposureResult:
    """Per-country outputs (Decimals) for one variant (L1 or one L2 grid point)."""

    variant: str
    import_multiplier: Decimal
    export_multiplier: Decimal
    import_elasticity: Decimal
    export_elasticity: Decimal
    baseline_import_value_usd: Decimal
    baseline_export_value_usd: Decimal
    baseline_gdp_usd: Decimal
    scenario_import_value_usd: Decimal
    scenario_export_value_usd: Decimal
    import_value_change_usd: Decimal
    export_value_change_usd: Decimal
    net_external_cost_change_usd: Decimal
    exposure_percent_baseline_gdp: Decimal

    def outputs(self) -> dict[str, Decimal]:
        return {k: getattr(self, k) for k in OUTPUT_KEYS}

    def as_json(self) -> dict[str, str]:
        return {
            k: (dstr(v) if isinstance(v, Decimal) else str(v))
            for k, v in self.__dict__.items()
        }


def _compute(M: Decimal, X: Decimal, G: Decimal, p_m: Decimal, p_x: Decimal,
             e_m: Decimal, e_x: Decimal, variant: str) -> ExposureResult:
    with localcontext(_CTX):
        f_m = _power(p_m, 1 + e_m)
        f_x = _power(p_x, 1 + e_x)
        d_m = M * (f_m - 1)
        d_x = X * (f_x - 1)
        d_net = d_m - d_x
        exposure = 100 * d_net / G
        return ExposureResult(
            variant=variant,
            import_multiplier=p_m,
            export_multiplier=p_x,
            import_elasticity=e_m,
            export_elasticity=e_x,
            baseline_import_value_usd=M,
            baseline_export_value_usd=X,
            baseline_gdp_usd=G,
            scenario_import_value_usd=M + d_m,
            scenario_export_value_usd=X + d_x,
            import_value_change_usd=d_m,
            export_value_change_usd=d_x,
            net_external_cost_change_usd=d_net,
            exposure_percent_baseline_gdp=exposure,
        )


def _check_values(M: Decimal, X: Decimal, G: Decimal) -> None:
    if M < 0 or X < 0:
        raise ValidationProblem("border values M and X must be non-negative")
    if G <= 0:
        raise ValidationProblem("baseline nominal GDP must be positive")


def exposure_l1(M: Any, X: Any, G: Any, import_multiplier: Any, export_multiplier: Any) -> ExposureResult:
    """Deterministic fixed-quantity exposure (L1)."""
    m, x, g = _dec(M, "M"), _dec(X, "X"), _dec(G, "G")
    p_m, p_x = _dec(import_multiplier, "import_multiplier"), _dec(export_multiplier, "export_multiplier")
    _check_values(m, x, g)
    _check_multiplier(p_m, "import_multiplier")
    _check_multiplier(p_x, "export_multiplier")
    return _compute(m, x, g, p_m, p_x, Decimal(0), Decimal(0), "L1")


def l2_variant_name(import_elasticity: Any, export_elasticity: Any) -> str:
    e_m, e_x = _dec(import_elasticity, "import_elasticity"), _dec(export_elasticity, "export_elasticity")
    return f"L2:eM={dstr(e_m)};eX={dstr(e_x)}"


def exposure_l2(M: Any, X: Any, G: Any, import_multiplier: Any, export_multiplier: Any,
                import_elasticity: Any, export_elasticity: Any) -> ExposureResult:
    """Elasticity-sensitivity variant (L2). ε = 0 reproduces L1 exactly."""
    m, x, g = _dec(M, "M"), _dec(X, "X"), _dec(G, "G")
    p_m, p_x = _dec(import_multiplier, "import_multiplier"), _dec(export_multiplier, "export_multiplier")
    e_m, e_x = _dec(import_elasticity, "import_elasticity"), _dec(export_elasticity, "export_elasticity")
    _check_values(m, x, g)
    _check_multiplier(p_m, "import_multiplier")
    _check_multiplier(p_x, "export_multiplier")
    _check_elasticity(e_m, IMPORT_ELASTICITY_ENVELOPE, "import_elasticity")
    _check_elasticity(e_x, EXPORT_ELASTICITY_ENVELOPE, "export_elasticity")
    return _compute(m, x, g, p_m, p_x, e_m, e_x, l2_variant_name(e_m, e_x))


# --------------------------------------------------------------------------- typed inputs
@dataclass(frozen=True)
class MonetaryInput:
    """One model input with its semantics. ``value`` None or Missing means missing."""

    name: str  # "M" | "X" | "G"
    value: Decimal | Missing | None
    reference_year: int | None
    unit_ref: str | None = USD
    currency: str | None = "USD"
    price_basis: str | None = "nominal"
    price_reference_year: int | None = None
    aggregation: str | None = "period_flow_total"
    valuation: str | None = None
    source_flags: Mapping[str, Any] = field(default_factory=dict)
    estimate_id: str | None = None

    @classmethod
    def from_value_dict(cls, name: str, value: Mapping[str, Any] | None, *, reference_year: int | None,
                        aggregation: str | None, source_flags: Mapping[str, Any] | None = None,
                        estimate_id: str | None = None) -> MonetaryInput:
        """Build from a kernel value dict (as stored on belief estimates)."""
        if value is None:
            return cls(name, None, reference_year, aggregation=aggregation,
                       source_flags=dict(source_flags or {}), estimate_id=estimate_id)
        v = parse_value(dict(value))
        if isinstance(v, Missing):
            return cls(name, v, reference_year, aggregation=aggregation,
                       source_flags=dict(source_flags or {}), estimate_id=estimate_id)
        kind = getattr(v, "kind", None)
        if kind not in ("decimal", "float"):
            raise IncompatibleSemantics(f"input {name} is a {kind} value, not a monetary quantity", input=name)
        basis = v.basis
        num = v.as_decimal() if kind == "decimal" else Decimal(repr(v.value))
        return cls(
            name=name,
            value=num,
            reference_year=reference_year,
            unit_ref=v.unit_ref,
            currency=basis.currency,
            price_basis=basis.price_basis,
            price_reference_year=basis.price_reference_year,
            aggregation=aggregation,
            valuation=basis.valuation,
            source_flags=dict(source_flags or {}),
            estimate_id=estimate_id,
        )

    @property
    def is_missing(self) -> bool:
        return self.value is None or isinstance(self.value, Missing)


@dataclass(frozen=True)
class CountryInputs:
    country: str
    imports: MonetaryInput
    exports: MonetaryInput
    gdp: MonetaryInput

    def by_name(self) -> dict[str, MonetaryInput]:
        return {"M": self.imports, "X": self.exports, "G": self.gdp}


def check_horizon(periods: int, unit: str) -> None:
    if (periods, unit) != SUPPORTED_HORIZON:
        raise CapabilityGap(
            f"horizon {periods} x {unit} is unsupported: only one annual accounting period; "
            "dynamic multi-year effects are not produced by this accounting model",
            capability="horizon.multi_period",
        )


def validate_country_inputs(ci: CountryInputs, *, reference_year: int, horizon_periods: int = 1,
                            horizon_unit: str = "annual_accounting_period") -> tuple[Decimal, Decimal, Decimal]:
    """Return (M, X, G) or raise MissingInput / IncompatibleSemantics / CapabilityGap.

    Missing inputs are never replaced with zero. All inputs must be current-USD
    nominal annual flows for the same reference year as requested.
    """
    check_horizon(horizon_periods, horizon_unit)
    missing = [
        {"country": ci.country, "input": n, "concept": INPUT_CONCEPTS[n], "reference_year": reference_year,
         "reason": (inp.value.reason.value if isinstance(inp.value, Missing) else MissingReason.unavailable.value)}
        for n, inp in ci.by_name().items() if inp.is_missing
    ]
    if missing:
        names = ", ".join(f"{m['input']} ({m['concept']})" for m in missing)
        raise MissingInput(f"{ci.country} {reference_year}: required input(s) missing: {names}",
                           country=ci.country, reference_year=reference_year, missing=missing)
    years = {n: inp.reference_year for n, inp in ci.by_name().items()}
    if any(y != reference_year for y in years.values()):
        raise IncompatibleSemantics(
            f"{ci.country}: inputs must share the requested reference year {reference_year} (got {years})",
            country=ci.country, reference_years=years,
        )
    for n, inp in ci.by_name().items():
        problems = []
        if inp.unit_ref != USD:
            problems.append(f"unit {inp.unit_ref!r} is not {USD}")
        if (inp.currency or "").upper() != "USD":
            problems.append(f"currency {inp.currency!r} is not USD")
        if inp.price_basis not in NOMINAL_PRICE_BASES:
            problems.append(f"price basis {inp.price_basis!r} is not nominal current prices")
        if inp.price_reference_year is not None:
            problems.append(f"constant-price reference year {inp.price_reference_year} is not current USD")
        if inp.aggregation != "period_flow_total":
            problems.append(f"aggregation {inp.aggregation!r} is not an annual period flow total")
        if problems:
            raise IncompatibleSemantics(
                f"{ci.country} input {n}: mixed or unsupported basis: " + "; ".join(problems),
                country=ci.country, input=n, problems=problems,
            )
    m, x, g = (inp.value for inp in (ci.imports, ci.exports, ci.gdp))
    assert isinstance(m, Decimal) and isinstance(x, Decimal) and isinstance(g, Decimal)
    _check_values(m, x, g)
    return m, x, g


def evaluate_country(ci: CountryInputs, *, reference_year: int, import_multiplier: Any, export_multiplier: Any,
                     elasticity_grid: tuple[tuple[Any, Any], ...] | None = None,
                     horizon_periods: int = 1, horizon_unit: str = "annual_accounting_period",
                     ) -> list[ExposureResult]:
    """Validate typed inputs then compute L1 (no grid) or every L2 grid point."""
    m, x, g = validate_country_inputs(ci, reference_year=reference_year, horizon_periods=horizon_periods,
                                      horizon_unit=horizon_unit)
    if not elasticity_grid:
        return [exposure_l1(m, x, g, import_multiplier, export_multiplier)]
    return [exposure_l2(m, x, g, import_multiplier, export_multiplier, em, ex) for em, ex in elasticity_grid]


def sensitivity_range(results: list[ExposureResult], output_key: str) -> tuple[Decimal, Decimal]:
    """Min/max of an output across the given L2 variants: a sensitivity range, never a confidence interval.

    The range covers exactly the variants passed. Callers that publish it next to the L1
    value must include the ε=(0,0) variant (bit-identical to L1) so the range contains L1.
    """
    if not results:
        raise ValidationProblem("empty elasticity grid")
    vals = [getattr(r, output_key) for r in results]
    return min(vals), max(vals)


def _input_flags(inp: MonetaryInput) -> dict[str, Any]:
    f = dict(inp.source_flags)
    out: dict[str, Any] = {
        "concept": INPUT_CONCEPTS.get(inp.name),
        "valuation": inp.valuation,
        "reference_year": inp.reference_year,
    }
    # The inputs are trade values and GDP. Comtrade's legacyEstimationFlag (0/2/4/6) codes estimated
    # quantity/net weight, never the USD value, so neither it nor the 1.1.0 derived key
    # provider_estimation_flagged (code != 0) labels a value input as estimated; only an explicit
    # provider_value_estimated=true does (Comtrade 1.2.0 emits null: undeterminable from the response).
    for k in ("provider_value_estimated", "legacyEstimationFlag", "provider_derived_aggregate", "isReported",
              "isAggregate", "classificationCode", "indicator", "obs_status", "provider_returned_no_rows"):
        if k in f:
            out[k] = f[k]
    if f.get("provider_value_estimated") is True:
        out["note"] = "provider marks this value as estimated"
    elif f.get("isReported") is False or f.get("provider_derived_aggregate"):
        out["note"] = "provider marks this value as not directly reported (isReported=false)"
    return out


def explain(inputs: CountryInputs | None = None, *, fidelity: str = "L1") -> dict[str, Any]:
    """Human-readable explanation block attached to every result."""
    flags: dict[str, Any] = {}
    valuation_note = "imports commonly CIF and exports FOB; the provider-declared basis of each value is kept"
    if inputs is not None:
        flags = {n: _input_flags(i) for n, i in inputs.by_name().items()}
        if inputs.imports.valuation or inputs.exports.valuation:
            valuation_note = (f"import value basis: {inputs.imports.valuation or 'undeclared'}; "
                              f"export value basis: {inputs.exports.valuation or 'undeclared'}. Applying a "
                              "multiplier to a CIF value includes that border-value convention.")
    return {
        "model": f"{MODEL_KEY}@{MODEL_VERSION}",
        "modality": "assumption_conditioned_scenario",
        "sign_convention": (
            "net_external_cost_change_usd = import_value_change_usd − export_value_change_usd. Positive means "
            "a higher net external payment for crude under the stated assumptions; negative net cost "
            "indicates net receipt exposure (exports gain more value than imports cost)."
        ),
        "units": {
            "import_value_change_usd": "current US dollars over one annual accounting period",
            "export_value_change_usd": "current US dollars over one annual accounting period",
            "net_external_cost_change_usd": "current US dollars over one annual accounting period",
            "exposure_percent_baseline_gdp": "percent of same-year baseline nominal GDP (current USD)",
        },
        "assumptions": [
            "same-year source-reported border values and nominal GDP, all in current USD",
            valuation_note,
            "one annual accounting period; no dynamics",
            "fixed quantities (L1)" if fidelity != "L2" else
            "assumed quantity elasticities over a labelled grid (L2); elasticities are scenario assumptions "
            "with no probability interpretation",
            "no general equilibrium, exchange-rate, fiscal or second-round effects",
            "a headline crude benchmark price change is not automatically this border-price intervention",
        ],
        "uncertainty": (
            "Unquantified: the sources publish no uncertainty for M, X or G and no justified error model is "
            "introduced. In an L2 run the min/max over the elasticity grid together with the fixed-quantity "
            "point ε=(0,0) is published as a separate row: a sensitivity range, not a confidence interval "
            "(assumption sensitivity, no coverage level); the L1 value keeps its unquantified status."
        ),
        "numerical": f"Decimal arithmetic at {PRECISION} significant digits",
        "source_flags": flags,
        "not_claimed": NOT_CLAIMED,
    }
