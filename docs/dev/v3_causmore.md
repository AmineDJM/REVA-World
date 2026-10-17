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
| **PA-X v10** (2,257 agreements 1990-2025) + codebook v10 | `www.peaceagreements.org/cms/documents/…` (2 requests, 4.5 MB) | **CC BY-NC-SA 4.0** (Terms_of_Use.pdf; "commercial use is strictly prohibited"; notify peacerep@ed.ac.uk) — `open_licence: false`, flagged | yes |
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
| PA-X | `ucdp:conflict:<UcdpCon>` -> `pax:agt:<AgtId>`; `cow:war:<CowWar>` -> `pax:agt:<AgtId>` | `reaction_to` | `peace_agreement` ("… addressing conflict with a view to ending it") + `UcdpCon`, `CowWar` | same mapping as CAUSDATA's UCDP peace agreements; the conflict provoked the agreement that addresses it |
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
| PA-X v10 | 2,257 agreements, 183 processes | 2,440 / 24,440 | 3,334 `reaction_to` (2,045 from UCDP conflicts, 1,289 from COW wars) |
| IDMC GIDD | 22,119 rows -> 21,678 events | 43,786 / 156,002 | 22,108 `has_effect` |
| UCDP dyad term. | 3,432 dyad-years -> 985 episodes | 1,900 / 10,209 | 915 `coded_outcome` (d_outcome 5: 395, 3: 149, 1: 109, 2: 101, 6: 88, 4: 73) |

Anomalies reported (never corrected): IDMC 11 fully identical provider rows (no Event ID) loaded once; IDMC rows
without an Event ID are their own events (keyed by a sha256 of their fields — never grouped by name); UCDP dyad
episodes 64901, 70901, 72202, 83203, 83306, 88201, 1179801 have several terminal rows (the last is used) and 53801
has no outcome; PA-X UCDP ids 1113 (4 agreements) and 1070 (2) are ambiguous for same_as.

DEVDB_SECTION

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
