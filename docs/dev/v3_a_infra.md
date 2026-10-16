# V3 — A-INFRA: physical networks between countries (cables, air routes, airports, space, ports)

Agent A-INFRA, 2026-10-16. The owner wants countries "connected like neurons"; this layer adds the **physical**
connections: submarine cables, air routes, launch services (whose rockets carry whose satellites), plus the
per-country stock of airports, ports, launch sites and satellites. Everything is a **REVA-derived count of provider
records**, labelled as such (`flags.derived` + exact `formula`), with staleness and licence flags; a missing row
means "no such record in the source", never zero.

## 1. Sources: licence, access, decision

| Source | Access | Licence (checked 2026-10-16) | Decision |
|---|---|---|---|
| **TeleGeography Submarine Cable Map** | former GitHub repo `telegeography/www.submarinecablemap.com` (HTTP 404 now); live site JSON API `submarinecablemap.com/api/v3/...` | repo stated **CC BY-NC-SA 3.0 (non-commercial)**; the live API states no licence | **skipped** (NC / unstated); recorded in `atlas_cables.SKIPPED` and the table rights |
| **Wikidata** submarine communications cable items (`Q506572` + subclasses `Q361047` transatlantic, `Q2395922` transatlantic telegraph) | `Special:WhatLinksHere/<class>` + `Special:EntityData/<QID>.json` (the action API and WDQS were answering 429 to the shared egress all day), under WDHARVEST's machine-wide lock | CC0 (community-edited, not verified) | **ingested**: `atlas.wikidata_cables` → `wikidata.submarine_cables` |
| **OpenFlights** airports / airlines / routes | GitHub raw `jpatokal/openflights/master/data/*.dat` + data page `openflights.org/data.php` (licence + staleness checked every run) | **ODbL 1.0** + DbCL. Obligations: attribute OpenFlights; a derived database made public must be under ODbL (share-alike). Repo `LICENSE` (AGPL-3.0) covers code only | **ingested**: `atlas.openflights` → `openflights.routes_2014`, **STALE**: provider warning "route data ceased providing updates in June 2014 ... historical value only" on every row |
| **OurAirports** | `davidmegginson.github.io/ourairports-data/{airports,countries}.csv` + terms page | **Public domain** ("All data is released to the Public Domain") | **ingested**: `atlas.ourairports` → `ourairports.airports` (current, daily dump) |
| **GCAT** (J. McDowell) | `planet4589.org/space/gcat/tsv/...` (orgs, sites, launch, satcat) + index page | **CC BY 4.0** ("Licence: CC-BY-4.0", index page; marker checked every run) | **ingested**: `atlas.gcat` → `gcat.launches`, `gcat.payloads` (release 1.8.8, data update 2026-10-15) |
| **UNOOSA** Register of Objects Launched into Outer Space | online search index only, no bulk file, no open licence statement | not open | **skipped** (GCAT covers the same objects, with owner states) |
| **NGA World Port Index** (Pub. 150) | `msi.nga.mil/api/publications/world-port-index?output=csv` (the legacy `download?key=...UpdatedPub150.csv` URL answers 403) | U.S. Government work (17 U.S.C. 105); not stated in the file → `licence_status: us_government_work_not_stated_in_file` | **ingested**: `atlas.nga_wpi` → `nga.wpi` |
| UNCTAD liner shipping connectivity (LSCI / bilateral LSBCI) | — | — | A-TRADE's (`unctad.lsbci`): **referenced, not duplicated** — the shipping country-pair network is there |
| ENTSO-E power interconnections | transparency platform (registration, restrictive terms) | restricted | **skipped**; no open global alternative found (OSM power lines are ODbL but carry no cross-border pair semantics without heavy GIS work) |

## 2. What was built

| File | Purpose |
|---|---|
| `src/reva/adapters/connectors/atlas_cables.py` | `atlas.wikidata_cables` (source_key `wikidata.wbapi`, shares WDHARVEST's budget) |
| `src/reva/adapters/connectors/atlas_flights.py` | `atlas.openflights`, `atlas.ourairports` (one module, dispatch on `run.spec.name`) |
| `src/reva/adapters/connectors/atlas_space.py` | `atlas.gcat` |
| `src/reva/adapters/connectors/atlas_ports.py` | `atlas.nga_wpi` |
| `src/reva/adapters/connectors/atlas_infra_common.py` | shared helpers: fetch+archive through the run, `RowBuilder` (refuses duplicates, never emits a count < 1), `derived()` flags, `Partner` (dyadic folding), GCAT TSV reader |
| `registry/sources/{openflights,ourairports,gcat,nga_wpi}.yaml` | source registrations with rights (ODbL share-alike, staleness, US-gov work status) |
| `registry/atlas/catalog/infrastructure.yaml` | 17 provider-declared country concepts (air 7, maritime 2, space 6, telecom 2), per-concept `theme` (economy / science) |
| `registry/atlas/geo_aliases/a_infra.yaml` | schemes `gcat` (110 GCAT state codes, each with its `orgs.tsv` row as evidence), `openflights_name` (19 airport-country spellings), `ourairports` (ISO2 + XK), `nga_wpi` (ISO2) |
| `registry/atlas/budget_requests/a_infra.yaml` | `openflights.data` 10/day, `ourairports.data` 6, `gcat.planet4589` 10, `nga.wpi` 3 (+29 requests, +108 MB/day) |
| `tests/unit/test_atlas_infra.py` (12), `tests/integration/test_atlas_infra_ingest.py` (2, `db`) | offline tests; fixtures `tests/fixtures/source/atlas.{openflights,ourairports,gcat,nga_wpi,wikidata_cables}` (1 MB total, trimmed excerpts of real bytes, each manifest says how) |

Protocol: `atlas_source()` + `async atlas_ingest(run)`; every byte fetched with `run.get` and archived with
`run.archive` before parsing; no HTTP client is built in these modules (invariant 30's client check passes; its
"registered sources == pinned budget keys" check lists the four new keys until ATLASOPS merges the request).
Truncation guards (e.g. ≥ 50,000 routes, ≥ 50,000 GCAT launches) refuse a short download; licence markers refuse a
changed licence (rights event, never assumed).

## 3. Tables and indicators

All counts: `flags.derived = "count"`, `flags.formula`, `flags.absent_rows` (absent = no record), raw file in
`raw_sha256`/`selector`, look-up files in `flags.inputs`.

**Country-pair (dyadic) rows for LINKS** — partner folded as `|partner=<ISO3>` and `flags.partner_iso3` =
`flags.counterpart_iso3` (an unmapped partner is folded as `|partner_code=<provider code>` with
`partner_iso3: null`, kept but never a link):

| Table | Indicator | Direction | Period | Meaning |
|---|---|---|---|---|
| `wikidata.submarine_cables` | `CABLES_SHARED\|partner=X` | undirected (`flags.undirected`; emitted for both countries) | retrieval day | Wikidata cable items whose P17 lists both; `flags.cables` = QID, label, service year, revision, raw sha |
| `wikidata.submarine_cables` | `CABLE_FIRST_SERVICE_YEAR\|partner=X` | undirected | retrieval day | earliest service year (P729, else P571) of the shared cables: "connected by cable since" (`derived: min`) |
| `openflights.routes_2014` | `ROUTES_OPERATED\|partner=X` | origin → destination | 2014-06 | airline × airport-pair records, codeshares excluded |
| `openflights.routes_2014` | `ROUTE_AIRPORT_PAIRS\|partner=X`, `ROUTE_AIRLINES\|partner=X` | origin → destination | 2014-06 | distinct airport pairs / airlines (incl. codeshares) |
| `gcat.payloads` | `PAYLOADS_LAUNCHED_FROM\|partner=X` | owner state → launch-site state (`flags.direction`) | launch year | satellites of one state launched from another state's site |

**Country rows**: `ourairports.airports` (`AIRPORTS|type=<t>`, `AIRPORTS_OPEN`, `AIRPORTS_SCHEDULED_SERVICE`,
`AIRPORTS_CLOSED`; snapshot), `openflights.routes_2014` (`ROUTES_OPERATED_INTL_OUT`, `ROUTES_OPERATED_DOMESTIC`,
`ROUTE_DEST_COUNTRIES`, `ROUTE_AIRPORTS`), `gcat.launches` (`ORBITAL_LAUNCH_ATTEMPTS|by=site_state`,
`ORBITAL_LAUNCH_SUCCESSES|by=site_state`, `ORBITAL_LAUNCH_ATTEMPTS|by=agency_state`,
`ORBITAL_LAUNCH_SITES_USED|by=site_state`; per year), `gcat.payloads` (`PAYLOADS_CATALOGUED|by=owner_state` per
year, `PAYLOADS_STATUS_IN_ORBIT|by=owner_state` snapshot of the GCAT data update — includes dead satellites),
`nga.wpi` (`PORTS_LISTED`, `PORTS_LISTED|harbor_size=L|M|S|V`; snapshot), `wikidata.submarine_cables`
(`CABLES_LISTED`, `CABLE_PARTNER_COUNTRIES` snapshot, `CABLES_ENTERING_SERVICE` per year).

Definitions worth knowing:

* GCAT *orbital launch attempt* = `LaunchCode` category `O` or `D` and outcome `S`/`F`/`U` (pad explosions `E`
  and aborts `A` excluded); success = `S` (GCAT codes a success fraction < 0.75 as `F`). A site's state is the
  state GCAT attributes **that site record** to for its period (Baikonur = USSR `NIIP-5` before 1989, Russia
  `GIK-5` after; Kourou = French Guiana `GUF`; Kwajalein = `TTPI`).
* WPI facility columns are not counted (`loContainer` is `U` = unknown for 2,905 of 2,951 ports: a count would
  read "no container port" where the source says "unknown").

## 4. Country codes (kept, never guessed)

* **GCAT**: 110 current-state codes mapped (`F`→FRA, `UK`→GBR, `J`→JPN, `GUF`→GUF, `BGN`→BGR...). Unmapped and
  listed: `SU` (entity_qid Q15180), `CSSR`/`CSFR` (Q33946), `HKUK`, `TTPI`, intergovernmental organisations
  (`I-ESA`, `I-INT`, `I-EUM`...), composite agencies (`multi:AU+US`...), `ESCN`/`ESIB` (Canary/Balearic Islands:
  part of Spain, whose code `E` is mapped — the atlas refuses two provider geographies on one country).
* **OpenFlights**: provider's own `countries.dat` (name → ISO2) + 19 airport-country spellings with evidence;
  `Netherlands Antilles` (dissolved) and `West Bank` (one airport, Jerusalem Atarot) unmapped.
* **OurAirports**: ISO2 + `XK`→XKX; `XP` (Paracel Islands, disputed) and `ZZ` (unknown) unmapped.
* **WPI**: ISO2; `QM` Midway, `QW` Wake, `XU` Johnston Atoll unmapped (parts of UMI; not merged).
* **Wikidata cables**: P17 QIDs → ISO3 via WDHARVEST's `registry/entities/country_qids.json`; other QIDs (historical
  states, territories without a crosswalk entry) keep `provider_geo = <QID>`, `iso3` null.

## 5. Coverage (full provider files of 2026-10-16, through the same builders and the atlas validator)

| Table | Rows | Crosswalk countries | Unmapped geos | Period |
|---|---|---|---|---|
| `openflights.routes_2014` | 14,392 (13,515 dyadic) | 224 | Netherlands Antilles | 2014-06 (stale) |
| `ourairports.airports` | 1,531 | 245 | XP, ZZ | 2026-10-16 |
| `gcat.launches` | 1,719 | 22 launching states | SU, TTPI, I-ESA, I-ELDO, ESCN, multi-agency (5), unknown site/agency (2) | 1957–2026 |
| `gcat.payloads` | 2,811 (1,413 dyadic) | 107 owner states | SU, CSSR, CSFR, HKUK, ESIB, 10 IGOs | 1957–2026 + snapshot |
| `nga.wpi` | 548 | 167 | QM, QW, XU | 2026-10-16 |
| `wikidata.submarine_cables` | see §6 | see §6 | see §6 | snapshot + service years |

Source totals: OpenFlights 67,663 route records (571 with an airport not in `airports.dat`, counted in the report,
never assigned), 4,558 directed country pairs; GCAT 7,406 orbital launch attempts (1 with an unknown site code
`TYSC?`), 27,852 payloads (all joined to a launch site), 1,437 owner→site-state pair-years; OurAirports 86,251
records; WPI 2,951 ports.

Countries with no row in any of the four non-cable tables: ALA, BVT, CHI, PCN, SJM, TKL (OurAirports lists
Svalbard airports under NO).

### Sample country-pair links

* Air (OpenFlights, June 2014, operated routes): GBR→ESP 491, ESP→GBR 485, DEU→ESP 332, MEX→USA 247, ITA→DEU 171,
  TWN→CHN 166. France reaches 115 countries by direct flights (most of any country), UK 99, Germany 97, USA 93.
* Space (GCAT, payloads by owner state → launch-site state, all years): GBR→RUS 408 (OneWeb on Soyuz), USA→RUS 259,
  USA→GUF 238, GBR→USA 238, USA→IND 223, USA→NZL 136 (Rocket Lab), FRA→GUF 89. Algeria: RUS 2002 (1), IND 2010 (1),
  IND 2016 (3), CHN 2017 (1), CHN 2026 (2).
* Launch states 2025 (attempts by site state): USA 181, CHN 92, NZL 17, RUS 17, GUF 7, IND 5, JPN 4, AUS 1.
* Cables: see §6.

## 6. Live ingest

LIVE_RESULTS_PLACEHOLDER

## 7. Honesty notes and limits

* OpenFlights routes are **stale** (June 2014): use them as the 2014 air network, not today's. No other open,
  current, global route dataset was found (OurAirports has no routes; commercial schedules are proprietary).
* OurAirports and Wikidata are community-edited; GCAT is one researcher's compilation; none is verified by REVA.
* WPI is a navigation list, not a census of ports (USA 657 ports, India 7).
* Wikidata cable coverage is partial (only part of the world's ~600 systems have items; some, e.g. TAT-8, lack
  P17). A missing cable pair is "no Wikidata item lists both", not "no cable".
* GCAT `PAYLOADS_STATUS_IN_ORBIT` counts objects still in orbit, including dead satellites.

## 8. Requests

1. **ATLASOPS/ATLASCORE**: merge `registry/atlas/budget_requests/a_infra.yaml` (4 new keys). Until merged, the live
   runs of `atlas.openflights`, `atlas.ourairports`, `atlas.gcat`, `atlas.nga_wpi` are refused (fail closed) and
   invariant 30's registered-sources test lists these keys (with other agents' unmerged keys).
2. **Lead**: seed `register_atlas_connector_versions` picks up the five new atlas sources automatically.
3. **LINKS / LINKS2**: dyadic rows as in §3 (`flags.partner_iso3`; cables `flags.undirected = true`, both directions
   emitted — dedupe as one undirected link; air and space are directed). Suggested link types: `submarine_cable`
   (undirected, weight = cables, since = `CABLE_FIRST_SERVICE_YEAR`), `air_routes_2014` (directed, weight = operated
   routes, stale), `launch_service` (owner → site state, weight = payloads, per year).
4. **WDHARVEST**: optional — P559 (terminus) / P609 (terminus location) on cable items could recover landing
   countries for items without P17 (TAT-8) through the location's P17.
