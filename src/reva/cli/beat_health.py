"""V7 BEAT-HEALTH evaluation CLI (read-only on the database; writes a JSON result file).

uv run python -m reva.cli.beat_health dev       # rounds already used by the V6 back-test (development, not evidence)
uv run python -m reva.cli.beat_health holdout   # pre-registered holdout; refused before the protocol's date or if
                                                # the frozen module changed

Protocol: ``registry/forecasts/beat_health/protocol.yaml``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

from reva.evaluation.health_registry import beat as B
from reva.ontology.registry import REGISTRY_ROOT

DIR = REGISTRY_ROOT / "forecasts" / "beat_health"
PROTOCOL = DIR / "protocol.yaml"
FAMILIES = ("nhsn.flu_hosp", "nhsn.covid_hosp")


def module_sha256() -> str:
    return hashlib.sha256(Path(B.__file__).read_bytes()).hexdigest()


def load_family(key: str) -> tuple[B.Panel, dict[str, Any], dict[str, Any]]:
    """As-of panel of one hub family from the latest live table versions (read-only)."""
    from sqlalchemy import text

    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres.engine import make_engine
    from reva.evaluation.health_registry import core as C
    from reva.evaluation.health_registry import service as S
    from reva.evaluation.registry.data import FrameCache, latest_version
    from reva.models.forecasting import health_quantile as HQ

    eng = make_engine("worker")
    fam = C.load_specs().family(key)
    with eng.connect() as conn:
        now = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        pins = {k: latest_version(conn, k, origin="live", cutoff=now) for k in S._tables(fam)}
    if any(p is None for p in pins.values()):
        raise SystemExit(f"{key}: missing table {sorted(k for k, p in pins.items() if p is None)}")
    cache = FrameCache(get_store(), size=3)
    fr = {k: cache.get(p["artifact_sha256"]) for k, p in pins.items() if p}
    series, _, _ = C.extract_series(fr[fam.dataset_key], fam)
    vint = C.extract_vintages(fr[str(fam.backtest["vintages_dataset_key"])], fam)
    assert fam.reference is not None
    ens = C.extract_ensemble(fr[str(fam.reference["dataset_key"])], fam)
    lag = int(fam.backtest.get("vintage_to_reference_days", 7))
    rows: list[dict[str, Any]] = []
    skipped: list[str] = []
    for ref in sorted({k[0] for k in ens}):
        rd = date.fromisoformat(ref)
        v = date.fromordinal(rd.toordinal() - lag)
        if v not in vint:
            skipped.append(ref)
            continue
        hist = C.history_as_of(vint, v)
        r_idx = C.week_idx(date.fromordinal(rd.toordinal() - 6), "sunday")
        for geo in sorted(hist):
            known = hist[geo]
            for h in fam.horizons:
                t = r_idx + h
                e = ens.get((ref, geo, t))
                if e is None or not known or t <= known[-1][0]:
                    continue
                cell = series.get(geo, {}).get(t)
                res = C.run_models(fam, known, t)["health.blend"]
                rows.append(
                    {
                        "ref": ref,
                        "geo": geo,
                        "h": h,
                        "target": t,
                        "history": known,
                        "truth": None if cell is None else float(cell.value),
                        "ens": [float(x) for x in e[0]],
                        "blend": None if isinstance(res, HQ.Abstain) else list(res.values),
                    }
                )
    meta = {
        "pins": {k: S._short_pin(p) for k, p in pins.items() if p},
        "rounds_without_vintage": skipped,
        "evaluated_at": now.isoformat(),
    }
    return B.build_panel(rows, vint), meta, dict(fam.raw)


def evaluate(which: str) -> dict[str, Any]:
    proto = yaml.safe_load(PROTOCOL.read_text(encoding="utf-8"))
    sha = module_sha256()
    if which == "holdout":
        if sha != proto["frozen_code"]["module_sha256"]:
            raise SystemExit("frozen module changed since pre-registration: holdout evaluation refused")
        not_before = date.fromisoformat(str(proto["holdouts"]["H1"]["evaluate_not_before"]))
        if datetime.now(UTC).date() < not_before:
            raise SystemExit(f"holdout H1 may be evaluated once, on or after {not_before}")
    alpha = float(proto["decision_rule"]["alpha"])
    start = str(proto["holdouts"]["H1"]["first_reference_date"])
    results: dict[str, Any] = {}
    tests: dict[str, Any] = {}
    for key in FAMILIES:
        p, meta, _ = load_family(key)
        fc, chosen = B.run_candidates(p)
        rounds = {r for r in p.refs if (r < start if which == "dev" else r >= start)}
        cmp = {
            t: B.compare(p, {**fc, "reva.blend_alone": p.blend}, t, rounds) for t in B.TRUTHS
        }
        results[key] = {**meta, "comparisons": cmp, "chosen_parameters": chosen}
        for c in B.CANDIDATES:
            tests[f"{which}|{key}|{c}"] = cmp[str(proto["decision_rule"]["primary_truth"])]["models"][c]
    return {
        "schema": "reva-beat-health-result/1",
        "set": which,
        "protocol_version": proto["version"],
        "module_sha256": sha,
        "label": "DEVELOPMENT: rounds already used by the V6 back-test; selection data, not evidence"
        if which == "dev"
        else "PRE-REGISTERED HOLDOUT H1",
        "families": results,
        "verdicts": B.verdicts(tests, alpha),
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m reva.cli.beat_health")
    ap.add_argument("set", choices=["dev", "holdout"])
    a = ap.parse_args(argv)
    res = evaluate(a.set)
    out = DIR / f"{a.set}_result.json"
    out.write_text(json.dumps(res, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(json.dumps(res["verdicts"], indent=1))


if __name__ == "__main__":
    main()
