# Parallel implementation brief (read fully before working)

Binding specification: `/root/.claude/uploads/a82f628c-2790-54df-b0b4-28930bdc7a97/864d2dd3-REVA_Claude_Code_Prompt.md`
(implementation prompt + architecture + appendices). Read the sections relevant to your area. Scientific
honesty, provenance/time semantics and security beat polish. Never fabricate data or stub success: an
unsupported capability returns a typed blocked/unavailable result.

## Environment
- Repo: `/home/user/REVA-World`, Python 3.12 via `uv run ...` (ignore the `UV_NATIVE_TLS` warning). Node 22 available.
- PostgreSQL 17 + PostGIS 3.5 runs at 127.0.0.1:5432 (docker container `reva-db`, superuser `reva_owner`,
  password in `.env`). The dev database `reva` holds live data: **never reset or write to it**. Tests use the
  `db` fixture in `tests/conftest.py`, which creates a fresh uniquely named database per pytest session,
  applies migrations, provisions runtime role passwords and seeds the registry. It exposes engines
  `db.owner` (migration owner, superuser locally), `db.api` (reva_api), `db.worker` (reva_worker),
  `db.sim` (reva_sim), `db.settings`, and `db.store` (filesystem ArtifactStore). Mark DB tests with
  `pytestmark = pytest.mark.db`. Async tests work (`asyncio_mode=auto`).
- Live network to providers works; tests must be offline (use captured fixtures in
  `tests/fixtures/source/<connector>/manifest.json` via `reva.evidence.runner.FixtureHttp`). Live tests
  must be marked `@pytest.mark.live` (deselected by default).
- Do **not** `git commit`/`git push`, and NEVER run `git stash`, `git reset`, `git checkout -- <path>`, `git restore` or `git clean` — other agents' uncommitted work lives in the same working tree (a stash at 23:48 temporarily wiped 38 files). Do not edit files owned by someone else (table below). If you need a
  change in a shared file (pyproject.toml dependencies, migrations, kernel, `adapters/postgres/repositories.py`,
  `cli/main.py`, conftest), do not edit it: put the exact requested change in your final report. You may
  add new modules inside your own directories. No new Alembic migrations: report needed schema changes.

## Existing building blocks (read these)
- `src/reva/kernel/*` — ids/canonical hashing, typed values with `Missing`, `UncertaintySpec`, `TimeExtent`,
  `Support`, `Context`/`Origin`/`Modality`, records, errors (`RevaError.problem()` → RFC 9457 dict).
- `migrations/versions/0001..0007` — the schema, triggers, SECURITY DEFINER functions
  (`ops.submit_job`, `ops.request_cancel`, `ops.assert_lease`, `ops.heartbeat`, `ops.complete_job`,
  `ops.review_proposal`, `sim.create_scenario`) and grants/RLS per role.
- `src/reva/adapters/postgres/repositories.py` (artifacts, activities, contexts, supports, outbox, audit,
  publication lock), `engine.py` (`make_engine(kind, settings)`).
- `src/reva/adapters/objects/*` — content-addressed filesystem/S3 stores (`ArtifactStore` port).
- `src/reva/adapters/http/safe_client.py` — SafeHttpClient (only HTTP path for connectors).
- `src/reva/evidence/ingest.py` (`publish_acquisition`), `evidence/runner.py` (`run_connector`, `FixtureHttp`,
  `RecordingHttp`, `make_live_client`, `post_publish_for`), `evidence/revisions.py`, `evidence/cards.py`.
- Connectors: `adapters/connectors/{worldbank,comtrade,naturalearth,crossref}.py`, `registry.py`.
- `src/reva/belief/{selection,publication,snapshots}.py` — `publish_release(engine, store, cutoff=, origin=,
  environment=)`, `release_as_of`, `estimates`.
- `src/reva/simulation/spec.py` — `validate_scenario_spec(dict) -> ScenarioSpec` (`.canonical_bytes()`,
  `.digest()`, `.as_json()`).
- `src/reva/operations/context.py` — `JobContext` handler contract (`handle(payload, ctx) -> dict`).
- `src/reva/ports/autonomy.py` — normative autonomy protocols/DTOs.
- `registry/` — concepts, entities (`reva.cli.seed.entity_id(key)` gives deterministic entity UUIDs, e.g.
  `entity_id("FRA")`), sources, mappings, models, policies, requirement catalogue.
- Live facts: Comtrade 2023 HS2709 world totals exist for FRA/CHN/USA; DZA returned zero rows 2020–2023 (2024 has one H4 row per flow, isReported=false, legacyEstimationFlag=6)
  (stored as typed Missing). WDI population/GDP 2000-2024 for all four.

## Ownership
| Area | Owner | Paths |
|---|---|---|
| Population/compression numerics | agent A | src/reva/population, src/reva/compression, tests/unit/population, tests/property/test_population_properties.py, tests/performance/test_population_perf.py |
| Forecasting/tournament/VOI numerics | agent B | src/reva/models/{forecasting,metrics.py,tournament.py}, src/reva/evaluation/{splits,scoring,voi}.py, tests/unit/forecasting, tests/scientific/test_voi_analytic.py, tests/scientific/test_uncertainty_types.py |
| Simulation + crude exposure | agent C | src/reva/models/economic, src/reva/simulation (except spec.py, read-only), tests/scientific/test_crude_exposure*.py, tests/integration/test_simulation*.py |
| Queue/worker/scheduler/ops | agent D | src/reva/adapters/postgres/queue.py, src/reva/operations/* (except context.py, environment.py), tests/integration/test_queue*.py, tests/integration/test_worker*.py, tests/integration/test_scheduler*.py |
| HTTP API | agent E | src/reva/api/**, scripts/export_contracts.py, contracts/openapi.json, tests/integration/test_api*.py, tests/security/test_auth*.py |
| UI | agent F | ui/** |
| Autonomy + Eurostat | agent G | src/reva/autonomy/**, src/reva/adapters/connectors/eurostat_template.py, src/reva/world/graph.py? (no), tests/integration/test_autonomy*.py, tests/integration/test_eurostat*.py, tests/security/test_prompt_injection*.py, tests/security/test_sandbox*.py, tests/fixtures/source/eurostat*/ |
| Replay/leakage + temporal/revision acceptance | agent H1 | src/reva/evaluation/{replay,cutoff,leakage}.py, tests/replay/**, tests/integration/test_revisions*.py, tests/fixtures/vintages/** |
| Security/storage/kernel/contract tests | agent H2 | tests/security/test_{roles,ssrf,http,secrets,sql}*.py, tests/unit/test_{kernel,storage,values,ids,settings}*.py, tests/integration/test_storage*.py, tests/contracts/**, tests/fixtures/adversarial/** |
| Deployment/ops/docs | agent I | Dockerfile, .dockerignore, Makefile, scripts/*.py and *.sh (except dev_reset_db.sh), .github/**, docs/** (except docs/dev and docs/implementation_status.md), tests/security/test_start_env*.py, tests/integration/test_backup_restore*.py |
| World graph library | agent J | src/reva/world/{graph.py,entities.py,identities.py,events.py,queries.py}, tests/unit/test_world_graph*.py, tests/integration/test_world_graph*.py |
| Ontology services, units, components, exports | agent K | src/reva/ontology/{components.py,mappings.py,units.py,validation.py,exports.py}, registry/components/*.schema.json, tests/unit/test_ontology*.py, tests/contracts/test_registry_components*.py |
| Model evaluation service + forecast ledger + model registry | agent L | src/reva/models/{registry.py,applicability.py,ports.py}, src/reva/evaluation/{tournament_service.py,ledger.py}, tests/integration/test_evaluation_service*.py, tests/unit/test_model_registry*.py |
| Extension interfaces P8–P12 + capability descriptors | agent M | src/reva/models/physical/**, src/reva/extensions/**, src/reva/capabilities.py, tests/unit/test_capabilities*.py, tests/unit/test_extensions*.py |
| Code review of core (read-only, report) | agent R | docs/dev/review_core.md only |
| Spec compliance audit (read-only, report) | agent S | docs/dev/spec_audit.md only |
| Integration, CLI, world/space.py, ledger | lead | src/reva/cli/**, src/reva/world/space.py, src/reva/population/service.py, src/reva/belief/**, src/reva/evidence/**, conftest, pyproject, migrations, docs/implementation_status.md |

## Cross-agent interface names (fixed)
- API app: `reva.api.app:app` (FastAPI) and factory `reva.api.app:create_app(settings=None)`.
- Worker entrypoint: `reva.operations.worker:main()`; job handler registry `reva.operations.jobs.HANDLERS`
  (job_type → dotted path `module:function`, lazily imported).
- Simulation job handler: `reva.simulation.jobs:handle_simulation(payload, ctx)`; payload
  `{"scenario_id": uuid, "fidelity": "L0|L1|L2"}`.
- Replay job handler: `reva.evaluation.replay:handle_replay(payload, ctx)`.
- Discovery job handler: `reva.autonomy.jobs:handle_discovery(payload, ctx)`; gaps: `reva.autonomy.jobs:handle_gaps`.
- Ingest job handler (agent D): `reva.operations.jobs:handle_ingest` payload `{"connector": name, "mode": "live"|"fixture"}`.
- Belief publication handler (agent D): `reva.operations.jobs:handle_publish_beliefs`.
- Bootstrap: `reva.cli.bootstrap:bootstrap(settings)`; seed: `reva.cli.seed:seed(engine, store)`.

## Wave 6 ownership (added during integration)
| Agent | Owns |
|---|---|
| DOC | docs/data_constitution.md, src/reva/ontology/components.py, registry/components/trade_crude* |
| DOC2 | docs/methodology.md, limitations.md, api.md, scientific_validation.md, operations.md (not backup section), threat_model.md, README.md |
| UIX | ui/** |
| DKR | Dockerfile, .dockerignore, compose.yaml, scripts/start_*.py, scripts/_runtime_env.py, scripts/validate_blueprint.py, render.yaml, docs/deployment.md |
| BKP | scripts/backup*.{sh,py}, scripts/restore.sh, tests/integration/test_backup_restore*.py, docs/operations.md backup section |
| SEC | src/reva/api/**, tests/security/test_api_adv_*.py |
| CI | .github/workflows/**, Makefile, pyproject.toml tool sections |
| CVAL | src/reva/adapters/connectors/{worldbank,comtrade}.py + their tests |
| HOOK | src/reva/operations/jobs.py, belief/publication.py, world/queries.py, autonomy/repair.py |
| GOV | 0007_security.py (except sim grants), 0006 ops/model governance functions, cli/bootstrap.py, cli/seed.py |
| SIMFIX | 0004 sim.*, 0006 sim.create_scenario, 0007 sim/artifact grants, simulation/**, register_artifact |
| PUBFIX | 0001/0002/0003/0005, belief/** except publication.py, PUBLICATION_LOCK probe, eligibility scopes |
| MIGR | adapters/postgres/schema_contract.py, migrations/schema_contract.json, `reva db verify-schema` |
| LEDGER | docs/implementation_status.md |

## V2 roadmap (owner-approved 2026-10-15) — see docs/roadmap/v2_design.md for ownership
Agents: ENG (platform/plugins/0008), ALLC (all countries), NLC (NL drafts), POP (cohort projection), IO (input-output prices), PEN (retirement age + households), AFX (R6 autonomy fixes), APIX (API fields), UI2 (UI round 2).
