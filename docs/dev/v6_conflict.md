# V6 CONFLICT — wars & security at finer space-time (UCDP GED, non-state, one-sided; military spending; peacekeeping)

Agent V6CONFLICT, 2026-10-17. Status: **done, loaded live into the dev DB** (connector `atlas.ucdp_ged` 1.0.0, budget
policy budgets-1.3.32: 12 requests, 67.2 MB on the wire, 0 refused; 8 atlas tables, 2,761 KG entities and 39,941 statements,
0 rejected). Tests: `tests/unit/test_atlas_ucdp_ged.py` (11), `tests/integration/test_ucdp_ged_ingest.py` (1, db).

## 0. For PRED-CONFLICT — table and entity names (stable)

Connector `atlas.ucdp_ged` (source key `ucdp.ged`, atlas source `ucdp_ged`). Every table is in the fixed atlas long
schema (`reva.atlas.tables.ATLAS_SCHEMA`); periods are half-open `[period_start, period_end)`.

| Atlas table | Grain | `provider_geo` / `iso3` | Indicators |
|---|---|---|---|
| `atlas.ucdp_ged.events` | one row per GED 26.1 event (1989-01-01 → 2025-12-31) | `gw:<country_id>` / crosswalk ISO3 at the event date | `deaths_best\|event=<GED id>` (value = `best`); flags: `tov` (1 state-based, 2 non-state, 3 one-sided), `low`, `high`, `deaths_a/b/civilians/unknown`, `conflict_new_id`, `dyad_new_id`, `side_a_new_id`, `side_b_new_id`, `lat`, `lon`, `where_prec`, `date_prec`, `event_clarity`, `active_year`, `priogrid_gid`, `adm1` (REVA point-in-polygon, see §3) |
| `atlas.ucdp_ged.country_month` | country × calendar month (month of `date_start`) | `gw:<country_id>` / ISO3 | `events\|tov=<all,sb,ns,os>`, `deaths_best\|tov=…`, `deaths_low\|tov=…`, `deaths_high\|tov=…`, `deaths_civilians\|tov=…` |
| `atlas.ucdp_ged.adm1_month` | ADM1 × month | `iso3166_2:<code>` / iso3 **null by design** (country in `flags.country_iso3`) | same indicators; only events with `where_prec` 1-4 placed by REVA point-in-polygon |
| `atlas.ucdp_ged.priogrid_month` | PRIO-GRID cell × month (GED's own `priogrid_gid`) | `priogrid:<gid>` / iso3 null | `events\|tov=…`, `deaths_best\|tov=…`, `deaths_low\|tov=…`, `deaths_high\|tov=…` |
| `atlas.ucdp_ged.cand_events`, `cand_country_month`, `cand_adm1_month`, `cand_priogrid_month` | same, from the **UCDP Candidate** monthly/quarterly releases (2026, preliminary, `flags.candidate = true`, `code_status` verbatim) | same | same |

Zero semantics: cells are emitted only where GED records ≥ 1 event. GED is a census of events of UCDP-coded organised
violence (≥ 1 death; dyads that crossed 25 battle-related deaths in at least one year); an absent country-month in
the covered period means *GED records no event*, which a model may treat as 0 **with that definition** (the atlas
never writes the zero). Candidate cells are not comparable one-to-one with final GED (the Candidate relaxes the
annual threshold; events can be dropped or re-coded in the annual release).

KG (source key `ucdp.ged`): conflict-level entities only — `ucdp:conflict:<id>` (state-based, KGGEOPOL),
`ucdp:nonstate:<id>` (UCDP Non-State 26.1), `ucdp:onesided:<id>` (UCDP One-Sided 26.1), with GED-derived yearly
event counts / fatalities, first/last event dates and `ucdpged:events_in_adm1` → `iso3166_2:<code>`. Individual
events are never KG entities.

## 1. Sources (checked 2026-10-17, all without an account)

| File | Licence | Bytes | Use |
|---|---|---|---|
| `ucdp.uu.se/downloads/ged/ged261-csv.zip` (GED 26.1, 417,968 events 1989-2025) | CC BY 4.0 | 39.1 MB | events + aggregates + KG statistics |
| `ged/ged261.pdf` (codebook, sha256 `f767380e…`), `candidateged/ucdp-candidate-codebook1.5.pdf` (`0107eac7…`) | CC BY 4.0 | 1.6 MB | archived; definitions quoted in code/catalog |
| downloads page + Candidate CSVs discovered on it: `GEDEvent_v26_01_26_06.csv` (quarterly Jan-Jun), `GEDEvent_v26_0_7.csv`, `GEDEvent_v26_0_8.csv` | CC BY 4.0 | 10.7 MB | `cand_*` tables (13,659 events after 26 ids superseded by a later release) |
| `nsos/ucdp-nonstate-261-csv.zip` (1,821 rows / 1,001 conflicts), `nsos/ucdp-onesided-261-csv.zip` (1,408 rows / 378 instances), `actor/ucdp-actor-261-csv.zip` | CC BY 4.0 | 0.18 MB | KG conflict-level entities |
| Natural Earth 1:10m admin-1 ZIP (sha256 `efc59726…`, **the same bytes V4CLIMATE used**) | public domain | 14.9 MB | ADM1 polygons |
| countrycode 1.9.0 (CRAN) | GPL-3 code list | 0.5 MB | GW code -> ISO3 (`StateCodeCrosswalk`) |

The UCDP API needs a token (not used). Raw archive of a run ~67 MB (NE, actor and countrycode bytes are already in the
store from V4CLIMATE / KGGEOPOL: content-addressed). Source registry `registry/sources/ucdp_ged.yaml`, budget request
`registry/atlas/budget_requests/v6conflict.yaml` (ucdp.ged 40/day, merged in budgets-1.3.30+).

## 2. Loaded (dev DB, live, 2026-10-17)

| Atlas table | Rows |
|---|---|
| `ucdp_ged.events` | 417,968 |
| `ucdp_ged.country_month` | 203,725 (15,341 country-months with events, 1989-01 .. 2025-12) |
| `ucdp_ged.adm1_month` | 698,205 |
| `ucdp_ged.priogrid_month` | 1,114,636 |
| `ucdp_ged.cand_events` / `cand_country_month` / `cand_adm1_month` / `cand_priogrid_month` | 13,659 / 5,895 / 24,525 / 47,624 (2026-01 .. 2026-08) |

Parquet sizes are small (events ~15 MB, aggregates 1-4 MB, zstd). Catalog `registry/atlas/catalog/conflict_events.yaml`:
5 monthly country concepts (deaths all / sb / ns / os, events all).

Codebook checks reported by the run (never corrected): `best = deaths_a+deaths_b+deaths_civilians+deaths_unknown` holds for
all events; 5,075 final events (44 Candidate) have `low > best` or `best > high`; 32,778 events have `best = 0` (an event
exists when "at least one dead needs to be registered in the high, best or low estimate"); 7,184 events span two calendar
months (counted in the month of `date_start`, `flags.spanning`). Unmapped GW codes kept with `iso3 = null` and the
historical-state QID: 260 (FRG 1989-90), 345 (Yugoslavia / FRY), 365 (USSR 1989-91).

**KG** (source key `ucdp.ged`, revision `ucdp-26.1+v6conflict.1`): 1,001 `ucdp:nonstate:<id>` (kind `non_state_conflict`),
378 `ucdp:onesided:<id>` (kind `one_sided_violence`), 1,382 `ucdp:actor:<id>` (195 identical to KGGEOPOL's and therefore
`unchanged`; 197 temporary coalitions not in the Actor Dataset, labelled with the dataset's name and their component ids);
39,941 statements: `ucdpnsos:fatalities` 3,229 (best + low/high bounds, as published), `ucdp:nonstate_side` 2,444,
`ucdp:location` 2,105, `ucdpnsos:dyad_id` 1,379, `ucdp:episode_end` 1,147, `ucdp:start_date` / `ucdpnsos:org_level` 1,001
each, `ucdpnsos:perpetrator` 592 (government perpetrators resolved to the state entity, e.g. Q794 Iran), `ucdp:secondary_party`
39, and the REVA-computed GED statistics on all 1,581 GED conflict ids (202 state-based on KGGEOPOL's `ucdp:conflict:<id>`,
all 202 present): `ucdpged:events` / `ucdpged:fatalities` per conflict-year (8,415 each), first / last event dates, and
7,012 `ucdpged:events_in_adm1`. Predicates: `registry/kg/predicates_conflict.yaml`; family `located_in` for
`ucdpged:events_in_adm1` (`registry/kg/relations_v4.d/v6conflict.yaml`); events taxonomy: new types `non_state_conflict`,
`one_sided_violence`, dataset kinds `ucdp:nonstate:` / `ucdp:onesided:`, roles perpetrator / admin_area; link
statement edges `ucdp:location`, `ucdp:nonstate_side`, `ucdpnsos:perpetrator`, `ucdpged:events_in_adm1`.

Example paths (live ids):

* `ucdp:conflict:234` "Israel: Palestine" —`ucdpged:events_in_adm1` (derived point_in_polygon; 9,089 events, 66,770 best
  deaths, 1989-2025; all 9,089 flagged `events_ged_country_differs`: GED codes them under Israel)→ `iso3166_2:PS-GZZ` Gaza.
  Its GED fatalities: 2023 26,800 / 2024 21,600 / 2025 14,456 (`ucdpged:fatalities`, sums of best).
* `ucdp:onesided:484` "Government of Afghanistan - civilians" —`ucdpnsos:perpetrator`→ state entity of the Afghan
  government (GW 700); `atlas.ucdp_ged.events` row `deaths_best|event=637360` (Herat, 2026 Candidate) has
  `conflict_new_id` 484 in flags.
* Peace operations: `unpk:mission:MONUSCO` —`unpk:mission_country`→ Q974 (DR Congo) ←`ucdp:location`— `ucdp:nonstate:4575`
  "Alur - Lendu". No identifier links a mission to a conflict, so the link is this path through the country (now
  traversable: statement edges above); no direct mission-conflict edge is made.

## 3. ADM1 placement (REVA computation)

Points = GED coordinates of events with `where_prec` 1-4 (exact, <= 25 km, ADM2 or ADM1 representative point); 5 (linear /
fuzzy area), 6 (country only) and 7 (waters / airspace) are never placed. Polygon = Natural Earth 1:10m admin-1 (shapely
STRtree, `covered_by`); identity = the polygon's published `iso_3166_2` code only (GED `adm_1` names are never read; the
column is not even loaded). 60 codes Natural Earth gives to several polygons (AF-URU, AF-PAR, BA-BIH, CO-CUN, SD-DS,
PE-LIM, 19 PH codes ...) and placeholder codes are not used. Geometry decides; when the code's country differs from GED's
country the event is still placed and counted in `flags.events_ged_country_differs`.

Final GED (connector 1.0.0, as loaded): **all placed events 376,082** = status `placed` 360,986 + placed with GED country different 14,674 + placed with historical GED state 422 (corrected by FIX6, AUDIT4 L6: "placed 360,986" counted only status `placed`); placed with GED country different 14,674 (Gaza/West Bank under Israel ~10,850; pre-2011
Sudan events in today's South Sudan; border points at 1:10m); placed with historical GED state 422; not placed: where_prec
5/6/7 18,645 / 5,309 / 266, ambiguous or placeholder code 15,112 (8,597 in Bosnia: BA-BIH), outside polygons 2,554
(coast / generalisation). Candidate: 11,618 + 656 + 2 placed of 13,659.

**FIX6 (connector 1.0.1, AUDIT4 M1/M2; code and tests done, live re-ingest queued: disk < 5 GB).** The placement rule
is now: the polygon's country must be GED's own country, except a reviewed territory list
(`registry/conflict/territory_exceptions.yaml`, curator decision pending owner review: ISR→PSE, SDN→SSD before
2011-07-09, IDN→TLS before 2002-05-20, ETH→ERI before 1993-05-24, ZAF→NAM before 1990-03-21); other cross-border hits
are `not_placed:border_generalisation` (polygon kept in `flags.adm1_polygon`). Recomputed offline on the archived
GED 26.1 bytes: placed 360,989 + territory exceptions 12,023 (ISR→PSE 10,846, SDN→SSD 771, IDN→TLS 290, ETH→ERI 65,
ZAF→NAM 51) + historical 419 = 373,431; border generalisation 2,651 (SYR→TUR 577, CAF→COD 215, COD→RWA 202, …);
`adm1_month` 698,205 → 688,050 rows; country-month cells flag their events placed in another territory
(`events_placed_in_other_territory`, deaths, `other_territory_iso3`; 607 cells, 329 of them Israel's). KG:
215 `ucdpged:events_in_adm1` edges are retracted on re-ingest (`reclassified_by_reva`, raw unchanged), 141 get new
counts, all 6,797 remaining cite the Natural Earth polygons as a second reference (public domain). See
`docs/dev/v6_fix6.md`.

## 4. Military expenditure / arms (SIPRI via WDI)

SIPRI is not ingested (A-MILITARY, `docs/dev/v3_a_military.md`). The WDI copies are already in the atlas (`atlas.wb.wdi_econ`,
A-WB): MS.MIL.XPND.GD.ZS (8,187 values, 166 countries, 1960-2025), .XPND.CD 8,284, .XPND.CN 8,379, .XPND.ZS 4,930,
MS.MIL.XPRT.KD 2,416 / .MPRT.KD 6,988 (SIPRI TIV), MS.MIL.TOTL.P1 5,355 / .TOTL.TF.ZS 5,025 (IISS, 1985-2020). **Terms
checked 2026-10-17 in the WDI series metadata**: `License_Type` = "SIPRI terms and conditions"
(https://www.sipri.org/about/terms-and-conditions) for the XPND and arms-transfer series, i.e. reproduction needs SIPRI's
authorisation outside fair use (non-commercial and < 10 % of a data set); MS.MIL.TOTL.* are "CC BY-4.0". The catalog
concepts `wb.ms.mil.*` carry the licence note and the `licence:check` tag, but the **atlas rows have empty flags** (the
licence is per series, not per value). Owner decision below. No new military data was loaded.

## 5. Peace processes / peacekeeping

UN peacekeeping (`atlas.peacekeeping`, `unpk:mission:*`), UCDP peace agreements and termination (CAUSDATA, `ucdp:pa:*`,
`ucdp:episode:*`), dyad episodes (CAUSMORE `ucdp:dyadepisode:*`) are already loaded. They meet the new data through UCDP
conflict ids (`ucdp:conflict:<id>` is the subject of both GED statistics and the peace-agreement claims) and through
countries (`ucdp:location` / `unpk:mission_country`), never by name.

## 6. Missing, owner decisions, commands

Missing / not done:
* GED events before 1989 do not exist; Candidate 2026 is preliminary (Candidate releases include events "regardless of the
  annual threshold"; do not mix with final GED without saying so).
* Individual events are not KG entities (by design); GED source text (articles, headlines) is not copied.
* No conflict-level KG statistics from Candidate data (preliminary); no time roles (event cards) from GED first/last dates
  (they would change the intervals of KGGEOPOL's conflict cards: left to EVENTS).
* ADM1 for the 60 ambiguous Natural Earth codes (needs a reviewed per-polygon code fix or another ADM1 source, e.g.
  geoBoundaries - licence to check).
* PRED-CONFLICT: the tables exist; `src/reva/evaluation/conflict` does not read them yet.

Owner decisions:
1. SIPRI-sourced WDI series (MS.MIL.XPND.*, MS.MIL.XPRT/MPRT.KD) are under SIPRI terms: request authorisation from SIPRI
   (needs a named person / affiliation) or restrict serving them (non-commercial, < 10 %); A-WB rows should carry a
   per-value licence flag.
2. ADM1 placement rule: geometry first with the country-differs count (current) vs dropping events whose GED country
   differs (V4CLIMATE's station rule). The Gaza / West Bank events are the main case.

Commands (re-run, e.g. monthly for new Candidate releases):
```bash
df -h /   # >= 5 GB free
flock /tmp/reva-devdb.lock uv run reva seed
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.ucdp_ged
uv run pytest -q tests/unit/test_atlas_ucdp_ged.py tests/integration/test_ucdp_ged_ingest.py
# fixtures: python -I tests/fixtures/source/atlas.ucdp_ged/cut_fixture.py <download_dir> tests/fixtures/source/atlas.ucdp_ged
```
