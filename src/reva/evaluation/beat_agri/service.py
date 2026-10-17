"""BEAT-AGRI evaluation: development stage, frozen hypotheses, holdout opened once (ledger), prospective records.

Pre-registration: ``registry/forecasts/beat_agri/protocol.yaml`` (committed before any evaluation window was
opened, with :func:`code_sha256` of the files in :data:`SOURCE_FILES`); ``hypotheses.yaml`` (written by the
development stage from the protocol's mechanical selection rule, committed before the holdout is opened);
``holdout_ledger.yaml`` (append-only; one opening per hypotheses digest).

Comparison: on each WASDE release (origin = release day + 1, 00:00 UTC) REVA's candidate vs WASDE's own number in
that release (points, AE in % of the truth) and vs the fair density benchmark N(WASDE point, RMS of WASDE's own past
relative errors at the same series and report month) (CRPS in % of the truth). Inference unit = marketing year:
loss differentials are averaged over the cell's cases within each marketing year and tested with Diebold-Mariano
(HLN correction, h = 1, one-sided: candidate better); Holm over the whole family.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from reva.evaluation.beat_agri import cases as C
from reva.models.tournament import dm_hln_test, holm_rejections

SCHEMA = "reva-beat-agri/1"
REGISTRY_DIR = Path(__file__).resolve().parents[4] / "registry" / "forecasts" / "beat_agri"
SOURCE_FILES = ("cases.py", "service.py")
BURN_IN = (1998, 2004)
DEV = (2005, 2016)
HOLDOUT = (2017, 2024)
PROSPECTIVE_MY = 2026
ALPHA = 0.05
DEV_P = 0.10
MIN_YEARS = 6
COVERAGE_BAND = (0.70, 0.90)
BENCH = {"ae": "wasde", "crps": "wasde"}
Row = tuple[C.Case, dict[str, dict[str, float | None]]]


def split_of(my: int) -> str:
    if DEV[0] <= my <= DEV[1]:
        return "development"
    if HOLDOUT[0] <= my <= HOLDOUT[1]:
        return "holdout"
    return "burn_in" if my < DEV[0] else "not_evaluated"


def code_sha256() -> str:
    h = hashlib.sha256()
    base = Path(__file__).resolve().parent
    for f in SOURCE_FILES:
        h.update(f.encode() + b"\0" + (base / f).read_bytes() + b"\0")
    return h.hexdigest()


def canonical_sha256(doc: Any) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def all_cases(
    v: C.Vintages, w: C.Weather | None, last_my: int = HOLDOUT[1]
) -> tuple[list[C.Case], list[dict[str, Any]]]:
    """Cases from the burn-in year on (candidates need past truths); scored splits are selected afterwards."""
    cs, dropped = C.build_cases(v, first_my=BURN_IN[0], last_my=last_my)
    C.compute_candidates(cs, w)
    return cs, dropped


def _in_cell(c: C.Case, group: str, window: str) -> bool:
    a, b = C.WINDOWS[window]
    return c.series in C.GROUPS[group] and a <= c.k <= b


def paired(rows: Sequence[Row], cand: str, loss: str) -> dict[str, Any]:
    """Candidate vs WASDE (AE: WASDE point; CRPS: WASDE fair fan) on common cases; differential per marketing year."""
    by_y: dict[int, list[float]] = defaultdict(list)
    lc: list[float] = []
    lb: list[float] = []
    cov: list[float] = []
    for c, s in rows:
        a, b = s.get(cand), s.get(BENCH[loss])
        if not a or not b or a.get(loss) is None or b.get(loss) is None:
            continue
        x, z = float(a[loss]), float(b[loss])  # type: ignore[arg-type]
        by_y[c.my].append(x - z)
        lc.append(x)
        lb.append(z)
        if loss == "crps" and a.get("cov80") is not None:
            cov.append(float(a["cov80"]))  # type: ignore[arg-type]
    d = [math.fsum(v) / len(v) for _y, v in sorted(by_y.items())]
    res: dict[str, Any] = {
        "candidate": cand,
        "benchmark": "wasde_point" if loss == "ae" else "wasde_fair_fan",
        "loss": loss,
        "n_cases": len(lc),
        "n_years": len(d),
        "years": sorted(by_y),
        "loss_candidate": math.fsum(lc) / len(lc) if lc else None,
        "loss_benchmark": math.fsum(lb) / len(lb) if lb else None,
        "coverage80": (math.fsum(cov) / len(cov)) if cov else None,
    }
    res["relative"] = res["loss_candidate"] / res["loss_benchmark"] if lc and res["loss_benchmark"] else None
    if len(d) < MIN_YEARS:
        res.update(tested=False, p_better=None, reason=f"fewer than {MIN_YEARS} marketing years")
        return res
    t = dm_hln_test(d, 1)
    res.update(tested=True, dm_stat=t.statistic, p_better=t.p_less, p_worse=t.p_greater)
    return res


def comparisons(rows: Sequence[Row]) -> list[dict[str, Any]]:
    """The full pre-registered grid: every candidate x its cells x {AE, CRPS}."""
    out = []
    for cand, cells in C.CELLS.items():
        for g, w in cells:
            cr = [r for r in rows if _in_cell(r[0], g, w)]
            for loss in ("ae", "crps"):
                t = paired(cr, cand, loss)
                if t["n_cases"]:
                    out.append({"group": g, "window": w, **t})
    return out


def holm(tests: list[dict[str, Any]], alpha: float = ALPHA) -> None:
    pv = {i: float(t["p_better"]) for i, t in enumerate(tests) if t.get("tested")}
    rej = holm_rejections(pv, alpha)
    for i, t in enumerate(tests):
        t["holm_m"] = len(pv)
        t["holm_rejected"] = i in rej


def hyp_key(t: Mapping[str, Any]) -> str:
    return f"{t['group']}|{t['window']}|{t['candidate']}|{t['loss']}"


def select_hypotheses(dev_tests: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Per (group, window, loss): among development tests with relative < 1 and one-sided p < DEV_P, the candidate
    with the lowest relative loss (at most one hypothesis per cell and loss)."""
    best: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for t in dev_tests:
        if not t.get("tested") or t.get("relative") is None or t["relative"] >= 1 or t["p_better"] >= DEV_P:
            continue
        k = (t["group"], t["window"], t["loss"])
        if k not in best or t["relative"] < best[k]["relative"]:
            best[k] = t
    return [
        {
            "key": hyp_key(t),
            "group": t["group"],
            "window": t["window"],
            "candidate": t["candidate"],
            "loss": t["loss"],
            "development_relative": t["relative"],
            "development_p": t["p_better"],
        }
        for _k, t in sorted(best.items())
    ]


def summaries(rows: Sequence[Row]) -> list[dict[str, Any]]:
    """Descriptive: per series x window x model, MAE %, mean CRPS %, 80 % coverage, n (all cases of the rows)."""
    acc: dict[tuple[str, str, str], list[dict[str, float | None]]] = defaultdict(list)
    for c, s in rows:
        for m, sc in s.items():
            acc[(c.series, C.window_of(c.k), m)].append(sc)
    out = []
    for (se, w, m), xs in sorted(acc.items()):
        ae = [float(x["ae"]) for x in xs if x.get("ae") is not None]  # type: ignore[arg-type]
        cr = [float(x["crps"]) for x in xs if x.get("crps") is not None]  # type: ignore[arg-type]
        cv = [float(x["cov80"]) for x in xs if x.get("cov80") is not None]  # type: ignore[arg-type]
        out.append({"series": se, "window": w, "model": m, "n": len(ae),
                    "mae_pct": statistics.fmean(ae) if ae else None,
                    "crps_pct": statistics.fmean(cr) if cr else None,
                    "coverage80": statistics.fmean(cv) if cv else None})  # fmt: skip
    return out


def wasde_by_month(rows: Sequence[Row]) -> list[dict[str, Any]]:
    """Descriptive: WASDE's revision path — MAE % and mean signed relative error (truth - WASDE) per series x k."""
    acc: dict[tuple[str, int], list[C.Case]] = defaultdict(list)
    for c, _s in rows:
        acc[(c.series, c.k)].append(c)
    return [
        {
            "series": se,
            "k": k,
            "n": len(cs),
            "mae_pct": statistics.fmean(100 * abs(c.err) for c in cs),
            "mean_error_pct": statistics.fmean(100 * c.err for c in cs),
        }
        for (se, k), cs in sorted(acc.items())
    ]


def stage_rows(cases: Sequence[C.Case], split: str) -> list[Row]:
    return C.scored([c for c in cases if split_of(c.my) == split])


def development(cases: Sequence[C.Case], pins: Mapping[str, Any]) -> dict[str, Any]:
    rows = stage_rows(cases, "development")
    tests = comparisons(rows)
    holm(tests)
    descriptive_trend = [paired([r for r in rows if r[0].series.endswith(".yield") and C.WINDOWS[w][0] <= r[0].k <= C.WINDOWS[w][1]], "trend", "ae") | {"group": "us_yield", "window": w} for w in C.WINDOWS]  # fmt: skip
    return {
        "schema": SCHEMA,
        "stage": "development",
        "computed_at": datetime.now(UTC).isoformat(),
        "code_sha256": code_sha256(),
        "marketing_years": list(DEV),
        "pins": dict(pins),
        "n_cases": len(rows),
        "n_years": len({r[0].my for r in rows}),
        "family_size": sum(1 for t in tests if t.get("tested")),
        "tests": tests,
        "descriptive": {
            "pure_reva_trend_vs_wasde_us_yield": descriptive_trend,
            "summaries": summaries(rows),
            "wasde_revision_path": wasde_by_month(rows),
        },
    }


def holdout(cases: Sequence[C.Case], hyps: Sequence[Mapping[str, Any]], pins: Mapping[str, Any]) -> dict[str, Any]:
    rows = stage_rows(cases, "holdout")
    results = []
    for h in hyps:
        cr = [r for r in rows if _in_cell(r[0], h["group"], h["window"])]
        results.append(
            {"key": h["key"], "group": h["group"], "window": h["window"], **paired(cr, h["candidate"], h["loss"])}
        )
    holm(results)
    for r in results:
        ok = bool(r.get("holm_rejected")) and r.get("relative") is not None and r["relative"] < 1
        if r["loss"] == "crps":
            cov = r.get("coverage80")
            ok = ok and cov is not None and COVERAGE_BAND[0] <= cov <= COVERAGE_BAND[1]
        r["confirmed"] = ok
    full = comparisons(rows)  # descriptive: the whole grid on the holdout (no selection, no claim)
    return {
        "schema": SCHEMA,
        "stage": "holdout",
        "computed_at": datetime.now(UTC).isoformat(),
        "code_sha256": code_sha256(),
        "marketing_years": list(HOLDOUT),
        "pins": dict(pins),
        "n_cases": len(rows),
        "family_size": sum(1 for r in results if r.get("tested")),
        "results": results,
        "confirmed": [r["key"] for r in results if r["confirmed"]],
        "descriptive": {
            "full_grid_unselected": full,
            "summaries": summaries(rows),
            "wasde_revision_path": wasde_by_month(rows),
            "pure_reva_trend_vs_wasde_us_yield": [
                paired(
                    [
                        r
                        for r in rows
                        if r[0].series.endswith(".yield") and C.WINDOWS[w][0] <= r[0].k <= C.WINDOWS[w][1]
                    ],
                    "trend",
                    "ae",
                )
                | {"window": w}
                for w in C.WINDOWS
            ],
        },
    }


def check_unopened(ledger: Mapping[str, Any], hyp_sha: str) -> None:
    for e in ledger.get("openings") or []:
        if e.get("hypotheses_sha256") == hyp_sha:
            raise RuntimeError("the holdout of these hypotheses was already opened (ledger): refusing a second opening")


def prospective_records(v: C.Vintages, w: C.Weather | None, now: datetime) -> dict[str, Any]:
    """REVA candidates and the WASDE benchmark for marketing year PROSPECTIVE_MY on the WASDE release in force at
    ``now`` (cases inside the pre-registered windows only); the truth is unknown (pending)."""
    rel = v.in_force(now)
    if rel is None:
        raise LookupError("no WASDE release in force")
    cs, _ = C.build_cases(_with_pending_truth(v, PROSPECTIVE_MY), first_my=BURN_IN[0], last_my=PROSPECTIVE_MY)
    C.compute_candidates(cs, w)
    recs = []
    for c in cs:
        if c.my != PROSPECTIVE_MY or c.release != rel.release:
            continue
        recs.append({"series": c.series, "marketing_year": c.my, "k": c.k, "window": C.window_of(c.k),
                     "release": c.release.isoformat(), "available_at": c.t.isoformat(),
                     "wasde_point": c.official, "points": dict(c.points), "sds": dict(c.sds),
                     "truth_rule": truth_rule_text(c.series, c.my), "n_past": c.info.get("n_past")})  # fmt: skip
    return {"release_in_force": rel.release.isoformat(), "records": recs}


PENDING = datetime(9999, 1, 1, tzinfo=UTC)


def _with_pending_truth(v: C.Vintages, my: int) -> C.Vintages:
    """A copy whose truth lookup returns a placeholder (never available) for marketing year ``my`` so that the
    open year's cases can be built; the placeholder value is never scored."""

    class _V(C.Vintages):
        def truth(self, series: str, y: int) -> tuple[float, datetime, Any] | None:
            if y == my:
                return float("nan"), PENDING, None
            return super().truth(series, y)

    return _V(v.releases)


def truth_rule_text(series: str, my: int) -> str:
    m = C.TRUTH_MONTH[series]
    return (
        f"value of {series} for marketing year {my}/{(my + 1) % 100:02d} in the first WASDE released on or after "
        f"{my + 1}-{m:02d}-01 (USDA, as published; ESMIS archive)"
    )


def rounded(obj: Any, digits: int = 6) -> Any:
    if isinstance(obj, float):
        return obj if not math.isfinite(obj) else float(f"{obj:.{digits}g}")
    if isinstance(obj, dict):
        return {k: rounded(v, digits) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [rounded(v, digits) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def write_json(path: Path, doc: Mapping[str, Any]) -> str:
    data = json.dumps(rounded(dict(doc)), indent=1, sort_keys=True, default=str).encode() + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text()) if path.exists() else {}


def append_ledger(path: Path, entry: Mapping[str, Any]) -> None:
    doc = load_yaml(path) or {
        "schema": "reva-beat-agri-holdout-ledger/1",
        "statement": "Append-only: one entry per opening of the BEAT-AGRI holdout. Never edit past entries.",
        "openings": [],
    }
    doc.setdefault("openings", []).append(dict(entry))
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


__all__ = [
    "DEV",
    "HOLDOUT",
    "REGISTRY_DIR",
    "all_cases",
    "append_ledger",
    "canonical_sha256",
    "check_unopened",
    "code_sha256",
    "comparisons",
    "development",
    "holdout",
    "paired",
    "prospective_records",
    "select_hypotheses",
    "split_of",
    "write_json",
]
