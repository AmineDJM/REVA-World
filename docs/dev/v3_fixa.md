# V3 FIXA — causal core and API fixes for the second audit (grading.yaml version 3)

Agent FIXA, 2026-10-17. Input: the second independent audit `docs/dev/review_v3_causal_audit2.md` (defects N1, N2,
N3, N7, N9, N11 and the remainders of M1, M2), with `v3_causfix.md`, `v3_apifix.md` and `v3_audfix.md`. Scope: the
causal core and its API (`src/reva/causal/**`, `registry/causal/**`, `src/reva/api/routes/causal.py`, the causal
parts of `src/reva/nlqa/**`, `ui/src/v3/causal/**`), plus the KG statement loader's retraction reason
(`src/reva/kg/model.py`) and one migration. The dataset connectors (IDMC, UCDP, COW: N4, N5, N6, N8, N10, N12) are
agent FIXB's and were not edited here.

**REVA still asserts no causation.** Every change either tells the reader more exactly who asserts what, or
stops weaker evidence from looking stronger. No row was edited or deleted: corrected retraction reasons are new
annotation rows, every regrade is a new `kg.causal_grade` row (version 1 and 2 rows stay).

## 1. What changed

| # | Defect | Fix | Where |
|---|---|---|---|
| N1 | Every retraction (4,264 claims, 1,635 KG statements) said `removed_at_source`: the loaders hard-coded it | **Migration `0018_retraction_reasons`** (the enum had no honest value for KG statements, and old rows needed a place for a correction): reasons `reclassified_by_reva` and `absent_from_delivery` added to both retraction tables, a nullable `detail jsonb` (basis, raw sha256, revision / connector from → to), the append-only table `kg.retraction_annotation` (target row, the reason as recorded — restated and checked by a trigger —, the corrected reason, evidence, note, annotator) and the view `kg.retraction_reason_current` (`effective_reason`). **Loaders derive the reason**: a claim / statement absent from a delivery whose raw artifact is the one the retracted row cites → `reclassified_by_reva` (same bytes: the provider still has the row); a dataset caller may pass `source_row_present` (row verified absent → `removed_at_source`; present → `reclassified_by_reva`); otherwise `absent_from_delivery` ("not checked"). `load_batch(retract_reason="removed_at_source")` only when the caller verified the removal. **Old rows**: `reva causal annotate-retractions` appends one annotation per `removed_at_source` row whose retracting load carries the retracted row's raw sha256 (evidence `raw_unchanged`, raw sha256, creating / retracting load ids and times, revisions, connector, relations sha256); any other row is left unannotated and counted as `unverified`. | `migrations/versions/0018_retraction_reasons.py`, `reva.causal.model.dataset_retraction_reason`, `reva.kg.model._load_retractions`, `reva.causal.retractions`, CLI |
| N2 | P1480 *sourcing circumstances* (presumably, possibly) ignored; scope qualifiers stored but not worded | grading v3: `grade.D.sourcing_hedged` (cap D), fact `nature.sourcing_circumstances`, kept out of chains (`CHAIN_QUARANTINE_FACTS`). Wording: "Wikidata qualifie : <valeur> (circonstances de la source, P1480)" / "Wikidata qualifies: … (sourcing circumstances, P1480)". P518 / P1001 / P1011 / P1012 / P1013 worded "(s'applique à la partie : … ; à l'exclusion de : … ; y compris : …)", fact `scope.qualified` (grade unchanged). Claim JSON `attribution.sourcing_circumstances` / `scope_qualifiers`; NLQA evidence and sentence; UI badges. | `grading.yaml`, `grading.py`, `grader.py`, `graph.attribution_of`, NLQA, UI `api.ts` / `parts.tsx` |
| N3 | Parties and nature values shown as bare QIDs | Every ref without a KG label reads `Q7747 (label not loaded)` / `Q7747 (libellé non chargé)` with `label_loaded: false` — never a bare id, never a hand-written label. NLQA French sentences use the French label (or that stand-in). `reva causal queue-labels` lists the items named by claims without an en/fr KG label and puts them at tier 1 of `var/kg/harvest_state/causal.causwd.json` (no network). | `graph._ref`, `reva.causal.labels`, NLQA `compose`, UI `partiesFr` |
| N7 | Coded outcomes dated by the cause's own end: `consistent` by construction, counted toward A | When an `outcome`-lane claim's effect start window equals the cause's end window, the temporal detail says `reason: effect_dated_by_cause_end` (with both windows), fact `temporal.effect_dated_by_cause_end`, and grade A requires `effect_dated_by_cause_end: false`. The check value itself is kept. | `grader.outcome_dated_by_cause_end`, `grading.yaml` |
| N9 | Causal API carried no licence | `source.rights` per claim: the source registry's rights (latest `evidence.source_metadata_version`, else `evidence.source`, as `/v1/sources`), normalised by the atlas rule (`reva.atlas.snapshot.normalise_rights`); fallback to the raw artifact's `meta.artifact.rights`; `null` when nothing is recorded (never "open"). Envelope warning `licence_restricted` naming each restricted source (same text shape as atlas LIC-1). NLQA evidence `citation.rights` and its licence note now cover causal claims. UI reads `source.rights` first (registry map as fallback). | `graph.source_rights`, `graph.licence_warning`, route `_env`, NLQA, UI |
| N11 | A Wikidata statement with a DOI reference graded B like an expert dataset | Decision per `docs/roadmap/v3_causality.md` (T2 = "scholarly reference with a citation stored verbatim — only when such a reference arrives through a source's own references"): the tier stays **T2**, but a community-edited statement whose evidence is that reference is not an expert finding — REVA has not checked that the work asserts the link. Uncorroborated → **C** (`grade.C.t2_wikidata_single`, fact `tier.T2.wikidata_reference`); corroborated by an independent group → the T1/T2 rules. A dataset's own T2 claim is unchanged. | `grading.yaml`, `grader.py` |
| M1 rem. | 625 Wikidata claims with a class endpoint shown as causes in `/why` | `/why` files a claim whose cause or effect is a class (the KG's own P279, or a P31 target without P31: `graph.class_nodes`) in a new lane **`generic`** ("énoncés génériques (classe) ; pas des causes de cet événement"), with `generic: {class_endpoints, rule}`; counted in `generic_claims`, not in `causal_claims`; summary says so. NLQA counts them apart and words each "(énoncé générique : la cause est une classe …)". UI group "Énoncés génériques (classe)". | `graph.why`, `vocab.GENERIC_LANE`, `relations.yaml` lane label, NLQA, UI |
| M2 rem. | Chains routed through entities missing from the KG (e.g. Q8698) | Decision: **never traverse** them as intermediate nodes (an endpoint the reader asked for is kept). Unknown = no `kg.entity` row and no statement about it (the loader's `unknown_endpoint_claims` rule). `search.unloaded_nodes_skipped` / `_sample`, `node_rule`, and the not-found reason name them; UI "entité(s) non chargée(s) … non traversée(s)". | `graph.unloaded_nodes`, `graph.chain`, UI `ChainFinder` |

Also: the chain not-found reason now says "attributed/qualified/hedged by Wikidata"; `contracts/openapi.json` and
`migrations/schema_contract.json` regenerated (head `0018_retraction_reasons`); `docs/dev/v3_causal_interface.md`
updated (storage, retraction reasons, version 3, claim JSON fields); `README.md` head.

## 2. Dev DB `reva` (origin live) — before / after

Order: migration `0018` (`flock /tmp/reva-devdb.lock uv run reva db upgrade`; `reva db verify-schema`: matches the
contract, fingerprint `4f8e1eec…`), `reva causal queue-labels`, `reva causal annotate-retractions --origin live`,
then `flock /tmp/reva-devdb.lock uv run reva causal refresh --origin live` (04:1x UTC).

**FIXB was loading at the same time.** Between my "before" snapshot (51,446 current claims) and the refresh, FIXB
re-delivered IDMC and UCDP (loads 54–55): 1,210 IDMC claims were retracted and 21,820 claim rows are new
versions, so the current total is **50,243**. Those changes are FIXB's; the grade deltas below that are mine are
isolated by rule id. FIXB's IDMC retraction (load 54) already ran the new loader code and is stored as
`reclassified_by_reva`, basis `raw_unchanged` (same workbook bytes) — the first retractions with an honest reason.

### Retractions (N1)

| | before | after |
|---|---:|---:|
| claim retractions, recorded `removed_at_source` | 4,264 | 4,264 (rows unchanged) |
| … with an annotation `reclassified_by_reva` (evidence `raw_unchanged`) | 0 | **4,264** (PA-X 3,334, UCDP PA 382, Archigos 327, MEC 90, NAVCO 1.3 62, NAVCO 1.2 37, ESRB 27, ICB 5) |
| KG statement retractions, recorded `removed_at_source` | 1,635 | 1,635 (rows unchanged) |
| … with an annotation `reclassified_by_reva` | 0 | **1,635** (`archigos:coup`, load 395 note "AUDFIX M5: coup records superseded") |
| `unverified` (raw differs, not annotated) | — | 0 / 0 |
| new claim retractions written with a derived reason | — | 1,210 `reclassified_by_reva` (FIXB's IDMC load 54) |

`kg.retraction_reason_current`: 5,474 claim retractions and 1,635 statement retractions, every `effective_reason`
= `reclassified_by_reva`; 0 with `removed_at_source` as effective reason. A second annotate run inserts 0.

### Grades (grading v3, load 56: 50,243 grade rows written, every current claim; 0 ungraded)

| | v2 (before, 51,446 claims) | v3 (after, 50,243 claims) |
|---|---|---|
| A / B / C / D / E | 0 / 48,979 / 160 / 2,259 / 48 | 0 / 47,767 / 158 / 2,262 / 56 |

Transitions caused by version 3 (same claim row, v2 → v3): **C → D 3** (`grade.D.sourcing_hedged`: 75168 Itaewon
"presumably", 75438 / 75605 "possibly"; 75454 was already D), **B → C 1** (`grade.C.t2_wikidata_single`: 75505,
K–Pg extinction, DOI 10.1126/science.adk4868). Every other grade difference between the two tables comes from claim rows that are new
in FIXB's re-deliveries (no v2 grade; 21,820 rows: B 21,810, E 10). A dry run of v2 and v3 on the 51,446 claims of the before snapshot gives
exactly these 4 transitions and nothing else.

Facts on current grades: `temporal.effect_dated_by_cause_end` **7,531** (before the UCDP re-delivery: 7,538, the
audit's count exactly; now consistent 6,336 / overlapping 1,193 / inconsistent 2; COW 3,266, ICB 1,633, UCDP
1,411, TIES 936, EUSANCT 283). No A grade existed or exists, so N7 changes no grade today; it prevents 6,336
corroborated outcome claims from reaching A on a tautology. `nature.sourcing_circumstances` 4, `scope.qualified` 8
(P518 3, P1001 1, P1011 1, P1012 2, P1013 1), `tier.T2.wikidata_reference` 2 (75505 → C; Q84263196's T2 claim was
already D), `attribution.supported_by` 12, `nature.qualified` 26.

### Labels and the harvester queue (N3)

* Items named by current Wikidata claims without an en/fr KG label: **702** = 25 qualifier values (P3680 parties 9,
  P5102 values 7, P1480 2, scope 7 — Q171043 is also an endpoint) + 677 endpoints (659 without any KG entity, the
  others with a KG entity but no en/fr label).
* Wording: all of them now read "Qn (libellé non chargé)" / "(label not loaded)": e.g. 74253 "records that Q7747
  (label not loaded) and Q1140115 (label not loaded) assert that Russo-Ukrainian war has cause eastward expansion of
  NATO"; 75168 "… a pour cause Q109905701 (libellé non chargé) (Wikidata qualifie : Q18122778 (libellé non chargé)
  (circonstances de la source, P1480))". No label was written by hand.
* Queue (`State.add`, tier 1, backup of the state file kept in the session scratchpad): **9 queued new** (the 2
  P1480 values Q18122778, Q30230067 and the 7 scope values — APIFIX had queued the P3680 / P5102 items and the
  endpoints), 674 already at tier 1, 0 moved, **19 already fetched but not in the KG with an en/fr label**
  (they need `reva kg import-harvest --from-db reva_kg_causwd`, or have no en/fr label at the source). 683 items
  at tier 1 in total. Nothing was fetched (no network; CAUSWD's share was 0 today per APIFIX).

### `/why` and chains (M1, M2)

* 625 of 1,672 Wikidata claims have a class endpoint (260 class items; the audit's count). In `/why` they are now
  in lane `generic`. Example `/why Q15860072` (Russo-Ukrainian war): `causal_claims` 4, `generic_claims` 6 (before:
  10 claims in the causal lanes).
* 659 ids unknown to the KG, on 846 claims (unchanged). 13 of them sit between an incoming and an outgoing causal
  edge (44 edges) and are the only ones a chain could route through: Q8698, Q1036897, Q10737, Q13649246, Q168983,
  Q1964968, Q267505, Q27824398, Q28113354, Q3196, Q81341687, Q961603, Q97738250. They are no longer traversed.
  `/chain Q201684→Q186356` (Wall Street crash → New Deal): the path through "Q8698" is gone; one path remains, via
  Q696482 (Great Depression in the United States), grade D, with `unloaded_nodes_sample: ["Q8698"]`.
* `/chain Q80011627→Q82955730` (the audit's "possibly" chain, grade C before): not composed now
  (`quarantined_skipped: 1`). `/chain Q110999040→Q454`: still not composed (5 class nodes).

### Licences (N9)

All 12 sources of current claims have registry rights; 4 are restricted under the atlas rule — `idmc.gidd`
(CC BY-NC 4.0, 20,898 claims), `cow.data` (not an open licence, cannot charge), `icb.data` (no open licence stated),
`esrb.fcdb` (website copyright terms) — 28,671 claims, each with `source.rights.restricted = true` and the envelope
warning `licence_restricted`. `ties.unc` ("no licence stated") has `open_licence: null` and is not flagged
restricted, as on the atlas side (unknown, never shown as open).

## 3. Tests and checks

* `tests/unit/causal/test_fixa.py` (16): version-3 table and decisions (hedged, Wikidata T2 alone / corroborated,
  dataset T2 unchanged, outcome dated by its cause's end not A), grader facts on synthetic claims, the retraction
  reason derivation (4 cases), migration 0018 statics, unloaded labels, P1480 / scope wording, licence warning.
* `tests/integration/test_fixa_store.py` (7, database): dataset retraction reasons (same raw → reclassified,
  changed raw → absent_from_delivery, verified → removed_at_source, with `detail`); KG statement retraction
  reasons (3 cases); a legacy `removed_at_source` row annotated with evidence, idempotent, the trigger refusing an
  annotation that does not restate its row, UPDATE refused; P1480 claim D, worded, not chained, P518 worded; `/why`
  generic lane and counts; chains not routing through an unloaded entity (and routing once it is loaded); IDMC
  claim carrying CC BY-NC `source.rights` and the warning.
* Updated: `test_causal_grading.py` (version 3, all 2⁷ flag × tier × check × corroboration combinations decided),
  `test_causfix_store.py` (the unlabelled party now reads "(label not loaded)"), `test_audfix_loader.py` (same raw
  → `reclassified_by_reva`), `test_atlas_coups_ingest.py` (a seeded 1.0.0 raw ≠ the 1.1.0 delivery →
  `absent_from_delivery`), `test_nlqa_causal.py` (the French answer names the party by its French label).
* `ui/src/v3/causal/fixa.test.tsx` (5): API `source.rights` parsed and shown on the claim card without the
  registry, `/why` generic lane (not a cause; empty-causes message), chain `unloaded_nodes_*`, unloaded party and
  P1480 hedge.
* NLQA UI fixture regenerated (`REVA_REGEN_UI_FIXTURE=1 … -k ui_fixture`): the only diff is the two new evidence
  keys `citation.rights` and `generic` (both `null`) on the 8 causal evidence items.
* Results: `ruff check` / `ruff format --check` clean on every touched file; `mypy` (project config) clean, and no
  new error in the touched modules (pre-existing ones in `kg/model.py`, `routes/causal.py:610`, `cli/main.py`,
  `nlqa/executor.py` untouched); pytest `tests/unit/causal tests/unit/nlqa tests/unit/events tests/contracts
  tests/invariants` + links retention statics: **582 passed, 1 skipped**; integration (causal store / api / ops /
  sameas / wikidata demo / causfix / audfix / causdata / causmore / events / nlqa / schema contract / api contract
  + `test_fixa_store`): **95 passed, 3 failed** — `test_causmore_ingest::test_idmc_ingest`,
  `test_events_dataset_kinds::test_causal_claims_attach_to_typed_events` (`war_outcome` kind) and
  `test_events_datasets::test_cow_war_interval_is_derived_from_spells`, all on the IDMC / COW connectors and
  fixtures FIXB was editing during the run (`atlas_idmc.py`, `atlas_cowwars.py`, their test fixtures); not
  touched here. UI: `npx tsc --noEmit` clean, `npm run test` **248 passed, 1 skipped**.
  Re-run after the last edits: `tests/unit/causal tests/unit/nlqa` + integration `test_fixa_store`, `test_causfix_store`,
  `test_causal_api`, `test_causal_store`, `test_audfix_loader`, `test_causal_sameas`, `test_api_contract`,
  `test_schema_contract`, `test_nlqa_api`: **313 passed**; `tests/security` (grants, immutability, hardening):
  **674 passed, 5 skipped**; `scripts/export_contracts.py --check` and `scripts/update_schema_contract.py --check`:
  up to date.

## 4. Audit ids

| Audit id | Status |
|---|---|
| N1 | fixed: reasons derived by both loaders; 4,264 + 1,635 old rows annotated `reclassified_by_reva` with the same-sha evidence (rows unchanged) |
| N2 | fixed: P1480 capped D, worded, never chained; scope qualifiers worded |
| N3 | fixed on the display side (no bare QID anywhere, "(libellé non chargé)"); the labels themselves wait for the CAUSWD harvest (all 702 items at tier 1; 19 fetched need an import) |
| N7 | fixed: `effect_dated_by_cause_end` reason + fact, excluded from A |
| N9 | fixed: `source.rights` per claim + `licence_restricted` warning; UI wired to the API field |
| N11 | fixed: T2 kept, Wikidata T2 alone capped at C (documented in grading.yaml v3) |
| M1 | fixed (display): class-level statements in the `generic` lane of `/why`, not counted as causes |
| M2 | fixed for chains: unknown entities never intermediate nodes (13 such junctions, 44 edges); the KG gap itself (659 ids) waits for the harvest |
| H3 | the remaining part (claimants unreadable) is N3 above |
| L8 | the remaining part (reasons) is N1 above |
| N4, N5, N6, N8, N10, N12 | FIXB (dataset connectors), not in this report |

## 5. Follow-ups

* When CAUSWD has budget: harvest the 683 tier-1 items, `reva kg import-harvest --from-db reva_kg_causwd`, then
  `reva causal refresh --origin live` — the "(libellé non chargé)" stand-ins and the 13 unloaded junctions go away
  by themselves; nothing else needs to change.
* Connectors that can verify that a row left the provider file should pass `source_row_present` /
  `retract_reason="removed_at_source"`; until then a changed file gives `absent_from_delivery`, which is honest.
* `/v1/causal/events/{id}` counts do not split generic statements out yet (only `/why` does).
