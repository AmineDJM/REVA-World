# V3 A-CONTENTION — social movements, protests, resistance campaigns

Agent A-CONTENTION, 2026-10-16. Specs: `docs/roadmap/v3_causality.md`, `docs/dev/v3_atlas_interface.md` §7,
`docs/dev/v3_causal_interface.md` (CAUSCORE). Status: three atlas sources on the source platform, fixtures, offline
and throwaway-DB tests, full-scale load of the real files into a throwaway DB with all 14,993 T1 claims loaded and
graded by CAUSCORE's `load_dataset_claims` / `grade_claims` (§7); budgets requested (not merged yet, so no live run).

## 1. What exists

| Piece | Path |
|---|---|
| NAVCO 1.2 / 1.3 / 2.1 / 3.0 + MEC connector (`atlas.navco`, `atlas.mec`) and the shared helpers (Dataverse version checks, tab reader, places, counting, KG predicates, claims) | `src/reva/adapters/connectors/atlas_navco.py` |
| Mass Mobilization connector (`atlas.massmob`) | `src/reva/adapters/connectors/atlas_massmob.py` |
| Sources | `registry/sources/{navco,mec,massmob}.yaml` |
| Location-name schemes `navco_loc`, `mec_loc`, `mmp_loc` (every provider name, with evidence) | `registry/atlas/geo_aliases/a_contention.yaml` |
| Atlas catalog (40 concepts, domain `contention`, theme `politics`) | `registry/atlas/catalog/contention.yaml` |
| Budget request | `registry/atlas/budget_requests/a_contention.yaml` |
| Fixtures (slices of the real downloads, hash-pinned) | `tests/fixtures/source/atlas.{navco,mec,massmob}/` (186 KB, 54 KB, 63 KB) |
| Tests | `tests/unit/test_atlas_contention.py` (offline), `tests/integration/test_atlas_contention_ingest.py` (throwaway DB, fixture mode) |

Run: `uv run python -m reva.atlas.ingest atlas.navco|atlas.mec|atlas.massmob [--fixture] [--option claims_staging=/abs/dir]`
(live runs need the budget merge, §8).

## 2. Sources, access, licences (checked 2026-10-16)

All on Harvard Dataverse, **CC0 1.0** (each pinned version's `license` record, re-checked on every run; a change
stops the run with `licence_change`). Files are fetched by id (redirect to `dvn-cloud-iqss.s3.amazonaws.com`). The
`.tab` export is checked against the version record's `filesize` (Dataverse's `md5` is the original xlsx/dta
upload's, verified: `format=original` matches it, the tab does not). The codebook each claim quotes must still be
listed in the version with the pinned md5.

| Dataset | Pinned | Content | Used for |
|---|---|---|---|
| NAVCO 1.2 (Chenoweth & Shay) | `doi:10.7910/DVN/0UZOTX` v2.0, file 3657601 (82,521 B; 389 campaigns + 473 blank spreadsheet rows, skipped and counted) | maximalist campaigns 1945-2013, outcome as of end 2014, REGVIOL, DEFECT, PARTICIPATION, flank, external support | KG + claims |
| NAVCO 1.3 list | `doi:10.7910/DVN/ON9XND` v1.0, file 3769104 (73,204 B; 622 campaigns 1900-2019) | method, goals, outcome as of 2019-12-31 | atlas `navco.n13_cy`, KG, claims |
| NAVCO 2.1 | `doi:10.7910/DVN/MHOXDV` v2.1, file 3620879 (1,384,763 B; 384 campaigns, 2,717 campaign-years 1945-2013) | day-precision dates, yearly method/goal/size, repression level, regime-inflicted fatalities, defection, progress, success | atlas `navco.n21_cy`, KG, claims |
| MEC v3.0 (Chenoweth & Kang) | `doi:10.7910/DVN/JQWQNW` v3.0, file 13992597 (1,333,438 B; 2,734 episodes 1955-2018) | category 2 reformist / 4 maximalist, day dates, goals with coder notes, size, regviol, defect, outcome (345 UNKNOWN) | atlas `mec.episodes_cy`, KG, claims |
| NAVCO 3.0 (Chenoweth, Pinckney & Lewis) | `doi:10.7910/DVN/INNYEO` v1.0, file 3612128 `navco3-0full.xlsx` (20,662,692 B, not ingested by Dataverse: md5 checked) | 112,381 CAMEO-style action lines (actor-verb-target per country-day), 26 countries 1990-2012 | atlas `navco.n30_cy` only (counts) |
| Mass Mobilization v5.1 (Clark & Regan) | `doi:10.7910/DVN/HTTWYL` v5.1, file 4291456 (15,625,472 B; 15,239 protests + 1,906 `protest = 0` country-years, 1990-2020) | protests of >= 50 people against the government: dates, location text, protester violence, participants, identity, 4 demands, 7 state responses | atlas `mmp.protests_cy`, KG, claims |

**Not stored**: MMP `sources` (newspaper citations with journalists' names) and `notes` (news excerpts, third-party
copyright); they are emptied in the fixture slice as well. Free-text fields that are kept are short coder fields:
NAVCO/MEC campaign names, targets and actors, MEC goal descriptions, MMP location and protester identity.

**Not ingested (decisions)**

* **NAVCO 3.0 in the KG**: each row carries `event_desc` (a pasted news lead), `source_title`, `localities` and
  `event_actor_list` (named people); only the coded columns are used (atlas counts), nothing textual is stored
  and the fixture slice drops those columns.
* **GDELT** (event database, CC-like "unlimited use with citation"): not ingested in bulk. The 1979-present event
  files run to hundreds of GB (15-minute updates since 2015), far beyond this agent's 300 MB disk cap and the KG
  size target; events are machine-coded from news by CAMEO dictionaries with heavy duplication and no codebook-level
  outcome/response coding, so they could never be T1 claims. If needed: country-month aggregates through GDELT's
  own query services, flagged as machine-coded — not done.
* **ACLED**: skipped. Access requires a registered (personal) account and an access key, and the terms of use
  restrict redistribution of the data; both are outside what an agent may do here (no personal accounts).
* **Major Episodes / NAVCO 1.1, 2.0**: superseded by the versions above (kept out to avoid duplicate events).

## 3. Geography

Provider location names → crosswalk keys through `registry/atlas/geo_aliases/a_contention.yaml`, one scheme per
dataset family (`navco_loc` 173 names: 133 exact crosswalk-label matches, 29 decisions, 11 unmapped; `mec_loc` 180:
149 / 23 / 8; `mmp_loc` 166: 144 / 16 / 6; `navco3_loc` 26 lower-case NAVCO 3.0 names, each by its `cowcode`). Decisions cite the provider's own code on the same rows (NAVCO 2.1
`loc_iso` ISO numeric, MEC `ccode` COW, MMP `ccode` Polity), e.g. `Burma/Myanmar` → MMR (loc_iso 104),
`Tibet` → CHN (NAVCO 2.1 loc_iso 156 / MEC ccode 710: the providers place Tibet campaigns in China; the atlas cell
lists the provider name), `Northern Ireland` → GBR (MEC ccode 200), `Hyderabad` / `Travancore` → IND (loc_iso 356).
NAVCO 1.3 defines LOCATION as the "contemporary sovereign country name": `Russia` 1905-1921 rows map to RUS and
every atlas cell repeats that codebook definition. Unmapped (kept, `iso3 = null`, provider name as `provider_geo`):
USSR, Yugoslavia, Czechoslovakia, East/West Germany, South Vietnam, North/South Yemen, Serbia and Montenegro,
Ottoman Empire, Natal, `Israel and Palestine` (two entities), `West Papua` (MEC codes it 910 = Papua New Guinea
for a territory in Indonesia: conflicting evidence, not chosen).

Atlas cells are per crosswalk country: names of one country (Burma + Burma/Myanmar; China + Tibet) are counted
together (`provider_geo` = the crosswalk key, `flags.provider_locations` lists the names) — the atlas refuses two
provider geographies mapping to one country-year, and REVA must not choose between them.

KG location: the country's Wikidata QID (`registry/entities/country_qids.json`), else `iso3:<KEY>` (PSE);
historical names resolve to A-HISTECON's verified, dated QIDs (`historical_states_qids.json`, COW entries) —
USSR 1922-1991 → Q15180, Yugoslavia 1919-1991 → Q36704 / 1992-2002 → Q838261, Czechoslovakia Q33946, East Germany
Q16957, ... — else a provider place entity `<ns>:place:<slug>` (e.g. the NAVCO "USSR" row of 1920-21, before the
Soviet Union existed, stays `navco:place:ussr`).

## 4. Atlas tables (all COMPUTED, exact counts per country-year)

| Table | Indicators | Rows (full data) |
|---|---|---|
| `navco.n13_cy` | `campaigns_active` (BYEAR ≤ y ≤ EYEAR; EYEAR = peak year, or end year when the peak is unknown), by `method=` nonviolent/violent and `goal=`; `campaign_onsets` (+ method); `campaigns_eyear` and `|outcome=success/limited/failure/ongoing` | 40,590 |
| `navco.n21_cy` | `campaign_years` and by `prim_method=`, `repression=none/mild/moderate/extreme/unknown`, `progress=0..5`, `goal=0..5`, `sec_defect=1`, `success=1` | 46,838 |
| `navco.n30_cy` | `action_lines` (NAVCO 3.0 coded lines dated in the year; one news event can give several lines and reciprocal actions are coded once per side — a count of coded actions, not of events), by `actor=` (state, non_state, international, nonaligned, local_state), `tactic=` (violent, nonviolent, mixed), `verb=` (the 21 root verbs, e.g. `14_protest`, `18_repress_and_abuse`), `state_posture=1..7` | 16,650 |
| `mec.episodes_cy` | `episodes_active` ([bdate, edate] overlaps y), by `category=` reformist/maximalist, `tactics=`, `regviol=1`; `episode_onsets` (+ category); `episodes_ending` and `|outcome=success/limited/failure/ongoing/unknown` | 56,144 |
| `mmp.protests_cy` | `protests` (MMP year = start year), `protester_violence=0/1`, `demand=` (7), `response=` (7 incl. `ignore`), `participants_category=` (6) | 112,148 |

Every cell: `flags` = `computed_from`, `method`, the indicator `definition`, `provider_locations`, the counted
`items` (provider ids), codebook notes; `selector` = the provider lines. NAVCO/MEC: only country-years with at
least one campaign/episode are listed (absence of a row is not a reported zero; zeros inside a listed
country-year are computed from the same complete coding). MMP: a country-year coded `protest = 0` is a
**provider-reported zero** (flag `zero_semantics`, selector = that provider line); country-years absent from the
file are not covered. NAVCO lists are consensus samples of mature campaigns (codebook: under-reporting of failed
nonviolent campaigns) — flagged `consensus_list`.

## 5. KG (source keys `harvard.dataverse.navco`, `.mec`, `.massmob`)

Entities (kind `event` unless noted): `navco12:<id>-<BYEAR>`, `navco13:<id>-<BYEAR>` (NAVCO 1.3 ids are "Random
numbers assigned" and repeat: id 408 is both *Karens* 1948 and *Pro-Democracy movement in Burma* 1988; the four
repeated (id, BYEAR) pairs get `-1`/`-2` in file order), `navco21:<id>`, `mec:<id>`, `mmp:<id>`; state repression
events `navco21:<id>:repression:<year>` (repression ≥ 1), `navco12:<key>:regime_violence`, `mec:<id>:regime_violence`;
MMP state responses `mmp:<id>:state_response` (any response other than `ignore`); coded outcomes
`<event>:outcome` (kind `campaign_outcome`); provider places and `iso3:` nodes as above.

Dates are the Wikidata time properties the causal grader reads: `P580` start time / `P582` end time / `P585` point in
time, with the provider's precision (NAVCO 1.x BYEAR year; NAVCO 2.1, MEC, MMP day). NAVCO EYEAR is
`contention:peak_or_end_year` (it is not an end date). Repression / response events carry the dates of the campaign
year, campaign or protest they belong to (the datasets give them no own date) — said in a qualifier.

32 dataset predicates `contention:*` (registered with fr/en labels; none causal): `event_type`, `location`,
`location_text`, `target`, `actor`, `peak_or_end_year`, `primary_method`, `goal`, `category`,
`peak_participation`, `size_category`, `coded_outcome`, `progress`, `repression_level`, `regime_violence`,
`security_force_defection`, `regime_inflicted_fatalities`, `violent_flank`, `external_support_campaign`,
`external_support_regime`, `simultaneous_violent_campaign`, `provider_id`, `outcome_of`, `directed_at`,
`same_campaign_as`, `phase_of`, `protester_demand`, `state_response`, `protester_violence`, `protester_identity`,
`participants_reported`, `participants_category`. Every statement: `statement_id` = stable dataset key, raw
sha256 and `{file, line, column}` reference.

Links between datasets (only with evidence): NAVCO 1.2 → NAVCO 2.1 through the shared id — 1.2 codebook: the id's
source is NAVCO 2.1, and split phases "share a NAVCO ID and are coded as a single campaign in the NAVCO 2.1 dataset"
→ `contention:same_campaign_as` (unique id) or `contention:phase_of` (split rows, e.g. Syria 282: uprising and civil
war). NAVCO 1.3 ↔ 1.2/2.1 and MEC ↔ NAVCO are **not** linked: 1.3 ids are random (355 of 370 shared ids have the same
name, 15 do not), MEC has its own ids; matching by name/date is not evidence. EVENTS may add `same_as` with a mapping
table later.

## 6. Causal claims (tier T1)

Delivered as `causdata_common.ClaimDelivery` = exactly the arguments of `reva.causal.load_dataset_claims` (one
complete delivery per dataset file: `navco12`, `navco13`, `navco21`, `mec`, `mmp`; `dataset_revision` =
`<doi>@<version>#<file id>`; `Codebook` = the pinned codebook file id/md5/sha256 and its definitions quoted
verbatim — `atlas_navco.CODEBOOK`, `atlas_massmob.MANUAL`, checked against the pinned PDF/docx text on 2026-10-16).
Loaded when CAUSCORE's loader and schema exist; otherwise staged as JSON in `run.options["claims_staging"]`
(CAUSDATA's format, loadable with `causdata_common.load_staged`). Selector = `{file, line, columns, coded}`
(`coded` = the coded value, e.g. `success (ongoing)`, `repression = 3 (extreme)`, `arrests, beatings`).

| Relation (direction as `registry/causal/relations.yaml`) | Claims | Codebook term (quoted) |
|---|---|---|
| `coded_outcome`: cause = campaign/episode, effect = its `:outcome` entity | NAVCO 1.2: 389 · NAVCO 1.3: 620 (2 rows carry no outcome flag) · NAVCO 2.1: 278 (campaigns with an end-year row: progress + success) · MEC: 2,389 (345 UNKNOWN: no claim) | SUCCESS / LIMITED / FAILURE / ONGOING (ongoing rows use ONGOING: "Campaigns coded as ongoing are also coded as failures"); NAVCO 2.1 `success` |
| `reaction_to`: cause = campaign/protest, effect = the state repression / response | NAVCO 2.1: 2,629 campaign-years with repression ≥ 1 · NAVCO 1.2: 352 (REGVIOL = 1) · MEC: 1,115 (regviol = 1) · MMP: 7,221 protests with a response other than `ignore` | 2.1 "most repressive episode or activity perpetrated by the state in response to campaign activity"; 1.2/MEC "used violence to crack down on campaign" (MEC table: "retaliate against the campaign"); MMP "actions (or inaction) the state might take in response to protests" |

Total 14,993 T1 claims (`coded_outcome` 3,676, `reaction_to` 11,317).

Decisions: MMP `ignore` makes no claim (manual: it may be the press ignoring the response rather than the state
not responding). Demands and goals are what protesters seek, not causes (context statements only). Defection,
external support, violent flank are attributes (no codebook wording of cause/response). No claim joins two
datasets. Expected temporal checks: `coded_outcome` consistent/overlapping (BYEAR vs EYEAR), `reaction_to`
overlapping (responses dated by their campaign/protest) — never invented as "consistent".

## 7. Full-scale load (real 2026-10-16 bytes, throwaway DB, dropped afterwards)

Script: the three atlas sources run through `AtlasRun` on a fresh bootstrapped + seeded database
(`reva_acontention_*`), with a `FixtureHttp` root holding the unmodified real downloads (origin `test_fixture`:
these bytes were not fetched through the budget meter, the budgets are not merged), then `reva.causal.grade_claims`.

| Source | Time | Peak RSS | Atlas rows | KG entities / statements (rejected) | Claims loaded (rejected, unknown entities) |
|---|---|---|---|---|---|
| `atlas.navco` (1.2, 1.3, 2.1, 3.0) | 74 s (NAVCO 3.0 workbook ≈ 40 s) | 0.73 GB | n13_cy 40,590 · n21_cy 46,838 · n30_cy 16,650 | 5,667 / 56,979 (0) | 4,268 (0, 0) |
| `atlas.mec` | 18 s | 0.73 GB | episodes_cy 56,144 | 6,240 / 64,090 (0) | 3,504 (0, 0) |
| `atlas.massmob` | 53 s | 1.0 GB | protests_cy 112,148 | 22,460 / 216,256 (0) | 7,221 (0, 0) |

`kg.statement` after the three loads: 327 MB (337,325 statements); database 434 MB. CAUSCORE's loader was present:
all 14,993 claims loaded through `load_dataset_claims` (no rejection, every cause/effect a loaded KG entity), then
graded in 10 s: **14,991 B** (T1, single dataset) and **2 E** (`temporal.inconsistent`): two NAVCO 2.1 provider
anomalies, shown not corrected — *Anya Nya* (Sudan) has a 1962 campaign-year row with repression coded while its
start_date is 1963-10-01; *Carnation Revolution* (Portugal) has a 1973 row and a start_date (1974-09-09) after its
end_date (1974-04-25). Temporal checks: `coded_outcome` consistent 1,699 / overlapping 1,977; `reaction_to`
consistent 2,277 (NAVCO 2.1 repression in later campaign years) / overlapping 9,038 (same-period responses) /
inconsistent 2. No claim is A: no second independent source asserts the same (cause, effect) — the event ids are
dataset-scoped until EVENTS links them.

Spot checks (KG): NAVCO 1.3 campaigns starting 2011 include *Protest for Constitutional Reform* (Jordan, 2011-2013),
*Libyan Civil War* (2011), *Anti-al-Bashir Government* (Sudan, 2011-2013); `navco21:282` (Syria) has repression
claims for 2011, 2012, 2013 (`repression = 3 (extreme)`, `NAVCO2-1_ForPublication.tab` lines 1905-1907);
MMP France: 27 protests in 1990, 49 in 2016; Canada 1998 is a provider-reported zero.

## 8. Requests to other agents / lead (not edited by me)

* **ATLASCORE**: merge `registry/atlas/budget_requests/a_contention.yaml` (`harvard.dataverse.navco` 20/day 45 MB,
  `.mec` 6/day 3 MB, `.massmob` 6/day 33 MB). Until then invariant 30's "budget keys = registered sources" test
  fails for the three new `registry/sources/*.yaml` and live runs are refused. Optional: a theme rule
  `{domain: contention, theme: politics}` (the catalog sets `theme: politics` per concept meanwhile).
* **CAUSCORE**: deliveries follow `docs/dev/v3_causal_interface.md`; relations `coded_outcome`, `reaction_to` only.
* **EVENTS**: map `contention:event_type` values (`resistance_campaign`, `contentious_episode`, `protest`,
  `state_repression`, `state_response`, `coded_outcome`) onto `registry/events/taxonomy.yaml`; `same_as` across
  NAVCO 1.3 / MEC / Wikidata needs an evidenced mapping table.
* Shared code used: KGGEOPOL `atlas_geopol_common` (KG records/loader, AtlasRows), CAUSDATA `causdata_common`
  (`ClaimRecord`, `ClaimDelivery`, `deliver_claims`), WDHARVEST allow-list for P580/P582/P585.

## 9. Gaps

MMP stops in 2020 and MEC in 2018 (no open successor checked); NAVCO 1.3 has no
repression variable (only 1.2 / 2.1 / MEC do); participation figures are conservative provider estimates
("thousands" = 2,001); MMP participants are kept as reported text ("1000s", "50+"), not converted.
