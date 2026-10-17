# V6 FIX6 — AUDIT4 fixes (M1, M2, L1–L6) and two PRED-CONFLICT requests

Agent FIX6, 2026-10-17. Input: `docs/dev/review_v6_audit.md` (AUDIT4) §4, plus a lead note for PRED-CONFLICT (GW codes
260 / 345 without ISO3; a resolution rule for country-month cells that are absent because nothing happened).
Disk was 4.4–4.8 GB free during the whole run (< 5 GB), so **no live re-ingest was run**: the code, registry and
tests are done, the impact was recomputed offline from the archived raw bytes (read only), and the re-ingest is queued
(§9). Two small DB writes were made under `flock` (L5 annotation, below).

| ID | Fix | Where | State |
|---|---|---|---|
| M1 | Border rule: a polygon of another country than GED's own places the event only for a reviewed territory exception; else `not_placed:border_generalisation` | `atlas_ucdp_ged.py` 1.0.1, `registry/conflict/territory_exceptions.yaml` | code + tests; re-ingest queued |
| M2 | Country-month cells count their events placed in another territory (`events_placed_in_other_territory`, deaths, `other_territory_iso3`); catalog note on the ISR concepts; GED's coding unchanged | same; `registry/atlas/catalog/conflict_events.yaml` | code + tests; re-ingest queued |
| L1 | `ucdpged:first_event_date` before 1989-04-01 gets `left_censored: true` + note | `atlas_ucdp_ged.py` | 79 statements on re-ingest |
| L2 | GED ADM1 statements cite the Natural Earth zip as a second reference ("Public domain (Natural Earth)"); families API serves it as `rights.inputs` (also for the 7,012 existing statements, from their `natural_earth_sha256` qualifier); adm1 tables record `geometry_licence` | `atlas_geopol_common.KgStatement.extra_refs`, `kg/family_query.py` | read side live now |
| L3 | Energy report: real family sizes (48 / 60 / 10, STEO excluded), STEO-vs-RW p-values labelled unadjusted; the family construction itself was changed by FIX7 (one Holm family of 260, QA-PRED X3) | `docs/dev/v6_pred_energy.md` | done |
| L4 | Outcome-vintage mismatch measured and flagged on both registered WEO densities and in the PRED-MODELS report | `registered_models.yaml` (`outcome_vintage_mismatch`), `v6_pred_models.md` §6 | done (flag; holdout not re-opened) |
| L5 | Curation `retired:` records (reason, `replaced_by`) copied by the loader into the retraction detail; the two existing Gutmann retractions annotated (append-only artifact + activity) | `effects/curation.py`, `effects/model.py`, `effects/retired.py`, `atlas_effects_lit.py`, Gutmann YAML | done, live |
| L6 | Report drift corrected | `v6_effects_more.md`, `v6_conflict.md`, `v6_pred_energy.md` | done |
| lead (1) | GW 260 → DEU from 1990-10-03 (GW policy window); 345 → SRB from 2006-06-05 already true | `atlas_geopol_common.py` | code + test; `atlas.ucdp` re-ingest queued |
| lead (2) | Registry `absence_rule: zero_within_coverage` for event-census tables | `evaluation/registry/absence.py`, `questions.py`, `service.py` | code + tests |

## 1. M1 — GED ADM1 border rule

`place_adm1` now returns a `Placement` (codes, status, the covering polygon of rejected events, the exception label).
An event with `where_prec` 1–4 covered by exactly one usable Natural Earth polygon is:
* `placed` when the polygon's ISO 3166-1 prefix is GED's own country (GW code → ISO3 at the event date);
* `placed:territory_exception` when the (GED ISO3, polygon ISO3, date) matches the reviewed list;
* `placed:ged_country_historical_or_unmapped` when GED's state has no ISO entity (USSR, Yugoslavia, …) — unchanged;
* otherwise `not_placed:border_generalisation`, with `flags.adm1_polygon` kept on the event row for audit.

Matching is by ISO3 codes and dates only (no names). Territory list (`registry/conflict/territory_exceptions.yaml`,
`review: pending_owner`, curator decision for owner decision 2 of `v6_conflict.md`): **ISR→PSE** (no end: GED codes
Gaza / West Bank under Israel, country_id 666 — a coding fact, not a sovereignty statement), **SDN→SSD** before
2011-07-09, **IDN→TLS** before 2002-05-20, **ETH→ERI** before 1993-05-24, **ZAF→NAM** before 1990-03-21 (independence
dates of the GW state list). Each was checked against the data: all 771 SDN→SSD and 290 IDN→TLS events predate
independence; 65 of 88 ETH→ERI events predate 1993-05-24 (the 23 later ones, Badme war era, are border cases and now
not placed); all 51 ZAF→NAM events are 1989.

Recomputed offline on the archived GED 26.1 zip (`8c941d84…`), Natural Earth (`efc59726…`) and countrycode
(`d560b1c2…`), connector 1.0.1:

| status | 1.0.0 (loaded) | 1.0.1 |
|---|---:|---:|
| placed | 360,986 | 360,989 |
| placed:ged_country_differs | 14,674 | (retired) |
| placed:territory_exception | — | 12,023 (ISR→PSE 10,846, SDN→SSD 771, IDN→TLS 290, ETH→ERI 65, ZAF→NAM 51) |
| placed:ged_country_historical_or_unmapped | 422 | 419 |
| not_placed:border_generalisation | — | **2,651** (SYR→TUR 577, CAF→COD 215, COD→RWA 202, YEM→SAU 117, UKR→RUS 111, NGA→CMR 109, …; EGY→PSE 105) |
| all placed | 376,082 | 373,431 |
| `adm1_month` rows | 698,205 | 688,050 |

(+3 placed / −3 historical: three Germany events coded GW 260 after 1990 now resolve to DEU, lead fix (1).)

**KG (`ucdpged:events_in_adm1`, family `located_in`).** Of the 7,012 current edges (595 with a country-differs count),
on re-ingest **215 are retracted** — every one of the 595 made only of border-generalisation events — by the new
`retract_absent_ged_statements` (complete final-GED delivery only): the statements cite the GED zip of the delivery,
so the loader derives `reclassified_by_reva` (basis `raw_unchanged`). 141 edges keep their key with new event counts;
6,797 remain, 238 with a differs count (138 territory exceptions, `events_territory_exception`). The AUDIT4 examples:
`ucdp:conflict:13648` "Egypt: Islamic State" → PS-GZZ (94 events) **retracted**; `ucdp:nonstate:13744` → TR-63 (174)
**retracted**; `ucdp:nonstate:13022` → TR-63 (144) **retracted**; `ucdp:onesided:16101` → RW-04 (40) **retracted**;
`ucdp:conflict:234` "Israel: Palestine" → PS-GZZ keeps 9,088 of 9,089 events (all territory exceptions). The KG
revision becomes `ucdp-26.1+v6conflict.2`; all retractions are append-only rows, nothing is edited.

## 2. M2 — Israel country cells

`country_month` (and `cand_country_month`) values are unchanged (GED's coding kept verbatim). Every cell that contains
events placed under a territory exception now carries `events_placed_in_other_territory`,
`deaths_best_placed_in_other_territory`, `other_territory_iso3` (e.g. `["PSE"]`) and `other_territory_note` (REVA-placed
events only, `where_prec` 1–4: a lower bound). Offline: 607 flagged cells per tov=all indicator, 329 of them Israel's;
12,023 events / 151,936 best deaths flagged in all. Event rows carry `flags.territory_exception` ("ISR->PSE").
The five catalog concepts of `conflict_events.yaml` carry a note on the ISR rows (and SDN/IDN/ETH/ZAF windows). Whether
to also publish a PSE share stays owner decision 2.

## 3. L1 — left-censored first event

`ucdpged:first_event_date` gets `left_censored: true` and the note "UCDP GED coverage starts 1989-01-01 … the date is
the earliest GED event, not the conflict's start" when the first event is before 1989-04-01 (GED's first quarter):
**79 of 1,581** conflicts, matching AUDIT4. The predicate name and labels were not changed (registered properties).

## 4. L2 — Natural Earth rights on UCDP statements (with FIX4C)

FIX4C's rule serves a statement's own reference licence when its raw artifact was recorded for another source. The
GED statements' raw artifact is the GED zip (recorded for `ucdp.ged`, CC BY 4.0), so their rights correctly stay
CC BY 4.0; the polygons are a second input. Now:
* `KgStatement.extra_refs` (neutral record) → appended to the KG references; GED `events_in_adm1` statements cite
  `{raw_sha256: efc59726…, role: adm1_polygons, licence: "Public domain (Natural Earth)", attribution: "Made with
  Natural Earth"}` (on re-ingest).
* Families API `rights.inputs`: further references to another raw artifact with a licence; for statements loaded
  before (the 7,012 current ones) it is derived from their `qualifiers.natural_earth_sha256` (basis stated). **Live
  now, no reload needed**: the 7,012 statements show CC BY 4.0 (UCDP) + input "Public domain (Natural Earth)".
* `adm1_month` / `cand_adm1_month` table rights record `geometry_licence` (on re-ingest).
`meta.artifact.rights` of `efc59726…` still says NOAA (content-addressed, append-only; FIX4C §L2).

## 5. L3 — energy back-test text

`v6_pred_energy.md` §3: the V6 per-series families were 48 tests (each price series), 60 (each quantity series) and 10
(`COPR_WORLD`), STEO excluded — not "60–72". The family construction itself was replaced by FIX7 (one Holm family of
260 tests over the run; 4 winners instead of 17), so no code change here. The STEO-vs-RW column and summary now say the
p-values are **unadjusted** one-sided DM-HLN p-values.

## 6. L4 — WEO densities: validation vintage ≠ resolution vintage

Measured on the archived WEO vintages (`load_vintage_cells`, real GDP growth, non-projection cells): for target years
2015–2024 the October-of-next-year vintage (validation rule, `realtime.weo_cases`) and the first vintage in which the
year is an actual (April of next year, the `weo.gdp_growth` resolution rule) differ by > 0.05 pp for 71–138 of
142–193 countries per year; mean absolute difference 0.27–1.29 pp (2021: 1.29), p90 0.67–2.0 pp. Re-validating
coverage against April values would re-open the spent holdout (forbidden by the protocol), so it was not done; the
mismatch is flagged as an appended `outcome_vintage_mismatch` field on both WEO entries of `registered_models.yaml` and
in `v6_pred_models.md` §6. Prospective coverage on resolved registry questions is the real test (both densities are
already on probation, FIX7). Owner decision: either align the family's resolution rule with October values in a new
family version, or keep April and accept that the holdout says nothing about April calibration.

## 7. L5 — reasons for the FIX4B retractions

* Curation schema: optional top-level `retired: [{id, reason, replaced_by: [ids], basis}]` (validated: the id must not
  be curated, `replaced_by` must list curated ids). `load_effect_estimates(..., retired=...)` writes per-row retraction
  detail `{basis, raw_sha256s, retired: {reason, replaced_by: [estimate keys], basis}}`; `atlas.effects_lit` passes it.
* The Gutmann entry records both retirements (horizon convention AUDIT3 M5a; replaced by
  `…:gdp_pc_growth_years_1_2`).
* Existing retractions 1 and 2 (load 20): `kg.effect_estimate_retraction` allows one row per estimate row and
  `kg.retraction_annotation` only targets statement / causal-claim retractions (no migration allowed), so the record was
  appended as artifact `24926bfc…` (schema `reva-effect-retraction-annotation/1`) with `meta.activity`
  `e41da24a-5a01-40a5-9ae5-07c3bf9e0423` (kind `effects.retraction_annotation`, parameters `retraction_ids [1, 2]`),
  written live under flock; re-running appends nothing.

## 8. L6 and the PRED-CONFLICT requests

L6: `v6_effects_more.md` 302 → **301** (296 + 5), and "every estimate states `shock_profile`" → 20 current rows are
`unspecified` (12 Ramey survey multipliers + 8 Ortega–Osbat); `v6_conflict.md` §3 all placed = **376,082** (360,986
counted only `placed`) plus the FIX6 rule and numbers; `v6_pred_energy.md` family sizes (L3).

**GW 260 / 345.** `atlas.ucdp.orgvio_cy` (registered 2026-10-16): gw:260 has no ISO3 in all 37 rows (1989–2025) — the
undated historical entry 260 "West Germany" (`historical_states_qids.json`, shared with COW) answered every date. Fix:
GW policy window 260 → DEU from 1990-10-03 (Gleditsch & Ward continue 260 for unified Germany; countrycode's panel
publishes gwn 260 → DEU), COW unchanged (COW codes unified Germany 255). gw:345 already resolves to SRB from 2006-06-05
(20 rows 2006–2025 carry SRB); the 17 rows 1989–2005 stay without ISO3 by design (SFRY / FRY / Serbia and Montenegro
are not Serbia: historical-state caveat). So the 10 PRED-CONFLICT skips at the last data year are most likely gw:260
only (5 families × 2 horizons); after the `atlas.ucdp` re-ingest Germany gets questions. The fix also applies to every
GW-coded table (`StateCodeCrosswalk.resolve('ucdp'|'gw', 260, ≥ 1990-10-03)`).

**Zero by absence.** A family may declare `absence_rule: zero_within_coverage` only for the event-census tables
`atlas.ucdp_ged.{country,adm1,priogrid}_month` and their `cand_*` versions (any other table is refused). The question
seals `absence_rule` + its text (other families' questions and ids are unchanged). At resolution, a table version
covers the periods from its first to its last published cell of the indicator (any geography); an absent cell for the
question's geography in a covered period resolves as `"0"` with cell reference `{absent_cell: true, value_label: "0
recorded (no cell: no event recorded by the source in a covered period)", rule, table_semantics, coverage}`; outside
coverage the question waits (or voids by its void date). The zero exists only in the resolution record. Questions
sealed before this rule never resolve by absence.

## 9. Tests

* `tests/unit/test_atlas_ucdp_ged.py` (17, +6): border rule with synthetic polygons (Kobane-like SYR point in TR, Gaza
  ISR exception, EGY in PS not placed, SDN before/after 2011-07-09), territory registry, ISR country-month flags,
  left-censoring and NE reference, families-API input licence, GW 260 / 345 resolution.
* `tests/integration/test_ucdp_ged_ingest.py` (+ retraction of absent GED statements → `reclassified_by_reva` /
  `raw_unchanged`).
* `tests/integration/test_fix6_effect_retirement.py` (2): retirement record in the retraction detail; annotation
  append-only and idempotent; curation validation.
* `tests/unit/test_fix6_zero_by_absence.py` (3): table allow-list, labelled zero only within coverage, `resolve_batch`.
* Run: the above + related geopol / causdata / sanctions / coups / colonial / politics / FIX4C / links / catalog /
  effects / registry suites, invariants (one unrelated failure: `nlqa/domain_*` NotImplementedError, another agent's
  in-flight work). ruff + mypy clean on touched modules.

## 10. Commands to finish (when free disk ≥ 5 GB)

```bash
df -h /   # >= 5 GB free
flock /tmp/reva-devdb.lock uv run reva seed                                   # registers atlas.ucdp_ged 1.0.1
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.ucdp_ged            # M1/M2/L1/L2: new tables + KG revision .2,
                                                                              # 215 located_in edges retracted
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.ucdp                # orgvio_cy: gw:260 -> DEU 1991-2025
# then PRED-CONFLICT can regenerate (Germany questions) and seal country-month families with absence_rule
```

Owner decisions: (1) the territory list (`review: pending_owner`), in particular ISR→PSE and whether to publish a PSE
share; (2) WEO resolution vintage (§6); (3) the ETH→ERI and later border cases stay unplaced unless reviewed.
