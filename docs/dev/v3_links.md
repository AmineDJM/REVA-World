# V3 LINKS — the interconnection layer and the path engine

Owner: agent LINKS. Code: `src/reva/links/` (`model.py`, `taxonomy.py`, `derive.py`, `paths.py`,
`service.py`, `fixture.py`), route `src/reva/api/routes/links.py` (+1 registration line in
`src/reva/api/app.py`). Registry: `registry/links/` (`link_types.yaml` + schema, `hubs.yaml`,
`fixture_entities.yaml`). Tests: `tests/unit/links/`, `tests/integration/test_links_db.py`.

The owner wants "the whole planet connected like neurons": Columbus → Castile/Portugal → Tordesillas →
Brazil speaks Portuguese; the US stance on European decolonisation in the 1950s–60s. This layer turns
sourced statements and atlas rows into **typed, dated, evidenced links**, and finds and explains **paths**
between any two entities, hop by hop, each hop citing its evidence.

## 1. Honesty rules as implemented

| Rule | Where it is enforced |
|---|---|
| No evidence, no link | `Link.__post_init__` raises `NoEvidence` on an empty evidence tuple; `Evidence` refuses a statement without id or an atlas row without `raw_sha256` + `selector`; `derive.assert_all_evidenced` re-checks every derived link (also in the API). |
| A path is not a cause | Hops read "X — <label> → Y". Every path result carries the disclaimer ("Chemin de connexions (« relié via »), pas une explication causale" / "A path of connections, not a causal explanation"); the API puts `path_not_causal` first in `warnings`. |
| Causal wording only when the source asserts it | Only statement edges whose property is causal (P828 has cause, P1542 has effect, and their refinements P1478/P1536/P1479/P1537 listed with them in the V3 honesty rules) get causal wording, always attributed and in the statement's own direction: "selon Wikidata (contribution communautaire), X a pour cause Y". `taxonomy.validate_taxonomy` rejects causal wording anywhere else (including derived-link honesty texts). |
| Community-edited ≠ verified | Evidence from `wikidata*` sources is labelled "Wikidata (community-edited)"; the API adds the `community_edited` warning. Deprecated-rank statements cannot become a `Stmt`. |
| No zero for missing | Atlas derivations skip a missing value (`missing_value`) and a reported zero (`reported_zero`) — neither creates a link; both are counted in the derivation report. |
| Weights only from source quantities | `Link.weight` is a decimal string and is accepted only with atlas-row or dataset-statement evidence (never from a Wikidata edge); LINKS computes no score itself. |
| Undated is not "always" | Statements without dates give `temporal.kind = "undated"`; shared-* links built from an undated statement are flagged `temporal_basis: undated`; the path text says "(non daté dans la source)" / "(undated in source)"; `include_undated=false` drops them. |
| Missing ≠ absent | Every derivation returns `skipped` counts and samples (disjoint periods, hub organisations not expanded, colonies without a polity, rows without counterpart...). |

## 2. Link taxonomy (`registry/links/link_types.yaml`)

Validated by `link_types.schema.json` + semantic checks (`reva.links.taxonomy.validate_taxonomy`: unique
ids, honesty wording, causal flags, weights only on atlas methods, statement-edge properties in the KG
allow-list). Statuses: `derivable` (implemented, inputs come from the KG), `awaiting_source` (implemented
against the interface; the owning agent's data is not delivered yet), `no_open_source`.

| id | scope | dir. | derivation (inputs) | temporal | weight | status |
|---|---|---|---|---|---|---|
| `border_wikidata` | country–country | no | P47 shares border with (land/maritime not distinguished by Wikidata) | interval / undated | – | derivable |
| `land_border` | country–country | no | A-GEO Natural Earth 10m adjacency (`geography.land_border_length`) | snapshot | km if measured | awaiting_source |
| `maritime_neighbour` | country–country | no | Marine Regions EEZ boundaries (CC BY 4.0), not ingested by anyone yet | snapshot | – | no_open_source |
| `shared_official_language` | country–country | no | two P37 to the same language; intersection of periods | interval | – | derivable |
| `shared_language_used` | country–country | no | two P2936 to the same language | interval | – | derivable |
| `shared_language_family` | country–country | no | P37/P2936 languages differ but share an ancestor via P279/P361 (nearest common ancestor = `via`) | interval | – | derivable |
| `colony_of` | entity→entity | yes | subject P31 (P279*) colony Q133156 / protectorate Q164142 / dependent territory Q161243, and P17 or P361 → a polity | P17/P361 qualifiers, else P571..P576 | – | derivable |
| `former_colony_of` | country→country | yes | S replaces/follows (P1365/P1366/P155/P156, ≤ 3 steps) an entity C that is `colony_of` P; `via` = intermediates + C | the colony interval | – | derivable |
| `successor_of` | entity→entity | yes | P1365/P1366 (and P155/P156 between polities) | statement dates | – | derivable |
| `co_member` | country–country | no | two dated P463 to the same organisation; overlap of the memberships; organisations with > 60 members not expanded pairwise | interval | – | derivable |
| `alliance_co_member` | country–country | no | as `co_member`, organisation P31 (P279*) military alliance Q1127126 | interval | – | derivable |
| `co_signatory` | entity–entity | no | two P1891 signatories of the same treaty | signature dates, else treaty P585/P571 | – | derivable |
| `conflict_co_participant` | entity–entity | no | P710 / P1344 / P607 on the same conflict; **sides not stated** | conflict P580/P582 or P585 | – | derivable |
| `conflict_same_side` / `conflict_opposing_sides` | country–country | no | UCDP dyads (side A/B) loaded by KGGEOPOL with a `ucdp:side` qualifier | conflict-year | – | awaiting_source |
| `diplomatic_relation` | country–country | no | P530 | interval / undated | – | derivable |
| `shared_head_of_state` | country–country | no | two P35 to the same person over overlapping periods (personal unions, Commonwealth realms) | interval | – | derivable |
| `shared_currency` | country–country | no | two P38 to the same currency | interval | – | derivable |
| `shared_legal_family` | country–country | no | equal `value_text` of an A-LAW classification concept, same year | snapshot | – | awaiting_source |
| `unga_vote_agreement` | country–country | no | dyadic agreement score as published by the source (e.g. Voeten et al.), KGGEOPOL | snapshot | score | awaiting_source |
| `bilateral_trade` | country→country | yes | IMF DOTS exports (A-IMF), `iso3` → counterpart | snapshot | USD as reported | awaiting_source |
| `migrant_stock` | country→country | yes | UN DESA International Migrant Stock (A-UN), origin → destination | snapshot | persons | awaiting_source |

`statement_edges` lists 100+ KG properties the path engine traverses directly, with French/English labels
for both directions (e.g. P37 "langue officielle" / "langue officielle de"). Properties not listed there
(P31 instance of, identifiers, literals) are never hops.

## 3. Derivations (`src/reva/links/derive.py`)

Pure functions over `Stmt` (the link layer's view of a `kg.statement` / `KgStatement`, built by
`model.stmt_from_any`) and `AtlasRowRef` (an atlas-table row). `KgIndex` indexes statements by property,
subject and object and computes class closures (P31 + P279 chains — the chain statements are evidence
too). `polities()` = instances of state/sovereign state/country/historical country/empire/kingdom/realm/
colony classes plus the countries given explicitly (crosswalk QIDs). `derive_all(statements, focus=...,
atlas_dyadic=...)` runs everything and returns links + a per-type report of what was skipped and why.

Atlas rows: ATLASCORE's long schema has a single `iso3`. Dyadic cells must say which other country they
are about; LINKS reads, in order, a `counterpart_iso3` column, `flags.counterpart_iso3`, or a folded
indicator dimension `|counterpart=<ISO3>` (`partner=`/`destination=`: row iso3 is the subject;
`origin=`: row iso3 is the destination, as in UN DESA tables by destination and origin). Countries the KG
cannot map to a QID keep the node `iso3:<CODE>` (`basis.qid_unmapped`), never dropped.

## 4. Path engine (`src/reva/links/paths.py`)

* **Graph**: in-memory multigraph of statement edges (both directions, inverse labels) and derived links,
  built from Python lists (`build_graph`) or from the database (`graph_from_db` → `KgNeighbourhood`:
  bounded frontier expansion of ⌈h/2⌉ hops around the source and ⌊h/2⌋ around the target over
  `kg.statement_current`, plus classification/date context for derivations, labels from
  `kg.entity_label`; blocked hubs are not expanded, frontiers above `max_rows_per_hop` are flagged).
* **Search**: BFS from the target (≤ `max_hops`), then BFS from the source restricted to nodes that can
  still reach the target (bidirectional pruning), then Yen k-shortest simple paths
  (`networkx.shortest_simple_paths`) on the pruned subgraph. `max_hops` ≤ 6, `k` ≤ 10, expansion budget
  200 000 edges, at most 400 Yen candidates; hitting a bound sets `truncated`.
* **Costs / hub damping** (`registry/links/hubs.yaml`): edge cost = the kind's `path_cost`; entering an
  intermediate node adds its declared penalty (UN 6, UNESCO/WHO/IMF/World Bank 5, continents 3...; QIDs checked against Wikidata labels) plus
  `per_doubling × (log2 degree − log2 64)` for nodes above 64 distinct neighbours. A derived link carries
  the penalties of its `via` nodes (a co-membership *via* the UN is damped like the two hops through the UN),
  and a derived link via a blocked hub is refused like the hub itself. *Blocked* hubs (Earth,
  human, country, sovereign state, state, city, colony, empire, treaty, war, language, Indo-European,
  English...) are never intermediate nodes unless `include_blocked_hubs=true`; excluded properties (P31,
  P21, P106, P101, P30 continent, P2348, P122, P166) are traversed only when named in `kinds`.
* **Filters**: `as_of` year (interval contains it; a dated event happened by then; a snapshot is that
  year) or `window=(from, to)` (overlap); `kinds` / `exclude_kinds` (`P37`, `statement:P37`,
  `link:co_member`, `co_member`); `include_undated`.
* **Explanations**: each hop has French and English text and its evidence (`cite()`:
  `wikidata:Q155$… rev 2550000000 [fixture]`, `imf_dots:<sha12>#<selector>`), e.g.
  `Brésil — langue officielle → portugais (non daté dans la source) [wikidata:Q155$… rev …]`.
  Directed derived links traversed backwards use their inverse label ("ancienne puissance coloniale de").

## 5. Offline fixture (real Wikidata)

`registry/links/fixture_entities.yaml` lists the QIDs; `uv run python -m reva.links.fixture capture
[out.json] [cache_dir]` fetches them with WDHARVEST's `WikidataClient.get_entities` (machine-wide lock
`var/locks/wikidata.lock`, shared cool-downs, Retry-After, maxlag), extracts allow-listed claims with
`extract_entity`/`to_kg_statement`, and keeps statements whose object is in the set plus dates and ISO
codes. Each statement keeps its Wikidata GUID, entity revision id and the raw response sha256; the file is
`origin: test_fixture` (`tests/unit/links/fixtures/wikidata_links_fixture.json`).

Captured on 2026-10-15 in two waves (4 `wbgetentities` batches of ≤ 20 ids; wave 2 added entities found in
wave-1 claims plus three searched QIDs): **76 entities, 544 statements** (P530 87, P31 66, P571 50, P17 40,
P463 29, P37 18, P47 18, P527 16, P2936 16, P1365 12, P1891 3...). All seed QIDs were checked against the
captured English labels (test-enforced). Entity revision ids were read separately with `props=info`
because the client's default `props` omit it (see cross-owner request 8); every entity's `modified` time
precedes the claims retrieval, so the recorded revisions are the ones captured.

What the real statements give (tests `tests/unit/links/test_links_fixture.py`):

* derived from the fixture: 13 `border_wikidata`, 44 `diplomatic_relation`, 81 `shared_official_language`,
  72 `shared_language_used`, 135 `co_member` (16 candidate pairs refused: disjoint membership periods),
  3 `co_signatory` (Tordesillas), 7 `conflict_co_participant`, 4 `colony_of`, 15 `successor_of`,
  6 `former_colony_of` — every one evidenced by statement GUIDs;
* `Brazil —former colony of→ Portuguese Empire (via Colonial Brazil, 1500–1822)`: Brazil P1365 Colonial
  Brazil; Colonial Brazil P31 colony; Colonial Brazil P361 Portuguese Empire [1500, 1822];
* **Columbus chain** (waypoints Columbus → Tordesillas → Brazil → Portuguese, ≤ 3 hops per segment):
  1. Christophe Colomb — personnalité importante → Isabelle la Catholique (non daté dans la source)
  2. Isabelle la Catholique — signataire de → traité de Tordesillas (1494-07-02)
  3. traité de Tordesillas — pays → royaume de Portugal (non daté dans la source)
  4. royaume de Portugal — langue officielle commune (via portugais) → Brésil (non daté dans la source)
  5. Brésil — langue officielle → portugais (non daté dans la source)

  each followed by its citation, e.g. `[wikidata:Q180897$577800bc-… rev … [fixture]]`. Without waypoints the
  cheapest Columbus → Brazil connection is shorter and duller: Wikidata says Columbus spoke Portuguese
  (P1412) and Portuguese has country (P17) Brazil. Tordesillas → Brazil also goes "Treaty of Tordesillas —
  significant event of → Portuguese Empire — country of → Brazil (1500–1815)".
* **US / decolonisation 1950s–60s** (waypoints US → Eisenhower → Suez Crisis → UK):
  "United States — head of government → Dwight D. Eisenhower (1953-01-20–1961-01-20) | Eisenhower —
  conflict → Cold War | Cold War — has part → Suez Crisis | Suez Crisis — participant → United Kingdom".
  That is a chain of connections, **not** a US stance: Wikidata's Suez Crisis lists Israel, the UK, France
  and Egypt as participants (no sides, not the US); UNGA Resolution 1514 (Q2629473) has only its class
  (P31) in Wikidata, so the engine answers `no_traversable_statements`; the US is not connected to
  "decolonization" (Q230533) within 6 hops. The US stance (e.g. its abstention on 1514) is **not
  established** by the loaded sources and is not asserted anywhere.
* Hub damping on real data: without it, "US and UK both members of the United Nations" (derived co_member
  via UN, cost 1.2) would be the top US–UK connection; with the UN penalty (6) the first path is the
  sourced diplomatic relation.
* Time filter: as of 1956 the US — Eisenhower (P6, 1953–1961) — Cold War path exists; as of 1965 it does
  not.

## 6. Recommended API shape

Implemented in `src/reva/api/routes/links.py` (read scope, standard envelope, problems as
`application/problem+json`):

* `GET /v1/links/path?from=Q7322&to=Q5146&k=3&max_hops=4&as_of=1960|from_year&to_year&kind=P37&kind=link:former_colony_of&exclude_kind=…&include_undated=true&include_blocked_hubs=false`
  `&via=Q180897&via=BRA` (≤ 4 waypoints: one path forced through them, each segment searched on its own)
  — `from`/`to`/`via` accept a QID or an ISO3 code (resolved through P298). `data`: `query`, `found`, `reason`
  (`no_path_within_max_hops`, `no_traversable_statements`, `no_path_segment:<a>-><b>`), `truncated`, `explored_nodes`, `paths[]` (`nodes`, `cost`, `hop_count`,
  `text.fr[]`, `text.en[]`, `hops[]` with `from`, `to`, `kind` statement|link, `type` (property or link
  type), `forward`, `causal`, `temporal`, `via`, `weight`/`unit`, `evidence[]` (+`cite`), `sources`,
  `alternatives`, `text.fr`, `text.en`), `labels` (fr/en per node), `disclaimer`. Warnings:
  `path_not_causal` (always first), `community_edited`, `fixture_data`, `truncated`.
Integration test (`tests/integration/test_links_db.py`, throwaway database from `tests/conftest.py`):
the fixture is loaded with KGCORE's `load_batch` (source key `wikidata.links_fixture`, origin
`test_fixture`, the fixture file archived as the raw artifact), then `/v1/links/path` (incl. waypoints),
`/v1/links/BRA` and the service functions read it back from `kg.statement_current`.

* `GET /v1/links/{iso3|qid}?link_type=…&year=1960` — every derived link touching the country (KG
  neighbourhood: borders, diplomacy, languages + genealogy, currency, head of state, memberships,
  treaties, conflicts, succession chains up to 4 steps), each with evidence; `link_types` gives every
  type's status and count (an `awaiting_source` type has no links — that is not "no connection");
  `derivation` reports the skipped candidates by reason.

`/v1/kg/path` (KGCORE) stays the raw shortest-statement BFS; see the cross-owner request below for
letting it delegate to the LINKS engine.

## 7. Coverage limits

* Link quality is bounded by the KG: Wikidata P463 memberships are often undated (→ undated co-membership,
  flagged); P530 diplomatic relations are rarely dated; P47 mixes land and maritime borders; Wikidata has
  no "colony of" property (colonial ties are reconstructed from classification + P17/P361 + succession,
  each step cited); conflict participants carry no sides.
* Atlas-based types (trade, migration, land borders, legal family, UNGA agreement) are implemented and
  unit-tested on rows shaped like ATLASCORE's schema, but no such table exists yet: they produce no link
  until A-IMF / A-UN / A-GEO / A-LAW / KGGEOPOL deliver.
* The DB adapter derives links on the fetched neighbourhood only (focus queries); there is no
  materialised `links.*` table (no migration owned by LINKS). A planet-wide precomputation would need one.
* The "US position on European decolonisation" is answered only with what sources state: Wikidata gives
  participation / significant-person / approval statements (Suez Crisis, Resolution 1514), not a stance.
  The US vote on Resolution 1514 (an abstention, per UN records) is **not** in the fixture: it needs the
  UNGA voting dataset (KGGEOPOL). Until then the engine says "connected via" and nothing more.

## 8. Cross-owner requests

1. **ATLASCORE** (`src/reva/atlas/tables.py`): add an optional nullable column `counterpart_iso3`
   (crosswalk key) to `ATLAS_SCHEMA` for dyadic datasets (additive). Until then LINKS reads the folded
   indicator dimension `|counterpart=<ISO3>` (or `|origin=<ISO3>` for migrant stock by destination). Please
   state the convention in `docs/dev/v3_atlas_interface.md` §1.
2. **A-IMF** (DOTS) and **A-UN** (UN DESA migrant stock): fold the partner country as
   `|counterpart=<ISO3>` (exports reported by `iso3` to counterpart) and `|origin=<ISO3>` (migrants born in
   origin living in `iso3`), mapped through the crosswalk; keep the provider code as `|counterpart_code=`.
   Concept ids used by LINKS: `trade.bilateral_exports_total`, `population.migrant_stock_by_origin`.
3. **A-GEO**: Natural Earth 10m admin-0 adjacency as a dyadic table (`geography.land_border_length`, km
   when measured); maritime boundaries (Marine Regions EEZ v12, CC BY 4.0) if budget allows.
4. **KGGEOPOL**: (a) P463 memberships of the 250 crosswalk countries **with P580/P582** (co-membership needs
   dates); (b) UCDP dyads as statements `country —P607→ ucdp:conflict:<id>` with qualifier `ucdp:side`
   = `A`/`B` and P585 = conflict-year (or point us to the shape you choose); (c) UNGA vote agreement per
   dyad-year as a dyadic atlas table (`geopolitics.unga_vote_agreement`) and, ideally, per-resolution votes
   (`country —reva vote→ resolution` with `yes/no/abstain`) so "US vote on A/RES/1514 (XV)" becomes a
   citable statement.
5. **KGHIST**: make sure the colonies spec keeps P17/P361 with P580/P582 and P1365/P1366 (it does per
   its description) and that historical states seed P31 → historical country (Q3024240) so `polities()`
   recognises them.
6. **KGCORE** (`src/reva/api/routes/kg.py`): optionally add `semantics=links` to `/v1/kg/path`
   delegating to `reva.links.service.path_between(conn, from, to, ...)` (typed hops, hub damping, k
   shortest weighted paths, fr/en explanations) — or keep both and let the UI call `/v1/links/path`.
8. **WDHARVEST** (`src/reva/adapters/connectors/wikidata.py`): `ENTITY_PROPS` lacks `info`, so
   `wbgetentities` returns no `lastrevid` and `extract_entity` stores `source_revision = None` on every
   statement (seen in this capture). Add `"info"` to `ENTITY_PROPS` (one word; `LABEL_PROPS` too).
9. **Lead**: `contracts/openapi.json` must be re-exported (`scripts/export_contracts.py`) after the
   `/v1/links` router registration (one line in `src/reva/api/app.py`).

---

# LINKS2 — country–country "neurons" from atlas snapshots + KG, materialised (agent LINKS2, 2026-10-16)

Owner: agent LINKS2 (second pass on `src/reva/links/**`, `registry/links/**`, `src/reva/api/routes/links.py`,
`tests/unit/links/**`, `tests/integration/test_links_*.py`, migration `0012_links_store`). Supersedes the
`awaiting_source` statuses of §2: every atlas-based type whose data now exists is `derivable`.

## L2.1 What was added

| Piece | Path |
|---|---|
| Atlas derivations over a snapshot's DuckDB file (dyadic kinds + computed UNGA vote agreement, top-k, pair and country reads) | `src/reva/links/atlas_links.py` |
| Dataset-statement derivations (CShapes borders / colonial rule, UCDP sides, UN peacekeeping) with dated `geopol:corresponds_to_iso3` mapping | `src/reva/links/dataset_kg.py` |
| `shared_polity_territory` (KGPOLITY overlay) and `shared_language_glottolog` (KGLANG) — `derive_shared_subject`, bounded; Glottolog genealogy in `shared_language_family` | `src/reva/links/derive.py` |
| Materialised link store: materialiser (worker), readers (API), CLI | `src/reva/links/store.py`, `uv run python -m reva.links.store materialise …` |
| Store tables (append-only, sealed per transaction) | `migrations/versions/0012_links_store.py`, `migrations/schema_contract.json` regenerated (head `0012_links_store`) |
| Country view / pair view (store first, on-demand fallback), predecessor-state links | `src/reva/links/service.py` (`country_view`, `pair_view`) |
| API: `GET /v1/links/{iso3}?kind=&year=&facet=&limit=`, `GET /v1/links/pair?a=&b=…` | `src/reva/api/routes/links.py`, `contracts/openapi.json` re-exported |
| Path engine: stored links as edges, namespaced predicates as hops, ordered hop-type steps, `via_any`/`via_all`, non-transit entity kinds, `max_hops` ≤ 8 | `src/reva/links/paths.py`, `registry/links/link_types.yaml` (`statement_edges`) |
| Tests | `tests/unit/links/test_links_atlas.py` (12), `test_links_dataset_kg.py` (7), `test_links_anchor_chains.py` (22: KGHIST's 20 chains), BCE year test in `test_links_model.py`; `tests/integration/test_links_store.py` (6, own throwaway DB, 7 atlas fixture sources) |

## L2.2 Link types now derived (29 in the taxonomy; `link_types.schema.json` adds `derivation.atlas`, methods `atlas_computed`, `kg_dataset_*`, weight kind `derived_index`, namespaced `statement_edges`)

| id | input | weight | temporal |
|---|---|---|---|
| `land_border` | A-GEO `naturalearth.admin0_10m_borders` (`land_border_km\|partner=`, `flags.partner_iso3`); both directions merged into one undirected link (both rows are evidence; disagreeing rows → no link, counted) | km (1:10m, as measured) | retrieval-day snapshot year |
| `land_border_cshapes` (new) | KGGEOPOL `cshapes:shares_border_with` ∩ `geopol:corresponds_to_iso3` of both territories | – | dated interval (day precision) |
| `colonial_rule_cshapes` (new, directed territory → ruler) | `cshapes:ruled_by` (+status) ; territory named by the country it corresponds to (`basis.territory_mapping`) | – | dependency period |
| `bilateral_trade` | A-IMF `imf.imts` `XG_FOB_USD\|partner=` (exports reported by the exporter) | US dollars as reported | year |
| `bilateral_trade_imports` (new) | `imf.imts` `MG_CIF_USD\|partner=` (imports reported by the importer; exporter → importer). Mirror flows are never merged | US dollars | year |
| `bilateral_trade_baci` (new) | A-TRADE `baci.hs92_pair_*` `X\|partner=…\|section=S` (facet `section=`; `TOTAL` default) | thousand US dollars | year |
| `migrant_stock` | A-UN `un_desa.ims2024` `migrant_stock\|origin=…\|sex=T` (origin → destination) | persons | year |
| `unga_vote_agreement` | **computed** from `unga.votes` + `unga.rollcalls` (below) | `derived_index` share 0–1 | decade (stored) / year (pair view) |
| `conflict_same_side`, `conflict_opposing_sides` | UCDP `ucdp:primary_party`/`ucdp:secondary_party` with qualifier `side` a/b, overlapping conflict-year runs, via the conflict | – | overlap |
| `peacekeeping_contributor_to_host` (new, A-MILITARY) | `unpk:contributed_personnel_to` (country → mission, per year) ∘ `unpk:mission_country` (mission → country), overlapping; linear (≤ 200,000, cap recorded); max personnel kept in `basis`, not a weight | – | overlap |
| `shared_polity_territory` (KGPOLITY) | two countries objects of `geo:covered_territory_of` with the same polity, overlapping runs; polities covering > 60 countries not expanded (recorded), ≤ 200,000 pairs | – | intersection |
| `shared_language_glottolog` (new, KGLANG) | two countries in `glottolog:spoken_in` of the same languoid; languages listed in > 60 countries are hubs (recorded), ≤ 200,000 pairs | – | undated |
| `shared_language_family` | unchanged + Glottolog genealogy (`glottolog:parent`/`in_group`/`family`, entered from a Wikidata language through `glottolog:wikidata_item`) | – | interval |
| Wikidata KG kinds (§2) | unchanged; `border_wikidata` now keeps Wikidata-sourced P47 only (Natural Earth / GeoNames P47 loads would otherwise be mislabelled) | – | – |

Not derived: `maritime_neighbour` (no open source ingested), `shared_legal_family` (A-LAW categorical rows not wired),
treaty co-parties beyond Wikidata P1891 (no A-TREATIES data), factbook partner shares (not needed: IMTS/BACI are the
quantities). WALS/Grambank feature values are text and never become links.

**UNGA vote agreement (labelled `derived`).** For a pair and a period (decade stored; year in the pair view) and an
issue (`all`, or Voeten's keyword codes `co hr me nu di ec`): `share = n_identical / n_common`, `n_common` = roll calls
on which both cast yes / no / abstain (absent and non-member excluded), identical = same position (abstain = abstain
counts). Requires `n_common ≥ 10`, else no link (counted `fewer_than_min_common_rollcalls`). `basis` keeps `n_common`,
`n_identical`, `both_yes/no/abstain`, the roll-call years and method `links-vote-agreement/1`; the response adds the
formula and the caveat ("a share of identical votes, not an alliance, friendship or alignment of interests").
Evidence = the archived RData's raw sha256 + a selector stating the computation (`pair`, `years`, `issue`,
`positions`, `min_common`): reproducible from the archived file. Historical voters keep their own node (USSR Q15180,
never RUS). The pair view can list the per-roll-call positions behind a value (`votes_detail_from/to/issue`, ≤ 200).

Identity: `Link.key` now includes the facet (issue / HS section), so two issues with the same share stay two links.
BCE: `AtlasRowRef.year` / `period_year()` parse the signed astronomical year (`-0043-…` → -43, DuckDB `0044-… (BC)` →
-43); vote-detail dates use `reva.atlas.query.iso_date_sql` (KGBCE request).

## L2.3 Materialised store (`kg.link_materialisation`, `kg.link_row`; migration 0012)

* One materialisation per (atlas snapshot sha256 + origin, KG watermark = max `kg.load.id` + its `recorded_at`, KG origin,
  method `links-materialise/1`, taxonomy sha256); `report` holds per-type counts and skip reasons; written by the worker
  in **one transaction** (statement trigger: link rows must belong to a materialisation created in the same
  transaction — complete or absent). Append-only (`meta.reject_mutation`), server `recorded_at`/`recorded_by`.
  Grants: worker INSERT+SELECT, api/sim SELECT, gate none (0011 already revoked its `kg` usage). In the `kg` schema
  on purpose (schema-contract `APP_SCHEMAS` and the runtime grant model already cover it).
* KG kinds and `land_border` are stored in full; atlas kinds keep, per (country, period, facet) and direction, the
  **union of both endpoints' top-k** (default k = 10) — every country's top-k is complete. Full pair histories are
  re-read from the same snapshot on demand.
* Each row: type, facet, endpoints (QID / `iso3:<KEY>` / dataset entity) + their ISO3 keys, direction, `year_lo/hi`
  (astronomical integers), exact temporal JSON, `weight` decimal string + `weight_num` (ordering only) + unit, `via`,
  evidence (non-empty; `jsonb_array_length ≥ 1` check), basis, origin (`live`/`test_fixture`/`mixed`).
* Readers: `latest_materialisation(live_only=…)` (production serves only live atlas + live KG materialisations),
  `country_rows` (SQL top-N per kind/facet/direction, latest period for snapshot/decade kinds unless `year`),
  `pair_rows`, `rows_touching` (path engine, ≤ 20,000 rows/query, cap reported as truncation), `predecessors` +
  `rows_between_nodes` (pair view: links with the other country's predecessor states through stored
  `successor_of`, each shown with its succession chain).
* Fallback: no materialisation → on-demand (KG neighbourhood derivation + the latest atlas snapshot read for that
  country), same response shape with `mode: "on_demand"`.

## L2.4 API

* `GET /v1/links/{iso3|qid}?kind=…&year=&facet=issue=co|section=XVI&limit=1..10` — `data.mode`
  (`materialised`/`on_demand`), `store` (materialisation id, atlas snapshot, KG watermark, top_k), `by_kind[kind].groups[]`
  (direction `outgoing`/`incoming`/`both`, facet, `period`, `total`, ranked `links` with `text.fr/en`, evidence, `iso3`,
  `weight_kind`), flat `links`, `labels` (KG labels, else crosswalk names), `link_types` (status + stored count), `notes`.
  Warnings: `link_not_causal` first, `derived_index` when vote agreement is present, `fixture_data`/`community_edited`
  per evidence origin. `link_type=` stays as an alias of `kind=`; `qid` is kept for the UI3 schema.
* `GET /v1/links/pair?a=BRA&b=PRT[&kind=…][&from_year&to_year][&votes_detail_from=1960&votes_detail_to=1960&votes_detail_issue=co]`
  — `kinds[kind]` (count, first/last year, every link with evidence), `kinds_without_links` (an absent kind is "no
  link", never zero), `predecessor_links`, `vote_detail`, `store`, `atlas_snapshot`, `truncated`.
* Bounds (SEC-1 style): limit ≤ 10; pair: ≤ 1,200 rows per atlas kind, ≤ 5,000 store rows, 8 s budget checked between
  kinds (truncation listed and warned), vote detail ≤ 11 years and ≤ 200 roll calls; country store read ≤ 20,000 rows;
  path: ≤ 20,000 stored links per query, constrained search ≤ 2,000,000 DFS steps + the 20 s request budget,
  `max_hops` ≤ 8, ≤ 8 ordered steps.
* `/v1/links/path`: hop kinds may be namespaced predicates (`kind=geo:covered_territory_of`); the route now takes
  `steps`, `via_any`, `via_all`, `non_transit` (LINKWIRE, §LW.2).

## L2.5 Path engine and KGHIST's anchor chains

`PathQuery` gains `steps` (ordered hop-kind sets matched in order by distinct hops, greedy subsequence matching —
exact), `via_any`, `via_all` (unordered waypoints; ordered `via` waypoints stay), `non_transit_props` (objects of
these properties — languages, continents, eras — may end a path but are never passed through: `PathGraph.passable`).
Constrained queries use an iterative-deepening DFS over the pruned graph (shortest admissible first, ≥ 10 admissible
candidates ranked by cost, `k` kept) instead of Yen over collapsed parallel edges; a constrained "no path" answers
`no_path_satisfying_constraints`. Namespaced dataset predicates are `statement_edges` now (validated against
`registry/kg/predicates_*.yaml` + KGGEOPOL `GEOPOL_PROPERTIES` + A-MILITARY `UNPK_PROPERTIES`):
`geo:covered_territory_of`, `cliopatria:denotes`, `cliopatria:member_of`, `cshapes:ruled_by`,
`cshapes:shares_border_with`, `ucdp:primary_party`, `ucdp:secondary_party`, `glottolog:spoken_in|parent|family|in_group|wikidata_item`,
`unpk:contributed_personnel_to`, `unpk:mission_country` (Brazil → Portuguese Empire: `—territoire actuel couvert en
partie par (superposition calculée)→ … —décrite (selon Cliopatria) par→ Q200464`, unit-tested).

`tests/unit/links/test_links_anchor_chains.py` evaluates all 20 chains of `registry/kg/anchor_chains.yaml` on the
recorded Wikidata claims (6,187 claims, 109 entities; every hop cites its statement GUID + revision): **18 found, 2
"no sourced path"** (`berlin_conference_congo`, `suez_crisis_usa_decolonisation`), exactly the expectations.
E.g. Columbus → Portuguese (via Tordesillas, steps leadership/participation → signatory/territorial → territorial/
succession → official language, Portuguese never transited): Columbus — significant event → Q3771259 — country →
Crown of Castile — citizen ← Q45859 — signatory of → Treaty of Tordesillas (1494) — significant event of → Portuguese
Empire — country of → State of Brazil — official language → Portuguese. Without non-transit kinds the plain shortest
path is the trivial language hop (test-enforced).

## L2.6 Demonstration on real data (throwaway DB `reva_links2_demo`, dropped afterwards)

Live, budget-metered ingests (2026-10-16, policy `budgets-1.3.5`, 0 refusals): `atlas.cshapes` (2 req, 4.7 MB; KG
1,884 statements), `atlas.ucdp` (5 req, 0.87 MB; KG 9,588), `atlas.naturalearth_10m` (6 req, 16.4 MB),
`atlas.un_ims` (1 req, 6.0 MB), `atlas.unga_votes --dataset votes` and `--dataset rollcalls` (4 req, 9.3 MB each run;
KG vote statements deliberately not loaded), `atlas.imf --dataset imts` (23 req, 112 MB). KG Wikidata kinds come
from the captured real-Wikidata links fixture (76 entities, 544 statements, origin `test_fixture` — so answers carry
`fixture_data`). Live snapshot `2aed9d60691e…` (11 tables, 3,431,862 rows). Materialisation (top-k 10): **384,583
links in 87 s** (KG + dataset 17.6k, IMTS exports 97,944, imports 98,520, migrant stock 26,125, NE borders 321,
UNGA agreement 144,043 decade×issue links), `kg.link_row` 419 MB. Response times (warm): pair BRA–PRT 1.6 s (cold
2.8 s), USA–x votes 0.2–0.45 s, country view 0.05 s, path BRA→PRT 2.2 s. Per-type counts:

```
bilateral_trade 97944 · bilateral_trade_imports 98520 · unga_vote_agreement 144043 · migrant_stock 26125
conflict_same_side 15926 · conflict_opposing_sides 335 · land_border_cshapes 646 · colonial_rule_cshapes 343
land_border 321 · co_member 135 · shared_official_language 81 · shared_language_used 72 · diplomatic_relation 44
successor_of 15 · border_wikidata 13 · former_colony_of 6 · colony_of 4 · conflict_co_participant 7 · co_signatory 3
```

**`/v1/links/pair?a=BRA&b=PRT`** (every line cites its evidence; IMTS cites the raw sha + SDMX series selector, UN DESA
the xlsx cell, Wikidata the statement GUID + revision):

* Trade (IMF IMTS, 2000–2025, 52 + 52 cells): Brazil → Portugal exports reported by Brazil 379,774,421 USD (2000),
  3,416,220,938 (2024), 3,299,445,083 (2025) `[imf:a2531395fb83#…"series":"BRA.XG_FOB_USD.PRT.A","time_period":"2025"]`;
  Portugal → Brazil 180,369,496 (2000), 1,233,438,789 (2024), 1,202,294,976 (2025). Mirror flows (Portugal's imports
  from Brazil, CIF) are a separate kind: 3,036,664,742 USD (2025).
* Migrant stock (UN DESA 2024, 1990–2024): born in Brazil living in Portugal 23,722 (1990) → 173,266 (2024)
  `[un_desa:0e10179d0518#{"cell":"O19258",…"sheet":"Table 1"}]`; born in Portugal living in Brazil 270,645 (1990) →
  146,697 (2024).
* Share of identical UNGA votes (computed, all roll calls / colonialism-coded): 1950s 0.5989 (n=187) / 0.5185;
  1960s 0.3902 / 0.3800; 1970s 0.6004 / 0.5833; 1980s 0.4532 / 0.5305; 1990s 0.6154 / 0.6183; 2000s 0.6767 /
  0.7542; 2010s 0.6055 / 0.6609; 2020–24 0.5407 / 0.4348 `[unga:987f89d81054#{"pair":["BRA","PRT"],"years":[1950,1959],"issue":"all",…}]`.
* Language (Wikidata, fixture): shared official language and language used via Portuguese (undated in source).
* Colonial (via predecessors, `predecessor_links`): Brazil — former colony of (via Colonial Brazil) → Portuguese Empire
  (1500–1822), shown with "Portugal — successor of → Portuguese Empire" `[wikidata.links_fixture:Q45$6b62100c-…]`;
  Brazil — former colony of → United Kingdom of Portugal, Brazil and the Algarves (1815–1822) with "Portugal — successor
  of → Kingdom of Portugal — successor of → United Kingdom…(1822)". CShapes has no BRA–PRT colonial row (starts 1886).
* Also: co-members via the United Nations since 1955-12-14; diplomatic relations (undated); UCDP same side (secondary
  parties on side A of the Central African Republic conflicts 416 and 15283, 2020–2023 — UCDP codes troop
  contributors to a side as secondary parties).

**USA vs GBR / FRA / IND, share of identical votes on colonialism-coded roll calls (Voeten `co`), by decade**
(computed; `n_identical / n_common`, both voted):

| decade | USA–GBR | USA–FRA | USA–IND |
|---|---|---|---|
| 1950s | 0.7600 (57/75) | 0.6269 (42/67) | 0.4133 (31/75) |
| 1960s | 0.8673 (98/113) | 0.6429 (72/112) | 0.2479 (29/117) |
| 1970s | 0.6548 (110/168) | 0.5460 (95/174) | 0.1420 (25/176) |
| 1980s | 0.5793 (95/164) | 0.3899 (62/159) | 0.0305 (5/164) |
| 1990s | 0.5682 (75/132) | 0.3588 (47/131) | 0.0379 (5/132) |
| 2000s | 0.4298 (52/121) | 0.2645 (32/121) | 0.0667 (8/120) |
| 2010s | 0.4914 (57/116) | 0.3276 (38/116) | 0.0690 (8/116) |
| 2020–24 | 0.8714 (61/70) | 0.7857 (55/70) | 0.2714 (19/70) |

All roll calls, 1960s: USA–GBR 0.8595 (422/491), USA–FRA 0.6151, USA–IND 0.3154. Consistent with KGGEOPOL's counts
(USA on CO roll calls 1960s: 30 yes / 47 no / 41 abstain = 118 positions; 113 shared with the UK). Per-roll-call
evidence (`votes_detail_from=1960&votes_detail_to=1960&votes_detail_issue=co`, 11 roll calls): 1960-12-01 R/15/1273D
"TO ADOPT THE 43-POWER DRAFT RESOLUTION (A/L.323 AND ADD. 1-6)" — USA abstain, GBR abstain; R/15/1272 (USSR draft
declaration on granting independence) — USA no, GBR no; 1960-12-02 R/15/1291 — USA yes, GBR abstain. These are
shares of identical votes, not a measure of alliance or of interests; the issue coding is keyword-based.

**`/v1/links/BRA?limit=5`** (materialised, latest period per kind, labels from the crosswalk when the KG has none):
exports 2025 reported by Brazil to China 99,940,244,710 USD, United States 38,056,005,598, Argentina 18,105,303,283;
exports to Brazil from China 71,604,309,339, United States 54,383,648,702, Germany 14,558,571,108; born in Brazil
living in the United States 738,994, Japan 207,220, Spain 179,946 (2024); living in Brazil, born in Venezuela 571,919,
Portugal 146,697, Haiti 88,383; land borders (NE 1:10m) Bolivia 2,664.44 km, Peru 1,800.03 km, Venezuela 1,601.95 km;
highest shares of identical votes 2020–24 (all roll calls) Equatorial Guinea 0.8398, Eswatini 0.8351, Ghana 0.8290;
former colony of the Portuguese Empire (via Colonial Brazil, 1500–1822). Response 0.05 s.

**`/v1/links/pair?a=DZA&b=FRA`**: CShapes "Algeria — territory ruled by → France", 4 periods 1886-01-01 → 1962-07-04
(status colony; fixed-border periods), `[cshapes.cran:gw615:ruled_by:452 rev cshapes-2.0; cshapes.cran:gw615:iso3:DZA:1886]`
(CC BY-NC-SA: non-commercial); migrants born in Algeria living in France 1,344,293 (1990); IMTS Algeria → France
2,920,739,361 USD (2000).

## L2.7 Honesty and limits

* A link is "connected via", never a cause; vote agreement is a share of identical votes (derived index, formula and N
  shown); missing data gives no link (`kinds_without_links`, skip counters), a reported zero gives no link
  (`reported_zero` counted); no zero-weight row can be stored (integration test).
* The store keeps top-10 partners for atlas kinds; a pair outside both top-10s is still answered by the pair view
  (snapshot re-read), not by `/v1/links/{iso3}`.
* Decade links are labelled with the decade (`2020–2029`) while the data end in 2024 (`basis.roll_call_years` gives
  the real span).
* `conflict_same_side` is dominated by secondary parties (15,037 of 15,926: coalitions and troop contributors);
  `basis.roles` says which.
* KG Wikidata kinds in the demo come from the 76-entity fixture; a full WDHARVEST KG makes them planet-wide (the
  materialiser reads ≤ 5 M statements, refuses beyond). CShapes/UCDP/UNPK/Cliopatria/Glottolog derivations read code
  entities through `geopol:corresponds_to_iso3`; peacekeeping, polity-overlay and Glottolog links are unit-tested on
  statements shaped like the connectors' (their data were not loaded in the demo DB).
* `kg.link_row` costs ~1.1 KB per row (419 MB for 385k rows); constant wording (formula, caveat, flow) is added at
  response time, not stored.

## L2.8 Requests to other owners

1. **Lead / ATLASOPS**: a worker job kind `links_materialise` (`reva.links.store.materialise`) scheduled after the daily
   atlas snapshot (needs `ops.submit_job` allow-list; lead-owned migration) and a `reva links materialise` CLI alias.
2. **NLQA** (`src/reva/nlqa/adapters.py` `LINK_GROUPS`): add `bilateral_trade_imports`, `bilateral_trade_baci` (trade),
   `land_border_cshapes` (border), `colonial_rule_cshapes` (colonial), `peacekeeping_contributor_to_host`,
   `shared_polity_territory`, `shared_language_glottolog`; use `service.pair_view` for "X and Y" questions and render
   `unga_vote_agreement` as "share of identical votes" with N.
3. **UI3**: `/v1/links/{iso3}` now carries `by_kind` groups and `mode`; `/v1/links/pair` is new (zod schemas in
   `ui/src/v3/api/links.ts` stay valid: `qid`, `links`, `link_types[*].derived` kept).
4. **Route parameters for constrained paths**: `steps`/`via_any`/`via_all`/`non_transit` are in `PathQuery` and the service;
   exposing them on `/v1/links/path` (and on KGCORE's `/v1/kg/path`) is a small follow-up.
5. **A-TREATIES / A-LAW**: no data delivered; `co_signatory` remains Wikidata P1891 only, `shared_legal_family` awaiting.


# LINKWIRE — the links layer wired into ops, the path route, NLQA and the UI (agent LINKWIRE, 2026-10-16)

## LW.1 Operations

* Job kind `links_materialise` → `reva.links.jobs:handle_links_materialise` (payload `atlas_origin`, `atlas_snapshot`,
  `kg_origin`, `top_k`, `with_atlas`, `skip_unchanged`; unknown keys refused). With `skip_unchanged` (default) the
  handler resolves (snapshot sha, KG watermark, taxonomy sha, method version, top-k) first and reuses an identical
  materialisation (`reused: true`, nothing written). Allowed by `ops.submit_job` from migration `0013_links_ops`
  (chained after `0012_links_store`; `migrations/schema_contract.json` regenerated, head `0013_links_ops`), schedulable
  (`SCHEDULE_JOB_TYPES`), seeded daily as `links_materialise:live` two hours after `atlas_snapshot:live` (live atlas +
  live KG: the only materialisations production serves). CLI: `reva links materialise` (alias of
  `python -m reva.links.store materialise`). Tests: `tests/integration/test_links_ops.py`.
* `scripts/e2e_stack.py --v3-fixtures` also replays UN DESA IMS, UNGA votes, CShapes, UCDP, BACI and UNTC and
  materialises the store after the snapshot (`v3.links` in the stack file).

## LW.2 Constrained paths on `/v1/links/path`

`steps` (repeatable, one per ordered step; each a comma-separated set of hop kinds, `steps=P1891,P710&steps=P37`;
≤ 8 steps, ≤ 16 kinds per step, never more steps than `max_hops`), `via_any` / `via_all` (≤ 8 QIDs or ISO3 codes;
ISO3 resolved like the ends), `non_transit` (≤ 16 properties: `P37`, `P2936`, `P30`, `P2348` or namespaced
predicates), `max_hops` ≤ 8. Ordered `via` cannot be combined with these (422). Malformed values → 422; the SEC-1
bounds of the engine (2,000,000 DFS steps, 20 s request budget, ≤ 20,000 stored links per read) apply.
`contracts/openapi.json` re-exported. Test: `tests/integration/test_links_db.py::test_links_path_route_constrained_parameters`.

## LW.3 `co_party` (A-TREATIES)

New link type `co_party` (country_country, undirected, interval): two states objects of `untc:party` statements of the
same treaty (UN Treaty Collection, source key `untc.mtdsg`) over overlapping periods (consent instrument .. withdrawal
effect), `via` = the treaty (`untc:treaty:<MTDSG>` for most, a QID for the few mapped in
`registry/entities/treaty_qids.json`). Not linked, and counted in the derivation report: participants outside the
crosswalk (`untc:participant:*`, e.g. the European Union → `non_state_participant`), `untc:former_party` rows (no
stated date of effect → `former_party_end_not_stated`), disjoint periods, and treaties with more than 150 parties
(`near_universal_treaty_not_expanded`, with the party count — near-universal treaties say little about a pair and
would dominate the store); ≤ 200,000 pairs. `untc:party` / `untc:former_party` are statement edges (path hops,
"partie à (Collection des traités de l'ONU)"); `kg_allowlist()` reads A-TREATIES' `KG_PROPERTIES`. The materialiser
and the on-demand country view read `untc:party`. e2e fixture store: 14,876 `co_party` links (2 treaties skipped as
near-universal); BRA–PRT co-parties of the Rome Statute (since 2002-06-20) and MTDSG IV-5. Tests:
`tests/unit/links/test_links_co_party.py`.

## LW.4 NLQA and UI

* NLQA: `LINK_GROUPS` covers every taxonomy type (test-enforced): trade + `bilateral_trade_imports` /
  `bilateral_trade_baci`, border + `land_border_cshapes`, colonial + `colonial_rule_cshapes`, shared_language +
  `shared_language_glottolog`, treaty + `co_party`, new groups `peacekeeping` and `historical_polity`. New closed-set
  operation `links_pair` (see `docs/dev/v3_nlqa.md`).
* UI: "Pays ↔ Pays" screen `/v3/relation?a=BRA&b=PRT` (`ui/src/v3/pages/RelationPage.tsx`, view model
  `ui/src/v3/lib/relation.ts`): overview lanes per link kind, themes (trade and migrant stocks per direction as exact
  series, vote agreement by decade with `n_identical / n_common` and the caveat, colonial rule, borders, languages,
  treaties, conflicts, peacekeeping), each link cited; predecessor-state links with their succession chain; kinds
  without links listed as "aucun lien (≠ zéro)". Dossier pays links use `by_kind` groups (direction, period, top-N of
  total) and link each partner to the pair screen. Chemin has advanced options (ordered steps, via any/all,
  non-transit presets).


# LINKS3 — physical networks between countries: cables, 2014 air routes, launch-site states (agent LINKS3, 2026-10-16)

Input: A-INFRA's dyadic atlas rows (`docs/dev/v3_a_infra.md` §3, "Country-pair data for LINKS2": indicator
`<NAME>|partner=<ISO3>` + `flags.partner_iso3`; a partner outside the crosswalk is folded as `|partner_code=<code>`).
Three new `atlas_dyadic` types (taxonomy: 33 types), wired into the derivation, the materialised store, the
on-demand country view, the pair view, the path engine (stored links are edges), NLQA and the Pays ↔ Pays screen.

## L3.1 Link types

| id | table / indicator | direction | weight | temporal | companions (basis + cited evidence rows) |
|---|---|---|---|---|---|
| `submarine_cable` | `wikidata.submarine_cables` `CABLES_SHARED\|partner=` | undirected: A-INFRA emits both directions, merged into one link (both rows are evidence; disagreeing rows → no link, counted) | cables (count of Wikidata cable items listing both) | retrieval-day snapshot | `first_service_year` ← `CABLE_FIRST_SERVICE_YEAR` (earliest P729, else P571) |
| `air_routes_2014` | `openflights.routes_2014` `ROUTES_OPERATED\|partner=` | origin → destination | routes (airline × airport pair, codeshares excluded) | 2014 (June 2014 period; `year_from: period_start`, the month is not a year-view period) | `airport_pairs` ← `ROUTE_AIRPORT_PAIRS`, `airlines` ← `ROUTE_AIRLINES` |
| `launch_service` | `gcat.payloads` `PAYLOADS_LAUNCHED_FROM\|partner=` | owner state → launch-site state (GCAT `flags.direction`) | payloads | launch year | – |

Wording (fr/en labels, honesty) says only what the rows say: "câbles sous-marins listant les deux pays (Wikidata)",
"liaisons aériennes directes recensées en juin 2014 (OpenFlights, données figées)", "charges utiles lancées depuis un
site attribué à" (never "launched by": the site state is not the launcher's supplier; Kourou is French Guiana `GUF`
per GCAT, so France → GUF is a link, France → France is not). NLQA: new user-level group `infrastructure`
(`src/reva/nlqa/adapters.py` LINK_GROUPS + `src/reva/nlqa/catalog.py` LINK_KINDS, coverage test-enforced).

**Caveat and licence on every link.** `derivation.atlas.caveat` (fr/en) and `licence` are stored in each link's
`basis` (`basis.caveat`, `basis.licence`) — in the store row, the pair view, the country view and path hops:
cables = community-edited Wikidata, partial coverage (~190 of ~600 systems), retired cables not excluded, no link ≠
no cable, CC0; air = **frozen June 2014** (provider warning) and **ODbL 1.0** (attribute OpenFlights; share-alike for
a published derived database); launches = one researcher's compilation (CC BY 4.0), site-state attribution, USSR and
IGOs unmapped. The cables' evidence row carries an `Evidence.note` naming the cable items (label, QID, revision;
`+N more` when A-INFRA capped the list — every QID stays in the selector); `basis.source_flags.cables` keeps them
structured. The route's `community_edited` warning fires on cable evidence (source `wikidata`).

**Stored in full** (`stored_in_full: true`): the three tables are small (live: 1,263 cable pairs, 4,558 directed
air pairs, 1,413 owner→site pair-years), so the store keeps every link instead of the union of top-k.

## L3.2 The Q55 → NLD mapping hop (explicit, cited, never silent)

Wikidata cable items list P17 = `Q55` (Netherlands, constituent country) for 10 cables; Wikidata deprecated the ISO
codes on Q55 (P297/P298 rank deprecated, qualifier P8327 "intended subject" = `Q29999`), and `Q29999` (Kingdom of the
Netherlands) is the crosswalk QID of NLD. `registry/links/geo_mapping_hops.yaml` declares the hop: provider code
`Q55` → NLD for `(wikidata, submarine_cables)` only, through the statement **`Q55 — country (P17) → Q29999`**
(`q55$3F58B793-57C8-4C66-87ED-403C7E298EBF`, rank normal, entity revision 2556226405; the deprecation statements and
the Special:EntityData sha256 checked on 2026-10-16 are cited there as documentation). Mechanics
(`src/reva/links/geo_hops.py`, `AtlasLinkReader(hop_evidence=…)`):

* the hop is applied **only when the KG holds that statement** (current, not deprecated): the materialiser reads it
  from its KG statements (P17 is in `KG_LINK_PROPERTIES`), the pair view and the on-demand view with one bounded
  `kg.statement_current` query; the statement becomes a `kg_statement` evidence of every link it produces (with a
  note), and `basis.mapping_hop` records provider code, target ISO3/QID, statement id and the registry;
* without the statement nothing is mapped: the rows stay unmapped and are counted as
  `mapping_hop_without_kg_evidence` (report: `atlas.submarine_cable.skipped`); with it, `mapped_via_hop: {"Q55": n}`
  and the materialisation report lists `mapping_hops: {"Q55": "<statement id>"}`;
* the Q55 row side (`provider_geo = Q55`) and partner side (`|partner_code=Q55`) are both read; `Q55`–`Q29999` maps
  onto one country and is never a self link;
* when a direct NLD (`Q29999`) row and a hopped `Q55` row reach the same pair, the link counts the **union** of the
  cable items their selectors list (`union_selector_key: wikidata_entities`; `basis.items`, `combined_from`,
  `combination`), never the sum (a cable listing both Q55 and Q29999 counts once); an incomplete selector list gives
  no link (`union_selector_list_incomplete`); `first_service_year` = the earliest of the rows'. Directed types
  never combine: a collision is refused and counted (`mapping_hop_collides_with_direct_row`);
* `taxonomy_sha256` (materialisation identity, `skip_unchanged`) now covers `geo_mapping_hops.yaml` too.

Requested of WDHARVEST: harvest `Q55`'s P17 statement into the live KG (the production store applies the hop only
then); until then the 10 live Q55 cables stay unmapped and counted.

## L3.3 Generic `atlas_spec` options (schema `registry/links/link_types.schema.json`, semantic checks in `validate_taxonomy`)

`year_from` (`attached` | `period_start`), `companions` ({basis key: indicator name}; joined on provider geo, dataset,
period and the same partner dimension; reserved basis keys refused), `union_selector_key` (undirected types only),
`stored_in_full`, `mapping_hops` (refused unless a registry hop applies), `caveat` (fr/en ≤ 600 chars), `licence`.
Rows folded as `|partner_code=` are now read for every atlas type, so they are counted as `partner_unmapped`
instead of being left out of the derivation report.

## L3.4 UI (Pays ↔ Pays)

New theme "Réseaux physiques (câbles sous-marins, vols de 2014, lancements spatiaux)" in `ui/src/v3/lib/relation.ts`:
cables and air routes are items whose sentence carries the details (cable names, "mise en service la plus ancienne",
the cited Q55 hop; airport pairs, airlines, "réseau de juin 2014, données figées"), the cable lane starts at the
earliest service year; launches are per-direction yearly series; each kind shows its stored caveat and licence once
(`KindView.notes`, rendered in `RelationPage.tsx`). zod schemas unchanged (loose: `basis`, `Evidence.note` pass
through). `npm run typecheck`, vitest (190 passed) and `npm run build` pass.

## L3.5 Tests and figures

* `tests/unit/links/test_links_infra.py` (13): A-INFRA's own builders on its recorded fixtures → a DuckDB built like
  `reva.atlas.query._build_db` → derivations. Fixture figures: cables 670 links (675 with the hop; 10 Q55 rows counted
  `mapping_hop_without_kg_evidence` without it), GBR–USA 1 (Apollo, first service 2003), CAN–GBR first service 1956
  (TAT-1), GBR–NLD 2 via Q55 (REMBRANDT-2, Concerto 1; 1999); union case (Q55, Q29999 and both listed) → 3 distinct
  items, not 4; air 22 links (Netherlands Antilles unmapped, counted), GBR→MAR 25 routes / 14 airport pairs / 7
  airlines and MAR→GBR 25 as a second link; launches 34 links (ESA, Intersputnik, NATO… owners counted unmapped),
  e.g. USA→RUS 26 payloads (1999), USA→GUF 4, DEU→GUF 2 (2024).
* `tests/integration/test_links_infra_store.py` (6, `db`, own throwaway database `reva_test_links3_*`): fixture ingest
  of `atlas.wikidata_cables`, `atlas.openflights`, `atlas.gcat`; KG = links fixture + the real Q55 hop statement
  (`tests/unit/links/fixtures/wikidata_q55_hop.json`, P17 only: the deprecated ISO statements are never loaded);
  materialisation (stored in full, caveat + licence on every row, report `mapping_hops`), `/v1/links/GBR?kind=submarine_cable`
  (NLD via the hop, cable-name notes, `community_edited` warning), `/v1/links/pair?a=GBR&b=MAR` (two 2014 air links
  with the caveat), `/v1/links/USA?kind=launch_service`, the on-demand view of NOR (hop read from the KG), a stored
  `air_routes_2014` path hop.
* Green: `tests/unit/links` (120), `tests/review_v3`, the anchor chains, `tests/unit/nlqa`, `tests/unit/test_atlas_infra.py`,
  `tests/integration/test_links_{store,db,ops}.py`, `test_api_contract.py`; `contracts/openapi.json` unchanged (route
  models carry `data: dict`).

Not done: no live materialisation (A-INFRA's live tables lived in a dropped throwaway database; a live run of
`atlas.wikidata_cables` costs ~400 Wikidata requests); `scripts/e2e_stack.py --v3-fixtures` does not replay the
A-INFRA fixtures yet (owner: lead / LINKWIRE).


# EVMAP — sanctions, colonial datasets and shared natural events between countries (agent EVMAP, 2026-10-16)

Inputs: A-SANCTIONS' dyadic atlas tables (`docs/dev/v3_a_sanctions.md` §4-5), A-COLONIAL's KG statements
(`docs/dev/v3_a_colonial.md` §5, §10), A-HAZARDS' NCEI tsunami records (`docs/dev/v3_a_hazards.md`). Five new link
types (taxonomy: 38), wired like LINKS3's: derivation, materialised store, on-demand country view (atlas kinds), pair
view, path engine (stored links are edges), NLQA (`LINK_GROUPS` / `LINK_KINDS`) and the Pays ↔ Pays screen. None is
causal; each link carries its licence / terms in `basis.licence`.

## EV.1 Link types

| id | input | direction | weight | temporal | terms (`basis.licence`) |
|---|---|---|---|---|---|
| `sanction_episode` | `ties.dyad_year` + `eusanct.dyad_year` `CASES_ACTIVE\|partner=` (atlas_dyadic, two sources); companion `IMPOSITIONS_ACTIVE` → `basis.impositions_active` (cited) | sender → target | cases (REVA count of coded cases) | case-active year | TIES: no licence stated, cite-only (maintainer review); EUSANCT: CC BY 4.0 |
| `us_sanctions_program` | `ofac.programs` `ACTIVE_PROGRAMS\|partner=` (row USA) | USA → country named by the program title | programs | retrieval day | public domain (US Government work) |
| `colonised_by_coldat` | `coldat:colonized_by` (kg_dataset_colonial) | colonised country → colonizer | – | first .. last year | CC0 1.0 |
| `colonial_rule_cow` | `cowent:colony_of` / `protectorate_of` / `mandated_to` / `possession_of` + `geopol:corresponds_to_iso3` | territory → ruler | – | status spell as printed, left-censored at 1816 (`basis.left_censored`, shown "≤ 1816") | COW terms: cite, no charging |
| `hazard_shared_event` | `ncei:country` + `ncei:runup_country` (+ `ncei:tsunami_validity`, the event's `P585`) of one `ncei_hazards:tsunami:*` (new method `kg_dataset_hazard`) | source country → runup country, via the event | – | event date (point) | US Government work (NOAA NCEI) |

* **Several datasets, one type, never summed.** New `atlas_spec` options (schema + `AtlasLinkReader`):
  `extra_sources` (same datasets / indicators in further atlas sources), `facet_source` (each link's facet is
  `source=<name>`: TIES and EUSANCT overlap — EUSANCT re-uses TIES cases — so their counts are separate links and
  series, never added), `licences` (source → terms, overriding `licence`). Evidence and `basis.source` name the row's
  own source; companions join on the same source. A `source=` facet is never a filter: the store's `country_rows` and
  the on-demand ranking keep every source as its own group (`facet LIKE 'source=%'`). Organisation senders (EU, UN,
  EEC code 1000) are not countries: counted `iso3_unmapped`, no link (an `org → country` kind would need an evidenced
  org mapping, not proposed).
* **COLDAT** states each spell under two date aggregations (last date, mean): statements with the same years give one
  link citing both (`basis.aggregations`); different years give one link per aggregation. One spell per (country,
  power): interruptions are not coded (said in `basis.note`).
* **COW entities**: territory named like `colonial_rule_cshapes` (`basis.territory_mapping` qid / later_state /
  same_entity / none — unmapped territories keep their `cowent:<code>` node and are counted
  `territory_without_country`); occupied_by / claimed_by / leased_to / part_of / neutral_zone_of are not colonial rule:
  no link. The CShapes derivation now shares this code (`_derive_territory_rule`), unchanged in behaviour.
* **hazard_shared_event** exists only when one NCEI tsunami record lists runups in a country other than its source
  country: "même événement naturel observé" — never a cause, never an exchange. Validity −1 (erroneous) → no link;
  more than 60 runup countries on one event → not expanded (counted `too_many_runup_countries`); ≤ 200,000 links;
  `basis` = runup record count, published runup deaths sum, validity code, number of runup countries. The causal
  side (earthquake → tsunami `coded_trigger`) stays A-HAZARDS' T1 claims, never this link.
* ICOW data are not used for links (ICOW asks users not to redistribute; its independence events are in the event
  view, cited, with the courtesy flagged by A-COLONIAL).

## EV.2 Wiring

* `src/reva/links/atlas_links.py` (`spec_sources`, multi-source CTEs, per-source licence), `dataset_kg.py`
  (`derive_coldat`, `derive_cow_colonial`, `derive_hazard_shared_event`, `DATASET_PROPERTIES` extended so the
  materialiser reads them), `store.py` / `service.py` (source facets), schema `kg_dataset_hazard` +
  `extra_sources` / `facet_source` / `licences`.
* NLQA: `colonial` gains `colonised_by_coldat`, `colonial_rule_cow`; new user-level groups `sanctions`
  (`sanction_episode`, `us_sanctions_program`) and `natural_hazard` (`hazard_shared_event`) in `LINK_GROUPS` and
  `LINK_KINDS` (coverage test-enforced); the UI Demander fixture's `catalog_digest` regenerated.
* UI (`ui/src/v3/lib/relation.ts`): themes "Sanctions (épisodes codés TIES / EUSANCT, programmes OFAC)" and
  "Catastrophes naturelles (même événement observé dans les deux pays)"; COLDAT / COW kinds in the colonial lane next to
  CShapes; sanction series are named by dataset ("jeu de données TIES (1945-2005)"), never merged; hazard and colonial
  items carry their basis details (runup records, aggregation, status); the stored licence of each kind is shown once.

## EV.3 Tests and figures

* `tests/unit/links/test_links_evmap.py` (6): A-SANCTIONS' builders on its recorded fixtures → DuckDB built like
  `_build_db` → `sanction_episode` (TIES and EUSANCT links, facet per source, per-source licence, USA → CUB 1960 from
  the TIES Cuba case, organisation senders counted unmapped, store row round trip) and `us_sanctions_program` (20
  links, USA → CUB = 1 program); COLDAT aggregation merge / split; COW left-censoring and occupation excluded; hazard
  links (source → runup countries only, erroneous record, no source country, bound).
* `tests/integration/test_events_dataset_kinds.py` (`db`): after the all-fixtures load, one materialisation
  (test_fixture snapshot + KG) gives `sanction_episode` 5,561 (787 cells with an unmapped sender, 571 with an
  unmapped target), `us_sanctions_program` 20, `colonised_by_coldat` 255, `colonial_rule_cow` 620 (442 rule
  statements on territories without a country correspondence, kept on their own node), `hazard_shared_event` 23 (19
  runups in the source country itself: no link); every row evidenced, with terms, no zero weight; two facets for
  `sanction_episode`; the pair view USA ↔ CUB lists TIES episodes from 1960 and the OFAC program; the country view of
  CUB groups `source=ties` incoming.
* Green: `tests/unit/links` (126), `tests/unit/events`, `tests/unit/nlqa`, `tests/review_v3`,
  `tests/integration/test_links_{store,db,ops,infra_store}.py`, `test_events_{view,datasets}.py`,
  `test_api_contract.py` (`contracts/openapi.json` unchanged: route models carry `data: dict`); UI typecheck, vitest
  (221 passed), build.

Not done: no live materialisation (the new sources' budgets are not merged); `scripts/e2e_stack.py --v3-fixtures`
does not replay these fixtures (owner: lead / LINKWIRE).

# LINKRET — link store retention: `reva links prune` (agent LINKRET, 2026-10-17)

The store (L2.3) was append-only with no deletion path: every daily `links_materialise` run with new inputs added a
full materialisation and nothing removed the old ones. Measured on the dev DB (2026-10-17): **one** live
materialisation (id 1, atlas snapshot `1d9b950f…`, KG watermark 321) = 1,147,110 rows, `kg.link_row` **1.46 GB**
(heap 1.39 GB incl. TOAST, indexes 42 MB) — about 1.3 kB per row, so ~1.4 GB per full materialisation, not the
~400 MB estimated earlier.

**Code.** `src/reva/links/retention.py` (policy, plan, prune, vacuum), CLI `reva links prune` in
`src/reva/cli/main.py`, migration `migrations/versions/0017_links_retention.py` (schema contract regenerated, head
`0017_links_retention`), readers in `store.py` / `jobs.py`. Tests: `tests/unit/links/test_links_retention.py` (5),
`tests/integration/test_links_retention_db.py` (4, own throwaway DB `reva_test_linkret_*`).

```sh
uv run reva links prune --dry-run                 # what would be pruned, and why each kept one is kept
uv run reva links prune [--keep 2] [--vacuum plain|full|none] [--reason "…"]   # owner credentials
```

**What is kept** (each kept materialisation is listed with its reasons):

| Reason | Why |
|---|---|
| `latest` | the newest materialisation: the path engine reads `latest_materialisation(conn)` |
| `latest_live` | newest live-atlas (or atlas-less) + live-KG one: what production's API serves (`live_only`) |
| `latest_lineage:<atlas>/<kg>` | newest per (atlas origin, KG origin): fixture and live stores coexist |
| `recent` | the newest `--keep N` (default 2, the current one included) |
| `current_atlas_snapshot:<origin>` | newest materialisation of the latest recorded atlas snapshot of each origin |
| `atlas_pin` | newest materialisation of the snapshot pinned in the atlas query cache (`live-snapshot.pin`) |
| `fk:<schema.table.column>` | referenced by a foreign key from any other table (found from the catalog: a future release / pin table is honoured without code changes; a unit test flags any new `REFERENCES kg.link_materialisation`) |

References searched and found **not** to pin a materialisation: `belief.release` / `release_artifact` /
`release_dataset` and `ops.publication` (artefacts and datasets only); no `meta.artifact` schema embeds a
materialisation id (checked on the dev DB too); `/v1/links*` responses are `Cache-Control: no-store` and NLQA
answers are not persisted; `ops.job` results of `links_materialise` name their materialisation id — they stay
resolvable because the materialisation row is never deleted.

**What a prune does** (one transaction per materialisation, owner engine): `kg.prune_link_materialisation(id, run,
reason, policy)` counts the rows and their size (`pg_column_size`), inserts the `kg.link_prune` record, deletes the
link rows and checks the count; the CLI adds one `ops.audit` row (actor `owner:links-prune`, action `links.prune`,
target `{materialisation_id, run_id}`, details: rows deleted, row bytes, estimated disk bytes, inputs, keep policy).
The `kg.link_materialisation` row (inputs: atlas snapshot, KG watermark, taxonomy sha256, method, top-k) stays.
Then `VACUUM (ANALYZE) kg.link_row` (default `plain`: the space is reused by the next materialisation, so the table
stays at about (keep + 1) materialisations; it is **not** returned to the OS) or `--vacuum full` (rewrites the table
and returns the space; ACCESS EXCLUSIVE lock, needs free disk equal to what remains).
Throughput measured on a throwaway DB: 2 × 500,000 rows (~0.56 GB each) pruned and vacuumed in 30.5 s.

**Database guards (migration 0017, whatever the caller).**
* `kg.link_row` DELETE is refused (`meta.reject_mutation`) unless a `kg.link_prune` row for that materialisation was
  inserted **in the same transaction** (`kg.link_prune_in_xact`); UPDATE and TRUNCATE stay refused; the reviewed
  restricted-deletion path (`reva_restricted_deletion` + ticket) is unchanged (integration test).
* `kg.link_prune` is append-only; only the owner inserts (runtime roles: SELECT only; `kg.prune_link_materialisation`
  EXECUTE revoked from PUBLIC and granted to nobody; both refuse a non-owner session). The insert is refused for the
  latest materialisation of its lineage and for the latest live one, and when `rows_deleted` exceeds `link_count`;
  a deferred constraint trigger refuses the commit if any row of a pruned materialisation remains.
* Readers: `latest_materialisation` and the job's `existing_materialisation` (skip-unchanged reuse) never return a
  pruned materialisation — a job with the same inputs re-materialises instead of reusing empty rows.

**Rebuild and reproducibility (honest limit).** `reva links materialise --atlas-snapshot <sha> --kg-origin <o>`
re-derives from the same immutable atlas snapshot but from the KG as it is then (`kg.statement_current`, context
and genealogy reads are not bounded by the recorded watermark), so a rebuild can differ from the pruned rows.
Anything that must stay bit-reproducible must be kept (referenced), not rebuilt — hence the `fk:` rule.

**Dev DB run (2026-10-17).** Dev DB migrated to `0017_links_retention` (`reva db bootstrap`, fingerprint
`d2e33cc3…` = contract). `reva links prune --dry-run` and the real run: **nothing to prune** — the store holds a
single materialisation (id 1), kept as latest / latest_live / lineage / recent / current atlas snapshot / atlas pin.
0 rows deleted, no VACUUM needed (0 dead tuples), 0 bytes freed. The policy starts to delete with the third
materialisation of new inputs (keep 2): from then on each daily run with new inputs frees ~1.4 GB for reuse.
Not automated: pruning needs owner credentials, so it is not a worker job; run it after `links_materialise`.
