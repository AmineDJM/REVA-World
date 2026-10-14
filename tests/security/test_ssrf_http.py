"""SafeHttpClient and safe archive extraction against hostile servers (offline).

The transport is an ``httpx.MockTransport`` and DNS is a fake resolver, so every
test is deterministic and nothing leaves the machine.
"""

from __future__ import annotations

import gzip
import io
import struct
import zipfile
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from reva.adapters.http.archive import read_zip_members
from reva.adapters.http.rate_limit import HostPolicy, RateLimiter
from reva.adapters.http.safe_client import (
    HttpResult,
    SafeHttpClient,
    address_is_public,
    request_fingerprint,
    require_json,
)
from reva.kernel.errors import SourceResponseError, UnsafeRequest

ADV = Path(__file__).resolve().parents[1] / "fixtures" / "adversarial"
HOST = "api.example.org"
OTHER = "cdn.example.org"
PUBLIC = {HOST: ["93.184.216.34"], OTHER: ["93.184.216.35"]}


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)


def resolver(table: dict[str, Sequence[str]]) -> Callable[[str], Sequence[str]]:
    def resolve(host: str) -> Sequence[str]:
        if host not in table:
            raise UnsafeRequest(f"DNS resolution failed for {host}")
        return table[host]

    return resolve


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    rec: Recorder,
    *,
    dns: dict[str, Sequence[str]] | None = None,
    allowed: Sequence[str] = (HOST, OTHER),
    **kw: object,
) -> SafeHttpClient:
    def wrapped(request: httpx.Request) -> httpx.Response:
        rec.requests.append(request)
        return handler(request)

    return SafeHttpClient(
        allowed_hosts=allowed,
        transport=httpx.MockTransport(wrapped),
        resolver=resolver(dns if dns is not None else PUBLIC),
        rate_limiter=RateLimiter(default=HostPolicy(min_interval_seconds=0, max_retry_after_seconds=30)),
        sleep=rec.sleep,
        **kw,  # type: ignore[arg-type]
    )


def ok_json(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


# =========================================================================== URL / host policy
@pytest.mark.parametrize(
    "url,why",
    [
        (f"http://{HOST}/x", "only https"),
        (f"ftp://{HOST}/x", "only https"),
        ("file:///etc/passwd", "only https"),
        ("https://evil.example.net/x", "not allowlisted"),
        (f"https://{HOST}.evil.net/x", "not allowlisted"),
        (f"https://evil.net/{HOST}", "not allowlisted"),
        (f"https://user:pw@{HOST}/x", "credentials"),
        (f"https://{HOST}@evil.net/x", "credentials"),
        (f"https://{HOST}:8443/x", "non-standard port"),
        ("https://169.254.169.254/latest/meta-data/", "not allowlisted"),
        ("https://[::1]/x", "not allowlisted"),
    ],
)
async def test_disallowed_urls_never_reach_the_network(url: str, why: str) -> None:
    rec = Recorder()
    async with make_client(ok_json, rec) as c:
        with pytest.raises(UnsafeRequest, match=why):
            await c.get(url)
    assert rec.requests == []


async def test_literal_ip_hosts_rejected_even_if_allowlisted() -> None:
    rec = Recorder()
    async with make_client(ok_json, rec, allowed=("93.184.216.34",), dns={"93.184.216.34": ["93.184.216.34"]}) as c:
        with pytest.raises(UnsafeRequest, match="literal IP"):
            await c.get("https://93.184.216.34/x")
    assert rec.requests == []


async def test_allowlist_is_case_insensitive_exact_match() -> None:
    rec = Recorder()
    async with make_client(ok_json, rec) as c:
        r = await c.get(f"https://{HOST.upper()}/x")
    assert r.status == 200


@pytest.mark.parametrize(
    "addrs",
    [
        ["127.0.0.1"],
        ["10.0.0.5"],
        ["172.16.3.4"],
        ["192.168.1.10"],
        ["169.254.169.254"],
        ["100.64.0.1"],
        ["0.0.0.0"],
        ["::1"],
        ["fd00::1"],
        ["fe80::1%eth0"],
        ["fd00:ec2::254"],
        ["::ffff:127.0.0.1"],
        ["::ffff:169.254.169.254"],
        ["64:ff9b::a9fe:a9fe"],
        ["2002:a9fe:a9fe::1"],
        ["224.0.0.1"],
        ["192.0.2.1"],
        ["93.184.216.34", "10.0.0.5"],  # one private answer poisons the set
    ],
)
async def test_hosts_resolving_to_non_public_addresses_rejected(addrs: list[str]) -> None:
    rec = Recorder()
    async with make_client(ok_json, rec, dns={HOST: addrs}) as c:
        with pytest.raises(UnsafeRequest, match="non-public"):
            await c.get(f"https://{HOST}/x")
    assert rec.requests == []
    assert not all(address_is_public(a) for a in addrs)


async def test_unresolvable_or_empty_dns_rejected() -> None:
    rec = Recorder()
    async with make_client(ok_json, rec, dns={HOST: []}) as c:
        with pytest.raises(UnsafeRequest, match="did not resolve"):
            await c.get(f"https://{HOST}/x")
    async with make_client(ok_json, rec, dns={}) as c:
        with pytest.raises(UnsafeRequest, match="DNS"):
            await c.get(f"https://{HOST}/x")
    assert rec.requests == []


def test_public_addresses_accepted() -> None:
    assert address_is_public("93.184.216.34")
    assert address_is_public("2606:4700:4700::1111")


# =========================================================================== redirects
def redirect_to(location: str, status: int = 302) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == HOST:
            return httpx.Response(status, headers={"location": location})
        return httpx.Response(200, json={"reached": str(request.url)})

    return handler


@pytest.mark.parametrize(
    "location,dns,why",
    [
        (f"https://{OTHER}/meta", {**PUBLIC, OTHER: ["169.254.169.254"]}, "non-public"),
        (f"https://{OTHER}/admin", {**PUBLIC, OTHER: ["10.1.2.3"]}, "non-public"),
        ("https://internal.corp.example/", {**PUBLIC, "internal.corp.example": ["10.0.0.1"]}, "not allowlisted"),
        ("https://169.254.169.254/latest/meta-data/", PUBLIC, "not allowlisted"),
        (f"http://{OTHER}/plain", PUBLIC, "only https"),
        (f"https://{OTHER}:8080/x", PUBLIC, "non-standard port"),
        ("//evil.example.net/x", PUBLIC, "not allowlisted"),  # scheme-relative
    ],
)
async def test_redirect_hop_is_checked(location: str, dns: dict, why: str) -> None:
    rec = Recorder()
    async with make_client(redirect_to(location), rec, dns=dns) as c:
        with pytest.raises(UnsafeRequest, match=why):
            await c.get(f"https://{HOST}/start")
    # Only the first, allowed hop was ever sent.
    assert [r.url.host for r in rec.requests] == [HOST]


async def test_allowed_redirect_is_followed_and_recorded() -> None:
    rec = Recorder()
    async with make_client(redirect_to(f"https://{OTHER}/final", 301), rec) as c:
        r = await c.get(f"https://{HOST}/start")
    assert r.status == 200 and r.final_url == f"https://{OTHER}/final"
    assert r.redirects == (f"https://{HOST}/start",)
    assert r.response_metadata()["redirects"] == [f"https://{HOST}/start"]


async def test_too_many_redirects() -> None:
    rec = Recorder()

    def loop(request: httpx.Request) -> httpx.Response:
        n = int(request.url.params.get("n", "0"))
        return httpx.Response(302, headers={"location": f"https://{HOST}/r?n={n + 1}"})

    async with make_client(loop, rec, max_redirects=3) as c:
        with pytest.raises(UnsafeRequest, match="too many redirects"):
            await c.get(f"https://{HOST}/r")
    assert len(rec.requests) == 4  # original + 3 allowed hops, then refused


async def test_redirect_without_location_fails() -> None:
    rec = Recorder()
    async with make_client(lambda r: httpx.Response(302), rec) as c:
        with pytest.raises(SourceResponseError, match="without Location"):
            await c.get(f"https://{HOST}/x")


# =========================================================================== size limits
class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.served = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for c in self.chunks:
            self.served += 1
            yield c


async def test_oversized_declared_content_length_rejected_before_reading() -> None:
    rec = Recorder()
    stream = Chunks([b"x" * 10])

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": str(10**9)}, stream=stream)

    async with make_client(handler, rec, max_bytes=1_000_000) as c:
        with pytest.raises(UnsafeRequest, match="declared content-length"):
            await c.get(f"https://{HOST}/big")
    assert stream.served == 0


async def test_oversized_streamed_body_rejected_early() -> None:
    rec = Recorder()
    stream = Chunks([b"y" * 65536 for _ in range(200)])  # 13 MB, no content-length
    async with make_client(lambda r: httpx.Response(200, stream=stream), rec, max_bytes=1_000_000) as c:
        with pytest.raises(UnsafeRequest, match="exceeds limit"):
            await c.get(f"https://{HOST}/stream")
    assert stream.served < 20  # stopped reading soon after the limit


async def test_per_request_limit_cannot_raise_the_client_limit() -> None:
    rec = Recorder()
    body = b"z" * 300_000
    async with make_client(lambda r: httpx.Response(200, content=body), rec, max_bytes=100_000) as c:
        with pytest.raises(UnsafeRequest):
            await c.get(f"https://{HOST}/x", max_bytes=10**9)


def _gzip_bomb(decoded_bytes: int) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9) as g:
        block = b"\x00" * (1 << 20)
        for _ in range(decoded_bytes // len(block)):
            g.write(block)
    return buf.getvalue()


async def test_gzip_bomb_beyond_size_limit_rejected() -> None:
    bomb = _gzip_bomb(64 << 20)
    assert len(bomb) < 200_000  # a small transfer ...
    chunks = [bomb[i : i + 4096] for i in range(0, len(bomb), 4096)]
    rec = Recorder()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Chunks(chunks))

    async with make_client(handler, rec, max_bytes=5_000_000) as c:
        with pytest.raises(UnsafeRequest):  # ... that expands past the decoded-bytes limit
            await c.get(f"https://{HOST}/bomb")


async def test_gzip_bomb_within_size_limit_rejected_by_ratio() -> None:
    bomb = _gzip_bomb(20 << 20)
    chunks = [bomb[i : i + 4096] for i in range(0, len(bomb), 4096)]
    rec = Recorder()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Chunks(chunks))

    async with make_client(handler, rec, max_bytes=50_000_000) as c:
        with pytest.raises(UnsafeRequest, match="compression ratio"):
            await c.get(f"https://{HOST}/bomb")


async def test_ordinary_gzip_body_is_decoded() -> None:
    payload = b'{"rows": [' + b",".join(b'{"v": %d}' % i for i in range(2000)) + b"]}"
    rec = Recorder()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-encoding": "gzip", "content-type": "application/json"},
                              content=gzip.compress(payload))

    async with make_client(handler, rec) as c:
        r = await c.get(f"https://{HOST}/ok")
    assert r.body == payload and require_json(r)["rows"][-1] == {"v": 1999}


# =========================================================================== retries
async def test_429_retry_after_is_honoured_then_succeeds() -> None:
    rec = Recorder()
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "7"})
        return httpx.Response(200, json={"ok": 1})

    async with make_client(handler, rec) as c:
        r = await c.get(f"https://{HOST}/x")
    assert r.status == 200 and r.attempts == 2
    assert rec.sleeps == [7.0]


async def test_retry_after_http_date_form() -> None:
    rec = Recorder()
    calls = {"n": 0}
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=10), usegmt=True)

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, headers={"retry-after": when}) if calls["n"] == 1 else httpx.Response(200)

    async with make_client(handler, rec) as c:
        await c.get(f"https://{HOST}/x")
    assert len(rec.sleeps) == 1 and 0 < rec.sleeps[0] <= 10


async def test_retry_after_beyond_cap_raises_without_sleeping() -> None:
    rec = Recorder()
    async with make_client(lambda r: httpx.Response(429, headers={"retry-after": "86400"}), rec) as c:
        with pytest.raises(SourceResponseError, match="Retry-After exceeds budget") as ei:
            await c.get(f"https://{HOST}/x")
    assert ei.value.retryable
    assert rec.sleeps == [] and len(rec.requests) == 1


async def test_5xx_retried_with_bounded_attempts() -> None:
    rec = Recorder()
    async with make_client(lambda r: httpx.Response(502), rec, max_attempts=3) as c:
        with pytest.raises(SourceResponseError, match="HTTP 502") as ei:
            await c.get(f"https://{HOST}/x")
    assert ei.value.retryable and ei.value.details["attempts"] == 3
    assert len(rec.requests) == 3 and len(rec.sleeps) == 2
    assert all(0 <= s <= 60 for s in rec.sleeps)


async def test_transport_errors_retried_with_bounded_attempts() -> None:
    rec = Recorder()

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with make_client(handler, rec, max_attempts=4) as c:
        with pytest.raises(SourceResponseError, match="transport failure"):
            await c.get(f"https://{HOST}/x")
    assert len(rec.requests) == 4 and len(rec.sleeps) == 3


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
async def test_client_errors_not_retried(status: int) -> None:
    rec = Recorder()
    async with make_client(lambda r: httpx.Response(status), rec) as c:
        with pytest.raises(SourceResponseError) as ei:
            await c.get(f"https://{HOST}/x")
    assert not ei.value.retryable and len(rec.requests) == 1


# =========================================================================== body parsing
def result(body: bytes, ctype: str = "application/json", status: int = 200) -> HttpResult:
    return HttpResult(f"https://{HOST}/x", f"https://{HOST}/x", status, {"content-type": ctype}, body,
                      datetime.now(UTC), 0.0, (), 1)


@pytest.mark.parametrize(
    "body,ctype",
    [
        ((ADV / "html_error_200.html").read_bytes(), "text/html; charset=utf-8"),
        ((ADV / "html_error_200.html").read_bytes(), "application/json"),  # lying content-type
        (b"\n\n   <HTML><body>Service Unavailable</body></HTML>", "application/json"),
        (b"<!doctype html><title>Maintenance</title>", "text/plain"),
    ],
)
def test_html_error_page_with_200_is_a_failure(body: bytes, ctype: str) -> None:
    with pytest.raises(SourceResponseError) as ei:
        require_json(result(body, ctype))
    assert ei.value.details.get("kind") == "html_body"


@pytest.mark.parametrize("body", [b"", b"{", b'{"a": 1,}', b"[1, 2", b"\xff\xfe\x00", b"null null", b"{'a': 1}"])
def test_malformed_json_is_a_failure(body: bytes) -> None:
    with pytest.raises(SourceResponseError) as ei:
        require_json(result(body))
    assert ei.value.details.get("kind") == "malformed_json" and not ei.value.retryable


@pytest.mark.parametrize("body", [b'{"v": NaN}', b'{"v": Infinity}', b'[-Infinity]', b'{"a":[1,{"b":NaN}]}'])
def test_non_finite_json_constants_rejected(body: bytes) -> None:
    with pytest.raises(SourceResponseError):
        require_json(result(body))


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: require_json accepts numeric overflow (1e400 -> float('inf')); NaN/Infinity are forbidden "
        "but an overflowing literal smuggles Infinity in. Use parse_float=Decimal or a finite check"
    ),
)
@pytest.mark.parametrize("body", [b'{"v": 1e400}', b"[-1e999]"])
def test_overflowing_json_numbers_rejected(body: bytes) -> None:
    with pytest.raises(SourceResponseError):
        require_json(result(body))


def test_valid_json_parsed() -> None:
    assert require_json(result(b'{"a": [1, 2.5, "x", null]}')) == {"a": [1, 2.5, "x", None]}


# =========================================================================== fingerprints
def test_request_fingerprint_strips_secret_params_and_is_canonical() -> None:
    secret = "SECRET-KEY-123"
    a = request_fingerprint("GET", f"https://{HOST}/v1?b=2&subscription-key={secret}&a=1",
                            secret_params=("subscription-key",))
    b = request_fingerprint("get", f"https://{HOST.upper()}/v1?a=1&b=2#frag")
    c = request_fingerprint("GET", f"https://{HOST}/v1?a=1&b=2&subscription-key=OTHER",
                            secret_params=("subscription-key",))
    assert a == b == c  # same request regardless of secret value, order, case or fragment
    assert secret not in a
    assert request_fingerprint("GET", f"https://{HOST}/v1?a=1&b=3") != b  # real params matter
    assert request_fingerprint("POST", f"https://{HOST}/v1?a=1&b=2") != b


# =========================================================================== archives
def _zip(members: dict[str, bytes], method: int = zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=method) as z:
        for name, data in members.items():
            z.writestr(zipfile.ZipInfo(name), data, compress_type=method)
    return buf.getvalue()


@pytest.mark.parametrize("name", ["../evil.shp", "a/../../evil.shp", "/etc/evil.shp", "dir\\evil.shp", ".."])
def test_zip_path_traversal_rejected(name: str) -> None:
    data = _zip({"ok.shp": b"1", name: b"payload"})
    with pytest.raises(UnsafeRequest, match="unsafe archive member path"):
        read_zip_members(data, (".shp",))


def test_zip_bomb_ratio_rejected() -> None:
    data = _zip({"world.shp": b"\x00" * (20 << 20)})
    assert len(data) < 100_000
    with pytest.raises(UnsafeRequest, match="compression ratio"):
        read_zip_members(data, (".shp",))


def test_zip_total_expansion_limit() -> None:
    import os

    data = _zip({"a.shp": os.urandom(300_000), "b.dbf": os.urandom(300_000)})  # incompressible
    with pytest.raises(UnsafeRequest, match="beyond the allowed size"):
        read_zip_members(data, (".shp", ".dbf"), max_total=500_000)


def test_zip_member_count_limit() -> None:
    data = _zip({f"f{i}.txt": b"" for i in range(1001)}, method=zipfile.ZIP_STORED)
    with pytest.raises(UnsafeRequest, match="too many archive members"):
        read_zip_members(data, (".shp",))


def _lying_size_zip() -> bytes:
    payload = b"A" * 1000
    data = bytearray(_zip({"x.shp": payload}, method=zipfile.ZIP_STORED))
    # Patch the declared uncompressed size (local header @22, central directory @24) to 10 bytes.
    data[22:26] = struct.pack("<I", 10)
    cd = data.rfind(b"PK\x01\x02")
    data[cd + 24 : cd + 28] = struct.pack("<I", 10)
    return bytes(data)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: read_zip_members lets zipfile.BadZipFile/zlib errors from corrupt, truncated or size-lying "
        "archives escape as non-RevaError exceptions; run_connector only catches RevaError, so one hostile "
        "archive aborts the whole connector run instead of quarantining the item"
    ),
)
@pytest.mark.parametrize("kind", ["lying_size", "truncated", "not_a_zip"])
def test_corrupt_archives_fail_as_typed_errors(kind: str) -> None:
    from reva.kernel.errors import RevaError

    good = _zip({"x.shp": b"B" * 5000})
    data = {"lying_size": _lying_size_zip(), "truncated": good[: len(good) // 2], "not_a_zip": b"<html>"}[kind]
    with pytest.raises(RevaError):
        read_zip_members(data, (".shp",))


def test_zip_members_filtered_by_suffix_and_flattened() -> None:
    data = _zip({"ne/ne_110m.shp": b"S", "ne/ne_110m.dbf": b"D", "ne/readme.html": b"<html>", "ne/": b""})
    out = read_zip_members(data, (".shp", ".dbf"))
    assert out == {"ne_110m.shp": b"S", "ne_110m.dbf": b"D"}


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: read_zip_members keys members by basename, so 'a/x.shp' and 'b/x.shp' silently overwrite "
        "each other (silent ambiguity/truncation); duplicate basenames must raise"
    ),
)
def test_zip_duplicate_basenames_are_ambiguous() -> None:
    data = _zip({"a/x.shp": b"first", "b/x.shp": b"second"})
    with pytest.raises(UnsafeRequest):
        read_zip_members(data, (".shp",))


def test_adversarial_zip_fixture_files_rejected() -> None:
    for f in sorted(ADV.glob("*.zip")):
        with pytest.raises(UnsafeRequest):
            read_zip_members(f.read_bytes(), (".shp", ".dbf", ".txt", ".prj"))
