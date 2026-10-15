"""ScenarioSpec v2.0.0: a discriminated union on ``model_key`` built from the plugins.

Every branch shares the common fields (baseline release, knowledge cutoff,
countries, reference year, horizon, fidelity, missing-input policy, seed, title)
and takes ``intervention``, ``assumptions`` and ``outputs`` — and optionally
narrower ``countries`` / ``reference_year`` / ``horizon`` / ``fidelity`` /
``missing_input_policy`` and extra ``allOf`` conditions — from the plugin's
``spec_schema()``. A plugin without ``spec_schema()`` gets permissive objects
there and its ``validate()`` is the only model check.

The crude branch reproduces the binding V1 appendix (``contracts/
scenario.v1.schema.json``) exactly, except that ``schema_version`` accepts
``"1.0.0"`` (V1 specs keep validating unchanged) and ``"2.0.0"``.

``python -m reva.simulation.scenario_schema`` regenerates
``contracts/scenario.schema.json`` from the registered plugins (``--check`` only
compares).
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "2.0.0"
CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
SCHEMA_FILE = CONTRACTS / "scenario.schema.json"
MAX_SAFE_INT = 9007199254740991

#: Fragment keys a plugin's ``spec_schema()`` may return.
FRAGMENT_KEYS = frozenset(
    {
        "intervention",
        "assumptions",
        "outputs",
        "countries",
        "reference_year",
        "horizon",
        "fidelity",
        "missing_input_policy",
        "schema_version",
        "allOf",
    }
)

COMMON_PROPERTIES: dict[str, Any] = {
    "schema_version": {"const": SCHEMA_VERSION},
    "title": {"type": "string", "minLength": 1, "maxLength": 200},
    "baseline_release_id": {"type": "string", "format": "uuid"},
    "knowledge_cutoff": {"type": "string", "format": "date-time"},
    "countries": {
        "type": "array",
        "items": {"type": "string", "pattern": "^[A-Z]{3}$"},
        "minItems": 1,
        "maxItems": 300,
        "uniqueItems": True,
    },
    "reference_year": {"type": "integer", "minimum": 1950, "maximum": 2100},
    "horizon": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "periods": {"type": "integer", "minimum": 1, "maximum": 100},
            "unit": {"enum": ["annual_accounting_period", "calendar_year"]},
        },
        "required": ["periods", "unit"],
    },
    "intervention": {"type": "object"},
    "assumptions": {"type": "object"},
    "fidelity": {"enum": ["L0", "L1", "L2"]},
    "missing_input_policy": {"enum": ["fail", "skip_missing_countries"]},
    "outputs": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"type": "string", "minLength": 1}},
    "seed": {"type": "integer", "minimum": 0, "maximum": MAX_SAFE_INT},
}
REQUIRED = [
    "schema_version",
    "title",
    "baseline_release_id",
    "knowledge_cutoff",
    "model_key",
    "model_version",
    "countries",
    "reference_year",
    "horizon",
    "intervention",
    "assumptions",
    "fidelity",
    "missing_input_policy",
    "outputs",
    "seed",
]
PROPERTY_ORDER = [
    "schema_version",
    "title",
    "baseline_release_id",
    "knowledge_cutoff",
    "model_key",
    "model_version",
    "countries",
    "reference_year",
    "horizon",
    "intervention",
    "assumptions",
    "fidelity",
    "missing_input_policy",
    "outputs",
    "seed",
]


def crude_branch_schema() -> dict[str, Any]:
    """The crude model's fragments: V1 appendix semantics (plus schema_version 2.0.0)."""
    return {
        "schema_version": {"enum": ["1.0.0", "2.0.0"]},
        # V2 step 1 (ALLC): registry country keys or exactly ["ALL"] (resolved at compile);
        # schema_version 1.0.0 keeps the V1 four countries and the "fail" policy (allOf below).
        "countries": {
            "type": "array",
            "items": {"type": "string", "pattern": "^[A-Z]{3}$"},
            "minItems": 1,
            "maxItems": 300,
            "uniqueItems": True,
        },
        "reference_year": {"type": "integer", "minimum": 2020, "maximum": 2024},
        "horizon": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"periods": {"const": 1}, "unit": {"const": "annual_accounting_period"}},
            "required": ["periods", "unit"],
        },
        "intervention": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "kind": {"const": "set_exogenous_border_price_multiplier"},
                "import_multiplier": {"type": "number", "exclusiveMinimum": 0, "maximum": 3},
                "export_multiplier": {"type": "number", "exclusiveMinimum": 0, "maximum": 3},
                "unit": {"const": "ratio"},
            },
            "required": ["kind", "import_multiplier", "export_multiplier", "unit"],
        },
        "assumptions": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "quantity_response": {"enum": ["fixed", "elasticity_sensitivity"]},
                "elasticity_grid": {
                    "type": "array",
                    "maxItems": 100,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "import_elasticity": {"type": "number", "minimum": -2, "maximum": 1},
                            "export_elasticity": {"type": "number", "minimum": -1, "maximum": 2},
                            "basis": {"const": "scenario_assumption"},
                        },
                        "required": ["import_elasticity", "export_elasticity", "basis"],
                    },
                },
                "no_general_equilibrium": {"const": True},
            },
            "required": ["quantity_response", "elasticity_grid", "no_general_equilibrium"],
        },
        "fidelity": {"enum": ["L0", "L1", "L2"]},
        "missing_input_policy": {"enum": ["fail", "skip_missing_countries"]},
        "outputs": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {
                "enum": [
                    "import_value_change_usd",
                    "export_value_change_usd",
                    "net_external_cost_change_usd",
                    "exposure_percent_baseline_gdp",
                ]
            },
        },
        "allOf": [
            {
                "if": {"properties": {"schema_version": {"const": "1.0.0"}}},
                "then": {
                    "properties": {
                        "countries": {"items": {"enum": ["FRA", "DZA", "CHN", "USA"]}, "maxItems": 4},
                        "missing_input_policy": {"const": "fail"},
                    }
                },
            },
            {
                "if": {"properties": {"countries": {"contains": {"const": "ALL"}}}},
                "then": {"properties": {"countries": {"maxItems": 1}}},
            },
            {
                "if": {"properties": {"fidelity": {"const": "L1"}}},
                "then": {
                    "properties": {
                        "assumptions": {
                            "properties": {"quantity_response": {"const": "fixed"}, "elasticity_grid": {"maxItems": 0}}
                        }
                    }
                },
            },
            {
                "if": {"properties": {"fidelity": {"const": "L2"}}},
                "then": {
                    "properties": {
                        "assumptions": {
                            "properties": {
                                "quantity_response": {"const": "elasticity_sensitivity"},
                                "elasticity_grid": {"minItems": 1},
                            }
                        }
                    }
                },
            },
        ],
    }


def branch_schema(model: Any) -> dict[str, Any]:
    """The closed object schema of one model's specs."""
    hook = getattr(model, "spec_schema", None)
    fragments: dict[str, Any] = dict(hook()) if callable(hook) else {}
    unknown = set(fragments) - FRAGMENT_KEYS
    if unknown:
        raise ValueError(f"{model.model_key}: spec_schema() returned unknown keys {sorted(unknown)}")
    props = copy.deepcopy(COMMON_PROPERTIES)
    props["model_key"] = {"const": model.model_key}
    props["model_version"] = {"const": model.model_version}
    for k, v in fragments.items():
        if k != "allOf":
            props[k] = copy.deepcopy(v)
    branch: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {k: props[k] for k in PROPERTY_ORDER},
        "required": list(REQUIRED),
    }
    if fragments.get("allOf"):
        branch["allOf"] = copy.deepcopy(fragments["allOf"])
    return branch


def build_schema(models: dict[str, Any] | None = None) -> dict[str, Any]:
    """The v2.0.0 union over the registered plugins (sorted by model_key)."""
    if models is None:
        from reva.simulation.plugins import load_all

        models = load_all()
    keys = sorted(models)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "REVA ScenarioSpec v2.0.0 — discriminated union on model_key",
        "description": (
            "Common fields are shared by every model; intervention, assumptions and outputs (and any "
            "narrower common field) come from the model plugin's spec_schema(). Crude-exposure specs "
            "with schema_version 1.0.0 (the V1 appendix, contracts/scenario.v1.schema.json) remain "
            "valid. Generated by `python -m reva.simulation.scenario_schema`; do not edit by hand."
        ),
        "type": "object",
        "additionalProperties": False,
        "required": ["model_key"],
        # Closed at the top level too: only the common field names exist; each branch
        # then constrains them (and is itself closed).
        "properties": {k: ({"enum": keys} if k == "model_key" else {}) for k in PROPERTY_ORDER},
        "allOf": [
            {
                "if": {"properties": {"model_key": {"const": k}}, "required": ["model_key"]},
                "then": {"$ref": f"#/$defs/{k}"},
            }
            for k in keys
        ],
        "$defs": {k: branch_schema(models[k]) for k in keys},
    }


def render(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    text = render(build_schema())
    if "--check" in args:
        current = SCHEMA_FILE.read_text() if SCHEMA_FILE.exists() else ""
        if current != text:
            print(f"{SCHEMA_FILE} is stale: run `uv run python -m reva.simulation.scenario_schema`")
            return 1
        print(f"{SCHEMA_FILE} is up to date")
        return 0
    SCHEMA_FILE.write_text(text)
    print(f"wrote {SCHEMA_FILE}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
