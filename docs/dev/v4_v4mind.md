# V4 V4MIND — psychology, behaviour, values, sociology (measured by populations)

Agent V4MIND, 2026-10-17. Scope: how populations feel, trust, spend their time, are cared for in mental health,
what national cultures score on value dimensions, what people work as, and psychology / sociology as sciences.
Everything below is what public sources state, aggregated by country (or by research topic); nothing is an
individual-level measurement, and **no correlation between these series is causal** (they are survey averages,
register counts and provider estimates; any co-movement across countries or years is an association only —
causal statements enter REVA only through the causal loader as attributed claims, `docs/roadmap/v3_causality.md`).

## Existing coverage reused, not duplicated

| Already in REVA (V3) | Where | V4MIND decision |
|---|---|---|
| Life evaluation (Cantril ladder, Gallup World Poll via World Happiness Report) | `atlas_whr.py`, `quality_of_life.yaml` | kept; OECD How's Life? life satisfaction added as a separate, differently-defined series |
| Suicide rates (WHO `MH_12`, `SDGSUICIDE`; UN SDG `SH_STA_SCIDE`) | `atlas_who.py`, `atlas_un.py` | not re-requested |
| Homicide (WHO GHE, WDI/SDG copies of UNODC) | `atlas_society_justice.py`, A-WB, A-UN | not re-requested (UNODC portal: UN copyright, no reuse) |
| Religion composition (RCS-Dem, COW WRP), fractionalisation, CPI, SIGI | `atlas_society_*.py` | not re-requested; Pew not needed (and account-gated) |
| Internet users (ITU via WDI, UN SDG `IT_USE_ii99`, Factbook, Eurostat NUTS) | A-WB, A-UN, A-FACTBOOK, GAPHUNT | not re-requested |
| PISA / PIAAC (How's Life? "knowledge and skills") | `atlas_pisa.py` (`oecd.hsl_skills`) | the other How's Life? measures about people's inner life are V4MIND's |
| Freedom of religion and civil liberties (V-Dem) | `atlas_vdem.py` | unchanged |
| OpenAlex works per country × domain/field | `atlas_openalex.py` (A-SCI) | extended to subfields through a subclass of A-SCI's query (no copy of the machinery) |
| ILOSTAT labour series | `atlas_ilo.py` (A-ILO) | A-ILO's connector class reused with occupation indicators |

## Sources (all fetched through `run.get` → archived raw bytes → parsed → registered)

| Connector (version) | Source / URL | Licence | Fetched (live, 2026-10-17) | Raw bytes |
|---|---|---|---|---|
| `atlas.oecd.wellbeing` (1.0.1) | OECD How's Life? `OECD.WISE.WDP:DSD_HSL@DF_HSL_CWB` and `@DF_HSL_FWB`, `sdmx.oecd.org` SDMX-CSV with labels | OECD T&C, CC BY 4.0 default since 2024-07 — **third-party survey upstream for some measures (e.g. Gallup World Poll): licence review** | 2 requests | 22.1 MB |
| `atlas.who_mental` (1.0.1) | WHO GHO OData `ghoapi.azureedge.net` (Mental Health Atlas `MH_*`, `GDO_q35`, `RSUD_1/2`) | CC BY 4.0 + WHO terms | 30 requests | 1.9 MB |
| `atlas.un_sdg_timeuse` (1.0.1) | UN SDG API v5 series `SL_DOM_TSPD`, `SL_DOM_TSPDCW`, `SL_DOM_TSPDDC` (indicator 5.4.1) | UNdata terms (free use with attribution) | 3 requests | 7.5 MB |
| `atlas.hofstede` (1.0.0) | geerthofstede.com dimension data matrix page + `6-dimensions-for-website-2015-08-16.csv` | **research use only, no open licence, no redistribution grant** (page sentence checked every run) | 2 requests | 0.07 MB |
| `atlas.ilostat_occupation` (1.0.1) | ILOSTAT `rplumber.ilo.org`: `EMP_2EMP_SEX_OCU_NB_A`, `EMP_TEMP_SEX_OCU_NB_A`, `HOW_TEMP_SEX_OCU_NB_A` + TOC + 5 dictionaries + `classif1` dictionary | CC BY 4.0 | 10 requests | 4.5 MB |
| `atlas.openalex_taxonomy` (1.0.1) | OpenAlex snapshot on S3 `openalex.s3.amazonaws.com/data/jsonl/{domains,fields,subfields,topics}/` (manifests + 10 gzip JSON-lines files, snapshot 2026-10-14) | CC0 | 14 requests | 7.4 MB |
| `atlas.openalex_mind` (1.0.1) | OpenAlex API group_by (works per country × year × subfield 3200-3207, 3312) | CC0 | **not run** (see "missing") | 0 |

Total archived raw ≈ 43 MB (limit 150 MB). New source keys: `hofstede.dimensions`, `openalex.snapshot`
(`registry/sources/`). Budgets: request `registry/atlas/budget_requests/v4mind.yaml`, merged as budgets-1.3.15
(digest re-pinned in `src/reva/autonomy/policy.py`). No e-mail or personal data in any request (generic UA; the
OpenAlex connector sends no `mailto`).

## What was loaded into the dev DB `reva` (origin `live`)

Atlas tables (682,021 rows):

| Table | Rows | Countries | Years | Catalog cells |
|---|---|---|---|---|
| `oecd.hsl_mind_current` | 57,765 | 47 | 2004–2025 | 6,704 |
| `oecd.hsl_mind_social_capital` | 16,188 | 47 | 2004–2025 | 2,126 |
| `who.gho_mental` | 4,052 | 194 | 2004–2017 | 2,171 |
| `un_sdg.sdg_timeuse` | 11,046 | 98 | 2000–2026 (survey years) | 828 |
| `hofstede.dimensions_2015` | 666 (161 typed missing `#NULL!`) | 101 | 2010 (edition year, flagged) | 666 |
| `ilostat.emp_2emp_sex_ocu_nb_a` | 260,442 | 189 | 1991–2025 | via source_order |
| `ilostat.emp_temp_sex_ocu_nb_a` | 228,417 | 208 | 1969–2025 | — |
| `ilostat.how_temp_sex_ocu_nb_a` | 103,445 | 172 | 1991–2025 | — |

Catalog `registry/atlas/catalog/mind.yaml`: 58 provider-declared concepts (domain `mind`, theme `behaviour`, ISCO
employment concepts theme `labour`): 23 OECD well-being/trust/time-use, 13 WHO mental health, 6 SDG time use
(women/men 15+), 6 Hofstede dimensions, 10 ILO employment by ISCO-08 major group. Definitions quote the provider
names verbatim (OECD `CL_MEASURE`, GHO indicator names, SDG series descriptions, Hofstede column names, ILOSTAT
labels).

KG (`kg.entity` / `kg.statement`, append-only, 4 load source keys), 122,239 live statements:

| Kind | Entities | Source key | Statements |
|---|---|---|---|
| `survey_wave` (+1 `atlas_table`) | 246 | `unsd.sdg_timeuse` | 975 (source text, reference year, `survey_in` country, `data_source_for` table) |
| `occupation` (ISCO-08 major 10 + sub-major 43) (+3 `atlas_table`) | 53 | `ilostat.classif1` | 123 (`isco:code`, `isco:part_of`, `ilostat:measured_by`) |
| `research_domain` 4, `research_field` 26, `research_subfield` 252, `research_topic` 4,516 | 4,798 | `openalex.snapshot` | 121,141 (part_of 4,794, Wikidata ids 282, Wikipedia URLs, 101,671 keywords with scores, works/citation counts at the snapshot date) |

Examples (ids as stored):

* **Time use**: `unsd_tus:484:2014:15d65fc522` "Encuesta Nacional de Uso del Tiempo (ENUT) 2014 by Instituto Nacional de
  Estadística y Geografía (INEGI)" → `unsd_tus:survey_in` (located_in) → `Q96` (Mexico); → `data_source_for` (context)
  → `atlas:un_sdg.sdg_timeuse`, whose cell `SL_DOM_TSPD|Age=15+|Location=ALLAREA|Reporting Type=G|Sex=FEMALE`, MEX,
  2014 = 23.667 % of a day (2019: 24.12, 2024: 21.40), concept `mind.time_use.unpaid_domestic_and_care_work.women`.
* **Occupations**: `isco08:26` "Legal, social and cultural professionals" → `isco:part_of` (part_of) → `isco08:2`
  "Professionals" → `ilostat:measured_by` (measured_by, qualifier `classif1=OCU_ISCO08_2`) →
  `atlas:ilostat.emp_2emp_sex_ocu_nb_a`; FRA 2025 = 7,409.545 thousand (ILO modelled estimate, concept
  `mind.ilo.employment_isco08_2`).
* **Psychology as a science**: `openalex:T10475` "Psychological Well-being and Life Satisfaction" → part_of →
  `openalex:subfield.3207` "Social Psychology" (`openalex:wikidata_id` Q161272) → part_of → `openalex:field.32`
  "Psychology" (Q9418) → part_of → `openalex:domain.2` "Social Sciences"; `openalex:subfield.3312` "Sociology and
  Political Science" carries Q34749. The Wikidata ids are identifiers OpenAlex publishes (for V4LINK's same-as
  work), never a merge.
* **Well-being**: `oecd.hsl_mind_current` `11_1|unit_measure=0_TO_10|age=_T|sex=_T|education_lev=_T` FRA 2024 =
  7.138569648 (0-10 scale; concept `mind.oecd.life_satisfaction`); `7_4` "Feeling lonely" JPN 2023 = 4.8 % of 16+;
  `14_1` "Trust in others" DNK 2025 = 5.7 (0-10).
* **Mental health services**: `who.gho_mental` `MH_6` psychiatrists per 100,000: FRA 2017 = 20.9069, IND 2016 =
  0.29235 (Mental Health Atlas, country-reported; flags `estimate_class: country_reported`).
* **Values**: `hofstede.dimensions_2015` JPN `mas` = 95, `uai` = 92, `pdi` = 54 (provider code `JPN`; Hofstede's
  `CHI` = China, mapped by name, never as Channel Islands).

## Relation families used (`registry/kg/relations_v4.d/v4mind.yaml`)

`unsd_tus:survey_in` → located_in; `unsd_tus:data_source_for` → context; `isco:part_of` → part_of;
`ilostat:measured_by` → measured_by; `openalex:part_of` → part_of; Wikidata `P2579` studied by, `P425` field of this
occupation, `P1269` facet of → context. None is causal (`reva.kg.families check`: ok). `measured_by` objects are
`atlas:<source>.<dataset>` entities of kind `atlas_table` (REVA-made keys, described as such).

## Wikidata (specs written, not harvested)

`registry/kg/queries/society/mind_psychological_phenomena.yaml` (items with MeSH tree code P672 in F01 Behavior and
Behavior Mechanisms / F02 Psychological Phenomena → `psych_construct`), `mind_mental_disorders.yaml` (MeSH F03 or a
DSM-5 code P1930 → `mental_disorder`; only identifiers/names Wikidata holds — the DSM itself is proprietary),
`mind_isco08_occupations.yaml` (items with ISCO-08 P952 → `occupation`). Selection is by identifiers, never by
label. Property requests `registry/kg/property_requests/v4mind.yaml`: P672, P486, P1930, P952 (external ids), P425.
`harvest validate`: 8/8 society specs valid. Not harvested: the Wikidata API answered HTTP 429 to every request on
2026-10-17, and the property ids were written from knowledge — the WDHARVEST merge check confirms them.

## Missing, and why

| Wanted | Status | Reason / evidence |
|---|---|---|
| World Values Survey / European Values Study aggregates | not loaded | downloads require a registration form (name, e-mail, purpose) and WVSA terms; no open aggregate file. Owner decision. |
| Global Preference Survey (Falk et al.: risk, patience, altruism, trust, reciprocity) | not loaded | `gps.econ.uni-bonn.de/downloads` requires name/organisation/e-mail, CC BY-NC-SA 4.0, and "these data must not be transferred to third parties without prior approval". Owner decision. |
| Hofstede | loaded, **flagged research-only** | see licence above; owner decides to keep (research) or drop |
| Gallup (trust, social connections) | not loaded | paid; only reaches REVA as published aggregates (WHR, some OECD measures) |
| OECD Time Use database (`OECD.WISE.INE:DSD_TIME_USE@DF_TIME_USE`, minutes/day by activity) | not loaded | the flow has **no time dimension** (no survey year in the data): values cannot be placed in time; SDG 5.4.1 used instead |
| OpenAlex works per country × psychology/sociology subfield | connector + tests ready, **no live run, no fixture** | OpenAlex API free per-IP budget exhausted (HTTP 429 "Insufficient budget … resets at midnight UTC"); an API key needs an e-mail address (owner decision) |
| WHO Mental Health Atlas 2020/2024 editions | not loaded | GHO API holds the 2011–2017 rounds only; later editions are PDF country profiles |
| `MH_21`/`MH_22`/`MH_23` (total MH workforce, occupational therapists, other doctors) | empty | no country row in the GHO API (requested, reported as empty) |
| ISCO-08 minor/unit groups and official definitions | not loaded | `webapps.ilo.org/.../isco/` answers 403 to non-browser clients; ILOSTAT publishes 1-2 digit groups only; Wikidata P952 spec covers the rest once harvested |
| APA Thesaurus, DSM texts | not loaded | proprietary |
| Pew religion practice datasets | not loaded | account + terms; religion composition already covered by V3 (RCS-Dem, WRP) |
| Psychological construct ↔ atlas series links (e.g. "loneliness" → `7_4`) | not made | no source publishes such an identifier link; a label match would be name mapping (refused) |

## Owner decisions

1. Hofstede dimension scores: keep for research use (flagged `research_only_no_redistribution_grant` on every cell)
   or remove the table from snapshots.
2. OECD How's Life? cells with third-party survey upstream (Gallup World Poll for some countries/measures): licence
   review before any redistribution.
3. WVS/EVS and GPS: register (account + terms; GPS is CC BY-NC-SA with a no-transfer clause) or leave out.
4. OpenAlex API key (requires an e-mail address) for `atlas.openalex_mind`, or wait for the free per-IP budget.
5. Region aggregates in SDG 5.4.1 (M49 9, 62, 202, 419, 513, 747, 753) and State of Palestine (275) became
   `survey_wave` entities without a `survey_in` country (no reviewed QID for 275 in `country_qids.json`).

## Commands to finish later (bigger machine, free disk ≥ 5 GB; dev DB writes under the lock)

```bash
flock /tmp/reva-devdb.lock uv run reva seed
# OpenAlex works per country for psychology / sociology subfields (after midnight UTC, or with OPENALEX_API_KEY set)
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.openalex_mind
# then record its fixture (2020 queries) with the same recorder pattern as tests/fixtures/source/atlas.openalex
# Wikidata: merge the property requests, then harvest the three specs (throwaway DB, imported by the lead)
uv run python -m reva.kg.harvest merge-property-requests
uv run python -m reva.kg.harvest run registry/kg/queries/society/mind_psychological_phenomena.yaml --via api
uv run python -m reva.kg.harvest run registry/kg/queries/society/mind_mental_disorders.yaml --via api
uv run python -m reva.kg.harvest run registry/kg/queries/society/mind_isco08_occupations.yaml --via api
# re-runs of the loaded sources are idempotent (atlas tables content-addressed, KG statements unchanged)
for s in atlas.oecd.wellbeing atlas.who_mental atlas.un_sdg_timeuse atlas.hofstede atlas.ilostat_occupation \
         atlas.openalex_taxonomy; do flock /tmp/reva-devdb.lock uv run reva atlas ingest $s; done
```

Not run by V4MIND (lead's job): `reva atlas snapshot`, `reva links materialise`, `reva links prune`.

## Code and tests

* Connectors: `src/reva/adapters/connectors/atlas_mind_{common,oecd,who,timeuse,hofstede,occupations,openalex}.py`
  (no HTTP client built; every fetch through `run.get`, archived before parsing).
* Registry: `registry/atlas/catalog/mind.yaml`, `registry/atlas/geo_aliases/v4mind.yaml` (scheme `hofstede`, 101
  aliases by country name, 10 regions/sub-national groups unmapped on purpose), `registry/kg/relations_v4.d/v4mind.yaml`,
  `registry/kg/queries/society/mind_*.yaml`, `registry/kg/property_requests/v4mind.yaml`, `registry/sources/{hofstede_dimensions,openalex_snapshot}.yaml`,
  `registry/atlas/budget_requests/v4mind.yaml`.
* Fixtures (real captures 2026-10-17, subsets documented per manifest entry): `tests/fixtures/source/atlas.{oecd.wellbeing,who_mental,un_sdg_timeuse,hofstede,ilostat_occupation,openalex_taxonomy}`.
* Tests: `tests/unit/test_atlas_mind.py` (16, parsing, drift refusals, licence sentence, name-only geo mapping,
  ISCO coding rule, OpenAlex hierarchy, subfield row builder on A-SCI's captured page) and
  `tests/integration/test_atlas_mind_ingest.py` (6, fixture ingest into the throwaway DB, KG links, idempotent re-runs):
  22 passed; ruff + mypy clean on the V4MIND modules.
