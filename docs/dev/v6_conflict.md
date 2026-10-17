# V6 CONFLICT — wars & security at finer space-time (UCDP GED, non-state, one-sided; military spending; peacekeeping)

Agent V6CONFLICT, 2026-10-17. Status: **skeleton** (table names fixed so PRED-CONFLICT can code against them; numbers
filled in when the connector has run).

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

## 1. Sources
(to be filled)

## 2. Tables, entities, statements loaded
(to be filled)

## 3. ADM1 placement (REVA computation)
(to be filled)

## 4. Military expenditure / arms (SIPRI via WDI)
(to be filled)

## 5. Peace processes / peacekeeping links
(to be filled)

## 6. Missing, owner decisions, commands
(to be filled)
