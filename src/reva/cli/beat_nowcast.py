"""BEAT-NOWCAST CLI (V7): development stage, holdout (opened once), descriptive peek of the inputs.

Inputs: the ``atlas.fed_nowcasts`` tables — from the dev DB (``--source db``, after ``reva atlas ingest
atlas.fed_nowcasts``) or, when the live ingest is blocked (disk < 5 GB), parsed by the same connector parsers from
the raw files in ``--raw-dir`` (``--source files``; every file's sha256 is pinned in the result) — plus the dev
DB's ``vintage_us.rtdsm`` (CPI levels) and ``eia.spot_d`` (gasoline spot) tables, read-only (API role).

    uv run python -m reva.cli.beat_nowcast dev --source files --raw-dir <dir>      # -> development.json + hypotheses.yaml
    uv run python -m reva.cli.beat_nowcast holdout --source files --raw-dir <dir>  # refuses unless committed; ONCE
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
import typer
import yaml

from reva.evaluation.beat_nowcast import service as S

app = typer.Typer(add_completion=False, help=__doc__)
RAW_FILES = {
    "gdpnow": ["gdpnow.xlsx"],
    "nyfed": ["nyfed_2016.xlsx", "nyfed_2023.xlsx"],
    "cleveland": ["clev_month.json", "clev_quarter.json"],
    "rtdsm_monthly": ["employMvMd.xlsx", "iptMvMd.xlsx", "hMvMd.xlsx", "hstartsMvMd.xlsx", "rconmMvMd.xlsx"],
    "release_values": ["routput_first_second_third.xlsx"],
}
FED_KEYS = [f"atlas.fed_nowcasts.{d}" for d in RAW_FILES]


def _frames_from_files(raw_dir: Path) -> tuple[pl.DataFrame, dict[str, Any]]:
    from reva.adapters.connectors import atlas_fed_nowcasts as F
    from reva.adapters.connectors.vintage_common import Rows
    from reva.evaluation.vintages import cells_from_atlas_frame

    frames, pins = [], {}
    for ds, files in RAW_FILES.items():
        rows = Rows()
        for f in files:
            body = (raw_dir / f).read_bytes()
            sha = hashlib.sha256(body).hexdigest()
            pins[f] = {"sha256": sha, "bytes": len(body), "url": (raw_dir / f"{f}.url").read_text().strip()
                       if (raw_dir / f"{f}.url").exists() else None}  # fmt: skip
            if ds == "gdpnow":
                F.parse_gdpnow(body, raw_sha256=sha, rows=rows)
            elif ds == "nyfed":
                F.parse_nyfed(body, edition="2016" if "2016" in f else "2023", raw_sha256=sha, rows=rows)
            elif ds == "cleveland":
                F.parse_cleveland(body, freq="month" if "month" in f else "quarter", raw_sha256=sha, rows=rows)
            elif ds == "rtdsm_monthly":
                F.parse_rtdsm_monthly(body, file=f, raw_sha256=sha, rows=rows)
            else:
                F.parse_release_values(body, raw_sha256=sha, rows=rows)
        frames.append(
            cells_from_atlas_frame(rows.frame(), dataset_key=f"atlas.fed_nowcasts.{ds}", table_recorded_at=None)
        )
    return pl.concat(frames, how="vertical"), {
        "source": "raw files parsed by atlas_fed_nowcasts " + F.VERSION,
        "files": pins,
    }


def load_inputs(source: str, raw_dir: Path | None) -> Any:
    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres.engine import make_engine
    from reva.evaluation.registry.data import FrameCache, latest_version
    from reva.evaluation.vintages import cells_from_atlas_frame, load_vintage_cells
    from reva.settings import get_settings

    eng = make_engine("api", get_settings())
    store = get_store()
    fc = FrameCache(store, size=1)
    now = datetime.now(UTC)
    pins: dict[str, Any] = {}
    with eng.connect() as conn:
        if source == "db":
            cells = load_vintage_cells(conn, store, dataset_keys=FED_KEYS)
            pins["fed_nowcasts"] = {"source": "dev DB atlas tables", "keys": FED_KEYS, "rows": cells.height}
        else:
            if raw_dir is None:
                raise typer.BadParameter("--raw-dir is required with --source files")
            cells, pins["fed_nowcasts"] = _frames_from_files(raw_dir)
        p_rt = latest_version(conn, "atlas.vintage_us.rtdsm", origin="live", cutoff=now)
        p_sp = latest_version(conn, "atlas.eia.spot_d", origin="live", cutoff=now)
    if p_rt is None or p_sp is None:
        raise typer.BadParameter("vintage_us.rtdsm / eia.spot_d have no live table version")
    rt = cells_from_atlas_frame(fc.get(p_rt["artifact_sha256"]), dataset_key="atlas.vintage_us.rtdsm",
                                table_recorded_at=datetime.fromisoformat(str(p_rt["recorded_at"])))  # fmt: skip
    rt = rt.filter(pl.col("provider_variable") == "CPI")
    spot = fc.get(p_sp["artifact_sha256"])
    for k, p in (("atlas.vintage_us.rtdsm", p_rt), ("atlas.eia.spot_d", p_sp)):
        pins[k] = {x: str(p[x]) for x in ("dataset_key", "artifact_sha256", "recorded_at", "row_count")}
    return S.inputs_from_cells(cells, rt, spot, pins)


def _git(*args: str) -> str:
    git = shutil.which("git") or "/usr/bin/git"
    return subprocess.run([git, *args], cwd=S.REGISTRY_DIR, check=True, capture_output=True, text=True).stdout


def _committed(path: str) -> bool:
    try:
        _git("ls-files", "--error-unmatch", path)
    except subprocess.CalledProcessError:
        return False
    return not _git("status", "--porcelain", "--", path).strip()


def _protocol_ok() -> str:
    if not _committed("protocol.yaml"):
        raise SystemExit("protocol.yaml must be committed before any evaluation window is opened")
    proto = yaml.safe_load((S.REGISTRY_DIR / "protocol.yaml").read_text())
    if proto.get("code_sha256") != S.code_sha256():
        raise SystemExit(f"code digest {S.code_sha256()} differs from the protocol's {proto.get('code_sha256')}")
    return S.canonical_sha256(proto)


@app.command("dev")
def dev_cmd(source: str = typer.Option("db"), raw_dir: Path | None = typer.Option(None)) -> None:
    """Development stage (targets 2014-2021) -> development.json and hypotheses.yaml."""
    proto_sha = _protocol_ok()
    doc = S.development(load_inputs(source, raw_dir))
    dsha = S.write_json(S.REGISTRY_DIR / "development.json", doc)
    hyps = S.select_hypotheses(doc)
    hyp_doc = {
        "schema": "reva-beat-nowcast-hypotheses/1",
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
    typer.echo(yaml.safe_dump({"family_size": len(hyps), "hypotheses": hyps, "build": doc["build"]}, sort_keys=False))


@app.command("holdout")
def holdout_cmd(source: str = typer.Option("db"), raw_dir: Path | None = typer.Option(None)) -> None:
    """Opens the holdout ONCE (refuses unless protocol.yaml and hypotheses.yaml are committed and unchanged)."""
    _protocol_ok()
    if not _committed("hypotheses.yaml"):
        raise SystemExit("hypotheses.yaml must be committed before the holdout is opened")
    hyp_doc = yaml.safe_load((S.REGISTRY_DIR / "hypotheses.yaml").read_text())
    ledger = S.REGISTRY_DIR / "holdout_ledger.yaml"
    sha = S.check_unopened(hyp_doc, S.load_yaml(ledger))
    inp = load_inputs(source, raw_dir)
    opened = datetime.now(UTC).isoformat()
    S.append_ledger(ledger, {"hypotheses_sha256": sha, "opened_at": opened, "code_sha256": S.code_sha256(),
                             "status": "opening"})  # fmt: skip
    doc = S.holdout(inp, hyp_doc)
    doc["opened_at"] = opened
    hsha = S.write_json(S.REGISTRY_DIR / "holdout.json", doc)
    S.append_ledger(ledger, {"hypotheses_sha256": sha, "opened_at": opened, "holdout_json_sha256": hsha,
                             "confirmed": [S.hyp_key(r) for r in doc["results"] if r["confirmed"]],
                             "status": "scored"})  # fmt: skip
    for r in doc["results"]:
        typer.echo(f"{S.hyp_key(r)} rel={r['relative']} p={r.get('p_better')} holm={r.get('holm_rejected')} "
                   f"cov80={r['cov80_candidate']} confirmed={r['confirmed']}")  # fmt: skip


@app.command("seal")
def seal_cmd(
    source: str = typer.Option("db"),
    raw_dir: Path | None = typer.Option(None),
    as_of: str = typer.Option("", help="YYYY-MM-DD (default: today UTC)"),
) -> None:
    """Seals REVA's prospective nowcasts (2026Q4, 2027Q1 GDP; 2026-10..2027-03 CPI) after the holdout was opened:
    sealed/<as_of>.json (canonical sha256) + an append-only line in sealed.yaml (commit both)."""
    _protocol_ok()
    led = S.load_yaml(S.REGISTRY_DIR / "holdout_ledger.yaml")
    if not any(o.get("status") == "scored" for o in led.get("openings", []) or []):
        raise SystemExit("prospective sealing follows the holdout opening (protocol)")
    day = datetime.fromisoformat(as_of).date() if as_of else datetime.now(UTC).date()
    doc = S.prospective(load_inputs(source, raw_dir), day)
    doc["sealed_at"] = datetime.now(UTC).isoformat()
    sha = S.canonical_sha256(doc)
    (S.REGISTRY_DIR / "sealed").mkdir(exist_ok=True)
    out = S.REGISTRY_DIR / "sealed" / f"{day.isoformat()}.json"
    if out.exists():
        raise SystemExit(f"{out} exists: sealed records are never overwritten")
    S.write_json(out, {**doc, "canonical_sha256": sha})
    path = S.REGISTRY_DIR / "sealed.yaml"
    reg = S.load_yaml(path) or {"schema": "reva-beat-nowcast-sealed-index/1",
                                "statement": "Append-only index of sealed prospective records (canonical sha256 of "
                                "the record without this field)."}  # fmt: skip
    reg.setdefault("records", []).append({"as_of": day.isoformat(), "file": f"sealed/{day.isoformat()}.json",
                                          "canonical_sha256": sha, "sealed_at": doc["sealed_at"],
                                          "n_records": len(doc["records"])})  # fmt: skip
    path.write_text(yaml.safe_dump(reg, sort_keys=False))
    typer.echo(f"sealed {len(doc['records'])} records, sha256 {sha}")


if __name__ == "__main__":
    app()
