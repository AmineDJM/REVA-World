"""S3 ArtifactStore against moto: verified, conditional, ETag-independent storage."""

from __future__ import annotations

import hashlib
import io
from collections.abc import Iterator
from typing import Any

import pytest

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")

from moto import mock_aws  # noqa: E402

from reva.adapters.objects.common import object_key  # noqa: E402
from reva.adapters.objects.s3 import S3ArtifactStore  # noqa: E402
from reva.kernel.errors import IntegrityFailure, UnsafeRequest  # noqa: E402

BUCKET = "reva-h2-test-bucket"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@pytest.fixture()
def s3(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    # Dummy credentials only; moto intercepts every call (no network).
    for k, v in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


@pytest.fixture()
def store(s3: Any, tmp_path) -> S3ArtifactStore:  # type: ignore[no-untyped-def]
    return S3ArtifactStore(s3, BUCKET, "reva/", spool_dir=str(tmp_path))


def keys(client: Any) -> list[str]:
    return [o["Key"] for o in client.list_objects_v2(Bucket=BUCKET).get("Contents", [])]


class Stream(io.RawIOBase):
    def __init__(self, total: int, fail_after: int | None = None) -> None:
        self.total, self.sent, self.fail_after = total, 0, fail_after

    def readable(self) -> bool:
        return True

    def read(self, n: int = -1) -> bytes:
        if self.fail_after is not None and self.sent >= self.fail_after:
            raise ConnectionResetError("injected mid-upload failure")
        n = self.total - self.sent if n < 0 else min(n, self.total - self.sent)
        self.sent += n
        return b"\x01" * n


def test_put_get_head_range_read(store: S3ArtifactStore, s3: Any) -> None:
    data = b"comtrade raw bytes " * 100
    obj = store.put_bytes(data, "raw", "application/json")
    key = object_key("reva/", "raw", sha(data))
    assert obj.storage_uri == f"s3://{BUCKET}/{key}"
    assert keys(s3) == [key]
    assert store.get("raw", obj.sha256) == data
    assert store.range_read("raw", obj.sha256, 3, 5) == data[3:8]
    assert b"".join(store.open("raw", obj.sha256)) == data
    head = store.head("raw", obj.sha256)
    assert head is not None and head.sha256 == sha(data) and head.byte_size == len(data)
    # Re-put of identical bytes is idempotent.
    assert store.put_bytes(data, "raw", "application/json").storage_uri == obj.storage_uri
    assert keys(s3) == [key]
    assert store.head("raw", "0" * 64) is None


def test_sha_comes_from_verified_metadata_never_from_etag(store: S3ArtifactStore, s3: Any) -> None:
    data = b"bytes uploaded out of band"
    key = object_key("reva/", "raw", sha(data))
    s3.put_object(Bucket=BUCKET, Key=key, Body=data)  # no reva-sha256 metadata
    etag = s3.head_object(Bucket=BUCKET, Key=key)["ETag"].strip('"')
    assert etag == hashlib.md5(data).hexdigest()
    head = store.head("raw", sha(data))
    assert head is not None
    assert head.sha256 != etag and head.sha256 == ""  # ETag is never promoted to a SHA-256
    # Without the verified hash the store refuses to adopt the object.
    with pytest.raises(IntegrityFailure):
        store.put_bytes(data, "raw", "application/octet-stream")


def test_metadata_sha_mismatch_detected(store: S3ArtifactStore, s3: Any) -> None:
    data = b"genuine"
    key = object_key("reva/", "raw", sha(data))
    s3.put_object(Bucket=BUCKET, Key=key, Body=data, Metadata={"reva-sha256": "f" * 64})
    with pytest.raises(IntegrityFailure):
        store.put_bytes(data, "raw", "application/octet-stream")


def test_tampered_bytes_detected_on_get(store: S3ArtifactStore, s3: Any) -> None:
    data = b"original"
    obj = store.put_bytes(data, "raw", "text/plain")
    key = object_key("reva/", "raw", obj.sha256)
    # An attacker/bug rewrites bytes but copies the metadata.
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"modified", Metadata={"reva-sha256": obj.sha256})
    with pytest.raises(IntegrityFailure):
        store.get("raw", obj.sha256)


def test_conditional_write_never_overwrites_on_race(store: S3ArtifactStore, s3: Any) -> None:
    """Another writer lands different bytes between our HEAD and PUT."""
    data = b"our bytes"
    key = object_key("reva/", "raw", sha(data))
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"their bytes", Metadata={"reva-sha256": "e" * 64})
    real_head = store._head_key
    calls = {"n": 0}

    def stale_head(k: str):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return None if calls["n"] == 1 else real_head(k)

    store._head_key = stale_head  # type: ignore[method-assign]
    with pytest.raises(IntegrityFailure):
        store.put_bytes(data, "raw", "text/plain")
    body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    assert body == b"their bytes"  # If-None-Match prevented the overwrite


def test_max_bytes_exceeded_uploads_nothing(store: S3ArtifactStore, s3: Any, tmp_path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(UnsafeRequest):
        store.put(Stream(3 * 1024 * 1024), "raw", "application/octet-stream", max_bytes=1024 * 1024)
    assert keys(s3) == []
    assert list(tmp_path.iterdir()) == []  # spool file removed


def test_mid_stream_failure_uploads_nothing(store: S3ArtifactStore, s3: Any, tmp_path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ConnectionResetError):
        store.put(
            Stream(4 * 1024 * 1024, fail_after=2 * 1024 * 1024),
            "raw",
            "application/octet-stream",
            max_bytes=8 * 1024 * 1024,
        )
    assert keys(s3) == []
    assert list(tmp_path.iterdir()) == []


def test_upload_failure_leaves_no_object(store: S3ArtifactStore, s3: Any, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def boom(**_: Any) -> None:
        raise ConnectionError("network dropped during PutObject")

    monkeypatch.setattr(s3, "put_object", boom)
    with pytest.raises(ConnectionError):
        store.put_bytes(b"never stored", "raw", "text/plain")
    assert keys(s3) == []


def test_delete_orphan_confined_to_prefix(store: S3ArtifactStore, s3: Any) -> None:
    s3.put_object(Bucket=BUCKET, Key="other/thing", Body=b"x")
    with pytest.raises(ValueError):
        store.delete_orphan(f"s3://{BUCKET}/other/thing")
    with pytest.raises(ValueError):
        store.delete_orphan("s3://another-bucket/reva/raw/x")
    obj = store.put_bytes(b"orphan", "quarantine", "text/plain")
    store.delete_orphan(obj.storage_uri)
    assert store.head("quarantine", obj.sha256) is None
    assert "other/thing" in keys(s3)


def test_list_uris_scoped_to_namespace(store: S3ArtifactStore) -> None:
    a = store.put_bytes(b"a", "raw", "text/plain")
    store.put_bytes(b"b", "belief", "text/plain")
    assert [u for u, _ in store.list_uris("raw")] == [a.storage_uri]


def test_factory_builds_s3_store_for_production_settings(s3: Any) -> None:
    from reva.adapters.objects.factory import build_store
    from reva.settings import Settings

    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        reva_env="production",
        reva_object_backend="s3",
        reva_s3_bucket=BUCKET,
        reva_s3_region="us-east-1",
        reva_s3_access_key_id="testing",
        reva_s3_secret_access_key="testing",
        reva_session_secret="s" * 40,
        reva_public_base_url="https://reva.example.org",
    )
    st = build_store(s)
    assert isinstance(st, S3ArtifactStore)
    obj = st.put_bytes(b"via factory", "raw", "text/plain")
    assert st.get("raw", obj.sha256) == b"via factory"
