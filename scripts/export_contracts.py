"""Export the HTTP contract deterministically: contracts/openapi.json.

Usage: uv run python scripts/export_contracts.py [--check]

The schema is generated from ``reva.api.app.create_app`` with fixed, secret-free
settings so the output never depends on the local environment. ``--check`` exits
non-zero when the committed file differs (for CI).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from reva.api.app import create_app  # noqa: E402
from reva.settings import Settings  # noqa: E402

OUT = ROOT / "contracts" / "openapi.json"


def render() -> str:
    settings = Settings(_env_file=None, reva_env="local")  # type: ignore[call-arg]
    schema = create_app(settings).openapi()
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    content = render()
    if "--check" in argv:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != content:
            print(f"{OUT.relative_to(ROOT)} is out of date; run scripts/export_contracts.py", file=sys.stderr)
            return 1
        print("contracts/openapi.json is up to date")
        return 0
    OUT.write_text(content, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(content)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
