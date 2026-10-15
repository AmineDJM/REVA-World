"""Atlas table schema validation, country-code mapping and the extra code file (offline)."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import polars as pl
import pytest

from reva.atlas import country_codes, tables
from reva.atlas.tables import (
    ATLAS_ARROW_SCHEMA,
    ATLAS_COLUMNS,
    ATLAS_SCHEMA,
    country_iso3,
    json_obj,
    missing_flags,
    to_arrow,
    validate_atlas_frame,
    year_period,
)
from reva.kernel.errors import ValidationProblem

RAW = "a" * 64


def row(**over: Any) -> dict[str, Any]:
    s, e = year_period(2021)
    base = {
        "source": "wb",
        "dataset": "wdi",
        "indicator": "SP.POP.TOTL",
        "concept_id": None,
        "iso3": None,
        "provider_geo": "FRA",
        "entity_qid": None,
        "period_start": s,
        "period_end": e,
        "value": "68170228",
        "value_text": None,
        "unit": "persons",
        "flags": "{}",
        "raw_sha256": RAW,
        "selector": json_obj(row=1, column="2021"),
    }
    return {**base, **over}


def frame(*rows: dict[str, Any]) -> pl.DataFrame:
    return pl.DataFrame(list(rows), schema=ATLAS_SCHEMA)


def check(df: pl.DataFrame, **kw: Any) -> tuple[pl.DataFrame, list[dict[str, Any]]]:
    return validate_atlas_frame(df, source="wb", dataset="wdi", raw_sha256s=[RAW], **kw)


def test_schema_columns_and_arrow_nullability() -> None:
    assert ATLAS_COLUMNS == (
        "source", "dataset", "indicator", "concept_id", "iso3", "provider_geo", "entity_qid", "period_start",
        "period_end", "value", "value_text", "unit", "flags", "raw_sha256", "selector",
    )  # fmt: skip
    nullable = {f.name for f in ATLAS_ARROW_SCHEMA if f.nullable}
    assert nullable == {"concept_id", "iso3", "entity_qid", "value", "value_text"}


def test_valid_frame_maps_geo_and_lists_unmapped_rows() -> None:
    df = frame(row(), row(provider_geo="WLD"), row(provider_geo="XKX"), row(provider_geo="DEU", value="-1.5e3"))
    out, unmapped = check(df, geo_scheme="wb")
    assert dict(zip(out["provider_geo"], out["iso3"], strict=True)) == {
        "DEU": "DEU", "FRA": "FRA", "WLD": None, "XKX": "XKX",
    }  # fmt: skip
    assert unmapped == [{"provider_geo": "WLD", "rows": 1}]
    assert out.height == 4  # unmapped rows kept
    assert to_arrow(out).schema.equals(ATLAS_ARROW_SCHEMA)


def test_row_order_does_not_change_registered_bytes() -> None:
    from reva.simulation.datasets import parquet_bytes

    df = frame(row(), row(provider_geo="DEU"), row(provider_geo="WLD"))
    a, _ = check(df, geo_scheme="wb")
    b, _ = check(df.reverse(), geo_scheme="wb")
    assert parquet_bytes(to_arrow(a)) == parquet_bytes(to_arrow(b))


@pytest.mark.parametrize(
    "over, match",
    [
        ({"value": "1,000"}, "strict decimal"),
        ({"value": "NaN"}, "strict decimal"),
        ({"value": "inf"}, "strict decimal"),
        ({"value": "007"}, "strict decimal"),
        ({"value": ""}, "strict decimal"),
        ({"value": None}, "flags.missing"),
        ({"value": None, "flags": json_obj(note="x")}, "flags.missing"),
        ({"source": "imf"}, "column source"),
        ({"period_end": date(2021, 1, 1)}, "half-open"),
        ({"raw_sha256": "b" * 64}, "raw_sha256s"),
        ({"flags": "not json"}, "flags is not valid JSON"),
        ({"flags": "[1]"}, "JSON object"),
        ({"selector": "{}"}, "selector must be a non-empty"),
        ({"entity_qid": "142"}, "entity_qid"),
        ({"concept_id": "Bad Id"}, "concept_id"),
        ({"iso3": "ZZZ"}, "crosswalk entity keys"),
        ({"unit": ""}, "unit must be"),
        ({"indicator": "a\nb"}, "indicator must be"),
    ],
)
def test_refusals(over: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationProblem, match=match):
        check(frame(row(**over)))


def test_missing_with_reason_and_text_cells_accepted() -> None:
    out, _ = check(
        frame(
            row(value=None, flags=missing_flags("not_reported", provider_flag="..")),
            row(provider_geo="DEU", value=None, value_text="Yes", unit="1"),
        )
    )
    assert out.height == 2
    with pytest.raises(ValueError):
        missing_flags("zero")


def test_structure_refusals() -> None:
    with pytest.raises(ValidationProblem, match="columns do not match"):
        check(frame(row()).drop("selector"))
    with pytest.raises(ValidationProblem, match="columns do not match"):
        check(frame(row()).with_columns(extra=pl.lit(1)))
    with pytest.raises(ValidationProblem, match="at least one row"):
        check(frame(row()).head(0))
    with pytest.raises(ValidationProblem, match="duplicate cells"):
        check(frame(row(), row(value="2")))
    with pytest.raises(ValidationProblem, match="type"):
        check(frame(row()).with_columns(period_start=pl.lit("not a date")))
    with pytest.raises(ValidationProblem, match="required columns contain nulls"):
        check(frame(row(unit=None)))
    with pytest.raises(ValidationProblem, match="no dots"):
        validate_atlas_frame(frame(row()), source="world.bank", dataset="wdi", raw_sha256s=[RAW])
    with pytest.raises(ValidationProblem, match="disagrees"):
        check(frame(row(iso3="DEU")), geo_scheme="wb")
    with pytest.raises(ValidationProblem, match="map to the same country"):
        check(frame(row(iso3="FRA"), row(provider_geo="250", iso3="FRA")))
    _, unmapped = check(frame(row(), row(provider_geo="250")), geo_scheme="iso3")  # 250 unmapped: kept
    assert unmapped == [{"provider_geo": "250", "rows": 1}]


def test_country_codes_schemes() -> None:
    assert country_iso3("FRA", "iso3") == "FRA"
    assert country_iso3("fr", "iso2") == "FRA"
    assert country_iso3("004", "m49") == country_iso3("4", "iso_numeric") == "AFG"
    assert country_iso3("251", "comtrade") == "FRA"  # Comtrade France (incl. Monaco) reporter code
    assert country_iso3("WLD", "wb") is None and country_iso3("XKX", "wb") == "XKX"
    assert country_iso3("EL", "eurostat") == "GRC" and country_iso3("UK", "eurostat") == "GBR"
    assert country_iso3("GR", "eurostat") is None and country_iso3("XK", "eurostat") == "XKX"
    assert country_iso3("GM", "fips") == "DEU" and country_iso3("KV", "fips") == "XKX"
    assert country_iso3(None, "iso3") is None
    with pytest.raises(ValueError, match="unknown geo scheme"):
        country_iso3("FR", "klingon")


def test_extra_code_file_is_rederivable_from_the_archive() -> None:
    committed = json.loads(country_codes.OUT.read_text())
    assert committed == json.loads(json.dumps(country_codes.build()))
    assert committed["schemes"]["fips"]["source"]["sha256"] == (
        "c55f638cfaafb4fe45a9aa15120a0f1f78e8a34a8e1847d7b9320cf740fbc9ab"
    )


def test_geo_alias_files_extend_without_conflicts(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    d = tmp_path / "geo_aliases"
    d.mkdir()
    (d / "a_owid.yaml").write_text(
        "scheme: owid\nbase: iso3\naliases:\n  - {code: OWID_KOS, iso3: XKX, source: test}\n", encoding="utf-8"
    )
    monkeypatch.setattr(tables, "GEO_ALIASES_DIR", d)
    tables.geo_index.cache_clear()
    try:
        assert country_iso3("OWID_KOS", "owid") == "XKX" and country_iso3("FRA", "owid") == "FRA"
        (d / "b_bad.yaml").write_text("scheme: owid\naliases: {FRA: DEU}\n", encoding="utf-8")
        tables.geo_index.cache_clear()
        with pytest.raises(ValidationProblem, match="already maps"):
            tables.geo_index()
        (d / "b_bad.yaml").write_text("scheme: x\naliases: {Q: ZZZ}\n", encoding="utf-8")
        tables.geo_index.cache_clear()
        with pytest.raises(ValidationProblem, match="not a crosswalk entity"):
            tables.geo_index()
    finally:
        tables.geo_index.cache_clear()


def test_registered_geo_alias_files_are_valid() -> None:
    tables.geo_index.cache_clear()
    idx = tables.geo_index()
    assert {"iso3", "iso2", "m49", "wb", "comtrade", "eurostat", "fips"} <= set(idx.schemes())
