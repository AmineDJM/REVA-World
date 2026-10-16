# V3 A-COLONIAL — colonial history and decolonisation of every country

Agent A-COLONIAL, 2026-10-16. Specs: `docs/roadmap/v3_world_graph.md`, `docs/roadmap/v3_causality.md`,
`docs/dev/v3_atlas_interface.md` §7, `docs/dev/v3_causal_interface.md`. Status: three atlas sources on the source
platform, fixtures, offline and throwaway-DB tests, a full-scale load of the real files into a throwaway database
(dropped afterwards), and a source-by-source comparison with CShapes and Cliopatria (both ingested live, budget-metered,
into the same throwaway database). Budgets requested (not merged yet, so no live run of the three new sources).

The owner's chains this serves: *Columbus → Spain/Portugal → Brazil speaks Portuguese* now has dated, sourced colonial
hops (Brazil —colonised by→ Portugal 1533–1822 per COLDAT; ICOW: primary colonial ruler Portugal, independence from
Portugal 1822-09, non-violent; COW: territory 140 colony of Portugal 1816–1822, independence case #46 dated 1824-12);
*US stance on decolonisation 1950s–60s* gets the colonial side (who ruled whom, when, independence dates and modes)
next to KGGEOPOL's UNGA votes. REVA asserts no cause anywhere in this layer (§6).

## 1. What exists

| Piece | Path |
|---|---|
| COLDAT + ICOW connector (`atlas.coldat`, `atlas.icow`), shared KG helpers, predicates, `colonial:independence_key` | `src/reva/adapters/connectors/atlas_colonial.py` |
| COW Territorial Change v6 + COW entities list connector (`atlas.cowtc`), bounded PDF text reader for `Entities.pdf` | `src/reva/adapters/connectors/atlas_terrchange.py` |
| Sources | `registry/sources/{coldat,icow,cow_terrchange}.yaml` |
| Coded relations (all non-causal, codebook quotes, pinned codebook sha256s) | `registry/causal/relations_colonial.yaml` |
| Geo schemes `coldat_name` (195 names) and `cowent` (18 entity codes, exact-name aliases with evidence; 3 refused) | `registry/atlas/geo_aliases/a_colonial.yaml` |
| Atlas catalog (117 concepts, domain `colonial`, theme `history`) | `registry/atlas/catalog/colonial.yaml` |
| Budget request | `registry/atlas/budget_requests/a_colonial.yaml` |
| Fixtures | `tests/fixtures/source/atlas.coldat/` (84 KB, real files), `atlas.icow/` (864 KB: two re-zipped slices + countrycode tarball shared by the three manifests), `atlas.cowtc/` (264 KB, real zip) |
| Tests | `tests/unit/test_atlas_colonial.py` (18, offline), `tests/integration/test_atlas_colonial_ingest.py` (1, `db`) |

Run: `uv run python -m reva.atlas.ingest atlas.coldat|atlas.icow|atlas.cowtc [--fixture] [--dataset …]` (live runs need
the budget merge, §10).

## 2. Sources, access, licences (checked 2026-10-16)

| Source | Pinned | Access | Licence / terms |
|---|---|---|---|
| **COLDAT** — Colonial Dates Dataset (Becker 2019) | Harvard Dataverse `doi:10.7910/DVN/T9SDEW` **v3.0** (2023-09-21): `COLDAT_dyads.tab` (id 7416947, 45,560 B), `COLDAT_colonies.tab` (7416946, 14,940 B), `read_me.txt` (7416948); quoted paper `Introducing COLDAT.pdf` (3476840, md5 `31f7a831…`, not fetched: its md5 must still be in the version record) | version record + 3 files by id (redirect to the Dataverse S3 bucket) | **CC0 1.0** (version record `license.name`, re-checked every run; a change stops the run with `licence_change`) |
| **ICOW Colonial History 1.1** (Hensel 2018) | `https://www.paulhensel.org/Data/colhist.zip` (179,640 B): `coldata110.csv` (217 COW states), `colcode.pdf` (codebook, sha256 pinned) | **https only**: `http://www.paulhensel.org/Data/*.zip` answers a JavaScript bot-verification page (7 KB HTML); the connector refuses a non-zip body | ICOW terms: free download, cite; *"Please do not redistribute ICOW data to other scholars"* — a professional courtesy, not an open licence: flagged (`review_flag`) |
| **ICOW Territorial Claims, provisional 1.01** (Frederick, Hensel & Macaulay 2017) | `https://www.paulhensel.org/Data/ICOWprov.zip` (374,577 B): `ICOWprov101.csv` (837 dyadic claims, 372 territories, 1816–2001), `UsersManual.pdf` (sha256 pinned) | same | same |
| **COW Territorial Change v6** (Tir et al. 1998; hosted by S. V. Miller) | `https://correlatesofwar.org/wp-content/uploads/terr-changes-v6.zip` (261,434 B, sha256 `849341106d55…`): `tc2018.csv` (842 changes 1816–2018), `tcmanual.pdf` and `Entities.pdf` (sha256 pinned) | one request | COW terms: cite, users cannot charge for access (not an open licence; as `registry/sources/correlates_of_war.yaml`) |
| countrycode 1.9.0 (CRAN) | `countrycode_1.9.0.tar.gz` (539,016 B) | fetched and archived by each run | GPL-3 package; code lists are facts (KGGEOPOL's crosswalk provenance) |

Every codebook quoted is checked by sha256 before parsing (`relations_colonial.yaml: documents`); a changed codebook
stops the dataset (the quoted labels might no longer be the provider's). Raw bytes are archived only after parsing
succeeded. Not ingested: ICOW Data 1.1 (full territorial / river / maritime claims with peaceful settlement attempts,
regional only) — the provisional world-wide claims file covers the claims and dates; the ICOW Historical State Names
data set (names only, synchronised with the colonial data).

**ICOW fixture caveat.** Because ICOW asks users not to redistribute its data, the repository fixture holds only slices
(21 of 217 colonial-history rows, 50 of 837 claims; lines byte-identical, codebooks unmodified as their sha256 is
pinned). A maintainer may decide that even these slices must go; the connector code does not depend on them.

## 3. Country codes

* **COLDAT names** → geo scheme `coldat_name`: 178 names are exact crosswalk labels / ISO / UN / World Bank names; the
  17 others are the CLDR English names of countrycode 1.9.0's `codelist` (e.g. `Congo - Kinshasa` → COD, cown 490;
  `Myanmar (Burma)` → MMR; `St. Kitts & Nevis` → KNA), each alias stating its evidence. 195/195 mapped.
* **COW state numbers** (ICOW, COW TC, COLDAT colonizers) → KGGEOPOL's `StateCodeCrosswalk` at the coded date
  (month-precision dates resolve at the end of the coded month: Taiwan 1949-12 → TWN, Kosovo 2008-02 → XKX).
  Historical states keep `iso3 = null` and their verified QID (`historical_states_qids.json`): West Germany Q713750,
  Bavaria, Two Sicilies, Zanzibar, North/South Yemen, South Vietnam… (29 ICOW codes unmapped, listed in the manifest).
* **COLDAT colonizers** → COW codes (Belgium 211, Britain 200, France 220, Germany 255, Italy 325, Netherlands 210,
  Portugal 235, Spain 230 — REVA's mapping of COLDAT's eight European powers onto the COW list it is built from),
  resolved at the spell's first year: Germany 1884 → German Empire Q43287, Italy 1889 → Kingdom of Italy Q172579,
  Britain/Spain/Portugal/France/Netherlands/Belgium → the present-day state (before 1816 the crosswalk's
  single-entity rule applies; the colonizer as coded is kept in every statement's qualifiers).
* **COW political-entity numbers** (`Entities.pdf`, TC `entity`): territories `cowent:<code>`; a code the crosswalk
  knows as a COW state maps to that state (e.g. 540 Angola → AGO, flagged `iso3_meaning`: the territory that code later
  denotes); 18 non-state codes map by exact printed name (Puerto Rico 6 → PRI, Macao 721 → MAC, Hong Kong 720 → HKG,
  Guadeloupe 65 → GLP, …). Refused, with the entities list's own evidence: 4776 "Benin" (printed "Became part of 475",
  Nigeria: the Benin kingdom, not BEN), 1010 "Georgia" (part of the USA / Confederacy), 9900 "Samoa" (991 American
  Samoa was part of 9900).

## 4. Atlas tables

| Table | Content | Rows | Countries | Period |
|---|---|---:|---:|---|
| `coldat.dyads_v3` | per (country, colonizer): `colonized|colonizer=<c>` (0/1 as coded; period = dataset coverage 1462..2016-12-31); for `agg=max` (last date aggregation) and `agg=mean` (mean aggregation): `colonial_rule|colonizer=<c>|agg=<a>` (value_text = power, period = [first, last + 1)), `colonial_rule_start…`, `colonial_rule_end…` (years) | 2,526 | 195 (127 ever colonised; 32 by more than one power) | 1462–2016 |
| `icow.colhist_v11` | per COW state: `independence_year`, `independence_from`, `independence_violent` (0/1), `independence_type` (Formation 36 / Decolonization 128 / Secession 38 / Partition 15), `primary_colonial_ruler` (171), `seceded_from`, `secession_year`, `secession_violent`, `merged_into`, `cow_system_entry`, `gw_system_entry`; coder notes in the flags | 1,545 | 193 | 301–2011 |
| `icow.claims_prov101` | one row per dyadic claim and claimant: `territorial_claim|claimdy=<id>|role=challenger|target`, value_text = territory, period = [begin month, end month) (109 ongoing at 2001-12-31), resolution / violence / salience / MIDs over the issue in the flags | 1,674 | 151 | 1816–2001 |
| `cowtc.changes_cy` | **computed** distinct TC cases per state-year: `gained` / `lost`, `|procedure=<conquest, annexation, cession, secession, unification, mandated_territory, not_applicable>`, `|independence=1`, `|military_conflict=1`; flags list the case numbers | 3,034 | 188 | 1816–2015 |
| `cowtc.entity_status` | the COW entities list as printed: `status|relation=<colony_of, protectorate_of, mandated_to, possession_of, leased_to, occupied_by, claimed_by, part_of, neutral_zone_of>|counterpart=<code>`, value_text = counterpart | 2,700 | 192 | 1816–1997 |

COLDAT long and wide files agree cell by cell (0 differences; a difference would be flagged `wide_file_differs` on
the cells, never resolved). Entities list: 2,706 printed rows of 1,154 entity codes read (0 unassigned); 30 rows
print an unreadable Begin Year (`-198`, `-199`, `-196` for some claimed islands): kept as printed, no period
invented (6 rows have no year at all and are KG-only); 4 codes carry two names in the list (374, 1152, 3351, 3377 —
reported); one row prints only a counterpart (`1032 Turks and Caicos Is. 1848 1862 "51"`: `status_unreadable`).
"1993" is the latest End Year of the list: flagged as such, never read as an end of rule.

## 5. Knowledge graph

Source keys `harvard.dataverse.coldat` (revision `doi:10.7910/DVN/T9SDEW@3.0`), `icow.paulhensel`
(`icow-colonial-history-1.1`, `icow-territorial-claims-provisional-1.01`), `cow.terrchange`
(`cow-territorial-change-v6`). Every statement: stable dataset key, raw sha256 and a `{file, line|page,row, column}`
reference; dates are the Wikidata time properties the causal grader and the event view read (`P580`/`P582`/`P585`) with
the provider's precision (year, or month when coded).

| Records | Count (full data) |
|---|---:|
| `coldat:colonized_by` country → colonizer (start/end year; one per aggregation, qualifiers: colonizer as coded, COW code, definition) | 322 |
| ICOW independence events `icowcol:independence:<ccode>` (`P585`, `icowcol:independence_of`, `…_from`, `…_violent`, `…_type`, coder note) + secession (38) and merger (19) events; `icowcol:primary_colonial_ruler` (171); COW / GW system entry dates | 277 entities, 2,450 statements |
| ICOW dyadic territorial claims `icowclaim:<claimdy>` (`P580`/`P582`, challenger, target, territory, claim number, region, resolution, ended through violence, salience, MIDs / fatal MIDs / wars over the issue) | 837 entities, 11,500 statements |
| COW TC events `cowtc:<number>` (`P585`, gainer, loser, territory, procedure, independence, military conflict, area, population, portion, territory types, contiguity, system entry/exit) | 842 events |
| COW entities `cowent:<code>` and their status spells (`cowent:colony_of` 315, `protectorate_of` 173, `mandated_to` 31, `possession_of` 101, `leased_to` 29, `occupied_by` 326, `claimed_by` 48, `part_of` 1,656, `neutral_zone_of` 26) + dated `geopol:corresponds_to_iso3` (502) | 1,165 entities; with the TC events 15,812 statements |
| `colonial:independence_key` (ICOW 178 + COW TC 188) | 366 |

52 dataset predicates (`colonial:*`, `coldat:*`, `icowcol:*`, `icowclaim:*`, `cowtc:*`, `cowent:*`) are registered with
fr/en labels and inverse names; none is causal (unit test against `CAUSAL_PIDS`). States are country QIDs
(`country_qids.json`), historical QIDs or `cow:<code>` entities with `geopol:corresponds_to_iso3`, exactly as KGGEOPOL.

**Identity across datasets (`same_as`, evidence only).** An ICOW independence and a COW TC independence carry the same
`colonial:independence_key` = `independence|cow:<state>|from|cow:<former ruler>` when both datasets code the same COW
state gaining independence from the same COW state (shared COW codes; dates are not part of the key). EVENTS' existing
`same_property` rule kind turns that into `events:same_as` (I do not write EVENTS' predicate). Run in the throwaway
database with an ad-hoc rule (`max_group: 2`): **145 links** (e.g. `cowtc:46` = `icowcol:independence:140`, Brazil;
`cowtc:795` = `icowcol:independence:540`, Angola) and **9 ambiguous keys** reported, not linked (two COW TC
independences of one state from one ruler: Estonia, Latvia, Lithuania, Georgia, Armenia, Azerbaijan, Ukraine… from
Russia in 1918–21 and 1991; Slovakia 1939 and 1993). A linked pair can still disagree on the date — that is listed in
§7, never resolved.

## 6. Causal claims: none (and why)

Mapping in `registry/causal/relations_colonial.yaml` (owner A-COLONIAL; 12 rules, each with the codebook text and the
reason). The lead's rule: a T1 claim only where the codebook codes a relation that is causal by definition **and the
dataset identifies both ends**. None of the four datasets identifies the cause event:

| Variable | Codebook (quoted) | Decision |
|---|---|---|
| ICOW `IndViol` (92 of 217 states = 1) | "The independence occurred through organized violence … if it occurred through armed revolt by the entity, or … through armed conflict between the former ruler and another state" | **attribute** of the independence event (`icowcol:independence_violent`); no war / MID identifier in the data → no `precipitated`/`coded_outcome` claim |
| ICOW `SecViol` | "The secession occurred through organized violence" | attribute (same reason) |
| ICOW `ColRuler` | "most responsible for shaping the development of the entity" | attribute statement with the definition |
| ICOW claims `resolved` (e.g. 7 Military Conquest/Occupation, 5 Independence), `endviol`, `midsiss`/`warsiss` | "Type of claim resolution …"; "Did claim end through organized violence?"; MIDs "over the issue" counted | attributes / context: the ending event and the disputes are categories and counts, not identified events |
| COW TC `procedur` = 1 Conquest (123 cases), `conflict` = 1 (229) | "Conquest occurs when armed force is the primary agent of the territorial change … [it] may take place with a bare minimum of force … coded as involving no military conflict"; "coded if there was military conflict between organized forces of both sides" | attributes (`cowtc:procedure`, `cowtc:military_conflict`); no war identifier in v6 |
| COW TC `indep`, gainer / loser / entity | "termination of colonial rule over a dependency" | context (participants); the independence is the change itself |
| COLDAT spells, COW entities statuses | dated state of affairs | attributes |

So `deliver_claims` is never called; each run summary says `claims: {"delivered": 0, "reason": …}` and the
integration test checks that no `kg.causal_load` row exists for the three source keys. Linking these modes to a COW
war or UCDP conflict would need an evidenced mapping table (not name/date matching) — a possible future task for
CAUSDATA/EVENTS.

## 7. Comparison with CShapes and Cliopatria — disagreements listed, never resolved

Method (a one-off comparison script, not committed — the method is fully described here; inputs = the real raw files parsed with the connectors' own parsers and
the KG statements of CShapes 2.0 and Cliopatria v0.2.1 ingested **live** into the same throwaway database —
CShapes 2 requests / 4.72 MB, Cliopatria 3 requests / 51.0 MB / 588 s, budget policy `budgets-1.3.7`, 0 refused):
per (present-day country, colonial power) the spell each source states. COW entities: spells `colony / protectorate /
mandate / possession of` the power's COW code, territory code mapped as in §3. CShapes: `cshapes:ruled_by` periods
whose `owner_gwcode` is the power, territory → ISO3 by its `geopol:corresponds_to_iso3`; "independence" = the start of
an `independent` period that follows a dependent one inside the 1886–2019 window (a polity independent on 1886-01-01
is left-censored, not an independence). Cliopatria: KGPOLITY's derived `geo:covered_territory_of` runs (share of the
present-day country's area inside the drawn polygon); polities are grouped under a power **by name, for this report
only** (list below; nothing is stored), presence counts only with a maximum share ≥ 10 %. Countries in M49 Europe are
left out of the CShapes / Cliopatria / COW-entities side (metropolitan polities, not colonial rule). Thresholds:
end years differing by more than 2 years, independence years by more than 1.

What the differences mostly are (read the rows, they are not errors by default): **definitions** (COLDAT's "last date
aggregation" takes the latest date any of its sources gives — Canada 1919, Australia 1919, New Zealand 1920, South
Africa 1920 = COW system entry —, ICOW dates dominion independence earlier: 1867, 1901, 1907, 1910; COW TC dates by the
treaty: Brazil 1824-12 vs ICOW 1822-09); **coverage** (CShapes starts 1886, COW 1816, COLDAT covers only 8 European
powers, the COW entities list stops at 1993); **granularity** (Cliopatria draws the Dutch/Spanish/Portuguese
Caribbean and Gulf outposts that COLDAT does not code: Aruba, Curaçao, Anguilla, Bahrain under the Iberian union…);
and some provider artefacts worth a maintainer look: Cliopatria's *French Fifth Republic* polygon covers 99.7 % of
Algeria in a row spanning 1961–2023 (KGPOLITY run `1961, 2023`), COLDAT codes Somalia–Italy ending 1936 (Italian East
Africa) while ICOW and COW TC date Somalia's independence from Italy 1960, COLDAT's Equatorial Guinea–Portugal spell is the single year 1778.

| Kind | Count |
|---|---:|
| rule by this power shown by another source, no COLDAT spell (`absent_in_coldat`) | 92 |
| last year of rule differs by more than 2 years (`end_year_differs`) | 70 |
| independence year differs by more than 1 year (`independence_year_differs`) | 15 |
| COLDAT spell after 1816, no COW entities-list spell (country has other spells) (`absent_in_cow_entities`) | 12 |
| COLDAT spell after 1886, no CShapes dependency period with this owner (country present in CShapes) (`absent_in_cshapes`) | 6 |
| COLDAT spell, no Cliopatria polity of this power over the country (country present in Cliopatria) (`absent_in_cliopatria`) | 3 |

<details><summary>rule by this power shown by another source, no COLDAT spell: 92</summary>

| Country | Power | What each source states |
|---|---|---|
| Aruba (ABW) | britain | Cliopatria (share>=10%): [1799, 1814, 0.6515] |
| Aruba (ABW) | netherlands | COW entities: [[1069, 'colony_of', 1816, 1845]]; Cliopatria (share>=10%): [1636, 2024, 0.6515] |
| Aruba (ABW) | spain | Cliopatria (share>=10%): [1529, 1635, 0.6515] |
| Afghanistan (AFG) | britain | CShapes: ['1888-12-31', '1919-04-30', ['protectorate']] |
| Anguilla (AIA) | britain | COW entities: [[1062, 'colony_of', 1816, 1882], [1062, 'colony_of', 1969, 1993]]; Cliopatria (share>=10%): [1691, 1814, 0.9238] |
| Anguilla (AIA) | france | Cliopatria (share>=10%): [1626, 1943, 0.9238] |
| Anguilla (AIA) | netherlands | Cliopatria (share>=10%): [1629, 2024, 0.9238] |
| Anguilla (AIA) | portugal | Cliopatria (share>=10%): [1809, 1810, 0.4437] |
| United Arab Emirates (ARE) | portugal | Cliopatria (share>=10%): [1521, 1581, 0.3694] |
| French Southern Territories (ATF) | france | Cliopatria (share>=10%): [1895, 2024, 0.8729] |
| Bonaire, Sint Eustatius and Saba (BES) | britain | Cliopatria (share>=10%): [1799, 1814, 0.7913] |
| Bonaire, Sint Eustatius and Saba (BES) | netherlands | Cliopatria (share>=10%): [1636, 2024, 0.7913] |
| Bonaire, Sint Eustatius and Saba (BES) | spain | Cliopatria (share>=10%): [1529, 1635, 0.7913] |
| Bahrain (BHR) | portugal | Cliopatria (share>=10%): [1521, 1581, 0.7936] |
| Bahrain (BHR) | spain | Cliopatria (share>=10%): [1582, 1608, 0.7936] |
| Bermuda (BMU) | britain | COW entities: [[30, 'colony_of', 1816, 1993]]; Cliopatria (share>=10%): [1609, 2024, 0.5338] |
| Central African Republic (CAF) | germany | Cliopatria (share>=10%): [1911, 1915, 0.2338] |
| Congo (COG) | belgium | Cliopatria (share>=10%): [1890, 1959, 0.1294] |
| Congo (COG) | germany | Cliopatria (share>=10%): [1911, 1915, 0.3242] |
| Cabo Verde (CPV) | spain | Cliopatria (share>=10%): [1582, 1639, 0.822] |
| Curaçao (CUW) | britain | Cliopatria (share>=10%): [1799, 1814, 0.881] |
| Curaçao (CUW) | netherlands | COW entities: [[1065, 'colony_of', 1816, 1845]]; Cliopatria (share>=10%): [1636, 2024, 0.881] |
| Curaçao (CUW) | spain | Cliopatria (share>=10%): [1529, 1635, 0.881] |
| Dominican Republic (DOM) | france | Cliopatria (share>=10%): [1700, 1804, 0.9915] |
| Dominican Republic (DOM) | portugal | Cliopatria (share>=10%): [1805, 1808, 0.9648] |
| Western Sahara (ESH) | spain | Cliopatria (share>=10%): [1890, 1975, 0.9944] |
| Ethiopia (ETH) | britain | Cliopatria (share>=10%): [1941, 1941, 0.9976] |
| Ethiopia (ETH) | italy | Cliopatria (share>=10%): [1936, 1940, 0.9949] |
| Falkland Islands (Malvinas) (FLK) | britain | Cliopatria (share>=10%): [1769, 2024, 0.8918] |
| Micronesia (Federated States of) (FSM) | germany | Cliopatria (share>=10%): [1900, 1914, 0.2904] |
| Gabon (GAB) | germany | Cliopatria (share>=10%): [1911, 1915, 0.273] |
| Guadeloupe (GLP) | britain | Cliopatria (share>=10%): [1762, 1813, 0.7743] |
| Guadeloupe (GLP) | france | COW entities: [[65, 'colony_of', 1816, 1993]]; Cliopatria (share>=10%): [1636, 2024, 0.7743] |
| Guadeloupe (GLP) | portugal | Cliopatria (share>=10%): [1805, 1810, 0.7743] |
| Gambia (GMB) | france | Cliopatria (share>=10%): [1880, 1959, 0.1346] |
| French Guiana (GUF) | france | Cliopatria (share>=10%): [1670, 2024, 0.9762] |
| French Guiana (GUF) | portugal | Cliopatria (share>=10%): [1778, 1823, 0.9951] |
| Guam (GUM) | spain | Cliopatria (share>=10%): [1670, 1897, 0.88] |
| China, Hong Kong Special Administrative Region (HKG) | britain | COW entities: [[720, 'colony_of', 1841, 1942], [720, 'colony_of', 1945, 1996]]; Cliopatria (share>=10%): [1842, 1999, 0.7849] |
| Haiti (HTI) | spain | Cliopatria (share>=10%): [1502, 1699, 0.9661] |
| Indonesia (IDN) | britain | Cliopatria (share>=10%): [1811, 1813, 0.1353] |
| British Indian Ocean Territory (IOT) | britain | COW entities: [[5920, 'colony_of', 1965, 1993]] |
| Iran (Islamic Republic of) (IRN) | britain | Cliopatria (share>=10%): [1915, 1945, 0.1808] |
| Iran (Islamic Republic of) (IRN) | netherlands | Cliopatria (share>=10%): [1895, 1897, 0.9833] |
| Israel (ISR) | france | Cliopatria (share>=10%): [1799, 1943, 0.149] |
| Kuwait (KWT) | portugal | Cliopatria (share>=10%): [1521, 1581, 0.1991] |
| Lebanon (LBN) | britain | Cliopatria (share>=10%): [1918, 1919, 0.8207] |
| Libya (LBY) | france | Cliopatria (share>=10%): [1905, 1952, 0.3629] |
| Sri Lanka (LKA) | netherlands | Cliopatria (share>=10%): [1659, 1795, 0.9913] |
| Sri Lanka (LKA) | spain | Cliopatria (share>=10%): [1582, 1639, 0.5074] |
| China, Macao Special Administrative Region (MAC) | portugal | COW entities: [[721, 'colony_of', 1816, 1993]]; Cliopatria (share>=10%): [1564, 2023, 0.5956] |
| China, Macao Special Administrative Region (MAC) | spain | Cliopatria (share>=10%): [1582, 1639, 0.5956] |
| Saint Martin (French Part) (MAF) | britain | Cliopatria (share>=10%): [1691, 1814, 0.9996] |
| Saint Martin (French Part) (MAF) | france | Cliopatria (share>=10%): [1626, 1701, 0.9996] |
| Saint Martin (French Part) (MAF) | netherlands | Cliopatria (share>=10%): [1629, 2024, 0.9996] |
| Mozambique (MOZ) | britain | Cliopatria (share>=10%): [1918, 1918, 0.2786] |
| Montserrat (MSR) | britain | COW entities: [[59, 'colony_of', 1816, 1871], [59, 'colony_of', 1956, 1958], [59, 'colony_of', 1962, 1993]] |
| Martinique (MTQ) | britain | Cliopatria (share>=10%): [1762, 1814, 0.8696] |
| Martinique (MTQ) | france | COW entities: [[66, 'colony_of', 1816, 1993]]; Cliopatria (share>=10%): [1636, 2024, 0.8696] |
| Martinique (MTQ) | portugal | Cliopatria (share>=10%): [1805, 1808, 0.8696] |
| Mauritius (MUS) | netherlands | Cliopatria (share>=10%): [1640, 1712, 0.7075] |
| Mayotte (MYT) | france | COW entities: [[5812, 'colony_of', 1843, 1914], [5812, 'colony_of', 1974, 1993]]; Cliopatria (share>=10%): [1677, 2023, 0.2044] |
| New Caledonia (NCL) | france | Cliopatria (share>=10%): [1860, 2024, 0.7782] |
| Nicaragua (NIC) | britain | Cliopatria (share>=10%): [1744, 1852, 0.1289] |
| Oman (OMN) | britain | Cliopatria (share>=10%): [1870, 2023, 0.9833] |
| Papua New Guinea (PNG) | germany | Cliopatria (share>=10%): [1885, 1914, 0.4967] |
| Puerto Rico (PRI) | spain | COW entities: [[6, 'colony_of', 1821, 1898]]; Cliopatria (share>=10%): [1512, 1899, 0.9515] |
| State of Palestine (PSE) | britain | Cliopatria (share>=10%): [1917, 1947, 0.9735] |
| French Polynesia (PYF) | france | COW entities: [[960, 'colony_of', 1903, 1993]] |
| Qatar (QAT) | portugal | Cliopatria (share>=10%): [1521, 1581, 0.988] |
| Réunion (REU) | britain | Cliopatria (share>=10%): [1811, 1819, 0.7922] |
| Réunion (REU) | france | Cliopatria (share>=10%): [1670, 2024, 0.7922] |
| South Georgia and the South Sandwich Islands (SGS) | britain | Cliopatria (share>=10%): [1775, 2024, 0.762] |
| Saint Pierre and Miquelon (SPM) | france | Cliopatria (share>=10%): [1691, 2024, 0.4203] |
| South Sudan (SSD) | britain | Cliopatria (share>=10%): [1885, 1955, 0.9987] |
| Sao Tome and Principe (STP) | spain | Cliopatria (share>=10%): [1582, 1639, 0.5398] |
| Suriname (SUR) | britain | Cliopatria (share>=10%): [1653, 1819, 0.9751] |
| Sint Maarten (Dutch part) (SXM) | britain | Cliopatria (share>=10%): [1691, 1814, 0.8786] |
| Sint Maarten (Dutch part) (SXM) | france | Cliopatria (share>=10%): [1683, 1701, 0.8786] |
| Sint Maarten (Dutch part) (SXM) | netherlands | Cliopatria (share>=10%): [1629, 2024, 0.8786] |
| Syrian Arab Republic (SYR) | britain | Cliopatria (share>=10%): [1918, 1919, 0.8343] |
| Turks and Caicos Islands (TCA) | britain | Cliopatria (share>=10%): [1683, 1995, 0.114] |
| Timor-Leste (TLS) | spain | Cliopatria (share>=10%): [1582, 1639, 0.1082] |
| Tunisia (TUN) | germany | Cliopatria (share>=10%): [1943, 1943, 0.1339] |
| Taiwan (TWN) | netherlands | Cliopatria (share>=10%): [1626, 1661, 0.9888] |
| Taiwan (TWN) | spain | Cliopatria (share>=10%): [1626, 1658, 0.1294] |
| Saint Vincent and the Grenadines (VCT) | france | Cliopatria (share>=10%): [1721, 1782, 0.6915] |
| United States Virgin Islands (VIR) | britain | Cliopatria (share>=10%): [1626, 1968, 0.5418] |
| United States Virgin Islands (VIR) | france | Cliopatria (share>=10%): [1626, 1712, 0.5418] |
| United States Virgin Islands (VIR) | spain | Cliopatria (share>=10%): [1629, 1631, 0.5418] |
| Kosovo (XKX) | germany | Cliopatria (share>=10%): [1941, 1944, 0.7445] |
| Kosovo (XKX) | italy | Cliopatria (share>=10%): [1941, 1943, 0.7213] |

</details>

<details><summary>last year of rule differs by more than 2 years: 70</summary>

| Country | Power | What each source states |
|---|---|---|
| Aruba (ABW) | netherlands | COW entities: 1845; Cliopatria: 2024 |
| Anguilla (AIA) | britain | COW entities: 1993; Cliopatria: 1814 |
| United Arab Emirates (ARE) | britain | COLDAT(max): 1971; COW entities: 1971; CShapes: 1971; Cliopatria: 2023 |
| Argentina (ARG) | spain | COLDAT(max): 1819; Cliopatria: 1810 |
| Antigua and Barbuda (ATG) | britain | COLDAT(max): 1981; COW entities: 1967; Cliopatria: 1968 |
| Australia (AUS) | britain | COLDAT(max): 1919; COW entities: 1919; Cliopatria: 1904 |
| Burundi (BDI) | germany | COLDAT(max): 1922; Cliopatria: 1916 |
| Bahrain (BHR) | britain | COLDAT(max): 1971; Cliopatria: 2023 |
| Bahamas (BHS) | britain | COLDAT(max): 1973; COW entities: 1973; CShapes: 1973; Cliopatria: 1995 |
| Belize (BLZ) | britain | COLDAT(max): 1981; COW entities: 1981; CShapes: 1981; Cliopatria: 1995 |
| Bermuda (BMU) | britain | COW entities: 1993; Cliopatria: 2024 |
| Brunei Darussalam (BRN) | britain | COLDAT(max): 1984; COW entities: 1983; CShapes: 1983; Cliopatria: 1995 |
| Canada (CAN) | britain | COLDAT(max): 1919; COW entities: 1919; Cliopatria: 1957 |
| Chile (CHL) | spain | COLDAT(max): 1818; Cliopatria: 1852 |
| Comoros (COM) | france | COLDAT(max): 1975; COW entities: 1975; CShapes: 1975; Cliopatria: 2023 |
| Curaçao (CUW) | netherlands | COW entities: 1845; Cliopatria: 2024 |
| Cyprus (CYP) | britain | COLDAT(max): 1960; COW entities: 1960; CShapes: 1960; Cliopatria: 1968 |
| Djibouti (DJI) | france | COLDAT(max): 1977; COW entities: 1977; CShapes: 1977; Cliopatria: 2023 |
| Dominica (DMA) | britain | COLDAT(max): 1978; COW entities: 1967; Cliopatria: 1995 |
| Dominica (DMA) | france | COLDAT(max): 1763; Cliopatria: 1782 |
| Dominican Republic (DOM) | spain | COLDAT(max): 1865; COW entities: 1865; Cliopatria: 1872 |
| Algeria (DZA) | france | COLDAT(max): 1962; COW entities: 1962; CShapes: 1962; Cliopatria: 2023 |
| Ecuador (ECU) | spain | COLDAT(max): 1829; COW entities: 1819; Cliopatria: 1821 |
| Egypt (EGY) | britain | COLDAT(max): 1935; COW entities: 1922; CShapes: 1922; Cliopatria: 1921 |
| Eritrea (ERI) | britain | COLDAT(max): 1951; CShapes: 1952; Cliopatria: 1955 |
| Eritrea (ERI) | italy | COLDAT(max): 1941; COW entities: 1936; CShapes: 1941; Cliopatria: 1940 |
| Fiji (FJI) | britain | COLDAT(max): 1970; COW entities: 1970; CShapes: 1970; Cliopatria: 1995 |
| Guadeloupe (GLP) | france | COW entities: 1993; Cliopatria: 2024 |
| Gambia (GMB) | britain | COLDAT(max): 1965; COW entities: 1965; CShapes: 1965; Cliopatria: 1968 |
| Equatorial Guinea (GNQ) | spain | COLDAT(max): 1968; COW entities: 1960; CShapes: 1968; Cliopatria: 1972 |
| Grenada (GRD) | britain | COLDAT(max): 1974; COW entities: 1967; Cliopatria: 1995 |
| Grenada (GRD) | france | COLDAT(max): 1762; Cliopatria: 1782 |
| China, Hong Kong Special Administrative Region (HKG) | britain | COW entities: 1996; Cliopatria: 1999 |
| Indonesia (IDN) | netherlands | COLDAT(max): 1962; COW entities: 1949; CShapes: 1945; Cliopatria: 1949 |
| Iraq (IRQ) | britain | COLDAT(max): 1932; COW entities: 1932; CShapes: 1932; Cliopatria: 1946 |
| Jamaica (JAM) | britain | COLDAT(max): 1962; COW entities: 1958; CShapes: 1962; Cliopatria: 1962 |
| Jamaica (JAM) | spain | COLDAT(max): 1655; Cliopatria: 1658 |
| Kenya (KEN) | britain | COLDAT(max): 1963; COW entities: 1963; CShapes: 1963; Cliopatria: 1968 |
| Saint Kitts and Nevis (KNA) | britain | COLDAT(max): 1983; COW entities: 1967 |
| Kuwait (KWT) | britain | COLDAT(max): 1961; Cliopatria: 1968 |
| Lebanon (LBN) | france | COLDAT(max): 1946; COW entities: 1946; CShapes: 1944; Cliopatria: 1943 |
| Saint Lucia (LCA) | britain | COLDAT(max): 1979; COW entities: 1967; Cliopatria: 1995 |
| Saint Lucia (LCA) | france | COLDAT(max): 1814; Cliopatria: 1761 |
| Sri Lanka (LKA) | portugal | COLDAT(max): 1656; Cliopatria: 1686 |
| China, Macao Special Administrative Region (MAC) | portugal | COW entities: 1993; Cliopatria: 2023 |
| Morocco (MAR) | france | COLDAT(max): 1956; COW entities: 1956; CShapes: 1956; Cliopatria: 2023 |
| Martinique (MTQ) | france | COW entities: 1993; Cliopatria: 2024 |
| Mauritius (MUS) | france | COLDAT(max): 1810; Cliopatria: 1814 |
| Malawi (MWI) | britain | COLDAT(max): 1964; COW entities: 1964; CShapes: 1964; Cliopatria: 1968 |
| Malaysia (MYS) | britain | COLDAT(max): 1963; COW entities: 1957; CShapes: 1957; Cliopatria: 1962 |
| Mayotte (MYT) | france | COW entities: 1993; Cliopatria: 2023 |
| Namibia (NAM) | britain | COLDAT(max): 1920; Cliopatria: 1931 |
| New Zealand (NZL) | britain | COLDAT(max): 1920; COW entities: 1920; CShapes: 1907; Cliopatria: 1957 |
| Peru (PER) | spain | COLDAT(max): 1824; COW entities: 1821; Cliopatria: 1821 |
| Papua New Guinea (PNG) | britain | COLDAT(max): 1920; Cliopatria: 1945 |
| Paraguay (PRY) | spain | COLDAT(max): 1811; Cliopatria: 1823 |
| Qatar (QAT) | britain | COLDAT(max): 1971; CShapes: 1971; Cliopatria: 2023 |
| Solomon Islands (SLB) | britain | COLDAT(max): 1978; COW entities: 1978; CShapes: 1978; Cliopatria: 1995 |
| Solomon Islands (SLB) | germany | COLDAT(max): 1918; Cliopatria: 1897 |
| Somalia (SOM) | italy | COLDAT(max): 1936; Cliopatria: 1940 |
| Suriname (SUR) | netherlands | COLDAT(max): 1974; COW entities: 1954; CShapes: 1975; Cliopatria: 2023 |
| Trinidad and Tobago (TTO) | britain | COLDAT(max): 1962; COW entities: 1958; CShapes: 1962; Cliopatria: 1962 |
| Tunisia (TUN) | france | COLDAT(max): 1956; COW entities: 1956; CShapes: 1955; Cliopatria: 2023 |
| United Republic of Tanzania (TZA) | britain | COLDAT(max): 1963; COW entities: 1961; CShapes: 1961; Cliopatria: 1960 |
| Uganda (UGA) | britain | COLDAT(max): 1962; COW entities: 1962; CShapes: 1962; Cliopatria: 1968 |
| Uruguay (URY) | spain | COLDAT(max): 1828; Cliopatria: 1810 |
| Saint Vincent and the Grenadines (VCT) | britain | COLDAT(max): 1979; COW entities: 1969; Cliopatria: 1968 |
| South Africa (ZAF) | britain | COLDAT(max): 1920; COW entities: 1920; Cliopatria: 1931 |
| Zambia (ZMB) | britain | COLDAT(max): 1964; COW entities: 1953; CShapes: 1953; Cliopatria: 1968 |
| Zimbabwe (ZWE) | britain | COLDAT(max): 1980; COW entities: 1980; CShapes: 1965; Cliopatria: 1968 |

</details>

<details><summary>independence year differs by more than 1 year: 15</summary>

| Country | Power | What each source states |
|---|---|---|
| Argentina (ARG) | spain | ICOW: 1816; COLDAT end(max): 1819; COW TC #3: 1816 |
| Australia (AUS) | britain | ICOW: 1901; COLDAT end(max): 1919; COW TC #525: 1920 |
| Brazil (BRA) | portugal | ICOW: 1822; COLDAT end(max): 1822; COW TC #46: 1824 |
| Canada (CAN) | britain | ICOW: 1867; COLDAT end(max): 1919; COW TC #496: 1920 |
| Haiti (HTI) | france | ICOW: 1804; COLDAT end(max): 1804; CShapes first independent: 1934 |
| Indonesia (IDN) | netherlands | ICOW: 1949; COLDAT end(max): 1962; COW TC #647: 1949; CShapes first independent: 1945 |
| Lebanon (LBN) | france | ICOW: 1943; COLDAT end(max): 1946; COW TC #608: 1946; CShapes first independent: 1944 |
| Malaysia (MYS) | britain | ICOW: 1957; COLDAT end(max): 1963; COW TC #680: 1957; CShapes first independent: 1957 |
| New Zealand (NZL) | britain | ICOW: 1907; COLDAT end(max): 1920; COW TC #527: 1920; CShapes first independent: 1907 |
| Peru (PER) | spain | ICOW: 1824; COLDAT end(max): 1824; COW TC #42: 1821 |
| Sudan (SDN) | britain | ICOW: 1956; COLDAT end(max): 1956; COW TC #276: 1884; COW TC #674: 1956; CShapes first independent: 1956 |
| Somalia (SOM) | italy | ICOW: 1960; COLDAT end(max): 1936; COW TC #706: 1960 |
| United Republic of Tanzania (TZA) | britain | ICOW: 1961; COLDAT end(max): 1963; COW TC #713: 1961; CShapes first independent: 1961 |
| South Africa (ZAF) | britain | ICOW: 1910; COLDAT end(max): 1920; COW TC #519: 1920 |
| Zimbabwe (ZWE) | britain | ICOW: 1965; COLDAT end(max): 1980; COW TC #819: 1980; CShapes first independent: 1965 |

</details>

<details><summary>COLDAT spell after 1816, no COW entities-list spell (country has other spells): 12</summary>

| Country | Power | What each source states |
|---|---|---|
| Belize (BLZ) | spain | COLDAT(max): [1524, 1862] |
| Cameroon (CMR) | germany | COLDAT(max): [1884, 1916] |
| Eritrea (ERI) | britain | COLDAT(max): [1941, 1951] |
| Ghana (GHA) | germany | COLDAT(max): [1884, 1916] |
| Ghana (GHA) | portugal | COLDAT(max): [1482, 1823] |
| Equatorial Guinea (GNQ) | britain | COLDAT(max): [1827, 1857] |
| Morocco (MAR) | spain | COLDAT(max): [1884, 1975] |
| Namibia (NAM) | britain | COLDAT(max): [1915, 1920] |
| Solomon Islands (SLB) | germany | COLDAT(max): [1885, 1918] |
| Togo (TGO) | germany | COLDAT(max): [1885, 1916] |
| Vanuatu (VUT) | france | COLDAT(max): [1887, 1980] |
| Samoa (WSM) | britain | COLDAT(max): [1914, 1920] |

</details>

<details><summary>COLDAT spell after 1886, no CShapes dependency period with this owner (country present in CShapes): 6</summary>

| Country | Power | What each source states |
|---|---|---|
| Cameroon (CMR) | germany | COLDAT(max): [1884, 1916] |
| Ghana (GHA) | germany | COLDAT(max): [1884, 1916] |
| Morocco (MAR) | spain | COLDAT(max): [1884, 1975] |
| Namibia (NAM) | britain | COLDAT(max): [1915, 1920] |
| Solomon Islands (SLB) | germany | COLDAT(max): [1885, 1918] |
| Togo (TGO) | germany | COLDAT(max): [1885, 1916] |

</details>

<details><summary>COLDAT spell, no Cliopatria polity of this power over the country (country present in Cliopatria): 3</summary>

| Country | Power | What each source states |
|---|---|---|
| Ghana (GHA) | portugal | COLDAT(max): [1482, 1823] |
| Equatorial Guinea (GNQ) | britain | COLDAT(max): [1827, 1857] |
| Senegal (SEN) | britain | COLDAT(max): [1693, 1817] |

</details>

<details><summary>Cliopatria polities grouped under each power for this comparison (name-based, comparison only, nothing stored)</summary>

* **belgium** (2): belgian_congo, kingdom_of_belgium
* **britain** (20): anglo_saxon_england, british_africa, british_cape_colony, british_ceylon, british_colonial_empire, british_east_africa, british_empire, british_gold_coast, british_overseas_territories, british_raj, commonwealth_of_england, denmark_england, english_colonial_empire, grp_allegiance_of_kingdom_of_hawai_i_to_british_empire, grp_anglo_saxon_england, grp_british_empire, grp_kingdom_of_england, kingdom_of_england, kingdom_of_great_britain, norman_england
* **france** (34): bourbon_kingdom_of_france, first_french_colonial_empire, first_french_empire, free_french, french_africa, french_algeria, french_colony_of_guiana, french_consulate, french_directory, french_fifth_republic, french_first_republic, french_fourth_republic, french_india, french_indochina, french_louisiana, french_mandate_for_syria_and_lebanon, french_second_republic, french_third_republic, grp_allegiance_of_duchy_of_aquitaine_to_kingdom_of_france, grp_bourbon_kingdom_of_france, grp_first_french_empire, grp_french_consulate, grp_french_directory, grp_french_first_republic, grp_french_fourth_republic, grp_french_second_republic, grp_french_third_republic, grp_kingdom_of_france, grp_second_french_empire, grp_vichy_france, kingdom_of_france, new_france, second_french_empire, vichy_france
* **germany** (9): federal_republic_of_germany, federated_republic_of_germany, german_africa, german_confederation, german_democratic_republic, german_empire, kingdom_of_germany, nazi_germany, north_german_confederation
* **italy** (5): italian_africa, italian_city_states, italian_nationalists, italian_republic, kingdom_of_italy
* **netherlands** (15): batavian_republic, dutch_cape_colony, dutch_ceylon, dutch_east_indies, dutch_gold_coast, dutch_republic, grp_batavian_republic, grp_dutch_republic, grp_netherlands, napoleonic_batavia_republic, netherlands, netherlands_antilles, new_netherland, sovereign_principality_of_the_united_netherlands, union_of_utrecht
* **portugal** (10): county_of_portugal, estado_novo, grp_portuguese_empire, kingdom_of_portugal, portugal, portuguese_africa, portuguese_ceylon, portuguese_colonies, portuguese_empire, portuguese_republic
* **spain** (13): crown_of_castile, first_spanish_republic, francoist_spain, grp_alliance_between_papal_states_and_spanish_empire, grp_personal_union_of_crown_of_aragon_with_crown_of_castile, grp_personal_union_of_spanish_empire_with_habsburg_monarchy, grp_spanish_empire, kingdom_of_castile, kingdom_of_spain, pro_habsburg_spain, second_spanish_republic, spanish_empire, spanish_nationalists

</details>

## 8. Per country: colonial spells and independence as each source states them (Portugal, Spain, Britain, France)

Rows: every present-day country that at least one source ties to the power (COLDAT spell, ICOW primary ruler or
independence from it, COW entities spell, COW TC independence from it, CShapes dependency period, or a Cliopatria
polity of the power over ≥ 10 % of the country; Europe excluded on the CShapes / Cliopatria / COW-entities side).
Cells are what the source codes, unmodified: COLDAT years (first–last year; `max` = last date aggregation, `mean` =
mean aggregation); ICOW primary ruler ("primary ruler" = this power) and its independence (date, "violent" =
`IndViol` = 1); COW entities list spells (`<entity code>: colony|protectorate|mandated|possession <Begin>–<End>`,
first three); COW TC independence cases from this power (`#number year-month`); CShapes dependency periods with this
owner (first start → last end, status); Cliopatria first–last provider year of the grouped polities and the maximum
share of the country's present-day area. An empty cell = the source says nothing for that pair (or does not cover the
period: CShapes from 1886, COW from 1816).

#### Portugal

| Country | COLDAT max (last date) | COLDAT mean | ICOW: ruler / independence from (date, violent?) | COW entities list | COW TC independence | CShapes (1886+) | Cliopatria |
|---|---|---|---|---|---|---|---|
| Anguilla (AIA) |  |  | not in ICOW |  |  |  | 1809–1810 (max 44 %) |
| Angola (AGO) | 1576–1975 | 1576–1974 | primary ruler; independence 1975-11 from Portugal (violent) | 540: colony 1816–1975 | #795 1975-11 | 1886-01-01 → 1975-11-10 (colony) | 1772–1975 (max 99 %) |
| Bahrain (BHR) |  |  | primary ruler United Kingdom; independence 1971-08 from United Kingdom |  |  |  | 1521–1581 (max 79 %) |
| Brazil (BRA) | 1533–1822 | 1533–1822 | primary ruler; independence 1822-09 from Portugal | 140: colony 1816–1822 | #46 1824-12 |  | 1540–1823 (max 91 %) |
| Cape Verde (CPV) | 1462–1975 | 1462–1975 | primary ruler; independence 1975-07 from Portugal | 402: colony 1816–1975 | #793 1975-07 | 1886-01-01 → 1975-07-04 (colony) | 1463–1975 (max 82 %) |
| Dominican Republic (DOM) |  |  | primary ruler Spain; independence 1844-02 from Haiti (violent) |  |  |  | 1805–1808 (max 96 %) |
| Equatorial Guinea (GNQ) | 1778–1778 | 1778–1778 | primary ruler Spain; independence 1968-10 from Spain |  |  |  | 1497–1777 (max 7 %) |
| Guadeloupe (GLP) |  |  | not in ICOW |  |  |  | 1805–1810 (max 77 %) |
| French Guiana (GUF) |  |  | not in ICOW |  |  |  | 1778–1823 (max 100 %) |
| Ghana (GHA) | 1482–1823 | 1476–1823 | primary ruler United Kingdom; independence 1957-03 from United Kingdom |  |  |  |  |
| Guinea-Bissau (GNB) | 1879–1974 | 1879–1973 | primary ruler; independence 1974-09 from Portugal | 404: colony 1879–1974 | #788 1974-09 | 1886-01-01 → 1974-09-09 (colony) | 1564–1975 (max 90 %) |
| Kuwait (KWT) |  |  | primary ruler United Kingdom; independence 1961-06 from United Kingdom |  |  |  | 1521–1581 (max 20 %) |
| China, Macao Special Administrative Region (MAC) |  |  | not in ICOW | 721: colony 1816–1993 |  |  | 1564–2023 (max 60 %) |
| Martinique (MTQ) |  |  | not in ICOW |  |  |  | 1805–1808 (max 87 %) |
| Malaysia (MYS) | 1511–1641 | 1511–1641 | primary ruler United Kingdom; independence 1957-08 from United Kingdom |  |  |  | 1512–1647 (max 7 %) |
| Mozambique (MOZ) | 1885–1975 | 1695–1974 | primary ruler; independence 1975-06 from Portugal (violent) | 541: colony 1816–1975 | #796 1975-06 | 1886-01-01 → 1975-06-24 (colony) | 1734–1975 (max 97 %) |
| Oman (OMN) |  |  | primary ruler; independence 1741 from Turkey (violent) |  |  |  | 1507–1705 (max 1 %) |
| Qatar (QAT) |  |  | primary ruler United Kingdom; independence 1971-09 from United Kingdom |  |  |  | 1521–1581 (max 99 %) |
| Sri Lanka (LKA) | 1619–1656 | 1619–1656 | primary ruler United Kingdom; independence 1948-02 from United Kingdom |  |  |  | 1507–1686 (max 99 %) |
| São Tomé & Príncipe (STP) | 1522–1975 | 1522–1975 | primary ruler; independence 1975-07 from Portugal | 403: colony 1816–1975 | #794 1975-07 |  | 1497–1975 (max 54 %) |
| Timor-Leste (TLS) | 1642–1975 | 1642–1975 | primary ruler; independence 2002-05 from Indonesia (violent) | 860: colony 1816–1942; 860: colony 1945–1975 |  | 1886-01-01 → 1976-07-16 (colony) | 1526–1975 (max 96 %) |
| United Arab Emirates (ARE) |  |  | primary ruler United Kingdom; independence 1971-12 from United Kingdom |  |  |  | 1521–1581 (max 37 %) |
| Uruguay (URY) | 1680–1822 | 1680–1822 | primary ruler Spain; independence 1828-08 from Brazil (violent) |  |  |  | 1683–1823 (max 100 %) |

#### Spain

| Country | COLDAT max (last date) | COLDAT mean | ICOW: ruler / independence from (date, violent?) | COW entities list | COW TC independence | CShapes (1886+) | Cliopatria |
|---|---|---|---|---|---|---|---|
| Aruba (ABW) |  |  | not in ICOW |  |  |  | 1529–1635 (max 65 %) |
| Argentina (ARG) | 1580–1819 | 1565–1818 | primary ruler; independence 1816-07 from Spain (violent) |  | #3 1816-07 |  | 1540–1810 (max 53 %) |
| Bonaire, Sint Eustatius and Saba (BES) |  |  | not in ICOW |  |  |  | 1529–1635 (max 79 %) |
| Bahrain (BHR) |  |  | primary ruler United Kingdom; independence 1971-08 from United Kingdom |  |  |  | 1582–1608 (max 79 %) |
| Belize (BLZ) | 1524–1862 | 1524–1862 | primary ruler United Kingdom; independence 1981-09 from United Kingdom |  |  |  | 1547–1821 (max 9 %) |
| Bolivia (BOL) | 1559–1825 | 1545–1825 | primary ruler; independence 1825-08 from Spain (violent) | 145: colony 1816–1825 | #50 1825-08 |  | 1540–1823 (max 99 %) |
| Curaçao (CUW) |  |  | not in ICOW |  |  |  | 1529–1635 (max 88 %) |
| Cape Verde (CPV) |  |  | primary ruler Portugal; independence 1975-07 from Portugal |  |  |  | 1582–1639 (max 82 %) |
| Chile (CHL) | 1541–1818 | 1541–1817 | primary ruler; independence 1818-02 from Spain (violent) |  | #31 1818-12 |  | 1540–1852 (max 64 %) |
| Colombia (COL) | 1525–1820 | 1518–1819 | primary ruler; independence 1819-12 from Spain (violent) | 100: colony 1816–1819 | #37 1819-12 |  | 1534–1821 (max 100 %) |
| Costa Rica (CRI) | 1524–1821 | 1516–1821 | primary ruler; independence 1838-11 from COW 89 (violent) |  |  |  | 1526–1821 (max 100 %) |
| Cuba (CUB) | 1511–1899 | 1511–1898 | primary ruler; independence 1902-05 from United States of America | 40: colony 1816–1898 |  | 1886-01-01 → 1898-12-09 (colony) | 1512–1899 (max 98 %) |
| Dominican Republic (DOM) | 1495–1865 | 1493–1839 | primary ruler; independence 1844-02 from Haiti (violent) | 42: colony 1816–1821; 42: colony 1861–1865 | #39 1821-11, #168 1865-12 |  | 1497–1872 (max 99 %) |
| Western Sahara (ESH) |  |  | not in ICOW |  |  |  | 1890–1975 (max 99 %) |
| Ecuador (ECU) | 1535–1829 | 1534–1823 | primary ruler; independence 1830-05 from Colombia | 130: colony 1816–1819 |  |  | 1534–1821 (max 97 %) |
| El Salvador (SLV) | 1528–1821 | 1525–1821 | primary ruler; independence 1840-04 from COW 89 (violent) |  |  |  | 1526–1821 (max 100 %) |
| Equatorial Guinea (GNQ) | 1858–1968 | 1858–1968 | primary ruler; independence 1968-10 from Spain | 411: colony 1858–1960 | #753 1968-10 | 1886-01-01 → 1968-10-11 (colony) | 1582–1972 (max 93 %) |
| Guam (GUM) |  |  | not in ICOW |  |  |  | 1670–1897 (max 88 %) |
| Guatemala (GTM) | 1524–1821 | 1524–1821 | primary ruler; independence 1839-04 from COW 89 (violent) |  |  |  | 1526–1821 (max 100 %) |
| Haiti (HTI) |  |  | primary ruler France; independence 1804-01 from France (violent) |  |  |  | 1502–1699 (max 97 %) |
| Honduras (HND) | 1524–1821 | 1524–1821 | primary ruler; independence 1838-11 from COW 89 (violent) |  |  |  | 1526–1821 (max 99 %) |
| Jamaica (JAM) | 1509–1655 | 1509–1655 | primary ruler United Kingdom; independence 1962-08 from United Kingdom |  |  |  | 1512–1658 (max 99 %) |
| China, Macao Special Administrative Region (MAC) |  |  | not in ICOW |  |  |  | 1582–1639 (max 60 %) |
| Mexico (MEX) | 1521–1821 | 1520–1821 | primary ruler; independence 1821-09 from Spain (violent) |  | #40 1821-03 |  | 1521–1821 (max 99 %) |
| Micronesia (Federated States of) (FSM) | 1650–1899 | 1650–1899 | primary ruler United States of America; independence 1986-11 from United States of America |  |  |  | 1877–1899 (max 29 %) |
| Morocco (MAR) | 1884–1975 | 1884–1975 | primary ruler France; independence 1666-06 from none (violent) |  |  |  | 1885–1975 (max 37 %) |
| Netherlands (NLD) |  |  | primary ruler none; independence 1581-07 from Spain (violent) |  |  |  |  |
| Nicaragua (NIC) | 1524–1821 | 1523–1821 | primary ruler; independence 1838-04 from COW 89 (violent) |  |  |  | 1526–1821 (max 100 %) |
| Puerto Rico (PRI) |  |  | not in ICOW | 6: colony 1821–1898 |  |  | 1512–1899 (max 95 %) |
| Palau (PLW) | 1886–1899 | 1886–1899 | primary ruler United States of America; independence 1994-10 from United States of America |  |  |  | 1885–1899 (max 56 %) |
| Panama (PAN) | 1519–1821 | 1514–1820 | primary ruler; independence 1903-11 from Colombia (violent) | 95: colony 1816–1819 |  |  | 1512–1821 (max 99 %) |
| Paraguay (PRY) | 1537–1811 | 1537–1811 | primary ruler; independence 1811-05 from Spain (violent) |  |  |  | 1540–1823 (max 98 %) |
| Peru (PER) | 1533–1824 | 1532–1823 | primary ruler; independence 1824-12 from Spain (violent) | 135: colony 1817–1821 | #42 1821-07 |  | 1540–1821 (max 100 %) |
| Philippines (PHL) | 1565–1898 | 1565–1898 | primary ruler; independence 1946-07 from United States of America | 840: colony 1816–1898 |  | 1886-01-01 → 1898-12-10 (colony) | 1572–1899 (max 97 %) |
| Sri Lanka (LKA) |  |  | primary ruler United Kingdom; independence 1948-02 from United Kingdom |  |  |  | 1582–1639 (max 51 %) |
| São Tomé & Príncipe (STP) |  |  | primary ruler Portugal; independence 1975-07 from Portugal |  |  |  | 1582–1639 (max 54 %) |
| Taiwan (TWN) |  |  | primary ruler none; independence 1949-12 from China (PRC) (violent) |  |  |  | 1626–1658 (max 13 %) |
| Timor-Leste (TLS) |  |  | primary ruler Portugal; independence 2002-05 from Indonesia (violent) |  |  |  | 1582–1639 (max 11 %) |
| Trinidad & Tobago (TTO) | 1592–1797 | 1592–1797 | primary ruler United Kingdom; independence 1962-08 from United Kingdom |  |  |  | 1534–1796 (max 90 %) |
| Uruguay (URY) | 1625–1828 | 1590–1823 | primary ruler; independence 1828-08 from Brazil (violent) |  |  |  | 1706–1810 (max 99 %) |
| United States Virgin Islands (VIR) |  |  | not in ICOW |  |  |  | 1629–1631 (max 54 %) |
| Venezuela (VEN) | 1556–1821 | 1534–1820 | primary ruler; independence 1830-01 from Colombia | 101: colony 1816–1819 |  |  | 1529–1821 (max 99 %) |

#### Britain

| Country | COLDAT max (last date) | COLDAT mean | ICOW: ruler / independence from (date, violent?) | COW entities list | COW TC independence | CShapes (1886+) | Cliopatria |
|---|---|---|---|---|---|---|---|
| Aruba (ABW) |  |  | not in ICOW |  |  |  | 1799–1814 (max 65 %) |
| Anguilla (AIA) |  |  | not in ICOW | 1062: colony 1816–1882; 1062: colony 1969–1993 |  |  | 1691–1814 (max 92 %) |
| Afghanistan (AFG) |  |  | primary ruler; independence 1919-08 from United Kingdom |  |  | 1888-12-31 → 1919-04-30 (protectorate) |  |
| Antigua & Barbuda (ATG) | 1632–1981 | 1632–1974 | primary ruler; independence 1981-11 from United Kingdom | 58: colony 1816–1871; 58: colony 1956–1958; 58: colony 1962–1967 | #824 1981-11 |  | 1632–1968 (max 47 %) |
| Australia (AUS) | 1829–1919 | 1798–1904 | primary ruler; independence 1901-01 from United Kingdom | 900: colony 1901–1919 | #525 1920-12 |  | 1828–1904 (max 99 %) |
| Bonaire, Sint Eustatius and Saba (BES) |  |  | not in ICOW |  |  |  | 1799–1814 (max 79 %) |
| Bermuda (BMU) |  |  | not in ICOW | 30: colony 1816–1993 |  |  | 1609–2024 (max 53 %) |
| Bahamas (BHS) | 1783–1973 | 1738–1973 | primary ruler; independence 1973-07 from United Kingdom | 31: colony 1816–1973 | #783 1973-07 | 1886-01-01 → 1973-07-09 (colony) | 1648–1995 (max 73 %) |
| Bahrain (BHR) | 1861–1971 | 1861–1971 | primary ruler; independence 1971-08 from United Kingdom |  | #768 1971-07 |  | 1895–2023 (max 79 %) |
| Bangladesh (BGD) | 1857–1947 | 1796–1947 | primary ruler; independence 1971-12 from Pakistan (violent) |  |  |  | 1762–1946 (max 99 %) |
| Barbados (BRB) | 1627–1966 | 1627–1966 | primary ruler; independence 1966-11 from United Kingdom | 53: colony 1816–1958; 53: colony 1962–1966 | #744 1966-11 | 1886-01-01 → 1966-11-29 (colony) | 1629–1968 (max 88 %) |
| Belize (BLZ) | 1798–1981 | 1798–1980 | primary ruler; independence 1981-09 from United Kingdom | 80: colony 1816–1862; 80: colony 1884–1981 | #823 1981-09 | 1886-01-01 → 1981-09-20 (colony) | 1670–1995 (max 91 %) |
| Bhutan (BTN) | 1910–1949 | 1888–1949 | primary ruler; independence 1947-08 from India | 760: protectorate 1865–1949 |  | 1886-01-01 → 1947-08-14 (protectorate) | 1842–1946 (max 2 %) |
| Botswana (BWA) | 1895–1966 | 1887–1966 | primary ruler; independence 1966-09 from United Kingdom | 571: protectorate 1885–1966 | #747 1966-09 | 1886-01-01 → 1966-09-29 (protectorate) | 1885–1968 (max 100 %) |
| Brunei (BRN) | 1888–1984 | 1888–1984 | primary ruler; independence 1984-01 from United Kingdom | 835: protectorate 1888–1941; 835: protectorate 1945–1983 | #828 1984-01 | 1888-09-17 → 1983-12-31 (protectorate) | 1885–1995 (max 100 %) |
| Curaçao (CUW) |  |  | not in ICOW |  |  |  | 1799–1814 (max 88 %) |
| Canada (CAN) | 1763–1919 | 1737–1877 | primary ruler; independence 1867-07 from United Kingdom | 20: colony 1841–1919 | #496 1920-12 |  | 1713–1957 (max 98 %) |
| Cyprus (CYP) | 1914–1960 | 1887–1960 | primary ruler; independence 1960-08 from United Kingdom (violent) | 352: colony 1914–1960 | #689 1960-08 | 1914-11-05 → 1960-08-15 (colony) | 1880–1968 (max 99 %) |
| Dominica (DMA) | 1805–1978 | 1805–1972 | primary ruler; independence 1978-11 from United Kingdom | 54: colony 1816–1871; 54: colony 1962–1967 | #811 1978-11 |  | 1762–1995 (max 99 %) |
| Egypt (EGY) | 1882–1935 | 1882–1926 | primary ruler Turkey; independence 1831-11 from Turkey | 651: colony 1882–1914; 651: protectorate 1914–1922 | #540 1922-02 | 1886-01-01 → 1922-02-27 (colony/protectorate) | 1885–1921 (max 100 %) |
| Equatorial Guinea (GNQ) | 1827–1857 | 1827–1857 | primary ruler Spain; independence 1968-10 from Spain |  |  |  |  |
| Eritrea (ERI) | 1941–1951 | 1941–1951 | primary ruler Italy (19th Century Sardinia); independence 1993-05 from Ethiopia (violent) |  |  | 1941-05-19 → 1952-09-14 (occupied) | 1885–1955 (max 100 %) |
| Eswatini (SWZ) | 1903–1968 | 1897–1968 | primary ruler; independence 1968-09 from United Kingdom | 572: colony 1902–1968 | #754 1968-09 | 1902-08-07 → 1968-09-05 (colony) | 1877–1968 (max 95 %) |
| Ethiopia (ETH) |  |  | primary ruler none; independence 1855-02 from none (violent) |  |  |  | 1941–1941 (max 100 %) |
| Falkland Islands (Malvinas) (FLK) |  |  | not in ICOW |  |  |  | 1769–2024 (max 89 %) |
| Fiji (FJI) | 1874–1970 | 1872–1970 | primary ruler; independence 1970-10 from United Kingdom | 950: colony 1874–1970 | #763 1970-10 | 1886-01-01 → 1970-10-09 (colony) | 1877–1995 (max 83 %) |
| Guadeloupe (GLP) |  |  | not in ICOW |  |  |  | 1762–1813 (max 77 %) |
| Gambia (GMB) | 1888–1965 | 1852–1965 | primary ruler; independence 1965-02 from United Kingdom | 420: colony 1816–1821; 420: colony 1843–1866; 420: colony 1888–1965 … | #739 1965-02 | 1889-08-10 → 1965-02-17 (colony) | 1820–1968 (max 87 %) |
| Ghana (GHA) | 1874–1957 | 1874–1957 | primary ruler; independence 1957-03 from United Kingdom | 452: colony 1816–1821; 452: colony 1843–1866; 452: colony 1874–1957 | #678 1957-03 | 1886-01-01 → 1957-03-05 (colony) | 1700–1959 (max 99 %) |
| Grenada (GRD) | 1762–1974 | 1762–1970 | primary ruler; independence 1974-02 from United Kingdom | 55: colony 1816–1833; 55: colony 1962–1967 | #787 1974-02 |  | 1762–1995 (max 80 %) |
| Guyana (GUY) | 1814–1966 | 1814–1966 | primary ruler; independence 1966-05 from United Kingdom | 110: colony 1831–1966 | #745 1966-05 | 1886-01-01 → 1966-05-25 (colony) | 1781–1968 (max 97 %) |
| China, Hong Kong Special Administrative Region (HKG) |  |  | not in ICOW | 720: colony 1841–1942; 720: colony 1945–1996 |  |  | 1842–1999 (max 78 %) |
| British Indian Ocean Territory (IOT) |  |  | not in ICOW | 5920: colony 1965–1993 |  |  |  |
| India (IND) | 1857–1947 | 1803–1947 | primary ruler; independence 1947-08 from United Kingdom | 750: colony 1816–1947 | #621 1947-08 | 1886-01-01 → 1947-08-14 (colony) | 1762–1946 (max 99 %) |
| Indonesia (IDN) |  |  | primary ruler Netherlands; independence 1949-12 from Netherlands (violent) |  |  |  | 1811–1813 (max 14 %) |
| Iran (IRN) |  |  | primary ruler none; independence 1502 from none (violent) |  |  |  | 1915–1945 (max 18 %) |
| Iraq (IRQ) | 1920–1932 | 1917–1932 | primary ruler Turkey; independence 1932-10 from United Kingdom | 645: mandated 1920–1932 | #561 1932-10 | 1920-04-26 → 1932-10-02 (mandate/occupied) | 1915–1946 (max 98 %) |
| Ireland (IRL) |  |  | primary ruler; independence 1922-12 from United Kingdom (violent) |  | #537 1922-12 |  |  |
| Israel (ISR) | 1923–1948 | 1920–1948 | primary ruler Turkey; independence 1948-05 from United Kingdom (violent) | 666: mandated 1923–1948 | #629 1948-05 |  | 1885–1947 (max 97 %) |
| Jamaica (JAM) | 1655–1962 | 1655–1962 | primary ruler; independence 1962-08 from United Kingdom | 51: colony 1816–1958 | #719 1962-08 | 1886-01-01 → 1962-08-05 (colony) | 1659–1962 (max 99 %) |
| Jordan (JOR) | 1923–1946 | 1923–1946 | primary ruler Turkey; independence 1946-05 from United Kingdom | 663: mandated 1923–1946 | #609 1946-03 | 1920-04-26 → 1946-05-24 (mandate/occupied) | 1918–1947 (max 100 %) |
| Kenya (KEN) | 1895–1963 | 1891–1963 | primary ruler; independence 1963-12 from United Kingdom (violent) | 501: protectorate 1895–1920; 501: colony 1895–1963 | #728 1963-12 | 1888-10-15 → 1963-12-11 (colony/protectorate) | 1890–1968 (max 99 %) |
| Kiribati (KIR) | 1892–1979 | 1892–1979 | primary ruler; independence 1979-07 from United Kingdom | 946: colony 1975–1979 | #818 1979-07 |  |  |
| Kuwait (KWT) | 1914–1961 | 1914–1961 | primary ruler; independence 1961-06 from United Kingdom |  | #715 1961-06 |  | 1915–1968 (max 78 %) |
| Lebanon (LBN) |  |  | primary ruler Turkey; independence 1943-11 from France |  |  |  | 1918–1919 (max 82 %) |
| Lesotho (LSO) | 1884–1966 | 1874–1966 | primary ruler; independence 1966-10 from United Kingdom | 570: protectorate 1868–1871; 570: colony 1884–1966 | #746 1966-10 | 1886-01-01 → 1966-10-03 (colony) | 1822–1968 (max 100 %) |
| Libya (LBY) | 1946–1952 | 1946–1952 | primary ruler Turkey; independence 1951-12 from United Kingdom | 620: mandated 1946–1952 |  | 1943-05-13 → 1951-12-23 (mandate/occupied) | 1885–1952 (max 63 %) |
| Saint Martin (French Part) (MAF) |  |  | not in ICOW |  |  |  | 1691–1814 (max 100 %) |
| Montserrat (MSR) |  |  | not in ICOW | 59: colony 1816–1871; 59: colony 1956–1958; 59: colony 1962–1993 |  |  |  |
| Martinique (MTQ) |  |  | not in ICOW |  |  |  | 1762–1814 (max 87 %) |
| Malawi (MWI) | 1891–1964 | 1891–1964 | primary ruler; independence 1964-07 from United Kingdom | 553: protectorate 1891–1964 | #738 1964-07 | 1891-06-10 → 1964-07-05 (protectorate) | 1895–1968 (max 99 %) |
| Malaysia (MYS) | 1888–1963 | 1847–1958 | primary ruler; independence 1957-08 from United Kingdom | 820: colony 1946–1957 | #680 1957-08 | 1946-04-01 → 1957-08-30 (colony) | 1873–1962 (max 98 %) |
| Maldives (MDV) | 1887–1965 | 1887–1965 | primary ruler; independence 1965-07 from United Kingdom | 781: protectorate 1887–1965 | #742 1965-07 | 1887-12-16 → 1965-05-25 (protectorate) |  |
| Malta (MLT) |  |  | primary ruler; independence 1964-09 from United Kingdom |  | #735 1964-09 |  |  |
| Mauritius (MUS) | 1810–1968 | 1810–1968 | primary ruler France; independence 1968-03 from United Kingdom | 590: colony 1816–1968 | #755 1968-03 | 1886-01-01 → 1968-03-11 (colony) | 1811–1968 (max 71 %) |
| Mozambique (MOZ) |  |  | primary ruler Portugal; independence 1975-06 from Portugal (violent) |  |  |  | 1918–1918 (max 28 %) |
| Myanmar (Burma) (MMR) | 1886–1948 | 1879–1948 | primary ruler; independence 1948-01 from United Kingdom | 775: colony 1937–1942; 775: colony 1945–1948 | #632 1948-01 | 1937-04-01 → 1948-01-03 (colony) | 1825–1947 (max 99 %) |
| Namibia (NAM) | 1915–1920 | 1915–1920 | primary ruler; independence 1990-03 from South Africa (violent) |  |  |  | 1890–1931 (max 99 %) |
| Nauru (NRU) | 1914–1968 | 1914–1944 | primary ruler Australia; independence 1968-01 from Australia |  |  |  |  |
| New Zealand (NZL) | 1840–1920 | 1840–1909 | primary ruler; independence 1907-09 from United Kingdom | 920: colony 1840–1920 | #527 1920-12 | 1886-01-01 → 1907-08-31 (colony) | 1830–1957 (max 96 %) |
| Nicaragua (NIC) |  |  | primary ruler Spain; independence 1838-04 from COW 89 (violent) |  |  |  | 1744–1852 (max 13 %) |
| Nigeria (NGA) | 1899–1960 | 1874–1960 | primary ruler; independence 1960-10 from United Kingdom | 475: colony 1914–1960 | #700 1960-10 | 1914-01-01 → 1960-09-30 (colony) | 1885–1960 (max 99 %) |
| Oman (OMN) |  |  | primary ruler Portugal; independence 1741 from Turkey (violent) |  |  |  | 1870–2023 (max 98 %) |
| State of Palestine (PSE) |  |  | not in ICOW |  |  |  | 1917–1947 (max 97 %) |
| Pakistan (PAK) | 1857–1947 | 1830–1947 | primary ruler; independence 1947-08 from United Kingdom |  | #622 1947-08 |  | 1820–1946 (max 99 %) |
| Papua New Guinea (PNG) | 1884–1920 | 1884–1912 | primary ruler; independence 1975-09 from Australia |  |  |  | 1885–1945 (max 96 %) |
| Qatar (QAT) | 1916–1971 | 1914–1970 | primary ruler; independence 1971-09 from United Kingdom |  | #769 1971-09 | 1916-11-03 → 1971-09-02 (protectorate) | 1936–2023 (max 96 %) |
| Réunion (REU) |  |  | not in ICOW |  |  |  | 1811–1819 (max 79 %) |
| South Georgia and the South Sandwich Islands (SGS) |  |  | not in ICOW |  |  |  | 1775–2024 (max 76 %) |
| Sint Maarten (Dutch part) (SXM) |  |  | not in ICOW |  |  |  | 1691–1814 (max 88 %) |
| Samoa (WSM) | 1914–1920 | 1914–1920 | primary ruler New Zealand; independence 1962-01 from New Zealand |  |  |  |  |
| Senegal (SEN) | 1693–1817 | 1693–1817 | primary ruler France; independence 1960-08 from Mali | 433: colony 1816–1817 |  |  |  |
| Seychelles (SYC) | 1814–1976 | 1814–1976 | primary ruler; independence 1976-06 from United Kingdom | 591: colony 1903–1976 | #805 1976-06 |  | 1811–1995 (max 6 %) |
| Sierra Leone (SLE) | 1896–1961 | 1838–1961 | primary ruler; independence 1961-04 from United Kingdom | 451: colony 1816–1961; 451: protectorate 1896–1961 | #710 1961-04 | 1886-01-01 → 1961-04-26 (colony) | 1788–1960 (max 99 %) |
| Singapore (SGP) | 1826–1963 | 1821–1961 | primary ruler; independence 1965-08 from Malaysia | 830: colony 1946–1963 |  | 1946-04-01 → 1962-12-31 (colony) | 1820–1962 (max 89 %) |
| Solomon Islands (SLB) | 1893–1978 | 1893–1978 | primary ruler; independence 1978-07 from United Kingdom | 940: protectorate 1893–1978 | #813 1978-07 | 1893-03-15 → 1978-07-06 (protectorate) | 1895–1995 (max 71 %) |
| Somalia (SOM) | 1888–1960 | 1885–1956 | primary ruler Italy (19th Century Sardinia); independence 1960-07 from Italy (19th Century Sardinia) |  |  |  | 1890–1959 (max 99 %) |
| South Africa (ZAF) | 1814–1920 | 1801–1912 | primary ruler; independence 1910-05 from United Kingdom | 560: colony 1910–1920 | #519 1920-12 |  | 1796–1931 (max 99 %) |
| South Sudan (SSD) |  |  | primary ruler; independence 2011-07 from Sudan (violent) |  |  |  | 1885–1955 (max 100 %) |
| Sri Lanka (LKA) | 1798–1948 | 1797–1948 | primary ruler; independence 1948-02 from United Kingdom | 780: colony 1833–1948 | #633 1948-01 | 1886-01-01 → 1948-02-03 (colony) | 1796–1947 (max 99 %) |
| St. Kitts & Nevis (KNA) | 1623–1983 | 1623–1975 | primary ruler; independence 1983-09 from United Kingdom | 60: colony 1956–1958; 60: colony 1962–1967 | #827 1983-09 |  |  |
| St. Lucia (LCA) | 1814–1979 | 1814–1973 | primary ruler; independence 1979-02 from United Kingdom | 56: colony 1816–1838; 56: colony 1962–1967 | #815 1979-02 |  | 1762–1995 (max 62 %) |
| St. Vincent & Grenadines (VCT) | 1762–1979 | 1762–1974 | primary ruler; independence 1979-10 from United Kingdom | 57: colony 1816–1833; 57: colony 1962–1969 | #816 1979-10 |  | 1762–1968 (max 69 %) |
| Sudan (SDN) | 1898–1956 | 1898–1956 | primary ruler; independence 1956-01 from United Kingdom | 625: colony 1898–1956 | #276 1884-01, #674 1956-01 | 1899-01-19 → 1955-12-31 (colony) | 1885–1955 (max 100 %) |
| Suriname (SUR) |  |  | primary ruler Netherlands; independence 1975-11 from Netherlands |  |  |  | 1653–1819 (max 98 %) |
| Syria (SYR) |  |  | primary ruler Turkey; independence 1946-04 from France (violent) |  |  |  | 1918–1919 (max 83 %) |
| Turks and Caicos Islands (TCA) |  |  | not in ICOW |  |  |  | 1683–1995 (max 11 %) |
| Tanzania (TZA) | 1918–1963 | 1912–1961 | primary ruler; independence 1961-12 from United Kingdom | 510: mandated 1922–1961 | #713 1961-12 | 1916-10-09 → 1961-12-08 (mandate/occupied) | 1916–1960 (max 98 %) |
| Tonga (TON) | 1900–1970 | 1900–1970 | primary ruler; independence 1970-06 from United Kingdom | 955: protectorate 1900–1970 | #764 1970-06 |  |  |
| Trinidad & Tobago (TTO) | 1797–1962 | 1797–1962 | primary ruler; independence 1962-08 from United Kingdom | 52: colony 1816–1958 | #720 1962-08 | 1886-01-01 → 1962-08-30 (colony) | 1762–1962 (max 94 %) |
| Tuvalu (TUV) | 1892–1979 | 1892–1978 | primary ruler; independence 1978-10 from United Kingdom | 947: colony 1975–1979 | #814 1978-10 |  |  |
| Uganda (UGA) | 1894–1962 | 1892–1962 | primary ruler; independence 1962-10 from United Kingdom | 500: protectorate 1894–1962 | #721 1962-10 | 1894-06-08 → 1962-10-08 (protectorate) | 1885–1968 (max 100 %) |
| United Arab Emirates (ARE) | 1892–1971 | 1892–1971 | primary ruler; independence 1971-12 from United Kingdom | 696: protectorate 1892–1971 |  | 1892-03-08 → 1971-12-01 (protectorate) | 1820–2023 (max 98 %) |
| United States (USA) | 1607–1783 | 1607–1781 | primary ruler; independence 1783-09 from United Kingdom (violent) |  |  |  | 1683–1782 (max 13 %) |
| United States Virgin Islands (VIR) |  |  | not in ICOW |  |  |  | 1626–1968 (max 54 %) |
| Vanuatu (VUT) | 1906–1980 | 1906–1980 | primary ruler; independence 1980-07 from France | 935: colony 1906–1980 |  |  |  |
| Yemen (YEM) | 1959–1967 | 1879–1967 | primary ruler; independence 1990-05 from Yemen Arab Rep. (Sanaa / North Yemen) |  |  |  | 1870–1968 (max 67 %) |
| Zambia (ZMB) | 1923–1964 | 1900–1964 | primary ruler; independence 1964-10 from United Kingdom | 551: colony 1911–1953 | #737 1964-10 | 1911-08-18 → 1953-07-31 (colony) | 1895–1968 (max 99 %) |
| Zimbabwe (ZWE) | 1923–1980 | 1900–1975 | primary ruler; independence 1965-11 from United Kingdom | 552: colony 1889–1953; 552: colony 1964–1965; 552: colony 1979–1980 | #819 1980-04 | 1889-01-01 → 1965-11-10 (colony) | 1890–1968 (max 99 %) |

#### France

| Country | COLDAT max (last date) | COLDAT mean | ICOW: ruler / independence from (date, violent?) | COW entities list | COW TC independence | CShapes (1886+) | Cliopatria |
|---|---|---|---|---|---|---|---|
| Anguilla (AIA) |  |  | not in ICOW |  |  |  | 1626–1943 (max 92 %) |
| French Southern Territories (ATF) |  |  | not in ICOW |  |  |  | 1895–2024 (max 87 %) |
| Algeria (DZA) | 1848–1962 | 1836–1962 | primary ruler; independence 1962-07 from France (violent) | 615: colony 1831–1962 | #724 1962-07 | 1886-01-01 → 1962-07-04 (colony) | 1840–2023 (max 100 %) |
| Benin (BEN) | 1894–1960 | 1873–1960 | primary ruler; independence 1960-08 from France | 434: colony 1894–1960 | #693 1960-08 | 1895-01-01 → 1960-07-31 (colony) | 1885–1960 (max 100 %) |
| Burkina Faso (BFA) | 1895–1960 | 1895–1960 | primary ruler; independence 1960-08 from France | 439: colony 1919–1932; 439: colony 1947–1960 | #697 1960-08 | 1919-03-01 → 1960-08-04 (colony) | 1895–1960 (max 100 %) |
| Cambodia (KHM) | 1884–1953 | 1868–1952 | primary ruler; independence 1953-11 from France (violent) | 811: colony 1884–1887; 811: colony 1949–1953 | #656 1953-11 | 1886-01-01 → 1953-11-08 (colony) | 1868–1955 (max 99 %) |
| Cameroon (CMR) | 1922–1960 | 1922–1960 | primary ruler; independence 1960-01 from France (violent) | 471: mandated 1922–1960 | #699 1960-01 | 1919-06-28 → 1959-12-31 (mandate/occupied) | 1900–1959 (max 90 %) |
| Canada (CAN) | 1608–1763 | 1608–1763 | primary ruler United Kingdom; independence 1867-07 from United Kingdom |  |  |  | 1738–1761 (max 1 %) |
| Central African Republic (CAF) | 1906–1960 | 1898–1960 | primary ruler; independence 1960-10 from France | 482: colony 1906–1960 | #702 1960-08 | 1906-02-11 → 1960-08-12 (colony) | 1895–1960 (max 99 %) |
| Chad (TCD) | 1910–1960 | 1899–1960 | primary ruler; independence 1960-08 from France | 483: colony 1920–1960 | #703 1960-08 | 1900-09-05 → 1960-08-10 (colony/occupied) | 1900–1960 (max 100 %) |
| Comoros (COM) | 1914–1975 | 1878–1975 | primary ruler; independence 1975-07 from France | 581: colony 1947–1975 | #797 1975-07 | 1946-10-27 → 1975-07-05 (colony) | 1677–2023 (max 53 %) |
| Congo - Brazzaville (COG) | 1882–1960 | 1881–1960 | primary ruler; independence 1960-08 from France | 484: colony 1882–1960 | #704 1960-08 | 1886-01-01 → 1960-08-14 (colony) | 1895–1960 (max 97 %) |
| Côte d’Ivoire (CIV) | 1889–1960 | 1869–1960 | primary ruler; independence 1960-08 from France | 437: protectorate 1889–1893; 437: colony 1893–1960 | #696 1960-08 | 1889-01-10 → 1960-08-06 (colony/protectorate) | 1890–1960 (max 99 %) |
| Djibouti (DJI) | 1884–1977 | 1869–1977 | primary ruler; independence 1977-06 from France | 522: colony 1884–1977 | #810 1977-06 | 1886-01-01 → 1977-06-26 (colony) | 1890–2023 (max 97 %) |
| Dominica (DMA) | 1632–1763 | 1632–1763 | primary ruler United Kingdom; independence 1978-11 from United Kingdom |  |  |  | 1718–1782 (max 99 %) |
| Dominican Republic (DOM) |  |  | primary ruler Spain; independence 1844-02 from Haiti (violent) |  |  |  | 1700–1804 (max 99 %) |
| Guadeloupe (GLP) |  |  | not in ICOW | 65: colony 1816–1993 |  |  | 1636–2024 (max 77 %) |
| French Guiana (GUF) |  |  | not in ICOW |  |  |  | 1670–2024 (max 98 %) |
| Gabon (GAB) | 1886–1960 | 1855–1960 | primary ruler; independence 1960-08 from France | 481: protectorate 1839–1903; 481: colony 1903–1960 | #701 1960-08 | 1886-01-01 → 1960-08-16 (colony/protectorate) | 1842–1960 (max 100 %) |
| Gambia (GMB) |  |  | primary ruler United Kingdom; independence 1965-02 from United Kingdom |  |  |  | 1880–1959 (max 13 %) |
| Grenada (GRD) | 1650–1762 | 1650–1762 | primary ruler United Kingdom; independence 1974-02 from United Kingdom |  |  |  | 1653–1782 (max 80 %) |
| Guinea (GIN) | 1881–1958 | 1858–1958 | primary ruler; independence 1958-10 from France | 438: protectorate 1843–1893; 438: colony 1893–1958 | #681 1958-10 | 1894-01-01 → 1958-10-01 (colony) | 1880–1959 (max 99 %) |
| Haiti (HTI) | 1665–1804 | 1665–1804 | primary ruler; independence 1804-01 from France (violent) |  |  |  | 1670–1804 (max 97 %) |
| Israel (ISR) |  |  | primary ruler Turkey; independence 1948-05 from United Kingdom (violent) |  |  |  | 1799–1943 (max 15 %) |
| Laos (LAO) | 1893–1954 | 1892–1952 | primary ruler; independence 1953-10 from France (violent) | 812: colony 1949–1954 | #661 1953-12 | 1893-10-03 → 1954-04-30 (colony) | 1895–1955 (max 96 %) |
| Lebanon (LBN) | 1923–1946 | 1921–1946 | primary ruler Turkey; independence 1943-11 from France | 660: mandated 1923–1946 | #608 1946-08 | 1920-04-26 → 1944-11-21 (mandate/occupied) | 1918–1943 (max 90 %) |
| Libya (LBY) |  |  | primary ruler Turkey; independence 1951-12 from United Kingdom |  |  |  | 1905–1952 (max 36 %) |
| Saint Martin (French Part) (MAF) |  |  | not in ICOW |  |  |  | 1626–1701 (max 100 %) |
| Martinique (MTQ) |  |  | not in ICOW | 66: colony 1816–1993 |  |  | 1636–2024 (max 87 %) |
| Mayotte (MYT) |  |  | not in ICOW | 5812: colony 1843–1914; 5812: colony 1974–1993 |  |  | 1677–2023 (max 20 %) |
| Madagascar (MDG) | 1895–1960 | 1890–1960 | primary ruler; independence 1960-06 from France | 580: protectorate 1882–1896; 580: colony 1896–1960 | #708 1960-06 | 1896-08-06 → 1960-06-25 (colony) | 1677–1959 (max 98 %) |
| Mali (MLI) | 1904–1960 | 1897–1960 | primary ruler; independence 1960-09 from France | 432: colony 1904–1959 | #690 1960-04 | 1905-01-01 → 1960-09-21 (colony) | 1880–1960 (max 100 %) |
| Mauritania (MRT) | 1903–1960 | 1882–1960 | primary ruler; independence 1960-11 from France | 435: protectorate 1903–1921; 435: colony 1921–1960 | #694 1960-11 | 1904-04-08 → 1960-11-27 (colony/protectorate) | 1860–1960 (max 99 %) |
| Mauritius (MUS) | 1715–1810 | 1715–1810 | primary ruler; independence 1968-03 from United Kingdom |  |  |  | 1721–1814 (max 71 %) |
| Morocco (MAR) | 1912–1956 | 1912–1956 | primary ruler; independence 1666-06 from none (violent) | 600: protectorate 1911–1956 | #670 1956-03 | 1912-11-27 → 1956-03-01 (protectorate) | 1905–2023 (max 63 %) |
| New Caledonia (NCL) |  |  | not in ICOW |  |  |  | 1860–2024 (max 78 %) |
| Niger (NER) | 1922–1960 | 1910–1960 | primary ruler; independence 1960-08 from France | 436: colony 1904–1960 | #695 1960-08 | 1905-01-01 → 1960-08-02 (colony) | 1900–1960 (max 100 %) |
| French Polynesia (PYF) |  |  | not in ICOW | 960: colony 1903–1993 |  |  |  |
| Réunion (REU) |  |  | not in ICOW |  |  |  | 1670–2024 (max 79 %) |
| Saint Pierre and Miquelon (SPM) |  |  | not in ICOW |  |  |  | 1691–2024 (max 42 %) |
| Sint Maarten (Dutch part) (SXM) |  |  | not in ICOW |  |  |  | 1683–1701 (max 88 %) |
| Senegal (SEN) | 1854–1960 | 1770–1960 | primary ruler; independence 1960-08 from Mali | 433: colony 1817–1959 |  | 1886-01-01 → 1959-04-03 (colony) | 1677–1959 (max 99 %) |
| Seychelles (SYC) | 1756–1794 | 1756–1794 | primary ruler United Kingdom; independence 1976-06 from United Kingdom |  |  |  | 1757–1810 (max 6 %) |
| St. Lucia (LCA) | 1650–1814 | 1650–1814 | primary ruler United Kingdom; independence 1979-02 from United Kingdom |  |  |  | 1645–1761 (max 62 %) |
| St. Vincent & Grenadines (VCT) |  |  | primary ruler United Kingdom; independence 1979-10 from United Kingdom |  |  |  | 1721–1782 (max 69 %) |
| Syria (SYR) | 1923–1946 | 1922–1944 | primary ruler Turkey; independence 1946-04 from France (violent) | 652: mandated 1923–1946 | #607 1946-04 | 1920-04-26 → 1945-12-31 (mandate/occupied) | 1920–1945 (max 99 %) |
| Togo (TGO) | 1922–1960 | 1922–1960 | primary ruler; independence 1960-04 from France | 461: mandated 1922–1960 | #698 1960-04 | 1922-07-20 → 1960-04-26 (mandate) | 1895–1960 (max 99 %) |
| Tunisia (TUN) | 1881–1956 | 1877–1956 | primary ruler Turkey; independence 1591 from Turkey (violent) | 616: protectorate 1881–1956 | #673 1956-03 | 1886-01-01 → 1955-12-31 (protectorate) | 1870–2023 (max 98 %) |
| United States Virgin Islands (VIR) |  |  | not in ICOW |  |  |  | 1626–1712 (max 54 %) |
| Vanuatu (VUT) | 1887–1980 | 1887–1980 | primary ruler United Kingdom; independence 1980-07 from France |  | #821 1980-07 |  |  |
| Vietnam (VNM) | 1887–1954 | 1869–1951 | primary ruler; independence 1954-07 from France (violent) |  |  | 1893-01-02 → 1954-04-30 (colony) | 1860–1955 (max 99 %) |

## 9. Full-scale load (real 2026-10-16 bytes, throwaway DB `reva_acolonial_tmp`, dropped afterwards)

The three sources ran through `AtlasRun` on a freshly bootstrapped and seeded database with a `FixtureHttp` root
holding the unmodified real downloads (origin `test_fixture`: these bytes were not fetched through the budget meter,
the budgets are not merged). CShapes and Cliopatria ran **live** in the same database for §7.

| Source | Time | Peak RSS | Atlas rows | KG entities / statements (rejected) |
|---|---|---|---|---|
| `atlas.coldat` | 9.4 s | 229 MB | dyads_v3 2,526 | 0 / 322 (0) — every subject and object is a QID |
| `atlas.icow` | 12.1 s | 268 MB | colhist_v11 1,545 · claims_prov101 1,674 | 1,114 / 13,950 (0) |
| `atlas.cowtc` | 13.1 s | 284 MB | changes_cy 3,034 · entity_status 2,700 | 2,022 / 15,812 (0) |

Re-running is a no-op (`created: false`, 0 statements inserted, all unchanged). Database after the five sources 109 MB;
object store 61 MB (of which Cliopatria 46 MB). No causal claim, no grading needed.

Spot checks (KG): `Q155` (Brazil) —`coldat:colonized_by`→ `Q45` (Portugal) 1533–1822 (both aggregations);
`icowcol:independence:140` 1822-09, from Q45, "No = The independence did not involve organized violence.",
Decolonization; `cowent:140` —`cowent:colony_of`→ Q45 1816–1822; `cowtc:46` 1824-12 gainer Q155 loser Q45, procedure
"Not applicable", independence; both events share `independence|cow:140|from|cow:235`. Angola: COLDAT 1576–1975,
ICOW 1975-11 violent, COW entity 540 colony of Portugal 1816–1975, TC #795 1975-11 with military conflict, CShapes
colony to 1975-11-10, Cliopatria Portuguese polities over Angola 1772–1975 (max share 99 %; Kingdom of Kongo 1440–1669 is not grouped).

## 10. Requests to other agents / lead (not edited by me)

* **ATLASCORE**: merge `registry/atlas/budget_requests/a_colonial.yaml` (`harvard.dataverse.coldat` 10/day 3 MB,
  `icow.paulhensel` 8/day 5 MB, `cow.terrchange` 6/day 3 MB). Until then invariant 30's
  `test_budget_keys_are_registered_sources_and_cover_every_connector` fails for exactly these three new
  `registry/sources/*.yaml` keys (verified: no other extra key) and live runs are refused. Optional theme rule
  `{domain: colonial, theme: history}` (each concept carries `theme: history` meanwhile).
* **EVENTS**: (1) add the rule `{id: colonial_independence_key, kind: same_property, property:
  "colonial:independence_key", status: active, max_group: 2}` to `registry/events/same_as_rules.yaml` (§5: 145 links,
  9 ambiguous); (2) `dataset_kinds` for `icowcol:independence:` (type `independence`), `icowcol:secession:`
  (`independence` or a secession type), `icowcol:merger:` (`annexation`/unification), `cowtc:` (type
  `transfer_of_sovereignty`, `independence` when `colonial:event_type` = independence), `icowclaim:` (a
  `territorial_claim` type) with `coded_properties` = the `icowcol:*` / `cowtc:*` / `icowclaim:*` attributes; the
  participant roles `icowcol:independence_from` / `cowtc:loser` = `former_ruler`.
* **LINKS**: a `colonial_rule_cow` link type from `cowent:colony_of|protectorate_of|mandated_to|possession_of`
  (territory → ruler, dated, territory mapped by `geopol:corresponds_to_iso3`, like `colonial_rule_cshapes` but
  1816–1993) and a `colonised_by_coldat` type from `coldat:colonized_by` (country QID → colonizer, dated, two
  aggregations); both are dataset statements, labelled "colonised by (COLDAT)" — never causal.
* **KGPOLITY**: check the Cliopatria run `french_fifth_republic` × Algeria 1961–2023 (share 99.7 %) — provider row or
  overlay artefact.
* **Maintainer**: ICOW's no-redistribution courtesy (data rows in REVA and the 21 + 50-line fixture slices); COW terms
  (cite, no charging) as for the other COW datasets.

## 11. Gaps

COLDAT gives one spell per (country, power) — first and last year, not interruptions (e.g. British occupations of
French Caribbean islands are inside or outside the spell as the sources aggregate them) — and only for eight European
powers (no Ottoman, Russian, US, Japanese, Danish or Swedish rule; those are in ICOW's ruler / COW entities / CShapes).
ICOW 1.1 codes no years of colonial rule (its planned "all colonial rulers with dates" is not released). COW TC and
entities start in 1816 (Brazil's 300 years of Portuguese rule appear only as 1816–1822); the entities list stops at
1993 and has unreadable years for a few claimed islands. ICOW claims stop in 2001 and identify no MID / settlement
event in the provisional file. Wikidata's colonial statements (KGHIST / LINKS `colony_of`, `former_colony_of`) remain
the only source for pre-1816 colonial entities as named polities (Colonial Brazil, New Spain…), and Cliopatria the
only spatial one.
