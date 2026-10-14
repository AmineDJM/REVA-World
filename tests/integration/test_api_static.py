"""Static UI serving and SPA fallback (no database needed: engines are lazy)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from reva.api.app import create_app
from reva.settings import Settings


def _settings() -> Settings:
    return Settings(_env_file=None, reva_env="test", reva_allowed_hosts="testserver")  # type: ignore[call-arg]


def test_root_points_to_api_without_ui(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(), static_dir=tmp_path / "missing")) as c:
        r = c.get("/")
        assert r.status_code == 200 and r.json()["api"] == "/v1"
        assert c.get("/country/FRA").status_code == 404


def test_spa_fallback_and_assets(tmp_path: Path) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><title>REVA</title>")
    (tmp_path / "assets" / "app-1234.js").write_text("console.log(1)")
    (tmp_path / ".env").write_text("SECRET=1")
    (tmp_path.parent / "outside.txt").write_text("outside")
    with TestClient(create_app(_settings(), static_dir=tmp_path)) as c:
        assert "REVA" in c.get("/").text
        deep = c.get("/country/FRA")
        assert deep.status_code == 200 and "REVA" in deep.text
        js = c.get("/assets/app-1234.js")
        assert js.text == "console.log(1)" and "immutable" in js.headers["cache-control"]
        assert "SECRET" not in c.get("/.env").text
        assert "outside" not in c.get("/..%2Foutside.txt").text
        r = c.get("/v1/unknown")
        assert r.status_code == 404 and r.headers["content-type"].startswith("application/problem+json")
        assert c.get("/health/unknown").status_code == 404
        assert "default-src 'self'" in deep.headers["content-security-policy"]
