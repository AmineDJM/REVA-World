"""V4MIND atlas sources end to end on the throwaway test database (fixture replay, no network): atlas tables
registered, KG survey waves / ISCO-08 groups / OpenAlex taxonomy loaded with provenance, re-runs idempotent."""

from __future__ import annotations

from typing import Any

import polars as pl
import pytest
from sqlalchemy import text

from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.atlas.tables import read_atlas_table

pytestmark = pytest.mark.db


def _reg(db: Any, name: str) -> None:
    ensure_registered(db.owner, db.store, get_source(name).spec)


def _count(db: Any, sql: str, **kw: Any) -> int:
    with db.owner.connect() as c:
        return int(c.execute(text(sql), kw).scalar_one())


async def _run(db: Any, name: str) -> dict[str, Any]:
    _reg(db, name)
    out = await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="fixture")
    assert out["status"] == "completed", out
    return out


async def test_oecd_wellbeing_tables(db: Any) -> None:
    out = await _run(db, "atlas.oecd.wellbeing")
    s = out["summary"]
    assert s["ok"], s["errors"]
    assert {t["table"] for t in s["tables"]} == {"oecd.hsl_mind_current", "oecd.hsl_mind_social_capital"}
    t = next(t for t in s["tables"] if t["table"] == "oecd.hsl_mind_current")
    frame = read_atlas_table(db.store, t["artifact_sha256"])
    jpn = frame.filter((pl.col("iso3") == "JPN") & (pl.col("concept_id") == "mind.oecd.life_satisfaction"))
    assert jpn.height >= 1 and jpn["value"].null_count() == 0
    again = await run_atlas_ingest(db.settings, db.worker, db.store, "atlas.oecd.wellbeing", mode="fixture")
    assert all(t["created"] is False for t in again["summary"]["tables"])


@pytest.mark.parametrize("name", ["atlas.who_mental", "atlas.hofstede"])
async def test_who_and_hofstede_tables(db: Any, name: str) -> None:
    out = await _run(db, name)
    reg = out["summary"]["atlas"]
    assert reg["created"] is True
    frame = read_atlas_table(db.store, reg["artifact_sha256"])
    if name == "atlas.hofstede":
        unmapped = {u["provider_geo"] for u in reg.get("unmapped_provider_geo") or []}
        assert {"ARA", "BEN", "CAF", "SAW"} <= unmapped and "CHI" not in unmapped
        assert frame.filter(pl.col("iso3") == "CHN").height == 6
        assert frame.filter(pl.col("provider_geo") == "CHI")["iso3"].unique().to_list() == ["CHN"]
    else:
        assert frame.filter(pl.col("concept_id") == "mind.who.psychiatrists").height >= 3


async def test_time_use_table_and_survey_waves(db: Any) -> None:
    out = await _run(db, "atlas.un_sdg_timeuse")
    kg = out["summary"]["kg"]
    assert kg["rejected"] == {} and kg["entities_inserted"] == kg["entities"] and kg["statements_inserted"] > 0
    n = _count(
        db,
        "SELECT count(*) FROM kg.statement_current WHERE property_pid = 'unsd_tus:survey_in' AND object_qid = 'Q96' "
        "AND origin = 'test_fixture'",
    )
    assert n >= 3  # Mexican time-use survey rounds located in Mexico
    again = await run_atlas_ingest(db.settings, db.worker, db.store, "atlas.un_sdg_timeuse", mode="fixture")
    assert again["summary"]["kg"]["statements_inserted"] == 0


async def test_occupations_tables_and_isco_groups(db: Any) -> None:
    out = await _run(db, "atlas.ilostat_occupation")
    s = out["summary"]
    assert {t["table"] for t in s["tables"]} == {
        "ilostat.emp_2emp_sex_ocu_nb_a", "ilostat.emp_temp_sex_ocu_nb_a", "ilostat.how_temp_sex_ocu_nb_a",
    }  # fmt: skip
    assert s["kg"]["rejected"] == {} and s["kg"]["groups"] >= 50
    assert _count(db, "SELECT count(*) FROM kg.entity_current WHERE kind = 'occupation' AND qid LIKE 'isco08:%'") >= 50
    assert (
        _count(
            db,
            "SELECT count(*) FROM kg.statement_current WHERE subject_qid = 'isco08:26' AND property_pid = 'isco:part_of' "
            "AND object_qid = 'isco08:2'",
        )
        == 1
    )
    assert (
        _count(
            db,
            "SELECT count(*) FROM kg.statement_current WHERE subject_qid = 'isco08:2' "
            "AND property_pid = 'ilostat:measured_by' AND object_qid = 'atlas:ilostat.emp_2emp_sex_ocu_nb_a'",
        )
        == 1
    )


async def test_openalex_taxonomy_kg(db: Any) -> None:
    out = await _run(db, "atlas.openalex_taxonomy")
    kg = out["summary"]["kg"]
    assert kg["rejected"] == {} and kg["entities_inserted"] > 20
    assert (
        _count(
            db,
            "SELECT count(*) FROM kg.statement_current WHERE subject_qid = 'openalex:subfield.3207' "
            "AND property_pid = 'openalex:part_of' AND object_qid = 'openalex:field.32'",
        )
        == 1
    )
