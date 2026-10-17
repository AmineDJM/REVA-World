"""V7 BEAT-ENERGY: sealed prospective forecasts for the pre-registered winners only (agent BEAT-ENERGY).

Winners (``registry/forecasts/beat_energy/protocol.yaml``, run ``docs/dev/v7_beat_energy_result.json``): W1 h = 0 for
Brent ``BREPUUS`` and WTI ``WTIPUUS`` — the monthly-mean nowcast of the CURRENT month from EIA daily spot prices, 1-4
weeks after an STEO release (timeliness win). Henry Hub h = 0 and every h >= 1 did not survive Holm: not issued.

The batch asks one quantity question per series on the issue month of the STEO issue in force (reference period
= that month, ``cycle_horizon`` 0 of the quarterly cycle) and freezes, besides the three winners, the two
benchmarks of the pre-registered comparison: EIA's STEO point (attributed) and STEO's fair log-normal density.
The forecasters are exactly the registered code (:mod:`reva.evaluation.beat_energy`, offset 7 days: daily prices
up to the seal day minus ``DAILY_LAG_DAYS``), placed at the seal instant. Resolution and scoring run unchanged
through ``registry.service.resolve_due`` (frozen family docs, pinned tables listed under ``data``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from reva.evaluation import beat_energy as be
from reva.evaluation.energy_backtests import Origin
from reva.models.forecasting import energy_models as em

WIN_CODES = {"BREPUUS": "eia.brent_monthly", "WTIPUUS": "eia.wti_monthly"}
OFFSET = 7
MODELS: dict[str, tuple[str, str]] = {
    "forecast.beat_energy_daily_nowcast": ("1.0.0", "W1 winner: month-mean nowcast from EIA daily spot prices"),
    "forecast.beat_energy_combo_steo_daily": (
        "1.0.0",
        "W1 winner: combination with EIA STEO (inverse-MSE log weights from past errors)",
    ),
    "forecast.beat_energy_daily_nowcast_ln": ("1.0.0", "W1 winner: log-normal around the daily nowcast"),
    "forecast.eia_steo_fair_ln": ("1.0.0", "BENCHMARK: log-normal around EIA's STEO point, sigma from STEO's errors"),
    "forecast.reference_eia_steo": ("1.0.0", "external reference, attributed to EIA (STEO point)"),
}
KEY_OF = {
    "daily_rw": "forecast.beat_energy_daily_nowcast",
    "combo_steo_daily": "forecast.beat_energy_combo_steo_daily",
    "daily_rw_ln": "forecast.beat_energy_daily_nowcast_ln",
    "steo_fair_ln": "forecast.eia_steo_fair_ln",
    "steo": "forecast.reference_eia_steo",
}
CLAIM = (
    "timeliness: REVA nowcast of the current month's mean price from daily spot prices published after the STEO "
    "issue in force; pre-registered W1 winner (registry/forecasts/beat_energy/protocol.yaml); the reference month "
    "is partly elapsed at issue (see question timing)"
)


def seal_records(
    origins: Sequence[Origin],
    release_of: Mapping[str, date],
    daily: Mapping[str, Mapping[date, float]],
    now: datetime,
) -> dict[str, dict[str, Any]]:
    """Per winning code: the backtest record of the registered forecasters at ``now`` (h = 0, offset 7 history)."""
    inforce = max((o for o in origins if o.available_at <= now), key=lambda o: o.available_at)
    if (now.year * 12 + now.month - 1) != inforce.issue_idx:
        raise ValueError("the STEO issue in force is not for the current month: h = 0 is not the current month")
    rows, _ = be.w1_rows(origins, release_of, be.next_release_map(list(release_of.values())), daily, {})
    rows = [r for r in rows if r.offset == OFFSET and r.h == 0 and r.code in WIN_CODES and r.t < now]
    for code, sid in be.SPOT_SERIES.items():
        if code not in WIN_CODES:
            continue
        s = (inforce.steo.get(code) or {}).get(inforce.issue_idx)
        if s is None:
            raise LookupError(f"no STEO projection of {code} for the issue month")
        kn = be.known_daily(daily.get(sid, {}), now)
        rows.append(
            be.W1Row(
                code, inforce.vintage, inforce.issue_idx, OFFSET, now, 0, s, be.daily_point(kn, inforce.issue_idx), None
            )  # fmt: skip
        )
    recs = be.w1_forecasts(rows, daily)
    out = {r["code"]: r for r in recs if r["t"] == now.isoformat()}
    out["_inforce"] = {"vintage": inforce.vintage, "release": inforce.release_text}
    return out


def build_batch(
    original: Mapping[str, Any],
    recs: Mapping[str, Mapping[str, Any]],
    series_by_family: Mapping[str, Any],
    pins: Mapping[str, Mapping[str, Any]],
    steo_cells: Mapping[str, Mapping[str, Any]],
    *,
    now: datetime,
    cycle: str,
) -> dict[str, Any]:
    from reva.evaluation.registry.energy import to_predictive
    from reva.evaluation.registry.forecasts import _record, abstention
    from reva.evaluation.registry.questions import generate_questions
    from reva.evaluation.registry.service import BATCH_SCHEMA, _family_of
    from reva.models.forecasting.registry_baselines import Predictive

    fam_docs: dict[str, Any] = {}
    questions: list[dict[str, Any]] = []
    forecasts: list[dict[str, Any]] = []
    for code, fam_key in WIN_CODES.items():
        doc = dict(original["families"][fam_key])
        doc["questions"] = [{"type": "quantity", "horizons": [0]}]
        doc["beat_energy"] = {"protocol": be.PROTOCOL_PATH, "window": "W1", "h": 0}
        fam = _family_of(doc)
        fam_docs[fam_key] = doc
        pin = pins[fam.dataset_key]
        qs, skipped = generate_questions(
            fam, series_by_family[fam_key], cycle=cycle, interval_level=0.8, source_pin=pin, issued_on=now.date()
        )
        if not qs:
            raise LookupError(f"no question generated for {fam_key}: {skipped}")
        q = qs[0]
        r = recs[code]
        if q["reference_period"] != r["target"]:
            raise ValueError(f"question period {q['reference_period']} != nowcast target {r['target']}")
        questions.append(q)
        hist: list[tuple[int, float]] = []
        detail = (
            ("steo_issue", str(recs["_inforce"]["vintage"])),
            ("steo_release_date", str(recs["_inforce"]["release"])),
            ("daily_information", f"EIA daily spot {be.SPOT_SERIES[code]}, days <= seal day - {be.DAILY_LAG_DAYS}"),
            ("past_errors", str(r["n_past"])),
            ("protocol", be.PROTOCOL_PATH),
        )
        for m, mk in KEY_OF.items():
            v = r.get(m)
            if v is None:
                forecasts.append(abstention(q, mk, f"fewer than {be.MIN_PAST} past errors at this offset"))
                continue
            p = to_predictive(v, detail) if isinstance(v, em.Pred) else Predictive("point", float(v), detail=detail)
            rec = _record(q, mk, p, hist)
            if m == "steo":
                rec["attribution"] = (
                    f"U.S. Energy Information Administration, Short-Term Energy Outlook {recs['_inforce']['vintage']} (projection), as pinned at issue"
                )
                rec["source_cell"] = dict(steo_cells[code])
            elif m == "steo_fair_ln":
                rec["attribution"] = "median: EIA STEO projection (attributed); spread: RMS of STEO's past log errors"
                rec["claim"] = "benchmark (error-based fan around the STEO point)"
            else:
                rec["claim"] = CLAIM
                if m == "combo_steo_daily":
                    rec["attribution"] = "REVA combination of the EIA STEO projection and REVA's daily nowcast"
                    rec["weights"] = {"steo": r.get("w_steo")}
                rec["licence_review"] = (
                    "input daily prices are Refinitiv (LSEG) data republished by EIA (atlas.eia.spot_d licence_review)"
                )
            forecasts.append(rec)
    return {
        "schema": BATCH_SCHEMA,
        "label": "V7 BEAT-ENERGY sealed winners (pre-registered W1 timeliness nowcasts) + benchmarks",
        "origin": original["origin"],
        "cycle": cycle,
        "issued_at": now.isoformat(),
        "replay_mode": "operational_as_known",
        "supplement_of": original.get("_sha256"),
        "statement": (
            "Only the Holm winners of registry/forecasts/beat_energy/protocol.yaml are issued (W1 h0: Brent, WTI), "
            "with the pre-registered benchmarks (EIA STEO point, STEO fair log-normal). Target month "
            f"{questions[0]['reference_period']} is partly elapsed at issue (question timing)."
        ),
        "specs": original["specs"],
        "families": fam_docs,
        "models": [{"model_key": k, "version": v, "description": d} for k, (v, d) in MODELS.items()],
        "data": {
            "selection_rule": "per dataset_key: latest evidence.dataset version at issued_at",
            "tables": dict(pins),
        },
        "code_digest": original.get("code_digest"),
        "questions": questions,
        "forecasts": forecasts,
        "generation": {"skipped": [], "pending_families": [], "notes": []},
    }


def seal(engine: Any, store: Any, *, origin: str = "live", actor: str = "agent:beat-energy") -> dict[str, Any]:
    """Pins tables at the seal instant, builds and stores the batch, records one issue row per model."""
    import polars as pl
    from sqlalchemy import text

    from reva.adapters.connectors.atlas_vintage_steo import PAGE_URL, parse_archive_page
    from reva.adapters.postgres import repositories as repo
    from reva.atlas.tables import read_atlas_table
    from reva.evaluation.energy_backtests import VINTAGE_TABLE, origins_from_cells
    from reva.evaluation.ledger import load_frozen_manifest
    from reva.evaluation.registry import periods as P
    from reva.evaluation.registry.data import latest_version
    from reva.evaluation.registry.questions import extract_series
    from reva.evaluation.registry.service import ISSUE_KIND, ISSUE_LOCK, SCOPE_KIND, _batches, _family_of, _insert_eval
    from reva.evaluation.vintages import cells_from_atlas_frame

    with engine.connect() as conn:
        now: datetime = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        cycle = P.cycle_of(now.date())
        done = conn.execute(
            text(
                "SELECT count(*) FROM model.evaluation WHERE metrics->>'kind' = :k AND metrics->>'label' LIKE 'V7 BEAT-ENERGY%' AND task_scope->>'cycle' = :c"
            ),
            {"k": ISSUE_KIND, "c": cycle},
        ).scalar_one()
        if done:
            return {"status": "already_sealed_this_cycle", "cycle": cycle}
        mv = {
            r["model_key"]: r["id"]
            for r in conn.execute(
                text("SELECT model_key, version, id::text AS id FROM model.version WHERE model_key = ANY(:k)"),
                {"k": sorted(MODELS)},
            ).mappings()
            if r["version"] == MODELS[r["model_key"]][0]
        }
        if set(mv) != set(MODELS):
            return {"status": "models_not_seeded", "missing": sorted(set(MODELS) - set(mv))}
        originals = [b for b in _batches(conn, origin) if b["cycle"] == cycle]
        original = None
        for b in originals:
            man = dict(load_frozen_manifest(store, b["sha"]))
            if not man.get("supplement_of") and all(f in man.get("families", {}) for f in WIN_CODES.values()):
                man["_sha256"] = b["sha"]
                original = man
        if original is None:
            return {"status": "no_original_batch_with_the_families", "cycle": cycle}
        page = conn.execute(
            text(
                "SELECT raw_sha256 FROM evidence.acquisition WHERE origin = :o AND response_metadata->>'final_url' = :u ORDER BY retrieved_at DESC LIMIT 1"
            ),
            {"o": origin, "u": PAGE_URL},
        ).scalar_one()
        pins = {}
        for k in ("atlas.eia_steo.steo_observed", VINTAGE_TABLE, "atlas.eia.spot_d"):
            p = latest_version(conn, k, origin=origin, cutoff=now)
            if p is None:
                return {"status": "table_missing", "table": k}
            pins[k] = p
    issues, _ = parse_archive_page(store.get("raw", page))
    release_of = {f"{i.year:04d}-{i.month:02d}": i.release for i in issues if i.release is not None}
    vpin = pins[VINTAGE_TABLE]
    cells = cells_from_atlas_frame(
        read_atlas_table(store, vpin["artifact_sha256"]), dataset_key=VINTAGE_TABLE,
        table_recorded_at=datetime.fromisoformat(str(vpin["recorded_at"])),
    )  # fmt: skip
    origins = origins_from_cells(cells, codes=tuple(be.SPOT_SERIES))
    spot = read_atlas_table(store, pins["atlas.eia.spot_d"]["artifact_sha256"])
    daily: dict[str, dict[date, float]] = {}
    for sid in be.SPOT_SERIES.values():
        f = spot.filter((pl.col("indicator") == f"EIA_SPOT|series={sid}") & pl.col("value").is_not_null())
        daily[sid] = {d: float(v) for d, v in f.select("period_start", "value").iter_rows()}
    recs = seal_records(origins, release_of, daily, now)
    vint = recs["_inforce"]["vintage"]
    steo_cells = {}
    for code in WIN_CODES:
        y, m = (int(x) for x in vint.split("-"))
        row = cells.filter(
            (pl.col("vintage") == vint) & (pl.col("variable") == f"eia_steo:{code}") & (pl.col("period_start") == date(y, m, 1))
        ).row(0, named=True)  # fmt: skip
        steo_cells[code] = {k: str(row[k]) for k in ("raw_sha256", "selector", "indicator", "value")} | {
            "table": {k: vpin[k] for k in ("dataset_key", "artifact_sha256", "recorded_at")}
        }
    obs = read_atlas_table(store, pins["atlas.eia_steo.steo_observed"]["artifact_sha256"])
    series_by_family = {fk: extract_series(obs, _family_of(original["families"][fk]))[0] for fk in WIN_CODES.values()}
    batch = build_batch(original, recs, series_by_family, pins, steo_cells, now=now, cycle=cycle)
    qids = sorted(q["question_id"] for q in batch["questions"])
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ISSUE_LOCK})
        sha = repo.store_json_artifact(
            conn, store, batch, "evaluation", schema_version="forecast-registry-batch/1",
            rights={"licence": "internal-derived; STEO values: U.S. public domain (EIA, attributed); daily spot "
                    "prices Refinitiv (LSEG) via EIA: licence_review"},
        )  # fmt: skip
        act = repo.simple_activity(
            "evaluation.forecast_registry_issue", "evaluation", actor,
            {"cycle": cycle, "origin": origin, "batch_sha256": sha, "supplement_of": original["_sha256"]},
        )  # fmt: skip
        repo.record_activity(conn, act, inputs=[(original["_sha256"], {"role": "original_batch"})], outputs=[sha])
        ctx = repo.create_context(conn, mode="evaluation", origin=origin, manifest_sha256=sha, knowledge_cutoff=now)
        scope = {
            "kind": SCOPE_KIND, "origin": origin, "cycle": cycle, "batch_sha256": sha,
            "supplement_of": original["_sha256"],
            "question_keys": sorted(q["question_key"] for q in batch["questions"]), "question_ids": qids,
        }  # fmt: skip
        for mk, (ver, _d) in MODELS.items():
            mine = [f for f in batch["forecasts"] if f["model"] == mk]
            _insert_eval(conn, mv[mk], ctx, sha, "operational_as_known", scope,
                         {"kind": ISSUE_KIND, "status": "sealed", "label": batch["label"], "model": f"{mk}@{ver}",
                          "issued_at": now.isoformat(), "batch_sha256": sha, "supplement_of": original["_sha256"],
                          "n_questions": len(qids), "n_issued": sum(1 for f in mine if f["status"] == "issued"),
                          "n_abstained": sum(1 for f in mine if f["status"] == "abstained")}, act.id)  # fmt: skip
    return {
        "status": "sealed",
        "batch_sha256": sha,
        "cycle": cycle,
        "issued_at": now.isoformat(),
        "questions": [(q["question_id"], q["family"], q["reference_period"]) for q in batch["questions"]],
        "forecasts": [(f["question_id"], f["model"], f.get("point"), f["status"]) for f in batch["forecasts"]],
    }


__all__ = ["KEY_OF", "MODELS", "WIN_CODES", "build_batch", "seal", "seal_records"]
