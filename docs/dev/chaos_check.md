# Chaos check: real-process fault injection for the queue, worker and publication

Date: 2026-10-15. Scope: `src/reva/adapters/postgres/queue.py`, `src/reva/operations/worker.py`,
`src/reva/operations/jobs.py` (publish_beliefs), `src/reva/belief/publication.py`,
`src/reva/simulation/jobs.py`. No `src/` file was edited. The faults were delivered to real worker
**OS processes** against throwaway databases. The dev DB `reva` was never touched.

## How to run

```
uv run pytest tests/chaos -m chaos -q          # ~2.5 min, 11 tests (10 pass, 1 strict xfail = CHAOS-1)
```

* `tests/chaos/_harness.py` creates a fresh database per module (`reva_test_chaos_<label>_<hex>`)
  the same way as `tests/conftest.py::db`: create, bootstrap, seed as the owner, then drop
  (`REVA_KEEP_TEST_DB=1` keeps it). It also disables `ops.schedule`.
* Workers are spawned with `subprocess.Popen`. The environment is `REVA_DB_NAME=<throwaway>`,
  `REVA_ENV=test`, `REVA_JOB_LEASE_SECONDS=10` (the settings minimum),
  `REVA_JOB_HEARTBEAT_SECONDS=1`, `REVA_WORKER_DRAIN_SECONDS=5` and `REVA_OBJECT_ROOT=<tmp>`.
  `REVA_MIGRATION_DATABASE_URL` points at the throwaway DB; the worker strips it anyway.
  Two kinds of process are used:
  * the real CLI, `reva worker`;
  * `tests/chaos/_chaos_worker.py`, which runs the unchanged `reva.operations.worker.main()`. It adds
    only a test-only `chaos_slow` job type (cooperative or stubborn, resumable from a checkpoint)
    and two opt-in delay hooks. `CHAOS_PLAN_DELAY` sleeps after `simulation.jobs.plan_run`.
    `CHAOS_PUBLISH_DELAY` sleeps inside `publish_release`'s open transaction, after the release
    row is inserted and before it is sealed. Each hook writes a JSON event, so faults are
    delivered at a known point and not after a timed sleep.
* **No built-in delay hook exists** in settings or env for ingest, simulation or publication, so the
  slow paths above live in test code (monkeypatched inside the worker process). Nothing was added
  to `src/`.
* `CHAOS_LAUNCHER=<path>` swaps in another launcher. It was used to validate the CHAOS-1 patch
  below in a real process before the patch lands.

## Results (4 full passes, all stable; the first 3 were the flakiness check)

| # | Test (`tests/chaos/…`) | Fault | Verified | Result | Call time (runs 1/2/3) |
|---|---|---|---|---|---|
| 1 | `test_chaos_queue.py::test_sigkill_mid_simulation_reclaimed_after_lease_expiry_and_completed_once` | SIGKILL worker A while it computes a real L1 simulation | Job stays `running` until A's **DB-clock** `lease_until`. It is reclaimed only after that (first new fencing token observed at or after A's lease end) by a real `reva worker` CLI process. It ends `succeeded` with attempts=2, a higher fencing token and **exactly one `sim.run`**. Zombie with A's stale token: the full `handle_simulation` raises `StaleLease`; `queue.complete/heartbeat/release/fail` raise `StaleLease`; raw `ops.complete_job` gives SQLSTATE `RV001`. The job row and result are unchanged and there is still 1 run. | pass ×3 | 13.4 / 12.7 / 13.7 s |
| 1b | `…::test_sigstop_zombie_wakes_after_reclaim_and_cannot_publish` | SIGSTOP A mid-simulation, B completes it, then SIGCONT A (a live zombie) | A wakes, its heartbeat or safe point hits the fence and logs `lost_lease`. A survives and publishes nothing. 1 run, result unchanged. | pass ×3 | 11.5 / 11.4 / 12.2 s |
| 2 | `…::test_sigterm_releases_job_without_cancel_or_attempt_and_exits_within_grace` | SIGTERM during a cooperative job | Exit code 0 in < 5 s (the drain grace). Job is `queued`, attempts=0, `finished_at` NULL, `last_error.code=released`, checkpoint saved. Never `cancelled`. A new worker resumes from the checkpoint and the job ends `succeeded` with attempts=1. | pass ×3 | 7.8 / 7.6 / 8.8 s |
| 2b | `…::test_sigterm_with_uncooperative_handler_exits_at_grace_and_lease_recovers` | SIGTERM during a handler that ignores stop signals | Process exits after the 5 s grace (rc=1, "not drained"). Job left `running`, recovered by lease expiry, then `succeeded` with attempts=2. The abandoned attempt never finished. | pass ×3 | 15.2 / 16.1 / 16.7 s |
| 3a | `…::test_backend_terminated_mid_heartbeat_worker_reconnects_and_keeps_lease` | Lock the job row so the next `ops.heartbeat` blocks, `pg_terminate_backend` that backend *mid-heartbeat*, then terminate every worker backend | The worker reconnects (pool pre-ping): the lease is extended again and `ops.worker_presence.last_seen` advances. Job `succeeded` with attempts=1 and the same fencing token. The process stays alive. | pass ×3 | 9.5 / 8.9 / 9.6 s |
| 3b | `…::test_sustained_db_outage_handler_stops_by_lease_expiry_then_worker_reconnects` | `REVOKE CONNECT` on the throwaway DB from reva_worker/reva_sim and terminate their backends, held for lease + 6 s, then re-grant | The worker survives, reconnects and the job is eventually `succeeded` with a new fencing token. **But the handler keeps working for the whole outage, past its lease** (see CHAOS-1). | **strict xfail ×3** (CHAOS-1) | 18.0 / 17.9 / 18.5 s |
| 4 | `…::test_request_cancel_during_simulation_cancels_and_publishes_nothing` | `ops.request_cancel` (API role) while a real simulation computes | Job ends `cancelled` with `result_ref={"published": false}`. 0 `sim.run` rows for the job and the scenario. The worker then takes the next job. | pass ×3 | 5.1 / 5.2 / 6.5 s |
| 4b | `…::test_request_cancel_stops_cooperative_handler_promptly` | `ops.request_cancel` during a cooperative job | `cancelled`, `result_ref` NULL, no work steps more than 1.5 s after the cancel, never finished. | pass ×3 | 1.9 / 1.7 / 2.4 s |
| 5 | `…::test_twenty_parallel_submissions_with_one_idempotency_key_create_one_job` | 20 threads, each with its own DB session, barrier-released: `ops.submit_job` (API path), `queue.enqueue` (internal path), and the same key with 20 different payloads | API path: 1 job, 20 identical ids. Internal path: 1 job, distinct from the `api:` namespace. Conflicting payloads: exactly 1 accepted, 19 refused with "idempotency key reused with a different request". 1 row per key. | pass ×3 | 0.5 / 0.4 / 0.9 s |
| 6a | `test_chaos_publication.py::test_concurrent_publish_jobs_on_two_workers_make_one_release` | 3 `publish_beliefs` jobs for one dirty set (1 from the outbox, 2 duplicates), 2 real `reva worker` processes | **Exactly 1 release and 1 `ops.publication`**. Results `published`, `up_to_date`, `up_to_date`. 0 dirty belief nodes, 0 unsealed releases. Contention was really exercised: two jobs hit `PublicationBusy` and retried (attempts=2). | pass ×3 | 12.6 / 7.5 / 7.3 s |
| 6b | `…::test_sigkill_inside_publication_transaction_leaves_no_half_published_release` | SIGKILL while the worker sleeps *inside* `publish_release`'s transaction (release row inserted, not yet sealed) | Before the kill, the release is invisible and the publication advisory lock is held. After the kill: the lock is freed; release, publication, estimate and outbox counts are unchanged; the dirty set is intact. After lease expiry a CLI worker publishes **one** new sealed release (a different id from the killed one) with its estimates. The job ends `succeeded` with attempts=2. Every release has its `ops.publication` row. | pass ×3 | 37.1 / 20.7 / 25.0 s |

Module setup (fresh DB, plus the fixture ingest and baseline release for simulations) took 10–17 s.
Every test is under 60 s (the longest was 37 s). A fourth full pass, after aligning seeding with the
updated `tests/conftest.py`, also passed: 10 passed and 1 xfailed.

## Bugs found

### CHAOS-1: a worker cut off from the DB keeps running the handler past its lease (medium)

**Status (round 6, agent STAB): fixed** in `src/reva/operations/worker.py` as proposed below (local lease
deadline at 90 % of the lease from the send time of the last successful extension; the check also runs on
rate-limited handler safe points). The strict xfail was removed; the test now waits for the presence update
after the re-grant instead of asserting it immediately (a 5-step resumed attempt can finish before the
next 1 s presence beat). `tests/chaos` passed 11/11, then the queue module 9/9 twice.

**What.** `Worker._beat` treats every non-`StaleLease` heartbeat error as "retry next beat". It
returns the last known status (`running`) and never updates `last_hb`. Nothing compares local time
with the lease, so during a database outage:

* `ctx.is_cancelled()` keeps returning False;
* the handler keeps working for as long as the outage lasts, although its lease expired at
  `last successful beat + lease`;
* it stops only when the DB comes back and the next beat returns `RV001`.

In the meantime, another worker that can still reach the DB (for example a partition that cuts off
only this worker) requeues and re-runs the job. Two holders then do the same work concurrently.
Fenced DB commits are still rejected, so DB state stays correct. Side effects that are not fenced
are not protected:
* object-store writes;
* outbound HTTP and API-budget spend;
* sandbox runs;
* CPU.

This is the "must not keep doing work without a lease" requirement.

**Repro (deterministic).** `uv run pytest tests/chaos/test_chaos_queue.py --runxfail -k sustained`
fails with `35 steps of the expired attempt ran after its lease; last at +16.1s`. The lease is 10 s
and the outage 16 s. The worker log shows `heartbeat failed … OperationalError` for 15.6 s and then
`lease lost … -> lost_lease in 16.481s` only after the re-grant. Manual repro:
1. Run a long job on a worker.
2. Run `REVOKE CONNECT ON DATABASE <db> FROM reva_worker` and
   `SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename='reva_worker'`.
3. Wait longer than `REVA_JOB_LEASE_SECONDS`. The handler is still making progress.

**Proposed patch** (`src/reva/operations/worker.py`, owner HOOK/D). It was validated in a real
process through `CHAOS_LAUNCHER`, which monkeypatches exactly this logic. With it, the whole queue
chaos module passes, including 3b with `--runxfail`. The log then shows
`lease … presumed lost: no successful heartbeat for 9.1s (lease 10s)` and `-> lost_lease in 9.066s`.
Afterwards the job is requeued, reclaimed and `succeeded`.

```diff
@@ class _Active:
     lost: bool = False
     last_hb: float = field(default_factory=time.monotonic)
+    # Monotonic time at which the last *successful* lease extension was sent (claim, then each
+    # heartbeat): the DB-clock lease cannot expire before lease_basis + lease_seconds.
+    lease_basis: float | None = None
+    last_attempt: float = 0.0
+
+    def __post_init__(self) -> None:
+        if self.lease_basis is None:
+            self.lease_basis = self.job.claimed_monotonic
@@
 HEARTBEAT_STATEMENT_TIMEOUT_MS = 2000
+LEASE_SAFETY_FRACTION = 0.1  # presume the lease lost this fraction before it could expire
@@ class Worker:
     def _beat(self, st: _Active, progress: dict[str, Any] | None) -> str:
+        sent = time.monotonic()
+        st.last_attempt = sent
         try:
             status = queue.heartbeat(...)
         except StaleLease:
             ...
         except Exception as e:
             if queue.is_lock_busy(e):
                 log.info(...)
             else:
                 log.warning(...)
+            if self._lease_presumed_lost(st):
+                raise StaleLease(f"lease presumed lost for job {st.job.id}: no successful heartbeat "
+                                 f"within the {self.lease_seconds}s lease") from e
             return st.status
         st.status = status
         st.last_hb = time.monotonic()
+        st.lease_basis = sent
         return status
+
+    def _lease_presumed_lost(self, st: _Active) -> bool:
+        """Local, conservative lease deadline: stop work before the DB lease can have expired."""
+        if not st.lost and time.monotonic() >= st.lease_basis + self.lease_seconds * (1 - LEASE_SAFETY_FRACTION):
+            st.lost = True
+            log.error("lease for job %s presumed lost: no successful heartbeat for %.1fs (lease %ss)",
+                      st.job.id, time.monotonic() - st.lease_basis, self.lease_seconds)
+        return st.lost

     def _active_state(self, job_id: UUID) -> _Active:
         ...
-        if st.lost:
+        if st.lost or self._lease_presumed_lost(st):
             raise StaleLease(f"lease lost for job {job_id}")
         return st

     def _ctx_heartbeat(self, job_id, progress=None) -> str:
         st = self._active_state(job_id)
         status = st.status
-        if progress is not None or time.monotonic() - st.last_hb >= HEARTBEAT_MIN_INTERVAL:
+        if progress is not None or time.monotonic() - max(st.last_hb, st.last_attempt) >= HEARTBEAT_MIN_INTERVAL:
             status = self._beat(st, progress)
```

The `last_attempt` rate limit fixes CHAOS-2 below. Apply the same `max(st.last_hb, st.last_attempt)`
in `_ctx_user_cancel`. The lease basis is the beat's *send* time, which is conservative because the
DB sets `lease_until` later than that. `claimed_monotonic` is taken just after the claim commits,
and the 10 % margin covers that round trip. After landing, remove the `xfail` marker from
`test_sustained_db_outage_handler_stops_by_lease_expiry_then_worker_reconnects`. It is `strict`, so
it will report XPASS as a failure until the marker is removed.

### CHAOS-2: failed heartbeats are retried at every handler safe point (low)

**Status (round 6): fixed** with the `last_attempt` rate limit (both `_ctx_heartbeat` and `_ctx_user_cancel`).

**What.** The 1 s handler heartbeat rate limit (`HEARTBEAT_MIN_INTERVAL`) is keyed on `last_hb`,
which a failed beat never updates. During an outage, every `ctx.is_cancelled()` therefore opens a
new DB connection attempt. The test saw about 150 `heartbeat failed` warnings in 16 s, about 10/s,
one per step. With a real network partition, each attempt can block the handler for up to the
heartbeat pool's `connect_timeout` of 5 s. **Repro:** the log of 3b without the patch. **Patch:**
the `last_attempt` lines above (validated: 9 warnings instead of about 150).

## Observations (by design or low impact, no test failure)

* **Drain-deadline jobs keep their lease (low, design).** When a handler ignores SIGTERM, the
  worker exits after the drain grace but leaves the job `running`. It is recovered only by lease
  expiry: up to 120 s with the default lease, and the attempt is consumed (attempts=2 in 2b).
  Optional improvement: after `drain()` returns False in `main()`, call `queue.release(...)` for
  each still-active job before exiting. The fence makes this safe, because any later write by the
  dying handler is rejected once `worker_id` is NULL. 2b asserts the current behaviour and would
  need updating.
* **`PublicationBusy` consumes attempts.** A publish job that loses the advisory-lock race is
  retried through `queue.fail(retryable=True)`: backoff 5/10/20/40 s, attempt counted. Under
  sustained contention, a duplicate publish job could dead-letter. That is harmless, because the
  dirty set stays and is re-enqueued by the outbox or the next ingest, but it is noisy. Consider
  releasing instead (`queue.release`, no attempt) or re-queueing with `available_at` and no
  increment.
* **Orphan objects after a kill mid-publication** (the estimates Parquet and the manifests are
  written to the store before commit) are expected and documented ("harmless (GC)"). The DB side
  is all-or-nothing, as verified in 6b.
* The worker logs a `schema drift` warning at start on these throwaway DBs because
  `migrations/schema_contract.json` is stale relative to in-flight migration edits. In test env the
  mode is `warn`, so it does not block.
