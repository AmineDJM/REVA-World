# V6 — FIX7: fixes for QA-PRED's review of the forecasting claims

Agent FIX7, 2026-10-17. Input: `docs/dev/review_v6_predictions.md` (QA-PRED). Append-only throughout: no sealed
batch, manifest, holdout file or ledger entry was edited; the locked PRED-MODELS holdout was **not** re-opened or
re-scored (the fair-benchmark comparison runs on development origins only). Sealed batches are annotated by appended
artifacts. Status per QA-PRED finding: new column in `review_v6_predictions.md` §2 and its §9.

## Summary

| # | Item | Fix | Result |
|---|---|---|---|
| 1 (X1, H) | Strawman density benchmark | `src/reva/evaluation/pred_models/fair_benchmark.py`: `own` = N(official point, RMS of that series' own past official errors with `outcome_known_at <= origin`, ≥ 8); `pooled` = Gaussian twin of the conformal density. CLI `uv run python -m reva.cli.pred_models fair-dev` (development origins only) → `registry/forecasts/pred_models/fair_benchmark_development.json`. Module kept out of the protocol's `SOURCE_FILES` (protocol code digest unchanged) | **No registered density beats the fair benchmark on development**: WEO GDP h0 conformal ×0.985 (p 0.033, Holm ✗), bias-corrected ×0.992 (p 0.18); SPF current quarter ×1.018 (worse). Old strawman: ×0.66 / ×0.65 / ×0.73 |
| 1 | Re-labelling | `registered_models.yaml` (appended fields `label_en`, `claim`, `not_a_better_point_forecast`, `holdout_benchmark_was_weak`, `fair_benchmark`, `development_vs_fair_benchmark`, `issuance`), `methods.yaml` descriptions, `issue.py` descriptions (→ registry batches, leaderboard API `description`), UI labels (`PrevisionsPage.tsx`), v6_pred_models.md (notice + §12) | wording everywhere: "calibrated interval around the IMF WEO / SPF point — not a better point forecast"; holdout win recorded as "against a weak benchmark" |
| 1 | Future issuance | WEO densities on `probation`; the fair benchmark `forecast.weo_error_fan@1.0.0` (methods.yaml) issues in the same supplementary batches on the same questions; `issue.fair_gate` (≥ 30 resolved common questions, CRPS, one-sided country-clustered DM, p < 0.05) → `confirmed` / `suspended`, appended to `issuance_decisions.yaml`; `suspended` → `issuing_scopes` drops the model. SPF density: no family, must beat `benchmark.spf_errfan` prospectively first | see §DB |
| 2 (X3, §3.D) | Energy multiplicity | `energy_backtests._summaries`: ONE Holm family per run over every (series, issuable candidate, horizon) CRPS-vs-RW test; `energy_models.NOT_ISSUABLE = {combo_steo_rw}` (tested, reported, never a winner); `registry/energy.py` never issues it even if a winners file lists it; winners carry family size, 80 % coverage, AE difference vs STEO | re-run (digest `52fce398…`, 23,690 records): **17 → 4 winners** (family 260): `ar_diff` COPRPUS h2 (CRPS ×0.83, cov80 0.68), PAPR_WORLD h2/h3 (×0.91/×0.89, 0.65/0.61), `combo_models` PAPR_WORLD h1 (×0.92, 0.64); all four have a higher point error than STEO. `energy_winners.yaml` regenerated; v6_pred_energy.md corrected |
| 2 | Sealed batch `6d76937c…` | annotation appended (18 `combo_steo_rw` forecasts: EIA-derived, dominated by STEO, not evidence of skill, excluded from future issuance) | see §DB |
| 3 (X8) | SPF stale no-change | `vintage_backtests.spf_backtest(baseline_vintage="survey_quarter")` (default): the RTDSM vintage of the survey quarter = the panellists' information set; V5 rule kept as `"available_then"` | rel. MAE h2..h6 **0.509 / 0.647 / 0.703 / 0.675 / 0.642** (was 0.460 / 0.643 / 0.672 / 0.651 / 0.620; QA-PRED 0.509 / 0.648 / 0.705 / 0.680 / 0.644). Table corrected in v5_pred_vintage.md §6.C |
| 4 (X10, X11) | Registry prospectivity, voids | `questions.generate_questions(issued_on=)`: no question on a period that has ended at issue (unless family `nowcast: true`), `issue_horizon` and `timing` recorded; `service.resolve_batch`: `not_prospective` if the period had ended at issue or the outcome cell's provider publication date (`PUBLICATION_FLAGS`: release_date, publication_date, published_at, last_updated, data_update; ISO only) ≤ seal date; `void_questions`: after the period end only with `counterfactual=True`, counterfactual scores logged in the resolution and per model (`metrics.counterfactual_scores`); refused if no outcome yet; all checks before any write. `annotate_batch` (append-only annotations) | tests below |
| 5 (X13, X2, X9, §3.F) | Conflict | `conflict/stats.dm_test(d, clusters, time_clusters)`: two-way (country, year) clustered variance (CGM; fallback max one-way), `z_country_only` kept; backtest + monthly VIEWS (country, run) use it; VIEWS comparison on `abs_error`/`msle` only; `input_vintage_note` (V-Dem v16 / current WDI revised); new questions carry `timing` + `issue_horizon` | batch `ef57141a…`: 765 questions on 2026 = near-nowcasts (79 % of the year elapsed), annotated |
| 6 (§7, X9) | Health | `health_registry.service.admission_basis`: `candidate_admission` on family + question, `admission` on candidate records ("FINAL-VINTAGE RESULT …"), GHE `target_note` ("next WHO GHE release, a modelled estimate, not registered deaths") | batch `0ab8fcc2…` (612 blend + 504 log-drift forecasts) annotated |
| 7 (X5, X6, X7) | Minor | X5 documented (v6_pred_models §12) + `compare(purge=True)`; X6: `relative_median_ae` and `headline_metric` in `weo_backtest`, WEO CPI h0 median ratio **0.38** (mean ratio 10.0 never a headline); X7: `outcome_vintage_note` in the WEO backtest output and docs | — |

## Fair benchmark, development only (registered scopes)

| scope | candidate | old benchmark (persistence spread) | fair `own` | fair `pooled` | purged (X5): old / own / pooled |
|---|---|---|---|---|---|
| WEO GDP h0 | `weo_conformal` | ×0.663 p 2e-8 ✓ | ×0.985 p 0.033 ✗ | ×0.924 p 0.049 ✗ | ×0.678 / ×0.988 / ×0.934 |
| WEO GDP h0 | `weo_bias_corrected` | ×0.654 p 7e-11 ✓ | ×0.992 p 0.18 ✗ | ×0.912 p 0.002 ✓ | ×0.668 / ×0.996 / ×0.921 (✗) |
| US GDP current quarter | `spf_conformal` | ×0.726 p 2e-14 ✓ | ×1.018 p 0.70 ✗ | ×1.009 p 0.62 ✗ | ×0.720 / ×1.015 / ×1.007 |

✓/✗ = Holm-rejected within the benchmark's family (WEO m = 18, US m = 45). Development 80 % coverage, WEO GDP h0: old
0.98, own 0.87, pooled 0.92, conformal 0.82; US: old 1.00, own 0.79, conformal 0.74. The holdout gain was mostly the
strawman's over-wide spread; the registered densities are about as good as the official forecaster's own error fan.

## DB (dev DB `reva`, origin `live`, under `flock /tmp/reva-devdb.lock`)

DB_RESULTS_PLACEHOLDER

## Tests

| File | What |
|---|---|
| `tests/unit/test_fix7_predictions.py` (8) | error fan = N(point, RMS own errors); only errors published at the origin; dev comparison never scores holdout origins, purge drops overlapping cases; `fair_gate` pending / confirmed / suspended; labels + fair benchmark issues with the WEO densities; two-way DM wider than country-only under a common year shock; `issue_timing`, provider publication flags (labels never guessed); health final-vintage admission labels |
| `tests/unit/test_forecast_registry.py` (+3) | no question on an ended period, `issue_horizon`/`timing`; resolution not prospective on provider release date ≤ seal and on period ended at issue; void after period end refused without counterfactual, logged with it, refused when no outcome yet (nothing written on refusal) |
| `tests/unit/test_energy_forecasting.py` (+1), `tests/unit/test_registry_energy.py` (+1, 1 updated) | one Holm family across variables, `combo_steo_rw` never a winner, winner rows carry coverage / STEO comparison; EIA-derived combination never issued |
| `tests/integration/test_forecast_registry_db.py::test_05` (updated) | void after period end requires counterfactual scores. Note: test_01/02/04/05 of this file already fail on the pre-FIX7 code (same 4 failures at `dea38ee`: 31 models instead of 5 in test_01; resolution after FIX5's record-date rule in test_02, which test_04/05 depend on) — owner PRED-REGISTRY / FIX5 |

Re-run suites: `tests/unit/predict`, registry, vintages, energy, conflict, health, finance, model registry — 122 passed;
ruff, mypy clean on the changed modules.

## Not done / owner decisions

1. **Finance and health single Holm family in code** (X3) — documented only; health p-values survive a correction
   over 18 cells. **X12** (leaderboard on common sets) not in scope.
2. **X7 independent outturns** (national-accounts first releases for advanced economies) — needs OECD/Eurostat
   first-release vintages; owner decision.
3. **`realtime.us_cases` RTDSM vintage** (X8 for REVA's own US models): unchanged to keep the protocol code digest;
   use the survey-quarter vintage in the next protocol.
4. **Gate thresholds** (`GATE_MIN_QUESTIONS = 30`, α = 0.05, country clusters) are FIX7's choice; confirm before the
   first decision. The first `weo.gdp_growth` h0 outcomes arrive with the April 2027 WEO.
5. Conflict DM tables in v6_pred_conflict.md were computed with country clusters only; re-run `run_backtest` to
   refresh them (conclusion "0 winners" cannot get stronger).

## Commands

```bash
uv run python -m reva.cli.pred_models fair-dev                       # development only; writes fair_benchmark_development.json
uv run python -m reva.cli.energy_backtest --out /tmp/energy_bt.json --winners registry/forecasts/energy_winners.yaml
flock /tmp/reva-devdb.lock uv run reva seed                          # forecast.weo_error_fan@1.0.0
flock /tmp/reva-devdb.lock uv run python scripts/fix7/annotate_sealed_batches.py --write
flock /tmp/reva-devdb.lock uv run python -c "from reva.adapters.objects.factory import get_store; from reva.adapters.postgres.engine import make_engine; from reva.settings import get_settings; from reva.evaluation.pred_models.issue import issue_supplements; print(issue_supplements(make_engine('worker', get_settings()), get_store()))"   # fair-benchmark supplements (also run by the weekly issue job)
uv run pytest -q tests/unit/test_fix7_predictions.py tests/unit/test_forecast_registry.py \
  tests/unit/test_energy_forecasting.py tests/unit/test_registry_energy.py tests/unit/test_conflict_forecasting.py
```
