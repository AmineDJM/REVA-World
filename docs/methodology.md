# Methodology (V1)

This page documents the methods V1 actually runs, their formulas and their
limits. Every method is a registered, versioned model (`registry/models/*.yaml`,
`model.version`) and outputs carry the method key/version. None of these methods
produces a causal or welfare claim.

## 1. Belief publication: `belief.source_select` 1.0.0 (`source_select_v1`)

Implementation: `src/reva/belief/selection.py`, `src/reva/belief/publication.py`.
Policy: `registry/policies/selection.yaml` (`selection-1.0.0`).

1. **Filter** to observations recorded at or before the cutoff, of the release's
   origin class (`live` or `test_fixture`, never mixed), not retracted before the
   cutoff, and eligible for that concept/subject under a versioned eligibility
   approval (`evidence.is_eligible`). Filtering happens **before** revision
   selection, so a later correction can never change an earlier as-of answer.
2. **Select the revision**: the highest `revision_no` within each
   `(source, logical_key)` lineage that survived the filter.
3. **Apply the published preference** for the exact estimand (e.g. WDI for
   population and GDP; Comtrade public preview for HS 2709 totals).
4. **Publish `selected_report`** with the source's own uncertainty status
   (typically `unquantified`); no uncertainty is invented.
5. **Keep alternatives and discrepancy flags**; incomparable definitions are
   never averaged.

A release is immutable: the Parquet estimate table and manifests are written to
the object store first, then the release, estimates, inputs, artifact references
and a monotonically increasing publication sequence commit in one transaction
under the publication advisory lock. As-of reads (`release_as_of`) return the
release REVA actually had published at that time (operational_as_known).

**Limits.** A selected official value is not a calibrated posterior about
reality. Source-family dependence is recorded but not modelled in V1.

## 2. Crude-oil border-value exposure: `economic.crude_exposure` 1.0.0

Implementation: `src/reva/models/economic/crude_exposure.py` (pure; `decimal`,
50-digit context). Scenario contract: `contracts/scenario.schema.json`.

Inputs for one country and one year: M and X, the same-year source-reported HS
2709 import and export border values in current USD (imports usually CIF, exports
FOB; the basis stays explicit), and G, nominal GDP in current USD for the same
year. All three must come from the pinned baseline release.

**L1 (fixed quantities)**, multipliers `p_M, p_X > 0`:

    ΔM = M × (p_M − 1)
    ΔX = X × (p_X − 1)
    Δnet_external_cost = ΔM − ΔX
    exposure_percent_of_baseline_GDP = 100 × Δnet_external_cost / G

Reference case: M = 100, X = 40, G = 1000, p_M = p_X = 1.25 → ΔM = 25, ΔX = 10,
Δnet = 15, exposure = 1.5 %. All multipliers 1 → 0. A negative Δnet is net
receipt exposure under the stated assumptions.

**L2 (labelled elasticity sensitivity)** with assumed quantity elasticities ε:

    M' = M × p_M^(1 + ε_M)      X' = X × p_X^(1 + ε_X)

ε = 0 executes exactly the L1 expression (bit-identical, not approximately). The
grid of results is a **sensitivity range**, not a probability interval.

**L0** is an exact cache of a previously computed identical specification;
**L3/L4** return `capability_not_available` (no justified parameter
distributions; no competing models).

**Applicability.** One annual accounting period; countries with complete inputs
for the chosen year only. Different reference years or valuation bases for the
same run are rejected. The default live scenario uses FRA, CHN and USA; an
all-four request returns `missing_input` for DZA (no 2023 Comtrade preview rows).
Five-year GDP, welfare, inflation, fiscal or balance-of-payments questions are
unsupported. Kilian (2009) is the reason one universal oil-price multiplier is
not attached to the result.

## 3. Forecasting tournament

Models (`src/reva/models/forecasting/`), strictly positive annual series only:

| Model | Point forecast | Uncertainty |
|---|---|---|
| `forecast.last_value` 1.0.0 (naive baseline, always present) | last training value | `unquantified` |
| `forecast.log_drift` 1.0.0 | `exp(z_n + h·c)`, `c` = mean log difference | Gaussian log-scale prediction interval with `σ²·h·(1 + h/T)` |
| `forecast.local_level_log` 1.0.0 | Kalman predictive median of the log local-level model (statsmodels, deterministic MLE) | state-space **prediction** interval for the future released statistic |

Values ≤ 0 make the log models ineligible (never clipped). The estimand is the
future *released* statistic, not latent reality.

Tournament (`src/reva/models/tournament.py`, service in
`src/reva/evaluation/tournament_service.py`):

* rolling expanding-window origins; each fit sees only a cutoff-scoped view of the
  series (`reva.evaluation.splits`);
* scores: absolute error, MASE (scale from the training window; undefined when
  the scale is zero), interval coverage for prediction intervals only, CRPS/log
  score where a distribution exists;
* promotion (policy `promotion-1.0.0`): ≥ 30 paired origin-series cases, a
  moving-block bootstrap (block 3, 1000 replicates, 95 %) interval of the mean
  MASE difference entirely below zero, and no series/horizon subgroup degraded by
  more than 10 %. Otherwise `keep_incumbent` with reasons, reported as
  "insufficient evaluation" where applicable;
* a locked final holdout cannot be reused by the challenger; the replay mode
  (`operational_as_known`, `available_then_reconstruction`,
  `revised_data_retrospective`) is carried into every result and the
  retrospective mode is labelled as not a real-time backtest.

## 4. Population recipe and compression (France, age × recorded sex)

Implementation: `src/reva/population/` and `src/reva/compression/`.

* **Recipe** (`reva-population-recipe-1`): one geography/date of Eurostat
  `demo_pjan` (population on 1 January). Only the disjoint leaf partition
  {M, F} × {single-year ages, `Y_OPEN`, `UNK`} is counted; totals and subtotals are
  used only for consistency diagnostics and never alter source counts. Recorded
  sex keeps the source definition (not gender identity). Observed zeros are
  structural zeros; missing cells stay missing.
* **Materialisation**: ≤ 100,000 weighted representative records in Parquet. Every
  nonzero cell receives ≥ 1 record; the rest are allocated by largest remainder
  with exact integer arithmetic. Each record carries the exact rational weight
  `count_c / k_c`, so per-cell weight sums equal the source counts exactly.
* **Calibration diagnostics**: max absolute/relative calibration error, effective
  sample size, weight concentration, structural-zero violations; IPF reports
  infeasibility instead of altering targets.
* **Compression**: coarsening into age bands and refinement back to the recipe's
  own leaves conserve total weight exactly; refinement along any dimension the
  recipe does not contain (income, household, health …) raises
  `UnsupportedRefinement`.

**Limits.** The recipe supports only age × recorded-sex questions. It cannot
support childcare, income or household simulations, and dividing a national
import-bill change by synthetic people is not a distributional welfare result.
The January population concept is distinct from WDI mid-year population and the
two are never reconciled as the same quantity.

## 5. Value of information (demonstration only)

`src/reva/evaluation/voi.py`: exact and nested Monte Carlo EVSI on a finite
analytic decision problem. Without a declared action set and utility, the system
reports a unitless uncertainty-reduction proxy, never a monetary value.
