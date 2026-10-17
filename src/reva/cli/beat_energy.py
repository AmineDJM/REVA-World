"""CLI of the V7 BEAT-ENERGY pre-registered evaluation: ``python -m reva.cli.beat_energy``.

Refuses to run unless ``registry/forecasts/beat_energy/protocol.yaml`` is committed and pins the sha256 of
:mod:`reva.evaluation.beat_energy` (the code the protocol registered). Kept out of the evaluation package so it never
reaches the object-store factory (invariant 11).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from reva.evaluation import beat_energy as be


def check_protocol(root: Path) -> dict[str, Any]:
    proto_path = root / be.PROTOCOL_PATH
    proto = yaml.safe_load(proto_path.read_text(encoding="utf-8"))
    code = (root / "src/reva/evaluation/beat_energy.py").read_bytes()
    if hashlib.sha256(code).hexdigest() != proto["code_sha256"]:
        raise SystemExit("beat_energy.py differs from the code pinned in the protocol: refusing to evaluate")
    git = shutil.which("git")
    if git is None:
        raise SystemExit("git not found: cannot prove the protocol is committed")
    tracked = subprocess.run(
        [git, "log", "-1", "--format=%H", "--", be.PROTOCOL_PATH], cwd=root, capture_output=True, text=True
    ).stdout.strip()
    if not tracked:
        raise SystemExit("protocol is not committed: refusing to evaluate")
    if proto["family_size"] != len(be.declared_tests()):
        raise SystemExit("declared family size differs from the protocol")
    return {"protocol_commit": tracked, "protocol_sha256": hashlib.sha256(proto_path.read_bytes()).hexdigest()}


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres.engine import make_engine
    from reva.settings import get_settings

    ap = argparse.ArgumentParser(description="V7 BEAT-ENERGY pre-registered evaluation")
    ap.add_argument("--origin", default="live")
    ap.add_argument("--out", required=True, help="full JSON (records included)")
    ap.add_argument("--summary", help="slim JSON without records (committed next to the report)")
    ap.add_argument("--seal", action="store_true", help="seal the protocol's winners (run under flock)")
    a = ap.parse_args(argv)
    pinned = check_protocol(Path.cwd())
    eng = make_engine("api", get_settings())
    if a.seal:
        from reva.evaluation.beat_energy_seal import seal

        out = seal(eng, get_store(), origin=a.origin)
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=1, default=str)
        print(json.dumps(out, default=str)[:3000])
        return 0
    with eng.connect() as conn:
        res = be.load_and_run(conn, get_store(), origin=a.origin)
    res["protocol_check"] = pinned
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=1, default=str)
    if a.summary:
        with open(a.summary, "w", encoding="utf-8") as fh:
            json.dump({k: v for k, v in res.items() if k != "records"}, fh, indent=1, default=str)
    print(json.dumps({"digest": res["digest"], "family_size": res["family_size"], "wins": len(res["wins"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
