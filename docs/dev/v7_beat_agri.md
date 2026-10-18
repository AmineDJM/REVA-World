# V7 BEAT-AGRI — REVA vs USDA WASDE crop projections (pre-registered, same-date)

Agent BEAT-AGRI, 2026-10-17/18. Goal (owner): REVA's numeric forecasts must beat the official forecasters, judged
honestly. Track: production, yield and ending stocks of corn, soybeans, wheat, rice — United States and World — vs
the USDA World Agricultural Supply and Demand Estimates (WASDE) on every monthly release. Standard:
`docs/dev/review_v6_predictions.md` (fair benchmark X1, no point-CRPS X2, one Holm family X3, holdout X4, no
final-vintage advantage hidden X9).

## Result in one paragraph

**REVA does not beat WASDE.** Pre-registered protocol (`registry/forecasts/beat_agri/protocol.yaml`, code sha256
`60b0257e…`, committed before any data were loaded or any window opened); development on marketing years (MY)
2005–2016 selected 12 hypotheses by the mechanical rule; the holdout (MY 2017–2024, 1,604 cases, untouched until
2026-10-18 17:57 UTC, opened once — `holdout_ledger.yaml`) confirmed **0 of 12** (one Holm family, α 0.05). The
best holdout results — revision-smoothing correction of WASDE's World production, mid/late season — have AE ×0.90–0.91
and CRPS ×0.92–0.93, one-sided p 0.12–0.20: direction right, not significant. U.S. corrections lose (×1.02–1.11),
World ending-stocks corrections lose badly on points (×1.36–1.59), the weather-adjusted U.S. corn/soybean candidate
loses (AE ×1.05–1.20), and REVA's pure trend model is 18–119 % worse than WASDE on U.S. yields. The 2026/27 marketing
year is sealed in the forecast registry (batch `5d903078…`, 18 questions, 90 forecasts) for prospective scoring.

## Sources (new connectors, loaded live)

| Connector (source key) | What | Licence | Fetched (one full run) | On disk |
|---|---|---|---|---|
| `atlas.usda_wasde_esmis` 1.0.2 (`usda.wasde_esmis`) | USDA ESMIS archive (`esmis.nal.usda.gov/publication/world-agricultural-supply-and-demand-estimates?page=N`, 36 listing pages) + one report per release 1998-01 → 2026-10: TXT to 2010-06, XML from 2010-07 (incl. the 2018-10..2021-07 `latest.xml` / `WASDE-<n>-<Month>-<DD>-<YYYY>.xml` names and the `xmlns="wasde"` reports) | Public domain (U.S. Gov. work, 17 U.S.C. 105), attribution kept | 383 requests, 437 MB | 407 raw objects, **14.3 MB** (DISKZ zstd; XML 45–70×) |
| `atlas.ncei_cag` 1.0.1 (`noaa.ncei_cag`) | NOAA NCEI Climate at a Glance: Corn Belt (261), Soybean Belt (262), Primary Corn & Soybean Belt (260), monthly average temperature and precipitation 1990-01 → 2026-09 | Public domain (NOAA), cite NCEI | 6 requests, 32 kB | 2,646 rows |

Tables: `atlas.usda_wasde_esmis.headline` (20,640 rows; artifact `a853d4df…`; 344 releases parsed, 2 corrections
without the tables, 1 re-issue (2008-10-28) whose layout is not read, 7 releases with no report file (revision
workbooks, announcements, `.doc`, `.asc`) — each listed with its reason in the run summary) and
`atlas.ncei_cag.agbelt_monthly` (artifact `b00838e2…`). Cells: `wasde_esmis:<series>|v=<release date>`, series
`us.{corn,soybeans,wheat,rice}.{yield,production,ending_stocks}` and `world.{…}.{production,ending_stocks}`; period
= MY first calendar year; the projection year's value = the report month's column; status by position
(projection / estimate / published history), printed label kept; labels verbatim in flags; `available_at` = release
day + 1 (00:00 UTC) — the ESMIS `Last-Modified` of old files is the 2018 migration date and proves nothing. TXT vs XML
of the same release (2010-08-12) agree on 60/60 cells up to ±0.01 (2 cells, rendering rounding; unit test).
Weather: one current nClimDiv release (final vintage); month m usable from day 12 of m+1 (NCEI's September 2026
release is stamped 2026-10-06), labelled `final_vintage_with_publication_lag`.

Budgets: `registry/atlas/budget_requests/beat_agri.yaml` → budgets-1.3.40, then 1.3.42 (usda.wasde_esmis 900/day: a
same-day re-run after the 1.0.2 parser fix), noaa.ncei_cag 12/day; digest re-pinned in `src/reva/autonomy/policy.py`.
Source registry: `registry/sources/usda_wasde_esmis.yaml`, `registry/sources/noaa_ncei_cag.yaml`.

Disk rule: every live ingest started with ≥ 5.0 GB free measured in bytes (5.04–5.74 × 10⁹ B; e.g. the final WASDE
run started at 5,037,711,360 B = 4.69 GiB). If the rule is read in GiB (as BEAT-ENERGY's seal does), the
ingests ran at 4.69–5.35 GiB, i.e. below it; the archive added 14.3 MB.

## Protocol (pre-registered; see the YAML for the full text)

* **Same date.** Origin = each WASDE release (first release of each month printing the MY as projection year),
  information = everything published by release day + 1. Benchmark = WASDE's own number in that release.
* **Truth (declared in advance).** USDA's later estimate as published: the MY's value in the first WASDE released on
  or after 1 January Y+1 (U.S. production, yield — incorporates NASS's annual Crop Production / Small Grains
  summaries) or 1 November Y+1 (U.S. ending stocks, World series). January 2019 (shutdown) → February 2019 by rule.
* **Windows.** U.S. corn/soybeans/rice production & yield: May–Dec (k 0–7); U.S. wheat production & yield: May–Sep;
  stocks and World: May–Apr. Cells: early k 0–2, mid 3–5, late 6–11.
* **Candidates** (all use WASDE's number; stated so): `bc` bias correction (shrunk mean past relative error, same
  series × report month); `rev` revision smoothing (pooled slope of past errors on the latest WASDE revision,
  Isengildina-Irwin-Good 2006, shrunk, clipped); `wx` U.S. corn/soybean yield & production + ridge on belt
  June–August temperature/precipitation anomalies usable at the release. Pure REVA `trend` (no WASDE input):
  descriptive. Densities: N(point, RMS of own past relative errors).
* **Fair density benchmark** (QA-PRED X1): N(WASDE point, |W| × RMS of WASDE's own past relative errors at the same
  series and report month, ≥ 5 MYs). Points on AE, densities on CRPS — never a point on CRPS.
* **Inference.** Losses in % of the truth; differentials averaged within each MY (MY = unit; cases of one MY share
  the outcome); DM-HLN, h 1, one-sided; ≥ 6 MYs. Selection: per (group, window, loss) the best candidate with dev
  relative < 1 and p < 0.10. Holdout: one Holm over all hypotheses; CRPS hypotheses also need 80 % coverage in
  [0.70, 0.90].
* Splits: burn-in 1998–2004 (history only), development 2005–2016, holdout 2017–2024, MY 2025 not evaluated,
  prospective 2026/27.

## Development (MY 2005–2016; 2,386 cases; 68 comparisons; `development.json`)

| Cell | bc AE | rev AE | bc CRPS | rev CRPS | note |
|---|---|---|---|---|---|
| U.S. yield early / mid / late | 1.04 / 1.03 / 1.01 | 1.03 / 1.03 / 1.00 | 1.03 / 1.05 / 1.02 | 1.01 / 1.05 / 1.02 | WASDE unbeaten |
| U.S. production early / mid / late | 1.06 / 1.04 / 1.00 | 1.05 / 1.05 / 0.99 | 1.04 / 1.05 / 1.01 | 1.02 / 1.05 / 1.02 | WASDE unbeaten |
| U.S. ending stocks early / mid / late | 0.96 / 1.00 / **0.93** (p .035) | 0.97 / 1.00 / 0.93 (p .043) | 0.96 / 0.99 / **0.91** (p .025) | 0.96 / 0.99 / 0.91 | late selected |
| World production early / mid / late | 0.98 / 0.87 / 0.81 (p .0003, Holm ✓) | **0.95** (p .074) / **0.86** (p .012) / **0.79** (p .0005, Holm ✓) | 0.98 / 0.90 / 0.84 | **0.94** / **0.89** / **0.81** | rev selected ×6 |
| World ending stocks early / mid / late | 0.95 / 0.86 / 0.98 | 0.99 / **0.86** (p .097) / 0.98 | 0.86 / **0.79** (Holm ✓) / **0.76** (Holm ✓) | **0.85** (p .039) / 0.79 / 0.77 | 4 selected |
| wx U.S. corn+soy yield mid / late | AE 0.98 / 1.16 | — | CRPS 0.93 / 1.08 | — | not selected |
| wx U.S. corn+soy production mid / late | AE 1.00 / 1.13 | — | CRPS 0.93 / 1.08 | — | not selected |

Relative loss = candidate / WASDE (AE) or candidate / fair fan (CRPS); bold = selected for the holdout. Pure REVA
trend vs WASDE on U.S. yields: ×1.35 / ×3.0 / ×7.0 (early / mid / late). 12 hypotheses (`hypotheses.yaml`,
sha256 `d0e1b094…`, committed in `db90fa7` before the opening).

## Holdout (MY 2017–2024; 1,604 cases; opened once 2026-10-18 17:57:20 UTC; family 12)

| Hypothesis | n cases / MYs | REVA loss | WASDE loss | Relative | p (one-sided) | Holm | cov80 | Confirmed |
|---|---|---|---|---|---|---|---|---|
| U.S. ending stocks, late, bc, AE | 188 / 8 | 13.09 % | 11.80 % | 1.109 | 0.91 | ✗ | — | **no** |
| U.S. ending stocks, late, bc, CRPS | 188 / 8 | 9.61 | 8.80 | 1.092 | 0.91 | ✗ | 0.94 | **no** |
| World ending stocks, early, rev, CRPS | 94 / 8 | 8.94 | 8.92 | 1.002 | 0.47 | ✗ | 0.83 | **no** |
| World ending stocks, mid, rev, AE | 96 / 8 | 11.42 | 7.33 | 1.559 | 0.96 | ✗ | — | **no** |
| World ending stocks, mid, bc, CRPS | 96 / 8 | 8.38 | 8.28 | 1.011 | 0.55 | ✗ | 0.88 | **no** |
| World ending stocks, late, bc, CRPS | 188 / 8 | 5.40 | 5.89 | 0.916 | 0.10 | ✗ | 0.93 | **no** |
| World production, early, rev, AE | 96 / 8 | 2.27 | 2.28 | 0.999 | 0.48 | ✗ | — | **no** |
| World production, early, rev, CRPS | 94 / 8 | 1.67 | 1.63 | 1.026 | 0.71 | ✗ | 0.86 | **no** |
| World production, mid, rev, AE | 96 / 8 | 1.70 | 1.86 | 0.913 | 0.14 | ✗ | — | **no** |
| World production, mid, rev, CRPS | 96 / 8 | 1.25 | 1.34 | 0.933 | 0.20 | ✗ | 0.88 | **no** |
| World production, late, rev, AE | 188 / 8 | 1.06 | 1.18 | 0.901 | 0.13 | ✗ | — | **no** |
| World production, late, rev, CRPS | 188 / 8 | 0.77 | 0.84 | 0.917 | 0.12 | ✗ | 0.78 | **no** |

Losses in % of the truth. **0 of 12 confirmed.** Unselected holdout grid (descriptive, no claim): no comparison
with p < 0.05 anywhere; closest: bc World production late AE ×0.84 (p 0.052). Losses: bc/rev U.S. yield ×1.00–1.11,
U.S. production ×0.98–1.04, U.S. ending stocks ×1.05–1.12, World ending stocks AE ×1.36–1.59 (the past bias did not
persist after 2016); wx U.S. corn+soy yield AE ×1.20 (mid) / ×1.05 (late), production ×1.15 / ×1.05, CRPS
×0.99–1.15; pure trend vs WASDE U.S. yields ×1.18 / ×1.56 / ×2.19.

WASDE's own revision path (holdout, MAE % of truth by report month k = 0 May … 11 April): U.S. corn yield 2.5 → 1.1;
U.S. soybean yield 3.0 → 1.1; U.S. corn ending stocks 20.8 → 5.8 (mean signed error −14 % in May: WASDE's early
stocks projections were too high on average, yet the bias correction learned on 1998–2016 did not help in
2017–2024); World corn production 3.0 → 1.3 (mean signed error +0.7…+1.2 %: truth above the projection); World
wheat ending stocks 4.3 → 1.9. Full table: `holdout.json` → `descriptive.wasde_revision_path`.

## Scoreboard (wins AND losses)

| Against | Wins | Losses / no win |
|---|---|---|
| USDA WASDE point, same release (AE) | **none** | 6 pre-registered AE hypotheses unconfirmed (×0.90–×1.56); every unselected cell unconfirmed |
| WASDE fair error fan (CRPS) | **none** | 6 pre-registered CRPS hypotheses unconfirmed (×0.92–×1.09) |
| Pure REVA (trend) vs WASDE | **none** | ×1.18–×2.19 worse on U.S. yields (descriptive) |

Honest wording: "On the pre-registered 2017–2024 holdout, no REVA correction of the WASDE projection beat WASDE on
the same release date; a revision-smoothing correction of WASDE's World production projections points the right
way (−7 to −10 %) but is not significant with 8 marketing years."

## Prospective (2026/27, sealed)

Batch `5d903078a6e98e5e3cf34667bb27a128e2031b27a80bce9f6f6187538490c857` (2026-10-18 17:58:22 UTC, dev DB, origin
`live`; `model.evaluation` rows with `metrics.kind = registry_issue`, `task_scope.kind = beat_agri`): 18 quantity
questions on the WASDE of 2026-10-09 (k = 5; U.S. wheat yield/production are outside their May–Sep window and not
asked), each with its truth rule; 90 forecasts: `forecast.reference_usda_wasde` (attributed), `forecast.wasde_fair_fan`
(benchmark), `forecast.beat_agri_bc`, `forecast.beat_agri_rev`, `forecast.beat_agri_wx` (U.S. corn/soybeans yield and
production only), each labelled "NOT confirmed on the holdout". Examples: U.S. corn yield WASDE 181.2 bu/acre (fan σ
3.16) vs bc 181.03, rev 180.94, wx 180.67; World corn production 1,293.75 Mt (σ 32.95) vs bc 1,312.44, rev 1,312.89;
U.S. soybean ending stocks 315 mbu (σ 100.8) vs bc 288.2. Truths: January 2027 (U.S. production/yield), November
2027 (stocks, World). The generic registry resolver does not handle this scope; scoring is by the same losses with
`reva.evaluation.beat_agri` (resolver command still to write, see below). Prospective claims need ≥ 6 resolved MYs.

## Defects found, and their effect

* `cases.compute_candidates` fills a `wx` point for any corn/soybean series (it keys on the crop), including U.S.
  ending stocks and World corn/soybeans. Those are outside every pre-registered wx cell: never scored, and not issued
  (`seal.WX_SERIES`, test). The evaluated code was not changed (digest = protocol's).
* Connector 1.0.0/1.0.1 skipped the 2018-10..2021-07 archive names and two XMLs with a default namespace; 1.0.2
  (before any window was opened) reads them; the evaluation used only the 1.0.2 table.

## Owner decisions / not done

1. **NASS QuickStats** (crop condition, objective yield) needs an api.data.gov key — the most plausible information
   edge between WASDE releases; not used. **Futures** (CBOT) are licensed — not used (owner decision).
2. **Between-release nowcasts** (e.g. late-July weather / condition before the August WASDE) were not in the
   owner's same-date family; a new protocol could test them on the prospective registry.
3. **Price outlooks** (FAO FFPI subindices, World Bank Pink Sheet): no official numeric forecast vintages in REVA
   (World Bank CMO forecast tables not loaded; FAO/AMIS outlooks are narrative) — not tested.
4. **Resolver**: a `reva.cli.beat_agri resolve` command (truth by the declared rule from the WASDE table version
   published after the seal) is still to be written before January 2027; monthly re-sealing on each later 2026/27
   WASDE release (`seal --seal` is idempotent per release) should be scheduled.

## Files and commands

Code: `src/reva/adapters/connectors/atlas_usda_wasde_esmis.py`, `atlas_ncei_cag.py`; `src/reva/evaluation/beat_agri/`
(`cases.py`, `service.py` — protocol digest; `seal.py`); CLI `src/reva/cli/beat_agri.py`. Registry:
`registry/forecasts/beat_agri/{protocol.yaml, development.json, hypotheses.yaml, holdout.json, holdout_ledger.yaml}`,
`registry/models/methods.yaml` (5 models). Result: `docs/dev/v7_beat_agri_result.json`. Tests:
`tests/unit/test_beat_agri_connectors.py` (8), `tests/unit/test_beat_agri.py` (10, synthetic: no look-ahead, truth
rule, fair fan, selection, single opening, sealing), `tests/integration/test_beat_agri_ingest.py` (2, fixture mode);
fixtures `tests/fixtures/source/atlas.{usda_wasde_esmis,ncei_cag}` (live captures; XML = sub-report excerpts).

```bash
df -h /                                                                   # >= 5 GB free
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.usda_wasde_esmis   # full archive (~383 requests, 437 MB fetched); a run registers only the releases it read, so the monthly refresh is a full run
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.ncei_cag
uv run python -m reva.cli.beat_agri describe
uv run python -m reva.cli.beat_agri seal                                   # dry run on the WASDE in force
flock /tmp/reva-devdb.lock uv run python -m reva.cli.beat_agri seal --seal # once per new 2026/27 release
```
