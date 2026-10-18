# V8API — shared backend services of the V8 console (search, change feed, boards & watch rules)

Agent V8API, 2026-10-17/18. Contract for the other agents: `docs/dev/v8_api_interface.md` (published first, then
updated to the implementation). All routes need the usual `/v1` auth (`read` scope; POST/DELETE with a cookie
session also need `X-CSRF-Token`, as everywhere).

## What was built

| Piece | Files |
|---|---|
| Universal search `GET /v1/v8/search`, `GET /v1/v8/search/status` | `src/reva/api/routes/v8_search.py`, `src/reva/v8/search/{normalize,hrefs,build,index}.py` |
| Change feed `GET /v1/v8/changes` | `src/reva/api/routes/v8_changes.py`, `src/reva/v8/changes/feed.py` |
| Boards & watch rules `/v1/v8/boards…`, alerts | `src/reva/api/routes/v8_boards.py`, `src/reva/v8/boards/{models,store,alerts}.py`, `migrations/versions/0020_v8_boards.py` |
| CLI `reva v8 index build|status|query`, `reva v8 changes tail [--follow]` | `src/reva/cli/v8_index.py`, `src/reva/cli/v8_changes.py` (discovered by the `reva.cli.v8_*` hook in `main.py`; no edit of main.py by me) |
| Tests | `tests/unit/v8/test_v8api_search.py`, `tests/unit/v8/test_v8api_boards_changes.py` (14 tests), `tests/integration/v8/test_v8api_devdb.py` (5 tests on the dev DB + built index) — all pass |

### 1. Search

* **Index**: one SQLite file `var/v8/search_index/search.sqlite` built by `uv run reva v8 index build` (reads the dev
  DB as `reva_api`, read-only, + registries; atomic replace). Built 2026-10-18: **604,776 records, 964,539 names,
  ~1.1 M identifiers, 302,018,560 bytes (288 MiB)**, build 15 min on a loaded box (load average 12–17; 60 % in the
  KG pass). Kinds: 579,008 KG entities, 9,881 regions (ISO 3166-2, NUTS, OECD TL), 251 countries/territories,
  10,019 indicators (9,992 catalog concepts + 27 headline concepts), 348 atlas tables, 4,738 forecast questions,
  44 families, 5 reference decisions, 301 effect estimates, 181 sources. A failing collector is reported in
  `meta.errors` and the others still build (0 errors).
* **Contents**: fr/en labels and aliases (accent- and case-folded), the record's own id plus readable variants
  (`hgnc:1100`, `CHEMBL941`, `MONDO_…`, ISO 3166-2 / NUTS local codes), identifiers its own source publishes (P297/
  P298/P299, ISO 639, ATC, DrugBank, Ensembl/NCBI gene, Mondo/DOID/UBERON xrefs, IATA/ICAO, InChIKey…), country codes
  (ISO3/ISO2/numeric/M49/World Bank + QID), atlas provider indicator codes (`NY.GDP.MKTP.CD`), concept ids, table
  keys, question ids/keys, estimate keys, source keys.
* **Ranking** (strict tiers): exact id/code → exact label/alias → prefix → word prefix (or all words) → substring /
  fuzzy trigram; within a tier a prior (kind weight, Wikidata sitelinks) then the shorter label. Popular records
  (prior ≥ 0.42: countries, indicators, tables, questions, families, decisions, effects, sources, well-known KG
  items; 91 k names) live in memory (sorted names + numpy trigram postings, loaded from blobs in ~0.25–1.5 s);
  the long tail is read from SQLite B-tree ranges and an FTS5 trigram index. Lower tiers are only read when the
  better ones hold fewer than `limit` hits.
* **Honesty**: lookup, not identity — « France » the country and « France » a GPPD power plant are two hits; the only
  merge is the declared ISO3 → QID map (`registry/entities/country_qids.json`). Every response carries the
  `lookup_not_identity` warning. Decisions are labelled « scénario non validé », effects « attribuée ».
* **Hrefs**: `v8Links` scheme (Python mirror in `hrefs.py`): countries `/v8/fiche/country/FRA`, KG records
  `/v8/fiche/<kg kind>/<id>`, questions `/v8/fiche/question/fq_…`, effects `/v8/fiche/effect/<key>`, sources
  `/v8/fiche/source/<key>` (all accepted by V8FICHE's `/v1/v8/entity/{kind}/{id}`), indicators/tables
  `/v8/monde?table=…&concept=…`, families `/v8/scores?track=…`, decisions `/v8/decision?ref=…`.
* Missing index → `503 search_index_missing` (palette shows it; V8SHELL falls back to `/v1/kg/search`).

**Latency** (in-process HTTP through the full `create_app()` stack incl. middleware; dev box at load average 12–17,
20 queries × 5 after one warm pass, n = 100): **p50 8.6 ms, p95 24.1 ms, max 53.5 ms** (an earlier run: p50 3.5, p95
26.1). Index-only p95 assertion in the integration test (< 150 ms) passes. First request in a fresh process ≈ 1–1.5 s
(loading the in-memory layer). Typical queries (top hits):

| query | p50 ms | top hits |
|---|---|---|
| France | 4.3 | FRA (pays, exact) · GPPD « France » (installation, exact) · régime de Vichy |
| pétrole brut | 4.3 | famille « Production mondiale de pétrole brut » · indicateurs exportations/importations de pétrole brut (EIA) |
| FRA | 3.3 | FRA (id) · Glottolog French (ISO 639 `fra`) · Frankfurt airport (IATA FRA) |
| imatinib | 7.7 | ChEMBL CHEMBL941 · eEML medicine 95 · IMATINIB MESYLATE |
| inflation | 5.3 | Q35865 inflation · headline « Inflation (IPC) » · familles IPCH / WEO |
| HGNC:1100 | 3.0 | BRCA1 (id) |
| Brent | 2.7 | ISO 3166-2 GB-BEN, OECD UKI72 (exact « Brent ») · familles et indicateur prix du Brent |
| OPEP | 5.7 | décision « OPEP+ réduit sa production… » (the OPEC Wikidata item is not loaded) |
| paludisme | 4.7 | Q12156 paludisme · formes de paludisme |
| Ghawar | 4.0 | *no hit*: not in any loaded source (honest empty) |
| Allemagne / chômage / malaria / CHEMBL941 / Q142 / cote d'ivoire / BRCA1 / petrol / OPEC | 2.5–7.9 | DEU · WDI chômage series · DOID/Mondo/EFO/ICD-11/MeSH malaria · CHEMBL941 · FRA (QID) · CIV · hgnc:1100 · EIA petroleum table · OPEP+ decision |

Known misses: typo tolerance is weak on short words (« Brnet » finds nothing: trigram similarity 0.2); « FR-IDF »
has no record (the KG holds French departments `FR-59`…, not regions).

### 2. Change feed

`GET /v1/v8/changes?since=&until=&kinds=&limit=&cursor=` (newest first, keyset cursor, per-kind counts over the
whole window, French titles, refs for watch rules, links). Kinds: `ingest` (evidence.dataset registrations grouped
per source per 10 min, revisions flagged), `atlas_snapshot`, `kg_batch` (kg.load grouped per source per 10 min;
entity/statement counts from `var/v8/search_index/load_counts.json` written by the index build because those tables
have no load index — loads newer than the last build say « not computed yet »), `causal_claims` (added from the same
file, retracted and graded live), `effects` (added / retired with reasons), `forecast_batch` (forecast + health
registry issue activities, question counts from the sealed batches), `question_resolved` (one item per question:
status, outcome, per-forecaster scores, `official` = the `*.reference_*` forecaster's scores or `"absent"`),
`evaluation` (back-tests, annotations), `owner_decision` (open decisions of V8COUVERTURE's
`registry/coverage/pending.yaml`, dated by its `updated`).

Measured (7-day window, dev DB): 172 ingest groups, 120 KG groups (from 1,271 loads), 73 causal loads, 38 effect
loads, 26 sealed batches, 5 resolutions, 13 evaluations, 68 owner decisions. **Warm uncached 180–370 ms**, cached
(20 s per query) 3–28 ms; first call in a fresh process 7–12 s (atlas source discovery + sealed-manifest cache,
then reused).

### 3. Boards & watch rules

* Migration **`0020_v8_boards`** (applied to the dev DB under `flock /tmp/reva-devdb.lock`; `schema_contract.json`
  regenerated by `scripts/update_schema_contract.py`, `reva db verify-schema` → matches, head 0020): table
  `ops.v8_board (owner, id, title, doc jsonb ≤ 512 KiB, version, created_at, updated_at)`; `reva_api` may only
  SELECT; writes only through SECURITY DEFINER `ops.v8_board_create/update/delete` (principal-checked, search_path
  pinned, revoked from PUBLIC). Owner = the principal's audit identity (`human:admin`, `token:<label>`).
* Board document = V8BOARDS' zod `Board` (items `entity|series|forecast|decision|note` with `uid/added_at`, rules
  `new_data|revision|resolution|threshold`), validated server-side with the same rules as `ruleProblem`; unknown
  fields refused, so a value can never be stored in a board.
* Optimistic concurrency: `POST /v1/v8/boards/{id}` with the `version` read → 409 `version_conflict` with the
  current board. POST is used for updates because the API's CORS allows GET/POST/DELETE only.
* Alerts `GET /v1/v8/boards/{id}/alerts?since=` (default 30 days): series ← ingests of its table or of any table of
  the concept's source order / headline series (new data) or re-registrations (revision); entity ← KG batches of the
  record's source or ingests naming its ISO3; forecast ← `question_resolved`; threshold ← latest **observed** value
  (projections excluded) of the series from the atlas query already open in the API process (never opened or built
  in the request); `fired` / `not_fired` with value, period, source, licence; otherwise `not_evaluable` with the
  reason (`atlas_query_not_ready`, `no_observed_value`, `item_missing`) — never compared as 0.
* Integration test runs the whole CRUD + rules + alerts cycle through the routes inside a rolled-back transaction
  (dev DB keeps 0 boards).

### Strict startup (schema contract)

`migrations/schema_contract.json` was regenerated the documented way (`uv run python scripts/update_schema_contract.py`
on a throwaway `reva_contract_*` database; `--check` → « up to date (6be5d85c323ec63f, head 0020_v8_boards) »). Agents
that saw drift had started their API between the migration being applied and the contract regeneration. Verified on
2026-10-18: `REVA_SCHEMA_CONTRACT=enforce uv run reva api` logs « schema contract verified (6be5d85c323ec63f) » and
`/health/ready` is 200 (migrations 0020_v8_boards); `reva db verify-schema` → match. Test
`tests/integration/v8/test_v8api_schema_contract.py`: the committed contract's head is the latest migration and holds
the `ops.v8_board*` objects; the dev DB passes `assert_schema_contract(mode="enforce")` as `reva_api` (the check the
API runs at startup). Agents can drop `REVA_SCHEMA_CONTRACT=warn`.

## Checks

ruff + mypy clean on my files; `pytest tests/unit/v8/test_v8api_*.py tests/integration/v8/test_v8api_devdb.py` →
19 passed (+2 schema-contract tests); `pytest tests/invariants` → 191 passed, 1 skipped.

## Not done / for the lead

* `contracts/openapi.json` **not regenerated** (`scripts/export_contracts.py` would also capture every other agent's
  in-flight routes); `tests/integration/test_api_contract.py` will flag it until the lead regenerates it once at
  integration.
* The index is a derived artefact: rerun `uv run reva v8 index build` after big loads (it also refreshes
  `load_counts.json`). In production it must be built once per deploy (no network needed; ~15 min on this box).
  Size is 288 MiB (302 MB decimal): to keep growth in check, low-value KG kinds already carry no description.
* Search has no index on the KG statements' free-text values and no OPEC/Ghawar records (not in the loaded sources).
* Feed: no per-cell revision diff (a `revision` means a new version of a table, not a changed value); owner decisions
  carry no per-item date in their registry, so they appear at the registry's `updated` date.
* During this run my `ruff format` invocation once took `src/reva/v8` with an exclude pattern that may have
  reformatted files of other v8 packages (formatting only, no behaviour change); later runs were restricted to my
  paths.
