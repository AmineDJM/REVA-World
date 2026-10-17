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
    assert n == s["claims"]["idmc_disasters"]["claims"]
    again = await _run(db, atlas_idmc, tmp_path)
    assert again["kg"]["statements_inserted"] == 0


async def test_ucdp_dyadterm_ingest(db: Any, tmp_path: Path) -> None:
    s = await _run(db, atlas_ucdp_dyadterm, tmp_path)
    assert s["codebook"]["matches"] and not s["kg"]["rejected"], s
    _claims_ok(s, ("term_dyad",))
    again = await _run(db, atlas_ucdp_dyadterm, tmp_path)
    assert again["kg"]["statements_inserted"] == 0
