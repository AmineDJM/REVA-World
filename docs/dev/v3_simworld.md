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
| Tests | `tests/unit/test_simworld_connectors.py` (7), `tests/unit/test_simworld_models.py` (13), `tests/integration/test_simworld_pipeline.py` (5, DB), `tests/integration/test_simworld_live.py` (`live`); fixtures `tests/fixtures/source/un.wpp/`, `tests/fixtures/source/ilostat.labour_age5/`, helper `tests/fixtures/simworld/simworld_fixtures.py` |

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

BACKTEST_SUMMARY

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

LIVE_SECTION

## Coverage summary (which countries each model runs for)

COVERAGE_SECTION

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

BACKTEST_TABLE
