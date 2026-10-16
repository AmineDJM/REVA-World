# V3 — A-COUPS: coups and political leaders (events with dates for every country)

Agent A-COUPS, 2026-10-16. Spec: `docs/roadmap/v3_causality.md`; platform: `docs/dev/v3_atlas_interface.md` §7;
causal contract: `docs/dev/v3_causal_interface.md` (CAUSCORE). Status: three atlas sources declared through the
atlas source protocol (`atlas_source()` / `atlas_ingest(run)`, fetched only through `run.get` / `run.post`,
budget-metered live), catalog, byte-faithful fixtures, offline + throwaway-DB tests, and one live run into a
per-session throwaway database (`reva_test_*`, dropped afterwards; never the dev DB). See §6 for what the live
run could and could not reach.

| file | role |
|---|---|
| `src/reva/adapters/connectors/atlas_coups.py` | source `atlas.coups` (Powell & Thyne coup list -> atlas + KG coup events) |
| `src/reva/adapters/connectors/atlas_leaders.py` | sources `atlas.archigos` (Archigos 4.1 -> atlas + KG leaders, spells, removals, Archigos coups + T1 claims + optional Wikidata `same_as`) and `atlas.reign_leaders` (REIGN-format leader list 1945-2026 -> atlas + KG); bounded Stata 117-119 `.dta` reader |
| `registry/sources/{powell_thyne_coups,archigos,reign_leaderlist}.yaml` | source entries (rights, hosts, evidence) — keys `powell_thyne.coups`, `archigos.leaders`, `reign.leaderlist` |
| `registry/atlas/catalog/regime_events.yaml` | 11 provider-declared concepts (theme `politics`) |
| `registry/atlas/budget_requests/a_coups.yaml` | 6 / 18 / 6 requests per day |
| `tests/unit/test_atlas_coups.py` (21), `tests/integration/test_atlas_coups_ingest.py` (4, `db`), `tests/integration/test_atlas_coups_live.py` (1, `live and db`, deselected) | |
| `tests/unit/atlas_coups_fixtures.py` | builds the fixtures from the real captures (documented subsets: CSV lines and `.dta` rows copied byte for byte, header/map rewritten; workbook rows with original texts and row numbers) |
| `tests/fixtures/source/atlas.{coups,archigos,reign_leaders}/` | 92 / 248 / 100 kB (incl. KGGEOPOL's countrycode slice) |

No `registry/atlas/geo_aliases/a_coups.yaml`: all three providers use Correlates of War codes, resolved date-aware by
KGGEOPOL's `StateCodeCrosswalk` (iso3 pre-filled, historical states kept with their QID); no provider-specific code
needed an alias.

Run: `uv run python -m reva.atlas.ingest atlas.coups | atlas.archigos [--option wikidata=1] | atlas.reign_leaders
[--fixture]`. Live only after ATLASCORE merges the budget request (the live test overlays it, like A-CULTURE's).

## 1. Sources researched (licences checked 2026-10-16)

| Source | Access | Licence / terms | Result |
|---|---|---|---|
| **Powell & Thyne**, Global Instances of Coups 1950-present | `jonathanmpowell.com/coups/` links the current list `wp-content/uploads/2026/08/pt_20260829.csv` (V2026.08.29, 497 events, 20 kB); the former `www.uky.edu/~clthyn2/coup_data/` answers 403 to everyone | **none stated**; cite Powell & Thyne (2011) JPR 48(2) | **ingested** (code + fixtures + offline/DB tests); **live blocked**: the WordPress.com host answers the platform's Python/httpx client with a 403 "Checking your browser… Javascript required" challenge while plain `curl` gets 200 (TLS-fingerprint bot filter). Not circumvented (no browser impersonation, no third-party mirror). |
| **Archigos 4.1** (Goemans, Gleditsch & Chiozza), 1875-2015 | `www.rochester.edu/college/faculty/hgoemans/Archigos_4.1_stata14.dta` (Stata 118, 2.9 MB, no content type); codebook `Archigos_4.1.pdf` (sha256 `6be0ec5c…`) | **none stated**; "We would appreciate it if users would cite this article" (data.htm) | **ingested live** |
| **REIGN** (Bell, Besaw & Frank, One Earth Future) | REIGN stopped in August 2021 (GitHub `OEFDataScience/REIGN.github.io`, `REIGN_2021_8.csv` 34 MB, `leader_list_8_21.csv`); an updated **REIGN-format `leaderlist.xlsx` (2026-04-14)** is linked on `jonathanmpowell.com/coups/` | none stated (citation requested); no LICENSE in the repository | the 2026 list is **ingested** (fixtures/tests); **live blocked** by the same 403 challenge. The frozen 2021 GitHub CSV is reachable but mixes UTF-8 and Windows-1252 bytes; not implemented (Archigos + the 2026 list cover the same leaders). |
| **Cline Center Coup d'État Project** v2.2.2 (1,161 events 1945-2026, realized/attempted/conspiracy, actor types, fate of the deposed leader) | Illinois Data Bank `IDB-7095878` (DOI 10.13012/B2IDB-9651987_V10) | CC BY (per the databank listing; record not readable by us) | **not ingested**: `databank.illinois.edu` (and its `aws-databank-alb` alias) answer 403 to every non-browser User-Agent (REVA, curl, httpx); not circumvented. Best next source once reachable (open licence, richer coding). |
| **Colpus** (Chin, Carter & Wright 2021) | no public distribution found (Harvard Dataverse search "Colpus" / "varieties of coups": nothing; the Cline Center merged 47 Colpus events into v2.2.1) | — | not available |
| **PLAD** (Political Leaders' Affiliation Database, Göttingen) | `plad.uni-goettingen.de/data/` | none stated | **skipped by rule**: personal data beyond office-holding (geocoded birthplace, ethnicity, spouses) |
| **Global Leader Ideology** (Herre 2023, BJPolS) | GitHub `bastianherre/global-leader-ideologies` (CSV/DTA) | no licence file (LICENSE 404), README asks for citation | not ingested (ideology country-year, not dated events); candidate for A-POL if wanted |
| Polity5 coup/regime-change variables | CSP | redistribution prohibited (A-POL's finding) | not used |

V-Dem, LIED and BMR (A-POL) are not duplicated: no regime index is re-ingested here.

## 2. Outputs (full real files, 2026-10-16; the live run reproduced the Archigos numbers, §6)

### Powell & Thyne (`powell_thyne.coups`)
* Dataset V2026.08.29: **497 events** (248 successful, 249 failed), 1950-05-10 → 2026-08-29, 93 crosswalk countries
  (top: Bolivia 24, Argentina 20, Sudan 17, Haiti 13, Venezuela 13). By decade (successful/failed): 1950s 30/30,
  1960s 67/56, 1970s 55/47, 1980s 38/40, 1990s 22/39, 2000s 12/18, 2010s 11/11, 2020s 13/8.
* atlas `powell_thyne.coup_events`: 497 rows, one per event, period = the day, `value` = provider code (2/1),
  `value_text` = successful/failed. The Niger 2026-08-29 attempt carries `flags.provisional` (provider note
  "Data have been tentatively updated … The coding will be updated to reflect the outcome within one week").
* atlas `powell_thyne.coups_cy` (**computed**, exact): 886 cells, indicators `COUP_EVENTS`, `COUPS_SUCCESSFUL`,
  `COUPS_FAILED`; every cell lists its event ids; only state-years with ≥ 1 event. Unmapped, kept with QIDs: COW
  365 in 1991 (USSR Q15180, the August 1991 attempt), 678/680 (North/South Yemen before 1990), 817 (South Vietnam).
* KG: 497 event entities `ptcoup:<ccode>:<YYYY-MM-DD>` (kind `event`; P&T have no ids and no two events share
  state and day — checked, fail closed), 1,988 statements: `coup:event_type` = coup, `coup:outcome`, `P585` (day,
  read by the causal grader), `coup:against_state` (QID via the crosswalk). `ccode_polity`, filled in 8 rows, is
  kept verbatim.
* Fail-closed checks: the page's coup definition must still read "illegal and overt attempts by the military or
  other elites within the state apparatus to unseat the sitting executive" / "control power for at least 7 days";
  required columns, codes 1/2, calendar dates, one dataset version, ≥ 450 rows live.

### Archigos 4.1 (`archigos.leaders`)
* 3,409 leader spells, 2,802 leaders, 1840-07-23 → 2015-12-31 (168 spells "Still in Office", censored at
  2015-12-31). Entry: Regular 2,796, Irregular 571, Foreign Imposition 40, Unknown 2. Exit: Regular 2,293, Irregular
  600, Natural Death 202, Still in Office 168, Foreign 72, Retired (ill health) 68, Suicide 3, Unknown 3.
* atlas `archigos.leaders_cy` (**computed**, exact): 26,474 cells, 27 indicators (`LEADERS_IN_OFFICE`,
  `LEADER_ENTRIES[|entry=…]`, `LEADER_EXITS[|exit=…|exitcode=…]`, provider labels verbatim in flags), **172
  crosswalk countries**; 23 COW codes stay unmapped in some years (German states/Reich, Austria-Hungary, Piedmont/
  Italy before 1861, Serbia/Yugoslavia, USSR/Russian Empire, Ottoman Empire, Qing/ROC China, Korea, Vietnams, Yemens,
  Hejaz/Najd …), with QIDs where A-HISTECON verified them.
* KG: 2,802 `archigos:leader:<leadid>` (person), 672 removal events `archigos:exit:<obsid>` (Irregular 600 +
  Foreign 72), 327 coup events `archigos:coup:<obsid>`; 13,724 statements: `archigos:leader_of` (3,409 spells,
  start/end day precision, qualifiers entry, exit, exitcode, posttenurefate, prevtimesinoffice, verbatim), birth year
  (2,783), death date (1,892), English-Wikipedia title from the DBpedia URI (1,168), removal statements, coup
  statements (`coup:event_type`, `P585`, `coup:against_state`, `coup:leader_removed`, `coup:perpetrator_type`) and
  149 context edges `coup:same_state_and_day_as` to P&T events.
* Provider quirks handled: 15 strings are Windows-1252, not UTF-8 (e.g. "Hernández", HON-2014) — decoded as
  cp1252 and listed in the run report; `yrdied` -777 (alive or presumed alive; the codebook says NA) and -999
  (missing) are not years; `dbpediauri` is wrapped in Proofpoint URL-Defense links — decoded deterministically
  (`-XX` → `%XX`, `_` → `/`, two percent-decodings). Unknown categories fail closed.

### REIGN-format leader list (`reign.leaderlist`)
* 2,739 rows (1921-04-11 → 2026-04-01 starts; 195 open spells; 98 rows without `leadid` → entity
  `reign:leader:c<ccode>-<start>`), updated 2026-04-14.
* atlas `reign.leaders_cy` (**computed**): 15,172 cells (`LEADERS_IN_OFFICE`, `LEADER_ENTRIES`), **194 crosswalk
  countries**; open spells counted through 2026.
* KG: 2,738 leaders, 2,739 `reign:leader_of` (start day, end month precision).
* Three provider defects kept and flagged, never corrected: MNE row 1117 end month 6 without year (end unknown,
  treated as in office), ISL row 1530 start day/month 12/21 (start kept at year precision), row 2242 end month 20
  (end kept at year precision).

## 3. Coded relations (CAUSCORE T1) — why they rest on Archigos alone

The mission asked to link Archigos "removed by coup" exits to the coup event. CAUSDATA's
`registry/causal/relations_datasets.yaml` sets `no_join_claims: REVA never creates a causal claim by joining two
datasets` — and Archigos itself codes both the coup and its outcome. So:

* Every Archigos spell with exit **Irregular** and exitcode "Removed by Military, without/with Foreign Support"
  ("the classic “military coup.”", codebook p. 3) or "Removed by Other Government Actors, without/with Foreign
  Support" becomes a coup event `archigos:coup:<obsid>` and **one T1 claim** through
  `reva.causal.load_dataset_claims`: cause `archigos:coup:<obsid>` → effect `archigos:exit:<obsid>`, relation
  **`coded_outcome`**, `mechanism` = the exitcode verbatim, `selector` = the `.dta` row (file, row, obsid, columns
  ccode/enddate/exit/exitcode), `raw_sha256` = the Archigos file only, `scope` = date + state entity.
  **327 claims** (283 `exitcode_military`, 44 `exitcode_other_government_actors`; 238 since 1950).
* Codebook (`Codebook(name="Archigos: A Data Set on Leaders 1875-2015, Version 4.1 (codebook)", version "4.1",
  url Archigos_4.1.pdf)`), definitions quoted verbatim (pp. 2-3): `exit_irregular`, `exitcode`,
  `exitcode_military` ("the great majority of irregular removals (277) are the result of action by domestic military
  actors without foreign support, the classic “military coup.”"), `exitcode_other_government_actors`.
* "Removed in Military Power Struggle Short of Coup", rebels, popular protest, assassination and foreign removal are
  **not** coups by Archigos's own words and get no coup event or claim.
* Temporal check: cause and effect carry the same `P585` day → `overlapping`; without corroboration the grader
  gives **B** (T1): live, 327 × B (§6).
* **Context, not identity, not causation**: when a *successful* P&T coup has the same COW code and the same day as an
  Archigos coup, `archigos:coup:<obsid> coup:same_state_and_day_as ptcoup:<…>` (qualifiers: method, P&T version,
  row, raw sha256). 149 of 327 match; of the 238 since 1950 the 89 others are 25 one day apart, 17 at 2-7 days,
  5 at 8-31 days, 36 further, 6 in states without any P&T coup — **not linked** (no date tolerance). 22 successful
  P&T coups fall on the end day of a spell Archigos codes otherwise (8 "Short of Coup", 6 Regular, 4 popular
  protest, 3 rebels, 1 natural death): not linked, counted. No P&T event matches two Archigos coups. EVENTS may decide whether these
  context edges justify `same_as`; A-COUPS does not assert it.

## 4. Wikidata `same_as` (option `wikidata=1`)

Archigos gives a DBpedia resource (= English-Wikipedia title) for 1,467 spells (1,168 leaders). The run sends
WDQS POST queries (250 titles each, machine-wide Wikidata lock) mapping the exact article IRI (MediaWiki URL
encoding, sitelink `schema:about`) to an item and reading `P31` and `P569`. `archigos:same_as` is written only if
the title is a sitelink of exactly one item, the item is an instance of human (Q5) and, when both sides have one,
the Wikidata birth year equals Archigos's `yrborn`; redirects/renamed titles, several titles per leader, non-humans
and birth-year disagreements are counted, not linked. Never by name. Each statement names its WDQS response
(`raw_sha256`) and carries the evidence in qualifiers. Live: **1,028 of 1,167 titled leaders linked** (87 titles
not a sitelink, 44 birth-year disagreements, 7 non-human items, 1 leader with two titles). A WDQS refusal (429/503)
sets a machine-wide cool-down and the run continues without `same_as`.

## 5. Tests

* `tests/unit/test_atlas_coups.py` (offline, 21): page link/definition/note and drift, CSV validation (codes,
  duplicates, columns, truncation), atlas frames validate, exact counts (sum of `COUP_EVENTS` = events), USSR row,
  `.dta` reader round trip / truncation / unknown release, Archigos parse (cp1252 row, year codes, DBpedia titles,
  category drift), claims rest on Archigos alone (no P&T id in any claim), context edges, `DatasetClaim`/`Codebook`
  construction against CAUSCORE, WDQS IRI encoding and the same_as gates, leader-list defects and header drift,
  specs discovered, every catalog series exists in the built tables, fixture hashes.
* `tests/integration/test_atlas_coups_ingest.py` (`db`, fixture mode, 4): atlas tables registered, KG loaded with 0
  rejections, causal claims loaded into `kg.causal_claim` (when migration `0015_causal` exists) with the Allende
  1973-09-11 chain checked end to end, idempotent re-runs.
* `tests/integration/test_atlas_coups_live.py` (`live and db`, deselected): one budget-metered live run of each source
  into the throwaway DB; a provider refusal is recorded in `REVA_COUPS_LIVE_OUT`, not hidden.

## 6. Live run (2026-10-16, throwaway DB, budget overlay of `a_coups.yaml`)

`pytest -m "live and db" tests/integration/test_atlas_coups_live.py` — 1 passed (129 s, after a first attempt that
waited ~30 min on the machine-wide WDQS cool-down and then failed on a WDQS date format; fixed, unit-tested).

| source | result |
|---|---|
| `atlas.coups` | **provider refused**: HTTP 403 from `jonathanmpowell.com` (bot challenge to the platform client), 1 request, nothing archived or registered |
| `atlas.archigos` (`wikidata=1`) | **completed**: 8 metered requests / 4.1 MB (countrycode, `.dta`, the refused P&T page, 5 WDQS POSTs), 7 raw artifacts archived; atlas `archigos.leaders_cy` 26,474 cells, 27 indicators, 172 countries, 1840-2015 (identical to §2); KG 3,808 entities + **14,603 statements, 0 rejected**; **327 T1 `coded_outcome` claims loaded** (0 rejected, 0 unknown entities) and graded by CAUSCORE's grader: **327 × B** (T1, temporal `overlapping` — same day —, single source); `same_as`: **1,028 leaders linked** to Wikidata; 87 titles not a sitelink (redirect/renamed), 44 birth-year disagreements, 7 items not humans, 1 leader with two titles — not linked; the P&T context edges were skipped (P&T host refused: `join.powell_thyne.status = powell_thyne_unavailable`) |
| `atlas.reign_leaders` | **provider refused**: HTTP 403 from `jonathanmpowell.com` |

KG statements by predicate after the run: `archigos:leader_of` 3,409, `archigos:birth_year` 2,783,
`archigos:death_date` 1,892, `archigos:enwiki_title` 1,168, `archigos:same_as` 1,028, `P585` 999, removal statements
672 × 3, coup statements 327 × 4. The 149 `coup:same_state_and_day_as` edges and the P&T events/atlas tables are
verified on the real captures (§2) and in fixture mode, not live.

## 7. Requests to other agents / lead (not edited by me)

* **ATLASCORE**: merge `registry/atlas/budget_requests/a_coups.yaml` (`powell_thyne.coups` 6, `archigos.leaders` 18,
  `reign.leaderlist` 6 per day).
* **Lead / ATLASOPS**: `jonathanmpowell.com` (P&T coups and the REIGN-format list) and `databank.illinois.edu`
  (Cline Center, CC BY) refuse the platform's HTTP client (403 bot challenges). A sanctioned path is needed (provider
  contact / allow-listing); A-COUPS did not impersonate a browser or use mirrors. Until then P&T and the 2026
  leader list are fixture-only; the code is ready.
* **EVENTS**: coup events exist under two ids (`ptcoup:*` from P&T, `archigos:coup:*` from Archigos) linked by
  `coup:same_state_and_day_as` (context). Taxonomy type `coup`; dates in `P585`.
* **CAUSCORE**: claims use source key `archigos.leaders`, dataset `coup_removals`, revision `archigos-4.1`.
* **Maintainer**: none of the three providers states a licence (cite-only); `review_flag` set in the source entries.
