# REVA World developer commands (architecture §23). Run `make help`.
# API/worker run on the host through uv; only PostGIS runs in Docker locally.

SHELL := /bin/bash
.DEFAULT_GOAL := help
UV ?= uv
PY := $(UV) run --frozen
NPM ?= npm
IMAGE ?= reva-world:local
BACKUP_DIR ?= ./var/backups
SCENARIO ?= crude-border-price-25
PROFILE ?= mvp

.PHONY: help env up down sync bootstrap upgrade seed fixtures verify api worker ui ui-ci ui-build ui-test \
        ingest-live demo test test-db test-live lint format typecheck imports registry blueprint blueprint-live \
        image smoke backup restore check

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

ui: ## Vite dev server (proxies /v1 and /health to the local API)
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

demo: ## Run the default crude border-price scenario end to end
	$(PY) reva demo --scenario $(SCENARIO)

test: ## Offline test suite (DB tests use a fresh per-session database)
	$(PY) pytest

test-db: ## Only database-backed tests
	$(PY) pytest -m db

test-live: ## Live provider smoke tests (network)
	$(PY) pytest -m live

lint: imports ## Ruff lint + format check, import boundaries
	$(PY) ruff check .
	$(PY) ruff format --check .

format: ## Apply Ruff formatting
	$(PY) ruff format .

typecheck: ## mypy strict on the kernel
	$(PY) mypy

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

check: lint typecheck registry blueprint test ## Everything CI runs offline (except UI and image)
