# V5 PRED-VINTAGE — vintages layer (data as published at the time)

Agent PRED-VINTAGE, 2026-10-17. Roadmap: `docs/roadmap/v5_prediction_engine.md` (decision 4: "every backtest uses
vintage data as published at the time … or is labelled final-vintage pseudo-real-time"). Baseline gap:
`docs/strategy/world_map.md` §5.3–5.4 — REVA's own clock starts on 2026-10-14, so without provider vintages no
backtest "as known at the time" is possible.

## 1. What was built

| Piece | File |
|---|---|
| Shared vintage conventions (flags, families, availability rules, variables) | `src/reva/adapters/connectors/vintage_common.py` |
| IMF WEO vintages (DBnomics copies 2008-04..2024-10; IMF portal 2025-04, 2025-10, 2026-04, 2026-10) | `src/reva/adapters/connectors/atlas_vintage_weo.py` (`atlas.vintage_weo_dbn`, `atlas.vintage_weo_imf`) |
| Philadelphia Fed RTDSM (ROUTPUT, P, RUC, CPI) + SPF medians with true release dates | `src/reva/adapters/connectors/atlas_vintage_philfed.py` (`atlas.vintage_philfed`) |
| ECB SPF (HICP, real GDP, unemployment; average point forecasts) | `src/reva/adapters/connectors/atlas_vintage_ecb_spf.py` (`atlas.vintage_ecb_spf`) |
| OECD real-time database (STES revisions = MEI original release data and revisions) | `src/reva/adapters/connectors/atlas_vintage_oecd.py` (`atlas.vintage_oecd_rt`) |
| Vintage-aware accessor, replay manifest | `src/reva/evaluation/vintages.py`; `CutoffRepository.vintages(store)` in `src/reva/evaluation/cutoff.py`; `replay.vintage_replay` |
| Real-time backtests (WEO vs persistence; SPF vs no-change) | `src/reva/evaluation/vintage_backtests.py` |
| Tests | `tests/unit/test_vintages.py` (no-look-ahead proofs), `tests/unit/test_atlas_vintage_connectors.py`, `tests/integration/test_atlas_vintage_db.py` |
| Registry | `registry/sources/{dbnomics_api,philfed_data,ecb_data_api}.yaml`; `registry/atlas/budget_requests/pred_vintage.yaml` (merged: budgets-1.3.21, digest re-pinned) |

No migration, no change to `src/reva/causal/**`, no schema change: vintage tables are ordinary atlas tables.
`reva.evaluation.vintages` reads the stored tables with its own SQL + Parquet read (no import of the atlas/ingest
stack), so invariant 11 (no network code reachable from `reva.evaluation`) holds; `tests/invariants`: 191 passed.

### 1.1 Storage: atlas tables keyed by (source, vintage, reference period, country, variable)

The atlas long schema requires `(indicator, provider_geo, period)` unique, so the vintage is folded into the
indicator: `<provider code>|v=<vintage>` (`NGDP_RPCH|v=2008-04`, `ROUTPUT|v=2008Q1`, `RGDP3|v=2008Q1`,
`HICP.2009|v=2008-Q1`, `B1GQ_Q.Q.XDC._T|v=200802`). Every cell's `flags` carry, always:

| flag | meaning |
|---|---|
| `family` | vintage family (`imf_weo`, `philfed_rtdsm`, `philfed_spf`, `ecb_spf`, `oecd_mei_rt`); the as-of series key is (family, variable, provider_geo, period) |
| `vintage` | the provider's vintage label, verbatim |
| `vintage_date_text` | the publication date as the provider states it, verbatim (`2/12/08`, `8/31/90*`, `2025-10-14T13:00:00Z`), else `null` (never invented) |
| `available_at` | conservative UTC instant from which REVA asserts the vintage was public; `null` = no proof (never selected) |
| `availability_basis` | rule id (§2) |
| `variable` / `provider_variable` | REVA vintage variable id (`vintage_common.VARIABLES`, mapped by provider code only) / provider code |
| `estimate_kind` | `projection` (forecast / survey expectation), `actual` (WEO ≤ declared latest actual year), `published_history`, `published_history_boundary_unknown` |

Raw bytes are archived (`evidence.acquisition`) and each cell has `raw_sha256` + `selector` (file/sheet/cell,
series code, CSV line). Licence per table in `meta.artifact.rights` (§6).

## 2. Availability rules (publication dates)

| rule | family | evidence (verbatim where the provider states it) | available_at |
|---|---|---|---|
| `imf_publication_date_attribute` | WEO (IMF portal) | SDMX dataset attribute `PUBLICATION_DATE` (`2025-10-14T13:00:00Z`, `2026-04-14T13:00:00Z`, `2025-04-22T13:00:00Z`, `2026-10-13T02:00:00Z`) | that instant |
| `weo_release_month_end` | WEO (DBnomics) | DBnomics dataset code `IMF/WEO:YYYY-MM` = the IMF release file month; the day is not in the copy | 1st of the following month 00:00 UTC |
| `rtdsm_middle_month_end` | RTDSM | `gen_doc_NIPA.pdf` (archived): "the observations represent the data as they were known in the middle month of each quarter (February, May, August, and November)" | 1st of the month after the middle month |
| `spf_news_release_date` | US SPF | `spf-release-dates.txt` "News Release Date" (verbatim, e.g. `8/31/90*`); "True deadline and news release dates for surveys prior to 1990:Q2 are not known." | day after the release date 00:00 UTC; surveys before 1990:Q2 → `null` (excluded) |
| `ecb_spf_round_quarter_end` | ECB SPF | survey round `YYYY-Qn`; the publication day is not in the data | 1st day of the next quarter |
| `oecd_mei_edition_month_end` | OECD | `EDITION` (CL_EDITION name e.g. "February 1999"); day not in the data | 1st of the following month |

All rules can only make a vintage *later* than it really was (never earlier), so as-of selections can lose a few
days of information but can never leak. The residual assumption of the two month-end rules (WEO DBnomics, OECD)
and of the ECB quarter-end rule is that the provider published within the labelled month/quarter (true for every
WEO release we know of, including the "September 2011" WEO, which DBnomics does not carry).

## 3. Vintage-aware accessor (no look-ahead)

`CutoffRepository(conn, cutoff, mode).vintages(store)` → `VintageAccessor`:

* `available_then` (reconstruction): a cell is admissible iff `available_at` (with a basis) ≤ cutoff; today's table
  versions are read. Exclusions are counted (`no_availability_proof`, `available_after_cutoff`).
* `as_known` (operational): additionally the atlas table version must have been recorded by REVA at/before the
  cutoff — before 2026-10-17 nothing is admissible, which is the truth about REVA's own knowledge.
* `revised_data_retrospective`: as-of methods refuse (`CapabilityGap`); only `final_vintage()` works, labelled
  `final_vintage_pseudo_real_time` (roadmap decision 4).

Methods: `latest()` (per series key the latest admissible vintage; a period whose latest vintage holds only a
projection is dropped and counted `only_projection_at_cutoff` — projections are never observations),
`forecasts(family=…)`, `vintage_in_force(family)`, `history(...)` (revision triangle as known at the cutoff),
`first_release(...)`. Time is filtered *before* any vintage is chosen; every returned row is asserted
(vectorised) and passed through `LeakageGuard` (`observation_revision` with `source_available_at` +
`availability_basis`), which raises `LeakageBlocked` on any violation. `vintage_replay(conn, store, cutoff,
family=…)` (also `reva.evaluation.replay.vintage_replay`) returns an `available_then_reconstruction` manifest
(inputs with availability proof, exclusions, vintage in force).

Tests proving no look-ahead (`tests/unit/test_vintages.py`): the answer changes exactly at the availability
instant (1 µs before → old vintage); appending later vintages never changes an earlier answer; projections are
never returned as observations; no proof → exclusion; `as_known` admits nothing before REVA recorded the table;
the retrospective mode is explicit and labelled; the guard blocks a late cell even if selection were bypassed
(monkeypatched); a property test over every publication instant of real captured WEO and SPF vintages (±1 µs and
random offsets) checks that every selected cell was published at/before the cutoff and is the latest admissible
vintage of its key; SPF as of the day before / after a release date; the WEO backtest uses only the vintage in
force. DB integration test: fixture ingest of all five sources, accessor via `CutoffRepository`, replay manifest.

## 4. Live load (dev DB `reva`, origin `live`, 2026-10-17 15:06–15:19 UTC)

| atlas table | source (connector 1.0.1) | requests | raw bytes archived | cells |
|---|---|---|---|---|
| `vintage_weo.weo_dbnomics` | `atlas.vintage_weo_dbn` (api.db.nomics.world) | 33 | 46,883,826 | 973,864 (316,035 `NA` cells skipped) |
| `vintage_weo.weo_imf` | `atlas.vintage_weo_imf` (api.imf.org) | 4 | 9,574,142 | 165,965 |
| `vintage_us.rtdsm` + `vintage_us.spf_median` | `atlas.vintage_philfed` (www.philadelphiafed.org) | 9 | 3,374,564 | 333,823 + 21,477 |
| `vintage_ea.ecb_spf` | `atlas.vintage_ecb_spf` (data-api.ecb.europa.eu) | 1 | 564,582 | 1,539 (329 rolling-horizon cells not loaded) |
| `vintage_oecd.stes_revisions` | `atlas.vintage_oecd_rt` (sdmx.oecd.org) | 28 | 7,619,126 | 72,120 |
| **total** | 5 sources | **75** | **68.0 MB** (budget ≤ 100 MB) | **1,568,788** |

No request failed; every run settled its budget (`budgets-1.3.24` at run time).

## 5. Coverage (variables × countries × vintages)

| family | variable (REVA id ← provider code) | geographies | vintages | span |
|---|---|---|---|---|
| IMF WEO (`imf_weo`) | `real_gdp_growth` ← NGDP_RPCH | 211 (196 ISO countries + aggregates in the portal vintages) | 37 (2008-04 … 2026-10; 2011-09 missing) | years 1980 – 2031 |
| | `cpi_inflation` ← PCPIPCH | 211 | 37 | |
| | `unemployment_rate` ← LUR | 129 | 37 | |
| | `current_account_gdp` ← BCA_NGDPD | 209 | 37 | |
| | `gg_gross_debt_gdp` ← GGXWDG_NGDP | 209 | 32 (from 2010-10: not in earlier releases) | |
| Philadelphia Fed RTDSM (`philfed_rtdsm`) | `real_gdp_level` ← ROUTPUT; `gdp_price_index` ← P; `unemployment_rate` ← RUC (monthly obs); `cpi_level` ← CPI (monthly obs) | USA | 244 quarterly vintages 1965Q4 – 2026Q3 (CPI: 129, from 1994Q3) | obs 1947 – 2026Q3 |
| US SPF medians (`philfed_spf`) | 20 variables: real/nominal GDP level and q/q growth, GDP price index and inflation, unemployment, CPI / core CPI / PCE / core PCE (q/q annualised and Q4/Q4), T-bill, 10y T-bond, industrial production, housing starts, payrolls | USA | 232 surveys 1968Q4 – 2026Q3, **145 with a known release date (1990Q2 on)**; 5,321 earlier cells have no availability and are never selected | targets 1968Q3 – 2029 |
| ECB SPF (`ecb_spf`) | `hicp_inflation`, `real_gdp_growth`, `unemployment_rate` (calendar-year + longer-term horizons) | euro area (U2) | 111 rounds 1999-Q1 – 2026-Q3 | targets 1999 – 2031 |
| OECD MEI real-time (`oecd_mei_rt`) | `real_gdp_level` ← B1GQ_Q (quarterly), `cpi_level` ← CP (monthly), `unemployment_rate` ← UNEMP (monthly) | G7: USA, JPN, DEU, FRA, GBR, ITA, CAN | 111 quarterly editions 199902 – 202608 | each edition: its last 3–4 years |

Examples (from the live tables; ids = atlas indicator `|v=` vintage):

* Germany real GDP growth for 2009 (`vintage_weo.*`, `NGDP_RPCH|v=…`, `DEU`): 2008-04 **+1.037** (projection) →
  2008-10 −0.002 → 2009-04 −5.614 → 2009-10 −5.297 (projections) → 2010-04 −4.973 (published) → … → 2024-10 −5.548 →
  2025-10 −5.55 (actual, IMF portal, `LATEST_ACTUAL_ANNUAL_DATA`) → 2026-10 −5.545992. As of 2009-01-15 the accessor
  returns only the 2008-04 and 2008-10 vintages.
* US real GDP 2008:Q4 (`vintage_us.rtdsm`, `ROUTPUT|v=…`): first in the 2009Q1 vintage (11,599.4, available
  2009-03-01), 2009Q2 11,522.1, 2009Q3 13,141.9 — the 2009 benchmark revision re-based the level (2005 dollars):
  levels must never be compared across vintages; the SPF backtest computes growth inside each vintage.
* Cross-check of sources: SPF 2008:Q1 `RGDP1` (the quarter before the survey, "known to forecasters") = 11,677.4 =
  RTDSM `ROUTPUT|v=2008Q1` for 2007:Q4 = 11,677.4.

## 6. Backtests this enables (run on the live tables, `src/reva/evaluation/vintage_backtests.py`)

All are `available_then` reconstructions (REVA did not exist at these origins), point forecasts only, no probability.

**A. IMF WEO vs persistence, real GDP growth, all countries** (origins 1 June and 1 December 2008–2025: the April
resp. October WEO in force; baseline = previous year as published *in the same vintage*; outcome = the target year
in the first WEO vintage available on/after 1 October of the next year; scored as of 2026-10-13):

| horizon | n | MAE WEO | MAE persistence | median AE WEO / persistence | rel. MAE | WEO closer | mean error WEO |
|---|---|---|---|---|---|---|---|
| current year (h0) | 6,617 | 1.53 | 3.61 | 0.79 / 1.66 | 0.42 | 72 % | +0.02 |
| next year (h1) | 6,220 | 2.83 | 4.18 | 1.46 / 2.25 | 0.68 | 62 % | **+1.24** (optimism) |
| G20 countries, h0 | 684 | 0.77 | 2.64 | 0.45 / 1.29 | 0.29 | 80 % | −0.11 |
| G20 countries, h1 | 646 | 1.84 | 3.11 | 1.00 / 1.94 | 0.59 | 69 % | +0.80 |

December origins beat June origins at h0 (MAE 1.23 vs 1.83): more of the year is known. The next-year optimism
bias (+1.2 pp) is the known WEO bias. **B. WEO CPI inflation** (same design): median AE 0.62 vs 1.62 (h0), 1.46 vs
2.09 (h1), WEO closer in 76 % / 64 % of cases, but the **means are dominated by hyperinflation outliers** (MAE 1,719
vs 172): cross-country inflation panels must be scored with medians or by country, never pooled means.

**C. US SPF median real GDP growth (annualised q/q) vs no change** (145 surveys 1990Q3–2026Q3 with their true
release dates; outcome = first release from the RTDSM). **Corrected by FIX7 (QA-PRED X8, 2026-10-17):** the
no-change baseline now uses the **RTDSM vintage of the survey quarter** — the data "as known in the middle month"
of that quarter, i.e. the information set SPF panellists had at their mid-quarter deadline — instead of the vintage
in force under the conservative availability rule, which was one quarter staler than what forecasters had and
inflated the SPF's advantage (`spf_backtest(baseline_vintage="survey_quarter")`, the default; the V5 rule stays
available as `baseline_vintage="available_then"`):

| horizon | n | MAE SPF | MAE no-change | median AE SPF / no-change | rel. MAE (corrected) | rel. MAE (V5, stale baseline) |
|---|---|---|---|---|---|---|
| current quarter (2) | 145 | 1.29 | 2.54 | 0.95 / 1.35 | **0.51** | 0.46 |
| +1 quarter (3) | 143 | 1.83 | 2.83 | 1.09 / 1.40 | **0.65** | 0.64 |
| +2 (4) | 142 | 1.98 | 2.82 | 1.22 / 1.36 | **0.70** | 0.67 |
| +3 (5) | 141 | 1.97 | 2.92 | 1.06 / 1.52 | **0.67** | 0.65 |
| +4 (6) | 140 | 1.97 | 3.07 | 1.18 / 1.71 | **0.64** | 0.62 |

The SPF still beats no change at every horizon, by less (current quarter −49 % instead of −54 %). QA-PRED's
independent re-run gave 0.509 / 0.648 / 0.705 / 0.680 / 0.644 (same rule; third-digit differences only).

**B'. WEO CPI headline (FIX7, QA-PRED X6).** Pooled cross-country inflation is reported on the **ratio of median
absolute errors** (`relative_median_ae`, `headline_metric` in the result): h0 **0.38**, h1 **0.70** (WEO closer in
76 % / 64 % of cases). The pooled-mean ratio (h0 10.0, h1 16.7 — WEO *worse*, driven by hyperinflation outliers)
must never be quoted as a headline in either direction. **Scoring vintage (X7):** WEO outcomes are the WEO's own
October t+1 estimate (same institution; for many low-capacity countries an IMF staff estimate), which flatters the
WEO and any WEO-wrapped model; an independent-outturn robustness run (national accounts first releases for
advanced economies) is not done yet.

Further backtests the layer supports now (no new data needed): any REVA model for the 5 WEO variables × ~190
countries at 1–2 years ahead, scored against WEO as the strong baseline (the roadmap's "IMF WEO, SPF" baselines for
PRED-REGISTRY); US output, inflation and unemployment models at 1–6 quarters against SPF medians with RTDSM first
releases (and revision analysis: first vs later releases); euro-area HICP / GDP / unemployment against ECB SPF
(calendar-year targets, 1999–2026); G7 nowcasting / short-horizon GDP, CPI, unemployment models on OECD MEI
editions (1999–2026, first-release and early-revision triangles). Every model fold must read its inputs through
`CutoffRepository(conn, origin, "available_then").vintages(store)`; a backtest that does not is
"final-vintage pseudo-real-time" and must say so (`VintageAccessor.final_vintage`, labelled).

## 7. Sources, licences, what is missing

| source | licence / terms (verbatim basis) | flag |
|---|---|---|
| IMF WEO (DBnomics copies; IMF portal) | IMF Copyright and Usage terms; provider attribute "(c) International Monetary Fund Copyright. All Rights Reserved." | not an open licence (`open_licence: false`); attribution required |
| Philadelphia Fed RTDSM / SPF | no data licence on the data pages; footer "Copyright 2026. All rights reserved. Federal Reserve Bank of Philadelphia" | **restricted**: redistribution not stated → owner decision before republishing values |
| ECB SPF | "users of this website may make free use of the information obtained directly from it subject to the following conditions: When such information is distributed or reproduced, it must appear accurately and the ECB must be cited as the source." | attribution |
| OECD STES revisions | OECD Terms and Conditions (CC BY 4.0 default since 2024-07-01; dataset-level licence not separately verified) | attribution |

Missing, and why:

* **ALFRED / FRED vintages**: `alfred.stlouisfed.org` and `fred.stlouisfed.org` graph CSV endpoints return an empty
  reply / HTTP/2 stream error to this client (no key involved); not worked around. The RTDSM covers the key US NIPA
  series; ALFRED (≈ all FRED series with exact vintage dates) needs either a FRED API key (owner) or another network
  path. **Owner decision.**
* **IMF's own WEO vintage files** (`www.imf.org/.../weo-database/<year>/<month>`, `WEOhistorical`): 403 "Access
  Denied" (Akamai) to non-browser clients; not worked around. DBnomics copies were used instead (release month only,
  no "Estimates Start After" column → `published_history_boundary_unknown` before the vintage year). The IMF
  "Historical WEO forecasts" file (forecasts back to 1990) is behind the same block. **Owner decision** (manual
  download into the archive, or IMF access arrangement). WEO September 2011 is in neither DBnomics nor the portal.
* **OECD Economic Outlook vintages**: the portal serves EO 114–119 as dataflows but publishes no release date in
  their metadata; without an evidenced date they would be unusable as-of, so they were not loaded.
* OECD STES: only G7, three variables, quarterly editions and a 3-year history window per edition (disk budget);
  the full database (all OECD members, monthly editions, deep history) is a larger run (§8).
* ECB SPF rolling horizons (P12M, P24M …) not loaded: their reference period depends on each round's latest data
  release, which the data does not state. Probability distributions (SPF density forecasts) not loaded.
* Kosovo's WEO code `UVK` stays unmapped (no published identifier links it to `XKX`); aggregates stay unmapped.
* Vintage tables are not in the catalog `source_order`: they serve evaluation, not the headline answer layer.

## 8. Commands

```bash
# tests
uv run pytest -q tests/unit/test_vintages.py tests/unit/test_atlas_vintage_connectors.py tests/invariants
uv run pytest -q tests/integration/test_atlas_vintage_db.py        # throwaway DB, fixtures
# live (dev DB; check df -h / >= 5 GB first)
flock /tmp/reva-devdb.lock uv run reva seed
for s in atlas.vintage_ecb_spf atlas.vintage_philfed atlas.vintage_weo_imf atlas.vintage_oecd_rt atlas.vintage_weo_dbn; do
  flock /tmp/reva-devdb.lock uv run reva atlas ingest $s; done
# a later WEO / SPF / MEI release: re-run the source (new table version; earlier as-of answers unchanged).
# bigger machine: widen atlas_vintage_oecd.AREAS / EDITION_MONTHS / HISTORY_YEARS and atlas_vintage_weo.SUBJECTS
# (bump the connector version), then re-ingest.
```

```python
from datetime import datetime, UTC
from reva.evaluation.cutoff import CutoffRepository
acc = CutoffRepository(conn, datetime(2009, 1, 15, tzinfo=UTC), "available_then").vintages(store)
acc.vintage_in_force("imf_weo")                         # {'vintage': '2008-10', ...}
acc.forecasts(family="imf_weo", variable="real_gdp_growth", geos=["DEU"]).rows
acc.latest(family="philfed_rtdsm", variable="real_gdp_level").rows   # as published then
```

## 9. Owner decisions

1. ALFRED/FRED: provide a FRED API key (or allow another path) — blocked for this client today.
2. IMF www.imf.org downloads (WEO vintage files, Historical WEO forecasts): manual capture into the archive or an
   access arrangement — blocked (Akamai 403) for non-browser clients.
3. Philadelphia Fed data: no stated licence ("All rights reserved" footer) — confirm REVA may republish derived
   numbers or keep them internal (scores only).
4. Whether REVA should rely on DBnomics as redistributor of IMF releases (provenance is DBnomics' copy of the IMF
   files; it has not been cross-checked against IMF originals, which this client cannot download).
