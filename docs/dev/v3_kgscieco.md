# V3 KGSCIECO — science, technology, economy and the physical world in the KG

Agent KGSCIECO, 2026-10-15/16. Spec: `docs/roadmap/v3_world_graph.md`. Format and executor: WDHARVEST
(`registry/kg/queries/README.md`, `_schema.json`, `src/reva/kg/harvest.py`). Storage/path engine: KGCORE
(`docs/dev/v3_kgcore.md`).

Goal: connect discoveries, inventions, awards, institutions, space programmes, money, companies, crises and the
physical planet (mountains, rivers, seas, disasters) to the countries, people and events seeded by the other
KG agents, so the path engine can walk e.g. *Pasteur → rabies vaccine → France* or *Dutch East India Company →
Indonesia → Dutch*. Nothing here is a harvest run: these are query specs; WDHARVEST runs them.

## Files

| Path | What |
|---|---|
| `registry/kg/queries/science/*.yaml` | 12 specs (11 seed, 1 statements) |
| `registry/kg/queries/economy/*.yaml` | 6 seed specs |
| `registry/kg/queries/nature/{landforms,water,disasters,protected}/*.yaml` | 12 seed specs (domain `geography`, ids `geography.nature.*`) |
| `registry/kg/property_requests/kgscieco.yaml` | 25 main-property requests + 1 qualifier (same lines as "Requests" below) |
| `tests/unit/test_kg_queries_scieco.py` | offline checks: validator, layout, bounds, budget, every QID/PID label-verified |
| `tests/fixtures/kg/scieco/qid_labels.json` | recorded wbgetentities label check of every id used (sha256s of the archived raws) |

**Nature layout.** The schema's `domain` enum has no `nature` (it has `geography`), and `validate_spec`
checks domain == directory only for files directly under `registry/kg/queries/<domain>/`. Nature specs
therefore live one level deeper (`nature/<topic>/x.yaml`), declare `domain: geography` and use ids
`geography.nature.<name>`; they validate today and do not collide with any other domain. If the lead adds
`nature` to the enum, a rename of the ids is mechanical (the ids are the resume-cursor keys; nothing has run).

**Design choice: seeds, not statement lists.** Almost every spec is `seed_entities` *without* a `properties`
restriction: each selected item gets a full `wbgetentities` snapshot filtered to the allow-list. The edges
(P61 discoverer, P575 time, P495 origin, P17 country, P828/P1542 cause/effect, P1120 deaths, P571/P576 …)
come with their statement ids, qualifiers and references, and every property requested below is picked up on
the next (re-)harvest *without any spec edit* once WDHARVEST allow-lists it (same for `core.countries`,
which is unrestricted: P610 highest point, P1304 central bank arrive there). Only award edges, whose subject
(the laureate) is not otherwise seeded, use a `statements` spec. Bounds are Wikipedia reach (sitelinks)
thresholds — a coverage choice, stated in each description, not a judgement of importance.

## Specs

`max_rows` is the declared upper bound (the spec fails rather than truncating); "counted" is a WDQS
`SELECT (COUNT(*) …)` over the spec's own SPARQL with the split placeholders filled for the whole range
(see "Test queries").

| Spec | Purpose / selection | max_rows | counted |
|---|---|---:|---:|
| science.discoveries_inventions | items with P61 or P575, ≥ 15 sitelinks, not humans | 12000 | COUNT_science.discoveries_inventions |
| science.chemical_elements | P31 chemical element (Q11344); `api_search` too | 200 | COUNT_science.chemical_elements |
| science.eponymous_laws_theories | direct P31 of 12 classes (physical/scientific/gas law, scientific theory, physical phenomenon, theorem, constant, equation, unit, SI base/derived unit, conjecture), ≥ 5 sitelinks; split by VALUES (3 classes/request) | 15000 | COUNT_science.eponymous_laws_theories |
| science.academic_disciplines | P31 academic discipline / branch of science, ≥ 10 | 6000 | COUNT_science.academic_disciplines |
| science.universities | P31 university / public / private / research / university of technology / collegiate, ≥ 20 | 8000 | COUNT_science.universities |
| science.research_institutes | P31 research institute / academy of sciences / space agency, ≥ 10, + CERN, Max Planck Society, Pasteur Institute | 4000 | COUNT_science.research_institutes |
| science.science_awards | P31/P279* science award, ≥ 10, + the 6 Nobel prizes | 3000 | COUNT_science.science_awards |
| science.award_recipients | statements: P166 (+P585 year) to every seeded award; people **and organisations** | 80000 | COUNT_science.award_recipients |
| science.space_missions | P31 satellite / Earth satellite / space probe / flyby probe / space mission / human spaceflight / space telescope / space laboratory, ≥ 10 | 4000 | COUNT_science.space_missions |
| science.observatories | P31 astronomical observatory / observatory, ≥ 5 | 3000 | COUNT_science.observatories |
| science.vaccines | P1924 vaccine-for ∪ P31/P279* vaccine ∪ P31 vaccine type, ≥ 3 | 3000 | COUNT_science.vaccines |
| economy.central_banks | P31/P279* central bank; `api_search` too | 3000 | COUNT_economy.central_banks |
| economy.stock_exchanges | P31/P279* stock exchange, ≥ 3 | 2000 | COUNT_economy.stock_exchanges |
| economy.major_companies | P452 industry ∪ P414 listed, ≥ 40 sitelinks, not humans | 8000 | COUNT_economy.major_companies |
| economy.colonial_trading_companies | P31/P279* chartered company / East India company ∪ P31 colonial society ∪ trading company founded < 1900 ∪ 9 named companies | 3000 | COUNT_economy.colonial_trading_companies |
| economy.trade_agreements | P31/P279* free trade agreement / trade bloc / free trade area / customs union / currency union, ≥ 3 | 2500 | COUNT_economy.trade_agreements |
| economy.economic_crises | P31/P279* economic crisis / financial crisis / recession / oil crisis / stock market crash / bubble / hyperinflation / depression, ≥ 3, + Marshall Plan, Bretton Woods system | 3000 | COUNT_economy.economic_crises |
| geography.nature.highest_points | objects of P610/P1589 of the 250 crosswalk countries | 600 | COUNT_geography.nature.highest_points |
| geography.nature.mountains | P31 mountain / volcano / stratovolcano / massif ≥ 30, mountain range ≥ 20 | 6000 | COUNT_geography.nature.mountains |
| geography.nature.deserts | P31 desert, ≥ 5 | 1500 | COUNT_geography.nature.deserts |
| geography.nature.rivers | P31 river, ≥ 25 | 6000 | COUNT_geography.nature.rivers |
| geography.nature.lakes | P31 lake / salt lake / endorheic lake / reservoir, ≥ 25 | 5000 | COUNT_geography.nature.lakes |
| geography.nature.seas_oceans | P31 ocean / sea / marginal sea / adjacent sea / gulf / bay / strait, ≥ 15 | 3000 | COUNT_geography.nature.seas_oceans |
| geography.nature.earthquakes | P31/P279* earthquake or tsunami, ≥ 3; year split (−2000..2029, 500 y) + undated | 12000 | COUNT_geography.nature.earthquakes |
| geography.nature.volcanic_eruptions | P31/P279* volcanic eruption; `api_search` too | 4000 | COUNT_geography.nature.volcanic_eruptions |
| geography.nature.famines | P31/P279* famine; `api_search` too | 2000 | COUNT_geography.nature.famines |
| geography.nature.epidemics | P31/P279* disease outbreak / epidemic / pandemic, ≥ 3 | 10000 | COUNT_geography.nature.epidemics |
| geography.nature.other_disasters | P31/P279* natural disaster / flood / tropical cyclone with ≥ 100 deaths or ≥ 15 sitelinks; year split (−1000..2029, 100 y) + undated | 12000 | COUNT_geography.nature.other_disasters |
| geography.nature.national_parks | P31/P279* national park, ≥ 5 | 6000 | COUNT_geography.nature.national_parks |

Budget: seeds ≤ 147,800 entities + ≤ 80,000 award statements (upper bounds; the test caps the sum at
250,000). At KGCORE's measured ~0.6 KB/statement the expected volume is well inside the 6 GB KG budget.
`expand_hops: 1` (label snapshots of objects) is used where objects are few and valuable (discoveries, awards,
crises, colonial companies, disasters); the large geography specs use `0` (their objects — countries, seas,
ranges — are seeded by other specs; admin regions stay bare QIDs) to save `wbgetentities` calls.

### Not duplicated (checked against the other owners' specs)

* **Currencies with periods of use per country** — KGCULT `culture.country_currencies` (P38 + P580/P582),
  `culture.currencies`, `culture.historical_state_currencies`. Only the issuer/central-bank hops are added
  here (P562/P1304 requests + `economy.central_banks`).
* **Economic unions and memberships with dates** — KGGEOPOL `geopolitics.international_organisations`
  (every org with ≥ 3 state members + intergovernmental orgs: EU/EEC, Comecon, OPEC, WTO, ASEAN, EFTA,
  Mercosur, GCC, EAEU …) and `geopolitics.state_memberships` (P463 with start/end, role, end cause).
  `economy.trade_agreements` only adds agreements/blocs typed as FTA / trade bloc / customs or currency union
  (NAFTA is typed "trade bloc" + "free trade agreement", not treaty; eurozone is "currency union").
* **Nobel laureates as people** — KGPEOPLE `people.nobel_laureates` (+ scientists ≥ 25 sitelinks). The
  award *edges* with years (and organisation laureates) are `science.award_recipients`.
* **Treaties, wars, colonies, voyages** — KGHIST (`history.treaties`, `history.wars` incl. Yom Kippur War,
  `history.colonies`, `history.expeditions`).
* **Seas a country borders** — P206 is allow-listed and harvested with `core.countries`.

## Requests

### To WDHARVEST / lead: properties (also in `registry/kg/property_requests/kgscieco.yaml`)

Every id, its Wikidata datatype and English label were checked with `wbgetentities` (recorded in
`tests/fixtures/kg/scieco/qid_labels.json`; datatypes in the probe log). `merge-property-requests` re-checks
them and takes the official en/fr labels. Proposed allow-list lines (format of `properties.yaml`):

```yaml
  - {property: P138,  reva_predicate: named_after,        inverse: eponym_of,             kind: entity,   group: science}
  - {property: P1027, reva_predicate: conferred_by,       inverse: confers,               kind: entity,   group: science}
  - {property: P1924, reva_predicate: vaccine_for,        inverse: prevented_by_vaccine,  kind: entity,   group: science}
  - {property: P178,  reva_predicate: developer,          inverse: developer_of,          kind: entity,   group: science}
  - {property: P176,  reva_predicate: manufacturer,       inverse: manufacturer_of,       kind: entity,   group: science}
  - {property: P619,  reva_predicate: launch_date,        inverse: null,                  kind: time,     group: science}
  - {property: P1304, reva_predicate: central_bank,       inverse: central_bank_of,       kind: entity,   group: economy}
  - {property: P562,  reva_predicate: issuer,             inverse: issuer_of,             kind: entity,   group: economy}
  - {property: P414,  reva_predicate: listed_on,          inverse: lists,                 kind: entity,   group: economy}
  - {property: P2541, reva_predicate: operating_area,     inverse: operating_area_of,     kind: entity,   group: economy}
  - {property: P1056, reva_predicate: produces,           inverse: produced_by,           kind: entity,   group: economy}
  - {property: P610,  reva_predicate: highest_point,      inverse: highest_point_of,      kind: entity,   group: nature}
  - {property: P1589, reva_predicate: lowest_point,       inverse: lowest_point_of,       kind: entity,   group: nature}
  - {property: P2044, reva_predicate: elevation,          inverse: null,                  kind: quantity, group: nature}
  - {property: P4552, reva_predicate: in_mountain_range,  inverse: mountain_range_of,     kind: entity,   group: nature}
  - {property: P403,  reva_predicate: mouth,              inverse: receives_watercourse,  kind: entity,   group: nature}
  - {property: P885,  reva_predicate: watercourse_origin, inverse: origin_of_watercourse, kind: entity,   group: nature}
  - {property: P974,  reva_predicate: tributary,          inverse: tributary_of,          kind: entity,   group: nature}
  - {property: P4614, reva_predicate: drainage_basin,     inverse: drainage_basin_of,     kind: entity,   group: nature}
  - {property: P205,  reva_predicate: basin_country,      inverse: country_in_basin_of,   kind: entity,   group: nature}
  - {property: P2043, reva_predicate: length,             inverse: null,                  kind: quantity, group: nature}
  - {property: P2527, reva_predicate: magnitude_moment,   inverse: null,                  kind: quantity, group: nature}
  - {property: P2528, reva_predicate: magnitude_richter,  inverse: null,                  kind: quantity, group: nature}
  - {property: P1603, reva_predicate: number_of_cases,    inverse: null,                  kind: quantity, group: nature}
  - {property: P814,  reva_predicate: iucn_category,      inverse: iucn_category_of,      kind: entity,   group: nature}
# qualifier list:
  - {property: P1686, expected_en: "for work"}   # award received -> the work it was awarded for
```

Priority if only a few are accepted: **P138** (eponyms: elements, laws, units → people and places), **P610**
(highest point per country), **P403/P205** (river mouths, basin countries), **P2541** (colonial company →
territory), **P1304/P562** (country/currency → central bank), **P1924** (vaccine → disease).
`P448` (launch site) was dropped: `wbgetentities` reports it missing.

### To the lead

1. Optional: add `"nature"` to the `domain` enum of `_schema.json` if the nested `nature/<topic>/` layout
   with `domain: geography` is not wanted (see "Nature layout").
2. Run order: `science.science_awards` before `science.award_recipients` (declared in `depends_on`).

RESULTS_PLACEHOLDER
