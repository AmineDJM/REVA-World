"""pytest plugin: dump JSON bodies of V3 API responses made through TestClient (read-only capture)."""
import json, os, re
import starlette.testclient as tc

OUT = os.environ["UI3_DUMP"]
_orig = tc.TestClient.request

def request(self, method, url, *a, **kw):
    r = _orig(self, method, url, *a, **kw)
    path = str(url)
    if re.search(r"/v1/(kg|atlas|links|ask)\b", path) and r.status_code < 300:
        try:
            body = r.json()
        except Exception:
            return r
        with open(OUT, "a") as f:
            f.write(json.dumps({"method": method, "url": path, "status": r.status_code, "body": body}, default=str) + "\n")
    return r

tc.TestClient.request = request
