"""Every observation exported from a real fixture ingestion conforms to observation.schema.json.

Captured World Bank WDI and UN Comtrade responses are replayed through the real
ingest path (``run_connector`` + ``FixtureHttp``, ``persist_cursor=False``) into
the session ``db``; each resulting ``evidence.observation`` row is exported with
``reva.evidence.export.export_observation`` and validated with JSON Schema Draft
2020-12 plus a format checker that really checks ``uuid`` and ``date-time``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import text

from reva.adapters.connectors.registry import builtin_connectors
from reva.evidence.export import ObservationNotFound, export_observation
from reva.evidence.runner import FixtureHttp, run_connector

pytestmark = pytest.mark.db

SCHEMA = json.loads((Path(__file__).resolve().parents[2] / "contracts" / "observation.schema.json").read_text())
CONNECTORS = ("worldbank.wdi.indicators", "un.comtrade.public_exact")
SOURCES = ("worldbank.wdi", "un.comtrade.public_preview")


def strict_format_checker() -> FormatChecker:
    """Draft 2020-12 formats; ``date-time`` is checked even without rfc3339-validator installed."""
    import re
    from datetime import datetime

    checker = FormatChecker()
    rfc3339 = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$")

    @checker.checks("date-time", raises=ValueError)
    def _dt(v: object) -> bool:
        if not isinstance(v, str):
            return True
        if not rfc3339.match(v):
            return False
        datetime.fromisoformat(v)
        return True

    assert not checker.conforms("2024-13-01T00:00:00Z", "date-time")
    assert not checker.conforms("2024-01-01 00:00:00", "date-time")
    assert not checker.conforms("not-a-uuid", "uuid")
    return checker


VALIDATOR = Draft202012Validator(SCHEMA, format_checker=strict_format_checker())


@pytest.fixture(scope="module")
def ingested(db: Any) -> list[dict[str, Any]]:
    for name in CONNECTORS:
        c = builtin_connectors()[name]
        rep = asyncio.run(run_connector(c, FixtureHttp(name), db.store, db.worker, persist_cursor=False))
        assert all(o.status in ("published", "unchanged") for o in rep.outcomes), rep.as_dict()
    with db.api.connect() as c:
        rows = (
            c.execute(
                text(
                    """SELECT o.*, a.retrieved_at AS acq_retrieved_at, a.raw_sha256 AS acq_raw_sha256,
                              s.source_key
                         FROM evidence.observation o
                         JOIN evidence.acquisition a ON a.id = o.acquisition_id
                         JOIN evidence.source s ON s.id = o.source_id
                        WHERE s.source_key = ANY(:keys)
                        ORDER BY o.logical_key, o.revision_no, o.id"""
                ),
                {"keys": list(SOURCES)},
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


@pytest.fixture(scope="module")
def exported(db: Any, ingested: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    with db.api.connect() as c:
        return [(row, export_observation(c, row["id"])) for row in ingested]


def test_both_sources_and_both_value_shapes_are_exercised(exported: list[tuple[dict, dict]]) -> None:
    from collections import Counter

    counts = Counter((row["source_key"], rec["value"]["kind"]) for row, rec in exported)
    assert len(exported) > 200, counts
    assert {row["source_key"] for row, _ in exported} == set(SOURCES)
    kinds = {rec["value"]["kind"] for _, rec in exported}
    assert kinds == {"decimal", "missing"}  # Comtrade DZA rows are typed Missing


def test_every_exported_observation_validates_against_contract(exported: list[tuple[dict, dict]]) -> None:
    bad = {}
    for row, rec in exported:
        errs = [f"/{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in VALIDATOR.iter_errors(rec)]
        if errs:
            bad[str(row["id"])] = errs[:3]
    assert bad == {}


def test_export_is_faithful_to_the_stored_row(exported: list[tuple[dict, dict]]) -> None:
    for row, rec in exported:
        for f in ("source_id", "connector_version_id", "acquisition_id", "subject_id", "concept_version_id",
                  "support_id", "provenance_activity_id"):
            assert rec[f] == str(row[f])
        assert rec["logical_key"] == row["logical_key"]
        assert rec["source_revision_id"] == row["source_revision_id"]
        assert rec["raw_sha256"] == row["acq_raw_sha256"] == row["raw_selector"]["artifact_sha256"]
        assert rec["raw_selector"] == row["raw_selector"]
        assert rec["source_flags"] == row["source_flags"]
        assert rec["availability_basis"] == row["availability_basis"]
        for f, src in (("observed_at", "observed_at"), ("source_published_at", "source_published_at"),
                       ("source_available_at", "source_available_at"), ("retrieved_at", "acq_retrieved_at")):
            if row[src] is None:
                assert rec[f] is None
            else:
                assert rec[f].endswith("Z")
                from datetime import datetime

                assert datetime.fromisoformat(rec[f]) == row[src].astimezone(UTC)  # same instant
        stored = row["value"]
        if stored["kind"] == "decimal":
            # The decimal string is copied byte-for-byte, with unit and basis as stored.
            assert rec["value"] == {"kind": "decimal", "value": stored["value"], "unit_ref": stored["unit_ref"],
                                    "basis": stored["basis"]}
        else:
            assert rec["value"] == {"kind": "missing", "reason": stored["reason"]}
        # Connectors publish no uncertainty: unquantified/unknown, nothing invented.
        u = rec["uncertainty"]
        assert (u["status"], u["kind"]) == ("unquantified", "unknown")
        assert u["artifact_sha256"] is None and u["method_version"] is None
        assert u["description"] == row["uncertainty"]["description"]


def test_decimal_strings_round_trip_exactly_through_json(exported: list[tuple[dict, dict]]) -> None:
    for row, rec in exported:
        back = json.loads(json.dumps(rec, allow_nan=False))
        assert back == rec
        if rec["value"]["kind"] == "decimal":
            assert back["value"]["value"] == row["value"]["value"]
            assert Decimal(back["value"]["value"]) == Decimal(row["value"]["value"])


def test_comtrade_dza_missing_is_exported_as_typed_missing(exported: list[tuple[dict, dict]]) -> None:
    from reva.cli.seed import entity_id

    dza = str(entity_id("DZA"))
    no_rows = [rec for row, rec in exported
               if row["source_key"] == "un.comtrade.public_preview" and row["source_flags"].get("provider_returned_no_rows")]
    assert any(rec["subject_id"] == dza for rec in no_rows), "captured DZA zero-row years must be exported"
    for rec in no_rows:  # an absent provider row is typed Missing, never a number or zero
        assert rec["value"] == {"kind": "missing", "reason": "unavailable"}


def test_export_is_deterministic(db: Any, exported: list[tuple[dict, dict]]) -> None:
    row, rec = exported[0]
    with db.api.connect() as c:
        assert export_observation(c, row["id"]) == rec
        assert export_observation(c, str(row["id"])) == rec


def test_unknown_observation_id_is_a_typed_not_found(db: Any) -> None:
    with db.api.connect() as c, pytest.raises(ObservationNotFound) as e:
        export_observation(c, uuid.uuid4())
    assert e.value.problem()["status"] == 404
