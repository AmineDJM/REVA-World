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


if __name__ == "__main__":
    app()
