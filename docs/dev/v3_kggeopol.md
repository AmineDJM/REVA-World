# V3 KGGEOPOL — geopolitics with history and evidence

Agent KGGEOPOL, 2026-10-15. Spec: `docs/roadmap/v3_world_graph.md`. Status: connectors, fixtures, offline and
throwaway-DB tests done; full-scale load of the real files into a throwaway DB done (§4.4); budgets merged by
ATLASCORE; Wikidata specs written and schema-valid (execution by WDHARVEST).

## 1. What exists

| Piece | Path |
|---|---|
| Shared helpers (GW/COW → ISO3 crosswalk with policy, KG adapter + loader, countrycode fetch) | `src/reva/adapters/connectors/atlas_geopol_common.py` |
| UCDP connector | `src/reva/adapters/connectors/atlas_ucdp.py` (`atlas.ucdp`) |
| UNGA votes connector | `src/reva/adapters/connectors/atlas_unga_votes.py` (`atlas.unga_votes`) |
| CShapes connector | `src/reva/adapters/connectors/atlas_cshapes.py` (`atlas.cshapes`) |
| Wikidata query specs (11) | `registry/kg/queries/geopolitics/*.yaml` |
| Atlas catalog (12 concepts) | `registry/atlas/catalog/geopolitics.yaml` |
| Sources | `registry/sources/{ucdp,unga_votes,cshapes}.yaml` |
| Budget request | `registry/atlas/budget_requests/kggeopol.yaml` |
| Fixtures (trimmed slices of the real 2026-10-15 downloads, hash-pinned) | `tests/fixtures/source/atlas.{ucdp,unga_votes,cshapes}/` (92 KB, 120 KB, 300 KB) |
| Tests | `tests/unit/test_atlas_geopol.py` (15, offline), `tests/integration/test_atlas_geopol_ingest.py` (3, throwaway DB, fixture mode), `tests/integration/test_atlas_geopol_live.py` (3, `live`) |

The three modules follow the atlas source platform (`docs/dev/v3_atlas_interface.md` §7): `atlas_source()` +
`async atlas_ingest(run)`; every request goes through `run.get` (budget-metered SafeHttpClient live,
`FixtureHttp` offline); raw bytes are archived (`run.archive`) **after** parsing succeeds (nothing is archived on
schema drift); atlas tables via `run.register`; KG records via `reva.kg.model.load_batch` (bounded batches,
dataset predicates registered with `register_properties`) when the `kg` schema exists.
Run: `uv run python -m reva.atlas.ingest atlas.ucdp|atlas.unga_votes|atlas.cshapes [--fixture]`.

## 2. Sources, access, licences

| Source | Access (checked 2026-10-15) | Licence |
|---|---|---|
| **UCDP 26.1** (ACD conflict-year 1946–2025, BRD conflict-year 1989–2025, Country-Year Organized Violence 1989–2025, Actor list) | **UCDP API now needs a token** (`ucdpapi.pcr.uu.se` → HTTP 401 "API token required, x-ucdp-access-token"); the official CSV zips on `ucdp.uu.se/downloads` need none and are used (4 zips, 40–170 KB). Actor CSV is Windows-1252 (declared, decoded strictly). | **CC BY 4.0** (downloads page) |
| **Voeten et al. UNGA votes** (Harvard Dataverse `doi:10.7910/DVN/LEJUQZ`) | The dataset was renamed "UNGA Ideal Points"; versions 34–39 **no longer contain the raw votes**. Version **33.0** (2024-06-24) is the last one with `UNVotes-1.RData` (file id 10295576, 8.7 MB, R data frame `completeVotes`); old-version files remain downloadable by id (redirect to the Dataverse S3 bucket). The connector pins v33.0, reads the version record (licence must be CC0, file md5 must match) and parses the RData with A-POL's bounded reader `atlas_rdata.py`. | **CC0 1.0** |
| **CShapes 2.0** (1886–2019, ETH Zürich ICR) | The authors' **CRAN package `cshapes` 2.0** ships the full dataset (independent states *and* dependencies with owner, capital, border-definition flag; GW and COW codings) as simplified TopoJSON (4.2 MB). The ICR shapefile has GW independent states only (no status/owner) and the ICR host resets proxy connections intermittently. | **CC BY-NC-SA 4.0** (data; package code GPL). **Non-commercial, share-alike**: derived tables/statements carry the same licence; exclude from any commercial deployment. Flagged in `registry/sources/cshapes.yaml`. |
| countrycode 1.9.0 (CRAN, `codelist_panel`) | fetched and archived by every run (0.54 MB) | GPL-3 package; code lists are facts |

## 3. Country codes (GW / COW → crosswalk), never guessed

`StateCodeCrosswalk.resolve(system, code, date)` (`atlas_geopol_common.py`), in this order:

1. **Policy windows naming a current entity** (documented continuations): GW 678 → YEM from 1990-05-22, GW/COW
   816 → VNM from 1976-07-02, COW 345 → SRB from 2006-06-05, UCDP's own 345 → SRB after 2006 (UCDP keeps 345
   where GW uses 340), GW/COW 347 → XKX from 2008-02-17, **GW microstates** (GW numbers absent from countrycode,
   from Gleditsch's `microstatessystem.dat`, sha256 recorded) — note **GW 970 = Kiribati but COW 970 = Nauru**.
2. **Historical-state QIDs** from A-HISTECON's `registry/entities/historical_states_qids.json` (dated, each checked
   against Wikidata with evidence; COW codes, applied to the GW codes that denote the same polity): Soviet Union
   Q15180 (1922–1991), Russian Empire, German Reich periods, West/East Germany, Yugoslavia/FRY/Serbia-Montenegro,
   Czechoslovakia, North/South Yemen, North/South Vietnam, Ottoman Empire, Qing/ROC China, Zanzibar, Taiwan ...
   → `iso3 = null`, `entity_qid = Q…` in atlas rows; the QID is the KG subject.
3. **Policy windows for historical states without QID** → `iso3 = null` with the state's name.
4. **countrycode panel** (country-year); outside the panel's years, the nearest year only when the code maps to
   one entity across all years.

Anything else stays unmapped (`iso3 = null`, provider code verbatim, listed in the manifest). Atlas validation
also refuses two provider codes mapping to one country in the same period (COW 260 after unification is
therefore not DEU).

## 4. Outputs and coverage (full real data, 2026-10-15)

### UCDP (`ucdp.downloads`)
| Output | Coverage |
|---|---|
| atlas `ucdp.orgvio_cy` — 26 UCDP columns verbatim (sb/ns/os/total deaths best/low/high, dyad counts) | 185,432 cells, **194 crosswalk countries**, 1989–2025; 8 unmapped codes kept (USSR 1989–91 → `entity_qid` Q15180, GDR/FRG 1989–90, Czechoslovakia, Yugoslavia/FRY to 2006, North/South Yemen 1989–90, Taiwan). UCDP-coded zeros kept, flagged with the codebook caveat. |
| atlas `ucdp.acd_cy` — **computed** counts of distinct conflicts per country-year: located / at war intensity / primary party / secondary party | 6,835 cells, 161 countries, 1946–2025 (only years with ≥ 1 conflict; no fabricated zeros); flags list the conflict ids and the method. |
| KG | 303 conflicts (`ucdp:conflict:<id>`), 594 non-state actors (`ucdp:actor:<id>`), 10,159 statements: primary/secondary party, location, non-state side (each as runs of years with the exact years in qualifiers), incompatibility, type (history kept when it changes), start date (UCDP precision), episode ends, intensity per year (2,816), battle-related deaths per year with low/high bounds (1,650, 1989–2025). |

### UNGA votes (`harvard.dataverse.unvotes`)
| Output | Coverage |
|---|---|
| atlas `unga.votes` — country × roll call × position (yes/no/abstain/absent) | 1,027,498 cells, **193 countries**, 6,551 roll calls, **1946-01-01 → 2024-06-04**; 255,406 "not a member" rows counted, not stored. |
| atlas `unga.rollcalls` — description, symbol, date, issue codes, provider totals | 6,551 (issue-coded: CO 1,027 · HR 1,063 · ME 1,110 · DI 1,162 · NU 922 · EC 835) |
| atlas `unga.vote_shares` — **computed** n_yes/n_no/n_abstain/n_absent and `share_yes = yes/(yes+no+abstain)` per country × year/decade × issue (co, hr, me, nu, di, ec, all) | 431,489 cells; every row says `computed_from`, the formula and Voeten's issue-code caveat; votes grouped by the state the code denotes **at the vote date** (COW 365 = USSR → Q15180 until 1991-12-20, then RUS). |
| KG | 6,551 roll-call entities `unga:rcid:<rcid>`; 930,813 vote statements (`unga:voted_yes|no|abstain`, day precision; yes 742,383 · no 70,441 · abstain 117,989) + symbol/date/issue statements. |

Provider defects handled explicitly (never silently): 336 conflicting (roll call, country) cells (COW 345 has three
rows per roll call 1999–2005: the old seat "absent" twice plus FR Yugoslavia's vote) → no position,
`missing: unreliable`, provider values listed; 87 identical duplicates collapsed; 127 misaligned 2023–24 rows (they
carry another roll call's description/date) flagged `inconsistent`; one 2023 row without ccode kept as `cow:NA`. None
of them feeds shares or KG statements.

**What the data shows for "US position on decolonisation, 1950s–60s"** (computed from the file; CO = Voeten's
keyword-based colonialism code, all CO roll calls incl. paragraph/amendment votes):

| Decade | USA yes / no / abst. → share_yes | UK | France | USSR (Q15180) | India |
|---|---|---|---|---|---|
| 1950s | 32 / 29 / 14 → **0.43** | 0.28 | 0.40 | 0.69 | 0.65 |
| 1960s | 30 / 47 / 41 → **0.25** | 0.20 | 0.13 | 0.81 | 0.79 |
| 1970s | 27 / 88 / 63 → **0.15** | 0.20 | 0.18 | 0.96 | 0.93 |

These are counts and ratios from the dataset, not an interpretation; a question-answering layer must show them
with the caveats (issue coding, roll-call selection, absences excluded) and per-resolution evidence
(`unga.votes` + `unga.rollcalls`, KG `unga:voted_*`).

### CShapes 2.0 (`cshapes.cran`)
| Output | Coverage |
|---|---|
| atlas `cshapes.polities_gw` / `polities_cow` — per polity period: status, owner (dependencies), capital, borders-defined | 710 / 805 polity periods (GW: 365 independent, 219 colony, 62 protectorate, 39 occupied, 23 mandate, 2 N/A); 2,475 / 2,813 cells; 171 / 192 crosswalk countries; 84 / 86 codes unmapped (historical states and CShapes territory codes such as 1201, 3461). Dependency rows map to the state the territory later became, flagged `iso3_meaning` (borders may differ). |
| dataset `geo.cshapes.polygons` (`register_dataset`) | 1,515 rows (GW + COW), GeoJSON of the authors' simplified topology, coordinates rounded to 1e-4°, 4.4 MB Parquet (zstd). |
| KG | 270 territory entities `cshapes:gw:<code>`; `cshapes:polity_period` 710 (day precision), `cshapes:ruled_by` 343 (dependency → ruling state, e.g. Algeria → France until 1962-07-04), `cshapes:shares_border_with` 642 dated land-border intervals (shared TopoJSON arcs of polities valid at the same time, consecutive periods merged; 306 pairs on 2019-06-01), `geopol:corresponds_to_iso3` 262. |

### Wikidata specs (`registry/kg/queries/geopolitics/`, all 11 valid against `_schema.json`)
`international_organisations` (seed: organisations with ≥ 3 state members via P463 ∪ direct instances of
international organization Q484652 / intergovernmental organization Q245065 / military alliance Q1127126 /
supranational union Q1335818 — class QIDs verified with `wbgetentities` 2026-10-15), `state_memberships` (P463 with
P580/P582, P2868 role, P1534 end cause; values split over the seeded organisations), `organisation_member_parts`
(P527 org → state), `diplomatic_relations` (P530, 250 countries), `historical_diplomatic_relations`,
`shared_borders` (P47 with P518), `historical_shared_borders`, `disputed_territories` (Q15239622),
`territory_claims` (P1336 with dates), `military_alliances` (Q1127126 subclasses), `conflict_participation` (P607).
All properties used are already allow-listed. **Structured sanctions are not specified**: Wikidata has no
consistently modelled sanctions class/property (candidate class QIDs did not verify); a gap, not a guess.
`harvest try` was not completed: first it failed in WDHARVEST's harvest-DB setup (`atlas.bmr@1.0.0 registered
with different content`, since fixed by its owner), then WDQS answered 429 with `Retry-After: 1000` machine-wide
for most of the session; I stopped my try requests rather than add load. The specs are schema-valid but their
row bounds and run times have not been tried against WDQS.

### 4.3 Live ingest (budget-metered, `pytest -m "live and db" tests/integration/test_atlas_geopol_live.py`, 2026-10-15)

3 passed in 718 s on a per-session throwaway database: UCDP 5 requests / 0.87 MB, CShapes 2 / 4.72 MB, UNGA 4 /
9.27 MB (budget policy `budgets-1.3.4`, no refusal). Registered: `atlas.ucdp.orgvio_cy` 185,432,
`atlas.ucdp.acd_cy` 6,835, `atlas.cshapes.polities_gw` 2,475, `polities_cow` 2,813, `geo.cshapes.polygons` 1,515,
`atlas.unga.votes` 1,027,498, `atlas.unga.rollcalls` 6,551, `atlas.unga.vote_shares` 431,489 rows; KG 10,159 +
1,957 + 950,213 statements inserted, **0 rejected**. The live bytes equal the bytes analysed in §4 (identical counts).

### 4.4 Full-scale load (real 2026-10-15 bytes, throwaway DB `reva_kggeopol_*`, dropped afterwards)

| Source | Time | Peak RSS | KG statements (rejected) | `kg.statement` size after |
|---|---|---|---|---|
| UCDP | 31 s | 0.7 GB | 10,159 (0) | 9.6 MB |
| CShapes | 43 s | 0.8 GB | 1,957 (0) | 11.7 MB |
| UNGA votes | 898 s | 2.8 GB | 950,213 (0) | 819 MB |

The UNGA KG load is the expensive part (0.82 GB of the ≤ 6 GB KG target). If that is too much,
`vote_statements(..., issues_only=True)` limits KG vote statements to issue-coded roll calls (the atlas keeps every
vote). A first attempt that materialised every row as a dict was OOM-killed at 3.8 GB (cgroup); the pipeline now
registers each table and releases it before the next, builds tables column-wise (`AtlasRows`) and streams KG
statements to `load_batch` in 100k batches. KG cross-check: US votes on CO-coded roll calls dated 1960–1969 in
`kg.statement` = 30 yes / 47 no / 41 abstain, identical to `unga.vote_shares` (1960s decade).

## 5. KG conventions (for KGCORE / LINKS / NLQA / UI3)

* Entities: Wikidata QIDs when the state resolves (country QIDs from `registry/entities/country_qids.json`
  **when WDHARVEST writes it — not present yet**, historical QIDs from A-HISTECON's file); otherwise state-code
  entities `gw:<code>` / `cow:<code>` (same scheme as A-HISTECON's `atlas_cow`) linked by
  `geopol:corresponds_to_iso3` (dated string literal). Dataset entities: `ucdp:conflict:<id>`, `ucdp:actor:<id>`,
  `unga:rcid:<rcid>`, `cshapes:gw:<code>`.
* 21 dataset predicates (`GEOPOL_PROPERTIES`), registered by the loader: `ucdp:*`, `unga:*`, `cshapes:*`,
  `geopol:corresponds_to_iso3`, with fr/en labels and inverse names. No causal predicate.
* Every statement: `statement_id` = stable dataset key, `raw_sha256` of the archived file, and a reference
  `{raw_sha256, selector}` locating the row(s) in the raw file; `source_revision` = dataset version.
* Until `country_qids.json` exists, states are `gw:`/`cow:` entities (e.g. the USA's UNGA votes are on `cow:2`);
  re-running after it exists re-keys subjects to QIDs (statement ids are per dataset row, so the new subject is a
  new revision of the same statement key).

## 6. Gaps and alternatives

* UNGA votes after 2024-06-04: not in Voeten v33.0. Alternatives: UNGA-DM database (Fjelstul, Hug & Kilby,
  unvotes.unige.ch) and the UN Digital Library voting data — licence/format not yet checked; not ingested.
* UCDP fatalities exist only from 1989; 1946–1988 has conflict-years and intensity levels (computed counts).
* CShapes ends 2019-12-31; area per period is not in the CRAN TopoJSON (the ICR shapefile has it); not computed
  from simplified polygons on purpose.
* Sanctions: no structured Wikidata source (above); GSDB (Global Sanctions Data Base) is the obvious next source
  (licence to check).

## 7. Requests to other agents / lead (not edited by me)

* **ATLASCORE**: budgets merged (`cshapes.cran` 8, `harvard.dataverse.unvotes` 12, `ucdp.downloads`); invariant 30
  no longer lists my keys.
* **WDHARVEST**: run `registry/kg/queries/geopolitics/**`; write `registry/entities/country_qids.json`.
* Shared code: A-HISTECON's `atlas_cow.py` imports my `StateCodeCrosswalk` (API kept stable); my modules import
  A-POL's `atlas_rdata.read_rdata_frame`.
