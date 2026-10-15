# V3 A-WB — World Bank atlas layer (all economies, all indicators)

Agent A-WB, 2026-10-15. Connector `src/reva/adapters/connectors/atlas_worldbank.py` (atlas source
`atlas.worldbank`, source key `worldbank.atlas`), catalog builder `atlas_worldbank_catalog.py`, catalog
`registry/atlas/catalog/worldbank.yaml` (generated), source `registry/sources/worldbank_atlas.yaml`, budget
request `registry/atlas/budget_requests/a_wb.yaml` (merged by ATLASCORE as `worldbank.atlas: 40`).
All numbers below come from the **real live ingest** of 2026-10-15 (throwaway database
`reva_awb_live_*`, dropped afterwards; throwaway object store deleted).

## Sources (bulk, one request per database)

| database | bulk file (DataBank) | release seen | licence (Series metadata "License Type") |
|---|---|---|---|
| World Development Indicators | WDI_CSV.zip (283 MB) | 2026-10-08 | CC BY-4.0 (1,489), SIPRI terms (6), CC BY 3.0 IGO (3) |
| Worldwide Governance Indicators | WGI_CSV.zip (3.3 MB) | 2026-09-25 | CC BY-4.0 (36) |
| Gender Statistics | Gender_Stats_CSV.zip (171 MB) | 2026-10-14 | CC BY-4.0 (1,170), CC BY 3.0 IGO (153), blank (88) |
| Health Nutrition and Population | HNP_Stats_CSV.zip (121 MB) | 2026-07-01 | CC BY-4.0 (434), blank (41) |
| Education Statistics | EdStats_CSV.zip (39 MB) | **2018-01-22** (bulk file not refreshed; API lists 8,450 series vs 3,665 in the ZIP) | blank |
| Jobs | Jobs_CSV.zip (34 MB) | 2025-07-01 | CC BY-4.0 (165), IEA terms (1) |
| Doing Business (archive) | DB_CSV.zip (1.3 MB) | 2024-02-14 (DB2020 data, 2003–2019) | blank |
| International Debt Statistics | IDS_CSV.zip (11 MB, Windows-1252) | 2025-12-05 | blank |
| Poverty and Inequality Platform | PIP API CSV, release `20260922_2021_01_02_PROD`, lines 3.00/4.20/8.30 (2021 PPP), `fill_gaps=false` | 2026-09-22 | CC BY 4.0 (WB PIP) |

French labels: `api.worldbank.org/v2/fr/source/<id>/indicator` (one request per database). Blank licences are
recorded as "not stated in the series metadata" (World Bank Terms of Use for Datasets default to CC BY 4.0 unless
otherwise noted — not re-verified: the Data Catalog API answered HTTP 429 repeatedly). Non-CC-BY-4.0 series are
tagged `licence:check` in the catalog (SIPRI, IEA, CC BY 3.0 IGO, blank).

Live run: 19 requests, 672,013,604 bytes, 378 s, 0 blocked items, budget `budgets-1.3.3` reserved 19 /
consumed 19. Archived raw 641 MB + Parquet 145 MB = **787 MB** (budget 1.5 GB). Peak RSS 5.3 GB (WDI parse).

## Parsing rules
* Raw bytes archived (`run.archive`, `evidence.acquisition`) before parsing; ZIP members parsed in memory,
  classified by header row (file names differ per database); UTF-8, else Windows-1252 (IDS).
* Empty cell → **no row** (never zero). All 21,955,246 kept values were already strict decimals (0 coerced).
* Geography: `wb` scheme, else `iso3` (WGI territories AIA, COK, GUF, JEY, MTQ, NIU, REU; flag
  `geo_via: iso3`). WB aggregates (crosswalk `excluded_worldbank_aggregates`) **excluded**, counted per code in
  each table's `coverage_note` (WDI 1,539,985 cells; Gender 561,461; HNP 544,718; IDS 420,428; EdStats 287,957;
  Jobs 81,454). Kept with `iso3 = null` and listed: TWN (WGI, DB, PIP), ANT (WGI, dissolved), 22 Doing Business
  cities (CHN_BEI, USA_NY, …).
* Flags: provider `footnote`, `series_year_note`, `period_after_vintage` (year later than the data file's
  own date: EdStats projections 2019–2100, IDS projected debt service to 2032), PIP survey descriptors.
* Indicators: WB code; IDS folds `|counterpart=WLD`; PIP folds `|povline=…|level=…|welfare=…`.
* WDI split by WB topic into `wdi_econ` / `wdi_social` (≤ 5 M rows per table).

## Coverage (live)

Totals: **10 atlas tables, 21,955,246 non-null cells, 6,886 WB series codes + 209 PIP indicators, 7,238 catalog
concepts, 224 countries/territories** (217 WB economies + 7 via ISO3), years 1960–2100 (87 distinct; 2026–2100 only
projections), 36,756 cells on unmapped geographies.

| dataset | cells | indicators | countries | years | unmapped kept | raw ZIP (bytes) |
|---|---:|---:|---:|---|---|---:|
| wb.wdi_econ | 3,634,069 | 736 | 217 | 1960–2025 | – | 283,089,754 |
| wb.wdi_social | 3,871,680 | 761 | 217 | 1960–2025 | – | 283,089,754 |
| wb.gender | 2,768,043 | 1,358 | 217 | 1960–2025 | – | 171,479,946 |
| wb.hnp | 3,478,037 | 462 | 217 | 1960–2025 | – | 120,890,865 |
| wb.edstats | 4,794,244 | 3,665 | 217 | 1970–2100 | – | 38,943,514 |
| wb.ids | 2,169,501 | 576 | 120 | 1970–2032 | – | 11,142,704 |
| wb.jobs | 581,112 | 165 | 217 | 1990–2016 | – | 34,157,783 |
| wb.wgi | 202,092 | 36 | 214 | 1996–2025 | ANT, TWN | 3,336,633 |
| wb.doingbusiness | 366,211 | 194 | 190 | 2003–2019 | 23 (TWN + 22 DB cities) | 1,325,081 |
| wb.pip | 90,257 | 209 | 171 | 1963–2025 | TWN | 3,938,334 |

Per REVA domain (catalog `subdomain`; theme in `theme`):

| REVA domain (subdomain) | concepts | cells | countries | cells 2015–2024 |
|---|---:|---:|---:|---:|
| population | 401 | 5,641,783 | 217 | 725,555 |
| education | 3,576 | 4,499,625 | 213 | 376,176 |
| finance | 659 | 2,639,381 | 210 | 537,537 |
| health | 345 | 2,459,869 | 217 | 680,192 |
| economy | 284 | 2,007,482 | 217 | 416,990 |
| labour | 174 | 1,256,016 | 215 | 331,038 |
| trade | 172 | 1,170,629 | 215 | 265,013 |
| environment | 73 | 516,254 | 216 | 111,533 |
| business | 299 | 447,418 | 198 | 171,523 |
| governance | 111 | 417,315 | 224 | 159,535 |
| gender | 818 | 207,266 | 216 | 96,281 |
| agriculture | 24 | 184,599 | 216 | 33,956 |
| energy | 27 | 160,982 | 217 | 45,128 |
| infrastructure | 29 | 146,761 | 216 | 34,625 |
| poverty | 235 | 144,470 | 177 | 44,125 |
| science | 8 | 28,541 | 207 | 10,058 |
| geography | 3 | 26,855 | 216 | 4,526 |

Catalog labels: French from the World Bank for 1,528 concepts, written by A-WB (flagged `fr_label:agent_a_wb`)
for 216 (7 key WDI/WGI labels the WB leaves untranslated + 209 PIP), **missing for 5,494** (`fr_label:missing`; `labels.fr` repeats the
English text because the schema requires it — never machine-translated).

## Countries with the most gaps
Fewest WDI cells 2015–2024 (max 10856):
MAF 1237 (11%), IMN 1728 (15%), CHI 1809 (16%), MNP 1932 (17%), GIB 2129 (19%), ASM 2386 (22%), LIE 2450 (22%), VGB 2455 (22%), SXM 2672 (24%), FRO 2687 (24%), MCO 2753 (25%), VIR 2797 (25%), GUM 2915 (26%), GRL 3260 (30%), CUW 3274 (30%), TCA 3547 (32%), NCL 3646 (33%), PYF 3764 (34%), CYM 3932 (36%), PRK 4027 (37%), AND 4408 (40%), ABW 4425 (40%), PRI 4551 (41%), NRU 4679 (43%), BMU 4738 (43%)

The 33 crosswalk entities that are not World Bank economies have no WDI data; 7 of them get WGI only. No WB data at all (26): ALA (Åland Islands), ATA (Antarctica), ATF (French Southern Territories), BES (Bonaire, Sint Eustatius and Saba), BLM (Saint Barthélemy), BVT (Bouvet Island), CCK (Cocos (Keeling) Islands), CXR (Christmas Island), ESH (Western Sahara), FLK (Falkland Islands (Malvinas)), GGY (Guernsey), GLP (Guadeloupe), HMD (Heard Island and McDonald Islands), IOT (British Indian Ocean Territory), MSR (Montserrat), MYT (Mayotte), NFK (Norfolk Island), PCN (Pitcairn), SGS (South Georgia and the South Sandwich Islands), SHN (Saint Helena), SJM (Svalbard and Jan Mayen Islands), SPM (Saint Pierre and Miquelon), TKL (Tokelau), UMI (United States Minor Outlying Islands), VAT (Holy See), WLF (Wallis and Futuna Islands)

## Key concepts named by the owner — gaps for other agents
Countries = of the 217 WB economies (the 33 non-WB entities above are missing everywhere and are not repeated).

| concept (WDI code) | countries any year | countries 2019–2024 | best recent year (countries) | WB economies without a 2019–2024 value (last year if older) |
|---|---:|---:|---|---|
| population: SP.POP.TOTL | 217 | 217 | 2025 (217) | none |
| density: EN.POP.DNST | 216 | 216 | 2024 (214) | XKX |
| area: AG.LND.TOTL.K2 | 216 | 216 | 2024 (214) | XKX |
| area: AG.SRF.TOTL.K2 | 216 | 216 | 2024 (213) | XKX |
| unemployment: SL.UEM.TOTL.ZS | 187 | 187 | 2025 (182) | ABW, AND, ASM, ATG, BMU, CUW, CYM, DMA, FRO, FSM, GIB, GRD, GRL, IMN, KIR, KNA, LIE, MAF, MCO, MHL, MNP, NRU, PLW, SMR, SXM, SYC, TCA, TUV, VGB, XKX |
| unemployment: SL.UEM.TOTL.NE.ZS | 209 | 170 | 2025 (87) | ABW@2011, AND, ASM@2010, BHR@2015, BMU@2014, CAF, CHI, COG@2012, CUB@2018, CYM@2015, DJI@2025, DMA@2001, DZA@2017, ERI, FRO@2013, FSM@2014, GIB@1991, GRL@2015, GUM@2016, HTI@2012, IMN@2016, KNA@2001, KWT@2016, LBR@2017, LBY@2012, LIE@2017, MAF, MCO@2016, MNP@2010, NIC@2018, NPL@2017, PRK, PYF@2012, SLB@2013, SLE@2018, SSD@2008, STP@2017, SUR@2016, SXM, SYR@2011, TCA@2008, TJK@2016, TKM@2010, VCT@2008, VGB@1991, VIR, YEM@2014 |
| wage_proxies: NY.GNP.PCAP.CD | 207 | 202 | 2025 (182) | ASM, CHI, ERI@2011, GIB, GRL@2007, GUM, LIE@2009, MAF, MCO, MNP, PRK, SSD@2015, VGB, VIR, YEM@2018 |
| wage_proxies: NY.GNP.PCAP.PP.CD | 202 | 197 | 2025 (181) | ASM, CHI, CUB, ERI@2011, GIB, GRL@2007, GUM, IMN, LIE, MAF, MCO, MNP, NCL, PRK, PYF, SSD@2015, VEN@2011, VGB, VIR, YEM@2013 |
| wage_proxies: NY.ADJ.NNTY.PC.CD | 185 | 177 | 2021 (169) | AND, ASM, ATG, BMU, CHI, CUW, CYM, ERI@2011, FRO, GBR@2018, GIB, GRD, GRL, GUM, HKG@1980, IMN, KNA, LIE, MAC, MAF, MCO, MHL, MLT, MNP, NCL@2000, NRU, PLW, PRI, PRK, PSE, PYF@2000, SMR, SSD@2015, SXM, TUV, VEN@2014, VGB, VIR, XKX, YEM@2018 |
| wage_proxies: NE.CON.PRVT.PC.KD | 180 | 169 | 2025 (129) | ABW@2015, AFG, AND, ATG, AZE@2015, BRB, CHI, CUW@2015, CYM, DMA, ERI, FJI@2015, FRO@2015, FSM, GIB, GRD, GUY, IMN, JAM, JOR, KNA, LAO@2016, LBR, LCA, LIE, MAF, MCO, MMR, MWI, NCL@2015, NGA, NRU, PNG, PRK, PYF@2015, SSD, SUR, SXM, TCA, TKM, TTO, TUV, URY@2015, VCT, VEN, VGB, YEM@2018, ZMB@2015 |
| wage_proxies: SL.EMP.WORK.ZS | 187 | 187 | 2025 (182) | ABW, AND, ASM, ATG, BMU, CUW, CYM, DMA, FRO, FSM, GIB, GRD, GRL, IMN, KIR, KNA, LIE, MAF, MCO, MHL, MNP, NRU, PLW, SMR, SXM, SYC, TCA, TUV, VGB, XKX |
| gdp: NY.GDP.MKTP.CD | 214 | 211 | 2025 (186) | ERI@2011, GIB, PRK, SSD@2015, VGB, YEM@2018 |
| gdp: NY.GDP.PCAP.CD | 214 | 211 | 2025 (186) | ERI@2011, GIB, PRK, SSD@2015, VGB, YEM@2018 |
| gdp: NY.GDP.MKTP.PP.CD | 203 | 199 | 2025 (185) | ASM, CHI, CUB, ERI@2011, GIB, GUM, IMN, LIE, MAF, MCO, MNP, NCL, PRK, PYF, SSD@2015, VEN@2011, VGB, YEM@2013 |
| trade_balance: NE.RSB.GNFS.CD | 194 | 184 | 2025 (138) | AND, BDI, BRB, CHI, CUW@2018, DMA, ERI@2011, GIB, GRD, GUY@2005, IMN, JAM, JOR@2007, KNA, LAO@2016, LBR, LCA, LIE, MAF, MCO, MMR, NGA@1960, PNG@2004, PRK, SSD@2015, SUR@2010, SXM, TCA, TTO, TUV, VCT, VGB, YEM@2018 |
| trade_balance: NE.RSB.GNFS.ZS | 194 | 184 | 2025 (138) | AND, BDI, BRB, CHI, CUW@2018, DMA, ERI@2011, GIB, GRD, GUY@2005, IMN, JAM, JOR@2007, KNA, LAO@2016, LBR, LCA, LIE, MAF, MCO, MMR, NGA@1960, PNG@2004, PRK, SSD@2015, SUR@2010, SXM, TCA, TTO, TUV, VCT, VGB, YEM@2018 |
| trade_balance: BN.GSR.GNFS.CD | 200 | 186 | 2025 (138) | ASM, BRB@2017, CAF@1994, CHI, CUB, ERI@2000, FRO@2011, FSM@2014, GAB@2015, GIB, GNQ@1996, GRL, GUM, IMN, IRN@2000, LIE, MAF, MCO, MNP, NCL@2016, PRI, PRK, PYF@2016, SOM, SYR@2010, TCA@2018, TCD@1994, TKM, VGB, VIR, YEM@2016 |
| trade_balance: BN.CAB.XOKA.CD | 200 | 186 | 2025 (138) | ASM, BRB@2017, CAF@1994, CHI, CUB, ERI@2000, FRO@2011, FSM@2014, GAB@2015, GIB, GNQ@1996, GRL, GUM, IMN, IRN@2000, LIE, MAF, MCO, MNP, NCL@2016, PRI, PRK, PYF@2016, SOM, SYR@2010, TCA@2018, TCD@1994, TKM, VGB, VIR, YEM@2016 |
| trade_balance: NE.EXP.GNFS.CD | 194 | 184 | 2025 (138) | AND, BDI, BRB, CHI, CUW@2018, DMA, ERI@2011, GIB, GRD, GUY@2005, IMN, JAM, JOR@2007, KNA, LAO@2016, LBR, LCA, LIE, MAF, MCO, MMR, NGA@1960, PNG@2004, PRK, SSD@2015, SUR@2010, SXM, TCA, TTO, TUV, VCT, VGB, YEM@2018 |
| trade_balance: NE.IMP.GNFS.CD | 194 | 184 | 2025 (138) | AND, BDI, BRB, CHI, CUW@2018, DMA, ERI@2011, GIB, GRD, GUY@2005, IMN, JAM, JOR@2007, KNA, LAO@2016, LBR, LCA, LIE, MAF, MCO, MMR, NGA@1960, PNG@2004, PRK, SSD@2015, SUR@2010, SXM, TCA, TTO, TUV, VCT, VGB, YEM@2018 |
| PIP mean (national) | 170 | 125 | – | see PIP survey years |

**Not in any World Bank bulk file:**
* **Average wage** — no series. Proxies here: GNI per capita (Atlas, PPP), adjusted net national income per
  capita, household consumption per capita, PIP mean/median welfare per day (survey years only: 170 countries,
  125 with a 2019–2024 survey), share of wage and salaried workers. → A-ILO (ILOSTAT mean nominal/real monthly
  earnings of employees), OECD average annual wages for OECD members.
* **Minimum wage** — no series (DB_CSV.zip does not include the "Employing workers" minimum-wage data). → A-ILO
  (ILOSTAT statutory nominal gross monthly minimum wage), A-LAW (legal texts).
* **Unemployment** for small states (ILO modelled estimates cover 187 economies) → ILOSTAT national series,
  national statistical offices, IMF WEO (LUR).
* **Trade balance** gaps (Caribbean, micro-states, PRK, recent conflict economies) → IMF BOP/WEO (A-IMF),
  UN Comtrade goods totals, UNSD national accounts (AMA).
* **GDP** for PRK, GIB, VGB, ERI, SSD, YEM after the dates shown → UNSD AMA, IMF WEO, Maddison (history).
* **Area/density for Kosovo (XKX)** → Natural Earth / A-GEO.
* The 26 entities with no WB data at all (Western Sahara, Holy See, Guernsey, French overseas departments
  GLP/MYT, …) → UN WPP (population), A-GEO (area), Factbook.

## Caveats
* Doing Business was discontinued in 2021 after data irregularities; kept as an archive, every concept says so.
* EdStats bulk is the 2018 release; current UIS data (A-HEALTHEDU) should rank before it.
* Many WDI series are compiled from other organisations (UN WPP, ILO modelled estimates, IMF, WHO, UIS): the
  upstream organisation is in each concept's notes; they are not independent evidence of those sources.
* Same series code in two databases under different names is not merged (separate concept `wb.<db>.<code>`).

## Requests to other owners
* **ATLASCORE**: load catalog YAML with `yaml.CSafeLoader` — `worldbank.yaml` is 9.4 MB (7,238 concepts) and
  a strict `load_catalog()` takes ~19 s with the pure-Python loader (parse ~5 s with libyaml); `labels.fr`
  optional (or a `fr_missing` marker) so untranslated labels need not repeat English.
* **Lead**: seed registration of `atlas.worldbank` via `register_atlas_connector_versions`; CLI alias
  `reva atlas ingest atlas.worldbank`.

## How to run
* Offline: `uv run pytest tests/unit/test_atlas_worldbank.py tests/integration/test_atlas_worldbank_ingest.py`
  (recorded slices in `tests/fixtures/source/atlas.worldbank/`, 360 KB).
* Live (budget-metered): `uv run python -m reva.atlas.ingest atlas.worldbank [--dataset wgi]`; live test
  `pytest -m live tests/integration/test_atlas_worldbank_live.py` (WGI only).
* Catalog: `uv run python -m reva.adapters.connectors.atlas_worldbank_catalog` (rebuilds from the archived raw).
