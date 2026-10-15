# Independent promotion gate (process separation)

Agent GATE, 2026-10-15. This closes the residual risk recorded in `docs/limitations.md`
("Governance limits"), R5-01 part 3 and R6-03. The V1 autonomy gate used to run inside the worker
process with the worker's database login. Because `ops.record_gate_result` was executable by
`reva_worker`, the worker could record a passing gate result for its own proposal.

## What changed

### Database (`migrations/versions/0007_security.py`, governance section)

**New login `reva_gate`**
- Created by the migration like the other runtime roles: `NOLOGIN NOINHERIT`, with forbidden attributes checked.
- Bootstrap then sets `LOGIN`, a SCRAM verifier and the configurable `CONNECTION LIMIT` (`REVA_ROLE_CONNECTION_LIMIT`).
- `verify_runtime_role_attributes`, `verify_ownership` and `verify_runtime_memberships` cover it, because `RUNTIME_ROLES` is derived from `ROLE_LOGIN`.

**Only `reva_gate` records gate results**
- `EXECUTE ON ops.record_gate_result` is granted to `reva_gate` only.
- `reva_worker` lost that grant, and lost `INSERT` on `ops.gate_result`.
- `ops.gate_result` is now a governed table. `meta.enforce_governance` refuses every inserter except the `reva_gate` login, including the migration owner.
- `meta.session_principal()` knows `reva_gate`.

**Approvals require `recorded_by = 'reva_gate'`**
- `ops.consume_approval` (used by `evidence.approve_eligibility`, `ontology.approve_mapping` and `model.approve_version`) accepts a gate result only if `recorded_by = 'reva_gate'`. An API-recorded human review is still accepted as before.
- `evidence.restore_eligibility` re-enables a version only if its last eligible row is independently approved. That means one of:
  - it was written by the owner (reviewed seed or admin CLI);
  - an `ops.approval_use` row exists for exactly that target, whose approval is a `reva_gate` pass or an API-recorded review.

**New table `ops.gate_request`**
- Columns: `proposal_id`, `gate_name`, `reports` (an array of at most 16 entries and 256 KiB), `policy_sha256`, `recorded_by`, `recorded_at`.
- It is append-only (immutable trigger) and `recorded_by` is forced.
- The worker may `INSERT` and `SELECT`. The gate and the API may only `SELECT`.

**Least privilege for `reva_gate`**
- `USAGE` on `meta`, `evidence` and `ops`.
- `SELECT` on `ops.proposal`, `ops.gate_request`, `ops.gate_result`, `evidence.connector_version`, `evidence.source` and `evidence.eligibility`. These are what `PolicyPromotionGate` and the T1 binding read.
- `SELECT, INSERT` on `meta.artifact` and `meta.artifact_location`, so the gate can register its decision evidence. Two restrictions apply:
  - The trigger `a02_gate_artifact` (`WHEN session_user = 'reva_gate'`) accepts only `gate-decision/1` JSON.
  - RLS allows locations only in the `evaluation` namespace.
- No other table, schema or function. The gate cannot read observations, beliefs, jobs, tokens, reviews or audit rows. It cannot submit jobs, approve, or record promotions.

**Indexes**
- `ops.gate_request(recorded_at, id)`.
- `ops.gate_result(proposal_id, recorded_at)`.

### AFX's R6-02 and R6-03 database patches (applied by GATE in the 0007 governance section)

**R6-02: the gate pass is bound to what the gate evaluated**
- `ops.record_gate_result` is re-created with a seventh argument, `p_scope jsonb DEFAULT NULL`. It is stored in the new column `ops.gate_result.scope` (a JSON object of at most 64 KiB) and is still executable by `reva_gate` only.
- `PolicyPromotionGate._record` passes `t1_scope.db_scope` for a `promote` decision: concept, dataset, subject_ids, years, manifest digest and mapping key.
- For a gate pass, `evidence.approve_eligibility` and `ontology.approve_mapping` use the gate-derived scope, not the proposer's `ops.proposal.summary.scope`. A pass without a scope approves nothing. A human review still uses the reviewed proposal's scope.
- `approve_eligibility` also requires two things of the connector version:
  - `declared_capabilities.manifest_sha256` must equal the scope's `manifest_sha256`. `cli/seed.register_connector_version` now records it from `spec.field_mappings.manifest_sha256`.
  - `declared_capabilities.datasets` must equal `[scope.dataset]`.
- `reva_registry` gains `SELECT` on `evidence.connector_version`.

**R6-03: T2+ changes need a named human review**
`ops.consume_approval` refuses a gate pass for `model_registration` or `mapping_equivalence` proposals, and for any proposal with `permission_tier >= 2`. Such proposals need an approving human review recorded through the API.

**Regression tests** are in `tests/security/test_gate_separation_security.py`:
- `test_r6_02_gate_pass_is_bound_to_the_gate_derived_scope_and_manifest`;
- `test_r6_03_t2_proposals_need_a_human_review_not_a_gate_pass`.

**Schema contract.** `migrations/schema_contract.json` was regenerated at 02:08 with `scripts/update_schema_contract.py`, on a throwaway database. No migration had been edited for more than 35 minutes, and the 0007 and 0008 edits were committed. The regeneration also picked up ENG's `ops.reserve_llm_budget`/`ops.settle_llm_budget` from 0008. `--check` reports that the contract is up to date.

### Process (`src/reva/gate/`, new package)

**Worker side: `reva.gate.client.GateClient`**
- It is a drop-in for `PolicyPromotionGate.decide` in the worker.
- It inserts an `ops.gate_request` row with the persisted proposal id and the report objects, then polls for the newest `ops.gate_result` with `recorded_by = 'reva_gate'` recorded after the request.
- The outcome is read from the gate's digest-verified evidence, which must agree with the database row. Otherwise the decision is treated as `blocked`.
- The wait is bounded by `REVA_GATE_WAIT_SECONDS` (default 120). On timeout the client returns an unrecorded `blocked` decision whose reason starts with `gate_pending`. Discovery then reports `status: gate_pending` and promotes nothing.

**Gate process: `reva.gate.service.GateService`**
- Entrypoints: `reva gate [--once]`, `python -m reva.gate.service` and `scripts/start_gate.py`.
- It logs in as `reva_gate`; `check_gate_login` refuses any other login.
- It polls `ops.gate_request` for requests that have no later `reva_gate` decision. It polls rather than using the job queue, so it needs no `ops.job` write privileges, and the worker cannot claim gate work by mistake.
- It takes a per-request session advisory lock and re-checks that the request is still pending.
- It runs the unchanged reviewed `PolicyPromotionGate` with its own engine. That class loads the `ops.proposal` row and its digest-verified document, derives the tier, verifies the reports against their stored evidence and, for T1, recomputes the validator and sandbox. The pinned policy is loaded by the gate itself, never taken from the request.
- The gate then records the decision through `ops.record_gate_result`.

**Failure handling (fail closed)**
- Kill switch or autonomy disabled: nothing is recorded and the request stays pending.
- Typed refusal (`RevaError`, for example a malformed request, a tampered or unreadable artifact, or an unknown proposal): a `blocked` decision is recorded.
- Unexpected error: retried on the next poll. After 5 failures a `blocked` decision is recorded.
- If even the `blocked` decision cannot be recorded, the request is skipped for the rest of the process lifetime.

**Promotion**
- The worker promotes only through `promote_connector`, using the decision returned by the client. That function checks the gate row and its evidence. The approval functions then require that the cited gate result was recorded by `reva_gate`.

**Call sites switched to `GateClient`**

Each is a minimal edit marked `GATE`:
- `src/reva/autonomy/jobs.py`: discovery, and the equivalence rejection that reuses the same client;
- `src/reva/autonomy/repair.py`: `handle_connector_repair`;
- the docstring of `src/reva/autonomy/gate.py`: the "honest limit" paragraph is replaced.

`PolicyPromotionGate` itself is unchanged. With the worker's engine its `_record` now fails with `permission denied`.

### Settings, start scripts and Blueprint

**Settings**
- `Role` and `ProcessRole` include `gate`.
- `ROLE_LOGIN["gate"] = "reva_gate"`.
- New settings: `REVA_GATE_DB_PASSWORD` (secret), `REVA_GATE_WAIT_SECONDS` and `REVA_GATE_POLL_SECONDS`.
- `runtime_url("gate")`.
- `required_for("gate")`: the gate password, and no owner URL in production.
- `required_for("bootstrap")` needs the gate password in production only. Outside production the gate login may be left unprovisioned. Nothing can then be auto-promoted, which is fail closed and keeps older local or test bootstraps working.
- The engine kind `gate` is added.

**`scripts/start_gate.py`**
It mirrors `start_worker.py`:
- it refuses owner credentials in production and a conflicting `REVA_PROCESS_ROLE`;
- it validates the configuration the child will receive;
- its allowlisted child environment holds only `REVA_GATE_DB_PASSWORD` and the gate's own S3 credential. There are no worker, sim or API passwords, no session secret, no provider keys and no proxy (the gate makes no outbound requests);
- it waits, bounded by `REVA_GATE_STARTUP_DEADLINE_SECONDS`, until the `reva_gate` login can see `ops.gate_request` and may execute `ops.record_gate_result`.

**`scripts/_runtime_env.py`**
It gains the `gate` component. The worker allowlist never includes the gate password.

**`render.yaml`**
- `REVA_GATE_DB_PASSWORD` is generated on `reva-api` because the pre-deploy bootstrap provisions it. The API child does not receive it.
- New background worker service `reva-gate`:
  - plan `0.5c-512mb`, one instance;
  - `python scripts/start_gate.py`, `REVA_PROCESS_ROLE=gate`;
  - the password from `reva-api`;
  - its own `sync: false` S3 key pair, prefix-scoped: read the autonomy prefixes, write `evaluation/`, `models/` and `normalised/`.
- The pre-deploy is unchanged.
- `scripts/validate_blueprint.py` checks the gate service and that the worker never receives the gate password. It passes.

**`.env.example` / `.env`**
`REVA_GATE_DB_PASSWORD=<local-only value>` (see `.env.example`).

## Tests

**`tests/security/test_gate_separation_security.py`** (new, 16 tests). It covers:
- worker, API and simulation logins cannot record gate results, either through the function or directly;
- the gate login can, and its rows carry `recorded_by = 'reva_gate'`; the identity checks still fire;
- the owner is refused;
- forged gate rows (trigger bypass with `recorded_by` `reva_worker` or the owner) are refused by `approve_eligibility`, `approve_mapping` and `approve_version`, while a positive control passes;
- `restore_eligibility` refuses an unapproved eligible row, with an owner-written positive control;
- the gate's least-privilege matrix;
- evidence-only artifact registration;
- `gate_request` is worker-written and append-only;
- role hygiene: `NOINHERIT`, a login, and a membership is detected;
- settings: the gate password is required only in production;
- `start_gate.py` scrubbing and fail-closed checks;
- Blueprint checks.

**`tests/integration/test_gate_separation.py`** (new, end to end):
1. The worker files a request. With no gate running it gets `gate_pending`, nothing is recorded, and promotion is refused.
2. `python -m reva.gate.service --once` runs as a real subprocess with only the gate login and the test database. It recomputes the reports and records `pass` as `reva_gate`.
3. The worker promotes through `approve_eligibility`. The `ops.approval_use` row cites the `reva_gate` result.
4. Rollback restores the superseded version through the hardened `restore_eligibility`.

A second test covers the service failing closed: the kill switch records nothing, and a malformed request is recorded as `blocked`.

**Adapted helpers (not production checks)**
- `tests/conftest.py`: `DB.gate` (the `reva_gate` engine), plus a `gate_service` fixture that runs `GateService` in a background thread with its own engine.
- Tests that called `PolicyPromotionGate(db.worker, …)` now use `db.gate`.
- Discovery and repair tests that run the worker step request `gate_service`.
- `test_db_hardening.py` and `test_governance_r5.py` record gate results with the gate login on committed proposals.
- The worker-INSERT matrix now expects `ops.gate_request` and no `ops.gate_result`.
- Expected role lists include `reva_gate`.
- `scripts/demo_workflow.py` step 9 runs a `GateService` next to the in-process worker.

## Residual risk (honest)

- **Inputs are worker-stored.** The gate decides on what the worker stored: the proposal document, the report evidence, and the captured fixture bank it re-runs the validator and sandbox on. A compromised worker chooses *what* is evaluated, though not the verdict. T1 binding (registry semantics, pinned template digest, approved mapping, content-derived version) limits what can pass.
- **Same image.** Both processes run the same image, so a code-level compromise affects both.
- **Local development** (`reva gate`, `make`/uv on the host): the gate runs on the same host or container, usually as the same OS user, from the same `.env` that holds every runtime password. The separation there is a credential convention, not an isolation boundary.
- **Render:** `reva-gate` is a separate service with its own environment. The API service also holds the gate password because its pre-deploy bootstrap provisions it, exactly as it holds the worker and sim passwords; `start_api.py` scrubs it from the API child.
- **Still worker-writable:**
  - `model.record_promotion` (tournament promotions);
  - `model.evaluation`;
  - `meta.artifact` metadata;
  - objects in the store. The gate verifies digests, so content under a digest cannot change, but the worker can write new objects.
- **`gate_pending`.** If the gate service is down, discovery reports `gate_pending` and does not promote. The request stays queued and the gate decides it later. The `gate_followup` worker job (GATE2, below) then promotes the late pass from the stored proposal.

## Follow-ups for other owners

- `docs/deployment.md` and `docs/operations.md` (DKR/DOC2) should document:
  - the `reva-gate` service and its S3 key pair (prefix-scoped as above);
  - running `reva gate` locally next to `reva worker`;
  - the `gate_pending` status.
- `Makefile` (CI): add a `gate:` target (`$(PY) reva gate`).
- `reva verify` (`src/reva/cli/main.py`) could also probe the `gate` login when `REVA_GATE_DB_PASSWORD` is set.
- AFX:
  - `PolicyPromotionGate._record` could assert `session_user = 'reva_gate'` before recording, for a clearer error than `permission denied`;
  - `_verify_gate_row` in `jobs.py` could also require `recorded_by = 'reva_gate'` as defence in depth. The database already enforces it at approval time.

## GATE2 follow-ups (implemented 2026-10-15)

**Late gate decisions: `gate_followup`.**
- Handler: `reva.autonomy.followup:handle_gate_followup`, registered in `operations/jobs.HANDLERS`.
- Schedule: `gate_followup:promotion`, every 300 s, in `cli/seed.DEFAULT_SCHEDULES`. `operations/schedules.SCHEDULE_JOB_TYPES` allows it.
- Candidates are `connector_promotion` proposals that meet all of these:
  - their newest decision is a `reva_gate` pass;
  - the pass is not consumed by an `ops.approval_use`;
  - the pass has no recorded `autonomy.gate_followup` outcome;
  - the pass is younger than `max_age_hours` (default 168);
  - the pass is older than `REVA_GATE_WAIT_SECONDS` + 30 s, so a discovery step that is still waiting handles its own decision.
- Promotion goes through `promote_connector`, from the stored proposal. No provider data is fetched.
- Safety:
  - the job is fenced by the lease, including the outcome record;
  - the kill switch is checked before the run, before each candidate and inside the promotion transaction. A blocked run records nothing.
- Outcomes:
  - a typed or database refusal is recorded once and is terminal;
  - an `OperationalError` is retried on the next run.
- The gate login cannot write `ops.outbox` or submit jobs, so the job is scheduled rather than triggered by the gate.
- Not covered: `connector_repair:*` proposals. Their promotion path is `repair.py` and needs a separate follow-up.

**Rollback restore chain** (`autonomy/jobs.rollback_promotion`, `_restore_superseded`):
- A rollback row no longer copies `superseded_by`, so a rolled-back version counts as rolled back, not as superseded.
- Restoration happens only if the rolled-back version was the eligible one.
- The restore walks back past versions that were themselves rolled back, to the newest version retired by supersession of that chain. It calls `evidence.restore_eligibility` with the chain member as `p_rolled_back`.
- A refused restore leaves the rollback in place (savepoint).
- Tests: `tests/integration/test_autonomy_rollback_chain.py`.

**`_verify_gate_row`** also requires `recorded_by = 'reva_gate'`. Test: `tests/security/test_gate_row_recorded_by.py`.

**`reva verify`** adds `db_gate`, which comes from `reva.gate.verify.verify_gate`:
- It logs in as `reva_gate` and compares the login's actual privileges with the expected set:
  - role attributes and memberships;
  - schema USAGE and CREATE;
  - relation privileges, at table or any-column level;
  - EXECUTE on SECURITY DEFINER functions.
- When `REVA_GATE_DB_PASSWORD` is unset, the check is skipped with a note outside production and fails in production.
- Tests: `tests/security/test_gate_verify.py`.

**Make and docs:**
- `make gate`;
- `docs/deployment.md`: the `reva-gate` service, its S3 key pair, and the gate password generated on `reva-api`;
- `docs/operations.md`: running `reva gate` locally, `gate_pending`, `gate_followup`, rollback and the kill switch.
