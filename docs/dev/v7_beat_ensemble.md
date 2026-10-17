# V7 BEAT-ENSEMBLE — online combination of every forecaster in the prospective registry

Agent BEAT-ENSEMBLE, 2026-10-17. Protocol: `docs/roadmap/v7_beat_official.md` (track 1, "combination with the
official forecast as an input"). Standard: `docs/dev/review_v6_predictions.md` (QA-PRED). Registry:
`docs/dev/v5_pred_registry.md`.

## 1. Result in one paragraph

The registry now seals a **`reva.ensemble`** forecast on every open question, next to (never instead of) the
forecasters it combines: REVA baselines and models **and the attributed official forecasts** (IMF WEO, EIA STEO,
VIEWS, ...). Weights are learnt per registry family × question type × role **only from resolved prospective
registry scores** known at the ensemble's seal; back-tests never set them; until 20 questions of a task have
resolved, the weights are **equal and labelled `equal_weights_insufficient_history`**. Each ensemble batch records
the rule, the exact score rows behind every weight vector (question, model, loss + digest + resolution manifests)
and, per forecast, its components (model + sealed batch) and weights; a pure re-derivation reproduces the batch bit
for bit. A scoreboard **"REVA ensemble vs best single forecaster"** (API `GET /v1/forecasts/ensemble`, UI section in
*Prévisions*) fills in only as questions resolve: DM test (two-way clustered) per family × type × metric, Holm
across all tested cells. **Live dev DB (origin `live`), 2026-10-17 22:06 UTC: 6 ensemble batches, 2,393 ensemble
forecasts sealed over 42 families, all with equal weights (0 resolved questions exist), 0 scored, 127 scoreboard
cells all `awaiting_resolutions`.** No claim of any kind exists yet, and none can be backfilled.

## 2. What was built

| Part | File |
|---|---|
| Weights and combination rules (pure, domain-agnostic) | `src/reva/models/forecasting/online_combination.py` |
| Registry ensemble: tasks, sealed weight records, forecasts, supplement batch, issue service, re-derivation, scoreboard | `src/reva/evaluation/registry/ensemble.py` |
| `reva.ensemble` in `all_models()` (resolution scores it like any forecaster) | `src/reva/evaluation/registry/forecasts.py` |
| Job: `forecast_registry` `issue` now also seals the ensemble (after the baselines and the PRED-MODELS supplements); new action `ensemble` | `src/reva/evaluation/registry/service.py` |
| CLI `reva forecasts ensemble` (and `reva forecasts issue` runs it) | `src/reva/cli/main.py` |
| API `GET /v1/forecasts/ensemble`; batches carry `supplement_of`; leaderboard `kind = reva_ensemble`; pending count no longer double-counts supplementary batches | `src/reva/api/routes/forecasts.py`, `src/reva/evaluation/registry/read.py`, `contracts/openapi.json`, `ui/src/api/openapi.gen.ts` |
| UI section "Ensemble REVA vs meilleur prévisionniste seul" | `ui/src/v3/pages/PrevisionsPage.tsx`, `ui/src/v3/api/forecasts.ts` |
| Model entry (seeded in the dev DB) | `registry/models/methods.yaml` (`reva.ensemble` 1.0.0, candidate) |
| Tests | `tests/unit/forecasting/test_online_combination.py` (4), `tests/unit/test_registry_ensemble.py` (3), `tests/integration/test_registry_ensemble_db.py` (3, throwaway DB + API), `ui/src/v3/pages/previsions.test.tsx` (extended) |

No migration: ensemble batches are ordinary supplementary sealed batches (`supplement_of` = the original batch,
`task_scope.ensemble = true`), same storage as PRED-MODELS supplements.

## 3. The rule (pre-declared, recorded in every sealed manifest as `ensemble.rule`)

**Tasks.** One weight vector per `family|type|role`:

| Question type | Role | Loss that sets the weights | Components |
|---|---|---|---|
| quantity | location | absolute error of the point | every forecaster with a point (official point-only forecasts included) |
| quantity | shape | CRPS | distributional forecasters only |
| interval | location / shape | absolute error / interval score | as above |
| binary | probability | Brier score | forecasters with a probability |

**Weights** (`hedge_relative_loss_shrunk/1`). Score rows = `registry_score` rows of the same origin recorded at or
before the ensemble's `issued_at` (database clock); excluded: `registry_backtest` (final-vintage pseudo-real-time),
`counterfactual_scores` of voided questions, the ensemble's own scores. Per resolved question, each component's loss
is divided by the median loss of the components scored on that question (scale-free: countries, units and
hyperinflation do not dominate; QA-PRED X6), clipped at 5, rescaled to [0, 1]. Each component's mean relative loss is
shrunk towards 1 with prior weight 10 (components abstain on different questions — X12 — and a new component starts
as the median performer). Weights = exponentially weighted average forecaster (Hedge, η = √(8 ln K / n), n =
informative resolved questions of the task). **n < 20 → equal weights, status `equal_weights_insufficient_history`**;
one component → `single_component`. Per question the task weights are restricted to the components present and
renormalised.

**Combination.** Point = weighted mean of the points. Distribution = quantile averaging of the components'
deviations from their own median, re-centred on the combined point (`q(τ) = loc + Σ v_k (Q_k(τ) − Q_k(0.5))`; the
combined median is the combined point); sealed as an empirical predictive of 50 mid-point quantiles, plus the 7
registry quantiles and, for interval questions, the central interval computed directly. Binary = linear pool.
No distributional component → point-only ensemble (scored on absolute error only).

**Same-date rule.** An ensemble is issued only for questions whose original batch was sealed at most 8 days earlier
(the weekly cadence; normally the same run), whose reference period has not ended (unless the family is a declared
nowcast) and that are not already handled; it combines only forecasts sealed before it. Its information set is
therefore the union of its components' plus resolutions known at its seal; the resolution path applies the usual
prospectivity checks against the *ensemble's* `issued_at`.

**Reproducibility.** `ensemble.tasks[*]` holds `history` (question, model, loss as sealed decimal strings),
`history_digest`, `resolution_sha256s`, `per_component` (n, mean and shrunk relative loss), `eta`, `weights`;
each forecast holds `ensemble.components` (model + batch sha256), `weight_status` and the per-question weights.
`ensemble.recheck_weights(manifest)` recomputes the weights from the recorded rows;
`ensemble.rebuild_forecasts(manifest, components)` recomputes every forecast from the component batches — both
are exact (tested on the throwaway DB).

## 4. Scoreboard "REVA ensemble vs best single forecaster" (`GET /v1/forecasts/ensemble`)

Per family × question type × metric (quantity: CRPS and absolute error; interval: interval score and absolute error;
binary: Brier): the ensemble vs each forecaster it combined, on the questions both scored. **CRPS is compared only
with distributional forecasters** (QA-PRED X2: a point forecast's CRPS is its absolute error); official point
forecasts are compared on absolute error. The "best single" is the eligible competitor (scored on ≥ max(20, 50 %)
of the ensemble's scored questions) with the highest mean-loss ratio ensemble/competitor — chosen ex post, which is
conservative for the ensemble. Test: DM on the per-question differential scaled by the question's median component
loss, two-way clustered by geography and reference period (X13); **one Holm over every tested cell** (two-sided,
family size printed; X3). Verdicts: `awaiting_resolutions`, `no_comparable_competitor`, `not_testable`,
`no_significant_difference`, `ensemble_better`, `best_single_better` — wins and losses both listed. A win reads
"REVA's combination beats <forecaster> alone", never "REVA beats the IMF".

## 5. Live run (dev DB, origin `live`)

`reva seed` (model `reva.ensemble@1.0.0` registered 21:58 UTC), then `reva forecasts ensemble` at
2026-10-17 22:06:37 UTC:

| Ensemble batch (seal) | Supplement of | Questions | Weight tasks |
|---|---|---|---|
| `e3b567f6fa58f2bb…` | `96c6ad48…` (WEO, FAO, BIS, IMF) | 429 (5 voided WEO fiscal-year questions skipped as handled) | 18 equal |
| `6693a526ba2c029d…` | `da05d336…` (Eurostat, FFPI) | 141 | 12 equal |
| `0e0eaa700a4125f3…` | `179421ef…` (EIA Brent) | 11 | 5 equal |
| `52264ec05d590f04…` | `6d76937c…` (PRED-ENERGY) | 122 | 32 equal |
| `1dca4d3232536ecf…` | `6f19e671…` (finance) | 160 | 56 equal |
| `7797793dda7b4cf6…` | `ef57141a…` (PRED-CONFLICT, VIEWS attributed) | 1,530 | 6 equal |

Total 2,393 ensemble forecasts, 0 abstentions, 42 families, 129 weight tasks, all `equal_weights_insufficient_history`
(the live registry has 0 resolved questions). Example — `fq_0b7a9aed0be92999ea2a50eb`, real GDP growth, Argentina,
2026: 7 components (IMF WEO projection attributed, AR, historical quantiles, persistence, `weo_conformal`,
`weo_bias_corrected`, `weo_error_fan`), location weights 1/7 each, shape weights 1/6 (WEO point-only excluded);
sealed point 2.73, 5–95 % quantiles −4.82 … 11.80. Scoreboard: 127 cells, all `awaiting_resolutions`, Holm family 0.

## 6. Honesty notes and limits

* **No evidence yet.** The ensemble has 0 scored questions; the first resolutions (NHSN, FFPI, HICP, Brent) are
  expected from late October / mid-December 2026. Until each task has 20 resolved questions its weights stay equal.
* **Equal weights are per forecaster, not per information source.** Three WEO-wrapped densities plus the WEO point
  give the IMF's number ~4/7 of the location weight on WEO questions at equal weights; the online weights will
  correct this only as far as resolved scores show it. (Owner decision 2.)
* **Interval-question location uses absolute error** of the point, the shape uses the interval score; binary
  questions get a probability only (no point), like the PRED-FIN probability-only forecasters.
* **Selection ex post.** "Best single" is chosen after the outcomes (conservative); per-family claims need the Holm
  correction across the whole scoreboard, which shrinks power as families multiply.
* **Families × roles are the weighting unit**, not horizons: a family with very different horizons shares weights.
  Horizon-specific tasks would need ~4× more resolutions.
* BOA (Bernstein online aggregation) was not implemented: Hedge with shrinkage is enough while n is tiny; BOA can be
  added as a second pre-declared method (new model version) once tasks have ≳ 100 resolutions.
* Pre-existing failures seen while testing, not from this agent: `tests/integration/test_forecast_registry_db.py`
  `test_05` (FIX7 void test expects a period that has not ended to be ended), `tests/unit/forecasting/
  test_pred_models.py::test_only_holdout_confirmed_scopes_with_a_family_issue` (FIX7 fair benchmark not in
  `ISSUERS`). I fixed two stale assertions in `test_forecast_registry_db.py` (model-row count = `len(all_models())`;
  later table versions read without the FIX5 future-date cut, which the test's simulated passage of time requires).

## 7. Owner decisions

1. Adopt `reva.ensemble` as REVA's headline forecast where it has evidence (after `ensemble_better` verdicts), and
   word wins as "REVA's combination beats <forecaster> alone".
2. Weight per information source (group WEO-wrapped densities with the WEO point) instead of per forecaster?
3. Minimum resolutions before online weights (20) and the scoreboard minimum (20 pairs, 50 % coverage): keep?

## 8. Commands

```bash
flock /tmp/reva-devdb.lock uv run reva seed                        # registers reva.ensemble (done)
flock /tmp/reva-devdb.lock uv run reva forecasts issue             # baselines + PRED-MODELS supplements + ensemble
flock /tmp/reva-devdb.lock uv run reva forecasts ensemble          # ensemble only (idempotent; done 22:06 UTC)
flock /tmp/reva-devdb.lock uv run reva forecasts resolve           # scores the ensemble with every other forecaster
curl -H "Authorization: Bearer $TOKEN" localhost:8000/v1/forecasts/ensemble?origin=live
uv run pytest tests/unit/forecasting/test_online_combination.py tests/unit/test_registry_ensemble.py \
  tests/integration/test_registry_ensemble_db.py
cd ui && npx vitest run src/v3/pages/previsions.test.tsx
```

The weekly `forecast_registry:issue:live` schedule runs the ensemble after every issue; the daily resolve job scores
it. Nothing else is needed for the scoreboard to fill in.
