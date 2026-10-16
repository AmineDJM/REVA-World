# V3 atlas — UN layer (agent A-UN)

Connector module `src/reva/adapters/connectors/atlas_un.py` (ATLASCORE atlas-source protocol:
`atlas_source()` + `atlas_ingest(run)`; all fetches through `run.get`, budget-metered live). Catalog
`registry/atlas/catalog/un.yaml` (domain `un`, 137 provider-declared concepts). Sources `registry/sources/{un_wpp,
un_desa_ims,un_sdg,unhcr_population,unsd_syb}.yaml`. Geo aliases `registry/atlas/geo_aliases/a_un.yaml`
(scheme `un_m49x`). Budget request `registry/atlas/budget_requests/a_un.yaml` (merged in budgets-1.3.1).
Tests: `tests/unit/test_atlas_un.py` (offline), `tests/integration/test_atlas_un_ingest.py` (db, fixture
mode through `reva.atlas.ingest.run_atlas_ingest`); fixtures `tests/fixtures/source/atlas.{un_wpp,un_ims,
un_sdg,unhcr,unsd_syb}` are excerpts of the real bytes retrieved 2026-10-15 (each manifest entry names
what was kept and the sha256 of the full file).

Run: `uv run python -m reva.atlas.ingest atlas.un_wpp [--dataset wpp2024_demo]` (also `atlas.un_ims`,
`atlas.un_sdg`, `atlas.unhcr`, `atlas.unsd_syb`).

## Sources and tables

| source (connector) | atlas table `<source>.<dataset>` | rows | crosswalk entities | years | raw |
|---|---|---:|---:|---|---|
| WPP 2024 (`atlas.un_wpp`, `un.wpp`) | `un_wpp.wpp2024_demo` — 54 demographic indicators, estimates + medium projection | 4,491,894 | 237 | 1950–2023 est., 2024–2100 proj. | Demographic_Indicators_Medium.csv.gz 16.6 MB |
| | `un_wpp.wpp2024_variants` — total population, 17 projection variants (not Medium) | 636,405 | 237 | 2024–2100 | TotalPopulationBySex.csv.gz 17.0 MB |
| | `un_wpp.wpp2024_age5` — population by 5-year age group × sex | 2,587,410 | 237 | 1950–2023 | PopulationByAge5GroupSex_Medium.csv.gz 29.9 MB |
| | `un_wpp.wpp2024_age5_proj` — same, medium projection | 2,692,305 | 237 | 2024–2100 | (same file) |
| UN DESA IMS 2024 (`atlas.un_ims`, `un.desa_ims`) | `un_desa.ims2024` — bilateral migrant stock destination × origin × sex | 672,719 | 235 (dest.) | 1990, 1995 … 2020, 2024 | xlsx 6.0 MB |
| UNHCR (`atlas.unhcr`, `unhcr.population`) | `unhcr.pop_bilateral` — 9 population types, origin × asylum | 207,639 | 195 (asylum) | 1951–2025 | 78 JSON pages, 42.9 MB |
| | `unhcr.pop_by_asylum` | 18,615 | 195 | 1951–2025 | |
| | `unhcr.pop_by_origin` | 16,471 | 214 | 1960–2025 | |
| UNSD SYB68 T02 (`atlas.unsd_syb`, `unsd.syb`) | `unsd.syb68_t02` — surface area, population, density | 7,727 | 237 | 2010, 2015, 2023, 2025 | CSV 2.1 MB (Windows-1252) |
| SDG API v5 (`atlas.un_sdg`, `un.sdg`) | `un_sdg.sdg_headline` — 44 headline series, all disaggregations | 354,639 | 248 | 1963–2026 | 61 JSON pages, 234.5 MB |

Live run 2026-10-15/16 into throwaway database `reva_aun_12794a0a` (objects under the scratchpad,
506 MB incl. raw + Parquet of every version): every request went through the budget ledger (`reserve_ingest` → metered → settle);
consumed: un.wpp 3 requests / 63.5 MB, un.desa_ims 1 / 6.0 MB, unsd.syb 1 / 2.1 MB, unhcr.population
78 / 42.9 MB, un.sdg 61 (45 reserved, 16 topped up) / 234.5 MB; the SDG run took 33 min (server pages 6–100 s).
WPP/IMS/SYB were re-run (connector 1.1.0, parser `atlas-un-2`: compact WPP flags) after the crosswalk
1.1.0 added TWN (code 158); the tables above are those versions (UNHCR/SDG: 1.0.0, `atlas-un-1`).
One intermediate re-run was OOM-killed by the shared container's memory cgroup while parsing the
5-year file (no partial table: registration is atomic); the WPP builder now reads only the needed
columns and builds one table at a time (peak ~3 GB RSS for the age tables). No request refused. The dev database `reva` was never touched.

Disk: well under the 1.5 GB per-agent budget (Parquet is small: e.g. `wpp2024_age5` 17 MB).

## Honesty decisions

* **Projections are separate series.** WPP years ≥ 2024 carry `|proj=medium` in the indicator and
  `flags.projection = true` (+ variant); variant tables carry `PopTotal|variant=<name>` and are all
  projections (PI variants flagged `probabilistic_interval`). The catalog gives projections their own
  concepts (`*_proj_medium`, `un.wpp.population_proj_<variant>`), so a best-available lookup for an
  estimate concept can never return a projected value.
* **Values as reported.** WPP in its own units (thousands, per 1,000, years…), IMS/UNHCR persons, SYB
  area in *thousand km²*. SYB thousands separators (`"130,094"`) are re-spelled as strict decimals with
  `flags.provider_text` kept (159 cells). SDG non-numeric values (e.g. `">99"`) go to `value_text`.
* **UNHCR string "0" is not a zero.** The API returns real figures as JSON integers and every cell
  without a figure as the string `"0"` (or `"-"`); no integer 0 occurs. Only integers are stored:
  903,660 string-"0" and 138,738 "-" bilateral cells were omitted and counted, never stored as 0.
  Years are end-year stocks; the running year (2026) is excluded (`latest_complete_year()`).
* **Incomplete inputs are not registered.** A UNHCR span with a missing page is refused; an SDG
  series with a missing page or a record count different from `totalElements` is left out of the table
  and named in its coverage note.
* **Bilateral encoding (for LINKS).** `provider_geo` = reporting side (IMS: destination M49; UNHCR:
  country of asylum ISO3), counterpart folded into the indicator (`migrant_stock|origin=<M49>|sex=T|M|F`,
  `refugees|origin=<ISO3>`), and `flags.counterpart_provider_geo / counterpart_iso3 / counterpart_label`.
  IMS origin/destination `900` = World (totals); regions are kept (iso3 null).
* **Aggregates kept, never dropped.** WPP has 319 aggregate locations (World, SDG/World Bank/ADB
  regions, income groups…) = 58 % of WPP rows, all with `iso3 = null` and `flags.loc_type / geo_label`.
* WPP single-age files were **not** ingested: two files of 62 + 67 MB → ~14 M atlas rows (over the
  ~5 M rows/table guidance; the 5-year table alone peaks at ~3.8 GB RSS while parsing in a shared
  15 GB container). 5-year groups are used, split in an estimate and a projection table (each < 5 M rows).
  The 75 MB `Demographic_Indicators_OtherVariants` file is not ingested either; total population of
  every variant comes from `TotalPopulationBySex`.

## Geography mapping (`un_m49x` = crosswalk M49 + 2 evidence-backed aliases)

* `412` → `XKX` (WPP row "Kosovo (under UNSC res. 1244)", ISO3_code XKX) and `830` → `CHI` (IMS
  "Channel Islands"; Guernsey/Jersey not listed separately there). Quoted with raw sha256 in the
  alias file.
* `158` → `TWN` comes from the crosswalk itself (1.1.0: ISO 3166-1 numeric of TWN; UNSD: "for strictly
  statistical purposes, the numerical code 158 can be used to represent this area"). WPP/IMS label it
  "China, Taiwan Province of China", the SYB "Other non-specified areas"; the SDG database has no 158 rows.
  CHN and TWN are never summed or split.
* Kept unmapped: UNHCR `UNK` (unknown), `XXA` (stateless), `TIB` (Tibetan) and every regional /
  income / development aggregate (WPP 318, IMS 52, SYB 34, SDG 56 provider geographies).

## Countries / territories missing in the World Bank that the UN layer covers

The crosswalk has 250 entities; 33 have no World Bank code. Covered here:

| table | non-WB entities covered |
|---|---|
| WPP 2024 (all 4 tables), SYB68 | 21: TWN Taiwan, AIA Anguilla, BES Bonaire/St Eustatius/Saba, BLM Saint Barthélemy, COK Cook Islands, ESH Western Sahara, FLK Falkland Islands, GGY Guernsey, GLP Guadeloupe, GUF French Guiana, JEY Jersey, MSR Montserrat, MTQ Martinique, MYT Mayotte, NIU Niue, REU Réunion, SHN Saint Helena, SPM Saint Pierre and Miquelon, TKL Tokelau, VAT Holy See, WLF Wallis and Futuna |
| IMS 2024 | 19 of the above (GGY/JEY reported together as CHI 830) |
| SDG headline | 31: the WPP list except TWN, plus uninhabited/remote territories for which custodians publish area-based series (ATF, BVT, CCK, CXR, HMD, IOT, NFK, PCN, SGS, SJM, UMI — e.g. forest share, PM2.5, marine KBA protection) |
| UNHCR by origin | 8 (AIA, COK, ESH, GLP, GUF, MTQ, NIU, VAT); as asylum country: AIA, GUF, MSR |

Not covered by WPP/IMS/SYB (uninhabited or without own figures): ALA Åland, ATA Antarctica, ATF, BVT,
CCK, CXR, HMD, IOT, NFK, PCN Pitcairn, SGS, SJM, UMI (the SDG table covers the last eleven for some
area-based series); ALA and ATA appear in no UN table. CHI appears only in IMS (WPP/SYB report GGY and
JEY separately); XKX is in WPP, SYB and SDG (alias 412) but not in IMS (included with Serbia) or UNHCR.

## Cross-checks against the World Bank (scratch analysis, WB API retrieved 2026-10-15)

* **Population 2023**: WPP `TPopulation1July` × 1000 vs WDI `SP.POP.TOTL` within 1 % for 177 of 216
  economies. Largest gaps: Moldova +24.8 %, Curaçao +19.9 %, US Virgin Islands −18.3 %, Albania +16.6 %,
  Togo +13.1 %, Qatar +12.2 % (WDI uses national figures for these; WPP its own estimates) — shown per
  source, never blended.
* **Area / density**: WPP does not publish area; WPP population ÷ `PopDensity` matches WDI *land* area
  `AG.LND.TOTL.K2` within 1 % for 161/215 economies (vs 102/215 for surface area), i.e. WPP density is
  per land area; largest gaps Bahamas, Monaco, Norway, Tuvalu, Serbia, Ethiopia, South Sudan.
* **SYB surface area vs WDI `AG.SRF.TOTL.K2` (2022)**, economies ≥ 5,000 km²: within 2 % for 145/167.
  SYB is rounded to whole thousand km² (13 small territories read 0). Large gaps worth a maintainer look:
  Greenland (SYB 2,166 k vs WDI 410 k — WDI apparently ice-free area), France (SYB 552 k metropolitan vs
  WDI 606 k), and WDI values that exceed the countries' total area and look like WDI data problems
  rather than definitional differences: Canada 15.6 M km², Norway 624.5 k, Dominican Republic 146.8 k,
  Croatia 88.1 k, UAE 98.6 k, Sweden 528.9 k (forwarded for A-WB / A-GEO).
* SYB area exists for 14 territories that have no WDI area (ESH 266 k, GUF 84 k, FLK 12 k, REU 3 k,
  GLP 2 k, MTQ 1 k; seven micro-territories 0).

## SDG headline series

44 series (goals 1–17) fetched in full with every disaggregation the database holds, except two heavy
series filtered server-side to totals (declared in `SDG_SERIES`, checked on every record): SH_HIV_INCD
`Age=ALLAGE`, SE_TOT_CPLR `Location=ALLAREA, Quantile=_T`. Release 2026.Q3.G.01. Indicator code =
`<series>|<dimension>=<code>|…` (sorted, incl. `Reporting Type` G = global/custodian, N = national);
1,430 non-numeric values kept as `value_text`, 4,589 empty records omitted; lower/upper bounds, nature
(C/CA/E/M/G), unit multiplier, footnotes and the custodian source text are in `flags`. No incomplete or
missing series in the live run.

Crosswalk entities per series (years): AG_LND_FRST 236 (1990–2025), EG_ACS_ELEC 218, EG_EGY_CLEAN 194,
EG_FEC_RNEW 231, EN_ATM_CO2 149, EN_ATM_PM25 237 (2010–2023), EN_LND_SLUM 219, ER_MRN_MPA 191,
GB_XPD_RSDV 155, IT_NET_BBND 217, IT_USE_ii99 225, NY_GDP_PCAP 210, SE_PRE_PARTN 197, SE_TOT_CPLR 169,
SG_GEN_PARL 193 (to 2026), SG_REG_BRTH 184, SH_ACS_DTP3 194, SH_ACS_UNHC_25 195, SH_DTH_NCOM 185,
SH_DYN_IMRT/MORT/NMRT 200, SH_H2O_SAFE 169, SH_HIV_INCD 146, SH_SAN_SAFE 161, SH_STA_ASAIRP 183 (2019 only),
SH_STA_BRTC 193, SH_STA_MORT 195, SH_STA_SCIDE 185, SH_STA_STNT 162, SH_STA_TRAF 195 (2021 only),
SH_STA_WAST 161, SH_TBS_INCD 214, SI_COV_BENFTS 216, SI_POV_DAY1 170 (1963–2025), SI_POV_NAHC 159,
SL_TLF_NEET 185, SL_TLF_UEM 217, SM_POP_REFG_OR 224, SN_ITK_DEFC 168, SP_DYN_ADKL 229, SP_DYN_MRBF18 144,
VC_DTH_TOTR 20 (only countries with conflict deaths), VC_IHR_PSRC 200.

Catalog: 40 `un.sdg.*` concepts point at the observed headline cell of each series (every dimension a
total code, or single-valued in the series, e.g. `Sex=FEMALE` for maternal mortality; `Reporting
Type=G`). Four series have no single headline cell (several age groups / education levels and no total):
SE_TOT_CPLR, SG_REG_BRTH, SL_TLF_UEM, SP_DYN_ADKL — in the table, no catalog concept (choosing an age
group would be an editorial pick; left to review).

SDG series are compiled by custodian agencies (WHO, UNICEF, ILO, UNESCO, ITU, IPU, World Bank…): they are
not independent of those agencies' own atlas tables, and the catalog lists only the SDG series.

## Requests to the lead / ATLASCORE

* Seed hook `register_atlas_connector_versions(conn, store, act)` (ATLASCORE's request) also covers the
  five `atlas.un_*`/`atlas.unhcr`/`atlas.unsd_syb` sources; nothing else needed from shared files.
* The platform live client timeout is `max(settings, 120 s)`; the SDG server answered single pages in
  10–90 s on 2026-10-15 and occasionally slower — pages that time out are typed errors and their series
  are left out (named). An `AtlasSourceSpec.timeout_seconds` (e.g. 300 s for `atlas.un_sdg`) would help.
* `tests/invariants/test_invariant_30_budgets.py`: `atlas_un.py` is not flagged (no client built; all
  requests via `run.get`). At the time of writing one test still fails on other agents' sources that are
  registered but not yet in the budgets policy (eia.opendata_bulk, geonames.cities, unctad.bulk,
  sscp.dataverse) — not A-UN keys (all five A-UN keys are merged).
