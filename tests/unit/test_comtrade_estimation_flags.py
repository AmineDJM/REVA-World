"""Comtrade 1.2.0 estimation flags follow the UNSD legacyEstimationFlag coding (offline).

UNSD codes 0/2/4/6 mean no estimation / quantity / net weight / both estimated: they
concern quantity and weight, never the USD value. Value observations therefore carry
``provider_value_estimated = null`` (undeterminable from the response) and only
net-weight observations carry ``provider_quantity_estimated``. Readers (crude exposure
explanation, flow aggregation) label a value as estimated only on an explicit
``provider_value_estimated = true``; the 1.1.0 key ``provider_estimation_flagged`` on old
rows is read as quantity estimation, never as an estimated value.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from reva.adapters.connectors.comtrade import (
    CONNECTOR_VERSION,
    PARSER_VERSION,
    ComtradeConnector,
)
from reva.autonomy.repair import dumps_exact, load_exact
from reva.evidence.runner import FixtureHttp
from reva.models.economic.crude_exposure import CountryInputs, MonetaryInput, explain
from reva.ports.connectors import FetchBudget, ObservationBatch, ParsedObservation, QualityReport
from reva.world.queries import _item

REGISTRY = Path(__file__).parents[2] / "registry"
CT = "un.comtrade.public_exact"


async def _parse(iso: str, flow: str, year: int, edit: Any = None
                 ) -> tuple[ParsedObservation, ParsedObservation, ObservationBatch, QualityReport]:
    ct = ComtradeConnector(reporters=(iso,), total_years=(year,), bilateral_years=())
    item = next(i for i in ct.plan(None, FetchBudget()).items if i.key == f"total:{iso}:{flow}:{year}")
    raw = await ct.fetch(item, FixtureHttp(CT))
    if edit is not None:
        obj = load_exact(raw.body)
        edit(obj["data"][0])
        raw = raw.model_copy(update={"body": dumps_exact(obj)})
    (batch,) = ct.parse(raw)
    value = next(o for o in batch.observations if o.logical_key.endswith("primaryValue"))
    weight = next(o for o in batch.observations if o.logical_key.endswith("netWgt"))
    return value, weight, batch, ct.validate(batch)


def test_version_bump_is_registered() -> None:
    spec = ComtradeConnector().describe()
    assert (spec.version, spec.parser_version) == (CONNECTOR_VERSION, PARSER_VERSION) == ("1.2.0", "comtrade-parse-3")
    reg = yaml.safe_load((REGISTRY / "mappings" / "comtrade.yaml").read_text())
    assert reg["connector"] == {"name": CT, "version": CONNECTOR_VERSION}


async def test_code_6_marks_quantity_and_weight_not_the_value() -> None:
    # CHN imports 2023 (captured live): legacyEstimationFlag 6, isQtyEstimated/isNetWgtEstimated true.
    value, weight, _batch, report = await _parse("CHN", "M", 2023)
    raw = value.source_flags
    assert str(raw["legacyEstimationFlag"]) == "6" and raw["isQtyEstimated"] is True
    assert raw["isNetWgtEstimated"] is True  # raw provider flags kept verbatim
    assert raw["provider_value_estimated"] is None  # undeterminable: never claimed either way
    assert "provider_quantity_estimated" not in raw and "provider_estimation_flagged" not in raw
    assert weight.source_flags["provider_quantity_estimated"] is True
    assert "provider_value_estimated" not in weight.source_flags
    assert report.outcome == "pass"
    info = next(f for f in report.findings if f.code == "provider_quantity_estimated")
    assert info.severity == "info" and "trade values are as reported" in info.message
    assert all(f.code != "provider_estimation_flagged" for f in report.findings)


async def test_code_0_marks_nothing() -> None:
    value, weight, _, report = await _parse("USA", "M", 2023)
    assert str(value.source_flags["legacyEstimationFlag"]) == "0"
    assert value.source_flags["provider_value_estimated"] is None
    assert weight.source_flags["provider_quantity_estimated"] is False
    assert all(f.code != "provider_quantity_estimated" for f in report.findings)


@pytest.mark.parametrize(("code", "qty"), [(0, False), (2, True), (4, True), (6, True)])
async def test_unsd_codes_decode_to_quantity_estimation_only(code: int, qty: bool) -> None:
    def edit(r: dict[str, Any]) -> None:
        r.update(legacyEstimationFlag=Decimal(code), isQtyEstimated=False, isNetWgtEstimated=False)

    value, weight, _, report = await _parse("USA", "M", 2023, edit)
    assert weight.source_flags["provider_quantity_estimated"] is qty
    assert value.source_flags["provider_value_estimated"] is None
    assert all(f.code != "estimation_code_unrecognised" for f in report.findings)


async def test_unrecognised_code_is_a_review_warning_not_a_claim() -> None:
    def edit(r: dict[str, Any]) -> None:
        r.update(legacyEstimationFlag=Decimal(1), isQtyEstimated=False, isNetWgtEstimated=False)

    value, weight, _, report = await _parse("USA", "M", 2023, edit)
    assert value.source_flags["provider_value_estimated"] is None
    assert value.source_flags["legacy_estimation_code_unrecognised"] is True
    assert weight.source_flags["provider_quantity_estimated"] is False
    warn = next(f for f in report.findings if f.code == "estimation_code_unrecognised")
    assert warn.severity == "warning" and report.outcome == "pass"


# --------------------------------------------------------------------------- readers
_V = {"kind": "decimal", "value": "337600000000", "unit_ref": "unit.usd",
      "basis": {"currency": "USD", "price_basis": "nominal", "scale_factor": "1", "valuation": "CIF"}}


def _explain_flags(flags: dict[str, Any]) -> dict[str, Any]:
    m = MonetaryInput.from_value_dict("M", _V, reference_year=2023, aggregation="period_flow_total",
                                      source_flags=flags)
    return explain(CountryInputs("CHN", m, m, m))["source_flags"]["M"]


def test_crude_exposure_does_not_label_quantity_estimation_as_an_estimated_value() -> None:
    new_row = {"provider_value_estimated": None, "legacyEstimationFlag": "6", "isQtyEstimated": True,
               "isNetWgtEstimated": True, "isReported": False, "isAggregate": True}
    f = _explain_flags(new_row)
    assert f["note"] == "provider marks this value as not directly reported (isReported=false)"
    assert "estimat" not in f["note"] and f["legacyEstimationFlag"] == "6"
    # 1.1.0 row: the legacy key coded quantity/weight; it neither labels the value nor is re-exported.
    old_row = {"provider_estimation_flagged": True, "legacyEstimationFlag": "6", "isReported": False}
    f = _explain_flags(old_row)
    assert "estimat" not in f["note"] and "provider_estimation_flagged" not in f
    f = _explain_flags({"provider_estimation_flagged": True, "legacyEstimationFlag": "6", "isReported": True})
    assert "note" not in f


def test_crude_exposure_labels_an_explicit_value_estimate() -> None:
    assert _explain_flags({"provider_value_estimated": True})["note"] == "provider marks this value as estimated"


def _flow_row(flags: dict[str, Any]) -> dict[str, Any]:
    return {"id": uuid.uuid4(), "subject_id": uuid.uuid4(), "concept_id": "trade.hs2709.import_value.world_total",
            "concept_version": "1.0.0", "semantic_profile": {}, "spatial_spec": {}, "resolution": {"partner": "0"},
            "support_id": uuid.uuid4(), "temporal_extent": {}, "value": _V, "source_flags": flags}


@pytest.mark.parametrize(
    ("flags", "derived"),
    [
        ({"provider_value_estimated": None, "legacyEstimationFlag": "6", "isReported": False}, False),
        ({"provider_estimation_flagged": True, "legacyEstimationFlag": "6"}, False),  # 1.1.0 row
        ({"provider_derived_aggregate": True}, True),  # 1.0.0 row
        ({"provider_value_estimated": True}, True),
    ],
)
def test_flow_items_mark_only_value_estimates(flags: dict[str, Any], derived: bool) -> None:
    assert _item(_flow_row(flags), "observation").provider_derived is derived
