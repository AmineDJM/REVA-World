"""Runs of the decision studio: read-only contexts, input-hash cache, sensitivity (tornado), A/B comparison.

* :func:`db_contexts` builds the engine's ports on one read-only connection exactly as
  ``reva.predict.run.run_on_connection`` does (effects layer, causal claims, atlas + registry baseline, plugin
  data), wrapped in memo proxies so that repeated engine runs (sensitivity) never re-read the database.
* :func:`compute` runs the V5 engine (``reva.predict.engine.Engine``) and keeps, besides its JSON result, the
  Monte-Carlo draws of every estimate-chain variable and the standard-normal draw of every estimate (the engine's
  named counter-based streams), for the paired comparison and the variance shares.
* :func:`sensitivity` — one at a time: every estimate on a variable's path is set to its 5 % then 95 % quantile
  (normal approximation of the uncertainty its source publishes) while the others stay at their central value;
  the bar is the change of the result. An estimate without published uncertainty gets no bar (it is listed as
  such, never drawn as zero). Complemented by the share of the Monte-Carlo variance each estimate's draw explains
  (r², exact for a single-estimate chain, indicative otherwise).
* :func:`compare` — two scenarios side by side and, for variables both reach with the same draws and seed, the
  paired difference B − A draw by draw (common random numbers: the same estimate gets the same draw in both runs).

Every function reads; nothing is written to the database.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from sqlalchemy import Connection, text

from reva.predict import db as pdb
from reva.predict import engine as pe
from reva.predict.context import PluginData
from reva.predict.decision import Decision, VarRef

log = logging.getLogger(__name__)

OAT_Z = 1.6448536269514722
OAT_DRAWS = 100
STUDIO_VERSION = "reva.v8.decision@1.0.0"


# ------------------------------------------------------------------------------------------- hashing
def _canon(o: Any) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def scenario_hash(decision: Decision, *, draws: int, seed: int, as_of_max: str | None, origin: str) -> str:
    """Hash of the scenario's inputs only (decision + run settings); stable across data loads."""
    doc = {
        "decision": decision.model_dump(mode="json"),
        "draws": draws,
        "seed": seed,
        "as_of_max": as_of_max,
        "origin": origin,
        "engine": pe.ENGINE_ID,
    }
    return hashlib.sha256(_canon(doc).encode()).hexdigest()


# ------------------------------------------------------------------------------------------- memo ports
class MemoEffects:
    """Caches ``by_cause`` / ``variable_info`` of an effect source (one connection's lifetime)."""

    def __init__(self, inner: Any):
        self.inner = inner
        self._by: dict[str, list[dict[str, Any]]] = {}
        self._info: dict[str, dict[str, Any] | None] = {}

    def status(self) -> dict[str, Any]:
        return dict(self.inner.status())

    def by_cause(self, ref: VarRef) -> list[dict[str, Any]]:
        if ref.key not in self._by:
            self._by[ref.key] = list(self.inner.by_cause(ref))
        return self._by[ref.key]

    def variable_info(self, ref: VarRef) -> dict[str, Any] | None:
        if ref.key not in self._info:
            self._info[ref.key] = self.inner.variable_info(ref)
        return self._info[ref.key]

    def atlas_concepts_of(self, ref: VarRef) -> list[str]:
        f = getattr(self.inner, "atlas_concepts_of", None)
        if f is not None:
            return list(f(ref))
        return [ref.id] if ref.kind == "atlas_concept" else []


class MemoBaseline:
    def __init__(self, inner: Any):
        self.inner = inner
        self._m: dict[tuple[str, str], dict[str, Any] | None] = {}

    def baseline(self, ref: VarRef, geo: str) -> dict[str, Any] | None:
        k = (ref.key, geo)
        if k not in self._m:
            self._m[k] = self.inner.baseline(ref, geo)
        return self._m[k]


class MemoClaims:
    def __init__(self, inner: Any):
        self.inner = inner
        self._m: dict[tuple[str, ...], list[dict[str, Any]]] = {}

    def claims_touching(self, ids: Sequence[str], limit_per_id: int = 25) -> list[dict[str, Any]]:
        k = (*ids, str(limit_per_id))
        if k not in self._m:
            self._m[k] = list(self.inner.claims_touching(ids, limit_per_id))
        return self._m[k]


def memo_runner(
    run: Callable[[dict[str, Any]], dict[str, Any]] | None,
) -> Callable[[dict[str, Any]], dict[str, Any]] | None:
    if run is None:
        return None
    cache: dict[str, dict[str, Any]] = {}

    def wrapped(spec: dict[str, Any]) -> dict[str, Any]:
        k = _canon(spec)
        if k not in cache:
            cache[k] = run(spec)
        return cache[k]

    return wrapped


# ------------------------------------------------------------------------------------------- contexts
@dataclass
class Contexts:
    effects: Any
    claims: Any
    baseline: Any
    plugin_data: PluginData | None = None
    extra_gaps: list[dict[str, Any]] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    lookup: Callable[[Sequence[str]], dict[str, dict[str, Any] | None]] = lambda keys: {}
    near: Callable[[str, str], list[dict[str, Any]]] = lambda node, mode: []
    fingerprint: str = "static"


def static_contexts(effects: Any, claims: Any, baseline: Any, plugin_data: PluginData | None = None) -> Contexts:
    """Contexts over in-memory ports (tests, documentation runs): lookup reads the static estimates."""
    estimates = {str(e["estimate_key"]): dict(e) for e in getattr(effects, "_estimates", [])}

    def lookup(keys: Sequence[str]) -> dict[str, dict[str, Any] | None]:
        return {k: estimates.get(k) for k in keys}

    def near(node: str, mode: str) -> list[dict[str, Any]]:
        _kind, _, rest = node.partition(":")
        ident = rest.rpartition("@")[0]
        out = []
        for e in estimates.values():
            c = e.get("cause") or {}
            cid = str(c.get("id"))
            same = cid == ident
            if (mode == "same" and same) or (mode == "related" and not same and cid.startswith(ident)):
                out.append(_near_row(e, same))
        return out[:12]

    return Contexts(
        MemoEffects(effects),
        MemoClaims(claims),
        MemoBaseline(baseline),
        plugin_data,
        lookup=lookup,
        near=near,
        fingerprint=hashlib.sha256(_canon(sorted(estimates)).encode()).hexdigest()[:16],
    )


def _near_row(e: Mapping[str, Any], same: bool) -> dict[str, Any]:
    c, f = e.get("cause") or {}, e.get("effect") or {}
    return {
        "estimate_key": e.get("estimate_key"),
        "cause": f"{c.get('kind')}:{c.get('id')}",
        "cause_transform": c.get("transform"),
        "effect": f"{f.get('kind')}:{f.get('id')}",
        "wording_fr": e.get("wording_fr"),
        "relation": "same_cause" if same else "related_cause",
    }


def data_fingerprint(conn: Connection) -> str:
    """Cheap fingerprint of what a run reads (effect layer size/last row, latest live release)."""
    parts: list[str] = []
    try:
        with conn.begin_nested():
            row = conn.execute(text("SELECT count(*), coalesce(max(id), 0) FROM kg.effect_estimate_current")).one()
        parts.append(f"fx{row[0]}:{row[1]}")
    except Exception:
        parts.append("fx-absent")
    try:
        with conn.begin_nested():
            rel = pdb.latest_release(conn)
        parts.append(f"rel{(rel or {}).get('id')}")
    except Exception:
        parts.append("rel-absent")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


#: Cross-request caches of what is expensive and identical between runs on the same data (keyed by the data
#: fingerprint / atlas snapshot / release; 15-minute time-to-live): the forecast-registry index (loading it
#: re-hashes every sealed batch manifest), per-(variable, country) baselines, the FIGARO IO arrays or their gap.
def registry_paths(conn: Connection, store: Any) -> dict[tuple[str, str], dict[str, dict[str, Any]]]:
    """PRED-REGISTRY's sealed baseline forecasts as paths per (concept, country), in ONE pass over the batches.

    Same selection as ``reva.predict.db.RegistryBaseline.baseline`` (quantity questions with a concept and a
    geography; issued forecasts; preferred model IMF WEO reference, else AR, else persistence; forecasts of a
    supplementary batch added to the original's; period label ``2027-Q1`` → ``2027Q1``), but without one
    ``registry.read.question`` call per question: that call scans every batch manifest and, with more batches
    than the registry's manifest cache holds, re-verifies all of them each time (minutes per scenario)."""
    from reva.evaluation.registry import read as R

    preferred = pdb.RegistryBaseline.PREFERRED
    qinfo: dict[str, dict[str, Any]] = {}
    orig: dict[str, dict[str, dict[str, Any]]] = {}
    supp: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for b in R.batches(conn, None):
        m = R.CACHE.get(store, b["batch_sha256"])
        if m.get("supplement_of"):
            for f in m.get("forecasts") or []:
                if f.get("status") == "issued":
                    supp.setdefault((str(m["supplement_of"]), str(f["question_id"])), {})[str(f["model"])] = {
                        **f,
                        "_batch": b["batch_sha256"],
                    }
            continue
        fresh = set()
        for q in m.get("questions") or []:
            qid = str(q["question_id"])
            if qid in qinfo or q.get("type") != "quantity" or not q.get("concept_id") or not q.get("geo"):
                continue
            qinfo[qid] = {**{k: q.get(k) for k in ("concept_id", "geo", "reference_period", "unit")}, "batch": b}
            fresh.add(qid)
        for f in m.get("forecasts") or []:
            qid = str(f["question_id"])
            if qid in fresh and f.get("status") == "issued":
                orig.setdefault(qid, {})[str(f["model"])] = {**f, "_batch": b["batch_sha256"]}
    out: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for qid, q in qinfo.items():
        fcs = {**orig.get(qid, {}), **supp.get((str(q["batch"].get("batch_sha256")), qid), {})}
        model = next((x for x in preferred if x in fcs and fcs[x].get("point") is not None), None)
        if model is None:
            continue
        label = str(q["reference_period"]).replace("-", "")
        out.setdefault((str(q["concept_id"]), str(q["geo"])), {})[label] = {
            "value": fcs[model]["point"],
            "model": model,
            "question_id": qid,
            "batch_sha256": fcs[model]["_batch"],  # the batch that sealed the chosen forecast
            "unit": q.get("unit"),
        }
    return out


class SharedRegistryBaseline(pdb.RegistryBaseline):
    """``RegistryBaseline`` reading the registry through :func:`registry_paths` (one pass, shared between
    requests for 15 minutes); the fallback (atlas persistence) and the output shape are unchanged."""

    def _paths(self) -> dict[tuple[str, str], dict[str, dict[str, Any]]]:
        hit = _shared().get("registry_paths")
        if hit is not None:
            return hit  # type: ignore[no-any-return]
        try:
            with self.conn.begin_nested():
                paths = registry_paths(self.conn, self.store)
        except Exception as e:
            self.gap = f"forecast registry unavailable: {type(e).__name__}: {str(e)[:160]}"
            return {}
        _shared().put("registry_paths", paths)
        return paths

    def baseline(self, ref: VarRef, geo: str) -> dict[str, Any] | None:
        base = self.fallback.baseline(ref, geo) if self.fallback is not None else None
        concepts = (
            self.effects.atlas_concepts_of(ref)
            if self.effects is not None
            else ([ref.id] if ref.kind == "atlas_concept" else [])
        )
        idx = self._paths()
        path: dict[str, dict[str, Any]] = {}
        for cid in concepts:
            path.update(idx.get((cid, geo), {}))
        if not path:
            return base
        out = dict(base or {"kind": "registry_baseline_forecast", "unit": next(iter(path.values()))["unit"]})
        out["path"] = path
        out["path_label_fr"] = (
            "prévisions de référence scellées du registre (modèles de base), pas des prévisions de REVA"
        )
        return out


class SharedBaseline:
    """Baselines memoised across requests for one data fingerprint and atlas snapshot."""

    def __init__(self, inner: Any, stamp: str):
        self.inner = inner
        self.stamp = stamp

    def baseline(self, ref: VarRef, geo: str) -> dict[str, Any] | None:
        key = f"baseline|{self.stamp}|{ref.key}|{geo}"
        hit = _shared().get(key)
        if hit is not None:
            return hit[0]  # type: ignore[no-any-return]
        val = self.inner.baseline(ref, geo)
        if getattr(self.inner, "gap", None) is None:
            _shared().put(key, (val,))
        return val  # type: ignore[no-any-return]


def _io_for(conn: Connection, store: Any) -> tuple[Any, dict[str, Any], str | None]:
    rel = pdb.latest_release(conn)
    if rel is None:
        return None, {}, "no published live belief release on this database"
    key = f"io|{rel['id']}"
    hit = _shared().get(key)
    if hit is None:
        hit = pdb.io_arrays(conn, store, rel)
        _shared().put(key, hit)
    return hit


def db_contexts(
    conn: Connection, store: Any, decision: Decision, *, origin: str = "any", as_of_max: str | None = None
) -> Contexts:
    """The engine's ports on one read-only connection (same construction as ``run_on_connection``; the registry
    index, baselines and IO table are shared between requests on the same data)."""
    fp = data_fingerprint(conn)
    effects = pdb.DbEffectSource(conn, origin=origin, as_of_max=as_of_max)
    claims = pdb.DbClaimSource(conn, origin=None if origin == "any" else origin)
    atlas, baseline_gap = pdb.atlas_baseline(conn, store, effects)
    baseline = SharedBaseline(
        SharedRegistryBaseline(conn, store, atlas, effects), f"{fp}|{getattr(atlas, 'snapshot', None)}"
    )
    data = pdb.plugin_data(conn, store, decision, with_io=False)
    if data.crude_provenance:  # a live release exists: IO arrays (or their typed gap) from the shared cache
        data.io_arrays, data.io_provenance, data.io_gap = _io_for(conn, store)
    data.retirement_runner = memo_runner(data.retirement_runner)
    extra = [{"code": "baseline_unavailable", "message": baseline_gap}] if baseline_gap else []
    prov = {
        "database_read_only": True,
        "release": data.crude_provenance or None,
        "io": data.io_provenance or None,
        "atlas_snapshot": getattr(atlas, "snapshot", None),
    }

    def lookup(keys: Sequence[str]) -> dict[str, dict[str, Any] | None]:
        out: dict[str, dict[str, Any] | None] = {}
        try:
            import reva.effects as fx
        except ImportError:
            return {k: None for k in keys}
        for k in keys:
            try:
                with conn.begin_nested():
                    out[k] = fx.get_estimate(conn, k, origin=origin)
            except Exception:
                log.warning("estimate %s unreadable", k)
                out[k] = None
        return out

    def near(node: str, mode: str) -> list[dict[str, Any]]:
        _kind, _, rest = node.partition(":")
        ident = rest.rpartition("@")[0]
        if not ident:
            return []
        origins = ["live", "test_fixture"] if origin == "any" else [origin]
        like = ident.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        sql = (
            "SELECT estimate_key FROM kg.effect_estimate_current WHERE origin = ANY(:o) AND "
            + ("cause_id = :id" if mode == "same" else "cause_id LIKE :like ESCAPE '\\' AND cause_id <> :id")
            + " ORDER BY cause_id, estimate_key LIMIT 12"
        )
        try:
            with conn.begin_nested():
                keys = [r[0] for r in conn.execute(text(sql), {"o": origins, "id": ident, "like": like})]
        except Exception:
            return []
        full = lookup(keys)
        return [_near_row(full[k] or {"estimate_key": k}, mode == "same") for k in keys if full.get(k)]

    return Contexts(
        MemoEffects(effects),
        MemoClaims(claims),
        MemoBaseline(baseline),
        data,
        extra_gaps=extra,
        provenance=prov,
        lookup=lookup,
        near=near,
        fingerprint=fp,
    )


# ------------------------------------------------------------------------------------------- compute
@dataclass
class Computed:
    result: dict[str, Any]
    draws: dict[str, np.ndarray]  # variable id -> (n, H) dated or (n,) undated, float64
    z: dict[str, np.ndarray]  # estimate key -> (n,) standard-normal draws used
    n: int
    seed: int


class _FixedZEngine(pe.Engine):
    """The V5 engine with every estimate's standard-normal draw fixed (one-at-a-time sensitivity)."""

    def __init__(self, *a: Any, z_fixed: Mapping[str, float], **kw: Any):
        super().__init__(*a, **kw)
        self._fixed = dict(z_fixed)

    def _zs(self, key: str) -> np.ndarray:
        if key not in self._z:
            self._z[key] = np.full(self.n, float(self._fixed.get(key, 0.0)))
        return self._z[key]


def _engine_draws(eng: pe.Engine) -> dict[str, np.ndarray]:
    out: dict[str, np.ndarray] = {}
    for nid, node in eng.nodes.items():
        if node.depth <= 0:
            continue
        arr = node.dated if node.dated is not None else node.undated
        if arr is not None:
            out[nid] = np.asarray(arr, dtype=np.float64)
    return out


def compute(
    decision: Decision, ctx: Contexts, *, draws: int = 2000, seed: int = 0, as_of_max: str | None = None
) -> Computed:
    t0 = time.perf_counter()
    eng = pe.Engine(
        decision, ctx.effects, ctx.claims, ctx.baseline, ctx.plugin_data, draws=draws, seed=seed, as_of_max=as_of_max
    )
    out = eng.run()
    out["gaps"].extend(ctx.extra_gaps)
    out["provenance"] = {
        **ctx.provenance,
        "compute_seconds": round(time.perf_counter() - t0, 3),
        "data_fingerprint": ctx.fingerprint,
    }
    return Computed(out, _engine_draws(eng), {k: np.asarray(v) for k, v in eng._z.items()}, draws, seed)


def _fixed_run(decision: Decision, ctx: Contexts, z: Mapping[str, float], as_of_max: str | None) -> dict[str, Any]:
    eng = _FixedZEngine(
        decision,
        ctx.effects,
        ctx.claims,
        ctx.baseline,
        ctx.plugin_data,
        draws=OAT_DRAWS,
        seed=0,
        as_of_max=as_of_max,
        z_fixed=z,
    )
    return eng.run()


def _values(v: Mapping[str, Any]) -> dict[str, float | None]:
    if v.get("frequency") == "undated":
        u = v.get("undated") or {}
        return {"undated": u.get("p50")}
    return {str(p["period"]): (None if p.get("missing_reason") else p.get("p50")) for p in v.get("periods") or []}


def _path_keys(v: Mapping[str, Any], hops: Mapping[str, Mapping[str, Any]]) -> list[str]:
    return [
        str(hops[h]["estimate_key"]) for h in v.get("path") or [] if h in hops and hops[h].get("kind") == "estimate"
    ]


def _r2(y: np.ndarray, z: np.ndarray) -> float | None:
    if y.size < 3 or z.size != y.size or not np.isfinite(y).all():
        return None
    sy, sz = float(np.std(y)), float(np.std(z))
    if sy == 0 or sz == 0:
        return None
    r = float(np.corrcoef(y, z)[0, 1])
    return round(r * r, 4)


def sensitivity(
    decision: Decision,
    ctx: Contexts,
    base: Computed,
    *,
    as_of_max: str | None = None,
    only: str | None = None,
    labels: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    labels = labels or {}
    hops = {h["id"]: h for h in base.result.get("hops") or []}
    variables = [v for v in base.result.get("variables") or [] if only is None or v["id"] == only]
    keys = sorted({k for v in variables for k in _path_keys(v, hops)})
    center = _fixed_run(decision, ctx, {}, as_of_max)
    runs = 1
    lo: dict[str, dict[str, Any]] = {}
    hi: dict[str, dict[str, Any]] = {}
    for k in keys:
        lo[k] = _fixed_run(decision, ctx, {k: -OAT_Z}, as_of_max)
        hi[k] = _fixed_run(decision, ctx, {k: OAT_Z}, as_of_max)
        runs += 2

    def by_id(res: Mapping[str, Any]) -> dict[str, dict[str, float | None]]:
        return {v["id"]: _values(v) for v in res.get("variables") or []}

    c_vals = by_id(center)
    lo_vals = {k: by_id(r) for k, r in lo.items()}
    hi_vals = {k: by_id(r) for k, r in hi.items()}
    out_vars = []
    for v in variables:
        vid = v["id"]
        cv = c_vals.get(vid, {})
        periods = list(cv)
        done = [p for p in periods if cv.get(p) is not None]
        head = done[-1] if done else None
        bars: list[dict[str, Any]] = []
        flat: list[dict[str, Any]] = []
        for k in _path_keys(v, hops):
            lv, hv = lo_vals[k].get(vid, {}), hi_vals[k].get(vid, {})
            swing = {
                p: (abs(hv[p] - lv[p]) if hv.get(p) is not None and lv.get(p) is not None else None)  # type: ignore[operator]
                for p in periods
            }
            if all((s or 0.0) <= 1e-12 * max(1.0, abs(cv.get(p) or 0.0)) for p, s in swing.items()):
                flat.append({"estimate_key": k, "short": labels.get(k) or k})
                continue
            r2: dict[str, float | None] = {}
            y = base.draws.get(vid)
            zk = base.z.get(k)
            for i, p in enumerate(periods):
                if y is None or zk is None:
                    r2[p] = None
                elif y.ndim == 1:
                    r2[p] = _r2(y, zk)
                else:
                    r2[p] = _r2(y[:, i], zk) if i < y.shape[1] else None
            bars.append(
                {"estimate_key": k, "short": labels.get(k) or k, "low": lv, "high": hv, "swing": swing, "mc_r2": r2}
            )
        if head is not None:
            bars.sort(key=lambda b: -(b["swing"].get(head) or 0.0))
        out_vars.append(
            {
                "id": vid,
                "label_fr": (v.get("variable") or {}).get("label_fr") or (v.get("variable") or {}).get("id"),
                "geo": v.get("geo"),
                "unit": v.get("unit"),
                "frequency": v.get("frequency"),
                "periods": periods,
                "headline_period": head,
                "center": cv,
                "bars": bars,
                "no_uncertainty": flat,
            }
        )
    return {
        "method": "one_at_a_time",
        "method_fr": (
            "Un à la fois : chaque estimation du chemin est placée à son quantile 5 % puis 95 % (approximation "
            "normale de l'incertitude que publie sa source), les autres restant à leur valeur centrale (z = 0) ; "
            "la barre est l'écart du résultat. Une estimation sans incertitude publiée n'a pas de barre (listée à "
            "part, jamais dessinée comme nulle). r² : part de la variance des tirages Monte-Carlo expliquée par le "
            "tirage de cette estimation (exacte pour une chaîne à une estimation, indicative sinon)."
        ),
        "z": OAT_Z,
        "center_label_fr": "valeurs centrales des estimations (z = 0) : peut différer de la médiane des tirages",
        "variables": out_vars,
        "runs": runs,
        "compute_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


# ------------------------------------------------------------------------------------------- compare
def _q(a: np.ndarray) -> dict[str, float]:
    qs = np.quantile(a, pe.QUANTILES)
    return {
        "p05": pe._r(qs[0]),
        "p25": pe._r(qs[1]),
        "p50": pe._r(qs[2]),
        "p75": pe._r(qs[3]),
        "p95": pe._r(qs[4]),
        "degenerate": bool(np.ptp(a) == 0),
    }


def _periods_of(v: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    if v.get("frequency") == "undated":
        return {"undated": dict(v.get("undated") or {})}
    return {str(p["period"]): dict(p) for p in v.get("periods") or []}


def compare(a: Computed, b: Computed) -> dict[str, Any]:
    va = {v["id"]: v for v in a.result.get("variables") or []}
    vb = {v["id"]: v for v in b.result.get("variables") or []}
    paired_ok = a.n == b.n and a.seed == b.seed
    rows = []
    for vid in [*va, *[x for x in vb if x not in va]]:
        A, B = va.get(vid), vb.get(vid)
        base = A or B or {}
        row: dict[str, Any] = {
            "id": vid,
            "label_fr": (base.get("variable") or {}).get("label_fr") or (base.get("variable") or {}).get("id"),
            "geo": base.get("geo"),
            "unit": base.get("unit"),
            "frequency": base.get("frequency"),
            "in_a": A is not None,
            "in_b": B is not None,
            "periods": [],
        }
        if A is not None and B is not None:
            pa, pb = _periods_of(A), _periods_of(B)
            da, db = a.draws.get(vid), b.draws.get(vid)
            same_shape = da is not None and db is not None and da.shape == db.shape
            same_path_kind = A.get("unit") == B.get("unit") and A.get("frequency") == B.get("frequency")
            for i, p in enumerate([*pa, *[x for x in pb if x not in pa]]):
                qa, qb = pa.get(p) or {}, pb.get(p) or {}
                ent: dict[str, Any] = {
                    "period": p,
                    "a": None
                    if qa.get("missing_reason") or qa.get("p50") is None
                    else {k: qa.get(k) for k in ("p05", "p50", "p95", "degenerate")},
                    "b": None
                    if qb.get("missing_reason") or qb.get("p50") is None
                    else {k: qb.get(k) for k in ("p05", "p50", "p95", "degenerate")},
                    "a_missing": qa.get("missing_reason"),
                    "b_missing": qb.get("missing_reason"),
                    "diff": None,
                    "diff_note_fr": None,
                }
                if ent["a"] is None or ent["b"] is None:
                    ent["diff_note_fr"] = "écart non calculé : valeur manquante dans un des deux scénarios"
                elif not (paired_ok and same_shape and same_path_kind):
                    ent["diff_note_fr"] = (
                        "écart apparié non calculé : tirages ou graine différents, ou sortie de modèle sans tirages"
                    )
                else:
                    assert da is not None and db is not None
                    d = (db - da) if da.ndim == 1 else (db[:, i] - da[:, i])
                    if np.isfinite(d).all():
                        ent["diff"] = _q(d)
                row["periods"].append(ent)
        rows.append(row)
    ga = {(g.get("code"), g.get("node")) for g in a.result.get("gaps") or []}
    gb = {(g.get("code"), g.get("node")) for g in b.result.get("gaps") or []}
    da_doc, db_doc = a.result.get("decision") or {}, b.result.get("decision") or {}
    changed = sorted(
        k
        for k in set(da_doc) | set(db_doc)
        if k not in ("id", "label_fr", "label_en") and da_doc.get(k) != db_doc.get(k)
    )
    return {
        "paired": paired_ok,
        "paired_fr": (
            "écart B − A calculé tirage par tirage (mêmes nombres aléatoires pour une même estimation dans les deux "
            "scénarios)"
            if paired_ok
            else "tirages ou graines différents : seules les valeurs de chaque scénario sont montrées"
        ),
        "changed_fields": changed,
        "variables": rows,
        "only_a_gaps": sorted([list(x) for x in ga - gb], key=str),
        "only_b_gaps": sorted([list(x) for x in gb - ga], key=str),
        "labels": {"a": a.result.get("label"), "b": b.result.get("label")},
    }


# ------------------------------------------------------------------------------------------- cache
class LruCache:
    """Small thread-safe LRU with a time-to-live (in-process; results are recomputed after a restart)."""

    def __init__(self, maxsize: int, ttl: float):
        self.maxsize = maxsize
        self.ttl = ttl
        self._d: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            hit = self._d.get(key)
            if hit is None:
                return None
            if time.monotonic() - hit[0] > self.ttl:
                del self._d[key]
                return None
            self._d.move_to_end(key)
            return hit[1]

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._d[key] = (time.monotonic(), value)
            self._d.move_to_end(key)
            while len(self._d) > self.maxsize:
                self._d.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()

    def __len__(self) -> int:
        return len(self._d)


RUNS = LruCache(24, 1800.0)
SHARED = LruCache(4096, 900.0)


def _shared() -> LruCache:
    return SHARED


SENSITIVITY = LruCache(48, 1800.0)


# ------------------------------------------------------------------------------------------- warm-up
_WARM: dict[str, Any] = {"state": "cold", "detail": None, "seconds": None}
_WARM_LOCK = threading.Lock()


def engine_state() -> dict[str, Any]:
    return dict(_WARM)


def mark_warm() -> None:
    if _WARM["state"] != "warm":
        _WARM.update(state="warm", detail=None)


def start_warmup(engine_getter: Callable[[], Any], store: Any) -> bool:
    """Load the atlas catalog, the live snapshot's query file and the forecast-registry index once, in a
    background thread (read-only). The first engine run of a process otherwise spends 1-2 min on them (integrity
    hash of the query file, re-hash of every sealed registry manifest)."""
    with _WARM_LOCK:
        if _WARM["state"] in ("warming", "warm"):
            return False
        _WARM.update(state="warming", detail=None)

    def work() -> None:
        t0 = time.perf_counter()
        try:
            pe._atlas_catalog()
            with engine_getter().connect() as conn, conn.begin():
                conn.execute(text("SET TRANSACTION READ ONLY"))
                pdb.atlas_baseline(conn, store, None)
                SharedRegistryBaseline(conn, store, None, None)._paths()
                _io_for(conn, store)
            _WARM.update(state="warm", detail=None, seconds=round(time.perf_counter() - t0, 1))
        except Exception as e:  # never fatal: the first run will pay the cost instead
            _WARM.update(state="failed", detail=f"{type(e).__name__}: {str(e)[:200]}")

    threading.Thread(target=work, name="v8-decision-warmup", daemon=True).start()
    return True
