# V3 — KGCULT: languages and culture as connective tissue

Agent KGCULT, 2026-10-15. Status: specs written and schema-valid; harvest runs are WDHARVEST's;
UNESCO World Heritage atlas connector implemented and tested (fixture + throwaway DB).

## 1. Wikidata query specs (`registry/kg/queries/culture/`, 18 specs)

| Spec | Purpose | Selects | Bound (`max_rows`) |
|---|---|---|---|
| `culture.languages` | seed | every item with a non-deprecated ISO 639-3 code (P220); claims: codes (P218/P219/P220/P305/P1394), genealogy (P31/P279/P361/P4913), speakers (P1098 with point-in-time, applies-to-part, role, method), writing system (P282), status (P1999/P3823), regulator, indigenous to | 12 000 |
| `culture.language_families` | seed | instances of language family (Q25295) | 15 000 |
| `culture.language_ancestors` | seed | P279 ancestors of every ISO 639-3 language **that carry a Glottocode (P1394)** (branches not typed as families) | 8 000 |
| `culture.writing_systems` | seed | objects of P282 of ISO languages + instances of writing system (Q8192); ISO 15924, based on (P144) | 4 000 |
| `culture.country_official_languages` | statements | P37 of the 250 crosswalk countries, start/end/point-in-time, applies-to-part (P518) | 3 000 |
| `culture.country_languages_used` | statements | P2936 of the 250 countries, speakers (P1098) / proportion (P1107) qualifiers (Brazil alone: 222) | 40 000 |
| `culture.historical_state_languages` | statements | P37 + P2936 of historical countries, empires, colonies, protectorates, dependent territories (P31/P279*, FILTER EXISTS: no duplicate rows) | 20 000 |
| `culture.country_religions` | statements | P140 of the 250 countries with proportion/dates | 3 000 |
| `culture.religions` | seed | religions (Q9174) and denominations (Q13414953) with ≥ 5 sitelinks + every P140 object of a country; founders (P112), branches (P279/P361), inception | 8 000 |
| `culture.religion_ancestors` | seed | P279 closure above `culture.religions` (depends_on) | 6 000 |
| `culture.country_currencies` | statements | P38 of the 250 countries with start/end | 3 000 |
| `culture.historical_state_currencies` | statements | P38 of historical states/empires/colonies/protectorates/dependencies with dates | 8 000 |
| `culture.currencies` | seed | items with ISO 4217 (P498) + every P38 object of a country; succession (P1365/P1366/P155/P156) | 3 000 |
| `culture.world_heritage_sites` | seed | items with a WH Site ID (P757); P1435 with start/end, criteria (P2614), country, location | 6 000 |
| `culture.world_heritage_designations` | statements | P1435 = World Heritage Site (Q9259) with start (inscription) / end (removal) | 6 000 |
| `culture.cultural_movements` | seed | cultural/art/literary movements (Q2198855/Q968159/Q3326717) with ≥ 3 sitelinks; periods, origin, influences | 8 000 |
| `culture.movement_members` | statements | P135 of humans with ≥ 20 sitelinks (split by QID prefix) | 40 000 |
| `culture.country_cuisines` | statements | P2012 (cuisine) of the 250 countries | 1 000 |

Entity budget, **bounded by construction**: seeded entities ≤ 70 000 (sum of seed `max_rows`,
overlapping) + subjects of the statement specs whose subjects are not the 250 countries
(`historical_state_*`, `movement_members`, `world_heritage_designations`) ≤ 74 000 rows ⇒ ≤ 144 000
entities ≤ 150 000 target (`test_entity_budget`). A spec that would exceed its bound fails with
`row_bound_exceeded` (never truncated). Full-size COUNT queries were not run: WDQS throttled this
machine's shared IP for most of the session (HTTP 429, Retry-After up to 900 s), so bounds are
upper estimates checked only by the anchors and by WDHARVEST's runs.

Design choices (honesty):

* **Seeds vs statements.** Seed specs give versioned snapshots (entity revision id, every
  allow-listed claim with qualifiers and references). Statement specs are used where the subject
  set is the 250 countries or a class closure, to make the qualifiers explicit and the row bound
  checkable. All statement specs bind the `wds:` statement node and the rank, filter deprecated rank.
* **Speakers are dated claims, not current estimates.** P1098 is kept with its point in time,
  applies-to-part (L1/L2 via P518/P3831), determination method and references. Consumers must show
  the date and the reference; values from different dates are never summed or compared silently.
* **P140 on countries is inconsistent on Wikidata** (sometimes state religion, sometimes majority
  religion with P1107 proportion, often absent). Absence is not evidence; the spec notes say so.
* **Sitelink thresholds** (religions ≥ 5, movements ≥ 3, movement members ≥ 20) are coverage
  bounds, not judgements of importance; stated in each spec's description.
* **Language genealogy.** Wikidata's P279 chain mixes genealogical branches with ontology classes:
  the unfiltered closure of the 7 anchor languages reached 139 items, climbing from "language" into
  BFO/DOLCE upper-ontology classes ("continuant", "entity") and contested macro-families without a
  Glottocode (Eurasiatic, Italo-Celtic). `language_ancestors` therefore seeds only ancestors with a
  Glottocode (P1394) — Glottolog's classification as mirrored on Wikidata. With the filter the
  live anchor query for Portuguese returns exactly 10 genealogical ancestors (Galician–Portuguese,
  West Iberian, Iberian Romance, Gallo-Iberian, Western Romance, Italo-Western, Romance,
  Latino-Faliscan, Italic, Indo-European; captured 2026-10-15 23:31 UTC) and none of the ontology
  classes. Swahili reaches Bantu and Niger–Congo, Arabic Semitic and Afroasiatic through Wikidata
  alone, so a Glottolog connector (CC BY 4.0, `glottolog_languoid.csv.zip`) was not needed; it
  remains the fallback.

### Colonial diffusion paths (no invented links)

The path *Brazil → official language → Portuguese* needs, hop by hop, statements that exist on
Wikidata and are harvested:

| Hop | Statement | Harvested by |
|---|---|---|
| Brazil → Portuguese | P37 (Q155 → Q5146) | `culture.country_official_languages` |
| Brazil → predecessor (Empire of Brazil, United Kingdom of Portugal, Brazil and the Algarves, State of Brazil) | P1365 / P155 | KGHIST `history.country_predecessors`, `history.state_succession` |
| colony → Portuguese Empire | P361 / P17 (with dates) | KGHIST `history.colonies` |
| Portuguese Empire → Portuguese | P37 / P2936 | `culture.historical_state_languages` (+ KGHIST `history.empires` snapshot) |

KGCULT does not create "spread language to" links: the path engine assembles the hops above and
labels each with its property ("official language", "replaces", "part of").

### Property requests

`registry/kg/property_requests/kgcult.yaml`: P219, P305, P1394, P4913, P1999, P3823, P1018, P506,
P144, P498, P2012, P2614 (main) and P1107, P1098, P1114 (qualifiers). Until WDHARVEST merges them,
`python -m reva.kg.harvest validate` reports the 7 specs that use them as invalid (no other errors).

## 2. UNESCO World Heritage List → atlas (`src/reva/adapters/connectors/atlas_unesco_whc.py`)

* **Source.** UNESCO open-data portal, dataset `whc001` (`data.unesco.org`, Opendatasoft API v2.1):
  licence **CC BY-SA 4.0** (declared in the dataset record), attribution "UNESCO". The legacy
  exports `whc.unesco.org/en/list/xml|json` and the WHC terms page (`/en/syndication`) return a
  Cloudflare browser challenge (HTTP 403) to non-browser clients, so they are not used; the licence
  applied is the one the dataset record states, and a licence change blocks the run.
* **Requests.** 2 per run (dataset record ~26 kB + JSON export of 19 columns ~0.7 MB). Budget
  request `registry/atlas/budget_requests/kgcult.yaml` (`unesco.whc`: 4/day). Source entry
  `registry/sources/unesco_whc.yaml`.
* **Atlas table** `unesco_whc.whc001` (geo scheme `iso2`), concepts in
  `registry/atlas/catalog/culture.yaml`: properties total / cultural / natural / mixed / in danger /
  transboundary (snapshot at the source revision day) and properties inscribed per year.
  Counts are per State Party (a transboundary property counts for each); `flags.aggregation =
  count`, `excludes_removed_properties = true` (Dresden 2009, Liverpool 2021, Arabian Oryx 2007 are
  absent from the current List). Per-year counts are *not* a reconstruction of the List in that year.
* **Coverage (2026-10-15 revision):** 1 273 properties (991 cultural, 240 natural, 42 mixed),
  172 mapped countries; 1 property without State Party code (Old City of Jerusalem, "site proposed
  by Jordan") kept as unmapped geography `states_names:…`, never assigned. 17 properties have no
  criteria in the export (flag `criteria_not_reported`; never inferred). Brazil: 26 properties
  (16 cultural, 9 natural, 1 mixed; 1 transboundary with Argentina).
* **Tests.** `tests/unit/test_atlas_unesco_whc.py` (captured live fixture
  `tests/fixtures/source/atlas.unesco_whc/`, recorded 2026-10-15 22:08 UTC through SafeHttpClient),
  `tests/integration/test_atlas_unesco_whc_ingest.py` (throwaway DB through
  `reva.atlas.ingest.run_atlas_ingest(mode="fixture")`: archive raw → `register_atlas_table`,
  idempotent re-run; a `live`-marked budgeted run that skips until `unesco.whc` has a budget).

### Not provided (no sourced, openly licensed series)

* **Linguistic diversity index.** Greenberg's LDI is published by Ethnologue (SIL, proprietary
  licence). Computing one from Wikidata P2936/P1098 would be a REVA estimate over community-edited
  data, not a source value: not done.
* **Number of official languages per country** stays a KG fact (P37 statements with regional
  P518 and temporal qualifiers), not an atlas cell: a count would hide those qualifiers.

## 3. Anchors

See `tests/unit/test_kg_queries_culture.py` (offline; fixtures `tests/fixtures/kg/culture/*.json`
captured through the locked `WikidataClient`).

## 4. Requests to other agents / lead

* **Lead:** none for the connector: it follows the atlas source protocol (`atlas_source()` +
  `atlas_ingest(run)`, discovered by module name; run with
  `uv run python -m reva.atlas.ingest atlas.unesco_whc [--fixture]`). The seeded deployment needs
  the lead's `register_atlas_connector_versions` seed step (already planned by ATLASCORE).
* **ATLASCORE:** merge `registry/atlas/budget_requests/kgcult.yaml` (`unesco.whc`). Until merged,
  `tests/invariants/test_invariant_30_budgets.py::test_budget_keys_are_registered_sources_and_cover_every_connector`
  sees `unesco.whc` in `registry/sources/` without a budget key.
* **WDHARVEST:** merge `registry/kg/property_requests/kgcult.yaml`; run the culture specs
  (`religion_ancestors` after `religions`).
