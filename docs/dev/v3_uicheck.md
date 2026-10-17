# V3 UICHECK — the causality UI on the real dev database

Agent UICHECK, 2026-10-17. Scope: `ui/src/v3/**` (screen "Causes & conséquences", its chain finder, Chemin, Frise,
Demander) and its zod schemas, checked against the **dev database `reva`** (origin live, ~52k current causal claims:
ICB, MID, UCDP, NAVCO, MMP, MEC, Archigos, NOAA NCEI, TIES, EUSANCT, ESRB, COW wars, IDMC, UCDP dyad terminations,
Wikidata T2–T4; 915 `same_as` links). Specs: `docs/roadmap/v3_causality.md`, `docs/dev/v3_causal_interface.md`,
`docs/dev/v3_causfix.md` (grading v2, `attribution`), `docs/dev/v3_causmore.md`, `docs/dev/v3_sameas.md`.
No backend file was changed; API defects are listed in §4 with the request and the response.

## 1. How it was checked

* `uv run reva api --port 8411` against `reva` (read-only use), a temporary `read` token
  (`reva auth create-token --label service:uicheck`, **revoked** at the end with `reva auth revoke-token`), Vite dev
  server (`ui`, port 5199, `/v1` proxied to the API), Chromium driven by Playwright (`/opt/pw-browsers`) with the token
  as an `Authorization` header. Read-only SQL (`default_transaction_read_only=on`) only to pick examples.
* Events exercised: Cuban Missile Crisis `icb:crisis:196` (+ its outcome record), WWI `Q361`, Sarajevo assassination
  `Q192050`, Great Depression `Q8698`, Russo-Ukrainian war `Q15860072` (P3680 attribution), dissolution of the USSR
  `Q5167679` (P3680 + P5102), NOAA tsunami `ncei_hazards:tsunami:1`, IDMC `idmc:event:1`, COW World War II
  `cow:war:139` (29 coded outcomes), sanctions `eusanct:case:1990122101` / `ties:case:1990122101`, NAVCO
  `navco21:408` (67 claims, the largest causal neighbourhood in the DB), Indonesia `Q252` (5,138 IDMC events attached
  as context), chains Sarajevo → WWI, TIES case → EUSANCT policy result (identity hop), ICB 196 → its outcome; Chemin
  Sarajevo → WWI; Frise WWI and Indonesia; Demander "Pourquoi la Première Guerre mondiale ?".
* Page loads on the real data: every Causes page is complete in ~1 s (4–6 requests); the largest neighbourhood
  (`navco21:408`) renders without freeze. `POST /v1/ask` took 26 s on the first call (backend), `/v1/links/path`
  10–15 s (Chemin, backend).

Screenshots (`docs/dev/screens/`, 14 files, 1.7 MB in all, PNG8):

| File | What it shows |
|---|---|
| `v3_causal_wwi.png` | WWI: graded graph, 15 causes / 17 effects each "selon Wikidata (T3/T4) · Pxxx", context edges worded "relation de contexte, pas une cause" |
| `v3_causal_inception_detail.png` | WWI → German Empire: `undetermined`, reason "entité datée par sa création (P571)…", existence span 1871–1918, grade rules v2 |
| `v3_causal_attribution_detail.png` | NATO expansion → Russo-Ukrainian war: "Selon Wikidata (T4), affirmation attribuée à Q7747 et Q1140115 … Wikidata ne l’affirme pas lui-même", `grade.D.attributed` |
| `v3_causal_icb196_outcome.png` | ICB 196: coded trigger as a cause, coded outcome listed apart ("issue, pas une cause", diamond under the event), "licence non ouverte" (ICB terms) |
| `v3_causal_cow139_outcomes.png` | COW World War II: 29 per-state coded outcomes listed as outcomes (COW terms: "accès payant interdit") |
| `v3_causal_idmc_licence.png` | IDMC flood → displacements: `has_effect` T1 B, **licence non commerciale** (CC BY-NC) on the line and in the licence list |
| `v3_causal_noaa_detail.png` | NOAA earthquake → tsunami: coded trigger T1, codebook, public-domain licence |
| `v3_causal_depression_card.png` | `Q8698`: not a KG entity — said so instead of passing the bare id off as a name |
| `v3_causal_chain_identity.png` | TIES ≡ EUSANCT identity hop with its evidence and rule note, "Composition par REVA … à travers 1 lien(s) d’identité", server duplicate merged and counted |
| `v3_causal_chain_sarajevo.png` | Sarajevo → WWI, one hop asserted by two Wikidata statements (P1478 on WWI, P1536 on Sarajevo), grade D |
| `v3_causal_chain_outcome_excluded.png` | ICB 196 → its outcome: no chain; why outcomes are never composed |
| `v3_causal_navco408_large.png` | 67-claim neighbourhood: list paged by 25, graph and frise bounded with the counts of what is not drawn |
| `v3_causal_chemin.png` | Chemin Sarajevo → WWI: "cause immédiate de" (was "a pour cause sous-jacente") |
| `v3_causal_demander_why.png` | NLQA why WWI: every sentence "Selon Wikidata (T4 …)", evidence items with French relation labels |

## 2. Bugs found and fixed (UI only)

| # | Where | Problem on the real data | Fix |
|---|---|---|---|
| 1 | `causal/api.ts`, `parts.tsx`, `model.ts` | The new claim `attribution` (P3680 *statement supported by*) was ignored: an attributed claim read "Affirmé selon Wikidata (T4)" | Parsed (`supported_by`, `nature_of_statement`, `source_validity`, notes, plus `scope.source_validity` doubtful codes). Line badge "attribuée à X — Wikidata ne l’affirme pas lui-même"; detail header "Selon Wikidata (T4), affirmation attribuée à X" + "Wikidata indique que X affirme ce lien ; Wikidata ne l’affirme pas lui-même"; P5102 nature shown verbatim; "grade plafonné à D" only when a `grade.D.*` cap rule decided it |
| 2 | `api.ts`, `parts.tsx` | `temporal_check` reason never shown (the UI read a non-existent `temporal_detail`); "undetermined" was worded "dates manquantes" even for WWI → empires; an entity's date was shown as its founding day | Reads `grade.temporal.reason` (known reasons translated: inception, missing start of cause/effect/both), `entity_dated_by_inception`, `cause_time` / `effect_time`; detail shows "entité existant de 1 janv. 1871 à 9 nov. 1918 — datée par sa création (P571), pas un événement"; label "antériorité non établie (date manquante, ou entité datée par sa création)" |
| 3 | `CausesPage.tsx`, `model.ts`, `parts.tsx`, `CausalGraph.tsx`, `CausalTimeline.tsx` | `coded_outcome` listed under "Conséquences affirmées", counted as a consequence, drawn as an effect; on an outcome record "Pourquoi ?" listed the event under "Déclencheurs codés" (a cause) | Outcomes split out everywhere: lists "Issues codées (comment l’événement s’est terminé)" / "Issue codée de", graph diamonds under the event, timeline "issue codée : …", "Pourquoi ?" group "Issue codée de… — pas une cause" (out of every causal lane, whatever lane the server sends), event counts "n cause(s) · m conséquence(s) · k issue(s) codée(s)" |
| 4 | `parts.tsx` (ClaimLine) | In the effects list the line read "New Deal · a causé" (effect label followed by the cause→effect label: reversed meaning) | Every line is the full sentence in the source's direction: "cause — relation → effet", the event on screen in plain text, the other end a link; Wikidata property shown (`P1478`, `P1536` …) so the two statements of one link are told apart |
| 5 | `lib/paths.ts` (Chemin) | P1536 rendered "a pour cause sous-jacente" (it is *immediate cause of*), P1537 "a pour effet contributif", P1479 "a pour cause contributive"; backward causal hops "a pour effet (sens inverse)" | Wikidata's own labels ("cause immédiate de", "facteur contributif de", "a pour facteur contributif"); backward hops use the inverse ("est un effet de") |
| 6 | `causal/rights.ts` (new), `parts.tsx`, `CausesPage.tsx` | No licence anywhere on the causality screen (IDMC CC BY-NC, ICB / COW / TIES / Archigos not open) | Rights read once from `GET /v1/sources` (registry `rights`), flags derived with the atlas rule (`normalise_rights`); badge on each restricted claim line, licence row in the claim card, "Licences restrictives des sources affichées" list (claims and event-card sources); an unavailable registry is said |
| 7 | `ChainFinder.tsx`, `api.ts` | The server returned the same path twice (§4.2); "composition par REVA" only in a collapsed panel; `max_hops` read from the wrong place; English not-found reason; nothing on what the search skipped | Identical paths merged and counted ("1 chemin(s) identique(s) renvoyé(s) en double par le serveur"); visible per-chain wording "Composition par REVA de N affirmation(s) … à travers K lien(s) d’identité … aucune source n’affirme cette chaîne"; identity rule note shown; `search.max_hops`; known reasons in French; class nodes / quarantined / below-grade / ungraded counts; banner and empty state say outcomes, context, attributed/qualified and cyclic claims are never composed |
| 8 | `CausesPage.tsx`, `model.ts`, `CausalTimeline.tsx` | 66-claim neighbourhood drew 66 boxes per column and 66 frise rows | Lists paged by 25 ("afficher 25 de plus (41 restantes)"), graph ≤ 40 nodes per column ("26 affirmation(s) non dessinée(s) … toutes figurent dans les listes"), frise ≤ 30 rows per lane with the count |
| 9 | `parts.tsx`, `CausesPage.tsx` | Empty states said "Aucune affirmation sourcée" / "dans les données consultées" | "Aucune source chargée n’affirme de cause (de conséquence) pour cet événement", "Aucune source chargée n’affirme de cause ni de conséquence … REVA ne déduit aucune cause de la chronologie" |
| 10 | `parts.tsx` (EventCard) | `Q8698` titled "Q8698" as if it were a name; React duplicate-key errors on WWI participants | Title from the EVENTS card, the causal view, else the label the claims carry; "Cet identifiant n’est pas une entité du graphe de connaissances chargé : seules des affirmations causales le citent"; participant keys unique |
| 11 | `parts.tsx`, `CausesPage.tsx` | Candidate label did not read "non sourcé — candidats" | `CANDIDATE_LABEL = 'non sourcé — candidats (extraits de textes ou proposés par un modèle, non vérifiés)'`, toggle "Afficher l’espace « … »" |
| 12 | `model.ts` | Grade legend still described grading v1 (D = T4 only) | v2 wording: D includes the caps (attributed, nature-qualified, source-doubtful); A "corroborée par une source indépendante" |
| 13 | `model.ts` (ticks) | Time axis ticks "1961.5" | Whole years only |
| 14 | `lib/kinds.ts` | Event types in English ("international crisis", "disaster event" …) | French labels for the 20 kinds of causal records in the DB |
| 15 | `pages/DemanderPage.tsx` | Evidence items showed raw relation ids (`immediate_cause`); lede did not mention causal claims | French relation labels, "(issue, pas une cause)" for outcomes, `supported_by` worded when the server sends it (§4.5); lede updated |

Context edges were already worded "relation de contexte, pas une cause" (checked on WWI, ICB 196, IDMC, Indonesia:
the causal view bounds context at 50 and the UI says "liste bornée").

## 3. Tests

* New `ui/src/v3/causal/uicheck.test.tsx` (14) on `real_samples.fixture.json` — responses recorded from the dev DB
  (attributed claim 27009, nature-qualified claim, WWI → German Empire, ICB 196 coded outcome, the duplicated sanctions
  chain, registry rights of 6 sources; trimmed, contents unchanged): attribution wording, nature, doubtful codes,
  inception span, outcomes in why/graph/lists/counts, licence flags (IDMC NC, CC0 open, COW charge, ICB not open),
  chain dedupe + wording + identity note, French not-found reason, paging and bounds of a 60-claim neighbourhood,
  integer ticks, Chemin property labels.
* Updated `causal.test.tsx` / `model.test.ts` for the outcome split and the candidate label.
* `npm run test` (vitest): 31 files, 240 passed, 1 skipped. `npm run typecheck`: clean. `npm run build` (to a
  scratch `--outDir`, removed afterwards): ok.

## 4. API / data issues found (not fixed here: backend belongs to other agents)

1. **`GET /v1/events/{id}` → 500** for ICB and NAVCO events (EVENTS, `src/reva/api/routes/events.py:385`).
   `GET /v1/events/icb:crisis:196` (also `navco21:408`): `pydantic ValidationError: 1 validation error for EventCard
   dataset_kind.source_key — Input should be a valid string [input_value=None]`. The UI falls back to the causal
   event view (no participants/places from cited statements for those events).
2. **Duplicated chain paths** (CAUSCORE/SAMEAS, `reva.causal.graph.chain`).
   `GET /v1/causal/chain?from=ties:case:1990122101&to=eusanct:policy_result:1990122101&max_hops=4` → `paths_found: 2`,
   two identical paths (identity `reva.events.same_as:eusanct_ties_case:eusanct:case:1990122101=ties:case:1990122101`,
   then claim `ds:eusanct.kondata:eusanct_case:1990122101:hse_contribution`). Merged and counted by the UI.
3. **Outcomes counted as causes by the API**. `GET /v1/causal/events/icb:crisis:196:outcome/why` files the
   `coded_outcome` claim in lane `coded` and `summary_fr` says "1 affirmation(s) causale(s) sourcée(s)";
   `GET /v1/causal/events/{id}` counts `effects: {"cause": {"B": n}}` including coded outcomes (family `cause`).
   The UI separates them; an `outcome` lane / count bucket in the API would remove the need (CAUSFIX follow-up "count
   coded_outcome apart" applies to the API too). `not_found_reason` for `icb:crisis:196 → icb:crisis:196:outcome` is
   the generic "no causal chain within max_hops among claims graded at least D", it does not say the only link is an
   excluded outcome.
4. **Wording/labels in the data**: `Q8698` (Great Depression), `Q5167679` have no KG entity (only claims cite them;
   `/v1/events/Q8698` 404, causal view label = id); attribution parties `Q7747`, `Q1140115`, nature `Q20820099` and
   several places (`Q55643`, `Q7204` …) have no KG label, so the UI names them by QID.
5. **NLQA** (`src/reva/nlqa`): causal evidence items carry no `attribution` / `supported_by` (the sentence text has
   it); `TEMPORAL_TEXT` words every `undetermined` as "dates manquantes, antériorité non vérifiable", also for
   entities dated by inception; the keyword planner found no entity for "Pourquoi la guerre russo-ukrainienne ?" and
   "Pourquoi la dislocation de l URSS ?", so attribution narration could not be checked live; first `/v1/ask` 26 s.
6. No current claim carries `scope.source_validity` yet (AUDFIX's NOAA re-delivery not in `reva` at the time of the
   check): the doubtful-coding display is covered by a unit test only.
7. `/v1/causal/events/{id}/causes|effects` have `limit ≤ 500` and no offset; the UI asks 200 and says "Liste bornée
   par le serveur" when truncated. The largest causal neighbourhood today is 67 claims; IDMC's 22,108 claims are one
   or two per event (country hubs such as Indonesia hold IDMC events only as context, bounded at 50 by the API).

## 5. Remaining UI limits

* The graph is drawn with Cytoscape in a dark canvas; edge grade letters only (rule ids are in the cards).
* The chain finder does not expose `min_grade`; the server default (D) applies and is shown when it skips claims.
* Licence flags rely on the registry's recorded `rights` text (same derivation as the atlas); a source with no
  recorded rights shows "non indiquée dans le registre des sources".
* Frise and Neurones show KG statements, not causal claims; they were checked for causal wording only (none on the
  WWI frise). Chemin's causal hops are worded "selon Wikidata …, relation causale affirmée par la source".
