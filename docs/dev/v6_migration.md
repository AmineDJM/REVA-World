# V6 MIGR — human mobility (asylum, refugees, migrant deaths, internal displacement)

Date: 2026-10-17. Agent: V6MIGR. Code: `src/reva/adapters/connectors/atlas_migration.py` (4 atlas sources). No DB
migration, no Wikidata query. Tests: `tests/unit/test_atlas_migration.py` (12),
`tests/integration/test_atlas_migration_ingest.py` (4, throwaway DB, fixture mode).

## What existed (extended, not duplicated)

| already loaded | owner | table(s) |
|---|---|---|
| UN DESA International Migrant Stock 2024, destination × origin × sex, 1990–2024 | A-UN (`atlas_un.py`) | `un_desa.ims2024` (672,719 cells), link kind `migrant_stock` |
| UNHCR population stocks (refugees, asylum-seekers, IDPs, stateless, returnees, OIP, …) origin × asylum, 1951–2025 | A-UN | `unhcr.pop_bilateral` (207,639), `pop_by_asylum`, `pop_by_origin` |
| IDMC disaster events + attributed displacements (KG, T1 `has_effect` claims) | CAUSMORE/FIXB (`atlas_idmc.py`) | KG `idmc:event:*`, `idmc:displacement:*` |
| Remittance inflows / outflows by country (BPM6) | A-WB | `wb.wdi_econ` `BX.TRF.PWKR.CD.DT` (received), `BM.TRF.PWKR.CD.DT` (paid), `BX.TRF.PWKR.DT.GD.ZS` |

So UN DESA IMS, the UNHCR stocks and the remittance country totals were **not** fetched again.

## New sources and tables

| connector (`source_key`) | provider, URL | licence | atlas table | fetched |
|---|---|---|---|---|
| `atlas.unhcr_asylum` (`unhcr.asylum`, new) | UNHCR Refugee Population Statistics API v1, `api.unhcr.org/population/v1/asylum-applications/` and `/asylum-decisions/` (`download=true`: ZIP of CSV, every year) + `limit=1` JSON check | CC BY 4.0 (pending maintainer review, as for `unhcr.population`) | `unhcr.asylum_applications`, `unhcr.asylum_decisions` | 2 ZIP (0.74 + 1.02 MB) + 2 JSON (< 1 KB) |
| `atlas.eurostat_asylum` (`eurostat.dissemination`, shared) | Eurostat `migr_asyappctzm` bulk TSV (`sdmx/2.1/data/migr_asyappctzm?format=TSV&compressed=true`) + OBS_FLAG code list | Eurostat reuse policy (CC BY 4.0) | `eurostat.asylum_applicants_m` | 9.9 MB gz (472 MB uncompressed, streamed) + 23 KB XML |
| `atlas.iom_mmp` (`iom.missing_migrants`, new) | IOM Missing Migrants Project via HDX (`missing-migrants-project-data`, resource `iom-missing-migrants-project-data.csv`) | CC BY-IGO (HDX record; IOM site 403 to non-browser clients → maintainer review) | `iom.mmp_monthly` | package_show + 7.4 MB CSV |
| `atlas.idmc_national` (`idmc.gidd`, shared with atlas.idmc) | IDMC GIDD national figures via HDX (`IDMC_Internal_Displacement_Conflict-Violence_Disasters.xlsx`) | **CC BY-NC 4.0** (workbook README; HDX says CC BY — the more restrictive applies; `rights.non_commercial: true`) | `idmc.gidd_national` | package_show + 0.48 MB xlsx |

Total raw per full run ≈ 20 MB (well under the 100 MB cap; JSON/CSV are stored zstd-compressed by DISKZ, the gzip/zip
ones as is). Budgets: `registry/atlas/budget_requests/v6migr.yaml` → `budgets-1.3.33` (unhcr.asylum 10/day,
iom.missing_migrants 8/day), digest re-pinned in `src/reva/autonomy/policy.py`.

### Table shapes (measured on the full provider files, 2026-10-17)

| table | cells | shape |
|---|---|---|
| `unhcr.asylum_applications` | 120,597 | asylum country (`provider_geo`, ISO3) × origin × year 2000–2025 × authority × application type × stage × cases/persons; indicator `applied\|origin=SYR\|authority=G\|app_type=N\|stage=NA\|count=P`; unit `persons` or `cases` |
| `unhcr.asylum_decisions` | 324,642 | same keys (no application type) × `dec_recognized` / `dec_other` (complementary protection) / `dec_rejected` / `dec_closed` / `dec_total` |
| `eurostat.asylum_applicants_m` | 2,872,383 | reporting country (34 incl. EU27_2020) × citizenship (206) × month 2008-01–2026-09 × applicant TOTAL / FRST (first time); sex T, age TOTAL, persons; indicator `asylum_applicants\|applicant=FRST\|citizen=SY` |
| `iom.mmp_monthly` | 34,900 | country of incident (151) or MMP region (16) × month 2014-01–2026-10 × cause of death (7 categories + ALL); `dead_and_missing` (REVA sum) and `incident_records` (REVA count) |
| `idmc.gidd_national` | 8,207 | country × year 2008–2024 × {conflict, disaster} × {IDP stock, internal displacements} × {rounded, raw} |

Live load status: see "Live run" below.

## Honesty decisions

* **UNHCR "0" is no figure.** The JSON API writes a cell without a figure as the *string* `"0"`; the CSV download
  writes it as `0`. For 2024 the CSV `0` cells of asylum-decisions equal the JSON string-`"0"` cells exactly
  (recognized 3001, complementary 4542, rejected 1907, otherwise closed 1778); so `0` is omitted and counted
  (`*_no_figure_omitted` in the report: 245,003 decision cells over all years), never stored as zero.
  asylum-applications has no `0`.
* **Download verified against the API.** Each ZIP is checked against the API's own `maxPages` (record count at
  `limit=1`) and `total` (column sums): 120,597 records / 40,727,399 applied; 113,929 records / totals of the five
  decision columns — exact match on 2026-10-17. A mismatch refuses the table (typed `incomplete`).
* **Codes**: only labels the API documentation gives are stated (authority G/J/U, application type N/R/A, stage
  NA/RA/AR, cases/persons — quoted verbatim in the coverage note); other codes (app_type FA, J, NA, NR, RA, SP, V;
  stage EO, FA, FI, IN, JR, SP, TA, BL, CA, TP, TR) are kept verbatim and listed as "without a documented label".
* **Eurostat**: `:` cells not stored (279,417, counted); reported zeros kept (2,469,081 — most citizenship × country
  cells are 0); flags `p` (8,652) and `b` (824) kept with the OBS_FLAG label. Citizenship aggregates (TOTAL,
  EXT_EU27_2020, EU27_2020, STLS stateless, UNK, RNC, UK_OCT) keep `counterpart_iso3 = null`.
* **IOM MMP cells are REVA aggregates of provider records**, labelled in `flags.aggregation`, with the CSV row numbers
  of every record in the selector. Month = month of `reported_date` (codebook: "Estimated date of death"; a
  "cumulative total" record carries the latest date of its range — quoted in the coverage note). `cause_death` is a
  cause of *death* (drowning, violence, …), never a cause of migration and never a causal claim. One provider total
  "1,022" is re-spelled `1022` with `provider_text_respelled`. 20 pairs of fully identical records are counted as
  published and listed in the coverage note. Country names are mapped only by **exact** equality (case-fold, whitespace
  collapse, HTML character reference `&#039;` decoded) with one crosswalk name field of exactly one entity:
  `registry/atlas/geo_aliases/v6migr.yaml` (scheme `iom_mmp`, 151/151 names, each with its evidence line). Origins
  (`country_origin`, often "Unknown" or lists such as "Mali, Côte d'Ivoire, Guinea…") are not used: no dyadic link.
* **IDMC national**: rounded and raw columns are separate indicators (`|rounded=yes/no`); the five README definitions
  used (and the licence sentence) must be present verbatim in the archived workbook, else the table is refused (unit
  test alters one and checks the refusal). The trigger (conflict and violence / disasters) is IDMC's coding kept in
  the indicator name; **no new causal claim** (IDMC's event-level disaster → displacement claims stay atlas.idmc's;
  a country-level "conflict caused N displacements" claim would add nothing attributable beyond the definition).
  The SADD sheet (sex/age split modelled by IDMC from UN WPP) and the context texts are not loaded.

## Links store (registry/links/link_types.yaml)

| kind | status | source | direction / weight |
|---|---|---|---|
| `migrant_stock` | existing | `un_desa.ims2024` | origin → destination, persons (both sexes) |
| `refugee_flow` | **new, derivable** | `unhcr.pop_bilateral` `refugees\|origin=<ISO3>` | origin → country of asylum, refugees under UNHCR's mandate at 31 Dec (persons). The id says *flow* for the displacement relation; the weight is an end-year **stock** (stated in labels/notes). |
| `asylum_applications` | **new, derivable** | `unhcr.asylum_applications` | origin → country of asylum, claims lodged in the year, persons only (`require_dims count=P`; case-level series are never converted), one facet per authority × application type × stage (never summed) |
| `remittance` | **new, no_open_source** | — | see "Missing" |

All are `causal: false`, honesty "relié via / connected via …", weighted by the provider's own number, evidence =
raw sha256 + selector of the cell. Unit test over the fixture tables: `refugee_flow` gives `iso3:SYR → iso3:DEU`
(weight "100" on the synthetic row; stateless XXA counted `partner_unmapped`, a reported zero gives no link);
`asylum_applications` gives e.g. **Syria → Germany, 2015, facet authority=G, app_type=N, stage=NA: 158,657 persons**
(real UNHCR figure, `unhcr.asylum_applications`, CSV row cited).

UI (`ui/src/v3/lib/relation.ts`, `ui/src/v3/api/links.ts`): the "migration" lane of Pays ↔ Pays / Neurones now holds
`migrant_stock`, `refugee_flow`, `asylum_applications`, `remittance`; NLQA link group `migration` likewise
(`src/reva/nlqa/adapters.py`). The bundled static UI (`src/reva/api/static`) is not rebuilt by this agent (lead's
final pass). The links appear after the lead's `reva atlas snapshot` + `reva links materialise` (not run here, per
the common rules).

Example chain once materialised (figures from the fixture tables): Syria (SYR) —asylum_applications (2015,
authority=G, app_type=N, stage=NA: 158,657 persons; DEU 2015 decisions at stage NA: 97,656 recognised, 101,937 total)→
Germany (DEU); Syria —refugee_flow (UNHCR pop_bilateral, end-year stock)→ each country of asylum; Eurostat: first-time
Syrian applicants in Germany, October 2015 = 30,215 persons (`eurostat.asylum_applicants_m`). Syria's
`idmc.gidd_national` 2023: 7,248,188 conflict IDPs (raw; rounded 7,248,000), 174,420 conflict internal displacements,
702,205 disaster internal displacements.

## Missing, and why

* **Bilateral remittance matrix (World Bank / KNOMAD)**: KNOMAD closed in 2024; the 2021 matrix URL
  (`knomad.org/sites/default/files/2022-12/bilateral_remittance_matrix_2021_0.xlsx`) now redirects to a World Bank
  brief; the World Bank data-catalog search API answered 429; the Center for Global Development 2024 re-run
  (Huckstep & Clemens) answers 403 to non-browser clients (not worked around). Both matrices are **modelled
  allocations** of aggregate inflows over migrant stocks and incomes — not observed transactions, and partly derived
  from the IMS stocks already loaded. **Owner decision**: supply the file (World Bank 2021 matrix or CGD 2024) and its
  licence; the connector then needs a builder with a mandatory "modelled, not observed" caveat in every link basis.
  Country totals are in WDI (above).
* **IOM missing migrants by origin → incident country**: origins are free-text lists or "Unknown" (9,634 of 23,720
  records); no dyadic link is built.
* **UNHCR demographics / solutions endpoints** (sex-age breakdown, resettlement, naturalisation) not loaded (scope).
* **Causal claims**: none new (see IDMC above); MMP causes of death are not causes of migration.

## Live run

Free disk was 4.7–4.9 GB during this session (< 5 GB): per the common rules the live loads were **not** run inline.
A gated script (`seed`, then each source only when free disk ≥ 5 GB, each under `flock`) was queued behind the
dev-DB lock; its outcome is recorded below when known. Every parser was run on the full live provider files fetched
during development (counts in the table above), and the fixture-mode ingest passes on a throwaway DB.

Commands (bigger machine or once ≥ 5 GB are free; ~20 MB raw, ~10 MB Parquet in total):

```
flock /tmp/reva-devdb.lock uv run reva seed                      # registers atlas_migration 1.0.1 (4 sources)
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.idmc_national
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.unhcr_asylum
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.iom_mmp
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.eurostat_asylum   # 2.87 M cells, ~25 s parse, ~1 GB RAM
# then (lead): reva atlas snapshot; reva links materialise  -> refugee_flow, asylum_applications links
```

Fixture-mode runs (no network): `--fixture --option unhcr_origin=SYR` (UNHCR), `--fixture --option
eurostat_key=M.PER.SY+UA.T.TOTAL+FRST.TOTAL.DE+FR+EU27_2020` (Eurostat), `--fixture` (MMP, IDMC).

Outcome of the queued run (21:18 UTC): `reva seed` exit 0 — connector versions `atlas.{unhcr_asylum,
eurostat_asylum,iom_mmp,idmc_national}@1.0.1` are registered in the dev DB; all four ingests **skipped: free disk 4,323
MB < 5 GB**. Nothing live was archived or registered by V6MIGR (0 bytes of raw archive used). Run the four
`reva atlas ingest` commands above once ≥ 5 GB are free; no other step is needed before the lead's snapshot.
