"""Synthetic drifted payloads derived from the captured fixture bank (tests/fixtures/source).

Every case names its captured source fixture (file + SHA-256 from the capture
manifest) and a declarative derivation. ``python tests/fixtures/repair/derive.py``
rewrites the drifted files and ``manifest.json``; the repair tests re-derive each
payload and require byte equality, so a drifted fixture can never silently diverge
from the captured history it claims to come from. These payloads are synthetic
(no provider ever returned them) and are used offline only.

Import from tests with ``load_derive()`` in the test module (tests are not a package).
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from reva.autonomy.repair import (
    ADDITIVE,
    AUTH,
    CLASSIFICATION,
    ENDPOINT,
    RENAME,
    SCALE,
    TYPE_CHANGE,
    UNIT,
    dumps_exact,
    load_exact,
)

ROOT = Path(__file__).parent
SOURCE = ROOT.parent / "source"
WB = "worldbank.wdi.indicators"
CT = "un.comtrade.public_exact"
WB_POP = (
    "https://api.worldbank.org/v2/country/FRA;DZA;CHN;USA/indicator/SP.POP.TOTL?format=json"
    "&date=2000:2024&per_page=1000&page=1"
)
CT_FRA_M_2023 = (
    "https://comtradeapi.un.org/public/v1/preview/C/A/HS?period=2023&reporterCode=251&cmdCode=2709"
    "&flowCode=M&partnerCode=0&partner2Code=0&customsCode=C00&motCode=0&maxRecords=500"
)

CASES: list[dict[str, Any]] = [
    {
        "case": "wb_added_field",
        "connector": WB,
        "url": WB_POP,
        "status": 200,
        "derivation": {"op": "add_field", "records": "$[1][]", "field": "footnote", "value": ""},
        "expected": ADDITIVE,
    },
    {
        "case": "wb_renamed_field",
        "connector": WB,
        "url": WB_POP,
        "status": 200,
        "derivation": {"op": "rename", "records": "$[1][]", "from": "countryiso3code", "to": "countryIso3Code"},
        "expected": RENAME,
    },
    {
        "case": "wb_unit_text_and_scale",
        "connector": WB,
        "url": WB_POP,
        "status": 200,
        "derivation": {
            "op": "unit_scale",
            "records": "$[1][]",
            "unit_field": "unit",
            "unit": "thousands",
            "value_fields": ["value"],
            "divide_by": "1000",
        },
        "expected": UNIT,
    },
    {
        "case": "ct_added_field",
        "connector": CT,
        "url": CT_FRA_M_2023,
        "status": 200,
        "derivation": {"op": "add_field", "records": "$.data[]", "field": "isLegacyFlagged", "value": False},
        "expected": ADDITIVE,
    },
    {
        "case": "ct_renamed_field",
        "connector": CT,
        "url": CT_FRA_M_2023,
        "status": 200,
        "derivation": {"op": "rename", "records": "$.data[]", "from": "primaryValue", "to": "primaryValueUsd"},
        "expected": RENAME,
    },
    {
        "case": "ct_silent_scale",
        "connector": CT,
        "url": CT_FRA_M_2023,
        "status": 200,
        "derivation": {
            "op": "unit_scale",
            "records": "$.data[]",
            "unit_field": None,
            "unit": None,
            "value_fields": ["primaryValue", "cifvalue", "fobvalue"],
            "divide_by": "1000",
        },
        "expected": SCALE,
    },
    {
        "case": "ct_value_type_change",
        "connector": CT,
        "url": CT_FRA_M_2023,
        "status": 200,
        "derivation": {"op": "stringify", "records": "$.data[]", "field": "primaryValue"},
        "expected": TYPE_CHANGE,
    },
    {
        "case": "ct_classification_change",
        "connector": CT,
        "url": CT_FRA_M_2023,
        "status": 200,
        "derivation": {"op": "set_value", "records": "$.data[]", "field": "classificationCode", "value": "H7"},
        "expected": CLASSIFICATION,
    },
    {
        "case": "wb_endpoint_404",
        "connector": WB,
        "url": WB_POP,
        "status": 404,
        "derivation": {"op": "status_only", "note": "series endpoint answers 404 on every scheduled run"},
        "expected": ENDPOINT,
    },
    {
        "case": "ct_auth_401",
        "connector": CT,
        "url": "*",
        "status": 401,
        "derivation": {
            "op": "status_only",
            "note": "public preview starts requiring a subscription key (all requests)",
        },
        "expected": AUTH,
    },
]


def _capture(connector: str, url: str) -> dict[str, Any]:
    entries = json.loads((SOURCE / connector / "manifest.json").read_text())
    return next(e for e in entries if e["url"] == url)


def _records(obj: Any, path: str) -> list[dict[str, Any]]:
    if path == "$[1][]":
        return list(obj[1])
    if path == "$.data[]":
        return list(obj["data"])
    raise ValueError(path)


def derive(case: dict[str, Any]) -> bytes | None:
    d = case["derivation"]
    if d["op"] == "status_only":
        return None
    src = _capture(case["connector"], case["url"])
    body = (SOURCE / case["connector"] / src["file"]).read_bytes()
    if hashlib.sha256(body).hexdigest() != src["sha256"]:
        raise ValueError(f"captured fixture {src['file']} does not match its manifest hash")
    obj = load_exact(body)
    for r in _records(obj, d["records"]):
        if d["op"] == "add_field":
            r[d["field"]] = d["value"]
        elif d["op"] == "rename":
            r[d["to"]] = r.pop(d["from"])
        elif d["op"] == "set_value":
            r[d["field"]] = d["value"]
        elif d["op"] == "stringify":
            if r.get(d["field"]) is not None:
                r[d["field"]] = str(r[d["field"]])
        elif d["op"] == "unit_scale":
            if d["unit_field"]:
                r[d["unit_field"]] = d["unit"]
            for f in d["value_fields"]:
                if isinstance(r.get(f), Decimal) and r[f] != 0:
                    r[f] = r[f] / Decimal(d["divide_by"])
        else:
            raise ValueError(d["op"])
    return dumps_exact(obj)


def manifest() -> list[dict[str, Any]]:
    out = []
    for c in CASES:
        body = derive(c)
        entry: dict[str, Any] = {k: c[k] for k in ("case", "connector", "url", "status", "derivation", "expected")}
        if c["url"] != "*":
            src = _capture(c["connector"], c["url"])
            entry["source_fixture"] = {"file": f"{c['connector']}/{src['file']}", "sha256": src["sha256"]}
        entry["file"] = f"{c['case']}.json" if body is not None else None
        entry["sha256"] = hashlib.sha256(body).hexdigest() if body is not None else None
        entry["synthetic"] = True
        out.append(entry)
    return out


def main() -> None:
    for c in CASES:
        body = derive(c)
        if body is not None:
            (ROOT / f"{c['case']}.json").write_bytes(body)
    (ROOT / "manifest.json").write_text(json.dumps(manifest(), indent=2) + "\n")


if __name__ == "__main__":
    main()
