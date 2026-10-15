# Back-test: `demography.cohort_component@1.0.0` (V2 step 3a, agent POP)

**Label: conditional projection under stated assumptions, not a forecast.** The numbers below
describe how this *method* would have done on past data. They are not a probability statement
about any future projection, and they are shown whether good or bad (V2 honesty rule 3).

* Stored evaluation artifact: [`v2_pop_backtest_evaluation.json`](v2_pop_backtest_evaluation.json)
  (schema `reva-population-backtest/1`, sha256 `4a57a5e2…3ad08703` of the file; it lists the sha256 and
  URL of every archived Eurostat response it was computed from and the model implementation digest).
  In a database the same manifest is stored content-addressed (namespace `evaluation`) with one
  `model.evaluation` row per country (`replay_mode = revised_data_retrospective`) by
  `reva.evaluation.population_backtest.run_population_evaluation`, which reads only the dataset
  inputs pinned by a belief release (tested in `tests/integration/test_demography_cohort.py`; run live
  for FRA, DEU, ITA, ESP, POL, see "Live end-to-end run" below).
* Code: `reva.models.population.cohort.backtest` (pure); nothing is fitted on the test period and
  no parameter was tuned after seeing the results.

## Design

| | primary (leak-free) | literal reading of the brief |
|---|---|---|
| jump-off population | observed 1 January **2013** (demo_pjan) | observed 1 January **2014** |
| mortality, fertility, migration from | calendar years **2008–2012** | calendar years **2009–2013** |
| target | observed 1 January 2023 (10 projected years) | observed 1 January 2023 (9 years) |

The brief says "start from observed 2013 population and 2009–2013 rates". Rates for calendar year
2013 need the 2013 deaths/births and the population of 1 January 2014 (exposure and the migration
residual), which lie *after* a 1 January 2013 jump-off; using them would leak the first projection
year. Decision (owner asleep, conservative): the **primary** design ends the base period before the
jump-off (2008–2012 → jump-off 2013); the literal design is reported too, with its jump-off moved to
1 January 2014 so that it is also leak-free. Both are enforced by the model (`project()` refuses a
base period or migration window that does not end before the jump-off year).

Variants: mortality and fertility always constant at base-period rates; net migration either the
**average residual** of the base period (the L1 default) or **zero**. EUROPOP2023 assumptions do not
exist for 2013 and are not used in the back-test.

Benchmark: **naive no-change** (the 2023 total equals the jump-off total).

Metrics (percent): **APE** of a group total = |projected − observed| / observed × 100 (both sexes);
**MAPE** = mean of the APE over the single-age × sex cells of the group (cells with an observed 0
are excluded and counted). Groups 0–14, 15–64, 65+ (65 to the open age) and all ages. The open
age is the data's (see "open age" column; ages above it are summed, never split).

Data vintage: the **current** Eurostat vintage (fetched 2026-10-15), i.e. revised historical data,
not what was published in 2013 (`revised_data_retrospective`). Status flags in the test period
(b = break, p = provisional, e = estimated) are listed per country; a break means the 2023 figure is
not on the same basis as the 2013 one, and part of the error is then a statistical revision, not a
projection error.

## Results — primary design (jump-off 2013, rates 2008–2012, target 2023)

| Country | open age | total APE (avg. residual mig.) | 0-14 APE / MAPE | 15-64 APE / MAPE | 65+ APE / MAPE | all-ages single-age MAPE | total APE (zero mig.) | total APE naive no-change | test-period flags |
|---|---|---|---|---|---|---|---|---|---|
| AUT | 100 | 5.304 | 7.465 / 7.423 | 5.694 / 5.766 | 2.392 / 4.126 | 5.427 | 8.807 | 7.171 | – |
| BEL | 100 | 2.175 | 8.828 / 9.306 | 1.744 / 1.96 | 2.009 / 5.487 | 4.308 | 4.744 | 5.151 | – |
| BGR | 100 | 1.598 | 3.24 / 11.431 | 2.114 / 5.341 | 0.76 / 6.194 | 6.55 | 3.494 | 11.707 | – |
| CHE | 100 | 0.93 | 1.268 / 1.784 | 1.416 / 1.661 | 1 / 4.666 | 2.75 | 8.431 | 8.806 | – |
| CYP | 100 | 9.309 | 16.443 / 16.589 | 10.388 / 10.749 | 1.235 / 10.796 | 11.633 | 5.667 | 9.086 | – |
| CZE | 100 | 2.626 | 7.935 / 8.038 | 1.394 / 1.828 | 2.247 / 6.724 | 4.495 | 5.336 | 2.876 | 2021:b |
| DEU | 95 | 8.898 | 14.887 / 14.818 | 9.379 / 9.695 | 3.755 / 4.643 | 8.864 | 7.344 | 3.122 | 2023:b |
| DNK | 100 | 2.096 | 2.542 / 4.852 | 2.467 / 2.491 | 4.566 / 6.519 | 4.277 | 5.503 | 5.563 | – |
| ESP | 100 | 1.722 | 1.292 / 3.767 | 2.501 / 2.942 | 1.195 / 3.795 | 3.368 | 4.204 | 2.855 | – |
| EST | 100 | 8.912 | 8.21 / 8.153 | 9.861 / 9.789 | 6.51 / 9.939 | 9.599 | 6.379 | 3.347 | – |
| FIN | 100 | 0.743 | 12.463 / 14.737 | 0.952 / 1 | 2.374 / 4.401 | 4.252 | 2.343 | 2.468 | – |
| FRA | 99 | 0.936 | 3.557 / 4.776 | 1.561 / 1.939 | 2.775 / 3.964 | 3.073 | 1.731 | 3.921 | 2014:b, 2018:p, 2023:p |
| GRC | 100 | 0.783 | 3.742 / 4.249 | 0.592 / 2.898 | 0.399 / 10.865 | 5.938 | 2.303 | 5.558 | – |
| HRV | 85 | 4.646 | 9.997 / 10.052 | 6.167 / 6.388 | 2.923 / 3.975 | 6.438 | 5.537 | 10.307 | – |
| HUN | 85 | 2.671 | 12.319 / 12.551 | 2.014 / 4.264 | 2.068 / 3.22 | 5.454 | 3.161 | 3.078 | – |
| IRL | 100 | 9.518 | 1.797 / 2.533 | 12.369 / 12.463 | 7.059 / 10.381 | 10.246 | 6.065 | 12.551 | – |
| ISL | 100 | 14.696 | 1.095 / 5.814 | 20.939 / 19.702 | 3.416 / 7.441 | 13.269 | 10.798 | 16.995 | – |
| ITA | 100 | 3.828 | 8.058 / 8.821 | 4.744 / 5.196 | 0.783 / 3.516 | 5.136 | 1.512 | 2.17 | 2019:b |
| LIE | 100 | 1.469 | 3.268 / 5.851 | 1.575 / 3.313 | 0.209 / 11.781 | 6.632 | 5.76 | 7.155 | – |
| LTU | 100 | 15.895 | 16.286 / 16.354 | 19.159 / 19.1 | 4.993 / 7.905 | 14.702 | 1.675 | 4.107 | – |
| LUX | 95 | 2.566 | 1.391 / 5.054 | 3.236 / 3.436 | 3.667 / 6.962 | 4.827 | 17.243 | 18.73 | 2017:b |
| LVA | 100 | 14.611 | 22.551 / 22.687 | 15.532 / 16.355 | 5.862 / 6.977 | 13.953 | 0.383 | 6.785 | – |
| MLT | 100 | 18.621 | 7.981 / 7.912 | 24.53 / 21.529 | 4.068 / 14.094 | 16.857 | 22.426 | 22.246 | – |
| NLD | 100 | 2.848 | 2.244 / 5.368 | 4.283 / 4.336 | 2.125 / 4.168 | 4.429 | 4.842 | 5.792 | – |
| NOR | 100 | 4.119 | 14.909 / 15.933 | 3.493 / 3.483 | 3.447 / 7.127 | 6.631 | 4.952 | 7.974 | – |
| POL | 100 | 0.134 | 0.683 / 4.485 | 0.411 / 1.931 | 0.132 / 2.996 | 2.69 | 1.347 | 3.561 | 2021:bep, 2022:ep, 2023:ep |
| PRT | 100 | 4.666 | 7.131 / 7.354 | 3.325 / 3.822 | 6.879 / 7.42 | 5.629 | 3.941 | 0.121 | – |
| ROU | 100 | 3.998 | 14.742 / 14.907 | 0.632 / 12.982 | 6.109 / 11.376 | 12.696 | 0.425 | 5.063 | 2020:e, 2021:e, 2022:e, 2023:e |
| SVK | 100 | 1.739 | 7.224 / 8.519 | 0.139 / 1.021 | 2.716 / 6.611 | 4.127 | 1.767 | 0.331 | – |
| SVN | 100 | 1.222 | 0.457 / 1.035 | 0.581 / 1.65 | 3.658 / 5.802 | 3.038 | 4.53 | 2.747 | – |
| SWE | 100 | 1.895 | 2.799 / 5.611 | 2.921 / 2.888 | 2.764 / 5.802 | 4.331 | 7.62 | 9.178 | – |

Summary over 31 countries (median / mean, %): total APE average-residual migration 2.67 / 5.01; zero migration 4.84 / 5.49; naive no-change 5.56 / 6.79; all-ages single-age x sex MAPE 5.45 / 6.96; group APE 0-14 7.22 / 7.32, 15-64 2.92 / 5.68, 65+ 2.76 / 3.04.
Average-residual projection beats the naive no-change total in 23 of 31 countries; zero migration beats it in 21.

## Results — literal design (jump-off 2014, rates 2009–2013, target 2023)

| Country | open age | total APE (avg. residual mig.) | 0-14 APE / MAPE | 15-64 APE / MAPE | 65+ APE / MAPE | all-ages single-age MAPE | total APE (zero mig.) | total APE naive no-change | test-period flags |
|---|---|---|---|---|---|---|---|---|---|
| AUT | 100 | 4.14 | 6.157 / 6.115 | 4.39 / 4.477 | 1.804 / 3.423 | 4.345 | 7.913 | 6.557 | – |
| BEL | 100 | 1.097 | 6.557 / 7.117 | 0.596 / 1.057 | 1.859 / 4.668 | 3.244 | 4.428 | 4.786 | – |
| BGR | 100 | 2.456 | 4.991 / 10.833 | 3.245 / 4.958 | 1.165 / 5.713 | 6.1 | 2.935 | 10.387 | – |
| CHE | 100 | 0.946 | 1.741 / 1.958 | 1.243 / 1.477 | 0.694 / 5.203 | 2.877 | 7.153 | 7.666 | – |
| CYP | 100 | 1.844 | 6.277 / 6.684 | 1.972 / 5.266 | 2.601 / 10.069 | 7.189 | 6.61 | 9.383 | – |
| CZE | 100 | 4.032 | 9.5 / 9.628 | 3.311 / 3.523 | 1.936 / 6.24 | 5.398 | 5.236 | 2.91 | 2021:b |
| DEU | 95 | 6.817 | 12.106 / 12.039 | 6.978 / 7.25 | 3.031 / 4.139 | 6.994 | 6.563 | 2.829 | 2023:b |
| DNK | 100 | 2.278 | 0.404 / 3.274 | 2.501 / 2.519 | 3.678 / 5.125 | 3.56 | 5.134 | 5.148 | – |
| ESP | 100 | 5.266 | 4.409 / 4.68 | 6.598 / 6.993 | 1.465 / 3.991 | 5.579 | 4.767 | 3.306 | – |
| EST | 100 | 8.791 | 9.997 / 9.96 | 9.59 / 9.52 | 5.312 / 8.054 | 9.063 | 6.465 | 3.665 | – |
| FIN | 100 | 0.885 | 11.785 / 13.944 | 0.729 / 0.858 | 1.915 / 3.447 | 3.724 | 1.959 | 2.026 | – |
| FRA | 99 | 0.351 | 5.472 / 5.858 | 0.361 / 1.321 | 1.743 / 3.027 | 2.599 | 1.102 | 3.092 | 2018:p, 2023:p |
| GRC | 100 | 1.641 | 0.178 / 1.409 | 2.145 / 3.433 | 1.094 / 10.83 | 5.769 | 1.415 | 4.734 | – |
| HRV | 85 | 3.139 | 7.31 / 7.343 | 4.071 / 4.195 | 2.057 / 3.206 | 4.503 | 5.21 | 9.523 | – |
| HUN | 85 | 2.961 | 12.326 / 12.509 | 2.38 / 4.458 | 1.82 / 2.931 | 5.489 | 2.957 | 2.609 | – |
| IRL | 100 | 10.574 | 4.421 / 4.394 | 13.278 / 13.354 | 6.753 / 9.636 | 10.698 | 6.523 | 12.019 | – |
| ISL | 100 | 13.939 | 3.128 / 5.511 | 19.446 / 18.148 | 2.547 / 6.911 | 12.266 | 10.569 | 16.012 | – |
| ITA | 100 | 2.823 | 6.162 / 6.843 | 3.478 / 3.797 | 0.636 / 3.393 | 4.105 | 1.067 | 2.286 | 2019:b |
| LIE | 100 | 0.786 | 2.011 / 5.408 | 0.825 / 2.973 | 0.247 / 8.934 | 5.407 | 5.198 | 6.422 | – |
| LTU | 100 | 14.43 | 14.418 / 14.445 | 17.622 / 17.751 | 4.063 / 6.55 | 13.267 | 1.863 | 3.17 | – |
| LUX | 100 | 1.49 | 1.994 / 5.122 | 2.239 / 2.839 | 1.718 / 6.633 | 4.53 | 15.282 | 16.817 | 2017:b |
| LVA | 100 | 12.804 | 20.713 / 20.814 | 13.599 / 14.36 | 4.46 / 5.609 | 12.199 | 0.887 | 5.605 | – |
| MLT | 100 | 16.39 | 4.769 / 4.719 | 22.032 / 19.111 | 3.494 / 11.049 | 14.1 | 21.06 | 21.012 | – |
| NLD | 100 | 3.096 | 0.831 / 4.656 | 4.507 / 4.559 | 1.574 / 3.906 | 4.341 | 4.702 | 5.513 | – |
| NOR | 100 | 3.774 | 13.202 / 14.18 | 3.267 / 3.286 | 2.984 / 5.765 | 5.788 | 4.177 | 6.922 | – |
| POL | 100 | 0.158 | 1.812 / 4.17 | 0.549 / 1.92 | 0.411 / 2.094 | 2.316 | 1.288 | 3.439 | 2021:bep, 2022:ep, 2023:ep |
| PRT | 100 | 5.772 | 9.025 / 9.236 | 4.805 / 5.13 | 6.568 / 7.306 | 6.515 | 4.343 | 0.69 | – |
| ROU | 100 | 1.313 | 10.836 / 10.894 | 3.303 / 11.642 | 8.489 / 11.051 | 11.32 | 0.303 | 4.682 | 2020:e, 2021:e, 2022:e, 2023:e |
| SVK | 100 | 1.491 | 6.981 / 8.232 | 0.082 / 0.895 | 1.754 / 4.517 | 3.276 | 1.531 | 0.237 | – |
| SVN | 100 | 2.893 | 1.813 / 1.862 | 3.013 / 3.177 | 3.295 / 5.452 | 3.793 | 4.277 | 2.64 | – |
| SWE | 100 | 1.521 | 3.014 / 5.387 | 2.542 / 2.48 | 2.272 / 4.868 | 3.763 | 6.785 | 8.332 | – |

Summary over 31 countries (median / mean, %): total APE average-residual migration 2.89 / 4.51; zero migration 4.70 / 5.15; naive no-change 4.79 / 6.27; all-ages single-age x sex MAPE 5.41 / 6.26; group APE 0-14 6.16 / 6.59, 15-64 3.27 / 5.31, 65+ 1.94 / 2.69.
Average-residual projection beats the naive no-change total in 20 of 31 countries; zero migration beats it in 21.

## Reading the results honestly

* The method (average-residual migration) beats the naive no-change benchmark on the total
  population in 23 of 31 countries, **not everywhere**: it is worse in Lithuania (15.9 % vs 4.1 %),
  Latvia (14.6 % vs 6.8 %), Germany (8.9 % vs 3.1 %), Estonia (8.9 % vs 3.3 %), Portugal (4.7 % vs
  0.1 %), Italy (3.8 % vs 2.2 %), Slovakia (1.7 % vs 0.3 %) and Cyprus (9.3 % vs 9.1 %).
* **The migration assumption dominates the error.** The residual-method average of 2008–2012
  captures the migration regime of the financial-crisis years (large emigration from the Baltic
  states and Portugal, which later reversed; low immigration to Germany before 2015) and, worse,
  **census revisions**: the 2011 census round shows up as breaks (`b`) in the base-period population
  of BEL, CHE, DEU, FRA, GRC, HUN, LUX, POL, SVN (see `input_status_flags` in the evaluation file).
  The residual method books such a revision as "net migration": Germany's residual for 2010
  (1 January 2010 → 1 January 2011, the census-based break) is −1.39 million persons, against
  −0.07 to +0.41 million in the other base years, and averaging it projects it forward. This is a known weakness
  of residual migration estimates, kept visible here rather than corrected by hand.
* Zero migration is better than average-residual migration in 10 of 31 countries (primary design:
  CYP, DEU, EST, IRL, ISL, ITA, LTU, LVA, PRT, ROU);
  neither assumption is a forecast, which is why L2 reports the whole assumption grid.
* Ages 0–14 carry the largest group errors (median APE 7.2 %): births depend on fertility held at
  its 2008–2012 level, while fertility fell in most countries after 2016, and on migration of young
  families. The 65+ group is the most stable (median APE 2.8 %), because it depends mostly on
  survival of cohorts already present.
* Small populations (ISL, MLT, LUX, CYP, LIE) have noisy single-age cells (high single-age MAPE) and
  migration regimes that changed strongly after 2013 (Malta: 421 464 → 542 051 persons, +28.6 %).

## Plausibility comparison with EUROPOP2023 (not a validation)

Projection from 1 January 2025 with base period 2019–2023 (it includes the COVID-19 excess
mortality of 2020–2022, held constant: a stated assumption that lowers survival), compared with the
EUROPOP2023 baseline (proj_23np, BSL) for 2050. EUROPOP2023 is another institution's conditional
projection; agreement is not evidence of correctness. The main structural difference is mortality:
EUROPOP2023 assumes rising life expectancy, this model holds base-period death rates constant. With
EUROPOP's own migration and fertility assumptions the REVA population is therefore lower and older
cohorts smaller (lower old-age dependency ratio), as expected.

| Country | EUROPOP2023 BSL 2050 total | REVA 2050 total, EUROPOP mig.+fert. (diff %) | REVA 2050, avg. residual mig. (diff %) | REVA 2050, zero mig. (diff %) | 65+/15-64 EUROPOP | 65+/15-64 REVA (EUROPOP mig.+fert.) |
|---|---|---|---|---|---|---|
| AUT | 9527146 | 9298347 (-2.4) | 10046453 (5.45) | 8046956 (-15.54) | 46.43 | 41.46 |
| BEL | 12559197 | 12247016 (-2.49) | 13253324 (5.53) | 10975196 (-12.61) | 42.3 | 37.73 |
| BGR | 5857619 | 4844798 (-17.29) | 5126433 (-12.48) | 4893564 (-16.46) | 53.36 | 44.52 |
| CHE | 9739036 | 9683710 (-0.57) | 10600580 (8.85) | 8303882 (-14.74) | 45.59 | 41.22 |
| CYP | 982420 | 961193 (-2.16) | 1244564 (26.68) | 912662 (-7.1) | 38.32 | 41.65 |
| CZE | 10747331 | 10114964 (-5.88) | 11583527 (7.78) | 9517938 (-11.44) | 47.16 | 41.23 |
| DEU | 84813866 | 79966423 (-5.72) | 82017671 (-3.3) | 70561807 (-16.8) | 45.67 | 41.01 |
| DNK | 6150048 | 5995549 (-2.51) | 6412375 (4.27) | 5569603 (-9.44) | 42.11 | 37.41 |
| ESP | 50460161 | 49370637 (-2.16) | 54919175 (8.84) | 41913161 (-16.94) | 58.97 | 53.69 |
| EST | 1340292 | 1269657 (-5.27) | 1550077 (15.65) | 1170483 (-12.67) | 46.21 | 40.33 |
| FIN | 5459062 | 5279395 (-3.29) | 5679670 (4.04) | 4835023 (-11.43) | 46.13 | 41.24 |
| FRA | 70641762 | 68583748 (-2.91) | 71583998 (1.33) | 65938992 (-6.66) | 47.99 | 44.01 |
| GRC | 8964997 | 8566389 (-4.45) | 8338459 (-6.99) | 8426013 (-6.01) | 67.89 | 59.9 |
| HRV | 3310003 | 3182492 (-3.85) | 3086196 (-6.76) | 3124452 (-5.61) | 51.85 | 42.67 |
| HUN | 9230736 | 8550924 (-7.36) | 8418481 (-8.8) | 7759711 (-15.94) | 45.46 | 37.36 |
| IRL | 6009043 | 6039112 (0.5) | 7144431 (18.89) | 5479043 (-8.82) | 43.25 | 40.11 |
| ISL | 537665 | 513157 (-4.56) | 504262 (-6.21) | 401129 (-25.39) | 31.95 | 29.38 |
| ITA | 57518500 | 55246876 (-3.95) | 50612773 (-12.01) | 47240151 (-17.87) | 61.28 | 55.01 |
| LIE | – | no EUROPOP2023 projection (Liechtenstein is not projected) | – | – | – | – |
| LTU | 2337340 | 2187973 (-6.39) | 3068784 (31.29) | 2259786 (-3.32) | 53.4 | 44.78 |
| LUX | 897072 | 875759 (-2.38) | 952059 (6.13) | 650166 (-27.52) | 36.1 | 32.74 |
| LVA | 1466019 | 1352973 (-7.71) | 1558559 (6.31) | 1463992 (-0.14) | 53.61 | 44.86 |
| MLT | 744353 | 751610 (0.97) | 889072 (19.44) | 502164 (-32.54) | 34.23 | 30.61 |
| NLD | 18743896 | 18140752 (-3.22) | 20468198 (9.2) | 16481462 (-12.07) | 42.28 | 37.78 |
| NOR | 6215295 | 6172133 (-0.69) | 6308040 (1.49) | 5297434 (-14.77) | 40.96 | 37.15 |
| POL | 34623544 | 30430194 (-12.11) | 25773187 (-25.56) | 29605091 (-14.49) | 50.47 | 44.06 |
| PRT | 9650059 | 9611119 (-0.4) | 11299625 (17.09) | 8922846 (-7.54) | 62.91 | 55.37 |
| ROU | 16439020 | 15505009 (-5.68) | 16522586 (0.51) | 15969927 (-2.85) | 50.24 | 39.78 |
| SVK | 5179658 | 4761697 (-8.07) | 4612533 (-10.95) | 4619159 (-10.82) | 49.66 | 42.14 |
| SVN | 2094023 | 2025966 (-3.25) | 2181218 (4.16) | 1828265 (-12.69) | 53.72 | 47.33 |
| SWE | 12130240 | 11706959 (-3.49) | 11531413 (-4.94) | 10001440 (-17.55) | 38.52 | 35.58 |

Median difference of the 2050 total vs EUROPOP2023 (30 countries): EUROPOP migration+fertility -3.39 % (range -17.29 to 0.97); average residual migration 4.21 % (range -25.56 to 31.29); zero migration -12.64 %. Median difference of the 65+/15-64 ratio (REVA - EUROPOP, EUROPOP assumptions): -5.12 points (range -10.46 to 3.33).

Germany and Lithuania: the 2022 (DEU) and 2022–2024 (LTU) births-by-mother's-age tables are
incomplete at source (not published / `|N` not for publication); those years are left out of the
fertility-rate estimation (never imputed) and the omission is listed in the run's `data_notes`.

## Live end-to-end run

See `docs/dev/v2_pop.md` ("Live run") for the live ingest of FRA, DEU, ITA, ESP and POL into a
throwaway database, the release pinning, the L2 projection through the worker job path and the
stored evaluation (whose numbers equal the rows above for those countries).
