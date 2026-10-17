# V7 BEAT-MACRO — same-date nowcasts and combinations vs the IMF WEO and the ECB SPF

Agent BEAT-MACRO, 2026-10-17. Binding protocol: `docs/roadmap/v7_beat_official.md`; standard:
`docs/dev/review_v6_predictions.md`. Pre-registration: `registry/forecasts/beat_macro/protocol.yaml`.
Not financial advice.

## 1. Result in one paragraph

On **same-date** comparisons (on the 1st of a month t, REVA's forecast vs the latest official forecast published by
t), with a protocol committed before any window was opened, a mechanical development stage (origins before 2019)
that froze **7 hypotheses**, and a holdout of **information dates V6 never evaluated** (1st of every month except
June/December 2019–2025 for the WEO; quarter starts 2019–2025 for the ECB SPF, never used before) opened **once**
with one Holm over the 7: **1 hypothesis is confirmed** — *REVA's combination of the IMF WEO unemployment projection
with REVA's nowcast from the OECD MEI real-time monthly unemployment rates beats the WEO alone on the same date*
for the G7 current year (AE ×0.630, MAE 0.405 vs 0.644 pp, p = 0.0044, Holm threshold 0.0083; 490 pairs, 70
origins; better in every year 2019–2025, ×0.60 excluding 2020–21). **This combination uses the IMF forecast as an
input.** Everything else failed the pre-registered test: the WEO-CPI combination for ~160 countries (AE ×0.836,
p = 0.020) was not significant after Holm, and its density beat the fair error fan (CRPS ×0.832, Holm-rejected) but
under-covered (80 % interval covered 62 %); the G7 CPI combination (×0.97), the G7 unemployment densities (cover
56–67 %) and the pure-REVA CPI density (×0.90) failed. **No pure-REVA model (one that does not use the official
forecast) passed any test**; for GDP (G7, real-time) the pure nowcast is ×1.86 worse than the WEO and the
combination is equal to it (×0.99); against the ECB SPF nothing reached development significance. The confirmed
combination was sealed prospectively in the registry for the 7 G7 questions `weo.unemployment` 2026
(batch `43d9d1bf…`). The attributed oil pass-through made every CPI nowcast worse.

## 2. What was built

| Part | File |
|---|---|
| Lagged final-vintage monthly inputs (IMF CPI, Eurostat HICP, EIA Brent) with publication-lag rules and a look-ahead assertion | `src/reva/evaluation/beat_macro/inputs.py` |
| Same-date cases: `weo_cpi` (panel), `g7rt` (OECD MEI real-time editions), `ecb_hicp`; bridges (CPI index / y/y, GDP q/q, unemployment) | `src/reva/evaluation/beat_macro/cases.py` |
| Second pass: stacking with the official forecast (weights from past errors, shrunk to ½), conformal densities, **fair error-fan benchmark** | `src/reva/evaluation/beat_macro/combine.py` |
| Development stage, mechanical hypothesis selection, holdout opened once (ledger, code digest), descriptives | `src/reva/evaluation/beat_macro/service.py` |
| Prospective sealing of confirmed scopes (supplementary registry batch) | `src/reva/evaluation/beat_macro/seal.py` |
| CLI `dev` / `holdout` / `issue` | `src/reva/cli/beat_macro.py` |
| Tests (bridges exact on synthetic truth, lag rule, stack uses only published errors, later outcomes never change earlier forecasts, holdout once, seal scope + attribution) — 9 | `tests/unit/forecasting/test_beat_macro.py` |
| Evidence | `registry/forecasts/beat_macro/{protocol.yaml, development.json, hypotheses.yaml, holdout.json, holdout_ledger.yaml, sealed.yaml}` |
| Model entry | `registry/models/methods.yaml` (`forecast.bm_stack@1.0.0`, seeded) |

No download, no new raw archive (0 MB), no migration, no new dependency. Inputs: the vintage tables already in the dev
DB (2,233,400 cells) and today's `atlas.imf.cpi_m` (sha `31a4f255…`), `atlas.eurostat.hicp_m` (`ed13ff43…`),
`atlas.eia.spot_d` (`cc75e5b1…`), pinned in every manifest.

## 3. Process (order of events, all on `claude/kind-darwin-kyy8re`)

| UTC 2026-10-17 | Event | Commit |
|---|---|---|
| 21:58:05 | `protocol.yaml` committed (design, candidates, benchmarks, selection and confirmation rules) — nothing built or scored yet | `652bcf0` (+ `d0bb1af` YAML quoting fix, content unchanged) |
| 22:00:58 | code + tests committed (smoke-tested on 2009–2010 development origins only) | `9972c31` |
| 22:03:15 | development stage (origins < 2019) → `development.json`, `hypotheses.yaml` (7, written by the rule) | committed in `463d316` (22:03:25, lead's snapshot) |
| 22:03:43 | holdout opened once (CLI refuses unless `hypotheses.yaml` is committed and unchanged; ledger keyed on its sha256 `47d4f51d…`, code sha256 `a6bd813b…`) | `7c6cb67` |
| ≈22:25 | confirmed scope sealed in the registry (supplementary batch) | `82ac033` |

## 4. Design (as pre-registered)

* **Same-date rule.** Origin t = 00:00 UTC on the 1st of a month. WEO in force under PRED-VINTAGE's rules (DBnomics
  vintages available the 1st of the month after release, IMF portal vintages at their publication instant): at
  1st-of-month origins this equals the true WEO in force (April/October releases are never on the 1st), except
  the September 2011 WEO (missing; development only). ECB SPF: round in force at quarter starts (the conservative
  "1st of next quarter" rule equals the true publication order there).
* **GDP fairness.** WEO Updates (January/July, GDP for ~30 economies incl. the G7) are not in REVA's vintages, so G7 GDP
  is compared only at January, May, July, November origins, excluding July 2011 and July 2020 (Updates published in
  June; dates from the IMF archive titles, not machine-verified). CPI and unemployment country projections appear only
  in full WEO releases.
* **Targets** = calendar year of the origin. Outcomes: WEO tasks — first WEO vintage on/after 1 October of the next
  year (same-institution, QA-PRED X7); ECB task — Eurostat HICP annual average. Inflation scored on
  `z = 100 ln(1+π/100)` (X6).
* **Candidates.** `bm_bridge` (pure REVA: published months + persistence of the last y/y rate / last level / median
  q/q), `bm_bridge_oil` (+ attributed oil pass-through, 0.04 pp per pp of oil inflation, Choi et al. 2017, IMF
  WP/17/196, `registry/effects/literature/choi_…_2017.yaml`), `bm_stack`/`bm_stack_oil` (**official forecast as an
  input**: official + λ(bridge − official), λ = clipped LS on past errors of the same bucket = variable × origin
  month, last 10 past origins, shrunk toward ½ with a 3-origin prior). Densities: point + past normalised errors.
* **Benchmarks.** Official point (AE). **Fair density**: N(official point, series scale × RMS of the official
  forecaster's own past normalised errors, same bucket and window) — not V6's persistence-spread strawman (X1).
* **Tests.** Per-origin mean loss differential, DM-HLN one-sided, horizon 12 for monthly origins (rectangular lag 11
  spans origins sharing a target year), 4 for quarterly/GDP; one Holm over all development comparisons; holdout:
  one Holm over the 7 hypotheses; densities must also cover 70–90 % at 80 %.
* **Inputs and labels.** Vintage families via `VintageAccessor` + `LeakageGuard`. Final-vintage monthly series with a
  publication-lag rule (`final_vintage_with_publication_lag`): IMF CPI month m usable from the 1st of
  m + max(3, gap+2) (gap = today's reporting delay of the country; 171 countries, lags 3–14 months), Eurostat HICP
  m + 2, Brent m + 1. The single confirmed result uses **no** final-vintage input (MEI editions + WEO vintages only).

## 5. Development stage (origins before 2019; Holm over 32 comparisons)

| task | variable | candidate | vs | loss | pairs / origins | rel. | p | Holm |
|---|---|---|---|---|---|---|---|---|
| weo_cpi | CPI | bridge | WEO point | AE | 14,459 / 128 | 0.987 | 0.55 | — |
| weo_cpi | CPI | bridge | fair fan | CRPS | 10,296 / 80 | 0.684 | 0.00028 | ✓ |
| weo_cpi | CPI | stack | WEO point | AE | 10,300 / 80 | 0.826 | 1e-25 | ✓ |
| weo_cpi | CPI | stack | fair fan | CRPS | 4,946 / 34 | 0.688 | 2e-40 | ✓ |
| weo_cpi | CPI | bridge_oil | WEO point | AE | 14,459 / 128 | 1.057 | 0.72 | — |
| weo_cpi | CPI | stack_oil | WEO point / fan | AE / CRPS | | 0.848 / 0.701 | 5e-8 / 2e-15 | ✓ / ✓ (not best) |
| g7rt | CPI | bridge / bridge_oil | WEO point | AE | 896 / 128 | 1.295 / 1.448 | 0.93 / 0.95 | — |
| g7rt | CPI | stack | WEO point | AE | 560 / 80 | 0.878 | 1.8e-6 | ✓ |
| g7rt | CPI | stack | fair fan | CRPS | 238 / 34 | 1.141 | 0.88 | — |
| g7rt | GDP | bridge | WEO point | AE | 294 / 42 | 1.960 | 0.99 | — |
| g7rt | GDP | stack | WEO point / fan | AE / CRPS | 189 / 27, 84 / 12 | 1.049 / 0.926 | 0.81 / 0.07 | — |
| g7rt | unemployment | bridge | WEO point | AE | 896 / 128 | 0.745 | 0.0079 | — |
| g7rt | unemployment | bridge | fair fan | CRPS | 560 / 80 | 0.807 | 0.00065 | ✓ |
| g7rt | unemployment | stack | WEO point | AE | 560 / 80 | 0.809 | 7e-10 | ✓ |
| g7rt | unemployment | stack | fair fan | CRPS | 238 / 34 | 0.451 | 6e-10 | ✓ |
| ecb_hicp | HICP | bridge / stack | ECB SPF point | AE | 76 / 76, 43 / 43 | 0.811 / 0.806 | 0.06 / 0.045 | — |
| ecb_hicp | HICP | all | fair fan | CRPS | | 0.89–1.39 | ≥ 0.26 | — |

The remaining rows are in `development.json`. Selection rule → the 7 hypotheses of §6.

## 6. Holdout (opened once; one Holm, m = 7; α = 0.05)

| # | scope | candidate (uses official?) | vs | loss | pairs / origins | candidate / benchmark | rel. | p | Holm | cov80 | confirmed |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | G7 unemployment, cy | `bm_stack` (**yes, WEO**) | WEO point | AE | 490 / 70 | 0.405 / 0.644 | **0.630** | 0.0044 | ✓ | — | **yes** |
| 2 | WEO CPI panel, cy | `bm_stack` (yes) | fair fan | CRPS | 10,425 / 70 | 1.434 / 1.724 | 0.832 | 0.0038 | ✓ | **0.62** | no (under-covers) |
| 3 | WEO CPI panel, cy | `bm_stack` (yes) | WEO point | AE | 10,425 / 70 | 1.692 / 2.025 | 0.836 | 0.020 | — | — | no |
| 4 | G7 unemployment | `bm_bridge` (no) | fair fan | CRPS | 490 / 70 | 0.356 / 0.531 | 0.670 | 0.020 | — | 0.67 | no |
| 5 | G7 unemployment | `bm_stack` (yes) | fair fan | CRPS | 490 / 70 | 0.343 / 0.531 | 0.645 | 0.023 | — | 0.56 | no |
| 6 | WEO CPI panel | `bm_bridge` (no) | fair fan | CRPS | 10,425 / 70 | 1.554 / 1.724 | 0.902 | 0.17 | — | 0.62 | no |
| 7 | G7 CPI | `bm_stack` (yes) | WEO point | AE | 460 / 70 | 0.904 / 0.931 | 0.970 | 0.39 | — | — | no |

Units: AE/CRPS in pp (unemployment) or log-pp (CPI, z scale). Holdout scope: 13,129 WEO-CPI cases (≈160
countries pass the consistency rule; 70 origins), 1,169 G7 cases, 28 ECB SPF origins.

**Robustness of the confirmed result (descriptive, not tests of new hypotheses).** Per target year, stack/WEO AE
ratio: 2019 0.65, 2020 0.70, 2021 0.59, 2022 0.61, 2023 0.33, 2024 0.83, 2025 0.68; excluding 2020–21: ×0.600
(p = 0.0036, 50 origins). By origin month: 0.33 (Sep/Oct, MEI August edition vs stale April WEO) to 0.82 (May, no
information edge); all 7 countries better (per-country median ×0.57). Why it works: WEO unemployment projections are
stale for 6 months while monthly unemployment rates arrive with a few weeks' lag and annual means are persistent.

## 7. Descriptive holdout results (every comparison; none is a claim)

| task / variable | REVA model | vs official point (AE) | vs fair fan (CRPS) |
|---|---|---|---|
| WEO CPI panel | bridge (pure) | 0.925 (p 0.28) | 0.902 (p 0.17) |
| | bridge_oil (pure + oil) | 1.087 | 1.053 |
| | stack_oil | 0.873 (p 0.009) | 0.869 (p 0.024) |
| G7 CPI (real-time) | bridge / bridge_oil | 1.134 / 1.426 | 1.141 / 1.415 |
| | stack / stack_oil | 0.970 / 0.960 | 1.001 / 1.002 |
| G7 GDP (real-time, fair months) | bridge | **1.857** | 1.833 |
| | stack | 0.992 (p 0.40) | 1.001 |
| G7 unemployment | bridge (pure) | **0.649 (p 0.0036)** — not a hypothesis (dev AE p 0.0079 was not Holm-significant) | 0.670 |
| ECB SPF HICP (EA) | bridge / stack | 1.030 / 0.958 | 0.997 / 0.917 |
| | bridge_oil / stack_oil | 1.098 / 0.968 | 1.056 / 0.939 |

WEO CPI by origin month (stack/WEO AE): Jan 0.96, Feb 0.92, Mar 0.89, Apr 0.84, May 0.96, Jul 0.85, Aug 0.76, Sep 0.66,
Oct 0.57, Nov 0.81 — the gain grows with the number of months published after the WEO; 2025 targets: ×1.16 (worse).
Per country: stack better than the WEO in 77 % of 163 countries (median ratio 0.87); pure bridge 52 % (0.99).
Coverage of the fair fan itself: 0.81 (CPI panel), 0.78 (G7 unemployment), 0.68 (ECB) — the fair benchmark is
reasonably calibrated, unlike V6's strawman (0.93–1.00).

## 8. Honest scoreboard

**Wins (pre-registered, holdout, Holm over the family of 7)**

| What | Against | Size | Wording |
|---|---|---|---|
| G7 current-year unemployment: WEO projection combined with REVA's nowcast from OECD MEI real-time monthly rates | the IMF WEO in force on the same date | AE −37 % (MAE 0.41 vs 0.64 pp), 70 origins 2019–2025 | "REVA's combination of the IMF WEO with REVA's monthly nowcast beats the WEO alone on the same date (it uses the WEO as an input; the gain comes from months published after the WEO)." Its interval is too narrow (56 % at 80 %). |

**Losses / no evidence**

| What | Result |
|---|---|
| Any pure REVA model vs the WEO or the ECB SPF on points | none confirmed; G7 GDP nowcast ×1.86 worse, G7 CPI ×1.13, panel CPI ×0.93 (n.s.), ECB HICP ×1.03 |
| WEO-CPI combination (≈160 countries) on points | ×0.836, p 0.020 — **not** significant after Holm |
| Densities vs the fair error fan | Holm-rejected only for the WEO-CPI combination (×0.832) but it covers 62 % → not confirmed; G7 unemployment densities ×0.65–0.67 cover 56–67 % → not confirmed |
| Attributed oil pass-through (structural prior) | worsens every CPI nowcast (panel ×1.09, G7 ×1.43, ECB ×1.10 vs official) |
| ECB SPF (euro-area HICP) | no development hypothesis; holdout stack ×0.96 AE (n.s., 28 origins) |
| G7 GDP combination | ×0.99 — equal to the WEO |
| US SPF | not tested (no between-release real-time data, see §10) |

## 9. Prospective registry

Sealed (dev DB, origin live, cycle 2026-Q4, 2026-10-17): supplementary batch
`43d9d1bf046aafaf63cc6253738d1dd40a5210539388ea14b3c92257a2cd8944` (supplement of batch `96c6ad48…`), model
`forecast.bm_stack@1.0.0`, 7 questions `weo.unemployment` 2026 (h0). Inputs at issue: WEO 2026-10 in force
(attributed input), MEI edition 202608, past errors of the November bucket (the information state at issue = a
fresh October WEO + the August MEI edition; asserted that no outcome was published between issue and 1 November).
λ = 0.62. Points (80 % interval): USA 4.22 [3.88, 4.32], JPN 2.54 [2.21, 2.65], DEU 3.92 [3.59, 4.03], FRA 8.27
[7.94, 8.38], GBR 5.06 [4.73, 5.17], ITA 5.59 [5.26, 5.70], CAN 6.56 [6.23, 6.67] (WEO points 4.16, 2.53, 3.97, 8.42,
5.28, 5.61, 6.55). Resolution by the registry rule (first WEO vintage with 2026 as an actual, ≈ April 2027; the
holdout used the October-of-next-year vintage). Nothing else was sealed (no other scope confirmed). Note: at the
time of sealing the October WEO is fresh, so the expected prospective gain is the small November-bucket one (×0.64 on
the holdout); the larger gains come at later monthly origins, which need monthly issuance (§11).

## 10. Honesty notes and limits

* **Uses the IMF forecast.** The only win is a combination; its point is the WEO moved toward REVA's nowcast. It must
  never be quoted as "REVA beats the IMF" without that sentence.
* **Timeliness, not superior modelling.** The gain comes from data published after the WEO (6-month release cycle).
  At May origins (WEO just published) the combination gains little (×0.82); against a monthly-updated official
  forecast the edge would shrink.
* **Same-institution outcome** (X7): outcomes are the WEO's own October-of-next-year values; for the G7 these are
  national statistics. For unemployment, REVA's bridge adds the WEO-vs-MEI level gap of the last observed year
  (national vs harmonised definitions).
* **Targets overlap V6's holdout targets** (2019–2025); the information dates and the nowcasts are new, but the size
  of WEO errors in these years was known from V6 reports when this protocol was written.
* **Final-vintage CPI** (panel and ECB) relies on the non-revision of headline CPI/HICP under a lag rule. On the G7,
  the same bridge built from today's IMF CPI had MAE 0.99 vs 1.06 from the real-time MEI editions (holdout; 0.45 vs
  0.57 in development) — the gap mixes revisions with more published months under the monthly lag rule than under
  quarterly MEI editions; the CPI-panel results (none confirmed) may be slightly flattered. The confirmed result
  uses no final-vintage input.
* **Densities under-cover** on the holdout (56–67 % at 80 %): 2020–2023 errors exceed the past window's; any interval
  from `bm_stack` is too narrow.
* **WEO Update dates** used to exclude July 2011 / July 2020 GDP origins are from IMF archive titles, not verified
  against a fetched source (imf.org blocks this client).
* Licences: IMF WEO/CPI (IMF terms, attribution, not open); OECD MEI (CC BY 4.0); Eurostat (attribution); ECB SPF
  (attribution); EIA (public domain). The sealed forecasts carry the IMF attribution of their input.

## 11. Not done, and why

* **US SPF**: ALFRED blocked; RTDSM and MEI editions are quarterly, so between SPF releases REVA has no information
  the panellists lacked — no fair timeliness test exists with REVA's data (declared in the protocol).
* **WEO bias correction (next year)**: a WEO-only transform is constant between releases, so between-release origins
  would re-score V6's spent holdout — prospective only.
* **Gradient boosting / ridge with spillover features from trade/IO links**: not built (no dependency; the cross-
  country element is the pooled stacking weight; the one structural prior tried — oil pass-through — failed).
* **Broad GDP/unemployment panels**: only final vintages (revised) beyond the G7 — ineligible.
* **Monthly issuance**: the registry issues per quarterly cycle; the stack's largest gains are at August–October
  origins, so a monthly supplementary issuance job is needed to realise them prospectively.

## 12. Owner decisions

1. Add a monthly issuance hook for `forecast.bm_stack` (G7 unemployment) on the 1st of each month (supplementary
   batches on the open `weo.unemployment` h0 questions), so prospective evidence covers the months where it gains.
2. FRED/ALFRED API key — the only route to between-release real-time data for a US SPF test.
3. Archive WEO Update vintages (January/July) — needed for fair GDP comparisons at all months.
4. Whether the under-covering `bm_stack` interval may be shown, or only its point (recommended: point only, with the
   "uses the IMF forecast" wording).

## 13. Commands

```bash
uv run pytest -q tests/unit/forecasting/test_beat_macro.py
uv run python -m reva.cli.beat_macro dev       # development stage -> development.json + hypotheses.yaml (done; commit)
uv run python -m reva.cli.beat_macro holdout   # opens the holdout ONCE (done; ledger refuses a second opening)
uv run python -m reva.cli.beat_macro issue --dry-run
flock /tmp/reva-devdb.lock uv run reva seed
flock /tmp/reva-devdb.lock uv run python -m reva.cli.beat_macro issue --seal   # October only (bucket mapping)
```
