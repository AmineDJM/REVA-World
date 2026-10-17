"""Effect estimates (migration 0019_effect_estimates): loader, append-only storage, retraction / reinstatement,
grants, the connector in fixture mode, the readers and the /v1/effects API, on a throwaway database.

Synthetic estimates (dataset ``eztest*``) and the synthetic fixture document; nothing here is a real-world fact."""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from reva.effects import (
    EffectEstimateIn,
    Horizon,
    IrfPoint,
    RevaRun,
    Uncertainty,
    compare_estimates,
    get_estimate,
    list_estimates,
    load_effect_estimates,
)
from reva.kernel.errors import ValidationProblem

pytestmark = pytest.mark.db


def _raw(db, tag: str) -> str:  # type: ignore[no-untyped-def]
    return db.store.put_bytes(f"effects-test {tag} {uuid.uuid4()}".encode(), "raw", "text/plain").sha256


def _est(raw: str, i: int, **kw: Any) -> EffectEstimateIn:
    base: dict[str, Any] = dict(
        estimate_id=f"e{i}",
        entry_id="synthetic_paper",
        cause_kind="variable",
        cause_id="output.gap",
        cause_definition="Yt - Yt* (synthetic)",
        cause_transform="gap",
        effect_kind="variable",
        effect_id="labour.unemployment_gap",
        effect_definition="Ut - Ut* (synthetic)",
        effect_transform="gap",
        measure="slope",
        unit="pp_per_percent",
        sign="negative",
        method="time_series_regression",
        as_of="2013-01-31",
        point=-0.4 - i / 100,
        point_kind="point",
        as_printed=f"{-0.4 - i / 100:.2f}",
        uncertainty=Uncertainty(se=0.05, status="reported"),
        scope={"countries": ["FRA"], "period": {"from": "1980", "to": "2011"}},
        citation={"text": "Synthetic (2013).", "short": "Synthetic (2013)"},
        locator=f"Table 1 row {i}",
        quote=f"row {i} {-0.4 - i / 100:.2f} (0.05)",
        transcription="transcribed",
        raw_sha256=raw,
        rights={"licence": "CC0", "restricted": False},
    )
    base.update(kw)
    return EffectEstimateIn(**base)


def _load(db, ds: str, raw: str, ests, **kw):  # type: ignore[no-untyped-def]
    return load_effect_estimates(
        db.worker,
        db.store,
        source_kind=kw.pop("source_kind", "publication"),
        source_key="effects.literature",
        dataset=ds,
        raw_sha256s=[raw],
        retrieved_at=datetime.now(UTC),
        origin="test_fixture",
        estimates=ests,
        **kw,
    )


def _current(db, ds: str) -> dict[str, dict[str, Any]]:  # type: ignore[no-untyped-def]
    with db.api.connect() as c:
        return {
            r["estimate_key"]: dict(r)
            for r in c.execute(text("SELECT * FROM kg.effect_estimate_current WHERE dataset = :d"), {"d": ds})
            .mappings()
        }


def test_loader_idempotent_revision_retraction_reinstatement(db) -> None:  # type: ignore[no-untyped-def]
    ds = "eztest" + uuid.uuid4().hex[:6]
    raw = _raw(db, ds)
    r1 = _load(db, ds, raw, [_est(raw, i) for i in range(3)])
    assert (r1.inserted, r1.new, r1.rejected_total) == (3, 3, 0) and r1.load_id, r1.rejected_sample
    cur = _current(db, ds)
    assert len(cur) == 3
    row = cur["pub:effects.literature:synthetic_paper:e1"]
    assert float(row["point"]) == -0.41 and row["sign"] == "negative" and row["source_kind"] == "publication"
    r2 = _load(db, ds, raw, [_est(raw, i) for i in range(3)])
    assert (r2.inserted, r2.unchanged, r2.load_id) == (0, 3, None)  # nothing written, no load row
    changed = _est(raw, 1, applicability="REVA note: changed")
    r3 = _load(db, ds, raw, [_est(raw, 0), changed])  # e2 absent from a complete delivery
    assert (r3.new_revision, r3.retracted, r3.unchanged) == (1, 1, 1)
    cur = _current(db, ds)
    assert set(cur) == {"pub:effects.literature:synthetic_paper:e0", "pub:effects.literature:synthetic_paper:e1"}
    assert cur["pub:effects.literature:synthetic_paper:e1"]["applicability"] == "REVA note: changed"
    with db.api.connect() as c:
        reason = c.execute(
            text("SELECT reason FROM kg.effect_estimate_retraction WHERE estimate_key LIKE :k"),
            {"k": "%synthetic_paper:e2"},
        ).scalars().all()  # fmt: skip
    assert "absent_from_delivery" in reason
    r4 = _load(db, ds, raw, [_est(raw, i) for i in range(3)], complete=False)
    assert r4.reinstated == 1 and r4.new_revision == 1  # e2 back; e1 back to its first content
    bad = _est(raw, 5, sign="positive")
    r5 = _load(db, ds, raw, [_est(raw, i) for i in range(2)] + [bad])
    assert r5.rejected_counts == {"sign_mismatch": 1} and not r5.complete_effective and r5.retracted == 0


def test_database_refuses_inconsistent_rows_and_mutation(db) -> None:  # type: ignore[no-untyped-def]
    ds = "eztest" + uuid.uuid4().hex[:6]
    raw = _raw(db, ds)
    rep = _load(db, ds, raw, [_est(raw, 0)])
    with db.owner.connect() as c:
        rid = c.execute(text("SELECT id FROM kg.effect_estimate WHERE load_id = :l"), {"l": rep.load_id}).scalar_one()
    with pytest.raises(DBAPIError), db.owner.begin() as c:
        c.execute(text("UPDATE kg.effect_estimate SET point = 0 WHERE id = :i"), {"i": rid})
    with pytest.raises(DBAPIError), db.owner.begin() as c:
        c.execute(text("DELETE FROM kg.effect_load WHERE id = :i"), {"i": rep.load_id})
    # a sealed load cannot receive rows later; the api role cannot write at all
    with pytest.raises(DBAPIError), db.worker.begin() as c:
        c.execute(
            text("""INSERT INTO kg.effect_estimate_retraction(load_id, estimate_row_id, estimate_key, origin, reason)
                    SELECT load_id, id, estimate_key, origin, 'withdrawn_by_curator' FROM kg.effect_estimate
                     WHERE id = :i"""),
            {"i": rid},
        )
    with pytest.raises(DBAPIError), db.api.begin() as c:
        c.execute(
            text("""INSERT INTO kg.effect_load(kind, source_key, dataset, origin, raw_sha256s, retrieved_at,
                        loader_version) VALUES ('identity','x','y','test_fixture','{}', now(), 'x')""")
        )
    # CHECK constraints back the Python validation (sign vs point; unit vs measure)
    for col, val in (("sign", "positive"), ("unit", "currency_per_currency")):
        with pytest.raises(DBAPIError), db.worker.begin() as c:
            lid = c.execute(
                text("""INSERT INTO kg.effect_load(kind, source_key, dataset, origin, raw_sha256s, retrieved_at,
                            loader_version) VALUES ('publication','effects.literature',:d,'test_fixture',
                            ARRAY[:r], now(), 't') RETURNING id"""),
                {"d": ds, "r": raw},
            ).scalar_one()
            c.execute(
                text(f"""INSERT INTO kg.effect_estimate(load_id, estimate_key, origin, content_sha256, cause_kind,
                        cause_id, cause_definition, cause_transform, effect_kind, effect_id, effect_definition,
                        effect_transform, measure, unit, sign, point, point_kind, as_printed, uncertainty_status,
                        horizon_kind, method, source_kind, source_key, dataset, citation, locator, quote,
                        transcription, raw_sha256, as_of, retrieved_at)
                        SELECT :l, 'pub:effects.literature:x:y', 'test_fixture', repeat('a', 64), 'variable',
                        'output.gap', 'd', 'gap', 'variable', 'labour.unemployment_gap', 'd', 'gap', 'slope',
                        {"'" + val + "'" if col == "unit" else "'pp_per_percent'"},
                        {"'" + val + "'" if col == "sign" else "'negative'"}, -0.4, 'point', '-0.4', 'not_reported',
                        'impact', 'other', 'publication', 'effects.literature', :d, '{{"text":"x"}}', 'T1', 'q -0.4',
                        'transcribed', :r, '2013-01-01', retrieved_at FROM kg.effect_load WHERE id = :l"""),
                {"l": lid, "d": ds, "r": raw},
            )


def test_reva_run_estimates_and_readers(db) -> None:  # type: ignore[no-untyped-def]
    ds = "eztest" + uuid.uuid4().hex[:6]
    raw = _raw(db, ds)
    run = RevaRun("run" + ds, "deadbeef", "atlas:snapshot-test", "2026-10-17", replicates=())
    e = _est(
        raw,
        0,
        entry_id=None,
        cause_kind="event_type",
        cause_id="financial_crisis",
        cause_transform="occurrence",
        effect_kind="atlas_concept",
        effect_id="imf.gdp_real_growth",
        effect_transform="growth_rate",
        measure="response",
        unit="percent",
        shock_size=1,
        shock_unit="occurrence",
        point=-2.5,
        as_printed=None,
        uncertainty=Uncertainty(se=0.8, status="reported"),
        horizon=Horizon("at_horizon", 2, "year"),
        irf=(IrfPoint(0, "year", -1.0, se=0.4), IrfPoint(1, "year", -2.0, se=0.6), IrfPoint(2, "year", -2.5, se=0.8)),
        method="local_projection",
        citation=None,
        locator=None,
        quote=None,
        transcription=None,
        raw_sha256=None,
        run=run,
        as_of="2026-10-17",
        scope={"countries": ["WLD"], "period": {"from": "1970", "to": "2017"}},
    )
    rep = _load(db, ds, raw, [e], source_kind="reva_run")
    assert rep.inserted == 1, rep.as_dict()
    with db.api.connect() as c:
        got = get_estimate(c, f"run:run{ds}:e0")
        assert got is not None and got["source"]["kind"] == "reva_run" and len(got["irf"]) == 3
        assert "REVA estimation run" in got["wording"] and "REVA does not establish" in got["wording"]
        assert got["cause"]["label_loaded"] and got["effect"]["label_loaded"]
        hits = list_estimates(c, cause="financial_crisis", effect="imf.gdp_real_growth", country="FRA")
        assert any(h["estimate_key"] == got["estimate_key"] for h in hits)  # WLD scope matches any country
        assert list_estimates(c, cause="financial_crisis", as_of_max="2020-01-01") == [] or all(
            h["as_of"] <= "2020-01-01" for h in list_estimates(c, cause="financial_crisis", as_of_max="2020-01-01")
        )
        cmp_ = compare_estimates(c, cause="financial_crisis", effect="imf.gdp_real_growth")
        assert cmp_["n_estimates"] >= 1 and cmp_["groups"][0]["comparability_key"]["measure"] == "response"
    with pytest.raises(ValidationProblem):
        _load(db, ds, raw, [e], source_kind="nonsense")


async def test_connector_fixture_run_loads_synthetic_entry(db) -> None:  # type: ignore[no-untyped-def]
    from reva.adapters.connectors import atlas_effects_lit as C
    from reva.atlas.ingest import run_atlas_ingest
    from reva.atlas.sources import ensure_registered

    with db.owner.connect() as c:
        exists = c.execute(
            text("SELECT count(*) FROM evidence.connector_version WHERE name = :n"), {"n": C.CONNECTOR_NAME}
        ).scalar_one()
    if not exists:
        ensure_registered(db.owner, db.store, C.atlas_source())
    out = await run_atlas_ingest(db.settings, db.worker, db.store, C.CONNECTOR_NAME, mode="fixture")
    assert out["status"] == "completed", out
    s = out["summary"]["entries"]["synthetic_okun_fixture"]
    assert s["status"] == "loaded" and s["refused_by_text_check"] == {} and s["load"]["inserted"] == 3
    cur = _current(db, "synthetic_okun_fixture")
    assert {r["transcription"] for r in cur.values()} == {"transcribed_double_checked"}
    again = await run_atlas_ingest(db.settings, db.worker, db.store, C.CONNECTOR_NAME, mode="fixture")
    assert again["summary"]["entries"]["synthetic_okun_fixture"]["load"]["unchanged"] == 3


@pytest.fixture(scope="module")
def api(db) -> Iterator[tuple[TestClient, str]]:  # type: ignore[no-untyped-def]
    from reva.api.app import create_app
    from reva.api.auth import new_api_token

    raw = _raw(db, "api")
    _load(db, "eztestapi", raw, [_est(raw, i) for i in range(3)])
    restricted = _est(raw, 9, effect_kind="atlas_concept", effect_id="imf.cpi_index", effect_transform="percent_change",
                      cause_id="oil_price.global", cause_transform="percent_change", measure="response",
                      unit="percent", shock_size=10, shock_unit="percent", sign="positive", point=0.36,
                      as_printed="0.36", quote="Total 0.36", rights={"licence": "(c)", "restricted": True})  # fmt: skip
    _load(db, "eztestapi2", raw, [restricted])
    tok, sha = new_api_token()
    with db.owner.begin() as c:
        c.execute(
            text("INSERT INTO ops.api_token(token_sha256, scopes, label) VALUES (:h, :s, 'effects-read')"),
            {"h": sha, "s": ["read"]},
        )
    settings = type(db.settings).model_validate(
        {
            **db.settings.model_dump(),
            "reva_session_secret": SecretStr("s" * 48),
            "reva_allowed_hosts": "testserver,localhost",
            "reva_cors_origins": "",
        }
    )
    app = create_app(settings)
    try:
        with TestClient(app) as client:
            yield client, tok
    finally:
        app.state.engine.dispose()


def _get(api, path: str, **params: Any) -> Any:  # type: ignore[no-untyped-def]
    client, tok = api
    return client.get(path, params=params, headers={"Authorization": f"Bearer {tok}"})


def test_api_list_get_compare_variables(api) -> None:  # type: ignore[no-untyped-def]
    r = _get(api, "/v1/effects/estimates", cause="output.gap", effect="labour.unemployment_gap", limit=2)
    assert r.status_code == 200, r.text
    body = r.json()
    codes = {w["code"] for w in body["warnings"]}
    assert "attributed_estimates" in codes and body["origin"] == "test_fixture"
    assert len(body["data"]) == 2 and body["next_cursor"]
    page2 = _get(api, "/v1/effects/estimates", cause="output.gap", limit=2, cursor=body["next_cursor"]).json()
    assert {d["row_id"] for d in page2["data"]}.isdisjoint({d["row_id"] for d in body["data"]})
    key = "pub:effects.literature:synthetic_paper:e1"
    one = _get(api, f"/v1/effects/estimates/{key}")
    assert one.status_code == 200 and one.json()["data"]["source"]["locator"] == "Table 1 row 1"
    assert _get(api, "/v1/effects/estimates/pub:effects.literature:nope:x").status_code == 404
    assert _get(api, "/v1/effects/estimates/bad key").status_code == 422
    # an atlas concept matches estimates on variables curated as measuring it
    via = _get(api, "/v1/effects/estimates", effect="imf.cpi_index").json()
    assert via["data"] and "licence_restricted" in {w["code"] for w in via["warnings"]}
    cmp_ = _get(api, "/v1/effects/compare", cause="output.gap", effect="labour.unemployment_gap").json()
    assert "no_pooling" in {w["code"] for w in cmp_["warnings"]} and cmp_["data"]["groups"][0]["n"] >= 3
    assert _get(api, "/v1/effects/compare", cause="output.gap").status_code == 422
    v = _get(api, "/v1/effects/variables").json()
    assert any(x["id"] == "prices.consumer_level" and x["atlas_concepts"] == ["imf.cpi_index"] for x in v["data"])


def test_dataclass_replace_keeps_inputs_frozen() -> None:
    e = _est("a" * 64, 0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.point = 1.0  # type: ignore[misc]
