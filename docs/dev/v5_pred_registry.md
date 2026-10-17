# V5 PRED-REGISTRY — prospective forecast registry in production use

Agent PRED-REGISTRY, 2026-10-17. Roadmap: `docs/roadmap/v5_prediction_engine.md` (decisions 2 and 4). Baseline
before this work: `docs/strategy/world_map.md` §1 / §5.3 — "**0** prévision émise", `model.evaluation` empty.

## 1. Result in one paragraph

The registry now runs for real in the dev DB. **586 questions** (434 + 141 + 11, see §5) were generated from atlas
series and **2,287 forecasts** (2,176 baseline + 111 attributed IMF WEO projections) were frozen and **sealed** on 2026-10-17 (cycle `2026-Q4`, origin `live`):
inflation (WEO annual, FAOSTAT monthly CPI and food inflation, Eurostat HICP), unemployment (WEO, Eurostat monthly),
GDP growth (WEO annual, Eurostat quarterly), residential property prices (BIS quarterly), FX (IMF annual average),
policy rates (IMF annual), the FAO Food Price Index and Brent (EIA STEO), for 21 G20+ economies, 10 EU countries and the
world benchmarks, 1–12 months / 1–8 quarters / current and next year ahead. Only **baselines** forecast (persistence,
seasonal naive, AR(p) fitted as of issue, historical quantiles) plus the IMF WEO projection **attributed to the IMF**
(point only). Nothing is resolved yet (earliest expected resolutions: early December 2026 for the FFPI, mid December for HICP). A daily resolution job and a weekly issue job are scheduled; a **final-vintage pseudo-real-time**
back-test (28,965 scored replays, labelled as such) is stored. API `/v1/forecasts/*`, UI screen **Prévisions**
(`/v3/previsions`), CLI `reva forecasts …`. No migration.

## 2. What was built

| Part | Files |
|---|---|
| Question families (variables, sources, rules) | `registry/forecasts/question_specs.yaml` (23 families) |
| Baselines (pure numerics) | `src/reva/models/forecasting/registry_baselines.py` |
| Periods, specs, questions, forecasts, scores (pure) | `src/reva/evaluation/registry/{periods,specs,questions,forecasts,scores}.py` |
| Atlas reads (table versions, verified frames) | `src/reva/evaluation/registry/data.py` |
| Issue / seal, resolve / score, back-test, job handler | `src/reva/evaluation/registry/service.py` |
| API reads (seal re-verified on every read) | `src/reva/evaluation/registry/read.py`, `src/reva/api/routes/forecasts.py` |
| Models (candidates) | `registry/models/methods.yaml` (5 entries) — seeded in the dev DB |
| Job kind + schedules | `forecast_registry` in `src/reva/operations/jobs.py`, `schedules.py`; `forecast_registry:issue:live` (7 d), `forecast_registry:resolve:live` (1 d) in `src/reva/cli/seed.py` — seeded |
| CLI | `reva forecasts issue|resolve|backtest|status` (`src/reva/cli/main.py`) |
| UI | `ui/src/v3/pages/PrevisionsPage.tsx`, `ui/src/v3/api/forecasts.ts`, route + screen entry |
| Tests | `tests/unit/forecasting/test_registry_baselines.py` (7), `tests/unit/test_forecast_registry.py` (10), `tests/integration/test_forecast_registry_db.py` (4, throwaway DB + API), `ui/src/v3/pages/previsions.test.tsx` (2) |
| Contracts | `contracts/openapi.json` (7 `/v1/forecasts` paths), `ui/src/api/openapi.gen.ts` regenerated |

## 3. Question schema (`reva-forecast-question/1`)

Every question is fully defined at issue time and hashed (`question_id = "fq_" + logical_hash[:24]`):

* **variable**: `family`, `concept_id` (atlas catalog id when one exists), `label_fr/en`, `definition` quoted verbatim
  from the catalog or the provider file (FAOSTAT item labels were checked in the archived raw CSV), or REVA's
  derivation rule when the value is derived (`yoy_pct_from_index`, `monthly_mean_of_daily`), `unit`;
* **geography**: `geo` (ISO3 or a declared non-country key such as `WORLD`, `BRENT-STEO`, `EUR-USD`), labels;
* **reference period**: `M` `2027-01` / `Q` `2027-Q1` / `A` `2027`, half-open `[period_start, period_end)`;
  `cycle_horizon` = periods after the quarterly cycle anchor (horizons: M 1, 3, 6, 12; Q 1, 2, 4, 8; A 0, 1);
* **type**: `quantity` (predictive distribution → CRPS, absolute error, PIT), `interval` (central 80 % interval →
  coverage, interval score), `binary` ("strictly above `threshold`"; threshold = latest value published at issue,
  with its period in `threshold_basis`) → Brier and log score;
* **resolution**: declared source (atlas `dataset_key` + indicator), `value_rule`, rule
  `first_observed_release_after_seal`, `expected_date` (period end + publication lag), `void_after`, the pinned
  issue table version, and a free-text note.

**Resolution rule.** The value of the declared cell in the **first version of the declared atlas table registered
after the seal** that publishes it as an observation (projection cells — WEO beyond `LATEST_ACTUAL_ANNUAL_DATA`,
STEO forecasts — are never outcomes); checked from the end of the reference period. No value by `void_after` →
`void` (typed, never scored). A value already observed in the pinned issue version → `not_prospective` (never
scored). The outcome keeps its provenance (table version, artifact sha256, raw sha256 + selector + flags of every
source cell).

**Prospectivity, FIX7 (QA-PRED X10/X11, 2026-10-17).** Checked against the **issue date and the provider's
publication date**, not only REVA's pinned table: (1) at issue, a target period that has **ended** at `issued_at` is
refused (skipped with a reason) unless the family declares `nowcast: true`; every new question records
`issue_horizon` (periods from the issue period; `cycle_horizon` stays the cycle-anchored label) and `timing`
(`forecast` / `nowcast` / `near_nowcast` / `ended`, elapsed fraction of the target period at issue); (2) at
resolution, `not_prospective` also when the period had ended at issue (older batches), or when the outcome cell's
provider publication date (flags `release_date`, `publication_date`, `published_at`, `last_updated`, `data_update`;
ISO dates only, labels never guessed) is on or before the seal date. Where no cell declares such a date only the
table-relative check applies (stated). **Voids:** `void_questions` closes a question by rule only **before its
period ends**; after that, `counterfactual=True` is required and the counterfactual scores (against the first value
published after the seal) are logged in the void resolution and in each model's `registry_score` row
(`metrics.counterfactual_scores` / `counterfactual_summary`) for the leaderboard audit; with no outcome published
yet the void is refused. **Annotations:** `annotate_batch` appends a content-addressed note to a sealed batch
(provenance input = the batch) without touching it.

## 4. Sealing (freeze before resolution)

`issue_cycle` (worker credentials, advisory lock): DB clock `issued_at` → cycle; pins, per family, the latest
version of its table at `issued_at` (same rule as an atlas snapshot; no snapshot is taken); generates the questions
of the cycle not issued yet (de-duplicated on `question_key` = cycle|family|geo|period|type); forecasts every
question with every model; drops a question only if *every* baseline abstains (listed with reasons). The batch
manifest (`reva-forecast-registry-batch/1`: questions, frozen forecasts and abstentions with reasons, table pins,
per-question training digest, specs sha256, code digest, label) is stored content-addressed (`evaluation`
namespace) and its **sha256 is the seal**; one immutable `model.evaluation` row per model (`metrics.kind =
'registry_issue'`, replay mode `operational_as_known`) in a new `evaluation` context with `knowledge_cutoff =
issued_at`; provenance activity with the pinned table artifacts as inputs; outbox event. Every API/resolution read
re-verifies the manifest bytes against the sha256 (`ledger.load_frozen_manifest`). Forecast numbers are stored as
10-significant-digit decimal strings: that string *is* the forecast.

Storage needed no migration: the existing append-only structures suffice (`meta.artifact` + object store for the
manifests, `model.evaluation` rows distinguished by `metrics.kind` = `registry_issue` / `registry_score` /
`registry_backtest`, `meta.context`, `meta.activity`). A dedicated indexed table would only make question-level SQL
queries cheaper (today the API reads the cached, verified manifests) — not needed at this volume.

## 5. First real batches (dev DB, live origin, cycle 2026-Q4)

| Batch (seal sha256) | issued_at (UTC) | Questions | Families (questions) |
|---|---|---|---|
| `96c6ad48167100d5f5ee8d8195cb1a785d206f493cd9fa12f1937c54a0f001c5` | 2026-10-17 14:02:32 | 434 (211 quantity, 169 interval, 54 binary) | weo.cpi_inflation 40, weo.gdp_growth 38, weo.unemployment 37, fao.cpi_inflation_yoy 84, fao.food_inflation 126, bis.property_prices_real_yoy 68, imf.fx_usd_annual_avg 17, imf.policy_rate_annual 24 |
| `da05d33693745d42cff4635ade809912812480f7a6a59046c46127cdc67e7b35` | 2026-10-17 14:21:07 | 141 (84 quantity, 44 interval, 13 binary) | eurostat.hicp_inflation 50, eurostat.unemployment 40, eurostat.gdp_growth_yoy 40, fao.food_price_index 11 — tables V4ECON registered at 14:05–14:11, picked up by the second run (what the weekly schedule does) |
| `179421efa0abf1ea133ed2320ac5770f1f0e503abd3fa4397736a78f4a155938` | 2026-10-17 15:32:03 | 11 (4 quantity, 4 interval, 3 binary; 44 forecasts) | eia.brent_monthly (STEO `BREPUUS`, monthly; the family's geography was corrected to EIA's verbatim `USA` after batch 2) |

Forecasts batch 1: persistence 434, hist_quantile 432 (2 abstained: < 10 changes), AR 424 (8 non-stationary fits,
2 too short), seasonal naive 278 (156 abstentions: annual series), IMF WEO reference 111 (4: no projection cell).
Batch 2: 141 × 4 baselines, no abstention. Pinned tables (batch 1): `atlas.imf.weo` (WEO 2026-10 vintage,
`708ea329…`), `atlas.fao.cp` (`8dcf69e1…`), `atlas.bis.spp` (`9291d6d5…`), `atlas.imf.er` (`cc7a9fc4…`),
`atlas.imf.mfs_ir` (`e1661d1b…`).

Example (batch 1, `fq_71bcaa3af23740b1f21047bd`): « Inflation des prix alimentaires (glissement annuel) » pour
Argentina (ARG), période 2027-01, sera-t-elle strictement supérieure à 32.740047 (dernière valeur publiée, 2026-03) ?
P(oui): persistence 0.5, seasonal naive 0.520, AR 0.677, historical quantiles 0.573 — resolved by the first FAOSTAT CP
release registered after the seal that publishes ARG 2027-01 (expected ≈ 2027-08-20, void after 2028-07-24).

Generation report (listed in the manifest, never filled in): 7 policy-rate and 4 property-price panel countries
have no series; 3 euro-area members have no national FX after 1998 (stale); 104 WEO GDP cells are fiscal-year
windows (e.g. IND) and are not calendar years, so those economies get no WEO GDP question.

## 6. Baselines and the reference forecaster

| model_key (all `1.0.0`, `candidate`) | Definition |
|---|---|
| `forecast.registry_persistence` | random walk: point = last value; N(last, σ²), σ = RMS of historical h-step changes of the final consecutive run (≥ 8) |
| `forecast.registry_seasonal_naive` | same season in the most recent observed year; σ = RMS of seasonal-naive errors at that lag; M/Q only |
| `forecast.registry_ar` | AR(p) with intercept on levels, OLS on the last 120 months / 80 quarters / 40 years as of issue, p ≤ 6/4/2 by AIC; psi-weight h-step variance; non-stationary fit → abstain |
| `forecast.registry_hist_quantile` | last value + empirical h-step changes (≥ 10); point = median; binary by rule of succession (k+1)/(n+2) |
| `forecast.reference_imf_weo` | the WEO projection cell of the pinned vintage, attributed to the IMF; point only (interval/binary → abstain) |

Decision 2 of the roadmap is respected: these are not judged models; their probabilities exist to be scored. Every
API response carries `baseline_forecasts_only` and `not_advice` warnings.

## 7. Scores, leaderboard, calibration

Per frozen forecast: absolute error; CRPS (closed form Gaussian; `E|X−y| − ½E|X−X'|` for the empirical sample);
log predictive density (Gaussian); PIT; interval coverage, width and interval score (G&R 2007); Brier and binary log
score. Not-applicable metrics are `null` ("n. a." in the UI), never 0. Leaderboard per model and per type, plus
**MAE / CRPS / Brier relative to persistence on the questions both scored** (ratio and skill = 1 − ratio).
Calibration: PIT histogram (10 bins), reliability table (binary), empirical vs nominal 80 % coverage.

## 8. Pseudo-real-time back-test — final vintage (labelled; never real-time skill)

`backtest sha256 2b149cd396d5b52292d56a696ab3aa58d47fb8a3b59b400f472a517aa8eab2e9` (replay mode
`revised_data_retrospective`, label "final-vintage pseudo-real-time"): rolling origins over the last 8 years (monthly
every 3 months), each fit only on periods ≤ origin, horizons = publication lag + question horizons, 8 families,
28,965 scored replays. Ratios are on common cases (< 1 = better than persistence):

| Family (horizons, periods) | AR MAE/persistence | AR CRPS/persistence | hist-quantile CRPS/pers. | 80 % coverage (persistence) |
|---|---|---|---|---|
| WEO GDP growth (1, 2 y) | 0.82, 0.67 | 0.80, 0.69 | 1.00, 1.02 | 76 %, 66 % |
| WEO unemployment (1, 2 y) | 1.06, 1.04 | 1.05, 1.06 | 1.02, 1.06 | 82 %, 83 % |
| WEO CPI inflation (1, 2 y) | 3.43, 3.51 (hyperinflation cases) | 0.75, 0.77 | 0.27, 0.35 | 79 %, 76 % |
| FAO CPI y/y (8–19 m) | 1.07 → 0.97 | 1.09 → 0.99 | ≈ 1.05 | 71 % → 62 % |
| FAO food inflation (8–19 m) | 0.99 → 0.96 | 1.01 → 0.98 | ≈ 1.05 | 74 % → 67 % |
| BIS real property prices y/y (4–11 q) | 0.93 → 0.81 | 0.89 → 0.83 | ≈ 1.03 | 80 % → 71 % |
| IMF FX annual average (2 y) | 0.86 | 1.06 | 0.86 | 72 % |
| IMF policy rate annual (2 y) | 1.04 | 1.04 | 1.02 | 68 % |

Reading: no baseline is reliably better than persistence; AR helps for GDP growth and property prices; all
intervals under-cover at longer horizons (the "Gaussian h-step RMS" spread is too narrow). These are final-vintage
numbers; WEO real-time results belong to PRED-VINTAGE (`src/reva/evaluation/vintage_backtests.py`).

## 9. API, UI, CLI, schedules

* `GET /v1/forecasts/families` (status available / awaiting_table / awaiting_source), `/batches`, `/questions`
  (filters family, geo, type, status, cycle; paging), `/questions/{fq_…}` (sealed question, frozen forecasts with
  scores, outcome with provenance, seal verified), `/leaderboard`, `/calibration?model=`, `/backtests` (warning
  `final_vintage_pseudo_real_time`). `read` scope; envelope `context_mode = evaluation`.
* UI **Prévisions** (`/v3/previsions`, French): honesty banner, sealed batches, leaderboard (n. a. never 0),
  calibration, question list with filters and a detail panel (definition quoted, seal, each forecaster's point /
  interval / probability or its abstention reason, score), question families with status, back-test table labelled
  "millésime final, pas une compétence en temps réel". Static bundle not rebuilt here (`cd ui && npm run build`).
* CLI: `reva forecasts issue [--family k]`, `reva forecasts resolve`, `reva forecasts backtest [--years 8]`,
  `reva forecasts status`.
* Schedules (seeded in the dev DB): `forecast_registry:issue:live` every 7 days (a no-op inside a cycle unless a new
  family's table landed; a new quarter issues a full new cycle, ≥ 100 questions), `forecast_registry:resolve:live`
  daily.

## 10. Families waiting for tables (picked up automatically by the weekly issue)

| Family | Table | Owner |
|---|---|---|
| imf.cpi_inflation_monthly / unemployment_monthly / gdp_growth_quarterly / policy_rate_monthly / fx_usd_monthly_avg | `atlas.imf.{cpi_m, ls_mq, qnea_q, mfs_ir_m, er_m}` (`atlas.imf_hf`; ingest queued on the lock at writing time) | V4ECON |
| eia.brent_spot_monthly_mean | `atlas.eia.spot_d` (`EIA_SPOT|series=RBRTE`, monthly mean of daily) | PRED-HF |
| ecb.eurusd / eurcny / eurjpy_monthly_mean | `atlas.ecb.fx_ref` (monthly mean of daily) | PRED-HF |
| bis.policy_rate_daily | `atlas.bis.cbpol_d` (monthly mean of daily levels) | PRED-HF |

## 11. Honesty notes and limits

* FAOSTAT CP values include FAO-estimated/imputed cells (flags `E`, `I`): kept with their flags as training data and
  as outcomes; a question resolved by an `E` cell says so in its outcome provenance.
* First-release resolution: later revisions do not change a score (by design; the revised values are visible in
  later table versions).
* The STEO Brent series is declared by EIA with geography `USA` (verbatim); the question key is `BRENT-STEO`.
* FAO FFPI cells are flagged `non_commercial` (FAO website terms); EIA spot prices carry `licence_review`
  (Refinitiv). The registry cites the tables; it does not relicense values.
* Persistence's binary probability against "above the last value" is 0.5 by construction (reference).
* The WEO reference forecaster uses the WEO vintage pinned at issue (2026-10); vintage-correct WEO *back-tests* are
  PRED-VINTAGE's (`vintage_backtests.py`).
* Pre-existing failures seen while testing, not from this agent: invariant 11 (`reva.evaluation.vintages` →
  `reva.atlas.snapshot` → … → connectors) and invariant 30 (PRED-HF budget keys `bis.bulk`, `ecb.stats`,
  `nyfed.markets`, `ustreasury.rates`, `boe.iadb`). The registry itself passes invariant 11 (its only connector import
  was replaced by a direct read of `registry/entities/country_crosswalk.json`).

## 12. Owner decisions

1. Publish the registry (public pre-registration = credibility) or keep it private (world_map §7 Q18)?
2. Accept FAOSTAT estimated (`E`) cells as outcomes, or void such questions?
3. FFPI non-commercial terms: keep FFPI questions only for private evaluation?
4. Success criterion at 6 months (world_map Q20, e.g. 300 pre-registered / 100 resolved): today 586 / 0.

## 13. Commands

```bash
flock /tmp/reva-devdb.lock uv run reva seed                       # models + schedules (done)
flock /tmp/reva-devdb.lock uv run reva forecasts issue            # current cycle, new families only
flock /tmp/reva-devdb.lock uv run reva forecasts resolve          # due questions -> scores
flock /tmp/reva-devdb.lock uv run reva forecasts backtest --years 8
uv run reva forecasts status --origin live
uv run pytest tests/unit/test_forecast_registry.py tests/unit/forecasting/test_registry_baselines.py \
  tests/integration/test_forecast_registry_db.py
cd ui && npx vitest run src/v3/pages/previsions.test.tsx && npm run build
```
