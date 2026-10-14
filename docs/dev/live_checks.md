# Live source checks — 2026-10-14

Implementation prompt §14 item 4 ("Live source checks and any missing country/year coverage"), §6, §17.4
(real live acquisitions required for live acceptance) and architecture §10.4.

**Run:** 2026-10-14 23:31:13 → 23:34:30 UTC, git `d2524865cf19389c5c127900c2d34dfd922f0a55` (with the
uncommitted working tree of that moment). Fresh database `reva_live_e3dc57761a` created, bootstrapped
(migrations 0001–0007) and seeded with the same pattern as the `db` test fixture, then dropped. The dev
database `reva` was not touched. Every acquisition has origin `live` (55 live, 0 non-live). Nothing was
recorded into `tests/fixtures`. Raw bytes are kept content-addressed in
`var/live/runs/20261014T233107Z/objects/`, the full machine-readable record is `var/live/live_checks.json`
and a generated rendering is `var/live/live_checks.md`.

**Re-run:** `uv run python scripts/live_source_checks.py [--keep-db] [--connector NAME] [--skip-eurostat]`
(about 3.5 minutes, dominated by Comtrade spacing). Reusable structural checks:
`uv run pytest -m live tests/live` (deselected by default).

Rate limits: the shared per-host limiter from `make_live_client` enforced Comtrade ≥ 1.5 s spacing, one
request in flight (observed ≈ 3.5 s per request including latency); World Bank 0.5 s, Crossref 1 s,
Eurostat 1 s, Natural Earth 1 s. All 60 requests (55 connector, 2 reference, 3 Eurostat) ended in HTTP 200. Comtrade, however, answered
the first attempt of almost every request with HTTP 429 (see Surprises 2); the client honoured
`Retry-After` and succeeded on attempt 2 (twice on attempt 3).

## Gate summary

| Gate | Result |
|---|---|
| Comtrade reporter codes FRA=251, DZA=12, CHN=156, USA=842 match the provider's Reporters.json | **pass** |
| Comtrade partnerAreas.json matches the pinned `registry/reference/manifest.yaml` SHA-256 | **pass** |
| `worldbank.wdi.indicators` live (5 requests, 5 published, pagination fully consumed) | **pass** |
| `un.comtrade.public_exact` live (48 requests, 48 published, no quarantine) | **pass** |
| `naturalearth.admin0_110m` live (1 request, published, geometries matched) | **pass** |
| `crossref.works.doi` live (1 request, study metadata stored) | **pass** |
| Live belief release published | **pass**: release `b9d17dfc-4d01-4055-9e4b-a2439d9ba478`, sequence 1, 482 estimates, manifest `d565d972a37dbe856660f0eb99b08bcc62fb635406872465994fce5b810ac739`, cutoff 2026-10-14T23:34:24.743Z |
| No fixture-origin evidence in the live database | **pass** |
| Eurostat `demo_pjan` FR 2023 through agent G's approved template (live discovery → manifest → live sample → validate) | **pass** (validated, deliberately **not published**: promotion goes through the autonomy gate) |

All connector health rows are `healthy` with request success ratio 1.0.

## Coverage matrix (catalogue `mvp-1.0.0`, release `b9d17dfc…`)

Each cell is one requirement × country × year. `value`: the release holds a decimal value. `Missing`:
the release holds a typed Missing (reason and source flags shown). `absent`: the release holds nothing for
that cell (with no live observation in the evidence store either).

| Requirement | Concept | Cells | value | Missing | absent |
|---|---|---|---|---|---|
| R-POP-WDI | demography.population.wdi_midyear_total (FRA, DZA, CHN, USA × 2000–2024) | 100 | 100 | 0 | 0 |
| R-GDP-WDI | economics.gdp.nominal_current_usd (× 2020–2024) | 20 | 20 | 0 | 0 |
| R-CRUDE-M | trade.hs2709.import_value.world_total (× 2020–2024) | 20 | 16 | 4 | 0 |
| R-CRUDE-X | trade.hs2709.export_value.world_total (× 2020–2024) | 20 | 16 | 4 | 0 |
| R-POP-JAN-AGESEX | demography.population.jan1_age_sex (FRA × 2023) | 1 | 0 | 0 | 1 |
| **Total** | | **161** | **152** | **8** | **1** |

### Gaps

- **DZA crude imports and exports 2020–2023 (8 cells):** typed `Missing(reason=unavailable)` with
  `provider_returned_no_rows=true`; Comtrade answered HTTP 200 with `count: 0` for each exact key. Never
  zero-filled.
- **R-POP-JAN-AGESEX FRA 2023 (1 cell):** absent. No built-in connector serves it. The Eurostat
  template path validated live (below) but publication needs the autonomy promotion gate
  (`reva.autonomy.jobs:handle_discovery`), which this run deliberately did not invoke.
- WDI has no null value in 2000–2024 for any of the four countries in either indicator
  (`lastupdated` 2026-10-08).

Values per cell (as published in the release; USD nominal for trade and GDP, persons for population):

#### R-POP-WDI `demography.population.wdi_midyear_total` — {'value': 100, 'missing': 0, 'absent': 0}

| geo | value | missing | absent | first/last value year |
|---|---|---|---|---|
| FRA | 25 | 0 | 0 | 2000/2024 |
| DZA | 25 | 0 | 0 | 2000/2024 |
| CHN | 25 | 0 | 0 | 2000/2024 |
| USA | 25 | 0 | 0 | 2000/2024 |

#### R-GDP-WDI `economics.gdp.nominal_current_usd` — {'value': 20, 'missing': 0, 'absent': 0}

| geo | 2020 | 2021 | 2022 | 2023 | 2024 |
|---|---|---|---|---|---|
| FRA | 2647926055110.05 | 2966433692008.09 | 2794788137066.94 | 3056250648138.29 | 3160442622465.08 |
| DZA | 164873415325.201 | 186231205262.082 | 225581644702.568 | 247923887214.655 | 269322281664.773 |
| CHN | 14996414166715.1 | 18201698719564 | 18316765021690.2 | 18270356654533.2 | 18729668435848 |
| USA | 21375281000000 | 23725645000000 | 26054614000000 | 27811517000000 | 29298013000000 |

#### R-CRUDE-M `trade.hs2709.import_value.world_total` — {'value': 16, 'missing': 4, 'absent': 0}

| geo | 2020 | 2021 | 2022 | 2023 | 2024 |
|---|---|---|---|---|---|
| FRA | 11557281800.595 | 19741889380.538 | 35069451103.978 | 31530866245.503 | 29685193775.098 |
| DZA | Missing(unavailable; provider_returned_no_rows) | Missing(unavailable; provider_returned_no_rows) | Missing(unavailable; provider_returned_no_rows) | Missing(unavailable; provider_returned_no_rows) | 5564 |
| CHN | 178452854859 | 258522886285 | 365512019551 | 337644667180 | 325186599333 |
| USA | 81629597574 | 138383895802 | 204715565176 | 172426221223 | 174423623898 |

#### R-CRUDE-X `trade.hs2709.export_value.world_total` — {'value': 16, 'missing': 4, 'absent': 0}

| geo | 2020 | 2021 | 2022 | 2023 | 2024 |
|---|---|---|---|---|---|
| FRA | 39776782.061 | 17913806.843 | 3909418.116 | 43219393.04 | 16103887.031 |
| DZA | Missing(unavailable; provider_returned_no_rows) | Missing(unavailable; provider_returned_no_rows) | Missing(unavailable; provider_returned_no_rows) | Missing(unavailable; provider_returned_no_rows) | 14183287575.254 |
| CHN | 481625542 | 1010393812 | 1438103655 | 843802125 | 1178947474 |
| USA | 50285962130 | 69356113331 | 117034462388 | 117160520564 | 118499361483 |

#### R-POP-JAN-AGESEX `demography.population.jan1_age_sex` — {'value': 0, 'missing': 0, 'absent': 1}

| geo | 2023 |
|---|---|
| FRA | absent |

## Comtrade HS2709 world totals (partner 0), per reporter/flow/year

| reporter | flow | year | rows | primaryValue | cif | fob | netWgt | cls | isReported | isAggregate | legacyEst |
|---|---|---|---|---|---|---|---|---|---|---|---|
| FRA | M | 2020 | 1 | 11557281800.595 | 11557281800.595 | None | None | H5 | False | True | 0 |
| FRA | M | 2021 | 1 | 19741889380.538 | 19741889380.538 | None | None | H5 | False | True | 0 |
| FRA | M | 2022 | 1 | 35069451103.978 | 35069451103.978 | None | None | H6 | False | True | 0 |
| FRA | M | 2023 | 1 | 31530866245.503 | 31530866245.503 | None | None | H6 | False | True | 0 |
| FRA | M | 2024 | 1 | 29685193775.098 | 29685193775.098 | None | 47020133246.088 | H6 | False | True | 6 |
| FRA | X | 2020 | 1 | 39776782.061 | None | 39776782.061 | None | H5 | False | True | 0 |
| FRA | X | 2021 | 1 | 17913806.843 | None | 17913806.843 | None | H5 | False | True | 0 |
| FRA | X | 2022 | 1 | 3909418.116 | None | 3909418.116 | None | H6 | False | True | 0 |
| FRA | X | 2023 | 1 | 43219393.04 | None | 43219393.04 | None | H6 | False | True | 0 |
| FRA | X | 2024 | 1 | 16103887.031 | None | 16103887.031 | 24440664.903 | H6 | False | True | 6 |
| CHN | M | 2020 | 1 | 178452854859.0 | 178452854859.0 | 0.0 | 542006688757.0 | H5 | False | True | 0 |
| CHN | M | 2021 | 1 | 258522886285.0 | 258522886285.0 | None | 512922149241.0 | H5 | False | True | 0 |
| CHN | M | 2022 | 1 | 365512019551.0 | 365512019551.0 | None | 508276035032.0 | H6 | False | True | 0 |
| CHN | M | 2023 | 1 | 337644667180.0 | 337644667180.0 | None | 553991815980.023 | H6 | False | True | 6 |
| CHN | M | 2024 | 1 | 325186599333.0 | 325186599333.0 | None | 553231901236.0 | H6 | False | True | 0 |
| CHN | X | 2020 | 1 | 481625542.0 | 0.0 | 481625542.0 | 1638107900.0 | H5 | False | True | 0 |
| CHN | X | 2021 | 1 | 1010393812.0 | None | 1010393812.0 | 2610761300.0 | H5 | False | True | 0 |
| CHN | X | 2022 | 1 | 1438103655.0 | None | 1438103655.0 | 2052010500.0 | H6 | False | True | 0 |
| CHN | X | 2023 | 1 | 843802125.0 | None | 843802125.0 | 1420845300.0 | H6 | False | True | 0 |
| CHN | X | 2024 | 1 | 1178947474.0 | None | 1178947474.0 | 2089129800.0 | H6 | False | True | 0 |
| USA | M | 2020 | 1 | 81629597574.0 | 81629597574.0 | 0.0 | 331755659220.0 | H5 | False | True | 0 |
| USA | M | 2021 | 1 | 138383895802.0 | 138383895802.0 | 0.0 | 350245061670.0 | H5 | False | True | 0 |
| USA | M | 2022 | 1 | 204715565176.0 | 204715565176.0 | 198385384839.0 | 359125481334.0 | H6 | False | True | 0 |
| USA | M | 2023 | 1 | 172426221223.0 | 172426221223.0 | 165333122697.0 | 370391660223.0 | H6 | False | True | 0 |
| USA | M | 2024 | 1 | 174423623898.0 | 174423623898.0 | 167480754189.0 | 375657066222.0 | H6 | False | True | 0 |
| USA | X | 2020 | 1 | 50285962130.0 | 0.0 | 50285962130.0 | 184211339079.0 | H5 | False | True | 0 |
| USA | X | 2021 | 1 | 69356113331.0 | 0.0 | 69356113331.0 | 172772127009.0 | H5 | False | True | 0 |
| USA | X | 2022 | 1 | 117034462388.0 | None | 117034462388.0 | 209184635442.0 | H6 | False | True | 0 |
| USA | X | 2023 | 1 | 117160520564.0 | None | 117160520564.0 | 235677354408.0 | H6 | False | True | 0 |
| USA | X | 2024 | 1 | 118499361483.0 | None | 118499361483.0 | 238788419322.0 | H6 | False | True | 0 |
| DZA | M | 2020 | 0 | None | None | None | None | None | None | None | None |
| DZA | M | 2021 | 0 | None | None | None | None | None | None | None | None |
| DZA | M | 2022 | 0 | None | None | None | None | None | None | None | None |
| DZA | M | 2023 | 0 | None | None | None | None | None | None | None | None |
| DZA | M | 2024 | 1 | 5564.0 | 5564.0 | None | 9095.868 | H4 | False | True | 6 |
| DZA | X | 2020 | 0 | None | None | None | None | None | None | None | None |
| DZA | X | 2021 | 0 | None | None | None | None | None | None | None | None |
| DZA | X | 2022 | 0 | None | None | None | None | None | None | None | None |
| DZA | X | 2023 | 0 | None | None | None | None | None | None | None | None |
| DZA | X | 2024 | 1 | 14183287575.254 | None | 14183287575.254 | 26059869885.602 | H4 | False | True | 6 |

## Comtrade bilateral 2023 (partner rows excl. World)

| reporter | flow:year | rows | partner rows | world row in response | null values | cls |
|---|---|---|---|---|---|---|
| FRA | M:2023 | 43 | 42 | True | 0 | H6 |
| FRA | X:2023 | 23 | 22 | True | 0 | H6 |
| CHN | M:2023 | 50 | 49 | True | 0 | H6 |
| CHN | X:2023 | 4 | 3 | True | 0 | H6 |
| USA | M:2023 | 46 | 45 | True | 0 | H6 |
| USA | X:2023 | 42 | 41 | True | 0 | H6 |
| DZA | M:2023 | 0 | 0 | False | 0 |  |
| DZA | X:2023 | 0 | 0 | False | 0 |  |


## Negative demonstration: Algeria (DZA, Comtrade reporter 12) HS 2709

| Flow | 2020 | 2021 | 2022 | 2023 | 2024 |
|---|---|---|---|---|---|
| Imports (M), world partner 0 | 0 rows → Missing | 0 rows → Missing | 0 rows → Missing | 0 rows → Missing | **1 row: primaryValue 5,564 USD (CIF), netWgt 9,095.868 kg, H4, isReported=false, isAggregate=true, legacyEstimationFlag=6** |
| Exports (X), world partner 0 | 0 rows → Missing | 0 rows → Missing | 0 rows → Missing | 0 rows → Missing | **1 row: primaryValue 14,183,287,575.254 USD (FOB), netWgt 26,059,869,885.602 kg, H4, isReported=false, isAggregate=true, legacyEstimationFlag=6** |
| Bilateral 2023 (M and X) | | | | 0 rows | |

- The empty answers are HTTP 200 bodies `{"elapsedTime":"…","count":0,"data":[],"error":""}` (58 bytes). They
  become typed `Missing(unavailable)` with `provider_returned_no_rows=true`, never zero.
- **2024 values do exist** (as the brief reported). The live release selects them, so the DZA 2024 crude
  cells are `value`, not `Missing`. Unlike FRA/CHN/USA they are reported in classification **H4** (not H6)
  and carry `legacyEstimationFlag=6`. This run did not check what flag 6 means; it is kept verbatim in
  `source_flags` for review. The import figure is tiny (5,564 USD for about 9.1 t). It is a real provider
  value and must not be mistaken for a crude-import dependency, nor treated as a measurement of zero.
- Consequence for the crude-exposure demonstration: a request for DZA **2023** still has no input
  (missing_input). A request for DZA **2024** now has inputs, so the "all-four request returns
  missing_input for DZA" negative test holds only for 2020–2023. It is not true for every year.

## Provider reference metadata

| Item | URL | HTTP | Bytes | SHA-256 | Retrieved (UTC) | Notes |
|---|---|---|---|---|---|---|
| Comtrade reporters | https://comtradeapi.un.org/files/v1/app/reference/Reporters.json | 200 | 80,831 | `f2a1d78949089831c1fc7714e009996887db724a8d5a43379cdc37be9449f74b` | 2026-10-14 23:31 | 255 entries; ETag `0x8DF09E893526196`, Last-Modified Thu, 03 Sep 2026 18:24:21 GMT |
| Comtrade partner areas | https://comtradeapi.un.org/files/v1/app/reference/partnerAreas.json | 200 | 96,279 | `2da5667693b5a7f8b203028964a431cdb46356d3a377c7369bb9736c59f03733` | 2026-10-14 23:31 | 310 entries; **matches the pin** in `registry/reference/manifest.yaml`; id 0 = "World" |

| ISO3 | Expected | Provider entry | reporterNote | Effective / expired | Verified |
|---|---|---|---|---|---|
| FRA | 251 | 251 "France" | France, Monaco | 1900-01-01 / — | yes (M49 250 is not a Comtrade reporter code for FRA) |
| DZA | 12 | 12 "Algeria" | Algeria | 1900-01-01 / — | yes |
| CHN | 156 | 156 "China" | China | 1900-01-01 / — | yes |
| USA | 842 | 842 "USA" | USA, Puerto Rico and US Virgin Islands | 1981-01-01 / — | yes; the provider also lists **841** "USA and Puerto Rico (...1980)", expired 1980-12-31 |

## World Bank Indicators v2

| Request | HTTP | Bytes | SHA-256 | page/pages/per_page/total | lastupdated |
|---|---|---|---|---|---|
| https://api.worldbank.org/v2/country/FRA;DZA;CHN;USA?format=json&per_page=100 | 200 | 1,660 | `ceaf9aeb32e47c3eba5b8483124958e68f62d991a4b35ab5623415aad6d9bc1c` | 1/1/100/4 | — |
| https://api.worldbank.org/v2/indicator/SP.POP.TOTL?format=json | 200 | 1,099 | `2f8491efc00bf2c0995112ff8e87aba69e5e9d22d0880b22f7e3a4ec757ba05c` | — | — |
| https://api.worldbank.org/v2/country/FRA;DZA;CHN;USA/indicator/SP.POP.TOTL?format=json&date=2000:2024&per_page=1000&page=1 | 200 | 19,844 | `b8c35c012e255e3f72d6bf0a080cbbf290abd831cdba1837c819859d07b3f2a9` | 1/1/1000/100 | 2026-10-08 |
| https://api.worldbank.org/v2/indicator/NY.GDP.MKTP.CD?format=json | 200 | 938 | `51cec1e144a5bad8d92238630c0a305e20116e86d34ad95c7c315ed166255b28` | — | — |
| https://api.worldbank.org/v2/country/FRA;DZA;CHN;USA/indicator/NY.GDP.MKTP.CD?format=json&date=2000:2024&per_page=1000&page=1 | 200 | 20,810 | `57676d1b218a15d1789442a2c2cf7a13dc1009f558ada1fa48f3098f128d628f` | 1/1/1000/100 | 2026-10-08 |

- Pagination is fully consumed: 1 page declared and 1 fetched per indicator, and 100 rows = `total` 100
  (4 countries × 25 years). No row was null. Every country's latest year with a value is 2024 for both
  indicators. `sourceid` is 2 (WDI).
- The SP.POP.TOTL `sourceNote` still reads "…de facto definition… The values shown are midyear
  estimates.", so the `midyear` semantic-drift guard passes. The NY.GDP.MKTP.CD note contains the
  guarded phrases.
- `lastupdated` (2026-10-08) is dataset-level metadata and is not used as a per-value publication time.

## Natural Earth 1:110m Admin-0

| URL | HTTP | Bytes | SHA-256 | ETag | Last-Modified | Version |
|---|---|---|---|---|---|---|
| https://naciscdn.org/naturalearth/110m/cultural/ne_110m_admin_0_countries.zip | 200 | 214,976 | `0f243aeac8ac6cf26f0417285b0bd33ac47f1b5bdb719fd3e0df37d03ea37110` | `"374f5381a2ff702d3d79d345a9e5f65c"` | Fri, 13 May 2022 05:22:22 GMT | 5.1.1 |

- 177 features. TYPE counts: Sovereign country 156, Country 10, Indeterminate 3, Disputed 3,
  Dependency 3, Sovereignty 2.
- Match statuses written to `world.display_geometry`: **reviewed_match 4** (FRA:43 France, DZA:82
  Algeria, CHN:139 China, USA:4 United States), **disputed 9** (ATA, CYN, FLK, ISR, KOS, PSX, SAH, SOL,
  TWN), **unmatched 164**. Only the four MVP countries have reviewed aliases, so everything else is
  unmatched by design.
- The "disputed" status comes from `classify_feature` (TYPE in {Disputed, Indeterminate} **or** a
  non-empty `NOTE_BRK`), so it also marks ISR and ATA. This is a display-classification heuristic, not a
  sovereignty statement, and it needs review before any label is shown to users.

## Crossref

| URL | HTTP | Bytes | SHA-256 | Rate-limit headers |
|---|---|---|---|---|
| https://api.crossref.org/works/10.1257/aer.99.3.1053 | 200 | 3,574 | `80facae388a309a3c1663f6ae6634445093effb3b9b8ae7b5b4a7c2f2d5cb514` | x-rate-limit-limit 5, x-rate-limit-interval 1s, x-concurrency-limit 1, x-api-pool public-single |

- Title: "Not All Oil Price Shocks Are Alike: Disentangling Demand and Supply Shocks in the Crude Oil
  Market". *American Economic Review*, published 2009-05-01. Indexed 2026-10-14T13:27:54Z.
  is-referenced-by-count 3,815 (a ranking signal only).
- **Update signals: none.** `updated-by` and `update-to` are absent and the `relation` object is empty.
  The study is stored in `evidence.study`, pinned to the raw artifact hash above.
- No `mailto` was sent because `REVA_HTTP_CONTACT_EMAIL` is empty, so the request went to the public
  pool, not the polite pool.

## Eurostat demo_pjan FR 2023 (through agent G's template; not published)

| Request | HTTP | Bytes | SHA-256 |
|---|---|---|---|
| https://ec.europa.eu/eurostat/api/dissemination/catalogue/toc/txt?lang=en | 200 | 1,968,970 | `cc3d0aee50baaea0b59a6423bb12d680de0d2fb9af71dbfc11f7e07e992a386c` |
| https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/demo_pjan?format=JSON&lang=EN&geo=FR&time=2023 (inspection, then template fetch) | 200 ×2 | 12,165 | `78b10d9dc1e9d381b26553d70d17f8dcf9284dc331fa542afd0e62ce414d15c1` |

- Live discovery found 7,560 datasets in the catalogue. Ranked: demo_pjan, demo_pjangroup,
  demo_pjanbroad, migr_pop1ctz, yth_demo_010. Mapping: exact to `demography.population.jan1_age_sex@1.0.0`,
  **incompatible** with `demography.population.wdi_midyear_total@1.0.0`. Generated manifest SHA-256:
  `7459e93db100977279d18ec1e17e2cde2e0e0da517f83719fce2d7046c179df6`. Template digest:
  `sha256:366d4f30c194646d03db5ec159b5dd5251f42fae4bd74dd75408d8603d896d18`.
- Dimension order `[freq, unit, age, sex, geo, time]` equals the pinned order. Size `[1,1,103,3,1,1]`:
  309 positions, 309 values, 309 status entries, 0 missing cells. Status codes seen: `p` only
  ("provisional"), on all 309 cells. Quality outcome **pass** with no findings, so additivity holds.
- Dataset `updated` / UPDATE_DATA is 2026-09-25T23:00:00+02:00 (dataset-level metadata only).
  OBS_PERIOD_OVERALL_LATEST is 2025.
- FR total (sex T, age TOTAL) on 1 January 2023 is **68,277,210 (p)**. WDI SP.POP.TOTL FRA 2023 (midyear,
  de facto) is **68,372,286**. The two definitions and supports differ, and these numbers are not
  identical. This confirms that the mapping must stay `incompatible`, not exact.

## Drift against the captured CI fixtures (read-only comparison)

| Connector | Compared | Byte-identical | Semantically identical | Changed |
|---|---|---|---|---|
| worldbank.wdi.indicators | 5 | 5 | 5 | 0 |
| un.comtrade.public_exact | 48 | 4 | 48 | 0 |
| naturalearth.admin0_110m | 1 | 1 | 1 | 0 |
| crossref.works.doi | 1 | 1 | 1 | 0 |
| eurostat.demo_pjan.fr | 2 | 2 | 2 | 0 |

Comtrade bodies differ byte-wise only in the server-reported `elapsedTime`.

## Surprises and follow-ups

1. **DZA 2024 Comtrade values exist** (H4, legacyEstimationFlag=6, imports only 5,564 USD). The negative
   demonstration "DZA returns zero rows 2020–2024" in `docs/dev/AGENT_BRIEF.md` is out of date for 2024.
   Missing input for DZA holds for 2020–2023 only.
2. **Comtrade answers HTTP 429 at 1.5 s spacing.** Almost every request needed 2 attempts, and two needed
   3. A diagnostic replay showed the first attempt receiving
   `429 {"message": "Rate limit is exceeded. Try again in 2|3 seconds."}` with `Retry-After: 2|3`.
   SafeHttpClient honoured Retry-After and every item succeeded, but the configured interval is below
   the provider's effective limit. Recommendation for the owner of `src/reva/evidence/runner.py`: set
   `comtradeapi.un.org` `min_interval_seconds` to about 3.0 so the client does not depend on 429 retries
   (with `max_attempts=4`, a burst could otherwise exhaust attempts).
3. **`isReported=false, isAggregate=true` on every Comtrade row** (totals and bilateral, all reporters
   and years). The connector's `provider_derived_aggregate` flag is therefore true everywhere and does
   not discriminate. Each batch logs an info finding "values are provider-derived aggregates". The flag
   needs a better definition, for example based on `legacyEstimationFlag` or partner-total semantics,
   before the UI shows it as a quality signal.
4. **Zero-valued CIF/FOB fields:** CHN exports 2020 and USA exports 2020–21 have `cifvalue=0.0`, and CHN
   imports 2020 and USA imports 2020–21 have `fobvalue=0.0` (not null). The connector keeps these only as flags and uses `primaryValue` with the
   valuation basis inferred from equality, which is correct, but consumers must not read these zeros as
   values.
5. **FRA net weight is null for 2020–2023** (present for 2024), so the net-weight observations are
   Missing with `weight_unusable`. Do not derive barrels.
6. **Byte-level idempotency does not hold for Comtrade** (`elapsedTime`). Each run stores new raw
   artifacts and acquisitions. Revision detection relies on the semantic hash, and no spurious revision
   was created. GC and storage planning should account for this.
7. The Comtrade preview returns no `partnerDesc` in bilateral rows. Partner identity therefore relies on
   the pinned partnerAreas.json, which still matches its pin.
8. Natural Earth's `disputed` heuristic also flags ISR and ATA (see above).
9. The runtime roles' cluster-wide connection limit (10 per role) is shared by every parallel test
   database. One run failed at seeding with "too many connections for role reva_worker". The script now
   retries seeding, but connector runs can still hit the limit when many sessions run at once.

## Raw request record: Comtrade (48 requests, all HTTP 200)

| reporter | flow | period | scope | status | bytes | count | raw SHA-256 | retrieved_at (UTC) |
|---|---|---|---|---|---|---|---|---|
| FRA | M | 2020 | total | 200 | 958 | 1 | `7be3d46c2ec5691df0356fc68a4f3b0b702826f53f14e63d1497fa8fc0490db3` | 23:31:22 |
| FRA | M | 2021 | total | 200 | 958 | 1 | `d7d3612ba3522f6f1f0da25f65a29f28e1bd8d4884132101ebdf3a16a6ad3357` | 23:31:26 |
| FRA | M | 2022 | total | 200 | 958 | 1 | `942c8323830c10319f53e9256b2e6e0363933c2ded45bb3229027c4a5f3f4113` | 23:31:29 |
| FRA | M | 2023 | total | 200 | 958 | 1 | `f409056691155374ca5fb44ac4e29b2f7efb404d5828e096056323cd40506add` | 23:31:33 |
| FRA | M | 2024 | total | 200 | 978 | 1 | `b727e64de9be0d3f9bb15a776c1e0951f788952e89e46070da4153b19fc76111` | 23:31:37 |
| FRA | M | 2023 | bilateral | 200 | 38997 | 43 | `0f0486e5f29c7caf666891f30db1f46cb5734946776662e14fa0df9dfb5b4470` | 23:31:41 |
| FRA | X | 2020 | total | 200 | 952 | 1 | `24c742950b0bae0339ff9fbeed758d94d0c5897f5a0458ab66adbe48b0096852` | 23:31:45 |
| FRA | X | 2021 | total | 200 | 952 | 1 | `0ccbd0df034991c1432ebe0621fa4f1ada37701201c3a8272db8f5f30d7c8e0e` | 23:31:48 |
| FRA | X | 2022 | total | 200 | 950 | 1 | `592f86a0c3105bcd903d2c352f59721a20a55c19f0247e69b759874bb4b4c9d9` | 23:31:52 |
| FRA | X | 2023 | total | 200 | 950 | 1 | `a9bf101961cdf154cbd6e5d10bebe2a6e780478400c74c961836b5643529accd` | 23:31:55 |
| FRA | X | 2024 | total | 200 | 966 | 1 | `421b5cbfe1d44285c75ef79e56c9d68edb13139fa1691f47fee407bbad81dfd6` | 23:31:59 |
| FRA | X | 2023 | bilateral | 200 | 20538 | 23 | `9ac57911cf0449d14ecf1195365f6cb5105798f3f6af3a0915b68ec8a41fb216` | 23:32:03 |
| CHN | M | 2020 | total | 200 | 985 | 1 | `5218c2f3168978d0a55e7af5687374c696d32205f9fbdd45654d99e19015ed02` | 23:32:06 |
| CHN | M | 2021 | total | 200 | 986 | 1 | `944730c2bc8fa8d0ee875f0dfb2a5008481b5569ed51c77db605c55b6d71df49` | 23:32:10 |
| CHN | M | 2022 | total | 200 | 986 | 1 | `f52ec506fbf61f0492e9468ff56fbefa9146dddeed18350ffbcc0a2e7229cae1` | 23:32:14 |
| CHN | M | 2023 | total | 200 | 978 | 1 | `3ea6da2d7014131cd68bd506f7ff9fcf836181aa42a7317b7d5f5cab01495842` | 23:32:18 |
| CHN | M | 2024 | total | 200 | 976 | 1 | `56a2d624156293fbcbcfb6e48030a2d69801f2054c7b14bf00a284cfae8b32c9` | 23:32:21 |
| CHN | M | 2023 | bilateral | 200 | 45746 | 50 | `1f041e3ef31e084e05e1fee0abaf554366a3139c438d50c58b550060b6506e08` | 23:32:25 |
| CHN | X | 2020 | total | 200 | 973 | 1 | `c33420f7a626a66ecaea6d728f0840b18290b884d2227ebf2db5608e11993041` | 23:32:29 |
| CHN | X | 2021 | total | 200 | 976 | 1 | `25dee7d5ae4ddf5d65b6c19da619c983128d966a0d91d41d3a8232e3a129439e` | 23:32:32 |
| CHN | X | 2022 | total | 200 | 976 | 1 | `3e5a98a2dd3a6d2d8159113c4097728bfa6a89ad605aa27e27fa9e57d2575319` | 23:32:36 |
| CHN | X | 2023 | total | 200 | 974 | 1 | `d25897039e5170dbf40a507a4daa575b31b068c2422d78c0d9f3d2856df72ef4` | 23:32:40 |
| CHN | X | 2024 | total | 200 | 975 | 1 | `ac8b0b764ac911e3d04c030ba18d78fce52f02ed5b6fed7f9ac58e4e7cddcbde` | 23:32:47 |
| CHN | X | 2023 | bilateral | 200 | 3722 | 4 | `a527b2a704f493ad7f11fbbf7c4f66bba6fe69e2ee1cdcc4b5a8227c9863cd69` | 23:32:53 |
| USA | M | 2020 | total | 200 | 982 | 1 | `a3614462c9bd0ea7450adf8af4f2669dad97553ad63025644b4930f1bf66343b` | 23:32:57 |
| USA | M | 2021 | total | 200 | 984 | 1 | `e1fe4dfc16b4ffd3bcaedd5bf50bc11bafa61e4c14984fc1f5a8acd311afc477` | 23:33:02 |
| USA | M | 2022 | total | 200 | 995 | 1 | `8d9112b8f158a237afbea6b289d8bc8df0d2877e21005e5ff336f9738ad3f001` | 23:33:04 |
| USA | M | 2023 | total | 200 | 995 | 1 | `8c9014dc928e9fe629ff46836f7e1503cd0781e835ea72eafe9926c9be438c4f` | 23:33:08 |
| USA | M | 2024 | total | 200 | 995 | 1 | `49a5e1e8dfca1347ea291495b6715331d1628deebff7fb44b03705a2efd93318` | 23:33:12 |
| USA | M | 2023 | bilateral | 200 | 42251 | 46 | `d5f7e64ef3d68829a907b861c1c5a1ff7def8e427f01a124e48d9af3cad23759` | 23:33:16 |
| USA | X | 2020 | total | 200 | 982 | 1 | `7ad22b4e4e886be58dfc89becf9cf2afd414d1106da2713fbf35b4610ac8ecea` | 23:33:19 |
| USA | X | 2021 | total | 200 | 982 | 1 | `df0b56f687d8ca159690bc0e3c39fb67ad76f65346b76e1b5d5464280bc1892a` | 23:33:24 |
| USA | X | 2022 | total | 200 | 985 | 1 | `82042c2bdd398b4d7fc25353ce0c255a216bb7ea01c01fc37d0f54115dcd99de` | 23:33:26 |
| USA | X | 2023 | total | 200 | 985 | 1 | `7cf953f8eb3c56af60f569396d631e72fde4f01d3afb08a7731bc0d0e8935a1b` | 23:33:30 |
| USA | X | 2024 | total | 200 | 985 | 1 | `886122511dbf5cbeb908b8d92a118f1d94910e50325eb1d5778cb699d78c6d45` | 23:33:35 |
| USA | X | 2023 | bilateral | 200 | 38576 | 42 | `a51d5a74f2e44f09206433069799402c843eca4ee3fa0855b443e0d449d60e16` | 23:33:38 |
| DZA | M | 2020 | total | 200 | 58 | 0 | `d5b6bec2ca12c01d3de898153073fea5196e40f417bf4617352282e5478ee1bd` | 23:33:41 |
| DZA | M | 2021 | total | 200 | 58 | 0 | `d5b6bec2ca12c01d3de898153073fea5196e40f417bf4617352282e5478ee1bd` | 23:33:45 |
| DZA | M | 2022 | total | 200 | 58 | 0 | `521c3442c33af89e73ffef06a0e06631bf52944cd98740a2e527c04641939e08` | 23:33:48 |
| DZA | M | 2023 | total | 200 | 58 | 0 | `521c3442c33af89e73ffef06a0e06631bf52944cd98740a2e527c04641939e08` | 23:33:52 |
| DZA | M | 2024 | total | 200 | 945 | 1 | `2199e1e9066ac2283841b2be374db4c5f225aeb598dee2964d2cb23cb924f380` | 23:33:56 |
| DZA | M | 2023 | bilateral | 200 | 58 | 0 | `f3680a1dd8407c30d82902892c8744a6b5c616f6f817fbcff9545710ea68f763` | 23:34:00 |
| DZA | X | 2020 | total | 200 | 58 | 0 | `521c3442c33af89e73ffef06a0e06631bf52944cd98740a2e527c04641939e08` | 23:34:03 |
| DZA | X | 2021 | total | 200 | 58 | 0 | `521c3442c33af89e73ffef06a0e06631bf52944cd98740a2e527c04641939e08` | 23:34:07 |
| DZA | X | 2022 | total | 200 | 58 | 0 | `521c3442c33af89e73ffef06a0e06631bf52944cd98740a2e527c04641939e08` | 23:34:10 |
| DZA | X | 2023 | total | 200 | 58 | 0 | `74fb9e013d2918f975bfabd9d7de6ec6c766547ddfcbaed48bfea66fe1eb2e96` | 23:34:14 |
| DZA | X | 2024 | total | 200 | 977 | 1 | `f482bb59d92d3f34e8897f3c971102ed5b9428003b68a3766803dc670917573c` | 23:34:18 |
| DZA | X | 2023 | bilateral | 200 | 58 | 0 | `521c3442c33af89e73ffef06a0e06631bf52944cd98740a2e527c04641939e08` | 23:34:21 |
