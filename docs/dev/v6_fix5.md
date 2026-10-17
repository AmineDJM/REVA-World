# V6 — FIX5: projection vocabulary, future-dated cells, out-of-range values (QA-DATA D1, D2, D6)

Agent FIX5, 2026-10-17. Input: `docs/dev/v6_qa_data.md` / `docs/dev/qa_report_latest.md` (QA-DATA defects D1, D2,
D6), `docs/dev/v6_fix4b.md` (the M6 projection-filter work this replaces with one shared rule). All fixes are **read
time**. No value, row, manifest or archived object was changed. No connector was bumped and nothing was re-ingested:
the query-time rules close every defect, and free disk was 4.3 GB, under the 5 GB ingest rule. No migration.

## Result: `uv run reva qa run --domain time --domain units` (dev DB `reva`, origin live, 345 tables, 108.8 M cells)

| check | before (QA-DATA) | after (FIX5) |
|---|---|---|
| `time.projection_word_unflagged` (**D1**) | **fail** 10,804 (fao.gt `forecast`) | **pass** 0 / 28,495,043 |
| `time.future_observation` (**D2**) | **fail** 15,329 | **pass** 0 / 97,094,905 |
| `time.estimate_kind_unclassified` (new) | — | **pass** 0 / 108,809,405 |
| `time.projection_by_date_rule` (new) | — | warn 4,518: these cells are now read as not observed, and the connector should flag them itself |
| `time.projection_table_unflagged` | pass 0 | pass 0 (provider flags only) |
| `units.negative_count` (**D6**) | warn 942 (wb.edstats UIS.MENF.56) | **pass** 0: the series is a signed net flow (provider definition) |
| `units.bounded_share` (**D6**) | warn 531 | warn 531: kept as published and flagged at read time (`flags.reva_qa`) |

Summary for these two domains: fail 0, warn 4, info 3, pass 7. The two other warns were not touched. One is
`units.per_capita_identity` 217 (D5). The other is `time.precision_mismatch` 1,096: these are `boe.bs_q` cells
added after QA-DATA by another agent, and that agent should look at them. Full output:
`/tmp/claude-0/-home-user-REVA-World/a82f628c-2790-54df-b0b4-28930bdc7a97/scratchpad/qa_fix5.{json,md}`. `docs/dev/qa_report_latest.md` was not
regenerated; the lead's final pass does that.

`time.projection_by_date_rule` breakdown: the 15,329 future-dated cells minus the 10,804 FAO forecasts (D1) and
the 7 treaty dates:

| table | cells | why not observed |
|---|---:|---|
| ilostat.eap_dwap_sex_age_rt_a / une_deap / how_temp / sdg_0831 | 2,349 / 783 / 261 / 261 | rows sourced "ILO - Modelled Estimates" (XA:8405) inside national series, years 2027+: after the table's record date |
| oecd.pag | 503 | Pensions at a Glance projections (PPEP, OAWAR, LE at 65) to 2084, `flags {}`: after the record date |
| eia_steo.steo_observed | 354 | 2026-11 to 2027-12: after the STEO release `series_last_updated` 2026-10-06 (see below) |
| who.gho | 6 | `M_Est_smk_curr_std` 2030 (WHO comment: "This is a projection."): after the record date |
| imf.er_m | 1 | ZWE XDC_USD 2027-03 (an IMF cell dated in the future): after the record date |

## D1: one projection vocabulary for every reader

* **`registry/atlas/projection_kinds.yaml`** is the only list. Each `estimate_kind` value is put in one class:
  * `observation`: observation, actual, published_history, provisional, observation_gridded, announced_schedule.
  * `estimate` (a provider estimate of a past period; it still answers observation questions, labelled "estimate"):
    estimate, estimated, imputed, model_estimate, modelled, modeled, nowcast, reanalysis.
  * `projection` (never an observation): projection, projected, **forecast** (FAOSTAT "F" = "Forecast value"),
    **model_simulation** (CMIP6 "historical" runs; these also carry a truthy `projection` flag), unknown_boundary,
    published_history_boundary_unknown.
  * Any other value is **unknown and treated as not observed** (fail-safe).
* **`src/reva/atlas/projection.py`** holds the rules. It provides `PROJECTION_SQL` (flags only),
  `projection_sql(alias, period=, recorded_sql=)`, `is_projection(flags, period_start=, recorded=, table=)`,
  `kind_class` and `estimate_status`. A cell is not an observation when any of these holds:
  1. its kind is a projection kind;
  2. its kind is unknown;
  3. the `projection` flag is truthy, or `period_after_vintage` is true;
  4. its period starts after a provider release date carried in its flags (`series_last_updated`);
  5. its period starts after the date its table was recorded. Readers that do not know that date use their as-of
     date (today) instead.

  Rules 4 and 5 do not apply to the `announced_schedule` kind or to the `cbcal.meetings` and `untc.treaty_status`
  tables. Those hold announced future dates: the date a notified treaty withdrawal takes effect, or a meeting
  date. They are facts as announced, not values of a future period.
* **Readers switched to it:**
  * The atlas query layer (`reva.atlas.query`):
    * Headline resolution uses rules 1–5, taking rule 5 from an Arrow table `tr` (table sha256 → `recorded_at`
      of the snapshot's table).
    * Every cell returned by `_cell` carries `projection: true` when the rules mark it. This covers table and series
      reads, the API and the UI. The UI only shows what the API returns.
    * `PROJECTION_SQL` is re-exported for compatibility.
  * The decision engine's baseline (`reva.predict.db`).
  * The forecast registry (`reva.evaluation.registry.questions`):
    * `extract_series(frame, family, recorded)` takes the pin's `recorded_at` and drops cells after it from the
      observed series. The service passes it at issue, resolution and backtest time.
    * The registry gets no "today" fallback, so tests that simulate later releases still work.
  * The health registry (`health_registry.core`): kinds and flags only, no date fallback.
  * The energy backtests' `outcomes_from_frame`, which also used to count STEO 2026-11+ cells as outcomes.
  * NLQA: `ports.is_projection` / `cell_is_projection` / `estimate_status`, in the executor, adapters and compose.
  * The QA checks: `projection_frame` runs the same SQL in DuckDB.
* `PROJECTION_KINDS` in `reva.evaluation.registry.questions` and `health_registry.core` is now the shared tuple.
  It used to be `("projection", "unknown_boundary")` and missed `forecast` and `published_history_boundary_unknown`.
* Not changed: `reva.evaluation.vintages` / `pred_models.realtime` / `vintage_backtests`. These filter a column
  `estimate_kind != "projection"` of the **vintage store**, which is a separate vocabulary (actual /
  published_history / published_history_boundary_unknown). Real-time evaluation uses
  `published_history_boundary_unknown` on purpose as "values as then published".

## D2: future-dated cells

* **EIA STEO**: the connector already uses EIA's `lastHistoricalPeriod`. The problem is EIA's own metadata: on some
  series of the 2026-10 release (e.g. `STEO.COPRAP.M` / `.A`, Appalachia crude production) it declares **202712 /
  2027**, which is later than the release itself (`last_updated` 2026-10-06). So the 354 cells sit at or before the
  declared boundary and went to `steo_observed`. Fix: rule 4 above. A period that starts after the release date in
  the cell's own flags is a forecast, whatever boundary is declared. The release month itself (2026-10) is a period
  that has begun; QA reports it under `period_not_ended`, as before.
* **ILO**: the connector flags projections (`flags.projection`, years after the TOC vintage) only in `ilo_modelled`
  indicators. In the *national* indicators, rows whose source is "ILO - Modelled Estimates" are flagged
  `ilo_estimate_in_national_series` with no projection marker. **OECD PAG**: the provider sends no flags (`{}`).
  For both, rule 5 applies, which is the "reference period after publication" rule, using the table's record date
  as the publication bound. It is documented in the yaml and in `reva.atlas.projection`.
* **Residual, for the connectors** (the warn check lists the tables):
  * ILO rows inside national series for the vintage year +1 that has already started (e.g. 2026 from a Nov-2025
    vintage) are not caught by a date rule. `atlas.ilo` should set `projection` on rows sourced XA:8405 for years
    after the modelled-estimates vintage.
  * `atlas.eia_steo` could move periods after `last_updated` to `steo_forecast` itself.
  * Either change needs a version bump and a re-ingest when disk allows (commands below).

## D6: values out of range (never altered)

* **`registry/atlas/value_range_notes.yaml`**, read by `src/reva/atlas/value_notes.py`, lists the following series.
  Definitions are quoted verbatim from the WDI / EdStats metadata in `registry/atlas/catalog/worldbank.yaml`.

  | series | rule | cells | note |
  |---|---|---:|---|
  | `wb.wdi_social.EG.ELC.RNWX.ZS` (renewables excl. hydro, % of electricity production) | range [0, 100] | 453 | above 100 contradicts the definition |
  | `wb.wdi_social.ER.H2O.FWDM.ZS` (domestic share of total withdrawal) | range [0, 100] | 27 | the provider's "can exceed 100 percent" clause is about withdrawals vs *renewable resources*, a different ratio |
  | `wb.wdi_social.ER.H2O.FWAG.ZS` (agriculture share of total withdrawal) | range [0, 100] | 4 | same as above |
  | `wb.edstats.UIS.MENF.56` | `signed: true` | 942 | "inbound students … minus … outbound students": a net flow, so negative values are within the definition |

* **Range series**: every cell the atlas query layer returns, if its value is outside the range, is served with its
  value unchanged. A **copy** of its flags carries `reva_qa = {check: outside_definitional_range, range,
  value_unchanged: true, note, registry}`. This reaches NLQA, the API and the UI. The QA `units.bounded_share` check
  keeps counting these cells (warn).
* **Signed series**: `units.negative_count` no longer counts them.
* Not covered, left for the owners: `wrp.national sumreligpct` > 1 (42), `sscp.frac_v1 language_frac` > 1 (2),
  `SL.TLF.BASC.MA.ZS` (2), `EG.USE.CRNW.ZS` (1). They can be added to the same file with the provider definition.

## Tests

| file | what |
|---|---|
| `tests/unit/test_fix5_projection_kinds.py` (22 + 1 live) | Every `estimate_kind` value present in the dev DB on 2026-10-17 (15 values, with cell counts) is classified. Every `estimate_kind` literal in `src/reva/adapters/connectors/*.py` (AST scan) is classified, which catches new connectors. SQL (DuckDB) and Python give the same result on a 14-case matrix (FAO forecast, an unknown kind, CMIP6, STEO before/after release, ILO and OECD after record date, announced schedule, vintage flag). Covered as well: table exemption and BCE text, readers sharing the tuple, NLQA `cell_is_projection`, energy outcomes skipping STEO 2026-11, QA `projection_frame`, and value notes (flag on a copy, value unchanged, signed series). `-m live` re-scans the DB. |
| `tests/unit/test_qa_fixture.py` (updated) | future cells are now reported under `projection_by_date_rule`; `forecast` is no longer an unflagged forecast word; `projection_table_unflagged` still counts cells without a provider flag |

Re-run, all passing:
* `tests/unit/{nlqa,predict}`
* `test_forecast_registry`, `test_qa_fixture`, `test_energy_forecasting`, `test_registry_energy`, `test_atlas_*`
* the 16 unit files that use the atlas query
* `test_fix4b_projection_sql`, `test_pred_health`, `test_v4health`, `test_finance_registry`
* integration: `test_atlas_headline`, `test_atlas_table_cells`, `test_atlas_api`

That is 310 + 204 + 20 tests. One test fails, and not because of this change:
`tests/unit/nlqa/test_nlqa_causal.py::test_ui_fixture_is_the_current_pipeline_output`. Its UI fixture predates
`prompt_version` nlqa-plan-1.3.0 and a catalog digest change made by another agent. That agent should regenerate it
(`REVA_REGEN_UI_FIXTURE=1`).

## Owner decisions

1. **Classes**: `reanalysis` (ERA5) is classed as estimate, and `provisional` and `observation_gridded` (CRU) as
   observation. `model_simulation` (CMIP6 historical) is classed as projection. Confirm.
2. **Rule 5 anchor**: the date the table version was **recorded**, which is at or after the fetch. A cell dated
   between the fetch and the record time (minutes) is not caught. Flags-only readers fall back to today.
3. **Announced future dates** (`untc.treaty_status`, `cbcal.meetings`) are observations of an announcement, not
   projections. `untc.treaty_status` is added to QA `future_ok_tables`.
4. Should `time.projection_by_date_rule` be **fail** for CI? Today it is warn, because readers are already
   protected and the remaining work is connector hygiene.

## Commands (to finish on a bigger machine; not run: free disk 4.3 GB < 5 GB)

```
# optional connector hygiene (after bumping atlas.ilo / atlas.eia_steo as described in D2 "residual")
flock /tmp/reva-devdb.lock uv run reva seed
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.ilo
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.eia_steo
# verification (read-only)
uv run reva qa run --domain time --domain units --fail-on fail
uv run pytest -m live tests/unit/test_fix5_projection_kinds.py
```
