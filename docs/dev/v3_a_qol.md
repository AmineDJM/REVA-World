# V3 atlas — quality of life, environment, energy (agent A-QOL)

Status 2026-10-15: four connectors, eight atlas tables, three catalogs (200 concepts). Offline tests pass.
One live ingest into a throwaway database (`reva_aqol_live_55661085`, dropped afterwards) registered all
eight tables. The dev database `reva` was never touched.

## 1. Sources: what was used, what was refused, and why

| Source | Access | Licence (checked 2026-10-15) | Decision |
|---|---|---|---|
| UNDP HDR 2025 composite indices (HDI, GDI, IHDI, GII, PHDI and their components) | `HDR25_Composite_indices_complete_time_series.csv` (2.0 MB) + codebook `HDR25_Composite_indices_metadata.xlsx` | CC BY 3.0 IGO (hdr.undp.org/terms-use) | **Ingested**: every year 1990-2023 |
| UNDP/OPHI Global MPI 2026 | `2026_gMPI_Table1and2.xlsx` (Table 1 latest survey, Table 2 harmonised over time) | CC BY 3.0 IGO | **Ingested** |
| World Happiness Report 2026 | `WHR26_Data_Figure_2.1.xlsx` (life evaluations 2012-2025 windows, 95 % CI, 6 explained-by parts, Dystopia+residual) | Free download, **no explicit open licence**. The page says "available to download for free". The data are aggregates of the proprietary Gallup World Poll. | **Ingested with a flag**: `rights.licence_status = free_download_no_explicit_licence`, `redistribution = unclear`. Gallup microdata are not ingested. |
| OWID energy-data and co2-data (GitHub) | `owid-*-data.csv` + codebooks (raw.githubusercontent.com) | CC BY 4.0 for OWID's work; third-party data stay under the original producer's terms | **Ingested**. The original source is kept per cell. |
| OWID grapher: Climate Watch GHG emissions by sector | CSV + chart metadata + per-indicator full metadata | Climate Watch CC BY 4.0 | **Ingested** |
| OWID grapher: annual temperature anomalies (ERA5) | same | Copernicus licence (attribution) | **Ingested** |
| OWID grapher: IHME air-pollution deaths | — | IHME non-commercial agreement; OWID marks it `nonRedistributable` | Refused |
| OWID grapher: WHO/UNICEF JMP drinking water | — | CC BY-NC-SA 3.0 IGO | Refused (non-commercial). WHO GHO is A-HEALTHEDU's. |
| OWID grapher: UNODC homicide | — | "© United Nations" terms; no open licence | Refused |
| OWID grapher: WID, WHR, UNDP, World Bank series | — | — | Not taken from OWID: these are ingested from the original producer (by this agent or by A-WB) |
| World Inequality Database (WID) | Bulk archive `wid_all_data.zip`: 882 MB, 848 members, 6.9 GB uncompressed. Read by HTTP byte ranges. | Neither WID's own pages nor the archive README state a licence. OWID's origin metadata records "CC BY 4.0" (url wid.world). | **Ingested**: 14 key series. Rights flag `cc_by_4.0_per_owid_metadata_not_stated_by_wid`. |
| Global Peace Index (IEP) | Behind a request form | Non-commercial licence; commercial use is paid (economicsandpeace.org/consulting/data-licensing) | Skipped |
| Social Progress Index | Behind the site's login/"premium tools" | No open licence found | Skipped |
| Cost of living (Numbeo, Expatistan, …) | — | Proprietary | Skipped. The open alternative is the World Bank ICP price level ratio / PPP (CC BY 4.0, WDI): A-WB. |
| Gini (World Bank PIP) | — | — | A-WB's. WID Gini series are provided here as a different concept, not a fallback. |

## 2. Connectors and atlas tables

All connectors use template `atlas_bulk` and run fetch → archive every raw response as an
`evidence.acquisition` → parse → `register_atlas_table`. Shared code is in `atlas_qol_common.py`.

| Connector (`src/reva/adapters/connectors/`) | Atlas table | Rows | Countries (crosswalk) | Indicators | Years | Unmapped geos (kept) |
|---|---|---:|---:|---:|---|---|
| `atlas_undp.py` (`atlas.undp_hdr`) | `undp.hdr25` | 200,350 | 195 | 42 | 1990-2023 | 11 UNDP aggregates (ZZA.VHHD … ZZK.WORLD) |
| | `undp.gmpi2026` | 4,865 | 113 | 27 (`t1.*` 13, `t2.*` 14) | survey years 2001-2025 | none (8 regional aggregates have no survey or period, so they are skipped and listed) |
| `atlas_whr.py` (`atlas.whr`) | `whr.whr26_fig21` | 13,085 | 162 | 11 | windows 2010-12 … 2023-25 | Taiwan Province of China, North Cyprus, Somaliland Region |
| `atlas_owid.py` (`atlas.owid`) | `owid.energy` | 947,092 | 219 | 125 | 1900-2025 | 95 (continents, income groups, "Africa (EI)"-type source regions, USSR, Czechoslovakia, East/West Germany, Yugoslavia, Serbia and Montenegro, ANT, TWN) |
| | `owid.co2` | 1,677,565 | 218 | 74 | 1750-2024 | 36 (regions, International aviation/shipping, Kuwaiti Oil Fires, Ryukyu Islands, TWN) |
| | `owid.g_ghg_by_sector` | 74,673 | 193 | 11 | 1990-2023 | 12 OWID_* aggregates |
| | `owid.g_temperature_anomaly` | 18,318 | 194 | 1 | 1940-2025 | OWID_WRL + 18 NIAID ocean/continent regions |
| `atlas_wid.py` (`atlas.wid`) | `wid.wid_key` | 25,554 (25-country live subset) | 25 live (232 possible) | 14 | 1800-2025 | none |

Live run cost: UNDP 3 requests / 2.1 MB; WHR 1 / 0.13 MB; OWID 20 / 25.1 MB; WID 52 / 105.8 MB for 25 countries.
In total 76 raw acquisitions (133 MB) and 203 MB of store including Parquet. The throwaway database and
store were deleted afterwards.

Parquet sha256 of the live tables:
- `undp.hdr25`: `59d3a2b7…c3ce39`
- `undp.gmpi2026`: `8649a2af…d60d36`
- `whr.whr26_fig21`: `77d0f6c2…e68837`
- `owid.energy`: `42e1ecf1…f8a276`
- `owid.co2`: `6389b0f9…d697256`
- `owid.g_ghg_by_sector`: `c9649f25…9477f3`
- `owid.g_temperature_anomaly`: `503774cc…dc0d1d`
- `wid.wid_key`: `25ba77e9…58ce83`

### Honesty notes, per source

- **UNDP**
  - The CSV is Windows-1252 (byte 0xF4 in "Côte d'Ivoire"). It is decoded as such, and the encoding is recorded.
  - The HDR25 codebook says `ineq_inc` covers "2010-2021, 2022-2023=2022" and `mmr` covers "2020-2023=2020". So the later cells repeat an earlier value. They are kept as published and flagged `carried_forward_from` (767 cells).
  - The five IHDI inequality measures have no unit in the codebook. Their unit "%" is taken from HDR25 Statistical Annex Table 3.
  - A variable without a declared unit makes the parse fail closed.
- **MPI**
  - Countries are named in the tables, not coded. Names map first through UNDP's own (iso3, country) pairs from the HDR25 CSV, then through crosswalk labels: 361 + 3 rows, nothing unmapped.
  - The survey years give the period (`2022/2023` = [2022-01-01, 2024-01-01)). Survey type and footnote texts are kept in `flags`.
  - The monetary-poverty columns give "most recent year available in 2014-2025" with no year. Their period is the whole window, and a flag says so.
  - `..` cells are omitted, never zero.
- **WHR**
  - `Year` is the last survey year of the 3-year window, checked against the WHR 2013 and WHR 2015 values in the file.
  - The 156 rows labelled 2011 (WHR 2012) are not 3-year averages, and the file does not give their window. They are excluded and counted, not given an invented period.
  - The explained-by parts are WHR's decomposition, not causal effects. The catalog notes say so.
- **OWID**
  - OWID is a compiler. Every cell carries `flags.original_source` (codebook citation) or, for grapher charts, the citation, the original producers and OWID's processing level. Each table's `rights.original_sources` lists the producers and their licences.
  - The `population` and `gdp` columns are excluded. They are pass-through copies of UN WPP and Maddison, which A-UN and A-HISTECON ingest at the source.
  - The Energy Institute Statistical Review data are "© Energy Institute". OWID redistributes them, and they are kept with that attribution. EI's own terms page returned 403 to our fetch, so it was not read directly.
  - GitHub's API is blocked in this environment, so the commit id is not recorded. The raw sha256 plus the ETag pin the bytes.
- **WID**
  - Only the bytes needed are read: the archive tail (central directory), `WID_countries.csv`, and the data and metadata members of each country.
  - Every member request carries `If-Range: <archive ETag>`. If WID replaces the archive during a run, the server would send the whole 882 MB file, which the size-capped client refuses. The run can therefore never mix two archive versions.
  - Each member's local header, name, sizes and CRC-32 are checked against the central directory.
  - WID back-casts and extrapolates some years. Its `data_quality` code and the series' `method` and `source` texts are kept per cell, and no year is relabelled. Example `method` text, France pre-tax shares: "Before 1980, series is constructed based on the trend observed in the fiscal income data available".
  - For code `diinc`, WID's metadata text reuses the "post-tax national income" wording. The label follows the WID codes dictionary, and the catalog note says so.
  - Values of `anninci992` are in the currency named by WID metadata (EUR, DZD, …), so they are not comparable across countries as published.

### Geography

Two schemes are defined in `registry/atlas/geo_aliases/a_qol.yaml`, each alias with its evidence:

- **`owid`**: base iso3, plus `OWID_KOS` → XKX and `Kosovo` → XKX. OWID's GitHub CSVs leave `iso_code` empty for Kosovo, so the connector uses the entity name there.
- **`wid`**: base iso2, plus `KS` → XKX and `XI` → CHI. WID also lists `XK` "Kosovo", but its data member is header-only, so it is never mapped. Taiwan (TW, TWN) is not a crosswalk entity and stays unmapped everywhere.

## 3. Coverage by key concept (latest year per country, live tables)

| Table | Indicator | Countries | Latest year (countries) |
|---|---|---:|---|
| undp.hdr25 | hdi | 193 | 2023: 193 |
| undp.hdr25 | ihdi | 170 | 2023: 169, 2015: 1 |
| undp.hdr25 | gdi / gii / phdi | 184 / 172 / 156 | 2023 |
| undp.gmpi2026 | t1.mpi / t2.mpi | 108 / 89 | survey years 2014-2025 (2024: 8, 2023: 14, 2022: 12, …) |
| whr.whr26_fig21 | life_evaluation | 162 | 2023-25 window: 146; older windows for 16 countries |
| owid.energy | primary_energy_consumption | 217 | 2024: 78, 2023: 139 |
| owid.energy | renewables_share_elec, carbon_intensity_elec | 213 | 2025: 90, 2024: 105 |
| owid.co2 | co2_per_capita / total_ghg / consumption_co2 | 213 / 198 / 119 | 2024 / 2024 / 2023 |
| owid.g_ghg_by_sector | 11 sectors | 191 | 2023 |
| owid.g_temperature_anomaly | temperature_anomaly | 194 | 2025 |
| wid.wid_key | 14 series | 24 of the 25 fetched | 2024 (2025 for some). Full run: up to 232 codes |

### Gaps and which source fills them

- **55 crosswalk entities are absent from HDR**, mostly territories plus Kosovo:
  - OWID fills 39 of them for energy and CO2 (e.g. Aruba, Bermuda, Greenland, Macao, New Caledonia, Puerto Rico, Kosovo).
  - WHR fills Kosovo and Puerto Rico.
  - WID fills Kosovo and the Channel Islands.
  - **15 have no A-QOL data at all**: Åland, French Southern Territories, Saint-Barthélemy, Bouvet, Cocos, Guernsey, BIOT, Jersey, Saint-Martin (FR), Mayotte, Norfolk Island, Pitcairn, Svalbard and Jan Mayen, Tokelau, US Minor Outlying Islands. They are statistically covered by their sovereign state, or uninhabited.
- **WHR misses 35 HDR countries**, mostly small island states plus Cuba, North Korea, Eritrea and Papua New Guinea. Gallup does not survey them. There is no open alternative for life evaluations.
- **OWID energy misses 6 HDR countries**: Andorra, Liechtenstein, Monaco, Marshall Islands, Palau, San Marino. OWID CO2 covers all HDR countries.
- **MPI covers developing countries only** (113). High-income countries have no MPI by design. For monetary poverty, the alternative is World Bank PIP (A-WB).
- **IHDI** exists for 170 countries. For income inequality elsewhere, use WID Gini (232 codes after a full run) or WB PIP Gini (A-WB).
- **WID**: the live ingest covered 25 countries (a subset to stay inside today's byte budget). A full run is about 465 range requests and 660 MB of transfer; see the budget request.

## 4. Catalogs (`registry/atlas/catalog/`)

All concepts are `provider_declared: true`. Definitions are the providers' texts: HDR25 codebook names, WHR
data-page wording, OWID codebook titles and descriptions, WID metadata. French labels are written by A-QOL.

- **`quality_of_life.yaml`** (90 concepts):
  - HDR indices and components (38)
  - WHR (11)
  - MPI Table 1 (13) and Table 2 harmonised (14)
  - WID (14)
- **`energy.yaml`** (63 concepts): primary energy, electricity, the mixes by source (share of electricity, share of primary energy, TWh) and fossil production. The source order is `owid.energy`, then the identical `owid.co2` copy where one exists.
- **`environment.yaml`** (47 concepts):
  - CO2 (territorial, consumption, cumulative, by fuel, land-use change)
  - GHG: CH4, N2O, totals and the 11 Climate Watch sectors
  - warming contribution
  - ERA5 temperature anomaly
  - the HDR PHDI components (CO2 production per capita, material footprint)

## 5. Budget, disk and shared changes requested

- **Budget request:** `registry/atlas/budget_requests/a_qol.yaml`. It covers `undp.hdr`, `whr.figure21`, `owid.compilations` and `wid.bulk`.
  - WID alone is about 660 MB per full refresh, which is more than today's global `http_bytes_total` (524 MB/day).
  - The request is for a monthly full run, or a split over two days, or a country-subset run (`WidConnector(countries=[...])`).
- **Disk:** the fixtures total about 0.9 MB. The live store (203 MB) was deleted after the run. 19 GB remained free throughout.
- **Shared-file changes for the lead or ATLASCORE** (none of these were edited by A-QOL):
  1. `src/reva/ports/connectors.py`: add `"atlas_bulk"` to `ConnectorSpec.template`'s `Literal`. `atlas_spec()` currently validates the other fields and sets the template afterwards. A-SCI asked for the same change.
  2. `src/reva/adapters/connectors/registry.py`: list `UndpHdrConnector()`, `WhrConnector()`, `OwidConnector()` and `WidConnector()` so the seed registers them. Until then, tests and the live run register them with `atlas_qol_common.register_connector`, which uses the seed's own function under the migration owner.
  3. `registry/policies/budgets.yaml`: merge the A-QOL request. Until it is merged, `tests/invariants/test_invariant_30_budgets.py` (budget keys == registered sources) fails for the four new `registry/sources/atlas_*.yaml` entries, as it does for the other V3 agents' new sources.
  4. Observed, not caused by A-QOL: bootstrapping a fresh database printed a schema-contract drift warning. `0009_kg` adds `pg_trgm`, which is not yet in `migrations/schema_contract.json` (KGCORE/MIGR).

## 6. Files

- Connectors (`src/reva/adapters/connectors/`):
  - `atlas_qol_common.py`
  - `atlas_undp.py`
  - `atlas_whr.py`
  - `atlas_owid.py`
  - `atlas_wid.py`
- XLSX reading reuses A-HISTECON's `atlas_xlsx.py`, read-only.
- Sources (`registry/sources/`):
  - `atlas_undp_hdr.yaml`
  - `atlas_whr.yaml`
  - `atlas_owid.yaml`
  - `atlas_wid.yaml`
- Catalogs (`registry/atlas/catalog/`):
  - `quality_of_life.yaml`
  - `energy.yaml`
  - `environment.yaml`
- Geo aliases: `registry/atlas/geo_aliases/a_qol.yaml`
- Budget request: `registry/atlas/budget_requests/a_qol.yaml`
- Fixtures (`tests/fixtures/source/`):
  - `atlas.undp_hdr/`, `atlas.whr/`, `atlas.owid/`: excerpts of the live responses; each manifest records the live sha256 and what was kept.
  - `atlas.wid/`: a small archive rebuilt from real member text, served by byte range in the tests.
- Tests:
  - `tests/unit/test_atlas_qol_connectors.py` (11, offline)
  - `tests/integration/test_atlas_qol_ingest.py` (2, DB, fixture bytes; idempotent re-run)
  - `tests/integration/test_atlas_qol_live.py` (3, `-m live`; passed 2026-10-15)
