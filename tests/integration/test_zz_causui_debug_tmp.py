from test_nlqa_api import *  # noqa
from test_nlqa_api import env, causal_claims  # noqa

def test_dbg(env, causal_claims):
    from reva.causal import graph
    import json
    with env.api.connect() as conn:
        w = graph.why(conn, "Q90000001")
        open("/tmp/claude-0/-home-user-REVA-World/a82f628c-2790-54df-b0b4-28930bdc7a97/scratchpad/why_dbg.json","w").write(json.dumps(w, default=str, indent=1))
    r = env.app.state.nlqa_backends.causal.explain("Q90000001", direction="causes", limit=10)
    open("/tmp/claude-0/-home-user-REVA-World/a82f628c-2790-54df-b0b4-28930bdc7a97/scratchpad/adapter_dbg.txt","w").write(repr(r))
