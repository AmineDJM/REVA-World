from test_nlqa_api import *  # noqa
from test_nlqa_api import env, causal_claims  # noqa

def test_dbg(env, causal_claims):
    from reva.causal import graph
    with env.api.connect() as conn:
        w = graph.why(conn, "Q90000001")
        print("WHY", {k: v for k, v in w.items() if k != "lanes"}, [ (l["lane"], len(l["claims"])) for l in w["lanes"]])
        if w["lanes"]:
            import json; print(json.dumps(w["lanes"][0]["claims"][0], default=str)[:1500])
    print("ADAPTER", env.app.state.nlqa_backends.causal.explain("Q90000001", direction="causes", limit=10))
