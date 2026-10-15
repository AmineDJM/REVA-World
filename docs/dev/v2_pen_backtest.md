# Back-test: France, 2010 pension reform, employment rate 60-64 (agent PEN)

Model `social.retirement_age@1.0.0` (experimental, maintainer review pending). Protocol
`reva-pen-backtest-fra-2010@1.0.0`, code `src/reva/models/social/backtest.py`, evaluation
artifact `docs/dev/v2_pen_backtest.json` (digest
`8ecf41da51ca6a29f96947ba4ff951caf18c44c75d8c2b691d4d7feb0305a913`, regenerated 2026-10-15 by agent
SFX1 because the FRA rule-table digest changed when its `model_lever.qualification` was added; every
result value is unchanged; the test
`tests/unit/social/test_retirement_backtest.py` recomputes it from the captured Eurostat
fixtures and fails if this file is stale).

## Read this first

**The central prediction fits well (mean absolute error 0.55 pp for women, 1.20 pp for men
over 2012-2019), but this is not evidence that the model's mechanism is right.** A placebo
check on ages 55-59 - which no path makes eligible, so the model predicts *no change* -
misses by 6-12 pp: employment of 55-59-year-olds rose by 11-12 pp between 2010 and 2019 for
reasons the model does not represent (cohort effects, earlier reforms, closure of other
early-exit routes). The same cohorts reach 60-64 a few years later, so part of the observed
60-64 rise is that trend, not the 2010 reform. The model gets close anyway because its
pension-receipt drop proxy (the share of 60-64-year-olds outside the labour force in 2010:
82.6 % of women, 80 % of men; the French administrative data of Rabaté & Rochut give a
retirement share of about 53 % at age 60 before the reform) overstates the drop at the affected
ages 60-61 here, which inflates the reform effect. **Two errors of opposite sign offset each other.** A good fit
here should not be read as validation; with this test the model cannot be distinguished from
"reform effect too large + trend omitted".

**Not pre-registered (verifiable).** The protocol, the grid, the choice of central point and the
results first appear together in the same uncommitted-then-snapshotted work (WIP snapshot commits);
no protocol digest was committed before the test years were examined. The central point (Staubli &
Zweimüller) is also the best-fitting grid point. Read "fixed before the test years were examined"
below as the author's statement, which git history cannot confirm. A future test should commit a
protocol digest first. The per-year errors of **every** grid point are reported in one table below,
so the choice of central point does not hide the spread.

**The pension-receipt proxy is not an upper bound in general.** Here the affected ages are 60-61,
whose inactivity is below the 60-64 group mean, so the group-mean proxy overstates the effect in this
back-test. For other reforms (e.g. ages 62-64 under the 2023 reform, base 2024) the same proxy can
understate; its direction is unknown in general (review finding H4).

## Protocol (stated by the author as fixed before the test years were examined; not verifiable)

* Information set: Eurostat LFS `lfsa_ergan` employment rate and `lfsa_argan` activity rate of
  France, ages 60-64, by sex, **2010 only** (the prediction function refuses any row after 2010);
  the FRA rule table; the literature grid. No parameter is estimated; nothing is tuned on
  2011-2019.
* Policy input: rule path `law_2010_as_amended_2011` (Loi n° 2010-1330
  accelerated by Loi n° 2011-1906 / LFSS 2012, as in CSS art. D161-2-1-9) against
  `pre_2010_counterfactual` (60 for every cohort). Using the as-applied schedule is a statement
  about the policy, not about the outcome. The November 2010 as-enacted schedule (4 months per
  cohort) is not in the archived sources and was not used.
* Share of person-time below the minimum age at 60-64 (uniform weights over single ages; no
  post-2010 population used): 2012 0.1083, 2013 0.1583, 2014 0.2319, 2015 0.2889, 2016 0.3375,
  2017-2019 0.4 (ages 60 and 61 fully below 62).
* Prediction: `e(t) = e(2010) + 100 * s * rho * I(t)` with `rho = 1 - activity_rate(2010)/100`
  (French data: 0.826 women, 0.80 men) and `s` the literature share of the pension-receipt drop
  that is employed instead.
* Grid: every `verified_from_text` entry of `registry/assumptions/retirement_employment_response.yaml`
  (agent LIT) **except estimates whose estimation sample overlaps the reform being tested**:
  Rabaté & Rochut (2020) estimate the 2010 French reform itself and are excluded (leakage;
  `test_no_grid_estimate_overlaps_the_backtested_reform_or_period`). Central point (called
  "pre-registered" by the author; not verifiable, see above, and it is also the best-fitting point):
  Staubli & Zweimüller (2013, Austria; s = 0.447 women, 0.392 men). Benchmark: persistence (2010
  rate carried forward).

## Results: central point (Staubli & Zweimüller), employment rate 60-64 (%)

| Year | Women: predicted | Women: observed | Women: error (pp) | Men: predicted | Men: observed | Men: error (pp) |
|---|---|---|---|---|---|---|
| 2012 | 20.6 | 19.7 | 0.9 | 22.5 | 23.5 | -1 |
| 2013 | 22.45 | 21.9 | 0.55 | 24.07 | 25 | -0.93 |
| 2014 | 25.16 | 25 (b) | 0.16 | 26.37 | 25.6 (b) | 0.77 |
| 2015 | 27.27 | 27.5 | -0.23 | 28.16 | 27.9 | 0.26 |
| 2016 | 29.06 | 28.5 | 0.56 | 29.68 | 28 | 1.68 |
| 2017 | 31.37 | 30 | 1.37 | 31.64 | 29.2 | 2.44 |
| 2018 | 31.37 | 31.3 | 0.07 | 31.64 | 31.1 | 0.54 |
| 2019 | 31.37 | 31.9 | -0.53 | 31.64 | 33.6 | -1.96 |

(b) = Eurostat break in series (French LFS redesign). 2010 base: women 16.6 %, men 19.1 %.
The model predicts a plateau from 2017 (the reform is fully phased in for 60-61) while observed
rates keep rising (men +2.5 pp 2017 -> 2019): the omitted trend again.

## Results: every grid point, per year (error = predicted - observed, percentage points)

| Grid point | s (F / M) | sex | 2012 | 2013 | 2014 | 2015 | 2016 | 2017 | 2018 | 2019 | mean error | MAE |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| staubli_zweimuller_2013_aut (central) | 0.447 / 0.392 | women | 0.9 | 0.55 | 0.16 | -0.23 | 0.56 | 1.37 | 0.07 | -0.53 | 0.36 | 0.55 |
| staubli_zweimuller_2013_aut (central) | 0.447 / 0.392 | men | -1 | -0.93 | 0.77 | 0.26 | 1.68 | 2.44 | 0.54 | -1.96 | 0.23 | 1.2 |
| cribb_emmerson_tetlow_2016_gbr | 0.548 / 0.548 | women | 1.8 | 1.87 | 2.1 | 2.18 | 3.38 | 4.71 | 3.41 | 2.81 | 2.78 | 2.78 |
| cribb_emmerson_tetlow_2016_gbr | 0.548 / 0.548 | men | 0.35 | 1.04 | 3.67 | 3.86 | 5.9 | 7.44 | 5.54 | 3.04 | 3.85 | 3.85 |
| geyer_welteke_2017_deu_wp | 0.578 / 0.578 | women | 2.07 | 2.26 | 2.67 | 2.89 | 4.21 | 5.7 | 4.4 | 3.8 | 3.5 | 3.5 |
| geyer_welteke_2017_deu_wp | 0.578 / 0.578 | men | 0.61 | 1.42 | 4.23 | 4.56 | 6.71 | 8.4 | 6.5 | 4 | 4.55 | 4.55 |
| geyer_welteke_2021_deu | 0.489 / 0.489 | women | 1.28 | 1.1 | 0.97 | 0.77 | 1.73 | 2.76 | 1.46 | 0.86 | 1.36 | 1.36 |
| geyer_welteke_2021_deu | 0.489 / 0.489 | men | -0.16 | 0.29 | 2.57 | 2.5 | 4.3 | 5.55 | 3.65 | 1.15 | 2.48 | 2.52 |
| atav_jongen_rabate_2021_nld_wp | 0.321 / 0.321 | women | -0.23 | -1.1 | -2.25 | -3.24 | -2.95 | -2.79 | -4.09 | -4.69 | -2.67 | 2.67 |
| atav_jongen_rabate_2021_nld_wp | 0.321 / 0.321 | men | -1.62 | -1.83 | -0.54 | -1.38 | -0.23 | 0.17 | -1.73 | -4.23 | -1.42 | 1.47 |
| ardito_2021_ita | 0.198 / 0.198 | women | -1.33 | -2.71 | -4.61 | -6.18 | -6.38 | -6.86 | -8.16 | -8.76 | -5.62 | 5.62 |
| ardito_2021_ita | 0.198 / 0.198 | men | -2.68 | -3.39 | -2.83 | -4.22 | -3.55 | -3.76 | -5.66 | -8.16 | -4.28 | 4.28 |
| benchmark_persistence | 0 / 0 | women | -3.1 | -5.3 | -8.4 | -10.9 | -11.9 | -13.4 | -14.7 | -15.3 | -10.38 | 10.38 |
| benchmark_persistence | 0 / 0 | men | -4.4 | -5.9 | -6.5 | -8.8 | -8.9 | -10.1 | -12 | -14.5 | -8.89 | 8.89 |

The literature range spans mean errors from about -5.6 to +4.6 pp: the response assumption matters
more than anything else in the model. Three grid points over-predict in almost every year and sex
(the British and both German estimates), two under-predict in almost every year (the Dutch and
Italian estimates); only the central point stays within about ±2.5 pp, which is exactly why it
should not be read as validated (see "Read this first").

## Placebo: ages 55-59 (model predicts no change)

| Year | Women 55-59: observed (error of "no change") | Men 55-59: observed (error) |
|---|---|---|
| 2012 | 63.5 (-6.3) | 71.3 (-6.9) |
| 2013 | 63.9 (-6.7) | 71.4 (-7) |
| 2014 | 64.9 (-7.7) | 71.1 (-6.7) |
| 2015 | 65.7 (-8.5) | 72.4 (-8) |
| 2016 | 67.2 (-10) | 73.8 (-9.4) |
| 2017 | 69.1 (-11.9) | 74.8 (-10.4) |
| 2018 | 68.8 (-11.6) | 75.7 (-11.3) |
| 2019 | 69.2 (-12) | 75.7 (-11.3) |

## Limits of this test

One country, one reform, one age group, two sexes, eight years (16 sex-year points that are
strongly autocorrelated): the denominator is small. The test checks the arithmetic chain
(rule table -> eligibility shares -> response) against outcomes, not the causal claim. It says
nothing about the 2023 reform, about other countries, or about the pension-recipient and
expenditure outputs (no age-specific ESSPROS recipients exist to test them).
