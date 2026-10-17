# V3 causal claims: adversarial honesty audit (agent AUDIT, 2026-10-17)

Scope: the causal claims in the dev database `reva` (origin `live`), read-only. The specs were
`docs/roadmap/v3_causality.md`, `docs/dev/v3_causops_live.md` and `docs/dev/v3_causdata.md`. The rules were
`registry/causal/grading.yaml` (sha256 `6e21ac14…`, version 1) and `relations*.yaml` (`relations.yaml` sha256
`c7024820…`). The code read was `src/reva/causal/**` and the claim-producing connectors at commit `0925135`.
No code, registry or database row was changed. The only output is this report.

Severity scale:

* **High**: a claim shown to readers overstates its evidence or misnames who asserts it, contradicting a
  roadmap principle, at scale or on sensitive content.
* **Medium**: a structural or semantic gap that lets weak or REVA-made links pass as source-asserted causal
  claims, or that cannot be verified.
* **Low**: a narrow inaccuracy or a hardening gap.

## 1. Summary

**Mechanical fidelity is clean.** No sampled claim misstates its source row: relation, direction, entity ids,
dates, temporal check, grade arithmetic and verbatim text all held. The population checks agree:

* All 26,789 T1 claims were re-derived from the raw rows.
* All 1,002 Wikidata claims were re-derived from the archived entity JSON.
* All 444 raw objects hash correctly.
* All 21 quoted codebook terms that can be checked match the archived PDFs.
* The grades and temporal checks of all 27,791 claims recompute exactly.

**The honesty problems are in what the claims are taken to mean:**

* **Wikidata tier and corroboration.** 143 of the 235 "referenced" T3 claims have no real source: their only
  "reference" is a Wikimedia import or a Wikidata inference. Ten B grades come from corroboration that is not
  independent.
* **Attribution.** For 10 claims, Wikidata itself says who makes them (P3680, e.g. Vladimir Putin on the
  causes of the invasion of Ukraine), yet the wording says "According to Wikidata, … caused …". The
  "nature of statement" qualifier (P5102) on 25 claims is ignored.
* **NOAA.** 788 tsunami trigger claims, 36 % of NOAA's, concern tsunamis that NCEI codes as *very doubtful*,
  *questionable* or *only a seiche*. They are graded B like definite tsunamis.
* **Structure.**
  * About 41 % of the Wikidata claims involve class-level concepts such as "war", "conflict" or "state". The
    `/v1/causal/chain` service composes them into a chain "full-scale Russo-Ukrainian war → … → war → peace
    process → peace treaty → peace".
  * 32 % of the Wikidata claims point to an entity absent from the KG.
  * 9,015 claims get an "overlapping" temporal check from dates that REVA copied from the cause.
  * 16,440 claims (59 %) quote codebooks whose bytes were never archived.

**Defects ranked by severity** (details in §5; the status column was added after the fixes, see
`docs/dev/v3_audfix.md` and `docs/dev/v3_causfix.md`):

| # | Sev. | Defect | Claims affected (snapshot) | Status (2026-10-17) |
|---|---|---|---:|---|
| H1 | High | Wikidata T3 tier granted on non-source "references" (P143 imported from Wikimedia, P4656, P3452 inferred from, P887 heuristic) | 143 (114 C and 7 B that should be D; 22 already E) | fixed — CAUSFIX (`grading.kg_tier`, `NON_SOURCE_REF_PIDS`; `v3_causfix.md`) |
| H2 | High | Non-independent corroboration lifts Wikidata claims to B (mirror statements whose second "source" is a Wikipedia import, a Wikidata inference or the same quotation) | 10, plus 2 arguable | fixed — CAUSFIX (`grading.reference_keys`; `v3_causfix.md`) |
| H3 | High | Claimant misattributed: P3680 "statement supported by" (Putin, Stalin…) rendered "According to Wikidata"; P5102 "nature of statement" ignored by tier, grade and wording | 10 (P3680), 25 (P5102) | fixed — CAUSFIX (grade cap D, attribution wording; `v3_causfix.md`) |
| H4 | High | NOAA trigger claims on tsunamis that NCEI codes as validity 0, 1 or 2 (seiche, very doubtful, questionable), and on cause code 2 "Questionable Earthquake", graded B like definite events | 788 + 23 | fixed — data: AUDFIX (`atlas.noaa_hazards` 1.1.0, `scope.source_validity`; `v3_audfix.md`); grade: CAUSFIX (`grade.D.source_doubtful`) |
| M1 | Medium | Generic, class-level Wikidata statements (e.g. "human → artificial object", "state ↔ process ↔ occurrence") loaded into the event causal graph; 3 of the 5 cycles; chain service composes through them | ≈ 414 (heuristic) | partly — CAUSFIX: chains skip class items; claims kept (`v3_causfix.md`) |
| M2 | Medium | Wikidata claims whose cause or effect is not in `kg.entity` (no label, kind or dates; always `undetermined`); not counted by the KG loader | 324 | fixed (reporting) — AUDFIX: both loaders count/sample claims with an endpoint missing from the KG (`model._count_unknown_endpoints`); frontier harvest not done |
| M3 | Medium | Circular temporal check: the effect is dated by copying the cause's dates, then reported as `overlapping` | 9,015 | fixed — AUDFIX: typed-missing effect dates (MMP, MEC, NAVCO 1.2), Archigos claims removed (M5); `undetermined` with reason |
| M4 | Medium | Codebook bytes not archived for NAVCO 1.2/1.3/2.1, MEC, MMP, TIES, Archigos and ESRB, so the "verbatim" definitions cannot be re-verified | 16,440 | fixed — AUDFIX: 8 codebooks fetched, archived, sha256-pinned; quotes verbatim with page/paragraph (`codebook.quote_source`) |
| M5 | Medium | Archigos: one coded attribute (manner of exit) reified into two events and a causal edge "coup → removal"; removals by "other government actors" labelled coups; definition quoted for the wrong sub-case | 327 (44 + 10 sub-cases) | fixed — AUDFIX (`atlas.archigos` 1.1.0): one removal event, no claim; 327 retracted |
| M6 | Medium | UCDP peace agreements: the codebook association "conflicts related to the dyads signing" turned into `reaction_to` | 382 | fixed — AUDFIX (`atlas.ucdp_term` 1.1.0): context `ucdppa:conflict`; 382 retracted |
| M7 | Medium | ESRB: claims regex-extracted from a free-text field ("Format: text"); narrative cells and loose mappings; mechanism silently truncated at 4,000 characters | 96 (12 specific) | fixed — AUDFIX (`atlas.ecrises.esrb` 1.1.0): exact objective lists only, truncation marked; 27 retracted |
| L1 | Low | NAVCO 2.1 outcomes with `success = 0` cite the definition of `success`; the coded category comes from `progress`, whose definition is not attached | 180 | fixed — AUDFIX (`atlas.navco` 1.1.x): success = 0 cites `progress` |
| L2 | Low | "Coded outcome" asserted for campaigns that the source codes as still ONGOING | 189 | fixed — AUDFIX: no outcome claim for ONGOING; 189 retracted |
| L3 | Low | Inconsistent handling of non-responses: ICB "No response-inaction" is worded as a provoked reaction, while MMP "ignore" is dropped | 5 / 296 | fixed — AUDFIX: inaction never `reaction_to` (ICB 5 retracted; MMP list kept verbatim) |
| L4 | Low | The KG import drops reference and qualifier properties outside its allow-list: 11 claims are T4 although Wikidata has a reference, and 20 claims lose qualifiers, including scope (P1001, P1011, P1012) | 31 | fixed — AUDFIX: claim refs/qualifiers verbatim from the raw entity JSON (`load_kg_claims(store=…)`) |
| L5 | Low | Quoted definitions truncated or not from archived bytes: MID `outcome` ends mid-sentence; the NOAA "help" prefix | 2,419 / 2,186 (text only) | fixed — AUDFIX: MID quote completed; NOAA help prefix dropped |
| L6 | Low | `coded_outcome` (an event → its own REVA-made outcome entity) makes up 36 % of the "causal claims" and counts in chains | 9,912 | decided — CAUSFIX: `coded_outcome` not traversed by chains (`v3_causfix.md`) |
| L7 | Low | A self-loop and five cycles are loaded as ordinary claims. The consistency report shows them, but nothing quarantines them | 1 + 13 edges | fixed — CAUSFIX: self-loop / cycle flags, chain quarantine |
| L8 | Low | Retraction is never exercised on live data; the KG retraction reason is mislabelled when a value becomes somevalue or novalue; complete → partial downgrade is silent | 0 retractions | fixed — AUDFIX: retraction exercised live (930 claim retractions through the loader, 1,635 KG statement retractions) and in throwaway-DB tests; `value_not_an_item` detail; `complete_requested/effective` |

## 2. Method

### 2.1 Snapshot

The audited population is the 27,791 current claims with `kg.causal_claim.id` from 1 to 27,791 (causal loads
1–20). Their grades are the latest `kg.causal_grade` rows with `load_id ≤ 21`, the grader run of 00:45 UTC.

The database moved during the audit: at 01:43 UTC another agent's `reva causal refresh` (causal loads 22 and
23) added 670 Wikidata claims (ids 27,792–28,461). It also re-graded 6 old claims after new KG dates arrived:
27476, 27648, 27656 and 27756 went from `undetermined` to `consistent`, 27751 from D to E (`inconsistent`), and
27763 was unchanged. The re-grades are correct. Those 670 claims are outside this audit.

### 2.2 Sample

The sample was drawn as follows:

* **Strata.** For dataset sources, a stratum is (source_key, dataset, relation, grade). For Wikidata, it is
  (source_key, tier, relation, grade). There are 44 strata.
* **Draw.** Each stratum's claim ids are sorted. Strata are visited in sorted order, and one
  `random.Random(20261017)` makes the draws with `rng.sample(ids, n)`.
* **Sizes.**
  * Dataset strata: `n = min(N, max(3, ceil(0.0075·N)))`.
  * Wikidata strata: `n = min(N, max(4, ceil(0.08·N)))`. Wikidata is heterogeneous, so it is oversampled.
* **Censuses.** Every claim with `temporal_check = inconsistent` (67) and every grade-E claim (72) was added.

The result is 334 random claims plus the censuses: **380 unique claims**.

Allocation:

| stratum | N | n |
|---|---:|---:|
| archigos.leaders / coup_removals / coded_outcome / B | 327 | 3 |
| cow.data / mid5 / coded_outcome / B | 2419 | 19 |
| esrb.fcdb / fcdb / contributing_factor / B | 96 | 3 |
| eusanct.kondata / eusanct_case / coded_outcome / B | 325 | 3 |
| eusanct.kondata / eusanct_case / coded_outcome / E | 1 | 1 |
| eusanct.kondata / eusanct_case / contributing_factor / B | 133 | 3 |
| harvard.dataverse.massmob / mmp / reaction_to / B | 7221 | 55 |
| harvard.dataverse.mec / mec / coded_outcome / B | 2389 | 18 |
| harvard.dataverse.mec / mec / reaction_to / B | 1115 | 9 |
| harvard.dataverse.navco / navco12 / coded_outcome / B | 389 | 3 |
| harvard.dataverse.navco / navco12 / reaction_to / B | 352 | 3 |
| harvard.dataverse.navco / navco13 / coded_outcome / B | 620 | 5 |
| harvard.dataverse.navco / navco21 / coded_outcome / B | 278 | 3 |
| harvard.dataverse.navco / navco21 / reaction_to / B | 2627 | 20 |
| harvard.dataverse.navco / navco21 / reaction_to / E | 2 | 2 |
| icb.data / icb1 / coded_outcome / B | 511 | 4 |
| icb.data / icb1 / coded_trigger / B | 511 | 4 |
| icb.data / icb2 / coded_outcome / B | 1124 | 9 |
| icb.data / icb2 / coded_trigger / B | 1126 | 9 |
| icb.data / icb2 / reaction_to / B | 1125 | 9 |
| icb.data / icb2 / reaction_to / E | 1 | 1 |
| noaa.ncei_hazards / tsunami_sources / coded_trigger / B | 2171 | 17 |
| noaa.ncei_hazards / tsunami_sources / coded_trigger / E | 15 | 3 |
| ties.unc / ties_case / coded_outcome / B | 1024 | 8 |
| ucdp.downloads / peace_agreements / reaction_to / B | 382 | 3 |
| ucdp.downloads / term_conflict / coded_outcome / B | 505 | 4 |
| wikidata.wbapi / T2 / has_effect / B | 1 | 1 |
| wikidata.wbapi / T3 / contributing_factor / C | 22 | 4 |
| wikidata.wbapi / T3 / has_cause / B | 8 | 4 |
| wikidata.wbapi / T3 / has_cause / C | 71 | 6 |
| wikidata.wbapi / T3 / has_cause / E | 4 | 4 |
| wikidata.wbapi / T3 / has_effect / B | 9 | 4 |
| wikidata.wbapi / T3 / has_effect / C | 66 | 6 |
| wikidata.wbapi / T3 / has_effect / E | 28 | 4 |
| wikidata.wbapi / T3 / immediate_cause / B | 3 | 3 |
| wikidata.wbapi / T3 / immediate_cause / C | 23 | 4 |
| wikidata.wbapi / T3 / immediate_cause / E | 1 | 1 |
| wikidata.wbapi / T4 / contributing_factor / D | 51 | 5 |
| wikidata.wbapi / T4 / has_cause / D | 245 | 20 |
| wikidata.wbapi / T4 / has_cause / E | 11 | 4 |
| wikidata.wbapi / T4 / has_effect / D | 290 | 24 |
| wikidata.wbapi / T4 / has_effect / E | 7 | 4 |
| wikidata.wbapi / T4 / immediate_cause / D | 160 | 13 |
| wikidata.wbapi / T4 / immediate_cause / E | 2 | 2 |

Sampled claim ids (`kg.causal_claim.id`):

<details><summary>380 ids</summary>

19, 51, 294, 464, 601, 666, 784, 963, 1178, 1190, 1271, 1392, 1687, 1889, 2229, 2451, 2638, 2822, 3202, 3219,
3247, 3268, 3366, 3488, 3604, 3646, 3713, 3803, 3847, 3938, 3944, 3969, 4226, 4227, 4357, 4398, 4752, 4896,
5121, 5129, 5147, 5157, 5749, 6024, 6355, 6383, 6500, 6535, 6543, 6571, 6651, 6665, 6672, 6706, 6803, 6879,
7038, 7080, 7220, 7380, 7479, 7584, 7725, 7737, 7745, 7808, 7831, 7912, 8291, 8322, 8473, 8552, 8643, 8667,
8758, 9133, 9232, 9260, 9271, 9375, 9711, 9836, 10188, 10218, 10517, 10571, 10740, 10816, 10830, 10953, 11020,
11143, 11316, 11395, 11409, 11432, 11483, 11547, 11631, 11832, 11854, 12402, 12410, 12433, 12513, 12560, 12629,
12745, 12785, 12849, 13010, 13026, 13073, 13207, 13221, 13348, 13457, 13504, 14256, 14319, 14370, 14427, 14462,
14848, 15071, 15123, 15210, 15397, 15618, 15889, 16150, 16243, 16263, 16707, 16828, 16871, 16917, 16996, 17544,
17937, 17945, 17982, 18049, 18069, 18086, 18097, 18202, 18215, 18221, 18378, 18459, 18463, 18526, 18571, 18666,
18679, 18699, 18857, 18986, 18999, 19071, 19088, 19365, 19598, 19600, 19628, 19740, 20120, 20225, 20260, 20704,
21120, 21218, 21481, 21847, 21859, 21861, 21917, 21977, 22054, 22092, 22121, 22464, 23039, 23072, 23187, 23553,
23566, 23592, 23697, 23784, 23833, 23917, 23975, 24100, 24146, 24183, 24232, 24292, 24420, 24501, 24641, 24690,
24708, 24785, 24837, 24963, 25129, 25248, 25525, 25646, 25785, 25791, 25862, 26012, 26055, 26059, 26061, 26072,
26074, 26182, 26319, 26443, 26463, 26544, 26571, 26643, 26676, 26695, 26722, 26748, 26750, 26771, 26790, 26818,
26819, 26820, 26821, 26834, 26842, 26844, 26847, 26852, 26860, 26907, 26930, 26931, 26952, 26977, 26979, 26982,
27001, 27017, 27018, 27019, 27029, 27031, 27032, 27033, 27034, 27036, 27037, 27039, 27040, 27049, 27050, 27057,
27062, 27087, 27088, 27092, 27097, 27098, 27101, 27104, 27105, 27106, 27107, 27108, 27110, 27112, 27121, 27134,
27137, 27138, 27145, 27148, 27151, 27153, 27156, 27157, 27161, 27170, 27181, 27188, 27190, 27193, 27194, 27200,
27212, 27214, 27217, 27221, 27222, 27232, 27250, 27253, 27254, 27256, 27258, 27271, 27272, 27278, 27279, 27284,
27302, 27305, 27313, 27316, 27324, 27329, 27339, 27346, 27348, 27350, 27353, 27356, 27372, 27376, 27377, 27378,
27380, 27382, 27386, 27394, 27396, 27399, 27403, 27411, 27416, 27418, 27420, 27424, 27427, 27440, 27449, 27461,
27469, 27473, 27479, 27495, 27497, 27504, 27505, 27516, 27521, 27524, 27526, 27533, 27554, 27566, 27591, 27606,
27616, 27637, 27663, 27674, 27683, 27707, 27708, 27717, 27728, 27729, 27745, 27749, 27754, 27755, 27758, 27771,
27772

</details>

### 2.3 Checks per claim

The checks were independent re-implementations. They did not call `reva.causal`. The one exception: the TIES
`.xls` file was read with the repository's BIFF reader `read_xls_first_sheet`. Database access used a
`default_transaction_read_only` session.

1. **Provenance.**
   * The raw bytes were found in `var/objects` or `var/kg/objects` (`reva/raw/sha256/aa/bb/<sha>`), and their
     sha256 was recomputed.
   * The exact source row was re-read with an independent parser:
     * CSV and TSV through `csv`;
     * the MID zip member `MIDA 5.0.csv` and the EUSANCT tar member `EUSANCT_Dataset_Case-level.csv`;
     * xlsx through a minimal `zipfile` and XML reader;
     * Stata through `pandas.read_stata`;
     * NOAA JSON pages, plus the reciprocal earthquake or eruption page;
     * the gzip Wikidata entity JSON, located by statement id.
   * The row key in the raw row (crisno, cracno, dispnum, c_epid, paid, caseid, NAVCOID, obsid, NCEI id,
     statement id) had to equal the selector.
2. **Relation per codebook.** The coded value had to be a valid code that the mapping in
   `relations_datasets.yaml` or the connector turns into this relation and codebook term:
   * ICB BREAK and TRIGGR 1–9, FOROUT 1–7, MAJRES 1–9, OUTCOM 1–5;
   * the MID outcome code → `outcome.*` term;
   * UCDP `c_epterm = 1` and `c_outcome` = the term;
   * the PA `conflict_id` cell contains the conflict;
   * MMP `protest = 1` with a non-empty response list equal to the coded one;
   * MEC and NAVCO flag = 1, and NAVCO 2.1 `repression ∈ {1, 2, 3}` for that year;
   * Archigos exit Irregular with a military or other-government exitcode;
   * EUSANCT and TIES `finaloutcome` coded, and EUSANCT contribution ≥ 3, result ≥ 3, HSE ≥ 9;
   * NOAA cause code ∈ hazard codes, the id link, `tsunamiEventId` reciprocal and validity ≠ −1;
   * Wikidata property → relation, the side of the cause (P828 and P1478 object; P1542, P1536 and P1537
     subject…), snaktype `value`, and rank not deprecated.
3. **Ids.** The cause and effect ids rebuilt from the raw row had to equal the stored ones.
4. **Dates.** The KG `P580`, `P585` or `P571` of the cause and the effect had to equal the raw coded date at the
   documented precision. Next, the temporal check was recomputed from the KG precision windows, with the
   start taken from P580, then P585, then P571, preferred rank first. The window bounds themselves were
   validated for every date statement on a claim endpoint: day, month and year bounds, Julian → Gregorian
   shift.
5. **Tier and grade.** The `grading.yaml` decision table was re-implemented: E inconsistent → E contested and
   uncorroborated → A → B expert → B T3 corroborated → C → D. Corroboration was recomputed by union-find over
   upstream keys with the community-drop rule, and the stored `rule_ids` had to contain the deciding rule and
   the tier and temporal facts. For Wikidata, the tier was also recomputed from the **raw** references.
6. **Mechanism.** The stored text had to be byte-equal to the raw cell (ESRB, Archigos). All other datasets
   store `mechanism = NULL`.
7. **No co-occurrence or context.** The codebook term must be a `reva_kind: claim` mapping. No claim pair may
   coincide with a context predicate (P361, P155, P710, `causdata:coded_issue`,
   `causdata:agreement_in_termination_window`).
8. **same_as and candidates.** No claim pair may coincide with `contention:same_campaign_as`,
   `eusanct:same_case_as`, `ecrises:same_crisis_window`, `ncei:same_as` or P460 statements. Candidates must
   never reach `kg.causal_claim` (no T5 or T6 tier; `kg.causal_candidate` empty).

### 2.4 Population checks

Checks 1–6 were run on **all 26,789 T1 claims** and **all 1,002 Wikidata claims**. Temporal and grade
recomputation (checks 4 and 5) was run on **all 27,791 claims**. Structure was checked over the whole
snapshot: self-loops, duplicates, SCC cycles, missing entities and retractions.

### 2.5 Codebooks

* **Archived and checked.** Codebook PDFs were extracted from the archive with `pdftotext -layout`: ICB1, ICB2,
  UCDP termination and UCDP PA (raw objects); the MID Dispute and Incident Coding Manuals (members of the MID
  zip); the EUSANCT case-level codebook (member of the RADAR tar). All 21 distinct quoted definitions were
  found verbatim, after the documented whitespace and hyphen normalisation.
* **Not archived.** The other codebooks were not archived (§5 M4). No network fetch was made, because a
  metered fetch writes budget-ledger rows and the database was to stay read-only.

## 3. Results per source

* **Fidelity error**: the claim misstates its raw row (relation, direction, ids, dates, temporal check, grade
  arithmetic, verbatim text).
* **Honesty issue**: the claim is faithful to the row but is affected by a claim-level defect of §5 (H1–H4,
  M1, M2, M5, M7, L1–L4). Dataset-wide design defects (M3, M4, M6, L6) are not counted per claim; they are
  listed in the last column.

Rates are over the random draws; census claims are reported separately. With 0 fidelity errors in 233 T1 draws
(and 0 in the 26,789-claim population check), the T1 fidelity error rate is below 1.3 % at 95 % in the sample
and 0 in the population.

| source / dataset | claims | sampled (random + census) | fidelity errors | honesty issues in random draws | population count of claim-level defects | dataset-wide caveats |
|---|---:|---:|---:|---:|---|---|
| icb.data / icb1 | 1,022 | 8 + 0 | 0 | 0 / 8 | — | — |
| icb.data / icb2 | 3,376 | 28 + 1 | 0 | 0 / 27 | L3: 5 (MAJRES = 1) | — |
| cow.data / mid5 | 2,419 | 19 + 0 | 0 | 0 / 19 | — | L5 (truncated definition) |
| ucdp.downloads / term_conflict | 505 | 4 + 0 | 0 | 0 / 4 | — | — |
| ucdp.downloads / peace_agreements | 382 | 3 + 0 | 0 | 0 / 3 | — | M6 (all 382) |
| harvard.dataverse.massmob / mmp | 7,221 | 55 + 0 | 0 | 2 / 55 (L3) | L3: 296 ("ignore" dropped) | M3 (all), M4 (all) |
| harvard.dataverse.mec / mec | 3,504 | 27 + 0 | 0 | 1 / 27 (L2) | L2: 90 | M3 (1,115), M4 (all) |
| harvard.dataverse.navco / navco12 | 741 | 6 + 0 | 0 | 0 / 6 | L2: 37 | M3 (352), M4 (all) |
| harvard.dataverse.navco / navco13 | 620 | 5 + 0 | 0 | 0 / 5 | L2: 62 | M4 (all) |
| harvard.dataverse.navco / navco21 | 2,907 | 23 + 2 | 0 | 3 / 23 (L1) | L1: 180 | M4 (all) |
| archigos.leaders / coup_removals | 327 | 3 + 0 | 0 | 0 / 3 | M5 sub-cases: 44 + 10 | M5 reification (all), M3 (all), M4 (all) |
| esrb.fcdb / fcdb | 96 | 3 + 0 | 0 | 0 / 3 | M7: 12 | M7 regex extraction (all), M4 (all) |
| eusanct.kondata / eusanct_case | 459 | 6 + 1 | 0 | 0 / 6 | — | — |
| ties.unc / ties_case | 1,024 | 8 + 0 | 0 | 0 / 8 | — | M4 (all) |
| noaa.ncei_hazards / tsunami_sources | 2,186 | 20 + 15 (12 census-only) | 0 | 6 / 20 (H4) | H4: 788 + 23 | L5 (help text not archived) |
| wikidata.wbapi (T2–T4) | 1,002 | 113 + 34 | 0 | 63 / 113 | H1: 143, H2: 10, H3: 35, M1: ≈ 414, M2: 324, L4: 31 | — |
| **total** | **27,791** | **334 + 46 (380 unique)** | **0** | **75 / 334** | | |

Census results:

* **All 67 temporally inconsistent claims.** The recomputed check agrees on all 67, and so does the raw source
  data:
  * NOAA: 15 claims. In each, the NCEI earthquake or eruption record is dated after its tsunami record, e.g.
    claim 25646: tsunami 1962 dated 1964-12-10, earthquake 6410 dated 1964-12-11; claim 24837: tsunami 504
    dated 1771-11-09, eruption 617 dated 1772-05-09.
  * NAVCO 2.1: 2 claims (9271, 9375). The campaign-year row precedes the campaign's start date.
  * ICB: 1 claim (2638). As documented.
  * EUSANCT: 1 claim (24183). The case starts 2005-03-15; the outcome is dated 2000-09-07.
  * Wikidata: 48 claims, mostly infobox imports such as "World War I has effect German Empire". These are
    checked against the state's inception (P571).

  None was silently corrected; all are graded E.
* **All 72 grade-E claims.** 67 are E for the temporal reason. The other 5 are E for
  `grade.E.contested_uncorroborated`, through P1310 "statement disputed by" qualifiers (27145, 27148, 27153,
  27156, and 27279 "Revolution of Dignity → War in Donbas"). The rule ids recorded are correct.

## 4. What is verified as correct

* **Raw provenance.** All 444 distinct raw objects cited by the claims exist in the archive, and their sha256
  matches:
  * 14 T1 dataset files (NAVCO 3, ICB 2, UCDP 2, the other 7 sources 1 each);
  * 16 NOAA tsunami pages; the reciprocal earthquake and eruption pages named in the selectors were also
    hash-checked when read;
  * 414 Wikidata entity documents.
* **Row and id fidelity.** It held in 100 % of the population for all 15 T1 datasets. Two apparent mismatches
  turned out to be legitimate:
  * NAVCO split campaigns are disambiguated as `…-1` / `…-2` (claims 8414–8416, 9060, 9061).
  * UCDP termination dates with precision codes 4–7 are stored at year precision, as documented.
* **Wikidata fidelity.** All 1,002 claims were re-found in the archived entity JSON with the same property,
  value, direction, statement id and rank. All are `normal` rank; there are no deprecated statements.
* **Codebook quotes.** ICB, MID, UCDP termination, UCDP PA and EUSANCT: all 21 quoted definitions are verbatim
  in the archived PDFs.
* **Temporal check.** The windows are correct: day, month and year bounds; Julian dates shifted to Gregorian
  (e.g. 1917 Julian → 1917-01-14..1918-01-13); NCEI B.C. years converted to astronomical years. Recomputed for
  all 27,791 claims, the check matches the stored value everywhere except the 5 claims re-graded at load 23
  after new dates arrived (§2.1).
* **Grades.** The deciding rule and the recorded `rule_ids` follow `grading.yaml` for 27,791 of 27,791 claims.
  The `rules_sha256` on every grade row equals the registry file.
* **Context and co-occurrence.** No claim comes from a context mapping. MID revision types, UCDP
  incompatibility, side A, incident order, TRIGENT, OUTESR and PCID all stay statements. The termination ↔
  agreement window is never a claim: 0 claim pairs coincide with `causdata:agreement_in_termination_window`.
* **same_as and candidates.**
  * 0 claim pairs coincide with any `same_as`-type statement (`contention:same_campaign_as` 375,
    `eusanct:same_case_as` 84, `ecrises:same_crisis_window` 77).
  * Corroboration groups use exact ids, so `same_as` never merges or corroborates.
  * `kg.causal_candidate` is empty in `reva`, and no claim has tier T5 or T6 (the CHECK constraint forbids it).
  * Graph services read candidates only in `graph.candidates`.
* **Duplicates.** 19 (cause, effect, relation, source) groups have 2 claims each. All are the two sides of a
  Wikidata inverse pair (P1478/P1536, P1479/P1537), or a Wikidata double statement, and are correctly grouped
  for corroboration. There are no dataset duplicates.
* **Load completeness.** All 15 dataset loads were `complete = true` with 0 rejected rows.

## 5. Defects

### H1 — High — Wikidata T3 tier granted on references that are not sources

**Evidence.**

* `reva.causal.model.claim_from_statement` sets `tier = "T2" if citation else ("T3" if refs else "T4")`. Any
  non-empty reference counts. 143 of the 235 T3 claims have references made only of these properties:
  * P143 "imported from Wikimedia project";
  * P4656 "Wikimedia import URL";
  * P813 "retrieved";
  * P3452 "inferred from", another Wikidata item;
  * P887 "based on heuristic".
* By reference property set: `{P143, P4656}` 74, `{P143, P4656, P813}` 32, `{P3452}` 18, `{P143}` 9,
  `{P887}` 4, `{P4656}` 3, `{P143, P813}` 2, `{P4656, P813}` 1.
* Grade outcome: 114 C and 7 B that would be D under "without references", and 22 that are E anyway.
* Examples, each verified in the archived raw JSON:
  * 26817, 26822, 26823 "World War I has effect Allies of the First World War / Russian Revolution / Russian
    Civil War": reference `P143 Q58251` (Azerbaijani Wikipedia) plus `P4656 https://az.wikipedia.org/?oldid=7446971`;
    graded C.
  * 26791 "1918 Ukrainian coup d'état immediate cause of Ukrainian State": `P143 Q199698`; graded C.
  * The same azwiki import also produced 26818–26821 "World War I has effect German Empire / Russian Empire /
    Ottoman Empire / Austria–Hungary", all T3. They are E only because of the dates.
* In the sample, 17 of the 147 Wikidata claims carry an overstated tier and grade; 39 are flagged once E
  claims are included.

**Why it matters.** `grading.yaml` reads "Tier T3: Wikidata causal statement with references", and the reader
is told the statement is referenced. Wikidata's own guidance treats P143 and P4656 as provenance of a bot
import, not as a source. P3452 and P887 mark derived statements.

**Fix** (CAUSCORE, `grading.py`, `model.py`):

* Define `NON_SOURCE_REF_PIDS = {P143, P4656, P813, P3452, P887}` in the registry.
* A reference whose properties all fall in that set does not count toward T3. The claim is then T4, with a
  fact rule such as `tier.T4.import_only` explaining why.
* Bump `grading.yaml` to version 2 so that every claim is re-graded.

### H2 — High — non-independent corroboration produces B grades

**Evidence.** The B-graded Wikidata claims are all corroborated by the *mirror* statement on the other entity:
P828 on the effect with P1542 on the cause, or P1478 with P1536. They count as two groups because their
`upstream_keys` differ. In 5 of the 10 mirror pairs the second "source" is not independent:

| claims | link | upstream groups | problem |
|---|---|---|---|
| 27138, 27363 | full-scale Russo-Ukrainian war → Q110653216 | `imported_from:Q206855` (ruwiki) and `refhash:` of `{P3452: Q112042826}` | a Wikipedia import and a Wikidata inference |
| 27157, 27377 | Q110999040 → Q119573587 | `imported_from:Q328` (enwiki) and `refhash:` of `{P887: Q69652283}` | a Wikipedia import and a heuristic |
| 27346, 27350 | Q130796 → Q2305266 | `url:europarl…` and `refhash:` of `{P3452: Q4413308}` | an inference from another Wikidata item |
| 27372, 27144 | full-scale war → accession of Finland and Sweden to NATO | two URLs and `refhash:` of `{P3452: Q112127201}` | an inference from another Wikidata item |
| 27214, 27568 | 1991 Soviet coup attempt → Declaration of Independence of Ukraine | `imported_from:Q199698` and `refhash:` of a reference holding **the same P1683 quotation** | the same evidence (the text of the Act) counted twice |

The pair 27064 / 27076 (Q167634 → dissolution of the Soviet Union) is arguable: one side is a ruwiki import,
the other a JSTOR article (`P888 2151700`).

**Why it matters.** Principle "sources sharing an upstream count once". `reference_keys` falls back to a
`refhash:` of the whole reference whenever P854, P248, P143, DOI or ISBN are absent. A Wikimedia import is
keyed as its own "item", so it never merges with anything.

**Fix** (`grading.reference_keys` / `upstream_keys`):

* Map P143 and P4656 to `community:wikidata`, which is not independent, as for unreferenced statements.
* Map P3452 to `inferred:<Q>`, and merge it with the upstream of the statement it was inferred from, or treat
  it as community.
* Map P887 to `community:wikidata`.
* Add `quote:<sha of P1683 text>` as an upstream key.

### H3 — High — claimant misattributed; nature-of-statement ignored

**Evidence: P3680.** 10 claims carry the qualifier P3680 "statement supported by". The wording built by
`graph._wording` is "According to Wikidata (statement …), <cause> caused <effect>; REVA does not establish
causation", and it ignores the qualifiers:

* 27009, 27057, 27145: "eastward expansion of NATO has cause / caused Russo-Ukrainian war", supported by Q7747
  (Vladimir Putin) and Q1140115. 27145 is also disputed by NATO.
* 27153: "neo-Nazism in Ukraine → full-scale Russo-Ukrainian war", supported by Putin, disputed by Q185441 and
  Ukraine.
* 27278: "Euromaidan immediate cause of the Russian annexation of Crimea", Putin.
* 27065: "Leninism has cause dissolution of the Soviet Union", Putin.
* 27148: "anti-Ukrainian sentiment → full-scale war", supported by Ukraine.
* 27151: Q4217811.
* 27126: Q1970048.
* 27353: Q855 (Stalin) and Q130786.

Wikidata here records *who claims* the link; it does not claim it. Only the 3 claims that also carry P1310
are graded E. 27009, 27057 and 27065 are D, and 27278 is C, with no hint of the real claimant except in the
raw `qualifiers` JSON.

**Evidence: P5102.** 25 claims carry the qualifier P5102 "nature of statement": 26878, 26950, 27045, 27063,
27125, 27156, 27158, 27214, 27215, 27241, 27276, 27428, 27440, 27502, 27503, 27504, 27568, 27569, 27599,
27600, 27601, 27619, 27760, 27767, 27768. The values are Q28962312, Q28962310, Q132555, Q712144, Q20820099
and Q110143752; the first two are likely the frequency values "often" and "rarely". They are not resolved,
and they affect neither tier, grade nor wording. Example: 27045 "Secret Additional Protocol to the
Molotov–Ribbentrop Pact immediate cause of World War II", P5102 = Q132555.

**Why it matters.** Principle 1 and the spec text "shows the reader exactly who asserts what". These are among
the most sensitive claims in the database.

**Fix** (`model.claim_from_statement`, `graph._wording`, registry):

* Store P3680 as `asserted_by` and word the claim "Wikidata records that <X> asserts that …".
* Keep a registry of P5102 values (e.g. allegation, hypothesis, rarely, disputed) with an effect for each:
  label, demote, or exclude from chains.
* Until then, exclude claims with P3680 or P5102 from chains and from "why" lanes, or grade them E with a new
  rule `grade.E.attributed_or_qualified`.

### H4 — High — NOAA claims on doubtful tsunamis graded as definite

**Evidence.**

* The archived NCEI descriptor `tsunami/validity` (raw `081f620e…`) reads: −1 "Erroneous entry", 0 "Event
  that only caused a seiche", 1 "Very Doubtful Tsunami", 2 "Questionable Tsunami", 3 "Probable Tsunami",
  4 "Definite Tsunami".
* `coded_links` in `atlas_noaa_hazards.py` excludes only −1. The claims break down by validity as 0: 32,
  1: 327, 2: 429, 3: 418, 4: 980. So **788 of the 2,186 claims (36 %)** assert "earthquake/eruption X is the
  coded trigger of tsunami Y" for an event that NCEI itself doubts.
* 23 claims rest on cause code 2 "Questionable Earthquake".
* All are graded B (`grade.B.expert`). Examples: 24604 (validity 1), 24606 (validity 1, cause code 2),
  24607 (validity 2), 24695 (validity 0).
* The connector's own atlas table filters validity 3–4 for counts, but the claims do not.
* Sample: 6 of 20 random NOAA draws.

**Fix** (`atlas_noaa_hazards.coded_links` and `grading.yaml`):

* Emit claims only for validity 3–4, or carry `eventValidity` and the cause-code label in `scope`.
* Add a grading condition such as `source_certainty: doubtful → grade C/D`, with a fact rule
  `certainty.doubtful`.
* Word cause code 2 as "questionable".

### M1 — Medium — class-level Wikidata statements in the event causal graph

**Evidence.**

* P828 and P1542 are also used on Wikidata between *classes*. Claims loaded include 26830/26839 "human →
  artificial object", 26840 "agent → artificial object", 26832 "conflict escalation → war", 26833
  "war has effect effects of war", 26878 "conflict has effect violence", 26888 "peace treaty has cause peace
  treaty" (the self-loop) and 26895…26919 "state ↔ process ↔ occurrence".
* Heuristic count: 414 of 1,002 Wikidata claims have at least one endpoint that has a P279 (subclass of)
  statement and no date. 90 have it on both sides.
* Three of the five cycles found are entirely among such concepts: {state, process, occurrence};
  {conflict, parental alienation, violence}; {inflation, Q13649246}.
* `graph.chain(conn, "Q110999040", "Q454", max_hops=6)`, run read-only, returns a chain of grade D:

  > full-scale Russo-Ukrainian war → accession of Finland and Sweden to NATO → conflict escalation → war
  > (Q198) → peace process → peace treaty → peace

  The chain note correctly says it is a REVA composition. Still, five of its six hops are generic
  ontology statements, not historical claims.

**Fix.**

* Keep only claims whose two endpoints are instances (P31) of event or condition classes from
  `registry/events/taxonomy.yaml`, or have a date.
* Route class-to-class statements to a separate `generic` lane that the chain, "why" and NLQA services never
  traverse.

### M2 — Medium — claims whose cause or effect is not in the KG

**Evidence.**

* 324 of 1,002 Wikidata claims (216 by cause, 114 by effect) reference an entity with no `kg.entity` row and
  no statements. Examples: Q8698 (Great Depression), Q3684508 (claim 27440).
* All 324 are `undetermined`, and the API shows a bare QID.
* `load_dataset_claims` reports `unknown_entities`, but `load_kg_claims` does not count them at all.
* Sample: 33 of 147 Wikidata claims.

**Fix.**

* Count and report unknown endpoints in `load_kg_claims`.
* Harvest the frontier entities (labels, P31, dates) before loading.
* Show the claim as "endpoint not in the knowledge graph", and keep it out of chains until the entity exists.

### M3 — Medium — temporal check made circular by copied dates

**Evidence.**

* The effect entities below have **no date of their own**. The connectors date them by copying the cause's
  dates, with a `dating` qualifier:
  * MMP `mmp:<id>:state_response`: "the protest's dates (MMP codes no separate date for the response)",
    7,221 claims;
  * MEC `mec:<id>:regime_violence`: "episode period", 1,115 claims;
  * NAVCO 1.2 `…:regime_violence`: "campaign period", 352 claims;
  * Archigos `archigos:coup:<obsid>`, dated with the exit day, 327 claims.
* The grader then reports `overlapping` ("The start dates overlap at their precision: the order cannot be
  decided") for all **9,015** claims, as if two independent dates had been compared.

**Fix.**

* The grader should treat a date carrying `dating` / `basis` = copied from another entity as absent, and
  return `undetermined`. Alternatively, add a fifth value `not_independently_dated`, with its own fact rule.
* The connectors could also emit no `P580` on such derived events.

### M4 — Medium — codebook text for 59 % of the claims cannot be re-verified

**Evidence.** The codebook files are absent from `var/objects`, `var/kg/objects`, `meta.artifact` and the test
fixtures, even where the claims cite a pinned sha256:

| source | codebook | pinned sha256 cited by the claims |
|---|---|---|
| NAVCO 1.2 | Dataverse file 3621197 | `6743dc61…` |
| NAVCO 1.3 | file 3769105 | `8a265f00…` |
| NAVCO 2.1 | file 3648624 | `6fe8faa4…` |
| MEC | file 11543083 (docx) | `3d5b0133…` |
| MMP | file 2775560 | `03541007…` |
| TIES | manual | `862fe788…` |
| Archigos 4.1 | PDF | none recorded |
| ESRB OP13 Annex A | | none recorded |

* The NAVCO connector checks only that the Dataverse version record lists the expected md5. It never fetches
  the file, and the quoted definitions are constants in `atlas_navco.CODEBOOK`.
* This covers 16,440 claims: NAVCO 4,268, MEC 3,504, MMP 7,221, TIES 1,024, Archigos 327 and ESRB 96.
* The quotes are plausible and match the published codebooks as far as known. But the "verbatim" guarantee
  that the CAUSDATA connectors enforce (fetch, archive, compare the sha256, `claims_withheld` on drift) is
  missing here.

**Fix.**

* Fetch and archive each codebook through the metered client once per revision, pin its sha256 in the
  relation registry, and withhold claims on a mismatch, as `atlas_icb`, `atlas_mid` and `atlas_ucdp_term` do.
* Generate the quotes from the archived bytes.

### M5 — Medium — Archigos: one attribute reified into a cause–effect pair

**Evidence.**

* Archigos codes one fact per leader spell: the exit (Irregular) and its `exitcode`. The connector builds two
  events from it, `archigos:coup:<obsid>` and `archigos:exit:<obsid>`, with the same day, and asserts
  `coded_outcome` coup → removal for all 327 claims. The coup *is* the removal. The causal edge exists only
  because REVA split one coded attribute in two.
* 44 claims label a removal "by Other Government Actors" a coup event. The quoted definition calls only the
  military removals "the classic 'military coup'". Example: 24325 PAN-1940.
* 10 claims with "…with Foreign Support" carry a definition that describes the "without foreign support"
  removals. Example: 24303 GUA-1951, the removal of Árbenz, "Removed by Military, with Foreign Support".
* `mechanism` holds the exitcode category label, e.g. "Removed by Military, without Foreign Support". It is
  verbatim, but it is a category, not a mechanism.

**Fix.**

* Make one event, the irregular removal, with the perpetrator and foreign-support qualifiers, and no causal
  claim. Or, if a claim is kept, use a relation that does not imply a distinct prior event.
* Attach the definition of each exitcode separately.
* Leave `mechanism` NULL.

### M6 — Medium — UCDP peace agreements: an association made causal

**Evidence.**

* The PA codebook (archived, verified) defines `conflict_id` as "Unique conflict identifiers for each
  individual conflicts related to the dyads signing the peace agreement". It defines a peace agreement as one
  that "addresses the disputed incompatibility". Neither text says the agreement is a reaction or response.
* `relations_datasets.yaml` turns this into `reaction_to` (conflict → agreement): "each agreement is coded as
  a response to that armed conflict". The claims are worded "<conflict> provoked the reaction <agreement>".
* This covers 382 claims, e.g. 7380 and 7479.

**Fix.** Store the pair as a context statement (`ucdppa:addresses_conflict`). If it is kept as a claim, put
REVA's interpretation in the claim (`scope.reva_reading`) and in the wording.

### M7 — Medium — ESRB: regex extraction from a free-text field

**Evidence.**

* The quoted ESRB definition says the "Accelerator and motivation" field is "Format: text".
  `atlas_ecrises.accelerator_objectives` regex-matches five objective names in the part of the text before
  "Motivation:" and creates one `contributing_factor` claim (polarity `promotes`) per match. That is
  pattern-based text extraction, which principle 7 sends to the candidate space.
* Most cells start with a clean list. These do not:
  * **Narrative cells.** 7795 (Residual events G30, Latvia 1998) gets "exposure concentration" out of "losses
    were further amplified by the exposure concentration". 7775 and 7776 (G70, Sweden 2008) come from
    "the main vulnerability comes under the heading 'mismatches…'. There were also elements of excessive
    credit growth".
  * **Loose mapping.** 7785, 7787 and 7789 map "mismatches in balance sheets" / "mismatches" to "mismatches
    and market illiquidity". "Currency mismatch" (G39) is not mapped.
* **Mechanism truncated.** The text is cut at 4,000 characters with no marker: 7713–7716 (G10 Cyprus, 6,651
  characters) and 7750–7751 (G36, 4,290 characters). Another 21 claims had surrounding whitespace stripped,
  which is benign.

**Fix.**

* Accept only cells whose head is a comma-separated list of the five objective names. Send anything else to
  `kg.causal_candidate` as T5.
* Store the full cell and mark truncation explicitly (`mechanism_truncated: true` plus the raw cell
  selector).

### L1 — Low — NAVCO 2.1 outcome term mismatch

180 claims with `success = 0` are coded from `progress`, e.g. 9170 "campaign ends in failure; success = 0".
They cite codebook term `success`, whose definition is that of success = 1: "achieved at least one stated,
maximalist goal". **Fix:** use term `progress`, whose definition is already in `atlas_navco.CODEBOOK["n21"]`,
for these claims.

### L2 — Low — outcomes asserted for ongoing campaigns

189 claims assert `coded_outcome` with the term ONGOING and the coded value "failure (ongoing)": MEC 90, e.g.
12069; NAVCO 1.2 37, e.g. 7807; NAVCO 1.3 62. The campaign had not ended at the dataset's cut-off. **Fix:** no
outcome claim for ONGOING, or a scope `as_of` date with wording "coded as ongoing (failure) as of
2018-12-31".

### L3 — Low — inconsistent handling of non-responses

* ICB2 keeps MAJRES = 1 "No response-inaction" as `reaction_to` ("provoked the reaction"): 1060, 1381, 1435,
  1456, 1585.
* MMP drops "ignore", which its codebook lists among the state's "actions (or inaction)". 296 claims lose it
  from the coded list, and protests with only "ignore" get no claim.

**Fix:** one policy for both. Either keep inaction with a wording such as "no response coded", or drop it in
both datasets.

### L4 — Low — KG import drops reference and qualifier properties

* **References.** The KG import keeps only allow-listed properties. 11 claims are T4 although the raw Wikidata
  statement has a reference: P1651 YouTube video ID, P4051 or P724. The claims are 27065–27067, 27070, 27125,
  27126, 27151, 27274, 27440, 27441 and 27446. 13 more lose some reference properties (P958, P123, P1810,
  P268…).
* **Qualifiers.** 20 claims lose qualifiers, including scope restrictions: P1011 "excluding" (27241), P1012
  "including" (27782), P1001 jurisdiction (26867, a `SCOPE_PLACE_PIDS` property), and nested P1542, P828 or
  P1479.

**Fix:** for causal claims, store the statement's references and qualifiers verbatim from the raw snak JSON,
whatever the KG allow-list, and compute the tier on the verbatim references, after H1.

### L5 — Low — truncated or unarchived definition text

* The MID `outcome` definition in `relations_datasets.yaml` stops mid-sentence ("…since no alteration of the
  status quo took") at a page break. It travels with all 2,419 MID claims. It is verbatim but truncated.
* The NOAA definitions prefix "Tsunami Cause Code. Valid values: 0 to 11. The source of the tsunami:" is a
  connector constant. Only the value labels come from the archived descriptor `ccb91b45…`.

**Fix:** complete the MID quote across the page break, and archive the HazEL help page or drop the prefix.

### L6 — Low — `coded_outcome` counted as causal

* 9,912 claims (36 %) link an event to an outcome entity that REVA created for it, e.g. `icb:crisis:N` →
  `icb:crisis:N:outcome` and `mid:dispute:D` → `…:outcome`. The relation is honestly labelled "has coded
  outcome".
* Even so, these claims are counted as "causal claims" in reports, they inflate the B total, and they sit in
  the `cause` family that chain search traverses.

**Fix:** a separate family (`outcome`), reported apart and excluded from "causal" totals and corroboration. Or
keep the family and state the count separately everywhere.

### L7 — Low — self-loop and cycles loaded unflagged

* 26888 "peace treaty has cause peace treaty" (Q625298, T4, D) is a self-loop.
* There are 5 cycles over 13 Wikidata nodes (Afrin operation ↔ YPG; Barbarossa → Eastern Front → Cold War →
  anti-communism → Barbarossa; plus the 3 generic cycles of M1).
* `/v1/causal/consistency` reports them, but they stay ordinary claims.

**Fix:** reject self-loops at load (`rep.reject("self_loop")`). Add a `consistency.cycle` fact to the grade
rows of cycle members, so the "why" view shows it.

### L8 — Low — retraction behaviour not evidenced live; minor code gaps

* `kg.causal_claim_retraction` has 0 rows. Retraction is covered only by the integration tests
  (`test_kg_retraction_and_reinstatement`, `test_dataset_claims_t1_codebook_retraction_and_rejections`).
* In `load_kg_claims`, a statement whose value becomes somevalue or novalue drops out of the scan (the query
  requires `object_qid IS NOT NULL`). It is then retracted with reason `source_statement_retracted`, which is
  wrong: the statement still exists.
* `load_dataset_claims` silently downgrades `complete = True` to partial when any row is rejected. The flag
  appears only in `rejected_counts`, and not in the load's `parameters.complete`, which records the
  downgraded value without saying why.
* No claim is retracted when its cause or effect entity is retracted from the KG.

**Fix:**

* Add a live smoke check that re-runs a delivery minus one row in a throwaway database.
* Distinguish the `value_not_an_item` reason.
* Record `complete_requested` and `complete_effective` separately.
* Retract or flag claims whose endpoint is retracted.

## 6. Limitations

* **Unresolved P5102 values.** The P5102 values (Q132555, Q712144, Q20820099, Q110143752) and some P3680
  entities (Q1140115, Q130786, Q4217811, Q1970048) could not be resolved to labels without a network fetch, so
  they are reported by QID.
* **Heuristic M1 count.** M1 counts endpoints that have P279 and no date, and both over- and under-counts are
  possible. The cycles and the chain example are exact.
* **Unarchived codebooks.** For the M4 sources, the codebook-relation check (§2.3, check 2) was made against
  the quoted definitions and the column semantics. It could not be made against archived codebook bytes.
* **Moving code.** Other agents were editing `src/reva/causal/**` and the connectors during the audit. Line
  references are to commit `0925135`.
