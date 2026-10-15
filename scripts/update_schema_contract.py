#!/usr/bin/env python3
"""Regenerate (or check) migrations/schema_contract.json.

Creates a throwaway database ``reva_contract_<random>`` on the cluster named by
REVA_MIGRATION_DATABASE_URL, bootstraps it (fresh ``alembic upgrade head`` +
runtime role provisioning), fingerprints its catalog with
``reva.adapters.postgres.schema_contract`` and drops it. The database named in
the URL (e.g. the dev database ``reva``) is never read or written.

    uv run python scripts/update_schema_contract.py           # write the contract
    uv run python scripts/update_schema_contract.py --check   # exit 1 if stale

Run it after any change to migrations/versions/*.py or adapters/postgres/ddl.py
and commit the result together with the migration change.
"""

from __future__ import annotations

import argparse
import json
import sys

from reva.adapters.postgres.schema_contract import (
    CONTRACT_PATH,
    build_reference_contract,
    diff_fingerprints,
    write_contract,
)
from reva.settings import load_settings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="compare with the committed contract; do not write")
    ap.add_argument("--keep-db", action="store_true", help="keep the throwaway database (debugging)")
    args = ap.parse_args(argv)

    fresh = build_reference_contract(load_settings(), keep=args.keep_db)
    old = json.loads(CONTRACT_PATH.read_text(encoding="utf-8")) if CONTRACT_PATH.exists() else None
    d = diff_fingerprints((old or {}).get("objects", {}), fresh["objects"])
    if args.check:
        if old is not None and d.ok:
            print(f"schema contract up to date ({fresh['fingerprint'][:16]}, head {fresh['alembic_head']})")
            return 0
        d.contract_head = (old or {}).get("alembic_head")
        d.live_head = fresh["alembic_head"]
        print("schema contract is STALE relative to the migrations "
              "(\"missing from database\" = only in the committed contract):", file=sys.stderr)
        print(d.summary(), file=sys.stderr)
        return 1
    write_contract(fresh)
    changes = "unchanged" if old is not None and d.ok else (
        f"{len(d.missing)} removed, {len(d.unexpected)} added, {len(d.changed)} changed")
    print(f"wrote {CONTRACT_PATH} (fingerprint {fresh['fingerprint'][:16]}, head {fresh['alembic_head']}, "
          f"{sum(fresh['counts'].values())} objects; {changes})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
