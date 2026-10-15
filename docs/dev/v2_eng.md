# V2 platform (agent ENG): simulation plugins, dataset inputs, multi-period results

Status: implemented 2026-10-15. Spec: `docs/roadmap/v2_design.md` ("Shared interfaces").

## Platform API (for model agents)

`src/reva/simulation/plugins.py` (pure: stdlib, kernel, pyarrow/polars — importable from `reva.models`)

| Name | What |
|---|---|
| `InputRequirement(name, concept, subject, years, kind)` | exactly the design-doc NamedTuple; `kind` is `"scalar"` or `"dataset"` |
| `SimulationModel` (Protocol) | `model_key`, `model_version`, `fidelities` (`{"L1"}`, `{"L1","L2"}`; L0 is the platform cache), `validate(spec)`, `requirements(spec)`, `compile(inputs, spec)` (pure), `compute(compiled, fidelity) -> list[ResultRow]` (pure, Decimal), `manifest()` |
| `ResolvedInputs` | read-only mappings: `scalar(name, subject, year) -> ScalarInput`, `dataset(name, subject) -> DatasetInput`, `subject(key) -> SubjectRef` (entity id + how it resolved), `missing_problems()`, `provenance()`; `ResolvedInputs.from_values(reqs, scalars=..., datasets=...)` builds one without a database for unit tests |
| `ScalarInput` | `value` = parsed kernel value or typed `Missing` (`is_missing`, `missing_reason`), `value_dict`, `estimate_id` (provenance), `temporal_extent`, `aggregation`, `source_flags` |
| `DatasetInput` | `table` = immutable `pyarrow.Table` (or None + `missing`), `frame()` = a fresh Polars frame per call, `manifest` (artifact sha256, source acquisition ids, parser version, schema, row count), `dataset_id`, `artifact_sha256` |
| `ResultRow(output_key, iso3, subject_id, variant, value, uncertainty, applicability, period=None)` | `period` = half-open `(date, date)`; `None` for single-period models |
| `REGISTRY`, `register(model)`, `get(key)`, `load_all()`, `KNOWN_MODEL_MODULES` | each plugin module calls `register(MODEL)` at import; `load_all()` imports `KNOWN_MODEL_MODULES` (`reva.simulation.crude_plugin`, `reva.models.economic.io_price`, `reva.models.population.cohort_component`, `reva.models.social.retirement_age`) and skips only modules that do not exist; an unknown `model_key` is `CapabilityGap(capability="model.<key>")` |
| `source_digest(*paths)` | implementation digest helper for `manifest()` |

Optional plugin hooks (looked up with `getattr`): `spec_schema()` (JSON Schema fragments for
`intervention`, `assumptions`, `outputs` and optionally narrower `countries`, `reference_year`,
`horizon`, `fidelity`, `missing_input_policy`, `schema_version`, `allOf`), `explanation(fidelity)`,
`run_applicability(compiled, fidelity)`, `seed_subjects(spec)`, `cost(spec)`; attributes
`missing_input_policies` (default `{"fail"}`; others are refused with `capability_gap`) and
`scheduling` (execution-order statement recorded on the run).

Missing inputs: if the compiled object has no `missing` attribute, the engine refuses the run
with `missing_input` (problems from `ResolvedInputs.missing_problems()`) **before** `compute`.
A plugin that exposes `missing` (and optionally `incompatible`) owns the reporting (the crude
plugin does, per country).

Experimental models: seed the `model.version` row with `approval_status: candidate` and return
`review_status: "maintainer_review_pending"` from `manifest()`. The engine then runs the model,
and the model manifest (`experimental: true`, `label`) and the run outcome applicability
(`model_label`) say "experimental model, not yet reviewed". `retired`, or `candidate` without
that review status, is refused (`capability_gap`, `model.approval`).

DB-side helpers (`src/reva/simulation/compile.py`): `resolve_inputs(conn, release, reqs, store)`,
`compile_run(conn, spec_json, store)`, `preflight_missing(conn, spec_json)` (submit-time check
for the API; datasets counted as present when pinned).

### Dataset inputs (`src/reva/simulation/datasets.py`)

* `register_dataset(conn, store, *, dataset_key, concept_id, subject_key, table, source_acquisition_ids, parser_version, origin, coverage=None, actor=..., rights=None) -> DatasetRecord` —
  worker role, caller's transaction. Takes the evidence write lock (shared), writes the Parquet
  table and a JSON manifest (`reva-dataset-manifest/1`) to namespace `normalised`, records a
  `normalisation` activity with the acquisitions' raw artifacts as inputs, inserts
  `evidence.dataset`. Idempotent per (key, subject, origin, table bytes). The parser owns the
  domain checks (e.g. IO accounting identities) before calling it.
* Publication: `publish_release` calls `publication_hook(conn, cutoff=, origin=)`; per
  (concept, subject) the latest dataset recorded at or before the cutoff with the release's
  origin is listed in the release manifest (`dataset_inputs`, by digest; releases without datasets
  keep the V1 manifest) and pinned (`belief.release_dataset` + `belief.release_artifact` role
  `dataset`) before `ops.publication` seals the release.
* Simulation: a `kind="dataset"` requirement with `concept` = the dataset's `concept_id` and
  `subject` = its `subject_key` resolves only to what the baseline release pinned; the bytes are
  re-hashed, row count and schema checked; unpinned → typed `Missing` → `missing_input`.

## Scenario spec 2.0.0

`contracts/scenario.schema.json` is generated (`uv run python -m reva.simulation.scenario_schema`,
`--check` to compare) from the registered plugins: a top-level closed object whose `model_key`
selects a `$defs` branch (`allOf` of `if model_key == K then $ref`). Branch = common fields +
the plugin's fragments, closed. The crude branch accepts `schema_version` `1.0.0` and `2.0.0`;
the V1 appendix is frozen as `contracts/scenario.v1.schema.json` (its digest pin in
`tests/contracts/test_json_schemas.py` is unchanged). `validate_scenario_spec` returns the V1
`ScenarioSpec` for crude specs (identical canonical bytes, digests, cache keys) and an immutable
`GenericScenarioSpec` for other models (schema branch → policy/fidelity support → cutoff →
fixture-baseline check → `plugin.validate`). Macro outputs (`gdp_loss`, `unemployment`,
`consumer_inflation`, `household_welfare`) are refused for every model.

## Migration `0008_v2`

* `sim.result.period_start/period_end` (date, both null or both set, `start < end`; check
  `result_period_half_open`); identity `UNIQUE NULLS NOT DISTINCT (run_id, output_key, subject_id,
  variant, period_start)` replaces the V1 unique. Single-period rows are written with the V1
  INSERT, and the L0 lookup reads periods through `to_jsonb(r)`, so the crude path also runs on a
  pre-0008 database.
* `evidence.dataset` (manifest: dataset key, concept, subject key, origin, table + manifest
  artifact sha256, `source_acquisition_ids uuid[]`, parser version, `table_schema`, row count,
  coverage, activity, server `recorded_at`). Trigger: acquisitions exist with the same origin,
  distinct; both artifacts verified. Append-only (`meta.reject_mutation`).
* `belief.release_dataset` (release, dataset, concept, subject, artifact sha; one per
  (release, concept, subject)). Trigger: pin restates its dataset, same origin, `recorded_at <=
  knowledge_cutoff`; `release_sealed` = `belief.reject_if_published` (exactly like estimates).
  Append-only.
* Grants: worker INSERT on both; api/worker/sim SELECT; new functions revoked from PUBLIC;
  `reva_restricted_deletion` SELECT/DELETE as 0007. No `recorded_by` column: these are not
  approval/review (governance) tables.

## Decisions (owner asleep; conservative)

1. **Bit identity proven, not assumed.** Before refactoring, goldens were captured with the V1
   engine (`tests/fixtures/simulation/crude_v1_golden.json`: full rows, digests, cache keys,
   baseline digests, schedules; `crude_v1_db_golden.json`: seven job-path runs on the fixture
   release incl. missing DZA, L3, L0 miss/hit). The live demo's recorded output digest
   `057728f2…4bfd` and FRA exposure `0.25756760879252203773441363615596620341591545855751`
   are reproduced from its recorded inputs. The *implementation digest* in the model manifest
   changes (by design: any code change invalidates L0 reuse and run reuse).
2. `ResultRow` lives in `plugins.py` (pure) so model packages can import it under the
   import-linter contract; `engine`/`compile` re-export every V1 name (tests, scripts unchanged).
3. Validation uses the in-memory union built from the registry, so a new plugin validates
   immediately; the exported file is checked by `tests/contracts/test_scenario_schema_v2.py`
   (run the generator after adding/changing a plugin).
4. Dataset eligibility = recorded at/before the cutoff with the release origin, latest per
   (concept, subject). There is no separate human eligibility decision for datasets: they must
   come from reviewed built-in connectors (documented limitation).
5. `missing_input_policy` values other than `fail` are refused unless the plugin declares them.
6. Capabilities: `implemented` now requires `tests=("path::test_name", …)` citing existing tests
   (V1 entries cite theirs via `_V1_TESTS`); implemented `V2` entries also need `docs=`.
   Added `reserved`: `demography.cohort_component`, `economic.io_price`, `social.retirement_age`,
   `nl.scenario_drafts`, `scenario.all_countries`, `scenario.skip_missing_countries`; added
   `implemented` (platform only, with tests): `simulation.platform.dataset_inputs`,
   `simulation.platform.period_results`. New phase label `V2`.

## Tests

New: `tests/unit/test_simulation_v2_plugins.py` (identity vs goldens and live demo, registry,
resolved inputs, periods), `tests/integration/test_simulation_v2_plugin_identity.py` (job path
vs V1 golden), `tests/integration/test_simulation_v2_datasets.py` (throwaway DB: register →
publish → pinned/sealed, grants, multi-period dataset toy plugin through the job path incl.
experimental label and L0 hit, unpinned → missing_input, period constraints, cutoff
eligibility), `tests/contracts/test_scenario_schema_v2.py`, `tests/unit/test_capabilities_v2.py`.
Results: see the ENG report (2026-10-15).
