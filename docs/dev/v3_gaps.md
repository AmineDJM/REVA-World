# V3 GAPHUNT — gap matrix of the headline concepts and the hunt for alternative sources

Agent GAPHUNT, 2026-10-16. Owner: "si tu ne trouves pas quelques données sur un pays, tu peux les chercher
ailleurs, mais cherche, cherche, cherche sans arrêt". Scope: the 251 crosswalk entities
(`registry/entities/country_crosswalk.json` 1.1.0, incl. TWN) × 22 headline concepts. Nothing is committed;
the dev database `reva` was only **read** (SELECTs on `evidence.dataset` + Parquet reads); every write went to
throwaway databases that were dropped.

## 1. What "a gap" means here

`scripts/atlas_gap_matrix.py` (reproducible, see §7) decides per concept and entity whether at least one
non-projection cell (numeric value or text) exists, at two levels:

* **headline** — a series listed by ATLASOPS in `registry/atlas/source_order/*.yaml` for the REVA headline
  concept(s) of that row (what the best-available view answers). In `--snapshot` mode the script also counts
  ATLASOPS's same-source derived cells; the static/tables numbers below do not (density derived from WDI
  population/land area is therefore under-counted, never over-counted).
* **any source** — the headline series plus every ingested series that reports the same kind of figure under
  another definition (CIA Factbook, UNSD AMA, GeoNames, WEO population, the gap-fill offices below …). A gap
  at this level means **no source REVA holds reports anything** for the entity. The series consulted per
  concept are listed in `docs/dev/v3_gaps.json` (`concepts.<key>.series_consulted`).

Concepts without a headline concept (languages, capital, currency, legal system, retirement age, voting age)
only have the "any source" level. "Trade balance / exports-imports" and "Government balance / budget" also
accept, at the "any" level, Factbook exports or budget revenues (labelled so: they are not balances).

### Inputs of the matrix (2026-10-16)

| input | how | sources |
|---|---|---|
| dev database `reva` (no atlas snapshot recorded yet) | `--tables --db reva --objects var/objects` (read-only) | the 40+ atlas tables ATLASOPS had loaded: UN WPP, UN SDG, UNSD SYB, IMF (WEO, BOP, GFS, IMTS), ILOSTAT, Eurostat/OECD wages, OWID, UNDP, WHO GHO, UIS, WBL, CCP, SSPTW |
| throwaway live ingest `reva_gaphunt_live_06422d39` (dropped) | `run_atlas_ingest(mode="live")`, budget-metered (policy budgets-1.3.5), then `--tables` | `atlas.factbook` (261 req, 9.8 MB), `atlas.unsd_ama` (10), `atlas.geonames_countryinfo` (1), `atlas.unesco_uis` (6), `atlas.wbl` (3), `atlas.ccp` (2), `atlas.unsd_syb` (1), **`atlas.gap_eurostat_nuts` (7)** — all `completed`, 0 refused |
| static, report-derived | `--static` | World Bank WDI lists of `docs/dev/v3_a_wb.md` (population, area, density, unemployment, GDP, GDP per capita, trade balance, current account); SSPTW retirement ages for 10 economies WBL lacks (`docs/dev/v3_a_law.md`). The WDI zip (283 MB, ~5 GB RSS to parse) was not re-ingested: other WDI series are not counted |
| gap-fill live ingest `reva_gaphunt_live_643be9e0` (dropped) | `run_atlas_ingest(mode="live")` after ATLASOPS merged the GAPHUNT budget keys (budgets-1.3.6) | `atlas.gap_spc`, `atlas.gap_insee`, `atlas.gap_cbs_bes`, `atlas.gap_jersey`, `atlas.gap_eurostat_nuts` — all `completed`; their coverage was first computed by parsing the same full responses, and the registered tables have exactly the same cells |

Not counted (not loaded anywhere today): World Bank series other than the listed ones, Natural Earth 1:10m
areas, UN WUP capitals, KG statements (Wikidata official languages P37, capitals P36). The "any source"
column is therefore a lower bound of what REVA will hold once everything is in one snapshot. GeoNames
reports population `0` for uninhabited territories: that provider value counts as "a value".

## 2. Gap matrix: before / after GAPHUNT

"before" = all inputs except the five gap-fill sources; "after" = with them. Headline after\* = if ATLASOPS
merges `registry/atlas/source_order/_proposals_gaphunt.yaml` (§5). Numbers are entities (of 251) with at least
one value.

| concept | headline before | headline after* | any source before | any source after | filled by GAPHUNT (any source) |
|---|---:|---:|---:|---:|---|
| Population | 238 | 240 (+ALA SJM) | 251 | 251 | — |
| Area | 216 | 223 (+ALA GLP GUF MTQ MYT REU SJM) | 251 | 251 | — |
| Population density | 216 | 216 | 238 | 240 | ALA PCN |
| GDP | 215 | 221 (+BES COK NIU PCN TKL WLF) | 224 | 234 | ALA BES GLP GUF MTQ MYT PCN REU SJM WLF |
| GDP per capita | 215 | 220 (+BES COK NIU TKL WLF) | 223 | 231 | ALA BES GLP GUF MTQ MYT REU WLF |
| Unemployment rate | 229 | 229 | 230 | 232 | BES MYT |
| Minimum wage | 175 | 175 | 175 | 180 | GLP GUF MTQ MYT REU |
| Average wage | 192 | 192 | 192 | 192 | — |
| Life expectancy | 237 | 238 (+ALA) | 237 | 238 | ALA |
| Literacy / schooling | 180 | 180 | 219 | 219 | — |
| HDI-like index | 193 | 193 | 193 | 193 | — |
| Inflation | 197 | 202 (+GLP GUF MTQ MYT REU) | 212 | 222 | ASM GLP GUF GUM MNP MTQ MYT NIU REU WLF |
| Government balance / budget | 197 | 197 | 213 | 215 | NIU PYF |
| Trade balance / exports-imports | 206 | 206 | 217 | 222 | COK NIU PCN TKL WLF |
| CO2 emissions | 214 | 214 | 223 | 223 | — |
| Internet use | 226 | 226 | 238 | 238 | — |
| Languages | – | – | 247 | 247 | — |
| Capital | – | – | 244 | 245 | TKL |
| Currency | – | – | 249 | 249 | — |
| Legal system | – | – | 238 | 238 | — |
| Retirement age | – | – | 198 | 198 | — |
| Voting age | – | – | 234 | 234 | — |

Cells (entity × concept) with **no value from any source**: **576 → 530** (−46). Headline cells without a value
(16 concepts with a headline): **670 → 644** once the proposals are merged.

Minimum wage: AUT, CHE, DNK, FIN, ISL, ITA, NOR, SWE are counted as "no value" but Eurostat states that **no
national minimum wage exists** there (value-less cells with flag `m`, kept by A-ILO): a provider statement, not
a gap to fill. Uninhabited entities (ATA, BVT, HMD, SGS, IOT, UMI, ATF) and the World Bank aggregate CHI
(Guernsey + Jersey, which are entities of their own) cannot be filled by construction.

### Entities with the most empty concepts (any source; 4 or more before or after)

| entity | empty concepts before | after | still empty (any source) |
|---|---:|---:|---|
| BVT Bouvet Island | 18 | 18 | density gdp gdp_per_capita unemployment minimum_wage average_wage life_expectancy literacy_schooling hdi inflation government_balance trade_balance co2 internet languages capital retirement_age voting_age |
| HMD Heard Island and McDonald Islands | 18 | 18 | density gdp gdp_per_capita unemployment minimum_wage average_wage life_expectancy literacy_schooling hdi inflation government_balance trade_balance co2 internet languages capital retirement_age voting_age |
| UMI United States Minor Outlying Islands | 18 | 18 | density gdp gdp_per_capita unemployment minimum_wage average_wage life_expectancy literacy_schooling hdi inflation government_balance trade_balance co2 internet capital legal_system retirement_age voting_age |
| ATA Antarctica | 17 | 17 | density gdp gdp_per_capita unemployment minimum_wage average_wage life_expectancy literacy_schooling hdi inflation government_balance trade_balance languages capital currency retirement_age voting_age |
| ATF French Southern Territories | 16 | 16 | density gdp gdp_per_capita unemployment minimum_wage average_wage life_expectancy literacy_schooling hdi inflation government_balance trade_balance co2 internet retirement_age voting_age |
| CHI Channel Islands | 16 | 16 | minimum_wage average_wage life_expectancy literacy_schooling hdi inflation government_balance trade_balance co2 internet languages capital currency legal_system retirement_age voting_age |
| IOT British Indian Ocean Territory | 16 | 16 | density gdp gdp_per_capita unemployment minimum_wage average_wage life_expectancy literacy_schooling hdi inflation government_balance trade_balance co2 internet retirement_age voting_age |
| SGS South Georgia and the South Sandwich Islands | 16 | 16 | density gdp gdp_per_capita unemployment minimum_wage average_wage life_expectancy literacy_schooling hdi inflation government_balance trade_balance co2 internet retirement_age voting_age |
| SJM Svalbard and Jan Mayen Islands | 17 | 16 | density gdp_per_capita unemployment minimum_wage average_wage life_expectancy literacy_schooling hdi inflation government_balance trade_balance co2 internet legal_system retirement_age voting_age |
| CCK Cocos (Keeling) Islands | 14 | 14 | density gdp gdp_per_capita unemployment minimum_wage average_wage life_expectancy literacy_schooling hdi inflation government_balance trade_balance co2 retirement_age |
| NFK Norfolk Island | 14 | 14 | density gdp gdp_per_capita unemployment minimum_wage average_wage life_expectancy literacy_schooling hdi inflation government_balance trade_balance co2 retirement_age |
| ALA Åland Islands | 17 | 13 | unemployment minimum_wage average_wage literacy_schooling hdi inflation government_balance trade_balance co2 internet legal_system retirement_age voting_age |
| CXR Christmas Island | 13 | 13 | density gdp gdp_per_capita unemployment minimum_wage average_wage life_expectancy literacy_schooling hdi inflation government_balance trade_balance retirement_age |
| ESH Western Sahara | 13 | 13 | gdp gdp_per_capita minimum_wage average_wage literacy_schooling hdi inflation government_balance trade_balance co2 internet legal_system retirement_age |
| BES Bonaire, Sint Eustatius and Saba | 15 | 12 | minimum_wage average_wage literacy_schooling hdi inflation government_balance trade_balance internet capital legal_system retirement_age voting_age |
| BLM Saint Barthélemy | 12 | 12 | gdp gdp_per_capita unemployment minimum_wage average_wage literacy_schooling hdi inflation government_balance trade_balance co2 retirement_age |
| PCN Pitcairn | 14 | 11 | gdp_per_capita unemployment minimum_wage average_wage life_expectancy literacy_schooling hdi inflation government_balance co2 retirement_age |
| VAT Holy See | 11 | 11 | gdp gdp_per_capita unemployment minimum_wage average_wage literacy_schooling hdi inflation government_balance co2 retirement_age |
| MAF Saint Martin (French Part) | 10 | 10 | unemployment minimum_wage average_wage literacy_schooling hdi inflation government_balance trade_balance co2 retirement_age |
| GGY Guernsey | 9 | 9 | gdp gdp_per_capita minimum_wage literacy_schooling hdi inflation government_balance trade_balance co2 |
| IMN Isle of Man | 9 | 9 | minimum_wage literacy_schooling hdi inflation government_balance trade_balance co2 internet retirement_age |
| MYT Mayotte | 14 | 9 | average_wage literacy_schooling hdi government_balance trade_balance co2 legal_system retirement_age voting_age |
| SPM Saint Pierre and Miquelon | 9 | 9 | gdp_per_capita unemployment minimum_wage average_wage hdi inflation government_balance trade_balance retirement_age |
| GIB Gibraltar | 8 | 8 | gdp gdp_per_capita minimum_wage literacy_schooling hdi inflation government_balance retirement_age |
| GLP Guadeloupe | 12 | 8 | average_wage hdi government_balance trade_balance co2 legal_system retirement_age voting_age |
| GUF French Guiana | 12 | 8 | average_wage hdi government_balance trade_balance co2 legal_system retirement_age voting_age |
| JEY Jersey | 8 | 8 | gdp_per_capita literacy_schooling hdi inflation government_balance trade_balance co2 retirement_age |
| MTQ Martinique | 12 | 8 | average_wage hdi government_balance trade_balance co2 legal_system retirement_age voting_age |
| SHN Saint Helena | 8 | 8 | gdp gdp_per_capita minimum_wage hdi inflation government_balance trade_balance retirement_age |
| FLK Falkland Islands (Malvinas) | 7 | 7 | gdp minimum_wage literacy_schooling hdi inflation government_balance retirement_age |
| FRO Faroe Islands | 7 | 7 | minimum_wage average_wage literacy_schooling hdi inflation government_balance retirement_age |
| PRK Democratic People's Republic of Korea | 7 | 7 | minimum_wage average_wage hdi inflation government_balance internet retirement_age |
| REU Réunion | 11 | 7 | hdi government_balance trade_balance co2 legal_system retirement_age voting_age |
| SXM Sint Maarten (Dutch part) | 7 | 7 | unemployment minimum_wage average_wage literacy_schooling hdi government_balance retirement_age |
| MNP Northern Mariana Islands | 7 | 6 | minimum_wage average_wage literacy_schooling hdi co2 retirement_age |
| VIR United States Virgin Islands | 6 | 6 | minimum_wage average_wage literacy_schooling hdi inflation retirement_age |
| GRL Greenland | 5 | 5 | minimum_wage average_wage literacy_schooling hdi retirement_age |
| MCO Monaco | 5 | 5 | average_wage hdi government_balance trade_balance co2 |
| TKL Tokelau | 7 | 5 | unemployment minimum_wage hdi co2 retirement_age |
| AIA Anguilla | 4 | 4 | minimum_wage average_wage hdi retirement_age |
| ASM American Samoa | 5 | 4 | minimum_wage average_wage hdi retirement_age |
| CUW Curaçao | 4 | 4 | minimum_wage hdi government_balance retirement_age |
| CYM Cayman Islands | 4 | 4 | minimum_wage average_wage hdi retirement_age |
| MSR Montserrat | 4 | 4 | minimum_wage average_wage hdi retirement_age |
| NIU Niue | 7 | 4 | minimum_wage average_wage hdi retirement_age |
| PYF French Polynesia | 5 | 4 | minimum_wage average_wage hdi retirement_age |
| TCA Turks and Caicos Islands | 4 | 4 | minimum_wage average_wage hdi retirement_age |
| VGB British Virgin Islands | 4 | 4 | minimum_wage average_wage hdi trade_balance |
| GUM Guam | 4 | 3 | minimum_wage hdi retirement_age |
| WLF Wallis and Futuna Islands | 7 | 3 | minimum_wage hdi retirement_age |


## 3. Sources added (one module per office)

All five follow `docs/dev/v3_atlas_interface.md` §7: `atlas_source()` + `async atlas_ingest(run)`; every request
is a GET through `run.get` (no HTTP client is built), raw bytes are archived with `run.archive` before
parsing, tables are registered with `run.register`. Shared plumbing: `src/reva/adapters/connectors/atlas_gap_common.py`
(fetch → archive → parse → register, strict decimals, periods, duplicate-cell refusal). No value is converted,
rescaled, annualised, imputed or blended; value-less cells are omitted.

| connector (module) | source key / budget | tables (`atlas.<source>.<dataset>`) | entities | licence |
|---|---|---|---|---|
| `atlas.gap_spc` (`atlas_gap_spc.py`) | `spc.pdh_stat` (requested 12/day) | `spc.pocket` (DF_POCKET: mid-year population, land area, density, life expectancy M/F, GDP LCU/USD, GDP per capita USD, inflation, merchandise trade, government revenue/expenditure % GDP, EEZ), `spc.keyfacts` (capital, languages, currency, government — texts), `spc.emprates` (unemployment rate, national total), `spc.digital` (ITU internet users DD024) | 22 PICTs: ASM COK FJI FSM GUM KIR MHL MNP NCL NIU NRU PCN PLW PNG PYF SLB TKL TON TUV VUT WLF WSM (9,676 pocket cells kept) | PDH catalogue "Other (Open)"; terms page 403 → **pending maintainer review** |
| `atlas.gap_eurostat_nuts` (`atlas_gap_eurostat_nuts.py`) | `eurostat.dissemination` (existing key, shared with A-ILO's Eurostat source) | `eurostat.nuts2_gapfill`: population 1 Jan, total/land area, density, GDP (MIO_EUR, EUR_HAB), unemployment 15-74 (LFS), life expectancy at birth, internet use (3 / 12 months) | GLP MTQ GUF REU MYT (FRY1-5), ALA (FI20), SJM (NO0B "Svalbard og Jan Mayen"); 1,040 cells | Eurostat reuse policy (CC BY 4.0) |
| `atlas.gap_insee` (`atlas_gap_insee.py`) | `insee.bdm` (requested 6/day) | `insee.ipc_dom` (IPC-2025 annual all-items index + annual variation = change of the annual average), `insee.smic_dom` (gross hourly Smic, DOM hors Mayotte series written per department it names + Mayotte series; monthly) | GLP MTQ GUF REU MYT (73 + 1,330 cells) | Licence Ouverte 2.0 (Etalab) |
| `atlas.gap_cbs_bes` (`atlas_gap_cbs_bes.py`) | `cbs.opendata` (requested 12/day) | `cbs.bes_population` (1 Jan / 31 Dec population, births, deaths), `cbs.bes_gdp` (GDP current and 2017 prices, million USD; changes; GDP per capita USD), `cbs.bes_labour` (unemployment, participation, 15-74, two-yearly survey; filtered server-side) | BES (CN01); islands GM9001-9003 kept unmapped with `partial_of_iso3: BES` | CC BY 4.0 (CBS copyright notice, quoted) |
| `atlas.gap_jersey` (`atlas_gap_jersey.py`) | `statistics_jersey.opendata` (requested 3/day) | `statsjersey.earnings` (mean and median monthly earnings, FTE basis, by sector, monthly from 2022) | JEY (1,344 cells) | Open Government Licence – Jersey v1.0 |

Honesty choices worth knowing:

* **SPC pocket summary repeats the latest known value in later years.** Its `DATA_YEAR` attribute names the
  year a value is for: only cells with `DATA_YEAR` = cell year are kept; 3,036 repeats (e.g. Wallis and Futuna
  revenue 2015 shown again for 2016-2025) are omitted and counted in the coverage note; `OBS_STATUS = O` (21)
  omitted. SPC's own currency conversions stay visible (`flags.obs_comment`, e.g. "Converted 0.7041 NZD/USD").
* **Eurostat labels are checked**: a NUTS code whose English label changes fails the parse (a relabelled
  region is never silently mapped). Population is a 1 January stock and GDP is in euro (`flags.reference`).
* **INSEE Smic for "départements d'outre-mer (hors Mayotte)"** is one series; it is written once per department
  the title names (GLP, MTQ, GUF, REU) with `provider_geo = "DOM"`, `|applies_to=D97x` in the indicator and
  `flags.applies_to_rule` quoting the title (maintainer review pending). Hourly, never turned into a monthly
  amount. INSEE `OBS_STATUS = O` cells carry `NaN`: omitted, never parsed.
* **CBS** reports empty survey cells of the labour table as `0` counts with a null rate: those rows are
  skipped (never a filled zero).
* **Jersey** already had ILOSTAT mean earnings (annual): Statistics Jersey adds a second, monthly FTE source;
  it does not change the matrix count.

Catalog: `registry/atlas/catalog/gapfill.yaml` (domain `gapfill`, 45 provider-declared concepts, each with an
explicit `theme`, French labels written by GAPHUNT). Geo aliases: `registry/atlas/geo_aliases/gaphunt.yaml`
(schemes `nuts_gaphunt`, `insee_gaphunt`, `cbs_gaphunt`, each alias citing the provider label). Sources:
`registry/sources/{spc_pdh_stat,insee_bdm,cbs_opendata,statistics_jersey}.yaml`. Budget request:
`registry/atlas/budget_requests/gaphunt.yaml` (`spc.pdh_stat` 12, `insee.bdm` 6, `cbs.opendata` 12,
`statistics_jersey.opendata` 3 requests/day; +33 requests, +12 MB/day in total).

## 4. Live runs

* `atlas.gap_eurostat_nuts`: **live ingest completed** twice through `run_atlas_ingest(mode="live")` into
  throwaway databases (`reva_gaphunt_live_197fdcd8`, `reva_gaphunt_live_06422d39`, both dropped): 7 requests
  reserved/consumed, 39,339 bytes, 7 raw artifacts archived, table `atlas.eurostat.nuts2_gapfill` (1,040 rows,
  10 indicators, 7 entities), ~10 s.
* `atlas.gap_spc`, `atlas.gap_insee`, `atlas.gap_cbs_bes`, `atlas.gap_jersey`: the platform **refused** the live
  run before any request (`budget_exhausted`, reason `unknown_source`: their keys are not in the pinned policy
  budgets-1.3.5) — fail closed as designed. Their fixtures are complete real provider responses captured on
  2026-10-16 (SPC: restricted SDMX keys, see `FLOWS`), and the "after" matrix used the full live responses
  parsed by the same parsers. Once ATLASOPS merges `gaphunt.yaml`, run
  `uv run python -m reva.atlas.ingest atlas.gap_spc` (and `atlas.gap_insee`, `atlas.gap_cbs_bes`, `atlas.gap_jersey`).
* Seed: the four new sources are discovered by module name; `register_atlas_connector_versions` registers them
  at the next `reva seed`.

## 5. Proposals to ATLASOPS (`registry/atlas/source_order/_proposals_gaphunt.yaml`, not loaded)

Only where the provider's definition is the headline concept's definition; appended **after** the existing
series (they answer only cells nobody else reports):

| headline concept | proposed series | fills |
|---|---|---|
| `reva.economy.inflation_cpi` | `insee.ipc_dom.CPI_ANNUAL_CHANGE\|…` (annual-average change) | GLP MTQ GUF REU MYT |
| `reva.economy.gdp_usd` | `cbs.bes_gdp.gdp_current_prices` (×1,000,000; USD is BES's currency), `spc.pocket.GDPCUSD` (×1000; SPC conversion) | BES; COK NIU PCN TKL WLF |
| `reva.economy.gdp_per_capita_usd` | `cbs.bes_gdp.gdp_per_capita`, `spc.pocket.GDPCPCUSD` | BES; COK NIU TKL WLF |
| `reva.economy.gdp_real_growth` | `cbs.bes_gdp.gdp_volume_change` | BES |
| `reva.science.internet_users` | `spc.digital.DD024` (ITU), Eurostat `I_IU3` (ITU definition, 16-74) | none new (second source for Pacific territories and DOMs already in SDG/ITU) |
| `reva.geography.land_area` / `surface_area` | Eurostat `reg_area3` L0008 / TOTAL | GLP MTQ GUF REU MYT ALA SJM |
| new `reva.population.total_1_january` | Eurostat `demo_r_d2jan`, CBS `population_1_january` | ALA SJM (+ DOMs, BES as a second view) |
| new `reva.health.life_expectancy_national_tables` | Eurostat `demo_r_mlifexp` | ALA (+ DOMs) |

Declared **not equivalent** (catalog concepts only): SPC mid-year population (de facto/de jure not stated),
Eurostat/SPC density (average population, unstated denominator), Eurostat/CBS unemployment 15-74 and SPC
unemployment (age band / definition), INSEE hourly Smic (not monthly), SPC life expectancy (M/F only), SPC
trade balance (valuation not stated), SPC inflation (average vs December not stated). A unit test validates
every proposal entry against `_schema.json`.

## 6. Every source searched, with the result

Machine-readable in `docs/dev/v3_gaps.json` (`searched_sources`, and `entities.<ISO3>.searched` for the UI's
"sources searched" panel of a gap; the list itself lives in `scripts/atlas_gap_matrix.py:SEARCHED`).

| source | searched for | result | licence | why / note |
|---|---|---|---|---|
| [Pacific Community (SPC) Pacific Data Hub .Stat](https://stats-sdmx-disseminate.pacificdata.org/rest/) | ASM COK FJI PYF GUM KIR MHL FSM NRU NCL NIU MNP PLW PNG PCN WSM SLB TKL TON TUV VUT WLF | **used** (`atlas.gap_spc`) | PDH catalogue 'Other (Open)', per-dataset licence (pending review) | DF_POCKET, DF_KEYFACTS, DF_EMPRATES, DF_DIGITAL_DEVELOPMENT; ingested live 2026-10-16 (budget spc.pdh_stat) |
| [Eurostat regional statistics (NUTS-2)](https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/) | GLP MTQ GUF REU MYT ALA SJM | **used** (`atlas.gap_eurostat_nuts`) | Eurostat reuse policy (CC BY 4.0) | ingested live 2026-10-16 (budget eurostat.dissemination) |
| [Insee BDM (SDMX)](https://bdm.insee.fr/series/sdmx/) | GLP MTQ GUF REU MYT | **used** (`atlas.gap_insee`) | Licence Ouverte 2.0 (Etalab) | CPI (annual average change) and Smic of the overseas departments; ingested live 2026-10-16 (budget insee.bdm) |
| [CBS Statistics Netherlands StatLine (OData)](https://opendata.cbs.nl/ODataApi/odata/) | BES | **used** (`atlas.gap_cbs_bes`) | CC BY 4.0 | population, GDP (USD), labour survey; CPI only quarterly per island (not taken); ingested live 2026-10-16 (budget cbs.opendata) |
| [Statistics Jersey (opendata.gov.je, CKAN)](https://opendata.gov.je/) | JEY | **used** (`atlas.gap_jersey`) | Open Government Licence - Jersey v1.0 | monthly FTE earnings (second source after ILOSTAT); RPI is not a CPI (not taken); registered unemployment is not ILO (not taken); ingested live 2026-10-16 |
| [Insee populations légales / COM (Saint-Martin, Saint-Barthélemy, Saint-Pierre-et-Miquelon)](https://www.insee.fr/fr/statistiques/2011101?geo=COM-97701) | MAF BLM SPM | **skipped** | Licence Ouverte 2.0 | census figures are HTML pages per collectivity, no SDMX series in the BDM; population already in WPP/Factbook |
| [IEOM / IEDOM (overseas issuing institutes)](https://www.ieom.fr/) | NCL PYF WLF SPM BLM MAF | **skipped** | not stated for data reuse | annual reports in PDF; no machine-readable open series found |
| [ISEE Nouvelle-Calédonie](https://www.isee.nc/) | NCL | **skipped** | not verified | data.isee.nc unreachable from the container (connection failed); NCL already covered by WB/AMA/SPC |
| [ISPF Polynésie française](https://www.ispf.pf/) | PYF | **skipped** | not verified | Excel/PDF publications; PYF covered by WB, AMA, SPC |
| [US Census Bureau International Database (IDB) API](https://api.census.gov/data/timeseries/idb/5year) | GGY JEY IMN AIA MSR SPM WLF SHN FLK MAF BLM PRI GUM VIR ASM MNP | **skipped** | US public domain | API now answers 302 to missing_key.html: a key is required (registration with an e-mail address; not done, no personal data in requests); no bulk file found |
| [US Census Bureau (Puerto Rico, Guam, USVI, CNMI, American Samoa)](https://data.census.gov/) | PRI GUM VIR MNP ASM | **skipped** | US public domain | same API key requirement; island areas already in WB/WPP; SPC covers GUM/MNP/ASM |
| [Statistics Greenland (PxWeb)](https://bank.stat.gl/api/v1/en/Greenland/) | GRL | **skipped** | not verified | PxWeb v1 data queries need POST (the atlas platform is GET-only); remaining GRL gaps (minimum/average wage, literacy, HDI) are not published as such |
| [Statistics Faroe Islands (PxWeb)](https://statbank.hagstova.fo/api/v1/en/H2/) | FRO | **skipped** | not verified | PxWeb v1 data queries need POST (GET-only platform) |
| [Kosovo Agency of Statistics (ASKdata PxWeb)](https://askdata.rks-gov.net/api/v1/en/ASKdata/) | XKX | **skipped** | not verified | PxWeb v1 data queries need POST; XKX gaps are minimum wage, literacy and HDI |
| [Statistics Norway (Svalbard)](https://data.ssb.no/api/pxwebapi/v2/) | SJM | **skipped** | CC BY 4.0 (SSB) | Eurostat NO0B already gives SJM population, area, GDP; PxWeb v2 GET exists (candidate for Svalbard settlements detail) |
| [Statistics and Research Åland (ÅSUB, PxWeb)](https://pxweb.asub.ax/PXWeb/api/v1/en/) | ALA | **skipped** | not verified | PxWeb v1 POST; Eurostat FI20 covers ALA population, area, GDP, life expectancy |
| [Guernsey data and analysis](https://www.gov.gg/data) | GGY | **skipped** | not verified | bulletins in PDF/Excel only |
| [Isle of Man Economic Affairs](https://www.gov.im/) | IMN | **skipped** | not verified | HTTP 403 to automated requests |
| [HM Government of Gibraltar statistics](https://www.gibraltar.gov.gi/statistics) | GIB | **skipped** | not verified | PDF abstracts only |
| [Bermuda Department of Statistics](https://www.gov.bm/department/statistics) | BMU | **skipped** | not verified | PDF reports; BMU covered by WB/AMA/IMF BOP |
| [Cayman Islands Economics and Statistics Office](https://www.eso.ky/) | CYM | **skipped** | not verified | PDF/Excel; CYM covered by WB/AMA |
| [Falkland Islands Government statistics](https://www.falklands.gov.fk/policy/statistics) | FLK | **skipped** | not verified | unreachable from the container (connection failed) |
| [CBS Aruba](https://cbs.aw/wp/) | ABW | **skipped** | not verified | Excel/PDF downloads, no API; ABW covered by WB |
| [CBS Curaçao](https://www.cbs.cw/) | CUW | **skipped** | not verified | Excel/PDF downloads, no API; CUW covered by WB/AMA |
| [STAT Sint Maarten](https://stat.gov.sx/) | SXM | **skipped** | not verified | unreachable from the container |
| [Taiwan DGBAS national statistics](https://nstatdb.dgbas.gov.tw/dgbasall/webMain.aspx?k=engmain) | TWN | **skipped** | Open Government Data License v1.0 (data.gov.tw) for data.gov.tw datasets | TWN gaps are literacy, HDI and average wage in ILOSTAT form: DGBAS computes a 'Taiwan HDI' with UNDP's method (a national computation, not UNDP's HDI) and publishes it in PDF/HTML; eng.stat.gov.tw unreachable; data.gov.tw reachable but no machine-readable HDI/literacy series found |
| [Hong Kong Census and Statistics Department (API)](https://www.censtatd.gov.hk/api/) | HKG | **no_data** | HK Government data terms | API works; HKG has no remaining gap in the 22 concepts (every concept has at least one source) |
| [Macao DSEC](https://www.dsec.gov.mo/) | MAC | **skipped** | not verified | MAC gaps: minimum wage (statutory since 2020) and retirement age; DSEC publishes neither as a series (legal texts) |
| [Monaco IMSEE](https://www.imsee.mc/) | MCO | **skipped** | not verified | PDF publications (Monaco en chiffres) |
| [San Marino statistics office](https://www.statistica.sm/) | SMR | **skipped** | not verified | HTML/PDF only; SMR gap is CO2 |
| [Liechtenstein Amt für Statistik](https://www.llv.li/) | LIE | **skipped** | not verified | HTTP 403 to automated requests |
| [Andorra Departament d'Estadística](https://www.estadistica.ad/) | AND | **skipped** | not verified | HTML/PxWeb (POST); AND has no remaining any-source gap (WDI/ILO modelled unemployment missing, Factbook has it) |
| [Holy See / Vatican City State](https://www.vatican.va/) | VAT | **no_data** | - | no statistical office and no national accounts, labour or price statistics exist; Factbook population and area only |
| [Bank of Korea (North Korea GDP estimates)](https://ecos.bok.or.kr/api/) | PRK | **skipped** | API key required | an external estimate (not an official DPRK statistic); API needs a key and was unreachable; PRK GDP already in UNSD AMA and Factbook |
| Western Sahara | ESH | **no_data** | - | no official statistics office for the territory; only the (stale, 2021) Factbook profile and UN WPP population |
| [Palestinian Central Bureau of Statistics](https://www.pcbs.gov.ps/) | PSE | **skipped** | not verified | unreachable from the container; PSE remaining gaps are legal system and voting age (Factbook has only West Bank/Gaza partial profiles) |
| [Northern Cyprus statistics (unrecognised; not a crosswalk entity)](https://www.stat.gov.ct.tr/) | (not a crosswalk entity) | **skipped** | not verified | unreachable; would stay a separate unmapped provider geography, never merged into CYP |
| Somaliland (unrecognised; not a crosswalk entity) | (not a crosswalk entity) | **skipped** | - | no open machine-readable statistics found; WHR/NE keep 'Somaliland' unmapped, never merged into SOM |
| Uninhabited / no permanent population | ATA BVT HMD SGS IOT UMI ATF | **no_data** | - | no resident population: no economy, labour, price, health or legal statistics exist; area (and GeoNames/Factbook texts) only |

Sources found in the reports of other agents for the same gaps (not repeated above): UNECE (POST API, licence
unclear — A-ILO), IHME GBD (non-commercial — A-HEALTHEDU), WageIndicator (terms — A-ILO), OECD PIAAC/MSTI
(different constructs — A-HEALTHEDU), Global Peace Index / Social Progress Index (closed — A-QOL).

## 7. Remaining gaps and where to look next

* **Uninhabited or aggregate** (ATA BVT HMD SGS IOT UMI ATF; CHI): nothing to find (documented, `no_data`).
* **French collectivities** BLM, MAF, SPM (Saint-Barthélemy, Saint-Martin, Saint-Pierre-et-Miquelon): INSEE
  publishes census populations as HTML pages only; IEOM publishes PDFs. Next: INSEE "populations légales" files
  (xlsx) if a stable download URL is confirmed; IEOM tables are not open data.
* **French DOM legal system / voting age** (GLP GUF MTQ MYT REU): French law and suffrage apply (Factbook France
  profile), but no source states it per territory; REVA does not infer it.
* **Crown dependencies and BOTs** (GGY, IMN, GIB, SHN, FLK): PDF-only publications or blocked sites (403).
  Isle of Man and Guernsey would need a document-extraction path (not a GET API).
* **PxWeb offices** (Greenland, Faroe, Kosovo ASK, Åland ÅSUB, Andorra): the data API needs POST, which the
  atlas platform (GET-only) does not offer; PxWeb **v2** offers GET (SSB already serves it) — a candidate once
  those offices upgrade; worth a platform discussion.
* **HDI for territories / TWN / XKX**: UNDP computes HDI only for its member set; national "HDI" computations
  (Taiwan DGBAS) are a different producer's computation — would be a separate concept, PDF/HTML only today.
* **Minimum wage** for 60+ entities: several have none by law (Eurostat-stated for 8); for the others (e.g. MAC,
  SGP, ARE, BRN) only legal texts exist; Wikidata has no structured minimum-wage property — A-LAW's legal-text
  route is the only one.
* **US territories** (PRI, GUM, VIR, MNP, ASM): the Census Bureau API now requires a key (registration with an
  e-mail address, not done: no personal data in requests); SPC covers GUM/MNP/ASM.

## 8. Re-run

```bash
# coverage of a database that has an atlas snapshot (the deployment, once ATLASOPS records one)
uv run python scripts/atlas_gap_matrix.py --snapshot --db reva --out-json /tmp/gaps.json --out-md /tmp/gaps.md
# a database without a snapshot (read-only), plus the report-derived World Bank lists
uv run python scripts/atlas_gap_matrix.py --static --tables --db reva --objects var/objects \
    --with-proposals --out-json /tmp/gaps.json --out-md /tmp/gaps.md
# union of coverage files saved earlier with --save-coverage FILE
uv run python scripts/atlas_gap_matrix.py --coverage a.json --coverage b.json --out-md /tmp/gaps.md
# gap-fill sources (live needs the budget keys merged; --fixture replays the captured responses)
uv run python -m reva.atlas.ingest atlas.gap_eurostat_nuts [--fixture]
```

## 9. Tests

* `tests/unit/test_atlas_gaphunt.py` (16, offline, real captured bytes): discovery and specs, sources and
  budget requests declared, geo aliases map only the gap entities, SPC coverage + carried-forward repeats never
  kept + omitted counts add up + coded-cell refusal, Eurostat seven entities + label-change fail-closed, INSEE CPI
  and Smic (DOM expansion flagged, hourly unit) + DTD refusal + scope-change refusal, CBS total → BES with islands
  unmapped + no filled zeros, Jersey monthly FTE + column-change refusal, strict decimals, proposals valid
  against `_schema.json`, gap matrix from static + coverage files (and every headline id exists).
* `tests/integration/test_atlas_gaphunt_db.py` (5, `db`): each source through `run_atlas_ingest(mode="fixture")`
  — raw archived as `evidence.acquisition`, tables registered (`atlas.table`, `test_fixture`), expected entities,
  idempotent second run.
* `tests/invariants/test_invariant_30_budgets.py`: no GAPHUNT module builds a client (static scan passes). The
  "budget keys == registered sources" test now also lists the four new source keys until ATLASOPS merges
  `gaphunt.yaml` (it already failed before on other agents' keys: `glottolog.cldf`, `grambank.cldf`, `wals.cldf`).

## 10. Requests

* **ATLASOPS**: merge `registry/atlas/budget_requests/gaphunt.yaml` (`budget_merge --write`, re-pin); review and
  merge `_proposals_gaphunt.yaml`; when the deployment snapshot exists, re-run `--snapshot` to replace the
  static World Bank lists by the real WDI coverage.
* **Lead / maintainer**: confirm the SPC PDH licence ("Other (Open)" in the catalogue; terms page not readable by
  automated clients) before publishing SPC values beyond the atlas; review the INSEE DOM Smic expansion rule.
* **UI3**: show `docs/dev/v3_gaps.json` → `entities.<ISO3>.searched` (+ `searched_sources`) next to an empty
  concept ("REVA a cherché ici").
