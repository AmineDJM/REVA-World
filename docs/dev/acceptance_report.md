# REVA World — acceptance report

Generated 2026-10-15T00:24:23.549648+00:00 by `scripts/acceptance_report.py` (machine-readable: `var/acceptance/acceptance_report.json`, schema `reva-acceptance-report/1`). Git `ab91cae58603` on `claude/kind-darwin-kyy8re` with 19 uncommitted paths. This file is generated; do not edit by hand.

Status vocabulary: **passed** (all counted evidence passed), **failed**, **blocked** (could not run here: credentials, deployment, network, browser, or excluded from this run), **missing** (no automated evidence exists). A criterion is never reported passed without evidence.

**Not deployed.** no deployment was performed from this environment: Render API access, the managed PostgreSQL instance and the private S3 bucket credentials are not available here (see docs/deployment.md).

## Summary

| Ledger | passed | failed | blocked | missing |
|---|---|---|---|---|
| P0–P7 gate criteria | 43 | 4 | 2 | 2 |
| §13 mandatory acceptance tests | 37 | 0 | 1 | 1 |
| §17.4 MVP gates | 6 | 0 | 0 | 0 |

Phases: P0 **FAILED**, P1 passed, P2 passed, P3 **FAILED**, P4 passed, P5 _missing_, P6 passed, P7 _blocked_

pytest cases: 1454 passed, 4 failed, 5 skipped (groups: unit, contracts, property, scientific, replay, security, integration, e2e, perf; excluded markers: live). UI vitest: 47 passed, 0 failed.

Failing tests:

- `tests/integration/test_eurostat_discovery.py::test_end_to_end_promotion_ingestion_and_belief_separation`
- `tests/integration/test_eurostat_discovery.py::test_rollback_retires_eligibility`
- `tests/integration/test_evidence_fixes.py::test_population_from_promoted_eurostat_connector`
- `tests/unit/test_ontology_mappings.py::test_exact_mapping_is_identity_only_within_applicability`

## Demonstration workflow (§17.3)

- **fixtures** (`var/acceptance/demo_fixtures.json`): overall `pass`, {'pass': 13, 'fail': 0, 'blocked': 0}
- **live** (`var/acceptance/demo_live.json`): overall `pass`, {'pass': 13, 'fail': 0, 'blocked': 0}

| Step | Status | Title | Reason |
|---|---|---|---|
| 1 | pass | Fetch and archive real source responses |  |
| 2 | pass | Show source flags, units, classifications and release uncertainty |  |
| 3 | pass | Publish source-selected baseline beliefs |  |
| 4 | pass | Explore trade hyperedges (product, reporter, counterpart, interval) |  |
| 5 | pass | Apply a +25% border-price scenario (L1, FRA/CHN/USA) through the job path |  |
| 6 | pass | Deterministic exposure plus labelled elasticity sensitivity range (L2) |  |
| 7 | pass | Drill from a result to exact response fields and raw artifacts |  |
| 8 | pass | Replay a two-vintage recorded sequence; later revisions excluded |  |
| 9 | pass | Discover and validate the Eurostat connector; reject false equivalence |  |
| 10 | pass | Materialise/calibrate/coarsen/refine the public demographic recipe |  |
| 11 | pass | Naive-versus-state-space tournament on a labelled retrospective panel |  |
| 12 | pass | Unresolved knowledge gaps and a reviewable connector change proposal |  |
| N | pass | Negative demo: all four countries -> explicit Algerian data blocker |  |

## Phase gates (architecture §25)

### P0 — repository and constitution: **FAILED**

| Gate criterion | Status | Evidence | Reasons |
|---|---|---|---|
| Clean local install (lockfile consistent) | passed | cmd uv_lock_check |  |
| /health/live works | passed | pytest 1/1 |  |
| Unit tests pass | **FAILED** | group unit (461 cases) | group:group: failed |
| Lint passes (ruff + import-linter boundaries) | **FAILED** | cmd ruff; cmd import_linter | command:ruff: failed (['75 +         except Exception:', '76 \|             if attempt == 2:', '   \|', '', 'Found 7 errors.', '[*] 4 fixable with `--fix` (1 unsafe fix available with `--fix --unsafe-fixes`).']) |
| Package imports without network/secrets | passed | cmd import_without_secrets; pytest 1/1 |  |
| Published build status is honest | _missing_ | doc; manual | doc:docs/implementation_status.md: missing; manual:manual: missing (honesty of the published status is a human review item; no automated check) |

### P1 — kernel, ontology, provenance, storage, safe HTTP: passed

| Gate criterion | Status | Evidence | Reasons |
|---|---|---|---|
| Re-fetch idempotency | passed | pytest 2/2 |  |
| Correction appends (supersedes chain) | passed | pytest 2/2 |  |
| Earlier cutoff sees original | passed | pytest 3/3; demo[fixtures] step 8 |  |
| Missing/zero distinction | passed | pytest 4/4 |  |
| Failed upload invisible | passed | pytest 3/3 |  |
| Wrong unit rejected | passed | pytest 2/2 |  |
| SQL roles deny invalid writes | passed | pytest 4/4 |  |

### P2 — real source adapters, fixtures, durable queue: passed

| Gate criterion | Status | Evidence | Reasons |
|---|---|---|---|
| Fetch each source live once | passed | live worldbank.wdi.indicators; live un.comtrade.public_exact; live naturalearth.admin0_110m; live crossref.works.doi; pytest 0/1 |  |
| Offline fixture replay equivalent | passed | pytest 2/2; demo[fixtures] step 1 |  |
| Pagination complete | passed | pytest 3/3 |  |
| 429 / HTML / API-error handled | passed | pytest 4/4 |  |
| Comtrade missing weight remains missing | passed | pytest 1/1 |  |
| Crash/retry produces one visible acquisition | passed | pytest 2/2 |  |

### P3 — beliefs, aliases, n-ary graph, cards: **FAILED**

| Gate criterion | Status | Evidence | Reasons |
|---|---|---|---|
| Every displayed value traces to raw source | passed | pytest 1/1; demo[fixtures] step 7 |  |
| Graph preserves participant roles | passed | pytest 2/2; demo[fixtures] step 4 |  |
| World view is release-pinned | **FAILED** | pytest 2/2; demo evidence release_pinned_edges=0 | demo_evidence:4: failed (publish_release did not pin any world.relation rows to the release (world.relation.release_id); the release-pinned graph context is empty, so the edges shown come from the evidence context as known now (same origin)) |
| WDI/Eurostat incompatible support cannot reconcile | **FAILED** | pytest 2/3; demo[fixtures] step 9 | tests/integration/test_eurostat_discovery.py::test_end_to_end_promotion_ingestion_and_belief_separation: failed (AssertionError: {'status': 'blocked', 'reason': 'proposal backlog is full (20 open >= 20); no new proposals'} assert 'blocked' == 'promoted'      - promoted   + blocked) |
| No scenario input allowed into beliefs | passed | pytest 1/1; demo[fixtures] step 5 |  |

### P4 — scenario contract, crude exposure L1/L2, simulation queue: passed

| Gate criterion | Status | Evidence | Reasons |
|---|---|---|---|
| Hand-computed cases match | passed | pytest 2/2; demo[fixtures] step 5 |  |
| p=1 gives zero | passed | pytest 1/1 |  |
| epsilon=0 equals L1 | passed | pytest 2/2; demo[fixtures] step 6 |  |
| Missing input blocks | passed | pytest 2/2; demo[fixtures] step N |  |
| 5-year GDP request rejected | passed | pytest 1/1 |  |
| Scenario writes cannot change beliefs | passed | pytest 2/2 |  |
| Cancellation/checkpoint/retry verified | passed | pytest 4/4 |  |

### P5 — replay, forecasting, scorecards, UI: _missing_

| Gate criterion | Status | Evidence | Reasons |
|---|---|---|---|
| Future revisions/features/weights blocked | passed | pytest 3/3 |  |
| Revised-data mode visibly labelled | passed | pytest 3/3; demo[fixtures] step 11 |  |
| Uncertainty types correct | passed | pytest 3/3; vitest |  |
| UI drill-through works | _missing_ | pytest 1/1; demo[fixtures] step 7; manual | manual:manual: missing (no UI-level test opens the provenance drawer and follows it to the raw artifact (ui/src has no provenance drawer test; the Playwright smoke does not exercise drill-through)) |
| No-result/missing/auth flows tested | passed | vitest; vitest; vitest; pytest 1/1 |  |
| Naive baseline always shown | passed | pytest 2/2; demo[fixtures] step 11 |  |

### P6 — autonomy, Eurostat, demographic recipe, gaps: passed

| Gate criterion | Status | Evidence | Reasons |
|---|---|---|---|
| Discover demo_pjan from official catalogue | passed | pytest 1/1; demo[fixtures] step 9 |  |
| Validate live sample | passed | live eurostat_demo_pjan; pytest 0/2 |  |
| Semantic mismatch rejected | passed | pytest 2/2; demo[fixtures] step 9 |  |
| 100k weighted records preserve leaf totals | passed | pytest 2/2; perf population_100k_wall_s=3.379; perf population_100k_peak_rss_mib=181.1; demo[fixtures] step 10 |  |
| Coarsen/refine preserves mass | passed | pytest 2/2; demo[fixtures] step 10 |  |
| Generated code cannot run in worker | passed | pytest 2/2 |  |
| Hostile document cannot alter policy | passed | pytest 2/2 |  |

### P7 — image, Render config, monitoring, backup, release: _blocked_

| Gate criterion | Status | Evidence | Reasons |
|---|---|---|---|
| Current schema/CLI validation | passed | cmd validate_contracts; cmd validate_blueprint; group contracts (93 cases) |  |
| Deploy only when access exists | _blocked_ | manual | manual:manual: blocked (no deployment was performed from this environment: Render API access, the managed PostgreSQL instance and the private S3 bucket credentials are not available here (see docs/deployment.md)) |
| Actual roles and credentials verified | _blocked_ | pytest 3/3; manual | manual:manual: blocked (production role/credential verification needs the deployed database: no deployment was performed from this environment: Render API access, the managed PostgreSQL instance and the private S3 bucket credentials are not available here (see docs/deployment.md)) |
| Restore from backups | passed | pytest 1/1 |  |
| No filesystem persistence assumptions | passed | pytest 2/2 |  |
| p95/RSS measurements recorded | passed | perf ordinary_api_p95_ms=171.914; perf cached_metadata_p95_ms=37.336; perf l1_four_country_ms=12.113; perf queue_ack_p95_ms=241.265; perf population_100k_peak_rss_mib=181.1; doc |  |
| Complete live demo | passed | demo[live] step * |  |

## Mandatory acceptance tests (implementation prompt §13)

| Section | Test | Status | Evidence | Reasons |
|---|---|---|---|---|
| Scientific/semantic | M=100, X=40, G=1000, multipliers 1.25 -> dM=25, dX=10, dnet=15, 1.5% exposure | passed | pytest 1/1 |  |
| Scientific/semantic | All multipliers 1 give zero | passed | pytest 1/1 |  |
| Scientific/semantic | Zero elasticities exactly recover the fixed-quantity calculation | passed | pytest 2/2 |  |
| Scientific/semantic | Quantity/valuation missingness blocks unsupported conversions | passed | pytest 5/5 |  |
| Scientific/semantic | Countries with different reference years or GDP bases cannot be silently combined | passed | pytest 4/4 |  |
| Scientific/semantic | January and mid-year population are not identical concepts | passed | pytest 3/3; demo[fixtures] step 9 |  |
| Scientific/semantic | Probability, credible/prediction intervals, bounds and sensitivity types remain distinct | passed | pytest 5/5; demo[fixtures] step 6 |  |
| Temporal/storage | Two revisions yield different as-of answers at appropriate cutoffs | passed | pytest 2/2; demo[fixtures] step 8 |  |
| Temporal/storage | A->B->A creates three revisions; identical consecutive polls do not | passed | pytest 2/2 |  |
| Temporal/storage | Replayed old raw data with new parser versions is distinguished from an original vintage | passed | pytest 2/2 |  |
| Temporal/storage | Staged/uncommitted objects are invisible | passed | pytest 3/3 |  |
| Temporal/storage | Missing source publication time is never filled with the statistical reference date | passed | pytest 1/1 |  |
| Temporal/storage | Scientific UPDATE/DELETE fails under runtime roles | passed | pytest 2/2 |  |
| Isolation/security | Simulation role cannot insert evidence or beliefs | passed | pytest 2/2 |  |
| Isolation/security | Generated code cannot get production credentials or network by default | passed | pytest 3/3 |  |
| Isolation/security | Prompt-injection text in a paper cannot change a policy/allowlist | passed | pytest 2/2 |  |
| Isolation/security | SSRF | passed | pytest 3/3 |  |
| Isolation/security | Redirect to private IP | passed | pytest 1/1 |  |
| Isolation/security | Zip bomb | passed | pytest 4/4 |  |
| Isolation/security | SQL injection | passed | pytest 3/3 |  |
| Isolation/security | Secret-log | passed | pytest 3/3 |  |
| Isolation/security | App startup environment contains no migration owner password/URL | passed | pytest 4/4 |  |
| Reliability | Kill worker after object upload and after DB commit; retry yields one visible result | passed | pytest 2/2 |  |
| Reliability | Expired lease/fencing holder cannot publish | passed | pytest 3/3 |  |
| Reliability | Two workers claiming a due job do not duplicate visible effects | passed | pytest 2/2 |  |
| Reliability | Rate limiting, HTML error page with HTTP 200 and schema drift lead to retries/quarantine | passed | pytest 5/5 |  |
| Reliability | Restore reconstructs identical logical manifests and as-of results | passed | pytest 1/1 |  |
| Population/compression | All leaf-cell totals conserved; no double-counted source totals | passed | pytest 3/3; demo[fixtures] step 10 |  |
| Population/compression | Structural-zero and unknown-category handling | passed | pytest 4/4 |  |
| Population/compression | Same seed/config produces same logical cohort | passed | pytest 2/2 |  |
| Population/compression | Coarsen/refine preserves total weights and reports unsupported new detail | passed | pytest 3/3; demo[fixtures] step 10 |  |
| Evaluation/UI | Six future-information leakage cases blocked | passed | pytest 1/1 |  |
| Evaluation/UI | Naive baseline present; insufficient evidence does not promote challenger | passed | pytest 3/3; demo[fixtures] step 11 |  |
| Evaluation/UI | Provenance drawer reaches source bytes/selectors | _missing_ | pytest 1/1; demo[fixtures] step 7; manual | manual:manual: missing (the UI provenance drawer itself has no automated test (API + demo evidence only)) |
| Evaluation/UI | Every simulation screen and export has scenario context | passed | vitest; vitest; vitest; pytest 1/1; playwright; playwright |  |
| Evaluation/UI | Negative/degraded states render clearly; no stub success | passed | vitest; vitest; vitest; pytest 2/2; demo[fixtures] step N |  |
| Performance | Measure on the named target resources | _blocked_ | perf ordinary_api_p95_ms@0.5cpu=268.076; perf population_100k_wall_s=3.379; manual | manual:manual: blocked (measurements ran on this host with emulated CPU quotas, not on the named Render instance types: no deployment was performed from this environment: Render API access, the managed PostgreSQL instance and the private S3 bucket credentials are not available here (see docs/deployment.md)) |
| Performance | 100k-record memory/time budget and API/L1 targets validated | passed | perf population_100k_wall_s=3.379; perf population_100k_peak_rss_mib=181.1; perf ordinary_api_p95_ms=171.914; perf cached_metadata_p95_ms=37.336; perf l1_four_country_ms=12.113; perf queue_ack_p95_ms=241.265; pytest 4/4 |  |
| Performance | Actual values reported; no thresholds weakened without an ADR | passed | doc; perf l1_four_country_ms=12.113 |  |

## MVP gates (architecture §17.4)

| Gate | Status | Evidence | Reasons |
|---|---|---|---|
| Real live acquisitions for the live acceptance run; deterministic CI uses captured fixtures | passed | live live_release_published; live no_fixture_origin_in_db; demo[fixtures] step 1; demo[live] step 1 |  |
| Missing source/credential/rate limit is a visible blocked/degraded gate, never fake success | passed | pytest 3/3 |  |
| All four country profiles exist even if one scenario input is unavailable | passed | pytest 2/2; demo[fixtures] step N |  |
| No scenario number can be retrieved through a canonical observation endpoint | passed | pytest 1/1; demo[fixtures] step 5 |  |
| No synthetic record is linked to a real individual | passed | pytest 2/2; demo[fixtures] step 10 |  |
| MVP operational only after complete live demonstration and restore test pass | passed | demo[live] step *; pytest 1/1 |  |

## Commands

- `uv_lock_check`: exit 0 (0.04 s, log `var/acceptance/logs/cmd_uv_lock_check.log`)
- `ruff`: exit 1 (0.05 s, log `var/acceptance/logs/cmd_ruff.log`)
- `import_linter`: exit 0 (0.24 s, log `var/acceptance/logs/cmd_import_linter.log`)
- `import_without_secrets`: exit 0 (2.24 s, log `var/acceptance/logs/cmd_import_without_secrets.log`)
- `validate_contracts`: exit 0 (0.45 s, log `var/acceptance/logs/cmd_validate_contracts.log`)
- `validate_blueprint`: exit 0 (0.18 s, log `var/acceptance/logs/cmd_validate_blueprint.log`)

## Recorded artefacts and reports

- Live source checks: `var/live/live_checks.json` (finished 2026-10-14T23:34:30.016089+00:00, git 176751ff26f4)
- Performance results: `var/perf/perf_results.json` (generated 2026-10-14T23:31:25+00:00)
- [review_core](review_core.md)
- [spec_audit](spec_audit.md)
- [perf_report](perf_report.md)
- [live_checks](live_checks.md)
- [review_api_sim_ops](review_api_sim_ops.md)
- [review_science](review_science.md)
- [test_triage](test_triage.md)

## Reproduce

```
uv run python scripts/demo_workflow.py              # §17.3 demo -> var/acceptance/demo_fixtures.json
uv run python scripts/demo_workflow.py --live       # live demo -> var/acceptance/demo_live.json
uv run python scripts/acceptance_report.py          # this report (add --with-perf --with-live)
```
