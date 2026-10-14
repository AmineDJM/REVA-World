# HTTP API

The normative contract is the generated OpenAPI document
[`contracts/openapi.json`](../contracts/openapi.json) (regenerate with
`uv run python scripts/export_contracts.py`; CI checks it parses as OpenAPI 3.x
via `scripts/validate_contracts.py`). This page is an orientation only.

## Conventions

* All application endpoints are under `/v1`; `/health/*` and `/metrics` are not.
* Authentication: private single-user deployment. `POST /v1/session` with the
  admin password (Argon2id hash in `REVA_ADMIN_PASSWORD_HASH`) sets an HttpOnly,
  Secure (production), SameSite session cookie plus a CSRF cookie; unsafe methods
  need the `X-CSRF-Token` header. Scoped, revocable, hashed Bearer API tokens are
  the alternative. Nothing is stored in browser localStorage.
* Errors are RFC 9457 problem details. Unsupported requests (e.g. a five-year GDP
  effect, L3/L4 fidelity) return **422 with a structured capability gap**, never a
  fabricated result.
* Long work returns **202** with a job id; every POST that starts work requires
  an `Idempotency-Key` header.
* Lists use keyset cursors; default page 100, maximum 1,000. Graph queries are
  bounded (default depth 2, ≤ 500 edges) and report truncation explicitly.
* Every result carries context, cutoff/release, modality, units, support,
  uncertainty status and provenance links. Missing values are typed, never zero.
  Timestamps are UTC; `as_of` must be timezone-aware.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health/live` | process liveness |
| GET | `/health/ready` | DB/schema readiness (Render health check path) |
| GET | `/metrics` | Prometheus metrics; requires an authenticated session/token with `read` scope |
| POST, GET, DELETE | `/v1/session` | login / current session / logout |
| GET | `/v1/entities`, `/v1/entities/{entity_ref}` | identity register, aliases, components |
| GET | `/v1/relations`, `/v1/events` | bounded n-ary graph and occurrences |
| GET | `/v1/observations`, `/v1/timeseries` | source records with vintage/support filters |
| GET | `/v1/beliefs`, `/v1/belief-releases`, `/v1/belief-releases/{release_id}` | release-pinned selected estimates |
| GET | `/v1/evidence`, `/v1/evidence/{evidence_id}` | evidence cards and source disagreement |
| GET | `/v1/provenance/{kind}/{ident}` | bounded lineage down to raw bytes and selectors |
| GET | `/v1/locations` | spatial supports (Natural Earth display geometry is display-only) |
| GET | `/v1/models`, `/v1/models/{model_ref}/evaluations` | model versions, applicability, scorecards |
| GET, POST | `/v1/scenarios`, GET `/v1/scenarios/{scenario_id}` | immutable scenario definitions |
| POST | `/v1/simulations`, GET `/v1/simulations/{sim_id}` | enqueue/read a run (L0–L2) |
| POST | `/v1/replay` | enqueue one explicit replay mode |
| GET | `/v1/sources`, `/v1/connectors` | source metadata, licences, connector health |
| GET | `/v1/ontology` | versioned concepts, components, mappings |
| GET | `/v1/coverage`, `/v1/gaps` | catalogue-relative coverage and gaps |
| POST | `/v1/discovery` | bounded official-catalogue discovery job |
| GET | `/v1/proposals`, POST `/v1/proposals/{proposal_id}/review` | proposals and human review |
| POST | `/v1/query` | validated QuerySpec (no SQL, no GraphQL) |
| GET | `/v1/jobs`, `/v1/jobs/{job_id}`, POST `/v1/jobs/{job_id}/cancel` | job status and cooperative cancellation |

The UI (React/Vite) is served by the same FastAPI process from
`src/reva/api/static/` (built in the Docker `ui` stage).
