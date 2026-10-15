# V2 step 3a — cohort-component population projection (agent POP)

Status: implemented 2026-10-15 as an **experimental model** (`approval_status: candidate`,
`review_status: maintainer_review_pending`; runs are labelled "experimental model, not yet reviewed").
Every output is a **conditional projection under stated assumptions, not a forecast**.

Back-test results: [`v2_pop_backtest.md`](v2_pop_backtest.md) (stored evaluation:
[`v2_pop_backtest_evaluation.json`](v2_pop_backtest_evaluation.json)).

## What exists

| Piece | Where |
|---|---|
| Reviewed built-in connector `eurostat.demography@1.0.0` | `src/reva/adapters/connectors/eurostat_demography.py` (registered in `registry.builtin_connectors`) |
| Pure model (numerics, back-test, EUROPOP comparison) | `src/reva/models/population/cohort.py` |
| Simulation plugin `demography.cohort_component@1.0.0` | `src/reva/models/population/cohort_component.py` (module listed in `plugins.KNOWN_MODEL_MODULES`) |
| Stored back-test evaluation (DB side) | `src/reva/evaluation/population_backtest.py` |
| Registry | `registry/sources/eurostat_demography.yaml`, `registry/concepts/demography_v2.yaml`, `registry/mappings/eurostat_demography.yaml`, `registry/models/methods.yaml` (model entry), `registry/policies/budgets.yaml` (`eurostat.demography: 300`, budgets-1.2.0, re-pinned in `src/reva/autonomy/policy.py` with a history line) |
| Tests | `tests/unit/test_eurostat_demography_connector.py`, `tests/unit/test_population_cohort.py`, `tests/property/test_population_cohort_properties.py`, `tests/integration/test_demography_cohort.py` (DB, offline fixtures), `tests/integration/test_demography_live.py` (`live`) |
| Fixtures | `tests/fixtures/source/eurostat.demography/` (LI + LU since 2008 and the three EUROPOP2023 tables, recorded live 2026-10-15 with `RecordingHttp`, ~530 kB) |
| Capability | `demography.cohort_component` → implemented in `src/reva/capabilities.py` (tests and this doc cited) |

## Stable API for agent PEN

```python
from reva.models.population import cohort as C

data = C.CountryDemography.from_tables(
    "FRA", population=pjan_table, deaths=magec_table, births=fasec_table,
    europop_migration=None, europop_fertility=None)        # Arrow tables / Polars frames / row dicts
assumptions = C.Assumptions(migration="average_residual",   # "zero" | "average_residual" | "europop2023_bsl"
                            fertility="constant_base_period_rates",  # | "europop2023_bsl"
                            base_period=(2019, 2023),        # calendar years of events, must end < jump_off
                            migration_window=None)           # default: the base period
proj = C.project(data, jump_off=2025, end_year=2041, assumptions=assumptions)
proj.population[2030]["F"]      # tuple[Decimal] for ages 0..proj.omega (index omega = open group omega+)
proj.total(2030, "M", 55, 70)   # persons aged 55..69 on 1 January 2030 (None = both sexes)
proj.flows[2030]                # births / deaths / net_migration by sex during 2030
C.output_values(proj)           # {(output_key, year): Decimal} unrounded
proj.single_age_table()         # pyarrow table: iso3, year, sex, age, open_group, persons (exact text), variant
proj.summary()                  # label, assumptions, TFR, e0, data notes, status flags, negative-cohort flags
```

* Inputs are the dataset-input tables of schema `reva-eurostat-demography-table/1` (one per
  (dataset, country); columns `dataset, geo, iso3, projection, year, sex, age, age_kind, age_start,
  age_end, cell_type, value (exact decimal text), missing_reason, status, unit`). Through the platform
  they arrive as `DatasetInput.table` for concepts `demography.population.jan1_age_sex`,
  `demography.deaths.age_sex`, `demography.live_births.mother_age_newborn_sex` (and the EUROPOP2023
  assumption concepts), subject = ISO3.
* Population is on **1 January**; sexes `"F"`, `"M"` (recorded sex); ages in completed years;
  `proj.omega` is between 85 and 100 depending on the data (never split). Values are `Decimal`
  person counts exact to 1e-9 (the model quantum); round only for presentation.
* Errors are typed: `C.DataGap` (a `MissingInput`) for an incomplete required table, `CapabilityGap`
  for unsupported data (open age < 85, EUROPOP not available for a year/country),
  `ValidationProblem` for a base period that does not end before the jump-off.
* PEN already uses this API (`src/reva/models/social/retirement_age.py`); it is stable for 1.x.

## Model (summary; full method in the `cohort.py` docstring)

* Annual steps 1 January → 1 January, single ages 0..omega−1 + open group omega+, by sex.
* Mortality: constant base-period rates `m_x = ΣD_x / ΣE_x`, `E_x = (P_x(t)+P_x(t+1))/2`;
  `q_x = m_x / (1 + (1 − a_x) m_x)` with `a_x = 1/2` (x ≥ 1) and Coale–Demeny `a_0`
  (Preston, Heuveline & Guillot 2001, table 3.3); life table with `l_0 = 1`, `L_x = l_{x+1} + a_x d_x`,
  `L_omega = l_omega / m_omega`; survival `S_b = L_0`, `S_x = L_{x+1}/L_x`, open `L_w/(L_{w−1}+L_w)`.
* Fertility: base-period age-specific rates on mean female exposure (mother's age in completed
  years; `Y10-14`/`Y_LT15` → exposure 10–14, `Y_GE50` → 50–54; unknown mother's age as a separate rate
  per woman 15–49, never spread); or the EUROPOP2023 BSL rates. Sex at birth: male share of births
  in the base period (data).
* Net migration (negative allowed): zero; mean of residual estimates
  `M_{x+1}(t) = P_{x+1}(t+1) − S_x P_x(t)` (age 0: `P_0(t+1) − S_b B_s(t)`) over a stated window; or
  EUROPOP2023 BSL by age and sex. Migrants are added at the end of the year.
* Numeric type: `decimal.Decimal`, precision 50, `ROUND_HALF_EVEN`; person counts quantized to 1e-9;
  accounting identities computed with `Inexact` trapped, so `P(t+1) = P(t) − D + B + M` holds
  **exactly** (asserted each step; property-tested). Exact rationals were rejected: denominators of
  survival products grow without bound over 25+ steps.
* Negative cohorts (possible only when negative net migration exceeds the survivors, or inherited)
  are kept (clipping would break the balancing equation) and flagged per (year, sex, age) with
  their cause.

## Plugin (`demography.cohort_component@1.0.0`)

Spec branch (ScenarioSpec 2.0.0): `reference_year` = jump-off; `horizon {periods ≤ 50, unit
calendar_year}`; `intervention {"kind": "baseline_projection"}`; `assumptions {mortality, fertility,
migration, base_period, migration_window?, sensitivity?}`; outputs from
`population.{total|female|male}.{all_ages|age_0_14|age_15_64|age_65_plus|age_80_plus|age_20_66|age_67_plus}`,
`old_age_dependency_ratio_65plus_per_15_64`, `old_age_dependency_ratio_67plus_per_20_66` (percent, both
always clearly named), `{births|deaths|net_migration}.{total|female|male}`, and the opt-in
`population_by_single_age` (rows `population.female.age_042`, …, `…age_100_plus`).

* Rows are multi-period: stocks carry `period = [YYYY-01-01, YYYY-01-02)` (the 1 January instant),
  flows `[YYYY-01-01, YYYY+1-01-01)`. Values are rounded to whole persons (ratios to 1e-4) at
  presentation; the single-age rows keep the model quantum.
* L1: one projection (variant `L1`). L2: L1 + every (migration × fertility) grid point
  (`L2:mig=…;fert=…`) + a separate `L2:sensitivity_range` row per output/year (min/max over the grid
  and L1, uncertainty kind `sensitivity_range`, **no level**). Mortality is constant in every variant.
* Missing inputs: per country `missing_input` (policy `fail`) or skipped (`skip_missing_countries`,
  listed in the run applicability); EUROPOP data missing for a grid variant only (e.g. Liechtenstein,
  not projected by Eurostat) makes that variant *unavailable* (reported), never zero-filled.
* Run applicability: per country open age, implied TFR and e0 of the base period, male share at
  birth, data notes, Eurostat status flags of every input year.

## Connector `eurostat.demography@1.0.0`

* Source key `eurostat.demography` (lineage family `eurostat`, same as `eurostat.dissemination`:
  never independent evidence of each other). Host `ec.europa.eu` only. Countries: EU-27 + EFTA
  (31; Eurostat geo → ISO3 is a reviewed table in the module, `EL → GRC`). Subjects resolve through
  the `iso3166_alpha3` aliases: no `eurostat_geo` aliases were added, so the autonomy template's T1
  eligibility scope is not widened.
* Requests: demo_pjan, demo_magec, demo_fasec per country in **9-year time windows** (2000–2008,
  2009–2017, 2018–latest; `sinceTimePeriod`/`untilTimePeriod`), plus proj_23nanmig, proj_23naasfr and
  proj_23np (BSL) for all countries in one request each: 282 requests per full cycle; the plan is
  resumable (cursor `done` list) so the ingest job's 100-request default completes a cycle in 3 runs.
  Why windows: `publish_acquisition` takes one advisory lock per observation; a 26-year single-age
  cube (≈8 000 cells) exceeded PostgreSQL's default lock table (`max_locks_per_transaction = 64`)
  live, while a window publishes ≤ ≈2 800 observations. (`window_years=None` keeps one request per
  table; the built-in instance uses windows.)
* Observations: every cell of the three observed tables (concepts above; logical key
  `estat-demo:{dataset}:{geo}:{sex}:{age}:{year}`; support resolution `{age, sex}`; `cell_type`
  leaf/subtotal/total; status flags and labels verbatim; absent → typed Missing; `|N` (not for
  publication) → Missing `suppressed`; unknown flag, unit/dimension drift, negative counts → blocked).
  EUROPOP2023 tables are **never observations**: projections enter only as labelled assumption
  datasets.
* Dataset inputs: when the last time window of a (dataset, country) is published, the post-publish
  hook `post_publish_datasets` assembles the table from the archived bytes of all windows and calls
  `reva.simulation.datasets.register_dataset` (dataset key `eurostat.demography.<dataset>`, all
  acquisition ids, parser `eurostat-demography-1`, coverage). `backfill_datasets(conn, store, origin=)`
  does the same from archived acquisitions for runners that do not dispatch the hook.
* Eligibility: exact mappings, eligible for the 31 subjects; **no selection preference** is listed
  for this source in `registry/policies/selection.yaml`, so the ≈6×10⁵ single-age cells are evidence
  but not selected into beliefs (the model reads pinned dataset inputs).

## Data coverage (Eurostat, fetched 2026-10-15)

| Country (geo) | demo_pjan years | demo_magec years | demo_fasec years (complete mother's-age table) | Y_OPEN starts seen (pjan) | status flags seen | EUROPOP2023 |
|---|---|---|---|---|---|---|
| AUT (AT) | 2000–2025 | 2000–2024 | 2007–2024 | 95, 100 | – | yes |
| BEL (BE) | 2000–2025 | 2000–2024 | 2007–2024 | 100 | b | yes |
| BGR (BG) | 2000–2025 | 2000–2024 | 2007–2024 | 90, 99, 100 | – | yes |
| CHE (CH) | 2000–2025 | 2000–2024 | 2007–2024 | 99, 100 | b | yes |
| CYP (CY) | 2000–2025 | 2000–2024 | 2007–2024 | 99, 100 | – | yes |
| CZE (CZ) | 2000–2025 | 2000–2024 | 2007–2024 | 100 | b | yes |
| DEU (DE) | 2000–2025 | 2000–2024 | 2007–2024 (missing 2022) | 90, 95, 99, 100 | b |N | yes |
| DNK (DK) | 2000–2025 | 2000–2024 | 2007–2024 | 100 | – | yes |
| ESP (ES) | 2000–2025 | 2000–2024 | 2007–2024 | 100 | – | yes |
| EST (EE) | 2000–2025 | 2000–2024 | 2007–2024 | 100 | – | yes |
| FIN (FI) | 2000–2025 | 2000–2024 | 2007–2024 | 100 | – | yes |
| FRA (FR) | 2000–2025 | 2000–2024 | 2007–2024 | 99, 100 | b p | yes |
| GRC (EL) | 2000–2025 | 2000–2024 | 2007–2024 | 100 | b | yes |
| HRV (HR) | 2000–2025 | 2000–2024 | 2007–2024 | 85, 100 | b e | yes |
| HUN (HU) | 2000–2025 | 2000–2024 | 2007–2024 | 85, 90, 100 | b | yes |
| IRL (IE) | 2000–2025 | 2000–2024 | 2007–2024 | 99, 100 | – | yes |
| ISL (IS) | 2000–2025 | 2000–2024 | 2007–2024 | 100 | – | yes |
| ITA (IT) | 2000–2025 | 2000–2024 | 2007–2024 | 100 | b | yes |
| LIE (LI) | 2000–2025 | 2000–2024 | 2007–2024 | 100 | – | no (not projected) |
| LTU (LT) | 2000–2025 | 2000–2024 | 2007–2021 | 100 | |N | yes |
| LUX (LU) | 2000–2025 | 2000–2024 | 2007–2024 | 95, 100 | b | yes |
| LVA (LV) | 2000–2025 | 2000–2024 | 2007–2024 | 100 | – | yes |
| MLT (MT) | 2000–2025 | 2000–2024 | 2007–2024 | 90, 99, 100 | – | yes |
| NLD (NL) | 2000–2025 | 2000–2024 | 2007–2024 | 100 | – | yes |
| NOR (NO) | 2000–2025 | 2000–2024 | 2007–2024 | 100 | – | yes |
| POL (PL) | 2000–2025 | 2000–2024 | 2007–2024 | 95, 100 | b bep ep | yes |
| PRT (PT) | 2000–2025 | 2000–2024 | 2007–2024 | 100 | – | yes |
| ROU (RO) | 2000–2025 | 2000–2024 | 2007–2024 | 100 | e | yes |
| SVK (SK) | 2000–2025 | 2000–2024 | 2007–2024 | 99, 100 | – | yes |
| SVN (SI) | 2000–2025 | 2000–2024 | 2007–2024 | 100 | b | yes |
| SWE (SE) | 2000–2025 | 2000–2024 | 2007–2024 | 100 | – | yes |

`|N` = not for publication (DEU births 2024 totals by sex, LTU 2022–2024 mother's-age classes).
Every run and back-test reports `base_period_years_used` per component (mortality, fertility, sex
ratio at birth) and `excluded_never_filled` (e.g. DEU with base period 2019–2023: fertility from
2019–2021 and 2023, 2022 excluded). A year without births by newborn sex cannot enter the
migration-residual window (typed `missing_input`), e.g. DEU 2024 until Eurostat publishes it. Germany's
2022 births by mother's age are not published by single age; Lithuania's 10–14 and 50+ classes are
missing for 2022–2024: those years are left out of fertility-rate estimation (reported in
`data_notes`), never imputed. Liechtenstein has no EUROPOP2023 projection. Unknown-age population and
deaths (`UNK`) are excluded (absorbed by the migration residual) and reported per year.

## Decisions (owner asleep; conservative)

1. **Back-test base period ends before the jump-off** (2008–2012 → 2013); the literal "2009–2013
   rates from 2013" design would leak 2013 events and the 2014 population, so it is reported with a
   1 January 2014 jump-off instead (both in `v2_pop_backtest.md`).
2. **Residual migration uses base-period survival** (not single-year life tables), so replaying the
   base period with its own residuals reproduces the observed populations exactly (tested). It is
   labelled "net migration plus statistical adjustment"; census breaks show up in it and are not
   corrected by hand (visible in the back-test).
3. **Mortality improvement is not modelled** in 1.0.0 (`proj_23naasmr` was not used: whether its
   values are rates or probabilities was not verified). Hence the EUROPOP2023 comparison is a
   plausibility check only, and REVA's 2050 old-age dependency ratios are lower than EUROPOP's.
4. **EUROPOP2023 ages**: net migration at age x is added to age x on the following 1 January and
   EUROPOP fertility rates are applied to the same exposure as observed rates (stated assumptions).
5. **Open age**: min over every table used, ≤ 100, ≥ 85; lowered (never below 85) for zero
   exposure, an open group without deaths, or m_x ≥ 2 (small numbers, e.g. Liechtenstein);
   never split.
6. **Unknown-age births** are a separate rate per woman 15–49 (needed so the projected births
   match total births); unknown-age population/deaths are excluded and reported.
7. **No eurostat_geo aliases** for the 30 new countries (would widen autonomy T1 scope); ISO3
   aliases with a reviewed geo table instead.
8. **Experimental** registration (candidate + `maintainer_review_pending`), as decided in the roadmap.

## Live run

`REVA_POP_LIVE_COUNTRIES=FR,DE,IT,ES,PL uv run pytest -m live tests/integration/test_demography_live.py`
(2026-10-15, 9 min 11 s, passed), in a throwaway database bootstrapped like a deployment:

1. Live ingest of `eurostat.demography` for FRA, DEU, ITA, ESP, POL (+ the three EUROPOP2023 tables):
   48 requests reserved and metered through the budget ledger (`budgets-1.2.0`, 48 consumed,
   2 334 036 bytes, 0 refused), 48 acquisitions published, 91 215 observations, dataset inputs
   registered by the post-publish hook (observed tables assembled from their three time windows).
2. Belief release `9f793a8b…` (live origin) pins the dataset inputs by digest.
3. Scenario (L2, jump-off 1 January 2025, horizon 25 years, base period 2019–2023, grid migration
   {zero, average residual, EUROPOP2023} × fertility {constant, EUROPOP2023}) created through
   `sim.create_scenario` and run through the worker job path `handle_simulation`: status `succeeded`,
   output digest `59c9c8df…86f5`, run outcome labelled "experimental model, not yet reviewed".
4. `run_population_evaluation` stored the back-test evaluation (manifest `1878cd06…744b`,
   `model.evaluation` rows, `revised_data_retrospective`); its numbers for these five countries are
   **identical** to `v2_pop_backtest_evaluation.json` (checked field by field).

Selected outputs (conditional projections under stated assumptions, not forecasts; mortality
constant at 2019–2023 rates, which include COVID-19 excess mortality):

| Country | total 2025 (observed jump-off) | total 2050 L1 (avg. residual mig., constant fertility) | 2050 L2 range over the grid | 65+/15-64 2025 | 65+/15-64 2050 L1 | 2050 L2 range |
|---|---|---|---|---|---|---|
| DEU | 83577140 | 82017671 | 70561807–82794754 | 35.891 | 39.1731 | 38.9829–48.3689 |
| ESP | 49128297 | 54919175 | 41913161–55721268 | 31.2174 | 47.4222 | 47.1602–63.4552 |
| FRA | 68882600 | 71583998 | 65938992–71834774 | 35.523 | 42.4375 | 42.3282–45.3077 |
| ITA | 58943464 | 50612773 | 47240151–55246876 | 39.0425 | 60.3901 | 55.0062–65.2008 |
| POL | 36497495 | 25773187 | 25773187–30430194 | 32.7056 | 50.8282 | 44.0641–50.8282 |

The L2 range is assumption sensitivity, not a confidence interval (no probability level).

## Requested changes in files owned by others (not edited)

* Done by others since: the worker ingest handler and the CLI now call
  `getattr(connector, "post_publish", None) or post_publish_for(spec.template)`; this connector exposes
  `EurostatDemographyConnector.post_publish` (= `post_publish_datasets`), so scheduled and CLI ingests
  register dataset inputs. `backfill_datasets(conn, store, origin=)` remains for archived acquisitions.
* `src/reva/cli/seed.py` now schedules `ingest:eurostat.demography` every 30 days. A full cycle is 282
  requests and the ingest job plans at most 100 per run (`FetchBudget()` default), so one cycle takes
  three scheduled runs (the cursor resumes); a daily schedule (budget 300/day) would complete a cycle
  in three days. Datasets of a country are registered as soon as all its windows are in.
* `src/reva/evidence/ingest.py` (lead): one advisory lock per observation exhausts PostgreSQL's
  default lock table for large tables (seen live: another agent's test held 7 588 advisory locks in
  one transaction while my 8 000-cell acquisition failed with `out of shared memory`). Either lock
  per acquisition/lineage batch or raise `max_locks_per_transaction` in the deployment (e.g. 1024).
* `registry/policies/selection.yaml` (PUBFIX): add `demography.deaths.age_sex` /
  `demography.live_births.mother_age_newborn_sex` / `eurostat.demography` preferences only if
  single-age beliefs are wanted in releases (≈6×10⁵ estimates per release).
* `contracts/scenario.schema.json` was regenerated with ENG's generator
  (`python -m reva.simulation.scenario_schema`) to include this plugin's branch (it also picked up the
  `social.retirement_age` branch registered by PEN).
