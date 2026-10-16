# V3 A-CITIES — cities and urbanisation for every country

Agent A-CITIES, 2026-10-15/16. Two atlas sources and one KG link, all through the single atlas ingest entry point
(`reva.atlas.ingest.run_atlas_ingest`) and WDHARVEST's Wikidata client. No HTTP client is built in these modules.

## What was built

| atlas source (connector) | module | atlas tables | requests / run |
|---|---|---|---:|
| `atlas.geonames_cities` (source key `geonames.cities`, CC BY 4.0) | `src/reva/adapters/connectors/atlas_cities.py` | `geonames.cities15000`, `geonames.cities15000_by_country`, `geonames.admin1`, `geonames.cities15000_names` (on request) | 2 (3 with names) |
| `atlas.un_wup` (source key `un.wup`, CC BY 3.0 IGO) | `src/reva/adapters/connectors/atlas_wup.py` | `un_wup.wup2025_urban_rural`, `un_wup.wup2025_degurba`, `un_wup.wup2025_cities`, `un_wup.wup2025_cities_area`, `un_wup.wup2025_capitals` | 4 |

Other files:

* sources `registry/sources/{geonames_cities,un_wup}.yaml`;
* budget request `registry/atlas/budget_requests/a_cities.yaml`;
* catalog `registry/atlas/catalog/urban.yaml` (20 provider-declared concepts, each with an explicit `theme`);
* geo alias `registry/atlas/geo_aliases/a_cities.yaml` (scheme `geonames_cc` = ISO alpha-2 + `XK` → `XKX`,
  quoted from GeoNames countryInfo);
* KG query specs `registry/kg/queries/places/wikidata/{geonames_ids,country_capitals}.yaml`;
* fixtures `tests/fixtures/source/atlas.{geonames_cities,un_wup}/` (excerpts of the real bytes, ~110 kB);
* tests `tests/unit/test_atlas_cities.py` (18, offline) and `tests/integration/test_atlas_cities_ingest.py`
  (2, `db`, fixture mode through the entry point).

Run:

```bash
uv run python -m reva.atlas.ingest atlas.geonames_cities [--dataset cities15000] [--option altnames=1] [--fixture]
uv run python -m reva.atlas.ingest atlas.un_wup [--dataset wup2025_capitals] [--fixture]
# Wikidata links (throwaway reva_* database that holds the GeoNames ingest):
uv run python -m reva.adapters.connectors.atlas_cities wikidata --db reva_xxx --atlas-object-root <root> \
    [--labels none|capitals|all] [--max-chunks N] [--report out.json]          # WDQS fan-out (all places)
uv run python -m reva.adapters.connectors.atlas_cities wikidata --db reva_xxx --atlas-object-root <root> \
    --via api [--max-ids N] [--client-mode linked_data]                        # WDQS-free (capitals + N ids)
```

## Design and honesty decisions

### GeoNames (cities15000, admin1, alternate names)

* **One row per place, folded into per-country atlas rows.** Atlas rows are per country, so `provider_geo` is
  the GeoNames country code (verbatim) and the place is folded into the indicator, as other agents fold a
  counterpart: `population|geonameid=2267057`, `name|…`, `asciiname|…`, `feature_code|…`, `admin1_code|…`,
  `latitude|…`, `longitude|…`, `elevation|…`, `dem_elevation|…`, `timezone|…`, `cc2|…`, `modification_date|…`.
  `entity_qid` stays null: the place's Wikidata item lives in the KG link (below), not in the atlas table.
* **Values as GeoNames states them.** GeoNames dates nothing, so every row uses the one-day retrieval snapshot as
  period (`flags.period_basis`, as A-GEO does for countryInfo). The place record's `modification date` is kept in
  the population flags; it is the last edit of the record, not the date of the figure, and the flags say so.
* **No filled numbers.** A population of `0` means "not known" in GeoNames (3 places, e.g. Plymouth, Montserrat;
  Ngerulmud, Palau). Those cells and DEM `-9999` ("no data") are omitted and counted, never stored as numbers.
  Empty elevations (most places) are omitted too.
* **Per-country aggregates only when exact.** They are counts and selections over this one file, so they are
  exact with respect to GeoNames:
  * places with population > 15,000, with and without PPLX (sections of a populated place), PPLH, PPLQ, PPLW
    and PPLCH;
  * the largest place and its population (ties listed);
  * the PPLC places and their count.
  * When a country has several PPLC places, all are listed (`A | B`, `multiple_pplc`) and none is chosen. A capital
    population is given only when there is exactly one PPLC place.
  * Three country codes have **no** PPLC place at all: EH, IL and PS. Jerusalem is coded PPLA. This is reported,
    not filled.
* **fr/en alternate names** need the 205 MB `alternateNamesV2.zip`; the 19-column cities file has no language
  tags. They are fetched only on request (`--dataset cities15000_names` or `--option altnames=1`). The file is
  streamed line by line, never held decompressed. Only `fr`/`en` names of cities15000 places are kept, at most 5
  per place and language:
  * order: preferred, then short, then the others; colloquial and historic names come last;
  * GeoNames' preferred/short/colloquial/historic/from/to flags are kept;
  * the number of names not kept is counted.
* **admin1CodesASCII** becomes `geonames.admin1`: name, ASCII name and geonameid per division, plus the count per
  country. Each place's `admin1_code` row carries the division name and geonameid in its flags.

### UN World Urbanization Prospects

* **Latest revision: WUP 2025.** It was released 2025-11-18 and is the 22nd edition. Its FAQ says it "supersedes
  all previous estimates and projections"; the previous edition was WUP 2018.
  * Licence statement in every WUP 2025 workbook: "Copyright © 2025 by United Nations, made available under a
    Creative Commons license CC BY 3.0 IGO".
  * The connector checks that this statement is still in the capitals workbook.
* **Two concepts of "urban", never mixed:**
  * national definitions (`wup2025_urban_rural`: Urban, Rural, Total, 1950–2050), which is the WUP 2018 concept;
  * the harmonised **Degree of Urbanisation** (`wup2025_degurba`: Cities, Towns, Rural, Cities and Towns, Total;
    population, share, land and built-up areas from 1975). This is new in 2025 and based on EC-JRC grids.
* **Cities.** WUP 2025 dropped the WUP 2018 "urban agglomerations ≥ 300,000" list (1950–2035). It publishes
  16,828 Degree-of-Urbanisation cities instead: ≥ 50,000 inhabitants and ≥ 1,500 per km², 1975–2050.
  * `wup2025_cities`: mid-year population per city and year, plus the population-weighted centroid of the city's
    latest estimate year.
  * `wup2025_cities_area`: land area, built-up area and density.
  * `flags.pop_plausibility` (Low/Moderate/High) is kept as the UN states it.
  * The WUP 2018 agglomeration list (1950–1970 values) is **not** ingested: it is superseded, and it ships only as
    legacy `.xls` workbooks, for which the project has no reader. See gaps.
* **Projections are separate series.** Every year ≥ 2026 carries the indicator suffix `|proj=wup2025` and
  `flags.projection = true`. The catalog has separate `_proj` concepts, so an estimate lookup can never return a
  projection.
* **Capitals (`wup2025_capitals`, file F20).**
  * Every capital row is kept, with its `Capital_Type` verbatim. There are 33 types, from "National capital" and
    "Seat of Government/Economic Capital/De facto capital" to "Territorial capital/De jure capital (evacuated due
    to volcano)".
  * Several capitals of one country are all kept. Malaysia lists Kuala Lumpur and Putrajaya; Sri Lanka lists the
    same settlement twice with two roles.
  * `national_capital_pop` exists only when exactly one row is a "National capital…".
  * A capital that is no DEGURBA settlement has no city code and no population (Plymouth in Montserrat, Alofi in
    Niue, Ngerulmud in Palau). It is named and given no number.
* **Unit metadata.** The UN indicator list gives "thousands" as unit of the mid-year `AREA_km2`/`BU_km2` columns.
  Those columns equal the 1 January km² columns, so they are stored as `km2` and the discrepancy is flagged
  (degurba rows) or noted (city coverage note).
* Aggregates (World, SDG regions, income groups…) are kept with `iso3 = null`. Geography uses A-UN's `un_m49x`
  scheme: 412 → XKX; 158 → TWN through the crosswalk.

### KG link: GeoNames place → Wikidata item (never by name)

* A place is linked to a Wikidata item **only** through Wikidata's own GeoNames ID statements (P1566):
  * Spec `geography.places_geonames_ids` uses `VALUES ?gn { "…" }`, up to 300 ids per WDQS request.
  * It runs through WDHARVEST's locked, paced, budget-metered `WikidataClient` (`reva.kg.harvest.budgeted_client`).
  * Rows are converted by WDHARVEST's `Harvester.rows_to_statements` and loaded by its `KgDbSink` as
    `wikidata.wdqs` P1566 statements, with statement ids and rank.
  * Chunks are resumable and bounded to 4 rows per id (`row_bound_exceeded`, never truncated).
  * Ids are sent in priority order: capitals and seats of government, admin-1 divisions, PPLA seats, then places by
    population. A throttled partial run therefore maps the important places first.
* A geonameid held by **exactly one** item is mapped. Held by several items, it is reported as ambiguous and not
  used.
* The GeoNames-sourced statements (source `geonames.cities`, reference = cities15000 line + raw sha256) are loaded
  only on mapped items:
  * `P17` country;
  * `P1376` capital of (PPLC only);
  * `P131` admin-1 division (only when the division's own geonameid is mapped);
  * `P1082` population (no date: GeoNames states none).
* Every statement records `subject_mapped_via: {property: P1566, wikidata_statement_id}`.
* Country objects come from `registry/entities/country_qids.json` (WDHARVEST). Without it, the country-valued
  statements are reported as blocked, as in A-GEO.
* Chain: "Lisbonne → capitale du Portugal" = Q597 —P1376 (GeoNames PPLC, `geonames.cities`)→ Q45. Wikidata's own
  P36/P1376 claims come from the core harvest. The fr label "Lisbonne" comes from the Wikidata label snapshot
  (`--labels`) or from the GeoNames fr alternate names.
* **Capital cross-check** (spec `geography.places_country_capitals`, one WDQS request): GeoNames PPLC is compared
  with Wikidata P36 for every item holding a P298 code. Only current statements count (no end time; rank
  preferred/normal). Agreement means a PPLC geonameid is one of the capital's own P1566 values. Each country gets
  a status, listed with both sides and never resolved:
  * `agree`;
  * `agree_partial_multiple`;
  * `disagree`;
  * `geonames_only`;
  * `wikidata_only`.
* **WDQS-free path** (`--via api`, used live on 2026-10-16). It applies when WDQS answers our shared egress with
  `429 Retry-After: 1000` on every request.
  * The countries' own snapshots (`country_qids.json`; Special:EntityData through WDHARVEST's client in
    `linked_data` mode, recent archived snapshots reused) give their P36 claims.
  * Every P36 object is loaded as a full snapshot, its claims filtered to P31/P17/P131/P1376/P1566/P625/P1082,
    with source `wikidata.wbapi` and the entity revision. Its own P1566 claims are the link: still Wikidata's
    statements, never a name.
  * `--max-ids N` additionally looks up the first N priority geonameids by CirrusSearch
    (`haswbstatement:P1566=…`, candidates only, verified by their snapshots' P1566 claims). That needs the
    action/REST search API, which was saturated during the run.
* The two specs are `enabled: false` for the generic harvester:
  * the first fans out over GeoNames ids, while the harvester splits only by QIDs;
  * the second is a cross-check, because countries' P36 claims already come from `core.countries`.
* They live in `places/wikidata/` with `domain: geography`, because the spec schema's domain enum has no `places`
  (request below).

## Live run (2026-10-16 00:14 UTC, throwaway database `reva_acities_live_7439c635`, objects under the scratchpad)

Every request went through `run_atlas_ingest`: budget reservation, then metered requests, then settlement
(policy budgets-1.3.5, after ATLASCORE merged my request). The dev database `reva` was never touched.

| source | status | requests / bytes (metered) | atlas tables (rows) |
|---|---|---|---|
| atlas.geonames_cities (`--option altnames=1`) | completed | 3 / 208,359,899 (reserved 2, 1 topped up) | admin1 (11,823), cities15000 (346,060), cities15000_by_country (1,698), cities15000_names (22,781) |
| atlas.un_wup | completed | 4 / 70,705,740 (reserved 5) | wup2025_urban_rural (290,657), wup2025_degurba (808,914), wup2025_cities (813,374), wup2025_cities_area (2,339,154), wup2025_capitals (1,459) |

Raw sha256 values:

* cities15000.zip: `d5cc2af7…`
* admin1CodesASCII.txt: `1da92a63…`
* alternateNamesV2.zip: `8f3e7b96…` (208 MB)
* WUP national definitions: `6f7f9fdb…`
* WUP DEGURBA level 1: `851cb5c2…`
* WUP cities: `e9c9b41e…`
* WUP F20: `cf2f934b…`

Disk: 300 MB of objects (raw 283 MB, of which 208 MB is alternate names; Parquet the rest) and a 21 MB database.
Peak RSS while building `wup2025_cities_area` (2.3 M rows) was about 3 GB.

### Coverage

* **GeoNames cities15000:** 34,163 places in 244 country codes. Every code maps, including XK → XKX, so 244 of
  the 251 crosswalk entities are covered.
  * No cities15000 place: ATA, BVT, HMD, IOT, TKL and UMI (uninhabited, or no place above 15,000 and no capital
    in the file), and CHI. GeoNames lists Guernsey (GG) and Jersey (JE) separately; the crosswalk's combined CHI
    is not a GeoNames country.
  * Feature codes: PPL 18,318, PPLA2 6,869, PPLA3 2,827, PPLA 2,420, PPLX 2,371, PPLA4 974, PPLC 241, PPLL 71,
    PPLA5 18, PPLH 13, PPLS 11, PPLQ 9, PPLG 9, PPLW 5, PPLF 4, STLMT 2, PPLR 1.
  * Omitted cells: 29,668 empty elevations, 58 DEM "no data" values, 3 populations of 0 (unknown).
* **GeoNames admin-1:** 3,865 divisions.
* **fr/en alternate names:** 22,847 found and 22,781 kept (66 over the bound of 5). 8,341 places have at least
  one fr name and 12,794 at least one en name.
* **WUP 2025, national definitions and DEGURBA level 1:** 237 countries/areas. Every crosswalk entity is covered
  except ALA, ATA, ATF, BVT, CCK, CHI, CXR, HMD, IOT, NFK, PCN, SGS, SJM and UMI (uninhabited or included with
  another country). 84–85 UN aggregates are kept unmapped.
* **WUP cities:** 16,828 city codes in 194 countries, 1975–2050. There are 12,138 cities ≥ 50k in 2025 and 205
  capital city codes. No city ≥ 50,000 exists in 57 small entities, so they have no WUP city row: ABW, AIA, ALA,
  AND, ASM, ATA, ATF, ATG, BES, BLM, BMU, BVT, CCK, CHI, COK, CXR, CYM, DMA, FLK, FRO, FSM, GGY, GRD, GRL, GUM,
  HMD, IMN, IOT, KNA, LCA, LIE, MAF, MHL, MNP, MSR, NFK, NIU, NRU, PCN, PLW, SGS, SHN, SJM, SMR, SPM, SXM, SYC,
  TCA, TKL, TON, TUV, UMI, VAT, VCT, VGB, VIR, WLF. Their capitals are in `wup2025_capitals`, which covers all
  237 locations (258 rows).
* **WUP capitals:**
  * Several "National capital…" rows: Malaysia (Kuala Lumpur, Putrajaya) and Sri Lanka (one settlement, two roles).
  * Capitals without a DEGURBA settlement or population: Plymouth (Montserrat), Alofi (Niue), Ngerulmud (Palau)
    and others.

### Gaps and alternatives

| gap | why | alternative |
|---|---|---|
| WUP cities 1950–1974; the "≥ 300k agglomerations 1950–2035" list | WUP 2025 cities start in 1975; WUP 2018 F12/F22 are superseded `.xls` workbooks | a reviewed `.xls` reader (xlrd) would let `WUP2018-F12-Cities_Over_300K.xls` be added as a separate, superseded-flagged table |
| GeoNames population dates | GeoNames states none | WUP city populations (dated, mid-year) or Wikidata P1082 with P585 (KG) |
| GeoNames place ≠ agglomeration | GeoNames counts municipalities; Wikidata/WUP use other boundaries | compare per source, never blend |
| EH, IL, PS have no PPLC in GeoNames | GeoNames coding | Wikidata P36 (cross-check), WUP F20 capital types |
| DEGURBA level 2 (suburbs, villages…) | not needed for country concepts | `WUP2025-DB-DEGURBA-Level2…csv.gz` (14 MB) if wanted |

### KG link (live, same throwaway database, 2026-10-16 18:5x UTC)

* **WDQS was unusable for us.** Every request got `429 Retry-After: 1000` (machine-wide cool-down shared with
  the other agents). The 38,028-id P1566 fan-out (`--via wdqs`, about 96 requests of 400 ids) therefore did not
  run; it stays the method for a quiet window. The CirrusSearch/REST search API was saturated too
  (`429 You are making too many requests`).
* **Run `--via api --max-ids 0 --client-mode linked_data`:**
  * 301 Special:EntityData requests, 41.8 MB, 556 snapshots reused from the shared archive;
  * metered under `wikidata.wbapi` (reserved 40, consumed 301, refused 0; policy budgets-1.3.6).
* **What it read and loaded:**
  * The 251 countries' snapshots (`country_qids.json`) gave 306 distinct P36 objects.
  * Each was loaded as a full snapshot: 7,581 `wikidata.wbapi` statements; WDHARVEST's loader rejected 1
    (`invalid_time`) in one batch.
  * Mapping through their own P1566 claims:
    * 255 of the 34,163 places are mapped, including 218 of the 241 PPLC places;
    * 29 of the 3,865 admin-1 divisions are mapped (capital cities that are also divisions);
    * 1 geonameid is ambiguous and not used: 1816670 Beijing, on Q956 and Q7334692.
* **GeoNames statements loaded** (`geonames.cities`): 747 in total, 0 rejected.
  * P17: 255; P1376: 218; P1082: 253; P131: 21.
  * Reloading inserts nothing (integration test).
* **Chain check** in the database:
  * Q597 has the fr label "Lisbonne" and the en label "Lisbon".
  * Q597 —P1376 (`geonames.cities`, PPLC, mapped via Wikidata's P1566 = 2267057)→ Q45. Wikidata's own
    `wikidata.wbapi` P1376 → Q45 sits next to it.
* **Remaining mapping** (33,908 places and 3,836 divisions) needs the WDQS fan-out or a bounded
  `--max-ids` search run, once Wikidata accepts our traffic again. The GeoNames statements of newly mapped
  places load automatically on the next atlas ingest or `wikidata` run, and are idempotent.

### Capitals: GeoNames PPLC vs Wikidata P36 (current claims), 2026-10-16

| status | countries |
|---|---:|
| agree (one PPLC = the GeoNames ID of the one current P36 capital) | 209 |
| agree_partial_multiple (Wikidata lists several current capitals; one matches the PPLC) | 9 |
| disagree (IDs differ) | 19 |
| geonames_only (PPLC, no current P36 on an ISO-coded item) | 4 |
| wikidata_only (P36, no PPLC in GeoNames) | 2 |

* **Several current capitals in Wikidata** (both sides listed, none chosen):
  * BEN: Porto-Novo preferred, Cotonou;
  * BOL: Sucre, La Paz;
  * LKA: Sri Jayawardenepura Kotte, Colombo;
  * MSR: Plymouth, Brades;
  * MYS: Kuala Lumpur preferred, Putrajaya;
  * SGS: King Edward Point preferred, Grytviken;
  * SWZ: Mbabane, Lobamba;
  * YEM: Sanaa, Aden;
  * ZAF: Pretoria, Cape Town, Bloemfontein.
* **wikidata_only:**
  * ISR: Jerusalem (Q1218, P1566 281184). GeoNames codes it PPLA, not PPLC.
  * IOT: Diego Garcia.
* **geonames_only:** BES (Kralendijk), HKG, MAC, SJM (Longyearbyen).
* **"disagree" is an ID disagreement, not necessarily another city.** Each case is listed with both sides:
  * ALA: Mariehamn 3041732 vs Q48329 → 3041733;
  * ARM: Yerevan 616052 vs Q1953 → 616051;
  * AZE: Baku 587084 vs Q9248 → 587081;
  * CCK: West Island 7304591 vs Q48675 → 1547351;
  * CHE: Bern 2661552 vs Q70 → 7285212;
  * ESP: Madrid 3117735 vs Q2807 → 6359304;
  * GNQ: Ciudad de la Paz 12226438 vs Q1140136 → 2306898;
  * GTM: Guatemala City 3598132 vs Q1555 → 3595529;
  * HTI: Port-au-Prince 3718426 vs Q34261 → 3718427;
  * KGZ: Bishkek 1528675 vs Q9361 → 1528334;
  * KIR: Tarawa 2110257 vs Q131233 (no P1566);
  * NRU: Yaren 7626461 vs Q31026 → 2110418;
  * MCO: Monaco 2993458 vs Q235 → 2993457;
  * MLI: Bamako 2460596 vs Q3703 → 2460594;
  * MLT: Valletta 2562305 vs Q23800 → 8334638;
  * PRK: Pyongyang 1871859 vs Q18808 → 1871856;
  * SGP: Singapore 1880252 vs Q334 → 1880251;
  * TJK: Dushanbe 1221874 vs Q9365 → 1221873;
  * VAT: Vatican City 6691831 vs Q237 → 3164670.
* **Reading the disagreements.** In most cases Wikidata's P1566 points to another GeoNames record of the same
  place, judging from the items' Wikidata labels, which match the PPLC names: Q1140136 is "Ciudad de la Paz",
  Q31026 "Yaren District", Q131233 "South Tarawa". In Equatorial Guinea both sides now name Ciudad de la Paz
  (the P36 claim starts 2026-01-03), but through different GeoNames ids. Kiribati's capital item has no
  GeoNames ID at all. Nothing is merged or corrected; the PPLC-based P1376 statements are loaded only for
  places that Wikidata links to that same geonameid.


### Per country (GeoNames cities15000 live bytes; WUP 2025)

"Places >15k" counts places with GeoNames population > 15,000, excluding PPLX sections and historical places
("incl. PPLX" counts every feature code). "Largest place" uses GeoNames' own figure. "PPLC" is GeoNames'
capital code; several are listed with "/" and "—" means none. "National capital (WUP)" is shown only when WUP
F20 has exactly one "National capital…" row; territories with a "Territorial capital" show "—". The urban
share is WUP 2025, national definitions, 2025.

| ISO3 | places >15k (excl. sections) | incl. PPLX | largest place (GeoNames pop.) | PPLC (GeoNames) | national capital (WUP 2025, mid-2025) | urban % 2025 (WUP, nat. def.) |
|---|---:|---:|---|---|---|---:|
| ABW | 4 | 4 | Oranjestad (29,998) | Oranjestad | — | 61.7 |
| AFG | 54 | 55 | Kabul (4,434,550) | Kabul | Kābul (Kabul) (5,636k) | 25.9 |
| AGO | 132 | 133 | Luanda (2,776,168) | Luanda | Luanda (11,370k) | 71.5 |
| AIA | 0 | 0 | The Valley (2,035) | The Valley | — | 100.0 |
| ALA | 0 | 0 | Mariehamn (10,682) | Mariehamn | — | — |
| ALB | 24 | 25 | Tirana (418,495) | Tirana | Tiranë (Tirana) (833k) | 58.9 |
| AND | 2 | 2 | Andorra la Vella (20,430) | Andorra la Vella | Andorra la Vella (29k) | 88.9 |
| ARE | 30 | 63 | Dubai (3,790,000) | Abu Dhabi | Abū Ẓabī (Abu Dhabi) (1,016k) | 86.0 |
| ARG | 315 | 329 | Buenos Aires (2,891,082) | Buenos Aires | Buenos Aires (14,018k) | 92.4 |
| ARM | 19 | 29 | Yerevan (1,144,700) | Yerevan | Yerevan (1,089k) | 65.9 |
| ASM | 0 | 0 | Pago Pago (11,500) | Pago Pago | — | 80.5 |
| ATF | 0 | 0 | Port-aux-Français (45) | Port-aux-Français | — | — |
| ATG | 1 | 1 | Saint John’s (51,737) | Saint John’s | Saint John's (32k) | 24.3 |
| AUS | 89 | 313 | Sydney (5,638,830) | Canberra | Canberra (49k) | 87.7 |
| AUT | 29 | 66 | Vienna (1,691,468) | Vienna | Wien (Vienna) (2,031k) | 69.7 |
| AZE | 65 | 65 | Baku (2,351,300) | Baku | Bakı (Baku) (1,978k) | 58.6 |
| BDI | 14 | 14 | Bujumbura (769,317) | Gitega | Gitega (198k) | 25.7 |
| BEL | 223 | 223 | Brussels (1,019,022) | Brussels | Bruxelles (Brussels) (1,406k) | 87.7 |
| BEN | 66 | 66 | Cotonou (679,012) | Porto-Novo | Porto-Novo (819k) | 53.4 |
| BES | 0 | 0 | Kralendijk (10,620) | Kralendijk | — | 81.1 |
| BFA | 74 | 74 | Ouagadougou (2,415,266) | Ouagadougou | Ouagadougou (3,206k) | 28.9 |
| BGD | 134 | 139 | Dhaka (10,356,500) | Dhaka | Dhaka (36,585k) | 33.2 |
| BGR | 55 | 56 | Sofia (1,152,556) | Sofia | Sofia (1,067k) | 74.2 |
| BHR | 8 | 8 | Al Muharraq (176,583) | Manama | Al-Manāmah (Manama) (1,539k) | 100.0 |
| BHS | 4 | 4 | Nassau (227,940) | Nassau | Nassau (250k) | 81.3 |
| BIH | 23 | 23 | Sarajevo (696,731) | Sarajevo | Sarajevo (254k) | 52.7 |
| BLM | 0 | 0 | Gustavia (5,988) | Gustavia | — | 100.0 |
| BLR | 56 | 56 | Minsk (1,742,124) | Minsk | Minsk (1,951k) | 79.5 |
| BLZ | 4 | 4 | Belize City (65,222) | Belmopan | Belmopan (28k) | 41.6 |
| BMU | 0 | 0 | Hamilton (902) | Hamilton | — | 100.0 |
| BOL | 39 | 39 | La Paz (2,004,652) | Sucre | Sucre (286k) | 71.6 |
| BRA | 2277 | 2377 | São Paulo (12,400,232) | Brasília | Brasília (1,521k) | 88.2 |
| BRB | 1 | 1 | Bridgetown (98,511) | Bridgetown | Bridgetown (121k) | 59.7 |
| BRN | 7 | 7 | Bandar Seri Begawan (64,409) | Bandar Seri Begawan | Bandar Seri Begawan (72k) | 75.0 |
| BTN | 4 | 4 | Thimphu (98,676) | Thimphu | Thimphu (65k) | 43.4 |
| BWA | 25 | 25 | Gaborone (246,325) | Gaborone | Gaborone (255k) | 68.0 |
| CAF | 33 | 33 | Bangui (812,407) | Bangui | Bangui (1,057k) | 44.6 |
| CAN | 295 | 511 | Toronto (2,794,356) | Ottawa | Ottawa (605k) | 82.9 |
| CCK | 0 | 0 | West Island (120) | West Island | — | — |
| CHE | 73 | 95 | Zürich (415,367) | Bern | Berne (Bern) (207k) | 85.7 |
| CHL | 139 | 147 | Santiago (4,837,295) | Santiago | Santiago (6,726k) | 89.2 |
| CHN | 2104 | 2122 | Shanghai (24,874,500) | Beijing | Beijing (17,013k) | 66.3 |
| CIV | 182 | 183 | Abidjan (6,321,017) | Yamoussoukro | Yamoussoukro (246k) | 54.5 |
| CMR | 87 | 87 | Douala (1,338,082) | Yaoundé | Yaoundé (5,106k) | 55.7 |
| COD | 114 | 114 | Kinshasa (16,000,000) | Kinshasa | Kinshasa (10,944k) | 45.1 |
| COG | 23 | 23 | Brazzaville (1,982,000) | Brazzaville | Brazzaville (3,656k) | 64.1 |
| COK | 0 | 0 | Avarua (13,373) | Avarua | Avarua (2k) | 76.9 |
| COL | 317 | 319 | Bogotá (7,674,366) | Bogotá | Bogotá (10,624k) | 78.8 |
| COM | 3 | 3 | Moroni (74,749) | Moroni | Moroni (171k) | 33.9 |
| CPV | 4 | 4 | Praia (137,868) | Praia | Praia (8k) | 77.6 |
| CRI | 36 | 41 | San José (335,007) | San José | San José (2,171k) | 79.7 |
| CUB | 123 | 140 | Havana (2,163,824) | Havana | La Habana (Havana) (1,594k) | 77.2 |
| CUW | 2 | 2 | Willemstad (125,000) | Willemstad | — | 91.5 |
| CXR | 0 | 0 | Flying Fish Cove (1,355) | Flying Fish Cove | — | — |
| CYM | 2 | 2 | George Town (29,370) | George Town | — | 100.0 |
| CYP | 15 | 15 | Nicosia (200,452) | Nicosia | Lefkosia (Nicosia) (333k) | 66.7 |
| CZE | 117 | 123 | Prague (1,165,581) | Prague | Praha (Prague) (1,198k) | 72.9 |
| DEU | 961 | 1140 | Berlin (3,426,354) | Berlin | Berlin (3,561k) | 82.1 |
| DJI | 7 | 7 | Djibouti (626,512) | Djibouti | Djibouti (738k) | 73.0 |
| DMA | 1 | 1 | Roseau (16,571) | Roseau | Roseau (16k) | 74.0 |
| DNK | 63 | 67 | Copenhagen (1,153,615) | Copenhagen | København (Copenhagen) (1,229k) | 88.8 |
| DOM | 45 | 50 | Santo Domingo (2,201,941) | Santo Domingo | Santo Domingo (4,600k) | 72.5 |
| DZA | 295 | 295 | Algiers (2,364,230) | Algiers | El Djazaïr (Algiers) (3,246k) | 75.8 |
| ECU | 71 | 71 | Quito (2,781,641) | Quito | Quito (2,535k) | 63.4 |
| EGY | 236 | 241 | Cairo (9,606,916) | Cairo | Al-Qahirah (Cairo) (25,566k) | 42.9 |
| ERI | 8 | 8 | Asmara (563,930) | Asmara | Asmara (228k) | 34.5 |
| ESH | 4 | 4 | Laayoune (196,331) | — | — | 86.9 |
| ESP | 545 | 736 | Madrid (3,255,944) | Madrid | Madrid (5,584k) | 80.5 |
| EST | 7 | 15 | Tallinn (394,024) | Tallinn | Tallinn (440k) | 70.7 |
| ETH | 233 | 233 | Addis Ababa (3,860,000) | Addis Ababa | Ādīs Ᾱbeba (Addis Ababa) (6,706k) | 24.1 |
| FIN | 78 | 102 | Helsinki (658,864) | Helsinki | Helsinki (Helsingfors) (1,014k) | 74.5 |
| FJI | 7 | 7 | Nasinu (92,043) | Suva | Suva (226k) | 59.5 |
| FLK | 0 | 0 | Stanley (2,213) | Stanley | — | 81.5 |
| FRA | 657 | 692 | Paris (2,138,551) | Paris | Paris (9,382k) | 78.8 |
| FRO | 0 | 0 | Tórshavn (13,200) | Tórshavn | — | 39.6 |
| FSM | 0 | 0 | Palikir (6,942) | Palikir | Palikir (13k) | 22.5 |
| GAB | 15 | 15 | Libreville (846,090) | Libreville | Libreville (921k) | 92.1 |
| GBR | 803 | 866 | London (8,961,989) | London | London (10,416k) | 83.3 |
| GEO | 17 | 17 | Tbilisi (1,049,498) | Tbilisi | Tbilisi (907k) | 61.2 |
| GGY | 1 | 1 | Saint Peter Port (16,488) | Saint Peter Port | — | 29.6 |
| GHA | 98 | 102 | Kumasi (2,544,530) | Accra | Accra (5,593k) | 58.9 |
| GIB | 1 | 1 | Gibraltar (26,544) | Gibraltar | — | 100.0 |
| GIN | 47 | 47 | Conakry (1,928,389) | Conakry | Conakry (12k) | 38.5 |
| GLP | 9 | 9 | Les Abymes (53,514) | Basse-Terre | — | 96.4 |
| GMB | 29 | 33 | Serekunda (340,000) | Banjul | Banjul (38k) | 65.0 |
| GNB | 9 | 15 | Bissau (439,704) | Bissau | Bissau (613k) | 46.7 |
| GNQ | 5 | 5 | Bata (173,046) | Ciudad de la Paz | Malabo (755k) | 71.6 |
| GRC | 117 | 118 | Athens (664,046) | Athens | Athína (Athens) (3,102k) | 79.2 |
| GRD | 0 | 0 | Saint George's (7,500) | Saint George's | Saint George's (10k) | 36.7 |
| GRL | 0 | 0 | Nuuk (14,798) | Nuuk | — | 88.6 |
| GTM | 102 | 102 | Guatemala City (994,938) | Guatemala City | Ciudad de Guatemala (Guatemala City) (2,838k) | 56.3 |
| GUF | 5 | 5 | Cayenne (61,550) | Cayenne | — | 86.6 |
| GUM | 4 | 5 | Dededo Village (44,943) | Hagåtña | — | 94.2 |
| GUY | 3 | 3 | Georgetown (235,017) | Georgetown | Georgetown (135k) | 26.5 |
| HKG | 136 | 141 | Hong Kong (7,396,076) | Hong Kong | — | 100.0 |
| HND | 65 | 65 | Tegucigalpa (850,848) | Tegucigalpa | Tegucigalpa (1,607k) | 59.3 |
| HRV | 24 | 25 | Zagreb (663,592) | Zagreb | Zagreb (623k) | 57.8 |
| HTI | 32 | 32 | Port-au-Prince (1,234,742) | Port-au-Prince | Port-au-Prince (1,868k) | 55.6 |
| HUN | 89 | 141 | Budapest (1,741,041) | Budapest | Budapest (1,609k) | 70.7 |
| IDN | 447 | 448 | Jakarta (8,540,121) | Jakarta | Jakarta (41,914k) | 59.4 |
| IMN | 1 | 1 | Douglas (26,218) | Douglas | — | 52.1 |
| IND | 3735 | 3780 | Mumbai (12,691,836) | New Delhi | New Delhi (30,222k) | 35.7 |
| IRL | 40 | 43 | Dublin (1,024,027) | Dublin | Dublin (1,143k) | 64.6 |
| IRN | 424 | 429 | Tehran (7,153,309) | Tehran | Tehrān (Tehran) (9,175k) | 77.4 |
| IRQ | 102 | 105 | Baghdad (7,216,000) | Baghdad | Baghdād (Baghdad) (6,391k) | 69.9 |
| ISL | 6 | 6 | Reykjavík (118,918) | Reykjavík | Reykjavík (218k) | 94.2 |
| ISR | 103 | 106 | Jerusalem (971,800) | — | Jerusalem (920k) | 91.6 |
| ITA | 598 | 661 | Rome (2,318,895) | Rome | Roma (Rome) (2,376k) | 69.7 |
| JAM | 12 | 13 | Kingston (937,700) | Kingston | Kingston (598k) | 59.4 |
| JEY | 1 | 1 | Saint Helier (28,000) | Saint Helier | — | 34.5 |
| JOR | 31 | 34 | Amman (1,275,857) | Amman | Ammān (Amman) (6,404k) | 93.2 |
| JPN | 1216 | 1300 | Tokyo (9,733,276) | Tokyo | Tōkyō (Tokyo) (33,413k) | 92.3 |
| KAZ | 83 | 84 | Almaty (1,977,011) | Astana | Astana (1,065k) | 62.4 |
| KEN | 120 | 122 | Nairobi (4,397,073) | Nairobi | Nairobi (6,134k) | 32.2 |
| KGZ | 32 | 32 | Bishkek (900,000) | Bishkek | Bishkek (1,343k) | 34.7 |
| KHM | 46 | 46 | Phnom Penh (1,573,544) | Phnom Penh | Phnum Pénh (Phnom Penh) (2,261k) | 41.1 |
| KIR | 1 | 1 | Tarawa (40,311) | Tarawa | South Tarawa (48k) | 63.4 |
| KNA | 0 | 0 | Basseterre (12,920) | Basseterre | Basseterre (6k) | 31.9 |
| KOR | 146 | 148 | Seoul (10,349,312) | Seoul | Seoul (22,490k) | 81.2 |
| KWT | 13 | 18 | Al Aḩmadī (637,411) | Kuwait City | Al Kuwayt (Kuwait City) (4,265k) | 100.0 |
| LAO | 18 | 18 | Vientiane (840,940) | Vientiane | Viangchan (Vientiane) (216k) | 40.3 |
| LBN | 17 | 18 | Beirut (1,916,100) | Beirut | Bayrūt (Beirut) (1,794k) | 91.0 |
| LBR | 16 | 16 | Monrovia (1,542,549) | Monrovia | Monrovia (1,879k) | 56.1 |
| LBY | 59 | 59 | Tripoli (1,302,947) | Tripoli | Ţarābulus (Tripoli) (1,240k) | 87.9 |
| LCA | 2 | 2 | Gros Islet (25,210) | Castries | Castries (40k) | 29.1 |
| LIE | 0 | 0 | Vaduz (5,197) | Vaduz | Vaduz (10k) | 14.6 |
| LKA | 60 | 60 | Colombo (648,034) | Colombo | — | 20.5 |
| LSO | 11 | 11 | Maseru (359,753) | Maseru | Maseru (287k) | 44.7 |
| LTU | 20 | 36 | Vilnius (542,366) | Vilnius | Vilnius (338k) | 69.1 |
| LUX | 3 | 3 | Luxembourg (76,684) | Luxembourg | Luxembourg-Ville (156k) | 94.9 |
| LVA | 14 | 15 | Riga (742,572) | Riga | Riga (550k) | 68.6 |
| MAC | 6 | 6 | Macau (649,335) | Macau | — | 100.0 |
| MAF | 0 | 0 | Marigot (5,700) | Marigot | — | 100.0 |
| MAR | 176 | 180 | Casablanca (3,665,954) | Rabat | Rabat (2,069k) | 63.1 |
| MCO | 1 | 2 | Monaco (32,965) | Monaco | Monaco (57k) | 100.0 |
| MDA | 22 | 22 | Chisinau (635,994) | Chisinau | Chișinău (422k) | 43.7 |
| MDG | 106 | 106 | Antananarivo (1,349,501) | Antananarivo | Antananarivo (3,916k) | 32.9 |
| MDV | 1 | 1 | Male (103,693) | Male | Malé (236k) | 42.5 |
| MEX | 625 | 643 | Mexico City (12,294,193) | Mexico City | Ciudad de México (Mexico City) (17,734k) | 80.0 |
| MHL | 2 | 2 | Majuro (25,400) | Majuro | Mājro (Majuro) (13k) | 79.1 |
| MKD | 36 | 38 | Skopje (474,889) | Skopje | Skopje (353k) | 62.9 |
| MLI | 60 | 60 | Bamako (4,227,569) | Bamako | Bamako (4,245k) | 32.2 |
| MLT | 6 | 6 | San Pawl il-Baħar (32,042) | Valletta | Valletta (373k) | 95.7 |
| MMR | 163 | 178 | Yangon (4,477,638) | Nay Pyi Taw | Nay Pyi Taw (54k) | 30.6 |
| MNE | 8 | 8 | Podgorica (236,852) | Podgorica | Podgorica (151k) | 65.6 |
| MNG | 24 | 24 | Ulan Bator (844,818) | Ulan Bator | Ulaanbaatar (1,597k) | 71.1 |
| MNP | 1 | 1 | Saipan (48,220) | Saipan | — | 92.2 |
| MOZ | 66 | 66 | Maputo (1,254,837) | Maputo | Maputo (3,166k) | 37.2 |
| MRT | 26 | 26 | Nouakchott (1,184,530) | Nouakchott | Nouakchott (1,562k) | 61.5 |
| MSR | 0 | 0 | Brades (1,000) | Plymouth | — | 9.0 |
| MTQ | 8 | 8 | Fort-de-France (89,995) | Fort-de-France | — | 96.8 |
| MUS | 14 | 14 | Port Louis (155,226) | Port Louis | Port Louis (549k) | 39.0 |
| MWI | 23 | 23 | Lilongwe (1,115,815) | Lilongwe | Lilongwe (961k) | 17.6 |
| MYS | 350 | 355 | Kuala Lumpur (1,453,975) | Kuala Lumpur | — | 77.4 |
| MYT | 5 | 6 | Mamoudzou (54,831) | Mamoudzou | — | 94.4 |
| NAM | 18 | 19 | Windhoek (386,219) | Windhoek | Windhoek (410k) | 50.9 |
| NCL | 3 | 3 | Nouméa (93,060) | Nouméa | — | 68.2 |
| NER | 42 | 42 | Niamey (1,323,691) | Niamey | Niamey (1,401k) | 18.4 |
| NFK | 0 | 0 | Kingston (880) | Kingston | — | — |
| NGA | 269 | 270 | Lagos (15,388,000) | Abuja | Abuja (1,374k) | 63.8 |
| NIC | 37 | 37 | Managua (973,087) | Managua | Managua (1,316k) | 59.4 |
| NIU | 0 | 0 | Alofi (624) | Alofi | — | 37.2 |
| NLD | 235 | 253 | Rotterdam (868,135) | Amsterdam | Amsterdam (1,236k) | 95.9 |
| NOR | 41 | 42 | Oslo (1,082,575) | Oslo | Oslo (875k) | 83.6 |
| NPL | 46 | 46 | Kathmandu (1,442,271) | Kathmandu | Kathmandu (3,232k) | 67.0 |
| NRU | 0 | 0 | Yaren (1,100) | Yaren | Yaren (7k) | 100.0 |
| NZL | 47 | 58 | Auckland (1,547,200) | Wellington | Wellington (149k) | 84.0 |
| OMN | 27 | 28 | Muscat (797,000) | Muscat | Masqąt (Muscat) (781k) | 79.5 |
| PAK | 363 | 372 | Lahore (13,004,135) | Islamabad | Islāmābād (4,860k) | 39.5 |
| PAN | 20 | 27 | Panama City (408,168) | Panama City | Ciudad de Panamá (Panama City) (1,527k) | 66.2 |
| PCN | 0 | 0 | Adamstown (46) | Adamstown | — | — |
| PER | 136 | 140 | Lima (7,737,002) | Lima | Lima (10,580k) | 85.6 |
| PHL | 519 | 530 | Quezon City (3,084,270) | Manila | Manila (24,735k) | 55.8 |
| PLW | 0 | 0 |  | Ngerulmud | — | 79.3 |
| PNG | 13 | 13 | Port Moresby (283,733) | Port Moresby | Port Moresby (413k) | 15.8 |
| POL | 344 | 363 | Warsaw (1,702,139) | Warsaw | Warszawa (Warsaw) (1,883k) | 60.1 |
| PRI | 21 | 21 | San Juan (418,140) | San Juan | — | 94.3 |
| PRK | 97 | 97 | Pyongyang (3,222,000) | Pyongyang | P'yŏngyang (Pyongyang) (2,902k) | 64.2 |
| PRT | 174 | 179 | Lisbon (517,802) | Lisbon | Lisboa (Lisbon) (1,914k) | 61.5 |
| PRY | 30 | 30 | Asunción (1,482,200) | Asunción | Asunción (1,315k) | 70.4 |
| PSE | 43 | 45 | Gaza (410,000) | — | Al-Quds (East Jerusalem) (1,106k) | 87.4 |
| PYF | 3 | 3 | Faaa (29,851) | Papeete | — | 61.8 |
| QAT | 10 | 26 | Doha (344,939) | Doha | Ad-Dawhah (Doha) (2,194k) | 99.4 |
| REU | 14 | 14 | Saint-Denis (154,765) | Saint-Denis | — | 97.3 |
| ROU | 126 | 134 | Bucharest (1,877,155) | Bucharest | București (Bucharest) (1,910k) | 52.2 |
| RUS | 980 | 1108 | Moscow (10,381,222) | Moscow | Moskva (Moscow) (14,525k) | 75.3 |
| RWA | 45 | 45 | Kigali (1,132,686) | Kigali | Kigali (1,484k) | 30.9 |
| SAU | 96 | 98 | Jeddah (4,697,000) | Riyadh | Ar-Riyāḑ (Riyadh) (6,916k) | 84.6 |
| SDN | 76 | 76 | Khartoum (1,974,647) | Khartoum | Al-Kharṭūm (Khartoum) (6,809k) | 34.9 |
| SEN | 66 | 71 | Dakar (2,646,503) | Dakar | Dakar (3,852k) | 55.8 |
| SGP | 31 | 65 | Singapore (5,638,700) | Singapore | Singapore (4,964k) | 100.0 |
| SGS | 0 | 0 | Grytviken (2) | Grytviken | — | — |
| SHN | 0 | 0 | Jamestown (637) | Jamestown | — | 37.2 |
| SJM | 0 | 0 | Longyearbyen (2,368) | Longyearbyen | — | — |
| SLB | 3 | 7 | Honiara (56,298) | Honiara | Honiara (131k) | 31.3 |
| SLE | 13 | 14 | Freetown (802,639) | Freetown | Freetown (1,936k) | 45.9 |
| SLV | 35 | 35 | San Salvador (525,990) | San Salvador | San Salvador (1,615k) | 75.5 |
| SMR | 0 | 0 | San Marino (4,500) | San Marino | San Marino (8k) | 97.2 |
| SOM | 42 | 42 | Mogadishu (2,587,183) | Mogadishu | Muqdisho (Mogadishu) (4,399k) | 55.1 |
| SPM | 0 | 0 | Saint-Pierre (6,200) | Saint-Pierre | — | 90.2 |
| SRB | 51 | 51 | Belgrade (1,273,651) | Belgrade | Beograd (Belgrade) (1,121k) | 62.6 |
| SSD | 16 | 16 | Juba (450,000) | Juba | Juba (391k) | 21.7 |
| STP | 1 | 1 | São Tomé (53,300) | São Tomé | São Tomé (148k) | 69.2 |
| SUR | 2 | 12 | Paramaribo (223,757) | Paramaribo | Paramaribo (253k) | 65.8 |
| SVK | 59 | 61 | Bratislava (423,737) | Bratislava | Bratislava (289k) | 53.2 |
| SVN | 9 | 9 | Ljubljana (272,220) | Ljubljana | Ljubljana (247k) | 56.0 |
| SWE | 99 | 111 | Stockholm (1,515,017) | Stockholm | Stockholm (1,500k) | 89.1 |
| SWZ | 2 | 2 | Manzini (110,537) | Mbabane | Mbabane (41k) | 26.7 |
| SXM | 0 | 0 | Philipsburg (1,400) | Philipsburg | — | 100.0 |
| SYC | 1 | 1 | Victoria (22,881) | Victoria | Victoria (44k) | 45.5 |
| SYR | 89 | 89 | Aleppo (2,098,210) | Damascus | Dimashq (Damascus) (4,288k) | 72.1 |
| TCA | 1 | 1 | Providenciales (23,769) | Cockburn Town | — | 94.4 |
| TCD | 46 | 46 | N'Djamena (1,359,526) | N'Djamena | N'Djaména (1,935k) | 27.6 |
| TGO | 20 | 20 | Lomé (2,188,376) | Lomé | Lomé (2,415k) | 44.4 |
| THA | 297 | 330 | Bangkok (5,104,476) | Bangkok | Krung Thep Maha Nakhon (Bangkok) (18,180k) | 62.8 |
| TJK | 31 | 31 | Dushanbe (679,400) | Dushanbe | Dushanbe (1,441k) | 26.2 |
| TKM | 36 | 36 | Ashgabat (1,030,063) | Ashgabat | Aşgabat (Ashgabat) (1,218k) | 47.1 |
| TLS | 10 | 10 | Dili (150,000) | Dili | Dili (345k) | 28.8 |
| TON | 1 | 1 | Nuku‘alofa (22,400) | Nuku‘alofa | Nukuʻalofa (48k) | 21.2 |
| TTO | 14 | 14 | Chaguanas (67,433) | Port of Spain | Port of Spain (478k) | 54.0 |
| TUN | 126 | 130 | Tunis (693,210) | Tunis | Tunis (2,473k) | 70.7 |
| TUR | 417 | 432 | Istanbul (15,701,602) | Ankara | Ankara (3,612k) | 89.5 |
| TUV | 0 | 0 | Funafuti (6,320) | Funafuti | Funafuti (7k) | 65.4 |
| TWN | 67 | 67 | Taipei (7,871,900) | Taipei | — | — |
| TZA | 227 | 228 | Dar es Salaam (5,383,728) | Dodoma | Dodoma (269k) | 36.9 |
| UGA | 141 | 141 | Kampala (1,680,600) | Kampala | Kampala (4,881k) | 31.9 |
| UKR | 271 | 401 | Kyiv (2,952,301) | Kyiv | Kyiv (Kiev) (2,615k) | 69.6 |
| URY | 31 | 31 | Montevideo (1,270,737) | Montevideo | Montevideo (1,240k) | 95.7 |
| USA | 3122 | 3408 | New York City (8,804,190) | Washington | Washington, D.C. (3,270k) | 80.2 |
| UZB | 109 | 112 | Tashkent (1,978,028) | Tashkent | Toshkent (Tashkent) (3,936k) | 51.0 |
| VAT | 0 | 0 | Vatican City (829) | Vatican City | Vatican City (2,376k) | 100.0 |
| VCT | 2 | 2 | Kingstown (24,518) | Kingstown | Kingstown (18k) | 48.0 |
| VEN | 214 | 216 | Caracas (3,000,000) | Caracas | Caracas (2,577k) | 89.3 |
| VGB | 0 | 0 | Road Town (8,449) | Road Town | — | 57.4 |
| VIR | 2 | 2 | Saint Croix (50,601) | Charlotte Amalie | — | 95.4 |
| VNM | 308 | 313 | Ho Chi Minh City (14,002,598) | Hanoi | Hà Nội (Hanoi) (5,068k) | 38.8 |
| VUT | 1 | 1 | Port-Vila (35,901) | Port-Vila | Port Vila (58k) | 22.3 |
| WLF | 0 | 0 | Mata-Utu (1,200) | Mata-Utu | — | 0.0 |
| WSM | 1 | 1 | Apia (40,407) | Apia | Apia (31k) | 17.5 |
| XKX | 21 | 21 | Pristina (550,000) | Pristina | — | — |
| YEM | 25 | 25 | Sanaa (1,937,451) | Sanaa | Şan'ā' (Sana'a) (4,019k) | 37.0 |
| ZAF | 297 | 307 | Johannesburg (9,418,183) | Pretoria | Pretoria (1,836k) | 63.8 |
| ZMB | 77 | 77 | Lusaka (2,212,301) | Lusaka | Lusaka (3,511k) | 45.9 |
| ZWE | 29 | 31 | Harare (1,542,813) | Harare | Harare (2,117k) | 40.5 |



## Licences

| source | licence | statement |
|---|---|---|
| GeoNames cities15000 / admin1CodesASCII / alternateNamesV2 | CC BY 4.0 | readme.txt: "This work is licensed under a Creative Commons Attribution 4.0 License"; attribution "GeoNames (www.geonames.org)" on every table |
| UN WUP 2025 | CC BY 3.0 IGO | every WUP 2025 workbook, `info` sheet: "Copyright © 2025 by United Nations, made available under a Creative Commons license CC BY 3.0 IGO" (checked by the connector); citation "United Nations, DESA, Population Division (2025). World Urbanization Prospects: The 2025 Revision, Online Edition." |
| Wikidata P1566/P36 | CC0 | community-edited, never "verified" (KG labels it) |

## Requests to other owners

1. **ATLASCORE:** done. The budget request was merged (`geonames.cities` 6/day, `un.wup` 10/day; budgets-1.3.5)
   and `tests/invariants/test_invariant_30_budgets.py` passes. My modules create no HTTP client.
2. **WDHARVEST:**
   * add `places` to the domain enum of `registry/kg/queries/_schema.json`; I will then move the two specs to
     `places/`;
   * a `split: {by: literal_values}` option would let the generic harvester run the GeoNames-id fan-out itself;
   * when WDQS accepts traffic again, run (or let me run) the full P1566 fan-out:
     `atlas_cities wikidata --via wdqs`, about 96 requests.
   * In the loaded capital snapshots, one `wikidata.wbapi` statement was rejected by `load_batch`
     (`invalid_time`) in WDHARVEST's converter.
3. **Lead:** the seed hook registers both atlas sources automatically. During my first bootstrap on
   2026-10-15, the in-flight `0011_kg_fixes` migration was not yet in the schema contract; that was fixed since
   (the contract verified on 2026-10-16).
