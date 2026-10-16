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
   CAUSWD in progress); once they land, seeds should come from their KG entities (sitelinks), not WDQS.
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

## 2. Rules (extractor `reva.causal_candidates.extract` 1.1.0)

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
* **v1.1.0 additions** (from the evaluation, §5): a participial connective ("…, causing", "…, leading to",
  "provoquant", "entraînant"…) not directly after its link takes the whole preceding clause as cause → no
  candidate; "[[A]] and resulted in" (the link closes the previous verb phrase) → none; the left gap must be
  an optional comma-enclosed parenthetical plus at most three words; a renaming apposition ("[[X]], or more
  commonly the Y, triggered") → none; French "ce qui" (clausal antecedent) is not a relative bridge.
* **Right argument**: the first link after the connective, ≤ 50 characters away, with no comma / relative
  pronoun / infinitive ("caused the King **to** sign") / conjunction in between; v1.1.0: no gerund clause
  ("resulted in the British **occupying** [[Java]]") and, after *cause*, no infinitive after the link
  ("caused [[Long-Term Capital Management]] … **to collapse**": the effect is the collapse).
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

## 4. Live run (2026-10-16, extractor 1.1.0)

| step | numbers |
|---|---|
| seeds | WDQS snapshot: 388 items ≥ 40 sitelinks; top 250 events (all have EN and FR articles) → **500 articles** (250 EN + 250 FR) |
| fetch | harvest: 126 content + 13 resolution requests answered 200, plus 74 answered 429 and waited out (Retry-After 12–50 s) = **213 metered requests** (reserved 145, topped up, refused 0); relink: 3 more. 64.2 MB transferred, 62.7 M characters of wikitext read and discarded; ~75 min |
| stored | 15,245 prefiltered sentences (11,216 EN, 4,029 FR) — `sentences.jsonl` 15 MB, the only text kept |
| connective matches | ~8,140; 8,058 without two usable arguments: no link on one side 6,184 · effect not adjacent 522 · participial clause 410 · gap crosses clause 345 · not clause subject 226 · anaphoric subject 205 · gap too long 132 · coordinated VP 29 · others 5 |
| raw candidates | 82 (both arguments are wikilinks) |
| dropped at linking | 61 modifier arguments (§2.3); 0 unresolved / missing / disambiguation / self-loop after redirects |
| **candidates** | **21** (15 EN, 6 FR) from 18 articles, 20 distinct QID pairs; relation hints: has_cause 17, precipitated 3, contributing_factor 1; flags: 1 hedged, 0 negated |
| resolution | 607 link targets resolved (EN 434, FR 173) by the wikis' own API; 1 missing page |
| CAUSCORE | `insert_candidates` into the throwaway DB `reva_causcand_kg` (migrated to `0016_causal_ops`): load 1, **21 inserted**, 0 rejected; re-run: 0 inserted, 21 unchanged; 18 sentence bundles archived (224 kB); `kg.causal_claim` untouched (0 rows) |

Outputs (scratch, not in the repo): `candidates.jsonl` (the 21), `candidates_v1.0.0.jsonl` (the 40 of the
first rule set), `sentences.jsonl`, `fetch_log.jsonl`, `resolutions.jsonl`, `http_log.jsonl`, reports.

**Yield is deliberately low.** Wikipedia links an entity at its first mention only, so in ~76 % of
connective sentences one side has no wikilink; REVA does not fill it by name. Most of the rest are
rejected by position rules or because the link only modifies the argument. The candidate space is a
small, precise, fully traceable set; widening it needs a reviewed linking source, not looser rules.

## 5. Evaluation (hand-checked, honest)

Judged by the agent reading every sentence (labels and the sentence records are in
`tests/fixtures/causal_candidates/eval_v1.1.0.json`; `test_hand_labelled_metrics_match_the_published_notes`
pins the numbers). **direction** = cause→effect as the sentence writes it, given the two arguments;
**cause / effect link** = the QID denotes that argument (not a modifier, a place, a person standing for an
action, or a broader topic); **correct** = both links right.

| set | n | direction | cause link | effect link | correct |
|---|---:|---:|---:|---:|---:|
| v1.0.0, all candidates (census) | 40 | 40 (100 %) | 31 (78 %) | 30 (75 %) | **22 (55 %)** |
| v1.1.0 lenient (modifier arguments kept), random 50 of 82 (`random.Random(20261016)`) | 50 | 50 (100 %) | 31 (62 %) | 22 (44 %) | **15 (30 %)** |
| **v1.1.0 published (census)** | 21 | 21 (100 %) | 19 (90 %) | 19 (90 %) | **18 (86 %)** |

A random sample of 50 of the published set was not possible (only 40, then 21, candidates exist): those
sets were checked in full; the 50-item random sample was drawn from the larger lenient pool. Small n:
the 86 % has a wide interval (Wilson 95 %: 65–95 %).

Modifier filter (lenient sample): 38 of 50 carry a `modifier_head`; 5 of those 38 are correct (false
drops: participles "attempting", "struck", "compare", "coronaviruses causing", "victory at Kesselsdorf"),
33 are wrong. Of the 12 without one, 10 are correct. Dropping them trades ~1/3 of the correct candidates
for precision 30 % → 83 % on that sample.

Negation/hedge flags: the one hedged published candidate is right ("may be caused by"); a French false
hedge ("contestation" = protest, matched by `contest\w*`) was fixed in 1.1.0. No negated candidate
occurred in the run (the rules are covered by unit tests only).

### Confusion notes (what goes wrong, by cause)

* **Direction: never wrong** in 111 judged items. Passives, *due to / à cause de / en raison de / est due à*
  and fronted forms are read the right way round. Direction errors would need a mis-parsed sentence, not
  a mis-read connective.
* **Modifier links** (dominant before the filter): "led to the resignation of [[Neville Chamberlain]]",
  "the collapse of the Russian Empire during the [[Russian Revolution]]", "[[Rafik Hariri]]'s
  assassination", "an uprising led by [[Judas of Galilee]]", "protests in [[Jordan]]". Fixed by the
  modifier rule (§2.3) — now case-insensitive heads, quotes, *led/ruled/commanded by*, *lors de* (v1.1.0).
* **Wrong subject**: an object or a place before the connective taken as the cause ("[[Germany]] invaded
  [[Poland]], which led to", "Acute food shortages reached Moscow and [[Saint Petersburg]] and contributed
  to", "Armenian control … caused the [[Khojaly massacre]] and resulted in", "la Fed abaisse son [[taux
  directeur]] …, provoquant"). Fixed in 1.1.0 by the participle, VP-coordination and left-gap-shape rules.
* **Causative/infinitive and gerund objects**: "caused [[the United States]] to send", "caused [[LTCM]] …
  to collapse", "resulted in the British occupying [[Java]]". Fixed in 1.1.0.
* **Relative "which" with a clausal or distant antecedent** ("an armed conflict sparked by …, which resulted
  in the independence of [[Bangladesh]]"): still possible; mitigated by the subject rule, not solved.
* **Generic concept links for specific events** (remaining error): "un [[attentat]] contre le sanctuaire
  al-Askari" → Q2223653 *terrorist attack* (class), "la [[marée noire]] due à …" → the generic article.
  Detecting it needs entity classes (P31) — a candidate for a later rule using KG snapshots, never names.
* **Piped links to broader articles** (remaining error): "the loss of … [[Territorial evolution of the
  Ottoman Empire|European territory]]", "[[Slogans de Mai 68|une violente contestation …]]". The
  `anchor_matches_target` hint catches some (and misfires on "Persian" → Iranian nationalism); kept as a
  reviewer hint only.
* **Agentive causes** ("l'[[Italie]] déclencha la guerre italo-turque"): judged correct as written (a state
  as agent); CAUSUI should show the sentence, not only the pair.
* **Duplicates across articles**: the same sentence in two articles (FR Gaza war / genocide articles)
  gives two candidates (different revisions) — correct, but the UI should group by sentence hash
  (`sentence_sha256`).
* **Rendering**: sentences start mid-markup when a split falls inside parentheses (")" first character);
  unknown templates are dropped and counted (`templates_dropped`); `{{spaces}}`-like templates now render
  as spaces.

## 6. Budgets, politeness, storage, safety

* Every Wikipedia request: `reserve_ingest` (key `wikipedia.causal_text`) → `metered` → `settle_metered`
  (`fetch.budgeted_session`), ledger in the throwaway DB `reva_causcand`. ATLASCORE merged the request
  during the run (`budgets-1.3.8`: `wikipedia.causal_text: 600`); the harvest itself ran under
  `--use-requested-budget`, which overlays exactly the requested limit in memory only for keys the pinned
  policy lacks (policy version suffixed `+causcand_request`; the pinned value wins once merged).
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

1. **ATLASCORE**: done (`budgets-1.3.8`); invariant 30 tests pass.
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
