"""Validates tests/invariants/coverage.yaml, the §24.1 invariant coverage map.

* exactly the 35 invariants, each once, with text, a known status and the fields
  that status requires (owner/gaps for partial, owner/reason for unproven, reason
  for not-applicable);
* every cited test node id exists: the file exists and defines that test function
  (top level or in a ``Test*`` class; parametrized ids are cited without their
  ``[...]`` suffix), resolved by parsing the file, so a renamed or deleted test
  breaks this check;
* every ``xfail`` entry is really marked xfail in its file, and never also listed
  as a proving test;
* every enforcement point exists: the file exists and contains the cited symbol;
* every test in ``tests/invariants/test_invariant_*.py`` is cited by some invariant;
* the data constitution lists the same 35 invariants.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
MAP = Path(__file__).with_name("coverage.yaml")
STATUSES = {"proven", "partially_proven", "unproven", "not_applicable_v1"}
REQUIRED = {
    "proven": (),
    "partially_proven": ("owner", "gaps"),
    "unproven": ("owner", "reason"),
    "not_applicable_v1": ("reason",),
}


@cache
def _doc() -> dict[str, Any]:
    return yaml.safe_load(MAP.read_text())


def _entries() -> list[dict[str, Any]]:
    return list(_doc()["invariants"])


@cache
def _defined_tests(rel: str) -> frozenset[str]:
    """``name`` and ``Class::name`` for every test function defined in ``rel``."""
    path = REPO / rel
    tree = ast.parse(path.read_text(), filename=rel)
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test"):
            out.add(node.name)
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef) and sub.name.startswith("test"):
                    out.add(f"{node.name}::{sub.name}")
    return frozenset(out)


def _node_problem(node_id: str) -> str | None:
    if "[" in node_id:
        return f"{node_id}: cite parametrized tests without the [param] suffix"
    rel, sep, name = node_id.partition("::")
    if not sep or not name:
        return f"{node_id}: not a pytest node id (path::test_name)"
    if not rel.startswith("tests/") or not rel.endswith(".py"):
        return f"{node_id}: node ids must point at a tests/**/*.py file"
    if not (REPO / rel).is_file():
        return f"{node_id}: file {rel} does not exist"
    if name not in _defined_tests(rel):
        return f"{node_id}: {rel} defines no test {name!r}"
    return None


def _xfail_marked(node_id: str) -> bool:
    rel, _, name = node_id.partition("::")
    tree = ast.parse((REPO / rel).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name.split("::")[-1]:
            return any("xfail" in ast.unparse(d) for d in node.decorator_list)
    return False


# --------------------------------------------------------------------------- structure


def test_header_and_exactly_35_invariants() -> None:
    doc = _doc()
    assert doc["schema"] == "reva-invariant-coverage/1"
    ids = [e["id"] for e in _entries()]
    assert sorted(ids) == list(range(1, 36)), ids
    dup = [i for i, n in Counter(ids).items() if n > 1]
    assert not dup, dup


@pytest.mark.parametrize("entry", _entries(), ids=lambda e: f"inv{e['id']:02d}")
def test_entry_is_complete_for_its_status(entry: dict[str, Any]) -> None:
    assert isinstance(entry.get("text"), str) and entry["text"].startswith("No "), entry.get("text")
    status = entry.get("status")
    assert status in STATUSES, status
    for key in REQUIRED[status]:
        assert isinstance(entry.get(key), str) and entry[key].strip(), f"inv {entry['id']}: {status} needs {key}"
    assert entry.get("enforcement"), f"inv {entry['id']}: no enforcement point"
    tests = entry.get("tests") or []
    if status in ("proven", "partially_proven"):
        assert tests, f"inv {entry['id']}: {status} without a proving test"
        offline = [t for t in tests if not t.startswith("tests/live/")]
        assert offline, f"inv {entry['id']}: only live tests; proof must run offline"
    if status == "unproven":
        assert not tests, "an unproven invariant cannot cite proving tests"
    assert len(tests) == len(set(tests)), "duplicate test ids"


# --------------------------------------------------------------------------- tests exist


@pytest.mark.parametrize("entry", _entries(), ids=lambda e: f"inv{e['id']:02d}")
def test_every_cited_test_node_exists(entry: dict[str, Any]) -> None:
    problems = [p for t in [*(entry.get("tests") or []), *(entry.get("xfail") or [])] if (p := _node_problem(t))]
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("entry", [e for e in _entries() if e.get("xfail")], ids=lambda e: f"inv{e['id']:02d}")
def test_xfail_entries_are_marked_and_not_counted_as_proof(entry: dict[str, Any]) -> None:
    assert entry["status"] != "proven", "an invariant with an expected failure is not proven"
    for t in entry["xfail"]:
        assert t not in (entry.get("tests") or []), t
        assert _xfail_marked(t), f"{t} is cited as xfail but carries no xfail marker"


def test_every_invariant_test_is_cited() -> None:
    cited = {t for e in _entries() for t in [*(e.get("tests") or []), *(e.get("xfail") or [])]}
    here = Path(__file__).parent
    missing = []
    for path in sorted(here.glob("test_invariant_*.py")):
        rel = path.relative_to(REPO).as_posix()
        missing += [
            f"{rel}::{n}"
            for n in sorted(_defined_tests(rel))
            if f"{rel}::{n}" not in cited and "positive_control" not in n
        ]
    assert not missing, "invariant tests not cited in coverage.yaml:\n" + "\n".join(missing)


# --------------------------------------------------------------------------- enforcement exists


@pytest.mark.parametrize("entry", _entries(), ids=lambda e: f"inv{e['id']:02d}")
def test_every_enforcement_point_exists(entry: dict[str, Any]) -> None:
    problems = []
    for ep in entry["enforcement"]:
        assert set(ep) >= {"file", "symbol", "kind"}, ep
        path = REPO / ep["file"]
        if not path.is_file():
            problems.append(f"{ep['file']}: missing")
        elif str(ep["symbol"]) not in path.read_text(errors="replace"):
            problems.append(f"{ep['file']}: symbol {ep['symbol']!r} not found")
    assert not problems, "\n".join(problems)


# --------------------------------------------------------------------------- constitution agrees


def test_data_constitution_lists_the_same_35_invariants() -> None:
    text = (REPO / "docs" / "data_constitution.md").read_text()
    rows = {int(m.group(1)) for m in re.finditer(r"^\|\s*(\d{1,2})\s*\|\s*No ", text, re.MULTILINE)}
    assert rows == set(range(1, 36)), sorted(set(range(1, 36)) ^ rows)


# --------------------------------------------------------------------------- self-checks of the validator


def test_validator_rejects_a_nonexistent_node() -> None:
    assert _node_problem("tests/invariants/test_coverage_map.py::test_does_not_exist")
    assert _node_problem("tests/nowhere/test_x.py::test_y")
    assert _node_problem("tests/invariants/test_coverage_map.py::test_header_and_exactly_35_invariants[x]")
    assert _node_problem("tests/invariants/test_coverage_map.py::test_header_and_exactly_35_invariants") is None


def test_status_counts_are_reported() -> None:
    counts = Counter(e["status"] for e in _entries())
    assert sum(counts.values()) == 35
    print("invariant coverage:", dict(sorted(counts.items())))
