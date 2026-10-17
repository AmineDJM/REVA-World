# V4 V4CORE — universal relation families, family traversal, entity page, space-time helpers

## FOR V4 AGENTS: how to map your properties to a relation family (read this first)

Every entity-valued property you load into the KG (a Wikidata P-id or your namespaced predicate, e.g.
`chebi:has_part`, `globi:eats`, `gem:owned_by`) should map to **one** universal family so that it is
traversable by family (`GET /v1/kg/entities/{id}/families`, family-constrained `GET /v1/kg/families/path`,
the "Fiche" UI page).

1. **Do not edit** `registry/kg/relations_v4.yaml` (owner V4CORE: families + core Wikidata mappings).
2. Create **your own file** `registry/kg/relations_v4.d/<agent lowercase>.yaml` (file name = `agent` lowercased):

   ```yaml
   version: 1
   agent: V4CHEM                 # ^[A-Z][A-Z0-9_]{1,31}$, equals the file stem upper-cased
   note: optional free text      # <= 2000 chars
   mappings:
     - property: "chebi:has_part"      # Wikidata P-id or <ns>:<name> (KG pid pattern; ns >= 2 chars)
       family: has_part                # one of the families below
       direction: forward              # optional: forward (default) = "subject <family> object"; reverse = "object <family> subject"
       subtype: has_part               # optional snake_case: the source's own relation name, kept for display
       source: chebi                   # optional: source key / provider
       share: {qualifier: share, scale: fraction}   # optional, only for families with share_allowed (made_of, part_of, has_part, owns, owned_by)
       subject_kinds: [compound]       # optional display hints
       object_kinds: [chemical_element]
       note: "verbatim caveat"         # optional, <= 500 chars
   ```

3. Check: `uv run python -m reva.kg.families check` (and `uv run pytest tests/unit/test_kg_families.py`).
   Rules it enforces:
   * schema `registry/kg/relations_v4.schema.json` (`#/$defs/dropin`);
   * **one family per property** across all files (first come; a duplicate is an error — coordinate in your report);
   * a Wikidata P-id must be in `registry/kg/properties.yaml` or requested in `registry/kg/property_requests/*.yaml`;
     a namespaced predicate must match the KG pid pattern and be registered by your loader
     (`reva.kg.model.register_properties`) before statements use it;
   * **no causal property**: Wikidata P828/P1542/P1478/P1479/P1536/P1537 and any namespaced name containing
     `cause(s|d)`, `causal`, `effect`, `material_basis`, `risk_factor`, `induces`, `leads_to` are refused. Causal
     statements a source makes go through the causal loader as attributed claims (`docs/roadmap/v3_causality.md`).
   * **share**: read only from the qualifier you name (in `kg.statement.qualifiers`; a list of one
     `{amount, unit}` or a scalar). Absent or ambiguous = Missing (`null`), never zero. `scale`: `fraction`,
     `percent`, `per_mille`, `ppm` (normalised to a fraction) or `source_unit` (quoted as given).
4. Commit only your file (`git add registry/kg/relations_v4.d/<agent>.yaml`). The API reloads the registry on
   restart; nothing else to register.

Families (`causal: false` for all — a schema constant):

| family | fr label | inverse (fr) | paired with | share |
|---|---|---|---|---|
| `is_a` | est un(e) | a pour instance ou sous-classe | — | |
| `part_of` | fait partie de | comprend | `has_part` | yes |
| `has_part` | comprend | fait partie de | `part_of` | yes |
| `made_of` | est composé de | est un constituant de | — | yes |
| `located_in` | est situé(e) dans | contient (lieu) | — | |
| `occurs_during` | a lieu pendant | est la période de | — | |
| `produces` | produit | est produit par | `produced_by` | |
| `produced_by` | est produit par | produit | `produces` | |
| `owns` | possède ou contrôle | est possédé ou contrôlé par | `owned_by` | yes |
| `owned_by` | est possédé ou contrôlé par | possède ou contrôle | `owns` | yes |
| `supplies` | fournit | achète à | `buys_from` | |
| `buys_from` | achète à | fournit | `supplies` | |
| `trades_with` | commerce avec | (symmetric) | — | |
| `eats` | mange | est mangé par | — | |
| `interacts_with` | interagit avec | (symmetric) | — | |
| `associated_with` | est associé à | (symmetric) — association, never causation | — | |
| `measured_by` | est mesuré par | mesure | — | |
| `discovered_by` | a été découvert par | a découvert | — | |
| `invented_by` | a été inventé ou développé par | a inventé ou développé | — | |
| `regulates` | régule | est régulé par | — | |
| `context` | a pour contexte | est le contexte de | — | |

Python: `from reva.kg.families import load_registry, share_of` — `reg.edge_family(pid, "out"|"in")` gives the
family and fr/en label of an edge read from either end; `reg.properties_for({"made_of"})` gives the
`(pid, direction)` pairs to traverse.

## Report (V4CORE, 2026-10-17)

V4CORE fetched no external data source of its own: it owns the backbone. One Wikidata call only:
`wbgetentities labels|datatype` for the 10 requested properties (merge of `registry/kg/property_requests/v4core.yaml`
into `registry/kg/properties.yaml`, budgeted client, 10 raw artifacts `30489b24d889…ba32075cc6ca`, a few KB).

### 1. Relation families

| Piece | Path |
|---|---|
| Families + core mappings (owner V4CORE) | `registry/kg/relations_v4.yaml` |
| JSON schema (`#/$defs/core`, `#/$defs/dropin`) | `registry/kg/relations_v4.schema.json` |
| Agent drop-ins | `registry/kg/relations_v4.d/<agent>.yaml` (11 files at the time of writing) |
| Resolver | `src/reva/kg/families.py` (`load_registry`, `validate`, `edge_family`, `properties_for`, `share_of`, `ATLAS_MEASURED_BY`, CLI `check`) |
| Tests | `tests/unit/test_kg_families.py` (19) |

* 21 families, all `causal: false` (schema constant). `never_map`: P828/P1542/P1478/P1479/P1536/P1537 and any
  namespaced predicate whose name contains a causal marker (`cause`, `effect`, `material_basis`, `risk_factor`,
  `induces`, `leads_to`…) are refused: causal claims go through the causal loader (`/v1/causal`).
* Core mappings (41): P31/P279 is_a; P361 part_of; P527 has_part; P186 made_of (share P1107); P131, P276, P17, P30,
  P706, P159, P2541, P927, P183, P9714, `place:located_in` located_in; P2348 occurs_during; P1056 produces;
  P176, P1582 produced_by; P127, P749 owned_by; P1830, P355 owns; P1034 eats; P2293, P780, P2283 associated_with;
  `atlas:measured_by` measured_by; P61 discovered_by; P178 invented_by; P128 regulates; P921, P452, P793 context;
  plus V3 dataset predicates (glottolog parent/family/spoken_in, cliopatria member_of, ucdp location, unpk mission
  country) in `relations_v4.d/v4core.yaml`.
* With the agents' drop-ins: **137 mappings from 12 files** (is_a 12, part_of 13, has_part 4, made_of 3,
  located_in 29, occurs_during 2, produces 3, produced_by 3, owns 2, owned_by 5, eats 3, interacts_with 7,
  associated_with 10, measured_by 4, discovered_by 1, invented_by 1, regulates 1, context 34; supplies,
  buys_from, trades_with: 0 — no open source loaded yet).
* Allow-list: P186, P927, P183, P9714, P1582, P1034, P2293, P780, P2283, P128 were requested and merged into
  `registry/kg/properties.yaml` (labels/datatypes from the property entities; only V4CORE's request file was
  merged, other agents' pending requests were left to their owners). Their statements exist once a harvest
  spec asks for them (e.g. V4CHEM `materials_made_from.yaml`).
* Validation: every mapped Wikidata P-id is allow-listed or requested; namespaced ids match the KG pid pattern;
  one family per property across all files; share only on `share_allowed` families.

### 2. Traversal (API and Neurones)

| Endpoint | Function |
|---|---|
| `GET /v1/kg/families` | registry (families, labels, inverse labels, every mapping with its file/agent) |
| `GET /v1/kg/entities/{qid}/families` | neighbours of ANY entity grouped by family (`family`, `per_family` ≤ 200, time window, origin); each neighbour: reading label (fr/en), source property + subtype, share (null = Missing), atlas hint (measured_by), rights of the raw artifact (Wikidata CC0; else `meta.artifact.rights` normalised), full statement provenance; literal values (identifiers, quantities, dates, coordinates) with rights; unmapped entity-valued properties counted (causal ones flagged); scan bounded at 20,000 rows (counts become lower bounds) |
| `GET /v1/kg/entities/{qid}/places` | evidenced `located_in` chain (current containment preferred over ended statements, administrative properties first), P625 coordinates per level, ISO3 of the country level |
| `GET /v1/kg/families/path?from=&to=&family=…` | `kg.query.paths` restricted to the families asked for **in reading direction** (new `edge_filter` hook; `part_of` ≠ `has_part`); hops annotated with `family` |
| `GET /v1/kg/path` | unchanged, hops now carry `family` (null when unmapped) |
| `GET /v1/links/path?family=…` | Neurones/links engine: family filter (direction-aware also in the backward search); hops carry `family` |

* Code: `src/reva/kg/family_query.py`, `src/reva/api/routes/kg_families.py` (+1 line in `app.py`), small
  additive edits in `src/reva/kg/query.py` (edge_filter), `src/reva/api/routes/kg.py` (`KgHop.family`),
  `src/reva/links/taxonomy.py` (every family-mapped property becomes a statement edge labelled by its family
  reading; `is_a`/`context` ones are opt-in, i.e. traversed only when named), `src/reva/links/paths.py`
  (`Edge.family`, `PathQuery.families`), `src/reva/api/routes/links.py` (`family` param).
* The country↔country links store (`kg.link_row`, materialisation, `/v1/links/countries`, `/pair`) is unchanged.
* Contract regenerated: `contracts/openapi.json` (+4 paths, `/v1/links/path` changed), `ui/src/api/openapi.gen.ts`.
* Tests: `tests/integration/test_kg_families_api.py` (6: petroleum —made_of(25 %)→ alkane —part_of→
  hydrocarbon; firm —produces→ petroleum; field → ADM1 → country chain with coordinates; the reverse reading and a
  causal statement are not family hops), `tests/unit/links/test_links_families.py` (2).
* Live check on the dev DB (read-only, warm): Brazil (Q155) families in 1.3 s (located_in 2,223 incoming,
  part_of 4, is_a 3, context 3, discovered_by 1; 61 unmapped properties counted; 138 literals); France 0.33 s;
  place chain Brazil → South America (Q18); family path Paris (Q90) —located_in→ … → France (Q142) in 0.10 s.

### 3. Fiche (UI)

`/v3/fiche?qid=<id>` (`ui/src/v3/pages/FichePage.tsx`, client `ui/src/v3/api/families.ts`, helpers
`ui/src/v3/lib/families.ts`, nav entry + palette example, "Fiche" link on every entity card). French UI: entity
card (labels, kind, provenance), banner "Une relation n’est pas une cause", identifiers, values (quantities as
the source gives them, Missing never 0), relations by family (reading label, neighbour link to its own Fiche,
kind, share, property and subtype, period, citation, licence badge, truncation counts), atlas series for
`measured_by` (sparkline + last value with licence, or atlas map link when the series has no country), space
(place chain with levels, ISO 3166-2 prefix inference labelled, country dossier link, equirectangular point map
of declared coordinates + OpenStreetMap link), time (first dated statements + Frise link), unmapped relations
(causal ones pointed to "Causes"), restricted licences of every value shown. Tests:
`ui/src/v3/pages/fiche.test.tsx` (5). Typecheck clean; full vitest 251/254 pass, the 2 failures
(`CountryPage`, `MapPage`, V2 pages untouched by V4CORE) are timeouts under load and pass when run alone.

### 4. Space-time helpers (`src/reva/kg/spacetime.py`, `tests/unit/test_kg_spacetime.py` 26)

* `Period` (day / month / quarter / year / decade / century; astronomical years, BCE), `parse_period`
  (`2024`, `2024-03`, `2024-03-15`, `2024-Q1`, `2020s`, `19C`, `-0043`), inclusive `bounds()`, atlas half-open
  `atlas_bounds()`, KG `kg_times()` (a quarter = start + end at month precision), `period_from_kg_time`,
  `overlaps` / `contains`, `iter_periods`, `label_fr()` ("1er trimestre 2024").
* Places: `parse_place` (ISO 3166-1 alpha-3, ISO 3166-2, geoBoundaries ids → `iso3:`, `iso3166_2:`,
  `geoboundaries:` KG ids), `PLACE_LOCATED_IN` predicate (`place:located_in`, family located_in; loaders register
  it), `located_in_statement(...)`, `place_chain` (evidenced climb; only structural inference: an ISO 3166-2
  code's country prefix, labelled), `chain_levels`, `iso2_to_iso3` (pinned Debian iso-codes table).
* No database change was needed: no migration added (0019 is reserved for PRED-EFFECT).

### 5. Atlas themes

`registry/atlas/themes.yaml`: chemistry, materials, biology, ecology, climate, health_body, industry, companies,
prices, behaviour (fr/en labels).

### Examples (links that resolve through families)

* synthetic test graph: `syn:fam_firm —produces→ syn:fam_petroleum —made_of (share 0.25, P1107)→ syn:fam_alkane
  —part_of→ syn:fam_hydrocarbon`; `syn:fam_firm —located_in→ field → ADM1 → country`.
* live: `Paris (Q90) —located_in (P131)→ … → France (Q142)`; Brazil's located_in in-edges (2,223 entities with
  P17/P131 Brazil); a Saudi field → `iso3166_2:SA-04` → Saudi Arabia works as soon as the energy/industry loaders
  emit `place:located_in` (their drop-ins already map their own predicates).

### Missing / owner decisions

* `supplies` / `buys_from` / `trades_with` have no mapping yet: no open firm-level supply-chain source has been
  loaded (Open Supply Hub needs an account for bulk; trade flows stay in the atlas and the country links store).
* Remaining failures in `tests/unit/test_kg_queries_scieco.py` come from other agents' in-flight query specs
  (V4CHEM / V4ECO ids not yet allow-listed or labelled), not from V4CORE files.
* A family label is generic ("est situé(e) dans"); the hop also shows the source property and subtype. If the
  owner prefers source-specific wording in Neurones, `statement_edges` entries in `link_types.yaml` override it.
* Later, on a bigger machine: nothing to re-run for V4CORE; after the agents' loads, `uv run python -m
  reva.kg.families check` and `uv run pytest tests/unit/test_kg_families.py` must stay green, and pending
  property requests are merged with `uv run python -m reva.kg.harvest merge-property-requests`.
