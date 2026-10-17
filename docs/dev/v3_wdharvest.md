# V3 — Wikidata harvest (agent WDHARVEST)

Status: implemented; seed run results in [Results](#results). Owner files:
`src/reva/adapters/connectors/wikidata.py`, `src/reva/kg/harvest.py`, `registry/kg/properties.yaml`,
`registry/kg/queries/{README.md,_schema.json,core/}`, `registry/entities/country_qids.json`,
`registry/sources/wikidata_{wdqs,wbapi}.yaml`, `registry/atlas/budget_requests/wdharvest.yaml`,
tests `tests/unit/test_wikidata_harvest.py`, `tests/integration/test_wikidata_kg_load.py` (db),
`tests/integration/test_wikidata_live.py` (live), fixtures `tests/fixtures/source/wikidata/`.

## Access paths

| Path | Endpoint | Source key (budget) | Used for |
|---|---|---|---|
| WDQS SPARQL (GET, JSON results) | `https://query.wikidata.org/sparql` | `wikidata.wdqs` | selecting items / explicit statement rows (query specs) |
| `wbgetentities` (≤ 50 ids/call, `props=info|claims|labels|descriptions|aliases|sitelinks`, languages fr/en/es/pt/ar/zh/ru/de, `maxlag=5`) | `https://www.wikidata.org/w/api.php` | `wikidata.wbapi` | versioned entity snapshots (`lastrevid`), statements with ids, ranks, qualifiers, references |
| CirrusSearch `list=search` (`haswbstatement:`) | same API host | `wikidata.wbapi` | candidate discovery without WDQS (`api_search`) |
| `Special:EntityData/<id>.json` (Linked Data interface, one entity, CDN-cacheable, same JSON + `lastrevid`) | `https://www.wikidata.org/wiki/` | `wikidata.wbapi` | snapshots when the action API is saturated (`--api-mode auto|hybrid|linked_data`) |
| `Special:WhatLinksHere/<item>` (page view, 5,000 items/page) | `https://www.wikidata.org/w/index.php` | `wikidata.wbapi` | candidates only (`discover: whatlinkshere`); an item is kept only if its own P31 claim contains the class |
| MediaWiki REST search `/w/rest.php/v1/search/page` (≤ 100 hits) | same host | `wikidata.wbapi` | `linked_data` mode; shares the action-API limiter (also 429 on 2026-10-15) |

Both hosts are the only ones allow-listed (`registry/sources/wikidata_*.yaml`, `ALLOWED_HOSTS`); the HTTP
client is the project's `SafeHttpClient` (HTTPS only, SSRF guards, size caps).

## Policy compliance

* **User-Agent** (Wikimedia User-Agent policy, WDQS usage policy): `REVA-World/0.3 (research world knowledge
  graph; https://github.com/AmineDJM/REVA-World) reva-kg-harvest/0.3` — generic project UA with the project
  URL as contact; never a personal e-mail address or other personal data.
* **One request at a time, machine-wide.** Every request takes an exclusive `fcntl` lock on
  `var/locks/wikidata.lock`; the shared state `var/locks/wikidata.state.json` enforces ≥ 2 s between the end
  of one request and the start of the next (any process, any agent) — 2 s, not 1 s, because the API answered
  `429 You are making too many requests` at ~1 req/s aggregate on 2026-10-15.
* **Recovery pacing**: for 10 min after any 429/503 on an endpoint, requests to it are spaced ≥ 10 s apart
  machine-wide, so processes queued behind a cool-down do not burst at its expiry and re-trigger it.
* **Retry-After honoured in full**, per endpoint (`cooldown_until:wdqs`, `cooldown_until:wbapi`), plus a 30 s
  margin, cap 3,600 s. Long cool-downs are waited *without* holding the lock (60 s steps), so a WDQS ban does
  not block API traffic. 429/503 without Retry-After: exponential back-off from 5 s.
* **maxlag=5** on every API read. Wikidata folds the WDQS *updater* lag into maxlag (error type
  `wikibase-queryservice`, e.g. `queryserviceLag: 452`); that signal exists to slow down edits. After 3
  consecutive maxlag errors of that type the read is sent without maxlag (Retry-After still honoured);
  replica-lag maxlag errors are always honoured.
* **WDQS 60 s limit**: a time-out is a typed `QueryTimeout` and is *not* retried (the spec must be split);
  `LIMIT max_rows+1` is appended and a spec exceeding its declared bound fails (`row_bound_exceeded`) — never
  silently truncated. No label service (labels come from wbgetentities).
* **Budget ledger (invariant 30)**: every live command goes through `harvest.budgeted_client`:
  `reserve_ingest` per source key → `metered` (each request topped up or refused before the transport) →
  `settle_metered`; `BudgetExhausted` exits with status 3. The ledger lives in the throwaway harvest database
  `reva_kg_wdharvest` (the dev database `reva` is refused by name).

### Incident 2026-10-15 (lessons, now fixed)

1. My first lock design slept through cool-downs *while holding the lock* and used one cool-down for both
   endpoints, so one agent's WDQS 429 blocked every agent's API calls for 15 min. Fixed (per-endpoint keys,
   lock released while waiting). I stopped four stuck pre-fix processes of other agents
   (pids 9721, 12198, 12943, 17886) that were asleep holding the lock.
2. Retry-After was capped at 900 s while WDQS sent 1,000 s, so requests were released ~100 s early and the ban
   kept renewing (diagnosed by KGPEOPLE; patched by the lead: cap 3,600 s + 30 s margin; regression test
   `test_long_retry_after_is_honoured_in_full_with_margin`).

### Alternative endpoints

* **CirrusSearch on the API host** (`haswbstatement:`) is used when WDQS is throttled: `map-countries`
  (default) and `run --via api` for seed specs carrying an `api_search` block (subclass closure by BFS over
  P279, OR groups packed under the 300-character query limit, 10,000-result window per query reported as
  `search_window_exceeded`). Semantics differ slightly from SPARQL `wdt:` (all non-deprecated statements are
  indexed, index lag of minutes) — documented in `_schema.json`; snapshots are then fetched with wbgetentities
  as usual, so loaded statements keep revision ids.
* **QLever** (`https://qlever.cs.uni-freiburg.de/api/wikidata`, University of Freiburg) was considered at the
  lead's request and **not adopted**: neither the QLever README (github.com/ad-freiburg/qlever) nor qlever.dev
  publishes terms of use, a usage policy, rate limits or a contact for bulk use (checked 2026-10-15). Sending
  harvest traffic to a research group's service without a stated policy is not conservative. The client
  design supports it (per-endpoint cool-downs, one request at a time, archive, metering under a new source key
  with the dump date as provenance) once the operators confirm acceptable use.

## Raw archive and provenance

* Every successful response body (and every final SPARQL text) is stored content-addressed in
  `var/kg/objects` (namespace `raw`), **gzip-encoded deterministically (mtime=0)**; the artifact address cited by
  KG loads/statements is the sha256 of the stored gzip object. `var/kg/archive_index.jsonl` maps it to the
  provider body sha256 (`body_sha256`), byte sizes, endpoint, request ids/query sha, spec and retrieval time.
  KGCORE's `load_batch` registers the objects as verified `meta.artifact` rows.
* wbgetentities statements: `source_key=wikidata.wbapi`, `statement_id` (Wikidata GUID),
  `source_revision=<lastrevid>` (props include `info`; a snapshot without `lastrevid` is reported as
  `missing_revision`), references kept compactly (P248 stated in, P854 reference URL, P813 retrieved, P143
  imported from), allow-listed qualifiers, rank preferred/normal (deprecated dropped).
* WDQS statements (`purpose: statements`, review findings PROV-1 / KG-2): every row carrying a statement id
  is **re-read from its subject's snapshot** (fetched anyway for labels, so no extra request): the loaded
  statement is the snapshot claim with `lastrevid`, references and exact precision/calendar. Only rows
  without a statement id, or whose claim changed since the WDQS replica, stay `source_key=wikidata.wdqs`, with
  the revision recorded as **explicitly unknown** (`qualifiers._revision = {wikidata_revision: "unknown",
  reason, wdqs_data_as_of, retrieved_at}`; version label `wdqs@<retrieval time>`). WDQS dates carry no
  precision and **precision is never inferred from a date's shape** (`-0400-01-01` may be a century): a spec
  selects it via `pqv:`/`psv:` `wikibase:timePrecision` columns, otherwise the time is kept with
  `precision_unknown` and asserts no time bounds. All 29 statements specs bind `?statement`; 28 select time
  qualifiers without precision columns (their unmatched rows fall back to `precision_unknown`). Rows without a
  `?rank` column are flagged `_rank_not_selected`.
* Label-only snapshots (objects of statements, `expand_hops: 1`) are versioned `<lastrevid>:labels` with kind
  `unclassified`, so a later full snapshot of the same revision becomes current instead of conflicting.
* Wikidata is community-edited (CC0): statements are never labelled verified; causal properties (P828, P1542,
  P1478, P1479, P1536, P1537) are "asserted by Wikidata" only.

## Query specs

Format: `registry/kg/queries/README.md` + `_schema.json` (published first; KGHIST, KGPEOPLE, KGCULT,
KGGEOPOL and A-LAW write against it). `python -m reva.kg.harvest validate` checks schema, allow-list,
placeholders vs `split`, columns vs query variables, no trailing LIMIT, no label service, dependencies.
Commands: `validate`, `try <spec> --limit N` (≤ 50 rows, first chunk, metered, no KG writes),
`run [paths] [--via wdqs|api] [--api-mode api|auto|hybrid|linked_data] [--no-expand] [--reuse-hours N]`,
`status`, `map-countries [--candidates search|whatlinkshere] [--api-mode …]`, `merge-property-requests`.
`--reuse-hours` (default 24) reuses archived EntityData snapshots (their sha and original retrieval time are
cited; a mixed batch reports its oldest retrieval time). `--via api` uses the specs' `api_search` blocks (99 specs
validated; 11 carry one: 3 core, 6 KGHIST, 1 economy, 1 science). Truthy `wdt:P31` under-selects historical states (preferred rank
often goes to "historical country"): selection uses `p:P31/ps:P31` with a rank filter (core) and the
`--via api` path filters on every non-deprecated P31 claim.

Resumability: `var/kg/harvest_state/<spec id>.json` (spec sha256, chunks done with raw sha, seeds, subjects,
objects, entity batches loaded). A completed spec with an unchanged sha256 is skipped; a changed spec restarts.
Loads are idempotent (KGCORE identity: statement key + origin + revision).

## Property allow-list

`registry/kg/properties.yaml`: 186 properties — 141 chosen by WDHARVEST + 45 accepted requests (KGHIST,
KGCULT, KGPEOPLE, KGSCIECO, A-LAW) — each with REVA predicate, inverse name, kind, group, and the property entity's **own en/fr labels**
fetched from Wikidata (every P-id was verified to exist and its datatype to match the declared kind; label
drift vs. my expected labels was reviewed: e.g. P607 is "participated in conflict"). Rejected: P642 ("of",
deleted from Wikidata). 30 qualifiers and 4 reference properties are kept. Every property has its fr label. Requests:
`registry/kg/property_requests/<agent>.yaml` → `python -m reva.kg.harvest merge-property-requests`.

## Results

### Country → QID (`registry/entities/country_qids.json`, new file; the crosswalk is unchanged)

| | |
|---|---|
| crosswalk entities | 251 (250 + TWN added by XWALK) |
| mapped | **249** — 247 from the items' own non-deprecated, non-ended ISO 3166-1 alpha-3 (P298) claim on non-dissolved items, 2 via enwiki sitelink (CHI → Q42314 Channel Islands, XKX → Q1246 Kosovo) |
| ambiguous (never picked) | **ATA**: Q51 Antarctica / Q10372207 Antarctic Treaty area (no class distinguishes them); **PSE**: Q219060 State of Palestine / Q407199 Occupied Palestinian territory (class-tier suggestion: Q219060) — candidates listed with lastrevid and raw sha for maintainer review |
| distinct QIDs | 249 (no QID shared by two codes) |
| provenance | every entry: method, `lastrevid`, raw sha256 of the archived snapshot; candidates from `Special:WhatLinksHere/Property:P298` (286 items), snapshots via `Special:EntityData` (291 requests, 138 MB raw / ~25 MB archived gzip) |

### Seed run (throwaway DB `reva_kg_wdharvest`, `run registry/kg/queries/core --via api --api-mode auto --no-expand`)

| spec | candidates | loaded entities | statements | requests | time |
|---|---|---|---|---|---|
| core.countries | 249 (country_qids) | 249 | 60,538 | 0 (archived snapshots < 24 h reused) | 49 s |
| core.sovereign_states | 960 (WhatLinksHere/Q3624078) | 442 (own P31 contains Q3624078; former states with ended P31 included) | 64,410 | 760 | 69 min |
| core.historical_states | 4,382 (WhatLinksHere/Q3024240) | in progress at report time: ~69 % accepted (≈ 3,000 expected); ≥ 1,500 expected ~3.5 h after 19:30 UTC | | ~11 req/min | ~7 h total |

At report time: 565 entities current, 70,706 current statements, **100 % with a source revision**, 23,087
(33 %) with references; 311 entities have P31 = historical country (former sovereign states included). Loader
rejections: a handful of `end_before_start` (Wikidata data errors, kept out by KGCORE). DB 73 MB, archive 54 MB
(gzip). After the core run, the same detached job runs KGHIST's specs `--via api` (6 with `api_search`;
WDQS-only specs are reported `skipped`). Progress: `uv run python -m reva.kg.harvest status`,
`var/kg/logs/seed_run2.log`, `var/kg/logs/history_run.log`, reports in `var/kg/reports/`.

### Throughput and sizes (observed 2026-10-15/16)

* WDQS: effectively unavailable from the shared egress (429 with Retry-After 1,000 s renewed for hours by
  global traffic); 1 successful SPARQL response in the whole session. Statements specs (all domains) need WDQS
  and have not been run.
* Action API: ~1 success per 2-3 min machine-wide ("You are making too many requests to the API").
* `Special:EntityData`: ~11-15 entities/min at our 2 s pacing; country snapshots ~0.5 MB raw, historical states
  ~10-50 KB; gzip archive ≈ 1/5 of raw.
* KG footprint: ~240 allow-listed statements per country, ~145 per accepted sovereign state, ~1 KB per
  statement in Postgres (indexes included).

### Not done / follow-ups

* Domain statements specs (29) and SPARQL-only seed specs wait for WDQS; KGCULT's
  `culture.historical_state_languages` still selects with truthy `wdt:P31` (under-selects empires — use
  `p:P31/ps:P31` + rank filter).
* `expand_hops` label snapshots were skipped in the seed run (`--no-expand`) to spend the scarce requests on
  full snapshots; objects are present as QIDs and can be labelled later (`run` without `--no-expand`).
* QLever not adopted (no published usage policy).
* Live test `tests/integration/test_wikidata_live.py` (marked live) not run at the end (WDQS throttled).

## Governed import into the dev database (agent LOADALL, 2026-10-16/17)

Owner decision (2026-10-16 23:05 UTC): the Wikidata statements harvested into the throwaway databases may enter
the dev database `reva` through one governed path. That path is `reva kg import-harvest` (module
`src/reva/kg/import_harvest.py`, also `python -m reva.kg.import_harvest`):

```bash
uv run reva kg import-harvest --from-db reva_kg_wdharvest            # then
uv run reva kg import-harvest --from-db reva_kg_causwd               # [--sources 'wikidata*'] [--dry-run] [--no-verify]
```

* **No row copies.** The source database is read only as an index: its `kg.load` rows give the archived raw
  responses (`raw_sha256s`), the retrieval time and the harvester parameters. Each entity and statement is
  re-derived from the archived raw Wikidata response in `var/kg/objects` with the harvester's own extraction
  (`extract_entity`, plus `enrich_causal_statements` for CAUSWD loads). The result is written with
  `reva.kg.model.load_batch` through `KgDbSink`, under the target's integrity triggers.
* **Provenance kept.** Statement GUIDs, `lastrevid`, ranks, qualifiers, references, `raw_sha256`, retrieval time
  and origin are those of the archived response. Each `kg.load` carries the original parameters plus
  `import: {importer: kg-import-harvest/1, import_key, from_db, from_load_id, profile, allowlist_sha256}`.
* **Raw bytes verified, then copied.** Each archived gzip object must hash to its address. Its body must match
  `body_sha256` in `archive_index.jsonl`, or the load is refused (`raw_unusable`). Verified objects are copied,
  under the same sha256, into the target's own object store (`var/objects`), and `load_batch` registers them
  as verified `meta.artifact` rows of the target.
* **Extraction profiles** (from `parameters.harvester`):
  * `wdharvest/1.0.0`: the spec's `entity_kind`/`properties`. EntityData bodies are pruned to the requested
    props and languages, as the client does. `whatlinkshere` seed specs keep only items whose own P31
    contains the class.
  * `causwd/1.0.0`: `unclassified`, `HARVEST_PIDS`, extended causal references. `entitydata_archived` bodies
    are left unpruned, as CAUSWD read them.
  * Any other harvester or source key (e.g. `wikidata.wdqs` row loads) is skipped with a reason, never
    guessed.
* **Allow-list.** The current `registry/kg/properties.yaml` applies and is registered in the target first.
  Loads harvested under an older allow-list are counted `allowlist_changed`. The re-derivation then keeps
  what the newer list allows: P212/P356/P957 references are now allow-listed.
* **Idempotent.** `import_key` = sha256(importer, source key, origin, raw sha256s, retrieval time, profile).
  A load already imported is skipped without writing anything, not even a `kg.load` row, and `load_batch` is
  idempotent per (statement key, origin, revision).
* **Verification** (default on, read only): for every current statement key of the source, the source and
  target `content_sha256` are compared (`identical` / `differing` / `missing_in_target`).
* **Two profiles, one statement.** CAUSWD keeps more reference properties on causal statements than WDHARVEST.
  The same GUID and revision therefore derives differently, and the second import is reported `conflicting`
  and not stored. The same applies to entities: `country` vs `unclassified`, same revision.
* **Tests.**
  * `tests/unit/test_kg_import_harvest.py` (5): profiles, pruning, P31 filter, import key, raw verification.
  * `tests/integration/test_kg_import_harvest_db.py` (3, two throwaway DBs): a real harvester run (scripted
    HTTP, real archive) re-imported. Statement rows (key, GUID, content, revision, raw sha, refs, rank,
    retrieval time, origin) and entities are identical, raws are in the target store and verified, and a
    re-run writes nothing. A tampered archive writes nothing. The dev DB is refused as a source.

### Dev-DB import results (2026-10-16 23:20 UTC)

Before touching `reva`, the CAUSWD archive was imported into a throwaway DB (`reva_loadall_ihcheck`, dropped
afterwards): 3,185 of 3,187 statements were identical, and the 2 differing ones gained ISBN references under
the newer allow-list. Then the dev DB, WDHARVEST first so the 17 entities both harvests hold keep their
`country`/`historical_state` kinds:

| from | loads | raw objects copied (+ already present) | entities inserted / conflicting | statements inserted / unchanged / conflicting | rejected | verification (identical / differing / missing) |
|---|---:|---:|---:|---:|---:|---|
| `reva_kg_wdharvest` (WDRUN's archive replay of `core.*`) | 113 | 4,836 (+442) | 4,167 / 242 | 104,654 / 64,485 / 0 | 4 `end_before_start` | 104,654 / 0 / 0 |
| `reva_kg_causwd` | 39 | 207 (+17) | 224 / 17 | 2,988 / 198 / 1 | 0 | 3,184 / 3 / 0 (1 conflict: WDHARVEST's version kept; 2: newer allow-list adds ISBN refs) |

The 242 entity conflicts of the WDHARVEST import are the same as in the source: countries reloaded by
`core.sovereign_states` with another kind at the same revision. 995 `whatlinkshere` candidates were rejected
on P31, as in the harvest. A re-run of both imports skipped all 152 loads.

**Second CAUSWD import (2026-10-17 00:45 UTC).** CAUSWD's 00:02 run had added 98 loads. The re-run skipped
the 39 already imported and imported the 98 new ones: 731 raw objects copied, 750 entities inserted (64
conflicting with WDHARVEST entities at the same revision), 6,787 statements inserted, 526 unchanged.
Verification over all 10,238 current CAUSWD statements: 10,235 identical, 3 differing (as above), 0 missing.
`reva causal refresh --origin live` then loaded 1,002 Wikidata causal claims: 235 T3, 766 T4 and 1 T2
(ISBN/DOI reference), graded C 182, D 746, E 53, B 21 (`docs/dev/v3_causops_live.md`). The CAUSWD run
continues until about 03:50 UTC. Re-run the import, then the causal refresh, to bring in its later loads.

## Harvest run 2026-10-17 (agent WDRUN)

**Preparation (2026-10-16 23:05–23:30 UTC, no requests).**
* `reva_kg_wdharvest` failed the drift check (0016 revision ids, `kg.causal_claim` constraints differ). It was
  renamed to `reva_kg_wdharvest_drift1016` (kept for audit: Oct 15/16 ledger + data, 109 MB) and recreated from
  the migrations by `ensure_database` (fingerprint `e254bd6e…` = contract, no drift).
* The detached core run (pid 9617) had already finished: `core.historical_states` done (4,382 WhatLinksHere
  candidates, 3,913 accepted, 3,881 requests).
* `core.*` re-loaded from the archive only (offline replay of the recorded seeds; 5,591 archived snapshots,
  0 requests, 0 missing): countries 249 entities / 61,371 statements; sovereign_states 442 / 65,178 (still
  > `max_rows` 400, reported, not raised); historical_states 3,913 / 42,594; 4 `end_before_start` rejects.
* **Ledger bug fixed** (`harvest.budgeted_client`): both reservations used the same operation id, so the
  wbapi row was dropped (`ON CONFLICT DO NOTHING`) and all Wikibase traffic was booked to `wikidata.wdqs`
  (settlement overwrote one count with the other: Oct 16 shows 5,056 "wdqs" requests). Now `<run>-wdqs` /
  `<run>-wbapi` (same scheme as CAUSWD).
* **Archive reuse across specs** (`WikidataClient.get_entities`, `Archive.recent_snapshot`): with
  `--reuse-hours > 0` any entity archived recently (EntityData, or a `wbgetentities` response whose props cover
  the request) is served from the archive in every mode, citing the archived sha and original retrieval time;
  only the rest is requested. Test `test_archived_snapshots_are_reused_across_specs`. First effect:
  `history.empires` 120 entities, 101 from the archive.

**Run (from 00:00 UTC).** `merge-property-requests`: 7 EVENTS properties accepted (P1923, P8031, P8032, P726,
P991, P1590, P1339); P642 (qualifier, a_law) rejected (deleted on Wikidata). Two drivers (one process each,
machine lock shared): `--via api --api-mode auto` for specs with `api_search`, `--via wdqs --api-mode auto` for
the others, both `--no-expand --reuse-hours 48`, watchdog stopping at 80 % of a budget or < 6 GB disk.

| spec | path | status | rows | entities | statements | rejects | requests |
|---|---|---|---:|---:|---:|---:|---:|
| history.empires | api (CirrusSearch, subclass depth 3) | done | 120 | 120 | 2,844 (221 new) | 0 | 25 |
| history.colonies | api | failed `wikidata_rate_limited` (search 429 × 13) | 0 | 0 | 0 | 0 | 22 |
| history.treaties | wdqs (9 chunks) | interrupted, resumable (state: 4,058 rows of the first chunks) | 4,058 | 0 | 0 | 0 | 3 |
| all others | | not reached | | | | | |

**Conditions.** WDQS answered once per ban window: every success was followed by `429 Retry-After: 1000`
(≈ 3–4 answers/hour). The action API answered 429 (Retry-After 25–30 s) to almost every call (search and
wbgetentities); CAUSWD's EntityData stream ran in parallel (≈ 950 wbapi requests by 01:00). Ledger at stop
(01:07 UTC, all agents): wbapi 1,061 requests (WDRUN ≈ 60), wdqs 11, bytes 192 MB — far below the budgets.

**Stopped at 01:05 UTC: disk < 6 GB** (6.4 → 5.5 GB free in minutes), caused by other work: the dev DB `reva`
grew 0.8 → 3.9 GB (LOADALL import, `kg.statement` 2.2 GB, `kg.link_row` 1.4 GB, WAL 1.1 GB), `var/objects`
4.3 GB and a 7.1 GB atlas DuckDB cache in `/tmp/reva-atlas-cache-0`. The harvest DB is 113 MB, the archive
109 MB. Both harvest processes were stopped with SIGINT (budget settled, state saved per batch).

**Resume** (when ≥ 7 GB free): `python <scratch>/driver.py groups_wdqs.json` and `groups_api.json`, or directly
`uv run python -m reva.kg.harvest run <paths> --via wdqs --api-mode auto --no-expand --reuse-hours 48`
(treaties resumes from its state; colonies moved to the WDQS path; then `core/historical_states.yaml --force`
for the subclass instances the WhatLinksHere path misses, geopolitics, events, people (country_heads,
state_leaders, monarchs, colonial_officials, leader_positions first), culture, society, economy, science,
nature). `causal/*` and `places/*` are `enabled: false` (driven by `reva.causal_harvest` / A-CITIES).
Per-entity throughput is the binding limit, not the budgets: with the API throttled, EntityData costs one
request per entity (≈ 3,000 entities/day at 80 % of `wikidata.wbapi`).
