# V4 V4ENERGY — energy & natural resources: what, where, who, how much, at what price, history, future

Agent V4ENERGY, 2026-10-17. Builds on (does not duplicate) the V3 energy layer: EIA International Energy Data
(`atlas.eia_intl`, production / consumption / trade / coal reserves / capacity / generation of 219 countries), USGS
Mineral Commodity Summaries 2026 + Minerals Yearbook production tables (`atlas.usgs_minerals`, ~90 mineral commodities,
reserves by country), OWID energy (Energy Institute Statistical Review + Ember, in TWh, `owid.energy`) and the World
Bank WDI energy series — see `docs/dev/v3_a_resources.md`, `docs/dev/v3_a_qol.md`. What V4ENERGY adds: **prices**
(monthly since 1960), **monthly production by country and published forecasts kept apart**, and the **facility
layer** (35k power plants, 12k mines and mineral plants, with owners, operators and equity shares as published), plus
Wikidata specs for energy sources, producing sites, discoverers and producing companies.

## 1. Sources

| connector (version) | source key / licence | fetched (live, 2026-10-17) | loaded |
|---|---|---|---|
| `atlas.wb_pinksheet` 1.0.1 (`src/reva/adapters/connectors/atlas_pinksheet.py`) | `worldbank.cmo` — **CC BY 4.0** (the Commodity Markets page links "Data Access and Licensing" to `datacatalog.worldbank.org/public-licenses#cc-by`; checked on every run, fails closed) | `https://www.worldbank.org/en/research/commodity-markets` (links read from the page; the doc id changes monthly) + `CMO-Historical-Data-Monthly.xlsx` + `CMO-Historical-Data-Annual.xlsx`: 3 requests, **3.82 MB** | 6 atlas tables, **73,749 cells** (dev DB `reva`) |
| `atlas.eia_steo` 1.0.2 (`atlas_eia_steo.py`) | `eia.opendata_bulk` — U.S. public domain | `https://www.eia.gov/opendata/bulk/manifest.txt` + `STEO.zip` (Short-Term Energy Outlook, release 2026-10-06): 2 requests, **5.48 MB** | 2 atlas tables: **718,234 observed + 35,213 forecast cells** |
| `atlas.wri_gppd` 1.0.1 (`atlas_gppd.py`) | `wri.gppd` — **CC BY 4.0** (README sentence checked on every run) | GitHub `wri/global-power-plant-database` `output_database/global_power_plant_database.csv` (v1.3.0, 11.97 MB) + `DATABASE_VERSION` + `README.md`: 3 requests | KG: 34,951 entities, 291,310 statements (see §2) |
| `atlas.usgs_myb_facilities` 1.0.1 (`atlas_usgs_facilities.py`) | `usgs.sciencebase` — U.S. public domain / CC0 (item or FGDC metadata must say so; fails closed) | ScienceBase item `6a8490171ba49b3133cd8055` (doi:10.5066/P1KEQASH) JSON + `2024_MYBv3_metadata.xml` + `2024MYBv3_Facilities_table.csv` (7.72 MB): 3 requests | KG: 12,134 entities, 116,335 statements (see §2) |
| Wikidata specs `registry/kg/queries/energy/*.yaml` (5) | `wikidata.wdqs` / `wikidata.wbapi` — CC0, community-edited | not harvested (WDQS 429 with Retry-After 1000 s and wbapi cool-downs for the whole session; ~110 wbapi entity snapshots fetched to check ids, archived in `var/kg/objects`, ledger `reva_kg_v4energy`) | — (run commands in §7) |

Registry: `registry/sources/worldbank_cmo.yaml`, `registry/sources/wri_gppd.yaml` (EIA and ScienceBase keys already
registered by A-RESOURCES); budget request `registry/atlas/budget_requests/v4energy.yaml` (merged: worldbank.cmo 8/day,
wri.gppd 6/day, eia.opendata_bulk 8 → 12/day); geo aliases `registry/atlas/geo_aliases/v4energy.yaml` (scheme `gppd`
= ISO3 + `KOS` → XKX; `usgs_name` + Barbados, Cabo Verde, Haiti, Somalia — exact ISO short names, each with its row
evidence); KG predicates `registry/kg/predicates_energy.yaml` (19 `gppd:*` / `usgs_myb:*`); families
`registry/kg/relations_v4.d/v4energy.yaml`; Wikidata property requests `registry/kg/property_requests/v4energy.yaml`
(P2109 nominal power output, P2197 production rate, P729 service entry, P730 service retirement, P13333 GEM Wiki ID —
ids, labels and datatypes checked with wbgetentities); query domain `energy` added to `registry/kg/queries/_schema.json`.
Fixtures (captured live 2026-10-17, licence + live sha256 + excerpt description in each manifest):
`tests/fixtures/source/atlas.{wb_pinksheet,eia_steo,wri_gppd,usgs_myb_facilities}/` (500 KB total). Tests:
`tests/unit/test_atlas_v4energy.py` (21), `tests/integration/test_atlas_v4energy_ingest.py` (4, fixture mode end to
end on a throwaway DB, idempotent re-runs).

Raw archive used by V4ENERGY: ~33 MB (well under the 150 MB cap). The sources were fetched only through `run.get`
(budget-metered, archived before parsing); no HTTP client is built in these modules.

## 2. What was loaded (dev DB `reva`)

### Prices — `atlas.wb_cmo.*` (World Bank Pink Sheet)

| table | cells | series | periods |
|---|---:|---:|---|
| `monthly_prices` | 50,455 | 71 (crude oil average/Brent/Dubai/WTI, coal Australia/South Africa, natural gas US/Europe/Japan LNG + index, fertilisers, metals incl. aluminium, iron ore, copper, lead, tin, nickel, zinc, gold, platinum, silver, agriculture) | 1960M01–2026M09 |
| `monthly_indices` | 12,816 | 16 (Total, Energy, Non-energy, Metals & Minerals, Base metals, Precious metals, …; 2010=100) | 1960M01–2026M09 |
| `annual_prices_nominal` / `_real` | 4,116 / 4,184 | 69 / 70 (real = 2010 US dollars, deflated by the MUV index, as the Definitions sheet says) | 1960–2025 |
| `annual_indices_nominal` / `_real` | 1,056 / 1,122 | 16 / 17 | 1960–2025 |

Values verbatim (cell text, e.g. `80.400000000000006` stays as written); `…` = not available → no cell (6,416 monthly,
436 annual); 4 `#VALUE!` error cells → value-less cells `flags.missing = provider_error` (never zero); unit = unit
row + the sheet's own price-basis subtitle (`($/bbl) | monthly prices in nominal US dollars, 1960 to present`);
`provider_geo = global_market` (market prices at named delivery points — never a country). Header markers `*`/`**`
kept; the Description sheet's notes (`* denotes forecast series`, series replacements, corrections) are quoted in
the coverage note. Example: `wb_cmo.monthly_prices` "Crude oil, Brent" 2026M09 = 116.8 $/bbl; 1960M01 "Crude oil,
average" = 1.6.

### Monthly production, prices and forecasts — `atlas.eia_steo.*` (EIA Short-Term Energy Outlook)

4,420 series (1,478 annual, 1,473 quarterly, 1,469 monthly), release 2026-10-06. **`steo_observed`** holds only
periods ≤ each series' `lastHistoricalPeriod`; **`steo_forecast`** holds later periods (`estimate_kind =
projection`, `projection_upstream = {model: "EIA Short-Term Energy Outlook", release}`) and the 16 series that
declare no boundary (`estimate_kind = unknown_boundary`, e.g. `STEO.PAPR_VE.A` Venezuela liquids production) —
never mixed. Contents: Brent/WTI spot (monthly since 1990), Henry Hub ($/MMBtu since 1997), crude/liquids production
of 43 countries (OPEC members, Russia, US, Canada, Brazil, …; ISO3 from EIA's `geography`), world balances,
inventories, U.S. regional series (EIA's ISO 3166-2 state lists; lists longer than the atlas' 64-character bound
become `eia_geo:<sha16>` with the full list in `flags.eia_geography`). Example: `STEO.BREPUUS.M` 2026-09 observed,
2026-10 → forecast 111 $/bbl (flagged); `STEO.COPR_SA.M` = Saudi Arabia crude oil production, monthly.

### Facilities — KG

**GPPD (`wri.gppd`)**: 34,936 power plants in 167 countries (`gppd:<gppd_idnr>`, kind `facility`) + 15 fuel
categories (`gppd:fuel:<code>`, kind `energy_source`, GPPD's own vocabulary). Statements: country 34,934 (34,931 to
the country QID, 3 to `iso3:` nodes), primary fuel 34,936, other fuel 2,312, capacity MW 34,936 (point in time =
`year_of_capacity_data` when given), capacity-weighted commissioning year 17,447 (a decimal year, verbatim), coordinates
34,936, owner name 20,868 (string), WEPP id 16,234, data source 34,921 (record URL as qualifier), reported generation
GWh 59,786 (one per plant-year 2013-2019, qualifier `generation_data_source`). Not loaded: WRI's modelled
`estimated_generation_gwh_*` (model output, not reports).

**USGS Minerals Yearbook 2024 facilities (`usgs.sciencebase`)**: 12,027 mines / quarries / brine operations / plants
(`usgs_myb:<USGS Facility ID>`, kind `facility`) and 107 commodity entities (`usgs_myb:commodity:<code>`, kind
`commodity`), from 14,752 facility × commodity entries. Statements: country 12,027 (via the `usgs_name` scheme),
feature type 12,102, ADM1 *name* 11,912 (NGA GNS name + division type; no code is published, so it is not linked to
an ADM1 entity), location text 12,027, operator name 12,027, produces → commodity 14,752 (qualifiers: the five USGS
classification levels, unit, status, footnote, currentness date), annual capacity 11,223 (unit, USGS capacity code
with its metadata meaning — `E` "Reported value is estimated", `C` combined capacity with the Global Entry keys it is
shared with: never summed), equity owner names 11,679 (share percent as qualifier when given), data sources 18,605.
`-999` ("Value is not known or not available", read from the archived metadata) → no statement / no share: 3,529
capacities and 1,370 shares are Missing, never zero.

(Live load reports and final KG counts: §8.)

## 3. Relation families (`registry/kg/relations_v4.d/v4energy.yaml`)

| property | family | subtype |
|---|---|---|
| `gppd:country`, `usgs_myb:country` | `located_in` | country |
| `usgs_myb:produces` | `produces` | mineral_commodity |
| `gppd:primary_fuel`, `gppd:other_fuel` | `context` | primary_fuel / other_fuel |
| Wikidata `P137` (operator) | `owned_by` | operator (note: control of operations as Wikidata states it, not ownership) |

The Wikidata properties the energy specs rely on are mapped by V4CORE: P1056 → `produces`, P127 → `owned_by`
(share from P1107), P749/P355, P61 → `discovered_by`, P17/P131 → `located_in`, P186 → `made_of`, P527 → `has_part`.
Literal predicates (capacities, generation, owner-name strings) are deliberately unmapped: an owner name is not an
edge.

## 4. Examples (ids as loaded)

* `usgs_myb:CHL0121` "Carmen Lithium Chemical Plant" → `usgs_myb:country` → Q298 (Chile); → `usgs_myb:produces` →
  `usgs_myb:commodity:lithium` (Level 3 Refinery; capacity 210,000 metric tons, code E "Reported value is
  estimated"); ADM1 name "Antofagasta"; operator "SQM Salar S.A."; equity owner "Sociedad Química y Minera de Chile
  S.A. (SQM)" share 100 %.
* `usgs_myb:CHL0117`…`CHL0120` "La Negra I–IV plant" (Albemarle Corp.): lithium capacity 80,000 t with code `C,E` —
  combined across the four plants (`shared_capacity_with` lists the other Global Entry keys); REVA never adds them.
* `usgs_myb:COD0007` "KOV" → Q974 (DR Congo) → produces `usgs_myb:commodity:cobalt` (30,000 t); operator "Katanga
  Mining Ltd."; equity owners "Glencore plc" 75 %, "Société Générale des Carrières et des Mines SA (Gécamines)" 25 %.
* `gppd:WRI1002693` "BELLEVILLE" → Q142 (France) → primary fuel `gppd:fuel:nuclear`; 2,620 MW; reported generation
  16,485.667 GWh (2016), 14,443.614 GWh (2017).
* `gppd:WRI1030636` "ABQ PP" → Q851 (Saudi Arabia), gas, 162 MW, owner name "Saudi Aramco" (a string: Saudi Aramco's
  company identity lives with V4INDUSTRY/Wikidata — linking the two needs an identifier GPPD does not publish).
* Prices and quantities of the same commodities: `wb_cmo.monthly_prices` "Crude oil, Brent", "Natural gas, US",
  "Copper", "Nickel" (the Pink Sheet has no lithium or cobalt price); `eia_steo.steo_observed`
  `STEO.COPR_SA.M` (Saudi crude production, monthly); V3 `eia.intl_petroleum` `INTL.57-1-TBPD.A` (annual production,
  all countries); V3 `usgs.mcs2026` lithium reserves by country.
* Chemistry link (by identifiers, once the Wikidata specs run): petroleum Q22656 → P279 fossil fuel Q12748;
  P527/P186 composition edges → items that V4LINK bridges to `chebi:` / `pubchem_cid:` through Wikidata's own P683 /
  P662 (`registry/kg/identity_rules_v4.yaml`, pack `chemistry`); elements lithium Q568, cobalt Q740, nickel Q744,
  copper Q753, uranium Q1098 are the same items V4CHEM's `element:<Z>` entities are bridged to.

## 5. What is missing, and why

| gap | why | alternative / next step |
|---|---|---|
| **Energy Institute Statistical Review** consolidated dataset (physical units, refinery capacity, uranium, critical minerals) | `www.energyinst.org` answers **403** to non-browser clients (also in V3); terms not readable | OWID redistributes the main EI series (TWh) — already loaded (`owid.energy`); owner decision (§6) |
| **Global Energy Monitor** trackers (oil & gas extraction, coal mines, integrated power — CC BY 4.0) | downloads require a request form with personal contact details; no public file URL. Not worked around (the GEM map data hosted elsewhere were not used) | owner decision (§6). Bridges are ready: V4INDUSTRY loads GLEIF's LEI→GEM ids as `gem:<E…>`; Wikidata P13333 (GEM Wiki ID) requested; GEM rows carry the wiki URL |
| Oil / gas **fields** with discovery dates, discoverers, operators, owners | only Wikidata states them openly; WDQS throttled all session | `registry/kg/queries/energy/producing_sites_by_product.yaml` (+ mines, power stations, companies, energy sources) — run commands §7 |
| Oil / gas **proved reserves** by country | EIA international data stopped publishing them; EI stopped; OPEC ASB terms unchecked | (V3 gap, unchanged) coal reserves (EIA) and mineral reserves (USGS MCS) are covered |
| **EIA International Energy Outlook** projections to 2050 (`IEO.zip`, 7.9 MB, editions 2017-2023, 7 scenarios) | region is only in the series name / a REVA-uncoded series token; mapping regions would be a REVA codebook | build from EIA's category tree (each category names its region) — next connector |
| World Bank commodity **price forecasts** | published as PDF only (`CMO-April-2026-Forecasts.pdf`) | none machine-readable |
| Daily spot prices (EIA `PET.zip`, 55 MB) | size vs disk; monthly Brent/WTI/Henry Hub already in STEO and the Pink Sheet | add `PET.zip` / `NG.zip` spot series later |
| ADM1 for facilities | GPPD gives coordinates only; USGS gives an ADM1 *name* (no code) | a point-in-polygon with geoBoundaries would be a REVA derivation (flag `computed_by: reva`) — owner decision |
| Company identity of owners / operators | GPPD and USGS publish names only; REVA never matches by name | Wikidata P127/P137 (when harvested) give QIDs; GEM entity ids via GLEIF |
| Crude oil composition (assays) | producer assays (Equinor, ExxonMobil, BP) are per-crude spreadsheets under proprietary terms | Wikidata P527/P186 composition edges (classes, not shares) |
| "Why a company decided to produce" | none of these sources states reasons; Wikidata P1056 states *that*, not *why*; causal statements (P828/P1542) are CAUSWD's attributed claims | company filings (SEC EDGAR, V4INDUSTRY) — text, not loaded |

## 6. Owner decisions

1. **GEM trackers**: accept the GEM download form (personal contact details) — or ask GEM for a stable URL — so the
   oil & gas extraction, coal mine and integrated power trackers (CC BY 4.0, with GEM unit / owner ids) can be
   fetched through `run.get`. Key encoding for `gem_wiki:` (V4LINK note) must be agreed at the same time.
2. **Energy Institute**: the Statistical Review site blocks non-browser clients; obtain an allowed URL / permission,
   or keep OWID's redistribution.
3. **Fuel vocabulary equivalence**: GPPD's 15 fuel categories and USGS's 107 commodity names are kept as provider
   vocabularies; a curated mapping to Wikidata items (e.g. GPPD "Coal" → Q24489) would be REVA-made
   (`agent_declared_equivalence_not_human_reviewed`) — not done.
4. **Facility ADM1 by coordinates** (see §5).

## 7. Commands to finish later (bigger machine / when WDQS answers)

```bash
df -h /                                   # >= 5 GB free
flock /tmp/reva-devdb.lock uv run reva seed
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.wb_pinksheet      # monthly refresh
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.eia_steo          # monthly (STEO release ~6th)
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.wri_gppd          # static (GPPD unmaintained since 2022)
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.usgs_myb_facilities
# Wikidata (throwaway harvest DB, then the governed import into reva):
uv run python -m reva.kg.harvest merge-property-requests                     # P2109 P2197 P729 P730 P13333
uv run python -m reva.kg.harvest run registry/kg/queries/energy --db reva_kg_v4energy --planned-wdqs 10 --planned-wbapi 1500
uv run python -m reva.kg.harvest run registry/kg/queries/energy --db reva_kg_v4energy --via api --planned-wbapi 1500  # WDQS down: mines + power stations only
flock /tmp/reva-devdb.lock uv run reva kg import-harvest --from-db reva_kg_v4energy --sources 'wikidata*'
```

## 8. Live load results

(filled in below after the loads)
