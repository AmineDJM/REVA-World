# V3 A-MILITARY — defence and security (UN peacekeeping; SIPRI, FAS, IISS assessed)

Agent A-MILITARY, 2026-10-16. Spec: `docs/roadmap/v3_world_graph.md`. One live, budget-metered ingest into a
throwaway database (`reva_test_*` from `tests/conftest.py`, dropped afterwards; the dev database `reva` was never
touched). Everything else is offline on recorded fixture slices.

## 1. Decisions per source (terms read 2026-10-16)

| Source | Terms (quoted / checked) | Decision |
|---|---|---|
| **SIPRI Military Expenditure Database** (1949–2025, file `SIPRI-Milex-data-1949-2025_v1.2.xlsx`, revised 2026-04-27) | sipri.org/about/terms-and-conditions (page sha256 `e0802c11…`): "Any reproduction—in any medium, electronic or printed—of the data requires authorization, except where this is covered by SIPRI's fair-use policy". Fair use = non-commercial **and** "the reproduction of less than 10 per cent of a published data set" ("Both the above conditions must apply"). Permission requests must include "your full name, where you are from, your affiliation"; commercial use (incl. "research or statistical content … services") needs a paid licence. The milex page repeats "Commercial use of SIPRI data must be authorized and licensed." | **Not ingested.** An atlas table is a full reproduction (> 10 %) served by an API; no authorisation exists, and a permission request needs a named person and affiliation (a maintainer decision, not an agent's). No `atlas_sipri.py` was written (a connector that must never run is a liability). Use the **World Bank WDI copies** already in the atlas (A-WB): `MS.MIL.XPND.GD.ZS` (% GDP), `MS.MIL.XPND.CD` (current US$), `MS.MIL.XPND.CN` (LCU), `MS.MIL.XPND.ZS` (% of general government expenditure) — catalog concepts `wb.ms.mil.*`, tagged `licence:check` ("SIPRI terms and conditions", series metadata). Constant-USD and per-capita series exist only in SIPRI's own file: **gap**. |
| **SIPRI Arms Transfers Database** (TIV, 1950–2025) | Same terms page; armstransfers page (sha256 `3468ce07…`): "Commercial use of SIPRI data must be authorized and licensed. For user rules … including the policy on fair use of the data, see the Terms and Conditions." | **Not ingested** (same reason). No bilateral supplier → recipient link material. The WDI copies carry country totals only: `MS.MIL.XPRT.KD` (exports) and `MS.MIL.MPRT.KD` (imports), in SIPRI TIV ("not a monetary value"; WDI: "A '0' indicates … between 0 and 0.5 million SIPRI TIV"), `licence:check`. **Top suppliers/recipients as reported cannot be listed from REVA data**; the WDI exporter/importer totals are the only REVA-held figures. |
| **UN Peace and Security Data Hub** (DPPA-DPO IMU, `psdata.un.org`): uniformed contributions, fatalities, missions | Each dataset's record on HDX, published by the Hub itself (organisation `unpeacesecurity`): `license_id: cc-by-igo` → **CC BY 3.0 IGO** (all 6 datasets used). The Hub web app's footer links the generic "Terms and Conditions of Use of United Nations Web Sites" (personal non-commercial use of site materials); the dataset-level licence set by the same publisher is the more specific statement and is applied (`registry/sources/un_psdata.yaml`, **maintainer review pending**). `peacekeeping.un.org` answers HTTP 403 to this container. | **Ingested**: `atlas.peacekeeping` (data from the Hub API, licence re-checked on HDX every run; a changed licence or publisher stops the run before anything is archived). |
| **FAS Status of World Nuclear Forces** (2026 estimates, 9 states) | fas.org page: "© 2026", no licence or reuse terms (fas.org/terms-of-use → 404); the inventory table is an image; FAS says the estimates "come with significant uncertainty". | **Not ingested** (no licence; no machine-readable table). |
| Nuclear weapons via **Wikidata** | CC0; Wikidata queries are WDHARVEST/KG* territory (`registry/kg/queries/**`, not owned by A-MILITARY). | Not done here; request in §7. Treaty status (NPT, CTBT, TPNW) is A-TREATIES. |
| **IISS Military Balance** | proprietary (subscription) | **Skipped.** Armed forces personnel are available as WDI `MS.MIL.TOTL.P1` / `MS.MIL.TOTL.TF.ZS` (A-WB; WDI sources them from IISS) and COW NMC `milper` 1816–2016 (A-HISTECON, `history.military_personnel`). |
| COW NMC military expenditure/personnel; CIA Factbook military fields; UCDP; UNGA votes; CShapes | owned by A-HISTECON / A-FACTBOOK / KGGEOPOL | Reference only (not duplicated). |
| IPI Peacekeeping Database (HDX, CC0) | CC0 | Not ingested: a third-party compilation of the same UN figures (1990–2021); the UN's own files are used. Alternative if the Hub goes away. |

## 2. What exists

| Piece | Path |
|---|---|
| Connector (`atlas.peacekeeping`, source key `un.psdata`, atlas source `unpk`) | `src/reva/adapters/connectors/atlas_peacekeeping.py` |
| Source registry | `registry/sources/un_psdata.yaml` |
| Catalog (11 concepts, domain `security`, theme `politics`) | `registry/atlas/catalog/security.yaml` |
| Geo schemes `unpk` (ISO3) and `unpk_location` (Field Missions `location` text → country, 19 evidenced aliases) | `registry/atlas/geo_aliases/a_military.yaml` |
| Budget request (`un.psdata`: 30 requests / 100 MB per day) | `registry/atlas/budget_requests/a_military.yaml` |
| Fixtures (12 responses: 6 HDX records trimmed to licence/organisation/resource fields — user and maintainer identifiers removed — and 6 CSVs, 4 of them row slices with verbatim row text; 104 KB) | `tests/fixtures/source/atlas.peacekeeping/` |
| Tests | `tests/unit/test_atlas_military.py` (14, offline), `tests/integration/test_atlas_military_ingest.py` (2, `db`, fixture mode), `tests/integration/test_atlas_military_live.py` (1, `live and db`) |

Run: `uv run python -m reva.atlas.ingest atlas.peacekeeping [--dataset contrib_monthly|contrib_pre2010|contrib_annual|fatalities|totals] [--fixture] [--option kg=false]`.
All requests go through `run.get` (GET only; no client built in the module; invariant-30 `test_every_live_provider_request_path_is_budgeted` passes). Raw bytes are archived after parsing and licence checks succeed.

## 3. Outputs and coverage (live run 2026-10-16)

Budget: policy `budgets-1.3.5` + the requested `un.psdata` limit overlaid by the test (not yet merged): reserved 14,
**consumed 12 requests, 46,577,669 bytes**, 0 refused. 12 raw artifacts archived (46.6 MB); 5 Parquet tables 2.8 MB.

| Atlas table | Rows | Countries (crosswalk) | Period | Content |
|---|---:|---:|---|---|
| `unpk.contrib_monthly` | 364,424 | 150 | 2010-01-31 → 2026-07-31 (199 month-end dates) | `uniformed_personnel\|mission=<acronym>\|type=<Troops, Staff Officer, Experts on Mission, Individual Police, Formed Police Units>\|sex=<male\|female>`, persons at the month's last reporting date (day period) |
| `unpk.contrib_pre2010` | 125,237 | 143 (+ USSR, Czechoslovakia, Yugoslavia/FRY/SCG via QIDs) | 1990-11-30 → 2009-12-31 (230 dates) | `uniformed_personnel\|mission=<m>\|type=<Troops, Civilian_Police, Observers, Individual_Police, Formed_Police_Units, Experts_on_Mission>` |
| `unpk.contrib_annual` | 12,042 | 150 | 2010 → 2026 (2026 flagged incomplete) | provider annual averages by type and sex |
| `unpk.fatalities` | 2,801 | 129 | 1948 → 2025 | `fatalities\|mission=<m>\|type=<Malicious Act, Accident, Illness, Other, To Be Determined>` by casualty nationality |
| `unpk.totals` (**computed**) | 81,837 | 171 | 1948 → 2026 | `uniformed_personnel_total` (+`\|sex=female`) per country and reporting date; `annual_average_uniformed_personnel_total` (+`\|type=…`, `\|sex=female`); `fatalities_total` (+`\|type=…`) |

Unmapped provider geographies (kept, listed in the manifests): `RUS` 25 / `CZE` 45 / `SRB` 183 rows of the
pre-2010 file (dates when the state was the USSR, Czechoslovakia, Yugoslavia, FR Yugoslavia or Serbia and Montenegro
→ `entity_qid` Q15180 / Q33946 / Q36704 / Q838261 / Q37024); fatalities `YUG` 10, `SCG` 6, `RUS` 1 and `SRB` 1
(transition years 1991 / 2006), `ZZ [Undetermined]` 11, `Occupied Palestinian Territory` 4 (no ISO code in the
file; not guessed), one blank nationality row.

**KG** (source key `un.psdata`, one `load_batch` pass, 2 batches): 105 entities (mission entities
`unpk:mission:<acronym>`), **20,430 statements, 0 rejected**:
`unpk:contributed_personnel_to` 20,081 (country → mission per calendar year; start/end = first/last reporting date
of that year; qualifiers: reporting dates, max uniformed personnel at one date (REVA sum), personnel types, dataset),
`unpk:mission_country` 91 (mission → country as reported: 69 from the pre-2010 file's `Mission_Country`, dated by
the first/last reporting date with that coding; 22 from the Field Missions Dashboard `location` through the
`unpk_location` aliases), `unpk:start_date` 71, `unpk:hq_location` 71, `unpk:end_date` 60 (provider: "Mandate
expiration date"), `unpk:initial_mandate` 29 (e.g. `S/RES/425 (1978)`), `unpk:lead_entity` 27. Countries are
Wikidata QIDs from `registry/entities/country_qids.json`; historical states use the QIDs above.
Not mapped to a country (reported in the run summary): UNDOF "Golan Heights", UNISFA "Abyei", UNMOGIP
"India / Pakistan", UNTSO "Middle East", and the regional offices UNOCA, UNOWAS, UNRCCA.

### Figures as reported (for orientation; every number is in the tables with its evidence)

* 2025 annual averages (computed sum over types and sexes): Nepal 5,837; Rwanda 5,802; Bangladesh 5,516;
  India 5,228; Pakistan 2,641; … China 1,831 — 119 contributing countries, 59,558 in total. Women: 6,003
  (Rwanda 666, Nepal 587, Bangladesh 414, Ghana 413).
* 2026-07-31: 44,819 uniformed personnel from 114 countries (Nepal 4,285, India 4,260, Rwanda 4,016, Bangladesh 4,003);
  largest missions MINUSCA 12,437, UNMISS 10,312, MONUSCO 8,744, UNIFIL 7,492.
* Peak in the files: 107,805 on 2015-04-30 (122 contributors); 10,329 on 1990-12-31; 69,931 on 1993-12-31.
* Fatalities 1948–2025: 4,482 (deduplicated); most by nationality India 183, Bangladesh 174, Pakistan 171,
  Nigeria 160, Ghana 156; worst years 1993 (252), 2010 (173), 1994 (168), 1961 (155).
* Totals from 2010 include special political missions present in the file (UNAMA, UNAMI, UNSOM, BINUH …).

## 4. Countries and historical states

Provider codes are ISO3 (all contributor codes are crosswalk keys). The provider back-codes dissolved states with
successor codes; `HISTORICAL_WINDOWS` keeps such dates unmapped with QIDs from
`registry/entities/historical_states_qids.json` (test-enforced):

| Provider code | Before | Historical state |
|---|---|---|
| RUS | 1991-12-26 | Soviet Union Q15180 |
| CZE, SVK | 1993-01-01 | Czechoslovakia Q33946 |
| SRB, MNE, YUG | 1992-04-27 | Yugoslavia Q36704 |
| SRB, MNE, YUG, SCG | 2003-02-04 | Federal Republic of Yugoslavia Q838261 |
| SRB (MNE 2006-06-03), SCG | 2006-06-05 | Serbia and Montenegro Q37024 |

`YUG`/`SCG` after their windows never map. A period that straddles a change (a calendar year such as RUS 1991,
SRB 2006) gets neither a country nor a QID (`state_transition_in_period`). The same rule gives the pre-2010
`Mission_Country` statements: UNMIK, coded `SRB` by the provider, is "Serbia and Montenegro" in 2005 and "Serbia"
in 2009 (and "Kosovo" in the Field Missions Dashboard) — each statement cites its own field; none is a statement
about sovereignty.

## 5. Provider defects handled (never silently)

* Pre-2010 file: from late 2009 the new categories duplicate the legacy ones — verified on all 1,041 rows that carry
  them: `Civilian_Police = Individual_Police + Formed_Police_Units` and `Observers = Experts_on_Mission` (0
  exceptions). Cells kept verbatim with `flags.category_overlap`; totals add only `Troops + Civilian_Police + Observers`.
* Fatalities: 179 (nationality, mission, type, year) groups are repeated identically under several mission ids
  (UNEF under ids 44/45/46, UNAVEM under 50–53, UNOSOM …) → one cell, `flags.provider_repeated_rows` (rows and ids);
  conflicting repeats would get no value (`missing: unreliable`) — none found. Caveat: if two operations sharing an
  acronym really had identical counts the same year, this undercounts; the flag says so.
* Annual file: personnel type `Troops ` (trailing space, 107 rows, 2017–2019) is the same type; the indicator uses the
  trimmed name and `flags.personnel_type_as_published` keeps the original.
* Monthly file: 36 empty cells → no row; reported zeros (e.g. `female_personnel = 0.0`) kept as reported; reporting
  dates 2019-10-30 and 2024-10-30 are not month ends (kept).
* PKO list: coordinates published with a decimal comma, some with latitude/longitude apparently swapped (MINUSMA,
  UNAVEM I/II, UNOSOM): only kept verbatim as qualifiers of `unpk:hq_location`, never used or corrected.
* Field Missions Dashboard: 4 trailing note/blank rows skipped (counted); "Cote d'Ivore" (sic) aliased with evidence;
  "UN Verification Mission in Colombia" is listed by name (contributions use `UNVMC`): two mission entities, not
  merged (no guessing).

## 6. Links (for LINKS / NLQA / UI3)

Path shape: `country —unpk:contributed_personnel_to→ unpk:mission:UNIFIL —unpk:mission_country→ Lebanon`, dated per
year, e.g. "France contributed uniformed personnel to UNIFIL (2010-01-31 … 2010-12-31)". The connection is a
deployment under a UN mandate; it says nothing about alliances or positions.

## 7. Requests to other agents / lead

1. **ATLASCORE/ATLASOPS**: merge `registry/atlas/budget_requests/a_military.yaml` (`un.psdata`, 30/day). Until then
   `test_budget_keys_are_registered_sources_and_cover_every_connector` lists `un.psdata` among the registered sources
   without a budget (it already fails for other agents' pending keys) and live runs are refused (fail closed).
2. **LINKS**: add `unpk:contributed_personnel_to` (fr "a fourni du personnel en uniforme à", inverse "a reçu du
   personnel en uniforme de") and `unpk:mission_country` ("pays de la mission (selon l'ONU)") to `statement_edges` in
   `registry/links/link_types.yaml`; optionally a derived `peacekeeping_contributor_to_host` (country → host country
   via a mission, weight = max personnel from the statement qualifier, not a new score).
3. **Lead / maintainer**: SIPRI data need written authorisation (named requester + affiliation; commercial use needs a
   paid licence). If granted, a connector for the milex xlsx and the arms-transfer trade registers can follow; until
   then the WDI copies (`licence:check`) are the only SIPRI-derived figures. Also confirm the CC BY 3.0 IGO reading of
   the Hub's HDX records vs the generic UN web terms.
4. **WDHARVEST / KGGEOPOL**: a Wikidata query spec for nuclear-weapon possession (states with a nuclear arsenal and
   dates of first test), if Wikidata models it consistently (property/class QIDs to be verified first); A-TREATIES
   covers NPT/CTBT/TPNW. Hub datasets with the same licence and good KG material: Security Council membership,
   vetoes since 1946, SC voting records (`dppa-scmembership`, `dppa-scvetoes`, `dppa-scvotes` on HDX).

## 8. Gaps and alternatives

* Military expenditure in constant USD, per capita and before 1960: only in SIPRI's file (not ingested); COW NMC
  (A-HISTECON) covers 1816–2016 in current currency.
* Bilateral arms transfers: no open source found with a usable licence (SIPRI only).
* Nuclear arsenals: no openly licensed estimates (FAS unlicensed; SIPRI Yearbook chapter under SIPRI terms).
* Peacekeeping before 1990-11: only fatalities (1948+) and mission dates; contributions by country are not in the
  UN files. Some months are missing in the provider data (provider caveat).
