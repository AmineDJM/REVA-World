"""CAUSMORE sources end to end on a throwaway database through the atlas platform (fixture mode: no network).

Raw slices and codebooks are archived as evidence acquisitions, KG records load without rejection, claims are
delivered through CAUSCORE's ``load_dataset_claims`` when the causal schema exists (else staged as JSON), the PA-X
UCDP agreement ids give evidenced ``same_as`` candidates, and re-running is a no-op.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_cowwars, atlas_idmc, atlas_pax, atlas_ucdp_dyadterm
from reva.adapters.connectors import causdata_common as cd
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered

pytestmark = pytest.mark.db


async def _run(db: Any, module: Any, staging: Path) -> dict[str, Any]:
    spec = module.atlas_source()
    ensure_registered(db.owner, db.store, spec)
    out = await run_atlas_ingest(
        db.settings, db.worker, db.store, spec.name, mode="fixture", options={"claims_staging": str(staging)}
    )
    assert out["status"] == "completed", out
    return out["summary"]


def _claims_ok(summary: dict[str, Any], datasets: tuple[str, ...]) -> None:
    for ds in datasets:
        rep = summary["claims"][ds]
        assert rep["claims"] > 0, (ds, rep)
        if rep["status"] == "loaded":
            assert not rep["report"].get("rejected_counts"), rep["report"]
        else:
            assert rep["status"] == "staged", rep
            doc = json.loads(Path(rep["file"]).read_text())
            assert doc["origin"] == "test_fixture" and len(doc["claims"]) == rep["claims"]


def _count(db: Any, sql: str, **kw: Any) -> int:
    with db.owner.connect() as c:
        return int(c.execute(text(sql), kw).scalar_one())


async def test_cowwars_ingest(db: Any, tmp_path: Path) -> None:
    s = await _run(db, atlas_cowwars, tmp_path)
    assert all(v["matches"] for v in s["codebooks"].values())
    assert not s["kg"]["rejected"], s["kg"]
    assert s["kg"]["statements_inserted"] == s["kg_counts"]["statements"]
    _claims_ok(s, ("cow_inter", "cow_extra", "cow_intra", "cow_nonstate"))
    ev = s["v51_numbering_evidence"]  # audit2 N8: v5.1 archived, its renumbering checkable offline
    assert ev["status"] == "archived" and ev["check"]["example_856"]["v5.1"] == "Romanian War of 1989"
    assert _count(db, "SELECT count(*) FROM evidence.acquisition WHERE raw_sha256 = :s", s=ev["sha256"]) >= 1
    n = _count(
        db,
        "SELECT count(*) FROM kg.statement_current WHERE subject_qid = 'cow:war:327' "
        "AND property_pid = 'cowwar:transformed_into' AND object_qid = 'cow:war:19'",
    )
    assert n == 2
    again = await _run(db, atlas_cowwars, tmp_path)
    assert again["kg"]["statements_inserted"] == 0


async def test_pax_ingest(db: Any, tmp_path: Path) -> None:
    s = await _run(db, atlas_pax, tmp_path)
    assert s["codebook"]["matches"] and not s["kg"]["rejected"], s
    # 1.1.0: no claims, an empty complete delivery (retracts 1.0.0's reaction_to claims)
    rep = s["claims"]["pax"]
    assert rep["claims"] == 0 and rep["status"] in ("loaded", "staged"), rep
    if cd.causal_available(db.worker):
        n = _count(
            db,
            "SELECT count(*) FROM kg.causal_claim_current WHERE effect_id LIKE 'pax:agt:%'",
        )
        assert n == 0
    assert _count(db, "SELECT count(*) FROM kg.statement_current WHERE property_pid = 'pax:ucdp_agreement_id'") >= 3
    again = await _run(db, atlas_pax, tmp_path)
    assert again["kg"]["statements_inserted"] == 0


async def test_idmc_ingest(db: Any, tmp_path: Path) -> None:
    s = await _run(db, atlas_idmc, tmp_path)
    assert s["codebook"]["matches"] and not s["kg"]["rejected"], s
    assert s["hdx"]["organisation"] == "international-displacement-monitoring-centre-idmc"
    _claims_ok(s, ("idmc_disasters",))
    n = _count(
        db,
        "SELECT count(*) FROM kg.statement_current WHERE subject_qid LIKE 'idmc:displacement:%' "
        "AND property_pid = 'idmc:internal_displacements'",
    )
    # every displacement record has its figure; anticipatory ones (audit2 N4) are context, not claims
    assert n == s["claims"]["idmc_disasters"]["claims"] + s["anticipatory_rows_no_claim"]
    assert s["anticipatory_rows_no_claim"] >= 4
    ctx = _count(
        db, "SELECT count(*) FROM kg.statement_current WHERE property_pid = 'idmc:displacement_in_anticipation_of'"
    )
    assert ctx == s["anticipatory_rows_no_claim"]
    assert (
        _count(db, "SELECT count(*) FROM kg.statement_current WHERE property_pid = 'idmc:source_duplicate_rows'") == 2
    )
    if cd.causal_available(db.worker):  # quote_source: README cell of the archived workbook (audit2 N8)
        with db.owner.connect() as c:
            src = c.execute(
                text(
                    "SELECT codebook->'quote_source' FROM kg.causal_claim_current "
                    "WHERE dataset = 'idmc_disasters' AND origin = 'test_fixture' LIMIT 1"
                )
            ).scalar_one()
            arch = c.execute(
                text("SELECT count(*) FROM evidence.acquisition WHERE raw_sha256 = :s"), {"s": src["sha256"]}
            ).scalar_one()
        assert src["cell"] == "A24" and arch >= 1
    again = await _run(db, atlas_idmc, tmp_path)
    assert again["kg"]["statements_inserted"] == 0


async def test_ucdp_dyadterm_ingest(db: Any, tmp_path: Path) -> None:
    s = await _run(db, atlas_ucdp_dyadterm, tmp_path)
    assert s["codebook"]["matches"] and not s["kg"]["rejected"], s
    _claims_ok(s, ("term_dyad",))
    if cd.causal_available(db.worker):  # both terminal rows of 64901 kept, contesting each other (audit2 N6)
        n = _count(
            db,
            "SELECT count(*) FROM kg.causal_claim_current WHERE cause_id = 'ucdp:dyadepisode:64901' "
            "AND cardinality(contests) = 1 AND codebook ? 'quote_source'",
        )
        assert n == 2
    # a 1.0.0-style single-terminal statement of a multi-terminal episode is retracted by the next run (audit2 N6)
    from datetime import UTC, datetime

    from reva.adapters.connectors import causmore_common as cm
    from reva.adapters.connectors.atlas_geopol_common import KgStatement

    with db.owner.connect() as c:
        raw = c.execute(
            text("SELECT raw_sha256 FROM kg.statement_current WHERE statement_key = 'ucdp.downloads:de64901:t:start'")
        ).scalar_one()
    old = KgStatement(
        key="de64901:t:end",
        subject="ucdp:dyadepisode:64901",
        property="P582",
        value=cd.time_value("1973-10-24", 11),
        qualifiers={},
        raw_sha256=raw,
        selector={"file": atlas_ucdp_dyadterm.DATA_FILE, "line": 1},
    )
    cm.load_kg(
        db.worker, db.store, [], [old], source_key="ucdp.downloads", source_revision="ucdp-term-dyad-v4-2024",
        raw_sha256s=[raw], retrieved_at=datetime(2026, 10, 17, 1, 0, tzinfo=UTC), origin="test_fixture",
    )  # fmt: skip
    key = "SELECT count(*) FROM kg.statement_current WHERE statement_key = 'ucdp.downloads:de64901:t:end'"
    assert _count(db, key) == 1
    again = await _run(db, atlas_ucdp_dyadterm, tmp_path)
    assert again["kg"]["statements_inserted"] == 0
    assert again["kg"]["superseded_single_terminal"]["retracted"] == 1 and _count(db, key) == 0
    assert _count(db, "SELECT count(*) FROM kg.statement_current WHERE statement_key LIKE 'ucdp.downloads:de64901:t1%'")
