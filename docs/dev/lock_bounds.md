# Bounded advisory locks (LOCKFIX, 2026-10-15)

## Problem

A live `demo_pjan` ingest of ~8,000 cells failed with `out of shared memory` /
`max_locks_per_transaction`. `publish_acquisition` took one transaction advisory lock **per
observation** (`hashtextextended(source|origin|logical_key)`). Every held lock is one entry in
PostgreSQL's *cluster-wide* lock table, which has about
`max_locks_per_transaction x (max_connections + max_prepared_transactions)` slots (64 x 100 = 6,400
locally, and the Render default is similar). It is shared by every database on the server, so a single large
acquisition, alongside another session holding thousands of locks, exhausts it for everyone. Managed
PostgreSQL may not let us raise `max_locks_per_transaction`, so the fix is in code.

## Why the per-lineage lock exists (and must stay)

A lineage is `(source_id, origin, logical_key)`. `_insert_observation` reads the lineage head and inserts
`revision_no = head + 1` (`supersedes_id = head`), or does nothing when the semantic hash is unchanged.
Two concurrent writers of one lineage must therefore be serialised. Without the lock, under READ COMMITTED
both read the same head and both claim the same revision. The second then fails the per-origin unique key
(the whole acquisition rolls back), or two identical re-fetches both record a "new" revision. The lock is
held until commit, and each later statement takes a fresh READ COMMITTED snapshot, so a writer that waited
sees the other's committed head.

## Design (implemented in `src/reva/evidence/ingest.py`)

STAB had already replaced the per-key locks with 256 buckets taken once per acquisition. LOCKFIX
reviewed that change and refined it:

| Aspect | Decision |
|---|---|
| Bound | Each lineage hashes into one of `LINEAGE_LOCK_BUCKETS = 256` buckets of its `(source, origin)`. An acquisition has exactly one source and one origin, so it holds **at most 256** lineage locks, whatever its size. |
| When | `lock_lineages` takes all distinct buckets **once, up front** (after the acquisition row, before any observation, support or dirty-node write), in **one statement**: `SELECT count(pg_advisory_xact_lock(7262050, k)) FROM unnest(:sorted_ids)`. |
| Order | Ids are sorted ascending and `unnest` yields array order, so every ingest acquires buckets in the same global order. Two ingests cannot deadlock on buckets. |
| Per-row re-take | `_lineage_head` re-takes its own bucket (one statement, re-entrant). A re-taken lock never waits and adds no lock-table entry, which keeps the helper safe if it is called on its own. |
| Key space | Two-int form `(classid, objid, objsubid) = (7262050, id, 2)`. All other REVA advisory locks use the bigint form (`objsubid = 1`): 7262001 migration, 7262002 role provisioning, 7262011 publication, 7262020 evidence snapshot/write, 7262021 publish job, 7262031 holdout, plus the `hashtextextended(...)` keys (`acq|`, `obj|`, `reva.release:`, `scenario|`, budgets, gate). PostgreSQL never confuses the two key forms. `id` is also always negative (`objid >= 2^31` in `pg_locks`), so even a probe that filters `pg_locks` on `objid` alone cannot match a small fixed key. |
| Namespacing | `id = h(source, origin, bucket)` with the sign bit forced, so lineages of different sources or origins do not share buckets (a 2^-31 hash collision would only serialise). STAB's version used a global `(7262050, bucket)`, under which any two large acquisitions serialised against each other whatever their source. |
| Wait | `lock_timeout` is raised to `LINEAGE_LOCK_WAIT = 60s` for the bucket statement only, then restored. A same-source writer may now wait for a whole earlier acquisition, not just a shared key, and the engines' default `lock_timeout = 5000` ms would turn that wait into a spurious failure. 60 s stays below the default job lease (120 s): the publication transaction holds the job row `FOR UPDATE` via `ops.assert_lease` (fence), so heartbeats for that job cannot renew it while it waits. |

### Trade-off

Two acquisitions of the **same** source and origin that together cover most buckets now run one after the
other, where before they ran in parallel on disjoint keys. Any acquisition of more than about 1,000 keys
touches nearly every bucket. Correctness is unchanged, and so is the result for writers of the same key.
If the first acquisition holds its buckets for more than 60 s, the second fails with a lock timeout and
the job is retried, which is idempotent (acquisition dedup plus revision dedup). Worker concurrency
defaults to 1, so this mostly matters when a CLI ingest runs alongside the worker.

### Lock order inside `publish_acquisition` (deadlock review)

1. `7262020` shared (evidence write; belief publication takes it exclusive).
2. fence `ops.assert_lease` (job row `FOR UPDATE`).
3. `acq|cv|fingerprint|origin` (bigint): serialises re-fetches of one request.
4. `obj|<raw sha>` via `register_artifact` (shared with orphan GC, which only *tries* the lock and
   never waits).
5. Lineage buckets, ascending, in one statement.
6. Per observation: `world.support` `INSERT ... ON CONFLICT` and the observation insert (row and
   transaction locks, not lock-table entries unless a writer waits).
7. `ops.dirty_node` upsert per concept, then `post_publish` (for example, Natural Earth registers one
   normalised artifact, taking an `obj|` lock after the buckets).

No cycle involves the buckets. Every ingest takes steps 1 to 5 in the same order and 5 in ascending order.
The only lock taken after the buckets that another ingest could take before them is the `obj|` lock of a
post-publish artifact. That would need a derived artifact whose bytes equal another acquisition's raw
bytes, and PostgreSQL's deadlock detector would still abort one side rather than hang.

**Existing contention outside these locks (unchanged, documented):** two ingests that share a
`world.support` digest or a concept's `belief:<concept>` dirty node also wait on each other's
*uncommitted rows*, under the default 5 s `lock_timeout`. This is why the cross-source test pauses writer A
right after it takes its buckets.

## Other lock sites reviewed

| Site | Pattern | Per-transaction entries | Action |
|---|---|---|---|
| `register_artifact` / `lock_object_xact` (`obj|sha`) | one per artifact registered | constant: ingest 1 (+1 post-publish), belief publication ~4, simulation run 2, dataset derive 2, population materialise <= ~5 | none: no loop over rows |
| `belief.reject_if_published` trigger (estimates, inputs, release artifacts, pinned `world.relation`) | per row, but always the **same** key `reva.release:<id>`, taken shared | 1 (re-entrant) | none. Measured: 1,166 rows and 202 pinned relations gave 7 advisory locks |
| `ops.check_publication` | `7262011` + release key | 2 | none |
| `pin_release_relations` (world.relation pinning) | one `INSERT ... SELECT`, per-row trigger as above | 1 | none |
| dataset `publication_hook` / `pin_datasets` | row inserts only | 0 advisory | none |
| `sim.create_scenario` (`scenario|sha`), budgets (`reva:budget:*`), holdout `7262031`, gate (`reva-gate-request|id`, session-level, one per request) | one per call | constant | none |
| GC `try_lock_object_xact` | try-lock, one short transaction per object | 1 | none |

## Proof (`tests/integration/test_lineage_locks.py`)

- `test_ten_thousand_observations_hold_a_bounded_number_of_locks` runs 10,000 observations in one
  acquisition and counts `pg_locks` inside the publication transaction: **256 lineage locks, 259
  advisory, 375 lock rows in total** (with `max_locks_per_transaction = 64`). It then rolls back. The
  test also checks that the `lock_timeout` override does not leak.
- `test_concurrent_writers_of_one_logical_key_are_serialised`: writer B is observed waiting
  (`granted = false`) on its bucket while A is open. A gets revision 1, and B gets revision 2 with
  `supersedes_id` set to A's revision 1. Two concurrent identical writes give one new revision and one
  `unchanged`.
- `test_overlapping_acquisitions_in_opposite_key_orders_do_not_deadlock`: four concurrent same-source
  acquisitions, with key sets overlapping in reversed and interleaved orders, all commit. The shared keys
  end at revisions [1, 2, 3, 4].
- `test_large_acquisition_does_not_block_another_source`: source B, with the same 1,000 logical keys,
  commits while A holds all its buckets.
- `test_lineage_lock_keys_never_collide_with_fixed_keys` checks that the key space is disjoint from the
  fixed keys.
- `test_belief_publication_lock_entries_do_not_grow_with_rows` checks that the advisory locks of a real
  publication do not grow with the number of rows.

Negative control: the same tests run with the old per-key locking emulated (scratch pytest plugin) fail.
The 10k test sees 10,000 lineage locks, and the opposite-order test sees lock timeouts on deadlocked
writers.
