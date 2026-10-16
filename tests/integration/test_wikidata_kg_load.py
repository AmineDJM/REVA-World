"""WDHARVEST -> KGCORE: harvested Wikidata batches load through ``reva.kg.model.load_batch`` (offline)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from reva.adapters.connectors import wikidata as wd
from reva.kg import harvest as hv


def _helpers() -> Any:
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "unit" / "test_wikidata_harvest.py"
    spec = importlib.util.spec_from_file_location("_wdharvest_helpers", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_h = _helpers()
BASE, ScriptedHttp, _router, _spec, client_for = _h.BASE, _h.ScriptedHttp, _h._router, _h._spec, _h.client_for

pytestmark = pytest.mark.db


async def test_seed_spec_loads_into_kg_tables(db: Any, tmp_path: Path) -> None:
    seen: list[list[str]] = []
    http = ScriptedHttp(router=_router(2, seen))
    client = client_for(http, tmp_path)
    client.archive = wd.Archive(db.store, None)  # raw bytes in the store the loader registers from
    sink = hv.KgDbSink(db.worker, db.store, origin="test_fixture")
    allow = wd.load_allowlist()
    props = sink.register_properties(allow)
    assert props["properties_inserted"] + props["unchanged"] == len(allow.properties)
    h = hv.Harvester(client, sink, allow=allow, state_dir=tmp_path / "state")
    rep = await h.run_spec(_spec(tmp_path, {**BASE, "entity_kind": "empire", "expand_hops": 1}))
    assert rep.status == "done", rep.failures
    assert sink.totals.get("statements_inserted", 0) > 0, sink.totals
    with db.owner.connect() as c:
        kinds = dict(c.execute(text("SELECT qid, kind FROM kg.entity_current WHERE origin='test_fixture'")).all())
        assert kinds["Q17167"] == "empire" and kinds["Q220"] == hv.LABEL_KIND
        row = c.execute(
            text("""SELECT value, rank, refs, source_key, source_revision, raw_sha256
                                 FROM kg.statement_current WHERE statement_id = 'Q17167$C'""")
        ).one()
        assert row.value["time"].startswith("-0043-03-15") and row.value["calendar"] == "julian"
        assert row.rank == "preferred" and row.source_key == wd.SOURCE_WBAPI and row.source_revision
        assert row.refs == [{"P248": ["Q123"], "P854": ["https://example.org/x"]}]
        # deprecated statements never enter
        assert c.execute(text("SELECT count(*) FROM kg.statement WHERE statement_id = 'Q17167$B'")).scalar() == 0
        loads = c.execute(text("SELECT source_key, raw_sha256s FROM kg.load")).all()
        assert all(json.dumps(r.raw_sha256s) for r in loads)
    # re-running the same batch inserts nothing new (idempotent loader + resumable state)
    before = dict(sink.totals)
    rep2 = await h.run_spec(_spec(tmp_path, {**BASE, "entity_kind": "empire", "expand_hops": 1}), force=True)
    assert rep2.status == "done"
    assert sink.totals["statements_inserted"] == before["statements_inserted"]
    # objects already known are not fetched again as label snapshots
    assert sink.known_entities(["Q220", "Q999999"]) == {"Q220"}
