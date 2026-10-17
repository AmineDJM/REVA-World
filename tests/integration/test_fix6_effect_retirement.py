"""FIX6 (AUDIT4 L5): a curator's retirement record (reason, replacing estimates) reaches the effect retraction,
and existing retractions get an appended annotation; throwaway database, synthetic estimates only."""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest
from sqlalchemy import text
from test_effects_store import _est, _load, _raw  # type: ignore[import-not-found]

from reva.effects.retired import ACTIVITY_KIND, annotate_existing_retractions, pending_annotations

pytestmark = pytest.mark.db

PRE = "pub:effects.literature:synthetic_paper:"


class _Entry:
    def __init__(self, recs: dict[str, dict[str, object]]) -> None:
        self.recs = recs

    def retired_by_estimate_key(self) -> dict[str, dict[str, object]]:
        return self.recs


def test_retired_record_in_retraction_detail_and_annotation(db) -> None:  # type: ignore[no-untyped-def]
    ds = "eztest" + uuid.uuid4().hex[:6]
    raw = _raw(db, ds)
    _load(db, ds, raw, [_est(raw, i) for i in range(4)])
    rec = {"reason": "horizon convention (synthetic)", "replaced_by": [PRE + "e0"]}
    # e3 retired with a record (loader copies it); e2 retired without one (an "old" retraction)
    r = _load(db, ds, raw, [_est(raw, 0), _est(raw, 1), _est(raw, 2)], retired={PRE + "e3": rec})
    assert r.retracted == 1
    r = _load(db, ds, raw, [_est(raw, 0), _est(raw, 1)])
    assert r.retracted == 1
    with db.worker.connect() as c:
        det = dict(
            c.execute(
                text("SELECT estimate_key, detail FROM kg.effect_estimate_retraction WHERE estimate_key LIKE :k"),
                {"k": PRE + "%"},
            ).all()
        )
    assert det[PRE + "e3"]["retired"] == rec and "retired" not in det[PRE + "e2"]
    entry = _Entry({PRE + "e2": {"reason": "synthetic later reason", "replaced_by": [PRE + "e1"]}, PRE + "e3": rec})
    with db.worker.connect() as c:
        todo = pending_annotations(c, [entry], origin="test_fixture")  # type: ignore[list-item]
    assert [t["estimate_key"] for t in todo] == [PRE + "e2"]  # e3 already carries its record
    out = annotate_existing_retractions(db.worker, db.store, [entry], origin="test_fixture")  # type: ignore[list-item]
    assert out["annotated"] == 1
    again = annotate_existing_retractions(db.worker, db.store, [entry], origin="test_fixture")  # type: ignore[list-item]
    assert again == {"annotated": 0}  # append-only, idempotent
    with db.worker.connect() as c:
        n = c.execute(text("SELECT count(*) FROM meta.activity WHERE kind = :k"), {"k": ACTIVITY_KIND}).scalar_one()
    assert n == 1


def test_curation_retired_validation() -> None:
    from pathlib import Path

    from reva.effects.curation import parse_entry
    from reva.kernel.errors import ValidationProblem

    p = Path(__file__).resolve().parents[2] / "registry/effects/literature/gutmann_neuenkirch_neumeier_2022.yaml"
    e = parse_entry(p.read_bytes(), p)
    recs = e.retired_by_estimate_key()
    pre = "pub:effects.literature:gutmann_neuenkirch_neumeier_2022:"
    assert set(recs) == {pre + "gdp_pc_growth_year1", pre + "gdp_pc_growth_year2"}
    assert all(r["replaced_by"] == [pre + "gdp_pc_growth_years_1_2"] for r in recs.values())
    bad = p.read_bytes().replace(b"replaced_by: [gdp_pc_growth_years_1_2]", b"replaced_by: [nope]", 1)
    with pytest.raises(ValidationProblem):
        parse_entry(bad, p)
    assert replace(e, retired=[]).retired_by_estimate_key() == {}
