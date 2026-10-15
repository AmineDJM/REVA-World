"""Invariant 29: no uncontrolled concurrency based on host ``os.cpu_count()``.

Static (AST) proof over the runtime code: nothing in ``src/reva`` or the process
start scripts calls ``os.cpu_count``/``os.process_cpu_count``/
``multiprocessing.cpu_count``/``os.sched_getaffinity``, and no executor or pool
is created without an explicit worker count (their defaults derive from the
host CPU count). Runtime concurrency is a bounded, validated setting.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RUNTIME = [*sorted((REPO / "src" / "reva").rglob("*.py")),
           *(REPO / "scripts" / n for n in ("start_api.py", "start_worker.py", "_runtime_env.py",
                                            "bootstrap_runtime.py"))]
HOST_CPU_CALLS = {"cpu_count", "process_cpu_count", "sched_getaffinity"}
POOLS = {"ThreadPoolExecutor": "max_workers", "ProcessPoolExecutor": "max_workers", "Pool": "processes",
         "ThreadPool": "processes"}


def _callee(node: ast.Call) -> str | None:
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return None


def _scan() -> list[str]:
    offenders: list[str] = []
    for path in RUNTIME:
        if not path.exists() or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(REPO).as_posix()
        tree = ast.parse(path.read_text(), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and any(a.name in HOST_CPU_CALLS for a in node.names):
                offenders.append(f"{rel}:{node.lineno} imports a host CPU-count function")
            if not isinstance(node, ast.Call):
                continue
            name = _callee(node)
            if name in HOST_CPU_CALLS:
                offenders.append(f"{rel}:{node.lineno} calls {name}()")
            if name in POOLS:
                kw = {k.arg for k in node.keywords}
                if POOLS[name] not in kw and not node.args:
                    offenders.append(f"{rel}:{node.lineno} {name}() without an explicit {POOLS[name]}")
    return offenders


def test_runtime_code_never_sizes_concurrency_from_the_host_cpu_count() -> None:
    assert len(RUNTIME) > 50  # the scan really covers the package
    offenders = _scan()
    assert not offenders, "\n".join(offenders)


def test_scan_detects_a_planted_violation(tmp_path, monkeypatch) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("import os\nfrom concurrent.futures import ThreadPoolExecutor\n"
                   "n = os.cpu_count()\nex = ThreadPoolExecutor()\n")
    monkeypatch.setattr(f"{__name__}.RUNTIME", [bad])
    monkeypatch.setattr(f"{__name__}.REPO", tmp_path)
    found = _scan()
    assert any("cpu_count" in f for f in found) and any("ThreadPoolExecutor" in f for f in found), found


def test_worker_concurrency_is_an_explicit_bounded_setting() -> None:
    from reva.settings import Settings

    field = Settings.model_fields["reva_worker_concurrency"]
    assert field.default == 1
    bounds = {type(m).__name__: m for m in field.metadata}
    assert bounds["Ge"].ge == 1 and bounds["Le"].le <= 8, field.metadata
