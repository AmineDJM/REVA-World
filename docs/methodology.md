# Methodology (V1)

This page documents the methods V1 actually runs, their formulas and their
limits. Every method is a registered, versioned model (`registry/models/methods.yaml`,
`model.version`) and outputs carry the method key/version. None of these methods
produces a causal or welfare claim.

## 1. Belief publication: `belief.source_select` 1.0.0 (`source_select_v1`)

Implementation: `src/reva/belief/selection.py`, `src/reva/belief/publication.py`.
Policy: `registry/policies/selection.yaml` (`selection-1.1.0`).

1. **Visibility filter** (before revision selection): only observations recorded
   at or before the knowledge cutoff and of the release's origin class are
   considered. A release has exactly one origin, `live` or `test_fixture`; the
   two are never mixed, and a `test_fixture` release cannot be published when
   `REVA_ENV=production` (`reva.kernel.context.assert_publishable`).
2. **Select the revision** per lineage `(source, origin, logical_key)` over its
   whole pre-cutoff history. Retraction and eligibility are applied to that
   history, not before it:
   * if the lineage's latest pre-cutoff revision is retracted (retraction recorded
     at/before the cutoff), the lineage is excluded and flagged
     `retracted_lineage_excluded:<source>`; it never falls back to the superseded
     value;
   * if the latest revision is not eligible, the latest eligible, non-retracted
     earlier revision is used and flagged `superseded_revision_selected`.
   Eligibility is the latest `evidence.eligibility` decision recorded at/before
   the cutoff whose scope (`subject_ids`, `years`) covers the record
   (`evidence.is_eligible`). Because only rows recorded at/before the cutoff are
   visible, a later correction can never change an earlier as-of answer.
3. **Apply the published preference** for the exact estimand (WDI for population
   and GDP; Comtrade public preview for HS 2709 values and flow-specific net
   weights; Eurostat for 1 January age × sex). An estimand with no entry in the
   preference policy is not selected at all.
4. **Publish `selected_report`** with the source's own uncertainty status
   (typically `unquantified`); no uncertainty is invented.
5. **Keep alternatives and discrepancy flags** (`source_disagreement:*`,
   `multiple_lineages_same_source`). Incomparable definitions are never averaged
   and never picked arbitrarily: when candidate lineages differ on a
   distinguishing dimension (`flowCode`, `partnerCode`) the estimand is
   **withheld** and listed under `withheld_estimands` in the release manifest.

**Atomic publication.** The publishing transaction first takes the evidence
snapshot lock exclusively (every evidence writer holds it shared), then the
publication advisory lock. With `cutoff=None` the knowledge cutoff is read from
the database clock (`clock_timestamp()`) **after both locks are granted**, and
selection runs in that same transaction. An explicit cutoff later than the
database clock is rejected; an explicit past cutoff is honoured. Objects (the
Parquet estimate table and manifests) are written to the object store first;
the release, estimates, inputs, artifact references, outbox event and the next
publication sequence then commit in one transaction. A release is immutable.

**As-of reads.** `release_as_of(T)` returns, among published releases (optionally
of one origin), the one with the highest publication sequence whose
`published_at <= T` **and** `knowledge_cutoff <= T`
(`src/reva/belief/snapshots.py`). It answers "what REVA actually had published at
T" (`operational_as_known`); it never reconstructs what a source said at T.

**Limits.** A selected official value is not a calibrated posterior about
reality. Source-family dependence is recorded but not modelled in V1.

## 2. Crude-oil border-value exposure: `economic.crude_exposure` 1.0.0

Implementation: `src/reva/models/economic/crude_exposure.py` (pure; `decimal`,
50-digit context), run as simulation plugin #1 (`src/reva/simulation/crude_plugin.py`)
by the model-agnostic engine `src/reva/simulation/engine.py`. Scenario contract:
`contracts/scenario.schema.json` (ScenarioSpec 2.0.0, a union on `model_key`; the
V1 appendix `contracts/scenario.v1.schema.json` specs keep validating). Moving the
model behind the plugin interface changed no output: result rows, output digests
and L0 cache keys are bit-identical to the V1 engine (golden tests, including the
recorded live demo).

**Simulation platform (V2).** Every model is a plugin (`reva.simulation.plugins`)
that declares its inputs; the platform resolves them only from the scenario's
pinned baseline release — scalars as selected estimates of exactly the requested
calendar year (a typed `Missing` stays missing), large tables as *dataset inputs*
the release pinned by sha256 at publication (re-hashed on read; unpinned is a
`missing_input`, never zero). Unless a model reports missing inputs itself, the
platform refuses the run before computing. Multi-period models publish one row per
half-open period `[start, end)`. Models seeded as `candidate` with review status
`maintainer_review_pending` run only labelled "experimental model, not yet
reviewed". Details: `docs/dev/v2_eng.md`.

This is an **assumption-conditioned border-value accounting stress test for one
annual accounting period**. It is not a forecast and not a GDP loss.

Inputs for one country and one year: M and X, the same-year source-reported HS
2709 import and export border values (world partner) in current USD, and G,
nominal GDP in current USD for the same year. The valuation label is kept per
value (imports CIF or FOB as reported, exports FOB; undeterminable bases are
labelled and flagged). All three must be `selected_report` estimates of the
scenario's pinned, published baseline release whose support is exactly the
reference calendar year.

**L1 (fixed quantities)**, multipliers `0 < p_M, p_X <= 3` (registered envelope):

    ΔM = M × (p_M − 1)
    ΔX = X × (p_X − 1)
    Δnet_external_cost = ΔM − ΔX
    exposure_percent_of_baseline_GDP = 100 × Δnet_external_cost / G

Reference case: M = 100, X = 40, G = 1000, p_M = p_X = 1.25 → ΔM = 25, ΔX = 10,
Δnet = 15, exposure = 1.5 %. All multipliers 1 → 0. A negative Δnet is net
receipt exposure under the stated assumptions. Each L1 output row carries
`unquantified` uncertainty (no published input uncertainty, no error model).

**L2 (labelled elasticity sensitivity)** with assumed quantity elasticities ε
(registered envelopes ε_M ∈ [−2, 1], ε_X ∈ [−1, 2]):

    M' = M × p_M^(1 + ε_M)      X' = X × p_X^(1 + ε_X)

ε = 0 executes exactly the L1 expression (bit-identical, not approximately). An
L2 run publishes, per country and output:

* the fixed-quantity `L1` row (unquantified uncertainty);
* one row per grid point (variant `L2:eM=…;eX=…`, unquantified uncertainty);
* a **separate `L2:sensitivity_range` row** whose value is a
  `sensitivity_range` object (min/max over the grid ∪ {ε = (0, 0)}, so it always
  contains the L1 value) and whose uncertainty kind is `sensitivity_range`
  **without any coverage or confidence level**. It is assumption sensitivity,
  not a confidence, credible or prediction interval, and says nothing about input
  measurement uncertainty. With the demo grid (ε_M ∈ {−0.5, −0.25, 0},
  ε_X ∈ {0, 0.25}) every non-zero assumption lowers the net bill, so the range is
  **one-sided by construction**: L1 is its upper end. It is not an interval
  around L1.

**L0** is an exact-specification cache lookup: the same full specification
(excluding only the fidelity), the same pinned baseline release and the same
model manifest (which includes a digest of the model and engine source) reuse a
prior succeeded L1/L2 outcome. Anything else is a recorded `cache_miss`
(outcome status `unsupported`); L0 never computes an answer.

**L3/L4** are reserved: requests fail with a `capability_gap` problem (HTTP 422
at the API; recorded as an `unsupported` run outcome in the worker). There are no
justified parameter distributions and no competing models.

**Applicability.** One annual accounting period; nominal current-USD annual flow
totals for one reference year; other horizons, bases or mixed years are rejected
(`capability_gap` / `incompatible_semantics`). `missing_input_policy` is `fail`:
if any requested country lacks an input, the whole request is refused (API
pre-flight returns 422 `missing_input`; a worker run is recorded with status
`missing_input` and no result rows) — a missing input is never imputed or
treated as zero. The default scenario uses FRA, CHN and USA for 2023. Algeria
(DZA) has **no** Comtrade HS 2709 world-total rows for 2020–2023 (the provider
returned zero rows; stored as typed `Missing`), so a DZA request for those years
is `missing_input`. For 2024 the provider returns one estimated row per flow
(classification H4, `legacyEstimationFlag=6`, `provider_estimation_flagged=true`):
imports 5,564 USD, exports about 14.2 billion USD; these are provider estimates,
not direct customs observations. Five-year GDP, welfare, inflation, fiscal or
balance-of-payments questions are unsupported. Kilian (2009) is the reason one
universal oil-price multiplier is not attached to the result.

## 3. Forecasting tournament

Models (`src/reva/models/forecasting/`), strictly positive annual series only:

| Model | Point forecast | Uncertainty |
|---|---|---|
| `forecast.last_value` 1.0.0 (naive baseline, always present) | last training value | `unquantified` |
| `forecast.log_drift` 1.0.0 | `exp(z_n + h·c)`, `c` = mean log difference (log-normal median) | Student-t (T−1 df) log-scale prediction interval with scale `s·sqrt(h·(1 + h/T))`; `unquantified` when fewer than 3 log differences or zero sample variance |
| `forecast.local_level_log` 1.0.0 | Kalman predictive median of the log local-level model (statsmodels, deterministic MLE) | plug-in state-space **prediction** interval for the future released statistic; **not calibrated** in short windows (nominal 0.80 covered about 0.66–0.70 in simulation), so empirical coverage is reported next to the nominal level |

Values ≤ 0 make the log models ineligible (never clipped). The estimand is the
future *released* statistic, not latent reality.

Tournament (`src/reva/models/tournament.py`, method `reva.models.tournament@2.0.0`;
service `src/reva/evaluation/tournament_service.py`; policy `promotion-1.1.0`):

* rolling expanding-window origins from the 3rd observation; each fit sees only a
  cutoff-scoped view of the series (`reva.evaluation.splits`);
* scores: absolute error, MASE-scaled error (scale from the training window;
  undefined when the scale is zero), and, for probabilistic forecasts only,
  interval coverage, scaled interval score and log-scale CRPS (otherwise
  `not_applicable`, never 0); cross-model rankings use paired common cases;
* promotion decision: ≥ 30 paired (series, origin, horizon) cases; per horizon,
  differentials are averaged across series within each origin year and tested
  with a one-sided **Diebold–Mariano test with the Harvey–Leybourne–Newbold
  correction** (≥ 8 origin years per horizon); p-values across horizons use
  **Holm's step-down** procedure at α = 0.025; any horizon where the challenger
  is significantly worse keeps the incumbent; a credible > 10 % subgroup
  degradation (or an untestably small subgroup with a point breach) routes to
  `needs_review`. Outcomes are `promote`, `keep_incumbent` (with reasons,
  including `insufficient_evaluation`) or `needs_review`. The moving-block
  bootstrap over origin years is reported as a **descriptive** interval only; it
  is not the decision rule. The false-promotion rate under exact-null panels is
  checked in `tests/scientific/test_tournament_calibration.py`;
* a **locked final holdout** (the last 2 calendar years of the panel) is hidden
  from development and opened at most once per model version, only to confirm a
  challenger that passed every development gate (non-inferiority margin 0);
  reuse is refused and recorded;
* the evaluation service runs only in `revised_data_retrospective` mode: scores
  use today's revised values split by year and are labelled "not a real-time
  backtest". A promotion is a recorded decision in `model.promotion` that sets the
  incumbent for later evaluations; V1 publishes no forecasts as beliefs, and
  shadow models with signed promotion (`models.shadow_promotion`, P11) are
  reserved.

**Replay modes** (`src/reva/evaluation/replay.py`): `operational_as_known`
reproduces the release REVA actually had published at the cutoff (typed
`no_release_at_cutoff` if none); `available_then_reconstruction` re-runs
`source_select_v1` only over observations with independently evidenced
availability at the cutoff (unproven records are excluded and counted; it is not
what REVA believed); `revised_data_retrospective` is labelled as not a real-time
backtest. Strict modes never fall back to the retrospective mode, and no LLM is
used in replay.

## 4. Population recipe and compression (France, age × recorded sex)

Implementation: `src/reva/population/` and `src/reva/compression/`.

* **Source path.** The 1 January age × sex table (Eurostat `demo_pjan`) has no
  built-in connector; it enters only through the autonomy Eurostat template
  (`eurostat_jsonstat`) under the approved exact mapping scoped to `geo = FR`
  (`registry/policies/promotion.yaml`).
* **Recipe** (`reva-population-recipe-2`): one geography/reference date. The
  leaves are the finest disjoint cover of the observed age range by source age
  codes (single years where observed, a source age band where no finer observed
  cells tile it), plus `Y_OPEN` (open top group) and `UNK`, by recorded sex M/F.
  Overlapping codes are never both counted; totals, sex `T` and subtotal bands are
  used only for consistency checks and never alter source counts. **Totals gate:**
  the leaves must reproduce each reported `TOTAL` exactly, otherwise the recipe is
  blocked (`InconsistentSourceTotals`) unless an explanation is recorded, and
  materialisation refuses a blocked recipe. Recorded sex keeps the source
  definition (not gender identity). A published 0 is an *observed* zero (no
  structural zeros exist for age × recorded sex); a missing count stays Missing,
  never zero. No age is imputed inside `Y_OPEN`, `UNK` or a band leaf.
* **Materialisation**: ≤ 100,000 weighted representative records in Parquet (not
  PostgreSQL rows). Every nonzero cell receives ≥ 1 record; the rest are allocated
  by largest remainder with exact integer arithmetic. Each record carries the
  exact rational weight `count_c / k_c`, so per-cell weight sums equal the source
  counts exactly. Records are not linked to real individuals.
* **Diagnostics**: max absolute/relative calibration error, effective sample
  size, weight concentration, support violations. Optional IPF raking reports
  infeasibility (`feasible=False`) or non-convergence instead of altering targets;
  calibrated weights are floats (exact fractions are not preserved).
* **Compression**: coarsening into age bands and refinement back to the recipe's
  own leaves conserve total weight exactly (certificates); refinement along any
  dimension the recipe does not contain (income, household, health …) raises
  `UnsupportedRefinement`; coarsening that depends on an unknown `Y_OPEN` bound is
  refused.

**Limits.** The recipe supports only age × recorded-sex questions. It cannot
support childcare, income or household simulations (household synthesis is
reserved, P10), and the crude stress test is deliberately not coupled to it:
dividing a national import-bill change by synthetic people is not a
distributional welfare result. The January population concept is distinct from
WDI mid-year population and the two are never reconciled as the same quantity.

## 5. Value of information (demonstration only)

`src/reva/evaluation/voi.py`: exact and nested Monte Carlo EVSI on a finite
analytic decision problem. Without a declared action set and utility, the system
reports a unitless uncertainty-reduction proxy, never a monetary value.

## Natural-language drafts (V2, `POST /v1/scenario-drafts`)

`src/reva/nl/`: an LLM turns one sentence (French or English, at most 4096
bytes) into a **draft** `ScenarioSpec`. It is a drafting aid, not a method: it
computes nothing, reads no data and creates no evidence.

* **Allow-list.** The model sees a catalogue built at request time from the
  scenario schema and the registered simulation plugins (model keys, countries,
  reference-year range, fidelities, outputs, settable parameter paths), the
  non-implemented capabilities and a few drafting refusal keys. A new model
  becomes draftable when its plugin is registered; nothing is hand-listed.
* **Server checks.** The output is schema-validated; any model key, country,
  output, parameter path/choice or fidelity outside the allow-list **rejects**
  the draft (no substitution, nothing dropped silently). The server fills only
  constants and its own fields (baseline release — the latest published one
  unless given — its knowledge cutoff, schema/model version, seed 0) and runs
  the same validator as hand-written specs. A required value the text does not
  give (for example the reference year or an export multiplier) is asked for
  (`needs_input`), never defaulted; the fidelity defaults to L1 and that choice
  is listed.
* **Numbers.** Every number of the draft is listed with its provenance:
  `stated` (written in the text), `converted_from_stated` (a percentage form,
  e.g. "+20 %" → 1.2), `not_in_text` (proposed by the LLM, flagged) or
  `schema_constant`. Limits are never clamped: "multiplier 1000" stays 1000 and
  is refused by the validator.
* **Unsupported asks** (GDP loss, unemployment, inflation, welfare,
  multi-year dynamics on a single-period model, a model that is not registered,
  data-value overrides, instructions addressed to the system) become typed items
  (`capability_gap` or `policy_refusal`) — from the LLM and from deterministic
  French/English keyword guards — and are never approximated by another model.
* **Restatement.** The `restatement` is generated by the server from the
  validated spec in the input language; the LLM's own paraphrase is returned
  separately (`llm_restatement`) for comparison.
* **Confirmation.** Nothing runs or is stored. The user runs a draft by
  submitting `draft_spec` to `POST /v1/scenarios`, which validates it again.
* **Provider and budget.** OpenAI Chat Completions (`gpt-4.1` unless
  `REVA_LLM_MODEL`), strict JSON-schema output, temperature 0, no tools; Gemini
  only after a retryable OpenAI failure. Each request is charged to the daily
  `llm.scenario_drafts` budget in `budgets.yaml` first; over budget is a 429,
  never a guess. Responses record the provider, the exact model that answered,
  the prompt version and the token usage. Temperature 0 does not make outputs
  bit-reproducible across provider versions; tests replay recorded responses.

Details, decisions and the live-call evidence: `docs/dev/v2_nlc.md`.
