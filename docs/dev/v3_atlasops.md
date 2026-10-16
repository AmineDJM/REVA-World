# V3 ATLASOPS — atlas integration, cross-source fallback, operations, dev-DB load

Agent ATLASOPS took over atlas integration from ATLASCORE on 2026-10-15. The interface doc for connector
agents is `docs/dev/v3_atlas_interface.md`; the ATLASCORE design is in `docs/dev/v3_atlascore.md`.

## 1. Connectors migrated to the atlas source platform

A-IMF's `atlas_imf.py` and A-QOL's `atlas_qol_common.py`, `atlas_undp.py`, `atlas_whr.py`, `atlas_owid.py`
and `atlas_wid.py` now declare `atlas_source()` (an `AtlasSourceSpec`) and `async atlas_ingest(run)`. They
fetch only through the run (`run.get`, `run.get_range`), archive with `run.archive` and register with
`run.register`. No module builds an HTTP client. The placeholder `ConnectorSpec` / `model_copy` workaround
and the old `register_connector` helpers are gone.

| connector | version | budget key | planned requests / run | notes |
|---|---|---|---|---|
| `atlas.imf` | 1.2.0 (parser `atlas_imf/2`) | `imf.sdmx` (60/day) | 30 | fixture mode replays the captured subset (10 economies, 2 IMTS reporters) |
| `atlas.undp_hdr` | 1.1.0 | `undp.hdr` | 3 | |
| `atlas.whr` | 1.1.0 | `whr.figure21` | 1 | |
| `atlas.owid` | 1.1.0 | `owid.compilations` | 24 | |
| `atlas.wid` | 1.1.0 | `wid.bulk` (500/day) | 470 | byte ranges; run option `countries=FR,DE,...` for a subset |

Platform additions in `src/reva/atlas/ingest.py`:

* `run.get_range(url, key=, start=, end= | suffix=, if_range=)`: one HTTP byte range. It is metered like
  `get` (one request, the bytes served). Only `206` is accepted. The fingerprint includes the range, and
  `content-range` is kept in the acquisition metadata.
* `ArchiveRangeFixtureHttp`: the fixture client for byte-range sources. The manifest is an object with
  `"kind": "byte_range_archive"` (WID).
* `run.options`: CLI `--option KEY=VALUE`, job payload `options`.
* `AtlasRun.offline(spec, http)`: a fetch-only run for parser tests.
* `run.post(url, key=, json_body= | content=, ...)` (GAPHUNT request: PxWeb of Greenland, Faroe, Kosovo ASK,
  Åland and Andorra; IRENA; UNECE are POST-only).
  * Transport: `reva.atlas.http_post.safe_post` sends the POST through the run's SafeHttpClient. That gives
    the same allow-list, DNS pinning, politeness, bounded decoding and retries as GET. Redirects are not
    followed, and no new client is created.
  * Metering: the POST is charged to the run's MeteredHttp reservation exactly like a GET (one request;
    request and response bytes count). It is refused before the transport when the budget is spent.
  * Provenance: the request body (≤ 64 KiB) is archived in `response_metadata.request`, and the
    fingerprint covers it.
  * Fixture mode replays a FixtureHttp entry keyed by that fingerprint.
  * A live run never POSTs outside the metered client.
  * `safe_client.py` and `operations/budgets.py` were not modified: they are pinned/T3 files.
* **Code-identity check.** A run is refused before any request if its connector version is not
  registered, or is registered with a different `code_digest` ("bump the version"). Acquisitions therefore
  always name the code that produced them.
* The seed registers every discovered atlas source. A source already registered with different content
  is skipped and logged, never fatal. The seed fingerprint includes the atlas sources (name, version), so
  a new source or version is registered by the next `reva seed` / `reva db bootstrap`.

Versions bumped because the code changed:

* IMF 1.2.0, QOL 1.1.0, ILO 1.0.1 (docstring only), HYDE 1.1.0.
* After TWNMAP and other agents' late edits, patch bumps to 1.0.1 so the dev-DB registration matches the
  code: WHR 1.1.1, constitute, regimes (LIED/BMR), vdem, fao, unctad, oecd.batis, RCS/WRP, worldbank,
  geonames, cckp, baci, oecd.fiscal, oecd.skills.
* A-UN had already bumped to 1.1.0.

## 2. CLI, jobs, schedules, migration

* `reva atlas list | ingest <source> [--dataset D] [--fixture] [--option K=V] | snapshot [--origin]
  [--warm] [--skip-unchanged]`.
* Job kinds `atlas_ingest` → `reva.atlas.ingest:handle_atlas_ingest` and `atlas_snapshot` →
  `reva.atlas.snapshot:handle_atlas_snapshot`, both in `HANDLERS` and both schedulable
  (`SCHEDULE_JOB_TYPES`).
* Migration `0010_atlas_ops.py`: `CREATE OR REPLACE` of `ops.submit_job` with the two kinds added to the
  allow-list. The body is otherwise identical to 0006; downgrade restores 0006. The schema contract was
  regenerated (head `0010_atlas_ops`).
* Seed schedules (`src/reva/cli/seed.py`):
  * Every finished atlas source runs monthly, 39 sources in total (`ATLAS_SCHEDULE_SLOTS`), including
    the 5 GAPHUNT gap-fill sources on day 12.
  * First slots are staggered over 12 days (`ATLAS_FIRST_SLOT_HOURS`). World Bank, WID and IMF each get a
    day of their own.
  * `atlas_snapshot:live` runs daily with `skip_unchanged` (no new table versions → nothing recorded) and
    `warm` (builds the query file).
  * A test checks that every scheduled source is discovered and that its planned requests fit its
    per-source budget.
* Budgets: `budget_merge --write`, re-pinned in `src/reva/autonomy/policy.py` with history notes. The
  current version is `budgets-1.3.6`, which adds the late requests of A-SOCIETY, A-RESOURCES, A-MILITARY,
  A-FISCAL, KGLANG, A-CULTURE and GAPHUNT. The earlier merge to `budgets-1.3.4` added:
  * `factbook.json`, `fao.faostat`, `unsd.ama`, `worldbank.atlas`
  * `ssa.ssptw` 220/day
  * `cshapes.cran`, `harvard.dataverse.unvotes`, `ucdp.downloads`
  * Invariant 30 passes.

## 3. Cross-source fallback: REVA headline concepts

`registry/atlas/source_order/<theme>.yaml` (schema `_schema.json`, loader `src/reva/atlas/source_order.py`)
declares **REVA headline concepts** (`reva.<theme>.<name>`). Each concept lists, in order, the provider
series that define the same quantity the same way, each with a written justification and caveats. Series
that look similar but are defined differently are listed under `not_equivalent` with the reason, and they
never answer. Examples:

* WEO population: national definitions.
* WHO life expectancy: a different mortality model.
* WEO unemployment: national definitions, sometimes registered unemployment.
* UNSD surface area: whole thousands of km2.
* WID Gini: a different distribution.
* OECD minimum wage: an annual average.

Optional `scale` + `reported_unit` give an exact unit factor. For example, WPP thousands → persons is
multiplied by 1000 in `Decimal`. A cell with another unit is skipped and reported `unit_mismatch`.

| theme | concepts |
|---|---|
| population | `reva.population.total` (WPP → WDI), `reva.population.density` (WDI → derived WDI pop / WDI land) |
| geography | `land_area` (WDI/FAO), `surface_area` (WDI/FAO) |
| economy | `gdp_usd`, `gdp_per_capita_usd`, `gdp_per_capita_ppp`, `gdp_real_growth` (WDI → WEO), `inflation_cpi` (WEO → WDI) |
| labour | `unemployment_rate` (ILO modelled → WDI modelled), `unemployment_rate_national` (ILO national → WDI national), `minimum_wage_monthly_lcu` (ILOSTAT → Eurostat), `average_monthly_earnings_lcu` (ILOSTAT tables) |
| health | `life_expectancy` (WPP → WDI → UNDP) |
| education | `literacy_rate_adult` (UIS → WDI) |
| environment | `co2_per_capita` (OWID/GCB → UNDP/GCP) |
| finance | `government_net_lending_gdp` (GFS S13 → WEO) |
| trade | `current_account_gdp` (WEO → WDI), `goods_balance_usd` (BOP → WDI, then derived credit − debit of one source), `goods_services_balance_usd`, `merchandise_balance_imts` (derived IMTS FOB − CIF; not the BOP balance) |
| society | `hdi` (UNDP), `gini_income` (WDI/PIP) |
| science | `internet_users` (SDG/ITU → WDI) |
| law | `homicide_rate` (SDG/UNODC → WDI) |

GAPHUNT proposals (`_proposals_gaphunt.yaml`) were reviewed and all merged:

* 10 gap-fill series were added after the multilateral ones:
  * INSEE overseas-departments CPI
  * CBS Caribbean Netherlands GDP, GDP per capita and real growth
  * SPC GDP and GDP per capita in USD
  * SPC and Eurostat internet use
  * Eurostat NUTS land and total area
* 8 `not_equivalent` exclusions were added.
* 2 new concepts were added: `reva.population.total_1_january` and
  `reva.health.life_expectancy_national_tables`.

Each entry keeps GAPHUNT's justification. The registry now has 27 headline concepts.

### How a value is chosen

* **Which series answers.** For each country and year, the first listed series that reports a value
  answers. Cells are eligible only if they are numeric, have the declared unit, and are not projections.
  A cell counts as a projection when it has `estimate_kind = projection`, a truthy `projection` flag, or
  `period_after_vintage`; the exception is a concept with `projections: allow`.
* **What every answer carries.**
  * `answered_by`: series, rank and justification.
  * `reported`: the value and unit exactly as reported, and the scale.
  * The cell's provenance and its true period.
  * `consulted`: every listed series in order, with a status:
    * `answered`
    * `also_reported`
    * `no_value`
    * `projection_only`
    * `unit_mismatch`
    * `series_not_in_snapshot`

  Countries without a value are listed with the same `consulted` statuses, so the answer shows where REVA
  looked.
* **Derived values.** A `ratio` or `difference` of two inputs from the same country and calendar year.
  * It is used only when no listed series reports the cell.
  * Both inputs come from ONE provider: `same_source`, the default and enforced at load time (review V3
    ATLAS-6). A concept pair such as population / land area expands into same-provider series pairs.
  * The answer cites both input cells.
  * Its flags carry both inputs' flags, with any estimate or projection flag lifted to the top.
  * Ratios are rounded once to 12 significant digits; differences are exact.
* **Units and status.** The headline unit is the concept unit, except for "named per cell" concepts such
  as currencies, which keep the cell's own unit. The equivalences are labelled
  `agent_declared_equivalence_not_human_reviewed`.

API (read scope): `GET /v1/atlas/headline/concepts`, `/headline/map?concept&year`,
`/headline/series?concept&iso3`, `/headline/coverage?theme`. The country dossier
`/v1/atlas/countries/{iso3}` has a `headline` section (latest value per concept, who answered, who was
consulted, concepts without data with their consulted statuses).

## 4. Themes

`registry/atlas/themes.yaml` assigns every catalog concept to one REVA theme. There are 16 themes: the 15
requested plus `society` for quality of life, well-being, poverty, inequality and gender, which none of
the 15 fits.

The theme of a concept is decided by the first of these that applies:

1. The concept's own optional `theme:` field (catalog schema).
2. A rule matching its domain, subdomain or id prefix. The `imf` and `un` domains are split by subdomain:
   IMF external/trade → trade, fiscal/monetary → finance, national accounts/prices → economy; UN area →
   geography, mortality → health.
3. For multi-topic catalogs (factbook, food_agriculture, unsd_ama, water), the subdomain itself or an
   alias of it.
4. The domain default.

All 8,581 catalog concepts resolve to a theme. A test fails if one does not; at runtime an unresolved
concept is listed in `Catalog.unthemed`, never fatal. `/v1/atlas/concepts`, `/countries/{iso3}` and
`/coverage` accept `?theme=`. Catalog changes:

* No catalog file of a running agent was edited.
* `labour.yaml` (A-ILO, finished) changed for review DATA-1: the OECD annual-average minimum wage became
  its own concept.
* `labels.fr` is now optional, with an explicit `fr_label: missing` marker, so English text is never
  copied into the French label.
* Big catalog files are parsed with `CSafeLoader` and cached as JSON in the private cache directory:
  strict load ~15 s cold → ~2 s warm.

## 5. BCE periods (lead request, A-HISTECON)

Atlas periods use the proleptic Gregorian calendar with astronomical years: year 0 = 1 BCE and -9999 =
10000 BCE, the same convention as the KG `KgTime`. Arrow `date32`, Parquet, Polars and DuckDB store such
dates natively, so the schema did not change. Only Python `date` cannot hold them.

* `tables.py` has `frame_with_year_periods`, `year_period_exprs` and `period_text`. `year_period(y < 1)`
  raises and points to these helpers. Coverage and refusal examples are BCE-safe.
* The query layer returns periods as ISO text (`-9999-01-01`).
* HYDE (`atlas_hyde.py` 1.1.0, parser `atlas_hyde/2`) now loads its 11 BCE time steps. Each pre-CE row
  carries `flags.year_numbering`, which states that the HYDE numeric step is used as the astronomical
  year.

## 6. Review V3 fixes (docs/dev/review_v3.md)

* **ATLAS-2: the year view now includes non-calendar cells.**
  * Cells spanning 150-1097 days are attached to a year: semesters, fiscal years, survey windows and WHR
    3-year windows.
  * Point-in-time cells (1-day "as of" periods: GeoNames, Natural Earth, Factbook, WBL, Constitute) are
    attached too. The dev-DB load showed these concepts were otherwise invisible.
  * The year is the provider's `flags.label_year` when given, else the year of the cell's last day. One
    cell per series, country and year, with calendar cells preferred, then the latest one.
  * Every answer shows its true period and `period_basis` (`calendar_year`, `point_in_time`,
    `sub_annual`, `twelve_month_window`, `multi_year`).
  * Maps list countries whose cells overlap the year but are not attached (monthly, quarterly, long
    windows) under `countries_with_other_period_cells`, never as absent.
  * DuckDB format is now `atlas-duckdb/6`.
* **ATLAS-1: the query cache is private and verified.**
  * The cache directory is per user and 0700 (`reva-atlas-cache-<uid>`). Group- or other-writable, or
    foreign-owned, directories are refused.
  * The lock is opened with `O_NOFOLLOW`.
  * Each query file has a sidecar `.sha256` (format, snapshot, file digest) that is verified on every
    open, together with `meta`. A mismatch removes both files and raises `IntegrityFailure`; the next read
    rebuilds from the verified Parquet.
* **ATLAS-3:** dossier series points carry `unit`, `period_start`, `period_end` and `period_basis`.
* **ATLAS-4:** WEO fiscal-year cells keep their true window, e.g. India 2024 = `[2024-04-01, 2025-04-01)`.
  They are flagged `period_basis: fiscal_year` and `label_year`. Without a stated FY mapping they are
  flagged `fiscal_year_labelled_as_calendar`.
* **ATLAS-5:** the API uses the live origin by default in production, and fixture snapshots are refused
  in production.
* **ATLAS-6:** named-per-cell units are kept, derived values are same-source only, and input flags are
  propagated (section 3).
* **DATA-1:**
  * The minimum-wage catalog concept and the headline concept are "at a reference date" (ILOSTAT month,
    Eurostat semester), with the currency caveat (ILOSTAT LCU = EUR for Norway).
  * OECD annual averages are a separate concept.
  * The IMF and ILO docstrings no longer claim TWN is unmapped.
* The `xfail` markers of `tests/review_v3/test_review_v3_atlas.py` were removed (4 tests pass).

### Query-file build (dev-DB lesson)

The first build over the full dev snapshot (159 tables, 87.5 M rows, 530 MB Parquet) used global sorts
across all rows and filled the disk with ~15 GB of DuckDB spill; it failed cleanly and the work directory
was removed. `_build_db` now works table by table:

* Each Parquet file is fetched (hash- and schema-verified) and its cells are computed into a temporary
  table.
* The rows are appended to `cells` (sorted by country within the table) and `cells_s` (globally sorted by
  series, since tables are loaded in (source, dataset) order).
* The Parquet copy is deleted after loading.
* Limits: memory `REVA_ATLAS_BUILD_MEMORY` (default 3GB), threads `REVA_ATLAS_BUILD_THREADS` (default 2).

Dev result: 16 min, a 6.4 GB query file, and the disk never below ~10 GB free.

## 7. Live load of the dev database `reva` (2026-10-15/16)

### How the load was run

* **Database.** `reva db bootstrap` brought the database to the migration head. It is now at
  `0013_links_ops`; 0011 and 0012 come from other agents and keep the atlas job kinds. `reva seed`
  registered the atlas connector versions.
* **Ingest.** Each source ran through `reva atlas ingest <source>`, one at a time, with a disk check
  before each run (stop if < 6 GB free). WID used `--option countries=` with 40 countries.
* **Restart.** The container restarted mid-load. Tables registered before the restart were kept; the
  remaining sources were run afterwards. The TWNMAP-affected sources (WHR, Constitute, LIED, BMR, V-Dem)
  were re-ingested after the version bumps.

### What is in the dev DB

* **Sources.** 59 atlas sources have at least one completed live run:
  * A-IMF, A-QOL, A-UN, A-ILO (with the Eurostat and OECD wage sources), A-HEALTHEDU, A-HISTECON,
    A-LAW, A-POL
  * A-GEO (Natural Earth, GeoNames, CCKP, INFORM), FAO, A-WB, A-TRADE (BACI, UNCTAD, BaTIS)
  * A-SOCIETY (except RCS), A-SCI (Nobel, WIPO), A-FACTBOOK, A-RESOURCES, KGGEOPOL (UCDP, UNGA)
  * A-FISCAL (OECD fiscal, PISA skills, BIS)
* **Tables.** 159 live atlas tables, 87,542,065 rows in their latest versions; Parquet artifacts total
  530 MB.
* **Raw acquisitions.** 1,594 archived live raw acquisitions, 3.5 GB in total (including the repeated IMF
  and WPP runs). `var/objects` is 3.8 GB.
* **Requests.** The metered runs used 1,533 requests and 3.17 GB over the two budget days, counting the
  last run of each source.
* **Biggest downloads.**
  * World Bank: 19 requests, 672 MB, 10 tables, 21.9 M rows.
  * FAO: 544 MB.
  * BACI: 366 MB.
  * WHO: 260 MB.
  * UN SDG: 234 MB.
  * WID: 176 MB.
  * IMF: 167 MB.
* **Budget.** The daily byte budget (3 GiB) was used up on 2026-10-16, so the ledger refused the tail of
  the queue (typed refusal, nothing partial registered):
  * `atlas.peacekeeping`: response capped at the remaining headroom.
  * The GAPHUNT gap-fill sources were not reached.
  * `atlas.openalex`: provider Retry-After above the politeness cap, 0 tables.
  * `atlas.rcs_dem`: OSF answered 503.

  These sources run on the next budget day, or from their monthly schedule.
* **Not loaded.** CShapes (non-commercial licence) and the KG vote loads, per the lead.

### Snapshot

| item | value |
|---|---|
| snapshot | `b11f1c51bc14197497d167e2bbc7d5d6b4ee320fa79a9db6d1d6ca1444fa9768` |
| origin | live |
| knowledge cutoff | 2026-10-16T19:19:27Z |
| tables / rows | 159 / 87,542,065 |
| query file | `atlas-duckdb/6`: 6.4 GB, built in 886 s, disk never below 9.9 GB free |

### API checks on the dev DB

The checks used the API test client against the dev DB with a temporary read token (revoked afterwards).
Script: `/tmp/claude-0/-home-user-REVA-World/a82f628c-2790-54df-b0b4-28930bdc7a97/scratchpad/verify_api.py`.

* **`/v1/atlas/countries/FRA`: 200.**
  * 5,261 catalog concepts with data; 3,989 without.
  * Headline section: 24 concepts answered. Two concepts have no series in the snapshot yet (the GAPHUNT
    ones). Literacy has no data because UIS publishes no literacy rate for France, and its `consulted`
    list shows that.
  * Example: population 2025 = 68,720,337. WDI answered; `consulted` shows WPP 2024 `no_value` because
    WPP estimates stop in 2023.
  * Example: the IMTS merchandise balance is `derived` (exports FOB − imports CIF, same source).
  * About 30 s on the first call: this includes the one-off sha256 verification of the 6.4 GB query file
    on open (ATLAS-1) and 8.5 k catalog concepts. Later calls in the same process are fast. Use `?theme=`
    or `?domain=` for light calls.
* **Maps: 60-150 ms.**
  * `/v1/atlas/map` for `imf.current_account_balance_gdp` 2025: 194 countries.
  * `/v1/atlas/map` for `quality_of_life.hdi` 2023: 193 countries.
  * `/v1/atlas/map` for `un.wpp.population` 2023: 237 countries.
  * `/v1/atlas/headline/map` for `reva.population.total` 2023: 238 countries (WPP 237, WDI 1).
* **`/v1/atlas/coverage`: 200** in 6.6 s on the first call, memoized afterwards. 9,250 catalog concepts,
  8,730 of them with data in the year view.
* **`/v1/atlas/headline/coverage`: 200** in 18 s on the first call, memoized afterwards.
* **`/v1/atlas/series?concept=history.population_persons&iso3=FRA`.** It starts with HYDE
  `-10000-01-01`..`-9999-01-01`, which is 10001 BCE in astronomical years. Each pre-CE cell is flagged
  `year_numbering`.

### Headline coverage (crosswalk: 251 entities)

| headline concept | countries | years | derived cells | latest year answered by (countries) | declared series not in snapshot |
|---|---:|---|---:|---|---|
| `reva.economy.gdp_per_capita_ppp` | 205 | 1980-2025 | 0 | imf.weo 8; wb.wdi_econ 197 | 0 |
| `reva.economy.gdp_per_capita_usd` | 215 | 1960-2026 | 0 | imf.weo 8; wb.wdi_econ 207 | 2 |
| `reva.economy.gdp_real_growth` | 215 | 1961-2026 | 0 | imf.weo 9; wb.wdi_econ 206 | 1 |
| `reva.economy.gdp_usd` | 215 | 1960-2026 | 0 | imf.weo 8; wb.wdi_econ 207 | 2 |
| `reva.economy.inflation_cpi` | 201 | 1960-2026 | 0 | imf.weo 190; wb.wdi_econ 11 | 1 |
| `reva.education.literacy_rate_adult` | 180 | 1970-2024 | 0 | uis.api 180 | 0 |
| `reva.environment.co2_per_capita` | 214 | 1750-2024 | 0 | owid.co2 214 | 0 |
| `reva.finance.government_net_lending_gdp` | 197 | 1972-2026 | 0 | imf.gfs_soo 12; imf.weo 185 | 0 |
| `reva.geography.land_area` | 216 | 1961-2024 | 0 | wb.wdi_social 216 | 1 |
| `reva.geography.surface_area` | 216 | 1961-2024 | 0 | wb.wdi_social 216 | 1 |
| `reva.health.life_expectancy` | 238 | 1950-2024 | 0 | un_wpp.wpp2024_demo 21; wb.wdi_social 217 | 0 |
| `reva.health.life_expectancy_national_tables` | 0 | None-None | 0 |  | 1 |
| `reva.labour.average_monthly_earnings_lcu` | 192 | 1969-2025 | 0 | ilostat.ear_emta_sex_cur_nb_a 192 | 0 |
| `reva.labour.minimum_wage_monthly_lcu` | 175 | 1980-2026 | 0 | ilostat.ear_inee_cur_nb_a 175 | 0 |
| `reva.labour.unemployment_rate` | 189 | 1991-2025 | 0 | ilostat.une_2eap_sex_age_rt_a 189 | 0 |
| `reva.labour.unemployment_rate_national` | 223 | 1947-2025 | 0 | ilostat.une_deap_sex_age_rt_a 223 | 0 |
| `reva.law.homicide_rate` | 208 | 1990-2024 | 0 | un_sdg.sdg_headline 199; wb.wdi_econ 9 | 0 |
| `reva.population.density` | 216 | 1961-2024 | 0 | wb.wdi_social 216 | 0 |
| `reva.population.total` | 238 | 1950-2025 | 0 | un_wpp.wpp2024_demo 21; wb.wdi_social 217 | 0 |
| `reva.population.total_1_january` | 0 | None-None | 0 |  | 2 |
| `reva.science.internet_users` | 230 | 1990-2025 | 0 | un_sdg.sdg_headline 226; wb.wdi_econ 4 | 2 |
| `reva.society.gini_income` | 171 | 1963-2025 | 0 | wb.wdi_social 171 | 0 |
| `reva.society.hdi` | 193 | 1990-2023 | 0 | undp.hdr25 193 | 0 |
| `reva.trade.current_account_gdp` | 204 | 1960-2026 | 0 | imf.weo 180; wb.wdi_econ 24 | 0 |
| `reva.trade.goods_balance_usd` | 202 | 1948-2025 | 0 | imf.bop 202 | 0 |
| `reva.trade.goods_services_balance_usd` | 202 | 1948-2025 | 0 | imf.bop 202 | 0 |
| `reva.trade.merchandise_balance_imts` | 209 | 2000-2025 | 5376 | derived 209 | 0 |

GAPHUNT gap matrix on this snapshot (`scripts/atlas_gap_matrix.py --snapshot`), saved in
`docs/dev/v3_gap_matrix_snapshot.md` / `.json`:

* With any source, population, area, languages and currency cover 249-251 entities.
* With the headline series alone, population and life expectancy reach 238. The 13 entities missing
  are uninhabited or depend on another state (ALA, ATA, ATF, BVT, CCK, CXR, HMD, IOT, NFK, PCN, SGS,
  SJM, UMI).
* The GAPHUNT gap-fill sources will close the French overseas departments, BES and the Pacific
  territories once they are ingested (next budget day).


## 8. Tests

Commands run (2026-10-16, after the container restart): `uv run pytest -m "not live and not perf"`
over `tests/unit/test_atlas_*.py`, `tests/integration/test_atlas_*.py`,
`tests/review_v3/test_review_v3_atlas.py`, `test_gate_followup.py`, `test_scheduler_slots.py`,
`test_worker_loop.py`, `tests/invariants/test_invariant_30_budgets.py` and `test_api_contract.py`.
Result: **698 passed, 0 failed** in 311 s. That run also included two new test files, which then passed
again on their own (6 passed):

* `tests/unit/test_atlas_post.py` (metered POST)
* `tests/unit/test_atlas_query_periods.py` (point-in-time and semester attachment, 5-digit BCE)

Tests added or adapted by ATLASOPS:

* New:
  * `tests/integration/test_atlas_ops.py`: job kinds, schedules within the budgets, `submit_job`
    allow-list, handlers, CLI, themes, fixture snapshots refused in production.
  * `tests/integration/test_atlas_headline.py`: cross-source fallback, consulted statuses, exact
    scaling, projections, same-source derived values, BCE, API, registry validity.
  * `tests/unit/test_atlas_post.py`
  * `tests/unit/test_atlas_query_periods.py`
* Migrated to the platform:
  * `tests/unit/test_atlas_imf.py`
  * `tests/integration/test_atlas_imf_db.py`
  * `tests/integration/test_atlas_imf_live.py`
  * `tests/unit/test_atlas_qol_connectors.py`
  * `tests/integration/test_atlas_qol_ingest.py`
  * `tests/integration/test_atlas_qol_live.py`
* Adapted to BCE:
  * `tests/unit/test_atlas_histecon.py`
  * `tests/integration/test_atlas_histecon_ingest.py`
* Review markers: in `tests/review_v3/test_review_v3_atlas.py` the ATLAS-1/2/3 `xfail` markers were
  removed.

Known failing outside ATLASOPS's files: `tests/integration/test_schema_contract.py`.
`migrations/schema_contract.json` is behind the migrations 0012/0013 added by other agents; it was
regenerated for 0010 by ATLASOPS.

## 9. Open issues

1. **Daily byte budget.** The 3 GiB/day cap is the binding constraint for a full atlas refresh: WB 0.7
   GB, WID 0.66 GB full, FAO 0.54 GB, BACI 0.37 GB. The monthly schedule spreads the runs over 12 days.
   The sources refused on 2026-10-16 (peacekeeping, GAPHUNT ×5, rcs_dem, openalex) still need a live run.
   OpenAlex needs a politeness and Retry-After window larger than the current cap.
2. **WID is a 40-country subset in the dev DB.** The full run is about 470 requests and 660 MB, and fits
   the budget on its own day.
3. **Query-file cost on open.** Verifying the sha256 of a 6.4 GB file takes about 15 s per process open.
   Opening the file read-only and verifying in the background is possible, but the current choice favours
   integrity. All uid-0 processes share the default cache directory, and `_evict` keeps the 2 newest
   files: tests that do not set `REVA_ATLAS_CACHE_DIR` could evict the dev query file. Tests should always
   set it.
4. **Same-version code changes.** Several in-flight agents still edit their atlas modules without bumping
   versions; ATLASOPS bumped the finished ones. `run_atlas_ingest` refuses such sources until they are
   bumped, and the seed logs and skips them.
5. **WB series keys.** World Bank keys in the headline files (dataset split `wdi_econ`/`wdi_social`) were
   checked against the live WB tables: they all resolve except where noted `series_not_in_snapshot`
   (only GAPHUNT series at present).
6. **Not my files.** `api/routes/system.py` `CANCEL_SCOPE` has no entry for `atlas_ingest`/`atlas_snapshot`,
   so these jobs cannot be cancelled through the API (worker or owner only).
   `tests/integration/test_schema_contract.py` is stale (see Tests).
7. **Equivalence review.** The headline equivalences are agent-declared (`review_status`). A maintainer
   should review them before any belief release uses them. The atlas never feeds releases directly.
