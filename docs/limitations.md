# Scientific limits and unsupported capabilities

REVA V1 is a private, single-user MVP. It is a disciplined evidence and
estimate register with one narrow scenario model — not a world model.

## What the outputs are not

* **Not a forecast of the economy.** The crude-oil scenario is a one-period,
  fixed-quantity (L1) or assumed-elasticity (L2) border-value accounting identity.
  It is not GDP loss, welfare change, inflation, fiscal revenue, an oil demand
  elasticity, a balance-of-payments forecast or a war/supply-shock model. A Brent
  headline price change is not automatically the scenario's multiplier.
* **Not causal.** No identified causal estimate exists in V1. Evidence cards
  describe studies (e.g. Kilian 2009); they do not supply coefficients to models.
* **Not reality.** A selected official statistic (`selected_report`) is what a
  source reported, with the source's own (usually unquantified) uncertainty.
* **Not a calibrated probability.** L2 sensitivity ranges are assumption grids;
  they carry no probability level.
* **Tests do not prove truth.** Passing tests verify code against stated
  mathematics and contracts; they say nothing about whether the world behaves as
  modelled (invariant 35).

## Coverage limits

* Countries: France, Algeria, China, United States profiles. Comtrade's public
  preview returned **no 2023 HS 2709 rows for Algeria**; the all-four scenario
  returns `missing_input` for DZA. This is an endpoint gap, not evidence that
  Algeria has no crude trade. The default live scenario uses FRA, CHN, USA.
* Comtrade preview values are provider-derived aggregates (`isReported=false`);
  quantities/net weight are often unusable and are kept missing; no barrels are
  inferred.
* Population: WDI mid-year totals and Eurostat 1 January age × recorded-sex
  (France only) are different concepts and are never reconciled as one quantity.
  The synthetic population supports age × recorded-sex questions only.
* Map geometry is Natural Earth 1:110m for display only; it settles no border or
  sovereignty question and may differ from statistical territories.
* Historical replay: V1 cannot reconstruct what was knowable in, say, 2015 from
  today's WDI responses. Only vintages REVA itself recorded support
  `operational_as_known`; `revised_data_retrospective` results are labelled as not
  real-time backtests.

## Unsupported capabilities (typed `capability_not_available`)

* Fidelity L3 (Monte Carlo without justified parameter distributions) and L4.
* Multi-period dynamics, feedback coupling, input–output/SFC models, physical
  fields beyond the analytic verification adapters, household or network
  synthesis, multi-actor reactions and decision optimisation (interfaces exist in
  `reva.extensions`; they refuse with structured gaps).
* Arbitrary generated code: autonomy is template-only; there is no secure
  external sandbox configured, so generated code is never executed.
* LLM use: disabled by default (budget 0); never used in numerical models,
  transformations, replay or tests.
* Value of information in money without a declared decision problem.

## Operational limits

* One API process (one Uvicorn worker) and one worker; CPU-bound work runs on the
  queue. No horizontal scaling, no high availability.
* Render filesystem is ephemeral; only the object store and PostgreSQL persist.
* Backups/restore are verified on small local data; production RPO/RTO must be
  re-measured in the first drill.
* Not deployed: deployment depends on external provisioning (bucket, scoped
  credentials, admin hash) and on the role-provisioning fix described in
  [deployment.md](deployment.md).
* Performance/cost figures are measurements on the development machine unless a
  document states the named Render plan.
