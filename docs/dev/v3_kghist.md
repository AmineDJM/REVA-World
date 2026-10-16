# V3 KGHIST — world history as a connected graph

Agent KGHIST, 2026-10-15/16. Owns `registry/kg/queries/history/**`, `registry/kg/anchor_chains.yaml`,
`registry/kg/property_requests/KGHIST.yaml`, `tests/unit/test_kg_queries_history.py` and this note.
No commits. The dev database `reva` was never touched. All Wikidata traffic went through WDHARVEST's locked
client: first `build_client`, then `harvest.budgeted_client` once WDHARVEST made it the only entry point.

## 1. What was delivered

| Artifact | Content |
|---|---|
| `registry/kg/queries/history/*.yaml` | 16 query specs (11 `seed_entities`, 5 `statements`). All pass `python -m reva.kg.harvest validate`. Total declared bound: 209,000 rows. |
| `registry/kg/anchor_chains.yaml` | 20 anchor chains with typed hop constraints, waypoints and 80 expected facts. These are the acceptance tests for the path engine. Also holds 104 entities whose QIDs were looked up, never typed from memory. |
| `registry/kg/queries/history/_recorded/` | Recorded Wikidata evidence: 1 WDQS label lookup (raw), 2 WDQS count queries (raw), 3 `wbgetentities` title lookups (raw), and 4 claim snapshots for 266 entities. The snapshots are reduced to history properties, keep their statement ids and revision ids, and their raw responses (23.7 MB) are archived in `var/kg/objects`. |
| `registry/kg/property_requests/KGHIST.yaml` | P945 (allegiance) and P137 (operator). WDHARVEST has merged both into the allow-list. |
| `tests/unit/test_kg_queries_history.py` | 34 offline tests (see §5). |

## 2. How Wikidata models colonial history (findings, with evidence)

All examples below come from the recorded snapshots.

* **There is no "colony of" property.** The colonial power is carried by:
  * `P17` country with `P580`/`P582` qualifiers, e.g. French Algeria (Q218272) → France, 1830–1962-07-05;
  * `P361` part of with dates, e.g. Colonial Brazil (Q2088324) → Portuguese Empire 1500–1822, then → United Kingdom of Portugal, Brazil and the Algarves 1815–1822;
  * the empire's own `P527` has part, e.g. Portuguese Empire (Q200464) → Colonial Brazil, Portuguese India, Portuguese Angola.

  Colonies are typed `P31 colony (Q133156)`, often with dated qualifiers (British Raj 1858–1947). The specs therefore seed colonies by class (`history.colonies`) **and** by their link to an empire (`history.empire_territories`). Their snapshots keep the dated P17/P361/P527 claims.
* **Pitfall: `wdt:P31` hides empires.** Many historical states give *preferred* rank to "historical country" (Q3024240). The truthy `wdt:P31` then drops their "empire" value. This affects the Spanish Empire, Russian Empire, Aztec Empire, Empire of Brazil and Portuguese Empire. A first WDQS count using `wdt:P31/wdt:P279* wd:Q48349` returned **27 empires**. Every class test in the KGHIST specs now walks all non-deprecated P31 statements instead: `?x p:P31 ?s . ?s ps:P31/wdt:P279* <class> ; wikibase:rank ?r . FILTER(?r != DeprecatedRank)`. A test enforces this. **Other domains' specs that use `wdt:P31/wdt:P279*` on historical items will under-select in the same way. For example, `core.historical_states` and `culture.historical_state_languages` should be checked.**
* **Succession** uses `P1365` replaces / `P1366` replaced by, often stated on one side only. Example: Belgian Congo has no P1366, and the DRC lists only Zaire under P1365. `P155`/`P156` also appear on states. Statements can be dated (`P585` on the P1366 of Colonial Brazil to the State of Brazil, 1621). `history.country_predecessors` follows `(P1365|^P1366)+` from the 250 countries. `history.state_succession` keeps both directions as explicit statements.
* **Independence** appears as:
  * the successor's `P571` inception (Brazil 1822-09-07, Indonesia 1945-08-17, Algeria 1962-07-05, Angola 1975-11-11, DRC 1960-06-30);
  * the dissolution (`P576`) of the colony;
  * an event item, e.g. "Independence of Brazil" (Q1548600, P585 1822-09-07), reached through a country's or an empire's `P793` significant event.

  The Portuguese Empire's P793 includes the Treaty of Tordesillas, the discovery of the sea route to India, the Discovery of Brazil and the Independence of Brazil, which is why `history.empire_event_items` exists.
* **Explorers' voyages** hang mainly off the person's `P793`. Columbus → his first to fourth voyages, which are `P527` parts of "Voyages of Christopher Columbus", an expedition. Sometimes the link is `P1344`. The voyage's own `P710` list usually names the crew, not the leader. Columbus's tie to Castile is `P945` allegiance → Crown of Castile, so P945 was requested and merged. `history.explorer_voyages` collects person→voyage statements through P1344 and P793.
* **Wars and treaties:**
  * wars carry `P710` participants, P580/P582, P276, P361 (e.g. Algerian War → Cold War and → Decolonisation of Africa), and sometimes P1542/P828;
  * treaties carry `P1891` signatories, which are often *persons*: the signatories of Tordesillas are rulers, not states. The parties appear as `P17` (Tordesillas → Kingdom of Portugal, Spanish Empire) or `P710` (Treaty of Paris 1783 → USA, Kingdom of France, Spanish Empire, Dutch Republic, Great Britain).
* **Data caveats (community-edited, shown as such, never corrected by REVA):**
  * "Discovery of Brazil" (Q3042917) has `P17 France`;
  * Nigeria's `P571` is 1963-10-01 (the republic). The 1960 independence is the `P571` of the Federation of Nigeria (Q5440850) and the `P576` of Colonial Nigeria;
  * Hernán Cortés has `P27` Spain/Mexico/New Spain;
  * the Suez Crisis lists Israel, the UK, France and Egypt as participants, not the USA.

## 3. Query specs and expected sizes

The WDQS sizes come from two recorded COUNT queries (`_recorded/wdqs_counts_1.*`, `wdqs_counts_2.*`). Count 1 used the truthy pattern, so its values are **lower bounds**; count 2 is rank-aware. If count 2 is missing, it did not complete in this session. "unmeasured" means WDQS was too throttled to measure (see §6). Bounds are set with margin. A spec that exceeds its bound fails with `row_bound_exceeded`; nothing is truncated.

| Spec | Purpose | Selection | Split | Measured | `max_rows` |
|---|---|---|---|---|---|
| `history.empires` | seed | P31 (any rank)/P279* empire Q48349 | — | 27 truthy (rank-hidden, see §2) | 5,000 |
| `history.colonies` | seed | colony Q133156, protectorate Q164142, dependent territory Q161243 | qid_prefix | 16,840 truthy | 30,000 |
| `history.empire_territories` | seed | historical state, colony etc. that is P361/P17 of an empire, or P527/P150 of one | — | unmeasured | 12,000 |
| `history.country_predecessors` | seed | `(P1365\|^P1366)+` and `P155\|^P156` from the 250 countries; polities only | values (countries, 25) | unmeasured | 8,000 |
| `history.country_event_items` | seed | P793 objects of the countries | values (countries) | unmeasured | 6,000 |
| `history.empire_event_items` | seed | P793 objects of seeded empires | values (spec:history.empires) | unmeasured | 5,000 |
| `history.wars` | seed | war Q198 closure, by year of P580/P585/P571, plus undated | year (−3000..2029, 500) | 2,755 truthy | 12,000 |
| `history.treaties` | seed | treaty Q131569 closure with a P1891 or ≥ 2 sitelinks | qid_prefix | 8,127 before filter | 15,000 |
| `history.expeditions` | seed | expedition Q2401485 closure | — | 2,071 truthy | 6,000 |
| `history.revolutions` | seed | revolution Q10931 closure | — | 221 truthy | 2,000 |
| `history.eras` | seed | historical period Q11514315 closure with ≥ 5 sitelinks | qid_prefix | 4,297 before filter | 8,000 |
| `history.country_events` | statements | country → P793 event (+P580/P582/P585) | values (countries) | unmeasured | 6,000 |
| `history.state_succession` | statements | P1365/P1366/P155/P156 of polities | qid_prefix (?state) | unmeasured | 60,000 |
| `history.explorer_voyages` | statements | person → P1344\|P793 → expedition | — | unmeasured | 30,000 |
| `history.country_discoverers` | statements | country → P61 discoverer | values (countries) | unmeasured | 1,000 |
| `history.decolonisation_successions` | statements | countries with P571 in 1940–1979 → P1365 colony, protectorate etc. | — | unmeasured | 3,000 |

Notes on the specs:

* The six class specs also carry `api_search` blocks, so WDHARVEST can run them with `run --via api` while WDQS is throttled. For treaties, the API route can only express "has a signatory", which is narrower.
* `core.historical_states` (WDHARVEST) runs after `history.empires` and `history.colonies`.
* Expected seeded entities, before de-duplication: about 25–40k, plus about 100k statement rows. The full history harvest is one bounded job of at most 209k rows.

**Recommended run order:** `empires` → `colonies` → `empire_territories`, `empire_event_items` → `country_predecessors`, `country_event_items` → the class specs → the statement specs.

## 4. Anchor chains (`registry/kg/anchor_chains.yaml`)

The file defines constraints, not paths. A chain passes when the engine returns a simple path from `from` to `to` that satisfies all of these:

* at most `max_hops` hops;
* its hops match the ordered `steps`. Each step is a set of hop classes, e.g. `territorial`, `state_succession`, `official_language`, `treaty_signatory`, `participation`, `leadership`, `asserted_causation`;
* it passes `via_all` and at least one of `via_any`, which are owner-named waypoints such as the Louisiana Purchase;
* it uses only `allowed_classes`, avoids `exclude` (present-day `modern_relations`: P530, P47, P463) and the global `excluded_properties` (P31, P279 and other meta properties);
* it never **passes through** a language, continent or era. Objects of P37/P2936/P30/P2348 may only end a path, so "both speak French" is not an explanation.

Every hop must cite its statement, and the wording follows the "connected via" rule. P828/P1542 hops read "asserted by Wikidata".

| # | Chain | Expectation | Shortest admissible witness in the recorded claims |
|---|---|---|---|
| 1 | Columbus ↔ Portuguese (Brazil), via Tordesillas | expected | Columbus –allegiance→ Crown of Castile ←replaces– Spanish Empire ←country– Treaty of Tordesillas –country→ Kingdom of Portugal –replaces→ United Kingdom of Portugal, Brazil and the Algarves –language used→ Portuguese |
| 2 | Tordesillas ↔ Brazil | expected | Tordesillas –country→ Kingdom of Portugal ←country– Portuguese Empire ←country– Brazil |
| 3 | Cabral ↔ Brazil | expected | Brazil –discoverer→ Cabral |
| 4 | Napoleon ↔ Louisiana Purchase ↔ USA | expected | Napoleon –conflict→ French Revolutionary Wars –participant→ Kingdom of France ←country– Louisiana (New France) ←replaces– Louisiana Purchase –country→ USA |
| 5 | Berlin Conference ↔ DR Congo | **may be absent** | none: the Belgian Congo → DRC succession is not stated in Wikidata |
| 6 | Suez Crisis 1956 ↔ USA ↔ decolonisation (of Africa) | **may be absent** | none: the US is not a recorded participant; needs UNGA votes (KGGEOPOL) |
| 7 | Spanish Empire ↔ Spanish in Mexico | expected | New Spain –located in→ Spanish Empire; Mexico –replaces→ New Spain; Mexico –language used→ Spanish |
| 8 | British Empire ↔ Pakistan | expected | British Raj –country→ British Empire; Pakistan –replaces→ British Raj |
| 9 | France ↔ Algeria | expected | Évian Accords –country→ France; Algeria –significant event→ Évian Accords |
| 10 | Dutch Empire ↔ Indonesia | expected | Dutch East Indies –part of→ Dutch Empire; Indonesia –replaces→ Dutch East Indies |
| 11 | Carnation Revolution ↔ Angola | expected | Carnation Revolution –country→ Portugal ←country– Portuguese Colonial War –country→ Angola |
| 12 | Vasco da Gama ↔ India | expected | da Gama –citizenship→ Kingdom of Portugal ←located in– Portuguese India –part of→ India |
| 13 | Magellan ↔ Philippines | expected | Magellan –allegiance→ Spanish Empire ←country– Captaincy General of the Philippines ←replaces– First Philippine Republic –country→ Philippines |
| 14 | France ↔ US independence | expected | Treaty of Paris (1783) –country→ France; –participant→ USA |
| 15 | Haitian Revolution ↔ France | expected | Haitian Revolution –country→ Saint-Domingue –country→ France |
| 16 | Ottoman Empire ↔ Turkey | expected | Turkey –replaces→ Ottoman Empire |
| 17 | Russian Empire ↔ Russia (via USSR/RSFSR) | expected | Russian Empire –replaced by→ Q139319 (no claims recorded) ←replaces– RSFSR ←replaces– Russia |
| 18 | Bolívar ↔ Venezuela | expected | Bolívar –citizenship→ Gran Colombia ←replaces– Venezuela |
| 19 | Cortés ↔ Mexico | expected | Cortés –citizenship→ New Spain ←replaces– Mexico |
| 20 | UK ↔ English in Nigeria | expected | Treaty of Lausanne –signatory→ UK, –participant→ British Empire ←country– Federation of Nigeria ←replaces– Nigeria –language used→ English |

How to read the witnesses:

* They are found offline by `witness_path()` in the test: a shortest admissible path, preferring succession, sovereignty, signatory and participation hops.
* They show that each chain is satisfiable from sourced statements. They are **not** the required answer, and not always the most natural one. In chain 20, for example, the UK ↔ British Empire link goes through a co-signed treaty because the recorded snapshot holds no direct UK–British Empire statement (the British Empire's P17 is the Kingdom of Great Britain and the United Kingdom of Great Britain and Ireland).
* The full KG has far more entities, so the engine may find other admissible paths. Each chain also lists `expected_facts` that the explanation should be able to cite, e.g. Brazil P571 1822, Tordesillas P585 1494, Louisiana Purchase P585 1803, Pakistan P571 1947, Angola P571 1975.

**Request to KGCORE.** The current path engine returns minimal-length paths with `max_hops` ≤ 5, P31/P279 excluded and a hub limit. To pass these chains it needs:

1. a property or class allow/deny list per query;
2. waypoints (`via`);
3. "non-transit" entity kinds (languages, continents, eras);
4. ordered hop-class constraints, or k-shortest paths (k ≥ 10) filtered by them;
5. `max_hops` up to 8. Chain 1 needs 6 hops; chains 4, 13 and 20 need 4–5.

Without (3), the shortest Columbus ↔ Portuguese path is a trivial shared-language or `languages spoken` hop.

## 5. Tests (`tests/unit/test_kg_queries_history.py`, offline, 34 tests)

* Every spec validates against WDHARVEST's schema and allow-list, is owned by KGHIST, and stays bounded (≤ 150k per spec, ≤ 400k total). Statement specs keep the statement id and filter deprecated rows. Class specs use rank-aware P31.
* Recording integrity: the sha256 of every committed raw body, the query text against its sha256, and snapshot provenance (archive sha256, `lastrevid`, statement-id prefix, rank).
* **Anchor reachability.** The selection rule of every spec is re-applied offline to the recorded claims: class closure via P31/P279, the country fan-out, the succession closure, empire events, explorer voyages and the decolonisation filter. Each anchor is then reachable either as a selected entity or as the object of an allow-listed claim of a seeded entity:
  * Columbus (Q7322): explorer_voyages;
  * Tordesillas (Q180897): treaties and empire_event_items;
  * Brazil (Q155), Portugal (Q45), Spain (Q29), USA (Q30): country_events;
  * Portuguese (Q5146): P37 of predecessors;
  * Cabral (Q174432): country_discoverers.

  The test also checks that key entities are selected by the intended specs.
* Every chain entity's QID matches a recorded enwiki-title lookup or WDQS label lookup.
* Each `path_expected` chain has a witness. Each `path_may_be_absent` chain has none, so its honesty label stays true.
* All 80 expected facts are recorded statements.

QID corrections found by lookup:

* Cabral is **Q174432**, not Q193066.
* Q217230 is the **Empire of Brazil**, not Portuguese/colonial Brazil. Colonial Brazil is Q2088324 and the State of Brazil is Q11876909.
* Tordesillas is Q180897.

## 6. Limits and honest notes

* **WDQS was heavily throttled** (machine-wide 429s with 900–1,000 s Retry-After) throughout. KGHIST obtained 3 WDQS responses: the label lookup and two counts. A count query waited 34 minutes in the lock queue. Membership and hop evidence therefore comes from `wbgetentities` snapshots (Wikibase API) plus an offline re-application of each spec's selection rule, not from LIMITed runs of each spec. The specs' SPARQL has passed validation but has **not** been executed in full. Counts for the statement specs and the fan-out specs are unmeasured.
* One exploratory label lookup (`label_lookup_1`) and one early probe went through the pre-budget `build_client`. The probe was cancelled before it ran, after WDHARVEST introduced `budgeted_client`. All later traffic was metered.
* The snapshots keep all non-deprecated ranks, matching the rank-aware specs. Values are compacted: entity id, time string, amount or text.
* The scratch helpers (recorder, path-search prototype, count script) live in the session scratchpad, not in the repo.
