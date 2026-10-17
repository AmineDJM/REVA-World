"""V7 BEAT-CONFLICT seal: ``python -m reva.cli.beat_conflict_seal [--dry-run]`` (run under flock on the dev DB).

Seals W3 of ``registry/forecasts/beat_conflict/protocol.yaml`` for the admitted families only (admission read from
``docs/dev/v7_beat_conflict_result.json``), with the registered code (:mod:`reva.evaluation.conflict.beat`):
origin 2026-08, steps 3-8, models cm_hurdle / cm_stack_views (admitted) and the benchmarks persistence / VIEWS run
in force (attributed). Country-month RISK ESTIMATES only.
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from collections.abc import Sequence
from datetime import date
from typing import Any

from reva.cli.beat_conflict import ROOT, check_protocol, load

RESULT = ROOT / "docs/dev/v7_beat_conflict_result.json"
FAM_BY_TYPE = {"quantity": "ucdp.sb_deaths_cm", "binary": "ucdp.sb_conflict25_cm"}


def build(
    L: dict[str, Any], proto: dict[str, Any], admitted: dict[str, bool], issued_on: date, cycle: str, log: Any
) -> dict[str, Any]:
    from reva.evaluation.conflict import beat as B
    from reva.evaluation.conflict import beat_seal as S

    i0 = B.midx(1989, 1)
    ccov = L["cand_cov"]
    hi2 = ccov[1] if ccov else L["final_cov"][1]
    d2 = B.build_cmdata(
        cells={k: {**L["final"][k], **L["cand"][k]} for k in L["final"]},
        spread_sets={k: {**L["sp_final"][k], **L["sp_cand"][k]} for k in L["sp_final"]},
        econ=L["econ"],
        universe=L["universe"],
        covered=(L["final_cov"][0], hi2),
        neighbours=L["neighbours"],
        i0=i0,
    )
    w3 = proto["windows"]["W3"]
    origin = B.midx(*map(int, str(w3["origin"]).split("-")))
    if origin != hi2:
        raise SystemExit(f"W3 origin {w3['origin']} is not the last loaded Candidate month {B.mlabel(hi2)}")
    s_lo, s_hi = map(int, str(w3["steps"]).split("-"))
    steps = list(range(s_lo, s_hi + 1))
    by_origin = {B.midx(*vr.last_input): vr for vr in L["views_runs"].values()}
    vr_now = by_origin.get(origin)
    if vr_now is None or not vr_now.release_date or vr_now.release_date > issued_on.isoformat():
        raise SystemExit("no VIEWS run with last input = W3 origin released before the seal")

    def views_extra(g: str, o: int, s: int) -> Any:
        vr = by_origin.get(o)
        key = (g, (o + s) // 12, (o + s) % 12 + 1)
        if vr is None or key not in vr.monthly or key not in vr.prob:
            return None
        return B.Fc(math.log1p(max(vr.monthly[key], 0.0)), vr.prob[key], None)

    fc = B.FeatureCache(d2)
    v_orig = sorted(o for o in by_origin if o < hi2)
    _, recsv = B.run_origins(d2, v_orig, steps, extra=views_extra, fc_cache=fc, log=log, keep_records=True)

    def outc(g: str, i: int) -> float | None:
        v = d2.y(d2.geos.index(g), i)
        return None if math.isnan(v) else v

    w_prob = B.stack_weight(recsv, outc, "logloss")
    w_pt = B.stack_weight(recsv, outc, "msle")
    del recsv
    _, recs = B.run_origins(
        d2, [origin], steps, extra=views_extra, fc_cache=fc, outcome=lambda _g, _i: math.nan, keep_records=True
    )
    iso_year = max(L["universe"])
    pin = L["tables"][S.DATASET_KEY]
    questions: list[dict[str, Any]] = []
    forecasts: list[dict[str, Any]] = []
    countries: set[str] = set()
    skipped: list[dict[str, Any]] = []
    for r in sorted(recs, key=lambda r: (r["gw"], r["step"])):
        g, s = r["gw"], r["step"]
        iso = (L["iso3"].get(g) or {}).get(iso_year)
        if not iso:
            if not any(x["gw"] == g for x in skipped):
                skipped.append({"gw": g, "reason": f"no ISO3 for this GW code in {iso_year} (crosswalk gap)"})
            continue
        countries.add(iso)
        y0 = d2.y(d2.geos.index(g), origin)
        for ty, fam in FAM_BY_TYPE.items():
            if not admitted.get(fam):
                continue
            q = S.question(fam, iso, g, origin + s, origin, y0, cycle, pin, issued_on)
            questions.append(q)
            cand, pers, vw = r.get(B.CAND), r.get(B.PERS), r.get(B.VIEWS)
            det = {"protocol": S.PROTOCOL_PATH, "window": "W3", "step": s}
            forecasts.append(S.record(q, B.CAND, cand, origin, {**det, "fit_month": B.mlabel(B.fit_month_for(origin))}))
            stack = B.stack_fc(vw, cand, float(w_prob["w"]), float(w_pt["w"])) if vw and cand else None
            rec = S.record(q, B.STACK, stack, origin, {**det, "w_views_prob": w_prob["w"], "w_views_point": w_pt["w"]})
            if stack is not None:
                rec["attribution"] = f"REVA combination of VIEWS run {vr_now.run} (attributed) and forecast.cm_hurdle"
                rec["claim"] = "a win reads: REVA's combination with VIEWS beats VIEWS alone"
            forecasts.append(rec)
            prec = S.record(q, B.PERS, pers, origin, det)
            prec["claim"] = "benchmark"
            forecasts.append(prec)
            vrec = S.record(
                q, B.VIEWS, vw, origin, {**det, "views_run": vr_now.run, "release_date": vr_now.release_date}
            )
            if vw is not None:
                vrec["attribution"] = (
                    f"VIEWS (Uppsala University / PRIO), run {vr_now.run} released {vr_now.release_date}; "
                    + ("main_dich" if ty == "binary" else "main_mean")
                )
                vrec["licence_review"] = "VIEWS licence not stated by the provider (V6 PRED-CONFLICT)"
                ref = vr_now.refs.get((g, (origin + s) // 12, (origin + s) % 12 + 1))
                if ref:
                    vrec["source_cell"] = ref
            forecasts.append(vrec)
    fams = {fam: S.family_doc(fam, sorted(countries)) for fam in FAM_BY_TYPE.values() if admitted.get(fam)}
    return {
        "questions": questions,
        "forecasts": forecasts,
        "families": fams,
        "skipped": skipped,
        "views_run": {"run": vr_now.run, "release_date": vr_now.release_date},
        "weights": {
            "prob": {k: v for k, v in w_prob.items() if k != "curve"},
            "point": {k: v for k, v in w_pt.items() if k != "curve"},
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    from sqlalchemy import text

    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres import repositories as repo
    from reva.adapters.postgres.engine import make_engine
    from reva.evaluation.conflict import beat_seal as S
    from reva.evaluation.conflict.service import CONFLICT_SCOPE
    from reva.evaluation.ledger import manifest_sha256
    from reva.evaluation.registry import periods as P
    from reva.evaluation.registry.service import (
        BATCH_SCHEMA,
        ISSUE_KIND,
        ISSUE_LOCK,
        SCOPE_KIND,
        _code_digest,
        _insert_eval,
        issued_question_keys,
        model_version_ids,
    )
    from reva.kernel.errors import IntegrityFailure, ValidationProblem
    from reva.settings import get_settings

    ap = argparse.ArgumentParser(description="V7 BEAT-CONFLICT seal (W3)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    proto = check_protocol()
    res = json.loads(RESULT.read_text())
    if res.get("protocol_code_sha256") != proto["code_sha256"]:
        raise SystemExit("result file was not produced by the registered code")
    admitted = {k: bool(v) for k, v in res["admitted"].items()}
    if not any(admitted.values()):
        print(json.dumps({"status": "nothing_admitted"}))
        return 0

    def log(m: str) -> None:
        print(m, file=sys.stderr, flush=True)

    eng = make_engine("worker", get_settings())
    store = get_store()
    L = load(eng, store)
    with eng.connect() as conn:
        now = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
    cycle = P.cycle_of(now.date())
    built = build(L, proto, admitted, now.date(), cycle, log)
    label = (
        "V7 BEAT-CONFLICT sealed W3: country-month conflict-RISK estimates of the W1-admitted REVA model and its "
        "combination with VIEWS, with the pre-registered benchmarks (persistence fair density, VIEWS attributed). "
        "Admission came from a FINAL-VINTAGE backtest, not real-time skill. Not predictions of specific attacks, "
        "actors or persons; not advice"
    )
    manifest = {
        "schema": BATCH_SCHEMA,
        "label": label,
        "conflict": True,
        "beat_conflict": {"protocol": S.PROTOCOL_PATH, "window": "W3", "admitted": admitted},
        "origin": "live",
        "cycle": cycle,
        "issued_at": now.isoformat(),
        "replay_mode": "operational_as_known",
        "statement": "Frozen at issued_at from the pinned atlas table versions (GED 26.1 final to 2025-12 + UCDP "
        "Candidate 2026-01..08), sealed by the sha256 of this manifest; scored only against UCDP Candidate "
        "country-month versions registered after issued_at (FIX6 zero_within_coverage).",
        "specs": {"version": "beat-conflict-1.0.0", "sha256": None, "interval_level": None},
        "families": built["families"],
        "models": [{"model_key": k, "version": v, "description": d} for k, (v, d) in S.MODELS.items()],
        "data": {
            "tables": {k: v for k, v in L["tables"].items() if v},
            "selection_rule": "per dataset_key: latest evidence.dataset version at issued_at",
        },
        "views_run_in_force": built["views_run"],
        "stack_weights": built["weights"],
        "code_digest": _code_digest(),
        "protocol_code_sha256": proto["code_sha256"],
        "questions": built["questions"],
        "forecasts": built["forecasts"],
        "generation": {"skipped": built["skipped"], "pending_families": [], "notes": []},
    }
    keys = sorted(q["question_key"] for q in built["questions"])
    summary = {
        "n_questions": len(keys),
        "n_forecasts": sum(1 for f in built["forecasts"] if f["status"] == "issued"),
        "n_abstained": sum(1 for f in built["forecasts"] if f["status"] == "abstained"),
        "views_run": built["views_run"],
        "weights": built["weights"],
        "skipped": built["skipped"],
    }
    if a.dry_run:
        summary["sha256_if_sealed_now"] = manifest_sha256(manifest)
        print(json.dumps(summary, indent=1, default=str))
        return 0
    with eng.connect() as conn:
        mv = model_version_ids(conn)
    by_type: dict[str, int] = defaultdict(int)
    for q in built["questions"]:
        by_type[q["type"]] += 1
    with eng.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ISSUE_LOCK})
        if issued_question_keys(conn, "live", cycle) & set(keys):
            raise ValidationProblem("these W3 questions are already sealed in this cycle")
        sha = repo.store_json_artifact(
            conn,
            store,
            manifest,
            "evaluation",
            schema_version="forecast-registry-batch/1",
            rights={
                "licence": "internal-derived; UCDP CC BY 4.0; IMF data per IMF terms; VIEWS forecasts attributed to "
                "VIEWS (licence not stated by the provider: licence_review)"
            },
        )
        if sha != manifest_sha256(manifest):
            raise IntegrityFailure("stored batch manifest hash differs from its canonical hash")
        act = repo.simple_activity(
            "evaluation.forecast_registry_issue",
            "evaluation",
            "worker:forecast_registry",
            {"cycle": cycle, "origin": "live", "batch_sha256": sha, "label": label, "questions": len(keys)},
        )
        repo.record_activity(
            conn,
            act,
            inputs=[
                (t["artifact_sha256"], {"role": "atlas_table", "dataset_key": k})
                for k, t in sorted(L["tables"].items())
                if t
            ],
            outputs=[sha],
        )
        ctx = repo.create_context(conn, mode="evaluation", origin="live", manifest_sha256=sha, knowledge_cutoff=now)
        scope = {
            "kind": SCOPE_KIND,
            "origin": "live",
            "cycle": cycle,
            "batch_sha256": sha,
            "question_keys": keys,
            CONFLICT_SCOPE: True,
            "beat_conflict": True,
        }
        for mk, (ver, _d) in sorted(S.MODELS.items()):
            mine = [f for f in built["forecasts"] if f["model"] == mk]
            _insert_eval(
                conn,
                mv[mk],
                ctx,
                sha,
                "operational_as_known",
                scope,
                {
                    "kind": ISSUE_KIND,
                    "status": "sealed",
                    "label": label,
                    "model": f"{mk}@{ver}",
                    "issued_at": now.isoformat(),
                    "batch_sha256": sha,
                    "n_questions": len(keys),
                    "n_issued": sum(1 for f in mine if f["status"] == "issued"),
                    "n_abstained": sum(1 for f in mine if f["status"] == "abstained"),
                    "by_type": dict(by_type),
                },
                act.id,
            )
        repo.emit_outbox(
            conn, "evaluation.forecast_registry_issued", {"batch_sha256": sha, "cycle": cycle, "origin": "live"}
        )
    summary["batch_sha256"] = sha
    summary["issued_at"] = now.isoformat()
    summary["cycle"] = cycle
    print(json.dumps(summary, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
