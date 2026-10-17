"""Content-Encoding handling of the safe HTTP client (agent SAMEAS: peaceagreements.org sends "none")."""

from __future__ import annotations

import httpx
import pytest

from reva.adapters.http.safe_client import UnsafeRequest, content_codings


def test_identity_aliases_and_refusals() -> None:
    assert content_codings(httpx.Headers({})) == "identity"
    assert content_codings(httpx.Headers({"content-encoding": "none"})) == "identity"
    assert content_codings(httpx.Headers({"content-encoding": "gzip"})) == "gzip"
    with pytest.raises(UnsafeRequest):
        content_codings(httpx.Headers({"content-encoding": "br"}))
    with pytest.raises(UnsafeRequest):
        content_codings(httpx.Headers({"content-encoding": "gzip, none"}))
