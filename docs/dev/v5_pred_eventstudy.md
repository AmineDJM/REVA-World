# V5 — PRED-EVENTSTUDY: event-study / local-projection engine on atlas panels

Agent PRED-EVENTSTUDY, 2026-10-17. Spec: `docs/roadmap/v5_prediction_engine.md` (row PRED-EVENTSTUDY, decisions 3
and 4), `docs/strategy/world_map.md` §5.4 step B, `docs/roadmap/v3_causality.md`. Writes REVA-run effect estimates
through PRED-EFFECT's interface (`docs/dev/v5_effect_interface.md`, `reva.effects`).

**What every number here is:** an *association* estimated by REVA under stated identifying assumptions (onset
timing as good as random given the controls, no anticipation, parallel pre-trends, no other shock coinciding with
the onsets). REVA does not establish causation; each run document and each effect estimate carries this caveat
(EN + FR), its pre-trend test and its placebo results.

## 1. What was built

| Piece | Path |
|---|---|
| Numerical core (numpy only, no I/O): FE OLS (exact one-way, alternating projections two-way), classical / HC1 / CR1 cluster covariance, Jordà local projections h = 0..H with placebo leads, TWFE distributed lead/lag event study with joint pre-trend Wald test, seeded permutation placebo, event-sample bin regression, HP filter | `src/reva/estimation/core.py` |
| Inputs: read-only atlas snapshot panels (best-available per catalog `source_order`, or one provider series; annual, and calendar quarters for provider series), treatments from the events layer (KG) or coded atlas series | `src/reva/estimation/data.py` |
| Treatments registry (7): LV banking & currency crises, Archigos military removals, NCEI earthquakes (M ≥ 7 / ≥ 1,000 deaths), TIES impositions, JST crises | `registry/estimation/treatments.yaml` |
| Replication targets (3) with verbatim quotes, specs, differences and tolerances declared before the first run | `registry/estimation/replications.yaml` |
| Replications + gate (≥ 3 PASS on the same snapshot before any new estimate) | `src/reva/estimation/replication.py` |
| Run orchestration: deterministic run id, as-of, vintage adapter (PRED-VINTAGE), run document, archive, effect estimates via `reva.effects` | `src/reva/estimation/run.py` |
| CLI `reva estimate treatments | replicate | event-study` (2 lines in `src/reva/cli/main.py`) | `src/reva/estimation/cli.py` |
| Tests: 23 unit (synthetic panels with known effects, exactness vs statsmodels, refusals, determinism, replication helpers, PRED-EFFECT validator), 2 db (throwaway DB: KG selection + as-of, archive, effect write + idempotent reload) | `tests/unit/estimation/`, `tests/integration/test_estimation_db.py` |

No migration, no new source, no network fetch by the engine (the three publication PDFs were downloaded once for
the replication targets; they are not in the repository, see §3).

### 1.1 Estimators (exact specification)

**Local projection** (default, `method = local_projection`), for each horizon h = 0..H a separate OLS on the
country-year grid:

`LHS(i,t,h) = β_h D(i,t) + Σ_{l=1..p} γ_l ΔY(i,t−l) + Σ_{l=1..p} δ_l D(i,t−l) + Σ_{j=1..h} φ_j D(i,t+j) + α_i + τ_t + ε`

* `D(i,t)` = 1 if the treatment has ≥ 1 onset in country i, year t (onset counts are kept in the grid);
* `LHS` by mode: `cumsum` Σ_{k=0..h} g(i,t+k) for growth rates (≈ % level change); `diff` y(t+h) − y(t−1) for
  levels (pp); `logdiff` 100·(ln y(t+h) − ln y(t−1)); `level` y(t+h);
* `ΔY` = the one-period change matching the mode (the growth rate itself for `cumsum`), p = 2 by default;
* `D(i,t+j)`, j = 1..h: onsets inside the projection window (Teulings & Zubanov 2014 correction; without it the
  synthetic test showed a 17 % attenuation at h = 5 with country fixed effects);
* country and year fixed effects; SE clustered by country (CR1: G/(G−1)·(N−1)/(N−K), country FE nested in the
  clusters not counted in K, as `reghdfe`); 95 % CI with t(G−1);
* sample rows are restricted to the treatment's **coverage** (years the dataset codes): country-years outside are
  excluded, never treated as "no event".

**Placebo leads** h = −L..−2: same regression without lagged-outcome controls, LHS = Y(t+h) − Y(t−1) (a positive
lead = the level |h|−1 years before the baseline was higher, i.e. a decline in the run-up).
**Event study (pre-trend test)**: `y(i,t) = Σ_{k=−L..H, k≠−1} b_k D(i,t−k) + α_i + τ_t + ε`, endpoints binned,
cluster-robust joint F-test of b_{−L..−2} = 0. **Permutation placebo**: each treated country's onset years are
re-drawn among its at-risk observed years (numpy `default_rng(seed)`), 200 draws by default, two-sided p-value
(1 + #|b_perm| ≥ |b|)/(1 + n).

### 1.2 Reproducibility and as-of

* Run id `es-<sha256(request, snapshot sha, outcome panel digest, event-set digest, code sha)[:20]>`; the code sha
  covers every `src/reva/estimation/*.py` file and both registry files; the git HEAD is recorded too (read from
  `.git`, no subprocess). Same inputs → same run id and byte-identical results (`result_sha256`; tested).
* Outcome panels come from **one pinned atlas snapshot** (sha256 + knowledge cutoff recorded); the DuckDB query
  file is opened read-only after checking its digest record and `meta` table; it is never built, rewritten or
  deleted by the engine (`--verify-bytes` style full re-hash available, refusal leaves the file untouched).
* KG treatments record the source keys, a digest of the raw artifacts, the latest `recorded_at` of the statements
  used and an event-set digest (event id, ISO3, onset date).
* `--as-of D`: KG statements recorded after D, onsets after D and outcome years not finished by D are ignored.
  Without a vintage accessor the outcome is the snapshot's **final vintage**: the run says
  `vintage_mode: final_vintage_pseudo_real_time` and must not be reported as real-time skill (decision 4). With
  `--outcome vintage:<family>.<variable> --as-of D` the panel comes from PRED-VINTAGE's accessor
  (`CutoffRepository(...).vintages(store).latest(...)`, mode `available_then` or `as_known`) through
  `PredVintagePanels`.
* Rights: the run lists restricted sources (e.g. LV "not an open licence", JST CC BY-NC-SA → `non_commercial`).

## 2. Data actually used (live atlas snapshot `717e8452…fcecfa`, cutoff 2026-10-17T01:52Z; dev KG)

| Input | What | Size |
|---|---|---|
| `concept:wb.ny.gdp.mktp.kd.zg` | WDI GDP growth (wb.wdi_econ, CC BY 4.0) | 11,408 cells, 214 countries, 1961-2025 |
| `concept:wb.ny.gdp.pcap.kd.zg` | WDI GDP per capita growth | |
| `concept:wb.sl.uem.totl.zs` | WDI unemployment (ILO modelled), wb.wdi_social | 6,531 cells, 187 countries, 1991-2025 |
| `series:imf.weo.NGDP_RPCH`, `series:wb.wdi_econ.NY.GDP.MKTP.KD` | LV replication | |
| `series:jst.macrohistory.rgdpbarro` | JST R6 real GDP per capita (CC BY-NC-SA) | |
| `series:laeven_valencia.sbcd.output_loss` | LV published output losses | 133 crises |
| KG `ecrises:lv:systemic_banking:*` | 151 onsets, 118 countries, 1976-2014 | raw 54257dd2… |
| KG `archigos:exit:*` (exitcode military) | 271 onsets, 73 countries (12 unmapped historical-state QIDs skipped) | |
| KG `ncei_hazards:earthquake:*` | M ≥ 7: 1,190 onsets / 71 countries (1900-2025); ≥ 1,000 deaths: 143 / 38 | 34 raw artifacts |
| KG `ties:case:*` imposed | 770 onsets, 130 countries, 1946-2005 (65 unmapped QIDs, 8 without imposition date) | |

Countries are mapped only through identifiers: object QIDs via `registry/entities/country_qids.json` (P298-based)
or the crosswalk key a connector stored as a qualifier (NCEI). Unmapped QIDs are counted and skipped.

## 3. Reproductions of published results (before any new estimate)

`uv run reva estimate replicate` — all three targets **PASS** on snapshot `717e8452…` (gate open). Tolerances were
written in `registry/estimation/replications.yaml` before the first computation.

| Target | Published | REVA | Tolerance | Verdict |
|---|---|---|---|---|
| **Jordà, Schularick & Taylor (2013, NBER WP 17621) Table 4**, unconditional paths of log real GDP per capita after normal (N) vs financial (F) recession peaks, years 1-5 | N: −2.0, −0.0, 2.0, 3.3, 4.5; F: −2.7, −3.1, −2.5, −0.9, 1.0 (n = 173 / 50) | N: −2.09, −0.10, 1.91, 3.19, 4.50; F: −2.67, −3.05, −2.41, −0.87, 1.05 (n = 173 / 50) | each of 10 within 2 published SE; counts equal | **PASS** (max deviation 0.43 SE) |
| **Laeven & Valencia (2020)** output losses per banking crisis (their footnote definition, quoted) recomputed from WEO/WDI | 133 crises, mean 30.9 % of trend GDP | 131 computed: Spearman 0.756, median abs. diff 6.3 pp, 60 % within ±10 pp, mean 25.9 | n ≥ 80, Spearman ≥ 0.75, median abs. diff ≤ 10 pp | **PASS (marginal on Spearman)** |
| **Cerra & Saxena (2007, BIS WP 226) Table 2**, all country recession episodes, World Bank data 1960-2001 | −7.5 % cumulative loss, 1.62 years, 637 episodes | −8.19 %, 1.63 years, 728 episodes, 192 countries | ±1.5 pp, ±0.25 y, ±20 % | **PASS** |

Details and documented differences:

* **JST 2013 Table 4.** Peaks: the paper's Table 1 transcribed programmatically from the PDF text (298 peaks, the
  paper's own total). Exclusion rule quoted from the paper ("the war windows 1914–18 and 1939–45 are excluded, as
  are data corresponding to peaks which are within 5 years of the wars looking forwards, or 2 years looking
  backwards"; recessions starting 2007-08 excluded): implemented as "drop p if [p−2, p+5] meets 1914-18 or
  1939-45, or p+5 > 2008" — reproduces 173 + 50 = 223 exactly. Data: JST R6 `rgdpbarro` (2021) instead of the
  2011 Barro-Ursúa file; classical OLS SE (the paper does not state its estimator; REVA's SEs are within 0.05 of
  the published ones).
* **LV 2020 output losses.** Footnote quoted verbatim from the data file (atlas flags). The extrapolation rule of
  the HP trend is not stated; REVA's declared primary reading (trend growth of year T−1) gives Spearman 0.756 —
  just above the threshold. Sensitivities (reported, *not* used for the verdict because they were chosen after
  seeing the data would be specification search): WDI only 0.794 / 5.4 pp; mean trend growth over the window
  0.845 / 4.3 pp. REVA's losses are lower on average (25.9 vs 30.9): later WEO/WDI vintages revised the post-crisis
  paths. 2 crises not computable (fewer than 4 pre-crisis years in both sources).
* **Cerra-Saxena Table 2.** Same definition ("years of negative growth"), current WDI release: 728 vs 637
  episodes (back-filled country-years since 2005), deeper mean loss (−8.2 vs −7.5). A documented-difference
  variant with LV dates instead of Caprio-Klingebiel (association rule chosen by REVA) is printed but not part of
  the verdict.
* Considered and not used: Cerra & Saxena (2008 AER) impulse responses (exact numbers not verifiable from an open
  text; the BIS WP version has no IRF table); Neuenkirch & Neumeier (2015) and Gutmann, Neuenkirch & Neumeier
  (2021, event study, −2.24 pp / −1.76 pp in years 1-2) use the Global Sanctions Database (not loaded; TIES ends
  2005) and a sanction-threat reference group — a TIES-based attempt would not be a reproduction; Noy (2009) /
  Felbermayr & Gröschl (2014) need EM-DAT damages or GeoMet intensities (not loaded).
* Publication files (not redistributed; sha256 in the registry): JST WP `ea302115…`, BIS WP 226 `80c2b084…`
  (+ the LV data file already archived by A-ECRISES). `reva estimate replicate --papers <dir>` archives them as
  restricted `raw` artifacts.

## 4. New REVA estimates (local projections, h = 0..5, annual, 200 permutation draws, seed 20261017)

RESULTS_TABLE_PLACEHOLDER

## 5. Effect-estimate layer

ESTIMATES_PLACEHOLDER

## 6. What is missing / owner decisions

OWNER_PLACEHOLDER

## 7. Commands

```bash
uv run reva estimate treatments
uv run reva estimate replicate [--snapshot <sha>] [--out rep.json]            # gate: >= 3 PASS
flock /tmp/reva-devdb.lock uv run reva estimate replicate --archive --papers <dir with the PDFs>
uv run reva estimate event-study --treatment coup_archigos_military --outcome concept:wb.ny.gdp.mktp.kd.zg \
    --lhs cumsum --horizons 5 [--lags 2 --leads 3 --perm 200 --seed 20261017 --as-of YYYY-MM-DD --out run.json]
flock /tmp/reva-devdb.lock uv run reva estimate event-study ... --write-effects     # archive + PRED-EFFECT loader
uv run reva estimate event-study --treatment banking_crisis_lv --outcome vintage:imf_weo.real_gdp_growth \
    --as-of 2009-12-31 --vintage-mode available_then                                 # PRED-VINTAGE panels
uv run pytest tests/unit/estimation tests/integration/test_estimation_db.py
```
