# Review V6 — are any of REVA's forecasting claims inflated? (QA-PRED)

Agent QA-PRED (independent adversarial reviewer), 2026-10-17. Read-only: no code changed, nothing written to the
dev DB (every DB access was a `SET TRANSACTION READ ONLY` session with the `api` role; re-runs wrote only to the
agent's scratchpad). Scope: `docs/dev/v5_pred_*.md`, `docs/dev/v6_pred_*.md` (as they landed), `src/reva/evaluation/**`,
`src/reva/models/forecasting/**`, `registry/forecasts/**`, `registry/predict/validated_chains.yaml`.

Verdict scale: **credible** (claim holds as worded; caveats only) · **overstated** (direction holds, wording or size
inflated, or a design choice tilts it) · **invalid** (the claim as worded is not supported). Severity of each
finding: H / M / L.

## 0. Result in one paragraph

No reported number was fabricated: every backtest I re-ran reproduced the reported figures to the printed digit
(WEO vs persistence, G20 subset, SPF vs no-change, PRED-MODELS WEO holdout cell, the 17 PRED-ENERGY winners,
PRED-FIN ratios, PRED-CONFLICT "no winner"). The look-ahead machinery (vintage accessor with conservative
availability instants, leakage guard, `outcome_known_at` gating of conformal errors) is sound and its tests pass.
**The inflation risk is in the wording, not the arithmetic:** (1) the only holdout-confirmed "wins" (PRED-MODELS) are
calibrated *densities wrapped around the IMF's and the SPF's own point forecasts*, beaten against a REVA-built
strawman density whose 80 % interval covers 95–99 % of outcomes — they must never be described as "REVA beats the
IMF/SPF"; (2) no REVA model beats IMF WEO, the SPF median or EIA STEO on point accuracy anywhere; (3) REVA's own
econometric models (direct AR, BVAR, DFM, pools) passed 0 of 16 holdout hypotheses; (4) 11 of the 17 energy
"winners" are an equal-weight average with EIA's own projection and are worse than EIA's projection alone;
(5) the prospective registry has sealed thousands of forecasts and resolved **0**. Several design issues would
inflate future claims if left as they are (cycle-anchored targets + table-relative prospectivity check; post-hoc
void path; point-vs-density CRPS in the conflict–VIEWS comparison; candidate-only final-vintage covariates in
conflict; Holm families per variable). Exact fixes are listed per item.

## 1. What I re-ran (read-only) and what reproduced

Inputs: all vintage cells (`atlas.vintage_*`, 2,233,400 cells) dumped read-only to the scratchpad; atlas tables via
the services' own read-only loaders.

| Backtest | Reported | Re-run (QA-PRED) | Match |
|---|---|---|---|
| WEO vs persistence, GDP h0 / h1 (`vintage_backtests.weo_backtest`) | n 6,617 / 6,220; MAE 1.53 vs 3.61 / 2.83 vs 4.18; mean error h1 +1.24 | 6,617 / 6,220; 1.5337 vs 3.6136 / 2.8321 vs 4.1807; +1.2443 | exact |
| same, G20 h0 / h1 | 684 / 646; rel. MAE 0.29 / 0.59 | 684 / 646; 0.2921 / 0.5927 | exact |
| WEO CPI h0 | median AE 0.62 vs 1.62; MAE 1,719 vs 172 | 0.621 vs 1.621; **1,718.9 vs 171.8 (relative MAE 10.0)** | exact |
| SPF median GDP vs no-change, h2..h6 (`spf_backtest`) | rel. MAE 0.46 / 0.64 / 0.67 / 0.65 / 0.62 | 0.4604 / 0.6429 / 0.6715 / 0.6506 / 0.6202 | exact |
| PRED-MODELS holdout, WEO GDP h0, `weo_conformal` vs `imf_weo_gauss` (CRPS) | rel. 0.765, p 0.00053, 14 origins | rebuilt cases from vintages: rel. 0.765, p 0.00053, 14 origins | exact |
| PRED-ENERGY STEO archive backtest (`reva.cli.energy_backtest`, 54 issues 2013-07..2026-10) | 17 winners (COPRPUS, PAPR_WORLD) | same 17 (code before the `ae` addition; digest differs only by that) | exact |
| PRED-FIN subset (EUR/USD month end, 2s10s, FOMC hike, BoE hike) | GARCH EUR/USD 12 m 0.985*, EH 2s10s 1 m 0.976*, Taylor FOMC hike 0.62 (p 0.016), BoE momentum 0.82* | 0.985 (p 2.8e-6), 0.976 (p 0.0075), 0.620 (p 0.016), 0.822 (p 0.006) | exact |
| PRED-CONFLICT (`conflict.backtest.run_backtest` on the live panel) | (report not yet written) | **0 winners**; VIEWS runs loaded: **0** (no VIEWS comparison exists) | n/a |
| Prospective registry (`model.evaluation`) | 586+ questions, 2,287+ forecasts sealed, 0 resolved | rows: `registry_issue` 15, `registry_backtest` 4, `registry_score` **0** | exact |

Unit tests run: `tests/unit/test_vintages.py`, `tests/unit/test_forecast_registry.py` — 22 passed.

## 2. Cross-cutting findings

| # | Finding | Sev. | Where | Fix |
|---|---|---|---|---|
| X1 | **Strawman density benchmarks.** `benchmark.imf_weo_gauss` / `benchmark.spf_gauss` = N(official point, σ of REVA's *persistence* predictive). On the WEO GDP h0 holdout their 80 % interval covers 0.93–1.00 of outcomes at every origin except 2020-06 and 2021-06 (0.77, 0.87) (re-run per origin); `spf_gauss` covers 0.96. Beating them shows only that REVA spreads the official point better than a naive spread, not that REVA forecasts better than the IMF/SPF. | H | `pred_models/realtime.py:gauss_around` | Benchmark = N(point, σ = RMS of the official forecaster's *own past errors* known at the origin) (the standard error-based fan); re-test on the prospective registry (the locked holdout is spent). Word every claim as "calibrated density around the IMF/SPF point". |
| X2 | **CRPS of a point forecast = its absolute error**, so any density centred on the same point "beats" it. Fixed by PRED-ENERGY in `07e194e` (STEO now compared on AE/SE); **still present** in PRED-CONFLICT's VIEWS comparison (`crps: abs(v - y)` for VIEWS vs REVA sample CRPS). | H (conflict) | `evaluation/conflict/backtest.py` (VIEWS block) | Compare REVA with VIEWS on point losses only (AE, MSLE of the REVA median), never CRPS. |
| X3 | **Holm families narrower than the claim.** Energy: Holm per variable (60 tests) while 7 variables (334 tests) are reported together; under one global Holm 10 of 17 winners survive, and only 4 if the EIA-combination is excluded. Finance: Holm per model, none across the 4 candidates (harmless today: 0 winners). Health: Bonferroni over baselines, none over the 3 candidates. My finance subset re-run shows the sensitivity: with 4 families instead of 13, the Taylor logit becomes "Holm-significant" for FOMC and BoE hikes — the pre-declared full family is the one that counts. | M | `energy_backtests.py:_summaries`, `finance_backtest.py:holm_by_model`, `health_registry/core.py:run_backtest` | Declare the family as "every candidate × cell that can lead to a registration in this run" (one Holm per backtest), fixed before the run, and print the family size next to each winner. |
| X4 | **No holdout outside PRED-MODELS.** Energy, finance, conflict and health winners are selected and their size measured on the same sample (winner's curse). | M | all `winners` rules | Treat in-sample winners as *admission to the registry*, never as evidence of skill; report skill only from resolved prospective questions (or a locked holdout as in PRED-MODELS). |
| X5 | **Dev/holdout target overlap.** PRED-MODELS splits on the origin date only: dev origin 2018-12-01 h1 targets 2019, the holdout's 2019-06/12 h0 cases target the same year (same outcome); same for the US task (dev SPF h6 from 2012 targets 2013–14). | L | `pred_models/service.py:build_cases`, `gate.split_of` | Purge/embargo: dev cases must have `target < split` (or `outcome_known_at < split`). |
| X6 | **Pooled cross-country means on inflation are meaningless** (hyperinflation): WEO CPI MAE 1,719 vs persistence 172 (WEO *10× worse* on MAE, better on median); PRED-MODELS holdout direct AR CPI CRPS 1,399× persistence, combos MAE up to 1.2 million. | M | `vintage_backtests`, PRED-MODELS WEO CPI cells | Score inflation on `100·ln(1+π/100)` or per country (median of per-country ratios); never publish a pooled-mean CPI ratio or a median-only headline. |
| X7 | **Same-institution outcomes.** WEO forecasts are scored against the WEO's own October t+1 estimate (for many low-capacity countries an IMF staff estimate). Flatters WEO and any WEO-wrapped model. | M | `weo_backtest`, `pred_models/realtime.weo_cases` | Robustness run with independent outturns where they exist (national accounts via OECD/Eurostat first releases for advanced economies) and report the advanced-economy subset separately. |
| X8 | **Conservative availability rules can weaken a baseline.** The RTDSM "middle month + 1" rule makes the SPF no-change baseline one quarter staler than what panelists knew (inflates SPF's advantage) and makes REVA's US models one quarter staler than the SPF (deflates REVA vs SPF). | L/M | `spf_backtest`, `pred_models/realtime.us_cases` | Use the RTDSM vintage *of the survey quarter* (data as of mid-middle-month, before the SPF release date) for both the baseline and the models. Effect measured below (§3.C). |
| X9 | **Final-vintage inputs only on the candidate side** inflate the candidate. Conflict: the KG candidate alone uses V-Dem v16 and current WDI (both re-estimated/revised back in time); baselines use UCDP history only. Health: FluNet/ERVISS backfill favours AR-type candidates (clean recent weeks) over climatology. | M | `conflict/models.features`, health `final_vintage_tasks` | Conflict: V-Dem/WDI version available at the origin (archived versions) or declare the candidate ineligible as PRED-MODELS did with `dfm_nowcast_x`. Health: admit candidates only from the as-of hub families; final-vintage families informational. |
| X10 | **Registry prospectivity is table-relative, targets are cycle-anchored.** Monthly/quarterly targets are `cycle anchor + h` (`questions.generate_questions`), and the only prospectivity check (issue and resolution) is "the pinned issue table already holds the value". A family first issued late in a cycle (the weekly job picks up new families) can ask about a period that has *ended* and whose value is *already published by the provider* if REVA's table version is older; it would then resolve as "prospective". Horizon labels are then also wrong (h counted from the anchor, not the seal). | M | `evaluation/registry/questions.py`, `service.resolve_batch` | At issue: refuse targets with `period_end ≤ issued_at` unless the family is declared a nowcast, compute horizons from `issued_at`. At resolution: mark `not_prospective` when the source cell's provider publication/as-of date (`flags.as_of`, release date) ≤ `issued_at`. |
| X11 | **Post-hoc voids.** FIX4B (`fd04e85`) adds `void_questions` (void by rule, reason required). Used for the 5 WEO fiscal-year questions (outcome-independent defect: fine), but nothing stops a void after the outcome is visible, i.e. selective deletion of bad forecasts. | M | `evaluation/registry/service.py:void_questions` | Allow only before `period_end` (or before any later table version exists), or require that the void manifest also records what every model's score *would have been* and keep those in the leaderboard audit. |
| X12 | **Leaderboards on per-model question sets.** Models abstain on different questions (e.g. seasonal naive on annual series); absolute means per model are on different sets. The ratios vs persistence are on common cases (correct). | L | `registry/scores.py`, UI leaderboard | Show absolute means only on the common set, or always next to `n`. |
| X13 | **DM dependence.** PRED-MODELS averages loss differentials per origin before DM (correct for cross-sectional correlation). PRED-CONFLICT clusters by country and ignores within-year cross-country correlation (global shocks): z too large. Health uses a fixed lag 3 for every horizon (adequate for hub horizons 0–3). Energy and finance HAC lags match the overlap. | M (conflict) | `conflict/stats.dm_test` | Average differentials per origin year (as PRED-MODELS) or two-way cluster (country, year). |

## 3. Verdicts per model / family

### A. PRED-VINTAGE — real-time benchmark backtests (`src/reva/evaluation/vintage_backtests.py`)

| Claim | Verdict | Evidence | Exact fix |
|---|---|---|---|
| IMF WEO beats persistence on real GDP growth (h0 rel. MAE 0.42; h1 0.68; G20 0.29 / 0.59) | **credible** (a benchmark fact, not REVA skill) | reproduced exactly; persistence uses the same vintage; outcomes from a later vintage with `outcome_known_at` | add the X7 robustness run; state persistence is a weak naive |
| WEO CPI "median AE 0.62 vs 1.62 … means dominated by hyperinflation" | **overstated** if quoted as "WEO beats persistence on inflation" | re-run: on MAE WEO is **10× worse** (1,718.9 vs 171.8; mean error +1,701) | X6: log-transform or per-country scoring; print both |
| SPF median beats no-change (rel. MAE 0.46…0.62) | **overstated (mildly)** | stale baseline (X8). Re-run with the RTDSM vintage of the survey quarter as no-change: h2 **0.509** (reported 0.460), h3 0.648 (0.643), h4 0.705 (0.672), h5 0.680 (0.651), h6 0.644 (0.620) | X8; re-publish the table |

### B. PRED-REGISTRY — prospective registry and final-vintage backtest (`src/reva/evaluation/registry/**`)

| Item | Verdict | Evidence | Exact fix |
|---|---|---|---|
| Sealing (manifest sha256 = seal, verified on every read, 10-significant-digit strings, DB clock `issued_at`, table pins) | **credible** | code read; manifests re-verified by `ledger.load_frozen_manifest` | — |
| Resolution "first table version after the seal publishing the cell as an observation"; projection cells never outcomes; `not_prospective`; void after `void_after` | **credible with gaps** | X10 (table-relative prospectivity, cycle-anchored targets), X11 (post-hoc void path) | X10, X11 |
| Final-vintage pseudo-real-time backtest (28,965 replays) — "no baseline reliably better than persistence; AR helps GDP and property prices; intervals under-cover" | **credible** (claims are modest and labelled) | design: rolling origins, fit only on periods ≤ origin, horizon includes publication lag; label `revised_data_retrospective` | for WEO CPI quote only the CRPS/log-scale ratio (AR MAE 3.43 vs CRPS 0.75 is X6) |
| "2,287 forecasts sealed" | **credible**; **0 resolved** — no prospective skill claim is possible yet | `model.evaluation`: 0 `registry_score` rows | — |

### C. PRED-MODELS — locked-holdout model tournament (`src/reva/evaluation/pred_models/**`, `registry/forecasts/pred_models/**`)

Process check: splits fixed since the first commit (`d9ee983`); protocol (30 hypotheses, code sha256
`c8b6063a…`) committed at 20:15:36 (`53bd6da`) before the holdout opening logged at 20:20:35; the current code digest
equals the protocol's; ledger refuses a second opening; Holm over all 30 jointly; coverage band for densities;
`dfm_nowcast_x` (final-vintage inputs) declared ineligible. This is the best-designed evaluation in REVA.

| Claim / scope | Verdict | Evidence | Exact fix |
|---|---|---|---|
| Registered: `weo_conformal`, WEO GDP h0 (holdout CRPS rel. 0.765 vs `imf_weo_gauss`, p 0.0005, cov80 0.745) | **credible but narrow; overstated if called "beats the IMF"** | reproduced; point = WEO point (AE identical); benchmark cov80 0.93–1.00 at 12 of 14 origins (X1); conformal cov80 0.37 / 0.49 / 0.50 at the 2020-06, 2020-12, 2021-06 origins | X1 wording + new benchmark; publish per-origin coverage |
| Registered: `weo_bias_corrected`, WEO GDP h0 (rel. 0.763) | **credible but narrow** | its point is not better than WEO's: holdout AE ratio **0.999** (p 0.30) | describe as a density, not a better point |
| Registered: `spf_conformal`, US GDP current quarter (rel. 0.914, cov80 0.889) | **credible but narrow** | `spf_gauss` cov80 0.963; point = SPF median | X1 |
| Holm-rejected but not registered (WEO CPI h1 cov80 0.61; US unemployment spf2 cov80 0.943) | **correctly not registered** | coverage band rule | — |
| REVA's own models vs persistence (direct AR, BVAR, pools: 16 hypotheses) | **none confirmed** — any claim that REVA's econometric models beat naive forecasts is **invalid** today | holdout p 0.07–0.82 (GDP ratios 0.58–0.78 point in the right direction but are underpowered; CPI direct AR 1,399× worse) | more holdout data via the registry; X6 for CPI |
| REVA models vs WEO / SPF on points (AE) | **REVA loses everywhere** | holdout AE ratios vs SPF: GDP 0.98–1.86 (none significantly below 1), unemployment 1.20–6.67, inflation 1.02–1.23; vs WEO GDP 1.03–2.24 (direct AR h0 2.24, combo h0 1.43). One raw signal that cannot be claimed: `weo_bias_corrected` unemployment h0 AE 0.979 vs WEO (holdout p 7e-5) was not a pre-registered hypothesis (it failed development) — a candidate for the next protocol, not a result | X8 (the info handicap explains part of the US gap; it is not inflation) |
| G7 task (BVAR / DFM nowcasts vs AR) | **no dev hypothesis** — no claim | holdout AE ratios 0.94–1.20, none significant | — |
| Dev/holdout separation | **minor leak** | X5 | X5 |

### D. PRED-ENERGY — STEO archive vintage backtest (`energy_backtests.py`, `energy_models.py`, `registry/forecasts/energy_winners.yaml`)

Design: real-time information sets from 54 archived STEO issues (2013-07..2026-10, conservative availability =
day after release, checked against `Last-Modified`); outcomes final vintage (labelled); DM-HLN with overlap lags;
Holm per variable. Fixed during this review (`07e194e`): EIA's points are compared on AE/SE, not CRPS.

| Claim | Verdict | Evidence | Exact fix |
|---|---|---|---|
| 17 "winners" vs the random walk (U.S. crude production h1–8, world liquids h1–4) | **overstated** | X3: under one Holm over all 334 candidate×cell tests 10 survive; excluding `combo_steo_rw` (half EIA's number) only **4** remain: `ar_diff` COPRPUS h2, PAPR_WORLD h2–3, `combo_models` PAPR_WORLD h1 (CRPS ratios 0.83 / 0.91 / 0.89 / 0.92, but 80 % coverage only 0.61–0.68: a better CRPS with mis-calibrated intervals). 11 of 17 are `combo_steo_rw`, which is **worse than STEO alone** on points (COPRPUS h4 MAE 0.445 vs STEO 0.337) and under-covers (cov80 0.67–0.71) | one Holm per run; label `combo_steo_rw` as "EIA-derived"; do not issue it where STEO alone has lower point loss |
| Any REVA model beats EIA STEO | **invalid** | vs STEO on squared error: 1 raw p < 0.05 of 334 tests (COPR_WORLD h1, n = 8); pure-REVA winners have higher RMSE than STEO (COPRPUS h2 0.490 vs 0.357; PAPR_WORLD h3 2.86 vs 2.67). On CRPS 128 of 334 "significant" — the X2 artefact | never compare STEO on CRPS (done in code; keep it out of the report) |
| Prices (Brent, WTI, Henry Hub): no REVA model beats the random walk | **credible** (negative result) | inventory / ARDL CRPS ratios 1.01–1.29; STEO itself MAE ratio 0.95–1.05 on oil, 0.76–0.83 on Henry Hub 3–12 m | — |
| Information set "as published" | **credible with caveat** | quantity series' recent months in each issue are EIA *estimates* (`published_history_boundary_unknown`), so `rw`/`ar_diff` partly ride on EIA's nowcast | state it in the report; prefer per-series `lastHistoricalPeriod` where the workbook gives it |
| COPR_WORLD results | **not evidential** | n = 4–8 origins | drop from any table or mark n |

### E. PRED-FIN — finance families (`finance_backtest.py`, `finance_models.py`, `docs/dev/v6_pred_fin.md`)

| Claim | Verdict | Evidence | Exact fix |
|---|---|---|---|
| "No candidate significantly beats the benchmarks on any (family, horizon) cell" | **credible** | re-run subset reproduces the ratios; intersection-union over benchmarks; final-vintage labelled; inputs lagged (BIS 7 d, HICP 20 d, IMF CPI 45 d) | — |
| GARCH tightens 12-month EUR/USD intervals (0.985*, vs RW only) | **credible** as worded (vs RW, not vs the forward proxy) | — | — |
| Winner rule tightened after a 3-family trial | **acceptable** (tightened, i.e. conservative; disclosed) | report §5 | keep the trial's numbers in the manifest |
| Meeting backtest validates the meeting questions | **overstated** | backtest = fixed 42-day windows from month ends; questions = "next scheduled meeting, level on decision day + 7" | backtest on actual historical meeting dates (Fed/ECB archives) before any meeting candidate is admitted |
| Benchmarks are "strong" | **overstated** (stated by the agent) | no market-implied benchmark (futures/OIS/forwards are licensed) — the benchmark REVA would most likely lose to is absent | owner decision already listed; never claim skill on meetings/FX without it |

### F. PRED-CONFLICT — country-year conflict risk (`src/reva/evaluation/conflict/**`)

Report `docs/dev/v6_pred_conflict.md` not yet landed at writing time (`docs/dev/v6_conflict.md` is V6CONFLICT's data
report). Re-run on the live panel: **0 winners**; `forecast.conflict_kg_logit` Brier/CRPS ratios to persistence
0.95–1.13 (e.g. onset h2 0.951, escalation h1 1.133); **0 VIEWS runs loaded**, so no VIEWS comparison exists.

| Item | Verdict | Evidence | Exact fix |
|---|---|---|---|
| Training "as of" origin (targets ≤ origin) | **credible** | `run_backtest` train filter `t ≤ t0 − h` | — |
| Any future "REVA vs VIEWS" CRPS claim | **invalid by design** | X2 (VIEWS point CRPS = AE); information tilt in REVA's favour (stated in code) | X2: point losses only; restate the information tilt |
| KG candidate advantage | **would be overstated** | X9 (V-Dem v16 / current WDI only in the candidate) | X9 |
| DM significance | **overstated** | X13 (no within-year dependence) | X13 |
| Registry h = 1 question (target 2026, sealed Oct 2026) | **label issue** | ~80 % of the target year elapsed at the seal; backtest h = 1 assumed end-of-year origin | call it a nowcast of the current year; score separately |

### G. PRED-HEALTH — health registry (`src/reva/evaluation/health_registry/**`, `registry/forecasts/health_question_specs.yaml`)

Report not yet landed at writing time; code review only.

| Item | Verdict | Evidence | Exact fix |
|---|---|---|---|
| As-of hub backtests (NHSN flu/COVID): round data vintage, hub ensemble attributed, same truth for all | **credible design** | `as_of_tasks` uses the vintage dated R − 7 (flu) / R − 3 (COVID) | — |
| Final-vintage families (FluNet, ERVISS, WHO GHE) as a basis for admitting candidates | **would be overstated** | X9 (backfill); GHE values are *modelled estimates re-estimated each release*, so "forecasting GHE" is forecasting a model's output | admit candidates only from as-of families; label GHE questions "WHO model estimate" |
| Winner rule (WIS < every baseline; DM p < α / #baselines) | **acceptable**, multiplicity across candidates/families uncorrected (X3) | `run_backtest` | X3 |
| Question timing (targets from the issue week) | **credible** | `round_of` uses the issue date (unlike X10) | — |

### H. Other V5 prediction pieces

| Piece | Verdict | Note |
|---|---|---|
| PRED-ENGINE (`registry/predict/validated_chains.yaml` empty; every result « scénario non validé — pas une probabilité ») | **credible** (no forecast claim) | the ECB +50 bp numbers are a linear ×2 scaling of a +25 bp meta-analysis (stated); keep it out of any accuracy table |
| PRED-EVENTSTUDY (6 local projections, 5 written) | **credible as associations** | writing rule declared before the first write; final-vintage; LV replication marginal (Spearman 0.756 vs 0.75); earthquake M ≥ 7 lead −3 = +1.0 (t ≈ 1.8) — not forecasts, never to be cited as predictive skill |
| PRED-EFFECT, PRED-HF | **no forecasting claim** | data layers |

## 4. Honest scoreboard — where REVA beats benchmarks, where it doesn't

*Real-time = inputs as published at each origin (provider vintages); final-vintage = today's revised data.
"REVA" means a model REVA built; official forecasts (IMF, SPF, EIA, VIEWS, CDC hubs) are benchmarks.*

**REVA beats a benchmark (holdout or real-time, significance after correction)**

| What | Against | Size | Evidence quality | Honest wording |
|---|---|---|---|---|
| Calibrated density around the IMF WEO current-year GDP projection | naive Gaussian (WEO point, persistence spread) | CRPS −23 % | locked holdout 2019–2025, 14 origins | "REVA gives the IMF's own number a better-calibrated uncertainty band"; the point is the IMF's; under-covers in crises (37–50 % at 2020–21 origins) |
| Calibrated density around the SPF current-quarter GDP median | naive Gaussian (SPF point, persistence spread) | CRPS −9 % | locked holdout 2013–2026, 54 origins | same as above, for the SPF |
| AR on changes, U.S. crude production 2 months; world liquids 2–3 months; model average, world liquids 1 month | random walk | CRPS −8 to −17 % (0.83, 0.91, 0.89, 0.92) | real-time STEO vintages, in-sample selection (no holdout), survives one global Holm | beats a random walk, **still worse than EIA's STEO on points, and its 80 % intervals cover only 61–68 %** |

**REVA does not beat the benchmark**

| Domain | Result |
|---|---|
| Macro points (GDP, inflation, unemployment) | No REVA model beats IMF WEO or the SPF median on point accuracy (pre-registered tests): holdout AE ratios 0.98–6.7 vs SPF, 1.03–2.24 vs WEO GDP |
| REVA econometric models vs naive | 0 of 16 holdout hypotheses (direct AR, BVAR, pools vs persistence) confirmed; G7 nowcasts: no development hypothesis |
| Energy | Nothing beats EIA STEO on points; nothing beats the random walk on Brent, WTI, Henry Hub |
| Finance | 0 winners: FX (EUR vs USD/GBP/JPY/CHF/CNY), 10y and 2s10s Treasury yields, Brent, FOMC/ECB/BoE moves; structural models (PPP, Nelson-Siegel) are worse than the random walk |
| Conflict | 0 winners vs persistence / base rate / history-only logit; no VIEWS comparison loaded |
| Health | backtest numbers not yet published (pending PRED-HEALTH report) |
| Prospective registry | thousands of sealed forecasts, **0 resolved**: no prospective evidence of any kind yet |

**Benchmarks that beat naive forecasts (not REVA's merit, useful as references):** IMF WEO vs persistence (GDP h0
MAE −58 %); SPF vs no-change (−49 % current quarter with the fair baseline); EIA STEO vs random walk on U.S. crude
production (MAE −23 to −43 %) and Henry Hub 3–12 months (−17 to −24 %); EH forward of the 2s10s spread at 1 month;
rate-cycle momentum for BoE hikes.

## 5. Re-check log

* Pass 1 (≈ 20:30–21:00 UTC): V5 reports, PRED-MODELS protocol + holdout (`4975837`), PRED-ENERGY code and
  winners (`07e194e`), PRED-FIN report, conflict / health code; re-runs in §1.
* Pass 2 and pass 3: see the appended sections below (reports landing after pass 1).

## 6. Pass 2 (≈ 21:15 UTC) — reports landed: `v6_pred_models.md`, `v6_pred_energy.md` (filled), `v6_pred_fin_backtest.json`

**PRED-MODELS report.** Headline matches my re-run and is worded honestly ("No REVA model beats the WEO or the SPF
point forecast"; "what REVA can add is calibrated uncertainty around those strong points"). Remaining wording risk:
"CRPS −23.5 % vs a naive Gaussian around the WEO" is correct, but the benchmark's spread is the *persistence*
spread (X1) — the report should say so in the headline sentence, and any downstream text (UI, registry
leaderboard, roadmap) must not shorten it to "beats the IMF". The two WEO winners now issue sealed forecasts for
`weo.gdp_growth` h0 through supplementary batches: their point is the WEO projection (attributed) — the issued
record must carry the IMF attribution of the point, not present it as a REVA forecast. Verdicts in §3.C unchanged.

**PRED-ENERGY report.** Accurate and reproducible (my numbers in §3.D match its table: STEO vs RW MAE ratios,
Henry Hub −17 to −24 %, U.S. crude −23 to −43 %). Two items stay **overstated**: (1) "17 (series, horizon)
winners" uses one Holm family per series (60–72 tests); one Holm over the run leaves 10, and 4 without the
STEO+RW combination (X3); (2) the sealed batch `6d76937c…` issues 18 `combo_steo_rw` forecasts on cells where
EIA's STEO point alone has the lower MAE (e.g. U.S. crude h4: combo 0.445 vs STEO 0.337 vs RW 0.580) — a
"winner vs the random walk" that is dominated by the attributed reference. Fix: admit an EIA-derived combination only
where it also beats STEO's point on AE/SE; report the winners' 80 % coverage (0.61–0.71) next to them. New
statement verified: "for Brent and WTI, EIA's STEO point forecasts are not better than a random walk" — **credible**
(my re-run: MAE ratio 0.95–1.05, no significant DM test).

**PRED-FIN backtest JSON** committed; §5 table of the report matches my subset re-run. No change of verdict.
