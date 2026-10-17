"""Event-study engine against a throwaway database: KG treatment selection (prefix, statement filters,
identifier-based country mapping, as-of), then a full run whose run document is archived and whose REVA-run effect
estimate is written through PRED-EFFECT's loader (idempotent reload).

Synthetic KG events (``estsyn:`` ids, source ``estimationtest``) and a synthetic atlas query file; nothing here
is a real-world fact."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pytest

from reva.estimation import run as runmod
from reva.estimation.data import SnapshotPanels, Treatment, kg_onsets
from reva.kg.model import KgEntity, KgProperty, KgStatement, load_batch, register_properties
from reva.kg.query import country_qid_registry

pytestmark = pytest.mark.db
SRC = "estimationtest"
SNAP = "ef" * 32


def _t(iso: str, prec: int) -> dict[str, Any]:
    return {"type": "time", "time": iso, "precision": prec, "calendar": "gregorian"}


def _q(v: float) -> dict[str, Any]:
    return {"type": "quantity", "amount": str(v), "unit": "1"}


TREATMENT = Treatment(
    id="estsyn_crisis",
    event_type="banking_crisis",
    source="kg",
    definition="synthetic crisis (test)",
    citation="none (synthetic)",
    coverage_years=(1965, 2015),
    spec={
        "id_prefix": "estsyn:crisis:",
        "onset": {"property": "P580"},
        "country": {"property": "P17", "via": "qid"},
        "filters": [{"property": "estsyn:severity", "min": 5}],
    },
)


@pytest.fixture(scope="module")
def kg(db) -> Any:  # type: ignore[no-untyped-def]
    register_properties(
        db.worker,
        [
            KgProperty("P17", "item", "country", "country"),
            KgProperty("P580", "time", "start_time", "start time"),
            KgProperty("estsyn:severity", "quantity", "severity", "severity (test)"),
        ],
    )
    reg = country_qid_registry()
    isos = sorted(reg)[:40]
    rng = np.random.default_rng(3)
    ents: list[KgEntity] = []
    sts: list[KgStatement] = []
    onsets: dict[tuple[str, int], int] = {}
    n = 0
    for iso in isos:
        for y in sorted({int(x) for x in rng.choice(range(1970, 2005), size=2, replace=False)}):
            n += 1
            eid = f"estsyn:crisis:{n}"
            ents.append(KgEntity(eid, "event", {"en": f"synthetic crisis {n}"}))
            sts += [
                KgStatement(eid, "P17", reg[iso], statement_id=f"c{n}"),
                KgStatement(eid, "P580", value=_t(f"{y}-03-01", 10), statement_id=f"s{n}"),
                KgStatement(eid, "estsyn:severity", value=_q(7), statement_id=f"v{n}"),
            ]
            onsets[(iso, y)] = 1
    # filtered out (severity below threshold), unmapped country, no onset
    ents += [KgEntity(f"estsyn:crisis:{k}", "event", {"en": k}) for k in ("weak", "nowhere", "undated")]
    sts += [
        KgStatement("estsyn:crisis:weak", "P17", reg[isos[0]], statement_id="w1"),
        KgStatement("estsyn:crisis:weak", "P580", value=_t("1990-01-01", 10), statement_id="w2"),
        KgStatement("estsyn:crisis:weak", "estsyn:severity", value=_q(2), statement_id="w3"),
        KgStatement("estsyn:crisis:nowhere", "P17", "Q4115189", statement_id="n1"),
        KgStatement("estsyn:crisis:nowhere", "P580", value=_t("1990-01-01", 10), statement_id="n2"),
        KgStatement("estsyn:crisis:nowhere", "estsyn:severity", value=_q(9), statement_id="n3"),
        KgStatement("estsyn:crisis:undated", "P17", reg[isos[1]], statement_id="u1"),
        KgStatement("estsyn:crisis:undated", "estsyn:severity", value=_q(9), statement_id="u3"),
    ]
    ents.append(KgEntity("Q4115189", "test_place", {"en": "not a country"}))
    raw = db.store.put_bytes(b"estimation synthetic events v1", "raw", "application/json").sha256
    rep = load_batch(
        db.worker,
        db.store,
        entities=ents,
        statements=sts,
        source_key=SRC,
        raw_sha256s=[raw],
        retrieved_at=datetime.now(UTC),
        origin="test_fixture",
    )
    assert rep.rejected_total == 0, rep.rejected
    return db, isos, onsets


def test_kg_onsets_selection_and_as_of(kg) -> None:  # type: ignore[no-untyped-def]
    db, _isos, onsets = kg
    with db.api.connect() as c:
        ev = kg_onsets(c, TREATMENT, origin="test_fixture")
        early = kg_onsets(c, TREATMENT, origin="test_fixture", as_of=datetime(2000, 1, 1, tzinfo=UTC))
    assert {(o.iso3, o.year) for o in ev.onsets} == set(onsets)
    assert ev.skipped["unmapped_country_qid"] == 1 and ev.skipped["no_onset"] == 1
    assert all(o.date.endswith("-03-01") and o.precision == 10 for o in ev.onsets)
    assert ev.provenance["source_keys"] == [SRC]
    assert early.onsets == []  # nothing was recorded by 2000-01-01


def _atlas_file(directory: Path, isos: list[str], onsets: dict[tuple[str, int], int]) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"atlas-{SNAP}-v6.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE meta AS SELECT 'atlas-duckdb/6' AS format, ? AS snapshot_sha256, 'test_fixture' AS origin,"
        " '2026-10-17T00:00:00+00:00' AS cutoff",
        [SNAP],
    )
    con.execute(
        "CREATE TABLE series AS SELECT 1 AS sid, 'wb' AS source, 'wdi_econ' AS dataset, "
        "'NY.GDP.MKTP.KD.ZG' AS indicator"
    )
    con.execute("""CREATE TABLE cells_s(sid INTEGER, iso3 VARCHAR, year INTEGER, has_value BOOLEAN, value VARCHAR,
                   value_text VARCHAR, unit VARCHAR, raw_sha256 VARCHAR, table_sha VARCHAR, period_start DATE,
                   period_end DATE)""")
    rng = np.random.default_rng(9)
    rows = []
    for iso in isos:
        base = rng.normal(3, 1)
        for y in range(1960, 2016):
            g = base + rng.normal(0, 1) - 2.5 * onsets.get((iso, y), 0)
            rows.append((1, iso, y, True, f"{g:.6f}", None, "%", "a" * 64, "b" * 64, date(y, 1, 1), date(y + 1, 1, 1)))
    con.executemany("INSERT INTO cells_s VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.close()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_name(path.name + ".sha256").write_text(
        json.dumps({"format": "atlas-duckdb/6", "snapshot_sha256": SNAP, "sha256": digest})
    )


def test_run_archive_and_effect_estimate(kg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    db, isos, onsets = kg
    pytest.importorskip("reva.effects")
    _atlas_file(tmp_path / "cache", isos, onsets)
    monkeypatch.setattr(runmod, "load_treatments", lambda: {TREATMENT.id: TREATMENT})
    req = runmod.EventStudyRequest(
        treatment=TREATMENT.id, outcome="concept:wb.ny.gdp.mktp.kd.zg", horizons=2, n_perm=10, origin="test_fixture"
    )
    with SnapshotPanels(SNAP, tmp_path / "cache") as panels, db.api.connect() as c:
        doc = runmod.run_event_study(req, panels, conn=c, replication_report={"gate_open": True, "passed": ["t"]})
    h0 = doc["results"]["local_projection"]["irf"][0]
    assert h0["point"] == pytest.approx(-2.5, abs=0.8)
    sha = runmod.archive_run(db.worker, db.store, doc)
    assert len(sha) == 64
    rep = runmod.write_effect_estimates(db.worker, db.store, doc, sha, origin="test_fixture")
    assert rep["inserted"] == 1, json.dumps(rep, default=str)
    again = runmod.write_effect_estimates(db.worker, db.store, doc, sha, origin="test_fixture")
    assert again["inserted"] == 0
    from reva.effects import get_estimate

    with db.api.connect() as c:
        est = get_estimate(c, f"run:{doc['run_id']}:h2")
    assert est is not None
    assert est["method"] == "local_projection" and est["source"]["kind"] == "reva_run"
    assert est["source"]["run"]["run_id"] == doc["run_id"]
    _ = timedelta
