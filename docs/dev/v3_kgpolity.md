# V3 KGPOLITY — who ruled the land of each present-day country, when (Cliopatria + territorial overlay)

Agent KGPOLITY, 2026-10-15/16. Goal: make the history of polities over territory explicit and connectable, so that
chains such as "Brazil ← Portuguese Empire", "Algeria ← French Africa ← Regency of Algiers ← Ottoman Empire",
"India ← British Raj ← British Empire ← Mughal Empire" are **sourced statements** in the knowledge graph, each hop
citing its raw artifacts and geometries.

Live ingest: one run into a throwaway database (`reva_kgpolity_live_*`, dropped afterwards, object store deleted)
through the atlas platform (`reva.atlas.ingest.run_atlas_ingest`, live, budget-metered). The dev database `reva` was
never touched. All tests are offline (captured, trimmed fixture).

## 1. Sources and decisions

| Source | Access | Licence (checked 2026-10-15) | Decision |
|---|---|---|---|
| **Cliopatria** (Seshat Global History Databank), release **v0.2.1** (git `5f433377139a`) | `raw.githubusercontent.com/Seshat-Global-History-Databank/cliopatria/v0.2.1/cliopatria.geojson.zip` (46.0 MB zip; one member `cliopatria_polities_only_v021.geojson`, 172.9 MB, 13,797 features); Zenodo record 13363121 mirrors the GitHub releases (Zenodo timed out from this container) | **CC BY 4.0** (`LICENSE.md` of the tag; fetched and checked every run — any other text blocks the run). Paper: Bennett et al. (2025), *Scientific Data*, doi:10.1038/s41597-025-04516-9 | **ingested** into the KG (`source_key cliopatria.github`) |
| **Natural Earth 1:10m Admin-0 map units** 5.1.1 | `naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_0_map_units.zip` (5.0 MB) | public domain | fetched and archived **in the same run** (A-GEO's NE 10m raws lived only in A-GEO's dropped throwaway DB); same entity rules as A-GEO (`assign_entity`: ISO_A3 > ADM0_A3 > ISO_A3_EH > KOS→XKX) |
| **Seshat Global History Databank** variables (population, territory, government) | seshat-db.com (CSV/JSON/API), Equinox-2020 spreadsheet, Polaris snapshot on GitHub | seshat-db.com terms: Public Data **CC BY-SA 4.0**, but "Account required to download: … you must create a free account and consent to the Current Terms" (and "If you do not agree, do not … download data, or use the API"). Equinox-2020: **CC BY-NC-SA** (non-commercial). `build_polaris_dataset` repo: MIT licence for the *software*; the data it pulls comes from the account-gated API | **not ingested.** REVA cannot accept a user agreement on the owner's behalf; Equinox is NC. No `registry/sources/seshat.yaml` was added (a registered source without a budget would break invariant 30). Maintainer decision needed: create an account and accept the terms, then a connector can read CC BY-SA public data (share-alike applies to REVA's derived tables). Cliopatria's own `SeshatID` column is loaded (`cliopatria:seshat_polity_id`, 1,115 runs, 613 ids) so a later Seshat ingest joins directly. |

Cliopatria provider caveat (README, quoted on every overlay statement): "these maps reflect only one version of the
territory held by past polities. Border uncertainties, as well as differing opinions on the names, territorial
changes, and durations of polities, are common challenges facing historians."

## 2. What was built

| File | Purpose |
|---|---|
| `src/reva/adapters/connectors/atlas_cliopatria.py` | atlas source `atlas.cliopatria` (`atlas_source()` + `async atlas_ingest(run)`; fetch only via `run.get`, `run.archive`), parser, KG records, `load_kg`, coverage |
| `src/reva/kg/overlay.py` | pure spatial overlay: NE map units → territories, tiled index, streaming GeoJSON reader, ellipsoidal areas, runs (`spans`) |
| `registry/sources/cliopatria.yaml` | source registration (rights, release, caveat) |
| `registry/kg/predicates_polity.yaml` | the five dataset predicates (registered by the connector through `register_properties`, like `cow:code_denotes`) |
| `registry/atlas/budget_requests/kgpolity.yaml` | `cliopatria.github`: 6 requests/day — **merged by ATLASCORE (budgets-1.3.5)** |
| `registry/links/link_types.yaml` | new derived link type `shared_polity_territory` (status `awaiting_source`, see §8) |
| `pyproject.toml`, `uv.lock` | `shapely==2.1.2` (pinned; 2.2.0 was < 2 weeks old). No pyproj: areas use A-GEO's `atlas_geodesy` |
| `tests/unit/test_kg_polity.py` (12), `tests/integration/test_kg_polity_ingest.py` (2 + 1 xfail, `db`) | offline tests; fixture `tests/fixtures/source/atlas.cliopatria/` (406 kB) |

The fixture directory is named after the connector (`atlas.cliopatria`), because the platform's `FixtureHttp`
replays `tests/fixtures/source/<connector name>/`. It holds the real `LICENSE.md`, **299 real Cliopatria rows**
(Portuguese Empire, Ottoman Empire, Regency of Algiers, French Algeria/Africa, Algeria, Mughal Empire, British Raj,
Republic of India, Empire of Brazil, Sumerian City-States, Roman Empire −31..22) with **simplified polygons**
(0.05°), and **12 real Natural Earth countries** rewritten with pyshp and simplified (0.02°); the manifest says so
and gives the original sha256s.

Run: `uv run python -m reva.atlas.ingest atlas.cliopatria [--fixture]` (against the configured DB — use a throwaway
one). One full run = 3 requests (licence, Cliopatria zip, NE map units), ~51 MB.

## 3. Method

**Rows → KG.** Entities `cliopatria:polity:<slug>` (names in parentheses — the provider's groupings such as
"(Portuguese Empire)", "(French Third Republic)" — get the prefix `grp_`; colliding slugs get `-<sha6(name)>`), kind
`polity` (1,587) or `polity_relation` (50, Type RELATION). Statements (`source_key cliopatria.github`):

| predicate | what | count (live) |
|---|---|---:|
| `cliopatria:reported_area` | per row: the provider's Area (km², EPSG:6933) over FromYear..ToYear; qualifiers row ordinal, geometry sha256, SeshatID, Wikipedia phrase, Wikidata id, REVA's ellipsoidal area for cross-check | 13,797 |
| `cliopatria:denotes` | polity → the Wikidata QID **the provider records** on its rows, per run of rows ("provider_declared; not checked by REVA") | 2,065 |
| `cliopatria:member_of` | component → relation, from `MemberOf` and `Components` merged (the two fields are not mirror images: 327 vs 571 pairs) | 692 |
| `cliopatria:seshat_polity_id` | the provider's SeshatID per run of rows | 1,115 |
| `geo:covered_territory_of` | **derived** overlay, polity → present-day country (below) | 8,410 |

**Years.** The README says "negative for BCE"; the file also writes `0` between `-1` and `1` (e.g. Roman Empire
`-14..0` then `1..5`). KG times are astronomical (KGCORE): provider `y<0` → `y+1`, `0` → `0` (1 BCE, flagged
`provider_year_zero`), `y>0` → `y`; precision year. The provider integers stay in every statement
(`provider_years`). Range −3400 … 2024.

**Overlay** (`reva.kg.overlay`, method id `reva.kg.overlay/1`):
* Natural Earth map units are grouped into crosswalk entities with A-GEO's rules; units outside the crosswalk stay
  separate territories `naturalearth:<ADM0_A3>` (Somaliland, N. Cyprus, Siachen, Bir Tawil, …), never merged into a
  claimant. 263 territories: 250 with a crosswalk key (249 of the 250 crosswalk entities — `CHI` has no NE unit,
  Guernsey and Jersey are separate — plus `TWN`, now a key) and 13 NE-only units.
* Each Cliopatria row polygon (repaired with `make_valid` when invalid: 13 rows) is intersected with the
  territories in lon/lat (GEOS, shapely 2.1.2; both sources draw straight lon/lat edges). Areas are WGS 84
  ellipsoidal ring areas via the authalic latitude (A-GEO's `atlas_geodesy`, also used for country areas).
  Share = overlap / country area.
* Speed/memory: territories are cut into ≤ 400-vertex, ≤ 4° pieces (8,976 pieces; a piece fully inside a polity
  counts by area, no intersection); identical polygons are computed once (2,477 cache hits); the GeoJSON is streamed
  feature by feature (`raw_decode` on a sliding buffer, ≤ 32 MB per feature). Peak RSS ~300 MB; the whole overlay
  takes 11–20 min on the shared, loaded 4-vCPU host. Tiling was checked against untiled intersections (first 500
  rows, 1,750 overlaps: identical, max share difference 1.5e-9); piece areas sum to every country's area (0 mismatches).
* **Runs**: for each polity, its rows ordered by year; a run is a maximal sequence of consecutive, time-contiguous
  rows each covering ≥ **1 %** of the country (smaller overlaps are mostly the mismatch between two independently
  drawn coastlines/rivers: 29,043 row-overlaps below 1 % were not stored, only counted). One
  `geo:covered_territory_of` statement per run: start/end = first FromYear / last ToYear; qualifiers
  `share_of_country_area {max,min,first,last}` (decimal strings), `overlap_km2_max`, `country_area_km2`, `rows[]`
  (row, provider years, share, overlap km², **Cliopatria geometry sha256**), `country_geometry_sha256` (normalised
  WKB of the NE country), `ne_units`, `ne_version`, `ne_raw_sha256`, `cliopatria_raw_sha256`, `min_share`,
  `derivation: computed_spatial_overlay`, `label: derived`, `caveat`; references name both URLs. The statement's
  `raw_sha256` is the Cliopatria zip; the load's raw artifacts are the licence, the zip and the NE layer.
* **Object node**: the country's Wikidata QID from WDHARVEST's `registry/entities/country_qids.json` (250 mapped,
  delivered during this work) — else unambiguous same-origin P298 statements in the KG — else the REVA node
  `iso3:<KEY>` (LINKS' convention). Statement ids do not depend on the node, so a load with QIDs after one without is
  a new revision of the same statements.
* A full run retracts (`removed_at_source`) current statements of this source/origin that the new build lacks.

## 4. Live run

LIVE3_PLACEHOLDER

## 5. Polities by era

Distinct Cliopatria entities with at least one row overlapping the era (provider years). 1,637 entities in all,
13,797 rows; every entity carries a provider Wikidata id on at least one row — rows without one: 5
("(Allegiance of East Franks to Carolingian Empire)" 911–921; Timurid Empire 1507–1528, four rows), kept without a
`denotes` statement (nothing guessed). No invalid QID strings. So no `candidates` file was needed.

| era (provider years) | entities with ≥ 1 row |
|---|---:|
| before 1000 BCE | 29 |
| 1000 BCE – 1 BCE | 186 |
| 1 – 500 | 154 |
| 501 – 1000 | 338 |
| 1001 – 1500 | 550 |
| 1501 – 1800 | 342 |
| 1801 – 1900 | 319 |
| 1901 – 2024 | 361 |

## 6. Example chains (live statements)

Every line is one `geo:covered_territory_of` statement (key `cliopatria.github:<id>`), raw artifact
`e10a4e429fca…` (Cliopatria v0.2.1 zip) + NE map units `45bebe2aaf8b…`; the provider's QID for the polity in
brackets; shares are of the **present-day** country's area.

**Brazil** (NE geometry `3337c1c51c44…`)
* Portuguese Empire [Q200464] 1540–1581, share 1.6–4.2 % (coastal captaincies; Cliopatria draws no Portuguese
  polygon in Brazil before 1540) — `overlay:portuguese_empire:BRA:1540:1581`, first row geometry `69c80330116a…`
* Spanish Empire [Q80702] 1582–1639, 4.2–9.7 % (Iberian Union, drawn under the Spanish Empire) — `overlay:spanish_empire:BRA:1582:1639`
* Portuguese Empire [Q200464] 1640–1821, 6.3 → 91.3 % (34 rows) — `overlay:portuguese_empire:BRA:1640:1821`, first row geometry `e08a1be2b682…`
* Empire of Brazil [Q217230] 1822–1889, 88.1–97.5 % — `overlay:empire_of_brazil:BRA:1822:1889`
* Brazilian Republic [Q155] 1890–1931 … Republic of Brazil 2024, 97.5–99.5 %

**Algeria** (NE `b38d102c0bbd…`)
* Ottoman Empire [Q12560] 1556–1705, 9.0 % (24 rows: the coastal Tell; Cliopatria does not draw the Sahara) — `overlay:ottoman_empire:DZA:1556:1705`
* Regency of Algiers [Q3045696] 1706–1845, 7.0–9.1 % — `overlay:regency_of_algiers:DZA:1706:1845`
* French Algeria [Q218272] 1840–1847, 1.3–9.1 % — `overlay:french_algeria:DZA:1840:1847`
* French Africa [Q19951607] 1848–1957, 9.1 → 99.9 % (37 rows) — `overlay:french_africa:DZA:1848:1957` (Cliopatria has no
  "French colonial empire" polity; France's colonies are "French Africa" and the groupings "(Second French Empire)",
  "(French Third Republic)", … which also have runs over Algeria)
* People's Democratic Republic of Algeria [Q262] 1963–2024, 99.5 %

**India** (NE `13923f59d33e…`)
* Mughal Empire [Q33296] 1529–1546 (2.4–21.8 %) and 1556–1856 (3.6 → 84.4 %, 46 rows) — `overlay:mughal_empire:IND:1556:1856`
* Maratha Empire [Q83618] 1721–1845, 2.6–40.3 %
* British Empire [Q8680] 1762–1858, 8.1 → 93.2 % (40 rows) — `overlay:british_empire:IND:1762:1858`
* British Raj [Q129286] 1859–1946, 93.4–99.3 % — `overlay:british_raj:IND:1859:1946`
* Republic of India [Q668] 1947–2024, 88.0–99.2 %

**Tunisia**: Carthage [Q2429397] 650–145 BCE (2.3–52.6 %) → Roman Empire [Q12544] 31 BCE–394 (52.8–56.0 %) → … →
Ottoman Empire 1572–1712 → Beylik of Tunis 1706–1884 → French Africa 1870–1957. **Egypt**: Ptolemaic Kingdom
[Q2320005] 331–28 BCE → Roman Empire 27 BCE–394 → … → Ottoman Empire 1519–1810 → Muhammad Ali dynasty 1811–1865.
**Mexico**: Spanish Empire 1521–1821 (10.8 → 98.7 %).

Read with care: about half of Tunisia and Egypt and ~90 % of Algeria are desert the pre-modern polygons do not cover,
so a "9 %" Ottoman share of Algeria means "the drawn polygon covers 9 % of today's Algeria", not "weak rule".

## 7. Coverage for every present-day territory

218 of the 250 crosswalk-keyed territories have at least one polity run (≥ 1 %). First covered year (crosswalk
territories): before 1 CE for 80, 1–1499 for 53, 1500–1799 for 56, from 1800 for 29. No run: 32 small islands and
dependencies Cliopatria does not draw (ASM, ATA, BLM, BVT, CCK, COK, CXR, CYM, GGY, GIB, HMD, IOT, JEY, KIR, KNA,
MDV, MHL, MNP, MSR, NFK, NIU, NRU, PCN, PYF, SHN, TKL, TON, TUV, UMI, VGB, WLF, WSM) and 4 NE-only units (Bajo
Nuevo, Wake Island as NE names it, Scarborough Shoal, Serranilla). `CHI` (Channel Islands) has no NE unit
(Guernsey/Jersey are separate units). "years covered" counts provider years in which at least one polity covers
≥ 1 % of the territory.

<details><summary>Per territory (263 rows)</summary>

| key | country | area km² (NE 1:10m) | polities (runs ≥ 1 %) | first year | last year | years covered |
|---|---|---:|---:|---|---|---:|
| ABW | Aruba | 170 | 11 | 1529 | 2024 | 496 |
| AFG | Afghanistan | 642,181 | 74 | 615 BCE | 2024 | 2640 |
| AGO | Angola | 1,244,652 | 8 | 1440 | 2024 | 546 |
| AIA | Anguilla | 87 | 27 | 1626 | 2024 | 399 |
| ALA | Åland Islands | 942 | 10 | 1250 | 2024 | 774 |
| ALB | Albania | 28,336 | 52 | 615 BCE | 2024 | 2636 |
| AND | Andorra | 452 | 66 | 50 BCE | 2024 | 2075 |
| ARE | United Arab Emirates | 71,085 | 29 | 2250 BCE | 2024 | 2157 |
| ARG | Argentina | 2,782,896 | 12 | 1497 | 2024 | 528 |
| ARM | Armenia | 29,588 | 58 | 800 BCE | 2024 | 2825 |
| ASM | American Samoa | 180 | 0 |  |  | 0 |
| ATA | Antarctica | 12,358,218 | 0 |  |  | 0 |
| ATF | French Southern Territories | 7,280 | 5 | 1895 | 2024 | 130 |
| ATG | Antigua and Barbuda | 452 | 6 | 1632 | 2024 | 393 |
| AUS | Australia | 7,691,174 | 3 | 1828 | 2024 | 197 |
| AUT | Austria | 83,993 | 71 | 14 BCE | 2024 | 2039 |
| AZE | Azerbaijan | 86,250 | 64 | 800 BCE | 2024 | 2825 |
| BDI | Burundi | 27,041 | 7 | 1895 | 2024 | 130 |
| BEL | Belgium | 30,505 | 78 | 50 BCE | 2024 | 2075 |
| BEN | Benin | 116,113 | 9 | 1752 | 2024 | 224 |
| BES | Bonaire, Sint Eustatius and Saba | 295 | 11 | 1529 | 2024 | 496 |
| BFA | Burkina Faso | 272,769 | 8 | 1834 | 2024 | 191 |
| BGD | Bangladesh | 136,904 | 39 | 450 BCE | 2024 | 2101 |
| BGR | Bulgaria | 112,760 | 52 | 550 BCE | 2024 | 2575 |
| BHR | Bahrain | 688 | 27 | 126 BCE | 2024 | 2148 |
| BHS | Bahamas | 12,604 | 6 | 1648 | 2024 | 363 |
| BIH | Bosnia and Herzegovina | 51,827 | 64 | 247 BCE | 2024 | 2108 |
| BLM | Saint Barthélemy | 24 | 0 |  |  | 0 |
| BLR | Belarus | 207,499 | 38 | 880 | 2024 | 1145 |
| BLZ | Belize | 22,299 | 20 | 247 BCE | 2024 | 2272 |
| BMU | Bermuda | 62 | 7 | 1609 | 2024 | 416 |
| BOL | Bolivia (Plurinational State of) | 1,086,808 | 13 | 1497 | 2024 | 528 |
| BRA | Brazil | 8,472,666 | 21 | 1540 | 2024 | 485 |
| BRB | Barbados | 444 | 6 | 1629 | 2024 | 396 |
| BRN | Brunei Darussalam | 5,717 | 6 | 1363 | 2024 | 662 |
| BTN | Bhutan | 40,421 | 21 | 318 BCE | 2024 | 1690 |
| BVT | Bouvet Island | 55 | 0 |  |  | 0 |
| BWA | Botswana | 579,029 | 7 | 1885 | 2024 | 140 |
| CAF | Central African Republic | 617,984 | 9 | 1890 | 2024 | 135 |
| CAN | Canada | 9,945,630 | 5 | 1713 | 2024 | 312 |
| CCK | Cocos (Keeling) Islands | 13 | 0 |  |  | 0 |
| CHE | Switzerland | 41,436 | 83 | 50 BCE | 2024 | 2075 |
| CHL | Chile | 736,595 | 13 | 1497 | 2024 | 528 |
| CHN | China | 9,375,219 | 141 | 1600 BCE | 2024 | 3625 |
| CIV | Côte d’Ivoire | 320,677 | 8 | 1236 | 2024 | 657 |
| CMR | Cameroon | 464,319 | 12 | 1482 | 2024 | 543 |
| COD | Democratic Republic of the Congo | 2,325,240 | 9 | 1440 | 2024 | 546 |
| COG | Congo | 344,889 | 11 | 1890 | 2024 | 135 |
| COK | Cook Islands | 191 | 0 |  |  | 0 |
| COL | Colombia | 1,135,118 | 11 | 1534 | 2024 | 491 |
| COM | Comoros | 1,672 | 16 | 1677 | 2024 | 348 |
| CPV | Cabo Verde | 3,927 | 10 | 1463 | 2024 | 562 |
| CRI | Costa Rica | 51,151 | 9 | 1526 | 2024 | 499 |
| CUB | Cuba | 109,929 | 13 | 1512 | 2024 | 513 |
| CUW | Curaçao | 463 | 11 | 1529 | 2024 | 496 |
| CXR | Christmas Island | 99 | 0 |  |  | 0 |
| CYM | Cayman Islands | 309 | 0 |  |  | 0 |
| CYP | Cyprus | 5,395 | 27 | 1000 BCE | 2024 | 3025 |
| CZE | Czechia | 78,759 | 57 | 407 | 2024 | 1386 |
| DEU | Germany | 357,674 | 138 | 50 BCE | 2024 | 2075 |
| DJI | Djibouti | 21,848 | 17 | 1285 | 2024 | 627 |
| DMA | Dominica | 730 | 6 | 1718 | 2024 | 307 |
| DNK | Denmark | 42,696 | 26 | 154 | 2024 | 1871 |
| DOM | Dominican Republic | 48,440 | 18 | 1497 | 2024 | 528 |
| DZA | Algeria | 2,308,858 | 42 | 450 BCE | 2024 | 2466 |
| ECU | Ecuador | 255,014 | 16 | 1375 | 2024 | 650 |
| EGY | Egypt | 1,001,058 | 60 | 3000 BCE | 2024 | 5025 |
| ERI | Eritrea | 122,538 | 16 | 50 BCE | 2024 | 1750 |
| ESH | Western Sahara | 90,496 | 6 | 1056 | 2024 | 218 |
| ESP | Spain | 506,885 | 79 | 500 BCE | 2024 | 2525 |
| EST | Estonia | 45,819 | 27 | 1056 | 2024 | 969 |
| ETH | Ethiopia | 1,127,375 | 12 | 50 BCE | 2024 | 2075 |
| FIN | Finland | 333,059 | 14 | 1056 | 2024 | 960 |
| FJI | Fiji | 18,930 | 4 | 1871 | 2024 | 154 |
| FLK | Falkland Islands (Malvinas) | 11,597 | 7 | 1769 | 2024 | 256 |
| FRA | France | 547,846 | 140 | 550 BCE | 2024 | 2575 |
| FRO | Faroe Islands | 1,328 | 14 | 800 | 2024 | 1225 |
| FSM | Micronesia (Federated States of) | 634 | 6 | 1877 | 2024 | 148 |
| GAB | Gabon | 259,968 | 13 | 1842 | 2024 | 183 |
| GBR | United Kingdom of Great Britain and Northern Ireland | 243,783 | 53 | 43 | 2024 | 1953 |
| GEO | Georgia | 69,571 | 62 | 650 BCE | 2024 | 2675 |
| GGY | Guernsey | 74 | 0 |  |  | 0 |
| GHA | Ghana | 238,669 | 17 | 1642 | 2024 | 383 |
| GIB | Gibraltar | 4 | 0 |  |  | 0 |
| GIN | Guinea | 244,302 | 9 | 1236 | 2024 | 527 |
| GLP | Guadeloupe | 1,657 | 28 | 1636 | 2024 | 389 |
| GMB | Gambia | 10,501 | 14 | 1236 | 2024 | 564 |
| GNB | Guinea-Bissau | 32,829 | 18 | 1415 | 2024 | 610 |
| GNQ | Equatorial Guinea | 26,672 | 21 | 1497 | 2024 | 528 |
| GRC | Greece | 131,353 | 81 | 1600 BCE | 2024 | 3625 |
| GRD | Grenada | 347 | 6 | 1653 | 2024 | 372 |
| GRL | Greenland | 2,154,380 | 4 | 1744 | 2024 | 281 |
| GTM | Guatemala | 108,811 | 15 | 247 BCE | 2024 | 1782 |
| GUF | French Guiana | 83,184 | 29 | 1670 | 2024 | 355 |
| GUM | Guam | 564 | 6 | 1670 | 2024 | 355 |
| GUY | Guyana | 211,214 | 13 | 1619 | 2024 | 406 |
| HKG | China, Hong Kong Special Administrative Region | 1,036 | 36 | 210 BCE | 2024 | 2235 |
| HMD | Heard Island and McDonald Islands | 395 | 0 |  |  | 0 |
| HND | Honduras | 112,236 | 11 | 247 BCE | 2024 | 1596 |
| HRV | Croatia | 55,023 | 75 | 247 BCE | 2024 | 2211 |
| HTI | Haiti | 26,892 | 15 | 1502 | 2024 | 523 |
| HUN | Hungary | 93,201 | 64 | 43 | 2024 | 1982 |
| IDN | Indonesia | 1,879,826 | 38 | 534 | 2024 | 1491 |
| IMN | Isle of Man | 575 | 17 | 587 | 2024 | 1242 |
| IND | India | 3,150,745 | 110 | 2300 BCE | 2024 | 3165 |
| IOT | British Indian Ocean Territory | 49 | 0 |  |  | 0 |
| IRL | Ireland | 69,445 | 17 | 850 | 2024 | 1175 |
| IRN | Iran (Islamic Republic of) | 1,622,509 | 85 | 2700 BCE | 2024 | 4725 |
| IRQ | Iraq | 437,367 | 72 | 3400 BCE | 2024 | 5425 |
| ISL | Iceland | 102,390 | 15 | 870 | 2024 | 1155 |
| ISR | Israel | 22,101 | 65 | 1800 BCE | 2024 | 3825 |
| ITA | Italy | 301,185 | 116 | 750 BCE | 2024 | 2775 |
| JAM | Jamaica | 11,033 | 13 | 1512 | 2024 | 513 |
| JEY | Jersey | 119 | 0 |  |  | 0 |
| JOR | Jordan | 88,863 | 50 | 1700 BCE | 2024 | 3575 |
| JPN | Japan | 373,507 | 12 | 540 | 2024 | 1485 |
| KAZ | Kazakhstan | 2,714,265 | 71 | 650 BCE | 2024 | 2529 |
| KEN | Kenya | 585,702 | 6 | 1814 | 2024 | 177 |
| KGZ | Kyrgyzstan | 198,990 | 65 | 550 BCE | 2024 | 2575 |
| KHM | Cambodia | 181,060 | 18 | 51 | 2024 | 1974 |
| KIR | Kiribati | 950 | 0 |  |  | 0 |
| KNA | Saint Kitts and Nevis | 264 | 0 |  |  | 0 |
| KOR | Republic of Korea | 98,544 | 30 | 197 BCE | 2024 | 2222 |
| KWT | Kuwait | 17,474 | 69 | 1700 BCE | 2024 | 3525 |
| LAO | Lao People's Democratic Republic | 228,114 | 29 | 555 | 2024 | 1470 |
| LBN | Lebanon | 10,004 | 55 | 2300 BCE | 2024 | 3925 |
| LBR | Liberia | 95,298 | 7 | 1822 | 2024 | 203 |
| LBY | Libya | 1,623,759 | 48 | 615 BCE | 2024 | 2640 |
| LCA | Saint Lucia | 605 | 6 | 1645 | 2024 | 358 |
| LIE | Liechtenstein | 137 | 43 | 14 BCE | 2024 | 2025 |
| LKA | Sri Lanka | 66,291 | 23 | 291 BCE | 2024 | 2316 |
| LSO | Lesotho | 30,106 | 8 | 1822 | 2024 | 203 |
| LTU | Lithuania | 64,939 | 39 | 990 | 2024 | 1035 |
| LUX | Luxembourg | 2,608 | 79 | 50 BCE | 2024 | 2075 |
| LVA | Latvia | 64,576 | 30 | 1085 | 2024 | 940 |
| MAC | China, Macao Special Administrative Region | 30 | 37 | 210 BCE | 2024 | 2235 |
| MAF | Saint Martin (French Part) | 68 | 11 | 1626 | 2024 | 399 |
| MAR | Morocco | 591,744 | 35 | 450 BCE | 2024 | 2317 |
| MCO | Monaco | 19 | 1 | 1943 | 1943 | 1 |
| MDA | Republic of Moldova | 33,206 | 45 | 650 BCE | 2024 | 1998 |
| MDG | Madagascar | 592,982 | 9 | 1677 | 2024 | 277 |
| MDV | Maldives | 109 | 0 |  |  | 0 |
| MEX | Mexico | 1,957,845 | 21 | 247 BCE | 2024 | 2265 |
| MHL | Marshall Islands | 165 | 0 |  |  | 0 |
| MKD | North Macedonia | 25,385 | 48 | 550 BCE | 2024 | 2481 |
| MLI | Mali | 1,252,723 | 15 | 793 | 2024 | 1101 |
| MLT | Malta | 326 | 31 | 900 BCE | 2024 | 2609 |
| MMR | Myanmar | 663,159 | 27 | 84 | 2024 | 1941 |
| MNE | Montenegro | 13,728 | 48 | 256 BCE | 2024 | 2090 |
| MNG | Mongolia | 1,564,662 | 30 | 208 BCE | 2024 | 1914 |
| MNP | Northern Mariana Islands | 579 | 0 |  |  | 0 |
| MOZ | Mozambique | 788,447 | 10 | 1734 | 2024 | 291 |
| MRT | Mauritania | 1,036,389 | 13 | 793 | 2024 | 967 |
| MSR | Montserrat | 100 | 0 |  |  | 0 |
| MTQ | Martinique | 1,098 | 25 | 1636 | 2024 | 389 |
| MUS | Mauritius | 2,014 | 17 | 1640 | 2024 | 366 |
| MWI | Malawi | 119,398 | 11 | 1890 | 2024 | 135 |
| MYS | Malaysia | 327,885 | 30 | 692 | 2024 | 1333 |
| MYT | Mayotte | 395 | 16 | 1677 | 2024 | 348 |
| NAM | Namibia | 822,714 | 5 | 1885 | 2024 | 140 |
| NCL | New Caledonia | 18,841 | 7 | 1856 | 2024 | 169 |
| NER | Niger | 1,181,302 | 8 | 1487 | 2024 | 538 |
| NFK | Norfolk Island | 41 | 0 |  |  | 0 |
| NGA | Nigeria | 907,499 | 9 | 1482 | 2024 | 543 |
| NIC | Nicaragua | 128,685 | 12 | 1526 | 2024 | 499 |
| NIU | Niue | 221 | 0 |  |  | 0 |
| NLD | Netherlands (Kingdom of the) | 37,102 | 78 | 50 BCE | 2024 | 2075 |
| NOR | Norway | 319,936 | 19 | 184 | 2024 | 1841 |
| NPL | Nepal | 147,106 | 33 | 600 BCE | 2024 | 2107 |
| NRU | Naoero | 29 | 0 |  |  | 0 |
| NZL | New Zealand | 268,495 | 3 | 1830 | 2024 | 195 |
| OMN | Oman | 311,213 | 31 | 126 BCE | 2024 | 2080 |
| PAK | Pakistan | 872,939 | 87 | 2500 BCE | 2024 | 3385 |
| PAN | Panama | 74,530 | 17 | 1512 | 2024 | 513 |
| PCN | Pitcairn | 43 | 0 |  |  | 0 |
| PER | Peru | 1,289,866 | 10 | 1344 | 2024 | 681 |
| PHL | Philippines | 293,291 | 12 | 1375 | 2024 | 650 |
| PLW | Palau | 492 | 6 | 1885 | 2024 | 140 |
| PNG | Papua New Guinea | 465,147 | 7 | 1885 | 2024 | 140 |
| POL | Poland | 313,428 | 89 | 43 | 2024 | 1467 |
| PRI | Puerto Rico | 9,006 | 9 | 1512 | 2024 | 513 |
| PRK | Democratic People's Republic of Korea | 122,379 | 47 | 197 BCE | 2024 | 2222 |
| PRT | Portugal | 91,390 | 46 | 144 BCE | 2024 | 2169 |
| PRY | Paraguay | 399,901 | 16 | 1540 | 2024 | 485 |
| PSE | State of Palestine | 6,074 | 53 | 1800 BCE | 2024 | 3825 |
| PYF | French Polynesia | 3,336 | 0 |  |  | 0 |
| QAT | Qatar | 11,150 | 26 | 126 BCE | 2024 | 1916 |
| REU | Réunion | 2,582 | 25 | 1670 | 2024 | 355 |
| ROU | Romania | 236,377 | 66 | 650 BCE | 2024 | 2645 |
| RUS | Russian Federation | 16,980,192 | 63 | 650 BCE | 2024 | 2159 |
| RWA | Rwanda | 25,305 | 7 | 1895 | 2024 | 130 |
| SAU | Saudi Arabia | 1,921,725 | 35 | 188 BCE | 2024 | 2150 |
| SDN | Sudan | 1,855,615 | 15 | 1500 BCE | 2024 | 3525 |
| SEN | Senegal | 196,225 | 31 | 1040 | 2024 | 944 |
| SGP | Singapore | 510 | 14 | 692 | 2024 | 1333 |
| SGS | South Georgia and the South Sandwich Islands | 3,922 | 5 | 1775 | 2024 | 250 |
| SHN | Saint Helena | 367 | 0 |  |  | 0 |
| SJM | Svalbard and Jan Mayen Islands | 62,082 | 1 | 1926 | 2024 | 95 |
| SLB | Solomon Islands | 27,199 | 5 | 1890 | 2024 | 135 |
| SLE | Sierra Leone | 71,612 | 14 | 1788 | 2024 | 237 |
| SLV | El Salvador | 20,539 | 10 | 247 BCE | 2024 | 1596 |
| SMR | San Marino | 60 | 38 | 291 BCE | 2024 | 2302 |
| SOM | Somalia | 471,815 | 7 | 1840 | 2024 | 185 |
| SPM | Saint Pierre and Miquelon | 243 | 22 | 1691 | 2024 | 323 |
| SRB | Serbia | 77,629 | 73 | 337 BCE | 2024 | 2111 |
| SSD | South Sudan | 626,859 | 8 | 1507 | 2024 | 518 |
| STP | Sao Tome and Principe | 1,037 | 10 | 1497 | 2024 | 528 |
| SUR | Suriname | 145,124 | 26 | 1653 | 2024 | 372 |
| SVK | Slovakia | 48,458 | 56 | 337 | 2024 | 1367 |
| SVN | Slovenia | 20,327 | 71 | 164 BCE | 2024 | 2189 |
| SWE | Sweden | 446,174 | 22 | 30 | 2024 | 1747 |
| SWZ | Eswatini | 17,112 | 15 | 1820 | 2024 | 205 |
| SXM | Sint Maarten (Dutch part) | 23 | 11 | 1629 | 2024 | 396 |
| SYC | Seychelles | 436 | 18 | 1757 | 2024 | 268 |
| SYR | Syrian Arab Republic | 185,939 | 81 | 2500 BCE | 2024 | 4225 |
| TCA | Turks and Caicos Islands | 445 | 6 | 1683 | 2024 | 342 |
| TCD | Chad | 1,266,283 | 15 | 1072 | 2024 | 856 |
| TGO | Togo | 56,863 | 13 | 1752 | 2024 | 224 |
| THA | Thailand | 514,454 | 33 | 51 | 2024 | 1974 |
| TJK | Tajikistan | 142,239 | 66 | 550 BCE | 2024 | 2575 |
| TKL | Tokelau | 4 | 0 |  |  | 0 |
| TKM | Turkmenistan | 470,850 | 74 | 615 BCE | 2024 | 2640 |
| TLS | Timor-Leste | 15,082 | 22 | 1363 | 2024 | 611 |
| TON | Tonga | 603 | 0 |  |  | 0 |
| TTO | Trinidad and Tobago | 5,123 | 16 | 1534 | 2024 | 491 |
| TUN | Tunisia | 156,613 | 40 | 800 BCE | 2024 | 2825 |
| TUR | Türkiye | 780,080 | 114 | 2300 BCE | 2024 | 3725 |
| TUV | Tuvalu | 23 | 0 |  |  | 0 |
| TWN | Taiwan | 36,198 | 8 | 1626 | 2024 | 399 |
| TZA | United Republic of Tanzania | 941,506 | 8 | 1700 | 2024 | 325 |
| UGA | Uganda | 241,854 | 7 | 1877 | 2024 | 148 |
| UKR | Ukraine | 571,999 | 81 | 650 BCE | 2024 | 2320 |
| UMI | United States Minor Outlying Islands | 28 | 0 |  |  | 0 |
| URY | Uruguay | 177,342 | 13 | 1683 | 2024 | 342 |
| USA | United States of America | 9,464,218 | 14 | 1629 | 2024 | 369 |
| UZB | Uzbekistan | 447,831 | 65 | 650 BCE | 2024 | 2675 |
| VAT | Holy See | 0 | 23 | 750 BCE | 2024 | 2537 |
| VCT | Saint Vincent and the Grenadines | 368 | 6 | 1721 | 2024 | 304 |
| VEN | Venezuela (Bolivarian Republic of) | 912,684 | 7 | 1529 | 2024 | 496 |
| VGB | British Virgin Islands | 145 | 0 |  |  | 0 |
| VIR | United States Virgin Islands | 355 | 10 | 1626 | 2024 | 399 |
| VNM | Viet Nam | 328,892 | 52 | 256 BCE | 2024 | 2281 |
| VUT | Vanuatu | 12,297 | 2 | 1890 | 2024 | 135 |
| WLF | Wallis and Futuna Islands | 139 | 0 |  |  | 0 |
| WSM | Samoa | 2,780 | 0 |  |  | 0 |
| XKX | Kosovo | 10,913 | 38 | 323 BCE | 2024 | 2206 |
| YEM | Yemen | 453,075 | 32 | 800 BCE | 2024 | 2825 |
| ZAF | South Africa | 1,219,825 | 12 | 1653 | 2024 | 372 |
| ZMB | Zambia | 751,914 | 4 | 1895 | 2024 | 130 |
| ZWE | Zimbabwe | 389,338 | 11 | 1890 | 2024 | 135 |
| naturalearth:BJN | Bajo Nuevo Bank | 0 | 0 |  |  | 0 |
| naturalearth:BRT | Bir Tawil | 2,019 | 14 | 1000 BCE | 2024 | 2722 |
| naturalearth:CNM | United Nations Buffer Zone in Cyprus | 341 | 27 | 1000 BCE | 2024 | 3025 |
| naturalearth:CYN | Turkish Republic of Northern Cyprus | 3,319 | 27 | 1000 BCE | 2024 | 3025 |
| naturalearth:ESB | Dhekelia Cantonment | 127 | 26 | 1000 BCE | 2024 | 3025 |
| naturalearth:KAS | Siachen Glacier | 2,088 | 30 | 626 | 2024 | 1399 |
| naturalearth:PGA | Wake Island | 12 | 0 |  |  | 0 |
| naturalearth:SCR | Scarborough Shoal | 0 | 0 |  |  | 0 |
| naturalearth:SER | Serranilla Bank | 0 | 0 |  |  | 0 |
| naturalearth:SOL | Somaliland | 167,407 | 10 | 1285 | 2024 | 735 |
| naturalearth:SPI | Southern Patagonian Ice Field | 1,409 | 3 | 1885 | 2024 | 140 |
| naturalearth:USG | Guantanamo Bay Naval Base | 65 | 12 | 1512 | 2024 | 513 |
| naturalearth:WSB | Akrotiri Sovereign Base Area | 99 | 23 | 1000 BCE | 2024 | 3025 |
</details>

## 8. Interconnection (LINKS) and paths

* `registry/links/link_types.yaml`: new derived link **`shared_polity_territory`** (country–country, undirected,
  method `kg_shared_subject` over `geo:covered_territory_of`, `via: polity`, interval semantics, no weight, honesty
  "connected via: present-day territories partly covered by {via} (computed overlay, approximate borders)"), status
  `awaiting_source` because `src/reva/links/derive.py` (LINKS) does not wire it yet. Schema and taxonomy valid;
  `uv run pytest tests/unit/links -q` → 56 passed.
* KGCORE's `/v1/kg/path` traverses the new statements already (they are entity-valued): the integration test finds
  Brazil → (covered_territory_of, Portuguese Empire, Cliopatria) → (denotes) → Q200464 in 2 hops.

## 9. Tests

* `uv run pytest tests/unit/test_kg_polity.py -q` → 12 passed (area vs closed form, holes, tiling invariance,
  shares, fast path, cache, invalid-geometry repair, runs incl. year 0 and gaps, streaming reader bounds and
  layout-independence, year convention, ids/collisions, predicates file, KGCORE normalisation of every record,
  statement ids independent of the country node, licence/release guards, no HTTP client in the modules, real
  fixture chains and provider QID).
* `uv run pytest tests/integration/test_kg_polity_ingest.py -q` (`db`) → 2 passed, 1 xfailed: fixture ingest through
  `run_atlas_ingest` (0 rejected; derived qualifiers; three raws on the load; BCE astronomical times; provider QID;
  KG path country → polity → Wikidata item); exact re-run inserts nothing; a build lacking one statement retracts
  it. The xfail documents the KGCORE BCE-date bug below.
* `tests/invariants/test_invariant_30_budgets.py`: no failure from KGPOLITY files (`cliopatria.github` is in
  budgets-1.3.5; the live-client scan is clean). It currently fails only on `grambank.cldf`, `glottolog.cldf`,
  `wals.cldf` (another agent's unmerged sources).

## 10. Caveats

* Derived ≠ sourced. `geo:covered_territory_of` is REVA's computation from two sourced geometries; it is labelled
  (`derivation`, `label`, `caveat`, method id) and cites both raws and both geometry digests. KGCORE's generic
  provenance still prints `verification: source_reported_not_verified` for it (request 3).
* Cliopatria borders are approximate and its years are sampling steps; rows can extend the provider's claim to
  steppes/deserts or omit them. Natural Earth's default view is de facto (e.g. Crimea with Russia, Western Sahara
  split). A share is of present-day area, says nothing about sovereignty, effective control, population or causes,
  and the 1 % threshold drops genuine small overlaps along with digitisation slivers.
* Provider-declared QIDs are not verified by REVA (e.g. "Brazilian Republic" → Q155, the present-day Brazil item).
* Groupings ("(Portuguese Empire)", "(French Third Republic)") are separate entities and have their own runs, so a
  country often shows both a polity and its grouping over the same years.

## 11. Requests to other owners

1. **KGCORE (bug)** — `src/reva/kg/query.py` selects `kg.statement.valid_lo/valid_hi` as SQL `date`; psycopg cannot
   load BC dates into `datetime.date` (`DataError: date too small (before year 1): '0203-01-01 BC'`), so
   `paths`/`neighbours`/`timeline` (and the `/v1/kg/*` routes) fail as soon as a BCE-dated statement is read — this
   also hits Wikidata BCE inceptions (Roman Empire, Carthage…). Seen in the live readback (path from Algeria) and
   pinned by `test_path_across_bce_dated_overlay` (xfail, `DataError`). Fix: select `valid_lo::text` /
   `valid_hi::text` (the module already has `_sort_key` for `'… BC'` strings) or register a psycopg text loader
   for `date` on these queries.
2. **LINKS** — (a) wire `shared_polity_territory` in `derive.py` (`kg_shared_subject` over
   `geo:covered_territory_of`, intersection of the two runs, bounded pairwise expansion: a large empire covers
   dozens of countries); (b) allow dataset predicates (`ns:name`) in `statement_edges` (schema pattern is `^P…`) so
   `geo:covered_territory_of` ("a couvert une partie du territoire actuel de" / inverse "territoire actuel couvert en
   partie par") and `cliopatria:denotes` become hops of `/v1/links/path`; labels are in
   `registry/kg/predicates_polity.yaml`.
3. **KGCORE** — let `_provenance` label statements carrying `qualifiers.derivation = computed_spatial_overlay` as
   `derived_computed` instead of `source_reported_not_verified`.
4. **WDHARVEST** — Cliopatria's 1,6xx provider QIDs (objects of `cliopatria:denotes`) are mostly not KG entities yet;
   a spec seeded from them (labels, P31, P571/P576, P1365/P1366) would join this layer to the Wikidata graph.
5. **Maintainer** — Seshat Databank variables need an account and acceptance of the Seshat user agreement (Public
   Data CC BY-SA 4.0); decide whether REVA does that.
6. **Lead** — `pyproject.toml`/`uv.lock` gained `shapely==2.1.2`; seed the atlas connector versions as planned
   (`register_atlas_connector_versions`) so `atlas.cliopatria` can run in the deployment.
