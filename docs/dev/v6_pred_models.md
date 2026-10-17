# V6 PRED-MODELS — forecasting models on honest vintage backtests

Agent PRED-MODELS, 2026-10-17. Plan: `docs/roadmap/v6_plan.md`; method rules: `docs/roadmap/v5_prediction_engine.md`
(decisions 2 and 4), `docs/strategy/world_map.md` §5. Data: PRED-VINTAGE's vintage tables (`docs/dev/v5_pred_vintage.md`).
Registry: PRED-REGISTRY (`docs/dev/v5_pred_registry.md`). Not financial advice.

> **FIX7 correction (2026-10-17, QA-PRED X1/X5/X6/X7, `docs/dev/v6_fix7.md`).** The three registered models are
> **calibrated intervals around the IMF WEO / SPF point — not better point forecasts**, and their holdout "win" was
> against a **weak benchmark**: `benchmark.imf_weo_gauss` / `benchmark.spf_gauss` = N(official point, spread of
> REVA's *persistence* predictive), whose 80 % interval covered 93–100 % (WEO) / 96 % (SPF) of holdout outcomes.
> Against the **fair benchmark** N(official point, RMS of the official forecaster's own past errors known at the
> origin), re-run on the **development sample only** (the holdout is spent): WEO GDP h0 conformal CRPS ×0.985
> (p 0.033, not Holm-significant), bias-corrected ×0.992 (p 0.18), SPF current quarter ×1.018 (worse) — §12. They
> stay registered but on **probation**: future issuance requires beating the fair benchmark
> (`forecast.weo_error_fan`, now sealed next to them) on the next prospective evaluation. Never write "REVA beats
> the IMF/SPF".

## 1. Result in one paragraph

Sixteen candidate models (direct AR, Minnesota BVAR, two dynamic-factor nowcasts, equal-weight combinations,
bias corrections, conformal densities; one of them pseudo-real-time and never eligible) were evaluated **in real time** — every input read through
`VintageAccessor(available_then)` at each origin, outcomes = first releases from later vintages — against
persistence, the registry AR, the **IMF WEO** (2008–2025, ≈190 countries) and the **Philadelphia Fed SPF** median
(1990–2026), plus G7 quarterly targets on the **OECD MEI real-time editions** (1999–2026): 27,258 scored development
cases and 16,127 scored holdout cases. **No REVA model beats the WEO or the SPF point forecast** — not in
development, not on the holdout (e.g. WEO GDP current year: direct AR MAE ×2.2, WEO+AR combination ×1.4; SPF
current-quarter GDP: BVAR ×1.86). What REVA can add is **calibrated uncertainty around those strong points**: after a
development stage (30 holdout hypotheses frozen in `protocol.yaml`, committed in `53bd6da` before the holdout was
opened) the locked holdout was opened **once**; with Holm over all 30 hypotheses, **3 were confirmed** (significant
and calibrated): the conformal density around the WEO current-year GDP projection (CRPS −23.5 % vs a naive Gaussian
around the WEO, p = 0.0005, 80 % coverage 0.75), its bias-corrected variant (−23.7 %, p = 0.0004, 0.75) and the
conformal density around the SPF current-quarter GDP nowcast (−8.6 %, p = 2e-5, coverage 0.89). Three more passed
Holm but failed calibration (WEO CPI h1 under-covers at 0.61/0.62; SPF unemployment over-covers at 0.94); the
other 24 failed on the holdout (COVID-era errors dominate it). The two WEO winners are **registered** for the
registry family `weo.gdp_growth`, horizon 0, and issue sealed forecasts through supplementary batches (§7); the SPF
winner has no registry family yet. **No validated chain** was recorded for PRED-ENGINE (§8).

## 2. What was built

| Part | File |
|---|---|
| Direct h-step AR / ARDL (OLS, BIC lags, leverage-inflated Gaussian) | `src/reva/models/forecasting/direct.py` |
| BVAR with Minnesota prior (Theil mixed estimation per equation, predictive simulation, explosive draws redrawn) | `src/reva/models/forecasting/bvar.py` |
| One-factor DFM nowcast (PCA + Kalman/RTS over the ragged edge, bridge equation, recursive missing quarter) | `src/reva/models/forecasting/dfm.py` |
| Equal-weight combinations, linear pool, conformal densities, bias correction | `src/reva/models/forecasting/combination.py` |
| Real-time cases per origin (WEO panel, US RTDSM+SPF, G7 OECD MEI editions; transforms inside each edition) | `src/reva/evaluation/pred_models/realtime.py` |
| Second pass: combinations/conformal/bias using only errors published at the origin | `src/reva/evaluation/pred_models/derived.py` |
| Scores (AE, CRPS, log, PIT, 80 % coverage, interval score), DM-HLN by origin, Holm | `src/reva/evaluation/pred_models/gate.py` (reuses `reva.models.tournament.dm_hln_test`, `holm_rejections`) |
| Development stage, protocol, locked holdout (opened once, ledger), registration, DB manifest | `src/reva/evaluation/pred_models/service.py`, CLI `src/reva/cli/pred_models.py` |
| Issuance of registered models in the registry (supplementary sealed batches) | `src/reva/evaluation/pred_models/issue.py`; hooks: `registry/forecasts.py::pred_models()`, `registry/service.py` (issue action), `registry/read.py` (dedup, merged forecasts) |
| Evidence | `registry/forecasts/pred_models/{development.json, protocol.yaml, holdout.json, holdout_ledger.yaml, registered_models.yaml}` |
| Model entries (16, `candidate`) | `registry/models/methods.yaml` (seeded in the dev DB) |
| Tests | `tests/unit/forecasting/test_pred_models.py` (15: estimators on synthetic truth, conformal coverage, no-look-ahead of derived errors, later vintages never change a case, holdout opens once, issuance) |

No new dependency (numpy/scipy/polars only), no migration, no network fetch (no raw archive: 0 MB).

## 3. Design (fixed before the holdout)

* **Tasks.** `weo`: real GDP growth, CPI inflation, unemployment, all WEO countries with an ISO3, origins 1 Jun /
  1 Dec 2008–2025, h0 (current year) and h1 (next year); outcome = first WEO vintage on/after 1 Oct of the next year.
  `us`: real GDP growth and GDP-price inflation (annualised q/q) and the unemployment rate, origins = SPF release
  dates (1990Q2+), SPF horizons 2 (current quarter) … 6; models see the RTDSM vintage in force at the release
  (REVA's conservative rule: one quarter staler than the panellists' information — a handicap kept on purpose).
  `g7`: GDP q/q, CPI y/y of the quarterly mean, unemployment (quarterly mean), origins = OECD MEI edition
  availability, k0 (the origin's quarter) … k3; transformations inside each edition, never across (base years).
* **Benchmarks.** WEO / SPF medians (point; loss AE); for densities a naive Gaussian around the WEO/SPF point with
  the persistence spread (`benchmark.imf_weo_gauss`, `benchmark.spf_gauss`; loss CRPS); persistence; registry AR.
* **Splits.** Development before 2019-01-01 (weo), 2013-01-01 (us), 2016-01-01 (g7); holdout after.
* **Tests.** Loss differential averaged across series within each origin, DM with HLN correction (lag 1–2 for
  annual semi-annual origins, h for quarterly), one-sided; Holm within each (task, candidate, benchmark, loss)
  family in development; **Holm over all holdout hypotheses jointly**; density winners must also cover 70–90 % at
  the 80 % level on the holdout. Hyper-parameters fixed in advance (Minnesota λ = 0.2/0.5/1, p = 4; expanding
  windows; ≥ 15 past errors for conformal).
* **Ineligible.** `forecast.dfm_nowcast_x` (adds final-vintage industrial production and market rates with stated
  publication lags) is pseudo-real-time: reported, never registered.

## 4. Development stage (origins before the split; full tables in the appendix)

| task | scored cases | origins | comparisons tested | Holm-rejected (eligible) |
|---|---|---|---|---|
| weo | 20,196 | 22 (2008-06 … 2018-12) | 60 | 14 |
| us | 1,350 | 90 (1990-09 … 2012-11) | 150 | 16 |
| g7 | 5,712 | 68 (1999-03 … 2015-12) | 102 | 0 |

Reading: (1) WEO and SPF points are never beaten (WEO GDP h0: AR ×1.99, WEO+AR ×1.34; h1 ×1.19 / ×1.04; SPF current
quarter GDP: AR ×1.36 on common cases). (2) REVA's direct AR / BVAR beat persistence on CRPS (WEO panel, US). (3) The
G7 DFM nowcast improves on persistence (CRPS ×0.81, p = 0.043) and on the registry AR (×0.97) but not significantly
after Holm; the richer pseudo-real-time DFM-X is no better. (4) The conformal density around the WEO/SPF points beats
the naive Gaussian by a wide margin (GDP h0 ×0.66, SPF GDP current quarter ×0.73).

## 5. Locked holdout (opened once, 2026-10-17; ledger `holdout_ledger.yaml`, `holdout.json` sha256 `f73d8afa…`)


Holdout cases: weo 12,054 (14 origins 2019-06 … 2025-12), us 775 (54 surveys 2013-02 … 2026-05), g7 3,298 (42
editions 2016-03 … 2026-06). Only the 30 frozen hypotheses can lead to registration (Holm over all 30, α = 0.05):

| # | task | variable | horizon | candidate | benchmark | loss | dev rel. / p | holdout pairs / origins | holdout rel. | holdout p | Holm (m=30) | cov80 | confirmed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | weo | cpi_inflation | h0 | forecast.weo_conformal | benchmark.imf_weo_gauss | crps | 0.943 / 2.3e-18 | 2609 / 14 | 0.996 | 0.19 | — | 0.70 | no |
| 2 | weo | cpi_inflation | h1 | forecast.weo_conformal | benchmark.imf_weo_gauss | crps | 0.993 / 8.6e-07 | 2222 / 12 | 0.993 | 6.9e-05 | reject | 0.61 | no |
| 3 | weo | real_gdp_growth | h0 | forecast.weo_conformal | benchmark.imf_weo_gauss | crps | 0.663 / 1.8e-08 | 2502 / 14 | 0.765 | 0.00053 | reject | 0.75 | **yes** |
| 4 | weo | unemployment_rate | h0 | forecast.weo_conformal | benchmark.imf_weo_gauss | crps | 0.914 / 9.4e-05 | 1387 / 14 | 0.919 | 0.0054 | — | 0.74 | no |
| 5 | weo | cpi_inflation | h0 | forecast.weo_bias_corrected | benchmark.imf_weo_gauss | crps | 0.946 / 5.8e-14 | 2609 / 14 | 0.997 | 0.24 | — | 0.71 | no |
| 6 | weo | cpi_inflation | h1 | forecast.weo_bias_corrected | benchmark.imf_weo_gauss | crps | 0.993 / 1.9e-07 | 2222 / 12 | 0.994 | 3.7e-07 | reject | 0.62 | no |
| 7 | weo | real_gdp_growth | h0 | forecast.weo_bias_corrected | benchmark.imf_weo_gauss | crps | 0.654 / 6.5e-11 | 2502 / 14 | 0.763 | 0.00039 | reject | 0.75 | **yes** |
| 8 | weo | unemployment_rate | h0 | forecast.weo_bias_corrected | benchmark.imf_weo_gauss | crps | 0.920 / 0.00014 | 1387 / 14 | 0.924 | 0.0064 | — | 0.74 | no |
| 9 | weo | cpi_inflation | h0 | forecast.direct_ar | benchmark.persistence | crps | 0.839 / 1.3e-07 | 2589 / 14 | 1398.630 | 0.82 | — | 0.85 | no |
| 10 | weo | cpi_inflation | h1 | forecast.direct_ar | benchmark.persistence | crps | 0.830 / 8.8e-07 | 2210 / 12 | 2524.820 | 0.79 | — | 0.81 | no |
| 11 | weo | real_gdp_growth | h0 | forecast.direct_ar | benchmark.persistence | crps | 0.888 / 0.0055 | 2482 / 14 | 0.779 | 0.078 | — | 0.77 | no |
| 12 | weo | real_gdp_growth | h1 | forecast.direct_ar | benchmark.persistence | crps | 0.809 / 1e-05 | 2100 / 12 | 0.723 | 0.076 | — | 0.76 | no |
| 13 | weo | real_gdp_growth | h0 | forecast.weo_conformal | benchmark.persistence | crps | 0.451 / 9.2e-07 | 2502 / 14 | 0.359 | 0.0044 | — | 0.75 | no |
| 14 | weo | unemployment_rate | h0 | forecast.weo_conformal | benchmark.persistence | crps | 0.782 / 0.0012 | 1387 / 14 | 0.834 | 0.041 | — | 0.74 | no |
| 15 | us | real_gdp_growth | spf2 | forecast.spf_conformal | benchmark.spf_gauss | crps | 0.726 / 2e-14 | 54 / 54 | 0.914 | 1.8e-05 | reject | 0.89 | **yes** |
| 16 | us | real_gdp_growth | spf3 | forecast.spf_conformal | benchmark.spf_gauss | crps | 0.818 / 2.5e-05 | 53 / 53 | 0.997 | 0.46 | — | 0.91 | no |
| 17 | us | unemployment_rate | spf2 | forecast.spf_conformal | benchmark.spf_gauss | crps | 0.465 / 2e-15 | 53 / 53 | 0.688 | 8.5e-07 | reject | 0.94 | no |
| 18 | us | unemployment_rate | spf3 | forecast.spf_conformal | benchmark.spf_gauss | crps | 0.665 / 0.00053 | 52 / 52 | 0.952 | 0.15 | — | 0.92 | no |
| 19 | us | real_gdp_growth | spf6 | forecast.bvar_minnesota | benchmark.persistence | crps | 0.772 / 0.0037 | 50 / 50 | 0.583 | 0.095 | — | 0.90 | no |
| 20 | us | unemployment_rate | spf2 | forecast.bvar_minnesota | benchmark.persistence | crps | 0.781 / 0.0013 | 53 / 53 | 1.016 | 0.58 | — | 0.91 | no |
| 21 | us | unemployment_rate | spf3 | forecast.bvar_minnesota | benchmark.persistence | crps | 0.804 / 0.0004 | 52 / 52 | 0.991 | 0.45 | — | 0.81 | no |
| 22 | us | unemployment_rate | spf4 | forecast.bvar_minnesota | benchmark.persistence | crps | 0.815 / 0.00015 | 51 / 51 | 0.961 | 0.28 | — | 0.76 | no |
| 23 | us | unemployment_rate | spf5 | forecast.bvar_minnesota | benchmark.persistence | crps | 0.827 / 0.00081 | 50 / 50 | 0.929 | 0.16 | — | 0.80 | no |
| 24 | us | unemployment_rate | spf6 | forecast.bvar_minnesota | benchmark.persistence | crps | 0.831 / 0.0019 | 49 / 49 | 0.910 | 0.16 | — | 0.73 | no |
| 25 | us | real_gdp_growth | spf2 | forecast.direct_ar | benchmark.persistence | crps | 0.802 / 0.0025 | 54 / 54 | 0.664 | 0.073 | — | 0.93 | no |
| 26 | us | real_gdp_growth | spf4 | forecast.direct_ar | benchmark.persistence | crps | 0.750 / 0.0031 | 52 / 52 | 0.625 | 0.1 | — | 0.90 | no |
| 27 | us | real_gdp_growth | spf5 | forecast.direct_ar | benchmark.persistence | crps | 0.718 / 8.2e-05 | 51 / 51 | 0.611 | 0.088 | — | 0.90 | no |
| 28 | us | real_gdp_growth | spf6 | forecast.direct_ar | benchmark.persistence | crps | 0.702 / 9.6e-05 | 50 / 50 | 0.618 | 0.08 | — | 0.86 | no |
| 29 | us | real_gdp_growth | spf5 | forecast.pool_bvar_ar | benchmark.persistence | crps | 0.740 / 5.4e-05 | 51 / 51 | 0.615 | 0.092 | — | 0.90 | no |
| 30 | us | real_gdp_growth | spf6 | forecast.pool_bvar_ar | benchmark.persistence | crps | 0.719 / 2.7e-05 | 50 / 50 | 0.594 | 0.088 | — | 0.90 | no |

Why the others failed: the holdout contains 2020–2022 (COVID collapse/rebound, the 2021–23 inflation surge), where
every spread estimated from earlier errors is too narrow or the gain disappears (WEO CPI h0: −0.4 %; direct AR vs
persistence for CPI: hyperinflation cases make the ratio meaningless, ×1,399); the US BVAR/AR gains over persistence
(×0.58–0.66) are large but not significant with ≈ 50 quarterly origins.

Descriptive holdout results that were **not** hypotheses (cannot be registered under this protocol; candidates for
the next protocol, a new model version, or prospective confirmation): WEO bias correction vs WEO on unemployment h0
(AE ×0.979, p = 0.0001); G7 direct AR vs persistence for GDP k0 (CRPS ×0.92, p < 0.001); G7 BVAR vs persistence
(GDP k0 CRPS ×0.73, p = 0.10). The G7 DFM nowcast, promising in development (×0.81 vs persistence), is no better than
persistence on the holdout (CRPS ×1.007, mostly 2020) but better than the registry AR (×0.90, p = 0.039, not a
hypothesis).

## 6. Registered forecasters (`registry/forecasts/pred_models/registered_models.yaml`)

| model | scope (task / variable / horizon) | benchmark beaten (holdout) | registry family |
|---|---|---|---|
| `forecast.weo_conformal@1.0.0` | weo / real GDP growth / h0 (current year) | naive Gaussian around WEO: CRPS ×0.765, p = 0.0005, cov80 0.75 | `weo.gdp_growth`, horizon 0 |
| `forecast.weo_bias_corrected@1.0.0` | weo / real GDP growth / h0 | same: CRPS ×0.763, p = 0.0004, cov80 0.75 | `weo.gdp_growth`, horizon 0 |
| `forecast.spf_conformal@1.0.0` | us / real GDP growth / SPF horizon 2 (current quarter) | naive Gaussian around SPF: CRPS ×0.914, p = 2e-5, cov80 0.89 | none yet (awaiting a US quarterly GDP family) |

*FIX7:* "benchmark beaten" above is the weak N(point, persistence spread) benchmark (QA-PRED X1); against the fair
error-fan benchmark none of the three wins on development (§12). Status: WEO densities on probation (issue with the
fair benchmark `forecast.weo_error_fan` sealed next to them; gate `issue.fair_gate`); SPF density issues nothing.

What these models are: **REVA's calibrated uncertainty around the IMF's / SPF panel's point forecast** (points are
the IMF's and the SPF's, attributed; the WEO bias correction moves the point by a median past error, AE ×0.999 —
equal to the WEO). They do not make REVA a better point forecaster than the IMF or the SPF.

## 7. Issuance in the registry

`registered_models.yaml` → `reva.evaluation.pred_models.issue.registry_models()` → `registry.forecasts.all_models()`.
`reva forecasts issue` (and the weekly `forecast_registry:issue` job) now also calls `issue_supplements`: for the
sealed, open questions of the current cycle whose family/horizon is a registered scope, a **supplementary sealed
batch** (same question records, same pinned table versions as the original batch, `supplement_of` = original seal,
new `issued_at`, new sha256 seal) with the registered models' forecasts; resolution/scoring reuse `resolve_due`
unchanged; `read.questions` lists each question once and `read.question` merges the supplementary forecasts. Inputs at
issue: the WEO projection of the pinned `atlas.imf.weo` version and the normalised WEO errors of the vintage
backtest whose outcome was published at `issued_at`. From the next cycle (2027-Q1) the same path runs on the new
questions.

**Issued (dev DB, origin live, cycle 2026-Q4, 2026-10-17 21:04:41 UTC):** supplementary batch
`a8b7a89d055d983df01755ec600a72b10920f2465042e485617b0a37dc514bf7` (supplement of batch 1
`96c6ad48…`), 19 `weo.gdp_growth` questions (G20+, target year 2026), 38 sealed forecasts (2 models × 19), each with
6,543 past normalised WEO errors published by the issue time. Examples (80 % central interval, % real GDP growth
2026): USA 2.33 [0.98, 3.84]; DEU 1.32 [−0.49, 3.35]; CHN 4.47 [3.28, 5.81] (conformal); bias-corrected points 2.36 /
1.36 / 4.50. Expected resolution: first WEO vintage publishing 2026 as an actual (≈ April 2027). The registered SPF
density issues nothing (no family; `awaiting_family` in the job result).

## 8. Validated chains for PRED-ENGINE

None recorded (`registry/predict/validated_chains.yaml` unchanged). A chain needs a back-test record **and** a
calibration record; the engine requires the calibration from the prospective registry (`judge_requirements` in
`src/reva/predict/engine.py`), and no registry question has resolved yet (first WEO h0 resolutions ≈ April 2027).
Moreover no PRED-ENGINE chain uses these forecasters (its chains are effect-estimate/plugin hops; its baseline
is persistence). When the registered WEO density has resolved questions with coverage within 70–90 %, the judge
can append an entry for a chain that uses it (back-test record = `holdout.json` sha256 `f73d8afa…`).

## 9. Honesty notes and limits

* Real-time only through PRED-VINTAGE's conservative availability rules (never earlier than true publication). The
  US comparison handicaps REVA's models by one quarter of information versus the SPF panellists (stated).
* WEO DBnomics vintages carry the release month only; WEO 2011-09 missing; outcome = first vintage on/after 1 Oct
  of the following year (sometimes a later one).
* Log scores of sample-based densities use a Gaussian KDE (Silverman); they punish thin sample tails heavily (BVAR,
  conformal at long horizons) — CRPS is the primary loss.
* Cross-country WEO panels: means of CPI errors are dominated by hyperinflations (ratios of 100+ are not
  meaningful); medians are in the tables; the tests average differentials within origins.
* Holdout used once; the ledger refuses re-opening the same protocol or re-testing a hypothesis with the same model
  version. Tuning after this point requires a new model version and a fresh holdout (or prospective evidence).
* Licences: IMF WEO (IMF terms, attribution, not open); Philadelphia Fed RTDSM/SPF "all rights reserved" (scores
  and model outputs kept internal — owner decision 3 of PRED-VINTAGE applies); OECD (CC BY 4.0); KEI/IMF PI used only
  by the ineligible DFM-X.
* Data not used / missing: ALFRED, IMF Historical WEO forecasts file, OECD EO vintages (all blocked or undated, see
  `docs/dev/v5_pred_vintage.md`); OECD MEI real-time only for G7 and 3 variables, so the DFM has 2 real-time
  indicators (unemployment change, CPI y/y) — a richer real-time monthly panel (ALFRED, full OECD STES) is the most
  promising route to a nowcast that beats the AR.

## 10. Owner decisions

1. Publish the WEO/SPF conformal densities (they are REVA-made intervals around IMF / SPF points; Philadelphia Fed
   data carry no redistribution licence)?
2. Add a US quarterly real-GDP-growth question family (resolved by the RTDSM / BEA first release) so the registered
   SPF density can issue; it needs the SPF 2026Q4 release (mid-November 2026) as input.
3. FRED API key (ALFRED) to widen the real-time monthly panel for nowcasting.

## 11. Commands

```bash
uv run pytest -q tests/unit/forecasting/test_pred_models.py
uv run python -m reva.cli.pred_models dev                 # development stage -> protocol.yaml (commit it!)
uv run python -m reva.cli.pred_models holdout             # opens the locked holdout ONCE (ledger)
flock /tmp/reva-devdb.lock uv run python -m reva.cli.pred_models holdout --store   # (with DB manifest)
flock /tmp/reva-devdb.lock uv run reva seed                          # model versions (done)
flock /tmp/reva-devdb.lock uv run reva forecasts issue               # baselines + registered-model supplements
flock /tmp/reva-devdb.lock uv run reva forecasts resolve
```

## 12. FIX7 addendum — fair density benchmark (development only), overlap, CPI, scoring vintage

**Fair benchmark** (`src/reva/evaluation/pred_models/fair_benchmark.py`, outside the protocol's `SOURCE_FILES`, so
the protocol code digest is unchanged): `own` = N(official point, RMS of that series' own past official errors at
the same horizon with `outcome_known_at <= origin`, ≥ 8 errors); `pooled` = N(official point, series scale × RMS of
the pooled normalised past official errors, ≥ 15) — the Gaussian twin of the conformal density. Re-run with
`uv run python -m reva.cli.pred_models fair-dev` on **development origins only** (`build_cases(stage="development")`
never forecasts a holdout origin); result `registry/forecasts/pred_models/fair_benchmark_development.json`. CRPS
ratios candidate / benchmark, one-sided DM-HLN by origin, Holm per benchmark over the task's 3 density candidates ×
cells (WEO m = 18, US m = 45):

| scope | candidate | vs old (persistence spread) | vs fair `own` error fan | vs fair `pooled` error fan |
|---|---|---|---|---|
| WEO GDP h0 | `weo_conformal` | ×0.663, p 2e-8, Holm ✓ (19 origins) | ×0.985, p 0.033, Holm ✗ (13 origins) | ×0.924, p 0.049, Holm ✗ |
| WEO GDP h0 | `weo_bias_corrected` | ×0.654, p 7e-11, Holm ✓ | ×0.992, p 0.18, Holm ✗ | ×0.912, p 0.002, Holm ✓ |
| US GDP current quarter | `spf_conformal` | ×0.726, p 2e-14, Holm ✓ (74 origins) | **×1.018**, p 0.70 (worse) | ×1.009, p 0.62 |
| same, purged (X5) | `weo_conformal` / `weo_bias_corrected` / `spf_conformal` | ×0.678 / ×0.668 / ×0.720 | ×0.988 (p 0.095) / ×0.996 / ×1.015 | ×0.934 / ×0.921 (p 0.006, Holm ✗) / ×1.007 |

80 % coverage on development, WEO GDP h0: old benchmark 0.98, fair `own` 0.87, `pooled` 0.92, conformal 0.82; US
current quarter: old 1.00, `own` 0.79, conformal 0.74. **Reading:** the registered densities are about as good as the
standard error-based fan of the official forecaster; the large holdout gain was mostly the strawman's over-wide
spread. Their only remaining edge is shape/scale pooling across countries (the bias-corrected WEO variant beats the
pooled Gaussian fan on the unpurged development sample, not after the purge) — not established.

**Issuance rule (registered_models.yaml, `issuance`):** the two WEO densities are on `probation`; the fair benchmark
`forecast.weo_error_fan@1.0.0` (methods.yaml) is issued in the same supplementary batches on the same questions;
once ≥ 30 common questions resolve, `issue.fair_gate` (CRPS, one-sided country-clustered DM, p < 0.05) decides
`confirmed` or `suspended`, appended to `registry/forecasts/pred_models/issuance_decisions.yaml`; `suspended` models
issue nothing (`issue.issuing_scopes`). The SPF density (no registry family) must beat `benchmark.spf_errfan`
prospectively before any issuance.

**X5 — dev/holdout target overlap (documented, not re-tested):** the split is on the origin date only, so
development cases can target periods whose outcomes were published after the split (dev origin 2018-12 h1 targets
2019, also targeted by the holdout's 2019-06/12 h0 cases; US dev SPF h6 from 2012 targets 2013–14). The locked
holdout is spent and is not re-opened; any future protocol must purge development cases with
`outcome_known_at >= split` (`fair_benchmark.compare(purge=True)` does it; the purged rows are in the table above).

**X6 — pooled CPI:** pooled cross-country inflation means are dominated by hyperinflation (direct AR CPI CRPS
1,399× persistence on the holdout; WEO CPI h0 MAE 10× persistence while its median AE is 0.38×): quote medians
(`gate.summary.median_ae`; `vintage_backtests` `relative_median_ae` headline) or per-country ratios, never a pooled
mean ratio, for every CPI cell of this report.

**X7 — WEO scoring vintage:** WEO cases are scored against the target year in the first WEO vintage available on/after
1 October of the next year — the IMF's own estimate (for many low-capacity countries an IMF staff estimate), which
flatters the WEO and every WEO-wrapped density. An independent-outturn robustness run is not done (owner decision).

**X8 (not changed here):** `realtime.us_cases` still gives REVA's US models the conservative RTDSM vintage (one
quarter staler than the SPF panellists); changing it would change the protocol code digest. It handicaps REVA, it does
not inflate any claim; a future protocol should use the survey-quarter vintage (as `vintage_backtests.spf_backtest`
now does).

## Appendix A — development stage, full tables (summaries and every tested comparison)


#### weo — development (20196 scored cases, 22 origins ['2008-06-01T00:00:00+00:00', '2018-12-01T00:00:00+00:00'])

| variable | horizon | model | n | MAE | CRPS | log | cov80 |
|---|---|---|---|---|---|---|---|
| cpi_inflation | h0 | benchmark.imf_weo | 4071 | 334.886 | n. a. | n. a. | n. a. |
| cpi_inflation | h0 | benchmark.imf_weo_gauss | 4029 | 338.240 | 366.027 | -10739.400 | 0.98 |
| cpi_inflation | h0 | benchmark.persistence | 4029 | 35.366 | 62.060 | -55.825 | 0.93 |
| cpi_inflation | h0 | benchmark.registry_ar | 3894 | 18.993 | 21.536 | -3.330 | 0.90 |
| cpi_inflation | h0 | forecast.combo_weo_ar | 3385 | 210.348 | 214.556 | -4.589 | 0.82 |
| cpi_inflation | h0 | forecast.direct_ar | 3900 | 51.313 | 53.674 | -13.202 | 0.91 |
| cpi_inflation | h0 | forecast.weo_bias_corrected | 3499 | 389.949 | 393.966 | -2.400 | 0.86 |
| cpi_inflation | h0 | forecast.weo_conformal | 3499 | 389.142 | 392.862 | -2.347 | 0.83 |
| cpi_inflation | h1 | benchmark.imf_weo | 4069 | 2488.690 | n. a. | n. a. | n. a. |
| cpi_inflation | h1 | benchmark.imf_weo_gauss | 4013 | 2523.050 | 2553.920 | -441448.000 | 0.97 |
| cpi_inflation | h1 | benchmark.persistence | 4013 | 46.039 | 76.203 | -608.128 | 0.94 |
| cpi_inflation | h1 | benchmark.registry_ar | 3892 | 24.382 | 24.142 | -3.770 | 0.92 |
| cpi_inflation | h1 | forecast.combo_weo_ar | 3035 | 1699.700 | 1703.780 | -4.776 | 0.79 |
| cpi_inflation | h1 | forecast.direct_ar | 3898 | 65.184 | 65.051 | -145.318 | 0.91 |
| cpi_inflation | h1 | forecast.weo_bias_corrected | 3139 | 3228.180 | 3231.760 | -2.927 | 0.85 |
| cpi_inflation | h1 | forecast.weo_conformal | 3139 | 3224.720 | 3231.930 | -2.914 | 0.83 |
| real_gdp_growth | h0 | benchmark.imf_weo | 4083 | 1.349 | n. a. | n. a. | n. a. |
| real_gdp_growth | h0 | benchmark.imf_weo_gauss | 4041 | 1.329 | 1.618 | -2.567 | 0.98 |
| real_gdp_growth | h0 | benchmark.persistence | 4041 | 2.697 | 2.398 | -2.929 | 0.89 |
| real_gdp_growth | h0 | benchmark.registry_ar | 3919 | 2.567 | 2.058 | -2.821 | 0.87 |
| real_gdp_growth | h0 | forecast.combo_weo_ar | 3412 | 1.682 | 1.412 | -2.913 | 0.84 |
| real_gdp_growth | h0 | forecast.direct_ar | 3927 | 2.610 | 2.102 | -2.773 | 0.88 |
| real_gdp_growth | h0 | forecast.weo_bias_corrected | 3510 | 1.318 | 1.052 | -2.542 | 0.83 |
| real_gdp_growth | h0 | forecast.weo_conformal | 3510 | 1.268 | 1.066 | -2.557 | 0.82 |
| real_gdp_growth | h1 | benchmark.imf_weo | 4080 | 2.417 | n. a. | n. a. | n. a. |
| real_gdp_growth | h1 | benchmark.imf_weo_gauss | 4026 | 2.338 | 2.260 | -2.901 | 0.94 |
| real_gdp_growth | h1 | benchmark.persistence | 4026 | 3.182 | 2.748 | -3.087 | 0.89 |
| real_gdp_growth | h1 | benchmark.registry_ar | 3916 | 2.725 | 2.167 | -2.948 | 0.88 |
| real_gdp_growth | h1 | forecast.combo_weo_ar | 3064 | 2.084 | 1.986 | -2.714 | 0.84 |
| real_gdp_growth | h1 | forecast.direct_ar | 3924 | 2.754 | 2.192 | -2.966 | 0.89 |
| real_gdp_growth | h1 | forecast.weo_bias_corrected | 3150 | 2.506 | 1.896 | -2.702 | 0.90 |
| real_gdp_growth | h1 | forecast.weo_conformal | 3150 | 2.021 | 2.028 | -2.738 | 0.85 |
| unemployment_rate | h0 | benchmark.imf_weo | 1956 | 0.714 | n. a. | n. a. | n. a. |
| unemployment_rate | h0 | benchmark.imf_weo_gauss | 1945 | 0.717 | 0.660 | -2.357 | 0.91 |
| unemployment_rate | h0 | benchmark.persistence | 1945 | 0.966 | 0.787 | -2.602 | 0.85 |
| unemployment_rate | h0 | benchmark.registry_ar | 1858 | 0.944 | 0.741 | -2.664 | 0.82 |
| unemployment_rate | h0 | forecast.combo_weo_ar | 1801 | 0.809 | 0.660 | -2.449 | 0.78 |
| unemployment_rate | h0 | forecast.direct_ar | 1896 | 0.995 | 0.782 | -2.560 | 0.83 |
| unemployment_rate | h0 | forecast.weo_bias_corrected | 1850 | 0.733 | 0.620 | -3.318 | 0.81 |
| unemployment_rate | h0 | forecast.weo_conformal | 1850 | 0.731 | 0.616 | -3.456 | 0.80 |
| unemployment_rate | h1 | benchmark.imf_weo | 1937 | 1.210 | n. a. | n. a. | n. a. |
| unemployment_rate | h1 | benchmark.imf_weo_gauss | 1922 | 1.217 | 1.011 | -2.264 | 0.87 |
| unemployment_rate | h1 | benchmark.persistence | 1922 | 1.512 | 1.184 | -2.463 | 0.84 |
| unemployment_rate | h1 | benchmark.registry_ar | 1839 | 1.514 | 1.144 | -2.510 | 0.78 |
| unemployment_rate | h1 | forecast.combo_weo_ar | 1718 | 1.343 | 1.265 | -2.444 | 0.70 |
| unemployment_rate | h1 | forecast.direct_ar | 1879 | 1.671 | 1.275 | -2.579 | 0.77 |
| unemployment_rate | h1 | forecast.weo_bias_corrected | 1765 | 1.368 | 1.043 | -2.168 | 0.79 |
| unemployment_rate | h1 | forecast.weo_conformal | 1765 | 1.196 | 1.094 | -2.206 | 0.75 |

| variable | horizon | candidate | benchmark | loss | n pairs / origins | relative | p (better) | Holm |
|---|---|---|---|---|---|---|---|---|
| cpi_inflation | h0 | forecast.direct_ar | benchmark.imf_weo | ae | 3900 / 22 | 0.147 | 0.1902 | no |
| cpi_inflation | h1 | forecast.direct_ar | benchmark.imf_weo | ae | 3898 / 22 | 0.025 | 0.1880 | no |
| real_gdp_growth | h0 | forecast.direct_ar | benchmark.imf_weo | ae | 3927 / 22 | 1.986 | 1.0000 | no |
| real_gdp_growth | h1 | forecast.direct_ar | benchmark.imf_weo | ae | 3924 / 22 | 1.191 | 1.0000 | no |
| unemployment_rate | h0 | forecast.direct_ar | benchmark.imf_weo | ae | 1896 / 22 | 1.389 | 0.9989 | no |
| unemployment_rate | h1 | forecast.direct_ar | benchmark.imf_weo | ae | 1879 / 22 | 1.376 | 1.0000 | no |
| cpi_inflation | h0 | forecast.combo_weo_ar | benchmark.imf_weo | ae | 3385 / 19 | 0.523 | 0.1886 | no |
| cpi_inflation | h1 | forecast.combo_weo_ar | benchmark.imf_weo | ae | 3035 / 17 | 0.510 | 0.1952 | no |
| real_gdp_growth | h0 | forecast.combo_weo_ar | benchmark.imf_weo | ae | 3412 / 19 | 1.342 | 1.0000 | no |
| real_gdp_growth | h1 | forecast.combo_weo_ar | benchmark.imf_weo | ae | 3064 / 17 | 1.044 | 0.9987 | no |
| unemployment_rate | h0 | forecast.combo_weo_ar | benchmark.imf_weo | ae | 1801 / 19 | 1.107 | 0.9984 | no |
| unemployment_rate | h1 | forecast.combo_weo_ar | benchmark.imf_weo | ae | 1718 / 17 | 1.124 | 1.0000 | no |
| cpi_inflation | h0 | forecast.combo_weo_ar_persist | benchmark.imf_weo | ae | 3385 / 19 | 0.351 | 0.1834 | no |
| cpi_inflation | h1 | forecast.combo_weo_ar_persist | benchmark.imf_weo | ae | 3035 / 17 | 0.344 | 0.1945 | no |
| real_gdp_growth | h0 | forecast.combo_weo_ar_persist | benchmark.imf_weo | ae | 3412 / 19 | 1.433 | 0.9971 | no |
| real_gdp_growth | h1 | forecast.combo_weo_ar_persist | benchmark.imf_weo | ae | 3064 / 17 | 1.038 | 0.8760 | no |
| unemployment_rate | h0 | forecast.combo_weo_ar_persist | benchmark.imf_weo | ae | 1801 / 19 | 1.141 | 0.9988 | no |
| unemployment_rate | h1 | forecast.combo_weo_ar_persist | benchmark.imf_weo | ae | 1718 / 17 | 1.132 | 0.9995 | no |
| cpi_inflation | h0 | forecast.weo_bias_corrected | benchmark.imf_weo | ae | 3499 / 19 | 1.002 | 0.8447 | no |
| cpi_inflation | h1 | forecast.weo_bias_corrected | benchmark.imf_weo | ae | 3139 / 17 | 1.001 | 0.8316 | no |
| real_gdp_growth | h0 | forecast.weo_bias_corrected | benchmark.imf_weo | ae | 3510 / 19 | 1.039 | 0.8573 | no |
| real_gdp_growth | h1 | forecast.weo_bias_corrected | benchmark.imf_weo | ae | 3150 / 17 | 1.240 | 0.8063 | no |
| unemployment_rate | h0 | forecast.weo_bias_corrected | benchmark.imf_weo | ae | 1850 / 19 | 1.003 | 0.6706 | no |
| unemployment_rate | h1 | forecast.weo_bias_corrected | benchmark.imf_weo | ae | 1765 / 17 | 1.144 | 0.8293 | no |
| cpi_inflation | h0 | forecast.weo_conformal | benchmark.imf_weo_gauss | crps | 3499 / 19 | 0.943 | 0.0000 | yes |
| cpi_inflation | h1 | forecast.weo_conformal | benchmark.imf_weo_gauss | crps | 3130 / 17 | 0.993 | 0.0000 | yes |
| real_gdp_growth | h0 | forecast.weo_conformal | benchmark.imf_weo_gauss | crps | 3510 / 19 | 0.663 | 0.0000 | yes |
| real_gdp_growth | h1 | forecast.weo_conformal | benchmark.imf_weo_gauss | crps | 3145 / 17 | 0.970 | 0.4528 | no |
| unemployment_rate | h0 | forecast.weo_conformal | benchmark.imf_weo_gauss | crps | 1850 / 19 | 0.914 | 0.0001 | yes |
| unemployment_rate | h1 | forecast.weo_conformal | benchmark.imf_weo_gauss | crps | 1761 / 17 | 1.086 | 0.7787 | no |
| cpi_inflation | h0 | forecast.weo_bias_corrected | benchmark.imf_weo_gauss | crps | 3499 / 19 | 0.946 | 0.0000 | yes |
| cpi_inflation | h1 | forecast.weo_bias_corrected | benchmark.imf_weo_gauss | crps | 3130 / 17 | 0.993 | 0.0000 | yes |
| real_gdp_growth | h0 | forecast.weo_bias_corrected | benchmark.imf_weo_gauss | crps | 3510 / 19 | 0.654 | 0.0000 | yes |
| real_gdp_growth | h1 | forecast.weo_bias_corrected | benchmark.imf_weo_gauss | crps | 3145 / 17 | 0.906 | 0.2240 | no |
| unemployment_rate | h0 | forecast.weo_bias_corrected | benchmark.imf_weo_gauss | crps | 1850 / 19 | 0.920 | 0.0001 | yes |
| unemployment_rate | h1 | forecast.weo_bias_corrected | benchmark.imf_weo_gauss | crps | 1761 / 17 | 1.036 | 0.6710 | no |
| cpi_inflation | h0 | forecast.combo_weo_ar | benchmark.imf_weo_gauss | crps | 3385 / 19 | 0.499 | 0.1602 | no |
| cpi_inflation | h1 | forecast.combo_weo_ar | benchmark.imf_weo_gauss | crps | 3035 / 17 | 0.506 | 0.1914 | no |
| real_gdp_growth | h0 | forecast.combo_weo_ar | benchmark.imf_weo_gauss | crps | 3412 / 19 | 0.893 | 0.1423 | no |
| real_gdp_growth | h1 | forecast.combo_weo_ar | benchmark.imf_weo_gauss | crps | 3064 / 17 | 0.959 | 0.4318 | no |
| unemployment_rate | h0 | forecast.combo_weo_ar | benchmark.imf_weo_gauss | crps | 1801 / 19 | 0.978 | 0.4994 | no |
| unemployment_rate | h1 | forecast.combo_weo_ar | benchmark.imf_weo_gauss | crps | 1718 / 17 | 1.255 | 0.9076 | no |
| cpi_inflation | h0 | forecast.direct_ar | benchmark.persistence | crps | 3900 / 22 | 0.839 | 0.0000 | yes |
| cpi_inflation | h1 | forecast.direct_ar | benchmark.persistence | crps | 3898 / 22 | 0.830 | 0.0000 | yes |
| real_gdp_growth | h0 | forecast.direct_ar | benchmark.persistence | crps | 3927 / 22 | 0.888 | 0.0055 | yes |
| real_gdp_growth | h1 | forecast.direct_ar | benchmark.persistence | crps | 3924 / 22 | 0.809 | 0.0000 | yes |
| unemployment_rate | h0 | forecast.direct_ar | benchmark.persistence | crps | 1896 / 22 | 0.986 | 0.2105 | no |
| unemployment_rate | h1 | forecast.direct_ar | benchmark.persistence | crps | 1879 / 22 | 1.070 | 0.8186 | no |
| cpi_inflation | h0 | forecast.direct_ar_conformal | benchmark.persistence | crps | 3385 / 19 | 0.884 | 0.0349 | no |
| cpi_inflation | h1 | forecast.direct_ar_conformal | benchmark.persistence | crps | 3035 / 17 | 0.873 | 0.0109 | no |
| real_gdp_growth | h0 | forecast.direct_ar_conformal | benchmark.persistence | crps | 3412 / 19 | 0.914 | 0.0278 | no |
| real_gdp_growth | h1 | forecast.direct_ar_conformal | benchmark.persistence | crps | 3064 / 17 | 0.910 | 0.2264 | no |
| unemployment_rate | h0 | forecast.direct_ar_conformal | benchmark.persistence | crps | 1801 / 19 | 1.097 | 0.8410 | no |
| unemployment_rate | h1 | forecast.direct_ar_conformal | benchmark.persistence | crps | 1718 / 17 | 1.388 | 0.9371 | no |
| cpi_inflation | h0 | forecast.weo_conformal | benchmark.persistence | crps | 3499 / 19 | 5.932 | 0.8051 | no |
| cpi_inflation | h1 | forecast.weo_conformal | benchmark.persistence | crps | 3130 / 17 | 37.376 | 0.8046 | no |
| real_gdp_growth | h0 | forecast.weo_conformal | benchmark.persistence | crps | 3510 / 19 | 0.451 | 0.0000 | yes |
| real_gdp_growth | h1 | forecast.weo_conformal | benchmark.persistence | crps | 3145 / 17 | 0.815 | 0.0907 | no |
| unemployment_rate | h0 | forecast.weo_conformal | benchmark.persistence | crps | 1850 / 19 | 0.782 | 0.0012 | yes |
| unemployment_rate | h1 | forecast.weo_conformal | benchmark.persistence | crps | 1761 / 17 | 0.952 | 0.3634 | no |

#### us — development (1350 scored cases, 90 origins ['1990-09-01T00:00:00+00:00', '2012-11-10T00:00:00+00:00'])

| variable | horizon | model | n | MAE | CRPS | log | cov80 |
|---|---|---|---|---|---|---|---|
| gdp_price_inflation | spf2 | benchmark.persistence | 90 | 1.047 | 0.738 | -1.715 | 0.86 |
| gdp_price_inflation | spf2 | benchmark.registry_ar | 90 | 0.890 | 0.628 | -1.581 | 0.77 |
| gdp_price_inflation | spf2 | benchmark.spf_gauss | 90 | 0.750 | 0.567 | -1.532 | 0.94 |
| gdp_price_inflation | spf2 | benchmark.spf_median | 90 | 0.750 | n. a. | n. a. | n. a. |
| gdp_price_inflation | spf2 | forecast.bvar_minnesota | 90 | 0.983 | 0.695 | -1.647 | 0.82 |
| gdp_price_inflation | spf2 | forecast.combo_spf_bvar | 74 | 0.794 | 0.535 | -1.456 | 0.73 |
| gdp_price_inflation | spf2 | forecast.direct_ar | 90 | 0.946 | 0.665 | -1.611 | 0.84 |
| gdp_price_inflation | spf2 | forecast.pool_bvar_ar | 90 | 0.953 | 0.675 | -1.628 | 0.86 |
| gdp_price_inflation | spf2 | forecast.spf_conformal | 74 | 0.729 | 0.511 | -1.329 | 0.69 |
| gdp_price_inflation | spf3 | benchmark.persistence | 90 | 0.975 | 0.727 | -1.748 | 0.88 |
| gdp_price_inflation | spf3 | benchmark.registry_ar | 90 | 0.907 | 0.631 | -1.589 | 0.79 |
| gdp_price_inflation | spf3 | benchmark.spf_gauss | 90 | 0.802 | 0.597 | -1.576 | 0.97 |
| gdp_price_inflation | spf3 | benchmark.spf_median | 90 | 0.802 | n. a. | n. a. | n. a. |
| gdp_price_inflation | spf3 | forecast.bvar_minnesota | 90 | 1.078 | 0.756 | -1.750 | 0.89 |
| gdp_price_inflation | spf3 | forecast.combo_spf_bvar | 73 | 0.864 | 0.576 | -1.524 | 0.73 |
| gdp_price_inflation | spf3 | forecast.direct_ar | 90 | 0.976 | 0.688 | -1.641 | 0.93 |
| gdp_price_inflation | spf3 | forecast.pool_bvar_ar | 90 | 1.017 | 0.715 | -1.695 | 0.89 |
| gdp_price_inflation | spf3 | forecast.spf_conformal | 73 | 0.772 | 0.550 | -1.469 | 0.71 |
| gdp_price_inflation | spf4 | benchmark.persistence | 90 | 0.931 | 0.706 | -1.730 | 0.94 |
| gdp_price_inflation | spf4 | benchmark.registry_ar | 90 | 0.957 | 0.671 | -1.665 | 0.79 |
| gdp_price_inflation | spf4 | benchmark.spf_gauss | 90 | 0.819 | 0.620 | -1.625 | 0.96 |
| gdp_price_inflation | spf4 | benchmark.spf_median | 90 | 0.819 | n. a. | n. a. | n. a. |
| gdp_price_inflation | spf4 | forecast.bvar_minnesota | 90 | 1.216 | 0.830 | -1.824 | 0.90 |
| gdp_price_inflation | spf4 | forecast.combo_spf_bvar | 72 | 0.956 | 0.615 | -1.672 | 0.62 |
| gdp_price_inflation | spf4 | forecast.direct_ar | 90 | 1.091 | 0.754 | -1.725 | 0.90 |
| gdp_price_inflation | spf4 | forecast.pool_bvar_ar | 90 | 1.139 | 0.784 | -1.770 | 0.90 |
| gdp_price_inflation | spf4 | forecast.spf_conformal | 72 | 0.790 | 0.561 | -1.541 | 0.67 |
| gdp_price_inflation | spf5 | benchmark.persistence | 90 | 1.010 | 0.777 | -1.822 | 0.91 |
| gdp_price_inflation | spf5 | benchmark.registry_ar | 90 | 1.009 | 0.715 | -1.685 | 0.77 |
| gdp_price_inflation | spf5 | benchmark.spf_gauss | 90 | 0.883 | 0.679 | -1.731 | 0.98 |
| gdp_price_inflation | spf5 | benchmark.spf_median | 90 | 0.883 | n. a. | n. a. | n. a. |
| gdp_price_inflation | spf5 | forecast.bvar_minnesota | 90 | 1.323 | 0.918 | -1.922 | 0.87 |
| gdp_price_inflation | spf5 | forecast.combo_spf_bvar | 71 | 1.002 | 0.677 | -1.757 | 0.61 |
| gdp_price_inflation | spf5 | forecast.direct_ar | 90 | 1.190 | 0.818 | -1.785 | 0.87 |
| gdp_price_inflation | spf5 | forecast.pool_bvar_ar | 90 | 1.246 | 0.858 | -1.855 | 0.87 |
| gdp_price_inflation | spf5 | forecast.spf_conformal | 71 | 0.835 | 0.620 | -1.733 | 0.61 |
| gdp_price_inflation | spf6 | benchmark.persistence | 90 | 1.122 | 0.832 | -1.879 | 0.92 |
| gdp_price_inflation | spf6 | benchmark.registry_ar | 90 | 1.072 | 0.762 | -1.731 | 0.73 |
| gdp_price_inflation | spf6 | benchmark.spf_gauss | 90 | 0.944 | 0.722 | -1.788 | 0.97 |
| gdp_price_inflation | spf6 | benchmark.spf_median | 90 | 0.944 | n. a. | n. a. | n. a. |
| gdp_price_inflation | spf6 | forecast.bvar_minnesota | 90 | 1.452 | 1.000 | -1.989 | 0.83 |
| gdp_price_inflation | spf6 | forecast.combo_spf_bvar | 70 | 1.084 | 0.718 | -1.982 | 0.61 |
| gdp_price_inflation | spf6 | forecast.direct_ar | 90 | 1.332 | 0.900 | -1.867 | 0.86 |
| gdp_price_inflation | spf6 | forecast.pool_bvar_ar | 90 | 1.389 | 0.940 | -1.928 | 0.88 |
| gdp_price_inflation | spf6 | forecast.spf_conformal | 70 | 0.880 | 0.657 | -1.890 | 0.61 |
| real_gdp_growth | spf2 | benchmark.persistence | 90 | 1.921 | 1.586 | -2.578 | 0.93 |
| real_gdp_growth | spf2 | benchmark.registry_ar | 90 | 1.626 | 1.261 | -2.335 | 0.91 |
| real_gdp_growth | spf2 | benchmark.spf_gauss | 90 | 1.164 | 1.193 | -2.428 | 1.00 |
| real_gdp_growth | spf2 | benchmark.spf_median | 90 | 1.164 | n. a. | n. a. | n. a. |
| real_gdp_growth | spf2 | forecast.bvar_minnesota | 90 | 1.941 | 1.414 | -2.417 | 0.90 |
| real_gdp_growth | spf2 | forecast.combo_spf_bvar | 74 | 1.420 | 1.039 | -2.087 | 0.69 |
| real_gdp_growth | spf2 | forecast.direct_ar | 90 | 1.578 | 1.272 | -2.356 | 0.94 |
| real_gdp_growth | spf2 | forecast.pool_bvar_ar | 90 | 1.721 | 1.313 | -2.393 | 0.94 |
| real_gdp_growth | spf2 | forecast.spf_conformal | 74 | 1.211 | 0.864 | -1.846 | 0.74 |
| real_gdp_growth | spf3 | benchmark.persistence | 90 | 2.078 | 1.682 | -2.654 | 0.96 |
| real_gdp_growth | spf3 | benchmark.registry_ar | 90 | 1.675 | 1.292 | -2.363 | 0.92 |
| real_gdp_growth | spf3 | benchmark.spf_gauss | 90 | 1.425 | 1.367 | -2.530 | 0.99 |
| real_gdp_growth | spf3 | benchmark.spf_median | 90 | 1.425 | n. a. | n. a. | n. a. |
| real_gdp_growth | spf3 | forecast.bvar_minnesota | 90 | 1.981 | 1.455 | -2.449 | 0.90 |
| real_gdp_growth | spf3 | forecast.combo_spf_bvar | 73 | 1.650 | 1.205 | -2.349 | 0.71 |
| real_gdp_growth | spf3 | forecast.direct_ar | 90 | 1.620 | 1.303 | -2.382 | 0.97 |
| real_gdp_growth | spf3 | forecast.pool_bvar_ar | 90 | 1.753 | 1.345 | -2.435 | 0.94 |
| real_gdp_growth | spf3 | forecast.spf_conformal | 73 | 1.496 | 1.121 | -2.195 | 0.66 |
| real_gdp_growth | spf4 | benchmark.persistence | 90 | 2.088 | 1.713 | -2.669 | 0.97 |
| real_gdp_growth | spf4 | benchmark.registry_ar | 90 | 1.614 | 1.252 | -2.344 | 0.92 |
| real_gdp_growth | spf4 | benchmark.spf_gauss | 90 | 1.552 | 1.446 | -2.573 | 0.98 |
| real_gdp_growth | spf4 | benchmark.spf_median | 90 | 1.552 | n. a. | n. a. | n. a. |
| real_gdp_growth | spf4 | forecast.bvar_minnesota | 90 | 1.967 | 1.442 | -2.478 | 0.90 |
| real_gdp_growth | spf4 | forecast.combo_spf_bvar | 72 | 1.759 | 1.303 | -2.583 | 0.65 |
| real_gdp_growth | spf4 | forecast.direct_ar | 90 | 1.583 | 1.285 | -2.377 | 0.96 |
| real_gdp_growth | spf4 | forecast.pool_bvar_ar | 90 | 1.735 | 1.327 | -2.427 | 0.94 |
| real_gdp_growth | spf4 | forecast.spf_conformal | 72 | 1.629 | 1.250 | -2.397 | 0.68 |
| real_gdp_growth | spf5 | benchmark.persistence | 90 | 2.121 | 1.753 | -2.697 | 0.94 |
| real_gdp_growth | spf5 | benchmark.registry_ar | 90 | 1.526 | 1.206 | -2.334 | 0.93 |
| real_gdp_growth | spf5 | benchmark.spf_gauss | 90 | 1.570 | 1.505 | -2.620 | 0.98 |
| real_gdp_growth | spf5 | benchmark.spf_median | 90 | 1.570 | n. a. | n. a. | n. a. |
| real_gdp_growth | spf5 | forecast.bvar_minnesota | 90 | 1.888 | 1.402 | -2.483 | 0.93 |
| real_gdp_growth | spf5 | forecast.combo_spf_bvar | 71 | 1.790 | 1.330 | -2.752 | 0.63 |
| real_gdp_growth | spf5 | forecast.direct_ar | 90 | 1.551 | 1.258 | -2.365 | 0.97 |
| real_gdp_growth | spf5 | forecast.pool_bvar_ar | 90 | 1.698 | 1.297 | -2.418 | 0.96 |
| real_gdp_growth | spf5 | forecast.spf_conformal | 71 | 1.683 | 1.303 | -2.598 | 0.62 |
| real_gdp_growth | spf6 | benchmark.persistence | 90 | 2.194 | 1.787 | -2.708 | 0.96 |
| real_gdp_growth | spf6 | benchmark.registry_ar | 90 | 1.506 | 1.204 | -2.336 | 0.93 |
| real_gdp_growth | spf6 | benchmark.spf_gauss | 90 | 1.542 | 1.496 | -2.615 | 0.98 |
| real_gdp_growth | spf6 | benchmark.spf_median | 90 | 1.542 | n. a. | n. a. | n. a. |
| real_gdp_growth | spf6 | forecast.bvar_minnesota | 90 | 1.824 | 1.380 | -2.454 | 0.91 |
| real_gdp_growth | spf6 | forecast.combo_spf_bvar | 70 | 1.772 | 1.329 | -2.898 | 0.63 |
| real_gdp_growth | spf6 | forecast.direct_ar | 90 | 1.547 | 1.254 | -2.358 | 0.97 |
| real_gdp_growth | spf6 | forecast.pool_bvar_ar | 90 | 1.666 | 1.284 | -2.410 | 0.96 |
| real_gdp_growth | spf6 | forecast.spf_conformal | 70 | 1.656 | 1.303 | -2.761 | 0.64 |
| unemployment_rate | spf2 | benchmark.persistence | 90 | 0.383 | 0.293 | -0.890 | 0.89 |
| unemployment_rate | spf2 | benchmark.registry_ar | 89 | 0.334 | 0.244 | -0.747 | 0.81 |
| unemployment_rate | spf2 | benchmark.spf_gauss | 90 | 0.105 | 0.150 | -0.406 | 1.00 |
| unemployment_rate | spf2 | benchmark.spf_median | 90 | 0.105 | n. a. | n. a. | n. a. |
| unemployment_rate | spf2 | forecast.bvar_minnesota | 90 | 0.295 | 0.229 | -0.894 | 0.83 |
| unemployment_rate | spf2 | forecast.combo_spf_bvar | 74 | 0.168 | 0.132 | -0.331 | 0.70 |
| unemployment_rate | spf2 | forecast.direct_ar | 90 | 0.347 | 0.249 | -0.655 | 0.88 |
| unemployment_rate | spf2 | forecast.pool_bvar_ar | 90 | 0.319 | 0.232 | -0.620 | 0.87 |
| unemployment_rate | spf2 | forecast.spf_conformal | 74 | 0.099 | 0.070 | 0.596 | 0.72 |
| unemployment_rate | spf3 | benchmark.persistence | 90 | 0.546 | 0.416 | -1.269 | 0.88 |
| unemployment_rate | spf3 | benchmark.registry_ar | 89 | 0.490 | 0.357 | -1.213 | 0.84 |
| unemployment_rate | spf3 | benchmark.spf_gauss | 90 | 0.229 | 0.240 | -0.807 | 0.97 |
| unemployment_rate | spf3 | benchmark.spf_median | 90 | 0.229 | n. a. | n. a. | n. a. |
| unemployment_rate | spf3 | forecast.bvar_minnesota | 90 | 0.435 | 0.335 | -1.837 | 0.84 |
| unemployment_rate | spf3 | forecast.combo_spf_bvar | 73 | 0.299 | 0.235 | -1.796 | 0.70 |
| unemployment_rate | spf3 | forecast.direct_ar | 90 | 0.505 | 0.367 | -1.061 | 0.86 |
| unemployment_rate | spf3 | forecast.pool_bvar_ar | 90 | 0.455 | 0.338 | -1.097 | 0.89 |
| unemployment_rate | spf3 | forecast.spf_conformal | 73 | 0.220 | 0.161 | -0.798 | 0.79 |
| unemployment_rate | spf4 | benchmark.persistence | 90 | 0.703 | 0.530 | -1.517 | 0.87 |
| unemployment_rate | spf4 | benchmark.registry_ar | 89 | 0.648 | 0.477 | -1.575 | 0.81 |
| unemployment_rate | spf4 | benchmark.spf_gauss | 90 | 0.345 | 0.324 | -1.074 | 0.97 |
| unemployment_rate | spf4 | benchmark.spf_median | 90 | 0.345 | n. a. | n. a. | n. a. |
| unemployment_rate | spf4 | forecast.bvar_minnesota | 90 | 0.571 | 0.432 | -2.808 | 0.84 |
| unemployment_rate | spf4 | forecast.combo_spf_bvar | 72 | 0.418 | 0.339 | -3.189 | 0.71 |
| unemployment_rate | spf4 | forecast.direct_ar | 90 | 0.653 | 0.469 | -1.327 | 0.90 |
| unemployment_rate | spf4 | forecast.pool_bvar_ar | 90 | 0.590 | 0.433 | -1.458 | 0.88 |
| unemployment_rate | spf4 | forecast.spf_conformal | 72 | 0.337 | 0.252 | -2.503 | 0.85 |
| unemployment_rate | spf5 | benchmark.persistence | 90 | 0.853 | 0.638 | -1.699 | 0.87 |
| unemployment_rate | spf5 | benchmark.registry_ar | 89 | 0.828 | 0.607 | -1.893 | 0.81 |
| unemployment_rate | spf5 | benchmark.spf_gauss | 90 | 0.468 | 0.417 | -1.298 | 0.96 |
| unemployment_rate | spf5 | benchmark.spf_median | 90 | 0.468 | n. a. | n. a. | n. a. |
| unemployment_rate | spf5 | forecast.bvar_minnesota | 90 | 0.689 | 0.528 | -3.100 | 0.81 |
| unemployment_rate | spf5 | forecast.combo_spf_bvar | 71 | 0.559 | 0.467 | -5.272 | 0.62 |
| unemployment_rate | spf5 | forecast.direct_ar | 90 | 0.821 | 0.582 | -1.547 | 0.89 |
| unemployment_rate | spf5 | forecast.pool_bvar_ar | 90 | 0.730 | 0.533 | -1.726 | 0.88 |
| unemployment_rate | spf5 | forecast.spf_conformal | 71 | 0.478 | 0.373 | -3.982 | 0.76 |
| unemployment_rate | spf6 | benchmark.persistence | 90 | 0.998 | 0.735 | -1.834 | 0.91 |
| unemployment_rate | spf6 | benchmark.registry_ar | 89 | 0.979 | 0.717 | -2.111 | 0.79 |
| unemployment_rate | spf6 | benchmark.spf_gauss | 90 | 0.583 | 0.503 | -1.471 | 0.94 |
| unemployment_rate | spf6 | benchmark.spf_median | 90 | 0.583 | n. a. | n. a. | n. a. |
| unemployment_rate | spf6 | forecast.bvar_minnesota | 90 | 0.794 | 0.610 | -3.418 | 0.83 |
| unemployment_rate | spf6 | forecast.combo_spf_bvar | 70 | 0.695 | 0.597 | -6.146 | 0.59 |
| unemployment_rate | spf6 | forecast.direct_ar | 90 | 0.932 | 0.671 | -1.693 | 0.86 |
| unemployment_rate | spf6 | forecast.pool_bvar_ar | 90 | 0.830 | 0.614 | -1.856 | 0.88 |
| unemployment_rate | spf6 | forecast.spf_conformal | 70 | 0.609 | 0.506 | -5.002 | 0.70 |

| variable | horizon | candidate | benchmark | loss | n pairs / origins | relative | p (better) | Holm |
|---|---|---|---|---|---|---|---|---|
| gdp_price_inflation | spf2 | forecast.direct_ar | benchmark.spf_median | ae | 90 / 90 | 1.262 | 0.9985 | no |
| gdp_price_inflation | spf3 | forecast.direct_ar | benchmark.spf_median | ae | 90 / 90 | 1.217 | 0.9905 | no |
| gdp_price_inflation | spf4 | forecast.direct_ar | benchmark.spf_median | ae | 90 / 90 | 1.332 | 0.9999 | no |
| gdp_price_inflation | spf5 | forecast.direct_ar | benchmark.spf_median | ae | 90 / 90 | 1.348 | 0.9954 | no |
| gdp_price_inflation | spf6 | forecast.direct_ar | benchmark.spf_median | ae | 90 / 90 | 1.411 | 0.9991 | no |
| real_gdp_growth | spf2 | forecast.direct_ar | benchmark.spf_median | ae | 90 / 90 | 1.356 | 0.9773 | no |
| real_gdp_growth | spf3 | forecast.direct_ar | benchmark.spf_median | ae | 90 / 90 | 1.136 | 0.9072 | no |
| real_gdp_growth | spf4 | forecast.direct_ar | benchmark.spf_median | ae | 90 / 90 | 1.020 | 0.6423 | no |
| real_gdp_growth | spf5 | forecast.direct_ar | benchmark.spf_median | ae | 90 / 90 | 0.988 | 0.3820 | no |
| real_gdp_growth | spf6 | forecast.direct_ar | benchmark.spf_median | ae | 90 / 90 | 1.003 | 0.5484 | no |
| unemployment_rate | spf2 | forecast.direct_ar | benchmark.spf_median | ae | 90 / 90 | 3.312 | 1.0000 | no |
| unemployment_rate | spf3 | forecast.direct_ar | benchmark.spf_median | ae | 90 / 90 | 2.201 | 1.0000 | no |
| unemployment_rate | spf4 | forecast.direct_ar | benchmark.spf_median | ae | 90 / 90 | 1.893 | 1.0000 | no |
| unemployment_rate | spf5 | forecast.direct_ar | benchmark.spf_median | ae | 90 / 90 | 1.754 | 0.9999 | no |
| unemployment_rate | spf6 | forecast.direct_ar | benchmark.spf_median | ae | 90 / 90 | 1.599 | 0.9996 | no |
| gdp_price_inflation | spf2 | forecast.bvar_minnesota | benchmark.spf_median | ae | 90 / 90 | 1.311 | 0.9988 | no |
| gdp_price_inflation | spf3 | forecast.bvar_minnesota | benchmark.spf_median | ae | 90 / 90 | 1.344 | 0.9996 | no |
| gdp_price_inflation | spf4 | forecast.bvar_minnesota | benchmark.spf_median | ae | 90 / 90 | 1.485 | 0.9999 | no |
| gdp_price_inflation | spf5 | forecast.bvar_minnesota | benchmark.spf_median | ae | 90 / 90 | 1.498 | 0.9981 | no |
| gdp_price_inflation | spf6 | forecast.bvar_minnesota | benchmark.spf_median | ae | 90 / 90 | 1.538 | 0.9996 | no |
| real_gdp_growth | spf2 | forecast.bvar_minnesota | benchmark.spf_median | ae | 90 / 90 | 1.668 | 0.9994 | no |
| real_gdp_growth | spf3 | forecast.bvar_minnesota | benchmark.spf_median | ae | 90 / 90 | 1.390 | 0.9928 | no |
| real_gdp_growth | spf4 | forecast.bvar_minnesota | benchmark.spf_median | ae | 90 / 90 | 1.267 | 0.9462 | no |
| real_gdp_growth | spf5 | forecast.bvar_minnesota | benchmark.spf_median | ae | 90 / 90 | 1.203 | 0.8855 | no |
| real_gdp_growth | spf6 | forecast.bvar_minnesota | benchmark.spf_median | ae | 90 / 90 | 1.183 | 0.8294 | no |
| unemployment_rate | spf2 | forecast.bvar_minnesota | benchmark.spf_median | ae | 90 / 90 | 2.816 | 0.9999 | no |
| unemployment_rate | spf3 | forecast.bvar_minnesota | benchmark.spf_median | ae | 90 / 90 | 1.899 | 0.9973 | no |
| unemployment_rate | spf4 | forecast.bvar_minnesota | benchmark.spf_median | ae | 90 / 90 | 1.656 | 0.9919 | no |
| unemployment_rate | spf5 | forecast.bvar_minnesota | benchmark.spf_median | ae | 90 / 90 | 1.472 | 0.9682 | no |
| unemployment_rate | spf6 | forecast.bvar_minnesota | benchmark.spf_median | ae | 90 / 90 | 1.362 | 0.9348 | no |
| gdp_price_inflation | spf2 | forecast.combo_spf_bvar | benchmark.spf_median | ae | 74 / 74 | 1.089 | 0.9350 | no |
| gdp_price_inflation | spf3 | forecast.combo_spf_bvar | benchmark.spf_median | ae | 73 / 73 | 1.118 | 0.9737 | no |
| gdp_price_inflation | spf4 | forecast.combo_spf_bvar | benchmark.spf_median | ae | 72 / 72 | 1.209 | 0.9939 | no |
| gdp_price_inflation | spf5 | forecast.combo_spf_bvar | benchmark.spf_median | ae | 71 / 71 | 1.200 | 0.9545 | no |
| gdp_price_inflation | spf6 | forecast.combo_spf_bvar | benchmark.spf_median | ae | 70 / 70 | 1.232 | 0.9813 | no |
| real_gdp_growth | spf2 | forecast.combo_spf_bvar | benchmark.spf_median | ae | 74 / 74 | 1.173 | 0.9456 | no |
| real_gdp_growth | spf3 | forecast.combo_spf_bvar | benchmark.spf_median | ae | 73 / 73 | 1.103 | 0.9009 | no |
| real_gdp_growth | spf4 | forecast.combo_spf_bvar | benchmark.spf_median | ae | 72 / 72 | 1.080 | 0.8045 | no |
| real_gdp_growth | spf5 | forecast.combo_spf_bvar | benchmark.spf_median | ae | 71 / 71 | 1.063 | 0.7451 | no |
| real_gdp_growth | spf6 | forecast.combo_spf_bvar | benchmark.spf_median | ae | 70 / 70 | 1.070 | 0.7245 | no |
| unemployment_rate | spf2 | forecast.combo_spf_bvar | benchmark.spf_median | ae | 74 / 74 | 1.701 | 0.9899 | no |
| unemployment_rate | spf3 | forecast.combo_spf_bvar | benchmark.spf_median | ae | 73 / 73 | 1.358 | 0.9671 | no |
| unemployment_rate | spf4 | forecast.combo_spf_bvar | benchmark.spf_median | ae | 72 / 72 | 1.239 | 0.9370 | no |
| unemployment_rate | spf5 | forecast.combo_spf_bvar | benchmark.spf_median | ae | 71 / 71 | 1.170 | 0.8956 | no |
| unemployment_rate | spf6 | forecast.combo_spf_bvar | benchmark.spf_median | ae | 70 / 70 | 1.141 | 0.8695 | no |
| gdp_price_inflation | spf2 | forecast.combo_spf_ar | benchmark.spf_median | ae | 74 / 74 | 1.068 | 0.9422 | no |
| gdp_price_inflation | spf3 | forecast.combo_spf_ar | benchmark.spf_median | ae | 73 / 73 | 1.058 | 0.8753 | no |
| gdp_price_inflation | spf4 | forecast.combo_spf_ar | benchmark.spf_median | ae | 72 / 72 | 1.116 | 0.9886 | no |
| gdp_price_inflation | spf5 | forecast.combo_spf_ar | benchmark.spf_median | ae | 71 / 71 | 1.110 | 0.9211 | no |
| gdp_price_inflation | spf6 | forecast.combo_spf_ar | benchmark.spf_median | ae | 70 / 70 | 1.136 | 0.9651 | no |
| real_gdp_growth | spf2 | forecast.combo_spf_ar | benchmark.spf_median | ae | 74 / 74 | 1.081 | 0.8179 | no |
| real_gdp_growth | spf3 | forecast.combo_spf_ar | benchmark.spf_median | ae | 73 / 73 | 1.020 | 0.6853 | no |
| real_gdp_growth | spf4 | forecast.combo_spf_ar | benchmark.spf_median | ae | 72 / 72 | 0.986 | 0.3107 | no |
| real_gdp_growth | spf5 | forecast.combo_spf_ar | benchmark.spf_median | ae | 71 / 71 | 0.966 | 0.0199 | no |
| real_gdp_growth | spf6 | forecast.combo_spf_ar | benchmark.spf_median | ae | 70 / 70 | 0.986 | 0.1658 | no |
| unemployment_rate | spf2 | forecast.combo_spf_ar | benchmark.spf_median | ae | 74 / 74 | 2.070 | 1.0000 | no |
| unemployment_rate | spf3 | forecast.combo_spf_ar | benchmark.spf_median | ae | 73 / 73 | 1.577 | 0.9999 | no |
| unemployment_rate | spf4 | forecast.combo_spf_ar | benchmark.spf_median | ae | 72 / 72 | 1.364 | 0.9925 | no |
| unemployment_rate | spf5 | forecast.combo_spf_ar | benchmark.spf_median | ae | 71 / 71 | 1.324 | 0.9943 | no |
| unemployment_rate | spf6 | forecast.combo_spf_ar | benchmark.spf_median | ae | 70 / 70 | 1.236 | 0.9631 | no |
| gdp_price_inflation | spf2 | forecast.spf_conformal | benchmark.spf_gauss | crps | 74 / 74 | 0.928 | 0.0829 | no |
| gdp_price_inflation | spf3 | forecast.spf_conformal | benchmark.spf_gauss | crps | 73 / 73 | 0.962 | 0.2872 | no |
| gdp_price_inflation | spf4 | forecast.spf_conformal | benchmark.spf_gauss | crps | 72 / 72 | 0.944 | 0.2843 | no |
| gdp_price_inflation | spf5 | forecast.spf_conformal | benchmark.spf_gauss | crps | 71 / 71 | 0.964 | 0.3837 | no |
| gdp_price_inflation | spf6 | forecast.spf_conformal | benchmark.spf_gauss | crps | 70 / 70 | 0.968 | 0.4123 | no |
| real_gdp_growth | spf2 | forecast.spf_conformal | benchmark.spf_gauss | crps | 74 / 74 | 0.726 | 0.0000 | yes |
| real_gdp_growth | spf3 | forecast.spf_conformal | benchmark.spf_gauss | crps | 73 / 73 | 0.818 | 0.0000 | yes |
| real_gdp_growth | spf4 | forecast.spf_conformal | benchmark.spf_gauss | crps | 72 / 72 | 0.859 | 0.0072 | no |
| real_gdp_growth | spf5 | forecast.spf_conformal | benchmark.spf_gauss | crps | 71 / 71 | 0.853 | 0.0178 | no |
| real_gdp_growth | spf6 | forecast.spf_conformal | benchmark.spf_gauss | crps | 70 / 70 | 0.858 | 0.0309 | no |
| unemployment_rate | spf2 | forecast.spf_conformal | benchmark.spf_gauss | crps | 74 / 74 | 0.465 | 0.0000 | yes |
| unemployment_rate | spf3 | forecast.spf_conformal | benchmark.spf_gauss | crps | 73 / 73 | 0.665 | 0.0005 | yes |
| unemployment_rate | spf4 | forecast.spf_conformal | benchmark.spf_gauss | crps | 72 / 72 | 0.770 | 0.0252 | no |
| unemployment_rate | spf5 | forecast.spf_conformal | benchmark.spf_gauss | crps | 71 / 71 | 0.872 | 0.1530 | no |
| unemployment_rate | spf6 | forecast.spf_conformal | benchmark.spf_gauss | crps | 70 / 70 | 0.958 | 0.3640 | no |
| gdp_price_inflation | spf2 | forecast.combo_spf_bvar | benchmark.spf_gauss | crps | 74 / 74 | 0.973 | 0.3422 | no |
| gdp_price_inflation | spf3 | forecast.combo_spf_bvar | benchmark.spf_gauss | crps | 73 / 73 | 1.007 | 0.5413 | no |
| gdp_price_inflation | spf4 | forecast.combo_spf_bvar | benchmark.spf_gauss | crps | 72 / 72 | 1.034 | 0.6362 | no |
| gdp_price_inflation | spf5 | forecast.combo_spf_bvar | benchmark.spf_gauss | crps | 71 / 71 | 1.052 | 0.6914 | no |
| gdp_price_inflation | spf6 | forecast.combo_spf_bvar | benchmark.spf_gauss | crps | 70 / 70 | 1.059 | 0.6634 | no |
| real_gdp_growth | spf2 | forecast.combo_spf_bvar | benchmark.spf_gauss | crps | 74 / 74 | 0.873 | 0.0482 | no |
| real_gdp_growth | spf3 | forecast.combo_spf_bvar | benchmark.spf_gauss | crps | 73 / 73 | 0.879 | 0.0192 | no |
| real_gdp_growth | spf4 | forecast.combo_spf_bvar | benchmark.spf_gauss | crps | 72 / 72 | 0.895 | 0.0311 | no |
| real_gdp_growth | spf5 | forecast.combo_spf_bvar | benchmark.spf_gauss | crps | 71 / 71 | 0.871 | 0.0102 | no |
| real_gdp_growth | spf6 | forecast.combo_spf_bvar | benchmark.spf_gauss | crps | 70 / 70 | 0.875 | 0.0268 | no |
| unemployment_rate | spf2 | forecast.combo_spf_bvar | benchmark.spf_gauss | crps | 74 / 74 | 0.876 | 0.2628 | no |
| unemployment_rate | spf3 | forecast.combo_spf_bvar | benchmark.spf_gauss | crps | 73 / 73 | 0.972 | 0.4502 | no |
| unemployment_rate | spf4 | forecast.combo_spf_bvar | benchmark.spf_gauss | crps | 72 / 72 | 1.037 | 0.5640 | no |
| unemployment_rate | spf5 | forecast.combo_spf_bvar | benchmark.spf_gauss | crps | 71 / 71 | 1.092 | 0.6792 | no |
| unemployment_rate | spf6 | forecast.combo_spf_bvar | benchmark.spf_gauss | crps | 70 / 70 | 1.131 | 0.7697 | no |
| gdp_price_inflation | spf2 | forecast.combo_spf_ar | benchmark.spf_gauss | crps | 74 / 74 | 0.963 | 0.2882 | no |
| gdp_price_inflation | spf3 | forecast.combo_spf_ar | benchmark.spf_gauss | crps | 73 / 73 | 0.982 | 0.4064 | no |
| gdp_price_inflation | spf4 | forecast.combo_spf_ar | benchmark.spf_gauss | crps | 72 / 72 | 1.014 | 0.5553 | no |
| gdp_price_inflation | spf5 | forecast.combo_spf_ar | benchmark.spf_gauss | crps | 71 / 71 | 1.015 | 0.5457 | no |
| gdp_price_inflation | spf6 | forecast.combo_spf_ar | benchmark.spf_gauss | crps | 70 / 70 | 1.050 | 0.6255 | no |
| real_gdp_growth | spf2 | forecast.combo_spf_ar | benchmark.spf_gauss | crps | 74 / 74 | 0.811 | 0.0050 | no |
| real_gdp_growth | spf3 | forecast.combo_spf_ar | benchmark.spf_gauss | crps | 73 / 73 | 0.828 | 0.0055 | no |
| real_gdp_growth | spf4 | forecast.combo_spf_ar | benchmark.spf_gauss | crps | 72 / 72 | 0.842 | 0.0139 | no |
| real_gdp_growth | spf5 | forecast.combo_spf_ar | benchmark.spf_gauss | crps | 71 / 71 | 0.820 | 0.0061 | no |
| real_gdp_growth | spf6 | forecast.combo_spf_ar | benchmark.spf_gauss | crps | 70 / 70 | 0.836 | 0.0174 | no |
| unemployment_rate | spf2 | forecast.combo_spf_ar | benchmark.spf_gauss | crps | 74 / 74 | 0.977 | 0.4475 | no |
| unemployment_rate | spf3 | forecast.combo_spf_ar | benchmark.spf_gauss | crps | 73 / 73 | 1.057 | 0.6101 | no |
| unemployment_rate | spf4 | forecast.combo_spf_ar | benchmark.spf_gauss | crps | 72 / 72 | 1.109 | 0.6891 | no |
| unemployment_rate | spf5 | forecast.combo_spf_ar | benchmark.spf_gauss | crps | 71 / 71 | 1.177 | 0.8100 | no |
| unemployment_rate | spf6 | forecast.combo_spf_ar | benchmark.spf_gauss | crps | 70 / 70 | 1.208 | 0.8641 | no |
| gdp_price_inflation | spf2 | forecast.bvar_minnesota | benchmark.persistence | crps | 90 / 90 | 0.942 | 0.1161 | no |
| gdp_price_inflation | spf3 | forecast.bvar_minnesota | benchmark.persistence | crps | 90 / 90 | 1.040 | 0.7185 | no |
| gdp_price_inflation | spf4 | forecast.bvar_minnesota | benchmark.persistence | crps | 90 / 90 | 1.175 | 0.9615 | no |
| gdp_price_inflation | spf5 | forecast.bvar_minnesota | benchmark.persistence | crps | 90 / 90 | 1.181 | 0.9416 | no |
| gdp_price_inflation | spf6 | forecast.bvar_minnesota | benchmark.persistence | crps | 90 / 90 | 1.202 | 0.9313 | no |
| real_gdp_growth | spf2 | forecast.bvar_minnesota | benchmark.persistence | crps | 90 / 90 | 0.892 | 0.1272 | no |
| real_gdp_growth | spf3 | forecast.bvar_minnesota | benchmark.persistence | crps | 90 / 90 | 0.865 | 0.1127 | no |
| real_gdp_growth | spf4 | forecast.bvar_minnesota | benchmark.persistence | crps | 90 / 90 | 0.842 | 0.0753 | no |
| real_gdp_growth | spf5 | forecast.bvar_minnesota | benchmark.persistence | crps | 90 / 90 | 0.800 | 0.0083 | no |
| real_gdp_growth | spf6 | forecast.bvar_minnesota | benchmark.persistence | crps | 90 / 90 | 0.772 | 0.0037 | yes |
| unemployment_rate | spf2 | forecast.bvar_minnesota | benchmark.persistence | crps | 90 / 90 | 0.781 | 0.0013 | yes |
| unemployment_rate | spf3 | forecast.bvar_minnesota | benchmark.persistence | crps | 90 / 90 | 0.804 | 0.0004 | yes |
| unemployment_rate | spf4 | forecast.bvar_minnesota | benchmark.persistence | crps | 90 / 90 | 0.815 | 0.0001 | yes |
| unemployment_rate | spf5 | forecast.bvar_minnesota | benchmark.persistence | crps | 90 / 90 | 0.827 | 0.0008 | yes |
| unemployment_rate | spf6 | forecast.bvar_minnesota | benchmark.persistence | crps | 90 / 90 | 0.831 | 0.0019 | yes |
| gdp_price_inflation | spf2 | forecast.direct_ar | benchmark.persistence | crps | 90 / 90 | 0.902 | 0.0212 | no |
| gdp_price_inflation | spf3 | forecast.direct_ar | benchmark.persistence | crps | 90 / 90 | 0.946 | 0.1727 | no |
| gdp_price_inflation | spf4 | forecast.direct_ar | benchmark.persistence | crps | 90 / 90 | 1.068 | 0.8719 | no |
| gdp_price_inflation | spf5 | forecast.direct_ar | benchmark.persistence | crps | 90 / 90 | 1.053 | 0.7212 | no |
| gdp_price_inflation | spf6 | forecast.direct_ar | benchmark.persistence | crps | 90 / 90 | 1.082 | 0.7900 | no |
| real_gdp_growth | spf2 | forecast.direct_ar | benchmark.persistence | crps | 90 / 90 | 0.802 | 0.0025 | yes |
| real_gdp_growth | spf3 | forecast.direct_ar | benchmark.persistence | crps | 90 / 90 | 0.774 | 0.0062 | no |
| real_gdp_growth | spf4 | forecast.direct_ar | benchmark.persistence | crps | 90 / 90 | 0.750 | 0.0031 | yes |
| real_gdp_growth | spf5 | forecast.direct_ar | benchmark.persistence | crps | 90 / 90 | 0.718 | 0.0001 | yes |
| real_gdp_growth | spf6 | forecast.direct_ar | benchmark.persistence | crps | 90 / 90 | 0.702 | 0.0001 | yes |
| unemployment_rate | spf2 | forecast.direct_ar | benchmark.persistence | crps | 90 / 90 | 0.852 | 0.0930 | no |
| unemployment_rate | spf3 | forecast.direct_ar | benchmark.persistence | crps | 90 / 90 | 0.881 | 0.1359 | no |
| unemployment_rate | spf4 | forecast.direct_ar | benchmark.persistence | crps | 90 / 90 | 0.885 | 0.1451 | no |
| unemployment_rate | spf5 | forecast.direct_ar | benchmark.persistence | crps | 90 / 90 | 0.913 | 0.2363 | no |
| unemployment_rate | spf6 | forecast.direct_ar | benchmark.persistence | crps | 90 / 90 | 0.913 | 0.2600 | no |
| gdp_price_inflation | spf2 | forecast.pool_bvar_ar | benchmark.persistence | crps | 90 / 90 | 0.914 | 0.0334 | no |
| gdp_price_inflation | spf3 | forecast.pool_bvar_ar | benchmark.persistence | crps | 90 / 90 | 0.984 | 0.3978 | no |
| gdp_price_inflation | spf4 | forecast.pool_bvar_ar | benchmark.persistence | crps | 90 / 90 | 1.110 | 0.9234 | no |
| gdp_price_inflation | spf5 | forecast.pool_bvar_ar | benchmark.persistence | crps | 90 / 90 | 1.104 | 0.8511 | no |
| gdp_price_inflation | spf6 | forecast.pool_bvar_ar | benchmark.persistence | crps | 90 / 90 | 1.131 | 0.8706 | no |
| real_gdp_growth | spf2 | forecast.pool_bvar_ar | benchmark.persistence | crps | 90 / 90 | 0.828 | 0.0156 | no |
| real_gdp_growth | spf3 | forecast.pool_bvar_ar | benchmark.persistence | crps | 90 / 90 | 0.800 | 0.0187 | no |
| real_gdp_growth | spf4 | forecast.pool_bvar_ar | benchmark.persistence | crps | 90 / 90 | 0.775 | 0.0078 | no |
| real_gdp_growth | spf5 | forecast.pool_bvar_ar | benchmark.persistence | crps | 90 / 90 | 0.740 | 0.0001 | yes |
| real_gdp_growth | spf6 | forecast.pool_bvar_ar | benchmark.persistence | crps | 90 / 90 | 0.719 | 0.0000 | yes |
| unemployment_rate | spf2 | forecast.pool_bvar_ar | benchmark.persistence | crps | 90 / 90 | 0.791 | 0.0102 | no |
| unemployment_rate | spf3 | forecast.pool_bvar_ar | benchmark.persistence | crps | 90 / 90 | 0.813 | 0.0085 | no |
| unemployment_rate | spf4 | forecast.pool_bvar_ar | benchmark.persistence | crps | 90 / 90 | 0.818 | 0.0048 | no |
| unemployment_rate | spf5 | forecast.pool_bvar_ar | benchmark.persistence | crps | 90 / 90 | 0.836 | 0.0139 | no |
| unemployment_rate | spf6 | forecast.pool_bvar_ar | benchmark.persistence | crps | 90 / 90 | 0.836 | 0.0174 | no |

#### g7 — development (5712 scored cases, 68 origins ['1999-03-01T00:00:00+00:00', '2015-12-01T00:00:00+00:00'])

| variable | horizon | model | n | MAE | CRPS | log | cov80 |
|---|---|---|---|---|---|---|---|
| cpi_inflation | k0 | benchmark.persistence | 469 | 0.418 | 0.307 | -0.873 | 0.77 |
| cpi_inflation | k0 | benchmark.registry_ar | 363 | 0.435 | 0.320 | -0.957 | 0.72 |
| cpi_inflation | k0 | forecast.bvar_minnesota | 330 | 0.616 | 0.453 | -1.788 | 0.68 |
| cpi_inflation | k0 | forecast.direct_ar | 364 | 0.428 | 0.314 | -0.883 | 0.76 |
| cpi_inflation | k0 | forecast.pool_bvar_ar | 330 | 0.489 | 0.352 | -1.126 | 0.78 |
| cpi_inflation | k1 | benchmark.persistence | 462 | 0.663 | 0.490 | -1.410 | 0.76 |
| cpi_inflation | k1 | benchmark.registry_ar | 363 | 0.685 | 0.510 | -1.543 | 0.66 |
| cpi_inflation | k1 | forecast.bvar_minnesota | 330 | 0.808 | 0.607 | -2.277 | 0.62 |
| cpi_inflation | k1 | forecast.direct_ar | 364 | 0.674 | 0.495 | -1.404 | 0.69 |
| cpi_inflation | k1 | forecast.pool_bvar_ar | 330 | 0.732 | 0.533 | -1.647 | 0.68 |
| cpi_inflation | k2 | benchmark.persistence | 455 | 0.843 | 0.621 | -1.688 | 0.76 |
| cpi_inflation | k2 | benchmark.registry_ar | 363 | 0.850 | 0.634 | -1.876 | 0.63 |
| cpi_inflation | k2 | forecast.bvar_minnesota | 330 | 0.947 | 0.707 | -2.455 | 0.57 |
| cpi_inflation | k2 | forecast.direct_ar | 364 | 0.812 | 0.605 | -1.721 | 0.65 |
| cpi_inflation | k2 | forecast.pool_bvar_ar | 330 | 0.866 | 0.641 | -1.929 | 0.62 |
| cpi_inflation | k3 | benchmark.persistence | 448 | 0.980 | 0.715 | -1.826 | 0.79 |
| cpi_inflation | k3 | benchmark.registry_ar | 363 | 0.950 | 0.716 | -2.051 | 0.60 |
| cpi_inflation | k3 | forecast.bvar_minnesota | 330 | 1.007 | 0.746 | -2.299 | 0.58 |
| cpi_inflation | k3 | forecast.direct_ar | 364 | 0.922 | 0.693 | -1.913 | 0.62 |
| cpi_inflation | k3 | forecast.pool_bvar_ar | 330 | 0.953 | 0.709 | -2.071 | 0.62 |
| real_gdp_growth | k0 | benchmark.persistence | 475 | 0.568 | 0.422 | -1.122 | 0.83 |
| real_gdp_growth | k0 | benchmark.registry_ar | 379 | 0.458 | 0.356 | -1.144 | 0.83 |
| real_gdp_growth | k0 | forecast.bvar_minnesota | 330 | 0.480 | 0.358 | -2.192 | 0.84 |
| real_gdp_growth | k0 | forecast.combo_dfm_bvar | 307 | 0.468 | 0.366 | -1.391 | 0.77 |
| real_gdp_growth | k0 | forecast.dfm_nowcast | 406 | 0.453 | 0.346 | -1.096 | 0.82 |
| real_gdp_growth | k0 | forecast.dfm_nowcast_x | 384 | 0.454 | 0.339 | -1.033 | 0.76 |
| real_gdp_growth | k0 | forecast.direct_ar | 379 | 0.446 | 0.348 | -1.067 | 0.84 |
| real_gdp_growth | k0 | forecast.pool_bvar_ar | 330 | 0.453 | 0.346 | -1.509 | 0.85 |
| real_gdp_growth | k1 | benchmark.persistence | 469 | 0.608 | 0.453 | -1.242 | 0.85 |
| real_gdp_growth | k1 | benchmark.registry_ar | 379 | 0.460 | 0.364 | -1.287 | 0.85 |
| real_gdp_growth | k1 | forecast.bvar_minnesota | 330 | 0.487 | 0.367 | -2.869 | 0.84 |
| real_gdp_growth | k1 | forecast.combo_dfm_bvar | 66 | 0.440 | 0.360 | -2.859 | 0.80 |
| real_gdp_growth | k1 | forecast.dfm_nowcast | 92 | 0.409 | 0.317 | -1.150 | 0.82 |
| real_gdp_growth | k1 | forecast.dfm_nowcast_x | 92 | 0.384 | 0.297 | -1.052 | 0.76 |
| real_gdp_growth | k1 | forecast.direct_ar | 379 | 0.455 | 0.358 | -1.234 | 0.85 |
| real_gdp_growth | k1 | forecast.pool_bvar_ar | 330 | 0.458 | 0.356 | -2.310 | 0.88 |
| real_gdp_growth | k2 | benchmark.persistence | 464 | 0.633 | 0.482 | -1.372 | 0.84 |
| real_gdp_growth | k2 | benchmark.registry_ar | 379 | 0.471 | 0.371 | -1.388 | 0.85 |
| real_gdp_growth | k2 | forecast.bvar_minnesota | 330 | 0.496 | 0.375 | -4.169 | 0.83 |
| real_gdp_growth | k2 | forecast.direct_ar | 379 | 0.468 | 0.368 | -1.391 | 0.86 |
| real_gdp_growth | k2 | forecast.pool_bvar_ar | 330 | 0.469 | 0.367 | -3.276 | 0.85 |
| real_gdp_growth | k3 | benchmark.persistence | 457 | 0.632 | 0.483 | -1.418 | 0.84 |
| real_gdp_growth | k3 | benchmark.registry_ar | 379 | 0.460 | 0.369 | -1.425 | 0.86 |
| real_gdp_growth | k3 | forecast.bvar_minnesota | 330 | 0.496 | 0.379 | -3.908 | 0.82 |
| real_gdp_growth | k3 | forecast.direct_ar | 379 | 0.460 | 0.364 | -1.447 | 0.85 |
| real_gdp_growth | k3 | forecast.pool_bvar_ar | 330 | 0.472 | 0.370 | -3.570 | 0.86 |
| unemployment_rate | k0 | benchmark.persistence | 476 | 0.253 | 0.189 | -0.538 | 0.73 |
| unemployment_rate | k0 | benchmark.registry_ar | 365 | 0.230 | 0.177 | -0.748 | 0.63 |
| unemployment_rate | k0 | forecast.bvar_minnesota | 330 | 0.289 | 0.217 | -1.522 | 0.67 |
| unemployment_rate | k0 | forecast.direct_ar | 390 | 0.249 | 0.191 | -0.805 | 0.65 |
| unemployment_rate | k0 | forecast.pool_bvar_ar | 330 | 0.257 | 0.189 | -0.821 | 0.74 |
| unemployment_rate | k1 | benchmark.persistence | 476 | 0.392 | 0.290 | -1.069 | 0.76 |
| unemployment_rate | k1 | benchmark.registry_ar | 365 | 0.356 | 0.272 | -1.293 | 0.69 |
| unemployment_rate | k1 | forecast.bvar_minnesota | 330 | 0.415 | 0.309 | -3.072 | 0.65 |
| unemployment_rate | k1 | forecast.direct_ar | 390 | 0.385 | 0.293 | -1.306 | 0.68 |
| unemployment_rate | k1 | forecast.pool_bvar_ar | 330 | 0.377 | 0.283 | -2.196 | 0.75 |
| unemployment_rate | k2 | benchmark.persistence | 474 | 0.522 | 0.384 | -1.411 | 0.78 |
| unemployment_rate | k2 | benchmark.registry_ar | 365 | 0.487 | 0.371 | -1.718 | 0.67 |
| unemployment_rate | k2 | forecast.bvar_minnesota | 330 | 0.543 | 0.411 | -4.117 | 0.62 |
| unemployment_rate | k2 | forecast.direct_ar | 390 | 0.518 | 0.394 | -1.763 | 0.68 |
| unemployment_rate | k2 | forecast.pool_bvar_ar | 330 | 0.511 | 0.385 | -3.659 | 0.71 |
| unemployment_rate | k3 | benchmark.persistence | 471 | 0.636 | 0.467 | -1.559 | 0.82 |
| unemployment_rate | k3 | benchmark.registry_ar | 365 | 0.600 | 0.457 | -2.038 | 0.67 |
| unemployment_rate | k3 | forecast.bvar_minnesota | 330 | 0.676 | 0.513 | -6.026 | 0.60 |
| unemployment_rate | k3 | forecast.direct_ar | 390 | 0.640 | 0.487 | -2.116 | 0.64 |
| unemployment_rate | k3 | forecast.pool_bvar_ar | 330 | 0.634 | 0.483 | -4.818 | 0.68 |

| variable | horizon | candidate | benchmark | loss | n pairs / origins | relative | p (better) | Holm |
|---|---|---|---|---|---|---|---|---|
| cpi_inflation | k0 | forecast.bvar_minnesota | benchmark.persistence | crps | 330 / 48 | 1.392 | 1.0000 | no |
| cpi_inflation | k1 | forecast.bvar_minnesota | benchmark.persistence | crps | 330 / 48 | 1.149 | 0.9652 | no |
| cpi_inflation | k2 | forecast.bvar_minnesota | benchmark.persistence | crps | 330 / 48 | 1.048 | 0.6869 | no |
| cpi_inflation | k3 | forecast.bvar_minnesota | benchmark.persistence | crps | 330 / 48 | 0.960 | 0.3962 | no |
| real_gdp_growth | k0 | forecast.bvar_minnesota | benchmark.persistence | crps | 330 / 48 | 0.809 | 0.0582 | no |
| real_gdp_growth | k1 | forecast.bvar_minnesota | benchmark.persistence | crps | 330 / 48 | 0.772 | 0.0399 | no |
| real_gdp_growth | k2 | forecast.bvar_minnesota | benchmark.persistence | crps | 330 / 48 | 0.732 | 0.0313 | no |
| real_gdp_growth | k3 | forecast.bvar_minnesota | benchmark.persistence | crps | 330 / 48 | 0.728 | 0.0349 | no |
| unemployment_rate | k0 | forecast.bvar_minnesota | benchmark.persistence | crps | 330 / 48 | 1.102 | 0.9894 | no |
| unemployment_rate | k1 | forecast.bvar_minnesota | benchmark.persistence | crps | 330 / 48 | 1.030 | 0.7635 | no |
| unemployment_rate | k2 | forecast.bvar_minnesota | benchmark.persistence | crps | 330 / 48 | 1.027 | 0.7447 | no |
| unemployment_rate | k3 | forecast.bvar_minnesota | benchmark.persistence | crps | 330 / 48 | 1.050 | 0.9198 | no |
| cpi_inflation | k0 | forecast.direct_ar | benchmark.persistence | crps | 364 / 52 | 0.993 | 0.4403 | no |
| cpi_inflation | k1 | forecast.direct_ar | benchmark.persistence | crps | 364 / 52 | 0.976 | 0.3849 | no |
| cpi_inflation | k2 | forecast.direct_ar | benchmark.persistence | crps | 364 / 52 | 0.937 | 0.3275 | no |
| cpi_inflation | k3 | forecast.direct_ar | benchmark.persistence | crps | 364 / 52 | 0.929 | 0.3439 | no |
| real_gdp_growth | k0 | forecast.direct_ar | benchmark.persistence | crps | 379 / 55 | 0.812 | 0.0268 | no |
| real_gdp_growth | k1 | forecast.direct_ar | benchmark.persistence | crps | 379 / 55 | 0.780 | 0.0198 | no |
| real_gdp_growth | k2 | forecast.direct_ar | benchmark.persistence | crps | 379 / 55 | 0.742 | 0.0138 | no |
| real_gdp_growth | k3 | forecast.direct_ar | benchmark.persistence | crps | 379 / 55 | 0.729 | 0.0195 | no |
| unemployment_rate | k0 | forecast.direct_ar | benchmark.persistence | crps | 390 / 56 | 1.024 | 0.7068 | no |
| unemployment_rate | k1 | forecast.direct_ar | benchmark.persistence | crps | 390 / 56 | 1.029 | 0.6853 | no |
| unemployment_rate | k2 | forecast.direct_ar | benchmark.persistence | crps | 390 / 56 | 1.044 | 0.7096 | no |
| unemployment_rate | k3 | forecast.direct_ar | benchmark.persistence | crps | 390 / 56 | 1.057 | 0.7159 | no |
| cpi_inflation | k0 | forecast.pool_bvar_ar | benchmark.persistence | crps | 330 / 48 | 1.083 | 0.9324 | no |
| cpi_inflation | k1 | forecast.pool_bvar_ar | benchmark.persistence | crps | 330 / 48 | 1.007 | 0.5483 | no |
| cpi_inflation | k2 | forecast.pool_bvar_ar | benchmark.persistence | crps | 330 / 48 | 0.950 | 0.3557 | no |
| cpi_inflation | k3 | forecast.pool_bvar_ar | benchmark.persistence | crps | 330 / 48 | 0.913 | 0.3082 | no |
| real_gdp_growth | k0 | forecast.pool_bvar_ar | benchmark.persistence | crps | 330 / 48 | 0.783 | 0.0342 | no |
| real_gdp_growth | k1 | forecast.pool_bvar_ar | benchmark.persistence | crps | 330 / 48 | 0.750 | 0.0274 | no |
| real_gdp_growth | k2 | forecast.pool_bvar_ar | benchmark.persistence | crps | 330 / 48 | 0.716 | 0.0256 | no |
| real_gdp_growth | k3 | forecast.pool_bvar_ar | benchmark.persistence | crps | 330 / 48 | 0.712 | 0.0308 | no |
| unemployment_rate | k0 | forecast.pool_bvar_ar | benchmark.persistence | crps | 330 / 48 | 0.962 | 0.1244 | no |
| unemployment_rate | k1 | forecast.pool_bvar_ar | benchmark.persistence | crps | 330 / 48 | 0.942 | 0.0626 | no |
| unemployment_rate | k2 | forecast.pool_bvar_ar | benchmark.persistence | crps | 330 / 48 | 0.962 | 0.1699 | no |
| unemployment_rate | k3 | forecast.pool_bvar_ar | benchmark.persistence | crps | 330 / 48 | 0.988 | 0.3899 | no |
| real_gdp_growth | k0 | forecast.dfm_nowcast | benchmark.persistence | crps | 406 / 59 | 0.811 | 0.0431 | no |
| real_gdp_growth | k1 | forecast.dfm_nowcast | benchmark.persistence | crps | 92 / 57 | 0.833 | 0.0402 | no |
| real_gdp_growth | k0 | forecast.combo_dfm_bvar | benchmark.persistence | crps | 307 / 44 | 0.807 | 0.0574 | no |
| real_gdp_growth | k1 | forecast.combo_dfm_bvar | benchmark.persistence | crps | 66 / 31 | 0.893 | 0.1696 | no |
| cpi_inflation | k0 | forecast.bvar_conformal | benchmark.persistence | crps | 315 / 45 | 1.432 | 1.0000 | no |
| cpi_inflation | k1 | forecast.bvar_conformal | benchmark.persistence | crps | 308 / 44 | 1.180 | 0.9937 | no |
| cpi_inflation | k2 | forecast.bvar_conformal | benchmark.persistence | crps | 301 / 43 | 1.079 | 0.8266 | no |
| cpi_inflation | k3 | forecast.bvar_conformal | benchmark.persistence | crps | 294 / 42 | 0.992 | 0.4734 | no |
| real_gdp_growth | k0 | forecast.bvar_conformal | benchmark.persistence | crps | 308 / 44 | 0.834 | 0.0822 | no |
| real_gdp_growth | k1 | forecast.bvar_conformal | benchmark.persistence | crps | 301 / 43 | 0.812 | 0.0743 | no |
| real_gdp_growth | k2 | forecast.bvar_conformal | benchmark.persistence | crps | 294 / 42 | 0.773 | 0.0629 | no |
| real_gdp_growth | k3 | forecast.bvar_conformal | benchmark.persistence | crps | 287 / 41 | 0.773 | 0.0809 | no |
| unemployment_rate | k0 | forecast.bvar_conformal | benchmark.persistence | crps | 308 / 44 | 1.114 | 0.9939 | no |
| unemployment_rate | k1 | forecast.bvar_conformal | benchmark.persistence | crps | 301 / 43 | 1.049 | 0.8367 | no |
| unemployment_rate | k2 | forecast.bvar_conformal | benchmark.persistence | crps | 294 / 42 | 1.071 | 0.8792 | no |
| unemployment_rate | k3 | forecast.bvar_conformal | benchmark.persistence | crps | 287 / 41 | 1.125 | 0.9493 | no |
| real_gdp_growth | k0 | forecast.dfm_conformal | benchmark.persistence | crps | 384 / 55 | 0.847 | 0.0568 | no |
| real_gdp_growth | k1 | forecast.dfm_conformal | benchmark.persistence | crps | 76 / 41 | 0.863 | 0.0532 | no |
| real_gdp_growth | k0 | forecast.dfm_nowcast_x | benchmark.persistence | crps | 384 / 59 | 0.812 | 0.0503 | no |
| real_gdp_growth | k1 | forecast.dfm_nowcast_x | benchmark.persistence | crps | 92 / 57 | 0.779 | 0.0324 | no |
| cpi_inflation | k0 | forecast.bvar_minnesota | benchmark.registry_ar | crps | 329 / 48 | 1.380 | 1.0000 | no |
| cpi_inflation | k1 | forecast.bvar_minnesota | benchmark.registry_ar | crps | 329 / 48 | 1.122 | 0.9593 | no |
| cpi_inflation | k2 | forecast.bvar_minnesota | benchmark.registry_ar | crps | 329 / 48 | 1.044 | 0.7824 | no |
| cpi_inflation | k3 | forecast.bvar_minnesota | benchmark.registry_ar | crps | 329 / 48 | 0.986 | 0.4069 | no |
| real_gdp_growth | k0 | forecast.bvar_minnesota | benchmark.registry_ar | crps | 330 / 48 | 0.971 | 0.2876 | no |
| real_gdp_growth | k1 | forecast.bvar_minnesota | benchmark.registry_ar | crps | 330 / 48 | 0.968 | 0.2985 | no |
| real_gdp_growth | k2 | forecast.bvar_minnesota | benchmark.registry_ar | crps | 330 / 48 | 0.968 | 0.3021 | no |
| real_gdp_growth | k3 | forecast.bvar_minnesota | benchmark.registry_ar | crps | 330 / 48 | 0.980 | 0.3703 | no |
| unemployment_rate | k0 | forecast.bvar_minnesota | benchmark.registry_ar | crps | 314 / 48 | 1.166 | 0.9948 | no |
| unemployment_rate | k1 | forecast.bvar_minnesota | benchmark.registry_ar | crps | 314 / 48 | 1.094 | 0.9770 | no |
| unemployment_rate | k2 | forecast.bvar_minnesota | benchmark.registry_ar | crps | 314 / 48 | 1.059 | 0.8181 | no |
| unemployment_rate | k3 | forecast.bvar_minnesota | benchmark.registry_ar | crps | 314 / 48 | 1.066 | 0.7561 | no |
| cpi_inflation | k0 | forecast.pool_bvar_ar | benchmark.registry_ar | crps | 329 / 48 | 1.072 | 0.9743 | no |
| cpi_inflation | k1 | forecast.pool_bvar_ar | benchmark.registry_ar | crps | 329 / 48 | 0.992 | 0.4483 | no |
| cpi_inflation | k2 | forecast.pool_bvar_ar | benchmark.registry_ar | crps | 329 / 48 | 0.957 | 0.1784 | no |
| cpi_inflation | k3 | forecast.pool_bvar_ar | benchmark.registry_ar | crps | 329 / 48 | 0.946 | 0.0285 | no |
| real_gdp_growth | k0 | forecast.pool_bvar_ar | benchmark.registry_ar | crps | 330 / 48 | 0.939 | 0.0605 | no |
| real_gdp_growth | k1 | forecast.pool_bvar_ar | benchmark.registry_ar | crps | 330 / 48 | 0.941 | 0.0964 | no |
| real_gdp_growth | k2 | forecast.pool_bvar_ar | benchmark.registry_ar | crps | 330 / 48 | 0.947 | 0.1329 | no |
| real_gdp_growth | k3 | forecast.pool_bvar_ar | benchmark.registry_ar | crps | 330 / 48 | 0.958 | 0.1529 | no |
| unemployment_rate | k0 | forecast.pool_bvar_ar | benchmark.registry_ar | crps | 314 / 48 | 0.993 | 0.4868 | no |
| unemployment_rate | k1 | forecast.pool_bvar_ar | benchmark.registry_ar | crps | 314 / 48 | 0.980 | 0.2766 | no |
| unemployment_rate | k2 | forecast.pool_bvar_ar | benchmark.registry_ar | crps | 314 / 48 | 0.977 | 0.2816 | no |
| unemployment_rate | k3 | forecast.pool_bvar_ar | benchmark.registry_ar | crps | 314 / 48 | 0.994 | 0.4332 | no |
| real_gdp_growth | k0 | forecast.dfm_nowcast | benchmark.registry_ar | crps | 378 / 55 | 0.965 | 0.1039 | no |
| real_gdp_growth | k1 | forecast.dfm_nowcast | benchmark.registry_ar | crps | 88 / 53 | 0.940 | 0.0314 | no |
| real_gdp_growth | k0 | forecast.combo_dfm_bvar | benchmark.registry_ar | crps | 307 / 44 | 0.962 | 0.1540 | no |
| real_gdp_growth | k1 | forecast.combo_dfm_bvar | benchmark.registry_ar | crps | 66 / 31 | 0.985 | 0.2807 | no |
| real_gdp_growth | k0 | forecast.dfm_nowcast_x | benchmark.registry_ar | crps | 360 / 55 | 0.958 | 0.1308 | no |
| real_gdp_growth | k1 | forecast.dfm_nowcast_x | benchmark.registry_ar | crps | 88 / 53 | 0.873 | 0.0465 | no |
| cpi_inflation | k0 | forecast.bvar_minnesota | benchmark.registry_ar | ae | 329 / 48 | 1.378 | 1.0000 | no |
| cpi_inflation | k1 | forecast.bvar_minnesota | benchmark.registry_ar | ae | 329 / 48 | 1.113 | 0.9561 | no |
| cpi_inflation | k2 | forecast.bvar_minnesota | benchmark.registry_ar | ae | 329 / 48 | 1.045 | 0.8084 | no |
| cpi_inflation | k3 | forecast.bvar_minnesota | benchmark.registry_ar | ae | 329 / 48 | 1.005 | 0.6561 | no |
| real_gdp_growth | k0 | forecast.bvar_minnesota | benchmark.registry_ar | ae | 330 / 48 | 1.020 | 0.5933 | no |
| real_gdp_growth | k1 | forecast.bvar_minnesota | benchmark.registry_ar | ae | 330 / 48 | 1.023 | 0.6825 | no |
| real_gdp_growth | k2 | forecast.bvar_minnesota | benchmark.registry_ar | ae | 330 / 48 | 1.010 | 0.5587 | no |
| real_gdp_growth | k3 | forecast.bvar_minnesota | benchmark.registry_ar | ae | 330 / 48 | 1.033 | 0.7224 | no |
| unemployment_rate | k0 | forecast.bvar_minnesota | benchmark.registry_ar | ae | 314 / 48 | 1.191 | 0.9987 | no |
| unemployment_rate | k1 | forecast.bvar_minnesota | benchmark.registry_ar | ae | 314 / 48 | 1.122 | 0.9954 | no |
| unemployment_rate | k2 | forecast.bvar_minnesota | benchmark.registry_ar | ae | 314 / 48 | 1.069 | 0.8180 | no |
| unemployment_rate | k3 | forecast.bvar_minnesota | benchmark.registry_ar | ae | 314 / 48 | 1.078 | 0.7836 | no |
| real_gdp_growth | k0 | forecast.dfm_nowcast | benchmark.registry_ar | ae | 378 / 55 | 0.973 | 0.1569 | no |
| real_gdp_growth | k1 | forecast.dfm_nowcast | benchmark.registry_ar | ae | 88 / 53 | 0.963 | 0.1273 | no |
| real_gdp_growth | k0 | forecast.dfm_nowcast_x | benchmark.registry_ar | ae | 360 / 55 | 0.993 | 0.3450 | no |
| real_gdp_growth | k1 | forecast.dfm_nowcast_x | benchmark.registry_ar | ae | 88 / 53 | 0.903 | 0.1327 | no |


## Appendix B — locked holdout, full tables (summaries; every comparison, descriptive except the 30 hypotheses of §5)

#### g7 — holdout (3298 scored cases, 42 origins ['2016-03-01T00:00:00+00:00', '2026-06-01T00:00:00+00:00'])

| variable | horizon | model | n | MAE | CRPS | log | cov80 |
|---|---|---|---|---|---|---|---|
| cpi_inflation | k0 | benchmark.persistence | 271 | 0.561 | 0.432 | -1.425 | 0.75 |
| cpi_inflation | k0 | benchmark.registry_ar | 269 | 0.482 | 0.370 | -1.202 | 0.75 |
| cpi_inflation | k0 | forecast.bvar_minnesota | 270 | 0.599 | 0.452 | -2.919 | 0.77 |
| cpi_inflation | k0 | forecast.direct_ar | 271 | 0.520 | 0.400 | -1.375 | 0.75 |
| cpi_inflation | k0 | forecast.pool_bvar_ar | 270 | 0.527 | 0.401 | -1.807 | 0.80 |
| cpi_inflation | k1 | benchmark.persistence | 265 | 0.970 | 0.757 | -2.074 | 0.70 |
| cpi_inflation | k1 | benchmark.registry_ar | 263 | 0.822 | 0.647 | -1.953 | 0.74 |
| cpi_inflation | k1 | forecast.bvar_minnesota | 264 | 0.938 | 0.726 | -3.667 | 0.72 |
| cpi_inflation | k1 | forecast.direct_ar | 265 | 0.849 | 0.674 | -2.012 | 0.73 |
| cpi_inflation | k1 | forecast.pool_bvar_ar | 264 | 0.855 | 0.672 | -2.767 | 0.77 |
| cpi_inflation | k2 | benchmark.persistence | 258 | 1.346 | 1.060 | -2.587 | 0.69 |
| cpi_inflation | k2 | benchmark.registry_ar | 256 | 1.132 | 0.912 | -2.642 | 0.70 |
| cpi_inflation | k2 | forecast.bvar_minnesota | 257 | 1.243 | 0.981 | -5.871 | 0.70 |
| cpi_inflation | k2 | forecast.direct_ar | 258 | 1.163 | 0.940 | -2.731 | 0.71 |
| cpi_inflation | k2 | forecast.pool_bvar_ar | 257 | 1.167 | 0.928 | -4.189 | 0.74 |
| cpi_inflation | k3 | benchmark.persistence | 251 | 1.675 | 1.335 | -3.022 | 0.66 |
| cpi_inflation | k3 | benchmark.registry_ar | 249 | 1.398 | 1.149 | -3.355 | 0.69 |
| cpi_inflation | k3 | forecast.bvar_minnesota | 250 | 1.463 | 1.183 | -9.106 | 0.70 |
| cpi_inflation | k3 | forecast.direct_ar | 251 | 1.427 | 1.196 | -3.611 | 0.70 |
| cpi_inflation | k3 | forecast.pool_bvar_ar | 250 | 1.387 | 1.139 | -6.298 | 0.73 |
| real_gdp_growth | k0 | benchmark.persistence | 292 | 1.616 | 1.609 | -11.303 | 0.89 |
| real_gdp_growth | k0 | benchmark.registry_ar | 283 | 1.005 | 0.989 | -8.881 | 0.90 |
| real_gdp_growth | k0 | forecast.bvar_minnesota | 282 | 1.255 | 1.137 | -7.870 | 0.90 |
| real_gdp_growth | k0 | forecast.combo_dfm_bvar | 282 | 1.401 | 1.480 | -7.874 | 0.88 |
| real_gdp_growth | k0 | forecast.dfm_nowcast | 292 | 1.746 | 1.620 | -9.822 | 0.88 |
| real_gdp_growth | k0 | forecast.dfm_nowcast_x | 292 | 1.878 | 1.703 | -11.023 | 0.85 |
| real_gdp_growth | k0 | forecast.direct_ar | 292 | 1.619 | 1.484 | -9.411 | 0.88 |
| real_gdp_growth | k0 | forecast.pool_bvar_ar | 282 | 1.320 | 1.190 | -10.239 | 0.90 |
| real_gdp_growth | k1 | benchmark.persistence | 285 | 1.754 | 1.686 | -11.159 | 0.85 |
| real_gdp_growth | k1 | benchmark.registry_ar | 276 | 1.092 | 1.114 | -12.780 | 0.89 |
| real_gdp_growth | k1 | forecast.bvar_minnesota | 276 | 1.220 | 1.145 | -8.629 | 0.88 |
| real_gdp_growth | k1 | forecast.combo_dfm_bvar | 163 | 1.204 | 1.363 | -2.745 | 0.90 |
| real_gdp_growth | k1 | forecast.dfm_nowcast | 164 | 1.490 | 1.424 | -10.297 | 0.90 |
| real_gdp_growth | k1 | forecast.dfm_nowcast_x | 164 | 1.474 | 1.408 | -15.098 | 0.90 |
| real_gdp_growth | k1 | forecast.direct_ar | 285 | 1.522 | 1.415 | -11.700 | 0.89 |
| real_gdp_growth | k1 | forecast.pool_bvar_ar | 276 | 1.337 | 1.201 | -13.850 | 0.90 |
| real_gdp_growth | k2 | benchmark.persistence | 278 | 1.747 | 1.702 | -10.400 | 0.84 |
| real_gdp_growth | k2 | benchmark.registry_ar | 269 | 1.045 | 1.083 | -14.965 | 0.90 |
| real_gdp_growth | k2 | forecast.bvar_minnesota | 269 | 1.210 | 1.148 | -7.450 | 0.88 |
| real_gdp_growth | k2 | forecast.direct_ar | 278 | 1.137 | 1.104 | -14.087 | 0.91 |
| real_gdp_growth | k2 | forecast.pool_bvar_ar | 269 | 1.142 | 1.119 | -10.494 | 0.90 |
| real_gdp_growth | k3 | benchmark.persistence | 271 | 1.819 | 1.755 | -9.539 | 0.82 |
| real_gdp_growth | k3 | benchmark.registry_ar | 262 | 1.104 | 1.142 | -15.135 | 0.87 |
| real_gdp_growth | k3 | forecast.bvar_minnesota | 262 | 1.254 | 1.183 | -6.444 | 0.87 |
| real_gdp_growth | k3 | forecast.direct_ar | 271 | 1.139 | 1.120 | -13.990 | 0.89 |
| real_gdp_growth | k3 | forecast.pool_bvar_ar | 262 | 1.161 | 1.141 | -11.103 | 0.88 |
| unemployment_rate | k0 | benchmark.persistence | 291 | 0.314 | 0.261 | -3.402 | 0.83 |
| unemployment_rate | k0 | benchmark.registry_ar | 287 | 0.304 | 0.248 | -5.090 | 0.76 |
| unemployment_rate | k0 | forecast.bvar_minnesota | 281 | 0.354 | 0.288 | -0.955 | 0.77 |
| unemployment_rate | k0 | forecast.direct_ar | 291 | 0.412 | 0.341 | -5.243 | 0.75 |
| unemployment_rate | k0 | forecast.pool_bvar_ar | 281 | 0.376 | 0.296 | -0.601 | 0.78 |
| unemployment_rate | k1 | benchmark.persistence | 285 | 0.465 | 0.378 | -2.120 | 0.84 |
| unemployment_rate | k1 | benchmark.registry_ar | 281 | 0.455 | 0.356 | -2.769 | 0.79 |
| unemployment_rate | k1 | forecast.bvar_minnesota | 275 | 0.536 | 0.420 | -2.455 | 0.75 |
| unemployment_rate | k1 | forecast.direct_ar | 285 | 0.557 | 0.444 | -2.947 | 0.78 |
| unemployment_rate | k1 | forecast.pool_bvar_ar | 275 | 0.529 | 0.413 | -3.219 | 0.80 |
| unemployment_rate | k2 | benchmark.persistence | 278 | 0.597 | 0.474 | -1.799 | 0.83 |
| unemployment_rate | k2 | benchmark.registry_ar | 274 | 0.600 | 0.457 | -2.133 | 0.77 |
| unemployment_rate | k2 | forecast.bvar_minnesota | 268 | 0.682 | 0.523 | -6.965 | 0.71 |
| unemployment_rate | k2 | forecast.direct_ar | 278 | 0.720 | 0.562 | -2.284 | 0.76 |
| unemployment_rate | k2 | forecast.pool_bvar_ar | 268 | 0.662 | 0.513 | -5.345 | 0.76 |
| unemployment_rate | k3 | benchmark.persistence | 271 | 0.725 | 0.562 | -1.741 | 0.82 |
| unemployment_rate | k3 | benchmark.registry_ar | 267 | 0.719 | 0.544 | -1.981 | 0.76 |
| unemployment_rate | k3 | forecast.bvar_minnesota | 261 | 0.825 | 0.618 | -6.068 | 0.68 |
| unemployment_rate | k3 | forecast.direct_ar | 271 | 0.806 | 0.622 | -2.034 | 0.72 |
| unemployment_rate | k3 | forecast.pool_bvar_ar | 261 | 0.789 | 0.591 | -6.480 | 0.74 |

| variable | horizon | candidate | benchmark | loss | n pairs / origins | relative | p (better) | Holm |
|---|---|---|---|---|---|---|---|---|
| cpi_inflation | k0 | forecast.bvar_minnesota | benchmark.persistence | crps | 270 / 42 | 1.044 | 0.6800 | no |
| cpi_inflation | k1 | forecast.bvar_minnesota | benchmark.persistence | crps | 264 / 41 | 0.956 | 0.3719 | no |
| cpi_inflation | k2 | forecast.bvar_minnesota | benchmark.persistence | crps | 257 / 40 | 0.923 | 0.3094 | no |
| cpi_inflation | k3 | forecast.bvar_minnesota | benchmark.persistence | crps | 250 / 39 | 0.885 | 0.2060 | no |
| real_gdp_growth | k0 | forecast.bvar_minnesota | benchmark.persistence | crps | 282 / 42 | 0.733 | 0.1030 | no |
| real_gdp_growth | k1 | forecast.bvar_minnesota | benchmark.persistence | crps | 276 / 41 | 0.678 | 0.0950 | no |
| real_gdp_growth | k2 | forecast.bvar_minnesota | benchmark.persistence | crps | 269 / 40 | 0.675 | 0.1049 | no |
| real_gdp_growth | k3 | forecast.bvar_minnesota | benchmark.persistence | crps | 262 / 39 | 0.679 | 0.1108 | no |
| unemployment_rate | k0 | forecast.bvar_minnesota | benchmark.persistence | crps | 281 / 42 | 1.096 | 0.9926 | no |
| unemployment_rate | k1 | forecast.bvar_minnesota | benchmark.persistence | crps | 275 / 41 | 1.090 | 0.9886 | no |
| unemployment_rate | k2 | forecast.bvar_minnesota | benchmark.persistence | crps | 268 / 40 | 1.086 | 0.9939 | no |
| unemployment_rate | k3 | forecast.bvar_minnesota | benchmark.persistence | crps | 261 / 39 | 1.076 | 1.0000 | no |
| cpi_inflation | k0 | forecast.direct_ar | benchmark.persistence | crps | 271 / 42 | 0.927 | 0.1604 | no |
| cpi_inflation | k1 | forecast.direct_ar | benchmark.persistence | crps | 265 / 41 | 0.890 | 0.2402 | no |
| cpi_inflation | k2 | forecast.direct_ar | benchmark.persistence | crps | 258 / 40 | 0.887 | 0.2863 | no |
| cpi_inflation | k3 | forecast.direct_ar | benchmark.persistence | crps | 251 / 39 | 0.896 | 0.3063 | no |
| real_gdp_growth | k0 | forecast.direct_ar | benchmark.persistence | crps | 292 / 42 | 0.923 | 0.0000 | yes |
| real_gdp_growth | k1 | forecast.direct_ar | benchmark.persistence | crps | 285 / 41 | 0.839 | 0.0153 | no |
| real_gdp_growth | k2 | forecast.direct_ar | benchmark.persistence | crps | 278 / 40 | 0.648 | 0.0999 | no |
| real_gdp_growth | k3 | forecast.direct_ar | benchmark.persistence | crps | 271 / 39 | 0.638 | 0.1071 | no |
| unemployment_rate | k0 | forecast.direct_ar | benchmark.persistence | crps | 291 / 42 | 1.306 | 0.8575 | no |
| unemployment_rate | k1 | forecast.direct_ar | benchmark.persistence | crps | 285 / 41 | 1.175 | 0.8571 | no |
| unemployment_rate | k2 | forecast.direct_ar | benchmark.persistence | crps | 278 / 40 | 1.187 | 0.8761 | no |
| unemployment_rate | k3 | forecast.direct_ar | benchmark.persistence | crps | 271 / 39 | 1.106 | 0.9591 | no |
| cpi_inflation | k0 | forecast.pool_bvar_ar | benchmark.persistence | crps | 270 / 42 | 0.928 | 0.1675 | no |
| cpi_inflation | k1 | forecast.pool_bvar_ar | benchmark.persistence | crps | 264 / 41 | 0.885 | 0.2189 | no |
| cpi_inflation | k2 | forecast.pool_bvar_ar | benchmark.persistence | crps | 257 / 40 | 0.873 | 0.2395 | no |
| cpi_inflation | k3 | forecast.pool_bvar_ar | benchmark.persistence | crps | 250 / 39 | 0.852 | 0.1985 | no |
| real_gdp_growth | k0 | forecast.pool_bvar_ar | benchmark.persistence | crps | 282 / 42 | 0.767 | 0.0802 | no |
| real_gdp_growth | k1 | forecast.pool_bvar_ar | benchmark.persistence | crps | 276 / 41 | 0.711 | 0.0761 | no |
| real_gdp_growth | k2 | forecast.pool_bvar_ar | benchmark.persistence | crps | 269 / 40 | 0.658 | 0.0988 | no |
| real_gdp_growth | k3 | forecast.pool_bvar_ar | benchmark.persistence | crps | 262 / 39 | 0.654 | 0.1060 | no |
| unemployment_rate | k0 | forecast.pool_bvar_ar | benchmark.persistence | crps | 281 / 42 | 1.126 | 0.8782 | no |
| unemployment_rate | k1 | forecast.pool_bvar_ar | benchmark.persistence | crps | 275 / 41 | 1.070 | 0.8965 | no |
| unemployment_rate | k2 | forecast.pool_bvar_ar | benchmark.persistence | crps | 268 / 40 | 1.066 | 0.9120 | no |
| unemployment_rate | k3 | forecast.pool_bvar_ar | benchmark.persistence | crps | 261 / 39 | 1.030 | 0.8124 | no |
| real_gdp_growth | k0 | forecast.dfm_nowcast | benchmark.persistence | crps | 292 / 42 | 1.007 | 0.5309 | no |
| real_gdp_growth | k1 | forecast.dfm_nowcast | benchmark.persistence | crps | 164 / 40 | 0.813 | 0.0545 | no |
| real_gdp_growth | k0 | forecast.combo_dfm_bvar | benchmark.persistence | crps | 282 / 42 | 0.954 | 0.1534 | no |
| real_gdp_growth | k1 | forecast.combo_dfm_bvar | benchmark.persistence | crps | 163 / 40 | 0.800 | 0.1572 | no |
| cpi_inflation | k0 | forecast.bvar_conformal | benchmark.persistence | crps | 270 / 42 | 1.072 | 0.7984 | no |
| cpi_inflation | k1 | forecast.bvar_conformal | benchmark.persistence | crps | 264 / 41 | 0.979 | 0.4428 | no |
| cpi_inflation | k2 | forecast.bvar_conformal | benchmark.persistence | crps | 257 / 40 | 0.960 | 0.4026 | no |
| cpi_inflation | k3 | forecast.bvar_conformal | benchmark.persistence | crps | 250 / 39 | 0.925 | 0.3040 | no |
| real_gdp_growth | k0 | forecast.bvar_conformal | benchmark.persistence | crps | 282 / 42 | 0.839 | 0.2105 | no |
| real_gdp_growth | k1 | forecast.bvar_conformal | benchmark.persistence | crps | 276 / 41 | 0.789 | 0.1812 | no |
| real_gdp_growth | k2 | forecast.bvar_conformal | benchmark.persistence | crps | 269 / 40 | 0.793 | 0.2005 | no |
| real_gdp_growth | k3 | forecast.bvar_conformal | benchmark.persistence | crps | 262 / 39 | 0.797 | 0.2058 | no |
| unemployment_rate | k0 | forecast.bvar_conformal | benchmark.persistence | crps | 281 / 42 | 1.157 | 1.0000 | no |
| unemployment_rate | k1 | forecast.bvar_conformal | benchmark.persistence | crps | 275 / 41 | 1.144 | 0.9953 | no |
| unemployment_rate | k2 | forecast.bvar_conformal | benchmark.persistence | crps | 268 / 40 | 1.138 | 0.9686 | no |
| unemployment_rate | k3 | forecast.bvar_conformal | benchmark.persistence | crps | 261 / 39 | 1.130 | 0.9177 | no |
| real_gdp_growth | k0 | forecast.dfm_conformal | benchmark.persistence | crps | 292 / 42 | 1.117 | 0.8500 | no |
| real_gdp_growth | k1 | forecast.dfm_conformal | benchmark.persistence | crps | 164 / 40 | 0.934 | 0.1644 | no |
| real_gdp_growth | k0 | forecast.dfm_nowcast_x | benchmark.persistence | crps | 292 / 42 | 1.059 | 0.7103 | no |
| real_gdp_growth | k1 | forecast.dfm_nowcast_x | benchmark.persistence | crps | 164 / 40 | 0.803 | 0.0513 | no |
| cpi_inflation | k0 | forecast.bvar_minnesota | benchmark.registry_ar | crps | 268 / 42 | 1.167 | 0.9935 | no |
| cpi_inflation | k1 | forecast.bvar_minnesota | benchmark.registry_ar | crps | 262 / 41 | 1.070 | 0.9295 | no |
| cpi_inflation | k2 | forecast.bvar_minnesota | benchmark.registry_ar | crps | 255 / 40 | 1.026 | 0.7079 | no |
| cpi_inflation | k3 | forecast.bvar_minnesota | benchmark.registry_ar | crps | 248 / 39 | 0.976 | 0.2774 | no |
| real_gdp_growth | k0 | forecast.bvar_minnesota | benchmark.registry_ar | crps | 274 / 42 | 0.890 | 0.0803 | no |
| real_gdp_growth | k1 | forecast.bvar_minnesota | benchmark.registry_ar | crps | 268 / 41 | 0.956 | 0.1212 | no |
| real_gdp_growth | k2 | forecast.bvar_minnesota | benchmark.registry_ar | crps | 261 / 40 | 0.968 | 0.1229 | no |
| real_gdp_growth | k3 | forecast.bvar_minnesota | benchmark.registry_ar | crps | 254 / 39 | 0.972 | 0.1324 | no |
| unemployment_rate | k0 | forecast.bvar_minnesota | benchmark.registry_ar | crps | 277 / 42 | 1.012 | 0.6276 | no |
| unemployment_rate | k1 | forecast.bvar_minnesota | benchmark.registry_ar | crps | 271 / 41 | 1.022 | 0.7996 | no |
| unemployment_rate | k2 | forecast.bvar_minnesota | benchmark.registry_ar | crps | 264 / 40 | 1.020 | 0.9158 | no |
| unemployment_rate | k3 | forecast.bvar_minnesota | benchmark.registry_ar | crps | 257 / 39 | 1.028 | 1.0000 | no |
| cpi_inflation | k0 | forecast.pool_bvar_ar | benchmark.registry_ar | crps | 268 / 42 | 1.021 | 0.7724 | no |
| cpi_inflation | k1 | forecast.pool_bvar_ar | benchmark.registry_ar | crps | 262 / 41 | 0.982 | 0.3110 | no |
| cpi_inflation | k2 | forecast.pool_bvar_ar | benchmark.registry_ar | crps | 255 / 40 | 0.967 | 0.2291 | no |
| cpi_inflation | k3 | forecast.pool_bvar_ar | benchmark.registry_ar | crps | 248 / 39 | 0.945 | 0.2052 | no |
| real_gdp_growth | k0 | forecast.pool_bvar_ar | benchmark.registry_ar | crps | 274 / 42 | 0.872 | 0.0442 | no |
| real_gdp_growth | k1 | forecast.pool_bvar_ar | benchmark.registry_ar | crps | 268 / 41 | 0.939 | 0.0716 | no |
| real_gdp_growth | k2 | forecast.pool_bvar_ar | benchmark.registry_ar | crps | 261 / 40 | 0.954 | 0.0461 | no |
| real_gdp_growth | k3 | forecast.pool_bvar_ar | benchmark.registry_ar | crps | 254 / 39 | 0.960 | 0.0659 | no |
| unemployment_rate | k0 | forecast.pool_bvar_ar | benchmark.registry_ar | crps | 277 / 42 | 0.960 | 0.0649 | no |
| unemployment_rate | k1 | forecast.pool_bvar_ar | benchmark.registry_ar | crps | 271 / 41 | 0.968 | 0.0442 | no |
| unemployment_rate | k2 | forecast.pool_bvar_ar | benchmark.registry_ar | crps | 264 / 40 | 0.963 | 0.0591 | no |
| unemployment_rate | k3 | forecast.pool_bvar_ar | benchmark.registry_ar | crps | 257 / 39 | 0.970 | 0.0154 | no |
| real_gdp_growth | k0 | forecast.dfm_nowcast | benchmark.registry_ar | crps | 283 / 42 | 0.903 | 0.0390 | no |
| real_gdp_growth | k1 | forecast.dfm_nowcast | benchmark.registry_ar | crps | 156 / 40 | 0.943 | 0.0803 | no |
| real_gdp_growth | k0 | forecast.combo_dfm_bvar | benchmark.registry_ar | crps | 274 / 42 | 0.996 | 0.3648 | no |
| real_gdp_growth | k1 | forecast.combo_dfm_bvar | benchmark.registry_ar | crps | 156 / 40 | 1.097 | 0.5455 | no |
| real_gdp_growth | k0 | forecast.dfm_nowcast_x | benchmark.registry_ar | crps | 283 / 42 | 0.931 | 0.0945 | no |
| real_gdp_growth | k1 | forecast.dfm_nowcast_x | benchmark.registry_ar | crps | 156 / 40 | 0.930 | 0.0696 | no |
| cpi_inflation | k0 | forecast.bvar_minnesota | benchmark.registry_ar | ae | 268 / 42 | 1.198 | 0.9989 | no |
| cpi_inflation | k1 | forecast.bvar_minnesota | benchmark.registry_ar | ae | 262 / 41 | 1.097 | 0.9930 | no |
| cpi_inflation | k2 | forecast.bvar_minnesota | benchmark.registry_ar | ae | 255 / 40 | 1.054 | 0.9308 | no |
| cpi_inflation | k3 | forecast.bvar_minnesota | benchmark.registry_ar | ae | 248 / 39 | 0.998 | 0.4320 | no |
| real_gdp_growth | k0 | forecast.bvar_minnesota | benchmark.registry_ar | ae | 274 / 42 | 0.958 | 0.2511 | no |
| real_gdp_growth | k1 | forecast.bvar_minnesota | benchmark.registry_ar | ae | 268 / 41 | 1.024 | 0.4680 | no |
| real_gdp_growth | k2 | forecast.bvar_minnesota | benchmark.registry_ar | ae | 261 / 40 | 1.030 | 0.4954 | no |
| real_gdp_growth | k3 | forecast.bvar_minnesota | benchmark.registry_ar | ae | 254 / 39 | 1.041 | 0.5701 | no |
| unemployment_rate | k0 | forecast.bvar_minnesota | benchmark.registry_ar | ae | 277 / 42 | 1.010 | 0.5849 | no |
| unemployment_rate | k1 | forecast.bvar_minnesota | benchmark.registry_ar | ae | 271 / 41 | 1.026 | 0.7955 | no |
| unemployment_rate | k2 | forecast.bvar_minnesota | benchmark.registry_ar | ae | 264 / 40 | 1.011 | 0.7493 | no |
| unemployment_rate | k3 | forecast.bvar_minnesota | benchmark.registry_ar | ae | 257 / 39 | 1.035 | 0.9897 | no |
| real_gdp_growth | k0 | forecast.dfm_nowcast | benchmark.registry_ar | ae | 283 / 42 | 0.944 | 0.1638 | no |
| real_gdp_growth | k1 | forecast.dfm_nowcast | benchmark.registry_ar | ae | 156 / 40 | 0.986 | 0.1810 | no |
| real_gdp_growth | k0 | forecast.dfm_nowcast_x | benchmark.registry_ar | ae | 283 / 42 | 1.012 | 0.4434 | no |
| real_gdp_growth | k1 | forecast.dfm_nowcast_x | benchmark.registry_ar | ae | 156 / 40 | 0.981 | 0.1716 | no |

#### us — holdout (775 scored cases, 54 origins ['2013-02-16T00:00:00+00:00', '2026-05-16T00:00:00+00:00'])

| variable | horizon | model | n | MAE | CRPS | log | cov80 |
|---|---|---|---|---|---|---|---|
| gdp_price_inflation | spf2 | benchmark.persistence | 54 | 1.216 | 0.918 | -2.222 | 0.61 |
| gdp_price_inflation | spf2 | benchmark.registry_ar | 54 | 1.194 | 0.919 | -2.170 | 0.72 |
| gdp_price_inflation | spf2 | benchmark.spf_gauss | 54 | 1.003 | 0.726 | -1.758 | 0.81 |
| gdp_price_inflation | spf2 | benchmark.spf_median | 54 | 1.003 | n. a. | n. a. | n. a. |
| gdp_price_inflation | spf2 | forecast.bvar_minnesota | 54 | 1.228 | 0.946 | -2.988 | 0.69 |
| gdp_price_inflation | spf2 | forecast.combo_spf_bvar | 54 | 1.091 | 0.870 | -1.837 | 0.76 |
| gdp_price_inflation | spf2 | forecast.direct_ar | 54 | 1.226 | 0.951 | -2.439 | 0.65 |
| gdp_price_inflation | spf2 | forecast.pool_bvar_ar | 54 | 1.220 | 0.943 | -2.864 | 0.67 |
| gdp_price_inflation | spf2 | forecast.spf_conformal | 54 | 1.003 | 0.746 | -1.634 | 0.80 |
| gdp_price_inflation | spf3 | benchmark.persistence | 53 | 1.437 | 1.099 | -2.561 | 0.60 |
| gdp_price_inflation | spf3 | benchmark.registry_ar | 53 | 1.331 | 1.047 | -2.479 | 0.70 |
| gdp_price_inflation | spf3 | benchmark.spf_gauss | 53 | 1.179 | 0.930 | -2.331 | 0.77 |
| gdp_price_inflation | spf3 | benchmark.spf_median | 53 | 1.179 | n. a. | n. a. | n. a. |
| gdp_price_inflation | spf3 | forecast.bvar_minnesota | 53 | 1.333 | 1.044 | -3.039 | 0.68 |
| gdp_price_inflation | spf3 | forecast.combo_spf_bvar | 53 | 1.237 | 1.051 | -2.162 | 0.75 |
| gdp_price_inflation | spf3 | forecast.direct_ar | 53 | 1.268 | 1.013 | -2.746 | 0.68 |
| gdp_price_inflation | spf3 | forecast.pool_bvar_ar | 53 | 1.297 | 1.025 | -3.738 | 0.68 |
| gdp_price_inflation | spf3 | forecast.spf_conformal | 53 | 1.179 | 0.958 | -2.082 | 0.77 |
| gdp_price_inflation | spf4 | benchmark.persistence | 52 | 1.554 | 1.212 | -2.926 | 0.65 |
| gdp_price_inflation | spf4 | benchmark.registry_ar | 52 | 1.377 | 1.097 | -2.742 | 0.73 |
| gdp_price_inflation | spf4 | benchmark.spf_gauss | 52 | 1.244 | 0.987 | -2.476 | 0.75 |
| gdp_price_inflation | spf4 | benchmark.spf_median | 52 | 1.244 | n. a. | n. a. | n. a. |
| gdp_price_inflation | spf4 | forecast.bvar_minnesota | 52 | 1.393 | 1.098 | -4.766 | 0.71 |
| gdp_price_inflation | spf4 | forecast.combo_spf_bvar | 52 | 1.289 | 1.171 | -2.704 | 0.75 |
| gdp_price_inflation | spf4 | forecast.direct_ar | 52 | 1.306 | 1.059 | -3.103 | 0.69 |
| gdp_price_inflation | spf4 | forecast.pool_bvar_ar | 52 | 1.331 | 1.069 | -4.904 | 0.73 |
| gdp_price_inflation | spf4 | forecast.spf_conformal | 52 | 1.244 | 1.055 | -2.932 | 0.75 |
| gdp_price_inflation | spf5 | benchmark.persistence | 51 | 1.767 | 1.344 | -3.212 | 0.61 |
| gdp_price_inflation | spf5 | benchmark.registry_ar | 51 | 1.478 | 1.163 | -2.983 | 0.73 |
| gdp_price_inflation | spf5 | benchmark.spf_gauss | 51 | 1.311 | 1.055 | -2.763 | 0.76 |
| gdp_price_inflation | spf5 | benchmark.spf_median | 51 | 1.311 | n. a. | n. a. | n. a. |
| gdp_price_inflation | spf5 | forecast.bvar_minnesota | 51 | 1.454 | 1.148 | -7.100 | 0.67 |
| gdp_price_inflation | spf5 | forecast.combo_spf_bvar | 51 | 1.367 | 1.258 | -3.694 | 0.76 |
| gdp_price_inflation | spf5 | forecast.direct_ar | 51 | 1.516 | 1.182 | -3.450 | 0.71 |
| gdp_price_inflation | spf5 | forecast.pool_bvar_ar | 51 | 1.445 | 1.128 | -6.604 | 0.75 |
| gdp_price_inflation | spf5 | forecast.spf_conformal | 51 | 1.311 | 1.133 | -3.685 | 0.76 |
| gdp_price_inflation | spf6 | benchmark.persistence | 50 | 1.861 | 1.490 | -3.580 | 0.60 |
| gdp_price_inflation | spf6 | benchmark.registry_ar | 50 | 1.531 | 1.211 | -3.144 | 0.70 |
| gdp_price_inflation | spf6 | benchmark.spf_gauss | 50 | 1.345 | 1.066 | -2.713 | 0.76 |
| gdp_price_inflation | spf6 | benchmark.spf_median | 50 | 1.345 | n. a. | n. a. | n. a. |
| gdp_price_inflation | spf6 | forecast.bvar_minnesota | 50 | 1.528 | 1.210 | -6.305 | 0.70 |
| gdp_price_inflation | spf6 | forecast.combo_spf_bvar | 50 | 1.404 | 1.366 | -5.017 | 0.76 |
| gdp_price_inflation | spf6 | forecast.direct_ar | 50 | 1.554 | 1.228 | -3.665 | 0.64 |
| gdp_price_inflation | spf6 | forecast.pool_bvar_ar | 50 | 1.483 | 1.176 | -6.861 | 0.72 |
| gdp_price_inflation | spf6 | forecast.spf_conformal | 50 | 1.345 | 1.187 | -3.806 | 0.76 |
| real_gdp_growth | spf2 | benchmark.persistence | 54 | 4.328 | 3.807 | -6.609 | 0.91 |
| real_gdp_growth | spf2 | benchmark.registry_ar | 54 | 2.966 | 2.827 | -6.416 | 0.93 |
| real_gdp_growth | spf2 | benchmark.spf_gauss | 54 | 1.519 | 1.512 | -2.642 | 0.96 |
| real_gdp_growth | spf2 | benchmark.spf_median | 54 | 1.519 | n. a. | n. a. | n. a. |
| real_gdp_growth | spf2 | forecast.bvar_minnesota | 54 | 2.827 | 2.466 | -2.366 | 0.91 |
| real_gdp_growth | spf2 | forecast.combo_spf_bvar | 54 | 2.117 | 2.084 | -24.666 | 0.89 |
| real_gdp_growth | spf2 | forecast.direct_ar | 54 | 2.799 | 2.526 | -6.300 | 0.93 |
| real_gdp_growth | spf2 | forecast.pool_bvar_ar | 54 | 2.789 | 2.484 | -13.625 | 0.91 |
| real_gdp_growth | spf2 | forecast.spf_conformal | 54 | 1.519 | 1.382 | -8.460 | 0.89 |
| real_gdp_growth | spf3 | benchmark.persistence | 53 | 4.168 | 3.802 | -5.792 | 0.89 |
| real_gdp_growth | spf3 | benchmark.registry_ar | 53 | 3.112 | 2.878 | -6.380 | 0.92 |
| real_gdp_growth | spf3 | benchmark.spf_gauss | 53 | 2.528 | 2.392 | -4.253 | 0.94 |
| real_gdp_growth | spf3 | benchmark.spf_median | 53 | 2.528 | n. a. | n. a. | n. a. |
| real_gdp_growth | spf3 | forecast.bvar_minnesota | 53 | 2.713 | 2.427 | -2.390 | 0.92 |
| real_gdp_growth | spf3 | forecast.combo_spf_bvar | 53 | 2.569 | 2.607 | -2.450 | 0.91 |
| real_gdp_growth | spf3 | forecast.direct_ar | 53 | 2.665 | 2.443 | -6.060 | 0.92 |
| real_gdp_growth | spf3 | forecast.pool_bvar_ar | 53 | 2.630 | 2.419 | -14.047 | 0.92 |
| real_gdp_growth | spf3 | forecast.spf_conformal | 53 | 2.528 | 2.384 | -2.603 | 0.91 |
| real_gdp_growth | spf4 | benchmark.persistence | 52 | 4.458 | 4.051 | -5.895 | 0.87 |
| real_gdp_growth | spf4 | benchmark.registry_ar | 52 | 3.064 | 2.864 | -6.407 | 0.90 |
| real_gdp_growth | spf4 | benchmark.spf_gauss | 52 | 2.734 | 2.584 | -4.618 | 0.94 |
| real_gdp_growth | spf4 | benchmark.spf_median | 52 | 2.734 | n. a. | n. a. | n. a. |
| real_gdp_growth | spf4 | forecast.bvar_minnesota | 52 | 2.744 | 2.451 | -13.247 | 0.92 |
| real_gdp_growth | spf4 | forecast.combo_spf_bvar | 52 | 2.691 | 2.755 | -2.464 | 0.90 |
| real_gdp_growth | spf4 | forecast.direct_ar | 52 | 2.836 | 2.532 | -6.081 | 0.90 |
| real_gdp_growth | spf4 | forecast.pool_bvar_ar | 52 | 2.768 | 2.474 | -12.955 | 0.92 |
| real_gdp_growth | spf4 | forecast.spf_conformal | 52 | 2.734 | 2.648 | -2.395 | 0.88 |
| real_gdp_growth | spf5 | benchmark.persistence | 51 | 4.634 | 4.029 | -5.505 | 0.84 |
| real_gdp_growth | spf5 | benchmark.registry_ar | 51 | 3.051 | 2.805 | -6.295 | 0.88 |
| real_gdp_growth | spf5 | benchmark.spf_gauss | 51 | 2.680 | 2.568 | -4.483 | 0.94 |
| real_gdp_growth | spf5 | benchmark.spf_median | 51 | 2.680 | n. a. | n. a. | n. a. |
| real_gdp_growth | spf5 | forecast.bvar_minnesota | 51 | 2.825 | 2.515 | -14.923 | 0.90 |
| real_gdp_growth | spf5 | forecast.combo_spf_bvar | 51 | 2.722 | 2.787 | -2.502 | 0.86 |
| real_gdp_growth | spf5 | forecast.direct_ar | 51 | 2.664 | 2.463 | -6.068 | 0.90 |
| real_gdp_growth | spf5 | forecast.pool_bvar_ar | 51 | 2.720 | 2.480 | -12.642 | 0.90 |
| real_gdp_growth | spf5 | forecast.spf_conformal | 51 | 2.680 | 2.639 | -2.383 | 0.88 |
| real_gdp_growth | spf6 | benchmark.persistence | 50 | 4.940 | 4.278 | -5.934 | 0.84 |
| real_gdp_growth | spf6 | benchmark.registry_ar | 50 | 3.061 | 2.847 | -6.207 | 0.88 |
| real_gdp_growth | spf6 | benchmark.spf_gauss | 50 | 2.737 | 2.629 | -4.605 | 0.92 |
| real_gdp_growth | spf6 | benchmark.spf_median | 50 | 2.737 | n. a. | n. a. | n. a. |
| real_gdp_growth | spf6 | forecast.bvar_minnesota | 50 | 2.718 | 2.496 | -2.389 | 0.90 |
| real_gdp_growth | spf6 | forecast.combo_spf_bvar | 50 | 2.703 | 2.796 | -2.566 | 0.86 |
| real_gdp_growth | spf6 | forecast.direct_ar | 50 | 2.951 | 2.645 | -6.163 | 0.86 |
| real_gdp_growth | spf6 | forecast.pool_bvar_ar | 50 | 2.807 | 2.540 | -13.947 | 0.90 |
| real_gdp_growth | spf6 | forecast.spf_conformal | 50 | 2.737 | 2.730 | -2.506 | 0.84 |
| unemployment_rate | spf2 | benchmark.persistence | 53 | 0.755 | 0.678 | -5.222 | 0.89 |
| unemployment_rate | spf2 | benchmark.registry_ar | 50 | 0.650 | 0.550 | -7.784 | 0.94 |
| unemployment_rate | spf2 | benchmark.spf_gauss | 53 | 0.158 | 0.265 | -1.019 | 0.96 |
| unemployment_rate | spf2 | benchmark.spf_median | 53 | 0.158 | n. a. | n. a. | n. a. |
| unemployment_rate | spf2 | forecast.bvar_minnesota | 53 | 0.774 | 0.689 | -14.810 | 0.91 |
| unemployment_rate | spf2 | forecast.combo_spf_bvar | 53 | 0.373 | 0.344 | -4.097 | 0.92 |
| unemployment_rate | spf2 | forecast.direct_ar | 53 | 1.056 | 0.912 | -8.810 | 0.87 |
| unemployment_rate | spf2 | forecast.pool_bvar_ar | 53 | 0.835 | 0.735 | -10.591 | 0.92 |
| unemployment_rate | spf2 | forecast.spf_conformal | 53 | 0.158 | 0.182 | -9.913 | 0.94 |
| unemployment_rate | spf3 | benchmark.persistence | 52 | 0.978 | 0.841 | -3.649 | 0.81 |
| unemployment_rate | spf3 | benchmark.registry_ar | 49 | 0.858 | 0.688 | -4.418 | 0.88 |
| unemployment_rate | spf3 | benchmark.spf_gauss | 52 | 0.469 | 0.535 | -2.885 | 0.94 |
| unemployment_rate | spf3 | benchmark.spf_median | 52 | 0.469 | n. a. | n. a. | n. a. |
| unemployment_rate | spf3 | forecast.bvar_minnesota | 52 | 0.979 | 0.833 | -10.198 | 0.81 |
| unemployment_rate | spf3 | forecast.combo_spf_bvar | 52 | 0.587 | 0.553 | -1.042 | 0.90 |
| unemployment_rate | spf3 | forecast.direct_ar | 52 | 1.299 | 1.110 | -5.288 | 0.81 |
| unemployment_rate | spf3 | forecast.pool_bvar_ar | 52 | 0.989 | 0.872 | -7.562 | 0.88 |
| unemployment_rate | spf3 | forecast.spf_conformal | 52 | 0.469 | 0.510 | -0.468 | 0.92 |
| unemployment_rate | spf4 | benchmark.persistence | 51 | 1.178 | 0.983 | -3.078 | 0.76 |
| unemployment_rate | spf4 | benchmark.registry_ar | 48 | 1.017 | 0.784 | -3.249 | 0.85 |
| unemployment_rate | spf4 | benchmark.spf_gauss | 51 | 0.658 | 0.679 | -2.624 | 0.92 |
| unemployment_rate | spf4 | benchmark.spf_median | 51 | 0.658 | n. a. | n. a. | n. a. |
| unemployment_rate | spf4 | forecast.bvar_minnesota | 51 | 1.144 | 0.945 | -6.331 | 0.76 |
| unemployment_rate | spf4 | forecast.combo_spf_bvar | 51 | 0.789 | 0.759 | -4.551 | 0.88 |
| unemployment_rate | spf4 | forecast.direct_ar | 51 | 1.408 | 1.173 | -3.868 | 0.86 |
| unemployment_rate | spf4 | forecast.pool_bvar_ar | 51 | 1.171 | 0.969 | -17.627 | 0.86 |
| unemployment_rate | spf4 | forecast.spf_conformal | 51 | 0.658 | 0.718 | -22.078 | 0.90 |
| unemployment_rate | spf5 | benchmark.persistence | 50 | 1.373 | 1.112 | -2.864 | 0.74 |
| unemployment_rate | spf5 | benchmark.registry_ar | 47 | 1.122 | 0.839 | -2.579 | 0.85 |
| unemployment_rate | spf5 | benchmark.spf_gauss | 50 | 0.757 | 0.746 | -2.323 | 0.90 |
| unemployment_rate | spf5 | benchmark.spf_median | 50 | 0.757 | n. a. | n. a. | n. a. |
| unemployment_rate | spf5 | forecast.bvar_minnesota | 50 | 1.298 | 1.033 | -14.099 | 0.80 |
| unemployment_rate | spf5 | forecast.combo_spf_bvar | 50 | 0.939 | 0.907 | -5.924 | 0.86 |
| unemployment_rate | spf5 | forecast.direct_ar | 50 | 1.689 | 1.387 | -3.332 | 0.80 |
| unemployment_rate | spf5 | forecast.pool_bvar_ar | 50 | 1.369 | 1.085 | -8.641 | 0.84 |
| unemployment_rate | spf5 | forecast.spf_conformal | 50 | 0.757 | 0.850 | -28.471 | 0.86 |
| unemployment_rate | spf6 | benchmark.persistence | 49 | 1.526 | 1.207 | -2.765 | 0.76 |
| unemployment_rate | spf6 | benchmark.registry_ar | 46 | 1.218 | 0.879 | -2.307 | 0.83 |
| unemployment_rate | spf6 | benchmark.spf_gauss | 49 | 0.853 | 0.814 | -2.215 | 0.88 |
| unemployment_rate | spf6 | benchmark.spf_median | 49 | 0.853 | n. a. | n. a. | n. a. |
| unemployment_rate | spf6 | forecast.bvar_minnesota | 49 | 1.431 | 1.099 | -10.171 | 0.73 |
| unemployment_rate | spf6 | forecast.combo_spf_bvar | 49 | 1.075 | 1.043 | -2.366 | 0.88 |
| unemployment_rate | spf6 | forecast.direct_ar | 49 | 1.785 | 1.471 | -3.049 | 0.78 |
| unemployment_rate | spf6 | forecast.pool_bvar_ar | 49 | 1.463 | 1.137 | -5.901 | 0.84 |
| unemployment_rate | spf6 | forecast.spf_conformal | 49 | 0.853 | 0.968 | -9.617 | 0.86 |

| variable | horizon | candidate | benchmark | loss | n pairs / origins | relative | p (better) | Holm |
|---|---|---|---|---|---|---|---|---|
| gdp_price_inflation | spf2 | forecast.direct_ar | benchmark.spf_median | ae | 54 / 54 | 1.222 | 0.9554 | no |
| gdp_price_inflation | spf3 | forecast.direct_ar | benchmark.spf_median | ae | 53 / 53 | 1.075 | 0.7950 | no |
| gdp_price_inflation | spf4 | forecast.direct_ar | benchmark.spf_median | ae | 52 / 52 | 1.050 | 0.8961 | no |
| gdp_price_inflation | spf5 | forecast.direct_ar | benchmark.spf_median | ae | 51 / 51 | 1.156 | 0.8814 | no |
| gdp_price_inflation | spf6 | forecast.direct_ar | benchmark.spf_median | ae | 50 / 50 | 1.156 | 0.9050 | no |
| real_gdp_growth | spf2 | forecast.direct_ar | benchmark.spf_median | ae | 54 / 54 | 1.843 | 0.8725 | no |
| real_gdp_growth | spf3 | forecast.direct_ar | benchmark.spf_median | ae | 53 / 53 | 1.054 | 0.6622 | no |
| real_gdp_growth | spf4 | forecast.direct_ar | benchmark.spf_median | ae | 52 / 52 | 1.037 | 0.6267 | no |
| real_gdp_growth | spf5 | forecast.direct_ar | benchmark.spf_median | ae | 51 / 51 | 0.994 | 0.4559 | no |
| real_gdp_growth | spf6 | forecast.direct_ar | benchmark.spf_median | ae | 50 / 50 | 1.078 | 0.7914 | no |
| unemployment_rate | spf2 | forecast.direct_ar | benchmark.spf_median | ae | 53 / 53 | 6.666 | 0.9681 | no |
| unemployment_rate | spf3 | forecast.direct_ar | benchmark.spf_median | ae | 52 / 52 | 2.768 | 0.9274 | no |
| unemployment_rate | spf4 | forecast.direct_ar | benchmark.spf_median | ae | 51 / 51 | 2.140 | 0.9391 | no |
| unemployment_rate | spf5 | forecast.direct_ar | benchmark.spf_median | ae | 50 / 50 | 2.230 | 0.9324 | no |
| unemployment_rate | spf6 | forecast.direct_ar | benchmark.spf_median | ae | 49 / 49 | 2.091 | 0.9325 | no |
| gdp_price_inflation | spf2 | forecast.bvar_minnesota | benchmark.spf_median | ae | 54 / 54 | 1.225 | 0.9774 | no |
| gdp_price_inflation | spf3 | forecast.bvar_minnesota | benchmark.spf_median | ae | 53 / 53 | 1.131 | 0.8824 | no |
| gdp_price_inflation | spf4 | forecast.bvar_minnesota | benchmark.spf_median | ae | 52 / 52 | 1.120 | 0.9791 | no |
| gdp_price_inflation | spf5 | forecast.bvar_minnesota | benchmark.spf_median | ae | 51 / 51 | 1.109 | 0.9975 | no |
| gdp_price_inflation | spf6 | forecast.bvar_minnesota | benchmark.spf_median | ae | 50 / 50 | 1.136 | 0.9612 | no |
| real_gdp_growth | spf2 | forecast.bvar_minnesota | benchmark.spf_median | ae | 54 / 54 | 1.861 | 0.8905 | no |
| real_gdp_growth | spf3 | forecast.bvar_minnesota | benchmark.spf_median | ae | 53 / 53 | 1.073 | 0.7480 | no |
| real_gdp_growth | spf4 | forecast.bvar_minnesota | benchmark.spf_median | ae | 52 / 52 | 1.004 | 0.5265 | no |
| real_gdp_growth | spf5 | forecast.bvar_minnesota | benchmark.spf_median | ae | 51 / 51 | 1.054 | 0.9202 | no |
| real_gdp_growth | spf6 | forecast.bvar_minnesota | benchmark.spf_median | ae | 50 / 50 | 0.993 | 0.4174 | no |
| unemployment_rate | spf2 | forecast.bvar_minnesota | benchmark.spf_median | ae | 53 / 53 | 4.887 | 0.9704 | no |
| unemployment_rate | spf3 | forecast.bvar_minnesota | benchmark.spf_median | ae | 52 / 52 | 2.086 | 0.9617 | no |
| unemployment_rate | spf4 | forecast.bvar_minnesota | benchmark.spf_median | ae | 51 / 51 | 1.739 | 0.9623 | no |
| unemployment_rate | spf5 | forecast.bvar_minnesota | benchmark.spf_median | ae | 50 / 50 | 1.714 | 0.9623 | no |
| unemployment_rate | spf6 | forecast.bvar_minnesota | benchmark.spf_median | ae | 49 / 49 | 1.677 | 0.9732 | no |
| gdp_price_inflation | spf2 | forecast.combo_spf_bvar | benchmark.spf_median | ae | 54 / 54 | 1.088 | 0.9295 | no |
| gdp_price_inflation | spf3 | forecast.combo_spf_bvar | benchmark.spf_median | ae | 53 / 53 | 1.049 | 0.8212 | no |
| gdp_price_inflation | spf4 | forecast.combo_spf_bvar | benchmark.spf_median | ae | 52 / 52 | 1.037 | 0.9479 | no |
| gdp_price_inflation | spf5 | forecast.combo_spf_bvar | benchmark.spf_median | ae | 51 / 51 | 1.042 | 0.9999 | no |
| gdp_price_inflation | spf6 | forecast.combo_spf_bvar | benchmark.spf_median | ae | 50 / 50 | 1.044 | 0.9456 | no |
| real_gdp_growth | spf2 | forecast.combo_spf_bvar | benchmark.spf_median | ae | 54 / 54 | 1.393 | 0.8670 | no |
| real_gdp_growth | spf3 | forecast.combo_spf_bvar | benchmark.spf_median | ae | 53 / 53 | 1.016 | 0.6609 | no |
| real_gdp_growth | spf4 | forecast.combo_spf_bvar | benchmark.spf_median | ae | 52 / 52 | 0.984 | 0.2571 | no |
| real_gdp_growth | spf5 | forecast.combo_spf_bvar | benchmark.spf_median | ae | 51 / 51 | 1.016 | 0.8191 | no |
| real_gdp_growth | spf6 | forecast.combo_spf_bvar | benchmark.spf_median | ae | 50 / 50 | 0.988 | 0.2370 | no |
| unemployment_rate | spf2 | forecast.combo_spf_bvar | benchmark.spf_median | ae | 53 / 53 | 2.354 | 0.9551 | no |
| unemployment_rate | spf3 | forecast.combo_spf_bvar | benchmark.spf_median | ae | 52 / 52 | 1.250 | 0.8600 | no |
| unemployment_rate | spf4 | forecast.combo_spf_bvar | benchmark.spf_median | ae | 51 / 51 | 1.200 | 0.8701 | no |
| unemployment_rate | spf5 | forecast.combo_spf_bvar | benchmark.spf_median | ae | 50 / 50 | 1.239 | 0.9108 | no |
| unemployment_rate | spf6 | forecast.combo_spf_bvar | benchmark.spf_median | ae | 49 / 49 | 1.260 | 0.9475 | no |
| gdp_price_inflation | spf2 | forecast.combo_spf_ar | benchmark.spf_median | ae | 54 / 54 | 1.072 | 0.8736 | no |
| gdp_price_inflation | spf3 | forecast.combo_spf_ar | benchmark.spf_median | ae | 53 / 53 | 1.025 | 0.7048 | no |
| gdp_price_inflation | spf4 | forecast.combo_spf_ar | benchmark.spf_median | ae | 52 / 52 | 1.024 | 0.8768 | no |
| gdp_price_inflation | spf5 | forecast.combo_spf_ar | benchmark.spf_median | ae | 51 / 51 | 1.055 | 0.8894 | no |
| gdp_price_inflation | spf6 | forecast.combo_spf_ar | benchmark.spf_median | ae | 50 / 50 | 1.051 | 0.8823 | no |
| real_gdp_growth | spf2 | forecast.combo_spf_ar | benchmark.spf_median | ae | 54 / 54 | 1.383 | 0.8481 | no |
| real_gdp_growth | spf3 | forecast.combo_spf_ar | benchmark.spf_median | ae | 53 / 53 | 0.978 | 0.3453 | no |
| real_gdp_growth | spf4 | forecast.combo_spf_ar | benchmark.spf_median | ae | 52 / 52 | 0.999 | 0.4929 | no |
| real_gdp_growth | spf5 | forecast.combo_spf_ar | benchmark.spf_median | ae | 51 / 51 | 0.978 | 0.2115 | no |
| real_gdp_growth | spf6 | forecast.combo_spf_ar | benchmark.spf_median | ae | 50 / 50 | 1.031 | 0.7378 | no |
| unemployment_rate | spf2 | forecast.combo_spf_ar | benchmark.spf_median | ae | 53 / 53 | 3.237 | 0.9533 | no |
| unemployment_rate | spf3 | forecast.combo_spf_ar | benchmark.spf_median | ae | 52 / 52 | 1.541 | 0.9021 | no |
| unemployment_rate | spf4 | forecast.combo_spf_ar | benchmark.spf_median | ae | 51 / 51 | 1.370 | 0.9607 | no |
| unemployment_rate | spf5 | forecast.combo_spf_ar | benchmark.spf_median | ae | 50 / 50 | 1.454 | 0.9392 | no |
| unemployment_rate | spf6 | forecast.combo_spf_ar | benchmark.spf_median | ae | 49 / 49 | 1.431 | 0.9381 | no |
| gdp_price_inflation | spf2 | forecast.spf_conformal | benchmark.spf_gauss | crps | 54 / 54 | 1.027 | 0.8786 | no |
| gdp_price_inflation | spf3 | forecast.spf_conformal | benchmark.spf_gauss | crps | 53 / 53 | 1.030 | 0.8725 | no |
| gdp_price_inflation | spf4 | forecast.spf_conformal | benchmark.spf_gauss | crps | 52 / 52 | 1.068 | 0.8838 | no |
| gdp_price_inflation | spf5 | forecast.spf_conformal | benchmark.spf_gauss | crps | 51 / 51 | 1.074 | 0.8946 | no |
| gdp_price_inflation | spf6 | forecast.spf_conformal | benchmark.spf_gauss | crps | 50 / 50 | 1.113 | 0.9073 | no |
| real_gdp_growth | spf2 | forecast.spf_conformal | benchmark.spf_gauss | crps | 54 / 54 | 0.914 | 0.0000 | yes |
| real_gdp_growth | spf3 | forecast.spf_conformal | benchmark.spf_gauss | crps | 53 / 53 | 0.997 | 0.4621 | no |
| real_gdp_growth | spf4 | forecast.spf_conformal | benchmark.spf_gauss | crps | 52 / 52 | 1.025 | 0.7524 | no |
| real_gdp_growth | spf5 | forecast.spf_conformal | benchmark.spf_gauss | crps | 51 / 51 | 1.027 | 0.7214 | no |
| real_gdp_growth | spf6 | forecast.spf_conformal | benchmark.spf_gauss | crps | 50 / 50 | 1.038 | 0.7657 | no |
| unemployment_rate | spf2 | forecast.spf_conformal | benchmark.spf_gauss | crps | 53 / 53 | 0.688 | 0.0000 | yes |
| unemployment_rate | spf3 | forecast.spf_conformal | benchmark.spf_gauss | crps | 52 / 52 | 0.952 | 0.1454 | no |
| unemployment_rate | spf4 | forecast.spf_conformal | benchmark.spf_gauss | crps | 51 / 51 | 1.058 | 0.7682 | no |
| unemployment_rate | spf5 | forecast.spf_conformal | benchmark.spf_gauss | crps | 50 / 50 | 1.140 | 0.8668 | no |
| unemployment_rate | spf6 | forecast.spf_conformal | benchmark.spf_gauss | crps | 49 / 49 | 1.189 | 0.8720 | no |
| gdp_price_inflation | spf2 | forecast.combo_spf_bvar | benchmark.spf_gauss | crps | 54 / 54 | 1.198 | 0.9978 | no |
| gdp_price_inflation | spf3 | forecast.combo_spf_bvar | benchmark.spf_gauss | crps | 53 / 53 | 1.130 | 0.9690 | no |
| gdp_price_inflation | spf4 | forecast.combo_spf_bvar | benchmark.spf_gauss | crps | 52 / 52 | 1.186 | 0.9648 | no |
| gdp_price_inflation | spf5 | forecast.combo_spf_bvar | benchmark.spf_gauss | crps | 51 / 51 | 1.192 | 0.9643 | no |
| gdp_price_inflation | spf6 | forecast.combo_spf_bvar | benchmark.spf_gauss | crps | 50 / 50 | 1.281 | 0.9721 | no |
| real_gdp_growth | spf2 | forecast.combo_spf_bvar | benchmark.spf_gauss | crps | 54 / 54 | 1.378 | 0.8585 | no |
| real_gdp_growth | spf3 | forecast.combo_spf_bvar | benchmark.spf_gauss | crps | 53 / 53 | 1.090 | 0.8853 | no |
| real_gdp_growth | spf4 | forecast.combo_spf_bvar | benchmark.spf_gauss | crps | 52 / 52 | 1.066 | 0.8933 | no |
| real_gdp_growth | spf5 | forecast.combo_spf_bvar | benchmark.spf_gauss | crps | 51 / 51 | 1.085 | 0.8845 | no |
| real_gdp_growth | spf6 | forecast.combo_spf_bvar | benchmark.spf_gauss | crps | 50 / 50 | 1.063 | 0.8225 | no |
| unemployment_rate | spf2 | forecast.combo_spf_bvar | benchmark.spf_gauss | crps | 53 / 53 | 1.298 | 0.7657 | no |
| unemployment_rate | spf3 | forecast.combo_spf_bvar | benchmark.spf_gauss | crps | 52 / 52 | 1.033 | 0.6363 | no |
| unemployment_rate | spf4 | forecast.combo_spf_bvar | benchmark.spf_gauss | crps | 51 / 51 | 1.118 | 0.8510 | no |
| unemployment_rate | spf5 | forecast.combo_spf_bvar | benchmark.spf_gauss | crps | 50 / 50 | 1.216 | 0.8995 | no |
| unemployment_rate | spf6 | forecast.combo_spf_bvar | benchmark.spf_gauss | crps | 49 / 49 | 1.281 | 0.8935 | no |
| gdp_price_inflation | spf2 | forecast.combo_spf_ar | benchmark.spf_gauss | crps | 54 / 54 | 1.168 | 0.9954 | no |
| gdp_price_inflation | spf3 | forecast.combo_spf_ar | benchmark.spf_gauss | crps | 53 / 53 | 1.082 | 0.9466 | no |
| gdp_price_inflation | spf4 | forecast.combo_spf_ar | benchmark.spf_gauss | crps | 52 / 52 | 1.132 | 0.9399 | no |
| gdp_price_inflation | spf5 | forecast.combo_spf_ar | benchmark.spf_gauss | crps | 51 / 51 | 1.193 | 0.9625 | no |
| gdp_price_inflation | spf6 | forecast.combo_spf_ar | benchmark.spf_gauss | crps | 50 / 50 | 1.264 | 0.9698 | no |
| real_gdp_growth | spf2 | forecast.combo_spf_ar | benchmark.spf_gauss | crps | 54 / 54 | 1.320 | 0.8074 | no |
| real_gdp_growth | spf3 | forecast.combo_spf_ar | benchmark.spf_gauss | crps | 53 / 53 | 1.031 | 0.6569 | no |
| real_gdp_growth | spf4 | forecast.combo_spf_ar | benchmark.spf_gauss | crps | 52 / 52 | 1.030 | 0.7182 | no |
| real_gdp_growth | spf5 | forecast.combo_spf_ar | benchmark.spf_gauss | crps | 51 / 51 | 1.022 | 0.6785 | no |
| real_gdp_growth | spf6 | forecast.combo_spf_ar | benchmark.spf_gauss | crps | 50 / 50 | 1.063 | 0.7928 | no |
| unemployment_rate | spf2 | forecast.combo_spf_ar | benchmark.spf_gauss | crps | 53 / 53 | 1.768 | 0.8612 | no |
| unemployment_rate | spf3 | forecast.combo_spf_ar | benchmark.spf_gauss | crps | 52 / 52 | 1.347 | 0.8656 | no |
| unemployment_rate | spf4 | forecast.combo_spf_ar | benchmark.spf_gauss | crps | 51 / 51 | 1.322 | 0.9256 | no |
| unemployment_rate | spf5 | forecast.combo_spf_ar | benchmark.spf_gauss | crps | 50 / 50 | 1.485 | 0.9217 | no |
| unemployment_rate | spf6 | forecast.combo_spf_ar | benchmark.spf_gauss | crps | 49 / 49 | 1.557 | 0.9048 | no |
| gdp_price_inflation | spf2 | forecast.bvar_minnesota | benchmark.persistence | crps | 54 / 54 | 1.031 | 0.5948 | no |
| gdp_price_inflation | spf3 | forecast.bvar_minnesota | benchmark.persistence | crps | 53 / 53 | 0.950 | 0.3671 | no |
| gdp_price_inflation | spf4 | forecast.bvar_minnesota | benchmark.persistence | crps | 52 / 52 | 0.906 | 0.2226 | no |
| gdp_price_inflation | spf5 | forecast.bvar_minnesota | benchmark.persistence | crps | 51 / 51 | 0.854 | 0.1252 | no |
| gdp_price_inflation | spf6 | forecast.bvar_minnesota | benchmark.persistence | crps | 50 / 50 | 0.812 | 0.0740 | no |
| real_gdp_growth | spf2 | forecast.bvar_minnesota | benchmark.persistence | crps | 54 / 54 | 0.648 | 0.0822 | no |
| real_gdp_growth | spf3 | forecast.bvar_minnesota | benchmark.persistence | crps | 53 / 53 | 0.638 | 0.1045 | no |
| real_gdp_growth | spf4 | forecast.bvar_minnesota | benchmark.persistence | crps | 52 / 52 | 0.605 | 0.1113 | no |
| real_gdp_growth | spf5 | forecast.bvar_minnesota | benchmark.persistence | crps | 51 / 51 | 0.624 | 0.0975 | no |
| real_gdp_growth | spf6 | forecast.bvar_minnesota | benchmark.persistence | crps | 50 / 50 | 0.583 | 0.0945 | no |
| unemployment_rate | spf2 | forecast.bvar_minnesota | benchmark.persistence | crps | 53 / 53 | 1.016 | 0.5787 | no |
| unemployment_rate | spf3 | forecast.bvar_minnesota | benchmark.persistence | crps | 52 / 52 | 0.991 | 0.4549 | no |
| unemployment_rate | spf4 | forecast.bvar_minnesota | benchmark.persistence | crps | 51 / 51 | 0.961 | 0.2801 | no |
| unemployment_rate | spf5 | forecast.bvar_minnesota | benchmark.persistence | crps | 50 / 50 | 0.929 | 0.1635 | no |
| unemployment_rate | spf6 | forecast.bvar_minnesota | benchmark.persistence | crps | 49 / 49 | 0.910 | 0.1560 | no |
| gdp_price_inflation | spf2 | forecast.direct_ar | benchmark.persistence | crps | 54 / 54 | 1.036 | 0.6001 | no |
| gdp_price_inflation | spf3 | forecast.direct_ar | benchmark.persistence | crps | 53 / 53 | 0.922 | 0.3137 | no |
| gdp_price_inflation | spf4 | forecast.direct_ar | benchmark.persistence | crps | 52 / 52 | 0.874 | 0.1937 | no |
| gdp_price_inflation | spf5 | forecast.direct_ar | benchmark.persistence | crps | 51 / 51 | 0.880 | 0.1154 | no |
| gdp_price_inflation | spf6 | forecast.direct_ar | benchmark.persistence | crps | 50 / 50 | 0.824 | 0.0250 | no |
| real_gdp_growth | spf2 | forecast.direct_ar | benchmark.persistence | crps | 54 / 54 | 0.664 | 0.0729 | no |
| real_gdp_growth | spf3 | forecast.direct_ar | benchmark.persistence | crps | 53 / 53 | 0.642 | 0.0974 | no |
| real_gdp_growth | spf4 | forecast.direct_ar | benchmark.persistence | crps | 52 / 52 | 0.625 | 0.1008 | no |
| real_gdp_growth | spf5 | forecast.direct_ar | benchmark.persistence | crps | 51 / 51 | 0.611 | 0.0884 | no |
| real_gdp_growth | spf6 | forecast.direct_ar | benchmark.persistence | crps | 50 / 50 | 0.618 | 0.0797 | no |
| unemployment_rate | spf2 | forecast.direct_ar | benchmark.persistence | crps | 53 / 53 | 1.345 | 0.8653 | no |
| unemployment_rate | spf3 | forecast.direct_ar | benchmark.persistence | crps | 52 / 52 | 1.321 | 0.7911 | no |
| unemployment_rate | spf4 | forecast.direct_ar | benchmark.persistence | crps | 51 / 51 | 1.194 | 0.7542 | no |
| unemployment_rate | spf5 | forecast.direct_ar | benchmark.persistence | crps | 50 / 50 | 1.247 | 0.7784 | no |
| unemployment_rate | spf6 | forecast.direct_ar | benchmark.persistence | crps | 49 / 49 | 1.218 | 0.7521 | no |
| gdp_price_inflation | spf2 | forecast.pool_bvar_ar | benchmark.persistence | crps | 54 / 54 | 1.027 | 0.5801 | no |
| gdp_price_inflation | spf3 | forecast.pool_bvar_ar | benchmark.persistence | crps | 53 / 53 | 0.933 | 0.3315 | no |
| gdp_price_inflation | spf4 | forecast.pool_bvar_ar | benchmark.persistence | crps | 52 / 52 | 0.882 | 0.1951 | no |
| gdp_price_inflation | spf5 | forecast.pool_bvar_ar | benchmark.persistence | crps | 51 / 51 | 0.840 | 0.1193 | no |
| gdp_price_inflation | spf6 | forecast.pool_bvar_ar | benchmark.persistence | crps | 50 / 50 | 0.789 | 0.0610 | no |
| real_gdp_growth | spf2 | forecast.pool_bvar_ar | benchmark.persistence | crps | 54 / 54 | 0.653 | 0.0764 | no |
| real_gdp_growth | spf3 | forecast.pool_bvar_ar | benchmark.persistence | crps | 53 / 53 | 0.636 | 0.1001 | no |
| real_gdp_growth | spf4 | forecast.pool_bvar_ar | benchmark.persistence | crps | 52 / 52 | 0.611 | 0.1053 | no |
| real_gdp_growth | spf5 | forecast.pool_bvar_ar | benchmark.persistence | crps | 51 / 51 | 0.615 | 0.0919 | no |
| real_gdp_growth | spf6 | forecast.pool_bvar_ar | benchmark.persistence | crps | 50 / 50 | 0.594 | 0.0877 | no |
| unemployment_rate | spf2 | forecast.pool_bvar_ar | benchmark.persistence | crps | 53 / 53 | 1.085 | 0.7561 | no |
| unemployment_rate | spf3 | forecast.pool_bvar_ar | benchmark.persistence | crps | 52 / 52 | 1.038 | 0.5990 | no |
| unemployment_rate | spf4 | forecast.pool_bvar_ar | benchmark.persistence | crps | 51 / 51 | 0.986 | 0.4430 | no |
| unemployment_rate | spf5 | forecast.pool_bvar_ar | benchmark.persistence | crps | 50 / 50 | 0.975 | 0.3831 | no |
| unemployment_rate | spf6 | forecast.pool_bvar_ar | benchmark.persistence | crps | 49 / 49 | 0.942 | 0.2345 | no |

#### weo — holdout (12054 scored cases, 14 origins ['2019-06-01T00:00:00+00:00', '2025-12-01T00:00:00+00:00'])

| variable | horizon | model | n | MAE | CRPS | log | cov80 |
|---|---|---|---|---|---|---|---|
| cpi_inflation | h0 | benchmark.imf_weo | 2625 | 3881.890 | n. a. | n. a. | n. a. |
| cpi_inflation | h0 | benchmark.imf_weo_gauss | 2609 | 3905.610 | 3898.400 | -4.324 | 0.97 |
| cpi_inflation | h0 | benchmark.persistence | 2609 | 387.392 | 376.420 | -3.884 | 0.86 |
| cpi_inflation | h0 | benchmark.registry_ar | 2579 | 20.907 | 27.593 | -3.768 | 0.84 |
| cpi_inflation | h0 | forecast.combo_weo_ar | 2589 | 282589.000 | 282436.000 | -7.434 | 0.67 |
| cpi_inflation | h0 | forecast.direct_ar | 2589 | 561243.000 | 530329.000 | -3.776 | 0.85 |
| cpi_inflation | h0 | forecast.weo_bias_corrected | 2609 | 3904.040 | 3885.880 | -3.136 | 0.71 |
| cpi_inflation | h0 | forecast.weo_conformal | 2609 | 3905.610 | 3884.000 | -3.173 | 0.70 |
| cpi_inflation | h1 | benchmark.imf_weo | 2240 | 4703.400 | n. a. | n. a. | n. a. |
| cpi_inflation | h1 | benchmark.imf_weo_gauss | 2222 | 4741.210 | 4727.200 | -5.440 | 0.91 |
| cpi_inflation | h1 | benchmark.persistence | 2222 | 473.664 | 456.657 | -4.349 | 0.83 |
| cpi_inflation | h1 | benchmark.registry_ar | 2202 | 25.826 | 29.244 | -4.149 | 0.81 |
| cpi_inflation | h1 | forecast.combo_weo_ar | 2210 | 621141.000 | 620889.000 | -8.504 | 0.57 |
| cpi_inflation | h1 | forecast.direct_ar | 2210 | 1237520.000 | 1158790.000 | -4.214 | 0.81 |
| cpi_inflation | h1 | forecast.weo_bias_corrected | 2226 | 4728.720 | 4692.240 | -7.312 | 0.62 |
| cpi_inflation | h1 | forecast.weo_conformal | 2226 | 4732.690 | 4683.600 | -7.532 | 0.61 |
| real_gdp_growth | h0 | benchmark.imf_weo | 2508 | 1.861 | n. a. | n. a. | n. a. |
| real_gdp_growth | h0 | benchmark.imf_weo_gauss | 2502 | 1.859 | 1.958 | -2.671 | 0.95 |
| real_gdp_growth | h0 | benchmark.persistence | 2502 | 5.049 | 4.175 | -3.843 | 0.75 |
| real_gdp_growth | h0 | benchmark.registry_ar | 2475 | 4.093 | 3.243 | -3.620 | 0.75 |
| real_gdp_growth | h0 | forecast.combo_weo_ar | 2482 | 2.593 | 2.081 | -3.905 | 0.72 |
| real_gdp_growth | h0 | forecast.direct_ar | 2482 | 4.066 | 3.206 | -3.492 | 0.77 |
| real_gdp_growth | h0 | forecast.weo_bias_corrected | 2502 | 1.858 | 1.495 | -3.052 | 0.75 |
| real_gdp_growth | h0 | forecast.weo_conformal | 2502 | 1.859 | 1.499 | -3.054 | 0.75 |
| real_gdp_growth | h1 | benchmark.imf_weo | 2122 | 3.644 | n. a. | n. a. | n. a. |
| real_gdp_growth | h1 | benchmark.imf_weo_gauss | 2112 | 3.639 | 3.103 | -3.200 | 0.85 |
| real_gdp_growth | h1 | benchmark.persistence | 2112 | 5.993 | 4.711 | -3.845 | 0.68 |
| real_gdp_growth | h1 | benchmark.registry_ar | 2095 | 4.318 | 3.378 | -3.608 | 0.75 |
| real_gdp_growth | h1 | forecast.combo_weo_ar | 2100 | 3.721 | 2.997 | -4.228 | 0.69 |
| real_gdp_growth | h1 | forecast.direct_ar | 2100 | 4.311 | 3.378 | -3.564 | 0.76 |
| real_gdp_growth | h1 | forecast.weo_bias_corrected | 2116 | 3.583 | 2.978 | -4.082 | 0.72 |
| real_gdp_growth | h1 | forecast.weo_conformal | 2116 | 3.638 | 2.916 | -3.952 | 0.71 |
| unemployment_rate | h0 | benchmark.imf_weo | 1387 | 0.924 | n. a. | n. a. | n. a. |
| unemployment_rate | h0 | benchmark.imf_weo_gauss | 1387 | 0.924 | 0.808 | -2.024 | 0.86 |
| unemployment_rate | h0 | benchmark.persistence | 1387 | 1.094 | 0.891 | -2.086 | 0.82 |
| unemployment_rate | h0 | benchmark.registry_ar | 1345 | 1.175 | 0.903 | -2.379 | 0.76 |
| unemployment_rate | h0 | forecast.combo_weo_ar | 1374 | 0.967 | 0.716 | -3.937 | 0.74 |
| unemployment_rate | h0 | forecast.direct_ar | 1374 | 1.184 | 0.913 | -2.293 | 0.77 |
| unemployment_rate | h0 | forecast.weo_bias_corrected | 1387 | 0.905 | 0.747 | -2.913 | 0.74 |
| unemployment_rate | h0 | forecast.weo_conformal | 1387 | 0.924 | 0.743 | -2.899 | 0.74 |
| unemployment_rate | h1 | benchmark.imf_weo | 1172 | 1.405 | n. a. | n. a. | n. a. |
| unemployment_rate | h1 | benchmark.imf_weo_gauss | 1172 | 1.405 | 1.165 | -2.282 | 0.82 |
| unemployment_rate | h1 | benchmark.persistence | 1172 | 1.718 | 1.359 | -2.436 | 0.79 |
| unemployment_rate | h1 | benchmark.registry_ar | 1134 | 1.969 | 1.461 | -2.727 | 0.68 |
| unemployment_rate | h1 | forecast.combo_weo_ar | 1159 | 1.597 | 1.169 | -2.812 | 0.72 |
| unemployment_rate | h1 | forecast.direct_ar | 1159 | 1.979 | 1.459 | -2.691 | 0.70 |
| unemployment_rate | h1 | forecast.weo_bias_corrected | 1172 | 1.392 | 1.093 | -3.706 | 0.75 |
| unemployment_rate | h1 | forecast.weo_conformal | 1172 | 1.405 | 1.106 | -3.678 | 0.74 |

| variable | horizon | candidate | benchmark | loss | n pairs / origins | relative | p (better) | Holm |
|---|---|---|---|---|---|---|---|---|
| cpi_inflation | h0 | forecast.direct_ar | benchmark.imf_weo | ae | 2589 / 14 | 142.603 | 0.8205 | no |
| cpi_inflation | h1 | forecast.direct_ar | benchmark.imf_weo | ae | 2210 / 12 | 259.613 | 0.7929 | no |
| real_gdp_growth | h0 | forecast.direct_ar | benchmark.imf_weo | ae | 2482 / 14 | 2.238 | 0.9946 | no |
| real_gdp_growth | h1 | forecast.direct_ar | benchmark.imf_weo | ae | 2100 / 12 | 1.198 | 0.9992 | no |
| unemployment_rate | h0 | forecast.direct_ar | benchmark.imf_weo | ae | 1374 / 14 | 1.276 | 0.9639 | no |
| unemployment_rate | h1 | forecast.direct_ar | benchmark.imf_weo | ae | 1159 / 12 | 1.404 | 0.9834 | no |
| cpi_inflation | h0 | forecast.combo_weo_ar | benchmark.imf_weo | ae | 2589 / 14 | 71.802 | 0.8205 | no |
| cpi_inflation | h1 | forecast.combo_weo_ar | benchmark.imf_weo | ae | 2210 / 12 | 130.306 | 0.7929 | no |
| real_gdp_growth | h0 | forecast.combo_weo_ar | benchmark.imf_weo | ae | 2482 / 14 | 1.427 | 0.9959 | no |
| real_gdp_growth | h1 | forecast.combo_weo_ar | benchmark.imf_weo | ae | 2100 / 12 | 1.034 | 0.9109 | no |
| unemployment_rate | h0 | forecast.combo_weo_ar | benchmark.imf_weo | ae | 1374 / 14 | 1.042 | 0.6510 | no |
| unemployment_rate | h1 | forecast.combo_weo_ar | benchmark.imf_weo | ae | 1159 / 12 | 1.133 | 0.9076 | no |
| cpi_inflation | h0 | forecast.combo_weo_ar_persist | benchmark.imf_weo | ae | 2589 / 14 | 47.901 | 0.8206 | no |
| cpi_inflation | h1 | forecast.combo_weo_ar_persist | benchmark.imf_weo | ae | 2210 / 12 | 86.904 | 0.7929 | no |
| real_gdp_growth | h0 | forecast.combo_weo_ar_persist | benchmark.imf_weo | ae | 2482 / 14 | 1.707 | 0.9845 | no |
| real_gdp_growth | h1 | forecast.combo_weo_ar_persist | benchmark.imf_weo | ae | 2100 / 12 | 1.154 | 0.9415 | no |
| unemployment_rate | h0 | forecast.combo_weo_ar_persist | benchmark.imf_weo | ae | 1374 / 14 | 1.044 | 0.6335 | no |
| unemployment_rate | h1 | forecast.combo_weo_ar_persist | benchmark.imf_weo | ae | 1159 / 12 | 1.132 | 0.8627 | no |
| cpi_inflation | h0 | forecast.weo_bias_corrected | benchmark.imf_weo | ae | 2609 / 14 | 1.000 | 0.1652 | no |
| cpi_inflation | h1 | forecast.weo_bias_corrected | benchmark.imf_weo | ae | 2226 / 12 | 0.999 | 0.1968 | no |
| real_gdp_growth | h0 | forecast.weo_bias_corrected | benchmark.imf_weo | ae | 2502 / 14 | 0.999 | 0.2974 | no |
| real_gdp_growth | h1 | forecast.weo_bias_corrected | benchmark.imf_weo | ae | 2116 / 12 | 0.985 | 0.3215 | no |
| unemployment_rate | h0 | forecast.weo_bias_corrected | benchmark.imf_weo | ae | 1387 / 14 | 0.979 | 0.0001 | yes |
| unemployment_rate | h1 | forecast.weo_bias_corrected | benchmark.imf_weo | ae | 1172 / 12 | 0.991 | 0.1806 | no |
| cpi_inflation | h0 | forecast.weo_conformal | benchmark.imf_weo_gauss | crps | 2609 / 14 | 0.996 | 0.1924 | no |
| cpi_inflation | h1 | forecast.weo_conformal | benchmark.imf_weo_gauss | crps | 2222 / 12 | 0.993 | 0.0001 | yes |
| real_gdp_growth | h0 | forecast.weo_conformal | benchmark.imf_weo_gauss | crps | 2502 / 14 | 0.765 | 0.0005 | yes |
| real_gdp_growth | h1 | forecast.weo_conformal | benchmark.imf_weo_gauss | crps | 2112 / 12 | 0.939 | 0.0767 | no |
| unemployment_rate | h0 | forecast.weo_conformal | benchmark.imf_weo_gauss | crps | 1387 / 14 | 0.919 | 0.0054 | yes |
| unemployment_rate | h1 | forecast.weo_conformal | benchmark.imf_weo_gauss | crps | 1172 / 12 | 0.949 | 0.0359 | no |
| cpi_inflation | h0 | forecast.weo_bias_corrected | benchmark.imf_weo_gauss | crps | 2609 / 14 | 0.997 | 0.2414 | no |
| cpi_inflation | h1 | forecast.weo_bias_corrected | benchmark.imf_weo_gauss | crps | 2222 / 12 | 0.994 | 0.0000 | yes |
| real_gdp_growth | h0 | forecast.weo_bias_corrected | benchmark.imf_weo_gauss | crps | 2502 / 14 | 0.763 | 0.0004 | yes |
| real_gdp_growth | h1 | forecast.weo_bias_corrected | benchmark.imf_weo_gauss | crps | 2112 / 12 | 0.960 | 0.2456 | no |
| unemployment_rate | h0 | forecast.weo_bias_corrected | benchmark.imf_weo_gauss | crps | 1387 / 14 | 0.924 | 0.0064 | yes |
| unemployment_rate | h1 | forecast.weo_bias_corrected | benchmark.imf_weo_gauss | crps | 1172 / 12 | 0.938 | 0.0578 | no |
| cpi_inflation | h0 | forecast.combo_weo_ar | benchmark.imf_weo_gauss | crps | 2589 / 14 | 71.896 | 0.8205 | no |
| cpi_inflation | h1 | forecast.combo_weo_ar | benchmark.imf_weo_gauss | crps | 2210 / 12 | 130.639 | 0.7929 | no |
| real_gdp_growth | h0 | forecast.combo_weo_ar | benchmark.imf_weo_gauss | crps | 2482 / 14 | 1.087 | 0.6559 | no |
| real_gdp_growth | h1 | forecast.combo_weo_ar | benchmark.imf_weo_gauss | crps | 2100 / 12 | 0.977 | 0.2525 | no |
| unemployment_rate | h0 | forecast.combo_weo_ar | benchmark.imf_weo_gauss | crps | 1374 / 14 | 0.883 | 0.0664 | no |
| unemployment_rate | h1 | forecast.combo_weo_ar | benchmark.imf_weo_gauss | crps | 1159 / 12 | 1.000 | 0.4925 | no |
| cpi_inflation | h0 | forecast.direct_ar | benchmark.persistence | crps | 2589 / 14 | 1398.630 | 0.8204 | no |
| cpi_inflation | h1 | forecast.direct_ar | benchmark.persistence | crps | 2210 / 12 | 2524.820 | 0.7928 | no |
| real_gdp_growth | h0 | forecast.direct_ar | benchmark.persistence | crps | 2482 / 14 | 0.779 | 0.0782 | no |
| real_gdp_growth | h1 | forecast.direct_ar | benchmark.persistence | crps | 2100 / 12 | 0.723 | 0.0759 | no |
| unemployment_rate | h0 | forecast.direct_ar | benchmark.persistence | crps | 1374 / 14 | 1.021 | 0.7833 | no |
| unemployment_rate | h1 | forecast.direct_ar | benchmark.persistence | crps | 1159 / 12 | 1.068 | 0.9938 | no |
| cpi_inflation | h0 | forecast.direct_ar_conformal | benchmark.persistence | crps | 2589 / 14 | 1479.400 | 0.8205 | no |
| cpi_inflation | h1 | forecast.direct_ar_conformal | benchmark.persistence | crps | 2210 / 12 | 2695.370 | 0.7929 | no |
| real_gdp_growth | h0 | forecast.direct_ar_conformal | benchmark.persistence | crps | 2482 / 14 | 0.803 | 0.0607 | no |
| real_gdp_growth | h1 | forecast.direct_ar_conformal | benchmark.persistence | crps | 2100 / 12 | 0.736 | 0.0621 | no |
| unemployment_rate | h0 | forecast.direct_ar_conformal | benchmark.persistence | crps | 1374 / 14 | 0.994 | 0.4132 | no |
| unemployment_rate | h1 | forecast.direct_ar_conformal | benchmark.persistence | crps | 1159 / 12 | 1.033 | 0.9858 | no |
| cpi_inflation | h0 | forecast.weo_conformal | benchmark.persistence | crps | 2609 / 14 | 10.318 | 0.8134 | no |
| cpi_inflation | h1 | forecast.weo_conformal | benchmark.persistence | crps | 2222 / 12 | 10.275 | 0.7910 | no |
| real_gdp_growth | h0 | forecast.weo_conformal | benchmark.persistence | crps | 2502 / 14 | 0.359 | 0.0044 | yes |
| real_gdp_growth | h1 | forecast.weo_conformal | benchmark.persistence | crps | 2112 / 12 | 0.619 | 0.0332 | no |
| unemployment_rate | h0 | forecast.weo_conformal | benchmark.persistence | crps | 1387 / 14 | 0.834 | 0.0414 | no |
| unemployment_rate | h1 | forecast.weo_conformal | benchmark.persistence | crps | 1172 / 12 | 0.814 | 0.0675 | no |
