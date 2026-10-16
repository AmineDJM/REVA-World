import json, random, time, uuid
from datetime import UTC, datetime
import pytest
from reva.kg.model import KgProperty, KgStatement, load_batch, register_properties
pytestmark = pytest.mark.db
OUT = "/tmp/claude-0/-home-user-REVA-World/a82f628c-2790-54df-b0b4-28930bdc7a97/scratchpad/perf.json"

def test_perf(db):
    from reva.causal import load_kg_claims, grade_claims
    from reva.causal import graph as cg
    register_properties(db.worker, [KgProperty(p, "item", n, n) for p, n in [("P828","has_cause"),("P1542","has_effect"),("P1478","x1"),("P1536","x2"),("P1479","x3"),("P1537","x4")]] + [KgProperty(p, "time", n, n) for p, n in [("P580","s"),("P582","e"),("P585","p"),("P571","i")]])
    rnd = random.Random(7)
    N_NODES, N_EDGES = 20000, 50000
    sts = []
    for i in range(N_NODES):
        y = rnd.randint(-500, 2000)
        if rnd.random() < 0.8:
            sts.append(KgStatement(f"pf:n{i}", "P580", value={"type": "time", "time": f"{y:+05d}-00-00T00:00:00Z", "precision": 9}, statement_id=f"d{i}"))
    for j in range(N_EDGES):
        a, b = rnd.randrange(N_NODES), rnd.randrange(N_NODES)
        if j % 50 == 0: b = rnd.randrange(20)  # hubs
        pid = rnd.choice(["P828", "P1542", "P1537"])
        refs = [{"P854": [f"https://ex.org/{rnd.randrange(5000)}"]}] if rnd.random() < 0.5 else []
        sts.append(KgStatement(f"pf:n{a}", pid, f"pf:n{b}", statement_id=f"e{j}", references=refs))
    raw = db.store.put_bytes(f"perf {uuid.uuid4()}".encode(), "raw", "application/json").sha256
    out = {}
    t = time.perf_counter()
    load_batch(db.worker, db.store, statements=sts, source_key="wikidata.perf", raw_sha256s=[raw], retrieved_at=datetime.now(UTC), origin="live")
    out["kg_load_s"] = time.perf_counter() - t
    t = time.perf_counter(); r = load_kg_claims(db.worker, origin="live"); out["claims_load_s"] = time.perf_counter() - t; out["claims"] = r.inserted
    t = time.perf_counter(); r = load_kg_claims(db.worker, origin="live"); out["claims_reload_unchanged_s"] = time.perf_counter() - t
    t = time.perf_counter(); g = grade_claims(db.worker, origin="live"); out["grade_s"] = time.perf_counter() - t; out["by_grade"] = g.by_grade
    t = time.perf_counter(); g = grade_claims(db.worker, origin="live"); out["regrade_unchanged_s"] = time.perf_counter() - t; out["regrade_written"] = g.written
    times = []; found = 0
    with db.api.connect() as c:
        for k in range(30):
            a, b = rnd.randrange(20, N_NODES), rnd.randrange(20, N_NODES)
            t = time.perf_counter(); res = cg.chain(c, f"pf:n{a}", f"pf:n{b}", max_hops=6, min_grade="E"); times.append(time.perf_counter() - t)
            found += res["found"]
        times.sort(); out["chain6_p50_ms"] = times[15]*1000; out["chain6_p95_ms"] = times[28]*1000; out["chain6_max_ms"] = times[-1]*1000; out["chain_found"] = found
        t = time.perf_counter(); cs = cg.consistency(c, origin="live"); out["consistency_s"] = time.perf_counter() - t
        out["cycles"] = cs["cycles"]["count"]; out["temporal_violations"] = cs["temporal_violations"]["count"]; out["self_loops"] = cs["self_loops"]["count"]
        t = time.perf_counter(); cg.causes(c, "pf:n3", limit=500); out["causes_hub_ms"] = (time.perf_counter() - t)*1000
        t = time.perf_counter(); cg.why(c, "pf:n3"); out["why_hub_ms"] = (time.perf_counter() - t)*1000
        t = time.perf_counter(); cg.event(c, "pf:n3"); out["event_hub_ms"] = (time.perf_counter() - t)*1000
        out["db_size_causal_mb"] = c.exec_driver_sql("SELECT sum(pg_total_relation_size(oid))/1e6 FROM pg_class WHERE relname LIKE 'causal_%' AND relkind IN ('r','i')").scalar()
    json.dump(out, open(OUT, "w"), indent=1, default=str)
