"""`reva estimate ...` commands (PRED-EVENTSTUDY).

* ``reva estimate treatments`` — the coded event types usable as treatments;
* ``reva estimate replicate`` — reproduce the published results of ``registry/estimation/replications.yaml`` on
  the pinned atlas snapshot (PASS/FAIL per target; the gate for new estimates);
* ``reva estimate event-study --treatment <id> --outcome concept:<id> ...`` — one local-projection run; refused
  unless at least 3 published results are reproduced on the same snapshot. ``--archive`` stores the run document
  as an artifact; ``--write-effects`` also writes the REVA-run effect estimate through PRED-EFFECT's loader
  (dev DB writes: run under ``flock /tmp/reva-devdb.lock``).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import typer

estimate_app = typer.Typer(
    no_args_is_help=True, help="V5 estimation: event studies / local projections on atlas panels"
)


def _out(obj: Any) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def _snapshot_sha(snapshot: str | None, origin: str) -> str:
    if snapshot:
        return snapshot
    from reva.adapters.postgres.engine import make_engine
    from reva.atlas.snapshot import latest_snapshot_ref

    eng = make_engine("api")
    try:
        with eng.connect() as conn:
            ref = latest_snapshot_ref(conn, origin)
    finally:
        eng.dispose()
    if ref is None:
        typer.echo(f"no atlas snapshot recorded for origin {origin}", err=True)
        raise typer.Exit(2)
    return str(ref["sha"])


@estimate_app.command("treatments")
def treatments_cmd() -> None:
    """List the treatments (coded event types with onset dates) of registry/estimation/treatments.yaml."""
    from reva.estimation.data import load_treatments

    _out([t.as_dict() for t in load_treatments().values()])


def _strip(rep: dict[str, Any], full: bool) -> dict[str, Any]:
    if full:
        return rep
    out = dict(rep)
    res = []
    for r in rep["results"]:
        r = {k: v for k, v in r.items() if k not in ("outcome", "inputs", "rows")}
        res.append(r)
    out["results"] = res
    return out


@estimate_app.command("replicate")
def replicate_cmd(
    snapshot: str | None = typer.Option(None, help="atlas snapshot sha256 (default: latest of --origin)"),
    origin: str = typer.Option("live", help="snapshot origin"),
    only: list[str] = typer.Option([], "--only", help="target id(s) to run"),
    out: Path | None = typer.Option(None, help="write the full report (JSON) to this file"),
    archive: bool = typer.Option(False, "--archive", help="store the report as an artifact (worker credentials)"),
    papers: Path | None = typer.Option(
        None, help="directory holding the publication PDFs named in the registry: archive the ones whose sha256 matches"
    ),
    full: bool = typer.Option(False, "--full", help="print per-crisis rows and inputs"),
) -> None:
    """Reproduce the published results before any new estimate (PASS needs the tolerance declared in the registry)."""
    from reva.estimation.data import SnapshotPanels
    from reva.estimation.replication import run_replications

    sha = _snapshot_sha(snapshot, origin)
    with SnapshotPanels(sha) as panels:
        rep = run_replications(panels, only=list(only) or None)
    if out:
        out.write_text(json.dumps(rep, indent=1, default=str, ensure_ascii=False))
    if archive or papers:
        from reva.adapters.objects.factory import get_store
        from reva.adapters.postgres import repositories as repo
        from reva.adapters.postgres.engine import make_engine
        from reva.estimation.replication import load_targets

        eng = make_engine("worker")
        store = get_store()
        archived: dict[str, Any] = {}
        with eng.begin() as conn:
            if archive:
                archived["report_sha256"] = repo.store_json_artifact(
                    conn, store, rep, "evaluation", schema_version="reva-eventstudy-replication/1"
                )
            if papers:
                import hashlib

                for t in load_targets().values():
                    want = str(t.get("file_sha256", ""))
                    for f in sorted(papers.glob("*.pdf")):
                        data = f.read_bytes()
                        if hashlib.sha256(data).hexdigest() == want:
                            obj = store.put_bytes(data, "raw", "application/pdf")
                            repo.register_artifact(
                                conn,
                                obj,
                                schema_version="publication-pdf/1",
                                rights={
                                    "licence": "copyright of the authors/publisher; archived for provenance only",
                                    "restricted": True,
                                    "url": t.get("url"),
                                },
                            )
                            archived[t["id"]] = obj.sha256
        eng.dispose()
        rep["archived"] = archived
    _out(_strip(rep, full))
    if not rep["gate_open"]:
        raise typer.Exit(1)


@estimate_app.command("event-study")
def event_study_cmd(
    treatment: str = typer.Option(..., help="treatment id (reva estimate treatments)"),
    outcome: str = typer.Option(..., help="concept:<atlas concept id> or series:<source>.<dataset>.<indicator>"),
    horizons: int = typer.Option(5, help="IRF horizons 0..H"),
    lags: int = typer.Option(2, help="lags of the outcome change and of the treatment"),
    leads: int = typer.Option(3, help="placebo leads -L..-2 and event-study leads"),
    lhs: str = typer.Option("cumsum", help="cumsum (growth rates) | diff (levels, pp) | logdiff (levels, %) | level"),
    universe: str = typer.Option("all", help="all | treated_dataset (countries the event dataset codes)"),
    frequency: str = typer.Option("annual", help="annual | quarter (provider series only)"),
    start: int | None = typer.Option(None, help="first year"),
    end: int | None = typer.Option(None, help="last year"),
    as_of: str | None = typer.Option(None, "--as-of", help="YYYY-MM-DD: ignore what was recorded / happened later"),
    seed: int = typer.Option(20261017, help="permutation placebo seed"),
    perm: int = typer.Option(200, help="permutation placebo draws (0 = none)"),
    snapshot: str | None = typer.Option(None, help="atlas snapshot sha256 (default: latest of --origin)"),
    origin: str = typer.Option("live", help="snapshot and KG origin"),
    out: Path | None = typer.Option(None, help="write the run document (JSON) to this file"),
    archive: bool = typer.Option(False, "--archive", help="store the run document as an artifact (worker)"),
    write_effects: bool = typer.Option(
        False, "--write-effects", help="also write the REVA-run effect estimate (PRED-EFFECT loader; implies --archive)"
    ),
) -> None:
    """Local projections of an atlas outcome on coded event onsets (Jordà), with pre-trend and placebo tests.
    Every output is an association under stated assumptions, never an established causal effect."""
    from reva.adapters.postgres.engine import make_engine
    from reva.estimation.data import SnapshotPanels
    from reva.estimation.replication import gate, run_replications
    from reva.estimation.run import EventStudyRequest, run_event_study

    req = EventStudyRequest(
        treatment=treatment,
        outcome=outcome,
        horizons=horizons,
        lags=lags,
        leads=leads,
        lhs=lhs,  # type: ignore[arg-type]
        universe=universe,
        frequency=frequency,  # type: ignore[arg-type]
        start=start,
        end=end,
        as_of=date.fromisoformat(as_of) if as_of else None,
        seed=seed,
        n_perm=perm,
        origin=origin,
    )
    sha = _snapshot_sha(snapshot, origin)
    eng = make_engine("api")
    try:
        with SnapshotPanels(sha) as panels:
            rep = run_replications(panels)
            gate(rep)
            with eng.connect() as conn:
                doc = run_event_study(
                    req, panels, conn=conn, replication_report=rep, progress=lambda m: typer.echo(m, err=True)
                )
    finally:
        eng.dispose()
    if out:
        out.write_text(json.dumps(doc, indent=1, default=str, ensure_ascii=False))
    summary: dict[str, Any] = {
        "run_id": doc["run_id"],
        "treatment": doc["inputs"]["events"]["treatment"]["id"],
        "outcome": doc["inputs"]["outcome"]["ref"],
        "onsets": doc["inputs"]["events"]["onsets"],
        "irf": [
            {k: round(v, 3) if isinstance(v, float) else v for k, v in e.items() if k != "t_crit"}
            for e in doc["results"]["local_projection"]["irf"]
        ],
        "placebo_leads": doc["results"]["local_projection"]["placebo_leads"],
        "pretrend_test": (doc["results"]["event_study"] or {}).get("pretrend_test"),
        "interpretation": doc["interpretation"],
        "replication_gate": doc["replication_gate"],
        "vintage_mode": doc["inputs"]["vintage_mode"],
        "rights": doc["rights"],
        "caveats": doc["caveats"],
    }
    if archive or write_effects:
        from reva.adapters.objects.factory import get_store
        from reva.estimation.run import archive_run, write_effect_estimates

        weng = make_engine("worker")
        store = get_store()
        try:
            run_sha = archive_run(weng, store, doc)
            summary["run_document_sha256"] = run_sha
            if write_effects:
                summary["effect_load"] = write_effect_estimates(weng, store, doc, run_sha, origin=origin)
        finally:
            weng.dispose()
    _out(summary)
