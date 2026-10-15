"""Coupling (port compatibility, Tarjan SCC, feedback rejection) and named RNG streams."""

from __future__ import annotations

import numpy as np
import pytest

from reva.kernel.errors import CapabilityGap, IncompatibleSemantics
from reva.models.economic import crude_exposure as ce
from reva.simulation import rng
from reva.simulation.coupling import (
    CouplingGraph,
    PortSpec,
    check_compatible,
    feedback_components,
    reject_feedback,
    strongly_connected_components,
    topological_order,
)

M = PortSpec("M", "trade.hs2709.import_value.world_total", "unit.usd", "nominal_current_usd", "period_flow_total")


def test_port_compatibility():
    check_compatible(M, M)
    for change in (
        {"unit": "unit.kg"},
        {"basis": "real_2015_usd"},
        {"aggregation": "point_in_time_stock"},
        {"concept": "economics.gdp.nominal_current_usd"},
        {"temporal_support": "calendar_month"},
    ):
        other = PortSpec(**{**M.__dict__, **change})
        with pytest.raises(IncompatibleSemantics):
            check_compatible(M, other)


def test_tarjan_components():
    adj = {"a": ["b"], "b": ["c"], "c": ["a", "d"], "d": ["e"], "e": ["d"], "f": ["f"], "g": []}
    comps = {tuple(c) for c in strongly_connected_components(adj)}
    assert comps == {("a", "b", "c"), ("d", "e"), ("f",), ("g",)}
    assert {tuple(c) for c in feedback_components(adj)} == {("a", "b", "c"), ("d", "e"), ("f",)}
    with pytest.raises(CapabilityGap) as ei:
        reject_feedback(adj)
    assert ei.value.details["capability"] == "coupling.feedback"


def test_tarjan_deep_chain_is_iterative():
    n = 5000
    adj = {f"n{i:05d}": [f"n{i + 1:05d}"] for i in range(n)}
    assert len(strongly_connected_components(adj)) == n + 1
    adj[f"n{n:05d}"] = ["n00000"]
    assert len(feedback_components(adj)) == 1


def test_acyclic_graph_schedules_and_cycle_rejected():
    g = CouplingGraph()
    inputs = [PortSpec.from_dict(p) for p in ce.DESCRIPTOR["input_ports"]]
    outputs = [PortSpec.from_dict(p) for p in ce.DESCRIPTOR["output_ports"]]
    g.add_node("baseline", outputs=inputs)
    g.add_node("model", inputs=inputs, outputs=outputs)
    for p in inputs:
        g.connect("baseline", p.name, "model", p.name)
    assert g.schedule() == ["baseline", "model"]
    assert topological_order({"b": ["c"], "a": ["c"], "c": []}) == ["a", "b", "c"]
    # a second model fed back into the first forms a feedback loop -> rejected
    fb = PortSpec("fb", "x", "unit.usd", "nominal_current_usd", "period_flow_total")
    g2 = CouplingGraph()
    g2.add_node("A", inputs=[fb], outputs=[fb])
    g2.add_node("B", inputs=[fb], outputs=[fb])
    g2.connect("A", "fb", "B", "fb")
    g2.connect("B", "fb", "A", "fb")
    with pytest.raises(CapabilityGap):
        g2.schedule()


def test_unbound_or_double_owned_inputs_rejected():
    g = CouplingGraph()
    g.add_node("src", outputs=[M])
    g.add_node("src2", outputs=[M])
    g.add_node("model", inputs=[M, PortSpec(**{**M.__dict__, "name": "X"})])
    g.connect("src", "M", "model", "M")
    with pytest.raises(IncompatibleSemantics):
        g.connect("src2", "M", "model", "M")
    with pytest.raises(IncompatibleSemantics):
        g.schedule()


def test_named_streams_are_deterministic_and_order_independent():
    keys = [(1729, "economic.crude_exposure@1.0.0", e, d) for e in ("FRA", "CHN", "USA") for d in range(3)]
    first = {k: rng.stream(*k).random(4) for k in keys}
    second = {k: rng.stream(*k).random(4) for k in reversed(keys)}  # different "scheduling" order
    for k in keys:
        np.testing.assert_array_equal(first[k], second[k])
    flat = np.concatenate([first[k] for k in keys])
    assert len(np.unique(flat)) == len(flat)  # distinct streams
    assert not np.array_equal(rng.stream(1729, "m", "FRA", 0).random(4), rng.stream(1730, "m", "FRA", 0).random(4))
    assert isinstance(rng.stream(1, "m", "e", 0).bit_generator, np.random.Philox)
    assert rng.stream_key(1, "m", "e", 0) == rng.stream_key(1, "m", "e", 0)
    with pytest.raises(ValueError):
        rng.stream(-1, "m", "e", 0)


def test_seed_plan_records_streams():
    plan = rng.seed_plan(1729, "economic.crude_exposure@1.0.0", ["USA", "FRA"], 0)
    assert plan["random_draws_used"] == 0 and plan["generator"] == "numpy.random.Philox"
    assert [s["entity_id"] for s in plan["streams"]] == ["FRA", "USA"]
