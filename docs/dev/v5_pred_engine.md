# V5 — PRED-ENGINE: decision → distributions (decision engine)

Agent PRED-ENGINE, 2026-10-17. Spec: `docs/roadmap/v5_prediction_engine.md` (row PRED-ENGINE, decisions 2–5),
`docs/strategy/world_map.md` §5. Reads PRED-EFFECT's layer through `docs/dev/v5_effect_interface.md` and
PRED-REGISTRY's sealed baseline forecasts. No migration, no source, no network fetch, no write to the dev DB.

**What a result is:** a *scenario conditional on the decision and on stated assumptions*, labelled
**« scénario non validé — pas une probabilité »** until its exact model chain has passed the judge (back-test on
vintage data + calibration record). Every number comes from an attributed effect estimate (publication, REVA run
or identity) or from a REVA simulator under its stated assumptions; a causal link without a quantified estimate
stays a **lien non chiffré** and never gets a number. The fan charts show only the uncertainty the estimates
publish (not model, transport or chain uncertainty). Research, not advice.

## 1. What was built

| Piece | Path |
|---|---|
| Decision schema (actor, instrument, target, magnitude, start quarter, duration, horizon, countries, analogue anchors), instruments → *direct changes* with exact unit factors only, the 5 reference decisions | `src/reva/predict/decision.py`, `registry/predict/instruments.yaml`, `registry/predict/reference_decisions.yaml` |
| Effect estimate (PRED-EFFECT JSON) → propagation hop: change units in/out, β (point or point / shock size), uncertainty from the estimate only (`normal_approx`), lag profile on a quarterly grid or undated; applicability (scope × geography, estimation period × start year); deterministic choice among estimates of one edge | `src/reva/predict/estimates.py` |
| Engine: breadth-first propagation, Monte Carlo (named Philox streams of `reva.simulation.rng`), step-response superposition, plugins, honesty gates, validation status, unquantified links, JSON result | `src/reva/predict/engine.py` |
| Simulators as plugins: `economic.io_price@1.0.0` (FIGARO cost push), `economic.crude_exposure@1.0.0` (border-value accounting), `social.retirement_age` (registered plugin, run on the pinned release) | `src/reva/predict/propagators.py`, wiring in `registry/predict/variables.yaml` |
| Ports (effects, claims, baseline, plugin data) and static implementations for tests | `src/reva/predict/context.py` |
| Read-only DB adapters: `reva.effects` readers, causal claims of anchors (`reva.causal.graph`), atlas baseline (latest actual cell, projections excluded, query file never built), PRED-REGISTRY baseline forecasts (`reva.evaluation.registry.read`), crude inputs from the latest live release, FIGARO table and retirement datasets from the release (savepoints: a refused read never aborts the request) | `src/reva/predict/db.py` |
| CLI (read-only transaction, `reva_sim` role) | `uv run python -m reva.predict.run --reference all --out <file>` (`src/reva/predict/run.py`) |
| Judge registry (empty) | `registry/predict/validated_chains.yaml` |
| API `GET /v1/predict/decisions`, `POST /v1/predict/decision` (read scope; contracts regenerated: `contracts/openapi.json`, `ui/src/api/openapi.gen.ts`) | `src/reva/api/routes/predict.py`, 1 line in `src/reva/api/app.py` |
| UI « Décision » (French): composer (reference or composed decision), fan charts / interval bars per variable, causal path with sources, liens non chiffrés, excluded estimates and alternative paths, gaps, validation status | `ui/src/v5/decision/**`, route `/v3/decision` (`ui/src/v3/V3App.tsx`, `ui/src/v3/screens.ts`) |
| Tests: 23 unit (synthetic estimates with analytic answers), 4 integration (throwaway DB, `kg.effect_estimate` row written through `reva.effects`), 6 vitest | `tests/unit/predict/`, `tests/integration/test_predict_api.py`, `ui/src/v5/decision/decision.test.tsx` |

## 2. Decisions and instruments

A decision: `id`, `label_fr`, `actor {label_fr, kg_id?, geo?}`, `instrument`, `target {label_fr, kg_id?, geo?}`,
`magnitude {value (decimal string), unit}`, `start` (`2027Q1`), `duration_quarters` (null = held over the horizon),
`horizon_quarters` (≤ 12), `countries` (ISO3, ≤ 12), `anchors` (KG ids of analogue episodes), `plugin_options`.

| Instrument | Direct change (variable @ geo, transform, unit) | Magnitude units (exact factor) |
|---|---|---|
| `oil_production_quota_change` | atlas `resources.energy.eia.crude_oil_production` @ WLD, level change, thousand b/d | mb/d (×1000), kb/d |
| `oil_price_assumption` | variable `oil_price.global` @ WLD, percent change (user-stated price assumption, not a REVA estimate) | percent |
| `export_ban` | variable `trade.export_restriction` @ actor, occurrence | share of exports banned (only 1 = complete) |
| `policy_rate_change` | atlas `imf.policy_rate` @ each country of interest, pp change | bp (×0.01), pp |
| `import_ban` | event type `sanction_episode` @ target, occurrence | share of imports banned (only 1) |
| `statutory_retirement_age` | variable `pension.minimum_pension_age` @ actor, **level** (fed only to the retirement model) | years (×12), months |

Each direct change carries its `basis_fr/basis_en` (e.g. "full compliance, no offsetting supply, no stock draw"):
the decision → variable translation is a stated scenario assumption, shown on every result.

Reference decisions (`registry/predict/reference_decisions.yaml`): OPEC+ −2 mb/d from 2027Q1 (held); India rice
export ban 2027Q1, 4 quarters; ECB +50 bp 2027Q1 (DEU, FRA, ITA, ESP, NLD — a selection, not the whole euro area);
EU ban on Russian oil from 2027Q1; France minimum pension age 65 in 2027 (reference path `law_in_force_2026`,
first birth cohort 1962 = 2027 − 65, *derived*). Analogue anchors chosen by REVA's curator and verified by
identifier against the dev KG: OPEC+ → Q4574284 "1970s energy crisis", Q1925730 "1979 energy crisis"; EU ban →
Q111022187 "international sanctions during the Russian invasion of Ukraine", Q111034374 "economic impact of the
Russian invasion of Ukraine", `eusanct:case:2014022701`. No analogue with sourced claims was found for the rice
ban, the ECB hike or the French reform (none invented).

## 3. Propagation

* **Matching** by identifier only: an estimate's `cause` equals the reached variable (kind + id), or PRED-EFFECT's
  curated `atlas_concepts` link relates them (both directions). Never by name.
* **Geography**: variables listed `global` in `registry/predict/variables.yaml` (world crude production,
  `oil_price.global`) live at WLD; a global cause feeds country-level effects of every country of interest, a
  country cause feeds its own country.
* **Units** (no implicit conversion): changes are `percent`, `log_points`, `pp`, `pp_gdp`, `occurrence`,
  `std_dev` or `level:<unit>`. `elasticity`/`pass_through` map percent → percent; `semi_elasticity` pp or unit →
  percent; `slope` per its unit; `response` = point / shock size per shock unit (linear scaling, stated; flagged
  as *linear extrapolation* beyond 3× the published shock). Percent ↔ log points is accepted with a stated
  small-change approximation; `std_dev` shocks are not propagated (the cause's SD is unknown).
* **Time** (quarterly grid from the start): a lag profile (`irf`) is read as the response to a *sustained* change
  of the cause (step response, an assumption) and composed by superposition of the cause path's increments,
  `y_t = Σ_s R(t−s)·(x_s − x_{s−1})`. Monthly profiles are averaged per quarter; annual responses (and `impact` /
  `at_horizon` in years) cover the four quarters of their year; a lag the source did not publish leaves the period
  missing (`lag_not_published`), never interpolated. Cumulative / peak / average / long-run / unspecified
  horizons give an **undated** result; an undated estimate fed by a dated path uses the last available value
  (stated). A decision of finite duration produces a step up and a step down.
* **Monte Carlo**: one named stream per estimate (`rng.stream(seed, "reva.predict.engine@1.0.0", estimate_key, 0)`),
  β = mean + sd·z with `normal_approx` (basis `se`, `ci` = symmetric interval, `range` = labelled heuristic,
  `dispersion_across_estimates` = heterogeneity, labelled). The same z at every horizon of one estimate
  (comonotone, stated). No published uncertainty → the estimate is a scenario point: draws are degenerate and the
  variable says `uncertainty_complete: false` with the estimate keys. Common random numbers across decisions
  (tested: doubling the decision doubles every quantile exactly).
* **Plugins**: run once per (model, reached variable). IO and crude exposure are exactly linear in the price
  change for a fixed pass-through, so the unit response is computed once and multiplied by each draw; their
  single-period input is the mean change over the first four quarters (one annual accounting period, stated).
  Plugin outputs are terminal (an IO basket at basic prices is not a CPI; a border-value exposure is not a GDP loss).
* **Baseline**: per (variable, country) the latest *actual* atlas cell (projection flags excluded, same rule as the
  atlas headline layer), held flat and labelled persistence; where PRED-REGISTRY sealed a baseline forecast for
  exactly an engine period (`2027-Q1` ↔ `2027Q1`), that forecast is used for that period (preferred model: IMF WEO
  reference, else AR, else persistence). A scenario level (baseline + change) is shown only when the units compose
  (pp on a %-unit concept, percent on a level concept).

## 4. Honesty gates

| Gate | Rule |
|---|---|
| Validation label | `validation.status = unvalidated` and label « scénario non validé — pas une probabilité » unless `registry/predict/validated_chains.yaml` holds the chain's signature (sha256 of its sorted estimate keys and plugin versions) with **both** a back-test record and a calibration record. The file is empty: every result today is unvalidated. |
| Unquantified links | sign-only estimates and sourced causal claims touching the decision's anchors are listed (`unquantified_links`) with their wording, source, tier and grade; the API contract and the UI reject such an entry if it carries a number. |
| Double counting | one estimate per edge (rule `estimates.CHOICE_RULE`: exact scope > world panel > group/unstated; published uncertainty > none; lag profile > single horizon > undated; identification family; latest as-of; key), the others listed as alternatives "non additionnées"; one path per variable (breadth-first, the shortest path wins; any other path is listed as "chemin alternatif non cumulé"). Nothing is ever summed or averaged. |
| Applicability | out-of-scope estimates are excluded with a reason; world-panel and country-group scopes are used with a warning; data ending more than 20 years before the start (or before it) is flagged; units must match; `as_of_max` drops estimates not yet knowable (replays). |
| Typed gaps | no estimate for a cause, variable outside the effect vocabulary, plugin inputs absent from the pinned release (never zero-filled), missing lags, absent baseline: all listed in `gaps`. |
| Provenance | every hop names its estimate key, source kind/key, citation, locator, quote, raw sha256 / run, as-of, the source's caveats and REVA's applicability note; plugin hops name the model version, release id, dataset sha256 and the plugin's assumptions. |

## 5. API and UI

`GET /v1/predict/decisions` → `{decisions, instruments, label}` (envelope `context_mode: registry`).
`POST /v1/predict/decision` body `{reference_id | decision, draws (100–5000), seed, as_of_max?, origin}` →
envelope `context_mode: simulation`, `modality: assumption_conditioned_scenario`, warnings `unvalidated_scenario`,
`not_financial_advice` (+ `effect_layer_absent`), data = the engine result (`label`, `validation`,
`disclaimers_fr`, `direct_changes`, `variables[]` with quarterly/yearly periods or an undated interval, `hops[]`,
`unquantified_links[]`, `excluded_links[]`, `alternative_paths[]`, `gaps[]`, `effect_layer`, `chain`,
`provenance`). Read-only transaction.

UI `/v3/decision` (« Décision », in the V3 sub-navigation): pick a reference decision or compose one (instrument,
actor/target, magnitude and unit, start, duration, horizon, countries, draws, seed; invalid input is refused before
any request), then: validation block with the label, the judge requirements and the chain signature; direct
changes with their basis; one card per variable (fan chart 5–95 % / 25–75 % / median against zero, missing periods
marked and never drawn as zero, table of values; undated results as an interval bar; "distribution incomplète"
when an estimate on the path has no published uncertainty); the causal path with each estimate's wording, key,
locator, coefficient ± σ, applicability and source link; liens non chiffrés; excluded estimates and non-cumulated
paths; gaps.

## 6. Tests

* Unit (`tests/unit/predict/test_predict_engine.py`, 23, synthetic estimates only): the five reference decisions and
  their exact unit factors; schema refusals; a static hop reproduces N(β·Δ, (sd·Δ)²) quantiles; step-response
  superposition with a finite duration (exact); missing lags stay missing; two-hop composition; response scaling;
  one estimate per edge (exact scope chosen, world panel listed, not summed); one path per variable; scope and unit
  exclusions; sign-only estimates and anchor claims stay unquantified; validation label only with both records;
  as-of filter; seeded reproducibility and common random numbers; baseline levels (persistence and registry path);
  `normal_approx` fallback; IO plugin = hand-solved Leontief system; crude plugin = the accounting identity;
  retirement plugin run once (780 months, cohort 1962) and its refusal as a gap; extrapolation flag; annual impact
  response of a user-stated oil assumption.
* Integration (`tests/integration/test_predict_api.py`, 4, throwaway DB): reference list; a composed decision
  propagated through a `kg.effect_estimate` row written with `reva.effects.load_effect_estimates` (synthetic REVA
  run, test_fixture) — median 0.5 pp, missing lag, out-of-scope DEU excluded, simulator gaps on an empty release;
  reference decisions report gaps, not numbers; 404/422 problems.
* UI (`ui/src/v5/decision/decision.test.tsx`, 6): full screen flow, form refusal before any request, "nothing
  quantified" message, contract rejects a numbered unquantified link and an unlabelled result.

## 7. Reference decisions on the dev DB (read-only)

RUN_OUTPUTS_PLACEHOLDER
