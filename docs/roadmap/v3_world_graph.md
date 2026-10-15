# REVA World V3 — the whole planet, connected

Owner request (2026-10-15): every country in the world; every domain (history, science, society, culture,
quality of life, politics, geopolitics, borders, area, population, density, minimum and average wages,
unemployment, laws, trade surpluses and deficits, …); everything interconnected "like neurons" — e.g. the
chain Columbus → Spain/Portugal → Tordesillas → Brazil speaks Portuguese, or the US position on European
decolonisation in the 1950s–60s. Research sources relentlessly; when a source lacks a country, look elsewhere.

## Three layers

1. **World knowledge graph (KG).** Entities of every kind (current and historical states, empires, colonies,
   people, events, wars, treaties, voyages, organisations, languages, religions, places, eras) and typed,
   dated, sourced statements between them. Backbone: Wikidata (CC0), enriched by specialised open datasets
   (UCDP conflicts, UN General Assembly votes, CShapes historical borders, membership lists…).
2. **Atlas.** Every indicator for every country and year from every open source: World Bank (all WDI + WGI),
   IMF, UN (WPP, migration, SDG), ILO (wages, minimum wage, unemployment), WHO, UNESCO UIS, UNDP HDI, World
   Happiness Report, OWID compilations, V-Dem, Maddison/Clio-Infra (history back centuries), OpenAlex/Nobel/
   WIPO (science), Constitute/World Bank WBL/SSA (laws), Natural Earth/GeoNames (geography), trade totals.
3. **Interconnections.** Country–country and entity–entity links (borders, shared/official languages, colonial
   ties, trade, migration, alliances, co-membership, conflicts) and a path engine that explains *how* two
   things are connected, hop by hop, each hop citing its statement.

## Honesty rules (in addition to the data constitution)

* **Every statement keeps its source**: Wikidata item/property/statement id, revision id, retrieval time and
  the statement's own references when present; dataset statements keep raw-artifact sha256 + selector.
* **A path is not a cause.** The path engine says "connected via" with the property label of each hop
  ("colonised by", "official language", "participant in", "signatory of"). Causal wording appears only when a
  source asserts it (Wikidata `has cause` P828 / `has effect` P1542 with that statement's provenance), labelled
  "asserted by <source>".
* **Community-edited ≠ verified.** Wikidata statements are labelled as such; deprecated-rank statements are
  excluded; preferred/normal rank recorded.
* **Atlas cells are what a source reports**, shown per source. The "best available" view picks by a declared,
  per-concept source order and always names the source and its flags. No imputation, no blending, no zero for
  missing. A country missing in one source is searched in the others and the result says which one answered.
* **LLM output is never a fact.** An LLM may draft queries or propose *candidate* links extracted from text;
  candidates live in a separate, labelled space and never mix with sourced statements.
* **Reviewed beliefs stay a separate, smaller layer.** Atlas and KG feed the evidence store; only reviewed
  concepts enter belief releases and simulations.

## Platform decisions

* **KG storage**: migration `0009_kg.py` (agent KGCORE): `kg.entity` (qid, kind, labels, descriptions,
  aliases, sitelinks count), `kg.statement` (subject, property, object entity or typed literal, temporal
  qualifiers start/end/point-in-time with precision, other qualifiers jsonb, rank, references jsonb, source
  key, source revision, raw artifact sha256, retrieved_at), `kg.property` (allow-listed properties with REVA
  predicate names, inverse names, kind), all append-only/versioned (new revision = new row; current view
  derived). Bulk load via staging + `INSERT … SELECT` (or COPY) in bounded batches; indexes for neighbour,
  timeline and path queries. API: `GET /v1/kg/entities/{qid}`, `/neighbours`, `/timeline`, `/path?from&to`,
  `/search`.
* **Wikidata access** (agent WDHARVEST): SPARQL (WDQS) with a descriptive User-Agent and contact, one query at
  a time machine-wide (cross-process lock), back-off on 429/503, ≤ 60 s queries split by key ranges; plus
  `wbgetentities` (50 ids per call) for entity snapshots. Raw responses archived content-addressed. Domain
  agents write query specs in `registry/kg/queries/<domain>/*.yaml` (SPARQL + mapping to REVA predicates +
  expected row bound); WDHARVEST executes them. Domain agents may run small LIMITed test queries only through
  WDHARVEST's client (which enforces the lock).
* **Atlas storage** (agent ATLASCORE): an *atlas table* is a content-addressed Parquet dataset registered with
  `register_dataset` (migration 0008 tables) with a fixed long schema:
  `source, dataset, indicator, concept_id (nullable), iso3, entity_qid (nullable), period_start, period_end,
  value (decimal string) | value_text, unit, flags (json), raw_sha256, selector`. Queries via DuckDB over the
  pinned Parquet files. `registry/atlas/catalog/<domain>.yaml` declares concepts (id, labels fr/en, unit,
  definition, domain, source order). Catalog concepts are `provider_declared` (not human-reviewed) and say so.
  API: `GET /v1/atlas/concepts`, `/v1/atlas/countries/{iso3}` (dossier, every domain), `/v1/atlas/values?
  concept&year` (map), `/v1/atlas/coverage`.
* **Budgets**: only ATLASCORE edits and re-pins `registry/policies/budgets.yaml`. Other agents write their
  needs to `registry/atlas/budget_requests/<agent>.yaml`; ATLASCORE merges them. Prefer bulk downloads (one
  request for a whole dataset) over per-indicator APIs.
* **Disk**: the container has ~21 GB free. Budget per agent: ≤ 1.5 GB of archived raw + Parquet; KG database
  target ≤ 6 GB. Before any large download run `df -h /` and stop with a report if < 5 GB would remain. Delete
  scratch copies; never keep two copies of a raw file.
* **Countries**: the canonical list is `registry/entities/country_crosswalk.json` (250 entities) plus
  historical states as KG entities. Every atlas table maps provider codes through the crosswalk; unmapped
  codes are kept and listed, never dropped silently.

## Work split (V3 agents)

| Agent | Owns |
|---|---|
| KGCORE | `migrations/versions/0009_kg.py`, `src/reva/kg/**`, `src/reva/api/routes/kg.py` (+1 router line), KG tests |
| WDHARVEST | `src/reva/adapters/connectors/wikidata.py`, `src/reva/kg/harvest.py`, `registry/kg/properties.yaml`, harvest runs |
| KGHIST | `registry/kg/queries/history/**` (states, empires, colonies, wars, treaties, voyages, independence, eras) |
| KGPEOPLE | `registry/kg/queries/people/**` (rulers and heads of state over time, explorers, scientists, leaders) |
| KGCULT | `registry/kg/queries/culture/**` (languages, families, official/spoken languages, religions, heritage) |
| KGGEOPOL | `registry/kg/queries/geopolitics/**` + connectors for UCDP, UNGA votes, CShapes (memberships with dates, diplomatic relations, conflicts, decolonisation) |
| ATLASCORE | `migrations/versions/0010_atlas.py` (if needed), `src/reva/atlas/**`, `src/reva/api/routes/atlas.py`, `registry/atlas/catalog/_schema.json`, budgets |
| A-WB | World Bank bulk WDI + WGI + other WB datasets |
| A-IMF | IMF WEO, BOP, DOTS (bilateral trade totals), fiscal |
| A-UN | UN WPP 2024, UN DESA migrant stock, UN SDG API, UNHCR |
| A-ILO | ILOSTAT (unemployment, wages, minimum wage, informality, labour force) |
| A-HEALTHEDU | WHO GHO, UNESCO UIS (education, R&D, researchers) |
| A-QOL | UNDP HDI, World Happiness Report, OWID (energy, CO2, many compilations), Gini |
| A-POL | V-Dem, WGI (if not A-WB), IPU Parline, Polity if open |
| A-HISTECON | Maddison Project, Clio-Infra, CShapes border history (with KGGEOPOL), historical population |
| A-SCI | OpenAlex (works per country/year/field), Nobel API, WIPO IP statistics |
| A-LAW | Constitute Project, World Bank Women Business and the Law, SSA "Social Security Programs Throughout the World" (retirement ages all countries), legal ages |
| A-GEO | Natural Earth 10m (area, coastline, land borders adjacency), GeoNames country info, climate normals |
| LINKS | interconnection layer: country–country edges from atlas + KG, co-membership, shared language, colonial ties, trade/migration flows |
| NLQA | natural-language questions → KG/atlas typed queries; answers cite statements, never invent |
| UI3 | `ui/**`: graph explorer ("neurons"), path finder ("comment X est lié à Y ?"), timeline, atlas map for any indicator, country dossier |

Each atlas agent: research sources (licence, coverage, update cadence, bulk endpoints), implement connector(s)
in `src/reva/adapters/connectors/atlas_<source>.py`, catalog YAML, small recorded fixtures, offline tests, one
real live ingest into a throwaway database, a coverage report `docs/dev/v3_<agent>.md` (countries × concepts ×
years, gaps, and which alternative sources fill which gaps).
