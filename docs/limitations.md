# Scientific limits and unsupported capabilities

REVA V1 is a private, single-user MVP. It is a disciplined evidence and
estimate register with one narrow scenario model — not a world model.

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

* Countries: France, Algeria, China, United States profiles.
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
* LLM use: disabled by default (budget 0); never used in numerical models,
  transformations, replay or tests.
* Value of information in money without a declared decision problem.

## Governance limits (residual risk)

* **Governance approvals are enforced in the database, but the gate is not independent.** Since
  migration `0007` every governance row records the writing login (`recorded_by = session_user`);
  human-attributed actors are accepted only from the API login, the migration owner or the approval
  functions; approved states (`eligible`, `approved` mappings and model versions, `reviewed` claims)
  are written only by the owner or by `reva_registry`-owned functions that require a passing gate
  result or a human review recorded through the API, bound to the proposal's recorded scope and used
  once (`ops.approval_use`). Seeding runs as the owner during bootstrap. Residual risk: the V1 gate
  still runs inside the worker, so the worker can record a passing gate result for its own proposal;
  `model.evaluation` rows and worker artifacts (`meta.artifact`) are still worker-writable. The real
  fix is a separate gate login and process. See [threat_model.md](threat_model.md).
* The autonomy gate runs in the same worker process with the same credentials as
  discovery; independence is enforced by re-derivation and DB checks on gate
  results, not by process separation.

## Operational limits

* One API process (one Uvicorn worker) and one worker; CPU-bound work runs on the
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
