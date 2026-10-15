"""Invariant 11: no model execution fetching live inputs.

Model execution reads pinned inputs from a published belief release; it never
holds an HTTP client. Proven three ways:

* the *transitive* import graph (grimp, the engine behind import-linter) of every
  model-executing package contains no HTTP library, connector, HTTP adapter or
  ingestion module;
* the import-linter contracts that encode this boundary are present and kept;
* a real L1 and L2 simulation job succeeds with every network path disabled, and
  the handler contract (``JobContext``) carries no HTTP client.
"""

from __future__ import annotations

import dataclasses
import os
import socket
import subprocess
import sys
import tomllib
import typing
from pathlib import Path
from typing import Any

import grimp
import pytest
from sqlalchemy import text

from ._helpers import claim_simulation_job, create_scenario, scenario_spec

REPO = Path(__file__).resolve().parents[2]

NETWORK_MODULES = (
    "httpx",
    "httpcore",
    "requests",
    "urllib3",
    "aiohttp",
    "boto3",
    "botocore",
    "reva.adapters.http",
    "reva.adapters.connectors",
    "reva.evidence.runner",
    "reva.evidence.ingest",
    "reva.autonomy",
    "reva.api",
)
MODEL_EXECUTION = ("reva.models", "reva.simulation", "reva.evaluation", "reva.compression", "reva.extensions")


@pytest.fixture(scope="module")
def graph() -> grimp.ImportGraph:
    return grimp.build_graph("reva", include_external_packages=True)


def _violations(graph: grimp.ImportGraph, package: str) -> list[tuple[str, ...]]:
    mods = {m for m in graph.modules if m == package or m.startswith(package + ".")}
    upstream: set[str] = set()
    for m in mods:
        upstream |= graph.find_upstream_modules(m)
    bad = sorted(u for u in upstream for n in NETWORK_MODULES if u == n or u.startswith(n + "."))
    chains: list[tuple[str, ...]] = []
    for b in bad:
        for m in sorted(mods):
            chain = graph.find_shortest_chain(importer=m, imported=b)
            if chain:
                chains.append(tuple(chain))
                break
    return chains


@pytest.mark.parametrize("package", MODEL_EXECUTION)
def test_model_execution_cannot_reach_the_network_through_imports(graph, package: str) -> None:
    chains = _violations(graph, package)
    assert not chains, f"{package} transitively imports network/ingest code:\n" + "\n".join(map(" -> ".join, chains))


@pytest.mark.xfail(
    strict=False,
    reason=(
        "owner: lead (src/reva/population/service.py). handle_population imports reva.cli.seed.entity_id, and "
        "reva.cli.seed imports the connector registry -> SafeHttpClient -> httpx. No fetch happens, but the import "
        "boundary is open; fix: move entity_id() to a pure module (e.g. reva.kernel.ids) and import it from there."
    ),
)
def test_population_job_cannot_reach_the_network_through_imports(graph) -> None:
    chains = _violations(graph, "reva.population")
    assert not chains, "reva.population transitively imports network code:\n" + "\n".join(map(" -> ".join, chains))


def test_import_linter_contracts_encode_the_boundary_and_are_kept() -> None:
    cfg = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["importlinter"]
    forbidden = {
        tuple(c["source_modules"]): set(c["forbidden_modules"]) for c in cfg["contracts"] if c["type"] == "forbidden"
    }
    assert {"httpx", "reva.adapters"} <= forbidden[("reva.models",)]
    assert {"httpx", "reva.adapters.connectors"} <= forbidden[("reva.simulation",)]

    exe = Path(sys.executable).parent / "lint-imports"
    if not exe.exists():
        pytest.skip("lint-imports is not installed in this environment (dev dependency group)")
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    proc = subprocess.run(
        [str(exe), "--config", str(REPO / "pyproject.toml"), "--no-cache"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
    assert "numerical models have no HTTP, DB or adapters KEPT" in proc.stdout
    assert "simulation does not write evidence or beliefs KEPT" in proc.stdout


def test_handler_contract_carries_no_http_client() -> None:
    from reva.operations.context import JobContext
    from reva.simulation.jobs import handle_simulation

    hints = typing.get_type_hints(JobContext)
    for f in dataclasses.fields(JobContext):
        rendered = repr(hints.get(f.name, f.type)).lower()
        assert "http" not in rendered and "client" not in rendered, (f.name, rendered)
    params = list(typing.get_type_hints(handle_simulation))
    assert params[:2] == ["payload", "ctx"], params


class NetworkUsed(AssertionError):
    pass


@pytest.mark.db
@pytest.mark.parametrize("fidelity", ["L1", "L2"])
def test_simulation_succeeds_with_all_network_paths_disabled(db, fixture_release, monkeypatch, fidelity) -> None:
    import httpx

    from reva.adapters.http.safe_client import SafeHttpClient
    from reva.simulation.jobs import handle_simulation

    over: dict[str, Any] = {"title": f"Invariant 11: offline {fidelity}", "fidelity": fidelity}
    if fidelity == "L2":
        over["assumptions"] = {
            "quantity_response": "elasticity_sensitivity",
            "no_general_equilibrium": True,
            "elasticity_grid": [{"import_elasticity": -0.3, "export_elasticity": 0.2, "basis": "scenario_assumption"}],
        }
    sid = create_scenario(db, scenario_spec(fixture_release, **over))
    payload = {"scenario_id": str(sid), "fidelity": fidelity}
    ctx = claim_simulation_job(db, payload)

    calls: list[str] = []

    def refuse(name: str):
        def _refuse(*a: Any, **k: Any) -> Any:
            calls.append(name)
            raise NetworkUsed(f"model execution attempted network access via {name}")

        return _refuse

    real_getaddrinfo = socket.getaddrinfo

    def local_only_getaddrinfo(host: Any, *a: Any, **k: Any) -> Any:
        if host not in ("localhost", "127.0.0.1", "::1", None) and not str(host).startswith("/"):
            calls.append(f"getaddrinfo:{host}")
            raise NetworkUsed(f"name resolution of {host!r} during model execution")
        return real_getaddrinfo(host, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", local_only_getaddrinfo)
    monkeypatch.setattr(httpx.Client, "send", refuse("httpx.Client.send"))
    monkeypatch.setattr(httpx.AsyncClient, "send", refuse("httpx.AsyncClient.send"))
    monkeypatch.setattr(SafeHttpClient, "get", refuse("SafeHttpClient.get"))

    ref = handle_simulation(payload, ctx)
    assert not calls, calls
    assert ref["status"] == "succeeded", ref
    with db.owner.connect() as c:
        n = c.execute(text("SELECT count(*) FROM sim.result WHERE run_id = :r"), {"r": ref["run_id"]}).scalar_one()
        inputs = c.execute(text("SELECT baseline_release_id FROM sim.scenario WHERE id = :s"), {"s": sid}).scalar_one()
    assert n > 0
    assert str(inputs) == str(fixture_release["release_id"])  # inputs come from the pinned release only
