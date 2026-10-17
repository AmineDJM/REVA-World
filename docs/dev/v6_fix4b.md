# V6 — FIX4B: prediction-layer honesty fixes from audit AUDIT3 (M3, M4, M5, M6, M7, L3)

Agent FIX4B, 2026-10-17. Input: `docs/dev/review_v4_v5_audit.md` (AUDIT3). Rule followed throughout: append-only —
effect estimates get **new revisions or retractions**, sealed forecast questions get an **appended void
resolution**; no row, manifest or archived object was edited. No migration. Commits `34c4117`/`3c96a2d` (in-flight
work captured by the lead's WIP snapshots), `e44317a`, `fd04e85` and the report commit.

## Summary

| Audit | Defect | Fix | Data written (dev DB `reva`, origin `live`) |
|---|---|---|---|
| **M3** | 4 REVA event-study rows stored a cumulative GDP *level* response under the GDP *growth* concepts; the engine printed it as "% annual growth" | Effect loader + engine refuse level/growth inconsistencies (`reva.effects.consistency`, curated id links in `registry/effects/variables.yaml`); the event-study writer puts a cumulative-growth LHS on the **level** variable; the engine never converts level ↔ growth | 5 new revisions (loads 15–19): coups, 2 earthquake runs → `output.real_gdp_level`; TIES sanctions → `output.real_gdp_per_capita`; all `effect_transform = percent_change` |
| **M4** | ECB +50 bp: alphabetical last tie-break picked the publication-bias-*uncorrected* mean | Rule (5) of `CHOICE_RULE` = declared variant preference (`registry/predict/estimate_preferences.yaml`: bias-corrected > uncorrected, median > mean); a tie left after every rule is a gap `unresolved_tie` — no estimate chosen, never alphabetical; the chosen variant is named in the hop (`preference`) and the variable notes ("variante retenue : …") | registry file only |
| **M5a** | Gutmann et al. "first year of a sanction episode" stored as horizon 1 → put one year late | Convention h = 0 = impact / onset year (documented in `v5_effect_interface.md`); the two single-horizon rows replaced by one lag profile h = 0 (−1.32), h = 1 (−1.50) | load 20: 1 new (`gdp_pc_growth_years_1_2`), 1 revision (`gdp_pc_level_first_two_years`), 2 retracted `withdrawn_by_curator` (`…_year1`, `…_year2`) |
| **M5b** | growth estimate (pp) stored on the GDP-per-capita *level* variable | New growth variables `output.real_gdp_per_capita_growth`, `output.real_gdp_growth` (curated links to `wb.ny.gdp.pcap.kd.zg` / `wb.ny.gdp.mktp.kd.zg`); `growth_rate` on a level variable that has a growth variable is rejected | same load 20; EFFECTS-MORE's Dell–Jones–Olken (6) and Laudati–Pesaran (1) curation rows moved to the growth variables before their load |
| **M5c** | REVA pooled-panel run marked `exact` for RUS | Applicability `exact` only for a single-country scope; `panel_member` = "pooled panel (205 countries; RUS is in the listed sample): an average over the panel, not an estimate specific to RUS"; country group = "pooled panel ('158 countries', verbatim); RUS membership not checked" | — |
| **M6** | 1,978 STEO cells (`estimate_kind = unknown_boundary`, 16 series, 85 after the fetch month) escaped the projection filter | `PROJECTION_SQL` and the engine's baseline filter treat `unknown_boundary` / `published_history_boundary_unknown` as never-observations (the boundary is unknown, so no period can be asserted as observed); same rule as the registry's `PROJECTION_KINDS`. No re-ingest needed (query-time rule) | — |
| **M7** | 6,956 WEO cells of fiscal-year economies (CPI, unemployment, current account, trade volumes; `latest_actual = FY…`, no reporting months on the series) placed on calendar years; 5 sealed questions affected | `atlas.imf` 1.3.0 / parser `atlas_imf/3`: such a series takes the reporting months the same country's GDP series declares (`flags.reporting_year_from`), else `period_basis = fiscal_year_labelled_as_calendar` — never a plain calendar year (`atlas.vintage_weo` 1.0.2 shares the parser); the registry never builds or resolves a calendar-year question from a fiscal-year cell; `void_questions` closes a sealed question as void with a reason by an appended resolution | 5 questions **void** (`fiscal_year_basis_mismatch`), resolution `39b404b0…`, 30 `registry_score` rows (`n_void = 5`, no scores); WEO re-ingest: see §M7 |
| **L3** | 5 REVA-run rows without `shock_profile` | `shock_profile = one_time_impulse` (onset dummy) on every run estimate | in the 5 revisions above |

## M3 — level vs growth

* `reva.effects.consistency.side_problem` (pure) + `Resolver.growth_info` (identifier links only): rejection codes
  `{cause,effect}_level_transform_on_growth_concept`, `…_percent_response_on_growth_concept`,
  `…_cumulative_lhs_on_growth_concept`, `…_growth_rate_on_level_variable`. Growth ids = every `growth_variable`,
  every `growth_concepts` entry, and the atlas concepts the growth variables link to. Variables without a curated
  growth link (e.g. agricultural value added) are not constrained.
* `reva.estimation.run.effect_target`: a `cumsum` run on a growth concept is written on the unique level variable
  whose `growth_concepts` list that concept; no unique level variable → refused (`ValidationProblem`), never
  mislabelled. The effect definition says "written on the level variable … a cumulative level response, not a growth
  rate".
* Engine: `estimates.consistency_problem` adds the same codes to the hop's problems → excluded `not_propagatable`.
  **No conversion**: the growth implied by a cumulative LP profile (β_h − β_{h−1}) is not exactly the growth
  response (each horizon is a separate regression with its own sample and window controls), so it is not computed.
  The pre-existing percent ↔ log-points pairing stays a labelled small-change approximation (unchanged).
* Re-write (no re-estimation; numbers unchanged): `scripts/fix4b/rewrite_eventstudy_estimates.py` reads each run
  document back from the object store by the sha256 its load recorded (`0875ef3f…`, `e9db9c7d…`, `02e00977…`,
  `04330893…`, `88abcba0…`; canonical `$float` tags decoded) and calls the unchanged writer
  (`write_effect_estimates`, `complete=True`). Keys unchanged → `new_revision` 5, retracted 0.
* Check of the whole layer after the fixes: 0 of 301 current estimates violate the rule (SQL in §Commands).

## M4 — declared variant preference

`registry/predict/estimate_preferences.yaml` declares two rules (statement en/fr) and ranks the 10 ECB OP 377
variants. The rule text is REVA's choice, stated as such; the source supports it ("meta-averages are inflated by
publication bias" — the curation note on that entry). Re-run (§7c of `v5_pred_engine.md`): inflation −0.06 pp =
median (mean −0.10 pp listed); real GDP and the consumer price level now come from estimates EFFECTS-MORE loaded
meanwhile (van Els et al. 2001, Angeloni et al. 2002); the Angeloni AWM vs EMM consumer-price responses tie on
every rule → **gap `unresolved_tie`, no number** (owner decision 2).

## M5 — sanctions

Gutmann–Neuenkirch–Neumeier curation (`registry/effects/literature/gutmann_neuenkirch_neumeier_2022.yaml`): one
profile estimate on `output.real_gdp_per_capita_growth`, `irf` h = 0, 1 (unit year, `as_printed` per point, quote
re-found on pdf p.14 by the connector's text check), REVA note on the horizon convention. Engine result for the EU
ban: 2027 −1.32 pp, 2028 −1.50 pp (growth), and separately the REVA run's level response +0.18 % / −0.20 %
(5–95 % −0.38…+0.77 / −0.79…+0.43).

## M6 — STEO unknown boundary

Population re-counted on the latest live `atlas.eia_steo.steo_forecast` (`bdad9160…`): 1,978 `unknown_boundary`
cells, 16 series, 85 with a period from 2026-10 on — all now excluded from observation answers by
`PROJECTION_SQL` (they stay readable when projections are explicitly allowed, still flagged `unknown_boundary`).
The connector already keeps them out of `steo_observed`; no connector change or re-ingest was needed.

## M7 — WEO fiscal years and the 5 sealed questions

* Parser: `weo_country_reporting_years` picks the country's months from `NGDP_RPCH`, `NGDP_R`, `NGDP`, `NGDPD`
  (else any series of that country declaring non-calendar months); the series' own `FY(t/t+1) = CY(t)` /
  `FY(t-1/t) = CY(t)` note still decides the window; no months anywhere → `fiscal_year_labelled_as_calendar`.
  On the fixture: 302 cells (EGY, IND: CPI, LUR, BCA, trade volumes, population) now carry their true fiscal
  window; no FY-series cell is left without a `period_basis`.
* Registry: `questions.is_fiscal_year_cell` drops any cell whose metadata says fiscal year (also cells an older
  parser placed on calendar years) from question generation, training and resolution, with a note.
* The 5 sealed questions (`fq_ce2ce06b59d841b5091a9d62`, `fq_2cd61181ed8f5ad5b27bad82` IND CPI 2026/2027,
  `fq_c1f49d578e1d3333dacee22e` EGY CPI 2027, `fq_7f4930b1540b2285e6aae2a0`, `fq_bd1b7d6205294f4f2ab21f02` EGY
  unemployment 2026/2027; batch `96c6ad48…`) are **void**, reason `fiscal_year_basis_mismatch`, detail naming the
  calendar wording, the FY basis and the reporting year (`scripts/fix4b/void_fiscal_questions.py` →
  `service.void_questions`). The sealed manifest is untouched; the void resolution is an appended artifact
  (`39b404b0…`) plus one `registry_score` row per model; no forecast is scored.
* WEO re-ingest: WEO_REINGEST_PLACEHOLDER

## Tests (all passing)

| File | What |
|---|---|
| `tests/unit/effects/test_fix4b_consistency.py` (8) | growth links, 4 rejection codes, consistent rows pass, unlinked variables unconstrained, event-study level mapping + refusal + shock profile |
| `tests/unit/predict/test_fix4b_engine.py` (7) | level response on a growth concept excluded (never converted); level response propagates with h = 0 in the start year; growth-on-level refused; `panel_member` / group wording; unresolved tie → gap; declared preference picks the bias-corrected median and names it (−0.16 % at 50 bp); preference file rules |
| `tests/unit/test_fix4b_projection_sql.py` (1) | `unknown_boundary` and `published_history_boundary_unknown` never observations (DuckDB on the real SQL; engine baseline filter) |
| `tests/unit/test_atlas_imf.py` (+2) | FY series never on plain calendar years (fixture), country reporting-year selection |
| `tests/unit/test_forecast_registry.py` (+1) | fiscal-year cells dropped from calendar questions with a note |
| `tests/integration/test_forecast_registry_db.py` (+1, throwaway DB) | void: unknown id and missing reason refused before any write; handled questions left alone; appended resolution + score rows |

Existing suites re-run: `tests/unit/predict`, `tests/unit/effects`, `tests/unit/estimation`, registry unit tests —
all pass. `tests/integration/test_forecast_registry_db.py::test_01` fails independently of this work: it expects
5 issue rows per batch, the model registry now has 30 models (`all_models()` grew with other PRED-* work) — owner
PRED-REGISTRY.

## Owner decisions

1. **Shock profile of REVA event studies**: written as `one_time_impulse` (onset dummy D(i,t) = 1 in the onset
   year only; the response includes what the episode does afterwards, later onsets in the window controlled). The
   audit suggested `state_indicator`; that vocabulary means "on while the episode lasts", which the regressor is not.
2. **Ties between models of one paper** (Angeloni et al. AWM vs EMM; also any two estimates equal on all rules):
   now a typed gap. Declare a preference, or run one scenario per model (PRED-ENGINE decision 3)?
3. **Median over mean / bias-corrected first** is REVA's declared choice for meta-analyses; confirm.
4. **Growth variables** `output.real_gdp_growth`, `output.real_gdp_per_capita_growth` link to the WDI growth
   concepts as "the same quantity"; sources on PWT or national accounts differ in coverage (stated in `atlas_note`).
5. **Fiscal reporting months borrowed from the country's GDP series** (same WEO release, by country code): the
   WEO does not repeat the months on CPI/LUR/BCA series. Alternative: label all of them
   `fiscal_year_labelled_as_calendar` without a window.
6. **Edits to EFFECTS-MORE curation** (`dell_jones_olken_2008.yaml`, `laudati_pesaran_2021.yaml`): only the effect
   id moved to the growth variables (required by the new loader rule); EFFECTS-MORE should confirm.
7. Engine expands world quantities (`oil.world_consumption`, `oil.world_production`,
   `output.world_industrial_production`) per country of interest: declare them `global` in
   `registry/predict/variables.yaml` (not changed here).

## Commands

```bash
uv run pytest -q tests/unit/effects tests/unit/predict tests/unit/estimation tests/unit/test_fix4b_projection_sql.py \
  tests/unit/test_atlas_imf.py tests/unit/test_forecast_registry.py
uv run pytest -q tests/integration/test_forecast_registry_db.py::test_05_void_by_registry_rule_is_appended_never_edited
flock /tmp/reva-devdb.lock uv run python scripts/fix4b/rewrite_eventstudy_estimates.py --write   # done (loads 15-19)
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.effects_lit --option entry=gutmann_neuenkirch_neumeier_2022  # done (load 20)
flock /tmp/reva-devdb.lock uv run python scripts/fix4b/void_fiscal_questions.py --write          # done
flock /tmp/reva-devdb.lock uv run reva seed && flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.imf --dataset weo
uv run python -m reva.predict.run --reference all --draws 2000 --seed 0                           # §7c of v5_pred_engine.md
# layer check (expect 0 rows):
# SELECT estimate_key FROM kg.effect_estimate_current WHERE (effect_transform='growth_rate' AND effect_id IN
#   ('output.real_gdp_level','output.real_gdp_per_capita')) OR (effect_id IN ('wb.ny.gdp.pcap.kd.zg',
#   'wb.ny.gdp.mktp.kd.zg','output.real_gdp_growth','output.real_gdp_per_capita_growth')
#   AND effect_transform IN ('percent_change','log_change','log_level'));
```
