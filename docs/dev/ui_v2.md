# UI V2 (agent UIR): world map, mirror view, scenario composer, multi-period and IO results, ⌘K

Date: 2026-10-15, 00:59–02:25 UTC. Only `ui/**` was edited, plus this report. Nothing was committed by this agent; the lead's WIP snapshots picked up the files from the shared tree. The dev database `reva` was never touched: every check ran on throwaway `reva_e2e_*` databases, and each was dropped on SIGINT.

The UI language stays English, which is what the UI already used. No i18n framework exists, so none was added. The composer accepts French text and shows the server's restatement in the input language (`lang="fr"`).

## Results

All commands were run in `ui/`.

| Command | Result |
|---|---|
| `npm run typecheck` (app and node/e2e configs) | pass, 0 errors |
| `npx vitest run` | **22 files, 144 tests, all pass** (round 2 ended at 12 files, 87 tests) |
| `npm run build -- --outDir <scratch>/ui-build --emptyOutDir` | pass. `src/reva/api/static` was not written |
| `npm run gen:types` | `src/api/openapi.gen.ts` regenerated from `contracts/openapi.json` (APIX fields, `ScenarioDraftResponse`, `period_start`/`period_end`) |
| e2e on the recorded-LLM stack (`uv run python ui/e2e/recorded_llm_stack.py --stack-file <scratch>/stack.json --static-dir <scratch>/ui-build`), then `REVA_E2E_STACK_FILE=… PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers npx playwright test` | **53 passed, 1 skipped** (59 s). The skipped test is the "drafting disabled" path; it only runs on a stack without a drafter |
| V2 specs on the plain stack (`uv run python scripts/e2e_stack.py …`) | 7 passed, 8 skipped. Each skip states its reason: no recorded drafter, or the API's own `missing_input` refusal (no dataset inputs pinned). The "drafting disabled: 503" test passes here |

### The e2e stack wrapper: `ui/e2e/recorded_llm_stack.py`

This wrapper runs `scripts/e2e_stack.py` unchanged. That script is owned by agent I, so the wrapper patches it in-process.

- **LLM drafts are replayed, never live.** `app.state.nl_drafter_factory` is set to `RecordedDrafter(tests/fixtures/nl/recorded)`, as the NLC API tests do. No live LLM call is ever made.
- **LLM budget ledger.** If `ops.reserve_llm_budget` is absent from the throwaway database, `LEDGER_FUNCTIONS_SQL` is installed. This is a no-op once 0008 ships the functions.
- **V2 dataset inputs are ingested offline before the release is published**, so the release pins them:
  - the recorded Eurostat labour/pensions fixture, with its dataset hook;
  - the recorded Eurostat demography fixtures for LI and LU (windowed) and for FR (`tests/fixtures/social`);
  - the **synthetic** 3-economy × 2-industry FIGARO-format table from `tests/fixtures/io_tables/synthetic.py`, through the real connector, identity checks and `publish_io_dataset`.
- **Release cutoff.** The wrapper moves the release cutoff after these ingests, because the stack computes its cutoff before the hook runs.

These datasets are test fixtures and are labelled TEST FIXTURE on every screen. The IO numbers come from a synthetic table, not from FIGARO figures.

The full-plan V2 recorded fixtures cannot be ingested with their default plans: they were captured for restricted plans, so most requests have no captured response. Those fixtures are `worldbank.wdi.all_economies`, `un.comtrade.public_batch`, `eurostat.demography` and `eurostat.figaro.iciot_ii`. Pass `--no-v2-fixtures` to skip the dataset ingests.

## Screens (new routes and nav: Map, Compose, Runs; `/runs/:id`; ⌘K palette in the header)

### 1. World map, `/map?sim=<job>&focus=<entity>&flow=M|X`

Files: `pages/MapPage.tsx`, `components/ExposureMap.tsx`, `lib/worldmap.ts`, `lib/colorScale.ts`.

- **Run picker.** It lists succeeded `economic.crude_exposure` runs, from `/v1/jobs` and `/v1/scenarios`.
- **Choropleth.**
  - Colours come only from the run's own L1 `exposure_percent_baseline_gdp` rows.
  - The palette is diverging, with blue for net exporters, a grey midpoint and red for net importers.
  - Breaks are fixed (0.25, 1, 2.5, 5 %), and classes use exact decimal comparison.
  - The legend prints every range.
- **Countries without complete inputs** are drawn with a **hatched pattern** (a texture, not a colour class).
  - They come from `outcome.applicability.countries[iso].status` and `skipped_countries`, using `subject_id` when it is given.
  - Hovering shows the typed reason, for example "Algeria: skipped: inputs incomplete — M · trade.hs2709.import_value.world_total 2023 — missing: unavailable. Not zero."
  - Other countries are shown as "not in this run" or as display features without a reviewed entity.
- **Partner arcs** are great-circle arcs, with width ∝ √value, for the selected country and flow.
  - Relations come from **the run's baseline release** (release-pinned `/v1/relations`). Where the release records no value, evidence as of the scenario cutoff fills it, and that row is marked.
  - Partner "World" is never a partner row or an arc.
  - Composite or aggregate partners (for example "Areas, nes") are listed and never mapped.
  - A reporter that lists itself as a partner (re-imports) is listed and not drawn.
  - A partner with no reviewed display geometry is "listed only".
  - The table shows the largest 12 partners and has a "Show all" button.
- **Text alternative.** A country table carries exact values in `data-exact` and Trace buttons. The map has `role="img"` and a summary label.
- **Reduced motion.** `prefers-reduced-motion` sets the MapLibre fade duration to 0.
- **Truncation.** A `results_truncated` warning is shown.

### 2. Mirror view (`components/MirrorPanel.tsx`)

The mirror view is shown on `/country/:iso3` and opens on the map when a country has no result.

- It shows what other reporters say about their crude trade with the country.
- Every row says **"reported by partner"**.
- The explanation covers valuation (CIF vs FOB), timing and coverage, and says the figures are never used to fill the country's own cells, map polygon or inputs, and never summed.
- e2e checks the DZA rows against `/v1/relations` and checks that DZA's own crude-import cells stay missing.

### 3. Scenario composer, `/compose`

Files: `pages/ComposerPage.tsx`, `components/SpecEditor.tsx`, `lib/draft.ts`.

- A permanent notice reads "Draft written by a language model. Nothing runs until you confirm."
- **Request.** `POST /v1/scenario-drafts` sends `{text, baseline_release_id}` with an Idempotency-Key.
- **Review screen**, built to the real `ScenarioDraftResponse`:
  - status (`draft_ready`, `needs_input`, `draft_invalid`, `draft_rejected` or `nothing_draftable`) and "nothing executed";
  - the **server restatement**, which is authoritative, and separately, collapsed, the **LLM's own paraphrase** marked untrusted;
  - provider and model (for example `openai · gpt-4.1-2025-04-14`), the requested model, prompt version and sha, tokens, latency and any fallback;
  - **capability-gap chips** (key, quoted text, reason, source), with policy refusals styled separately;
  - `numbers_to_confirm` with their provenance; **"not in the text" is highlighted**;
  - `assumptions_added`, warnings, and validator errors;
  - the **experimental-model flag**, from the `experimental_model:` warning or a non-crude `model_key`.
- **Editing.** The draft spec is shown as a **readable typed form**: numbers stay numbers and lists stay lists, and the identity and release-pinning keys are read-only.
  - `needs_input` gives one empty input per `missing_fields` path. Nothing is pre-filled.
- **JSON diff.** One diff compares the draft with what Confirm will submit, including the pinned release. A second compares the previous draft with the current one. The exact JSON that Confirm will submit is also shown.
- **Confirm.** **Confirm is the only button that posts.** It sends `POST /v1/scenarios` and then `POST /v1/simulations`, and navigates to `/runs/<job>`.
  - Confirm is disabled for a candidate spec until the user edits it.
  - A `missing_input` refusal is shown as the data-gap table.
- **Typed messages, never a silent fallback**, for:
  - 503 `llm_unavailable`
  - 429 `budget_exhausted`
  - 502 `llm_provider_error`
  - 413, 409, and 404/405 (endpoint absent)

### 4. Multi-period results, `/runs/:id` (`pages/RunPage.tsx`, `lib/runviews.ts`, `PeriodChart`, `PopulationPyramid`, `BacktestPanel`)

- **Period grouping.** Rows are grouped by `period_start`/`period_end`, which are half-open; tables print `[start, end)`.
- **Labels.** "Conditional projection under stated assumptions, not a forecast" and "Experimental model — maintainer review pending" (showing the API approval status).
- **`demography.cohort_component`:**
  - population pyramid with males on the left and females on the right, from the single-age outputs `population.<sex>.age_NNN[_plus]`, falling back to disjoint broad groups;
  - the scale is fixed across years;
  - a **year slider** (range input with `aria-valuetext`) and an output element;
  - a table alternative;
  - an indicator selector with a chart.
- **`social.retirement_age`:**
  - one chart per output (employment 55–69, change, recipients, dependency ratio, expenditure);
  - the model's own `output_label` is used;
  - expenditure is badged **approximate**;
  - L2 runs have no L1 rows, so the line is the stated central grid point (`outcome.applicability.countries[iso].central_grid_point`), named on screen. Without a stated central point no line is drawn.
- **Charts.** These are SVG.
  - The L1 or central line is 2 px.
  - The L2 `sensitivity_range` rows form a **ribbon**, labelled "assumption sensitivity range (not a confidence interval)"; it is never centred and never shown with ±.
  - Missing years appear as gaps.
  - A hover or keyboard crosshair (←/→/Home/End) feeds an `aria-live` readout.
  - Each chart has a table with the central point's position in the range.
- **Back-test panel.**
  - It renders `outcome.applicability.backtest` (or per-country back-test blocks), in the cohort `backtest()` shape or as flat rows, showing predicted, observed and the error **as served, including when it is bad**.
  - When none is attached it says so and points to `docs/dev/v2_pop_backtest.md` or `docs/dev/v2_pen_backtest.md`.
  - It lists the count from `/v1/models/{id}/evaluations`.
- A `results_truncated` warning gives an "Incomplete listing" notice.

### 5. IO price propagation (`economic.io_price` on `/runs/:id`, `components/IoHeatmap.tsx`)

- **Label.** "First-round cost-push, full pass-through; not an inflation forecast". The L2 label is used for L2 runs. The label also says fixed coefficients, no substitution, and that the basket is not a CPI.
- **Stated shocks**, with the note that NACE B is broader than crude petroleum.
- **Household basket table**: one row per country with the L1 change, the L2 sensitivity range and its one-sidedness note, and the decomposition (domestic and imported products, domestic and foreign shocks).
- **Country × industry heatmap.**
  - Every cell carries screen-reader text and `data-exact`.
  - Shocked cells are outlined.
  - Missing cells and cells with no output are hatched.
  - Breaks are fixed per run, and a hover readout shows the exact value.

### 6. ⌘K command palette (`components/CommandPalette.tsx`, `lib/palette.ts`)

- **Open and close.** Ctrl+K or ⌘K, or the header "Search" button.
- **Search.** The search is deterministic, with no LLM:
  - it folds case and diacritics;
  - scoring is exact > prefix > word prefix > substring > title subsequence, and every term must match;
  - ties break by kind, then title, then id.
- **Sources.** Countries (`/v1/entities`), concepts (`/v1/ontology`, which open `/evidence?concept=`), releases (which open `/simulation?release=`), runs (`/runs/:id`) and screens. Only GETs are made. A source that fails is named and not hidden.
- **Keyboard and focus.** Combobox and listbox pattern with `aria-activedescendant`, ↑/↓/Enter, Escape that restores focus, and a Tab trap.

## APIX items integrated (coordinator message)

1. **Source flags.** Country panels read `source_flags` and `source_flags_truncated` from `/v1/beliefs` and `/v1/timeseries`. The extra `/v1/observations` call per panel is gone; e2e asserts that no such request is made.
   - `provider_value_estimated: null` reads "estimation status undeterminable from provider flags".
   - A truncation note links to Trace.
2. **Valid time.** `validTimeLabel` formats the envelope window, for example "2023 (scenario reference year) · [2023-01-01, 2024-01-01)", honouring `bounds`.
3. **Stale and paused publication.**
   - A stale badge shows `stale_reason`.
   - A "repair proposal" link goes to `/acquisition?proposal=…`; the matching card is outlined and scrolled to.
   - "served from release #N" is shown when `served_from_release_id` differs from the baseline release.
   - A banner reads "PUBLICATION PAUSED" for the `publication_paused` warning.
4. **Pagination.** Belief cursors: the UI does not paginate beliefs (single bounded page), so there is no cursor to restart.
5. **Missing inputs.** `MissingInputs` reads the V2 `missing_input` row shape (`reason`, `reference_year`) as well as the V1 shape.

## Accessibility and mobile

- Every V2 screen is in the a11y and 375 px e2e loop (`V2_SCREENS`): every control has an accessible name, every table has a caption, and nothing overflows horizontally.
- The map at 375 px with a run loaded is tested separately.
- Cohort, IO and retirement run pages were checked at 375 px with screenshots; there is no horizontal scroll, and wide tables and the heatmap scroll inside `.table-wrap`.
- Charts are focusable and keyboard readable.
- Colour is never the only cue:
  - hatching marks gaps;
  - outlines mark shocked cells;
  - text marks approximate outputs;
  - legends always list each range.
- The global `prefers-reduced-motion` rule applies, and the map fade is 0 under reduced motion.

## Prototype design intent (showcase) vs what was built

| Intent | Status |
|---|---|
| Absence as an object | Built: hatched countries with typed reasons, MissingValue, the data-gap table, and partner rows that are "not reported" |
| Mirror statistics | Built (map and country page) |
| Sensitivity grid / one-sided L2 range | Built: ribbons and ranges labelled "not a confidence interval", the central point's position stated in words, and the IO one-sidedness note |
| Provenance X-ray | The existing drawer and Trace buttons are on every new value. The release node shows `dataset_artifacts_pinned` |
| Dot-matrix world | Not built: MapLibre polygons from the ingested Natural Earth geometry were used, as the task asked |
| Live shock slider | **Deliberately not built.** It would make the UI compute results that no recorded run produced. A slider is used only for the pyramid year |

## Backend gaps found (sent to the coordinator; status)

- **Fixed.** Results lacked `period_start`/`period_end` (now served, along with the `results_truncated` warning).
- **Fixed.** Envelope `assumptions` were crude-specific for every model ("import xNone …").
- **Fixed.** Release-pinned relations served `value: null`.
- **Fixed.** The estimate provenance walk was truncated by release dataset artifacts before reaching the observation. This broke `country.spec.ts` "provenance drawer …" on a stack with datasets; it passes now.
- **Open.** No API field serves back-test results (POP and PEN write them to `docs/dev/v2_*_backtest.*`; `/v1/models/{id}/evaluations` returns 0 for the V2 models).
- **Open, for the e2e_stack owner (I).** `scripts/e2e_stack.py` computes the release cutoff before ingesting and has no V2 fixture or recorded-drafter mode. `ui/e2e/recorded_llm_stack.py` works around both and could be merged into the script.
- **Open, for the MIGR owner.** The stack logs `schema drift … not in contract: ops.reserve_llm_budget/settle_llm_budget` when the ledger functions are installed by the wrapper. If 0008 now ships them, regenerate `migrations/schema_contract.json`.

## Files

- **New:**
  - `src/api/v2types.ts`
  - `src/lib/{worldmap,colorScale,palette,draft,runviews,useSimulation}.ts`
  - `src/components/{ExposureMap,MirrorPanel,CommandPalette,SpecEditor,HonestyLabels,PeriodChart,PopulationPyramid,IoHeatmap,BacktestPanel}.tsx`
  - `src/pages/{MapPage,ComposerPage,RunsPage,RunPage}.tsx`
  - `src/test/v2fixtures.ts`
- **New tests:**
  - `src/lib/{worldmap,colorScale,palette,draft,runviews}.test.ts`
  - `src/components/CommandPalette.test.tsx`
  - `src/pages/{ComposerPage,RunPage,MapPage,CountryPage}.test.tsx`
  - `e2e/{v2helpers.ts,v2_map.spec.ts,v2_composer.spec.ts,v2_runs.spec.ts,recorded_llm_stack.py}`
- **Changed:**
  - `src/App.tsx` (routes, nav, palette)
  - `src/api/{client,types,openapi.gen}.ts`
  - `src/lib/format.ts` (valid time, undeterminable, persons display)
  - `src/components/{ContextBanner,ProvenanceDrawer}.tsx`
  - `src/pages/{CountryPage,SimulationPage,EvidencePage,AcquisitionPage}.tsx`
  - `src/styles.css`
  - `e2e/helpers.ts`, `e2e/a11y_mobile.spec.ts`, `e2e/country.spec.ts`
  - `src/lib/format.test.ts`, `src/pages/SimulationPage.test.tsx`
