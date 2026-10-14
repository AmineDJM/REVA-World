"""Model coupling: port compatibility and strongly-connected-component detection.

V1 supports acyclic one-way coupling only. Any feedback component (a strongly
connected component with more than one node, or a self-loop) is rejected: it
would need an explicit solver configuration (step size, convergence criterion,
damping, iteration cap, conservative exchange, error bounds) that does not exist
yet, and the last iterate must never be published as an equilibrium.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from reva.kernel.errors import CapabilityGap, IncompatibleSemantics


@dataclass(frozen=True)
class PortSpec:
    """What a value on a port means. Stock/flow/rate is carried by ``aggregation``."""

    name: str
    concept: str
    unit: str
    basis: str
    aggregation: str
    temporal_support: str = "calendar_year"

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> PortSpec:
        return cls(
            name=str(d["name"]),
            concept=str(d["concept"]),
            unit=str(d["unit"]),
            basis=str(d["basis"]),
            aggregation=str(d["aggregation"]),
            temporal_support=str(d.get("temporal_support", "calendar_year")),
        )


def check_compatible(source: PortSpec, target: PortSpec) -> None:
    """Raise IncompatibleSemantics unless ``source`` may feed ``target`` without a transform.

    No implicit conversions: annual totals never feed monthly ports, nominal never
    feeds real, stocks never feed flows. Such links need a versioned operator.
    """
    mismatches = [
        f"{attr}: {getattr(source, attr)!r} -> {getattr(target, attr)!r}"
        for attr in ("concept", "unit", "basis", "aggregation", "temporal_support")
        if getattr(source, attr) != getattr(target, attr)
    ]
    if mismatches:
        raise IncompatibleSemantics(
            f"port {source.name} cannot feed {target.name} without an approved transform operator",
            mismatches=mismatches,
        )


@dataclass(frozen=True)
class Edge:
    source_node: str
    source_port: str
    target_node: str
    target_port: str


@dataclass
class CouplingGraph:
    nodes: dict[str, dict[str, dict[str, PortSpec]]] = field(default_factory=dict)  # node -> {in,out} -> ports
    edges: list[Edge] = field(default_factory=list)

    def add_node(self, node: str, inputs: Iterable[PortSpec] = (), outputs: Iterable[PortSpec] = ()) -> None:
        if node in self.nodes:
            raise ValueError(f"duplicate node {node}")
        self.nodes[node] = {"in": {p.name: p for p in inputs}, "out": {p.name: p for p in outputs}}

    def connect(self, source_node: str, source_port: str, target_node: str, target_port: str) -> None:
        try:
            src = self.nodes[source_node]["out"][source_port]
            dst = self.nodes[target_node]["in"][target_port]
        except KeyError as e:
            raise IncompatibleSemantics(f"unknown port {e.args[0]!r}") from e
        check_compatible(src, dst)
        if any(e.target_node == target_node and e.target_port == target_port for e in self.edges):
            raise IncompatibleSemantics(f"input {target_node}.{target_port} already has an owning producer")
        self.edges.append(Edge(source_node, source_port, target_node, target_port))

    def adjacency(self) -> dict[str, list[str]]:
        adj: dict[str, list[str]] = {n: [] for n in self.nodes}
        for e in self.edges:
            if e.target_node not in adj[e.source_node]:
                adj[e.source_node].append(e.target_node)
        return adj

    def unbound_inputs(self) -> list[str]:
        bound = {(e.target_node, e.target_port) for e in self.edges}
        return sorted(f"{n}.{p}" for n, ports in self.nodes.items() for p in ports["in"] if (n, p) not in bound)

    def schedule(self) -> list[str]:
        """Deterministic topological order; rejects feedback and unbound inputs."""
        reject_feedback(self.adjacency())
        missing = self.unbound_inputs()
        if missing:
            raise IncompatibleSemantics(f"unbound model inputs: {missing}", unbound=missing)
        return topological_order(self.adjacency())


def strongly_connected_components(adj: Mapping[str, Iterable[str]]) -> list[list[str]]:
    """Tarjan's algorithm (iterative, deterministic order). Returns components in reverse topological order."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    counter = 0
    nodes = sorted(set(adj) | {w for vs in adj.values() for w in vs})
    succ = {v: sorted(adj.get(v, ())) for v in nodes}
    for root in nodes:
        if root in index:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            v, i = work.pop()
            if i == 0:
                index[v] = low[v] = counter
                counter += 1
                stack.append(v)
                on_stack.add(v)
            recurse = False
            children = succ[v]
            while i < len(children):
                w = children[i]
                i += 1
                if w not in index:
                    work.append((v, i))
                    work.append((w, 0))
                    recurse = True
                    break
                if w in on_stack:
                    low[v] = min(low[v], index[w])
            if recurse:
                continue
            if low[v] == index[v]:
                comp: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                out.append(sorted(comp))
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
    return out


def feedback_components(adj: Mapping[str, Iterable[str]]) -> list[list[str]]:
    comps = strongly_connected_components(adj)
    return [c for c in comps if len(c) > 1 or c[0] in set(adj.get(c[0], ()))]


def reject_feedback(adj: Mapping[str, Iterable[str]]) -> None:
    cycles = feedback_components(adj)
    if cycles:
        raise CapabilityGap(
            "feedback coupling is unsupported without a convergence contract (step size, convergence "
            "criterion, damping, iteration cap, conservative exchange, error bounds); V1 executes acyclic "
            "one-way coupling only",
            capability="coupling.feedback",
            components=cycles,
        )


def topological_order(adj: Mapping[str, Iterable[str]]) -> list[str]:
    nodes = sorted(set(adj) | {w for vs in adj.values() for w in vs})
    indeg = {n: 0 for n in nodes}
    for v in nodes:
        for w in set(adj.get(v, ())):
            indeg[w] += 1
    ready = sorted(n for n in nodes if indeg[n] == 0)
    order: list[str] = []
    while ready:
        v = ready.pop(0)
        order.append(v)
        for w in sorted(set(adj.get(v, ()))):
            indeg[w] -= 1
            if indeg[w] == 0:
                ready.append(w)
                ready.sort()
    if len(order) != len(nodes):
        raise CapabilityGap("graph contains a cycle", capability="coupling.feedback")
    return order
