# Review: API, simulation and operations (agent R2, adversarial, read-only)

Scope:
- `src/reva/api/**`
- `src/reva/simulation/**` (including `spec.py`)
- `src/reva/models/economic/crude_exposure.py`
- `src/reva/adapters/postgres/queue.py`
- `src/reva/operations/**`
- `src/reva/cli/{main,demo}.py`

Reviewed against prompt §7, §9 and §11, and architecture §16, §17, §19 and §21. The review was done on the working tree
of 2026-10-14 (HEAD `633e3d5`, plus uncommitted changes by other agents). Several files changed while the review
was running. Every finding below was re-checked against the current text. Findings already reported in
`docs/dev/review_core.md` (R-01 to R-28) are not repeated; they are cited where they interact with a finding here.
No code was modified.

## Method

**VERIFIED** means the finding was reproduced. The DB tests ran against a fresh scratch database: the `tests/conftest.py`
`db` fixture, `reva_test_<random>`, dropped afterwards. The dev DB `reva` was never touched. The other tests were offline.
**PLAUSIBLE** means the finding was established by reading the code only.

Scripts and raw output are in `/tmp/claude-0/-home-user-REVA-World/a82f628c-2790-54df-b0b4-28930bdc7a97/scratchpad/review2/`:

| File | Contents |
|---|---|
| `test_r2_nodb.py` | Offline tests |
| `test_r2_db.py` | DB tests, part 1 |
| `test_r2_db2.py` | DB tests, part 2 |
| `test_r2_db3.py` | DB test for the outbox stall |
| `test_r2_db4.py` | DB test for the L2 `GET /v1/simulations` 500 (R2-20) |
| `out.txt`, `out3.txt`, `out4.txt` | Raw output |

To run them:

```
uv run pytest <file> -p no:cacheprovider --rootdir /home/user/REVA-World -s
```

The scratch `conftest.py` imports the repository fixtures. It also patched the then-broken bootstrap; see "Out of scope" below.

Severity uses the same scale as `review_core.md`:
- **High**: breaks a §24.1 invariant or a security/privilege boundary on a normal code path.
- **Medium**: needs a race, a restart or a specific request sequence, or is a scientific or labelling defect with limited reach.
- **Low**: hardening or a latent defect.

## Summary

| ID | Sev | Status | Area | One-line summary |
|---|---|---|---|---|
| R2-01 | High | VERIFIED | worker / sim | A SIGTERM (every deploy) permanently marks a running simulation job `cancelled` instead of re-queuing it; the evaluation handler has the same flaw. |
| R2-02 | High | VERIFIED | idempotency | The client's `Idempotency-Key` shares one global namespace with internal keys. A `simulate`-scope token can suppress scheduled ingests and stall outbox invalidation and belief publication. |
| R2-03 | Medium | VERIFIED | API envelope | `GET /v1/simulations/{job}` for a succeeded job that reused a prior run shows no run and no results, and the warning "not_finished". |
| R2-04 | Medium | VERIFIED | API honesty | `/v1/timeseries` shows a retracted observation as the current value, with no flag and no warning. |
| R2-05 | Medium | VERIFIED (fix in flight) | sim science | L2 attached the grid's min/max to the fixed-quantity (L1) value as its "uncertainty" even when the range excluded that value. A working-tree fix (R3-03) is in progress, but it causes R2-20. |
| R2-20 | Med-High | VERIFIED (working tree) | API contract | The in-flight R2-05 fix stores `value.kind="sensitivity_range"`, which the API's `TypedValue` union rejects, so `GET /v1/simulations/{id}` returns 500 for every L2 run. |
| R2-06 | Medium | VERIFIED | sim cache | The L1/L2 run idempotency key ignores the model manifest. After a model-code change, L1 silently returns the old run while L0 reports a cache miss. |
| R2-07 | Medium | VERIFIED | authz | Job cancellation is not scoped by job type. A `simulate`-only token can cancel ingest, publication or discovery jobs. |
| R2-08 | Med-Low | VERIFIED | queue | Capped priority aging does not prevent starvation. The docstring's bound ("always overtakes after 200 min") is false. |
| R2-09 | Med-Low | VERIFIED | sessions | Logout revocation is in-process memory only. A logged-out cookie is valid again after any restart or redeploy, for the rest of its 8 h TTL. |
| R2-10 | Low-Med | VERIFIED | auth DoS | The login rate limiter never evicts non-empty buckets and rescans 5,000 keys on each attempt once it holds 10,000 clients: memory is unbounded and every attempt costs O(n). |
| R2-11 | Low | VERIFIED | API | A well-formed but mistyped keyset cursor returns HTTP 500 `database_error` with a logged stack trace, instead of 400. |
| R2-12 | Low | PLAUSIBLE | authz | Any API token whose label starts with `human:` is accepted as a human proposal reviewer, and `human:admin` is indistinguishable from the session admin. |
| R2-13 | Low | PLAUSIBLE | info leak | A job's `last_error` stores `str(exception)`. For SQLAlchemy errors that includes SQL text and bound parameters, and every `read` token can see it. |
| R2-14 | Low | PLAUSIBLE | queue | During a long belief publication, the heartbeat thread blocks on the job-row lock that the publication holds, so leases can expire and jobs are re-run. |
| R2-15 | Low | PLAUSIBLE | API | Two concurrent identical `POST /v1/scenarios` race on `spec_digest UNIQUE`; the loser gets a 500. |
| R2-16 | Low | PLAUSIBLE | demo CLI | Uses the host clock for the cutoff, has no `--origin`, ignores cancellation and mixes results across runs. |
| R2-17 | Low | PLAUSIBLE | scheduler | Slot-key text depends on the session TimeZone. On a key conflict the scheduler silently accepts a job of another type (see R2-02). |
| R2-18 | Low | PLAUSIBLE | API | `/v1/query` accepts naive `as_of` for `beliefs` and `relations` (only `timeseries` checks it). |
| R2-19 | Low | PLAUSIBLE | API honesty | `/v1/provenance/sim_result/{id}` returns scenario values with `modality=metadata` and without the scenario banner. |

Out of scope, reported for the owners:
- **Bootstrap bind-parameter bug (VERIFIED).** It was fixed in the working tree while this review ran.
- **Broken `scenario`/`run` provenance links.** These were fixed concurrently.

Details of both are under "Out of scope / fixed during the review".

---

## High

### R2-01 — SIGTERM turns a running simulation into a permanently cancelled job (VERIFIED)

**Code involved**
- `operations/worker.py:300-301`. During shutdown, `_ctx_heartbeat` returns `"cancel_requested"`. This is the only signal handlers have, and it is the same value as a user cancellation.
- `simulation/jobs.py:98-106`. When `ctx.is_cancelled()` is true, the handler itself calls `ctx.complete_in(conn, "cancelled", …)`, which is terminal in the DB, and returns. The worker then sees `completed_in_handler` and does nothing more.
- `operations/worker.py:402-404`. The return-value path has the same flaw: `{"status": "cancelled"}` always becomes `queue.finish_cancelled`, with no shutdown check. The `JobCancelled` branch at `:384-392` has that check.
- `evaluation/tournament_service.py:786,794` returns `{"status": "cancelled"}`. It is now wired as the `evaluation` handler (`operations/jobs.py:53`), so evaluation jobs lose work the same way.

**Failure scenario**
1. Render sends SIGTERM on every deploy.
2. Every simulation that is between `plan_run` and publication is finalised as `cancelled`, with `result_ref={"published": false}`.
3. Nobody requested cancellation. The job is never retried, and the user sees a cancelled run.

The worker docstring and spec §7 promise the opposite: "Interrupted jobs are released back to the queue without consuming an attempt".

**Evidence** (`test_sigterm_during_simulation_marks_job_cancelled`). The test uses the real `Worker` and the real `handle_simulation`, and calls `request_stop("SIGTERM")` right after `plan_run`:
```
worker outcome: completed_in_handler | job status: cancelled | result_ref: {'published': False, 'scenario_id': '…'}
```

**Fix**

1. Handlers must never finalise a cancellation themselves. They signal it and let the worker decide. In `simulation/jobs.py`, replace lines 98-106 with:
   ```python
   if ctx.is_cancelled():
       from reva.operations.jobs import JobCancelled
       raise JobCancelled("cancelled before publication; nothing was published")
   ```
2. In `operations/worker.py`, make every cancellation path shutdown-aware. `queue.release()` already re-reads the DB status under the row lock and maps a real `cancel_requested` to `cancelled`, so during shutdown it is always correct to call it:
   ```python
   except JobCancelled as e:
       try:
           if self._shutdown.is_set():
               status = queue.release(self.engine, job, ctx.checkpoint, "worker_shutdown")
               return done("released" if status == "queued" else status, None, ack)
           queue.finish_cancelled(...)
   ...
   if res.get("status") == "cancelled":
       if self._shutdown.is_set():
           status = queue.release(self.engine, job, ctx.checkpoint, "worker_shutdown")
           return done("released" if status == "queued" else status, None, ack)
       queue.finish_cancelled(...)
   ```
3. Optionally make the signals distinct: `_ctx_heartbeat` returns `"shutdown_requested"`, and `JobContext.is_cancelled()` treats both values as "stop".
4. Add a regression test equivalent to the scratch test above (in agent D's `tests/integration/test_worker*.py`).

### R2-02 — API idempotency keys collide with internal keys: low-scope tokens can block ingest scheduling and belief publication (VERIFIED)

**Code involved**

`ops.submit_job` (`migrations/versions/0006_operations.py:194-221`) stores the client-supplied `Idempotency-Key` verbatim in the globally unique `ops.job.idempotency_key`. `api/routes/sim.py:62-75` passes it through unchanged. The deps regex allows any printable ASCII, including `:`. Internal producers use the same column with predictable keys:

| Producer | Key format | Behaviour when the key is already taken |
|---|---|---|
| Scheduler (`operations/schedules.py:50,83-95`) | `schedule:{key}:{slot.isoformat()}` | `ON CONFLICT DO NOTHING`, then takes whatever job owns the key; it never checks the job type or payload. |
| `enqueue_publish_beliefs` (`operations/jobs.py:136-142`) | `publish_beliefs:` + `logical_hash({"job_type": "publish_beliefs", "payload": {"origin": …, "through_outbox_id": N}})` | `queue.enqueue` raises "idempotency key reused with a different request". |

Outbox ids are monotonic and the hash is public code, so these keys can be computed in advance.

**Failure scenario**

A token with only `read` + `simulate`, or a buggy client, posts `POST /v1/replay` with `Idempotency-Key` set to one of these keys:

1. **Scheduler key.** The scheduler treats the replay job as the slot's ingest job. The ingest never runs and the schedule advances.
2. **`publish_beliefs` key.**
   - `process_outbox` raises on every tick and rolls back the whole batch. Invalidation, dirty nodes and publication for every later event behind it stall.
   - This lasts until a newer observation event changes `through_outbox_id`. An attacker can pre-claim a whole range of future ids.
   - `handle_ingest` also fails after it has already published its data.

This crosses the scope boundary: `simulate` must not control ingestion or publication (§21 "submit/cancel authorised jobs"). It also misimplements §7, which says the key covers job type plus immutable input/spec digests.

A related effect: two different principals who use the same key get each other's job.

**Evidence**

`test_api_idempotency_key_preempts_scheduler_slot`:
```
scheduler slot schedule:r2-wdi:2026-10-14T23:33:01+00:00 -> created False job_type replay
```
and no ingest job exists for that slot.

`test_api_idempotency_key_blocks_publish_beliefs`:
```
publish_beliefs enqueue after pre-claim: idempotency key reused with a different request
```

`test_outbox_stalls` (`out3.txt`):
```
tick 0: process_outbox failed: ValidationProblem: idempotency key reused with a different request
tick 1: … tick 2: …
event still unprocessed: True
```

**Fix** (new migration, plus API and scheduler changes)

1. Namespace API keys inside the DB function, so that no caller of `ops.submit_job` can reach internal keys:
   ```sql
   -- ops.submit_job
   k := 'api:' || encode(sha256(convert_to(p_created_by, 'UTF8')), 'hex') || ':' || p_type || ':' || p_idempotency_key;
   INSERT … VALUES (…, k, …) ON CONFLICT (idempotency_key) DO NOTHING RETURNING id INTO jid;
   IF jid IS NULL THEN
     SELECT id INTO jid FROM ops.job WHERE idempotency_key = k AND job_type = p_type AND payload = p_payload; …
   ```
2. Make internal producers refuse keys that begin with `api:`.
3. Make the scheduler verify the conflicting row, as `queue.enqueue` already does: `SELECT id, job_type = :t AND payload = CAST(:p AS jsonb) AS same`. If it is not the same, log an error and do not advance `next_due_at`.
4. `enqueue_publish_beliefs` and `process_outbox` must not let a foreign row abort the batch: catch `ValidationProblem` for that single enqueue and record an audit or alert.

---

## Medium

### R2-03 — A succeeded simulation job that reused a prior run reports "not finished" with no results (VERIFIED)

**Code involved**
- `simulation/jobs.py:112-114`. When `sim.run` already has the run key, the job completes `succeeded`, with `result_ref.run_id` set to the earlier run and `idempotent_replay: true`.
- `api/routes/sim.py:297` looks the run up only by `job_id = :i OR id = :i`, so for the second job it finds nothing. The response then has `run=null` and `results=[]`, and adds the warning `not_finished` ("No outcome recorded yet", `:350`), while `job.status == "succeeded"`.
- This happens whenever a second `POST /v1/simulations` with a different Idempotency-Key targets the same scenario and fidelity. Re-clicking "Run" in the UI does exactly that.

**Evidence** (`test_second_simulation_job_shows_not_finished`):
```
f0b33c0d job: succeeded run: True  results: 12 warnings: ['fixture_data', 'scenario_context']
123b1998 job: succeeded run: False results: 0  warnings: ['fixture_data', 'scenario_context', 'not_finished']
```

**Fix** (`api/routes/sim.py:get_simulation`):
1. After the lookup, add:
   ```python
   if run is None and job and job["status"] == "succeeded" and (job["result_ref"] or {}).get("run_id"):
       run = conn.execute(text("SELECT * FROM sim.run WHERE id = :r"),
                          {"r": job["result_ref"]["run_id"]}).mappings().one_or_none()
   ```
2. Add a warning `idempotent_replay` naming the run's original job.
3. Also use `result_ref.run_id`, not `ORDER BY recorded_at DESC LIMIT 1`, whenever it is present.

### R2-04 — Timeseries presents a retracted observation as the current value (VERIFIED)

**Code involved**
- `api/routes/evidence.py:199` (`timeseries_response`) takes `head = lin[-1]`, the highest revision visible at `as_of`.
- It never reads the `retracted` column that `OBS_SQL` already computes.
- `TimeseriesPoint` has no `retracted` field, and no warning is added.

`/v1/observations` does warn (`retracted_records`), so the two endpoints disagree. This breaks §11 ("Show source flags…"; a retraction is the strongest source flag). It is also inconsistent with belief selection, which excludes retracted revisions (review_core R-27).

**Evidence** (`test_retracted_head_shown_in_timeseries`). After inserting an `evidence.observation_retraction` for FRA GDP 2023:
```
after retraction: value {'kind': 'decimal', 'value': '3056250648138.29', …} keys [… no 'retracted' …] warnings ['fixture_data']
```

**Fix**
1. Add `retracted: bool` (and `retraction_reason`) to `TimeseriesPoint`. Set it from `head["retracted"]`, and add the `retracted_records` warning when any head is retracted.
2. Decide on one semantic for both views and document it. Either:
   - (a) show the retracted head flagged, with value kept for audit; or
   - (b) mirror `ELIGIBLE_SQL` and choose the latest non-retracted revision, with a `superseded_revision_selected` flag.

   Never show it silently.

### R2-05 — L2 attaches a sensitivity range that can exclude the value it qualifies (VERIFIED)

**Code involved**
- `simulation/engine.py:205-211`. In an L2 run, the fixed-quantity row (`variant="L1"`) gets `uncertainty = sensitivity_range(min/max over the grid variants only)`.
- The grid need not contain ε=(0,0). The schema and `validate_scenario_spec` only require a non-empty grid within the envelope. When it does not contain (0,0), the L1 value lies outside its own "range".
- The row is also labelled `status=quantified`, which contradicts `explain()`: "Unquantified … L2 min/max … is a sensitivity range". A UI that draws the band around the point shows a band that does not contain the point.

The repository test `test_l2_sensitivity_range` misses this because its grid includes (0,0).

**Evidence** (`test_l1_row_range_can_exclude_l1_value`; M=1000, p=1.25, grid ε_M ∈ {0.5, 1.0}):
```
L1 value 250.00 range 397.5424859373685602557335429570… 562.5000
```

**Fix**
- **Preferred: keep the grid range off the L1 value.**
  1. Emit the L1 row with `_unknown_uncertainty()`, exactly as for an L1 run.
  2. Emit one additional row per output and country, `variant="L2:grid_range"`, with no point value. A `kind: "missing"` value with reason `not_applicable` would need a new reason; alternatively store the range as the value with a `"range"` unit note.
  3. Give that row `uncertainty.kind=sensitivity_range` and `status` "unquantified" (the range is an assumption sweep, not uncertainty about the value).
- **Minimal: make the range contain the point.**
  1. Compute `lo, hi` over `variants + [l1]`.
  2. Say so in `description` ("grid ∪ fixed-quantity case").
  3. Validate `lower ≤ value ≤ upper` in `UncertaintySpec` for interval kinds attached to a point value.

**Status at the end of the review.** The working tree now implements the "preferred" option:
- `engine.py` adds a separate `L2:sensitivity_range` row over grid ∪ {(0,0)};
- the L1 row keeps unknown uncertainty.

That change introduces R2-20.

### R2-20 — The new `L2:sensitivity_range` row breaks `GET /v1/simulations` (VERIFIED against the working tree)

**Code involved**
- The in-flight `simulation/engine.py:_range_value` stores `sim.result.value = {"kind": "sensitivity_range", "lower": …, "upper": …, …}`.
- `api/schemas.py:79` defines `TypedValue = DecimalValue | FloatValue | MissingValue | OtherValue`, discriminated on `kind`. It has no `sensitivity_range` member, and `SimResult.value` is a `TypedValue`.
- Response validation therefore fails, and every L2 simulation becomes an HTTP 500 `internal_error`. L0 cache hits of L2 runs are affected too.

**Evidence** (`test_r2_db4.py`, L2 scenario via the API, job run by the real `Worker`):
```
GET L2 simulation: 500 {"type":"https://reva.invalid/problems/internal_error", … "code":"internal_error", …}
```

**Fix** (agent E, together with agent C):
1. Add the model and include it in the union:
   ```python
   class SensitivityRangeValue(OpenModel):
       kind: Literal["sensitivity_range"] = "sensitivity_range"
       lower: str
       upper: str
       unit_ref: str
       basis: dict[str, Any] = Field(default_factory=dict)
       evaluated_points: int | None = None
       includes_fixed_quantity_point: bool | None = None
       statement: str = Field(description="assumption sensitivity, not a confidence interval")
   TypedValue = Annotated[DecimalValue | FloatValue | MissingValue | SensitivityRangeValue | OtherValue,
                          Field(discriminator="kind")]
   ```
2. Re-export `contracts/openapi.json` and the UI types, and make the UI render it as a range, never as a point.
3. Add an L2 case to `tests/integration/test_api_endpoints.py`.

### R2-06 — L1/L2 run reuse ignores model and implementation changes; L0 and L1 then disagree (VERIFIED)

**Code involved**
- `simulation/jobs.py:41` builds the run key as `sim:{spec_digest}:{fidelity}:{baseline_release_id}`.
- `handle_simulation` returns any prior run with that key (`:112-114`).
- The L0 cache deliberately keys on `model_manifest_logical_sha256`, which includes `implementation_digest()` (`engine.py:47-54`).

After a fix to `crude_exposure.py` or the engine (same `1.0.0` version):
- L0 correctly reports `cache_miss`.
- A fresh L1 request silently returns the result computed by the old code, marked `idempotent_replay: true`.

Non-succeeded outcomes are replayed in the same way. That includes a `missing_input` caused by a mutable `world.alias` lookup in `compile.resolve_subject`. So a missing alias or a model fix is never re-evaluated for that scenario.

This conflicts with §9 ("Every run pins … model manifests") as seen by the requester: the job returns a run pinned to a manifest that is no longer the current model.

**Evidence** (`test_l1_idempotent_replay_survives_implementation_change`):
```
L1 #1 run ec494b1d | L1 #2 (new impl) run ec494b1d replay True | L0 (new impl) cache_miss
```

**Fix**

```python
def default_idempotency_key(spec_digest, fidelity, baseline_release_id, manifest):
    return f"sim:{spec_digest}:{fidelity}:{baseline_release_id}:{logical_hash(manifest)[:32]}"
```

Also replay only prior runs whose status is `succeeded`, or explicitly key failures by `job_id`, so that a failed run can be retried after the cause is fixed.

### R2-07 — Cancellation is authorised by "any job-starting scope", not by the job's type (VERIFIED)

**Code involved**
- `api/routes/system.py:184-193` accepts the cancel if the principal has `simulate` or `ingest`, for any job.
- `ops.request_cancel` has no type check either.

So:
- a `simulate`-only service token can cancel `ingest`, `publish_beliefs`, `discovery`, `gc` and `health` jobs;
- an `ingest` token can cancel users' simulations.

**Evidence** (`test_simulate_scope_can_cancel_ingest_job`):
```
cancel by simulate-only token: 200 {'job_id': '7a88aa10-…', 'status': 'cancelled'}
```

**Fix**
```python
CANCEL_SCOPE = {"simulation": "simulate", "replay": "simulate", "evaluation": "simulate",
                "ingest": "ingest", "discovery": "ingest", "publish_beliefs": "ingest", "health": "ingest",
                "gc": "ingest", "gaps": "ingest", "population": "simulate"}
jt = conn.execute(text("SELECT job_type FROM ops.job WHERE id = :i"), …).scalar_one_or_none()
if jt is None: raise NotFound(...)
need = CANCEL_SCOPE.get(jt)
if need is None or need not in principal.scopes:
    raise Forbidden(f"scope '{need}' required to cancel a {jt} job", required_scope=need)
```
Optionally also pass the allowed job types into `ops.request_cancel(p_job, p_actor, p_allowed_types text[])` (new migration), so the DB enforces the rule too.

---

## Medium-Low

### R2-08 — Capped aging does not prevent starvation (VERIFIED)

**Code involved**

`adapters/postgres/queue.py:177` (`CLAIM_SQL`) orders by `priority + LEAST(cap=200, floor(age/60 s))`. The docstring (`:22-30`) claims that a low-priority job "always overtakes a stream of fresh high-priority jobs after at most 200 min".

That holds only if the competing high-priority jobs are less than about one minute old. Under sustained overload the backlog of high-priority jobs ages too:
- An old p=-100 job tops out at 100.
- A p=100 job that has waited five minutes scores 105.
- So the old job loses for as long as any backlog of higher-priority jobs exists.

§7 requires "Priority with starvation protection".

**Evidence** (`test_capped_aging_does_not_prevent_starvation`; p=-100 aged 600 min vs p=100 aged 5 min):
```
claimed: high (low waited 600 min > 200 min bound)
```

**Fix.** Remove the cap. Uncapped linear aging bounds the wait by the creation-time difference, (Δpriority × aging_seconds). Alternatively, add a hard deadline:
```sql
ORDER BY (created_at < clock_timestamp() - make_interval(secs => :max_wait)) DESC,
         (priority + floor(EXTRACT(EPOCH FROM clock_timestamp() - created_at) / :aging)) DESC,
         created_at, id
```
Then correct the docstring.

### R2-09 — Logout is undone by any restart (VERIFIED)

**Code involved**
- `api/auth.py:229-235` (`logout`) records the session id only in `AuthService._revoked`, an in-process dict.
- The session secret persists across restarts (`REVA_SESSION_SECRET` is required in production), so a cookie that was logged out becomes valid again after every deploy or restart, until its 8 h TTL ends.

This weakens §21 "signed short-lived HttpOnly sessions … audit record". The audit log shows a logout, but the session is still usable.

**Evidence** (`test_logout_revocation_lost_on_restart`):
```
logged-out cookie after restart: 200
```
In the same app the cookie gets 401 (asserted).

**Fix.** Persist revocations. This needs a new migration and the lead's grant:
```sql
CREATE TABLE ops.session_revocation (sid_sha256 text PRIMARY KEY,
  expires_at timestamptz NOT NULL, recorded_at timestamptz NOT NULL DEFAULT clock_timestamp());
GRANT SELECT, INSERT ON ops.session_revocation TO reva_api;
```
- Check it in `_from_session` with a primary-key lookup.
- Purge expired rows opportunistically.
- A simpler alternative: store a persisted `sessions_not_before` timestamp, set by logout, and reject cookies signed before it. This also gives a "log out everywhere" operation.

---

## Low-Medium / Low

### R2-10 — Login rate limiter: unbounded memory and O(n) work per attempt (VERIFIED)

**Code involved**

In `api/auth.py:144-158`, once the limiter holds more than 10,000 keys, every call runs `list(self._hits)[:5000]`. That copies all keys and scans 5,000 of them under the global lock, and it deletes only *empty* deques. A deque is emptied only when its own key is seen again, so idle buckets are never removed.

**Failure scenario**

An attacker rotates source addresses (IPv6 makes this cheap):
- the dict grows without bound;
- every login, including the real admin's, pays O(n) while holding the lock.

There is also no global cap, so distributed guessing can force unlimited Argon2 hashing (CPU).

**Evidence** (`test_rate_limiter_unbounded_growth_and_cost`):
```
keys retained after 30k distinct clients: 30000; total 9.68s
```

**Fix**
1. Keep an `OrderedDict` keyed by client, moved to the end on each hit.
2. On insert, pop from the front while `len > MAX_KEYS` (for example 10,000) or while the oldest bucket's newest timestamp is older than `window`.
3. Add a global bucket (for example 60 attempts per minute in total) checked before Argon2 verification.

The X-Forwarded-For handling itself is sound: uvicorn 0.54 takes the rightmost untrusted address; see "Checked and fine".

### R2-11 — A mistyped cursor returns 500 (VERIFIED)

**Code involved**
- `api/deps.py:48-58` (`decode_cursor`) checks only that the cursor is a list of the right length.
- The values are then cast in SQL (`CAST(:c0 AS timestamptz)`, `CAST(:c1 AS uuid)`). A forged cursor makes PostgreSQL raise 22007 or 22P02, which `_db` maps to 500 `database_error` with `log.exception`, a full stack trace per request.

**Evidence** (`test_bad_cursor_is_500`). `GET /v1/jobs?cursor=<b64 ["not-a-date","x"]>` gives:
```
bad cursor: 500 database_error
```

**Fix**
1. Give `decode_cursor` a per-endpoint schema, `decode_cursor(cursor, (datetime.fromisoformat, UUID))`, that converts each element. Raise `ApiProblem(400, "invalid_cursor")` on `ValueError` or `TypeError`.
2. Also map SQLSTATE class 22 to 400 `invalid_parameter` in `_db`.

### R2-12 — `human:`-labelled API tokens act as human reviewers (PLAUSIBLE)

**Code involved**
- `api/auth.py:97-103` (`Principal.human_reviewer`) treats any token whose free-form label starts with `human:` as a human.
- `reva auth create-token --label` accepts any label, does not require it to be unique, and accepts `human:admin`. That label is identical to the session reviewer identity, so reviews and audit entries become indistinguishable.
- A CI or service token labelled `human:ci` with the `review` scope can approve autonomy proposals. `ops.review_proposal` only checks `LIKE 'human:%'` (review_core R-26).

**Fix**
- Store the principal kind explicitly in a new `ops.api_token.principal_kind` column, with values `human` or `service`, set by a separate `--human <name>` CLI flag.
- Refuse labels that start with `human:` otherwise.
- Record the reviewer as `human:<name>` and the audit actor as `human:<name>@token:<id>`.
- Require label uniqueness for human tokens.

### R2-13 — `last_error` can carry SQL text and parameters to read-scope clients (PLAUSIBLE)

**Code involved**
- `operations/worker.py:343` builds the error document from `getattr(exc, "message", None) or str(exc)`.
- For SQLAlchemy `DBAPIError`, `str(exc)` contains `[SQL: …]` and `[parameters: …]`.
- `redact_text` removes only known secret values and `key=value` patterns. The text is then served by `GET /v1/jobs` and `/v1/jobs/{id}` (`last_error`) to any `read` token.

The impact is low in a single-user V1: payload values are not secrets. It does expose internal schema detail, and `MAX_ERROR_MESSAGE` caps it at 2 KB.

**Fix.** For `DBAPIError`, store only `{"sqlstate": …, "message_primary": diag.message_primary[:300]}`, as `api/errors._db_message` does, and keep the full text in the redacted log only.

### R2-14 — The heartbeat blocks behind the publication's job-row lock (PLAUSIBLE)

**Code involved**
- `belief/publication.py:108` takes `fence(conn)`, i.e. `ops.assert_lease`, which runs `SELECT … FOR UPDATE` on the job row at the start of a long publication transaction.
- `ops.heartbeat` locks the same row, so the worker's heartbeat thread (`worker.py:267-277`) blocks until commit. With `concurrency > 1`, the other jobs' heartbeats block behind it too.

If the publication takes longer than the lease (120 s):
1. after commit, the heartbeat and the worker's `queue.complete` raise `StaleLease`;
2. the job is re-queued and re-run, returning `up_to_date`.

The release is correct. The job history is not, and other jobs can lose their leases.

**Fix**
- Fence with a non-blocking check at the start: `ops.assert_lease` with `FOR UPDATE NOWAIT` is not right either. Instead, call `fence(conn)` as the last statement before commit, as `handle_simulation` effectively does with `complete_in`.
- Give the heartbeat its own short `lock_timeout` and treat `LockNotAvailable` as "busy, retry".

### R2-15 — Concurrent identical scenario creation returns 500 (PLAUSIBLE)

`sim.create_scenario` checks for `spec_digest` and then inserts. Two concurrent identical requests both pass the check, and the second fails `UNIQUE(spec_digest)` with 23505, which surfaces as 500 `database_error`.

**Fix.** In the function, add `INSERT … ON CONFLICT (spec_digest) DO NOTHING` and re-select. Alternatively, take `pg_advisory_xact_lock(hashtextextended(p_spec_digest, 0))` before the existence check. Both need a new migration.

### R2-16 — Demo CLI rough edges (PLAUSIBLE)

All in `cli/demo.py`:
- **Host clock for the cutoff.** `knowledge_cutoff = datetime.now(UTC)` uses the host clock. If the host is behind the DB clock and the release was just published, `compile.load_release` raises `LeakageBlocked`, and the demo records an `unsupported` run.
  - Fix: use `SELECT clock_timestamp()` from the DB, or `max(release.knowledge_cutoff, db_now)`.
- **No origin option.** `reva demo` has no `--origin`. It always resolves a `live` release, so it cannot run on a fixture-only local database even though `run_demo` supports it.
- **Bypassed claim path.** `_claim` bypasses `queue.claim` and gives the handler no heartbeat. A cancellation through the API during the demo is ignored, and a `cancel_requested` job is then completed `succeeded`.
- **Results mixed across runs.** The results query selects all runs of the scenario (`run.scenario_id = :s`), not `result_ref.run_id`. The filter `… or True` on line 115 is a no-op.

### R2-17 — Scheduler slot-key text depends on the session TimeZone (PLAUSIBLE)

`slot_key` uses `slot.isoformat()` of a timestamptz returned in the connection's TimeZone. Two workers whose sessions have different TimeZone settings produce different keys for the same instant. The `FOR UPDATE` and `next_due_at` advance is the real guard, so this only matters together with R2-02 or a failed advance.

**Fix.** Use `slot.astimezone(UTC).isoformat()`, and check the type and payload of a conflicting row (R2-02 fix 3).

### R2-18 — `/v1/query` accepts naive `as_of` (PLAUSIBLE)

`api/routes/sim.py:run_query` checks the timezone only for `TimeseriesQuery`. `BeliefsQuery.as_of` and `RelationsQuery.as_of` pass naive datetimes to SQL, where the server session TimeZone interprets them. The GET endpoints reject the same input (`require_aware`).

**Fix.** Call `require_aware(spec.as_of, "as_of")` for every kind.

### R2-19 — Simulation provenance lacks the scenario banner (PLAUSIBLE)

`GET /v1/provenance/sim_result/{id}` returns the scenario value (`attributes.value`) with `modality="metadata"`, and its warnings omit `scenario_context`. Every other simulation response carries that banner.

**Fix.** For kinds `sim_result`, `run` and `scenario`:
- add `SCENARIO_BANNER`;
- set `modality="assumption_conditioned_scenario"`;
- set `context_id` from the scenario's context.

---

## Out of scope / fixed during the review

- **Bootstrap role provisioning failed about 96% of the time (VERIFIED, now fixed in the working tree).**
  - `cli/bootstrap.py:provision_roles` executed `text(stmt)`. The SCRAM verifier contains `=:` (base64 padding followed by a colon), which SQLAlchemy parsed as a bind parameter. Every `db`-fixture session therefore failed with `ConfigurationError … (SQLSTATE StatementError)`.
  - Offline check: 193 of 200 generated statements contained a spurious bind.
  - The working tree now uses `conn.exec_driver_sql(stmt)`, which is correct. Owners should add a unit test asserting `text(f"'{verifier}'")` is never used.
- **`/v1/scenarios/{id}` and `/v1/simulations/{id}` provenance links (`scenario`, `run`) returned 422.** `ProvenanceKind` and `context_mode` were extended concurrently. They now return 200 with `context_mode=simulation`. Not reported.
- **Test environment.** Runtime roles have `CONNECTION LIMIT 10`, and roles are cluster-global, so parallel agents' test databases share that budget. One verification run failed with "too many connections for role reva_worker" until other sessions finished. This is expected locally, but it makes DB tests flaky under parallel CI. Consider a higher limit, or per-database roles, for test clusters only.

## Checked and fine

**Authentication and CSRF**
- Every `/v1` router carries `require_scope("read")`, and every unsafe route additionally requires a specific scope. The exceptions are `POST /v1/session` (login), and `GET`/`DELETE /v1/session` and `POST /v1/jobs/{id}/cancel`, which use `authenticated`; the cancel route has its own scope check (see R2-07). `/metrics` requires `read`.
- No route or SPA catch-all accepts unsafe methods without authentication.
- CSRF:
  - Every non-GET/HEAD/OPTIONS cookie request is checked inside `authenticate`, which every protected dependency runs.
  - The header must equal both the cookie and the value bound in the signed session, compared in constant time.
  - Bearer requests skip CSRF, which is correct: browsers cannot attach them cross-site without a CORS preflight.
- Login issues a fresh session id and CSRF token, so session fixation is not possible. Cookies are HttpOnly and SameSite=Strict, and Secure in production or under an `https` base URL.
- Token lookup is by SHA-256, so there is no timing oracle. Unknown scopes are dropped and revocation is honoured.
- **X-Forwarded-For.** `scripts/start_api.py` runs uvicorn 0.54 with `--proxy-headers` and private-range trusted proxies. uvicorn selects the rightmost *untrusted* XFF entry, so a client cannot spoof its rate-limit key through Render's appending proxy. The one exception is a caller inside the trusted private ranges (the same Render private network); keep `REVA_FORWARDED_ALLOW_IPS` as narrow as the platform allows.

**API responses**
- **SQL injection.** No dynamic SQL in the reviewed routes; all filters are bound parameters, and the `ILIKE` pattern is escaped.
- **Bounded queries.**
  - Page sizes are 1..1000.
  - Timeseries is capped at 5,000 rows, provenance at 1,000 nodes and depth 8, relations at depth 2 and 500 edges.
  - Simulation results are limited to 5,000 rows.
- **Problem details.** They never echo request bodies. DB errors map to generic problems, except P0001 messages from the narrow functions, which are intended.
- **Envelopes.**
  - Missing values stay typed and are counted in warnings for beliefs and timeseries.
  - Fixture origin is labelled on every evidence, belief, scenario and simulation response, and "mixed" is labelled as well.
  - Scenario and simulation responses carry `context_mode=simulation` and the scenario banner (except R2-19).
  - Sensitivity ranges use `kind=sensitivity_range` with `level=null`, never a confidence level.

**Simulation**
- **Scenario cutoff vs baseline.**
  - Checked at creation (`cutoff ≥ release cutoff`, not in the future, strict RFC 3339 parse now in `spec.py`).
  - Re-checked at execution (`compile.load_release` raises `LeakageBlocked`).
  - Plans re-validate the stored spec.
- **L0 cache key.**
  - `logical_hash(spec − fidelity)`, plus baseline and `model_manifest_logical_sha256` (which includes `implementation_digest`).
  - Floats are canonicalised by `repr`, and the jsonb round-trip preserves them, so two different specs cannot share a key.
  - L1 specs (fixed quantities, empty grid) and L2 specs (grid) can never collide.
  - Cached outputs are re-digested and compared before reuse.
- **Determinism.**
  - Countries are sorted and output digests are computed over sorted rows. Grid order only changes the spec digest, never the outputs.
  - Duplicate grid points are evaluated once.
  - Both iterative Tarjan implementations (`simulation/coupling.py`, `operations/invalidation.py`) match a brute-force SCC on 2,000 random graphs.
- **Numerics.**
  - The 50-digit `Context` inherits the default traps.
  - The ε=0 path returns `p` exactly, so the zero-elasticity L2 case equals L1 bit for bit.
  - Integral exponents use exact `**`; others use correctly rounded `exp(ln)`.
  - Formatting happens inside the model context.
  - Missing M, X or G becomes per-country `missing_input`, never zero.
  - G ≤ 0 or negative border values are rejected.
  - Ambiguous baselines (more than one selected estimate) are rejected.
  - Basis, unit and year mismatches become `IncompatibleSemantics`.
  - DZA returns `missing_input`.
- **Result publication.** Run, results, outcome, artifacts and `ops.complete_job` commit in one sim-role transaction that starts with `assert_lease` and holds the job-row lock. A stale holder cannot publish, and cancellation that arrives during publication waits on the lock.

**Queue and scheduler**
- **Queue protocol.**
  - Claims use `FOR UPDATE SKIP LOCKED` in a short committed transaction, and leases use the DB clock.
  - Fencing tokens come from a sequence.
  - Requeueing expired jobs skips locked rows, so it cannot steal a job that is mid-publication.
  - Dead-lettering happens at `max_attempts`, through both `fail` and lease expiry.
  - `release` returns the attempt and maps a pending user cancel to `cancelled`.
  - `heartbeat` lease seconds are now clamped to [10, 3600] in SQL.
- **Scheduler.** Due rows are locked `SKIP LOCKED` and advanced in the same transaction. Missed slots are coalesced to the latest one, so restarts cause no flood.

## Requested changes outside R2's ownership

| Owner | Change |
|---|---|
| Lead (migrations) | R2-02 fix 1 (namespaced keys in `ops.submit_job`); R2-07 optional DB-side type check; R2-09 `ops.session_revocation` table and grant; R2-15 `ON CONFLICT` in `sim.create_scenario`; R2-12 `ops.api_token.principal_kind`. |
| Agent C (simulation) | R2-01 handler change; R2-05; R2-06; R2-20 (with agent E). |
| Agent D (queue, worker, ops) | R2-01 worker change; R2-02 fixes 2–4; R2-08; R2-13; R2-14; R2-17. |
| Agent E (API) | R2-20; R2-03; R2-04; R2-07; R2-09 check; R2-10; R2-11; R2-12; R2-18; R2-19. |
| Lead (CLI) | R2-16 and R2-12 CLI flag. |
