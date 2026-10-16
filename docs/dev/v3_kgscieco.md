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

`max_rows` is the declared upper bound (the spec fails rather than truncating). "live" = result of a live
check through WDHARVEST's client (see "Test queries"); "—" = not reached because of WDQS throttling.

| Spec | Purpose / selection | max_rows | live |
|---|---|---:|---:|
| science.discoveries_inventions | items with P61 or P575, ≥ 15 sitelinks, not humans | 12000 | — |
| science.chemical_elements | P31 chemical element (Q11344); `api_search` too | 200 | try: 5/5 rows, 7.4 s |
| science.eponymous_laws_theories | direct P31 of 12 classes (physical/scientific/gas law, scientific theory, physical phenomenon, theorem, constant, equation, unit, SI base/derived unit, conjecture), ≥ 5 sitelinks; split by VALUES (3 classes/request) | 15000 | — |
| science.academic_disciplines | P31 academic discipline / branch of science, ≥ 10 | 6000 | — |
| science.universities | P31 university / public / private / research / university of technology / collegiate, ≥ 20 | 8000 | — |
| science.research_institutes | P31 research institute / academy of sciences / space agency, ≥ 10, + CERN, Max Planck Society, Pasteur Institute | 4000 | — |
| science.science_awards | P31/P279* science award, ≥ 10, + the 6 Nobel prizes | 3000 | — |
| science.award_recipients | statements: P166 (+P585 year) to every seeded award; people **and organisations** | 80000 | — |
| science.space_missions | P31 satellite / Earth satellite / space probe / flyby probe / space mission / human spaceflight / space telescope / space laboratory, ≥ 10 | 4000 | — |
| science.observatories | P31 astronomical observatory / observatory, ≥ 5 | 3000 | — |
| science.vaccines | P1924 vaccine-for ∪ P31/P279* vaccine ∪ P31 vaccine type, ≥ 3 | 3000 | — |
| economy.central_banks | P31/P279* central bank; `api_search` too | 3000 | — |
| economy.stock_exchanges | P31/P279* stock exchange, ≥ 3 | 2000 | — |
| economy.major_companies | P452 industry ∪ P414 listed, ≥ 40 sitelinks, not humans | 8000 | — |
| economy.colonial_trading_companies | P31/P279* chartered company / East India company ∪ P31 colonial society ∪ trading company founded < 1900 ∪ 9 named companies | 3000 | — |
| economy.trade_agreements | P31/P279* free trade agreement / trade bloc / free trade area / customs union / currency union, ≥ 3 | 2500 | — |
| economy.economic_crises | P31/P279* economic crisis / financial crisis / recession / oil crisis / stock market crash / bubble / hyperinflation / depression, ≥ 3, + Marshall Plan, Bretton Woods system | 3000 | — |
| geography.nature.highest_points | objects of P610/P1589 of the 250 crosswalk countries | 600 | — |
| geography.nature.mountains | P31 mountain / volcano / stratovolcano / massif ≥ 30, mountain range ≥ 20 | 6000 | — |
| geography.nature.deserts | P31 desert, ≥ 5 | 1500 | — |
| geography.nature.rivers | P31 river, ≥ 25 | 6000 | — |
| geography.nature.lakes | P31 lake / salt lake / endorheic lake / reservoir, ≥ 25 | 5000 | — |
| geography.nature.seas_oceans | P31 ocean / sea / marginal sea / adjacent sea / gulf / bay / strait, ≥ 15 | 3000 | — |
| geography.nature.earthquakes | P31/P279* earthquake or tsunami, ≥ 3; year split (−2000..2029, 500 y) + undated | 12000 | — |
| geography.nature.volcanic_eruptions | P31/P279* volcanic eruption; `api_search` too | 4000 | — |
| geography.nature.famines | P31/P279* famine; `api_search` too | 2000 | — |
| geography.nature.epidemics | P31/P279* disease outbreak / epidemic / pandemic, ≥ 3 | 10000 | — |
| geography.nature.other_disasters | P31/P279* natural disaster / flood / tropical cyclone with ≥ 100 deaths or ≥ 15 sitelinks; year split (−1000..2029, 100 y) + undated | 12000 | — |
| geography.nature.national_parks | P31/P279* national park, ≥ 5 | 6000 | — |

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

## Example chains enabled (checked on the live items, 2026-10-16)

Checked with one `wbgetentities` request (claims of 50 anchor items, raw sha256 `b669861e4596…`, archived
under `var/kg/objects`) through WDHARVEST's budgeted, locked client. "now" = edges whose property is already
allow-listed; "after request" = edges that appear once the requested property is allow-listed (the seed specs
need no edit). All hops are community-edited Wikidata statements; a path is a connection, not a cause, except
where the hop is P828/P1542/P1478 ("asserted by Wikidata").

| Chain | Hops (Wikidata ids) | Status |
|---|---|---|
| Polonium → named after → Poland; discovered by Marie & Pierre Curie, 1898 | Q979 P138 Q36; Q979 P61 Q7186/Q37463; P575 1898 | P61/P575 now (science.chemical_elements); P138 after request |
| Curium → named after → Marie & Pierre Curie; einsteinium → named after → Einstein | Q1876 P138 Q7186/Q37463; Q1892 P138 Q937 | after request (P138) |
| Edward Jenner → smallpox vaccine (1798) → vaccine for → smallpox | Q1037810 P61 Q40852, P575 1798, P1924 Q12214 | P61 now (science.vaccines); P1924 after request |
| Pasteur → rabies vaccine → France | rabies vaccine Q4007169 has **no P61** (only P1924 → rabies Q39222); the Pasteur hop would need his own P800 "notable work" (KGPEOPLE snapshots) | **not asserted** on the vaccine item — gap |
| Great Depression → has cause → Wall Street crash of 1929; the crash → has effect → Great Depression | Q8698 P828 Q201684; Q201684 P1542 Q8698; Q8698 P17 → France, US, … | now (economy.economic_crises; causal, asserted by Wikidata) |
| 1918 flu pandemic → has cause → World War I | Q178275 P828 Q361 (P1478 immediate cause Q161393) | now (geography.nature.epidemics; asserted by Wikidata) |
| 1973 oil crisis → has cause → Yom Kippur War | Q316817 carries **no P828/P1542** today (only dates 1973-10-16..1974-03-17); the Yom Kippur War (Q49100) is seeded by history.wars | **not asserted by Wikidata** — the path engine will only say "connected via" if another hop links them |
| 1815 Tambora eruption → has effect → Year Without a Summer; eruption → country → Dutch East Indies ← replaced by ← Indonesia | Q3591483 P1542 Q209625; Q3591483 P17 Q188161; Indonesia Q252 P1365 Q188161 | now (volcanic_eruptions + core.countries + history.colonies) |
| 2011 Tōhoku earthquake → has effect → (tsunami / Fukushima accident) → Japan | Q36204 P1542 Q1136168, Q171178; P17 Q17; P1120 19 759 | now (geography.nature.earthquakes) |
| Dutch East India Company → Dutch Republic; → Indonesia → Dutch language | Q159766 P17 Q170072 (1602–1799). The company has **no P2541 operating area** and no edge to the Dutch East Indies; Indonesia Q252 P1365 Q188161 (Dutch East Indies) | company → Dutch Republic now; company → Indonesia **missing in Wikidata** (P2541 requested, but VOC has none) — gap |
| Toyota → listed on → Tokyo / London / New York stock exchanges; Toyota → Japan → central bank → Bank of Japan | Q53268 P414 Q217475/Q171240/Q13677; Q53268 P17 Q17; Q17 P1304 Q333101 | P17 now; P414 and P1304 after request |
| France ↔ Mont Blanc ↔ Italy (shared highest point) | Q142 P610 Q583; Q38 P610 Q583 | after request (P610; core.countries + geography.nature.highest_points) |
| Nile → mouth → Mediterranean Sea; Nile → origin → Lake Victoria; basin countries Egypt, Sudan, Ethiopia … | Q3392 P403 Q4918, P885 Q5505, P205 (7 countries); Danube Q1653 P403 Q166 (Black Sea) | P17 now; P403/P885/P205 after request |
| Sputnik 1 → operator → Soviet Union; Apollo 11 → operator → NASA → US; Chandrayaan-3 → ISRO → India | Q80811 P137 Q15180; Q43653 P137 Q23548; Q65049774 P137 Q229058, P17 Q668 | now (science.space_missions) |
| Fields Medal → conferred by → International Mathematical Union; → named after → J. C. Fields | Q28835 P1027 Q828490, P138 Q315431 | after request (P1027/P138); recipients + years now (science.award_recipients) |
| Mount Everest → country → Nepal, China; Caspian Sea → basin countries → Russia, Iran, Kazakhstan, Azerbaijan, Turkmenistan | Q513 P17 Q837/Q148; Q5484 P205 … | P17 now; P205 after request |

## Test queries (live, through WDHARVEST's client only)

WDQS and the Wikidata API were heavily throttled during this work (shared machine-wide lock; repeated
429 cool-downs of ~17 min set by the service, plus WDHARVEST's own API-mode harvest holding the slot). Every
request went through `reva.kg.harvest.budgeted_client` / `python -m reva.kg.harvest try` (lock, pacing,
Retry-After, budget meter, raw archive). No direct curl/httpx.

TRIES_PLACEHOLDER

## Validation

* `uv run python -m reva.kg.harvest validate` → **99 specs, 99 valid** (29 of them KGSCIECO), no network.
* `uv run pytest tests/unit/test_kg_queries_scieco.py` → **7 passed**: harvester validator (schema,
  allow-list, placeholders, no LIMIT/label service), layout/owner, bounds (timeouts ≤ 60 s, entity budget
  ≤ 250 000), no requested property used as a column, **every QID/PID in the specs and requests has the
  expected English label** in the recorded label check, requested labels match Wikidata, fixture re-read from
  the archived raw responses when present. ruff + mypy clean.

## Gaps and honest limits

* **Row counts are bounds, not measurements** for the specs not listed under "Test queries": WDQS throttling
  prevented the planned `COUNT(*)` pass (the job was stopped after ~45 min without a single slot). Every spec
  fails loudly (`row_bound_exceeded`) rather than truncating if a bound is too low; WDHARVEST should run
  `science.*`/`economy.*` first and adjust `max_rows` from the run report if needed.
* Sitelink thresholds are a coverage choice (notability), not importance: small countries' rivers, firms and
  universities with fewer Wikipedia articles are under-represented. Lower thresholds per spec when budget allows.
* Class-based selection depends on Wikidata typing: inventions are typed inconsistently (telephone, transistor,
  steam engine have unrelated P31 classes), hence the P61/P575 selection; inventions with neither are missed.
  Kruger is typed "national park of South Africa" (subclass — covered by P279*), Kilimanjaro "massif" (covered).
* Requested properties carry most nature/economy edges (mouths, basin countries, highest points, central
  banks, listings, operating areas, eponyms). Until merged, those hops are absent from the graph.
* Wikidata itself lacks some expected edges (checked above): no cause on the 1973 oil crisis, no
  operating area on the Dutch East India Company, no inventor on the rabies vaccine. They are reported, not
  filled: REVA does not invent links.
* Economic magnitudes (GDP, revenue, market capitalisation, trade) are atlas data (A-WB, A-IMF), not KG.
* `science.award_recipients` depends on the seeds of `science.science_awards` (run order declared).
* `api_search` (WDQS-free seeding) is declared only for small class-only specs (chemical elements, central
  banks, volcanic eruptions, famines); the sitelink-bounded specs need WDQS.
