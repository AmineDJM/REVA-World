# Review: autonomy, sandbox, Eurostat template, deployment and UI (agent R4, adversarial, read-only)

Scope:
- `src/reva/autonomy/**`, including `sandbox.py` and `sandbox_child.py`
- `src/reva/adapters/connectors/eurostat_template.py`
- `scripts/{start_api,start_worker,bootstrap_runtime,_runtime_env,backup_restore}.py`, `scripts/{backup,restore}.sh`
- `Dockerfile`, `.dockerignore`, `.github/workflows/*`, `render.yaml`
- `ui/src/**`

Reviewed against prompt §10–§12, and architecture §10, §11, §12, §18, §19, §21, §23 and §24.

**Git state.** The review started at HEAD `333bb78` and finished at HEAD `229c667`, both with uncommitted changes by other agents. Files changed while the review was running:
- OPS-FIX:
  - `render.yaml` now has `REVA_DB_SSLMODE` and `REVA_PROCESS_ROLE`.
  - `scripts/_runtime_env.py` has gained `validate_for` and `check_process_role`.
  - `scripts/start_api.py`, `start_worker.py` and `bootstrap_runtime.py` were updated.
- `src/reva/autonomy/jobs.py` now passes `proxy_mode`.
- `ui/src/lib/scenario.ts`: the CSV export now handles `sensitivity_range`.

Every finding below was re-checked against the text current at the end of the review. Findings already reported in `review_core.md` (R-xx) or `review_api_sim_ops.md` (R2-xx) are not repeated; they are cited where they interact. No code was modified.

## Method

**VERIFIED** means reproduced by a scratch test:
- DB tests ran against a fresh database created by the `tests/conftest.py` `db` fixture (`reva_test_<random>`, dropped afterwards). The dev DB `reva` was never touched.
- The remaining tests were offline.

**PLAUSIBLE** means established by reading the code only.

Scripts and raw output are in `/tmp/claude-0/-home-user-REVA-World/a82f628c-2790-54df-b0b4-28930bdc7a97/scratchpad/review4/`:

| File | Contents |
|---|---|
| `test_r4_autonomy_db.py` | Gate bypass, rollback, stale spec, model registry (DB) |
| `test_r4_template_change.py` | Template revision silently disables the promoted connector (DB) |
| `test_r4_nodb.py` | Format-string template, untyped parser errors, sandbox network/`/proc`, budget expression |
| `render_env_check.py` | Builds each Render service's environment from `render.yaml` and runs the entrypoint validators |
| `format.ts` + `ui_check.ts`, `csvcell.ts` | UI rounding and CSV guard (`node --experimental-strip-types`) |
| `out_*.txt` | Raw output |

To run them:
```
uv run pytest <file> -p no:cacheprovider -c pyproject.toml --rootdir /home/user/REVA-World -s -o addopts=""
```

`npm --prefix ui run test -- --run`: 40/40 tests passed.

Severity uses the same scale as `review_core.md`:
- **High**: breaks a §24.1 invariant or a security/privilege boundary on a normal code path.
- **Medium**: needs a specific sequence, or is a scientific or labelling defect with limited reach.
- **Low**: hardening or a latent defect.

## Summary

| ID | Sev | Status | Area | One-line summary |
|---|---|---|---|---|
| R4-01 | High | VERIFIED | T1 gate | The gate auto-promotes manifests whose semantics, source, lineage family and name are not checked against policy or registry. A manifest declaring midyear support and another universe and unit, registered under `worldbank.wdi`, was promoted, and its name replaced the built-in WDI connector in the ingest handler. |
| R4-02 | Med-High | VERIFIED (working tree) | deploy | With the current `render.yaml`, the worker fails production validation: the https rule for `REVA_PUBLIC_BASE_URL` applies to the worker, which never receives that variable. The fix is in flight (OPS-FIX). |
| R4-03 | Medium | VERIFIED | promotion | Connector identity is `(name, version)` and the version is always `1.0.0`. Re-promoting a changed manifest keeps the old spec. After a template revision the promoted connector is dropped from ingest permanently, while discovery still reports `promoted`. |
| R4-04 | Medium | VERIFIED | rollback | After a rollback, re-running discovery reports `promoted` (gate pass, outbox event, audit) although eligibility stays `retired`. Retired connectors are still loaded for ingest, and the previous version is never restored. |
| R4-05 | Medium | VERIFIED | model registry | `PostgresModelRegistry.register` accepts any `GateDecision` object: a fabricated `human:nobody` decision registered an `approved` model. |
| R4-06 | Medium | VERIFIED | API ↔ autonomy | `POST /v1/discovery` fields (`catalogue`, `query`, `concept_id`, `max_candidates`) are ignored. Every call runs live Eurostat `demo_pjan` discovery, which can auto-promote. Unsupported catalogues are not rejected with 422. |
| R4-07 | Medium | VERIFIED | UI honesty | `formatDecimalString` drops the rounding carry: `0.9999999` displays as `0` and `1999.99999995` as `1,999`. |
| R4-08 | Med-Low | VERIFIED | budgets | Daily and per-source budgets are never enforced (`ops.budget_ledger` unused), and a per-source limit of `0` becomes 5 requests per job. |
| R4-09 | Med-Low | VERIFIED | template | `logical_key_template` is a Python format string run through `str.format_map`: attribute traversal and width-based memory blow-up, run in-process in the worker by the validator. |
| R4-10 | Med-Low | VERIFIED locally / PLAUSIBLE on Render | sandbox | The sandbox's network refusal is a monkeypatch (raw `_socket` still connects). The child runs as the worker's UID and can read the worker's `/proc/<ppid>/environ` (DB passwords). The evidence still reports `blocked_by_default`. |
| R4-11 | Low-Med | VERIFIED | tiers / policy | The template digest is not pinned in policy (`template_digest_ref` is a path), edits to `eurostat_template.py` are only T2, and paths are not normalised (`./src/reva/kernel/...` → T2, `registry/./policies/...` → T1). |
| R4-12 | Low-Med | PLAUSIBLE | backup | Plaintext dump on disk before `age`, left behind if encryption fails. Backups are unauthenticated, and restore runs `pg_restore` as owner on whatever archive sits next to its own checksum. |
| R4-13 | Low | PLAUSIBLE | kill switch | The kill switch is a process-start snapshot. In-flight discovery runs through to promotion, and promoted connectors keep being ingested while it is on. |
| R4-14 | Low | VERIFIED | parsing | Malformed JSON-stat raises untyped `ValueError`/`AttributeError`. The validator catches only `RevaError`, so the discovery job crashes and retries live instead of producing a `fail` report. |
| R4-15 | Low | PLAUSIBLE | sandbox | `preexec_fn` (which imports `resource`) in a multi-threaded worker can deadlock the child between fork and exec. |
| R4-16 | Low | VERIFIED | UI export | The CSV formula guard is bypassed by `-<digit>…` payloads, and a newline in the scenario title injects a raw line into the CSV header. |
| R4-17 | Low | VERIFIED (reading + R4-04 run) | labelling | The UI and API say "nothing is promoted without gates and a human review", but T1 auto-promotes. Proposals show the declared tier, not the derived one, and `needs_review` is stored and shown as `blocked`. |
| R4-18 | Low | PLAUSIBLE | CI | `live-smoke.yml` exposes `REVA_COMTRADE_API_KEY` at job level to every step, including third-party actions and `uv sync` builds. |

---

## High

### R4-01 — The T1 gate does not bind the manifest to the approved policy or registry semantics (VERIFIED)

**Code involved**

`gate.py:125-197` (`_t1_scope`) checks only:
- the template id and hosts;
- the template digest (proposer-claimed, compared with the current code);
- `mapping.relation` (proposer-supplied, inside the change document);
- that the concept id and version appear in `approved_exact_mappings`;
- geography (`filters[subject.dimension]`);
- rights;
- the eligibility-scope prefix;
- the presence of validator and sandbox reports.

It never compares any of the following with the registry concept's `semantic_profile`, the registered source, or a regenerated manifest:
- `request_parameters.semantics.*`: `temporal_support`, `population_universe`, `unit_ref`, `aggregation`, `spatial_scheme`;
- `source_key`, `lineage_family`, `name`, `version`;
- `subject.alias_provider`, `logical_key_template`, `resolution_dimensions`, `total_categories`, `cadence`, `max_response_bytes`.

The "independent" semantic checks are circular:
- `quality.contract_for` (`quality.py:25`) builds the data contract from the manifest itself.
- The parser stamps every observation with the manifest's own semantics.
- So validator and sandbox always agree with whatever the manifest says.

`jobs.py:_all_connectors` (in `operations/jobs.py`) does `connectors.update(promoted_connectors(...))`, so a promoted name overrides a reviewed built-in connector of the same name.

**Failure scenario**

1. A proposal carries the approved dataset (`demo_pjan`), concept and geography (FR), but with:
   - `temporal_support: point:07-01`, a different universe and a different unit;
   - `source_key: worldbank.wdi` and `lineage_family: worldbank`;
   - `name: worldbank.wdi.indicators`, `version: 9.9.9`.
   It could come from a future LLM-assisted generator, a bug, or any code holding worker credentials (R-01).
2. Validator, sandbox and gate all pass, and `promote_connector` registers the connector under the World Bank source with `eligible` status for the 1 January concept.
3. The ingest handler now runs the Eurostat template whenever `worldbank.wdi.indicators` is requested.

This is exactly the "tempting false equivalence" and the "source-family dependence" check that §10.2 requires the gate to stop. It also breaks §18.2: "Gate results are produced by a separately controlled evaluator, not accepted from the proposing agent".

**Root cause.** The gate runs inside the proposing job:
- It runs in the same process with the same worker credentials.
- Its reports are unsigned objects passed by the proposer (`jobs.py:193`).
- `ops.gate_result` is directly insertable by the worker (R-01).

So independence holds only by convention. In V1 the generator is deterministic and builds semantics from the registry, so this is latent today. It is still the only T1 auto-promotion boundary.

**Evidence** (`test_gate_promotes_manifest_with_wrong_semantics`, `out_db.txt`):
```
validator pass []   sandbox pass []
gate outcome: promote ('T1 within signed policy scope: ...')
ingest connector for 'worldbank.wdi.indicators': EurostatJsonStatConnector worldbank.wdi
registered under source: worldbank.wdi
```

**Fix** (agent G; `_all_connectors` is agent D)

1. In `_t1_scope`, regenerate the expected manifest deterministically from:
   - the approved policy entry,
   - the registry concept's `semantic_profile`,
   - the candidate's inspection artifact.

   Use `DeclarativeConnectorGenerator.build_manifest`, called by the gate itself. Then require `manifest == expected`, or at least require equality on:
   - `semantics` (each field equal to the registry profile),
   - `source_key` and `lineage_family` (from `registry/sources`),
   - `name` (`eurostat.<dataset>.<geo>`),
   - `subject` (`geo` / `eurostat_geo`),
   - `logical_key_template`, `resolution_dimensions`, `total_categories`.
2. Validator: derive the data contract from the registry concept and the policy entry, never from the manifest (`contract_for(concept, policy_entry, manifest)`), so that `temporal_support_mismatch` and `unit_mismatch` can fire.
3. `_all_connectors`: never let a promoted connector shadow a built-in name. Keep separate namespaces (for example, require the `eurostat.` prefix) and raise on collision.
4. Make reports independent in fact:
   - The gate re-runs the validator and sandbox itself from the persisted proposal, rather than accepting caller-supplied `ValidationReport`s.
   - Or the gate loads reports from DB rows written by a separately credentialed evaluator.

   Combine this with R-01's fix: `ops.record_gate_result` must be the only write path, and it must not be granted to the role that writes proposals.

---

## Medium-High

### R4-02 — The worker cannot start in production with the current Blueprint (VERIFIED against the working tree; fix in flight)

**Code involved**

`settings.py:180` applies `REVA_PUBLIC_BASE_URL must be https in production` to every process role. `render.yaml` gives `REVA_PUBLIC_BASE_URL` only to `reva-api`, so the worker falls back to the default `http://localhost:8000`.

**History.** At the start of the review, `render.yaml` also lacked `REVA_DB_SSLMODE` (required in production by `settings.py:175`) and `REVA_PROCESS_ROLE`. Without `REVA_PROCESS_ROLE`, the worker was also asked for `REVA_SESSION_SECRET`. Every service and the pre-deploy step would have failed. OPS-FIX has since added both variables and `validate_for`.

**Evidence** (`render_env_check.py`, which builds each service's environment from `render.yaml` with dummy values):
```
api OK {... 'process_role': 'api', 'db': 'postgresql+psycopg://reva_api:***@db.internal:5432/reva?sslmode=require' ...}
worker FAIL ConfigError ... invalid production configuration: REVA_PUBLIC_BASE_URL must be https in production
bootstrap OK
```

**Fix** (agent I / OPS-FIX, plus lead for `settings.py`)

- Apply the base-URL rule only when `reva_process_role in (None, "api")`, as is already done for the session secret.
- Add `render_env_check.py` as a test (`tests/security/test_start_env*`) so that `render.yaml` is always validated through each entrypoint's real `validate()`.

---

## Medium

### R4-03 — A re-promoted or re-templated connector keeps a stale spec, or disappears from ingest (VERIFIED)

**Code involved**
- `generator.py:93` hard-codes `"version": "1.0.0"`.
- `cli/seed.py:191-193` (`register_connector_version`) returns the existing row for `(name, version)` without comparing `spec_sha256` or `code_digest`.
- `jobs.py:384` (`promoted_connectors`) skips any row whose stored `code_digest` differs from the current `template_digest()`, logging only a warning.

**Failure scenarios**

1. *Changed manifest, same name.* The gate approves manifest B. `promote_connector` returns manifest A's `connector_version_id`, and ingest keeps running A. The gate decision, outbox event and audit all describe B.
2. *Reviewed template revision.*
   - Every existing promoted connector is skipped from ingest.
   - Re-running discovery cannot register the new spec, because the version is still `1.0.0`.
   - The connector is gone for good, yet discovery reports `promoted`.
   - This is the self-healing path of §10.3.

**Evidence**

`test_second_promotion_same_name_version_keeps_stale_spec`: gate `promote`, but the running manifest still has `population_universe = usually_resident_population_eurostat`, not `changed_universe_v2`.

`test_template_change_disables_connector_forever`:
```
run2: promoted promote rows: [('1.0.0', 'sha256:366d4')] connector loaded for ingest: False
```

**Fix** (lead for `seed.py`; agent G for the generator and jobs)

1. `register_connector_version` raises `ImmutableRecordViolation` when `(name, version)` exists with a different `spec_sha256` or `code_digest`.
2. Derive the generator's version from content, for example `1.0.<n>` with `n` = the number of prior versions, or include a short hash of the manifest and template digest. `promote_connector` must then register a new version.
3. When `promoted_connectors` skips a connector, it should emit a health or outbox event (`connector_template_stale`) and a gap, so that discovery re-proposes it, instead of only logging a warning.

### R4-04 — Rollback: re-promotion is reported but silently inert, and retired connectors keep running (first two VERIFIED; third PLAUSIBLE)

**Code involved**
- `cli/seed.py:255-263`: `record_mapping_and_eligibility` returns early when *any* earlier `eligible` row exists with the same `policy_version`, even if a later `retired` decision supersedes it.
- `jobs.py:205-207` sets `status = "promoted"` and emits `autonomy.connector_promoted` plus an audit row anyway.
- `promoted_connectors` (`jobs.py:371`) does not consult eligibility, so retired connectors are still built for ingest.
- Several versions of one name collapse to the newest (`out[r["name"]] = …`). Retiring the new version therefore never restores the previous one, contrary to §10.2 step 9 ("keep the previous connector available for rollback").

**Evidence** (`test_rollback_not_reenabled_but_reported_promoted`):
```
run2 after rollback: promoted promote {...}
eligibility decisions: ['eligible', 'retired'] is_eligible: False promoted events: 3 pass gate rows: 3
promoted_connectors still contains retired connector: True
```

Only one connector was promoted, but three `connector_promoted` events and three passing gate rows exist. Every discovery run adds another set.

**Fix** (agent G, plus lead for `seed.py`)

1. Before persisting a proposal, `discovery_flow` checks for a later `retired` decision for the same connector and manifest. If there is one, it records the result `rolled_back_hold` and stops: re-enabling needs a human review.
2. `record_mapping_and_eligibility` compares against the *latest* decision. The gate and the job must not report `promoted` unless a new eligibility row was actually written.
3. `promoted_connectors` loads only versions whose latest decision is `eligible`. Rollback re-selects the most recent earlier eligible version.

### R4-05 — The model registry trusts any `GateDecision` object (VERIFIED)

**Code involved**

`autonomy/models.py:59-66` checks only `approval.outcome == "promote"` and the `reviewer_identity` prefix. It does not check:
- that `decision_id` exists in `ops.gate_result`;
- that its proposal concerns this model, key and manifest;
- that the effective tier was ≥ T2 with a recorded human review.

A T0 auto-promote decision for an unrelated proposal, or a hand-built object, is enough.

**Evidence** (`test_model_registry_accepts_unrelated_or_fabricated_decision`):
```
registered with fabricated decision: approved gate_result rows for that decision: 0
```

**Fix** (agent G; lead for SQL)

Go through `model.record_promotion` (the checked DB path mentioned in `0007_security.py`), and verify inside SQL that:
- the gate result exists with `decision='pass'`;
- its proposal's manifest references this `spec_sha256`;
- a `human:` review approved it.

Reject in Python too: re-read the gate row by `decision_id` and compare.

### R4-06 — `POST /v1/discovery` ignores its request (VERIFIED)

**Code involved**
- `api/schemas.py:922` (`DiscoveryRequest`) has `catalogue`, `query`, `concept_id` and `max_candidates`.
- `jobs.py:101-102` reads only `requirement_id` (default `R-POP-JAN-AGESEX`) and `mode` (default `live`).

**Failure scenario**

A user asks to search the `worldbank` catalogue for "nominal GDP". The worker:
1. runs live Eurostat `demo_pjan` discovery;
2. makes up to 3 requests to ec.europa.eu;
3. can auto-promote the France population connector.

The UI then lists the outcome as the result of the user's query. §19 requires unsupported capabilities to return a typed 422.

**Evidence**
```
API payload: {'catalogue': 'worldbank', 'query': 'nominal GDP', 'concept_id': 'economy.gdp.nominal', 'max_candidates': 1}
handler: ... ran with {'requirement': 'R-POP-JAN-AGESEX', 'mode': 'live'}
```

**Fix** (agent E with agent G)

- Make the API schema match the handler: `requirement_id: Literal[...]` from `DISCOVERABLE_REQUIREMENTS`, and `mode` (`fixture` refused in production).
- Return 422 for any `catalogue` other than `eurostat` or any unknown requirement.
- The handler rejects unknown payload keys.

### R4-07 — UI decimal rounding drops the carry: near-1 values display as 0 (VERIFIED)

**Code involved**

`ui/src/lib/format.ts:65` keeps only `roundFraction(...).frac` and discards the carried integer part.

**Affected displays**
- `ValueView`;
- the uncertainty and sensitivity ranges in `UncertaintyPanel`;
- simulation values. These are 50-digit decimals: for example an `exposure_percent_baseline_gdp` of 0.99999995 is displayed as "0".

This is a "missing never shown as zero" class of honesty defect: the value is not missing, but it is shown off by one unit.

**Evidence**
```
0.9999999 -> 0      9.9999999 -> 9      1999.99999995 -> 1,999      -0.99999999 -> −0
```

**Fix** (agent F)
```ts
if (frac.length > maxFractionDigits) {
  const r = roundFraction(intPart, frac, maxFractionDigits);
  intPart = r.int; frac = r.frac;   // make intPart `let`
}
```
Add vitest cases for the four inputs above.

---

## Medium-Low

### R4-08 — Budgets: daily limits are never enforced, and a limit of 0 means 5 (VERIFIED)

**Code involved**
- `budgets.yaml` sets `daily.http_requests_total`, `daily.http_bytes_total` and `per_source_daily_requests`. Nothing reads the daily totals, and `ops.budget_ledger` has no writer (grep).
- `jobs.py:119`: `min(5, guard.source_request_limit(...) or 5)` turns a per-source limit of 0 ("disabled") into 5.
- Each discovery job has its own fresh budget, so repeated `POST /v1/discovery` calls make unbounded daily requests. This interacts with R4-06.

**Evidence**: `policy limit 0 -> requests allowed per discovery job: 5`.

**Fix** (agent G, plus agent D for the ledger)

- Record every outbound request and byte count in `ops.budget_ledger` (per source, per UTC day) from `SafeHttpClient`/`CapturingHttp`.
- `AutonomyGuard.check_budget(conn, source)` refuses when the daily total or per-source total is reached.
- Treat a limit of 0 as blocked, using `limit if limit is not None else default`.
- Alert at 80% (§20.3).

### R4-09 — `logical_key_template` is an unrestricted Python format string (VERIFIED)

**Code involved**

`eurostat_template.py:431` evaluates the manifest-supplied template with `str.format_map(coord)`. The connector JSON Schema leaves `request_parameters` as an open object, and `validate_manifest` does not inspect the template.

**Consequences**
- **Attribute and index traversal.** `{geo.__class__.__mro__}` works. Only `str` objects are reachable, so there is no code execution, but it shows that arbitrary format semantics are honoured.
- **Width-based memory blow-up.** Measured at width 200,000: 61 MiB peak over 309 cells. A width of `10_000_000` needs about 3 GiB.
  - The validator parses in-process in the worker, with no `RLIMIT_AS`, so a 2 GiB worker would be OOM-killed.
  - A promoted connector would do the same on every ingest.
- The gate does not check the template (R4-01).

**Evidence**: `key prefix: k:(<class 'str'>, <class 'object'>): … len: 200036 cells: 309 peak MiB: 61.0`.

**Fix** (agent G)

- Drop the field and always build the key from `key_dims`.
- Or validate it in `validate_manifest` against `^eurostat:\{dataset\}(:\{[a-z0-9_]+\})+$`, with each placeholder in `dimension_order`, and substitute with `re.sub`, never `format`.

### R4-10 — Sandbox isolation is weaker than its evidence claims (VERIFIED locally; PLAUSIBLE on Render)

**Code involved**

`sandbox_child.py:25-33` replaces `socket.socket.connect`, `create_connection` and `getaddrinfo` in Python, but:
- the `_socket` C module still connects;
- UDP `sendto` is not blocked;
- `os`/`subprocess` remain available.

Further:
- The child runs as the worker's UID. Same-UID processes can read `/proc/<worker pid>/environ`, which holds `REVA_WORKER_DB_PASSWORD`, `REVA_SIM_DB_PASSWORD` and the S3 keys, unless YAMA ptrace restrictions forbid it.
- The child can write `/tmp`; `writable_paths=()` is only declared.
- The `network_probe` self-test uses the patched `create_connection`, so it always reports "refused".
- `evidence.child.network_mode = "blocked_by_default_replay_only"`.

Only reviewed template code runs here (spec §18.3 accepts that a subprocess is not a boundary), so this is about honest evidence and defence in depth.

**Evidence** (`test_sandbox_network_block_is_nominal`):
```
socket.create_connection: PermissionError
_socket.connect: connected
(as grandchild) parent environ readable: True
```

**Fix** (agent G)

1. Label the isolation honestly: `network_mode: "python_guard_only"` and `env_isolation: "same_uid"`.
2. Where available, run the child under `unshare --net` (or `bwrap --unshare-net --die-with-parent --ro-bind`) and `prctl(PR_SET_DUMPABLE, 0)` in the worker. `PR_SET_DUMPABLE=0` also makes the worker's `/proc/<pid>/environ` unreadable to same-UID processes. Fall back to a `blocked` result for anything that is not the trusted template.
3. Make the probe use `_socket` directly.

### R4-11 — The approved template is not pinned, and tier derivation ignores path normalisation (VERIFIED)

**Code involved**
- `promotion.yaml:11` stores `template_digest_ref: <path>`, not a digest. The gate (`gate.py:152`) compares the proposer-claimed digest with whatever the template module currently hashes to, so any merged template edit is "the reviewed digest".
- `policy.py:154` (`T3_PATH_PREFIXES`) omits `src/reva/adapters/connectors/eurostat_template.py`. Editing the very code that T1 auto-promotion trusts is therefore only T2.
- Paths are not normalised:
  ```
  ('./src/reva/kernel/values.py',)       -> T2   (should be T3)
  ('registry/./policies/promotion.yaml',) -> T1   (should be T3)
  ```
  None of these can reach `promote`: T1 still requires a connector-change document. They are mislabelled for review only.

**Fix** (agent G; policy edits are maintainer/T3)

- Store `template_sha256: <digest>` in `promotion.yaml`, which is already digest-pinned, and require `template_digest() == policy.template_sha256`.
- Add the template module, `contracts/connector.schema.json` and `src/reva/autonomy` to T3.
- Normalise with `posixpath.normpath` and reject `..`, absolute paths and backslashes before tier derivation.

### R4-12 — Backup and restore: plaintext dump at rest; unauthenticated archives restored as owner (PLAUSIBLE)

**Code involved**

`backup_restore.py:400-409`:
- The dump is written in plaintext to the backup directory.
- `age` is then run.
- The plaintext is unlinked only on success. If `age` fails (`check=True` raises), the plaintext dump stays in a directory meant for off-site copy.

`age` gives confidentiality only: anyone holding the public recipient can produce a valid `reva.dump.age`. The only integrity check is the sha256 in `backup.json`, stored next to the dump.

`restore` (`:504-520`) runs `pg_restore` with the owner credential (superuser locally) on the archive. An attacker with write access to the backup storage can therefore:
- inject DDL or functions that run as the owner;
- make a doctored history pass, because `diff_manifests` compares against the attacker's own `backup.json`.

§21 asks for backups in separate storage precisely because that storage may be compromised.

**Fix** (agent I)

1. Pipe `pg_dump` into `age` (`stdout=age.stdin`) so plaintext never touches disk. Otherwise write to a temporary directory outside `--out` and delete it in `finally`.
2. Sign `backup.json` with a key that is not stored with the backups (`ssh-keygen -Y sign` or minisign) and include the dump's sha256 in the signed file. `restore` verifies the signature before decrypting.
3. Document that restore drills run as a non-superuser owner.

---

## Low

### R4-13 — The kill switch is a start-up snapshot and leaves promoted connectors running (PLAUSIBLE)

**Code involved**
- `AutonomyGuard.from_settings(ctx.settings)` reads the settings loaded at worker start (`jobs.py:76`). A discovery job already past its first check runs through to `promote_connector`; the gate re-checks only the same snapshot.
- Flipping `REVA_AUTONOMY_KILL_SWITCH` needs a redeploy, and that redeploy's SIGTERM is what actually stops the job.
- `operations/jobs.py:_all_connectors` loads promoted connectors regardless of the kill switch.

**Fix** (agent G)
- Read the kill switch from a DB flag (`ops.autonomy_control`, worker read-only, toggled through an admin CLI) at every guard check, including in `promote_connector`.
- Under the kill switch, exclude autonomy-promoted connectors from scheduled ingest.

### R4-14 — Untyped parser exceptions crash discovery and validation (VERIFIED)

**Code involved**
- `_positions` (`eurostat_template.py:169`) calls `int(str(v))` on a non-integer JSON-stat index, which raises `ValueError`.
- `discovery._inspect` raises `AttributeError` when a dimension is not an object.
- Deeply nested JSON raises `RecursionError`.
- `JsonStatConnectorValidator` catches only `RevaError`, so the whole discovery job fails and is retried up to `max_attempts`, re-fetching live each time, instead of producing a `fail` report.

**Evidence**: `exception: ValueError is RevaError: False`.

**Fix** (agent G): wrap structural access in `_doc` and `parse`, and convert `(ValueError, TypeError, AttributeError, KeyError, RecursionError)` into `SourceResponseError(kind="schema_drift", retryable=False)`.

### R4-15 — `preexec_fn` in a threaded worker (PLAUSIBLE)

**Code involved**

`sandbox.py:250` uses `preexec_fn=limits`, and `limits` imports `resource` after the fork. The worker runs heartbeat and `run_sync` threads. Python documents `preexec_fn` as unsafe with threads: the child can deadlock on the import lock and be reported as `wall_time_exceeded`.

**Fix**: import `resource` at module scope and apply limits with `resource.prlimit(proc.pid, ...)` right after `Popen`. Alternatively, run the child as `prlimit --as=… --cpu=… -- python -I -m …`.

### R4-16 — CSV export injection (VERIFIED)

**Code involved**
- `ui/src/lib/scenario.ts:204` (`csvCell`) exempts anything matching `^-?\d`, so `-2+3+cmd|' /C calc'!A0` is exported unguarded.
- `:215` writes `# scenario_title: ${title}` without stripping newlines. Titles allow `\n` (`contracts/scenario.schema.json`: only a length limit), so `Stress\n=HYPERLINK(…)` becomes its own CSV row.
- Titles can be set by any `simulate` token holder.

**Fix** (agent F)
- Guard any cell that does not fully match `^-?\d+(\.\d+)?([eE][+-]?\d+)?$` and starts with one of `= + - @ \t \r`.
- Apply `.replace(/[\r\n]+/g, ' ')` to every header value, as is already done for assumptions.
- Optionally reject control characters in scenario titles server-side (agent C/E).

### R4-17 — Promotion labelling is misleading (VERIFIED by reading and by the R4-04 run)

- `ui/src/pages/AcquisitionPage.tsx:332-333` and `api/schemas.py:933` say "nothing is promoted without gates and a human review". T1 promotes automatically: the end-to-end test promotes with no review row.
- Proposals display `ops.proposal.permission_tier`, which is the *declared* tier. The derived or effective tier exists only inside the gate-evidence artifact. A proposal declaring T0 while touching T3 paths is shown as "T0".
- `DB_DECISION` maps `needs_review` to `blocked`, which is the same label as kill-switch or policy-integrity blocks.

**Fix** (agents F, E, G)
- Correct the copy: "T1 within signed policy scope promotes automatically; other tiers need review".
- Persist and expose the effective tier and the gate outcome (`needs_review` as its own DB value; a migration is needed, so this is for the lead).

### R4-18 — The live-smoke secret is scoped too widely (PLAUSIBLE)

`live-smoke.yml:37` sets `REVA_COMTRADE_API_KEY` in job-level `env`. It is visible to:
- `setup-python`, `setup-uv` and `upload-artifact`;
- `uv sync` (package build backends);
- every test.

**Fix** (agent I): move it to the `env:` of the single "Live provider tests" step.

---

## Fixed or changed during the review

- **Blueprint and Settings mismatch.** `REVA_DB_SSLMODE` was missing, so every service and the pre-deploy failed; `REVA_PROCESS_ROLE` was missing, so the worker required the session secret. OPS-FIX fixed both in the working tree. The remaining gap is R4-02.
- **CSV export of the L2 `sensitivity_range` row.** It previously fell through to `missing: "unsupported value kind …"`. `resultValue` now exports its lower and upper bounds (`ui/src/lib/scenario.ts:194-197`).
- **A transient `UU scripts/e2e_stack.py`** (unmerged index entry) was seen during a concurrent snapshot. It was gone at the end.

## Checked and fine

**Eurostat template**
- Request URLs are `DATA_BASE` (a fixed `https://ec.europa.eu/...`), plus a dataset matching `^[a-z0-9_]{2,64}$`, plus `urlencode` of category codes matching `[A-Za-z0-9_\-]{1,40}` and integer years. There is no host or path injection.
- Manifest hosts must be ⊆ `TEMPLATE_HOSTS`. `make_live_client` uses the spec's hosts through `SafeHttpClient`.
- Huge exponents (`1e60000000`) are rejected by `require_json` (`_finite_float`), so the `format(Decimal,'f')` blow-up is not reachable (VERIFIED).
- The cube is bounded at 200k positions, the status vocabulary is closed, and an empty cube is a blocker.

**Catalogue text and prompt injection**
- TOC titles and inspection labels are control-stripped and length-capped, and are used only for lexical ranking.
- Dataset and category codes are regex-validated before they reach a manifest.
- No LLM is in the loop; `LlmEvidenceExtractor` is a typed `blocked` result.
- Nothing fetched selects hosts, tiers or policies.

**Policy pinning**
- Changed `promotion.yaml`/`budgets.yaml` bytes make the gate return `blocked`.
- `signed_policy` must equal the pinned digest.
- Pins live in a T3 path, and a T1 proposal cannot touch them.
- The proposer cannot pass `pinned_sha256`: it is a constructor argument of the gate, chosen by trusted code.

**Backlog and LLM**
- `max_open_proposals` is enforced inside the proposal-insert transaction, and closed means a pass/fail gate decision or a review.
- The LLM budget is `min(settings, policy)`.

**Entrypoints**
- `os.execvpe` is used with no shell.
- Allowlisted child environment: the owner URL, `DATABASE_URL` and `PG*` are never forwarded, and other components' secrets are dropped. `.env`-sourced secrets are blanked, and a `.env` file is refused in production.
- The worker refuses owner credentials in production.
- Thread variables come from the explicit budget and the cgroup quota, never `os.cpu_count()`.
- DSNs are built with `URL.create`, and errors are redacted.
- One Uvicorn worker; proxy headers are trusted only from private ranges (see R2 on XFF).

**Dockerfile**
- Base images are pinned by digest; the UI is built with `npm ci` from the lockfile and Python with `uv sync --frozen`.
- The runtime runs as non-root UID 10001, has no compilers, and `/app` is read-only except `/app/var`.
- The build CA comes in through a BuildKit secret mount (not stored in a layer).
- `.dockerignore` excludes `.env*`, keys, `.git`, tests and docs.

**Backup and restore**
- Credentials come only from the environment and go to `pg_dump` as `PG*` variables.
- Output is written under `umask 077`, and production requires encryption.
- The dump is consistent with a logical manifest taken from the same exported snapshot.
- Restore refuses a non-empty target and checks the version order of `pg_restore` and `pg_dump`.

**CI**
- All nine action SHAs match their tags (checked with `git ls-remote`).
- `permissions: contents: read`; `packages: write` only on the release job.
- `persist-credentials: false`; no `pull_request_target`; live tests run in a separate scheduled workflow.

**UI**
- No `dangerouslySetInnerHTML`, `innerHTML`, `eval` or MapLibre `setHTML`. Provider strings render as React text nodes and Cytoscape canvas labels.
- No token storage: session via an HttpOnly cookie, `credentials: 'same-origin'`, and an `X-CSRF-Token` header from the `reva_csrf` cookie on unsafe methods.
- No redirect parameter in login, so no open redirect.
- The CSP is `script-src 'self'` with `worker-src blob:`, which is compatible with MapLibre's worker.
- Missing chart points are gaps (`spanGaps: false`). Sensitivity ranges are labelled "not a confidence interval" in the table, `ValueView`, the CSV and the JSON.
- Exports embed the banner, scenario, baseline release, cutoff, model versions, fidelity and origin.

**render.yaml**
- The worker gets no migration URL, session secret, admin hash or API password.
- The worker's S3 credential is separate from the API's.

## Requested changes outside R4's ownership

| Owner | Change |
|---|---|
| Agent G (autonomy + Eurostat) | R4-01 (gate re-derivation, registry-based contract, independent reports); R4-03 generator versioning and stale-template signal; R4-04 rollback hold and eligibility-aware `promoted_connectors`; R4-05; R4-06 handler side; R4-08 guard; R4-09; R4-10; R4-11 (policy edit is T3/maintainer); R4-13; R4-14; R4-15 |
| Lead (`cli/seed.py`, migrations, `settings.py`) | R4-03 `register_connector_version` immutability check; R4-04 latest-decision comparison; R4-05 checked SQL path (`model.record_promotion`); R4-02 role-scoped base-URL rule; R4-17 `needs_review` gate decision value and effective-tier column |
| Agent D (ops) | R4-01 `_all_connectors` name collision; R4-08 `ops.budget_ledger` writer |
| Agent E (API) | R4-06 `DiscoveryRequest` schema and 422; R4-17 docstring |
| Agent F (UI) | R4-07; R4-16; R4-17 copy and tier display |
| Agent I / OPS-FIX (deploy) | R4-02 (Blueprint/validator test); R4-12; R4-18 |
