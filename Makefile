# REVA World developer commands (architecture §23). Run `make help`.
# API/worker run on the host through uv; only PostGIS runs in Docker locally.

SHELL := /bin/bash
.DEFAULT_GOAL := help
UV ?= uv
PY := $(UV) run --frozen
NPM ?= npm
IMAGE ?= reva-world:local
# Offline pytest selection, identical to ci.yml (pyproject addopts deselect the same markers).
PYTEST_MARKERS ?= not live and not perf
PYTEST_ARGS ?=
# Local UI build output for `make ui` (CI and the image build into src/reva/api/static).
UI_OUT ?= $(CURDIR)/var/ui-build
# Playwright browsers: preinstalled in the dev container; CI installs its own (ci.yml e2e job).
PLAYWRIGHT_BROWSERS_PATH ?= /opt/pw-browsers
export PLAYWRIGHT_BROWSERS_PATH
BACKUP_DIR ?= ./var/backups
SCENARIO ?= crude-border-price-25
PROFILE ?= mvp

.PHONY: help env up down sync bootstrap upgrade seed fixtures verify api worker gate ui ui-dev ui-ci ui-check ui-build \
        ui-test e2e ingest-live live-check demo test test-db test-live lint format format-check typecheck imports \
        registry blueprint blueprint-live image smoke backup restore check ci

help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-16s %s\n", $$1, $$2}'

env: ## Create .env from the example (local only; never reuse these passwords)
	@test -f .env || cp .env.example .env

up: env ## Start local PostGIS (docker compose)
	docker compose up -d --wait db

down: ## Stop local services (data volume kept)
	docker compose down

sync: ## Install locked Python dependencies (all extras + dev)
	$(UV) sync --frozen --all-extras

bootstrap: ## Advisory-locked migrations + runtime role provisioning (same code as Render predeploy)
	$(PY) reva db bootstrap

upgrade: ## Apply pending migrations with the migration owner
	$(PY) reva db upgrade

seed: ## Seed registry concepts, entities, sources, models, schedules
	$(PY) reva seed --profile $(PROFILE)

fixtures: ## Load captured source fixtures (origin=test_fixture; never publishable in production)
	$(PY) reva fixtures load --profile $(PROFILE)

verify: ## Verify configuration, schema, roles and storage locally
	$(PY) reva verify --profile local

api: ## Run the API (http://localhost:8000)
	$(PY) reva api

worker: ## Run the durable worker/scheduler
	$(PY) reva worker

gate: ## Run the independent promotion gate (login reva_gate; next to the worker)
	$(PY) reva gate

ui: ui-ci ui-check ## = CI ui job: npm ci, typecheck, vitest, production build (network for npm ci)

ui-check: ## UI typecheck + vitest + build into UI_OUT, using the installed node_modules (offline)
	$(NPM) --prefix ui run typecheck
	$(NPM) --prefix ui test -- --run
	$(NPM) --prefix ui run build -- --outDir $(UI_OUT) --emptyOutDir

ui-dev: ## Vite dev server (proxies /v1 and /health to the local API)
	$(NPM) --prefix ui ci
	$(NPM) --prefix ui run dev

ui-ci: ## Install locked UI dependencies
	$(NPM) --prefix ui ci

ui-build: ui-ci ## Production UI build into src/reva/api/static
	$(NPM) --prefix ui run build

ui-test: ui-ci ## UI typecheck + unit tests
	$(NPM) --prefix ui run typecheck
	$(NPM) --prefix ui test -- --run

ingest-live: ## Live ingestion from real providers (network; origin=live)
	$(PY) reva ingest --profile $(PROFILE) --live

live-check: ## = live-smoke.yml: live source checks into a fresh database (network; writes var/live/)
	$(PY) python scripts/live_source_checks.py

e2e: ## = CI e2e job: disposable stack (fresh DB, offline fixtures) + Playwright suite (needs `make up`)
	$(PY) python scripts/e2e_stack.py --run

demo: ## Run the default crude border-price scenario end to end
	$(PY) reva demo --scenario $(SCENARIO)

test: ## = CI tests job: offline pytest (DB tests use a fresh per-session database; needs `make up`)
	$(PY) pytest -q -m "$(PYTEST_MARKERS)" $(PYTEST_ARGS)

test-db: ## Only database-backed tests
	$(PY) pytest -m db

test-live: ## Live provider smoke tests (network)
	$(PY) pytest -m live -o addopts=""

lint: imports ## = CI static job lint steps: Ruff lint + import boundaries
	$(PY) ruff check .

format-check: ## Ruff format check (blocking in CI)
	$(PY) ruff format --check .

format: ## Apply Ruff formatting
	$(PY) ruff format .

typecheck: ## mypy strict on the kernel and ports
	$(PY) mypy -p reva.kernel -p reva.ports

imports: ## import-linter dependency contracts
	$(PY) python scripts/verify_imports.py

registry: ## Validate registry, JSON Schemas, scenario example and OpenAPI (offline)
	$(PY) python scripts/validate_contracts.py

blueprint: ## Validate render.yaml against the cached Render schema + REVA rules
	$(PY) python scripts/validate_blueprint.py

blueprint-live: ## Same, against the schema downloaded now (render CLI: `render blueprints validate render.yaml`)
	$(PY) python scripts/validate_blueprint.py --live

image: ## Build the production image (pass BUILD_FLAGS="--secret id=build_ca,src=ca.crt" behind a TLS proxy)
	docker build $(BUILD_FLAGS) --build-arg REVA_GIT_COMMIT=$$(git rev-parse HEAD 2>/dev/null || echo unknown) -t $(IMAGE) .

smoke: ## Non-root image smoke: uid != 0, app imports, no compilers
	docker run --rm -e REVA_ENV=test $(IMAGE) sh -c 'test "$$(id -u)" != 0 && python -c "import reva.api.app, reva.operations.worker" && ! command -v gcc && echo smoke-ok'

backup: ## Logical DB backup + object inventory (set REVA_BACKUP_AGE_RECIPIENT to encrypt)
	scripts/backup.sh --out $(BACKUP_DIR)

restore: ## Restore BACKUP=<dir> into the empty database in REVA_RESTORE_DATABASE_URL
	@test -n "$(BACKUP)" || (echo "usage: make restore BACKUP=var/backups/reva-backup-<ts>" && exit 2)
	scripts/restore.sh $(BACKUP) --verify-objects

check: lint typecheck registry blueprint test ## CI static + tests jobs, offline (except UI, e2e, audit, image)

ci: check ui-check e2e ## Everything in ci.yml that runs offline with installed dependencies
