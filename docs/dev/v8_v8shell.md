# V8SHELL — the console frame at `/v8`

Agent V8SHELL, 2026-10-17. Owner of `ui/src/v8/shell/**`, `ui/src/App.tsx`, vite/ts configs, package files (no
dependency added), `ui/e2e/v8_shell.spec.ts`, `ui/scripts/v8-bundle-report.mjs`.

## What exists now

### Route and mounting
- `App.tsx`: `/v8/*` renders the V8 console in its **own frame** (no V1 header) behind the same session check;
  `V8Root` is a lazy chunk, fetched **in parallel with the session check** when the landing URL is under `/v8`.
- `/` and every old screen (`/world`, `/v3/*`, …) are unchanged. The old header now shows a gradient
  **« Nouvelle console V8 → »** link (and the old ⌘K palette lists it).
- Served by `uv run reva api` (checked on port 8101: `GET /v8` → 200 `index.html`, `GET /v8/fiche/country/FRA` → 200,
  assets immutable-cached; `/health/ready` → ready).

### Feature discovery (`shell/discovery.ts`, `shell/registry.ts`)
`import.meta.glob('../features/*/index.{ts,tsx}', { eager: true })` → validated `V8Feature[]` sorted by `order`
(then French title). Paths are normalised (`/v8/monde` → `monde`), `''` is the `/v8` index (Accueil), routes are
mounted as `<path>/*`; a parameterised path with `navPath` also answers its nav target. Invalid modules, duplicate
ids/routes, id ≠ folder and `goKey` conflicts are **reported, not fatal** (listed in the `?` sheet). Nothing is
hard-coded: the nav lists what exists; unknown `/v8/...` URLs get a placeholder that says the section is absent and
lists the available ones; with no index feature, a built-in home (search door + feature cards) is shown.
Features found at the end of the run: accueil, demander, monde, comparer, liens, scores, tableaux, couverture,
dossier, fiche.

### Layout (`V8App.tsx`, `Nav.tsx`, `Breadcrumbs.tsx`, `Health.tsx`, `shell.css`)
- Left **rail** (208 px, collapsible to 52 px icons with `[` or the button; persisted), icons + French labels,
  `g x` hints on hover, « Anciens écrans » and the user/déconnexion at the bottom.
- **Top bar** (sticky, translucent): breadcrumbs from the URL (Console › feature › segments; a feature can name the
  last crumb by setting `document.title = 'France · Fiche'`), the palette trigger (« Rechercher… Ctrl K »), the
  **« état des données »** indicator (`/health/ready` every 60 s + on tab focus: green « Données prêtes » / amber /
  red « API injoignable », popover with each check in French and the V8 search-index state from
  `/v1/v8/search/status`), theme toggle, `?`.
- **Phone (≤ 760 px)**: the rail becomes a **bottom sheet** (menu button, 2-column grid of 44 px targets), search and
  data status collapse to icons, breadcrumbs keep the last two crumbs, main has **16 px gutters**; no horizontal
  scroll (checked by e2e at 375 px).
- Each feature runs inside an error boundary (the frame survives a crashing feature; chunk-load failures are named)
  and a Suspense skeleton with a fixed footprint. Scroll resets on path change; html gets `v8-active` while mounted
  so the page background / `color-scheme` follow the theme without touching the old screens.

### Command palette (`shell/palette/*`)
- **Ctrl/⌘-K** anywhere (also in inputs), **`/`** outside inputs, and the top-bar button.
- Items: « Aller à » (every linkable feature, with its `g x`), feature **commands** (a command matches by
  label/keywords, or when the query starts with one of its keywords — « comparer FRA DEU inflation » runs
  `run(query)`; a command returning `null` is hidden), built-ins (thème clair/sombre/système, replier la navigation,
  raccourcis, effacer les récents, anciens écrans), **async providers** (default + every `feature.search`).
- Providers: **debounce 120 ms**, the previous run **aborted** (AbortController) as soon as the query changes,
  results merged progressively in provider order, grouped by `hit.group` (8 per group, deduplicated by URL), a failing
  provider never hides the others. Fuzzy ranking is accent-insensitive with word-start priority (mid-word matches
  like « bre » in « sombre » do not surface commands).
- Keyboard: ↑/↓ (wrap), Enter, **Ctrl/⌘-Enter (new tab)**, Escape, Tab trapped; ARIA combobox + listbox with
  `aria-activedescendant`; the active item's feature chunk is prefetched.
- **Recents** (8, `localStorage` `v8.palette.recent`, every access in try/catch, malformed values ignored).
- **Default provider**: `GET /v1/v8/search?q=&limit=24&lang=fr` (V8API contract in `docs/dev/v8_api_interface.md`;
  hits validated by hand, malformed ones dropped). On **404** (route absent) it switches silently to the fallback
  for the page view; on 503 `search_index_missing`/other errors it retries the V8 index after 60 s. **Fallback** =
  `/v1/kg/search` (entities, French kind label, « donnée de test » for fixture origins) + the memoised atlas catalogue
  (`/v1/atlas/concepts`, word-start/substring matches only) → « Pays & régions », « Entités », « Indicateurs »; the
  palette footer says « recherche de secours (graphe + catalogue atlas) » / « index V8 ». The fallback code (zod +
  V3 clients) is a separate chunk loaded only when needed.
- **`g <key>`** go-shortcuts from `feature.goKey` (1.5 s window), **`?`** shortcuts sheet (global keys, palette
  keys, every `g x`, discovery problems).

### Design system (`shell/tokens.css`, `shell/ui/*`, `shell/README.md`)
Tokens `--v8-*`: surfaces/ink, accent, **honesty semantics** (observed, projection, estimate, scenario, judged,
missing + `--v8-missing-hatch` pattern, community, fixture, licence), win/loss/neutral, categorical chart series,
spacing (2…48 px), type scale with tabular-figure numerics (`.v8-num`), radii, shadows, motion (0 with reduced
motion), layout sizes, z-indexes. Light default, dark via `prefers-color-scheme`, forced with `<html data-theme>`;
theme changes dispatch `v8:theme`.
Primitives (all documented with examples in `ui/src/v8/shell/README.md`): `Panel`, `Stat` (exact decimal strings
formatted without float conversion, unit, period, honesty badge, `SourceChip`, missing → hatched reason), `Badge`
(observé / projection / estimation / scénario non validé / prévision jugée / manquant + community, fixture, licence,
win, loss, neutral), `MissingValue`, `SourceChip` (source + licence terms + attribution + period/vintage/retrieval +
provenance lines; `NC` flag; tap pins it; `atlasCellSource(cell)` and V3 `Citation` adapters), `KeyHint`,
`Tabs`/`UrlTabs`/`TabPanel` + `useUrlParam` (one URL per state), `EmptyState`, `ErrorState` (V3 error
classification: « Pas encore disponible », « Aucune donnée chargée », « Introuvable »), `Skeleton`, `VirtualTable`
(@tanstack/react-table + react-virtual, sticky header, numeric meta, row links with Ctrl-click), `Sparkline` (V3) and
`MiniLine` (gaps break the line, projections dashed), `Split` (drag + keyboard, stacks on phone), French formatters.
Adoption at the end of the run: 8 features import them (Badge/ErrorState/Skeleton ×11, SourceChip ×7, Panel ×6…).
Committed in the first ~25 minutes (commit `ce87d1c`), refined after.

## Honesty choices
- The shell never renders a value of its own; placeholders say what is absent and why (« Rien n’est affiché à la
  place »).
- `MissingValue`/`Stat` never show 0, « — » or blank for a missing value; projections/scenarios are tinted, dashed and
  badged; `SourceChip` names restrictive licences next to the value (not only in a footnote) and quotes required
  notices verbatim.
- The palette's fallback labels fixture records « donnée de test »; it does not merge records by name.
- The data-status indicator reports what `/health/ready` says, including failing checks; « API injoignable » when it
  does not answer.

## Measurements (built UI served by `uv run reva api --port 8101`, Chromium, machine load average 17–20)
- **LCP `/v8`**: median 1.0 s over 5 cold loads (FCP ≈ LCP; `/world` old screen under the same load: 0.93 s);
  `/v8/scores` 0.84 s, `/v8/monde` 0.79 s; e2e single runs 1.1–1.7 s. **CLS 0.004–0.007.**
- **Route switch** (nav click → feature rendered, prefetch on hover, Playwright wall time incl. ~50 ms driver
  overhead): 74–290 ms for most features; `/v8/comparer` 563 ms and `/v8/liens` 725 ms in the last loaded run (their
  pages; liens loads cytoscape).
- **Bundles** (`node scripts/v8-bundle-report.mjs`, gzip): old screens entry 96 KB JS; **/v8 frame 171 KB JS +
  13 KB CSS** cold (React/router/old entry 90 KB + shell 20 KB + **zod 26 KB pulled in by feature index modules**, see
  requests); per-route Δ: accueil 16, comparer 19, couverture 14, demander 17, dossier 10, fiche 15, scores 17,
  tableaux 28, liens 149 (cytoscape), monde 303 (maplibre). Budgets: frame ≤ 150, Δ ≤ 120 (≤ 420 with map/graph):
  only the frame is over, by the zod import below. Without it the frame is ≈ 145 KB.

## Tests
- Vitest (`npx vitest run src/v8/shell`, 39 tests): discovery (order, normalisation, nav targets, conflicts, invalid
  modules), keys (Ctrl/⌘-K in inputs, `/`, `?`, `g x` timing, `[`), theme (system/forced/persisted/throwing
  storage/hook), palette (fuzzy, command keyword run, grouping/dedup/caps, keyboard ↑↓ wrap/Enter/Ctrl-Enter,
  recents, **debounce 120 ms + abort + no stale results**, failing provider), default provider (V8 search, 404 →
  fallback sticky, abort), shell (nav order, routing, lazy feature, crash boundary, unknown URL, built-in home,
  palette/help/theme, health, breadcrumb page label), primitives.
- Playwright `ui/e2e/v8_shell.spec.ts` (6 tests, all green standalone): frame, palette (fallback results grouped,
  keyboard, Enter, recents via `/`), `g x` + `?` + theme (dark background asserted), phone (sheet, 16 px gutters, no
  horizontal scroll), old-nav link, performance (LCP < 2.5 s, CLS < 0.1, route switches printed). Run:
  `REVA_E2E_V8_URL=http://127.0.0.1:8101 PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers npx playwright test e2e/v8_shell.spec.ts`
  (`REVA_E2E_SHOTS=<dir>` writes screenshots). Against the e2e stack it uses the real session.

## Screenshots (scratch folder `…/scratchpad/v8/v8shell/`)
- Light, `/v8`: rail (Accueil active, 9 sections with icons), breadcrumb « Console », search field with Ctrl K,
  green « Données prêtes », Accueil page with hatched « indisponible » tiles and dashed empty states.
- Light palette « bré »: groups « Pays & régions » (Brésil, highlighted match, ↵) and « Entités » (fixture event),
  footer with key hints and « recherche de secours ».
- Dark `/v8` and dark palette: same frame on #0e1013, soft blue accent, hatched missing tiles readable.
- Phone 375 px: hamburger + crumb + icons; bottom sheet with a 2-column grid; full-width palette with ✕ instead of
  key hints. `/v8/scores` and `/v8/couverture` light/dark/phone in the same folder.
Screenshots use intercepted `/v1/session` and 404 for data routes (no admin session was available to this agent;
minting one was refused by the sandbox) — they show the frame and the features' honest « pas encore disponible »
states, not real data.

## Requests to the lead
1. **V8COUVERTURE and V8DOSSIER**: `features/couverture/index.ts` and `features/dossier/index.ts` statically import
   their `./api` (zod + V3 common) — that puts 26 KB gz into *every* `/v8` first load. Please `await import('./api')`
   inside `search`/`run` (rule now in `shell/README.md`, « keep index.ts tiny »). `scores`/`demander` import small
   pure modules (fine).
2. **V8COUVERTURE**: phone `/v8/couverture` overflows horizontally by 218 px (matrix toolbar); wrap or scroll inside
   a box.
3. In-flight feature type errors make `npm run build` stop at its `tsc` step (at my last run:
   `features/demander/AnswerCard.tsx:97` TS2345; earlier `features/accueil/AccueilPage.tsx`). I verified with
   `npx vite build` (same output); the lead's final build should run once the features are type-clean.
4. Real-data screenshots / e2e need a session: run `scripts/e2e_stack.py --run -- e2e/v8_shell.spec.ts` or give V8QA
   a dev login.
5. No contract change needed. No dependency added.
