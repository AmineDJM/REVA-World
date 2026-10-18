# V8BOARDS — Accueil (« le pouls du monde ») and Tableaux (boards, watch rules, alerts)

Agent V8BOARDS, wave V8, 2026-10-17/18. UI only. No backend module, no migration, no write to the dev DB: the
board service (`/v1/v8/boards`) is V8API's. Two features:

| Feature | Folder | Route(s) | Order | `g` key |
|---|---|---|---|---|
| `accueil` | `ui/src/v8/features/accueil/` | `/v8` (path `''`) | 0 | `g h` |
| `tableaux` | `ui/src/v8/features/tableaux/` | `/v8/tableaux`, `/v8/tableaux/:id`, `/v8/tableaux/partage#b=…` (path `tableaux`, the shell mounts `tableaux/*`) | 70 | `g t` |

Checks: `npx tsc -p tsconfig.json --noEmit` clean for the whole UI at the end; `npx vitest run src/v8/features/accueil
src/v8/features/tableaux`: **35 tests pass** (13 accueil, 22 tableaux); `uv run pytest -q tests/invariants`: 191
passed, 1 skipped. About 4.5 k lines (code, CSS, tests).

## `pinItem` — the one allowed cross-feature import

`ui/src/v8/features/tableaux/pin.ts` is the public surface of Tableaux. Any feature may import it (and only it):

```ts
import { pinItem } from '../tableaux/pin';
await pinItem({ kind: 'series', concept: 'reva.economy.inflation_cpi', geo: 'FRA', label: 'Inflation · France', headline: true });
await pinItem({ kind: 'entity', entityKind: 'drug', id: 'chembl:CHEMBL941', label: 'imatinib' });
await pinItem({ kind: 'forecast', questionId: 'fq_455b1860c774831e20563f8c', label: 'Inflation France 2026' });
await pinItem({ kind: 'decision', ref: 'ecb_plus_50bp_2027q1', label: 'BCE +50 pb', summary: '…' });
await pinItem({ kind: 'note', markdown: '# Hypothèses\n- …' });
// → { boardId, boardTitle, added /* false = already pinned */, origin: 'server' | 'local', href }
```

* Pins are **references** (ids, concept + country, question id, decision ref, note), never values: every value on a
  board is read live with its source.
* Target board: `opts.boardId`, else the last board used, else the most recent, else a new « Mes épingles ».
* A malformed pin throws a `TypeError` naming the problem (zod schema shared with V8API's board document).
* Also exported: `validatePin`, `listBoardsForPin(signal)` (for a "pin to…" menu), `onPinned(fn)` and the
  `v8:boards-changed` window event (emitted after every change; Accueil listens to it).
* Persistence: `/v1/v8/boards` when deployed (optimistic concurrency with `version`); when the route is absent
  (unknown-route 404, 405/501, or `503 boards_store_missing`) or unreachable, boards live in this browser
  (`localStorage`, labelled « local — ce navigateur ») and can be uploaded with « Enregistrer sur le serveur » once
  the service answers.

## Accueil — what it shows (one screen)

Header « Le pouls du monde » with the date/time, quick actions (ask box focused with `q` → `/v8/demander?q=…`,
Rechercher → opens the shell palette, Comparer, Décision, Tableaux), then:

1. **Couverture en bref** — five tiles: atlas rows/tables/places, KG statements/entities (V8COUVERTURE
   `/v1/v8/coverage/matrix?level=region`, with a source chip: computed-at and "stale" flag), sealed forecast questions
   with pending/resolved (V8SCORES `/v1/v8/scores/live`), next resolution date, open owner decisions. A number that
   cannot be read is a hatched « indisponible » with the reason on hover — never 0.
2. **Ce qui a changé** — V8API `/v1/v8/changes`: window 24 h / 7 j / 30 j (`?p=`), kind chips with counts
   (`?k=`), rows grouped by day: time, kind, French title, counts (« 18 540 lignes · 1 table »), summary, source,
   links to the change (Monde table, Fiche source, question, scores). Resolved questions show their judged status
   **only as the feed states it**: « annulée : <raison> », « aucun prévisionniste officiel scellé : pas de
   comparaison », or « comparée à <officiel> — une question n’est pas un verdict ». Owner decisions are left out of
   the default feed (they have their own panel). 20 rows, then « Afficher 30 de plus », then the server cursor.
   `?change=<id>` (alert links) scrolls to and highlights that change.
3. **Décisions qui vous attendent** — `/v1/v8/coverage/decisions` (raw body): kind, title, what it unlocks; 4 shown
   + « N autres » → `/v8/couverture?vue=decisions`.
4. **Prochaines résolutions** — `/v1/v8/scores/live` upcoming groups (date, relative day, family, number of
   questions, the official forecaster sealed against them or « pas de prévisionniste officiel scellé »), overdue
   groups, and dated pre-registered tests from `/v1/v8/scores/calendar?live=false`. While the scores service computes
   its live state (cold) the panel says so and refreshes once after 8 s. Fallback when V8SCORES is absent: first
   page (200) of pending questions from `/v1/forecasts/questions`, labelled « aperçu sur les N premières questions
   (sur T) ».
5. **Tableaux épinglés** — boards (server + local) with item/rule counts.

Keyboard: `j`/`k` move through changes, `Enter` opens the first link, `q` ask, `Ctrl/⌘-K` palette, `g t` boards.
Each panel loads and fails on its own; the three panels reading the same source share one request per 15 s
(`shared()`), failures are not cached.

## Tableaux — what it does

* **Index** (`/v8/tableaux`): boards list (server + local, origin badge, counts, last change), create (`n`), `j`/`k`.
  Palette provider « Tableaux » searches board titles; command « Ouvrir mes tableaux ».
* **Board** (`/v8/tableaux/:id`): responsive grid (`auto-fill, minmax(min(100%, 300px), 1fr)`) of item cards, each
  read only when on screen (IntersectionObserver):
  * **Série** — headline series (`/v1/atlas/headline/series?projections=include`) or catalog series
    (`/v1/atlas/series`): latest **observed** value (never a projection) with exact value in the tooltip, source
    chip (series id, licence, period, snapshot), mini chart: observed solid, projections dashed in the projection
    colour and labelled as year ranges (« projection 1980–1989, 2026–2031 »), missing years break the line, source
    changes marked; series consulted without data counted; « manquant : <raison> » when there is no observation.
  * **Prévision** — `/v1/forecasts/questions/{id}`: question text, status badge, expected resolution, last published
    value, « scellée le … · sceau vérifié » (or « sceau NON vérifié — ne pas utiliser »), forecasters with readable
    names, the official one first and badged « officiel · attribué », point + [q5 ; q95]; when resolved, the scores
    as served and the reminder that one question is not a verdict (link to Scores).
  * **Décision** — « scénario non validé — pas une probabilité », reference-decision facts from
    `/v1/predict/decisions`, link to `/v8/decision?ref=` (the propagation is computed there, not on the board).
  * **Entité** — link to the Fiche; for KG ids, description, statement counts, source, « communautaire, non
    vérifié » for Wikidata.
  * **Note** — safe Markdown subset rendered to React elements (no `innerHTML`; links only `http(s)` and `/v8`),
    edited in place (`Ctrl+Entrée` save, `Échap` cancel); an unlabelled note's first heading is its card title.
* **Reorder**: drag-and-drop on the ⠿ handle, or keyboard — `Espace`/`Entrée` grabs, arrows move, `Espace` drops,
  `Échap` cancels (restores the position); `Alt`+arrows / `Alt`+`Home`/`End` move directly. Every move is announced
  in an `aria-live` region (« … déplacé en position 3 sur 6 »).
* **Remove** with undo (banner button or `Ctrl+Z`), **rename** (✎), **delete** (confirm; references only).
* **Veille** (`r`): rules per item, only those that make sense — new data / revision / threshold on series,
  resolution on forecasts, new data on entities; threshold = latest **observed** value with `> ≥ < ≤` and a decimal
  typed in French (« 2,5 » → `"2.5"`, stored as a string).
* **Alertes**: server boards use `/v1/v8/boards/{id}/alerts` (fired, not-fired-with-observed-value, not evaluable
  with reason; each linked to the change). Local boards are evaluated in the browser against the change feed (last
  30 days) for what the feed alone decides (resolution; new data when the change names the item's table, concept or
  entity); revision and threshold are reported « évalué seulement par le serveur » — nothing is guessed. If the
  feed is unreadable every rule is « non évaluée : <raison> ». A summary line counts rules evaluated without firing.
* **Partager** (`s`): URL `/v8/tableaux/partage#b=<base64url(JSON)>` carrying the board itself (references, notes,
  rules; no values, no session data; in the fragment, so never sent to a server). The shared view is read-only,
  re-reads values live, says so, and offers « Garder une copie » (server if available, else local). Any decode or
  schema failure refuses the whole link (no partial render).
* Server conflicts (`409 version_conflict`, current board in `details.current`): banner « Modification non
  enregistrée » + « Recharger la version du serveur ». Saves are serialised; the server version flows from one save to
  the next.
* Keys on a board: `j`/`k` card focus, `a` add (search via `/v1/v8/search`, or paste any V8 link / `fq_…` id;
  indicators ask for an ISO3 country), `n` note, `r` rules, `s` share, `Ctrl+Z` undo. The board title is set as
  `document.title`, so the shell's last breadcrumb reads « Inflation & énergie », not the id.

## Screenshots (reviewed; `scratchpad/v8/v8boards/shots/`, live API on the dev DB unless stated)

* `accueil-live-desktop` / `accueil-live-board` (1440×900, light): glance strip 109 M atlas rows / 348 tables / 251
  places, 5 M KG statements / 589 k entities, 5 952 sealed questions (5 947 pending, 0 resolved), next resolution
  18 oct. (aujourd’hui), 68 open owner decisions; feed with 124 changes in 24 h (USDA WASDE 18 540 rows, NOAA NCEI
  2 646 rows, sealed batch 2026-Q4 with 5 models, ctgov KG batch 500 entities / 2 000 statements…); decisions panel
  (licences first); calendar starting with NHSN COVID-19 hospitalisations « contre cdc_covidhub ».
* `accueil-dark` (7 days): same layout, dark tokens; 450 changes.
* `accueil-phone` (375 px, full page): no horizontal scroll (scrollWidth 375), tiles 2 columns, feed rows wrap
  (time + kind, title below), 20 rows then « Afficher plus ».
* `tableaux-board-desktop` / `-dark` / `-phone`: board « Inflation & énergie » (browser-local for the screenshot, so
  nothing is written to the dev DB): inflation France 0,93 % (2025, observé, imf.weo.PCPIPCH, projections 2026–2031
  dashed, source change 1980), forecast question France 2026 (sealed 17 oct., FMI — WEO 2,41 officiel · attribué,
  persistence 0,93 [−2,02 ; 3,87], AR(p) 1,7, quantiles 0,71, REVA ensemble v2.0.0 1,44), GDP per capita PPP 63 975
  (2025, WDI; projections 1980–1989, 2026–2031), ECB +50 bp decision card, France entity, Markdown note; alert panel
  « Seuil : évalué seulement par le serveur » + « 2 règles évaluées sans déclenchement ». Phone 375 px: no
  horizontal scroll.
* `tableaux-rules`: rules panel open (3 rules, form with item/kind, help text).
* `tableaux-index` (local, with the absent-service note) and `tableaux-index-server` (real `/v1/v8/boards`, empty).

Review fixes made from the screenshots: phone overflow from long change links and long series ids (now wrap /
ellipsis); feed capped at 20 rows; decisions panel compacted (clamped lines); the coverage source chip moved inside
the atlas tile (it overlapped a tile label); projections shown as year ranges (the WEO PPP early years are flagged
projection by the atlas — a single "1980–2031" range was misleading); headline numbers rounded for display by
magnitude with the exact decimal in the tooltip (« 63 975 » instead of « 63 975,354 »); duplicate note heading
removed; « Lecture toujours en cours » after 4 s on slow item reads.

## Performance (dev box, load average ≈ 10–15, warm, 5 runs each)

| Read | warm latency |
|---|---|
| `/v1/v8/changes` (24 h, default kinds, 100 items) | 6–15 ms (574 ms first) |
| `/v1/v8/scores/live` | 4–7 ms (cold: ~25 s compute, served as `refreshing`) |
| `/v1/v8/scores/calendar?live=false` | 8–74 ms |
| `/v1/v8/coverage/decisions` | 5–46 ms |
| `/v1/v8/coverage/matrix?level=region` | 17–28 ms |
| `/v1/v8/boards` | 8–13 ms |
| `/v1/atlas/headline/series` (board series card) | 84–141 ms |
| `/v1/predict/decisions` | 7–18 ms |
| **`/v1/forecasts/questions/{id}` (board forecast card)** | **11.4–14.6 s** |

Accueil = 6 requests, all < 75 ms warm. Feature code is split: `index.ts` of both features is tiny (the board client
is imported lazily even by the palette provider); pages are `lazy()` chunks. Lighthouse was not run (no build allowed
for this agent; V8QA/lead build).

## Honesty choices

* No demo numbers anywhere: every empty state names what is missing and why (route absent, not computed, nothing
  recorded in the window, no sealed official forecaster).
* Missing values hatched « manquant : <raison> » / « indisponible » with the reason; never 0.
* Projections never answer "latest value"; drawn dashed in the projection colour, labelled with their years.
* Forecast verdicts only as the server states them; one resolved question is never presented as a verdict.
* Decisions always « scénario non validé — pas une probabilité ».
* Source chips and licences on series values; Wikidata « communautaire, non vérifié ».
* Local boards and shared links are labelled as such; a shared link renders read-only and says values are re-read now.
* Alerts on local boards evaluate only what the browser can decide; the rest is « évalué seulement par le serveur ».

## Test fixtures

Recorded from the dev DB (trimmed): `accueil/__fixtures__/changes_recorded.json` (from
`reva.v8.changes.feed.read_changes`, read-only transaction, before the route was deployed — same shape as the route
now serves), `scores_live.json`, `scores_calendar.json`, `coverage_decisions.json` (V8COUVERTURE service);
`tableaux/__fixtures__/forecast_question_fra_cpi_2026.json`, `headline_series_fra_inflation.json`. The coverage
matrix test body uses the `CoverageMeta` shape with the HANDOFF atlas numbers (marked in the test).

## What is missing / requests to the lead

1. **`/v1/forecasts/questions/{id}` takes 11–15 s warm** (also `/v1/v8/entity/question/…` ≈ 9 s): `R.question` in
   `src/reva/evaluation/registry/read.py` rescans every batch manifest and score row per call (26 batches now). Board
   forecast cards and the Fiche of a question wait that long. Needs an index by question id (owner: registry / V8API).
2. Server-side board persistence and server alerts were exercised only through mocks of the documented contract
   (and V8API's code shapes: `details.current` on 409, `not_fired` alert status, `503 boards_store_missing`): my
   brief forbids writes to the dev DB, so no board was created on the real server. The real `GET /v1/v8/boards` (read)
   answers and the UI switches to server mode.
3. The dev API refused to start after V8API applied `0020_v8_boards` before committing the schema contract
   (`SchemaDriftError`); I ran my own instance with `REVA_SCHEMA_CONTRACT=warn`. The committed contract now has
   head `0020_v8_boards`, so this is resolved.
4. Other features should now add « Épingler » buttons calling `pinItem` (Fiche, Monde, Scores, Décision). No shell
   change needed. No contract change requested; no dependency requested.
5. Accueil "today's events" relies on the change feed kinds V8API serves (ingest, KG batches, causal loads, effects,
   sealed batches, resolutions, evaluations, owner decisions); there is no separate "new events" kind yet (event
   datasets arrive as `ingest`/`kg_batch`).
