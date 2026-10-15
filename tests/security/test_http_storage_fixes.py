"""Regression tests for review findings R-11, R-12, R-13, R-16, R-21, R-22, R-24, R-25.

Offline: DNS is a fake resolver, sockets are httpcore mock streams, S3 is a fake
client. The GC tests at the end use the per-session test database.
"""

from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import io
import json
import os
import time
import tracemalloc
import typing
import uuid
import zipfile
import zlib
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpcore
import httpx
import pytest

from reva.adapters.http.archive import read_zip_members
from reva.adapters.http.rate_limit import HostPolicy, RateLimiter
from reva.adapters.http.safe_client import (
    HttpResult,
    PinnedNetworkBackend,
    SafeHttpClient,
    redact_url,
    resolve_proxy_mode,
)
from reva.adapters.objects.common import (
    confirm_object_locked,
    lock_object_xact,
    object_key,
    object_lock_key,
)
from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.adapters.objects.s3 import S3ArtifactStore
from reva.kernel.errors import (
    ConfigurationError,
    IntegrityFailure,
    SourceResponseError,
    UnsafeRequest,
    ValidationProblem,
)
from reva.kernel.ids import CANONICAL_HASH_VERSION, REVA_NAMESPACE, canonical_bytes, external_id, logical_hash

HOST = "api.example.org"
PUBLIC_IP = "93.184.216.34"
REPO = Path(__file__).resolve().parents[2]


# =========================================================================== R-12 DNS pinning
class CapturingStream(httpcore.AsyncNetworkStream):
    """Serves one canned HTTP/1.1 response; records writes and the TLS server name."""

    def __init__(self, response: bytes, log: dict[str, Any]) -> None:
        self._buf = [response]
        self.log = log

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._buf.pop(0) if self._buf else b""

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.log.setdefault("written", b"")
        self.log["written"] += buffer

    async def aclose(self) -> None:
        return None

    async def start_tls(
        self, ssl_context: Any, server_hostname: str | None = None, timeout: float | None = None
    ) -> httpcore.AsyncNetworkStream:
        self.log["sni"] = server_hostname
        return self

    def get_extra_info(self, info: str) -> Any:
        return None


class CapturingBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, response: bytes = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok") -> None:
        self.hosts: list[str] = []
        self.log: dict[str, Any] = {}
        self.response = response

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.hosts.append(host)
        return CapturingStream(self.response, self.log)

    async def sleep(self, seconds: float) -> None:
        return None


def rebinding_resolver(first: Sequence[str], then: Sequence[str]) -> tuple[Any, list[str]]:
    calls: list[str] = []

    def resolve(host: str) -> Sequence[str]:
        calls.append(host)
        return first if len(calls) == 1 else then

    return resolve, calls


def pinned_client(resolver: Any, backend: httpcore.AsyncNetworkBackend, **kw: Any) -> SafeHttpClient:
    return SafeHttpClient(
        allowed_hosts=[HOST],
        resolver=resolver,
        network_backend=backend,
        proxy_mode="direct",
        rate_limiter=RateLimiter(default=HostPolicy(min_interval_seconds=0)),
        max_attempts=1,
        **kw,
    )


async def test_connection_targets_the_vetted_address_not_a_rebinding_answer() -> None:
    resolve, calls = rebinding_resolver([PUBLIC_IP], ["127.0.0.1"])
    backend = CapturingBackend()
    async with pinned_client(resolve, backend) as c:
        r = await c.get(f"https://{HOST}/data")
    assert r.status == 200 and r.body == b"ok"
    assert backend.hosts == [PUBLIC_IP]  # TCP connect went to the vetted IP
    assert calls == [HOST]  # resolved exactly once: httpx/httpcore never re-resolved
    assert backend.log["sni"] == HOST  # TLS SNI / certificate verification by hostname
    assert f"Host: {HOST}".encode() in backend.log["written"]


async def test_second_request_revets_and_refuses_rebinding() -> None:
    resolve, _ = rebinding_resolver([PUBLIC_IP], ["127.0.0.1"])
    backend = CapturingBackend()
    async with pinned_client(resolve, backend) as c:
        await c.get(f"https://{HOST}/a")
        with pytest.raises(UnsafeRequest, match="non-public"):
            await c.get(f"https://{HOST}/b")
    assert backend.hosts == [PUBLIC_IP]


async def test_backend_without_pin_resolves_and_vets_every_answer() -> None:
    inner = CapturingBackend()
    client = pinned_client(lambda h: [PUBLIC_IP, "10.0.0.7"], inner)
    backend = PinnedNetworkBackend(client._vetted_addresses, inner)
    with pytest.raises(UnsafeRequest, match="non-public"):
        await backend.connect_tcp(HOST, 443)
    with pytest.raises(UnsafeRequest, match="not allowlisted"):
        await backend.connect_tcp("evil.example.net", 443)
    with pytest.raises(UnsafeRequest):
        await backend.connect_unix_socket("/var/run/docker.sock")
    assert inner.hosts == []
    await client.aclose()


async def test_backend_tries_next_vetted_address_on_connect_failure() -> None:
    class Flaky(CapturingBackend):
        async def connect_tcp(self, host: str, port: int, **kw: Any) -> httpcore.AsyncNetworkStream:
            self.hosts.append(host)
            if host == PUBLIC_IP:
                raise httpcore.ConnectError("refused")
            return CapturingStream(self.response, self.log)

    inner = Flaky()
    async with pinned_client(lambda h: [PUBLIC_IP, "93.184.216.35"], inner) as c:
        r = await c.get(f"https://{HOST}/x")
    assert r.status == 200 and inner.hosts == [PUBLIC_IP, "93.184.216.35"]


def test_proxy_mode_is_explicit() -> None:
    assert resolve_proxy_mode(None, {"REVA_ENV": "production"}) == "direct"
    assert resolve_proxy_mode(None, {"REVA_ENV": "local"}) == "env"
    assert resolve_proxy_mode(None, {"REVA_ENV": "production", "REVA_HTTP_PROXY_MODE": "env"}) == "env"
    assert resolve_proxy_mode("direct", {"REVA_HTTP_PROXY_MODE": "env"}) == "direct"
    with pytest.raises(ConfigurationError):
        resolve_proxy_mode("socks", {})


async def test_env_proxy_used_only_in_env_mode_and_never_implicitly() -> None:
    env = {"HTTPS_PROXY": "http://proxy.internal:3128", "NO_PROXY": "internal.example.org", "REVA_ENV": "local"}
    direct = SafeHttpClient(allowed_hosts=[HOST], proxy_mode="direct", environ=env)
    proxied = SafeHttpClient(allowed_hosts=[HOST, "internal.example.org"], proxy_mode="env", environ=env)
    try:
        assert direct.egress_for(HOST) == "direct" and direct.proxy_url is None
        assert proxied.egress_for(HOST) == "proxy" and proxied.proxy_url == env["HTTPS_PROXY"]
        assert proxied.egress_for("internal.example.org") == "direct"  # NO_PROXY honoured
        assert direct._client.trust_env is False and proxied._client.trust_env is False
    finally:
        await direct.aclose()
        await proxied.aclose()


# =========================================================================== R-13 decompression
def _gzip_stream(chunks: typing.Iterable[bytes]) -> bytes:
    c = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    out = [c.compress(ch) for ch in chunks]
    out.append(c.flush())
    return b"".join(out)


def _zeros(total: int, chunk: int = 1 << 20) -> typing.Iterator[bytes]:
    block = b"\0" * chunk
    for _ in range(total // chunk):
        yield block


@pytest.fixture(scope="module")
def nested_bomb() -> bytes:
    inner = _gzip_stream(_zeros(300 << 20))  # 300 MiB of zeros, built without a big allocation
    return gzip.compress(inner, compresslevel=9)


def mock_client(handler: Any, **kw: Any) -> SafeHttpClient:
    return SafeHttpClient(
        allowed_hosts=[HOST],
        transport=httpx.MockTransport(handler),
        resolver=lambda h: [PUBLIC_IP],
        rate_limiter=RateLimiter(default=HostPolicy(min_interval_seconds=0)),
        max_attempts=1,
        **kw,
    )


def _streamed(body: bytes, encoding: str | None, chunk: int = 65536) -> Any:
    headers = {"content-type": "application/json"}
    if encoding is not None:
        headers["content-encoding"] = encoding

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers=headers, stream=httpx.ByteStream(body) if len(body) <= chunk else Chunks(body, chunk)
        )

    return handler


class Chunks(httpx.AsyncByteStream):
    def __init__(self, body: bytes, size: int) -> None:
        self.body, self.size = body, size

    async def __aiter__(self) -> typing.AsyncIterator[bytes]:
        for i in range(0, len(self.body), self.size):
            yield self.body[i : i + self.size]


async def test_stacked_gzip_bomb_fails_fast_with_small_peak_memory(nested_bomb: bytes) -> None:
    assert len(nested_bomb) < 2000  # the reviewer's 661-byte class of payload
    tracemalloc.start()
    try:
        async with mock_client(_streamed(nested_bomb, "gzip, gzip"), max_bytes=5_000_000) as c:
            started = time.monotonic()
            with pytest.raises(UnsafeRequest, match="stacked content-encoding"):
                await c.get(f"https://{HOST}/bomb")
            elapsed = time.monotonic() - started
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    print(f"wire={len(nested_bomb)}B peak={peak / 1e6:.2f}MB elapsed={elapsed * 1000:.1f}ms")
    assert peak < 20_000_000
    assert elapsed < 1.0


async def test_single_gzip_bomb_never_decodes_past_limit_plus_one(nested_bomb: bytes) -> None:
    inner = gzip.decompress(nested_bomb)  # ~300 KB on the wire, 300 MiB decoded
    tracemalloc.start()
    try:
        async with mock_client(_streamed(inner, "gzip", chunk=4096), max_bytes=5_000_000) as c:
            with pytest.raises(UnsafeRequest):
                await c.get(f"https://{HOST}/bomb")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    print(f"single gzip wire={len(inner)}B peak={peak / 1e6:.2f}MB")
    assert peak < 20_000_000


@pytest.mark.parametrize("encoding", ["gzip, gzip", "gzip,deflate", "br", "zstd", "compress", "gzip, identity"])
async def test_stacked_or_unknown_codings_rejected(encoding: str) -> None:
    async with mock_client(_streamed(gzip.compress(b"{}"), encoding)) as c:
        with pytest.raises(UnsafeRequest):
            await c.get(f"https://{HOST}/x")


@pytest.mark.parametrize("encoding", [None, "identity", "gzip", "x-gzip", "deflate", "deflate-raw"])
async def test_supported_codings_decoded(encoding: str | None) -> None:
    payload = json.dumps({"rows": list(range(5000))}).encode()
    if encoding in ("gzip", "x-gzip"):
        body = gzip.compress(payload)
    elif encoding == "deflate":
        body = zlib.compress(payload)
    elif encoding == "deflate-raw":  # non-conforming servers send raw deflate as "deflate"
        c = zlib.compressobj(6, zlib.DEFLATED, -zlib.MAX_WBITS)
        body, encoding = c.compress(payload) + c.flush(), "deflate"
    else:
        body = payload
    async with mock_client(_streamed(body, encoding, chunk=1000)) as c:
        r = await c.get(f"https://{HOST}/x")
    assert r.body == payload


async def test_multi_member_gzip_and_truncation() -> None:
    payload = b"a" * 1000 + b"b" * 1000
    multi = gzip.compress(payload[:1000]) + gzip.compress(payload[1000:])
    async with mock_client(_streamed(multi, "gzip", chunk=7)) as c:
        assert (await c.get(f"https://{HOST}/x")).body == payload
    truncated = gzip.compress(payload)[:-12]
    async with mock_client(_streamed(truncated, "gzip")) as c:
        with pytest.raises(SourceResponseError, match="truncated"):
            await c.get(f"https://{HOST}/x")
    async with mock_client(_streamed(b"not gzip at all", "gzip")) as c:
        with pytest.raises(SourceResponseError, match="corrupt"):
            await c.get(f"https://{HOST}/x")


def test_response_metadata_redacts_secret_query_values() -> None:
    url = f"https://{HOST}/get?reporterCode=251&subscription-key=SEKRET"
    res = HttpResult(url, url, 200, {}, b"{}", datetime.now(UTC), 0.1, (url,), 1)
    meta = json.dumps(res.response_metadata())
    assert "SEKRET" not in meta and "reporterCode=251" in meta
    plain = f"https://{HOST}/get?a=1%2C2&b=x"
    assert redact_url(plain) == plain  # untouched (no re-encoding) without secrets


# =========================================================================== R-24 zip ambiguity
def _zip(entries: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries:
            with (
                pytest.warns(UserWarning)
                if any(n == name for n, _ in entries[: entries.index((name, data))])
                else _nullcontext()
            ):
                z.writestr(name, data)
    return buf.getvalue()


class _nullcontext:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *a: object) -> None:
        return None


@pytest.mark.parametrize(
    "entries",
    [
        [("ne/x.shp", b"GOOD"), ("ne/x.shp", b"DUPLICATE")],  # exact duplicate
        [("good/ne.shp", b"GOOD"), ("evil/ne.shp", b"EVIL")],  # basename collision
        [("NE/X.SHP", b"A"), ("ne/x.shp", b"B")],  # case-insensitive collision
        [("ne/./x.shp", b"A"), ("ne/x.shp", b"B")],  # duplicate after normalisation
        [("a/readme.txt", b"A"), ("A/README.TXT", b"B"), ("ne/x.shp", b"S")],  # unwanted members too
        [("a/x.shp", b"A"), ("b/X.shp", b"B")],  # case-folded basename collision
    ],
)
def test_ambiguous_zip_members_rejected(entries: list[tuple[str, bytes]]) -> None:
    with pytest.raises(UnsafeRequest, match="ambiguous"):
        read_zip_members(_zip(entries), (".shp",))


def test_unambiguous_zip_still_flattened() -> None:
    out = read_zip_members(_zip([("ne/x.shp", b"S"), ("ne/x.dbf", b"D"), ("ne/X.txt", b"T")]), (".shp", ".dbf"))
    assert out == {"x.shp": b"S", "x.dbf": b"D"}


@pytest.mark.parametrize("name", ["C:/x.shp", "../x.shp", "/abs/x.shp", "a/../../x.shp"])
def test_unsafe_member_paths_rejected(name: str) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo(name), b"x")
    with pytest.raises(UnsafeRequest, match="unsafe archive member"):
        read_zip_members(buf.getvalue(), (".shp",))


# =========================================================================== R-22 canonical hashing
def test_canonical_version_bumped() -> None:
    assert CANONICAL_HASH_VERSION == "reva-cjson-2"


def test_same_instant_in_any_offset_hashes_identically() -> None:
    a = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    for hours in (-11, -5, 1, 5.5, 14):
        b = a.astimezone(timezone(timedelta(hours=hours)))
        assert logical_hash({"t": b}) == logical_hash({"t": a})
    assert canonical_bytes(a) == b'{"$datetime":"2024-01-01T00:00:00+00:00"}'
    assert logical_hash(a) != logical_hash(a + timedelta(microseconds=1))
    with pytest.raises(ValueError):
        canonical_bytes(datetime(2024, 1, 1))


@pytest.mark.parametrize(
    "spoof,real",
    [
        ({"$decimal": "1"}, Decimal("1")),
        ({"$float": "1.5"}, 1.5),
        ({"$datetime": "2024-01-01T00:00:00+00:00"}, datetime(2024, 1, 1, tzinfo=UTC)),
        ({"$date": "2024-01-01"}, date(2024, 1, 1)),
        ({"x": {"$decimal": "2"}}, {"x": Decimal("2")}),
        ([{"$decimal": "3"}], [Decimal("3")]),
    ],
)
def test_type_tags_cannot_be_spoofed_by_user_keys(spoof: Any, real: Any) -> None:
    assert logical_hash(spoof) != logical_hash(real)
    assert canonical_bytes(spoof) != canonical_bytes(real)


def test_dollar_key_escaping_is_injective() -> None:
    assert logical_hash({"$decimal": "1"}) != logical_hash({"$$decimal": "1"})
    assert logical_hash({"$a": 1, "$$a": 2}) != logical_hash({"$$a": 1, "$a": 2})
    assert logical_hash({"plain": 1}) == logical_hash({"plain": 1})


def test_external_id_unchanged_for_ordinary_keys_and_injective() -> None:
    legacy = uuid.uuid5(REVA_NAMESPACE, "worldbank\x1fFRA\x1f1")
    assert external_id("worldbank", "FRA") == legacy  # existing deterministic ids are stable
    assert external_id("a\x1fb", "c") != external_id("a", "b\x1fc")
    assert external_id("a\\u001fb", "c") != external_id("a\x1fb", "c")


# =========================================================================== R-21 knowledge cutoff
def _example() -> dict[str, Any]:
    ex = json.loads((REPO / "contracts" / "scenario.example.json").read_text())
    ex["baseline_release_id"] = "11111111-1111-4111-8111-111111111111"
    ex["knowledge_cutoff"] = "2024-06-30T00:00:00Z"
    return ex


@pytest.mark.parametrize(
    "value",
    [
        "infinity",
        "-infinity",
        "yesterday",
        "now",
        "2026-99-99",
        "not a date",
        "2024-02-30T00:00:00Z",
        "2024-01-01T00:00:00",
        "2024-01-01",
        "2024-01-01 00:00:00Z",
        "2024-01-01T24:00:00Z",
        "２０２４-01-01T00:00:00Z",
        "2024-01-01T00:00:00+25:00",
        "2999-01-01T00:00:00Z",
    ],
)
def test_invalid_or_future_knowledge_cutoff_rejected(value: str) -> None:
    from reva.simulation.spec import validate_scenario_spec

    ex = _example()
    ex["knowledge_cutoff"] = value
    with pytest.raises(ValidationProblem):
        validate_scenario_spec(ex)


def test_valid_knowledge_cutoffs_accepted_and_skew_bounded() -> None:
    from reva.simulation.spec import parse_knowledge_cutoff, validate_scenario_spec

    for value in ("2024-06-30T00:00:00Z", "2024-06-30T02:00:00+02:00", "2024-06-30T00:00:00.123456z"):
        ex = _example()
        ex["knowledge_cutoff"] = value
        validate_scenario_spec(ex)
    now = datetime(2025, 1, 1, tzinfo=UTC)
    assert parse_knowledge_cutoff("2025-01-01T00:00:59Z", now=now) == now + timedelta(seconds=59)
    with pytest.raises(ValidationProblem):
        parse_knowledge_cutoff("2025-01-01T00:01:01Z", now=now)
    with pytest.raises(ValidationProblem):
        parse_knowledge_cutoff(1704067200, now=now)


# =========================================================================== R-16 / R-11 stores
class _CE(Exception):
    def __init__(self, code: str) -> None:
        self.response = {"Error": {"Code": code}}


class FakeS3:
    class exceptions:
        ClientError = _CE

    def __init__(self, server_checksums: bool = False) -> None:
        self.objs: dict[str, tuple[bytes, dict[str, str]]] = {}
        self.server_checksums = server_checksums
        self.gets: list[str] = []
        self.read_sizes: list[int] = []

    def head_object(self, Bucket: str, Key: str, **kw: Any) -> dict[str, Any]:
        if Key not in self.objs:
            raise _CE("404")
        data, md = self.objs[Key]
        r: dict[str, Any] = {
            "ContentLength": len(data),
            "Metadata": md,
            "LastModified": datetime(2020, 1, 1, tzinfo=UTC),
        }
        if kw.get("ChecksumMode") == "ENABLED" and self.server_checksums:
            r["ChecksumSHA256"] = base64.b64encode(hashlib.sha256(data).digest()).decode()
        return r

    def put_object(self, **kw: Any) -> None:
        if kw["Key"] in self.objs:
            raise _CE("PreconditionFailed")
        self.objs[kw["Key"]] = (kw["Body"].read(), dict(kw["Metadata"]))

    def get_object(self, Bucket: str, Key: str, **kw: Any) -> dict[str, Any]:
        self.gets.append(Key)
        sizes = self.read_sizes

        class Body(io.BytesIO):
            def read(self, n: int | None = -1) -> bytes:  # type: ignore[override]
                out = super().read(n)
                sizes.append(len(out))
                return out

        return {"Body": Body(self.objs[Key][0])}


def test_s3_existing_object_with_matching_metadata_but_wrong_bytes_rejected() -> None:
    good = b"A" * 10
    sha = hashlib.sha256(good).hexdigest()
    f = FakeS3()
    f.objs[object_key("reva/", "raw", sha)] = (b"B" * 10, {"reva-sha256": sha})
    with pytest.raises(IntegrityFailure):
        S3ArtifactStore(f, "b").put_bytes(good, "raw", "application/octet-stream")


def test_s3_existing_object_reverified_by_streaming_bounded_reads() -> None:
    good = os.urandom(3 * (1 << 20) + 17)
    sha = hashlib.sha256(good).hexdigest()
    f = FakeS3(server_checksums=True)  # server checksum is *not* trusted for pre-existing objects
    key = object_key("reva/", "raw", sha)
    f.objs[key] = (good, {"reva-sha256": sha})
    obj = S3ArtifactStore(f, "b").put_bytes(good, "raw", "application/octet-stream")
    assert obj.sha256 == sha and f.gets == [key]
    assert max(f.read_sizes) <= 1 << 20  # one chunk in memory at a time


def test_s3_existing_object_longer_than_expected_stops_reading_early() -> None:
    good = b"G" * 100
    sha = hashlib.sha256(good).hexdigest()
    f = FakeS3()
    f.objs[object_key("reva/", "raw", sha)] = (b"G" * 100 + b"X" * (5 << 20), {"reva-sha256": sha})
    f.head_object = lambda Bucket, Key, **kw: {"ContentLength": 100, "Metadata": {"reva-sha256": sha}}  # type: ignore[method-assign]
    with pytest.raises(IntegrityFailure):
        S3ArtifactStore(f, "b").put_bytes(good, "raw", "application/octet-stream")
    assert sum(f.read_sizes) <= 2 << 20


def test_s3_fresh_upload_uses_server_checksum_or_streams() -> None:
    data = b"fresh bytes"
    with_cs = FakeS3(server_checksums=True)
    S3ArtifactStore(with_cs, "b").put_bytes(data, "raw", "text/plain")
    assert with_cs.gets == []  # provider-computed full-object SHA-256 matched
    without = FakeS3(server_checksums=False)
    S3ArtifactStore(without, "b").put_bytes(data, "raw", "text/plain")
    assert len(without.gets) == 1  # no server checksum: stream-hashed


def test_s3_uri_mtime() -> None:
    f = FakeS3()
    st = S3ArtifactStore(f, "b")
    obj = st.put_bytes(b"x", "raw", "text/plain")
    assert st.uri_mtime(obj.storage_uri) == datetime(2020, 1, 1, tzinfo=UTC).timestamp()
    assert st.uri_mtime(obj.storage_uri.replace(obj.sha256, "0" * 64)) is None
    with pytest.raises(ValueError):
        st.uri_mtime("s3://other/reva/raw/x")


def test_filesystem_reuse_refreshes_age_and_reverifies_bytes(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / "objects")
    obj = store.put_bytes(b"reused", "normalised", "application/json")
    path = Path(obj.storage_uri.removeprefix("file://"))
    old = time.time() - 10 * 86400
    os.utime(path, (old, old))
    store.put_bytes(b"reused", "normalised", "application/json")
    assert store.uri_mtime(obj.storage_uri) > time.time() - 60  # GC sees a young object
    os.chmod(path, 0o644)
    path.write_bytes(b"rotted")  # same address, different bytes
    with pytest.raises(IntegrityFailure):
        store.put_bytes(b"reused", "normalised", "application/json")


def test_object_lock_key_shape() -> None:
    sha = hashlib.sha256(b"x").hexdigest()
    assert object_lock_key(sha) == f"obj|{sha}"
    with pytest.raises(ValueError):
        object_lock_key("abc")


# =========================================================================== R-25 settings
def _prod(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "reva_env": "production",
        "reva_object_backend": "s3",
        "reva_s3_bucket": "b",
        "reva_s3_access_key_id": "AKIAEXAMPLE",
        "reva_s3_secret_access_key": "s3-secret-value-xyz",
        "reva_session_secret": "s" * 48,
        "reva_public_base_url": "https://reva.example.org",
        "reva_db_sslmode": "verify-full",
    }
    base.update(over)
    return base


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for k in list(os.environ):
        if k.startswith("REVA_") or k == "DATABASE_URL":
            monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)


ARGON = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$aGFzaGhhc2hoYXNo"


def _settings(**kw: Any) -> Any:
    from reva.settings import Settings

    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


@pytest.mark.usefixtures("clean_env")
@pytest.mark.parametrize("mode", [None, "disable", "allow", "prefer"])
def test_production_requires_tls_to_postgres(mode: str | None) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="REVA_DB_SSLMODE"):
        _settings(**_prod(reva_db_sslmode=mode))


@pytest.mark.usefixtures("clean_env")
def test_production_rejects_wildcard_cors_and_non_argon_admin_hash() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="CORS"):
        _settings(**_prod(reva_cors_origins="https://a.example.org,*"))
    with pytest.raises(ValidationError, match="Argon2id"):
        _settings(**_prod(reva_admin_password_hash="$2b$12$bcrypt-not-argon"))
    with pytest.raises(ValidationError, match="Argon2id"):
        _settings(**_prod(reva_admin_password_hash="$argon2i$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"))


@pytest.mark.usefixtures("clean_env")
def test_required_for_each_process_role() -> None:
    s = _settings(**_prod())
    assert set(s.required_for("api")) == {
        "REVA_API_DB_PASSWORD",
        "REVA_ADMIN_PASSWORD_HASH (Argon2id; generate with `reva auth hash-password`)",
        "REVA_ALLOWED_HOSTS (public hostname(s), not only localhost)",
    }
    assert s.required_for("worker") == ["REVA_WORKER_DB_PASSWORD", "REVA_SIM_DB_PASSWORD"]
    assert s.required_for("sim") == ["REVA_SIM_DB_PASSWORD"]
    assert "REVA_MIGRATION_DATABASE_URL" in s.required_for("bootstrap")
    ready = _settings(
        **_prod(
            reva_api_db_password="p",
            reva_worker_db_password="w",
            reva_sim_db_password="s",
            reva_admin_password_hash=ARGON,
            reva_allowed_hosts="reva.example.org",
        )
    )
    for role in ("api", "worker", "sim"):
        assert ready.required_for(role) == []  # type: ignore[arg-type]
        assert ready.validate_for(role) is ready  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError, match="REVA_MIGRATION_DATABASE_URL"):
        ready.validate_for("bootstrap")
    local = _settings()
    assert local.required_for("api") == ["REVA_API_DB_PASSWORD"]  # only credentials outside production


@pytest.mark.usefixtures("clean_env")
def test_process_role_enforced_by_validator() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="REVA_WORKER_DB_PASSWORD"):
        _settings(**_prod(reva_process_role="worker"))
    # The worker never receives the session secret; its role does not need it.
    w = _settings(
        **_prod(
            reva_process_role="worker", reva_session_secret=None, reva_worker_db_password="w", reva_sim_db_password="s"
        )
    )
    assert w.reva_process_role == "worker"
    with pytest.raises(ValidationError, match="must not be present"):
        _settings(
            **_prod(
                reva_process_role="worker",
                reva_worker_db_password="w",
                reva_sim_db_password="s",
                reva_migration_database_url="postgresql://owner:pw@db/reva",
            )
        )
    with pytest.raises(ValidationError, match="REVA_SESSION_SECRET"):
        _settings(**_prod(reva_process_role="api", reva_session_secret=None))


@pytest.mark.usefixtures("clean_env")
def test_production_validation_error_text_hides_inputs() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as ei:
        _settings(**_prod(reva_db_sslmode=None, reva_session_secret="short-secret-zz"))
    assert "short-secret-zz" not in str(ei.value)
    assert "s3-secret-value-xyz" not in str(ei.value)


def test_redaction_covers_common_secret_names_and_url_credentials() -> None:
    from reva.settings import redact_env

    env = {
        "PGPASSWORD": "pg-pass-1",
        "AWS_SECRET_ACCESS_KEY": "aws-secret-2",
        "AWS_SESSION_TOKEN": "aws-token-3",
        "GITHUB_TOKEN": "gh-4",
        "SOME_SERVICE_API_KEY": "key-5",
        "MY_PRIVATE_KEY": "pk-6",
        "OTHER_DSN": "postgresql://u:dsn-pw-7@h/db",
        "HTTPS_PROXY": "http://user:proxy-pw-8@proxy.internal:3128",
        "UNRELATED_URL": "https://alice:url-pw-9@example.org/x",
        "REVA_ENV": "production",
        "PATH": "/usr/bin",
    }
    red = redact_env(env)
    dumped = json.dumps(red)
    for secret in (
        "pg-pass-1",
        "aws-secret-2",
        "aws-token-3",
        "gh-4",
        "key-5",
        "pk-6",
        "dsn-pw-7",
        "proxy-pw-8",
        "url-pw-9",
    ):
        assert secret not in dumped
    assert red["HTTPS_PROXY"] == "http://user:***@proxy.internal:3128"
    assert red["REVA_ENV"] == "production" and red["PATH"] == "/usr/bin"


# =========================================================================== R-11 GC vs reuse (database)
DAY = 86400


def _age(store: FilesystemArtifactStore, uri: str, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(Path(uri.removeprefix("file://")), (t, t))


@pytest.mark.db
def test_gc_does_not_delete_object_reused_between_scans(db: Any, tmp_path: Path) -> None:
    from reva.operations.gc import gc_scan

    store = FilesystemArtifactStore(tmp_path / "gc-objects")
    data = json.dumps({"gc-race": uuid.uuid4().hex}).encode()
    o1 = store.put_bytes(data, "normalised", "application/json")  # publication crashed: orphan
    _age(store, o1.storage_uri, 10 * DAY)
    gc_scan(db.worker, store, namespaces=("normalised",), delete=False)  # scan 1
    store.put_bytes(data, "normalised", "application/json")  # a new publication reuses the bytes
    rep = gc_scan(db.worker, store, namespaces=("normalised",))  # scan 2
    assert o1.storage_uri not in rep.deleted
    assert store.get("normalised", o1.sha256) == data


@pytest.mark.db
def test_gc_skips_object_whose_lock_a_publisher_holds(db: Any, tmp_path: Path) -> None:
    from reva.operations.gc import gc_scan

    store = FilesystemArtifactStore(tmp_path / "gc-objects")
    data = json.dumps({"gc-lock": uuid.uuid4().hex}).encode()
    obj = store.put_bytes(data, "normalised", "application/json")
    _age(store, obj.storage_uri, 10 * DAY)
    gc_scan(db.worker, store, namespaces=("normalised",), delete=False)
    _age(store, obj.storage_uri, 10 * DAY)
    # The publisher's registering transaction holds the object lock (advisory locks are per database,
    # so any role sees them; the owner engine avoids the shared reva_worker connection limit).
    with db.owner.begin() as publisher:
        lock_object_xact(publisher, obj.sha256)
        rep = gc_scan(db.worker, store, namespaces=("normalised",))
        assert rep.skipped_locked == 1 and rep.deleted == []
        confirm_object_locked(publisher, store, "normalised", obj.sha256, obj.byte_size)
    assert store.get("normalised", obj.sha256) == data


@pytest.mark.db
def test_gc_deletes_true_orphan_and_publisher_confirmation_detects_it(db: Any, tmp_path: Path) -> None:
    from reva.operations.gc import gc_scan

    store = FilesystemArtifactStore(tmp_path / "gc-objects")
    data = json.dumps({"gc-orphan": uuid.uuid4().hex}).encode()
    obj = store.put_bytes(data, "normalised", "application/json")
    _age(store, obj.storage_uri, 10 * DAY)
    gc_scan(db.worker, store, namespaces=("normalised",), delete=False)
    rep = gc_scan(db.worker, store, namespaces=("normalised",))
    assert rep.deleted == [obj.storage_uri]
    with db.owner.begin() as publisher, pytest.raises(IntegrityFailure, match="vanished"):
        confirm_object_locked(publisher, store, "normalised", obj.sha256)


def _unused() -> None:  # keep imports referenced for linters
    _ = binascii
