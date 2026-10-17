# V6 adversarial honesty audit (agent AUDIT4, 2026-10-17)

Scope: the V6 layers loaded live in the dev database `reva` (origin `live`), read-only. These are the UCDP GED
events and aggregates (V6CONFLICT), the 225 EFFECTS-MORE effect estimates, the FIX4B rewrites, the EIA STEO archive
and energy back-test (PRED-ENERGY), the PRED-MODELS holdout protocol and registrations, the forecast-registry seals and
voids, and the five reference decisions of the decision engine. Two spot checks were added: FIX4A's claim counts and
the politics/signals connectors. Reports read: `v6_conflict`, `v6_politics`, `v6_signals`, `v6_pred_energy`,
`v6_pred_models`, `v6_effects_more`, `v6_fix4b`, `v6_diskz`, `v6_integ`, and after a mid-audit pull `v6_fix4a`.

The method follows `review_v4_v5_audit.md`. Every check is an independent re-implementation that reads the archived
raw bytes through the store decoder (`reva.adapters.objects.compression.decode_bytes`) and re-hashes them. Raw CSVs
were parsed with polars, Natural Earth shapes with pyshp and shapely, xlsx files with a zip+XML reader of my own (not
`atlas_xlsx`), and PDFs with `pdftotext -layout`. The loaders under audit were not called.

Two exceptions are deliberate behaviour checks:
* the PRED-MODELS ledger guard (`check_holdout_unopened`), called in memory;
* `python -m reva.predict.run --reference all --draws 2000 --seed 0 --out <scratchpad>`. `src/reva/predict` holds no
  INSERT, UPDATE or DELETE statement.

Nothing was written to the database, the registry or the object store. This file is the only output. Snapshot window:
21:05–21:35 UTC. All queries were filtered by source key or dataset; there were no whole-table KG aggregates.
Scripts are in the AUDIT4 scratchpad (`audit4/`) and are not committed.

## 1. Summary

**Mechanical fidelity is clean: 0 transcription or arithmetic errors in 545 checked items.** These are 100 GED events, 55 aggregates, 30 KG statistics, 227 effect estimates, 40 STEO cells, 20 sealed STEO points, 26 engine hops, 30 holdout hypotheses and 17 energy winners, on top of 99 ADM1 recomputations, 54 vintages and 887 registry questions. In addition:
* 69 cited raw or derived objects were decoded and re-hash to their names (1 GED zip, 1 Natural Earth zip, 19
  publication PDFs, 31 STEO workbooks, 9 atlas Parquet tables, 8 registry manifests).
* All 8 registry seals and the FIX4B void resolution verify.
* All 225 EFFECTS-MORE quotes are found on their declared PDF page (census). A negative control (the same quotes
  against page + 3) found 0 of 15.
* 99 GED ADM1 placements recomputed independently are identical to REVA's.
* PRED-MODELS' Holm over 30 hypotheses reproduces 6 rejections and 3 confirmations.
* PRED-ENERGY's 17 winners reproduce exactly.

**The defects are semantic: 0 High, 2 Medium, 6 Low.**
* **M1:** in about 2,770 GED events, the 1:10m border generalisation puts events in a neighbouring state's ADM1
  cells and KG edges. Examples: Kobane (Syria) in Şanlıurfa (TR-63), Goma and Bukavu (DR Congo) in Rwanda,
  "Egyptian side of Rafah border" in Gaza.
* **M2:** 10,846 Gaza / West Bank events (69,843 best deaths) are counted under `iso3 = ISR` in `events` and
  `country_month`, with no row-level flag.
* Low: left-censored "first event date" for conflicts active before 1989; the Natural Earth artifact still carries
  NOAA's licence (AUDIT3 L2, now wider); an outcome-vintage mismatch between the PRED-MODELS holdout and the registry
  questions; Holm family size and scope misstated in the energy report; a FIX4B retraction reason that does not say
  why; report drift.

## 2. Method

### 2.1 Sample (seed `AUDIT4-2026101704`, recorded)

Python `random.Random(seed + suffix)` was run over sorted keys (suffixes `-agg`, `-eff`, `-steo`). Effects, seals,
holdout hypotheses, energy winners and engine hops were audited as a census.

| layer | stratum | n | check against archived bytes |
|---|---|---:|---|
| GED events | 25 per `type_of_violence` (sb / ns / os) + 25 events placed in `PS-*` | 100 | CSV row of `GEDEvent_v26_1.csv` (zip `8c941d84…`). Checked: best, low, high, deaths_a, civilians, tov, conflict id, lat/lon, where_prec, priogrid, `[date_start, date_end + 1 d)`, `gw:<country_id>`, selector id |
| GED ADM1 | the placeable events of the sample (where_prec 1–4) | 99 | **point-in-polygon recomputed** (pyshp + shapely `covered_by`, Natural Earth zip `efc59726…`). Usable code = ISO 3166-2 pattern and unique in NE; status (placed / ambiguous / outside / where_prec) compared |
| GED aggregates | `country_month` 20, `adm1_month` 20, `priogrid_month` 15 | 55 | recomputed from the raw CSV: month of `date_start`, tov filter, count or sum of best/low/high/civilians; half-open month period |
| GED KG | `ucdpged:fatalities` / `events` / `events_in_adm1` / `first_event_date` | 30 | conflict-year sums of best (bounds = sums of low/high), event counts, ADM1 count and deaths, min `date_start` |
| effects | census of EFFECTS-MORE loads 21–38 + FIX4B load 20 | 227 | quote token sequence on the declared `pdf_page`; `as_printed` in the quote (also per IRF point); point = printed or negated (`printed_transform`); sign; CI order; `shock_profile`; `rights.restricted`; secondary label vs locator row (44 rows) |
| FIX4B | effect revisions and retractions; void resolution | 7 + 2 + 1 | old rows still present, new revisions current, 0/301 level–growth violations (FIX4B's SQL), void artifact `39b404b0…` |
| STEO archive | all 54 vintages; 40 cells | 94 | availability rule (release + 1 day; Last-Modified ≤ release day); no non-projection cell at or after the issue month. Each cell: code in column A of the selector row, value at the selector column (0-based) + 1, year and month headers above the column |
| energy back-test | 17 winners, 7 series families | 17 | Holm recomputed from `v6_pred_energy_backtest.json` tests (CRPS vs RW, STEO reference excluded) |
| registry | 8 batch manifests (887 questions), 20 attributed STEO points, 19 supplement questions | 934 | seal = sha256 of the decoded manifest. Checked: question period not ended at issue; reference period > `last_observed`; pinned tables recorded before issue; STEO point = projection cell of the 2026-10 vintage; supplement questions byte-identical to the original batch's |
| PRED-MODELS | protocol, ledger, 30 hypotheses | 30 | canonical sha256 of `protocol.yaml` = ledger `3116db10…`; commit order; guard refuses re-opening; Holm over 30 + coverage 70–90 % recomputed |
| engine | 5 reference decisions | 26 hops | estimate key current; coefficient = point / shock size (unit-converted); unresolved ties carry no number; `geo: global` variables only at WLD; no averaging text; secondary / model-simulation provenance |

Population checks (not sampled):
* GED `events.value == best`: 417,968 of 417,968.
* GED ADM1 status distribution and country-pair breakdown of the 14,674 `placed:ged_country_differs` events.
* Candidate tables are 100 % `candidate: true` (13,659 + 5,895).
* KG fatalities bounds bracket the amount: 3,229 of 3,229.
* `shock_profile` of all 225 new estimates; evidence labels of 301 current estimates.
* Look-ahead on every question of 6 issue batches.
* FIX4A: ungraded live claims, `geno:` causes, and bare-filler RO:0004021 claims among 61,680 current claims.

<details><summary>Sampled ids</summary>

GED event ids, effect keys and STEO cell indexes are in the AUDIT4 scratchpad (`ged_sample_ids.json`,
`eff_census.json`). The seed above regenerates them deterministically from the same tables (events `ecfc4d62…`,
STEO archive `3ac6e561…`).
KG statement ids drawn with `ORDER BY md5(id || 'AUDIT4-2026101704') LIMIT 30` on `source_key = 'ucdp.ged'` and the
four GED-derived predicates.

</details>

### 2.2 Notes

* `meta.artifact` holds the NE zip, the GED zip, the STEO workbooks and the PDFs. All were read through the decoder;
  several are DISKZ-wrapped (`\x89RVZ`), and none was missing or corrupt.
* The `kg.statement` aggregate that spilled 2.2 GB of temp files (lead's note) was not one of mine. All AUDIT4 KG
  queries were filtered by `source_key` first.

## 3. Error rates per layer

| layer | items checked | fidelity errors | honesty defects (sample → population) |
|---|---:|---:|---|
| GED events (V6CONFLICT) | 100 (+ 417,968 population) | **0** | 25/25 `PS-*` events carry `iso3 = ISR` (M2: 10,846 events). Cross-border placement M1: about 2,767 / 376,082 placed events (0.74 %) |
| GED ADM1 placement | 99 recomputed | **0** (identical code and status) | geometry rule reproduced exactly. The rule itself mis-assigns border events (M1) |
| GED aggregates | 55 | **0** | ISR `country_month` cells carry no flag for Palestinian-territory events (M2) |
| GED KG | 30 (+ 3,229 bounds) | **0** | 79/1,581 `first_event_date` values fall in Jan–Mar 1989, the dataset start (L1). 595 `events_in_adm1` edges count GED-country-differs events, some across a border by generalisation (M1) |
| effect estimates (EFFECTS-MORE + FIX4B) | 227 (census) | **0** (227/227 quotes on page) | 44 secondary rows correctly labelled (survey-table rows of other authors; own rows primary). 12 new rows `shock_profile = unspecified` (honest, but the report says "every estimate states"; L6) |
| FIX4B rewrites | 7 revisions, 2 retractions, 5 voids | **0** | append-only verified (old rows 1–5, 47–49 kept); retraction detail gives no reason text (L5) |
| STEO archive (PRED-ENERGY) | 54 vintages + 40 cells | **0** | 0 vintages available before release; 0 Last-Modified after release; 0 non-projection cells at or after the issue month |
| energy back-test | 17 winners | **0** (17/17 reproduced) | report states Holm families of "60–72 tests"; actual 48–60 (10 for COPR_WORLD), STEO excluded; per-series families only (L3) |
| registry | 8 seals, 887 questions, 20 STEO points, 19 supplement questions | **0** (8/8 seals; 0 look-ahead; 20/20 points) | holdout outcome ≠ registry resolution vintage for the registered WEO densities (L4) |
| PRED-MODELS holdout | 30 hypotheses | **0** | protocol frozen in `53bd6da` (20:15:36) before the opening (20:20:35); ledger hash matches; guard refuses the same protocol and a tweaked protocol with the same hypotheses |
| decision engine | 5 decisions, 26 estimate hops | **0** numbers without an attributed estimate; 0 averages | 5 `unresolved_tie` gaps carry no number; 4/4 global variables only at WLD; Caldara hop −5 %/0.8 mb/d → −0.00625 %/kb/d × −2,000 = +12.5 % |

FIX4A spot check, confirmed in the DB:
* 0 ungraded among 61,680 current live claims;
* 0 `geno:` causes;
* 0 RO:0004021 claims with a bare filler;
* ClinGen negative curations still under `associated_with`: 296. This matches the "queued" status in `v6_fix4a.md`,
  so AUDIT3 H2 remains open.

Politics and signals: nothing was loaded live (0 datasets for gdelt, nelda, oxcgrt, whogov or the derived change
tables), which matches the reports. In code, country identity is ParlGov `country_name_short` (ISO3), WhoGov
`country_isocode`, NELDA GW codes through the countrycode crosswalk, and GDELT FIPS through the GeoNames FIPS
column. Unmapped codes are kept without iso3. No name matching was found.

## 4. Defects (ranked)

### Medium

**M1. Border generalisation places GED events in a neighbouring state's ADM1 (about 2,767 events, 595 KG edges).**

Geometry decides, and Natural Earth 1:10m polygons are generalised, so a point a few hundred metres inside one state
is "covered by" the neighbour's polygon. The 14,674 `placed:ged_country_differs` events break down as follows:
* Territorial cases (expected): ISR→PS 10,846, SDN→SS 771 (pre-2011), IDN→TL 290 (pre-2002).
* Everything else, about 2,767 events across 113 country pairs, for example:

| GED country → polygon | events | best deaths | typical places |
|---|---:|---:|---|
| SYR→TR | 577 | 4,104 | Kobane, Ayn al-Arab, Kasab |
| CAF→CD | 215 | 2,194 | |
| COD→RW | 202 | 4,844 | Goma, Bukavu |
| YEM→SA | 117 | 218 | |
| UKR→RU | 111 | 172 | Sumy and Kharkiv oblast villages |
| EGY→PS | 105 | 436 | "Egyptian side of Rafah border", Rafah town, North Sinai |

These events are summed into `adm1_month` cells of the wrong country (`flags.country_iso3 = TR`, …). The cells only
carry a count `events_ged_country_differs`. They also become KG `ucdpged:events_in_adm1` edges, family `located_in`,
for example:
* `ucdp:conflict:13648` "Egypt: Islamic State" → `iso3166_2:PS-GZZ` (94 events);
* `ucdp:nonstate:13744` "Euphrates Vulcano – IS" → `iso3166_2:TR-63` Şanlıurfa (174);
* `ucdp:nonstate:13022` "IS – PYD" → TR-63 (144);
* `ucdp:onesided:16101` → RW-04 (40).

So a reader is told that the Islamic State–Egypt conflict is located in Gaza and that Syrian non-state conflicts are
located in Turkey. GED names were rightly never read. The fix must be by geometry or identifiers.

*Fix (V6CONFLICT; owner decision 2 of `v6_conflict.md`):* when the GED country is a current state and differs from
the polygon's country, do not place the event (status `not_placed:border_generalisation`) unless the pair is on a
reviewed territory list (ISR→PS, SDN→SS before 2011-07-09, IDN→TL before 2002-05-20, …). Alternatively, place it in
the nearest polygon of the GED country within a stated tolerance. Then re-ingest: the KG edges are retracted by
delivery.

**M2. Gaza / West Bank events counted as Israel in the country tables, without a row flag.**

In `atlas.ucdp_ged.events` and `country_month`, events whose GED country is Israel (`gw:666`) get `iso3 = ISR`
through the GW crosswalk. This covers 10,846 events and 69,843 best deaths, located by REVA's own placement in
`PS-GZZ` / `PS-*`; the 2023–2025 GED totals are 26,800 / 21,600 / 14,456.

The 4,590 ISR `country_month` cells have no flag. The catalog concepts of `conflict_events.yaml` (country deaths by
ISO3) therefore show deaths in Gaza as deaths in Israel. Only `adm1_month` (`country_iso3 = PSE`, count of differing
events) and the report say so. This is GED's coding, kept verbatim, but the ISO3 assignment is REVA's.

*Fix:* count REVA-placed events whose polygon country differs from the GED country in each `country_month` cell
(`flags.events_placed_in_other_territory`, with the code list). Add a catalog note on the ISR concepts. Owner decision
2 should cover whether to also publish the PSE share.

### Low

* **L1. Left-censored `ucdpged:first_event_date`.** 79 of 1,581 conflicts have their first GED event in January–March
  1989, the start of GED (e.g. `ucdp:conflict:308` 1989-01-31). The qualifier says "min date_start" of GED 26.1, but
  the predicate reads as a conflict start. *Fix:* qualifier `left_censored: true` when the date falls in GED's first
  month or quarter, or rename the predicate `first_ged_event_date`.
* **L2. Natural Earth licence still NOAA's (AUDIT3 L2, wider).** `meta.artifact.rights` of `efc59726…` still says "US
  Government work (NOAA NCEI) … cite GHCN-M v4". The artifact is now also the geometry of 7,012 `ucdpged:events_in_adm1`
  statements (NE sha in `qualifiers.natural_earth_sha256`; `refs` cite only the GED zip) and of `adm1_month` and
  `cand_adm1_month`. *Fix:* as in AUDIT3 L2, plus add the NE artifact to the statements' `refs` with "Public domain
  (Natural Earth)".
* **L3. Energy back-test: Holm family misstated, and per-series only.** `v6_pred_energy.md` §3 says "Holm over all
  (candidate, horizon) tests of the series (60–72 tests)". The code excludes the STEO reference, giving 48 (prices),
  60 (quantities) and 10 (COPR_WORLD, testable only). Each of the 7 series is its own family, so registering winners
  across 7 families has no cross-series control. The STEO-vs-RW p-values in the §4 table (e.g. Henry Hub 0.015) are
  unadjusted one-sided p-values, shown in bold as "significant". The 17 winners themselves reproduce. *Fix:* correct
  the text; state per-series familywise control, or run Holm over all series (the 17 winners would need re-checking);
  label the STEO p-values "unadjusted".
* **L4. PRED-MODELS registered WEO densities are calibrated on a different outcome vintage than the one they will be
  scored on.** The holdout outcome is the first WEO vintage on or after 1 October of the following year
  (`realtime.py`, "same rule as `weo_backtest`"). The registry questions of `weo.gdp_growth` resolve on the "first WEO
  vintage in which the year is an actual" (expected 2027-05-01; supplement batch `a8b7a89d…`). The intervals were
  validated against October-of-next-year values and will be scored against April values. *Fix:* state it in
  `registered_models.yaml` and the scope, or align the family's resolution rule. Treat prospective coverage as the
  real test (already required by the judge for chains).
* **L5. FIX4B retractions do not say why.** The two Gutmann rows (`…:gdp_pc_growth_year1`, `…_year2`) are retracted
  `withdrawn_by_curator` with detail `{"basis": "absent_from_complete_delivery"}`. Nothing in the row points to the
  horizon convention (AUDIT3 M5a) or to the replacing profile `…:gdp_pc_growth_years_1_2`. *Fix:* the curation entry
  should carry `retired: [{key, reason, replaced_by}]`, which the loader copies into the retraction detail.
* **L6. Report drift.**
  * `v6_effects_more.md`: "302 current published + REVA-run estimates"; the DB holds 301 (296 + 5).
  * "Every estimate states `shock_profile`": 12 new rows (Ramey survey multipliers) and 8 V5 rows (Ortega–Osbat)
    are `unspecified`. That is honest, but not "stated".
  * `v6_conflict.md` §3: "placed 360,986" counts only status `placed`; all placed events are 376,082.
  * `v6_pred_energy.md`: the family sizes (L3).

## 5. Name-based mappings (inventory, V6)

None found in the audited V6 code paths:
* GED: GW code → ISO3 via countrycode; ADM1 by polygon ISO 3166-2; GED `adm_1` names not loaded.
* Effects → engine: by `cause_id` / `effect_id` and curated identifier links.
* STEO: by EIA series code; `provider_geo = not_stated`.
* Registry and PRED-MODELS: by dataset key and ISO3.
* ParlGov / WhoGov / NELDA / GDELT: identifiers (§3).

The curator-assigned ISO3 of EFFECTS-MORE `scope.countries` (from printed country names, e.g. van Els et al. table
columns) is the same documented curator assignment as AUDIT3 §5 row 3.

## 6. Checks that passed (worth keeping)

* Provenance: 69/69 objects decode and re-hash; every sampled GED row and STEO cell names its raw sha256 and a
  selector that resolves (STEO selector: 1-based row, 0-based column, consistently).
* GED: 417,968 / 417,968 `value = best`; 100/100 events, 55/55 aggregates and 30/30 KG statistics re-derived; 99/99
  ADM1 placements identical; Candidate data never mixed with final (100 % flagged, separate tables); zero never
  written for an absent country-month.
* Effects: 227/227 quotes on the declared page (negative control 0/15); 0 sign, CI, transform or rights violations;
  secondary labels match the table rows (44); model simulations are named as such in the engine hops.
* FIX4B: append-only (no row edited); 0/301 level–growth violations; 5 sealed questions void by an appended
  resolution naming each one, reason `fiscal_year_basis_mismatch`; batch `96c6ad48…` still verifies.
* STEO archive: 54/54 vintages available only from the day after release, with Last-Modified on or before the
  release day; 0 history cells at or after the issue month; the back-test information set excludes projections
  (`VintageAccessor.latest`) and RW / regressions use only `t + k ≤ last`.
* Registry: 8/8 seals; 0/887 questions ended or already observed at issue; pins recorded before issue; 20/20
  attributed STEO points equal the 2026-10 projection cells; supplement batch `a8b7a89d…` re-uses the 19 question
  records byte-for-byte and the same pins.
* PRED-MODELS: protocol frozen before the opening; the ledger refuses a second opening (same protocol, or the same
  hypotheses under the same model version); Holm over 30 → 6 rejections, 3 calibrated confirmations, exactly as
  reported; the SPF density issues nothing (no family).
* Engine: no number without an estimate key; alternatives and ties listed "jamais additionnées ni moyennées"; world
  variables (oil price, world production / consumption / IP) only at WLD; every result labelled « scénario non validé
  — pas une probabilité ».

## 7. Proposed order of fixes

1. M1 + M2 (V6CONFLICT, owner decision 2): border rule, country-cell flag, re-ingest `atlas.ucdp_ged`.
2. L4 (PRED-MODELS / PRED-REGISTRY): state or align the outcome vintage before the first WEO h0 resolutions
   (≈ April 2027).
3. L3, L6 (report text), L1, L2, L5 as convenient. AUDIT3 H2 (ClinGen) is still waiting for the queued `atlas.clingen`
   ingest.

## 8. Fix status

| ID | Status | By | Where |
|---|---|---|---|
| M1 | fixed in code; live re-ingest queued (disk < 5 GB) | FIX6 | `atlas_ucdp_ged.py` 1.0.1 border rule + `registry/conflict/territory_exceptions.yaml` (owner review); offline: 2,651 events unplaced, 215 KG edges to retract (`reclassified_by_reva`), 141 recounted; `v6_fix6.md` §1 |
| M2 | fixed in code; re-ingest queued | FIX6 | country-month `events_placed_in_other_territory` / deaths / `other_territory_iso3` (329 ISR cells), catalog note on ISR concepts; `v6_fix6.md` §2 |
| L1 | fixed in code; re-ingest queued | FIX6 | `left_censored: true` on 79 `ucdpged:first_event_date`; `v6_fix6.md` §3 |
| L2 | fixed (read side live; refs on re-ingest) | FIX6 (with FIX4C) | families API `rights.inputs` = Natural Earth public domain for the 7,012 statements; `extra_refs` + adm1 `geometry_licence`; `v6_fix6.md` §4 |
| L3 | fixed | FIX6 (text), FIX7 (one Holm family of 260) | `v6_pred_energy.md` §3/§4; `v6_fix6.md` §5 |
| L4 | flagged (holdout not re-opened); owner decision | FIX6 | `registered_models.yaml` `outcome_vintage_mismatch` (MAD 0.27–1.29 pp April vs October), `v6_pred_models.md` §6; `v6_fix6.md` §6 |
| L5 | fixed, live | FIX6 | curation `retired:` → retraction detail; existing retractions 1–2 annotated (artifact `24926bfc…`, activity `e41da24a…`); `v6_fix6.md` §7 |
| L6 | fixed | FIX6 | `v6_effects_more.md` (301; 20 `unspecified`), `v6_conflict.md` (376,082 placed), `v6_pred_energy.md`; `v6_fix6.md` §8 |
