# UI check (agent F): build, tests, e2e, scientific honesty, accessibility

Date: 2026-10-15, 00:15–00:37 UTC. Only files under `ui/` were edited, plus this report. Nothing was committed.
The dev database `reva` was not touched.

## Commands run and results

All commands were run in `ui/`. Final results:

| Command | Result |
|---|---|
| `npm run typecheck` (`tsc` for app + node/e2e configs) | pass, 0 errors |
| lint | no lint script or ESLint config exists in `ui/package.json`, so nothing was run |
| `npx vitest run` | **10 files, 59 tests, all pass** (the first run had 8 files and 47 tests; the new tests are listed below) |
| `npm run build -- --outDir <tmp> --emptyOutDir` (tsc + vite build) | pass. Build output went to a scratch or stack directory so the shared `src/reva/api/static` was not overwritten. One existing chunk-size note: `availability` is 1.03 MB, under the 1.6 MB limit |
| e2e stack: `uv run python scripts/e2e_stack.py --stack-file <scratch>/stack.json` (background) | Fresh database `reva_e2e_a7c4a8ab82`. Migrations, seed, offline captured fixtures (worldbank 200, comtrade 282, naturalearth, crossref), `test_fixture` release #1 with 482 estimates, built UI, uvicorn and a real worker thread. Ready in 22 s. Stopped with SIGINT and the **database was dropped** (log: `dropped database reva_e2e_a7c4a8ab82`) |
| `REVA_E2E_STACK_FILE=… PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers npx playwright test` (initial suite) | **25/25 pass** (24 s). The 4 failures listed in `ui/e2e/RESULTS.md` had already been fixed upstream |
| same, final run (suite extended to 34 tests, UI rebuilt into the stack's static dir) | **34/34 pass** (57 s) |

How the e2e runs: `playwright.config.ts` and `e2e/helpers.ts` point the tests at a real API and worker. They do not mock routes. The stack comes from `scripts/e2e_stack.py`, which creates a uniquely named throwaway database the same way the `db` fixture in `tests/conftest.py` does, and the tests read connection details from the stack file. `playwright install` was never run.

## Bugs fixed (ui/ only)

1. **Results "Limits" panel was empty.** `ResultsView` read `outcome.applicability.not_claimed`, but the API returns it at `outcome.applicability.explanation.not_claimed`. The panel now shows the model's own statement ("…not a GDP loss, welfare change, … forecast"), the uncertainty statement and the sign convention. If those are missing it falls back to the per-result `not_claimed` or the static limits text.
2. **Provider-estimated values were shown as plain "selected report".**
   - Country panels: the DZA 2024 crude import of 5,564 US$ has `isReported=false` and `legacyEstimationFlag=6` from the provider, but nothing on screen said so. The panels now load `/v1/observations` and join the provider source flags. The Notes column and the "Latest" headline now show, for example, "provider estimate (legacyEstimationFlag 6)" and "provider returned no rows for this key".
   - Simulation baseline-inputs table: it now shows `applicability.source_flags[M|X|G]`. For example, the CHN 2023 M value is shown as "provider estimation flagged … not a direct customs observation", and the FRA/USA inputs show `isReported=false`.
3. **Chart x-axis repeated years** (2020 2020 2020 2021 …) on short series such as the DZA trade chart. Ticks now fall only on whole years (`incrs`).
4. **Simulation banner** did not say "stress test" or "not GDP loss". The shared `SIMULATION_BANNER_TEXT`, which is also used in the CSV/JSON exports, now reads: "SIMULATION — assumption-conditioned stress test. Values are hypothetical consequences of the stated assumptions; they are not observations or forecasts and not an estimate of GDP loss." The text deliberately has no commas, so the CSV comment line stays unquoted.
5. **Simulation "Valid / reference time: not stated".** The banner now shows the scenario reference year, for example "2023 (scenario reference year; one annual accounting period)", whenever the envelope has no `valid_time`.
6. **DZA `missing_input` rejection was shown only as raw JSON.** It now also has a table captioned "Run rejected: required inputs missing for DZA — a data gap, not zero. No country was dropped and no value was imputed." It lists input, concept, year and state, for example "missing (unavailable)". The raw problem details stay below it.
7. **L2 range could read as an interval around L1** (coordinator item). Under the demo grid the range is one-sided. Each L2 cell now works out where L1 sits by an exact decimal comparison (`pointInRange`, with no float rounding) and states it, for example "L1 = upper end under this grid (one-sided range, not centred on L1)", or "lower end" for export changes. Nothing uses ± or a centred bar. The label now reads "assumption sensitivity range (not a confidence interval)".
8. **Promotion gates** (coordinator item). `needs_review` now has its own badge ("? Needs human review", amber), separate from pass ✓, fail ✗ and blocked ■. Each badge has a text label and a glyph, so colour is not the only cue, and unknown values show as "Unrecognised decision: x". Proposals show a tier badge (T0–T3) and the tier description. The copy reads "T1 within the signed policy scope promotes automatically without human review; T2/T3 require human review." A note appears when any gate returned `needs_review`.
9. Mobile: field hints and errors inside labels are now `display:block`. Before this, the multiplier hint sat inline next to the input at 375 px.

## Tests added

- `ui/src/components/ProvenanceDrawer.test.tsx` (coordinator item, P5 "UI drill-through" and §13 provenance drawer). It drills from an estimate to the observation and its selected raw field (`raw_selector` with `countryiso3code`), then to the acquisition (`raw_sha256`), the raw artifact (SHA-256 and edge selector) and its storage. It then follows "Trace from this acquisition", which opens a new lineage rooted at the acquisition with the same raw SHA and the source. It also checks Back, and that Escape closes the drawer and restores focus. There are also tests for the error state and for a missing provenance id.
- `ui/e2e/country.spec.ts` provenance test, extended against the real API:
  - the observation's selected field contains `countryiso3code`;
  - the acquisition's SHA-256 equals the API `raw_sha256`;
  - "Trace from this acquisition" opens a lineage rooted at the acquisition, with the raw artifact, connector version and `worldbank.wdi` source, and no observation;
  - Back works;
  - focus returns to the Trace button.
- `ui/e2e/country.spec.ts` DZA test: every value the provider flags as estimated shows the "provider estimate" note.
- `ui/e2e/simulation.spec.ts`:
  - L1 test: the banner wording (stress test, not forecasts, GDP loss), the non-empty result limits ("not a GDP loss") and the reference year;
  - DZA test: the missing-inputs table lists DZA only and shows "missing (…)";
  - L2 test: the France net-cost L1 position matches the API values, and there is no ± in the table.
- `ui/e2e/a11y_mobile.spec.ts` (new, 9 tests), run on every screen:
  - every visible input, select, button, link and summary has an accessible name, and every table has a caption or name;
  - at 375×812 there is no horizontal page scroll and no element sticks out past the viewport unless it sits in a scroll or clip container;
  - full-page screenshots at desktop and 375 px are written to `test-results/`;
  - a visible focus indicator is checked on the skip link, nav link, release select, title input, country checkbox, L2 radio and submit button.
- Unit tests:
  - `GateBadges.test.tsx`: decisions, tiers, and an Acquisition page with a T2 proposal and a `needs_review` gate;
  - `format.test.ts`: `sourceFlagNotes`, `compareDecimalStrings`, `pointInRange`;
  - `SimulationPage.test.tsx`: the missing-inputs table, limits, uncertainty, sign convention, provider flags and L1 position;
  - `ContextBanner.test.tsx`: the banner wording.

## Scientific-honesty review (state after fixes)

| Requirement | Status |
|---|---|
| Simulation is labelled an assumption-conditioned stress test, not a forecast and not GDP loss | Pass. A sticky screen banner, a red context banner, the "Limits of this model" panel, the result Limits from the API's `not_claimed`, and the CSV/JSON exports all say so. |
| DZA is a data gap, not zero | Pass. Country page: 2020–2023 render as `missing (unavailable)` with `data-missing-reason`, plus the "shown as gaps, not zero" summary and the "Data gaps for DZA" table. The 2024 value is shown together with its provider-estimate flag. Simulation: the DZA note before submit, then a 422 `missing_input` shown as a data-gap table, with no job and no results. |
| L2 sensitivity ranges are ranges with no confidence level | Pass. "assumption sensitivity range (not a confidence interval)"; no level, %, ± or centred bar; L1's position is worked out from the data. The e2e test checks that the served `level` is null. |
| test_fixture vs live origin | Pass. "TEST FIXTURE DATA" label on every screen and every panel banner, the origin field, and a "TEST FIXTURE" suffix in the release selector. |
| Uncertainty kinds shown | Pass. Badges with distinct treatments: "Uncertainty not quantified" with the note "does not mean exact", sensitivity range, prediction, bounds and exact-by-definition. The L2 uncertainty statement now also appears in the result Limits. |

## Accessibility and mobile (Playwright, Chromium)

- Every screen passes the accessible-name and table-caption checks.
- Keyboard: the existing test passes (skip link, the 7 nav links, form controls, Enter on a link, skip link focuses `#main`). A visible `:focus-visible` outline (3 px) is confirmed on the main controls.
- At 375 px there is no horizontal page scroll on any of the 8 screens, nor on the L1/L2 simulation results. Wide tables scroll inside `.table-wrap`.
- Screenshots (gitignored, not committed):
  - `ui/test-results/a11y_mobile-*/desktop-*.png` and `mobile375-*.png`, from the final suite run;
  - `ui/test-results/ui-check/`: `dza-crude-panel.png`, `sim-l1-results-desktop.png`, `sim-l1-results-mobile375.png`, `sim-l2-table.png`, `sim-l2-mobile375.png`, `acquisition-desktop.png`.

## Remaining issues

UI:
- uPlot creates `Intl.NumberFormat(navigator.language)` when the module loads. In a browser whose locale tag is invalid (for example `en-US@posix`, which Chromium reports here when no locale is set) every chart falls back to "Chart unavailable in this browser; see the table". The tables still carry the values. The e2e device profile sets `en-US`, so CI is not affected. A fix would be to load uPlot behind a locale guard, or to patch the locale before import.
- Simulation outputs print every decimal the API returns (for example `57,082,396,439.600717 US$`). This implies more precision than the inputs justify. Rounding for display (exports would keep full precision) is a product decision and was not changed.
- Full-page Playwright screenshots of the World map show a partial MapLibre canvas. This only happens while the viewport is resized for the capture; the normal viewport render is complete.
- The country-profile banner reads "Origin: no records" next to the TEST FIXTURE label, because the entity envelope has `origin: none` while the baseline release is a fixture.
- There is no e2e test for the gate badges against the real stack, because the stack has no proposals. The unit tests cover them.

Backend gaps (need owner action, not edited here):
- **API (E):** `/v1/beliefs` estimates and `/v1/timeseries` points do not carry provider `source_flags`, so the UI makes an extra `/v1/observations` request per panel to show "provider estimate / not directly reported". Suggestion: add `source_flags`, or a reduced `provider_flags`, to `BeliefEstimate` and `TimeseriesPoint`.
- **API (E):** the simulation envelope has `valid_time: null`. It could carry the scenario reference-year interval, and the UI would then use it instead of its own label.
- **API (E) / simulation (C):** in L1/L2 results, `uncertainty.status="unquantified"` with `kind="unknown"` sits on deterministic accounting outputs. The description explains this, but a separate status such as "conditional on assumptions; inputs unquantified" would read better. Optional.
- **Country envelope (E):** `GET /v1/entities/{id}` reports `origin: none` even though it is served from a fixture-only instance (see the UI note above).

## Files changed

- `ui/src/components/ContextBanner.tsx`, `ValueView.tsx`, `TimeSeriesChart.tsx`, `GateBadges.tsx` (new)
- `ui/src/pages/SimulationPage.tsx`, `CountryPage.tsx`, `AcquisitionPage.tsx`
- `ui/src/lib/format.ts`, `ui/src/styles.css`
- Tests:
  - `ui/src/components/ProvenanceDrawer.test.tsx` (new)
  - `ui/src/components/GateBadges.test.tsx` (new)
  - `ui/src/components/ContextBanner.test.tsx`
  - `ui/src/lib/format.test.ts`
  - `ui/src/pages/SimulationPage.test.tsx`
  - `ui/e2e/a11y_mobile.spec.ts` (new)
  - `ui/e2e/country.spec.ts`
  - `ui/e2e/simulation.spec.ts`
