"""CLI of the V6 PRED-MODELS evaluation: ``uv run python -m reva.cli.pred_models dev|holdout``.

Reads the vintage atlas tables of the dev DB (read-only, API credentials). ``--store`` also records the manifest in
the DB (evaluation namespace): run that under ``flock /tmp/reva-devdb.lock``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import typer

from reva.evaluation.pred_models import service as S

app = typer.Typer(no_args_is_help=True, help="V6 PRED-MODELS: real-time evaluation of forecasting models")


def _load() -> tuple[Any, Any, Any]:
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
        pins = {k: latest_version(conn, k, origin="live", cutoff=now) for k in ("atlas.oecd.kei_mq", "atlas.imf.pi_m")}
    fc = FrameCache(store, size=3)
    frames = {k: fc.get(p["artifact_sha256"]) if p else None for k, p in pins.items()}
    extra = S.extra_monthly_from_frames(frames["atlas.oecd.kei_mq"], frames["atlas.imf.pi_m"])
    return cells, extra, store


def _store(manifest: dict[str, Any], store: Any) -> str:
    from reva.adapters.postgres.engine import make_engine
    from reva.settings import get_settings

    return S.store_manifest(make_engine("worker", get_settings()), store, manifest)


@app.command("dev")
def dev_cmd(
    task: list[str] = typer.Option([], "--task", help="weo, us, g7 (repeatable; default all)"),
    store_db: bool = typer.Option(False, "--store", help="record the manifest in the dev DB (use flock)"),
) -> None:
    """Development stage: dev origins only; writes development.json and protocol.yaml (holdout hypotheses)."""
    cells, extra, store = _load()
    man = S.development(cells, tasks=tuple(task) or ("weo", "us", "g7"), extra=extra)
    sha = S.write_json(S.REGISTRY_DIR / "development.json", man)
    proto = S.protocol_from(man, dev_manifest_sha256=sha, written_at=datetime.now(UTC).isoformat())
    S.REGISTRY_DIR.joinpath("protocol.yaml").write_text(
        "# Locked-holdout protocol (agent PRED-MODELS): written by the development stage, committed before the\n"
        "# holdout is opened. Do not edit: the holdout ledger keys on its canonical sha256.\n"
        + __import__("yaml").safe_dump(proto, sort_keys=False),
        encoding="utf-8",
    )
    out = {"development_json_sha256": sha, "hypotheses": len(proto["hypotheses"]), "protocol": str(S.REGISTRY_DIR)}
    if store_db:
        out["db_manifest_sha256"] = _store(man, store)
    typer.echo(json.dumps(out, indent=1))


@app.command("holdout")
def holdout_cmd(store_db: bool = typer.Option(False, "--store", help="record the manifest in the dev DB")) -> None:
    """Open the locked holdout ONCE for the committed protocol; writes holdout.json, the ledger entry and
    registered_models.yaml."""
    proto = S.load_yaml(S.REGISTRY_DIR / "protocol.yaml")
    ledger = S.load_yaml(S.REGISTRY_DIR / "holdout_ledger.yaml")
    if not proto:
        raise typer.BadParameter("no protocol.yaml: run the development stage first")
    S.check_holdout_unopened(proto, ledger)
    cells, extra, store = _load()
    opened_at = datetime.now(UTC).isoformat()
    man = S.holdout(cells, proto, ledger, extra=extra)
    sha = S.write_json(S.REGISTRY_DIR / "holdout.json", man)
    S.append_ledger(
        S.REGISTRY_DIR / "holdout_ledger.yaml",
        {
            "protocol_sha256": man["protocol_sha256"],
            "model_version": proto["model_version"],
            "opened_at": opened_at,
            "holdout_json_sha256": sha,
            "hypotheses": [S._hyp_key(h) for h in proto["hypotheses"]],
            "confirmed": [S._hyp_key(h) for h in man["registered"]],
        },
    )
    S.REGISTRY_DIR.joinpath("registered_models.yaml").write_text(
        "# Forecasters that beat their benchmark with significance on the locked holdout (agent PRED-MODELS).\n"
        "# Each entry is a scope (task, variable, horizon) - the model is registered for that scope only.\n"
        + __import__("yaml").safe_dump(
            S._rounded(
                {
                    "schema": "reva-pred-models-registered/1",
                    "protocol_sha256": man["protocol_sha256"],
                    "holdout_json_sha256": sha,
                    "registered": man["registered"],
                }
            ),
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    out = {"holdout_json_sha256": sha, "registered": man["registered"]}
    if store_db:
        out["db_manifest_sha256"] = _store(man, store)
    typer.echo(json.dumps(S._rounded(out), indent=1))


if __name__ == "__main__":  # pragma: no cover
    app()
