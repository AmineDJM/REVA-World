"""BEAT-AGRI CLI (V7): REVA vs USDA WASDE crop projections — development stage, holdout (opened once), sealing.

Reads the dev DB read-only (API credentials): ``atlas.usda_wasde_esmis.headline`` (each WASDE report as published,
ESMIS archive) and ``atlas.ncei_cag.agbelt_monthly`` (NCEI ag-belt weather), pinned by artifact sha256.

    uv run python -m reva.cli.beat_agri describe   # data coverage only (no evaluation window)
    uv run python -m reva.cli.beat_agri dev        # -> development.json + hypotheses.yaml (commit them!)
    uv run python -m reva.cli.beat_agri holdout    # refuses unless hypotheses.yaml is committed; opens ONCE
    flock /tmp/reva-devdb.lock uv run python -m reva.cli.beat_agri seal --seal   # 2026/27 registry batch
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from typing import Any

import typer
import yaml

from reva.evaluation.beat_agri import cases as C
from reva.evaluation.beat_agri import service as S

app = typer.Typer(add_completion=False, help=__doc__)
WASDE_TABLE = "atlas.usda_wasde_esmis.headline"
WEATHER_TABLE = "atlas.ncei_cag.agbelt_monthly"


def load_inputs(*, need_weather: bool = True) -> tuple[C.Vintages, C.Weather | None, dict[str, Any]]:
    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres.engine import make_engine
    from reva.atlas.tables import read_atlas_table
    from reva.evaluation.registry.data import latest_version
    from reva.settings import get_settings

    eng = make_engine("api", get_settings())
    store = get_store()
    now = datetime.now(UTC)
    with eng.connect() as conn:
        pins = {k: latest_version(conn, k, origin="live", cutoff=now) for k in (WASDE_TABLE, WEATHER_TABLE)}
    if pins[WASDE_TABLE] is None:
        raise typer.BadParameter(f"{WASDE_TABLE} has no live table version (run the atlas ingest first)")
    if need_weather and pins[WEATHER_TABLE] is None:
        raise typer.BadParameter(f"{WEATHER_TABLE} has no live table version (run the atlas ingest first)")
    v = C.Vintages.from_frame(read_atlas_table(store, pins[WASDE_TABLE]["artifact_sha256"]))  # type: ignore[index]
    w = (
        C.Weather.from_frame(read_atlas_table(store, pins[WEATHER_TABLE]["artifact_sha256"]))  # type: ignore[index]
        if pins[WEATHER_TABLE]
        else None
    )
    pin_doc = {k: {x: p[x] for x in ("dataset_key", "artifact_sha256", "recorded_at", "row_count")}
               for k, p in pins.items() if p}  # fmt: skip
    return v, w, pin_doc


def _git(*args: str) -> str:
    git = shutil.which("git") or "/usr/bin/git"
    return subprocess.run([git, *args], cwd=S.REGISTRY_DIR, check=True, capture_output=True, text=True).stdout


def _committed(path: str) -> bool:
    try:
        _git("ls-files", "--error-unmatch", path)
    except subprocess.CalledProcessError:
        return False
    return not _git("status", "--porcelain", "--", path).strip()


def _protocol() -> dict[str, Any]:
    if not _committed("protocol.yaml"):
        raise SystemExit("protocol.yaml must be committed before any evaluation window is opened")
    doc: dict[str, Any] = yaml.safe_load((S.REGISTRY_DIR / "protocol.yaml").read_text())
    return doc


def _code_allowed(proto: dict[str, Any]) -> None:
    """The evaluated code must be the pre-registered code or a logged amendment (amendments.yaml)."""
    ok = {proto["code_sha256"]} | {
        a["code_sha256"] for a in (S.load_yaml(S.REGISTRY_DIR / "amendments.yaml").get("amendments") or [])
    }
    if S.code_sha256() not in ok:
        raise SystemExit(f"code digest {S.code_sha256()} is neither the protocol's nor a logged amendment")


@app.command("describe")
def describe_cmd() -> None:
    """Coverage of the inputs (releases, series per release); opens no evaluation window."""
    v, w, pins = load_inputs(need_weather=False)
    per_year: dict[int, int] = {}
    for r in v.releases:
        per_year[r.release.year] = per_year.get(r.release.year, 0) + 1
    partial = [r.release.isoformat() for r in v.releases if len({s for s, _ in r.values}) < len(C.SERIES)]
    typer.echo(yaml.safe_dump({"pins": pins, "releases": len(v.releases), "per_year": per_year,
                               "releases_missing_some_series": partial,
                               "weather_regions": sorted(w.values) if w else None}, sort_keys=False))  # fmt: skip


@app.command("dev")
def dev_cmd() -> None:
    """Development stage (marketing years 2005-2016) -> development.json and hypotheses.yaml."""
    proto = _protocol()
    _code_allowed(proto)
    proto_sha = S.canonical_sha256(proto)
    v, w, pins = load_inputs()
    cases, dropped = S.all_cases(v, w, last_my=S.DEV[1])  # holdout years are not even built
    doc = S.development(cases, pins)
    doc["dropped_cases"] = dropped
    dsha = S.write_json(S.REGISTRY_DIR / "development.json", doc)
    hyps = S.select_hypotheses(doc["tests"])
    hyp_doc = {
        "schema": "reva-beat-agri-hypotheses/1",
        "written_at": datetime.now(UTC).isoformat(),
        "statement": "Written by the development stage from protocol.yaml's selection rule; committed before the "
        "holdout is opened. Only these hypotheses can be confirmed (one Holm over all of them).",
        "protocol_sha256": proto_sha,
        "development_json_sha256": dsha,
        "code_sha256": doc["code_sha256"],
        "family_size": len(hyps),
        "hypotheses": hyps,
    }
    (S.REGISTRY_DIR / "hypotheses.yaml").write_text(yaml.safe_dump(hyp_doc, sort_keys=False))
    typer.echo(yaml.safe_dump({"n_cases": doc["n_cases"], "dev_family": doc["family_size"], "hypotheses": hyps},
                              sort_keys=False))  # fmt: skip


@app.command("holdout")
def holdout_cmd() -> None:
    """Opens the holdout ONCE (refuses unless hypotheses.yaml is committed and unchanged)."""
    proto = _protocol()
    _code_allowed(proto)
    if not _committed("hypotheses.yaml"):
        raise SystemExit("hypotheses.yaml must be committed before the holdout is opened")
    hyp_doc = yaml.safe_load((S.REGISTRY_DIR / "hypotheses.yaml").read_text())
    sha = S.canonical_sha256(hyp_doc)
    ledger_path = S.REGISTRY_DIR / "holdout_ledger.yaml"
    S.check_unopened(S.load_yaml(ledger_path), sha)
    opened = datetime.now(UTC).isoformat()
    S.append_ledger(ledger_path, {"hypotheses_sha256": sha, "opened_at": opened, "code_sha256": S.code_sha256(),
                                  "status": "opening"})  # fmt: skip
    v, w, pins = load_inputs()
    cases, _dropped = S.all_cases(v, w)
    doc = S.holdout(cases, hyp_doc["hypotheses"], pins)
    doc["opened_at"] = opened
    hsha = S.write_json(S.REGISTRY_DIR / "holdout.json", doc)
    S.append_ledger(ledger_path, {"hypotheses_sha256": sha, "opened_at": opened, "holdout_json_sha256": hsha,
                                  "confirmed": doc["confirmed"], "status": "scored"})  # fmt: skip
    for r in doc["results"]:
        typer.echo(f"{r['key']}  rel={r['relative']} p={r.get('p_better')} holm={r['holm_rejected']} "
                   f"cov80={r.get('coverage80')} confirmed={r['confirmed']}")  # fmt: skip


if __name__ == "__main__":
    app()
