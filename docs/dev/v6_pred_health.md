# V6 PRED-HEALTH — health / disease forecasting with honest benchmarks

Agent PRED-HEALTH, 2026-10-17. Brief: open surveillance series with timely resolution, question families (weekly
influenza positivity / ILI by country, US hospital admissions vs the CDC hub ensembles, annual deaths by cause),
baselines and candidates on as-of back-tests scored with WIS and Diebold-Mariano tests against the hub ensembles,
sealed forecasts (baselines + attributed hub ensemble + significant winners only), disease links by published
identifiers only.

## 1. Result in one paragraph

Four new atlas sources were loaded live (9 tables, 1.36 M cells, **59.4 MB raw**, cap 100 MB). Six question families
were back-tested and the back-tests sealed (`model.evaluation`, kind `health_backtest`); one live round was issued
and sealed: **1,212 questions, 3,448 quantile forecasts** (23 hub quantile levels each), batch
`0ab8fcc25d7710d21aef988d138e902f1d1117be948a47e4337f806cbecfd383` (issued 2026-10-17 21:27:08 UTC). Honest
headline: **the CDC hub ensembles beat every REVA model** on the as-of back-tests (FluSight: REVA's best model has
relative WIS 1.14, DM p = 0.026; COVID: 1.25, p < 1e-5). REVA's quantile blend significantly beats the
persistence and climatology baselines for FluNet positivity, and a log-drift model beats persistence for WHO GHE death
trends (final-vintage, 11 origins only), so those families issue the winning candidates. All other families issue
baselines (and the attributed ensemble) only. Nothing is resolved yet (first resolutions: NHSN on 2026-10-22, FluNet
/ ERVISS within 2–4 weeks; GHE at the next WHO release).

## 2. Sources (live, 2026-10-17)

| Atlas source → tables | What was fetched | Requests / raw bytes | Licence |
|---|---|---|---|
| `atlas.who_flunet` → `who.flunet` (152,172 cells) | WHO xMart `FLUMART/VIW_FNT`, one provider-side slice per country (30-country panel, ISO_YEAR ≥ 2015): specimens received/processed, influenza A/B/all positives, negatives, per `ORIGIN_SOURCE` | 30 / 7.35 MB | CC BY 4.0 + WHO terms |
| `atlas.cdc_flusight` → `cdc.flusight_target` (12,985), `cdc.flusight_vintages` (173,098; 61 dated vintages 2024-11-16 … 2026-10-10), `cdc.flusight_ensemble` (277,932; 57 rounds) | public S3 mirror of `cdcepi/FluSight-forecast-hub`: target CSV, `auxiliary-data/target-data-archive/target-hospital-admissions_<date>.csv`, `model-output/FluSight-ensemble/*.parquet` (horizons 0–3, quantiles) | 121 / 34.3 MB | MIT repo; CDC asks to be contacted about attribution (flagged) |
| `atlas.cdc_covidhub` → `cdc.covidhub_target` (17,119), `cdc.covidhub_vintages` (250,902; 92 `as_of` vintages 2024-11-20 … 2026-10-14), `cdc.covidhub_ensemble` (448,592; 92 rounds) | S3 mirror of `CDCgov/covid19-forecast-hub`: target CSV, hubverse `target-data/time-series.parquet`, `CovidHub-ensemble` parquet | 95 / 11.1 MB | CC0 / US public domain; attribution requested (flagged) |
| `atlas.ecdc_erviss` → `ecdc.erviss_ili` (8,146), `ecdc.erviss_sentinel` (14,362) | `EU-ECDC/Respiratory_viruses_weekly_data` raw CSVs: all-ages ILI/ARI consultation rates; sentinel influenza tests/detections/positivity (2022-W25 … 2026-W41) | 2 / 6.6 MB | **licence_review** (no LICENSE reachable; ECDC site 403 to non-browser clients) |

Connectors: `src/reva/adapters/connectors/atlas_{who_flunet,cdc_resp_hubs,ecdc_erviss}.py`; sources
`registry/sources/{who_flunet,cdc_resp_hubs,ecdc_erviss}.yaml`; budget request `registry/atlas/budget_requests/pred_health.yaml`
(merged in budgets-1.3.30); geo scheme `cdc_hub` (`registry/atlas/geo_aliases/pred_health.yaml`: only `US` → USA;
state FIPS codes stay unmapped; ERVISS country *names* are never mapped to codes). Values verbatim, nulls are typed
Missing, hub ensemble cells are `estimate_kind = projection` and never read as observations. Vintage tables keep a
52-week window per vintage plus the full history of the earliest one. Reused: V4HEALTH's live `atlas.who.ghe`.
Not used: OWID (its COVID series stopped updating; WHO/CDC/ECDC are the originals); WHO measles/cholera dashboards
(no provider-side download found without the dashboard app — not pursued in this budget); RespiCast (ECDC hub):
model-output file names cannot be listed without the GitHub API (blocked) — next benchmark for ERVISS ILI.

## 3. Question families (`registry/forecasts/health_question_specs.yaml`)

| Family | Series | Round / targets | Reference forecaster | Back-test |
|---|---|---|---|---|
| `flunet.flu_positivity` | REVA derivation from FluNet counts: 100·ΣINF_ALL/ΣSPEC_PROCESSED_NB over the week's origin rows (INF_ALL read as 0 only where INF_NEGATIVE = SPEC_PROCESSED_NB), ≥ 30 specimens | ISO week; +1…+4 weeks | — | final vintage, lag 2 w, 196 weekly origins from 2023-01 |
| `erviss.ili_rate` | ILIconsultationrate, all ages (verbatim) | ISO week; +1…+4 | — (RespiCast later) | final vintage, lag 1 w |
| `erviss.flu_positivity_sentinel` | sentinel influenza positivity (verbatim) | ISO week; +1…+4 | — | final vintage, lag 1 w |
| `nhsn.flu_hosp` | FluSight `wk inc flu hosp` | hub reference date (Saturday); horizons 0…3 | FluSight-ensemble (attributed) | **as of** each round's archived vintage |
| `nhsn.covid_hosp` | COVID hub `wk inc covid hosp` | same | CovidHub-ensemble (attributed) | **as of** each round's `as_of` vintage |
| `ghe.deaths` | WHO GHE deaths, causes 0, 10, 600, 1510, 20, 380, 610, 1100; G20+ panel | annual; last published year (2023) +1…+3 | — | final vintage, origins 2010–2020 |

Resolution: first table version registered after the seal publishing the cell as an observation; void after the
family's `void_after_days`; already-observed → not prospective. Every forecast is the 23 hub quantiles, scored by WIS
(Bracher et al. 2021: (1/(K+½)) Σ pinball), absolute error of the median, 50 % / 90 % coverage.

## 4. Models and the winner rule

Baselines (always issued): `health.persistence` (last value + symmetrised empirical h-step changes),
`health.climatology` (same season week ±2 in earlier seasons, ≥ 3 years, ≥ 26 weeks before target). Candidates (issued
only as winners): `health.log_ar` (AR(2) on weekly Δlog(y+1), deterministic residual bootstrap), `health.log_drift`
(annual log random walk with drift), `health.blend` (quantile average). References: `forecast.reference_flusight_ensemble`,
`forecast.reference_covidhub_ensemble` (registered in `registry/models/methods.yaml`, seeded). Winner rule: relative
WIS < 1 against **every** baseline and DM (HLN-corrected, Newey-West lag 3, per-origin mean WIS differences)
p < 0.05 / #baselines.

## 5. Back-test results (sealed; `health_backtest` rows)

| Family (kind, origins) | Mean WIS | Key comparisons (relative WIS, DM p) | Winners |
|---|---|---|---|
| FluNet positivity (final vintage, 196) | blend 3.14, log_ar 3.17, persistence 3.78, climatology 3.95 | blend/persistence 0.84 (p 9e-11), blend/climatology 0.81 (p 6e-16) | **blend** |
| ERVISS ILI (final vintage, 144) | climatology 34.7*, blend 40.4, persistence 46.8, log_ar 48.8 | blend/persistence 0.85 (p 2e-4), blend/climatology 1.05 (p 0.70) | none |
| ERVISS sentinel positivity (final vintage, 144) | log_ar 5.32, blend 5.36, climatology 5.80*, persistence 6.30 | blend/persistence 0.88 (p 0.001), blend/climatology 0.99 (p 0.91) | none |
| NHSN flu (as of, 47 rounds; 9 rounds without an archived vintage skipped) | **FluSight-ensemble 142.0**, blend 162.0, log_ar 211.7, persistence 220.9, climatology 291.2 | ensemble/persistence 0.64 (p 0.002); blend/ensemble 1.14 (p 0.026); blend/persistence 0.73 (p 0.003); blend/climatology 0.56 (p 0.14) | none |
| NHSN COVID (as of, 88 rounds) | **CovidHub-ensemble 30.5**, blend 38.9, persistence 40.0, log_ar 41.1 | ensemble/persistence 0.76 (p 2e-7); blend/ensemble 1.25 (p 9e-6); blend/persistence 0.95 (p 0.026) | none |
| GHE deaths (final vintage, 11 annual origins) | log_drift 25,822, blend 27,084, persistence 30,686 | log_drift/persistence 0.84 (p 3e-5) | **log_drift, blend** |

\* climatology is only available where ≥ 3 earlier seasons exist (ERVISS starts 2022-W25; COVID hub vintages start
2024-11, so COVID climatology is almost never applicable — 689 tasks — and does not pass the "beats every baseline"
test for anyone). Calibration: persistence 50 % intervals are far too narrow in-season for flu admissions (coverage
0.23); the ensembles are close to nominal (FluSight 0.54 / 0.83, CovidHub 0.52 / 0.90). The US national level
dominates the hub WIS averages (US: FluSight-ensemble 3,372 vs blend 3,865).
Caveats: FluNet/ERVISS/GHE results use final (revised) values — **never real-time skill**; GHE estimates are WHO model
output (smooth by construction), 11 origins only; DM tests pool locations within an origin.

## 6. Issued round (live, sealed)

Batch `0ab8fcc2…cd383`: flunet 108, ERVISS ILI 88, ERVISS sentinel 88, NHSN flu 212, NHSN COVID 212, GHE 504
questions. Forecasts: persistence 1,212, climatology 696 (+12 abstained), blend 612 (FluNet and GHE only — the
winners), log_drift 504 (GHE), FluSight-ensemble 212, CovidHub-ensemble 212 (both attributed: "not a REVA forecast").
Example: US flu admissions, epiweek ending 2026-10-24 (`hq_b436cbf777935aa7bf84997c`, last observed 3,835 for
EW-2026-10-10): medians persistence 3,835, climatology 1,536.5, FluSight-ensemble 5,679. Skipped and listed: 11 stale
series (e.g. ERVISS countries with no report since 2026-W09), 4 questions where every issued model abstained.
Commands: `uv run python -m reva.cli.health_registry {backtest|issue|resolve|status}` (moved out of
`reva.evaluation` for invariant 11; worker credentials, run under `flock /tmp/reva-devdb.lock`).

## 7. Disease links (V4HEALTH ids) — none, by rule

No source publishes a disease or pathogen identifier: FluNet (virus-type column names), NHSN/hubs (target names),
ERVISS (pathogen names), GHE API (WHO cause codes and titles, no ICD). Linking "influenza" → `doid:8469` or GHE
"Respiratory infections" → a Mondo class would be name matching, so every family carries `disease_link: null` with
the reason. Evidenced routes (owner decisions): (a) archive the GHE methods annex (ICD-10 lists per cause, CC BY-NC-SA
3.0 IGO) and join to DOID/Mondo ICD-10 xrefs (V4LINK); (b) a reviewed FluNet column → NCBI Taxonomy crosswalk
(INF_A → `ncbitaxon:11320`, INF_B → `ncbitaxon:11520`) maintained by a person, then the DOID/Mondo infectious-agent
claims already loaded give the disease.

## 8. Owner decisions

1. CDC hubs' attribution request (contact flusight@cdc.gov / covidhub@cdc.gov) before any publication of
   ensemble-vs-REVA comparisons. 2. ERVISS reuse terms (licence_review on every cell). 3. GHE annex / taxon crosswalk
   for disease links (§7). 4. Whether to publish the health registry (same question as the V5 registry).

## 9. Tests, files, commands

Tests: `tests/unit/test_pred_health.py` (16: parsers, typed Missing, listing/DTD/truncation refusal, vintage windows,
WIS identity, models, DM, week indices/rounds, positivity rule, as-of history, as-of back-test vs ensemble, winner
rule, questions, GHE), `tests/integration/test_pred_health_db.py` (fixture ingest of the 4 sources → back-tests →
sealed issue → later version → resolution and scores). Fixtures `tests/fixtures/source/atlas.{who_flunet,cdc_flusight,
cdc_covidhub,ecdc_erviss}` (trimmed real bytes, licence + note per entry). Code: `src/reva/models/forecasting/health_quantile.py`,
`src/reva/evaluation/health_registry/{core,service}.py`, `src/reva/cli/health_registry.py`.
Invariant 11: the remaining failure is `reva.evaluation.pred_models.cli` (PRED-MODELS), not this module.

```bash
df -h /   # >= 5 GB before any ingest
for s in atlas.ecdc_erviss atlas.who_flunet atlas.cdc_covidhub atlas.cdc_flusight; do flock /tmp/reva-devdb.lock uv run reva atlas ingest $s; done   # weekly
flock /tmp/reva-devdb.lock uv run python -m reva.cli.health_registry resolve    # daily
flock /tmp/reva-devdb.lock uv run python -m reva.cli.health_registry issue      # weekly (new rounds only)
flock /tmp/reva-devdb.lock uv run python -m reva.cli.health_registry backtest   # after each ingest season
```
Not scheduled as worker jobs yet (would touch the shared `operations/jobs.py`/`schedules.py`; lead decision).

## FIX7 addendum (2026-10-17, QA-PRED §7 / X9, `docs/dev/v6_fix7.md`)

* The FluNet `blend` and WHO GHE `log_drift` / `blend` "winners" are **final-vintage results**: admitted on
  back-tests run on revised / backfilled data (FluNet late reports favour AR-type candidates over climatology), and
  for GHE on 11 annual origins of a *modelled* series that WHO re-estimates in every release. They are not evidence of
  real-time skill; skill will only come from resolved prospective questions (or archived FluNet / RespiCast vintages).
* Registry metadata (going forward): every new health batch carries `candidate_admission` on the family and on each
  question (`backtest_kind`, `final_vintage: true`, note "FINAL-VINTAGE RESULT …"), an `admission` note on every
  candidate forecast record, and on GHE questions `target_note` = "forecast of the next WHO Global Health Estimates
  release (a modelled estimate …), not of registered deaths" (`health_registry.service.admission_basis`).
* Sealed batch `0ab8fcc2…` (612 blend + 504 log-drift candidate forecasts) is unchanged; an annotation with the same
  wording is appended to it (`scripts/fix7/annotate_sealed_batches.py`).
* Multiplicity (X3, minor): the FluNet and GHE p-values (≤ 3e-5) survive a Bonferroni correction over the 18
  candidate × family cells; the winner rule should still declare that family before the next run.
