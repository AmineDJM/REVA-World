# V7 BEAT-NOWCAST: REVA's nowcasts vs the Federal Reserve Banks' official nowcasts

Agent BEAT-NOWCAST, 2026-10-17/18. Roadmap: `docs/roadmap/v7_beat_official.md`. Standard: `docs/dev/review_v6_predictions.md`.
Result JSON: `docs/dev/v7_beat_nowcast_result.json`. Pre-registration: `registry/forecasts/beat_nowcast/protocol.yaml`
(commit `f35d7a9`, code sha256 `11ad0b60…`). Hypotheses: `hypotheses.yaml` (commit `f051a6d`). Holdout:
`holdout.json` + `holdout_ledger.yaml` (opened once, 2026-10-17T23:10:26Z, after both commits were pushed).

## 0. Result in one paragraph

**REVA does not beat any official nowcast on the pre-registered holdout: 0 of 5 hypotheses confirmed** (one Holm
family, m = 5). In development (2014–2021) combinations of the official nowcast with REVA's models looked better
than GDPNow (AE ×0.82) and the New York Fed Staff Nowcast (AE ×0.88). In the holdout (2022–2025) the GDPNow
advantage disappeared (AE ×1.010, p 0.50). Against the New York Fed, the combination had AE ×0.83 over 8 quarters
(p 0.052), which is not significant after Holm. The one Holm-rejected test (NY Fed, CRPS ×0.45) is **not confirmed**:
its 80 % intervals covered 100 % of outcomes, and its benchmark fan was inflated by pandemic-era errors of the
suspended 2016 model (§4). For inflation, the Cleveland Fed CPI nowcast beats everything REVA built (bias-corrected
Cleveland AE ×1.13, REVA's pure CPI bridge ×1.29). REVA's own models (bridge, DFM) never beat GDPNow
(holdout AE ×1.36 / ×1.68). Part of this is a real information handicap (§2.3).

## 1. Sources (connector `atlas.fed_nowcasts` 1.0.0, five vintage tables)

| table / family | provider file | what | licence (verbatim in table rights) |
|---|---|---|---|
| `fed_nowcasts.gdpnow` / `atl_gdpnow` | Atlanta Fed `GDPTrackingModelDataAndForecasts.xlsx` (10.9 MB): `TrackingDeepArchives` (2011Q4–2014Q1, "made before the model went 'live'", flagged), `TrackingArchives`, `CurrentQtrEvolution` | 2,150 update dates × quarter (2011Q3–2026Q3); BEA advance estimate + its publication date as recorded (60 quarters) | Atlanta Fed: reproduction "not for commercial gain or monetary profit", attribution, **non-commercial** |
| `fed_nowcasts.nyfed` / `nyfed_nowcast` | NY Fed `new-york-fed-staff-nowcast_data_2002-present.xlsx` (2016 model; ends 2021-08-27) + `New-York-Fed-Staff-Nowcast_download_data.xlsx` (model reintroduced 2023-09-08) | weekly; quarters < 2016Q1 are "historical reconstructions" and rows before 2023-09-08 are pre-relaunch output: loaded **without availability** (never selected) | NY Fed licence "to use, copy, and distribute Content for your personal or business purposes" |
| `fed_nowcasts.cleveland` / `clev_inflation_nowcast` | Cleveland Fed `nowcast_month.json`, `nowcast_quarter.json` (12.6 MB) | daily CPI/core CPI/PCE/core PCE nowcasts 2013-07→2026-10 (m/m and q/q annualised) + the "Actual" value on its release day | Cleveland Fed "noncommercial, personal, or educational purposes only", credit, **non-commercial** |
| `fed_nowcasts.rtdsm_monthly` / `philfed_rtdsm_monthly` | Philadelphia Fed RTDSM monthly vintages `employMvMd`, `iptMvMd`, `hMvMd`, `hstartsMvMd`, `rconmMvMd` (9.4 MB) | vintages 2010M01→2026M10 (~201 each), observations from 1990; usable from the 1st of the following month | no licence stated, "All rights reserved" (**restricted**, internal evaluation) |
| `fed_nowcasts.release_values` / `philfed_release_values` | Philadelphia Fed `routput_first_second_third.xlsx` | real GDP q/q SAAR first/second/third/most-recent release (truth) | same, restricted |

The archive has 11 files, 55 MB. Every vintage cell carries `available_at` and `availability_basis`. Five new
availability rules (`fed_nowcast_publication_day`, `rtdsm_monthly_vintage_month_end`,
`bea_release_recorded_by_atlanta_fed`, `bea_release_fourth_month_bound`, `bea_release_sixth_month_bound`,
`philfed_file_last_updated`) and five families were added to `vintage_common.py`. Budget: `fed.nowcasts` 30/day
(`budgets-1.3.41`, digest re-pinned in `policy.py`). Registry: `registry/sources/fed_nowcasts.yaml`.

Other inputs come from the dev DB, read-only:
- `vintage_us.rtdsm` (RTDSM quarterly CPI vintages), which gives CPI history.
- `eia.spot_d`, the NY Harbor conventional gasoline spot price (Refinitiv data via EIA, licence-review flag). It is
  used only 8 days after the price day because EIA publishes it weekly.

**Not used.** These sources were not used, and why:
- **FRED/ALFRED (St. Louis Fed).** This covers ALFRED vintages and the Economic News Index (STLENI). It is
  reachable now. However, its Terms of Use prohibit using "FRED® Content in connection with the development or
  training of any software program or system or machine learning … or any other program or process commonly known
  as artificial intelligence". **Owner decision.**
- **Euro area.** No official nowcast with an update history was found. The ECB publishes none, and Eurostat's
  flash estimate is the release itself.
- **NY Fed 2021-09 → 2023-09.** The nowcast was suspended in that period.

**Live ingest is not done (disk).** Free space was 4.0–4.9 GB during the session, below the 5 GB rule. The
connector, its fixtures and its tests are complete, and `uv run reva seed` was run before the drop. The evaluation
ran on the raw files, parsed by the same connector parsers. The sha256 of every file is pinned in
`holdout.json.pins`. The CLI option `--source files` exists for this case.

## 2. Design (pre-registered)

### 2.1 Same-date rule

- **Unit.** One case is one official nowcast of target period P published on day d, strictly before the truth's
  release day.
- **REVA cutoff `d 00:00 UTC`.** Every pure REVA input must be published before the official's publication day.
- **Official cutoff `d+1 00:00`.** This covers the official value of day d itself and, for `stack_all`, the other
  official nowcast published by d.
- **Guard.** Every case's inputs pass `LeakageGuard(available_then)`: 48,645 guard checks, 0 blocks.
- **Inference.** The loss differential is averaged over all publication days of a target. DM-HLN is one-sided with
  horizon 2.

### 2.2 Truth

**GDP.**
- Primary truth: the Philadelphia Fed "First" release value (BEA advance/initial estimate). It is known from the
  day after BEA's publication date as recorded by the Atlanta Fed.
- Cross-check: across 58 quarters, the Atlanta Fed's record of BEA's advance estimate differs from the
  Philadelphia Fed value by at most 0.002.
- Secondary truth: the third estimate, descriptive only.

**CPI.**
- Truth: first-release m/m as recorded by the Cleveland Fed on the BLS release day.

**Gaps.**
- 2025Q3 and 2025Q4 have `#N/A` first/third values in the Philadelphia Fed file (shutdown-delayed releases), so
  they are not scored. The holdout therefore has 14 of 16 GDP quarters.
- October 2025 CPI was never published, so the CPI holdout has 47 of 48 months.

### 2.3 Candidates

| key | type | definition |
|---|---|---|
| `forecast.nc_bridge` | pure REVA | GDP bridge OLS on 5 RTDSM monthly indicators, AR(1) ragged edge, first-release targets from 1993 (excluding 2020Q2–Q3) |
| `forecast.nc_dfm` | pure REVA | the V6 one-factor DFM (`reva.models.forecasting.dfm`, unchanged), same panel |
| `forecast.nc_cpi_bridge` | pure REVA | CPI: mean of last 12 known m/m + gasoline spot change; core: means of last 12 and 3 known core m/m |
| `forecast.nc_stack_reva` | **uses the official nowcast** | O + λ(R − O); λ from past errors of the same phase bucket (last 12 quarters / 24 months), shrunk to 0.5 |
| `forecast.nc_stack_all` | **uses both official GDP nowcasts** | the same with R = mean(REVA points, the other Fed nowcast published by d) |
| `forecast.nc_biascorr` | **uses the official nowcast** | O + shrunk mean past official error of the bucket |

Densities are the point plus the candidate's own past errors. The fair benchmark is N(O, RMS of the official's own
past errors), as in QA-PRED X1.

**Information handicap (structural, against REVA).** RTDSM monthly vintages are usable only from the 1st of the
next month. EIA spot prices are usable only after 8 days. GDPNow and the NY Fed see each release the same day.

## 3. Development (targets 2014–2021, 32 quarters / 96 months; selection sample without 2020Q2–Q3)

| cell | official MAE | best candidate (dev, selection sample) | relative | p |
|---|---|---|---|---|
| GDP vs GDPNow (1,217 updates) | 1.60 | stack_reva AE | 0.816 | 0.026 |
| | | stack_all CRPS vs fair fan | 0.898 | 0.14 |
| | | bridge AE / DFM AE | 1.06 / 1.53 | – |
| GDP vs NY Fed 2016 model (481 updates, 23 q) | 2.93 | stack_all AE | 0.881 | 0.0005 |
| | | stack_all CRPS | 0.846 | 0.11 |
| CPI m/m vs Cleveland (2,876 days) | 0.111 | biascorr AE | 0.9998 | 0.44 |
| | | pure CPI bridge AE | 1.39 | – |
| core CPI vs Cleveland | 0.106 | none < 1 (best biascorr 1.008) | – | – |

Selection rule: per (cell, loss), the best combination and best pure candidate with relative < 1. This gives **5
hypotheses, all combinations**. No pure-REVA candidate qualified.

## 4. Holdout (targets 2022–2025), one Holm family m = 5

| # | hypothesis | n targets / updates | relative | p (one-sided) | Holm | cov80 | confirmed |
|---|---|---|---|---|---|---|---|
| 1 | GDPNow, stack_reva, AE | 14 / 554 | **1.010** | 0.50 | no | – | **no** |
| 2 | GDPNow, stack_all, CRPS vs fair fan | 14 / 554 | 0.986 | 0.43 | no | 0.85 | **no** |
| 3 | NY Fed, stack_all, AE | 8 / 156 | 0.831 | 0.052 | no | – | **no** |
| 4 | NY Fed, stack_all, CRPS vs fair fan | 8 / 156 | 0.452 | <1e-4 | yes | **1.00** | **no** (coverage outside [0.70, 0.90]) |
| 5 | Cleveland CPI, biascorr, AE | 47 / 1,341 | **1.132** | 0.90 | no | – | **no** |

**Why #4 is not a win.** Its benchmark fan is built from the NY Fed's past errors in the last 12 quarters. At the
2023 relaunch those were 2016-model errors from 2018–2021, pandemic included. The fan was therefore far too wide
(CRPS 2.25 against an MAE of 1.03, coverage 100 %). The candidate's own density had the same problem (coverage
100 %). The coverage rule correctly refuses it.

**Holdout descriptives (not hypotheses, cannot be claimed):**

| cell | official MAE | bridge | DFM | stack_reva | stack_all | biascorr |
|---|---|---|---|---|---|---|
| GDP vs GDPNow | 1.047 | ×1.36 | ×1.68 | ×1.01 | ×1.02 | ×1.02 |
| GDP vs NY Fed (2023 model) | 1.031 | ×0.89 (p 0.003) | ×0.97 | ×0.96 | ×0.83 | ×0.94 |
| CPI m/m vs Cleveland | 0.135 | ×1.29 | – | ×1.03 | – | ×1.13 |
| core CPI vs Cleveland | 0.100 | ×0.97 (p 0.28) | – | ×1.01 | – | ×1.10 |

- **Against the third estimate (secondary truth).** GDPNow MAE is 1.084, stack_reva ×1.001 and stack_all ×1.010.
  NY Fed MAE is 1.271, stack_all ×0.875 and bridge ×0.928.
- **Signal for a future protocol, not a result.** REVA's pure bridge was ×0.89 the NY Fed's AE over 8 holdout
  quarters. It failed development (×1.12), so it is not a pre-registered hypothesis.

**Honest scoreboard:**
- **Beats an official nowcast:** nothing.
- **Loses to:**
  - GDPNow: every candidate.
  - Cleveland CPI: every candidate.
  - Cleveland core CPI: every candidate on CRPS; the pure bridge is ×0.97 on AE, not significant.
- **Not significantly different:** combinations vs the NY Fed (8 quarters, underpowered).

## 5. Disclosures

- **Pre-registration timing.** Development was run while debugging, before the protocol was committed. No model
  specification was changed after seeing development scores.
- **Holdout errors seen in advance.** GDPNow's TrackRecord rows for 2025Q2–2026Q2 (final-nowcast errors) were seen
  while inspecting the workbook. No REVA holdout number existed before the opening.
- **`written_at` mismatch.** The protocol's `written_at` reads 23:30Z, but the commit is at 23:01:58Z. The commit
  time is the binding timestamp.
- **Code committed by a lead WIP snapshot.** The code was first committed inside lead WIP snapshots
  (`6de8866` and earlier). The pre-registration commit pins its digest, and the CLI refuses to run if the digest
  differs.
- **Data the protocol did not anticipate.** The two unscored quarters (2025Q3–Q4) and the stale fair fan at the NY
  Fed relaunch are consequences of the data, not choices.

## 6. Prospective (sealed)

`registry/forecasts/beat_nowcast/sealed/2026-10-17.json`, canonical sha256 `c7370673…`, indexed in `sealed.yaml`,
16 records.

**GDP 2026Q4 and 2027Q1.** No official nowcast exists yet: GDPNow starts after the Q3 advance release, and the NY Fed
workbook has no 2026Q4 column. Only pure REVA points are sealed:

| target | bridge | DFM |
|---|---|---|
| 2026Q4 | 2.27 | 2.55 |
| 2027Q1 | 2.28 | abstains |

**CPI October 2026, against the Cleveland nowcast of 10/16:**

| | Cleveland | stack_reva | biascorr | pure bridge |
|---|---|---|---|---|
| CPI | 0.269 | 0.260 | 0.296 | 0.192 |
| core CPI | 0.211 | 0.213 | 0.210 | 0.224 |

**CPI November 2026 → March 2027.** No official nowcast yet, so these records are open.

**Not wired into the forecast registry DB.** Two reasons: the resolution table `atlas.fed_nowcasts.release_values`
is not ingested (disk), and no question family exists for US quarterly first-release GDP or monthly CPI. Since none
of these scopes was confirmed, nothing here may be claimed. The records are kept for the prospective scoreboard
only. Re-seal on later official dates with `beat_nowcast seal --as-of <day>`; each record is a new file and is never
overwritten.

## 7. Files

- **Connector:** `src/reva/adapters/connectors/atlas_fed_nowcasts.py`.
- **Evaluation:** `src/reva/evaluation/beat_nowcast/{data,models,cases,service}.py`. It does not import the object
  store; DB access is only in the CLI `src/reva/cli/beat_nowcast.py`.
- **Tests:**
  - `tests/unit/test_atlas_fed_nowcasts.py`: 8 tests.
  - `tests/unit/forecasting/test_beat_nowcast.py`: 11 tests, covering as-of selection against the
    VintageAccessor, second pass without look-ahead, guard, bridge, ledger.
  - `tests/integration/test_atlas_fed_nowcasts_db.py`: fixture ingest into a throwaway DB, read-back as of a date.
- **Fixtures:** `tests/fixtures/source/atlas.fed_nowcasts/` (11 excerpts, 137 kB, licence per entry).
- **Test runs:** 20 passed; invariants 191 passed, 1 skipped.

## 8. Commands to finish on a bigger machine (free disk ≥ 5 GB)

```bash
flock /tmp/reva-devdb.lock uv run reva seed
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.fed_nowcasts      # ~34 MB raw, 10 requests
# re-seal on new official publication days (GDPNow 2026Q4 from ~2026-10-30), from the DB tables:
uv run python -m reva.cli.beat_nowcast seal --source db --as-of 2026-11-02
```

## 9. Owner decisions

1. **FRED/ALFRED.** Its terms forbid use for developing or training AI or ML systems. Decide whether REVA may use it
   (it would give exact-date vintages of BLS/BEA series and remove REVA's information handicap) or whether REVA
   should go to BLS/BEA/Census directly.
2. **Atlanta Fed and Cleveland Fed content.** It is licensed for non-commercial use. Decide whether sealed records
   and stacks that embed their values can be displayed and redistributed.
3. **Philadelphia Fed files.** There is no stated licence ("All rights reserved"). Keep them for internal evaluation
   only.
4. **Registry wiring.** Add registry question families (US first-release GDP q/q SAAR, CPI m/m) once
   `atlas.fed_nowcasts` is ingested.
