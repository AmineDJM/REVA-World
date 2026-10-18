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
    ap.add_argument("--w3", action="store_true", help="evaluate W3 (needs atlas.eia_wpsr; w3_addendum.yaml)")
    a = ap.parse_args(argv)
    pinned = check_protocol(Path.cwd())
    eng = make_engine("api", get_settings())
    if a.seal:
        out = seal(make_engine("worker", get_settings()), get_store(), origin=a.origin)  # writer role
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
    if a.w3:
        res = run_w3(eng, get_store(), res, origin=a.origin) | {"protocol_check": pinned}
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


def run_w3(engine: Any, store: Any, base: dict[str, Any], *, origin: str = "live") -> dict[str, Any]:
    """W3 on the pinned WPSR table; the protocol's Holm family recomputed with W3's evaluated p-values."""
    from datetime import UTC, datetime

    from sqlalchemy import text

    from reva.adapters.connectors.atlas_vintage_steo import PAGE_URL, parse_archive_page
    from reva.atlas.tables import read_atlas_table
    from reva.evaluation import beat_energy_w3 as w3
    from reva.evaluation.energy_backtests import VINTAGE_TABLE, origins_from_cells, outcomes_from_frame
    from reva.evaluation.registry.data import latest_version
    from reva.evaluation.vintages import load_vintage_cells

    proto = yaml.safe_load(Path(be.PROTOCOL_PATH).with_name("w3_addendum.yaml").read_text(encoding="utf-8"))
    code = Path("src/reva/evaluation/beat_energy_w3.py").read_bytes()
    if hashlib.sha256(code).hexdigest() != proto["code_sha256"]:
        raise SystemExit("beat_energy_w3.py differs from the code pinned in w3_addendum.yaml: refusing")
    now = datetime.now(UTC)
    with engine.connect() as conn:
        page = conn.execute(
            text(
                "SELECT raw_sha256 FROM evidence.acquisition WHERE origin = :o AND "
                "response_metadata->>'final_url' = :u ORDER BY retrieved_at DESC LIMIT 1"
            ),
            {"o": origin, "u": PAGE_URL},
        ).scalar_one()
        cells = load_vintage_cells(conn, store, origin=origin, dataset_keys=[VINTAGE_TABLE])
        wpin = latest_version(conn, "atlas.eia.wpsr_w", origin=origin, cutoff=now)
        opin = latest_version(conn, "atlas.eia_steo.steo_observed", origin=origin, cutoff=now)
    if wpin is None or opin is None:
        raise SystemExit("atlas.eia.wpsr_w / steo_observed not registered")
    issues, _ = parse_archive_page(store.get("raw", page))
    release_of = {f"{i.year:04d}-{i.month:02d}": i.release for i in issues if i.release is not None}
    origins = origins_from_cells(cells, codes=tuple(w3.WPSR_SERIES))
    outcomes = outcomes_from_frame(read_atlas_table(store, opin["artifact_sha256"]), codes=tuple(w3.WPSR_SERIES))
    weekly = w3.weekly_from_frame(read_atlas_table(store, wpin["artifact_sha256"]))
    recs = w3.w3_records(origins, release_of, weekly, outcomes)
    tests = w3.w3_tests(recs)
    fam = w3.merged_family(base["tests"], tests)
    return {
        "label": "v7_beat_energy_w3",
        "base_digest": base["digest"],
        "w3_tests": tests,
        "family_size": fam["family_size"],
        "tests": fam["tests"],
        "wins": fam["wins"],
        "n_w3_records": len(recs),
        "digest": be.digest({"base": base["digest"], "w3": tests}),
        "inputs": {
            "wpsr_table": {k: wpin[k] for k in ("dataset_key", "artifact_sha256", "recorded_at")},
            "outcome_table": {k: opin[k] for k in ("dataset_key", "artifact_sha256", "recorded_at")},
            "run_at": now.isoformat(),
        },
        "records": recs,
    }


def seal(engine: Any, store: Any, *, origin: str = "live", actor: str = "agent:beat-energy") -> dict[str, Any]:
    """Seal the W1 winners on existing registry questions (supplementary batch), or return the unsealed nowcast
    with the reason when no registry question asks the STEO issue month or the timing is outside the window."""
    from datetime import date, datetime

    import polars as pl
    from sqlalchemy import text

    from reva.adapters.connectors.atlas_vintage_steo import PAGE_URL, parse_archive_page
    from reva.adapters.postgres import repositories as repo
    from reva.atlas.tables import read_atlas_table
    from reva.evaluation.beat_energy_seal import MODELS, WIN_CODES, seal_records
    from reva.evaluation.energy_backtests import VINTAGE_TABLE, origins_from_cells
    from reva.evaluation.registry.data import latest_version
    from reva.evaluation.registry.service import ISSUE_KIND, ISSUE_LOCK, SCOPE_KIND, _insert_eval
    from reva.evaluation.vintages import cells_from_atlas_frame
    from reva.models.forecasting.energy_models import Pred

    with engine.connect() as conn:
        now: datetime = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
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
        page = conn.execute(
            text(
                "SELECT raw_sha256 FROM evidence.acquisition WHERE origin = :o AND "
                "response_metadata->>'final_url' = :u ORDER BY retrieved_at DESC LIMIT 1"
            ),
            {"o": origin, "u": PAGE_URL},
        ).scalar_one()
        pins = {}
        for k in (VINTAGE_TABLE, "atlas.eia.spot_d"):
            p = latest_version(conn, k, origin=origin, cutoff=now)
            if p is None:
                return {"status": "table_missing", "table": k}
            pins[k] = {x: p[x] for x in ("dataset_key", "artifact_sha256", "recorded_at")}
    issues, _ = parse_archive_page(store.get("raw", page))
    release_of = {f"{i.year:04d}-{i.month:02d}": i.release for i in issues if i.release is not None}
    vpin = pins[VINTAGE_TABLE]
    cells = cells_from_atlas_frame(
        read_atlas_table(store, vpin["artifact_sha256"]),
        dataset_key=VINTAGE_TABLE,
        table_recorded_at=datetime.fromisoformat(str(vpin["recorded_at"])),
    )
    origins = origins_from_cells(cells, codes=tuple(be.SPOT_SERIES))
    spot = read_atlas_table(store, pins["atlas.eia.spot_d"]["artifact_sha256"])
    daily: dict[str, dict[date, float]] = {}
    for sid in be.SPOT_SERIES.values():
        f = spot.filter((pl.col("indicator") == f"EIA_SPOT|series={sid}") & pl.col("value").is_not_null())
        daily[sid] = {d: float(v) for d, v in f.select("period_start", "value").iter_rows()}
    try:
        recs = seal_records(origins, release_of, daily, now)
    except ValueError as e:
        return {"status": "outside_registered_window", "reason": str(e), "now": now.isoformat()}
    recs["_pins"] = pins
    vint = recs["_inforce"]["vintage"]
    y, m = (int(x) for x in vint.split("-"))
    steo_cells = {}
    for code in WIN_CODES:
        row = cells.filter(
            (pl.col("vintage") == vint)
            & (pl.col("variable") == f"eia_steo:{code}")
            & (pl.col("period_start") == date(y, m, 1))
        ).row(0, named=True)
        steo_cells[code] = {k: str(row[k]) for k in ("raw_sha256", "selector", "indicator", "value")} | {"table": vpin}
    nowcast = {
        c: {k: (v.__dict__ if isinstance(v, Pred) else v) for k, v in r.items()}
        for c, r in recs.items()
        if c in WIN_CODES
    }
    import shutil

    from reva.evaluation.beat_energy_seal import NOWCAST_FAMILIES, REDISTRIBUTION, nowcast_batch, nowcast_questions
    from reva.evaluation.registry import periods as P
    from reva.evaluation.registry.questions import extract_series
    from reva.evaluation.registry.service import _code_digest, _frozen_family
    from reva.evaluation.registry.specs import load_specs

    specs = load_specs()
    fams = {f.key: f for f in specs.families if f.key in NOWCAST_FAMILIES.values()}
    if len(fams) != len(NOWCAST_FAMILIES):
        return {"status": "nowcast_families_missing", "nowcast": nowcast}
    fam_docs = {k: _frozen_family(f) for k, f in fams.items()}
    with engine.connect() as conn:
        opin = latest_version(conn, "atlas.eia_steo.steo_observed", origin=origin, cutoff=now)
    if opin is None:
        return {"status": "table_missing", "table": "atlas.eia_steo.steo_observed"}
    obs = read_atlas_table(store, opin["artifact_sha256"])
    series_by_family = {k: extract_series(obs, f, opin.get("recorded_at"))[0] for k, f in fams.items()}
    cycle = P.cycle_of(now.date())
    try:
        questions = nowcast_questions(fam_docs, series_by_family, dict(opin), now=now, cycle=cycle)
    except (LookupError, ValueError) as e:
        return {"status": "no_nowcast_question", "reason": str(e), "nowcast": nowcast}
    tables = {"atlas.eia_steo.steo_observed": dict(opin), **pins}
    batch = nowcast_batch(fam_docs, questions, recs, steo_cells, now=now, cycle=cycle,
                          specs={"version": specs.version, "sha256": specs.sha256, "interval_level": specs.interval_level},
                          tables=tables, code_digest=_code_digest())  # fmt: skip
    qids = sorted(q["question_id"] for q in batch["questions"])
    free_gb = shutil.disk_usage("/").free / 2**30  # GiB, as `df -h` reports
    if free_gb < 5.0:
        return {"status": "queued_low_disk", "free_gb": round(free_gb, 2), "question_ids": qids, "nowcast": nowcast}
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ISSUE_LOCK})
        dup = conn.execute(
            text(
                "SELECT count(*) FROM model.evaluation WHERE metrics->>'kind' = :k AND metrics->>'label' LIKE "
                "'V7 BEAT-ENERGY%' AND task_scope->'question_ids' ?| :q"
            ),
            {"k": ISSUE_KIND, "q": qids},
        ).scalar_one()
        if dup:
            return {"status": "already_sealed", "question_ids": qids}
        sha = repo.store_json_artifact(
            conn, store, batch, "evaluation", schema_version="forecast-registry-batch/1",
            rights={"licence": "internal-derived; STEO values: U.S. public domain (EIA, attributed); daily spot "
                    "prices Refinitiv (LSEG) via EIA: licence_review", "redistribution": REDISTRIBUTION},
        )  # fmt: skip
        act = repo.simple_activity(
            "evaluation.forecast_registry_issue", "evaluation", actor,
            {"cycle": cycle, "origin": origin, "batch_sha256": sha},
        )  # fmt: skip
        repo.record_activity(conn, act, inputs=[], outputs=[sha])
        ctx = repo.create_context(conn, mode="evaluation", origin=origin, manifest_sha256=sha, knowledge_cutoff=now)
        scope = {
            "kind": SCOPE_KIND, "origin": origin, "cycle": cycle, "batch_sha256": sha,
            "question_keys": sorted(q["question_key"] for q in batch["questions"]), "question_ids": qids,
        }  # fmt: skip
        for mk, (ver, _d) in MODELS.items():
            mine = [f for f in batch["forecasts"] if f["model"] == mk]
            _insert_eval(conn, mv[mk], ctx, sha, "operational_as_known", scope,
                         {"kind": ISSUE_KIND, "status": "sealed", "label": batch["label"], "model": f"{mk}@{ver}",
                          "issued_at": now.isoformat(), "batch_sha256": sha, "n_questions": len(qids),
                          "n_issued": sum(1 for f in mine if f["status"] == "issued"),
                          "n_abstained": sum(1 for f in mine if f["status"] == "abstained"),
                          "redistribution": REDISTRIBUTION}, act.id)  # fmt: skip
    return {"status": "sealed", "issued_at": now.isoformat(), "batch_sha256": sha, "question_ids": qids,
            "free_gb_before_write": round(free_gb, 2), "nowcast": nowcast}  # fmt: skip


if __name__ == "__main__":
    raise SystemExit(main())
