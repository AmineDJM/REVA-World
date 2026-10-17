"""FIX4B (audit AUDIT3 M3 + L3): new revisions of the 5 REVA event-study estimates from their archived run documents.

Append-only: ``reva.effects.load_effect_estimates`` writes a new row per changed estimate (same key, new
content); nothing is edited or deleted. The run documents are read back from the object store (namespace
``evaluation``) by the sha256 recorded in each estimate's load; the estimation is not re-run (numbers unchanged).
Changes: cumulative-growth LHS written on the level variable (``reva.estimation.run.effect_target``) and
``shock_profile`` set. Usage: ``flock /tmp/reva-devdb.lock uv run python scripts/fix4b/rewrite_eventstudy_estimates.py
[--write]`` (without ``--write``: validate and print only).
"""

from __future__ import annotations

import json
import sys
from typing import Any

from sqlalchemy import text

from reva.adapters.objects.factory import get_store
from reva.adapters.postgres.engine import make_engine
from reva.effects import validate_estimate
from reva.estimation.run import SOURCE_KEY, build_effect_inputs, write_effect_estimates


def decode(obj: Any) -> Any:
    """Inverse of ``reva.kernel.ids`` canonical tags (floats as floats, dates kept as ISO text)."""
    if isinstance(obj, dict):
        if len(obj) == 1:
            ((k, v),) = obj.items()
            if k in ("$float", "$decimal"):
                return float(v)
            if k in ("$date", "$datetime"):
                return str(v)
        return {k: decode(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [decode(v) for v in obj]
    return obj


def main(write: bool) -> int:
    eng, store = make_engine("worker"), get_store()
    with eng.connect() as c:
        loads = c.execute(
            text(
                "SELECT DISTINCT ON (dataset) dataset, raw_sha256s[1] AS sha FROM kg.effect_load "
                "WHERE source_key = :k AND origin = 'live' ORDER BY dataset, id"
            ),
            {"k": SOURCE_KEY},
        ).all()
    for ds, sha in loads:
        doc = decode(json.loads(store.get("evaluation", sha)))
        for e in build_effect_inputs(doc):
            row = validate_estimate(e, source_kind="reva_run", source_key=SOURCE_KEY, raws={sha})
            print(ds, row["estimate_key"], e.effect_kind, e.effect_id, e.effect_transform, e.shock_profile)
        if write:
            print("  ->", write_effect_estimates(eng, store, doc, sha, origin="live"))
    return 0


if __name__ == "__main__":
    sys.exit(main("--write" in sys.argv))
