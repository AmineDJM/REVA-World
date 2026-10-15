# Back-test: France, 2010 pension reform, employment rate 60-64 (agent PEN)

Model `social.retirement_age@1.0.0` (experimental, maintainer review pending). Protocol
`reva-pen-backtest-fra-2010@1.0.0`, code `src/reva/models/social/backtest.py`, evaluation
artifact `docs/dev/v2_pen_backtest.json` (digest
`111ae6bfc8c6794666f2412aab665a94f111d1334287b51012c829940dc2e83f`; the test
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
pension-receipt drop is an *upper-bound proxy* (the share of 60-64-year-olds outside the
labour force in 2010: 82.6 % of women, 80 % of men; the French administrative data of
Rabaté & Rochut give a retirement share of about 53 % at age 60 before the reform), which
inflates the reform effect. **Two errors of opposite sign offset each other.** A good fit
here should not be read as validation; with this test the model cannot be distinguished from
"reform effect too large + trend omitted".

## Protocol (fixed before the test years were examined)

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
  `test_no_grid_estimate_overlaps_the_backtested_reform_or_period`). Pre-registered central point:
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

## Results: every grid point, 2012-2019

| Grid point | s (F / M) | Women: mean error / MAE (pp) | Men: mean error / MAE (pp) |
|---|---|---|---|
| staubli_zweimuller_2013_aut (central) | 0.447 / 0.392 | 0.36 / 0.55 | 0.23 / 1.2 |
| cribb_emmerson_tetlow_2016_gbr | 0.548 / 0.548 | 2.78 / 2.78 | 3.85 / 3.85 |
| geyer_welteke_2017_deu_wp | 0.578 / 0.578 | 3.5 / 3.5 | 4.55 / 4.55 |
| geyer_welteke_2021_deu | 0.489 / 0.489 | 1.36 / 1.36 | 2.48 / 2.52 |
| atav_jongen_rabate_2021_nld_wp | 0.321 / 0.321 | -2.67 / 2.67 | -1.42 / 1.47 |
| ardito_2021_ita | 0.198 / 0.198 | -5.62 / 5.62 | -4.28 / 4.28 |
| benchmark_persistence | 0 / 0 | -10.38 / 10.38 | -8.89 / 8.89 |

The literature range spans errors from about -5.6 to +4.6 pp on average: the response
assumption matters more than anything else in the model.

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
