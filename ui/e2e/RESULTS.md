# UI end-to-end results (CI item 10)

- **Date:** 2026-10-14, final run 23:56–23:58 UTC
- **Command:** `uv run python scripts/e2e_stack.py --run -- --reporter=list`
  (Chromium from `/opt/pw-browsers`, `@playwright/test` 1.56.1)
- **Stack:** a fresh database `reva_e2e_<hex>` (dropped afterwards), seeded registry, and offline captured
  fixtures for worldbank (200 observations), comtrade (282), naturalearth and crossref. The script publishes
  test_fixture release #1 (482 estimates), builds the UI into a temporary directory, then starts uvicorn
  with `create_app` and a generated Argon2id admin password plus a real `Worker` thread. Stack startup takes
  about 30–50 s.
- **Result: 25 tests, 21 passed, 4 failed** (1.4 min). The 4 failures are product gaps, not test faults.

## Coverage

| Spec | Checks | Status |
|---|---|---|
| auth.spec.ts | Unauthenticated navigation shows the sign-in form; data endpoints return 401 problem+json. A wrong password shows an error. A correct password signs in: the session cookie is HttpOnly and SameSite=Strict, `document.cookie` cannot read it, and localStorage and sessionStorage are empty. Sign-out ends the session. A forged POST without X-CSRF-Token, or with a wrong one, gets 403 on /v1/simulations, /v1/scenarios and DELETE /v1/session. With the real token the request reaches validation. | pass |
| screens.spec.ts | Each of the 8 screens shows modality, context, valid time and knowledge cutoff. The cutoff must be stated and must match the release date. A TEST FIXTURE label must be shown. Keyboard: skip link, all 7 nav links and the form controls up to "Create scenario and run"; Enter on a nav link; the skip link focuses `#main`. Reduced motion: under `emulateMedia({reducedMotion:'reduce'})` no transition or animation lasts more than 10 ms, and there is no smooth scroll. | 4 fail (see below); keyboard and reduced motion pass |
| country.spec.ts | DZA crude imports for 2020–2023 render as `missing (unavailable)` with `data-missing-reason`. They never show as 0, a blank or a dash, and the "shown as gaps, not zero" summary is present. The provenance drawer from FRA population "Trace" reaches estimate, observation (raw selector), acquisition, the raw artifact (SHA-256 matches the API `raw_sha256`, with a `Selector (selected from)` row containing `countryiso3code`) and the storage location. Following the artifact, Back and Escape all work. An unknown country degrades clearly. | pass |
| simulation.spec.ts | **L1** FRA/CHN/USA: the real worker job succeeds. The API confirms ΔM = 0.25·M for France. The table lists France, China and United States with no missing values. The context banner shows SIMULATION, the release and the cutoff. The screen banner is sticky after scrolling. The **CSV** export has comment lines for the SIMULATION banner, simulation id, scenario id, baseline release, cutoff, model version, fidelity and assumptions, with one row per result. The **JSON** export has the same context and matches the served values. **L2**: the caption and column read "sensitivity range (not a confidence interval)", and no confidence/credible/95% wording appears elsewhere. A sensitivity range has no coverage level, and the export labels it the same way. **DZA**: the run is refused with 422 `missing_input` naming DZA only; no job, no results. **L3**: not offered (only 2 fidelity radios); POST with fidelity L3 returns 422 problem+json. An unknown run id shows "Not found". The fixture baseline UUID is never posted. | pass |
| smoke.spec.ts | /health/live and /health/ready answer. The UI shell has CSP, X-Frame-Options DENY and nosniff headers. The map is marked display-only. No external network requests on World, Country or Graph. Unknown screens and endpoints render clearly (404 problem+json). | pass |

## Failures (cause → owner)

| Test | Cause | Owner |
|---|---|---|
| `/evidence` context + cutoff | The banner shows "Knowledge cutoff: not stated". `/v1/observations` and `/v1/evidence` return `knowledge_cutoff: null` and `valid_time: null`. | E2 (API envelope + UI banner) |
| `/graph` context + cutoff | Same: `/v1/relations` returns no `knowledge_cutoff`. | E2 |
| `/acquisition` context + cutoff | Same: `/v1/connectors` and `/v1/sources` (context `registry`) return no `knowledge_cutoff`. | E2 |
| `/models` context + cutoff + fixture label | `/v1/models` returns no `knowledge_cutoff` and has origin `none`, so there is no TEST FIXTURE label even though the instance only holds fixture data. | E2 |

Suggested fix (E2): for "as of now" reads, set `knowledge_cutoff` in every envelope to the time the query was
answered (the database statement timestamp). Registry/catalogue reads could carry the latest release cutoff
instead, so the banner never says "not stated". For the models screen, either show an instance-level
fixture-mode badge (for example when the latest release origin is `test_fixture`) or have the models
envelope report the origin of the evaluation data.

## Issues found during the session (since fixed upstream)

- GET `/v1/simulations/{id}` returned 500 for every L2 run. `SimResult.value` rejected the engine's
  `L2:sensitivity_range` row, whose value kind is `sensitivity_range`. This was fixed by 23:52; L2 now passes.
- Worker job `handler_unavailable`: `reva.simulation.jobs` could not import `JobCancelled` from
  `reva.operations.context` while that file was being edited (23:50). The UI showed it correctly as "Last
  error". It was fixed by 23:52.
- The shared runtime roles have a cluster-wide `CONNECTION LIMIT 10` used by every concurrent test session. On
  the first attempt `reva_worker` was exhausted. `e2e_stack.py` now retries a refused login with backoff and
  uses small pools (worker 4, API 3).

## Notes

- Banners inside the Country panels also show "Valid / reference time: not stated". The spec requires every
  result to show valid/reference time; for multi-year series a period range would be clearer. No test asserts
  this yet.
- Logins per run: 1 by the stack (written to the storageState), 2 in auth.spec.ts. The stack sets the login
  rate limit to 10 per minute; it is always enforced.
