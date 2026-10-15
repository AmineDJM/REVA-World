# Review of the FX1 database fixes (agent R5, adversarial, read-only)

Scope: FX1's in-place rewrite of `migrations/versions/0001..0007`, `src/reva/adapters/postgres/ddl.py` and
`src/reva/cli/bootstrap.py` (commit `c06463b`, plus the `ops.api_token.principal_kind` follow-up in
`186f0f5`). I checked these against the original findings in `docs/dev/review_core.md` (R-01, R-02, R-04, R-07,
R-08, R-09, R-15, R-17, R-18, R-19, R-26) and against prompt §4, architecture §6.1, §8 and §21. No code was modified.

Method:
- **VERIFIED** findings were reproduced with throwaway tests that use the real login roles against a fresh
  `reva_test_<random>` database (the `tests/conftest.py` `db` fixture). Each test database was dropped afterwards.
- The dev database `reva` was only *read*, in a session with `default_transaction_read_only=on`.
- Scripts and raw output are in
  `/tmp/claude-0/-home-user-REVA-World/a82f628c-2790-54df-b0b4-28930bdc7a97/scratchpad/review5/`
  (`test_r5_governance.py`, `test_r5_temporal.py`, `test_r5_publish.py`, `test_r5_rls.py`,
  `test_r5_membership.py`, `out_*.txt`).
- FX1's own suite, `tests/security/test_db_hardening.py`, passes (38 passed).

**PLAUSIBLE** means the issue was established by reading the code but not reproduced end to end.

## Summary

| ID | Sev | Status | One-line summary |
|---|---|---|---|
| R5-01 | High | VERIFIED | The worker can still approve its own registry state and forge "human" promotions. Gate/promotion functions check only the *format* of identities. |
| R5-02 | Medium | VERIFIED | `reva_sim` can squat a scenario's identity. `sim.create_scenario` then returns a scenario whose spec differs from the caller's. |
| R5-03 | Medium | VERIFIED | Selection and `belief.check_input` disagree on open-ended supports. One such observation makes *every* release of the concept fail. A `-infinity` bound breaks selection outright. |
| R5-04 | Medium | VERIFIED | R-15 is not closed: seven migrations were edited in place under unchanged revision ids. The dev DB reports head `0007_security` but has none of the fixes, and nothing detects this. |
| R5-05 | Med-Low | VERIFIED | `reva_sim` can pre-register `meta.artifact` metadata for predictable hashes. This permanently blocks the worker from registering those bytes, or labels them with the wrong rights. |
| R5-06 | Med-Low | VERIFIED | The worker can DELETE unexpired `ops.session_revocation` rows, which un-revokes stolen sessions. |
| R5-07 | Med-Low | VERIFIED | The release seal is check-then-insert. A concurrent transaction can append an estimate with no inputs to a release as it is published. Release-pinned `world.relation` is not sealed at all. |
| R5-08 | Low | VERIFIED | The R-19 probe (`pg_advisory_unlock`) destroys a caller's session lock. It also writes a server WARNING on every publication (352 in 15 min on the shared cluster). |
| R5-09 | Low | VERIFIED | Bootstrap does not check memberships *held by* runtime roles. On PG16+ `NOINHERIT` is only a default: an explicit grant to a runtime role passes every check. |
| R5-10 | Low | VERIFIED | A non-superuser CREATEROLE owner is implicitly `MEMBER` of `reva_restricted_deletion`, which it creates, so it bypasses the deletion guard without a reviewed grant. |
| R5-11 | Low | VERIFIED | Eligibility scope semantics: a 2-element `years` list is a range, string years never match, `classifications` is still ignored and `ref_year NULL` fails open. |
| R5-12 | Low | VERIFIED | Idempotency namespacing collides when `created_by` contains `:`. The worker can submit jobs as any API principal. |
| R5-13 | Low | VERIFIED | `meta.inserted_in_current_xact` raises (it does not return false) for "future-looking" xmins. Its "fails closed" comment is inaccurate, and the epoch-wrap case is theoretical. |
| R5-14 | Low | PLAUSIBLE | `force_recorded_at` exempts any SECURITY DEFINER code (`current_user` = owner) and silently overwrites values. It should key on `session_user` and reject. |
| R5-15 | Low | PLAUSIBLE | Two concurrent `sim.create_scenario` calls with the same spec: the second fails with a unique violation instead of returning the id. |
| R5-16 | Low | VERIFIED | Residual R-18 scope: `reva_sim` may declare any artifact (for example raw source bytes) an output of a simulation activity, and may use origin `model` (the worker's population activities). |

### Per original finding

| Original | Verdict |
|---|---|
| R-01 | **Partially closed.** Token minting and human reviews are closed (worker INSERT revoked, positive and negative tests pass). Gate results, promotions, eligibility, mappings, model versions, reviewed claims, `review` activities and audit rows remain forgeable (R5-01). |
| R-02 | **Closed for the sequential case.** `publish_release` (estimates before `ops.publication`, one transaction) still works: the positive control published 100 estimates. Not race-safe across transactions; `world.relation` is not covered (R5-07). |
| R-04 | **Closed.** The `origin` column is filled from and checked against the acquisition, the lineage is unique per origin, and `_lineage_head`/selection partition by origin. |
| R-07 | **Partially closed.** Scope matching and years are enforced in the trigger. Classifications are ignored, the encoding is ambiguous, and selection disagrees with the trigger on unbounded supports (R5-03, R5-11). |
| R-08 | **Closed for runtime roles.** `a00_*` fires first on every table that has it (checked from `pg_trigger`). Hazards remain for definer functions (R5-14). |
| R-09 | **Closed.** The xmin seal works under released and nested savepoints (VERIFIED), `entity_kinds` is COALESCEd, and the deferred participant cardinality trigger is present. |
| R-15 | **Not closed** (R5-04). |
| R-17 | **Closed.** Only a SCRAM verifier is sent, errors are sanitised, and FX1's tests pass. Production still has no minimum password length (minor). |
| R-18 | **Mostly closed.** Edge and location policies work. Residuals: R5-05, R5-16, and R5-02 (direct `sim.scenario` INSERT). |
| R-19 | **Closed, with side effects** (R5-08). |
| R-26 | **Closed.** Lease bounds and payload bound are verified by FX1's tests. |

### Questions asked by the lead

- **Trigger ordering, and legitimate backdating paths (vintage replays)?**
  - Nothing legitimate breaks. Vintage replays and parser replays keep the original `retrieved_at`, and select on `source_available_at`. `tests/integration/test_revisions.py:173-174` even asserts that the replay's `recorded_at` is *later*.
  - Logical restore is unaffected because `pg_restore` creates triggers post-data.
  - Only owner-run fixtures can backdate, which is intended.
- **Sealed triggers versus publication order?** Inserting estimates before `ops.publication` is still allowed; this was VERIFIED by a positive publication.
- **Unbounded support with `is_eligible`?**
  - It is not *silently* ineligible. The trigger fails closed but selection fails open, so the whole release aborts (R5-03).
  - No current connector produces NULL ranges: WDI and Comtrade are bounded, and Eurostat 1 January values are instants.
  - Any open-ended or unknown-start `TimeExtent` would trigger it (`repositories.ensure_support`, lines 247-254, writes NULL).
- **RLS bypass via ON CONFLICT DO UPDATE, COPY or functions?** None found.
  - Worker upsert: `permission denied for table context`.
  - Sim `COPY`: `COPY FROM not supported with row-level security`.
  - The only definer function that writes RLS tables is `sim.create_scenario`, which is API-only and intentional.
  - RLS is *absent* on `meta.artifact` and `meta.artifact_inline`. Only `meta.artifact` matters, because sim cannot insert into `meta.artifact_inline` (R5-05).
- **`inserted_in_current_xact` under subtransactions and wraparound?**
  - Correct for subtransactions (VERIFIED): a child xid is greater than the top xid, and `pg_xact_status` reports own committed subtransactions as in progress.
  - Wraparound: R5-13.
- **Advisory-lock probe?** R5-08.
- **Non-superuser CREATEROLE owner on PG16+?**
  - The owner keeps ADMIN on every role it creates (VERIFIED: `admin_option=t, inherit=f, set=f`, `createrole_self_grant=''`).
  - It cannot SET ROLE into them without first granting itself SET. Being able to do that is acceptable, because the owner is strictly more powerful and is removed before startup.
  - It is, however, implicitly `MEMBER` of `reva_restricted_deletion` (R5-10).
  - Re-provisioning roles that another (superuser) owner created needs an explicit `GRANT … WITH ADMIN OPTION`. FX1's test does exactly that; otherwise bootstrap fails cleanly with a `ConfigurationError` (SQLSTATE 42501).
- **Connection limit -1 in local/test?** This is fine. `reva_env` is `Literal["local","test","production"]`, and production defaults to 20 and must be at least 1.
  - Caveat: roles, passwords and limits are cluster-global, so bootstraps of different environments on one cluster overwrite each other, last writer wins. Do not share a cluster between production and anything else.

---

## High

### R5-01 — Worker can still approve its own registry state and forge "human" promotions (VERIFIED)
`0007_security.py:99-110` still grants `INSERT ON ALL TABLES` in seven schemas to `reva_worker`. Only `ops.api_token`,
`ops.proposal_review` and `ops.session_revocation` are revoked.

`ops.record_gate_result` and `model.record_promotion` (`0006_operations.py:324-384`), and the triggers behind them
(`0006:144-167`, `0004:64-85`), validate **identity strings**, never **who called**:
- The gate evaluator name is `'gate-evaluator:' || p_gate_name`, where the caller chooses `p_gate_name`.
- "Independence" means that string differs from `proposal.author`, which the worker also writes.
- The evidence may be *any* verified artifact.
- A promotion `reviewer` may be `human:<anything>`.

The worker can also write the evaluation that a promotion cites: it creates the `evaluation` context, the `model.evaluation` row and its metrics.

Evidence (`test_r5_governance.py`, every write in a rolled-back transaction):
```
worker forged: review activity as human:alice, unscoped eligibility (is_eligible -> True), approved mapping,
  approved model.version, reviewed claim, human audit row
worker recorded promotion 466923dd-… audited as human:alice     (own evaluation, metrics '{}')
worker-authored proposal 8dc85db5-… passed by worker-chosen evaluator, gate 34a5da1c-…
```

Severity: High.
- This is invariant 15 ("no model self-promotion or editing its own policy gates") and check 6 of §4 ("eligible only for … scopes explicitly approved").
- The controls look DB-enforced but are not.
- The realistic actor is not a malicious binary. It is the autonomy code path, which handles untrusted retrieved content (§21: "Embedded prompts cannot grant privileges") inside the worker. Any confused-deputy bug in it can approve sources, mappings, models and promotions with DB credentials it legitimately holds.

Today's writers are:
- seed (`cli/seed.py:221-283`, `_seed_models`, curated cards as `maintainer:`);
- the autonomy promotion path (`autonomy/jobs.py:437-466`, `_append_eligibility`);
- `evaluation/tournament_service.py:1001-1010` (direct `INSERT INTO model.promotion`);
- `evidence/cards.py:161`.

**Fix (concrete design).** It has four parts and needs one new migration (0008) or an in-place edit, if R5-04 is resolved by renumbering.

1. **Unforgeable attribution.** Add `recorded_by name NOT NULL` to `ops.audit`, `ops.gate_result`, `ops.proposal`,
   `model.promotion`, `model.evaluation`, `model.version`, `evidence.eligibility`, `ontology.mapping` and `evidence.claim`.
   Force it in a BEFORE INSERT trigger with `NEW.recorded_by := session_user`. Inside a SECURITY DEFINER function,
   `session_user` is still the calling login, unlike `current_user`.
2. **The worker may only write fail-safe states.** Use restrictive RLS:
   ```sql
   ALTER TABLE evidence.eligibility ENABLE ROW LEVEL SECURITY;
   CREATE POLICY elig_read ON evidence.eligibility FOR SELECT TO reva_api, reva_worker, reva_sim USING (true);
   CREATE POLICY elig_worker ON evidence.eligibility FOR INSERT TO reva_worker
     WITH CHECK (decision IN ('quarantine','retired'));          -- the worker may always fail safe
   -- same pattern:
   --   ontology.mapping   FOR INSERT TO reva_worker WITH CHECK (approval IN ('candidate','rejected'))
   --   model.version      FOR INSERT TO reva_worker WITH CHECK (approval_status IN ('candidate','retired'))
   --   evidence.claim     FOR INSERT TO reva_worker WITH CHECK (status IN ('candidate','rejected','retracted'))
   --   ops.audit          FOR INSERT TO reva_worker WITH CHECK (actor !~ '^(human|gate-evaluator):')
   ALTER POLICY activity_worker_insert ON meta.activity WITH CHECK (origin NOT IN ('simulation','review'));
   REVOKE INSERT ON ops.gate_result, model.promotion FROM reva_worker;
   REVOKE EXECUTE ON FUNCTION ops.record_gate_result(uuid,text,text,text,text,text) FROM reva_worker;
   ```
3. **Two new principals.**
   - `reva_gate` (LOGIN, NOINHERIT) is used by a separate gate-evaluator process: the same image, started with gate credentials, consuming only gate and tournament-decision jobs (§21 allows another service). It alone has `EXECUTE ON ops.record_gate_result` and `model.record_promotion`.
   - In those functions, require `reviewer LIKE 'gate-evaluator:%'`, and require `ev.recorded_by` to be the evaluator login, not the proposer's.
   - `reva_registry` is NOLOGIN and has no members. It owns SECURITY DEFINER approval functions, executable by the worker, that **verify an independent approval** before writing the approved state:
   ```sql
   CREATE FUNCTION evidence.approve_eligibility(p_gate_result uuid, p_cv uuid, p_concept uuid,
                                                p_applicability jsonb, p_policy text) RETURNS uuid
   LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
   DECLARE g ops.gate_result; m bytea;
   BEGIN
     SELECT * INTO g FROM ops.gate_result WHERE id = p_gate_result;
     IF g.decision IS DISTINCT FROM 'pass' OR g.recorded_by <> 'reva_gate' OR g.policy_version <> p_policy THEN
       RAISE EXCEPTION 'eligibility approval requires a passing gate recorded by reva_gate'; END IF;
     -- the proposal manifest must bind exactly this (connector version, concept, applicability)
     SELECT i.content INTO m FROM ops.proposal p JOIN meta.artifact_inline i ON i.artifact_sha256 = p.manifest_sha256
      WHERE p.id = g.proposal_id;
     IF m IS NULL OR convert_from(m,'UTF8')::jsonb -> 'approves' <> jsonb_build_object(
          'connector_version_id', p_cv, 'concept_version_id', p_concept, 'applicability', p_applicability) THEN
       RAISE EXCEPTION 'gate result does not cover this approval'; END IF;
     INSERT INTO evidence.eligibility(..., decision, ..., gate_result_id) VALUES (..., 'eligible', ..., p_gate_result);
     ...
   END $$;   -- + UNIQUE (gate_result_id): one gate pass approves one thing
   ```
   Add equivalent functions for mapping approval, model registration and claim review. `evidence.review_claim(p_claim, p_reviewer)` should be granted to `reva_api` only, like `ops.review_proposal`.
4. **Registry seed runs as the migration owner inside `bootstrap()`.** The `registry/` YAML is reviewed in git, so it is a deploy-time artifact, not a runtime write.
   - `conftest` and `reva seed` would pass `db.owner`.
   - Curated `maintainer:` cards are seeded the same way.

Required code changes:
- `autonomy/jobs.py:_append_eligibility('eligible')` calls `evidence.approve_eligibility`; `retired`/`quarantine` keep direct inserts.
- The tournament runs in the gate process and calls `model.record_promotion`.

Interim, if the separate process is not built for V1: do parts 1 and 2 except the promotion/gate revokes, and route all approvals through the `reva_registry` functions. At minimum, `model.record_promotion` must reject `human:` reviewers when `session_user = 'reva_worker'`. That at least stops worker code forging *human* approval, and makes the remaining self-approval attributable.

## Medium

### R5-02 — `reva_sim` squats scenario identity (VERIFIED)
`GRANT INSERT ON ALL TABLES IN SCHEMA sim TO reva_sim` (`0007:127`) includes `sim.scenario`, and `meta.artifact` is
also sim-insertable (`0007:129`). `sim.check_scenario` (`0004:98-110`) never checks that `spec` matches the
`spec_sha256` bytes. `sim.create_scenario` returns *any* existing row with that sha (`0006:399-400`) without comparing `spec`.

Scenario:
1. Code holding `reva_sim` computes the canonical bytes of a spec the user will submit.
2. It inserts a `meta.artifact` row for their sha, then a `sim.scenario` with that `spec_sha256` but different assumptions.
3. The API's later `create_scenario` returns the forged scenario.
4. The real canonical bytes are never stored, because of the early return.

Evidence (`test_sim_squats_scenario_identity`):
```
sim squatted 40152dc2-… ; API create_scenario returned 40152dc2-… with shock = 999 ; inline canonical bytes stored: 0
```
This breaks §8.4: "Assumptions are immutable. Editing produces a new scenario version."

Fix:
```sql
REVOKE INSERT ON sim.scenario FROM reva_sim;            -- scenarios only via sim.create_scenario (API)
-- sim.check_scenario():
IF NOT EXISTS (SELECT 1 FROM meta.artifact_inline i WHERE i.artifact_sha256 = NEW.spec_sha256
               AND convert_from(i.content, 'UTF8')::jsonb = NEW.spec) THEN
  RAISE EXCEPTION 'scenario spec does not match its verified canonical bytes';
END IF;
-- sim.create_scenario(): serialise and compare on reuse
PERFORM pg_advisory_xact_lock(hashtextextended('scenario:' || sha, 0));
SELECT id INTO sid FROM sim.scenario WHERE spec_sha256 = sha;
IF sid IS NOT NULL THEN
  IF NOT EXISTS (SELECT 1 FROM sim.scenario WHERE id = sid AND spec = p_spec) THEN
    RAISE EXCEPTION 'scenario % exists with a different spec', sid; END IF;
  RETURN sid;
END IF;
```
The advisory lock also fixes R5-15.

### R5-03 — Selection and the input trigger disagree on unbounded supports; one row blocks every release (VERIFIED)
The two sides compute the reference year differently:
- `belief.check_input` (`0005:96-103`) passes `0` when `valid_range` is NULL, lower-unbounded or infinite, so years-scoped decisions fail closed.
- `ELIGIBLE_SQL` (`belief/selection.py:70-71`) passes `EXTRACT(year FROM lower(valid_range))`, which is NULL for those supports.
- `evidence.is_eligible` treats `ref_year NULL` as "skip the years scope" (`0002:220`), which fails open.

Selection therefore picks the observation, and the trigger then aborts the whole publication transaction. All seeded decisions are years-scoped (`registry/mappings/*.yaml`). For a `[-infinity, …)` support, the selection query itself errors.

Evidence:
```
selection (ref_year NULL => years scope skipped) selects it: ['current']
belief.check_input (ref_year 0 => fail closed): observation 96de558a-… is not eligible for concept …
-- test_r5_publish.py, own DB:
positive control: published 100 estimates, seq 1
after one open-ended FRA observation, the whole release fails: ProgrammingError observation 5ca3adaa-… is not eligible …
eligible_latest with one -infinity support: cannot convert infinity to integer
```

Impact:
- One open-ended or unknown-start observation, which `ensure_support` writes as NULL (`repositories.py:247-254`), stops all belief publication for that concept until a reviewed deletion.
- Here R-07's fail-closed rule becomes fail-loud for the whole release.

Fix: use one definition of the reference year in both places, and remove the fail-open NULL path.
```sql
CREATE FUNCTION world.support_ref_year(sp uuid) RETURNS integer LANGUAGE sql STABLE AS $$
  SELECT CASE WHEN s.valid_range IS NULL OR lower_inf(s.valid_range) OR NOT isfinite(lower(s.valid_range))
              THEN 0 ELSE extract(year FROM lower(s.valid_range) AT TIME ZONE 'UTC')::int END
  FROM world.support s WHERE s.id = sp $$;
-- belief.check_input: ref_year := world.support_ref_year(o.support_id);
-- evidence.is_eligible: replace "ref_year IS NULL OR" by "COALESCE(ref_year, 0)" semantics (no skip).
```
In `selection.py`, call `evidence.is_eligible(…, world.support_ref_year(v.support_id))`. Grant `EXECUTE` on the new function to all three roles.

If open-ended supports must be publishable, add an explicit applicability form such as `"years": "any"` instead of relying on NULL.

### R5-04 — R-15 is not closed: seven migrations rewritten in place, and the dev DB silently drifted (VERIFIED)
FX1 documented a policy ("no database had been deployed", `0004:3-10`) instead of renumbering. But revision ids
`0001_kernel` … `0007_security` are unchanged, so any database created before `c06463b` reports "at head" and is never upgraded.

Evidence (dev DB, read-only):
```
alembic_version = 0007_security
evidence.observation.origin column: 0      (R-04)
triggers a00_server_recorded_at / release_sealed: 0      (R-08 / R-02)
functions record_gate_result / inserted_in_current_xact: 0
```
`reva bootstrap` against the dev DB is a no-op. Current code, such as `_lineage_head` querying `o.origin`, will fail there, and none of the security fixes apply.

Fix:
1. Bump the revision ids while keeping the mandated file names: `revision = "0001_kernel_r2"` … `"0007_security_r2"`, chained. A stale database then fails loudly in bootstrap with "Can't locate revision identified by '0007_security'".
2. Add a schema contract check to `verify_ownership`. Add `CREATE FUNCTION meta.schema_contract() RETURNS text … 'fx1-2026-10-15'` in 0007, and have bootstrap refuse to start when it is missing or different.
3. Add the CI guard from R-15: fail when a migration file present on `main` changes.
4. The lead must decide how to migrate the dev DB's live data: export, then re-ingest from raw artifacts.

## Medium-Low

### R5-05 — `reva_sim` pre-registers artifact metadata; the worker can then never register those bytes (VERIFIED)
`meta.artifact` has no RLS, and the sim may insert any row (`0007:129`). `register_artifact` uses `ON CONFLICT DO NOTHING`
(`repositories.py:49-65`), so the first writer's `byte_size`, `media_type`, `rights` and `schema_version` win.

A wrong `byte_size` makes every later location insert fail in `meta.check_location_size`. Rows are immutable, so the failure is permanent without the deletion process.

Many artifacts have predictable hashes:
- the ontology and model manifests (`store_json_artifact(reg.manifest())`);
- public raw responses.

Evidence: `worker registration of the real bytes after sim pre-squat: artifact 5422fdcd… size mismatch (1 vs 55)`.
With a correct size, the squat instead silently keeps sim-chosen `rights` and `media_type` (`test_sim_artifact_metadata_first_writer_wins`).

Fix:
- Enable RLS on `meta.artifact`. Give the sim a policy limited to its own schema versions: `simulation-result/%`, `evaluation-manifest/%`, `forecast-%`, `replay-manifest/%`, `model-manifest/%`. The worker and API get permissive policies.
- In `register_artifact`, after the insert, `SELECT byte_size, media_type, schema_version, rights` and raise a typed integrity error on mismatch, instead of proceeding.

### R5-06 — The worker can un-revoke live API sessions (VERIFIED)
`GRANT DELETE ON … ops.session_revocation TO reva_worker` (`0007:112`) is unrestricted. The pruning job needs only expired rows.
The API's in-memory cache (`api/auth.py:286-315`) hides this only until a restart or on another instance.

Evidence: `worker deleted unexpired revocation rows: 1`.

Fix:
```sql
ALTER TABLE ops.session_revocation ENABLE ROW LEVEL SECURITY;
CREATE POLICY rev_read ON ops.session_revocation FOR SELECT TO reva_api, reva_worker USING (true);
CREATE POLICY rev_api_insert ON ops.session_revocation FOR INSERT TO reva_api WITH CHECK (true);
CREATE POLICY rev_worker_prune ON ops.session_revocation FOR DELETE TO reva_worker
  USING (expires_at < clock_timestamp());
-- (the worker already has SELECT on ops.*; rev_read keeps it visible under RLS)
```

### R5-07 — The release seal is check-then-insert; release-pinned relations are unsealed (VERIFIED)
`belief.reject_if_published` (`0003:227-244`) checks `EXISTS (ops.publication …)` without any lock.

Scenario:
1. Transaction B inserts an estimate into a committed but unpublished release.
2. Transaction A publishes the release. `check_publication` cannot see B's uncommitted row, so its "every estimate has inputs" check passes.
3. B commits.

Evidence:
```
published release b1b76790-… estimates now 2 of which without inputs: 1
```
`publish_release` creates the release and publishes it in one transaction, so today's code is not exposed. Any future "stage then publish" job would be.

Separately, `world.relation.release_id` (the release-pinned graph read by `world/queries.py`) has no seal trigger, so edges can be added to a published release (latent: no writer yet).

Fix:
- In `belief.reject_if_published`, run `PERFORM pg_advisory_xact_lock_shared(hashtextextended('release:'||rel,0))` before the EXISTS check.
- In `ops.check_publication`, run `PERFORM pg_advisory_xact_lock(hashtextextended('release:'||NEW.release_id,0))` before validating. Each trigger statement takes a fresh snapshot, so the waiter then sees the committed rows.
- Attach the same trigger to `world.relation` when `release_id IS NOT NULL`.

## Low

### R5-08 — The publication lock probe has side effects (VERIFIED)
`ops.check_publication` calls `pg_advisory_unlock(7262011)` (`0003:174-189`):
- If the caller holds that key at session level, for any reason, the hold is released and then the transaction raises. Rollback does not restore session locks.
- On the normal path it writes `WARNING: you don't own a lock of type ExclusiveLock` to the server log on every publication.

Evidence:
```
session lock rows before: 1 after failed publication + rollback: 0
docker logs reva-db --since 15m | grep -c "don't own a lock"  ->  352
```
Fix: drop the probe and the `client_min_messages` juggling. Keep the `pg_locks` existence check (the lock was held before the identity was allocated) and add:
```sql
PERFORM pg_catalog.pg_advisory_xact_lock(7262011);   -- re-entrant for this backend; now held until commit
```
This holds whether or not the caller also holds a session lock, so sequence order equals commit order, with no WARNING and no release.

### R5-09 — Runtime role memberships are not verified (VERIFIED)
`verify_runtime_role_attributes` and `verify_ownership` (`bootstrap.py:228-278`) check attributes and only membership in
`reva_restricted_deletion`. On PG16+ a role's `NOINHERIT` is only the default for new grants:
`GRANT x TO reva_worker WITH INHERIT TRUE, SET TRUE` passes every check.

Evidence: `reva_worker rolinherit = False ; USAGE (inherits) / SET on probe: (True, True) ; bootstrap verify_* checks: passed`.
If `x` were the migration owner, the worker could `SET ROLE` to it and bypass everything.

Fix: in `verify_ownership`, fail when `SELECT … FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member WHERE r.rolname = ANY(runtime roles)` returns any row. Also fail when a runtime role holds `admin_option` on another role.

### R5-10 — The CREATEROLE owner is implicitly a member of `reva_restricted_deletion` (VERIFIED)
- On a managed cluster, 0001 runs as the CREATEROLE owner, which creates `reva_restricted_deletion` and receives an ADMIN-only grant.
- `pg_has_role(owner, role, 'MEMBER')` is then `true` (VERIFIED: `(MEMBER, USAGE, SET) = (True, False, False)`).
- `meta.reject_mutation` (`0001:37-47`) tests `MEMBER`, so the owner deletes scientific history by setting any ticket GUC, without a deliberate grant.
- A superuser always passes, too.

The owner can drop triggers anyway, so this is not an escalation, but the "separately reviewed" control is illusory.

Fix:
- Require an explicit `SET ROLE reva_restricted_deletion`: `IF current_user = 'reva_restricted_deletion' AND ticket …`.
- Grant DELETE on scientific tables to that NOLOGIN role only.
- Optionally validate the ticket against a `meta.deletion_ticket` row inserted by a reviewer.

### R5-11 — Eligibility applicability encoding (VERIFIED)
`is_eligible` (`0002:208-230`) has four gaps:
- A two-element `years` is always a range: `[2019, 2023]` approves 2021 (`{2019: True, 2021: True, 2023: True}`). This is ambiguous with Eurostat's `years: manifest["request_parameters"]["time"]` lists (`autonomy/jobs.py:451`).
- With three or more entries, string years never match: `'["2019","2020"]' @> to_jsonb(2020)` is `false`.
- `classifications` (seeded `[H4,H5,H6]` for Comtrade) is still ignored, so R-07's H3 case is open.
- `ref_year NULL` skips the years scope (see R5-03).

Fix:
- Use explicit forms, `{"years": {"from": 2020, "to": 2024}}` or `{"years": {"in": [2020, 2023]}}`, normalised to integers by seed and autonomy.
- Make unknown *scope* keys non-matching, keeping an allowlist of informational keys: `mapping_key`, `gate_result_id`, `rollback_sha256`, `superseded_by`, `subjects`, `geo`.
- Add a `classification` argument, fed from `source_flags->>'classificationCode'`.

### R5-12 — Job namespacing and principal spoofing (VERIFIED)
`k := 'api:' || created_by || ':' || type || ':' || key` (`0006:222`) is not injective when `created_by` contains `:`.
Token actors are `token:<label>`, with labels like `human:alice`.

Evidence: principal `token:alice` (key `m:simulation:K`) and principal `token:alice:simulation:m` (key `K`) received the **same job id**. The labels are admin-chosen, so the risk is low.

The worker is also granted `ops.submit_job` (`0007:118`) and may pass any `p_created_by`, for example `human:admin`.

Fix:
- Encode unambiguously: `'api:' || length(p_created_by) || ':' || p_created_by || …`, or reject `:` in `p_type`, or store `(created_by, client_key)` in separate unique columns.
- Give the worker its own `ops.enqueue_internal(...)`, which forces `created_by := 'worker:' || session_user`.

### R5-13 — `meta.inserted_in_current_xact` at the edges (VERIFIED for the error path)
`pg_xact_status` raises `transaction ID … is in the future` for a widened xid beyond `nextXid`. That is exactly what a
frozen tuple's raw xmin looks like when it is 2^31 to 2^32 xids old. The comment (`0001:69-77`) says the function fails closed with `false`.
- It still fails closed in effect (the insert is rejected), but with a misleading error.
- In the exact-epoch-wrap case, a frozen raw xmin could equal a concurrently running newer xid and return `true`. This needs about 4·10⁹ xids and is theoretical.

Fix: wrap the status call (`BEGIN … EXCEPTION WHEN others THEN RETURN false; END`), and correct the comment.

### R5-14 — `force_recorded_at` keys on `current_user` and overwrites silently (PLAUSIBLE)
`0001:59-67` and `0003:214-223` exempt the table owner by `current_user`. Inside any SECURITY DEFINER function (owned by
the migration owner), `current_user` is the owner, so a future definer function that passes a caller-supplied
`recorded_at`/`published_at` would reopen R-08. None does today.

Values supplied by non-owners are also silently replaced, so callers and tests never learn their value was ignored.

Fix:
- Exempt only when `session_user` owns the table.
- Raise when a non-owner supplies a value more than one second away from `clock_timestamp()`: `RAISE … 'recorded_at is server-generated'`.

### R5-15 — Concurrent duplicate `sim.create_scenario` (PLAUSIBLE)
Between the SELECT and the INSERT, a second caller with the same spec blocks on `meta.artifact`, then fails with a unique
violation on `spec_sha256` instead of returning the id. Fixed by the advisory lock in R5-02.

### R5-16 — Residual provenance scope for `reva_sim` (VERIFIED)
`activity_output_sim_insert` (`0007:196-198`) checks only the *activity* origin. The sim may therefore declare raw source bytes the output of a simulation activity.

Evidence: `producers of the raw source artifact: ['source', 'simulation']`.

Origin `model` is also shared with the worker's `population.materialise` activities (`population/service.py:126`).

Fix:
- Add `AND NOT EXISTS (SELECT 1 FROM meta.activity_output o JOIN meta.activity a ON a.id = o.activity_id WHERE o.artifact_sha256 = activity_output.artifact_sha256 AND a.origin NOT IN ('simulation','evaluation'))`.
- Remove `'model'` from the sim's activity origins, as R-18 suggested.

---

## Checked and fine
- **SECURITY DEFINER hygiene.**
  - All nine definer functions pin `search_path = pg_catalog, pg_temp`.
  - None is executable by PUBLIC.
  - EXECUTE grants match the role matrix: API gets `submit_job`, `request_cancel`, `review_proposal` and `create_scenario`; worker gets the lease functions, `submit_job`, `record_gate_result` and `record_promotion`; sim gets the lease functions only.
  - Note that `pg_default_acl` is empty: functions added by a later migration get PUBLIC EXECUTE unless revoked explicitly. Add `ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC` in 0007.
- **Argument validation** in the new functions covers NULLs, enum values, sizes and verified evidence. NULL reviewer or policy is caught by the trigger or a NOT NULL constraint.
- **Trigger order.** `a00_server_recorded_at` / `a00_server_published_at` sort first on all 11 tables that carry them alongside other BEFORE INSERT triggers (`pg_trigger`). No trigger reads `NEW.recorded_at`.
- **R-04 lineage keying**, the R-09 seal under released and nested savepoints, R-17 sanitisation, and the R-26 bounds.
- **Non-superuser bootstrap** works when the owner holds ADMIN on the runtime roles, and the provisioning retry and savepoint logic is sound. Advisory locks are per-database, which FX1's docstring acknowledges.
