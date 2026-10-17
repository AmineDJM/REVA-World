# V3 APIFIX — backend fixes for the API issues UICHECK found

Agent APIFIX, 2026-10-17. Input: `docs/dev/v3_uicheck.md` §4 (requests and responses on the dev DB `reva`, origin
live), with `docs/dev/v3_causfix.md`, `docs/dev/v3_sameas.md`, `docs/dev/v3_causal_interface.md`. Dataset connectors
and the causal loader's data side (AUDFIX) were not touched. No migration, no claim or grade written, no grading
rule changed. **REVA still asserts no causation**: every change below either says more exactly what a source codes
or stops something from looking like what it is not.

## 1. `GET /v1/events/{id}` → 500 for ICB and NAVCO records

**Root cause.** The taxonomy's *marker* dataset kinds are declared for any source (`source_key: null` in
`registry/events/taxonomy.yaml`: CAUSDATA `causdata:event_type`, A-CONTENTION `contention:event_type`, A-ECRISES,
A-COLONIAL, CAUSMORE — one marker written by several connectors). `reva.events.view._dataset_kind` returned the
taxonomy entry as is, so the card carried `dataset_kind.source_key = None` and the route's `EventCard` model (`str`)
refused it: every marker-typed record (ICB, MID, UCDP episodes, NAVCO, MEC, Mass Mobilization, crisis
chronologies, colonial records, PA-X, COW war outcomes, IDMC) answered 500.

**Fix** (`src/reva/events/view.py`). The marker lookup now reads the `source_key` of the marker statement that
recognised the record (deterministic: first by key) and returns the kind with that source; a kind declared for one
source is unchanged. The card names the source that typed the record (`icb:crisis:196` → `icb.data`,
`navco21:408` → `harvard.dataverse.navco`). Contract unchanged (`source_key` stays a string).

**Test** `tests/integration/test_events_card_devdb_sweep.py` (marker `db`): reads the configured database as
`reva_api` in a read-only transaction (`default_transaction_read_only = on`, checked) and builds the card of one
record of every kind present — every (entity source, entity kind) of `kg.entity_current`, every (claim source,
relation) of `kg.causal_claim_current` at both ends, every taxonomy dataset kind (one per id prefix, one per (marker
source, coded value)), the ids UICHECK used and `Q8698` — through `event_card` and the route's own response models.
Dev DB: **189 ids, 135 cards, 54 non-events (404), 0 failures, 4.6 s**. With the fix reverted the same test fails
on `icb:crisis:196` and the other marker records. It skips on a database without live causal claims (CI).
HTTP check on the dev DB after the fix: `/v1/events/icb:crisis:196` 200 (0.15 s), `/v1/events/navco21:408` 200.

## 2. Duplicated chain paths

**Cause.** In `reva.causal.graph.chain`, when the two search fronts meet on *both* records of one event (TIES case
and EUSANCT case, joined by `same_as`), each meeting node enumerates the same identity hop + claim: `ties:case:1990122101
→ eusanct:policy_result:1990122101` returned `paths_found: 2`, identical.

**Fix.** Built paths are deduplicated by their sequence of hops — claim hop = its claim keys, identity hop = its two
records and `same_as` statement keys — and the merge is counted in `search.duplicate_paths_merged` (new field,
`ChainSearch`). Dev DB: `paths_found` 2 → **1**, `duplicate_paths_merged: 1`; Sarajevo → WWI unchanged (1 path, 0
merged). Test: `tests/integration/test_causal_sameas.py::test_chain_through_identity_is_returned_once` (the TIES/EUSANCT
shape, synthetic ids; asserts the duplicate existed and was merged). The UI still merges client side and now adds
the server's count (`ui/src/v3/causal/api.ts`), wording "trouvé(s) en double par la recherche".

## 3. Coded outcomes counted and filed as causes

`registry/causal/relations.yaml`: new lane **`outcome`** ("coded outcomes (how the event ended according to a
dataset; not causes)"), `coded_outcome` moved to it (lane `coded` is now "dataset-coded triggers"); the loader
(`reva.causal.vocab`) refuses a registry where an `outcome`-lane relation is not in `chain_excluded`
(`Vocabulary.outcome_relations ⊆ chain_excluded`). Storage family stays `cause` (CAUSFIX L6 decision; no
migration); claim JSON `lane` is `outcome`.

* `/why`: outcome claims are in lane `outcome`, never in a causal lane; new `causal_claims` / `outcome_claims`
  counts; the summary counts only causal claims and adds "k issue(s) codée(s) … une issue n'est pas une cause".
  `icb:crisis:196:outcome/why`: lanes `[outcome: 1]`, `causal_claims 0`, summary "0 affirmation(s) causale(s) … 1
  issue(s) codée(s)" (was lane `coded`, "1 affirmation(s) causale(s)").
* `/v1/causal/events/{id}`: outcome claims in their own bucket `outcome` (same shape, family → grade → count).
  `icb:crisis:196`: causes `{cause: {B: 1}}` (the coded trigger), effects `{outcome: {B: 1}}` (was `{cause: …}`);
  `cow:war:139`: effects `{outcome: {B: 29}}`.
* Chain not-found reason when `from` or `to` is the outcome end of a coded-outcome claim: "icb:crisis:196:outcome is
  a coded outcome record (how an event ended according to a dataset): outcomes are never composed into causal
  chains (registry/causal/relations.yaml chain_excluded); no causal chain within …".
* UI in sync: `nCauses` / `nEffects` exclude the `outcome` bucket, `nOutcomes` shown in the event counts; the new
  reason is translated ("… : issue codée … — une issue n'est jamais composée dans une chaîne causale").

Tests: `tests/integration/test_causal_sameas.py::test_outcomes_have_their_own_lane_count_and_chain_reason`,
`tests/unit/causal/test_causfix.py::test_outcome_lane_must_be_chain_excluded`, `ui/src/v3/causal/apifix.test.ts` (3).
`contracts/openapi.json` regenerated (`CausalWhy.causal_claims/outcome_claims`, `ChainSearch.duplicate_paths_merged`,
descriptions). `ui/src/api/openapi.gen.ts` was **not** regenerated: it is far behind the contract already (no
`quarantined_skipped` etc.; a 12k-line diff of other agents' shapes) and no UI code reads these types.

## 4. NLQA (`src/reva/nlqa/**`)

* **Attribution in evidence.** Causal evidence items now carry `supported_by` (`[{id, label}]`, the parties
  Wikidata records as making the claim, P3680) and `attribution` (`supported_by`, `nature_of_statement` (P5102),
  `note`, `source_asserts_it_itself: false`), `null` when the claim carries none — as the sentence does ("qui
  attribue cette affirmation à … (Wikidata ne l'affirme pas lui-même)"). Dev DB, "Pourquoi la guerre
  russo-ukrainienne ?": evidence E4/E8 `supported_by: [Q7747, Q1140115]` (labelled by QID: no KG label, §6).
* **Temporal reason.** Evidence items carry `temporal` (`check`, `reason`, `entity_dated_by_inception`,
  `undated`) from the grade's temporal detail. `undetermined` is worded by its reason: entity dated by inception →
  "l'effet : entité datée par sa création (P571), pas un événement ; sa période d'existence est comparée,
  antériorité non établie" (WWI → German/Russian/Ottoman Empire, Austria-Hungary; Alsace-Lorraine → WWI);
  undated side → "pas de date de début dans le graphe pour la cause, antériorité non vérifiable"; no reason
  recorded → "antériorité non vérifiable : date absente ou hors de la plage prise en charge". "dates manquantes"
  is gone.
* **Coded outcomes** in the `why` answer: counted apart ("… ; s'y ajoutent k issue(s) codée(s) … qui ne sont pas
  des causes"), listed last (lane `outcome`).
* **Keyword planner / entity lookup.** Three causes, three fixes, none a name → id map:
  1. "Pourquoi la guerre russo-ukrainienne ?" was not planned: a "pourquoi …" phrase had to contain a capital or a
     year. It now also qualifies with an *event noun* plus a qualifier (closed list of event kinds, fr+en: guerre,
     crise, révolution, dissolution, chute, dépression, war, collapse … ; a lone "war" still names nothing).
  2. "Pourquoi la dissolution de l'URSS ?" was not planned: a capital after an elided article ("l'URSS") did not
     count. It does now.
  3. Lookup: no fr/en label or alias *contains* "dissolution de l'URSS" (the item is "dislocation de l'URSS" fr,
     "dissolution of the Soviet Union" en) and "l URSS" without its apostrophe matched nothing. When the KG search
     (exact / prefix / substring) finds nothing, the KG adapter now looks in the same label index
     (`kg.entity_label`, trigram) for entities whose fr/en labels and aliases hold **every content word** (whole
     words, function words dropped, 2–6 words); match kind `label_words`, worded "tous les mots de la question
     figurent dans ses libellés ou alias fr/en", equally good candidates listed as alternatives.

  Dev DB results: guerre russo-ukrainienne → Q15860072 (exact label), 10 sourced claims; "dissolution de l'URSS"
  and "dislocation de l URSS" → Q5167679 (`label_words`), 18 claims; "Grande Dépression" → **Q696482** "Grande
  Dépression aux États-Unis" (label prefix) because Q8698 is still not a KG entity (§6) — the answer names the item
  it used, so the substitution is visible; it becomes the exact match once Q8698 is imported.
* **The 26 s first `/v1/ask`.** Profiled: 20.3 s of the 26.5 s `build_backends` was `reva.atlas.query._verify_db`
  hashing the latest atlas snapshot's DuckDB query file (`/tmp/reva-atlas-cache-0/atlas-717e…-v6.duckdb`,
  **6,877 MB**) for its ATLAS-1 integrity check, the rest the atlas catalog load. It ran when the backends were
  built, so *every* question paid it once per process — causal and KG questions too, which never read the atlas.
  The verification is kept as is. Fixes: (a) `LazyAtlasAdapter` opens the atlas on first atlas operation (failure
  = `capability_unavailable`, remembered 300 s); (b) the API starts a background warm-up at startup
  (`reva.api.routes.ask.start_warm_up`, `REVA_NLQA_WARMUP`, default on except `REVA_ENV=test`) that builds the
  backends and opens the atlas. Measured on the dev DB (`reva api`, temporary read token, revoked):

  | | before | after, warm-up off | after, warm-up on |
  |---|---:|---:|---:|
  | build backends | 26.5 s | 0.19 s | in background (done < 38 s after start) |
  | first causal `/v1/ask` (HTTP) | ~26 s | 1.9 s | 0.47 s |
  | first atlas `/v1/ask` | (paid above) | 25.2 s | 1.6 s |
  | next atlas `/v1/ask` | ~1 s | 1.2 s | 1.4 s |

  The cost is avoidable only by not hashing 6.9 GB on the request path; a new atlas snapshot still costs its
  build/verification once (by design, SEC-3/ATLAS-1). `tests/integration/test_nlqa_api.py` updated (the missing
  snapshot is now reported when the lazy atlas is opened, not at build).

Tests: `tests/unit/nlqa/test_nlqa_causal.py` (+9: attribution/temporal evidence, undated side, outcomes, planner
cases), `tests/unit/nlqa/test_nlqa_adapters.py` (+2: lazy atlas, warm-up), `tests/integration/test_nlqa_entity_words.py`
(2, synthetic items).

## 5. `test_ui_fixture_is_the_current_pipeline_output`

Regenerated with the repo's generator (`REVA_REGEN_UI_FIXTURE=1 uv run pytest tests/unit/nlqa/test_nlqa_causal.py -k
ui_fixture`). Deep diff of `ui/src/v3/causal/ask_causal.fixture.json` before regeneration, all expected:
`catalog_digest` ×2 (`93744e1d…` → `075790e6…`: atlas concepts added by other agents); `evidence[].supported_by`,
`.attribution`, `.temporal` added on the 8 causal evidence items (§4); one `why` sentence and `answer_text`: the
undetermined wording ("dates manquantes, antériorité non vérifiable" → "antériorité non vérifiable : date absente ou
hors de la plage prise en charge"; the fixture claim has no temporal reason). Nothing else changed; the UI suite passes
on it.

## 6. KG gaps (dev DB, origin live, read-only counts)

| what | distinct | no KG entity | entity without en/fr label |
|---|---:|---:|---:|
| claim endpoints, provider keys | 92,061 | 0 | 0 |
| claim endpoints, Wikidata QIDs | 1,665 | **659** | 19 |
| P3680 *supported by* parties | 9 | 8 | 0 |
| P5102 *nature of statement* values | 7 | 7 | 0 |
| places (P276/P17/P30/P131) of QID endpoints | 706 | 468 | — |

1,141 distinct QIDs to fetch. `Q8698` (Great Depression) is one of the 659; **`Q5167679` is now a KG entity** (fetched
by the harvester and imported after UICHECK's check), so the UICHECK note is outdated for it; `Q7747`, `Q1140115`,
`Q20820099`, `Q55643`, `Q7204` have none.

**Harvester not run: no budget today.** `python -m reva.causal_harvest status` (before): fetched 1,406, frontier
3,110 (tiers 4/5/6), 377 of the 659 endpoints queued (360 at tier 5; Q8698 tier 6, rank 116), 282 endpoints, all 15
qualifier items and 466 places not queued at all. Today's CAUSWD share of `wikidata.wbapi` (computed exactly as
`causal_session` does, offline, 03:33 UTC): day allowance 1,196, CAUSWD used today 1,196 → **cap 0** (the shared
ledger books 1,358 wbapi requests today, 1,247 archived Wikibase-host responses). A session would refuse with
`no wbapi budget left for CAUSWD today`; the share rule was not bypassed, nothing was fetched, so neither
`reva kg import-harvest --from-db reva_kg_causwd` nor `reva causal refresh --origin live` was run (they would import
nothing new). No Wikimedia request was sent by this agent.

What was done instead: the 1,141 QIDs were **queued** in the harvester's own state (`var/kg/harvest_state/causal.causwd.json`,
`State.add`, backup kept outside the repo): 659 claim endpoints and 15 qualifier items at tier 1 (`TIER_KG_CAUSAL`;
282 new, 377 moved up from tiers 4–6), 467 places at tier 2 (`TIER_KG_EVENTS`), reason
`apifix:<group>_without_kg_entity`. The next CAUSWD session fetches them before anything else; at the usual ~1.2k
daily share they fit in one day even fetched one by one (Special:EntityData), then `flock /tmp/reva-devdb.lock uv run
reva kg import-harvest --from-db reva_kg_causwd` and `flock /tmp/reva-devdb.lock uv run reva causal refresh --origin
live` bring them into `reva`.

## 7. Checks run

* `ruff check`, `ruff format --check` on every touched Python file: clean. `uv run mypy` (project config,
  `reva.kernel`): clean. `mypy` on the touched modules: no error in APIFIX code; pre-existing errors remain in
  `reva/api/routes/causal.py:586` (Envelope fields), `reva/nlqa/adapters.py` (links adapters), `compose.py`,
  `executor.py`, `ports.py` — untouched lines.
* pytest: `tests/unit/nlqa`, `tests/unit/causal`, integration `test_nlqa_api`, `test_nlqa_entity_words`,
  `test_causal_api`, `test_causal_store`, `test_causal_sameas`, `test_causal_wikidata_demo`, `test_causal_ops`,
  `test_causfix_store`, `test_events_dataset_kinds`, `test_events_datasets`, `test_events_card_devdb_sweep`,
  `test_api_contract`: **305 passed**.
* UI: `npx vitest run` 32 files, 243 passed, 1 skipped; `npm run typecheck` clean.
* The API servers started for the measurements were stopped; the temporary token (`service:apifix`) was revoked.

## 8. Files

`src/reva/events/view.py`, `src/reva/causal/{graph,vocab}.py`, `registry/causal/relations.yaml`,
`src/reva/api/routes/{causal,ask}.py`, `src/reva/api/app.py`, `src/reva/nlqa/{adapters,causal_adapter,compose,executor,fallback,ports}.py`,
`contracts/openapi.json`, `ui/src/v3/causal/{api.ts,parts.tsx,ChainFinder.tsx,apifix.test.ts,ask_causal.fixture.json}`,
`docs/dev/v3_causal_interface.md` (lanes, counts, dedupe), tests listed above.
