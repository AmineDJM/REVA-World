"""`reva qa` in fixture mode: every domain runs on a DuckDB + in-memory object store built from small frames, and
each planted defect is found (and only those). No database or network needed."""

from __future__ import annotations

import hashlib
import io
import json
from datetime import date
from typing import Any

import polars as pl
import pytest

from reva.atlas.tables import ATLAS_SCHEMA
from reva.qa.backend import MemoryStore, fixture_backend, lit
from reva.qa.config import load_config
from reva.qa.runner import run_qa

TODAY = date(2026, 10, 17)
D = date


def _cells(rows: list[dict[str, object]], source: str, dataset: str, raw: str) -> bytes:
    full = []
    for r in rows:
        full.append(
            {
                "source": source,
                "dataset": dataset,
                "concept_id": None,
                "entity_qid": None,
                "value_text": None,
                "flags": "{}",
                "raw_sha256": raw,
                "selector": "{}",
                "provider_geo": r.get("iso3") or "X",
                **r,
            }
        )
    buf = io.BytesIO()
    pl.DataFrame(full, schema=ATLAS_SCHEMA).write_parquet(buf)
    return buf.getvalue()


def _year(y: int) -> dict[str, D]:
    return {"period_start": D(y, 1, 1), "period_end": D(y + 1, 1, 1)}


@pytest.fixture(scope="module")
def report() -> dict[str, object]:
    st = MemoryStore()
    raw_wdi = st.put_bytes(b"raw wdi bytes", "raw")
    raw_wpp = st.put_bytes(b"raw wpp bytes", "raw")
    raw_kg = st.put_bytes(b"raw kg bytes", "raw")
    raw_unlicensed = st.put_bytes(b"raw bytes without rights", "raw")
    corrupt = hashlib.sha256(b"original").hexdigest()
    st.objects[("raw", corrupt)] = b"tampered"  # bytes no longer hash to their address
    missing = hashlib.sha256(b"never stored").hexdigest()

    wdi = [
        {"indicator": "SP.POP.TOTL", "iso3": "DEU", "value": v, "unit": "Unit", **_year(y)}
        for y, v in ((2019, "83000000"), (2020, "83100000"), (2021, "83200000"), (2022, "83300000"), (2023, "83400000"))
    ] + [
        {"indicator": "QA.SHARE", "iso3": "DEU", "value": "120", "unit": "% of population", **_year(2020)},
        {"indicator": "QA.SHARE", "iso3": "DEU", "value": "50", "unit": "% of population", **_year(2030)},
        {"indicator": "QA.COUNT", "iso3": "DEU", "value": "-5", "unit": "persons", **_year(2020)},
        {
            "indicator": "QA.COUNT",
            "iso3": "DEU",
            "value": "7",
            "unit": "persons",
            **_year(2021),
            "raw_sha256": "f" * 64,
        },
    ]
    wpp = [
        {"indicator": "TPopulation1July", "iso3": "DEU", "value": v, "unit": "thousands", **_year(y)}
        for y, v in ((2019, "83000"), (2020, "83100"), (2021, "83200"), (2022, "91500"), (2023, "83400"))
    ]
    proj = [
        {
            "indicator": "PopTotal|proj=medium",
            "iso3": "DEU",
            "value": "1",
            "unit": "thousands",
            **_year(2050),
            "flags": '{"projection":true}',
        },
        {"indicator": "PopTotal|proj=medium", "iso3": "DEU", "value": "1", "unit": "thousands", **_year(2051)},
    ]
    vint = [
        {
            "indicator": "X|v=2024",
            "iso3": "DEU",
            "value": "1",
            "unit": "%",
            **_year(2025),
            "flags": '{"available_at":"2024-10-01T00:00:00Z","estimate_kind":"actual"}',
        },
        {
            "indicator": "X|v=2024",
            "iso3": "DEU",
            "value": "1",
            "unit": "%",
            **_year(2023),
            "flags": '{"available_at":"2024-10-01T00:00:00Z","estimate_kind":"actual"}',
        },
    ]
    vint.append(
        {
            "indicator": "Y",
            "iso3": "DEU",
            "value": "2",
            "unit": "%",
            **_year(2020),
            "flags": '{"estimate_kind":"forecast"}',
        }
    )
    tables = {
        ("wb", "wdi_social", raw_wdi): wdi,
        ("un_wpp", "wpp2024_demo", raw_wpp): wpp,
        ("un_wpp", "wpp2024_age5_proj", raw_wpp): proj,
        ("qafix", "vintage", raw_wpp): vint,
    }
    ds_rows, art_rows, loc_rows = [], [], []
    for (src, ds, raw), rows in tables.items():
        sha = st.put_bytes(_cells(rows, src, ds, raw), "normalised")
        ds_rows.append(
            {
                "dataset_key": f"atlas.{src}.{ds}",
                "subject_key": f"{src}.{ds}",
                "concept_id": "atlas.table",
                "origin": "live",
                "artifact_sha256": sha,
                "row_count": len(rows),
                "coverage": json.dumps({"raw_sha256s": [raw]}),
                "recorded_at": "2026-10-01T00:00:00",
            }
        )
        rights = None if src == "qafix" else json.dumps({"licence": "CC BY 4.0"})
        art_rows.append({"sha256": sha, "rights": rights})
        loc_rows.append({"artifact_sha256": sha, "namespace": "normalised", "verified_byte_size": 100})
    for sha, rights in (
        (raw_wdi, "CC BY 4.0"),
        (raw_wpp, "CC BY 3.0 IGO"),
        (raw_kg, "CC0"),
        (raw_unlicensed, None),
        (corrupt, "CC0"),
        (missing, "CC0"),
    ):
        art_rows.append({"sha256": sha, "rights": json.dumps({"licence": rights}) if rights else "{}"})
        loc_rows.append({"artifact_sha256": sha, "namespace": "raw", "verified_byte_size": 20})

    def stmt(
        i: int, s: str, p: str, o: str | None, raw: str | None = raw_kg, valid_lo: D | None = None
    ) -> dict[str, Any]:
        return {
            "id": i,
            "subject_qid": s,
            "property_pid": p,
            "object_qid": o,
            "origin": "live",
            "source_key": "qa.kg",
            "raw_sha256": raw,
            "valid_lo": valid_lo,
            "value_time_lo": None,
        }

    statements = [
        stmt(1, "Q1", "P279", "Q2"),
        stmt(2, "Q2", "P279", "Q1"),  # is_a cycle
        stmt(3, "Q3", "P361", "Q4"),  # part_of, no cycle
        stmt(4, "Q5", "P131", "Q6"),  # located_in chain ending at Q6 (no country)
        stmt(5, "Q7", "P17", "Q183"),  # Germany: reaches a country
        stmt(6, "Q8", "P31", "Q999"),  # dangling object
        stmt(7, "Q9", "P571", None, raw=None, valid_lo=D(2031, 1, 1)),  # no raw object, future-dated
        stmt(8, "Q9", "P828", "Q1", raw=raw_unlicensed),  # causal property kept in the KG, licence missing
    ]
    entities = [{"qid": q, "origin": "live"} for q in ("Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7", "Q8", "Q9", "Q183")]
    claims = [
        {
            "id": 10,
            "claim_key": "c1",
            "origin": "live",
            "cause_id": "Q1",
            "effect_id": "Q2",
            "relation": "has_cause",
            "source_key": "qa.causal",
            "raw_sha256": raw_kg,
            "qualifiers": "{}",
        },
        {
            "id": 11,
            "claim_key": "ds:mondo.obo:mondo:MONDO:1|RO:0004021|GO:1",
            "origin": "live",
            "cause_id": "go:1",
            "effect_id": "Q2",
            "relation": "has_cause",
            "source_key": "qa.causal",
            "raw_sha256": raw_kg,
            "qualifiers": "{}",
        },
    ]
    grades = [{"claim_row_id": 10, "claim_key": "c1", "origin": "live"}]
    effects = [{"source_key": "qa.effects", "raw_sha256": raw_kg, "origin": "live"}]
    frames = {
        "evidence.dataset": pl.DataFrame(ds_rows),
        "meta.artifact": pl.DataFrame(art_rows),
        "meta.artifact_location": pl.DataFrame(loc_rows),
        "meta.artifact_inline": pl.DataFrame({"artifact_sha256": []}, schema={"artifact_sha256": pl.String}),
        "kg.entity": pl.DataFrame(entities),
        "kg.statement_current": pl.DataFrame(
            statements, schema_overrides={"valid_lo": pl.Date, "value_time_lo": pl.Date, "raw_sha256": pl.String}
        ),
        "kg.causal_claim_current": pl.DataFrame(claims),
        "kg.causal_grade_current": pl.DataFrame(grades),
        "kg.effect_estimate_current": pl.DataFrame(effects),
    }
    be = fixture_backend(frames, st, today=TODAY)
    try:
        rep = run_qa(be, cfg=load_config())
    finally:
        be.close()
    md = rep.to_markdown()
    assert "# REVA data-quality report" in md
    out = rep.as_dict()
    out["_md"] = md
    return out


def _check(rep: dict[str, Any], cid: str) -> dict[str, Any]:
    by = {c["id"]: c for c in rep["checks"]}
    assert cid in by, sorted(by)
    out: dict[str, Any] = by[cid]
    return out


def test_no_check_errors(report: dict[str, Any]) -> None:
    assert [(c["id"], c.get("note")) for c in report["checks"] if c["status"] == "error"] == []
    assert report["mode"] == "fixture"


def test_cross_source_population(report: dict[str, Any]) -> None:
    c = _check(report, "cross_source.headline")
    row = next(r for r in c["breakdown"] if r["concept"] == "reva.population.total")
    assert row["overlap"] == 5 and row["diverging"] == 1  # 2022: 91.5 M (WPP x1000) vs 83.3 M
    assert c["examples"][0]["iso3"] == "DEU" and c["examples"][0]["year"] == 2022


def test_units(report: dict[str, Any]) -> None:
    assert _check(report, "units.bounded_share")["count"] == 1
    assert _check(report, "units.negative_count")["count"] == 1


def test_time(report: dict[str, Any]) -> None:
    # FIX5: QA.SHARE 2030 and the unflagged 2051 cell of the projection table start after their table's record
    # date: read as not observed by the shared date rule (registry/atlas/projection_kinds.yaml), reported apart
    assert _check(report, "time.future_observation")["count"] == 0
    assert _check(report, "time.projection_by_date_rule")["count"] == 2
    assert _check(report, "time.projection_table_unflagged")["count"] == 1  # provider flags only
    assert _check(report, "time.forecast_as_observation")["count"] == 1
    # estimate_kind "forecast" is a projection kind now (QA-DATA D1): no longer an observation
    assert _check(report, "time.projection_word_unflagged")["count"] == 0
    assert _check(report, "time.estimate_kind_unclassified")["count"] == 0
    kg = _check(report, "time.kg_future_statement")
    assert kg["count"] == 1 and kg["breakdown"][0]["predicate"] == "P571"


def test_graph(report: dict[str, Any]) -> None:
    assert _check(report, "graph.cycles.is_a")["count"] == 1
    assert _check(report, "graph.cycles.part_of")["count"] == 0
    d = _check(report, "graph.dangling_object")
    assert d["count"] == 1 and d["breakdown"][0]["predicate"] == "P31"
    li = _check(report, "graph.located_in_no_country")
    assert li["count"] == 1 and li["examples"][0]["subject"] == "Q5"


def test_licence_and_provenance(report: dict[str, Any]) -> None:
    lic = _check(report, "licence.atlas_tables")
    assert lic["count"] == 1 and lic["examples"][0]["table"] == "qafix.vintage"
    assert _check(report, "licence.kg_raw")["count"] == 1
    assert _check(report, "provenance.kg_no_raw")["count"] == 1
    objs = _check(report, "provenance.objects")
    assert objs["count"] == 2 and {e["state"] for e in objs["examples"]} == {"missing", "corrupt"}
    cell = _check(report, "provenance.atlas_cell_raw")
    assert cell["count"] == 1  # the QA.COUNT 2021 cell cites a raw object its table does not list


def test_causal(report: dict[str, Any]) -> None:
    assert _check(report, "causal.ungraded")["count"] == 1
    assert _check(report, "causal.missing_endpoint")["count"] == 1  # go:1 is not an entity
    assert _check(report, "causal.disruption_axioms_unqualified")["count"] == 1
    leak = _check(report, "causal.wording_leak_predicates")
    assert any(r["predicate"] == "P828" and r["family"] is None for r in leak["breakdown"])


def test_lit_rejects_injection() -> None:
    assert lit("wb.wdi_social") == "'wb.wdi_social'"
    with pytest.raises(Exception):
        lit("x'; DROP TABLE kg.statement; --")
