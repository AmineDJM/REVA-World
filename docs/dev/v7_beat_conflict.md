# V7 BEAT-CONFLICT — country-month conflict-risk estimates vs persistence and VIEWS

Agent BEAT-CONFLICT, 2026-10-17. Binding protocol: `docs/roadmap/v7_beat_official.md`. Everything here is a
**country-month risk estimate built from aggregate UCDP counts**. It is never a prediction of a specific attack,
actor or person. Associations used for prediction are not causal claims.

## 1. Result in one paragraph

The evaluation was pre-registered first, in `registry/forecasts/beat_conflict/protocol.yaml` (commit `1679961`,
code sha256 `6cc918d8…`, runner sha256 `a39bcd38…`). The runner refuses to start unless that file is committed
unchanged and both hashes match. There is **one Holm family of 21 tests**: 12 W1 tests are evaluated now, and 9
prospective W3 tests against VIEWS count as p = 1 until their outcomes are published.

**W1** is a final-vintage backtest: country-month, origins 2012-01..2021-12, steps 1-8 and 188,160 scored
forecasts. In W1, REVA's `forecast.cm_hurdle` **beats persistence on all 12 tests** (Brier, log loss, CRPS on
log1p deaths and MSLE in each of the 1-2, 3-5 and 6-8 step groups). Loss ratios are 0.73-0.85, and Holm p is at
most 0.005 inside the 21-test family, using the two-way cluster DM with t(9).

These wins **were measured on a final-vintage backtest (GED 26.1), not in real time**. They admitted the model to
the prospective registry. They are not evidence of real-time skill.

**REVA does not beat VIEWS yet.** No VIEWS window that V6 left unused could be evaluated, because free disk is
4.7 GB (< 5 GB), so no new VIEWS run or Candidate release could be ingested. On the V6-seen window (informational
only, with an information tilt in REVA's favour), REVA's point losses are lower than VIEWS' on every metric, but
**no difference is significant** (best one-sided p = 0.02 before any correction).

The REVA-vs-VIEWS test is now prospective. Batch `3f9e9bf236ebc1342477c158179d9a369d193f0629035a4c98b9f846e649d55a`
was sealed at 2026-10-17T22:28Z, with 2,340 questions on target months 2026-11..2027-04. It holds 9,216 forecasts
from cm_hurdle, cm_stack_views, persistence and the attributed VIEWS run `fatalities003_2026_08_t01`. The
questions resolve on UCDP Candidate releases under the FIX6 `zero_within_coverage` rule.

## 2. What was built

| Part | Path |
|---|---|
| Pre-registration (committed before evaluation) | `registry/forecasts/beat_conflict/protocol.yaml` |
| Models, scores, DM (two-way, t df), Holm family, stacking (pure) | `src/reva/evaluation/conflict/beat.py` |
| Runner (protocol + hash guard, read-only DB) | `src/reva/cli/beat_conflict.py` |
| Seal of W3 (registry batch format, FIX6 absence rule) | `src/reva/evaluation/conflict/beat_seal.py`, `src/reva/cli/beat_conflict_seal.py` |
| Model registration | `registry/models/methods.yaml` (`forecast.cm_hurdle`, `forecast.cm_stack_views`), `beat_conflict_models()` in `src/reva/evaluation/registry/forecasts.py` (seeded) |
| Result (summary, all family tests) | `docs/dev/v7_beat_conflict_result.json` |
| Tests | `tests/unit/test_beat_conflict.py` (7: absence semantics, no look-ahead in features and forecasts, CRPS, Holm with pending = 1, stack weights from past outcomes only, sealed family/question parse with the registry rules) |

**Country-month resolution rule (FIX6, reused).** `reva.evaluation.registry.absence` applies `zero_within_coverage`
to the GED and Candidate aggregates. Within the covered months of a table version, a state of the UCDP universe
with no cell counts as **0 recorded**, and it is labelled as such. Months outside the coverage and states outside
the universe are missing (NaN), never 0. The sealed questions carry `absence_rule` in their resolution and family
docs, so the generic resolver (`registry.service._coverage_for`) handles them.

## 3. Data (no new fetch: free disk 4.7 GB < 5 GB)

Pinned at the DB clock (versions in the result JSON):

- **Outcomes and features:**
  - `atlas.ucdp_ged.country_month` (GED 26.1, CC BY 4.0): `deaths_best|tov=sb/ns/os`, `events|tov=all`.
  - `atlas.ucdp_ged.cand_country_month` (Candidate 2026-01..08, preliminary).
- **Spatial spread:** `atlas.ucdp_ged.events` and `cand_events`. The features are distinct `priogrid_gid` cells,
  and distinct `adm1` units where `adm1_status = placed`.
- **Neighbours:** KG `cshapes:shares_border_with`. The 2019 borders are carried forward.
- **Economic shocks:** `atlas.imf.cpi_m` (CPI index) and `atlas.imf.er_m` (XDC per USD, period average), each read
  with a 2-month lag. Both are the current vintage.
- **VIEWS:** `atlas.views.cm_sb`, the 8 runs loaded in V6. The licence is not stated by the provider, so the data
  is flagged `licence_review`.

**Not used, and why:**

- GDELT is not loaded in the dev DB.
- Coups are not used: Archigos ends in 2015, and no coup table reaches 2025.
- Sanctions (`eusanctionsmap.regimes`) have no monthly dates usable as of t.
- UNHCR refugee data is annual.
- No new VIEWS runs or Candidate months were added, because the disk rule forbids ingesting.

## 4. W1 — family tests (FINAL-VINTAGE backtest, GED 26.1, data cut 2022-12)

Origins 2012-01..2021-12 (120), steps 1-8, 196 states. The event rate (≥ 25 deaths) is 7.4 %. DM differentials
are averaged per (country, origin) over the group's steps. The variance is two-way clustered by country and origin
year, and p comes from Student t with 9 df. Holm runs over all 21 tests.

| Steps | Metric | cm_hurdle | persistence | ratio | t | Holm p | Result |
|---|---|---|---|---|---|---|---|
| 1-2 | Brier | 0.01851 | 0.02332 | 0.794 | -5.41 | 0.0030 | **win** |
| 1-2 | log loss | 0.0642 | 0.0848 | 0.756 | -7.40 | 0.0004 | **win** |
| 1-2 | CRPS log1p | 0.1191 | 0.1418 | 0.840 | -6.45 | 0.0011 | **win** |
| 1-2 | MSLE | 0.2720 | 0.3742 | 0.727 | -5.71 | 0.0022 | **win** |
| 3-5 | Brier | 0.02103 | 0.02607 | 0.807 | -5.10 | 0.0042 | **win** |
| 3-5 | log loss | 0.0725 | 0.0955 | 0.759 | -6.88 | 0.0007 | **win** |
| 3-5 | CRPS log1p | 0.1343 | 0.1613 | 0.832 | -6.26 | 0.0013 | **win** |
| 3-5 | MSLE | 0.3411 | 0.4665 | 0.731 | -4.97 | 0.0046 | **win** |
| 6-8 | Brier | 0.02274 | 0.02814 | 0.808 | -4.97 | 0.0046 | **win** |
| 6-8 | log loss | 0.0788 | 0.1024 | 0.769 | -6.75 | 0.0008 | **win** |
| 6-8 | CRPS log1p | 0.1469 | 0.1731 | 0.849 | -6.01 | 0.0016 | **win** |
| 6-8 | MSLE | 0.3939 | 0.5242 | 0.751 | -4.81 | 0.0048 | **win** |

All the admission rules hold: the model is admitted for both `ucdp.sb_conflict25_cm` and `ucdp.sb_deaths_cm`, and
therefore so is the stack.

**Caveats:**

- GED final is revised data. REVA would only have Candidate in real time, and so would persistence: both sides use
  the same vintage, but real-time skill is unproven.
- IMF CPI and FX are current-vintage data.
- V6 used the annual totals of these years as country-year outcomes (disclosed in the protocol). No country-month
  model had been evaluated on these years before.

**Informational (not in the family):**

- **The IMF shocks add nothing.** The ablation `cm_hurdle_ged`, with no CPI/FX, is as good or slightly better:
  cm_hurdle / cm_hurdle_ged ratios are 0.999-1.017, and none is significant. It is reported as a loss for the
  "economic shocks" idea.
- cm_hurdle beats the country base rate (ratios 0.62-0.78).
- Calibration: the mean probability against the observed frequency per bin is close for cm_hurdle:
  - [0, 0.01): 0.0016 vs 0.0013
  - [0.25, 0.5): 0.37 vs 0.32
  - [0.75, 1): 0.89 vs 0.93

## 5. V — REVA vs VIEWS on the V6-seen window (INFORMATIONAL, not in the family)

The 7 VIEWS runs with known outcomes are fatalities001_2022_12, fatalities002_2023_12, 2024_12, 2025_03, 2025_06,
2025_09 and fatalities003_2025_12. Outcomes are GED 26.1 final up to 2025-12, and Candidate 2026-01..08
(preliminary).

REVA reads GED final up to each run's last input month. VIEWS did not have that information when it released, so
the comparison **favours REVA**. Time clusters are the runs, so p uses 6 df. The p values below are one-sided and
not corrected.

| Steps | Metric | cm_hurdle | VIEWS | t | p | persistence |
|---|---|---|---|---|---|---|
| 1-3 | Brier | 0.0241 | 0.0246 | -0.52 | 0.31 | 0.0278 (VIEWS better) |
| 1-3 | log loss | 0.0834 | 0.1006 | -1.06 | 0.17 | 0.1024 |
| 1-3 | MSLE | 0.313 | 0.349 | -1.38 | 0.11 | 0.440 (VIEWS better) |
| 3-8 | Brier | 0.0256 | 0.0282 | -1.36 | 0.11 | 0.0304 |
| 3-8 | log loss | 0.0883 | 0.1110 | -1.21 | 0.14 | 0.1053 |
| 3-8 | MSLE | 0.344 | 0.418 | -2.62 | 0.020 | 0.511 |
| 7-12 | Brier | 0.0263 | 0.0296 | -1.91 | 0.052 | 0.0310 |
| 7-12 | MSLE | 0.422 | 0.512 | -2.43 | 0.026 | 0.570 |

**REVA vs VIEWS has lower losses on every metric in this window, but none is significant**, even before
multiple-testing correction, with the information tilt in REVA's favour. This is not a win.

VIEWS' log loss suffers from probabilities that are close to 0 or 1 (V6 finding). On Brier, VIEWS beats
persistence in every group. On log loss the result is mixed: VIEWS is slightly better at steps 1-3 and worse at
steps 3-8.

**Stack weights** come from past pairs only (steps 3-8, n = 7,980): w(VIEWS) = **0.20** for P(≥ 25) on log loss,
and 0.20 for the log1p point on MSLE. On this information, past performance gives REVA's model 80 % of the weight.

## 6. W3 — sealed prospective forecasts (the real test vs VIEWS)

**What was sealed:**

- **Batch:** `3f9e9bf236ebc1342477c158179d9a369d193f0629035a4c98b9f846e649d55a`, cycle 2026-Q4, issued
  2026-10-17T22:28:02Z under flock. It has 2,340 questions: 195 states × 6 months × 2 families.
- **Forecasts:** 9,216 issued and 144 abstentions (VIEWS, and therefore the stack, is missing for a few states).
- **Inputs:** origin 2026-08, using GED 26.1 to 2025-12 plus Candidate 2026-01..08, with the fit of December 2025.
- **VIEWS benchmark:** the run in force, `fatalities003_2026_08_t01`, released 2026-09-25.
- **Skipped:** GW 260 (no ISO3 in the crosswalk).

**Examples** (target month 2026-11):

| Question | Last known month | cm_hurdle | stack | persistence | VIEWS |
|---|---|---|---|---|---|
| `fq_0fe0957ee213ff8ee0a7eb65`, Sudan, P(≥ 25) | 206 deaths (2026-08) | 0.976 | 0.981 | 0.926 | 1 |
| `fq_802eadbc46a2427d089c7b4d`, Colombia, P(≥ 25) | 59 deaths (2026-08) | 0.762 | 0.807 | 0.691 | 0.987 |
| `fq_093ba7b45f785e8e454e1b0a`, Norway, P(≥ 25) | — | 0.00084 | — | 0.0041 | 0 |
| `fq_be23a4b78c709a1cfaf97fdc`, Ukraine, deaths | 5,648 (2026-08) | 2,236 | 2,539 | 5,648 | 4,225 |

The Ukraine cm_hurdle and stack values are the MSLE-optimal points, expm1 of the mean on the log1p scale, which is
below the mean.

The W3 tests (9) are evaluated when UCDP Candidate versions covering 2027-04 are in the atlas, likely by mid-2027.
They use Holm over the same 21-test family, with the W1 p values fixed:

- cm_hurdle vs VIEWS on Brier, log loss and MSLE.
- cm_stack_views vs VIEWS on Brier, log loss and MSLE.
- cm_hurdle vs persistence on Brier, log loss and MSLE.

The time clusters are 6 target months, so p uses 5 df: power is low by design. A stack win would read "REVA's
combination with VIEWS beats VIEWS alone".

## 7. Scoreboard (exact)

- **Wins (12 of 21):** all 12 W1 tests, cm_hurdle vs persistence. They come from a final-vintage backtest and are
  admission to the registry, not real-time skill.
- **Pending (9 of 21):** all W3 tests vs VIEWS and persistence, counted at p = 1 today.
- **Losses or no effect (informational):**
  - The IMF CPI/FX shocks add no skill.
  - REVA is not significantly better than VIEWS on the seen window, despite an information tilt in its favour.
  - VIEWS beats persistence on Brier on the seen window (not tested in the family).
- **Not claimed:**
  - Any "REVA beats VIEWS" statement.
  - Any timeliness win against VIEWS. Only 8 of VIEWS' monthly runs are loaded, so between-run comparisons would
    use stale VIEWS runs.

## 8. Missing and owner decisions

1. **Disk below 5 GB.** Once more space is free, ingest the unused VIEWS monthly runs: fatalities002 2023-01..11,
   2024-01..11, 2025-01/02/04/05/07/08/10/11, and fatalities003 2026-01..07. They are about 1.7 MB each (option
   `runs=` of `atlas.views`). Also ingest the Candidate releases after 2026-08. This opens a timeliness track: REVA
   re-issued monthly against the VIEWS run in force. The track needs **its own new pre-registration**, because
   this family is closed.
2. **VIEWS licence** (unchanged from V6): `licence_review` on every VIEWS value and on the stack.
3. **Media signals.** GDELT (`gdelt.events_cd`) is not in this DB, and its history is about 730 days, too short
   for a W1-type backtest. It could only enter as a prospective signal.
4. **Monthly re-issue.** `beat_conflict_seal` seals exactly W3 (origin 2026-08). Later origins are new batches and
   are outside this family.

## 9. Commands

```bash
uv run python -m reva.cli.beat_conflict evaluate --out <full.json> --summary docs/dev/v7_beat_conflict_result.json
flock /tmp/reva-devdb.lock uv run reva seed
flock /tmp/reva-devdb.lock uv run python -m reva.cli.beat_conflict_seal [--dry-run]
flock /tmp/reva-devdb.lock uv run reva forecasts resolve       # generic resolver (FIX6 absence rule) as Candidate months arrive
uv run pytest -q tests/unit/test_beat_conflict.py
```
