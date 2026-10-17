# V7 BEAT-HEALTH — trying to beat the CDC hub ensembles (FluSight, COVID-19 Forecast Hub) on WIS

Agent BEAT-HEALTH, 2026-10-17. Binding protocol: `docs/roadmap/v7_beat_official.md`. Standard: `docs/dev/review_v6_predictions.md`.

## 1. Result in one paragraph

**No win.** On the development rounds (the V6 as-of rounds, already spent), no REVA method beats the hub ensembles:
the best candidates tie them (FluSight: width recalibration, relative WIS 0.998, DM p = 0.94; COVID: quantile
averaging with REVA's blend, 1.004, p = 0.35). Twelve further variants tried during development (signals, bias and
PIT corrections, hierarchy) were all worse than the ensemble out-of-sample. Even the in-sample oracle bound (best
constant correction picked with hindsight) is only 2–5 % WIS. So the dev admission rule admitted nothing and
**no prospective forecast was sealed**. Two candidates are frozen and pre-registered
(`registry/forecasts/beat_health/protocol.yaml`, module sha256 `19578136…70d3`, committed in `44c7dbb` / `55b4ef5`)
for a single-look holdout on rounds that no one could score at registration: **H1** = the 2026-27 season (reference
dates 2026-10-17 … 2027-05-29, evaluation on or after 2027-07-03). **H2** = FluSight 2023-24, which REVA has never
loaded; its ingest is queued because free disk was 4.8 GB, below the 5 GB rule. ECDC/ERVISS and FluNet have no official
forecaster in the atlas, so they have no test.

## 2. What "beat" means here (pre-registered)

* Comparison: each candidate vs the hub ensemble **of the same round** (FluSight-ensemble / CovidHub-ensemble, as
  published, attributed). The candidates *take the ensemble as an input*, so a win would be worded "REVA's
  post-processed ensemble beats the ensemble alone". It would never be worded "REVA beats the hub".
* Information set at round R: the ensemble's quantiles for R; NHSN **as-of** vintage R−7 (flu) / R−3 (COVID), as in
  V6; past WIS is computed only against values **as published in round R's vintage**. No later revisions are used,
  and no external signal enters any candidate.
* Truth: primary = the latest target table (the V6 rule and the hubs' own convention). Secondary =
  the first release (what the V6 sealed registry resolves on). Both are reported, but only the primary can declare a win.
* Test: DM with the HLN correction on per-round mean WIS differences, Newey-West lag 3. A test counts as a win only when
  relative WIS < 1, the mean difference is < 0, and the Holm-adjusted p is < 0.05. There is **one Holm family** for the
  whole holdout: 2 candidates × {H1 flu, H1 COVID, H2 flu} = 6 tests. A declared test that cannot run counts as p = 1.
  Coverage at 50 % and 90 % is reported for every model.

Frozen candidates (in `src/reva/evaluation/health_registry/beat.py`). Each uses past-WIS grids per horizon, pooled
over US + 52 jurisdictions with the hubs' own location codes. Pooling here means one shared parameter across the
hierarchy; state codes are never mapped to ISO. Each candidate equals the ensemble until 200 past tasks have been scored.

| Candidate | Brief item | Rule |
|---|---|---|
| `beat.qa_blend` | (a) stacking with the ensemble as an input, (d) pooling | `w·ensemble + (1−w)·REVA health.blend`, w ∈ {0, 0.05, …, 1} |
| `beat.width_recal` | (c) calibration from the ensemble's own past errors, (d) | `median + s·(q − median)`, s ∈ {0.8 … 1.5} |

## 3. Development results (V6 rounds: flu 47 rounds / 9,805 tasks; COVID 88 rounds / 18,338 tasks)

Source: `registry/forecasts/beat_health/dev_result.json` (`uv run python -m reva.cli.beat_health dev`; pins: the
latest live versions of `atlas.cdc.{flusight,covidhub}_{target,vintages,ensemble}` as of 2026-10-17). These are
**selection data, not evidence**.

| Family / truth | Ensemble WIS (cov50 / cov90) | `beat.qa_blend` rel. WIS (p) | `beat.width_recal` rel. WIS (p) | REVA blend alone |
|---|---|---|---|---|
| Flu, final | 142.00 (0.539 / 0.830) | 1.034 (0.089) — loss | 0.998 (0.94) — tie; cov 0.654 / 0.886 | 1.137 (0.026) — loss |
| Flu, first release | 128.21 (0.545 / 0.837) | 1.049 (0.065) | 0.999 (0.97) | 1.143 (0.005) |
| COVID, final | 30.54 (0.517 / 0.896) | 1.004 (0.35) — tie | 1.006 (0.30) — tie; cov 0.467 / 0.853 | 1.241 (7e-6) — loss |
| COVID, first release | 28.65 (0.530 / 0.892) | 1.005 (0.24) | 0.998 (0.75) | 1.242 (6e-5) |

Dev Holm family (4 tests, primary truth): adjusted p = 0.35 / 0.94 / 0.90 / 0.90. **0 wins, 4 non-wins**. Of these,
`qa_blend` on flu and both COVID candidates have relative WIS > 1. The weights the candidates learned: flu ensemble
weight 0.82 on average; COVID 0.91. Learned widths: flu 1.38 (the ensemble's flu intervals are too narrow: 90 % coverage 0.83);
COVID 0.90 (it narrows, which lowers 90 % coverage to 0.85 for no WIS gain).

### Every variant tried in development (all on the spent V6 rounds, past-only; relative WIS vs ensemble, final truth)

| Variant | Flu | COVID | Note |
|---|---|---|---|
| PIT recalibration (per-level coverage, shrunk n0 = 500) | 1.037 | 1.025 | |
| QA blend, then PIT recalibration | 1.124 | 1.025 | |
| log-median bias shift (shrunk) | 1.063 | 1.040 | |
| bias as a function of own NHSN growth, least squares | 1.086 | 1.042 | |
| own-growth multiplier, b picked by past WIS | 1.057 | — | |
| median scale factor (past WIS) / width + scale | 1.008 / 1.008 | 1.055 / 1.061 | |
| the same with an 8-round window: width / scale / both | 1.011 / 1.000 / 1.008 | 1.007 / 1.045 / 1.049 | |
| **FluNet USA positivity growth** (lag 2 w), least squares / WIS grid | 1.192 / 1.051 | — | final-vintage signal: informational only |
| bottom-up US = comonotone sum of state ensemble quantiles (in-sample) | 1.029 on US rows | 1.012 on US rows | average with the US ensemble: 1.013 / 0.996 |

In-sample correlation between FluNet positivity growth (USA 0.15–0.28, CAN 0.17–0.24 by horizon) and the
ensemble's log error is real but small. Fitted on past data only, it made forecasts worse. In-sample oracle bounds
(one constant per family, chosen with hindsight): flu −2.5 % (widen ×1.35 or scale ×1.1), COVID −0.9 % (final) /
−4.9 % (first release, scale by horizon). The ensembles leave very little systematic error to correct at 0–3 weeks.

## 4. Brief items that could not be done (and why)

* **(b) Signals.** FluNet exists in the atlas only as a final vintage, so using it would bring look-ahead (QA-PRED X9);
  it is excluded from candidates. ERVISS (Europe) has no plausible signal for US states at 0–3 weeks. GDELT health themes:
  no GDELT table is loaded. CCKP/CRU/ERA5 monthly temperatures do not reach the forecast dates. School calendars: no
  open source is loaded. Southern-hemisphere season lead: only two seasons, so it cannot be tested.
* **ECDC targets.** The RespiCast ensemble is the official benchmark for ERVISS. Its model output cannot be listed
  without the GitHub API (blocked; V6 §2), and disk is below 5 GB. **FluNet**: no official forecaster exists. No
  "beat" test is possible for either.
* **Prospective sealing.** The pre-registered dev admission rule admitted no candidate, so nothing was sealed and the
  flock was not needed. H1 will be evaluated by recomputation: the holdout CLI refuses to run if `beat.py` changed or
  before 2027-07-03. Inputs are the archived ensemble files and as-of NHSN vintages.

## 5. Queued (exact commands)

```bash
# weekly through 2027-06 (needed by H1; disk >= 5 GB)
for s in atlas.cdc_flusight atlas.cdc_covidhub; do flock /tmp/reva-devdb.lock uv run reva atlas ingest $s; done
# H2 (FluSight 2023-24): lower FIRST_REFERENCE_DATE in src/reva/adapters/connectors/atlas_cdc_resp_hubs.py to
# 2023-10-01 (~25 MB raw), re-ingest atlas.cdc_flusight; if the hub has no archived vintages for 2023-24, H2 is not
# run and its 2 tests count as p = 1 (no final-vintage substitute).
# single look, on or after 2027-07-03 (read-only DB; writes registry/forecasts/beat_health/holdout_result.json)
uv run python -m reva.cli.beat_health holdout
```

## 6. Files, tests

`registry/forecasts/beat_health/{protocol.yaml,dev_result.json}`, `src/reva/evaluation/health_registry/beat.py` (pure,
frozen), `src/reva/cli/beat_health.py`, `tests/unit/test_beat_health.py` (8 tests: vectorised WIS = registry WIS, Holm
with missing tests, past-only as-of truth, prior until 200 past tasks, first-release truth, win rule, protocol pins the
module sha and one 6-test family, holdout refused before its date). Ruff and mypy are clean. No DB writes, no
ingest, no new raw bytes.

## 7. Owner decisions

1. CDC hubs' attribution request (V6) before publishing any ensemble-vs-REVA comparison. 2. Disk space for the H2
ingest. 3. Whether to add an honest "REVA does not beat the CDC ensembles" line to the public scoreboard now. The
dev evidence already supports it, and it would stay unchanged unless H1 says otherwise.
