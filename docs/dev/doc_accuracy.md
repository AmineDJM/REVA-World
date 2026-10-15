# Documentation accuracy review (2026-10-15)

Scope: `docs/methodology.md`, `docs/limitations.md`, `docs/api.md`,
`docs/scientific_validation.md`, `docs/operations.md`, `docs/threat_model.md`,
`README.md`, checked against the working tree. Rule applied: a doc never claims
more than the code does.

## Corrections made

| Doc | Previous claim | Correction | Code reference |
|---|---|---|---|
| methodology §1 | Policy `selection-1.0.0` | `selection-1.1.0` | `registry/policies/selection.yaml` |
| methodology §1 | Retraction and eligibility filter happen before revision selection | Only recorded_at ≤ cutoff and origin filter precede revision selection; a retracted head excludes the lineage (no fallback); an ineligible head falls back to the latest eligible revision flagged `superseded_revision_selected`; eligibility is scope-aware (subject_ids/years) | `src/reva/belief/selection.py` (`ELIGIBLE_SQL`), `migrations/versions/0002_evidence.py` (`evidence.is_eligible`) |
| methodology §1 | (missing) incompatible lineages | Candidates differing on `flowCode`/`partnerCode` withhold the estimand (`withheld_estimands`); estimands without a preference entry are not selected | `selection.py` (`DISTINGUISHING_FLAGS`, `select_report`) |
| methodology §1 | Cutoff source unstated | `cutoff=None` reads `clock_timestamp()` after the evidence snapshot lock and the publication lock; future explicit cutoffs rejected; `test_fixture` unpublishable in production | `src/reva/belief/publication.py`, `src/reva/kernel/context.py` |
| methodology §1 | `release_as_of` = "release published at that time" (vague) | Highest sequence with `published_at <= T` and `knowledge_cutoff <= T`, optional origin | `src/reva/belief/snapshots.py` |
| methodology §2 | L2 "grid of results is a sensitivity range" | L2 publishes the L1 row, one row per grid point and a separate `L2:sensitivity_range` row (min/max over grid ∪ {ε=0}) with uncertainty kind `sensitivity_range` and no coverage level | `src/reva/simulation/engine.py` (`compute`, `_range_uncertainty`) |
| methodology §2 | L3/L4 return `capability_not_available` | They raise `CapabilityGap` → problem code `capability_gap` (422 at the API, `unsupported` run outcome in the worker) | `simulation/engine.py`, `api/routes/sim.py`, `kernel/errors.py` |
| methodology §2 | L0 "exact cache" | Cache key = spec minus fidelity + baseline release + model manifest (incl. code digest); miss = recorded `cache_miss`, never computed | `engine.py` (`_lookup`, `cache_key`, `implementation_digest`) |
| methodology §2 | "all-four request returns missing_input for DZA" | Missing inputs refuse the whole request (API pre-flight 422; worker run with no result rows); DZA gap is 2020–2023 only; 2024 has one estimated H4 row per flow | `api/routes/sim.py` (`_preflight_inputs`), `simulation/compile.py`, `docs/dev/live_checks.md` |
| methodology §2 | Envelopes unstated | multiplier (0, 3]; ε_M ∈ [−2, 1]; ε_X ∈ [−1, 2] | `models/economic/crude_exposure.py` |
| methodology §3 | log_drift interval Gaussian | Student-t (T−1 df); unquantified when < 3 differences or zero variance | `models/forecasting/drift.py` |
| methodology §3 | local_level interval presented without caveat | Plug-in MLE interval, not calibrated in short windows | `models/forecasting/local_level.py` |
| methodology §3 | Promotion = block-bootstrap CI below zero, policy `promotion-1.0.0` | Decision = DM/HLN per horizon (origin-averaged) + Holm at α=0.025, ≥ 8 origins/horizon, subgroup credible-degradation → `needs_review`; bootstrap descriptive only; policy `promotion-1.1.0` | `models/tournament.py` (method 2.0.0), `registry/policies/promotion.yaml` |
| methodology §3 | Locked holdout "cannot be reused" | Last 2 years, max 1 access per model version, non-inferiority margin 0; evaluation service only runs `revised_data_retrospective`; promotion records an incumbent pointer, no forecasts are published | `evaluation/tournament_service.py` |
| methodology §3 | Replay modes listed only | Semantics of the three modes, no fallback, no LLM | `evaluation/replay.py` |
| methodology §4 | `reva-population-recipe-1`, leaves = single years only | `reva-population-recipe-2`: finest disjoint cover incl. source age bands, totals gate (`InconsistentSourceTotals`) | `population/recipe.py` |
| methodology §4 | "Observed zeros are structural zeros" | Published zeros are observed zeros; no structural zeros exist for age × recorded sex | `population/recipe.py` docstring |
| methodology §4 | Source path unstated | Eurostat `demo_pjan` only via the autonomy template, mapping scoped to FR | `registry/policies/promotion.yaml`, `adapters/connectors/registry.py` |
| limitations | Comtrade values "provider-derived aggregates (isReported=false)" | 1.1.0 keeps raw flags; `provider_estimation_flagged` = `legacyEstimationFlag != 0`; flow-aware CIF/FOB labelling; one period per request | `adapters/connectors/comtrade.py` |
| limitations | "no 2023 HS 2709 rows for Algeria" | Zero rows 2020–2023 (typed Missing, `provider_returned_no_rows`); 2024 estimated H4 rows, flag 6 | `docs/dev/live_checks.md` |
| limitations | Typed `capability_not_available` heading; P8–P12 described loosely | Problem code is `capability_gap`; P8–P12 listed as interface-only; verification helpers are not the capability | `capabilities.py`, `kernel/errors.py` |
| limitations | (missing) governance forgery residual | Added "Governance limits": worker can self-approve eligibility/mapping/model.version (and evaluations) | `migrations/versions/0007_security.py` grants, `cli/main.py` (`seed` uses worker engine), `cli/seed.py`, `autonomy/jobs.py`, `autonomy/models.py` |
| limitations | Deploy blocked by role-provisioning fix | Fix is in code (no privileged ALTER ROLE clauses; https rule API-only); first deploy must still confirm CREATEROLE/PostGIS | `cli/bootstrap.py`, `migrations/versions/0007_security.py` (`SQL_ROLES`), `settings.py` |
| api | SameSite unspecified; any username | SameSite=Strict, username `admin`, 8 h TTL, Secure in production or https URL | `api/auth.py`, `api/routes/system.py` |
| api | Rate limit unspecified | `REVA_LOGIN_RATE_LIMIT_PER_MINUTE` (default 5, 1–1000, fail-closed), global `max(60, 12×limit)`, 429 `rate_limited` | `api/auth.py` |
| api | Graph default depth 2 | Default depth 1, max 2, ≤ 500 edges | `api/routes/world.py` |
| api | Missing endpoints | Added `POST /v1/evaluations`, `GET /v1/discovery/candidates`, `/openapi.json`, scopes column | `api/routes/sim.py`, `api/routes/catalog.py`, `api/app.py` |
| api | Scenario POST idempotency | Idempotent by spec digest (201/200), no Idempotency-Key | `api/routes/sim.py` |
| scientific_validation | V2 "block bootstrap" as machinery | DM/HLN + Holm decision, bootstrap descriptive, calibration test cited | `models/tournament.py`, `tests/scientific/test_tournament_calibration.py` |
| scientific_validation | Physical verification listed as V0 evidence without caveat | Marked verification-only; P8 capability not implemented | `capabilities.py` |
| threat_model | No role/function table; no governance-forgery threat | Added runtime role table (grants + SECURITY DEFINER functions) and an open residual-risk row for worker self-approval; autonomy boundary wording (child process is not a security boundary; gate shares worker credentials) | `migrations/versions/0006_operations.py`, `0007_security.py`, `autonomy/gate.py`, `autonomy/sandbox.py` |
| README | Title only | Short scope statement with links; no claims beyond the docs above | — |

## Not edited (reported)

* `docs/operations.md` backup/restore section (owned by BKP): the sentence
  "Bootstrap after such a restore needs a role allowed to alter the runtime roles
  (see deployment.md, known blocker)" refers to a blocker that the code has fixed
  (`cli/bootstrap.py` now issues only `ALTER ROLE … LOGIN NOINHERIT CONNECTION
  LIMIT … PASSWORD …`; it requires `rolsuper OR rolcreaterole`). Suggest: "needs a
  role with CREATEROLE (bootstrap checks it)". The rest of operations.md (alerts,
  GC 7-day minimum age, kill switch, upgrades) matches the code.
* `docs/deployment.md` (agent I) "Known blocker" section is stale on both points
  (worker https rule now API-only in `settings.py`; role provisioning fixed in
  `cli/bootstrap.py` and `0007` `SQL_ROLES`).
* `src/reva/capabilities.py` `models.shadow_promotion` says "signed promotion and
  locked holdouts are not implemented", but `evaluation/tournament_service.py`
  implements a locked holdout; the reason string should say only shadow models and
  signed promotion are missing.
