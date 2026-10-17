# V6 POLITICS — elections, cabinets and dated policy decisions as events

Agent V6POLITICS, 2026-10-17. Goal (v6_plan.md): dated political decisions and elections as events for prediction —
elections (with parties and outcomes), cabinet changes, policy events (OxCGRT), central-bank rate changes (derived from
BIS daily policy rates), tax reforms from structured open data. Status: **connectors, registry entries, fixtures and
tests committed; live loads queued** — free disk fell to 4.6–4.9 GB during this session (< 5 GB rule), so no live
ingest was run. All numbers in §2 are dry runs of the parsers on the **full raw files** fetched for inspection
(2026-10-17), not database counts.

## 0. For predictors — what to read

| Need | Where (stable names) |
|---|---|
| Election events (EU/OECD, 1900–2023), party vote shares & seats | KG `parlgov:election:<election_id>` (P585 day, P17 country, `parlgov:party_result` → `parlgov:party:<id>` with `vote_share_percent`, `seats`); atlas `parlgov.elections` (A-SOCIETY, already loaded) |
| Election rounds worldwide 1945–2010 with coded context (opposition allowed, incumbent lost, protests, monitors …) | KG `nelda:election:<electionID>` (P585, P17, `nelda:coded_answer` ×16 variables); atlas `nelda.election_rounds` (all 58 variables), `nelda.elections_cy` |
| Cabinet changes (EU/OECD) | KG `parlgov:cabinet:<cabinet_id>` (P580 day it took office, `parlgov:cabinet_party`, `parlgov:prime_minister_party`, `parlgov:caretaker`, `parlgov:previous_cabinet`, `parlgov:after_election`); atlas `parlgov.cabinets` (per start day), `parlgov.cabinets_cy` |
| Cabinet changes / leader changes worldwide (177 countries, 1966–2025) | atlas `whogov.government_starts` (`GOVERNMENT_START`, `LEADER_START`, one-day periods), `whogov.cabinet_cy` (July snapshots: size, women, military, retention, age, parties, leader, regime category); KG `whogov:government:<ISO3>:<start>` (P580/P582/P17) |
| Daily pandemic-policy stringency (185 countries, 2020–2022) | atlas `oxcgrt.indices_daily` (4 indices) |
| Dated policy-change events (OxCGRT) | atlas `oxcgrt.policy_changes` (DERIVED: level/flag changes per indicator and day, previous level/date, direction) and `oxcgrt.fiscal_announcements` (USD on announcement days) |
| Central-bank policy-rate changes (49 areas, 1945–2026) | atlas `bis.cbpol_d_changes` (DERIVED from `bis.cbpol_d`: new rate, previous rate/date, change in pp, `held_until`, BIS COMPILATION text) |
| Statutory CIT rate changes (OECD CTS, 2000–2026) | atlas `oecd.cts_cit_changes` (DERIVED from `oecd.cts_cit`: `CIT_C_CHANGE|…` combined, `CIT_CHANGE|…` central government) |

Event typing (`registry/events/taxonomy.yaml`, dataset kinds): `parlgov:election:` and `nelda:election:` → `election`;
`parlgov:cabinet:` and `whogov:government:` → new dataset type `cabinet_formation` (no Wikidata class). So
`reva.events.view.list_events(types=["election"], country=…, start=…, end=…)` lists them with their P585/P580 dates.
Taxonomy property roles: `parlgov:party_result` = candidate, `parlgov:cabinet_party` = participant,
`parlgov:previous_cabinet` / `previous_election` / `after_election` = structure *follows*.

**Caveats predictors must honour**
* A row of `bis.cbpol_d_changes` is a change in the BIS series, **not necessarily a policy decision**: for periods where
  BIS compiles a market rate (e.g. the US series before the target-rate period, AR, HR, NZ, AU …; 5,701 US and 4,815 HR
  changes) the series moves daily. Filter with `bis_compilation` (verbatim BIS text) and `held_until`.
* `CIT_C` (combined central + sub-central) moves every year for countries with averaged sub-central rates (USA);
  `CIT` (central) is the clean statutory series (e.g. USA 35 → 21 in 2018).
* `oxcgrt.policy_changes` are changes in OxCGRT's daily coding, not legal acts; the first recorded level of each
  series is not a change (counted, not emitted); gaps are flagged (`gap_days`).
* NELDA codes are for the round ("Was the incumbent replaced?" is the status at the end of that round, codebook p. 3).
* WhoGov government dates are "based on Döring (2019) or Bértoa (2020)" and leader dates "rely on Archigos" (WhoGov
  codebook) — not independent of ParlGov / Archigos. REVA never links WhoGov governments to ParlGov cabinets (no shared
  identifier; never by name or date).
* No causal statement anywhere: "cabinet after election" is ParlGov's parliament reference; party results and coded
  outcomes are values.

## 1. Sources (licence checked on every run)

| Source | URL | Licence | Fetched (bytes) | Connector / source key |
|---|---|---|---|---|
| ParlGov 2024 stable (Döring & Manow) | Harvard Dataverse doi:10.7910/DVN/2VZ5ZC: `view_cabinet.csv` (1,786,453), `view_party.csv` (258,195), `view_election.csv` (1,144,654), record JSON | **CC0 1.0** (record `license.name`; MD5 of each file vs record) | 3.2 MB | `atlas.parlgov_cabinets` / `parlgov.dataverse` (shares A-SOCIETY's key) |
| NELDA (Hyde & Marinov), Dataverse 2013 release | doi:10.7910/DVN/DDNWOR: `id & q-wide.tab` (3,697,425; tab export of the deposited .dta), `NELDA_codebook_2011.pdf` (197,673), record JSON; countrycode 1.9.0 (CRAN) for GW codes | **No licence stated**: record `license` empty, `termsOfUse` = "public", citation required → `commercial_use`/`redistribution`: not_granted (flagged like Archigos / P&T) | 3.9 MB (+ countrycode 0.5 MB, dedup) | `atlas.nelda` / `nelda.dataverse` |
| OxCGRT (Blavatnik School, Oxford) | `raw.githubusercontent.com/OxCGRT/covid-policy-dataset/main/`: `data/OxCGRT_compact_national_v1.csv` (43,837,034), `README.md`, `LICENSE.txt`, `documentation_and_codebook.md` (102,805) | **CC BY 4.0** (LICENSE.txt legal code + README sentence, both checked) | 44 MB | `atlas.oxcgrt` / `oxcgrt.github` |
| WhoGov Version 4 (Nyrup, Bramwell et al.) | DataverseNO doi:10.18710/OLPOZK: `WhoGov_crosssectional_V4.0.csv` (3,720,647), `0_ReadMe.txt`, `whogov_codebookandappendix.pdf` (300,524), record JSON | **CC BY 4.0** (record + ReadMe "Licenses/Restrictions: CC BY 4.0."; MD5s vs record) | 4.0 MB | `atlas.whogov` / `whogov.dataverseno` |
| BIS daily policy rates (input) | atlas `bis.cbpol_d` (PRED-HF, already in the dev DB) | BIS terms of permitted use (cite BIS) | 0 (no request) | `atlas.bis_cbpol_changes` / budget `bis.bulk` |
| OECD Corporate Tax Statistics (input) | atlas `oecd.cts_cit` (A-FISCAL, already in the dev DB) | OECD open by default (CC BY 4.0) | 0 (no request) | `atlas.oecd_cit_changes` / budget `oecd.sdmx` |

Raw total of a full live run: **≈ 55 MB** (≤ 100 MB budget). Budgets: `registry/atlas/budget_requests/v6politics.yaml`,
merged as **budgets-1.3.34** (new `nelda.dataverse` 10/day, `oxcgrt.github` 8/day, `whogov.dataverseno` 12/day;
`parlgov.dataverse` 6 → 10) and re-pinned in `src/reva/autonomy/policy.py`. Sources: `registry/sources/{nelda_dataverse,
oxcgrt_github,whogov_dataverseno}.yaml`.

**Not ingested (and why)**
* **NELDA 2011–2020 releases** (nelda.co): "Access to data available under the contact heading" — on request from the
  PIs only. Owner decision: request access (and a licence) from Hyde & Marinov.
* **Constituency-Level Elections Archive (CLEA)**: download behind registration, no open licence (A-SOCIETY reached the
  same conclusion). Owner decision.
* **IFES ElectionGuide**: website only, no bulk data or open licence found; not scraped.
* **WhoGov individual-level file** (`WhoGov_within_V4.0.csv`, 82 MB): over this container's disk budget; queued (§5).
* **OxCGRT** `ConfirmedCases`/`ConfirmedDeaths` (third-party epidemiological series), `MajorityVaccinated`/
  `PopulationVaccinated` ("We primarily source this data from Our World In Data"), V1–V4 vaccine categories,
  subnational files, E/NV/V variants — not OxCGRT's own national ordinal coding or not levels.
* **Pension reforms**: no structured open dataset with dated reforms found (OECD Pensions at a Glance tables are
  parameters, not dated events); EFFECTS-MORE holds attributed pension-age reform effects. **PIT top rates**
  (OECD Table I.7) are a straightforward next derived table once fetched via `oecd.sdmx`.
* No Wikidata query (as instructed).

## 2. Tables, entities, statements (dry runs on the full files, 2026-10-17)

| Output | Rows / records |
|---|---|
| KG ParlGov | 4,356 entities (1,707 parties, 1,028 elections, 1,621 cabinets), 38,114 statements: 9,016 `party_result`, 4,356 P17, 4,016 `cabinet_party`, 1,621 P580, 1,028 P585, 1,584 `prime_minister_party` (37 cabinets have no PM-party row in ParlGov: counted, not invented), 1,585 `previous_cabinet`, 991 `previous_election`, 1,387 `left_right`, 490 Manifesto ids; all 37 ISO3 codes resolve to a QID |
| atlas `parlgov.cabinets` / `parlgov.cabinets_cy` | 6,484 / 1,443 cells (1,621 cabinets 1900-05-27 … 2023-06-26) |
| KG NELDA | 2,976 entities (2,974 rounds + 2 `gw:` code entities), 62,454 statements; rounds: 2,003 legislative, 901 executive, 70 constituent assembly, 1945–2011; 1 month-precision date (Sudan 1974, mmdd 0400); unresolved GW codes kept as `gw:345` (2007–08) and `gw:816` (1976) |
| atlas `nelda.election_rounds` / `nelda.elections_cy` | 175,466 / 2,668 cells |
| atlas `whogov.cabinet_cy` / `whogov.government_starts` | 285,830 cells / 2,776 rows (1,037 government starts, 1,739 leader starts; 5 starts with two spellings, 16 leader starts with several names — all kept in flags) |
| KG WhoGov | 1,037 governments, 4,111 statements |
| atlas `oxcgrt.*` | fixture excerpt (2 countries, 2020): 1,483 change rows from 13 countries' first 3 MB; full file ≈ 203 k country-days → ≈ 0.8 M index cells, ≈ 20 k change rows (to be confirmed by the live run) |
| atlas `bis.cbpol_d_changes` | 33,099 rows, 49 areas (from the registered `bis.cbpol_d`, 638,911 rows) |
| atlas `oecd.cts_cit_changes` | 1,099 rows, 292 series (from the registered `oecd.cts_cit`) |

Examples (ids as loaded):
* `parlgov:cabinet:85` "Merkel I cabinet (Germany, from 2005-11-22)" —P580→ 2005-11-22; —P17→ Q183;
  —`parlgov:cabinet_party`→ `parlgov:party:1727` (CDU+CSU, seats 226, prime_minister 1) and `parlgov:party:558`
  (SPD, 222); —`parlgov:after_election`→ `parlgov:election:118` (2005-09-18; `party_result` SPD 34.2 % / 222 seats,
  CDU 27.8 % / 180 of 614); —`parlgov:previous_cabinet`→ `parlgov:cabinet:529`.
* `parlgov:cabinet:1661` Scholz (2021-12-08): SPD (PM party), B90/Grü, FDP; after `parlgov:election:1096` (2021-09-26).
* `nelda:election:220-1981-0426-P2` French presidential election, round 2, P585 1981-05-10, P17 Q142,
  `nelda:coded_answer` "Did the incumbent's party lose?" (nelda24) …
* atlas `oecd.cts_cit_changes` USA `CIT_CHANGE|targeting=ST|…` 2018: 21 (previous 35, change −14 pp).
* atlas `bis.cbpol_d_changes` XM 2026-09-16: 2.5 (previous 2.25), COMPILATION "From 18 Sep 2024 onwards: official
  central bank steering rate is the deposit facility rate …".
* atlas `whogov.government_starts` DEU `LEADER_START` 2025-05-06 "Friedrich Merz" (end dates, party `cdu`, snapshot
  years in flags; "relies on Archigos" per WhoGov).

## 3. Relation families (`registry/kg/relations_v4.d/v6politics.yaml`)

`parlgov:cabinet_party` → `has_part` (cabinet_includes_party); `parlgov:party_result`, `parlgov:prime_minister_party`,
`parlgov:after_election`, `parlgov:previous_cabinet`, `parlgov:previous_election` → `context`. Countries use Wikidata
`P17` (core mapping). `uv run python -m reva.kg.families check` → ok (23 families, 191 mappings). Predicates:
`registry/kg/predicates_politics.yaml` (registered by the loaders from that file).

## 4. Code, tests

* `src/reva/adapters/connectors/atlas_parlgov_kg.py`, `atlas_nelda.py`, `atlas_oxcgrt.py`, `atlas_whogov.py`,
  `atlas_politics_derived.py` (two specs), `v6politics_common.py` (predicates, bounded KG load, latest input table).
* Fixtures (real bytes fetched 2026-10-17, subsets, licence per manifest entry): `tests/fixtures/source/
  atlas.{parlgov_cabinets,nelda,oxcgrt,whogov}/`, `tests/fixtures/v6politics/` (atlas excerpts for the derived tables).
* `tests/unit/test_v6_politics.py` (10 tests: parsers, licence/record checks fail closed, change rows, registry);
  `tests/integration/test_v6_politics_ingest.py` (5 db tests: fixture ingests end to end, KG loaded without rejection,
  event card typed `cabinet_formation`, re-runs no-op, derived tables skip when the input is absent). All pass; ruff +
  mypy clean.

## 5. Owner decisions and commands to finish

Owner decisions: NELDA 2011–2020 (PI request), CLEA (registration, no open licence), IFES ElectionGuide (no open data),
WhoGov individual file (bigger machine), NELDA rights (no licence: non-commercial/internal use flagged).

Live runs (when `df -h /` shows ≥ 5 GB; each under the dev-DB lock; connector versions first):
```
flock /tmp/reva-devdb.lock uv run reva seed
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.parlgov_cabinets
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.nelda
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.whogov
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.oxcgrt          # 44 MB
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.bis_cbpol_changes   # no request; reads atlas.bis.cbpol_d
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.oecd_cit_changes    # no request; reads atlas.oecd.cts_cit
```
If OxCGRT reports an unmapped non-ISO code (e.g. Kosovo), add `registry/atlas/geo_aliases/v6politics.yaml` with the
archived row as evidence and bump `atlas.oxcgrt` to 1.0.1 with that scheme. Bigger machine: add
`WhoGov_within_V4.0.csv` (82 MB, ministers with portfolios → `whogov:minister` spells) and the OECD PIT top-rate table.
