# REVA V7 — beating official forecasters, honestly

Owner goal (2026-10-17): REVA's numeric forecasts must beat the IMF, the SPF, the EIA and every other official
forecaster in every domain. REVA pursues this with everything it has, and the judge decides: a win only counts
when it is measured against the official forecast **available at the same date**, on data **as published at
that date**, on a **pre-registered** evaluation, with proper scores and multiple-testing correction. No result
is ever adjusted to produce a win (`docs/dev/review_v6_predictions.md` is the standard).

## Where REVA can legitimately win

1. **Combination (stacking) with the official forecast as an input.** Forecast-combination literature: a
   well-weighted combination of the official forecast and independent models often beats each component.
   REVA's win is then "REVA's combined forecast beats the IMF alone", stated exactly so.
2. **Timeliness (nowcasting).** Official forecasts are published on fixed dates (WEO twice a year, STEO monthly,
   SPF quarterly). Between releases, REVA can update daily with fresher data (monthly/daily series, trade,
   prices, shipping, energy weekly data, media signals). The fair comparison: on date t, REVA's forecast vs the
   latest official forecast published on or before t.
3. **Bias correction.** Official forecasts have documented, persistent biases (WEO next-year GDP +1.24 pp).
   Correcting them with only past errors known at t is legitimate.
4. **Cross-domain causal structure.** Shocks propagate through REVA's graph (oil → importers' CPI via
   input-output; conflict → neighbours; disasters → GDP; trade links → spillovers) using attributed effect
   estimates, as features or structural priors.
5. **Breadth.** Panel/global models trained across ~200 countries and many domains (pooled gradient boosting,
   hierarchical Bayesian), with strict vintage discipline.

## Where winning is not expected (and will be reported as such)

Weather beyond ~2 weeks; asset prices/FX/yields (efficient markets — beating the random walk would itself be
exceptional); rare-event timing. REVA still competes there, but claims nothing without evidence.

## Protocol for every track

- Information set at t only (VintageAccessor; publication-date rules; LeakageGuard).
- Benchmarks: the official forecast available at t, its fair density (official point + σ from its own past
  errors), and the best naive.
- Pre-registration file committed before any holdout is opened; the V6 holdout is spent → new holdouts are
  either untouched targets/periods declared in advance or the **prospective registry** (sealed forecasts).
- Scores: MAE/RMSE for points vs points, CRPS/log score for densities vs densities; DM/HLN with two-way
  clustering where relevant; Holm across the whole family.
- A track "beats X" only when it wins on the pre-registered evaluation; the scoreboard lists wins AND losses.
