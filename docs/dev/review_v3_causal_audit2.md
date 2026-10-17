# V3 causal claims: second adversarial audit (agent AUDIT2, 2026-10-17)

Scope: the causal claims in the dev database `reva` (origin `live`), read-only, after the fixes reported by CAUSFIX
(`v3_causfix.md`), AUDFIX (`v3_audfix.md`), SAMEAS (`v3_sameas.md`) and CAUSMORE (`v3_causmore.md`). The first
audit is `review_v3_causal_audit.md` (defects H1–H4, M1–M7, L1–L8). The rules are `registry/causal/grading.yaml`
version 2 (sha256 `70a73066…`). None of the fix reports was taken on trust: each defect was re-checked against the
database and the archived bytes. No code, registry or claim row was changed. The only writes were one temporary
`read` API token (created for §5, then revoked: token id `3b2dd5a2…`, `revoked_at` set). This report is the only
committed output.

## 1. Summary

**Mechanical fidelity is still clean.**

* 0 fidelity errors in the 365-claim sample, and 0 in the population checks of the new sources: IDMC 22,108 claims,
  COW wars 892, UCDP dyad termination 915, NOAA 2,186, Wikidata 1,672 and ESRB 69.
* All 886 archived objects that the current claims cite are present, and their sha256 recomputes (raw rows, NOAA
  reciprocal pages, codebook `quote_source` documents, NCEI code lists).
* All 48 distinct quoted codebook definitions are verbatim in the archived codebooks, and the IDMC definition is
  verbatim in its README cell.
* The temporal check recomputes exactly for 51,446 of 51,446 current claims, and the grade decision table for
  51,446 of 51,446.
* No version-1 row was edited (0 updates, 0 deletes on any `kg.causal_*` table since the server started, before the
  first causal load). 0 current claims are ungraded.

**Status of the 19 defects of the first audit:**

* **14 fixed:** H1, H2, H4, M3, M4, M5, M6, M7, L1, L2, L3, L4, L5 and L7. L6 is fixed on the display side.
* **4 partly fixed:** H3, M1, M2 and L8.
* **0 not fixed.**

Several fixes are honest in mechanism, but the reader still cannot see what the fix was meant to show:

* **H3.** For 11 of the 12 attributed claims, the API wording names the claimant by a bare QID. For example "records
  that Q7747 and Q1140115 assert…", where Q7747 is Vladimir Putin. All 26 "nature of statement" values are shown
  as bare QIDs.
* **M2.** Endpoints missing from the KG now affect 846 claims (51 % of Wikidata claims), up from 324. The chain
  service traverses them as intermediate nodes.

**12 new defects, none High.**

* **Medium (4):**
  * every retraction (4,264 claims, 1,635 KG statements) is labelled `removed_at_source`, although the providers
    removed nothing;
  * Wikidata "sourcing circumstances" hedges (P1480: presumably, possibly) are ignored by grade, wording and chains;
  * claimants and "nature" values are shown as bare QIDs;
  * 1,206 IDMC claims rest on preventive evacuations or on a hazard *risk*, and they are worded "X has effect
    displacement" with no qualification.
* **Low (8):** IDMC placeholder-date events, one UCDP dyad outcome silently chosen among two coded ones,
  tautological temporal checks on 7,538 outcome claims, missing `quote_source` on the CAUSMORE claims, no licence
  in the causal API, UCDP "year assigned" precision, one Wikidata DOI claim graded like an expert dataset, and
  event identities that REVA made for IDMC rows without an Event ID.

PA-X has 0 current causal claims. Retractions are append-only and keep their history. The COW Intra-State v4.1
numbering choice is supported by the archived bytes, except for the v5.1 counter-evidence (§4.3).

## 2. Method

### 2.1 Snapshot

The audited population is the 51,446 current claims (`kg.causal_claim_current`, ids up to 75,705), with their
latest grade rows (`kg.causal_grade`, 105,696 rows; loads 1–53, the last grading load at 03:08:15 UTC). The database
did not move during the audit: the same maximum ids and counts were re-read at the end. Code reference: commit
`7355e5d`, the working tree used by the API test client. APIFIX committed `8475cc8` after the API checks.

All tables were exported read-only (`row_to_json`) to the scratchpad. All checks are independent re-implementations
and none of them calls `reva.causal`. They are listed below.

| check | what it reads | applied to |
|---|---|---|
| sha256 of every cited object | `var/objects`, `var/kg/objects` | 886 objects |
| raw row re-read | own xlsx (zipfile + XML), CSV/TSV, zip, tar, gzip JSON readers. TIES `.xls` through the repository's BIFF reader `read_xls_first_sheet`, as in audit 1 | population: IDMC, COW wars, UCDP dyad, NOAA, Wikidata, ESRB. Sample: the other datasets |
| codebook text verbatim | `pdftotext` (layout and raw), docx XML, xlsx README cells | 48 distinct definitions (all claims) |
| temporal recomputation | KG date windows, grading.yaml v2 rules (event start, inception spans, claim time qualifier) | 51,446 claims |
| date windows vs raw time strings | own precision / Julian→Gregorian arithmetic | 782 date statements on sample endpoints |
| grade decision table | grading.yaml v2 order E→E→D→D→D→A→B→B→C→D | 51,446 claims |
| Wikidata tier and verbatim refs/qualifiers | archived entity JSON located by statement id | 1,672 claims |
| structure | SCC (Tarjan), self-loops, duplicates, missing entities, same_as clusters | 51,446 claims, 915 same_as links |
| retraction history | `kg.causal_claim_retraction`, `kg.statement_retraction`, `pg_stat_user_tables` | all |
| API wording | `fastapi.testclient` on `reva.api.app.create_app`, Bearer read token | 28 calls, 40+ claims (§5) |

### 2.2 Sample (seed 2026101702, recorded)

The sample was drawn as follows:

* **Seed.** One `random.Random(2026101702)` drew all strata in the order of the table below, with `rng.sample`
  over sorted claim ids.
* **Weighting.** The sample is weighted toward the new sources and toward the risk areas of the brief: COW wars,
  IDMC, UCDP dyad, Wikidata attribution, NOAA validity caps, and same_as corroboration (which exists only in
  history: §4.5).
* **Size.** 365 claims, of which 355 are current and 10 are historical: interim same_as A grades, now retracted.
* **Results.**
  * Every sampled claim's raw bytes were re-hashed and its row re-read. Relation, direction, ids, codebook term,
    quote, dates, temporal check and rule ids were re-checked.
  * **Fidelity errors: 0 of 365.**
  * The last column of the table counts the honesty findings (§3, §4) that touch each stratum.

| stratum | N | n | fidelity errors | honesty findings in the stratum |
|---|---:|---:|---:|---|
| idmc / preventive evacuation | 1,178 | 20 | 0 | N4: 20 |
| idmc / no Event ID (REVA row key) | 2,695 | 20 | 0 | N12: 20, N5: 1 |
| idmc / multi-row event | 642 | 15 | 0 | N4: 3 |
| idmc / other | 17,701 | 34 | 0 | — |
| idmc / inconsistent (census) | 1 | 1 | 0 | — (genuine source inconsistency, E) |
| cow / inter | 337 | 15 | 0 | N7: 14 |
| cow / extra | 161 | 12 | 0 | N7: 12 |
| cow / intra (v4.1) | 332 | 15 | 0 | N7: 15 |
| cow / non-state | 62 | 8 | 0 | N7: 8 |
| ucdp dyad / multi-terminal or inconsistent (census) | 5 | 5 | 0 | N6: 4, N7: 4 |
| ucdp dyad / d_outcome 1…6 | 911 | 36 | 0 | N7: 25 |
| wd / P3680 (census) | 12 | 12 | 0 | N3: 11, M1: 5, M2: 3 |
| wd / P5102 (census) | 25 (+1 also P3680) | 25 | 0 | N3: 25, M1: 21, M2: 12 |
| wd / P1480 (census) | 4 | 4 | 0 | N2: 4, M2: 2 |
| wd / P1310 (census) | 1 | 1 | 0 | M1: 1 |
| wd / grade B (census) | 9 | 9 | 0 | M1: 2, M2: 1 |
| wd / T3 C | 157 | 12 | 0 | M1: 5, M2: 7 |
| wd / T4 non-source refs only | 187 | 8 | 0 | M2: 3 |
| wd / T4 unreferenced | 1,276 | 8 | 0 | M1: 2, M2: 4 |
| wd / cycle members | 28 | 6 | 0 | M1: 1, M2: 1 |
| noaa / validity 0–2 | 788 | 20 | 0 | — (all D, doubt worded) |
| noaa / cause code 2 (census) | 23 | 23 | 0 | — (all D) |
| noaa / validity 3–4 | 1,395 | 10 | 0 | — |
| other: icb1, icb2, mid5, term_conflict, mmp, mec, navco12/13/21, ties, eusanct, fcdb | 23,673 | 36 (3 each) | 0 | N7: 12 |
| history / same_as A (load 32; UCDP PA + PA-X, retracted) | 604 | 10 | 0 | N1: 10 |

<details><summary>365 sampled claim ids (`kg.causal_claim.id`)</summary>

387, 720, 970, 1197, 2855, 4173, 4927, 5895, 6267, 6826, 6938, 7212, 7432, 7440, 7531, 7612, 23858, 23887,
23970, 28493, 28699, 29745, 30496, 30745, 31059, 31796, 31850, 31851, 31877, 31894, 31902, 31939, 31947,
31969, 32021, 32022, 32043, 32067, 32072, 32081, 32148, 32157, 32197, 32214, 32220, 32225, 32244, 32261,
32268, 32276, 32283, 32286, 32297, 32315, 32323, 32357, 32362, 32368, 32391, 32402, 32473, 32480, 32484,
32513, 32527, 32556, 32607, 32629, 32648, 32650, 32655, 32665, 32667, 32678, 32679, 32820, 33143, 33223,
33592, 34360, 34591, 34674, 34698, 34701, 35038, 35424, 35825, 36139, 36230, 37214, 37564, 37640, 38128,
38344, 39093, 39505, 39612, 39643, 40171, 40268, 40320, 41018, 41020, 41052, 41054, 41067, 41088, 41102,
41110, 41307, 41429, 41766, 43542, 43594, 43610, 43694, 44590, 44686, 44800, 45037, 45403, 45614, 45775,
46056, 46101, 47111, 48858, 48996, 49248, 49393, 49408, 49542, 49619, 49835, 49873, 50517, 50530, 50642,
50644, 50745, 50785, 50864, 51226, 51283, 51362, 52619, 52628, 52773, 53041, 53074, 53123, 53302, 53335,
53502, 53588, 53755, 53781, 53834, 53847, 54134, 54180, 54210, 54443, 54474, 54536, 55044, 55055, 55075,
55089, 55108, 55112, 55135, 55185, 55245, 55257, 55267, 55268, 55273, 55282, 55301, 55360, 55371, 55406,
55413, 55430, 55474, 55492, 55500, 55555, 55557, 55576, 55581, 55592, 55605, 55638, 55671, 55701, 55705,
55727, 55791, 55812, 55820, 55824, 55851, 55872, 55923, 56110, 56552, 56587, 56708, 56861, 57043, 57582,
58305, 60066, 60748, 61207, 62891, 64690, 68154, 69813, 70755, 70757, 70817, 71114, 71337, 71451, 71847,
71905, 71916, 71933, 71971, 72004, 72031, 72055, 72058, 72084, 72086, 72210, 72219, 72289, 72402, 72403,
72423, 72457, 72458, 72502, 72533, 72546, 72630, 72632, 72951, 73000, 73070, 73165, 73187, 73217, 73220,
73278, 73315, 73409, 73438, 73485, 73499, 73520, 73560, 73565, 73572, 73621, 73649, 73655, 73809, 73814,
73861, 73878, 73982, 74002, 74006, 74028, 74030, 74067, 74076, 74122, 74127, 74193, 74194, 74253, 74263,
74289, 74301, 74307, 74309, 74316, 74353, 74354, 74356, 74369, 74370, 74389, 74392, 74394, 74395, 74397,
74400, 74402, 74414, 74436, 74448, 74449, 74458, 74459, 74485, 74489, 74496, 74513, 74520, 74522, 74523,
74590, 74597, 74616, 74622, 74630, 74672, 74678, 74680, 74684, 74685, 74727, 74737, 74746, 74747, 74748,
74777, 74812, 74813, 74823, 74843, 74844, 74845, 74863, 74902, 74914, 75004, 75011, 75012, 75094, 75147,
75168, 75177, 75275, 75280, 75324, 75380, 75416, 75429, 75438, 75443, 75454, 75483, 75493, 75499, 75505,
75605, 75645

</details>

**Claim ids moved.** Most claims of audit 1 have new versions (refs and qualifiers re-read, new connector
versions). Old ids are mapped to current ones by `claim_key`, e.g. 27009 → 74253, 26888 → 74132 and 24604 → 71845.

## 3. Status of the audit-1 defects

| # | Status | Re-check (all current claims unless stated) and evidence |
|---|---|---|
| H1 | **fixed** | **Tier recompute.** The tier was recomputed from the raw entity JSON (DOI/ISBN → T2; a reference that is not only P143/P4656/P3452/P887 (+P813) → T3; else T4). It agrees for 1,672 of 1,672 claims (T2 2, T3 190, T4 1,480). 187 claims carry `tier.T4.non_source_references_only`. **Audit ids.** 26791→74035, 26817→74061, 26818→74062, 26822→74066 and 26823→74067 are all T4 D. 0 non-import references point to Wikipedia or Wikidata URLs. **Residual (arguable).** 6 T3 claims rest only on references that combine P3452 "inferred from" with a quotation, a section or "stated in" (e.g. `{P1683, P3452, P958}`). |
| H2 | **fixed** | **Wikidata B grades.** There are 9 B claims: 8 T3 corroborated and 1 T2. Their groups were recomputed: Britannica vs DHS / Congress.gov (9/11 → DHS, Patriot Act), National Archives vs Smithsonian (JFK autopsy), and CDC + WHO case definition vs WHO situation reports. All are distinct publishers. **Audit pairs.** 27138/27363→74382/74607, 27157/27377, 27346/27350, 27372/27144, 27214/27568 and 27064/27076 now all have corroboration 1. **Residual hardening.** Upstream keys are per URL, not per publisher, so two documents of one organisation on mirror statements would count twice (none today). |
| H3 | **partly** → **fixed (FIXA)**: no bare QID is shown any more ("Q7747 (libellé non chargé)"), items queued at harvester tier 1 (`v3_fixa.md`) | **Mechanism: correct.** All 12 P3680 claims carry `attribution.supported_by`: 8 are decided D by `grade.D.attributed` and 4 are E. All 26 P5102 claims carry `nature.qualified`. The wording reads "Wikidata … records that X asserts that …; Wikidata does not assert it itself". The chain service skips them: `/chain Q112127201→Q110999040` reports `quarantined_skipped: 2` and composes only the unattributed 74302. **But the claimant cannot be read.** 11 of 12 attributed claims show the party as a bare QID: Q7747 on 74253, 74301, 74309, 74389, 74397 and 74522; Q855 and Q130786 on 74597; Q22686 on 75380; Q1140115, Q4217811, Q1970048 and Q1672300 elsewhere. Only Q212 "Ukraine" has a KG label. All 7 distinct P5102 values (26 claims) are shown as bare QIDs, e.g. 74400: "(Wikidata qualifies the nature of this statement as: Q132555)". See also N2 and N3. |
| H4 | **fixed** | All 2,186 NOAA claims carry `scope.source_validity`. Re-read from the archived pages: code and label equal the archived NCEI descriptors (`081f620e…` validity, `ccb91b45…` causes). The codes and the reciprocal page link agree for all claims. 791 claims have a doubtful entry: 783 are D (`grade.D.source_doubtful`) and 8 are E. 0 doubtful claims are graded B. The wording names the doubt, e.g. 71847: "(the source codes it as doubtful: Very Doubtful Tsunami (eventValidity = 1); … Questionable Earthquake (causeCode = 2))". Audit ids 24604/24606/24607/24695 → 71845/71847/71848/71936: all D. |
| M1 | **partly** → **fixed, display (FIXA)**: class-level claims in the `/why` lane `generic`, not counted as causes | Chains: `/chain Q110999040→Q454` (6 hops) is not composed, with "5 class node(s) were not traversed". Claims: 625 of 1,672 Wikidata claims (37 %) still have an endpoint that is a class by CAUSFIX's own KG rule (own P279, or a P31 target without P31). 150 have it on both sides (136 D, 12 C, 2 E). These are loaded, graded and shown in `/why` lanes, e.g. 74074 "human → artificial object" (was 26830) and 74049 / 74075 "… → war". |
| M2 | **partly** → **fixed for chains (FIXA)**: unknown entities never intermediate nodes; the KG gap waits for the harvest | **Reporting: fixed.** 846 of 1,672 Wikidata claims (50.6 %, 659 entities) have an endpoint with no `kg.entity` row, which matches the loader's count. There are 0 such claims among the dataset claims. **Not fixed.** (a) The share grew from 32 % to 51 %. (b) The chain search traverses unknown entities as intermediate nodes. `/chain Q201684→Q186356` returns the path "Wall Street crash of 1929 → **Q8698** (label "Q8698", kind null) → New Deal", grade D. (c) 9 of the missing entities are already archived as entity documents but not loaded: Q8698 (Great Depression), Q43109, Q66344, Q381072, Q177626, Q8072, Q8669, Q45382 and Q40231. |
| M3 | **fixed** | 0 effect date statements still carry a "copied" or "dating" qualifier. MMP 7,221, MEC 1,115 and NAVCO 1.2 352 are `undetermined`, with `undated: ["effect"]`, a reason and a typed-missing `effect_time`. The Archigos claims are gone. Every one of the 10,247 `undetermined` grades names the undated endpoint or the inception reason. Variant still present: N7. |
| M4 | **fixed** | **Codebook quotes.** The 48 distinct quoted definitions behind all T1 claims were checked against the archived bytes, and all are verbatim after whitespace normalisation. The documents: NAVCO 1.2/1.3/2.1, MEC (docx), MMP, TIES, ESRB OP13, ICB1/2, the MID manuals and codebook (zip members), the EUSANCT codebook (tar member), UCDP termination and the four COW war codebooks. NAVCO 1.2 SUCCESS spans the table column of pp. 10–11 and was checked by column extraction. **The rest.** The NOAA definitions equal the archived descriptor labels, and the IDMC definition equals README cell A24. The 8 AUDFIX codebooks are archived under their pinned sha256. Residual: N8. |
| M5 | **fixed** | 0 current `archigos.leaders` claims; 327 retracted. The 1,635 `archigos:coup:*` statements are retracted (0 current) and the 327 entities are `superseded_record`. Audit ids 24325 and 24303 are retracted. Retraction reason: N1. |
| M6 | **fixed** | 0 current `peace_agreements` claims; 382 retracted (7380 and 7479 included). The PA-X links are context only (`pax:ucdp_conflict` 2,045, `pax:cow_war` 1,289). `/why` of `ucdp:pa:31` and `pax:agt:879`: "No cause is asserted". |
| M7 | **fixed** | 69 claims. For every cited cell (re-read from raw `bf353524…`, both sheets), the first paragraph is exactly a comma list of the five ESRB objective names. The mechanism is byte-equal to the raw cell for 65 claims. The 4 truncated G10 claims (70760–70763) carry `mechanism_truncated {stored 4000, full 6651}`, which recomputes. The 27 non-list cells (7795, 7775, 7776, 7785, 7787, 7789, 7750, 7751 among them) are retracted. |
| L1 | **fixed** | 180 NAVCO 2.1 `success = 0` claims cite `progress`, 98 cite `success`. Sample 60066: raw `success=0`, `progress=3`, term `progress`. |
| L2 | **fixed** | 0 outcome claims for ONGOING. The NAVCO 1.x and MEC coded values are failure 1,344, limited 945 and success 920. COW outcome 5 ("ongoing as of 12/31/2007", 19 records) gives no claim. 189 claims are retracted (12069, 7807…). |
| L3 | **fixed** | Raw `majres` of the 1,121 current ICB2 `reaction_to` claims takes the values 2–9 only. The 5 MAJRES = 1 rows are retracted (1060, 1381, 1435, 1456, 1585). 296 MMP claims carry `inaction_not_a_reaction`. |
| L4 | **fixed** | References and qualifiers are equal to the raw entity JSON for 1,672 of 1,672 claims (independent snak comparison). 27066→74310 and 27274→74518 are now T3. P1011 (74485), P1012 (75026) and P1001 (74111) are kept. These scope qualifiers are stored but not worded (N2). |
| L5 | **fixed** | The MID `outcome` quote ends "…no alteration of the status quo took place." The NOAA definitions have no help-page prefix: all 2,186 equal `causeCode k = "<descriptor label>"`. |
| L6 | **fixed (display)** | `/why` has a separate lane, "outcome": "coded outcomes (how the event ended according to a dataset; not causes)". Its counts are given apart (`causal_claims` 0 / `outcome_claims` 1 for `cowwar:outcome:139:2:1`). `/chain cow:war:139→cowwar:outcome:139:2:1` refuses: "outcomes are never composed into causal chains". The share is now 11,203 coded outcomes (21.8 % of claims). |
| L7 | **fixed** | An independent SCC over asserted causal claims (excluding `coded_outcome`) finds 7 components with 27 edges, plus the self-loop 74132 (was 26888). This is exactly the set flagged `consistency.cycle` / `consistency.self_loop`. Merging same_as clusters creates no new cycle and no identity self-loop. `/consistency` lists them. |
| L8 | **partly** → **fixed (FIXA)**: reasons derived; the 4,264 + 1,635 old rows annotated (see N1) | **Fixed: history is kept.** Retraction runs live and is append-only: 4,264 claim retractions and 1,635 statement retractions. 0 updates or deletes on any causal table since the server started. All retracted rows and their grade rows are kept, with 0 grade rows written after a retraction and 0 retractions of a non-latest version. `complete_requested/effective` are recorded. **Not fixed: the reason.** Every one of the 4,264 retractions says `removed_at_source`, although the providers removed nothing (N1). |

## 4. New-source checks

### 4.1 IDMC GIDD disasters (22,108 `has_effect` claims)

* **Raw fidelity.** The archived workbook `60dd7c1e…` was re-read with an independent xlsx reader: 22,119 data
  rows.
  * Every claim points to its row, with the ISO3, Year, event key, "Displacement occurred" scope and a positive
    raw figure.
  * The event P580 equals the earliest Excel start date of its rows, and the displacement P585 equals the Year.
  * 22,108 rows have a claim. The other 11 are the second copies of 10 groups of fully identical provider rows,
    none of which has an Event ID and none of which loses a figure.
* **What the row asserts.** The README defines the figures as internal displacements "as a result of disasters over
  the reporting year" (A45/A46), per event row. So "event → displacement" is IDMC's own attribution, and
  `has_effect` is a fair reading for an ordinary row. Two kinds of row are not ordinary:
  * N4: 1,178 rows report preventive evacuations, and 57 event names are hazard *risks*.
  * N5: 1,103 claims rest on placeholder-date names.
* **Rows without an Event ID.** There are 2,706 such rows, giving 2,695 claims. Each row is its own event,
  `idmc:event:r<sha256[:16]>`, and is never grouped by name. That is honest, but the identity is REVA's (N12):
  188 such names recur on several rows, e.g. "Typhoon morakot (kiko)" × 3.
* **Duplicates.** Rows sharing (event, ISO3, Year) occur only for the 10 identical-row groups, so no figure is
  dropped. The collapse is reported in the ingest result only, not in the KG.
* **Temporal.** 22,091 claims are overlapping, 16 consistent and 1 inconsistent: 37214, whose source names the
  event "… 29/03/2017" but dates its start 2018-03-29. The inconsistent claim is graded E and was not corrected,
  which is correct.
* **Licence.**
  * The README (A28) says "This content is licensed under CC BY-NC", and HDX says CC BY.
  * `registry/sources/idmc_gidd.yaml` applies CC BY-NC, sets `open_licence: false` and carries a review flag. The
    connector RIGHTS do the same.
  * Not carried by the causal API (N9).

### 4.2 COW war data v4 (892 `coded_outcome` claims)

* **Raw fidelity.** The four archived CSVs were re-read (CR line endings, latin-1).
  * Every claim's `lines` are the records of its WarNum, and the Outcome code and label equal the scope.
  * Non-inter wars have a single Outcome across all their records, and the claim cites all of them.
  * Code 5 and missing codes give no claim.
  * By file: inter 337 claims for 337 records (95 wars), extra 161 for 163 wars, intra 332 for 334 wars,
    non-state 62.
* **Dates.** The outcome P585 equals the latest coded end of the cited records, for all 892 claims (extra-state's
  header `EndDay2 ` with a trailing space is read correctly).
* **Codebooks.** The four Outcome definitions are verbatim in the archived PDFs (`875bf75f…`, `0746a2f1…`,
  `7a15ea58…`, `82164764…`).

### 4.3 COW Intra-State v4.1 numbering choice

* **Supported by the archived bytes:**
  * All 66 distinct `CowWar` numbers in the archived PA-X v10 CSV (`a11d6491…`) resolve among the v4 war numbers.
  * The archived PA-X v10 codebook defines `CowWar` by citing Sarkees & Wayman, *Resort to War … 1816–2007*,
    which is the v4 war list.
  * `cow:war:856` is "Eighth Colombia" (intra v4.1, Outcome 5), as CAUSMORE says.
* **Not verifiable.** CAUSMORE's counter-evidence (v5.1 renumbers 856 as "Romanian War of 1989", and 9 + 7 PA-X
  numbers would not resolve or would change meaning) was never fetched. No v5.1 file has an `evidence.acquisition`
  row. The decision is sound and documented, but its stated reason cannot be re-checked offline (N8).

### 4.4 UCDP dyad termination (915 `coded_outcome` claims)

* **Raw fidelity.** The archived CSV `37b250d2…` (3,432 dyad-years) was re-read. Every claim is a `d_epterm = 1`
  row of its `d_epid`, and the term `d_outcome=k` equals the raw code. The termination P585 and the dyad P580 trace
  to `d_ependdate` and to `start_date2` or the first year.
* **Outcome definitions.** All six `d_outcome` definitions are verbatim (pp. 3–4 of `648c1601…`). The outcome-5
  caveat is kept.
* **Multi-terminal episodes.** 7 episodes have two terminal rows. The last row is kept and the first is dropped
  silently, and in 4 of them the two coded outcomes differ (N6).
* **Inconsistent claim.** 55075 is a genuine source inconsistency: dyad start 1977-06-01, end 1977-05-30 with
  precision 2. It is graded E.
* **Precision codes.** The mapping of `d_ependprec` 1–6 to day, month and year precision follows the codebook,
  except that 6 "Year is assigned … subjective judgment" is stored as an ordinary year (N10).

### 4.5 same_as corroboration and identity steps

* **The links.** There are 915 current `events:same_as` links. The evidence was checked against the raw bytes:
  * all 313 PA-X ↔ UCDP links equal the single `UcdpAgr` of the PA-X row, and the UCDP agreement exists in the
    archived UCDP PA workbook;
  * the UCDP ids 1113 and 1070, each cited by more than one PA-X agreement, are not linked;
  * all 84 EUSANCT ↔ TIES links have an identical case id and a `datasource` naming TIES;
  * the NAVCO links cite the provider's `contention:same_campaign_as` statement;
  * 459 links carry `independent_sources: false`.
* **Current same_as corroboration: none.** 0 current grade rows carry `corroboration.via_same_as`.
* **History.** The 604 interim A grades of load 32 are kept as history: 302 UCDP PA claims (superseded by a B row,
  then retracted) and 302 PA-X claims (retracted). The 10 sampled ones re-derive correctly from raw. For example,
  7440 `ucdp:conflict:316 → ucdp:pa:1107` is corroborated by the PA-X claim for agreement 601, whose `UcdpAgr` is
  1107.
* **Identity steps in chains.** `/chain ties:case:1990122101 → eusanct:policy_result:1990122101` returns one
  identity hop plus one claim hop:
  * the identity hop has `kind: identity`, `hop_grade: null`, the note "…it is not a causal claim and adds no
    causal hop", and its `same_as` evidence (rule, statement key, `independent_sources: false`);
  * the length counts the causal hop only (1);
  * the wording reads "… It crosses 1 identity link(s) … an identity, not a causal claim".

### 4.6 PA-X

0 current causal claims. All 3,334 claims of `atlas.pax` 1.0.0 are retracted, and their rows and grade rows are
kept: 302 of them still end with the interim A row, which is invisible because they are retracted. The PA-X raw
file has the same sha256 (`a11d6491…`) in both the 1.0.0 and the 1.1.0 runs, which is the evidence for N1.

## 5. API wording (test client, temporary read token, revoked)

The API was called 28 times: `/v1/causal/events/{id}/why` and `/effects`, `/v1/causal/chain` and
`/v1/causal/consistency`.

| claim / call | what is shown | verdict |
|---|---|---|
| 74253 (`/why Q15860072`) | "Wikidata … records that **Q7747 and Q1140115** assert that Russo-Ukrainian war has cause eastward expansion of NATO; Wikidata does not assert it itself…", D, `attribution.supported_by[].label = "Q7747"` | attribution correct, claimant unreadable (N3) |
| 74392 (`/why Q110999040`, dissent) | "records that **Ukraine** asserts that full-scale Russo-Ukrainian war has cause anti-Ukrainian sentiment", E | correct |
| 74400, 74369 | "… (Wikidata qualifies the nature of this statement as: **Q132555**)", E / D | mechanism correct, value unreadable (N3) |
| 75168 (`/why Q114912414`) | "According to Wikidata…, Itaewon Halloween crowd crush has cause Q109905701", **C**, `attribution: null`; the raw qualifier is P1480 = Q18122778 ("presumably") | hedge dropped (N2) |
| 75438 / 75454 (`/chain Q80011627→Q82955730`) | composed into a chain of grade C with no hedge; qualifier P1480 = Q30230067 ("possibly") | hedge dropped, chained (N2) |
| 71847 (`/why ncei_hazards:tsunami:4`) | "… is the coded trigger of … (the source codes it as doubtful: Very Doubtful Tsunami (eventValidity = 1); … Questionable Earthquake (causeCode = 2))", D | correct |
| 50644 (`/why idmc:displacement:7761:LKA:2020`) | "Sri Lanka: **Risk of Landslide** - Central … has effect Internal displacements in Sri Lanka (2020)…", B, lane "causes (unqualified)"; preventive evacuation only in `scope`. No licence anywhere in the response | not qualified (N4); no licence (N9) |
| 31965 (`/why cowwar:outcome:139:2:1`) | lane `outcome` "coded outcomes (… not causes)"; summary "an outcome is not a cause"; `causal_claims 0`, `outcome_claims 1` | correct |
| 55701 (`/why ucdp:dyadepisode:88201:termination`) | "has coded outcome … Victory for Side B"; the source also codes a 2004 peace agreement for the episode, which is not shown | N6 |
| 23856 (`/chain ties:case… → eusanct:policy_result…`) | identity hop labelled, not counted, evidence shown; "Composition by REVA of 1 separately asserted claim(s) … REVA does not assert the chain" | correct |
| 74481 → (Q8698) → 74482 (`/chain Q201684→Q186356`) | second path runs through an entity unknown to the KG, shown as "Q8698", kind null | M2 not fixed in chains |
| `/chain Q110999040→Q454`, `/chain cow:war:139→outcome` | not composed, with the class-node and outcome reasons given | correct |
| `/chain Q112127201→Q110999040` | `quarantined_skipped: 2` (the P3680 claims); composes only the unattributed 74302 | correct |
| 1198, 64690 (ICB, MMP) | "provoked the reaction …", verbatim-coded response | correct |

Every response carries the `claims_not_causation` warning, and every chain carries the `chain_is_composition`
note ("a chain is not evidence that the first event caused the last").

## 6. New defects

Severity scale as in audit 1.

**Fix status** (updated by the fixing agents; FIXB = dataset side, `docs/dev/v3_fixb.md`; FIXA = loader, grading, API
and UI):

| # | Severity | Status | Where / what |
|---|---|---|---|
| N1 | Medium | (FIXA) | |
| N2 | Medium | (FIXA) | |
| N3 | Medium | (FIXA) | |
| N4 | Medium | **fixed (FIXB)** | `atlas.idmc` 1.1.0: 1,210 preventive-evacuation / hazard-risk rows give no causal claim. They are context statements `idmc:displacement_in_anticipation_of` (basis, verbatim flag, README A24/A52), and the 1,210 claims were retracted `reclassified_by_reva`. No relation of `relations.yaml` fits; the need is recorded in `v3_fixb.md` |
| N5 | Low | **fixed (FIXB)** | The archived README does not say that 01/01 means year-only or aggregate, and REVA does not claim it. The 1,400 events whose coded start is 1 January are at year precision, with `coded_start` verbatim and `day_not_relied_on`; no day is invented |
| N6 | Low | **fixed (FIXB)** | `atlas.ucdp_dyadterm` 1.1.1 / `atlas.ucdp_term` 1.2.1: every terminal row is kept (one claim per row, `source_duplicate_terminal_rows`). Differing outcomes contest each other (12 claims, grade E): 7 dyad + 2 conflict episodes. 55701 is now claims 97300 / 97301 |
| N7 | Low | (FIXA) | |
| N8 | Low | **fixed (FIXB)** | 23,219 of 23,219 COW / UCDP termination / IDMC claims carry `codebook.quote_source` (archived file sha256 + PDF page / README cell). The COW intra-state v5.1 zip is archived (`b2d1f489…`) and the v4.1 choice is recomputed from its bytes. PA-X has 0 claims (codebook archived) |
| N9 | Low | (FIXA) | |
| N10 | Low | **fixed (FIXB)** | UCDP precision 6 is year precision with the typed note `source_date_certainty: assigned` (9 claims, scope `effect_time_certainty`); 7 is a typed missing date (none in this release). Grading on it is left to the causal core (`v3_fixb.md`) |
| N11 | Low | (FIXA) | |
| N12 | Low | **fixed (FIXB)** | 2,695 REVA-made IDMC identities labelled en/fr ("enregistrement IDMC sans identifiant d'événement — identité créée par REVA"). The 10 identical-row groups (11 copies) are recorded as `idmc:source_duplicate_rows` (count + row selectors) |

**Status after the fixes** (added after the audit; the evidence below is unchanged):

| # | Status |
|---|---|
| N1 | **fixed (FIXA, `v3_fixa.md`)**: migration `0018_retraction_reasons`; loaders derive `reclassified_by_reva` / `removed_at_source` / `absent_from_delivery`; the 4,264 claim and 1,635 statement rows are annotated `reclassified_by_reva` (same raw sha256), rows unchanged |
| N2 | **fixed (FIXA)**: P1480 capped at D (`grade.D.sourcing_hedged`, grading v3), worded, never chained; P518/P1001/P1011/P1012/P1013 worded |
| N3 | **fixed, display (FIXA)**: "Qn (libellé non chargé)"; the 25 qualifier items and 677 endpoints are at harvester tier 1 (labels not fetched yet) |
| N4 | FIXB (IDMC connector) |
| N5 | FIXB (IDMC connector) |
| N6 | FIXB (UCDP connector) |
| N7 | **fixed (FIXA)**: `temporal.reason = effect_dated_by_cause_end` (7,531 claims), not counted toward A |
| N8 | FIXB (connectors) |
| N9 | **fixed (FIXA)**: `source.rights` on every claim + `licence_restricted` warning; UI reads the API field |
| N10 | FIXB (UCDP connector) |
| N11 | **fixed (FIXA)**: T2 kept; a Wikidata T2 claim alone is capped at C (`grade.C.t2_wikidata_single`); 75505 B → C |
| N12 | FIXB (IDMC connector) |

### N1 — Medium — every retraction mislabelled `removed_at_source`

**Evidence.**

* All 4,264 rows of `kg.causal_claim_retraction` have the reason `removed_at_source`: PA-X 3,334, UCDP PA 382,
  Archigos 327, MEC 90, NAVCO 1.3 62, NAVCO 1.2 37, ESRB 27, ICB 5. So do the 1,635 `archigos:coup` statement
  retractions.
* In none of these cases did the provider remove the row. They are REVA reclassifications (audit M5, M6, M7, L2,
  L3, and CAUSMORE's PA-X context reading).
* The PA-X file is byte-identical in both runs (`a11d6491…`), and the UCDP PA workbook is `9a566617…` in both.
  The enum already has `no_longer_causal`.

**Why it matters.** The append-only history is the audit trail, and it tells a future reader that the sources
withdrew 4,264 coded links. That is false.

**Proposed fix (loader).**

* `load_dataset_claims` and the KG statement loader should take a retraction reason from the delivery, e.g.
  `ClaimDelivery.retract_reason = "no_longer_causal"` with a detail naming the registry or connector version.
* Use `removed_at_source` only when the raw row is absent from a new provider revision.
* The existing rows cannot be edited. Add a documented correction (a note table or a release note) that maps these
  4,264 rows to their real reason.

### N2 — Medium — Wikidata hedges and scope qualifiers ignored by grade, wording and chains

**Evidence.**

* P1480 "sourcing circumstances" appears on 4 claims: 75168 (Q18122778 "presumably": Itaewon crowd crush has
  cause Q109905701, graded C), and 75438, 75454 and 75605 (Q30230067 "possibly": contributing factors in the
  Berta Cáceres and Justo Sorto murder cases, graded C, D and C).
* These claims have no fact rule, no cap, `attribution: null` and no wording, and they compose into chains
  (`/chain Q80011627→Q82955730`, grade C).
* Scope qualifiers that restrict the claim are stored verbatim since L4 but never worded: P518 "applies to part" on
  74748, 75279 and 75284 (e.g. 74748, COVID-19 → Q93629491, applies to Q7569), P1001 on 74111, P1011 on 74485,
  P1012 on 75026 and 75142, and P1013 on 74197.
* The labels of Q18122778 and Q30230067 are not in the archive. They were identified from their public Wikidata
  items.

**Why it matters.** This is the same failure as H3, for a different property. A hedged "possibly" claim is shown
as if Wikidata asserted it outright, and is graded higher than unhedged T4 claims.

**Proposed fix.**

* Treat P1480 as P5102: fact `nature.sourcing_circumstances`, cap at D, verbatim value in the wording, and no chain
  composition.
* Word P518, P1001, P1011, P1012 and P1013 as a scope clause: "… (applies to: …)".

### N3 — Medium — attributed parties and nature values shown as bare QIDs

**Evidence.**

* 10 of the 11 distinct P3680 parties have no KG label: Q7747 (6 claims), Q1140115 (4), Q855, Q130786, Q22686,
  Q4217811, Q1970048, Q1672300.
* None of the 7 P5102 values has a label: Q28962312 (8 claims), Q712144 (6), Q132555 (5), Q28962310 (3),
  Q110143752 (2), Q20820099, Q41719.
* As a result, 11 of 12 attributed claims and 26 of 26 nature-qualified claims are worded with bare ids, e.g.
  74253 above. Only Q212 resolves ("Ukraine", 74392).

**Why it matters.**

* H3's purpose, "show the reader exactly who asserts what", is not met for the most sensitive claims in the
  database: Putin, Stalin and Trump appear as "Q7747", "Q855" and "Q22686".
* The P5102 values (e.g. alleged vs often vs rarely) decide how the claim should read, and they are opaque.

**Proposed fix.**

* Harvest and archive the entity documents of every qualifier value on causal claims (about 18 items) before
  wording.
* Until then, word them as "an unlabelled Wikidata item (Q7747)", so a bare id never stands in for a name. Show
  P5102 values with their label, or "(label not loaded)".

### N4 — Medium — IDMC preventive evacuations and hazard risks worded as plain effects

**Evidence.**

* 1,178 claims have `scope.displacement_occurred = "Displacement reporting preventive evacuations"`, and 57 IDMC
  event names describe a *risk*. The union is 1,206 claims. Examples of risk names:
  * "Sri Lanka: Risk of Landslide - Central (Kandy/Ganga ihala korale)", 50644;
  * "Azerbaijan: Risk of Landslide - Baku";
  * "USA: Dam Failure risk due to Flooding";
  * "Switzerland: (risk) snow avalanche".
* `/why` words these as "<event> has effect Internal displacements …" in the lane "causes (unqualified)", grade B,
  with the evacuation flag only in the raw `scope`.
* IDMC's definition does include displacement "in anticipation of the negative impact of natural hazards", so the
  attribution is IDMC's. But the wording makes a *risk* look like an occurred disaster that displaced people.
* IDMC's own flags disagree in places: 26 of the risk-named rows say "without preventive evacuations reported".

**Proposed fix.**

* Word these claims "… has effect (preventive evacuation, in anticipation of the hazard, as reported by IDMC) …".
* Put the README A52 definition in the claim's codebook definitions.
* Consider a fact rule, e.g. `scope.anticipatory`, that is shown on the grade.

### N5 — Low — IDMC placeholder-date and aggregate events stored as day-precise events

**Evidence.**

* 1,103 claims rest on rows named "<Country>[: <Hazard>][ - <Region>] - 01/01/<year>", with a 1 January start date
  stored at day precision (P580 precision 11, qualifier `approximate: true`).
* Examples: "Philippines: Storm - 01/01/2008" (2,039,155 displacements), "China: Flood - 01/01/2009" (4 rows),
  "Brazil: Storm - Santa Catarina - 01/01/2024".
* 491 of the 1,114 such rows share their country, year and hazard with other, dated events.
* This is consistent with IDMC's README A43, which says events are named "based on the country, type of hazard,
  location, and event start date" when no name exists. The 1 January is very likely a placeholder, and several of
  these rows are plausibly country- or region-year aggregates. That is an inference from the pattern. The README
  does not say it.

**Impact.** No grade changes: 1,102 of these claims are overlapping and 1 is consistent. The reader is shown a
precise date that the source almost certainly does not code.

**Proposed fix.** For a 1 January start whose name carries the same 01/01 date, store the start at year precision,
with a qualifier saying it is a placeholder. Label the event "(IDMC event record; may aggregate several hazards)".

### N6 — Low — UCDP dyad episodes coded with two terminations: one kept silently

**Evidence.**

* 7 episodes have two `d_epterm = 1` rows: 64901, 70901, 72202, 83203, 83306, 88201 and 1179801.
* The connector keeps the last row. In 4 cases the dropped outcome differs from the kept one:

  | claim | episode | dropped | kept |
  |---|---|---|---|
  | 55360 | 64901 | 1967 victory side A | 1973 ceasefire |
  | 55474 | 72202 | 2014 peace agreement | 2016 ceasefire |
  | 55638 | 83306 | 2017 low activity | 2023 victory side A |
  | 55701 | 88201 | 2004 peace agreement | 2011 victory side B |

* The claim's selector names only the kept line. The conflict is in the ingest report only.

**Why it matters.** REVA resolves a source-internal disagreement silently, which contradicts "kept and shown, never
corrected".

**Proposed fix.** Emit one claim per terminal row, or one claim citing both lines with a `source_conflict` scope
note.

### N7 — Low — tautological temporal checks on coded outcomes

**Evidence.**

* 7,538 `coded_outcome` claims have an effect whose date equals the cause's own end date. The outcome is dated "at
  the latest coded end … (REVA summary)" for COW, and by the termination or end date for MID, UCDP, ICB, TIES and
  EUSANCT.
* 6,343 of them are `consistent`. That only says that the record's start precedes its end: it is not independent
  temporal evidence.
* `temporal.consistent` is one of the conditions for grade A.

**Proposed fix.** A fact such as `temporal.same_record` ("the effect is dated by the cause's own end date"), with
`consistent` not counted toward A for such claims. Alternatively, word the check as "not applicable: outcome of the
same record".

### N8 — Low — CAUSMORE claims lack `quote_source`; unverifiable counter-evidence

**Evidence.**

* The 23,915 claims of COW wars, UCDP dyad and IDMC carry no `codebook.quote_source`: no page and no full sha256.
  The codebook `version` names only a 12-hex prefix, unlike AUDFIX's convention.
* IDMC's "idmc_readme sha256 9c6228afa45e" is a hash of the normalised README cell texts, not of an archived
  object, so it cannot be looked up by sha. The quote does verify against the archived workbook.
* The COW v5.1 renumbering evidence cited for the v4.1 choice (§4.3) is not archived.

**Proposed fix.**

* Add `quote_source {sha256, pages | cell}` from the registry's `pdf_page` / `cell`.
* Name the IDMC basis as "README cell A24 of workbook `60dd7c1e…`".
* Archive the v5.1 file, or quote the codebook's "War# changed" list.

### N9 — Low — causal API carries no licence for non-commercial sources

**Evidence.** `/why`, `/effects` and `/chain` responses for IDMC (CC BY-NC, `open_licence: false`) contain no
licence, rights or `open_licence` field, in neither the claim nor the envelope (`provenance_links: []`). The UI adds
licences from a separate registry call. API-only consumers do not see them.

**Proposed fix (APIFIX).** Include `source.licence` / `source.open_licence` per claim, or a `restrictive_licence`
warning listing the restricted sources in the response.

### N10 — Low — UCDP "year assigned" and "year missing" precision stored as an ordinary year

**Evidence.**

* `d_ependprec` / `c_ependprec` 6 ("Year is assigned. There is a wide disagreement … subjective judgment") and
  7 ("Year is missing") are stored at year precision with no uncertainty mark.
* This affects 9 claims (55105, 55478, 55513, 55604, 55736, 55819, 55830, 55891, 7136), 4 of which are
  `consistent`.

**Proposed fix.**

* Precision 6: widen to the decade, or mark the date as `assigned`.
* Precision 7: treat as typed missing, which gives `undetermined`.

### N11 — Low — a Wikidata statement with a DOI graded like an expert dataset

**Evidence.**

* 75505 "Cretaceous–Paleogene mass extinction has cause Q5096573" is T2 and graded B (`grade.B.expert`), the same
  as T1 datasets. The reason is a reference giving DOI 10.1126/science.adk4868 ("Ruthenium isotopes show the
  Chicxulub impactor was a carbonaceous-type asteroid").
* Nothing checks that the work asserts the causal link.

**Proposed fix.** Grade T2 statements B only with a quotation (P1683) or a page reference, and otherwise as T3.
Alternatively, word them "a scholarly work is cited; REVA has not checked that it asserts this link".

### N12 — Low — event identities that REVA made for IDMC rows without an Event ID

**Evidence.**

* 2,695 claims start from `idmc:event:r<hash>`, an event identity that REVA built from the row's fields. The
  entity description says only "Disaster event (Storm) in the IDMC Global Internal Displacement Database".
* 188 names recur on several such rows, e.g. "Typhoon morakot (kiko)" × 3 and "Cyclone 'aila'" × 2. These are
  plausibly one event split into several, which is the honest default (no name matching), but the label does not
  say so.
* The 11 collapsed identical rows are invisible in the KG.

**Proposed fix.**

* Description: "IDMC row without an Event ID (identity = REVA row key; not linked to other rows of the same name)".
* Record collapsed duplicates as a qualifier on the kept row.

## 7. Structure checks (new defects introduced?)

| check | result |
|---|---|
| self-loops | 1 (74132, flagged) |
| cycles | 7 SCCs, 27 edges, all flagged; no new cycle through same_as clusters; coded outcomes excluded by rule |
| duplicates (cause, effect, relation, source, dataset) | 50 groups, all Wikidata inverse pairs (P1478/P1536 39, P1479/P1537 11) grouped for corroboration; 0 dataset duplicates; current `claim_key` unique |
| grade rows | 0 current claims ungraded; all current grades are version 2 (`70a73066…`); 28,467 version-1 rows kept (loads 7–23, sha `6e21ac14…`); 0 grade rows reference a missing claim or carry a wrong `claim_key` |
| version-1 rows edited | 0: `n_tup_upd = n_tup_del = 0` for `causal_claim`, `causal_grade`, `causal_claim_retraction`, `causal_load`, `statement`, `statement_retraction`; the immutability triggers are enabled |
| claims pointing to missing entities | 846 (all Wikidata; 659 entities), see M2 |
| candidates | `kg.causal_candidate` empty, `kg.causal_review` empty |
| retracted claims that are current | 0; retractions of a non-latest version 0; reinstatements 0 |

## 8. Limitations

* **Labels from memory.** The labels of the qualifier values Q18122778, Q30230067, Q7747, Q855 and Q22686 come from
  their public Wikidata items. They are not in the archive (no network fetch was made). N3 is about exactly this
  gap.
* **N5 is an inference.** The placeholder and aggregate reading of IDMC's 01/01 events rests on the naming pattern
  and on README A43. IDMC does not state it.
* **Wording checked per source, not per claim.** The API wording was checked on the claims listed in §5. The other
  sampled claims were checked through the stored fields that the wording is built from (relation, attribution,
  scope, grade facts).
* **TIES row parser.** The TIES `.xls` rows were re-read with the repository's BIFF reader, as in audit 1. Every
  other parser was independent.
* **NLQA not audited.** APIFIX was editing it during this audit.
* **Scratch scripts.** All scripts and exports are in the session scratchpad (`audit2/`), outside the repository.
