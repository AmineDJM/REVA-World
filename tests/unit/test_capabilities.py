"""Capability catalogue consistency: honest statuses, structured gaps, API shape."""

from __future__ import annotations

import importlib
import importlib.util
import json
from types import SimpleNamespace

import pytest

from reva import capabilities as caps
from reva.kernel.errors import CapabilityGap

LATER_PHASES = {"P8", "P9", "P10", "P11", "P12"}


@pytest.fixture(autouse=True)
def _no_sandbox_env(monkeypatch):
    monkeypatch.delenv(caps.SANDBOX_ENDPOINT_ENV, raising=False)


def test_ids_unique_and_versioned():
    ids = [c.capability_id for c in caps.catalogue()]
    assert len(ids) == len(set(ids))
    d = caps.as_dict()
    assert d["catalogue_version"] == caps.CATALOGUE_VERSION
    assert d["schema"] == caps.CATALOGUE_SCHEMA
    assert len(d["digest"]) == 64
    json.dumps(d, allow_nan=False)  # machine-readable, JSON-safe
    assert sum(d["summary"].values()) == len(ids)


def test_every_unavailable_entry_has_reason_phase_and_gates():
    for c in caps.catalogue():
        assert c.phase in caps.PHASES
        assert c.required_evidence
        if c.status != "implemented":
            assert c.reason and c.reason.strip(), c.capability_id


def test_later_phase_domain_capabilities_are_never_implemented():
    for c in caps.catalogue():
        if c.phase in LATER_PHASES and c.status == "implemented":
            assert c.verification_only, f"{c.capability_id} claims a P8-P12 capability"
            assert c.capability_id.startswith("verification.")


def test_v1_fidelity_envelope():
    for level in ("L0", "L1", "L2"):
        assert caps.get(f"fidelity.{level}").status == "implemented"
    for level in ("L3", "L4"):
        c = caps.get(f"fidelity.{level}")
        assert c.status == "reserved" and not c.available
    assert "justified provenance" in caps.get("fidelity.L3").reason


def test_required_reserved_entries_present():
    expected = {
        "physical.field_adapter": "P8", "physical.field_to_region": "P8", "physical.external_model": "P8",
        "economic.input_output": "P9", "economic.sfc_ledger": "P9", "coupling.feedback": "P9",
        "population.households": "P10", "population.privacy_accounting": "P10",
        "autonomy.external_sandbox_runner": "P11", "decision.search": "P12", "decision.multi_actor": "P12",
        "query.ask_reva": "unscheduled",
    }
    for cid, phase in expected.items():
        c = caps.get(cid)
        assert c is not None, cid
        assert c.phase == phase
        assert c.status in ("reserved", "blocked")


def test_implemented_entries_point_to_existing_modules():
    for c in caps.catalogue():
        if c.status == "implemented":
            assert importlib.util.find_spec(c.owner_module) is not None, c.owner_module


def test_interfaces_importable():
    for c in caps.catalogue():
        if c.interface:
            importlib.import_module(c.interface)


def test_require_implemented_returns_capability():
    assert caps.require("fidelity.L1").capability_id == "fidelity.L1"


@pytest.mark.parametrize("cid", ["fidelity.L3", "fidelity.L4", "decision.search", "coupling.feedback"])
def test_require_raises_structured_gap(cid):
    with pytest.raises(CapabilityGap) as ei:
        caps.require(cid, request_digest="abc")
    err = ei.value
    assert err.details["capability"] == cid
    assert err.details["status"] == "reserved"
    assert err.details["kind"] == "capability_not_available"
    assert err.details["phase"]
    assert err.details["reason"]
    assert err.details["required_evidence"]
    assert err.details["context"] == {"request_digest": "abc"}
    problem = err.problem()
    assert problem["status"] == 422 and problem["code"] == "capability_gap"
    json.dumps(problem)


def test_require_unknown_capability():
    with pytest.raises(CapabilityGap) as ei:
        caps.require("teleportation.v1")
    assert ei.value.details["status"] == "unknown"


def test_not_available_typed_result():
    assert caps.not_available("fidelity.L2") is None
    gap = caps.not_available("fidelity.L4")
    assert gap.kind == "capability_not_available" and gap.status == "reserved"
    assert gap.as_dict()["capability_id"] == "fidelity.L4"


def test_sandbox_blocked_unless_configured(monkeypatch):
    assert caps.get("autonomy.external_sandbox_runner").status == "blocked"
    with pytest.raises(CapabilityGap) as ei:
        caps.require("autonomy.external_sandbox_runner")
    assert ei.value.details["status"] == "blocked"

    configured = SimpleNamespace(reva_sandbox_endpoint="https://runner.internal.example")
    c = caps.get("autonomy.external_sandbox_runner", configured)
    assert c.status == "reserved"  # configured is not the same as implemented
    with pytest.raises(CapabilityGap) as ei:
        caps.require("autonomy.external_sandbox_runner", configured)
    assert "runner.internal.example" not in json.dumps(ei.value.problem())

    monkeypatch.setenv(caps.SANDBOX_ENDPOINT_ENV, "https://runner.internal.example")
    assert caps.get("autonomy.external_sandbox_runner").status == "reserved"
    assert "runner.internal.example" not in json.dumps(caps.as_dict())


def test_capability_rejects_reserved_without_reason():
    with pytest.raises(ValueError):
        caps.Capability("x.y", "t", "reserved", "P9", "reva.x", ("gate",))
    with pytest.raises(ValueError):
        caps.Capability("x.y", "t", "implemented", "P99", "reva.x", ("gate",))


def test_ids_align_with_existing_gap_sites():
    """Capability ids raised elsewhere in the code base are declared in the catalogue."""
    for cid in ("fidelity.L3", "fidelity.L4", "coupling.feedback", "horizon.multi_period", "outputs.macroeconomic"):
        assert caps.get(cid) is not None
