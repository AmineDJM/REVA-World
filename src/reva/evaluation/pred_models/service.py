"""Development stage, locked holdout and registration of forecasting models (V6, agent PRED-MODELS).

Protocol (fixed before any holdout number exists):

1. **Development** (``stage="development"``): only origins strictly before the task's split date are forecast
   and scored (``SPLITS``). For every declared comparison (``COMPARISONS``: candidate, benchmark, loss) and every
   (variable, horizon) cell, a one-sided DM-HLN test by origin; Holm within each (task, candidate, benchmark,
   loss) family. A cell rejected by Holm becomes a **holdout hypothesis**. The hypotheses, the splits, alpha,
   model versions and the code digest are written to ``registry/forecasts/pred_models/protocol.yaml`` (and
   committed) before the holdout is opened.
2. **Locked holdout** (``stage="holdout"``): opened once per protocol (``holdout_ledger.yaml`` refuses a second
   opening of the same protocol or of a hypothesis already tested with the same model version). All origins are
   forecast (derived models need the earlier errors), only holdout origins are scored. Holm over **all**
   hypotheses jointly at ``ALPHA``. A hypothesis is confirmed iff Holm-rejected; a density candidate must also
   have a holdout central-80 % coverage within ``COVERAGE_BAND``. Confirmed (model, task, variable, horizon)
   scopes are written to ``registered_models.yaml``; everything else is reported, nothing is hidden.

Models whose inputs are not all real-time (``INELIGIBLE``: the DFM variant fed with final-vintage industrial
production and market rates) are evaluated and reported, never registered (roadmap decision 4).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import yaml

from reva.evaluation.pred_models import gate as G
from reva.evaluation.pred_models import realtime as R
from reva.evaluation.pred_models.derived import TASK_DERIVED, add_derived, conf_key
from reva.kernel.errors import PolicyDenied, ValidationProblem

SCHEMA = "reva-pred-models-evaluation/1"
LABEL = "real_time_vintage_backtest"
HONESTY = (
    "Real-time backtest: at every origin the models see only provider vintages whose evidenced, conservative "
    "availability instant is at or before the origin (available_then reconstruction; REVA did not exist at these "
    "origins). Outcomes are first releases from later vintages. Derived models use only past errors whose outcome "
    "was published at their origin. Not financial advice."
)
SPLITS = {"weo": "2019-01-01", "us": "2013-01-01", "g7": "2016-01-01"}
ALPHA = 0.05
COVERAGE_BAND = (0.70, 0.90)
DFM_X = R.DFM + "_x"
INELIGIBLE = {DFM_X: "monthly industrial production and market rates are final-vintage copies (pseudo-real-time)"}
REGISTRY_DIR = Path(__file__).resolve().parents[4] / "registry" / "forecasts" / "pred_models"

_W = R.WEO
_S = R.SPF
COMPARISONS: dict[str, list[tuple[str, str, str]]] = {
    "weo": [
        *[(c, _W, "ae") for c in (R.DIRECT_AR, "forecast.combo_weo_ar", "forecast.combo_weo_ar_persist",
                                  "forecast.weo_bias_corrected")],
        *[(c, R.WEO_GAUSS, "crps") for c in (conf_key(_W), "forecast.weo_bias_corrected", "forecast.combo_weo_ar")],
        *[(c, R.PERSIST, "crps") for c in (R.DIRECT_AR, conf_key(R.DIRECT_AR), conf_key(_W))],
    ],
    "us": [
        *[(c, _S, "ae") for c in (R.DIRECT_AR, R.BVAR, "forecast.combo_spf_bvar", "forecast.combo_spf_ar")],
        *[(c, R.SPF_GAUSS, "crps") for c in (conf_key(_S), "forecast.combo_spf_bvar", "forecast.combo_spf_ar")],
        *[(c, R.PERSIST, "crps") for c in (R.BVAR, R.DIRECT_AR, "forecast.pool_bvar_ar")],
    ],
    "g7": [
        *[(c, R.PERSIST, "crps") for c in (R.BVAR, R.DIRECT_AR, "forecast.pool_bvar_ar", R.DFM,
                                           "forecast.combo_dfm_bvar", conf_key(R.BVAR), conf_key(R.DFM), DFM_X)],
        *[(c, R.REG_AR, "crps") for c in (R.BVAR, "forecast.pool_bvar_ar", R.DFM, "forecast.combo_dfm_bvar", DFM_X)],
        *[(c, R.REG_AR, "ae") for c in (R.BVAR, R.DFM, DFM_X)],
    ],
}  # fmt: skip
TASK_DESIGN = {
    "weo": "IMF WEO panel, annual, origins 1 Jun / 1 Dec 2008-2025, h0 = current year, h1 = next year",
    "us": "United States, RTDSM vintages + SPF medians, origins = SPF release dates, SPF horizons 2..6",
    "g7": "G7, OECD MEI real-time editions, origins = edition availability, k0 = origin quarter .. k3",
}
SOURCE_FILES = (
    "src/reva/models/forecasting/direct.py",
    "src/reva/models/forecasting/bvar.py",
    "src/reva/models/forecasting/dfm.py",
    "src/reva/models/forecasting/combination.py",
    "src/reva/models/forecasting/registry_baselines.py",
    "src/reva/evaluation/pred_models/realtime.py",
    "src/reva/evaluation/pred_models/derived.py",
    "src/reva/evaluation/pred_models/gate.py",
    "src/reva/evaluation/pred_models/service.py",
)
MODEL_VERSION = "1.0.0"


def code_sha256(root: Path | None = None) -> str:
    base = root or Path(__file__).resolve().parents[4]
    h = hashlib.sha256()
    for f in SOURCE_FILES:
        h.update(f.encode() + b"\0" + (base / f).read_bytes() + b"\0")
    return h.hexdigest()


def canonical_sha256(doc: Any) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


# --------------------------------------------------------------------------- cases
def extra_monthly_from_frames(
    kei: pl.DataFrame | None, pi: pl.DataFrame | None
) -> Callable[[str, datetime], dict[str, dict[int, float]]]:
    """Final-vintage monthly indicators for the *ineligible* DFM variant, with stated publication-lag rules:
    market rates of month m usable from the 1st of m+1; industrial production of month m from the 1st of m+2."""

    def series(df: pl.DataFrame | None, indicator: str) -> dict[str, dict[int, float]]:
        out: dict[str, dict[int, float]] = defaultdict(dict)
        if df is None:
            return out
        for g, d, v in (
            df.filter(pl.col("indicator") == indicator).select("provider_geo", "period_start", "value").iter_rows()
        ):
            if v is not None and math.isfinite(float(v)):
                out[g][d.year * 12 + d.month - 1] = float(v)
        return out

    ir3 = series(kei, "IR3TIB|unit_measure=PA|freq=M")
    irl = series(kei, "IRLT|unit_measure=PA|freq=M")
    ip = series(pi, "IND|type_of_transformation=SA_IX")

    def fn(geo: str, origin: datetime) -> dict[str, dict[int, float]]:
        om = origin.year * 12 + origin.month - 1
        a, b, c = ir3.get(geo, {}), irl.get(geo, {}), ip.get(geo, {})
        return {
            "x_d_rate3m": {m: a[m] - a[m - 1] for m in a if m - 1 in a and m <= om - 1},
            "x_term_spread": {m: b[m] - a[m] for m in a if m in b and m <= om - 1},
            "x_ip_dlog": {
                m: 100 * math.log(c[m] / c[m - 1])
                for m in c
                if m - 1 in c and c[m - 1] > 0 and c[m] > 0 and m <= om - 2
            },
        }

    return fn


def build_cases(
    cells: pl.DataFrame,
    task: str,
    stage: str,
    *,
    extra: Callable[[str, datetime], dict[str, dict[int, float]]] | None = None,
) -> list[R.Case]:
    split = datetime.fromisoformat(SPLITS[task]).replace(tzinfo=UTC)

    def keep(t: datetime) -> bool:
        return stage == "holdout" or t < split

    if task == "weo":
        origins = [t for t in R.default_weo_origins() if keep(t)]
        cases = [c for v in R.WEO_VARIABLES for c in R.weo_cases(cells, v, origins)]
    elif task == "us":
        cases = R.us_cases(cells, origin_filter=keep)
    elif task == "g7":
        der = R.derive_g7_cells(cells)
        cases = R.g7_cases(der, [t for t in R.default_g7_origins(der) if keep(t)], extra_monthly=extra)
    else:
        raise ValidationProblem(f"unknown task {task!r}")
    add_derived(cases, TASK_DERIVED[task])
    return cases


# --------------------------------------------------------------------------- evaluation
def _cells(scored: Sequence[G.Scored]) -> dict[tuple[str, str], list[G.Scored]]:
    out: dict[tuple[str, str], list[G.Scored]] = defaultdict(list)
    for s in scored:
        out[(s.case.variable, s.case.horizon)].append(s)
    return out


def evaluate_split(cases: Sequence[R.Case], task: str, split: str) -> dict[str, Any]:
    scored = [s for s in G.score_cases(cases) if G.split_of(task, s.case.origin, SPLITS) == split]
    cells = _cells(scored)
    models = sorted({k for s in scored for k in s.scores})
    summaries = []
    for (var, hz), rows in sorted(cells.items()):
        for m in models:
            sm = G.summary(rows, m)
            if sm["n"]:
                summaries.append({"variable": var, "horizon": hz, "model": m, **sm})
    tests = []
    for cand, bench, loss in COMPARISONS[task]:
        fam = []
        for (var, hz), rows in sorted(cells.items()):
            t = G.paired_test(rows, cand, bench, loss, G.dm_lag(task, hz))
            if t["n_pairs"] == 0:
                continue
            fam.append({"task": task, "variable": var, "horizon": hz, **t, "eligible": cand not in INELIGIBLE})
        G.holm(fam, ALPHA)
        tests.extend(fam)
    origins = sorted({s.case.origin for s in scored})
    return {
        "task": task,
        "split": split,
        "design": TASK_DESIGN[task],
        "n_cases_scored": len(scored),
        "origins": [origins[0].isoformat(), origins[-1].isoformat()] if origins else None,
        "n_origins": len(origins),
        "summaries": summaries,
        "tests": tests,
        "abstentions": _abstentions(cases, task, split),
    }


def _abstentions(cases: Sequence[R.Case], task: str, split: str) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for c in cases:
        if G.split_of(task, c.origin, SPLITS) != split:
            continue
        for k, p in c.preds.items():
            if not hasattr(p, "point"):
                reason = str(getattr(p, "reason", "abstained"))
                reason = reason.split("(")[0].strip() if "(" in reason else reason
                out[k][reason] += 1
    return {k: dict(v) for k, v in out.items()}


def development(
    cells: pl.DataFrame,
    *,
    tasks: Sequence[str] = ("weo", "us", "g7"),
    extra: Callable[[str, datetime], dict[str, dict[int, float]]] | None = None,
) -> dict[str, Any]:
    results = [evaluate_split(build_cases(cells, t, "development", extra=extra), t, "development") for t in tasks]
    hyps = [
        {k: t[k] for k in ("task", "variable", "horizon", "candidate", "benchmark", "loss", "p_better", "relative")}
        for r in results
        for t in r["tests"]
        if t.get("holm_rejected") and t["eligible"]
    ]
    return {
        "schema": SCHEMA,
        "stage": "development",
        "label": LABEL,
        "honesty_statement": HONESTY,
        "splits": SPLITS,
        "alpha": ALPHA,
        "code_sha256": code_sha256(),
        "tasks": results,
        "holdout_hypotheses": hyps,
    }


def protocol_from(dev: Mapping[str, Any], *, dev_manifest_sha256: str, written_at: str) -> dict[str, Any]:
    return {
        "schema": "reva-pred-models-protocol/1",
        "written_at": written_at,
        "statement": "Fixed after the development stage and before the locked holdout is opened. The holdout is "
        "opened once; only these hypotheses can lead to registration (Holm over all of them jointly).",
        "splits": dict(SPLITS),
        "alpha": ALPHA,
        "coverage_band_density_models": list(COVERAGE_BAND),
        "model_version": MODEL_VERSION,
        "code_sha256": dev["code_sha256"],
        "development_manifest_sha256": dev_manifest_sha256,
        "ineligible_models": dict(INELIGIBLE),
        "hypotheses": [
            {**{k: h[k] for k in ("task", "variable", "horizon", "candidate", "benchmark", "loss")},
             "dev_p_better": float(f"{h['p_better']:.6g}"), "dev_relative": float(f"{h['relative']:.6g}")}
            for h in dev["holdout_hypotheses"]
        ],
    }  # fmt: skip


def _hyp_key(h: Mapping[str, Any]) -> str:
    return "|".join(str(h[k]) for k in ("task", "variable", "horizon", "candidate", "benchmark", "loss"))


def check_holdout_unopened(protocol: Mapping[str, Any], ledger: Mapping[str, Any]) -> str:
    psha = canonical_sha256(protocol)
    seen: set[str] = set()
    for e in ledger.get("openings") or []:
        if e.get("protocol_sha256") == psha:
            raise PolicyDenied(
                "the locked holdout of this protocol was already opened", retryable=False, protocol_sha256=psha
            )
        if e.get("model_version") == protocol["model_version"]:
            seen.update(e.get("hypotheses") or [])
    again = sorted({_hyp_key(h) for h in protocol["hypotheses"]} & seen)
    if again:
        raise PolicyDenied(
            "hypotheses already tested on the holdout with the same model version", retryable=False, hypotheses=again
        )
    return psha


def _is_density(model: str, loss: str) -> bool:
    return loss in ("crps", "log")


def holdout(
    cells: pl.DataFrame,
    protocol: Mapping[str, Any],
    ledger: Mapping[str, Any],
    *,
    extra: Callable[[str, datetime], dict[str, dict[int, float]]] | None = None,
) -> dict[str, Any]:
    psha = check_holdout_unopened(protocol, ledger)
    if protocol["code_sha256"] != code_sha256():
        raise PolicyDenied(
            "model code changed since the protocol was written", retryable=False, protocol_code=protocol["code_sha256"]
        )
    tasks = sorted({h["task"] for h in protocol["hypotheses"]} | {"weo", "us", "g7"})
    results = {t: evaluate_split(build_cases(cells, t, "holdout", extra=extra), t, "holdout") for t in tasks}
    index = {_hyp_key({**t, "task": task}): t for task, r in results.items() for t in r["tests"]}
    sumidx = {(r["task"], s["variable"], s["horizon"], s["model"]): s for r in results.values() for s in r["summaries"]}
    hyps = []
    for h in protocol["hypotheses"]:
        t = index.get(_hyp_key(h))
        hyps.append({**h, "holdout": t})
    tested = {i: float(x["holdout"]["p_better"]) for i, x in enumerate(hyps) if x["holdout"] and x["holdout"]["tested"]}
    from reva.models.tournament import holm_rejections

    rej = holm_rejections(tested, ALPHA)
    registered = []
    for i, x in enumerate(hyps):
        x["holm_m"] = len(tested)
        x["holm_rejected"] = i in rej
        cov = None
        if _is_density(x["candidate"], x["loss"]):
            s = sumidx.get((x["task"], x["variable"], x["horizon"], x["candidate"]))
            cov = s["coverage80"] if s else None
            x["holdout_coverage80"] = cov
            x["calibrated"] = cov is not None and COVERAGE_BAND[0] <= cov <= COVERAGE_BAND[1]
        x["confirmed"] = bool(x["holm_rejected"] and ((cov is None and not _is_density(x["candidate"], x["loss"]))
                                                     or x.get("calibrated")))  # fmt: skip
        if x["confirmed"]:
            registered.append(
                {k: x[k] for k in ("task", "variable", "horizon", "candidate", "benchmark", "loss")}
                | {
                    "holdout_p_better": x["holdout"]["p_better"],
                    "holdout_relative": x["holdout"]["relative"],
                    "holdout_coverage80": cov,
                }
            )
    return {
        "schema": SCHEMA,
        "stage": "holdout",
        "label": LABEL,
        "honesty_statement": HONESTY,
        "protocol_sha256": psha,
        "code_sha256": protocol["code_sha256"],
        "tasks": list(results.values()),
        "hypotheses": hyps,
        "registered": registered,
    }


# --------------------------------------------------------------------------- files
def _rounded(obj: Any, digits: int = 6) -> Any:
    if isinstance(obj, float):
        return float(f"{obj:.{digits}g}") if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _rounded(v, digits) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_rounded(v, digits) for v in obj]
    if isinstance(obj, (np.floating, np.integer)):
        return _rounded(obj.item(), digits)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return obj


def write_json(path: Path, doc: Mapping[str, Any]) -> str:
    data = json.dumps(_rounded(dict(doc)), sort_keys=True, indent=1, default=str).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def append_ledger(path: Path, entry: Mapping[str, Any]) -> None:
    doc = load_yaml(path) or {"schema": "reva-pred-models-holdout-ledger/1", "openings": []}
    doc.setdefault("openings", []).append(_rounded(dict(entry)))
    path.write_text(
        "# Append-only: one entry per opening of a locked holdout (agent PRED-MODELS). Never edit past entries.\n"
        + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- DB record
def store_manifest(engine: Any, store: Any, manifest: Mapping[str, Any], *, actor: str = "agent:pred-models") -> str:
    """Stores the manifest content-addressed (``evaluation`` namespace) with a provenance activity and an
    evaluation context; returns its sha256. (No model.evaluation rows: the per-model results live in the
    manifest and the registered scopes in ``registered_models.yaml``.)"""
    from sqlalchemy import text

    from reva.adapters.postgres import repositories as repo

    with engine.begin() as conn:
        now = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        sha = repo.store_json_artifact(
            conn,
            store,
            _rounded(dict(manifest)),
            "evaluation",
            schema_version="pred-models-evaluation/1",
            rights={
                "licence": "internal-derived; inputs: IMF WEO (IMF terms, attribution), Philadelphia Fed RTDSM/SPF "
                "(all rights reserved: internal scores only), OECD (CC BY 4.0), ECB (attribution)"
            },
        )
        act = repo.simple_activity(
            "evaluation.pred_models_" + str(manifest["stage"]), "evaluation", actor, {"label": LABEL, "sha256": sha}
        )
        repo.record_activity(conn, act, inputs=[], outputs=[sha])
        repo.create_context(conn, mode="evaluation", origin="live", manifest_sha256=sha, knowledge_cutoff=now)
    return sha


__all__ = [
    "ALPHA",
    "COMPARISONS",
    "COVERAGE_BAND",
    "INELIGIBLE",
    "REGISTRY_DIR",
    "SPLITS",
    "append_ledger",
    "build_cases",
    "check_holdout_unopened",
    "code_sha256",
    "development",
    "evaluate_split",
    "extra_monthly_from_frames",
    "holdout",
    "protocol_from",
    "store_manifest",
    "write_json",
]
