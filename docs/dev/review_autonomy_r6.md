# Review R6: autonomy re-verification after the R4 fixes (adversarial, read-only)

**Scope:**
- `src/reva/autonomy/**`
- `src/reva/adapters/connectors/eurostat_template.py`
- `registry/policies/promotion.yaml`
- the DB approval path the autonomy code now calls: `evidence.approve_eligibility`, `ops.consume_approval` and `model.approve_version` in `0007_security.py`. This path belongs to GOV.

The R4 deploy and UI items were re-checked briefly.

**Tree state.** The review was done against HEAD `9c451fe`…`6bb5389`, with the working tree in motion. GOV and the budget work landed while the review ran: `jobs.py` and `models.py` now go through the `approve_*` functions, and `budget.py` uses `reserve_egress`. Every result below comes from a final re-run against `6bb5389` plus the working tree at that time. No source, migration or existing test was edited. The dev DB `reva` was not touched.

## Method

Scratch tests live in `/tmp/claude-0/-home-user-REVA-World/a82f628c-2790-54df-b0b4-28930bdc7a97/scratchpad/review6/`:
- DB tests use the `tests/conftest.py` `db` fixture, which creates a throwaway `reva_test_*` database.
- No test uses the network. The "live" client is a fake that serves the captured Eurostat bytes.

| File | What it attacks |
|---|---|
| `test_r6_gate_db.py` | R4-01 re-attack, uncovered manifest fields, provider unit/dimension drift, forged reports, gate trusting the caller's proposal object, model approval without a human, repair-pause resume, confusable version |
| `test_r6_promote_db.py` | gate-pass replay into `promote_connector`, rollback-hold bypasses, fixture mode under `reva_env=production` |
| `test_r6_budget_db.py` | discovery egress accounting with a counting fake provider |
| `test_r6_nodb.py` | template-pin coverage, name collisions, trailing-newline identifiers, kill file dropped by the entrypoint, sandbox filesystem reach, R4-09/11/14 |
| `test_r6_repair_nodb.py` | whether a repair `MappingRevision` can change what history means and still pass the replay check |
| `r6_helpers.py`, `conftest.py` | helpers; `patch_years_shape` works around R6-01 so the logic behind it can be exercised |
| `out_*.txt` | raw output of the final run (27 tests, all reproduce the stated result) |

To run a file:
```
uv run pytest <file> -p no:cacheprovider -c pyproject.toml --rootdir /home/user/REVA-World -s -o addopts=""
```

**Labels:**
- **VERIFIED**: reproduced by a scratch test.
- **READING**: established by reading the code only.

**Severity** uses the R4 scale:
- **High**: breaks a §24.1 invariant or a privilege boundary on a normal path.
- **Medium**: needs a specific sequence, or is a scientific defect with limited reach.
- **Low**: hardening.

## Summary

- **R4 findings (18):**
  - 9 closed;
  - 8 partially closed, each with a residual tracked below;
  - 1 still open: R4-05.
- **New findings (16):**
  - 4 High;
  - 4 Medium;
  - 2 Medium-Low;
  - 6 Low.
  - All 16 are VERIFIED.

The R4-01 fix holds for what it binds. The semantics, source, lineage, name, subject, key layout, resolution dimensions and totals are now re-derived. A tampered manifest is rejected even when its reports are forged to pass (`test_r4_01_binding_rejects_even_with_passing_reports`).

What remains open is binding at the edges:
- the gate decision is not bound to the artifact that gets promoted;
- the gate does not read the persisted proposal;
- provider-chosen structure (unit code, extra dimensions) is not bound to policy;
- several manifest fields are still free.

As a result, a T1 auto-promotion can still publish values whose meaning differs from the registry concept (R6-02, R6-04).

### R4 status

| R4 | Status | Evidence / residual |
|---|---|---|
| R4-01 gate binding | **Partial** | Tampered semantics, source, lineage, name, built-in squat and key layout are rejected even with passing reports (VERIFIED). Residuals: R6-02, R6-03, R6-04, R6-06, R6-12. |
| R4-02 worker env | Closed | `render_env_check.py` gives api, worker and bootstrap all OK (VERIFIED). |
| R4-03 stale spec / versions | Closed | Content-derived versions 1.0.0 → 1.0.1 → 1.0.2 and the `template_changed` state work (VERIFIED in `test_b`). Residual: R6-05. |
| R4-04 rollback re-promotion | **Partial** | Rediscovery after a rollback reports `rolled_back_hold` (VERIFIED). The hold is bypassable: R6-05. |
| R4-05 model registry trusts decisions | **Open** | A model is `approved` with no human review, both by object swap and by a direct `ops.record_gate_result` (VERIFIED). See R6-03. |
| R4-06 discovery request ignored | Closed | API 422s plus the handler's `PAYLOAD_KEYS` check (READING). |
| R4-07 UI rounding carry | Closed | `format.test.ts` 6/6 passed, including 0.9999999 → 1 (VERIFIED). |
| R4-08 budgets | **Partial** | Ledger, limit 0 refused, and a shared egress lock (READING + test). Under-charging remains: R6-09. |
| R4-09 format-string key | Closed | Strict placeholder language; `{geo.__class__}` is refused (VERIFIED). |
| R4-10 sandbox | **Partial** | Network and pid namespaces, a `_socket` guard, a measured `network_mode`, and environ not visible under unshare (VERIFIED). The filesystem is still reachable: R6-13. |
| R4-11 pin / normalisation | **Partial** | `template_sha256` is pinned and the `./`, `..` and case variants derive T3 (VERIFIED). Residuals: R6-07 and R6-16. |
| R4-12 backup | **Partial** | The dump is streamed into `age`, so no plaintext touches disk (READING). Archives are still unauthenticated and restored as owner. |
| R4-13 kill switch snapshot | **Partial** | A live re-read now happens on every check and inside the promotion transaction. In deployment it cannot be triggered: R6-10. |
| R4-14 untyped parser errors | Closed | A malformed index raises `SourceResponseError(schema_drift)` (VERIFIED). New untyped escape: R6-01. |
| R4-15 `preexec_fn` | Closed | Removed; the child sets its own rlimits and the parent applies `prlimit` (READING). |
| R4-16 CSV injection | Closed | `PLAIN_NUMBER` guard and `headerValue` newline strip (READING). |
| R4-17 labelling | **Partial** | `needs_review` now has its own DB value. `ops.proposal.permission_tier` is still the declared tier: R6-03 records T1 for a T0 decision. The UI was not re-checked. |
| R4-18 CI secret scope | Closed | The secret is now set only in the env of the live steps (READING). |

---

## High

### R6-01: T1 promotion fails at the DB, and the failure is untyped (VERIFIED)

**Cause.** `promote_connector` and `connector_promotion_scope` write `applicability.years` as a list (`[2023]`). PUBFIX's `evidence.check_eligibility_applicability` only accepts `{"in": [...]}` or `{"from", "to"}`. Every promotion therefore raises `psycopg InvalidParameterValue`.

**Effects:**
- 8 existing autonomy and repair tests fail, including the end-to-end promotion, rollback and R4-03/04 tests.
- The error is a SQLAlchemy `DataError`, not a `RevaError`. It escapes `handle_discovery`, so the job crashes and is retried.
  - Each retry is a new attempt with a new reservation (`job_key` includes the attempt), so live discovery re-fetches every time.
  - Each attempt leaves another *unconsumed* passing gate row. Those rows are the input that R6-02 exploits.

**Fix:**
- *Agent G:* emit `{"in": years}` in both `promote_connector` and `connector_promotion_scope`.
- *Agent G:* catch `sqlalchemy.exc.DBAPIError` in `handle_discovery` and return a typed `failed` result that is not retried.
- *Lead:* add an end-to-end test to CI, since this regression landed silently.

### R6-02: A gate pass can be spent on a different manifest (VERIFIED, `test_a_gate_pass_replayed_for_another_dataset`)

**Code involved:**
- `promote_connector(engine, io, ctx, manifest, mapping, decision, policy)` takes `manifest` and `mapping` from the caller.
- `_verify_gate_row` only checks that the row is a `pass` for `decision.proposal_id`.
- The defence-in-depth `binding_problems` does not check any of the following; only the gate's `_t1_scope` does:
  - the approved entry, meaning the dataset ∈ `approved_exact_mappings`;
  - the geography scope;
  - the rights.
- GOV's `evidence.approve_eligibility` binds the pass to the proposal type, concept, `subject_ids` and years in the *proposer-written* `summary.scope`, and to one target `(connector_version_id, concept)`. It never binds the pass to the connector spec or manifest that the gate evaluated.

**Attack.** A genuine `demo_pjan`/FR pass was used to promote `eurostat.demo_mlexpec.fr`:
- the connector is Eurostat *life expectancy* (unit code `YR`);
- it was promoted as `demography.population.jan1_age_sex` with `unit.persons`;
- `binding_problems` returned `[]`, so it became `active` and is loaded for ingest;
- the genuine connector was then refused with "approval … already approved another eligibility".

**Fix:**
- *Agent G:*
  - `promote_connector` takes no manifest. It loads it from `ops.proposal.manifest_sha256` → proposal document → `patch_or_manifest` → `change.manifest` of the gate row's proposal, and the mapping from `change.mapping`.
  - Move the approved-entry, dataset, geo-scope and rights checks into `binding_problems`, so the gate and promotion share them.
- *GOV/lead:*
  - `summary.scope` records `manifest_sha256`, and `approve_eligibility` requires the connector version's spec manifest digest to match it.
  - The gate stores, in its evidence, the derived scope it checked. `approve_eligibility` compares against that scope rather than the proposer's `summary`.

### R6-03: The gate evaluates the caller's proposal object, not the persisted row, so models are approved without a human (VERIFIED)

**Code involved.** `PolicyPromotionGate.decide(proposal, …)` derives the tier from `proposal.changed_paths`, `requested_capabilities` and `patch_or_manifest`, all taken from the object. It records the result against `proposal.proposal_id`. `_record` only checks that this id exists.

**Evidence:**
- `test_gate_trusts_caller_proposal_object_not_persisted_row`: a persisted T1 `connector_promotion` proposal, given to the gate as a T0 `report` object with the same id and no reports, gets a recorded `pass`. The row still shows `permission_tier = 1`.
- `test_model_registration_without_human_review`: a `model_registration` proposal (`new_model`, T2) is registered through `PostgresModelRegistry.register` and the current `model.approve_version`. The result is `approval_status = approved` both ways:
  - object swap → `approved`;
  - the worker calling `ops.record_gate_result(…,'pass',…,'gate-evaluator:x')` directly → `approved`.

  `ops.consume_approval` accepts any gate pass for `model_registration`, but a legitimate T2 proposal can never receive a gate pass. So R4-05 is still open.

**Fix:**
- *Agent G:* `decide(proposal_id, …)` loads the proposal through `ProposalStore.load`. It refuses an object whose `proposal_json` digest differs from `ops.proposal.manifest_sha256`.
- *GOV:*
  - In `consume_approval`, `model_registration` (and any kind whose minimum tier is ≥ 2) requires an approving `proposal_review`. Gate passes are not accepted.
  - Revoke `ops.record_gate_result` from `reva_worker`, or make the gate a separate DB role. Otherwise any worker code can mint passes.

### R6-04: Provider metadata chooses semantics that T1 auto-promotes (VERIFIED)

**Cause.** The generator copies the provider's unit code (`semantics.unit_category`) and the dimension set from the inspection response. The binding derives `resolution_dimensions` from the manifest's own `dimension_order`. Neither the policy entry nor the registry pins:
- the provider unit code;
- the dimensions;
- the category vocabulary.

**Attack (a), `test_provider_unit_change_is_auto_promoted_with_registry_unit`.**
1. Eurostat starts serving demo_pjan in thousands: `unit: THS`, with values divided by 1000.
2. The validator, sandbox and gate all pass.
3. The France total is published as `{"value": "68277.21", "unit_ref": "unit.persons", "scale_factor": "1"}`, which is 1000× low.

Once the old version fails ingest on `unit_changed`, rediscovery regenerates the manifest and promotes it as a new version.

**Attack (b), `test_provider_extra_dimension_subpopulation_auto_promotes`.**
1. A `citizen` dimension pinned to `NAT` is added.
2. The gate promotes with `resolution = [age, sex, citizen]` and universe `usually_resident_population_eurostat`.
3. A nationals-only subpopulation is published as the registry's usually-resident population.

The same mechanism should let overlapping age aggregates through: they would be typed `leaf`, and the additivity mismatch is only a warning. That case is not tested.

**Fix** (agent G; the policy edit is T3/maintainer):
- `approved_exact_mappings` entries pin:
  - `provider_unit_codes: [NR]`;
  - `dimension_order: [freq, unit, age, sex, geo, time]`;
  - a per-dimension category vocabulary or pattern (`age: ^(TOTAL|Y_LT1|Y\d{1,2}|Y_OPEN|UNK)$`);
  - the total codes.
- `binding_problems` and the mapper's `_structure_findings` require equality with those pins.
- The validator contract comes from the policy entry.
- Make additivity failures blockers for T1.

---

## Medium

### R6-05: The rollback hold is bypassable (VERIFIED, `test_b_rollback_hold_bypasses`)

The hold applies only when the *generator's* `(name, version)` already has a retired decision. Two ways around it:

1. **The version is proposer-chosen and never bound.**
   - Identical retired content with version `1.0.1` passed the gate, was promoted, and became `active`.
   - The confusable `"1.0.0\n"` also passes the gate (`test_confusable_version_passes_gate`).
2. **Any template re-pin re-enables rolled-back content.** `resolve_connector_version` matches content *and* `code_digest`. After a maintainer re-pins the template for an unrelated edit, retired content gets a fresh version (`1.0.2`) and is auto-promoted with no review.

**Fix (agent G):**
- Hold on `content_key` (the manifest without its version) across all versions and digests: any retired version with the same content means `rolled_back_hold` until a human reapproves.
- The gate recomputes the expected version with `resolve_connector_version` and requires equality.

### R6-06: "Independent" validator and sandbox reports can be forged by the proposer (VERIFIED, `test_forged_validator_and_sandbox_reports_promote`)

**Code involved.** `_verified_reports` only checks that a report object agrees with the artifact it points to. Any holder of the `ArtifactStore` can write that artifact. The data-contract digest the gate expects is computable by anyone.

**Evidence.** Two hand-written passing evidence documents promoted a manifest that the real validator had blocked (`no_sample`).

The gate's docstring states this limit honestly, but the binding fix rests on these reports for data-level checks: values, flags, additivity.

**Fix:**
- The gate re-runs the validator and the replay sandbox itself, from the persisted proposal and the fixture bank. Both are cheap and deterministic.
- Alternatively, reports are DB rows written by a separate evaluator role and signed with a key the worker does not hold.

### R6-07: The template pin covers one file only (VERIFIED, `test_template_digest_ignores_imported_helpers`)

**Code involved.** `template_digest()` is `sha256(inspect.getsource(eurostat_template))`. The template's behaviour also depends on code and data outside that file:
- `base.json_decimal`, `plain`, `to_raw`;
- `safe_client.require_json`;
- `kernel.support`, `kernel.time`, `kernel.uncertainty`;
- `contracts/connector.schema.json`, which `validate_manifest` uses.

**Evidence.** Changing `json_decimal` to multiply values by 1000 left the digest equal to the policy pin, and the France total became 68,277,210,000.

In addition, `inspect.getsource` hashes the on-disk source through `linecache`, not the code that is actually loaded.

The tier map does mark these paths T3. The weakness is for *merged* changes: R4-11's "any template edit disables T1 until re-pinned" does not hold for them.

**Fix (agent G):** pin a digest over the sorted bytes of the transitive set of reviewed modules and the schema file. The set is enumerated explicitly in the template, and the digest is computed from `module.__file__` bytes at import.

### R6-08: A repair publication pause can be lifted by any unrelated gate pass (VERIFIED, `test_publication_pause_resumed_with_unrelated_gate_pass`)

**Code involved.** `resume_belief_publication(actor="gate-evaluator:…", gate_result_id=…)` accepts any `pass` by that evaluator.

**Evidence.** A T0 `report` proposal's automatic pass lifted a `unit_change` pause. The pause marker stores `proposal_id`, but it is never compared.

**Fix (HOOK/agent G):** require `gate_result.proposal_id` to equal the marker's `proposal_id`, a `connector_repair:*` proposal type, and a pass recorded after the pause.

---

## Medium-Low

### R6-09: Discovery under-charges its egress (VERIFIED, `test_validator_live_sample_is_not_charged`)

**Code involved.** In live mode, the validator gets `http.client`, the unwrapped client. It wraps that client in its own `CapturingHttp`, but `settle_discovery` counts only `http_box["http"]`. The discoverer and the validator each enforce `max_requests` separately, so a run can make 5 + 1 requests against a reservation of 5.

**Evidence.** The fake provider saw 3 GETs (1.99 MB), while the ledger settled `requests = 2`.

**Fix (agent G):** `live_factory` returns the same metered `CapturingHttp`, and one shared counter caps the whole run at the reservation.

### R6-10: The runtime kill switch cannot be triggered in deployment (VERIFIED, `test_kill_file_is_dropped_by_worker_entrypoint_env`)

**Code involved:**
- `scripts/_runtime_env.build_child_env` removes `REVA_AUTONOMY_KILL_FILE`, because it is neither a `Settings` field nor in `EXTRA_REVA_CONFIG`. The setting is not in `render.yaml` either.
- `live_kill_reason` re-reads `os.environ`, but nothing can change a running process's environment.

So stopping autonomy still needs a redeploy (R4-13). The API's discovery gate also reads only the settings snapshot.

**Fix:**
- *Agent I/lead:* add `reva_autonomy_kill_file` to `Settings` and set it in the Blueprint.
- *Agent G + lead:* better, add a DB flag (`ops.autonomy_control`, owner-writable, worker-readable). The guard reads it on every check and inside the promotion transaction, and the API reads it on submit.

---

## Low

### R6-11: Identifier anchoring and name collisions (VERIFIED)

**Code involved:**
- `_DATASET_RE.match`, `_CODE_RE.match` and the JSON-Schema `pattern`s use Python `$`, which also matches before a trailing `\n`. As a result, all of the following are accepted:
  - `dataset "demo_pjan\n"`;
  - `geo "FR\n"`;
  - `name "eurostat.demo_pjan.fr\n"`;
  - `version "1.0.0\n"` (see R6-05).
- `connector_name` collides in two ways:
  - `["FR-DE"]` and `["FR","DE"]` both give `…fr-de`;
  - `["fr"]` and `["FR"]` give the same name.

  Neither collision is reachable under the current FR-only scope.
- Built-ins cannot collide with template connectors: no built-in name has the `eurostat.` prefix, and the ingest handler applies built-ins last.

**Fix:** use `re.fullmatch` everywhere. Add explicit `fullmatch` checks for `name` and `version` in `validate_manifest`, because JSON-Schema patterns go through `re.search`. Canonicalise geo (uppercase, sorted) and join with a character that cannot appear in a code.

### R6-12: Manifest fields still outside the binding (VERIFIED, `test_uncovered_fields_still_auto_promote`)

The gate promoted a manifest with:
- `request_parameters.licence = "CC0 … no attribution"`, which became `ConnectorSpec.licence`;
- `rights_ref` pointing at the World Bank;
- `special_categories` swapping `Y_OPEN` and `UNK`, so that published flags label the open-ended age group `unknown`;
- `mapping_version` naming the *incompatible* WDI mapping;
- a provider-supplied `observed_status_labels` and `dataset_label` ("midyear … WDI compatible").

**Fix (agent G):** derive these in `binding.py`:
- licence and rights from the registered source;
- special categories from a template constant;
- `mapping_version` from the mapping manifest.

Alternatively, drop them from the manifest.

### R6-13: The sandbox child reaches the worker's filesystem (VERIFIED, `test_sandbox_child_filesystem_reach`)

**Evidence.** Under the exact `unshare --user --map-user=65534 --net --pid …` prefix, and in plain mode, the child:
- read a 0600 file owned by the worker;
- wrote outside its temporary directory. The host uid is unchanged, and there is no mount namespace.
- had no `RLIMIT_NPROC` and could fork.

In plain mode it could also signal its parent. On hosts without user namespaces, which is likely the case on Render, `network_not_os_isolated` and `parent_environment_readable` are only warnings, so T1 still promotes.

Only reviewed template code runs in the child, so this is defence in depth only.

**Fix:**
- Use a mount namespace with a read-only root and a private tmpfs (or `bwrap --ro-bind / / --tmpfs /tmp`).
- Set `RLIMIT_NPROC`.
- Set `prctl(PR_SET_DUMPABLE, 0)` in the worker.
- State in the evidence that the filesystem is shared.

### R6-14: The repair replay check treats estimation and status flags as non-semantic (VERIFIED, `test_r6_repair_nodb.py`)

**Evidence.** A `MappingRevision` that renames `isReported` or `legacyEstimationFlag` away removed those flags from all 48 stored Comtrade vintages (266 cells). The replay check still returned `pass`, with only a `historical_flags_changed` warning.

Changes to `classificationCode` and to values are correctly blocked. A conditional scale rule whose condition never matches the stored vintages also passes; this is by design, since such rules exist for new vintages. Repairs are T2 (named review), so nothing is applied silently, but the report understates the change.

**Fix (HOOK):**
- List estimation, status and classification flags in `SchemaSemantics` as semantic, so that changing them is a blocker.
- Report it when `accepted_history` hits its 500-row cap. This cap was not tested.

### R6-15: Fixture-mode discovery runs in production at the handler level (VERIFIED, `test_c_fixture_mode_in_production_handler`)

**Code involved:**
- Only the API refuses `mode=fixture` in production. `handle_discovery` runs it under `reva_env=production`, uncharged; jobs can also be submitted through `ops.submit_job` from the worker role.
- A fixture-mode promotion creates eligibility that live ingestion uses, because eligibility carries no origin.
- The production image excludes `tests/`, so the run would crash with an untyped error rather than return a typed refusal.

**Fix (agent G):** the handler refuses `fixture` when `reva_env == "production"`.

### R6-16: Registry files that T1 trusts derive only T1 (VERIFIED, `test_r4_11_path_normalisation`)

**Code involved.** `registry/concepts/demography.yaml` and `registry/entities/countries.yaml` derive T1. Yet `binding.py` treats the concept `semantic_profile` and the `eurostat_geo` aliases (which feed `eligibility_scope`) as ground truth.

**Fix:** add `registry/concepts`, `registry/entities`, `registry/mappings`, `registry/requirements` and `src/reva/ontology` to `T3_PATH_PREFIXES`.

---

## Checked and fine

- **Built-in shadowing.** Promoted names can no longer shadow built-ins: built-ins are applied last, and a collision is audited.
- **Gate refuses a tampered policy or template:**
  - a changed policy file or an unpinned template digest → `blocked`/`reject`;
  - a manifest from another template revision → `reject`.
- **Validator data contract.** It now comes from the registry profile. A manifest that lies about its universe, unit or support fails on data, not just on binding.
- **Budget ledger:**
  - per-source limit 0 is refused, and an unknown source fails closed;
  - ingest and discovery share one lock and one ledger.

  Concurrent overshoot was checked by reading only.
- **Kill switch:** re-read on every guard call, after taking the evidence lock in the promotion transaction, and before loading promoted connectors. The deployment gap is R6-10.
- **Sandbox network.** In the namespace the network is verified blocked, and `/proc/<ppid>` is not visible (`ppid` = 0). Evidence labels are measured, not nominal.
- **Prompt injection.** Catalogue titles, labels and status labels are control-stripped and length-capped, and are used only for ranking or as flags. No LLM is in the loop. The exception is structure, through R6-04: the unit code, dimensions and categories are provider data that reach the manifest.

## Interactions with GOV (in flight)

The DB approval path improves on R4:
- one approval binds to one target;
- the proposal type is checked;
- T0 passes can no longer approve eligibility;
- human-attributed rows are refused from runtime roles.

It still has these gaps:
- It binds to the proposer-written `ops.proposal.summary.scope` rather than to what the gate evaluated, and not to the connector spec (R6-02).
- It accepts gate passes for `model_registration` (R6-03).
- `reva_worker` still holds `ops.record_gate_result`.

During the review, `evidence.approve_eligibility` briefly failed with `boolean -> unknown`. That was GOV mid-edit, and it was resolved by the final run.
