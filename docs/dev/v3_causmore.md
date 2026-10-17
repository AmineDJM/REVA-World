# V3 CAUSMORE — more expert-coded event datasets with codebook-defined relations

Agent CAUSMORE, 2026-10-17. Owner's brief: "plus de données, plus de liens entre les événements, une causalité très
robuste — ne force pas ce lien". Specs: `docs/roadmap/v3_causality.md`, `docs/dev/v3_causal_interface.md`,
`docs/dev/v3_causdata.md` (same patterns and the same claim-delivery API).

Rule applied to every source: a T1 claim only where the provider's codebook itself defines the coded field as the
outcome / effect / what the record addresses. Transformations, sequences, part-of, participants, initiators,
identifiers and REVA joins of two datasets are context, never causal. A codebook that differs from the pinned one
withholds the dataset's claims (events and context still load).

## 1. What exists

| Piece | Path |
|---|---|
| Relation registry (verbatim codebook text, page/cell, decision and reason per variable) | `registry/causal/relations_causmore.yaml` |
| Shared helpers (registry, codebook checks, predicates, KG load through CAUSDATA's bounded loader, claim delivery through `causdata_common.deliver_claims`) | `src/reva/adapters/connectors/causmore_common.py` |
| COW War Data v4 (`atlas.cowwars`, source `cow.data`) | `src/reva/adapters/connectors/atlas_cowwars.py` |
| PA-X Peace Agreements Database v10 (`atlas.pax`, new source `pax.peaceagreements`) | `src/reva/adapters/connectors/atlas_pax.py`, `registry/sources/pax.yaml` |
| IDMC GIDD disasters (`atlas.idmc`, new source `idmc.gidd`) | `src/reva/adapters/connectors/atlas_idmc.py`, `registry/sources/idmc_gidd.yaml` |
| UCDP Conflict Termination v4-2024, dyad level (`atlas.ucdp_dyadterm`, source `ucdp.downloads`) | `src/reva/adapters/connectors/atlas_ucdp_dyadterm.py` |
| Event typing | `registry/events/taxonomy.yaml`: new type `disaster_displacement`; marker kinds `causmore:event_type` and `idmc:hazard_sub_type`; `cow:war:` coded type `cowwar:war_type` (civil wars), structure role of `cowwar:transformed_into` |
| Identity | `registry/events/same_as_rules.yaml` rule `pax_ucdp_agreement_id` (PA-X `UcdpAgr` -> `ucdp:pa:<id>`; SAMEAS then marked it `one_value_per_holder` + `independent_sources`) |
| Budgets | `registry/atlas/budget_requests/causmore.yaml`, merged: `budgets-1.3.9` (cow.data 34 -> 44, pax.peaceagreements 6, idmc.gidd 6), digest re-pinned in `src/reva/autonomy/policy.py` |
| Fixtures (slices of the real 2026-10-17 downloads in the providers' formats + full codebooks, licence in each manifest) | `tests/fixtures/source/atlas.{cowwars,pax,idmc,ucdp_dyadterm}/` (0.4, 0.9, 0.04, 0.27 MB) |
| Tests | `tests/unit/test_causmore.py` (14, offline), `tests/integration/test_causmore_ingest.py` (4, `db`, fixture mode, re-run is a no-op) |

All fetches go through `run.get` (budget-metered live, `FixtureHttp` offline); no module builds an HTTP client or
sets a User-Agent (test). All four sources are KG-only (a declared dataset gates the KG load; no atlas table).

## 2. Sources, access, licences (checked 2026-10-17)

| Source | Access (no account) | Licence | Claims |
|---|---|---|---|
| **COW War Data v4**: Inter-State 4.0, Extra-State 4.0, Intra-State **4.1**, Non-State 4.0 + 4 codebook PDFs | `correlatesofwar.org/wp-content/uploads/…` (8 requests, 0.5 MB) | COW terms: cite, no charging for access (not open) — as `atlas.cow` | yes |
| **PA-X v10** (2,257 agreements 1990-2025) + codebook v10 | `www.peaceagreements.org/cms/documents/…` (2 requests, 4.5 MB) | **CC BY-NC-SA 4.0** (Terms_of_Use.pdf; "commercial use is strictly prohibited"; notify peacerep@ed.ac.uk) — `open_licence: false`, flagged | no (context + identity) |
| **IDMC GIDD – Disasters** (event-level internal displacements 2008-2024) | HDX `package_show` + the resource it names (302 to HDX's S3 store) (2 requests, 1.7 MB). IDMC's own API needs a registered client: not used | README sheet: **CC BY-NC**; HDX record: CC BY. The more restrictive provider statement is applied, both recorded, flagged | yes |
| **UCDP Conflict Termination v4-2024, dyad file** + the pinned codebook | `ucdp.uu.se/downloads/monadterm/…` (2 requests, 1 MB) | CC BY 4.0 | yes |

Why intra-state **v4.1** and not v5.1: inter/extra/non-state v4.0 code transformations with v4 war numbers,
`atlas.cow` keys `cow:war:<n>` by v4 numbers and PA-X's `CowWar` cites them, while v5.1 renumbers wars and reuses
numbers for other wars (856 is "Eighth Colombia" in v4.1, "Romanian War of 1989" in v5.1; likewise 809, 854, 857,
860, 878, 882 among the numbers PA-X cites). One id namespace must mean one numbering. With v4.1 all 45
transformation pairs and all 66 PA-X `CowWar` numbers resolve; with v5.1, 9 PA-X numbers and 9 transformation
references did not resolve and 7 PA-X numbers would have pointed at a different war.

## 3. Relation decisions (`registry/causal/relations_causmore.yaml`)

| Dataset | Claim (cause -> effect) | Relation | Codebook term (quoted verbatim) | Why |
|---|---|---|---|---|
| COW inter-state | `cow:war:<n>` -> `cowwar:outcome:<n>:<ccode>:<k>` (one per state record) | `coded_outcome` | `Outcome` ("1 = Winner 2 = Loser … 8 = changed sides") | the codebook codes the war's outcome per state participant record (as ICB2 / MID) |
| COW extra/intra/non-state | `cow:war:<n>` -> `cowwar:outcome:<n>` | `coded_outcome` | `Outcome` ("1 – Side A wins 2 - Side B wins …") | war-level outcome; identical on every record of a war (checked; a disagreement would withhold it) |
| IDMC | `idmc:event:<id>` -> `idmc:displacement:<id>:<ISO3>:<Year>` | `has_effect`, polarity `promotes` | `disaster_displacement` ("forced to leave their homes … as a result, or in anticipation of the negative impact of natural hazards"); the figure is displacements "as a result of disasters" | the source attributes the displacement to the event; the preventive-evacuation flag travels in the claim scope |
| UCDP dyad termination | `ucdp:dyadepisode:<d_epid>` -> `…:termination` | `coded_outcome` | `d_outcome=<k>` (same category texts as CAUSDATA's `c_outcome=<k>`, incl. the outcome-5 caveat) | how each warring dyad's episode ended |

**Context only (never a claim):**

* **COW transformations** (`TransTo` / `TransFrom`) -> `cowwar:transformed_into` war -> war, one statement per
  (pair, file, column) with every coding row in the selector. The codebook: "if a war changes so that one or more of
  the parties doing the bulk of the fighting changes, the initial war ends and the conflict is transformed into a war
  of a second type" — one conflict changing category, not one war causing another. Taxonomy: structure family
  (`followed_by` / `follows`). 45 pairs, 41 coded by both wars' files.
* COW outcome 5 ("ongoing as of 12/31/2007") is an attribute, no outcome record; `Initiator` is a participant role
  (string / qualifier); intra-state state participants -> `P710` (`cow:<ccode>`, side).
* **PA-X conflict / war -> agreement** (`UcdpCon`, `CowWar`) -> `pax:ucdp_conflict` / `pax:cow_war` context
  statements. The codebook defines a peace agreement as a document "addressing conflict with a view to ending it":
  what it addresses, not that it is a reaction to (or caused by) the conflict. `atlas.pax` 1.0.0 delivered these as
  3,334 `reaction_to` claims (the mapping CAUSDATA had used for UCDP agreements); the causal audit's M6
  (`docs/dev/review_v3_causal_audit.md`) then judged that mapping an association made causal and AUDFIX turned the
  UCDP agreements into context. `atlas.pax` 1.1.0 applies the same reading: no claim, and its empty complete delivery
  retracts the 1.0.0 claims through the loader.
* PA-X `UcdpAgr` -> identifier `pax:ucdp_agreement_id` (same_as evidence), `PamAgr` identifier, `PP` -> `P361` part of
  `pax:process:<PP>`, `Contp` -> `causdata:coded_issue`, `Stage` verbatim. **No agreement -> agreement link**: "Imp:
  Aiming to implement an earlier agreement" names no agreement, `OthAgr` lists names "as found in the text" and
  "Pre … does not imply temporal precedence": joining them would be name matching.
* IDMC hazard classification (`idmc:hazard_sub_type`), GLIDE numbers and other event codes (identifiers).
* UCDP dyad `d_cfiredate` / `d_peagdate`: dated facts on the termination ("at, or after, the end of the dyad episode"),
  no agreement id, so never joined to an agreement record.

## 4. Counts on the full 2026-10-17 files (offline builds)

| Source | Rows | KG entities / statements | T1 claims |
|---|---|---|---|
| COW wars | 337 + 198 + 442 + 62 records; 95 + 163 + 334 + 62 = 654 wars | 1,288 (396 new intra/non-state wars, 892 outcomes) / 7,701 | 892 `coded_outcome` (337 inter per state, 161 extra, 332 intra, 62 non-state) |
| PA-X v10 | 2,257 agreements, 183 processes | 2,440 / 24,440 | none (2,045 conflict and 1,289 war links as context) |
| IDMC GIDD | 22,119 rows -> 21,678 events | 43,786 / 156,002 | 22,108 `has_effect` |
| UCDP dyad term. | 3,432 dyad-years -> 985 episodes | 1,900 / 10,209 | 915 `coded_outcome` (d_outcome 5: 395, 3: 149, 1: 109, 2: 101, 6: 88, 4: 73) |

Anomalies reported (never corrected): IDMC 11 fully identical provider rows (no Event ID) loaded once; IDMC rows
without an Event ID are their own events (keyed by a sha256 of their fields — never grouped by name); UCDP dyad
episodes 64901, 70901, 72202, 83203, 83306, 88201, 1179801 have several terminal rows (the last is used) and 53801
has no outcome; PA-X UCDP ids 1113 (4 agreements) and 1070 (2) are ambiguous for same_as.

## 5. Dev database load (2026-10-17, `flock /tmp/reva-devdb.lock`, one source at a time, live, budget-metered)

| Source | Requests | KG entities / statements inserted (rejected) | Claims loaded (rejected, unknown endpoints) |
|---|---|---|---|
| `atlas.cowwars` 1.0.0 | 8 | 1,288 / 7,701 (0) | 892 (0, 0) — cow_inter 337, cow_extra 161, cow_intra 332, cow_nonstate 62 |
| `atlas.pax` 1.0.0, then 1.1.0 | 2 + 2 | 2,440 / 24,440 (0) | 1.0.0: 3,334 `reaction_to`; 1.1.0: **all 3,334 retracted** (empty complete delivery), 0 current |
| `atlas.idmc` 1.0.0 | 2 | 43,786 / 156,002 (0) | 22,108 (0, 0) |
| `atlas.ucdp_dyadterm` 1.0.0 | 2 | 1,900 / 10,209 (0) | 915 (0, 0) |

All codebook checks matched the pinned documents (four COW PDFs, PA-X v10 PDF, UCDP termination PDF; IDMC README:
all six definitions found verbatim). The 1.1.0 PA-X run reported 3,334 `statements_conflicting`: the first 1.1.0 build
added an explanatory qualifier under the same source revision; the loader kept the 1.0.0 content (it never overwrites
a revision silently) and the committed 1.1.0 code emits exactly that content again, so the KG and the code agree.
`reva db bootstrap` registered the new connector versions; `reva causal refresh --origin live` afterwards (Wikidata
claims unchanged, everything graded).

**New current T1 claims: 23,915** (live dev DB total after the refresh: 52,376, including other agents' loads).

| Dataset | Relation | Claims | Grade B consistent | B overlapping | B undetermined | E inconsistent |
|---|---|---:|---:|---:|---:|---:|
| cow_inter | coded_outcome | 337 | 337 | 0 | 0 | 0 |
| cow_extra | coded_outcome | 161 | 159 | 2 | 0 | 0 |
| cow_intra | coded_outcome | 332 | 328 | 2 | 2 | 0 |
| cow_nonstate | coded_outcome | 62 | 56 | 6 | 0 | 0 |
| idmc_disasters | has_effect | 22,108 | 16 | 22,091 | 0 | 1 |
| term_dyad | coded_outcome | 915 | 632 | 282 | 0 | 1 |

No grade A: none of these claims is asserted by a second independent source on the same (cause, effect). IDMC claims
are mostly `overlapping` because the displacement is dated by its reporting year and the event by its start day in
that year. The two `inconsistent` claims are source inconsistencies, kept and shown, never corrected:

* claim 37214, `idmc:event:1632` -> `idmc:displacement:1632:AFG:2017`: the event's "Date of Event (start)" is
  2018-03-29, the displacement is reported for 2017.
* claim 55075, `ucdp:dyadepisode:44001` -> `…:termination`: dyad start (`start_date2`) 1977-06-01 (day), termination
  1977-05 (month precision).

Examples (dev DB ids):

* claim 31965 `cow:war:139` (World War II) -> `cowwar:outcome:139:2:1` (United States: Winner), `coded_outcome`,
  grade B (`tier.T1, temporal.consistent, corroboration.single, contest.none, grade.B.expert`).
* claim 32159 `cow:war:327` (Uruguay War, extra-state) -> `cowwar:outcome:327` ("The war was transformed into another type of war"), and the
  context statements `cow.data:cwt:327:19:extra:TransTo` and `cow.data:cwt:327:19:inter:TransFrom`
  (`cowwar:transformed_into` war 327 -> inter-state war 19, coded by both files) — the transformation is context.
* claim 32688 `idmc:event:1` (China southern provinces floods, June 2016) -> `idmc:displacement:1:CHN:2016`,
  `has_effect` (promotes), grade B.
* `cow:war:856` (Eighth Colombia, intra-state v4.1) has outcome 5 "ongoing as of 12/31/2007": an attribute, no claim;
  PA-X links 241 agreements to it as `pax:cow_war` context.


## 6. Rejected or deferred candidates, and why

| Candidate | Decision |
|---|---|
| COW Intra-State v5.1 | deferred: renumbers and reuses war numbers (see §2); needs its own namespace and a mapping from the codebook's change list |
| MID -> war escalation | none: MID 5.0 carries no COW war number (HostLev 5 = war is a level, not a link); no released MID-war mapping table |
| UCDP External Support (ESD 18.1) | not loaded: support to a side is participation/context; ACD secondary parties (troops) are already KGGEOPOL's `ucdp:secondary_party` |
| GLIDE (HDX, CC BY-IGO) | deferred: event identifiers without coded relations; IDMC now stores 1,077 GLIDE numbers (`idmc:glide_number`) so a later GLIDE load can use them as shared identifiers |
| ReliefWeb disasters | rejected: co-typing of hazards on one record, no coded secondary-hazard relation |
| PITF State Failure problem set | rejected: the codebook groups events into "consolidated" complex cases (part-of) but codes no relation between them |
| REIGN | rejected: no causal coding (and discontinued) |
| Smithsonian GVP eruptions | rejected: eruption attributes only; eruption -> tsunami is already NOAA NCEI's coded source event (A-HAZARDS) |
| NOAA tsunami source causes | already used by A-HAZARDS (earthquake/eruption -> tsunami) |
| WHO Disease Outbreak News | rejected: no coded relation (context only, not worth an event load here) |
| Powell & Thyne, CNTS, EM-DAT, ACLED, GTD | blocked / licensed / account required |

## 7. Follow-ups

* Intra-State v5.1 in its own namespace (`cow:war5:`) with an evidenced v4.1 <-> v5.1 mapping from the codebook's
  "War# changed" list.
* GLIDE events as an identity hub (IDMC, ReliefWeb) — context only.
* PA-X atlas table (agreements per country-year from `Loc1ISO`/`Loc2ISO`) if wanted.
