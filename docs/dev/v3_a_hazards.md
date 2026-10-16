# V3 A-HAZARDS — natural disasters as dated, located events with impacts and NCEI-coded triggers

Agent A-HAZARDS, 2026-10-16. Spec: `docs/roadmap/v3_causality.md` (A-HAZARDS row), interfaces
`docs/dev/v3_atlas_interface.md` §7 and `docs/dev/v3_causal_interface.md`. INFORM hazard *scores* (A-GEO,
`atlas_hazards.py`) are modelled exposure indices; this agent adds the *events*.

## Files

| File | What |
|---|---|
| `src/reva/adapters/connectors/atlas_noaa_hazards.py` | atlas sources `atlas.noaa_hazards` (NCEI) and `atlas.noaa_hazards_wikidata` (WDQS P1886) |
| `registry/sources/noaa_hazards.yaml` | source `noaa.ncei_hazards`: rights evidence, API, year convention, field-definition quotes |
| `registry/atlas/catalog/hazard_events.yaml` | 18 concepts `hazard_events.*` (domain `hazard_events`, theme `environment`), all COMPUTED |
| `registry/atlas/geo_aliases/a_hazards.yaml` | scheme `ncei_hazards` (base iso3): 213 NCEI country names -> crosswalk (169 exact, 44 manual), 18 left unmapped on purpose |
| `registry/atlas/budget_requests/a_hazards.yaml` | `noaa.ncei_hazards`: 600 requests/day, 60 MB/day (one ~240-request run + one retry) |
| `tests/fixtures/source/atlas.noaa_hazards/` | 16 verbatim NCEI responses for the run window 2018 (8 list pages + 8 code lists, 0.48 MB) |
| `tests/fixtures/source/atlas.noaa_hazards_wikidata/` | WDQS P1886 response trimmed to the 12 fixture volcanoes (22 of 1,853 rows; note in the manifest) |
| `tests/unit/test_atlas_hazards_events.py` | 18 offline tests (years, paging checks, sums, KG, claim rules, aliases, same_as, catalog) |
| `tests/integration/test_atlas_hazards_events_ingest.py` | fixture run in the throwaway DB: atlas table, KG, T1 claims + grades, idempotence, same_as |
| `tests/integration/test_atlas_hazards_events_live.py` | `-m "live and db"`: one budget-metered live run (budget request overlaid until merged) |

## Source and access (verified 2026-10-16)

NCEI/WDS Global Historical Hazard Database through the HazEL **hazard-service API v1**
(`https://www.ngdc.noaa.gov/hazel/hazard-service/api/v1`), documented by its own OpenAPI 3.1 file
`/hazel/hazard-service/docs/api.json` (sha256 `efd460d2…`). Lists are paged; `itemsPerPage` above 200 is refused
(`"must be less than or equal to 200"`), so a full run is ~240 GETs (earthquakes 34 pages, tsunami events 16,
runups 176, volcanoes 5, plus 8 code-list descriptors). No authentication, `access-control-allow-origin: *`.

| Database | DOI | Records (2026-10-16) | Range |
|---|---|---|---|
| Global Significant Earthquake Database | 10.7289/V5TD9V7K | 6,697 | 2150 BC – 2026 |
| Global Historical Tsunami Database: sources | 10.7289/V5PN93H7 | 3,138 | 2000 BC – 2026 |
| … runups (where effects were observed) | 10.7289/V5PN93H7 | 35,129 | |
| Global Significant Volcanic Eruptions Database | 10.7289/V5JW8BSH | 903 | 4360 BC – 2026 |

**Rights.** US Government work (NOAA): not subject to copyright in the US (17 U.S.C. 105). The NCEI ISO metadata of
the three databases (G012153, G02151, G10147; page sha256s in the source YAML) say "Access to this dataset is
unrestricted", "Fees: Free online", and ask to cite the DOI. Use constraints are a no-warranty disclaimer only.

**Field definitions** are quoted from the HazEL application help (`main.4768c122.js`, sha256 `174dc6c3…`): totals are
"total number of deaths from the earthquake and secondary effects (eg Tsunami)"; damage "in millions of U.S.
dollars. The dollar value listed is the value at the time of the event"; description codes 0–4 from the archived
descriptor lists (`effect/people`: 1 = "Few (~1 to 50 people)" … 4 = "Very Many (~1001 or more people)"); NCEI
"Volcano Number" = GVP's permanent volcano number.

**Years.** NCEI writes B.C. years negative without a year 0 (HazEL: "Year, example: 200 B.C. = -200"; no record has
year 0). Stored as astronomical years: NCEI `y < 0` → `y + 1` (the 2150 BC Jordan earthquake is year −2149 in the
atlas and `-2149-01-01` precision 9 in the KG). Month/day give month/day precision; an impossible day (1854-02-29,
tsunami 3437) keeps month precision with a `date_note`. NCEI does not state the calendar of pre-1582 dates; they are
stored as published.

**Not used.** Record comments (prose with literature references, one request per record), tsunami deposits and
marigrams, the volcano-locations list (volcano attributes come from the eruption records). **EM-DAT** (CRED,
UCLouvain): skipped — the data portal needs a registered account and CRED "distributes the data in open access for
non-commercial use" (emdat.be, 2026-10-16); Wikidata has P14355 (EM-DAT disaster number) but without EM-DAT data
there is nothing to link. **GDACS**: skipped (optional in the mission): near-real-time model-based alerts, not a
historical event record; its terms page was not found at `/About/termsofuse.aspx` (404).

## Outputs

### Atlas table `ncei_hazards.country_year` (COMPUTED)

Per NCEI country × event year (astronomical), separately for each database — never added across hazards, because
NCEI's "total" effects of one hazard already include its secondary hazards:

| Indicators | Rule |
|---|---|
| `eq_events`, `ts_events` (validity ≠ −1), `ts_events_valid34` (validity 3/4, NCEI's default query), `ts_runups`, `vo_eruptions` | count of NCEI records |
| `eq_deaths`, `eq_deaths_total`, `eq_injuries`, `eq_damage_musd`, `eq_houses_destroyed`, `ts_deaths`, `ts_deaths_total`, `ts_runup_deaths`, `vo_deaths`, `vo_deaths_total`, `vo_injuries`, `vo_damage_musd`, `vo_houses_destroyed` | sum of the published number over the records that publish it |

* Only country-years with ≥ 1 record; a sum cell exists only when ≥ 1 record publishes the number (no imputation,
  no filled zero). Flags of every cell: `computed_from` (database + DOI), `aggregation`, `records`,
  `records_with_value` / `records_without_value`, `ncei_country_names` (verbatim), `geo_basis`, `year_basis`,
  `raw_pages` when several pages contribute. Selector: endpoint, field and NCEI record ids.
* Geography: NCEI's `country` is the *present-day* country of the location (also for ancient events). Tsunami
  sources are counted in the source country; `ts_runup_*` in the country where the runup was observed (with the
  year of the source event). Mapped names are aggregated per crosswalk entity (`provider_geo` = its key, so "SOLOMON
  IS." and "SOLOMON ISLANDS" never make two rows for SLB); unmapped names stay with `iso3 = null` and their
  upper-cased NCEI name (oceans/seas, "USA TERRITORY", "UK TERRITORY", "KOREA", "SAINT MARTIN (FRENCH AND DUTCH
  TERRITORIES)", "AFRICA, NORTH COAST").
* Alias evidence (`a_hazards.yaml`): "exact normalised name match" against crosswalk label / ISO / World Bank / UN /
  Comtrade names (169), or "manual" with the reason (44: e.g. `RUSSIA`→RUS, `TURKEY`→TUR, `AZORES (PORTUGAL)`→PRT
  because NCEI names the sovereign, `CANARY ISLANDS`→ESP, `SABA (DUTCH TERRITORY)`→BES; maintainer review pending).

### KG (source key `noaa.ncei_hazards`)

| Entity | kind | Statements |
|---|---|---|
| `ncei_hazards:earthquake:<id>` | `earthquake` | `P585` date, `P625` coordinates, `ncei:location_name`, `ncei:country_name` (verbatim) + `ncei:country` (country QID when mapped), `ncei:region_code`, `ncei:area`, `ncei:magnitude` (one per NCEI scale field, qualifier `scale`), `ncei:focal_depth_km`, `ncei:mmi_intensity`, effects, `ncei:record_flags`, `ncei:associated_tsunami` / `ncei:associated_eruption` |
| `ncei_hazards:tsunami:<id>` | `tsunami` | date, coordinates, location/country, `ncei:tsunami_cause` and `ncei:tsunami_validity` (code + NCEI description), `ncei:warning_status`, `ncei:max_water_height_m`, `ncei:runup_count`, `ncei:deposit_count`, intensity/magnitudes, `ncei:oceanic_tsunami`, effects, `ncei:associated_earthquake` / `ncei:associated_eruption`, `ncei:runup_country` (QID) or `ncei:runup_country_name` per runup country with runup count and published runup deaths |
| `ncei_hazards:eruption:<id>` | `volcanic_eruption` | date (NCEI "Event Date" = peak of activity), coordinates, location/country, `ncei:vei`, `ncei:eruption_agent` (letters + NCEI descriptions), effects, `ncei:volcano`, associations |
| `ncei_hazards:volcano:<NCEI location id>` | `volcano` | `ncei:gvp_volcano_number` (external id), `ncei:previous_volcano_number`, `P625`, location, country, elevation, morphology, status, last-eruption code |

Effects (`ncei:deaths`, `missing`, `injuries`, `damage_million_usd`, `houses_destroyed`, `houses_damaged`) are
quantities with qualifier `scope` = "from the <hazard>" or "from the <hazard> and secondary effects" (NCEI
`…Total`), plus `ncei:<effect>_description` (NCEI code + decoded scale text). Impacts are attributes of the event,
never causal claims. Every statement cites its raw page sha256 and selector (endpoint, page, index, record id,
fields) and "stated in" the database DOI. NCEI associations are context edges (qualifier `nature`: "NCEI association
between records; context, not a causal claim"). `P585`/`P625` use WDHARVEST's property definitions; `ncei:*` are
registered by the connector. Kinds are dataset kinds for EVENTS' dataset-kind mapping (no `P31` class QIDs typed
here: the EVENTS taxonomy owns verified class QIDs).

A complete run retracts statements NCEI no longer has (`removed_at_source`); a windowed run (`--option
min_year=… max_year=…`) never retracts.

### Coded triggers (T1 claims, `reva.causal.load_dataset_claims`) {#coded-triggers}

`source_key noaa.ncei_hazards`, dataset `tsunami_sources`, relation `coded_trigger`, tier T1, codebook "NCEI/WDS
Global Historical Tsunami Database: tsunami source event record" (`https://doi.org/10.7289/V5PN93H7`). A claim
exists only when the NCEI **tsunami source record identifies its source event by id** and its **cause code names
that hazard**:

| Link field on the tsunami record | Cause codes that make it a claim (NCEI `tsunami/causes`) | Claim |
|---|---|---|
| `earthquakeEventId` | 1 Earthquake, 2 Questionable Earthquake, 3 Earthquake and Landslide, 4 Volcano and Earthquake, 5 Volcano, Earthquake and Landslide | `ncei_hazards:earthquake:<id>` → `ncei_hazards:tsunami:<id>` |
| `volcanoEventId` | 4, 5, 6 Volcano, 7 Volcano and Landslide | `ncei_hazards:eruption:<id>` → `ncei_hazards:tsunami:<id>` |

* Other combinations (e.g. an eruption linked to a tsunami coded 1 "Earthquake", an earthquake linked to a tsunami
  coded 9 "Meteorological" or 0 "Unknown") stay **context** (`ncei:associated_*`) and are counted in the run report
  (`*_link_context_only_cause_code_<n>`).
* Tsunami records NCEI marks validity −1 ("Erroneous entry") give no claim. Other validity levels are claims; the
  selector records `eventValidity` (and `causeCode`, the link field) so answers can show "questionable tsunami".
* Earthquake ↔ eruption associations have no direction in NCEI: context only.
* The codebook terms are `cause_code:<n>` with the verbatim NCEI help text and descriptor label, e.g.
  `Tsunami Cause Code. Valid values: 0 to 11. The source of the tsunami: 3 = "Earthquake and Landslide"`; the
  selector also points at the reciprocal record (`tsunamiEventId` on the earthquake/eruption), checked at build time
  (all links were reciprocal on 2026-10-16).
* Grading (CAUSCORE rules v1) in the fixture run: 19 claims, all **B** (`tier.T1`, `corroboration.single`); 18
  `temporal.overlapping` (same day), 1 `temporal.consistent` (Kadovar eruption 2018-01-05 → tsunami 2018-02-09).

### Wikidata `same_as` (volcanoes only)

`atlas.noaa_hazards_wikidata` (budget key `wikidata.wdqs`, 1 WDQS query) reads every non-deprecated `P1886`
(Global Volcanism Program ID) statement. NCEI's "Volcano Number" *is* the GVP number, so
`ncei_hazards:volcano:<id> --ncei:same_as--> Q…` (source key `wikidata.wdqs`, community-edited) is written when
exactly one Wikidata item carries that number and exactly one NCEI volcano has it; ambiguous numbers (e.g. 332010
Kīlauea on 6 items, 262000 on Krakatoa and Anak Krakatau) are reported, not linked. Qualifiers give the evidence,
the Wikidata statement id and rank. **Events get no `same_as`:** no Wikidata property holds NCEI hazard event ids
(WDQS property search 2026-10-16: P1886 GVP, P3196 USGS earthquake ID, P5182 JMA, P4207 INGV, P6419 GEOFON, P14355
EM-DAT, none NCEI), and NCEI list records carry none of those ids. Never by name or date.

## Live run (throwaway database, 2026-10-16)

Run through `run_atlas_ingest(mode="live")` (budget overlay of the request, policy `budgets-1.3.6+a_hazards_request`)
into a per-session throwaway database, then `atlas.noaa_hazards_wikidata` live on `wikidata.wdqs` (the WDQS call
waited ~6 min for the machine-wide WDQS cool-down set by other agents' 429s; the pytest session timed out on that
wait, the Wikidata step was finished on the same throwaway database and the database was then dropped).

| | Result |
|---|---|
| NCEI requests | 239 consumed of 260 reserved, 20.4 MB, 0 refused; 239 raw artifacts archived |
| Records | 6,697 earthquakes, 3,138 tsunami sources, 35,129 runups, 903 eruptions |
| Atlas `ncei_hazards.country_year` | 19,332 rows, 183 crosswalk countries, years −4359 … 2026 (134 rows BCE); unmapped kept: USA TERRITORY (178 rows), UK TERRITORY (30), PACIFIC OCEAN (16), ATLANTIC OCEAN (14), 10 other seas/regions, "(NO COUNTRY)" (4) |
| Rows per indicator | eq_events 4,503 · eq_deaths 1,746 · eq_deaths_total 1,646 · eq_injuries 980 · eq_houses_destroyed 706 · eq_damage_musd 525 · ts_events 2,175 · ts_events_valid34 1,291 · ts_deaths 268 · ts_deaths_total 640 · ts_runups 2,909 · ts_runup_deaths 278 · vo_eruptions 732 · vo_deaths 373 · vo_deaths_total 395 · vo_injuries 94 · vo_houses_destroyed 48 · vo_damage_musd 23 |
| KG | 11,017 entities (6,697 earthquakes, 3,138 tsunamis, 903 eruptions, 279 volcanoes), 174,629 statements, 0 rejected; 115 country statements not written (unmapped NCEI name), 17 (mapped entity without a country QID) |
| Coded triggers | 2,186 T1 `coded_trigger` claims (2,026 earthquake→tsunami, 160 eruption→tsunami), 0 rejected, 0 unknown entities. Context only: earthquake links with cause code 6 (14), 0 (9), 9 (6), 7 (3), none (2); eruption links with cause code 1 (23), 3 (3), 8 (2). Skipped as "Erroneous entry": 64 + 3 |
| Grades (rules v1) | B 2,171 (2,150 `overlapping`, 21 `consistent`); **E 15** `temporal.inconsistent`: NCEI dates the source event after its tsunami, e.g. Vesuvius eruption 1348 79-08-25 vs tsunami 3093 79-08-24, eruption 5324 (NCEI 197 BC) vs tsunami 3473 (198 BC), earthquake 9212 1899-01-29 vs tsunami 4862 1899-01-17 — kept and shown, never corrected |
| Wikidata `same_as` | 1 WDQS request (0.87 MB): 1,853 P1886 rows; 276 NCEI GVP numbers → 231 `same_as`, 44 ambiguous (number on several items), 1 not on Wikidata |

Spot checks (atlas): 2004 tsunami deaths by runup country IDN 167,540 · LKA 35,322 · IND 26,047 · THA 8,212 · SOM 572;
Japan 2011 eq_deaths 1,484, eq_deaths_total 18,433, ts_deaths 18,423, ts_runup_deaths 15,950, ts_runups 5,542 — the
"total" series overlap by design and are never added.

## Open items

* ATLASCORE: merge `registry/atlas/budget_requests/a_hazards.yaml` (`noaa.ncei_hazards` 600/day). Until then
  invariant 30 (`test_budget_keys_are_registered_sources_and_cover_every_connector`) lists `noaa.ncei_hazards`
  among the unbudgeted keys, like the other new V3 sources.
* EVENTS: map dataset kinds `earthquake`, `tsunami`, `volcanic_eruption` (and `volcano` as a place) in
  `registry/events/taxonomy.yaml`; places are `P625` + `ncei:country` (present-day country, as coded by NCEI).
* Maintainer review: the 44 manual geo aliases; the NOAA public-domain reading (standard for NOAA data).
* Possible later additions: per-record comments/references (`/{id}/info`, ~10.7k requests: would allow T2 citations),
  tsunami deposits, GVP eruption list (Smithsonian terms to check).
