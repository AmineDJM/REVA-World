# V3 A-SANCTIONS — sanctions between countries: episodes, coded outcomes, country pairs, regimes

Agent A-SANCTIONS, 2026-10-16. Connector `src/reva/adapters/connectors/atlas_sanctions.py` (four atlas sources,
one module), registry `registry/sources/{ties,eusanct,eu_sanctionsmap,ofac_programs}.yaml`,
`registry/atlas/catalog/sanctions.yaml`, `registry/atlas/geo_aliases/a_sanctions.yaml`,
`registry/atlas/budget_requests/a_sanctions.yaml`, fixtures `tests/fixtures/source/atlas.{ties,eusanct,
eu_sanctionsmap,ofac_programs}/`, tests `tests/unit/test_atlas_sanctions.py`,
`tests/integration/test_atlas_sanctions_ingest.py`. Contracts used: `docs/dev/v3_atlas_interface.md` §7,
`docs/dev/v3_causal_interface.md` (CAUSCORE `load_dataset_claims`), `docs/dev/v3_links.md` (dyadic rows).

## 1. Sources: access and licence check (2026-10-16)

| Candidate | Access | Licence / terms | Decision |
|---|---|---|---|
| **Global Sanctions Data Base** (Drexel/WIFO/Konstanz; R5 1950–2025, 1,794 cases) | only through a Mailchimp **request form** asking e-mail, first/last name, affiliation, country and intended use; data sent by e-mail | "Commercial use of the GSDB is prohibited. Please do not pass the data on to others" | **Not used**: personal data required to obtain it, no redistribution. Nothing requested. |
| **TIES 4.1** (Morgan, Bapat & Kobayashi 2014), 1945–2005, 1,412 cases | direct public download `TIESv4-1.xls` (0.49 MB), no form | no licence stated; citation requested | **Used** (`atlas.ties`), `open_licence: false`, cited on every row; flagged for maintainer review like COW. The five fixes of `TIESCorrection.txt` are already in v4.1 (checked on the file). |
| **EUSANCT 1.0** (Weber & Schneider 2020), EU/US/UN senders, 1989–2015, 326 cases | KonDATA/RADAR DOI 10.48606/10, one tar (64.8 MB) generated on request | **CC BY 4.0** (record's rights statement) | **Used** (`atlas.eusanct`); only the case-level CSV member is parsed. Found while checking TIES/GSDB alternatives: it covers EU and UN senders with per-sender dates and the HSE effectiveness coding. |
| **EU Sanctions Map** `api/v1/regime` | public JSON (0.4 MB), 55 regimes | no licence stated (disclaimer only); facts = references to EU/UN official acts | **Used, regime level only** (`atlas.eu_sanctionsmap`); `api/v1/fsd`, travel-ban files and search (persons/entities) are never requested. |
| **UN SC consolidated list** (XML) | public | UN | **Not used**: it is a list of designated individuals and entities (personal data). UN regimes come through the EU map ("UN", "UN and EU" regimes with their S/RES references). The UN SC sanctions pages answered HTTP 202 with an empty body (bot challenge) on 2026-10-16. |
| **US OFAC** "Sanctions Programs and Country Information" | public page (66 kB) | public domain (17 U.S.C. 105) | **Used, program level only** (`atlas.ofac_programs`); SDN / consolidated lists never requested. |

No source above is fetched at individual level; free-text columns that may name people (TIES `threatid*`
coder notes and `otherissue`; EUSANCT `*_id`, `*_other`, `otherissue`, `comments`, `link`) are not parsed into
any output (unit test `test_no_free_text_or_personal_fields`).

The TIES workbook is legacy Excel (BIFF8 `.xls`) and no xls reader is a dependency: the module carries a bounded
reader (OLE2 + BIFF8 records, first sheet, cell text, formulas never evaluated, size/cell caps), validated cell for
cell against `xlrd` on TIES 4.1 and on the EUSANCT case-level workbook (0 differences in 92k + 26k cells).

## 2. Events (KG, `kind = sanction_episode`)

One entity per case, `ties:case:<caseid>` / `eusanct:case:<caseid>` (dataset ids, never merged), with context
statements only (predicates `ties:*` / `eusanct:*`, registered by the connector):

* `sender` (item: country QID, a `cow:<n>` state-code entity for historical states, or `sanctions:org:eu` /
  `sanctions:org:un`) with start/end, `primary_sender` (TIES), per-sender `threat` / `imposition` (EUSANCT);
  `target` (item) with start/end; `cow_code` and `end_basis` qualifiers;
* `P580` / `P582` (Wikidata start/end time, WDHARVEST's own property definitions, as `atlas_cow` does) — the
  dates the event view and the causal grader read; TIES `ongoing_as_of` (last reported incident);
* `threat_issued`, `sanctions_imposed`, `imposition_start` (TIES: `stage_sequence` qualifier saying the threat
  stage precedes the imposition *within the same case — context, not a causal claim*);
* `issue` (sender objective as coded, provider category verbatim + code: TIES 15 categories, EUSANCT 14),
  `sanction_type_threatened` / `sanction_type_imposed` (provider category + code + a REVA `family` from the table
  below, labelled `family_basis: REVA grouping`), `institution_igo_code` (COW IGO code verbatim),
  `multilateralism` (EUSANCT), `final_outcome` (category + code + stage), `hse_policy_result`,
  `hse_sanction_contribution` (strings, EUSANCT);
* `outcome_record` (item) case -> `ties:outcome:<id>` / `eusanct:outcome:<id>` (`kind = coded_outcome`, label =
  the coded category, `P585` = the coded case end — the outcome is the nature of the termination, so it is
  dated only when an end is coded); `policy_result_record` case -> `eusanct:policy_result:<id>` (sender goals,
  HSE policy result, undated);
* `eusanct:same_case_as` -> `ties:case:<id>` only when the case id is identical, EUSANCT's own `datasource`
  names TIES and the target COW code is the same (84 links; 5 shared ids with a different target and 78
  "TIES" cases whose id is absent from TIES 4.1 are counted, not linked). Never by dates or names.

REVA type families (lookup, provider category always kept): trade = total/partial embargo, import/export
restriction, blockade; financial = asset freeze (TIES), major/targeted financial sanction (EUSANCT); aid =
termination of foreign aid; travel = travel ban; economic_agreement = suspension of economic agreement
(EUSANCT: and exclusion from organisations); diplomatic (EUSANCT 11); arms = arms embargo (EUSANCT 12; TIES has
no arms category); unspecific / other.

Regimes and programs: `eusanctionsmap:regime:<id>` (`sanction_regime`: listed_under_country, adopted_by,
measure_type, legal_act number + type + URL, thematic_programme, last_amended / expires — map timestamps are
local midnights in Europe/Brussels — and `listed_in_force` = retrieval day) and `ofac:program:<slug>`
(`sanction_program`: program_country only when the title names one country, imposed_by USA, last_updated,
listed_active).

Codes: COW state numbers through KGGEOPOL's date-aware `StateCodeCrosswalk` (countrycode 1.9.0, archived with
every run; historical states keep iso3 null and their verified QID). TIES/EUSANCT code **1000 = EEC/EU acting
collectively** (TIES manual, variable 5) -> `sanctions:org:eu`, never a country.

## 3. Causal claims (T1, `kg.causal_claim` through `reva.causal.load_dataset_claims`)

| Relation | cause -> effect | Rule | Codebook term (quoted verbatim in `Codebook.definitions`) |
|---|---|---|---|
| `coded_outcome` | `ties:case:<id>` -> `ties:outcome:<id>` | every case with a coded `finaloutcome` (1,024 of 1,412) | TIES users' manual v4, variable 39 "Final Outcome" (PDF sha256 `862fe788…`) |
| `coded_outcome` | `eusanct:case:<id>` -> `eusanct:outcome:<id>` | every case (326) | EUSANCT case-level codebook 2.0, variable 30 (PDF sha256 `8d8da4c5…`) |
| `contributing_factor`, polarity `promotes` | `eusanct:case:<id>` -> `eusanct:policy_result:<id>` | HSE success score >= 9 (133 cases) — the codebook's own threshold: "A score of 9 or higher implies that the sanction made a substantial contribution to the goals of the sender" (= contribution >= 3 and policy result >= 3) | variables 35-36 |

Dataset names `ties_case` / `eusanct_case`, complete deliveries (a case dropped by the source is retracted),
selectors `{"file", "row", "columns"}`, the archived raw sha256; grading is run after the load when
`reva.causal.grade_claims` exists. Full-data run: 1,483 claims, all grade B (T1, single source); temporal checks
1,199 consistent, 19 overlapping, 264 undetermined (no coded end, or the undated policy-result record) and **1 E
(`inconsistent`)**: EUSANCT case 2005031502 codes an end (2000-09-07) before its start (2005-03-15) — kept as coded,
flagged (`date_inconsistency` qualifier, `end_basis: coded_end_before_start`), never corrected.

Deliberately **not** produced:

* `reaction_to` / `coded_trigger` / `precipitated`: neither codebook codes a triggering *event*. Issue categories
  ("Release Citizens…", EUSANCT 12 "Violation of Constitutional Order: … imposed as a reaction to illegal seizures
  of state power") name a kind of target behaviour, not a dated event; pairing them with a coup dataset would be
  a join by dates, which the causal architecture forbids (CAUSDATA convention `no_join_claims`).
* TIES threat -> imposition: two stages of one case (the codebook defines the case as starting with the threat;
  an imposition means the threat did not succeed), stored as stage dates with a context qualifier, never a claim.
* HSE "negative" or "little or no" contribution, or score < 9: the codebook names no effect for them -> no claim
  (the coded values stay as statements).
* EUSANCT `threat_success` / `sanctions_success` / `success` are derived by EUSANCT from `finaloutcome`: they
  are qualifiers of the outcome record, not separate claims (no double counting).

## 4. Atlas tables (computed counts; a missing row = no coded case, never a zero)

| Table | Indicators | Row / partner |
|---|---|---|
| `ties.dyad_year`, `eusanct.dyad_year` | `CASES_STARTED`, `CASES_ACTIVE`, `IMPOSITIONS_ACTIVE` `|partner=<ISO3>` (else `|partner_code=cow:<n>`) | row = sender (`cow:<n>`, `org:EU`, `org:UN`), `flags.partner_iso3` = target, `case_ids`, `end_basis`, `sender_dates` |
| `ties.country_year`, `eusanct.country_year` | `TARGET_CASES_STARTED`, `TARGET_CASES_ACTIVE`, `TARGET_IMPOSITIONS_ACTIVE`, `SENDER_CASES_STARTED` | country (historical states: iso3 null + `entity_qid`) |
| `eusanctionsmap.regimes` | `REGIME|regime=<id>` (value_text = adopted by; flags: specification, measures, legal acts, dates), `REGIMES_LISTED` | country the regime is *listed under* (may target persons/entities linked to it, e.g. the blocking statute under USA); thematic regimes `thematic:<code>`, iso3 null |
| `ofac.programs` | `PROGRAM|program=<slug>`; `ACTIVE_PROGRAMS|partner=<ISO3>` (row USA) | program title names one country (20 of 36) |

Active years: start year .. coded end year; TIES without end -> through its "ongoing as of" year (it can exceed
2005: the 1959 Cuba case was last reported ongoing in 2013); EUSANCT "ongoing" -> 2015 (end of coverage); no end
and no ongoing date -> start year only; end before start -> start year only (flagged). TIES and EUSANCT
additional senders (`sender2..5`) have no dates of their own: their rows use the case's dates
(`flags.sender_dates` says so) — e.g. Japan, the UK, West Germany and Israel are coded senders of the 1959-2013
TIES Cuba case.

Full-data counts (throwaway database, real captures of 2026-10-16): TIES 12,640 dyad-year rows (730 mapped
directed pairs) + 6,480 country-year rows, 16,723 KG statements, 2,437 entities; EUSANCT 5,781 + 3,123 rows (279
pairs), 6,599 statements; EU map 55 regimes, 90 rows, 762 statements; OFAC 36 programs, 56 rows, 128 statements.
0 KG rejections, 0 claim rejections. Catalog: 7 country concepts (`registry/atlas/catalog/sanctions.yaml`, theme
politics).

## 5. LINKS: proposed link kinds (`registry/links/link_types.yaml`, owner LINKS)

```yaml
  - id: sanction_episode
    labels: {fr: "épisode de sanctions (menace ou imposition)", en: "sanctions episode (threat or imposition)"}
    inverse_labels: {fr: "cible d'un épisode de sanctions de", en: "target of a sanctions episode by"}
    scope: country_country
    directed: true
    derivation:
      method: atlas_dyadic
      sources: [ties, eusanct]
      atlas:
        source: ties                      # second entry with source: eusanct (same indicators)
        datasets: [dyad_year]
        indicator_prefix: "CASES_ACTIVE|partner="
        partner: flags.partner_iso3
        row_role: subject
        year_from: period_start
        companions: {impositions: IMPOSITIONS_ACTIVE}
        stored_in_full: true
        flow: "sanctions cases (threat and/or imposition) of the sender against the target active in the year"
    temporal: {kind: snapshot, description: "case-active year (start .. coded end; end basis in flags)"}
    weight: {kind: source_quantity, unit: cases, notes: "count of coded cases (REVA count of TIES/EUSANCT rows)"}
    honesty: {fr: "relié via : épisode de sanctions codé (TIES / EUSANCT) ; menace ou imposition, pas une cause",
              en: "connected via: coded sanctions episode (TIES / EUSANCT); threat or imposition, not a cause"}
    causal: false
    status: derivable
    path_cost: 1.5
  - id: us_sanctions_program
    labels: {fr: "programme de sanctions américain actif (OFAC)", en: "active US sanctions program (OFAC)"}
    scope: country_country
    directed: true
    derivation: {method: atlas_dyadic, sources: [ofac], atlas: {source: ofac, datasets: [programs],
                 indicator_prefix: "ACTIVE_PROGRAMS|partner=", partner: flags.partner_iso3, row_role: subject}}
    temporal: {kind: snapshot, description: "retrieval day of the OFAC program list"}
    causal: false
```

Organisation senders (`org:EU`, `org:UN`, TIES `cow:1000`) and EU-map regimes are organisation -> country: a
future `sanctions_regime_listed` kind (EU Q458 / UN Security Council Q37470) needs an evidenced org mapping, not
proposed here.

## 6. Requests to other owners

1. **ATLASCORE**: merge `registry/atlas/budget_requests/a_sanctions.yaml` (`ties.unc`, `eusanct.kondata`,
   `eu.sanctionsmap`, `ofac.programs`; invariant 30 `test_budget_keys_are_registered_sources_and_cover_every_connector`
   fails until merged, like for every new V3 source). Live runs are refused before that (fail closed), so the
   full-data run above used the real captures through a scratch fixture root.
2. **LINKS**: the two kinds of §5.
3. **EVENTS**: `dataset_kinds` for `ties:case:*`, `eusanct:case:*` -> `sanction_episode` (type id of the
   roadmap list); `eusanctionsmap:regime:*`, `ofac:program:*` are regimes/programs (not episodes);
   `eusanct:same_case_as` is evidence-based `same_as` material.
4. **CAUSCORE**: `reva/causal/__init__.py` re-exports nothing yet (this connector imports `reva.causal.model`
   as a fallback); `grade_claims` was not exported when tested.
5. **A-ECRISES**: `registry/sources/esrb_fcdb.yaml` (untracked, in progress) is invalid YAML (line 30: an
   unquoted `:` in `cadence`) and makes `load_registry` — hence the seed and every `db` test session — fail;
   the integration tests here were run in an isolated copy without that file.

## 7. Run

```bash
uv run python -m reva.atlas.ingest atlas.ties --fixture         # also atlas.eusanct, atlas.eu_sanctionsmap,
uv run python -m reva.atlas.ingest atlas.ties                   # atlas.ofac_programs (live after the budget merge)
uv run pytest tests/unit/test_atlas_sanctions.py
uv run pytest -m db tests/integration/test_atlas_sanctions_ingest.py
```

Run TIES before EUSANCT: the EUSANCT run reads the latest archived TIES workbook of the same origin to build the
`same_case_as` links (reported as skipped otherwise). The RADAR tar is regenerated on every download (tar mtimes),
so each EUSANCT download has its own raw sha256 and registers a new atlas version with identical cells.
