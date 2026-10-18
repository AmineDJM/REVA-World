# V8ASK — « Demander »: cited answers, answer-first page, Brainstormer

Agent V8ASK, 2026-10-17/18. Feature `demander` (route `/v8/demander`, order 15, `g d`), API `/v1/v8/ask/*`,
logic in `src/reva/v8/ask/`, NLQA extensions in `src/reva/nlqa/bridges.py` + `registered_tables.py` (+ small hooks).
No LLM, no network, no DB write (the V8 endpoint writes no audit row and no ledger row).

## 1. Coverage of the 47 golden questions (live dev DB, read-only)

Measured through the real endpoint `POST /v1/v8/ask/answer` (keyword planner, same pipeline as `/v1/ask`), atlas
snapshot `717e8452…`. Classifier `reva.v8.ask.golden.classify`: **answered** = ≥ 1 substantive sentence citing a
sourced record; **partial** = only sourced *neighbouring* evidence labelled non-equivalent; **unknown** = « REVA ne
sait pas » + what is missing.

| | before (NLQA6 code, 2026-10-17) | after (V8ASK) |
|---|---|---|
| answered with sources | **21 / 47** | **33 / 47** |
| partial (neighbouring estimates only) | 4 | 4 |
| REVA ne sait pas (+ cause) | 22 | 10 |

The ≥ 35 target is **not** reached: the 14 remaining questions have no honest answer in the loaded data (details
below); I did not add a translation table or relax the exact-match rule to reach it.

| Question | before | after | how / what is missing |
|---|---|---|---|
| De quoi est fait le méthane ? | unknown | **answered** | bridge `label_folded`: « méthane » = ChEBI "methane" up to accents (not a translation) |
| What is methane made of? | answered | answered | |
| De quoi est fait le pétrole ? / Composition du pétrole ? | unknown | unknown | no record labelled « pétrole »/"petroleum" in the KG (only approximate names) |
| De quoi est composé le cœur ? / Quelles sont les parties du cœur ? | unknown | **answered** ×2 | label bridge: Wikidata Q1072 (fr « cœur », en "heart") → UBERON `uberon:0000948` "heart" (27 part/has-part relations) |
| Quels gènes sur le chromosome 17 ? | unknown | **answered** | bridge `label_qualified`: "chromosome 17 (Human)" / "(Mouse)", one kind → 2,120 HGNC genes on the human record |
| De quoi est fait le granite ? | unknown | unknown | "Granite" is only a power plant in the KG (no rock record) |
| Composition chimique de l'eau ? | unknown | **answered** | Wikidata Q283 « eau » loaded meanwhile by WDOPS (has part H, O); V8ASK also drops the false "EAU" airport-code alias match |
| Que mange le loup ? / Qui mange le lapin ? / Que mange le tigre ? | answered | answered | |
| What does the wolf eat? | unknown | unknown | no record labelled "wolf" (Q18498 is "grey wolf"); head-noun matching refused |
| Que mange le lapi ? | unknown | unknown | typo: approximate names only (by design) |
| Où vit le lion ? / Where does the lion live? / Habitat du loup ? | unknown | unknown | no `located_in` / range relation loaded for any lion/wolf record (IUCN ranges, Wikidata P183/P9714 not loaded) |
| Combien d'espèces de mammifères ? | unknown | **answered** | catalog-name bridge: the atlas catalog labels GBIF class 359 « mammifères (Mammalia) » → COL `col:6224G` publishes 6,464 / 6,365 species (shown as published text) |
| How many species of mammals? | unknown | unknown | no exact "mammals" label; the en catalog label has no parenthesised name |
| Qui possède / Who owns the KOV mine? | answered | answered | |
| Que possède Glencore ? | unknown | **answered** | bridge `label_legal_form`: "GLENCORE PLC" / "GLENCORE AG" → GLEIF direct/ultimate parent of GLENCORE AG |
| Qui produit du cuivre ? | unknown | **answered** | label bridge Q753 (« cuivre » / "copper") → USGS MYB commodity "Copper": 719 producing facilities |
| Quelles centrales au charbon en Inde ? | unknown | **answered** | Q24489 « charbon »/"coal" (loaded meanwhile) → label bridge to GPPD "Coal" in `kg_cross`: 253 plants |
| Which coal power plants are in India? | answered | answered | (had regressed when Q24489 appeared: `kg_cross` now tries equal-label alternatives) |
| Prix du Brent en 2020 / Quel est le prix du Brent ? | unknown | **answered** ×2 | `atlas.eia.spot_d` is registered after the snapshot: read in its latest registered version (as the forecast registry does), labelled « hors instantané » |
| Maladies causées par Mycobacterium tuberculosis (fr/en) | answered | answered | |
| Causes de décès (France ×2, Sénégal) | answered | answered | |
| Quelles maladies causées par le virus Zika ? | unknown | **answered** | label bridge Q202864 → `ncbitaxon:64320` "Zika virus": MONDO T1 claim (has cause) |
| Forecasts (France ×2, Japon, chômage) | answered | answered | |
| Decisions (BCE 50 pb, 25 bp, 2 %, OPEP+) | answered | answered | |
| Que se passerait-il si les licornes votaient ? | unknown | unknown | no reference decision (by design) |
| Effet du pétrole sur l'inflation (fr/en), sur le chômage, sur le PIB | partial | partial | no published oil → inflation/unemployment/GDP estimate loaded; neighbours shown as non-equivalent |
| Impact des taux directeurs sur le PIB | answered | answered | |

Gains attributable to V8ASK: 10 (méthane, cœur ×2, chromosome 17, mammifères, Glencore, cuivre, Brent ×2, Zika)
+ charbon (fr; bridge on newly loaded data) + coal (en; regression fixed). Eau: data loaded by another agent.

**Latency (endpoint, 2026-10-18, machine shared with ~10 agents):** first pass median 2.2 s, p90 26 s, max 110 s;
warm (backends warm, answer cache bypassed) median **1.0 s**, p90 28 s; repeated question (answer cache) 5–9 ms.
Slow tails are not in V8ASK code:
* forecast questions: 25–35 s — `reva.evaluation.registry.read` re-verifies every sealed manifest on each call
  (104 issue batches, 232 MB of JSON, against a 16-manifest LRU). V8ASK memoises the port per (family, geo) /
  question for 1 h (`MemoForecasts`), so repeats are < 0.5 s. **Request to the registry owner:** index questions by id
  (question → batch sha) instead of scanning all manifests.
* decision scenarios: 7–100 s per engine run under load (V6 measured 1.5 s on a quiet machine); runs are seeded, so
  V8ASK memoises them per (decision, draws, seed) (`MemoDecisions`).
* p95 < 300 ms is therefore met only for cached answers; atlas/KG answers are 0.4–3 s warm.

Run it: `uv run python -m reva.v8.ask.golden [--json out.json]` (CLI, two passes), or through HTTP as above.

## 2. What was built

**Backend**
* `src/reva/nlqa/bridges.py` — resolution bridges, each worded in the answer, never silent:
  `label_folded` (accents/case only), `label_legal_form` (proper noun + PLC/AG/SA/…), `label_qualified` (one-kind
  disambiguation sets only — "Wolf (band)/(film)" refused), the code-alias guard ("EAU" ≠ « eau »), the record
  label bridge (other records carrying one of the chosen record's own fr/en labels) and the catalog-name bridge
  (scientific name the atlas catalog gives in parentheses). Wording: « fiche distincte portant le libellé anglais
  « heart » de cœur (Q1072) — rapprochement par le libellé publié, pas une identité affirmée par une source (fiches
  non fusionnées) ». Bridge queries failing (timeout) just mean "no bridge".
* `src/reva/nlqa/registered_tables.py` — latest registered version of a concept's declared table when the snapshot
  lacks it (pinned ≤ now, licence of the version, `outside_snapshot: true`); `atlas_period` says « Lu hors de
  l'instantané atlas courant… ».
* Hooks (covered by the 229 NLQA tests, all green): `adapters.py` (search refinement + `label_bridges`),
  `domain_exec.py` (bridges in `kg_families`/`kg_cross`, registered tables, species-count literal hint),
  `executor.py` (bridges in `why`), `compose.py`/`domain_compose.py` (wording), `ports.py` (`Backends.tables`),
  `domain_adapters.py` (wiring). **Honesty fix found on live data:** a statement read against its direction
  (hydrogen « part of » water) was worded « hydrogène — comprend hydrogène, oxygène »; the subject is now the
  object of such statements (« eau — comprend … »).
* `src/reva/v8/ask/` — `bundle.py` (headline: exact number / sentence / partial / « REVA ne sait pas »; evidence
  view chosen by shape: timeseries, bars (one unit; other units listed apart), forecast bands, graph, causal hops,
  estimates, scenario; « Comment c'est calculé »; sources with licence flags; deep-link targets; follow-ups; pin
  reference), `service.py` (answer cache, series re-read at the cited source, memo ports, catalog labels),
  `brainstorm.py`, `golden.py`.
* `src/reva/api/routes/v8_ask.py` — `POST /v1/v8/ask/answer`, `GET /v1/v8/ask/brainstorm?target=`,
  `GET /v1/v8/ask/examples` (scope `read`, per-actor limit `REVA_V8_ASK_RATE_PER_MINUTE`, default 30/min).

**Brainstormer** (registries + rules only): target parsing (verb, horizon, country by name or adjective), anchors
(forecast families, catalog concepts, scoreboard targets, effect variables — direct when *every* word matches, else
« proche (non équivalent) » by ≥ 2 shared words with an anchor label), drivers = causes of published estimates on
those variables (counts, signs, methods, references; never averaged), data status per driver (curated
`atlas_concepts` link or the concept itself; last *observed* period, projections counted apart, registered tables
outside the snapshot; missing → catalog candidates marked « non reliés par un curateur » + report lines naming the
id), attributed causal claims on the target, sealed forecasts (questions, statuses, models), scoreboard verdicts
copied from `registry/scoreboard/tracks.yaml`, ranked next steps. Latency 0.8–2.4 s.

**UI** (`ui/src/v8/features/demander/`): `DemanderPage` (Question / Brainstormer modes, `?q=` = `v8Links.ask`,
`?mode=brainstorm&t=`, Enter/↑/Esc, history in localStorage with try/catch, examples), `AnswerCard` (status badge,
honesty badge, big exact number or headline sentence, staggered reveal, actions: Épingler (`tableaux/pin`: a note
with the question + re-ask link, plus a series pin when concept+country), Copier le lien, Monde / Fiche / Comparer
/ Liens / Décision / Scores links, Brainstormer; sentences with E-id citations; long sentences « lire la suite »;
sources & licences; follow-ups; « Comment c'est calculé »; evidence list), `EvidenceView` + `TimeChart` (uPlot;
cited points circled, projections dashed, table fallback), `BrainstormView`, `index.ts` (palette: « ? … » →
Demander, « prévoir … » / « brainstorm … » → Brainstormer, two commands).

## 3. Tests
* `tests/unit/v8/test_v8_ask.py` (17): golden set = NLQA6 set, fixture-mode coverage floor + every unknown says what
  is missing, classifier, label rules, label bridge wording, registered-table fallback, bundle (number/bars/
  sources/targets/pin, unknown, forecast, estimates, scenario, path, graph), series reader + cache, brainstorm
  (parse, fixture run, unknown target), routes; regression tests for the reversed-statement subject and the
  `kg_cross` bridge.
* `tests/integration/v8/test_v8_ask_live_db.py` (4, `db` marker, read-only): méthane / cœur / Glencore via bridges,
  brainstorm on the real registries.
* `ui/src/v8/features/demander/demander.test.tsx` (14, fixtures generated by the real pipeline on fixture backends).
* `tests/unit/nlqa` 229 passed; `tests/invariants` 191 passed, 1 skipped; ruff + mypy clean on new modules; tsc clean
  on the feature. (vitest needs `--pool=threads` when the machine is loaded: forks time out.)

## 4. Screenshots (scratchpad `v8/V8ASK/`, reviewed and iterated)
* q01 Causes de décès en France — « 28,06 % of total deaths » for cancers, 8 WHO GHE values as ranked bars; the
  "probability of premature death" (another unit) listed apart; who/gho CC BY 4.0.
* q02 Prix du Brent — 126,55 $/b on 2026-10-14, 199 daily points 2026 (uPlot), « hors instantané » badge, EIA /
  Refinitiv licence text.
* q03 Prévision d'inflation France — REVA ensemble point with 5–95 % band, badge « prévision scellée », per-model
  dot-and-band rows (two sealed families), « en attente de résolution », link to Scores.
* q04 BCE +50 pb — « Scénario non validé — pas une probabilité » headline, variables per country listed once,
  « Ouvrir dans le studio de décision ».
* q05 Parties du cœur — « heart — « comprend » : cardiac chamber, fibroblast… (27) », bridge sentence, radial graph.
* q06 Espèces de mammifères — catalog-name bridge sentence, COL 6,464 / 6,365 species as published.
* q07 Pétrole → inflation — « Réponse partielle », what is missing (hatched), 8 neighbouring estimates each badged
  « voisine ».
* q08 Où vit le lion — « REVA ne sait pas. » + « relation non chargée pour cette entité ».
* b01 Brainstorm inflation française 4 trimestres — 4 drivers (policy rate, discounted unemployment, regional
  unemployment, sanctions) with references; imf.policy_rate missing for FRA (no national policy rate); 4 sealed
  families; scoreboard verdicts (« ne bat pas » rows copied with Holm basis); next steps.
* b02 / p02 Brainstorm Brent (dark, desktop and 390 px phone) — Brent concept loaded outside the snapshot to
  2026-10-14, drivers of « Prix mondial du pétrole brut » (proche, non équivalent), 2 families, 3 × bat / 6 × ne bat
  pas vs EIA STEO; next step: curate the link `oil_price.global` → `prices.daily.brent_spot`.
* p01 phone answer: no horizontal overflow on any capture (0 px).

## 5. Honesty choices
No translation table, no head-noun or fuzzy matching; every bridge is named with its rule; numbers are the exact
decimal strings of evidence (headline rounding for display only); estimates never averaged; forecasts carry their
status and model; scenarios « non validé — pas une probabilité »; projections not counted as « dernière période »
in brainstorm; neighbouring estimates labelled « voisine »; licences shown per source, NC flagged; registered-table
values flagged « hors instantané »; partial ≠ answered in the coverage count.

## 6. Requests to the lead / other owners
* Contract: a `v8Links.compare(series, geo)` helper (I use V8MONDE's `/v8/comparer?s=&g=` directly).
* Forecast registry read path (`reva.evaluation.registry.read.question/questions`): O(104 manifests) per call — index
  by question id; `ManifestCache(16)` thrashes.
* Family index (`reva.kg.family_query`): neighbours of a statement read against its direction come back with
  `inverse=False` (worked around in the composer).
* Curation suggestions surfaced by the Brainstormer: `oil_price.global` has no `atlas_concepts` link (Brent/WTI
  daily concepts exist); `imf.policy_rate` has no euro-area series for EA members.
* Data that would answer the remaining golden questions: French labels for ChEBI/GPPD/petroleum records, IUCN/
  Wikidata taxon ranges (P183/P9714), an en catalog label with "(Mammalia)", published oil → inflation/GDP/
  unemployment estimates.
