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

Run: `uv run python -m reva.predict.run --reference all --draws 2000 --seed 0` (role `reva_sim`, `SET TRANSACTION READ
ONLY`; nothing written), 2026-10-17 ~14:30 UTC. State of the dev DB at that time: migration 0019 applied but
**`kg.effect_estimate_current` empty** (PRED-EFFECT's `atlas.effects_lit` ingest and PRED-EVENTSTUDY's REVA-run
writes were queued behind the shared dev-DB lock); one published live belief release
(`3536d02e…`, 482 estimates: HS 2709 crude trade, nominal GDP, population; **no pinned dataset**: no FIGARO table, no
Eurostat/ILO retirement inputs); latest live atlas snapshot `717e8452…`; forecast registry readable.

**Result in one sentence: none of the five reference decisions yields a number on the dev DB today, and that is the
correct answer.** Each stops at a typed gap: no attributed estimate has the decision's direct variable as its cause
(OPEC+ crude supply → price, policy rate → anything, sanction episode → anything, export restriction is not even in
the effect vocabulary), or the simulator's inputs are not pinned (retirement). What remains is attributed context:
sourced causal claims around the analogue episodes (7 for OPEC+, 8 for the EU ban), listed without numbers.
First decision of a process: ~43 s (almost all of it the atlas query-file integrity hash and the catalog load, cached afterwards), then < 1 s per decision.

### `ecb_plus_50bp_2027q1` — La BCE relève ses taux directeurs de 50 points de base au 1er trimestre 2027

* Label: **scénario non validé — pas une probabilité** (validation: `unvalidated`, chain signature `4f53cda18c2baa0c…`).
* Effect layer: `present` (kg.effect_estimate_current via reva.effects; 0 estimates matched); chain: none.
* Read-only run: release `3536d02e-1f88-4ef9-a5d2-389d3ea93f01`, atlas snapshot `717e8452dc07da89…`, forecast registry: read, 2000 draws, seed 0, 42.827 s.
* Direct changes (scenario assumptions): `imf.policy_rate` @ DEU 0.5 percentage_points (step, held); `imf.policy_rate` @ FRA 0.5 percentage_points (step, held); `imf.policy_rate` @ ITA 0.5 percentage_points (step, held); `imf.policy_rate` @ ESP 0.5 percentage_points (step, held); `imf.policy_rate` @ NLD 0.5 percentage_points (step, held)

**No quantified variable.**

Gaps:
* `no_effect_estimate_for_cause`: aucune estimation d'effet n'a pour cause imf.policy_rate : la propagation s'arrête ici

### `eu_ban_russian_oil_2027q1` — L'UE interdit toute importation de pétrole russe à partir du 1er trimestre 2027

* Label: **scénario non validé — pas une probabilité** (validation: `unvalidated`, chain signature `4f53cda18c2baa0c…`).
* Effect layer: `present` (kg.effect_estimate_current via reva.effects; 0 estimates matched); chain: none.
* Read-only run: release `3536d02e-1f88-4ef9-a5d2-389d3ea93f01`, atlas snapshot `717e8452dc07da89…`, forecast registry: read, 2000 draws, seed 0, 0.48 s.
* Direct changes (scenario assumptions): `sanction_episode` @ RUS 1 occurrence (occurrence, held)

**No quantified variable.**

Liens non chiffrés (8):
* Selon Wikidata (déclaration Q110999040$0a72faa9-472b-eccf-8879-90e92d0f26d3, révision 2553782105, édition communautaire), invasion de l'Ukraine par la Russie a pour effet sanctions internationales lors de l'invasion de l'Ukraine par la Russie ; REVA n'établit pas de causalité. (grade D)
* Selon Wikidata (déclaration Q111022187$fc03c281-4881-4ac8-fa60-50f00864d2b6, révision 2553619590, édition communautaire), sanctions internationales lors de l'invasion de l'Ukraine par la Russie a pour cause invasion de l'Ukraine par la Russie ; REVA n'établit pas de causalité. (grade D)
* Selon Wikidata (déclaration Q111022187$18b240d2-415e-a337-18fa-64c56658ce74, révision 2553619590, édition communautaire), sanctions internationales lors de l'invasion de l'Ukraine par la Russie a pour effet Q113320321 (libellé non chargé) ; REVA n'établit pas de causalité. (grade D)
* Selon Wikidata (déclaration Q17373979$21baffb0-4ac3-a2be-9d11-a3c5f7d2e6cc, révision 2459817118, édition communautaire), sanctions contre la Russie a pour effet Impact économique de la guerre d'Ukraine ; REVA n'établit pas de causalité. (grade D)
* Selon Wikidata (déclaration Q110999040$30a0e890-4fae-b85b-5457-71d5316f6192, révision 2553782105, édition communautaire), invasion de l'Ukraine par la Russie a pour effet Impact économique de la guerre d'Ukraine ; REVA n'établit pas de causalité. (grade D)
* Selon Wikidata (déclaration Q111034374$da5887b5-4cf8-e184-ea22-d72deb567ca4, révision 2522132221, édition communautaire), Impact économique de la guerre d'Ukraine a pour cause invasion de l'Ukraine par la Russie ; REVA n'établit pas de causalité. (grade D)
* Selon Wikidata (déclaration Q111034374$044f14d4-4c4a-3606-431c-38c4bdf1a3a1, révision 2522132221, édition communautaire), Impact économique de la guerre d'Ukraine a pour cause sanctions contre la Russie ; REVA n'établit pas de causalité. (grade D)
* Selon eusanct.kondata (jeu de données eusanct_case, version eusanct-1.0, ligne {"columns":["caseid","finaloutcome"],"file":"EUSANCT_Dataset_Case-level.csv","row":317}), Sanctions case 2014022701 (EUSANCT): target Russia (COW 365), from 2014 a pour issue codée Outcome of sanctions case 2014022701 (EUSANCT): Stalemate after Sanction Imposition ; REVA n'établit pas de causalité. (grade B)

Gaps:
* `no_effect_estimate_for_cause`: aucune estimation d'effet n'a pour cause sanction_episode : la propagation s'arrête ici

### `france_retirement_65_2027` — La France porte l'âge légal de départ à la retraite à 65 ans en 2027

* Label: **scénario non validé — pas une probabilité** (validation: `unvalidated`, chain signature `36a351b546dbe9b9…`).
* Effect layer: `present` (kg.effect_estimate_current via reva.effects; 0 estimates matched); chain: `social.retirement_age@1.0.0`.
* Read-only run: release `3536d02e-1f88-4ef9-a5d2-389d3ea93f01`, atlas snapshot `717e8452dc07da89…`, forecast registry: read, 2000 draws, seed 0, 0.893 s.
* Direct changes (scenario assumptions): `pension.minimum_pension_age` @ FRA 780 months (step, held)

**No quantified variable.**

Hops:
* `h2` plugin `social.retirement_age@1.0.0` from variable:pension.minimum_pension_age@FRA: Projection conditionnelle sous hypothèses énoncées (pas une prévision), résultats « qualifiés » : les voies de départ anticipé (carrières longues, invalidité) ne sont pas modélisées ; réponse de l'emploi tirée de la littérature citée (registry/assumptions/retirement_employment_response.yaml).

Gaps:
* `missing_input` (FRA): the retirement model did not run on the pinned release — missing: demography.deaths.age_sex, demography.live_births.mother_age_newborn_sex, demography.population.jan1_age_sex, labour.activity_rate.lfs_age_sex, labour.employment_rate.lfs_age_sex, social.pension_beneficiaries.esspros_old_age, social.pension_expenditure.esspros

### `india_rice_export_ban_2027q1` — L'Inde interdit les exportations de riz pendant un an à partir du 1er trimestre 2027

* Label: **scénario non validé — pas une probabilité** (validation: `unvalidated`, chain signature `4f53cda18c2baa0c…`).
* Effect layer: `present` (kg.effect_estimate_current via reva.effects; 0 estimates matched); chain: none.
* Read-only run: release `3536d02e-1f88-4ef9-a5d2-389d3ea93f01`, atlas snapshot `717e8452dc07da89…`, forecast registry: read, 2000 draws, seed 0, 0.038 s.
* Direct changes (scenario assumptions): `trade.export_restriction` @ IND 1 occurrence (occurrence, 4 quarters)

**No quantified variable.**

Gaps:
* `variable_not_in_effect_vocabulary`: la variable trade.export_restriction n'est pas dans le vocabulaire des effets (registry/effects/variables.yaml) : aucune estimation ne peut la citer comme cause
* `no_effect_estimate_for_cause`: aucune estimation d'effet n'a pour cause trade.export_restriction : la propagation s'arrête ici

### `opec_plus_cut_2mbd_2027q1` — OPEP+ réduit sa production de 2 mb/j à partir du 1er trimestre 2027

* Label: **scénario non validé — pas une probabilité** (validation: `unvalidated`, chain signature `4f53cda18c2baa0c…`).
* Effect layer: `present` (kg.effect_estimate_current via reva.effects; 0 estimates matched); chain: none.
* Read-only run: release `3536d02e-1f88-4ef9-a5d2-389d3ea93f01`, atlas snapshot `717e8452dc07da89…`, forecast registry: read, 2000 draws, seed 0, 0.192 s.
* Direct changes (scenario assumptions): `resources.energy.eia.crude_oil_production` @ WLD -2000 thousand barrels per day (step, held)

**No quantified variable.**

Liens non chiffrés (7):
* Selon Wikidata (déclaration Q4574284$C3AB6850-FC50-4C5D-8C00-1FF73F550218, révision 2547562028, édition communautaire), choc pétrolier a comme facteur contributif guerre du Kippour ; REVA n'établit pas de causalité. (grade D)
* Selon Wikidata (déclaration Q4574284$31090F54-ABB6-4CCD-B19B-1DA9208E1960, révision 2547562028, édition communautaire), choc pétrolier a comme facteur contributif Révolution iranienne ; REVA n'établit pas de causalité. (grade E)
* Selon Wikidata (déclaration Q4574284$0FC8E5B0-E177-4FEF-803E-001221CBED95, révision 2547562028, édition communautaire), choc pétrolier a pour effet Q615231 (libellé non chargé) ; REVA n'établit pas de causalité. (grade D)
* Selon Wikidata (déclaration Q4574284$7B5C794D-A7E8-43A8-AD5A-4BB3265E1557, révision 2547562028, édition communautaire), choc pétrolier a pour effet Q800510 (libellé non chargé) ; REVA n'établit pas de causalité. (grade D)
* Selon Wikidata (déclaration Q126065$68f55414-4755-ab56-d41e-940d1aeca749, révision 2556614482, édition communautaire), Révolution iranienne a pour effet deuxième choc pétrolier ; REVA n'établit pas de causalité. (grade D)
* Selon Wikidata (déclaration Q1925730$29744924-491a-ae23-d23a-a4a940311fa9, révision 2537182131, édition communautaire), deuxième choc pétrolier a pour cause Révolution iranienne ; REVA n'établit pas de causalité. (grade D)
* Selon Wikidata (déclaration Q1925730$5d7fc053-472a-1391-891c-3d443f694158, révision 2537182131, édition communautaire), deuxième choc pétrolier a pour cause guerre Iran-Irak ; REVA n'établit pas de causalité. (grade E)

Gaps:
* `no_effect_estimate_for_cause`: aucune estimation d'effet n'a pour cause resources.energy.eia.crude_oil_production : la propagation s'arrête ici



### Illustration (not a reference decision): a user-stated oil-price assumption

To show the numeric path on real dev-DB data, the composed decision below states the oil-price change itself
(instrument `oil_price_assumption`; the +10 % for 4 quarters is an arbitrary round assumption, **not** an estimate
of any OPEC+ decision's effect on prices). Only the crude-exposure simulator could run (border values and GDP of
2024 in the release for FRA, USA, CHN); the IO model has no pinned FIGARO table, and no estimate was loaded for
`oil_price.global` yet (when PRED-EFFECT's load lands, Choi et al. 2017 — +0.4 pp CPI inflation per +10 % oil in
the year of the shock, no SE — will add `imf.inflation_cpi_avg` for countries in its 72-economy group scope,
flagged *group membership not verified* and *scenario point, no published uncertainty*).

### `illustration_oil_plus_10pct_2027q1` — Illustration : hypothèse d'un prix mondial du brut +10 % pendant 4 trimestres à partir de 2027T1

* Label: **scénario non validé — pas une probabilité** (validation: `unvalidated`, chain signature `053a440c0d460ff5…`).
* Effect layer: `present` (kg.effect_estimate_current via reva.effects; 0 estimates matched); chain: `economic.crude_exposure@1.0.0`.
* Read-only run: release `3536d02e-1f88-4ef9-a5d2-389d3ea93f01`, atlas snapshot `717e8452dc07da89…`, forecast registry: read, 2000 draws, seed 0, 37.864 s.
* Direct changes (scenario assumptions): `oil_price.global` @ WLD 10 percent (step, 4 quarters)

| Variable @ geo | unit | kind | value (5 % / median / 95 %) | uncertainty complete |
|---|---|---|---|---|
| Coût extérieur net du brut (valeur frontière), en % du PIB de référence — pas une perte de PIB @ FRA | pp_gdp | plugin_output | first_round: 0.0939 / 0.0939 / 0.0939 | yes |
| Coût extérieur net du brut (valeur frontière), en % du PIB de référence — pas une perte de PIB @ USA | pp_gdp | plugin_output | first_round: 0.0191 / 0.0191 / 0.0191 | yes |
| Coût extérieur net du brut (valeur frontière), en % du PIB de référence — pas une perte de PIB @ CHN | pp_gdp | plugin_output | first_round: 0.173 / 0.173 / 0.173 | yes |

Hops:
* `h2` plugin `economic.crude_exposure@1.0.0` from variable:oil_price.global@WLD: Les prix frontière du brut importé et exporté varient comme le prix du brut (multiplicateur 1 + Δ %), quantités fixes, une période comptable annuelle ; ce n'est ni une perte de PIB ni une inflation.

Gaps:
* `missing_input` (DEU): crude border values or GDP absent from the pinned release (never zero-filled) — M (trade.hs2709.import_value.world_total), X (trade.hs2709.export_value.world_total), G (economics.gdp.nominal_current_usd)
* `missing_input` (IND): crude border values or GDP absent from the pinned release (never zero-filled) — M (trade.hs2709.import_value.world_total), X (trade.hs2709.export_value.world_total), G (economics.gdp.nominal_current_usd)
* `missing_input` (JPN): crude border values or GDP absent from the pinned release (never zero-filled) — M (trade.hs2709.import_value.world_total), X (trade.hs2709.export_value.world_total), G (economics.gdp.nominal_current_usd)
* `plugin_data_missing`: the baseline release 3536d02e-1f88-4ef9-a5d2-389d3ea93f01 pins no economics.io.figaro_ii_table dataset (FIGARO); reason: unavailable
* `no_effect_estimate_for_cause`: aucune estimation d'effet n'a pour cause oil_price.global : la propagation s'arrête ici



Reading the table: +10 % on crude border prices with fixed 2024 quantities raises France's net crude border cost by
0.094 % of 2024 GDP (USD 29.7 bn imports vs 0.016 bn exports), China's by 0.17 %, the United States' by 0.019 %
(large own exports offset imports). Degenerate (5 % = 95 %) because the assumption is stated, not estimated; it is
an accounting exposure, not a GDP loss, an inflation or a forecast. DEU, IND, JPN: inputs absent from the release
(typed `missing_input`, never zero).

## 8. What is missing, and why

* **Effect estimates for the reference decisions' first links.** PRED-EFFECT's curated publications cover oil price →
  CPI, energy prices → CPI (IO), exchange-rate pass-through, fiscal multipliers, Okun, Phillips slopes, trade
  elasticity — none starts from crude *supply*, a policy *rate*, an *export restriction* or a *sanction*. The
  missing estimates, in order of value: (1) oil supply shock → real oil price (e.g. Kilian 2009; Baumeister &
  Hamilton 2019; Caldara, Cavallo & Iacoviello 2019 — *names of candidate sources, nothing transcribed*); (2)
  monetary-policy shock → euro-area HICP, GDP, unemployment (local projections / SVAR with IRFs and SEs); (3) export
  restriction → world rice price; (4) sanctions/embargo → target GDP and oil export volumes (PRED-EVENTSTUDY's TIES
  → GDP per capita estimate, +0.3 % CI −1.2..+1.7, will match `sanction_episode` @ RUS once written if its scope lists
  RUS). The engine needs no change for any of them: they propagate as soon as they are in `kg.effect_estimate_current`.
* **Variables outside the effect vocabulary**: `trade.export_restriction`, `pension.minimum_pension_age` (decision
  variables of two instruments). Request for PRED-EFFECT: add them (or an event type `export_restriction` to
  `registry/events/taxonomy.yaml`) so estimates can cite them.
* **Simulator inputs not pinned on the dev DB**: FIGARO 2024 (`economics.io.figaro_ii_table`, ~13 MB Parquet) and the
  retirement model's 7 Eurostat datasets (LFS employment/activity, ESSPROS beneficiaries/expenditure, population,
  deaths, births). They need a belief release that pins them (the V2 pipeline does it; lead operation).
* **Crude inputs** for DEU, IND, JPN are absent from the release (Comtrade/WDI coverage of that release).
* **Monthly price horizons** (roadmap: prices 1–12 months): the grid is quarterly; monthly lag profiles are
  averaged per quarter. A monthly grid is a contained change in `engine.py` (`H`, labels) once daily/monthly price
  estimates exist.
* **Feedback** (e.g. inflation → policy rate reaction) is out of scope: the propagation graph is acyclic by
  construction (visited set), as in `reva.simulation.coupling`.
* **Validation**: no chain has a back-test or calibration record; the judge (PRED-REGISTRY + PRED-VINTAGE) must write
  `registry/predict/validated_chains.yaml` entries. Until then no probability is ever shown.

## 9. Owner decisions

1. **Step-response reading of lag profiles.** Published IRFs are usually responses to a one-time shock with its own
   persistence; the engine reads them as responses to a sustained change and superposes increments (stated on every
   result). Alternative: impulse convolution (needs the cause's own dynamics). Keep the stated assumption?
2. **Group scopes.** Estimates scoped to a verbatim country group ("72 advanced and developing economies") are used
   for any country with a warning (membership not machine-checked). Stricter option: require a machine-readable
   country list for use outside "exact"/"WLD".
3. **Choice rule vs. showing all estimates.** One estimate per edge is chosen deterministically (alternatives
   listed). Alternative: run one scenario per estimate (an ensemble of chains, never averaged) — more honest about
   model disagreement, more output.
4. **Comonotone horizons** (one z per estimate across horizons) overstates the persistence of uncertainty when the
   source's horizons are independent estimates; no covariance is published, so either choice is an assumption.
5. **Analogue anchors** are REVA curation (5 ids). Should the curator also add anchors for the rice ban, the ECB hike
   and the French reform once suitable KG events exist?
6. **`oil_price_assumption`** lets a user state a price change (labelled as the user's assumption). Keep it, or
   allow only decisions whose first link is an attributed estimate?

## 10. Commands

```bash
uv run pytest -q tests/unit/predict                       # 23 unit
uv run pytest -q tests/integration/test_predict_api.py    # 4 integration (throwaway DB)
(cd ui && npx vitest run src/v5)                          # 6 UI
uv run ruff check src/reva/predict src/reva/api/routes/predict.py && uv run mypy src/reva/predict src/reva/api/routes/predict.py
uv run python scripts/export_contracts.py --check
# reference decisions on a database, read-only (JSON):
uv run python -m reva.predict.run --reference all --draws 2000 --seed 0 --out /tmp/pred.json
# after PRED-EFFECT / PRED-EVENTSTUDY loads, or once a release pins FIGARO + the retirement datasets: rerun the line
# above and replace §7. API: POST /v1/predict/decision {"reference_id": "opec_plus_cut_2mbd_2027q1"}; UI: /v3/decision
```

## 7b. Re-run after the effect layer landed (lead, 2026-10-17)

`uv run python -m reva.predict.run --reference all --draws 2000 --seed 0` with 77 current effect estimates
(72 published, 5 REVA event-study runs). Raw output kept in the lead's scratchpad (`ops/predict_run.json`).
Every result: « scénario non validé — pas une probabilité ».

* **ECB +50 bp (2027Q1), euro-area countries (DEU, FRA, ITA, ESP, NLD):** peak effects, date not published
  (undated), scaled linearly from the ECB Occasional Paper 377 meta-analysis (+25 bp → ×2, the engine's own
  scaling): CPI inflation −0.1 pp, real GDP level −0.5 %, consumer price level −0.36 %. Degenerate (no
  published uncertainty used) → flagged incomplete. Propagation stops after one hop (no estimate starts from
  inflation or GDP).
* **EU ban on Russian oil (2027Q1), RUS:** two estimates of the same edge (sanction episode → GDP per capita)
  are shown side by side, never averaged: REVA's event-study run `run:es-1b3eff04653ec3971c9c:h5` (TIES
  sanctions → GDP per capita growth, median +0.18 %, 5–95 % −0.38…+0.77, i.e. no significant effect, estimated
  on data to 2025) and Gutmann–Neuenkirch–Neumeier (2022) year-1 response −1.32 pp (no published uncertainty,
  scope "158 countries", RUS membership not machine-checked; lags 0–3 quarters not published → missing).
  8 Wikidata causal claims listed as liens non chiffrés.
* **OPEC+ −2 mb/d:** stops — no estimate has crude production as cause (Baumeister–Hamilton gives supply/demand
  elasticities, not a price response; deriving one would be REVA's own calculation).
* **India rice export ban:** stops — no estimate for `trade.export_restriction` (CEPR source refused, 403).
* **France 65 in 2027:** stops — the retirement model's Eurostat inputs are not pinned in the release.
