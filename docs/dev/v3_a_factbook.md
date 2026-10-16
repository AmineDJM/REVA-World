# V3 A-FACTBOOK: universal gap fillers (CIA World Factbook + UNSD AMA)

Owner request: cover **every** country and territory, and when one source lacks a country, look
elsewhere. The multilateral sources (World Bank, IMF, UN, ILO …) skip small states and
territories. This agent adds two sources whose coverage is near-universal by design.

Status: connectors, catalogs, geo aliases, fixtures and tests done. A-LAW's Factbook legal
connector is merged into the same module (lead decision 2026-10-15; see "Merge with A-LAW").
One live ingest into a throwaway database (`reva_afactbook_live_d2f4564c`, dropped afterwards) on
2026-10-16 00:00–00:06 UTC through `run_atlas_ingest(mode="live")` (budget-metered, policy
budgets-1.3.4): `atlas.factbook` 261 requests / 9.8 MB / 143 s, `atlas.factbook_disputes` 259 / 11.8 MB
/ 131 s, `atlas.unsd_ama` 10 / 13.3 MB / 71 s; 0 refused, 0 failed, 530 raw artifacts archived,
5 atlas tables registered. (A first live run on 2026-10-15, before the merge with A-LAW, gave the
same Factbook counts; that database was dropped too.)
The dev database `reva` was never touched. Nothing is committed.

## Sources researched (2026-10-15)

| Option | Finding | Used? |
|---|---|---|
| **CIA World Factbook**, cia.gov | **Discontinued.** On 2026-02-04 the CIA published "Spotlighting The World Factbook as We Bid a Fond Farewell": "One of CIA's oldest and most recognizable intelligence publications, The World Factbook, has sunset." `www.cia.gov/the-world-factbook/` now redirects (HTTP 302) to that story. The last official downloadable archive (zip) was the 2020 edition. | No (gone) |
| **factbook/factbook.json** (GitHub, CC0) | Weekly auto-update of every Factbook profile from the CIA site since August 2021, one JSON per entity (GEC file names), 261 entities incl. the 5 oceans and the World. Last data update: commit `783ed27` "auto-update week 4" (2026-01-22), i.e. the final online edition. Later commits only touch the README and move `aj`/`am`/`gg` between region folders. Full history kept (332 commits). | **Yes**: pinned commit `144d697` (data = `783ed27`) |
| Same mirror, commit `b1e7065` (2024-04-18) | The CIA removed the field "Disputes - international" on 2024-04-25 (mirror commit `4a26af5`). `b1e7065` is the last edition with it (259 files). | **Yes**: disputes only |
| Factbook 2020 zip mirrors / Internet Archive | Older edition; less current than the 2026-01 mirror. | No |
| **UNSD National Accounts - Analysis of Main Aggregates (AMA)** | Bulk XLSX per series for **220 countries/areas, 1970–2024**, complete by construction (UNSD fills gaps with estimates). Covers Anguilla, Montserrat, Cook Islands, Curaçao, Sint Maarten, Cayman, Turks and Caicos, Bermuda, Greenland, Monaco, Liechtenstein, San Marino, Andorra, French Polynesia, New Caledonia, Puerto Rico, Cuba, **North Korea**, Somalia, Kosovo, Zanzibar, plus former states (USSR, Yugoslavia, Czechoslovakia, Netherlands Antilles, the two Yemens…). Terms: UNdata ("free of charge … provided that UNdata is cited"). API `unstats.un.org/unsd/amaapi/api/file/<id>` answers 200 (the AMA web page itself answered 503). | **Yes** (second fallback) |
| UNSD Statistical Yearbook (UNdata SYB) | Near-universal too, but A-UN already ingests table T02 (population, area); its other tables re-publish WPP/AMA/other agencies. | No (A-UN) |
| UN M49 standard area table | Already the backbone of the crosswalk (ALLC/ATLASCORE); no indicators. | No |
| SPC Pacific Data Hub (.Stat) | Regional (22 Pacific territories); Factbook + AMA already cover these entities for headline figures. | Not now (candidate if Pacific detail is wanted) |
| Wikipedia / national statistics offices | Not multilateral, too many sources, licences vary. | No |

**Why these two.** The Factbook is the only open source with one template for all 261 entities
(texts and numbers, incl. uninhabited territories, Antarctica and disputed areas). It is a US
government compilation of estimates of various years, so it ranks **last** in any best-available
order. AMA is the official UN series with the widest economic coverage and a long history
(1970–), so it fills GDP/GNI/population for territories and former states where WB/IMF have
nothing, with an official multilateral provenance.

## What was built

* `src/reva/adapters/connectors/atlas_factbook.py` — two atlas sources (`atlas_source()` returns
  both; budget key `factbook.json`):
  * `atlas.factbook` (v1.1.0; 261 requests, one fetch → three tables):
    `legal_2026_01` (A-LAW's legal fields, unchanged indicators), `figures_2026_01` (numbers),
    `texts_2026_01` (every text verbatim);
  * `atlas.factbook_disputes` (v1.0.0; 259 requests): `disputes_2024_04`.
* `src/reva/adapters/connectors/atlas_unsd_ama.py` — `atlas.unsd_ama` (10 requests): table
  `unsd.ama` (9 bulk files + the file list).
* `registry/sources/factbook.yaml` (rewritten for the merged connector), `registry/sources/unsd_ama.yaml`
  (new), `registry/atlas/geo_aliases/a_factbook.yaml` (schemes `factbook_gec` and `unsd_ama`),
  `registry/atlas/catalog/factbook.yaml` (123 concepts: 94 numeric, 29 text; domain `factbook`),
  `registry/atlas/catalog/unsd_ama.yaml` (17 concepts; domain `unsd_ama`),
  `registry/atlas/budget_requests/a_factbook.yaml` (merged by ATLASCORE: `factbook.json` 900/day,
  `unsd.ama` 20/day).
* Fixtures: `tests/fixtures/source/atlas.factbook/` (12 profiles: A-LAW's FR, US, SA, TW, KV + GL,
  WI, WE, VT, XX, KN, SV; blob bytes of the pinned commit, identical to the live responses — the
  France file has the sha256 A-LAW recorded live), `atlas.factbook_disputes/` (FR, WI, TW at
  `b1e7065`), `atlas.unsd_ama/` (recorded file list + files 2, 9, 30 trimmed to six countries:
  derived fixtures, every other workbook part unchanged).
* Tests: `tests/unit/test_atlas_factbook.py` (26), `tests/unit/test_atlas_unsd_ama.py` (13),
  `tests/integration/test_atlas_factbook_db.py` (2, `db`: archive → register → read back →
  idempotent, through `run_atlas_ingest(mode="fixture")`). A-LAW's tests updated for the merge and
  passing: `tests/unit/test_atlas_law.py`, `tests/integration/test_atlas_law_ingest.py`,
  `tests/live/test_atlas_law_live.py` (spec list handling only).

Run options (`--option` / job payload `options`): `gecs=fr,tw,…` restricts Factbook entities,
`files=2,9` restricts AMA files.

## Honesty rules as implemented

**Factbook**

* Every numeric cell: `flags.estimate = true`, `flags.provider_nature = "US government (CIA)
  compilation/estimate; not an official national statistic"`, `flags.est_marker` (whether the
  text says "est."), `flags.source_text` (the text the number was read from), `flags.provider_note`
  (the field note, e.g. "data are in 2021 dollars" for real GDP), `flags.edition` (mirror commit,
  2026-01-22 edition, discontinuation date).
* Period = the year **as stated**: "(2024 est.)" → 2024; "(2020-25 est.)" → [2020, 2026);
  "(July 2020 est.)" → 2020. Year-keyed fields ("Real GDP 2024/2023/2022", "Exchange rates
  2020–2024", "Military expenditures 2021–2025") give one row per stated year. A sub-field without
  a year (e.g. population "male"/"female", age structure 0-14) takes the year of the only dated
  sibling of the same field, flagged `year_from_sibling`. Otherwise (area, coastline, elevation,
  borders) the as-published day of the file, flagged `period_basis = as_published`.
* Numbers are parsed only when the text matches the field's expected unit (e.g. population must
  not be followed by "%"; birth rate must say "births/1,000 population"); "NA", "uninhabited",
  "negligible", "less than 1%" give no number (the text row keeps them). Never a filled zero.
* Lists (languages, religions, ethnic groups, export/import partners, land borders) are split
  only into clean "name N%" / "name N km" segments, flagged `transcription = "list split … by
  agent A-FACTBOOK, maintainer review pending"`, with the full source text in flags; partners
  carry `partner_gec`/`partner_iso3` (name → GEC from the profiles' conventional short forms +
  a documented alias list).
* Texts verbatim (HTML tags removed, entities decoded). The two "Diplomatic representation"
  sections (addresses, phone numbers) are left out.
* Disputed entities (`DISPUTED`: Western Sahara, Kosovo, Taiwan, West Bank, Gaza Strip, Paracel
  and Spratly Islands, Falklands) carry `flags.disputed_territory`; every text that mentions a
  dispute/claim/occupation carries `flags.mentions_dispute`; disputes rows carry
  `disputed_wording_verbatim = true`. The wording is the US government's, not a legal finding.
* Staleness: `flags.mirror_last_changed` (per-file `git log -1` date at the pinned commit, embedded
  in the module). Western Sahara was dropped by the Factbook in December 2020 (US recognition of
  Moroccan sovereignty); its file last changed on 2021-01-01 and every row says so
  (`stale_profile`), with that date as as-published period.
* Geography: scheme `factbook_gec` = 240 aliases, each the GeoNames FIPS 10-4 code of the same
  entity (GEC codes are FIPS successors) checked against the Factbook name; Taiwan TW → TWN since
  ATLASCORE added TWN to the crosswalk. Partial territories stay **unmapped** with
  `flags.partial_of_iso3`: WE West Bank and GZ Gaza Strip (⊂ PSE), SV Svalbard and JN Jan Mayen
  (⊂ SJM), UM Pacific Island Wildlife Refuges, WQ Wake, BQ Navassa (⊂ UMI). Non-crosswalk
  entities (AX Akrotiri, DX Dhekelia, AT Ashmore and Cartier, CR Coral Sea Islands, IP
  Clipperton, PF Paracel, PG Spratly) and aggregates (EE EU, XX World, 5 oceans) are kept unmapped
  and listed in each manifest. France `FR` includes the overseas departments (as WB's FRA).

**UNSD AMA**

* Values = the exact cell text (strict decimals), never rescaled; units spelled out (`current US$`,
  `constant 2020 US$`, `% annual growth (constant 2020 prices)`, `persons`, `national currency per
  US dollar` + `flags.currency`). UNSD footnotes of a (country, item, year) cell are copied into
  `flags.footnotes`. `flags.provider_nature` says the series mix official data and UNSD gap
  estimates without marking which.
* Scheme `unsd_ama` = M49 + Kosovo 412 → XKX (from the AMA country list). Former states (200, 230,
  530, 720, 736, 810, 886, 890) and Tanzania Mainland 835 / Zanzibar 836 (AMA has **no United
  Republic of Tanzania total**) stay unmapped and listed. AMA "China (mainland)" 156 → CHN (as the
  ISO entity, excluding Hong Kong, Macao and Taiwan).

## Merge with A-LAW (lead decision 2026-10-15)

A-LAW had created `atlas_factbook.py` (connector `atlas.factbook`, table `factbook.legal_2026_01`,
255 profiles, `fips` scheme) and `registry/sources/factbook.yaml` before this agent started; this
agent first built a separate module (`atlas_factbook_profiles.py`) to avoid clobbering in-flight
work, then merged on the lead's instruction:

* one module `atlas_factbook.py`, one source key `factbook.json`, one `registry/sources/factbook.yaml`;
  the separate module was removed;
* `atlas.factbook` keeps A-LAW's name and its `legal_2026_01` table **with the same indicators**
  (`legal_system`, `suffrage`, `constitution.*`, `citizenship.*`, `international_law_participation`,
  `suffrage.minimum_age`, `legal_system.families_mentioned`; A-LAW's `transcribed by agent A-LAW`
  markers unchanged), so `registry/atlas/catalog/law.yaml` is untouched; A-LAW's functions
  (`parse_profile`, `families_mentioned`, `items`, `clean_text`, `_AGE`, `ATLAS_DATASET`,
  `CONNECTOR_NAME`) are kept. Version bumped to 1.1.0 (code changed);
* changes visible in the legal table: 261 profiles fetched (oceans/World added; they have no legal
  fields except XX), geography through `factbook_gec` instead of `fips` (TW now TWN; West Bank and
  Svalbard **no longer** mapped to PSE / SJM — they are only parts of them), period = each file's
  as-published day (Western Sahara 2021-01-01, flagged stale), extra flags (edition, disputed,
  partial). Full-mirror parse: 2,372 rows, 240 entities (A-LAW: 2,369 / 240);
* fixtures merged into `tests/fixtures/source/atlas.factbook/` (A-LAW's 5 + 7 new);
* A-LAW's tests adjusted: 261 planned requests, two sources discovered, `factbook_gec` scheme,
  249 fixture_missing items, three tables.

## Live tables (2026-10-16)

| table | rows | crosswalk entities | provider geos | indicators | periods |
|---|---|---|---|---|---|
| `atlas.factbook.legal_2026_01` | 2,372 | 240 | 252 | 11 | as published 2026-01-22 (WI 2021-01-01) |
| `atlas.factbook.figures_2026_01` | 30,946 | 240 | 261 | 1,731 (94 fields + 6 list families) | stated years 1990–2025 (87 % in 2020–2025); 2,766 as-published rows |
| `atlas.factbook.texts_2026_01` | 73,924 | 240 | 261 | 742 paths | as published 2026-01-21/22 (older for 21 stale files) |
| `atlas.factbook.disputes_2024_04` | 259 | 240 | 259 | 1 | edition 2024-04-18 |
| `atlas.unsd.ama` | 794,323 | 210 | 220 | 73 | 1970–2024 |

Factbook: 261 entities → 240 crosswalk entities; unmapped and kept (21): AT, AX, BQ, CR, DX, EE, GZ,
IP, JN, OO, PF, PG, SV, UM, WE, WQ, XO, XQ, XX, ZH, ZN. Crosswalk entities **without** a Factbook
profile (11): ALA Åland, BES Caribbean Netherlands, GLP, GUF, MTQ, MYT, REU (French overseas
departments, inside the France profile), CHI (WB aggregate; GGY and JEY are separate profiles),
PSE (only West Bank + Gaza Strip, partial, unmapped), SJM (only Svalbard + Jan Mayen, partial),
UMI (only partial profiles). 2,089 figure rows took their year from a dated sibling. 248 text rows
mention a dispute or claim.

AMA: 220 areas → 210 crosswalk entities; unmapped (10): former Czechoslovakia 200, Ethiopia 230,
Netherlands Antilles 530, Democratic Yemen 720, Sudan 736, USSR 810, Yemen Arab Republic 886,
Yugoslavia 890, Tanzania Mainland 835, Zanzibar 836. Not in AMA (41 crosswalk entities): mostly
uninhabited areas and dependencies (see below), and TWN, TZA (only the two Tanzanian parts).

### Factbook figures: entities per field (crosswalk entities; stated years)

| field | entities | years | field | entities | years |
|---|---|---|---|---|---|
| area.total | 239 | as published | gdp.real_ppp | 219 | 2014–2024 |
| land_boundaries.total | 239 | as published | gdp.real_per_capita_ppp | 217 | 2014–2024 |
| coastline | 238 | as published | gdp.real_growth | 215 | 2015–2024 |
| elevation highest/lowest | 240 | as published | gdp.official_exchange_rate | 224 | 2014–2024 |
| land use (6 shares) | 225–238 | 2011–2023 | inflation | 211 | 2015–2024 |
| population.total | 234 | 2020–2025 | exports / imports | 211 / 210 | 2009–2024 |
| population male/female | 231 | 2021–2025 | current_account_balance | 192 | 2013–2024 |
| age structure (3) | 227–228 | 2020–2024 | budget revenues/expenditures | 210 | 2014–2024 |
| median age | 228 | 2020–2025 | public_debt_gdp | 200 | 2013–2023 |
| birth / death rate | 227 / 228 | 2012–2025 | taxes_revenues_gdp | 151 | 2015–2023 |
| net_migration_rate | 227 | 2020–2025 | unemployment_rate | 188 | 2019–2024 |
| population_growth_rate | 233 | 2014–2025 | youth_unemployment.total | 187 | 2021–2024 |
| urban_population / rate | 227 | 2015–2023 | labor_force | 189 | 2010–2024 |
| life expectancy (3) | 226–227 | 2016–2024 | gini / income shares | 147 / 145 | 2015–2024 |
| infant mortality | 226 | 2020–2025 | poverty_line_population | 136 | 2015–2024 |
| total fertility rate | 226 | 2020–2025 | remittances_gdp | 198 | 2013–2024 |
| maternal mortality | 194 | 2023 | reserves / external_debt | 176 / 122 | 2015–2024 |
| literacy total / female | 120 / 128 | 2015–2024 | exchange_rate | 220 | 2013–2024 |
| health / education exp. (% GDP) | 190 / 190 | 2015–2024 | military_expenditure_gdp | 166 | 2015–2025 |
| physicians / hospital beds | 203 / 156 | 2012–2023 | refugees / IDPs / stateless | 172 / 117 / 95 | 2023–2024 |
| internet users / mobile per 100 | 232 / 223 | 2004–2024 | CO2 emissions | 211 | 2023 |

List families: religion shares 218 entities (1,171 rows), ethnic groups 199 (1,208), languages 114
(673; many profiles list languages without %), export/import partners 230 each (~1,140 rows),
land borders 162 entities (660 neighbour rows, `partner_iso3` resolved for all but French DOMs, West Bank/Gaza and
Guantanamo). Texts: median 323 text fields per
entity (climate, terrain, natural resources, government type, capital, independence, constitution,
legal system, suffrage, executive/legislative/judicial branches, currency, flag, anthem, economic
overview, transnational issues …).

## Gaps filled: entities other sources miss

Legend: Factbook figures present (pop = population, area, GDP = real GDP PPP, life exp., unemp.);
"texts" = number of Factbook text fields; AMA = GDP current US$ series.

### Crosswalk entities without a World Bank code (34)

| ISO3 | entity | Factbook figures | texts | AMA |
|---|---|---|---|---|
| AIA | Anguilla | pop, area, GDP, life exp. | 174 | 1970–2024 |
| COK | Cook Islands | pop, area, GDP, life exp. | 222 | 1970–2024 |
| MSR | Montserrat | pop, area, GDP, life exp. | 189 | 1970–2024 |
| TWN | Taiwan | pop, area, GDP, life exp., unemp. | 277 | — |
| JEY | Jersey | pop, area, GDP, life exp., unemp. | 187 | — |
| GGY | Guernsey | pop, area, life exp., unemp. | 190 | — |
| NIU | Niue | pop, area, GDP | 172 | — |
| TKL | Tokelau | pop, area, GDP | 148 | — |
| SPM | Saint Pierre and Miquelon | pop, area, GDP, life exp. | 170 | — |
| WLF | Wallis and Futuna | pop, area, life exp. | 171 | — |
| BLM | Saint Barthélemy | pop, area, life exp. | 132 | — |
| SHN | Saint Helena, Ascension and Tristan da Cunha | pop, area, life exp. | 167 | — |
| ESH | Western Sahara (stale 2021 profile, disputed) | pop, area, life exp. | 153 | — |
| FLK | Falkland Islands (disputed) | pop, area | 165 | — |
| VAT | Holy See | pop, area | 108 | — |
| NFK | Norfolk Island | pop, area | 119 | — |
| CXR | Christmas Island | pop, area | 113 | — |
| CCK | Cocos (Keeling) Islands | pop, area | 99 | — |
| PCN | Pitcairn | pop, area | 103 | — |
| ATA | Antarctica | area | 49 | — |
| IOT | British Indian Ocean Territory | area | 79 | — |
| SGS | South Georgia and South Sandwich | area | 43 | — |
| BVT | Bouvet Island | area | 41 | — |
| HMD | Heard and McDonald Islands | area | 38 | — |
| ATF | French Southern Territories | — (texts only) | 54 | — |
| ALA, BES, GLP, GUF, MTQ, MYT, REU, SJM, UMI | no own Factbook profile (see above) | — | 0 | — |

### World Bank economies missing from IMF WEO (21, from `docs/dev/v3_a_imf.md`)

All but CHI are filled by the Factbook with population and area, 16 with GDP (ASM, BMU, CUB,
CUW, CYM, FRO, GRL, MCO, MNP, NCL, PRK, PYF, SXM, TCA, VGB, VIR); AMA adds 1970–2024 GDP, GNI and
population for 12 of them (BMU, CUB, CUW, CYM, GRL, MCO, NCL, PRK, PYF, SXM, TCA, VGB) — notably
**North Korea and Cuba**, for which IMF has only IMTS merchandise trade.

### Crosswalk entities in no IMF table (37)

Factbook fills 26 with at least area/population (GDP for COK, JEY, MCO, MNP, NIU, PRI, SPM, TKL,
VGB, VIR); AMA fills COK, MCO, PRI, VGB with official 1970–2024 national accounts. Still empty:
ALA, BES, CHI, GLP, GUF, MTQ, MYT, REU, SJM, UMI (no own profile) and ATF (texts only).

### Entities in no UN table (13, from `docs/dev/v3_a_un.md`)

Factbook gives population for CCK, CXR, NFK, PCN; area for ATA, BVT, HMD, IOT, SGS (uninhabited);
texts for ATF. ALA, SJM, UMI remain without any figure (Åland is inside Finland's profile;
Svalbard/Jan Mayen and the US minor islands are profiled only as parts, kept unmapped).

### Remaining gaps and where else to look

* French overseas departments (GLP, GUF, MTQ, MYT, REU): inside the France profile and the WB/IMF
  France aggregates; INSEE / Eurostat (NUTS2 FRY*) have them separately (not open-ended here).
* Åland (ALA): Statistics Åland (ÅSUB) / Statistics Finland; BES: CBS Netherlands (Caribbean
  Netherlands); SJM: Statistics Norway (Svalbard population).
* Taiwan: Factbook only (+ IMF WEO "Taiwan Province of China" with A-IMF; now mapped to TWN).
* Tanzania in AMA: only Mainland/Zanzibar parts (unmapped) — TZA from WB/IMF.

## Cross-owner notes / requests

* **Lead (seed)**: `register_atlas_connector_versions` will register `atlas.factbook` 1.1.0,
  `atlas.factbook_disputes` 1.0.0 and `atlas.unsd_ama` 1.0.0 (sources `factbook.json`, `unsd.ama`
  are in `registry/sources`).
* **A-LAW / docs**: `docs/dev/v3_a_law.md` still describes the legal table as 255 profiles mapped
  via `fips`; the table now uses `factbook_gec` (see "Merge"). Their live test
  (`tests/live/test_atlas_law_live.py`) runs `atlas.factbook`, which now fetches 261 files and
  registers three tables.
* **ATLASCORE**: thanks for adding TWN; the `fips` scheme still maps FIPS WE → PSE and SV → SJM
  (correct for GeoNames, but a Factbook "West Bank" or "Svalbard" is only part of those entities):
  sources using Factbook codes should use `factbook_gec`. Consider crosswalk entries for
  Akrotiri/Dhekelia (UK sovereign base areas) only if another source reports them.
* **LINKS**: `factbook.figures_2026_01` has country–country rows: `land_boundary|partner=<name>`
  (km, `flags.partner_iso3`), `export_partner_share|name=…` / `import_partner_share|name=…`
  (%, `flags.partner_iso3`); `factbook.disputes_2024_04` has dispute texts per country.
* **KGCULT**: `language_share|name=…`, `religion_share|name=…`, `ethnic_group_share|name=…`
  (Factbook names verbatim, with % and year).
* **UI3 / NLQA**: Factbook cells must be displayed with "US government estimate (CIA World
  Factbook, final edition Jan 2026)"; texts of disputed entities with the flag.

## How to re-run

```bash
uv run python -m reva.atlas.ingest atlas.factbook            # 261 requests (legal + figures + texts)
uv run python -m reva.atlas.ingest atlas.factbook_disputes   # 259 requests
uv run python -m reva.atlas.ingest atlas.unsd_ama            # 10 requests
uv run python -m reva.atlas.ingest atlas.factbook --fixture --option gecs=fr,tw,kv
```
