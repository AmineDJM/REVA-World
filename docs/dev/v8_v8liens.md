# V8LIENS — « Liens » : how anything connects to anything

Agent V8LIENS (index 5), REVA World V8. Feature `liens` (route `/v8/liens`, order 40, `g l`).

## What was built

### Backend

| Endpoint | What it does |
|---|---|
| `GET /v1/v8/paths?from=&to=&families=&max_hops=&include_causal=&k=&hub_limit=&origin=` | k cheapest **diverse** paths (≤ 6 hops, default 4, k ≤ 10) between two nodes. `from`/`to`: KG id (`Q142`, `hgnc:1884`…), ISO3 (`FRA`, via the reviewed `country_qids.json`) or an effect node (`effvar:prices.consumer_level@FRA`). |
| `GET /v1/v8/neighbourhood?id=&families=&limit=&include_causal=` (alias `/v1/v8/paths/neighbourhood`) | ego graph: up to `limit` neighbours, round-robin over relation groups, most connected first, plus totals per group. |
| `GET /v1/v8/paths/nodes?q=` | picker search: typed id / ISO3, effect variables / atlas indicators / event types of the index (accent-insensitive), then `reva.kg.query.search`; each hit with its number of indexed links (`indexed=false` = cannot be a path end). |
| `GET /v1/v8/paths/status[?warm=true]` | index state (ready / building / stale / counts / timings); `warm=true` starts loading. |

Modules: `src/reva/api/routes/v8_paths.py`; `src/reva/v8/paths/{index,search,render,service}.py`.

**Graph (in-memory CSR index, numpy).** Built from the DB read-only:

* **statement edges** — every current `kg.statement` whose property is mapped to a V4 relation family
  (`relations_v4.yaml` + `relations_v4.d/*`, 192 mappings): 953,615 edges, 563,794 nodes. Read per small group of
  properties (index scans) and made "current" in Python exactly as `kg.statement_current` defines it (newest row of
  its `(statement_key, origin)`, not retracted) — the view's anti-join over 5 M rows is too slow (64 s). Each
  statement is traversable both ways and read in path order through its family (`fait partie de` one way,
  `comprend` the other). `hubs.yaml` `excluded_properties` (P31, P166…) are traversed only when their family is
  asked for explicitly.
* **causal-claim edges** (distinct type) — 51,598 current claims with stance `asserts`, tier and current grade;
  `chain_excluded` relations (coded outcomes…), chain-quarantined claims and self-loops left out, as in
  `/v1/causal/chain`.
* **effect-estimate edges** (distinct type) — all 301 current estimates, cause → effect between `effvar:<variable>`,
  `atlas:<concept>` (same node id as the KG `measured_by` objects), `evtype:<type>` or KG events. An effect estimated
  for exactly one country is its own node `<effect>@ISO3` ("Niveau des prix à la consommation — France").
* **declared bridges** (identifier links only, never names): estimate scope `<effect>@ISO3` ↔ the country's QID
  (116), variable → atlas concept from `registry/effects/variables.yaml` and event type → verified Wikidata class from
  `registry/events/taxonomy.yaml` (9).

**Search.** Hop-limited cheapest-cost relaxation from both ends (⌈H/2⌉ / ⌊H/2⌋ levels, vectorised), every node
reached from both sides is a meeting point (exact for the cheapest path ≤ H hops), candidates in cost order,
non-simple ones dropped, and a candidate identical to a kept path but for one intermediate node (or two adjacent ones)
is counted as a **variant** ("+ 32 variantes : autres mines de cobalt du Congo") instead of filling the list.
Costs: statement = the family's `path_cost` (1 for informative families, 2 associated_with / measured_by, 3 is_a /
context); causal claim by grade A 0.35, B 0.5, C 0.75, D 1.1, E/ungraded 1.6 (so with `include_causal` well-graded
claim chains rank first); estimate 0.6 (+0.25 without reported uncertainty); walking a claim/estimate against
cause → effect +1.0; scope 0.9; bridge 1.2; plus the penalty of each intermediate node = declared hubs.yaml penalty +
0.5 × per-doubling of degree above 64 (half the hubs.yaml rate: a country ≈ 3 hop units, "Homo sapiens" ≈ 5).
Blocked hubs (Earth, human, country…) are never intermediate; nodes with > `hub_limit` (2,000) links are reached but
not expanded, and listed. Entry cap per level (3 M, cheapest frontier first) and a 5 s budget are reported as
truncation, never as absence.

**Each hop returns**: kind (`statement` / `causal_claim` / `effect_estimate` / `estimate_scope` / `declared_bridge`),
direction vs the record, family read in path order + the source's own property, relation label `{fr, en}`, the
sentence "A — relation → B", source (title, statement id, revision, citation), **licence** (raw-artifact rights via
`family_query.rights_for`, claim source rights via `causal.graph`, estimate rights; `null` = unknown, shown
hatched "licence inconnue", never as open), **evidence strength** (verification for statements; grade + tier +
rule reasons for claims; method + uncertainty for estimates), dates, alternatives (parallel edges), and details
(qualifiers/references, attributed wording, published value + shock). Paths carry kind (documented / mixed /
causal), weakest grade (chain rule), restricted and unknown licences, variants.

**Index lifecycle.** First request: the on-disk cache `var/v8/paths/index-<data key>.npz` (71 MB, loaded in
0.4 s) when its key (max row ids, retraction counts, registry signature) matches; else a background build on a
dedicated connection (20 s on an idle machine, 60–230 s under today's load avg 15–22) while the request returns
`503 paths_index_building` after 25 s (the UI says so and retries). Every 5 min the key is re-read; a change
rebuilds in the background and the old index keeps serving. Renderer registries (effect labels need the atlas
catalog: ~10 s cold) are warmed in the background. CLI prebuild: `uv run python -m reva.v8.paths.service build`.

Reused, not forked: `reva.kg.query` (statement output, labels, kinds, property labels, search, ISO3 registry),
`reva.kg.family_query` (registry, rights — incl. its private `_rights`), `reva.causal.graph` (`_claims`/`_render`
claim output with grade, tier, attribution, source rights), `reva.effects.query.estimate_json`,
`reva.effects.variables.default_resolver`, `reva.links.taxonomy.load_hubs`.

### UI — `ui/src/v8/features/liens/`

`index.tsx` (feature + palette command "liens Q851 FRA" → path URL), `LiensPage.tsx`, `NodePicker.tsx`,
`parts.tsx` (path cards, hop rows, node card, ego list), `PathGraph.tsx` (cytoscape, lazy), `model.ts`, `api.ts`
(zod, tolerant but strict on sentence/kind/licence), `examples.ts`, `liens.css`, `liens.test.tsx`.

* Two pickers (De / À) searching `/v1/v8/paths/nodes` merged with `/v1/v8/search` (V8API) when it answers (silently
  ignored when absent); each hit shows kind, id and its number of indexed links; ids can be typed. ⇄ swaps.
* Family chips from `/v1/kg/families` (French labels; "toutes les familles" default; one scrollable row on phone),
  toggle **« inclure les affirmations causales attribuées »**, max links 2–6.
* Honesty line always visible: « Un chemin est un ensemble de liens documentés, pas une preuve causale. Les maillons
  causaux sont des affirmations de leurs sources (palier T1–T4, note A–E) ; les estimations sont celles de leurs
  auteurs. » (+ the server's full note).
* **Answer first**: one sentence (how many paths, shortest, via whom, how many go through attributed claims), then
  French warnings (Wikidata community, restrictive licences), then path cards: one readable sentence per hop with a
  coloured rail (solid = documented, purple = Wikidata, dashed amber = causal claim, dashed teal = estimate, dotted =
  declared), a tag "affirmation causale · note B · T1" / "déclaré par la source" / "Wikidata · non vérifié", the
  shell `SourceChip` (source + licence + statement id + property + family on hover/tap), "licence inconnue" hatched,
  dates, "+N autres liens entre ces deux nœuds", expandable attributed wording / published value, "maillon le plus
  faible : B", variants. Same-named nodes (a protein and the drug target built on it) show their ids.
* Graph (cytoscape): `from` left, `to` right, one row per path, selected path highlighted, others dimmed, legend;
  tap a node → node card (kind, id, links) with **Développer** (adds its neighbourhood around it), **Partir d'ici**,
  **Aller ici**, **Fiche →** (`v8Links.entity`, countries by ISO3).
* Only one end chosen → exploration mode: its ego graph (groups with French labels).
* Footer: links examined, nodes reached, search ms, index counts and build time, hubs not developed, truncation.
* **URL = state**: `?from=&to=&fam=&causal=1&hops=&p=<path>&n=<node>` (defaults omitted; `v8Links.path` compatible;
  discrete changes push history). Keyboard: `/` focus, `j`/`k` paths, `x` swap, `c` causal, `e` expand node.
* Empty page: 7 real example queries (below). States: skeletons with fixed height, index-building notice with
  auto-retry, `ErrorState` for others, empty result explains why and offers "inclure les affirmations causales" /
  "autoriser N+1 liens".

## Five real examples (dev DB, 2026-10-18) — screenshots

Screenshots in the scratchpad `…/scratchpad/v8/V8LIENS/` (1440 px light unless noted; 0 px horizontal overflow and no
console errors on all of them).

1. **gène → maladie → médicament** — `hgnc:1884` CFTR → `chembl:CHEMBL2010601` ivacaftor, causal on
   (`01_gene_disease_drug.png`; dark: `08_dark.png`): #1 « CFTR — a causé → cystic fibrosis » (affirmation causale,
   Mondo Disease Ontology, **note B · T1**) then « cystic fibrosis — est associé à → IVACAFTOR » (ChEMBL indication,
   CC BY-SA 3.0, flagged restrictive). #2 the mechanistic route: CFTR — produit → CFTR protein (HGNC) — fait partie
   de → CFTR drug target (ChEMBL; ids shown, same name) — interagit avec → IVACAFTOR. #3 via LUMACAFTOR (+1 variant
   TEZACAFTOR). Without the toggle, only the documented routes.
2. **conflit → pays → matière première** — `ucdp:conflict:265` « DR Congo (Zaire): Katanga » → cobalt
   (`02_conflict_country_commodity.png`): conflict — est situé dans → RD Congo (UCDP, dated 1961–1962) — contient →
   KOV Mine — produit → Cobalt (USGS Minerals Yearbook, dated 2024) ; « + 32 variantes (KTO, Mashamba East, Mutanda,
   Tenke Fungurume…) ». Graph: a clean 4-node line.
3. **espèce → cible → médicament → maladie** — `ncbitaxon:5833` Plasmodium falciparum → `efo:0001068` malaria
   (`03_species_drug_disease.png`): P. falciparum — (target organism) — Ferriprotoporphyrin IX target — interagit
   avec → hydroxychloroquine sulfate — est associé à → malaria (ChEMBL, CC BY-SA 3.0), « + 6 variantes » (mefloquine, chloroquine, artemether, lumefantrine…); #2 via another drug, + 3 variants (amodiaquine, quinine…).
4. **entreprise → pays → centrale** — `lei:529900S21EQ1BO4ESM68` TotalEnergies SE → nuclear fuel
   (`04_company_country_plant.png`): TotalEnergies SE — est situé dans → France (GLEIF) — contient → BELLEVILLE —
   (primary fuel) → Nuclear (Global Power Plant Database).
5. **énergie → pays → économie** — `gppd:fuel:nuclear` → `effvar:prices.consumer_level@FRA`, causal on
   (`05_energy_country_economy.png`): Nuclear — BELLEVILLE — France — est le périmètre de l'estimation → Niveau des
   prix à la consommation — France (sources: ESCB Task Force 2010, Ortega & Osbat 2020; « ECB publication; no licence
   statement captured… all rights reserved » shown as restrictive).

Plus the brief's own question, **« comment l'Arabie saoudite est-elle liée à l'inflation française ? »** —
`Q851` → `effvar:prices.consumer_level@FRA`, causal, 5 hops (`06_saudi_french_cpi.png`, phone `07_phone.png`):
#1 Arabie saoudite — contient → « La fauconnerie, un patrimoine humain vivant » (UNESCO ICH, CC BY-SA 4.0) — est
situé dans → France — est le périmètre de l'estimation → prix à la consommation — France; #2 the same via Slovakia,
then « prix à la consommation — Slovaquie — subit un effet estimé de (0,63 % pour un choc de 10 %) → Prix de
l'énergie — a un effet estimé sur (0,34 %…) → prix à la consommation — France » (+9 variants: other euro-area
countries). Honest, and visibly a set of documented links rather than an explanation. Also `09_ego.png`
(exploration of one node) and `00_empty.png` (examples).

**Not possible with the loaded data (said, not faked):**
* *species → ecoregion → country*: the RESOLVE ecoregions are an island (868 nodes: ecoregion → biome/realm only);
  no taxon → ecoregion or ecoregion → country statement is loaded (no Wikidata P183/P9714, no GBIF occurrences).
  Replaced by example 3.
* *chemistry → energy → economy*: ChEBI/PubChem compounds and elements are not linked to USGS commodities or GPPD
  fuels (shortest unweighted connection methane → GPPD "Gas" is 10 hops through unrelated items). Example 5 starts at
  the energy layer instead.
* *imatinib → leukemia* works without claims (ChEMBL: imatinib — imatinib mesylate — CML; 4 paths).
* *drought in Brazil → coffee price*: no coffee price variable/estimate and no drought–coffee claim is loaded.

## Performance (measured)

Dev DB, uvicorn single process on the shared 4-core container at **load average 15–22** (other agents' builds),
HTTP end-to-end incl. rendering (DB reads for labels, provenance, rights), `max_hops=4`, 12 real pairs × 5 rounds
after one warm-up round (`scratchpad/v8/V8LIENS/perf.py`):

* `/v1/v8/paths` total **p50 83 ms, p95 232 ms**, max 371 ms (target p95 < 500 ms ✔); search alone p50 15 ms,
  p95 75 ms (up to 136,741 CSR entries scanned for USA → Russia).
* `/v1/v8/neighbourhood` (France, 30) warm 25–40 ms (first call ~0.3–0.9 s cold).
* Index: 953,615 statements + 51,598 claims + 301 estimates, 563,794 nodes; numpy build 3.5–13 s, DB read 15 s idle /
  225 s under load; cache load 0.4 s; the whole API process (all routers, index loaded) measured at 825 MB RSS; a standalone build peaks at ~710 MB.
* `/v1/v8/paths/nodes` ~1 s (dominated by the KG substring label search); the UI merges `/v1/v8/search` results
  when V8API's index answers.

## Tests

* `tests/unit/v8/test_v8_paths.py` — 14 tests, synthetic graph, no DB: family readings in path order (part_of vs
  has_part), excluded properties opt-in, blocked hubs never intermediate, hub limit reported, claim edges only with
  `include_causal` and graded costs/direction, estimate scope → country, declared bridges, variants, simple/bounded
  paths, origin filter, ego round-robin, cache save/load + old cache removal, index holder.
* `tests/integration/v8/test_v8_paths_devdb.py` — 3 tests on the dev DB (read-only transaction): partial real index
  (ChEMBL/HGNC/UniProt/MONDO + all claims/estimates); CFTR → ivacaftor ranks the graded claim chain first, rendered
  hops carry grade/tier/source/family/licence; routes (paths, 404/422 cases, neighbourhood, nodes, status).
* `ui/src/v8/features/liens/liens.test.tsx` — 12 vitest tests: URL round-trip, answer sentence, strength/licence
  wording (unknown never open), French warnings, layout model; page: examples + honesty, chains with sources/grades/
  licences/variants/footer, URL state (causal toggle, family chip, `j`, node card, Fiche link, Développer), ego mode,
  503 building state, empty result actions; feature contract + palette command.
* `npx tsc --noEmit`: 0 errors (whole UI at the time of the run). ruff + mypy clean on my files;
  `pytest tests/invariants`: 191 passed, 1 skipped.

## Honesty choices

* No relation family is causal; causal claims are a separate edge type, always worded "selon <source>" with tier and
  grade, and the path's weakest grade is shown (chain rule). Estimates are labelled "estimation publiée", with method,
  uncertainty status, value and shock as printed; never drawn as observations. Walking a claim backwards is allowed
  (a documented link) but costs more and is worded as the inverse relation.
* A path is never called an explanation: the honesty line is permanent, the API returns `path_not_causal` and
  `claims_not_causation` warnings and the note in `data.note`.
* Licence per hop; unknown licence hatched « licence inconnue »; restrictive ones flagged (`lic.`/`NC` on the chip,
  "licence restrictive" on the path, list under the answer). Wikidata hops flagged community-edited.
* Truncation and undeveloped hubs are always listed; "no path" says it is bounded by the limits and filters.
* Same-name records are never merged (a protein and its ChEMBL target are two nodes, ids shown).
* Bridges are identifier links declared in registries only (country_qids.json, variables.yaml, events taxonomy).
* Example queries on the empty page are queries, not data.

## Missing / known limits

* Fiche links for virtual nodes (effect variables, `@ISO3` nodes) are absent (no Fiche kind for them yet).
* Links store (`kg.link_row`, derived country↔country links) is not traversed: the brief asked for relation families
  + claims + estimates; adding it would be an extra edge type.
* The first build after a data change is slow under load (60–230 s DB read); the old index keeps serving.
* The variant rule (same hop types, one or two adjacent nodes differ) can hide a genuinely different route of the
  same shape behind "+N variantes"; the examples list is shown.

## Requests to the lead

1. **`contracts/openapi.json` is stale for every `/v1/v8/*` route** (45 paths of all V8 agents incl. mine:
   `/v1/v8/paths`, `/paths/nodes`, `/paths/status`, `/neighbourhood`); `tests/integration/test_api_contract.py::
   test_committed_contract_is_current` fails. I did not regenerate it to avoid racing the other agents — please run
   `uv run python scripts/export_contracts.py` once after the wave.
2. The dev API refuses to start (`assert_schema_contract`: `ops.v8_board*` objects differ from the committed contract —
   V8API's migration). I ran my API with `REVA_SCHEMA_CONTRACT=warn`.
3. Data bridges that would make the missing cross-domain examples possible: ChEBI/element ↔ USGS commodity ↔ GPPD fuel
   `same_as`/`corresponds_to` (V4LINK), taxon range / endemic statements (P183, P9714) and an ecoregion ↔ country
   overlay; a curated `effvar` ↔ commodity link (e.g. `oil_price.global` ↔ crude petroleum) once the petroleum item is
   loaded.
4. No dependency requested; no contract change requested (`v8Links.path(from, to)` is used as is, extra state
   parameters are additive).
