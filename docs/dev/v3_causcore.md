# V3 CAUSCORE — causal claims: storage, loader, grader, graph services, API

Agent CAUSCORE, 2026-10-16. Spec: `docs/roadmap/v3_causality.md`. Interface for other agents (Python API,
JSON shapes): `docs/dev/v3_causal_interface.md`.

**REVA asserts no causation.** It stores what sources assert, checks it against time, counts independent
corroboration, keeps dissent side by side, and grades every claim A–E with declared, versioned rules. Every
answer says "according to <source>" / "selon <source>"; a chain says it is REVA's composition.

## What exists

| Piece | Path |
|---|---|
| Migration (schema `kg`, revises `0014_recorded_at_window`) | `migrations/versions/0015_causal.py` |
| Vocabulary + direction normalisation | `src/reva/causal/vocab.py`, `registry/causal/relations.yaml` |
| Temporal check (precision windows, BCE, Julian) | `src/reva/causal/temporal.py` |
| Grading rules, corroboration dedupe (pure) | `src/reva/causal/grading.py`, `registry/causal/grading.yaml` (version 1) |
| Loaders (KG, datasets T1/T2, candidates T5/T6) | `src/reva/causal/model.py` |
| Grader (DB) | `src/reva/causal/grader.py` |
| Read services (event, causes, effects, why, chain, consistency, candidates) | `src/reva/causal/graph.py` |
| HTTP API (`read` scope) | `src/reva/api/routes/causal.py` (+1 router line in `src/reva/api/app.py`) |
| Real Wikidata fixture + capture tool | `tests/unit/causal/fixtures/wikidata_causal_fixture.json`, `src/reva/causal/fixture.py` |
| Tests | `tests/unit/causal/` (38), `tests/integration/test_causal_store.py` (6), `test_causal_api.py` (7), `test_causal_wikidata_demo.py` (4) |
| Contracts | `migrations/schema_contract.json` (head `0015_causal`, fingerprint `0c9af1ca6e9d917e`, +327 objects), `contracts/openapi.json` (7 `/v1/causal/*` paths) |

Everything stays in schema `kg`, already covered by `APP_SCHEMAS` (schema contract) and `SCHEMAS`
(`scripts/backup_restore.py`): no backup or contract-scope change was needed.

## Storage (`0015_causal`)

| Table | Content |
|---|---|
| `kg.causal_load` | One row per write batch, `kind` ∈ `kg_statements` / `dataset` / `grading` / `candidates`; source key, dataset, origin, raw sha256s (each a verified `meta.artifact`, trigger `kg.check_causal_load`; required for datasets and candidates), retrieval time, KG load watermark, loader version, parameters (codebook, rules version, relations sha256). Server `recorded_at` / `recorded_by`. |
| `kg.causal_claim` | Claim versions (see the interface doc for every column). Identity `claim_key` + origin; one version per load. `reinstates_row_id` marks a claim back after a retraction. |
| `kg.causal_claim_retraction` | `removed_at_source` (complete dataset delivery without it), `source_statement_retracted` (its KG statement left the current view), `no_longer_causal`. |
| `kg.causal_grade` | Grade rows: claim row, `rules_version` + `rules_sha256`, grade, `rule_ids`, temporal check + windows used, corroboration + groups + corroborating keys, `contested_by`, `inputs_sha256`. |
| `kg.causal_candidate` | T5/T6 only, with extractor id/version, verbatim sentence, source revision (required), raw sha256. |
| `kg.causal_review` | Maintainer reviews of claims/candidates. Owner-only insert. |

Views: `kg.causal_claim_current` (+ `review_status`), `kg.causal_grade_current` (+ `graded_at`),
`kg.causal_candidate_current` (+ `promotion_status`).

**Integrity (same patterns as 0009/0011/0012).**

* Append-only (`meta.reject_mutation` / `reject_truncate`) on all six tables, owner included.
* Server `recorded_at` on `kg.causal_load` and `kg.causal_review`; `recorded_by` forced.
* Statement-level `AFTER INSERT` triggers (transition tables) bind every claim / retraction / grade /
  candidate row to its load: same origin, right load kind, inserted **in the transaction that created the
  load** (so the load's `recorded_at` is theirs). Dataset claims must restate the load's source, dataset and
  retrieval time and cite one of its raw artifacts.
* A KG-derived claim (`kg:` key, T2–T4) must restate its `kg.statement` row exactly: key, id, origin,
  source, revision, raw sha256 and retrieval time (`kg.check_causal_claim_rows`). A forged cause/effect on a
  real statement row with a different key is refused (test).
* CHECKs: closed relation vocabulary, `family = prevention` ⇔ `prevented_or_delayed` or polarity
  `inhibits`, T1 needs a codebook, T2 a citation, KG claims are T2–T4 and dataset claims T1–T2, candidates
  only T5/T6.
* Grants: `reva_worker` INSERT on all but `kg.causal_review` (and no USAGE on its sequence); `reva_api`,
  `reva_sim` SELECT; `reva_gate` nothing (no schema usage since 0011); functions revoked from PUBLIC;
  `reva_restricted_deletion` covers the tables. Tests prove api/sim cannot insert, worker and owner cannot
  update/delete/truncate, worker cannot record a review, gate cannot read.

## Loader

* **KG** (`load_kg_claims(engine, origin=None, source_keys=None)`): scans `kg.statement_current` for the six
  causal properties with an entity object, community-edited sources only (`wikidata`, `wikidata.*`; other
  sources deliver through `load_dataset_claims`), ≤ 500,000 statements per origin (a truncated scan retracts
  nothing). Direction normalised (`P828`/`P1478`/`P1479` on the effect, `P1542`/`P1536`/`P1537` on the cause;
  `asserted_on` keeps the side). Keeps statement key/id/row, revision, raw sha256, references verbatim,
  qualifiers; scope from `P276`/`P17`/`P1001` (place), `P518` (part), the statement's own times (period);
  `P1310` (*statement disputed by*) → `disputed_by` (contested). Tier T2 when a reference carries a DOI
  (P356) or ISBN (P212/P957) — not possible with WDHARVEST's current reference allow-list (P248, P854, P813,
  P143) — else T3 with references, T4 without. P1344/P710 are never loaded.
* **Datasets** (`load_dataset_claims`): validation per claim with a reason (never silent); claim key
  `ds:<source>:<dataset>:<id>`; stale deliveries (retrieved before the stored row) change nothing;
  `complete=True` retracts claims absent from the delivery, downgraded to partial when any row was rejected.
* **Idempotency**: classification against the latest stored row of each key — `new`, `revision` (content
  changed), `unchanged`, `reinstated` (latest row retracted), `stale`. Content hash covers every claim column
  except retrieval time, so a new KG statement revision (new statement row) is a new claim version. An
  unchanged reload writes nothing — not even a load row.
* **Candidates** (`insert_candidates`): T5/T6 only, idempotent by (key, content); never read by any claim
  service.

## Grader (`registry/causal/grading.yaml` version 1)

* **Temporal check** — event start = P580, else P585, else P571; end = P582, else P585, else P576. Dates are
  the KG's derived precision windows (`value_time_lo/hi`, read through `to_char(... 'BC')`, astronomical
  years; Julian already converted to proleptic Gregorian; century and coarser widened one unit each side).
  Several values: preferred rank, else the covering window. `consistent` (cause start window entirely before
  the effect's), `inconsistent` (entirely after), `overlapping`, `undetermined`. Dates come from the same
  origin as the claim.
* **Corroboration** — per (cause, effect, family, stance), union-find over upstream keys: `url:`
  (normalised P854: scheme, `www.`, trailing slash, fragment), `item:` (P248), `imported_from:` (P143),
  `doi:`, `isbn:`, `refhash:` (reference minus its retrieval date), `dataset:<source>/<dataset>`,
  `community:<source root>` for unreferenced statements. A community group is not counted beside a
  referenced group of the same source root.
* **Contestation** — opposite stance on the same (cause, effect, family) (symmetric), explicit `contests`,
  `P1310` qualifiers.
* **Grade** — first matching rule (E temporal, E contested-uncorroborated, A, B expert, B T3 corroborated, C,
  D); `rule_ids` = tier, temporal, corroboration, contest facts + the deciding rule, with en/fr reasons.
  The unit tests check the table decides every input combination.
* **Recompute** — `inputs_sha256` over rules version/sha, claim row + content, temporal detail,
  corroboration groups and keys, contesting keys; a row is written only when it differs from the claim row's
  latest grade (unchanged regrade writes nothing, no load row).

## Graph services and API

`/v1/causal/events/{id}`, `/causes`, `/effects`, `/why`, `/v1/causal/chain?from&to&max_hops≤6&min_grade&max_paths≤10`,
`/v1/causal/consistency`, `/v1/causal/candidates`. Envelope as `/v1/kg` (`context_mode=evidence`,
`modality=reported_observation`, `origin` + `fixture_data`, `community_edited`, `claims_not_causation`,
`chain_is_composition`, `unsourced_candidates`, `truncated` warnings). Problems: 404 unknown event (no KG
entity, statement, claim or candidate), 422 invalid id / grade / bounds / origin, 401 without token.

Bounds (review V3 SEC-1): claims lists ≤ 500 (best grades first, `truncated`); chain ≤ 6 hops, ≤ 10 paths,
5 s budget checked per level, 5,000 frontier nodes, 20,000 edges per level, 2,000 enumerated paths;
consistency samples ≤ 200, cycle detection over ≤ 200,000 distinct edges with an 8 s budget (iterative
Tarjan, no recursion); context edges ≤ 50.

Chain semantics: bidirectional BFS from `from` (cause side) to `to` (effect side) over graded `asserts`
claims of the `cause` family with grade ≥ `min_grade`; self-loops skipped; all minimal-length chains
through the meeting nodes, ranked by chain grade (weakest hop) then the sum of hop grades. A hop lists every
claim asserting it; hop grade = best of them. Ungraded and below-grade edges are counted in `search` and the
not-found reason says the filter may hide a chain.

## Demonstration on real Wikidata statements

`tests/unit/causal/fixtures/wikidata_causal_fixture.json`: 29 entities, 99 statements (37 causal) captured on
2026-10-16 via `Special:EntityData` through WDHARVEST's locked client (seeds Great Depression Q8698, Wall Street
crash Q201684, 1918–1920 flu pandemic Q178275, WWI Q361 + one hop), loaded by the real path
(`load_batch` → `load_kg_claims` → `grade_claims`). Results (`test_causal_wikidata_demo.py`):

* Wikidata asserts the crash → Great Depression link twice (P828 on Q8698, P1542 on Q201684), both
  unreferenced: one "Wikidata editors" source group (corroboration 1), T4, **D**; temporal `consistent`
  (crash start 1929-10-24 < Depression start 1929-10-29, day precision).
* 1918–1920 flu pandemic *has cause* WWI (P828, unreferenced): T4, **D**, `consistent` (1914-07-28 <
  1918-01, month precision); *has immediate cause* H1N1 in the immediate lane.
* Chain Sarajevo assassination → WWI → flu: 2 hops, chain grade D, each hop asserted separately (the first
  by both P1536 and P1478).
* WWI *has effect* Russian Empire / Ottoman Empire / German Empire / Austria-Hungary (referenced, T3): the
  effect "starts" (inception 1721, 1299, 1871, 1867) long before 1914 → `inconsistent`, **E**, listed by
  `/consistency` — shown, never dropped (the source means their end, which it did not say).

Fixture grades: 23 D, 10 C, 4 E. No cycles or self-loops in the fixture; synthetic tests cover both.

## Performance (measured)

Throwaway database, synthetic graph: 20,000 entities (80 % dated, BCE–2000), 50,000 causal Wikidata-shaped
statements (half referenced, 1 in 50 into 20 hubs). Shared 4-vCPU host, load average 2–6.

| Measure | Result |
|---|---|
| `load_kg_claims`, 50,000 new claims | 11.0 s |
| reload, unchanged (writes nothing) | 7.3 s |
| `grade_claims`, 50,000 claims | 12.6 s |
| regrade, unchanged (writes nothing) | 7.9 s |
| chain ≤ 6 hops, 30 random pairs | p50 13 ms, p95 23 ms (earlier run under load: p50 220 ms, p95 295 ms) |
| `/consistency` (Tarjan over 50k edges) | 1.0–2.0 s |
| causes / why / event of a hub (~1,000 claims) | 80 / 84 / 138 ms |
| size of the causal tables + indexes | 106 MB for 50k claims + 50k grades |

Python dominates the loader and grader (claim normalisation and hashing; per-claim grade inputs). A full
Wikidata causal refresh (order of 10^5 statements) fits in a worker job of under a minute.

## Honesty and limitations

* Only source assertions become claims. Precedence never creates one; context edges are never counted.
* Wikidata references kept by WDHARVEST are P248/P854/P813/P143 only, so Wikidata never yields T2 today and
  "independent" means *different cited reference*, not verified independence.
* The temporal check uses event *starts*; a claim whose effect is an entity (e.g. "WWI has effect Russian
  Empire") is compared with that entity's inception. That is the source's wording; REVA flags it, it does
  not reinterpret it.
* Grades are about the *evidence for the claim as sourced*, never about the truth of the causal link.
* `review_status` stays `unreviewed` until a maintainer inserts a `kg.causal_review` row as the owner; there is
  no review UI or API yet, and no candidate → claim promotion path (a promoted candidate would have to come back
  through a sourced loader).
* The loader scans per origin in one transaction; at ~10^6 causal statements it would need chunking by key
  range (bound `MAX_KG_STATEMENTS` = 500,000 refuses to retract on a truncated scan).
* Dates come from the same origin as the claim: a live claim on an entity dated only by fixture rows is
  `undetermined`.

## Requests to other owners

1. **Worker / ops (lead):** schedule `reva.causal.refresh(engine)` (KG loader + grader) after Wikidata harvests,
   e.g. as a job kind (needs an `ops.submit_job` allow-list migration like 0013 if submitted through the API).
2. **WDHARVEST:** add P356 (DOI), P212/P957 (ISBN) and P1683 (quote) to the reference allow-list so T2 and
   reference dedupe can use them.
3. **Lead:** `docs/final_report.md` / contract references: schema contract head `0015_causal`, fingerprint
   `0c9af1ca6e9d917e`.
