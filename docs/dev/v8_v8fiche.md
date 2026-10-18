# V8FICHE — the universal entity page (« Fiche »)

Agent V8FICHE, wave V8 (2026-10-17/18). Route `/v8/fiche/:kind/:id` (feature `fiche`, nav-hidden, reached from the
palette, from every `v8Links.entity(kind, id)` and from the shell's search hits). ONE bundle call per page.

## What was built

### API (`src/reva/api/routes/v8_entity.py`, logic in `src/reva/v8/entity/`)

| Route | What |
|---|---|
| `GET /v1/v8/entity/{kind}/{id}` | the whole page: identity, headline (3–6 numbers chosen per kind), facts, relations by family (+ related forms), series, top countries, space (place chain, point, country outline), dated statements, causal claims in/out, effect estimates, forecasts, « ce qui manque », sources, meta (timings, cache, atlas snapshot). `?fresh=true` bypasses the process cache. |
| `GET /v1/v8/entity/{kind}/{id}/relations?family=&limit=&cursor=` | next pages of one relation family (« Afficher plus »). |

Pydantic models are all prefixed `V8Fiche…` (no name clash in the OpenAPI document). Read-only, `read` scope.

**Resolution** (`resolve.py`, lenient on the URL, strict on the answer — one record or 404, never a guess):
`country/<ISO3>` in any case (`FRA`, `fra`), ISO alpha-2 (`country/FR`), any KG id (`Q142` → canonical
`country/FRA`); per-kind aliases and id templates from `profiles.yaml` (`drug/CHEMBL941` → `drug/chembl:CHEMBL941`,
`chemical/CHEBI:27732` → `compound/chebi:27732`, `conflict/13243` → `armed_conflict/ucdp:conflict:13243`,
`company/…` → `legal_entity/…`); anchor pages (`commodity/crude_oil`, aliases `brent`, `pétrole`, `wti`…); special
kinds used by V8API search hrefs: `question/fq_…`, `effect/<estimate_key>`, `source/<source_key>`. The bundle
returns the canonical `{kind, id}`; the UI replaces the URL with it (no history entry). An ISO code held by several
KG records is refused (404 listing the candidates), never picked. A generic `unclassified` Wikidata snapshot no
longer hides the `country` kind another source gives the same id.

**Profiles** (`src/reva/v8/entity/profiles.yaml`): per kind, URL aliases, id templates, 3–6 headline specs
(`atlas_headline`, `literal`, `family_count`, `relation`, `anchor_series`, `anchor_top`), expected facts/series for
« ce qui manque », authority links (Wikidata, ChEBI, HGNC, Mondo/OBO, EFO, ChEMBL, GLEIF, GBIF, UCDP, UniProt, MeSH,
UNII, NCBI, …) and external-id value links (DrugBank, Ensembl, NCBI Gene, ATC, OpenCorporates, OMIM, ICD-10…).
Headlines chosen: country — population, GDP (US$), real GDP growth, CPI inflation, life expectancy, unemployment
(REVA headline concepts: cross-source best value with who answered); compound — formula, mass, charge, elements,
classes/roles; gene — symbol, cytogenetic locus, locus type, encoded protein, chromosome; disease — parent classes,
associations (drugs, phenotypes), ICD-10 (WHO), OMIM; drug — max phase, first approval, ATC, mechanism targets,
indications (targets/indications read *via* the salt form ChEMBL declares, e.g. imatinib → imatinib mesylate
CHEMBL1642, shown as « via IMATINIB MESYLATE »); company — jurisdiction, status, creation, HQ city, subsidiaries,
direct parent (or the parent-reporting exception the LEI record declares); facility — capacity, latest generation
(series), primary fuel, country, commissioning; conflict — battle deaths (latest year + series, with the source's
low/high bounds), start, type, intensity, territory, event locations; taxon — rank, authorship, parent, related taxa.

**Anchor page `commodity/crude_oil`**: the KG has no « crude oil » record, so a REVA grouping page gathers the
nomenclature records (HS 2709/270900 in HS 1992 and 2022, CPC 12010, SITC 333, ISIC 0610, GPPD fuel « Oil »), the
EIA series (Brent spot RBRTE; world crude production, world liquids consumption, OECD stocks), the top producers
/ exporters of the EIA catalog concepts, 7 forecast families (Brent, WTI, world/US crude, OECD stocks…) and 4 effect
variables (global oil price, world oil production/consumption, energy prices). Labelled everywhere as « page
d’ancrage REVA » (badge, description, `identity.anchor = true`, `warnings: anchor_page`); every value keeps its
own source.

**Reused services**: `reva.kg.family_query` (families, places, rights), `reva.kg.query` (neighbours, timeline,
labels), `reva.causal.graph` (causes/effects), `reva.effects.query`, the forecast registry (`read.batches`,
`outcomes`, `_score_rows`, sealed manifests via `ledger.load_frozen_manifest`), the atlas query layer
(`headline_dossier`, `table_cells`, `map_values`, `atlas_tables_as_of`, `read_atlas_table`), `world.display_geometry`.

**Engineering**:
* Atlas work runs in a worker thread beside the Postgres work; each section runs in a SAVEPOINT and is timed, so a
  failed query (e.g. a statement timeout on a hub) is reported in `meta.errors` and as a « Manques » item without
  aborting the other sections.
* Hub mode: families with an incoming property used > 500 times (a country is the object of thousands of « country »
  statements) are left out of the full family scan and read as one page + an approximate count (`count_approx`,
  shown « ≈ »).
* Forecast registry index: the 23 sealed batches are 67 MB of manifests (~20 s to read); they are compacted once per
  process in a background thread (only new batches later). While it builds, the section says so
  (`meta.warming`, « Registre des prévisions en cours d’indexation ») and the bundle is not cached.
* Process cache: bundle keyed by (kind, id, origin, data version = max KG/causal/effect load ids + latest registry
  evaluation); resolution cache; atlas query re-resolved at most every 2 min; validated pydantic model reused on hits.
* Series whose table is recorded but not yet pinned by the atlas snapshot (e.g. `eia.spot_d` Brent daily, loaded
  after the 01:52 snapshot) are read from the published table artifact (sha256-verified) and labelled
  `out_of_snapshot` / « hors instantané »; when the table is not even recorded, « Manques » says so.

### UI (`ui/src/v8/features/fiche/`)

`index.ts` (feature, palette commands « Ouvrir une fiche (ISO3, Q-id, chebi:…) » and « Fiche : pétrole brut »),
`FichePage.tsx`, `sections.tsx`, `MiniMap.tsx`, `api.ts` (zod; values must be decimal strings), `model.ts`,
`fiche.css` (tokens only), `__fixtures__/` (4 recorded bundles), `fiche.test.tsx`.

* Answer first: kind badge, community/anchor badges, ids with authority links (deduplicated), title, aliases,
  description/definition (with its source chip), actions (Citer `c`, Épingler `p`, Chemin vers… → Liens with
  `from=<qid>`, entity source chip); then the 3–6 headline Stats (V8SHELL `Stat`, `SourceChip`, `MiniLine`;
  projections dashed; Missing hatched with the reason; counts link to their section; big magnitudes « 3,37 T »).
* Sticky in-page tabs with counts (Faits, Relations, Séries, Carte & temps, Causes & effets, Prévisions, Manques,
  Sources), scroll-spy, `j`/`k` to move (ignored after the shell's `g` prefix and while typing), `c` copies a
  citation (name, ids, URL, date, sources with licences, anchor caveat), `p` pins through the Tableaux public API
  (`../tableaux/pin`, server boards or browser boards).
* Every related record links to its Fiche; series open Monde (`v8Links.series(table, {indicator, sel})`); forecast
  questions open their own Fiche and the family scoreboard (`v8Links.scoreboard`).
* Phone: 2-column stats, single-column map/time, tables scroll inside their box; no horizontal page scroll on any
  of the 10 sample pages at 390 px (measured `scrollWidth`).

## Sample pages (real ids in the dev DB)

| Kind | URL |
|---|---|
| country | `/v8/fiche/country/FRA` |
| commodity (anchor) | `/v8/fiche/commodity/crude_oil` (`/brent` redirects) |
| chemical (ChEBI) | `/v8/fiche/compound/chebi:27732` (caffeine) |
| gene (HGNC) | `/v8/fiche/gene/hgnc:76` (ABL1) |
| disease (Mondo) | `/v8/fiche/disease/mondo:0005148` (type 2 diabetes) |
| drug (ChEMBL) | `/v8/fiche/drug/chembl:CHEMBL941` (imatinib) |
| company (LEI) | `/v8/fiche/legal_entity/lei:549300LKT9PW7ZIBDF31` (Stellantis) |
| power plant (GPPD) | `/v8/fiche/facility/gppd:WRI1002735` (Gravelines) |
| conflict (UCDP) | `/v8/fiche/armed_conflict/ucdp:conflict:13243` (Russia–Ukraine) |
| species (GBIF) | `/v8/fiche/taxon/gbif:5219404` (Panthera leo) |

## Screenshots (scratch `…/scratchpad/v8/v8fiche/shots/`, 33 PNG, light + dark + phone for the 10 kinds)

Reviewed one by one; fixes made after review: static nav route shadowing the param route (blank skeleton), headline
grid gap cell, « Unclassified » kind badge, overflowing GDP number, English atlas units (« persons », « % change »),
Wikidata unit item ids (Q712226 → km²), duplicated ISO id chip, timeline flooded by UN roll-call votes (now ≤ 4 per
property), binary questions without probability (`prob_above`), long claim wording widening the phone page, definition
shown twice, odd last stat on phone.

* **France** (light): « Pays · communautaire, non vérifié » and 7 id chips (Wikidata, ISO alpha-3/alpha-2/numeric,
  GeoNames, OSM); six stats — 66,4 M personnes (2023, UN WPP), 3,37 T US$ (2025, WB), +0,8 % (2025, WB), 0,9 % (2025,
  IMF), 83,3 ans (2023, UN WPP), 7,4 % (2024, ILOSTAT) — each with a sparkline and source chip; 14 facts; relations
  (is a, part of, has part, context; « est situé(e) dans ≈ 3 292 » in hub mode); 27 series grouped by theme; France
  outline with the Wikidata point; dated statements; 4 effect estimates (+11 scoped to France); 53 pending forecast
  questions (all « pas encore jugée »); 5 missing items with reasons; 18 sources.
* **Crude oil**: anchor badge; Brent 126,6 $/baril (2026-10-14, EIA, « lu hors instantané atlas »), world production
  84 383 kb/j (2025), consumption 103 110 kb/j (2024), top producer USA 13 586 kb/j then RUS · SAU · CAN; top
  exporters hatched (« aucune année récente avec au moins 20 pays renseignés »); 23 attributed effect estimates
  (Caldara, Cavallo & Iacoviello 2016…); 125 forecast questions.
* **Imatinib** (dark): ChEMBL licence flagged « lic. » (CC BY-SA 3.0); max phase 4, first approval 2001, ATC
  L01EA01 (link), 4 mechanism targets and 20 indications « via IMATINIB MESYLATE ».
* **Russia–Ukraine conflict**: 94 741 deaths (2025, interval 87 321 – 140 889 from UCDP), start 2014-03-18,
  interstate, intensity 2, Crimea, 52 event locations; deaths and GED event counts as dated series.
* **Gravelines**: 5 460 MW, 31 471 GWh (2017, 3 dated values), fuel Nuclear, country France; commissioning year
  hatched (not published by GPPD for this plant).
* Phone shots: no horizontal scroll, sticky tabs scroll inside their bar, ids wrap.

## Performance (dev DB, HTTP through the API, 2026-10-18, quiet machine)

| | p50 | p95 |
|---|---|---|
| cached page, 10 kinds × 20 requests (warm) | **5 ms** | **11 ms** |
| uncached (`?fresh=true`), 10 kinds × 3 | 62 ms | 740 ms |
| uncached France | 561 ms (median) | |
| uncached crude oil | 371 ms (median) | |
| uncached other kinds | 45–79 ms (median) | |

First request of a process: the atlas query file open (shared with every atlas route, 40 s–4 min depending on load)
and the forecast-registry index (~20 s, background; the page renders without it and says so). Payload: France 305 KB,
crude oil 184 KB, other kinds 14–64 KB (no gzip middleware in the API — see requests). Dev-DB test
`test_cached_page_is_fast` asserts p95 < 300 ms for a cached page.

## Honesty choices

* Every number has a SourceChip (source, licence with NC / restricted flags, period, statement key or atlas series,
  raw artifact sha, selector, verification: community / source-reported / derived / outside snapshot / REVA
  grouping). The Sources section lists every source behind a value shown with its licences.
* Missing is typed and hatched with a reason, never 0: headline stat absent (« aucune source chargée ne publie cette
  valeur pour cette entité », or the source's declared exception), atlas headline concepts with the consulted
  series and their statuses, anchor series (table loaded after the snapshot / never loaded), expected facts
  (property never loaded vs published for other records only), documented gaps (IHME GBD skipped for its
  non-commercial agreement — `docs/dev/v3_a_healthedu.md`; GBIF occurrences and IUCN status not loaded).
* Relations: « Une relation est une déclaration de source, pas une cause. » Causal claims: attributed wording
  (« Selon … »), tier T1–T4 and grade A–E with the grading reasons; REVA asserts no causation. Effect estimates:
  attributed wording, interval, scope, « REVA n’établit pas cet effet ».
* Forecasts: official forecasts labelled « officiel (attribué) » (IMF WEO, EIA STEO, VIEWS), REVA candidates /
  ensemble / naive baselines labelled; a pending question reads « en attente de résolution (attendue le …) — pas
  encore jugée »; when resolved, « bat / ne bat pas <officiel> sur cette question (une question ne prouve rien) ».
  None of the questions shown is resolved yet (5 void in the registry).
* Anchor page: a REVA grouping, said so in the badge, the description and the API warnings.
* Counts on hubs are lower bounds (« ≥ ») or approximations (« ≈ »), said so.

## Tests

* `tests/unit/v8/test_v8_fiche.py` — 12 tests: profiles/aliases, French kind labels, id candidates, anchors,
  authority/value links, decimal-string literals, period labels, forecast kinds and judgement wording, binary/interval
  records, typed Missing in headlines, source summary, route models.
* `tests/integration/v8/test_v8_fiche_devdb.py` — 19 tests on the dev DB (read-only): `country/FRA`, `fra`, `FR`,
  `pays/fr`, `entity/Q142` → `country/FRA`; provider ids and anchors; unknown id not guessed; honesty invariants on
  France, imatinib, crude oil, ABL1, T2D, Gravelines, lion; cached p95 < 300 ms.
* `ui/src/v8/features/fiche/fiche.test.tsx` — 14 vitest tests on 4 recorded bundles: contract, parse + honesty,
  decimal-string strictness, France page (one call, 6 stats with sources, 8 sections, map, Monde links, pending not
  judged, hatched Missing, title), relation links + Liens link, canonical URL replacement, anchor labelling, conflict
  series and claim grades, keyboard j/k/c/p, unknown id, helpers.
* `uv run pytest -q tests/invariants` passes; ruff + mypy clean on my files; `tsc --noEmit` clean.

## What is missing and why

* Deaths/DALYs per disease: WHO GHE (`atlas.who.ghe`) is recorded but not pinned by the current atlas snapshot and
  no mapping links Mondo/MeSH diseases to GHE causes; IHME GBD is an owner decision (non-commercial agreement).
* Species: GBIF occurrences and IUCN status not loaded; the lion page is thin (rank, authority, parent).
* Gene interactions / drug–gene links: ChEMBL targets are ChEMBL target records, not HGNC genes; no mapping loaded.
* Brent: daily EIA table loaded after the atlas snapshot of 2026-10-17 01:52 — read directly (labelled) until the
  next `reva atlas snapshot`.
* Mini-map shows a country outline + point; no sub-national geometry (V8MONDE serves none either).
* « Chemin vers… » opens Liens with `from=` only (the user picks the target there).

## Requests to the lead

1. **Contract**: `contracts/openapi.json` regenerated (append-only for my routes) by `scripts/export_contracts.py`;
   `ui/src/api/openapi.gen.ts` (`npm run gen:types`) not regenerated (shared file).
2. **Dev DB schema contract**: after the restart, `reva api` refused to start (schema drift around
   `ops.v8_board`, V8API's migration). I ran my dev API with `REVA_SCHEMA_CONTRACT=warn`; V8API should re-check
   the committed contract against migration `0020_v8_boards`.
3. **Auth for screenshots**: my dev API used a per-run admin password (Argon2id hash in the process environment
   only, same pattern as `scripts/e2e_stack.py`) and a real `/v1/session` login. The read API token I had created
   earlier with `reva auth create-token` was revoked.
4. A GZip middleware would shrink the France bundle (305 KB) ~8×; `app.py` is shared — lead decision.
5. Nav: I removed my `navPath` — the shell registers a *static* route for `navPath` of param features, which shadows
   the param route at that exact URL (`/v8/fiche/country/FRA` rendered without params). V8SHELL may want to skip
   that extra route when the nav item is hidden.
6. No new dependency.
