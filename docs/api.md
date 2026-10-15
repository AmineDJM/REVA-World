# HTTP API

The normative contract is the generated OpenAPI document
[`contracts/openapi.json`](../contracts/openapi.json) (regenerate with
`uv run python scripts/export_contracts.py`; CI checks it parses as OpenAPI 3.x
via `scripts/validate_contracts.py`). The running service also serves it at
`/openapi.json` (in production it requires the `read` scope); the interactive
`/docs` page exists only outside production.
This page is an orientation only.

## Conventions

* All application endpoints are under `/v1`; `/health/*` and `/metrics` are not.
  The API connects to PostgreSQL only as `reva_api`: it reads, and writes only
  audit/session-revocation rows and through SECURITY DEFINER functions
  (`ops.submit_job`, `ops.request_cancel`, `ops.review_proposal`,
  `sim.create_scenario`).
* Authentication: private single-user deployment. `POST /v1/session` with
  username `admin` and the admin password (Argon2id hash in
  `REVA_ADMIN_PASSWORD_HASH`) sets an HttpOnly, SameSite=Strict signed session
  cookie (8 h; `Secure` in production or when the public URL is https) plus a
  CSRF cookie; cookie-authenticated unsafe methods need the matching
  `X-CSRF-Token` header. Logout is recorded server-side, so a copied cookie stays
  invalid. Scoped, revocable Bearer API tokens (only their SHA-256 is stored) are
  the alternative. Nothing is stored in browser localStorage. Logging in again
  revokes the session cookie presented with the request, and changing
  `REVA_ADMIN_PASSWORD_HASH` ends every existing session.
* Request bodies are limited to 1 MiB (413 `payload_too_large`), checked before
  authentication and rate limiting. `REVA_CORS_ORIGINS=*` is refused at startup.
* Scopes: `read` (every `/v1` read and `/metrics`), `simulate` (scenarios,
  simulations, replay, evaluations), `ingest` (discovery), `review` (proposal
  reviews; additionally the principal must be the session or a `human:<name>`
  human token — service tokens never review). Job cancellation needs the scope
  that submits that job type.
* Login rate limit: per client, `REVA_LOGIN_RATE_LIMIT_PER_MINUTE` attempts per
  60 s (default 5, allowed 1–1000, invalid values fail at startup; never
  disabled), plus a global window of `max(60, 12 × limit)` attempts. Excess
  attempts get 429 `rate_limited` with `retry_after`; attempts, failures and
  rate-limit events are audited (the typed username is never stored: rows hold
  `username_matched_admin` and a keyed `username_tag`). The limiter is in memory (one API process).
* Errors are RFC 9457 problem details with a stable `code`. Unsupported requests
  (e.g. a five-year horizon, L3/L4 fidelity) return **422 `capability_gap`**;
  missing scenario inputs return **422 `missing_input`**; never a fabricated
  result.
* Long work returns **202** with a job id; every POST that starts a job requires
  an `Idempotency-Key` header (8–200 printable ASCII characters). `POST
  /v1/scenarios` is idempotent by spec digest instead (201 new, 200 existing).
* Lists use keyset cursors; default page 100, maximum 1,000. Relation graphs are
  bounded (default depth 1, maximum 2, ≤ 500 edges) and report truncation
  explicitly.
* Every result carries context, cutoff/release, modality, units, support,
  uncertainty status and provenance links. Missing values are typed, never zero.
  Timestamps are UTC; `as_of` and cutoffs must be timezone-aware.

## Endpoints

| Method | Path | Scope | Purpose |
|---|---|---|---|
| GET | `/health/live` | none | process liveness |
| GET | `/health/ready` | none | DB, migrations and `reva_api` role readiness (Render health check path) |
| GET | `/metrics` | read | Prometheus metrics |
| POST, GET, DELETE | `/v1/session` | none / authenticated | login / current session / logout |
| GET | `/v1/entities`, `/v1/entities/{entity_ref}` | read | identity register, aliases, components |
| GET | `/v1/relations`, `/v1/events` | read | bounded n-ary graph and occurrences |
| GET | `/v1/observations`, `/v1/timeseries` | read | source records with vintage/support filters |
| GET | `/v1/beliefs`, `/v1/belief-releases`, `/v1/belief-releases/{release_id}` | read | release-pinned selected estimates |
| GET | `/v1/evidence`, `/v1/evidence/{evidence_id}` | read | evidence cards and source disagreement |
| GET | `/v1/provenance/{kind}/{ident}` | read | bounded lineage down to raw bytes and selectors |
| GET | `/v1/locations` | read | spatial supports (Natural Earth geometry is display-only) |
| GET | `/v1/models`, `/v1/models/{model_ref}/evaluations` | read | model versions, applicability, scorecards |
| POST | `/v1/evaluations` | simulate | enqueue a forecast evaluation (revised-data retrospective; WDI population/GDP only, horizons 1–5) |
| GET, POST | `/v1/scenarios`; GET `/v1/scenarios/{scenario_id}` | read / simulate | immutable scenario definitions pinned to a published release |
| POST | `/v1/simulations`; GET `/v1/simulations/{sim_id}` | simulate / read | enqueue/read a run (L0–L2). L1/L2 must match the scenario's declared fidelity (L0 always allowed); inputs are pre-checked against the pinned release |
| POST | `/v1/replay` | simulate | enqueue one explicit replay mode |
| GET | `/v1/sources`, `/v1/connectors` | read | source metadata, licences, connector health |
| GET | `/v1/ontology` | read | versioned concepts, components, mappings |
| GET | `/v1/coverage`, `/v1/gaps` | read | catalogue-relative coverage and gaps |
| POST | `/v1/discovery` | ingest | bounded discovery job: Eurostat catalogue only, for a registered requirement with a template; refused when the autonomy kill switch is on or autonomy is disabled |
| GET | `/v1/discovery/candidates` | read | candidates recorded by completed discovery jobs and connector proposals |
| GET | `/v1/proposals`; POST `/v1/proposals/{proposal_id}/review` | read / review | proposals and named human review |
| POST | `/v1/query` | read | validated QuerySpec (timeseries, beliefs, relations; no SQL, no GraphQL) |
| GET | `/v1/jobs`, `/v1/jobs/{job_id}`; POST `/v1/jobs/{job_id}/cancel` | read / per job type | job status and cooperative cancellation |

The UI (React/Vite) is served by the same FastAPI process from
`src/reva/api/static/` (built in the Docker `ui` stage).
