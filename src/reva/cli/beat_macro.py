"""BEAT-MACRO CLI (V7): development stage, holdout (opened once), prospective issuance of confirmed scopes.

Reads the dev DB read-only (API credentials): the vintage atlas tables and today's IMF CPI / Eurostat HICP / EIA
spot tables (pinned by artifact sha256 in every manifest).

    uv run python -m reva.cli.beat_macro dev        # -> development.json + hypotheses.yaml (commit them!)
    uv run python -m reva.cli.beat_macro holdout    # refuses unless hypotheses.yaml is committed; opens ONCE
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from typing import Any

import typer
import yaml

from reva.evaluation.beat_macro import inputs as I
from reva.evaluation.beat_macro import service as S

app = typer.Typer(add_completion=False, help=__doc__)
LAGGED = ("atlas.imf.cpi_m", "atlas.eurostat.hicp_m", "atlas.eia.spot_d")


def load_inputs() -> S.Inputs:
    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres.engine import make_engine
    from reva.evaluation.registry.data import FrameCache, latest_version
    from reva.evaluation.vintages import load_vintage_cells
    from reva.settings import get_settings

    eng = make_engine("api", get_settings())
    store = get_store()
    with eng.connect() as conn:
        cells = load_vintage_cells(conn, store)
        now = datetime.now(UTC)
        pins = {k: latest_version(conn, k, origin="live", cutoff=now) for k in LAGGED}
    fc = FrameCache(store, size=1)
    frames: dict[str, Any] = {}
    for k, p in pins.items():
        if p is None:
            raise typer.BadParameter(f"{k} has no live table version")
        frames[k] = (fc.get(p["artifact_sha256"]), datetime.fromisoformat(p["recorded_at"]))
    pin_doc = {k: {x: p[x] for x in ("dataset_key", "artifact_sha256", "recorded_at", "row_count")}
               for k, p in pins.items() if p}  # fmt: skip
    pin_doc["vintage_cells"] = {"rows": cells.height}
    return S.Inputs(
        cells=cells,
        cpi=I.imf_cpi(*frames["atlas.imf.cpi_m"]),
        hicp=I.eurostat_hicp(*frames["atlas.eurostat.hicp_m"]),
        oil=I.brent_monthly(*frames["atlas.eia.spot_d"]),
        pins=pin_doc,
    )


def _git(*args: str) -> str:
    git = shutil.which("git") or "/usr/bin/git"
    return subprocess.run([git, *args], cwd=S.REGISTRY_DIR, check=True, capture_output=True, text=True).stdout


def _committed(path: str) -> bool:
    try:
        _git("ls-files", "--error-unmatch", path)
    except subprocess.CalledProcessError:
        return False
    return not _git("status", "--porcelain", "--", path).strip()


@app.command("dev")
def dev_cmd() -> None:
    """Development stage (origins before 2019) -> development.json and hypotheses.yaml."""
    if not _committed("protocol.yaml"):
        raise SystemExit("protocol.yaml must be committed before any window is opened")
    proto_sha = S.canonical_sha256(yaml.safe_load((S.REGISTRY_DIR / "protocol.yaml").read_text()))
    doc = S.development(load_inputs())
    dsha = S.write_json(S.REGISTRY_DIR / "development.json", doc)
    hyps = S.select_hypotheses(doc)
    hyp_doc = {
        "schema": "reva-beat-macro-hypotheses/1",
        "written_at": datetime.now(UTC).isoformat(),
        "statement": "Written by the development stage from protocol.yaml's selection rule; committed before the "
        "holdout is opened. Only these hypotheses can be confirmed (one Holm over all of them).",
        "protocol_sha256": proto_sha,
        "development_json_sha256": dsha,
        "code_sha256": doc["code_sha256"],
        "model_version": S.MODEL_VERSION,
        "family_size": len(hyps),
        "hypotheses": hyps,
    }
    (S.REGISTRY_DIR / "hypotheses.yaml").write_text(yaml.safe_dump(hyp_doc, sort_keys=False))
    typer.echo(yaml.safe_dump({"n_cases": doc["n_cases"], "family_size": len(hyps),
                               "cpi_revision_check": doc["cpi_revision_check"]}, sort_keys=False))  # fmt: skip


@app.command("holdout")
def holdout_cmd() -> None:
    """Opens the holdout ONCE (refuses unless hypotheses.yaml is committed and unchanged)."""
    if not _committed("hypotheses.yaml") or not _committed("protocol.yaml"):
        raise SystemExit("hypotheses.yaml and protocol.yaml must be committed before the holdout is opened")
    hyp_doc = yaml.safe_load((S.REGISTRY_DIR / "hypotheses.yaml").read_text())
    ledger_path = S.REGISTRY_DIR / "holdout_ledger.yaml"
    sha = S.check_unopened(hyp_doc, S.load_yaml(ledger_path))
    opened = datetime.now(UTC).isoformat()
    S.append_ledger(ledger_path, {"hypotheses_sha256": sha, "opened_at": opened, "code_sha256": S.code_sha256(),
                                  "status": "opening"})  # fmt: skip
    doc = S.holdout(load_inputs(), hyp_doc)
    doc["opened_at"] = opened
    hsha = S.write_json(S.REGISTRY_DIR / "holdout.json", doc)
    S.append_ledger(ledger_path, {"hypotheses_sha256": sha, "opened_at": opened, "holdout_json_sha256": hsha,
                                  "confirmed": [S.hyp_key(r) for r in doc["results"] if r["confirmed"]],
                                  "status": "scored"})  # fmt: skip
    for r in doc["results"]:
        typer.echo(f"{S.hyp_key(r)}  rel={r['relative']:.3f} p={r['p_better']:.2g} holm={r['holm_rejected']} "
                   f"cov80={r['cov80_candidate']} confirmed={r['confirmed']}")  # fmt: skip


@app.command("issue")
def issue_cmd(
    dry_run: bool = typer.Option(True, "--dry-run/--seal", help="--seal writes to the dev DB (use flock)"),
) -> None:
    """Seals supplementary registry batches for the confirmed scopes on the current cycle's questions."""
    from sqlalchemy import text

    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres import repositories as repo
    from reva.adapters.postgres.engine import make_engine
    from reva.evaluation.beat_macro import seal as SE
    from reva.evaluation.ledger import load_frozen_manifest
    from reva.evaluation.registry import periods as P
    from reva.evaluation.registry.service import ISSUE_KIND, ISSUE_LOCK, SCOPE_KIND, _batches, _insert_eval
    from reva.settings import get_settings

    scopes = SE.confirmed_scopes()
    if not scopes:
        typer.echo("no confirmed scope: nothing is sealed")
        return
    store = get_store()
    eng = make_engine("worker" if not dry_run else "api", get_settings())
    with eng.connect() as conn:
        issued_at: datetime = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        cycle = P.cycle_of(issued_at.date())
        originals = [b for b in _batches(conn, "live") if b["cycle"] == cycle]
        mv = {
            r["model_key"]: r["id"]
            for r in conn.execute(
                text("SELECT model_key, version, id::text AS id FROM model.version WHERE model_key = ANY(:k)"),
                {"k": [SE.SCOPES[s][2] for s in scopes]},
            ).mappings()
            if r["version"] == S.MODEL_VERSION
        }
    inp = load_inputs()
    now_cases = SE.issue_time_cases(inp, issued_at)
    for sk in scopes:
        family, hz, mk = SE.SCOPES[sk]
        for b in originals:
            man = dict(load_frozen_manifest(store, b["sha"]))
            if man.get("supplement_of"):
                continue
            man["_sha256"] = b["sha"]
            sup = SE.supplement(man, now_cases, issued_at=issued_at, model_key=mk, family=family, horizon=hz)
            if sup is None:
                continue
            for f in sup["forecasts"]:
                q = next(x for x in sup["questions"] if x["question_id"] == f["question_id"])
                typer.echo(f"{q['geo']} {q['reference_period']} {f['status']} point={f.get('point')} "
                           f"q10={f.get('quantiles', {}).get('0.1')} q90={f.get('quantiles', {}).get('0.9')} "
                           f"inputs={f.get('inputs', {}).get('imf_weo_projection')}/{f.get('inputs', {}).get('reva_nowcast_bridge')}"
                           f" lambda={f.get('inputs', {}).get('stack_lambda')}")  # fmt: skip
            if dry_run:
                continue
            if mk not in mv:
                raise SystemExit(f"{mk}@{S.MODEL_VERSION} not seeded: run `reva seed` under flock first")
            qids = sorted(q["question_id"] for q in sup["questions"])
            with eng.begin() as conn:
                conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ISSUE_LOCK})
                sha = repo.store_json_artifact(conn, store, sup, "evaluation", schema_version="forecast-registry-batch/1",
                                               rights={"licence": "internal-derived; WEO values: IMF terms (attribution); OECD CC BY 4.0"})  # fmt: skip
                act = repo.simple_activity(
                    "evaluation.forecast_registry_issue", "evaluation", "agent:beat-macro",
                    {"cycle": cycle, "origin": "live", "batch_sha256": sha, "supplement_of": b["sha"]},
                )  # fmt: skip
                repo.record_activity(conn, act, inputs=[(b["sha"], {"role": "original_batch"})], outputs=[sha])
                ctx = repo.create_context(conn, mode="evaluation", origin="live", manifest_sha256=sha,
                                          knowledge_cutoff=issued_at)  # fmt: skip
                scope = {"kind": SCOPE_KIND, "origin": "live", "cycle": cycle, "batch_sha256": sha,
                         "supplement_of": b["sha"], "question_keys": sorted(q["question_key"] for q in sup["questions"]),
                         "question_ids": qids}  # fmt: skip
                _insert_eval(conn, mv[mk], ctx, sha, "operational_as_known", scope,
                             {"kind": ISSUE_KIND, "status": "sealed", "label": sup["label"], "model": f"{mk}@{S.MODEL_VERSION}",
                              "issued_at": issued_at.isoformat(), "batch_sha256": sha, "supplement_of": b["sha"],
                              "n_questions": len(qids),
                              "n_issued": sum(1 for f in sup["forecasts"] if f["status"] == "issued"),
                              "n_abstained": sum(1 for f in sup["forecasts"] if f["status"] == "abstained")}, act.id)  # fmt: skip
            typer.echo(f"sealed supplementary batch {sha} (supplement of {b['sha'][:12]}), {len(qids)} questions")


if __name__ == "__main__":
    app()
