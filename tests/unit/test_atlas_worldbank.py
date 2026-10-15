"""World Bank atlas layer (agent A-WB): offline parser and catalog checks on recorded slices.

Fixtures: ``tests/fixtures/source/atlas.worldbank`` -- tiny slices of the real 2026-10-15 bulk ZIPs
(same members, encodings and member timestamps; rows limited to a few economies, aggregates and
unmapped codes), of the French indicator lists and of the pinned PIP release.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import zipfile
from pathlib import Path

import polars as pl
import pytest
import yaml

from reva.adapters.connectors import atlas_worldbank as wb
from reva.adapters.connectors.atlas_worldbank_catalog import build_catalog, concept_id, reva_domain
from reva.atlas.catalog import SCHEMA_PATH, load_catalog
from reva.atlas.tables import validate_atlas_frame
from reva.evidence.runner import _fixture_fingerprint
from reva.kernel.errors import SourceResponseError

FIX = Path(__file__).parents[1] / "fixtures" / "source" / "atlas.worldbank"
MANIFEST = {e["request_fingerprint"]: e for e in json.loads((FIX / "manifest.json").read_text())}


def body(url: str) -> bytes:
    e = MANIFEST[_fixture_fingerprint(url)]
    data = (FIX / e["file"]).read_bytes()
    assert hashlib.sha256(data).hexdigest() == e["sha256"]
    return data


def parsed(key: str) -> wb.ParsedDatabase:
    db = wb.DATABASES_BY_KEY[key]
    b = body(db.url)
    return wb.parse_bulk(db, b, hashlib.sha256(b).hexdigest())


def frame(p: wb.ParsedDatabase) -> pl.DataFrame:
    return pl.concat([t.frame for t in p.tables])


def cell(f: pl.DataFrame, ind: str, geo: str, year: int) -> list[dict]:
    return f.filter(
        (pl.col("indicator") == ind) & (pl.col("provider_geo") == geo) & (pl.col("period_start").dt.year() == year)
    ).to_dicts()


@pytest.mark.parametrize("key", [d.key for d in wb.DATABASES])
def test_every_database_slice_validates_against_the_atlas_schema(key: str) -> None:
    p = parsed(key)
    assert p.tables
    for t in p.tables:
        f, _ = validate_atlas_frame(t.frame, source="wb", dataset=t.dataset, raw_sha256s=[p.raw_sha256])
        assert f.height == t.frame.height > 0
        assert set(f["dataset"].unique()) == {t.dataset}
        note = json.loads(t.coverage_note)
        assert note["empty_cells"] == "absent (no row)"


def test_values_are_the_provider_text_and_empty_cells_are_absent() -> None:
    f = frame(parsed("wdi"))
    [pop] = cell(f, "SP.POP.TOTL", "FRA", 2020)
    assert (pop["value"], pop["iso3"], pop["unit"]) == ("67601110", "FRA", "Unit")  # WDI "Unit of measure"
    assert json.loads(pop["selector"]) == {"column": "2020", "file": "WDICSV.csv", "row": 34}
    # ILO modelled unemployment starts in 1991: no 1960-1990 rows (never zero-filled)
    une = f.filter((pl.col("indicator") == "SL.UEM.TOTL.ZS") & (pl.col("provider_geo") == "DZA"))
    assert une["period_start"].min().year == 1991
    assert une["value"].is_not_null().all() and not (une["value"] == "0").any()
    assert f["value"].str.contains(wb.DECIMAL_RE).all()
    [gdp] = cell(f, "NY.GDP.MKTP.CD", "FRA", 2020)
    assert gdp["unit"] == "Current US$"


def test_aggregates_are_excluded_and_listed() -> None:
    p = parsed("wdi")
    f = frame(p)
    assert not f["provider_geo"].is_in(["WLD", "HIC"]).any()
    assert set(p.excluded_aggregate_cells) == {"HIC", "WLD"}
    note = json.loads(p.tables[0].coverage_note)
    assert note["excluded_aggregates"] == p.excluded_aggregate_cells
    assert p.report["excluded_aggregate_cells"] == sum(p.excluded_aggregate_cells.values()) > 0


def test_wdi_is_split_by_topic_into_two_tables() -> None:
    p = parsed("wdi")
    by = {t.dataset: set(t.frame["indicator"].unique()) for t in p.tables}
    assert set(by) == {"wdi_econ", "wdi_social"}
    assert "NY.GDP.MKTP.CD" in by["wdi_econ"] and "NE.RSB.GNFS.CD" in by["wdi_econ"]
    assert "SP.POP.TOTL" in by["wdi_social"] and "SL.UEM.TOTL.ZS" in by["wdi_social"]


def test_unmapped_codes_are_kept_and_iso3_fallback_is_flagged() -> None:
    p = parsed("wgi")
    f = frame(p)
    assert p.geo.unmapped == {"ANT": "Netherlands Antilles", "TWN": "Taiwan, China"}
    assert f.filter(pl.col("provider_geo") == "TWN")["iso3"].is_null().all()
    aia = f.filter(pl.col("provider_geo") == "AIA")
    assert (aia["iso3"] == "AIA").all() and aia["flags"].str.contains('"geo_via":"iso3"').all()
    db = parsed("doingbusiness")
    assert "CHN_BEI" in db.geo.unmapped and frame(db).filter(pl.col("provider_geo") == "CHN_BEI").height > 0


def test_footnotes_and_projection_flags() -> None:
    f = frame(parsed("wdi"))
    noted = f.filter(pl.col("flags").str.contains('"footnote"'))
    assert noted.height > 0
    assert all(isinstance(json.loads(x)["footnote"], str) for x in noted["flags"].to_list())
    ed = frame(parsed("edstats"))  # 2018 release, Wittgenstein projections to 2100
    after = ed.filter(pl.col("period_start").dt.year() > 2018)
    assert after.height > 0 and after["flags"].str.contains('"period_after_vintage":true').all()
    assert not ed.filter(pl.col("period_start").dt.year() <= 2018)["flags"].str.contains("period_after_vintage").any()


def test_ids_counterpart_is_folded_and_footer_rows_skipped() -> None:
    p = parsed("ids")
    f = frame(p)
    assert set(f["indicator"].unique()) == {"DT.DOD.DECT.CD|counterpart=WLD", "DT.TDS.DECT.CD|counterpart=WLD"}
    assert p.report["footer_rows_skipped"] == 5
    assert json.loads(f["selector"][0])["counterpart"] == "WLD"
    assert f["provider_geo"].str.contains(" ").sum() == 0  # padding stripped
    assert p.report["encodings"]  # IDS metadata is Windows-1252


def test_decode_falls_back_to_cp1252() -> None:
    assert wb.decode("Côte d’Ivoire".encode("cp1252")) == ("Côte d’Ivoire", "cp1252")
    assert wb.decode(b"\xef\xbb\xbfabc") == ("abc", "utf-8")


def test_non_zip_and_html_bodies_are_refused() -> None:
    item = wb.bulk_item(wb.DATABASES_BY_KEY["wgi"])
    with pytest.raises(SourceResponseError):
        wb.check_body(item, b"<!DOCTYPE html><html>maintenance</html>")
    with pytest.raises(SourceResponseError):
        wb.check_body(item, b'{"error": 1}')
    with pytest.raises(SourceResponseError):
        wb.read_members(b"PK\x03\x04 truncated")


def test_zip_without_data_member_is_refused() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Series.csv", "Series Code,Indicator Name\nX,Y\n")
    with pytest.raises(SourceResponseError):
        wb.read_members(buf.getvalue())


def pip_bodies() -> dict[str, tuple[bytes, str]]:
    out = {}
    for line in wb.PIP_POVERTY_LINES:
        b = body(wb.pip_item(line).url)
        out[line] = (b, hashlib.sha256(b).hexdigest())
    return out


def test_pip_folds_dimensions_and_checks_line_independent_measures() -> None:
    bodies = pip_bodies()
    p = wb.parse_pip(bodies)
    f, unmapped = validate_atlas_frame(
        p.tables[0].frame, source="wb", dataset="pip", raw_sha256s=[s for _, s in bodies.values()]
    )
    inds = set(f["indicator"].unique())
    assert "headcount|povline=3.00|level=national|welfare=consumption" in inds
    assert any(i.startswith("gini|level=") for i in inds) and not any(i.startswith("gini|povline") for i in inds)
    assert unmapped == [{"provider_geo": "TWN", "rows": f.filter(pl.col("provider_geo") == "TWN").height}]
    assert f["flags"].str.contains(wb.PIP_VERSION).all()
    assert (f["concept_id"] == f["indicator"].map_elements(wb.pip_concept_id, return_dtype=pl.String)).all()
    # A release whose distribution measures differ between poverty-line files is refused.
    b3, s3 = bodies["3.00"]
    text = b3.decode()
    head, first, rest = text.split("\n", 2)
    cols = first.split(",")
    cols[head.split(",").index("gini")] = "0.999"
    tampered = "\n".join([head, ",".join(cols), rest]).encode()
    with pytest.raises(SourceResponseError):
        wb.parse_pip({**bodies, "4.20": (tampered, s3)})


def test_fr_labels_parse_and_paged_lists_are_refused() -> None:
    fr = wb.parse_fr_labels(body(wb.fr_item(wb.DATABASES_BY_KEY["wdi"]).url))
    assert fr["SL.UEM.TOTL.ZS"]["name"].startswith("Chômage")
    with pytest.raises(SourceResponseError):
        wb.parse_fr_labels(json.dumps([{"page": 1, "pages": 2, "total": 3}, [{"id": "X", "name": "x"}]]).encode())


def test_domain_mapping_of_key_concepts() -> None:
    assert reva_domain("SP.POP.TOTL", "wdi", "Health: Population: Structure") == "population"
    assert reva_domain("EN.POP.DNST", "wdi", "Environment: Land use") == "population"
    assert reva_domain("AG.LND.TOTL.K2", "wdi", "Environment: Land use") == "geography"
    assert reva_domain("SL.UEM.TOTL.ZS", "wdi", "Social Protection & Labor: Unemployment") == "labour"
    assert reva_domain("NY.GDP.MKTP.CD", "wdi", "Economic Policy & Debt: National accounts") == "economy"
    assert reva_domain("NE.RSB.GNFS.CD", "wdi", "Economic Policy & Debt: National accounts") == "trade"
    assert reva_domain("GOV_WGI_CC.EST", "wgi", "Control of Corruption") == "governance"
    assert concept_id("GOV_WGI_CC.EST") == "wb.gov_wgi_cc.est"


def test_catalog_is_valid_for_the_atlas_loader(tmp_path: Path) -> None:
    series, units, datasets, fr, shas = {}, {}, {}, {}, {}
    for db in wb.DATABASES:
        p = parsed(db.key)
        series[db.key], units[db.key], datasets[db.key], shas[db.key] = p.series, p.units, p.datasets, p.raw_sha256
        fr[db.key] = wb.parse_fr_labels(body(wb.fr_item(db).url))
    pip = wb.parse_pip(pip_bodies())
    doc, stats = build_catalog(series, units, datasets, fr, raw_sha256s=shas, pip_indicators=sorted(pip.units))
    shutil.copy(SCHEMA_PATH, tmp_path / "_schema.json")
    (tmp_path / "worldbank.yaml").write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
    cat = load_catalog(tmp_path)  # strict: raises on any schema problem
    une = cat.concepts["wb.sl.uem.totl.zs"]
    keys = [s.key for s in une.source_order]
    assert keys[0] == "wb.wdi_social.SL.UEM.TOTL.ZS" and "wb.jobs.SL.UEM.TOTL.ZS" in keys
    assert une.labels["fr"].startswith("Chômage") and "fr_label:provider" in une.tags
    assert une.subdomain == "labour" and "reva_domain:labour" in une.tags
    pop = cat.concepts["wb.sp.pop.totl"]
    assert "fr_label:agent_a_wb" in pop.tags and pop.labels["fr"] == "Population totale"
    ids = cat.concepts["wb.dt.dod.dect.cd"]
    assert [s.key for s in ids.source_order] == ["wb.ids.DT.DOD.DECT.CD|counterpart=WLD"]
    missing = [c for c in cat.concepts.values() if "fr_label:missing" in c.tags]
    assert all(c.labels["fr"] == c.labels["en"] for c in missing)  # English kept, never machine-translated
    assert stats["fr"]["missing"] == len(missing)
    assert all(c.provider_declared for c in cat.concepts.values())
    assert cat.concepts["wb.pip.headcount.pl3_00.national.consumption"].unit.startswith("share")
