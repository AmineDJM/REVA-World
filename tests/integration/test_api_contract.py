"""Exported HTTP contract: response schemas are in serialization mode (no database needed)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def _export() -> Any:
    spec = importlib.util.spec_from_file_location("export_contracts", ROOT / "scripts" / "export_contracts.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_committed_contract_is_current() -> None:
    mod = _export()
    assert (ROOT / "contracts" / "openapi.json").read_text(encoding="utf-8") == mod.render(), \
        "contracts/openapi.json is stale: run scripts/export_contracts.py"


def test_response_fields_with_defaults_are_required() -> None:
    import json

    schemas = json.loads(_export().render())["components"]["schemas"]
    env = set(schemas["BeliefListResponse"]["required"])
    assert {"warnings", "units", "provenance_links", "uncertainty", "next_cursor", "historical_validation"} <= env
    assert {"basis"} <= set(schemas["DecimalValue"]["required"])
    assert {"links", "result_ref"} <= set(schemas["Job"]["required"])
    assert {"origin", "acquisitions_by_origin", "latest_acquisition_origin"} <= set(schemas["ConnectorOut"]["required"])
    assert {"replay_mode_label", "is_naive_baseline", "included_as"} <= set(schemas["ModelEvaluation"]["required"])
    # Request bodies keep their defaults optional; problem members built outside pydantic stay optional.
    assert set(schemas["ReplayRequest"]["required"]) == {"mode", "cutoff"}
    assert "required" not in schemas["EvaluationRequest"]
    assert not {"details", "instance", "request_id"} & set(schemas["Problem"]["required"])
    assert not [k for k in schemas if k.endswith(("-Input", "-Output"))]


def test_provenance_kinds_cover_emitted_links() -> None:
    import json

    doc = json.loads(_export().render())
    link_kinds = set(doc["components"]["schemas"]["ProvenanceLink"]["properties"]["kind"]["enum"])
    params = doc["paths"]["/v1/provenance/{kind}/{ident}"]["get"]["parameters"]
    kind = next(p for p in params if p["name"] == "kind")["schema"]["enum"]
    assert link_kinds <= set(kind)
