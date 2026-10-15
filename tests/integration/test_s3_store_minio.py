"""S3ArtifactStore against a real S3 API (MinIO or another S3-compatible server).

The store contract (content addressing, verified reads, never-overwrite, bounded
memory, confinement) is run parametrized over the filesystem store (always) and
the S3 store (when an endpoint is configured), plus S3-only checks: provider
conditional writes and checksum validation, the documented API/worker
credential split, client settings, secret hygiene, a >= 50 MB streaming
object, and backup/restore of objects (``scripts/backup_restore.py``).

Environment (every S3 test skips with the reason when it is missing/unreachable):

* ``REVA_TEST_S3_ENDPOINT`` (e.g. ``http://127.0.0.1:19000``), ``REVA_TEST_S3_REGION``
  (default ``us-east-1``), ``REVA_TEST_S3_ACCESS_KEY_ID`` / ``REVA_TEST_S3_SECRET_ACCESS_KEY``:
  an admin credential that may create/delete buckets (each session uses fresh,
  uniquely named buckets and removes them afterwards).
* Credential split (optional): ``REVA_TEST_S3_SPLIT_BUCKET`` plus
  ``REVA_TEST_S3_API_KEY_ID``/``_API_SECRET`` and ``REVA_TEST_S3_WORKER_KEY_ID``/
  ``_WORKER_SECRET`` provisioned with the policies of docs/deployment.md
  (see docs/dev/s3_check.md for the exact MinIO commands).
* ``REVA_TEST_S3_LARGE_MB`` (default 64) for the streaming test.

Run: ``uv run pytest -m s3 tests/integration/test_s3_store_minio.py`` (the full
backup round trip additionally needs the ``db`` fixture and pg_dump >= server).
"""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

boto3 = pytest.importorskip("boto3")

from botocore.config import Config  # noqa: E402
from botocore.exceptions import ClientError, EndpointConnectionError  # noqa: E402

from reva.adapters.objects.common import object_key  # noqa: E402
from reva.adapters.objects.filesystem import FilesystemArtifactStore  # noqa: E402
from reva.adapters.objects.s3 import (  # noqa: E402
    CONNECT_TIMEOUT_S,
    READ_TIMEOUT_S,
    TOTAL_ATTEMPTS,
    S3ArtifactStore,
)
from reva.kernel.errors import ConfigurationError, IntegrityFailure, UnsafeRequest  # noqa: E402
from reva.settings import Settings  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
ENDPOINT = os.environ.get("REVA_TEST_S3_ENDPOINT", "").strip()
REGION = os.environ.get("REVA_TEST_S3_REGION", "us-east-1")
ADMIN_ID = os.environ.get("REVA_TEST_S3_ACCESS_KEY_ID", "")
ADMIN_SECRET = os.environ.get("REVA_TEST_S3_SECRET_ACCESS_KEY", "")
DENIED = ("AccessDenied", "403", "Forbidden")


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def code(e: BaseException) -> str:
    return str(getattr(e, "response", {}).get("Error", {}).get("Code", ""))


def raw_client(key_id: str, secret: str, **cfg: Any) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        region_name=REGION,
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        config=Config(retries={"total_max_attempts": 1}, connect_timeout=3, read_timeout=30, **cfg),
    )


def s3_settings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "reva_object_backend": "s3",
        "reva_s3_endpoint_url": ENDPOINT,
        "reva_s3_region": REGION,
        "reva_s3_access_key_id": ADMIN_ID,
        "reva_s3_secret_access_key": ADMIN_SECRET,
    }
    base.update(over)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


# --------------------------------------------------------------------------- availability
_availability: dict[str, str | None] = {}


def _s3_unavailable_reason() -> str | None:
    if "reason" in _availability:
        return _availability["reason"]
    reason: str | None = None
    if not ENDPOINT:
        reason = "no S3 endpoint: set REVA_TEST_S3_ENDPOINT (+ REVA_TEST_S3_ACCESS_KEY_ID/SECRET_ACCESS_KEY)"
    elif not (ADMIN_ID and ADMIN_SECRET):
        reason = "REVA_TEST_S3_ACCESS_KEY_ID / REVA_TEST_S3_SECRET_ACCESS_KEY not set"
    else:
        try:
            raw_client(ADMIN_ID, ADMIN_SECRET).list_buckets()
        except Exception as e:  # unreachable endpoint or bad admin credential
            reason = f"S3 endpoint {ENDPOINT} unavailable: {type(e).__name__} {code(e)}".strip()
    _availability["reason"] = reason
    return reason


def require_s3() -> None:
    reason = _s3_unavailable_reason()
    if reason:
        pytest.skip(reason)


def _empty_and_delete(client: Any, bucket: str) -> None:
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        for o in page.get("Contents", []):
            client.delete_object(Bucket=bucket, Key=o["Key"])
    client.delete_bucket(Bucket=bucket)


@pytest.fixture(scope="session")
def admin() -> Any:
    require_s3()
    return raw_client(ADMIN_ID, ADMIN_SECRET)


@pytest.fixture(scope="session")
def make_bucket(admin: Any) -> Iterator[Callable[[], str]]:
    created: list[str] = []

    def make() -> str:
        name = f"reva-it-{uuid.uuid4().hex[:16]}"
        admin.create_bucket(Bucket=name)
        created.append(name)
        return name

    yield make
    for b in created:
        try:
            _empty_and_delete(admin, b)
        except Exception:  # best effort cleanup
            pass


@pytest.fixture(scope="session")
def bucket(make_bucket: Callable[[], str]) -> str:
    return make_bucket()


# --------------------------------------------------------------------------- backend abstraction
@dataclass
class Backend:
    name: str
    store: Any
    corrupt: Callable[[str, str, bytes], None]  # out-of-band rewrite of an object's bytes
    stored_bytes: Callable[[str, str], bytes]  # out-of-band read of an object's bytes
    keys: Callable[[], list[str]]  # every object key the store wrote (relative to its root/bucket)


def _fs_backend(tmp_path: Path) -> Backend:
    store = FilesystemArtifactStore(tmp_path / "objects")

    def path(ns: str, h: str) -> Path:
        return store.root / object_key(store.prefix, ns, h)

    def corrupt(ns: str, h: str, data: bytes) -> None:
        p = path(ns, h)
        os.chmod(p, 0o644)
        p.write_bytes(data)

    def keys() -> list[str]:
        return sorted(
            str(p.relative_to(store.root)) for p in store.root.rglob("*") if p.is_file() and ".staging" not in p.parts
        )

    return Backend("filesystem", store, corrupt, lambda ns, h: path(ns, h).read_bytes(), keys)


def _s3_backend(admin: Any, bucket: str, tmp_path: Path) -> Backend:
    prefix = f"t-{uuid.uuid4().hex[:12]}/"
    store = S3ArtifactStore(admin, bucket, prefix, spool_dir=str(tmp_path))

    def corrupt(ns: str, h: str, data: bytes) -> None:
        # Non-conditional overwrite with copied metadata: what a bug or attacker with write access does.
        admin.put_object(Bucket=bucket, Key=object_key(prefix, ns, h), Body=data, Metadata={"reva-sha256": h})

    def stored(ns: str, h: str) -> bytes:
        return bytes(admin.get_object(Bucket=bucket, Key=object_key(prefix, ns, h))["Body"].read())

    def keys() -> list[str]:
        out = []
        for page in admin.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            out += [o["Key"] for o in page.get("Contents", [])]
        return sorted(out)

    return Backend("s3", store, corrupt, stored, keys)


@pytest.fixture(params=["filesystem", pytest.param("s3", marks=pytest.mark.s3)])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> Backend:
    if request.param == "filesystem":
        return _fs_backend(tmp_path)
    require_s3()
    return _s3_backend(request.getfixturevalue("admin"), request.getfixturevalue("bucket"), tmp_path)


class ZeroStream(io.RawIOBase):
    def __init__(self, total: int, fail_after: int | None = None, byte: bytes = b"\x00") -> None:
        self.total, self.sent, self.fail_after, self.byte = total, 0, fail_after, byte

    def readable(self) -> bool:
        return True

    def read(self, n: int = -1) -> bytes:
        if self.fail_after is not None and self.sent >= self.fail_after:
            raise ConnectionResetError("injected mid-stream failure")
        n = self.total - self.sent if n < 0 else min(n, self.total - self.sent)
        self.sent += n
        return self.byte * n


# =========================================================================== contract (both backends)
def test_contract_roundtrip(backend: Backend) -> None:
    st = backend.store
    data = os.urandom(10_000)
    obj = st.put(io.BytesIO(data), "raw", "application/octet-stream", max_bytes=len(data))
    assert (obj.sha256, obj.byte_size, obj.namespace, obj.media_type) == (
        sha(data),
        len(data),
        "raw",
        "application/octet-stream",
    )
    assert st.get("raw", obj.sha256) == data
    assert b"".join(st.open("raw", obj.sha256)) == data
    head = st.head("raw", obj.sha256)
    assert head is not None and (head.sha256, head.byte_size, head.storage_uri) == (
        obj.sha256,
        len(data),
        obj.storage_uri,
    )
    assert st.range_read("raw", obj.sha256, 5, 7) == data[5:12]
    assert st.range_read("raw", obj.sha256, len(data) - 3, 100) == data[-3:]
    assert st.range_read("raw", obj.sha256, 0, 0) == b""
    assert st.range_read("raw", obj.sha256, len(data) + 10, 5) == b""
    with pytest.raises(ValueError):
        st.range_read("raw", obj.sha256, -1, 2)


def test_contract_content_addressing(backend: Backend) -> None:
    st = backend.store
    data = b'{"a":1}' + os.urandom(8)
    a = st.put_bytes(data, "raw", "application/json")
    b = st.put_bytes(data, "raw", "application/json")  # same bytes: idempotent, same object
    assert a.storage_uri == b.storage_uri
    assert a.storage_uri.endswith(object_key(st.prefix, "raw", sha(data)))
    assert backend.keys() == [object_key(st.prefix, "raw", sha(data))]
    c = st.put_bytes(data, "quarantine", "application/json")
    assert c.storage_uri != a.storage_uri and len(backend.keys()) == 2
    assert st.head("belief", sha(data)) is None
    assert [u for u, _ in st.list_uris("raw")] == [a.storage_uri]


def test_contract_rejects_bad_namespace_and_address(backend: Backend) -> None:
    st = backend.store
    for ns in ("../../etc", "public", ""):
        with pytest.raises(ValueError):
            st.put_bytes(b"x", ns, "text/plain")
    for bad in ("../../../../etc/passwd", "A" * 64, "a" * 63, "", "a" * 62 + "/."):
        with pytest.raises(ValueError):
            st.head("raw", bad)
        with pytest.raises(ValueError):
            st.get("raw", bad)
    assert backend.keys() == []


def test_contract_missing_object(backend: Backend) -> None:
    st, missing = backend.store, "0" * 64
    assert st.head("raw", missing) is None
    with pytest.raises(FileNotFoundError):
        st.get("raw", missing)
    with pytest.raises(FileNotFoundError):
        b"".join(st.open("raw", missing))
    with pytest.raises(FileNotFoundError):
        st.range_read("raw", missing, 0, 4)


def test_contract_corruption_detected_on_get(backend: Backend) -> None:
    st = backend.store
    obj = st.put_bytes(b"original source bytes", "raw", "text/plain")
    backend.corrupt("raw", obj.sha256, b"tampered source bytes")
    with pytest.raises(IntegrityFailure) as ei:
        st.get("raw", obj.sha256)
    assert ei.value.code == "integrity_failure"


def test_contract_corruption_detected_on_streaming_open(backend: Backend) -> None:
    st = backend.store
    obj = st.put_bytes(os.urandom(3 * 1024 * 1024), "raw", "application/octet-stream")
    backend.corrupt("raw", obj.sha256, os.urandom(3 * 1024 * 1024))
    if backend.name == "filesystem":
        pytest.xfail("FilesystemArtifactStore.open() streams without verifying (reported in docs/dev/s3_check.md)")
    with pytest.raises(IntegrityFailure):
        for _ in st.open("raw", obj.sha256):
            pass


def test_contract_never_overwrites_or_repairs(backend: Backend) -> None:
    st = backend.store
    data = b"canonical bytes " + os.urandom(16)
    obj = st.put_bytes(data, "raw", "text/plain")
    backend.corrupt("raw", obj.sha256, b"corrupted!!")
    with pytest.raises(IntegrityFailure):
        st.put_bytes(data, "raw", "text/plain")  # re-putting the true bytes reports, never repairs
    assert backend.stored_bytes("raw", obj.sha256) == b"corrupted!!"


def test_contract_truncation_detected(backend: Backend) -> None:
    st = backend.store
    data = os.urandom(4096)
    obj = st.put_bytes(data, "normalised", "application/octet-stream")
    backend.corrupt("normalised", obj.sha256, data[:100])
    with pytest.raises(IntegrityFailure):
        st.get("normalised", obj.sha256)
    with pytest.raises(IntegrityFailure):
        st.put_bytes(data, "normalised", "application/octet-stream")


def test_contract_bounds_leave_nothing(backend: Backend) -> None:
    st = backend.store
    with pytest.raises(UnsafeRequest):
        st.put(ZeroStream(5 * 1024 * 1024), "raw", "application/octet-stream", max_bytes=3 * 1024 * 1024)
    with pytest.raises(ConnectionResetError):
        st.put(ZeroStream(8 * 1024 * 1024, fail_after=3 * 1024 * 1024), "raw", "x", max_bytes=16 * 1024 * 1024)
    assert backend.keys() == []


def test_contract_concurrent_identical_puts(backend: Backend) -> None:
    st = backend.store
    data = os.urandom(1024 * 1024)
    barrier = threading.Barrier(16)

    def put(_: int) -> str:
        barrier.wait()
        return st.put_bytes(data, "raw", "application/octet-stream").storage_uri  # type: ignore[no-any-return]

    with ThreadPoolExecutor(16) as ex:
        uris = list(ex.map(put, range(16)))
    assert len(set(uris)) == 1
    assert backend.keys() == [object_key(st.prefix, "raw", sha(data))]
    assert backend.stored_bytes("raw", sha(data)) == data


def test_contract_delete_orphan_confined(backend: Backend, tmp_path: Path) -> None:
    st = backend.store
    obj = st.put_bytes(b"orphan" + os.urandom(8), "quarantine", "text/plain")
    outside = (
        [f"file://{tmp_path / 'victim'}", "file:///etc/passwd"]
        if backend.name == "filesystem"
        else [f"s3://{st.bucket}/other/thing", f"s3://another-bucket/{st.prefix}raw/x"]
    )
    for uri in outside:
        with pytest.raises(ValueError):
            st.delete_orphan(uri)
    st.delete_orphan(obj.storage_uri)
    assert st.head("quarantine", obj.sha256) is None


# =========================================================================== S3 provider semantics


@pytest.mark.s3
def test_provider_enforces_if_none_match(admin: Any, bucket: str) -> None:
    key = f"cond/{uuid.uuid4().hex}"
    barrier = threading.Barrier(12)

    def put(i: int) -> str:
        barrier.wait()
        try:
            admin.put_object(Bucket=bucket, Key=key, Body=f"writer {i}".encode(), IfNoneMatch="*")
            return "ok"
        except ClientError as e:
            return code(e)

    with ThreadPoolExecutor(12) as ex:
        results = list(ex.map(put, range(12)))
    assert results.count("ok") == 1, results
    assert set(results) - {"ok"} <= {"PreconditionFailed", "ConditionalRequestConflict"}, results


@pytest.mark.s3
def test_provider_validates_sha256_checksum(admin: Any, bucket: str) -> None:
    import base64

    key = f"cksum/{uuid.uuid4().hex}"
    wrong = base64.b64encode(hashlib.sha256(b"something else").digest()).decode()
    with pytest.raises(ClientError) as ei:
        admin.put_object(Bucket=bucket, Key=key, Body=b"payload", ChecksumSHA256=wrong)
    assert code(ei.value) in ("BadDigest", "XAmzContentChecksumMismatch", "InvalidDigest", "XAmzContentSHA256Mismatch")
    with pytest.raises(ClientError):
        admin.head_object(Bucket=bucket, Key=key)


@pytest.mark.s3
def test_fresh_upload_verified_by_provider_full_object_checksum(admin: Any, bucket: str, tmp_path: Path) -> None:
    st = S3ArtifactStore(admin, bucket, f"t-{uuid.uuid4().hex[:8]}/", spool_dir=str(tmp_path))
    data = os.urandom(2048)
    key = object_key(st.prefix, "raw", sha(data))
    st.put_bytes(data, "raw", "application/octet-stream")
    r = admin.head_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
    assert r.get("ChecksumType", "FULL_OBJECT") == "FULL_OBJECT"
    assert st._server_checksum_matches(key, sha(data), len(data))


@pytest.mark.s3
def test_store_put_racing_rogue_writer_never_reports_success_over_foreign_bytes(
    admin: Any, bucket: str, tmp_path: Path
) -> None:
    """A rogue conditional writer and REVA writers race for one key: whoever lands first
    wins, nothing is overwritten, and a REVA put succeeds only if the true bytes landed."""
    st = S3ArtifactStore(admin, bucket, f"t-{uuid.uuid4().hex[:8]}/", spool_dir=str(tmp_path))
    for _ in range(8):
        data = os.urandom(256 * 1024)
        key = object_key(st.prefix, "raw", sha(data))
        barrier = threading.Barrier(8)

        def reva(_: int) -> str:
            barrier.wait()
            try:
                st.put_bytes(data, "raw", "application/octet-stream")
                return "stored"
            except IntegrityFailure:
                return "integrity"

        def rogue(i: int) -> str:
            barrier.wait()
            try:
                admin.put_object(
                    Bucket=bucket, Key=key, Body=b"rogue %d" % i, Metadata={"reva-sha256": sha(data)}, IfNoneMatch="*"
                )
                return "rogue_ok"
            except ClientError as e:
                return code(e)

        with ThreadPoolExecutor(8) as ex:
            futs = [ex.submit(reva, i) for i in range(4)] + [ex.submit(rogue, i) for i in range(4)]
            out = [f.result() for f in futs]
        final = bytes(admin.get_object(Bucket=bucket, Key=key)["Body"].read())
        reva_out, rogue_out = out[:4], out[4:]
        if final == data:
            assert reva_out == ["stored"] * 4 and "rogue_ok" not in rogue_out, out
        else:
            assert final.startswith(b"rogue ") and rogue_out.count("rogue_ok") == 1, out
            assert reva_out == ["integrity"] * 4, out


# =========================================================================== settings
def test_settings_blank_endpoint_and_region_are_unset() -> None:
    st = S3ArtifactStore.from_settings(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            reva_object_backend="s3",
            reva_s3_endpoint_url="",
            reva_s3_region=" ",
            reva_s3_bucket="b",
            reva_s3_access_key_id="k",
            reva_s3_secret_access_key="s",
        )
    )
    assert st.client.meta.endpoint_url.startswith("https://")  # AWS default endpoint, not ValueError('')


def test_settings_client_bounds_tls_and_addressing() -> None:
    st = S3ArtifactStore.from_settings(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            reva_object_backend="s3",
            reva_s3_endpoint_url="https://s3.eu-central-1.amazonaws.com",
            reva_s3_region="eu-central-1",
            reva_s3_bucket="b",
            reva_s3_access_key_id="k",
            reva_s3_secret_access_key="s",
            reva_s3_addressing_style="path",
        )
    )
    cfg = st.client.meta.config
    assert (cfg.connect_timeout, cfg.read_timeout) == (CONNECT_TIMEOUT_S, READ_TIMEOUT_S)
    assert cfg.retries == {"total_max_attempts": TOTAL_ATTEMPTS, "mode": "standard"}
    assert cfg.s3["addressing_style"] == "path"
    verify = st.client._endpoint.http_session._verify
    assert verify is True or (isinstance(verify, str) and Path(verify).is_file())  # never disabled
    pcfg = st.probe_client.meta.config
    assert pcfg.retries["total_max_attempts"] == 1 and pcfg.connect_timeout <= 2 and pcfg.read_timeout <= 3


def test_settings_reject_plaintext_endpoint_in_production_and_missing_credentials() -> None:
    prod = Settings(  # type: ignore[call-arg]
        _env_file=None,
        reva_env="production",
        reva_process_role="worker",
        reva_object_backend="s3",
        reva_s3_endpoint_url="http://minio.internal:9000",
        reva_s3_bucket="b",
        reva_s3_access_key_id="k",
        reva_s3_secret_access_key="s",
        reva_db_sslmode="verify-full",
        reva_worker_db_password="w",
        reva_sim_db_password="x",
    )
    with pytest.raises(ConfigurationError, match="https"):
        S3ArtifactStore.from_settings(prod)
    with pytest.raises(ConfigurationError):
        S3ArtifactStore.from_settings(
            Settings(_env_file=None, reva_object_backend="s3", reva_s3_bucket="b")  # type: ignore[call-arg]
        )


def test_unreachable_endpoint_fails_bounded_and_probe_never_raises(tmp_path: Path) -> None:
    st = S3ArtifactStore.from_settings(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            reva_object_backend="s3",
            reva_s3_endpoint_url="http://127.0.0.1:9",  # discard port: connection refused
            reva_s3_region="us-east-1",
            reva_s3_bucket="b",
            reva_s3_access_key_id="k",
            reva_s3_secret_access_key="s",
        )
    )
    st.spool_dir = str(tmp_path)
    t0 = time.monotonic()
    with pytest.raises(EndpointConnectionError):
        st.put_bytes(b"x", "raw", "text/plain")
    assert time.monotonic() - t0 < 60
    t0 = time.monotonic()
    res = st.probe()
    assert res["status"] == "unavailable" and res["error"] == "EndpointConnectionError"
    assert time.monotonic() - t0 < 10
    assert list(tmp_path.iterdir()) == []  # spool removed


@pytest.mark.s3
def test_probe_ok_against_real_endpoint(bucket: str) -> None:
    res = S3ArtifactStore.from_settings(s3_settings(reva_s3_bucket=bucket)).probe()
    assert res["status"] == "ok" and res["error"] is None and bucket not in json.dumps(res)


@pytest.mark.s3
def test_canary_credentials_never_in_exceptions_or_logs(bucket: str) -> None:
    from reva.operations import telemetry

    canary_id = "AKIACANARY" + uuid.uuid4().hex[:10].upper()
    canary_secret = "canarySecret/" + uuid.uuid4().hex
    settings = s3_settings(
        reva_s3_bucket=bucket, reva_s3_access_key_id=canary_id, reva_s3_secret_access_key=canary_secret
    )
    root = logging.getLogger()
    saved = (root.level, list(root.handlers), {n: logging.getLogger(n).level for n in telemetry.QUIET_LOGGERS})
    buf = io.StringIO()
    handler = telemetry.configure_logging("DEBUG", stream=buf)
    for n in telemetry.QUIET_LOGGERS:  # worst case: credential-bearing library loggers forced to DEBUG
        logging.getLogger(n).setLevel(logging.DEBUG)
    try:
        st = S3ArtifactStore.from_settings(settings)
        texts: list[str] = [repr(settings), str(settings.model_dump()), repr(st.__dict__)]
        for op in (
            lambda: st.put_bytes(b"canary payload", "raw", "text/plain"),
            lambda: st.get("raw", "0" * 64),
            lambda: list(st.list_uris("raw")),
        ):
            with pytest.raises(ClientError) as ei:
                op()
            assert code(ei.value) in ("InvalidAccessKeyId", "SignatureDoesNotMatch", "AccessDenied", "403")
            texts += [str(ei.value), repr(ei.value), "".join(traceback.format_exception(ei.value))]
            logging.getLogger("reva.test.s3").exception("s3 operation failed")
        texts.append(json.dumps(st.probe()))
    finally:
        root.removeHandler(handler)
        root.setLevel(saved[0])
        for h in saved[1]:
            if h not in root.handlers:
                root.addHandler(h)
        for n, lvl in saved[2].items():
            logging.getLogger(n).setLevel(lvl)
    log = buf.getvalue()
    assert "botocore" in log and "s3 operation failed" in log  # the handler saw the library records
    texts.append(log)
    for t in texts:
        assert canary_id not in t and canary_secret not in t


# =========================================================================== credential split
SPLIT_BUCKET = os.environ.get("REVA_TEST_S3_SPLIT_BUCKET", "")


@dataclass
class Split:
    bucket: str
    api: S3ArtifactStore
    worker: S3ArtifactStore
    api_raw: Any
    worker_raw: Any


@pytest.fixture()
def split(admin: Any, tmp_path: Path) -> Iterator[Split]:
    names = ("API_KEY_ID", "API_SECRET", "WORKER_KEY_ID", "WORKER_SECRET")
    vals = {n: os.environ.get(f"REVA_TEST_S3_{n}", "") for n in names}
    if not SPLIT_BUCKET or not all(vals.values()):
        pytest.skip("credential split not provisioned: set REVA_TEST_S3_SPLIT_BUCKET and REVA_TEST_S3_{API,WORKER}_*")
    api_raw = raw_client(vals["API_KEY_ID"], vals["API_SECRET"])
    worker_raw = raw_client(vals["WORKER_KEY_ID"], vals["WORKER_SECRET"])
    before = {
        o["Key"]
        for p in admin.get_paginator("list_objects_v2").paginate(Bucket=SPLIT_BUCKET)
        for o in p.get("Contents", [])
    }
    yield Split(
        SPLIT_BUCKET,
        S3ArtifactStore(api_raw, SPLIT_BUCKET, "reva/", spool_dir=str(tmp_path)),
        S3ArtifactStore(worker_raw, SPLIT_BUCKET, "reva/", spool_dir=str(tmp_path)),
        api_raw,
        worker_raw,
    )
    for p in admin.get_paginator("list_objects_v2").paginate(Bucket=SPLIT_BUCKET):
        for o in p.get("Contents", []):
            if o["Key"] not in before:
                admin.delete_object(Bucket=SPLIT_BUCKET, Key=o["Key"])


@pytest.mark.s3
def test_api_credential_is_read_only(split: Split, admin: Any) -> None:
    data = os.urandom(64)
    obj = split.worker.put_bytes(data, "raw", "application/octet-stream")  # worker writes ...
    assert split.api.get("raw", obj.sha256) == data  # ... API reads
    assert b"".join(split.api.open("raw", obj.sha256)) == data
    assert obj.storage_uri in [u for u, _ in split.api.list_uris("raw")]
    other = os.urandom(64)
    with pytest.raises(ClientError) as ei:
        split.api.put_bytes(other, "raw", "application/octet-stream")
    assert code(ei.value) in DENIED
    assert admin.list_objects_v2(Bucket=split.bucket, Prefix=object_key("reva/", "raw", sha(other)))["KeyCount"] == 0
    with pytest.raises(ClientError) as ei:
        split.api.delete_orphan(obj.storage_uri)
    assert code(ei.value) in DENIED and split.worker.head("raw", obj.sha256) is not None
    for call in (
        lambda: split.api_raw.put_object(Bucket=split.bucket, Key="outside/x", Body=b"x"),
        lambda: split.api_raw.list_objects_v2(Bucket=split.bucket),  # bucket root listing
        lambda: split.api_raw.put_bucket_versioning(
            Bucket=split.bucket, VersioningConfiguration={"Status": "Suspended"}
        ),
    ):
        with pytest.raises(ClientError) as ei:
            call()
        assert code(ei.value) in DENIED


@pytest.mark.s3
def test_api_credential_cannot_read_quarantine(split: Split) -> None:
    obj = split.worker.put_bytes(os.urandom(64), "quarantine", "application/octet-stream")
    with pytest.raises(ClientError) as ei:
        split.api.get("quarantine", obj.sha256)
    assert code(ei.value) in DENIED


@pytest.mark.s3
def test_worker_credential_confined_to_prefix(split: Split, admin: Any, tmp_path: Path) -> None:
    # Inside reva/: write, read, idempotent re-put, and (by design, for orphan GC) delete.
    data = os.urandom(64)
    obj = split.worker.put_bytes(data, "raw", "application/octet-stream")
    assert split.worker.put_bytes(data, "raw", "application/octet-stream").storage_uri == obj.storage_uri
    split.worker.delete_orphan(obj.storage_uri)
    assert split.worker.head("raw", obj.sha256) is None
    # Outside reva/: no write, no read, no delete; the store with another prefix cannot write either.
    admin.put_object(Bucket=split.bucket, Key="outside/keep", Body=b"keep")
    elsewhere = S3ArtifactStore(split.worker_raw, split.bucket, "outside/", spool_dir=str(tmp_path))
    with pytest.raises(ClientError) as ei:
        elsewhere.put_bytes(os.urandom(32), "raw", "application/octet-stream")
    assert code(ei.value) in DENIED
    for call in (
        lambda: split.worker_raw.put_object(Bucket=split.bucket, Key="outside/x", Body=b"x"),
        lambda: split.worker_raw.put_object(Bucket=split.bucket, Key="revax/x", Body=b"x"),
        lambda: split.worker_raw.get_object(Bucket=split.bucket, Key="outside/keep"),
        lambda: split.worker_raw.delete_object(Bucket=split.bucket, Key="outside/keep"),
        lambda: split.worker_raw.list_objects_v2(Bucket=split.bucket, Prefix="outside/"),
        lambda: split.worker_raw.put_bucket_versioning(
            Bucket=split.bucket, VersioningConfiguration={"Status": "Suspended"}
        ),
        lambda: split.worker_raw.put_bucket_policy(
            Bucket=split.bucket, Policy=json.dumps({"Version": "2012-10-17", "Statement": []})
        ),
        lambda: split.worker_raw.delete_bucket(Bucket=split.bucket),
    ):
        with pytest.raises(ClientError) as ei:
            call()
        assert code(ei.value) in DENIED
    assert admin.get_object(Bucket=split.bucket, Key="outside/keep")["Body"].read() == b"keep"


# =========================================================================== large objects
_CHILD = r"""
import hashlib, io, json, os, resource, sys, time
import boto3
from botocore.config import Config
from reva.adapters.objects.s3 import S3ArtifactStore

def rss_kb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

class Gen(io.RawIOBase):
    def __init__(self, total):
        self.total, self.sent = total, 0
        self.block = hashlib.sha256(b"seed").digest() * 32768  # 1 MiB, non-uniform bytes
    def readable(self):
        return True
    def read(self, n=-1):
        n = min(len(self.block), self.total - self.sent) if n < 0 else min(n, self.total - self.sent, len(self.block))
        self.sent += n
        return self.block[:n]

total = int(sys.argv[1]) * 1024 * 1024
env = os.environ
c = boto3.client(
    "s3",
    endpoint_url=env["REVA_TEST_S3_ENDPOINT"],
    region_name=env.get("REVA_TEST_S3_REGION", "us-east-1"),
    aws_access_key_id=env["REVA_TEST_S3_ACCESS_KEY_ID"],
    aws_secret_access_key=env["REVA_TEST_S3_SECRET_ACCESS_KEY"],
    config=Config(retries={"total_max_attempts": 1}),
)
st = S3ArtifactStore(c, sys.argv[2], sys.argv[3], spool_dir=sys.argv[4], default_max_bytes=total)
st.put_bytes(b"warm-up", "raw", "text/plain"); b"".join(st.open("raw", hashlib.sha256(b"warm-up").hexdigest()))
base = rss_kb(); t0 = time.monotonic()
obj = st.put(Gen(total), "raw", "application/octet-stream", max_bytes=total)
put_s = time.monotonic() - t0; after_put = rss_kb(); t0 = time.monotonic()
h = hashlib.sha256(); n = 0
for chunk in st.open("raw", obj.sha256):
    h.update(chunk); n += len(chunk)
open_s = time.monotonic() - t0; after_open = rss_kb()
assert h.hexdigest() == obj.sha256 and n == total
tail = st.range_read("raw", obj.sha256, total - 10, 10)
assert len(tail) == 10
again = st.put(Gen(total), "raw", "application/octet-stream", max_bytes=total)  # reuse path: stream re-hash
assert again.storage_uri == obj.storage_uri
after_reput = rss_kb()
whole = st.get("raw", obj.sha256)  # get() returns bytes: the whole object is in memory by contract
assert len(whole) == total
del whole
after_get = rss_kb()
out = {"bytes": total, "sha256": obj.sha256, "base_rss_kb": base, "after_put_kb": after_put}
out.update(after_open_kb=after_open, after_reput_kb=after_reput, after_get_kb=after_get)
out.update(put_seconds=round(put_s, 2), open_seconds=round(open_s, 2))
print(json.dumps(out))
"""


@pytest.mark.s3
def test_large_object_streams_with_bounded_rss(bucket: str, tmp_path: Path) -> None:
    require_s3()
    mb = int(os.environ.get("REVA_TEST_S3_LARGE_MB", "64"))
    assert mb >= 50
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, str(mb), bucket, f"t-{uuid.uuid4().hex[:8]}/", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    r = json.loads(proc.stdout.strip().splitlines()[-1])
    growth_mb = (max(r["after_put_kb"], r["after_open_kb"], r["after_reput_kb"]) - r["base_rss_kb"]) / 1024
    print(f"large object: {r} peak RSS growth {growth_mb:.1f} MiB")
    assert growth_mb < 32, r  # one 1 MiB chunk + client buffers, nowhere near the object size
    assert list(tmp_path.iterdir()) == []  # spool file removed


# =========================================================================== backup / restore
def _br() -> Any:
    sys.path.insert(0, str(ROOT / "scripts"))
    return importlib.import_module("backup_restore")


@pytest.mark.s3
def test_backup_objects_archive_roundtrip_between_buckets(
    admin: Any, make_bucket: Callable[[], str], tmp_path: Path
) -> None:
    br = _br()
    src_bucket, dst_bucket = make_bucket(), make_bucket()
    src_settings = s3_settings(reva_s3_bucket=src_bucket)
    src = S3ArtifactStore.from_settings(src_settings)
    src.spool_dir = str(tmp_path)
    objs = [
        src.put_bytes(os.urandom(n), ns, "application/octet-stream")
        for n, ns in ((10, "raw"), (2_000_000, "belief"), (1, "quarantine"))
    ]
    inv = br.object_inventory(src_settings)
    assert sorted(i["address"] for i in inv) == sorted(o.sha256 for o in objs)
    assert all(i["content_ok"] for i in inv)
    arch = tmp_path / "objects.tar"
    _, _, members = br.write_objects_archive(src, inv, "reva/", arch, None)
    assert members == 3
    dst_settings = s3_settings(reva_s3_bucket=dst_bucket)
    dst = S3ArtifactStore.from_settings(dst_settings)
    dst.spool_dir = str(tmp_path)
    res = br.restore_objects_archive(arch, None, dst, "reva/", {})
    assert res["objects"] == 3
    assert br.restore_objects_archive(arch, None, dst, "reva/", {})["objects"] == 3  # idempotent re-restore
    inv2 = br.object_inventory(dst_settings)
    assert [(i["key"], i["sha256"], i["bytes"]) for i in inv2] == [(i["key"], i["sha256"], i["bytes"]) for i in inv]
    # A corrupted source object fails the backup (inventory flags it; the archive refuses it).
    victim = objs[0]
    admin.put_object(Bucket=src_bucket, Key=object_key("reva/", "raw", victim.sha256), Body=b"corrupt")
    bad = [i for i in br.object_inventory(src_settings) if i["content_ok"] is False]
    assert [i["address"] for i in bad] == [victim.sha256]
    with pytest.raises((br.BackupError, IntegrityFailure)):
        br.write_objects_archive(src, br.object_inventory(src_settings), "reva/", tmp_path / "bad.tar", None)
    assert not (tmp_path / "bad.tar").exists()


@pytest.mark.s3
def test_backup_with_read_only_api_credential_cannot_see_quarantine(split: Split) -> None:
    """Documents the gap: a backup run with the API credential fails on quarantine objects."""
    br = _br()
    split.worker.put_bytes(os.urandom(32), "quarantine", "application/octet-stream")
    api_settings = s3_settings(
        reva_s3_bucket=split.bucket,
        reva_s3_access_key_id=os.environ["REVA_TEST_S3_API_KEY_ID"],
        reva_s3_secret_access_key=os.environ["REVA_TEST_S3_API_SECRET"],
    )
    with pytest.raises(ClientError) as ei:
        br.object_inventory(api_settings)
    assert code(ei.value) in DENIED


def _pg_tools_env(server_major: int) -> dict[str, str]:
    bindir = os.environ.get("REVA_PG_BINDIR")
    if bindir:
        return {"REVA_PG_BINDIR": bindir}
    container = os.environ.get("REVA_PG_CONTAINER", "reva-db")
    docker = shutil.which("docker")
    if docker:
        p = subprocess.run([docker, "inspect", "-f", "{{.State.Running}}", container], capture_output=True, text=True)
        if p.returncode == 0 and p.stdout.strip() == "true":
            return {"REVA_PG_CONTAINER": container}
    if not (shutil.which("pg_dump") and shutil.which("pg_restore")):
        pytest.skip("pg_dump/pg_restore not available (REVA_PG_BINDIR / REVA_PG_CONTAINER)")
    return {}


@pytest.mark.s3
@pytest.mark.db
def test_backup_restore_cli_round_trip_with_s3(
    db: Any, admin: Any, make_bucket: Callable[[], str], tmp_path: Path
) -> None:
    """scripts/backup.sh --include-objects and restore.sh --restore-objects with REVA_OBJECT_BACKEND=s3,
    into a fresh database and a fresh bucket (the session db is untouched)."""
    import asyncio

    from pydantic import SecretStr
    from sqlalchemy import create_engine, text

    from reva.adapters.connectors.registry import builtin_connectors
    from reva.adapters.postgres.engine import make_engine
    from reva.belief.publication import publish_release
    from reva.cli.bootstrap import bootstrap
    from reva.cli.seed import seed
    from reva.evidence.runner import FixtureHttp, run_connector
    from reva.kernel.context import Origin

    with db.owner.connect() as c:
        major = int(c.execute(text("SHOW server_version_num")).scalar_one()) // 10000
    tools_env = _pg_tools_env(major)
    src_bucket, dst_bucket = make_bucket(), make_bucket()
    name = f"reva_s3bk_{uuid.uuid4().hex[:10]}"
    target = f"{name}_r"
    admin_db = create_engine(db.settings.migration_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin_db.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
        c.execute(text(f'CREATE DATABASE "{target}"'))
    url = db.settings.migration_url().set(database=name).render_as_string(hide_password=False)
    s = db.settings.model_copy(
        update={
            "reva_migration_database_url": SecretStr(url),
            "reva_db_name": name,
            "reva_object_backend": "s3",
            "reva_s3_endpoint_url": ENDPOINT,
            "reva_s3_region": REGION,
            "reva_s3_bucket": src_bucket,
            "reva_s3_access_key_id": SecretStr(ADMIN_ID),
            "reva_s3_secret_access_key": SecretStr(ADMIN_SECRET),
        }
    )
    worker = None
    try:
        bootstrap(s)
        store = S3ArtifactStore.from_settings(s)
        worker = make_engine("worker", s)
        seed(worker, store)
        conn = builtin_connectors()["worldbank.wdi.indicators"]
        asyncio.run(run_connector(conn, FixtureHttp(conn.describe().name), store, worker, persist_cursor=False))
        publish_release(worker, store, cutoff=None, origin=Origin.test_fixture, environment="test")
        base_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path),
            "REVA_ENV": "test",
            "REVA_PYTHON": sys.executable,
            "REVA_OBJECT_BACKEND": "s3",
            "REVA_S3_ENDPOINT_URL": ENDPOINT,
            "REVA_S3_REGION": REGION,
            "REVA_S3_ACCESS_KEY_ID": ADMIN_ID,
            "REVA_S3_SECRET_ACCESS_KEY": ADMIN_SECRET,
            "REVA_S3_PREFIX": s.reva_s3_prefix,
            "REVA_DB_HOST": s.reva_db_host,
            "REVA_DB_PORT": str(s.reva_db_port),
            "REVA_API_DB_PASSWORD": s.reva_api_db_password.get_secret_value(),
            "REVA_WORKER_DB_PASSWORD": s.reva_worker_db_password.get_secret_value(),
            "REVA_SIM_DB_PASSWORD": s.reva_sim_db_password.get_secret_value(),
            "NO_PROXY": os.environ.get("NO_PROXY", "127.0.0.1,localhost"),
            **tools_env,
        }

        def run(script: str, args: list[str], env: dict[str, str]) -> dict[str, Any]:
            p = subprocess.run(
                ["bash", str(ROOT / "scripts" / script), *args],
                env=env,
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
            assert p.returncode == 0, p.stderr[-4000:]
            assert ADMIN_SECRET not in p.stdout + p.stderr
            return json.loads(p.stdout)  # type: ignore[no-any-return]

        out = run(
            "backup.sh",
            ["--out", str(tmp_path / "backups"), "--include-objects"],
            {**base_env, "REVA_MIGRATION_DATABASE_URL": url, "REVA_DB_NAME": name, "REVA_S3_BUCKET": src_bucket},
        )
        bdir = Path(out["path"])
        record = json.loads((bdir / "backup.json").read_text())
        assert record["objects_archive"]["members"] == record["objects"]["count"] > 0
        assert record["objects"]["artifacts_verified"] == record["objects"]["artifacts"] > 0
        for p in bdir.iterdir():
            assert ADMIN_SECRET.encode() not in p.read_bytes()
        res = run(
            "restore.sh",
            [str(bdir), "--restore-objects", "--verify-objects"],
            {
                **base_env,
                "REVA_RESTORE_DATABASE_URL": db.settings.migration_url()
                .set(database=target)
                .render_as_string(hide_password=False),
                "REVA_S3_BUCKET": dst_bucket,
            },
        )
        assert res["matches_backup"] and res["differences"] == []
        assert res["objects_restored"]["objects"] == record["objects_archive"]["members"]
        obj = res["objects"]
        assert obj["artifacts_verified"] == obj["artifacts"] == record["objects"]["artifacts"]
        assert obj["missing"] == obj["mismatched"] == obj["corrupt_objects"] == obj["unbacked_artifacts"] == []
        assert res["releases"]["releases"] == res["releases"]["verified"] >= 1
    finally:
        if worker is not None:
            worker.dispose()
        with admin_db.connect() as c:
            for n in (name, target):
                c.execute(text(f'DROP DATABASE IF EXISTS "{n}" WITH (FORCE)'))
        admin_db.dispose()
