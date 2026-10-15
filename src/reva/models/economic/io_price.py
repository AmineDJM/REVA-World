"""economic.io_price 1.0.0 — multi-region Leontief cost-push price model.

**First-round cost-push under full pass-through; not an inflation forecast.**

What it computes, for one reference year of an inter-country industry-by-industry
input-output table (Eurostat FIGARO, dataset input ``economics.io.figaro_ii_table``):

* industries ``k = (country, industry)``, N of them (FIGARO: 50 economies x 64 = 3200);
  ``Z[i, j]`` intermediate purchases of j from i, ``x_j`` gross output (row total, verified
  equal to the column total), technical coefficients ``A[i, j] = Z[i, j] / x_j``;
* the intervention sets the output price of chosen industries ``S`` (e.g. NACE ``B`` mining
  and quarrying in Saudi Arabia and Norway) exogenously to ``1 + π_s`` (baseline prices are 1);
* every other industry with positive output (set ``E``) passes on its unit-cost increase:

      Δp_E = θ (A_EEᵀ Δp_E + A_SEᵀ π_S)    ⇔    (I − θ A_EEᵀ) Δp_E = θ A_SEᵀ π_S

  with pass-through share θ (L1: θ = 1, full pass-through; L2: a labelled grid of θ in (0, 1]).
  This is the Leontief price model with exogenously priced sectors: "Δp = (I − Aᵀ)⁻¹ applied to
  the cost shock on inputs from the shocked industries", restricted to the non-shocked
  industries so that the shocked prices stay exactly at the stated change;
* the household basket of country d: ``Σ_k h_kd Δp_k / Σ_k h_kd`` with ``h`` the
  households' final consumption expenditure (``P3_S14``) of country d bought from every
  industry k (domestic and imported), valued at basic prices.

Contribution decomposition of each basket change (both sum exactly to the basket change
before rounding):

* by product origin — domestic products (k in d) vs imported products (k abroad);
* by shock origin — shocks located in d vs shocks located abroad (one extra right-hand side
  per shocked origin; the system is linear so contributions add up).

Assumptions (all stated on every result): fixed technical coefficients, no substitution,
no quantity or demand response, single period, nominal prices at basic prices, uniform pass-
through share θ for every non-shocked industry, taxes on products, margins already embedded
in trade industries, wages and profits per unit unchanged except for the absorbed share
(1 − θ). No exchange-rate, monetary, wage or second-round response. The basket is the
households' basic-price purchases, not a consumer price index (no product taxes, no
direct purchases abroad, fixed weights).

The shocked industry is an *industry* of the table, never a product sub-heading: FIGARO's
``B`` "Mining and quarrying" (NACE B05-B09) contains coal and lignite, crude petroleum and
natural gas, metal ores, other mining and quarrying, and mining support services. A shock to
``B`` is therefore a shock to the whole of that industry's output price, which is broader
than crude petroleum (the crude share of B differs widely by country). ``C19`` (coke and
refined petroleum products) and ``D35`` (electricity, gas, steam and air conditioning) can
also be shocked.

Numerics: the table is converted to float64 (the published values have 3 decimals in million
EUR; float64 holds them to ~1e-16 relative). The linear systems are solved by LU
factorisation with partial pivoting (LAPACK ``getrf``/``getrs`` through SciPy); every
solution is checked by its relative residual ``‖M x − b‖₂ / ‖b‖₂ < 1e-10`` and the run is
refused otherwise. Productivity is checked before solving: a rigorous Collatz-Wielandt upper
bound of the spectral radius of |A| must be < 1. Results are Decimal strings rounded half-even
to 6 decimals of a percent; rounding can make displayed contributions differ from the
displayed total by one unit in the last place.

Pure module: no database, network or clock (numpy, scipy, pyarrow and the kernel only).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import numpy as np

from reva.kernel.errors import CapabilityGap, IntegrityFailure, ValidationProblem
from reva.kernel.ids import entity_id, logical_hash
from reva.kernel.uncertainty import UncertaintyKind, UncertaintySpec, UncertaintyStatus

MODEL_KEY = "economic.io_price"
MODEL_VERSION = "1.0.0"
REVIEW_STATUS = "maintainer_review_pending"
LABEL = "first-round cost-push under full pass-through; not an inflation forecast"
L2_LABEL = ("first-round cost-push under a stated pass-through share; sensitivity over the pass-through grid; "
            "not an inflation forecast")

DATASET_INPUT = "io_table"
DATASET_CONCEPT = "economics.io.figaro_ii_table"
DATASET_SUBJECT = "WORLD"
HOUSEHOLD_FINAL_DEMAND = "P3_S14"
REST_OF_WORLD_KEY = "FIGARO_ROW"

#: Industries that may be shocked, with exactly what they contain (NACE Rev. 2 / CPA 2.1).
SHOCKABLE_INDUSTRIES: dict[str, str] = {
    "B": ("Mining and quarrying (NACE B05-B09): coal and lignite; crude petroleum and natural gas; metal ores; "
          "other mining and quarrying (stone, sand, clay, chemical minerals, salt, peat); mining support service "
          "activities. Broader than crude petroleum: a price change of B applies to all of these."),
    "C19": "Manufacture of coke and refined petroleum products (NACE C19).",
    "D35": "Electricity, gas, steam and air conditioning supply (NACE D35; includes manufactured-gas distribution).",
}
#: Registered applicability envelope (implementation limits, not empirical claims).
PERCENT_MIN_EXCLUSIVE = Decimal(-100)
PERCENT_MAX = Decimal(200)
MAX_SHOCKS = 200
DEFAULT_PASS_THROUGH_GRID: tuple[Decimal, ...] = (Decimal("0.5"), Decimal("0.75"), Decimal("1"))
RESULT_DECIMALS = 6
_QUANT = Decimal(1).scaleb(-RESULT_DECIMALS)
RESIDUAL_TOLERANCE = 1e-10
SPECTRAL_ITERATIONS = 200
SUPPORTED_HORIZON = {"periods": 1, "unit": "annual_accounting_period"}

OUTPUT_GROUPS: tuple[str, ...] = (
    "industry_price_change_percent",
    "household_basket_price_change_percent",
    "household_basket_contributions",
)
BASKET_KEY = "household_basket_price_change_percent"
CONTRIBUTION_KEYS: tuple[str, ...] = (
    "household_basket_contribution_domestic_products_percent",
    "household_basket_contribution_imported_products_percent",
    "household_basket_contribution_domestic_shocks_percent",
    "household_basket_contribution_foreign_shocks_percent",
)
INDUSTRY_PREFIX = "industry_price_change_percent:"
UNIT = "unit.percent"
SENSITIVITY_VARIANT = "L2:sensitivity_range"
SENSITIVITY_METHOD = "reva.models.economic.io_price.pass_through_grid_minmax@1.0.0"
SENSITIVITY_STATEMENT = "assumption sensitivity, not a confidence interval"

#: FIGARO economies by REVA key (ISO 3166 alpha-3; the rest of the world is FIGARO_ROW).
#: Must equal ``reva.adapters.connectors.io_tables.ECONOMIES`` (tested); the dataset manifest
#: is checked against it at compile time.
FIGARO_KEYS: tuple[str, ...] = (
    "BEL", "BGR", "CZE", "DNK", "DEU", "EST", "IRL", "GRC", "ESP", "FRA", "HRV", "ITA", "CYP", "LVA", "LTU", "LUX",
    "HUN", "MLT", "NLD", "AUT", "POL", "PRT", "ROU", "SVN", "SVK", "FIN", "SWE", "NOR", "CHE", "GBR", "MNE", "MKD",
    "ALB", "SRB", "TUR", "RUS", "ZAF", "CAN", "USA", "MEX", "ARG", "BRA", "CHN", "JPN", "KOR", "IND", "IDN", "SAU",
    "AUS", REST_OF_WORLD_KEY,
)

NOT_CLAIMED = (
    "First-round cost-push price changes under fixed input-output coefficients with the stated pass-through. Not "
    "an inflation forecast, not a consumer price index, not a GDP, welfare, wage, exchange-rate or monetary-policy "
    "response, no substitution between inputs, no demand or quantity response, one period, no dynamics."
)
ASSUMPTIONS: tuple[str, ...] = (
    "fixed technical coefficients A = Z / x of the reference-year table (no substitution)",
    "no demand, quantity, wage, exchange-rate or monetary response; one annual period; nominal basic prices",
    "the shocked industries' output prices change exactly by the stated percentage (exogenous prices)",
    "every other industry passes on the stated share θ of its unit-cost increase (L1: θ = 1, full pass-through)",
    "household basket weights: households' final consumption expenditure (P3_S14) by supplying industry at basic "
    "prices, domestic and imported; product taxes and direct purchases abroad are outside the basket",
    "a shocked industry is a whole table industry (e.g. NACE B mining and quarrying is broader than crude oil)",
)


# --------------------------------------------------------------------------- decimal helpers
def _dec(v: Any, what: str) -> Decimal:
    if isinstance(v, bool) or v is None:
        raise ValidationProblem(f"{what} must be a number")
    try:
        d = Decimal(repr(v)) if isinstance(v, float) else Decimal(str(v))
    except InvalidOperation as e:
        raise ValidationProblem(f"{what} is not a number: {v!r}") from e
    if not d.is_finite():
        raise ValidationProblem(f"{what} must be finite")
    return d


def to_result_decimal(x: float) -> Decimal:
    """Float64 result -> Decimal rounded half-even to RESULT_DECIMALS (no negative zero)."""
    if not np.isfinite(x):
        raise IntegrityFailure("non-finite model result")
    d = Decimal(repr(float(x))).quantize(_QUANT, rounding=ROUND_HALF_EVEN)
    return d if d != 0 else Decimal(0).quantize(_QUANT)


def dstr(d: Decimal) -> str:
    return format(d, "f")


# --------------------------------------------------------------------------- spec
@dataclass(frozen=True)
class Shock:
    origin: str  # REVA key: ISO3 or FIGARO_ROW
    industry: str
    percent: Decimal

    @property
    def fraction(self) -> float:
        return float(self.percent / 100)

    def as_json(self) -> dict[str, str]:
        return {"origin": self.origin, "industry": self.industry, "price_change_percent": dstr(self.percent)}


@dataclass(frozen=True)
class IOSpec:
    reference_year: int
    countries: tuple[str, ...]
    shocks: tuple[Shock, ...]
    pass_through_grid: tuple[Decimal, ...]
    outputs: tuple[str, ...]

    def grid_strings(self) -> list[str]:
        return [dstr(t) for t in self.pass_through_grid]


def parse_spec(spec: Mapping[str, Any]) -> IOSpec:
    """Model-specific checks (the JSON schema covers types; this covers meaning)."""
    horizon = dict(spec.get("horizon") or SUPPORTED_HORIZON)
    if horizon != SUPPORTED_HORIZON:
        raise CapabilityGap("economic.io_price is single-period: horizon must be 1 annual_accounting_period",
                            capability="horizon.multi_period")
    try:
        year = int(spec["reference_year"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValidationProblem("reference_year is required") from e
    countries = tuple(str(c) for c in spec.get("countries") or ())
    if not countries:
        raise ValidationProblem("countries must list at least one economy to report")
    unknown = [c for c in countries if c not in FIGARO_KEYS or c == REST_OF_WORLD_KEY]
    if unknown:
        raise CapabilityGap(f"not separately covered by the FIGARO table (part of its rest of the world or not a "
                            f"country): {unknown}", capability="economic.io_price.coverage", countries=unknown)
    if len(set(countries)) != len(countries):
        raise ValidationProblem("countries must be unique")
    inter = spec.get("intervention") or {}
    if inter.get("kind") != "set_exogenous_industry_price_change":
        raise ValidationProblem("intervention.kind must be set_exogenous_industry_price_change")
    raw = inter.get("shocks") or []
    if not raw or len(raw) > MAX_SHOCKS:
        raise ValidationProblem(f"intervention.shocks must list 1..{MAX_SHOCKS} shocks")
    shocks: list[Shock] = []
    for i, s in enumerate(raw):
        origin, industry = str(s.get("origin")), str(s.get("industry"))
        if origin not in FIGARO_KEYS:
            raise CapabilityGap(f"shock {i}: origin {origin!r} is not a FIGARO economy (use FIGARO_ROW for the rest "
                                "of the world)", capability="economic.io_price.coverage")
        if industry not in SHOCKABLE_INDUSTRIES:
            raise CapabilityGap(f"shock {i}: industry {industry!r} cannot be shocked (allowed: "
                                f"{sorted(SHOCKABLE_INDUSTRIES)})", capability="economic.io_price.shockable_industry")
        pct = _dec(s.get("price_change_percent"), f"shocks[{i}].price_change_percent")
        if not PERCENT_MIN_EXCLUSIVE < pct <= PERCENT_MAX:
            raise CapabilityGap(f"shock {i}: {pct}% is outside the registered envelope (-100, 200]",
                                capability="economic.io_price.shock_envelope")
        shocks.append(Shock(origin, industry, pct))
    if len({(s.origin, s.industry) for s in shocks}) != len(shocks):
        raise ValidationProblem("each (origin, industry) may be shocked once")
    assumptions = spec.get("assumptions") or {}
    grid_raw = assumptions.get("pass_through_grid")
    grid = tuple(_dec(t, "pass_through_grid[]") for t in grid_raw) if grid_raw is not None \
        else DEFAULT_PASS_THROUGH_GRID
    if not grid or any(not Decimal(0) < t <= Decimal(1) for t in grid):
        raise ValidationProblem("pass_through_grid values must lie in (0, 1]")
    grid = tuple(sorted(set(grid)))
    outputs = tuple(spec.get("outputs") or OUTPUT_GROUPS)
    bad = [o for o in outputs if o not in OUTPUT_GROUPS]
    if bad:
        raise CapabilityGap(f"unsupported outputs {bad} (supported: {list(OUTPUT_GROUPS)})",
                            capability="outputs.macroeconomic")
    return IOSpec(year, countries, tuple(shocks), grid, outputs)


# --------------------------------------------------------------------------- table -> arrays
@dataclass
class IOArrays:
    """Float64 view of one inter-country table (pure; built from the pinned Arrow table)."""

    economies: tuple[str, ...]  # table codes, e.g. "FR"
    keys: tuple[str, ...]  # REVA keys in the same order, e.g. "FRA"
    industries: tuple[str, ...]
    A: np.ndarray  # (N, N) technical coefficients
    x: np.ndarray  # (N,) gross output, million EUR
    households: np.ndarray  # (N, C) household final consumption by supplying industry and consuming economy
    reference_year: int | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.economies) * len(self.industries)

    def index(self, economy_pos: int, industry: str) -> int:
        return economy_pos * len(self.industries) + self.industries.index(industry)


def _codes(col: Any, index: Mapping[str, int]) -> np.ndarray:
    """Positions of a (dictionary or string) Arrow column in ``index`` (-1 = unknown)."""
    import pyarrow as pa
    import pyarrow.compute as pc

    if not pa.types.is_dictionary(col.type):
        col = pc.dictionary_encode(col)
    parts = []
    for ch in col.chunks:
        lut = np.array([index.get(str(v), -1) for v in ch.dictionary.to_pylist()], dtype=np.int16)
        idx = ch.indices.to_numpy(zero_copy_only=False)
        parts.append(lut[idx] if len(lut) else np.full(len(idx), -1, dtype=np.int16))
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)


def arrays_from_table(table: Any, coverage: Mapping[str, Any]) -> IOArrays:
    """Build A, x and the household weights from the long table (blocks Z, F, X).

    ``coverage`` is the dataset's coverage block (code lists, economy keys, reference year).
    """
    import pyarrow as pa
    import pyarrow.compute as pc

    lists = coverage.get("code_lists")
    keymap = coverage.get("economy_iso3")
    if not lists or not keymap:
        raise IntegrityFailure("dataset manifest lacks code lists / economy keys")
    economies = tuple(lists["economies"])
    industries = tuple(lists["industries"])
    keys = tuple(str(keymap[e]) for e in economies)
    unknown = [k for k in keys if k not in FIGARO_KEYS]
    if unknown:
        raise IntegrityFailure(f"dataset economies are not the reviewed FIGARO economies: {unknown}")
    e_idx = {e: i for i, e in enumerate(economies)}
    i_idx = {s: i for i, s in enumerate(industries)}
    n_i = len(industries)
    n = len(economies) * n_i
    block = _codes(table.column("block"), {"Z": 0, "F": 1, "V": 2, "X": 3})
    rc = _codes(table.column("row_country"), e_idx)
    rk = _codes(table.column("row_code"), i_idx)
    cc = _codes(table.column("col_country"), e_idx)
    ck = _codes(table.column("col_code"), {**i_idx, HOUSEHOLD_FINAL_DEMAND: -2})
    val = pc.cast(table.column("value"), pa.float64()).to_numpy()
    if np.isnan(val).any():
        raise IntegrityFailure("dataset contains missing values")
    row = rc.astype(np.int64) * n_i + rk
    zm = (block == 0)
    if (rc[zm] < 0).any() or (rk[zm] < 0).any() or (cc[zm] < 0).any() or (ck[zm] < 0).any():
        raise IntegrityFailure("intermediate block has codes outside the manifest code lists")
    z = np.zeros((n, n), dtype=np.float64)
    z[row[zm], cc[zm].astype(np.int64) * n_i + ck[zm]] = val[zm]
    if int(zm.sum()) != n * n:
        raise IntegrityFailure(f"intermediate block has {int(zm.sum())} cells, expected {n * n}")
    xm = (block == 3)
    x = np.full(n, np.nan)
    x[row[xm]] = val[xm]
    if np.isnan(x).any():
        raise IntegrityFailure("gross output is missing for some industries")
    hm = (block == 1) & (ck == -2)
    households = np.zeros((n, len(economies)), dtype=np.float64)
    households[row[hm], cc[hm]] = val[hm]
    if int(hm.sum()) != n * len(economies):
        raise IntegrityFailure("household final consumption block is incomplete")
    del block, rc, rk, cc, ck, val, row
    pos = x > 0
    if (x < 0).any():
        raise IntegrityFailure("negative gross output")
    if np.abs(z[~pos, :]).sum() != 0 or np.abs(households[~pos, :]).sum() != 0:
        raise IntegrityFailure("an industry without output supplies intermediate or household use")
    z[:, pos] /= x[pos]
    z[:, ~pos] = 0.0
    a = z
    diagnostics = {
        "industries": n, "zero_output_industries": int((~pos).sum()),
        "negative_coefficients": int((a < 0).sum()),
        "max_column_sum": float(a.sum(axis=0).max()),
        "negative_household_cells": int((households < 0).sum()),
    }
    return IOArrays(economies, keys, industries, a, x, households,
                    reference_year=coverage.get("reference_year"),
                    diagnostics=diagnostics)


def spectral_radius_upper_bound(a: np.ndarray, iterations: int = SPECTRAL_ITERATIONS) -> float:
    """Rigorous upper bound of ρ(|A|) ≥ ρ(A): Collatz-Wielandt ``max_i (|A|v)_i / v_i`` for v > 0.

    v is improved by damped power iteration (v ← |A|v + δ·1 keeps v strictly positive); the bound
    holds for every positive v, so stopping early only loosens it.
    """
    m = np.abs(a) if (a < 0).any() else a
    v = np.ones(m.shape[0])
    best = float("inf")
    for _ in range(iterations):
        w = m @ v
        best = min(best, float(np.max(w / v)))
        if best < 1.0 - 1e-6 and _ > 20:
            break
        v = w / np.max(w) + 1e-9 if np.max(w) > 0 else np.ones_like(v)
    return best


# --------------------------------------------------------------------------- solution
@dataclass(frozen=True)
class Solution:
    theta: Decimal
    dp: np.ndarray  # (N,) price change as a fraction; NaN for industries without output
    dp_by_origin: dict[str, np.ndarray]  # shock-origin key -> (N,) contribution
    residuals: dict[str, float]


def _shock_vector(arr: IOArrays, shocks: Sequence[Shock]) -> tuple[np.ndarray, np.ndarray, list[str]]:
    key_pos = {k: i for i, k in enumerate(arr.keys)}
    idx = np.array([arr.index(key_pos[s.origin], s.industry) for s in shocks], dtype=np.int64)
    if (arr.x[idx] <= 0).any():
        empty = [f"{s.origin}:{s.industry}" for s, i in zip(shocks, idx, strict=True) if arr.x[i] <= 0]
        raise CapabilityGap(f"shocked industries have no output in the table: {empty}",
                            capability="economic.io_price.shock_without_output")
    pi = np.array([s.fraction for s in shocks], dtype=np.float64)
    return idx, pi, [s.origin for s in shocks]


def solve(arr: IOArrays, shocks: Sequence[Shock], theta: Decimal) -> Solution:
    """Δp for one pass-through share, total and by shock origin, with residual checks."""
    from scipy.linalg import lu_factor, lu_solve

    s_idx, pi, origins = _shock_vector(arr, shocks)
    n = arr.n
    endo = np.setdiff1d(np.flatnonzero(arr.x > 0), s_idx)
    th = float(theta)
    groups = sorted(set(origins))
    rhs_shocks = np.zeros((len(s_idx), 1 + len(groups)))
    rhs_shocks[:, 0] = pi
    for g, key in enumerate(groups, start=1):
        rhs_shocks[:, g] = np.where(np.array(origins) == key, pi, 0.0)
    b = th * (arr.A[np.ix_(s_idx, endo)].T @ rhs_shocks)  # (|E|, 1+G)
    m = arr.A[np.ix_(endo, endo)].T * (-th)
    m[np.diag_indices_from(m)] += 1.0
    lu = lu_factor(m, check_finite=True)
    sol = lu_solve(lu, b, check_finite=True)
    del lu
    residuals: dict[str, float] = {}
    names = ["total", *groups]
    for c, name in enumerate(names):
        nb = float(np.linalg.norm(b[:, c]))
        r = float(np.linalg.norm(m @ sol[:, c] - b[:, c]))
        rel = r / nb if nb > 0 else (0.0 if float(np.linalg.norm(sol[:, c])) == 0.0 else float("inf"))
        residuals[name] = rel
        if not rel < RESIDUAL_TOLERANCE:
            raise IntegrityFailure(f"linear solve residual {rel:.3e} exceeds {RESIDUAL_TOLERANCE:g} ({name})",
                                   residual=rel, column=name)
    del m

    def full(col: int, exo: np.ndarray) -> np.ndarray:
        out = np.full(n, np.nan)
        out[endo] = sol[:, col]
        out[s_idx] = exo
        return out

    dp = full(0, pi)
    by_origin = {key: full(g, rhs_shocks[:, g]) for g, key in enumerate(groups, start=1)}
    return Solution(theta, dp, by_origin, residuals)


@dataclass(frozen=True)
class BasketResult:
    total: float
    domestic_products: float
    imported_products: float
    domestic_shocks: float
    foreign_shocks: float
    weight_total_mio_eur: float


def basket(arr: IOArrays, sol: Solution, economy_pos: int) -> BasketResult | None:
    """Household basket price change (fraction) of one economy and its decompositions.

    ``None`` when the table records no (positive) household consumption for the economy:
    the basket is then not applicable (published as a typed missing value, never zero).
    """
    h = arr.households[:, economy_pos]
    pos = arr.x > 0
    w_tot = float(h[pos].sum())
    if not w_tot > 0:
        return None
    dp = np.where(pos, sol.dp, 0.0)
    own = np.zeros(arr.n, dtype=bool)
    k = len(arr.industries)
    own[economy_pos * k:(economy_pos + 1) * k] = True
    dom = float(h[own] @ dp[own]) / w_tot
    imp = float(h[~own] @ dp[~own]) / w_tot
    total = dom + imp
    key = arr.keys[economy_pos]
    if key in sol.dp_by_origin:
        dom_shock = float(h @ np.where(pos, sol.dp_by_origin[key], 0.0)) / w_tot
    else:
        dom_shock = 0.0
    return BasketResult(total, dom, imp, dom_shock, total - dom_shock, w_tot)


# --------------------------------------------------------------------------- result rows
def _value(d: Decimal, notes: str) -> dict[str, Any]:
    return {"kind": "decimal", "value": dstr(d), "unit_ref": UNIT,
            "basis": {"price_basis": "nominal", "valuation": "basic_prices", "scale_factor": "1", "notes": notes}}


def _missing() -> dict[str, Any]:
    return {"kind": "missing", "reason": "not_applicable"}


def _unknown_uncertainty() -> dict[str, Any]:
    return UncertaintySpec.unknown(
        "Unquantified: the input-output table carries no published uncertainty and no error model is introduced; "
        "the value is conditional on the stated assumptions (" + LABEL + ")"
    ).model_dump(mode="json")


def _range_uncertainty(lo: Decimal, hi: Decimal, grid: Sequence[str]) -> dict[str, Any]:
    return UncertaintySpec(
        status=UncertaintyStatus.quantified, kind=UncertaintyKind.sensitivity_range,
        description=("Pass-through sensitivity range (" + SENSITIVITY_STATEMENT + "): min/max of this output over "
                     "the labelled pass-through grid together with full pass-through θ = 1. Not a confidence, "
                     "credible or prediction interval; no coverage level; says nothing about the table's own "
                     "measurement uncertainty, which is unquantified."),
        method_version=SENSITIVITY_METHOD, lower=dstr(lo), upper=dstr(hi),
        conditioning=(*(f"pass_through={t}" for t in grid), "full_pass_through:pass_through=1 (the L1 value) is "
                      "always included"),
    ).model_dump(mode="json")


@dataclass
class CompiledIO:
    spec: IOSpec
    arrays: IOArrays
    dataset: dict[str, Any]
    release: dict[str, Any]
    missing: list[dict[str, Any]] = field(default_factory=list)
    incompatible: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _Row:
    output_key: str
    country: str
    variant: str
    value: dict[str, Any]
    uncertainty: dict[str, Any]
    applicability: dict[str, Any]


def _variant(theta: Decimal) -> str:
    return f"L2:pass_through={dstr(theta.normalize()) if theta != 1 else '1'}"


def _rows_for(compiled: CompiledIO, fidelity: str) -> list[_Row]:
    s, arr = compiled.spec, compiled.arrays
    thetas: list[Decimal] = [Decimal(1)]
    if fidelity == "L2":
        thetas += [t for t in s.pass_through_grid if t != 1]
    sols = {t: solve(arr, s.shocks, t) for t in thetas}
    same_sign = all(sh.percent > 0 for sh in s.shocks) or all(sh.percent < 0 for sh in s.shocks)
    shocked = {(sh.origin, sh.industry) for sh in s.shocks}
    key_pos = {k: i for i, k in enumerate(arr.keys)}
    base_app = {"status": "applicable", "modality": "assumption_conditioned_scenario",
                "model": f"{MODEL_KEY}@{MODEL_VERSION}", "label": LABEL, "reference_year": s.reference_year,
                "dataset_id": compiled.dataset.get("dataset_id"), "not_claimed": NOT_CLAIMED}
    variants: list[tuple[str, Decimal]] = [("L1", Decimal(1))]
    if fidelity == "L2":
        variants += [(_variant(t), t) for t in s.pass_through_grid]
    rows: list[_Row] = []

    def emit(key: str, country: str, values: dict[Decimal, float | None], extra: dict[str, Any], notes: str) -> None:
        for name, t in variants:
            v = values[t]
            app = {**base_app, "country": country, "variant": name, "pass_through": dstr(t), **extra}
            if name != "L1":
                app["label"] = L2_LABEL
            rows.append(_Row(key, country, name, _missing() if v is None else _value(to_result_decimal(v), notes),
                             _unknown_uncertainty(), app))
        if fidelity == "L2" and all(values[t] is not None for t in values):
            pts = [to_result_decimal(values[t]) for t in values]  # type: ignore[arg-type]
            lo, hi = min(pts), max(pts)
            grid = s.grid_strings()
            one_sided = ("one-sided by construction: every shock has the same sign and A ≥ 0, so the magnitude "
                         "rises with θ and full pass-through (the L1 value) is the largest-magnitude end"
                         if same_sign and arr.diagnostics.get("negative_coefficients") == 0 else
                         "range over the evaluated points only; no monotonicity in θ is claimed (mixed-sign shocks)")
            rows.append(_Row(key, country, SENSITIVITY_VARIANT,
                             {"kind": "sensitivity_range", "lower": dstr(lo), "upper": dstr(hi), "unit_ref": UNIT,
                              "basis": {"valuation": "basic_prices", "notes": notes},
                              "evaluated_points": len(values), "includes_full_pass_through_point": True,
                              "statement": SENSITIVITY_STATEMENT, "one_sidedness": one_sided},
                             _range_uncertainty(lo, hi, grid),
                             {**base_app, "label": L2_LABEL, "country": country, "variant": SENSITIVITY_VARIANT,
                              "range_over": "pass-through grid ∪ {θ = 1}", "pass_through_grid": grid,
                              "one_sidedness": one_sided, **extra}))

    all_t = sorted(set(thetas))
    for country in s.countries:
        e = key_pos[country]
        if "industry_price_change_percent" in s.outputs:
            for ind in arr.industries:
                k = arr.index(e, ind)
                vals: dict[Decimal, float | None] = {}
                for t in all_t:
                    v = sols[t].dp[k]
                    vals[t] = None if np.isnan(v) else 100.0 * float(v)
                extra: dict[str, Any] = {"industry": ind}
                if (country, ind) in shocked:
                    extra["exogenous"] = "price change set by the intervention (not passed through)"
                if arr.x[k] <= 0:
                    extra["note"] = "industry has no output in the table; price change not applicable"
                emit(INDUSTRY_PREFIX + ind, country, vals, extra,
                     "output price change of this industry, percent of the baseline price")
        if BASKET_KEY in s.outputs or "household_basket_contributions" in s.outputs:
            res = {t: basket(arr, sols[t], e) for t in all_t}
            b1 = res[Decimal(1)]
            note = {} if b1 is not None else {
                "note": "no household final consumption recorded for this economy; basket not applicable"}
            if BASKET_KEY in s.outputs:
                emit(BASKET_KEY, country, {t: None if r is None else 100.0 * r.total for t, r in res.items()},
                     {"basket": "households' final consumption (P3_S14) at basic prices, domestic and imported",
                      "weight_total_mio_eur": None if b1 is None else f"{b1.weight_total_mio_eur:.3f}", **note},
                     "price change of the household basket at basic prices, percent")
            if "household_basket_contributions" in s.outputs:
                parts = {CONTRIBUTION_KEYS[0]: "domestic_products", CONTRIBUTION_KEYS[1]: "imported_products",
                         CONTRIBUTION_KEYS[2]: "domestic_shocks", CONTRIBUTION_KEYS[3]: "foreign_shocks"}
                for out_key, attr in parts.items():
                    emit(out_key, country,
                         {t: None if r is None else 100.0 * getattr(r, attr) for t, r in res.items()},
                         {"decomposition": attr, **note},
                         "contribution to the household basket price change, percent of the baseline basket "
                         "value; domestic+imported products and domestic+foreign shocks each sum to the basket "
                         "change before rounding")
    for t, sol in sols.items():
        compiled.arrays.diagnostics.setdefault("max_relative_residual", {})[dstr(t)] = max(sol.residuals.values())
        compiled.arrays.diagnostics.setdefault("right_hand_sides", {})[dstr(t)] = len(sol.residuals)
    return rows


# --------------------------------------------------------------------------- plugin
class IOPriceModel:
    """``economic.io_price@1.0.0`` as a :class:`reva.simulation.plugins.SimulationModel`."""

    model_key = MODEL_KEY
    model_version = MODEL_VERSION
    fidelities = frozenset({"L1", "L2"})
    missing_input_policies = frozenset({"fail"})

    def validate(self, spec: dict[str, Any]) -> None:
        parse_spec(spec)
        if spec.get("missing_input_policy", "fail") != "fail":
            raise CapabilityGap("economic.io_price supports missing_input_policy=fail only (one global table)",
                                capability="scenario.skip_missing_countries")

    def requirements(self, spec: dict[str, Any]) -> list[Any]:
        from reva.simulation.plugins import InputRequirement

        s = parse_spec(spec)
        return [InputRequirement(DATASET_INPUT, DATASET_CONCEPT, DATASET_SUBJECT, (s.reference_year,), "dataset")]

    def compile(self, inputs: Any, spec: dict[str, Any]) -> CompiledIO:
        s = parse_spec(spec)
        ds = inputs.dataset(DATASET_INPUT, DATASET_SUBJECT)
        empty = IOArrays((), (), (), np.zeros((0, 0)), np.zeros(0), np.zeros((0, 0)))
        if ds.is_missing:
            return CompiledIO(s, empty, {}, dict(inputs.release), missing=inputs.missing_problems())
        manifest = dict(ds.manifest)
        cov = manifest.get("coverage") or {}
        year = cov.get("reference_year")
        if year != s.reference_year:
            return CompiledIO(s, empty, {}, dict(inputs.release), incompatible=[{
                "code": "incompatible_semantics", "input": DATASET_INPUT,
                "message": f"the pinned IO table is for {year}, the scenario asks for {s.reference_year}",
                "table_year": year, "reference_year": s.reference_year}])
        if not (cov.get("identity_report") or {}).get("passed"):
            raise IntegrityFailure("the pinned IO table has no passing identity report")
        arr = arrays_from_table(ds.table, cov)
        rho = spectral_radius_upper_bound(arr.A)
        arr.diagnostics["spectral_radius_upper_bound"] = rho
        if not rho < 1.0:
            raise CapabilityGap(f"the table's Leontief system is not shown to be productive (ρ bound {rho:.6f})",
                                capability="economic.io_price.productivity")
        dataset = {"dataset_id": ds.dataset_id, "artifact_sha256": ds.artifact_sha256,
                   "manifest_sha256": manifest.get("manifest_sha256"), "dataset_key": manifest.get("dataset_key"),
                   "source": cov.get("source"), "reference_year": year}
        return CompiledIO(s, arr, dataset, dict(inputs.release))

    def compute(self, compiled: CompiledIO, fidelity: str) -> list[Any]:
        from reva.simulation.plugins import ResultRow

        if fidelity not in self.fidelities:
            raise CapabilityGap(f"economic.io_price computes L1 and L2, not {fidelity}",
                                capability=f"fidelity.{fidelity}")
        return [ResultRow(r.output_key, r.country, entity_id(r.country), r.variant, r.value, r.uncertainty,
                          r.applicability) for r in _rows_for(compiled, fidelity)]

    def manifest(self) -> dict[str, Any]:
        return {"implementation": "reva.models.economic.io_price", "implementation_digest": implementation_digest(),
                "review_status": REVIEW_STATUS, "numerics": numerics_statement(), "label": LABEL,
                "assumptions": list(ASSUMPTIONS), "not_claimed": NOT_CLAIMED,
                "shockable_industries": SHOCKABLE_INDUSTRIES}

    # -- optional hooks ------------------------------------------------------------------
    def spec_schema(self) -> dict[str, Any]:
        return spec_schema()

    def explanation(self, fidelity: str) -> dict[str, Any]:
        return explain(fidelity)

    def run_applicability(self, compiled: CompiledIO, fidelity: str) -> dict[str, Any]:
        return {"explanation": explain(fidelity), "label": LABEL if fidelity != "L2" else L2_LABEL,
                "intervention": [sh.as_json() for sh in compiled.spec.shocks],
                "shocked_industry_contents": {sh.industry: SHOCKABLE_INDUSTRIES[sh.industry]
                                              for sh in compiled.spec.shocks},
                "pass_through_grid": compiled.spec.grid_strings() if fidelity == "L2" else ["1"],
                "dataset": compiled.dataset, "diagnostics": compiled.arrays.diagnostics,
                "countries": list(compiled.spec.countries)}

    def seed_subjects(self, spec: dict[str, Any]) -> list[str]:
        return [str(c) for c in spec.get("countries", [])]

    def cost(self, spec: dict[str, Any]) -> dict[str, Any]:
        return {"pass_through_points": len((spec.get("assumptions") or {}).get("pass_through_grid")
                                           or DEFAULT_PASS_THROUGH_GRID)}

    def implementation_files(self) -> tuple[Path, ...]:
        return (Path(__file__),)


def numerics_statement() -> str:
    return (f"float64 LU with partial pivoting (SciPy/LAPACK); relative residual < {RESIDUAL_TOLERANCE:g} checked "
            f"per right-hand side; Collatz-Wielandt bound of ρ(|A|) < 1 checked; results rounded half-even to "
            f"{RESULT_DECIMALS} decimals of a percent")


def explain(fidelity: str = "L1") -> dict[str, Any]:
    return {
        "model": f"{MODEL_KEY}@{MODEL_VERSION}",
        "label": LABEL if fidelity != "L2" else L2_LABEL,
        "modality": "assumption_conditioned_scenario",
        "formula": "(I − θ A_EEᵀ) Δp_E = θ A_SEᵀ π_S; Δp_S = π_S; basket_d = Σ_k h_kd Δp_k / Σ_k h_kd",
        "units": {"industry_price_change_percent:<industry>": "percent of the baseline output price",
                  BASKET_KEY: "percent of the baseline household basket value (basic prices)",
                  "household_basket_contribution_*": "percent of the baseline basket value (sums to the basket)"},
        "assumptions": list(ASSUMPTIONS),
        "uncertainty": ("Unquantified: FIGARO publishes no uncertainty. L2 publishes a separate pass-through "
                        "sensitivity range row (min/max over the grid ∪ {θ = 1}); it has no probability level."),
        "numerical": numerics_statement(),
        "shockable_industries": SHOCKABLE_INDUSTRIES,
        "not_claimed": NOT_CLAIMED,
        "review_status": REVIEW_STATUS,
    }


def spec_schema() -> dict[str, Any]:
    num = {"oneOf": [{"type": "number"}, {"type": "string", "pattern": r"^-?[0-9]+(\.[0-9]+)?$"}]}
    return {
        "countries": {"type": "array", "minItems": 1, "maxItems": 49, "uniqueItems": True,
                      "items": {"enum": [k for k in FIGARO_KEYS if k != REST_OF_WORLD_KEY]}},
        "reference_year": {"type": "integer", "minimum": 2010, "maximum": 2100},
        "horizon": {"type": "object", "additionalProperties": False,
                    "properties": {"periods": {"const": 1}, "unit": {"const": "annual_accounting_period"}},
                    "required": ["periods", "unit"]},
        "missing_input_policy": {"const": "fail"},
        "intervention": {
            "type": "object", "additionalProperties": False, "required": ["kind", "shocks"],
            "properties": {
                "kind": {"const": "set_exogenous_industry_price_change"},
                "shocks": {"type": "array", "minItems": 1, "maxItems": MAX_SHOCKS, "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["origin", "industry", "price_change_percent"],
                    "properties": {"origin": {"enum": list(FIGARO_KEYS)},
                                   "industry": {"enum": sorted(SHOCKABLE_INDUSTRIES)},
                                   "price_change_percent": num}}},
            },
        },
        "assumptions": {
            "type": "object", "additionalProperties": False,
            "properties": {"pass_through_grid": {"type": "array", "minItems": 1, "maxItems": 10, "items": num}},
        },
        "outputs": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"enum": list(OUTPUT_GROUPS)}},
    }


_DIGEST: list[str] = []


def implementation_digest() -> str:
    if not _DIGEST:
        from reva.simulation.plugins import source_digest

        _DIGEST.append(source_digest(Path(__file__)))
    return _DIGEST[0]


#: Registry entry for ``registry/models/methods.yaml`` (seeded as a candidate, review pending).
REGISTRY_ENTRY: dict[str, Any] = {
    "model_key": MODEL_KEY,
    "version": MODEL_VERSION,
    "kind": "scenario",
    "approval_status": "candidate",
    "description": ("Multi-region Leontief cost-push price model on the Eurostat FIGARO inter-country table: "
                    "exogenous output-price change of chosen industries, uniform pass-through share, fixed "
                    "coefficients. " + LABEL + "."),
    "input_ports": [{"name": DATASET_INPUT, "kind": "dataset", "concept": DATASET_CONCEPT,
                     "subject": DATASET_SUBJECT}],
    "output_ports": [{"name": g, "unit": UNIT} for g in OUTPUT_GROUPS],
    "applicability": {"horizon": SUPPORTED_HORIZON, "price_change_percent_envelope": {"min_exclusive": -100,
                                                                                       "max": 200},
                      "pass_through_envelope": {"min_exclusive": 0, "max": 1},
                      "shockable_industries": sorted(SHOCKABLE_INDUSTRIES),
                      "fidelity_supported": ["L0", "L1", "L2"], "fidelity_reserved": ["L3", "L4"],
                      "unsupported_outputs": ["inflation_forecast", "consumer_price_index", "gdp_loss", "welfare"]},
    "assumptions": list(ASSUMPTIONS),
    "review_status": REVIEW_STATUS,
}


def registry_entry_digest() -> str:
    return logical_hash(REGISTRY_ENTRY)


def _register() -> Any:
    from reva.simulation import plugins

    return plugins.register(MODEL)


MODEL = IOPriceModel()
_register()
