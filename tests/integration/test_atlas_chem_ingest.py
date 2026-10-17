"""V4CHEM connectors end to end on a throwaway database, through ``reva.atlas.ingest`` (fixtures, no network).

Answers "what is methane / hematite made of" with sourced statements: chebi:16183 -is_a-> alkane -is_a->
hydrocarbon; chebi:16183 -formula_element-> element:6 (1 atom) and element:1 (4 atoms); element:6 is the PubChem
periodic-table element whose nuclides (nuclide:6-14, AMDC half-life) hang off it."""

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


def _rows(db: Any, sql: str, **kw: Any) -> list[Any]:
    with db.owner.connect() as c:
        return list(c.execute(text(sql), kw))


async def test_chem_layer_end_to_end(db):
    for name in ("atlas.chem_elements", "atlas.chem_codata", "atlas.chem_chebi", "atlas.chem_pubchem"):
        _reg(db, name)

    el = await run_atlas_ingest(db.settings, db.worker, db.store, "atlas.chem_elements", mode="fixture")
    assert el["status"] == "completed", el
    rep = el["summary"]
    assert rep["pages_parsed"] == 5 and rep["missing_pages"] == []
    assert rep["kg"]["status"] == "loaded" and rep["kg"]["rejected_counts"] == {}, rep["kg"]
    frame = read_atlas_table(db.store, rep["atlas_elements"]["artifact_sha256"])
    fe = frame.filter(pl.col("indicator") == "crustal_abundance|Z=26|provider=jlab")
    assert fe["value"].to_list() == ["56300"] and fe["provider_geo"].to_list() == ["earth_crust"]
    assert frame["iso3"].null_count() == frame.height

    cd = await run_atlas_ingest(db.settings, db.worker, db.store, "atlas.chem_codata", mode="fixture")
    assert cd["summary"]["constants"] == 355 and cd["summary"]["kg"]["rejected_counts"] == {}

    ch = await run_atlas_ingest(
        db.settings,
        db.worker,
        db.store,
        "atlas.chem_chebi",
        mode="fixture",
        options={"chebi_ids": ["16183", "50818", "16526", "76413", "46719", "15377"]},
    )
    assert ch["status"] == "completed", ch
    assert ch["summary"]["kg"]["status"] == "loaded" and ch["summary"]["kg"]["rejected_counts"] == {}, ch["summary"]

    pc = await run_atlas_ingest(
        db.settings,
        db.worker,
        db.store,
        "atlas.chem_pubchem",
        mode="fixture",
        options={"chebi_ids": ["15377", "16183", "16526", "50819", "99999999"]},
    )
    prep = pc["summary"]
    assert prep["not_in_pubchem"] == ["CHEBI:99999999"]  # refused id isolated by list splitting, reported
    assert prep["compounds"] == 4 and prep["kg"]["rejected_counts"] == {}, prep

    # methane: made of carbon (1) and hydrogen (4), is an alkane, alkane is a hydrocarbon
    made = _rows(
        db,
        """SELECT object_qid, qualifiers->>'atom_count' FROM kg.statement_current
           WHERE subject_qid = 'chebi:16183' AND property_pid = 'chebi:formula_element' AND origin = 'test_fixture'""",
    )
    assert sorted(made) == [("element:1", "4"), ("element:6", "1")]
    chain = _rows(
        db,
        """SELECT a.object_qid, b.object_qid FROM kg.statement_current a
           JOIN kg.statement_current b ON b.subject_qid = a.object_qid AND b.property_pid = 'chebi:is_a'
           WHERE a.subject_qid = 'chebi:16183' AND a.property_pid = 'chebi:is_a' AND a.object_qid = 'chebi:18310'""",
    )
    assert ("chebi:18310", "chebi:24632") in chain
    # PubChem's CID for the ChEBI deposit, and its composition from the PubChem formula
    dep = _rows(
        db,
        "SELECT subject_qid FROM kg.statement_current WHERE property_pid = 'pubchem:chebi_deposit' AND object_qid = 'chebi:16183'",
    )
    assert dep == [("pubchem:cid:297",)]
    # nuclides of carbon from the AMDC table on the PubChem carbon page
    c14 = _rows(
        db,
        """SELECT s.value->>'value' FROM kg.statement_current s
           WHERE s.subject_qid = 'nuclide:6-14' AND s.property_pid = 'pubchem:half_life'""",
    )
    assert c14 == [("5.70 ky ± 0.03",)]  # verbatim AMDC text
    years = _rows(
        db,
        "SELECT value->>'amount' FROM kg.statement_current WHERE subject_qid = 'nuclide:6-14' AND property_pid = 'pubchem:half_life_years'",
    )
    assert years == [("5700",)]  # unit scale computed by REVA (ky -> y), flagged computed_by
    kinds = dict(
        _rows(
            db,
            """SELECT qid, kind FROM kg.entity_current WHERE qid IN
               ('element:26', 'nuclide:92-238', 'chebi:50818', 'chebi:76413', 'pubchem:cid:280', 'codata:avogadro_constant')""",
        )
    )
    assert kinds == {
        "element:26": "chemical_element",
        "nuclide:92-238": "isotope",
        "chebi:50818": "mineral",
        "chebi:76413": "chemical_role",
        "pubchem:cid:280": "compound",
        "codata:avogadro_constant": "physical_constant",
    }

    again = await run_atlas_ingest(db.settings, db.worker, db.store, "atlas.chem_elements", mode="fixture")
    assert again["summary"]["kg"]["statements_inserted"] == 0
    assert again["summary"]["atlas_elements"]["created"] is False
