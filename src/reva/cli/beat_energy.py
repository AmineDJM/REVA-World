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
    sha = hashlib.sha256(code).hexdigest()
    relocation: dict[str, Any] | None = None
    if sha != proto["code_sha256"]:
        rel_path = proto_path.with_name("code_relocation.yaml")
        rel = yaml.safe_load(rel_path.read_text(encoding="utf-8")) if rel_path.exists() else {}
        if not (rel.get("from_code_sha256") == proto["code_sha256"] and rel.get("to_code_sha256") == sha):
            raise SystemExit("beat_energy.py differs from the code pinned in the protocol: refusing to evaluate")
        relocation = rel
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
    out: dict[str, Any] = {
        "protocol_commit": tracked,
        "protocol_sha256": hashlib.sha256(proto_path.read_bytes()).hexdigest(),
    }
    if relocation is not None:
        out["code_relocation"] = relocation
    return out


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
        out = seal(eng, get_store(), origin=a.origin)
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=1, default=str)
        print(json.dumps(out, default=str)[:3000])
        return 0
    with eng.connect() as conn:
        res = load_and_run(conn, get_store(), origin=a.origin)
    res["protocol_check"] = pinned
    reloc = pinned.get("code_relocation")
    if reloc and res["digest"] != reloc["preregistered_run_digest"]:
        raise SystemExit(f"relocated code gives digest {res['digest']}, not the pre-registered run's: refusing")
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=1, default=str)
    if a.summary:
        with open(a.summary, "w", encoding="utf-8") as fh:
            json.dump({k: v for k, v in res.items() if k != "records"}, fh, indent=1, default=str)
    print(json.dumps({"digest": res["digest"], "family_size": res["family_size"], "wins": len(res["wins"])}))
    return 0


def load_and_run(conn: Any, store: Any, *, origin: str = "live") -> dict[str, Any]:
    """Database entry point of the pre-registered evaluation (moved verbatim from reva.evaluation.beat_energy)."""
    from datetime import UTC, date, datetime

    import polars as pl
    from sqlalchemy import text

    from reva.adapters.connectors.atlas_vintage_steo import PAGE_URL, parse_archive_page
    from reva.atlas.tables import read_atlas_table
    from reva.evaluation.energy_backtests import VINTAGE_TABLE, origins_from_cells, outcomes_from_frame
    from reva.evaluation.registry.data import latest_version
    from reva.evaluation.vintages import load_vintage_cells

    now = datetime.now(UTC)
    page = conn.execute(
        text(
            "SELECT raw_sha256 FROM evidence.acquisition WHERE origin = :o AND response_metadata->>'final_url' = :u "
            "ORDER BY retrieved_at DESC LIMIT 1"
        ),
        {"o": origin, "u": PAGE_URL},
    ).scalar_one()
    issues, _ = parse_archive_page(store.get("raw", page))
    release_of = {f"{i.year:04d}-{i.month:02d}": i.release for i in issues if i.release is not None}
    cells = load_vintage_cells(conn, store, origin=origin, dataset_keys=[VINTAGE_TABLE])
    w1_o = origins_from_cells(cells, codes=tuple(be.SPOT_SERIES))
    w2_o = origins_from_cells(cells, codes=tuple(be.QTY_COMPONENT))
    pin = latest_version(conn, "atlas.eia_steo.steo_observed", origin=origin, cutoff=now)
    spin = latest_version(conn, "atlas.eia.spot_d", origin=origin, cutoff=now)
    if pin is None or spin is None:
        raise LookupError("atlas.eia_steo.steo_observed / atlas.eia.spot_d not registered")
    outcomes = outcomes_from_frame(
        read_atlas_table(store, pin["artifact_sha256"]), codes=(*be.SPOT_SERIES, *be.QTY_COMPONENT)
    )
    spot = read_atlas_table(store, spin["artifact_sha256"])
    daily: dict[str, dict[date, float]] = {}
    for sid in be.SPOT_SERIES.values():
        f = spot.filter((pl.col("indicator") == f"EIA_SPOT|series={sid}") & pl.col("value").is_not_null())
        daily[sid] = {d: float(v) for d, v in f.select("period_start", "value").iter_rows()}
    res = be.run(w1_o, w2_o, release_of, daily, outcomes)
    res["inputs"] = {
        "steo_archive_page_raw_sha256": page,
        "vintage_table": VINTAGE_TABLE,
        "vintage_issues_w1": len(w1_o),
        "vintage_issues_w2": len(w2_o),
        "outcome_table": {k: pin[k] for k in ("dataset_key", "artifact_sha256", "recorded_at")},
        "daily_table": {k: spin[k] for k in ("dataset_key", "artifact_sha256", "recorded_at")},
        "daily_licence_review": "EIA spot prices are Refinitiv (LSEG) data republished by EIA (flags.licence_review)",
        "run_at": now.isoformat(),
    }
    return res


def seal(engine: Any, store: Any, *, origin: str = "live", actor: str = "agent:beat-energy") -> dict[str, Any]:
    from datetime import date, datetime

    from reva.evaluation.beat_energy_seal import MODELS, WIN_CODES, build_batch, seal_records

    """Pins tables at the seal instant, builds and stores the batch, records one issue row per model."""
    import polars as pl
    from sqlalchemy import text

    from reva.adapters.connectors.atlas_vintage_steo import PAGE_URL, parse_archive_page
    from reva.adapters.postgres import repositories as repo
    from reva.atlas.tables import read_atlas_table
    from reva.evaluation.energy_backtests import VINTAGE_TABLE, origins_from_cells
    from reva.evaluation.ledger import load_frozen_manifest
    from reva.evaluation.registry import periods as P
    from reva.evaluation.registry.data import latest_version
    from reva.evaluation.registry.questions import extract_series
    from reva.evaluation.registry.service import ISSUE_KIND, ISSUE_LOCK, SCOPE_KIND, _batches, _family_of, _insert_eval
    from reva.evaluation.vintages import cells_from_atlas_frame

    with engine.connect() as conn:
        now: datetime = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        cycle = P.cycle_of(now.date())
        done = conn.execute(
            text(
                "SELECT count(*) FROM model.evaluation WHERE metrics->>'kind' = :k AND metrics->>'label' LIKE 'V7 BEAT-ENERGY%' AND task_scope->>'cycle' = :c"
            ),
            {"k": ISSUE_KIND, "c": cycle},
        ).scalar_one()
        if done:
            return {"status": "already_sealed_this_cycle", "cycle": cycle}
        mv = {
            r["model_key"]: r["id"]
            for r in conn.execute(
                text("SELECT model_key, version, id::text AS id FROM model.version WHERE model_key = ANY(:k)"),
                {"k": sorted(MODELS)},
            ).mappings()
            if r["version"] == MODELS[r["model_key"]][0]
        }
        if set(mv) != set(MODELS):
            return {"status": "models_not_seeded", "missing": sorted(set(MODELS) - set(mv))}
        originals = [b for b in _batches(conn, origin) if b["cycle"] == cycle]
        original = None
        for b in originals:
            man = dict(load_frozen_manifest(store, b["sha"]))
            if not man.get("supplement_of") and all(f in man.get("families", {}) for f in WIN_CODES.values()):
                man["_sha256"] = b["sha"]
                original = man
        if original is None:
            return {"status": "no_original_batch_with_the_families", "cycle": cycle}
        page = conn.execute(
            text(
                "SELECT raw_sha256 FROM evidence.acquisition WHERE origin = :o AND response_metadata->>'final_url' = :u ORDER BY retrieved_at DESC LIMIT 1"
            ),
            {"o": origin, "u": PAGE_URL},
        ).scalar_one()
        pins = {}
        for k in ("atlas.eia_steo.steo_observed", VINTAGE_TABLE, "atlas.eia.spot_d"):
            p = latest_version(conn, k, origin=origin, cutoff=now)
            if p is None:
                return {"status": "table_missing", "table": k}
            pins[k] = p
    issues, _ = parse_archive_page(store.get("raw", page))
    release_of = {f"{i.year:04d}-{i.month:02d}": i.release for i in issues if i.release is not None}
    vpin = pins[VINTAGE_TABLE]
    cells = cells_from_atlas_frame(
        read_atlas_table(store, vpin["artifact_sha256"]), dataset_key=VINTAGE_TABLE,
        table_recorded_at=datetime.fromisoformat(str(vpin["recorded_at"])),
    )  # fmt: skip
    origins = origins_from_cells(cells, codes=tuple(be.SPOT_SERIES))
    spot = read_atlas_table(store, pins["atlas.eia.spot_d"]["artifact_sha256"])
    daily: dict[str, dict[date, float]] = {}
    for sid in be.SPOT_SERIES.values():
        f = spot.filter((pl.col("indicator") == f"EIA_SPOT|series={sid}") & pl.col("value").is_not_null())
        daily[sid] = {d: float(v) for d, v in f.select("period_start", "value").iter_rows()}
    recs = seal_records(origins, release_of, daily, now)
    vint = recs["_inforce"]["vintage"]
    steo_cells = {}
    for code in WIN_CODES:
        y, m = (int(x) for x in vint.split("-"))
        row = cells.filter(
            (pl.col("vintage") == vint) & (pl.col("variable") == f"eia_steo:{code}") & (pl.col("period_start") == date(y, m, 1))
        ).row(0, named=True)  # fmt: skip
        steo_cells[code] = {k: str(row[k]) for k in ("raw_sha256", "selector", "indicator", "value")} | {
            "table": {k: vpin[k] for k in ("dataset_key", "artifact_sha256", "recorded_at")}
        }
    obs = read_atlas_table(store, pins["atlas.eia_steo.steo_observed"]["artifact_sha256"])
    series_by_family = {fk: extract_series(obs, _family_of(original["families"][fk]))[0] for fk in WIN_CODES.values()}
    batch = build_batch(original, recs, series_by_family, pins, steo_cells, now=now, cycle=cycle)
    qids = sorted(q["question_id"] for q in batch["questions"])
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ISSUE_LOCK})
        sha = repo.store_json_artifact(
            conn, store, batch, "evaluation", schema_version="forecast-registry-batch/1",
            rights={"licence": "internal-derived; STEO values: U.S. public domain (EIA, attributed); daily spot "
                    "prices Refinitiv (LSEG) via EIA: licence_review"},
        )  # fmt: skip
        act = repo.simple_activity(
            "evaluation.forecast_registry_issue", "evaluation", actor,
            {"cycle": cycle, "origin": origin, "batch_sha256": sha, "supplement_of": original["_sha256"]},
        )  # fmt: skip
        repo.record_activity(conn, act, inputs=[(original["_sha256"], {"role": "original_batch"})], outputs=[sha])
        ctx = repo.create_context(conn, mode="evaluation", origin=origin, manifest_sha256=sha, knowledge_cutoff=now)
        scope = {
            "kind": SCOPE_KIND, "origin": origin, "cycle": cycle, "batch_sha256": sha,
            "supplement_of": original["_sha256"],
            "question_keys": sorted(q["question_key"] for q in batch["questions"]), "question_ids": qids,
        }  # fmt: skip
        for mk, (ver, _d) in MODELS.items():
            mine = [f for f in batch["forecasts"] if f["model"] == mk]
            _insert_eval(conn, mv[mk], ctx, sha, "operational_as_known", scope,
                         {"kind": ISSUE_KIND, "status": "sealed", "label": batch["label"], "model": f"{mk}@{ver}",
                          "issued_at": now.isoformat(), "batch_sha256": sha, "supplement_of": original["_sha256"],
                          "n_questions": len(qids), "n_issued": sum(1 for f in mine if f["status"] == "issued"),
                          "n_abstained": sum(1 for f in mine if f["status"] == "abstained")}, act.id)  # fmt: skip
    return {
        "status": "sealed",
        "batch_sha256": sha,
        "cycle": cycle,
        "issued_at": now.isoformat(),
        "questions": [(q["question_id"], q["family"], q["reference_period"]) for q in batch["questions"]],
        "forecasts": [(f["question_id"], f["model"], f.get("point"), f["status"]) for f in batch["forecasts"]],
    }


if __name__ == "__main__":
    raise SystemExit(main())
