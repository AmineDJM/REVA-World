"""V8DECISION on the dev database `reva` (READ ONLY transaction; skipped when unreachable or without the effect
layer). The five reference decisions through the studio service: every result unvalidated, the rice ban stops
at its first link with the missing estimate named, the EU ban shows competing estimates side by side, the
one-pass registry baseline equals the V5 adapter's values, and a cached run is fast."""

from __future__ import annotations

import time
from typing import Any

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def db() -> Any:
    from reva.adapters.objects.factory import build_store
    from reva.adapters.postgres.engine import make_engine
    from reva.settings import get_settings

    s = get_settings()
    try:
        eng = make_engine("api", s)
        with eng.connect() as c:
            n = c.execute(text("SELECT count(*) FROM kg.effect_estimate_current")).scalar_one()
    except Exception:
        pytest.skip("dev database not reachable")
    if not n:
        pytest.skip("no effect estimates in the dev database")
    store = build_store(s)
    conn = eng.connect()
    tx = conn.begin()
    conn.execute(text("SET TRANSACTION READ ONLY"))
    yield conn, store
    tx.rollback()
    conn.close()


def _run(db: Any, ref: str) -> dict[str, Any]:
    from reva.predict.decision import reference_decisions
    from reva.v8.decision import runner as rn
    from reva.v8.decision import service as sv

    conn, store = db
    fp = rn.data_fingerprint(conn)
    bundle, _, _ = sv.run(sv.Scenario(reference_decisions()[ref]), fp, lambda d: rn.db_contexts(conn, store, d))
    return bundle


def test_reference_decisions_live(db: Any) -> None:
    from reva.predict.decision import reference_decisions
    from reva.predict.engine import LABEL_UNVALIDATED
    from reva.v8.decision import runner as rn
    from reva.v8.decision import service as sv

    out = {ref: _run(db, ref) for ref in reference_decisions()}
    for b in out.values():
        assert b["result"]["label"] == LABEL_UNVALIDATED and not b["honesty"]["validated"]
        for e in b["graph"]["edges"]:
            if e["kind"] == "estimate":
                assert e["estimate_key"] in b["evidence"]
    rice = out["india_rice_export_ban_2027q1"]["diagnosis"]
    assert not rice["has_result"]
    assert rice["blocking"][0]["node"] == "variable:trade.export_restriction@IND"
    eu = out["eu_ban_russian_oil_2027q1"]
    assert any(c["kind"] == "alternatives" and len(c["estimates"]) >= 2 for c in eu["conflicts"])
    assert eu["diagnosis"]["has_result"]
    # cached path
    conn, store = db
    fp = rn.data_fingerprint(conn)
    t0 = time.perf_counter()
    _, _, hit = sv.run(
        sv.Scenario(reference_decisions()["ecb_plus_50bp_2027q1"]), fp, lambda d: rn.db_contexts(conn, store, d)
    )
    assert hit and time.perf_counter() - t0 < 1.0


def test_one_pass_registry_baseline_matches_v5_adapter_values(db: Any) -> None:
    from reva.predict import db as pdb
    from reva.predict.decision import VarRef
    from reva.v8.decision import runner as rn

    conn, store = db
    paths = rn.registry_paths(conn, store)
    if not paths:
        pytest.skip("no registry baseline forecasts")
    cid, geo = sorted(paths)[0]
    ref = VarRef("atlas_concept", cid)
    new = rn.SharedRegistryBaseline(conn, store, None, None).baseline(ref, geo)
    old = pdb.RegistryBaseline(conn, store, None, None).baseline(ref, geo)
    assert new is not None and old is not None
    # same periods, values and models; the batch named is the one that sealed the chosen forecast
    assert {p: (v["value"], v["model"]) for p, v in new["path"].items()} == {
        p: (v["value"], v["model"]) for p, v in old["path"].items()
    }
