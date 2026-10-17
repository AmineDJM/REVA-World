# V4 V4ECON — macro at higher frequency and finer space, prices

Agent V4ECON (2026-10-17). Scope: monthly / quarterly macro series for every economy that publishes them,
sub-national regions (Eurostat NUTS, OECD TL2/TL3) as KG entities with their series, price levels by
expenditure category (ICP) and international food prices (FAO FFPI). Existing coverage was extended, not
duplicated: `atlas_imf.py` (annual WEO/BOP/GFS/ER/MFS_IR/IL/IMTS), `atlas_worldbank*.py` (WDI annual),
`atlas_gap_eurostat_nuts.py` (7 NUTS-2 overseas regions), `atlas_oecd_fiscal.py` (tax/social), `atlas_bis.py`
(policy rates monthly, credit, property prices), `atlas_inflate64.py`, `atlas_fao.py` (FAOSTAT incl. monthly
consumer price indices, domain CP), `atlas_ilo.py` + `atlas_wages_gapfill.py` (minimum wages: ILOSTAT + Eurostat
`earn_mw_cur`), V4ENERGY `atlas_pinksheet.py` (the whole World Bank Pink Sheet).

## Status

* **Code, fixtures, tests: done and committed.** 5 atlas sources, 1 KG layer module, 1 catalog file, 1 relation
  drop-in, budget request merged (budgets-1.3.17, digest re-pinned).
* **Live ingest: not run.** Free disk fell to 4.7 GB (< 5 GB rule) right before the live run (other agents'
  loads). Connector versions are registered in the dev DB (`reva seed`). Commands to finish are at the end.
* Tests: `tests/unit/test_v4econ_parsers.py` (32 passed), `tests/integration/test_v4econ_ingest.py` (7 passed:
  fixture ingest of every table, idempotent re-run, region KG edges in a throwaway DB); ruff / ruff format /
  mypy clean on all V4ECON files; `python -m reva.kg.families check` OK.

## Sources and atlas tables

All requests go through `run.get` (budget-metered live client or fixture client); every response is archived
(`run.archive`) before parsing. Sizes "live" were measured on 2026-10-17 with the exact live requests (the
probe responses were deleted afterwards; nothing was loaded). Raw archive of one full V4ECON run ≈ 110 MB.

| connector (`atlas.*`) | table | provider request | licence | live size | cells (exact = parsed probe; ~ = estimate from size) |
|---|---|---|---|---|---|
| `imf_hf` | `imf.cpi_m` | IMF.STA:CPI `.CPI._T.IX+YOY_PCH_PA_PT.M` | IMF terms (© IMF, citation; review pending as for A-IMF) | ~12 MB | ~170 k (192 economies) |
| | `imf.qnea_q` | IMF.STA:QNEA `.B1GQ.Q+V..XDC+YOY_PCH_PT+POP_PCH_PT.Q` (every S_ADJUSTMENT variant) | idem | ~7 MB | 39,997 (115 economies) |
| | `imf.ls_mq` | IMF.STA:LS `.U+UP.PT+PE.M+Q` | idem | 4.4 MB | 62,427 (122) |
| | `imf.pi_m` | IMF.STA:PI `.IND+C.IX+SA_IX.M` | idem | 5.7 MB | 71,397 (80) |
| | `imf.er_m` | IMF.STA:ER `.XDC_USD.PA_RT.M` | idem | ~11 MB | ~140 k |
| | `imf.mfs_ir_m` | IMF.STA:MFS_IR policy / money-market / T-bill / bond, `.M` | idem | 3.9 MB | 58,095 (117) |
| | `imf.bop_q` | IMF.STA:BOP `.NETCD_T.CAB+GS.USD.Q` | idem | 6.0 MB | 38,597 (161) |
| | `imf.qgfs_q` | IMF.STA:QGFS S13/S1311 revenue, expense, net lending, gross debt (market value) | idem | 1.1 MB | 15,143 (49) |
| `eurostat_macro` | `eurostat.hicp_m` | `prc_hicp_minr` TOTAL, I25 / RCH_M / RCH_A | Eurostat reuse policy (CC BY 4.0) | ~1 MB | ~43 k |
| | `eurostat.une_m` | `une_rt_m` total, NSA / SA / TC | idem | 0.5 MB | 42,287 (38) |
| | `eurostat.gdp_q` | `namq_10_gdp` B1GQ, CP_MEUR / CLV_PCH_PRE / CLV_PCH_SM, NSA/SA/CA/SCA | idem | 0.5 MB | 32,742 (45) |
| | `eurostat.gov_debt_q` | `gov_10q_ggdebt` S13 GD, MIO_EUR / PC_GDP | idem | 0.1 MB | 6,933 (32) |
| | `eurostat.ip_m` | `sts_inpr_m` B-D PRD I21, SCA / NSA | idem | ~0.4 MB | ~25 k |
| | `eurostat.nuts_gdp` | `nama_10r_3gdp` MIO_EUR / EUR_HAB / PPS_EU27_2020_HAB, NUTS 0-3 | idem | ~2 MB | ~126 k (1,814 geos) |
| | `eurostat.nuts_une` | `lfst_r_lfu3rt` 15-74, NUTS 0-2 | idem | 0.2 MB | 11,967 (510) |
| | `eurostat.nuts_pop` | `demo_r_pjanaggr3` population 1 January, NUTS 0-3 | idem | 1.0 MB | 55,318 (2,195) |
| `oecd_macro` | `oecd.kei_mq` | DSD_KEI@DF_KEI UNEMP, IR3TIB, IRLT, CA_GDP, B1GQ_Q, M+Q, from 1990 (code-only CSV) | OECD T&C, CC BY 4.0 default since 2024-07 | ~9 MB | ~84 k (54 areas) |
| | `oecd.reg_gdp` | DSD_REG_ECO@DF_GDP GDP current prices, XDC + USD_PPP_PS, TL2/TL3/CTRY | idem | ~15 MB | ~123 k |
| | `oecd.reg_une` | DSD_REG_LAB@DF_RATES UNE_RATE 15-64, TL2/TL3/CTRY | idem | ~4.5 MB | ~38 k |
| | `oecd.reg_pop` | DSD_REG_DEMO@DF_POP_BROAD POP total, TL2/TL3/CTRY | idem | 9.9 MB | 103,828 (3,375 geos) |
| | (KG only) | codelist `OECD/CL_REGIONAL/latest` (names en/fr, parents, TL family) | idem | 5.6 MB | 4,944 codes |
| `wb_icp` | `wb.icp2021` | API v2 source 90, all series × PX.WL + PPPGlob, 2021 + 2017 (revised) | CC BY 4.0 | 12.3 MB | 29,818 (207 geos, 48 headings; 9,926 nulls not stored) |
| `fao_ffpi` | `fao_ffpi.ffpi_m` | FFPI page + `food_price_indices_data.csv` | FAO website terms: **non-commercial** | 0.11 MB | 2,646 (1990-01 → 2026-09) |

Budget keys: `imf.sdmx` 76/day, `oecd.sdmx` 92, `eurostat.dissemination` 116, `worldbank.atlas` 42 (shared
keys raised by V4ECON's runs + one retry), `fao.ffpi` 4 (new, `registry/sources/fao_ffpi.yaml`).

### Rules implemented (all tables)

* **Period precision**: half-open month / quarter / year periods exactly as published (`2025-M01`, `2025-Q1`,
  `2025-09`); `flags.frequency`; never averaged or interpolated. Monthly and quarterly cells of one measure are
  distinct indicators (`…|freq=M`).
* **Seasonal adjustment verbatim**: `flags.seasonal_adjustment` = the provider's code and
  `flags.seasonal_adjustment_label` = its label (IMF `S_ADJUSTMENT` / `SA_*` codes, Eurostat `s_adj`, OECD
  `ADJUSTMENT`), or `"not_stated_by_provider"` (IMF LS/CPI/ER/MFS/BOP/QGFS, Eurostat HICP/debt, FFPI). Never
  assumed NSA.
* Values verbatim (strict decimal of the provider's text); IMF `SCALE` and OECD `UNIT_MULT` kept in flags /
  unit (`x10^6`), never applied; absent values are not cells (counted by status in each coverage note).
* Provider status / derivation kept with labels (IMF DERIVATION_TYPE, Eurostat `p`/`e`/`b`…, SDMX OBS_STATUS);
  estimates / provisional / projections flagged in `flags.estimate_kind`.
* Labels of IMF and OECD codes are quoted in the modules; an unquoted code **fails the table** (fail closed).
* **Revisions / vintages**: every run re-registers the table; changed provider content becomes a new version
  of `atlas.<source>.<dataset>` (the atlas revision mechanism keeps all earlier vintages). The provider's
  dataflow version + `UPDATE_DATE` (IMF), `updated` (Eurostat), structure version (OECD), `lastupdated` (World
  Bank) and release date (FAO) are in each coverage note. The IMF also publishes dated vintage dataflows
  (`CPI_2026_MAY_VINTAGE` …): not read here; real-time vintages are V5 PRED-VINTAGE's scope.
* **Regions are never countries**: regional tables set `iso3` only on country rows (NUTS 0 / OECD CTRY);
  region rows keep their code in `provider_geo` with `flags.nuts_level` / `flags.territorial_level` and
  `flags.country_iso3`.

## KG: statistical regions (`src/reva/adapters/connectors/econ_regions_kg.py`)

Loaded by `atlas.eurostat_macro` and `atlas.oecd_macro` after their regional tables register (source keys
`eurostat.dissemination` / `oecd.sdmx`; statements reference the raw sha256 + selector of the response that
carried the code).

| entity | kind | id authority | count (live probe) |
|---|---|---|---|
| `nuts:<code>` | `nuts_region` | Eurostat NUTS code (levels 1-3) | ~2,150 (union of the three NUTS tables) |
| `oecdreg:<code>` | `oecd_region` | OECD `CL_REGIONAL` code (TL2 / TL3) | ~3,320 (741 TL2, 2,577 TL3) |
| `atlas:<source>.<dataset>` | `data_series` | REVA atlas table key | 6 |

Predicates (registered with `register_properties`; families in `registry/kg/relations_v4.d/v4econ.yaml`):

| predicate | family | basis |
|---|---|---|
| `nuts:part_of` | `part_of` | NUTS coding rule: parent = code minus its last character (Reg. (EC) 1059/2003), only if the parent code is in the same response |
| `nuts:in_country` | `located_in` | two-letter NUTS country prefix → crosswalk scheme `eurostat` → `registry/entities/country_qids.json` |
| `oecdreg:part_of` | `part_of` | `CL_REGIONAL` `<Parent>` chain up to the first loaded region (e.g. `FR101 → FR1`; US TL3 economic areas → `USA_TL3` → country, so no regional parent) |
| `oecdreg:in_country` | `located_in` | the `COUNTRY` attribute (ISO3) of the data rows |
| `econ:measured_by` | `measured_by` | the atlas table holding the region's cells (qualifiers `table`, `provider_geo`) |
| `nuts:level`, `oecdreg:territorial_level` | (literal) | code length / `TERRITORIAL_LEVEL` |

Live-probe counts for `oecd.reg_pop` alone: 3,318 regions, 2,394 regional parents, 924 parents that are the
country itself (carried by `in_country`), 3,318 `measured_by`.

Examples (ids as loaded in the integration test from real fixture bytes):

* `nuts:FR101` (Paris) → `nuts:part_of` → `nuts:FR10` (Île-de-France, NUTS 2) → `nuts:part_of` → `nuts:FR1`
  (NUTS 1); `nuts:FR10` → `nuts:in_country` → `Q142` (France); `nuts:FR101` → `econ:measured_by` →
  `atlas:eurostat.nuts_gdp` (GDP, EUR per inhabitant, PPS per inhabitant, NUTS 3, 2000-2024).
* `oecdreg:FRL04` (Bouches-du-Rhône, TL3) → `oecdreg:part_of` → `oecdreg:FRL` (Provence-Alpes-Côte d'Azur,
  TL2) → `oecdreg:in_country` → `Q142`; `oecdreg:FRL04` → `econ:measured_by` → `atlas:oecd.reg_pop`.
* `nuts:EL303` → `nuts:in_country` → Greece (`EL` = Eurostat code for GRC).

Never merged: `nuts:FR101` and `oecdreg:FR101` are distinct entities (neither source publishes the
equivalence). Wikidata holds NUTS codes (P605); a `same_as` bridge through that external id is V4LINK's job.

## Catalog

`registry/atlas/catalog/macro_hf.yaml`: 23 provider-declared concepts, **one source each** (no cross-provider
equivalence asserted: e.g. IMF "SA" vs Eurostat "SCA" vs OECD "Calendar and seasonally adjusted" stay separate
concepts), themes `economy` / `prices`. Sub-annual concepts note "not attached to the atlas year view".

## What is missing and why

* **Global Data Lab** (subnational HDI, income, education, health): downloads and the API require a **login**
  ("You need to be logged in to download Subnational HDI files"), and the terms allow **non-commercial use
  only** ("can be freely downloaded and used for noncommercial purposes … Commercial use is only possible after
  written permission of the Global Data Lab", https://globaldatalab.org/termsofuse/). Not worked around → owner
  decision.
* **World Bank Pink Sheet**: V4ENERGY's `atlas.wb_pinksheet` already loads the whole monthly and annual
  workbook (energy *and* non-energy commodities, indices); V4ECON adds nothing to avoid a duplicate table.
  IMF PCPS (primary commodity prices, open SDMX) is a possible second source, not taken.
* **Minimum wages**: already covered (ILOSTAT `EAR_4MMN_CUR_NB` in `atlas.ilo`, Eurostat `earn_mw_cur` in
  `atlas.wages_gapfill`).
* **FAOSTAT consumer price indices**: already covered (`atlas.fao`, domain CP, monthly).
* Not taken for disk (same requests, documented in each module): IMF CPI COICOP divisions and weights, ER end of
  period, LS employment/labour force; OECD KEI CPI / production / exchange rates (IMF and Eurostat hold them),
  OECD QNA growth flows and PPP price-level indices (`DSD_PPP@DF_PPP_CPL`); ICP expenditure, per-capita and
  shares classifications and years 2018-2020; FFPI real indices (Excel). OECD regional GDP at constant prices,
  employment, GVA by activity; Eurostat regional GVA / employment / household income.
* The OECD labelled CSV is 2.7× larger than code-only; labels therefore come from quoted codelists and
  `CL_REGIONAL`. IMF SDMX does not serve CSV (XML only).
* NUTS version: Eurostat responses mix NUTS versions over time and do not state the version per cell; a code is
  kept as published (entity description names the dataset and update date).
* Eurostat NUTS↔OECD TL concordance and NUTS↔ISO 3166-2 are not published by these providers → not asserted.

## Owner decisions

1. **FAO Food Price Index licence**: FAO website terms = non-commercial with acknowledgement (the FFPI is not
   in the CC BY 4.0 "corporate statistical databases" list as far as the terms page shows). Every cell carries
   `flags.licence_restriction = "non_commercial"`; ask FAO or drop for commercial use.
2. **Global Data Lab**: account creation and acceptance of the non-commercial licence.
3. **IMF terms**: same pending review as A-IMF (`registry/sources/imf.yaml`).

## Commands to finish (dev DB, when `df -h /` shows ≥ 5 GB free; ~110 MB raw + ~40 MB Parquet)

```bash
cd /home/user/REVA-World
df -h /                                                    # >= 5 GB free, else stop
flock /tmp/reva-devdb.lock uv run reva seed                # already done 2026-10-17 (connector versions)
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.fao_ffpi
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.eurostat_macro   # + NUTS KG layer
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.wb_icp
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.oecd_macro       # + OECD region KG layer
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.imf_hf
# one dataset at a time if memory is tight: ... ingest atlas.imf_hf --dataset cpi_m
# the lead then runs: reva atlas snapshot --skip-unchanged ; reva links materialise
```

Note: the connector code digests include `atlas_fiscal_common.py` (all five) and `atlas_imf.py` (`atlas.imf_hf`);
if either changes, `run_atlas_ingest` refuses the run until the V4ECON connector version is bumped and re-seeded.

Check after loading: `SELECT property_pid, count(*) FROM kg.statement_current WHERE source_key IN
('eurostat.dissemination','oecd.sdmx') AND (property_pid LIKE 'nuts:%' OR property_pid LIKE 'oecdreg:%' OR
property_pid = 'econ:measured_by') GROUP BY 1;`

## Files

* `src/reva/adapters/connectors/atlas_imf_hf.py`, `atlas_eurostat_macro.py`, `atlas_oecd_macro.py`,
  `atlas_wb_icp.py`, `atlas_fao_ffpi.py`, `econ_regions_kg.py`
* `tests/unit/test_v4econ_parsers.py`, `tests/integration/test_v4econ_ingest.py`,
  `tests/fixtures/source/atlas.{imf_hf,eurostat_macro,oecd_macro,wb_icp,fao_ffpi}/` (small real responses
  captured 2026-10-17, licence in each manifest entry)
* `registry/kg/relations_v4.d/v4econ.yaml`, `registry/atlas/catalog/macro_hf.yaml`,
  `registry/sources/fao_ffpi.yaml`, `registry/atlas/budget_requests/v4econ.yaml`
