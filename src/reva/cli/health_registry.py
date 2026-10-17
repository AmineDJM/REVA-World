"""CLI of the health forecast registry (worker credentials; writes under ``flock /tmp/reva-devdb.lock``).

uv run python -m reva.cli.health_registry backtest [--family K ...] [--origin live]
uv run python -m reva.cli.health_registry issue    [--family K ...]
uv run python -m reva.cli.health_registry resolve
uv run python -m reva.cli.health_registry status
"""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace

from reva.evaluation.health_registry.service import handle_health_registry


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m reva.cli.health_registry")
    ap.add_argument("action", choices=["backtest", "issue", "resolve", "status"])
    ap.add_argument("--origin", default="live", choices=["live", "test_fixture"])
    ap.add_argument("--family", action="append", default=[])
    a = ap.parse_args(argv)
    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres.engine import make_engine
    from reva.settings import get_settings

    ctx = SimpleNamespace(worker_engine=make_engine("worker", get_settings()), store=get_store())
    payload = {"action": a.action, "origin": a.origin, **({"families": a.family} if a.family else {})}
    print(json.dumps(handle_health_registry(payload, ctx), indent=2, default=str))


if __name__ == "__main__":
    main()
