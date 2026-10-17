# V6 PRED-ENERGY — energy forecasts benchmarked against the EIA Short-Term Energy Outlook

Agent PRED-ENERGY, 2026-10-17. Plan: `docs/roadmap/v6_plan.md`. Builds on V4ENERGY (`atlas.eia_steo`, current STEO
bulk file: observed and forecast tables kept apart), PRED-VINTAGE (vintage conventions + accessor), PRED-REGISTRY
(sealed questions, proper scores).

## 1. Result in one paragraph

54 archived EIA STEO issues (quarterly, July 2013 – October 2026) are loaded live as an atlas **vintage table**
(`atlas.vintage_steo.steo_archive`, 664,612 cells, 241 series, ~49 MB raw) readable as-of through PRED-VINTAGE's
`VintageAccessor`. A real-time backtest (23,690 scored forecasts; inputs and EIA's forecasts as published at each
release, outcomes = EIA's current declared history) shows: **for Brent and WTI, EIA's STEO point forecasts are not
better than a random walk** (MAE ratio 0.95–1.05, no DM-HLN test significant at any horizon); STEO beats the random
walk significantly (unadjusted one-sided DM-HLN p < 0.05, no multiplicity correction) for **Henry Hub at 3–12 months** (MAE −17 to −24 %), **U.S. crude production at all horizons**
(−23 to −43 %) and **world liquids production at 3–12 months** (−17 to −31 %); OECD stocks −10 to −14 % (not
significant). No REVA price model (inventory gap, ARDL with fundamentals, combinations) beats the random walk after
Holm correction; ~~17~~ **4 (series, horizon) winners** exist after FIX7 (one Holm family over the whole run, 260
tests; the EIA-derived STEO+RW combination excluded — QA-PRED X3): `ar_diff` U.S. crude h2, world liquids h2–3,
`combo_models` world liquids h1 (CRPS ratios vs RW 0.83 / 0.91 / 0.89 / 0.92, but 80 % coverage only 0.61–0.68, and
all of them have a higher point error than EIA's STEO). The V5/V6 count of 17 used one Holm family per series and
counted 11 STEO+RW cells that are worse than STEO alone on points. These are in-sample admissions to the registry,
not evidence of skill. Sealed batch `6d76937c…` (2026-Q4): 122 questions, 719 issued
forecasts (generic baselines, `forecast.energy_rw`, 80 attributed EIA STEO points, 29 winner forecasts).

## 2. Sources and data

| connector | source key / licence | fetched (live 2026-10-17) | loaded |
|---|---|---|---|
| `atlas.vintage_steo` 1.0.0 (`src/reva/adapters/connectors/atlas_vintage_steo.py`) | `eia.steo_archive` (new, `registry/sources/eia_steo_archive.yaml`) — U.S. public domain | STEO Archives page `https://www.eia.gov/outlooks/steo/outlook.php` + 54 `archives/<mon><yy>_base.xlsx` (Jan/Apr/Jul/Oct, 2013-07 … 2026-10): 55 requests, ~49.4 MB | `vintage_steo.steo_archive`: 664,612 cells (488,887 history-as-published, 175,725 EIA projections), 241 series codes, 54 vintages |

* **Which files / why quarterly**: the archive lists 267 issues with data files since Aug 2004; files before July 2013
  are binary `.xls` (no reader in the project, no dependency added) and all 160 monthly `.xlsx` (~155 MB) exceed the
  100 MB raw cap → January/April/July/October issues (`ISSUE_MONTHS`). Sheets: `1tab`, `2tab` (prices), `3atab`
  (world production / consumption / inventories), `3dtab` (crude production by country), `4atab` (U.S. petroleum),
  `5atab` (U.S. natural gas).
* **Availability rule** (`vintage_common.AVAILABILITY_RULES`): `steo_archive_release_date` — the archive page's
  "Release Date" (verbatim, e.g. `10/06/2026`), `available_at` = next day 00:00 UTC, valid only if the file's
  `Last-Modified` header is on/before the release day (all 54 were); otherwise `steo_archive_file_last_modified`
  (day after Last-Modified); no header → no proof → never selected. EIA data-correction notices linked from an issue
  row are kept in `flags.eia_notice`.
* **Observations vs projections**: recent workbooks declare a workbook-wide "Last Historical Month" (`Dates`
  sheet); later months → `projection`; earlier → `published_history_boundary_unknown` (the boundary is per
  workbook, not per series: EIA's bulk file shows world production / OECD stocks with `lastHistoricalPeriod` 3
  months earlier, i.e. EIA estimates). Older workbooks (no declaration): months from the issue month on →
  `projection`. Identity: `variable = eia_steo:<CODE>` (EIA's code, upper-cased; verbatim in
  `provider_variable`), `provider_geo = not_stated` (the workbooks state none; the bulk STEO declares geography per
  code). Units from the row label / table heading / title (footnote markers `(a)` ignored; `unit_basis` flag).
  Text cells (`-`) are counted, never values.
* Shared files touched (small appends): `vintage_common.py` (family `eia_steo`, 2 rules, namespaced variables
  `eia_steo:*`), `vintages.py` (`VINTAGE_DATASET_KEYS`), budget request `registry/atlas/budget_requests/pred_energy.yaml`
  (merged, eia.steo_archive 70/day; digest re-pinned at the time), PRED-VINTAGE's tests updated for the new family.
* Fixtures `tests/fixtures/source/atlas.vintage_steo/` (146 KB: archive page reduced to 4 issue rows, Jul 2013 and
  Oct 2026 workbooks reduced to `Dates`/`2tab`/`3atab`, live sha256 in the manifest).

## 3. Backtest design (`src/reva/evaluation/energy_backtests.py`, CLI `python -m reva.cli.energy_backtest`)

* Origins = the 54 issues; at each origin's `available_at` a `VintageAccessor(available_then)` gives the information
  set (history as then published, older months from older issues) and the STEO projections of the issue in force.
  Horizon `h` = months after the issue month (1–12) = the registry's monthly cycle horizon for an issue made in a
  quarter's first month.
* Outcomes: latest `atlas.eia_steo.steo_observed` (declared history only) = final vintage of the target (labelled).
  Targets after EIA's last declared month are not scored (34 per price/U.S. series, 46 per world series).
* Forecasters (`src/reva/models/forecasting/energy_models.py`): `rw` (benchmark, futures-free: no futures curve is
  loaded in REVA), `steo` (EIA, attributed, point), prices: `inventory` (direct k-step regression on the OECD
  inventory gap vs its 5-year same-month mean), `ardl` (price momentum, OECD stock change, EIA implied world stock
  change); quantities: `drift`, `seasonal`, `ar_diff`; both: `combo_steo_rw`, `combo_models`.
* Scores: CRPS (closed forms Gaussian / log-normal), SE, AE, log score, PIT. Because a point forecast's CRPS is its
  AE, which a density centred on the same point always beats, **EIA's points are compared with the random walk on
  AE/SE**; densities among themselves on CRPS. DM test with HLN correction (rectangular HAC with k−1 lags, k =
  overlap in quarterly origin steps), t(n−1). Winners: CRPS gain over `rw`, one-sided p, **one Holm
  family over every (series, issuable candidate, horizon) test of the run** (FIX7; 260 tests; was one family per series of 48 tests for each price series, 60 for each quantity series and 10
  for `COPR_WORLD`, STEO excluded — the V6 text's "60–72" was wrong; AUDIT4 L3, corrected by FIX6),
  α = 5 %; `combo_steo_rw` is tested and reported but never a winner (`energy_models.NOT_ISSUABLE`).

## 4. Results (digest `4debaf21…`, `docs/dev/v6_pred_energy_backtest.json`; n ≈ 53 origins at h=1, fewer at h=12)

MAE ratio vs random walk (h = 1 / 3 / 6 / 9 / 12; < 1 better), and DM-HLN on AE for STEO:

| series | random-walk MAE (h1…h12) | **EIA STEO** | STEO vs RW (one-sided p, AE, **unadjusted**: no multiplicity correction; bold = p < 0.05 unadjusted) | combo STEO+RW | best REVA model |
|---|---|---|---|---|---|
| Brent `BREPUUS` ($/bbl) | 6.1 / 11.0 / 15.1 / 18.6 / 21.6 | 1.01 / 1.05 / 0.97 / 0.99 / 0.95 | 0.52 / 0.78 / 0.35 / – / 0.25 (n.s.) | 0.93–0.99 | none (inventory 1.09–1.20, ARDL 1.00–1.20) |
| WTI `WTIPUUS` | 5.9 / 10.5 / 13.9 / 16.7 / 19.7 | 0.96 / 1.01 / 0.96 / 1.01 / 0.95 | 0.36 / 0.58 / 0.31 / – / 0.26 (n.s.) | 0.94–0.99 | none |
| Henry Hub `NGHHUUS` ($/MMBtu) | 0.63 / 0.91 / 1.15 / 1.25 / 1.33 | 0.99 / **0.83 / 0.76 / 0.79 / 0.80** | 0.43 / **0.015 / 0.006** / – / **0.013** | 0.85–0.97 | none |
| U.S. crude `COPRPUS` (Mb/d) | 0.37 / 0.47 / 0.63 / 0.82 / 0.98 | **0.77 / 0.62 / 0.57 / 0.62 / 0.63** | **0.002 / <0.001 / <0.001** / – / **0.010** | 0.79–0.86 | ar_diff 0.78–0.90 |
| World liquids `PAPR_WORLD` | 1.6 / 2.1 / 2.7 / 3.2 / 3.7 | 0.91 / **0.83 / 0.77 / 0.73 / 0.69** | 0.13 / **0.008 / 0.001** / – / **0.030** | 0.77–0.90 | ar_diff 0.84–0.94 |
| World crude `COPR_WORLD` | (only 8–10 origins: series in archive files from 2024) | 1.16 / 0.87 / 0.76 / 0.70 / 0.59 | n.s. | 0.79–1.00 | none |
| OECD stocks `PASC_OECD_T3` (Mb) | 47 / 69 / 96 / 119 / 142 | 0.87 / 0.90 / 0.86 / 0.90 / 0.90 | 0.06 / 0.15 / 0.10 / – / 0.15 (n.s.) | 0.82–0.92 (CRPS 0.84–0.90, not Holm-significant) | none |

Winners (`registry/forecasts/energy_winners.yaml`, regenerated by FIX7, backtest digest `52fce398…`; CRPS vs RW, one
Holm family of 260): U.S. crude — `ar_diff` h2 (CRPS ×0.83, cov80 0.68, MAE +0.06 Mb/d vs STEO); world liquids —
`combo_models` h1 (×0.92, cov80 0.64), `ar_diff` h2 (×0.91, cov80 0.65), h3 (×0.89, cov80 0.61); each winner row now
carries its family size, 80 % coverage and its AE difference with STEO. *Superseded V6 list (per-series Holm):*
U.S. crude — `combo_steo_rw` h1–8, `ar_diff` h2–3, `combo_models` h2; world liquids — `combo_steo_rw` h2–4,
`ar_diff` h2–3, `combo_models` h1.
Random-walk 80 % coverage: prices 82–87 % (well calibrated), production/stocks 55–71 % (too narrow: trends).

Reading (honest): for crude prices, neither EIA nor REVA's fundamentals models beat no-change in real time —
consistent with the literature (Alquist-Kilian-Vigfusson; Baumeister-Kilian find small gains only with futures /
VAR combinations not available here). EIA's value lies in quantities (production) and natural gas. The inventory
and ARDL regressions are associations used for prediction, not causal claims; they lose to the random walk.

## 5. Registry (sealed forecasts)

* `registry/forecasts/question_specs.yaml`: 6 new families `eia.wti_monthly`, `eia.henry_hub_monthly`,
  `eia.us_crude_production`, `eia.world_liquids_production`, `eia.world_crude_production`,
  `eia.oecd_commercial_stocks`; `eia.brent_monthly` gets the `energy` block and quantity horizons 1–12. Each has an
  `energy: {code, vintage_table, winners}` block.
* `src/reva/evaluation/registry/energy.py` (hook in `service.build_batch` / `issue_cycle`, `all_models`): pins the
  vintage table, reads the STEO issue in force at `issued_at` (`as_known`), issues `forecast.energy_rw`,
  `forecast.reference_eia_steo` (point, quantity questions; `attribution` "U.S. Energy Information Administration,
  Short-Term Energy Outlook 2026-10 (projection), as pinned at issue", `source_cell` with raw sha256 + selector) and
  **only** the winners for that series and horizon (others abstain with "not a significant winner"). Log-normal
  predictives are frozen as 200-quantile empirical samples. 9 models in `registry/models/methods.yaml` (seeded).
* Sealed batch **`6d76937c0e6d123a7f56cd0454c681d57f7ead3c56bac7540095b3d9d3802164`** (2026-10-17 20:47 UTC,
  cycle 2026-Q4, under flock): 122 questions (80 quantity, 24 interval, 18 binary; 19 per new family, 8 new Brent
  horizons — Brent h 1/3/6/12 were sealed earlier by PRED-REGISTRY with baselines only), 719 issued forecasts:
  4 generic baselines × 122, energy_rw 122, EIA STEO 80, combo_steo_rw 18, ar_diff 8, combo_models 3.
  **FIX7 annotation (appended, batch unchanged):** its 18 `combo_steo_rw` forecasts are EIA-derived and dominated by
  STEO on points; they stay sealed and will be scored but are not evidence of REVA skill, and the model is excluded
  from future issuance (`scripts/fix7/annotate_sealed_batches.py`; annotation artifact listed in
  `docs/dev/v6_fix7.md`).
  Example `fq_2b7ab037c0a92a937bf6222a` (U.S. crude, 2027-01): persistence 13.965, EIA STEO 14.051, AR on changes
  14.151, STEO+RW 14.008 Mb/d. Earliest resolutions: Nov 2026 prices ~mid-Dec 2026.

## 6. Missing / limits / owner decisions

* Monthly issues (all 160 `.xlsx`, ~155 MB) and the 2004–2013 `.xls` issues: raw cap / no `.xls` reader. Bigger
  machine: set `ISSUE_MONTHS = tuple(range(1, 13))`, bump to 1.1.0, raise `eia.steo_archive` budget to ~200/day.
  An `.xls` reader (e.g. `xlrd`) is an owner decision (new dependency).
* No futures prices (NYMEX/ICE settlements are licensed) → no futures benchmark; EIA's own STEO uses futures for its
  price confidence bands. Owner decision if a licensed source is wanted.
* Outcomes use the final vintage of the target (EIA revises production); a first-release scoring variant is possible
  from the vintage table (not run).
* `COPR_WORLD` appears in archive files only from 2024 → 8–10 origins; no inference possible yet.
* Interval coverage of quantity densities is too narrow (55–71 % for 80 %).
* Note for the lead: the shared working tree had a leftover `.git/rebase-merge/autostash` (another agent's
  interrupted pull) during this session — not touched.

## 7. Commands

```bash
uv run pytest -q tests/unit/test_atlas_vintage_steo.py tests/unit/test_energy_forecasting.py tests/unit/test_registry_energy.py
uv run pytest -q tests/integration/test_atlas_vintage_db.py tests/integration/test_energy_backtest_db.py
df -h /   # >= 5 GB
flock /tmp/reva-devdb.lock uv run reva seed
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.vintage_steo          # each quarter (new issue)
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.eia_steo              # monthly (outcomes)
uv run python -m reva.cli.energy_backtest --out /tmp/energy_bt.json --winners registry/forecasts/energy_winners.yaml
flock /tmp/reva-devdb.lock uv run reva forecasts issue --family eia.brent_monthly --family eia.wti_monthly \
  --family eia.henry_hub_monthly --family eia.us_crude_production --family eia.world_liquids_production \
  --family eia.world_crude_production --family eia.oecd_commercial_stocks       # (weekly job does it too)
flock /tmp/reva-devdb.lock uv run reva forecasts resolve
```
