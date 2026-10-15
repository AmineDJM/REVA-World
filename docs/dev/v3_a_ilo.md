# V3 A-ILO — labour atlas layer (ILOSTAT + open gap-fill sources)

Agent A-ILO, 2026-10-15. Owner request: minimum wage, average wage and unemployment for every country.
Status: connectors on the ATLASCORE platform (`atlas_source()` / `atlas_ingest(run)`), catalog, fixtures, offline
tests and one live ingest into a throwaway database (`reva_ailo_live_4bda93d5`, 21 MB; objects in the agent
scratchpad) — all done. Nothing was committed by this agent; the dev database `reva` was never touched.

## What was built

| Path | What |
|---|---|
| `src/reva/adapters/connectors/atlas_ilo.py` | atlas source `atlas.ilostat` (source key `ilostat.rplumber`): 20 ILOSTAT bulk indicator files + 5 code dictionaries + TOC → 20 atlas tables `ilostat.<indicator id>` |
| `src/reva/adapters/connectors/atlas_wages_gapfill.py` | atlas sources `atlas.eurostat.earn_mw_cur` (`eurostat.dissemination`) → `eurostat.earn_mw_cur`; `atlas.oecd.earnings` (`oecd.sdmx`) → `oecd.earnings_mw_curp`, `oecd.earnings_av_an_wage` |
| `src/reva/adapters/connectors/atlas_labour_common.py` | `ingest_with_run(run, connector)`: plan → `run.get` → empty-body/media-type refusal → `run.archive` → parse → `run.register`; honours `--dataset`; never swallows `BudgetExhausted`; bounded gunzip, strict decimals, canonical JSON. No HTTP client is built anywhere in these modules (invariant 30 passes for them). |
| `registry/atlas/catalog/labour.yaml` | 41 provider-declared concepts, domain `labour` |
| `registry/atlas/geo_aliases/ilo.yaml` | scheme `ilo` = ISO3 + `KOS`→XKX, `CHA`→CHI (quoted from the ILOSTAT `ref_area` dictionary) |
| `registry/sources/ilostat.yaml`, `registry/sources/oecd_sdmx.yaml` | new source keys; Eurostat reuses `eurostat.dissemination` |
| `registry/atlas/budget_requests/a_ilo.yaml` | merged by ATLASCORE in budgets-1.3.1 (`ilostat.rplumber` 60/day, `oecd.sdmx` 6/day) |
| `tests/unit/test_atlas_ilo.py` (15), `tests/integration/test_atlas_ilo_ingest.py` (3, `db`) | offline; fixtures `tests/fixtures/source/atlas.ilostat/` (real responses restricted to FRA, DZA, KOS, AUT, TWN, X01 + full dictionaries + TOC, 684 KB), `atlas.eurostat.earn_mw_cur/` (FR, AT, EL, UK, DE), `atlas.oecd.earnings/` (FRA, USA, OECD). In fixture mode the connectors request exactly these restricted URLs (`FIXTURE_REF_AREAS` …). |

Run: `uv run python -m reva.atlas.ingest atlas.ilostat [--dataset ear_inee_cur_nb_a] [--fixture]`, same for
`atlas.eurostat.earn_mw_cur` and `atlas.oecd.earnings`. Live run 2026-10-15: ILOSTAT 26 requests / 20.8 MB,
Eurostat 2 / 0.4 MB, OECD 2 / 2.0 MB, all within the reservation, 23 tables registered, no errors.

## Sources researched

| Source | Access | Licence | Used |
|---|---|---|---|
| ILOSTAT bulk/API `rplumber.ilo.org` (`/data/indicator/?id=<ID>&format=.csv.gz`, `/metadata/dic`, `/metadata/toc`) | open; one gzip CSV per indicator, every area and year | CC BY 4.0 (ILO, datasets since 2023-05-03) | **yes**, primary |
| Legacy bulk `www.ilo.org/ilostat-files/WEB_bulk_download` | redirects to `webapps.ilo.org` → **404** | — | retired |
| ILOSTAT SDMX `sdmx.ilo.org` | reachable | CC BY 4.0 | no (same data; rplumber gives one request per indicator) |
| Eurostat `earn_mw_cur` + `ESTAT/OBS_FLAG` code list | open SDMX-CSV | Eurostat reuse policy (CC BY 4.0 equivalent) | **yes** (second source; explicit "no national minimum wage" statements) |
| OECD `OECD.ELS.SAE` `DSD_EARNINGS@MW_CURP`, `@AV_AN_WAGE` | open SDMX | OECD Terms; CC BY 4.0 is the OECD default since 2024-07-01 — dataset-level licence **not separately verified** (oecd.org answered 403) | **yes** |
| OECD `DSD_EARNINGS@RMW` (real minimum wages) | open | as above | no — deflated series; can be added as a separate table |
| UNECE statistical database ("Gross Average Monthly Wages", 56 countries incl. BLR, MCO, SMR, LIE, TKM, UZB) | PXWeb API, data queries need **POST** | no explicit open licence found (terms page 403) | **no** — SafeHttpClient/`run.get` is GET-only and the licence is unverified; would add average wages for BLR and MCO |
| WageIndicator minimum-wage database | web | free data only for "private, internal, non-commercial use"; newer terms forbid processing by AI tools | **no** (licence) |
| National statistical offices (Belstat, IMSEE Monaco, NCSI Oman, Iraq CSO, Iran SCI, HCP Morocco, …) | per-country HTML/PDF/XLSX | varies | not implemented (one bespoke parser per country) — follow-up |

Indicator codes changed: `EAR_4MMN_CUR_NB` and `EAR_4MTH_SEX_ECO_CUR_NB` (named in the request) are not in the
2026-10 ILOSTAT table of contents (1,964 series). Current equivalents: `EAR_INEE_CUR_NB_A` ("Monthly minimum wage
by currency") and `EAR_EMTA_SEX_ECO_CUR_NB_A` / `EAR_EMTA_SEX_CUR_NB_A` ("Average monthly earnings of employees by
sex (, economic activity) and currency"). **Real wage growth** is not an ILOSTAT indicator (Global Wage Report
annex only) → no concept. **Modelled informality** (`EMP_2IFL_SEX_RT_A`) is published for regional/income
aggregates only (0 countries).

ILOSTAT quirk: `rplumber.ilo.org` answers **HTTP 200 with an empty body** to clients without a descriptive
User-Agent (curl's default). An empty body is a failed request, never "no data" (`check_body`, tested).

## How cells are kept honest

* `indicator` = ILOSTAT code + every classification verbatim (`UNE_2EAP_SEX_AGE_RT|sex=SEX_T|classif1=AGE_YTHADULT_YGE15`).
* Every ILOSTAT row's `flags` carries the source code + label (`"BA:829"`, `"LFS - Labour Force Survey"`), the
  observation status + label, and every note decoded from the archived dictionaries (currency, minimum-wage type,
  reference month, coverage, breaks, ILO calculations such as "Hourly figure multiplied by 40 hours and 4.33 weeks").
* The bulk service returns one ILO-selected best source per area-year (verified on every file: no duplicate
  cells). A file with two sources for one cell is refused, never deduplicated.
* ILO modelled (`*_2*`, `SDG_0111`) and national estimates are separate tables and separate concepts; modelled cells
  after the vintage year (Nov. 2025 → 2026–2027) carry `flags.projection`; ILO estimates inside national series
  (regional aggregates; "ILO - SIALC Estimates" minimum wages of 9 Latin American countries) are flagged
  `ilo_estimate_in_national_series`.
* Currencies: each LCU cell's currency is read from that row's own `T30` note (`Currency: FRA - Euro (EUR)` → unit
  `EUR per month`); USD and 2021 PPP $ cells are ILOSTAT's own conversions, labelled so; Eurostat EUR/PPS and OECD
  USD PPP are those providers' conversions; OECD national-currency rows fold `unit=NCU` in the indicator and keep the
  ISO 4217 code in `unit`/`flags.currency`. REVA converts nothing.
* Value-less provider rows keep `flags.missing` (`unreliable` for ILOSTAT status `U`, else `not_reported`). Eurostat
  flag **`m` = "missing value; data cannot exist"** → `missing: not_applicable`: the provider's statement that no
  national minimum wage exists — never a zero.
* Eurostat periods are semesters (`[Y-01-01, Y-07-01)`, `[Y-07-01, Y+1-01-01)`).

## Catalog (`registry/atlas/catalog/labour.yaml`)

Unemployment (modelled/national × total, women, men, youth), participation, employment-to-population, employment by
sector and status, informality, weekly hours, **minimum wage** (monthly local currency: ILOSTAT → Eurostat NAC →
OECD monthly NCU; monthly USD and 2021 PPP $ (ILOSTAT); EUR and PPS (Eurostat); annual and hourly NCU (OECD)),
**average wage** (mean monthly earnings local currency / USD / 2021 PPP $: ILOSTAT by-sex table then by-activity
totals; median monthly earnings; OECD average annual wage — a different statistic, separate concepts), labour
income share, working poverty, child labour, union density, collective bargaining coverage. A unit test checks that
every `source_order` series exists and that no concept mixes modelled and national series.

## Gaps and what was searched for them

* **Minimum wage — 174 of 250 entities have a value.** Eurostat (31) and OECD (37) only add second sources for
  countries ILOSTAT already covers. Of the 76 without a value: 8 are stated by Eurostat to have **no national minimum
  wage** (AUT, CHE, DNK, FIN, ISL, ITA, NOR, SWE — note ILOSTAT does list a *regional* (cantonal) minimum wage for
  CHE and a sectoral one for ISL; both are shown per source, nothing is reconciled); ~25 are uninhabited/dependent territories; the rest (e.g. ARE,
  BRN, DJI, ERI, NAM, PRK, SGP, SOM, SSD, STP, TON, YEM, ZWE, XKX, MAC, NRU, FSM, LCA, LIE, PRI) were searched in
  ILOSTAT (all of `EAR_INEE_*`), Eurostat `earn_mw_cur` and OECD `MW_CURP`. Several of them have no general statutory
  national minimum wage (e.g. Singapore: sectoral Progressive Wage Model only), but no open machine-readable source
  stating that was found, so nothing is asserted.
* **Average wage — 192 have a value.** Missing (non-territories): BLR, CAF, DMA, DZA (sector-level only, no total),
  GAB, GRD, HTI, IRN, IRQ, KNA, LBY, MAR, MCO, OMN, PRK, SSD, STP, TUV, VCT. Searched: ILOSTAT mean/median earnings (by
  sex, by activity), OECD AV_AN_WAGE. UNECE would add BLR and MCO (not implemented: POST + licence); national offices
  are the remaining route.
* **Unemployment — 228 have a value** (ILO modelled 188 + national 222). Missing: AND, VAT and 20 uninhabited or
  small territories.

## Requested changes for the lead / ATLASCORE

1. Seed hook `register_atlas_connector_versions` (already planned by ATLASCORE) so `atlas.ilostat`,
   `atlas.eurostat.earn_mw_cur`, `atlas.oecd.earnings` are registered in the deployed database.
2. Optional: a POST-capable budgeted request path in the atlas platform if UNECE is wanted (after its licence is
   confirmed by the owner).
3. `tests/invariants/test_invariant_30_budgets.py` currently fails on files that are not A-ILO's (`unsd.ama`,
   `worldbank.atlas` registered without budget; `connectors/wikidata.py` creates a client) — A-ILO files pass.

## Concept coverage (live ingest 2026-10-15)

Countries = crosswalk entities (of 250) with at least one reported value; years = first–last reference year.

| concept | countries | years | countries per source (in source_order) |
|---|---|---|---|
| `labour.unemployment_rate.ilo_modelled` | 188 | 1991–2027 | ilostat 188 |
| `labour.unemployment_rate.national` | 222 | 1947–2025 | ilostat 222, ilostat 217 |
| `labour.unemployment_rate.ilo_modelled.female` | 188 | 1991–2027 | ilostat 188 |
| `labour.unemployment_rate.national.female` | 221 | 1948–2025 | ilostat 221, ilostat 217 |
| `labour.unemployment_rate.ilo_modelled.male` | 188 | 1991–2027 | ilostat 188 |
| `labour.unemployment_rate.national.male` | 221 | 1948–2025 | ilostat 221, ilostat 217 |
| `labour.youth_unemployment_rate.ilo_modelled` | 188 | 1991–2027 | ilostat 188 |
| `labour.youth_unemployment_rate.national` | 217 | 1948–2025 | ilostat 217, ilostat 217 |
| `labour.participation_rate.ilo_modelled` | 188 | 1990–2027 | ilostat 188 |
| `labour.participation_rate.ilo_modelled.female` | 188 | 1990–2027 | ilostat 188 |
| `labour.participation_rate.ilo_modelled.male` | 188 | 1990–2027 | ilostat 188 |
| `labour.participation_rate.national` | 227 | 1946–2025 | ilostat 227, ilostat 225 |
| `labour.employment_to_population.ilo_modelled` | 188 | 1991–2027 | ilostat 188 |
| `labour.employment.total.ilo_modelled` | 188 | 1991–2025 | ilostat 188 |
| `labour.employment.agriculture.ilo_modelled` | 188 | 1991–2025 | ilostat 188 |
| `labour.employment.industry.ilo_modelled` | 188 | 1991–2025 | ilostat 188 |
| `labour.employment.services.ilo_modelled` | 188 | 1991–2025 | ilostat 188 |
| `labour.employment.employees.ilo_modelled` | 188 | 1991–2025 | ilostat 188 |
| `labour.employment.self_employed.ilo_modelled` | 188 | 1991–2025 | ilostat 188 |
| `labour.informal_employment_share.national` | 147 | 2000–2025 | ilostat 147, ilostat 147 |
| `labour.informal_employment_rate.ilo_modelled` | 0 | -–- | ilostat 0 |
| `labour.weekly_hours.ilo_modelled` | 188 | 2005–2027 | ilostat 188 |
| `labour.weekly_hours.national` | 161 | 1976–2025 | ilostat 161 |
| `labour.minimum_wage.monthly.local_currency` | 174 | 1960–2026 | ilostat 174, eurostat 31, oecd 29 |
| `labour.minimum_wage.monthly.usd` | 168 | 1980–2026 | ilostat 168 |
| `labour.minimum_wage.monthly.ppp2021` | 168 | 1991–2026 | ilostat 168 |
| `labour.minimum_wage.monthly.eur` | 31 | 1999–2026 | eurostat 31 |
| `labour.minimum_wage.monthly.pps` | 29 | 1999–2026 | eurostat 29 |
| `labour.minimum_wage.annual.national_currency` | 37 | 1960–2025 | oecd 37 |
| `labour.minimum_wage.hourly.national_currency` | 19 | 1960–2025 | oecd 19 |
| `labour.mean_monthly_earnings.local_currency` | 192 | 1969–2025 | ilostat 192, ilostat 191, ilostat 187, ilostat 164, ilostat 98 |
| `labour.mean_monthly_earnings.usd` | 177 | 1969–2025 | ilostat 177, ilostat 176, ilostat 174, ilostat 153, ilostat 92 |
| `labour.mean_monthly_earnings.ppp2021` | 169 | 1990–2025 | ilostat 169, ilostat 168, ilostat 166, ilostat 150, ilostat 82 |
| `labour.median_monthly_earnings.local_currency` | 150 | 1989–2025 | ilostat 150 |
| `labour.average_annual_wage.oecd.usd_ppp_constant` | 38 | 1990–2025 | oecd 38 |
| `labour.average_annual_wage.oecd.national_currency` | 41 | 1990–2025 | oecd 41 |
| `labour.labour_income_share.ilo_modelled` | 188 | 2004–2026 | ilostat 188 |
| `labour.working_poverty_rate.ilo_modelled` | 117 | 2000–2025 | ilostat 117 |
| `labour.child_labour_share.national` | 65 | 2001–2025 | ilostat 65 |
| `labour.trade_union_density` | 138 | 2000–2020 | ilostat 138 |
| `labour.collective_bargaining_coverage` | 99 | 2000–2020 | ilostat 99 |

## Minimum wage — countries with data (latest year per source)

ILOSTAT `EAR_INEE_CUR_NB_A`: statutory monthly minimum wage, local currency (currency from the row's own note) and ILOSTAT's USD conversion; type = ILOSTAT note 'Type of minimum wage'. Eurostat `earn_mw_cur` NAC (latest semester with a value). OECD `MW_CURP` latest year (monthly series where the minimum wage is set monthly, otherwise the pay period shown).

| ISO3 | country | ILOSTAT (year: value unit) | ILOSTAT USD/month | ILOSTAT minimum-wage type | Eurostat NAC | OECD |
|---|---|---|---|---|---|---|
| AFG | Afghanistan | 2024: 5500 AFN per month | 2020: 71.602 | Sectoral (private sector) | — | — |
| AGO | Angola | 2024: 70000 AOA per month | 2024: 80.474 | National floor | — | — |
| ALB | Albania | 2026: 50000 ALL per month | 2025: 460.812 | Singular national minimum wage | 2026-S2: 50000 | — |
| AND | Andorra | 2024: 1376.27 EUR per month | 2024: 1489.675 | Singular national minimum wage | — | — |
| ARG | Argentina | 2024: 271571 ARS per month | 2024: 296.898 | Singular national minimum wage | — | — |
| ARM | Armenia | 2024: 75000 AMD per month | 2024: 190.971 | Singular national minimum wage | — | — |
| ATG | Antigua and Barbuda | 2024: 1558.8 XCD per month | 2024: 577.333 | Singular national minimum wage | — | — |
| AUS | Australia | 2024: 4023.436 AUD per month | 2024: 2655.106 | National floor | — | 2025: 931.95 AUD per week |
| AZE | Azerbaijan | 2024: 345 AZN per month | 2024: 202.941 | Singular national minimum wage | — | — |
| BDI | Burundi | 2024: 4156.8 BIF per month | 2024: 1.441 | Regional (urban areas) | — | — |
| BEL | Belgium | 2026: 2234 EUR per month | 2025: 2386.56 | Singular national minimum wage | 2026-S2: 2234 | 2025: 2108.4 EUR per month |
| BEN | Benin | 2024: 52000 XOF per month | 2024: 85.76 | Singular national minimum wage | — | — |
| BFA | Burkina Faso | 2024: 45000 XOF per month | 2024: 74.215 | Rate of the SMIG when SMIG/SMAG system | — | — |
| BGD | Bangladesh | 2024: 12500 BDT per month | 2024: 108.128 | Singular national minimum wage | — | — |
| BGR | Bulgaria | 2026: 620 EUR per month | 2025: 622.63 | Singular national minimum wage | 2026-S2: 620 | 2025: 550.67 EUR per month |
| BHR | Bahrain | 2024: 300 BHD per month | 2024: 797.872 |  | — | — |
| BHS | Bahamas | 2024: 1125.8 BSD per month | 2024: 1125.8 | Singular national minimum wage | — | — |
| BIH | Bosnia and Herzegovina | 2024: 626 BAM per month | 2024: 346.322 | Singular national minimum wage | — | — |
| BLR | Belarus | 2024: 626 BYN per month | 2024: 192.841 | Singular national minimum wage | — | — |
| BLZ | Belize | 2024: 1039.2 BZD per month | 2024: 519.6 | Singular national minimum wage | — | — |
| BOL | Bolivia (Plurinational State of) | 2024: 2500 BOB per month | 2024: 361.795 | Singular national minimum wage | — | — |
| BRA | Brazil | 2024: 1412 BRL per month | 2024: 262.018 | National floor | — | 2025: 1518 BRL per month |
| BRB | Barbados | 2024: 1473.33 BBD per month | 2024: 736.665 | Singular national minimum wage | — | — |
| BTN | Bhutan | 2024: 3750 BTN per month | 2024: 44.819 | Occupational | — | — |
| BWA | Botswana | 2024: 1883.03 BWP per month | 2024: 138.834 | Sectoral | — | — |
| CAF | Central African Republic | 2024: 35000 XAF per month | 2024: 57.723 | Rate of the SMIG when SMIG/SMAG system | — | — |
| CAN | Canada | 2024: 2884 CAD per month | 2024: 2106.094 | Regional | — | 2025: 16.57 CAD per hour |
| CHE | Switzerland | 2024: 4212.224 CHF per month | 2024: 4784.222 | Regional | — | — |
| CHL | Chile | 2024: 500000 CLP per month | 2024: 529.901 | For adults | — | 2025: 519333 CLP per month |
| CHN | China | 2024: 1930 CNY per month | 2024: 268.149 | Regional | — | — |
| CIV | Côte d’Ivoire | 2024: 75000 XOF per month | 2024: 123.692 | Rate of the SMIG when SMIG/SMAG system | — | — |
| CMR | Cameroon | 2024: 43969 XAF per month | 2024: 72.515 | Singular national minimum wage | — | — |
| COD | Democratic Republic of the Congo | 2024: 183950 CDF per month | 2023: 78.61 | Singular national minimum wage | — | — |
| COG | Congo | 2024: 120000 XAF per month | 2024: 197.907 | Singular national minimum wage | — | — |
| COK | Cook Islands | 2024: 1974 NZD per month | — | Singular national minimum wage | — | — |
| COL | Colombia | 2024: 1300000 COP per month | 2024: 319.063 | Singular national minimum wage | — | 2025: 1423500 COP per month |
| COM | Comoros | 2024: 55000 KMF per month | 2024: 121.006 | Singular national minimum wage | — | — |
| CPV | Cabo Verde | 2024: 15000 CVE per month | 2024: 147.171 | Singular national minimum wage | — | — |
| CRI | Costa Rica | 2024: 358609 CRC per month | 2024: 696.18 | Occupational (unskilled workers) | — | 2025: 367108.56 CRC per month |
| CUB | Cuba | 2024: 2100 CUP per month | — | Singular national minimum wage | — | — |
| CYP | Cyprus | 2026: 1088 EUR per month | 2025: 1130 |  | 2026-S2: 1088 | — |
| CZE | Czechia | 2026: 22400 CZK per month | 2025: 950.553 | Singular national minimum wage | 2026-S2: 22400 | 2025: 20800 CZK per month |
| DEU | Germany | 2026: 2343 EUR per month | 2025: 2441.93 | Singular national minimum wage | 2026-S2: 2343 | 2025: 2161 EUR per month |
| DMA | Dominica | 2024: 1560 XCD per month | 2024: 577.778 | Singular national minimum wage | — | — |
| DOM | Dominican Republic | 2024: 19450 DOP per month | 2024: 326.533 | Average of minimum wages based on establishment size | — | — |
| DZA | Algeria | 2024: 20000 DZD per month | 2024: 149.195 | Singular national minimum wage | — | — |
| ECU | Ecuador | 2024: 39.167 USD per month | 2024: 39.167 | Singular national minimum wage | — | — |
| EGY | Egypt | 2024: 6000 EGP per month | 2024: 132.452 | Singular national minimum wage | — | — |
| ESP | Spain | 2026: 1425 EUR per month | 2025: 1560.53 | Singular national minimum wage | 2026-S2: 1425 | 2025: 1381.33 EUR per month |
| EST | Estonia | 2026: 946 EUR per month | 2025: 1001.18 | Singular national minimum wage | 2026-S2: 946 | 2025: 886 EUR per month |
| ETH | Ethiopia | 2011: 420 ETB per month | 2011: 24.853 |  | — | — |
| FJI | Fiji | 2024: 935.28 FJD per month | 2024: 412.326 | National floor | — | — |
| FRA | France | 2026: 1867 EUR per month | 2025: 2036.26 | Singular national minimum wage | 2026-S2: 1867 | 2025: 1801.8 EUR per month |
| GAB | Gabon | 2024: 150000 XAF per month | 2024: 247.384 | Singular national minimum wage | — | — |
| GBR | United Kingdom of Great Britain and Northern Ireland | 2024: 1981.4 GBP per month | 2024: 2532.417 | National floor | 2020-S2: 1445 | 2025: 12.02 GBP per hour |
| GEO | Georgia | 2024: 20 GEL per month | 2024: 7.351 | Singular national minimum wage | — | — |
| GHA | Ghana | 2024: 471.53 GHS per month | 2024: 33.249 | Singular national minimum wage | — | — |
| GIN | Guinea | 2024: 550000 GNF per month | 2020: 46.001 | Singular national minimum wage | — | — |
| GMB | Gambia | 2024: 1300 GMD per month | 2024: 19.213 | Singular national minimum wage | — | — |
| GNB | Guinea-Bissau | 2024: 19030 XOF per month | 2024: 31.385 | Singular national minimum wage | — | — |
| GNQ | Equatorial Guinea | 2023: 117304 XAF per month | 2023: 193.389 | Singular national minimum wage | — | — |
| GRC | Greece | 2026: 1073 EUR per month | 2025: 1160.51 | Singular national minimum wage | 2026-S2: 1073 | 2025: 1012.09 EUR per month |
| GRD | Grenada | 2024: 1200 XCD per month | 2024: 444.444 | Sectoral (manufacturing) | — | — |
| GTM | Guatemala | 2024: 3343.01 GTQ per month | 2024: 430.882 | Sectoral (manufacturing) | — | — |
| GUY | Guyana | 2024: 60147 GYD per month | 2024: 288.475 | Singular national minimum wage | — | — |
| HKG | China, Hong Kong Special Administrative Region | 2024: 6928 HKD per month | 2024: 887.769 | Sectoral | — | — |
| HND | Honduras | 2024: 11507.77 HNL per month | 2024: 464.036 | Sectoral (manufacturing) | — | — |
| HRV | Croatia | 2026: 1050 EUR per month | 2025: 1096.1 | Singular national minimum wage | 2026-S2: 1050 | 2025: 970 EUR per month |
| HTI | Haiti | 2024: 17810 HTG per month | 2024: 135.118 | Sectoral | — | — |
| HUN | Hungary | 2026: 322800 HUF per month | 2025: 823.469 | Singular national minimum wage | 2026-S2: 322800 | 2025: 290800 HUF per month |
| IDN | Indonesia | 2024: 5067381 IDR per month | 2024: 319.599 | Regional | — | — |
| IND | India | 2024: 4628 INR per month | 2024: 55.313 | National floor | — | — |
| IRL | Ireland | 2026: 2391 EUR per month | 2025: 2578.66 | Singular national minimum wage | 2026-S2: 2391 | 2025: 13.5 EUR per hour |
| IRN | Iran (Islamic Republic of) | 2024: 53073300 IRR per month | 2023: 1263.65 | Singular national minimum wage | — | — |
| IRQ | Iraq | 2024: 350000 IQD per month | 2024: 269.231 | Occupational (unskilled workers) | — | — |
| ISL | Iceland | 2024: 425985 ISK per month | 2024: 3087.787 | Sectoral | — | — |
| ISR | Israel | 2024: 5880.02 ILS per month | 2024: 1589.315 | Singular national minimum wage | — | 2025: 6155.76 ILS per month |
| JAM | Jamaica | 2024: 64950 JMD per month | 2024: 415.176 | National floor | — | — |
| JEY | Jersey | 2016: 1207.204 JEP per month | — | Singular national minimum wage | — | — |
| JOR | Jordan | 2024: 260 JOD per month | 2024: 366.197 | National floor | — | — |
| JPN | Japan | 2024: 182726 JPY per month | 2024: 1207.178 | Regional | — | 2025: 1071.5 JPY per hour |
| KAZ | Kazakhstan | 2024: 85000 KZT per month | 2024: 181.251 | Singular national minimum wage | — | — |
| KEN | Kenya | 2024: 16033.14 KES per month | 2024: 118.92 | Occupational | — | — |
| KGZ | Kyrgyzstan | 2024: 2534 KGS per month | 2024: 29.076 | Singular national minimum wage | — | — |
| KHM | Cambodia | 2024: 200 KHR per month | 2024: 0.049 | Singular national minimum wage | — | — |
| KIR | Kiribati | 2024: 270.4 AUD per month | 2024: 178.44 |  | — | — |
| KNA | Saint Kitts and Nevis | 2024: 1560 XCD per month | 2024: 577.778 | Singular national minimum wage | — | — |
| KOR | Republic of Korea | 2024: 2060740 KRW per month | 2024: 1511.499 | Singular national minimum wage | — | 2025: 2096270 KRW per month |
| KWT | Kuwait | 2024: 75 KWD per month | 2024: 244.546 | Sectoral (private and oil sectors) | — | — |
| LAO | Lao People's Democratic Republic | 2024: 1600000 LAK per month | 2024: 79.336 | Singular national minimum wage | — | — |
| LBN | Lebanon | 2024: 18000000 LBP per month | 2024: 201.117 | Singular national minimum wage | — | — |
| LBR | Liberia | 2024: 91 USD per month | 2024: 91 | Occupational (unskilled workers) | — | — |
| LBY | Libya | 2024: 1000 LYD per month | 2024: 206.945 | Singular national minimum wage | — | — |
| LKA | Sri Lanka | 2024: 17500 LKR per month | 2024: 57.925 | Singular national minimum wage | — | — |
| LSO | Lesotho | 2024: 1881 LSL per month | 2024: 102.626 | National floor | — | — |
| LTU | Lithuania | 2026: 1153 EUR per month | 2025: 1172.94 | Singular national minimum wage | 2026-S2: 1153 | 2025: 1038 EUR per month |
| LUX | Luxembourg | 2026: 2771 EUR per month | 2025: 3055.52 | Singular national minimum wage | 2026-S2: 2771 | 2025: 2682 EUR per month |
| LVA | Latvia | 2026: 780 EUR per month | 2025: 836.2 | Singular national minimum wage | 2026-S2: 780 | 2025: 740 EUR per month |
| MAR | Morocco | 2024: 3120 MAD per month | 2024: 313.809 | Rate of the SMIG when SMIG/SMAG system | — | — |
| MCO | Monaco | 2024: 2017.78 EUR per month | 2024: 2184.045 |  | — | — |
| MDA | Republic of Moldova | 2026: 6300 MDL per month | 2025: 316.495 |  | 2026-S2: 6300 | — |
| MDG | Madagascar | 2024: 262880 MGA per month | 2024: 58.09 | Rate of the SMIG when SMIG/SMAG system | — | — |
| MDV | Maldives | 2022: 5700 MVR per month | 2022: 370.452 | Singular national minimum wage | — | — |
| MEX | Mexico | 2024: 6467.201 MXN per month | 2024: 353.305 | National floor | — | 2025: 278.8 MXN per day |
| MHL | Marshall Islands | 2024: 624 USD per month | 2024: 624 | Singular national minimum wage | — | — |
| MKD | North Macedonia | 2026: 38507 MKD per month | 2025: 659.354 | Singular national minimum wage | 2026-S2: 38507 | — |
| MLI | Mali | 2024: 40000 XOF per month | 2024: 65.969 | Rate of the SMIG when SMIG/SMAG system | — | — |
| MLT | Malta | 2026: 994 EUR per month | 2025: 1085.93 | Singular national minimum wage | 2026-S2: 994 | 2025: 961 EUR per month |
| MMR | Myanmar | 2024: 124800 MMK per month | 2020: 90.329 | Singular national minimum wage | — | — |
| MNE | Montenegro | 2026: 670 EUR per month | 2025: 757.1 | Singular national minimum wage | 2026-S2: 670 | — |
| MNG | Mongolia | 2024: 660000 MNT per month | 2024: 194.691 | Singular national minimum wage | — | — |
| MOZ | Mozambique | 2024: 9497.5 MZN per month | 2024: 148.619 | Sectoral (manufacturing) | — | — |
| MRT | Mauritania | 2022: 3000 MRU per month | 2022: 81.224 | Rate of the SMIG when SMIG/SMAG system | — | — |
| MUS | Mauritius | 2024: 15000 MUR per month | 2024: 323.174 | Occupational (unskilled workers) | — | — |
| MWI | Malawi | 2024: 90000 MWK per month | 2023: 43.063 | Singular national minimum wage | — | — |
| MYS | Malaysia | 2024: 1500 MYR per month | 2024: 327.767 |  | — | — |
| NCL | New Caledonia | 2024: 166536 XPF per month | 2024: 1509.759 |  | — | — |
| NER | Niger | 2024: 42000 XOF per month | 2024: 69.268 | Singular national minimum wage | — | — |
| NGA | Nigeria | 2024: 65000 NGN per month | 2024: 43.95 | Singular national minimum wage | — | — |
| NIC | Nicaragua | 2024: 7692.75 NIO per month | 2024: 210.045 | Sectoral (manufacturing) | — | — |
| NLD | Netherlands (Kingdom of the) | 2026: 2338 EUR per month | 2025: 2537.98 | Singular national minimum wage | 2026-S2: 2338 | 2025: 2228.4 EUR per month |
| NPL | Nepal | 2024: 17300 NPR per month | 2024: 129.368 | National floor | — | — |
| NZL | New Zealand | 2024: 4009.58 NZD per month | 2024: 2426.654 | For adults | — | 2025: 936.5 NZD per week |
| OMN | Oman | 2024: 325 OMR per month | 2024: 845.254 | Singular national minimum wage | — | — |
| PAK | Pakistan | 2024: 37000 PKR per month | 2024: 132.816 | Occupational (unskilled workers) | — | — |
| PAN | Panama | 2024: 482.18 PAB per month | 2024: 482.18 | Sectoral (manufacturing) | — | — |
| PER | Peru | 2024: 1025 PEN per month | 2024: 273.151 | Singular national minimum wage | — | 2025: 1130 PEN per month |
| PHL | Philippines | 2024: 8302 PHP per month | 2024: 144.91 | Regional (lowest provincial rate in non-agriculture) | — | — |
| PLW | Palau | 2024: 883 USD per month | 2024: 883 | Singular national minimum wage | — | — |
| PNG | Papua New Guinea | 2024: 728 PGK per month | 2024: 188.956 | Singular national minimum wage | — | — |
| POL | Poland | 2026: 4806 PLN per month | 2025: 1240.779 | Singular national minimum wage | 2026-S2: 4806 | 2025: 4666 PLN per month |
| PRT | Portugal | 2026: 1073 EUR per month | 2025: 1146.95 | Singular national minimum wage | 2026-S2: 1073 | 2025: 1015 EUR per month |
| PRY | Paraguay | 2024: 2798309 PYG per month | 2024: 370.135 | National floor for adults | — | — |
| PSE | State of Palestine | 2013: 1450 ILS per month | — |  | — | — |
| QAT | Qatar | 2024: 1000 QAR per month | 2024: 274.725 | Singular national minimum wage | — | — |
| ROU | Romania | 2026: 4325 RON per month | 2025: 905.932 | Singular national minimum wage | 2026-S2: 4325 | 2025: 4050 RON per month |
| RUS | Russian Federation | 2024: 19242 RUB per month | 2024: 207.904 | National floor | — | 2025: 22440 RUB per month |
| RWA | Rwanda | 2024: 2600 RWF per month | 2024: 1.972 | Singular national minimum wage | — | — |
| SAU | Saudi Arabia | 2024: 4000 SAR per month | 2024: 1066.667 |  | — | — |
| SDN | Sudan | 2024: 425 SDG per month | 2022: 0.777 |  | — | — |
| SEN | Senegal | 2024: 64175 XOF per month | 2024: 105.839 | Rate of the SMIG when SMIG/SMAG system | — | — |
| SLB | Solomon Islands | 2024: 1664 SBD per month | 2024: 196.81 | Rate of the SMIG when SMIG/SMAG system | — | — |
| SLE | Sierra Leone | 2024: 800 SLE per month | 2023: 37.55 | Singular national minimum wage | — | — |
| SLV | El Salvador | 2024: 365 USD per month | 2024: 365 | Sectoral (manufacturing) | — | — |
| SMR | San Marino | 2024: 1728.38 EUR per month | 2024: 1870.799 | Sectoral | — | — |
| SRB | Serbia | 2026: 87207 RSD per month | 2025: 696.28 | Singular national minimum wage | 2026-S2: 87207 | — |
| SUR | Suriname | 2024: 4160 SRD per month | 2024: 125.375 | National floor | — | — |
| SVK | Slovakia | 2026: 915 EUR per month | 2025: 922.08 | Singular national minimum wage | 2026-S2: 915 | 2025: 816 EUR per month |
| SVN | Slovenia | 2026: 1482 EUR per month | 2025: 1444.14 | Singular national minimum wage | 2026-S2: 1482 | 2025: 1277.72 EUR per month |
| SWZ | Eswatini | 2024: 600 SZL per month | 2024: 32.755 | Occupational (unskilled workers) | — | — |
| SYC | Seychelles | 2024: 6628.364 SCR per month | 2024: 456.194 |  | — | — |
| SYR | Syrian Arab Republic | 2024: 14760 SYP per month | 2022: 3.897 | Singular national minimum wage | — | — |
| TCD | Chad | 2024: 60000 XAF per month | 2024: 98.954 | Rate of the SMIG when SMIG/SMAG system | — | — |
| TGO | Togo | 2024: 52500 XOF per month | 2024: 86.584 | Rate of the SMIG when SMIG/SMAG system | — | — |
| THA | Thailand | 2024: 8963 THB per month | 2024: 253.956 |  | — | — |
| TJK | Tajikistan | 2024: 800 TJS per month | 2024: 74.078 | Singular national minimum wage | — | — |
| TKM | Turkmenistan | 2024: 1280 TMT per month | — | Singular national minimum wage | — | — |
| TLS | Timor-Leste | 2024: 115 USD per month | 2024: 115 | Singular national minimum wage | — | — |
| TTO | Trinidad and Tobago | 2024: 3550 TTD per month | 2024: 525.916 | Singular national minimum wage | — | — |
| TUN | Tunisia | 2024: 491.504 TND per month | 2024: 158.17 | Rate of the SMIG when SMIG/SMAG system | — | — |
| TUR | Türkiye | 2026: 33030 TRY per month | 2025: 659.134 | Singular national minimum wage | 2026-S2: 33030 | 2025: 26005.5 TRY per month |
| TZA | United Republic of Tanzania | 2024: 150000 TZS per month | 2024: 57.739 | Sectoral (manufacturing) | — | — |
| UGA | Uganda | 2024: 130000 UGX per month | 2024: 34.6 | Singular national minimum wage | — | — |
| UKR | Ukraine | 2026: 8647 UAH per month | 2025: 191.897 |  | 2026-S2: 8647 | — |
| URY | Uruguay | 2024: 1855.667 UYU per month | 2024: 46.146 | Singular national minimum wage | — | — |
| USA | United States of America | 2026: 1257 USD per month | 2026: 1257 | Singular national minimum wage | 2026-S2: 1257 | 2025: 7.25 USD per hour |
| UZB | Uzbekistan | 2024: 1050000 UZS per month | 2024: 82.989 | Singular national minimum wage | — | — |
| VCT | Saint Vincent and the Grenadines | 2024: 1310 XCD per month | 2024: 485.185 | Sectoral (manufacturing) | — | — |
| VEN | Venezuela (Bolivarian Republic of) | 2024: 130 VES per month | — | Singular national minimum wage | — | — |
| VNM | Viet Nam | 2024: 4960000 VND per month | 2024: 205.256 | Regional | — | — |
| VUT | Vanuatu | 2024: 57165 VUV per month | 2024: 479.713 | Singular national minimum wage | — | — |
| WSM | Samoa | 2024: 832 WST per month | 2024: 302.111 | Rate of the SMIG when SMIG/SMAG system | — | — |
| ZAF | South Africa | 2024: 4776.85 ZAR per month | 2024: 260.622 | Occupational | — | — |
| ZMB | Zambia | 2024: 1300 ZMW per month | 2024: 49.682 | Occupational (unskilled workers) | — | — |

**174 countries/territories with a minimum-wage value**: ILOSTAT 174, Eurostat 31, OECD 37; 0 only outside ILOSTAT: none (Eurostat/OECD add second sources, not new countries).

**Provider statement that no national minimum wage exists** (Eurostat flag `m` = 'missing value; data cannot exist' in the latest semester, kept as value-less cells): AUT (Austria), CHE (Switzerland), DNK (Denmark), FIN (Finland), ISL (Iceland), ITA (Italy), NOR (Norway), SWE (Sweden).

**No minimum-wage value in any implemented source (76)**: ABW Aruba, AIA Anguilla, ALA Åland Islands, ARE United Arab Emirates, ASM American Samoa, ATA Antarctica, ATF French Southern Territories, AUT Austria, BES Bonaire, Sint Eustatius and Saba, BLM Saint Barthélemy, BMU Bermuda, BRN Brunei Darussalam, BVT Bouvet Island, CCK Cocos (Keeling) Islands, CHI Channel Islands, CUW Curaçao, CXR Christmas Island, CYM Cayman Islands, DJI Djibouti, DNK Denmark, ERI Eritrea, ESH Western Sahara, FIN Finland, FLK Falkland Islands (Malvinas), FRO Faroe Islands, FSM Micronesia (Federated States of), GGY Guernsey, GIB Gibraltar, GLP Guadeloupe, GRL Greenland, GUF French Guiana, GUM Guam, HMD Heard Island and McDonald Islands, IMN Isle of Man, IOT British Indian Ocean Territory, ITA Italy, LCA Saint Lucia, LIE Liechtenstein, MAC China, Macao Special Administrative Region, MAF Saint Martin (French Part), MNP Northern Mariana Islands, MSR Montserrat, MTQ Martinique, MYT Mayotte, NAM Namibia, NFK Norfolk Island, NIU Niue, NOR Norway, NRU Naoero, PCN Pitcairn, PRI Puerto Rico, PRK Democratic People's Republic of Korea, PYF French Polynesia, REU Réunion, SGP Singapore, SGS South Georgia and the South Sandwich Islands, SHN Saint Helena, SJM Svalbard and Jan Mayen Islands, SOM Somalia, SPM Saint Pierre and Miquelon, SSD South Sudan, STP Sao Tome and Principe, SWE Sweden, SXM Sint Maarten (Dutch part), TCA Turks and Caicos Islands, TKL Tokelau, TON Tonga, TUV Tuvalu, UMI United States Minor Outlying Islands, VAT Holy See, VGB British Virgin Islands, VIR United States Virgin Islands, WLF Wallis and Futuna Islands, XKX Kosovo, YEM Yemen, ZWE Zimbabwe.


## Average wage — countries with data (latest year per source)

ILOSTAT mean nominal monthly earnings of employees, both sexes, all activities, local currency (by-sex table, else the by-activity total) + ILOSTAT USD conversion + the survey it comes from; OECD average annual wage per FTE employee (USD PPP, constant prices, base year in the unit). Gross/net and coverage differ by source (see flags.notes).

| ISO3 | country | ILOSTAT mean monthly (year: value unit) | ILOSTAT USD | ILOSTAT source | OECD annual USD PPP |
|---|---|---|---|---|---|
| ABW | Aruba | 2010: 3013 AWG per month | 2010: 1683.24 | PC - Population and Housing Census | — |
| AFG | Afghanistan | 2020: 13202.245 AFN per month | 2020: 171.874 | LFS - Labour Force Survey | — |
| AGO | Angola | 2024: 111867.158 AOA per month | 2024: 128.606 | LFS - Employment Survey | — |
| ALB | Albania | 2022: 61898 ALL per month | 2022: 547.568 | ADM-IR - General Directorate of Taxation | — |
| AND | Andorra | 2008: 1946.74 EUR per month | 2008: 2863.265 | ADM - Other Administrative records and related sources | — |
| ARE | United Arab Emirates | 2009: 7450.614 AED per month | 2009: 2028.758 | LFS - Labour Force Survey | — |
| ARG | Argentina | 2025: 1006879.138 ARS per month | 2025: 811.442 | LFS - Permanent Household Survey, Urban | — |
| ARM | Armenia | 2023: 159067.084 AMD per month | 2023: 405.291 | HIES - Households Living Conditions Survey | — |
| ATG | Antigua and Barbuda | 2023: 2840.209 XCD per month | 2023: 1051.929 | LFS - Labour Force Survey | — |
| AUS | Australia | 2021: 5905.537 AUD per month | 2021: 4436.17 | HIES - Household, Income and Labour Dynamics Survey | 2025: 72018.439 |
| AUT | Austria | 2025: 5446.933 EUR per month | 2025: 6155.034 | HIES - EU Statistics on Income and Living Conditions | 2025: 78301.337 |
| AZE | Azerbaijan | 2022: 839.4 AZN per month | 2022: 493.765 | ES - Labour-related Establishment Survey | — |
| BDI | Burundi | 2020: 96843.791 BIF per month | 2020: 50.57 | HIES - Living Standards Survey | — |
| BEL | Belgium | 2025: 6233.682 EUR per month | 2025: 7044.061 | HIES - EU Statistics on Income and Living Conditions | 2025: 80008.652 |
| BEN | Benin | 2022: 84524.927 XOF per month | 2022: 135.693 | HIES - Monitoring Survey of the Modular and Integrated Survey of Household Living Conditions | — |
| BFA | Burkina Faso | 2025: 123467.988 XOF per month | 2025: 212.17 | LFS - Labour Force Survey | — |
| BGD | Bangladesh | 2024: 15516.525 BDT per month | 2024: 134.222 | LFS - Labour Force Survey | — |
| BGR | Bulgaria | 2025: 1392.171 EUR per month | 2025: 1573.153 | HIES - EU Statistics on Income and Living Conditions | — |
| BHR | Bahrain | 2020: 353 BHD per month | 2020: 938.83 | ADM-IR - Social Insurance organization Records | — |
| BHS | Bahamas | 2024: 2943.724 BSD per month | 2024: 2943.724 | LFS - Labour Force & Household Survey | — |
| BIH | Bosnia and Herzegovina | 2025: 1733.722 BAM per month | 2025: 998.729 | LFS - Labour Force Survey | — |
| BLZ | Belize | 2020: 1020 BZD per month | 2020: 510 | LFS - Labour Force Survey | — |
| BMU | Bermuda | 2013: 6221.1 BMD per month | 2013: 6221.1 | LFS - Labour Force Survey | — |
| BOL | Bolivia (Plurinational State of) | 2025: 3189.626 BOB per month | 2025: 461.596 | LFS - Continuous Employment Survey | — |
| BRA | Brazil | 2025: 3393.154 BRL per month | 2025: 607.299 | HS - Continuous National Household Sample Survey | — |
| BRB | Barbados | 2016: 2680.239 BBD per month | 2016: 1340.12 | HIES - Survey on Living Conditions | — |
| BRN | Brunei Darussalam | 2014: 1799.403 BND per month | 2014: 1420.162 | LFS - Labour Force Survey | — |
| BTN | Bhutan | 2025: 28072.673 BTN per month | 2025: 322.088 | LFS - Labour Force Survey | — |
| BWA | Botswana | 2024: 6545.615 BWP per month | 2024: 482.603 | HS - Multi-Topic Household Survey | — |
| CAN | Canada | 2025: 5761.948 CAD per month | 2025: 4121.253 | LFS - Labour Force Survey | 2025: 67900.601 |
| CHE | Switzerland | 2025: 6467.845 CHF per month | 2025: 7779.875 | LFS - Labour Force Survey | 2025: 92285.059 |
| CHL | Chile | 2024: 966377.602 CLP per month | 2024: 1024.169 | HIES - Supplemental Income Survey | 2023: 40626.111 |
| CHN | China | 2022: 5436 CNY per month | 2022: 806.868 | ADM - Labour and Social Security Records | — |
| CIV | Côte d’Ivoire | 2022: 128952.459 XOF per month | 2022: 206.734 | HIES - Living Standards Measurement Survey | — |
| CMR | Cameroon | 2014: 133259.848 XAF per month | 2014: 269.889 | HS - Household Survey | — |
| COD | Democratic Republic of the Congo | 2016: 146109.887 CDF per month | 2016: 144.62 | HS - Core Welfare Indicators Questionnaire | — |
| COG | Congo | 2009: 101095.162 XAF per month | 2009: 214.962 | LFS - Employment Survey | — |
| COK | Cook Islands | 2023: 10125.126 NZD per month | — | LFS - Labour Force Survey | — |
| COL | Colombia | 2025: 2022657.14 COP per month | 2025: 499.035 | LFS - Integrated Household Survey | 2024: 30133.361 |
| COM | Comoros | 2021: 105355.189 KMF per month | 2021: 253.285 | LFS - National Survey on Employment and the Informal Sector | — |
| CPV | Cabo Verde | 2015: 32193.003 CVE per month | 2015: 323.92 | LFS - Continuous Multi-Objective Survey Employment and Labor Market Statistics | — |
| CRI | Costa Rica | 2025: 606508.073 CRC per month | 2025: 1204.592 | LFS - Continuous Employment Survey | 2024: 44430.678 |
| CUB | Cuba | 2010: 448 CUP per month | — | LFS - National Employment Survey | — |
| CUW | Curaçao | 2018: 2675.09 ANG per month | 2018: 1494.464 | LFS - Labour Force Survey | — |
| CYP | Cyprus | 2025: 2908.82 EUR per month | 2025: 3286.966 | HIES - EU Statistics on Income and Living Conditions | — |
| CZE | Czechia | 2025: 2403.911 EUR per month | 2025: 2716.419 | HIES - EU Statistics on Income and Living Conditions | 2025: 43607.144 |
| DEU | Germany | 2022: 4412.67 EUR per month | 2022: 4646.541 | HIES - EU Statistics on Income and Living Conditions | 2025: 76284.517 |
| DJI | Djibouti | 2017: 95504.295 DJF per month | 2017: 537.383 | HS - Djiboutian Household Survey | — |
| DNK | Denmark | 2025: 6187.68 EUR per month | 2025: 6992.079 | HIES - EU Statistics on Income and Living Conditions | 2025: 78090.445 |
| DOM | Dominican Republic | 2025: 30072.139 DOP per month | 2025: 485.14 | LFS - Continuous National Labour Force Survey | — |
| ECU | Ecuador | 2025: 538.453 USD per month | 2025: 538.453 | LFS - National Survey on Employment, Unemployment and Underemployment | — |
| EGY | Egypt | 2024: 4953.561 EGP per month | 2024: 109.352 | LFS - Labour Force Sample Survey | — |
| ESP | Spain | 2025: 3162.954 EUR per month | 2025: 3574.138 | HIES - EU Statistics on Income and Living Conditions | 2025: 57778.662 |
| EST | Estonia | 2025: 2847.39 EUR per month | 2025: 3217.551 | HIES - EU Statistics on Income and Living Conditions | 2025: 41019.039 |
| ETH | Ethiopia | 2021: 4195.965 ETB per month | 2021: 95.943 | LFS - National Labor Force Survey | — |
| FIN | Finland | 2025: 5097.386 EUR per month | 2025: 5760.046 | HIES - EU Statistics on Income and Living Conditions | 2025: 63053.422 |
| FJI | Fiji | 2024: 1392.11 FJD per month | 2024: 613.724 | LFS - Employment, Unemployment Survey | — |
| FLK | Falkland Islands (Malvinas) | 2016: 2154.167 FKP per month | — | PC - Population Census | — |
| FRA | France | 2025: 4445.62 EUR per month | 2025: 5023.551 | HIES - EU Statistics on Income and Living Conditions | 2025: 60483.173 |
| GBR | United Kingdom of Great Britain and Northern Ireland | 2025: 3156.794 GBP per month | 2025: 4156.553 | LFS - Labour Force Survey | 2025: 66299.139 |
| GEO | Georgia | 2023: 866.97 GEL per month | 2023: 329.902 | HIES - Household Income and Expenditure Survey | — |
| GGY | Guernsey | 2016: 2620 GGP per month | — | ADM - Official Estimates | — |
| GHA | Ghana | 2025: 2728.49 GHS per month | 2025: 217.291 | LFS - Labour Force Survey | — |
| GIB | Gibraltar | 2017: 1298.177 GIP per month | 2012: 1897.33 | ES - Employment Survey | — |
| GIN | Guinea | 2019: 1765988.126 GNF per month | 2019: 192.292 | LFS - National Survey on Employment and the Informal Sector | — |
| GMB | Gambia | 2018: 3988.236 GMD per month | 2018: 82.826 | LFS - Labour Force Survey | — |
| GNB | Guinea-Bissau | 2022: 87940.423 XOF per month | 2022: 140.984 | HIES - Harmonized Survey on Household Living Conditions | — |
| GNQ | Equatorial Guinea | 2023: 235536.519 XAF per month | 2023: 388.309 | HIES - National Household Survey | — |
| GRC | Greece | 2025: 1933.671 EUR per month | 2025: 2185.048 | HIES - EU Statistics on Income and Living Conditions | 2025: 32412.152 |
| GTM | Guatemala | 2025: 3845.671 GTQ per month | 2025: 500.73 | LFS - Employment and Income Survey | — |
| GUM | Guam | 2016: 2065.332 USD per month | 2016: 2065.332 | ES - Current Employment Survey | — |
| GUY | Guyana | 2024: 136562.43 GYD per month | 2024: 654.976 | LFS - Labour Force Survey | — |
| HKG | China, Hong Kong Special Administrative Region | 2016: 15000 HKD per month | 2016: 1932.429 | LFS - General Household Survey | — |
| HND | Honduras | 2025: 11248.067 HNL per month | 2025: 433.407 | HS - Continuous Multi-Purpose Household Survey | — |
| HRV | Croatia | 2025: 2206.746 EUR per month | 2025: 2493.623 | HIES - EU Statistics on Income and Living Conditions | — |
| HUN | Hungary | 2025: 1141.872 EUR per month | 2025: 1290.315 | HIES - EU Statistics on Income and Living Conditions | 2025: 39144.852 |
| IDN | Indonesia | 2023: 2778128.806 IDR per month | 2023: 182.329 | LFS - National Labour Force Survey | — |
| IMN | Isle of Man | 2016: 2966.464 IMP per month | 2016: 4005.301 | SNA - Isle of Man National Income | — |
| IND | India | 2025: 22219.99 INR per month | 2025: 254.938 | LFS - Periodic Labour Force Survey | — |
| IRL | Ireland | 2025: 5041.263 EUR per month | 2025: 5696.627 | HIES - EU Statistics on Income and Living Conditions | 2025: 70112.825 |
| ISL | Iceland | 2020: 5558.226 EUR per month | 2020: 6348.606 | HIES - EU Statistics on Income and Living Conditions | 2024: 79051.4322285775 |
| ISR | Israel | 2021: 10928 ILS per month | 2021: 3383.074 | HIES - Income Survey | 2025: 57425.877 |
| ITA | Italy | 2025: 3534.346 EUR per month | 2025: 3993.811 | HIES - EU Statistics on Income and Living Conditions | 2025: 53864.157 |
| JAM | Jamaica | 2014: 37139.627 JMD per month | 2014: 334.789 | LFS - Labour Force Survey | — |
| JEY | Jersey | 2016: 3031 JEP per month | — | ES - Index of Average Earnings | — |
| JOR | Jordan | 2024: 351.324 JOD per month | 2024: 494.823 | LFS - Employment and Unemployment Survey | — |
| JPN | Japan | 2021: 307400 JPY per month | 2021: 2800.801 | ES - Wage Structure Survey | 2025: 50182.646 |
| KAZ | Kazakhstan | 2022: 309867 KZT per month | 2022: 673.382 | ADM-EBR - Businesses and Organizations register | — |
| KEN | Kenya | 2019: 13932.869 KES per month | 2019: 136.608 | HS - Continuous household survey | — |
| KGZ | Kyrgyzstan | 2022: 27684 KGS per month | 2022: 329.116 | ES - Survey of enterprises, institutions and organisations | — |
| KHM | Cambodia | 2025: 1160637.168 KHR per month | 2025: 289.188 | LFS - Labour Force Survey | — |
| KIR | Kiribati | 2023: 538.458 AUD per month | 2023: 357.734 | HIES - Household Income and Expenditure Survey | — |
| KOR | Republic of Korea | 2022: 4091000 KRW per month | 2022: 3167.765 | ES - Survey on labor conditions by employment type | 2025: 61259.161 |
| KWT | Kuwait | 2016: 353 KWD per month | 2016: 1168.342 | ES - Annual Survey of Establishments | — |
| LAO | Lao People's Democratic Republic | 2022: 2503995.654 LAK per month | 2022: 178.408 | LFS - Labour Force Survey | — |
| LBN | Lebanon | 2019: 1175959.958 LBP per month | 2019: 780.073 | LFS - Labour Force Survey | — |
| LBR | Liberia | 2017: 1308.613 USD per month | 2017: 1308.613 | LFS - Labour Force Survey | — |
| LCA | Saint Lucia | 2016: 2121.452 XCD per month | 2016: 785.723 | HIES - Survey on Living Conditions | — |
| LIE | Liechtenstein | 2018: 6675 CHF per month | 2018: 6825.91 | ADM - Other Administrative records and related sources | — |
| LKA | Sri Lanka | 2024: 48842.945 LKR per month | 2024: 161.67 | LFS - Labour Force Survey | — |
| LSO | Lesotho | 2024: 2494.242 LSL per month | 2024: 136.084 | LFS - Integrated Labour Force Survey | — |
| LTU | Lithuania | 2025: 2331.024 EUR per month | 2025: 2634.057 | HIES - EU Statistics on Income and Living Conditions | 2025: 58111.602 |
| LUX | Luxembourg | 2025: 8097.042 EUR per month | 2025: 9149.658 | HIES - EU Statistics on Income and Living Conditions | 2025: 98529.748 |
| LVA | Latvia | 2025: 2322.449 EUR per month | 2025: 2624.367 | HIES - EU Statistics on Income and Living Conditions | 2025: 44708.836 |
| MAC | China, Macao Special Administrative Region | 2018: 15800 MOP per month | 2018: 1957.261 | LFS - Employment Survey | — |
| MDA | Republic of Moldova | 2025: 8708.951 MDL per month | 2025: 501.152 | LFS - Labour Force Survey | — |
| MDG | Madagascar | 2015: 209429.056 MGA per month | 2015: 71.392 | LFS - National Survey on Employment and the Informal Sector | — |
| MDV | Maldives | 2019: 11551.156 MVR per month | 2019: 750.951 | HIES - Household Income and Expenditure Survey | — |
| MEX | Mexico | 2025: 10608.909 MXN per month | 2025: 551.47 | LFS - National Occupation and Employment Survey | 2025: 24463.122 |
| MHL | Marshall Islands | 2019: 798.347 USD per month | 2019: 798.347 | HIES - Household Income and Expenditure Survey | — |
| MKD | North Macedonia | 2014: 30416 MKD per month | 2014: 654.993 | ES - Structure of Earnings Survey | — |
| MLI | Mali | 2024: 106709.88 XOF per month | 2024: 175.989 | LFS - Continuous Household Employment Survey | — |
| MLT | Malta | 2021: 3337.27 EUR per month | 2021: 3946.989 | HIES - EU Statistics on Income and Living Conditions | — |
| MMR | Myanmar | 2020: 187872.518 MMK per month | 2020: 135.98 | LFS - Labour Force Survey | — |
| MNE | Montenegro | 2022: 883 EUR per month | 2022: 929.799 | ES - Survey on employees and wages | — |
| MNG | Mongolia | 2025: 1987681.054 MNT per month | 2025: 560.677 | LFS - Labour Force Survey | — |
| MOZ | Mozambique | 2022: 13571.38 MZN per month | 2022: 212.548 | HIES - Household Budget Survey | — |
| MRT | Mauritania | 2019: 60522.504 MRU per month | — | HIES - Living Standards Survey | — |
| MUS | Mauritius | 2024: 32444.135 MUR per month | 2024: 699.006 | LFS - Continuous Multi-Purpose Household Survey | — |
| MWI | Malawi | 2013: 21062.003 MWK per month | 2013: 57.798 | LFS - Labour Force Survey | — |
| MYS | Malaysia | 2020: 2933 MYR per month | 2020: 697.755 | ES - Salaries and Wages Survey | — |
| NAM | Namibia | 2018: 7233.303 NAD per month | 2018: 546.572 | LFS - Labour Force Survey | — |
| NCL | New Caledonia | 2017: 220065.411 XPF per month | 2017: 2083.3 | LFS - Labour Force Survey | — |
| NER | Niger | 2022: 75078.919 XOF per month | 2022: 120.365 | HIES - National Survey on Household Living Conditions | — |
| NGA | Nigeria | 2024: 76488.787 NGN per month | 2024: 51.718 | HS - General Household Survey | — |
| NIC | Nicaragua | 2014: 9897.546 NIO per month | 2014: 381.278 | HIES - National Household Survey on Measuring Living Conditions | — |
| NLD | Netherlands (Kingdom of the) | 2025: 5665.609 EUR per month | 2025: 6402.139 | HIES - EU Statistics on Income and Living Conditions | 2025: 80136.33 |
| NOR | Norway | 2025: 5698.918 EUR per month | 2025: 6439.778 | HIES - EU Statistics on Income and Living Conditions | 2025: 73461.545 |
| NPL | Nepal | 2017: 17801.374 NPR per month | 2017: 170.329 | LFS - Labour Force Survey | — |
| NRU | Naoero | 2006: 9359.371 AUD per month | 2006: 7047.86 | HIES - Household Income and Expenditure Survey | — |
| NZL | New Zealand | 2022: 5990 NZD per month | 2022: 3797.91 | ADM - Linked employer-employee data | 2025: 60896.229 |
| PAK | Pakistan | 2025: 26628.502 PKR per month | 2025: 94.715 | LFS - Labour Force Survey | — |
| PAN | Panama | 2025: 928.568 PAB per month | 2025: 928.568 | LFS - Labour Market Survey | — |
| PER | Peru | 2025: 2008.014 PEN per month | 2025: 562.932 | LFS - Permanent Employment Survey, National | — |
| PHL | Philippines | 2025: 19938.488 PHP per month | 2025: 346.726 | LFS - Labour Force Survey | — |
| PLW | Palau | 2023: 1042.366 USD per month | 2023: 1042.366 | HIES - Household Income and Expenditure Survey | — |
| PNG | Papua New Guinea | 1980: 252.32 PGK per month | 1980: 376.066 | ES - Labour-related Establishment Survey | — |
| POL | Poland | 2025: 2037.092 EUR per month | 2025: 2301.914 | HIES - EU Statistics on Income and Living Conditions | 2025: 49074.08 |
| PRI | Puerto Rico | 2020: 2232.785 USD per month | 2020: 2232.785 | PC - Population Census | — |
| PRT | Portugal | 2025: 1262.156 EUR per month | 2025: 1426.237 | LFS - Employment Survey | 2025: 44937.011 |
| PRY | Paraguay | 2025: 3235818.31 PYG per month | 2025: 428.844 | HS - Continuous Permanent Household Survey | — |
| PSE | State of Palestine | 2025: 3195.099 ILS per month | — | LFS - Labour Force Survey | — |
| QAT | Qatar | 2022: 11963 QAR per month | 2022: 3286.538 | LFS - Labor Force Sample Survey | — |
| REU | Réunion | 2012: 2714.258 EUR per month | 2012: 3487.279 | ADM-EOR - Register of the Employment Office | — |
| ROU | Romania | 2025: 1524.253 EUR per month | 2025: 1722.405 | HIES - EU Statistics on Income and Living Conditions | — |
| RUS | Russian Federation | 2021: 57240 RUB per month | 2021: 777.144 | ES - Establishments sample Survey on employees' wages by Occupation | — |
| RWA | Rwanda | 2024: 69850.796 RWF per month | 2024: 52.992 | LFS - Labour Force Survey | — |
| SAU | Saudi Arabia | 2022: 6748 SAR per month | 2022: 1799.467 | LFS - Labour Force Survey | — |
| SDN | Sudan | 2022: 155379.238 SDG per month | 2022: 284.182 | LFS - Labor Market Panel Survey | — |
| SEN | Senegal | 2025: 161914.774 XOF per month | 2025: 278.238 | LFS - National Employment Survey | — |
| SGP | Singapore | 2021: 4680 SGD per month | 2021: 3483.482 | LFS - Labour Force Survey | — |
| SHN | Saint Helena | 2002: 265.6 GBP per month | — | ADM - Tax returns and Administrative Records | — |
| SLB | Solomon Islands | 2005: 14206.702 SBD per month | 2005: 1886.712 | HIES - Household Income and Expenditure Survey | — |
| SLE | Sierra Leone | 2018: 2057586.761 SLL per month | — | HS - Integrated Household Survey | — |
| SLV | El Salvador | 2025: 507.036 USD per month | 2025: 507.036 | HS - Multi-purpose Household Survey | — |
| SMR | San Marino | 2022: 2200 EUR per month | 2022: 2316.6 | OE - Estimates of the Office of economic planning, data processing and statistics | — |
| SOM | Somalia | 2022: 247.087 USD per month | — | HIES - Integrated Household Budget Survey | — |
| SRB | Serbia | 2025: 1379.624 EUR per month | 2025: 1558.976 | HIES - Survey on Income and living Conditions | — |
| SUR | Suriname | 2022: 6525.226 SRD per month | 2022: 264.081 | HIES - Survey on Living Conditions | — |
| SVK | Slovakia | 2025: 2208.978 EUR per month | 2025: 2496.145 | HIES - EU Statistics on Income and Living Conditions | 2025: 37831.713 |
| SVN | Slovenia | 2025: 3269.403 EUR per month | 2025: 3694.425 | HIES - EU Statistics on Income and Living Conditions | 2025: 63375.61 |
| SWE | Sweden | 2025: 4232.515 EUR per month | 2025: 4782.742 | HIES - EU Statistics on Income and Living Conditions | 2025: 61443.422 |
| SWZ | Eswatini | 2025: 11416.27 SZL per month | 2025: 638.176 | LFS - Labour Force Survey | — |
| SYC | Seychelles | 2022: 14972 SCR per month | 2022: 1048.989 | ADM-IR - Records of the Treasury Department and Seychelles Revenue Commission | — |
| SYR | Syrian Arab Republic | 2010: 11344 SYP per month | 2008: 946.281 | LFS - Labour Force Survey | — |
| TCD | Chad | 2022: 128464.351 XAF per month | 2022: 205.952 | HIES - Modular and Integrated Household Survey on Living Conditions | — |
| TGO | Togo | 2022: 85868.237 XOF per month | 2022: 137.662 | HIES - Living Standards Measurement Survey | — |
| THA | Thailand | 2025: 16698.516 THB per month | 2025: 507.819 | LFS - Labour Force Survey | — |
| TJK | Tajikistan | 2019: 1335.52 TJS per month | 2019: 140.133 | EC - Labour reports | — |
| TKL | Tokelau | 2016: 758.781 NZD per month | — | HIES - Household Income and Expenditure Survey | — |
| TKM | Turkmenistan | 2003: 1750000 TMM per month | — | EC - Economic or Establishment Census | — |
| TLS | Timor-Leste | 2021: 257.594 USD per month | 2021: 257.594 | LFS - Labour Force Survey | — |
| TON | Tonga | 2023: 1566.627 TOP per month | 2023: 662.786 | LFS - Labour Force Survey | — |
| TTO | Trinidad and Tobago | 2016: 5758.393 TTD per month | 2016: 863.461 | LFS - Continuous Sample Survey of the Population | — |
| TUN | Tunisia | 2019: 739.678 TND per month | 2019: 252.068 | LFS - National Population and Employment Survey | — |
| TUR | Türkiye | 2025: 39486.797 TRY per month | 2025: 1000.811 | LFS - Household Labour Force Survey | 2024: 57274.919 |
| TZA | United Republic of Tanzania | 2025: 603972.525 TZS per month | 2024: 211.626 | LFS - Labour Force Survey | — |
| UGA | Uganda | 2021: 577677.938 UGX per month | 2021: 161.045 | LFS - National Labour Force Survey | — |
| UKR | Ukraine | 2022: 14847 UAH per month | 2022: 459.058 | ES - Survey of enterprises on matters pertaining to labor statistics | — |
| URY | Uruguay | 2024: 47938.864 UYU per month | 2024: 1192.13 | LFS - Continuous Household Survey Implantation | — |
| USA | United States of America | 2025: 6272.925 USD per month | 2025: 6272.925 | LFS - Current Population Survey | 2025: 86977.138 |
| UZB | Uzbekistan | 2022: 3882157.089 UZS per month | 2022: 351.322 | OE - Official Estimates | — |
| VEN | Venezuela (Bolivarian Republic of) | 2020: 2942239 VES per month | — | LFS - Household Sample Survey | — |
| VNM | Viet Nam | 2024: 8579562.366 VND per month | 2024: 355.043 | LFS - Labour Force Survey | — |
| VUT | Vanuatu | 2010: 40547.152 VUV per month | 2010: 418.418 | HIES - Household Income and Expenditure Survey | — |
| WLF | Wallis and Futuna Islands | 2019: 234174.38 XPF per month | — | HIES - Household Income and Expenditure Survey | — |
| WSM | Samoa | 2022: 1648.438 WST per month | 2022: 613.03 | LFS - Labour Force Survey | — |
| XKX | Kosovo | 2019: 476.748 EUR per month | 2019: 533.743 | ADM - Payroll and Payment Records | — |
| YEM | Yemen | 2014: 61426.083 YER per month | 2014: 285.849 | LFS - Labour Force Survey | — |
| ZAF | South Africa | 2020: 7980.252 ZAR per month | 2020: 484.853 | HIES - National Income Dynamic Survey | — |
| ZMB | Zambia | 2024: 4458.154 ZMW per month | 2024: 170.377 | LFS - Labour Force Survey | — |
| ZWE | Zimbabwe | 2015: 763.58 ZWL per month | — | ES - Quarterly Employment Inquiry | — |

**192 countries/territories with an average-wage value** (ILOSTAT 192, OECD 38). **None (58)**: AIA Anguilla, ALA Åland Islands, ASM American Samoa, ATA Antarctica, ATF French Southern Territories, BES Bonaire, Sint Eustatius and Saba, BLM Saint Barthélemy, BLR Belarus, BVT Bouvet Island, CAF Central African Republic, CCK Cocos (Keeling) Islands, CHI Channel Islands, CXR Christmas Island, CYM Cayman Islands, DMA Dominica, DZA Algeria, ERI Eritrea, ESH Western Sahara, FRO Faroe Islands, FSM Micronesia (Federated States of), GAB Gabon, GLP Guadeloupe, GRD Grenada, GRL Greenland, GUF French Guiana, HMD Heard Island and McDonald Islands, HTI Haiti, IOT British Indian Ocean Territory, IRN Iran (Islamic Republic of), IRQ Iraq, KNA Saint Kitts and Nevis, LBY Libya, MAF Saint Martin (French Part), MAR Morocco, MCO Monaco, MNP Northern Mariana Islands, MSR Montserrat, MTQ Martinique, MYT Mayotte, NFK Norfolk Island, NIU Niue, OMN Oman, PCN Pitcairn, PRK Democratic People's Republic of Korea, PYF French Polynesia, SGS South Georgia and the South Sandwich Islands, SJM Svalbard and Jan Mayen Islands, SPM Saint Pierre and Miquelon, SSD South Sudan, STP Sao Tome and Principe, SXM Sint Maarten (Dutch part), TCA Turks and Caicos Islands, TUV Tuvalu, UMI United States Minor Outlying Islands, VAT Holy See, VCT Saint Vincent and the Grenadines, VGB British Virgin Islands, VIR United States Virgin Islands.


## Unemployment rate, ages 15+ — countries with data

ILO modelled estimate (latest year up to the Nov. 2025 vintage; 2026–2027 projections excluded) and the latest national estimate with its survey.

| ISO3 | country | ILO modelled (year: %) | national (year: %, source) |
|---|---|---|---|
| ABW | Aruba | — | 2011: 8.9 (LFS - Labour Force Survey) |
| AFG | Afghanistan | 2025: 13.351 | 2021: 5.679 (LFS - Labour Force Survey) |
| AGO | Angola | 2025: 14.108 | 2025: 10.391 (LFS - Employment Survey) |
| AIA | Anguilla | — | 2002: 7.801 (LFS - Labour Force Survey) |
| ALB | Albania | 2025: 10.926 | 2024: 8.431 (LFS - Labour Force Survey) |
| ARE | United Arab Emirates | 2025: 2.174 | 2025: 1.744 (LFS - Labour Force Survey) |
| ARG | Argentina | 2025: 7.145 | 2025: 7.377 (LFS - Permanent Household Survey, Urban) |
| ARM | Armenia | 2025: 12.871 | 2023: 8.298 (HIES - Households Living Conditions Survey) |
| ASM | American Samoa | — | 2010: 9.202 (PC - Population Census) |
| ATG | Antigua and Barbuda | — | 2023: 5.37 (LFS - Labour Force Survey) |
| AUS | Australia | 2025: 4.09 | 2025: 4.17 (LFS - Labour Force Survey) |
| AUT | Austria | 2025: 5.582 | 2025: 5.778 (LFS - Labour Force Survey) |
| AZE | Azerbaijan | 2025: 5.457 | 2022: 5.7 (LFS - Labour Force Survey) |
| BDI | Burundi | 2025: 0.922 | 2020: 1.03 (HIES - Living Standards Survey) |
| BEL | Belgium | 2025: 5.906 | 2025: 6.184 (LFS - EU Labour Force Survey) |
| BEN | Benin | 2025: 1.589 | 2022: 1.685 (HIES - Monitoring Survey of the Modular and Integrated Survey of Household Living Conditions) |
| BFA | Burkina Faso | 2025: 3.477 | 2025: 7.085 (LFS - Labour Force Survey) |
| BGD | Bangladesh | 2025: 3.778 | 2024: 3.635 (LFS - Labour Force Survey) |
| BGR | Bulgaria | 2025: 3.536 | 2025: 3.518 (LFS - EU Labour Force Survey) |
| BHR | Bahrain | 2025: 1.102 | 2015: 0.894 (OE - Estimates of Central informatics organization) |
| BHS | Bahamas | 2025: 9.207 | 2024: 8.154 (LFS - Labour Force & Household Survey) |
| BIH | Bosnia and Herzegovina | 2025: 11.037 | 2025: 10.732 (LFS - Labour Force Survey) |
| BLR | Belarus | 2025: 3.38 | 2025: 2.531 (LFS - Labour Force Survey) |
| BLZ | Belize | 2025: 8.856 | 2025: 1.028 (LFS - Labour Force Survey) |
| BMU | Bermuda | — | 2014: 9 (LFS - Labour Force Survey) |
| BOL | Bolivia (Plurinational State of) | 2025: 2.967 | 2025: 2.518 (LFS - Continuous Employment Survey) |
| BRA | Brazil | 2025: 5.97 | 2025: 5.832 (HS - Continuous National Household Sample Survey) |
| BRB | Barbados | 2025: 6.524 | 2024: 6.03 (LFS - Labour Force Survey) |
| BRN | Brunei Darussalam | 2025: 5.277 | 2025: 5.354 (LFS - Labour Force Survey) |
| BTN | Bhutan | 2025: 3.227 | 2025: 3.359 (LFS - Labour Force Survey) |
| BWA | Botswana | 2025: 24.477 | 2024: 24.433 (HS - Multi-Topic Household Survey) |
| CAF | Central African Republic | 2025: 6.251 | — |
| CAN | Canada | 2025: 6.907 | 2025: 6.819 (LFS - Labour Force Survey) |
| CHE | Switzerland | 2025: 4.873 | 2025: 4.841 (LFS - Labour Force Survey) |
| CHI | Channel Islands | 2025: 6.169 | — |
| CHL | Chile | 2025: 8.974 | 2025: 8.894 (LFS - National Employment Survey) |
| CHN | China | 2025: 4.615 | 2022: 4.569 (LFS - National Labour Survey) |
| CIV | Côte d’Ivoire | 2025: 2.323 | 2022: 2.31 (HIES - Living Standards Measurement Survey) |
| CMR | Cameroon | 2025: 3.602 | 2021: 3.275 (HS - Household Survey) |
| COD | Democratic Republic of the Congo | 2025: 4.434 | 2020: 1.466 (HIES - Indicator Cluster Survey) |
| COG | Congo | 2025: 19.876 | 2012: 10 (LFS - Employment Survey) |
| COK | Cook Islands | — | 2019: 1.258 (LFS - Labour Force Survey) |
| COL | Colombia | 2025: 8.29 | 2025: 8.428 (LFS - Integrated Household Survey) |
| COM | Comoros | 2025: 3.819 | 2021: 4.394 (LFS - National Survey on Employment and the Informal Sector) |
| CPV | Cabo Verde | 2025: 11.939 | 2019: 11.3 (LFS - Continuous Multi-Objective Survey Employment and Labor Market Statistics) |
| CRI | Costa Rica | 2025: 6.843 | 2025: 6.342 (LFS - Continuous Employment Survey) |
| CUB | Cuba | 2025: 1.746 | 2018: 1.7 (LFS - National Employment Survey) |
| CUW | Curaçao | — | 2020: 19.1 (LFS - Labour Force Survey) |
| CYM | Cayman Islands | — | 2015: 4.2 (LFS - Labour Force Survey) |
| CYP | Cyprus | 2025: 4.924 | 2025: 4.354 (LFS - Labour Force Survey) |
| CZE | Czechia | 2025: 2.825 | 2025: 2.785 (LFS - Labour Force Sample Survey) |
| DEU | Germany | 2025: 3.711 | 2025: 3.824 (LFS - EU Labour Force Survey) |
| DJI | Djibouti | 2025: 26.015 | 2025: 38.782 (LFS - Survey on Labour Statistics) |
| DMA | Dominica | — | 2001: 10.96 (PC - Population Census) |
| DNK | Denmark | 2025: 5.529 | 2025: 6.316 (LFS - EU Labour Force Survey) |
| DOM | Dominican Republic | 2025: 5.092 | 2025: 5.166 (LFS - Continuous National Labour Force Survey) |
| DZA | Algeria | 2025: 11.633 | 2017: 11.707 (LFS - Household Employment Survey) |
| ECU | Ecuador | 2025: 3.308 | 2025: 3.232 (LFS - National Survey on Employment, Unemployment and Underemployment) |
| EGY | Egypt | 2025: 6.781 | 2024: 5.945 (LFS - Labour Force Sample Survey) |
| ERI | Eritrea | 2025: 5.978 | — |
| ESH | Western Sahara | 2025: 9.17 | — |
| ESP | Spain | 2025: 10.376 | 2025: 10.504 (LFS - EU Labour Force Survey) |
| EST | Estonia | 2025: 8.313 | 2025: 7.44 (LFS - Labour Force Survey) |
| ETH | Ethiopia | 2025: 3.323 | 2021: 3.935 (LFS - National Labor Force Survey) |
| FIN | Finland | 2025: 9.461 | 2025: 9.608 (LFS - EU Labour Force Survey) |
| FJI | Fiji | 2025: 5.347 | 2024: 5.361 (LFS - Employment, Unemployment Survey) |
| FLK | Falkland Islands (Malvinas) | — | 2016: 0.974 (PC - Population Census) |
| FRA | France | 2025: 7.542 | 2025: 7.7 (LFS - Employment Survey) |
| FRO | Faroe Islands | — | 2013: 4.034 (LFS - Labour Force Survey) |
| FSM | Micronesia (Federated States of) | — | 2014: 8.863 (HIES - Household Income and Expenditure Survey) |
| GAB | Gabon | 2025: 20.154 | 2024: 17.407 (LFS - National Employment and Unemployment Survey) |
| GBR | United Kingdom of Great Britain and Northern Ireland | 2025: 4.746 | 2025: 4.896 (LFS - Labour Force Survey) |
| GEO | Georgia | 2025: 12.097 | 2025: 8.196 (LFS - Labour Force Survey) |
| GGY | Guernsey | — | 2016: 1.217 (ADM - Official Estimates) |
| GHA | Ghana | 2025: 2.979 | 2025: 3.231 (LFS - Labour Force Survey) |
| GIB | Gibraltar | — | 1991: 6.445 (PC - Population Census) |
| GIN | Guinea | 2025: 5.163 | 2019: 5.018 (LFS - National Survey on Employment and the Informal Sector) |
| GLP | Guadeloupe | — | 2016: 23.8 (LFS - Employment Survey) |
| GMB | Gambia | 2025: 6.521 | 2025: 7.053 (LFS - Labour Force Survey) |
| GNB | Guinea-Bissau | 2025: 2.673 | 2022: 2.666 (HIES - Harmonized Survey on Household Living Conditions) |
| GNQ | Equatorial Guinea | 2025: 8.337 | 2023: 6.513 (HIES - National Household Survey) |
| GRC | Greece | 2025: 8.54 | 2025: 8.838 (LFS - Labour Force Survey) |
| GRD | Grenada | — | 2023: 6.34 (LFS - Labour Force Survey) |
| GRL | Greenland | — | 2015: 9.1 (ADM - Employment and Unemployment for permanent residents) |
| GTM | Guatemala | 2025: 2.604 | 2025: 2.327 (LFS - Employment and Income Survey) |
| GUF | French Guiana | — | 2016: 23.2 (LFS - Employment Survey) |
| GUM | Guam | 2025: 5.569 | 2016: 5.4 (LFS - Current Labour Force Survey) |
| GUY | Guyana | 2025: 11.962 | 2024: 6.579 (LFS - Labour Force Survey) |
| HKG | China, Hong Kong Special Administrative Region | 2025: 2.799 | 2025: 3.652 (LFS - General Household Survey) |
| HND | Honduras | 2025: 4.92 | 2025: 4.851 (HS - Continuous Multi-Purpose Household Survey) |
| HRV | Croatia | 2025: 5.002 | 2025: 4.853 (LFS - EU Labour Force Survey) |
| HTI | Haiti | 2025: 14.944 | 2012: 14.1 (HIES - Enquete sur les Conditions de Vie) |
| HUN | Hungary | 2025: 4.521 | 2025: 4.415 (LFS - Labour Force Survey) |
| IDN | Indonesia | 2025: 3.237 | 2023: 3.424 (LFS - National Labour Force Survey) |
| IMN | Isle of Man | — | 2016: 2.667 (PC - Population Census) |
| IND | India | 2025: 4.219 | 2025: 4.506 (LFS - Periodic Labour Force Survey) |
| IRL | Ireland | 2025: 4.632 | 2025: 4.642 (LFS - EU Labour Force Survey) |
| IRN | Iran (Islamic Republic of) | 2025: 8.301 | 2024: 7.631 (LFS - Labour Force Survey) |
| IRQ | Iraq | 2025: 15.485 | 2021: 16.173 (LFS - Labour Force Survey) |
| ISL | Iceland | 2025: 3.625 | 2025: 4.433 (LFS - EU Labour Force Survey) |
| ISR | Israel | 2025: 3.489 | 2025: 3 (LFS - Labour Force Survey) |
| ITA | Italy | 2025: 6.391 | 2025: 6.133 (LFS - EU Labour Force Survey) |
| JAM | Jamaica | 2025: 3.286 | 2024: 4.174 (LFS - Labour Force Survey) |
| JEY | Jersey | — | 2011: 4.825 (PC - Jersey Census) |
| JOR | Jordan | 2025: 16.535 | 2024: 16.804 (LFS - Employment and Unemployment Survey) |
| JPN | Japan | 2025: 2.451 | 2025: 2.5 (LFS - Labour Force Survey) |
| KAZ | Kazakhstan | 2025: 4.771 | 2022: 4.9 (LFS - Sample Survey of Employment) |
| KEN | Kenya | 2025: 5.449 | 2021: 5.585 (HS - Continuous household survey) |
| KGZ | Kyrgyzstan | 2025: 3.526 | 2023: 3.498 (LFS - Employment and Unemployment Survey) |
| KHM | Cambodia | 2025: 0.263 | 2025: 3.275 (LFS - Labour Force Survey) |
| KIR | Kiribati | — | 2023: 2.786 (HIES - Household Income and Expenditure Survey) |
| KNA | Saint Kitts and Nevis | — | 2001: 5.122 (PC - Population Census) |
| KOR | Republic of Korea | 2025: 2.683 | 2025: 2.789 (LFS - Economically Active Population Survey) |
| KWT | Kuwait | 2025: 2.181 | 2016: 2.164 (LFS - Labour Force Sample Survey) |
| LAO | Lao People's Democratic Republic | 2025: 1.187 | 2022: 1.209 (LFS - Labour Force Survey) |
| LBN | Lebanon | 2023: 10.985 | 2019: 11.301 (LFS - Labour Force Survey) |
| LBR | Liberia | 2025: 2.884 | 2017: 5.931 (LFS - Labour Force Survey) |
| LBY | Libya | 2025: 18.758 | 2012: 19 (LFS - Labour Force Survey) |
| LCA | Saint Lucia | 2025: 9.452 | 2025: 7.661 (LFS - Labour Force Survey) |
| LIE | Liechtenstein | — | 2017: 2.31 (ADM - Administrative Register) |
| LKA | Sri Lanka | 2025: 4.005 | 2024: 4.317 (LFS - Labour Force Survey) |
| LSO | Lesotho | 2025: 16.315 | 2024: 18.701 (LFS - Integrated Labour Force Survey) |
| LTU | Lithuania | 2025: 6.731 | 2025: 6.866 (LFS - EU Labour Force Survey) |
| LUX | Luxembourg | 2025: 6.343 | 2025: 6.476 (LFS - EU Labour Force Survey) |
| LVA | Latvia | 2025: 6.568 | 2025: 6.886 (LFS - EU Labour Force Survey) |
| MAC | China, Macao Special Administrative Region | 2025: 2.347 | 2020: 2.542 (LFS - Employment Survey) |
| MAR | Morocco | 2025: 9 | 2022: 11.79 (LFS - National Employment Survey) |
| MCO | Monaco | — | 2016: 6.332 (PC - Population Census) |
| MDA | Republic of Moldova | 2025: 1.505 | 2025: 1.279 (LFS - Labour Force Survey) |
| MDG | Madagascar | 2025: 3.041 | 2022: 3.186 (HS - Periodic Household Survey) |
| MDV | Maldives | 2025: 4.514 | 2019: 4.562 (HIES - Household Income and Expenditure Survey) |
| MEX | Mexico | 2025: 2.673 | 2025: 2.542 (LFS - National Occupation and Employment Survey) |
| MHL | Marshall Islands | — | 2021: 9.823 (PC - Population Census) |
| MKD | North Macedonia | 2025: 12.256 | 2025: 11.531 (LFS - Labour Force Survey) |
| MLI | Mali | 2025: 2.831 | 2024: 4.5 (LFS - Continuous Household Employment Survey) |
| MLT | Malta | 2025: 2.896 | 2025: 3.05 (LFS - Labour Force Survey) |
| MMR | Myanmar | 2025: 3.047 | 2020: 1.476 (LFS - Labour Force Survey) |
| MNE | Montenegro | 2025: 13.572 | 2024: 11.534 (LFS - Labour Force Survey) |
| MNG | Mongolia | 2025: 5.233 | 2025: 5.408 (LFS - Labour Force Survey) |
| MNP | Northern Mariana Islands | — | 2010: 11.174 (PC - Population Census) |
| MOZ | Mozambique | 2025: 6.628 | 2022: 6.789 (HIES - Household Budget Survey) |
| MRT | Mauritania | 2025: 10.319 | 2019: 10.416 (HIES - Living Standards Survey) |
| MSR | Montserrat | — | 2020: 8.332 (LFS - Labour Force Survey) |
| MTQ | Martinique | — | 2016: 17.6 (LFS - Employment Survey) |
| MUS | Mauritius | 2025: 5.635 | 2024: 5.827 (LFS - Continuous Multi-Purpose Household Survey) |
| MWI | Malawi | 2025: 5.072 | 2024: 15.406 (LFS - Labour Force Survey) |
| MYS | Malaysia | 2025: 3.764 | 2022: 3.9 (LFS - Labour Force Survey) |
| NAM | Namibia | 2025: 19.291 | 2023: 35.19 (PC - Population Census) |
| NCL | New Caledonia | 2025: 11.174 | 2023: 10.151 (LFS - Labour Force Survey) |
| NER | Niger | 2025: 0.394 | 2022: 0.403 (HIES - National Survey on Household Living Conditions) |
| NGA | Nigeria | 2025: 3.064 | 2024: 3.454 (LFS - Unemployment, Under-employment Watch) |
| NIC | Nicaragua | 2025: 5.04 | 2018: 5.2 (LFS - Continuous Household Survey) |
| NIU | Niue | — | 2022: 0.646 (PC - Population Census) |
| NLD | Netherlands (Kingdom of the) | 2025: 3.874 | 2025: 3.848 (LFS - EU Labour Force Survey) |
| NOR | Norway | 2025: 4.637 | 2025: 4.456 (LFS - EU Labour Force Survey) |
| NPL | Nepal | 2025: 10.465 | 2022: 12.436 (HIES - Living Standards Survey) |
| NRU | Naoero | — | 2021: 5.056 (PC - Population Census) |
| NZL | New Zealand | 2025: 5.076 | 2025: 5.263 (LFS - Household Labour Force Survey) |
| OMN | Oman | 2025: 3.251 | 2024: 3.3 (OE - Official Estimates) |
| PAK | Pakistan | 2025: 5.42 | 2025: 6.957 (LFS - Labour Force Survey) |
| PAN | Panama | 2025: 8.36 | 2025: 8.306 (LFS - Labour Market Survey) |
| PER | Peru | 2025: 5.117 | 2025: 4.319 (LFS - Permanent Employment Survey, National) |
| PHL | Philippines | 2025: 2.235 | 2025: 2.604 (LFS - Labour Force Survey) |
| PLW | Palau | — | 2023: 2.091 (HIES - Household Income and Expenditure Survey) |
| PNG | Papua New Guinea | 2025: 2.571 | 2022: 2.689 (HS - Socio-Demographic and Economic Survey) |
| POL | Poland | 2025: 2.976 | 2025: 3.018 (LFS - Labour Force Survey) |
| PRI | Puerto Rico | 2025: 5.991 | 2025: 5.6 (OE - Official Estimates) |
| PRK | Democratic People's Republic of Korea | 2025: 3.545 | — |
| PRT | Portugal | 2025: 6.164 | 2025: 6.006 (LFS - EU Labour Force Survey) |
| PRY | Paraguay | 2025: 4.803 | 2025: 4.527 (HS - Continuous Permanent Household Survey) |
| PSE | State of Palestine | 2022: 24.42 | 2025: 28.71 (LFS - Labour Force Survey) |
| PYF | French Polynesia | 2025: 11.662 | 2012: 21.787 (PC - Population Census) |
| QAT | Qatar | 2025: 0.13 | 2024: 0.103 (LFS - Labor Force Sample Survey) |
| REU | Réunion | — | 2016: 22.394 (LFS - Labour Force Survey) |
| ROU | Romania | 2025: 5.992 | 2025: 6.105 (LFS - Household Labour Force Survey) |
| RUS | Russian Federation | 2025: 2.133 | 2025: 2.135 (LFS - Labour Force Survey) |
| RWA | Rwanda | 2025: 11.363 | 2025: 5.363 (LFS - Labour Force Survey) |
| SAU | Saudi Arabia | 2025: 3.038 | 2025: 3.238 (LFS - Labour Force Survey) |
| SDN | Sudan | 2022: 7.53 | 2022: 7.53 (LFS - Labor Market Panel Survey) |
| SEN | Senegal | 2025: 2.735 | 2025: 4.571 (LFS - National Employment Survey) |
| SGP | Singapore | 2025: 2.818 | 2025: 3.255 (LFS - Labour Force Survey) |
| SHN | Saint Helena | — | 2010: 1.986 (OE - Official Estimates) |
| SLB | Solomon Islands | 2025: 1.431 | 2013: 0.694 (HIES - Household Income and Expenditure Survey) |
| SLE | Sierra Leone | 2025: 3.15 | 2018: 1.646 (HS - Integrated Household Survey) |
| SLV | El Salvador | 2025: 3.303 | 2025: 2.925 (HS - Multi-purpose Household Survey) |
| SMR | San Marino | — | 2022: 4.915 (OE - Estimates of the Office of economic planning, data processing and statistics) |
| SOM | Somalia | 2025: 18.948 | 2022: 11.086 (HIES - Integrated Household Budget Survey) |
| SRB | Serbia | 2025: 7.123 | 2025: 7.301 (LFS - Labour Force Survey) |
| SSD | South Sudan | 2023: 12.374 | 2008: 12.298 (PC - Population and Housing Census) |
| STP | Sao Tome and Principe | 2025: 9.132 | 2017: 7.956 (HIES - Household Budget Survey) |
| SUR | Suriname | 2025: 7.834 | 2022: 6.739 (HIES - Survey on Living Conditions) |
| SVK | Slovakia | 2025: 5.359 | 2025: 5.418 (LFS - Labour Force Survey) |
| SVN | Slovenia | 2025: 3.175 | 2025: 3.876 (LFS - Labour Force Survey) |
| SWE | Sweden | 2025: 8.694 | 2025: 8.74 (LFS - EU Labour Force Survey) |
| SWZ | Eswatini | 2025: 34.202 | 2025: 28.713 (LFS - Labour Force Survey) |
| SYC | Seychelles | — | 2025: 2.58 (LFS - Labour Force Survey) |
| SYR | Syrian Arab Republic | 2025: 13.569 | 2011: 14.896 (LFS - Labour Force Survey) |
| TCA | Turks and Caicos Islands | — | 2008: 8.299 (LFS - Labour Force Survey) |
| TCD | Chad | 2025: 1.057 | 2022: 3.109 (HIES - Modular and Integrated Household Survey on Living Conditions) |
| TGO | Togo | 2025: 1.995 | 2022: 1.965 (HIES - Living Standards Measurement Survey) |
| THA | Thailand | 2025: 0.781 | 2025: 0.604 (LFS - Labour Force Survey) |
| TJK | Tajikistan | 2025: 6.931 | 2016: 0.081 (LFS - Labour Force Survey) |
| TKM | Turkmenistan | 2025: 4.267 | 2010: 4 (OE - Official Estimates) |
| TLS | Timor-Leste | 2025: 1.592 | 2022: 1.537 (PC - Population Census) |
| TON | Tonga | 2025: 2.008 | 2025: 1.976 (HIES - Household Income and Expenditure Survey) |
| TTO | Trinidad and Tobago | 2025: 3.329 | 2024: 4.006 (LFS - Continuous Sample Survey of the Population) |
| TUN | Tunisia | 2025: 15.108 | 2023: 15.106 (LFS - National Population and Employment Survey) |
| TUR | Türkiye | 2025: 8.52 | 2025: 8.348 (LFS - Household Labour Force Survey) |
| TUV | Tuvalu | — | 2022: 7.318 (PC - Population Census) |
| TZA | United Republic of Tanzania | 2025: 1.572 | 2025: 2.776 (LFS - Labour Force Survey) |
| UGA | Uganda | 2025: 2.746 | 2024: 7.505 (PC - Population Census) |
| UKR | Ukraine | 2021: 9.834 | 2021: 9.834 (LFS - Labour Force Survey) |
| URY | Uruguay | 2025: 7.516 | 2025: 7.339 (LFS - Continuous Household Survey) |
| USA | United States of America | 2025: 4.198 | 2025: 4.282 (LFS - Current Population Survey) |
| UZB | Uzbekistan | 2025: 4.622 | 2020: 5.293 (LFS - Labour Force Survey) |
| VCT | Saint Vincent and the Grenadines | 2025: 18.003 | 2008: 18.792 (OE - Official Estimates) |
| VEN | Venezuela (Bolivarian Republic of) | 2025: 5.307 | 2020: 7.526 (LFS - Household Sample Survey) |
| VGB | British Virgin Islands | — | 1991: 3.291 (PC - Population Census) |
| VIR | United States Virgin Islands | 2025: 11.443 | — |
| VNM | Viet Nam | 2025: 1.523 | 2024: 1.529 (LFS - Labour Force Survey) |
| VUT | Vanuatu | 2025: 5.073 | 2025: 1.412 (LFS - Labour Force Survey) |
| WLF | Wallis and Futuna Islands | — | 2023: 0.643 (PC - Population Census) |
| WSM | Samoa | 2025: 4.996 | 2022: 5.048 (LFS - Labour Force Survey) |
| XKX | Kosovo | — | 2024: 10.834 (LFS - Labour Force Survey) |
| YEM | Yemen | 2025: 17.322 | 2014: 13.467 (LFS - Labour Force Survey) |
| ZAF | South Africa | 2025: 32.391 | 2025: 29.588 (LFS - Quarterly Labour Force Survey) |
| ZMB | Zambia | 2025: 5.918 | 2024: 5.67 (LFS - Labour Force Survey) |
| ZWE | Zimbabwe | 2025: 9.293 | 2025: 9.164 (LFS - Labour Force Survey) |

**228 countries/territories with an unemployment rate** (modelled 188, national 222). **None (22)**: ALA Åland Islands, AND Andorra, ATA Antarctica, ATF French Southern Territories, BES Bonaire, Sint Eustatius and Saba, BLM Saint Barthélemy, BVT Bouvet Island, CCK Cocos (Keeling) Islands, CXR Christmas Island, HMD Heard Island and McDonald Islands, IOT British Indian Ocean Territory, MAF Saint Martin (French Part), MYT Mayotte, NFK Norfolk Island, PCN Pitcairn, SGS South Georgia and the South Sandwich Islands, SJM Svalbard and Jan Mayen Islands, SPM Saint Pierre and Miquelon, SXM Sint Maarten (Dutch part), TKL Tokelau, UMI United States Minor Outlying Islands, VAT Holy See.


## Registered tables (live, throwaway database)

| table | rows | countries | indicators | unmapped provider geographies (kept, iso3 = null) |
|---|---|---|---|---|
| `ilostat.une_2eap_sex_age_rt_a` | 91692 | 188 | 9 | TWN + 87 ILO aggregates (X..) |
| `ilostat.une_deap_sex_age_rt_a` | 323059 | 223 | 102 | ANT, TWN + 87 ILO aggregates (X..) |
| `ilostat.eap_2wap_sex_age_rt_a` | 282528 | 188 | 27 | TWN + 87 ILO aggregates (X..) |
| `ilostat.eap_dwap_sex_age_rt_a` | 422297 | 227 | 112 | ANT, TWN + 87 ILO aggregates (X..) |
| `ilostat.emp_2wap_sex_age_rt_a` | 91692 | 188 | 9 | TWN + 87 ILO aggregates (X..) |
| `ilostat.emp_2emp_sex_eco_nb_a` | 781326 | 188 | 81 | TWN + 87 ILO aggregates (X..) |
| `ilostat.emp_2emp_sex_ste_nb_a` | 231504 | 188 | 24 | TWN + 87 ILO aggregates (X..) |
| `ilostat.sdg_0831_sex_eco_rt_a` | 57326 | 147 | 55 | — + 87 ILO aggregates (X..) |
| `ilostat.emp_2ifl_sex_rt_a` | 6264 | 0 | 3 | — + 87 ILO aggregates (X..) |
| `ilostat.how_2emp_sex_nb_a` | 18972 | 188 | 3 | TWN + 87 ILO aggregates (X..) |
| `ilostat.how_temp_sex_nb_a` | 12676 | 161 | 4 | — + 87 ILO aggregates (X..) |
| `ilostat.ear_inee_cur_nb_a` | 11337 | 174 | 3 | TWN |
| `ilostat.ear_emta_sex_cur_nb_a` | 23983 | 193 | 12 | ANT |
| `ilostat.ear_emta_sex_eco_cur_nb_a` | 561748 | 195 | 522 | ANT, TWN |
| `ilostat.ear_emtm_sex_cur_nb_a` | 15069 | 150 | 12 | — |
| `ilostat.lap_2gdp_noc_rt_a` | 6329 | 188 | 1 | TWN + 87 ILO aggregates (X..) |
| `ilostat.sdg_0111_sex_age_rt_a` | 42030 | 117 | 9 | — + 76 ILO aggregates (X..) |
| `ilostat.cld_xchl_sex_age_rt_a` | 3974 | 134 | 12 | — + 30 ILO aggregates (X..) |
| `ilostat.ilr_tumt_noc_rt_a` | 1287 | 138 | 1 | TWN |
| `ilostat.ilr_cbct_noc_rt_a` | 865 | 99 | 1 | — |
| `eurostat.earn_mw_cur` | 6226 | 39 | 3 | — |
| `oecd.earnings_mw_curp` | 4039 | 37 | 5 | — |
| `oecd.earnings_av_an_wage` | 4031 | 41 | 3 | OECD |
