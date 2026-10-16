# V3 A-RESOURCES — natural resources: who produces and who holds what (coverage report)

Agent A-RESOURCES, 2026-10-15/16. Two atlas sources, eight atlas tables, one catalog (216 provider-declared concepts).
Tested offline (fixtures) and ingested once live through the single atlas entry point
(`reva.atlas.ingest.run_atlas_ingest`, budget-metered) into a throwaway database (`reva_ares_live_d926ce11`,
dropped afterwards with its object store). The dev database `reva` was never touched.

## 1. What was built

| atlas source (connector) | source key / licence | module | atlas tables | requests / bytes per full run |
|---|---|---|---|---:|
| `atlas.usgs_minerals` | `usgs.sciencebase` — U.S. public domain, data releases marked CC0 1.0 | `src/reva/adapters/connectors/atlas_usgs.py` | `usgs.mcs2026`, `usgs.myb2024`, `usgs.myb2019` | 9 / 11.0 MB |
| `atlas.eia_intl` | `eia.opendata_bulk` — U.S. public domain | `src/reva/adapters/connectors/atlas_eia.py` | `eia.intl_petroleum`, `eia.intl_gas`, `eia.intl_coal`, `eia.intl_electricity`, `eia.intl_energy` | 3 / 24.2 MB |

Registry: `registry/sources/usgs_nmic.yaml`, `registry/sources/eia_bulk.yaml`; budget request
`registry/atlas/budget_requests/a_resources.yaml` (usgs.sciencebase 20/day, eia.opendata_bulk 8/day; `python -m
reva.atlas.budget_merge` already plans both); geo aliases `registry/atlas/geo_aliases/a_resources.yaml` (schemes
`usgs_name`: 181 USGS country names → crosswalk, each with its reason; `eia`: ISO3 + `XKS` → `XKX`); catalog
`registry/atlas/catalog/resources.yaml` (domain `resources`, per-concept `theme`: minerals → `economy`, energy →
`energy`). Fixtures `tests/fixtures/source/atlas.usgs_minerals/` (0.55 MB) and `tests/fixtures/source/atlas.eia_intl/`
(0.14 MB). Tests `tests/unit/test_atlas_resources.py` (21) and `tests/integration/test_atlas_resources_ingest.py` (2,
fixture-mode end to end on a throwaway DB: archive → register → idempotent re-run → single-dataset run).

Both modules follow the atlas contract: `atlas_source()` + `async atlas_ingest(run)`, fetch only through `run.get`,
archive every raw response with `run.archive` before parsing, register with `run.register`. No HTTP client is built.

Run: `uv run python -m reva.atlas.ingest atlas.usgs_minerals` / `atlas.eia_intl` (`--dataset mcs2026` etc.;
`--fixture` offline).

### Sources and how they are fetched

* **USGS Mineral Commodity Summaries 2026** — ScienceBase item `69837e43b66b01367d7ec7c7` (doi:10.5066/P1WKQ63T),
  `MCS2026_Commodities_Data.csv`: world mine/refinery production (2024, 2025) and reserves by country for ~90
  non-fuel commodities, U.S. salient statistics 2021-2025 (production, imports, exports, consumption, prices, net import
  reliance), U.S. import sources 2021-24 (partner shares).
* **USGS Minerals Yearbook vol. III (Area Reports — International)** — the country-chapter production tables, compiled by
  USGS into machine-readable data releases: 2024 edition (item `6a8490171ba49b3133cd8055`, doi:10.5066/P1KEQASH,
  2020-2024, 165+ countries, published 2026-08-31) and 2019 edition table 1 (item `67febb3cd4be02350c2609bc`,
  doi:10.5066/P13HTKV3, 2015-2019). The per-country spreadsheets on usgs.gov are therefore not needed (the usgs.gov
  site also returned 403 to our fetches). No 2020-2023 editions were found on ScienceBase.
* The connector reads each ScienceBase item JSON, takes the CSV and FGDC-metadata URLs from the item's own file list
  (exact file names), archives all three, and fails closed if neither the item nor the metadata still says CC0 /
  public domain. Value codes are read from the archived metadata, not hard-coded.
* **EIA International Energy Data** — bulk `https://www.eia.gov/opendata/bulk/INTL.zip` (no key; the API v2 needs a
  registered key and is not used), checked against `manifest.txt` (access URL, `last_updated`), and EIA's notation
  from the archived footnotes page `international/content/info/footnotes.html`: `--` Not applicable, `NA` Not
  available, `W` Data withheld to avoid disclosure, `ie` Data are included elsewhere for this time period.

### Honesty rules as implemented

* Values verbatim: USGS numbers only lose their thousands separators (`56,000` → `56000`); EIA JSON numbers are kept
  as their literal text (`json.loads(parse_float=str)`), e.g. `81949.4481623602`. Units verbatim (`metric tons`,
  `thousand metric dry tons`, `million carats`, `thousand barrels per day`, `billion cubic meters`, `1000 metric tons`,
  `million short tons`, `billion kilowatthours`, `million kilowatts`, `quadrillion Btu` …); the unit is part of every
  USGS indicator code, so one indicator never mixes units.
* Withheld / missing never become zero: USGS `W` → value-less cell `flags.missing = confidential` (+ code and
  meaning); `NA` → `not_available`; `XX` → `not_applicable`; MYB `-999` ("not known or not available") →
  `not_available`; EIA `W` → `confidential`, `ie` → `not_reported` with "included elsewhere" (French overseas
  departments from 2011, folded into France by EIA); EIA `--`/`NA`/JSON null → no cell (counted).
* USGS's own `—` is defined as **zero** in the metadata, so it becomes `0` with `flags.provider_code = "—"` (the
  parse fails if the metadata stops saying so). Zeros reported by EIA are kept (1.26 M cells; EIA writes 0 for e.g.
  anthracite production in Aruba).
* Estimates and revisions verbatim: MCS `Notes` ("Estimated.", "Reported.", definitions) and `Other notes`; MYB 2024
  `Value Notes = Estimated`, sources, currentness date; MYB 2019 per-year footnotes ("Revised.", "Estimated.",
  "Preliminary.") → `flags.revised`, `flags.estimate_kind`. EIA: per-series `last_updated` in `flags.series_last_updated`
  (EIA revises series individually: 103 distinct stamps, 2016-11 … 2026-09).
* Qualitative entries (`Large`, `>2,000,000`, `330 - 390`) and coded ones (`s` small, `E` net exporter) go to
  `value_text`, never `value`.
* Identical-label rows (MCS Mica scrap-and-flake vs sheet blocks; MYB North Korea graphite) get `|dup=n` in file
  order and `flags.ambiguous_duplicate`; Ghana gold large-scale vs artisanal are told apart by their own note.
* MCS "Import Sources": the Country column is the *partner* (source of U.S. imports); `flags.reporter = United States`.
* EIA aggregates keep `flags.eia_members` (EIA's own member list, e.g. OECD = `ASM+AUS+AUT+…`).
* Not taken from EIA: the `TJ` and `MTOE` copies of series EIA also publishes in quadrillion Btu (33,208 series:
  EIA's unit conversions of the same quantity), GDP PPP (4701) and population (4702) inputs from other producers,
  3,975 monthly/quarterly series.

## 2. Coverage (live run)

| atlas table | rows | numeric | value-less (flagged) | value_text | indicators | countries (crosswalk) | unmapped geos | years | Parquet sha256 |
|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| `atlas.usgs.mcs2026` | 8,886 | 7,941 | 585 | 360 | 1,536 | 123 | 3 | 2021-2025 | `a2b2738e54b4…` |
| `atlas.usgs.myb2024` | 18,855 | 18,825 | 30 | 0 | 1,048 | 168 | 0 | 2020-2024 | `622aaf03f929…` |
| `atlas.usgs.myb2019` | 24,850 | 24,750 | 100 | 0 | 1,857 | 171 | 0 | 2015-2019 | `2a255dc5b381…` |
| `atlas.eia.intl_petroleum` | 546,863 | 544,425 | 2,438 | 0 | 64 | 218 | 46 | 1973-2025 | `1288ec897138…` |
| `atlas.eia.intl_gas` | 164,086 | 163,206 | 880 | 0 | 18 | 219 | 45 | 1980-2024 | `5521303c5826…` |
| `atlas.eia.intl_coal` | 947,122 | 944,982 | 2,140 | 0 | 85 | 219 | 46 | 1980-2024 | `b7727519eb81…` |
| `atlas.eia.intl_electricity` | 477,280 | 474,703 | 2,577 | 0 | 48 | 219 | 45 | 1980-2024 | `93d6ef7737e5…` |
| `atlas.eia.intl_energy` | 414,963 | 414,126 | 837 | 0 | 41 | 219 | 46 | 1949-2024 | `2177b3bbc4ff…` |

Crosswalk entities: 251. With at least one cell: USGS 173, EIA 219, either 219.
No cell in any A-RESOURCES table (32): AIA Anguilla, ALA Åland Islands, AND Andorra, ATF French Southern Territories, BES Bonaire, Sint Eustatius and Saba, BLM Saint Barthélemy, BVT Bouvet Island, CCK Cocos (Keeling) Islands, CHI Channel Islands, CUW Curaçao, CXR Christmas Island, GGY Guernsey, HMD Heard Island and McDonald Islands, IMN Isle of Man, IOT British Indian Ocean Territory, JEY Jersey, LIE Liechtenstein, MAF Saint Martin (French Part), MCO Monaco, MHL Marshall Islands, MYT Mayotte, NFK Norfolk Island, PCN Pitcairn, PLW Palau, SGS South Georgia and the South Sandwich Islands, SJM Svalbard and Jan Mayen Islands, SMR San Marino, SXM Sint Maarten (Dutch part), TKL Tokelau, UMI United States Minor Outlying Islands, VAT Holy See, WLF Wallis and Futuna Islands.

No USGS mineral cell (78): ABW, AIA, ALA, AND, ASM, ATA, ATF, ATG, BES, BLM, BMU, BRB, BVT, CCK, CHI, COK, COM, CPV, CUW, CXR, CYM, DMA, ESH, FLK, FRO, FSM, GGY, GIB, GMB, GNB, GRD, GUM, HKG, HMD, HTI, IMN, IOT, JEY, KIR, KNA, LCA, LIE, MAC, MAF, MCO, MDV, MHL, MNP, MSR, MTQ, MYT, NFK, NIU, PCN, PLW, PRI, PSE, PYF, SGS, SHN, SJM, SMR, SOM, SPM, STP, SXM, TCA, TKL, TON, TUV, UMI, VAT, VCT, VGB, VIR, VUT, WLF, WSM.

No EIA cell (32): AIA, ALA, AND, ATF, BES, BLM, BVT, CCK, CHI, CUW, CXR, GGY, HMD, IMN, IOT, JEY, LIE, MAF, MCO, MHL, MYT, NFK, PCN, PLW, SGS, SJM, SMR, SXM, TKL, UMI, VAT, WLF.

* **Countries × commodities × years.** USGS: MCS 2026 = 1,536 indicators (90 commodities) × 123 countries × 2021-2025
  (world tables 2024-2025); MYB = 1,048 (2020-2024) and 1,857 (2015-2019) commodity series × 168 / 171 countries.
  EIA = 256 annual series families × 219 countries (+ 46 aggregates / historical areas) × 1949/1973/1980-2025.
* The 32 entities with no A-RESOURCES cell are tiny territories, micro-states and uninhabited islands (Monaco, San
  Marino, Holy See, Liechtenstein, Andorra, Channel Islands, Pitcairn, Bouvet …). USGS adds 78 entities without a
  mineral row (mostly small islands; also Somalia, Haiti, Western Sahara, Palestine, Hong Kong, Macao — USGS reports no
  country chapter for them).

### MCS 2026: top 5 countries per commodity (latest year as reported; e = USGS estimate, W = withheld)

| commodity | statistic (unit) | year | 1 | 2 | 3 | 4 | 5 | world total |
|---|---|---|---|---|---|---|---|---|
| Alumina | Production: Alumina, refinery production (thousand metric dry tons) | 2025 | CHN 93,000 e | AUS 17,000 e | BRA 11,000 e | IND 8,200 e | RUS 2,900 e | 150,000 |
| Aluminum | Production: Smelter production (thousand metric tons) | 2025 | CHN 45,000 e | IND 4,200 e | RUS 3,900 e | CAN 3,300 e | ARE 2,700 e | 74,000 |
| Antimony | Production: Mine production (metric tons) | 2025 | CHN 40,000 e | RUS 32,000 e | TJK 22,000 e | BOL 5,000 e | MMR 4,500 e | 110,000 (W: USA) |
| Antimony | Reserves: Reserves (metric tons) | 2025 | CHN 830,000 | RUS 350,000 | BOL 310,000 | KGZ 260,000 | MMR 140,000 | >2,000,000 |
| Arsenic | Production: Production (arsenic trioxide, gross weight) (metric tons) | 2025 | PER 30,000 e | CHN 24,000 e | MAR 5,000 e | BEL 1,000 e | RUS 500 e | 61,000 |
| Asbestos | Production: Mine production (metric tons) | 2025 | RUS 310,000 e | KAZ 250,000 e | CHN 250,000 e | BRA 150,000 e |  | 960,000 |
| Asbestos | Reserves: Reserves (metric tons) | 2025 | RUS 110,000,000 | KAZ 20,000,000 | BRA 11,000,000 | CHN 7,100,000 |  | 150,000,000 |
| Barite | Production: Mine production (thousand metric tons) | 2025 | IND 3,000 e | CHN 2,200 e | MAR 1,000 e | KAZ 700 e | IRN 300 e | 8,700 (W: USA) |
| Barite | Reserves: Reserves (thousand metric tons) | 2025 | CHN 120,000 | IRN 100,000 | KAZ 85,000 | IND 51,000 | TUR 34,000 | NA |
| Bauxite | Production: Bauxite, mine production (thousand metric dry tons) | 2025 | GIN 150,000 e | AUS 97,000 e | CHN 87,000 e | BRA 33,000 e | IND 25,000 e | 440,000 (W: USA) |
| Bauxite | Reserves: Bauxite reserves (thousand metric dry tons) | 2025 | GIN 7,400,000 | AUS 3,700,000 | VNM 3,100,000 | IDN 2,900,000 | JAM 2,000,000 | 29,000,000 |
| Beryllium | Production: Mine production (metric tons) | 2025 | USA 230 e | BRA 80 e | CHN 77 e | NGA 40 e | MOZ 3 e | 430 |
| Bismuth | Production: Refinery production (metric tons) | 2025 | CHN 14,000 e | KOR 1,000 e | JPN 500 e | LAO 500 e | KAZ 180 e | 16,000 |
| Bromine | Production: Production (metric tons) | 2025 | ISR 200,000 e | JOR 110,000 e | CHN 90,000 e | JPN 20,000 e | IND 7,000 e | 430,000 (W: USA) |
| Cadmium | Production: Refinery production (metric tons) | 2025 | CHN 9,500 e | KOR 4,300 e | CAN 1,300 e | JPN 1,300 e | KAZ 1,100 e | 23,000 |
| Cement | Production: Cement production (thousand metric tons) | 2025 | CHN 1,700,000 e | IND 470,000 e | VNM 100,000 e | TUR 89,000 e | USA 84,000 e | 3,800,000 |
| Chromium | Production: Mine production (thousand metric tons) | 2025 | ZAF 23,000 e | TUR 9,000 e | KAZ 7,000 e | IND 3,000 e | BRA 2,000 e | 51,000 |
| Chromium | Reserves: Reserves: Cr2O3 content (thousand metric tons) | 2025 | ZAF 110,000 | KAZ 100,000 | ZWE 78,000 | IND 27,000 | FIN 16,000 | >540,000 |
| Chromium | Reserves: Reserves: Ore (thousand metric tons) | 2024 | ZAF 350,000 | KAZ 230,000 | ZWE 140,000 | IND 79,000 | FIN 63,000 | >1,200,000 |
| Clays | Production: Mine production: Bentonite (thousand metric tons) | 2025 | USA 4,100 e | IND 3,700 e | TUR 2,500 e | CHN 2,100 e | IRN 1,300 e | 20,000 |
| Clays | Production: Mine production: Fuller’s earth (thousand metric tons) | 2025 | USA 2,400 e | IND 730 e | ESP 690 e | SEN 190 e | MEX 120 e | 4,400 |
| Clays | Production: Mine production: Kaolin (thousand metric tons) | 2025 | IND 8,400 e | CHN 7,800 e | UZB 6,000 e | RUS 5,000 e | USA 4,800 e | 50,000 |
| Cobalt | Production: Mine production (metric tons) | 2025 | COD 230,000 e | IDN 44,000 e | RUS 7,700 e | MDG 3,900 e | AUS 3,700 e | 310,000 |
| Cobalt | Reserves: Reserves (metric tons) | 2025 | COD 6,000,000 | AUS 1,700,000 | RUS 800,000 | IDN 760,000 | CUB 500,000 | 12,000,000 |
| Copper | Production: Mine production (thousand metric tons) | 2025 | CHL 5,300 e | COD 3,200 e | PER 2,700 e | CHN 1,800 e | RUS 1,300 e | 23,000 |
| Copper | Production: Refinery production (thousand metric tons) | 2025 | CHN 14,000 e | COD 2,800 e | CHL 1,700 e | JPN 1,400 e | RUS 950 e | 29,000 |
| Copper | Reserves: Reserves (thousand metric tons) | 2025 | CHL 180,000 | AUS 100,000 | PER 85,000 | COD 80,000 | RUS 80,000 | 980,000 |
| Diamond (Industrial) | Production: Mine production (million carats) | 2025 | RUS 16 e | COD 7 e | BWA 5 e | ZWE 5 e | ZAF 3 e | 38 |
| Diamond (Industrial) | Reserves: Reserves (million carats) | 2025 | RUS 750 | BWA 250 | AGO 150 | COD 150 | ZAF 87 | 1,700 |
| Diatomite | Production: Mine production (thousand metric tons) | 2025 | USA 720 e | FRA 250 e | ARG 190 e | TUR 150 e | CHN 140 e | 2,500 |
| Diatomite | Reserves: Reserves (thousand metric tons) | 2025 | USA 250,000 | CHN 120,000 | ESP 57,000 | TUR 44,000 | KOR 2,200 | Large |
| Feldspar And Nepheline Syenite | Production: Mine production (thousand metric tons) | 2025 | IND 6,000 e | TUR 5,300 e | IRN 3,900 e | CHN 3,700 e | ITA 2,200 e | 31,000 |
| Feldspar And Nepheline Syenite | Reserves: Reserves (thousand metric tons) | 2025 | CHN 730,000 | TUR 720,000 | IND 320,000 | KOR 200,000 | IRN 130,000 | Large |
| Fluorspar | Production: Mine production (thousand metric tons) | 2025 | CHN 6,000 e | MEX 1,500 e | MNG 1,500 e | ZAF 410 e | VNM 160 e | 10,000 |
| Fluorspar | Reserves: Reserves (thousand metric tons) | 2025 | CHN 110,000 | MEX 68,000 | ZAF 41,000 | MNG 34,000 | VNM 16,000 | 330,000 |
| Garnet (Industrial) | Production: Mine production (metric tons) | 2025 | AUS 350,000 e | CHN 280,000 e | USA 77,000 e | IND 15,000 e | CZE 4,000 e | 730,000 |
| Gemstones | Production: Mine production (thousands of carats) | 2025 | RUS 21,000 e | AGO 13,000 e | BWA 13,000 e | CAN 13,000 e | NAM 2,300 e | 69,000 |
| Gemstones | Reserves: Reserves (thousands of carats) | 2025 | RUS 750,000 | BWA 250,000 | AGO 150,000 | COD 150,000 | CAN 110,000 | >2,000,000 |
| Gold | Production: Mine production (metric tons) | 2025 | CHN 380 e | RUS 310 e | AUS 280 e | CAN 200 e | USA 160 e | 3,300 |
| Gold | Reserves: Reserves (metric tons) | 2025 | AUS 13,000 | RUS 12,000 | ZAF 5,000 | IDN 3,600 | CAN 3,200 | 66,000 |
| Graphite (Natural) | Production: Mine production (metric tons) | 2025 | CHN 1,400,000 e | MDG 80,000 e | TZA 75,000 e | BRA 65,000 e | MOZ 60,000 e | 1,800,000 |
| Graphite (Natural) | Reserves: Reserves (metric tons) | 2025 | CHN 100,000,000 | BRA 74,000,000 | MDG 27,000,000 | MOZ 25,000,000 | TZA 18,000,000 | 310,000,000 |
| Gypsum | Production: Mine production (thousand metric tons) | 2025 | USA 20,000 e | IRN 16,000 e | OMN 14,000 e | CHN 12,000 e | ESP 11,000 e | 160,000 |
| Gypsum | Reserves: Reserves (thousand metric tons) | 2025 | CHN 1,800,000 | THA 910,000 | IRN 750,000 | USA 700,000 | BRA 450,000 | Large |
| Helium | Production: Helium Production (million cubic meters) | 2025 | USA 81 e | QAT 63 e | RUS 18 e | DZA 11 e | CAN 6 e | 190 |
| Helium | Reserves: Helium Reserves (million cubic meters) | 2025 | USA 8,500 | DZA 1,800 e | RUS 1,700 e | ZAF 400 | POL 24 | NA |
| Indium | Production: Refinery production (metric tons) | 2025 | CHN 760 e | KOR 180 e | JPN 65 e | CAN 40 e | FRA 21 e | 1,100 |
| Iodine | Production: Mine production (metric tons) | 2025 | CHL 23,000 e | JPN 9,000 e | TKM 800 e | IRN 700 e | AZE 210 e | 34,000 (W: USA) |
| Iodine | Reserves: Reserves (metric tons) | 2025 | JPN 4,900,000 | CHL 750,000 | USA 250,000 | AZE 170,000 | RUS 120,000 | >6,300,000 |
| Iron And Steel | Production: Pig iron (million metric tons) | 2025 | CHN 830 e | IND 98 e | JPN 59 e | RUS 47 e | KOR 41 e | 1,300 |
| Iron And Steel | Production: Raw steel (million metric tons) | 2025 | CHN 980 e | IND 160 e | USA 82 e | JPN 81 e | RUS 65 e | 1,900 |
| Iron Ore | Production: Mine production: Iron content (thousand metric tons) | 2025 | AUS 600,000 e | BRA 260,000 e | IND 190,000 e | CHN 180,000 e | IRN 61,000 e | 1,600,000 |
| Iron Ore | Production: Mine production: Usable ore (thousand metric tons) | 2025 | AUS 980,000 e | BRA 420,000 e | IND 310,000 e | CHN 290,000 e | IRN 93,000 e | 2,600,000 |
| Iron Ore | Reserves: Reserves (million metric tons): Crude ore (million metric tons) | 2025 | AUS 59,000 | RUS 35,000 | BRA 34,000 | CHN 17,000 | MRT 10,000 | 200,000 |
| Iron Ore | Reserves: Reserves (million metric tons): Iron content (million metric tons) | 2025 | AUS 27,000 | BRA 15,000 | RUS 14,000 | MRT 4,400 | IND 3,400 | 87,000 |
| Lead | Production: Mine production (thousand metric tons) | 2025 | CHN 1,900 e | AUS 480 e | PER 290 e | USA 280 e | RUS 260 e | 4,500 |
| Lead | Reserves: Reserves (thousand metric tons) | 2025 | AUS 34,000 | CHN 22,000 | RUS 8,900 | MEX 5,600 | PER 5,000 | 95,000 |
| Lime | Production: Production (thousand metric tons) | 2025 | CHN 310,000 e | IND 17,000 e | USA 15,000 e | BRA 8,200 e | KOR 5,000 e | 420,000 |
| Lithium | Production: Mine production (metric tons) | 2025 | AUS 92,000 e | CHN 62,000 e | CHL 56,000 e | ZWE 28,000 e | ARG 23,000 e | 290,000 (W: USA) |
| Lithium | Reserves: Reserves (metric tons) | 2025 | CHL 9,200,000 | AUS 8,400,000 | CHN 4,600,000 | ARG 4,400,000 | USA 4,400,000 | 37,000,000 |
| Magnesium Compounds | Production: Mine production (thousand metric tons) | 2025 | CHN 12,700 e | BRA 1,800 e | RUS 1,700 e | TUR 1,600 e | AUT 650 e | 21,000 (W: USA) |
| Magnesium Compounds | Reserves: Reserves (thousand metric tons) | 2025 | RUS 2,300,000 | SVK 1,200,000 | CHN 700,000 | AUS 280,000 | GRC 280,000 | 7,800,000 |
| Magnesium Metal | Production: Smelter production (thousand metric tons) | 2025 | CHN 950 e | RUS 60 e | BRA 20 e | ISR 20 e | TUR 15 e | 1,100 |
| Manganese | Production: Mine production (thousand metric tons) | 2025 | ZAF 7,600 e | GAB 5,000 e | GHA 2,000 e | AUS 1,600 e | BRA 800 e | 20,000 |
| Manganese | Reserves: Reserves (thousand metric tons) | 2025 | AUS 580,000 | ZAF 550,000 | BRA 300,000 | CHN 260,000 | GAB 61,000 | 1,700,000 |
| Mercury | Production: Mine production (metric tons) | 2025 | CHN 200 e | KGZ 5 e | TJK 4 e | MAR 2 e | NOR 1 e | 210 |
| Mica (Natural) | Production: Mica (Scrap and flake): production (metric tons) | 2025 | CHN 85,000 e | MDG 70,000 e | FIN 57,000 e | USA 26,000 e | KOR 15,000 e | 350,000 |
| Mica (Natural) | Production: Mica (Sheet): Mine production (metric tons) | 2025 | IND 1,000 e |  |  |  |  | NA |
| Molybdenum | Production: Mine production (metric tons) | 2025 | CHN 97,000 e | CHL 42,000 e | USA 40,000 e | PER 39,000 e | MEX 17,000 e | 260,000 |
| Molybdenum | Reserves: Reserves (thousand metric tons) (thousand metric tons) | 2025 | CHN 7,800 | USA 3,500 | CHL 2,600 | RUS 1,100 | PER 1,000 | 17,000 |
| Nickel | Production: Mine production (metric tons) | 2025 | IDN 2,600,000 e | PHL 270,000 e | RUS 200,000 e | CAN 140,000 e | NCL 140,000 e | 3,900,000 |
| Nickel | Reserves: Reserves (metric tons) | 2025 | IDN 62,000,000 | AUS 25,000,000 | BRA 16,000,000 | RUS 8,300,000 | NCL 7,100,000 | >140,000,000 |
| Niobium (Columbium) | Production: Mine production (metric tons) | 2025 | BRA 104,000 e | CAN 6,000 e | COD 970 e | RUS 300 e | RWA 200 e | 112,000 |
| Niobium (Columbium) | Reserves: Reserves (metric tons) | 2025 | BRA 14,000,000 | CHN 6,500,000 | CAN 640,000 | USA 210,000 | RUS 3,000 | >21,000,000 |
| Nitrogen (Fixed)—Ammonia | Production: Plant production (thousand metric tons) | 2025 | CHN 49,000 e | IND 15,000 e | RUS 15,000 e | USA 14,000 e | IDN 6,000 e | 160,000 |
| Palladium | Production: Mine production: Palladium (kilograms) | 2025 | RUS 84,000 e | ZAF 70,000 e | CAN 16,000 e | ZWE 15,000 e | USA 6,200 e | 190,000 |
| Peat | Production: Mine production (thousand metric tons) | 2025 | FIN 2,500 e | CAN 2,000 e | LVA 2,000 e | BLR 1,800 e | RUS 1,800 e | 17,000 |
| Peat | Reserves: Reserves (thousand metric tons) | 2025 | FIN 6,000,000 | BLR 2,600,000 | RUS 1,000,000 | CAN 720,000 | EST 570,000 | 13,000,000 |
| Perlite | Production: Production (thousand metric tons) | 2025 | CHN 1,500 e | TUR 1,400 e | GRC 840 e | USA 460 e | HUN 80 e | 4,600 |
| Perlite | Reserves: Reserves (thousand metric tons) | 2025 | GRC 180,000 | USA 50,000 | CHN 32,000 | SVK 30,000 | IRN 15,000 | NA |
| Phosphate Rock | Production: Mine production (thousand metric tons) | 2025 | CHN 110,000 e | MAR 36,000 e | USA 20,000 e | RUS 14,000 e | JOR 12,000 e | 250,000 |
| Phosphate Rock | Reserves: Reserves (thousand metric tons) | 2025 | MAR 50,000,000 | CHN 3,400,000 | EGY 2,800,000 | TUN 2,500,000 | RUS 2,400,000 | 73,000,000 |
| Platinum-Group Metals | Reserves: PGM reserves (kilograms) | 2025 | ZAF 63,000,000 | RUS 11,000,000 e | ZWE 1,300,000 | USA 590,000 | CAN 310,000 | >76,000,000 |
| Platinum | Production: Mine production: Platinum (kilograms) | 2025 | ZAF 120,000 e | RUS 20,000 e | ZWE 18,000 e | CAN 5,000 e | USA 1,800 e | 170,000 |
| Potash | Production: Mine production (thousand metric tons) | 2025 | CAN 15,000 e | RUS 10,000 e | CHN 6,300 e | BLR 6,000 e | DEU 3,000 e | 49,000 |
| Potash | Reserves: Reserves: K2O equivalent (thousand metric tons) | 2025 | RUS 2,000,000 | CAN 1,100,000 | LAO 1,000,000 | BLR 750,000 | USA 220,000 | >5,900,000 |
| Pumice And Pumicite | Production: Mine production (thousand metric tons) | 2025 | TUR 9,700 e | JOR 1,100 e | GRC 1,000 e | SAU 980 e | DZA 900 e | 20,000 |
| Rare Earths | Production: Mine production (metric tons) | 2025 | CHN 270,000 e | USA 51,000 e | AUS 29,000 e | MMR 22,000 e | THA 4,800 e | 390,000 |
| Rare Earths | Reserves: Reserves (metric tons) | 2025 | CHN 44,000,000 | BRA 21,000,000 | AUS 6,300,000 | RUS 3,800,000 | VNM 3,500,000 | >85,000,000 |
| Rhenium | Production: Mine production (kilograms) | 2025 | CHL 30,000 e | CHN 20,000 e | POL 10,000 e | USA 9,800 e | UZB 7,000 e | 81,000 |
| Rhenium | Reserves: Reserves (kilograms) | 2025 | CHL 1,300,000 | USA 400,000 | RUS 310,000 | CHN 200,000 | KAZ 190,000 | Large |
| Salt | Production: Mine production (thousand metric tons) | 2025 | CHN 56,000 e | USA 40,000 e | IND 30,000 e | DEU 15,000 e | CAN 13,000 e | 270,000 |
| Sand And Gravel (Industrial) | Production: Mine production (thousand metric tons) | 2025 | USA 120,000 e | CHN 92,000 e | NLD 68,000 e | TUR 14,000 e | FRA 13,000 e | 430,000 |
| Selenium | Production: Refinery production (metric tons) | 2025 | CHN 2,000 e | JPN 640 e | RUS 320 e | BEL 200 e | CAN 130 e | 3,800 |
| Silicon | Production: Ferrosilicon (thousand metric tons) | 2025 | CHN 3,500 e | RUS 420 e | BRA 170 e | NOR 150 e | KAZ 120 e | 5,000 (W: USA) |
| Silicon | Production: Silicon metal (thousand metric tons) | 2025 | CHN 4,000 e | BRA 180 e | NOR 130 e | FRA 68 e | AUS 47 e | 4,600 (W: USA) |
| Silver | Production: Mine production (metric tons) | 2025 | MEX 6,300 e | PER 3,600 e | CHN 3,400 e | BOL 1,500 e | CHL 1,400 e | 26,000 |
| Silver | Reserves: Reserves (metric tons) | 2025 | PER 110,000 | RUS 92,000 | AUS 91,000 | CHN 67,000 | POL 59,000 | 610,000 |
| Soda Ash | Production: Mine production: Natural (thousand metric tons) | 2025 | USA 12,000 e | TUR 6,000 e | BWA 290 e | KEN 270 e | ETH 18 e | — |
| Soda Ash | Reserves: Reserves: Natural (thousand metric tons) | 2025 | USA 23,000,000 | TUR 840,000 | ETH 400,000 | BWA 16,000 | KEN 7,000 | — |
| Strontium | Production: Mine production (metric tons) | 2025 | IRN 250,000 e | ESP 100,000 e | CHN 80,000 e | MEX 20,000 e | ARG 700 e | 450,000 |
| Sulfur | Production: Production, all forms (thousand metric tons) | 2025 | CHN 19,000 e | USA 8,100 | RUS 7,500 e | SAU 7,200 e | ARE 6,300 e | 84,000 |
| Talc And Pyrophyllite | Production: Mine production (thousand metric tons) | 2025 | KOR 300 e | ZAF 300 e | TUR 300 e | AFG 200 e | FIN 200 e | 6,900 |
| Tantalum | Production: Mine production (metric tons) | 2025 | COD 1,300 e | RWA 400 e | NGA 390 e | BRA 190 e | CHN 80 e | 2,500 |
| Tantalum | Reserves: Reserves (metric tons) | 2025 | CHN 240,000 | AUS 120,000 | BRA 40,000 | RUS 150 |  | NA |
| Tellurium | Production: Refinery production (metric tons) | 2025 | CHN 800 e | RUS 67 e | JPN 61 e | CAN 28 e | UZB 18 e | 1,000 |
| Tin | Production: Mine production (metric tons) | 2025 | CHN 71,000 e | IDN 61,000 e | PER 33,000 e | BRA 28,000 e | COD 27,000 e | 290,000 |
| Tin | Reserves: Reserves (metric tons) | 2025 | IDN 1,400,000 | CHN 1,200,000 | BRA 700,000 | MMR 700,000 | AUS 570,000 | >6,000,000 |
| Titanium Mineral Concentrates | Production: Mine production: Ilmenite (thousand metric tons) | 2025 | CHN 3,200 e | MOZ 1,900 e | ZAF 1,300 e | AUS 780 e | NOR 390 e | — |
| Titanium Mineral Concentrates | Production: Mine production: Rutile (thousand metric tons) | 2025 | AUS 200 e | SLE 110 e | ZAF 100 e | IND 13 e | MOZ 10 e | — |
| Titanium Mineral Concentrates | Reserves: Reserves: Ilmenite (thousand metric tons) | 2025 | AUS 170,000 | CHN 110,000 | CAN 50,000 | NOR 37,000 | MDG 30,000 | — |
| Titanium Mineral Concentrates | Reserves: Reserves: Rutile (thousand metric tons) | 2025 | AUS 35,000 | ZAF 6,200 | SLE 2,900 | UKR 2,500 | MOZ 720 | — |
| Titanium Sponge Metal | Production: Titanium sponge metal production (metric tons) | 2025 | CHN 260,000 e | JPN 53,000 e | RUS 25,000 e | KAZ 16,000 e | SAU 12,000 e | 370,000 |
| Tungsten | Production: Mine production (metric tons) | 2025 | CHN 67,000 e | VNM 3,000 e | KAZ 2,400 e | PRK 2,000 e | RUS 2,000 e | 85,000 |
| Tungsten | Reserves: Reserves (metric tons) | 2025 | CHN 2,500,000 | AUS 570,000 | RUS 400,000 | VNM 170,000 | ESP 66,000 | >4,700,000 |
| Vanadium | Production: Mine production (metric tons) | 2025 | CHN 82,000 e | RUS 21,000 e | BRA 5,300 e | ZAF 5,000 e |  | 110,000 |
| Vanadium | Reserves: Reserves (metric tons) | 2025 | AUS 10,000 | CHN 5,800 | RUS 5,000 | ZAF 520 | BRA 94 | 21,000 |
| Vermiculite | Production: Mine production (thousand metric tons) | 2025 | ZAF 160 e | USA 100 e | BRA 50 e | CHN 40 e | RUS 40 e | 460 |
| Vermiculite | Reserves: Reserves (thousand metric tons) | 2025 | USA 25,000 | ZAF 14,000 | TUR 11,000 | BRA 6,600 | CHN 2,900 | NA |
| Zeolites (Natural) | Production: Mine production (metric tons) | 2025 | SVK 280,000 e | GEO 240,000 e | KOR 160,000 e | CHN 150,000 e | RUS 130,000 e | 1,300,000 |
| Zinc | Production: Mine production (thousand metric tons) | 2025 | CHN 4,100 e | PER 1,500 e | AUS 1,100 e | IND 870 e | MEX 780 e | 13,000 |
| Zinc | Reserves: Reserves (thousand metric tons) | 2025 | AUS 64,000 | CHN 60,000 | RUS 29,000 | PER 18,000 | MEX 14,000 | 240,000 |
| Zirconium | Production: Zirconium mineral concentrates, mine production (thousand metric tons, gross weight) (thousand metric tons) | 2025 | AUS 400 e | ZAF 270 e | MOZ 160 e | CHN 100 e | USA 100 e | 1,200 |
| Zirconium | Reserves: Zirconium reserves(thousand metric tons, ZrO2 content) (thousand metric tons) | 2025 | AUS 55,000 | ZAF 5,900 | IDN 3,400 | SEN 2,600 | MDG 2,100 | >70,000 |

### EIA: top 5 per series (latest year with ≥ 50 reporting countries; aggregates excluded)

| series | unit | year | 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|---|---|
| crude oil production (`INTL.57-1-TBPD.A`) | thousand barrels per day | 2025 | USA 13,586 | RUS 9,885 | SAU 9,556 | CAN 4,964 | IRQ 4,388 |
| petroleum & other liquids production (`INTL.53-1-TBPD.A`) | thousand barrels per day | 2025 | USA 23,612 | SAU 11,214 | RUS 10,535 | CAN 6,257 | CHN 5,446 |
| petroleum consumption (`INTL.5-2-TBPD.A`) | thousand barrels per day | 2024 | USA 20,464 | CHN 16,371 | IND 5,599 | RUS 3,802 | SAU 3,695 |
| crude oil exports (`INTL.57-4-TBPD.A`) | thousand barrels per day | 2018 | SAU 7,341 | RUS 5,196 | IRQ 3,976 | CAN 3,177 | ARE 2,427 |
| crude oil imports (`INTL.57-3-TBPD.A`) | thousand barrels per day | 2018 | CHN 9,238 | USA 7,768 | IND 4,530 | KOR 3,034 | JPN 3,013 |
| dry gas production (`INTL.26-1-BCM.A`) | billion cubic meters | 2024 | USA 1,069 | RUS 642 | IRN 279 | CHN 258 | CAN 199 |
| dry gas exports (`INTL.26-4-BCM.A`) | billion cubic meters | 2024 | USA 218 | RUS 148 | NOR 126 | QAT 126 | AUS 106 |
| dry gas imports (`INTL.26-3-BCM.A`) | billion cubic meters | 2024 | CHN 179 | USA 89.05 | JPN 87.97 | DEU 74.26 | MEX 66.98 |
| coal production (`INTL.7-1-MT.A`) | 1000 metric tons | 2024 | CHN 4,402,501 | IND 1,012,946 | IDN 836,000 | RUS 476,208 | AUS 467,739 |
| coal exports (`INTL.7-4-MT.A`) | 1000 metric tons | 2024 | IDN 557,756 | AUS 362,042 | RUS 181,983 | USA 98,272 | MNG 74,702 |
| coal imports (`INTL.7-3-MT.A`) | 1000 metric tons | 2024 | CHN 542,783 | IND 232,812 | JPN 171,061 | KOR 115,583 | TWN 56,979 |
| coal reserves (`INTL.7-6-MST.A`) | million short tons | 2023 | USA 273,244 | RUS 178,757 | CHN 173,108 | AUS 164,764 | IND 140,795 |
| electricity generation (`INTL.2-12-BKWH.A`) | billion kilowatthours | 2024 | CHN 10,162 | USA 4,393 | IND 2,052 | RUS 1,160 | JPN 907 |
| electricity net imports (largest) (`INTL.2-23-BKWH.A`) | billion kilowatthours | 2024 | ITA 51 | GBR 33.41 | THA 32.41 | DEU 26.27 | USA 13.82 |
| electricity net imports (most negative = net exporters) (`INTL.2-23-BKWH.A`) | billion kilowatthours | 2024 | FRA -89.85 | LAO -36.7 | SWE -33.44 | PRY -30 | NOR -18.44 |
| solar capacity (`INTL.116-7-MK.A`) | million kilowatts | 2024 | CHN 887 | USA 177 | IND 97.38 | JPN 91.61 | DEU 89.94 |
| wind capacity (`INTL.37-7-MK.A`) | million kilowatts | 2024 | CHN 522 | USA 152 | DEU 72.82 | IND 48.16 | BRA 32.96 |
| nuclear generation (`INTL.27-12-BKWH.A`) | billion kilowatthours | 2024 | USA 782 | CHN 451 | FRA 362 | RUS 216 | KOR 189 |
| primary energy production (`INTL.44-1-QBTU.A`) | quadrillion Btu | 2024 | CHN 131 | USA 104 | RUS 60.26 | SAU 26.95 | CAN 22.64 |
| energy CO2 emissions (`INTL.4008-8-MMTCD.A`) | million metric tonnes carbon dioxide | 2024 | CHN 12,716 | USA 4,782 | IND 2,872 | RUS 1,952 | JPN 941 |

### Minerals Yearbook: commodities per country (series with ≥ 1 value), 2020-2024 and 2015-2019

* `myb2024`: 168 countries; median 17 series per country; most: CHN 134, RUS 114, ZAF 97, TUR 86, BRA 74, IND 70, ESP 67, KAZ 67; fewest: SGP 1, BEN 1, BLZ 1, BRN 1, TCD 1, LSO 1, GRL 1, REU 1.
* `myb2019`: 171 countries; median 21 series per country; most: CHN 157, ZAF 121, RUS 121, IND 115, BRA 115, TUR 106, DEU 93, CAN 92; fewest: BLZ 1, SSD 1, CAF 1, LSO 1, GLP 1, BEN 1, REU 1, SGP 1.


Notes on reading these tables: values are as published for the latest edition year (MCS 2026 → 2025, USGS estimates
marked `e`); `W` lists countries whose figure is withheld (no value, never zero). EIA crude-oil imports/exports are
complete only to 2018 (216 countries); 2019-2020 cover 36 OECD countries and the series stop in 2020.

## 3. EIA as a second energy source — definition differences (for ATLASOPS cross-source fallback)

OWID's energy compilation (`registry/atlas/catalog/energy.yaml`, A-QOL; Energy Institute + Ember + EIA) reports
energy in TWh. EIA reports physical units and quadrillion Btu, so the EIA concepts are separate concepts
(`resources.energy.eia.*`), not extra entries in OWID's `source_order`:

| topic | OWID / Energy Institute (A-QOL) | EIA (this agent) |
|---|---|---|
| oil production | `energy.oil_production`, TWh, crude + NGLs | `INTL.57-1-TBPD.A` crude incl. lease condensate (no NGPL); `INTL.53-1-TBPD.A` all liquids incl. NGPL, biofuels, refinery gain; thousand b/d |
| gas production | `energy.gas_production`, TWh | `INTL.26-1-BCM.A` dry gas, billion m³ (also bcf); gross, flared, reinjected separate |
| coal production | `energy.coal_production`, TWh | `INTL.7-1-MT.A`, 1000 metric tons, all ranks (+ thousand short tons, + per rank) |
| primary energy | `energy.primary_energy_consumption`, TWh, substitution method | `INTL.44-2-QBTU.A`, quad Btu, EIA conventions |
| electricity | generation TWh (Ember/EI) | `INTL.2-12-BKWH.A` net generation, billion kWh (= TWh) — the closest same-unit pair; capacity by source in million kW (not in OWID) |
| CO2 | OWID/GCP incl. cement and flaring | `INTL.4008-8-MMTCD.A` energy-related only |
| not in OWID | — | crude and gas imports/exports, coal trade, coal reserves, installed capacity by technology, electricity trade and net imports, OECD stocks |

A headline concept can convert `BKWH` ↔ TWh exactly (1:1); other pairs need declared conversion factors and differ
in scope, so they are better shown side by side.

## 4. Sources skipped or not completed, with reasons

| source | decision | reason |
|---|---|---|
| **BGS World Mineral Statistics** (UK, 1970-2022, production/imports/exports) | **skipped** | Not OGL. The "BGS Mineral Statistics terms and conditions (IPR)" allow use "for non-commercial academic and research purposes"; commercial use or giving the tables to a third party needs NERC permission (ipr@bgs.ac.uk); the OGC API collection declares `license: null`. Also, its API returns only 0/NULL for the yearbook's symbols (estimates `*` → 0, nil `—` → 0), which would violate "never zero for withheld/estimate". Could be added if BGS grants permission (historical depth 1970-2014 is the main loss). |
| **IRENA** (IRENASTAT renewable capacity/generation) | not ingested | Data are reachable (PxWeb API `pxweb.irena.org`, tables `Country_ELECSTAT_2026_H2_PX.px`), but PxWeb returns data only for POSTed queries and atlas runs are GET-only (`run.get`); IRENA's terms are attribution-style ("may be freely used, shared, copied … provided that appropriate acknowledgement is given") with third-party exclusions, but irena.org returned 403 so the dataset terms could not be read directly. EIA already covers installed capacity by technology for 219 countries. A `run.post` (budget-metered) would unlock it. |
| **Global Energy Monitor** trackers (CC BY 4.0) | not ingested | Downloads require a request form with personal contact details (no stable public file URL to fetch through `run.get`). Country aggregates would also be REVA sums of plant rows. |
| EIA API v2 | not used | Needs a registered `api_key` (`API_KEY_MISSING`); the bulk file has the same international series. |
| Oil / gas **proved reserves** | gap | EIA's international data no longer list crude-oil / natural-gas reserves (product catalogue: only coal reserves); the Energy Institute stopped publishing reserves; OPEC Annual Statistical Bulletin terms not checked. Coal reserves (EIA) and every mineral reserve (USGS) are covered. |
| MYB facilities table (`2024MYBv3_Facilities_table.csv`, 7.7 MB) | not ingested | Plant-level capacities, not country-year cells; candidate for KG facilities later. |

## 5. Gaps and alternatives

| gap | where | alternative |
|---|---|---|
| Mineral series 2015-2024 only; MCS world tables 2024-2025 | USGS | BGS (licence above); older MYB editions exist only as PDFs/spreadsheets per country |
| MYB 2019 mixes units per country (e.g. cement in thousand metric tons for 121 countries, metric tons for 27) | `usgs.myb2019` | kept as published (unit in the indicator); the catalog pairs only same-unit series |
| Crude oil trade after 2018 (only 36 OECD countries 2019-2020) | EIA | UN Comtrade / BACI (A-TRADE, HS 2709) for trade values and quantities |
| Oil/gas proved reserves | — | none open found (see above) |
| Minerals for small islands / micro-states | USGS | none needed (no production) |
| Surplus/deficit ("pays excédentaires/déficitaires") | — | EIA publishes imports, exports and electricity *net imports*; for oil/gas/coal a net balance would be a REVA computation (production − consumption or exports − imports) and is not stored as a source value; USGS publishes U.S. net import reliance only |

## 6. Notes for the lead / ATLASCORE / ATLASOPS

* **Budgets**: merge `registry/atlas/budget_requests/a_resources.yaml`. Until then
  `tests/invariants/test_invariant_30_budgets.py::test_budget_keys_are_registered_sources_and_cover_every_connector`
  lists `usgs.sciencebase` and `eia.opendata_bulk` among the registered-but-unbudgeted sources (the test already fails
  on main for other agents' unmerged keys: cliopatria.github, unctad.bulk, …). The other invariant-30 tests pass; the
  modules create no HTTP client. The live run used the pinned policy plus exactly the requested limits, in process,
  on the throwaway ledger (consumed 9 requests / 11.0 MB and 3 / 24.2 MB, 0 refused).
* **Seed**: both sources are discovered (`--list`); the seed registers them like every atlas source.
* **LINKS**: MCS "Import Sources" rows are U.S. import-dependence edges (partner → U.S., share of imports 2021-24, per
  commodity); EIA `flags.eia_members` gives aggregate membership (OPEC, OECD, EU27…) as EIA defines it.
* Live tables (Parquet sha256 prefixes above) were 13 MB of Parquet for 2.6 M cells; the store with raw archives was
  48 MB. Disk used by this agent's scratch: ~65 MB.
