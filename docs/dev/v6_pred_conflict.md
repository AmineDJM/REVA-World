# V6 PRED-CONFLICT — conflict-risk forecasting with an honest benchmark (VIEWS, UCDP)

Agent PRED-CONFLICT, 2026-10-17. Everything below is a **country-year or country-month risk estimate built from
aggregate statistics**: no question, forecast or model targets a specific attack, actor or person. Associations
used for prediction are never causal claims.

## 1. Result in one paragraph

VIEWS forecasts (8 monthly runs, 110,016 country-month cells, 13.4 MB, 26 metered requests) are in the atlas as an
**attributed benchmark** (`atlas.views.cm_sb`, forecasts only). Five country-year question families on UCDP
state-based fatalities were back-tested as of each origin 2004-2024 (126,811 scored forecasts, backtest sha256
`719eb458e0998bd932f922361528bad8ce71c85cb81f0d818b5ff2424e46bd11`), with cluster-robust DM tests and Holm
correction, and a country-month comparison against VIEWS on UCDP GED 26.1 (final) and UCDP Candidate 2026
(real-time vintage). **No REVA candidate passed the pre-declared winner rule**, so the sealed batch
(`ef57141ac8e02d9f3e6e9535577ee8a20634cdcad8acc501cc4a0d7cccb2c937`, cycle 2026-Q4, 1,530 questions for 2026/2027)
carries only the three baselines (1,530 forecasts each) and VIEWS attributed (378 quantity questions; it abstains,
with the reason, on the 1,152 binary ones). Headline: *persistence is very hard to beat*; VIEWS beats REVA's
baselines on country-month probabilities at 1-3 months in point estimate, not significantly after Holm, and loses on
log loss on the preliminary 2026 Candidate months because its probabilities are near-degenerate.

## 2. What was built

| Part | Path |
|---|---|
| VIEWS connector (`atlas.views` 1.0.1, source `views.api`) | `src/reva/adapters/connectors/atlas_views.py`, `registry/sources/views.yaml` |
| Budget request (merged: `views.api` 60/day, budgets-1.3.27, digest re-pinned) | `registry/atlas/budget_requests/pred_conflict.yaml` |
| Question families (verbatim UCDP definitions, risk wording) | `src/reva/evaluation/conflict/families.py` |
| Country-year panel (UCDP orgvio/ACD, V-Dem, WDI, CShapes borders from the KG) | `src/reva/evaluation/conflict/panel.py` |
| Forecasters (baselines, KG candidate, VIEWS reference) | `src/reva/evaluation/conflict/models.py` |
| DM (cluster-robust by country) + Holm | `src/reva/evaluation/conflict/stats.py` |
| As-of back-test + VIEWS country-year comparison | `src/reva/evaluation/conflict/backtest.py` |
| Country-month VIEWS benchmark on GED final / Candidate | `src/reva/evaluation/conflict/monthly.py` |
| VIEWS run parsing, annual sums, run choice by release date | `src/reva/evaluation/conflict/views.py` |
| DB: pins, back-test record, sealed issue (registry batch format) | `src/reva/evaluation/conflict/service.py` |
| CLI | `python -m reva.cli.conflict_forecasts backtest|issue` (`src/reva/cli/conflict_forecasts.py`) |
| Registry hooks | `conflict_models()` in `all_models()` (`src/reva/evaluation/registry/forecasts.py`); 5 entries appended to `registry/models/methods.yaml` (seeded) |
| Tests | `tests/unit/test_atlas_views.py` (6), `tests/unit/test_conflict_forecasting.py` (8), `tests/integration/test_atlas_views_ingest.py` (1, db, fixture ingest) — all pass |
| Fixture (real bytes, API `iso` filter SDN/COL/NOR, 120 KB) | `tests/fixtures/source/atlas.views/` |

Batches use the registry format (`reva-forecast-registry-batch/1`, scope `forecast_registry` + `conflict: true`), so
`/v1/forecasts/*`, the leaderboard and the **daily generic resolver** handle them: the frozen families point at
`atlas.ucdp.orgvio_cy` / `sb_total_deaths_best` with `above_fixed` thresholds (24 ⇔ ≥ 25, 999 ⇔ ≥ 1,000).

## 3. Sources

| Source | URL | Licence | Fetched |
|---|---|---|---|
| VIEWS API | `https://api.viewsforecasting.org/{run}/cm/sb/{main_mean,main_dich}` (+ `sc_cm_sb_main`, `sc_cm_sb_dich_main` for `fatalities001`), `/{run}/codebook`, run list | **not stated** by the provider (data page, API README, API wiki checked); citation requested (Hegre et al. 2021, 2022). `open_licence: false`, `licence_review` — the brief's "CC BY" could not be verified | 8 runs: 2022-12 (001), 2023-12, 2024-12, 2025-03/06/09 (002), 2025-12, 2026-08 (003); 26 requests, 13,363,265 bytes |
| VIEWS release dates | `raw.githubusercontent.com/wiki/prio-data/views_api/Available-datasets.md` (verbatim strings on every cell) | GitHub wiki of prio-data/views_api | 1 request |
| UCDP definitions (quoted verbatim in every question) | uu.se UCDP definitions page, HTML sha256 `b938d7a6…59fd0` | CC BY 4.0 (UCDP) | read once |
| Existing atlas (no new fetch) | `atlas.ucdp.orgvio_cy` (26.1), `atlas.ucdp.acd_cy`, `atlas.vdem.indices` (v16), `atlas.wb.wdi_econ/social`, KG `cshapes:shares_border_with` (642 statements); V6CONFLICT's `atlas.ucdp_ged.country_month` (GED 26.1) and `cand_country_month` (Candidate 2026-01..08) | per table | — |

VIEWS definitions quoted from the codebooks: `main_mean` "Point prediction of the number of fatalities in
state-based armed conflict" (originates from `pred_ln_ged_sb_dep`: a log-scale model, so the point is not an
expected value); `main_dich` "Predicted probability of at least 25 battle-related deaths (BRDs) in state-based armed
conflict per country-month". `main_dich` in `fatalities003` is close to 0/1 (160 of 191 countries exactly 0.0 in a
sampled month; the API rounds to 4 decimals) — log losses floor it at 5e-5.

## 4. Question families (country-year, resolved by the next UCDP annual release)

| Family | Type | Asked for | Questions (2026 + 2027) |
|---|---|---|---|
| `ucdp.sb_deaths_cy` | quantity: `sb_total_deaths_best` | every country with ISO3 | 390 |
| `ucdp.sb_conflict25_cy` | binary ≥ 25 | every country | 390 |
| `ucdp.sb_onset25_cy` | binary ≥ 25 (onset) | last published total < 25 | 322 |
| `ucdp.sb_war1000_cy` | binary ≥ 1,000 | every country | 390 |
| `ucdp.sb_escalation1000_cy` | binary ≥ 1,000 (escalation) | last total 25-999 | 38 |

UCDP applies 25 / 1,000 per conflict-year; REVA applies UCDP's cut-offs to the **country-year total** (said in each
definition). 10 skips: GW 260 (Germany) and GW 345 (Serbia/Yugoslavia) carry no ISO3 in KGGEOPOL's table
(crosswalk gap, not fixed here). Example (`fq_99d4aaf1da037df044d2fb59`): "Risk estimate: will UCDP's 2027 total of
battle-related deaths in state-based armed conflict located in Sudan (SDN) reach at least 1,000?" — persistence
0.629, base rate 0.615, history logit 0.823, VIEWS abstains (no calendar-year probability). Quantity
`fq_a9c649f9f0cf18ab0e0fb45a` (Sudan 2027): persistence 12,269, base-rate median 1,264, history-logit median 2,898,
VIEWS 4,024 (sum of 12 monthly points of run `fatalities003_2026_08_t01`, released 2026-09-25). VIEWS run per year:
2026 ← `fatalities003_2025_12_t01` (released 2026-01-30), 2027 ← `fatalities003_2026_08_t01`.

Expected resolution: UCDP 27.1 (≈ June 2027) for 2026, 28.1 for 2027; void after 640 days. **Owner action**: the
`atlas.ucdp` connector hard-codes 26.1 file names and must be moved to each new UCDP version.

## 5. Back-test (final-vintage pseudo-real-time; never real-time skill)

Origins 2004-2024 (data through year *t*), targets *t*+1 (h1) and *t*+2 (h2), fits on target years ≤ *t*. Primary
loss: Brier (binary), CRPS (quantity); ratio to persistence (< 1 better).

| Family | h | base rate | history logit | **KG candidate** | n |
|---|---|---|---|---|---|
| deaths (CRPS) | 1 | 1.52 | 1.07 | 1.12 | 4,103 |
| deaths (CRPS) | 2 | 1.02 | 0.88 | 0.91 | 3,907 |
| ≥ 25 (Brier) | 1 | 2.06 | 0.993 | 0.990 | 4,103 |
| ≥ 25 | 2 | 1.58 | 0.959 | 0.949 | 3,907 |
| onset ≥ 25 | 1 | 1.26 | 1.010 | 0.989 | 3,480 |
| onset ≥ 25 | 2 | 1.14 | 0.985 | 0.951 | 3,319 |
| ≥ 1,000 | 1 | 2.03 | 0.986 | 0.994 | 4,103 |
| ≥ 1,000 | 2 | 1.58 | 0.993 | 1.021 | 3,907 |
| escalation ≥ 1,000 | 1 | 1.25 | 1.015 | 1.133 | 458 |
| escalation ≥ 1,000 | 2 | 1.17 | 1.009 | 1.041 | 437 |

DM (KG candidate vs each baseline, cluster-robust by country, Holm over 30 tests): significantly better than the
base rate on ≥ 25, onset and ≥ 1,000 (Holm p ≤ 0.01), **never significantly better than persistence or the history
logit** (best: onset h2 vs persistence z = -2.23, Holm p 0.29). Winner rule (lower mean loss than every baseline
and Holm p < 0.05 vs each) → **no winner**; the candidate forecasts nothing in the sealed batch.

**VIEWS, country-year (December runs 2022/2023/2024, sum of monthly points):** h1 n = 570, h2 n = 380. MSLE VIEWS
1.32 (h1) / 2.40 (h2) vs persistence 0.84 / 1.60 and history logit 0.87 / 1.61 (two-sided DM z ≈ -2.9, Holm p
0.05-0.07: not significant at 5 %); CRPS (VIEWS = point mass) not significantly different. Caveat: REVA's origin-*t*
forecasts read final UCDP data for *t* (published June *t*+1, after VIEWS' January release, and revised since),
and VIEWS' sums are sums of log-scale point predictions: this comparison favours REVA and is not a verdict.

**VIEWS, country-month on GED 26.1 final (runs 2022-12 … 2025-09, 11,260 pairs per model):**

| Steps | VIEWS Brier / log loss / MSLE | persistence | country base rate |
|---|---|---|---|
| 1-3 | 0.0228 / 0.080 / 0.327 | 0.0252 / 0.093 / 0.368 | 0.0446 / 0.147 / 0.422 |
| 4-6 | 0.0287 / 0.100 / 0.436 | 0.0302 / 0.102 / 0.490 | 0.0469 / 0.156 / 0.483 |
| 7-12 | 0.0295 / 0.108 / 0.569 | 0.0307 / 0.108 / 0.568 | 0.0475 / 0.159 / 0.538 |

VIEWS is better than persistence in point estimate at 1-6 months (not significant after Holm) and significantly
better than the base rate on Brier and log loss (Holm p ≤ 0.01). Same information caveat (baselines read GED final).

**Real-time vintage check, UCDP Candidate 2026-01..08 (PRELIMINARY outcomes; runs 2025-03 … 2025-12):** VIEWS Brier
0.036 / 0.032 / 0.030 vs persistence 0.043 / 0.034 / 0.031; VIEWS log loss **worse** at 1-3 months (0.224 vs 0.156:
near-0 probabilities on months that turned out ≥ 25); nothing significant after Holm. Candidate figures may be
revised by GED 27.1.

## 6. Missing, why, and owner decisions

1. **VIEWS licence** not stated by the provider: confirm (e-mail VIEWS) before showing VIEWS values outside REVA;
   everything is flagged `licence_review`.
2. **Sealed country-month questions not issued**: GED/Candidate tables write no cell when no event is recorded, and
   the generic resolver needs a cell ("absent = 0 within a covered month" is not a registry rule yet). Decision:
   add a resolver rule for zero-by-absence tables (registry owner), then country-month families (VIEWS' own target,
   resolved by Candidate releases) can be sealed.
3. Coups (Archigos ends 2015; Powell & Thyne host returns 403) and sanctions (EUSANCT to 2017, TIES to 2005/2014)
   do not reach 2025, so the KG candidate cannot use them without a structural break; excluded.
4. GW 260 / 345 without ISO3 in `atlas.ucdp.orgvio_cy` (KGGEOPOL crosswalk) — no questions for Germany and Serbia.
5. Back-tests use final vintages (UCDP 26.1, V-Dem v16, current WDI); a real-time track record only comes from the
   sealed batches as they resolve (first: mid-2027).
6. Not done: PRIO-GRID/ADM1 forecasts, ICB-based crisis families (ICB ends 2021), neighbourhood features beyond land
   borders (CShapes ends 2019; 2019 borders carried forward, labelled).

## 7. Commands

```bash
flock /tmp/reva-devdb.lock uv run reva seed                                        # models + atlas.views registration
flock /tmp/reva-devdb.lock uv run python -m reva.atlas.ingest atlas.views          # VIEWS runs (option runs=a,b,...)
flock /tmp/reva-devdb.lock uv run python -m reva.cli.conflict_forecasts backtest   # record the as-of back-test
flock /tmp/reva-devdb.lock uv run python -m reva.cli.conflict_forecasts issue      # seal this cycle's questions
flock /tmp/reva-devdb.lock uv run reva forecasts resolve                           # generic daily resolver
uv run pytest tests/unit/test_atlas_views.py tests/unit/test_conflict_forecasting.py tests/integration/test_atlas_views_ingest.py
```

Each new VIEWS release: add its run id to the `runs` option (or `DEFAULT_RUNS`), ingest, re-run `issue` in the next
quarterly cycle. Pre-existing failures seen, not from this agent: invariant 11 (`reva.evaluation.health_registry.__main__`
imports the object-store factory), invariant 30 (pending `musicbrainz.stats` / `openlibrary.api` budgets).
