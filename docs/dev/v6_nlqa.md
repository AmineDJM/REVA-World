# V6 NLQA6 — "Demander" across the new domains

Agent NLQA6, 2026-10-17. Brief: make `POST /v1/ask` (UI "Demander") answer questions about composition and
structure, the living world, energy and industry, health, prediction and effects, honestly and with sources. No
data loaded, no DB write, no network; the LLM planner stays disabled (credits exhausted), so everything below runs
through the deterministic **keyword planner** (`planner: keyword_fallback`). Prompt bumped to `nlqa-plan-1.3.0`
(schema + rules for the new operations, for when the LLM planner returns).

## 1. New operations (closed set, validated server-side)

| op | arguments | backend (same modules as the V4–V6 APIs) |
|---|---|---|
| `kg_families` | `entity` (lookup ref), `families[]` (ids of `registry/kg/relations_v4.yaml`), `reading` forward/inverse/any, `count` | `reva.kg.family_query.entity_families` (= `/v1/kg/entities/{id}/families`) |
| `kg_cross` | `entity`, `families[]` ([] = any), `other_entity`, `other_families[]` | two-sided join on `kg.statement_current` over the family registry's (pid, direction) pairs |
| `effects` | `cause_text`, `effect_text` (words of the question), `country?` | `reva.effects.query.list_estimates` (= `/v1/effects/estimates`) |
| `forecast` | `text` (words of the question), `country?` | `reva.evaluation.registry.read` families / questions / question (= `/v1/forecasts/*`, seal re-verified) |
| `decision` | `text`, `magnitude?`, `unit?` (bp, pp, percent, mb/d, years) | `reva.predict.run.run_on_connection` in a READ ONLY transaction (= `POST /v1/predict/decision`) |
| `atlas_period` | `concept`, `from_year?`, `to_year?` | atlas snapshot cells of the concept's declared series, any geography (world prices, benchmarks) |

Validation rejects (never substitutes): unknown family (`family_not_in_list`), texts not taken from the question
(`text_not_in_question`), unknown unit, unknown country. Health questions reuse `why` (causal claims, T1–T4 only)
plus `kg_families` on associated_with / interacts_with / context; causes of death reuse `atlas_value` over the
catalog concepts whose `subdomain` is `causes_of_death` (WHO GHE).

Code: `src/reva/nlqa/domains.py` (question shapes + registry-label matcher), `domain_exec.py` (executor mixin),
`domain_compose.py` (fr/en templates + the sources/licences sentence), `domain_adapters.py` (real adapters,
wired by `build_backends`), small hooks in `plan.py`, `prompt.py`, `fallback.py`, `executor.py`, `compose.py`,
`catalog.py` (family ids, concept sub-domain), `ports.py` (new ports/records), `adapters.py` (`period_cells`),
`service.py` (the sources sentence is never sent to the rephraser).

## 2. Resolution rules (no name → id table)

* **Entities** only through the KG label index (`kg.entity_label`, fr/en labels + aliases) via `entity_lookup`.
  The V6 ops accept only `label_exact`, `alias_exact` or `label_words` matches: a prefix or fuzzy match is refused
  with its candidates listed ("Aucune entité au libellé ou alias exact … aucune réponse n'est donnée sur une entité
  incertaine") — live, "wolf" only prefix-matches *Wolfiporia cocos* and "charbon" (fr) only prefix-matches
  "charbon oropharyngé" (anthrax): both refused instead of answered.
* When several records share the **same exact label** (one taxon published by COL, EOL, GBIF and NCBI) and the
  chosen one has nothing, the next equally matching record is read and the answer says so ("réponse lue sur …,
  autre fiche au même libellé … fiches non fusionnées"). Applied to `kg_families` and to `why` (live: the causal
  claims on *Mycobacterium tuberculosis* sit on `ncbitaxon:1773`, not on the `col:CSMW3` record chosen first).
* **same_as** identity (V4LINK `v4link:wikidata_item`): when the entity has no requested relation, its identity
  neighbours are read and worded "identifie à la même entité … pas une fusion de fiches".
* **Registry terms** (effect variables / atlas concepts / event types of the effect layer, forecast families,
  reference decisions) are matched to the question's words by their own fr/en registry labels
  (`domains.match_terms`: ≥ half of the content words, change words such as "hausse" ignored); every tie is
  queried and every term used is shown in an identification sentence.
* Concepts: a new fallback `covering_concepts` takes catalog concepts whose labels hold *every* content word of
  the question ("prix du Brent" → `prices.daily.brent_spot`); with no country it plans `atlas_period`, never a
  country ranking.

## 3. Honesty wording (tested)

* Every answer ends with **"Sources et licences : …"** (source key + licence of every cited record; Wikidata CC0;
  "licence non renseignée" when the record carries none); restricted licences keep their V3 warning.
* Families are never causal; `associated_with` sentences end "association signalée par la source, jamais une
  cause", `interacts_with` "interaction enregistrée, pas une cause", `context` "relation de contexte, non causale".
* Counts: "Rattachements directs chargés … un nombre de déclarations chargées, pas un recensement".
* Literal values (owner names published as text, formulas) are quoted as "valeurs publiées comme texte … aucun
  rapprochement par le nom", with the published share qualifier when present; duplicates removed.
* Effects: each estimate keeps the effect layer's attributed wording ("Selon …, … ; REVA n'établit pas cet effet")
  with reference, locator, licence and a verbatim excerpt; then "présentée(s) une à une … REVA ne les moyenne pas,
  ne les combine pas". No exact pair → "Pas de donnée chargée … REVA ne déduit pas d'effet d'autres estimations",
  then neighbouring estimates labelled "non équivalentes, elles ne répondent pas à la question".
* Forecasts: per sealed question, every model with point, 5–95 % band, **method** (registry model description)
  and **score status** ("en attente de résolution … aucun score encore" / "résolue — valeur publiée …, scores …"),
  seal date, batch sha, "prévisions statistiques de référence, pas un conseil"; families that do not cover the
  country are named.
* Decisions: first sentence "Scénario non validé — pas une probabilité : moteur de décision REVA, décision de
  référence « … »" + validation status; a magnitude from the question applied to a **copy** of the reference
  (`<id>.ask`, unit checked against the instrument's units; otherwise the reference is shown and the mismatch
  said); per variable p50 and 5–95 % (or "aucune incertitude publiée sur cette chaîne"), estimate keys of the path;
  unquantified links counted without numbers; the engine's `disclaimers_fr` verbatim. No single matching
  reference → "le moteur ne compose pas de décision à partir d'un texte libre, aucun scénario n'est calculé".
* Geography-free series: first, last, lowest and highest *published* values, "aucune moyenne calculée".
* Unknown → "Aucune entité ne correspond à « … »" / "Pas de donnée chargée … une absence de donnée chargée, pas une
  absence dans le monde". Absent backend → typed `capability_unavailable`.

Fixed on the way: an absence record of an unresolved lookup had its `kind` overwritten by the lookup's kind hint
(`"any"`); the hint is now `entity_kind`.

## 4. Tests

* `tests/unit/nlqa/test_nlqa_domains.py` — **47 golden questions** (fr + en, fixture mode, real planner →
  validation → executor → composer → service) + 10 property tests: citations exist for every sentence, one
  sources sentence, associations never causal, family evidence `causal: false`, estimates one by one (two values,
  no mean), magnitude applied to a copy (+ sign from "baisse"), sealed-forecast evidence, capability_unavailable
  for each new backend, no entity outside the label index, plan rejections, fixture latency.
* Fakes: `tests/fixtures/nlqa/domain_fakes.py` (`fixture:` ids, `fixture.*` sources, origin test_fixture).
* UI fixture regenerated with the repo generator
  (`REVA_REGEN_UI_FIXTURE=1 uv run pytest tests/unit/nlqa/test_nlqa_causal.py -k ui_fixture`): keys `why`,
  `chain` + `composition`, `cross`, `health`, `effects`, `forecast`, `decision`, `missing`.
  `DemanderPage.tsx` labels the new evidence kinds; `causal.test.tsx` + 1 test (forecast/decision/effects/missing).
* Results: `tests/unit/nlqa` 229 passed; `tests/invariants` 191 passed, 1 skipped; UI `causal.test.tsx` 16 passed,
  `tsc --noEmit` clean; ruff clean on touched files. `tests/integration/test_nlqa_api.py` not run (it creates a
  throwaway database: no DB write in this brief); only its prompt-version string changed.

## 5. Live check on the dev DB (read-only) and latency

LIVE_RESULTS

## 6. Limits / owner decisions

* French labels are missing for many V4 records (ChEBI "methane", GPPD "Coal", NCBI taxa): fr questions about
  them answer "aucune entité" until labels are loaded (no translation table by design). Wikidata items with fr
  labels (loup, cœur, mammifères) often carry no V4 relations; the bridge is `same_as` where V4LINK loaded it.
* No taxonomic lineage of mammals is loaded (NCBI/COL parent links), so "combien d'espèces de mammifères" answers
  "pas de donnée chargée pour compter"; GBIF species counts in the atlas are per country and kingdom only.
* `eia.spot_d` (Brent daily) registered after the current atlas snapshot: "Prix du Brent en 2020" answers the
  absence until the lead's `reva atlas snapshot`.
* The decision engine is cold-slow (≈ 30–50 s first call: atlas snapshot verification, registry YAML parsing,
  plugin data); ≈ 1 s warm. The API warm-up opens the atlas; a warm-up call of the engine would remove the rest.
* The keyword planner recognises the shapes listed in `domains.py`; anything else falls back to the V3 rules. The
  LLM planner has the new operations in its schema but is untested live (no credits).
