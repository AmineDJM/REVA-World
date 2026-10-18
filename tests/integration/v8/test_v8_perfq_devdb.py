"""V8 PERFQ against the dev database ``reva`` (read-only; skipped when it is not reachable).

1. Every sealed manifest the registry references is rebuilt by the shared index exactly as ``load_frozen_manifest``
   returns it (keys, order, types).
2. ``registry.read.question`` equals the pre-index algorithm (kept verbatim below) on a sample of >= 30 questions
   across every family of every original batch: supplementary batches, ensemble v1/v2, point_only bm_stack
   (BEAT-MACRO), voids, resolutions; ``questions`` listing equals the pre-index listing.
3. The callers switched to the shared index (V8SCORES, V8DOSSIER, V8DECISION, ensemble scoreboard, change feed)
   give the same output as when they read the verified manifests directly.
4. ``GET /v1/forecasts/questions/{id}`` is p95 < 300 ms warm.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import text

from reva.api.auth import Principal

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def ctx() -> dict[str, Any]:
    from reva.adapters.objects.factory import build_store
    from reva.adapters.postgres.engine import make_engine
    from reva.settings import get_settings

    s = get_settings()
    try:
        eng = make_engine("api", s)
        with eng.connect() as c:
            n = c.execute(
                text("SELECT count(*) FROM model.evaluation WHERE task_scope->>'kind' = 'forecast_registry'")
            ).scalar_one()
    except Exception:
        pytest.skip("dev database not reachable")
    if not n:
        pytest.skip("no forecast registry rows in the dev database")
    return {"engine": eng, "store": build_store(s), "settings": s}


@pytest.fixture(scope="module")
def conn(ctx: dict[str, Any]) -> Iterator[Any]:
    with ctx["engine"].connect() as c:
        with c.begin():
            c.execute(text("SET TRANSACTION READ ONLY"))
            yield c


class _Verified:
    """Reads the verified manifest straight from the store on every call (what callers did before the index)."""

    class _M:
        def __init__(self, m: dict[str, Any]) -> None:
            self.m = m

        def manifest(self) -> dict[str, Any]:
            return self.m

        def questions(self) -> list[dict[str, Any]]:
            return list(self.m.get("questions") or [])

    def get(self, store: Any, sha: str) -> Any:
        from reva.evaluation.ledger import load_frozen_manifest

        return self._M(load_frozen_manifest(store, sha))


def _sample(conn: Any, store: Any) -> list[str]:
    """First and last question (by id) of every family of every original batch, every point_only question, every
    void/resolved one, and questions that ensemble v1 and v2 both forecast; plus an unknown id."""
    from reva.evaluation.registry import read as R
    from reva.evaluation.registry.manifest_index import INDEX

    picked: list[str] = []
    ens: dict[str, set[Any]] = defaultdict(set)
    for b in R.batches(conn, None):
        ix = INDEX.get(store, b["batch_sha256"])
        if ix.supplement_of:
            for f in ix.forecasts():
                if f.get("display") == "point_only":
                    picked.append(f["question_id"])
                if f["model"] == "reva.ensemble":
                    ens[f["question_id"]].add(f.get("model_version"))
            continue
        fams: dict[str, list[str]] = defaultdict(list)
        for q in ix.questions():
            fams[q["family"]].append(q["question_id"])
        for ids in fams.values():
            ids.sort()
            picked += [ids[0], ids[-1]]
    picked += sorted(R.outcomes(conn, store, None))
    picked += sorted(q for q, v in ens.items() if len(v) > 1)[:10]
    return [*sorted(set(picked)), "fq_" + "0" * 24]


def _ref_question(conn: Any, store: Any, question_id: str, load: Any) -> dict[str, Any] | None:
    """``registry.read.question`` before V8 PERFQ, verbatim except ``CACHE.get`` -> ``load``."""
    from reva.evaluation.registry.read import _display, _score_rows, batches, outcomes

    for b in batches(conn, None):
        m = load(store, b["batch_sha256"])
        if m.get("supplement_of"):
            continue
        q = next((x for x in m["questions"] if x["question_id"] == question_id), None)
        if q is None:
            continue
        oc = outcomes(conn, store, None, b["batch_sha256"]).get(question_id)
        scores: dict[tuple[str, Any], dict[str, Any]] = {}
        for r in _score_rows(conn, None, b["batch_sha256"]):
            for s in r["metrics"].get("scores", []):
                if s["question_id"] == question_id:
                    scores[(s["model"], s.get("model_version"))] = s
        fcs = [f for f in m["forecasts"] if f["question_id"] == question_id]
        for b2 in batches(conn, None):
            if b2["batch_sha256"] == b["batch_sha256"]:
                continue
            m2 = load(store, b2["batch_sha256"])
            if m2.get("supplement_of") != b["batch_sha256"]:
                continue
            sc2 = {(s["model"], s.get("model_version")): s for r in _score_rows(conn, None, b2["batch_sha256"])
                   for s in r["metrics"].get("scores", []) if s["question_id"] == question_id}  # fmt: skip
            scores.update(sc2)
            fcs += [{**f, "supplement_batch_sha256": b2["batch_sha256"]} for f in m2["forecasts"]
                    if f["question_id"] == question_id]  # fmt: skip
        return {
            "question": q,
            "forecasts": [{**_display(f), "score": scores.get((f["model"], f.get("model_version")))} for f in fcs],
            "outcome": oc,
            "status": (oc or {}).get("status", "pending"),
            "seal": {
                "batch_sha256": b["batch_sha256"], "issued_at": b["issued_at"], "cycle": b["cycle"],
                "origin": b["origin"], "context_id": b["context_id"], "verified": True, "label": m["label"],
                "data_tables": m["data"]["tables"], "code_digest": m.get("code_digest"),
            },
        }  # fmt: skip
    return None


def _ref_questions(conn: Any, store: Any, load: Any, family: str | None = None) -> tuple[list[dict[str, Any]], int]:
    """``registry.read.questions`` before V8 PERFQ (no filters but family), ``CACHE.get`` -> ``load``."""
    from reva.evaluation.registry.read import _summary, batches, outcomes

    out: list[dict[str, Any]] = []
    oc_all = outcomes(conn, store, None)
    listed: set[str] = set()
    for b in batches(conn, None):
        m = load(store, b["batch_sha256"])
        n_fc: dict[str, int] = defaultdict(int)
        for f in m["forecasts"]:
            if f["status"] == "issued":
                n_fc[f["question_id"]] += 1
        for q in m["questions"]:
            if family and q["family"] != family:
                continue
            if q["question_id"] in listed:
                continue
            listed.add(q["question_id"])
            out.append(_summary(q, b, n_fc[q["question_id"]], oc_all.get(q["question_id"])))
    return out, len(out)


def test_index_rebuilds_every_verified_manifest(conn: Any, ctx: dict[str, Any]) -> None:
    from reva.evaluation.ledger import load_frozen_manifest
    from reva.evaluation.registry.manifest_index import INDEX

    shas = list(
        conn.execute(
            text(
                """SELECT DISTINCT evaluation_manifest_sha256 FROM model.evaluation
                   WHERE task_scope->>'kind' IN ('forecast_registry', 'forecast_registry_health')
                   UNION SELECT DISTINCT metrics->>'resolution_sha256' FROM model.evaluation
                   WHERE task_scope->>'kind' = 'forecast_registry' AND metrics->>'resolution_sha256' IS NOT NULL"""
            )
        ).scalars()
    )
    for sha in shas:
        m = load_frozen_manifest(ctx["store"], sha)
        r = INDEX.get(ctx["store"], sha).manifest()
        assert r == m and list(r) == list(m), sha
        assert repr(r) == repr(m), sha  # same types and key order all the way down
    print(f"{len(shas)} manifests; index {INDEX.size_bytes / 1e6:.1f} MB")


def test_question_equals_pre_index_read_on_a_sample(conn: Any, ctx: dict[str, Any]) -> None:
    from reva.api.routes.forecasts import QuestionDetail
    from reva.evaluation.ledger import load_frozen_manifest
    from reva.evaluation.registry import read as R

    store = ctx["store"]
    ids = _sample(conn, store)
    assert len(ids) >= 30
    wanted = set(ids)
    slim: dict[str, dict[str, Any]] = {}  # each manifest restricted to the sample (same order): memory stays small
    for b in R.batches(conn, None):
        m = load_frozen_manifest(store, b["batch_sha256"])
        m["questions"] = [q for q in m["questions"] if q["question_id"] in wanted]
        m["forecasts"] = [f for f in m["forecasts"] if f["question_id"] in wanted]
        slim[b["batch_sha256"]] = m
    seen: dict[str, int] = defaultdict(int)
    for qid in ids:
        got = R.question(conn, store, qid)
        ref = _ref_question(conn, store, qid, lambda _s, sha: slim[sha])
        assert repr(got) == repr(ref), qid
        if got is None:
            continue
        assert QuestionDetail(**got).model_dump_json() == QuestionDetail(**ref).model_dump_json()  # type: ignore[arg-type]
        fs = got["forecasts"]
        seen["questions"] += 1
        seen["supplement"] += any("supplement_batch_sha256" in f for f in fs)
        seen["ensemble_v1_v2"] += {f.get("model_version") for f in fs if f["model"] == "reva.ensemble"} >= {
            None,
            "2.0.0",
        }
        seen["point_only"] += any(f.get("display") == "point_only" and "quantiles" not in f for f in fs)
        seen["void"] += got["status"] == "void"
        seen["outcome"] += got["outcome"] is not None
    print(dict(seen))
    assert seen["questions"] >= 30 and seen["supplement"] and seen["ensemble_v1_v2"] and seen["point_only"]
    assert seen["outcome"]


def test_questions_listing_equals_pre_index_listing(conn: Any, ctx: dict[str, Any]) -> None:
    from reva.evaluation.ledger import load_frozen_manifest
    from reva.evaluation.registry import read as R

    def load(store: Any, sha: str) -> dict[str, Any]:
        return load_frozen_manifest(store, sha)

    store = ctx["store"]
    got = R.questions(conn, store, None, limit=100_000)
    assert repr(got) == repr(_ref_questions(conn, store, load))
    fam = got[0][0]["family"]
    assert R.questions(conn, store, None, family=fam, limit=100_000) == _ref_questions(conn, store, load, fam)


def test_switched_callers_are_unchanged(
    conn: Any, ctx: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from reva.evaluation.registry import manifest_index as MI
    from reva.evaluation.registry.ensemble import scoreboard
    from reva.v8.changes import feed
    from reva.v8.decision.runner import registry_paths
    from reva.v8.dossier import registry_index as DI
    from reva.v8.scores import live as L

    store = ctx["store"]
    monkeypatch.setattr(DI, "DISK", tmp_path)

    def run() -> list[str]:
        monkeypatch.setattr(L, "_SUMMARIES", {})
        DI._MEM.clear()
        for p in tmp_path.glob("*"):
            p.unlink()
        out = [
            scoreboard(conn, store, None),
            registry_paths(conn, store),
            L._main_registry(conn, store, "live"),
            DI.build_index(conn, store, "live"),
            feed.resolutions(conn, datetime(2000, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC), None, store),
        ]
        return [repr(x) for x in out]

    new = run()
    shared = MI.INDEX
    monkeypatch.setattr(MI, "INDEX", _Verified())
    monkeypatch.setattr(DI, "INDEX", MI.INDEX)
    try:
        old = run()
    finally:
        monkeypatch.setattr(MI, "INDEX", shared)
    for name, a, b in zip(["scoreboard", "registry_paths", "scores", "dossier", "feed"], new, old, strict=True):
        assert a == b, name


class _Auth:
    def authenticate(self, request: Request) -> Principal:
        return Principal(username="t", actor="token:v8-perfq-test", method="token", scopes=frozenset({"read"}))


def test_question_route_is_fast_warm(conn: Any, ctx: dict[str, Any]) -> None:
    from reva.api.errors import install_error_handlers
    from reva.api.routes import forecasts

    ids = _sample(conn, ctx["store"])
    app = FastAPI()
    app.state.auth = _Auth()
    app.state.settings = ctx["settings"]
    app.state.engine = lambda: ctx["engine"]
    app.state.forecast_store = ctx["store"]
    install_error_handlers(app)
    app.include_router(forecasts.router)
    ms: list[float] = []
    with TestClient(app) as c:
        assert c.get(f"/v1/forecasts/questions/{ids[0]}").status_code == 200  # warm
        for _ in range(3):
            for qid in ids:
                t = time.perf_counter()
                r = c.get(f"/v1/forecasts/questions/{qid}")
                ms.append((time.perf_counter() - t) * 1000)
                assert r.status_code == (404 if qid == "fq_" + "0" * 24 else 200), r.text
    ms.sort()
    p50, p95 = ms[len(ms) // 2], ms[int(0.95 * (len(ms) - 1))]
    print(f"GET /v1/forecasts/questions/{{id}} warm: n={len(ms)} p50 {p50:.1f} ms p95 {p95:.1f} ms max {ms[-1]:.1f}")
    assert p95 < 300, f"p95 {p95:.1f} ms"
