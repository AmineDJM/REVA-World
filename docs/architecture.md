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
  (migrations `0001`–`0007`). Scientific tables are append-only via
  `meta.reject_mutation` triggers; runtime roles have only the grants in `0007`.
* **Object store** (`raw/ normalised/ belief/ models/ scenarios/ evaluation/
  quarantine/`), key `reva/<namespace>/sha256/<aa>/<bb>/<sha256>`; filesystem
  locally, S3-compatible in production; objects are written before the metadata
  transaction that makes them visible (§8.2).
* **Analytical data**: Parquet written per release/materialisation; DuckDB is
  per-process only.

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
| 4 | §12 says bootstrap must "create/update runtime roles", but `ALTER ROLE … NOSUPERUSER NOBYPASSRLS NOCREATEDB` is refused for a non-superuser `CREATEROLE` owner (PostgreSQL 16+), which is what a managed database provides. Verified locally against PostgreSQL 17.5 with a temporary non-superuser `CREATEROLE` role. | Open change request (lead-owned `reva/cli/bootstrap.py`, migration `0007`): set only `LOGIN NOINHERIT CONNECTION LIMIT PASSWORD` on existing roles and **verify** `rolsuper/rolcreatedb/rolcreaterole/rolbypassrls/rolreplication` are false instead of re-asserting them. Until then the first Render deploy may fail in pre-deploy (fail-closed, visible). | deployment.md "Known blocker" |
| 5 | §23 says runtime roles never get the owner credential, but local development reads `.env`, which contains it, and `Settings()` re-reads `.env` in the child. | Start scripts neutralise every secret that a local `.env` would re-inject (process environment wins over dotenv in pydantic-settings); in production a `.env` in the working directory is refused. | `tests/security/test_start_env.py::test_start_api_neutralises_local_dotenv` |
| 6 | A logical dump without `--create` does not carry database-level grants (`REVOKE ALL ON DATABASE … FROM PUBLIC`, `GRANT CONNECT`), which `0007` applies only once. | `scripts/restore.sh` re-applies them after `pg_restore`; the restore test asserts `PUBLIC` has no `CONNECT`. | `tests/integration/test_backup_restore.py` |
| 7 | `belief.snapshots.estimates()` orders by concept/label/period with no unique tie-breaker, so equal-key rows (bilateral partner flows) come back in physical order. | Restore comparison sorts by estimate id; change request filed to append `e.id` to the `ORDER BY` for deterministic pagination. | test above |
| 8 | Host `pg_dump` 16 cannot dump the PostgreSQL 17 server. | Backup/restore accept `REVA_PG_CONTAINER` (run tools in the DB container) or `REVA_PG_BINDIR`; CI runs them inside the service container. | `scripts/backup_restore.py` |
| 9 | `render.yaml` gives the API `REVA_S3_*` without saying which credential. | Documented: the API service's `REVA_S3_ACCESS_KEY_ID/SECRET` must be a read-only credential; the worker's are its own prefix-scoped write credential (never `fromService`). The validator enforces the latter. | deployment.md |
| 10 | Settings (production) requires `REVA_DB_SSLMODE` ∈ {require, verify-ca, verify-full} and per-process checks keyed by `REVA_PROCESS_ROLE`; the appendix Blueprint sets neither, so both services would fail startup. | Minimal Blueprint update: `REVA_DB_SSLMODE=require` in `reva-common`, `REVA_PROCESS_ROLE=api`/`worker` per service; start scripts set and verify the role in the child; the pre-deploy runs as `bootstrap`. | deployment.md "Minimal reviewed compatibility update" |
