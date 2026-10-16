# V3 SIMWORLD — the V2 simulations for every country

Agent SIMWORLD, 2026-10-15/16. Owner request: "tous les pays du monde … changement de l'âge de la retraite …
branché par un pays à tout". Everything here follows the V2 pattern (reviewed built-in connectors →
archived raw bytes → Parquet dataset inputs → belief release → plugin); **no atlas table is a simulation
input**. All new models are experimental (`review_status: maintainer_review_pending`, approval
`candidate`), labelled "experimental model, not yet reviewed", and every output is a *conditional
projection under stated assumptions, not a forecast*.

## What was built

| Piece | Where |
|---|---|
| Versioned plugin registry (several versions of one model key; 1.0.0 stays the default) | `src/reva/simulation/plugins.py` (`VERSIONS`, `get(key, version)`, `versions_of`), version-aware lookups in `compile.py`, `engine.py`, `spec.py`, per-version schema branches in `scenario_schema.py` (`$defs["<key>@<version>"]`), `contracts/scenario.schema.json` regenerated; NL drafting keeps targeting default versions (`src/reva/nl/catalog.py`) |
| `countries: ["ALL"]` for dataset-input models | `src/reva/simulation/compile.py::resolve_all_countries` (subjects with a pinned dataset of a required concept) |
| Reviewed built-in connector `un.wpp@1.0.1` (WPP 2024) | `src/reva/adapters/connectors/un_wpp_demography.py`, registered in `registry.builtin_connectors` |
| Reviewed built-in connector `ilostat.labour_age5@1.0.1` | `src/reva/adapters/connectors/ilostat_labour.py`, registered in `registry.builtin_connectors` |
| Concepts / mappings | `registry/concepts/demography_wpp.yaml`, `registry/concepts/labour_ilostat.yaml`, `registry/mappings/un_wpp.yaml`, `registry/mappings/ilostat_labour.yaml`; source files `un_wpp.yaml`, `ilostat.yaml` annotated (no new source key) |
| `demography.cohort_component@1.1.0` | `src/reva/models/population/cohort_world.py`, WPP adapter `src/reva/models/population/wpp.py` (numerics: `cohort.py`, unchanged) |
| `social.retirement_age@1.1.0` | `src/reva/models/social/retirement_world.py` (rules loader extended in `rules.py`: WBL source kind, WBL transcription note, separate WBL loader) |
| WBL 2026 rule tables (163) + gaps (23) | `registry/rules/retirement_age_wbl2026/<ISO3>.yaml`, `…/wbl2026_gaps.yaml`, archived excerpt `registry/rules/sources/WBL2026_pension_full_benefit_ages_excerpt.txt`; builder `src/reva/cli/build_wbl_rule_tables.py` |
| Model registry | `registry/models/methods.yaml` (1.1.0 entries, listed before the 1.0.0 entries) |
| Capabilities | `simulation.platform.model_versions`, `demography.cohort_component.world`, `social.retirement_age.world` (implemented); `economic.io_price.world` (reserved, reason below) |
| Budget request | `registry/atlas/budget_requests/simworld.yaml` (`un.wpp` 20/day; merged as budgets-1.3.5) |
| Tests | `tests/unit/test_simworld_connectors.py` (7), `tests/unit/test_simworld_models.py` (12), `tests/integration/test_simworld_pipeline.py` (5, DB), `tests/integration/test_simworld_live.py` (`live`); fixtures `tests/fixtures/source/un.wpp/`, `tests/fixtures/source/ilostat.labour_age5/`, helper `tests/fixtures/simworld/simworld_fixtures.py` |

Connector versions: both were first registered as 1.0.0 in the dev database while the code was still
changing; the final content is **`un.wpp@1.0.1`** and **`ilostat.labour_age5@1.0.1`** (registered rows
never edited).

## Model versions: why 1.1.0 and what 1.0.0 keeps

The platform held exactly one plugin per `model_key`, and every V2 test and stored scenario names
`model_version: "1.0.0"`. The registry now keeps every registered version; the first one (1.0.0) remains
`REGISTRY[key]` and is what every key-only lookup returns, so a 1.0.0 spec runs the unchanged 1.0.0 code
(its cache key, schema branch and manifest are unchanged). A spec naming `"1.1.0"` runs the new plugin. (The cohort 1.0.0 sources are untouched; the retirement
1.0.0 implementation digest changes only because `rules.py` gained the WBL loader — same behaviour.)
The 1.0.0 retirement model never sees the WBL tables (they live in their own directory), so its table list,
refusals (ITA, BEL, SWE, POL) and manifest are unchanged.

## 1. Population for every country — connector `un.wpp`

* **Source**: UN DESA Population Division, *World Population Prospects 2024*, official CSV bulk files on
  `population.un.org` (files dated 2024-12-13): `WPP2024_Population1JanuaryBySingleAgeSex_Medium_1950-2023`
  (62.1 MB), `WPP2024_DeathsBySingleAgeSex_Medium_1950-2023` (45.2 MB), `WPP2024_Fertility_by_Age5`
  (82.9 MB, all variants), `WPP2024_Demographic_Indicators_Medium` (16.6 MB).
* **Licence (verified 2026-10-15)**: the WPP 2024 *Summary of Results* (p. ii, PDF sha256 `b5421436…d748d7`)
  states "Figures and tables in this publication can be reproduced without prior permission under a Creative
  Commons license (CC BY 3.0 IGO)". The CSV files themselves carry no licence text; CC BY 3.0 IGO is applied
  as the Division's stated licence (maintainer review pending, as in `registry/sources/un_wpp.yaml`).
* **Fetching**: the connector contract caps one response at 50 MiB, so each file is fetched in byte ranges
  of ≤ 48 MiB (HTTP `Range`, `206 Partial Content`; 2 + 1 + 2 + 1 = 6 requests). Each range is one archived
  acquisition; the URL carries `?bytes=a-b` (ignored by the server) so every part has its own request
  fingerprint. Size and ETag of every file are pinned: a revised file blocks (`file_revised`,
  `content_range_mismatch`). The gzip CRC/length trailer verifies the reassembled file.
* **Dataset inputs** (one Parquet table per dataset × country, years 1990–2023, estimates only — no
  projection year is kept, so nothing is labelled `projection`): 1 January population and calendar-year
  deaths by single age 0–99 and 100+ and sex; births by mother's five-year age group (10–14 … 50–54);
  births by newborn sex **derived** as `Births × SRB/(100+SRB)` (column `derivation`); a summary table
  (TPopulation1Jan, Births, Deaths, NetMigrations, SRB, TFR, e0, CNMR) used for diagnostics only. Values
  are exact persons (WPP thousands with the decimal point shifted). 237 countries/areas, all crosswalk keys
  (aggregates counted and dropped; no unmapped location in WPP 2024). The single-age fertility file
  (302 MB, every variant) was not used: five-year groups keep the download at 207 MB and the cohort model
  handles grouped mother's ages (rate applied to the group's female exposure — stated).
* **Template value**: the pinned `ConnectorSpec.template` literal has no neutral "bulk file" value and
  `src/reva/ports/connectors.py` must not be edited; both connectors use `eurostat_sdmx_csv`, the V2
  built-in-only value (as FIGARO does), and bring their own post-publish hook. A neutral template value
  (e.g. `bulk_csv_gz`) would need an edit of the digest-pinned file by the lead.

## 2. Cohort-component projection for every country — `demography.cohort_component@1.1.0`

* Same numerics as 1.0.0 (`cohort.py` unchanged). New required `assumptions.data_source`:
  `eurostat_demography` (the 31 EU/EFTA countries; **identical values to 1.0.0**, tested on the LU/LI
  fixtures) or `un_wpp2024` (any WPP country; `countries: ["ALL"]` accepted).
* WPP path: migration `zero` or `average_residual`; fertility constant base-period rates; EUROPOP2023
  options refused (`capability_gap`); jump-off ≤ 1 January 2023 (later would need WPP projections as
  observed input: refused); base period from 1990. WPP carries no status flags, so no series-break
  exclusion can happen. Each run reports, per country, REVA's residual migration next to WPP's own
  `NetMigrations` for the migration window (`wpp_net_migration_check`, diagnostic only).
* **Residual migration against WPP** is the migration implied by WPP's own reconstruction plus the gap
  between WPP's single-year mortality and REVA's base-period survival; it is not measured migration.
* Refusals: territories whose base-period exposure/deaths force the open age below 85 (`capability_gap
  demography.cohort_component.open_age`): **NIU, NRU, TKL, TUV, VAT** in the live run (also FLK in the
  back-test with the 2008–2012 base period). Missing datasets → `missing_input` per country.
* A run's results are one manifest bounded by the platform at 32 MiB (`sim.register_artifact`). The live
  "ALL countries × 26 outputs" request would exceed it, so 1.1.0 refuses oversized runs before publication
  with `capability_gap simulation.result_size` (split the countries). Two outputs × 28 years × 231 countries
  (12 936 rows) fit.

### Back-test 2013 → 2023 for every country (WPP is the "truth" here — read this first)

**WPP estimates are themselves model-based reconstructions.** Comparing a projection from 1 January 2013
(rates and residual migration from 2008–2012 only) with the WPP estimate for 1 January 2023 measures how
far REVA's constant-rate projection drifts from WPP's own reconstruction, i.e. *consistency with WPP*, not
accuracy against censuses or registers. Where WPP itself is weakly anchored (no recent census, conflict)
the "error" says little about reality. Data are the release-pinned dataset inputs of the live run.

Countries back-tested: 232; refused: 5 (FLK (capability_gap), NIU (capability_gap), NRU (capability_gap), TKL (capability_gap), VAT (capability_gap))

| statistic (total population APE %, 2023) | avg-residual migration | zero migration | naive no-change | constant growth |
|---|---:|---:|---:|---:|
| median | 2.958 | 4.176 | 11.354 | 3.332 |
| mean | 5.292 | 7.054 | 12.587 | 6.015 |
| 90th percentile | 13.904 | 15.647 | 25.080 | 13.560 |
| max | 51.430 | 62.026 | 33.617 | 60.347 |

Average-residual projection worse than naive no-change: 47 countries (ALB, AND, ARM, BIH, BMU, COK, CPV, CZE, DEU, EST, FJI, FRO, FSM, GEO, GUM, GUY, HUN, IMN, ITA, LBN, LBY, LTU, LVA, MAF, MDA, MNE, MSR, MUS, NCL, OMN, PLW, POL, PRT, ROU, RUS, SGP, SHN, SMR, SSD, SVK, SYR, TON, TUV, TWN, VEN, WLF, XKX).
Worse than constant growth of the total: 99 countries.
* age_0_14: median APE 7.365 %, 90th pct 18.227 %, max 66.189 % (MAF)
* age_15_64: median APE 2.540 %, 90th pct 15.042 %, max 51.403 % (SSD)
* age_65_plus: median APE 3.312 %, 90th pct 11.294 %, max 49.122 % (OMN)
* single-age x sex MAPE (all ages): median 7.444 %, 90th pct 24.188 %

Worst 15 countries (total APE, average-residual migration):

| ISO3 | APE avg-residual | APE zero mig | naive | constant growth | 0-14 | 15-64 | 65+ | single-age MAPE |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| SSD | 51.430 | 30.263 | 1.537 | 60.347 | 52.385 | 51.403 | 38.552 | 200.054 |
| COK | 47.833 | 34.734 | 21.842 | 53.073 | 40.697 | 51.123 | 45.595 | 47.642 |
| MAF | 41.869 | 50.742 | 32.265 | 45.260 | 66.189 | 47.605 | 10.252 | 45.750 |
| TUV | 29.824 | 30.325 | 11.137 | 28.159 | 37.460 | 25.770 | 30.338 | 29.479 |
| OMN | 26.371 | 10.442 | 24.164 | 57.267 | 1.322 | 35.361 | 49.122 | 39.666 |
| AND | 25.013 | 3.791 | 5.477 | 20.643 | 11.842 | 26.839 | 27.140 | 29.851 |
| PLW | 20.764 | 2.566 | 0.755 | 16.063 | 17.618 | 24.273 | 2.908 | 24.302 |
| SMR | 19.848 | 2.138 | 0.883 | 28.217 | 23.096 | 21.042 | 14.208 | 24.188 |
| MHL | 18.996 | 62.026 | 29.441 | 24.773 | 22.737 | 17.246 | 13.609 | 18.818 |
| JOR | 18.421 | 14.776 | 33.009 | 18.736 | 10.146 | 23.011 | 10.431 | 17.176 |
| VEN | 18.250 | 20.323 | 5.260 | 19.939 | 19.090 | 19.713 | 5.543 | 86.730 |
| WLF | 18.233 | 19.613 | 10.113 | 9.042 | 8.331 | 24.171 | 5.903 | 19.546 |
| MLT | 17.312 | 19.171 | 19.256 | 15.547 | 11.105 | 21.822 | 5.983 | 15.040 |
| LBY | 17.273 | 0.177 | 14.203 | 12.321 | 14.247 | 19.411 | 5.275 | 13.108 |
| SAU | 16.053 | 0.360 | 16.200 | 22.017 | 18.227 | 15.547 | 10.262 | 24.654 |

Bad results are kept. The largest errors come from (a) conflict and displacement shocks after 2012 (South
Sudan, Syria, Libya, Venezuela, Yemen — the residual average of 2008–2012 cannot know them), (b) very
small populations where a few hundred migrants or one revision dominate (Cook Islands, Saint-Martin,
Tuvalu, Palau, San Marino, Andorra), and (c) Gulf states with labour-migration swings (Oman, Jordan's
refugee inflow). For 47 countries the projection is worse than carrying the 2013 total forward unchanged,
and for 99 worse than compounding the 2008→2013 average growth of the total: a cohort model with frozen
2008–2012 migration is not a better *total-population* predictor than simple trends in many places; its
value is the age structure (65+ median APE 3.3 %), which trend benchmarks do not provide.
The full per-country table is in the appendix and in `docs/dev/v3_simworld_backtest.json`.

## 3. Retirement age for more countries — `social.retirement_age@1.1.0`

### Rule tables (WBL 2026)

* Source: World Bank *Women, Business and the Law 2026* data download (CC BY 4.0), sheet `10. Pension`,
  questions 10.1.1.1/10.1.1.2 "Ages men/women (full benefits)" and WBL's *Legal basis* column, law as of
  **1 October 2025** (workbook sha256 `c51f7210…e666c`, retrieved 2026-10-15). The excerpt committed in
  `registry/rules/sources/WBL2026_pension_full_benefit_ages_excerpt.txt` holds the verbatim cell values
  with cell references; every row's locator is checked against it by the loader.
* **163 tables** (`registry/rules/retirement_age_wbl2026/`), each marked `transcribed by agent SIMWORLD from
  WBL 2026, maintainer review pending`, source kind `secondary_legal_dataset` (the legal text itself is not
  archived), one track per sex (`model_lever.sex_tracks`), age kind `normal_pension_age`. WBL gives one age
  at one date and no reform schedule, so each table has two paths: `wbl2026_in_force_2025` (claim year 2025
  only, verbatim) and `wbl2026_held_constant` (that age for every claim year — an explicitly labelled
  **counterfactual** reference; rows outside 2025 are `counterfactual_extension` with a note). No phased
  schedule was transcribed (WBL gives none; no official text was archived for these countries).
* WBL code `KSV` (Kosovo) is mapped to the crosswalk key `XKX` (stated in that table's notes).
* **23 economies refused** (`wbl2026_gaps.yaml`, reason per country): no pension system stated ("NS":
  AFG, BGD, BTN, ERI, LBN, MMR, PSE, SOM, ZAF) and ages published to one decimal that are not whole
  months, so the month is not in the source (ALB 61.8 women, BGR 64.7/62.3, CHN 60.2/50.3, CZE 64.6/63.2,
  HRV, ISR, LTU, PRT, PRI, ROU, SRB, SVK, USA 66.8, VNM) — never rounded or guessed. FRA, DEU, ESP, NLD keep
  their law-text tables (paths named after the law, not `wbl2026_*`). Not WBL economies (no table): AND, CUB,
  LIE, MCO, NRU, PRK, TKM, TUV and the territories.

### Inputs and method

* Population: the 1.1.0 cohort numerics on WPP (jump-off `reference_year + 1` ≤ 2023, so
  `reference_year` ≤ 2022).
* Rates: ILOSTAT `EMP_DWAP_SEX_AGE_RT_A` and `EAP_DWAP_SEX_AGE_RT_A` (CC BY 4.0, ILO-selected source per
  country-year, status flags kept; `KOS` → `XKX` reviewed against the ILOSTAT area dictionary; `ANT`
  unmapped), five-year bands 55–59 and 60–64 of the latest year ≤ `reference_year` (at most 10 years older)
  where both rates exist for every (sex, band) cell — **held constant from that year** (stated; the year is
  in every row's applicability).
* **Ages 55–64 only.** ILOSTAT's five-year classification ends with 65+; there is no 65–69 band. A 65+
  rate is not a 65–69 rate, so it is not used: eligibility changes at any age outside 55–64 are refused
  (`capability_gap social.retirement_age.age_range`), never cut.
* Same eligibility arithmetic, pension-receipt proxy `rho = 1 − LFPR/100` (unknown direction; L2 bounding
  variants use the 55–59 and 60–64 bands at every age) and cited employment-response grid as 1.0.0. Every
  literature point is a European estimate: outside its country the value is a *transferred* assumption,
  stated in every row's qualification and caveats.
* Outputs: `employed_persons_55_64`, `employed_persons_55_64_reference`, `employment_change_55_64`,
  `pension_recipients_change_55_64`, `employment_rate.{F,M}.{Y55-59,Y60-64}`, `old_age_dependency_ratio`.
  **Not computed** (refused when requested): pension recipient levels and expenditure (no open per-country
  beneficiary/expenditure data wired for this path) and every 55–69 aggregate.
* A country whose response assumption pushes an employment rate out of [0, 100], or whose ILOSTAT
  employment rate exceeds its participation rate (KAZ 2022, women 55–59), is reported per country
  (`incompatible` / `inconsistent_input`) and skipped under `skip_missing_countries`.

**Known bias, read before using the numbers**: the model treats every person of an affected age as subject
to the legal age. Where the contributory pension system covers a small share of workers (most low-income
countries; e.g. India's EPS age 58 applies to formal-sector members only) the affected population, and so
the employment and recipient changes, are **overstated** — they describe what the stated legal change would
do if the whole population were covered. Pension coverage (e.g. ILOSTAT SDG 1.3.1) is not used yet. There is
no back-test outside France.
*(Addressed in `social.retirement_age@1.2.0`, which applies the change to the pension-covered share only;
1.1.0 is unchanged — see the SIMFIX section at the end of this file.)*

## 4. Input-output price model for every country — not implemented

`economic.io_price@1.0.0` runs for the 49 FIGARO economies + rest of the world; any other country is
refused (`capability_gap economic.io_price.coverage`). Checked on 2026-10-15:

| Candidate | Coverage | Licence | Access / size | Decision |
|---|---|---|---|---|
| OECD ICIO 2023 ed. | 76 economies + rest of world × 45 industries | OECD data CC BY 4.0 (OECD terms, could not be re-read: page 403) | oecd.org bulk zips (≈300–500 MB per 5-year block, ≈1 GB CSV per year) answer **HTTP 403** to this environment's clients (bot protection); the tables are not in the OECD SDMX API (only GHG footprint indicators) | not implemented: no archivable automated download |
| EXIOBASE 3.11.2 (Zenodo 10.5281/zenodo.23039074, 2026-09-30) | 44 countries + 5 rest-of-world regions | v3.9+: customised **CC BY-NC-SA** (non-commercial); v3.8.2 was CC BY-SA 4.0 | IOT per year ≈ 270–300 MB zip (60 files, 15 GB in all) | not implemented: no coverage gain over FIGARO (same major economies) and non-commercial licence |
| Eora | ~190 countries | not open (registration, restrictive terms) | — | excluded |

Capability `economic.io_price.world` stays `reserved` with this reason. Feasible next step: ICIO via a
maintainer-downloaded archive (manual retrieval, hashed, committed outside the repo) through the same
dataset-input path, adding ~26 economies (e.g. VNM, THA, MYS, PHL, CHL, COL, PER, EGY, MAR, NGA, SEN, PAK,
BGD, KAZ, NZL, ISR); every other country would still be inside a rest-of-world block.

## 5. Live run (throwaway database, never `reva`)

Run `REVA_SIMWORLD_LIVE_OUT=… uv run pytest -m live tests/integration/test_simworld_live.py` on 2026-10-16
(18:10–18:24 UTC, 13 min 41 s, passed), throwaway database `reva_live_simworld_1c79ee35cd` (dropped by the
fixture), object store in the agent scratch folder (deleted afterwards). Every request reserved and metered
through the throwaway database's budget ledger (policy budgets-1.3.5):

| Step | Result | Time |
|---|---|---|
| `un.wpp@1.0.1` ingest | 6 byte-range requests, 206 708 142 bytes, 0 refused; 6 acquisitions; 948 dataset inputs (237 countries × 4) | 229 s (incl. parsing ≈ 15 M CSV rows and registration) |
| `ilostat.labour_age5@1.0.1` ingest | 2 requests, 4 894 525 bytes; 217 (employment ratio) + 225 (participation) dataset inputs | 31 s |
| `worldbank.wdi.indicators` (a release needs one selected estimate; live, same origin) | 5 requests, 44 351 bytes | 3 s |
| Belief release `780faddf…` | 1 390 dataset inputs pinned | 2.6 s |
| Cohort 1.1.0, `countries ["ALL"]`, WPP, jump-off 2023, 27 years, L1, 2 outputs | succeeded: **231 countries projected**, 5 refused (NIU, NRU, TKL, TUV, VAT: open age), 12 936 rows | 78 s |
| Retirement 1.1.0, ALL, WBL age **+24 months for cohorts born 1965+**, L1 | succeeded: **88 countries**, 62 refused (age range: the +2 years reaches 65+ or the age is below 55), 74 no rule table, 12 missing ILOSTAT inputs | 115 s |
| Retirement 1.1.0, ALL, **target age 62 for cohorts born 1965+**, L1 | succeeded: **126 countries**, 19 age range, 4 response assumption out of bounds (LUX, MUS, SDN, TTO), 1 inconsistent input (KAZ), 4 law-text countries (DEU, ESP, FRA, NLD: their tables have no `wbl2026_held_constant` path), 70 no rule table, 12 missing inputs | 221 s |
| Retirement 1.1.0, +24 months, **L2** grid, SEN BRA JPN IND MEX | succeeded for SEN and IND (858 rows); BRA, JPN, MEX refused (65+ ages) | 7 s |
| Back-test 2013 → 2023, every country, from the pinned datasets | 232 countries, 5 refused (FLK, NIU, NRU, TKL, VAT) | 106 s |

Disk: 272 MB object store in all (raw archives 202 MiB, Parquet datasets + manifests 45 MiB, run manifests); deleted after the run;
committed fixtures 368 kB. An earlier attempt of the same run (00:14–00:20 UTC) gave identical ingest,
cohort and back-test numbers; its "target 62" run was refused as a whole because four countries' response
assumption left [0, 100] at compute — fixed (such a country is now reported and skipped, tested).

Selected values (conditional projections, not forecasts):

| Country | population 1 Jan 2023 (WPP) | 2050 (avg. residual migration, constant rates) | OADR 65+/15-64 2023 → 2050 |
|---|---:|---:|---|
| SEN | 17 867 073 | 35 013 494 | 6.2 → 7.9 |
| NGA | 225 494 749 | 411 706 029 | 5.5 → 6.5 |
| IND | 1 431 702 948 | 1 658 200 808 | 10.0 → 17.1 |
| CHN | 1 424 260 946 | 1 234 622 048 | 20.4 → 43.4 |
| BRA | 210 707 000 | 209 239 178 | 15.0 → 31.4 |
| USA | 342 475 098 | 360 217 390 | 26.4 → 33.4 |
| JPN | 124 670 714 | 101 271 418 | 50.1 → 65.9 |

Retirement, target 62 from the 1965 cohort (L1, central grid point Rabaté & Rochut 2020 FRA, transferred), 2030:
SEN (60 → 62) employment 55–64 **+45 453**, persons kept out of pension receipt −104 011; JPN (65 → 62)
−517 128 / +1 183 360; KOR +260 568 / −596 266; IDN +1 235 576 / −2 827 404; **IND +10 555 364 / −24 154 151** —
the India figure shows the full-coverage overstatement described above (EPS age 58 applied to everybody
aged 58–61, while the scheme covers formal-sector members only). L2 for SEN (+24 months, 2032): employment
change 0 (no-response reference) to 64 615 (Geyer & Welteke 2017), central 48 853.

## Coverage summary (which countries each model runs for)

| Model | Runs for | Refused (typed) |
|---|---|---|
| `demography.cohort_component@1.1.0` (`un_wpp2024`) | 231 of the 237 WPP countries/areas in the live run (every WPP key; `ALL` selects 236 — XKX has no ISO 3166 alias and must be named explicitly) | NIU, NRU, TKL, TUV, VAT (open age < 85, `capability_gap`); FLK also with a 2008–2012 base period |
| `demography.cohort_component@1.1.0` (`eurostat_demography`) / 1.0.0 | 31 EU/EFTA countries | as 1.0.0 |
| `social.retirement_age@1.1.0` | 126 countries with a WBL table, WPP data and complete ILOSTAT rates (live, target 62); which countries run depends on the requested ages (55–64 only) | 70 without a rule table (23 WBL gaps + non-WBL territories), 12 missing ILOSTAT rates within 10 years (CAF, COG, DMA, GAB, GNQ, HTI, KNA, KWT, LBY, SSD, SYR, VCT), eligibility outside 55–64 (e.g. NGA, LKA, MWI age 50; AUS, GBR, ITA, NOR 65+ references), KAZ (employment > participation), law-text countries need their own paths |
| `social.retirement_age@1.0.0` | FRA, DEU, ESP, NLD (unchanged) | ITA, BEL, SWE, POL and every other country |
| `economic.io_price@1.0.0` | 49 FIGARO economies + rest of world | every other country (`economic.io_price.coverage`); global extension reserved (section 4) |

## Assumptions (all stated in the outputs)

* Cohort 1.1.0 on WPP: mortality constant at base-period rates (no improvement); migrants added at the end
  of the year; residual migration = WPP-implied migration + survival differences; fertility constant by
  five-year mother's age group; births by sex from WPP total births and SRB; open age never split.
* Retirement 1.1.0: WBL age held constant (counterfactual reference); ILOSTAT rates of the latest complete
  year held constant; rho proxy of unknown direction; transferred European response estimates; full pension
  coverage assumed (overstatement where coverage is low); claim-year ages attained once (model assumption).

## Licences

UN WPP 2024: CC BY 3.0 IGO (stated by UN DESA for WPP 2024 figures and tables; CSV files carry no text).
ILOSTAT: CC BY 4.0. World Bank WBL: CC BY 4.0. Attribution strings are stored with every dataset
(`rights`) and rule-table source.

## Open issues / requests

* Lead: a neutral connector template value (e.g. `bulk_csv_gz`) in the pinned `src/reva/ports/connectors.py`
  instead of reusing `eurostat_sdmx_csv`.
* Schedules: no `ingest:un.wpp` / `ingest:ilostat.labour_age5` schedule was added to `src/reva/cli/seed.py`
  (WPP is biennial; ILOSTAT monthly would be reasonable).
* NL drafting (`nl/catalog.py`) only targets default (1.0.0) versions; drafting 1.1.0 specs is NLC's call.
* Pension coverage scaling (ILOSTAT SDG 1.3.1 / social security inquiry) to remove the full-coverage
  overstatement; pension recipient levels outside ESSPROS.
* WBL ages with one decimal (23 refusals) need the law text (or SSA SSPTW 2018-19, older) to recover months.
* `tests/invariants/test_invariant_30_budgets.py::test_budget_keys_are_registered_sources_and_cover_every_connector`
  fails on sources registered by other agents (grambank, wals, glottolog …) not yet in budgets.yaml — not
  SIMWORLD files.

## Appendix — back-test table (all countries)

| ISO3 | open age | APE avg-res. | APE zero | naive | const. growth | linear | 0-14 | 15-64 | 65+ | MAPE single age |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| ABW | 93 | 1.863 | 2.922 | 5.297 | 4.611 | 3.896 | 7.312 | 1.383 | 2.045 | 5.533 |
| AFG | 100 | 10.855 | 8.284 | 23.903 | 9.451 | 1.388 | 17.612 | 5.660 | 5.977 | 11.398 |
| AGO | 100 | 2.681 | 1.659 | 29.076 | 4.240 | 4.232 | 4.443 | 1.451 | 2.364 | 7.458 |
| AIA | 90 | 7.651 | 3.306 | 8.505 | 4.130 | 4.281 | 3.438 | 9.573 | 12.573 | 14.977 |
| ALB | 100 | 6.159 | 9.141 | 3.096 | 3.598 | 3.943 | 4.943 | 7.894 | 0.220 | 7.324 |
| AND | 100 | 25.013 | 3.791 | 5.477 | 20.643 | 22.751 | 11.842 | 26.839 | 27.140 | 29.851 |
| ARE | 100 | 7.893 | 20.014 | 26.760 | 18.533 | 4.578 | 11.857 | 6.372 | 44.205 | 33.126 |
| ARG | 100 | 2.510 | 2.630 | 6.797 | 3.484 | 2.705 | 11.861 | 0.334 | 0.509 | 3.012 |
| ARM | 100 | 6.032 | 3.325 | 0.691 | 2.325 | 2.395 | 3.894 | 7.924 | 0.455 | 8.497 |
| ASM | 92 | 4.505 | 35.848 | 13.286 | 6.455 | 6.129 | 16.928 | 0.346 | 0.594 | 11.092 |
| ATG | 100 | 5.384 | 1.971 | 5.784 | 7.040 | 5.864 | 16.437 | 3.464 | 0.835 | 5.541 |
| AUS | 100 | 2.231 | 7.437 | 12.428 | 3.965 | 1.972 | 8.455 | 1.916 | 3.143 | 4.840 |
| AUT | 100 | 5.650 | 9.198 | 7.472 | 4.243 | 4.325 | 7.389 | 5.591 | 4.593 | 6.888 |
| AZE | 100 | 0.248 | 0.692 | 8.629 | 3.114 | 2.091 | 1.261 | 0.329 | 3.912 | 6.189 |
| BDI | 100 | 12.089 | 6.458 | 24.027 | 15.641 | 4.761 | 14.846 | 10.125 | 2.385 | 46.244 |
| BEL | 100 | 2.550 | 4.519 | 4.782 | 3.848 | 3.302 | 9.272 | 2.482 | 2.776 | 5.229 |
| BEN | 100 | 1.915 | 1.559 | 24.277 | 2.033 | 3.298 | 4.486 | 0.046 | 0.201 | 2.010 |
| BES | 95 | 9.520 | 19.049 | 20.880 | 22.509 | 10.192 | 5.679 | 10.105 | 11.049 | 11.460 |
| BFA | 98 | 3.142 | 5.016 | 23.420 | 2.956 | 2.352 | 9.841 | 1.783 | 3.222 | 6.175 |
| BGD | 100 | 0.246 | 6.278 | 8.578 | 0.295 | 0.302 | 0.655 | 0.021 | 6.741 | 15.250 |
| BGR | 100 | 4.133 | 0.580 | 6.907 | 0.766 | 1.205 | 13.520 | 3.514 | 0.340 | 5.691 |
| BHR | 100 | 2.828 | 11.718 | 20.668 | 1.350 | 2.379 | 11.703 | 1.275 | 11.294 | 15.917 |
| BHS | 100 | 4.992 | 0.658 | 5.340 | 6.396 | 5.407 | 18.292 | 1.943 | 2.100 | 5.537 |
| BIH | 100 | 14.104 | 8.654 | 13.745 | 5.269 | 8.042 | 24.380 | 16.367 | 0.786 | 16.013 |
| BLM | 90 | 1.873 | 12.253 | 15.792 | 0.242 | 2.110 | 26.833 | 15.048 | 33.193 | 38.369 |
| BLR | 100 | 0.685 | 1.532 | 3.463 | 1.693 | 1.670 | 2.032 | 0.399 | 3.622 | 8.002 |
| BLZ | 100 | 3.916 | 1.411 | 17.182 | 4.962 | 1.324 | 14.224 | 0.183 | 3.548 | 7.148 |
| BMU | 100 | 2.658 | 1.431 | 1.757 | 0.424 | 0.437 | 2.168 | 2.517 | 6.403 | 11.479 |
| BOL | 100 | 2.955 | 4.214 | 12.831 | 2.632 | 0.838 | 9.109 | 0.229 | 6.178 | 4.790 |
| BRA | 100 | 0.060 | 1.049 | 6.184 | 2.105 | 1.594 | 2.121 | 0.486 | 1.417 | 2.432 |
| BRB | 100 | 0.667 | 0.255 | 1.766 | 2.142 | 2.029 | 3.900 | 0.472 | 1.844 | 3.082 |
| BRN | 100 | 5.201 | 0.259 | 10.639 | 6.403 | 4.297 | 18.138 | 3.100 | 13.875 | 16.282 |
| BTN | 100 | 2.218 | 4.134 | 7.961 | 2.788 | 1.930 | 21.143 | 3.453 | 1.676 | 8.804 |
| BWA | 100 | 2.655 | 2.135 | 13.620 | 4.698 | 2.219 | 7.191 | 0.312 | 3.316 | 9.649 |
| CAF | 100 | 1.627 | 12.620 | 8.613 | 3.718 | 3.906 | 2.127 | 0.936 | 6.011 | 15.776 |
| CAN | 100 | 1.544 | 8.171 | 10.111 | 0.153 | 0.650 | 4.706 | 2.749 | 2.463 | 4.282 |
| CHE | 100 | 0.597 | 8.915 | 9.068 | 2.129 | 1.191 | 0.635 | 1.413 | 2.187 | 2.891 |
| CHL | 100 | 2.699 | 4.208 | 10.209 | 0.894 | 1.561 | 7.192 | 5.318 | 2.213 | 5.868 |
| CHN | 100 | 0.281 | 0.531 | 3.489 | 3.137 | 2.814 | 5.577 | 0.307 | 3.183 | 4.718 |
| CIV | 100 | 4.119 | 2.808 | 22.994 | 4.390 | 7.200 | 6.649 | 3.194 | 16.743 | 31.859 |
| CMR | 100 | 0.203 | 0.663 | 24.627 | 0.142 | 4.662 | 3.128 | 1.908 | 1.900 | 3.496 |
| COD | 100 | 0.747 | 0.697 | 28.424 | 1.240 | 7.140 | 0.102 | 1.401 | 2.673 | 6.172 |
| COG | 100 | 7.005 | 3.710 | 21.443 | 10.146 | 2.986 | 8.393 | 6.180 | 3.218 | 27.885 |
| COK | 90 | 47.833 | 34.734 | 21.842 | 53.073 | 48.118 | 40.697 | 51.123 | 45.595 | 47.642 |
| COL | 100 | 2.957 | 1.807 | 11.710 | 1.540 | 2.342 | 5.570 | 5.779 | 0.724 | 7.567 |
| COM | 97 | 1.177 | 7.161 | 18.258 | 0.184 | 2.718 | 6.735 | 2.225 | 1.424 | 7.056 |
| CPV | 99 | 2.969 | 13.865 | 1.708 | 3.593 | 3.387 | 2.946 | 3.623 | 3.719 | 7.667 |
| CRI | 100 | 2.240 | 1.655 | 7.898 | 4.206 | 3.131 | 10.570 | 0.330 | 0.516 | 2.542 |
| CUB | 100 | 0.526 | 3.762 | 2.381 | 2.599 | 2.598 | 1.604 | 0.344 | 3.192 | 5.160 |
| CUW | 96 | 5.870 | 10.324 | 12.580 | 3.469 | 4.124 | 9.721 | 11.523 | 3.314 | 13.783 |
| CYM | 95 | 0.193 | 15.647 | 19.939 | 5.573 | 0.744 | 4.388 | 0.016 | 6.444 | 8.089 |
| CYP | 100 | 1.478 | 8.437 | 12.199 | 3.547 | 1.703 | 0.025 | 2.056 | 0.338 | 3.761 |
| CZE | 100 | 3.076 | 5.398 | 2.936 | 0.037 | 0.101 | 6.216 | 2.531 | 2.346 | 4.869 |
| DEU | 100 | 6.746 | 8.471 | 4.124 | 4.805 | 4.808 | 14.366 | 5.949 | 4.290 | 7.592 |
| DJI | 97 | 3.168 | 0.988 | 14.777 | 3.965 | 1.349 | 11.116 | 0.043 | 2.458 | 3.452 |
| DMA | 100 | 2.933 | 7.693 | 3.451 | 3.791 | 3.790 | 15.466 | 0.394 | 3.077 | 10.611 |
| DNK | 100 | 2.107 | 5.569 | 5.551 | 1.117 | 1.267 | 2.403 | 2.467 | 4.507 | 3.969 |
| DOM | 100 | 0.650 | 4.458 | 10.193 | 1.881 | 0.786 | 4.667 | 0.874 | 0.542 | 4.253 |
| DZA | 100 | 0.972 | 0.662 | 17.026 | 0.888 | 1.573 | 3.040 | 0.046 | 1.046 | 3.361 |
| ECU | 100 | 2.244 | 2.791 | 12.339 | 3.384 | 1.543 | 14.148 | 2.133 | 1.164 | 4.186 |
| EGY | 100 | 0.405 | 1.280 | 17.008 | 1.753 | 0.927 | 3.404 | 1.220 | 1.121 | 8.165 |
| ERI | 100 | 8.383 | 15.522 | 11.208 | 7.847 | 5.242 | 12.371 | 6.295 | 0.446 | 8.715 |
| ESH | 100 | 5.778 | 11.553 | 21.790 | 12.849 | 4.411 | 9.469 | 5.156 | 1.164 | 8.952 |
| ESP | 100 | 0.802 | 3.290 | 1.963 | 2.405 | 2.265 | 3.318 | 1.333 | 1.797 | 3.234 |
| EST | 100 | 9.004 | 6.457 | 3.443 | 6.067 | 6.122 | 6.766 | 9.588 | 8.966 | 11.305 |
| ETH | 100 | 3.311 | 2.671 | 23.540 | 1.443 | 3.381 | 10.450 | 1.125 | 5.580 | 5.873 |
| FIN | 100 | 0.243 | 2.869 | 2.926 | 1.753 | 1.590 | 11.867 | 1.220 | 3.409 | 5.026 |
| FJI | 98 | 1.052 | 13.317 | 0.654 | 2.548 | 2.473 | 5.955 | 0.990 | 1.076 | 5.875 |
| FRA | 100 | 0.155 | 1.367 | 3.253 | 1.795 | 1.605 | 5.276 | 0.649 | 3.013 | 2.964 |
| FRO | 100 | 14.372 | 8.241 | 11.360 | 12.526 | 12.537 | 18.168 | 16.304 | 3.278 | 14.313 |
| FSM | 92 | 7.461 | 16.109 | 3.891 | 6.385 | 6.434 | 3.992 | 9.078 | 9.475 | 13.424 |
| GAB | 100 | 5.314 | 2.668 | 23.512 | 8.142 | 0.810 | 4.983 | 5.760 | 1.771 | 8.532 |
| GBR | 100 | 0.139 | 4.148 | 6.245 | 1.115 | 0.708 | 4.355 | 0.639 | 2.605 | 3.112 |
| GEO | 100 | 9.903 | 0.571 | 0.442 | 6.024 | 6.353 | 18.147 | 9.306 | 0.919 | 10.296 |
| GGY | 100 | 2.201 | 3.342 | 3.005 | 0.502 | 0.550 | 3.465 | 2.941 | 3.780 | 5.091 |
| GHA | 100 | 2.393 | 2.019 | 19.151 | 3.239 | 0.546 | 6.382 | 0.229 | 1.798 | 4.621 |
| GIB | 100 | 5.338 | 11.937 | 15.312 | 3.807 | 4.860 | 8.388 | 6.282 | 1.298 | 6.260 |
| GIN | 100 | 1.060 | 3.711 | 22.314 | 0.715 | 4.379 | 6.625 | 2.853 | 3.209 | 6.204 |
| GLP | 100 | 4.714 | 12.922 | 8.411 | 8.530 | 8.530 | 17.630 | 4.747 | 4.762 | 7.689 |
| GMB | 100 | 5.108 | 6.256 | 22.255 | 4.135 | 1.116 | 12.680 | 0.087 | 1.272 | 4.688 |
| GNB | 100 | 2.270 | 3.517 | 21.409 | 1.946 | 2.235 | 8.305 | 1.569 | 3.457 | 4.290 |
| GNQ | 100 | 6.277 | 6.420 | 27.713 | 12.112 | 0.771 | 8.859 | 4.981 | 0.465 | 10.456 |
| GRC | 100 | 1.873 | 2.960 | 6.353 | 5.350 | 5.343 | 2.882 | 2.196 | 0.408 | 4.537 |
| GRD | 100 | 0.379 | 4.801 | 3.310 | 0.965 | 0.828 | 8.604 | 3.777 | 4.075 | 8.745 |
| GRL | 91 | 0.467 | 6.812 | 1.262 | 1.051 | 1.051 | 4.183 | 1.077 | 3.530 | 5.472 |
| GTM | 100 | 4.496 | 7.047 | 15.263 | 3.414 | 0.803 | 13.861 | 0.248 | 3.371 | 7.612 |
| GUF | 97 | 2.032 | 2.228 | 17.157 | 3.297 | 0.151 | 7.200 | 0.323 | 6.186 | 11.258 |
| GUM | 100 | 0.923 | 15.321 | 0.195 | 1.052 | 1.047 | 1.845 | 0.539 | 6.551 | 7.223 |
| GUY | 100 | 11.921 | 4.816 | 9.228 | 11.081 | 11.110 | 10.209 | 13.720 | 1.638 | 10.748 |
| HKG | 100 | 1.817 | 2.084 | 2.920 | 3.788 | 3.459 | 15.522 | 1.219 | 3.258 | 9.625 |
| HND | 100 | 2.888 | 3.516 | 16.600 | 2.919 | 0.048 | 9.865 | 0.206 | 1.322 | 10.086 |
| HRV | 100 | 3.113 | 4.084 | 8.942 | 5.739 | 5.667 | 7.341 | 4.248 | 2.702 | 6.303 |
| HTI | 98 | 1.670 | 4.587 | 12.298 | 1.072 | 0.285 | 8.611 | 1.381 | 4.053 | 4.485 |
| HUN | 100 | 3.080 | 3.956 | 2.289 | 0.125 | 0.168 | 11.726 | 2.299 | 0.547 | 6.841 |
| IDN | 100 | 1.135 | 1.366 | 9.182 | 3.149 | 2.020 | 5.348 | 0.280 | 0.245 | 3.177 |
| IMN | 100 | 4.894 | 0.070 | 0.109 | 6.769 | 6.455 | 16.323 | 3.280 | 1.950 | 6.685 |
| IND | 100 | 2.640 | 2.942 | 10.083 | 3.601 | 2.214 | 11.439 | 0.250 | 1.197 | 3.948 |
| IRL | 100 | 7.456 | 4.205 | 10.746 | 4.313 | 4.642 | 1.476 | 10.447 | 5.914 | 9.621 |
| IRN | 100 | 2.117 | 1.619 | 11.347 | 0.428 | 0.629 | 2.241 | 2.118 | 1.748 | 8.956 |
| IRQ | 100 | 11.353 | 2.910 | 22.557 | 14.724 | 5.073 | 20.306 | 6.146 | 3.765 | 10.850 |
| ISL | 100 | 13.904 | 9.994 | 16.233 | 12.784 | 12.887 | 0.415 | 19.912 | 3.775 | 12.192 |
| ISR | 100 | 1.916 | 3.349 | 16.458 | 0.108 | 2.174 | 1.533 | 2.074 | 2.006 | 2.682 |
| ITA | 100 | 3.447 | 1.830 | 1.771 | 6.158 | 6.021 | 9.217 | 3.957 | 0.852 | 4.766 |
| JAM | 100 | 1.455 | 6.630 | 2.163 | 2.082 | 1.949 | 13.623 | 1.762 | 1.356 | 4.245 |
| JEY | 100 | 3.351 | 3.568 | 4.089 | 5.557 | 4.886 | 6.188 | 3.730 | 0.260 | 5.079 |
| JOR | 100 | 18.421 | 14.776 | 33.009 | 18.736 | 20.675 | 10.146 | 23.011 | 10.431 | 17.176 |
| JPN | 100 | 1.589 | 2.493 | 2.536 | 2.062 | 2.061 | 2.248 | 1.004 | 2.495 | 2.742 |
| KAZ | 100 | 4.219 | 3.654 | 13.701 | 1.797 | 2.902 | 6.992 | 2.482 | 7.420 | 6.704 |
| KEN | 100 | 6.612 | 7.093 | 18.882 | 7.535 | 2.447 | 16.568 | 1.083 | 9.962 | 9.915 |
| KGZ | 99 | 7.851 | 2.629 | 18.089 | 4.738 | 6.176 | 9.174 | 8.907 | 12.661 | 15.446 |
| KHM | 100 | 0.393 | 4.050 | 13.039 | 1.414 | 0.170 | 4.289 | 1.254 | 1.787 | 2.916 |
| KIR | 92 | 4.523 | 9.053 | 14.034 | 3.367 | 1.104 | 10.190 | 1.556 | 0.162 | 6.161 |
| KNA | 98 | 0.581 | 6.482 | 0.728 | 1.942 | 1.931 | 0.168 | 0.306 | 3.185 | 7.429 |
| KOR | 100 | 0.563 | 2.908 | 3.714 | 2.660 | 2.360 | 11.776 | 1.283 | 5.428 | 7.113 |
| KWT | 100 | 8.687 | 19.209 | 28.496 | 26.242 | 6.885 | 16.767 | 5.659 | 38.646 | 29.411 |
| LAO | 100 | 0.468 | 4.673 | 13.755 | 0.185 | 1.602 | 3.352 | 2.474 | 2.337 | 53.442 |
| LBN | 100 | 8.302 | 5.200 | 7.370 | 10.250 | 8.078 | 2.472 | 14.185 | 0.229 | 17.505 |
| LBR | 100 | 12.084 | 7.968 | 18.619 | 12.741 | 5.859 | 15.546 | 10.029 | 4.994 | 31.823 |
| LBY | 100 | 17.273 | 0.177 | 14.203 | 12.321 | 12.352 | 14.247 | 19.411 | 5.275 | 13.108 |
| LCA | 96 | 0.109 | 2.208 | 3.390 | 2.073 | 1.851 | 4.110 | 2.459 | 12.668 | 9.194 |
| LIE | 100 | 1.392 | 5.385 | 6.820 | 0.908 | 0.459 | 2.600 | 1.236 | 1.033 | 6.206 |
| LKA | 100 | 2.698 | 1.208 | 7.168 | 0.728 | 1.045 | 3.030 | 4.128 | 5.707 | 6.591 |
| LSO | 100 | 2.494 | 0.529 | 11.031 | 3.110 | 3.603 | 3.809 | 5.572 | 11.265 | 7.301 |
| LTU | 100 | 15.719 | 1.096 | 4.613 | 10.452 | 12.303 | 16.044 | 19.386 | 3.375 | 13.408 |
| LUX | 100 | 2.627 | 17.493 | 18.752 | 0.012 | 2.735 | 0.996 | 3.244 | 3.611 | 5.574 |
| LVA | 100 | 14.112 | 0.331 | 7.377 | 8.454 | 10.452 | 22.503 | 14.695 | 6.084 | 14.236 |
| MAC | 100 | 2.147 | 12.279 | 16.350 | 5.093 | 1.690 | 6.767 | 3.951 | 1.790 | 8.963 |
| MAF | 95 | 41.869 | 50.742 | 32.265 | 45.260 | 44.375 | 66.189 | 47.605 | 10.252 | 45.750 |
| MAR | 100 | 0.445 | 3.022 | 10.523 | 2.325 | 1.089 | 5.449 | 1.035 | 3.907 | 4.312 |
| MCO | 100 | 1.521 | 15.067 | 10.336 | 9.790 | 6.932 | 1.762 | 3.236 | 0.942 | 5.827 |
| MDA | 100 | 15.923 | 12.002 | 11.838 | 8.823 | 12.212 | 30.687 | 12.568 | 10.720 | 17.334 |
| MDG | 100 | 3.323 | 3.394 | 22.815 | 2.717 | 2.261 | 8.474 | 0.100 | 0.396 | 3.585 |
| MDV | 100 | 0.174 | 12.498 | 25.080 | 6.350 | 1.004 | 10.537 | 2.874 | 3.238 | 9.999 |
| MEX | 100 | 3.568 | 4.081 | 8.972 | 4.839 | 3.443 | 8.977 | 1.696 | 2.246 | 4.616 |
| MHL | 90 | 18.996 | 62.026 | 29.441 | 24.773 | 24.643 | 22.737 | 17.246 | 13.609 | 18.818 |
| MKD | 100 | 0.322 | 10.970 | 10.043 | 4.171 | 3.925 | 0.443 | 1.237 | 3.280 | 7.388 |
| MLI | 96 | 0.214 | 3.407 | 26.626 | 0.292 | 5.397 | 2.502 | 1.751 | 2.495 | 4.162 |
| MLT | 100 | 17.312 | 19.171 | 19.256 | 15.547 | 15.670 | 11.105 | 21.822 | 5.983 | 15.040 |
| MMR | 100 | 1.462 | 1.047 | 7.223 | 0.355 | 0.714 | 0.382 | 1.971 | 2.968 | 6.534 |
| MNE | 100 | 1.483 | 2.386 | 0.948 | 1.123 | 1.123 | 5.621 | 0.244 | 3.494 | 4.008 |
| MNG | 100 | 6.485 | 3.739 | 17.234 | 3.996 | 5.398 | 13.980 | 2.465 | 7.583 | 7.680 |
| MNP | 90 | 14.570 | 34.743 | 16.237 | 3.036 | 5.820 | 14.172 | 18.204 | 14.752 | 27.129 |
| MOZ | 100 | 1.106 | 1.969 | 25.506 | 2.168 | 6.526 | 2.600 | 4.111 | 4.351 | 5.551 |
| MRT | 100 | 1.607 | 2.153 | 25.672 | 2.375 | 3.683 | 6.462 | 3.555 | 22.420 | 55.648 |
| MSR | 91 | 12.816 | 6.478 | 7.597 | 13.560 | 13.323 | 22.545 | 15.698 | 5.460 | 22.357 |
| MTQ | 100 | 1.414 | 15.496 | 12.391 | 6.585 | 6.350 | 10.130 | 0.475 | 0.725 | 7.639 |
| MUS | 100 | 2.078 | 4.316 | 1.226 | 4.114 | 4.054 | 11.369 | 0.616 | 0.824 | 7.075 |
| MWI | 100 | 3.967 | 4.619 | 23.519 | 2.395 | 2.754 | 13.156 | 2.441 | 5.706 | 5.068 |
| MYS | 100 | 2.059 | 2.966 | 14.301 | 3.004 | 0.758 | 9.926 | 0.272 | 0.144 | 7.266 |
| MYT | 95 | 6.838 | 1.547 | 28.494 | 5.850 | 10.115 | 9.995 | 5.522 | 18.878 | 19.610 |
| NAM | 100 | 9.391 | 7.660 | 24.071 | 9.139 | 11.033 | 9.652 | 9.578 | 3.491 | 9.240 |
| NCL | 100 | 9.776 | 4.841 | 4.945 | 12.287 | 10.250 | 17.816 | 8.477 | 1.869 | 10.375 |
| NER | 96 | 3.193 | 3.346 | 29.343 | 2.713 | 5.235 | 7.784 | 0.816 | 2.594 | 5.463 |
| NGA | 100 | 4.237 | 4.046 | 20.786 | 4.665 | 0.184 | 9.963 | 0.173 | 0.546 | 2.164 |
| NIC | 100 | 0.065 | 5.040 | 12.314 | 1.014 | 0.336 | 5.047 | 2.279 | 1.258 | 12.552 |
| NLD | 100 | 2.967 | 4.996 | 5.985 | 1.563 | 1.713 | 2.452 | 4.449 | 2.307 | 4.058 |
| NOR | 100 | 4.050 | 5.116 | 7.969 | 4.641 | 3.477 | 14.468 | 3.493 | 3.424 | 6.593 |
| NPL | 100 | 1.533 | 9.788 | 7.239 | 2.563 | 2.733 | 9.697 | 6.579 | 0.907 | 6.357 |
| NZL | 100 | 7.938 | 8.863 | 14.074 | 6.451 | 6.923 | 0.468 | 11.448 | 3.594 | 8.331 |
| OMN | 100 | 26.371 | 10.442 | 24.164 | 57.267 | 22.185 | 1.322 | 35.361 | 49.122 | 39.666 |
| PAK | 100 | 7.342 | 10.602 | 14.749 | 6.793 | 3.415 | 11.557 | 5.054 | 1.930 | 9.102 |
| PAN | 100 | 0.363 | 0.945 | 14.397 | 2.304 | 0.200 | 5.434 | 1.234 | 2.381 | 4.181 |
| PER | 100 | 6.800 | 0.146 | 11.817 | 5.235 | 5.582 | 0.970 | 10.882 | 7.680 | 13.774 |
| PHL | 100 | 6.795 | 8.437 | 11.536 | 7.199 | 4.667 | 22.499 | 0.670 | 3.248 | 23.169 |
| PLW | 93 | 20.764 | 2.566 | 0.755 | 16.063 | 18.098 | 17.618 | 24.273 | 2.908 | 24.302 |
| PNG | 100 | 5.032 | 0.579 | 20.435 | 6.763 | 1.322 | 9.976 | 2.675 | 1.074 | 11.423 |
| POL | 100 | 2.665 | 3.245 | 1.440 | 0.122 | 0.135 | 1.051 | 2.391 | 4.871 | 4.230 |
| PRI | 100 | 0.793 | 12.040 | 11.943 | 4.364 | 3.956 | 20.736 | 1.232 | 3.936 | 12.989 |
| PRK | 100 | 1.072 | 0.942 | 4.195 | 0.234 | 0.086 | 1.114 | 0.506 | 7.913 | 4.053 |
| PRT | 100 | 4.028 | 3.002 | 0.538 | 0.814 | 0.828 | 5.959 | 2.503 | 7.014 | 5.349 |
| PRY | 100 | 1.344 | 3.723 | 12.676 | 1.278 | 2.285 | 0.571 | 2.549 | 2.291 | 4.086 |
| PSE | 100 | 2.946 | 8.587 | 20.548 | 0.973 | 2.600 | 8.293 | 0.266 | 2.491 | 4.431 |
| PYF | 100 | 0.604 | 10.620 | 2.466 | 1.972 | 1.826 | 16.459 | 2.931 | 6.273 | 13.609 |
| QAT | 100 | 11.835 | 27.645 | 33.617 | 41.973 | 8.364 | 13.173 | 10.964 | 47.238 | 32.262 |
| REU | 100 | 1.131 | 7.437 | 3.365 | 0.866 | 0.732 | 5.148 | 1.190 | 5.433 | 8.554 |
| ROU | 100 | 6.317 | 0.400 | 4.911 | 1.845 | 2.190 | 13.512 | 6.297 | 0.533 | 9.319 |
| RUS | 100 | 2.678 | 4.996 | 0.848 | 0.023 | 0.017 | 6.311 | 1.100 | 5.140 | 7.114 |
| RWA | 100 | 1.395 | 4.490 | 20.605 | 1.826 | 2.029 | 5.516 | 0.833 | 5.599 | 4.658 |
| SAU | 100 | 16.053 | 0.360 | 16.200 | 22.017 | 12.505 | 18.227 | 15.547 | 10.262 | 24.654 |
| SDN | 100 | 4.946 | 0.647 | 25.368 | 5.346 | 8.645 | 0.044 | 8.595 | 4.625 | 10.332 |
| SEN | 100 | 3.051 | 1.375 | 24.497 | 3.086 | 6.777 | 5.523 | 8.878 | 2.428 | 27.700 |
| SGP | 100 | 15.090 | 3.825 | 7.263 | 19.740 | 14.985 | 17.353 | 17.132 | 0.990 | 11.518 |
| SHN | 88 | 5.671 | 1.977 | 1.956 | 8.086 | 7.822 | 0.689 | 14.282 | 10.681 | 26.891 |
| SLB | 100 | 2.027 | 3.585 | 26.106 | 0.925 | 5.951 | 1.895 | 4.464 | 3.145 | 9.606 |
| SLE | 100 | 3.585 | 2.617 | 20.746 | 2.940 | 1.320 | 11.119 | 1.021 | 4.136 | 4.076 |
| SLV | 100 | 0.918 | 10.642 | 2.682 | 0.798 | 0.707 | 9.126 | 2.291 | 1.604 | 4.161 |
| SMR | 100 | 19.848 | 2.138 | 0.883 | 28.217 | 23.058 | 23.096 | 21.042 | 14.208 | 24.188 |
| SOM | 100 | 12.994 | 4.394 | 29.928 | 15.663 | 17.527 | 13.308 | 12.426 | 18.528 | 17.932 |
| SPM | 88 | 1.180 | 2.944 | 4.799 | 2.695 | 2.662 | 17.413 | 3.979 | 5.775 | 15.474 |
| SRB | 100 | 0.203 | 0.397 | 7.548 | 2.758 | 2.592 | 4.192 | 1.401 | 0.378 | 4.367 |
| SSD | 100 | 51.430 | 30.263 | 1.537 | 60.347 | 41.074 | 52.385 | 51.403 | 38.552 | 200.054 |
| STP | 94 | 3.949 | 12.021 | 16.293 | 3.874 | 0.835 | 9.374 | 1.063 | 7.121 | 5.721 |
| SUR | 98 | 0.594 | 2.725 | 9.616 | 1.442 | 0.521 | 6.508 | 1.243 | 3.726 | 5.286 |
| SVK | 100 | 3.471 | 3.242 | 1.963 | 0.825 | 0.835 | 7.737 | 2.231 | 4.258 | 6.269 |
| SVN | 100 | 2.871 | 4.725 | 2.944 | 0.274 | 0.196 | 1.110 | 3.288 | 2.862 | 4.220 |
| SWE | 100 | 1.975 | 7.772 | 9.179 | 1.654 | 2.092 | 2.362 | 2.921 | 2.783 | 4.342 |
| SWZ | 100 | 4.337 | 7.108 | 7.988 | 2.441 | 2.680 | 0.094 | 6.235 | 10.596 | 7.002 |
| SXM | 94 | 6.267 | 10.983 | 16.498 | 3.506 | 4.849 | 9.707 | 8.717 | 12.221 | 17.413 |
| SYC | 96 | 7.210 | 13.637 | 20.431 | 4.485 | 6.541 | 7.026 | 7.295 | 6.927 | 8.773 |
| SYR | 100 | 2.959 | 21.902 | 2.245 | 6.549 | 5.997 | 18.520 | 4.645 | 3.776 | 10.730 |
| TCA | 95 | 3.403 | 23.821 | 28.579 | 7.082 | 2.394 | 0.374 | 4.855 | 0.729 | 6.013 |
| TCD | 100 | 1.475 | 0.090 | 28.399 | 1.899 | 5.236 | 3.846 | 0.635 | 1.071 | 2.841 |
| TGO | 100 | 1.700 | 1.515 | 21.786 | 2.772 | 1.822 | 5.075 | 0.386 | 3.640 | 4.015 |
| THA | 100 | 1.223 | 0.037 | 2.882 | 3.814 | 3.486 | 9.585 | 0.667 | 4.927 | 6.163 |
| TJK | 100 | 2.453 | 1.181 | 21.125 | 1.590 | 4.603 | 1.145 | 3.202 | 3.309 | 5.648 |
| TKM | 100 | 1.972 | 3.253 | 19.533 | 0.956 | 3.657 | 6.024 | 0.375 | 4.330 | 8.215 |
| TLS | 96 | 7.116 | 7.522 | 16.692 | 5.560 | 1.908 | 19.811 | 1.114 | 17.663 | 11.919 |
| TON | 100 | 2.880 | 27.703 | 2.370 | 3.110 | 3.106 | 3.468 | 2.857 | 0.126 | 10.788 |
| TTO | 100 | 3.471 | 1.926 | 6.121 | 0.614 | 0.845 | 1.743 | 3.829 | 4.009 | 5.008 |
| TUN | 100 | 0.337 | 1.547 | 8.948 | 1.435 | 0.624 | 1.595 | 1.081 | 0.126 | 4.287 |
| TUR | 100 | 3.024 | 4.209 | 13.100 | 1.367 | 2.436 | 1.336 | 2.825 | 8.174 | 8.507 |
| TUV | 85 | 29.824 | 30.325 | 11.137 | 28.159 | 26.424 | 37.460 | 25.770 | 30.338 | 29.479 |
| TWN | 100 | 0.604 | 0.667 | 0.083 | 3.069 | 3.003 | 0.358 | 1.451 | 2.061 | 3.522 |
| TZA | 100 | 3.274 | 0.461 | 26.788 | 4.152 | 8.335 | 0.055 | 5.704 | 5.322 | 5.177 |
| UGA | 100 | 0.425 | 4.298 | 27.402 | 2.717 | 7.635 | 8.138 | 7.129 | 8.619 | 11.108 |
| UKR | 100 | 14.633 | 13.914 | 21.444 | 17.589 | 17.494 | 24.536 | 15.042 | 5.231 | 19.144 |
| URY | 100 | 0.030 | 2.640 | 1.437 | 1.060 | 1.014 | 8.163 | 1.895 | 1.730 | 4.974 |
| USA | 100 | 1.450 | 3.464 | 6.966 | 2.588 | 1.910 | 7.089 | 0.321 | 0.098 | 2.324 |
| UZB | 100 | 3.542 | 2.844 | 16.464 | 2.396 | 3.955 | 9.767 | 0.582 | 3.017 | 5.263 |
| VCT | 99 | 2.973 | 13.753 | 6.658 | 1.295 | 1.084 | 1.351 | 5.461 | 8.508 | 11.488 |
| VEN | 100 | 18.250 | 20.323 | 5.260 | 19.939 | 18.562 | 19.090 | 19.713 | 5.543 | 86.730 |
| VGB | 96 | 6.849 | 17.073 | 19.908 | 15.151 | 6.684 | 25.250 | 3.530 | 2.887 | 13.595 |
| VIR | 97 | 6.644 | 23.052 | 20.480 | 13.007 | 12.640 | 19.862 | 4.112 | 3.794 | 11.088 |
| VNM | 100 | 0.316 | 0.749 | 9.986 | 2.479 | 1.318 | 3.616 | 1.785 | 0.412 | 4.246 |
| VUT | 100 | 0.854 | 4.240 | 20.551 | 0.258 | 3.103 | 3.625 | 1.340 | 5.107 | 10.597 |
| WLF | 90 | 18.233 | 19.613 | 10.113 | 9.042 | 11.969 | 8.331 | 24.171 | 5.903 | 19.546 |
| WSM | 97 | 0.197 | 17.437 | 8.611 | 0.758 | 1.231 | 1.240 | 0.170 | 3.374 | 8.496 |
| XKX | 100 | 6.463 | 20.242 | 6.196 | 7.501 | 7.490 | 23.004 | 3.874 | 12.346 | 20.445 |
| YEM | 100 | 2.500 | 1.734 | 25.588 | 1.551 | 4.159 | 2.721 | 2.343 | 2.384 | 44.919 |
| ZAF | 100 | 3.374 | 4.977 | 13.632 | 1.546 | 2.682 | 5.979 | 6.480 | 8.942 | 9.687 |
| ZMB | 100 | 3.150 | 1.628 | 25.832 | 4.071 | 2.720 | 9.379 | 1.317 | 3.909 | 6.160 |
| ZWE | 100 | 1.551 | 7.623 | 14.123 | 0.298 | 1.297 | 2.205 | 3.958 | 8.105 | 6.366 |
