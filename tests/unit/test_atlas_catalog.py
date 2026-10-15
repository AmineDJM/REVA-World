"""Atlas catalog: the registered domain files validate against _schema.json; loader rules (offline)."""

from __future__ import annotations

from pathlib import Path

import pytest

from reva.atlas.catalog import CATALOG_DIR, Series, load_catalog
from reva.kernel.errors import ValidationProblem

GOOD = """
domain: labour
concepts:
  - id: labour.unemployment_rate
    labels: {fr: "Taux de chômage", en: "Unemployment rate"}
    definition: "ILO definition."
    unit: "%"
    domain: labour
    subdomain: unemployment
    source_order: ["ilo.ilostat_une.UNE_RT|sex=T", {source: wb, dataset: wdi, indicator: SL.UEM.TOTL.ZS}]
    provider_declared: true
"""


def test_registered_catalog_files_are_valid() -> None:
    """Every registry/atlas/catalog/<domain>.yaml written by an atlas agent must pass (strict)."""
    cat = load_catalog(CATALOG_DIR, strict=True)
    for c in cat.concepts.values():
        assert c.provider_declared and c.source_order
        assert c.domain == Path(c.file).stem


def test_series_parsing_splits_on_the_first_two_dots() -> None:
    s = Series.parse("wb.wdi.SP.POP.TOTL")
    assert (s.source, s.dataset, s.indicator) == ("wb", "wdi", "SP.POP.TOTL")
    with pytest.raises(ValidationProblem):
        Series.parse("wb.wdi")


def test_loader_accepts_both_series_forms(tmp_path: Path) -> None:
    (tmp_path / "labour.yaml").write_text(GOOD, encoding="utf-8")
    cat = load_catalog(tmp_path)
    c = cat.concepts["labour.unemployment_rate"]
    assert [s.key for s in c.source_order] == ["ilo.ilostat_une.UNE_RT|sex=T", "wb.wdi.SL.UEM.TOTL.ZS"]
    assert cat.series_rows() == [
        ("labour.unemployment_rate", 0, "ilo", "ilostat_une", "UNE_RT|sex=T"),
        ("labour.unemployment_rate", 1, "wb", "wdi", "SL.UEM.TOTL.ZS"),
    ]
    assert [x.id for x in cat.search("chômage")] == ["labour.unemployment_rate"]
    assert cat.search("nothing-like-this") == []


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda t: t.replace("provider_declared: true", "provider_declared: false"), "provider_declared"),
        (lambda t: t.replace("domain: labour\nconcepts", "domain: health\nconcepts"), "file name"),
        (lambda t: t.replace('fr: "Taux de chômage", ', ""), "fr"),
        (lambda t: t.replace('"ilo.ilostat_une.UNE_RT|sex=T"', '"ilo.UNE_RT"'), "source_order"),
        (lambda t: t.replace('"ilo.ilostat_une.UNE_RT|sex=T"', '"wb.wdi.SL.UEM.TOTL.ZS"'), "source_order"),
        (lambda t: t.replace("unit:", "colour: red\n    unit:"), "colour"),
    ],
)
def test_loader_refuses_invalid_files(tmp_path: Path, mutate, match: str) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "labour.yaml").write_text(mutate(GOOD), encoding="utf-8")
    with pytest.raises(ValidationProblem) as e:
        load_catalog(tmp_path)
    assert match in str(e.value.details) or match in str(e.value)
    lax = load_catalog(tmp_path, strict=False)
    assert lax.errors and not lax.concepts


def test_duplicate_ids_across_files_are_refused(tmp_path: Path) -> None:
    (tmp_path / "labour.yaml").write_text(GOOD, encoding="utf-8")
    (tmp_path / "work.yaml").write_text(
        GOOD.replace("labour", "work").replace("work.unemployment", "labour.unemployment")
    )
    with pytest.raises(ValidationProblem, match=r"work\.yaml"):
        load_catalog(tmp_path)
