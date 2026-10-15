# V3 — A-HISTECON: long-run history layer (Maddison, Clio-Infra, Gapminder, HYDE, Correlates of War)

Agent A-HISTECON, 2026-10-15. One live ingest of all five sources into a throwaway database
(`reva_histecon_live_34885ed2`, dropped afterwards, objects store deleted) through the atlas platform
(`reva.atlas.ingest.run_atlas_ingest`, live mode, budget-metered with policy `budgets-1.3.2`). The dev database
`reva` was never touched. All tests offline (captured fixtures, several trimmed — each file's manifest says so).

## 1. Sources: access, licence, decision

| Source | Access (bulk) | Licence (checked 2026-10-15) | Decision |
|---|---|---|---|
| **Maddison Project Database 2023** | DataverseNL `doi:10.34894/INZBF2`: record + `mpd2023_web.xlsx` (4.9 MB, SHA-1 checked) | CC BY 4.0 (record; checked every run, other licence = blocked) | ingested: `maddison.mpd2023` |
| **Clio-Infra** | DataverseNL collection `clioinfra` (IISH deposits migrated): search API → 89 dataset records → `*-historical.xlsx` (SHA-1 checked) | per dataset: CC0-1.0 (85) or CC BY-SA 4.0 (Global Hunger, Infant Mortality, Numeracy, Gender Equality of Numeracy) — on every row | ingested: `clio.infra` (90 indicator files). clio-infra.eu states no licence: its "Compact" files (a derived view, checked identical for Height) are not used. `Geocoder` is restricted (HTTP 403): skipped; `topics.xlsx` (index) skipped |
| **Gapminder** | `raw.githubusercontent.com/open-numbers/ddf--gapminder--fasttrack` (datapackage, concepts, geo entities, pop/lex/gdp_pcap datapoints) | CC BY 4.0 (gapminder.org/free-material) | ingested: `gapminder.fasttrack`, **estimates only** (see §3) |
| **HYDE** | 3.3: Utrecht Yoda portal behind an **Anubis proof-of-work bot challenge** — not circumvented. 3.2.1: DANS `doi:10.17026/dans-25g-gez3`; country summaries sit inside a 5.3 GB zip → **one HTTP Range request for the last 1.5 MB** (txt members + zip64 central directory), Deflate64 members inflated by a bounded pure-Python decoder, CRC-32 checked | readme: "CC BY 3.0 applies to all of the HYDE data"; DANS record: CC0-1.0 → stricter CC BY 3.0 applied, both recorded | ingested: `hyde.hyde321` (9 variables) |
| **Correlates of War** | correlatesofwar.org bulk files: States2024, System2024, MajorPowers2024, NMCv7, Inter-/Extra-State War v4.0 (+ CRAN `countrycode` 1.9.0 tarball for KGGEOPOL's code panel) | **not an open licence**: COW FAQ "data be properly cited; COW data users cannot charge for access" → `open_licence: false`, citation on every row; flagged for maintainer review | ingested: `cow.nmc7`, `cow.system2024`, `cow.wars4` + KG statements |
| OWID historical | not needed: every original above was available | — | not used |

Not ingested from COW: Intra-State Wars v5.1 and Non-State Wars v4.0 (non-state actors; raw format differs) — follow-up.

## 2. What was built

| File | Purpose |
|---|---|
| `src/reva/adapters/connectors/atlas_maddison.py` | `atlas.maddison.mpd2023` |
| `src/reva/adapters/connectors/atlas_clio.py` | `atlas.clio_infra` |
| `src/reva/adapters/connectors/atlas_gapminder.py` | `atlas.gapminder.fasttrack` |
| `src/reva/adapters/connectors/atlas_hyde.py` | `atlas.hyde.hyde321` (Range request + zip64 tail reader) |
| `src/reva/adapters/connectors/atlas_cow.py` | `atlas.cow` (3 tables + KG load) |
| `src/reva/adapters/connectors/atlas_history_common.py` | `source_spec`, `ingest_with_run` (fetch via `run.http`, `run.archive`, `run.register`), decimal/canon helpers, historical-QID lookup |
| `src/reva/adapters/connectors/atlas_xlsx.py` | stdlib, bounded, read-only XLSX reader (exact cell text, no float conversion; DTD/entities refused) |
| `src/reva/adapters/connectors/atlas_inflate64.py` | bounded Deflate64 inflater (port of zlib `puff` + `infback9` differences) |
| `registry/atlas/catalog/history.yaml` | 30 provider-declared concepts (one unit/definition per concept) |
| `registry/entities/historical_states_qids.json` | 78 agent-mapped entries (Maddison, Clio, Gapminder, HYDE, COW period-bound) with Wikidata evidence |
| `registry/sources/{maddison,clio_infra,gapminder,hyde,correlates_of_war}.yaml` | source registrations (rights) |
| `registry/atlas/budget_requests/a_histecon.yaml` | merged by ATLASCORE into `budgets-1.3.2` |
| `tests/unit/test_atlas_histecon.py` (17), `tests/integration/test_atlas_histecon_ingest.py` (2, `db`) | offline tests (fixtures `tests/fixtures/source/atlas.{maddison.mpd2023,clio_infra,gapminder.fasttrack,hyde.hyde321,cow}`, 1.7 MB) |

All five modules follow the atlas platform protocol (`atlas_source()` + `async atlas_ingest(run)`; no HTTP client
created in the modules; invariant 30 is clean for these files). Live: `uv run python -m reva.atlas.ingest atlas.cow`
etc. (against the configured DB — use a throwaway one).

## 3. Provider notes kept as flags (historical honesty)

* **Maddison**: MPD 2023 publishes **no per-cell benchmark/interpolation flag** (earlier releases had `i_cig`/`i_bm`).
  Rows carry `mpd_cell_flag: "none_published"` and, for GDP pc, the provider's own source spans covering that
  country-year (`mpd_sources`, from the `Sources` sheet, incl. territory qualifiers such as "(England)"), the general
  2008-2022 note (TED / UN national accounts), or a pointer to "Maddison original sources". Pre-1820 rows carry
  `mpd_sparse_period` (isolated benchmark years exist only). SUN/CSK/YUG after dissolution are successor-state
  aggregates (the provider says so; carried by the source span flag).
* **Clio-Infra**: rows are *polities*. `cshapes/*` rows with an ISO numeric `ccode` are country series (iso3 set);
  values outside the polity's own years are the provider's back-cast and flagged `outside_polity_years`.
  `geacron/*` rows are polities at **historical borders** (e.g. "France 1820-1879", "Ottoman 1800-1819") — kept with
  iso3 null; only distinct historical states get a QID. Height carries the birth-decade note.
* **Gapminder**: projections / "IF-scenario" years are **not loaded** (pop > 2023: UN WPP 2024 medium variant;
  lex > 2019; gdp_pcap > 2024: scenario) — 45,630 cells excluded. Every row carries its documented segment and
  `composite_reconstruction` before the modern segment (Gapminder's pre-1950 / pre-1990 series are built from
  Maddison/Clio, so they are not independent confirmation). `lex` data is v15 but the documentation page is v14:
  rows carry `documentation_version_mismatch` and the v14 cut-off is applied.
* **HYDE**: modelled estimates (`hyde_modelled`). The readme lists `pop_c.txt ... in 1000` but the archive holds
  `popc_c.txt` whose values are inhabitants (world 4,432,267 in 10000 BCE; France 2017 = 6.55e7) — unit "inhabitants",
  conflict noted on every population row. **BCE steps and year 0 are not representable** in atlas periods
  (dates from year 1): 20,196 cells (10000 BCE … 0 CE) not loaded.
* **COW NMC**: missing code `-9` omitted (milex 2,360, milper 456, pec 448, upop 68, irst 26 cells); **milex unit
  changes**: thousands of current GBP 1816-1913, thousands of current USD from 1914 (NMC v7 documentation) — row unit
  says which, `currency_break` flag. Leading-dot decimals (`.0397`) normalised losslessly.
* **COW codes → countries**: KGGEOPOL's date-aware `StateCodeCrosswalk` (countrycode panel + policy windows) is reused,
  not duplicated: e.g. 365 in 1950 → iso3 null (USSR), 2000 → RUS. Period-bound QIDs from
  `historical_states_qids.json` fill `entity_qid` (365 1922-1991 → Q15180; 255 1871-1918 → Q43287; 640 ≤1922 → Q12560 …).

## 4. Live ingest (2026-10-15, throwaway DB)

| Source | Requests | Raw bytes | Seconds | Tables (rows) | Parquet sha256 |
|---|---|---|---|---|---|
| atlas.maddison.mpd2023 | 2 | 4.9 MB | 9.5 | mpd2023 (39,650) | `69a052b1924e…` |
| atlas.gapminder.fasttrack | 6 | 3.7 MB | 9.7 | fasttrack (129,597) | `f0155a977f05…` |
| atlas.cow | 7 | 5.4 MB | 19.0 | nmc7 (116,489), system2024 (17,511), wars4 (519) | `c3789cfd5451…`, `f506cf45a12a…`, `c67a046ae3c4…` |
| atlas.hyde.hyde321 | 3 | 1.5 MB | 8.8 | hyde321 (117,504) | `2f5c65c7a392…` |
| atlas.clio_infra | 181 | 14.2 MB | 255 | infra (951,495) | `36fba6b75f8f…` |

Budget ledger: reserved = consumed for every source (Clio reserved 185, used 181), 0 refused. Store after the run:
37 MB (raw + Parquet) — far under the 1.5 GB budget; disk free 19 GB before and after.

**KG** (`source_key cow.data`, one `load_batch`): 477 entities (217 `cow:<ccode>` state codes — same id scheme as
KGGEOPOL —, `cow:system:interstate`, `cow:system:major_powers`, 258 `cow:war:<n>`), 1,116 statements, 0 rejected:
`P463` member of the interstate system / major powers per spell (day precision; an end of 2024-12-31 is the end of
the data → kept open with `observed_until`; 1816-01-01 starts carry "COW data begin 1816"), `P1344` participant in
(per war-participation spell; `-9` lowers the date precision, `-7` = ongoing), `P31` war → Q198, and
`cow:code_denotes` (state code → Wikidata QID, dated, agent-mapped) from `historical_states_qids.json`.
Re-loading is a no-op (tested).

## 5. Coverage (live tables; crosswalk = 250 entities)

| table | rows | indicators | crosswalk countries | unmapped geos (rows) | first year | last year |
|---|---|---|---|---|---|---|
| `atlas.clio.infra` | 951,495 | 90 | 193 | 262 (11,875) | 1500 | 2018 |
| `atlas.cow.nmc7` | 116,489 | 7 | 195 | 25 (8,346) | 1816 | 2022 |
| `atlas.cow.system2024` | 17,511 | 1 | 195 | 25 (1,280) | 1816 | 2024 |
| `atlas.cow.wars4` | 519 | 346 | 85 | 20 (57) | 1816 | 2006 |
| `atlas.gapminder.fasttrack` | 129,597 | 3 | 196 | 1 (669) | 1800 | 2024 |
| `atlas.hyde.hyde321` | 117,504 | 9 | 199 | 5 (2,880) | 100 | 2017 |
| `atlas.maddison.mpd2023` | 39,650 | 2 | 165 | 14 (1,345) | 1 | 2022 |

Countries (crosswalk entities) with at least one value, by century of the period start:

| table | before 1500 | 1500s | 1600s | 1700s | 1800s | 1900s | 2000+ |
|---|---|---|---|---|---|---|---|
| `atlas.clio.infra` | 0 | 193 | 193 | 193 | 193 | 193 | 193 |
| `atlas.cow.nmc7` | 0 | 0 | 0 | 0 | 41 | 188 | 194 |
| `atlas.cow.system2024` | 0 | 0 | 0 | 0 | 41 | 188 | 194 |
| `atlas.cow.wars4` | 0 | 0 | 0 | 0 | 29 | 80 | 15 |
| `atlas.gapminder.fasttrack` | 0 | 0 | 0 | 0 | 196 | 196 | 196 |
| `atlas.hyde.hyde321` | 199 | 199 | 199 | 199 | 199 | 199 | 199 |
| `atlas.maddison.mpd2023` | 46 | 45 | 46 | 46 | 94 | 165 | 165 |

Clio-Infra key indicators: crosswalk countries with at least one value, by century:

| Clio indicator | before 1500 | 1500s | 1600s | 1700s | 1800s | 1900s | 2000+ |
|---|---|---|---|---|---|---|---|
| GDP_Capita-historical | 0 | 10 | 11 | 13 | 61 | 152 | 151 |
| Total_Population-historical | 0 | 71 | 69 | 82 | 188 | 190 | 190 |
| Height-historical | 0 | 2 | 3 | 25 | 116 | 150 | 39 |
| Labourers_wage-historical | 0 | 0 | 0 | 0 | 30 | 131 | 46 |
| Total_life_expectancy-historical | 0 | 1 | 1 | 4 | 32 | 180 | 180 |
| Urbanization_ratio-historical | 0 | 63 | 65 | 75 | 168 | 182 | 181 |
| Numeracy(Total)-historical | 0 | 6 | 14 | 27 | 114 | 136 | 0 |
| income_inequality-historical | 0 | 0 | 0 | 0 | 79 | 135 | 82 |
| Homicide_rates-historical | 0 | 0 | 0 | 0 | 18 | 120 | 177 |
| Average_years_education-historical | 0 | 0 | 0 | 0 | 86 | 138 | 136 |
| polity-historical | 0 | 0 | 0 | 0 | 50 | 159 | 161 |
| conflicts_international-historical | 0 | 190 | 190 | 190 | 190 | 191 | 191 |

Notes: Clio's binary indicators (armed conflicts, gold standard) report 0/1 for ~190 countries back to 1500 at
*current* borders (provider back-cast) — that is why Clio "covers" every century; the key-indicator table shows the
real depth. Maddison before 1500: 46 countries (year 1 CE and medieval benchmarks). HYDE starts at 100 CE in the
atlas (BCE not representable).

### Gaps and which source fills them

* **No value in any history table (34 entities)**: small territories and dependencies (ALA, ATA, BMU, CYM, GIB, GUM,
  IMN, JEY, MAC, PYF, …). Clio has geacron/name rows for some (Bermuda, Gibraltar, Cayman Islands, Isle of Man,
  Jersey, Macau, Netherlands Antilles) but without an ISO code: they stay unmapped (no guessed mapping).
* Countries without Maddison (85): Gapminder (196 countries from 1800), HYDE (199 from 100 CE), Clio population/
  urbanisation (188/168 in the 1800s) fill population; GDP per capita before 1990 for them comes only from
  Gapminder's composite reconstruction (flagged) or Clio GDP (1990 GK$, 61 countries in the 1800s).
* Before 1800 (outside Gapminder): Maddison (≈46 countries/century), HYDE (all, modelled), Clio population and
  urbanisation (≈70 countries).
* State-level power/population 1816-2022 (territory of the time, e.g. Ottoman Empire, USSR): COW NMC
  (`history.state_population_cow` is a separate concept from population at current borders).

<details><summary>Per-country depth (first year with a value per source)</summary>

Per country: first year with a value (Clio: first year over all indicators, and the number of Clio indicators); empty = no value from that source.

| iso3 | country | MPD gdppc | MPD pop | Clio first | Clio #ind | Gapminder pop | HYDE popc | COW member | sources |
|---|---|---|---|---|---|---|---|---|---|
| ABW | Aruba |  |  |  |  |  | 100 |  | 1 |
| AFG | Afghanistan | 1950 | 1820 | 1500 | 49 | 1800 | 100 | 1919 | 7 |
| AGO | Angola | 1950 | 1950 | 1500 | 58 | 1800 | 100 | 1975 | 7 |
| AIA | Anguilla |  |  |  |  |  | 100 |  | 1 |
| ALA | Åland Islands |  |  |  |  |  |  |  | 0 |
| ALB | Albania | 1870 | 1 | 1500 | 63 | 1800 | 100 | 1914 | 7 |
| AND | Andorra |  |  | 1500 | 14 | 1800 | 100 | 1993 | 5 |
| ARE | United Arab Emirates | 1950 | 1950 | 1500 | 46 | 1800 | 100 | 1971 | 7 |
| ARG | Argentina | 1800 | 1820 | 1500 | 85 | 1800 | 100 | 1841 | 7 |
| ARM | Armenia | 1973 | 1950 | 1500 | 67 | 1800 | 100 | 1991 | 7 |
| ASM | American Samoa |  |  |  |  |  | 100 |  | 1 |
| ATA | Antarctica |  |  |  |  |  |  |  | 0 |
| ATF | French Southern Territories |  |  |  |  |  |  |  | 0 |
| ATG | Antigua and Barbuda |  |  | 1500 | 36 | 1800 | 100 | 1981 | 5 |
| AUS | Australia | 1820 | 1 | 1500 | 90 | 1800 | 100 | 1920 | 7 |
| AUT | Austria | 1820 | 1 | 1500 | 81 | 1800 | 100 | 1919 | 7 |
| AZE | Azerbaijan | 1973 | 1950 | 1500 | 62 | 1800 | 100 | 1991 | 7 |
| BDI | Burundi | 1950 | 1950 | 1500 | 62 | 1800 | 100 | 1962 | 7 |
| BEL | Belgium | 1 | 1 | 1500 | 73 | 1800 | 100 | 1830 | 7 |
| BEN | Benin | 1950 | 1950 | 1500 | 57 | 1800 | 100 | 1960 | 7 |
| BES | Bonaire, Sint Eustatius and Saba |  |  |  |  |  |  |  | 0 |
| BFA | Burkina Faso | 1950 | 1950 | 1500 | 59 | 1800 | 100 | 1960 | 7 |
| BGD | Bangladesh | 1950 | 1950 | 1500 | 61 | 1800 | 100 | 1971 | 7 |
| BGR | Bulgaria | 1870 | 1 | 1500 | 76 | 1800 | 100 | 1908 | 7 |
| BHR | Bahrain | 1950 | 1950 | 1500 | 53 | 1800 | 100 | 1971 | 7 |
| BHS | Bahamas |  |  | 1500 | 41 | 1800 | 100 | 1973 | 5 |
| BIH | Bosnia and Herzegovina | 1952 | 1950 | 1500 | 53 | 1800 | 100 | 1992 | 7 |
| BLM | Saint Barthélemy |  |  |  |  |  |  |  | 0 |
| BLR | Belarus | 1973 | 1950 | 1500 | 58 | 1800 | 100 | 1991 | 7 |
| BLZ | Belize |  |  | 1500 | 43 | 1800 | 100 | 1981 | 5 |
| BMU | Bermuda |  |  |  |  |  |  |  | 0 |
| BOL | Bolivia (Plurinational State of) | 1846 | 1820 | 1500 | 73 | 1800 | 100 | 1848 | 7 |
| BRA | Brazil | 1800 | 1000 | 1500 | 86 | 1800 | 100 | 1822 | 7 |
| BRB | Barbados | 1950 | 1950 | 1500 | 44 | 1800 | 100 | 1966 | 7 |
| BRN | Brunei Darussalam |  |  | 1500 | 35 | 1800 | 100 | 1984 | 5 |
| BTN | Bhutan |  |  | 1500 | 45 | 1800 | 100 | 1971 | 5 |
| BVT | Bouvet Island |  |  |  |  |  |  |  | 0 |
| BWA | Botswana | 1950 | 1950 | 1500 | 68 | 1800 | 100 | 1966 | 7 |
| CAF | Central African Republic | 1950 | 1950 | 1500 | 63 | 1800 | 100 | 1960 | 7 |
| CAN | Canada | 1820 | 1 | 1500 | 88 | 1800 | 100 | 1920 | 7 |
| CCK | Cocos (Keeling) Islands |  |  |  |  |  |  |  | 0 |
| CHE | Switzerland | 1 | 1 | 1500 | 72 | 1800 | 100 | 1816 | 7 |
| CHI | Channel Islands |  |  |  |  |  |  |  | 0 |
| CHL | Chile | 1800 | 1820 | 1500 | 79 | 1800 | 100 | 1839 | 7 |
| CHN | China | 1000 | 1 | 1500 | 85 | 1800 | 100 | 1860 | 7 |
| CIV | Côte d’Ivoire | 1887 | 1950 | 1500 | 63 | 1800 | 100 | 1960 | 7 |
| CMR | Cameroon | 1950 | 1950 | 1500 | 65 | 1800 | 100 | 1960 | 7 |
| COD | Democratic Republic of the Congo | 1950 | 1950 | 1500 | 64 | 1800 | 100 | 1960 | 7 |
| COG | Congo | 1950 | 1950 | 1500 | 62 | 1800 | 100 | 1960 | 7 |
| COK | Cook Islands |  |  |  |  |  |  |  | 0 |
| COL | Colombia | 1800 | 1820 | 1500 | 75 | 1800 | 100 | 1831 | 7 |
| COM | Comoros | 1950 | 1950 | 1500 | 47 | 1800 | 100 | 1975 | 7 |
| CPV | Cabo Verde | 1950 | 1950 | 1500 | 51 | 1800 | 100 | 1975 | 7 |
| CRI | Costa Rica | 1920 | 1820 | 1500 | 66 | 1800 | 100 | 1920 | 7 |
| CUB | Cuba | 1690 | 1820 | 1500 | 62 | 1800 | 100 | 1902 | 7 |
| CUW | Curaçao |  |  |  |  |  |  |  | 0 |
| CXR | Christmas Island |  |  |  |  |  |  |  | 0 |
| CYM | Cayman Islands |  |  |  |  |  |  |  | 0 |
| CYP | Cyprus | 1950 | 1950 | 1500 | 63 | 1800 | 100 | 1960 | 7 |
| CZE | Czechia | 1970 | 1950 | 1500 | 71 | 1800 | 100 | 1993 | 7 |
| DEU | Germany | 1500 | 1 | 1500 | 88 | 1800 | 100 | 1945 | 7 |
| DJI | Djibouti | 1950 | 1950 | 1500 | 41 | 1800 | 100 | 1977 | 7 |
| DMA | Dominica | 1950 | 1820 | 1500 | 34 | 1800 | 100 | 1978 | 7 |
| DNK | Denmark | 1820 | 1 | 1500 | 72 | 1800 | 100 | 1816 | 7 |
| DOM | Dominican Republic | 1950 | 1950 | 1500 | 68 | 1800 | 100 | 1894 | 7 |
| DZA | Algeria | 1820 | 1 | 1500 | 72 | 1800 | 100 | 1962 | 7 |
| ECU | Ecuador | 1870 | 1820 | 1500 | 70 | 1800 | 100 | 1854 | 7 |
| EGY | Egypt | 1 | 1 | 1500 | 73 | 1800 | 100 | 1855 | 7 |
| ERI | Eritrea |  |  | 1500 | 45 | 1800 | 100 | 1993 | 5 |
| ESH | Western Sahara |  |  |  |  |  | 100 |  | 1 |
| ESP | Spain | 1 | 1 | 1500 | 89 | 1800 | 100 | 1816 | 7 |
| EST | Estonia | 1855 | 1950 | 1500 | 63 | 1800 | 100 | 1918 | 7 |
| ETH | Ethiopia | 1950 | 1 | 1500 | 23 | 1800 | 100 | 1898 | 7 |
| FIN | Finland | 1600 | 1 | 1500 | 80 | 1800 | 100 | 1917 | 7 |
| FJI | Fiji |  |  | 1500 | 53 | 1800 | 100 | 1970 | 5 |
| FLK | Falkland Islands (Malvinas) |  |  |  |  |  | 100 |  | 1 |
| FRA | France | 1 | 1 | 1500 | 89 | 1800 | 100 | 1816 | 7 |
| FRO | Faroe Islands |  |  |  |  |  | 100 |  | 1 |
| FSM | Micronesia (Federated States of) |  |  | 1500 | 18 | 1800 |  | 1991 | 4 |
| GAB | Gabon | 1950 | 1950 | 1500 | 60 | 1800 | 100 | 1960 | 7 |
| GBR | United Kingdom of Great Britain and Northern Ireland | 1000 | 1 | 1500 | 89 | 1800 | 100 | 1816 | 7 |
| GEO | Georgia | 1973 | 1950 | 1500 | 63 | 1800 | 100 | 1991 | 7 |
| GGY | Guernsey |  |  |  |  |  |  |  | 0 |
| GHA | Ghana | 1885 | 1870 | 1500 | 68 | 1800 | 100 | 1957 | 7 |
| GIB | Gibraltar |  |  |  |  |  |  |  | 0 |
| GIN | Guinea | 1950 | 1950 | 1500 | 59 | 1800 | 100 | 1958 | 7 |
| GLP | Guadeloupe |  |  |  |  |  | 100 |  | 1 |
| GMB | Gambia | 1950 | 1950 | 1500 | 54 | 1800 | 100 | 1965 | 7 |
| GNB | Guinea-Bissau | 1950 | 1950 | 1500 | 53 | 1800 | 100 | 1974 | 7 |
| GNQ | Equatorial Guinea | 1950 | 1950 | 1500 | 48 | 1800 | 100 | 1968 | 7 |
| GRC | Greece | 1 | 1 | 1500 | 82 | 1800 | 100 | 1828 | 7 |
| GRD | Grenada |  |  | 1500 | 39 | 1800 | 100 | 1974 | 5 |
| GRL | Greenland |  |  |  |  |  | 100 |  | 1 |
| GTM | Guatemala | 1920 | 1820 | 1500 | 72 | 1800 | 100 | 1868 | 7 |
| GUF | French Guiana |  |  |  |  |  | 100 |  | 1 |
| GUM | Guam |  |  |  |  |  |  |  | 0 |
| GUY | Guyana |  |  | 1500 | 58 | 1800 | 100 | 1966 | 5 |
| HKG | China, Hong Kong Special Administrative Region | 1820 | 1820 |  |  | 1800 | 100 |  | 4 |
| HMD | Heard Island and McDonald Islands |  |  |  |  |  |  |  | 0 |
| HND | Honduras | 1920 | 1820 | 1500 | 68 | 1800 | 100 | 1899 | 7 |
| HRV | Croatia | 1952 | 1950 | 1500 | 60 | 1800 | 100 | 1992 | 7 |
| HTI | Haiti | 1945 | 1820 | 1500 | 66 | 1800 | 100 | 1859 | 7 |
| HUN | Hungary | 1870 | 1 | 1500 | 79 | 1800 | 100 | 1918 | 7 |
| IDN | Indonesia | 1815 | 1 | 1500 | 74 | 1800 | 100 | 1949 | 7 |
| IMN | Isle of Man |  |  |  |  |  |  |  | 0 |
| IND | India | 1600 | 1 | 1500 | 86 | 1800 | 100 | 1947 | 7 |
| IOT | British Indian Ocean Territory |  |  |  |  |  |  |  | 0 |
| IRL | Ireland | 1820 | 1500 | 1500 | 74 | 1800 | 100 | 1922 | 7 |
| IRN | Iran (Islamic Republic of) | 1 | 1 | 1500 | 64 | 1800 | 100 | 1855 | 7 |
| IRQ | Iraq | 1 | 1 | 1500 | 57 | 1800 | 100 | 1932 | 7 |
| ISL | Iceland | 1870 | 1950 | 1500 | 53 | 1800 | 100 | 1944 | 7 |
| ISR | Israel | 1 | 1950 | 1500 | 69 | 1800 | 100 | 1948 | 7 |
| ITA | Italy | 1 | 1 | 1500 | 87 | 1800 | 100 | 1816 | 7 |
| JAM | Jamaica | 1820 | 1820 | 1500 | 66 | 1800 | 100 | 1962 | 7 |
| JEY | Jersey |  |  |  |  |  |  |  | 0 |
| JOR | Jordan | 1 | 1820 | 1500 | 58 | 1800 | 100 | 1946 | 7 |
| JPN | Japan | 730 | 1 | 1500 | 88 | 1800 | 100 | 1860 | 7 |
| KAZ | Kazakhstan | 1973 | 1950 | 1500 | 71 | 1800 | 100 | 1991 | 7 |
| KEN | Kenya | 1904 | 1950 | 1500 | 73 | 1800 | 100 | 1963 | 7 |
| KGZ | Kyrgyzstan | 1973 | 1950 | 1500 | 60 | 1800 | 100 | 1991 | 7 |
| KHM | Cambodia | 1950 | 1820 | 1500 | 51 | 1800 | 100 | 1953 | 7 |
| KIR | Kiribati |  |  | 1500 | 19 | 1800 |  | 1999 | 4 |
| KNA | Saint Kitts and Nevis |  |  | 1500 | 29 | 1800 | 100 | 1983 | 5 |
| KOR | Republic of Korea | 1820 | 1820 | 1500 | 77 | 1800 | 100 | 1887 | 7 |
| KWT | Kuwait | 1950 | 1950 | 1500 | 49 | 1800 | 100 | 1961 | 7 |
| LAO | Lao People's Democratic Republic | 1950 | 1820 | 1500 | 57 | 1800 | 100 | 1953 | 7 |
| LBN | Lebanon | 1820 | 1820 | 1500 | 51 | 1800 | 100 | 1946 | 7 |
| LBR | Liberia | 1845 | 1950 | 1500 | 53 | 1800 | 100 | 1920 | 7 |
| LBY | Libya | 1950 | 1 | 1500 | 48 | 1800 | 100 | 1951 | 7 |
| LCA | Saint Lucia | 1950 | 1950 | 1500 | 41 | 1800 | 100 | 1979 | 7 |
| LIE | Liechtenstein |  |  | 1500 | 20 | 1800 | 100 | 1990 | 5 |
| LKA | Sri Lanka | 1820 | 1820 | 1500 | 63 | 1800 | 100 | 1948 | 7 |
| LSO | Lesotho | 1950 | 1950 | 1500 | 50 | 1800 | 100 | 1966 | 7 |
| LTU | Lithuania | 1973 | 1950 | 1500 | 63 | 1800 | 100 | 1918 | 7 |
| LUX | Luxembourg | 1950 | 1950 | 1500 | 49 | 1800 | 100 | 1920 | 7 |
| LVA | Latvia | 1973 | 1950 | 1500 | 62 | 1800 | 100 | 1918 | 7 |
| MAC | China, Macao Special Administrative Region |  |  |  |  |  |  |  | 0 |
| MAF | Saint Martin (French Part) |  |  |  |  |  |  |  | 0 |
| MAR | Morocco | 1820 | 1 | 1500 | 74 | 1800 | 100 | 1847 | 7 |
| MCO | Monaco |  |  | 1500 | 7 | 1800 |  | 1993 | 4 |
| MDA | Republic of Moldova | 1973 | 1950 | 1500 | 63 | 1800 | 100 | 1991 | 7 |
| MDG | Madagascar | 1950 | 1000 | 1500 | 58 | 1800 | 100 | 1960 | 7 |
| MDV | Maldives |  |  | 1500 | 28 | 1800 |  | 1965 | 4 |
| MEX | Mexico | 1550 | 1 | 1500 | 86 | 1800 | 100 | 1831 | 7 |
| MHL | Marshall Islands |  |  | 1500 | 18 | 1800 |  | 1991 | 4 |
| MKD | North Macedonia | 1952 | 1950 | 1500 | 63 | 1800 | 100 | 1993 | 7 |
| MLI | Mali | 1950 | 1950 | 1500 | 62 | 1800 | 100 | 1960 | 7 |
| MLT | Malta | 1950 | 1950 | 1500 | 50 | 1800 | 100 | 1964 | 7 |
| MMR | Myanmar | 1820 | 1820 | 1500 | 74 | 1800 | 100 | 1948 | 7 |
| MNE | Montenegro | 1952 | 1950 | 1500 | 30 | 1800 |  | 2006 | 6 |
| MNG | Mongolia | 1950 | 1820 | 1500 | 63 | 1800 | 100 | 1921 | 7 |
| MNP | Northern Mariana Islands |  |  |  |  |  |  |  | 0 |
| MOZ | Mozambique | 1950 | 1 | 1500 | 58 | 1800 | 100 | 1975 | 7 |
| MRT | Mauritania | 1950 | 1950 | 1500 | 51 | 1800 | 100 | 1960 | 7 |
| MSR | Montserrat |  |  |  |  |  | 100 |  | 1 |
| MTQ | Martinique |  |  |  |  |  | 100 |  | 1 |
| MUS | Mauritius | 1950 | 1950 | 1500 | 58 | 1800 | 100 | 1968 | 7 |
| MWI | Malawi | 1904 | 1950 | 1500 | 53 | 1800 | 100 | 1964 | 7 |
| MYS | Malaysia | 1820 | 1820 | 1500 | 75 | 1800 | 100 | 1957 | 7 |
| MYT | Mayotte |  |  |  |  |  | 100 |  | 1 |
| NAM | Namibia | 1950 | 1950 | 1500 | 65 | 1800 | 100 | 1990 | 7 |
| NCL | New Caledonia |  |  |  |  |  | 100 |  | 1 |
| NER | Niger | 1950 | 1950 | 1500 | 60 | 1800 | 100 | 1960 | 7 |
| NFK | Norfolk Island |  |  |  |  |  |  |  | 0 |
| NGA | Nigeria | 1885 | 1950 | 1500 | 69 | 1800 | 100 | 1960 | 7 |
| NIC | Nicaragua | 1920 | 1820 | 1500 | 71 | 1800 | 100 | 1900 | 7 |
| NIU | Niue |  |  |  |  |  |  |  | 0 |
| NLD | Netherlands (Kingdom of the) | 1348 | 1 | 1500 | 78 | 1800 | 100 | 1816 | 7 |
| NOR | Norway | 1820 | 1 | 1500 | 81 | 1800 | 100 | 1905 | 7 |
| NPL | Nepal | 1820 | 1820 | 1500 | 59 | 1800 | 100 | 1920 | 7 |
| NRU | Naoero |  |  | 1500 | 9 | 1800 |  | 1999 | 4 |
| NZL | New Zealand | 1820 | 1000 | 1500 | 71 | 1800 | 100 | 1920 | 7 |
| OMN | Oman | 1950 | 1820 | 1500 | 50 | 1800 | 100 | 1971 | 7 |
| PAK | Pakistan | 1950 | 1950 | 1500 | 65 | 1800 | 100 | 1947 | 7 |
| PAN | Panama | 1906 | 1850 | 1500 | 64 | 1800 | 100 | 1903 | 7 |
| PCN | Pitcairn |  |  |  |  |  |  |  | 0 |
| PER | Peru | 1595 | 1000 | 1500 | 77 | 1800 | 100 | 1839 | 7 |
| PHL | Philippines | 1820 | 1820 | 1500 | 75 | 1800 | 100 | 1946 | 7 |
| PLW | Palau |  |  | 1500 | 17 | 1800 |  | 1994 | 4 |
| PNG | Papua New Guinea |  |  | 1500 | 53 | 1800 | 100 | 1975 | 5 |
| POL | Poland | 1400 | 1 | 1500 | 85 | 1800 | 100 | 1918 | 7 |
| PRI | Puerto Rico | 1950 | 1820 |  |  |  | 100 |  | 3 |
| PRK | Democratic People's Republic of Korea | 1820 | 1820 | 1500 | 55 | 1800 | 100 | 1948 | 7 |
| PRT | Portugal | 1 | 1 | 1500 | 80 | 1800 | 100 | 1816 | 7 |
| PRY | Paraguay | 1939 | 1820 | 1500 | 64 | 1800 | 100 | 1846 | 7 |
| PSE | State of Palestine | 1870 | 1820 |  |  | 1800 |  |  | 3 |
| PYF | French Polynesia |  |  |  |  |  |  |  | 0 |
| QAT | Qatar | 1950 | 1950 | 1500 | 48 | 1800 | 100 | 1971 | 7 |
| REU | Réunion |  |  |  |  |  | 100 |  | 1 |
| ROU | Romania | 1862 | 1 | 1500 | 77 | 1800 | 100 | 1878 | 7 |
| RUS | Russian Federation | 1860 | 1950 | 1500 | 81 | 1800 | 100 | 1816 | 7 |
| RWA | Rwanda | 1950 | 1950 | 1500 | 57 | 1800 | 100 | 1962 | 7 |
| SAU | Saudi Arabia | 1820 | 1820 | 1500 | 56 | 1800 | 100 | 1927 | 7 |
| SDN | Sudan | 1950 | 1 | 1500 | 8 | 1800 |  | 1956 | 6 |
| SEN | Senegal | 1950 | 1950 | 1500 | 60 | 1800 | 100 | 1960 | 7 |
| SGP | Singapore | 1870 | 1820 | 1500 | 60 | 1800 | 100 | 1965 | 7 |
| SGS | South Georgia and the South Sandwich Islands |  |  |  |  |  |  |  | 0 |
| SHN | Saint Helena |  |  |  |  |  |  |  | 0 |
| SJM | Svalbard and Jan Mayen Islands |  |  |  |  |  | 100 |  | 1 |
| SLB | Solomon Islands |  |  | 1500 | 44 | 1800 | 100 | 1978 | 5 |
| SLE | Sierra Leone | 1950 | 1950 | 1500 | 61 | 1800 | 100 | 1961 | 7 |
| SLV | El Salvador | 1920 | 1820 | 1500 | 66 | 1800 | 100 | 1875 | 7 |
| SMR | San Marino |  |  | 1500 | 23 | 1800 |  | 1992 | 4 |
| SOM | Somalia |  |  | 1500 | 44 | 1800 | 100 | 1960 | 5 |
| SPM | Saint Pierre and Miquelon |  |  |  |  |  | 100 |  | 1 |
| SRB | Serbia | 1952 | 1950 | 1500 | 32 | 1800 |  | 2006 | 6 |
| SSD | South Sudan |  |  | 1500 | 16 | 1800 |  | 2011 | 4 |
| STP | Sao Tome and Principe | 1950 | 1950 | 1500 | 30 | 1800 | 100 | 1975 | 7 |
| SUR | Suriname |  |  | 1500 | 56 | 1800 | 100 | 1975 | 5 |
| SVK | Slovakia | 1985 | 1950 | 1500 | 75 | 1800 | 100 | 1993 | 7 |
| SVN | Slovenia | 1952 | 1950 | 1500 | 64 | 1800 | 100 | 1992 | 7 |
| SWE | Sweden | 1300 | 1 | 1500 | 85 | 1800 | 100 | 1816 | 7 |
| SWZ | Eswatini | 1950 | 1950 | 1500 | 60 | 1800 | 100 | 1968 | 7 |
| SXM | Sint Maarten (Dutch part) |  |  |  |  |  |  |  | 0 |
| SYC | Seychelles | 1950 | 1950 | 1500 | 39 | 1800 |  | 1976 | 6 |
| SYR | Syrian Arab Republic | 1820 | 1820 | 1500 | 60 | 1800 | 100 | 1946 | 7 |
| TCA | Turks and Caicos Islands |  |  |  |  |  | 100 |  | 1 |
| TCD | Chad | 1950 | 1950 | 1500 | 56 | 1800 | 100 | 1960 | 7 |
| TGO | Togo | 1950 | 1950 | 1500 | 60 | 1800 | 100 | 1960 | 7 |
| THA | Thailand | 1820 | 1820 | 1500 | 80 | 1800 | 100 | 1887 | 7 |
| TJK | Tajikistan | 1973 | 1950 | 1500 | 61 | 1800 | 100 | 1991 | 7 |
| TKL | Tokelau |  |  |  |  |  |  |  | 0 |
| TKM | Turkmenistan | 1973 | 1950 | 1500 | 50 | 1800 | 100 | 1991 | 7 |
| TLS | Timor-Leste |  |  | 1500 | 26 | 1800 | 100 | 2002 | 5 |
| TON | Tonga |  |  | 1500 | 34 | 1800 | 100 | 1999 | 5 |
| TTO | Trinidad and Tobago | 1950 | 1820 | 1500 | 61 | 1800 | 100 | 1962 | 7 |
| TUN | Tunisia | 1 | 1 | 1500 | 66 | 1800 | 100 | 1825 | 7 |
| TUR | Türkiye | 1 | 1 | 1500 | 85 | 1800 | 100 | 1816 | 7 |
| TUV | Tuvalu |  |  | 1500 | 13 | 1800 |  | 2000 | 4 |
| TZA | United Republic of Tanzania | 1950 | 1950 | 1500 | 67 | 1800 | 100 | 1961 | 7 |
| UGA | Uganda | 1906 | 1950 | 1500 | 64 | 1800 | 100 | 1962 | 7 |
| UKR | Ukraine | 1973 | 1950 | 1500 | 67 | 1800 | 100 | 1991 | 7 |
| UMI | United States Minor Outlying Islands |  |  |  |  |  |  |  | 0 |
| URY | Uruguay | 1800 | 1820 | 1500 | 65 | 1800 | 100 | 1882 | 7 |
| USA | United States of America | 1650 | 1 | 1500 | 89 | 1800 | 100 | 1816 | 7 |
| UZB | Uzbekistan | 1973 | 1950 | 1500 | 59 | 1800 | 100 | 1991 | 7 |
| VAT | Holy See |  |  |  |  | 1800 |  | 1816 | 2 |
| VCT | Saint Vincent and the Grenadines |  |  | 1500 | 34 | 1800 | 100 | 1979 | 5 |
| VEN | Venezuela (Bolivarian Republic of) | 1800 | 1820 | 1500 | 67 | 1800 | 100 | 1841 | 7 |
| VGB | British Virgin Islands |  |  |  |  |  |  |  | 0 |
| VIR | United States Virgin Islands |  |  |  |  |  | 100 |  | 1 |
| VNM | Viet Nam | 1820 | 1820 | 1500 | 64 | 1800 | 100 | 1977 | 7 |
| VUT | Vanuatu |  |  | 1500 | 32 | 1800 | 100 | 1981 | 5 |
| WLF | Wallis and Futuna Islands |  |  |  |  |  |  |  | 0 |
| WSM | Samoa |  |  | 1500 | 30 | 1800 | 100 | 1976 | 5 |
| XKX | Kosovo |  |  |  |  |  |  | 2008 | 1 |
| YEM | Yemen | 1950 | 1820 | 1500 | 45 | 1800 | 100 | 1990 | 7 |
| ZAF | South Africa | 1700 | 1 | 1500 | 86 | 1800 | 100 | 1920 | 7 |
| ZMB | Zambia | 1906 | 1950 | 1500 | 72 | 1800 | 100 | 1964 | 7 |
| ZWE | Zimbabwe | 1914 | 1950 | 1500 | 68 | 1800 | 100 | 1965 | 7 |

</details>

## 6. Historical states → QIDs (`registry/entities/historical_states_qids.json`)

78 entries, `agent_mapped_pending_maintainer_review`, each with the Wikidata label, description, inception/dissolution,
revision id and the raw `wbgetentities` response sha256 (fetched through WDHARVEST's locked client; one SPARQL
attempt waited out a shared WDQS 429 cool-down and was replaced by `wbgetentities sites=enwiki&titles=…`).
Only distinct states are mapped (USSR, Czechoslovakia, Yugoslavia/SFRY/FRY, both Germanies, Ottoman Empire, Prussia,
German Confederation, Austrian Empire/Austria-Hungary, German Empire/Weimar/Nazi Germany, Qing/ROC, Sardinia/
Kingdom of Italy, the German and Italian states of COW, Joseon/Korean Empire, Zanzibar, the Yemens and Vietnams,
UAR, Netherlands Antilles) plus Taiwan (current, absent from the ALLC crosswalk). Earlier-border versions of current
countries (GEACRON "France 1820-1879") are deliberately not mapped. Never changes an iso3.

## 7. Limitations / follow-ups

* HYDE 3.3 not reachable without passing a bot challenge (not attempted); 3.2.1 used.
* BCE / year-0 values cannot be stored (atlas periods are `date`s ≥ year 1): ATLASCORE decision needed (e.g. an
  astronomical-year period column) — 20,196 HYDE cells and nothing else affected (Maddison starts at year 1).
* COW terms are not an open licence (cite, no charging) — maintainer to confirm redistribution in REVA.
* Gapminder `lex` v15 documentation not published (v14 cut-off applied, flagged).
* COW Intra-State / Non-State wars not parsed yet.
* Clio indicators not in the catalog (≈65 of 90: livestock, metals production, labour disputes, exchange rates, …)
  are in the atlas table and can be catalogued later.

## 8. Requests for other agents (no shared file edited)

1. **Lead**: the seeded deployment must register the atlas connector versions (`register_atlas_connector_versions`
   in the seed, per §7 of the interface) — my five specs are discovered automatically.
2. **KGCORE / WDHARVEST**: `atlas_cow.load_kg` registers one dataset predicate with `register_properties`:
   `cow:code_denotes` (item; inverse `denoted_by_cow_code`) plus P31/P463/P1344 from WDHARVEST's allow-list
   definitions. Please confirm the namespace predicate is acceptable or provide a shared one
   (KGGEOPOL plans `geopol:state_code_of` for the same kind of link).
3. **ATLASCORE**: BCE periods (above). Invariant-30 currently fails on other agents' files only
   (`wikidata.py` client creation; budget keys `worldbank.atlas`, `unsd.ama` without sources) — not on A-HISTECON files.
