"""A-HEALTHEDU atlas connectors (WHO GHO, UNESCO UIS): offline parsing against captured fixtures.

Fixtures (tests/fixtures/source/atlas.who_gho, atlas.unesco_uis) are real responses captured on
2026-10-15 for small subsets (WHO: 6 indicators x DZA/FRA/HKG/IND/NGA; UIS: 5 indicators in 2 batches x
DZA/FRA/IND/NGA/TUR, data version 20260507-91260335).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import polars as pl
import pytest
import yaml

from reva.adapters.connectors import atlas_uis as uis
from reva.adapters.connectors import atlas_who as who
from reva.adapters.connectors.atlas_healthedu_common import MAX_FLAGS_CHARS, AtlasConnectorSpec, fit_flags, items_ok
from reva.atlas.catalog import Series, load_catalog
from reva.atlas.tables import validate_atlas_frame
from reva.evidence.runner import FIXTURE_ROOT, FixtureHttp
from reva.kernel.errors import SourceResponseError, ValidationProblem
from reva.ports.connectors import FetchBudget, FetchOutcome

WHO_IND = ["WHOSIS_000001", "MDG_0000000032", "MDG_0000000007", "M_Est_tob_curr_std", "GHED_CHEGDP_SHA2011", "RS_198"]
WHO_CTRY = ["DZA", "FRA", "HKG", "IND", "NGA"]
UIS_BATCHES = [["LR.AG15T99", "LR.AG15T99.F"], ["XGDP.FSGOV", "EXPGDP.TOT", "CR.MOD.1"]]
UIS_CTRY = ["DZA", "FRA", "IND", "NGA", "TUR"]
CATALOG = Path(__file__).resolve().parents[2] / "registry" / "atlas" / "catalog"


def who_conn() -> who.WhoGhoConnector:
    return who.WhoGhoConnector(WHO_IND, WHO_CTRY)


def uis_conn() -> uis.UisConnector:
    return uis.UisConnector(UIS_BATCHES, UIS_CTRY)


async def _fetch_all(conn: Any) -> tuple[dict[str, bytes], dict[str, str]]:
    http = FixtureHttp(conn.describe().name)
    queue = list(conn.plan(None, FetchBudget()).items)
    bodies: dict[str, bytes] = {}
    while queue:
        item = queue.pop(0)
        raw = await conn.fetch(item, http)
        for b in conn.parse(raw):
            assert conn.validate(b).outcome == "pass", (item.key, b.metadata)
            queue.extend(b.follow_up)
        bodies[item.key] = raw.body
    import hashlib

    return bodies, {k: hashlib.sha256(v).hexdigest() for k, v in bodies.items()}


@pytest.fixture(scope="module")
def who_frame() -> pl.DataFrame:
    import asyncio

    c = who_conn()
    bodies, raws = asyncio.run(_fetch_all(c))
    frame, _note, extra = c.atlas_frame(bodies, raws)
    v, unmapped = validate_atlas_frame(
        frame, source="who", dataset="gho", raw_sha256s=list(raws.values()), geo_scheme="iso3"
    )
    assert unmapped == []
    assert set(extra["indicator_names"]) == set(WHO_IND)
    return v


@pytest.fixture(scope="module")
def uis_frame() -> pl.DataFrame:
    import asyncio

    c = uis_conn()
    bodies, raws = asyncio.run(_fetch_all(c))
    frame, note, _extra = c.atlas_frame(bodies, raws)
    assert "20260507-91260335" in note
    v, unmapped = validate_atlas_frame(
        frame, source="uis", dataset="api", raw_sha256s=list(raws.values()), geo_scheme="iso3"
    )
    assert unmapped == []
    return v


def _cell(frame: pl.DataFrame, indicator: str, geo: str, year: int) -> dict[str, Any]:
    rows = frame.filter(
        (pl.col("indicator") == indicator)
        & (pl.col("provider_geo") == geo)
        & (pl.col("period_start").dt.year() == year)
    ).to_dicts()
    assert len(rows) == 1, (indicator, geo, year, rows)
    r = rows[0]
    r["flags"] = json.loads(r["flags"])
    r["selector"] = json.loads(r["selector"])
    return r


def _raw_row(conn_name: str, item_key: str, conn: Any, pred: Any) -> dict[str, Any]:
    man = json.loads((FIXTURE_ROOT / conn_name / "manifest.json").read_text())
    url = next(i.url for i in conn.items() if i.key == item_key)
    entry = next(e for e in man if e["url"] == url)
    doc = json.loads((FIXTURE_ROOT / conn_name / entry["file"]).read_text())
    return next(r for r in doc["value"] if pred(r))


# ------------------------------------------------------------------------------------------- WHO
def test_who_values_are_exact_source_text_with_sex_dimension_folded(who_frame: pl.DataFrame) -> None:
    c = _cell(who_frame, "WHOSIS_000001|sex=SEX_FMLE", "FRA", 2019)
    raw = _raw_row(
        "atlas.who_gho",
        "gho.data:WHOSIS_000001",
        who_conn(),
        lambda r: r["SpatialDim"] == "FRA" and r["TimeDim"] == 2019 and r["Dim1"] == "SEX_FMLE",
    )
    assert c["selector"]["Id"] == str(raw["Id"])
    assert c["value"] == who.dec_str(who.Decimal(str(raw["NumericValue"])))
    assert c["iso3"] == "FRA" and c["unit"] == "years"
    assert c["flags"]["estimate_class"] == "who_comparable_estimate"
    assert c["flags"]["display_value"] == raw["Value"]
    assert c["flags"]["low"] is not None and c["flags"]["high"] is not None
    assert c["concept_id"] == "health.life_expectancy_birth.female"
    assert str(c["period_start"]) == "2019-01-01" and str(c["period_end"]) == "2020-01-01"


def test_who_three_dimensions_folded_and_comments_kept(who_frame: pl.DataFrame) -> None:
    ind = "MDG_0000000007|sex=SEX_BTSX|agegroup=AGEGROUP_YEARSUNDER5|wealthquintile=WEALTHQUINTILE_TOTL"
    c = _cell(who_frame, ind, "IND", 2020)
    assert c["concept_id"] == "health.under5_mortality"
    assert "not the official statistics of India" in c["flags"]["comments"]
    quint = who_frame.filter(pl.col("indicator").str.contains("WEALTHQUINTILE_WQ1")).height
    assert quint > 0  # survey-based quintile rows kept as their own series


def test_who_text_only_values_never_become_numbers(who_frame: pl.DataFrame) -> None:
    mmr = who_frame.filter(pl.col("indicator") == "MDG_0000000032")
    assert mmr.height > 0
    assert mmr["value"].null_count() == mmr.height
    assert mmr["value_text"].null_count() == 0
    flags = [json.loads(f) for f in mmr["flags"]]
    assert all(f["numeric_value_absent"] and f["estimate_class"] == "country_reported" for f in flags)
    hkg = _cell(who_frame, "RS_198", "HKG", 2021)
    assert hkg["value"] is None and hkg["value_text"] == "0.0"  # WHO shows '0.0' with no NumericValue


def test_who_projection_flag_only_from_provider_comment(who_frame: pl.DataFrame) -> None:
    tob = who_frame.filter(pl.col("indicator").str.starts_with("M_Est_tob_curr_std"))
    flags = [json.loads(f) for f in tob["flags"]]
    proj = [f for f in flags if f.get("projection")]
    assert proj and all("this is a projection" in f["comments"].lower() for f in proj)
    assert all("projection" not in f for f in flags if "this is a projection" not in f.get("comments", "").lower())


def test_who_ghed_is_labelled_mixed(who_frame: pl.DataFrame) -> None:
    g = who_frame.filter(pl.col("indicator") == "GHED_CHEGDP_SHA2011")
    assert g.height > 0
    assert {json.loads(f)["estimate_class"] for f in g["flags"]} == {"country_reported_with_who_estimates"}


def test_who_drift_is_refused() -> None:
    body = json.dumps({"value": [{k: None for k in who.SELECT if k != "Comments"}]}).encode()
    with pytest.raises(SourceResponseError):
        who.parse_data(body, "X")
    row = {k: None for k in who.SELECT} | {
        "IndicatorCode": "X",
        "SpatialDimType": "COUNTRY",
        "SpatialDim": "FRA",
        "TimeDimType": "YEAR_RANGE",
        "TimeDim": 2020,
    }
    with pytest.raises(SourceResponseError, match="TimeDimType"):
        who.parse_data(json.dumps({"value": [row]}).encode(), "X")
    row["TimeDimType"] = "YEAR"
    with pytest.raises(SourceResponseError, match="paged"):
        who.parse_data(json.dumps({"value": [row], "@odata.nextLink": "x"}).encode(), "X")
    with pytest.raises(SourceResponseError, match="dimension"):
        who.fold_indicator("X", {"Dim1Type": "SEX", "Dim1": None})
    row["NumericValue"] = "12"  # a string where a number is expected
    with pytest.raises(SourceResponseError, match="not a number"):
        who.parse_data(json.dumps({"value": [row]}).encode(), "X")


async def test_who_archived_or_missing_indicator_blocks_registration() -> None:
    c = who_conn()
    bodies, raws = await _fetch_all(c)
    doc = json.loads(bodies["gho.indicator_list"])
    for v in doc["value"]:
        if v["IndicatorCode"] == "RS_198":
            v["IndicatorName"] = "Archived, see RS_200"
    bodies["gho.indicator_list"] = json.dumps(doc).encode()
    with pytest.raises(ValidationProblem):
        c.atlas_frame(bodies, raws)


def test_who_empty_indicator_is_quarantined() -> None:
    from reva.ports.connectors import ObservationBatch

    b = ObservationBatch(
        connector="c",
        connector_version="1",
        item_key="gho.data:X",
        observations=(),
        metadata={"kind": "data", "indicator": "X", "rows": 0},
    )
    assert who_conn().validate(b).outcome == "quarantine"


def test_who_urls_request_country_rows_only() -> None:
    url = who.data_url("WHOSIS_000001")
    assert "SpatialDimType%20eq%20'COUNTRY'" in url and "$select=Id,IndicatorCode" in url
    sub = who.data_url("WHOSIS_000001", ["FRA"])
    assert "SpatialDim%20eq%20'FRA'" in sub
    assert {i.key for i in who.WhoGhoConnector().items()} == {"gho.indicator_list"} | {
        f"gho.data:{i.code}" for i in who.INDICATORS
    }


# ------------------------------------------------------------------------------------------- UIS
def test_uis_values_qualifiers_and_version(uis_frame: pl.DataFrame) -> None:
    flags = [json.loads(f) for f in uis_frame["flags"]]
    assert {f["uis_data_version"] for f in flags} == {"20260507-91260335"}
    nil = _cell(uis_frame, "XGDP.FSGOV", "TUR", 1998)
    assert nil["value"] == "0" and nil["flags"]["magnitude"] == "NIL"  # provider's report, not a filled zero
    mod = uis_frame.filter(pl.col("indicator") == "CR.MOD.1")
    assert {json.loads(f)["estimate_class"] for f in mod["flags"]} == {"uis_modelled"}
    lit = uis_frame.filter(pl.col("indicator") == "LR.AG15T99")
    assert lit.height > 0 and set(lit["concept_id"].to_list()) == {"education.literacy_rate_adult"}
    assert any("footnotes" in json.loads(f) for f in lit["flags"])


def test_uis_follow_ups_are_pinned_to_the_version() -> None:
    c = uis.UisConnector()
    items = c.follow_ups("20260507-91260335")
    assert len(items) == len(uis.BATCHES)
    assert all("version=20260507-91260335" in i.url and "geoUnitType=NATIONAL" in i.url for i in items)
    assert sorted(c.expected_item_keys()) == sorted(["uis.version"] + [i.key for i in items])
    assert len({i.code for i in uis.INDICATORS}) == len(uis.INDICATORS)


def test_uis_drift_and_caps() -> None:
    rec = {
        "indicatorId": "OTHER",
        "geoUnit": "FRA",
        "year": 2020,
        "value": 1.5,
        "magnitude": None,
        "qualifier": None,
        "footnotes": [],
    }
    with pytest.raises(SourceResponseError, match="unrequested"):
        uis.parse_data(json.dumps({"records": [rec]}).encode(), ["LR.AG15T99"])
    rec["indicatorId"] = "LR.AG15T99"
    del rec["footnotes"]
    with pytest.raises(SourceResponseError, match="lacks"):
        uis.parse_data(json.dumps({"records": [rec]}).encode(), ["LR.AG15T99"])
    with pytest.raises(SourceResponseError):
        uis.parse_version(b'{"nothing": 1}')
    from reva.ports.connectors import ObservationBatch

    def b(n: int) -> ObservationBatch:
        return ObservationBatch(
            connector="c",
            connector_version="1",
            item_key="uis.data:1",
            observations=(),
            metadata={"kind": "data", "records": n},
        )

    assert uis_conn().validate(b(0)).outcome == "quarantine"
    assert uis_conn().validate(b(100_000)).outcome == "quarantine"
    assert uis_conn().validate(b(10)).outcome == "pass"


async def test_uis_missing_indicator_metadata_blocks_registration() -> None:
    c = uis_conn()
    bodies, raws = await _fetch_all(c)
    doc = json.loads(bodies["uis.data:2"])
    doc["indicatorMetadata"] = [m for m in doc["indicatorMetadata"] if m["indicatorCode"] != "EXPGDP.TOT"]
    bodies["uis.data:2"] = json.dumps(doc).encode()
    with pytest.raises(ValidationProblem):
        c.atlas_frame(bodies, raws)


# ------------------------------------------------------------------------------------------- shared
def test_specs_are_atlas_specs() -> None:
    for c in (who.WhoGhoConnector(), uis.UisConnector()):
        spec = c.describe()
        assert isinstance(spec, AtlasConnectorSpec)
        assert spec.model_dump(mode="json")["template"].startswith("atlas_")
        assert spec.max_response_bytes <= 52_428_800
    assert who.WhoGhoConnector().describe().source_key == "who.gho"
    assert uis.UisConnector().describe().source_key == "unesco.uis"


def test_fit_flags_never_exceeds_limit_and_says_when_it_shortened() -> None:
    long = {"estimate_class": "x", "comments": "a" * 10_000}
    s = fit_flags(long, long_keys=("comments",))
    assert len(s) <= MAX_FLAGS_CHARS and json.loads(s)["notes_truncated"] is True
    fn = {"footnotes": [{"type": "Source", "value": "b" * 3000}] * 5}
    s2 = fit_flags(fn, long_keys=("footnotes",))
    assert len(s2) <= MAX_FLAGS_CHARS and json.loads(s2)["notes_truncated"] is True
    assert fit_flags({"a": 1}) == '{"a":1}'


def test_items_ok_requires_every_planned_item() -> None:
    o = [
        FetchOutcome(item_key="a", status="published", raw_sha256="0" * 64),
        FetchOutcome(item_key="b", status="quarantined", raw_sha256="1" * 64),
    ]
    ok, bad = items_ok(o, ["a", "b", "c"])
    assert not ok and {x["item"] for x in bad} == {"b", "c"}


def test_catalog_series_point_at_curated_indicators_with_matching_units() -> None:
    cat = load_catalog()
    assert not getattr(cat, "errors", [])
    who_units = {i.code: i.unit for i in who.INDICATORS}
    uis_units = {i.code: i.unit for i in uis.INDICATORS}
    seen_who, seen_uis = set(), set()
    for fname, domain in (("health.yaml", "health"), ("education.yaml", "education")):
        doc = yaml.safe_load((CATALOG / fname).read_text())
        assert doc["domain"] == domain
        for c in doc["concepts"]:
            assert c["provider_declared"] is True
            for s in c["source_order"]:
                ser = Series.parse(s)
                if (ser.source, ser.dataset) == ("who", "gho"):
                    code = ser.indicator.split("|")[0]
                    assert code in who_units, s
                    assert who_units[code] == c["unit"], (c["id"], who_units[code], c["unit"])
                    seen_who.add(code)
                elif (ser.source, ser.dataset) == ("uis", "api"):
                    assert ser.indicator in uis_units, s
                    assert uis_units[ser.indicator] == c["unit"], (c["id"], uis_units[ser.indicator], c["unit"])
                    seen_uis.add(ser.indicator)
                else:
                    assert (ser.source, ser.dataset) == ("wb", "wdi_social"), s
            assert c["source_order"][0].startswith(("who.gho.", "uis.api.")), c["id"]
    assert seen_who == set(who_units), set(who_units) - seen_who
    assert seen_uis == set(uis_units), set(uis_units) - seen_uis


def test_concept_hints_match_catalog() -> None:
    for folded, concept in who.CONCEPT_HINTS.items():
        assert folded.split("|")[0] in who.INDICATORS_BY_CODE and concept.startswith("health.")
    for code, concept in uis.CONCEPT_HINTS.items():
        assert code in uis.INDICATORS_BY_CODE and concept.startswith(("education.", "rd."))
