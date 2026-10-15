# Redaction check (R7): secrets in logs, errors, job records, audit, outbox, artifacts, traces

Date: 2026-10-15. Test file: `tests/security/test_redaction_r7.py`. It plants unique canaries
(`CANARY<kind><hex>`) and asserts they never show up. Each guard test first checks that the raw
material really contains the canary, so a pass is meaningful. DB tests run against throwaway
databases only: the session `db` fixture plus a module-scoped `reva_test_r7_<hex>` database that is
dropped afterwards. The dev database `reva` is never used. The API subprocess test points at port 1.

## Results

| Run | Result |
|---|---|
| `pytest tests/security/test_redaction_r7.py` | **23 passed, 6 xfailed (strict)** |
| `pytest tests/security tests/unit/test_settings.py tests/integration/test_queue_telemetry.py` | 694 passed, 5 skipped, 6 xfailed, 4 failed. None of the 4 failures comes from these changes; see the note below |
| `pytest tests/unit tests/integration/test_queue_{gc,reliability,invalidation}.py tests/contracts` | 636 passed |
| `ruff check` on every touched file | clean |

The 4 unrelated failures:

* `test_http_connectors::test_comtrade_hostile_strings_are_inert_data`: the Comtrade
  `classification_unreviewed` rule, which is in-flight connector work.
* `test_db_hardening::test_bootstrap_with_non_superuser_createrole_owner`: in-flight migration work.
* `test_start_env::test_bootstrap_runtime_reports_schema_without_secrets` and
  `test_governance_r5::test_registry_seed_...` were flaky. Both pass on re-run. They failed because
  other sessions were creating cluster-global `fx1_owner_*` roles and changing migrations at the
  same time.

Each strict xfail marks a leak in a file owned by someone else. It names the patch below. Once the
patch lands the test XPASSes, which fails the run, so the marker must then be removed. Each xfail
was run with `--runxfail` to confirm it fails because of the leak, not because of a bug in the test.

## Findings by area

1. **Settings.** `repr`, `str`, `model_dump`, `model_dump_json`, `runtime_url` and `migration_url`
   all mask every `SecretStr` and the owner DSN password (tested). Validation errors never echo
   inputs on the `__init__` path.
   - Fixed: the `Settings.model_validate` path (used by scripts and tests) kept raw inputs in
     `errors()`. It now redacts them the same way.
   - Fixed: plain URL settings (`REVA_S3_ENDPOINT_URL`, `REVA_PUBLIC_BASE_URL`,
     `REVA_SANDBOX_ENDPOINT`, `REVA_CORS_ORIGINS`) could carry `user:pw@`, which then appeared in
     reprs. They are now rejected.
   - Fixed: secrets read from `./.env` never reach `os.environ`, so log redaction did not know their
     values. Settings now registers every secret value with telemetry (bounded, values of 6 chars or
     more).
   - Worker refused-start stderr was tested with a real subprocess: it names the settings, never the
     values.
2. **SafeHttpClient.** URL keys never reach errors, tracebacks, metadata or httpx logs, even at
   DEBUG (tested: 429/Retry-After, Retry-After over budget, transport errors with their chained
   cause, redirect to a host off the allowlist, http plus userinfo).
   - Fixed: a redirect to a different origin re-sent `Authorization` and provider-key headers to the
     new host. Credential headers are now dropped on a cross-origin redirect.
   - Fixed: `redact_url` now also masks the userinfo password.
   - Fixed: presigned-S3 `X-Amz-Signature`, `X-Amz-Credential` and `X-Amz-Security-Token` are now
     secret query parameters.
   - Fixed: `HttpResult.__repr__` no longer shows the raw URL or the body.
3. **DB errors.** These were tested with real failures: permission denied while looking up a token
   hash, and a unique violation whose `DETAIL` echoes `Key (token_sha256)=(...)`. SQLAlchemy's
   `str(exc)` includes `[parameters: {...}]` with the token hash.
   - Fixed in `telemetry.redact_text` (used by the log filter, `queue.error_document` and the outbox):
     `[parameters: ...]` becomes `***`, and PostgreSQL value echoes are masked (`Key (...)=(...)`,
     `Failing row contains`, `invalid input syntax ...: "v"`, `JSON data, line N:`).
   - End to end with the real `Worker.execute`: `ops.job.last_error`, `public_error()` and the worker
     log contain no parameters.
   - Still open, defence in depth: patch P1 (`hide_parameters=True`).
4. **API 500 responses.** Handled DB errors and unhandled exceptions return generic RFC 9457 bodies
   with `request_id`: no SQL, no traceback, no class names (tested in-process).
   - **Leak (P4):** the API process never installs the redacting JSON handler. Under uvicorn,
     `log.exception(...)` in `api/errors.py` falls through to Python's last-resort handler, which
     prints **plain, unredacted tracebacks** to stderr (bound parameters, `Authorization` values).
     The `reva.api.access` INFO lines are dropped. This was reproduced in a subprocess that runs the
     module app the way uvicorn does. The same subprocess with `configure_logging` applied (the
     patch) is clean and the errors are logged as JSON (tested).
   - `configure_logging` now also takes over the `uvicorn`, `uvicorn.error` and `uvicorn.access`
     loggers, so "Exception in ASGI application" tracebacks and access lines with query strings go
     through redaction. It also pins httpx, httpcore, botocore, boto3, s3transfer, urllib3 and
     sqlalchemy at WARNING or above.
   - P5: the unhandled-500 response has `request_id` in its body but no `X-Request-ID` header.
5. **Audit and outbox.** Tested flow: failed login, a 422 with the password in the body, login,
   a secret in a query string, logout, bearer token, bad token, and a DB failure during token
   lookup. After it, `ops.audit`, `ops.outbox`, `ops.job`, the stored artifacts and the logs contain
   none of: admin password, wrong password, session cookie, CSRF token, API token, token SHA-256.
   - Fixed: `operations/audit.py` ran text redaction over serialized JSON, which the new tail
     patterns could have cut off. It now redacts leaf by leaf (`redact_obj`: secret-named keys are
     masked whole), so the document stays valid JSON.
   - Recommendation P8: `session.login_failed` stores the submitted `username[:64]` verbatim in the
     immutable audit table. A password typed into the username field persists there.
6. **Sandbox.** The child environment is not empty but an allowlist: `LANG`, `LC_ALL`, `TZ`, a fixed
   `PATH`, `PYTHON*`, a temporary `HOME`/`TMPDIR`, dead proxies and `REVA_SANDBOX_ISOLATION`.
   Planted DSNs, passwords, `AWS_*` values, the Comtrade key and proxy credentials are all absent
   (tested with a real subprocess).
   - **Leak (P6):** on a non-zero exit the child's stderr is stored verbatim in
     `evidence["child"]["error"]` and the finding message, and from there in the evaluation
     artifact. A child that read the worker's `/proc/<ppid>/environ` (possible without a PID
     namespace, and already flagged as a warning) could launder a secret into evidence.
7. **Log level and ingest.** The default is `REVA_LOG_LEVEL=INFO` (Settings, `render.yaml`, worker
   `main`). A fixture ingest logged at DEBUG produces only counts and ids: no body snippets, and
   every line is under 2,000 chars (tested). Also tested at DEBUG: no httpx URL lines and no
   sqlalchemy parameter logs.
   - P7: `FixtureHttp` echoes the raw request URL, query keys included, in its "no captured
     fixture" error.

## Patches for files owned by others

**P1 (lead): `src/reva/adapters/postgres/engine.py`, `make_engine`.** Hide bound parameters in
every exception at the source.

```python
    return create_engine(
        url,
        pool_size=s.reva_db_pool_size,
        max_overflow=s.reva_db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
        hide_parameters=True,  # exception text never carries bound values (token hashes, payloads)
        connect_args={
```

**P3 (agent D): `src/reva/adapters/postgres/queue.py`.** Exception `details` are stored unredacted
in `last_error` and shown over HTTP by `public_error`.

```python
def finish_cancelled(engine: Engine, job: ClaimedJob, details: Mapping[str, Any] | None = None) -> None:
    from reva.operations.telemetry import redact_obj

    with engine.begin() as c:
        _complete(c, job, "cancelled", None, {"code": "cancelled", **redact_obj(dict(details or {}))})


def error_document(code: str, message: str, *, retryable: bool, attempt: int, **extra: Any) -> dict[str, Any]:
    from reva.operations.telemetry import redact_obj, redact_text, secret_values

    known = secret_values()
    return {
        "code": code[:64],
        "message": redact_text(message, known)[:MAX_ERROR_MESSAGE],
        "retryable": retryable,
        "attempt": attempt,
        **redact_obj(extra, known),
    }
```

**P4 (SEC): `src/reva/api/app.py`.** Install the redacting JSON handler in the served process, and
bind `request_id` for log correlation.

```python
def _module_app() -> Any:
    from reva.operations.telemetry import configure_logging

    settings = get_settings()
    configure_logging(settings.reva_log_level)  # JSON + RedactionFilter; adopts uvicorn's loggers
    return create_app(settings)
```

In the `_headers` middleware, replace `response = await call_next(request)` with:

```python
        from reva.operations import telemetry as tm

        with tm.log_context(request_id=rid):
            response = await call_next(request)
```

**P5 (SEC): `src/reva/api/errors.py`.** Correlate 500s: add the header, and put the request id on
the log lines.

```python
    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        rid = getattr(request.state, "request_id", None)
        log.exception("unhandled error", extra={"request_id": rid} if rid else None)
        response = problem_response(request, _problem(500, "internal_error", "internal error"))
        if rid:
            response.headers["X-Request-ID"] = rid
        ...
```

In `_db`, change `log.exception("database error")` the same way:
`log.exception("database error", extra={"request_id": getattr(request.state, "request_id", None)})`.

**P6 (G): `src/reva/autonomy/sandbox.py`, end of `TrustedTemplateSandbox._spawn`.** Also redact
`resp` before `evidence["external_response"] = resp` in `_code_proposal`.

```python
        from reva.operations.telemetry import redact_obj, redact_text, secret_values

        known = secret_values()
        if proc.returncode != 0:
            out.setdefault("ok", False)
            out.setdefault("error", redact_text(stderr.decode(errors="replace"), known)[-500:])
        out = redact_obj(out, known)  # child-reported strings become evidence: scrub them all
        out["launcher"] = " ".join(unshare[1:-1]) if unshare else "plain_subprocess"
        return out, usage
```

**P7 (lead): `src/reva/evidence/runner.py:65`.**

```python
raise SourceResponseError(f"no captured fixture for {redact_url(url)}", retryable=False, kind="fixture_missing")
```

**P8 (SEC, recommendation, no test): `src/reva/api/auth.py` `login`.** For
`session.login_rate_limited` and `session.login_failed`, store
`{"username_is_admin": user_ok}` (or `hashlib.sha256(username.encode()).hexdigest()[:16]`)
instead of `username[:64]`.

## Low-risk notes (no change)

- `connectors/base.to_raw` and `autonomy/fixtures` fingerprints hash URLs that may contain secret
  query parameters. The SHA-256 of a high-entropy key is not reversible, and those URLs carry no
  key today: Comtrade uses the public endpoint and `REVA_COMTRADE_API_KEY` is unused.
- `SourceResponseError("transport failure: <type>") from e` keeps the httpx cause. Its text
  carries no URL (tested through the formatted traceback).
- `redact_text` works on plain text. For JSON documents use `redact_obj`; the tail-of-line rules
  are not quote-aware.

## Files changed (redaction only)

- `src/reva/operations/telemetry.py`: shape rules (SQL parameters, PostgreSQL echoes, credential
  headers, bearer and basic tokens, secret query parameters, `reva_` tokens and cookies, Argon2id
  hashes), `register_secret_values`, `redact_obj`, a refreshing `RedactionFilter`, and
  `configure_logging` taking over the uvicorn loggers and quieting credential-bearing libraries.
- `src/reva/operations/audit.py`: structure-preserving redaction.
- `src/reva/adapters/http/safe_client.py`: cross-origin credential-header stripping, userinfo
  redaction, S3 presign parameters, `HttpResult.__repr__`.
- `src/reva/settings.py`: redacted `model_validate`, rejection of credentials in URL settings, and
  registration of secrets for redaction.
- New: `tests/security/test_redaction_r7.py`.
