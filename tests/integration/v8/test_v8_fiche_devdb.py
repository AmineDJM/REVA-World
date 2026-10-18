"""V8 Fiche bundle against the dev database `reva` (read-only transactions) and its live atlas snapshot.

Skipped when the database is unreachable, holds no KG (not the dev DB) or the latest atlas query file is not
materialised (never built here). Checks resolution of the URL forms other features use (``country/<ISO3>`` in any
case, alpha-2, Q-ids, provider ids, anchors), the honesty of the bundle (every number has a source, Missing has a
reason), and the warm latency of a cached page (p95 < 300 ms).
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import Connection, text

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def env() -> dict[str, Any]:
    from reva.adapters.objects.factory import build_store
    from reva.adapters.postgres.engine import make_engine
    from reva.atlas.query import SnapshotNotMaterialised, query_for, request_snapshot
    from reva.settings import get_settings

    s = get_settings()
    store = build_store(s)
    try:
        eng = make_engine("api", s)
        with eng.connect() as conn:
            if not conn.execute(text("SELECT EXISTS (SELECT 1 FROM kg.entity_current WHERE qid = 'Q142')")).scalar_one():
                pytest.skip("not the dev database (no Q142)")
            snap, _ = request_snapshot(conn, store, origin=None)
    except Exception as e:
        pytest.skip(f"dev database or atlas snapshot not available: {type(e).__name__}")
    try:
        q = query_for(snap, store, build=False)
    except SnapshotNotMaterialised:
        pytest.skip("the atlas query file is not materialised on this machine")

    @contextmanager
    def conn_factory() -> Iterator[Connection]:
        with eng.connect() as c:
            c.execute(text("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"))
            c.commit()
            with c.begin():
                yield c

    return {"conn": conn_factory, "atlas": lambda: q, "store": lambda: store}


def _build(env: dict[str, Any], kind: str, ident: str, **kw: Any) -> dict[str, Any]:
    from reva.v8.entity.bundle import build

    return build(env["conn"], env["atlas"], env["store"], kind, ident, origin=None, **kw)


@pytest.mark.parametrize("kind,ident", [("country", "FRA"), ("country", "fra"), ("country", "FR"), ("pays", "fr"), ("entity", "Q142")])
def test_country_url_forms_resolve_to_iso3(env: dict[str, Any], kind: str, ident: str) -> None:
    from reva.v8.entity.resolve import resolve

    with env["conn"]() as conn:
        r = resolve(conn, kind, ident)
    assert (r.kind, r.id, r.iso3, r.qid) == ("country", "FRA", "FRA", "Q142")


@pytest.mark.parametrize(
    "kind,ident,canonical",
    [
        ("drug", "CHEMBL941", ("drug", "chembl:CHEMBL941")),
        ("chemical", "CHEBI:27732", ("compound", "chebi:27732")),
        ("conflict", "13243", ("armed_conflict", "ucdp:conflict:13243")),
        ("company", "lei:549300LKT9PW7ZIBDF31", ("legal_entity", "lei:549300LKT9PW7ZIBDF31")),
        ("commodity", "brent", ("commodity", "crude_oil")),
    ],
)
def test_provider_ids_and_anchors_resolve(env: dict[str, Any], kind: str, ident: str, canonical: tuple[str, str]) -> None:
    from reva.v8.entity.resolve import resolve

    with env["conn"]() as conn:
        r = resolve(conn, kind, ident)
    assert (r.kind, r.id) == canonical


def test_unknown_id_is_not_guessed(env: dict[str, Any]) -> None:
    from reva.v8.entity.resolve import NotResolved, resolve

    with env["conn"]() as conn, pytest.raises(NotResolved):
        resolve(conn, "drug", "CHEMBL0000000000")


def _assert_honest(b: dict[str, Any]) -> None:
    from reva.api.routes.v8_entity import V8FicheBundle

    V8FicheBundle.model_validate(b)
    assert not b["meta"]["errors"], b["meta"]["errors"]
    for s in b["headline"]:
        if s["value"] is None and s["value_text"] is None and s["ref"] is None:
            assert s["missing"] and s["missing"]["reason_fr"], s  # typed Missing, never an empty number
        if s["value"] is not None and s["kind"] not in ("count",):
            assert s["source"] is not None, s  # every number with its source
    for c in b["series"]:
        assert (c["value"] is not None and c["source"]) or c["missing"] or c["value_text"], c["key"]
    for m in b["missing"]:
        assert m["reason_fr"]
    for c in b["claims"]["incoming"] + b["claims"]["outgoing"]:
        assert c["source"]["source"] and c["tier"]  # attributed, with tier
    for q in b["forecasts"]["questions"]:
        if q["status"] == "pending":
            assert "pas encore jugée" in q["judged_fr"]


def test_country_bundle(env: dict[str, Any]) -> None:
    b = _build(env, "country", "FRA", use_cache=False)
    if b["meta"]["warming"]:  # first access of this process: the registry index is being read
        b = _build(env, "country", "FRA", use_cache=False)
    _assert_honest(b)
    assert b["identity"]["label"] == "France" and b["identity"]["iso3"] == "FRA"
    keys = [s["key"] for s in b["headline"]]
    assert keys[:2] == ["reva.population.total", "reva.economy.gdp_usd"] and len(keys) == 6
    assert len(b["series"]) >= 10
    assert b["space"]["geometry"] is not None
    assert any(i["scheme"] == "ISO 3166-1 alpha-3" and i["value"] == "FRA" for i in b["identity"]["ids"])


def test_drug_bundle_reads_salt_forms(env: dict[str, Any]) -> None:
    b = _build(env, "drug", "chembl:CHEMBL941", use_cache=False)
    _assert_honest(b)
    forms = {f["ref"]["id"] for f in b["related_forms"]}
    assert "chembl:CHEMBL1642" in forms  # imatinib mesylate carries the mechanism targets and indications
    targets = next(s for s in b["headline"] if s["key"] == "interacts_with")
    assert targets["value"] is not None and int(targets["value"]) >= 1


def test_anchor_bundle_is_labelled(env: dict[str, Any]) -> None:
    b = _build(env, "commodity", "crude_oil", use_cache=False)
    _assert_honest(b)
    assert b["identity"]["anchor"] is True and b["identity"]["anchor_note"]
    for s in b["series"]:
        if s["out_of_snapshot"]:
            assert s["source"]["verification"] == "outside_atlas_snapshot"


@pytest.mark.parametrize("kind,ident", [("gene", "hgnc:76"), ("disease", "mondo:0005148"), ("facility", "gppd:WRI1002735"), ("taxon", "gbif:5219404")])
def test_other_kinds_are_honest(env: dict[str, Any], kind: str, ident: str) -> None:
    _assert_honest(_build(env, kind, ident, use_cache=False))


def test_cached_page_is_fast(env: dict[str, Any]) -> None:
    _build(env, "country", "FRA")
    _build(env, "country", "FRA")
    ms = []
    for _ in range(20):
        t0 = time.perf_counter()
        b = _build(env, "country", "FRA")
        ms.append((time.perf_counter() - t0) * 1000)
        assert b["meta"]["cached"]
    p95 = statistics.quantiles(ms, n=20)[18]
    assert p95 < 300, f"cached bundle p95 {p95:.0f} ms"
