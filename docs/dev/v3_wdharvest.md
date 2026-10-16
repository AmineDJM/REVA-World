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
