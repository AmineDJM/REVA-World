# V3 — CAUSCAND: causal candidates mined from Wikipedia text (tier T5, quarantined)

Agent CAUSCAND, 2026-10-16. Spec: `docs/roadmap/v3_causality.md` (principle 7: candidates are quarantined;
tiers T5 text-mined / T6 model-proposed), contract `docs/dev/v3_causal_interface.md` (`insert_candidates`).

**A candidate is not a claim.** It records that one sentence of one Wikipedia revision uses an explicit
causal connective between two of the sentence's own wikilinks. It is never promoted automatically
(`promotion_status = not_promoted` until a maintainer review in `kg.causal_review`, which no runtime role can
write), is shown only under the "candidats non sourcés" toggle (CAUSUI) and is never used by NLQA.

| file | role |
|---|---|
| `src/reva/causal_candidates/seeds.py` | event seeds: one budgeted WDQS query (direct `P31` of 25 event classes, sitelinks >= 40), recorded snapshot |
| `src/reva/causal_candidates/wikitext.py` | offset-preserving wikitext renderer (rendered sentence ↔ verbatim wikitext slice ↔ wikilinks) and sentence splitter |
| `src/reva/causal_candidates/patterns.py` | connective rules EN/FR with direction rules, negation / hedge cues, argument-position lexicons |
| `src/reva/causal_candidates/extract.py` | sentence records, argument rules, linking to QIDs, candidate documents (`EXTRACTOR_VERSION`) |
| `src/reva/causal_candidates/fetch.py` | the only network path: MediaWiki action API, budget-metered, throttled, resumable |
| `src/reva/causal_candidates/insert.py` | hand-off to CAUSCORE `insert_candidates` (sentence bundles as cited artifacts) |
| `src/reva/causal_candidates/cli.py` | `python -m reva.causal_candidates seeds|harvest|relink|sample` |
| `registry/sources/wikipedia_causal_text.yaml` | source `wikipedia.causal_text` (CC BY-SA 4.0, hosts en/fr.wikipedia.org) |
| `registry/atlas/budget_requests/causcand.yaml` | 600 requests/day for `wikipedia.causal_text` |
| `tests/unit/test_causal_candidates.py`, `tests/unit/test_causal_candidates_fetch.py` | offline tests (no network, no DB) |
| `tests/fixtures/causal_candidates/` | seed snapshot (CC0), API excerpts (verbatim sentences of pinned revisions, CC BY-SA 4.0), reconstructed resolution responses, `manifest.json`, hand-labelled evaluation sample |

## 1. Pipeline

1. **Seeds** (`seeds.py`). Which articles are read is decided by Wikidata sitelink counts, not by REVA:
   one WDQS query (budget key `wikidata.wdqs`, through `reva.kg.harvest.budgeted_client`, ledger in the
   throwaway DB `reva_causcand`, raw response archived in `var/kg/archive_index.jsonl`) lists direct
   instances of war, civil war, world war, war of national liberation, armed conflict, revolution, coup,
   rebellion, riot, financial / economic crisis, recession, famine, epidemic, pandemic, disaster, natural
   disaster, earthquake, tsunami, genocide, massacre, terrorist attack, invasion, conquest with >= 40
   sitelinks and their enwiki/frwiki titles (`schema:about` sitelinks). Snapshot 2026-10-16:
   388 items (`tests/fixtures/causal_candidates/event_seeds_2026-10-16.json`, query + sha256s inside).
   The query also listed Q3002150 by mistake (it is "aircraft crash", not a political crisis): it is not
   in `EVENT_CLASSES` and items matched only through it are filtered out. The run used the **top 250 events**
   → 250 EN + 245 FR titles = **495 articles** (≤ 500). The KG held no event entities when this ran (EVENTS /
   CAUSWD in progress); `seeds_from_kg` is documented as the replacement once they land.
2. **Fetch** (`fetch.py`). `action=query&prop=revisions|pageprops&rvprop=ids|timestamp|content` 4 titles per
   request (`maxlag=5`, generic project User-Agent, no personal data), one request at a time, >= 10 s per
   host. Each response is hashed and reduced *in memory* to the sentences matching a broad connective
   prefilter; full articles are **never stored**. `fetch_log.jsonl` keeps per article: title, page id, QID,
   revision id + timestamp, response sha256, wikitext sha256 and size. Resumable (logged titles skipped).
3. **Extract** (`extract.py`, `patterns.py`). See §2.
4. **Link**. Each argument is a wikilink *of the sentence itself*; its target is resolved by the same wiki's
   API (`prop=pageprops&ppprop=wikibase_item|disambiguation&redirects=1`, 50 titles per request) →
   Wikidata QID. Dropped (counted): missing page, non-article namespace, disambiguation page, no Wikidata
   item, date/year/century pages, self-loops (same QID after redirects), duplicates, and arguments whose link
   only modifies an unlinked head noun (§2.3). **No name matching, no fuzzy lookup, anywhere.**
5. **Store / insert** (`insert.py`). JSONL staging (`candidates.jsonl`), then `reva.causal.model.insert_candidates`
   into a throwaway DB (§4).

## 2. Rules (extractor `reva.causal_candidates.extract` 1.0.0)

### 2.1 Connectives and direction rules

Direction is always the one written. `cause_left`: cause before the connective, effect after.
`cause_right`: effect before, cause after (including fronted "Due to A, B …" / "En raison de A, B …").

| family (mission list) | EN rule ids | FR rule ids | direction | relation hint |
|---|---|---|---|---|
| caused | `en.caused` (caused, causing), `en.cause_base` (did/would/not … cause) | `fr.a_cause` (a causé, causa…; addition) | cause_left | has_cause |
| caused by (passive) | `en.caused_by` | `fr.cause_par` (addition) | cause_right | has_cause |
| led to | `en.led_to` (led/leads/lead/leading [adv] to) | — | cause_left | has_cause |
| resulted in | `en.resulted_in`; `en.resulted_from` (addition, reverse) | — | left / right | has_cause |
| triggered | `en.triggered`, `en.trigger_base`, `en.triggered_by` | `fr.a_declenche`, `fr.declenche_par` | left / right | precipitated |
| sparked | `en.sparked`, `en.spark_base`, `en.sparked_by` | — | left / right | precipitated |
| precipitated | `en.precipitated`, `en.precipitate_base`, `en.precipitated_by` | — | left / right | precipitated |
| contributed to | `en.contributed_to` (+ adverb: "contributed greatly to") | `fr.a_contribue` | cause_left | contributing_factor |
| due to | `en.due_to` (not "due to be/begin/…" schedules) | `fr.est_due_a` (dû/due/dus/dues à) | cause_right | has_cause |
| because of | `en.because_of` | `fr.a_cause_de` | cause_right | has_cause |
| as a result of | `en.as_a_result_of` | `fr.consequence_de` (est la conséquence de, en conséquence de) | cause_right | has_cause |
| a provoqué | — | `fr.a_provoque` (compound + passé simple/présent/participe), `fr.provoque_par` | left / right | has_cause |
| a entraîné | — | `fr.a_entraine` (not reflexive *s'entraîner*; passive "entraîné par" = *trained by*, not matched) | cause_left | has_cause |
| en raison de | — | `fr.en_raison_de` | cause_right | has_cause |

Passives win over actives when two matches overlap. The relation hint is about the wording only.

### 2.2 Arguments (only the sentence's wikilinks)

* **Clause limits**: an argument never crosses `; : ( ) [ ]`, a spaced dash, or *but / while / whereas /
  although / however / because* (*mais / tandis / alors que / bien que / cependant / parce que*), nor another
  connective of the same sentence.
* **Left argument**: the nearest link before the connective, ≤ 60 characters away, no subject pronoun or
  demonstrative in between ("…, and this led to" → none), an even number of commas in between
  (appositive ", signed in 1919," allowed; "…, encountering little opposition due to" refused);
  relative bridges (", which led to", ", qui a provoqué") allowed. Prepositional chains are walked back to
  their head ("the [[assassination]] of [[Franz Ferdinand]] in [[Sarajevo]] triggered" → assassination).
  The argument must open its clause (≤ 4 words before it after an optional introductory phrase such as
  "In 1914," / "Pendant la guerre,"; no other link before it in the clause; no -ing word in English):
  "[[Germany]] invaded [[Poland]], which led to" yields nothing.
* **Effect on the left of a cause_right connective** must be adjacent: only a copula / auxiliary and adverbs
  between ("[[purpura]] due to", "X was largely due to"). "The [[Supreme Court]] annulled the result due to
  fraud" yields nothing (the effect is the annulment, which is not linked).
* **Right argument**: the first link after the connective, ≤ 50 characters away, with no comma / relative
  pronoun / infinitive ("caused the King **to** sign") / conjunction in between.
* **Coordination**: "[[A]] and [[B]] led to [[C]] and [[D]]" → 4 pairs (max 4 per connective); a bare-comma
  list counts only when closed by *and/or/et/ou*; coordinated links share the modifier status (§2.3).
* **Fronted** cause_right connectives ("Due to the [[A]], [[B]] rose") take the main clause's first link
  (≤ 30 characters after the top-level comma) as the effect (flag `fronted`).

### 2.3 Modifier arguments (dropped by default)

A link that only modifies an unlinked head noun does not denote the argument: "the **resignation** of Prime
Minister [[Neville Chamberlain]]", "the **collapse** of the [[Soviet Union]]", "[[supply chain]] **capacity
limitations**", "[[Germany]]**'s** defeat". Detected by (a) a content noun + preposition right before the
link (EN/FR), (b) in English, a following word that is not a preposition / conjunction / auxiliary /
connective / adverb / participle / capitalised word. Transparent heads keep the link (outbreak, start,
beginning, onset, eruption, escalation, launch; déclenchement, début, commencement, éclatement, escalade,
lancement). Such candidates are dropped at linking (`modifier_argument`); `keep_modifier_arguments=True`
keeps them with `modifier_head` set.

### 2.4 Negation / hedge flags (direction kept as written)

* `negated`: a negation cue in the connective's clause, ≤ 50 characters before it or ≤ 15 after
  (not, never, did not, no evidence, denied …; ne / n' / pas / jamais / aucun / sans … — "not only" and
  "non seulement" excluded).
* `hedged`: a hedge cue anywhere in the sentence (might, could, lowercase *may*, probably, allegedly,
  "historians argue/debate", "it is thought", debated / disputed / controversial / theory, "if";
  aurait / pourrait / semble / peut-être / sans doute / selon certains / certains historiens / débat /
  controverse / hypothèse / "si" …).
* `negation_hedge_flag` ∈ `none | negated | hedged | negated+hedged`; matched cues are stored.

## 3. Candidate record (`candidates.jsonl`)

`candidate_id` (sha256 of lang, revision, paragraph, sentence, connective position + id, both QIDs,
extractor version), `tier: T5`, `source_key: wikipedia.causal_text`, `extractor {id, version}`,
`cause_qid`, `effect_qid`, `relation_hint`, `direction_rule` (`<connective id>:<direction>`), `connective`
`{id, family, text, span, listed}`, `cause`/`effect` `{qid, link_target (as written), resolved_title,
redirected, anchor, span, modifier_head, anchor_matches_target}`, `negation_hedge_flag`, `flags` (cues,
`relative`, `coordinated_*`, `fronted`, `introductory_phrase`, `in_list`, `templates_dropped`), `sentence`
(REVA's rendering of the wikitext), `sentence_wikitext` (verbatim source slice), `source` `{wiki, lang,
article_title, article_qid, page_id, revision_id, revision_timestamp, section, paragraph, sentence_index,
url (…/w/index.php?oldid=<rev>), raw_sha256 (API response), retrieved_at}`, `rights` (CC BY-SA 4.0,
attribution with revision), `promotion_status: not_promoted`, `review_status: unreviewed`.

`anchor_matches_target = false` marks piped links whose displayed text shares no content word with the
target page ("[[Northern theater of the American Revolutionary War after Saratoga|raids by British-allied
tribes]]"): a reviewer hint, never a linking input.

RUNSECTION

## 6. Budgets, politeness, storage, safety

* Every Wikipedia request: `reserve_ingest` (key `wikipedia.causal_text`) → `metered` → `settle_metered`
  (`fetch.budgeted_session`), ledger in the throwaway DB `reva_causcand`. Until ATLASCORE merges
  `registry/atlas/budget_requests/causcand.yaml`, `--use-requested-budget` overlays exactly the requested
  limit in memory for keys the pinned policy lacks (policy version suffixed `+causcand_request`; the pinned
  file is untouched; once merged the pinned value wins).
* The live client is built by `reva.atlas.ingest._live_client` (SafeHttpClient restricted to the two hosts,
  `min_interval_seconds=10`, one at a time) and always wrapped by the meter. `fetch.py` contains no
  `SafeHttpClient(` string, so invariant 30's scanner does not see it: **request to the lead** — add
  `"src/reva/causal_candidates/fetch.py": ("reserve_ingest(", "metered(", "settle_metered(")` to `BUDGETED`
  in `tests/invariants/test_invariant_30_budgets.py` so the guarantee is checked, not only true.
* 429/503: wait Retry-After (≤ 120 s) and retry, at most 3 attempts per request (each attempt metered); then
  the run stops, keeps what is logged, and a later run resumes.
* Never the dev DB: `ensure_database` / `harvest_settings` refuse `reva`, `postgres` and non-`reva_*` names.
* Storage: no full article on disk; `sentences.jsonl` keeps prefiltered sentences (rendered + wikitext +
  links) for offline re-tuning; bundles keep only candidate sentences.

## 7. Requests to other agents

1. **ATLASCORE**: merge `registry/atlas/budget_requests/causcand.yaml` (new key `wikipedia.causal_text`,
   600/day). Until then `test_budget_keys_are_registered_sources_and_cover_every_connector` lists it with the
   other agents' pending keys.
2. **Lead**: add `fetch.py` to invariant 30's `BUDGETED` (above).
3. **CAUSCORE**: `kg.causal_candidate` has no column for the negation/hedge flag or the direction rule; both
   are in `selector` (`negation_hedge_flag`, `cues`, `direction_rule`, `connective`). If the candidate API
   should filter on them, a column or an indexed JSON path is needed. `promotion_status` is not a column in
   the migrated table either (derived from `kg.causal_review`?) — the interface says `not_promoted` until a
   review.
4. **CAUSUI**: show `sentence`, the article/revision link (`selector.url`), the connective, both anchors,
   `negation_hedge_flag` and `selector.cause/effect.anchor` next to the resolved labels; label every
   candidate "non sourcé — candidat (T5, extrait automatiquement de Wikipedia)".
5. **NLQA**: never read `kg.causal_candidate`.
6. **EVENTS / CAUSWD**: when event entities with sitelinks land in the KG, `seeds.py` can read them instead
   of the WDQS snapshot.

## 8. Run it

```
uv run python -m reva.causal_candidates seeds --out <scratch>/seeds.json                     # WDQS, budgeted
uv run python -m reva.causal_candidates harvest --seeds tests/fixtures/causal_candidates/event_seeds_2026-10-16.json \
    --top 250 --out <scratch>/run1 --use-requested-budget                                   # en/fr Wikipedia
uv run python -m reva.causal_candidates relink --out <scratch>/run1 [--offline]             # re-tuned rules
uv run python -m reva.causal_candidates sample --out <scratch>/run1 --n 50 --seed 20261016
uv run pytest tests/unit/test_causal_candidates.py tests/unit/test_causal_candidates_fetch.py
```
