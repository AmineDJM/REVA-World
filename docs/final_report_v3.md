# REVA World — V3 completion report (implementation prompt §14 format)

Date: 2026-10-16 (UTC). Branch: `claude/kind-darwin-kyy8re`. Written by agent V3FINAL. V1 and V2 are covered by
`docs/final_report.md`. This report covers V3: the planet-wide atlas, the knowledge graph, the country–country
links, events and causality, the simulations for all countries, the "Planète" UI and natural-language questions.
The two V3 roadmaps are `docs/roadmap/v3_world_graph.md` and `docs/roadmap/v3_causality.md`.

**Where the numbers come from.** Every number below comes from one of two places: the run made for this report
(§3; the commands, the code they ran on and the results are listed there), or an agent's report under `docs/dev/`
(the file is cited each time). Most live loads happened in **throwaway databases** that were dropped afterwards. The
report says when a number comes from such a load and not from the dev database `reva`. This report never reads or
writes the dev database. Two other agents (LOADALL, WDRUN) were loading the dev database and the Wikidata harvest
database while this report was written. Their results are **not** included, and the dev-database figures below are
the latest *reported* ones (ATLASOPS, 2026-10-16), not a fresh count. **Not deployed:** nothing V3 runs in
production (the credential blockers are in `docs/final_report.md` §5 and §6 below).

## 1. What was built and how to run it

### 1.1 Atlas: every indicator, every country (`docs/dev/v3_atlascore.md`, `v3_atlasops.md`, `v3_a_*.md`)

* **Storage and queries.** An atlas table is a content-addressed Parquet dataset in a fixed long schema (source,
  dataset, indicator, concept, ISO3, period, decimal value or text, unit, flags, raw sha256, selector). Tables are
  registered through the 0008 dataset tables, and a snapshot pins the table versions. DuckDB answers queries over
  a verified query file: a sidecar sha256 is checked on every open (review ATLAS-1), and the cache is private
  (0700).
* **Sources.** 108 atlas sources are declared in code (`uv run reva atlas list`, run for this report). They live in
  99 `atlas_*.py` modules and cover the World Bank, IMF, UN, ILO, WHO, UIS, UNDP, WHR, OWID, WID, V-Dem, Maddison,
  Clio-Infra, HYDE, FAO, BACI, UNCTAD, the CIA Factbook, laws (WBL, Constitute, CCP, SSA), geography (Natural
  Earth, GeoNames, CCKP), science (Nobel, WIPO, OpenAlex), treaties, culture, military (UN peacekeeping),
  resources, infrastructure, cities, colonial history, crises, contention, coups, sanctions, hazards and the
  GAPHUNT gap-fill offices (INSEE, CBS, SPC, Jersey, Eurostat NUTS). The 39 catalog files hold about 9,250
  concepts in 16 themes (`/v1/atlas/coverage`, ATLASOPS §7). Catalog concepts are `provider_declared`, not
  reviewed.
* **Countries.** Crosswalk 1.1.0 has 251 entities: every ISO 3166-1 code including Taiwan, plus `XKX` and `CHI`
  (`docs/dev/v3_xwalk.md`). It is agent-built and pending maintainer review. Unmapped provider codes are kept and
  listed.
* **Headline layer (cross-source fallback).** `registry/atlas/source_order/*.yaml` defines 27 REVA headline
  concepts. Each lists, in order, the provider series that measure the same quantity in the same way, with a
  written justification. It also lists `not_equivalent` series, which never answer, and cell rules that mark
  upstream projections, nowcasts and extrapolations. Every answer names:
  * the answering series and its rank;
  * every series consulted, with a status;
  * the true period of the cell;
  * a `source_break` where the answering series changes between years;
  * the licence of the cell (`rights`, with the `licence_restricted` warning).
  Projections never answer by default.
* **Operations.** `reva atlas list | ingest | snapshot`; job kinds `atlas_ingest`/`atlas_snapshot` (migration
  `0010_atlas_ops`); 39 monthly source schedules plus a daily snapshot. Every request is budget-metered
  (`budgets-1.3.8`). A run is refused when the connector version is not registered with the same code digest.
* **API (read scope):** `/v1/atlas/concepts`, `/countries/{iso3}`, `/map`, `/series`, `/coverage`,
  `/headline/{concepts,map,series,coverage}`.

### 1.2 World knowledge graph (`v3_kgcore.md`, `v3_wdharvest.md`, `v3_kghist.md`, `v3_kgpeople.md`, `v3_kgcult.md`, `v3_kgscieco.md`, `v3_kggeopol.md`, `v3_kgpolity.md`, `v3_kglang.md`)

* **Storage.** Schema `kg` (migrations `0009_kg`, `0011_kg_fixes`) holds entities, labels, allow-listed properties
  and typed, dated statements. Each statement records its precision, qualifiers, references, rank, source key,
  source revision and raw sha256.
  * Everything is append-only.
  * Retractions remove a statement from the current view; nothing is deleted.
  * Deprecated ranks are refused.
  * Only `reva_worker` can insert. The gate role has no `kg` access since 0011.
* **Wikidata access.** The harvester (`reva.kg.harvest`) sends one request at a time across the machine, honours
  Retry-After per endpoint and archives raw bytes. It has three access paths:
  * WDQS SPARQL;
  * `wbgetentities` / `Special:EntityData` snapshots, which carry the revision id, references and exact date
    precision;
  * CirrusSearch for discovery while WDQS is throttled.
  Precision is never inferred from a date's shape (review KG-2 fixed).
* **Query specs.** 116 YAML specs in 12 domains (core, history, people, culture, geopolitics, science, economy,
  nature, society, places, events, causal), validated by `python -m reva.kg.harvest validate`. There are also 20
  anchor chains with 80 expected facts (`registry/kg/anchor_chains.yaml`).
* **Dataset layers.** UCDP, UNGA votes, CShapes, Cliopatria polities with a computed territorial overlay,
  Glottolog/WALS/Grambank, the UN Treaty Collection, UNESCO (heritage, intangible heritage), the Olympics,
  peacekeeping, COW/ICOW colonial history, crises, contention, coups, sanctions, hazards, and cables, flights and
  space.
* **API:** `/v1/kg/entities/{qid}`, `/neighbours`, `/timeline`, `/v1/kg/path`, `/v1/kg/search`. Wikidata
  statements are labelled `community_edited_not_verified`.

### 1.3 Interconnections, the "neurons" (`docs/dev/v3_links.md`: LINKS, LINKS2, LINKWIRE, LINKS3, EVMAP)

* **Link types.** 38 types in `registry/links/link_types.yaml`, covering:
  * borders: Wikidata, Natural Earth, CShapes;
  * languages and language families;
  * colonial rule: Wikidata, CShapes, COLDAT, COW;
  * succession, memberships and alliances;
  * treaty co-parties (UN Treaty Collection) and conflicts (UCDP sides);
  * diplomatic relations;
  * flows: trade (IMF IMTS, BACI) and migrant stock (UN DESA);
  * UNGA vote agreement;
  * peacekeeping, historical polities and Glottolog;
  * cables, air routes and launch sites;
  * sanctions and shared hazards.
  Every link carries evidence (statement id and revision, or raw sha256 plus selector). A path says "connected
  via", never "caused by".
* **Store.** A materialised store (`kg.link_materialisation`/`kg.link_row`, migrations `0012_links_store`,
  `0013_links_ops`) keeps one materialisation per atlas snapshot plus KG watermark. It is written in one
  transaction by the worker (`reva links materialise`; job `links_materialise`, daily schedule). Without a
  materialisation, the country view derives links on demand and reports truncation.
* **Path engine.** k-paths with hub damping, time filters, ordered `steps`, `via_any`/`via_all` waypoints and
  non-transit kinds. On the recorded Wikidata claims, the 20 anchor chains give **18 found and 2 "no sourced
  path"**, exactly as expected (`tests/unit/links/test_links_anchor_chains.py`). For example, Columbus → Crown of
  Castile → Treaty of Tordesillas → Portuguese Empire → State of Brazil → Portuguese, with every hop citing its
  statement.
* **API:** `/v1/links/{ref}`, `/v1/links/pair?a&b`, `/v1/links/path`.

### 1.4 Events and causality (`docs/roadmap/v3_causality.md`; `v3_causcore.md`, `v3_causal_interface.md`, `v3_events.md`, `v3_causdata.md`, `v3_causwd.md`, `v3_causcand.md`)

* **Event backbone (EVENTS, EVMAP).**
  * `registry/events/taxonomy.yaml`: 82 event types (fr/en) and 19 roles, with Wikidata classes verified against
    recorded snapshots.
  * A rank-aware event view: typing, dated intervals, roles, part-of/follows structure, and causal statements
    listed as *asserted by* their source.
  * `same_as` only from evidence (a shared identifier or a mapping table), never from fuzzy name or date matching.
  * 11 event harvest specs.
  * API: `/v1/events`, `/v1/events/{id}`, `/search`, `/types`.
* **Causal store (CAUSCORE, migrations `0015_causal`, `0016_causal_ops`).**
  * Append-only tables for claims, retractions, grades, quarantined candidates (T5/T6) and owner-only reviews.
  * A closed relation vocabulary, and source tiers T1–T6.
  * A deterministic **A–E grade** computed from `registry/causal/grading.yaml` (version 1). The grade uses date
    precision windows for the temporal check and union-find over upstream references for independent
    corroboration. It always carries its rule ids.
  * A claim is never a fact. A chain is labelled "composition by REVA", graded by its weakest hop, at most 6 hops.
  * Daily `causal_refresh` job and `reva causal refresh`.
* **Claim loaders.**
  * Wikidata statements: P828, P1542, P1478, P1479, P1536, P1537. P1344 and P710 count as context only.
  * Expert-coded datasets (T1, codebook passages quoted verbatim, codebook PDFs pinned by sha256): ICB v16, COW MID
    5, UCDP termination and peace agreements, NAVCO/MEC/Mass Mobilization, Archigos coups, the ESRB crises
    database, TIES/EUSANCT/HSE sanctions, NOAA NCEI tsunami sources.
  * REVA never creates a claim by joining two datasets (`no_join_claims`).
* **Candidates (CAUSCAND).** Connective patterns in English and French Wikipedia, revision-pinned, linked only
  through the sentence's own wikilinks. They are quarantined (T5) and never mixed with claims.
* **API:** `/v1/causal/events/{id}`, `/causes`, `/effects`, `/why`, `/v1/causal/chain`, `/v1/causal/consistency`,
  `/v1/causal/candidates`.

### 1.5 Simulations for all countries (`docs/dev/v3_simworld.md`, SIMWORLD and SIMFIX)

All three model versions are experimental (`maintainer_review_pending`), and every output is a "conditional
projection under stated assumptions, not a forecast".

* **Cohort projection.** `demography.cohort_component@1.1.0` runs on UN WPP 2024 (reviewed built-in connector
  `un.wpp@1.0.1`, byte-range bulk CSV) for every WPP country, with `countries: ["ALL"]`.
* **Retirement age.**
  * `social.retirement_age@1.1.0`: 163 WBL 2026 rule tables transcribed by an agent (23 gaps), with ILOSTAT age-band
    rates.
  * `@1.2.0`: the age change applies only to the pension-covered share (ILOSTAT SDG 1.3.1).
* **Versions.** Version 1.0.0 of both models is unchanged and stays the default.
* **IO price model.** `economic.io_price` stays limited to the 49 FIGARO economies. OECD ICIO answers HTTP 403 to
  this environment, and EXIOBASE is non-commercial.

### 1.6 UI "Planète" (`ui/src/v3/**`)

Eight screens under `/v3`:

* **Neurones**: explore the graph from an entity.
* **Chemin**: how X is linked to Y; a path is not a cause.
* **Frise**: timeline of an entity.
* **Atlas**: choropleth of any concept, with gaps hatched and their reason shown, and licence badges.
* **Dossier pays**: every domain of one country, with the headline section.
* **Pays ↔ Pays**: all links between two countries over time, each one cited.
* **Causes & conséquences**: the graded causal graph, "Pourquoi ?" lanes, timeline, dissent panel, a chain finder
  with the "composition REVA" banner, and a toggle for the quarantined candidates labelled "non sourcé — candidats".
* **Demander**: natural-language questions.

There are 54 V3 TypeScript files. The Zod schemas are checked against real backend responses
(`ui/src/v3/api/contract.dump.test.ts`, which runs only when a dump file is given).

### 1.7 Natural-language questions (`docs/dev/v3_nlqa.md`)

`POST /v1/ask` answers in French or English from sourced data only:

* An LLM or a deterministic keyword planner writes a typed plan. Allow-lists validate it and reject anything
  outside them, never substitute.
* There are 11 operations: lookup, neighbours, timeline, path, atlas value/rank/series, links, links pair, `why`,
  `causal_chain`.
* Every sentence comes from a template and cites its evidence ids.
* Answers state projections and licences, and never rank across currencies.
* Causal answers say "selon <source> (Tn)", and a chain is called REVA's composition.

**The LLM planner was never run live**: OpenAI answered `insufficient_quota` and Gemini answered 402. The keyword
planner is what runs.

### 1.8 How to run it (V3 additions to `README.md`)

```bash
docker start reva-db || make up            # PostgreSQL 17 + PostGIS
uv sync && (cd ui && npm ci)
uv run reva db bootstrap                   # migrations to head 0017_links_retention, roles, seed
uv run reva atlas list                     # 108 atlas sources
uv run reva atlas ingest <source>          # one at a time, budget-metered (3 GiB/day cap)
uv run reva atlas snapshot --skip-unchanged --warm
uv run reva links materialise
uv run reva causal refresh
uv run python -m reva.kg.harvest run registry/kg/queries/<domain> --via api   # Wikidata, resumable
uv run reva api                            # UI "Planète" at http://localhost:8000/v3
uv run python scripts/e2e_stack.py --v3-fixtures --run   # disposable V3 stack + Playwright
```

Resume details, disk needs and the data snapshot branches are in `docs/dev/HANDOFF_V3.md`.

## 2. Git summary and migrations

* The branch has **284 commits**. **135 of them** came after the V1/V2 final report (`197e78c`, 2026-10-15 04:41
  UTC) and before this report (HEAD `8ff853d`, 2026-10-16 23:07 UTC): 35 on 2026-10-15 and 100 on 2026-10-16.
  72 of the 135 are "V3 WIP snapshot: in-flight agent work" commits made by the lead while agents were running.
* `git diff --shortstat 197e78c HEAD`: 1,759 files changed, +691,569 / −6,825 lines. Most of the added lines are
  recorded fixtures (`tests/fixtures`, 36 MB) and registry files.
* Tree now: 397 Python modules in `src/`, 322 test modules, 139 UI source files (54 under `ui/src/v3`), 52 V3
  agent reports `docs/dev/v3_*.md`.
* Migrations added in V3:

| Migration | Owner | Content |
|---|---|---|
| `0009_kg` | KGCORE | schema `kg`: entities, labels, properties, statements, retractions, current views, integrity triggers, grants |
| `0010_atlas_ops` | ATLASOPS | `atlas_ingest` / `atlas_snapshot` in the `ops.submit_job` allow-list |
| `0011_kg_fixes` | REVFIX | review KG-1 (reinstatement after retraction) and SEC-2 (gate has no `kg` access) |
| `0012_links_store` | LINKS2 | materialised link store (`kg.link_materialisation`, `kg.link_row`) |
| `0013_links_ops` | LINKWIRE | `links_materialise` job kind |
| `0014_recorded_at_window` | lead | server timestamps accept the column default evaluated earlier in the same statement (still reject pre-statement and future values) |
| `0015_causal` | CAUSCORE | causal claims, grades, retractions, candidates, owner-only reviews |
| `0016_causal_ops` | CAUSOPS | `causal_refresh` job kind |
| `0017_links_retention` | LINKRET | owner-only `kg.prune_link_materialisation`, `kg.link_prune` audit table, `reva links prune` |

  Schema contract: head `0017_links_retention` (`migrations/schema_contract.json`). The V3 schema fixes from the reviews
  and from operations shipped as new migrations (0011, 0014). This report did not check whether 0009–0016 were
  edited in place before 0016.
* **Data is not in the main branch.** The dev database, `var/objects` (~3.8 GB) and the Wikidata archive are
  container-local. pg_dumps and archives were pushed to branches `reva-data-20261016-b1` … `-b4`
  (`docs/dev/HANDOFF_V3.md`).

## 3. Actual test results (this run)

**Run conditions.** Agent V3FINAL ran these checks on 2026-10-16 between 23:05 and 23:58 UTC, on the shared 4-vCPU
session container. Two things affected the run:

* **The tree changed under the run.** It started on `e1ccf03` plus WDRUN's uncommitted `harvest.py` edit. During the
  run the lead committed the WIP snapshots `8ff853d` (version bumps of 12 atlas connectors), `e411fe1` (Wikidata
  harvest import, new tests, these docs) and `5af8df2`.
* **Other work ran at the same time.** LOADALL and WDRUN were working, and the e2e stack ran in parallel with the
  last 40 minutes of the pytest run.

DB tests used throwaway databases (`reva_test_*`, `reva_e2e_*`, dropped afterwards). The dev database was not used.

| Check | Command | Result |
|---|---|---|
| pytest offline | `uv run pytest -m "not live and not perf" -q -x --maxfail=50` | **4,074 passed, 12 failed**, 31 skipped, 66 deselected, of 4,117 collected (50 min 38 s). Each failure is diagnosed below |
| failing files re-run alone (current tree) | `uv run pytest tests/integration/<file>.py` for the 7 files with failures | **all pass alone**: events_datasets 6, kg_lang_ingest 3, kg_polity_ingest 3, links_db 4, links_ops 4, events_dataset_kinds 7, wikidata_kg_load 1. Run together, 6 of these files still give 7 failures (same causes) |
| `ruff check .` | | **2 errors → 0**: `ui/src/v3/test/dumpresp.py` (E401, I001; a UI capture helper), fixed by `ruff check --fix` |
| `ruff format --check .` | | **20 → 17 files would be reformatted** after formatting 3 non-product files (`dumpresp.py`, `tests/integration/test_wikidata_{kg_load,live}.py`). The remaining 17 are left to their owners (see below) |
| `lint-imports` | | **5 contracts kept, 0 broken** |
| `mypy -p reva.kernel -p reva.ports` | | **pass** (13 source files) |
| UI typecheck | `npm run typecheck` | **pass** |
| UI unit tests | `npx vitest run` | **221 passed, 1 skipped** (29 files passed, 1 skipped: `contract.dump.test.ts` needs a response dump) |
| UI build | `npm run build` | **pass**. The maplibre chunk is 1.03 MB (278 kB gzip) |
| UI e2e (V3 stack) | `uv run python scripts/e2e_stack.py --run --v3-fixtures` | **81 passed, 1 failed, 8 skipped** (Playwright 2.1 min; stack 3 min 41 s in total). The stack replayed 18 atlas fixture sources (all `completed`), the 76-entity Wikidata links fixture, the 37 causal fixture statements, a fixture snapshot and one link materialisation. V3 specs: `v3_live_stack` 13/13, `v3_planet` 10/10, `v3_causal` 7/7, `v3_causal_live` 6/6. The 8 skipped are V2 composer/runs specs that need `--recorded-drafter --v2-fixtures` |
| live-marked tests, Docker build, performance | — | **not run** for V3 |
| FIX3: the 5 affected files + link unit tests, **one session** | `uv run pytest -q tests/integration/test_links_db.py tests/integration/test_events_datasets.py tests/integration/test_events_dataset_kinds.py tests/integration/test_links_ops.py tests/integration/test_wikidata_kg_load.py tests/unit/links` | **170 passed, 0 failed** (4 min 6 s) |
| FIX3: same, after the files that polluted the shared DB in the sweep | the same files preceded by `test_causal_{api,ops,store,wikidata_demo}.py`, `test_kg_{api,import_harvest_db,lang_ingest,paths,polity_ingest,store}.py`, `test_atlas_treaties_ingest.py`, `test_events_view.py` | **260 passed, 0 failed** (5 min 7 s). The synthetic `czapi` claims and other `same_as` / materialisation / property loads are in the DB before the fixed tests run |
| FIX3: UI unit tests, typecheck, build | `npx vitest run`, `npm run typecheck`, `npm run build` | **225 passed, 1 skipped** (+4: `src/v3/screens.test.ts` 3, `CommandPalette.test.tsx` 1); typecheck **pass**; build **pass** |
| FIX3: palette e2e | `uv run python scripts/e2e_stack.py --run --v3-fixtures -- e2e/v2_runs.spec.ts e2e/v3_live_stack.spec.ts` | **15 passed, 0 failed, 4 skipped** (41 s). "⌘K palette" passes; `v3_live_stack` 13/13 (incl. `/v1/links/path` "Chemin advanced options"). The 4 skipped are V2 runs specs that need `--v2-fixtures` |

**The 12 pytest failures.** None of them is a stale version or count in a test. **Status after FIX3:** the 3 link
failures and the UI palette defect are fixed in the product, and the 4 isolation failures are fixed in the tests.
See the FIX3 rows above.

* **Test isolation: the session-scoped test database is shared by every test file (4 failures).** Each test passes
  alone. They fail after other files load KG or link data into the same database:
  * `test_events_datasets.py::test_same_as_from_evidence_only`: other loads add `same_as` pairs beyond the
    expected Paris Agreement pair.
  * `test_events_dataset_kinds.py::test_causal_claims_attach_to_typed_events`: the `czapi` claims do not attach
    to typed events after earlier loads.
  * `test_links_ops.py::test_handler_materialises_then_reuses_unchanged_inputs`: the KG watermark moved, so the
    test got a new materialisation (`4 == 3`) instead of a reuse.
  * `test_wikidata_kg_load.py::test_seed_spec_loads_into_kg_tables`: properties already registered by other files
    give `163 == 186`.
  **Fixed (FIX3), tests only; no product bug behind them.** Each test now asserts on its own data:
  * `same_as`: the test looks only at derived links that touch its own pair, and checks that nothing else links
    to either end. It loads only that report, and counts retractions against the active `same_as` statements.
  * `causal_claims`: claims are filtered to the `source_key`s of the ingests the module fixture ran.
  * `links_ops`: materialisations are counted from the test's first `materialisation_id`.
  * `wikidata_kg_load`: the check is `versions_inserted + unchanged == len(allow.properties)`. The property
    registry is append-only and versioned. A pid that was already registered with another definition gets a new
    version: neither inserted nor unchanged, but correct. None of the connectors that register Wikidata pids
    define them differently: all derive from `registry/kg/properties.yaml`. The 23 extra versions came from that
    file changing during the sweep.
* **Tree changed during the run (5 failures).** `test_kg_lang_ingest.py` (2) and `test_kg_polity_ingest.py` (3)
  failed with `ImmutableRecordViolation: connector atlas.glottolog@1.1.0 / atlas.cliopatria@1.0.0 is registered
  with different content`. Commit `8ff853d` bumped these connectors on disk while the pytest process held the old
  module. All 6 tests pass on the current tree.
* **Product defect, not fixed (owner LINKS2/EVMAP), 3 failures:** `test_links_db.py::test_path_columbus_to_portuguese_from_db`,
  `::test_links_http_api` and `::test_links_path_route_constrained_parameters` fail with `LinkError: not a date:
  '1998-08'` (and `'1993-01'`).
  * Cause: the link store serialises a month-precision temporal as `YYYY-MM` (`PartialDate.iso()`), and
    `reva.links.store.temporal_from_json` → `PartialDate.parse` accepts only `YYYY-MM-DD` (`_TIME_RE` in
    `src/reva/links/model.py`). Reproduced directly: `temporal_from_json({"kind": "interval", "start": "1998-08"})`
    raises `LinkError`.
  * Impact: any materialisation that holds a month-precision link (here from the dataset fixtures loaded by other
    tests, e.g. the sanctions and hazard kinds) makes stored-link reads fail. `/v1/links/path` answers **500**.
  * The tests pass alone only because no such link is materialised then.
  * **Fixed (FIX3).** `PartialDate.parse` now reads `YYYY`, `YYYY-MM` and `YYYY-MM-DD`, with signed and
    astronomical years and Wikidata `00` unknowns. It rejects month > 12 and day > 31.
  * `PartialDate.iso()` writes BCE years with 4 digits (`-0043`, was `-043`).
  * New `PartialDate.from_iso` and `Temporal.from_json` are the exact inverses of `iso()` / `to_json()`.
    `to_json` adds `<field>_precision` only for decade/century precisions, which a year string cannot carry.
  * `store.temporal_from_json` delegates to `Temporal.from_json`.
  * Round-trip unit tests in `tests/unit/links/test_links_model.py` cover year, month, day, BCE, year 0,
    5-digit years and precisions 6–8.

**Also found.**

* **UI e2e failure (product defect, owner UI3):** `v2_runs.spec.ts` "⌘K palette: deterministic search over
  countries and runs, keyboard only".
  * What happens: typing "algeria" shows a stale first option "Pays ↔ Pays — Brésil et Portugal" (the list has 6
    options while the status says "5 results"), so Algeria is not first.
  * Cause: `ui/src/v3/screens.ts` puts `/v3/relation?a=BRA&b=PRT` in `V3_PALETTE` twice: once as the screen entry
    and once as the example entry. The palette keys its options by `${kind}:${id}` (`CommandPalette.tsx`), so two
    items share one React key and React leaves a stale node behind.
  * **Fixed (FIX3).** The duplicate example entry is removed. `V3_PALETTE` is built through `uniqueByRoute`
    (first entry per route wins). `CommandPalette` also drops repeated screen routes, so no caller can produce a
    duplicate key.
  * Tests: vitest `src/v3/screens.test.ts` checks that palette routes are unique.
    `CommandPalette.test.tsx` renders a duplicated route, expects one option and no React "same key" warning.
    The second test fails without the dedupe.
* **`ruff format` leftovers (owners):**
  * 14 atlas connector modules and `src/reva/adapters/connectors/wikidata.py` / `src/reva/kg/harvest.py`. These were
    *not* reformatted: a reformat changes the connector code digest, and the code-identity check would then refuse
    the registered version until it is bumped. WDRUN is also editing the two Wikidata files.
  * Code blocks in three `docs/dev` reports (`v3_a_imf.md`, `v3_atlas_interface.md`, `v3_causal_interface.md`).

## 4. Live source coverage

### 4.1 Atlas in the dev database (ATLASOPS, `docs/dev/v3_atlasops.md` §7; not recounted here)

* **59 atlas sources** with at least one completed live run; **159 live tables, 87,542,065 rows** in their latest
  versions; 530 MB of Parquet.
* 1,594 archived live raw acquisitions (3.5 GB); 1,533 metered requests and 3.17 GB over two budget days.
* Live snapshot `b11f1c51…9768` (knowledge cutoff 2026-10-16T19:19:27Z); `atlas-duckdb/6` query file 6.4 GB,
  built in 886 s.
* API checks on that snapshot (temporary read token, revoked afterwards):
  * 9,250 catalog concepts, 8,730 with data in the year view;
  * the FRA dossier: 5,261 concepts with data;
  * `un.wpp.population` 2023: 237 countries; HDI 2023: 193; IMF current account 2025: 194.
* **Refused by the 3 GiB/day byte cap or by the provider on 2026-10-16, then still pending:** peacekeeping, the
  five GAPHUNT gap-fill sources, OpenAlex (Retry-After above the politeness cap) and RCS (OSF 503). WID holds a
  40-country subset. CShapes and the KG vote statements were deliberately not loaded into the dev database
  (non-commercial licence; size). LOADALL was loading further sources while this report was written; its counts
  are not here.

### 4.2 Countries per headline concept (dev snapshot `b11f1c51…`, ATLASOPS §7; crosswalk of 251 entities)

| Headline concept | Countries | Years | Latest year answered by |
|---|---:|---|---|
| `reva.population.total` | 238 | 1950–2025 | WPP 21, WDI 217 |
| `reva.health.life_expectancy` | 238 | 1950–2024 | WPP 21, WDI 217 |
| `reva.science.internet_users` | 230 | 1990–2025 | UN SDG 226, WDI 4 |
| `reva.labour.unemployment_rate_national` | 223 | 1947–2025 | ILOSTAT national 223 |
| `reva.geography.land_area` / `surface_area` / `reva.population.density` | 216 each | 1961–2024 | WDI 216 |
| `reva.economy.gdp_usd` / `gdp_per_capita_usd` / `gdp_real_growth` | 215 each | 1960/61–2026 | WDI 206–207, WEO 8–9 |
| `reva.environment.co2_per_capita` | 214 | 1750–2024 | OWID 214 |
| `reva.trade.merchandise_balance_imts` | 209 | 2000–2025 | derived (same source) 209 |
| `reva.law.homicide_rate` | 208 | 1990–2024 | UN SDG 199, WDI 9 |
| `reva.economy.gdp_per_capita_ppp` | 205 | 1980–2025 | WDI 197, WEO 8 |
| `reva.trade.current_account_gdp` | 204 | 1960–2026 | WEO 180, WDI 24 |
| `reva.trade.goods_balance_usd` / `goods_services_balance_usd` | 202 each | 1948–2025 | IMF BOP 202 |
| `reva.economy.inflation_cpi` | 201 | 1960–2026 | WEO 190, WDI 11 |
| `reva.finance.government_net_lending_gdp` | 197 | 1972–2026 | WEO 185, GFS 12 |
| `reva.society.hdi` | 193 | 1990–2023 | UNDP 193 |
| `reva.labour.average_monthly_earnings_lcu` | 192 | 1969–2025 | ILOSTAT 192 |
| `reva.labour.unemployment_rate` (modelled) | 189 | 1991–2025 | ILOSTAT modelled 189 |
| `reva.education.literacy_rate_adult` | 180 | 1970–2024 | UIS 180 |
| `reva.labour.minimum_wage_monthly_lcu` | 175 | 1980–2026 | ILOSTAT 175 |
| `reva.society.gini_income` | 171 | 1963–2025 | WDI 171 |
| `reva.population.total_1_january`, `reva.health.life_expectancy_national_tables` | 0 | — | GAPHUNT series not yet in the snapshot |

This table was computed **before** the review V3b fixes (HL-1/HL-2/LAB-1). Since those fixes, WDI population and
life expectancy after 2023 and ILOSTAT's 2025 nowcast are `projection_upstream`/`nowcast` and are excluded by
default. The "2025"/"2026" ends of some rows therefore no longer answer by default; the table was not recomputed.

**Gap matrix** (GAPHUNT, `docs/dev/v3_gaps.md`, `v3_gap_matrix_snapshot.md`; 251 entities):

* With any source REVA holds, population and area cover 251 entities, currency 249 and languages 247.
* With the headline series alone, population and life expectancy reach 238. The 13 entities without them are
  uninhabited or dependent territories (ALA, ATA, ATF, BVT, CCK, CXR, HMD, IOT, NFK, PCN, SGS, SJM, UMI).
* Cells (entity × concept) with no value from any source fell from 576 to 530 after the gap-fill sources.
* Minimum wage covers 175 entities. Eight of the countries without a value have **no national minimum wage**
  according to Eurostat (a provider statement, not a gap).
* Retirement age covers 198 entities and voting age 234.

### 4.3 Knowledge graph statements

**Wikidata** goes to the throwaway harvest database `reva_kg_wdharvest`, never the dev database.

* WDQS was throttled with HTTP 429 for the whole session; one successful SPARQL response was recorded
  (`v3_wdharvest.md`). The harvest therefore ran through `Special:EntityData`/`wbgetentities`:
  * `core.countries`: 249 entities, 60,538 statements;
  * `core.sovereign_states`: 442 entities, 64,410 statements, 760 requests;
  * `core.historical_states`: 3,913 entities, 42,431 statements, 3,881 requests, finished 2026-10-16 22:25 UTC.
    This figure comes from the container-local run report `var/kg/reports/kgharvest-20261016T181323-2d64cd.json`,
    which is not in git.
* At WDHARVEST's report time: 565 entities current, 70,706 current statements, 100 % with a source revision and 33 %
  with references.
* Country → QID: 249 of 251 mapped. ATA and PSE are ambiguous and listed for maintainer review, never picked.
* The ~110 domain specs (history, people, culture, science, economy, nature, events, …) are written and validated
  but **not harvested**: most need WDQS. WDRUN was running the next harvest while this report was written.

**Dataset KG layers.** All of these were full-scale loads into throwaway databases (later dropped), with 0
statements rejected unless stated:

| Source | KG statements | Report |
|---|---:|---|
| UNGA votes (Voeten) | 950,213 (930,813 vote statements) | `v3_kggeopol.md` |
| Glottolog 5.3 + WALS 2020.6 + Grambank 1.0.3 + Wikidata P1394 | 602,817 current, 27,194 entities | `v3_kglang.md` |
| NAVCO / MEC / Mass Mobilization | 337,325 | `v3_a_contention.md` |
| NOAA NCEI hazards | 174,629 (11,017 entities) | `v3_a_hazards.md` |
| COW MID 5.0/5.01 | 80,076 | `v3_causdata.md` |
| ICB v16 | 29,904 | `v3_causdata.md` |
| Cliopatria v0.2.1 + territorial overlay | 26,079 | `v3_kgpolity.md` |
| UN peacekeeping | 20,430 | `v3_a_military.md` |
| TIES / EUSANCT / HSE sanctions | 16,723 (TIES part) | `v3_a_sanctions.md` |
| COW territorial change + entity status / ICOW / COLDAT | 15,812 / 13,950 / 322 | `v3_a_colonial.md` |
| Archigos 4.1 | 14,603 | `v3_a_coups.md` |
| UN Treaty Collection (55 treaties) | 13,664 | `v3_a_treaties.md` |
| UCDP / CShapes 2.0 | 10,159 / 1,957 | `v3_kggeopol.md` |
| UCDP termination + peace agreements | 7,913 | `v3_causdata.md` |
| UNESCO heritage links / intangible heritage / Olympics | 3,918 / 2,244 / 1,952 | `v3_a_culture.md` |

What the dev database holds in `kg.*` today was not counted for this report (rule: the dev database is not read).
`docs/dev/HANDOFF_V3.md` says CAUSOPS was loading event and causal sources into it, and no CAUSOPS result file
exists (`docs/dev/v3_causops_live.md` is absent).

### 4.4 Links

* **No live materialisation is recorded for the dev database.** Review V3b found none (TRUNC-1), and no later
  report records one.
* LINKS2 demonstration (throwaway `reva_links2_demo`, live atlas snapshot of 11 tables plus the 76-entity Wikidata
  fixture): **384,583 links in 87 s** (`kg.link_row` 419 MB). They break down as:
  * UNGA agreement 144,043;
  * IMTS exports 97,944 and imports 98,520;
  * migrant stock 26,125;
  * UCDP same side 15,926;
  * CShapes borders 646 and colonial rule 343.
  Response times: pair view BRA–PRT 1.6 s warm, path BRA→PRT 2.2 s.
* EVMAP fixture materialisation:
  * sanctions 5,561 and US sanctions programs 20;
  * COLDAT 255, COW colonial rule 620;
  * shared hazards 23.

### 4.5 Causal claims by tier and grade

No A grade exists anywhere: A requires independent corroboration, which a single coded dataset cannot give itself.

**T1 (expert-coded) claims.** All from full-scale loads into throwaway databases, graded by the rules v1:

| Source | Claims | B | E (temporal inconsistency kept and shown) | Report |
|---|---:|---:|---:|---|
| NAVCO 1.2/1.3/2.1 + MEC + Mass Mobilization | 14,993 | 14,991 | 2 | `v3_a_contention.md` |
| ICB v16 + COW MID 5 + UCDP termination/peace agreements | 7,704 | 7,703 | 1 | `v3_causdata.md` |
| NOAA NCEI tsunami sources (coded triggers) | 2,186 | 2,171 | 15 | `v3_a_hazards.md` |
| TIES / EUSANCT / HSE sanctions | 1,483 | 1,483 | 0 | `v3_a_sanctions.md` |
| Archigos 4.1 coups → removals | 327 | 327 | 0 | `v3_a_coups.md` |
| ESRB financial crises (contributing factors) | 96 | 96 | 0 | `v3_a_ecrises.md` |
| **Total T1** | **26,789** | **26,771** | **18** | |

**Wikidata claims (T3/T4).** These come from CAUSWD (throwaway `reva_kg_causwd`), container-local report
`var/kg/reports/causwd_report.json` generated 2026-10-16 21:10 UTC. `docs/dev/v3_causwd.md` has no results
section:

* 389 causal statements (P1542 195, P828 143, P1479 21, P1478 16, P1537 9, P1536 5) → 389 claims;
* by tier: T3 (referenced) 112, T4 (unreferenced) 277;
* by grade: B 2, C 97, D 273, E 17;
* temporal check: 65 consistent, 19 overlapping, 13 inconsistent, 292 undetermined (mostly missing dates);
* 2 corroborated, 4 contested.

The 99-statement real Wikidata fixture used in tests gives 23 D, 10 C and 4 E (`v3_causcore.md`).

**Candidates (T5, quarantined):** 21 (15 EN, 6 FR) from 500 articles, 18 of 21 (86 %) correct on hand-check
(Wilson 95 %: 65–95 %). They were inserted into `reva_causcand_kg`; `kg.causal_claim` stayed untouched
(`v3_causcand.md`).

**Reviews:** no claim has a maintainer review (`review_status: unreviewed` everywhere).

### 4.6 Simulations (live throwaway runs, `v3_simworld.md`)

* Cohort 1.1.0 on WPP 2024, `ALL`, jump-off 2023 to 2050: **231 countries projected**. NIU, NRU, TKL, TUV and VAT
  were refused (open age below 85).
* Back-test 2013 → 2023, measured against WPP rather than the truth: 232 countries. Median total APE is 2.96 %
  with average-residual migration. The projection is worse than naive no-change for 47 countries and worse than
  constant growth for 99.
* Retirement, target age 62 from the 1965 cohort: 1.1.0 ran for **126 countries** and 1.2.0 (coverage-scaled) for
  **128**.
  * Countries refused: 74 without a rule table, 12–13 with missing ILOSTAT inputs, and those whose ages fall
    outside 55–64.
  * India 2030 employment change: +10.56 M under 1.1.0, +4.49 M under 1.2.0. That figure is still overstated,
    because SDG 1.3.1 counts non-contributory pensions.

## 5. Licences and restricted sources

Every atlas cell and headline answer carries `rights`. The API warns `licence_restricted`, `/v1/ask` adds a licence
note, and the UI shows "licence non commerciale" badges (review V3b LIC-1/LIC-2 fixes). Sources flagged for a
maintainer licence review (`docs/dev/HANDOFF_V3.md` and the agents' reports):

* **Non-commercial:**
  * IPU Parline (CC BY-NC-SA)
  * CShapes 2.0 (CC BY-NC-SA; not loaded into the dev database)
  * JST Macrohistory
  * Constitute / CCP (commercial use prohibited)
  * EXIOBASE ≥ 3.9 (not ingested)
* **No redistribution, or UN terms:**
  * ICOW
  * UN Treaty Collection (UN Terms of Use, recorded `open_licence: false` on `co_party` links)
  * OHCHR
* **Unclear or cite-only:**
  * WHR
  * OECD-WTO BaTIS
  * SPC Pacific
  * INFORM
  * Laeven-Valencia (© IMF)
  * Reinhart-Rogoff
  * ICB (no licence stated)
  * Archigos
  * COW ("no charging for access")
* **Share-alike or no-derivatives:**
  * V-Dem (CC BY-SA)
  * TI CPI (CC BY-ND)
  * UNESCO heritage (CC BY-SA)
  * Wikipedia sentences in the candidate store (CC BY-SA 4.0)
* **Not ingested because of terms:**
  * SIPRI military expenditure and arms transfers (written authorisation needed); WDI copies tagged `licence:check`
    are used instead.
  * Seshat (account and terms acceptance required).
  * Eora (not open).

## 6. Credential and owner blockers (unchanged from `docs/dev/HANDOFF_V3.md` unless stated)

* **LLM credits:** OpenAI (`insufficient_quota`) and Gemini (402) are exhausted. V3 NLQA has never run its LLM
  planner live, and V2 NL drafts are affected too.
* **Provider keys and accounts:** a Comtrade subscription key, an OpenAlex API key (the shared free budget was
  exhausted; cursor paging refused without a key), a Seshat account, and SIPRI authorisation.
* **Hosts that refuse non-browser clients:** Powell & Thyne coups, the Cline Center (Illinois Data Bank), ILO
  NORMLEX, UNODA, CITES/Ramsar, OECD ICIO bulk files and the ILO World Social Protection Database. None of them was
  circumvented. Someone has to ask the providers or allow-list REVA.
* **Wikidata capacity:** WDQS was throttled all session, and the budgets are `wikidata.wdqs` 1,500/day and
  `wikidata.wbapi` 4,000/day. The full domain harvest needs several budget days. A ledger defect booked all
  Wikibase traffic to the WDQS account (found by CAUSWD). WDRUN's fix (one ledger operation per source key,
  `src/reva/kg/harvest.py`) was committed in the WIP snapshot `e411fe1` while this report was written. The same
  snapshot also added `reva.kg.import_harvest`, an import of the harvest database into a target database, which
  this report does not cover.
* **Deployment** is still blocked on the V1/V2 credentials (Render, S3 bucket and scoped keys, admin password):
  see `docs/final_report.md` §5.
* **Human reviews nobody has done yet:**
  * the 163 WBL retirement rule tables;
  * the headline source-order equivalences (`agent_declared_equivalence_not_human_reviewed`);
  * the crosswalk 1.1.0 and the manual country aliases;
  * the ATA/PSE QIDs;
  * every causal claim (`kg.causal_review` is owner-only);
  * every V3 model version.
* **Privacy:** the environment exposes the owner's e-mail address to agents. Two agents put it once in a
  User-Agent header (Wikidata, paulhensel.org). For this report, `git grep` found the address nowhere in the
  committed tree, and the Wikidata client sends a generic project User-Agent (`v3_wdharvest.md`). The provider
  request logs cannot be recalled. Removing the variable from the environment prevents a recurrence.

## 7. Scientific and honesty limits

* **A link is not a cause; a causal claim is not a fact.** REVA stores what sources assert. Grades describe the
  evidence for the claim as sourced, never the truth of the causal link, and there are no probabilities. Chains
  are REVA's composition of separately asserted claims. "Independent" corroboration means a *different cited
  reference*; REVA has not verified that the sources really are independent.
* **The temporal check uses event starts.** "WWI has effect Russian Empire" is compared with the empire's
  inception and graded E: REVA shows the claim, never reinterprets it. Most Wikidata claims are `undetermined`
  because dates are missing (292 of 389).
* **T1 grades are B by construction.** A single coded dataset cannot corroborate itself, so 26,771 B grades say
  "one expert-coded source", not "well established".
* **Wikidata is community-edited.** Statements are labelled as such and never "verified". WDQS rows without a
  snapshot keep the revision explicitly unknown. Wikidata yields no T2 claims, because DOI/ISBN references are
  not on the reference allow-list of the main harvester (CAUSWD keeps them for causal statements).
* **The atlas reports what each source reports.** The headline layer chooses a source per country and year. The
  equivalences between series are agent-declared, not reviewed. Source changes inside a series are flagged
  (`source_break`), not smoothed. Derived values (ratios and differences) are same-source only. Projections,
  nowcasts and extrapolations never answer by default. The fallback is still a choice among definitions: WPP
  France is metropolitan, the WDI includes the overseas departments, and the WDI never answers for FRA or MDA.
* **Coverage is uneven.** Small territories and uninhabited entities have most of the gaps (§4.2). The WID dev
  table is a 40-country subset. The atlas query file needs 6.4 GB and ~15–30 s for the first open in each process.
* **Simulations** are the V2 models extended. They are experimental: constant mortality and frozen base-period
  migration in the cohort model, and transferred European response estimates plus WBL ages transcribed by an
  agent in the retirement model. The cohort back-test is a consistency check against WPP, not against censuses.
* **NLQA.** The keyword planner is lexical. LLM behaviour is unverified, and the rephrase check is lexical.

## 8. Independent reviews and fixes

* **Review V3** (`docs/dev/review_v3.md`, agent V3REVIEW, 2026-10-15) covered the KG, the atlas core, links, NLQA
  and eight atlas sources. It found **0 critical, 4 high, 9 medium and 10 low** findings, with 14 strict-xfail
  reproductions and 20 spot checks against live providers.
  * The high findings:
    * ASK-1: rankings across currencies.
    * ASK-2: projections stated as facts.
    * KG-2: false date precision on WDQS dates.
    * ATLAS-2: non-calendar cells reported as absent.
  * Fixes:
    * commit `01f84bb`: the NLQA, KG-1/SEC-1/SEC-2 (migration `0011_kg_fixes`), KG-3 and LINKS-1/2/3 findings;
    * commit `8fc9ca7`: ATLAS-1…6 and DATA-1;
    * WDHARVEST: KG-2 and PROV-1 (precision never inferred, snapshot revisions, `precision_unknown`).
  * No `xfail` marker remains in `tests/review_v3`. The low findings without reproductions (ASK-5, ASK-6, KG-4)
    are addressed in code but were not re-reviewed.
* **Review V3b** (`docs/dev/review_v3b.md`, agent V3REVIEW2, 2026-10-16) covered the headline layer, the links
  store, the world simulations, a sample of the new sources and the Pays ↔ Pays screen. It found **0 critical,
  3 high, 6 medium, 3 low and 1 info** findings, with 10 strict-xfail reproductions and 20 spot checks.
  * The high findings:
    * HL-2: WDI 2024–2025 population values are WPP projections, served as actuals.
    * HL-1: WPP and WDI series spliced without a break flag.
    * LIC-1: restricted licences invisible on answers.
  * Agent REVFIX2 fixed **all 13**; ASK-7 only partly (no headline operation in NLQA). The fix table is in the
    review file, and no `xfail` marker remains in `tests/review_v3b`.
* No V3 finding was re-reviewed by a third party.

## 9. Open items

1. **Wikidata harvest** (the biggest gap): about 110 domain specs remain. Merge the pending property requests
   (P1923, P8031, P8032, P726, P991, P1590, P1339), and recreate
   `reva_kg_wdharvest`, which was migrated to an earlier 0015 revision (HANDOFF).
2. **Dev-database loads to finish and record:**
   * the sources refused on 2026-10-16 (§4.1);
   * the full WID run;
   * a live link materialisation;
   * `reva causal refresh` over the live KG and the event/causal sources (no CAUSOPS result file exists).
3. **Links store retention.** Each full materialisation is ≈ 400 MB, and nothing deletes old ones.
4. **Docs that predate V3:** `docs/api.md`, `docs/limitations.md` and `docs/architecture.md` have no V3 section.
   `contracts/openapi.json` (29 V3 paths) and the agent reports are the reference meanwhile.
5. **Ops gaps:**
   * `api/routes/system.py` `CANCEL_SCOPE` has no entry for `atlas_ingest`/`atlas_snapshot` (ATLASOPS §9);
   * no ingest schedule for `un.wpp` / `ilostat.*` (SIMWORLD);
   * NL drafting targets only the 1.0.0 model versions;
   * NLQA has no headline-concept operation (ASK-7).
6. **Licence decisions** (§5) before any non-private or commercial use.
7. **Cleanup** of leftover throwaway databases (`reva_test_*` and the agents' `reva_*` databases on the local
   server).
8. **Defects and debt found by this sweep (§3):**
   * link-store month-precision temporals crash stored-link reads (`/v1/links/path` 500);
   * duplicate ⌘K palette key (`v2_runs` e2e failure);
   * four integration tests depend on test order (shared session database);
   * 17 files are not `ruff format`-clean.

## 10. What this report did not verify

* Current contents of the dev database `reva` (atlas tables, KG, links, causal claims): the report quotes the
  agents' last reports.
* Any live provider request: no live-marked test was run (`-m live` was excluded by design).
* The figures from throwaway loads that were dropped (most of §4.3–4.6): they come from the agents' reports and
  cannot be re-read.
* The LOADALL and WDRUN work in progress.
* Performance on deployment hardware (no deployment).
