#!/usr/bin/env python3
"""Enforce the architecture's dependency boundaries with import-linter.

Contracts live in pyproject.toml ([tool.importlinter]). This wrapper makes the
package importable from a checkout, runs ``lint-imports`` and propagates its
exit status, so CI and ``make lint`` use one command.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    exe = shutil.which("lint-imports")
    if exe is None:
        print("lint-imports not found; install the dev dependency group (uv sync --frozen)", file=sys.stderr)
        return 2
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src") + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return subprocess.run([exe, "--config", str(ROOT / "pyproject.toml")], cwd=ROOT, env=env,
                          check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
