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
