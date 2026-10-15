"""eurostat.labour_pensions connector: offline parsing, semantics and drift checks (captured fixtures)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from reva.adapters.connectors import eurostat_labour as lab
from reva.evidence.runner import FixtureHttp
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import FetchBudget, RawAcquisition


def _raws() -> list[RawAcquisition]:
    c = lab.EurostatLabourPensionsConnector()

    async def go() -> list[RawAcquisition]:
        http = FixtureHttp(lab.CONNECTOR_NAME)
        return [await c.fetch(i, http) for i in c.plan(None, FetchBudget()).items]

    return asyncio.run(go())


RAWS = {r.item.params["table"]: r for r in _raws()}


def _with_body(raw: RawAcquisition, body: bytes) -> RawAcquisition:
    return raw.model_copy(update={"body": body})


def test_plan_one_request_per_table_and_budget_respected() -> None:
    c = lab.EurostatLabourPensionsConnector()
    plan = c.plan(None, FetchBudget())
    assert [i.params["table"] for i in plan.items] == [t.code for t in lab.TABLES]
    assert all(i.url.startswith(lab.DATA_BASE) for i in plan.items)
    short = c.plan(None, FetchBudget(max_requests=3))
    assert len(short.items) == 3 and short.budget_exhausted


@pytest.mark.parametrize("code", [t.code for t in lab.TABLES])
def test_every_fixture_table_parses_and_validates(code: str) -> None:
    c = lab.EurostatLabourPensionsConnector()
    (batch,) = list(c.parse(RAWS[code]))
    q = c.validate(batch)
    assert q.outcome == "pass", q.findings
    assert len(batch.observations) == batch.metadata["cube_positions"]
    t = lab.TABLES_BY_CODE[code]
    assert {o.concept_id for o in batch.observations} == {t.concept_id}


def test_missing_values_stay_typed_missing_never_zero() -> None:
    c = lab.EurostatLabourPensionsConnector()
    (batch,) = list(c.parse(RAWS["spr_exp_pens"]))
    missing = [o for o in batch.observations if o.value["kind"] == "missing"]
    assert missing, "fixture has suppressed/unavailable cells"
    assert all(o.source_flags.get("absent_in_value_map") for o in missing)
    assert all("value" not in o.value for o in missing)


def test_values_kept_verbatim_in_provider_scale_with_flags() -> None:
    c = lab.EurostatLabourPensionsConnector()
    (batch,) = list(c.parse(RAWS["lfsa_ergan"]))
    fr = {o.logical_key: o for o in batch.observations}
    o = fr["eurostat:lfsa_ergan:FR:sex=M:age=Y60-64:2014"]
    assert o.value == {"kind": "decimal", "value": "25.6", "unit_ref": "unit.percent", "basis": {"scale_factor": "1"}}
    assert o.source_flags["status"] == "b" and o.source_flags["status_labels"] == {"b": "break in time series"}
    assert o.support.resolution == {"sex": "M", "age": "Y60-64"}
    assert (o.subject_provider, o.subject_key, o.support.spatial.key) == ("iso3166_alpha3", "FRA", "ESTAT:FR")
    (eg,) = list(c.parse(RAWS["lfsa_egan"]))
    assert all(x.value["basis"]["scale_factor"] == "1000" for x in eg.observations if x.value["kind"] == "decimal")
    totals = [x for x in batch.observations if x.support.resolution["sex"] == "T"]
    assert totals and all(x.source_flags["cell_type"] == "subtotal" for x in totals)


def test_pension_expenditure_is_nominal_euro_millions() -> None:
    c = lab.EurostatLabourPensionsConnector()
    (batch,) = list(c.parse(RAWS["spr_exp_pens"]))
    v = next(o.value for o in batch.observations if o.value["kind"] == "decimal")
    assert v["unit_ref"] == "unit.eur" and v["basis"] == {
        "scale_factor": "1000000",
        "currency": "EUR",
        "price_basis": "nominal",
    }


def _doc(code: str) -> dict[str, Any]:
    return json.loads(RAWS[code].body)


def test_dimension_order_drift_blocks() -> None:
    doc = _doc("ilc_lvph01")
    doc["id"] = list(reversed(doc["id"]))
    doc["size"] = list(reversed(doc["size"]))
    c = lab.EurostatLabourPensionsConnector()
    (batch,) = list(c.parse(_with_body(RAWS["ilc_lvph01"], json.dumps(doc).encode())))
    q = c.validate(batch)
    assert q.outcome == "blocked" and any(f.code == "dimension_order_changed" for f in q.findings)


def test_unknown_status_flag_blocks() -> None:
    doc = _doc("ilc_lvph01")
    doc["status"] = {"0": "q"}
    c = lab.EurostatLabourPensionsConnector()
    (batch,) = list(c.parse(_with_body(RAWS["ilc_lvph01"], json.dumps(doc).encode())))
    assert any(f.code == "unknown_status_flag" for f in c.validate(batch).findings)


def test_unit_change_blocks() -> None:
    doc = _doc("ilc_lvph01")
    doc["dimension"]["unit"]["category"]["index"] = {"PC": 0}
    doc["dimension"]["unit"]["category"]["label"] = {"PC": "Percentage"}
    c = lab.EurostatLabourPensionsConnector()
    (batch,) = list(c.parse(_with_body(RAWS["ilc_lvph01"], json.dumps(doc).encode())))
    assert any(f.code == "pinned_categories_changed" for f in c.validate(batch).findings)


def test_empty_value_map_is_blocked_not_zero() -> None:
    doc = _doc("ilc_lvph01")
    doc["value"] = {}
    doc.pop("status", None)
    c = lab.EurostatLabourPensionsConnector()
    (batch,) = list(c.parse(_with_body(RAWS["ilc_lvph01"], json.dumps(doc).encode())))
    q = c.validate(batch)
    assert q.outcome == "blocked" and any(f.code == "empty_response" for f in q.findings)
    assert all(o.value["kind"] == "missing" for o in batch.observations)


def test_api_error_document_is_typed() -> None:
    with pytest.raises(SourceResponseError):
        list(
            lab.EurostatLabourPensionsConnector().parse(
                _with_body(RAWS["ilc_lvph01"], b'{"error": {"status": 400, "label": "bad"}}')
            )
        )


def test_dataset_derivation_is_deterministic_and_per_country() -> None:
    a = lab.derive_dataset_tables(RAWS["lfsa_ergan"].body, "lfsa_ergan")
    b = lab.derive_dataset_tables(RAWS["lfsa_ergan"].body, "lfsa_ergan")
    assert sorted(a) == sorted(lab.EUROSTAT_GEO_TO_ISO3.values())
    assert all(a[k].equals(b[k]) for k in a)
    fra = a["FRA"].to_pylist()
    assert {r["dim1_name"] for r in fra} == {"sex"} and {r["dim2_name"] for r in fra} == {"age"}
    missing = [r for r in fra if r["value"] is None]
    assert all(r["missing_reason"] for r in missing)
    m = lab.dataset_manifest("lfsa_ergan", "FRA", a["FRA"], raw_sha256="0" * 64)
    assert m["row_count"] == a["FRA"].num_rows and m["parser_version"] == lab.PARSER_VERSION


def test_derivation_refuses_drifted_bytes() -> None:
    doc = _doc("ilc_lvph01")
    doc["status"] = {"0": "q"}
    with pytest.raises(SourceResponseError):
        lab.derive_dataset_tables(json.dumps(doc).encode(), "ilc_lvph01")


def test_no_single_year_ages_are_claimed() -> None:
    ages = set(lab.TABLES_BY_CODE["lfsa_ergan"].filters["age"])
    assert ages == {"Y55-59", "Y60-64", "Y65-69"}


def test_unknown_geo_rejected() -> None:
    with pytest.raises(ValueError):
        lab.EurostatLabourPensionsConnector(geos=("FR", "XX"))
