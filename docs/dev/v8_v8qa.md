# V8QA — integration, build and end-to-end audit of the V8 console

Agent V8QA, 2026-10-18. The audit passed, so `/` now redirects to `/v8`. The old screens keep their paths
(`/world`, `/country/…`, `/v3/…`, …). The V8 rail's « Anciens écrans » link and the palette command now point to
`/world`.

## Verdict

| | |
|---|---|
| `npm run build` (tsc + vite) | **pass**, tsc clean |
| Bundle (`node ui/scripts/v8-bundle-report.mjs`, gzip) | `/v8` frame **115.7 KB JS** (budget ≤ 150) + 12.3 KB CSS; route Δ 66–85 KB (budget ≤ 120); Liens 205 and Monde 359 (budget ≤ 420 with a map or graph). No budget exceeded. |
| Real-data tour `ui/e2e/v8_tour.spec.ts` (dev DB, built UI, signed-in session) | **23/23 pass**: 21 stops, a keyboard-only path and the `/` → `/v8` switch |
| UI unit tests (`npx vitest run`) | 462 passed, 1 skipped (59 files); `src/v8`: 194 passed |
| Python | `tests/invariants` + `tests/unit/v8`: 355 passed. `tests/integration/test_api_contract.py`: 3 passed. `test_forecast_registry_db`: 5 passed. `test_fix5_projection_kinds`: 21 passed. |
| `/v8` LCP (built UI, real data, 3 runs) | 624 / 712 / 640 ms, CLS 0.026–0.084. Route switch 35–66 ms per feature. |
| Phase 3 switch | **done** (`App.tsx`) |

## Pass/fail per feature × check (live dev DB, Chromium, 1440 px and 390 px, light and dark)

« key » means the answer is on screen within the time shown. « 0 err » means no console error, no page error and no
5xx on `/v1`. « 390 » means no horizontal scroll at 390 px. « names » means every visible button, link and field has
an accessible name. « artefacts » means no NaN, `undefined`, `[object Object]` or `Infinity` is rendered. « honesty »
is a manual review of the screenshots.

| Stop (real id) | key (ms) | 0 err | 390 | names | artefacts | light / dark / phone | honesty |
|---|---|---|---|---|---|---|---|
| Accueil `/v8` | 615 | ✓ | ✓ | ✓ | ✓ | ✓ ✓ ✓ | ✓ unavailable tiles hatched; counts come from the registry |
| Fiche country FRA | 820 | ✓ | ✓ | ✓ | ✓ | ✓ ✓ ✓ | ✓ chip on each stat; fixed: text facts showed Russian first (« французы »), now French, then English |
| Fiche commodity crude_oil | 644 | ✓ | ✓ | ✓ | ✓ | ✓ ✓ ✓ | ✓ « lu hors instantané atlas » shown; REVA anchor page labelled |
| Fiche drug chembl:CHEMBL941 | 562 | ✓ | ✓ | ✓ | ✓ | ✓ ✓ ✓ | ✓ ChEMBL `lic.` flag on every value |
| Fiche armed_conflict ucdp:conflict:13243 | 579 | ✓ | ✓ | ✓ | ✓ | ✓ ✓ ✓ (phone crumb fixed) | ✓ UCDP interval shown |
| Fiche disease mondo:0005136 | 545 | ✓ | ✓ | ✓ | ✓ | ✓ ✓ ✓ | ✓ CIM-10/OMIM hatched « manquant : la source ne publie pas… » (title « malaria » is the source label) |
| Monde GDP per capita | 669 | ✓ | ✓ | ✓ | ✓ | ✓ ✓ ✓ | ✓ missing hatched; 65 places listed as « sans valeur » |
| Monde IMF inflation | 989 | ✓ | ✓ | ✓ | ✓ | ✓ ✓ ✓ (**empty-map bug fixed**) | ✓ projections hidden by default (10 counted) |
| Comparer FRA/DEU/ITA × 2 indicators | 588 | ✓ | ✓ | ✓ | ✓ | ✓ ✓ ✓ (dark kbd fixed) | ✓ projection badge, dashed after the last observation |
| Liens imatinib → CML | 654 | ✓ | ✓ | ✓ | ✓ | ✓ ✓ ✓ | ✓ « pas une preuve causale », restrictive licence listed |
| Demander « prix du Brent » | 728 | ✓ | ✓ | ✓ | ✓ | ✓ ✓ ✓ | ✓ « hors instantané » note; projections are never the latest value |
| Demander brainstorm « prix du Brent » | 2,444 | ✓ | ✓ | ✓ | ✓ | ✓ ✓ ✓ | ✓ verdicts reworded (below) |
| Décision OPEP+ −2 mb/j | 574 | ✓ | ✓ | ✓ | ✓ | ✓ ✓ ✓ | ✓ « scénario non validé — pas une probabilité », « pas un conseil financier », missing hatched |
| Scores | 652 | ✓ | ✓ | ✓ | ✓ | ✓ ✓ ✓ | ✓ wins reworded (below) |
| Scores track beat_energy | 665 | ✓ | ✓ | ✓ | ✓ | ✓ ✓ ✓ | ✓ |
| Tableaux | 524 | ✓ | ✓ | ✓ | ✓ | ✓ ✓ ✓ | ✓ empty state, no demo board |
| Couverture | 532 | ✓ | ✓ | ✓ | ✓ | ✓ ✓ ✓ | ✓ « Couverture ≠ vérité » |
| Dossier index | 529 | ✓ | ✓ | ✓ | ✓ | ✓ ✓ ✓ | ✓ |
| Dossier FRA | 660 | ✓ | ✓ | ✓ | ✓ | ✓ ✓ ✓ | ✓ |
| Dossier crude_oil | 699 | ✓ | ✓ | ✓ | ✓ | ✓ ✓ ✓ | ✓ BEAT sentence fixed (below) |
| Dossier malaria | 645 | ✓ | ✓ | ✓ | ✓ | ✓ ✓ ✓ | ✓ « somme REVA des estimations-pays » labelled |
| Keyboard only | — | ✓ | | | | | Ctrl-K → type → Enter; `g m`; Tab reaches the rail with a visible focus ring; `j` focuses a Fiche section |
| `/` → `/v8`, `/world` and `/v3/` still reachable | — | ✓ | | | | | |

The timings are for warm caches. The first tour run on a fresh process gave 0.5–4.4 s (Dossier crude_oil 4.4 s,
Demander 2.4–3.4 s). Screenshots of every stop in light, dark and phone (63 files) are in
`…/scratchpad/v8/V8QA/shots/`.

## Phase 1 — integration fixes (each has a test)

1. **Shell route shadowing.** `discovery.navTargetRoutes()` now adds a nav-target route only when all of these hold:
   the feature is visible, it has params, and no feature route (including its own param route) already answers
   that URL. A deep `navPath` such as `/v8/x/country/FRA` now reaches the param route with its params.
   - Tests: `discovery.test.ts`, `V8App.test.tsx` (« a deep navPath … gets its params »).
   - Related breadcrumb fixes: crumbs that no route answers are no longer links (`/v8/fiche/country` was a dead
     link). Kind segments are in French (pays, matière première, médicament, maladie, conflit…). Only the last
     crumb carries `aria-current`.
2. **Épingler.** New public `features/tableaux/PinButton.tsx`:
   - `<PinButton item={…}/>` lazy-imports `pin.ts` on click and reports in a polite status.
   - `linkNote(label, href, lines)` builds a note pin that points back to the exact V8 URL. Pins store references,
     never values.
   - Added to Monde (map URL, plus a series pin of the selected country when the series has a catalog concept),
     Comparer (exact URL), Scores (track view and cell drawer), Liens (found path) and Dossier.
   - Fiche, Demander and Décision already had one.
   - Tests: `PinButton.test.tsx` (3) and assertions in the monde, comparer, scores, liens and dossier tests.
   - Not clicked on the live server: the boards store is in the dev DB, which is read-only for agents.
3. **`v8Links.compare`.** Demander (`links.ts`) and Monde (`PlacePanel`) use it. Tests are in demander and monde.
4. **BEAT wording.**
   - **Dossier crude_oil:** `beat.tests` now returns:
     - `n_win_tests` (2 of 6 absolute-error tests);
     - `scope`: « le mois en cours seulement (h = 0) », computed from the wins' horizons;
     - `reading`: « un avantage de fraîcheur entre deux publications de la prévision officielle (REVA lit des prix
       quotidiens publiés après elle), pas une meilleure prévision des mois suivants »;
     - `caveats`: the French texts of `registry/scoreboard/tracks.yaml`, quoted for the « bat » rows of that series.
     - The summary and section sentences in `registry/dossier/commodity.yaml` use these values, and the cache
       invalidates because the template version is a content hash.
   - **Scores:**
     - Each win line says « la combinaison qui utilise la prévision FMI (WEO) bat FMI (WEO) seul — avantage de
       fraîcheur … pas une meilleure prévision en général » when every win in the group uses the official
       forecast. A mixed group says « N tests battent X (dont k combinaisons qui utilisent sa prévision) ».
     - The headline adds « Parmi les 7 victoires : 3 par une combinaison … ; 7 grâce à un avantage de fraîcheur … ».
   - **Demander brainstorm:** example verdicts are worded the same way server-side (`_verdict_wording`). They now
     show the horizon and the quoted caveats.
   - **Unchanged:** Accueil and the Fiche forecasts section render only per-question statuses (« une question ne
     prouve rien »), not track verdicts.
   - Tests: `tests/unit/v8/test_v8qa_honesty.py`. It also checks that every headline win of a combination or
     post-processed candidate carries `uses_official_forecast`.
5. **gzip.** `Compression` is Starlette `GZipMiddleware` with `minimum_size` 1 KB.
   - It is placed inside the security-header middleware, so it sees each response's single body and the minimum
     size applies. Outside, Starlette treated the `BaseHTTPMiddleware` stream as streaming and compressed even
     15-byte bodies.
   - `/v1/session` is never compressed: its body carries the CSRF token (BREACH).
   - France Fiche bundle: **312,874 B → 43,172 B (7.2×)**, about +12 ms of CPU on loopback.
   - Tests: `test_v8qa_gzip.py` (4), and `tests/security/test_api_adv_http_surface.py` + `test_auth_session.py`
     still pass (59 passed, 5 skipped).
6. **Start-up warm-up.** `src/reva/api/warmup.py`, one lifespan hook. One daemon thread runs the warmers in order,
   each in try/except with a log line; the result is in `app.state.v8_warmup`.
   - Warmers: `forecast_registry` (PERFQ's manifest index via `R.questions(conn, store, None, limit=1)`) plus every
     `reva.api.routes.v8_*` module that exposes `warm(app)`. Added for `v8_scores` (live state, background),
     `v8_series` (snapshot + series index), `v8_paths` (path index load or build, no wait), `v8_entity` (Fiche
     forecast index) and `v8_decision` (engine warm-up).
   - Switch: `REVA_V8_WARMUP`; off under `REVA_ENV=test`.
   - `/health/ready` answered in **31 ms** with the warm-up running.
   - Measured warm-up durations: registry 5.8 s, paths 1.8 s, series 33.8 s, the others start their own threads.
   - First calls 60 s after start, with vs without warm-up: scores/live **0.44 vs 2.90 s**, Monde map **1.80 vs
     5.44 s**, paths **0.05 vs 1.38 s**, Fiche FRA **0.87 vs 3.59 s**.
   - Tests: `test_v8qa_warmup.py` (4: non-blocking, failure isolation, env switch, discovery).
7. **Monde « empty map ».** Reproduced on live data with IMF inflation (`imf.weo` PCPIPCH): Venezuela at 252 %
   stretched the symmetric diverging scale (± max/8 neutral band), so about 170 countries fell into the neutral grey
   and the map looked empty. The diverging breaks are now quantiles of |v| (fifths), symmetric around 0, and the
   extreme value sits in the extreme class. Colours follow the number of classes. Tests are in `monde.test.tsx`
   (outlier case, few-magnitude case). The GDP map and the e2e fixture map were never affected.
8. **Contracts.** `scripts/export_contracts.py` gave no diff (current). `test_api_contract.py`: 3 passed.
   `npm run gen:types` regenerated `ui/src/api/openapi.gen.ts`, which was stale for every V8 route; tsc is clean.

Two pre-existing red tests the lead flagged are fixed:
- `test_fix5_projection_kinds`: `final_vintage` (NCEI nClimDiv, an observed value in the current revisable release)
  is now classified as `observation` in `registry/atlas/projection_kinds.yaml`.
- `test_forecast_registry_db::test_05`: the void-after-period step now monkeypatches `period_ended` and
  `extract_series` like `test_02`, so it no longer depends on the calendar date.

## Phase 2 — accessibility

- **Contrast**, computed from `tokens.css` (WCAG AA ≥ 4.5 for text):
  - `--v8-fg-subtle` was **3.06–3.43** in light and 4.12 in dark; it is now `#686e79` / `#848b97`, ≥ 4.58 on bg,
    surface and surface-2.
  - `--v8-scenario` on its background was 4.48 and is now 5.05.
  - Every other honesty colour on its background is 4.57–7.85 in both themes; the body text is 14.6–16.8.
- **Names:** the tour checks every visible interactive element on each page; none was unnamed.
- **Keyboard:** see the tour row above. Tabs use roles (`tablist`/`tab`) and `aria-selected`; the palette is an ARIA
  combobox.
- **Not done:** axe-core is not installed and I added no dependency, so the checklist is manual plus the scripted
  checks above. Screen-reader announcements were not tested.

## Remaining issues (none blocks the switch)

- **CLS on `/v8` with real data:** 0.026–0.084, below 0.1 but above the shell's mocked 0.007. Accueil tiles and feed
  grow as data arrives. Reserve their heights.
- **Shared code per route:** every route still costs about 66–85 KB Δ, mostly shared V3 `common` + zod (`schemas`
  chunk, 26.6 KB). This is within budget. Making a V8-only zod-free API layer would bring Δ to about 40 KB.
- **Cold start:** the first atlas open of a process still costs about 30–35 s, done in the background now. The
  Dossier first build is 1–4 s uncached (V8DOSSIER report).
- **English labels from the sources** (« malaria », « Dollars per Barrel », « Current US$ », WDI descriptions).
  These are data, not UI text. They need French labels in the catalog and KG.
- **Flaky unit test:** `fiche.test.tsx` « country/FRA: answer first… » failed once in a full parallel run (1.4 s,
  load-dependent) and passes alone and on rerun.
- **Monde ranking rows** have no per-row source chip. Source and licence are in the tooltip, the answer chip and the
  place panel.
- **Fiche facts:** the leading « · » before « aussi : » when the entity has no English label is cosmetic.

## How to rerun

1. Build the UI: `cd ui && npm run build`.
2. Start `uv run reva api --port 8190` with `REVA_ADMIN_PASSWORD_HASH` (an Argon2id hash of a per-run password, in the
   process environment only, as `scripts/e2e_stack.py` does), `REVA_SESSION_SECRET` and
   `REVA_SCHEMA_CONTRACT=warn`.
3. `POST /v1/session` once into a Playwright storageState file.
4. Run:

   ```
   REVA_E2E_BASE_URL=http://127.0.0.1:8190 REVA_E2E_STORAGE_STATE=<state> REVA_E2E_TOUR_LIVE=1 \
     [REVA_E2E_SHOTS=<dir>] PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers npx playwright test e2e/v8_tour.spec.ts --workers=1
   ```

On the fixture e2e stack (`scripts/e2e_stack.py --run -- e2e/v8_tour.spec.ts`) leave `REVA_E2E_TOUR_LIVE` unset:
only the frame, overflow, names and errors are checked, because the entity ids are not in the fixtures. No
credential was minted outside this documented pattern, and the per-run password and cookies were deleted after the
run.
