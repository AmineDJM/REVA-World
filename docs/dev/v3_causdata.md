# V3 CAUSDATA — expert-coded crises and conflicts: events + T1 causal claims

Agent CAUSDATA, 2026-10-16. Specs: `docs/roadmap/v3_causality.md` (T1 = expert-coded research dataset with a codebook
definition), `docs/dev/v3_causal_interface.md` (CAUSCORE `load_dataset_claims`), `docs/dev/v3_atlas_interface.md` §7
(atlas source platform). Status: three connectors, relation registry, fixtures, offline + throwaway-DB tests done;
full-scale load of the real files into a throwaway DB done (§5); budgets requested (ICB needs a merge, §6).

## 1. What exists

| Piece | Path |
|---|---|
| Shared helpers (claims in CAUSCORE's exact interface, staging, coded dates, event time statements, dataset predicates, bounded KG loader, computed counts) | `src/reva/adapters/connectors/causdata_common.py` |
| ICB v16 connector (`atlas.icb`) | `src/reva/adapters/connectors/atlas_icb.py` |
| COW MID 5.0/5.01 connector (`atlas.mid`) | `src/reva/adapters/connectors/atlas_mid.py` |
| UCDP termination v4-2024 + peace agreements 22.2 connector (`atlas.ucdp_term`) | `src/reva/adapters/connectors/atlas_ucdp_term.py` |
| Dataset variable → REVA relation, with verbatim codebook text and the reason | `registry/causal/relations_datasets.yaml` |
| New source | `registry/sources/icb.yaml` (`icb.data`); MID uses `cow.data` (A-HISTECON), UCDP uses `ucdp.downloads` (KGGEOPOL) |
| Atlas catalog (15 computed-count concepts, theme politics) | `registry/atlas/catalog/crises.yaml` |
| Budget request | `registry/atlas/budget_requests/causdata.yaml` |
| Fixtures (slices of the real 2026-10-16 downloads + full codebook PDFs, hash-pinned) | `tests/fixtures/source/atlas.{icb,mid,ucdp_term}/` (0.9, 0.9, 1.0 MB) |
| Tests | `tests/unit/test_causdata.py` (14, offline), `tests/integration/test_causdata_ingest.py` (3, `db`, fixture mode), `tests/integration/test_causdata_live.py` (3, `live and db`) |

Run: `uv run python -m reva.atlas.ingest atlas.icb|atlas.mid|atlas.ucdp_term [--fixture] [--option claims_staging=DIR]`.
Every request goes through `run.get` (budget-metered live, `FixtureHttp` offline); raw bytes and codebook PDFs are
archived after parsing succeeds; nothing in these modules builds an HTTP client (invariant 30 static scan clean).

## 2. Sources, access, terms (checked 2026-10-16)

| Source | Access | Terms |
|---|---|---|
| **ICB v16** (Brecher, Wilkenfeld, Beardsley, James, Quinn; codebook 2025-06-20): 512 crises 1918-2021, 1,131 crisis actors | Public Box shared links on `sites.duke.edu/icbdata/data-collections/` (no account): `icb1v16.csv`, `icb2v16.csv`, ICB1/ICB2 codebook PDFs. Each download = 3 redirects (duke.box.com → duke.app.box.com → public.boxcloud.com), exactly the SafeHttpClient maximum | **No licence stated; citation requested** → `open_licence: false`, cite-only, flagged for maintainer review (same treatment as COW) |
| **COW MID 5.0 / 5.01** (disputes 1816-2014, incidents 1992-2014) | `correlatesofwar.org/wp-content/uploads/MID-5-Data-and-Supporting-Materials.zip` (MIDA/MIDB 5.0 + Dispute and Incident Coding Manuals) and `Incident_Level_5.01.zip` (MIDI/MIDIP 5.01 + codebook) | COW terms: cite, no charging for access (not open) — same as A-HISTECON's `cow.data` |
| **UCDP Conflict Termination v4-2024** (Kreutz 2010; conflict level, 1946-2024) | `ucdp.uu.se/downloads/monadterm/…_Conflict.csv` + codebook PDF | CC BY 4.0 |
| **UCDP Peace Agreements 22.2** (1975-2021) | `ucdp.uu.se/downloads/peace/ucdp-peace-agreements-222.xlsx` + codebook 22.1 PDF | CC BY 4.0 |

No source needs registration or personal data. The UCDP dyad-level termination file, ICB dyads/non-state-actor/near-
crisis data and the UCDP Conflict Issues Dataset were not ingested (follow-ups, §7).

**Codebook integrity.** Every run fetches the codebook PDFs (MID: the PDFs inside the zips) and compares their sha256
with `relations_datasets.yaml` `documents.*.sha256`. A mismatch withholds that dataset's claims (`claims_withheld`)
while events still load: the quoted definitions might no longer be the provider's.

## 3. Relation semantics (decisions, `registry/causal/relations_datasets.yaml`)

Every claim is a **T1** claim delivered through `load_dataset_claims` with `Codebook(name, version + PDF sha256, url,
definitions)` where `definitions` are the codebook passages quoted verbatim (whitespace normalised, page-number lines
dropped, physical PDF page recorded) and `codebook_term` names the passage the claim rests on. `mechanism` is always
`None` (the datasets carry no per-row source text; REVA never paraphrases). The selector names the file, row and
columns.

| Dataset | Claim (cause → effect) | Relation | Codebook term | Why |
|---|---|---|---|---|
| ICB1 | `icb:crisis:N:trigger` → `icb:crisis:N` | `coded_trigger` | BREAK | "the breakpoint … catalyzes a crisis for the earliest actor"; its date "is also the initiation date for the entire international crisis" |
| ICB1 | `icb:crisis:N` → `icb:crisis:N:outcome` | `coded_outcome` | FOROUT | "the form of the outcome of an international crisis at its termination point" |
| ICB2 | `…:actor:A:trigger` → `icb:crisis:N:actor:A` | `coded_trigger` | TRIGGR | "The trigger or precipitating cause of a foreign policy crisis" — kept as `coded_trigger` (not `immediate_cause`) so the source's own category stays visible |
| ICB2 | `…:actor:A:trigger` → `…:actor:A:response` | `reaction_to` | MAJRES | "Once the decision makers of a state perceive the act/event/change which triggers its crisis, they will decide on an appropriate response" |
| ICB2 | `icb:crisis:N:actor:A` → `…:actor:A:outcome` | `coded_outcome` | OUTCOM | "the content of crisis termination", per actor |
| MID 5.0 | `mid:dispute:D` → `mid:dispute:D:outcome` | `coded_outcome` | outcome.victory / yield / stalemate / compromise / released / unclear (Dispute Coding Manual), `outcome` for 9 "joins ongoing war" | the manual defines the outcome as the alteration of the status quo "as a result of actions taken during the dispute"; -9 (missing) → no claim |
| UCDP term. | `ucdp:episode:E` → `ucdp:episode:E:termination` | `coded_outcome` | `c_outcome=1..6` | the coded way the episode ended; for 5 (low activity) the quoted definition itself says "There is no information in this dataset regarding the group-specific circumstance", and travels with the claim |
| UCDP PA | `ucdp:conflict:C` → `ucdp:pa:P` | `reaction_to` | peace_agreement | "a formal agreement between at least two opposing primary warring parties, which addresses the disputed incompatibility"; one claim per (agreement, conflict id) |

**Not causal (context statements only, never claims):**

* **Coded issues** — MID revision type (RevType1/2 of a revisionist state) and UCDP incompatibility are stored as
  `causdata:coded_issue` (qualifiers: state `sought_by`, rank 1/2, side). The manuals define them as "the type of
  revisions sought by that state" / "the apparent issue(s) at stake in a dispute … related to the goals of the
  so-called revisionist state", judged "not on some intuition about who was the 'aggressor' … or the state that
  'started it all'": what is at stake, not what caused the dispute.
* **MID side A / originator** — side A is "the side that contains the state (or states) that first crossed the
  threat-display-use of force threshold", a designation the manual calls "ultimately arbitrary": a sequence
  convention, stored as `P710` qualifiers, never an initiator-as-cause claim.
* **MID incident order** — the manual says each incident "is an outgrowth of or a response to one or more previous
  incidents" but does not code which one: only `P361` (part of the dispute) and `P155` (follows the previous incident
  in the v5.01 temporal order) are stored.
* **ICB triggering entity (TRIGENT)** is an attribute of the trigger act (`icb:triggering_entity` / `_kind` for
  995 internal / 996 non-state / 997 several states); OUTESR "tension escalation — crisis recurred … during the
  subsequent five-year period" names no later crisis → attribute only. PCID → `P361` part of `icb:pc:<id>`
  (regional buckets 40-44 = "not part of protracted conflicts" → nothing).
* **Termination ↔ agreement** — the termination codebook says outcome 1 is an agreement "signed either during the last
  year of active conflict or the first year of inactivity" but does not name it. REVA lists every agreement of the
  same conflict dated in that window as `causdata:agreement_in_termination_window` (rule + candidate count in the
  qualifiers). In the real data 32 of 60 such terminations have no candidate (the PA data start in 1975) and those
  with candidates often have several (up to 12): a join by REVA would be a manufactured link, so it is never a claim.

## 4. KG records

Entity kinds (for EVENTS' dataset-kind mapping): `international_crisis`, `foreign_policy_crisis`, `crisis_trigger`,
`crisis_response`, `crisis_outcome`, `protracted_conflict` (ICB); `militarized_interstate_dispute`,
`militarized_incident`, `dispute_outcome` (MID); `armed_conflict_episode`, `conflict_termination`, `peace_agreement`
(UCDP); `state_code` (shared). Each event also has `causdata:event_type` = its kind.

Dates are Wikidata properties `P580` start / `P582` end / `P585` point in time (the dates CAUSCORE's temporal check
reads), at the coded precision: ICB day 88 or blank and MID day -9 → month (10), missing month → year (9); UCDP
precision codes 1 → day, 2-3 → month, 4-7 → year (as KGGEOPOL). An impossible date is kept at month precision, never
corrected. Context: `P361`, `P155`, `P710` (allow-listed by WDHARVEST, registered from `registry/kg/properties.yaml`)
and 27 dataset predicates (`CAUSDATA_PROPERTIES`: `causdata:*`, `icb:*`, `mid:*`, `ucdpterm:*`, `ucdppa:*`), none
causal (a test checks no CAUSAL_PIDS is ever used). States: KGGEOPOL's `StateEntityResolver` (Wikidata QID for
historical states, else `gw:`/`cow:` code entities with `geopol:corresponds_to_iso3`). UCDP conflicts are KGGEOPOL's
`ucdp:conflict:<id>` entities (not re-emitted). KG source keys = registry source keys (`icb.data`, `cow.data`,
`ucdp.downloads`); statement ids are prefixed (`c…`, `a…`, `d…`, `i…`, `ep…`, `pa…`) so they never collide with
A-HISTECON's / KGGEOPOL's statements under the same source key.

Atlas tables (all COMPUTED counts, only state-years with ≥ 1 case): `icb.actor_cy`, `cow.mid5_cy`, `ucdp.term_cy`,
`ucdp.pa_cy` (catalog `registry/atlas/catalog/crises.yaml`).

## 5. Full-scale load (real 2026-10-16 bytes, throwaway DB, dropped afterwards)

See §5 table (filled from `fullscale_stats.json`).

## 6. Claims delivery and budgets

`deliver_claims(run, deliveries)`: when `load_dataset_claims` (found in `reva.causal` or `reva.causal.model`) and
`kg.causal_claim` exist, each delivery is loaded (`complete=True`: a row dropped by the provider is retracted);
otherwise, with the run option `claims_staging=DIR`, each delivery is written as one JSON file
(`<source_key>__<dataset>__<origin>.json`, exactly the interface fields) and `causdata_common.load_staged(engine,
store, DIR)` loads and grades them later; otherwise the summary says `causal_unavailable`. A-CONTENTION's
`atlas_navco` uses the same `ClaimRecord` / `ClaimDelivery` / `deliver_claims` API (kept stable).

Budgets (`registry/atlas/budget_requests/causdata.yaml`): `icb.data` 16/day (new source; a live ICB run is refused
until ATLASCORE merges it, and invariant 30's `test_budget_keys_are_registered_sources_and_cover_every_connector`
fails on `icb.data` until then — the usual registration/merge lag of a new source), `cow.data` 34/day and
`ucdp.downloads` 30/day (shared with A-HISTECON / KGGEOPOL; MID and UCDP live runs fit the current 26 / 16).

## 7. Gaps and follow-ups

* ICB dyadic data, ICB non-state-actor data (1987+), near crises (Iahknis & James 2019): not ingested.
* UCDP dyad-level termination file (`…_Dyad.csv`) and the UCDP Conflict Issues Dataset (rebel stated goals): not
  ingested; issues would be `coded_issue` context.
* No `same_as` between ICB crises, MIDs, UCDP conflicts or COW wars: none of the files carries the other's identifier
  (ICB-MID comparisons exist in the literature, e.g. Hewitt 2003, but no released mapping table was found).
* ICB names are the provider's upper-case abbreviations (e.g. "PAL. PRT./ISRAEL IND."), kept verbatim.
