# V3 KGCORE — world knowledge graph storage, queries, API

Agent KGCORE, 2026-10-15. Spec: `docs/roadmap/v3_world_graph.md` (KG storage, API, honesty rules).

## What exists

| Piece | Path |
|---|---|
| Migration (schema `kg`) | `migrations/versions/0009_kg.py` (revises `0008_v2`) |
| Python interface + bulk loader | `src/reva/kg/model.py` |
| Read-only queries + path engine | `src/reva/kg/query.py` |
| HTTP API (`read` scope) | `src/reva/api/routes/kg.py` (one router line in `src/reva/api/app.py`) |
| Tests | `tests/unit/test_kg_model.py` (39), `tests/integration/test_kg_store.py` (12), `test_kg_paths.py` (11), `test_kg_api.py` (13), `tests/performance/test_kg_perf.py` (perf marker) |
| Contracts | `contracts/openapi.json` (5 `/v1/kg/*` paths), `migrations/schema_contract.json` (head `0009_kg`, fingerprint `f88944af18f9a2c7`) |

### Storage (`kg` schema, everything append-only)

* `kg.load`: one row per loaded batch. It is the provenance manifest: source key, origin (`live`/`test_fixture`), every raw artifact sha256 the batch was parsed from (each must be a verified `meta.artifact`: trigger `kg.check_load`), retrieval time (not in the future), loader version, server `recorded_at` and `recorded_by` (login).
* `kg.entity` + `kg.entity_label`: entity snapshots (qid, kind, descriptions, aliases, sitelinks, source revision, content sha256) and labels/aliases per language (search index).
* `kg.property` (pid, value kind; never changes) + `kg.property_version` (REVA predicate, inverse name, en/fr labels and inverse labels; a rename appends a version). This is the allow-list: statements must use a registered property.
* `kg.statement`: subject, property, object qid **or** typed literal `value` (jsonb with `type`; quantities as decimal strings).
  * Temporal qualifiers: `start_time/end_time/pit_time` (raw strings) with Wikibase precision, plus `time_calendars` when not Gregorian.
  * Derived inclusive `valid_lo/valid_hi` and `value_time_lo/hi` dates, used only for filtering and sorting.
  * `qualifiers`, `refs` (the statement's own references), `rank` (preferred/normal; **deprecated is refused by a CHECK**).
  * Identity and provenance: `statement_key`, `statement_id`, `source_key`, `source_revision`, `raw_sha256` (must be one of its load's raws), `retrieved_at`, `content_sha256`.
* `kg.statement_retraction`: a source deprecated or removed a statement, so that row leaves the current view. Nothing is deleted.
* Views:
  * `kg.statement_current`: latest row per (statement_key, origin), not retracted, with the load's `recorded_at`.
  * `kg.entity_current` and `kg.property_current`: the same idea for entities and properties.

### Security and integrity

* Integrity triggers:
  * Append-only (`meta.reject_mutation` / `reject_truncate`) on all 7 tables.
  * Server `recorded_at` on `kg.load` and the property tables.
  * `recorded_by` on the same tables.
  * Statement-level `AFTER INSERT` triggers (transition tables) check that batch rows match their load (source, origin, retrieval time, raw artifact). They also check that rows are inserted **in the transaction that created the load**, so the load's `recorded_at` is theirs, and that the property is allow-listed.
* Grants: `reva_worker` holds INSERT only. `reva_api`, `reva_sim` and `reva_gate` can only SELECT. All `kg.*` functions are `REVOKE ALL … FROM PUBLIC`. The restricted-deletion role covers the tables, as in 0007/0008.
* Tests prove that api/sim/gate cannot insert, update or delete, that the worker cannot update, and that even the owner cannot mutate or truncate.
* Label search uses a `pg_trgm` GIN index when the extension can be created. `pg_trgm` is trusted, so a non-superuser owner with CREATE on the database can install it. If it cannot be created, the migration logs a NOTICE and keeps only the `lower(label) text_pattern_ops` prefix index.

### Identity, idempotency, revisions (`load_batch`)

* Statement key: `<source_key>:<statement_id>`, or `<source_key>:h:<sha of subject, property, object/value, times, qualifiers>`. Rank and references are not part of the identity, so a change in them is a new revision of the same statement. Two sources stay distinct even for the same claim.
* Unique on (statement_key, origin, source_revision). How a batch row is classified:

| Case | Result |
|---|---|
| Same key, same revision, same content | **unchanged** (an exact reload inserts nothing) |
| Same key, same revision, different content | **conflicting**: reported, not stored |
| New revision with changed content | **revision**: appended; the current view shows it |
| New revision with unchanged content | **unchanged**: not re-stored |

  The stored row keeps the revision and retrieval time at which that content was first recorded.
* A batch retrieved before the stored latest row is **stale**: reported, not stored.
* A deprecated-rank statement is rejected. If an earlier version is stored, a `deprecated_at_source` retraction removes it from the current view. `retract_keys` records `removed_at_source`.
* Rejections are per statement, with a reason, and are counted in `LoadReport.rejected_counts` (sample in `rejected`). Never silent. Reasons:
  * unknown property, property kind mismatch, object xor value, float quantities, invalid ids or time;
  * end before start, raw artifact not in the batch, source mismatch, conflicting duplicates in the batch, too large.
* Bounds: 50,000 entities, 200,000 statements and 50,000 retractions per call. Staging chunks are 25,000 rows.
* Raw artifacts that are neither registered nor in the object store refuse the whole batch (`IntegrityFailure`). If the bytes are in the store (`raw` namespace) but unregistered, the loader registers them.
* Loads of one source are serialised by an advisory lock.

### Time semantics

* Time strings use astronomical year numbering (year 0 = 1 BCE), as WDQS returns them. `wikibase_json_time()` converts the historical numbering of the JSON API and reads its calendar model.
* Derived bounds span the period at each precision: day; month; year (1975 → 1975-01-01..1975-12-31); decade (1990s). Century and coarser are **widened one unit each side**, because sources disagree on which year stands for a century.
* Julian dates (precision ≥ year) are converted to proleptic Gregorian through the Julian day number: 1582-10-04 Julian → 1582-10-14. The raw string is kept.
* Dates outside PostgreSQL's range (before 4713 BCE, e.g. geological time) are kept raw and count as undated for filters.
* Validity: start (or point in time) .. end (or point in time). A missing side is open.
* Output (KGBCE fix): the derived bounds (`valid_from`, `valid_to`, timeline `sort_date`, path `search.time_window`) are ISO 8601 with astronomical years, rendered like the atlas `period_text` (`-0043-03-15` = 15 March 44 BCE, `0000` = 1 BCE). Queries select the `date` columns through `to_char(…, 'YYYY-MM-DD BC')` (psycopg cannot load BC `date`s) and convert them in Python (`astro_iso`); filtering and ordering stay in SQL on the dates.

### Queries and API

| Endpoint | Function | Notes |
|---|---|---|
| `GET /v1/kg/entities/{qid}` | `entity` | live snapshot preferred; provenance incl. raw sha256s; degree (capped) |
| `GET /v1/kg/entities/{qid}/neighbours` | `neighbours` | direction out/in/both, `property` list, neighbour `kind`, `valid_from/valid_to` (+`include_undated`), literals optional, keyset cursor, ≤ 1000 |
| `GET /v1/kg/entities/{qid}/timeline` | `timeline` | statements dated by validity qualifiers or a time value (e.g. inception), sorted, ≤ 2000, truncation flagged |
| `GET /v1/kg/search?q=&lang=fr&lang=en` | `search` | exact > prefix > substring (≥ 3 chars), labels and aliases, LIKE wildcards escaped |
| `GET /v1/kg/path?from=&to=` | `paths` | see below |

Every statement carries its provenance:

* source key, statement id/key, source revision, raw sha256;
* retrieval and recording times, load id, origin;
* rank and its own references;
* `verification`: `community_edited_not_verified` for Wikidata (`wikidata`/`wikidata.*` keys); `derived` (with `derivation` and `derivation_method`) when the statement's qualifiers declare a `derivation` (e.g. the Cliopatria overlay `geo:covered_territory_of`, `computed_spatial_overlay`, `reva.kg.overlay/1`; envelope warning `derived_statements`); `source_reported_not_verified` for other sources.

Errors are RFC 9457 problems: 404 for an unknown entity, 422 for invalid ids, time bounds, hop counts or property ids. A missing token is 401.

**Path engine.** A bidirectional breadth-first search in SQL and Python over entity-valued current statements, in either direction:

* `max_hops` ≤ 5.
* Nodes with more than `hub_limit` (default 2000) entity-valued statements are not traversed unless they are an endpoint or listed in `allow_hub`. Skipped hubs are reported with their degree.
* `instance of`/`subclass of` (P31/P279) are excluded unless explicitly allowed.
* With a time window, every hop must overlap it. Undated hops are allowed and flagged `undated` unless `include_undated=false`.
* There are per-level frontier and edge caps. Hitting one sets `truncated` and the reason. A "not found" says whether bounds or hubs may hide a connection.
* Up to `max_paths` paths are returned, all of minimal length. Each hop gives its property labels (en/fr; an inverse label for reverse traversal), direction, dates, time status and the full statement provenance.

Wording:

* An ordinary hop reads "A is connected via '<label>' to B". The path reads "connected … via N sourced statement(s); a connection is not a cause". The envelope carries a `path_not_causal` warning.
* Causal wording appears only for the source-asserted causal properties `CAUSAL_PIDS` = P828, P1542, P1478, P1479, P1536, P1537. This is the same set WDHARVEST's allow-list marks causal. Such a hop reads "a causal relation asserted by <source> (statement …), not established by REVA" and gets `causal_assertion: {asserted_by, statement_key}`.

## Performance (measured)

Synthetic graph built by `tests/performance/test_kg_perf.py` (`REVA_KG_PERF_STATEMENTS=1000000`, `-m perf`), loaded into a throwaway database that was dropped afterwards:

* 200,000 entities with en/fr labels.
* 1,000,000 statements. 80 % are random edges; 20 % go to 20 hubs of about 10,000 edges each. 40 % are dated.

Host: shared 4-vCPU development container. **Load average was 8–10 throughout**, from about 30 other agents. PostgreSQL 17 had stock settings (`shared_buffers` 128 MB).

| Measure | Result |
|---|---|
| Statement load, end to end (`load_batch`, batches of 50,000, incl. validation, hashing, classification, triggers) | **11,385 statements/s** (87.8 s for 1M; per batch 3.6–5.5 s, flat as the table grows) — **target 20,000/s not met** |
| Entity load (200k entities, 400k labels) | 6,252 entities/s |
| Re-loading an unchanged 50k batch (all `unchanged`) | 3.2 s |
| Size on disk for 1M statements + 200k entities | 744 MB (statement 525 MB incl. indexes, labels 112 MB, entities 106 MB) → about 0.6 KB/statement; the 6 GB KG budget fits about 8M Wikidata-shaped statements |
| Path ≤ 4 hops (30 random pairs) | p50 36 ms, p95 127 ms, max 222 ms |
| Path ≤ 4 hops with a 1900–1950 window | p50 23 ms, p95 70 ms |
| Path ≤ 5 hops | p50 87 ms, p95 212 ms, max 325 ms |
| Path from a hub endpoint (~10k edges) | 1.08 s |
| Neighbours (100) / timeline / entity / search | p95 11 / 10 / 5 / 45 ms |

Few random pairs connect within 4–5 hops: 0/30 at 4 hops, 4/30 at 5. That is expected. The 20 hubs exceed the default hub limit and are not traversed, and the remaining random graph (200k nodes, mean degree about 8) has typical distances of about 6. No search was truncated.

**Why 20k/s was not reached, and what was done.** Profiling one 50k batch:

* Before optimisation:
  * JIT compilation cost about 1 s per staging statement. Now `SET LOCAL jit = off`.
  * Bounded-repetition regex CHECKs (`{64}`, `{0,128}`) cost 10 µs per value; unbounded patterns plus `length()` cost 1 µs. Rewritten.
  * Two per-row FK probes. Replaced by one set-based check in the statement-level trigger.
  * A GiST range index and a redundant (key, origin, id) index. Both dropped: time filters always ride on a subject, object or property index.
  * psycopg per-element array binding. Replaced by one jsonb parameter per chunk, because runtime roles have no TEMP privilege for COPY staging and this migration does not widen database privileges.
  * Python normalisation at 35 µs/statement. Now a shared JSON encoder and cached date parsing.
  * Python normalisation now overlaps with database execution on one fixed helper thread.
* Together these took the loader from 3.2k/s to 11.4k/s.
* The remaining cost is in the database: about 75–85 µs per row on this loaded host. Roughly half is maintaining the five b-tree indexes (PK, identity, subject, object, property+time). The rest is CHECKs, classification look-ups and the transition-table trigger. Python is no longer the limit.

Options, not applied:

* Drop `statement_property_time`: about 15 % faster; no current endpoint needs it, but the spec asks for a by-property index.
* Build secondary indexes after the first bulk harvest: needs owner DDL, not a runtime-role action.
* Run the initial harvest on an unloaded host, or with `shared_buffers`/`max_wal_size` raised.

## Honesty and limitations

* Statements are what a source reports. Wikidata is community-edited; nothing in `kg` is verified by REVA.
* Paths are connections, not explanations.
* Removal of a statement at the source is only recorded when the harvester passes `retract_keys`. `load_batch` cannot infer absence from a partial batch.
* An unchanged re-retrieval does not record a newer "last confirmed" revision. The stored row keeps the revision and retrieval time at which that content was first seen.
* Hub degree for the path engine is counted on stored rows, revisions included, and capped. It is a traversal heuristic, not an exact degree.
* The `test_fixture` and `live` origins are kept apart: separate identities and current rows, labelled per statement, and envelope `origin` `mixed` when both appear.

## Requests to other owners (not done by KGCORE)

1. **MIGR / lead:** add `"kg"` to `APP_SCHEMAS` in `src/reva/adapters/postgres/schema_contract.py`, then regenerate the contract. Until then, `migrations/schema_contract.json` fingerprints only the `pg_trgm` extension and the alembic head, not the `kg` objects.
2. **BKP:** add `"kg"` to `SCHEMAS` in `scripts/backup_restore.py`. Today backups do not include the knowledge graph.
3. **Lead:** `docs/final_report.md` cites the old contract fingerprint `be8cadc331511a1b`; it is now `f88944af18f9a2c7` (head `0009_kg`).
