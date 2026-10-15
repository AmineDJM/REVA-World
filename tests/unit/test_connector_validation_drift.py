"""Connector validation blocks silent provider drift (offline, no database).

Drifted payloads are the synthetic derivations in tests/fixtures/repair (byte-checked
against the captured bank by tests/integration/test_connector_repair.py) plus small
in-memory derivations of the captured live fixtures. Blocking means the report is
``blocked`` (the ingest path then quarantines the acquisition); nothing is coerced.
Every captured live fixture and every recorded vintage must still pass.
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from reva.adapters.connectors import comtrade as CT
from reva.adapters.connectors.comtrade import ComtradeConnector
from reva.adapters.connectors.worldbank import INDICATORS, WorldBankConnector
from reva.adapters.http.safe_client import request_fingerprint
from reva.autonomy.repair import dumps_exact, load_exact, semantic_outputs, value_comparison
from reva.evidence.runner import FixtureHttp
from reva.ports.connectors import FetchBudget, FetchItem, QualityReport, RawAcquisition

FIXTURES = Path(__file__).parents[1] / "fixtures"
REPAIR = FIXTURES / "repair"
SOURCE = FIXTURES / "source"
REGISTRY = Path(__file__).parents[2] / "registry"
CASES = {c["case"]: c for c in json.loads((REPAIR / "manifest.json").read_text())}
WB = "worldbank.wdi.indicators"
CTN = "un.comtrade.public_exact"


def _connector(name: str) -> Any:
    return WorldBankConnector() if name == WB else ComtradeConnector()


def _item(connector: Any, url: str) -> FetchItem:
    return next(i for i in connector.plan(None, FetchBudget()).items if i.url == url)


async def _fetch(connector: Any, item: FetchItem, body: bytes | None = None) -> RawAcquisition:
    name = connector.describe().name
    overrides = {request_fingerprint("GET", item.url): body} if body is not None else None
    return await connector.fetch(item, FixtureHttp(name, overrides=overrides))


def _validate(connector: Any, raw: RawAcquisition) -> list[QualityReport]:
    return [connector.validate(b) for b in connector.parse(raw)]


def _blockers(reports: list[QualityReport]) -> set[str]:
    return {f.code for r in reports for f in r.findings if f.severity == "blocker"}


def _captured(name: str, url: str) -> bytes:
    e = next(x for x in json.loads((SOURCE / name / "manifest.json").read_text()) if x["url"] == url)
    return (SOURCE / name / e["file"]).read_bytes()


async def _case(case: str) -> list[QualityReport]:
    c = CASES[case]
    conn = _connector(c["connector"])
    item = _item(conn, c["url"])
    return _validate(conn, await _fetch(conn, item, (REPAIR / c["file"]).read_bytes()))


async def _derived(name: str, url: str, edit: Callable[[Any], None]) -> list[QualityReport]:
    conn = _connector(name)
    obj = load_exact(_captured(name, url))
    edit(obj)
    return _validate(conn, await _fetch(conn, _item(conn, url), dumps_exact(obj)))


WB_POP = CASES["wb_unit_text_and_scale"]["url"]
CT_FRA_M = CASES["ct_renamed_field"]["url"]


# --------------------------------------------------------------------------- no false positives
@pytest.mark.parametrize("name", [WB, CTN])
async def test_every_captured_live_fixture_still_passes(name: str) -> None:
    conn = _connector(name)
    entries = [e for e in json.loads((SOURCE / name / "manifest.json").read_text()) if e["status"] == 200]
    by_fp = {e["request_fingerprint"]: e for e in entries}
    queue = list(conn.plan(None, FetchBudget()).items)
    checked = 0
    while queue:
        item = queue.pop(0)
        if request_fingerprint("GET", item.url) not in by_fp:
            continue  # optional request never captured (e.g. a bilateral query)
        raw = await _fetch(conn, item)
        batches = list(conn.parse(raw))
        for b in batches:
            queue.extend(b.follow_up)
            report = conn.validate(b)
            assert report.outcome == "pass", (item.key, [f.model_dump() for f in report.findings])
        checked += 1
    assert checked == len(entries)


def _vintage_loader() -> Any:
    spec = importlib.util.spec_from_file_location("vintage_loader", FIXTURES / "vintages" / "vintage_loader.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def test_every_recorded_vintage_still_passes() -> None:
    vl = _vintage_loader()
    n = 0
    for name, s in vl.MANIFEST["series"].items():
        for v in s["vintages"]:
            c = vl.connector_for(name)
            raw = await vl.fetch_vintage(name, v["label"], c)
            assert _blockers(_validate(c, raw)) == set(), (name, v["label"])
            n += 1
    assert n >= 10


# --------------------------------------------------------------------------- World Bank
async def test_wb_unit_change_with_rescaled_values_blocks() -> None:
    reports = await _case("wb_unit_text_and_scale")
    assert "unit_drift" in _blockers(reports) and all(r.outcome == "blocked" for r in reports)
    msg = next(f.message for r in reports for f in r.findings if f.code == "unit_drift")
    assert "'thousands'" in msg and "100 rows" in msg


@pytest.mark.parametrize(
    ("edit", "code"),
    [
        (lambda r: r.update(decimal=1), "decimal_drift"),
        (lambda r: r.pop("decimal"), "decimal_drift"),
        (lambda r: r.pop("unit"), "unit_drift"),
        (lambda r: r.update(unit=None), "unit_drift"),
        (lambda r: r.update(value=str(r["value"])), "value_type_drift"),
        (lambda r: r.pop("value"), "value_type_drift"),
        (lambda r: r.update(indicator={"id": "SP.POP.TOTL.FE.IN", "value": "Population, female"}),
         "indicator_mismatch"),
    ],
    ids=["decimal_changed", "decimal_absent", "unit_absent", "unit_null", "value_as_string", "value_absent",
         "indicator_changed"],
)
async def test_wb_marker_drift_blocks(edit: Callable[[dict[str, Any]], Any], code: str) -> None:
    def apply(obj: Any) -> None:
        for r in obj[1]:
            edit(r)

    reports = await _derived(WB, WB_POP, apply)
    assert code in _blockers(reports)


async def test_wb_added_unused_field_still_passes_and_rename_still_blocks() -> None:
    assert _blockers(await _case("wb_added_field")) == set()
    assert "unexpected_subject" in _blockers(await _case("wb_renamed_field"))


async def test_wb_null_value_is_missing_not_drift() -> None:
    def null_one(obj: Any) -> None:
        obj[1][0]["value"] = None

    reports = await _derived(WB, WB_POP, null_one)
    assert _blockers(reports) == set()
    assert any(f.code == "missing_values" and f.severity == "info" for r in reports for f in r.findings)


def test_wb_reviewed_markers_cover_every_mapped_indicator() -> None:
    mapped = {m["source_ref"]["indicator"] for m in yaml.safe_load(
        (REGISTRY / "mappings" / "worldbank.yaml").read_text())["mappings"]}
    assert mapped == set(INDICATORS)
    assert all(INDICATORS[i]["provider_unit"] == "" and INDICATORS[i]["provider_decimal"] == "0" for i in mapped)


# --------------------------------------------------------------------------- Comtrade
async def test_ct_renamed_value_field_blocks_instead_of_becoming_missing() -> None:
    reports = await _case("ct_renamed_field")
    blockers = _blockers(reports)
    assert "field_absent" in blockers and all(r.outcome == "blocked" for r in reports)
    msg = next(f.message for r in reports for f in r.findings if f.code == "field_absent")
    assert "'primaryValue'" in msg


async def test_ct_present_null_value_stays_missing_with_a_warning() -> None:
    def null_value(obj: Any) -> None:
        obj["data"][0]["primaryValue"] = None

    reports = await _derived(CTN, CT_FRA_M, null_value)
    assert _blockers(reports) == set() and all(r.outcome == "pass" for r in reports)
    assert any(f.code == "missing_flow" and f.severity == "warning" for r in reports for f in r.findings)


async def test_ct_string_typed_number_blocks() -> None:
    reports = await _case("ct_value_type_change")
    assert "value_type_drift" in _blockers(reports)
    msg = next(f.message for r in reports for f in r.findings if f.code == "value_type_drift")
    assert "'primaryValue' is string" in msg

    def string_weight(obj: Any) -> None:
        obj["data"][0]["netWgt"] = "1234567"  # numeric text where a JSON number is expected

    assert "value_type_drift" in _blockers(await _derived(CTN, CT_FRA_M, string_weight))


async def test_ct_unreviewed_classification_blocks() -> None:
    reports = await _case("ct_classification_change")
    assert "classification_unreviewed" in _blockers(reports)
    msg = next(f.message for r in reports for f in r.findings if f.code == "classification_unreviewed")
    assert "H7" in msg


def test_ct_reviewed_classifications_mirror_the_registry_mapping() -> None:
    maps = yaml.safe_load((REGISTRY / "mappings" / "comtrade.yaml").read_text())["mappings"]
    declared = {tuple(m["applicability"]["classifications"]) for m in maps
                if "classifications" in m["applicability"]}
    assert declared == {CT.REVIEWED_CLASSIFICATIONS}


async def test_ct_added_unused_field_still_passes() -> None:
    assert _blockers(await _case("ct_added_field")) == set()


async def test_ct_silent_rescale_is_left_to_the_vintage_comparison() -> None:
    """A uniform /1000 keeps types, units and dimensions: one payload cannot tell it from a
    genuine revision, so validation does not block it (no magnitude heuristic). The repair
    advisor's comparison against the accepted vintage of the same request detects it."""
    c = CASES["ct_silent_scale"]
    reports = await _case("ct_silent_scale")
    assert _blockers(reports) == set()
    conn = ComtradeConnector()
    item = _item(conn, c["url"])
    accepted = semantic_outputs(list(conn.parse(await _fetch(conn, item))))[0]
    drifted = semantic_outputs(list(conn.parse(await _fetch(conn, item, (REPAIR / c["file"]).read_bytes()))))[0]
    cmp = value_comparison(accepted, drifted)
    assert cmp["uniform_power_of_ten"] and cmp["factor_new_over_old"] == "1E-3"
