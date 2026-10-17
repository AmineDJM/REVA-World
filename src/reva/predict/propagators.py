"""Existing REVA simulators as propagation plugins of the decision engine (V5, agent PRED-ENGINE).

Each plugin consumes one engine variable (identifier match only, ``registry/predict/variables.yaml``) and returns
*terminal* outputs: they are model outputs under the model's own stated assumptions (labels repeated on every
value), not variables that effect estimates may chain on (an IO household basket at basic prices is not a CPI;
a border-value exposure is not a GDP loss).

* ``economic.io_price@1.0.0`` — FIGARO multi-region Leontief cost push. The model is linear in the shock for a
  fixed pass-through share, so the unit response (B price +1 % in every economy, θ = 1) is solved once and each
  Monte-Carlo draw of the oil-price change is multiplied through (exact, not an approximation).
* ``economic.crude_exposure@1.0.0`` — L1 border-value accounting, exactly linear in (p − 1): unit response from
  ``exposure_l1`` (Decimal) times each draw.
* ``social.retirement_age`` — the registered plugin run once on the pinned release (deterministic: the decision
  states the age; the literature response grid is the model's own L2 business, not sampled here).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import numpy as np

from reva.predict.decision import VarRef

IO_KEY = "economic.io_price"
CRUDE_KEY = "economic.crude_exposure"
RETIREMENT_KEY = "social.retirement_age"


@dataclass
class PluginOutput:
    variable: VarRef
    geo: str
    unit: str
    label_fr: str
    frequency: str  # "undated" | "year"
    undated: np.ndarray | None = None  # (n,)
    yearly: dict[int, np.ndarray] = field(default_factory=dict)  # year -> (n,)
    notes: list[str] = field(default_factory=list)


@dataclass
class PluginResult:
    model: str
    outputs: list[PluginOutput] = field(default_factory=list)
    gaps: list[dict[str, Any]] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    labels: list[str] = field(default_factory=list)


def run_io_price(arrays: Any, x: np.ndarray, countries: list[str], provenance: dict[str, Any]) -> PluginResult:
    """Household basket (basic prices) change per country of interest for an oil-price change of ``x`` % per draw."""
    from reva.models.economic import io_price as io

    res = PluginResult(model=f"{io.MODEL_KEY}@{io.MODEL_VERSION}", provenance=dict(provenance), labels=[io.LABEL])
    keys = list(arrays.keys)
    shocks = []
    for pos, key in enumerate(keys):
        i = arrays.index(pos, "B")
        if arrays.x[i] > 0:
            shocks.append(io.Shock(key, "B", Decimal(1)))
    if not shocks:
        res.gaps.append({"code": "io_no_shockable_industry", "message": "no economy of the table has output in B"})
        return res
    sol = io.solve(arrays, shocks, Decimal(1))
    res.provenance.update(
        {
            "theta": "1",
            "shocked": "B (Mining and quarrying) of every economy with output, +1 % unit response",
            "max_relative_residual": max(sol.residuals.values()),
            "reference_year": arrays.reference_year,
        }
    )
    for c in countries:
        if c not in keys:
            res.gaps.append(
                {"code": "io_country_not_in_table", "geo": c, "message": f"{c} is not an economy of the IO table"}
            )
            continue
        b = io.basket(arrays, sol, keys.index(c))
        if b is None:
            res.gaps.append({"code": "io_no_household_basket", "geo": c, "message": "no household consumption"})
            continue
        unit_resp = b.total * 100.0  # percent basket change per 1 % change of B prices
        res.outputs.append(
            PluginOutput(
                variable=VarRef("plugin_output", f"{io.MODEL_KEY}.household_basket_price_change_percent"),
                geo=c,
                unit="percent",
                label_fr="Prix du panier des ménages (prix de base), poussée des coûts au premier tour — pas un IPC",
                frequency="undated",
                undated=unit_resp * x,
                notes=[
                    f"unit response: {unit_resp:.6f} % per 1 % change of B output prices (θ = 1)",
                    "first-round cost push at basic prices (no product taxes, no wage or margin response); "
                    "not a consumer price index and not an inflation forecast",
                ],
            )
        )
    return res


def _dec(v: Any) -> Decimal | None:
    if v is None:
        return None
    try:
        d = Decimal(str(v))
    except Exception:  # noqa: BLE001 - any unparsable value is missing, never zero
        return None
    return d if d.is_finite() else None


def run_crude_exposure(
    inputs: dict[str, dict[str, Any]], x: np.ndarray, countries: list[str], provenance: dict[str, Any]
) -> PluginResult:
    """Net crude border-value cost change in % of baseline GDP per country for a crude price change of ``x`` %."""
    from reva.models.economic import crude_exposure as ce

    res = PluginResult(model=f"{ce.MODEL_KEY}@{ce.MODEL_VERSION}", provenance=dict(provenance), labels=[ce.NOT_CLAIMED])
    for c in countries:
        row = inputs.get(c) or {}
        m, xv, g = _dec(row.get("M")), _dec(row.get("X")), _dec(row.get("G"))
        missing = [k for k, v in (("M", m), ("X", xv), ("G", g)) if v is None]
        if missing:
            res.gaps.append(
                {
                    "code": "missing_input",
                    "geo": c,
                    "inputs": [f"{k} ({ce.INPUT_CONCEPTS[k]})" for k in missing],
                    "message": "crude border values or GDP absent from the pinned release (never zero-filled)",
                }
            )
            continue
        unit = ce.exposure_l1(m, xv, g, Decimal("1.01"), Decimal("1.01"))
        u = float(unit.exposure_percent_baseline_gdp)  # % of GDP per 1 % price change (exactly linear)
        res.outputs.append(
            PluginOutput(
                variable=VarRef("plugin_output", f"{ce.MODEL_KEY}.exposure_percent_baseline_gdp"),
                geo=c,
                unit="pp_gdp",
                label_fr="Coût extérieur net du brut (valeur frontière), en % du PIB de référence — pas une perte de PIB",
                frequency="undated",
                undated=u * x,
                notes=[
                    f"reference year {row.get('year')}; M={m} X={xv} G={g} (current USD)",
                    f"unit response: {u:.6f} % of GDP per 1 % crude price change (fixed quantities, L1)",
                ],
            )
        )
        res.provenance.setdefault("inputs", {})[c] = row.get("provenance")
    return res


RETIREMENT_OUTPUTS = {
    "employment_change_55_69": ("level:persons", "Emploi des 55-69 ans : scénario moins référence (personnes)"),
    "pension_recipients_change": ("level:persons", "Bénéficiaires d'une pension de vieillesse : écart (personnes)"),
    "pension_expenditure_change_approx_eur": (
        "level:euro",
        "Dépense de pension : écart approximatif (euros nominaux de l'année de la moyenne, non indexés)",
    ),
}


def run_retirement(runner: Any, iso3: str, target_months: int, options: dict[str, Any], n: int) -> PluginResult:
    """Run the registered retirement-age plugin once (deterministic) and keep its central (L1) yearly outputs."""
    spec = {
        "model_key": RETIREMENT_KEY,
        "model_version": str(options.get("model_version", "1.0.0")),
        "countries": [iso3],
        "reference_year": int(options.get("reference_year", 2024)),
        "horizon": {"periods": int(options.get("periods", 8)), "unit": "calendar_year"},
        "intervention": {
            "kind": "retirement_age_target",
            "reference_path": str(options.get("reference_path", "law_in_force_2026")),
            "target_age_months": int(target_months),
            "first_birth_year": int(options["first_birth_year"]),
            "phase_in_months_per_cohort": int(options.get("phase_in_months_per_cohort", 0)),
        },
        "assumptions": dict(options.get("assumptions") or {}),
        "fidelity": "L1",
        "missing_input_policy": "fail",
        "outputs": sorted(RETIREMENT_OUTPUTS),
        "seed": 0,
    }
    res = PluginResult(model=f"{RETIREMENT_KEY}@{spec['model_version']}", provenance={"spec": spec})
    out = runner(spec)
    res.provenance.update(out.get("provenance") or {})
    res.labels = list(out.get("labels") or [])
    if out.get("status") != "succeeded":
        res.gaps.append(
            {
                "code": str(out.get("status") or "unsupported"),
                "geo": iso3,
                "message": "the retirement model did not run on the pinned release",
                "problems": list(out.get("problems") or [])[:20],
            }
        )
        return res
    series: dict[str, dict[int, float]] = {}
    for r in out.get("rows") or []:
        key = r.get("output_key")
        if key not in RETIREMENT_OUTPUTS or r.get("variant") != "L1":
            continue
        v = (r.get("value") or {}).get("value")
        d = _dec(v)
        year = r.get("year")
        if d is None or year is None:
            continue
        series.setdefault(str(key), {})[int(year)] = float(d)
    for key, by_year in sorted(series.items()):
        unit, label = RETIREMENT_OUTPUTS[key]
        res.outputs.append(
            PluginOutput(
                variable=VarRef("plugin_output", f"{RETIREMENT_KEY}.{key}"),
                geo=iso3,
                unit=unit,
                label_fr=label,
                frequency="year",
                yearly={y: np.full(n, val) for y, val in sorted(by_year.items())},
                notes=["deterministic model output (no estimate uncertainty sampled); results are 'qualified'"],
            )
        )
    return res
