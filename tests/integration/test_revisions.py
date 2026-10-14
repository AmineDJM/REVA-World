"""Temporal/revision acceptance tests through the real connector + ingest path.

Vintage payloads live in tests/fixtures/vintages (synthetic values in the exact
World Bank v2 response shape, years outside the captured live fixture).
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import exc as sa_exc
from sqlalchemy import text

from reva.adapters.connectors.registry import builtin_connectors
from reva.belief.publication import publish_release
from reva.belief.selection import eligible_latest
from reva.belief.snapshots import release_as_of
from reva.evaluation.cutoff import CutoffRepository
from reva.evaluation.replay import operational_as_known
from reva.evidence.runner import FixtureHttp, run_connector
from reva.kernel.context import Origin

pytestmark = pytest.mark.db

POP = "demography.population.wdi_midyear_total"
VINTAGES = Path(__file__).parents[1] / "fixtures" / "vintages"


def _loader():
    if "vintage_loader" not in sys.modules:
        spec = importlib.util.spec_from_file_location("vintage_loader", VINTAGES / "vintage_loader.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["vintage_loader"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["vintage_loader"]


vl = _loader()


def _now(engine) -> datetime:
    with engine.connect() as c:
        return c.execute(text("SELECT clock_timestamp()")).scalar_one()


def _lineage(engine, logical_key: str) -> list[dict]:
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(
            """SELECT o.id, o.revision_no, o.supersedes_id, o.value->>'value' AS v, o.recorded_at,
                      o.acquisition_id, o.parser_version, o.source_published_at, cv.version AS connector_version
               FROM evidence.observation o JOIN evidence.connector_version cv ON cv.id = o.connector_version_id
               WHERE o.logical_key = :k ORDER BY o.revision_no"""), {"k": logical_key}).mappings()]


def _value_for(manifest: dict, logical_key: str) -> str:
    by_obs = {i["observation_id"]: i["logical_key"] for i in manifest["inputs"]}
    vals = [o["value"]["value"] for o in manifest["outputs"]
            if any(by_obs.get(x) == logical_key for x in o["input_observation_ids"])]
    assert len(vals) == 1, (logical_key, vals)
    return vals[0]


async def test_two_revisions_yield_different_as_of_answers(db):
    name = "pop_fra_1990_1991_two_vintages"
    key = "wb:SP.POP.TOTL:FRA:1991"
    s1, _ = await vl.ingest_vintage(db.worker, db.store, name, "v1")
    assert (s1.inserted, s1.unchanged) == (2, 0)
    r1 = publish_release(db.worker, db.store, cutoff=_now(db.worker), origin=Origin.test_fixture,
                         environment="test", concepts=[POP])
    t_between = _now(db.worker)
    with db.api.connect() as conn:
        before_correction = operational_as_known(conn, t_between, origin="test_fixture", concepts=[POP],
                                                 store=db.store)
    s2, _ = await vl.ingest_vintage(db.worker, db.store, name, "v2")
    assert (s2.inserted, s2.unchanged) == (1, 1)  # only the revised 1991 value
    r2 = publish_release(db.worker, db.store, cutoff=_now(db.worker), origin=Origin.test_fixture,
                         environment="test", concepts=[POP])
    t_after = _now(db.worker)

    lineage = _lineage(db.worker, key)
    assert [(r["revision_no"], r["v"]) for r in lineage] == [(1, "56976123"), (2, "56982040")]

    with db.api.connect() as conn:
        assert release_as_of(conn, t_between, "test_fixture")["id"] == r1.release_id
        assert release_as_of(conn, t_after, "test_fixture")["id"] == r2.release_id
        between = operational_as_known(conn, t_between, origin="test_fixture", concepts=[POP], store=db.store)
        after = operational_as_known(conn, t_after, origin="test_fixture", concepts=[POP], store=db.store)
        assert between["status"] == after["status"] == "ok"
        assert between["release"]["release_id"] == str(r1.release_id)
        assert after["release"]["release_id"] == str(r2.release_id)
        assert _value_for(between, key) == "56976123"
        assert _value_for(after, key) == "56982040"
        assert between["manifest_verified"] is True and after["manifest_verified"] is True
        # A later correction cannot change an earlier as-of query.
        strip = ("replayed_at",)
        assert {k: v for k, v in between.items() if k not in strip} == {
            k: v for k, v in before_correction.items() if k not in strip}
        # Filtering precedes revision selection in the cutoff-scoped repository as well.
        rows_between = {r["logical_key"]: r for r in CutoffRepository(
            conn, t_between, "as_known", origins=["test_fixture"]).observations([POP]).rows}
        rows_after = {r["logical_key"]: r for r in CutoffRepository(
            conn, t_after, "as_known", origins=["test_fixture"]).observations([POP]).rows}
        assert rows_between[key]["revision_no"] == 1 and rows_between[key]["value"]["value"] == "56976123"
        assert rows_after[key]["revision_no"] == 2 and rows_after[key]["value"]["value"] == "56982040"
        assert rows_after[key]["first_release_value"]["value"] == "56976123"
        # and agrees with the belief publication's own eligibility query
        latest = {r["logical_key"]: r["id"] for r in eligible_latest(conn, t_between, ["test_fixture"], [POP])}
        assert latest[key] == rows_between[key]["id"]


async def test_aba_creates_three_revisions_identical_polls_create_none(db):
    name = "pop_fra_1985_1986_aba"
    sa, _ = await vl.ingest_vintage(db.worker, db.store, name, "a")
    assert sa.inserted == 2 and not sa.duplicate_acquisition
    # identical bytes: no new acquisition at all
    dup, _ = await vl.ingest_vintage(db.worker, db.store, name, "a")
    assert dup.duplicate_acquisition and dup.acquisition_id == sa.acquisition_id and dup.inserted == 0
    # different bytes (provider header changed) but identical semantic state: no revision
    rep, _ = await vl.ingest_vintage(db.worker, db.store, name, "a_repoll")
    assert not rep.duplicate_acquisition and rep.acquisition_id != sa.acquisition_id
    assert (rep.inserted, rep.unchanged) == (0, 2)
    sb, _ = await vl.ingest_vintage(db.worker, db.store, name, "b")
    assert (sb.inserted, sb.unchanged) == (1, 1)
    sa2, _ = await vl.ingest_vintage(db.worker, db.store, name, "a_again")
    assert (sa2.inserted, sa2.unchanged) == (1, 1)

    lin = _lineage(db.worker, "wb:SP.POP.TOTL:FRA:1986")
    assert [(r["revision_no"], r["v"]) for r in lin] == [(1, "55411045"), (2, "55420511"), (3, "55411045")]
    assert lin[0]["supersedes_id"] is None
    assert lin[1]["supersedes_id"] == lin[0]["id"] and lin[2]["supersedes_id"] == lin[1]["id"]
    assert len(_lineage(db.worker, "wb:SP.POP.TOTL:FRA:1985")) == 1
    with db.worker.connect() as c:
        n_acq = c.execute(text("SELECT count(*) FROM evidence.acquisition WHERE raw_sha256 = ANY(:s)"),
                          {"s": [sa.raw_sha256, rep.raw_sha256, sb.raw_sha256, sa2.raw_sha256]}).scalar_one()
    assert n_acq == 4


async def test_aba_with_byte_identical_return_creates_third_revision(db):
    name = "pop_fra_1955_1956_exact_return"
    await vl.ingest_vintage(db.worker, db.store, name, "a")
    await vl.ingest_vintage(db.worker, db.store, name, "b")
    again, _ = await vl.ingest_vintage(db.worker, db.store, name, "a")  # exact same bytes as the first poll
    lin = _lineage(db.worker, "wb:SP.POP.TOTL:FRA:1956")
    assert [r["v"] for r in lin] == ["43843000", "43850250", "43843000"], (again, lin)


async def test_replayed_raw_with_new_parser_is_distinguished_from_original_vintage(db):
    name = "pop_fra_1980_1981_parser_replay"
    orig, raw0 = await vl.ingest_vintage(db.worker, db.store, name, "v1")
    assert orig.inserted == 2
    y0, y1 = vl.series(name)["years"]
    replay_conn = vl.ReplayParserWorldBank(countries=("FRA",), years=(y0, y1))
    vl.register_connector_variant(db.worker, db.store, replay_conn)
    archived = db.store.get("raw", orig.raw_sha256)  # the original vintage bytes, as archived
    rep, _raw1 = await vl.ingest_vintage(db.worker, db.store, name, "v1", connector=replay_conn,
                                        replay_of=str(orig.acquisition_id), retrieved_at=raw0.retrieved_at,
                                        body=archived)
    assert not rep.duplicate_acquisition and rep.acquisition_id != orig.acquisition_id
    assert rep.raw_sha256 == orig.raw_sha256
    assert (rep.inserted, rep.unchanged) == (0, 2)  # no spurious revision for unchanged values
    with db.worker.connect() as c:
        acqs = {str(r["id"]): dict(r) for r in c.execute(text(
            """SELECT a.id, a.retrieved_at, a.recorded_at, a.response_metadata, cv.version, cv.name
               FROM evidence.acquisition a JOIN evidence.connector_version cv ON cv.id = a.connector_version_id
               WHERE a.id = ANY(:ids)"""), {"ids": [orig.acquisition_id, rep.acquisition_id]}).mappings()}
    o, r = acqs[str(orig.acquisition_id)], acqs[str(rep.acquisition_id)]
    assert o["version"] == "1.0.0" and r["version"] == vl.ReplayParserWorldBank.VERSION
    assert o["name"] == r["name"] == "worldbank.wdi.indicators"
    assert r["retrieved_at"] == o["retrieved_at"] == raw0.retrieved_at  # original retrieval clock preserved
    assert r["recorded_at"] > o["recorded_at"]
    assert r["response_metadata"]["replay_of_acquisition_id"] == str(orig.acquisition_id)
    assert "replay_of_acquisition_id" not in o["response_metadata"]
    for year in (y0, y1):
        lin = _lineage(db.worker, f"wb:SP.POP.TOTL:FRA:{year}")
        assert len(lin) == 1
        assert lin[0]["connector_version"] == "1.0.0" and lin[0]["parser_version"] == "wb-parse-1"
    # replaying the same bytes with the same new parser again is idempotent
    again, _ = await vl.ingest_vintage(db.worker, db.store, name, "v1", connector=replay_conn,
                                       replay_of=str(orig.acquisition_id), retrieved_at=raw0.retrieved_at,
                                       body=archived)
    assert again.duplicate_acquisition and again.acquisition_id == rep.acquisition_id


async def test_missing_source_publication_time_is_never_the_reference_date(db):
    conns = builtin_connectors()
    for name in ("worldbank.wdi.indicators", "un.comtrade.public_exact"):
        c = conns[name]
        await run_connector(c, FixtureHttp(name), db.store, db.worker, persist_cursor=False)
    await vl.ingest_vintage(db.worker, db.store, "pop_fra_1950_1951_chain", "v1")
    with db.worker.connect() as c:
        rows = c.execute(text(
            """SELECT s.source_key, count(*) AS n, count(o.source_published_at) AS with_pub
               FROM evidence.observation o JOIN evidence.source s ON s.id = o.source_id
               WHERE s.source_key IN ('worldbank.wdi', 'un.comtrade.public_preview')
               GROUP BY s.source_key"""
        )).mappings().all()
        dataset_dates = c.execute(text(
            """SELECT count(*) FROM evidence.acquisition a,
                      jsonb_array_elements(a.response_metadata->'batch_metadata') b
               WHERE b ? 'dataset_last_updated' AND b->>'dataset_last_updated' IS NOT NULL"""
        )).scalar_one()
    stats = {r["source_key"]: r for r in rows}
    assert stats["worldbank.wdi"]["n"] > 0 and stats["un.comtrade.public_preview"]["n"] > 0
    for r in stats.values():
        # neither the reference period nor the dataset-level 'lastupdated' was substituted
        assert r["with_pub"] == 0, r
    assert dataset_dates > 0  # dataset-level update time is preserved as metadata only


async def test_supersedes_chain_constraints_are_enforced_by_the_database(db):
    name = "pop_fra_1950_1951_chain"
    await vl.ingest_vintage(db.worker, db.store, name, "v1")
    await vl.ingest_vintage(db.worker, db.store, name, "v2")
    k50, k51 = "wb:SP.POP.TOTL:FRA:1950", "wb:SP.POP.TOTL:FRA:1951"
    l50, l51 = _lineage(db.worker, k50), _lineage(db.worker, k51)
    assert [r["revision_no"] for r in l50] == [1] and [r["revision_no"] for r in l51] == [1, 2]
    insert = text(
        """INSERT INTO evidence.observation(source_id, connector_version_id, acquisition_id, logical_key,
             revision_no, supersedes_id, subject_id, concept_version_id, support_id, value, uncertainty,
             source_flags, raw_selector, parser_version, provenance_activity_id, semantic_hash, record_hash)
           SELECT source_id, connector_version_id, acquisition_id, :lk, :rev, :sup, subject_id,
             concept_version_id, support_id, '{"kind":"decimal","value":"1","unit_ref":"unit.persons"}'::jsonb,
             uncertainty, source_flags, raw_selector, parser_version, provenance_activity_id, 'x', 'y'
           FROM evidence.observation WHERE id = :base""")
    head51 = l51[1]
    cases = {
        "skips_a_revision_number": {"lk": k51, "rev": 4, "sup": head51["id"], "base": head51["id"]},
        "points_to_another_logical_key": {"lk": k50, "rev": 3, "sup": head51["id"], "base": head51["id"]},
        "forks_an_already_superseded_revision": {"lk": k51, "rev": 2, "sup": l51[0]["id"], "base": head51["id"]},
        "later_revision_without_supersedes": {"lk": k51, "rev": 3, "sup": None, "base": head51["id"]},
        "first_revision_with_supersedes": {"lk": k50 + ":bogus", "rev": 1, "sup": l50[0]["id"],
                                           "base": head51["id"]},
    }
    for label, params in cases.items():
        with pytest.raises(sa_exc.DBAPIError), db.worker.begin() as c:
            c.execute(insert, params)
        assert [r["revision_no"] for r in _lineage(db.worker, k51)] == [1, 2], label
    assert [r["revision_no"] for r in _lineage(db.worker, k50)] == [1]
    # and history is append-only under the runtime role
    with pytest.raises(sa_exc.DBAPIError), db.worker.begin() as c:
        c.execute(text("UPDATE evidence.observation SET revision_no = 9 WHERE id = :i"), {"i": head51["id"]})
