"""Load recorded vintage fixtures and push them through the real ingest path.

Each vintage is served by :class:`reva.evidence.runner.FixtureHttp` (override for
the exact request fingerprint), fetched/parsed/validated by the real World Bank
connector and published by :func:`reva.evidence.ingest.publish_acquisition`. The
recorded vintage retrieval time from ``manifest.json`` is preserved.

Import with ``load_vintage_loader()`` from a test module (tests are not a package).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from reva.adapters.connectors.worldbank import WorldBankConnector
from reva.adapters.http.safe_client import request_fingerprint
from reva.evidence.ingest import PublishSummary, publish_acquisition
from reva.evidence.runner import FixtureHttp
from reva.ports.connectors import ConnectorSpec, FetchBudget, FetchItem, RawAcquisition

ROOT = Path(__file__).parent
MANIFEST: dict[str, Any] = json.loads((ROOT / "manifest.json").read_text())


def series(name: str) -> dict[str, Any]:
    return MANIFEST["series"][name]


def vintage(name: str, label: str) -> dict[str, Any]:
    for v in series(name)["vintages"]:
        if v["label"] == label:
            return v
    raise KeyError(f"{name}:{label}")


def vintage_bytes(name: str, label: str) -> bytes:
    return (ROOT / vintage(name, label)["file"]).read_bytes()


def connector_for(name: str) -> WorldBankConnector:
    y0, y1 = series(name)["years"]
    return WorldBankConnector(countries=tuple(MANIFEST["countries"]), years=(y0, y1))


class ReplayParserWorldBank(WorldBankConnector):
    """Same parsing code, registered as a NEW connector/parser version (re-parse of old bytes)."""

    VERSION = "1.1.0-vintage-replay-test"
    PARSER = "wb-parse-2-vintage-replay-test"

    def describe(self) -> ConnectorSpec:
        return super().describe().model_copy(update={"version": self.VERSION, "parser_version": self.PARSER})


def series_item(connector: WorldBankConnector) -> FetchItem:
    plan = connector.plan(None, FetchBudget())
    return next(i for i in plan.items if i.key == f"series:{MANIFEST['indicator']}:p1")


async def fetch_vintage(name: str, label: str, connector: WorldBankConnector | None = None,
                        body: bytes | None = None) -> RawAcquisition:
    c = connector or connector_for(name)
    item = series_item(c)
    data = vintage_bytes(name, label) if body is None else body
    http = FixtureHttp(MANIFEST["connector"], overrides={request_fingerprint("GET", item.url): data})
    raw = await c.fetch(item, http)
    return raw.model_copy(update={"retrieved_at": datetime.fromisoformat(vintage(name, label)["retrieved_at"])})


def with_availability(batches: list[Any], availability: dict[str, Any]) -> list[Any]:
    at = datetime.fromisoformat(availability["source_available_at"])
    basis = availability["availability_basis"]
    return [
        b.model_copy(update={"observations": tuple(
            o.model_copy(update={"source_available_at": at, "availability_basis": basis}) for o in b.observations)})
        for b in batches
    ]


async def ingest_vintage(engine: Engine, store: Any, name: str, label: str, *,
                         connector: WorldBankConnector | None = None, replay_of: str | None = None,
                         retrieved_at: datetime | None = None, body: bytes | None = None
                         ) -> tuple[PublishSummary, RawAcquisition]:
    c = connector or connector_for(name)
    raw = await fetch_vintage(name, label, c, body)
    update: dict[str, Any] = {}
    if replay_of is not None:
        update["replay_of_acquisition_id"] = replay_of
    if retrieved_at is not None:
        update["retrieved_at"] = retrieved_at
    if update:
        raw = raw.model_copy(update=update)
    batches = list(c.parse(raw))
    reports = [c.validate(b) for b in batches]
    assert all(not r.blocking for r in reports), [r.model_dump() for r in reports]
    avail = vintage(name, label).get("availability")
    if avail:
        batches = with_availability(batches, avail)
    summary = publish_acquisition(engine, store, c.describe(), raw, batches, reports)
    return summary, raw


def register_connector_variant(engine: Engine, store: Any, connector: WorldBankConnector) -> None:
    """Register a connector version and its (reviewed) eligibility like the seed does."""
    from reva.adapters.postgres import repositories as repo
    from reva.cli.seed import record_mapping_and_eligibility, register_connector_version
    from reva.ontology.registry import load_registry

    spec = connector.describe()
    reg = load_registry()
    base = [m for m in reg.mappings if m["connector"]["name"] == spec.name]
    with engine.begin() as conn:
        act = repo.simple_activity("registry.connector_variant", "review", "maintainer:test-vintages",
                                   {"connector": spec.name, "version": spec.version})
        repo.record_activity(conn, act)
        register_connector_version(conn, store, spec, act.id)
        for m in base:
            record_mapping_and_eligibility(
                conn, {**m, "connector": {"name": spec.name, "version": spec.version}}, act.id,
                policy_version=f"test-{spec.version}",
            )
