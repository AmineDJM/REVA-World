# V3 SAMEAS — evidenced cross-source event identity, corroboration across sources, chains across sources

Agent SAMEAS, 2026-10-17. Spec: `docs/roadmap/v3_causality.md` (Event model: "the same real event coming from several
sources is linked by explicit `same_as` statements with evidence ... never merged by fuzzy name/date matching";
claim `corroboration`; principle 6, chains are compositions). Builds on EVENTS (`docs/dev/v3_events.md` §5) and
CAUSCORE (`reva.causal`). Dev database `reva`, origin `live`.

**Bottom line.** Of the identifiers the loaded event datasets carry, **only provider-published crosswalks** connect
records of two sources; **Wikidata holds no identifier property** for any of the loaded event datasets (COW, UCDP,
ICB, MID, Archigos, NAVCO, Mass Mobilization, TIES, EUSANCT, PRIO, NCEI hazard events, IDMC/GLIDE). REVA now loads
__LINKS__ evidenced `events:same_as` links and the grader and the chain search resolve them, but **no claim reaches
grade A through them in the dev DB**: the only cross-provider identity that carried causal claims on both sides
(PA-X ↔ UCDP peace agreements) lost those claims in the same hour, when the causal audit (M6) and CAUSMORE
reclassified "conflict → agreement" as context in both datasets. Details, numbers and what would change it below.

## 1. Evidence searched

### 1.1 Wikidata identifier properties (budget-metered, archived)

Full-text search of the property namespace (ns 120) through `Special:Search` page views, then
`Special:EntityData` snapshots of the candidate properties, all through the budget-metered `wikidata.wbapi` client
(`reva.events.verify.wbapi_client`: machine-wide lock, ledger, archive in `var/kg/objects`; generic project
User-Agent, no personal data). The action API (`wbsearchentities`, `list=search`) answered HTTP 429 to the shared
egress during the run (WDQS was not used: its budget belongs to the harvest). 43 searches + 38 property snapshots,
recorded with their raw sha256 in `registry/events/_recorded/wikidata_property_search.json`.

| searched | result |
|---|---|
| "Correlates of War", "Correlates", "war identifier" | no COW property (hits are unrelated: P11821 "quality is the result of process", …) |
| "UCDP", "Uppsala Conflict Data", "PRIO", "conflict identifier", "peace agreement", "PA-X" | none for UCDP/PRIO/PA-X (P13196 Three Decks conflict ID, P11157 HiSCoD ID, P12537 EJAtlas ID are other databases) |
| "International Crisis Behavior", "crisis", "Militarized Interstate Dispute", "coup", "Archigos", "NAVCO", "Mass Mobilization", "Threat and Imposition", "sanctions" | none (P14521 EU sanctions tracker *subject* ID and P10632 OpenSanctions ID identify persons/entities, not episodes) |
| "NCEI", "NGDC", "National Centers for Environmental Information", "tsunami", "earthquake", "volcanic eruption" | earthquake identifiers exist — P3196 USGS earthquake ID, P6419 GEOFON, P4207 INGV, P5182 JMA — but NCEI records carry none of them (checked in the archived NCEI API responses); no NCEI event property |
| "GLIDE", "GLIDE number", "Global Identifier Number", "ReliefWeb", "EM-DAT", "disaster identifier", "Internal Displacement Monitoring" | P14355 EM-DAT disaster number exists (EM-DAT is not loaded: account required); no GLIDE / ReliefWeb property, so IDMC's GLIDE numbers cannot reach Wikidata |
| "Global Volcanism Program" (control) | P1886 — the property NCEI *volcano* records (places, not events) are already linked by |
| "Treaty Series", "United Nations Treaty" | P4231 UNTS registration number, P9966 UNTC object ID — the UNTC pages REVA archived carry neither |

So the rule "a Wikidata item holding two dataset IDs" has nothing to work with today; the inactive rules in
`registry/events/same_as_rules.yaml` (`ucdp_conflict_id_on_wikidata`, `cow_war_number_on_wikidata`,
`icb_crisis_on_wikidata`, `mid_dispute_on_wikidata`, `ncei_hazard_event_on_wikidata`, `unpk_mission_on_wikidata`)
record this evidence and activate with a property id once one exists.

### 1.2 Provider crosswalks in the archived raw bytes

Headers and codebooks of the archived files (`var/objects`):

| dataset (file) | cross-dataset keys published by the provider |
|---|---|
| ICB1/ICB2 v16 (`69757d13…`, `33e0a3de…`) | none (crisno, cracno, cracid, pcid; no MID / COW war / UCDP key) |
| MID 5.0 MIDA/MIDB/MIDI/MIDIP (`f1d2f47e…`, `45847bad…`) | none (no COW war number, no ICB number) |
| COW wars v4 (`2535e30b…`, `529c325f…`) | only COW-internal transitions (TransFrom/TransTo: context) |
| UCDP/PRIO ACD v26.1, termination, peace agreements | UCDP-internal ids only (conflict_id, episode, agreement) |
| NAVCO 1.2 / 2.1 | one campaign id across versions (codebook: "NAVCO ID source = NAVCO 2.1") — same project |
| MEC | `sourcenavco` is a *source flag*, not a NAVCO id |
| EUSANCT (`e5d94f19…`) | the TIES case id, with `datasource` naming TIES — EUSANCT took those cases over |
| **PA-X v10** (CAUSMORE) | **`UcdpAgr`: the UCDP peace-agreement id of the same document** (codebook p. 24); `UcdpCon`, `CowWar` are associations (the conflict an agreement addresses), not identities |
| IDMC GIDD (CAUSMORE) | GLIDE numbers and IFRC appeal ids — no other loaded dataset carries them |
| NCEI hazards | NCEI-internal associations (tsunami ↔ earthquake ↔ eruption) and GVP volcano numbers (places) |

## 2. Rules (`registry/events/same_as_rules.yaml`, version 2)

New in `reva.events.same_as`:

* kind **`statement_link`** — a provider-coded identity statement already written by the connector from the
  provider's own key (`eusanct:same_case_as`, `contention:same_campaign_as`); one-to-one only (a record linked to
  several records of the other side is ambiguous, never linked); evidence = the statement key, source key, raw
  sha256 and qualifiers;
* **`one_value_per_holder`** on `prefix_vs_property` — a record citing several records of the other dataset (one
  PA-X agreement citing two UCDP agreements) is ambiguous: linking it would merge the two UCDP records;
* **`independent_sources`** copied into each link's evidence: `false` when one record derives from the other.

The existing guards stay: a value held by more than two records is ambiguous (`max_group`), `one_per_source`
(two records of one dataset are two events), never names or dates.

| rule | kind | independent | evidence |
|---|---|---|---|
| `pax_ucdp_agreement_id` (CAUSMORE's rule + SAMEAS guards) | prefix_vs_property `pax:ucdp_agreement_id` → `ucdp:pa:` | yes (caveat §4) | PA-X `UcdpAgr` |
| `eusanct_ties_case` | statement_link `eusanct:same_case_as` | **no** (EUSANCT took the case over from TIES) | identical case id, datasource names TIES, identical target COW code |
| `navco12_navco21_campaign` | statement_link `contention:same_campaign_as` | **no** (one project) | NAVCO 1.2 codebook |
| `colonial_independence_key` (A-COLONIAL, now loaded) | same_property | — | shared COW state / ruler codes |
| `untc_mtdsg_number`, `untc_treaty_qids` (EVENTS) | same_property / mapping table | — | MTDSG number; verified QID table |

CLI: `uv run reva events same-as --origin live [--dry-run]` (derive + load as KG statements `events:same_as`,
source `reva.events.same_as`, worker role; links whose evidence disappeared are retracted). No migration.

## 3. Grader and chain search

**Grading rules version 2** (shared with CAUSFIX's version 2, one changelog in `registry/causal/grading.yaml`; old
grade rows untouched, new rows written because the inputs changed):

* section `identity: {resolve_same_as: true, same_as_property: events:same_as}`;
* `reva.causal.identity` reads the current `events:same_as` statements of the origin and builds clusters
  (union-find; a cluster above 12 records is left unresolved and reported);
* corroboration groups claims on (cause **cluster**, effect **cluster**, family, stance): the same pair of events
  coded by two sources under their own record ids is one link;
* records joined by a link with `independent_sources: false` get the upstream key `lineage:<link>`: their claims
  fall in one source group (count once), exactly like two Wikidata statements citing one reference;
* fact rule `corroboration.via_same_as` is attached when corroboration counted a claim made on another record of
  the same events; the grade row's `corroborating_claim_keys` lists those claims and its inputs hash includes the
  identity used.

**Chain search** (`reva.causal.graph.chain`, `/v1/causal/chain`): the bidirectional search adds the same_as
cluster members of every reached node at no causal cost, so a chain may continue from one source's record of an
event to another source's record of it. Each path lists its hops with `kind: claim | identity`; an identity hop
has no grade and no claim, carries the `same_as` statements with their evidence (rule, evidence type, property,
value, raw sha256, `independent_sources`) and the note "An identity hop joins two records of the same event … it
is not a causal claim and adds no causal hop". `length` counts causal hops only; `identity_hops` and
`cross_source` are reported; the wording stays "Composition by REVA of N separately asserted claim(s) … REVA does
not assert the chain" and adds "It crosses K identity link(s) …". Two records of one event are refused as a chain
("from and to are records of the same event"). UI (`ui/src/v3/causal/ChainFinder.tsx`): identity hops render as
"Identité : X ≡ Y — même événement enregistré par deux sources, relié par un lien same_as fondé sur une preuve …
ce n'est pas une affirmation causale ni un maillon", with rule, evidence and statement key; they are not counted
as maillons and carry no grade. NLQA keeps only chains whose every hop is a sourced claim, so an identity-crossing
chain is not narrated by `causal_chain` yet (it is shown by the API and the UI).

## 4. Dev DB run (2026-10-17)

__RUN__

## 5. Honesty notes

* **PA-X ↔ UCDP independence.** The link itself is PA-X's published cross-reference. For the 313 linked agreements,
  PA-X's own conflict coding (`UcdpCon`) equals UCDP's `conflict_id` for 298, overlaps for 4, differs for 9 (e.g. `pax:agt:422` codes conflict 385 where UCDP agreement 1069 codes 384; four PA-X
  agreements code 309 where UCDP codes 11348) and is empty for 2: PA-X did not simply copy UCDP. The cross-reference
  still shows PA-X's coders consulted UCDP's list, so REVA cannot exclude copying for some agreements; the rule is
  declared independent with this caveat. Moot today: neither dataset's conflict → agreement association is a
  causal claim any more (audit M6, atlas.ucdp_term 1.1.0, atlas.pax 1.1.0).
* **Derived records count once.** EUSANCT cases taken over from TIES and NAVCO 1.2 / 2.1 campaigns are linked for
  identity (event view, chains) but never corroborate each other (`lineage:` upstream).
* **Outcome records.** Each dataset's coded-outcome node (`…:outcome`) is REVA's node for that dataset's coding; it
  is never linked by same_as across datasets (the audit's L6: outcomes are not corroboration material).
* **Nothing is merged**; the links are statements with their evidence, retracted when the evidence disappears.

## 6. What would produce grade A / cross-source chains

1. A Wikidata identifier property for one of the loaded event datasets (UCDP conflict id, COW war number, ICB crisis
   number…) — a property proposal on Wikidata is the owner's call; the inactive rules activate with its id.
2. Loading a dataset that carries an identifier both sides hold, e.g. USGS ComCat events (P3196 on Wikidata) *with*
   a provider crosswalk to NCEI (none known), or EM-DAT (P14355; account required) with IDMC's EM-DAT/GLIDE codes.
3. A second independent provider coding the same *causal* link as an existing dataset (e.g. Powell & Thyne coups
   vs Archigos coups: the host blocks non-browser clients — owner decision in HANDOFF).

## 7. Files

`src/reva/causal/identity.py` (new), `src/reva/causal/grader.py`, `src/reva/causal/grading.py`,
`src/reva/causal/graph.py`, `src/reva/causal/temporal.py` (a textual `scope.period` no longer crashes
`claim_time_window`), `registry/causal/grading.yaml`, `src/reva/events/same_as.py`,
`registry/events/same_as_rules.yaml`, `registry/events/_recorded/wikidata_property_search.json`,
`src/reva/api/routes/causal.py` (chain hop `kind`, `same_as`, `identity_hops`, `cross_source`),
`contracts/openapi.json`, `src/reva/cli/main.py` (`reva events same-as`), `src/reva/adapters/http/safe_client.py`
(`Content-Encoding: none`, sent by peaceagreements.org, read as identity — same byte bound),
`ui/src/v3/causal/{api.ts,model.ts,ChainFinder.tsx,fixtures.ts,causal.test.tsx}`; tests
`tests/unit/causal/test_causal_identity.py`, `tests/integration/test_causal_sameas.py`,
`tests/unit/test_safe_client_codings.py`.
