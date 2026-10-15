# HTTP API adversarial security review

Scope: the FastAPI surface in `src/reva/api/**` (app factory, auth, deps, errors, routes) and the
database functions it calls (`ops.submit_job`, `ops.request_cancel`, `ops.review_proposal`,
`sim.create_scenario`), read from `migrations/versions/0006_operations.py` and `0007_security.py`.
I traced each candidate issue to a concrete request before rating it. The regression tests are in
`tests/security/test_api_adv_http_surface.py` (prefix `test_api_adv_`). Migrations were not edited. DB-level
items are listed for the migration reviewer.

Severity scale: High (exploitable without credentials or crosses a trust boundary), Medium (unauthenticated
resource abuse or a real defence gap), Low (hardening with a narrow precondition), Info (accepted or by design).

## Findings fixed in this pass

| # | Finding | Sev. | Request path | Fix | Test |
|---|---|---|---|---|---|
| A1 | No request-body size limit. `POST /v1/session` is unauthenticated. FastAPI buffered and JSON-parsed any body (a 3 MB body was read and only then rejected with 422) before the login rate limiter ran. `POST /v1/scenarios` takes a free `dict` body. | Medium | `POST /v1/session` with a large or chunked body | Added an ASGI `BodySizeLimit` in `app.py` (default 1 MiB, `create_app(max_body_bytes=)`). It refuses a declared `Content-Length` above the cap, or an unparseable one, without reading the body. It counts chunked bodies as they stream and fails the read with 413. The response is RFC 9457 `payload_too_large` with the normal security headers. | `test_oversized_bodies_are_refused_before_parsing`, `test_default_body_cap_is_one_mebibyte` |
| A2 | Every rate-limited login attempt inserted a row into append-only `ops.audit`. An unauthenticated client could grow the audit table without bound at request rate, even though each request was "rejected". | Medium | repeated `POST /v1/session` past the limit | `AuthService._audit_rate_limited`: one `session.login_rate_limited` row per client per window (bounded LRU map). Failed-credential audits were already bounded by the limiter. | `test_rate_limited_attempts_are_audited_once_per_window` |
| A3 | Sessions survived an admin password rotation. Validity depended only on the session secret, so rotating `REVA_ADMIN_PASSWORD_HASH` after a suspected compromise left stolen cookies valid for up to 8 h. | Low | replay an old `reva_session` cookie after the hash changes | The signed session carries `pb`, an HMAC (keyed from the session secret) of the admin hash in force at login. `_from_session` compares it in constant time, and a mismatch or missing hash gives 401. Sessions issued before this change need one re-login. | `test_rotating_the_admin_password_ends_existing_sessions` |
| A4 | Logging in again left the previously presented session valid, so a copied older cookie outlived the user's re-login. Session fixation itself was not possible: the sid is server-generated inside a signed cookie and a new one is issued on every login. | Low | `POST /v1/session` while holding cookie A, then replay A | `login()` revokes the still-valid session presented with the login (persisted to `ops.session_revocation`) before issuing the new sid. | `test_relogin_rotates_session_and_revokes_the_presented_one` |
| A5 | `/openapi.json` was public in production. `/docs` was already disabled. | Low | `GET /openapi.json` without credentials | In production `openapi_url=None`, and `/openapi.json` is served by a route that requires the `read` scope. Local and test behaviour is unchanged. `app.openapi()` and the committed contract (`scripts/export_contracts.py`) are unchanged. | `test_production_hides_docs_and_requires_auth_for_openapi`, `test_local_still_serves_openapi_and_docs` |
| A6 | A NUL byte in a SPA path raised `ValueError` in `Path.resolve()`. The result was a 500 and an "unhandled error" log line per request. No traversal was possible. | Low | `GET /%00`, `GET /assets/%00.js` | `spa()` treats `ValueError`/`OSError` from path resolution as 404. | `test_spa_path_tricks_never_500_or_escape` (also covers `..%2F`, `%2e%2e`, `..%5c`, symlink escape, over-long names) |
| A7 | 500 responses had no security headers (no `nosniff`, CSP or `X-Frame-Options`). The generic handler runs in Starlette's outermost `ServerErrorMiddleware`, outside the header middleware. | Low | any unhandled exception | Header logic is factored into `apply_security_headers()`, exposed as `app.state.apply_security_headers` and applied by the `Exception` handler in `errors.py`. The body is still a generic `internal_error` with no exception text. | `test_internal_errors_carry_security_headers_and_no_internals` |
| A8 | `REVA_CORS_ORIGINS=*` was accepted outside production together with `allow_credentials=True`. Starlette then reflects any `Origin` with `Access-Control-Allow-Credentials: true`. Settings rejected this only in production. | Low | a local or test deployment configured with `*` | `create_app` raises `ConfigurationError` whenever the origins contain `*`. | `test_wildcard_cors_with_credentials_is_refused_everywhere` |

Also changed:

* `errors.py` maps HTTP 413 to the stable code `payload_too_large`.
* `routes/sim.py`: `sim.create_scenario` now serialises identical creators with an advisory lock (R5-15) and
  returns the row a concurrent caller committed. `POST /v1/scenarios` reported that row as 201 Created,
  failing the existing R2-15 test. It now checks `meta.inserted_in_current_xact(xmin)` and answers 200 for a
  scenario it did not insert.
* `app.py` lifespan (coordinator request): the schema-contract startup guard
  (`assert_schema_contract`, run via `asyncio.to_thread`). `SchemaDriftError` refuses startup in production.
  In warn mode (the `REVA_ENV=test` default) drift, or a database that is not configured, is only logged.

## Verified with no change needed (regression tests added where cheap)

* **Authentication on every route.** The test walks the routes from the app object, descending into
  included routers, and cross-checks them against `app.openapi()["paths"]`. Every route outside the public
  allowlist (`/health/live`, `/health/ready`, `POST /v1/session`, `/`, the SPA catch-all, plus
  `/openapi.json` and `/docs` outside production) answers 401 in three cases: no credentials, a session
  cookie signed with another key, and a non-Bearer `Authorization` header. A static check also asserts that
  each non-public `APIRoute` depends on `authenticated` or `require_<scope>`. Tests:
  `test_every_non_public_route_requires_authentication` and
  `test_api_routes_all_declare_an_auth_dependency`. Authentication runs before body or parameter
  validation, so unauthenticated probes learn nothing from 422 messages.
* **Cookie flags.** The session cookie is HttpOnly. Both cookies are `SameSite=Strict`, `Path=/` and
  host-only (no `Domain`). Both are `Secure` in production or when `REVA_PUBLIC_BASE_URL` is https. Tests:
  `test_csrf_double_submit_cannot_be_satisfied_by_a_tossed_cookie` and
  `test_secure_flag_on_cookies_in_production`. Note: a `__Host-` prefix would also pin `Secure` and `Path`,
  but it cannot be used while local http development is supported.
* **CSRF.** Every unsafe cookie-authenticated method needs `X-CSRF-Token`. The token must equal both the
  cookie and the value bound inside the signed session, so a tossed `reva_csrf` cookie with a matching header
  still gets 403 (tested). Bearer requests skip CSRF, which is correct: browsers never attach
  `Authorization` cross-site. A Bearer header always takes precedence and never falls back to the cookie.
  JSON bodies sent as `text/plain` are not parsed as JSON by FastAPI, which is a second barrier against form
  posts.
* **Session revocation.** Logout is persisted as a SHA-256 of the sid in `ops.session_revocation` and checked
  on every request, so it holds across restarts (existing test R2-09). Expiry is enforced server-side by
  itsdangerous `max_age`, not only by the cookie `Max-Age`.
* **Login rate limit and forwarded headers.** The client key is `request.client.host` (the TCP peer). The app
  never reads `X-Forwarded-For`, `X-Real-IP`, `Forwarded`, `True-Client-IP` or `CF-Connecting-IP`: rotating
  them does not reset the per-client bucket (`test_login_rate_limit_ignores_client_supplied_address_headers`).
  Proxy-header trust lives in Uvicorn: `scripts/start_api.py` passes `--proxy-headers` with
  `--forwarded-allow-ips` limited to loopback and private ranges, and Uvicorn 0.54 takes the right-most
  untrusted `X-Forwarded-For` hop. A spoofed left-most entry is therefore ignored behind Render's proxy. A
  global budget (`max(60, 12×limit)`/min) caps Argon2 work under distributed guessing.
* **Timing-safe comparisons.** Username and CSRF use `hmac.compare_digest`, as does the new password binding.
  The password check is Argon2id `verify` against a dummy hash when the username is wrong. Session signatures
  are verified by itsdangerous in constant time. Tokens are looked up by SHA-256 digest, so no secret is
  compared in Python.
* **Token hashing at rest.** `ops.api_token` stores only `sha256(token)`. Tokens are `reva_` plus 256 random
  bits, so a fast hash is adequate. The API has no token-minting route (the CLI mints, as migration owner).
  The new test asserts that the stored value is a 64-hex digest.
* **Human and service principals.** Reviews require `principal.human_reviewer`: the interactive session, or a
  `review`-scoped token labelled `human:<name>` (not `human:admin`) whose `principal_kind` is `human`. A
  service token labelled `human:x` is refused (`test_service_token_with_human_label_cannot_review`). The DB
  function also refuses non-`human:` reviewers and self-review.
* **Idempotency namespacing.** `ops.submit_job` stores `api:<created_by>:<type>:<key>` and the API passes
  `principal.actor` as `created_by`. Two principals using the same key get distinct jobs. The same principal
  with the same request gets the same job, and with a different payload gets 409 `idempotency_conflict`
  (`test_idempotency_keys_are_namespaced_per_principal`). See D1 for a residual ambiguity at the DB level.
* **Cancellation.** Authorised by the scope that may submit the job type (`CANCEL_SCOPE`); unknown types
  cannot be cancelled and read-only principals cannot cancel
  (`test_cancel_requires_the_job_types_scope_even_for_the_creator`). It is not ownership-based; see I1.
* **Error responses.** Validation problems carry loc, msg and type only, never input. DB errors map to typed
  problems: `P0001` returns the function's own message, `22xxx` returns 400 `invalid_parameter`, `57014`
  returns 503, and anything else is a generic 500. Job `last_error` is sanitised by `public_error`. A
  regression check for SQL or driver markers is in `test_database_errors_do_not_leak_sql`.
* **Pagination caps.** `page_limit` is ≤1000 everywhere. Belief releases are ≤100. Relations allow
  `max_edges` ≤500 and `depth` ≤2. Provenance allows `max_nodes` ≤1000 with `MAX_DEPTH`. Discovery
  candidates are ≤1000 (50 jobs scanned). Simulation results are ≤5000. QuerySpec lists are bounded
  (≤50 subjects, ≤20 concepts). Cursors are typed and validated (400 `invalid_cursor`). Catalogue lists are
  registry-sized.
* **CORS.** Explicit origin allowlist only, with credentials, limited methods and headers, and preflight
  cached for 600 s. Unknown origins get no `Access-Control-Allow-Origin` (existing test).
* **Security headers.** CSP `default-src 'self'; script-src 'self'; … object-src 'none'; base-uri 'self';
  form-action 'self'; frame-ancestors 'none'`, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy:
  no-referrer`, COOP, `Permissions-Policy`, and HSTS in production. `/v1` responses are `no-store`. The
  relaxed docs CSP (jsDelivr, `'unsafe-inline'`) applies only to `/docs`, which does not exist in production.
  `style-src 'unsafe-inline'` remains for the UI; it is a residual and low-value XSS aid, accepted.
* **Path traversal.** The API has no artifact or object download route. Provenance validates artifact ids as
  64-hex and other ids as UUIDs, and object bytes are never served. The only file-serving code is the SPA
  route, which resolves symlinks and requires `is_relative_to(static root)` and no dot-segments. It is tested
  against encoded traversal, backslashes, NUL and a symlink pointing outside.
* **SSRF.** No request field is a URL or host. `POST /v1/discovery` accepts only a `catalogue` literal (only
  `eurostat` proceeds), a `requirement_id` that must be in `DISCOVERABLE_REQUIREMENTS`, and `mode`. Free-text
  `query`, `concept_id` and `max_candidates` are refused with 422. The job payload is exactly
  `{requirement_id, mode}`, and the handler (`reva.autonomy.jobs`) builds `SafeHttpClient(allowed_hosts=
  TEMPLATE_HOSTS, …)`. Replay, evaluation and scenario bodies contain no URLs.
* **Host header.** `TrustedHostMiddleware` uses `REVA_ALLOWED_HOSTS`, and production requires a non-local
  host list. `X-Request-ID` is accepted only when alphanumeric or dash and at most 64 characters, so it cannot
  inject into logs.

## DB-level items for the migration reviewer (not fixed: migrations are out of scope)

| # | Finding | Sev. | Requested change |
|---|---|---|---|
| D1 | `ops.submit_job` namespace `'api:' || created_by || ':' || type || ':' || key` is delimiter-ambiguous. Token labels may contain `:`, so actor `token:svc:replay:p` with key `K` and actor `token:svc` with key `p:replay:K` produce the same stored key. Labels are also not unique, so two live tokens with the same label share a namespace and an audit identity. The precondition is an admin-minted label plus a predictable victim key. With differing payloads the impact is a 409 (blocking); with identical payloads, the other principal's job id is returned (job metadata is already readable with `read`). | Low | Build the stored key unambiguously, e.g. `'api:' || encode(sha256(convert_to(created_by,'UTF8')),'hex') || ':' || p_type || ':' || p_idempotency_key`, or length-prefix `created_by`. Add `CREATE UNIQUE INDEX ON ops.api_token(label) WHERE revoked_at IS NULL`, and optionally restrict labels to `^(human:)?[a-z0-9][a-z0-9._-]{0,63}$`. |
| D2 | `ops.request_cancel(uuid, text)` is granted to `reva_api` with no job-type or authorisation check. Only the API's `CANCEL_SCOPE` gate stands between a `reva_api` SQL-level compromise and cancelling scheduler jobs (`publish_beliefs`, `gc`, `health`). | Info | Defence in depth: refuse job types outside the API-cancellable set inside the function, or take the required scope or actor as an argument and check it. |
| D3 | API tokens have no expiry column. They are only revocable, which meets the spec ("hashed, scoped and revocable"). | Info | Optional `expires_at timestamptz` with the API rejecting expired tokens. The API reads `row.get(...)`, so it can adopt the column without breaking older schemas. |

## Accepted or by design (Info)

* **I1 Cancellation by scope, not ownership.** V1 is a private single-user deployment. `simulate` may cancel
  any simulation, replay, evaluation or population job, and `ingest` may cancel ingestion, publication or
  maintenance jobs, whoever submitted them. This is pinned by the R2-07 test in
  `tests/integration/test_api_endpoints.py`. For per-owner cancellation, compare `ops.job.created_by` with
  `principal.actor` for token principals.
* **I2 Global login budget as a lockout lever.** Sixty bad attempts per minute from any set of peers return
  429 for everyone, the admin included. This is the price of bounding distributed guessing. Recommendation:
  add an edge or proxy per-IP limit in production.
* **I3 Unauthenticated `/health/ready`** reports the runtime role name, the alembic revision and a check
  summary. It is needed by the Render health check. The disclosure has low value; trim `detail` in
  production if desired.
* **I4 Invalid Bearer tokens** each cost one indexed `SELECT` with no throttle. The cost is cheap and bounded
  by the token-length check (≤512).
* **I5 Provenance `storage_location` nodes** expose `storage_uri` (bucket and prefix) to authenticated readers
  by design (lineage).
* **I6 TrustedHost rejections** (400 `Invalid host header`) are plain text without security headers. They
  come from the outermost Starlette middleware, are harmless and are not worth a custom middleware.

## Follow-up for other owners

* `docs/api.md` (agent I) says the running service serves `/openapi.json`. In production this now requires
  authentication (`read` scope). The page should say so, and mention the 413 cap (1 MiB) and that rotating
  the admin password hash ends all sessions.
