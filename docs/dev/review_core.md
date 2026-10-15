# Core code review (agent R, adversarial, read-only)

Scope: lead-owned core — migrations 0001–0007, `adapters/postgres/{repositories,engine,ddl}`,
`cli/{bootstrap,seed,main}`, `evidence/{ingest,revisions,runner,cards}`, `belief/*`, connectors
(`base, worldbank, comtrade, naturalearth, crossref`), `adapters/http/*`, `adapters/objects/*`,
`kernel/*`, `settings.py`, `world/space.py`, `population/service.py`, `simulation/spec.py`,
`operations/context.py`. Reviewed against prompt §4–§7 and architecture §6, §8, §21, §24.1.
Reviewed at HEAD `62a8cc7`. No code was modified.

Method: every finding marked **VERIFIED** was reproduced with a throwaway test against a fresh
scratch database (the `db` fixture pattern, `reva_test_<random>`, dropped afterwards; the dev DB
`reva` was never touched) or an offline script. Scripts and raw output are in the agent scratchpad:
`/tmp/claude-0/-home-user-REVA-World/a82f628c-2790-54df-b0b4-28930bdc7a97/scratchpad/review/`
(`test_review_db.py`, `test_review_db2.py`, `test_review_nodb.py`, `out_db.txt`, `out_nodb.txt`).
**PLAUSIBLE** means the issue was established by reading the code but not reproduced end to end.

Severity: **High** = breaks a §24.1 invariant or a security boundary on a normal code path;
**Medium** = needs a race, a crash or a privileged-but-buggy caller, or is a scientific defect with
limited reach; **Low** = hardening or a latent defect.

## Summary

| ID | Sev | Status | Area | One-line summary |
|---|---|---|---|---|
| R-01 | High | VERIFIED | grants | `reva_worker` can insert API tokens with every scope, human proposal reviews, passing gate results, model promotions and audit rows |
| R-02 | High | VERIFIED | belief | A published release is not sealed: estimates and inputs can still be appended after publication |
| R-03 | High | VERIFIED | temporal | `recorded_at` is the insert time, not the commit time, so a release can silently omit observations that are `≤ cutoff` |
| R-04 | High | VERIFIED | revisions | Live ingest dedups against a fixture-origin lineage head, so live observations are silently dropped |
| R-05 | High | VERIFIED | Comtrade | Import and export net weights share concept, subject and support, so one belief estimate mixes them |
| R-06 | Med-High | VERIFIED | invalidation | Publication deletes every `belief:%` dirty node, including concepts it did not publish and ingests it did not see |
| R-07 | Medium | VERIFIED | eligibility | `is_eligible` takes the latest decision across all scopes and ignores the `years`/`classifications` applicability |
| R-08 | Medium | VERIFIED | temporal | The worker can backdate `recorded_at` on observations and eligibility decisions |
| R-09 | Medium | VERIFIED | graph | Relation cardinality is bypassed by inserting participants in a later transaction |
| R-10 | Medium | VERIFIED | identity | Several Comtrade partner codes (250/251, 840/841/842, …) alias the same country entity |
| R-11 | Medium | VERIFIED | storage | GC deletes an object that a concurrent publication reuses by content address, leaving a committed "verified" artifact with no bytes |
| R-12 | Medium | VERIFIED | SSRF | DNS TOCTOU: the vetted address is not the address the client connects to |
| R-13 | Medium | VERIFIED | HTTP | Nested `Content-Encoding` turns 661 wire bytes into a 631 MB allocation before the size limit fires |
| R-14 | Medium | VERIFIED | atomicity | Post-publish steps (geometries, studies) are outside the transaction and never retried |
| R-15 | Medium | VERIFIED (git) | migrations | Applied migration 0004 was edited in place (`cache_miss`), so existing databases drift |
| R-16 | Med-Low | VERIFIED | storage | The S3 store "verifies" against metadata it wrote itself; existing objects are never re-hashed |
| R-17 | Med-Low | VERIFIED | secrets | Bootstrap puts the plaintext role password into SQL text, so it leaks in exception messages and server logs |
| R-18 | Med-Low | VERIFIED | provenance | `reva_sim` can attach outputs to source activities and register fake "verified" locations for any artifact |
| R-19 | Low | VERIFIED | publication | The lock check accepts a session-level or `(int4,int4)` advisory lock |
| R-20 | Low | VERIFIED | as-of | A backfill (older-cutoff) release becomes the "current" release |
| R-21 | Low | VERIFIED | scenario | The scenario `knowledge_cutoff` format is never validated (`infinity`, `yesterday`, `not a date` all pass) |
| R-22 | Low | VERIFIED | hashing | Datetimes are not UTC-normalised in canonical JSON; the `$decimal`/`$float` tags are spoofable |
| R-23 | Low | VERIFIED | Comtrade | Valuation inference ignores flow: an export can be labelled CIF |
| R-24 | Low | VERIFIED | archive | Duplicate or colliding zip member names are resolved silently (last one wins) |
| R-25 | Low | PLAUSIBLE | settings | Production validation gaps; incomplete secret redaction |
| R-26 | Low | PLAUSIBLE | functions | Unvalidated `heartbeat` lease seconds, self-asserted `human:` reviewer, unbounded payloads |
| R-27 | Low | PLAUSIBLE | selection | Retracting the latest revision silently resurrects a superseded value; the prior-estimate path ignores origin |
| R-28 | Low | PLAUSIBLE | ingest | The acquisition idempotency key ignores `parser_version` |

Already known elsewhere, not re-reported: `sim.create_scenario` trusts the caller's `p_spec_digest`
(agent H2's strict-xfail `test_create_scenario_does_not_alias_different_specs`).

---

## High

### R-01 — Worker can forge governance, authentication and audit records (VERIFIED)
`migrations/versions/0007_security.py:397-398` grants
`GRANT INSERT ON ALL TABLES IN SCHEMA {meta,ontology,world,evidence,belief,model,ops} TO reva_worker`.
That includes:
- `ops.api_token`, read by `api/auth.py:_from_token`;
- `ops.proposal_review` and `ops.gate_result`, read by `autonomy/policy.py`;
- `model.promotion`, `ops.audit`, `evidence.eligibility` and `ontology.mapping` (with `approval='approved'`).

Autonomy discovery runs inside the worker (`reva.autonomy.jobs`). Code holding worker credentials can therefore:
- mint a bearer token with every scope, which escalates to API admin;
- approve its own proposal as `human:admin`;
- record a passing gate with any `gate-evaluator:*` name;
- promote a model.

This breaks invariant 15 ("no model self-promotion or editing its own policy gates") and §21 ("API
service tokens are hashed, scoped and revocable": revocation does not help when new tokens can be inserted).

Failure scenario: a buggy or compromised discovery handler inserts
`ops.api_token(token_sha256=sha256(T), scopes=ALL)`, then calls the API with `Bearer T`.

Evidence (`test_worker_can_forge_governance_rows`, run inside a rolled-back transaction):
```
worker inserted: approving human review, passing gate, all-scope API token fd2c6fa2-…, human audit row, model promotion
```
Fix: grant the worker INSERT only on what it writes, and route governance writes through checked functions.
```diff
-    for sch in ("meta", "ontology", "world", "evidence", "belief", "model", "ops"):
-        s.append(f"GRANT INSERT ON ALL TABLES IN SCHEMA {sch} TO reva_worker;")
+    for sch in ("meta", "ontology", "world", "evidence", "belief", "model", "ops"):
+        s.append(f"GRANT INSERT ON ALL TABLES IN SCHEMA {sch} TO reva_worker;")
+    # Governance/authentication rows are never written with worker credentials.
+    s.append("REVOKE INSERT ON ops.api_token, ops.proposal_review, ops.gate_result, "
+             "model.promotion FROM reva_worker;")
```
Also do the following (a new migration is needed):
- `ops.api_token`: inserts only by the migration owner / admin CLI, or through a `SECURITY DEFINER ops.issue_token(...)` executable by the API's admin session path.
- `ops.gate_result`: insert through a separate `reva_gate` login (or a SECURITY DEFINER function owned by it) that the worker cannot call.
- `ops.proposal_review`: insert only through `ops.review_proposal`, which the worker must not be granted (it currently is not).
- `model.promotion`: insert through a function that checks an approving `proposal_review` from a `human:` reviewer who is not the author.
- `ops.audit`: keep INSERT, but add `actor_role text NOT NULL DEFAULT current_user`, so forged rows are at least attributable.

### R-02 — A published release can still be appended to (VERIFIED)
The `immutable()` triggers (`adapters/postgres/ddl.py:12`) block UPDATE, DELETE and TRUNCATE, but not INSERT.
Nothing stops new `belief.estimate`, `belief.input` or `belief.release_artifact` rows for a release that already has
an `ops.publication` row. A release is "immutable" only at row level: its contents can grow after publication.
This breaks invariants 2 and 18 and §8.2 ("readers use only committed manifests").
The stored manifest and Parquet then no longer match the DB estimate set that `snapshots.estimates()` serves.

Evidence (`test_published_release_accepts_new_estimates`):
```
published release seq=2: estimates before=100 after worker append=101
```
Fix (new migration):
```sql
CREATE FUNCTION belief.reject_if_published() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE rel uuid;
BEGIN
  rel := CASE TG_TABLE_NAME
           WHEN 'estimate' THEN NEW.release_id
           WHEN 'release_artifact' THEN NEW.release_id
           WHEN 'input' THEN (SELECT release_id FROM belief.estimate WHERE id = NEW.estimate_id)
         END;
  IF EXISTS (SELECT 1 FROM ops.publication WHERE release_id = rel) THEN
    RAISE EXCEPTION 'release % is published and sealed', rel;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER sealed BEFORE INSERT ON belief.estimate FOR EACH ROW EXECUTE FUNCTION belief.reject_if_published();
CREATE TRIGGER sealed BEFORE INSERT ON belief.input FOR EACH ROW EXECUTE FUNCTION belief.reject_if_published();
CREATE TRIGGER sealed BEFORE INSERT ON belief.release_artifact FOR EACH ROW EXECUTE FUNCTION belief.reject_if_published();
```
Apply the same "sealed after first use" rule to `world.relation_participant` (see R-09).

### R-03 — `recorded_at` is the insert time, not the commit time; a release can omit observations at or before its cutoff (VERIFIED)
- `recorded_at DEFAULT clock_timestamp()` (`0002_evidence.py:123`, and on every table) is taken at INSERT.
- `publish_release` reads candidates in a separate connection before its own transaction (`belief/publication.py:83-84`).
- The callers pass `cutoff = clock_timestamp()`: `operations/jobs.py:handle_publish_beliefs` and `cli/main.py:127`.

If an ingest transaction inserted rows before the cutoff and commits after the selection snapshot, the release
excludes observations with `recorded_at ≤ knowledge_cutoff`. Any later as-of reconstruction (`eligible_latest(cutoff)`,
`available_then`, replay) includes them, so the release disagrees with its own declared cutoff.
Retractions and eligibility decisions committed late have the same problem; for a late retraction the
`belief.check_input` trigger cannot see it and admits an observation that the cutoff says was retracted.
This is the "do not assume sequence/insert order is commit order" rule of §6.2, applied to timestamps.

Evidence (`test_inflight_ingest_with_earlier_recorded_at_breaks_release_cutoff`):
```
observation recorded_at=…23:07:08.902 <= release cutoff=…23:07:09.107: True; in release: 0;
visible to an as-of(cutoff) query now: True
```
Fix: make the writers that `eligible_latest` depends on mutually exclusive with publication, and take the cutoff
inside the locked transaction.
```diff
 # evidence/ingest.py publish_acquisition, first statement in the transaction
     with engine.begin() as conn:  # 4: one transaction
+        conn.execute(text("SELECT pg_advisory_xact_lock_shared(:k)"), {"k": repo.PUBLICATION_LOCK})
         reg = connector_registration(conn, spec)
```
Take the same shared lock in every writer of `evidence.observation_retraction`, `evidence.eligibility` and `evidence.claim`.
```diff
 # belief/publication.py
-    with engine.connect() as conn:
-        rows = eligible_latest(conn, cutoff, origins, concepts)
-    selections = select(rows, preferences)
-    ...
-    with engine.begin() as conn:
+    with engine.begin() as conn:
+        conn.execute(text("SET LOCAL lock_timeout = '120s'"))
+        repo.take_publication_lock(conn)          # waits for in-flight ingests to commit
+        db_now = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
+        if cutoff > db_now:
+            raise ValidationProblem("cutoff is in the future")
+        rows = eligible_latest(conn, cutoff, origins, concepts)   # fresh READ COMMITTED snapshot
+        selections = select(rows, preferences)
+        # objects may still be written here (before commit); keep the lock to commit
```
Holding the publication lock for the whole transaction stays correct for sequence ordering.
Also set `recorded_at` server-side (R-08).

### R-04 — Live ingest dedups against a fixture lineage head, so live data is silently dropped (VERIFIED)
- `_lineage_head` (`evidence/ingest.py:77-90`) and `UNIQUE(source_id, logical_key, revision_no)` key lineages without origin.
- Fixture and live acquisitions of the same series therefore share one revision chain.
- When a live fetch returns the same semantic state as a fixture-origin head, `decide_revision` returns `unchanged` and no live row is inserted.
- The live release then has nothing to select, because `eligible_latest` filters `a.origin = ANY(origins)`.

A live revision would also `supersede` a fixture record. And if fixture rev n+1 differs from live rev n, then a
genuine provider revision equal to the fixture value is dropped and the live world keeps the stale value.
This affects any environment where `reva fixtures load` and live ingests are mixed (the dev DB, staging).

Evidence (`test_live_ingest_dedups_against_fixture_head`; same bytes, `origin=live`):
```
live-labelled re-ingest: inserted 0 unchanged 200
live acquisitions 5 live observations 0
publish live release -> ValidationProblem no eligible observations at this cutoff; nothing to publish
```
Fix (schema change; report to lead): give lineages an origin.
```sql
ALTER TABLE evidence.observation ADD COLUMN origin text;            -- backfill from acquisition, then NOT NULL
ALTER TABLE evidence.acquisition ADD CONSTRAINT acq_origin_key UNIQUE (id, origin);
ALTER TABLE evidence.observation ADD FOREIGN KEY (acquisition_id, origin) REFERENCES evidence.acquisition (id, origin);
ALTER TABLE evidence.observation DROP CONSTRAINT observation_source_id_logical_key_revision_no_key;
ALTER TABLE evidence.observation ADD UNIQUE (source_id, origin, logical_key, revision_no);
-- supersedes FK must include origin: (supersedes_id, source_id, origin, logical_key)
```
Code changes:
```diff
-def _lineage_head(conn, source_id, logical_key):
-    conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"{source_id}|{logical_key}"})
-    row = conn.execute(text("SELECT id, revision_no, semantic_hash FROM evidence.observation "
-        "WHERE source_id = :s AND logical_key = :k ORDER BY revision_no DESC LIMIT 1"), ...)
+def _lineage_head(conn, source_id, origin, logical_key):
+    conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
+                 {"k": f"{source_id}|{origin}|{logical_key}"})
+    row = conn.execute(text("SELECT id, revision_no, semantic_hash FROM evidence.observation "
+        "WHERE source_id = :s AND origin = :o AND logical_key = :k ORDER BY revision_no DESC LIMIT 1"), ...)
```
In `belief/selection.py`, partition by `(o.source_id, o.origin, o.logical_key)`.

### R-05 — Comtrade import and export net weights collapse into one belief estimate (VERIFIED)
`comtrade.py:250` uses the single concept `trade.hs2709.net_weight.world_total` for both `flowCode=M` and `X`.
`_support(iso, year, "0")` is identical for both flows, and the subject is the reporter. `belief.selection.select`
groups by `(subject, concept_version, support)`, so the two flows fall into one group. The import weight wins on
`logical_key` order and the export weight becomes an "alternative" flagged `source_disagreement`.
The published `net_weight.world_total` therefore silently means "import weight". The disagreement flag is
scientifically false: these are different quantities, not disagreeing sources. This breaks invariant 7
(no aggregation across incompatible support) and the concept registry's own semantics.

Evidence (`test_net_weight_import_export_collapse`):
```
keys ['comtrade:HS2709:M:156:0:2021:netWgt', 'comtrade:HS2709:X:156:0:2021:netWgt'] vals ['512922149241', '2610761300']
estimate 512922149241 ['source_disagreement:un.comtrade.public_preview', 'multiple_lineages_same_source'] alt ['2610761300']
```
Fix: use flow-specific concepts. Add `trade.hs2709.import_net_weight.world_total` and
`trade.hs2709.export_net_weight.world_total` to the registry, with mappings, eligibility and preferences.
```diff
-            concept = "trade.hs2709.net_weight.world_total"
+            concept = {"M": "trade.hs2709.import_net_weight.world_total",
+                       "X": "trade.hs2709.export_net_weight.world_total"}[flow]
```
Bump the connector version (1.0.0 → 1.1.0), because the concept mapping changes. As a defensive check in
`select()`, raise or flag `incompatible_lineages` when one source contributes multiple lineages whose
`source_flags` differ on a dimension listed in the concept profile.

---

## Medium

### R-06 — Dirty-node invalidations are lost at publication (VERIFIED for the concept filter; race PLAUSIBLE)
`belief/publication.py:185-186` runs:
```sql
DELETE FROM ops.dirty_node WHERE node_key LIKE 'belief:%' AND since_sequence <= (SELECT max(id) FROM ops.outbox)
```
1. It ignores the `concepts=` filter. Publishing only population clears the GDP and trade dirty nodes, so
   `handle_publish_beliefs` then returns `up_to_date` while those concepts were never republished.
2. Selection ran earlier, in another snapshot (R-03). An ingest that commits between selection and this DELETE
   has its dirty mark erased, although its observations are not in the release.
3. `since_sequence` is `max(outbox.id)` read before the ingest's own outbox insert. Outbox identity order is not
   commit order, so the comparison is meaningless.

Evidence (`test_partial_publication_clears_unrelated_dirty_nodes`):
```
dirty nodes after publishing ONLY population: []      (belief:economics.gdp… was present before)
```
Fix (on top of the R-03 lock protocol, under which selection sees every committed ingest and no ingest is in flight):
```diff
-        conn.execute(text("DELETE FROM ops.dirty_node WHERE node_key LIKE 'belief:%' AND since_sequence <= "
-                          "(SELECT COALESCE(max(id),0) FROM ops.outbox)"))
+        published_nodes = sorted({f"belief:{s.concept_id}" for s in selections} |
+                                 ({f"belief:{c}" for c in concepts} if concepts else set()))
+        if concepts is None:
+            published_nodes = None  # full publication covers every belief node
+        conn.execute(text("DELETE FROM ops.dirty_node WHERE node_key LIKE 'belief:%' "
+                          "AND (CAST(:n AS text[]) IS NULL OR node_key = ANY(CAST(:n AS text[])))"),
+                     {"n": published_nodes})
```

### R-07 — `evidence.is_eligible` ignores scope (VERIFIED)
`0002_evidence.py:190-199` picks the single latest decision for `(connector_version, concept)` and only then
checks `subject_ids`. Two consequences:
- A later decision scoped to another subject overrides everything. A DZA-only `quarantine` makes FRA ineligible.
  Conversely, an `eligible` decision for `[CHN]` revokes FRA.
- Applicability keys other than `subject_ids` are ignored. The seeded Comtrade decisions carry
  `years: [2020, 2024]` and `classifications: [H4, H5, H6]`, but a 2019 or H3 observation is still eligible.
  This contradicts check 6 of §4: "eligible only for concepts/applicability scopes explicitly approved".

Evidence (`test_eligibility_subject_scoped_quarantine_disables_everyone`):
```
FRA eligible before DZA-only quarantine: True after: False
comtrade eligibility applicability (years/classifications ignored by is_eligible): {'years': [2020, 2024], 'classifications': ['H4', 'H5', 'H6']}
```
Fix: select the latest decision among those whose scope covers the record, and enforce the years scope.
This changes the signature, so update the callers in `belief/selection.py:50` and `belief.check_input`.
```sql
CREATE OR REPLACE FUNCTION evidence.is_eligible(cv uuid, concept uuid, subject uuid, at timestamptz,
                                                period_start date, classification text)
RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT COALESCE((
    SELECT e.decision = 'eligible'
    FROM evidence.eligibility e
    WHERE e.connector_version_id = cv AND e.concept_version_id = concept AND e.recorded_at <= at
      AND (NOT e.applicability ? 'subject_ids' OR e.applicability->'subject_ids' ? subject::text)
      AND (NOT e.applicability ? 'years' OR extract(year FROM period_start)::int
             BETWEEN (e.applicability->'years'->>0)::int AND (e.applicability->'years'->>1)::int)
      AND (NOT e.applicability ? 'classifications' OR classification IS NULL
             OR e.applicability->'classifications' ? classification)
    ORDER BY e.recorded_at DESC, e.id DESC LIMIT 1), false)
$$;
```
Pass `lower(support.valid_range)::date` and `source_flags->>'classificationCode'`. Any unknown applicability key
should make the decision non-matching (fail closed), so a new scope dimension cannot be silently ignored.

### R-08 — `recorded_at` is caller-controlled (VERIFIED)
`recorded_at` is only a DEFAULT. The worker can insert an observation and an eligibility decision with
`recorded_at = 2001-01-01`, and both pass every as-of filter (`selection.py:48`, `check_input`) for any historical
cutoff. That contradicts §6.1 ("recorded_at: server-generated, immutable") and makes strict replay leak-proofing
depend on application discipline.

Evidence (`test_worker_can_backdate_recorded_at`):
```
eligible as of 2005-01-01: [('wb:SP.POP.TOTL:FRA:2020|backdated', '67601110')]
```
Fix: force the column server-side on every scientific table (generate the statements next to `immutable()`).
```sql
CREATE FUNCTION meta.force_recorded_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.recorded_at := clock_timestamp(); RETURN NEW; END $$;
-- for each table with recorded_at:
CREATE TRIGGER force_recorded_at BEFORE INSERT ON evidence.observation
  FOR EACH ROW EXECUTE FUNCTION meta.force_recorded_at();
```
Apply the same to `ops.publication.published_at`.

### R-09 — Relation cardinality bypassed by later participant inserts (VERIFIED)
`world.check_relation_cardinality` (`0003_belief_graph.py:243-258`) is a deferred trigger on `world.relation`
INSERT only. A participant added in a later transaction is checked only by `world.check_participant`
(role and kind, not count). Evidence (`test_relation_participant_added_after_commit`; the signature says `role.reporter max 1`):
```
participants now: [(0,'role.reporter','France'), (1,'role.partner','Azerbaijan'), (2,'role.product','HS 2709…'), (3,'role.reporter','Algeria')]
```
Fix: seal relations to their creating transaction and re-check cardinality.
```sql
-- in world.check_participant():
IF (SELECT xmin::text FROM world.relation WHERE id = NEW.relation_id) <> pg_current_xact_id()::xid::text THEN
  RAISE EXCEPTION 'relation % is sealed; participants must be inserted with the relation', NEW.relation_id;
END IF;
```
Also add a deferred constraint trigger on `world.relation_participant` that runs the same cardinality check for `NEW.relation_id`.

A related latent gap: when a role's `entity_kinds` is absent, `NOT (NULL ? kind)` evaluates to NULL and the check
passes, so any entity kind is accepted. Use `COALESCE((sig->'roles'->role_key->'entity_kinds') ? kind_key, false)`.

### R-10 — Comtrade partner codes are many-to-one aliased to country entities (VERIFIED)
`cli/seed.py:96-117` maps every partner whose `PartnerCodeIsoAlpha3` is a seeded country to that entity:
- 250 "Metropolitan France" and 251 "France, Monaco" both map to FRA;
- 840, 841 ("USA and Puerto Rico (…1980)") and 842 ("USA, PR, USVI") all map to USA.

The reference file has 15 such ISO3 duplicates. These aliases are `provider_declared`, which `resolve_alias` accepts.
Bilateral relations with different territorial definitions then point at one entity. That breaks invariant 23 and
the repository's own note in `registry/entities/countries.yaml`.

Evidence (`test_comtrade_partner_codes_alias_same_entity`):
```
[('250','France','provider_declared'), ('251','France','provider_declared'), ('840','United States',…), ('841',…), ('842',…)]
```
Fix: only reuse the country entity for the code that is already the reviewed reporter alias (251, 842, 156, 12).
Give every other code its own `comtrade_partner:<code>` entity, related to the country through a reviewed crosswalk.
```diff
-        if iso3 in by_iso:
-            eid = by_iso[iso3]
+        reviewed = {str(a["key"]) for e in reg.entities for a in e["aliases"] if a["provider"] == "comtrade_reporter"}
+        if iso3 in by_iso and code in reviewed:
+            eid = by_iso[iso3]
         else:
             eid = entity_id(f"comtrade_partner:{code}")
```
Also honour `entryExpiredDate` (841 expired in 1980) by setting `world.alias.valid_range`.

### R-11 — GC can delete bytes that a concurrent publication is reusing by content address (VERIFIED)
`FilesystemArtifactStore.put` (`objects/filesystem.py:45`) and `S3ArtifactStore.put` return success without
touching an object that already exists. Its age (mtime/LastModified) therefore stays old, and nothing records that
a publication is about to reference it. `operations/gc.py` re-checks references only against committed rows.

Interleaving:
1. A publication crashes and leaves orphan X (age > 7 days).
2. GC scan 1 runs.
3. A new acquisition of identical bytes calls `put` (exists, verified), then
4. GC scan 2 deletes X, then
5. the acquisition commits `artifact_location` for X.

The committed "verified" artifact now has no bytes, which breaks prompt §4 check 11 and invariant 1.

Evidence (`test_gc_race_with_content_addressed_reuse`, a deterministic interleaving):
```
mtime after re-put still old: True
gc deleted: ['file://…/normalised/sha256/74/58/7458…'] | committed artifact verified: True | file exists: False
```
Fix (needs the lead's store and agent D's GC):
- Refresh age on reuse: `os.utime(final)` in the filesystem `put`. For S3, a `copy_object` with `MetadataDirective='REPLACE'` onto itself, or an `ops.object_lease(sha, until)` row written in a committed autonomous transaction before `put` returns.
- Exclusion: publishers hold `pg_advisory_lock_shared(GC_LOCK)` (session-level) from the first object write until commit. `gc_scan` takes `pg_try_advisory_lock(GC_LOCK)` around the re-check and delete, and re-stats mtime immediately before deleting.

### R-12 — SafeHttpClient DNS TOCTOU: the vetted address is not the connected address (VERIFIED)
`check_url` (`safe_client.py:136-158`) resolves through `self.resolver` and vets those addresses. httpx then
resolves the host again, independently, at connect time (`_client.stream`, line 218). A rebinding answer, or a
split-horizon/hosts entry for an allow-listed host, is never checked. In production `trust_env=True` (line 124),
so with `HTTPS_PROXY` set the proxy does its own resolution and the local vetting is only advisory.

Evidence (`test_dns_toctou_connects_to_unvetted_address`):
```
vetted address 93.184.216.34, actual TCP connection accepted on 127.0.0.1:443 from [('127.0.0.1', 32800)]
```
Fix: connect to the vetted IP. Implement an `httpcore.AsyncNetworkBackend` whose `connect_tcp(host, port)`
resolves, vets every address, then connects to the vetted address (TLS SNI and Host remain the hostname), and pass
it as `httpx.AsyncHTTPTransport(...)` with `trust_env=False`. If an egress proxy is mandatory, document that SSRF
enforcement moves to the proxy (allowlist plus private-range deny) and make the allowlist the proxy's job too.
As a minimum interim check, before reading the body:
```python
peer = resp.extensions["network_stream"].get_extra_info("server_addr")
if peer and not address_is_public(peer[0]):
    raise UnsafeRequest("connected to non-public address")
```

### R-13 — Decompression memory blow-up before the size limit (VERIFIED)
`_get_once` iterates `resp.aiter_bytes()` (line 233), and httpx decodes each raw chunk without `max_length`.
The limit is checked only after a decoded chunk exists. With `Content-Encoding: gzip, gzip`, which httpx
accepts although only `gzip, deflate` was requested, a 64 KiB raw chunk can expand by roughly 1000×1000.
The ratio check reads `num_bytes_downloaded`, but it runs after allocation.

Evidence (`test_nested_gzip_memory_before_limit`, limit 5 MB):
```
wire bytes: 661 …  -> 'response exceeds limit 5000000 bytes (decoded)'; python peak allocation during request: 631 MB
```
Scaling the inner payload makes the worker run out of memory. Only allow-listed hosts (or their CDNs) can serve
this, hence Medium.

Fix: read raw bytes and decode incrementally with a bound.
```python
codings = [c.strip() for c in resp.headers.get("content-encoding", "identity").split(",") if c.strip()]
if len(codings) > 1 or codings[0] not in ("identity", "gzip", "deflate"):
    raise UnsafeRequest(f"unsupported content-encoding {codings}")
d = (
    zlib.decompressobj(16 + zlib.MAX_WBITS if codings[0] == "gzip" else zlib.MAX_WBITS)
    if codings[0] != "identity"
    else None
)
async for raw in resp.aiter_raw():
    buf = raw if d is None else d.decompress(raw, limit - total + 1)
    while True:
        total += len(buf)
        if total > limit:
            raise UnsafeRequest(...)
        chunks.append(buf)
        if d is None or not d.unconsumed_tail:
            break
        buf = d.decompress(d.unconsumed_tail, limit - total + 1)
```

### R-14 — Post-publish steps are not atomic with the acquisition and never retried (VERIFIED)
`evidence/runner.py:237-238` calls `post_publish` (`world/space.publish_display_geometries`,
`evidence/cards.publish_study`) after the acquisition transaction committed, and only when
`not summary.duplicate_acquisition`. If the step crashes, the rerun sees a duplicate acquisition, skips the step,
reports `unchanged`, and the cursor completes. The geometries or study never appear and health reads "healthy".

Evidence (`test_post_publish_failure_is_never_retried`):
```
retry outcomes: ['unchanged'] post_publish calls: 1 acquisitions: 1 display geometries: 0
```
Fix: run post-publish inside the acquisition transaction. Pass `conn` into `publish_acquisition(..., post_publish=fn)`
and invoke it before commit; both functions already write objects first, which is allowed before commit.
Alternatively, make it idempotent and re-run it for duplicates:
```diff
-            if post_publish is not None and summary.namespace == "raw" and not summary.duplicate_acquisition:
+            if post_publish is not None and summary.namespace == "raw":   # idempotent: ON CONFLICT DO NOTHING / hash compare
                 post_publish(engine, store, raw, batches, summary)
```

### R-15 — Applied migration edited in place (VERIFIED from git history)
`git diff 0b233dd HEAD -- migrations/versions/0004_models_sim.py` adds `'cache_miss'` to the
`sim.run_outcome.status` CHECK inside 0004. Databases already at head (the dev DB `reva`, and any deployed
database) never re-run 0004. They keep the old constraint and reject `cache_miss`, while fresh test databases accept it.
Fix: restore 0004 and add `0008_run_outcome_cache_miss.py`:
```sql
ALTER TABLE sim.run_outcome DROP CONSTRAINT run_outcome_status_check;
ALTER TABLE sim.run_outcome ADD CONSTRAINT run_outcome_status_check
  CHECK (status IN ('succeeded','failed','unsupported','missing_input','cache_miss'));
```
Add a CI guard that fails when a migration file already present on the main branch changes.

---

## Medium-Low

### R-16 — The S3 store "verifies" against its own metadata (VERIFIED with a fake client)
`_head_key` (`objects/s3.py:100-101`) returns `Metadata["reva-sha256"]`, a value REVA wrote, and `verify()` compares
it with itself. When the object already exists (HEAD hit, or `PreconditionFailed` on `IfNoneMatch`), its bytes are
never checked. A corrupted object, or one uploaded by older code or a tool with matching metadata, is accepted and
its location is recorded as checksum-verified. `IfNoneMatch` also needs botocore ≥ 1.35 and provider support:
S3-compatible stores that ignore it will overwrite. The prior HEAD narrows that window but does not close it.

Evidence (`test_s3_put_trusts_metadata_of_existing_object`):
```
put() reported verified object: True
but get() -> IntegrityFailure
```
Fix:
```diff
-            r = self.client.head_object(Bucket=self.bucket, Key=key)
+            r = self.client.head_object(Bucket=self.bucket, Key=key, ChecksumMode="ENABLED")
 ...
-        meta_sha = (r.get("Metadata") or {}).get("reva-sha256", "")
+        server = r.get("ChecksumSHA256")           # provider-computed (full-object, single-part uploads)
+        meta_sha = base64.b64decode(server).hex() if server and "-" not in server else ""
```
When the server checksum is absent or composite, stream-re-hash the object before recording a location
(bounded by `default_max_bytes`).

### R-17 — Bootstrap leaks the role password through SQL text (VERIFIED)
`cli/bootstrap.py:61-65` formats `ALTER ROLE … PASSWORD 'plaintext'` and executes it as literal SQL. Any failure
(role missing, lock timeout, permission) raises a SQLAlchemy error whose message embeds the statement and password.
The CLI prints that traceback. With `log_statement=ddl`/`all`, `log_min_error_statement` or pg_stat_statements,
the plaintext also reaches server logs. This breaks invariant 31.

Evidence (`test_provision_roles_error_contains_password`):
```
exception contains plaintext password: True
[SQL: ALTER ROLE reva_review_nonexistent_role LOGIN … CONNECTION LIMIT 10 PASSWORD '<PASSWORD>']
```
Fix: send a SCRAM verifier, never the password, and sanitise errors.
```python
def scram_verifier(pw: str, iterations: int = 4096) -> str:
    import base64, hashlib, hmac, os

    salt = os.urandom(16)
    salted = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iterations)
    ck = hmac.new(salted, b"Client Key", "sha256").digest()
    sk = hmac.new(salted, b"Server Key", "sha256").digest()
    b64 = lambda b: base64.b64encode(b).decode()
    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(hashlib.sha256(ck).digest())}:{b64(sk)}"


...
try:
    conn.execute(text(stmt_with_verifier))
except Exception as e:
    raise ConfigurationError(
        f"provisioning {login} failed: {type(e.orig if hasattr(e, 'orig') else e).__name__}"
    ) from None
```
Also require a minimum password length (for example ≥ 24) before provisioning.

### R-18 — `reva_sim` can rewrite provenance of non-simulation activities (VERIFIED)
`0007_security.py:417-420` gives `reva_sim` INSERT on `meta.artifact`, `meta.activity_input/output` and
`meta.artifact_location`. RLS exists only on `activity` (origin), `context` (mode) and `artifact_location` (namespace).
Simulation credentials can therefore:
- attach any artifact as an output of a `source` activity;
- register an extra "verified" location with a fake URI for any artifact (including raw source bytes), under namespace `scenarios`;
- create artifacts with arbitrary `rights` and `schema_version`.

That poisons provenance (§8.3) and `meta.artifact_is_verified`. It is the "accidental-write isolation" that §21
asks the sim role to provide.

Evidence (`test_sim_can_attach_outputs_to_source_activities`):
```
sim attached artifact ffffffff.. as OUTPUT of a 'source' activity; artifact_is_verified=True
sim added a 'verified' location for raw artifact ceaf9aeb
```
Fix:
```sql
ALTER TABLE meta.activity_output ENABLE ROW LEVEL SECURITY;
ALTER TABLE meta.activity_input  ENABLE ROW LEVEL SECURITY;
CREATE POLICY ao_read ON meta.activity_output FOR SELECT TO reva_api, reva_worker, reva_sim USING (true);
CREATE POLICY ao_worker ON meta.activity_output FOR INSERT TO reva_worker
  WITH CHECK ((SELECT origin FROM meta.activity a WHERE a.id = activity_id) <> 'simulation');
CREATE POLICY ao_sim ON meta.activity_output FOR INSERT TO reva_sim
  WITH CHECK ((SELECT origin FROM meta.activity a WHERE a.id = activity_id) IN ('simulation','evaluation'));
-- same pair for activity_input
CREATE POLICY location_sim_insert2 ON meta.artifact_location AS RESTRICTIVE FOR INSERT TO reva_sim
  WITH CHECK (NOT EXISTS (SELECT 1 FROM meta.artifact_location l WHERE l.artifact_sha256 = artifact_sha256
                          AND l.namespace NOT IN ('scenarios','evaluation')));
```
Also stop `reva_sim` from using `origin = 'model'` (reserve it for the worker registry).

---

## Low

### R-19 — The publication lock predicate is too loose (VERIFIED)
`ops.check_publication` (`0003_belief_graph.py:167-170`) checks `classid = 0 AND objid = 7262011` in `pg_locks`.
A session-level `pg_advisory_lock(0, 7262011)`, the two-int4 form with `objsubid = 2`, also satisfies it. A caller
could take a session lock and release it before commit, so sequence order would no longer equal commit order.
`take_publication_lock` uses the correct xact lock today. Evidence: `test_publication_check_accepts_session_lock` → `True`.
```diff
-  IF NOT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()
-                 AND objid = 7262011 AND classid = 0 AND granted) THEN
+  IF NOT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()
+                 AND objid = 7262011 AND classid = 0 AND objsubid = 1 AND granted) THEN
     RAISE EXCEPTION ...
   END IF;
+  PERFORM pg_advisory_xact_lock(7262011);   -- re-entrant; guarantees the lock is held to commit
```

### R-20 — Backfill releases become "current" (VERIFIED, latent)
`release_as_of(conn, None)` (`belief/snapshots.py:13-21`) returns the highest `sequence`. A release published later
with an older `knowledge_cutoff`, such as a replay or backfill, becomes the world API's current release.
All production callers pass `now()` today. Evidence: `test_backfill_release_hijacks_latest` →
`is backfill: True`. Fix: in `ops.check_publication`, reject a cutoff older than the latest published
cutoff of the same origin unless the release is flagged `backfill`, and exclude flagged releases from
`release_as_of(None)`. Separately, `api/routes/world.py:128` calls `release_as_of(conn, None)` without an origin,
so in non-production databases a fixture release can be served as the current world.

### R-21 — The scenario spec's `knowledge_cutoff` is not validated (VERIFIED)
`jsonschema.FormatChecker()` (`simulation/spec.py:112`) silently skips `format: date-time` because
`rfc3339-validator` is not installed. `"infinity"`, `"yesterday"`, `"2026-99-99"` and `"not a date"` all pass
`validate_scenario_spec`. `sim.create_scenario` then casts with `::timestamptz`:
- PostgreSQL accepts `'infinity'` and `'yesterday'`;
- an `infinity` context cutoff then makes every psycopg read of that row fail ("timestamp too large").

`api/routes/sim.py` mitigates with `datetime.fromisoformat`, a timezone requirement and a baseline comparison.
`cli/demo.py` and `simulation/engine.py` rely on the validator alone, and the SECURITY DEFINER function accepts
anything. Fix: add `rfc3339-validator` to dependencies (or parse explicitly in `validate_scenario_spec`).
In `sim.create_scenario`, require `p_spec->>'knowledge_cutoff' ~ '^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:\d\d)$'`,
a finite value, `<= clock_timestamp()` and `>=` the baseline release cutoff.

### R-22 — Canonical hashing pitfalls (VERIFIED)
`kernel/ids.py:50-53` hashes `datetime.isoformat()` with its original offset, so the same instant in `+00:00` and
`+01:00` produces different `logical_hash` and `semantic_hash` values. A parser change in offset rendering then
creates spurious revisions. A mapping `{"$decimal": "1"}` hashes identically to `Decimal("1")`, because the type
tags are not escaped. `Decimal("1.0")` and `Decimal("1")` differ; that is documented, but connectors must keep using `dec_str`.
```diff
-        return {"$datetime": obj.isoformat()}
+        return {"$datetime": obj.astimezone(UTC).isoformat(timespec="microseconds")}
 ...
-            out[k] = _normalise(v)
+            out[("$" + k) if k.startswith("$") else k] = _normalise(v)   # escape user keys colliding with tags
```
This changes hashes, so bump `CANONICAL_HASH_VERSION` to `reva-cjson-2`.

### R-23 — Comtrade valuation inference ignores flow (VERIFIED)
`ComtradeConnector._valuation` (`comtrade.py:206-212`) never uses `flow`. An export row with `cif == fob == primary`
is labelled `CIF`. Evidence: `export with cif==fob==primary -> CIF`. Fix: for `M`, return CIF when
`pv == cif`; for `X`, return FOB when `pv == fob`; when both match, or for the other flow, return
`"primaryValue (basis ambiguous)"`.

### R-24 — Zip members resolved silently (VERIFIED)
`archive.py:35` keys members by basename. With duplicate entries, or `good/ne.shp` plus `evil/ne.shp`, the last one
wins silently. Evidence: `members returned: {'ne.shp': b'DUPLICATE-NAME'}`. Fix:
`if base in out: raise UnsafeRequest(f"ambiguous archive member {base!r}")`.

### R-25 — Settings production validation gaps and redaction (PLAUSIBLE, by reading `settings.py:96-114,169-170`)
Production does not require:
- `REVA_{API,WORKER,SIM}_DB_PASSWORD`;
- `REVA_ADMIN_PASSWORD_HASH`;
- `REVA_DB_SSLMODE in {verify-full, require}`;
- a non-default `REVA_ALLOWED_HOSTS`;
- the absence of `REVA_MIGRATION_DATABASE_URL` at runtime (§21: "removed from the environment before application startup").

It also allows `REVA_CORS_ORIGINS=*` and reads `./.env` in production. `redact_env` misses `PGPASSWORD`,
`AWS_SECRET_ACCESS_KEY`, `*_TOKEN`, `*_KEY` and proxy URLs that carry credentials.
Suggested additions to `_production`: check each of the above, and `if os.environ.get("REVA_PROCESS_ROLE") in ("api","worker") and self.reva_migration_database_url: missing.append(...)`.
Redaction: match by pattern (`(?i)(pass|secret|token|key|credential|dsn|url)$`) instead of an allowlist.

### R-26 — Narrow functions: argument validation gaps (PLAUSIBLE)
- `ops.heartbeat` (`0006_operations.py:233-243`) accepts any `p_lease_seconds`: a negative value expires the lease at once, and 10^9 makes it effectively permanent. Clamp it to `[10, 3600]`.
- `ops.review_proposal` trusts the caller-supplied `p_reviewer LIKE 'human:%'`. The API is the only grantee, so this is acceptable only while the API derives it from the session principal; document that, or pass `session_user`/principal into the audit.
- `submit_job` and `create_scenario` have no payload size bound: add `pg_column_size(p_payload) < 65536`.

### R-27 — Selection semantics (PLAUSIBLE design issues)
- `ELIGIBLE_SQL` (`belief/selection.py:43-52`) filters retractions and ineligible connector versions before
  `row_number()`. Retracting revision 2, or a revision from a non-eligible connector version, silently republishes
  superseded revision 1 as `selected_report`, with no flag. The prompt orders filter-then-select, so this may be
  intended, but the release should carry `discrepancy_flags: ["superseded_revision_selected"]` when `rn = 1` is not
  the lineage's absolute latest revision at the cutoff.
- `belief.check_input`'s prior-estimate branch (`0005:241-246`) checks the cutoff but not origin or publication.
  A live release can cite an estimate from a `test_fixture` or unpublished release. Add
  `IF r.origin = 'live' AND prior_origin <> 'live'` and require an `ops.publication` row.

### R-28 — The acquisition idempotency key ignores `parser_version` (PLAUSIBLE)
`acquisition_idempotency_key` (`evidence/ingest.py:67-69`) and the latest-sha short-circuit treat identical bytes as
a duplicate even when only `spec.parser_version` changed. A parser fix shipped without a connector version bump
never reprocesses captured responses. Either include `parser_version` in the key, or assert in seed/CI that a
`parser_version` change implies a new `ConnectorSpec.version`.

---

## Checked and fine

- **SECURITY DEFINER hygiene.**
  - All seven functions pin `search_path = pg_catalog, pg_temp` (pg_temp explicitly last) and use schema-qualified tables.
  - Runtime roles have no TEMP or CREATE on any schema (`REVOKE ALL ON DATABASE … FROM PUBLIC`, only CONNECT granted).
  - Non-definer trigger functions therefore cannot be hijacked through `search_path`.
- **Immutability.**
  - UPDATE, DELETE and TRUNCATE triggers cover every scientific table and `ops.publication/audit/proposal/review/gate/health`.
  - There is no `ON CONFLICT DO UPDATE` on an immutable table: core code uses only `DO NOTHING` (artifact, location, support, entity, alias, display geometry); `DO UPDATE` appears only on mutable ops tables (dirty_node, cursor, gc_candidate).
  - The deletion bypass requires membership in `reva_restricted_deletion`, which bootstrap verifies runtime roles lack.
- **Revision lineage under concurrency.** A per-lineage `pg_advisory_xact_lock` followed by a fresh READ COMMITTED snapshot is correct. `UNIQUE(source_id, logical_key, revision_no)` and `UNIQUE(supersedes_id)` back it up. A→B→A yields three revisions, and the trigger enforces `supersedes = revision_no − 1`. The only caveat is cross-origin keying (R-04).
- **Publication sequence ordering.** The exclusive xact advisory lock is taken before the `ops.publication` INSERT and held to commit, so identity order equals commit order. Caveats: R-19 (predicate looseness) and R-03 (cutoff and selection outside the lock).
- **As-of filter order.** `recorded_at <= cutoff` is applied before `row_number()`, and retractions are as-of (`r.recorded_at <= cutoff`). Eligibility is evaluated at the cutoff. Caveats: R-03, R-07, R-08.
- **Fixture/live separation at publication.** `check_input` rejects fixture observations in live releases, `assert_publishable` blocks fixture releases in production, and acquisition dedup is per origin. Caveat: lineage dedup (R-04).
- **SafeHttpClient address classification.** On Python 3.12 all of the following are rejected:
  - `64:ff9b::a9fe:a9fe`, `64:ff9b::7f00:1`, `64:ff9b:1::/48`;
  - 6to4 (`2002:7f00:1::1`, `2002:a9fe:a9fe::`) and Teredo;
  - `::ffff:127.0.0.1`, `::127.0.0.1`, `::ffff:169.254.169.254`;
  - CGNAT `100.64/10`, `192.0.0.170`, `0.0.0.0`, `fe80::1%eth0`, `fd00:ec2::254`, `198.18/15`.

  The client also enforces HTTPS only, an exact host allowlist, no userinfo, port 443 only, and literal-IP rejection. Every redirect hop is re-vetted, with a bounded hop count. It checks the declared Content-Length and the decoded-size limit (the memory caveat is R-13), and treats HTML bodies as failures.
- **Zip safety.** The reader blocks path traversal and absolute or backslash names, caps members at 1000 and checks the ratio. It reads `file_size + 1` and compares, so lying headers are bounded, and the CRC is checked at EOF.
- **Filesystem store.** Spooling is bounded with fsync, creation is an atomic `link()`, existing objects are never overwritten, every write is followed by a re-hash, and `delete_orphan` is confined to the root.
- **Comtrade validation.** It checks every fixed dimension (typeCode, freqCode, cmdCode, customsCode, motCode, partner2Code, reporter, flow, partner for totals), period membership, `count == rows`, `rows < maxRecords`, and blocks duplicate `(period, partner)`. A missing flow becomes typed Missing. Null or zero weight becomes Missing with `weight_unusable`. No barrels are derived. Partner totals and bilateral rows use distinct concepts and supports.
- **WDI validation.** It checks per-page row counts against `per_page`/`total`, duplicate keys and unexpected subjects, plus full expected-key coverage for single-page responses (the default `per_page=1000` makes every MVP response a single page). Nulls become Missing, and `lastupdated` is kept as metadata only.
- **Missing values.** The DB CHECK forbids `kind=missing` together with `value`, and `require_number` raises rather than returning zero.
- **Bootstrap quoting.** Role names use `%I`, passwords `%L` (quoting is correct; only exposure is an issue, R-17). Roles are NOINHERIT, NOBYPASSRLS, with a connection limit, and an ownership check runs.
- **Context typing.** `belief.release` requires a belief context with matching origin and cutoff. Scenario, evaluation and population contexts are enforced by trigger, and RLS stops the worker creating simulation contexts and the sim creating belief contexts.
- **Evidence cards.** Coefficients and standard errors require full-text or table spans; LLM extraction is candidate-only (enforced by both a DB CHECK and the model).
