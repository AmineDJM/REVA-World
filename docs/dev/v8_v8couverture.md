# V8 COUVERTURE: REVA's coverage map (what is loaded and what is missing)

Agent V8COUVERTURE, 2026-10-17/18. Feature `couverture` (route `/v8/couverture`, order 80, `g v`). Owned files:

* UI: `ui/src/v8/features/couverture/`, containing `index.ts`, `api.ts`, `model.ts`, `CouverturePage.tsx`, `Matrix.tsx`, `Detail.tsx`, `Pending.tsx`, `Freshness.tsx`, `useLoad.ts`, `couverture.css` and `couverture.test.tsx`.
* API: `src/reva/api/routes/v8_coverage.py`.
* Logic: `src/reva/v8/coverage/` (`domains.py`, `compute.py`, `pending.py`, `service.py`).
* CLI: `src/reva/cli/v8_coverage.py`, plus a small hook in `src/reva/cli/main.py`.
* Registry: `registry/coverage/domains.yaml` and `registry/coverage/pending.yaml`.
* Tests: `tests/unit/v8/test_v8_coverage_{registry,compute,api}.py` and `tests/integration/v8/test_v8_coverage_devdb.py`.

Most of this work is committed. Some of it went in through the lead's WIP snapshots (16d0bc5, 23aa9fb …), and part of it went in through BEAT-ENERGY's commit d1e1fda, because the agents share one git index.

## 1. The coverage model

**Domains** (`registry/coverage/domains.yaml`): 24 domains plus "Non classé".

* The domains are: population, macroeconomy and labour, prices, finance, trade, industry, energy, chemistry, agriculture, climate, environment, hazards, biology, health, drugs, society/mind, education/science, politics/law, conflict, migration, infrastructure, digital, culture, history and geography.
* Every atlas theme (`themes.yaml`, all 26) maps to exactly one domain. A test enforces this.
* An atlas table gets its domain by the first rule that matches:
  1. an explicit table;
  2. its declaring connector (this handles shared source keys such as eurostat, imf, oecd and wb);
  3. its atlas source;
  4. the majority theme of the catalog concepts that cite it;
  5. otherwise "Non classé".
* Live result: 278 tables by source, 45 by catalog theme, 15 by table, 10 by connector, 0 unclassified.
* KG entity kinds go to a domain through a (kind, source) rule first, then the kind alone. For example, `event` from `harvard.dataverse.*` goes to politics, and from `reinhart_rogoff.*` to finance. Only 38 k of 589 k KG entities stay unclassified: `wikidata_item`, generic `event`, `unclassified`.

**Places** are the 251 entities of the crosswalk, grouped by UN M49 region and subregion. Four entities have no M49 region: ATA, CHI, TWN and XKX. They get an explicit REVA placement with its basis, which the UI flags with `*`.

**Per cell** (domain × country):

* rows, tables, first year, latest *observed* year and future-dated rows. A row is "future" when its period starts after today (projections, scenarios, announcements). Future rows never count as the latest observation.
* KG entities attached to the country. These are subjects of a `located_in`-family property whose object is the country's Wikidata QID or one of its ISO 3166-2 codes (matched by identifier prefix, never by name).

**Per table:**

* row count and countries; rows with no country (aggregates, ADM1 and grid tables: 41 tables have no country at all);
* periods, versions, and when the table was recorded;
* last fetch (from live acquisitions per connector) against the connector's declared cadence, which gives the "à rafraîchir" flag;
* licence class (open / non-commercial / restricted / undetermined), from `normalise_rights`, with any required notice.

**Gaps:**

* 41 of 222 declared atlas connectors have never been acquired.
* Some catalog concepts have no loaded table: environment 136/385, finance 102/893, politics 38/638 …
* For each domain, the countries with nothing attached.

**Computation:** `uv run reva v8 coverage refresh`, with `--no-kg` to skip the KG pass.

* It reads the dev DB in a READ ONLY transaction, plus each table's Parquet artifact (only the `iso3` and `period_start` columns). It also reads the catalog, the source specs and the KG.
* It writes `var/v8/coverage/coverage.json` (2.0 MB, atomic replace). Nothing is written to the DB.
* It took 80 s on a quiet DB, and 197 s to 717 s while other agents were loading. The cost is the two `kg.statement_current` scans (≈ 40 s and 60 s).
* `uv run reva v8 coverage check-pending` validates the registry.
* The live snapshot holds 348 tables, 108,813,505 rows, 589 k KG entities and 5.0 M statements. Licence classes across tables: 249 open, 45 undetermined, 40 restricted, 14 non-commercial.

## 2. Queue and blockers (`registry/coverage/pending.yaml`, 93 items)

**Sources.** Transcribed from every V4/V5/V6/V7 report section titled "owner decisions", "queued", "missing" or "commands", from `HANDOFF_V3.md`, and from the lead's todo, including the new BEAT-MACRO, BEAT-CONFLICT, BEAT-ENERGY, FIX6, FIX7 and V6DRUGS items.

* 25 loads: 19 that wait for ≥ 5 GB of free disk and 4 that need a bigger machine (full GDELT, long trade history, ecoregion overlay, Wikidata harvest).
* 68 decisions: licences, accounts and keys, permissions, attribution contacts and policy choices.

**Fields of each item:**

* `kind`, `title`;
* `unlocks`: domains, atlas sources, tables, tracks such as `beat-health:H2`, and French text saying what becomes possible;
* `action` and the exact `commands`;
* `who` (owner or lead), `status` as written in the report, `disk_mb` and `source_docs`.

**Validator** (pydantic, `tests/unit/v8/test_v8_coverage_registry.py`):

* Kinds come from a closed list.
* No e-mail address may appear anywhere: the registry is refused if one does.
* A decision carries no automatic check, so only the owner can close it.
* A load needs its commands. `disk` and `bigger_machine` items are always loads.
* Ids are unique; domains exist; source docs exist (`lead:` marks the lead's todo, which lives outside the repo); every checked `atlas.*` connector is declared.

**Live check of loads.** A load's `check` names connector runs, with an optional minimum version and expected atlas tables. The API compares them with the live acquisitions and registered tables, so the UI can say "exécution constatée le …" without anyone editing the file. That wording is deliberate: it confirms the run happened, not that the data is correct.

* At the last check, 4 of the 25 loads are observed done: V6CULTURE, the FIX4A reruns, FIX6 UCDP GED 1.0.1 (run by the lead on 2026-10-17 at 22:35) and FIX4C.
* OPSLOAD has no automatic check on purpose: a partial demography run is also an acquisition.

## 3. API `/v1/v8/coverage/*` (read scope, pydantic models, raw bodies)

| Endpoint | Content | Warm p50 / p95 |
|---|---|---|
| `GET /matrix?level=region\|subregion\|country` | page bundle: meta (totals, staleness = tables registered since computation, note), columns, rows with summary + cells; absent cell = absent | 22/43, 27/49, 33/49 ms |
| `GET /domains/{id}` | tables (licence, periods, overdue), countries with/without data (+ region/subregion), KG kinds, rows-per-year histogram, catalog gaps, connectors never loaded, pending items with live check | 11/21 ms |
| `GET /countries/{ISO3}` | per-domain cells, tables (licence, required notices), KG kinds attached | 11/31 ms |
| `GET /freshness` | per domain: tables' latest observed year, last fetch vs cadence, histogram; connectors never loaded | 7/13 ms |
| `GET /pending`, `GET /decisions` | items grouped by kind with live observations (cached 60 s), open disk need | 5/13, 7/12 ms |

Latency was measured with 20 rounds against the dev DB while other agents were writing to it. All endpoints stay far under the 300 ms target.

If `coverage.json` is missing, the API answers 404 with "run `uv run reva v8 coverage refresh`". If `pending.yaml` is invalid, it answers 404 with the check command.

The V8BOARDS Accueil already consumes `/decisions` and `/matrix?level=region`, and the shapes are compatible.

## 4. UI (`/v8/couverture`)

**URL state:** `?vue=matrice|decisions|chargements|fraicheur`, `?niveau`, `?metrique=pays|lignes|annee`, `?domaine`, `?zone`, `?pays`, and `?q`, `?filtre`, `?masquer` on the lists.

**Answer strip:** tables (348), rows (109 M), KG entities (589 k), domains with tables (23 of 25, naming the two without: Biologie and Numérique), open decisions (68) and queued loads (22, "chacun exige 5 Go libres"). The two counters link to their tabs.

**Heat matrix** (domains × continents, sub-regions or countries):

* Three measures: share of countries with rows, rows on a log scale, and freshness of the latest observed year.
* A cell with nothing stored is **hatched** (the shell's missing hatch). Its tooltip reads "rien de stocké n’est rattaché à ce lieu — ce n’est pas l’absence du phénomène". A cell with KG entities only is dotted. No cell ever shows 0.
* Keyboard: the arrow keys, Home and End move between cells; Enter opens; Esc closes the detail.
* Clicking a row opens the domain. Clicking a cell opens the domain filtered on that zone. In country view, clicking a column header opens the country profile.
* The country view has 251 narrow columns that scroll inside the panel and are grouped by continent.

**Domain detail** (side panel; on narrow screens it comes first):

* zone-aware stats and the licence bar;
* "Pays sans aucune donnée" first, as hatched chips;
* compact table list, each with a SourceChip (licence, NC and restriction flags, attribution, coverage note on hover), a "+futur" projection badge and an "à rafraîchir" badge;
* rows-per-year histogram, with years after today in the projection colour;
* KG kinds, with how entities are attached to countries;
* catalog concepts without a table, connectors never loaded, and the pending items that would unblock the domain.

**Country profile:**

* one bar per domain on a log scale, hatched with "rien de rattaché à ce pays" when empty;
* years formatted with "av. J.-C." (HYDE reaches 10 000 av. J.-C.);
* tables with licence chips, required provider notices shown verbatim, and KG kinds;
* a link to the Fiche.

**Décisions du propriétaire:** grouped by kind, with a filter. Each card shows what it unlocks (domain chips open the domain), the action, who must act, its status ("à décider", so it is not confused with an "open" licence) and its sources.

**Chargements en attente:**

* a one-sentence answer: N recorded, N already run according to the DB, Mo needed, the 5 GB rule, and the 9 GB lead pass named on its own;
* the live-check badge; exact commands with "Copier tout"; runs already observed are sorted last and can be hidden.

**Fraîcheur:**

* per domain, a strip with one dot per table at its latest observed year over a light area of rows per year, and a "today" line;
* the overdue list;
* the 41 declared connectors never loaded.

**Footer:** when the aggregates were computed and whether they are stale ("N tables chargées depuis — à recalculer"), plus the copyable refresh command.

**Palette:** six commands; "couverture FRA" opens `?pays=FRA`. The search provider covers domains, decisions and loads, and loads its API lazily.

**Screenshots reviewed** (`scratchpad/v8/couverture/`; light, dark and 375 px phone):

* The first version saturated in blue, cut long labels and made the page 593 px wide on a phone. The causes were the segmented controls in the panel header and the matrix width.
* I then moved the controls into the body, gave the label column 204 px, narrowed the country cells to 15 px and constrained the panel width. Phone `scrollWidth` is now 375/375 on the matrix, decisions and country pages.
* In the side panel, table keys were breaking mid-word; they are now a two-line list.
* "Pays sans donnée" moved above the tables.
* The domain chips on the cards showed raw ids; they now show French labels.
* Text on dark heat cells is now readable in dark mode (`--v8-surface` ink).
* The "ouverte" status could be confused with an open licence; it now reads "à décider".
* On a phone the opened detail now comes before the matrix.

What the final screenshots show:

* Conflict detail filtered on Africa: 55 of 60 countries covered, 28 tables. The five without data are La Réunion, Mayotte, Saint Helena, the French Southern Territories and the British Indian Ocean Territory.
* Health: 232 of 251 countries.
* France: 24 of 26 domains, 266 tables.

## 5. Honesty choices

* Coverage counts what REVA has stored. The note "Couverture ≠ vérité : l’absence d’une source n’est pas l’absence du phénomène" is on the page, in every API response and in every absent-cell tooltip.
* An absent cell is absent in the API and hatched in the UI, never shown as 0.
* Rows dated after today are counted apart ("+futur") and never shown as the latest observation.
* Biology (169 k KG entities: genes, taxa, proteins) is hatched on the map because nothing in it is attached to a country. The panel labels it "non localisé" rather than "manquant".
* KG-to-country counts are lower bounds: only direct identifier links count, and the rule is stated in the UI.
* "Exécution constatée" confirms a connector run at the right version plus the expected tables. It does not say the data is right.
* Report statuses are kept as written; the live check is shown next to them.
* All licence classes are visible, including NC; provider-required notices (NY Fed) are quoted verbatim in the country profile.
* No e-mail address or personal data appears anywhere. The SEC item asks for a project role mailbox, never the owner's personal address.

## 6. Tests and checks

* Python: 18 unit tests (registry validity and consistency, assignment rules, places, item rules, aggregation helpers, observation logic, all routes over a synthetic document, typed 404). 2 integration tests on the dev DB, read-only: two real tables recomputed and checked against their manifests (rows = mapped + no-country; countries = manifest count), and live observation of every load.
* ruff and mypy are clean on my files; `tests/invariants` gives 191 passed, 1 skipped.
* UI: 11 vitest tests (model wording and shading, matrix hatching, URL state, arrow-key navigation, Escape, grouped decisions and filter, loads with live check and copy, done items last, the never-computed hint, the feature contract and palette). `tsc` is clean across the whole UI (0 errors at the time of the check).
* V8SHELL fixes:
  * `index.ts` no longer imports `./api` or `./model` statically; the palette provider `await import('./api')`. I did not run `node ui/scripts/v8-bundle-report.mjs`, because it needs `npm run build`, which my brief forbids. The shell budget should be re-checked after the lead's build.
  * The 375 px overflow is fixed (scrollWidth 375 measured).

## 7. Missing, limits, requests to the lead

* The aggregates are a snapshot. The page shows how stale they are, but it does not schedule the refresh itself. **Request:** add `uv run reva v8 coverage refresh` as a worker job after each atlas snapshot. It is read-only and takes about 1–2 min on a quiet DB.
* `cli/main.py`: I added a generic `reva v8 <name>` group that auto-discovers `reva/cli/v8_*.py` modules exposing `cli`. This mirrors `routes/v8_*`. Other V8 agents can use it.
* The time dimension is year-level: first year, latest observed year, and a rows-per-year histogram. There is no per-indicator vintage view.
* Place is country or M49 region. ADM1 coverage (from the `iso3166_2` and `nuts` tables) is not broken out.
* KG statements per domain are counted by source, not by domain.
* The dev API refused to start under `REVA_ENV=local` on 2026-10-18 with a schema-contract drift: "migrations changed after this database was migrated". Screenshots were taken with `REVA_ENV=test`, which only logs the drift. **The lead should check the migrations.**
* The shell's `/v1/v8/search/status` returned 404 in my dev runs. That belongs to V8SHELL/V8API, not this feature.
* No contract change and no new dependency is needed.
