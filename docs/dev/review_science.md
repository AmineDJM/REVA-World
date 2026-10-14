# Scientific-methods review (agent R3)

Reviewer role: statistician / econometrician / demographer. Read-only review: no code was changed.
Scope: crude exposure and simulation sensitivity labelling, forecasting models, metrics, tournament,
evaluation (splits, scoring, VOI, replay, leakage, cutoff), population and compression, `source_select_v1`,
the uncertainty kernel, concept definitions and the Kilian (2009) evidence card.

Every numerical claim below was checked with a throwaway script in
`/tmp/claude-0/-home-user-REVA-World/a82f628c-2790-54df-b0b4-28930bdc7a97/scratchpad/review3/`
(`s1_exposure.py` … `s12_crps.py`). They are pure Python, run with `uv run python <script>`, and use no database.

References used: Hyndman & Koehler (2006) on MASE; Hyndman & Athanasopoulos, *FPP* (variance of the drift method);
Künsch (1989) and Lahiri (2003) on the moving-block bootstrap; Politis & White (2004) on block length;
Diebold & Mariano (1995); Gneiting & Raftery (2007) on proper scores and the interval score;
Raiffa & Schlaifer (1961) on preposterior analysis and EVSI; Durbin & Koopman (2012) on the local level model;
Kish (1965) on effective sample size; Deming & Stephan (1940) and Bishop, Fienberg & Holland on IPF;
and Eurostat `demo_pjan` and WDI metadata (the captured fixtures).

## Severity summary

| id | sev | area | one-line |
|---|---|---|---|
| R3-01 | **High** | population/recipe | Age bands that are the *only* coverage of an age range (e.g. `Y_GE85` with no `Y_OPEN`) are dropped as "subtotals". A source that publishes its top ages only as such a band then loses 37% of its population (in the R3-01 test case) and is still materialised. |
| R3-02 | **High** | tournament | A moving-block bootstrap with block length 3 over cases ordered (series, origin, horizon) is anti-conservative. The false-promotion rate is 11–15% against a nominal 2.5%. |
| R3-03 | **High** | simulation engine | An L2 run gives the L1 point a "quantified sensitivity_range" as its uncertainty. That range can exclude the L1 point itself, and it replaces the "unquantified" status. |
| R3-04 | Medium | forecasting/drift | The prediction interval uses a normal quantile with a plug-in σ from as few as 2 differences. A nominal 80% interval covers 58% at n=3 and 72% at n=6. A constant-growth series gets a zero-width "quantified" interval. |
| R3-05 | Medium | forecasting/local_level | The plug-in MLE interval under-covers (about 0.66–0.70 at n=6–10, nominal 0.80). Spec §9.2 asks for calibration against later releases, and none is done. |
| R3-06 | Medium | tournament + tournament_service | Model-level MASE is computed over *different case sets* for each model. The ranking flips (shown in s7), and the service picks the winner on these unpaired numbers. |
| R3-07 | Medium | tournament | The subgroup gate compares noisy point estimates. A truly better model's chance of promotion *falls* as the panel grows (0.63 at 8 series, 0.15 at 32). |
| R3-08 | Medium | belief/selection | Lineages flagged `incompatible_lineages:flowCode` still publish a `selected_report`, chosen by the lexicographic order of `logical_key`. |
| R3-09 | Medium | population/recipe | The lower bound of `Y_OPEN` is inferred from age *codes* even when their counts are missing. It comes out as 100 when the true bound is 85. |
| R3-10 | Medium | evaluation/cutoff | `available_then` excludes values that were retracted later or became ineligible later. That is hindsight leakage: it flatters reconstructions and backtests. |
| R3-11 | Med-Low | tournament | No proper probabilistic score is computed (CRPS, log score or interval score), despite spec §12.2. Coverage and width alone can be gamed. |
| R3-12 | Low | population | Observed zero counts are labelled `structural_zeros`. Spec §15.1 distinguishes zero observations from impossible cells. |
| R3-13 | Low | evaluation/voi | The docstring says the nested-MC inner bias is "upward". The ratio estimator's bias can go either way: here it is −3.5% (4 SE) at n_inner=64. |
| R3-14 | Low | evaluation/replay | The fold guard places the knowledge cutoff at 31 Dec of the *reference* year, ignoring publication lag. MAE is pooled across countries of very different scale. |
| R3-15 | Low | population/service (lead) | `"ess": diag.get("ess")` is always `None`; the diagnostics key is `effective_sample_size`. |
| R3-16 | Low | registry/concepts, sources | The Eurostat population concept omits the caveat about legal/registered residents and the completed-years age convention. WDI population's dependence on Eurostat and UN WPP is not recorded in its lineage family. |
| R3-17 | Low | tournament | MASE scales are taken from 2-point windows and from gapped years, which gives heavy-tailed scaled errors and mixes 1-year and multi-year differences. |
| R3-18 | Low | tournament_service | The holdout "confirmation" accepts a challenger that is up to 10% *worse* than the incumbent, judged on a point estimate from about 16 cells. |
| R3-19 | Low | kernel/uncertainty | A sensitivity range can only be recorded under `status=quantified`. Scenario-assumption spread therefore displaces the measurement-uncertainty status. This is the root cause of part of R3-03. |

---

## High

### R3-01: Banded age coverage silently dropped from the "disjoint leaf partition"

* **Where:** `src/reva/population/recipe.py:493-498` (`leaf_codes` is selected by code *kind*), `:546-560` (every band is excluded as a "subtotal"), `:365` (`is_internally_consistent` is computed but never enforced). Neither `materialise.allocate` (`src/reva/population/materialise.py:181`) nor `population/service.py:74-77` checks it.
* **Issue:** A disjoint leaf partition is a set of cells that *covers* the universe without overlap. The code instead fixes the leaves to {single years, `Y_OPEN`, `UNK`}. Every band (`Y_LTn`, `Ya-b`, `Y_GEn`, `Y_GTn`) is excluded, even when no finer cells exist beneath it. The consistency check against `TOTAL` then reports `inconsistent`, but the recipe is still built and materialised.
* **Why it is wrong:** It breaks spec §10 ("All leaf-cell totals conserved"), §15.2 ("Preserve every source cell's population exactly") and invariant 24 ("No … failed conservation check hidden"). The population is understated, and the diagnostics compare the table only against the truncated recipe, so they report zero calibration error.
* **Evidence:** In `s10_population.py` case A, the source has singles `Y_LT1`–`Y84` plus `Y_GE85`, with `TOTAL`=1350 per sex. The recipe total is 1700 against an expected 2700, `is_internally_consistent=False`, and the materialised exact total weight is 1700. This cannot happen with the captured FR `demo_pjan` fixture, whose 103 codes are `TOTAL`, `Y_LT1`, `Y1`–`Y99`, `Y_OPEN` and `UNK`. It can happen with any Eurostat table the generic template may admit that top-codes with a band (`demo_pjanbroad`, `demo_pjangroup` and some older national vintages).
* **Fix:**
  1. Build the partition as the set of *minimal* cells: a band is a leaf when none of the single ages it contains is present with an observed count. Reject partial overlap with `ValidationProblem`.
  2. Make `build_recipe` raise, or return a typed blocked result, when the `TOTAL`×sex check is `inconsistent`, unless an explicit `accept_inconsistent_source=True` override is recorded in the recipe.
  3. Add a property test: the sum of leaf counts equals the reported `TOTAL` for every generated mix of singles, bands and `Y_OPEN`.

### R3-02: The block bootstrap is anti-conservative and promotes challengers too often

* **Where:** `src/reva/models/tournament.py:71` (`block_length: 3`), `:235` (ordering), `:269-277`, `registry/policies/promotion.yaml` (`block_length: 3`).
* **Issue:** The paired loss differences are ordered (series, origin, horizon), so a 3-block covers about one origin's horizons. Expanding-window, multi-horizon errors overlap in target years and training data, so they are correlated across many origins (an h-step error is MA(h−1) even when the model is correct, and expanding windows add longer memory). Künsch's moving-block bootstrap is consistent only when the block length grows with n; a fixed L=3 discards most of the long-run variance.
* **Evidence:** `s4_tournament_null.py` runs the real `run_tournament` under an exact null: two mirror-image models with equal expected loss, 8 random-walk series × 23 years, horizons 1–3, 480 pairs. P(interval upper bound < 0) is 0.217 at L=1, **0.150 at L=3** and 0.087 at L=10, against a nominal one-sided 0.025. `s5`/`s6` separate the causes: with a fixed scale and L=3, size is 0.120; with L=8, 0.052; with L=20, 0.037. Re-ordering by (series, horizon, origin) makes it worse (0.135–0.165).
* **Why it matters:** The "interval excludes zero" gate is the main protection against promoting on noise (§12.2). At the shipped settings it rejects a true null about 5–6 times more often than its nominal level.
* **Fix:**
  1. Keep the (series, origin, horizon) order. Choose L automatically (Politis–White with a floor of 3×max horizon), or set L ≥ 20 for annual panels with horizons up to 3.
  2. Alternatively, use a Diebold–Mariano / HAC test per horizon (Newey–West lag h−1), or a series-cluster bootstrap when there are enough series.
  3. Record the effective number of independent series in the decision.
  4. Add `s4` as a scientific test asserting size ≤ 0.05 under the null at the chosen policy.

  Changing the policy file needs maintainer review, because its digest is pinned.

### R3-03: The L2 sensitivity range is attached to the L1 point and can exclude it

* **Where:** `src/reva/simulation/engine.py:205-211` (the L1 row in an L2 run gets `_range_uncertainty(lo, hi, grid)`), `src/reva/simulation/spec.py:120-130` and `contracts/scenario.schema.json` (no requirement that the grid contain (0,0)).
* **Issue 1:** The range is the min/max over the grid only. When the grid omits ε=(0,0), the "uncertainty" stored for the L1 value is an interval that does not contain that value. The UI shows the two side by side as "Fixed quantities (L1)" and "Sensitivity range".
* **Issue 2:** The same L1 number carries `status=unquantified` in an L1 run but `status=quantified, kind=sensitivity_range` in an L2 run. Its measurement uncertainty is no better known. The spread across scenario assumptions has displaced the statement that the sources publish no uncertainty for M, X or G (spec §9.1, invariants 9 and 21).
* **Evidence:** In `s1_exposure.py`, with M=100, X=40, G=1000 and p=1.25, the L1 exposure is 1.5. The grid {(−0.5, 0.5), (−0.2, 0.3)} gives a range of [−0.410, 0.608], and `L1 inside range: False`. The scope checks accept this grid. The acceptance identities hold: 25/10/15/1.5%, all-ones gives 0, and ε=0 is bit-identical to L1.
* **Fix:**
  * Keep the L1 row's uncertainty as `UncertaintySpec.unknown(...)`.
  * Publish the grid range as its own result row (variant `L2:grid_range`), or in `applicability["sensitivity"]`, labelled "min/max over the listed grid points (the L1 point is included only if (0,0) is in the grid)".
  * Better, compute the range over grid ∪ {L1}, or require (0,0) in the grid during the scope checks.
  * See R3-19 for the kernel side.

## Medium

### R3-04: Drift prediction intervals are badly under-covered for short windows, and degenerate intervals pass as "quantified"

* **Where:** `src/reva/models/forecasting/drift.py:67-71` (normal quantile, plug-in sd), `:105` (`min_train = 3`).
* **Evidence:** `s2_drift_cov.py` simulates exactly from the model's own data-generating process. A nominal 0.80 interval gives empirical coverage of 0.576 at n=3, 0.671 at n=4, 0.716 at n=6, 0.769 at n=10 and 0.781 at n=20. The variance factor h(1+h/T) is correct (FPP's drift method), but with T−1 degrees of freedom the predictive pivot is Student-t. `s3` shows that a t_{T−1} quantile gives 0.804 at n=3 and 0.799 at n=6.
* **Degenerate case:** A series with exactly constant growth has sd=0 and gets an interval of width 2e−13 that is labelled `quantified` 80%.
* **Why it matters:** The interval is presented as a calibrated 80% prediction interval. Rolling origins start at n=3 in the tournament, so the coverage statistics are dominated by these miscalibrated folds.
* **Fix:**
  * Use `scipy.stats.t.ppf(0.5+lvl/2, df=n_diffs-1)` (exact under Gaussian i.i.d. log-differences).
  * Raise `min_train` to at least 4.
  * When `resid_sd == 0`, return `IneligibleReport("zero_residual_variance")` or `UncertaintySpec.unknown`.
  * Update ASSUMPTIONS, which currently say "no t correction".

### R3-05: Local-level intervals ignore parameter uncertainty and are not calibrated

* **Where:** `src/reva/models/forecasting/local_level.py:72-78`.
* **Sound:** The h-step predictive mean and variance agree with Durbin & Koopman: a_{n+1} and P_{n+1}+(h−1)σ²_η+σ²_ε. `s3` matches statsmodels to 4 significant figures for h=1,2,3. The median point is the Bayes act under absolute loss, which is consistent with MAE/MASE.
* **Issue:** With 6–10 annual points the MLE variances often sit on the zero boundary, so plug-in intervals are too narrow. `s3` gives coverage of 0.703 (n=6) and 0.663 (n=10) for nominal 0.80, with data simulated from the model. The limitation is disclosed in ASSUMPTIONS, but the output is still typed as an 80% prediction interval. Spec §9.2 asks to "calibrate prediction errors against later releases".
* **Fix:** Either calibrate (empirical or conformal quantiles of past rolling-origin log errors for the same series and horizon, inside the fold, labelled as such), or add `conditioning=("plug-in MLE; parameter uncertainty ignored; nominal level not achieved in short samples",)` and report the empirical coverage next to every nominal level in tournament outputs.

### R3-06: Model summary metrics are unpaired and the ranking can flip

* **Where:** `src/reva/models/tournament.py:175-195,388` (each model's MASE is taken over its *own* eligible cases) and `src/reva/evaluation/tournament_service.py:470` (the winner among confirmed challengers is the one with the lowest development MASE).
* **Evidence:** In `s7_unpaired.py`, the naive model is scored on 480 cases, local level on 384 and drift on 456. Overall, local level (1.992) looks better than naive (2.024). On the common local-level-eligible cases, naive (1.965) beats local level (1.992). The naive baseline's MASE includes the noisy 2-point-window origins that the other models never face.
* **Fix:** Report the headline MASE (and coverage) on the intersection of cases where every compared model is eligible, or per pair, and show the unpaired figures only as secondary. Pick the winner among challengers with a paired comparison on common cases, or keep the incumbent and route to review, as the comment at `:474` already concedes.

### R3-07: The subgroup gate uses point estimates and gets *stricter* as data grow

* **Where:** `src/reva/models/tournament.py:253-263, 283-286`.
* **Evidence:** `s8_subgroup_power.py` uses a DGP where drift is truly better (log drift 0.02, sd 0.03). P(promote) is 0.53 with 2 series, 0.63 with 8 and 0.15 with 32. P(blocked by some subgroup's noisy +10% point estimate) is 0.10, 0.37 and 0.85.
* **Why it matters:** The probability that at least one noisy series mean exceeds +10% rises with the number of subgroups. The spec's rule is "no prespecified subgroup with more than 10% loss degradation **unless reviewed**".
* **Fix:** Judge degradation with uncertainty (for example, a block-bootstrap lower bound of the subgroup's relative degradation above 10%) and require a minimum subgroup n. Turn a breach into `requires_review` rather than an automatic block, and report the number of subgroups tested.

### R3-08: `source_select_v1` publishes a value when lineages are incompatible

* **Where:** `src/reva/belief/selection.py:171-177`.
* **Evidence:** In `s11_selection.py`, an import lineage (`flowCode=M`, 100) and an export lineage (`flowCode=X`, 40) under one concept are flagged `incompatible_lineages:flowCode`, yet `{"value": "100"}` is still published, chosen by the `logical_key` sort order. `simulation/compile._resolve_input` never inspects `discrepancy_flags`, so such an estimate can enter a scenario.
* **Why it is wrong:** Spec §9.2 says "do not average incomparable definitions". Selecting an arbitrary member of an incomparable set is the same error.
* **Fix:** When `incompatible_lineages:*` is raised, publish no selection for that estimand. Record a blocked selection or gap instead, with the alternatives listed.
* **Minor:** `alt["value"] != chosen["value"]` flags `"100"` vs `"100.0"` as a disagreement. Compare `Decimal` values.

### R3-09: The `Y_OPEN` lower bound is inferred from code presence, not observed counts

* **Where:** `src/reva/population/recipe.py:497-498`.
* **Evidence:** In `s10` case B (`Y85`–`Y99` reported `:`, `Y_OPEN` = 85+), with `allow_missing=True` the inferred `open_age_lower_bound` is 100 instead of 85. Eurostat labels `Y_OPEN` only "Open-ended age class", and its lower bound varies by country and year. Coarsening and refinement (`stratified.py:155`, `refine.py:348`) then treat the group as 100+.
* **Fix:** Infer the bound as 1 + (highest single age with an observed count). Verify it with the `TOTAL` consistency check, or take it from dataset metadata. If singles above that age are missing, or the check fails, set it to `None` (unknown) and let `coarsen` refuse open-band operations, which it already does when the bound is unknown.

### R3-10: Hindsight in `available_then` reconstruction

* **Where:** `src/reva/evaluation/cutoff.py:189-201` (`elig_at = retract_at = now` in `available_then`).
* **Issue:** A value that was genuinely available at the cutoff, but was retracted or quarantined later, is excluded from the reconstruction. This uses post-cutoff knowledge and removes exactly the bad inputs a real-time forecaster would have faced, so it biases reconstructions and any backtest built on them in a favourable direction. The docstring discloses the "current algorithm design", but it does not separate *availability* (a cutoff fact) from *ex-post quality knowledge*.
* **Fix:** In `available_then`, evaluate retractions with `recorded_at <= cutoff`. Report later retractions and quarantines as flags (`retracted_after_cutoff`) rather than excluding them. Optionally provide a separately labelled "cleaned reconstruction" variant.

### R3-11: No proper probabilistic score in the tournament

* **Where:** `src/reva/models/tournament.py` (only coverage and width are computed). `metrics.crps_gaussian` and `log_score_gaussian` are correct (`s12` matches numerical integration to 1e−8) but unused.
* **Why it matters:** Spec §12.2 asks for proper scores for probabilistic forecasts. Coverage and width are not jointly proper: a model can trade one for the other.
* **Fix:** Add the interval score (Gneiting & Raftery 2007, eq. 43) at the declared level for all interval-producing models. Add log-scale Gaussian CRPS for drift and local level (both have Gaussian log predictives), scaled the same way as MASE when pooled across series.

## Low

* **R3-12 (`recipe.py:569`, `diagnostics.py:345-350`):** `structural_zeros` lists cells with observed count 0. In the FR 2023 fixture these are `F|UNK` and `M|UNK`, and `s10` case C shows the same. These are observed zeros, not impossible cells (§15.1). Rename them `observed_zero_cells`, keep `structural_zeros` for declared impossibilities (none for age × sex), and rename the diagnostic `records_in_observed_zero_cells` accordingly. The zero-record behaviour is fine.
* **R3-13 (`voi.py:125,142`):** The docstring claims the inner-loop bias is upward. The self-normalised posterior estimate p̂=kL₁/(kL₁+(n−k)L₀) is concave or convex in k, so the sign is indeterminate. `s9` gives exact EVSI 7.200 (checked with rational arithmetic) against MC 6.948±0.065 at n_inner=64 and 5.58 at n_inner=2: a downward bias that `std_error` does not include. The shipped test uses n_inner=2000, where it is negligible. Because the outcome space is finite, compute E[U|x] exactly, or draw inner θ from the exact posterior, and fix the docstring.
* **R3-14 (`replay.py:386`, `:432-441`):**
  * The fold guard's knowledge cutoff is 31 Dec of the reference year, which conflates reference time with availability. Annual WDI or Comtrade values arrive months later, so "h=1" is partly a nowcast. Disclose this in the design block, or use a declared publication-lag assumption.
  * `mae_latest_revised` pools absolute errors across countries whose levels differ by orders of magnitude (the CHN/USA populations against FRA/DZA). Report MASE or a per-series breakdown instead.
* **R3-15 (`population/service.py:120`, lead-owned):** `"ess": diag.get("ess")` is always `None`; use `diag["effective_sample_size"]`.
  * At `:59`, `tuple(str(st))` splits a status string into characters. That only works by accident for single-letter Eurostat flags.
  * At `:57`, `int(v["value"])` fails on `"123.0"`.
* **R3-16 (`registry/concepts/demography.yaml`, `registry/sources/worldbank.yaml`):**
  * Eurostat's `demo_pjan` metadata says that where usually-resident population is unavailable, countries may report legal or registered residents, and that age is age in completed years at 1 January. Add both to the definition.
  * The captured WDI `SP.POP.TOTL` metadata lists UN WPP, NSOs and **Eurostat** as source organisations, so for France WDI population is not independent of `demo_pjan`. `lineage_family: worldbank_wdi` does not record that dependence (invariant 13).
* **R3-17 (`tournament.py:67, 345`; tournament_service drops missing years):**
  * A 2-point MASE scale is |y₂−y₁|, which can be arbitrarily small, so scaled errors are heavy-tailed: in `s5`, the largest |diff| is about 8× the median.
  * Missing years create gaps, so `mase_scale` averages 1-year and multi-year differences.
  * Start paired comparisons at the largest `min_train` among the compared models, and require consecutive years for the scale, or scale by the per-year difference.
* **R3-18 (`tournament_service.py:451-465`):** The locked-holdout "confirmation" passes a challenger whose holdout MASE is up to 10% *worse* than the incumbent's, judged on a point estimate from about 16 cells. Call it a non-inferiority screen, or require holdout MASE no worse than the incumbent's together with an uncertainty statement. The single-access rule is sound.
* **R3-19 (`kernel/uncertainty.py:87-93`):** `sensitivity_range` is valid only with `status=quantified`. A value cannot say both "measurement uncertainty unquantified" and "scenario-assumption spread is [a,b]". This is a kernel change for the lead or H2: allow a separate `sensitivity` attachment, or a status such as `conditional_on_assumptions`, so R3-03 can be fixed without overloading status.

---

## Verified sound

* **Crude exposure (`crude_exposure.py`):**
  * ΔM, ΔX, Δnet and exposure are implemented exactly as in §9 and §17.2, with 50-digit `Decimal`.
  * The acceptance numbers are reproduced (25/10/15/1.5%), all-ones gives exactly 0, and ε=0 is bit-identical to L1 (`s1`).
  * Missing inputs raise `MissingInput` and are never zeroed. A shared reference year, nominal current USD, annual-flow aggregation and the multiplier/elasticity envelopes are enforced.
  * The NOT_CLAIMED text is correct, and valuation bases come from the provider. The Comtrade connector infers CIF/FOB per flow from `primaryValue` equality and never assumes it, which matters because the USA reports imports on a customs (FOB-type) basis.
  * The L2 constant-elasticity form M·p^(1+ε) is right. L3/L4 return capability gaps, and the L0 cache key excludes only the fidelity. The L1/L2 grid rules make cache hits unambiguous.
* **Metrics:**
  * The MASE scale is the in-sample one-step naive MAE with m=1, as in Hyndman & Koehler (2006). An undefined scale gives `None`, never an epsilon.
  * Gaussian CRPS and log score match numerical integration and scipy (`s12`).
  * Coverage refuses non-prediction-interval kinds.
* **Drift model:** The variance factor σ²h(1+h/T) matches the FPP drift method (apart from R3-04). Its exp(log-mean) median point forecast is the Bayes act under absolute loss.
* **Local level model:** Kalman predictive moments match Durbin & Koopman (`s3`). Fits use only the training window. Failures and non-convergence return `IneligibleReport`.
* **Tournament mechanics:**
  * The moving-block resampling follows Künsch: uniform starts over n−L+1, then concatenation and truncation. Only the block length and ordering are at fault (R3-02).
  * Losses are paired on common cases, insufficient pairs keep the incumbent, the naive baseline is always evaluated, and `replay_mode` is mandatory. The service adds a single-access locked holdout and a degrees-of-freedom declaration.
* **Splits:** Horizons are calendar offsets, so gaps never turn a 2-year forecast into a 1-step one. `CutoffScopedSeries` raises `LeakageBlocked` on any later read.
* **Value of information:**
  * The exact EVSI enumeration is correct Raiffa–Schlaifer preposterior analysis, checked against exact rational arithmetic in three cases, including EVSI=EVPI for a perfect test.
  * Without a decision specification, only a unitless proxy is returned, never a monetary EVSI.
* **Leakage, cutoff and replay:**
  * An unknown deciding timestamp blocks the input. Strict mode never falls back to retrospective mode, and retrospective outputs are labelled everywhere with the "not issued at these origins" disclaimer.
  * Revision selection happens inside the time-filtered set, and first-release and latest-revised targets are scored separately.
  * Operational replay reads the original release and its pinned inputs.
* **Population and compression:**
  * Per-record weights are exact `Fraction`s, so cell, sex and total conservation is exact through materialise, coarsen and refine.
  * The minimum-one largest-remainder allocation reports a typed `ResolutionError` with coarsening options.
  * The Kish ESS and the Gini formula agree with independent computations (`s10` D).
  * IPF pre-checks never alter targets, and a jointly infeasible zero pattern is reported as `converged=False` with a residual of 0.1 (`s10` E).
  * Unsupported refinement dimensions are refused, sensitive attributes are never synthesised, and `Y_OPEN` and `UNK` stay explicit.
  * The FR 2023 fixture (`TOTAL`, `Y_LT1`, `Y1`–`Y99`, `Y_OPEN`, `UNK` by T/M/F) parses into the correct 2×102 leaf partition.
* **`source_select_v1`:** Revision is selected over the whole pre-cutoff lineage before the eligibility and retraction rules. A retracted head excludes the lineage with no stale fallback. A concept with no policy is not selected, and the original uncertainty is passed through. R3-08 is the one gap.
* **Concepts:**
  * The WDI `SP.POP.TOTL` text ("de facto definition … all residents regardless of legal status or citizenship … midyear estimates") and the `NY.GDP.MKTP.CD` text ("total income earned through the production of goods and services in an economic territory during an accounting period … current prices … US dollars") match the captured provider `sourceNote`s verbatim.
  * Midyear (WDI) and 1 January (Eurostat) are distinct concepts.
  * World-total and bilateral trade are distinct supports. The Comtrade `partnerCode 0` / `partner2Code 0` / `customsCode C00` / `motCode 0` dimensions are stated correctly. Imports are "typically CIF" and exports FOB, with the two kept separate. Net weight is never converted to barrels.
* **`registry/evidence/kilian2009.yaml`:** Nothing is fabricated. The DOI 10.1257/aer.99.3.1053 and the title are correct (American Economic Review 99(3):1053–69, 2009). Every quantitative field is `not_extracted`, the review scope is "qualitative relevance only", and the relevance statement relies only on the title. One nuance: Kilian separates *three* shocks (crude supply, aggregate demand, oil-specific/precautionary demand), so "supply vs demand" is a simplification consistent with the title, not an error.

## Requested changes outside my ownership (for owners and the lead)

* **Agent A:** R3-01, R3-09 and R3-12. Add a property test that leaf sums equal the reported `TOTAL`.
* **Agent B:**
  * R3-02 (block length and a null-size test; the policy YAML needs maintainer review because it is digest-pinned).
  * R3-04 (t quantile, `min_train`, zero-sd handling).
  * R3-05, R3-06, R3-07, R3-11, R3-13 and R3-17.
* **Agent C:** R3-03 (separate range row, (0,0) rule, or range over grid ∪ L1).
* **Agent H1:** R3-10 and R3-14.
* **Agent L:** R3-06 (winner selection) and R3-18.
* **Lead:**
  * R3-08 (`belief/selection.py`).
  * R3-15 (`population/service.py`).
  * R3-16 (registry text and the WDI lineage-family dependence).
  * R3-19 (a kernel uncertainty status or attachment, shared with H2).
