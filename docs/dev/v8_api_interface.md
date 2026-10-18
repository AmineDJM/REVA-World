# V8 API interface (agent V8API) — search, change feed, boards & watchlists

Status: **implemented (2026-10-18)** — report and measurements in `docs/dev/v8_v8api.md`. Shapes below are
the contract for V8SHELL (palette), V8BOARDS (Tableaux + Accueil), V8FICHE, V8COUVERTURE and the other feature
agents. Additive changes only (new optional fields, new kinds); anything breaking goes through this file.

All routes: `/v1/v8/...`, same auth as every `/v1` route (`src/reva/api/auth.py`): admin session cookie (+
`X-CSRF-Token` on POST/DELETE) or `Authorization: Bearer` API token; `read` scope for everything here (boards are
per-principal user state, not science). Responses are the usual REVA envelope (`data`, `context_mode`, `origin`,
`modality`, `warnings`, `cost`, `next_cursor`, `knowledge_cutoff`…); only `data` is described below. Errors are
RFC 9457 problems (`code`, `title`, `detail`). Times are ISO 8601 UTC strings. Decimal values are strings.

Module ownership: `src/reva/api/routes/v8_search.py`, `v8_changes.py`, `v8_boards.py`; logic in
`src/reva/v8/{search,changes,boards}/`; CLI `reva v8 index …` / `reva v8 changes …` in `src/reva/cli/v8_index.py`,
`src/reva/cli/v8_changes.py` (discovered by `reva.cli.main`).

---------------------------------------------------------------------------------------------------------------

## 1. Universal search — `GET /v1/v8/search`

| param | type | default | meaning |
|---|---|---|---|
| `q` | string 1..200 | required | free text, an id (`FRA`, `HGNC:1100`, `Q142`, `CHEBI:45783`, `fq_…`), a code (`FR-IDF`, `FR10`) |
| `kinds` | comma list | all | restrict to search kinds (table below) |
| `limit` | int 1..100 | 20 | max hits overall |
| `lang` | `fr` \| `en` | `fr` | label language preferred for `label` (other language used as fallback) |

Matching (lookup, never identity): normalised text = lower-case, accents stripped (`pétrole` = `petrole`,
`Côte d'Ivoire` = `cote d ivoire`), punctuation → space. Tiers, best first:

1. **exact id / code** (`id` match: authority ids `hgnc:1100`, `chebi:…`, `mondo:…`, `lei:…`, `gbif:…`, Wikidata
   `Q…`, ISO 3166-1 alpha-2/alpha-3/numeric, ISO 3166-2, NUTS, forecast question ids, estimate keys, source keys)
2. **exact label / alias** (whole normalised string)
3. **prefix** of a label/alias (`fran` → France)
4. **word prefix** inside a label (`brut` → « Pétrole brut »)
5. **trigram** containment (typo-light, ≥ 3 characters)

Within a tier the score adds a prior (Wikidata sitelinks, kind weight: countries > indicators > other entities,
forecast questions pending > resolved). A document is only ever one source's record: two records with the same
name are two hits (e.g. « France » the country and « France » a power plant of GPPD). The only merges are
**declared code mappings** (ISO3 ↔ Wikidata QID from `registry/entities/country_qids.json`), never names.

### Response `data`

```jsonc
{
  "query": "pétrole brut",
  "normalized": "petrole brut",
  "hits": [SearchHit, ...],          // ranked, best first, at most `limit`
  "groups": [{"group": "Indicateurs", "count": 7}, ...],   // counts among returned hits, in display order
  "index": {"built_at": "2026-10-18T16:51:23Z", "version": "v8s-1", "documents": 604776},
  "truncated": true                    // more matches existed than `limit`
}
```

`SearchHit` (superset of `PaletteHit` in `ui/src/v8/contract.ts`; the palette can use it as is):

```jsonc
{
  "id": "Q142",                 // the record's own id in its source (ISO3 for kind=country)
  "kind": "country",            // search kind (table below)
  "label": "France",            // in `lang`, else the other language, else the source label
  "sublabel": "Pays · FRA · Q142",   // French: kind label · codes · source / period / status
  "href": "/v8/fiche/country/FRA",   // built with v8Links (contract.ts) — see table
  "score": 0.97,                // 0..1, comparable within one response only
  "group": "Pays & régions",
  "match": "exact",             // id | exact | prefix | word | trigram
  "matched": "France",          // the label / alias / code that matched (may differ from label)
  "entity_kind": "country",     // KG kind for kg entities (drug, gene, taxon, nuts_region, …), else null
  "source": "wikidata",         // source key of the record (kg load source, atlas source, registry …) or null
  "origin": "live",             // live | test_fixture (production serves live only)
  "extra": {"iso3": "FRA", "qid": "Q142"}   // small kind-specific facts (codes, unit, status); optional
}
```

### Search kinds, groups, hrefs

| kind | what | group (FR) | href |
|---|---|---|---|
| `country` | atlas crosswalk countries/territories (ISO3), with KG QID when declared | Pays & régions | `v8Links.entity('country', ISO3)` |
| `region` | ISO 3166-2 subdivisions, NUTS, OECD regions (KG records) | Pays & régions | `v8Links.entity(entity_kind, qid)` |
| `entity` | every other KG record (drug, gene, protein, disease, taxon, facility, event, legal entity, …) | Entités | `v8Links.entity(entity_kind, qid)` |
| `indicator` | atlas catalog concepts (labels fr/en, id, unit) and headline concepts | Indicateurs | `v8Links.series(<first source table>, {concept})` (headline: `{concept, headline: '1'}`) |
| `table` | atlas tables (`source.dataset`, title) | Tables | `v8Links.series('<source>.<dataset>')` |
| `question` | forecast registry questions (`fq_…`) | Prévisions | `v8Links.entity('question', fq_id)` |
| `family` | forecast question families | Prévisions | `v8Links.scoreboard(family_key)` |
| `decision` | reference decisions (PRED-ENGINE) | Décisions | `v8Links.decision(ref_id)` |
| `effect` | effect estimates, « cause → effet » labels | Effets | `v8Links.entity('effect', estimate_key)` |
| `source` | data sources (atlas sources, KG sources, effect/causal sources) | Sources | `v8Links.entity('source', source_key)` |

Fiche consumers: `/v8/fiche/country/FRA` uses ISO3 (atlas dossier key; `extra.qid` gives the KG record when
declared); every other KG kind uses the KG id (`Q…` or `prefix:local`), readable by `/v1/kg/entities/{qid}`.

Errors: `503 search_index_missing` when the index has not been built (`uv run reva v8 index build`); the palette
should show « index de recherche absent » and fall back to `/v1/kg/search`.

Performance target: p95 < 150 ms warm (measured numbers in `docs/dev/v8_v8api.md`).

### Index

Prebuilt SQLite file `var/v8/search_index/search.sqlite` (FTS5 trigram, `remove_diacritics`), built by
`uv run reva v8 index build` from the dev DB (read-only) + registries; ≤ 300 MB. The API opens it read-only.
`GET /v1/v8/search/status` → `data: {built, built_at, version, documents, names, codes, kinds, errors, origin,
path_bytes, popular_names_in_memory}`.

---------------------------------------------------------------------------------------------------------------

## 2. Change feed — `GET /v1/v8/changes`

| param | type | default | meaning |
|---|---|---|---|
| `since` | ISO datetime | now − 7 days | items with `at > since` |
| `until` | ISO datetime | now | items with `at <= until` |
| `kinds` | comma list | all | change kinds (below) |
| `limit` | int 1..200 | 50 | page size |
| `cursor` | opaque | – | `next_cursor` of the previous page (newest → oldest) |

Response `data`:

```jsonc
{
  "since": "...", "until": "...",
  "items": [ChangeItem, ...],       // newest first
  "counts": {"ingest": 12, "kg_batch": 3, ...}   // per kind in [since, until] (all pages)
}
```

`ChangeItem`:

```jsonc
{
  "id": "ingest:atlas.imf.gdd:2026-10-17T21:27:42Z",   // stable, unique
  "kind": "ingest",
  "at": "2026-10-17T21:27:42.43Z",       // server recorded_at
  "title": "IMF — Global Debt Database : 30 187 lignes",   // French, one line
  "summary": "1 table mise à jour (atlas.imf.gdd)",          // French, optional
  "source": "imf",                        // source key or null
  "severity": "info",                     // info | notable | action
  "counts": {"rows": 30187, "tables": 1}, // integers only, kind-specific
  "refs": [{"type": "table", "id": "atlas.imf.gdd"}],       // what changed (used by watch rules)
  "links": [{"label": "Voir la table", "href": "/v8/monde?table=imf.gdd"}],
  "origin": "live",                       // live | test_fixture
  "detail": {}                            // small kind-specific facts (decimal strings)
}
```

`refs[].type` ∈ `table` (atlas `source.dataset`), `concept`, `geo` (ISO3), `entity` (KG id), `question`
(`fq_…`), `family`, `batch` (sha256), `estimate`, `claim_load`, `source`.

| kind | from | refs | severity |
|---|---|---|---|
| `ingest` | `evidence.dataset` rows (atlas tables + other datasets) grouped per source per minute | table, source | info |
| `atlas_snapshot` | `meta.activity` kind `atlas.snapshot` | – | notable |
| `kg_batch` | `kg.load` grouped per source per 10 min (entities/statements from `load_counts.json`, retractions) | source, kg_load | info |
| `causal_claims` | `kg.causal_load` (claims added / retracted in the load) | source, claim_load | info |
| `effects` | `kg.effect_load` + `kg.effect_estimate_retraction` (added / retired) | estimate, source | notable |
| `forecast_batch` | sealed issue batches of the forecast registry | batch, family, question | notable |
| `question_resolved` | resolutions (outcome vs forecasts, score vs the official forecaster when present) | question, family | notable |
| `evaluation` | other evaluation activities (back-tests, registry annotations) | – | info |
| `owner_decision` | items only the owner can decide (licence reviews, keys, accounts, queued loads) | source | action |

Honesty: an item reports what was recorded, with counts read from the DB; nothing is inferred. A resolved
question carries the official forecaster's score only when one was sealed with it, else
`detail.official: "absent"`.

---------------------------------------------------------------------------------------------------------------

## 3. Boards & watchlists — `/v1/v8/boards`

Persisted in Postgres (migration `0020_v8_boards`, tables `ops.v8_board`; written only through
SECURITY DEFINER functions), one owner per board = the authenticated principal's audit identity
(`human:admin`, `token:<label>`). A board document is the same shape as V8BOARDS' zod `Board`
(`ui/src/v8/features/tableaux/model.ts`) plus server fields:

```jsonc
Board {
  "id": "b_0123456789abcdef",          // client-chosen (^[A-Za-z0-9_-]{1,64}$) or server-assigned
  "title": "Énergie",                  // 1..200
  "items": [BoardItem, ...],           // ≤ 200, ordered
  "rules": [WatchRule, ...],           // ≤ 200
  "created_at": "...", "updated_at": "...",
  "version": 3                         // server: optimistic concurrency counter
}
BoardItem = PinItem & {"uid": "i_…", "added_at": "..."}
PinItem =
  {"kind": "entity",   "entityKind": "drug", "id": "chembl:CHEMBL941", "label": "imatinib", "sublabel"?: "…"}
| {"kind": "series",   "concept": "macro.cpi_inflation", "geo": "FRA", "label": "…", "table"?: "imf.weo",
                       "unit"?: "%", "headline"?: true}
| {"kind": "forecast", "questionId": "fq_0123456789abcdef01234567", "label": "…"}
| {"kind": "decision", "ref": "…", "label": "…", "summary"?: "…"}
| {"kind": "note",     "markdown": "…(≤ 20 000)", "label"?: "…"}
WatchRule = {"id": "r_…", "kind": "new_data|revision|resolution|threshold", "item_uid": "i_…",
             "op"?: ">|>=|<|<=", "value"?: "2.5" (decimal string), "created_at": "..."}
```

Validation mirrors `rulesFor` / `ruleProblem` of model.ts (threshold only on series; resolution only on
forecast; new_data on series/entity; revision on series) → `422` with the reason.

| method & path | body | returns |
|---|---|---|
| `GET /v1/v8/boards` | – | `data: {boards: [BoardSummary]}` — `{id, title, n_items, n_rules, updated_at, version}` |
| `POST /v1/v8/boards` | `{title, items?, rules?, id?}` | `201`, `data: Board` (409 if `id` exists) |
| `GET /v1/v8/boards/{id}` | – | `data: Board` (404 if absent or not yours) |
| `POST /v1/v8/boards/{id}` | full `{title, items, rules, version}` | `data: Board` (version + 1); `409 version_conflict` with the current board in `detail` if `version` is stale |
| `DELETE /v1/v8/boards/{id}` | – | `204` |
| `POST /v1/v8/boards/{id}/rules` | `{kind, item_uid, op?, value?}` | `201`, `data: Board` |
| `DELETE /v1/v8/boards/{id}/rules/{rule_id}` | – | `data: Board` |
| `GET /v1/v8/boards/{id}/alerts?since=` | – | `data: {since, evaluated_at, n_rules, alerts: [Alert]}` |

(POST is used for updates because the API's CORS policy allows GET/POST/DELETE only.)

`Alert`:

```jsonc
{
  "rule_id": "r_…", "item_uid": "i_…", "kind": "new_data",
  "at": "...",                                // when the triggering change was recorded
  "message": "Nouvelle donnée : imf.weo (France)",   // French
  "change": ChangeItem | null,                // the change-feed item that fired it
  "observed": {"value": "2.31", "period": "2025", "source": "imf.weo", "unit": "%"} | null,  // threshold only
  "status": "fired" | "not_fired" | "not_evaluable",  // not_fired: threshold evaluated and not crossed
  "reason": null,                             // not_evaluable reasons: no_observed_value, atlas_query_not_ready, item_missing
  "item_label": "Inflation FR"
}
```

Rule semantics (evaluated against the change feed since `since`, default: board `updated_at` − 30 days):
`new_data` fires on an `ingest` touching the item (series: its table / a table of the concept's source order;
entity: a `kg_batch` whose source holds the record); `revision` fires on an `ingest` of a table that was
already present before (a new version of the same table); `resolution` fires on `question_resolved` for the
question; `threshold` compares the latest **observed** value of the series (never a projection) with
`op value` and fires when it holds (with the observed value, period and source), else nothing; a series with no
observed value returns `status: "not_evaluable"` with the reason (never treated as 0).

---------------------------------------------------------------------------------------------------------------

## 4. CLI

```
uv run reva v8 index build [--out var/v8/search_index] [--origin live|any]   # (re)build index + load_counts.json
uv run reva v8 index status
uv run reva v8 index query "<q>" [--kinds …] [--limit 10]                     # same ranking as the API
uv run reva v8 changes tail [--since 2026-10-16T00:00Z] [--kinds …] [--follow]
```
