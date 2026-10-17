# V3 AUDFIX: dataset and loader fixes for the causal audit

Agent AUDFIX, 2026-10-17. Scope: the dataset / loader-data side of `docs/dev/review_v3_causal_audit.md`
(H4 data part, M2–M7, L1–L5, L8). Grading rules and Wikidata tiering are CAUSFIX's; `same_as` and corroboration
are SAMEAS's.

## Interface for CAUSFIX: the `source_validity` claim qualifier (H4)

A dataset claim whose source codes the certainty or validity of the coded event or of the coded link carries the
key **`source_validity`** in `kg.causal_claim.scope` (`DatasetClaim.scope["source_validity"]`). It is a list
(one entry per coded certainty field), each entry verbatim from the source's own code list:

```json
{
  "source_validity": [
    {
      "field": "eventValidity",
      "code": "1",
      "label": "Very Doubtful Tsunami",
      "codelist": "NCEI descriptor tsunami/validity",
      "codelist_sha256": "081f620e…",
      "certainty": "doubtful"
    },
    {
      "field": "causeCode",
      "code": "2",
      "label": "Questionable Earthquake",
      "codelist": "NCEI descriptor tsunami/causes",
      "codelist_sha256": "ccb91b45…",
      "certainty": "doubtful"
    }
  ]
}
```

* `code` and `label` are the source's code and its label, copied from the archived code list (`codelist_sha256`
  is the archived response that holds the label).
* `certainty` is the registry reading of that code. Its values are `doubtful` and `definite_or_probable`. The
  code → certainty mapping is declared per source in the registry with the source's label (NOAA:
  `registry/causal/relations_datasets.yaml` → `source_validity.noaa_tsunami`). It is never inferred from text.
* A claim gets one entry per coded certainty field. A claim whose source codes no such field has no
  `source_validity` key (the absence means "not coded", not "certain").
* Grading hook for CAUSFIX: any entry with `certainty: doubtful` should lower the grade (e.g. a fact rule
  `certainty.doubtful`). The NOAA entries ship with the `atlas.noaa_hazards` connector version 1.1.0.

NOAA codes:

| field | code | NCEI label (descriptor) | certainty | claim |
|---|---|---|---|---|
| eventValidity | −1 | Erroneous entry | — | no claim (excluded, as before) |
| eventValidity | 0 | Event that only caused a seiche | doubtful | claim with qualifier |
| eventValidity | 1 | Very Doubtful Tsunami | doubtful | claim with qualifier |
| eventValidity | 2 | Questionable Tsunami | doubtful | claim with qualifier |
| eventValidity | 3 | Probable Tsunami | definite_or_probable | claim with qualifier |
| eventValidity | 4 | Definite Tsunami | definite_or_probable | claim with qualifier |
| causeCode | 2 | Questionable Earthquake | doubtful | claim with qualifier |
| causeCode | 1, 3–7 | (descriptor label) | definite_or_probable | claim with qualifier |


## Defects fixed (dataset / loader side)

| # | What was wrong (audit) | Fix | Where | Connector |
|---|---|---|---|---|
| H4 (data) | NCEI's validity / cause-certainty codes were dropped: 788 + 23 doubtful tsunamis looked definite | Every NOAA claim carries `scope.source_validity` (above). "Erroneous entry" (−1) stays excluded: it is the only code NCEI's code list defines as not a real record; 0 "only caused a seiche" is kept with `certainty: doubtful` because NCEI still names the source event of that record. The CAUSFIX rule `grade.D.source_doubtful` reads it. | `atlas_noaa_hazards.source_validity`, `relations_datasets.yaml` `source_validity` | `atlas.noaa_hazards` 1.1.0 |
| M2 | `load_kg_claims` did not count claims whose cause/effect is not in the KG | Both loaders count and sample them (`unknown_endpoint_claims`, `unknown_endpoint_sample`, also in the load parameters). Nothing is dropped or invented. | `model._count_unknown_endpoints` | — |
| M3 | MMP state responses, MEC and NAVCO 1.2 regime violence were dated with the protest's / campaign's dates, Archigos coups with the exit day, then reported `overlapping` | The source codes no date for these effects: their `P580`/`P582` are now Wikidata "unknown value" (`{"type": "somevalue"}`) with the typed-missing qualifier `{"missing": {"kind": "missing", "reason": "not_collected"}, "note": …}`; the claim scope carries `effect_time` (same typed missing). The temporal check is `undetermined` with `undated: ["effect"]`, a reason and `effect_time_missing`. Archigos: no claim any more (M5). | `causdata_common.missing_date_qualifiers`, `atlas_navco.Kg.missing_time`, `temporal.temporal_detail`, `grader.compute` | navco/mec/massmob 1.1.1 |
| M4 | Codebooks of NAVCO, MEC, MMP, TIES, Archigos, ESRB never archived; quotes unverifiable | See "Codebooks archived" below | `causdata_common.PinnedCodebook/fetch_codebook/codebook_sources`, `Codebook.sources` → `codebook.quote_source` | navco/mec/massmob/archigos/ecrises/ties |
| M5 | Archigos: one coded attribute (exit + exitcode) turned into coup event + removal + `coded_outcome` claim; "other government actors" called coups; "without foreign support" passage quoted for removals with foreign support | One removal event `archigos:exit:<obsid>` with the exitcode verbatim, `removed_by` / `foreign_support` (the two parts of that label), and the codebook passage of **that exact exitcode** when the codebook has one (else the general EXITCODE passage, said so). No claim (empty complete delivery retracts the 327). The 1.0.0 `archigos:coup:*` statements (1,635) are retracted; the entities are re-emitted as `superseded_record`; the taxonomy no longer types `archigos:coup:` as an event. The Powell & Thyne context link now starts from the removal event (the P&T host answered 403 to the platform client in both runs of the day, so no link exists yet). | `atlas_leaders` | `atlas.archigos` 1.1.1 |
| M6 | UCDP PA "conflicts related to the dyads signing" turned into `reaction_to` | Context only: the existing statement `ucdppa:conflict` (agreement → conflict); the `peace_agreements` delivery stays, empty and complete, so the 382 claims are retracted | `atlas_ucdp_term`, `relations_datasets.yaml` | `atlas.ucdp_term` 1.1.1 |
| M7 | ESRB claims regex-extracted from a "Format: text" field; mechanism cut at 4,000 characters without a mark | A cell gives claims only when its first paragraph is exactly a comma-separated list of the five objective names. 13 cells (27 claims) that name objectives inside narrative, abbreviate them ("excessive credit growth", "mismatches in balance sheets") or number them give no claim and are listed (`accelerator_not_a_list`). Mechanism = the raw cell, byte-equal (no strip); a cell over the 4,000-character column is stored as its first 4,000 characters with `scope.mechanism_truncated {stored_chars, full_chars, full_text_cell}` (4 claims). | `atlas_ecrises.accelerator_objectives`, `relations_ecrises.yaml` | `atlas.ecrises.*` 1.1.0 |
| L1 | NAVCO 2.1 `success = 0` outcomes cited the `success` definition | `codebook_term = progress` when success = 0 (180 claims) | `atlas_navco.n21_claims` | navco 1.1.1 |
| L2 | `coded_outcome` asserted for campaigns coded ONGOING | No outcome claim for them (the coded "failure" is censoring); the KG keeps the coded outcome with its `as_of` | `navco1_claims`, `mec_claims` | navco/mec 1.1.1 |
| L3 | ICB "No response-inaction" worded as a provoked reaction; MMP "ignore" silently dropped | One policy: inaction is coded but is never `reaction_to`. ICB MAJRES = 1 keeps its `icb:response_type` attribute, no claim (5 retracted); MMP claims keep the full coded response list verbatim (`selector.coded_responses`, `inaction_not_a_reaction: ["ignore"]`, 296 claims), protests with only "ignore" still give no claim | `atlas_icb`, `atlas_massmob.mmp_claims` | `atlas.icb` 1.1.0, massmob 1.1.1 |
| L4 | KG import keeps only allow-listed reference / qualifier properties, so 11 claims were T4 despite a reference and 20 lost qualifiers (P1001, P1011, P1012…) | `load_kg_claims(store=…)` (used by `reva causal refresh`) reads each statement's references and qualifiers verbatim from the archived raw entity JSON (compacted like the harvester, every property; temporal qualifiers stay the claim period) and tiers on them; `selector.refs_qualifiers` says so; a raw that cannot be read falls back to the KG copy, counted (`refs_source_counts`) | `model._RawStatements` | — |
| L5 | MID `outcome` quote ended "…the status quo took"; NOAA definitions began with a help-page sentence typed in the connector | MID quote completed ("took place."); NOAA definitions are only the archived descriptor labels (`causeCode 2 = "Questionable Earthquake" (NCEI descriptor tsunami/causes)`) | `relations_datasets.yaml`, `atlas_noaa_hazards.codebook_definitions` | `atlas.mid` 1.0.1, noaa 1.1.0 |
| L8 | Retraction never exercised live; somevalue/novalue mislabelled `source_statement_retracted`; complete → partial downgrade silent | Retraction exercised in the dev DB through the loader (below) and in throwaway-DB tests; a statement whose value stopped being an item is retracted `no_longer_causal` with the detail `value_not_an_item` (report + load parameters; no migration); loads record `complete_requested`, `complete_effective`, `complete_downgrade_reason` | `model.load_kg_claims`, `load_dataset_claims` | — |

KG revisions: the KG loader treats "same statement key, same source revision, different content" as a conflicting
source and does not store it. The first 1.1.0 runs of NAVCO and UCDP hit that (5,285 and 382 statements not stored),
because REVA re-wrote statements of an unchanged provider file. From 1.1.1 the KG source revision of NAVCO, MEC, MMP and
Archigos names the REVA representation too (`…+reva-1.1.1`): unchanged content is not re-stored, changed content is a
new revision. UCDP 1.1.1 keeps the 1.0.0 `ucdppa:conflict` content instead (the context reading lives in the registry).
## Codebooks archived (M4)

Each codebook is now fetched through the run's metered client (`causdata_common.fetch_codebook`), archived as an
`evidence.acquisition` of the connector version, and compared with its pinned sha256. A changed codebook withholds
that file's claims (`claims_withheld`), as `atlas_icb` / `atlas_mid` / `atlas_ucdp_term` already did. Each T1 claim
carries `codebook.quote_source`: the archived document, its sha256, the page(s) (PDF, 1-based physical page) or
paragraph (docx body), and the extraction used. The sha256 is also one of the load's raw objects.

| source | codebook (pinned sha256) | quotes | verbatim check against the archived bytes |
|---|---|---|---|
| NAVCO 1.2 | `NAVCO 1.2 Codebook.pdf`, Dataverse 3621197 (`6743dc61…`) | 9 | **4 fixed**: SUCCESS, DEFECT and EYEAR were cut short (SUCCESS lost "Note that where information was available regarding the campaign’s outcome in 2014, the most current information was used."); FAILURE and DEFECT had ASCII quotes for the printed “ ” ’ |
| NAVCO 1.3 | `NAVCO 1.3 Codebook List.pdf`, 3769105 (`8a265f00…`) | 6 | **1 fixed**: EYEAR cut before "(i.e. if the regime changed in 2002, the peak is 2002)." |
| NAVCO 2.1 | `NAVCO_2.1_codebook.pdf`, 3648624 (`6fe8faa4…`) | 5 + 3 repression levels | **5 fixed**: the three repression-level texts replaced the printed bullets "●" by ";" and level 1 / 3 dropped items ("maintain the status quo during conflict", "mass violence"); start_date / end_date completed with their printed format notes |
| MEC v3.0 | `MEC_Codebook_2024.docx`, 11543083 (`3d5b0133…`) | 6 | **1 fixed**: ONGOING lost "Note that campaigns that were coded as ongoing as of December 31, 2018 were also coded 1 for failure." |
| MMP v5.1 | `MM_users_manual_0515.pdf`, 2775560 (`03541007…`) | 5 | all verbatim |
| TIES 4.1 | `tiesusersmanualv4.pdf` (`862fe788…`) | 1 | verbatim once the PDF's ligature glyphs are read as the letters they print (the text layer maps "fi" to U+2026 and "ff" to U+00A4); pages 12–13 |
| Archigos 4.1 | `Archigos_4.1.pdf` (`6be0ec5c…`) | 4 | all verbatim (pages 9–10) |
| ESRB | ESRB Occasional Paper 13, `esrb.op13.en.pdf` (`18a5343d…`) | 1 | **fixed**: the quote added a colon after the field name, wrote "-" for the printed "–", and appended REVA's own "(ESRB Occasional Paper 13, Annex A)" inside the quote; page 44 |

The CAUSDATA quotes (ICB1, ICB2, MID, UCDP termination, UCDP PA) were re-checked the same way: 52 of 52 verbatim at
their declared pages, after the MID `outcome` completion (L5).

Fixtures: the CC0 codebooks (NAVCO, MEC, MMP) and ESRB OP13 ("free use with citation") are captured byte for byte in
`tests/fixtures/source/atlas.{navco,mec,massmob,ecrises.esrb}/`; `tests/unit/test_audfix_codebooks.py` re-checks
every quote against them (poppler `pdftotext`; docx parsed in Python). The TIES and Archigos files state no
redistribution grant, so they have no fixture; in fixture mode a codebook without a captured fixture is reported
(`fixture_missing`) and the claims carry no `quote_source`. Live, a missing or changed codebook stops the claims.

## Dev-DB re-ingest and results (2026-10-17, origin `live`)

Connector versions were bumped, `reva seed` registered them (the registration check reports `ok` for every touched
connector: `atlas.noaa_hazards(_wikidata)` 1.1.0, `atlas.icb` 1.1.0, `atlas.mid` 1.0.1, `atlas.ucdp_term` 1.1.1,
`atlas.navco` / `atlas.mec` 1.1.1, `atlas.massmob` 1.1.1, `atlas.archigos` 1.1.1, `atlas.ecrises.*` 1.1.0,
`atlas.ties` / `atlas.eusanct` 1.1.0), then each source was ingested alone with
`flock /tmp/reva-devdb.lock uv run reva atlas ingest <source>`, and finally `reva causal refresh --origin live`.
Budgets: `audfix.yaml` merged as budgets 1.3.11 / 1.3.12 (digest re-pinned in `src/reva/autonomy/policy.py`).

| source | requests | claims: new revision / retracted | KG |
|---|---:|---|---|
| `atlas.ucdp_term` | 5 | term 0 / 0 (505 unchanged); **peace_agreements 0 / 382** | 382 PA statements conflicting in the 1.1.0 run (not stored), content restored in 1.1.1 |
| `atlas.icb` | 5 | icb2: **5 retracted** (MAJRES = 1) | unchanged |
| `atlas.mid` | 3 | **23 new revisions** (completed `outcome` quote) | unchanged |
| `atlas.navco` (×2: 1.1.0, 1.1.1) | 11 + 11 | navco12 704 / **37**, navco13 558 / **62**, navco21 2,907 / 0 | 5,285 new revisions (typed-missing dates, L1 qualifier) |
| `atlas.mec` | 3 | 3,414 / **90** | 2,230 new revisions |
| `atlas.massmob` | 3 | 7,221 / 0 | 14,442 new revisions |
| `atlas.archigos` | 4 | 0 / **327** | 1,926 new revisions, 327 superseded entities, **1,635 coup statements retracted** |
| `atlas.ecrises.esrb` | 2 | 69 / **27** | unchanged |
| `atlas.ties` | 3 | 1,024 / 0 | unchanged |
| `atlas.noaa_hazards` (×2) | 239 + 239 | 2,186 + 3 (label fix) / 0 | unchanged |
| `reva causal refresh` | — | Wikidata 1,672 new revisions (refs/qualifiers verbatim), 0 retracted; unknown endpoints counted: 846 claims (659 entities) | — |

The second NOAA run fixed a registry error found in the first: the YAML flow mapping split the label "Volcano,
Earthquake and Landslide", so 3 cause-code-5 claims were `unclassified`; labels are now quoted and a test checks every
registry label against the archived descriptor.

**Before / after per defect** (current live claims; before = 52,376 claims at 02:40 UTC, after = 51,446):

| # | before | after |
|---|---|---|
| H4 | 2,186 NOAA claims, none with NCEI's codes; doubtful (validity 0–2: 788, cause code 2: 23) graded B (2,171 B, 15 E) | 2,186 with `source_validity`; 791 have a `doubtful` entry → **783 D** (CAUSFIX rule) + 8 E (temporal), 1,388 B, 15 E |
| M2 | Wikidata loader: not counted | **846** of 1,672 Wikidata claims (659 entities) reported by `load_kg_claims` (`unknown_endpoint_claims` + sample); the audit's 324 was at 1,002 claims |
| M3 | 9,015 `overlapping` on copied dates (MMP 7,221, MEC 1,115, NAVCO 1.2 352, Archigos 327) | **0**: 8,688 `undetermined` with `undated: ["effect"]` + `effect_time_missing`; Archigos claims gone (M5) |
| M4 | 16,440 claims quoting unarchived codebooks | 0: all 15,897 remaining claims of these sources carry `codebook.quote_source` (NAVCO 4,169, MEC 3,414, MMP 7,221, TIES 1,024, ESRB 69); Archigos quotes on the KG removal events; 8 codebooks archived live (`evidence.acquisition`) |
| M5 | 327 coup → removal claims; 1,635 coup statements | **0** claims, 0 current coup statements |
| M6 | 382 `reaction_to` | **0** (context `ucdppa:conflict`) |
| M7 | 96 regex claims; 6 silently cut at 4,000 | **69** list-coded claims; 4 marked `mechanism_truncated` |
| L1 | 180 `success = 0` citing `success` | 0 (180 cite `progress`) |
| L2 | 189 outcome claims for ONGOING (MEC 90, NAVCO 1.2 37, 1.3 62) | **0** |
| L3 | ICB 5 inaction reactions; MMP "ignore" dropped from 296 claims | ICB **0**; MMP 296 claims keep the full list, inaction marked |
| L4 | 1,672 Wikidata claims with the allow-listed refs only (T3 179, T4 1,491) | 1,672 verbatim from the raw JSON (T3 190, T4 1,480: +11 T3, the audit's 11) |
| L5 | MID `outcome` cut (23 claims cite it); NOAA help prefix on 2,186 | **0 / 0** |
| L8 | retractions exercised only in tests | **930** claim retractions through the loader this run (382 + 5 + 37 + 62 + 90 + 327 + 27), 1,635 KG statement retractions; tests on throwaway DBs |

Totals: grades B 50,692 → 48,979, C 154 → 160, D 1,482 → 2,259, E 48 → 48; temporal overlapping 38,759 → 29,726,
undetermined 1,586 → 10,247. (Other agents' loads ran in between: CAUSMORE's claims are in both snapshots.)

## Tests

* `tests/integration/test_audfix_loader.py` (throwaway DB): unknown-endpoint counting; somevalue → `no_longer_causal`
  + `value_not_an_item`; a complete delivery minus one row retracts that row (append-only, reinstated when it comes
  back); `complete_requested/effective`; an empty complete delivery retracts all; `quote_source`; verbatim refs and
  qualifiers from raw (L4).
* `tests/integration/test_atlas_coups_ingest.py`: a database holding the 1.0.0 Archigos coup statement and claim is
  cleaned by the 1.1.x run (statement and claim retracted, rows kept).
* `tests/unit/test_audfix_codebooks.py`: every quote verbatim at its declared page / paragraph in the archived codebook
  bytes (fixtures), pins and locations of TIES / Archigos.
* Connector tests updated: NOAA `source_validity` and label registry, ICB inaction, UCDP PA context, NAVCO/MEC/MMP
  (typed-missing dates, ONGOING, L1, L3, codebook delivery), Archigos one-event model, ESRB strict lists and truncation
  marker, event-kind test (no `archigos:coup` events).
* Unit suite: 2,259 passed. One unrelated failure deselected: `tests/unit/nlqa/test_nlqa_causal.py::
  test_ui_fixture_is_the_current_pipeline_output` (the NLQA catalog digest changed with new atlas catalog concepts;
  the UI fixture in `ui/src/v3/causal/`, being edited by the UI agent, needs regenerating).

## Not done / residuals

* M2: frontier entities are counted, not harvested; claims with a missing endpoint are not kept out of chains here
  (CAUSFIX's chain rules apply). KG entity retraction does not exist, so retracted endpoints are only visible as
  unknown endpoints.
* M5: the Powell & Thyne coup page answers HTTP 403 to the platform client, so the context link to P&T events is not
  in the dev DB (it was not there before either).
* M7 candidates: the 27 dropped ESRB cells are listed in the build report, not inserted into `kg.causal_candidate`
  (the candidate space is for Wikipedia text / models, T5/T6).
* CAUSMORE's PA-X `reaction_to` (UCDP conflict → PA-X agreement) has the same shape as M6; CAUSMORE already moved it to
  context (`atlas.pax` 1.1.0).
