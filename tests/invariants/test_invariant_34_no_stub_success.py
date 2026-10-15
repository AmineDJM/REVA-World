"""Invariant 34: no production success status from a stub or unavailable capability.

* every registered job type resolves to a real handler (not a ``pass``/constant
  stub); an unregistered or unimportable handler is a typed, non-retryable
  ``handler_unavailable`` failure, never a success;
* every capability that is not implemented raises a structured
  ``CapabilityGap`` from ``require`` and has a typed ``CapabilityNotAvailable``
  result: there is no code path that returns an output for it;
* no runtime module raises ``NotImplementedError`` as a placeholder.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

from reva import capabilities
from reva.kernel.errors import CapabilityGap
from reva.operations.jobs import HANDLERS, HandlerUnavailable, classify_exception, resolve_handler

SRC = Path(__file__).resolve().parents[2] / "src" / "reva"


def _is_stub(fn) -> bool:
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    body = tree.body[0].body  # type: ignore[attr-defined]
    stmts = [s for s in body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
    if not stmts:
        return True
    if len(stmts) == 1 and isinstance(stmts[0], ast.Pass):
        return True
    if len(stmts) == 1 and isinstance(stmts[0], ast.Return):
        v = stmts[0].value
        return (
            v is None
            or isinstance(v, ast.Constant)
            or (isinstance(v, ast.Dict) and all(isinstance(x, ast.Constant) for x in v.values))
        )
    return False


@pytest.mark.parametrize("job_type", sorted(HANDLERS))
def test_every_registered_job_type_resolves_to_a_real_handler(job_type: str) -> None:
    fn = resolve_handler(job_type)
    assert callable(fn)
    assert not _is_stub(fn), f"{job_type} handler {HANDLERS[job_type]} is a stub"
    params = list(inspect.signature(fn).parameters)
    assert params[:2] == ["payload", "ctx"], params


def test_stub_detector_positive_control() -> None:
    def stub(payload, ctx):
        return {"status": "succeeded"}

    assert _is_stub(stub)


def test_unknown_or_missing_handler_is_a_non_retryable_typed_failure(monkeypatch) -> None:
    with pytest.raises(HandlerUnavailable) as ei:
        resolve_handler("no_such_job_type")
    assert classify_exception(ei.value) == ("handler_unavailable", False)

    monkeypatch.setitem(HANDLERS, "ghost", "reva.operations.does_not_exist:handle")
    with pytest.raises(HandlerUnavailable):
        resolve_handler("ghost")
    monkeypatch.setitem(HANDLERS, "ghost", "reva.operations.jobs:does_not_exist")
    with pytest.raises(HandlerUnavailable):
        resolve_handler("ghost")


def test_every_unavailable_capability_raises_a_typed_gap() -> None:
    unavailable = [c for c in capabilities.catalogue() if not c.available]
    assert unavailable, "the catalogue lists no reserved/blocked capability"
    for cap in unavailable:
        gap = capabilities.not_available(cap.capability_id)
        assert gap is not None and gap.kind == "capability_not_available" and gap.reason
        with pytest.raises(CapabilityGap) as ei:
            capabilities.require(cap.capability_id)
        assert ei.value.details["status"] in ("reserved", "blocked")
    with pytest.raises(CapabilityGap):
        capabilities.require("no.such.capability")


def test_no_runtime_module_raises_not_implemented_as_a_placeholder() -> None:
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Raise) and node.exc is not None:
                exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
                if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
                    offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert not offenders, offenders
