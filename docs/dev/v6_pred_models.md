# V6 PRED-MODELS — forecasting models on honest vintage backtests

Agent PRED-MODELS, 2026-10-17. Plan: `docs/roadmap/v6_plan.md`; method rules: `docs/roadmap/v5_prediction_engine.md`
(decisions 2 and 4), `docs/strategy/world_map.md` §5. Data: PRED-VINTAGE's vintage tables (`docs/dev/v5_pred_vintage.md`).
Registry: PRED-REGISTRY (`docs/dev/v5_pred_registry.md`). Not financial advice.

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
| Development stage, protocol, locked holdout (opened once, ledger), registration, DB manifest | `src/reva/evaluation/pred_models/service.py`, `cli.py` |
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

