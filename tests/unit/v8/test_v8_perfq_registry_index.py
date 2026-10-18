"""V8 PERFQ: the shared verified-manifest index behind ``registry.read`` (no database).

The registry read side must answer exactly what it answered before the index (the pre-index ``question`` and
``questions`` are kept verbatim below as the reference), verify each manifest once per sha256, refuse a tampered
manifest every time, and see new batches immediately (nothing cached across the batch list)."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from reva.evaluation.ledger import load_frozen_manifest
from reva.evaluation.registry import manifest_index as MI
from reva.evaluation.registry import read as R
from reva.kernel.errors import IntegrityFailure
from reva.kernel.ids import canonical_bytes


class Store:
    def __init__(self) -> None:
        self.objs: dict[str, bytes] = {}
        self.gets = 0

    def put(self, manifest: dict[str, Any]) -> str:
        data = canonical_bytes(manifest)
        sha = hashlib.sha256(data).hexdigest()
        self.objs[sha] = data
        return sha

    def get(self, namespace: str, sha: str) -> bytes:
        assert namespace == "evaluation"
        self.gets += 1
        return self.objs[sha]


def _q(qid: str, family: str = "weo.gdp_growth", geo: str = "FRA", **kw: Any) -> dict[str, Any]:
    return {
        "question_id": qid, "question_key": f"k-{qid}", "cycle": "2026-Q4", "family": family, "concept_id": "c",
        "label_fr": "Croissance", "label_en": "Growth", "geo": geo, "geo_label_fr": "France", "geo_label_en": "France",
        "reference_period": "2027", "type": "quantity", "unit": "%", "text_fr": "t", "text_en": "t",
        "resolution": {"expected_date": "2027-04-15", "void_after": "2028-01-01", "source": "weo"}, **kw,
    }  # fmt: skip


def _f(qid: str, model: str, point: float, **kw: Any) -> dict[str, Any]:
    return {
        "question_id": qid, "model": model, "schema": 1, "status": "issued", "point": point,
        "quantiles": {"0.05": Decimal("0.1"), "0.95": point + 1.5}, "predictive": {"kind": "normal"}, **kw,
    }  # fmt: skip


def _manifest(qs: list[dict[str, Any]], fcs: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    return {
        "schema": 1, "label": "BASELINE forecasts only", "cycle": "2026-Q4", "origin": "test_fixture",
        "issued_at": datetime(2026, 10, 1, tzinfo=UTC), "data": {"tables": [{"key": "weo", "version": 3}]},
        "code_digest": "abc", "questions": qs, "forecasts": fcs, **kw,
    }  # fmt: skip


class Registry:
    """A fake registry: manifests in a store plus the evaluation rows ``batches``/``_score_rows`` would read."""

    def __init__(self) -> None:
        self.store = Store()
        self.batch_rows: list[dict[str, Any]] = []
        self.scores: list[dict[str, Any]] = []
        self.t = 0
        self.base = self.older = ""

    def seal(self, m: dict[str, Any]) -> str:
        sha = self.store.put(m)
        self.t += 1
        self.batch_rows.insert(0, {  # newest first, like ``batches``
            "batch_sha256": sha, "context_id": f"ctx-{self.t}", "cycle": m["cycle"], "origin": m["origin"],
            "issued_at": m["issued_at"].isoformat(), "recorded_at": self.t, "n_questions": len(m["questions"]),
            "by_type": {}, "label": m["label"], "supplement_of": m.get("supplement_of"), "models": [],
        })  # fmt: skip
        return sha

    def batches(self, conn: Any, origin: str | None) -> list[dict[str, Any]]:
        return [dict(b) for b in self.batch_rows]

    def score_rows(self, conn: Any, origin: str | None, batch_sha: str | None = None) -> list[dict[str, Any]]:
        return [r for r in self.scores if batch_sha is None or r["task_scope"].get("batch_sha256") == batch_sha]


# ------------------------------------------------------------------ the pre-index read side, verbatim (reference)


def ref_question(reg: Registry, question_id: str) -> dict[str, Any] | None:
    conn, store = None, reg.store
    batches, _score_rows, CACHE = reg.batches, reg.score_rows, _Loader(store)

    def outcomes(conn: Any, store: Any, origin: Any, batch_sha: Any = None) -> dict[str, dict[str, Any]]:
        shas = sorted({r["metrics"].get("resolution_sha256") for r in _score_rows(conn, origin, batch_sha)} - {None})
        out: dict[str, dict[str, Any]] = {}
        for sha in shas:
            doc = CACHE.get(store, sha)
            for o in doc.get("outcomes", []):
                out[o["question_id"]] = {**o, "resolution_sha256": sha, "resolved_at": doc.get("resolved_at")}
        return out

    for b in batches(conn, None):
        m = CACHE.get(store, b["batch_sha256"])
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
            m2 = CACHE.get(store, b2["batch_sha256"])
            if m2.get("supplement_of") != b["batch_sha256"]:
                continue
            sc2 = {(s["model"], s.get("model_version")): s for r in _score_rows(conn, None, b2["batch_sha256"])
                   for s in r["metrics"].get("scores", []) if s["question_id"] == question_id}  # fmt: skip
            scores.update(sc2)
            fcs += [{**f, "supplement_batch_sha256": b2["batch_sha256"]} for f in m2["forecasts"]
                    if f["question_id"] == question_id]  # fmt: skip
        return {
            "question": q,
            "forecasts": [{**R._display(f), "score": scores.get((f["model"], f.get("model_version")))} for f in fcs],
            "outcome": oc,
            "status": (oc or {}).get("status", "pending"),
            "seal": {
                "batch_sha256": b["batch_sha256"], "issued_at": b["issued_at"], "cycle": b["cycle"],
                "origin": b["origin"], "context_id": b["context_id"], "verified": True, "label": m["label"],
                "data_tables": m["data"]["tables"], "code_digest": m.get("code_digest"),
            },
        }  # fmt: skip
    return None


def ref_questions(reg: Registry, **kw: Any) -> tuple[list[dict[str, Any]], int]:
    loader = _Loader(reg.store)
    oc_all = R._outcomes_of(reg.store, reg.score_rows(None, None))
    out: list[dict[str, Any]] = []
    listed: set[str] = set()
    for b in reg.batches(None, None):
        m = loader.get(reg.store, b["batch_sha256"])
        n_fc: dict[str, int] = defaultdict(int)
        for f in m["forecasts"]:
            if f["status"] == "issued":
                n_fc[f["question_id"]] += 1
        for q in m["questions"]:
            if kw.get("family") and q["family"] != kw["family"]:
                continue
            if q["question_id"] in listed:
                continue
            listed.add(q["question_id"])
            out.append(R._summary(q, b, n_fc[q["question_id"]], oc_all.get(q["question_id"])))
    return out, len(out)


class _Loader:
    def __init__(self, store: Store) -> None:
        self.store = store

    def get(self, store: Any, sha: str) -> dict[str, Any]:
        return load_frozen_manifest(store, sha)


# ------------------------------------------------------------------ fixtures


@pytest.fixture(autouse=True)
def fresh_index(monkeypatch: pytest.MonkeyPatch) -> MI.Index:
    ix = MI.Index()
    monkeypatch.setattr(MI, "INDEX", ix)
    monkeypatch.setattr(R, "INDEX", ix)
    monkeypatch.setattr(R, "CACHE", R.ManifestCache())
    return ix


@pytest.fixture()
def reg(monkeypatch: pytest.MonkeyPatch) -> Registry:
    g = Registry()
    base = g.seal(_manifest(
        [_q("fq_a"), _q("fq_b", family="weo.unemployment"), _q("fq_c", geo="DEU"), _q("fq_a", label_fr="doublon")],
        [_f("fq_a", "forecast.registry_ar", 1.0), _f("fq_b", "forecast.registry_ar", 7.5),
         _f("fq_a", "forecast.registry_persistence", 1.2), {**_f("fq_c", "forecast.registry_ar", 2.0),
         "status": "abstained", "reason": "short"}, _f("fq_a", "forecast.reference_imf_weo", 1.1)],
    ))  # fmt: skip
    older = g.seal(_manifest([_q("fq_a", label_fr="plus ancien")], [_f("fq_a", "forecast.registry_ar", 9.0)]))
    g.batch_rows.append(g.batch_rows.pop(0))  # recorded first: last in the newest-first order
    g.seal(_manifest([_q("fq_a")], [_f("fq_a", "reva.ensemble", 1.05)], supplement_of=base,
                     label="REVA ENSEMBLE v1"))  # fmt: skip
    g.seal(_manifest([_q("fq_a")], [_f("fq_a", "reva.ensemble", 1.07, model_version="2.0.0")], supplement_of=base,
                     label="REVA ENSEMBLE v2"))  # fmt: skip
    g.seal(_manifest([_q("fq_b", family="weo.unemployment")],
                     [_f("fq_b", "forecast.bm_stack", 7.1, display="point_only", interval_status="sealed")],
                     supplement_of=base, label="BEAT-MACRO"))  # fmt: skip
    res = g.store.put({
        "schema": 1, "resolved_at": "2026-10-17T20:49:30+00:00",
        "outcomes": [{"question_id": "fq_a", "status": "resolved", "value": Decimal("1.3")},
                     {"question_id": "fq_c", "status": "void", "reason": "fiscal_year_basis_mismatch"}],
    })  # fmt: skip
    g.scores.append({"task_scope": {"batch_sha256": base}, "model_key": "forecast.registry_ar", "recorded_at": 1,
                     "metrics": {"resolution_sha256": res, "scores": [
                         {"question_id": "fq_a", "model": "forecast.registry_ar", "abs_error": 0.3}]}})  # fmt: skip
    g.scores.append({"task_scope": {"batch_sha256": g.batch_rows[1]["batch_sha256"]}, "model_key": "reva.ensemble",
                     "recorded_at": 2, "metrics": {"resolution_sha256": res, "scores": [
                         {"question_id": "fq_a", "model": "reva.ensemble", "model_version": "2.0.0",
                          "abs_error": 0.23}]}})  # fmt: skip
    g.base, g.older = base, older
    monkeypatch.setattr(R, "batches", g.batches)
    monkeypatch.setattr(R, "_score_rows", g.score_rows)
    return g


# ------------------------------------------------------------------ tests


def test_index_rebuilds_the_verified_manifest_exactly(reg: Registry) -> None:
    for sha in [b["batch_sha256"] for b in reg.batch_rows] + [s["metrics"]["resolution_sha256"] for s in reg.scores]:
        m = load_frozen_manifest(reg.store, sha)  # type: ignore[arg-type]
        ix = MI.build(m, sha)
        assert ix.manifest() == m and list(ix.manifest()) == list(m)  # same keys, same order, same types
        assert ix.questions() == m.get("questions", []) and ix.forecasts() == m.get("forecasts", [])
        assert ix.header() == {k: v for k, v in m.items() if k not in ("questions", "forecasts")}
    m = load_frozen_manifest(reg.store, reg.base)  # type: ignore[arg-type]
    ix = MI.build(m, "x")
    q, fcs = ix.get("fq_a")
    assert q is not None and q == m["questions"][0] and q["label_fr"] == "Croissance"  # first match, not the duplicate
    assert [f["model"] for f in fcs] == ["forecast.registry_ar", "forecast.registry_persistence",
                                         "forecast.reference_imf_weo"]  # fmt: skip
    assert isinstance(fcs[0]["quantiles"]["0.05"], Decimal)
    assert ix.n_issued == {"fq_a": 3, "fq_b": 1}
    assert ix.get("fq_zzz") == (None, [])
    a, b = ix.get("fq_a")[1], ix.get("fq_a")[1]
    a[0]["point"] = -1
    assert b[0]["point"] == 1.0  # fresh objects on every read


@pytest.mark.parametrize("qid", ["fq_a", "fq_b", "fq_c", "fq_missing"])
def test_question_is_identical_to_the_pre_index_read(reg: Registry, qid: str) -> None:
    got = R.question(None, reg.store, qid)
    assert got == ref_question(reg, qid)
    assert repr(got) == repr(ref_question(reg, qid))  # same key order and types all the way down


def test_question_semantics_kept(reg: Registry) -> None:
    d = R.question(None, reg.store, "fq_a")
    assert d is not None
    assert d["seal"]["batch_sha256"] == reg.base  # newest original batch wins
    ens = [f for f in d["forecasts"] if f["model"] == "reva.ensemble"]
    assert [(f.get("model_version"), f["point"]) for f in ens] == [("2.0.0", 1.07), (None, 1.05)]  # v1/v2 apart
    assert ens[0]["score"] == {"question_id": "fq_a", "model": "reva.ensemble", "model_version": "2.0.0",
                               "abs_error": 0.23} and ens[1]["score"] is None  # fmt: skip
    assert d["status"] == "resolved" and d["outcome"]["value"] == Decimal("1.3")
    b = R.question(None, reg.store, "fq_b")
    assert b is not None
    pt = next(f for f in b["forecasts"] if f["model"] == "forecast.bm_stack")
    assert "quantiles" not in pt and "predictive" not in pt and pt["interval_status"] == "sealed"  # BEAT-MACRO
    c = R.question(None, reg.store, "fq_c")
    assert c is not None and c["status"] == "void"


def test_questions_listing_is_identical(reg: Registry) -> None:
    assert R.questions(None, reg.store, None, limit=1000) == ref_questions(reg)
    assert R.questions(None, reg.store, None, family="weo.unemployment") == ref_questions(
        reg, family="weo.unemployment"
    )


def test_each_manifest_is_verified_once_then_served_from_the_index(reg: Registry, fresh_index: MI.Index) -> None:
    R.question(None, reg.store, "fq_a")
    n_batches = len(reg.batch_rows)
    assert fresh_index.builds == n_batches
    gets = reg.store.gets
    for qid in ("fq_a", "fq_b", "fq_c", "fq_missing") * 5:
        R.question(None, reg.store, qid)
    assert fresh_index.builds == n_batches
    assert reg.store.gets - gets == 0  # resolution manifest stays in ManifestCache, batches in the index


def test_tampered_manifest_is_refused_every_time_and_never_cached(reg: Registry, fresh_index: MI.Index) -> None:
    sha = reg.base
    good = reg.store.objs[sha]
    reg.store.objs[sha] = good.replace(b"7.5", b"7.6")
    for _ in range(2):
        with pytest.raises(IntegrityFailure):
            R.question(None, reg.store, "fq_a")
    assert fresh_index.peek(sha) is None
    reg.store.objs[sha] = good
    assert R.question(None, reg.store, "fq_a") == ref_question(reg, "fq_a")


def test_a_new_batch_or_resolution_is_visible_immediately(reg: Registry) -> None:
    assert R.question(None, reg.store, "fq_new") is None
    new = reg.seal(_manifest([_q("fq_new")], [_f("fq_new", "forecast.registry_ar", 3.0)]))
    d = R.question(None, reg.store, "fq_new")
    assert d is not None and d["seal"]["batch_sha256"] == new and d["status"] == "pending"
    sup = reg.seal(_manifest([_q("fq_b")], [_f("fq_b", "forecast.weo_conformal", 7.0)],
                             supplement_of=reg.base))  # fmt: skip
    b = R.question(None, reg.store, "fq_b")
    assert b is not None and any(f.get("supplement_batch_sha256") == sup for f in b["forecasts"])
    assert b == ref_question(reg, "fq_b")
    res = reg.store.put({"schema": 1, "resolved_at": "2026-11-01T00:00:00+00:00",
                         "outcomes": [{"question_id": "fq_new", "status": "void", "reason": "x"}]})  # fmt: skip
    reg.scores.append({"task_scope": {"batch_sha256": new}, "model_key": "m", "recorded_at": 3,
                       "metrics": {"resolution_sha256": res, "scores": []}})  # fmt: skip
    d = R.question(None, reg.store, "fq_new")
    assert d is not None and d["status"] == "void"
    assert d == ref_question(reg, "fq_new")


def test_header_only_manifest_scans_like_the_manifest() -> None:
    m = {"schema": 1, "outcomes": [{"question_id": "fq_a", "status": "void"}]}
    ix = MI.build(m, "s")
    assert ix.manifest() == m and ix.questions() == [] and ix.forecasts() == [] and ix.get("fq_a") == (None, [])
