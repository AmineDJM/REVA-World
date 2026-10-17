# V6 PRED-FIN — finance prediction, done honestly

Agent PRED-FIN, 2026-10-17. Plan: `docs/roadmap/v6_plan.md` (PRED-FIN row); method: `docs/strategy/world_map.md` §5
(skill must be *proved* against strong benchmarks on sealed, prospective questions). Inputs: PRED-HF daily series
(`docs/dev/v5_pred_hf.md`) and the PRED-REGISTRY machinery (`docs/dev/v5_pred_registry.md`).

> **Disclaimer (carried by every finance question, forecast record and back-test manifest).** Research forecasts
> for evaluation only: not investment advice, not a recommendation to buy, sell or hold any asset, not a trading
> signal. Exchange rates, bond yields and oil prices are close to unpredictable beyond a random walk; every REVA
> finance forecast is scored against one. (`reva.models.forecasting.finance_models.DISCLAIMER` / `DISCLAIMER_FR`.)

## 1. Result in one paragraph

REVA now asks and seals finance questions — FX month-end levels and direction at 1/3/12 months (EUR against USD,
GBP, JPY, CHF, CNY), the US 10-year Treasury par yield, the 2s10s spread, binary "hike / cut at the next scheduled
meeting" questions for the FOMC, the ECB Governing Council and the Bank of England MPC, and Brent monthly averages
— each with a declared resolution source and rule. Benchmarks are the random walk with historical-volatility
intervals, a CIP/UIP forward *proxy* (market forwards are licensed), the expectations-hypothesis forward from the
Treasury curve, and base-rate / momentum probabilities for meetings. Four candidate models (GARCH(1,1), dynamic
Nelson-Siegel, PPP mean reversion, Taylor-rule logit) were evaluated on a rolling pseudo-real-time back-test
(2000-2026, 38,249 level replays + 5,317 meeting replays) with CRPS/Brier and Diebold-Mariano/HLN tests, Holm
corrected. **Plainly: no candidate model significantly beats the benchmarks on any (family, horizon) cell, so no
candidate forecast is sealed.** This is the expected result — beating the random walk on FX and yields out of sample
is rarely achieved (Meese & Rogoff 1983; Rossi 2013; Duffee 2013) — and REVA's numbers reproduce it: the best point
models are within ±2 % of the random walk's CRPS, and the structural ones (PPP, Nelson-Siegel) are *worse*. What is
forecastable is what the literature says: volatility (GARCH tightens 12-month EUR/USD intervals, significant vs the
random walk but not vs the forward proxy), the slope of the yield curve (the EH forward of the 2s10s spread beats the
random walk at 1 month), and the persistence of rate cycles (momentum beats the base rate for BoE and ECB hikes).

## 2. What was built

| Piece | File |
|---|---|
| Benchmarks and candidates (pure numerics) | `src/reva/models/forecasting/finance_models.py` |
| Registry finance layer: daily inputs as published at the origin, forecasters, frozen records | `src/reva/evaluation/registry/finance.py` |
| Registry extensions: value rules `month_end_of_daily`, `month_end_spread_of_daily`, `meeting_change`; threshold rule `above_fixed`; `finance` block; meeting question generator; meeting resolution; probability-only scores; input + calendar tables pinned in the seal | `src/reva/evaluation/registry/{specs,questions,service,scores,forecasts}.py` |
| Question families (13 new + finance blocks on 4 existing daily families) | `registry/forecasts/question_specs.yaml` |
| Rolling pseudo-real-time back-test, DM-HLN + Holm, winners rule | `src/reva/evaluation/finance_backtest.py`, CLI `python -m reva.cli.fin_backtest` |
| Central-bank meeting calendars connector (FOMC, ECB GC, BoE MPC) | `src/reva/adapters/connectors/atlas_cb_calendars.py` (`atlas.cb_calendars`, table `cbcal.meetings`, source `cbcal.calendars`) |
| Source registry, budget | `registry/sources/cbcal_calendars.yaml`, `registry/atlas/budget_requests/pred_fin.yaml` (merged in budgets-1.3.31) |
| Models (9, seeded) | `registry/models/methods.yaml` (`fin.*`) |
| Tests | `tests/unit/forecasting/test_finance_models.py` (10), `tests/unit/test_finance_registry.py` (5), `tests/unit/test_finance_backtest.py` (3), `tests/unit/test_atlas_cb_calendars.py` (4) |
| Fixtures (verbatim slices of the real pages, 2026-10-17, licence per file) | `tests/fixtures/source/atlas.cb_calendars/` (76 KB) |
| Back-test result | `docs/dev/v6_pred_fin_backtest.json` |

No migration, no KG change, no trading or portfolio logic anywhere.

## 3. Question families (`registry/forecasts/question_specs.yaml`)

| Family | Variable (source, rule) | Questions | Resolution |
|---|---|---|---|
| `fin.fx_eur{usd,gbp,jpy,chf,cny}_month_end` | ECB euro reference rate, quote currency per EUR, **last published day of the month** (`atlas.ecb.fx_ref`, `month_end_of_daily`) | quantity, 80 % interval, binary "strictly above the latest published month-end value" at 1, 3, 12 months | first `ecb.fx_ref` version after the seal that also publishes a later day; TARGET closing days have no rate (the last published day counts) |
| `fin.ust_10y_month_end` | US Treasury par yield, "10 Yr" (`atlas.ust.par_yield`) | same | same rule on `ust.par_yield` |
| `fin.ust_2s10s_month_end` | 10 Yr − 2 Yr on the last day both are published (`month_end_spread_of_daily`) | same | same; both source cells cited |
| `fin.{fomc,ecb_dfr,boe_bank_rate}_{hike,cut}` | policy rate in force on **decision day + 7 days** minus the level in force at issue (FOMC: NY Fed `targetRateTo`; ECB: DFR "date of changes" level; BoE: `IUDBEDR`) — `meeting_change` | binary "move in the asked direction > 0" at the next 1 and 2 scheduled meetings of the published calendar | rate table version after the seal that publishes a day on or after the level day; decision days from `cbcal.meetings` (cited in each question: calendar text, raw sha256, selector) |
| `eia.brent_spot_monthly_mean` (existing) | Brent monthly mean of daily EIA spot | existing 1/3/6/12 questions; finance block adds `fin.rw_histvol` | unchanged |
| `ecb.eur{usd,cny,jpy}_monthly_mean` (existing) | monthly mean of ECB reference rates | existing; finance block adds `fin.rw_histvol`, `fin.fx_forward_cip` | unchanged |

Why "+ 7 days": ECB rate changes take effect the following Wednesday; the Fed's the next day; the BoE's the same
day. An unscheduled move between issue and the level day counts (stated in each question's definition).
Every finance question carries the bilingual `disclaimer`. Generic registry baselines (persistence, AR, seasonal
naive, historical quantiles) still run on the monthly level families for comparability; meeting families run only
the meeting benchmarks (a Gaussian random walk is meaningless for a step-function rate).

## 4. Benchmarks — and which market-implied ones are *not* open

| Benchmark | Families | Notes |
|---|---|---|
| `fin.rw_histvol` random walk / martingale | all level families | point = latest published daily value (mid-month data used, unlike the monthly persistence baseline); variance = mean squared daily change over the last 250 published days, aggregated **exactly** over the target's weekdays (month end or monthly mean); log-normal (FX, oil; 199-point quantile grid) or Gaussian (yields) |
| `fin.fx_forward_cip` | FX | **proxy**: expected log change = (policy rate quote − policy rate euro) × time, BIS daily policy rates lagged 7 days (weekly bulk files) |
| `fin.ust_eh_forward` | 10y, 2s10s | forward yield from the origin day's par curve, par ≈ zero-coupon, linear interpolation (approximation, stated) |
| `fin.meeting_base_rate`, `fin.meeting_momentum` | meetings | share of month-end-started windows of the same length (last 25 years, ending by the origin) with a move up/down; momentum conditions on the last move in the previous 365 days; rule of succession |

**Not available openly (documented, not worked around):** FX forward points and NDFs (Refinitiv/LSEG, Bloomberg);
fed-funds, SOFR, €STR and SONIA futures/OIS (CME, ICE, broker data) — so no market-implied policy path;
Brent and WTI futures (ICE, CME/NYMEX; EIA republishes NYMEX contracts but they are exchange-licensed, PRED-HF
did not take them); Treasury forward rates are *derived* from the open par curve (done). The EIA STEO Brent
projection is a strong open reference but is PRED-ENERGY's benchmark (vintage back-test in
`src/reva/evaluation/energy_backtests.py`).

## 5. Candidates and the back-test

Rolling pseudo-real-time back-test (`reva.evaluation.finance_backtest`): month-end origins 2000-01 … 2026-09, every
forecast fitted only on data dated at or before the origin (inputs with their publication lags: BIS rates 7 days,
euro-area HICP 20 days, IMF CPI 45 days); targets = the family's own target 1/3/12 months ahead (1/3/6/12 for the
existing monthly-mean families); meetings: P(rate in force 42 days later moved up/down), monthly origins.
**Final vintage** (today's table versions; daily market data are rarely revised) — labelled
`final-vintage pseudo-real-time`, never real-time skill. Primary metric CRPS (levels) / Brier (meetings);
DM-HLN one-sided on the time series of loss differentials (lag h−1; 1 for the 42-day windows), Holm per model and
benchmark across (family, horizon) cells at 5 %. **Winner rule:** a candidate wins a cell only if it is
significantly better than *every* benchmark scored on that cell (intersection-union). Only winners may join a
family's sealed `forecasters`.

Honesty note on the rule: a first 3-family trial used "better than the random walk only"; under that rule GARCH
EUR/USD at 12 months (and, in the trial, Taylor FOMC-cut) would pass. The rule was tightened to
"better than every benchmark" *before* the full run was used for any decision, because a candidate that only beats
the weakest benchmark has not shown skill. Under the looser rule the full run admits exactly two cells (GARCH,
EUR/USD month-end and monthly-mean, h = 12); under the adopted rule none.

### 5.1 Levels: CRPS ratio to the random walk (< 1 = better; * = Holm-significant)

| Family | h | forward / EH | GARCH(1,1) | PPP | Nelson-Siegel |
|---|---|---|---|---|---|
| EUR/USD month end | 1 / 3 / 12 | 1.001 / 1.002 / 0.993 | 0.994 / 0.997 / **0.985*** | 1.019 / 1.046 / 1.016 | — |
| EUR/GBP month end | 1 / 3 / 12 | 0.999 / 0.995 / 0.976 | 0.996 / 1.002 / 0.990 | 1.001 / 1.002 / 0.978 | — |
| EUR/JPY month end | 1 / 3 / 12 | 1.008 / 1.022 / 1.050 | 0.996 / 1.002 / 1.007 | 1.046 (worse*) / 1.127 / 1.355 | — |
| EUR/CHF month end | 1 / 3 / 12 | 1.003 / 1.005 / 1.001 | 0.973 / 0.987 / 0.986 | 1.000 / 1.007 / 1.008 | — |
| EUR/CNY month end | 1 / 3 / 12 | 1.022 / 1.062 / 1.213 | 0.990 / 1.001 / 0.995 | 1.020 / 1.051 / 1.273 | — |
| UST 10y month end | 1 / 3 / 12 | 1.007 / 1.017 / 1.056 | 0.999 / 0.999 / 0.981 | — | 1.024 / 1.053 / 1.145 |
| UST 2s10s month end | 1 / 3 / 12 | **0.976*** / 0.944 / 0.891 | 0.990 / 0.996 / 0.988 | — | 1.033 (worse*) / 1.072 (worse*) / 1.068 |
| Brent monthly mean | 1 / 3 / 6 / 12 | — | 0.983 / 0.979 / 0.992 / 0.996 | — | — |
| EUR/USD monthly mean | 1 / 3 / 6 / 12 | 1.001 / 1.001 / 0.999 / 0.993 | 0.996 / 0.993 / 0.988 / **0.985*** | 1.035 / 1.054 / 1.054 / 1.018 | — |

(n ≈ 300 origins per cell; PPP ≈ 200 because 120 months of published CPI are needed; CNY from 2005.)
Squared-error point tests: no model beats the random walk anywhere; PPP (EUR/JPY h=1) and Nelson-Siegel (2s10s
h=1) are significantly worse. Direction (Brier of "above the latest month-end"): every level model sits at
0.250 ± 0.001 on month-end families — a coin flip — except the EH forward of the 2s10s spread (0.953 and 0.884 of the
random walk's Brier at 1 and 3 months, Holm-significant): the curve slope mean-reverts as the forward predicts.
80 % interval coverage of the random walk: 0.75-0.87 (under-covers at 12 months on EUR/USD 0.75 and on 2s10s 0.66 —
volatility regimes); GARCH is closer to nominal on most FX cells (EUR/USD 12 m 0.78, EUR/CHF 12 m 0.80).

### 5.2 Meetings: Brier (event frequency in the 42-day windows), ratio to the base rate

| Family | event freq. | base rate | momentum | Taylor logit |
|---|---|---|---|---|
| FOMC hike | 0.17 | 0.146 | 0.113 (0.98) | 0.090 (0.62, p 0.016) |
| FOMC cut | 0.09 | 0.097 | 0.091 (0.87) | 0.075 (0.83, p 0.042) |
| ECB DFR hike | 0.11 | 0.107 | 0.087 (0.76) | 0.074 (0.70, p 0.023) |
| ECB DFR cut | 0.13 | 0.118 | 0.116 (0.99) | 0.109 (1.04) |
| BoE hike | 0.14 | 0.123 | 0.105 (0.82*) | 0.093 (0.75, p 0.012) |
| BoE cut | 0.14 | 0.127 | 0.118 (0.93) | 0.118 (0.93) |

The inflation-only Taylor logit has the lowest Brier on 5 of 6 families, but after Holm across the 6 cells none of
its p-values (smallest 0.012 vs threshold 0.0083) is significant against the base rate, and against momentum the
smallest is 0.036 — **not a winner**. Momentum (a benchmark) beats the base rate significantly for BoE hikes; on
the log score also for ECB hikes and FOMC cuts. Market-implied probabilities (fed-funds / €STR / SONIA futures) — the
benchmark professionals use and the one REVA would most likely lose to — are not open.

### 5.3 Pooled (informational, never used to admit a model)

Scale-free panel DM tests (loss differential scaled by the reference's mean loss per family, averaged per origin;
`pooled_informational` in the manifest). They are *not* part of the pre-declared winner rule:

| Pool (families) | Model | h | scaled CRPS/Brier diff. | one-sided p (better) |
|---|---|---|---|---|
| FX month end (5) | GARCH | 1 / 3 / 12 | −0.010 / −0.002 / −0.007 | 0.010 / 0.35 / 0.20 |
| FX month end (5) | forward proxy | 1 / 3 / 12 | +0.006 / +0.016 / +0.044 | worse (p_worse 0.003 / 0.009 / 0.021) |
| FX month end (5) | PPP | 1 / 3 / 12 | +0.015 / +0.040 / +0.092 | worse (p_worse 0.0003 / 0.003 / 0.08) |
| FX monthly mean (3) | forward proxy | 1-12 | +0.008 … +0.086 | worse (p_worse ≤ 0.0024) |
| Brent | GARCH | 1 / 3 | −0.017 / −0.021 | 0.013 / 0.035 |
| Meetings, hikes (3) | Taylor logit | — | −0.285 | 0.0025 |
| Meetings, hikes (3) | momentum | — | −0.134 | 0.025 |
| Meetings, cuts (3) | momentum / Taylor | — | −0.082 / −0.050 | 0.013 / 0.13 |

Reading: across currencies the carry/UIP forward is *worse* than the random walk (the forward-premium puzzle: high-rate
currencies do not depreciate as UIP says), PPP reversion is worse at 1-12 months, and the only consistent gains are in
the spread (GARCH) and in policy-cycle persistence. The pooled Taylor-logit result for hikes (−28 % Brier,
p = 0.0025) is the most promising lead; it is not admitted because the pre-declared rule is per family cell and
against every benchmark — it should be re-tested prospectively (sealed questions resolve from 2026-11).

## 6. What REVA issues (sealed)

See §9: the finance families' benchmark forecasts (and no candidate) sealed under `flock /tmp/reva-devdb.lock`.

## 7. Missing, and why

* **Market-implied benchmarks** (forwards, OIS, futures): licensed, not open (§4). Owner decision if a licence is
  wanted; REVA must then be compared against them before any claim.
* **Historical meeting calendars**: the archived pages list FOMC 2021-2027, ECB from 2026-10-29, BoE 2026-2027 only.
  The meeting back-test therefore uses fixed 42-day windows from month ends, not actual meeting dates (stated).
  The Fed's historical calendar pages (`fomchistorical*.htm`) and the ECB's past-meetings archive could replace this.
* **Output gap / unemployment** in the Taylor rule (inflation-only here; IMF LS UK series stops 2020).
* **Molodtsova-Papell Taylor-rule FX fundamentals** and Clark-West tests for nested models (DM is conservative for
  the random walk nested in the drift models; a Clark-West test would raise power for the forward/EH/PPP/NS models).
* **V6FINDATA tables** (central-bank balance sheets, COFER, euro-area AAA 10y curve, Fama-French factors,
  `registry/atlas/catalog/finance_v6.yaml`) landed as catalog entries; no families added yet — the euro-area AAA
  10-year spot yield is the natural next yield family with the same machinery (`month_end_of_daily`).
* **Vintages**: daily market data are final-vintage; no real-time revision study (rarely relevant here).

## 8. Owner decisions

1. **BoE calendar page licence**: the Bank's website content is licensed "for personal use or internal use within an
   individual organisation for non-commercial purposes" (Database terms do not cover the MPC dates page); BoE rows
   carry `flags.licence_restriction = non_commercial_internal`. Keep BoE meeting questions private, or ask the Bank.
2. **NY Fed Terms of Use notice** must be displayed with FOMC questions/outcomes (`nyfed.rates_d` resolution).
3. **EIA spot (Refinitiv source)** licence review stands for Brent questions (PRED-HF decision 1).
4. Publish finance questions publicly? The disclaimer is in every record; MAR/advice rules (world_map §5.7) argue
   for publishing only aggregate scores, never as signals.
5. Licence market-implied benchmarks (futures/OIS/forwards) — without them the meeting and FX evaluations miss the
   strongest reference.

## 9. Live runs (dev DB, 2026-10-17, writes under `flock /tmp/reva-devdb.lock`)

* `reva seed`: 9 `fin.*` model versions registered (candidate status).
* `reva atlas ingest atlas.cb_calendars` (free disk 5.4 GB): 6 requests, 624,345 bytes archived (budget
  `budgets-1.3.32`, `cbcal.calendars` 14/day), table `cbcal.meetings` **90 rows**: FOMC 56 decision days
  (2021-01-27 … 2027-12-08; one notation vote skipped), ECB Governing Council 18 (2026-10-29 … 2028-12-07; one
  unlabelled first day of a two-day meeting skipped), BoE MPC 16 (2026-02-05 … 2027-12-16); 3/3 licence quotes and
  4/4 page quotes found on the archived pages. Example: `POLICY_DECISION|body=FOMC` / `US` / 2026-10-28,
  value_text "2026 FOMC Meetings: October 27-28", flags.estimate_kind `announced_schedule`.
* Back-test recorded: manifest sha256 `c5a53a653cc4c1996d43caa277c12805dc420ac20906e8f9df91c7849bcdebf3`
  (`model.evaluation` rows `metrics.kind = finance_backtest`, replay mode `revised_data_retrospective`), copy in
  `docs/dev/v6_pred_fin_backtest.json`; **winners: none**.
* Sealed batch `6f19e6711db83adbef582e99643a37f3de7cf05b221530e66dc90a141c9fe2d1` (cycle 2026-Q4, issued
  2026-10-17 21:09:07 UTC): **160 questions, 764 forecasts** (17 abstentions: AR non-stationary fits) — FX month end
  5 × 9, UST 10y 9, 2s10s 9, policy meetings 12 (2 per family: FOMC 2026-10-28 / 12-09, ECB 2026-10-29 / 12-17, BoE
  2026-11-05 / 12-17), ECB monthly means 14, Brent monthly mean 11, and PRED-REGISTRY's pending `bis.policy_rate_daily`
  60. Finance forecasters issued: `fin.rw_histvol` 88, `fin.fx_forward_cip` 59, `fin.ust_eh_forward` 18,
  `fin.meeting_base_rate` 12, `fin.meeting_momentum` 12; candidates 0. Pinned tables: ecb.fx_ref, bis.cbpol_d,
  ust.par_yield, eia.spot_d, nyfed.rates_d, ecb.key_rates, boe.rates_d, cbcal.meetings.
* Examples: `fq_56efb10f91c45600d77fcf8c` "Will EUR/USD (month end) for 2026-11 be strictly above 1.1355 (latest
  month-end, 2026-09)?" — P(yes): random walk 0.318 (latest daily value 1.1253), forward proxy 0.353, monthly
  persistence 0.5. `fq_aeefdd807fd1fbd2bff396a8` "ECB DFR in force on 2026-11-05 (decision 2026-10-29) strictly
  above 2.5 %?" — base rate 0.063, momentum 0.217. `fq_fb30048ea8cf3dec3bc6b7b3` FOMC cut at the 2026-10-28
  meeting — base rate 0.056, momentum 0.018. `fq_416f768888d35e95b1390ee6` 2s10s above 0.41 in 2026-11 — random walk
  0.347, EH forward 0.276.
* Known wording defect in this sealed batch: the meeting question text prints the basis level with the family unit
  ("4.00 percentage points") — the level is in percent; the frozen text cannot change (fixed for later batches; the
  value and rule fields are unambiguous: `event.basis_value`, `resolution.basis`).
* Not run here (lead's job): `reva atlas snapshot`, `reva links materialise`.

## 10. Commands

```bash
uv run pytest -q tests/unit/forecasting/test_finance_models.py tests/unit/test_finance_registry.py \
  tests/unit/test_finance_backtest.py tests/unit/test_atlas_cb_calendars.py tests/unit/test_forecast_registry.py
flock /tmp/reva-devdb.lock uv run reva seed                                   # fin.* model versions
df -h /                                                                       # >= 5 GB free before an ingest
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.cb_calendars        # weekly
uv run python -m reva.cli.fin_backtest --out docs/dev/v6_pred_fin_backtest.json --record   # ~7 min, locks only to write
flock /tmp/reva-devdb.lock uv run reva forecasts issue --family fin.fx_eurusd_month_end ...  # see §9 for the list
```
A candidate joins a family's `finance.forecasters` only by an explicit spec edit citing the back-test sha256 and
the winning cells (none today). The admission is per family (all horizons); a per-horizon gate would be needed if a
candidate ever wins on some horizons only (not implemented: no winner).
