# V6 INTEG — integration fixes after V4 / V5

Agent INTEG, 2026-10-17. Brief: `docs/roadmap/v6_plan.md` (row INTEG) and the lead's FINAL4 todo. No data was
loaded; no belief release was published (see §3). Overall report: `docs/final_report_v4_v5.md`.

## 1. measured_by bridge (catalog concept vs atlas table entity)

**Problem.** V4CORE's `atlas:measured_by` points at `atlas:<catalog concept>` (qualifiers `concept`, `iso3`) and the
Fiche fetched `/v1/atlas/series?concept=…`. V4ECON (`econ:measured_by`, 12,010 statements), V4MIND
(`ilostat:measured_by`, 27) and V4ECO (`gbif_occ:measured_by`, not loaded yet) point at **table entities**
`atlas:<source>.<dataset>` (kinds `data_series` / `atlas_table` / `atlas_dataset`) with the selecting identifiers in
qualifiers (`table` + `provider_geo`; `atlas_table` + `classif1`; `indicators`). The Fiche sent e.g.
`concept=oecd.reg_pop` → "unknown atlas concept".

**Fix.**
* `src/reva/kg/families.py` `measured_by_target(object, qualifiers, kind)` → `kind: concept` (V4CORE convention,
  unchanged) or `kind: table` (`source`, `dataset`, `provider_geo`, `iso3`, `indicators`, `indicator_contains =
  classif1=<code>`). Only identifiers the statement carries; hostile values dropped.
* `src/reva/kg/family_query.py`: the `atlas` hint of every measured_by neighbour uses it (neighbour kind included);
  `AtlasHint` API model extended (`src/reva/api/routes/kg_families.py`).
* New `GET /v1/atlas/tables/{source}/{dataset}/cells?provider_geo=&iso3=&indicator=&indicator_contains=&limit=`
  (`AtlasQuery.table_cells`, `src/reva/api/routes/atlas.py`): per indicator counts / geographies / period range,
  cells (all periods as published, monthly included) when a geography is named, the catalog concepts that read the
  table, licences. Region codes stay `provider_geo` (never countries). A table not pinned by the snapshot →
  `in_snapshot: false` + warning `table_not_in_snapshot` (absent, not zero).
* UI: `FichePage` renders both kinds (`TableSeriesRow`: sparkline for calendar-year cells, last value with period,
  licence badge, notices); Neurones (`NeuronesPage`) adds "voir la série (Fiche)" on any edge to an `atlas:` node.
* Example (dev DB): `nuts:FR101` → `econ:measured_by` → `atlas:eurostat.nuts_gdp` (qualifier `provider_geo FR101`)
  now reads `/v1/atlas/tables/eurostat/nuts_gdp/cells?provider_geo=FR101`; `isco08:2` → `ilostat:measured_by` →
  `atlas:ilostat.emp_temp_sex_ocu_nb_a` reads `indicator_contains=classif1=OCU_ISCO08_2` (summary per indicator).
  **Until the lead's `reva atlas snapshot`, the V4ECON / V4MIND tables answer `in_snapshot: false`.**

## 2. Provider required notices (New York Fed)

* `reva.atlas.snapshot.normalise_rights` keeps `required_notice` (verbatim); `reva.atlas.query.registry_notices`
  reads `rights.required_notice` from `registry/sources/*.yaml` and maps it to atlas sources through the specs'
  `source_key`; `with_required_notice` fills it on table rights and source-spec rights.
* Served on every cell (`rights.required_notice`), in `licences`, and as one `required_notice` warning per source on
  every atlas answer that calls the licence warnings (dossier, series, map, headline map/series, table cells); the
  predict baseline passes cell `rights` through unchanged.
* `registry/sources/nyfed_markets.yaml` declares the notice (same text as `atlas_hf_nyfed.REQUIRED_NOTICE`; the
  recorded table rights already carry it).
* UI: `AtlasRights.required_notice`; `LicenceBadge` adds "avis du fournisseur obligatoire"; `LicenceList` always
  renders `RequiredNotices` (quoted verbatim, even under an open licence) — Atlas map, Dossier, Fiche series.

## 3. Belief release pinning (FIGARO + retirement inputs)

**Why nothing is pinned.** `reva.simulation.datasets.ELIGIBLE_SQL` (latest live dataset per concept/subject,
atlas tables excluded) is correct, and `publish_release` pins every eligible dataset. But the dev DB's
`evidence.dataset` holds only `atlas.table` (317), `events.crisis_episodes` (4), `causal.coded_relations` (3) and
`geo.polygons` (1): **no `economics.io.figaro_ii_table` and none of the 7 Eurostat retirement concepts**
(`demography.population.jan1_age_sex`, `demography.deaths.age_sex`, `demography.live_births.mother_age_newborn_sex`,
`labour.employment_rate.lfs_age_sex`, `labour.activity_rate.lfs_age_sex`,
`social.pension_beneficiaries.esspros_old_age`, `social.pension_expenditure.esspros`). V2 loaded them only in
throwaway databases. Second cause, fixed: `reva ingest --connector eurostat.figaro.iciot_ii` (CLI) archived the raw
bytes but never registered the dataset (only the worker `ingest` job called `publish_io_dataset`) —
`src/reva/cli/main.py` `_figaro_dataset` now does it for a complete year (identity checks refuse otherwise);
test `tests/unit/test_cli_figaro_dataset.py`.

**Not run** (free disk 4.9 GB at the check, below the 5 GB floor; FIGARO raw ≈ 55 MB, peak RSS ≈ 650 MB). Commands
for the lead (each under the dev-DB lock, `df -h /` ≥ 5 GB first):

```sh
flock /tmp/reva-devdb.lock uv run reva ingest --live --connector eurostat.figaro.iciot_ii   # 27 requests, registers the 2024 table
flock /tmp/reva-devdb.lock uv run reva ingest --live --connector eurostat.demography
flock /tmp/reva-devdb.lock uv run reva ingest --live --connector eurostat.labour_pensions
flock /tmp/reva-devdb.lock uv run reva beliefs publish                                      # pins every eligible dataset
uv run python -m reva.predict.run --reference all --out var/predict/reference.json         # IO + retirement plugins
```

## 4. V4LINK families (lead request; V4CORE had finished)

* `registry/kg/relations_v4.yaml`: `corresponds_to` (symmetric, group classification, cost 1.5) and `same_as`
  (symmetric, group identity, cost 0.5), both `causal: false`. `registry/kg/relations_v4.d/v4link.yaml`:
  `v4link:corresponds_to` → `corresponds_to`, `v4link:wikidata_item` → `same_as`. `families check`: 23 families,
  158 mappings, 14 files.
* Identity labelled as identity: `/v1/kg/entities/{id}/families` neighbours carry `identity: true`; links-engine hops
  carry `identity` and the UI words them "même entité (identité selon « … »)" with a badge (Chemin, Neurones, Fiche
  note "REVA ne fusionne pas les fiches").
* Concordance flags visible: `concordance_flags` (table, `*_partial`, relationship) on Fiche neighbours (`flags`),
  links hops (`flags`), KG-path hops and Neurones statements (from qualifiers).
* Traversal: the links taxonomy turns every mapped property into a statement edge (not opt-in), so `/v1/links/path`,
  `/v1/kg/families/path?family=same_as,corresponds_to` and Neurones traverse both.
* Left to V4BIO (running): `bio:wikidata_item` is still unmapped; it should map to `same_as` in `v4bio.yaml`.

## 5. Checks at HEAD

| Check | Result |
|---|---|
| `tests/invariants` + `tests/unit/test_kg_queries_scieco.py` (baseline before changes) | 198 passed, 1 skipped |
| Final sweep (invariants, scieco, new tests, `test_atlas_api.py`, `test_atlas_table_cells.py`) | see §5a |
| `python -m reva.kg.families check` / `python -m reva.kg.harvest validate` | ok (23 families, 158 mappings) / 142 specs valid |
| `tests/unit/links` (159), `tests/integration/test_kg_families_api.py` (7), new unit tests | pass |
| ruff check `.` | 8 findings, all in files of running agents (`test_pred_health*.py`, `test_v6_drugs.py`, uncommitted `atlas_gleif.py`): left |
| ruff format | `app.py`, `evaluation/replay.py` formatted; left: running agents' files (`test_v4link.py`, `test_pred_health*.py`, `atlas_effects_lit.py`) |
| `make typecheck` (mypy kernel + ports) | pass. (`mypy src` strict over everything is not the project gate: ~345 pre-existing errors) |
| `contracts/openapi.json` | regenerated, `--check` up to date; `ui/src/api/openapi.gen.ts` regenerated |
| UI typecheck / vitest | typecheck clean; vitest 254 + new tests pass; 13 failures in one loaded run were 5 s timeouts (ComposerPage etc.) that pass alone (machine load) |

### 5a. Final sweep (after all INTEG changes)

`tests/invariants` + `test_kg_queries_scieco.py` + `test_measured_by_bridge.py` + `test_links_identity_concordance.py`
+ `test_cli_figaro_dataset.py` + `test_atlas_table_cells.py` + `test_atlas_api.py`: **222 passed, 1 skipped, 1
failed**. The failure is new since the baseline and belongs to a running V6 agent (PRED-ENERGY), left to it:
invariant 11 — `reva.evaluation.registry.energy` imports `reva.evaluation.energy_backtests`, whose `main()` imports
`reva.adapters.objects.factory` (→ boto3); fix: move the CLI `main` out of the module imported by the registry (as
`reva.evaluation.pred_models.cli` / `health_registry.__main__` do). `python -m reva.kg.harvest validate`: one invalid
spec of V6CULTURE (`culture/v6/openlibrary_author_ids.yaml`, P648 not allow-listed), left to it. `families check`:
23 families, 181 mappings from 16 files (other V6 agents appended drop-ins meanwhile). mypy on the files INTEG
touched: only pre-existing findings (pyarrow / networkx stubs, envelope call-arg).
