#!/usr/bin/env python3
"""Validate render.yaml against Render's official JSON Schema plus REVA rules.

Default: offline, against the cached schema in contracts/render.schema.json
(retrieval metadata in contracts/render.schema.meta.json).

  --refresh   download the current schema first (stdlib urllib; honours the
              process proxy/CA configuration), update the cache and metadata
  --live      validate against a freshly downloaded schema without caching it

REVA-specific checks (security/operational invariants of the Blueprint):
  * reva-api has preDeployCommand ``python scripts/bootstrap_runtime.py`` and
    dockerCommand ``python scripts/start_api.py``; health path /health/ready;
  * reva-worker runs ``python scripts/start_worker.py`` and receives neither
    REVA_MIGRATION_DATABASE_URL nor DATABASE_URL nor the API role password,
    session secret or admin hash;
  * the migration-owner URL comes only from the database connectionString;
  * the common group selects s3 storage and production mode; one instance each.

Exit status 0 when valid; 1 with a list of problems otherwise.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_URL = "https://render.com/schema/render.yaml.json"
CACHE = ROOT / "contracts" / "render.schema.json"
META = ROOT / "contracts" / "render.schema.meta.json"

API_FORBIDDEN_ON_WORKER = {
    "REVA_MIGRATION_DATABASE_URL", "DATABASE_URL", "REVA_API_DB_PASSWORD", "REVA_SESSION_SECRET",
    "REVA_ADMIN_PASSWORD_HASH",
}


def download_schema() -> bytes:
    req = urllib.request.Request(SCHEMA_URL, headers={"User-Agent": "reva-blueprint-validator"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed https URL
        data: bytes = resp.read(5_000_000)
    json.loads(data)
    return data


def load_schema(mode: str) -> tuple[dict[str, Any], str]:
    if mode in {"refresh", "live"}:
        raw = download_schema()
        if mode == "refresh":
            CACHE.write_bytes(raw)
            meta = json.loads(META.read_text()) if META.exists() else {}
            meta.update({"source_url": SCHEMA_URL,
                         "retrieved_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "sha256": hashlib.sha256(raw).hexdigest()})
            META.write_text(json.dumps(meta, indent=2) + "\n")
        return json.loads(raw), f"{SCHEMA_URL} (downloaded)"
    raw = CACHE.read_bytes()
    meta = json.loads(META.read_text())
    if hashlib.sha256(raw).hexdigest() != meta["sha256"]:
        raise SystemExit("cached render schema does not match its recorded sha256; refresh it")
    return json.loads(raw), f"cached {CACHE.relative_to(ROOT)} retrieved {meta['retrieved_at']}"


def _env_keys(service: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {e["key"]: e for e in service.get("envVars", []) if "key" in e}


def reva_checks(doc: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    services = {s.get("name"): s for s in doc.get("services", [])}
    api, worker = services.get("reva-api"), services.get("reva-worker")
    if api is None or worker is None:
        return ["services reva-api and reva-worker are required"]
    if api.get("type") != "web" or worker.get("type") != "worker":
        problems.append("reva-api must be type web and reva-worker type worker")
    if api.get("preDeployCommand") != "python scripts/bootstrap_runtime.py":
        problems.append("reva-api preDeployCommand must be `python scripts/bootstrap_runtime.py`")
    if api.get("dockerCommand") != "python scripts/start_api.py":
        problems.append("reva-api dockerCommand must be `python scripts/start_api.py`")
    if worker.get("dockerCommand") != "python scripts/start_worker.py":
        problems.append("reva-worker dockerCommand must be `python scripts/start_worker.py`")
    if "preDeployCommand" in worker:
        problems.append("reva-worker must not run a preDeployCommand (no DDL outside the API predeploy)")
    if api.get("healthCheckPath") != "/health/ready":
        problems.append("reva-api healthCheckPath must be /health/ready")
    for svc in (api, worker):
        if svc.get("runtime") != "docker" or svc.get("dockerfilePath") != "./Dockerfile":
            problems.append(f"{svc['name']} must build ./Dockerfile with runtime docker")
        if svc.get("numInstances", 1) != 1:
            problems.append(f"{svc['name']} must run exactly one instance in V1")
    wkeys = _env_keys(worker)
    leaked = sorted(API_FORBIDDEN_ON_WORKER & set(wkeys))
    if leaked:
        problems.append(f"reva-worker must not receive {leaked}")
    akeys = _env_keys(api)
    mig = akeys.get("REVA_MIGRATION_DATABASE_URL")
    if not mig or mig.get("fromDatabase", {}).get("property") != "connectionString":
        problems.append("REVA_MIGRATION_DATABASE_URL must come from the database connectionString")
    for key in ("REVA_API_DB_PASSWORD", "REVA_WORKER_DB_PASSWORD", "REVA_SIM_DB_PASSWORD", "REVA_SESSION_SECRET"):
        if not akeys.get(key, {}).get("generateValue"):
            problems.append(f"{key} must be generated on reva-api (generateValue: true)")
    for key in ("REVA_WORKER_DB_PASSWORD", "REVA_SIM_DB_PASSWORD"):
        ref = wkeys.get(key, {}).get("fromService", {})
        if ref.get("name") != "reva-api" or ref.get("envVarKey") != key:
            problems.append(f"reva-worker {key} must reference reva-api's generated value")
    for key in ("REVA_S3_ACCESS_KEY_ID", "REVA_S3_SECRET_ACCESS_KEY", "REVA_ADMIN_PASSWORD_HASH"):
        target = wkeys if key.startswith("REVA_S3") else akeys
        if key in target and target[key].get("sync") is not False:
            problems.append(f"{key} must be a dashboard-supplied secret (sync: false)")
    for key in ("REVA_S3_ACCESS_KEY_ID", "REVA_S3_SECRET_ACCESS_KEY"):
        if "fromService" in wkeys.get(key, {}):
            problems.append(f"reva-worker {key} must be its own credential, not the API's")
    common = {e["key"]: e.get("value") for g in doc.get("envVarGroups", []) if g.get("name") == "reva-common"
              for e in g.get("envVars", [])}
    expected = {"REVA_ENV": "production", "REVA_OBJECT_BACKEND": "s3", "REVA_AUTONOMY_MODE": "template_only",
                "REVA_LLM_ENABLED": "false"}
    for k, v in expected.items():
        if common.get(k) != v:
            problems.append(f"reva-common {k} must be {v!r}")
    for db in doc.get("databases", []):
        if str(db.get("postgresMajorVersion")) != "17":
            problems.append("database must be PostgreSQL 17")
        if db.get("ipAllowList") != []:
            problems.append("database ipAllowList must be [] (private network only)")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("path", nargs="?", default=str(ROOT / "render.yaml"))
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--refresh", action="store_true")
    g.add_argument("--live", action="store_true")
    args = ap.parse_args(argv)
    mode = "refresh" if args.refresh else "live" if args.live else "cached"
    schema, origin = load_schema(mode)
    doc = yaml.safe_load(Path(args.path).read_text())
    validator = Draft202012Validator(schema)
    errors = [f"schema: /{'/'.join(map(str, e.absolute_path))}: {e.message[:300]}"
              for e in sorted(validator.iter_errors(doc), key=lambda e: list(map(str, e.absolute_path)))]
    errors += [f"reva: {p}" for p in reva_checks(doc)]
    result = {"blueprint": str(args.path), "schema": origin, "valid": not errors, "problems": errors}
    print(json.dumps(result, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
