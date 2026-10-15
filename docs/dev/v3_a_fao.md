# V3 atlas — food, agriculture, land, forests, water, rural life (agent A-FAO)

Status: connector, catalog (`food_agriculture`, `water`), geo aliases, budget request, fixtures and tests are
done. One live ingest ran on 2026-10-15 through the single atlas entry point (`run_atlas_ingest`, budget-metered)
into a throwaway database (`reva_afao_live_0c3c9f4d`, dropped afterwards). It made 20 requests, downloaded
543.8 MB, archived 20 raw artifacts and registered 19 atlas tables (13.1 M cells, 85.6 MB of Parquet) in 4 min 40 s
with 0 failures. The dev database `reva` was never touched. Nothing is committed.

## 1. Sources (researched 2026-10-15)

| Option | Status | Used? |
|---|---|---|
| **FAOSTAT bulk downloads** `https://bulks-faostat.fao.org/production/<stem>_E_All_Data_(Normalized).zip`, with listing `datasets_E.json` (code, file location, size, `DateUpdate`) | Open, no key, one zip per domain (0.1–410 MB). Each zip holds FAO's own code lists (`*_AreaCodes.csv` with M49, `*_Flags.csv` with flag meanings, items, elements). | **Yes: 19 domains** |
| FAOSTAT REST API `faostatservices.fao.org/api/v1` | Now returns HTTP 401 "Missing Authorization Header" (token required). | No |
| FAO AQUASTAT dissemination system | The FAO data catalog lists it as **CC BY-NC-SA 4.0** (non-commercial), which overrides the CC BY 4.0 default of the FAO database terms. Access is a BigQuery "smart-csv" endpoint. | **No.** SDG 6.4.1 and 6.4.2 (water stress, water-use efficiency) come from the FAOSTAT SDG domain (CC BY 4.0) instead. |
| FAO FRA (forest resources) | The FRA forest series are in FAOSTAT Land Use (forest land, primary and planted forest, carbon stock) and in SDG 15.1.1 / 15.2.1. | Via FAOSTAT RL and SDGB |
| FishStat (`fao.org/fishery/static/Data/GlobalProduction_2026.1.0.zip`, CC BY 4.0 per its ReadMe) | Per country × species × fishing area × source only. Country totals would have to be summed by REVA. | No (see gaps). Fish and seafood supply and production come from the FBS group 2960 and FBS fish detail. |
| FAOSTAT Trade Indices (68 MB → 4.2 GB CSV) and Detailed Trade Matrix (411 MB) | Index and partner detail are out of scope. | No. Agricultural trade totals come from TCL, filtered. |

**Licence.** The FAO Statistical Database terms of use (https://www.fao.org/contact-us/terms/db-terms-of-use/en)
say: "Unless specified otherwise in their metadata or webpage, all datasets disseminated through FAO corporate
statistical databases … are licensed under the Creative Commons Attribution-4.0 International licence (CC BY
4.0)". Every table's rights carry the licence `CC-BY-4.0`, the attribution "FAO. FAOSTAT: <domain>. Licence:
CC-BY-4.0." and the third-party series it redistributes.

**Third-party series stay FAO-labelled and flagged.** FAO flags redistributed values `X` ("value / figure from
external organization"). Each table's coverage note names the original producer:

* OA: UN DESA WPP. A-UN ingests it at the source.
* OEA and OER: ILO / ILOSTAT. A-ILO ingests it at the source.
* FS: WHO/UNICEF JMP, child malnutrition, anaemia, obesity, and World Bank GDP per capita PPP.
* MK: UNSD national accounts.
* GT: the UNFCCC inventories (`src=3051`).
* CP: national CPIs.

## 2. What was built

* `src/reva/adapters/connectors/atlas_fao.py` declares connector `atlas.fao` (`atlas_source()` → `AtlasSourceSpec`,
  source key `fao.faostat`, atlas source `fao`, host `bulks-faostat.fao.org`, 21 planned requests, ≤ 400 MB per
  response) and implements `async atlas_ingest(run)`. The module creates no HTTP client; every request goes
  through `run.get`, and invariant 30 is clean for this file.
  * The domain listing is fetched first and archived. The `DateUpdate` of each domain is recorded in every
    coverage note and drives the projection rule.
  * Each domain zip is fetched once, archived as an `evidence.acquisition` and parsed by `parse_domain`:
    * the data member is inflated as a stream (32 MB blocks of complete lines, CRC checked, ≤ 6 GB per member);
    * each block is read by polars as strings and filtered to the domain's bounded items and elements;
    * the row count is checked against the line count, so embedded newlines are refused;
    * cp1252 members (FBSH) are detected and decoded.
  * One atlas table is registered per domain (`run.register`, `raw_sha256s = [zip, listing]`).
* `registry/sources/atlas_fao.yaml` (source `fao.faostat`).
* `registry/atlas/geo_aliases/a_fao.yaml` (scheme `fao_m49`).
* `registry/atlas/budget_requests/a_fao.yaml` (45 requests/day, 600 MB/day). ATLASCORE has already merged it into
  `budgets.yaml` 1.3.3.
* `registry/atlas/catalog/food_agriculture.yaml` (96 concepts) and `registry/atlas/catalog/water.yaml` (8 concepts).
* `tests/fixtures/source/atlas.fao/`: the listing (whole file) plus 19 excerpt zips, 0.5 MB in total. Code-list
  members are verbatim. Data rows cover 12 areas: France, Algeria, China mainland, the "China" aggregate, USSR,
  Sudan (former), Sudan, World, Channel Islands, Taiwan, Ethiopia PDR and Ethiopia. A few years are kept, plus
  out-of-scope rows, missing-flag rows and text-value rows. Each manifest entry records the live sha256 and size.
* Tests:
  * `tests/unit/test_atlas_fao.py`: 34 tests, offline (every domain parses into a valid atlas table; mapping;
    missing flags; projections; refusals; catalog; `atlas_ingest` with a fake run).
  * `tests/integration/test_atlas_fao_ingest.py`: 1 test, `db`. Fixture-mode `run_atlas_ingest` registers 19
    tables, reads them back, checks that a re-run is a no-op and that a single-dataset run touches one table.

### Atlas tables (`source = fao`; live, 2026-10-15)

| dataset | FAOSTAT domain (DateUpdate) | scope | rows | countries | years | zip |
|---|---|---|---|---|---|---|
| `fbs` | FBS Food Balances 2010- (2025-10-28) | 50 items (totals, groups, fish detail, staples) × 11 elements (kcal/protein/fat per capita per day, kg/cap/yr, production, trade, domestic supply, food, feed, losses) | 1,198,691 | 177 | 2010–2023 | 55 MB |
| `fbsh` | FBSH Food Balances −2013, old methodology (2023-03-10) | same | 4,156,860 | 173 | 1961–2013 | 72 MB |
| `fs` | FS Suite of Food Security Indicators (2026-07-21) | all 128 series | 247,132 | 202 | 2000–2025 | 2.2 MB |
| `qcl` | QCL Production, crops and livestock (2025-12-31) | 75 items (key crops, meat, milk, eggs, live animals, FAO aggregates) × all elements | 1,855,276 | 201 | 1961–2024 | 34 MB |
| `qv` | QV Value of Agricultural Production (2026-05-11) | 15 aggregates × 3 elements | 388,172 | 202 | 1961–2024 | 30 MB |
| `pp` | PP Producer Prices (2026-10-09) | QCL key items, LCU/t, USD/t, index; annual values only | 346,920 | 176 | 1991–2025 | 12 MB |
| `cp` | CP Consumer Price Indices (2026-09-03) | all, monthly periods | 261,810 | 206 | 2000–2026 | 2.5 MB |
| `tcl` | TCL Trade, crops and livestock products (2026-07-24) | 24 items (total 1882, food excl. fish 1982, trade groups, 7 commodities), import/export value | 659,393 | 201 | 1961–2024 | 274 MB |
| `tcli` | TCLI Trade indicators (2026-03-05) | all | 116,294 | 183 | 2010–2024 | 0.9 MB |
| `rl` | RL Land Use (2026-10-02) | all | 421,765 | 236 | 1961–2025 | 3 MB |
| `gt` | GT Emissions totals (2025-10-28) | all items × CO2eq (AR5), CH4, N2O, CO2; FAO tier 1 and UNFCCC | 1,429,965 | 232 | 1961–2050 | 21 MB |
| `fo` | FO Forestry production and trade (2026-01-09) | 12 items (roundwood, wood fuel, sawnwood, panels, pulp, paper, charcoal, trade total) | 511,741 | 229 | 1961–2024 | 18 MB |
| `rfn` | RFN Fertilizers by nutrient (2026-07-30) | all | 256,244 | 202 | 1961–2024 | 2 MB |
| `rp` | RP Pesticides use (2026-07-30) | all | 104,633 | 210 | 1990–2024 | 0.7 MB |
| `oa` | OA Annual population (2026-03-09) | all (FAO-distributed UN figures) | 168,405 | 234 | 1950–2100 | 1.6 MB |
| `oea` | OEA Employment indicators, agriculture and agrifood systems (2026-06-17) | all (ILO data + FAO model) | 256,370 | 227 | 1947–2025 | 2.4 MB |
| `oer` | OER Employment indicators, rural (2026-06-17) | all (ILO) | 113,187 | 160 | 1952–2025 | 1 MB |
| `mk` | MK Macro indicators (2026-03-27) | 11 items × 7 elements (value, share of GDP, per capita, growth) | 318,893 | 210 | 1970–2024 | 7 MB |
| `sdgb` | SDGB SDG indicators, FAO custodian (2026-09-29) | all (2.1.1, 2.1.2, 2.4.1, 2.5, 2.a.1, 2.c.1, 5.a, 6.4.1, 6.4.2, 12.3.1, 14.x, 15.x) | 302,526 | 248 | 1974–2026 | 5 MB |

Indicator keys are `<FAOSTAT item code>|<element code>`. Where FAOSTAT has more dimensions they are folded in:

* OEA/OER: `|sex=<1 total, 2 male, 3 female>|src=<source code>`.
* GT: `|src=3050` (FAO tier 1) or `|src=3051` (UNFCCC).
* TCLI: `<item>|<indicator>`.
* SDGB: item codes such as `24002-TTT` or `24027-_T`.

The FAOSTAT names of every indicator are in the parse report (`DomainReport.indicators`) and in the catalog
definitions.

### Honesty rules as implemented

* **Missing never becomes a value.** Rows flagged `L`, `M`, `O` or `Q` produce no cell. FAO defines these as
  "Missing value; data exist but were not collected", "data cannot exist", "Missing value" and "suppressed".
  Rows with an empty value produce no cell either. Live counts:
  * QCL: 18,105 `M` rows. Some carry `0` in the file and are still not kept as zeros.
  * SDGB: `M` 93,523, `O` 63,903, `Q` 14,142, plus 4,210 empty values.
  * FS: `O` 29,647, `Q` 10,947, `L` 9.
  * CP, RL, RP, OEA and RFN: a few `L` or `M` rows each.
* **Reported zeros stay.** A zero that FAO publishes with a real flag (for example `A`) is a reported value and
  is kept verbatim.
* **Estimated and imputed rows are kept and flagged.** `flags = {"flag", "flag_meaning", "note"?,
  "estimate_kind"?}`. The flag letter is verbatim and its meaning comes from the zip's own `*_Flags.csv`.
  `estimate_kind` is `estimated` (E), `imputed` (I) or `forecast` (F). Example: every FBS cell is E or I.
* **Projections are labelled.** A cell whose period starts after the domain's FAOSTAT `DateUpdate` gets
  `estimate_kind = "projection"`, because it cannot be an observation. This covers OA years up to 2100 and
  GT years after the release; FAO's own `F` (GT projections to 2030/2050) stays `forecast`.
  * OA additionally carries `series_note`: FAO's item "Population - Est. & Proj." does not mark the UN
    estimate/projection boundary per cell, so OA years between the UN base year and the FAO release are
    *not* marked. The catalog note for `food_agriculture.rural_population_fao` says so.
* **Text values stay text.** Undernourishment below the reporting threshold is published as `<2.5`, `<0.1`,
  `<0.5` or `<100` and goes to `value_text`, never to a number. There are 16,537 such cells in FS and 3,799 in
  SDGB; for example Algeria and France PoU 2023 = `<2.5`.
* **Values are verbatim.** Strings like `3489.550000` are not rescaled. Units are as declared by FAO (`1000 ha`,
  `1000 USD`, `kcal/cap/d`, `t`, `An`). The catalog spells them out ("thousand hectares"). FAOSTAT's empty units
  get a rule only where the name states one (`index (2015 = 100)` for CPI items, `index (2014-2016 = 100)` for
  the producer price index); otherwise they are "not stated by FAOSTAT (see indicator)".
* **Period semantics.**
  * FAOSTAT 3-year averages (`2000-2002`) become 3-year periods `[2000, 2003)`. They appear in per-source views
    only, not in the calendar-year "best available" view.
  * SDG 2.1.1 and 2.1.2 values are reported by FAO for a single year, with the note "3-year average of the
    period …" kept per cell.
  * CP months become month periods.
* **Lineage.** Each cell has a selector `{"file": <CSV member>, "row": <line>, "zip": <bulk file>}`, so every
  cell points at its line in the archived zip.

### Geography

The connector does not guess. Every FAOSTAT row carries its FAO Area Code (kept as `provider_geo`) and its M49
code. The M49 code is checked against the zip's `*_AreaCodes.csv`, which is FAO's own country-definitions file
and is archived inside each zip; a disagreement refuses the parse. The M49 code is then mapped through scheme
`fao_m49`, which is the crosswalk M49 plus one evidenced alias: `830` Channel Islands → `CHI`.

* **Mapped:** 248 of the 250 crosswalk entities have at least one FAO cell. The two without any are XKX Kosovo
  (FAOSTAT does not publish Kosovo separately) and ATA Antarctica.
* **Unmapped and listed in every manifest:**
  * FAO aggregates: World, regions, income groups, LDC/LLDC/SIDS/LIFDC/NFIDC, EU, OECD, Annex I, "excluding
    intra-trade" trade groups, FAO fishing areas.
  * 351 "China": FAO's aggregate of the mainland, Hong Kong SAR, Macao SAR and Taiwan. China mainland (41) maps
    to CHN.
  * 214 "China, Taiwan Province of": not a crosswalk entity.
  * 285 Sark (SDGB only).
* **Historical areas, for KGHIST historical-state entities.** Cells are kept unmapped with their FAO code, M49
  code and periods as present in the live tables. Population (OA) starts in 1950 for all of them.

| FAO code | name | M49 | years in FAOSTAT (any domain) | domains |
|---|---|---|---|---|
| 228 | USSR | 810 | 1950–1991 | FBSH, QCL, QV, RL, FO, GT, MK, OA, RFN, RP, TCL |
| 248 | Yugoslav SFR | 890 | 1950–1991 | same |
| 51 | Czechoslovakia | 200 | 1950–1992 | same + PP |
| 62 | Ethiopia PDR | 230 | 1950–1992 | same + PP |
| 206 | Sudan (former) | 736 | 1950–2011 | same + PP, TCLI |
| 15 | Belgium-Luxembourg | 058 | 1950–1999 | FBSH, QCL, QV, RL, FO, GT, OA, PP, RFN, RP, TCL |
| 186 | Serbia and Montenegro | 891 | 1992–2005 | FBSH, QCL, QV, RL, FO, GT, OA, PP, RFN, RP, TCL |
| 151 | Netherlands Antilles (former) | 530 | 1950–2015 | FBSH, RL, FO, GT, MK, OA, OEA, CP, RP |
| 164 | Pacific Islands Trust Territory | 582 | 1950–1990 | RL, FO, GT, OA, RP |
| 246 / 247 | Yemen Ar Rp / Yemen Dem | 886 / 720 | 1970–1988 | MK |

## 3. Coverage by key concept (live tables; calendar-year cells; best-available view)

| concept | countries | latest year (mode) |
|---|---|---|
| `food_supply_kcal` (FBS, then FBSH) | 187 | 2023 |
| `protein_supply`, `fat_supply`, food supply quantities (cereals, meat, milk, fish, vegetables, fruits) | 187 | 2023 |
| `fish_seafood_production` (FBS 2960) | 187 | 2023 |
| `undernourishment_prevalence` (SDG 2.1.1) | 168 | 2024 |
| `food_insecurity_moderate_severe` / `_severe` (FIES, SDG 2.1.2) | 157 / 155 | 2024 |
| `dietary_energy_supply_pou` (FS 220001) | 159 | 2023 |
| `production.cereals` / `meat` / `milk` / `eggs` / `fruit` / `vegetables` | 184 / 200 / 190 / 196 / 198 / 197 | 2024 |
| `production.wheat` / `rice` / `maize` / `coffee` / `cocoa` | 124 / 141 / 174 / 83 / 62 | 2024 |
| `livestock.cattle` / `chickens` | 195 / 197 | 2024 |
| `gross_production_value` (constant I$) | 202 | 2024 |
| `agri_exports_value` / `agri_imports_value` (TCL 1882) | 201 | 2024 |
| `land.agricultural_land_share` / `cropland` / `arable_land` | 225 / 222 / 219 | 2024 |
| `land.forest_land` / `forest_share` (FRA via RL, then SDG 15.1.1) | 236 | 2025 |
| `land.irrigation_equipped` / `irrigated_share_agri` | 191 / 126 | 2024 |
| `fertilizer_use_per_ha.nitrogen`, `pesticide_use_per_ha` | 200 / 196 | 2024 |
| `emissions_co2eq.agrifood_systems` (FAO tier 1) / `ipcc_agriculture_unfccc` | 232 / 184 | 2023 / 2020 |
| `forestry_production.roundwood` | 205 | 2024 |
| `agri_value_added_share_gdp` (MK) | 207 | 2024 |
| `employment_agriculture_share` (ILO modelled via FAO) | 188 | 2025 |
| `water.stress.total` (SDG 6.4.2) / `water.use_efficiency.total` (SDG 6.4.1) | 182 / 170 | 2023 |
| `fish_stocks_sustainable_share` (SDG 14.4.1) | 96 | 2023 |

Example cells (live, verbatim):

* Food supply 2023: France 3489.55, Algeria 3396.01, USA 3947.33 kcal/cap/day (all FBS flag E). Algeria 1961
  (FBSH): 1619.
* Haiti PoU 2023: 52.6 % (E, 3-year average 2022–2024).
* Water stress 2022: Algeria 144.81 % (A).
* Agricultural exports 2024: Brazil 141.9 bn USD. Agricultural imports 2024: Algeria 11.5 bn USD.

## 4. Gaps and which sources fill them

* **Food balances (FBS 2010-) lack 73 crosswalk entities.**
  * **Japan (FAO 110) is absent from the current FBS bulk file**, as are Sudan, South Sudan, Chad, Mali,
    Burundi, Benin, Togo, Eritrea, Somalia, Equatorial Guinea, Central African Republic, Palestine, Brunei,
    Singapore and Dominica. Most of the rest are small territories.
  * FBSH (−2013) fills Japan, Sudan (former), Chad, Mali, Benin, Togo and other countries for 1961–2013. The
    catalog's `source_order` uses FBSH only where FBS has no cell for that year, so these countries show no
    value after 2013.
  * The FS series `dietary_energy_supply_pou` (220001) covers some of them for 2001–2024. It is a separate
    concept (a different definition).
  * No other open source publishes kcal/capita for Japan after 2013 in this atlas. OWID's "daily caloric
    supply" is FAO's own series.
* **Undernourishment (SDG 2.1.1) lacks 82 entities.** These are small islands and territories, high-income
  countries where FAO does not publish below-threshold values, Yemen, Slovakia, Bahrain and Qatar. The World
  Bank `SN.ITK.DEFC.ZS` is the same FAO series, so it adds no country. The FS 3-year series (210041) is the same
  estimate.
* **Water resources and withdrawals** (total renewable water resources, per capita, withdrawals by sector) come
  from AQUASTAT, which is CC BY-NC-SA 4.0 and not ingested. The open route is World Bank WDI `ER.H2O.INTR.PC`,
  `ER.H2O.FWTL.K3`, `ER.H2O.FWAG.ZS` and related series (A-WB), which are themselves sourced from AQUASTAT.
  SDG 6.4.1 and 6.4.2 are covered here for 170–184 countries.
* **Fisheries.** FBS gives fish and seafood production and supply per country (live-weight commodity
  equivalent), and SDGB gives SDG 14.4.1, 14.6.1, 14.7.1 and 14.b.1. Capture versus aquaculture totals per
  country need FishStat, which is per species. A follow-up could ingest FishStat as published (species detail,
  about 1 M rows) or wait for a lead decision on whether REVA may present labelled sums.
* **Agricultural trade totals** exist for 201 countries 1961–2024 (TCL). Bilateral agricultural trade (the TM
  matrix, 411 MB) is not ingested. IMF IMTS (A-IMF) and Comtrade give bilateral merchandise totals.
* **Producer prices** cover 97–141 countries for staple crops; CPI and food CPI cover 204–206 countries
  (monthly).
* **Rural population** here is FAO's copy of UN figures (232 countries) and is labelled as such. A-UN is the
  primary source.
* **Kosovo** has no FAOSTAT data at all. World Bank WDI agriculture and land series for XKX are sparse (A-WB).

## 5. Live run and resources

* Single entry point: `run_atlas_ingest(settings, worker, store, "atlas.fao", mode="live")`, the same code
  path as `uv run python -m reva.atlas.ingest atlas.fao`, against the throwaway database. Budget
  `budgets-1.3.3`: 21 requests reserved, 20 consumed, 543,820,574 bytes, 0 refused.
* Parse times on 4 vCPU: TCL (2.4 GB CSV) about 40 s, FBSH (1.5 GB) about 25 s, the others under 15 s each.
  Peak memory is about 2 GB, dominated by the in-memory zip and the FBSH frame.
* To check the final parser on live bytes, the archived live raws were re-parsed from the throwaway store and
  re-registered (origin `live`) with the final parser. That parser adds the projection marking; the cell
  values are unchanged.
* Disk:
  * Fixtures: 0.5 MB.
  * Live store: 602 MB of raw plus about 86 MB of Parquet per version. It and the throwaway database were
    deleted after the analysis.
  * Probe downloads were deleted before the live run, so only one copy of each raw file existed at a time.
  * Free space stayed at about 19 GB throughout.
* A full refresh is about 545 MB per run, most of it TCL (274 MB). `--dataset <name>` runs one domain.
  FAOSTAT domains update independently (`DateUpdate` in the listing), so a weekly or monthly schedule with
  per-domain runs is cheap.

## 6. Requests to other owners

* **Lead:**
  * Add a schedule entry `("atlas_ingest:atlas.fao", "atlas_ingest", {"source": "atlas.fao"}, 30 * 86400)`
    next to the other atlas sources in `src/reva/cli/seed.py`.
  * Nothing else is needed: the seed hook discovers `atlas_fao.py`, and `registry/sources/atlas_fao.yaml`
    registers `fao.faostat`.
* **ATLASCORE:** `fao.faostat` is already in `budgets.yaml` 1.3.3, so invariant 30's "budget keys = registered
  sources" holds for this key. A possible refinement is a per-response cap above 50 MB for this source in
  `budgets.yaml` if the meter applies one; this run's TCL response was 274 MB and went through.
* **A-WB:**
  * The catalog falls back to `wb.wdi_social.<code>` for WDI series that cite FAO: `SN.ITK.DEFC.ZS`,
    `SN.ITK.MSFI.ZS`, `SN.ITK.SVFI.ZS`, `AG.LND.AGRI.ZS`, `AG.LND.FRST.ZS`, `AG.LND.IRIG.AG.ZS`,
    `AG.PRD.CREL.MT`, `AG.YLD.CREL.KG`, `AG.LND.CREL.HA`, `ER.H2O.FWST.ZS` and `SL.AGR.EMPL.ZS`. Please
    confirm these land in `wdi_social` (Environment / Health / Social Protection & Labor topics) or tell me
    the right dataset key.
  * Please ingest the AQUASTAT-sourced `ER.H2O.*` resource and withdrawal series (they fill the water gap).
* **A-ILO:** optionally append `fao.oea.21144|6199|sex=1|src=3043` (FAO's copy of the ILO modelled
  agricultural employment, possibly another vintage) after the ILOSTAT series in
  `labour.employment.agriculture.ilo_modelled`. A-FAO did not edit `labour.yaml`.
* **A-UN:** the FAO OA rural and urban population is a copy of UN figures and is kept as
  `food_agriculture.rural_population_fao`. If A-UN ingests World Urbanization Prospects, a UN-sourced concept
  should come first.
* **KGHIST:** the historical-area table in §2 (FAO code, M49 code, years) can link FAO cells to
  historical-state entities (USSR, Yugoslav SFR, Czechoslovakia, Ethiopia PDR, Sudan (former),
  Belgium-Luxembourg, Serbia and Montenegro, Netherlands Antilles, Yemen Arab Republic and PDR, Pacific
  Islands Trust Territory).
* **NLQA / UI3:** the new domains `food_agriculture` (96 concepts) and `water` (8 concepts) have French and
  English labels. Flags carry `estimate_kind`; FBS cells are all E or I and should be shown as FAO estimates.
