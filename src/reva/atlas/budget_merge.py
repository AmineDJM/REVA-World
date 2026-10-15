"""Merge ``registry/atlas/budget_requests/*.yaml`` into ``registry/policies/budgets.yaml`` (ATLASCORE).

Only ATLASCORE edits the budgets policy (V3 roadmap). The merge is deterministic and conservative:

* a request is merged only for a **registered source** (``registry/sources/*.yaml`` ``source_key``):
  the invariant-30 test requires the pinned keys to equal the registered sources, and an unregistered
  key could never be charged anyway. Others are deferred and reported;
* per source: ``max(current limit, requested daily_requests)``, capped at :data:`MAX_PER_SOURCE`;
  several agents asking for the same source get the largest request (never the sum); existing
  limits are never lowered;
* daily totals: the 1.2.0 base totals (2,000 requests, 500 MiB) plus, for every merged key
  that was not in the base, its limit (requests) and its declared bytes per day, capped at
  :data:`MAX_TOTAL_REQUESTS` / :data:`MAX_TOTAL_BYTES` (disk: raw bytes are archived).

``uv run python -m reva.atlas.budget_merge`` prints the plan; ``--write`` rewrites the
``per_source_daily_requests`` and ``daily`` blocks (comments elsewhere kept) and bumps
``policy_version``. Re-pin ``PINNED_POLICY_DIGESTS["budgets.yaml"]`` in
``src/reva/autonomy/policy.py`` afterwards, with a history note (T3 maintainer-style change).
"""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path
from typing import Any

import yaml

from reva.ontology.registry import REGISTRY_ROOT

BUDGETS = REGISTRY_ROOT / "policies" / "budgets.yaml"
REQUESTS_DIR = REGISTRY_ROOT / "atlas" / "budget_requests"
SOURCES_DIR = REGISTRY_ROOT / "sources"

#: budgets-1.2.0 keys and totals (before V3): never lowered by a merge.
BASE_PER_SOURCE = {
    "worldbank.wdi": 200,
    "un.comtrade.public_preview": 100,
    "crossref.works": 50,
    "eurostat.dissemination": 100,
    "eurostat.demography": 300,
    "naturalearth.admin0_110m": 10,
}
BASE_TOTAL_REQUESTS = 2000
BASE_TOTAL_BYTES = 524_288_000
MAX_PER_SOURCE = 5000
MAX_TOTAL_REQUESTS = 20_000
MAX_TOTAL_BYTES = 3 * 1024**3  # 3 GiB/day: raw downloads are archived on a ~20 GB disk
DEFAULT_BYTES_PER_REQUEST = 1_000_000  # when a request declares no byte figure
MARKER = "  # --- V3 atlas/KG sources ---\n"


def _int(v: Any) -> int:
    return int(str(v).replace("_", "").replace("+", "").strip())


def registered_sources(directory: Path = SOURCES_DIR) -> set[str]:
    out = set()
    for p in sorted(directory.glob("*.yaml")):
        doc = yaml.safe_load(p.read_text()) or {}
        if doc.get("source_key"):
            out.add(str(doc["source_key"]))
    return out


def load_requests(directory: Path = REQUESTS_DIR) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Normalised requests ``{agent, file, source_key, daily_requests, bytes_per_day, reason}``."""
    reqs: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for p in sorted(directory.glob("*.y*ml")):
        try:
            doc = yaml.safe_load(p.read_text()) or {}
            agent = str(doc.get("agent") or p.stem)
            items: list[dict[str, Any]] = []
            for r in doc.get("requests") or []:
                items.append(
                    {
                        "source_key": str(r["source_key"]),
                        "daily_requests": _int(r["daily_requests"]),
                        "bytes_per_day": _int(r["expected_bytes_per_day"]) if r.get("expected_bytes_per_day") else None,
                        "reason": str(r.get("reason", "")).strip(),
                    }
                )
            for key, r in (doc.get("sources") or {}).items():  # alternative mapping form
                n = _int(r.get("per_source_daily_requests", r.get("daily_requests")))
                per = r.get("typical_response_bytes")
                items.append(
                    {
                        "source_key": str(key),
                        "daily_requests": n,
                        "bytes_per_day": _int(r["expected_bytes_per_day"])
                        if r.get("expected_bytes_per_day")
                        else (_int(per) * n if per else None),
                        "reason": str(r.get("reason", "")).strip(),
                    }
                )
            bytes_cap = None
            inc = (doc.get("daily_totals_increase") or {}).get("http_bytes_total")
            if inc is not None:
                bytes_cap = _int(inc)
            for it in items:
                if it["daily_requests"] <= 0:
                    raise ValueError(f"{it['source_key']}: daily_requests must be > 0")
                reqs.append({"agent": agent, "file": p.name, **it, "agent_bytes_total": bytes_cap})
        except Exception as e:  # a malformed request file is reported, never half-merged
            errors.append({"file": p.name, "error": str(e)[:300]})
    return reqs, errors


def plan(
    current: dict[str, Any], requests: list[dict[str, Any]], registered: set[str]
) -> tuple[dict[str, int], int, int, list[dict[str, Any]], list[dict[str, Any]]]:
    """(per_source, total_requests, total_bytes, merged, deferred)."""
    per = {**BASE_PER_SOURCE, **{k: int(v) for k, v in (current.get("per_source_daily_requests") or {}).items()}}
    merged: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    asked: dict[str, dict[str, Any]] = {}
    for r in requests:
        if r["source_key"] not in registered:
            deferred.append({**r, "why": "source_key not registered in registry/sources/*.yaml"})
            continue
        prev = asked.get(r["source_key"])
        if prev is None or r["daily_requests"] > prev["daily_requests"]:
            asked[r["source_key"]] = r
        merged.append(r)
    for key, r in asked.items():
        per[key] = max(per.get(key, 0), min(r["daily_requests"], MAX_PER_SOURCE))
    per = {k: v for k, v in per.items() if k in registered or k in BASE_PER_SOURCE}
    new_keys = [k for k in per if k not in BASE_PER_SOURCE]
    total_req = BASE_TOTAL_REQUESTS + sum(per[k] for k in new_keys)
    # bytes: per agent, the smaller of its declared total increase and the sum of its merged per-source figures
    by_agent: dict[str, int] = {}
    caps: dict[str, int | None] = {}
    for key in new_keys:
        r = asked.get(key)
        if r is None:
            continue
        b = r["bytes_per_day"] if r["bytes_per_day"] is not None else r["daily_requests"] * DEFAULT_BYTES_PER_REQUEST
        by_agent[r["agent"]] = by_agent.get(r["agent"], 0) + b
        caps[r["agent"]] = r["agent_bytes_total"]
    extra_bytes = sum(min(v, caps[a]) if caps[a] is not None else v for a, v in by_agent.items())
    total_bytes = BASE_TOTAL_BYTES + extra_bytes
    return (
        dict(sorted(per.items(), key=lambda kv: (kv[0] not in BASE_PER_SOURCE, kv[0]))),
        min(max(total_req, int((current.get("daily") or {}).get("http_requests_total", 0))), MAX_TOTAL_REQUESTS),
        min(max(total_bytes, int((current.get("daily") or {}).get("http_bytes_total", 0))), MAX_TOTAL_BYTES),
        merged,
        deferred,
    )


def render(
    text: str, per: dict[str, int], total_req: int, total_bytes: int, asked: list[dict[str, Any]], version: str
) -> str:
    """Rewrite the version, the ``daily`` block and the ``per_source_daily_requests`` block."""
    text = re.sub(r"^policy_version: .*$", f'policy_version: "{version}"', text, count=1, flags=re.M)
    text = re.sub(r"^  http_requests_total: .*$", f"  http_requests_total: {total_req}", text, count=1, flags=re.M)
    text = re.sub(r"^  http_bytes_total: .*$", f"  http_bytes_total: {total_bytes}", text, count=1, flags=re.M)
    m = re.search(r"^per_source_daily_requests:\n((?:  .*\n|\s*\n)*)", text, flags=re.M)
    if m is None:
        raise ValueError("budgets.yaml has no per_source_daily_requests block")
    block = m.group(1)
    base_region = block.split(MARKER, 1)[0]  # reviewed pre-V3 lines (and their comments), kept verbatim
    for key in BASE_PER_SOURCE:
        base_region = re.sub(rf"^  {re.escape(key)}: *\d+", f"  {key}: {per[key]}", base_region, flags=re.M)
    by_key: dict[str, list[dict[str, Any]]] = {}
    for r in asked:
        by_key.setdefault(r["source_key"], []).append(r)
    lines = [base_region.rstrip("\n") + "\n"]
    new = [k for k in per if k not in BASE_PER_SOURCE]
    if new:
        lines.append(MARKER)
        lines.append(
            "  # Merged by ATLASCORE (src/reva/atlas/budget_merge.py) from registry/atlas/budget_requests/*.yaml:\n"
            "  # max of the requests per registered source, capped at 5000/day; reasons in the request files.\n"
        )
    for k in new:
        who = ", ".join(sorted({f"{r['agent']} ({r['file']})" for r in by_key.get(k, [])})) or "earlier merge"
        lines.append(f"  # {k}: requested by {who}\n")
        lines.append(f"  {k}: {per[k]}\n")
    return text[: m.start(1)] + "".join(lines) + text[m.end(1) :]


def bump(version: str) -> str:
    m = re.match(r"^budgets-(\d+)\.(\d+)\.(\d+)$", version)
    if not m:
        return "budgets-1.3.0"
    major, minor, patch = (int(x) for x in m.groups())
    return f"budgets-{major}.3.0" if (major, minor) < (1, 3) else f"budgets-{major}.{minor}.{patch + 1}"


def main(argv: list[str] | None = None) -> None:  # pragma: no cover - maintenance entry point
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)
    text = BUDGETS.read_text()
    current = yaml.safe_load(text)
    reqs, errors = load_requests()
    registered = registered_sources()
    per, treq, tbytes, merged, deferred = plan(current, reqs, registered)
    changed = (
        per != current.get("per_source_daily_requests")
        or treq != current["daily"]["http_requests_total"]
        or (tbytes != current["daily"]["http_bytes_total"])
    )
    version = bump(current["policy_version"]) if changed else current["policy_version"]
    print(f"version {current['policy_version']} -> {version}; totals {treq} requests, {tbytes} bytes")
    for k, v in per.items():
        print(f"  {k}: {v}" + ("" if current.get("per_source_daily_requests", {}).get(k) == v else "   (changed)"))
    for d in deferred:
        print(f"  DEFERRED {d['source_key']} ({d['agent']}): {d['why']}")
    for e in errors:
        print(f"  ERROR {e['file']}: {e['error']}")
    missing = sorted(registered - set(per))
    if missing:
        print(f"  registered sources without any budget request: {missing}")
    if args.write and changed:
        out = render(text, per, treq, tbytes, merged, version)
        yaml.safe_load(out)
        BUDGETS.write_text(out)
        print(f"wrote {BUDGETS} sha256 {hashlib.sha256(out.encode()).hexdigest()}")


if __name__ == "__main__":  # pragma: no cover
    main()
