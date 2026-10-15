#!/usr/bin/env python3
"""The e2e stack with the *recorded* LLM drafter and the V2 fixtures (thin wrapper).

Equivalent to ``uv run python scripts/e2e_stack.py --recorded-drafter --v2-fixtures ...``: the
capabilities this wrapper used to patch in now live in scripts/e2e_stack.py as options (see its
docstring). ``--no-v2-fixtures`` keeps only the recorded drafter. No live LLM and no live provider
is ever called. Every other argument is passed through.

Usage (from the repository root)::

    uv run python ui/e2e/recorded_llm_stack.py --stack-file <scratch>/stack.json --static-dir <scratch>/ui-build
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def _load_stack_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("reva_e2e_stack", ROOT / "scripts" / "e2e_stack.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reva_e2e_stack"] = mod
    spec.loader.exec_module(mod)
    return mod


def stack_args(argv: list[str]) -> list[str]:
    args = [a for a in argv if a != "--no-v2-fixtures"]
    extra = ["--recorded-drafter"] if "--recorded-drafter" not in args else []
    if "--no-v2-fixtures" not in argv and "--v2-fixtures" not in args:
        extra.append("--v2-fixtures")
    return extra + args


if __name__ == "__main__":
    sys.exit(_load_stack_module().main(stack_args(sys.argv[1:])))
