"""No secrets in repository files, fixtures, connector specs, manifests or logs (§24.1 #31)."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from reva.adapters.connectors.base import to_raw
from reva.adapters.connectors.registry import builtin_connectors
from reva.adapters.http.rate_limit import HostPolicy, RateLimiter
from reva.adapters.http.safe_client import HttpResult, SafeHttpClient
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import FetchBudget, FetchItem
from reva.settings import SECRET_ENV_KEYS, redact_env

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures"

# High-confidence credential shapes: never acceptable anywhere in the repository.
HARD_PATTERNS = {
    "aws_access_key_id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "private_key_block": re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "slack_token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "openai_like_key": re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{24,}\b"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
}
# Softer shapes: acceptable only as documented local-only examples.
URL_CREDENTIALS = re.compile(r"[a-z][a-z0-9+.-]*://([^:@/\s'\"]+):([^@/\s'\"]+)@")
SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(REVA_[A-Z_]*(?:PASSWORD|SECRET|TOKEN|API_KEY|ACCESS_KEY_ID|SECRET_ACCESS_KEY)|DATABASE_URL"
    r"|POSTGRES_PASSWORD)\s*[:=]\s*['\"]?([^\s'\"#]+)"
)
LOCAL_ONLY_VALUE = re.compile(r"^(?:local_[a-z_]*only|replace_with_[a-z_]+)$")
PLACEHOLDERS = {"pw", "pass", "password", "PASSWORD", "secret", "***", "xxx", "changeme"}


def _placeholder(value: str) -> bool:
    """Templating, redaction or documentation ellipses rather than a value."""
    return value.startswith(("$", "{", "<", "*")) or value in PLACEHOLDERS or set(value) <= set(".…")


def tracked_text_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git not available")
    files = []
    for rel in out.decode().split("\0"):
        if not rel or rel == "uv.lock":
            continue
        p = REPO / rel
        if not p.is_file() or p.stat().st_size > 5_000_000:
            continue
        head = p.read_bytes()[:4096]
        if b"\0" in head:
            continue  # binary (zip, parquet, images)
        files.append(p)
    return files


def _allowed_example(token: str) -> bool:
    return "EXAMPLE" in token or "example" in token


def test_env_file_is_not_tracked_and_is_ignored() -> None:
    out = subprocess.run(["git", "ls-files", ".env"], cwd=REPO, capture_output=True, text=True).stdout
    assert out.strip() == ""
    ignored = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=REPO).returncode
    assert ignored == 0


def test_no_hard_credentials_anywhere_in_repository() -> None:
    hits = []
    for p in tracked_text_files():
        content = p.read_text(errors="replace")
        for name, rx in HARD_PATTERNS.items():
            for m in rx.finditer(content):
                if not _allowed_example(m.group(0)):
                    hits.append(f"{p.relative_to(REPO)}: {name}")
    assert hits == []


def test_non_test_files_have_no_credentials_except_documented_local_examples() -> None:
    hits = []
    for p in tracked_text_files():
        rel = str(p.relative_to(REPO))
        if rel.startswith("tests/") and not rel.startswith("tests/fixtures/"):
            continue  # test code may contain obviously fake values; fixtures may not
        content = p.read_text(errors="replace")
        for m in URL_CREDENTIALS.finditer(content):
            user, pw = m.group(1), m.group(2)
            if _placeholder(pw):
                continue  # placeholders / templating / redaction
            if LOCAL_ONLY_VALUE.match(pw):
                continue  # documented local-only example (self-describing value)
            hits.append(f"{rel}: credentials in URL for user {user!r}")
        for m in SECRET_ASSIGNMENT.finditer(content):
            value = m.group(2)
            if _placeholder(value) or value.lower() in ("", "none", "null", "true", "false"):
                continue
            if rel.endswith((".py", ".ts", ".tsx")):
                continue  # code refers to names; values come from the environment
            if LOCAL_ONLY_VALUE.match(value):
                continue  # e.g. ephemeral CI/compose service containers, .env.example
            if value.startswith("postgres") and URL_CREDENTIALS.search(value) is None:
                continue
            if value.startswith("postgres") and LOCAL_ONLY_VALUE.match(URL_CREDENTIALS.search(value).group(2)):
                continue
            hits.append(f"{rel}: {m.group(1)} has a literal value")
    assert hits == []


def test_documented_examples_are_marked_local_only() -> None:
    env_example = (REPO / ".env.example").read_text()
    assert "Never copy these passwords to production" in env_example
    for line in env_example.splitlines():
        key, _, value = line.partition("=")
        if key.strip() in SECRET_ENV_KEYS and value.strip():
            assert "local" in value or value.startswith("replace_with"), line


# --------------------------------------------------------------------------- fixtures / manifests
SECRET_QUERY = re.compile(
    r"(?i)[?&](subscription-key|api[_-]?key|apikey|token|access_token|key|secret|password"
    r"|signature|sig)="
)
SECRET_HEADERS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "ocp-apim-subscription-key",
}


def test_captured_fixture_manifests_contain_no_secrets() -> None:
    manifests = sorted((FIXTURES / "source").glob("*/manifest.json"))
    assert manifests, "expected captured fixtures"
    for m in manifests:
        for e in json.loads(m.read_text()):
            assert not SECRET_QUERY.search(e["url"]), (m, e["url"])
            assert not (SECRET_HEADERS & {h.lower() for h in e.get("headers", {})}), (m, e["headers"])
            assert e.get("licence"), f"{m}: captured fixture without licence"
            assert len(e["sha256"]) == 64


def test_captured_fixture_bodies_contain_no_credentials() -> None:
    for p in sorted((FIXTURES / "source").glob("*/*")):
        if p.suffix == ".zip":
            continue
        content = p.read_text(errors="replace")
        for name, rx in HARD_PATTERNS.items():
            assert not rx.search(content), (p, name)


def test_builtin_connector_specs_contain_no_secrets() -> None:
    for name, conn in builtin_connectors(contact_email="ops@example.org").items():
        spec = conn.describe()
        # Authentication is a reference to an environment secret, never a value.
        assert spec.authentication_secret_ref is None or re.fullmatch(
            r"[A-Z][A-Z0-9_]*", spec.authentication_secret_ref
        )
        blob = spec.model_dump_json()
        for rx in HARD_PATTERNS.values():
            assert not rx.search(blob), name
        assert not SECRET_QUERY.search(blob), name
        for item in conn.plan(None, FetchBudget(max_requests=1000)).items:
            assert not SECRET_QUERY.search(item.url), (name, item.url)


def test_registry_and_contracts_contain_no_secrets() -> None:
    for p in sorted([*(REPO / "registry").rglob("*"), *(REPO / "contracts").rglob("*")]):
        if p.is_file():
            content = p.read_text(errors="replace")
            for name, rx in HARD_PATTERNS.items():
                assert not rx.search(content), (p, name)
            assert not URL_CREDENTIALS.search(content), p


# --------------------------------------------------------------------------- runtime redaction
def test_redact_env_masks_every_declared_secret_key() -> None:
    env = {k: f"value-of-{k.lower()}" for k in SECRET_ENV_KEYS} | {"REVA_ENV": "production"}
    red = redact_env(env)
    dumped = json.dumps(red)
    for k in SECRET_ENV_KEYS:
        assert red[k] == "***"
        assert env[k] not in dumped
    assert red["REVA_ENV"] == "production"


def _failing_client() -> SafeHttpClient:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream exploded")

    return SafeHttpClient(
        allowed_hosts=["comtradeapi.un.org"],
        transport=httpx.MockTransport(handler),
        resolver=lambda h: ["93.184.216.34"],
        rate_limiter=RateLimiter(default=HostPolicy(min_interval_seconds=0)),
        sleep=lambda s: _noop(),
        max_attempts=2,
    )


async def test_http_errors_do_not_echo_secret_query_values() -> None:
    secret = "sub-key-0f9e8d7c6b5a"
    async with _failing_client() as client:
        with pytest.raises(SourceResponseError) as ei:
            await client.get(f"https://comtradeapi.un.org/data/v1/get?subscription-key={secret}&x=1")
    rendered = " ".join([str(ei.value), repr(ei.value.details), json.dumps(ei.value.problem(), default=str)])
    assert secret not in rendered


async def test_http_client_logs_do_not_contain_secret_query_values(caplog: pytest.LogCaptureFixture) -> None:
    secret = "sub-key-77aa66bb55cc"
    caplog.set_level(logging.INFO)  # the production default REVA_LOG_LEVEL
    async with _failing_client() as client:
        with pytest.raises(SourceResponseError):
            await client.get(f"https://comtradeapi.un.org/data/v1/get?subscription-key={secret}&x=1")
    assert secret not in caplog.text


async def _noop() -> None:
    return None


def _result_with_secret(secret: str) -> tuple[FetchItem, HttpResult]:
    url = f"https://comtradeapi.un.org/data/v1/get/C/A/HS?reporterCode=251&subscription-key={secret}"
    item = FetchItem(key="k", url=url, max_bytes=1000)
    res = HttpResult(url, url, 200, {"content-type": "application/json"}, b"{}", datetime.now(UTC), 0.1, (url,), 1)
    return item, res


def test_raw_acquisition_fingerprint_excludes_subscription_key() -> None:
    secret = "sub-key-1a2b3c4d5e6f"
    item, res = _result_with_secret(secret)
    raw = to_raw(item, res, "live")
    assert secret not in raw.request_fingerprint


def test_raw_acquisition_metadata_excludes_secret_query_values() -> None:
    secret = "sub-key-9z8y7x6w5v4u"
    item, res = _result_with_secret(secret)
    raw = to_raw(item, res, "live")
    assert secret not in json.dumps(raw.response_metadata, default=str)


async def test_recorded_fixture_manifest_excludes_secret_query_values(tmp_path: Path) -> None:
    from reva.evidence.runner import RecordingHttp

    secret = "sub-key-5t4r3e2w1q0p"
    _, res = _result_with_secret(secret)

    class FakeClient:
        async def get(self, url: str, **_: Any) -> HttpResult:
            return res

        async def aclose(self) -> None:
            return None

    rec = RecordingHttp(FakeClient(), "un.comtrade.keyed", "UN Comtrade terms", root=tmp_path)  # type: ignore[arg-type]
    await rec.get(res.url)
    rec.write_manifest()
    assert secret not in (tmp_path / "un.comtrade.keyed" / "manifest.json").read_text()
