# KG query specs (Wikidata harvest)

Owner of the format and of the executor: **WDHARVEST** (`src/reva/kg/harvest.py`,
`src/reva/adapters/connectors/wikidata.py`). Domain agents own their directory:

| Directory | Owner |
|---|---|
| `core/` | WDHARVEST (countries, historical states seed) |
| `history/` | KGHIST |
| `people/` | KGPEOPLE |
| `culture/` | KGCULT |
| `geopolitics/` | KGGEOPOL |
| `society/` | A-LAW (legal ages: majority, consent, marriage, candidacy, retirement) |
| `science/`, `economy/`, `nature/**` (domain `geography`) | KGSCIECO |
| `places/**` (domain `geography`) | A-CITIES |

One YAML file = one spec. The machine-checkable contract is [`_schema.json`](_schema.json)
(JSON Schema 2020-12); the harvester validates every spec against it before running anything.

## Fields

| Field | Req. | Meaning |
|---|---|---|
| `id` | yes | `<domain>.<name>`, unique; the resume cursor key. |
| `domain` | yes | equals the directory name. |
| `description` | yes | what the query selects and why. |
| `owner` | no | agent that wrote it. |
| `purpose` | yes | `seed_entities` or `statements` (below). |
| `entity_kind` | no | kind hint for seeded entities / subjects (`country`, `historical_state`, `empire`, `colony`, `person`, `war`, `treaty`, `voyage`, `organisation`, `language`, `religion`, `event`, `era`, …; see schema). |
| `sparql` | yes | the query. **No `LIMIT`, no label service.** |
| `max_rows` | yes | expected upper bound of rows for the whole spec (all chunks). |
| `timeout_seconds` | no | ≤ 60 (default 55). WDQS kills queries at 60 s: split instead. |
| `columns` | statements | SPARQL variable names (without `?`), see below. |
| `split` | no | `null`, `{by: year,…}`, `{by: qid_prefix,…}`, `{by: values,…}`. |
| `expand_hops` | no | `0` (default) or `1`: also fetch label snapshots of object entities. |
| `properties` | no | seed_entities only: keep only this subset of the allow-list. |
| `depends_on` | no | spec ids that must run first. |
| `enabled` | no | default `true`. |

### `purpose: seed_entities`

The query returns `?item` (or the variable named in `columns.item`). Every distinct QID is fetched with
`wbgetentities` (50 ids per call; claims, labels, descriptions, aliases, sitelinks in
fr/en/es/pt/ar/zh/ru/de) and its claims are filtered to the **allow-listed properties** in
[`../properties.yaml`](../properties.yaml). Deprecated-rank claims are dropped; preferred/normal rank,
qualifiers (start/end/point in time + others) and compact references (P248 stated in, P854 reference URL,
P813 retrieved) are kept. Each statement carries the Wikidata statement id and the entity revision id.

```yaml
id: history.empires
domain: history
owner: KGHIST
description: Every item that is an instance of empire (Q48349) or of a subclass of it.
purpose: seed_entities
entity_kind: empire
sparql: |
  SELECT DISTINCT ?item WHERE { ?item wdt:P31/wdt:P279* wd:Q48349 . }
max_rows: 3000
expand_hops: 1
```

### `purpose: statements`

The query returns explicit rows. `columns` maps them:

| key | meaning |
|---|---|
| `subject` | variable holding the subject IRI (required) |
| `property` **or** `property_var` | fixed `Pnnn` for every row, or a variable holding the property IRI |
| `object` | variable holding the object: entity IRI or literal (dates as `xsd:dateTime`) (required) |
| `statement` | strongly recommended: variable bound to the `wds:` statement node (`?s p:P39 ?statement`) — gives the stable statement id and lets the harvester de-duplicate against wbgetentities snapshots |
| `rank` | variable bound to `wikibase:rank` (deprecated rows are dropped anyway; filter them in SPARQL too) |
| `start`, `end`, `point_in_time` | P580 / P582 / P585 qualifier variables |
| `start_precision`, `end_precision`, `point_in_time_precision`, `object_precision` | `wikibase:timePrecision` of those times, selected through the full value node (`pqv:P580`, `psv:Pxxx`). **Without them a WDQS date is stored with `precision_unknown` and asserts no time bounds** — the harvester never guesses precision from a date's shape (`-0400-01-01` may be a century) |
| `qualifiers` | `{Pnnn: variable}` other qualifiers to keep |

The property must be in the allow-list; unknown properties fail validation (ask WDHARVEST — see
"Requesting properties"). Subjects get a label snapshot (no claims); objects too when `expand_hops: 1`.
Rows that carry a statement id are **re-read from the subject's wbgetentities snapshot** (fetched anyway for
labels): the loaded statement is the snapshot's claim, with the entity `lastrevid`, its references and exact
precision/calendar. Rows without a statement id (or whose claim changed since the WDQS replica) are loaded as
WDQS rows: revision recorded as explicitly unknown (`qualifiers._revision`), no references; their provenance
is the WDQS query (sha256 of the final query text), the raw response sha256 and the retrieval time. So: always
bind `?statement`.

```yaml
id: people.heads_of_state_positions
domain: people
owner: KGPEOPLE
description: Holders of head-of-state positions of current countries, with start/end dates.
purpose: statements
entity_kind: person
sparql: |
  SELECT ?person ?statement ?position ?start ?start_p ?end ?end_p ?rank WHERE {
    VALUES ?country { {{values}} }
    ?country wdt:P1906 ?position .
    ?person p:P39 ?statement .
    ?statement ps:P39 ?position ; wikibase:rank ?rank .
    FILTER(?rank != wikibase:DeprecatedRank)
    OPTIONAL { ?statement pqv:P580 [ wikibase:timeValue ?start ; wikibase:timePrecision ?start_p ] }
    OPTIONAL { ?statement pqv:P582 [ wikibase:timeValue ?end ; wikibase:timePrecision ?end_p ] }
  }
columns: {subject: person, property: P39, object: position, statement: statement, rank: rank,
          start: start, start_precision: start_p, end: end, end_precision: end_p}
split: {by: values, source: countries, chunk_size: 25}
max_rows: 60000
expand_hops: 1
```

## Splitting (queries that would exceed 60 s or would return very large results)

| `split.by` | placeholder | behaviour |
|---|---|---|
| `year` (`from`, `to`, `step`, `include_undated`) | `{{year_from}}`, `{{year_to}}` (inclusive ints; negative = BCE) | one request per window; with `include_undated: true` also runs `sparql_undated` once |
| `qid_prefix` (`prefixes`, default 1–9; `variable`, default `item`) | `{{qid_filter}}` → `FILTER(STRSTARTS(STR(?item), "http://www.wikidata.org/entity/Q1"))` | reduces rows per request, not the cost of the pattern |
| `values` (`source`: `countries` \| `spec:<id>` \| `list` + `qids`; `chunk_size` ≤ 500, default 50) | `{{values}}` → `wd:Q142 wd:Q183 …` | for per-entity fan-out (use `VALUES ?x { {{values}} }`) |

`countries` = the 250 crosswalk entities mapped in `registry/entities/country_qids.json`.
`spec:<id>` = the QIDs seeded by another spec (that spec must have run; list it in `depends_on`).

## Rules (enforced)

* The harvester appends `LIMIT max_rows+1` to every request; more than `max_rows` rows in total fails the spec
  with `row_bound_exceeded` (nothing is silently truncated). Set a realistic bound.
* No `SERVICE wikibase:label` — labels come from `wbgetentities` (multilingual, versioned).
* All Wikidata traffic goes through `reva.adapters.connectors.wikidata` which holds a **machine-wide lock**
  (`var/locks/wikidata.lock`): one request at a time across all agents, ≥ 2 s between requests,
  Retry-After honoured per endpoint (a WDQS cool-down does not block the API and is waited *without* holding
  the lock), back-off on 429/503/maxlag. Never call WDQS with curl/httpx directly.
* Every live request is metered by the budget ledger (`wikidata.wdqs`, `wikidata.wbapi`; reserve → meter →
  settle in the throwaway harvest database `reva_kg_wdharvest`, never `reva`). A refused budget exits with
  status 3 (`budget_exhausted`), never a partial success.
* Every raw response is archived content-addressed (gzip, `var/kg/objects`, index `var/kg/archive_index.jsonl`).
* WDQS throttles our shared egress hard (429 with Retry-After of 15 min observed on 2026-10-15): test with
  `try` sparingly, avoid queries near the 60 s limit, and prefer `split` to one huge query.

## Trying a spec (small, LIMITed)

```bash
uv run python -m reva.kg.harvest validate registry/kg/queries            # schema + allow-list checks, no network
uv run python -m reva.kg.harvest try registry/kg/queries/history/empires.yaml --limit 10   # 1 request, prints rows
```

`try` runs the first chunk only with `LIMIT <limit>` (≤ 50), through the locked, budget-metered client, and
writes nothing to the KG (only a ledger row in the throwaway harvest database). Full runs (`run`) are done by
WDHARVEST:

```bash
uv run python -m reva.kg.harvest run registry/kg/queries/core --planned-wdqs 20 --planned-wbapi 400
uv run python -m reva.kg.harvest status          # per-spec resume state (var/kg/harvest_state/)
```

## Requesting properties

Properties outside [`../properties.yaml`](../properties.yaml) are rejected. To request one, add it to
`registry/kg/property_requests/<agent>.yaml`:

```yaml
- property: P1376
  why: capital of (inverse view needed for colonial capitals)
  reva_predicate: capital_of
  inverse: has_capital
  kind: entity   # entity | time | quantity | string | monolingualtext | external_id | url | coordinate
  label_fr: capitale de
  label_en: capital of
```

WDHARVEST merges requests into the allow-list.
