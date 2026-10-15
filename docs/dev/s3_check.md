# S3 object store check against a real S3 API (2026-10-15)

Scope: `src/reva/adapters/objects/s3.py` (`S3ArtifactStore`) had only been tested
against moto's in-process mock. This check runs it against a real MinIO server,
compares it with the filesystem store's contract, proves the documented
credential split, and exercises `scripts/backup_restore.py --include-objects`
with `REVA_OBJECT_BACKEND=s3`.

Test file: `tests/integration/test_s3_store_minio.py` (marker `s3`). Every S3 test
skips with its reason when `REVA_TEST_S3_ENDPOINT` is unset or unreachable, so CI
stays offline; the offline part (filesystem contract, settings, unreachable
endpoint) always runs.

## Which S3 server, and how it was obtained

| Attempt | Result |
|---|---|
| `docker pull minio/minio:RELEASE.2025-09-07T16-13-09Z` (and `:latest`, `RELEASE.2025-04-22T22-12-26Z`) | `pull access denied ... repository does not exist` (other Docker Hub images pull fine) |
| `docker pull quay.io/minio/minio:RELEASE.2025-04-22T22-12-26Z` | `401 UNAUTHORIZED` |
| `https://dl.min.io/server/minio/release/linux-amd64/...`, `.../client/mc/...` | HTTP 410 Gone (community binaries withdrawn) |
| `dl.min.io/aistor/...` | available, but AIStor is the licensed commercial server: not used |
| **Build from source** (Go module proxy and GitHub are reachable) | **used** |

```sh
S=<scratch dir>
git clone --depth 1 --branch RELEASE.2025-10-15T17-29-55Z https://github.com/minio/minio $S/src/minio   # commit 9e49d5e7a648
git clone --depth 1 --branch RELEASE.2025-08-13T08-35-41Z https://github.com/minio/mc    $S/src/mc
(cd $S/src/minio && go build -trimpath -o $S/bin/minio .)   # go1.24.7 -> toolchain go1.24.8, ~7 min
(cd $S/src/mc    && go build -trimpath -o $S/bin/mc .)      # ~2.5 min
MINIO_ROOT_USER=reva-root MINIO_ROOT_PASSWORD=<random> \
  $S/bin/minio server $S/minio-data --address 127.0.0.1:19000 --console-address 127.0.0.1:19001 &
```

Fidelity: this is real MinIO (single node, single drive, the last community
release tag), with real SigV4 auth, IAM users/policies, conditional writes and
checksum validation. It is not AWS S3; differences that matter are listed under
"Not covered". moto server was not needed (and is a mock: it does not enforce
IAM policies, so it could not prove the credential split).

## Provisioning the credential split (MinIO)

The policies mirror `docs/deployment.md` ("Exact provisioning inputs" 2-3):

`reva-api-readonly.json` — read-only, approved namespaces only (no `quarantine/`):

```json
{"Version":"2012-10-17","Statement":[
 {"Sid":"ReadApprovedPrefixes","Effect":"Allow","Action":["s3:GetObject"],
  "Resource":["arn:aws:s3:::<bucket>/reva/raw/*","arn:aws:s3:::<bucket>/reva/normalised/*",
              "arn:aws:s3:::<bucket>/reva/belief/*","arn:aws:s3:::<bucket>/reva/models/*",
              "arn:aws:s3:::<bucket>/reva/scenarios/*","arn:aws:s3:::<bucket>/reva/evaluation/*"]},
 {"Sid":"ListUnderPrefix","Effect":"Allow","Action":["s3:ListBucket"],"Resource":["arn:aws:s3:::<bucket>"],
  "Condition":{"StringLike":{"s3:prefix":["reva/*"]}}}]}
```

`reva-worker-prefix-write.json` — read/write/delete under `reva/` only:

```json
{"Version":"2012-10-17","Statement":[
 {"Sid":"ObjectsUnderPrefix","Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:DeleteObject"],
  "Resource":["arn:aws:s3:::<bucket>/reva/*"]},
 {"Sid":"ListUnderPrefix","Effect":"Allow","Action":["s3:ListBucket"],"Resource":["arn:aws:s3:::<bucket>"],
  "Condition":{"StringLike":{"s3:prefix":["reva/*"]}}}]}
```

```sh
export MC_CONFIG_DIR=$S/mc-config
mc alias set local http://127.0.0.1:19000 reva-root "$ROOT_PW"
mc mb local/reva-split
mc admin policy create local reva-api-readonly        reva-api-readonly.json
mc admin policy create local reva-worker-prefix-write reva-worker-prefix-write.json
mc admin user add local reva-api-key    "$API_SECRET"
mc admin user add local reva-worker-key "$WORKER_SECRET"
mc admin policy attach local reva-api-readonly        --user reva-api-key
mc admin policy attach local reva-worker-prefix-write --user reva-worker-key
```

## Running

```sh
export REVA_TEST_S3_ENDPOINT=http://127.0.0.1:19000 REVA_TEST_S3_REGION=us-east-1
export REVA_TEST_S3_ACCESS_KEY_ID=reva-root REVA_TEST_S3_SECRET_ACCESS_KEY="$ROOT_PW"   # admin: creates/deletes test buckets
export REVA_TEST_S3_SPLIT_BUCKET=reva-split
export REVA_TEST_S3_API_KEY_ID=reva-api-key REVA_TEST_S3_API_SECRET="$API_SECRET"
export REVA_TEST_S3_WORKER_KEY_ID=reva-worker-key REVA_TEST_S3_WORKER_SECRET="$WORKER_SECRET"
uv run pytest -v tests/integration/test_s3_store_minio.py          # optional: REVA_TEST_S3_LARGE_MB=256
```

(127.0.0.1 is in `NO_PROXY`, so boto3 does not go through the egress proxy.)

## Results (actual)

* Offline (no endpoint): **14 passed, 24 skipped (each with the reason), 1 xfailed**.
* Against MinIO: **38 passed, 1 xfailed** in 33 s, including the `db`-marked full
  backup/restore CLI round trip. Race tests were repeated 5 times with no failures.
* Existing suites still pass with the changed `s3.py`: `test_storage_s3.py` (moto),
  `test_storage_fs.py`, `test_http_storage_fixes.py`, `test_storage_publication.py`,
  `test_backup_restore*.py`, settings/secrets tests: 188 passed, 24 skipped, 1 xfailed.

### 1. Contract parity, content addressing, integrity, never-overwrite

The repository has no backend-neutral store contract suite (`tests/unit/test_storage_fs.py`
is filesystem-specific), so the test file defines one and runs it over both backends
(`backend` fixture, `[filesystem]` and `[s3]`): round trip (put/get/open/head/range),
key = `<prefix><namespace>/sha256/aa/bb/<sha256>`, same bytes idempotent, namespaces
separate, invalid namespace/address rejected, missing object, out-of-band corruption
detected on `get`, re-put of the true bytes over a corrupted object refused
(`IntegrityFailure`) and the stored bytes left untouched (no silent repair),
truncation detected on get and put, `max_bytes`/mid-stream failure leave no object,
16 concurrent identical puts produce one object, `delete_orphan` confined.

Differences found against the filesystem store and fixed in `s3.py`:

| Behaviour | filesystem | S3 before | S3 now |
|---|---|---|---|
| `get`/`open`/`range_read` of a missing object | `FileNotFoundError` | botocore `ClientError(NoSuchKey)` | `FileNotFoundError` |
| `range_read(..., length=0)` | `b""` | `ValueError` | `b""` |
| `range_read` starting past the end | `b""` | `ClientError(InvalidRange)` | `b""` |
| `open()` of corrupted bytes | **not detected** | not detected | `IntegrityFailure` after the last chunk |

The filesystem `open()` gap remains (not owned here): the parity test is
`xfail(strict=True)` for `[filesystem]` (it turns into a failure once fixed, so the marker gets removed). Suggested fix in `filesystem.py`: hash
while yielding and raise `IntegrityFailure` after the last chunk, as `s3.py` now does.

Provider semantics on MinIO:

* `If-None-Match: *` is enforced: 12 concurrent conditional PUTs to one key → exactly
  one 200, eleven `PreconditionFailed` (412).
* A PUT with a wrong `ChecksumSHA256` is rejected and leaves no object.
* A fresh upload reports a `FULL_OBJECT` `ChecksumSHA256`, so `put` verifies a new
  object with one `HEAD ChecksumMode=ENABLED` (no re-download); a reused object is
  re-hashed by streaming it.
* Rogue-writer race: 4 REVA `put`s and 4 foreign conditional writers (same key,
  jittered start) for 40 rounds: both interleavings occur, a REVA `put` returned
  success only when the true bytes landed, otherwise all four raised
  `IntegrityFailure`; at most one foreign write ever succeeded; nothing was
  overwritten. (Scratch run, 60 rounds × 2 sizes: 34-35 REVA wins, 25-26 foreign
  wins, 0 violations.)
* If a provider ignored `If-None-Match`, the window between HEAD and PUT could
  overwrite (identical bytes in the normal case, since keys are content
  addresses); post-write verification would then report `IntegrityFailure` but
  cannot undo it. Bucket versioning (recommended in deployment.md) keeps the
  previous version.

### 2. Credential split (MinIO IAM, policies above)

| Check | Result |
|---|---|
| API key: read (`get`, `open`, `list_uris`) objects written by the worker | allowed |
| API key: `put` | `AccessDenied`, no object created |
| API key: `delete_orphan` | `AccessDenied`, object still present |
| API key: write outside `reva/`, list bucket root, `PutBucketVersioning` | `AccessDenied` |
| API key: read `quarantine/` | `AccessDenied` (by policy: quarantine is not an approved prefix) |
| Worker key: put / idempotent re-put / read under `reva/` | allowed |
| Worker key: delete under `reva/` | **allowed by design** (orphan GC, deployment.md item 3) |
| Worker key: store configured with prefix `outside/` → `put` | `AccessDenied` |
| Worker key: put `outside/x`, put `revax/x`, get/delete `outside/keep`, list `outside/` | `AccessDenied` |
| Worker key: `PutBucketVersioning`, `PutBucketPolicy`, `DeleteBucket` | `AccessDenied` |

Note: the worker policy's `reva/*` resource does not match `revax/...`, but a
policy written as `.../reva*` would; keep the trailing slash.

### 3. Settings, TLS, timeouts, secrets

* Blank `REVA_S3_ENDPOINT_URL` / `REVA_S3_REGION` (`""` or spaces) are now treated as
  unset (previously boto3 raised `ValueError: Invalid endpoint`); missing bucket or
  keys raise `ConfigurationError` instead of `AttributeError`, and the ambient AWS
  credential chain is never used.
* `http://` endpoint refused when `REVA_ENV=production` (`ConfigurationError`).
* TLS verification: left to botocore (on). Verified against a second MinIO with a
  self-signed certificate on `https://127.0.0.1:19443`: `put` → `SSLError: SSL
  validation failed`, `probe()` → `unavailable/SSLError`; with
  `AWS_CA_BUNDLE=<that cert>` both succeed. `AWS_CA_BUNDLE` *replaces* the trust
  store (set it to a bundle that also contains the public CAs if needed).
* Bounds: connect 10 s, read 60 s, 4 total attempts (standard retry mode); worst
  case per call ≈ 4 × 70 s + backoff. Unreachable endpoint (connection refused)
  fails with `EndpointConnectionError` in a few seconds.
* Addressing: new setting `REVA_S3_ADDRESSING_STYLE` = `auto` (default) | `path` |
  `virtual`. botocore's `auto` used path-style for the custom MinIO endpoint
  (`http://localhost:19000` works without `MINIO_DOMAIN`); `virtual` fails there.
* `S3ArtifactStore.probe()` (+ `factory.probe_store(store)`): one `ListObjectsV2`
  (`MaxKeys=1`, prefix) on a separate client with 2 s connect / 3 s read / 1 attempt;
  never raises; returns `{"backend","status","error","latency_ms"}` with no bucket,
  key or credential. Allowed by both the API and worker policies (`HeadBucket` is
  not: it is denied under a prefix-conditioned `ListBucket`).
* Canary keys (`AKIACANARY…`, `canarySecret/…`) against the real server
  (`InvalidAccessKeyId`): neither value appears in `str`/`repr`/traceback of the
  exceptions, `repr(settings)`, `settings.model_dump()`, `repr(store)`, the probe
  result, or the JSON log stream of `telemetry.configure_logging("DEBUG")` even with
  botocore/boto3/urllib3 loggers forced to DEBUG (the redaction filter masks them).
  **Without** REVA's logging setup, a plain root handler at DEBUG receives
  botocore's request headers and the **access key id leaks** (in the SigV4
  `Authorization: ... Credential=<key id>/...` header); the secret never does.
  The worker installs `configure_logging`; the API process (uvicorn via
  `reva serve` / `scripts/start_api.py`) does not, so it relies on botocore staying
  at the default WARNING level.

### 4. Large object (≥ 50 MB)

Measured in a child process (`ru_maxrss`, after a warm-up put/open):

| Size | put (spool + hash + single PUT + checksum HEAD) | `open()` stream + hash | re-put (stream re-hash) | peak RSS growth | `get()` |
|---|---|---|---|---|---|
| 64 MiB | 1.5 s | 0.35 s | included in peak | **0 MiB** (peak stayed at the 96 MB baseline) | +56 MiB |
| 256 MiB | 5.1 s | 1.8 s | included in peak | **0 MiB** (peak 90 MB) | +350 MiB |

Streaming put/open/re-put memory is bounded (one 1 MiB chunk). `get()` returns
`bytes` by contract, so the whole object is in memory (≈1.4× its size transiently
on botocore's read). Multipart upload is **not implemented**: one `PutObject` from
the spool file, so the limit is S3's 5 GiB single-PUT maximum (now enforced as an
upper bound on `max_bytes`; the store default is 1 GiB). The spool file needs local
disk equal to the object size.

### 5. Backup/restore with S3

* `scripts/backup.sh --include-objects` and `scripts/restore.sh --restore-objects
  --verify-objects` **work** with `REVA_OBJECT_BACKEND=s3`: throwaway source DB
  (bootstrap, seed, World Bank fixture ingest, one belief release) on bucket A,
  restored into a fresh DB and a fresh empty bucket B; manifests match, every
  artifact backed and verified, releases re-verify, archive members = inventory
  count, no secret in any backup file or script output
  (`test_backup_restore_cli_round_trip_with_s3`).
* Function level: inventory → archive → restore across buckets is byte-identical,
  re-restore is idempotent; a corrupted object is flagged `content_ok=false` by the
  inventory and makes `write_objects_archive` fail without leaving a partial tar.
* Gaps (no script edits made; BKP owns `scripts/backup*`):
  1. **Credential**: the backup needs `GetObject` on every namespace including
     `quarantine/`. With the read-only API key the inventory fails with
     `AccessDenied` as soon as a quarantine object exists (test
     `test_backup_with_read_only_api_credential_cannot_see_quarantine`); the worker
     key works but carries write/delete rights. Recommend a dedicated backup key:
     `s3:GetObject` on `reva/*` + prefix-conditioned `s3:ListBucket`. Restore needs
     `PutObject` (worker key or an admin key on the target bucket).
  2. Every object is downloaded **twice** (inventory re-hashes content, then the
     archive streams it again): 2× egress/time on large stores. Possible patch:
     `object_inventory(settings, verify_content=not include_objects)` in `backup()`
     — the archive writer already re-hashes every member and fails on mismatch
     (content_ok would then be `None` for archived objects in `objects.jsonl`).
  3. The archive is a local tar (plaintext unless `age` recipients are given) of
     the full object store: local disk ≥ total object bytes.
  4. Only current object versions are backed up (bucket versioning history is not).

## Code changes

* `src/reva/adapters/objects/s3.py`: blank endpoint/region → unset; explicit
  `ConfigurationError`s (bucket, keys, https in production); bounded client config
  (connect/read timeouts, total attempts, addressing style); missing objects →
  `FileNotFoundError`; `range_read` parity (length 0, past end); streaming `open()`
  verifies the hash; `put` tolerates a 403 on the pre-PUT HEAD (AWS returns 403 for
  a missing key when `s3:ListBucket` is prefix-conditioned; the conditional PUT then
  decides) and briefly re-HEADs after a `ConditionalRequestConflict`; `max_bytes`
  capped at 5 GiB; `probe()`.
* `src/reva/adapters/objects/factory.py`: `probe_store(store)`.
* `src/reva/settings.py`: one S3 field, `reva_s3_addressing_style`.
* `pyproject.toml`: `s3` marker.

## Not covered / limits of this check

* AWS-specific behaviour: 403 vs 404 on HEAD of a missing key under a
  prefix-conditioned `ListBucket` (MinIO returned 404 for both keys; the code now
  handles both), `409 ConditionalRequestConflict`, and `ChecksumType` semantics on
  AWS were not exercised.
* Multi-node MinIO / erasure coding, bucket versioning and object lock, provider
  throttling (503 SlowDown) under load.
* Filesystem `open()` integrity (xfail, see above); `reva.operations.gc` against S3
  (it uses `list_uris`/`uri_mtime`/`delete_orphan`, all exercised here individually).
