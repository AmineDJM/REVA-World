# Scientific limits and unsupported capabilities

REVA V1 is a private, single-user MVP. It is a disciplined evidence and
estimate register with one narrow scenario model — not a world model. V2 adds
experimental models (maintainer review pending) and natural-language drafts;
their limits are in [V2 models and drafts](#v2-models-and-drafts-experimental).

## What the outputs are not

* **Not a forecast of the economy.** The crude-oil scenario is a one-period,
  fixed-quantity (L1) or assumed-elasticity (L2) border-value accounting stress
  test. It is not GDP loss, welfare change, inflation, fiscal revenue, an oil
  demand elasticity, a balance-of-payments forecast or a war/supply-shock model.
  The `exposure_percent_baseline_gdp` output is a ratio to same-year baseline
  GDP, not a change in GDP. A Brent headline price change is not automatically
  the scenario's multiplier.
* **Not causal.** No identified causal estimate exists in V1. Evidence cards
  describe studies (e.g. Kilian 2009); they do not supply coefficients to models.
* **Not reality.** A selected official statistic (`selected_report`) is what a
  source reported, with the source's own (usually unquantified) uncertainty.
* **Not a calibrated probability.** L2 results are published as a separate
  `L2:sensitivity_range` row (min/max over the assumed elasticity grid and the
  fixed-quantity point); it carries no confidence, credible or coverage level.
  The L1 value keeps its own unquantified uncertainty.
* **Not a real-time backtest.** Forecast evaluations use today's revised data
  split by year (`revised_data_retrospective`); REVA issued no forecasts at those
  origins. The `local_level_log` prediction interval is a plug-in interval that
  under-covers in short windows.
* **Tests do not prove truth.** Passing tests verify code against stated
  mathematics and contracts; they say nothing about whether the world behaves as
  modelled (invariant 35).

## Coverage limits and known data gaps

* Countries: the four V1 profiles (France, Algeria, China, United States) use reviewed aliases; every
  other country uses provider-declared aliases from the archived Comtrade/World Bank/UNSD lists
  (`registry/entities/country_crosswalk.json`). The crude scenario can run for `ALL` countries with
  `missing_input_policy: skip_missing_countries`: for 2023, 85 countries have complete inputs and 144
  are skipped with typed reasons (coverage in [dev/v2_allc.md](dev/v2_allc.md)). Composite provider
  codes (490 "Other Asia, nes", 899, EU 97, historical entities) are never mapped to a country.
  Reporters whose customs territory is wider than the country (France incl. Monaco, USA incl.
  territories, Switzerland incl. Liechtenstein, Norway incl. Svalbard/Jan Mayen) are flagged.
  HS editions H0–H3 are stored but not eligible for the crude concepts until a human reviews that
  mapping (this excludes, e.g., Iraq's 2023 exports reported under H2). A country reporting one flow
  but not the other is skipped, never zero-filled.
* **Algeria (DZA) crude trade is a missing input, never zero.** Comtrade's public
  preview returned **zero rows** for DZA HS 2709 world-total imports and exports
  for **2020–2023**; these cells are stored as typed `Missing(unavailable)` with
  `provider_returned_no_rows=true`. Any scenario for DZA in those years is refused
  as `missing_input` (the whole request, not just DZA). For 2024 the provider
  returns one row per flow, in classification H4 rather than H6, with
  `isReported=false` and `legacyEstimationFlag=6` (`provider_estimation_flagged`):
  imports 5,564 USD (about 9 t) and exports about 14.2 billion USD. These are
  provider-flagged rows; flag 6 coincides exactly with `isQtyEstimated` and
  `isNetWgtEstimated` (UNSD coding: quantity and net weight estimated), so it is
  the tonnage, not necessarily the USD value, that the provider estimated
  (docs/dev/numeric_audit.md). This is an endpoint gap, not evidence about
  Algeria's crude trade. The default live scenario uses FRA, CHN, USA for 2023.
* Comtrade (connector 1.1.0, one period per request): values keep the raw
  `isReported`/`isAggregate` flags; `provider_estimation_flagged` marks only rows
  the provider flags as estimated (`legacyEstimationFlag != 0`). Valuation is
  labelled per flow (exports FOB; an import is CIF or FOB only when
  `primaryValue` matches exactly one of them, otherwise "basis not determinable").
  Quantities/net weights are often unusable and are kept missing; no barrels are
  inferred.
* Valuation basis and source choice move the headline numbers. An independent
  recomputation from raw provider data reproduces the 2023 demo exactly
  (docs/dev/numeric_audit.md), but: the US import row also carries a FOB value,
  and using it gives 0.0433 % of GDP instead of 0.0497 % (CIF); France's Comtrade
  import value (31.5 bn USD) is about 7 % above the French energy ministry's
  (SDES) 2023 crude bill (27.3 bn EUR), which would give 0.241 % instead of
  0.258 %. REVA does not reconcile national sources with Comtrade.
* Population: WDI mid-year totals and Eurostat 1 January age × recorded-sex
  (France only, available only through the autonomy Eurostat template) are
  different concepts and are never reconciled as one quantity. The synthetic
  population (recipe-2) supports age × recorded-sex questions only; no
  attribute is imputed inside open, unknown or band age groups.
* Map geometry is Natural Earth 1:110m for display only; it settles no border or
  sovereignty question and may differ from statistical territories.
* Crossref provides DOI metadata only: not full text and not causal validation.
* Historical replay: V1 cannot reconstruct what was knowable in, say, 2015 from
  today's WDI responses. Only releases REVA itself published support
  `operational_as_known`; `available_then_reconstruction` uses only observations
  with independently evidenced availability; `revised_data_retrospective` results
  are labelled as not real-time backtests.

## Unsupported capabilities (typed capability gaps)

Requests for these return a structured `capability_gap` problem (the capability
catalogue in `src/reva/capabilities.py` describes them as
`capability_not_available` with status `reserved` or `blocked`):

* Fidelity L3 (Monte Carlo without justified parameter distributions) and L4.
* Later phases P8–P12 are **interface-only** and none is implemented: physical
  fields and external physical models (P8), multi-period dynamics, feedback
  coupling, input–output/SFC models, macroeconomic outputs and causal policy
  models (P9), household synthesis, privacy accounting and network compression
  (P10), external sandbox runner, parser repair and shadow/signed model promotion
  (P11), decision search and multi-actor reactions (P12). Typed interfaces exist in
  `reva.extensions` and `reva.models.physical` and refuse with structured gaps. A
  few pure verification helpers (analytic spherical field aggregation,
  double-entry balance check, BMI lifecycle checker) exist only to test those
  future gates; they do not provide the domain capability.
* Arbitrary generated code: autonomy is template-only (declarative manifests run
  through reviewed template code); no external sandbox runner is configured, so
  generated code is never executed.
* LLM use: only for natural-language scenario **drafts** (V2, below), and only when
  `REVA_LLM_ENABLED` is set; never used in numerical models, transformations,
  replay or tests (tests replay recorded responses). The autonomy extraction
  budget `llm.monthly_usd` stays 0.
* Value of information in money without a declared decision problem.

## V2 models and drafts (experimental)

Every V2 model is registered as a candidate with `review_status: maintainer_review_pending` and
labelled "experimental model, not yet reviewed". A scientific review of the V2 models is in
[dev/review_science_v2.md](dev/review_science_v2.md); the limits below include its findings.

### All-countries scenarios

* `countries: ["ALL"]` (crude border-value model) is resolved at run time to every country with data
  in the pinned release; with `missing_input_policy: fail` one missing country refuses the whole run,
  with `skip_missing_countries` the skipped countries are listed with typed reasons (2023: 85 complete,
  144 skipped). Coverage is what Comtrade and the World Bank publish, not the world.
* A scenario cannot **exclude** countries: there is no exclusion list. "All countries except China" must
  be written as an explicit list; a natural-language draft that asks for an exclusion is withheld
  (`needs_input`), never drafted as `ALL`.

### Natural-language drafts (`POST /v1/scenario-drafts`)

* Enabled only with `REVA_LLM_ENABLED`, a credential-injecting egress proxy and the two ledger
  functions; budget `llm.scenario_drafts`: 200 requests and 2,000,000 tokens per day, at most 1,500
  output tokens per request (over budget: 429, nothing sent). Provider: OpenAI `gpt-4.1`; the Gemini
  fallback has not answered a live call successfully (billing), so its success path is unverified.
* A draft is a **proposal**, not a result: nothing runs until the user submits the spec. Temperature 0
  does not make outputs reproducible across provider versions.
* A draft can be valid and still describe a different scenario from the one asked. The server
  restatement is generated from the checked spec and server texts only (LLM-written text is kept in
  `llm_restatement` and in labelled `unsupported` items), and **lexical** French/English cross-checks
  withhold a draft (`needs_input`) when the text excludes countries, names a region while the draft
  says `ALL`, leaves an input-output shock origin unstated (a world price is never defaulted to the
  reported country; a world-wide shock needs 50 origins and cannot be drafted), or states a retirement
  age level that the draft turned into a shift in months. They warn when the chosen industry is broader
  or different than the words used (`B` is all of mining and quarrying; `C19` is refined products, not
  crude oil; `D35` is electricity and gas supply) and when a named region cannot be checked against an
  explicit country list. These checks are pattern matches: paraphrases they do not know pass to the
  user's review of the restatement. A draft holds at most 10 items per list (for example 10 shocks).

### Population projection (`demography.cohort_component`)

* EU-27 and EFTA countries from Eurostat only; single ages to an open group; recorded sex only.
* Mortality is held at base-period rates (no improvement), so old-age populations tend to be
  understated over long horizons; fertility and migration are labelled assumptions (zero, average residual,
  EUROPOP2023 baseline), and the L2 grid has no probability level. A conditional projection, not a
  forecast.
* "Average residual" migration is a residual of the population balance: it contains statistical
  adjustments and **census revisions**. Residual years whose 1 January population carries Eurostat's
  series-break flag `b` are excluded from the average by default and reported in the run warning
  `migration_residual_contains_break` (they can be kept only by explicit acknowledgement, still
  warned); an unflagged revision is not detected by this rule (an outlier diagnostic only warns). A
  window needs at least two usable years unless a single year is allowed explicitly (warned). The
  review measured why: with the 2019-23 window, Poland's 2021 census break alone made the 2050
  projection about 4 million lower (25.8 instead of 29.7 million with the 2021-23 window).
* The back-test (2013 → 2023) uses today's revised vintages (labelled
  `revised_data_retrospective`, which can make errors look smaller) and takes the open age group's
  structure from the target year. On total population the reviewer found a constant-growth
  extrapolation as good or better in most countries; see [dev/v2_pop_backtest.md](dev/v2_pop_backtest.md).
* Migrants are added at the end of the year (not exposed to mortality in the arrival year, though
  they enter the next year's birth exposure); the age convention of EUROPOP net migration is assumed,
  not verified.

### Input-output prices (`economic.io_price`)

* **First-round cost-push, not inflation.** Fixed technical coefficients of one FIGARO year (no
  substitution), no demand, quantity, wage, exchange-rate or monetary response, one period. Under full
  pass-through (L1) the result is the largest-magnitude end of the pass-through range; the L2 range
  is an assumption grid with no probability level. Uncertainty is unquantified.
* **Basic prices, not a CPI.** The household basket is households' purchases at basic prices: trade
  and transport margins are in it, **taxes less subsidies on products are not** (France, FIGARO 2024:
  13.2 % of the basic-price basket), nor are direct purchases abroad; weights are fixed. Each basket row
  also carries a purchasers'-price reading with product taxes held fixed per unit: for +10 % on B, C19
  and D35 in every FIGARO economy (θ = 1) France gives 0.93 % at basic prices and 0.82 % with fixed
  per-unit taxes (euro-area-12 pooled: 0.95 % and 0.84 %). Ad valorem taxes would keep the basic-price
  figure; the real mix lies between. Neither number is a consumer price index.
* **Industries, not products.** Only NACE `B`, `C19` and `D35` can be shocked. `B` is all of mining and
  quarrying (coal, crude petroleum and natural gas, metal ores, quarrying): FIGARO has no crude-oil
  split, so an "oil price" shock on B also moves metal ores and quarrying, and the crude share of B
  differs by country. `C19` is refined products; its shock leaves crude unchanged.
* Coverage: the 49 FIGARO countries are reported; the rest of the world can be a shock origin but is
  not reported; other countries are refused (`capability_gap`). No external benchmark was reproduced:
  the published IO studies use other years, tables and shock definitions (the all-energy run is about
  2.6 times the ECB's 2005-table figure; see [dev/v2_io.md](dev/v2_io.md)).

### Retirement age (`social.retirement_age`)

* Rule tables were transcribed by an agent from archived legal text (every row cited; maintainer
  review pending) for France, Germany, the Netherlands and Spain. **Refused** (`capability_gap`): Italy,
  Belgium, Sweden and Poland (no archived, sourced rule table), and Spain is refused by its own data
  checks (contribution-track heterogeneity); every other country has no rule table.
* Results are labelled **qualified** for every country: early routes (long careers, disability,
  inaptitude) are not modelled, so the affected population is overstated where they apply. The
  pension-receipt proxy (base-year inactivity of the five-year age group) has **unknown direction**:
  it can overstate or understate at the affected single ages. The employment response is a cited
  literature share (verified values only), not an estimate for the country.
* Eligibility is modelled at single ages 55-69 to 2040; a request whose eligibility differs outside
  those ages is refused, never cut. Expenditure is approximate (average old-age pension × change in
  recipients, nominal, no indexation); eligibility counts from the month the age is reached while
  pensions usually start the month after (about one month of timing bias, largely cancelling in
  scenario differences). The 2010-reform back-test fits partly
  through offsetting errors; the reviewer could not verify its pre-registration (protocol and results
  first appear in the same commits).
* A target age (e.g. "65 years") is a level, not a shift: natural-language drafts never convert one
  into the other; they ask, or use the model's target-age intervention where it is registered.

#### Outside the EU (`social.retirement_age@1.1.0` and `@1.2.0`)

* Legal ages are the World Bank WBL 2026 full-benefit ages (agent transcription, maintainer review
  pending) held constant as a counterfactual reference; ages 55-64 only (ILOSTAT has no 65-69 band);
  the employment response is a European estimate transferred to the country; no back-test outside
  France. See [dev/v3_simworld.md](dev/v3_simworld.md).
* **1.1.0 assumes full pension coverage**: every person of an affected age is treated as bound by the
  legal age, so in informal economies the employment and pension-receipt changes are overstated
  (India, age 62 from the 1965 cohort, 2030: +10.6 M employed, −24.2 M recipients). Use **1.2.0**,
  which applies the change only to the pension-covered share. 1.1.0 stays available unchanged for
  existing specs.
* 1.2.0's coverage is ILOSTAT SDG 1.3.1 "persons above retirement age receiving a pension" — the
  source's own value of the latest year ≤ the reference year (stated per row, flagged when older),
  by sex when published, else the total for both sexes. It is **not** the share of today's 55-64
  year-olds bound by the contributory age: it counts non-contributory (social) pensions and is
  measured above pensionable age, and it is applied uniformly to every single age 55-64. Where a social
  pension with its own age drives coverage (e.g. Mexico's universal pension, 100 % coverage) results
  stay overstated; elsewhere the direction is unknown. No coverage sensitivity is invented. Countries
  without a coverage value within 10 years are refused (`missing_input`), never assumed covered.
  ILOSTAT publishes no contributor series, and the ILO World Social Protection Database was not
  reachable (HTTP 403), so contributors are not used.

## Governance limits (residual risk)

* **Governance approvals are enforced in the database, and the gate is a separate process.** Since
  migration `0007` every governance row records the writing login (`recorded_by = session_user`);
  human-attributed actors are accepted only from the API login, the migration owner or the approval
  functions; approved states (`eligible`, `approved` mappings and model versions, `reviewed` claims)
  are written only by the owner or by `reva_registry`-owned functions that require a passing gate
  result **recorded by the gate login `reva_gate`** or a human review recorded through the API, bound
  to the proposal's recorded scope and used once (`ops.approval_use`). The promotion gate runs as its
  own process (`reva gate` / `scripts/start_gate.py`; the `reva-gate` service on Render) with its own
  database login, the only one that may record gate results; the worker files `ops.gate_request` rows
  and waits (`REVA_GATE_WAIT_SECONDS`, default 120 s) for the decision, reporting `gate_pending` and
  promoting nothing if none arrives. Seeding runs as the owner during bootstrap.
* Residual risk of the gate separation: the gate evaluates what the worker stored (the proposal
  document, the report evidence and the captured fixture bank it re-runs the validator and sandbox
  on), so a compromised worker can still choose the inputs, though not the verdict. Both processes run
  the same image. In local development the gate runs on the same host/container as the worker,
  normally from the same `.env` that holds every runtime password, so the separation there is a
  credential convention, not an isolation boundary; on Render it is a separate service with only the
  gate's own credentials. Without `REVA_GATE_DB_PASSWORD` (allowed outside production) the gate login
  is not provisioned and nothing is auto-promoted. `model.record_promotion` (tournament promotions),
  `model.evaluation` rows and worker artifacts (`meta.artifact`) remain worker-writable. See
  [threat_model.md](threat_model.md) and [dev/gate_separation.md](dev/gate_separation.md).

## Operational limits

* One API process (one Uvicorn worker), one worker and one gate process; CPU-bound work runs on the
  queue. No horizontal scaling, no high availability. The login rate limiter is
  in-memory per API process (`REVA_LOGIN_RATE_LIMIT_PER_MINUTE`, default 5).
* Render filesystem is ephemeral; only the object store and PostgreSQL persist.
* Backups/restore are verified on small local data; production RPO/RTO must be
  re-measured in the first drill.
* Not deployed: deployment depends on external provisioning (bucket, scoped
  credentials, admin hash) and on confirming on the first deploy that Render's
  database owner has `CREATEROLE` and may create the PostGIS extension (bootstrap
  checks both and fails closed). See [deployment.md](deployment.md).
* Performance/cost figures are measurements on the development machine unless a
  document states the named Render plan.
