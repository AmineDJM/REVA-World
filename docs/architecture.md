# REVA World architecture (as implemented)

REVA is a versioned federation of evidence, estimates and models, built as a
**modular monolith**: one API/UI process and one durable worker share the same
code, one PostgreSQL 17 + PostGIS database and one content-addressed object
store. It is not an omniscient world simulator; unsupported questions return a
typed capability gap. The binding design is the architecture specification
(sections cited as "§n"); this page maps it to the repository and records where
the implementation had to resolve contradictions or provider changes.

## Processes and data flow

```text
 External sources ──SafeHttpClient──▶ acquisition (raw bytes → object store, quarantine)
        │                                   │ parse/validate (deterministic, pinned schemas)
        ▼                                   ▼
  evidence.* (append-only observations, revisions, retractions, eligibility)
        │ source_select_v1 (belief.publication) under the publication advisory lock
        ▼
  belief.release / estimate / input  ──▶ world read projections (release-pinned)
        │ immutable baseline manifest
        ▼
  sim.scenario → sim.run → sim.result  (reva_sim role; never fed back as evidence)
        │
  evaluation (replay modes, tournaments, ledger) · autonomy (template-only proposals)
```

| Process | Entrypoint | Database login | Object credential |
|---|---|---|---|
| Pre-deploy | `python scripts/bootstrap_runtime.py` → `reva.cli.bootstrap.bootstrap` | migration owner (only here) | none |
| API/UI | `python scripts/start_api.py` → `uvicorn reva.api.app:app --workers 1` | `reva_api` | read-only, approved prefixes |
| Worker | `python scripts/start_worker.py` → `python -m reva.operations.worker` | `reva_worker` + separate `reva_sim` pool | prefix-scoped write |
| Promotion gate | `python scripts/start_gate.py` → `python -m reva.gate.service` (`reva gate` / `make gate` locally) | `reva_gate` (the only login that may record gate results) | own prefix-scoped key pair |

The start scripts build an **allowlisted child environment** (ADR 0009): the
migration-owner URL never reaches a runtime process, the API never holds the
worker/simulation passwords, and the worker never holds the API password,
session secret or admin hash. `tests/security/test_start_env.py` asserts this on
the environment the child process actually receives.

## Package boundaries (§2, enforced by import-linter)

| Package | Owns | Must not |
|---|---|---|
| `reva.kernel` | ids, canonical hashing, typed values with `Missing`, uncertainty specs, time/support/context, errors | import DB/HTTP/domain code |
| `reva.ontology` | registry loading, units, mappings, component schemas | redefine a published concept in place |
| `reva.evidence` | ingest, revisions, eligibility, evidence cards, connector runner | write beliefs |
| `reva.belief` | `source_select_v1`, release publication, as-of reads | accept scenario output |
| `reva.world` | entities, aliases, supports, n-ary graph, bounded queries | keep an unversioned "truth" table |
| `reva.models` | pure numerical models (crude exposure, forecasting), applicability | fetch data, touch the DB |
| `reva.simulation` | ScenarioSpec, compile, coupling, deterministic RNG, engine, results | write evidence/beliefs |
| `reva.population`, `reva.compression` | recipe, materialisation, calibration, certificates | invent unobserved detail |
| `reva.evaluation` | replay modes, cutoff repositories, leakage checks, scoring, tournament service, VOI | read past the cutoff |
| `reva.autonomy` | discovery, template generation, validation, gates, proposals, sandbox interface | import `reva.api`; promote itself |
| `reva.adapters` | PostgreSQL, filesystem/S3 objects, SafeHttpClient, connectors | hide semantic changes |
| `reva.operations` | queue, scheduler, worker loop, invalidation, GC, telemetry, audit | become a second model orchestrator |
| `reva.api` | FastAPI app, auth, bounded queries, job submission, static UI | execute user SQL or generated code |

Contracts live in `pyproject.toml` (`[tool.importlinter]`) and run through
`scripts/verify_imports.py` in CI.

## Storage

* **PostgreSQL schemas** `meta, ontology, world, evidence, belief, model, sim, ops`
  (migrations `0001`–`0008`; `0008_v2` adds result periods, dataset inputs and
  the LLM budget ledger functions). Scientific tables are append-only via
  `meta.reject_mutation` triggers; runtime roles have only the grants in `0007`.
* **Object store** (`raw/ normalised/ belief/ models/ scenarios/ evaluation/
  quarantine/`), key `reva/<namespace>/sha256/<aa>/<bb>/<sha256>`; filesystem
  locally, S3-compatible in production; objects are written before the metadata
  transaction that makes them visible (§8.2).
* **Analytical data**: Parquet written per release/materialisation; DuckDB is
  per-process only.

## V2 platform (2026-10-15; experimental models)

The V2 roadmap ([roadmap/v2_design.md](roadmap/v2_design.md)) extends the same
modular monolith; no new service except the promotion gate. Details are in the
agents' reports under `docs/dev/v2_*.md`.

* **Model plugins** (`src/reva/simulation/plugins.py`). A scenario's `model_key`
  selects a registered plugin (`validate`, `requirements`, pure `compile` and
  `compute`, `manifest`); `KNOWN_MODEL_MODULES` lists the crude model (plugin #1,
  outputs and digests bit-identical to V1, checked against stored goldens),
  `economic.io_price`, `demography.cohort_component` and `social.retirement_age`.
  `contracts/scenario.schema.json` (spec 2.0.0) is generated from the registered
  plugins; the V1 contract is frozen as `contracts/scenario.v1.schema.json`. An
  unknown `model_key` is a typed `capability_gap`. New models are seeded as
  `candidate` with `review_status: maintainer_review_pending` and every result is
  labelled "experimental model, not yet reviewed". See
  [dev/v2_eng.md](dev/v2_eng.md).
* **Dataset inputs** (`src/reva/simulation/datasets.py`, `evidence.dataset`).
  Large structured inputs (FIGARO IO tables, age × sex tables, LFS tables) are
  content-addressed Parquet tables derived from archived raw bytes, registered
  with a manifest (source acquisition ids, parser version, schema, row count,
  digests). A belief release pins, per (concept, subject), the latest dataset
  recorded at or before its cutoff (`belief.release_dataset`, sealed with the
  release); a scenario resolves datasets only through its baseline release, and an
  unpinned dataset is a typed `missing_input`.
* **Period results.** `sim.result.period_start/period_end` (UTC dates, half-open;
  both null for single-period models) carry multi-period projections; the API
  serves them on each result row.
* **Gate process** (`src/reva/gate/`). The worker files `ops.gate_request` rows
  and waits for the decision; the separate gate process (login `reva_gate`) re-runs
  the reviewed `PolicyPromotionGate` and records the result. Approval functions
  accept only gate results recorded by `reva_gate` (or a human review recorded
  through the API), and T2+ changes need a human review. If the gate is down,
  discovery reports `gate_pending` and promotes nothing. See
  [dev/gate_separation.md](dev/gate_separation.md).
* **Natural-language drafts** (`src/reva/nl/`, `POST /v1/scenario-drafts`). An
  LLM (OpenAI `gpt-4.1` through the credential-injecting egress proxy; Gemini
  fallback adapter not verified live) only turns text into a draft spec against an
  allow-list built from the registered plugins. The draft is validated by the same
  validator as hand-written specs, cross-checked against the text by the server,
  never executed or stored, and runs only when the user submits it to
  `POST /v1/scenarios`. Tokens and requests are capped per day through
  `ops.budget_ledger`. See [dev/v2_nlc.md](dev/v2_nlc.md).
* **Back-tests** (`src/reva/evaluation/backtests.py`, worker job `backtest`,
  `/v1/models/{model_key}/backtests`): revised-data retrospectives of the
  population and retirement models, computed from release-pinned datasets and
  stored as `model.evaluation` rows with a content-addressed manifest.

## Deployment shape

`render.yaml` is the specification's Blueprint with a minimal, documented compatibility update (`REVA_DB_SSLMODE`, `REVA_PROCESS_ROLE`; see deployment.md), validated against
Render's official JSON Schema (cached in `contracts/render.schema.json`) plus
REVA-specific rules in `scripts/validate_blueprint.py`. See
[deployment.md](deployment.md) and [operations.md](operations.md).

## Repository map

| Spec path (§22) | Implementation |
|---|---|
| `scripts/bootstrap_runtime.py`, `start_api.py`, `start_worker.py` | as named; shared helpers in `scripts/_runtime_env.py` |
| `scripts/validate_blueprint.py`, `verify_imports.py`, `export_contracts.py` | as named; plus `validate_contracts.py` (registry/JSON Schema/OpenAPI) |
| backup/restore commands (§21, P7) | `scripts/backup.sh`, `scripts/restore.sh` → `scripts/backup_restore.py` |
| `Dockerfile` | multi-stage: locked Node UI build → locked uv venv → slim non-root runtime |
| `.github/workflows/{ci,live-smoke,release}.yml` | actions pinned by commit SHA |

## Specification discrepancies and their resolution

The precedence rule of the prompt (scientific honesty → provenance/time →
security → executable MVP → operational simplicity → interface compatibility →
polish) decided each case.

| # | Discrepancy | Resolution | Evidence |
|---|---|---|---|
| 1 | §8.4 lists `model.version` under `0004_models_sim`, but `belief.estimate.method_version_id` (0003) references it. | `model.version` is created in `0003_belief_graph`; dependency order wins over grouping. | `migrations/versions/0003_belief_graph.py` docstring |
| 2 | §17.1 shows a Comtrade query template with `period=2023`; multi-period preview requests are not accepted by the public preview. | The connector plans exactly one period per request; each period is a separate bounded exact key. | `src/reva/adapters/connectors/comtrade.py` (`plan`) |
| 3 | The WDI definition text for `NY.GDP.MKTP.CD` retrieved on 2026-10-14 is worded differently from the classic "sum of gross value added by all resident producers" text. | The concept records the checked wording and date; the connector blocks publication when required definition terms disappear (`semantic_drift`), instead of matching exact prose. | `registry/concepts/economics.yaml`; `tests/security/test_http_connectors.py::test_wb_semantic_drift_in_indicator_metadata_blocks` |
| 4 | §12 says bootstrap must "create/update runtime roles", but `ALTER ROLE … NOSUPERUSER NOBYPASSRLS NOCREATEDB` is refused for a non-superuser `CREATEROLE` owner (PostgreSQL 16+), which is what a managed database provides. Verified locally against PostgreSQL 17.5 with a temporary non-superuser `CREATEROLE` role. | Implemented: bootstrap sets only `LOGIN NOINHERIT CONNECTION LIMIT PASSWORD` on existing roles and **verifies** (`verify_runtime_role_attributes`) that `rolsuper/rolcreatedb/rolcreaterole/rolbypassrls/rolreplication` are false instead of re-asserting them. Not yet exercised against a managed (Render) database. | `src/reva/cli/bootstrap.py`; `tests/security/test_db_hardening.py::test_bootstrap_with_non_superuser_createrole_owner` |
| 5 | §23 says runtime roles never get the owner credential, but local development reads `.env`, which contains it, and `Settings()` re-reads `.env` in the child. | Start scripts neutralise every secret that a local `.env` would re-inject (process environment wins over dotenv in pydantic-settings); in production a `.env` in the working directory is refused. | `tests/security/test_start_env.py::test_start_api_neutralises_local_dotenv` |
| 6 | A logical dump without `--create` does not carry database-level grants (`REVOKE ALL ON DATABASE … FROM PUBLIC`, `GRANT CONNECT`), which `0007` applies only once. | `scripts/restore.sh` re-applies them after `pg_restore`; the restore test asserts `PUBLIC` has no `CONNECT`. | `tests/integration/test_backup_restore.py` |
| 7 | `belief.snapshots.estimates()` orders by concept/label/period with no unique tie-breaker, so equal-key rows (bilateral partner flows) come back in physical order. | Restore comparison sorts by estimate id; change request filed to append `e.id` to the `ORDER BY` for deterministic pagination. | test above |
| 8 | Host `pg_dump` 16 cannot dump the PostgreSQL 17 server. | Backup/restore accept `REVA_PG_CONTAINER` (run tools in the DB container) or `REVA_PG_BINDIR`; CI runs them inside the service container. | `scripts/backup_restore.py` |
| 9 | `render.yaml` gives the API `REVA_S3_*` without saying which credential. | Documented: the API service's `REVA_S3_ACCESS_KEY_ID/SECRET` must be a read-only credential; the worker's are its own prefix-scoped write credential (never `fromService`). The validator enforces the latter. | deployment.md |
| 10 | Settings (production) requires `REVA_DB_SSLMODE` ∈ {require, verify-ca, verify-full} and per-process checks keyed by `REVA_PROCESS_ROLE`; the appendix Blueprint sets neither, so both services would fail startup. | Minimal Blueprint update: `REVA_DB_SSLMODE=require` in `reva-common`, `REVA_PROCESS_ROLE=api`/`worker` per service; start scripts set and verify the role in the child; the pre-deploy runs as `bootstrap`. | deployment.md "Minimal reviewed compatibility update" |
