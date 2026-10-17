"""V8 COUVERTURE: /v1/v8/coverage/* over a small synthetic coverage document (no database).

The document is SYNTHETIC (three countries, three tables) and only exercises the shaping rules: absent cells stay
absent (never zero), countries without data are listed, staleness and live checks are passed through.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from reva.v8.coverage import service as svc


def synthetic_doc() -> dict[str, Any]:
    t = {
        "domain_basis": "source",
        "indicators": 1,
        "numeric_cells": 1,
        "text_cells": 0,
        "missing_cells": 0,
        "rows_without_country": 0,
        "unknown_iso3": [],
        "period_min": "2000-01-01",
        "period_max": "2025-01-01",
        "first_recorded_at": "2026-10-16T00:00:00+00:00",
        "versions": 1,
        "connectors": [],
        "parser_version": "p/1",
        "coverage_note": None,
        "read_error": None,
        "future_rows": 0,
    }
    tables = [
        {
            **t,
            "key": "atlas.s1.gdp",
            "source": "s1",
            "dataset": "gdp",
            "domain": "macro",
            "rows": 30,
            "countries": 2,
            "latest_past_year": 2025,
            "recorded_at": "2026-10-16T10:00:00+00:00",
            "last_fetch_at": "2020-01-01T00:00:00+00:00",
            "cadence_seconds": 86400,
            "licence": {"licence": "CC-BY-4.0"},
            "licence_class": "open",
        },
        {
            **t,
            "key": "atlas.s2.cpi",
            "source": "s2",
            "dataset": "cpi",
            "domain": "macro",
            "rows": 5,
            "countries": 1,
            "latest_past_year": 2024,
            "recorded_at": "2026-10-17T10:00:00+00:00",
            "last_fetch_at": None,
            "cadence_seconds": None,
            "licence": {"licence": "CC-BY-NC-4.0", "non_commercial": True},
            "licence_class": "non_commercial",
        },
        {
            **t,
            "key": "atlas.s3.deaths",
            "source": "s3",
            "dataset": "deaths",
            "domain": "health",
            "rows": 7,
            "countries": 1,
            "latest_past_year": 2023,
            "recorded_at": "2026-10-15T10:00:00+00:00",
            "last_fetch_at": None,
            "cadence_seconds": None,
            "licence": None,
            "licence_class": "unknown",
        },
    ]
    return {
        "schema": "reva-v8-coverage/1",
        "computed_at": "2026-10-17T12:00:00+00:00",
        "db_time": "2026-10-17T12:00:00+00:00",
        "today": "2026-10-17",
        "origin": "live",
        "hist_from": 1900,
        "elapsed_s": 1.0,
        "domains": [
            {"id": "macro", "fr": "Macroéconomie et travail", "en": "Macro", "themes": ["economy"], "kg_kinds": []},
            {"id": "health", "fr": "Santé et maladies", "en": "Health", "themes": ["health"], "kg_kinds": ["disease"]},
            {"id": "other", "fr": "Non classé", "en": "Unclassified", "themes": [], "kg_kinds": []},
        ],
        "places": [
            {
                "iso3": "DEU",
                "label": "Germany",
                "label_fr": "Allemagne",
                "region": "Europe",
                "subregion": "Western Europe",
                "placement": "m49",
            },
            {
                "iso3": "FRA",
                "label": "France",
                "label_fr": "France",
                "region": "Europe",
                "subregion": "Western Europe",
                "placement": "m49",
            },
            {
                "iso3": "NGA",
                "label": "Nigeria",
                "label_fr": "Nigeria",
                "region": "Africa",
                "subregion": "Sub-Saharan Africa",
                "placement": "m49",
            },
        ],
        "regions": {"Africa": "Afrique", "Europe": "Europe"},
        "subregions": {"Sub-Saharan Africa": "Afrique subsaharienne", "Western Europe": "Europe de l’Ouest"},
        "tables": tables,
        "table_countries": {
            "atlas.s1.gdp": {"FRA": [20, 2000, 2025, 0], "DEU": [10, 2000, 2025, 0]},
            "atlas.s2.cpi": {"FRA": [5, 2020, 2024, 0]},
            "atlas.s3.deaths": {"NGA": [7, 2017, 2023, 0]},
        },
        "cells": {
            "macro": {"DEU": [10, 1, 2000, 2025, 0], "FRA": [25, 2, 2000, 2025, 0]},
            "health": {"NGA": [7, 1, 2017, 2023, 0]},
        },
        "hist": {"macro": {"2024": 3, "2025": 2}, "health": {"2023": 7}},
        "hist_before": {"macro": 0, "health": 0},
        "catalog": {"macro": {"concepts": 3, "with_loaded_table": 2, "without_loaded_table": 1, "examples": []}},
        "connectors": [
            {
                "name": "atlas.s9",
                "version": "1.0.0",
                "atlas_source": "s9",
                "source_key": "s9",
                "provider": "P9",
                "licence": "CC0",
                "datasets": ["t"],
                "cadence_seconds": 86400,
                "tables_expected": ["atlas.s9.t"],
                "tables_loaded": [],
                "acquired": False,
                "versions_acquired": [],
                "last_retrieved_at": None,
                "domain": "health",
            },
        ],
        "kg": {
            "located_in_properties": ["P17"],
            "located_in_note": "note",
            "kinds": [
                {
                    "kind": "disease",
                    "domain": "health",
                    "entities": 4,
                    "sources": {"mondo": 4},
                    "last": None,
                    "by_domain": {"health": 4},
                }
            ],
            "sources": [{"source_key": "mondo", "statements": 9, "entities": 4, "loads": 1, "last_load_at": None}],
            "cells": {"health": {"FRA": 2}},
            "kind_countries": {"disease": {"FRA": 2}},
        },
    }


@pytest.fixture()
def cov_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "coverage.json").write_text(json.dumps(synthetic_doc()), encoding="utf-8")
    monkeypatch.setattr(svc, "DEFAULT_DIR", tmp_path)
    return tmp_path


def test_matrix_region_level_counts_and_absent_cells(cov_dir: Path) -> None:
    c = svc.load()
    m = svc.matrix(c, "region", None, {"tables_registered_since": 0, "last_registered_at": None})
    assert [col["id"] for col in m["columns"]] == ["Africa", "Europe"]
    macro = next(r for r in m["rows"] if r["id"] == "macro")
    assert macro["cells"]["Europe"] == {
        "countries_with_rows": 2,
        "rows": 35,
        "tables": 2,
        "y1": 2025,
        "y1_median": 2025,
    }
    assert "Africa" not in macro["cells"]  # not covered: absent, never a zero cell
    health = next(r for r in m["rows"] if r["id"] == "health")
    assert health["cells"]["Europe"] == {
        "countries_with_rows": 0,
        "rows": 0,
        "tables": 0,
        "kg": 2,
        "countries_with_kg": 1,
    }
    assert macro["summary"]["licences"] == {"open": 1, "non_commercial": 1}
    assert all(r["id"] != "other" for r in m["rows"])  # empty "other" row hidden
    assert "absence" in m["meta"]["note"]


def test_matrix_country_level(cov_dir: Path) -> None:
    m = svc.matrix(svc.load(), "country", None, None)
    assert [col["id"] for col in m["columns"]] == ["NGA", "DEU", "FRA"]  # region order, then label
    macro = next(r for r in m["rows"] if r["id"] == "macro")
    assert macro["cells"]["FRA"] == {"rows": 25, "tables": 2, "y0": 2000, "y1": 2025, "future_rows": 0}
    assert "NGA" not in macro["cells"]


def test_domain_and_country_detail(cov_dir: Path) -> None:
    c = svc.load()
    d = svc.domain_detail(c, "macro", None, {})
    assert [t["key"] for t in d["tables"]] == ["atlas.s1.gdp", "atlas.s2.cpi"]
    assert d["tables"][0]["overdue"] is True and d["tables"][1]["overdue"] is None
    assert [x["iso3"] for x in d["countries_without_data"]] == ["NGA"]
    h = svc.domain_detail(c, "health", None, {})
    assert h["connectors"][0]["name"] == "atlas.s9" and h["kg_kinds"][0]["entities"] == 4
    fra = svc.country_detail(c, "FRA", None)
    assert {t["key"] for t in fra["tables"]} == {"atlas.s1.gdp", "atlas.s2.cpi"}
    assert fra["kg_kinds"] == [{"kind": "disease", "entities": 2}]
    assert next(x for x in fra["domains"] if x["id"] == "health")["cell"] == {"kg": 2}
    nga = svc.country_detail(c, "NGA", None)
    assert nga["domains_without_data"] == ["Macroéconomie et travail"]


def test_freshness_lists_never_loaded_connectors(cov_dir: Path) -> None:
    f = svc.freshness(svc.load())
    assert [d["id"] for d in f["domains"]] == ["macro", "health"]
    assert f["domains"][0]["tables"][0]["latest_past_year"] == 2025
    assert f["never_loaded"][0]["name"] == "atlas.s9"


class _Auth:
    def authenticate(self, request: Any) -> Any:
        from reva.api.auth import Principal

        return Principal(username="t", actor="token:t", method="token", scopes=frozenset({"read"}))


@pytest.fixture()
def client(cov_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from contextlib import contextmanager

    from reva.api.errors import install_error_handlers
    from reva.api.routes import v8_coverage

    app = FastAPI()
    install_error_handlers(app)
    app.state.auth = _Auth()
    app.include_router(v8_coverage.router)

    @contextmanager
    def fake_conn(request: Any) -> Iterator[object]:
        yield object()

    monkeypatch.setattr(v8_coverage, "read_conn", fake_conn)
    monkeypatch.setattr(svc, "staleness", lambda conn, c: {"tables_registered_since": 3, "last_registered_at": None})
    monkeypatch.setattr(svc, "observations", lambda conn, reg, origin="live": {"_checked_at": "now"})
    with TestClient(app) as tc:
        yield tc


def test_routes(client: TestClient) -> None:
    r = client.get("/v1/v8/coverage/matrix?level=subregion")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["meta"]["stale"]["tables_registered_since"] == 3
    assert {c["id"] for c in body["columns"]} == {"Western Europe", "Sub-Saharan Africa"}
    assert client.get("/v1/v8/coverage/matrix?level=planet").status_code == 422
    assert client.get("/v1/v8/coverage/domains/macro").json()["summary"]["tables"] == 2
    assert client.get("/v1/v8/coverage/domains/nope").status_code == 404
    assert client.get("/v1/v8/coverage/countries/FRA").json()["label"] == "France"
    assert client.get("/v1/v8/coverage/countries/ZZZ").status_code == 404
    assert client.get("/v1/v8/coverage/freshness").status_code == 200
    p = client.get("/v1/v8/coverage/pending").json()
    assert p["type"] == "load" and p["groups"] and p["checked_at"] == "now"
    d = client.get("/v1/v8/coverage/decisions").json()
    assert {g["kind"] for g in d["groups"]} >= {"licence", "account_key", "permission", "policy_choice"}


def test_not_computed_is_a_typed_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    monkeypatch.setattr(svc, "DEFAULT_DIR", tmp_path / "empty")
    r = client.get("/v1/v8/coverage/matrix")
    assert r.status_code == 404 and "reva v8 coverage refresh" in r.text
