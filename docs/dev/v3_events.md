# V3 EVENTS — the event backbone causal claims attach to

Agent EVENTS, 2026-10-16. Spec: `docs/roadmap/v3_causality.md` (section *Model → Event*). Interface consumed:
KGCORE (`docs/dev/v3_kgcore.md`), WDHARVEST (`docs/dev/v3_wdharvest.md`), CAUSCORE
(`docs/dev/v3_causal_interface.md`). Nothing here asserts causation: participation, place, time, part-of and
sequence are **context**; causal statements are only listed as *asserted by* their source.

## 1. What exists

| Piece | Path |
|---|---|
| Taxonomy (73 types fr/en, 16 roles, property roles, 13 dataset kinds, 1 derived kind) | `registry/events/taxonomy.yaml` |
| Recorded Wikidata verification (106 entities: labels en/fr, P31/P279 claims with rank and statement id, property datatypes, `lastrevid`, raw sha256) | `registry/events/_recorded/wikidata_verification.json` |
| `same_as` evidence rules | `registry/events/same_as_rules.yaml` |
| Taxonomy loader/validator | `src/reva/events/taxonomy.py` |
| Event view (read-only Python/SQL API) | `src/reva/events/view.py` |
| Evidence-based `same_as` (derive, load, retract) | `src/reva/events/same_as.py` |
| Budget-metered Wikidata verification + API-mode spec probe | `src/reva/events/verify.py` (`python -m reva.events.verify`) |
| Throwaway-DB demonstration | `src/reva/events/demo.py` (`python -m reva.events.demo`) |
| HTTP routes (`read` scope) + 1 router line in `src/reva/api/app.py` | `src/reva/api/routes/events.py` |
| Harvester specs (11, API-mode capable) | `registry/kg/queries/events/{political,society,violence,diplomacy}/*.yaml` |
| Property requests to WDHARVEST | `registry/kg/property_requests/events.yaml` |
| Tests | `tests/unit/events/test_events_{taxonomy,specs}.py` (17, offline), `tests/integration/test_events_view.py` (8, db), `tests/integration/test_events_datasets.py` (6, db, fixture ingests) |
| Contract | `contracts/openapi.json` regenerated (`/v1/events/{event_id}`, `/v1/events/search`, `/v1/events/types`) |

No migration: the view reads `kg.statement_current` / `kg.entity_current`; `same_as` links are ordinary KG
statements (`events:same_as`, source `reva.events.same_as`) written with `reva.kg.model.load_batch`.

## 2. Taxonomy

```
event [Q1190554 occurrence]
├─ armed_conflict [Q350604] ─ war [Q198] ─ civil_war [Q8465] · war_of_independence [Q21994376] · world_war [Q103495]
│                            · battle [Q178561] ─ siege [Q188055]
│                            · military_operation [Q645883] ─ military_expedition [Q1384277] · invasion [Q467011]
│                            · military_occupation [Q188686]
│                            · interstate / intrastate / extrasystemic_armed_conflict (UCDP coding) · armed_conflict_episode (UCDP)
├─ genocide [Q41397] · massacre [Q3199915] · terrorist_attack [Q2223653]
├─ revolution [Q10931] · coup [Q45382] · assassination [Q3882219] · rebellion [Q124734]
├─ protest [Q273120] ─ demonstration [Q175331] · riot [Q124757] · strike [Q49776] · resistance_campaign (NAVCO)
├─ election [Q40231] · referendum [Q43109] · reform [Q900406]
├─ independence ─ declaration_of_independence [Q1464916]   (+ CShapes status changes, derived)
├─ annexation [Q194465] · partition [Q518554] · transfer_of_sovereignty [Q25929224] · decolonization [Q230533]
├─ crisis [Q381072] ─ international_crisis (ICB) ─ foreign_policy_crisis
│                    · economic_crisis [Q290178] ─ financial_crisis [Q114380] ─ stock_market_crash [Q1020018]
│                                               · recession [Q176494] · depression [Q1814941] · oil_crisis [Q123193388] · hyperinflation [Q185565]
├─ crisis_trigger / crisis_response (ICB) · militarized_interstate_dispute ─ militarized_incident (COW MID)
├─ state_repression · state_response (contention datasets) · peacekeeping_mission (UN) · sanction_episode
├─ migration_wave [Q177626] · treaty [Q131569] ─ peace_treaty [Q625298] · peace_agreement (UCDP)
├─ conference [Q18564543] · economic_bubble [Q207781]
├─ disaster [Q8065] ─ earthquake [Q7944] · tsunami [Q8070] · volcanic_eruption [Q7692360] · flood [Q8068] · tropical_cyclone [Q8092]
├─ famine [Q168247] · disease_outbreak [Q3241045] ─ epidemic [Q44512] ─ pandemic [Q12184]
└─ expedition [Q2401485] · discovery [Q12772819]
```

Every type has fr/en labels and typical participant roles (`participant`, `actor`, `belligerent`,
`supporting_party`, `target`, `victim`, `perpetrator`, `organizer`, `operator`, `signatory`, `party`,
`former_party`, `candidate`, `winner`, `contributor`, `former_ruler`). A class maps to one type only. Types without
a Wikidata class are dataset types (reached only through `dataset_kinds`) or have no verified class
(`independence` itself, `sanction_episode`).

**Verification (no QID typed from memory).** `python -m reva.events.verify` fetched every candidate class and
property through WDHARVEST's locked client, budget-metered on `wikidata.wbapi` (`wbapi_client`: same lock,
pacing, archive and User-Agent as `budgeted_client`, but it reserves only the API key — the shared WDQS budget was
spent, 2,374/1,500, and reserving it blocked API-only work). The action API answered 429 for minutes at a time
(13 attempts exhausted twice), so snapshots came from `Special:EntityData` (one entity per request, archive reuse
72 h) and title lookups from `Special:ItemByTitle`. Records are compact (labels, non-deprecated P31/P279 with rank and
statement id, datatype, `lastrevid`); raw bodies stay in `var/kg/objects`. Findings:

* guesses rejected by the record: Q1752346 is *assembly* (not self-coup), Q3318733 *independentism* (an ideology),
  Q59858687 *civic holiday*, Q37055 *independence* is a concept (P279 sovereignty-like), so `independence` has no
  class and declarations use Q1464916;
* Q1384277 is *military expedition* (Suez Crisis's P31), subclass of both military operation and expedition;
* Q40231 is labelled *public election*; Q3882219 *assassination* (fr *magnicide*); Q177626 *human migration* is a
  general process class (instances mix named waves and concepts — the card shows the chain).

Unit tests refuse a taxonomy class without a record, check that each record's English label shares a word with
its type, and that every Wikidata property used has a recorded datatype.

## 3. The event view (`reva.events.view`)

| Function | What |
|---|---|
| `classify(conn, id)` | types with evidence; `primary`/`secondary`; `p31_unmatched` |
| `event_card(conn, id)` | full card (below), `None` when not an event |
| `list_events(conn, types=, start=, end=, country=, involving=, include_undated=, limit<=200, cursor=)` | bounded listing, keyset cursor |
| `descendant_classes`, `class_graph` | bounded class closures (depth ≤ 8, ≤ 600 nodes upward, ≤ 20,000 downward, truncation flagged) |
| `cshapes_independence_events(conn)` | derived independence events |

**Classification.** All non-deprecated `P31` statements of the item (the KG never stores deprecated rank; truthy is
never used). Each P31 class is followed up `P279` — current KG statements first; recorded verification claims only
for classes with no `P279` in the KG (`via: kg | recorded`, with statement key or revision + raw sha). A path stops
at the first class the taxonomy maps (Suez's *military expedition* is not also an *expedition*); `most_specific`
drops ancestors. If any P31 claim has preferred rank, the types reached from preferred claims are `primary` and the
others `secondary` — nothing is dropped. Listing uses the same rule in reverse (it does not descend into a class
mapped to another type), so a card and a listing agree. Dataset records are typed by `dataset_kinds`: by id prefix
(`ucdp:conflict:`, `cow:war:`, `unpk:mission:`, `untc:treaty:`, `ptcoup:`, `ncei_hazards:{earthquake,tsunami,eruption}:`,
`ties:case:`, `eusanct:case:`), or by a marker statement (`untc:mtdsg_number` from `untc.mtdsg` for QID-keyed
treaties; `causdata:event_type` and `contention:event_type`, whose coded values map to types — coded outcomes,
terminations and state codes map to *not an event*). UCDP's `ucdp:conflict_type` refines the type
(interstate / intrastate / extrasystemic).

**Time interval.** Roles: P580 start, P582 end, P585 point in time, P571 inception, P576 dissolved, dataset dates
(`ucdp:start_date`, `ucdp:episode_end`, `unpk:start_date`, `unpk:end_date`). Preferred rank first, then the
earliest start / latest end; every other stated value stays in `alternatives` (`conflicting_values`). Bounds come
from KGCORE's precision-derived dates (`earliest`/`latest`, astronomical ISO, BCE-safe). `consistency` =
`inconsistent` when start > end (shown, never corrected); a point in time outside start..end is noted. COW wars have
no war-level dates in the KG: the interval is **derived from the participation spells** and labelled so.

**Places** P276 location, P17 country, P131 admin area, `ucdp:location`, `unpk:mission_country`, `unpk:hq_location`.
**Participants** grouped per (participant, role, side, property): P710, P1344 (inverse), P607 (inverse, belligerent),
P664, P137, P1346, P1891 (point in time = signature), `untc:party`/`untc:former_party`, UCDP primary/secondary/
non-state sides with UCDP's side `a`/`b`, COW P1344 spells (source-specific role *belligerent*, side `1`/`2`, coded
initiator/outcome verbatim), `unpk:contributed_personnel_to` (inverse, contributor). Role qualifiers P2868/P3831 are
shown. **Structure** P361/P527/P155/P156/P793 in both directions (an inverse is cited on the entity that states it).
**Causal statements** P828/P1542/P1478/P1479/P1536/P1537 both directions, worded "asserted by <source> (statement …),
not established by REVA"; graded claims are CAUSCORE's (`/v1/causal/events/{id}`). Every item carries its statement
key, source, revision, raw sha256, rank, origin and verification label. Lists are bounded (2,000 statements per
direction, 500 participants, 10 statements per participant) with `truncated`.

**API** (`read` scope, envelope as `/v1/kg`, `origin` filter, warnings `context_not_causal`, `community_edited`,
`derived_event`, `fixture_data`, `truncated`):

| Route | |
|---|---|
| `GET /v1/events/{id}` | event card; 404 when unknown or not an event; 422 invalid id |
| `GET /v1/events/search?type=&from=&to=&country=&involving=&include_undated=&limit=&cursor=` | listing (`country` = ISO3 or QID of a place; `involving` adds participant matches) |
| `GET /v1/events/types` | the taxonomy |

The bare `GET /v1/events` already belongs to the V1 world-occurrence listing (`routes/world.py`, `world.occurrence`),
so the KG listing is `/v1/events/search` (the brief's `/v1/events?type&from&to&country`).

## 4. Dataset events in the view

| Source (agent) | Records | Type | Dates | Participants / places |
|---|---|---|---|---|
| UCDP ACD (KGGEOPOL) | `ucdp:conflict:<id>` | armed_conflict → UCDP subtype | `ucdp:start_date` (first battle death), `ucdp:episode_end` | primary/secondary/non-state parties with sides; `ucdp:location` |
| COW wars v4.0 (A-HISTECON) | `cow:war:<n>` | war | derived from P1344 spells | `cow:<ccode>` belligerents, side, initiator, outcome |
| UN peace operations (A-MILITARY) | `unpk:mission:<acronym>` | peacekeeping_mission | start/end | contributors (country → mission per year), mission country, HQ |
| UNTC (A-TREATIES) | treaty QIDs (marker `untc:mtdsg_number`) or `untc:treaty:<mtdsg>` | treaty | — (UNTC gives none) | signatories (dated), parties (consent date), former parties |
| CShapes 2.0 (KGGEOPOL) | derived `cshapes:independence:<gw>:<date>` | independence | start of the first `independent` period after another status | former ruling state (`cshapes:ruled_by`), territory + ISO3 |
| CAUSDATA, A-COUPS, A-HAZARDS, A-SANCTIONS, A-CONTENTION | as documented by those agents | mapped kinds (§3) | P580/P582/P585 | their P710 / dataset predicates where taxonomy maps them |
| Cliopatria (KGPOLITY) | polities | **not events** | — | polities are rulers/places, not occurrences; nothing derived |

CShapes changes are derived by REVA (`reva.events.cshapes_status_change/1`, inputs = the two period statements) and
the card says so (`derived`, warning `derived_event`). Only dependency → independent transitions are derived; border
changes and owner changes are not events here.

## 5. `same_as` (evidence only)

Rules (`registry/events/same_as_rules.yaml`): **active** — `untc_mtdsg_number` (two records sharing UNTC's MTDSG
number) and the evidenced mapping table `untc_treaty_qids` (A-TREATIES' `registry/entities/treaty_qids.json`,
entries `status: verified`: item label/alias equals the UNTC title, revision + raw sha cited). **Inactive, with the
evidence why**: `ucdp_conflict_id_on_wikidata`, `cow_war_number_on_wikidata`, `unpk_mission_on_wikidata` — no
Wikidata property holds these ids: the suggested P7166 is *Acharts.co song ID*; the small numeric identifiers on
archived war items are P7695 *e-WV West Virginia Encyclopedia ID*, P8472 *Encyclopédie berbère keyword ID*, P13956
*Encyclopedia of African American History entry ID*; none of the external-id claims on the Algerian War (UCDP 246),
Suez Crisis and 10 other archived war items carries a UCDP/COW key. COW ↔ UCDP share no identifier either. So **no
cross-dataset war link is made**; acronym-, name- or date-matching would be fuzzy matching.

A value held by more than two records is reported `ambiguous`, never linked. Links are `events:same_as` statements
(symmetric, subject < object), qualifiers `evidence`, `rule`, `derivation: evidence_linked_identity`,
`method: reva.events.same_as/1` (verification label `derived`); the raw artifact is the canonical evidence document;
a run retracts links whose evidence disappeared (`removed_at_source`). Tested: links from both rules, idempotent
reload, card display, retraction.

## 6. Harvester specs (`registry/kg/queries/events/`, domain `history`, ids `history.events.*`)

| Spec | Root classes (api_search depth) | Bound |
|---|---|---|
| coups | Q45382 (2) | sitelinks ≥ 3, 3,000 |
| assassinations | Q3882219 (2) | ≥ 3, 4,000 |
| independence_declarations | Q1464916 (2) | ≥ 2, 2,000 |
| national_elections | Q1076105, Q858439, Q43109 (1, `haswbstatement:P17`) | P17 + ≥ 8 sitelinks, 15,000 |
| reforms | Q900406 (3) | ≥ 2, 3,000 |
| political_crises | Q381072 (0; SPARQL excludes the economic-crisis branch) | ≥ 3, 3,000 |
| transfers_of_sovereignty | Q25929224, Q194465, Q518554 (1) | ≥ 2, 4,000 |
| protests | Q273120, Q175331, Q124734 (2) | ≥ 3, 12,000 |
| migration_waves | Q177626 (2) | ≥ 2, 3,000 |
| mass_violence | Q41397, Q3199915, Q2223653 (1) | ≥ 3, 8,000 |
| international_conferences | Q18564543 (2) | ≥ 2, 3,000 |

Reused, not duplicated: `history.revolutions`, `history.wars`, `history.treaties`, `economy.economic_crises`,
`geography.nature.{epidemics,famines,earthquakes,...}` (they keep every allow-listed property, causal ones
included). Every EVENTS spec is rank-aware (`p:P31/ps:P31`, deprecated excluded), lists its snapshot `properties`
with **P828/P1542/P1478/P1479/P1536/P1537**, and carries an `api_search` block (`run --via api`).
`python -m reva.kg.harvest validate`: 116/116 specs valid. Probes: WDQS `try` impossible (budget spent, 429
cool-downs); the API-mode probe (`python -m reva.events.verify --probe-specs`, REST search) was refused 13 times
(429) and stopped — no retry. Instead an **offline probe** re-applies each spec's api_search classes to KGHIST's
recorded claims: coups select Coup of 18 Brumaire and the Carnation Revolution; transfers select the Louisiana
Purchase, the Indian annexation of Goa and the Partition of India; conferences the Berlin Conference; crises the
Congo Crisis; protests the Haitian Revolution (slave rebellion ⊂ rebellion).

Property requests (`registry/kg/property_requests/events.yaml`, ids/datatypes verified): P1923 participating
team, P8031 perpetrator, P8032 victim(s), P726 candidate, P991 successful candidate, P1590 number of casualties,
P1339 number of injured. Until WDHARVEST merges them the specs cannot keep these claims.

## 7. Demonstration (throwaway DB)

See §7 results below (filled from `python -m reva.events.demo`).

## 8. Limitations and requests

* **WDHARVEST**: merge `property_requests/events.yaml`; run `registry/kg/queries/events` (`--via api`) when budgets
  allow. The harvest DB `reva_kg_wdharvest` was migrated to an earlier `0015_causal` and now fails `bootstrap`'s drift
  check; EVENTS' tools read its ledger without re-bootstrapping it.
* **Lead**: the bare `/v1/events` path is the V1 occurrence listing; the KG listing is `/v1/events/search`. Commit
  `contracts/openapi.json` with these routes (regenerate `ui/src/api/openapi.gen.ts` if the UI uses them).
* **CAUSCORE**: its temporal check reads start = P580 → P585 → P571; the view also reads dataset dates and COW spells
  (labelled derived). `reva.events.view.event_card(...)["interval"]` can be reused for event dates.
* **A-CONTENTION / A-ECRISES**: cross-dataset `same_as` needs a mapping table with evidence (e.g. the JST chronology
  PDF table): add it under `mapping_tables` in `registry/events/same_as_rules.yaml`.
* Dataset kinds for CAUSDATA / A-COUPS / A-HAZARDS / A-SANCTIONS / A-CONTENTION follow their documented ids and coded
  values; they are tested with synthetic records (CAUSDATA-style marker + coded outcome exclusion), not with those
  agents' fixtures.
* Wikidata is community-edited: a type is what Wikidata's P31/P279 claims say at the recorded revision, shown with
  its chain; COW intervals and CShapes independence events are REVA derivations and labelled so.
