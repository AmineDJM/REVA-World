# V3 CAUSOPS live run — event and causal sources in the dev DB (agent LOADALL, 2026-10-17)

LOADALL took over CAUSOPS's queue: it loaded the event and causal sources into the dev database `reva`,
recorded and warmed a new atlas snapshot, materialised the links store, refreshed the causal claims and
checked the API. Scripts are in the session scratchpad (`loadall/`). The ingest runner is CAUSOPS's
`run_loads.sh`: one source at a time, with `df -h /` ≥ 6 GB checked before each.

## 1. Connector versions (owner decision a)

`scratchpad/reg_status.py` reported code/spec changed after registration for 14 sources, and 3 source keys not
registered. Patch bumps:

| source(s) | version |
|---|---|
| cliopatria, wals, grambank, gcat (`atlas_space.py`), nga_wpi (`atlas_ports.py`) | 1.0.0 → 1.0.1 |
| openflights (`OF_VERSION`), ourairports (`OA_VERSION`) | 1.0.0 → 1.0.1 |
| geonames_cities (`atlas_cities.py`) | 1.0.0 → 1.0.1 |
| gap_cbs_bes, gap_eurostat_nuts, gap_insee, gap_spc | 1.0.0 → 1.0.1 |
| glottolog + glottolog_wikidata (`atlas_glottolog.py`) | 1.1.0 → 1.1.1 |

No test asserts these versions. `reva db bootstrap` re-seeded the registry and also registered the
`harvard.dataverse.coldat`, `cow.terrchange` and `icow.paulhensel` source keys. Afterwards `reg_status.py`
reported all 108 atlas sources `ok`.

## 2. Ingests (budget day 2026-10-17 UTC, started 00:00:26 DB clock)

First, `check_ref_pids.py` verified the CAUSCORE reference allow-list request on Wikidata through WDHARVEST's
budgeted client (2 wbapi requests). All four properties exist with the expected datatypes:

* P212 ISBN-13 (external-id)
* P356 DOI (external-id; no French label on Wikidata)
* P957 ISBN-10 (external-id)
* P1683 quotation (monolingualtext)

| source | status | requests | MB | atlas tables | rows |
|---|---|---:|---:|---:|---:|
| `atlas.rcs_dem` | failed | | | | |
| `atlas.cshapes` | completed | 2 | 4.7 | 2 | 5,288 |
| `atlas.icb` | completed | 5 | 1.8 | 1 | 1,770 |
| `atlas.mid` | completed | 3 | 4.3 | 1 | 14,838 |
| `atlas.ucdp_term` | completed | 5 | 2.0 | 2 | 795 |
| `atlas.ecrises.laeven_valencia` | completed | 1 | 0.1 | 1 | 3,531 |
| `atlas.ecrises.reinhart_rogoff` | completed | 1 | 1.6 | 1 | 171,546 |
| `atlas.ecrises.jst` | completed | 2 | 1.5 | 1 | 116,882 |
| `atlas.ecrises.esrb` | completed | 1 | 0.1 | 1 | 1,785 |
| `atlas.navco` | completed | 8 | 22.2 | 3 | 104,078 |
| `atlas.mec` | completed | 2 | 1.3 | 1 | 56,144 |
| `atlas.massmob` | completed | 2 | 15.6 | 1 | 112,148 |
| `atlas.ties` | completed | 2 | 1.0 | 2 | 19,120 |
| `atlas.eusanct` | completed | 2 | 65.4 | 2 | 8,904 |
| `atlas.eu_sanctionsmap` | completed | 1 | 0.4 | 1 | 90 |
| `atlas.ofac_programs` | completed | 1 | 0.1 | 1 | 56 |
| `atlas.untc` | completed | 56 | 25.4 | 1 | 23,129 |
| `atlas.ohchr` | completed | 20 | 3.6 | 1 | 8,735 |
| `atlas.unesco_ich` | completed | 2 | 0.5 | 1 | 1,652 |
| `atlas.unesco_whc` | completed | 2 | 0.7 | 1 | 2,050 |
| `atlas.unesco_whc_links` | completed | 2 | 0.7 | 0 | 0 |
| `atlas.peacekeeping` | completed | 12 | 46.6 | 5 | 586,341 |
| `atlas.archigos` | completed | 3 | 3.4 | 1 | 26,474 |
| `atlas.noaa_hazards` | completed | 239 | 20.4 | 1 | 19,332 |
| `atlas.coldat` | completed | 5 | 0.6 | 1 | 2,526 |
| `atlas.icow` | completed | 3 | 1.1 | 2 | 3,219 |
| `atlas.cowtc` | completed | 2 | 0.8 | 2 | 5,734 |
| `atlas.cliopatria` | completed | 3 | 51.0 | 0 | 0 |
| `atlas.glottolog` | completed | 2 | 49.7 | 1 | 4,636 |
| `atlas.wals` | completed | 2 | 12.6 | 0 | 0 |
| `atlas.grambank` | completed | 2 | 9.1 | 0 | 0 |
| `atlas.openflights` | completed | 5 | 3.9 | 1 | 14,392 |
| `atlas.ourairports` | failed | | | | |
| `atlas.gcat` | completed | 5 | 34.0 | 2 | 4,530 |
| `atlas.nga_wpi` | completed | 1 | 1.3 | 1 | 548 |
| `atlas.wikidata_cables` | failed | | | | |
| `atlas.gap_cbs_bes` | completed | 4 | 0.1 | 3 | 612 |
| `atlas.gap_eurostat_nuts` | completed | 7 | 0.0 | 1 | 1,040 |
| `atlas.gap_insee` | completed | 2 | 0.1 | 2 | 1,403 |
| `atlas.gap_spc` | completed | 4 | 2.6 | 4 | 10,167 |
| `atlas.gap_jersey` | completed | 1 | 0.0 | 1 | 1,344 |
| `atlas.geonames_cities` | completed | 2 | 3.5 | 3 | 359,601 |
| `atlas.un_wup` | completed | 4 | 70.7 | 5 | 4,253,558 |

Totals: 40 sources completed, about 0.53 GB downloaded. The ledger counted 506 requests and 0.47 GB for the
day by 00:44 UTC, well inside the 3 GiB cap. Some rows show 0 atlas tables (Cliopatria, WALS, Grambank, UNESCO
WHC links): these sources load only into the KG. Not loaded:

* `atlas.ourairports`: `HTTP 503 from ourairports.com` on all 3 attempts (00:33, 00:43, 00:44).
* `atlas.rcs_dem`: `HTTP 503 from api.osf.io`, as on 2026-10-16.
* `atlas.wikidata_cables`: the WDQS cool-down was in force (Retry-After about 1,000 s, renewed all session).
  The run was interrupted cleanly (SIGINT) after 7 min; its reservation was settled and nothing partial was
  registered. Without cables, the `submarine_cable` link kind has 0 links.
* Already present from 2026-10-16, so not re-run: `atlas.bis`, `atlas.oecd.fiscal`, `atlas.oecd.skills`,
  `atlas.usgs_minerals`, `atlas.eia_intl`.
* Optional modes not used: GeoNames alternate names (option, about 208 MB), `atlas.glottolog_wikidata`
  (WDQS).

**Next budget day:** run `uv run reva atlas ingest atlas.ourairports`, `atlas.rcs_dem` and
`atlas.wikidata_cables` (the last only when `var/locks/wikidata.state.json` shows no WDQS cool-down). Then
run `reva atlas snapshot --warm --skip-unchanged`, `reva links materialise --atlas-origin live --kg-origin
live` and `reva causal refresh --origin live`. Their monthly schedules also cover them.

## 3. Wikidata statements (owner decision c)

The new governed path `reva kg import-harvest` re-loads the Wikidata harvests from the archived raw responses
(see `v3_wdharvest.md`, "Governed import into the dev database"):

| harvest | loads | statements | verification |
|---|---:|---:|---|
| WDHARVEST `core.*` (`reva_kg_wdharvest`) | 113 | 104,654 | all identical |
| CAUSWD (`reva_kg_causwd`) | 39, then +98 at 00:45 UTC | 10,238 | 3 differ |

Of the 3 CAUSWD differences, 1 is a conflict where WDHARVEST's version was kept, and 2 gained ISBN
references under the newer allow-list. CAUSWD's 00:02 run (until about 03:50 UTC) keeps adding loads. Re-run
`uv run reva kg import-harvest --from-db reva_kg_causwd` then `reva causal refresh --origin live`; both are
idempotent.

## 4. Snapshot, links, causal refresh (owner decision b)

* **Snapshot.** `reva atlas snapshot --origin live --skip-unchanged` recorded
  `1d9b950f658c720013934875a390233ae602febac7a4e2cb2cb8c99b706d1f94`: 220 tables, 93,490,063 rows, cutoff
  2026-10-17T00:44:39Z (was 159 / 87.5 M). It was pinned with `pin_snapshot` (`live-snapshot.pin`).
* **Old query file.** The superseded `atlas-b11f1c51…-v6.duckdb` (6.4 GB) and its `.sha256` were deleted
  before the build.
* **Warm build.** `reva atlas snapshot --warm --skip-unchanged` reused the snapshot and built the query file:
  7.11 GB in 912 s. Free disk went from 14 GB to 7.2 GB.
* **Links.** `reva links materialise --atlas-origin live --kg-origin live` stored materialisation 1:
  1,147,110 links in 321 s (KG watermark load 321). The database is now 3.9 GB.
* **Causal refresh.** `reva causal refresh --origin live` loaded 1,002 Wikidata claims; the 26,789 dataset
  claims had been graded at ingest. A second run wrote nothing.

### Links by kind

| kind | links | kind | links |
|---|---:|---|---:|
| bilateral_trade_baci | 386,708 | air_routes_2014 | 4,421 |
| unga_vote_agreement | 144,043 | diplomatic_relation | 3,980 |
| co_party | 114,862 | shared_currency | 2,912 |
| bilateral_trade_imports | 98,520 | successor_of | 1,855 |
| bilateral_trade | 97,944 | hazard_shared_event | 1,555 |
| shared_polity_territory | 50,449 | launch_service | 1,237 |
| shared_official_language | 37,600 | border_wikidata | 1,044 |
| shared_language_used | 37,512 | land_border_cshapes | 646 |
| co_signatory | 28,116 | colonial_rule_cow | 620 |
| co_member | 27,404 | shared_head_of_state | 534 |
| conflict_co_participant | 26,157 | alliance_co_member | 466 |
| migrant_stock | 26,125 | colonial_rule_cshapes | 343 |
| conflict_same_side | 21,090 | land_border | 321 |
| peacekeeping_contributor_to_host | 9,700 | colonised_by_coldat | 255 |
| shared_language_glottolog | 8,383 | former_colony_of | 105 |
| sanction_episode | 7,683 | colony_of | 62 |
| conflict_opposing_sides | 4,438 | us_sanctions_program | 20 |

## 5. Counts (dev DB, origin live, 2026-10-17 01:10 UTC)

* **KG.** 119,640 current entities and 2,446,898 current statements. 71,544 entities are of an event kind,
  for example:

  | kind | entities |
  |---|---:|
  | event | 36,086 |
  | earthquake | 6,697 |
  | militarized_incident | 4,482 |
  | campaign_outcome | 3,676 |
  | tsunami | 3,138 |
  | militarized_interstate_dispute | 2,436 |
  | dispute_outcome | 2,419 |
  | sanction_episode | 1,738 |
  | crisis_trigger | 1,637 |
  | crisis_outcome | 1,635 |
  | foreign_policy_crisis | 1,131 |
  | volcanic_eruption | 903 |
  | armed_conflict_episode | 614 |
  | international_crisis | 512 |
  | conflict_termination | 505 |
  | peace_agreement | 374 |
  | armed_conflict | 303 |
  | war | 258 |

* **Causal claims (current, live): 27,791.**
  * By tier: T1 26,789; T2 1 (a Wikidata reference with an ISBN/DOI); T3 235; T4 766.
  * By grade (rules version 1): B 26,791; C 182; D 746; E 72; A 0. A needs corroboration by an
    independent second source that is also consistent in time. No two loaded sources assert the same link.
  * By relation (all `cause` family):

    | relation | claims |
    |---|---:|
    | reaction_to | 12,825 |
    | coded_outcome | 9,912 |
    | coded_trigger | 3,823 |
    | has_effect | 401 |
    | has_cause | 339 |
    | contributing_factor | 302 |
    | immediate_cause | 189 |

  * **Temporal check:** overlapping 16,363; consistent 10,390; undetermined 971; **inconsistent 67**.
* **Claims by source:**

| source | claims | B | C | D | E | temporally inconsistent |
|---|---:|---:|---:|---:|---:|---:|
| harvard.dataverse.massmob | 7,221 | 7,221 | 0 | 0 | 0 | 0 |
| icb.data | 4,398 | 4,397 | 0 | 0 | 1 | 1 |
| harvard.dataverse.navco | 4,268 | 4,266 | 0 | 0 | 2 | 2 |
| harvard.dataverse.mec | 3,504 | 3,504 | 0 | 0 | 0 | 0 |
| cow.data (MID) | 2,419 | 2,419 | 0 | 0 | 0 | 0 |
| noaa.ncei_hazards | 2,186 | 2,171 | 0 | 0 | 15 | 15 |
| ties.unc | 1,024 | 1,024 | 0 | 0 | 0 | 0 |
| wikidata.wbapi | 1,002 | 21 | 182 | 746 | 53 | 48 |
| ucdp.downloads | 887 | 887 | 0 | 0 | 0 | 0 |
| eusanct.kondata | 459 | 458 | 0 | 0 | 1 | 1 |
| archigos.leaders | 327 | 327 | 0 | 0 | 0 | 0 |
| esrb.fcdb | 96 | 96 | 0 | 0 | 0 | 0 |

The Laeven–Valencia, Reinhart–Rogoff, JST, CShapes, UNTC, UNESCO, peacekeeping, colonial (ColDat, ICOW,
COW territorial change) and language sources load events and context statements, but no causal claims: their
codebooks assert none.

## 6. API checks

Run with the API test client against the dev DB and a temporary `read` token, revoked afterwards
(`revoked_at` set and checked). Script: `scratchpad/loadall/verify_causal_api.py`; full output:
`scratchpad/loadall/verify_api.json`.

### `/v1/causal/events/{id}/why`

All answered 200 in 0.02–0.3 s.

* **ICB `icb:crisis:196` (Cuban Missiles).** 1 claim in the coded lane: "Breakpoint of CUBAN MISSILES:
  External change" is the coded trigger. T1, codebook ICB1 v16 term BREAK, raw sha `69757d13…`. Grade **B**
  (`tier.T1`, `temporal.overlapping` 1962-10-16 vs 1962-10-16 at day precision, `corroboration.single`,
  `contest.none`, `grade.B.expert`).
* **NOAA `ncei_hazards:tsunami:5413` (2011-03-11 Honshu).** Coded trigger: earthquake NCEI 9799, grade B.
* **NOAA `ncei_hazards:tsunami:2439` (2004-12-26 Sumatra).** Coded trigger: earthquake NCEI 5823, grade B.
* **Laeven–Valencia `ecrises:lv:systemic_banking:ARG:2001` and `…:USA:2007`.** 200 with 0 claims: "No
  cause is asserted by any loaded source". The dataset dates crises; it asserts no causes.
* **NAVCO `navco13:206-1980` (Solidarity, Poland 1980–1989).** 200 with 0 claims on the campaign itself.
  Its claim is on the campaign's outcome: `navco13:206-1980` → `navco13:206-1980:outcome` (coded_outcome,
  B, consistent).
* **Wikidata `Q8698` (Great Depression).** 1 claim: Wall Street crash *has effect*. T4, grade **D**,
  temporal `undetermined`. Q8698 itself is not in the dev KG yet (CAUSWD frontier), so it has no start
  date and no label.

### Other endpoints

* **`/v1/causal/chain`:**
  * `icb:crisis:196:trigger` → `icb:crisis:196:outcome`: found, 2 hops, chain grade B.
  * earthquake 9799 → tsunami 5413: found, 1 hop, B.
  * `Q201684` → `Q8698`: found, 1 hop, D.
* **`/v1/causal/consistency`** (0.7 s): 67 temporal violations, 1 self-loop (Wikidata "peace treaty
  has cause peace treaty", Q625298), 5 contested, 5 cycles, 0 ungraded.
* **`/v1/links/pair?a=USA&b=GBR`** (200, 24 s cold): 12 kinds. Counts include conflict_co_participant 194,
  conflict_same_side 60, conflict_opposing_sides 13, co_member 21, co_signatory 10 and shared language
  (Glottolog 5). There are no trade links for this pair, because the atlas trade kinds keep the top-10
  partners per facet and direction.
* **`/v1/links/pair?a=BRA&b=PRT`** (200, 6 s): 18 kinds, including:
  * unga_vote_agreement 411, bilateral_trade_baci 220, bilateral_trade 52, migrant_stock 16
  * colonial_rule_cow 1, colonised_by_coldat 1
  * shared_official_language 1, air_routes_2014 2, hazard_shared_event 1
* **`/v1/atlas/headline/map`** (0.1–2.6 s):
  * `reva.population.total` 2023: 238 countries (WPP 237, WDI 1).
  * `reva.economy.gdp_usd` 2023: 210 (WDI 204, WEO 3, SPC 2, CBS BES 1).
  * `reva.population.total_1_january` 2024: 8 (Eurostat NUTS gap-fill 7, CBS BES 1).
  * `reva.health.life_expectancy_national_tables` 2022: 6.
  * The last two concepts had no series in the previous snapshot.
* **`/v1/atlas/headline/series?concept=reva.population.total&iso3=BES`:** 200.
* **`/v1/events/search`** (5–7.5 s):
  * CUB 1962 (involving): 2 events, MID 61 and ICB 196 for CUB.
  * JPN 2011-03: 10 events (NCEI earthquakes and tsunamis, Wikidata items).
  * POL 1980–1989 (involving): 25 events (NAVCO Solidarity, Reinhart–Rogoff currency crisis, ICB, UCDP…).

## 7. Disk

Free disk after the run was 5.7 GB. The 7.1 GB query file is the pinned live snapshot; it must stay, or the
API rebuilds it on the next atlas read (about 15 min). `var/objects` is 4.1 GB.
