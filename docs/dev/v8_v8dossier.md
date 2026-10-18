# V8DOSSIER — one-click briefs (`/v8/dossier`)

Agent V8DOSSIER (index 11), feature `dossier` (route `dossier`, order 90, `g b`). Status: **done** — six showcase
briefs generate from the dev DB `reva`, render in the console, export to Markdown / print-ready HTML / JSON.

## What was built

### Backend — `src/reva/v8/dossier/` + `src/reva/api/routes/v8_dossier.py`

| Endpoint | What |
|---|---|
| `GET /v1/v8/dossier/subjects?q=&kind=&limit=` | featured briefs of every template + lookups: countries (crosswalk FR/EN names, ISO3), KG diseases (`mondo:`/`doid:`), drugs (`chembl:`), UCDP armed conflicts, reference decisions |
| `GET /v1/v8/dossier/{kind}/{id}?format=json\|md\|html&refresh=` | the brief (`reva-dossier/1` document), its Markdown export, or its print-ready HTML (A4 `@media print`, inline SVG charts, footnotes as superscript links; the reader prints to PDF — no PDF library) |

`read` scope, read-only (api role). Response model `Document` (pydantic). Kinds: `country`, `commodity`, `disease`,
`drug`, `conflict`, `decision`.

* **Templates are declarative YAML** — `registry/dossier/{country,commodity,disease,drug,conflict,decision}.yaml`:
  sections → blocks; each block = one data query + templated sentences / list item / table columns / chart / an
  `empty.what` used when the query has nothing. Subjects: `resolver` (`country` any ISO3; `kg` any Mondo/DOID id;
  `chembl_drug` any ChEMBL molecule — parent + salts via `chembl:parent_molecule` + eEML records; `ucdp_conflict`
  any `ucdp:conflict:<n>` — location countries via `ucdp:location`; `decision` any reference decision; `curated`)
  plus curated `subjects:` bindings (e.g. `crude_oil` → EIA concepts, STEO table, forecast family, BEAT file,
  effect variable, causal episodes, GPPD fuel, decision instruments; `malaria` → Mondo/DOID/EFO/MeSH/ICD-11 ids,
  WHO GHE cause 220 chosen by its verbatim WHO title, GHO incidence, WB bed nets), `generic:` parameters for
  non-curated subjects, `when:` to run a block only if a parameter exists, `vocab:` for French display of source
  codes (`{type|vocab}` keeps the exact code). Template version = sha256(12) of the file.
* **Queries** (`queries.py`, registry `QUERIES`, 22 functions) over existing services only: atlas query layer
  (`headline_dossier`, `best_cells` → rankings / country series / coverage), registered atlas tables newer than the
  snapshot (`latest_version` + `read_atlas_table`, sliced and cached per artifact sha: EIA STEO Brent/WTI, EIA world
  aggregate, WHO GHE, VIEWS), KG (`kq.neighbours`, `kq.timeline`, literals, yearly quantity series, `rights_for`),
  causal claims (`cg.why`, `cg.effects`), effect estimates (`eq.list_estimates`, `get_estimate`), forecast registry
  (compact index, below), BEAT results file (`docs/dev/v7_beat_energy_result.json`), decision engine
  (`run_on_connection`, 2000 draws, seed fixed). One new SQL: `kg.facilities` (GPPD plants by fuel, count + capacity
  sum per country, labelled « calcul REVA »).
* **Generator** (`generator.py`): every sentence is ONE template sentence whose `{placeholders}` are filled with
  sourced values (`Val`: text, exact decimal, unit, honesty kind, `Src` footnote) or explicit `Miss` (what +
  reason). A sentence with a missing value uses its `missing:` template, else becomes a hatched Missing block; an
  unknown placeholder is a `TemplateError` (never an empty string). Footnotes are deduplicated (source, dataset,
  indicator, period, reference, method) and **numbered in reading order** (summary first). Document = summary
  (« L'essentiel », answer first), sections, `not_covered` (static per template + every Missing of the run),
  `footnotes`, distinct `licences` (restricted first), `honesty` rules, `meta` (template + version, data snapshot id,
  atlas sha, timings per block).
* **Cache**: key = (kind, id, template version, data snapshot id, origin) — in-process LRU + JSON files in
  `$TMPDIR/reva-dossier-cache/`. The data snapshot id = sha256(16) of atlas snapshot sha + newest
  `evidence.dataset` recorded_at + max `kg.load` / `kg.causal_load` / `kg.effect_load` ids + newest
  `model.evaluation` (so any ingest, KG load, causal/effect load or registry batch invalidates).
* **Forecast registry index** (`registry_index.py`): the registry read side re-reads up to 109 sealed batch
  manifests per question (LRU of 16 → thrashing: `forecasts.geo` took 318 s for France). The index reads each
  manifest once (`load_frozen_manifest`, sha-verified), keeps question fields + each model's point and 5 %/95 %
  quantiles (point-only display rule respected), joins outcomes, cached per batch set (memory + JSON): now 55 ms warm.

### UI — `ui/src/v8/features/dossier/`

`index.ts` (feature; palette commands « dossier FRA », « dossier pétrole », one per showcase brief; palette search via
`/subjects`, API client lazy-loaded — no zod in the shell's first-load bundle, per V8SHELL's request), `DossierPage`
(picker + brief), `Reader` (document), `Chart` (SVG line / bar / fan with hover tooltip, gaps stay gaps, published
bounds as a band, projections dashed), `Footnote` (superscript chip → source card on hover/focus, tap pins, Escape
closes; NC flag in licence colour), `links.ts`, `useLoad.ts`, `dossier.css` (tokens only, serif reading font).

* `/v8/dossier`: search box (`/` focuses, ↑↓ Enter), kind filter chips, featured cards (11 ready briefs).
* `/v8/dossier/<kind>/<id>` (one URL per brief): sticky table of contents with per-section count of declared gaps,
  exports (Version imprimable (PDF) → `?format=html` in a new tab, Markdown, JSON downloads), « Recalculer »;
  the document: kicker + link to the Fiche, title, meta line (values, gaps, notes, generation time, snapshot),
  honesty legend (observé / estimation publiée / projection / prévision scellée / scénario non validé / calcul REVA /
  manquant), « L'essentiel », sections, « Ce que ce dossier ne couvre pas », « Règles de lecture », restrictive
  licences warning, « Sources » (numbered, anchor targets).
* One chip per run of values citing the same note; a table column citing one note shows it once in its header;
  missing table cells read « manquant » (hatched) with the reason on hover.
* Phone: single column, TOC collapsed, long source ids wrap, tables scroll inside their box — no page scroll at 390 px.
  Dark theme from tokens. Print stylesheet hides the console chrome.

## The six briefs (dev DB, 2026-10-18)

| Brief | Sections | Templated sentences | Sourced values | Notes | Declared gaps | Not covered |
|---|---|---|---|---|---|---|
| France (`country/FRA`) | 7 | 48 | 374 | 47 | 7 | 10 |
| Pétrole brut (`commodity/crude_oil`) | 10 | 30 | 713 | 43 | 0 | 5 |
| Paludisme (`disease/malaria`) | 6 | 48 | 179 | 20 | 2 | 6 |
| Imatinib (`drug/chembl:CHEMBL941`) | 5 | 33 | 48 | 16 | 1 | 5 |
| Russie – Ukraine (`conflict/ucdp:conflict:13243`) | 5 | 49 | 226 | 42 | 0 | 4 |
| OPEP+ −2 mb/j (`decision/opec_plus_cut_2mbd_2027q1`) | 4 | 19 | 242 | 26 | 3 | 6 |

Content highlights: France — headline economy/society (WDI, IMF, ILO, UN, PNUD…), Wikidata identity (head of state
and currency stated missing: the entities are not labelled in the KG), memberships, borders, dated facts, 49 effect
estimates covering FRA, 53 sealed registry questions (15 families, 0 resolved → « non jugée »), the 3 headline
indicators without any value. Crude oil — EIA world production 84 383 kb/d (2025), top producers / consumers /
exporters / importers (EIA, latest year ≥ 80 % of countries report), Brent 114,2 $/bbl (Sept. 2026, STEO history;
max 132,7 July 2008), WTI, 19 sealed Brent questions (REVA ensemble median + 90 % band vs EIA STEO reference, not
yet judged), BEAT-ENERGY verdict (REVA beats EIA at h0 only, MAE ×0,44 after Holm; h1, h2 do not), 7 published
oil-price elasticities, attributed causes/consequences of the 1970s oil crises, 2 320 oil-fired plants (GPPD,
calcul REVA), 2 simulable decisions. Malaria — Mondo + DOID verbatim definitions, classification, forms, signs,
WHO GHE deaths 600 557 in 2023 (somme REVA of 183 country estimates; Nigeria 185 829), DALYs, death rates,
GHO incidence ranking, bed-net surveys (latest per country since 2015), 25 drugs (ChEMBL + WHO EML). Imatinib —
identity, forms/identifiers, 4 mechanism targets, 10 ChEMBL indications, WHO EML recommendations, lists and history.
Russia–Ukraine — UCDP facts (interstate, government and territory, Crimea, intensity 2), parties, battle-related
deaths per year with published bounds (fan chart), 372 588 over 4 years (somme REVA), 50 ADM1 regions, PA-X
agreements, VIEWS projections for RUS/UKR (third party, not judged), Wikidata causes/consequences graded A–E.
OPEP+ — the decision, the stated scenario assumption, propagated variables (price +12,5 % point estimate: band
stated missing because the estimate publishes no uncertainty), the 4 cited estimates with their applicability notes,
unquantified analogue claims, the engine's gaps, Brent context and the no-decision sealed forecasts.

## Performance (measured on the shared dev machine, load average 16–19 on 4 cores)

| Brief | Cold process (incl. atlas file sha256 check, registry index) | Warm, uncached | Cached (same data snapshot) |
|---|---|---|---|
| France | 32–67 s | 1.4–3.7 s | 70–115 ms (JSON), 18–37 ms (md/html) |
| Pétrole brut | 9.4 s | 1.0–4.5 s | idem |
| Paludisme | 4.4 s | 0.44–1.5 s | idem |
| Imatinib | 0.45 s | 0.24–0.6 s | idem |
| Russie – Ukraine | 1.1 s | 0.17–0.87 s | idem |
| OPEP+ | 18 s (decision engine) | 0.16 s (engine memo) – 6.8 s | idem |

The page-bundle p95 < 300 ms target holds only for cached briefs. Uncached generation is a document build (10–50
queries); the slow parts are atlas rankings over all countries (0.5–0.9 s each), the headline dossier (1.4 s) and
the decision engine (6–18 s). Cache hits are rarer than they should be on the shared dev DB because other agents keep
writing `evidence.dataset` / `model.evaluation`, which changes the data snapshot id (correct invalidation, by design).

## Honesty choices

* No free text: every sentence = a template sentence (tested: text runs equal the template's literal segments; no
  digit in template text; every `template_id` exists in the template). Numbers come only from values.
* Every value has a footnote with source title, dataset, series/property, period, licence (or « non indiquée »),
  reference (raw artifact sha256, statement key, estimate key, question id, chain signature), status
  (community-edited, published estimate, sealed forecast, unvalidated scenario…), method for REVA computations.
* Honesty kinds drawn differently: observed, estimate (WHO GHE, UCDP best), projection (VIEWS, EIA STEO), sealed
  forecast, unvalidated scenario, « calcul REVA » (sums, counts — method in the note), attributed claim.
* Missing is a hatched block / part with its reason (e.g. « entité Q3052772 sans libellé chargé dans le graphe »,
  « estimation ponctuelle : aucune incertitude publiée sur ce lien »), never 0; every gap also lands in « Ce que ce
  dossier ne couvre pas » next to the template's static exclusions (trials, reserves, refineries, HS 2709 flows…).
* Causal statements only as « Selon <source> (niveau, note) : A a causé B » rows; REVA never asserts causation.
  Effect estimates cited, never pooled. Predictions: « non jugée » until resolved; BEAT verdicts quoted with Holm.
* A failing source never breaks a brief: the block becomes « source indisponible lors du calcul (<error type>) ».
* Curated bindings (GHE cause 220, Wikidata events of the Russo-Ukrainian war, crude-oil concepts) are explicit in
  the YAML, never fuzzy name matches.

## Tests

* `tests/unit/v8/test_dossier_generator.py` (15, no DB): sentences = templates filled; every value footnoted with
  source/licence/period; missing stated (Missing block, `missing:` template, list `item_missing`, compact table
  cells), never 0; footnotes deduplicated and numbered in order; unknown placeholder = error; vocab keeps exact code;
  exports keep every footnote; all six registry templates consistent (known queries, every `$param` provided for
  curated and generic subjects, no number in template text); French number formatting keeps exact values.
* `tests/integration/v8/test_dossier_devdb.py` (6, opt-in `REVA_V8_DEVDB=1 -m db`, reads the dev DB): the six
  showcase briefs generate with ≥ 40 values, every sentence from its template, every value/table cell/chart series
  footnoted, licences on numbers, `not_covered` non-empty, exports complete — **6 passed**.
* `ui/src/v8/features/dossier/dossier.test.tsx` (8, vitest): values + chips, chip collapsing, column notes, hatched
  missing with reason, source card (licence NC, period), section order (answer first … sources), routing with
  encoded ids, export links, picker, palette commands, chart scale.
* ruff + mypy clean on my files; `tsc` clean for the feature; `tests/invariants` 191 passed.

## Screenshots (scratchpad `v8/V8DOSSIER/shots/`, reviewed and iterated)

Picker (cards grid of the 11 ready briefs, kind chips, search with `/` hint). Crude oil desktop: sticky TOC +
exports at left, serif document, « L'essentiel » box, EIA world production line chart, top-10 producers bar chart and
table (unit in column header, one note per column), Brent chart, forecast table with hatched « manquant » where EIA
published no reference for the month. Malaria: verbatim definitions, classification lists, GHE bar chart + table
(estimates in teal). Decision: orange dashed scenario values, hatched gaps with French reasons, cited hops.
Conflict in dark theme: fan chart with UCDP bounds band, ADM1 list, PA-X agreements with NC-flagged notes (PA-X
licence), VIEWS dashed projections. Phone (390 px): no horizontal scroll on all six (fixed: legend nowrap, long
source ids in Missing blocks); TOC collapsed. Iterations fixed: repeated footnote chips, « décès décès »,
`(deaths)` chart units, duplicate forecast rows per month, duplicate claims (has_cause/has_effect pairs),
duplicate labels (Mondo/DOID forms, Kyiv), Q-ids shown as names, footnote numbering out of reading order.

## What is missing and why

* Trials (ClinicalTrials.gov not loaded), gene–disease associations, oil reserves (EIA INTL.57-6 not in the atlas),
  refineries, HS6 crude flows, drug-level FAERS, SIPRI — stated in each brief's « ne couvre pas ».
* France's head of state / currency: the Wikidata entities are referenced but not loaded in the KG (stated missing).
* Rankings use « latest year ≥ 80 % of the max country count » (EIA crude exports → 2018); stated in the honesty
  rules, not hidden.
* Uncached generation is seconds, not < 300 ms (see Performance); a nightly pre-warm of the showcase briefs per data
  snapshot would make the first view instant (not done: no scheduler owned by this agent).
* A commodity needs a curated binding (only `crude_oil` today); any country, Mondo/DOID disease, ChEMBL drug,
  UCDP conflict and reference decision works generically.

## Requests to the lead

1. `contracts/openapi.json`: the contract test needs regeneration for the two new paths
   (`/v1/v8/dossier/subjects`, `/v1/v8/dossier/{kind}/{sid}`). I did not commit it because the export reads the
   working tree, which holds other agents' uncommitted v8 routes; please regenerate once after merging the wave
   (`uv run python scripts/export_contracts.py`).
2. Bundle check: `node ui/scripts/v8-bundle-report.mjs` needs a build (not allowed for me); `index.ts` now imports
   only `react`, the contract types and `links.ts` (no zod).
3. Forecast registry read side: `ManifestCache(size=16)` thrashes with 109 batches (`R.question` re-reads manifests
   per question; the registry API is slow for the same reason). Raising the size (manifests are immutable) or
   reusing my `registry_index` approach would speed up `/v1/forecasts/questions/{id}` and V8SCORES too.
4. No new dependency.
