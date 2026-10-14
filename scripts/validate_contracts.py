#!/usr/bin/env python3
"""Offline registry + contract validation (CI step; no database needed).

* every contracts/*.schema.json and registry/components/*.schema.json is a valid
  JSON Schema (Draft 2020-12 unless it declares another dialect);
* contracts/scenario.example.json validates against contracts/scenario.schema.json
  and through the typed validator ``reva.simulation.spec.validate_scenario_spec``;
* contracts/openapi.json (when present) is an OpenAPI 3.x document;
* the YAML registry loads (concept versions unique, manifests computable).

Prints a JSON summary; exit 1 on any problem.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runtime_env import ensure_importable
from jsonschema import validators

ensure_importable()

ROOT = Path(__file__).resolve().parents[1]


def _check_schema(path: Path) -> str | None:
    try:
        schema = json.loads(path.read_text())
        cls = validators.validator_for(schema, default=validators.Draft202012Validator)
        cls.check_schema(schema)
    except Exception as e:
        return f"{path.relative_to(ROOT)}: {type(e).__name__}: {str(e)[:300]}"
    return None


def main() -> int:
    problems: list[str] = []
    checked: dict[str, Any] = {}
    schemas = sorted([*ROOT.glob("contracts/*.schema.json"), *ROOT.glob("registry/components/*.schema.json")])
    for p in schemas:
        err = _check_schema(p)
        if err:
            problems.append(err)
    checked["schemas"] = len(schemas)

    example = ROOT / "contracts" / "scenario.example.json"
    if example.exists():
        from jsonschema import Draft202012Validator

        schema = json.loads((ROOT / "contracts" / "scenario.schema.json").read_text())
        doc = json.loads(example.read_text())
        errs = [e.message for e in Draft202012Validator(schema).iter_errors(doc)]
        problems += [f"scenario.example.json: {m[:200]}" for m in errs]
        try:
            # The appendix example carries an illustrative baseline UUID that the typed
            # validator must refuse; with a concrete UUID substituted it must pass.
            from reva.kernel.errors import ValidationProblem
            from reva.simulation.spec import validate_scenario_spec

            try:
                validate_scenario_spec(doc)
                problems.append("scenario.example.json: illustrative baseline UUID was not rejected")
            except ValidationProblem as e:
                checked["placeholder_rejected"] = "baseline_release_id" in str(e)
            validate_scenario_spec({**doc, "baseline_release_id": str(uuid.uuid4())})
        except Exception as e:
            problems.append(f"scenario.example.json typed validation: {type(e).__name__}: {str(e)[:300]}")
        checked["scenario_example"] = True

    openapi = ROOT / "contracts" / "openapi.json"
    if openapi.exists():
        try:
            doc = json.loads(openapi.read_text())
            if not str(doc.get("openapi", "")).startswith("3.") or not doc.get("paths"):
                problems.append("contracts/openapi.json: not an OpenAPI 3.x document with paths")
            checked["openapi_paths"] = len(doc.get("paths", {}))
        except json.JSONDecodeError as e:
            problems.append(f"contracts/openapi.json: {e}")

    try:
        from reva.ontology.registry import load_registry

        reg = load_registry()
        reg.manifest()
        checked["registry"] = "ok"
    except Exception as e:
        problems.append(f"registry: {type(e).__name__}: {str(e)[:300]}")

    print(json.dumps({"valid": not problems, "checked": checked, "problems": problems}, indent=2))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
