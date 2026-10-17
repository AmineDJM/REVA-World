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

| Treatment → outcome (LHS) | Sample | IRF h = 0..5 | SE | h = 5 [95 % CI] | Placebo leads (SE); pre-trend | perm. p < 0.05 at h | Run id |
|---|---|---|---|---|---|---|---|
| `banking_crisis_lv` → `concept:wb.ny.gdp.mktp.kd.zg` (cumsum) | 151 onsets; h5 sample: 144 events, 212 countries, 8527 obs; years 1961-2025 | -1.86 / -4.69 / -5.65 / -6.51 / -6.82 / -7.47 | 0.44 / 0.88 / 1.25 / 1.53 / 1.78 / 1.94 | -7.47 [-11.30, -3.64] | h-3: +3.32 (1.07); h-2: +1.99 (0.71); F-test p = 0.02 | 0,1,2,3,4,5 | `es-ba6863761c52640f8fa5` |
| `banking_crisis_lv` → `concept:wb.sl.uem.totl.zs` (diff) | 151 onsets; h5 sample: 77 events, 187 countries, 4487 obs; years 1991-2025 | +0.60 / +1.78 / +2.01 / +2.13 / +2.50 / +2.81 | 0.17 / 0.37 / 0.41 / 0.46 / 0.55 / 0.61 | +2.81 [+1.62, +4.00] | h-3: -0.63 (0.36); h-2: -0.03 (0.14); F-test p = 0.61 | 0,1,2,3,4,5 | `es-291a40103934bbb2b7a8` |
| `coup_archigos_military` → `concept:wb.ny.gdp.mktp.kd.zg` (cumsum) | 271 onsets; h5 sample: 140 events, 211 countries, 8899 obs; years 1961-2025 | -2.41 / -3.35 / -4.06 / -4.59 / -4.47 / -3.92 | 0.53 / 0.71 / 0.93 / 1.12 / 1.24 / 1.33 | -3.92 [-6.55, -1.29] | h-3: +0.64 (0.74); h-2: +0.82 (0.53); F-test p = 0.21 | 0,1,2,3,4 | `es-9af1a4b329e01ee5e382` |
| `earthquake_ncei_m7` → `concept:wb.ny.gdp.mktp.kd.zg` (cumsum) | 1190 onsets; h5 sample: 344 events, 212 countries, 9903 obs; years 1961-2025 | -0.36 / -1.08 / -1.85 / -2.04 / -1.85 / -1.73 | 0.27 / 0.52 / 0.73 / 0.83 / 0.85 / 0.87 | -1.73 [-3.44, -0.03] | h-3: +1.00 (0.55); h-2: +0.56 (0.30); F-test p = 0.21 | 1,2,3,4 | `es-76c2041e480734d2686a` |
| `earthquake_ncei_1000_deaths` → `concept:wb.ny.gdp.mktp.kd.zg` (cumsum) | 143 onsets; h5 sample: 52 events, 212 countries, 9903 obs; years 1961-2025 | -0.70 / -0.34 / -1.61 / -2.38 / -1.15 / -0.99 | 0.44 / 0.81 / 1.08 / 1.28 / 1.80 / 1.90 | -0.99 [-4.74, +2.76] | h-3: +2.37 (1.46); h-2: +1.01 (1.12); F-test p = 0.11 | none | `es-c3a9b13419c5b3a1f0ec` |
| `sanctions_ties_imposed` → `concept:wb.ny.gdp.pcap.kd.zg` (cumsum) | 770 onsets; h5 sample: 436 events, 205 countries, 6849 obs; years 1961-2025 | +0.17 / -0.21 / +0.23 / +0.20 / +0.25 / +0.27 | 0.36 / 0.39 / 0.50 / 0.59 / 0.65 / 0.73 | +0.27 [-1.18, +1.72] | h-3: +0.02 (0.50); h-2: -0.21 (0.25); F-test p = 0.66 | none | `es-1b3eff04653ec3971c9c` |

Units: `cumsum` = cumulative GDP growth from t to t+h in % (≈ % change of the GDP level relative to country-years
without an onset); `diff` = change of the unemployment rate from t−1 to t+h in percentage points. Placebo leads:
Y(t+h) − Y(t−1), a positive value = higher level before the baseline year. All runs: snapshot `717e8452…`,
lags 2, leads 3, future-onset controls, country + year FE, SE clustered by country, replication gate open (3/3).

Reading (associations, not established effects):

* **Coups → GDP** (Archigos military removals): GDP about 2.4 % lower in the coup year and 3.9 % lower after five
  years than in comparable country-years (95 % CI −6.6 to −1.3); no significant pre-trend (p = 0.21), permutation
  p < 0.05 at h = 0..4. Successful military removals only; the onset year mixes pre-coup crisis and coup.
* **Banking crises → unemployment** (LV starts 1991-2017 where ILO modelled estimates exist): +0.6 pp in the start
  year, +2.8 pp after five years (CI +1.6 to +4.0); pre-trend p = 0.61. ILO modelled estimates are themselves
  model-based for many countries.
* **Banking crises → GDP**: −7.5 % at five years (CI −11.3 to −3.6), but the **pre-trend test fails** (p = 0.02;
  placebo leads +3.3 and +2.0: output was already falling before the coded start year). Flagged; **not written** to
  the effect layer (rule in §5). Its size is close to the output losses usually cited for banking crises, which is
  not evidence of identification.
* **Earthquakes M ≥ 7 → GDP**: −1.7 % at five years (CI −3.4 to −0.03), significant at h = 1..4 by permutation;
  lead h = −3 +1.0 (SE 0.55) is not significant but positive. 1,190 onsets include many offshore or low-damage
  events attributed to the country NCEI names.
* **Earthquakes with ≥ 1,000 deaths → GDP**: no horizon significant (−1.0 %, CI −4.7 to +2.8; 52 events at h = 5).
  Selecting on deaths selects on damage (see caveat), and the sample is small.
* **TIES sanction impositions (any sender) → GDP per capita**: no detectable association (+0.3 %, CI −1.2 to +1.7).
  This is not a replication of Neuenkirch & Neumeier (2015) or Gutmann et al. (2021) (other database, all senders,
  no threat reference group); it does not contradict them either.

## 5. Effect-estimate layer

Writer: `reva.estimation.run.write_effect_estimates` → `reva.effects.load_effect_estimates` with
`source_kind = "reva_run"`, `source_key = "reva.eventstudy"`, one `dataset` per (treatment, outcome)
(`eventstudy.<treatment>.<concept>`), the archived run document (artifact namespace `evaluation`, schema
`reva-eventstudy-run/1`) as the delivery's raw artifact, `complete = True` (a re-run with other numbers retracts
the old row for that dataset). One estimate per run: key `run:<run_id>:h5`, `cause_kind = event_type`
(`banking_crisis`, `coup`, `earthquake`, `sanction_episode`), `effect_kind = atlas_concept`, `measure = response`,
`shock = 1 occurrence`, unit `percent` (cumsum) or `percentage_points` (diff), SE + 95 % CI, full `irf` h = 0..5,
scope (treated countries ISO3, period, n_obs, n_countries), `method = local_projection`, identification text with
the pre-trend p-value and seed, `run = {run_id, code_version, data_snapshot "atlas:<sha>", as_of, spec_sha256}`,
`transcription = computed`, rights flags, curation `maintainer_review_pending`, applicability note labelled REVA's
with the causation caveat. Validated by `reva.effects.validate_estimate` in the unit tests and written/reloaded
(idempotent) in the throwaway-DB test.

**Writing rule (declared before the first write, enforced in code):** a run is written only if its replication gate
is open on the same snapshot and its pre-trend diagnostics pass (event-study F-test p ≥ 0.10 and no placebo lead
significant at 5 %). Null results are written like any other (no publication bias). Hence 5 of the 6 runs qualify;
`banking_crisis_lv → GDP` is archived and documented but not written.

EFFECT_STATUS_PLACEHOLDER

## 6. What is missing / owner decisions

* **Rights.** Laeven-Valencia (IMF terms, "not an open licence", flagged by A-ECRISES) and JST (CC BY-NC-SA,
  non-commercial) feed runs and replications; run documents and effect estimates carry `restricted` /
  `non_commercial` flags. Maintainer review of the LV terms is still pending (owner decision).
* **Publication PDFs** (JST NBER WP, BIS WP 226) are archived as restricted `raw` artifacts for provenance only
  (EconStor/NBER/BIS terms: personal and scholarly use); they are not in the repository. Owner: confirm this use.
* **PRED-EFFECT curation of the reproduced publications.** The published numbers REVA reproduced live in
  `registry/estimation/replications.yaml` (replication targets, not effect estimates). If PRED-EFFECT curates them as
  `pub:` entries, the next runs can list them in `run.replicates` (empty today).
* **Quarterly panels.** The engine handles calendar-quarter provider series, but the pinned snapshot holds only BIS
  property prices and debt-service ratios at quarterly frequency; the quarterly macro series of V4ECON were not in
  snapshot `717e8452…`. No quarterly estimate was produced.
* **Real-time (vintage) runs.** The PRED-VINTAGE adapter is wired (`vintage:<family>.<variable>` + `--as-of`) and
  unit-tested with a stub accessor; no live vintage run was made (vintage tables were landing during this work).
  Every estimate written here is `final_vintage_pseudo_real_time`.
* **Not reproduced / out of reach with loaded data:** Cerra-Saxena (2008 AER) IRFs (numbers not in an open
  text), sanctions papers based on the Global Sanctions Database, disaster papers based on EM-DAT / GeoMet. Owner:
  EM-DAT needs an account (non-commercial licence) — not worked around.
* **LV output losses reproduced marginally** (Spearman 0.756 vs threshold 0.75): treat that PASS as weak; a
  maintainer may decide to declare the mean-trend-growth reading as a new target (a new id, with this outcome kept).
* **Identification.** None of these designs has an instrument or a natural experiment; they are selection-on-
  observables event studies. PRED-ENGINE must treat them as associations with their pre-trend diagnostics, and
  prefer published quasi-experimental estimates when available.

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
