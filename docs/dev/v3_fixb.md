# V3 FIXB: dataset-side fixes of the second causal audit

Agent FIXB, 2026-10-17. Scope: the dataset side of `docs/dev/review_v3_causal_audit2.md`, defects **N4, N5, N6, N8,
N10 and N12**. The loader, grading, retraction reasons (N1), API and UI are FIXA's (`src/reva/causal/**`); nothing
there was edited. What the causal core may want from these changes is listed in "Needs for the causal core" at the
end.

## Summary

| # | Audit finding | Fix | Connector |
|---|---|---|---|
| N4 | 1,206 IDMC claims rest on preventive evacuations or a hazard *risk*, worded "X has effect displacement" | **1,210 rows give no causal claim** any more. Each row is a context statement `idmc:displacement_in_anticipation_of` (displacement → event) that names its basis. The 1,210 claims were retracted through the loader (reason `reclassified_by_reva`: same raw bytes). | `atlas.idmc` 1.1.0 |
| N5 | 1,103 claims rest on "… - 01/01/YYYY" events stored at day precision | The archived README does **not** say that 01/01 means year-only or aggregate, and REVA does not claim it does. REVA does not rely on the day either. **1,400 events** whose coded start is 1 January now have their start at year precision. The coded value is kept verbatim (`coded_start`) with the reason (`day_not_relied_on`). No day is invented. | `atlas.idmc` 1.1.0 |
| N6 | 7 UCDP dyad episodes with two terminal rows: the last was kept silently (4 with different outcomes) | **Every terminal row is kept.** There is one termination record and one claim per row, and each claim carries the note `source_duplicate_terminal_rows`. Claims whose outcomes differ contest each other, which gives grade E (contested). The same fix applies at the conflict level (2 episodes). | `atlas.ucdp_dyadterm` 1.1.1, `atlas.ucdp_term` 1.2.1 |
| N8 | CAUSMORE claims carry no `codebook.quote_source`; the COW v5.1 counter-evidence was not archived | **All 23,219 claims** of these sources carry `codebook.quote_source`: the sha256 of the archived FILE plus the PDF page or README cell. The COW Intra-State v5.1 zip is archived, and the v4.1 choice is recomputed from its bytes. | `atlas.cowwars` 1.1.0, `atlas.idmc` 1.1.0, `atlas.ucdp_dyadterm` 1.1.1, `atlas.ucdp_term` 1.2.1 |
| N10 | UCDP precision 6 ("year assigned") and 7 ("year missing") stored as an ordinary year | Code 6 is stored at year precision with the typed note `source_date_certainty` (`certainty: assigned`, codebook text verbatim). Code 7 is stored as a typed missing date (Wikidata "unknown value" + `missing {kind, reason: unavailable}`). The placeholder 31 December is never used as a date. | `causdata_common.ucdp_date`; both UCDP connectors |
| N12 | 2,695 claims start from REVA-made identities with no label saying so; 11 collapsed rows invisible in the KG | The 2,695 events are labelled "IDMC record without an Event ID - identity created by REVA" / "enregistrement IDMC sans identifiant d'événement — identité créée par REVA" in en/fr labels and descriptions. The 10 groups of identical rows (11 extra copies) are recorded as the provenance note `idmc:source_duplicate_rows`, with the count and the row selectors. | `atlas.idmc` 1.1.0 |

Every KG source revision now names the connector version (`…+reva-<version>`), because of the loader behaviour that
AUDFIX found: changed content under the same key and revision is not stored. Unchanged content under the new
revision is counted `unchanged`. 0 statements were conflicting or rejected in the four runs.

## N4 — anticipatory displacement (IDMC)

**Codebook, verbatim** (README sheet of the archived workbook `60dd7c1e…f025a2`):

* A24: "Disaster displacement: Refers to situations where people are forced to leave their homes or places of
  habitual residence as a result, or in anticipation of the negative impact of natural hazards."
* A52: "Displacement Occurred: This field contains values that represent if preventive evacuations were reported.
  These evacuations are the result of existing early warning systems."
* A43: "Event Name: Common or official event name for the event, if available. Otherwise, events are coded based on
  the country, type of hazard, location, and event start date."

**Rule (`atlas_idmc.anticipatory_basis`).** A row is anticipatory when at least one of these holds:

* its "Displacement occurred" value is "Displacement reporting preventive evacuations" (IDMC's flag);
* its IDMC-coded event name contains *risk(s)*, *threat(s) of*, *false alarm*, *pre-emptive*, *preventive* /
  *preventative* or *precautionary*. "Evacuated" alone is not enough, because it can follow an impact.

**Why not a relation from `registry/causal/relations.yaml`:**

* `has_effect` asserts that the displacement is an effect of the hazard event, but the hazard may never have struck.
  Examples: "Mexico: Tsunami false alarm - Chiapas", and the "Risk of Landslide" events.
* `reaction_to` ("provoked the reaction") and `precipitated` ("immediate cause") both say that the event itself
  provoked or triggered the move. IDMC instead attributes the evacuations to "existing early warning systems" (A52).
* The row's figure may also mix displacement in anticipation with displacement after an impact, and IDMC does not
  separate the two.

So each such row is a **context statement**, never a claim. It is `idmc:displacement_in_anticipation_of`
(displacement record → IDMC event; an item property, registered as not causal in `CAUSMORE_PROPERTIES`). Its
qualifiers hold the `basis`, the verbatim flag, the event name, the A24/A52 texts, `source_flags_disagree` when the
name says risk but IDMC's flag says "without preventive evacuations reported", and a note. The figure stays on the
displacement record, whose label adds "[in anticipation of the hazard: preventive evacuation or hazard risk, per
IDMC]". The need for a dedicated relation is recorded below.

**Counts (full 2025-05-13 file):** 1,210 rows.

| Basis | Rows |
|---|---:|
| IDMC flag only | 1,148 |
| Flag and name | 30 |
| Name only | 32 (27 of them with IDMC's flag "without preventive evacuations reported", marked `source_flags_disagree`) |

The audit counted 1,206 with a narrower name pattern ("risk" only).

**Example.** Audit claim 50644, `idmc:event:7761` "Sri Lanka: Risk of Landslide - Central (Kandy/Ganga ihala
korale) - 01/07/2020" → `idmc:displacement:7761:LKA:2020`, is retracted (`reclassified_by_reva`). The KG now holds
the context statement with basis `["displacement_occurred", "event_name:risk"]`.

## N5 — 1 January starts (IDMC)

The archived README defines the start date as "Approximate start date of the event" (A44). It says that names
without a common name are coded from "the event start date" (A43). Nowhere does it say that 01/01 means year-only
or an aggregate.

IDMC's IDU API codebook (14/10/2020, consulted, not archived because it is not REVA's source) gives the naming
pattern "Hazard Event Start Date DD/MM/YYYY" and "event_start_date: For disaster events correspond to Hazard
starting day". It does not mention placeholders either. The audit's placeholder or aggregate reading therefore
stays an inference, and REVA does not state it.

What REVA does instead:

* It does not rely on a 1 January day. A start coded as 1 January is stored at **year precision**.
* The qualifiers `coded_start` (the coded value, verbatim), `approximate: true` and `day_not_relied_on {reason:
  start_coded_as_1_january, name_carries_01_01, note}` say so. The note states that the README does not say whether
  1 January is a real day or a placeholder.
* No day is invented, and no aggregate label is added.

The data show why the day is not used:

* 1,089 such events have a name ending in the same "01/01/YYYY".
* 311 have a common name. Examples: "Cyclone 'aila'" (2009-01-01) and "Chile: Tsunami - Countrywide - 2021", whose
  coded name carries only the year.

**Count:** 1,400 events (1,397 current claims rest on them). Four names ending "01/01/YYYY" have another coded start,
for example "Myanmar: Riverbank erosion … - 01/01/2019", which starts 2019-01-10. They keep their coded day at day
precision. Grades are unchanged: an event start at year precision against a displacement year is still `overlapping`.

## N6 — several terminal rows (UCDP)

UCDP codes `d_epterm = 1` on two rows for dyad episodes 64901, 70901, 72202, 83203, 83306, 88201 and 1179801. The
conflict-level file does the same for episodes 20901 and 1528301.

**What is built for such an episode:**

* One `…:termination:<year>` record and one `coded_outcome` claim per terminal row (`source_claim_id
  <epid>:<year>:d_outcome`).
* `scope.source_duplicate_terminal_rows {terminal_rows [line, year, outcome, end date, precision], outcomes_differ,
  note}` on each claim and each time statement.
* A KG note `ucdpterm:source_duplicate_terminal_rows` on the episode.
* The episode's `P582` once per row.

**When the outcomes differ** (dyad 64901, 72202, 83306, 88201; conflict 20901, 1528301), each claim lists the other in
`contests` and carries `scope.contested_by_source: true`. The grader gives `contest.contested` →
`grade.E.contested_uncorroborated` to all 12 claims. That is the source disagreeing with itself, shown rather than
resolved.

**When the outcomes are the same** (70901, 83203, 1179801), both claims are kept and flagged, without contesting each
other.

**Retired records.**

* The 1.0.0 single `…:termination` record is re-emitted as `superseded_record`.
* Its statements are retracted (append-only): 32 dyad statements and 8 conflict statements.
* The 9 former claims (7 dyad, 2 conflict) are retracted `reclassified_by_reva`.

**Example.** Audit claim 55701 (88201, kept 2011 victory side B; dropped 2004 peace agreement) is now two claims,
97300 (2004, `d_outcome=1`) and 97301 (2011, `d_outcome=4`). Both are graded E as contested by source.

## N8 — `quote_source` and the COW v5.1 evidence

**Quote sources (`causmore_common.quote_sources`, `causdata_common.codebook(archived=…)`).** Each claim's
`codebook.quote_source` is `{document, document_key, url, sha256, page | sheet+cell, extraction}`. The sha256 is that
of the file archived by the same run (an `evidence.acquisition`) and equal to the pinned one. The file is also one of
the load's raw objects. The codebook `version` names the full sha256.

| Source | Archived file (sha256) | Location of the quote |
|---|---|---|
| COW (×4) | `875bf75f…`, `0746a2f1…`, `7a15ea58…`, `82164764…` | PDF pages 4 / 8 / 8 / 3 |
| UCDP termination (dyad and conflict) | `648c1601…` | `d_outcome=k`, `c_outcome=k`: pages 3–4 |
| IDMC | the workbook `60dd7c1e…` | sheet README, cell A24 (and the other README cells) |

The IDMC version is now "GIDD disasters 2025-05-13 (idmc_readme sha256 60dd7c1e…)", the workbook FILE. The old value
was a hash of the cell texts, which is kept in the ingest report as `readme_text_sha256`. The tests check that each
quote is verbatim at its page (pdftotext) or cell (workbook).

**PA-X.** The PA-X v10 codebook (`eaf91911…`) was already archived (CAUSMORE). PA-X has 0 claims, so there is no
quote to attach.

**COW Intra-State v5.1.** `atlas.cowwars` 1.1.0 fetches the provider's `Intra-State-Wars-v5.1.zip` through the
metered run. The zip (`b2d1f489…5236`, 887,487 bytes) is archived; its six members include "Codebook for Intra-state
v5.1 2.9.20.pdf" with the "Changes since Guide" list. `v51_numbering_check` recomputes the comparison from the
archived bytes and reports it in the ingest summary; it is never loaded as wars:

* 334 v4.1 wars and 420 v5.1 wars, with 295 shared numbers.
* 39 v4.1 numbers are absent from v5.1. Among PA-X's cited numbers, 790, 818, 836, 870, 892, 923, 930, 931 and 938
  are in this list: the 9 that CAUSMORE said would not resolve.
* 28 shared numbers start in another year.
* 856 is "Eighth Colombia" in v4.1 and "Romanian War of 1989" in v5.1.

A name comparison after REVA's normalisation (128 differ) is a report heuristic only.

## N10 — UCDP precision 6 / 7

The codebook (`648c1601…`), verbatim:

* d_ependprec, pp. 8–9: "6= Year is assigned. There is a wide disagreement between different sources, so that not
  even year can be coded precisely. The end year is assigned based on subjective judgment." and "7= Year is missing.
  No information on the end date is available; Enddate is set to 31 December of the last year recorded in the
  conflict dyad."
* c_ependprec, pp. 6–7: the same, ending "…in the conflict."
* start prec2, p. 13: "…The start year is assigned…" and "7= … Startdate is set to 1 January of the first year
  recorded in the conflict."

**Code 6** gives year precision plus the qualifier `source_date_certainty {code, certainty: assigned, text, codebook,
codebook_sha256, pages}`. The claim scope carries it as `effect_time_certainty`.

**Code 7** gives a typed missing date: P585/P582 `{"type": "somevalue"}` with `missing {kind: missing, reason:
unavailable}` and a note quoting the placeholder. The claim scope carries `effect_time` (AUDFIX's M3 convention, so
the check reads `undetermined`). For a start coded 7, the start year is taken from the episode's first active year
(`d_ep_startyear` / `c_ep_startyear`), and the placeholder is noted.

**Counts in the 2025-06-24 files:**

| Code | Field | Count |
|---|---|---|
| End 6 | Dyad claims | 8 |
| End 6 | Conflict claims (37201) | 1 |
| End 7 | Terminal rows | 0 (handled and unit-tested) |
| Start 6 | Dyad rows / conflict rows | 44 / 34 |
| Start 7 | Dyad rows / conflict rows | 13 / 8 |

The 9 claims stay graded B. Grading on `effect_time_certainty` is the causal core's call (below).

## N12 — REVA-made identities and collapsed rows (IDMC)

**Identities.** The 2,695 events `idmc:event:r<sha256[:16]>` have:

* the label `<name> (IDMC record without an Event ID - identity created by REVA)`, and the fr label `<name>
  (enregistrement IDMC sans identifiant d'événement — identité créée par REVA)`;
* en/fr descriptions saying that the key is the sha256 of the row's fields and that the event is not linked to other
  rows of the same name.

2,693 of these events have current claims; the other 2 rows are anticipatory (N4).

**Collapsed rows.** The 10 groups of fully identical rows (11 extra copies) each leave a string statement
`idmc:source_duplicate_rows` on the kept displacement record. For example: "2 fully identical IDMC rows (rows 496,
497 of sheet 1_Disaster_Displacement_data) loaded once". Its qualifiers are `count`, `rows`, `kept_row` and a note
(no figure is lost), and its selector holds the row list. The groups are rows 194–195, 343–344, 346–347, 407–408,
424–425, 496–497, 636–637, 640–641, 650–652 and 693–694.

## Dev-DB re-ingest (2026-10-17, origin `live`, `flock /tmp/reva-devdb.lock`, one source at a time)

**Setup.**

* `reva seed` registered `atlas.idmc` 1.1.0, `atlas.ucdp_dyadterm` 1.1.0 and 1.1.1, `atlas.ucdp_term` 1.2.0 and
  1.2.1, and `atlas.cowwars` 1.1.0. Every run passed the registration check.
* `atlas.ucdp_term` 1.2.0 was registered but never run. The first `atlas.ucdp_dyadterm` 1.1.0 run did not find the
  1.0.0 statements to retract: the stored statement keys carry the source-key prefix. 1.1.1 / 1.2.1 fix that, with
  an integration test that loads a 1.0.0-style statement and checks that the next run retracts it.
* Budgets: `registry/atlas/budget_requests/fixb.yaml` was merged as budgets-1.3.13 (idmc.gidd 6 → 12,
  ucdp.downloads 30 → 40, cow.data unchanged at 44; daily total 13,184 → 13,200). The digest is re-pinned in
  `src/reva/autonomy/policy.py`. The promotion template digest was not touched.

| Run | Requests (bytes) | KG | Claims |
|---|---:|---|---|
| `atlas.idmc` 1.1.0 | 2 (1.7 MB) | 3,905 entities and 2,620 statements inserted, 1,400 new revisions, 0 conflicting / rejected | 20,898 loaded (new revision: quote_source), **1,210 retracted** (`reclassified_by_reva`) |
| `atlas.ucdp_dyadterm` 1.1.0 | 2 (0.87 MB) | 21 entities (14 per-row terminations + 7 superseded), 109 statements, 38 new revisions | 922 (14 new, 908 new revision), **7 retracted** |
| `atlas.ucdp_dyadterm` 1.1.1 | 2 | 0 inserted; **32 single-terminal statements retracted** | 922 unchanged |
| `atlas.ucdp_term` 1.2.1 | 5 (2.0 MB) | 6 entities, 39 statements, 21 new revisions; **8 single-terminal statements retracted** | 507 (4 new, 503 new revision), **2 retracted**; peace_agreements 0 |
| `atlas.cowwars` 1.1.0 | 9 (1.38 MB, incl. v5.1 zip) | 7,701 unchanged | 892 new revisions (quote_source) |
| `reva causal refresh --origin live` | — | — | 50,245 graded (rules version 3, FIXA's) |

**Before / after** (current live claims of these datasets; before = 04:05 UTC, 51,446 claims in the DB; after =
50,245):

| Dataset | Claims | With `quote_source` | Grade E | Contested |
|---|---|---|---|---|
| idmc_disasters | 22,108 → **20,898** | 0 → **20,898** | 1 → 1 (37214, source inconsistency) | 0 → 0 |
| term_dyad | 915 → **922** | 0 → **922** | 1 → **9** (55075 + 8 contested-by-source) | 0 → **8** |
| term_conflict | 505 → **507** | 0 → **507** | 0 → **4** | 0 → **4** |
| cow (4 files) | 892 → 892 | 0 → **892** | 0 → 0 | 0 → 0 |
| **Total** | **24,420 → 23,219** | **0 → 23,219** | **2 → 14** | **0 → 12** |

**Per defect.**

| # | Before | After |
|---|---|---|
| N4 | 1,206 claims on preventive evacuations / risks (audit); by FIXB's rule 1,210, all `has_effect` grade B | 0 claims; 1,210 context statements `idmc:displacement_in_anticipation_of` |
| N5 | 1,103 claims on 01/01-named events, start at day precision (1,400 events with a 1 January start) | 0 events with a 1 January start at day precision; 1,400 at year precision with `coded_start` + `day_not_relied_on` |
| N6 | 7 dyad + 2 conflict episodes: one terminal row kept silently | 14 + 4 claims, all flagged; 12 contested-by-source (E); 9 old claims and 40 old statements retracted |
| N8 | 23,915 CAUSMORE claims without quote_source (+505 UCDP conflict-level); v5.1 not archived | 23,219 of 23,219 with quote_source; v5.1 zip archived (`b2d1f489…`) |
| N10 | 9 claims with precision 6 as an ordinary year | 9 with `source_date_certainty: assigned` (KG) and `effect_time_certainty` (scope); 0 with code 7 in this release |
| N12 | 2,695 claims from unlabelled REVA identities; 11 collapsed rows invisible | 2,695 events labelled en/fr (2,693 with claims); 10 `idmc:source_duplicate_rows` notes (11 copies) |

## Needs for the causal core (FIXA / CAUSCORE): not done here

1. **Anticipation relation (N4).** IDMC's "in anticipation of" displacement has no fitting relation in
   `relations.yaml`. A non-causal-family relation, for example `in_anticipation_of` (lane "anticipatory moves", never
   in chains), would let these 1,210 rows come back as T1 claims worded "displacement in anticipation of <hazard>
   (preventive evacuation, as reported by IDMC)". Until then they are KG context. The event view or API may also
   want to show `idmc:displacement_in_anticipation_of` next to causal claims, as it does other context edges.
2. **Precision 6 in grading (N10).** `scope.effect_time_certainty.certainty == "assigned"` (UCDP code 6) could keep
   `temporal.consistent` out of grade A, or mark a fact `temporal.date_assigned`. Code 7 already gives
   `effect_time` typed missing (M3 path).
3. **Contested by source (N6).** The claims use the existing `contests` field, and the current rules grade them E.
   If a separate fact or wording is wanted (the source disagreeing with itself, not a second source denying), the
   flag is `scope.contested_by_source: true`, with the rows in `scope.source_duplicate_terminal_rows`.
4. **Connector in retraction details (N1).** `causdata_common.deliver_claims` does not pass `parameters`
   (`connector`, `connector_version`) to `load_dataset_claims`. Adding it would change a module that every dataset
   connector imports, so it is left to the owner of the shared delivery path.
5. **Superseded records.** `ucdp:dyadepisode:<id>:termination` / `ucdp:episode:<id>:termination` of multi-terminal
   episodes are `superseded_record` entities with no current statements. KG entities cannot be retracted.

## Tests

* `tests/unit/test_fixb.py` (14, offline). Covers:
  * the N4 rule and context statements;
  * N5 year precision with the verbatim coded value;
  * N12 labels and duplicate notes;
  * N8 quote sources, each quote verbatim at its PDF page or README cell, plus the v5.1 check from the reduced zip
    fixture;
  * N6 per-row claims and contests at dyad and conflict level;
  * N10 codes 6/7 and the typed missing date;
  * the version bumps.
* `tests/integration/test_causmore_ingest.py` covers anticipatory statements, duplicate notes and the archived
  quote_source in IDMC, the contested pair and the retraction of a 1.0.0-style statement in the dyad file, and the
  archived v5.1 zip in COW. `test_causdata_ingest.py` is unchanged and passes.
* `test_causmore.py` and `test_causdata.py` were updated: dyad terminations may be per row, there are 18 conflict
  claims in the fixture, and `ucdp_date` replaces `_dated`.
* Fixtures:
  * the IDMC slice gains 12 real rows (149, 194–195, 198, 240, 650–652, 5355, 6299, 6842, 8720);
  * `atlas.cowwars` has a reduced copy of the v5.1 zip (the wars CSV member only, 4 rows; the real sha256 is in the
    manifest).
