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

COVERAGE_AND_RESULTS_PLACEHOLDER
