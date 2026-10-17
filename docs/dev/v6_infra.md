# V6 — V6INFRA: transport & logistics (ports, trade locations, chokepoints, airports, transport statistics)

Agent V6INFRA, 2026-10-17. Builds on A-INFRA (V3: `atlas_ports`, `atlas_flights`, `atlas_cables`,
`atlas_infra_common`) and V4CLIMATE's ADM1 entities (`iso3166_2:<code>`, kind `region_adm1`, Natural Earth admin-1).
Rule kept throughout: facilities are linked only by identifiers the providers publish (ISO 3166-2 codes, UN/LOCODEs,
ISO country codes) or by **labelled** REVA coordinate computations; never by name.

## 1. Sources, licences, decisions

| Source | Access (checked 2026-10-17) | Licence | Decision |
|---|---|---|---|
| **OurAirports** (retry) | `ourairports.com/data/` still served "Temporarily unavailable ... routine maintenance" (HTTP 200, 633 bytes; 503 on 2026-10-16); data on `davidmegginson.github.io` fine | public domain: terms page sentence, and the data repository's `LICENSE` (Unlicense, "released into the public domain") | **atlas.ourairports 1.1.0**: licence check falls back to the repository `LICENSE` (archived, basis recorded per row and in rights); KG airports added |
| **RCS-Dem 2.0** (retry, `atlas.rcs_dem`) | OSF answered 503 all morning; back (200) at ~20:30 UTC | ARDA usage agreement (flagged `open_licence: false`, A-SOCIETY) | live run queued (see §5) |
| **NGA World Port Index** | already loaded by A-INFRA (`nga.wpi`, 548 country rows) | US Government work | **atlas.nga_wpi 1.1.0**: every port becomes a KG entity (country, coordinates, UN/LOCODE, ADM1) |
| **UN/LOCODE** (UNECE) | `unece.org` download pages answer 403 (Cloudflare) to non-browser clients — not worked around; the new site `unlocode.unece.org` serves the publications page, terms and the **official release** package (2025-1, on the UN ICC GitLab `opensource.unicc.org`) without restriction | **conflicting statements**: footer "All UN/CEFACT standards are free to use under CC By 4.0 license" vs site terms "personal, non-commercial use, without any right to resell or redistribute them or to compile or create derivative works therefrom" | **ingested, flagged** `licence_status: conflicting_statements`, `licence_review: pending` on every row/statement; both statements archived and re-checked each run (owner decision §7) |
| **IMF PortWatch** | ArcGIS Online organisation `IMF-portwatch_imf_dataviz`; every item's licence field names `imf.org/external/terms.htm`, which answers 403 to the container (as for A-IMF) | IMF terms, not quotable here | **ingested, flagged** `licence_review: pending`; counts flagged `ais_derived_count`, tonnages `model_estimate` |
| **Eurostat transport** | statistics API (JSON-stat) | Eurostat reuse policy, CC BY 4.0 | **ingested** (aggregated official statistics only) |
| ITF / OECD transport statistics | `sdmx.oecd.org` dataflows `OECD.ITF` (`DSD_TRENDS@DF_TRENDSFREIGHT`, `…PASS`, `…CONT`, `DSD_ST@DF_STFREIGHT`…) exist, OECD CC BY 4.0 | open | **not built** (time): the OECD connector fails closed on unquoted codelist labels, the ITF codelists must be captured first (follow-up §8) |
| Shipping routes / AIS tracks | no open global route dataset found (MarineTraffic/Spire commercial; PortWatch publishes calls/transits, not routes) | — | chokepoint transits + UNCTAD LSBCI (A-TRADE) stand in for routes |

## 2. What was built

| File | Purpose |
|---|---|
| `src/reva/adapters/connectors/v6infra_common.py` | Natural Earth admin-1 fetch through the run (same URL/bytes as V4CLIMATE, stored once), ADM1 placement (`published_code` → `point_in_polygon` → `nearest_polygon` for coastal points), coordinate converters (WPI DMS, UN/LOCODE DDMM), `KgBuilder`, `data_series` entities |
| `src/reva/adapters/connectors/atlas_unlocode.py` | `atlas.unlocode` 1.0.0 (source key `unece.unlocode`) |
| `src/reva/adapters/connectors/atlas_portwatch.py` | `atlas.imf_portwatch` 1.0.0 (`imf.portwatch`) |
| `src/reva/adapters/connectors/atlas_eurostat_transport.py` | `atlas.eurostat_transport` 1.0.0 (reuses `atlas_eurostat_macro`'s parser; budget `eurostat.dissemination`) |
| `src/reva/adapters/connectors/atlas_ports.py` | `atlas.nga_wpi` 1.1.0 (KG layer) |
| `src/reva/adapters/connectors/atlas_flights.py` | `atlas.ourairports` 1.1.0 (licence fallback + KG), `atlas.openflights` 1.0.2 (module digest only) |
| `registry/sources/{unece_unlocode,imf_portwatch}.yaml` | registrations with rights |
| `registry/atlas/budget_requests/v6infra.yaml` | merged as **budgets-1.3.30** (digest re-pinned in `src/reva/autonomy/policy.py`) |
| `registry/kg/relations_v4.d/v6infra.yaml` | 7 family mappings (below) |
| `tests/unit/test_v6infra.py` (11), `tests/integration/test_v6infra_ingest.py` (1, `db`) | offline; fixtures `tests/fixtures/source/atlas.{unlocode,imf_portwatch}` + a Natural Earth excerpt added to `atlas.{nga_wpi,ourairports}` (1.7 MB total, derived excerpts, cutter `tests/fixtures/source/atlas.unlocode/cut_v6infra_fixtures.py`) |

## 3. Atlas tables

| Table | Rows (live) | Content |
|---|---|---|
| `unlocode.locations` | 1,655 (dry build; live deferred) | per country (ISO2): `LOCATIONS`, `LOCATIONS\|function=<0-7,B>` (labels from the release's SKOS file, e.g. `1` "Maritime transport (sea port)", `4` "Air transport (airport)", `B` "Cross Border (deprecated)"); edition 2025-1; 116,086 codes, 249 countries (`XZ` international waters unmapped) |
| `portwatch.chokepoints_daily` | 1,113,672 (dry build; live deferred) | 28 chokepoints × UTC day (2019-01-01 → 2026-10): `TRANSITS\|vessel_type=<container,dry_bulk,general_cargo,roro,tanker,cargo,total>` and `TRADE_VOLUME_EST\|vessel_type=…` (metric tons, model estimates) |
| `portwatch.country_monthly` | 50,760 (dry build; live deferred) | 180 economies × month 2019-01 → 2026-10 (October partial, flagged): `PORT_CALLS`, `IMPORT_VOLUME_EST`, `EXPORT_VOLUME_EST`, summed by the provider's ArcGIS query service over its daily port layer (5.86 M rows, not downloaded); `flags.port_days` |
| `eurostat.transport_rail_goods` / `_rail_pass` / `_road_goods` / `_iww_goods` | 1,334 / 1,322 / 2,436 / 790 | annual, EU + EFTA + candidates (2004/1999–2025) |
| `eurostat.transport_air_pass_m` | 29,389 | monthly passengers carried, total / national / international, 1993-01 → 2026-09 |
| `eurostat.transport_nuts2_sea_goods` / `_nuts2_air_pass` | 16,657 / 28,487 | NUTS 2 regions (codes verbatim; V4ECON `nuts:<code>` entities) |
| `nga.wpi`, `ourairports.airports` | 548 / 1,531 (live, new `ourairports.airports` version 2026-10-17) | country counts (A-INFRA); new versions registered by the 1.1.0 runs |

Honesty rules: values verbatim; a provider zero is kept as a reported zero, a blank is no row; PortWatch tonnages
are `estimate_kind: model_estimate` with the provider's definition quoted; monthly sums say who computed them
(`derived: sum`, "computed by the provider's ArcGIS query service") and that they cover only tracked ports.

## 4. Knowledge graph

| Source key | Entities | Statements (live) |
|---|---|---|
| `unece.unlocode` | 24,767 `trade_location` (`unlocode:<CC><LLL>`, locations with function 1 port or 4 airport) | 126,763 (dry build): `P17`, `P625`, `unlocode:function` (raw code + labels), `unlocode:status`, `unlocode:subdivision_code`, `unlocode:iata`, `unlocode:in_adm1` |
| `nga.wpi` | 2,951 `port` (`wpi:<portNumber>`) | 19,572 (dry build): `P17`, `P625`, `wpi:harbor_size`, `wpi:harbor_type`, `wpi:unlocode`, `wpi:at_unlocode`, `wpi:in_adm1` |
| `imf.portwatch` | 2,094: 28 `maritime_chokepoint`, 2,065 `port` (`portwatch:port<n>`, incl. 181 offshore `fso<n>` units of the ports database), 1 `data_series` | ~8,800 (dry build): `P625`, `P17`, `portwatch:locode`, `portwatch:at_unlocode`, `portwatch:in_adm1`, `infra:measured_by` |
| `ourairports.data` | **6,339 (live)** `airport` (`ourairports:<id>`: open large/medium airports or scheduled service) | **54,954 (live, 0 rejected)**: `P17`, `P625`, type, scheduled service, ident/ICAO/IATA, `ourairports:iso_region`, `ourairports:in_adm1` |

**Families** (`registry/kg/relations_v4.d/v6infra.yaml`): `unlocode:in_adm1`, `wpi:in_adm1`, `portwatch:in_adm1`,
`ourairports:in_adm1` → `located_in` (facility → `region_adm1`); `wpi:at_unlocode`, `portwatch:at_unlocode` →
`located_in` (port → UN/LOCODE location; identifiers only, never merged); `infra:measured_by` → `measured_by`
(chokepoint → `atlas:portwatch.chokepoints_daily`). Countries via `P17` (V4CORE's `located_in`). `part_of` is not
used: no provider published a containment between facilities (WPI regions are navigation groupings, not places).

**ADM1 placement, by basis** (dry builds of the full files, same code as the live runs):

| Facilities | published_code | point_in_polygon | nearest_polygon (≤ 0.05°) | no edge (reason) |
|---|---|---|---|---|
| UN/LOCODE (24,767) | 17,842 | 1,437 | 379 | 4,263 no subdivision & no coordinates; 610 offshore; 119 code not an NE subdivision & no coordinates; 117 polygon of another country |
| WPI ports (2,951) | — | 1,562 | 1,107 | 232 offshore beyond 0.05°; 50 polygon of another country |
| PortWatch ports (2,065) | — | 1,061 | 693 | 108 offshore; 22 other country; chokepoints: none (straits) |
| OurAirports (6,339) | 4,915 | 1,041 | — | 357 outside usable polygons; 26 other country |

`published_code` = the provider's ISO 3166-2 code names exactly one usable Natural Earth subdivision (UN/LOCODE
`Subdivision`, OurAirports `iso_region`); otherwise the REVA computation is used and labelled (`derived`, method,
Natural Earth raw sha256, distance for `nearest_polygon`). A subdivision of another country than the provider's is
never linked (border generalisation, disputed areas: e.g. ports listed under one country falling in a neighbour's
polygon at 1:10m).

**Example paths** (from the dry builds of the live files with the committed code; OurAirports ids are loaded):

* `wpi:48430` Abadan —`wpi:at_unlocode`→ `unlocode:IRABD` —`unlocode:in_adm1`→ … ; `wpi:48430` —`wpi:in_adm1`
  (point in polygon)→ `iso3166_2:IR-10` Khuzestan —P17→ Q794 Iran.
* `unlocode:DZALG` Alger (functions port, rail, road, airport…) —`unlocode:in_adm1` (published code `DZ-16`)→
  `iso3166_2:DZ-16` Alger —P17→ Q262 Algeria; `portwatch:<Algiers port>` —`portwatch:at_unlocode`→ `unlocode:DZALG`.
* `unlocode:FRMRS` Marseille —in_adm1 (published `FR-13`)→ `iso3166_2:FR-13` Bouches-du-Rhône; `unlocode:NLRTM`
  Rotterdam → `iso3166_2:NL-ZH` Zuid-Holland.
* `portwatch:chokepoint1` Suez Canal —`infra:measured_by`→ `atlas:portwatch.chokepoints_daily` (rows
  `TRANSITS|vessel_type=total @ provider_geo=chokepoint1`, daily); `portwatch:chokepoint6` Strait of Hormuz idem.

## 5. Live runs (dev DB `reva`, every write under `flock /tmp/reva-devdb.lock`)

| Source | Status | Requests / bytes | Result |
|---|---|---|---|
| `atlas.eurostat_transport` 1.0.0 | **completed** | 7 / 1.2 MB | 7 tables, 80,415 rows |
| `atlas.ourairports` 1.1.1 | **completed** (licence basis: repository LICENSE, the terms page was still in maintenance) | 5 / 27.7 MB (NE admin-1 bytes identical to V4CLIMATE's, stored once) | `ourairports.airports` 1,531 rows (new version); KG 6,339 airports, 54,954 statements, 0 rejected (load 479) |
| `atlas.nga_wpi` 1.1.0, `atlas.unlocode` 1.0.0, `atlas.imf_portwatch` 1.0.0 | refused before any request: "registered with different code; bump the version" — their digest includes `atlas_ghcnm`, which FIX4C changed (Natural Earth archived under its own spec) between my seed and the runs | 0 | versions bumped (1.1.1 / 1.0.1 / 1.0.1), my Natural Earth fetch now archives under `naturalearth.admin1_10m` like GHCN; a re-seed was queued behind ~7 seeds on the DB lock |
| `atlas.rcs_dem` (retry) | OSF back (HTTP 200) at ~20:30 UTC; **not run**: free disk fell to 4.3–4.6 GB (< 5 GB rule) | 0 | queued (§8) |
| re-runs of WPI / UN/LOCODE / PortWatch | **not run**: free disk < 5 GB | 0 | dry builds on the full live files (numbers in §3/§4) passed the atlas validator |

Raw archive added by V6INFRA so far: ~16 MB (Eurostat 1.2 MB, OurAirports 12.8 MB + licence files; Natural Earth
admin-1 identical to V4CLIMATE's archived copy, stored once). The deferred runs add ~13.5 MB (UN/LOCODE), ~12 MB
(PortWatch), 1.3 MB (WPI) and 40 MB (RCS-Dem): ~83 MB in total, within the 100 MB V6 cap.

Shared-file notes: `registry/sources/openfda_api.yaml` (V6DRUGS) had an unquoted `:` that broke the budget merge;
quoted (no content change). Budgets merged as budgets-1.3.30 (digest re-pinned); later merges by other agents
superseded it (budgets-1.3.36 at the OurAirports run).

## 6. Missing, and why

* **ITF/OECD transport** (global, beyond Europe): not built — §8.
* **Shipping routes / vessel tracks**: no open source; PortWatch daily port data per port (5.86 M rows) not
  downloaded (size); only country × month sums.
* **PortWatch history of `country_monthly`** is complete (2019→); per-port daily series would need ~600 MB of JSON.
* **UN/LOCODE KG** holds only port/airport locations (24,767 of 116,086); the other functions are in the counts.
* ADM1 for facilities whose coordinates fall offshore beyond 0.05° or in another country's polygon: none (counted).
* Rail/road networks (lines, terminals): no open global dataset with identifiers (OSM is ODbL, no cross-border
  identifiers) — only aggregated Eurostat statistics.

## 7. Owner decisions

1. **UN/LOCODE licence**: the site footer says CC BY 4.0 for UN/CEFACT standards, the site terms say personal,
   non-commercial, no redistribution, no derivative works. Loaded and flagged; decide whether REVA may keep/expose it
   (else: retract `unece.unlocode` statements and drop `unlocode.locations`).
2. **IMF PortWatch licence**: IMF terms page unreachable from the container (403); same review as A-IMF's datasets.
3. **OurAirports licence basis**: the repository `LICENSE` (Unlicense) was used while the terms page was in
   maintenance — confirm this is acceptable evidence (it is the provider's own data repository).
4. `nearest_polygon` (0.05° planar) for coastal ports: a REVA computation, labelled; accept or drop.

## 8. Commands to finish later

```
# remaining live runs (disk >= 5 GB free); seed first so the bumped versions are registered
flock /tmp/reva-devdb.lock uv run reva seed
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.unlocode
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.imf_portwatch
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.nga_wpi
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.rcs_dem
# fixtures (maintainer): capture the provider files, then
uv run python -I tests/fixtures/source/atlas.unlocode/cut_v6infra_fixtures.py <capture_dir> .
```

Follow-ups: ITF dataflows `OECD.ITF/DSD_TRENDS@DF_TRENDSFREIGHT|PASS|CONT|INFR`, `DSD_ST@DF_STFREIGHT|STPASS` through
the OECD connector pattern (capture `format=csvfilewithlabels` once to quote the codelists); V4LINK: Wikidata P1937
(UN/LOCODE), P238/P239 (IATA/ICAO) bridges would attach Wikidata port/airport items to these entities by identifier.
