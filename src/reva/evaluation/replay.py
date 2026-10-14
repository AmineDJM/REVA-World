"""Exact temporal replay in three explicitly labelled modes.

1. ``operational_as_known`` reproduces what REVA actually had published at the
   cutoff: the original release (published at/before the cutoff) and its pinned
   inputs and manifests. Nothing is re-selected; no backfilled source version or
   later mapping is used. No release at the cutoff is a typed
   ``no_release_at_cutoff`` result, never an empty success.
2. ``available_then_reconstruction`` re-runs source_select_v1
   (:func:`reva.belief.selection.select`) over observations whose availability
   at/before the cutoff is independently evidenced. It is *not* what REVA
   actually believed. Records without proof are excluded and counted; the
   algorithm-design date is reported separately from availability.
3. ``revised_data_retrospective`` splits current revised data temporally for a
   rolling forecast evaluation. It is labelled everywhere: it is not a real-time
   backtest and no forecasts were issued at these origins.

Strict modes never fall back to the retrospective mode: there is no code path
that downgrades, and the retrospective function must be called explicitly.
No LLM is used in replay.
"""

from __future__ import annotations

import itertools
import json
import math
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from reva.evaluation.cutoff import CutoffMode, CutoffRepository
from reva.evaluation.leakage import (
    RETROSPECTIVE_DISCLAIMER,
    RETROSPECTIVE_LABEL,
    GuardMode,
    LeakageGuard,
)
from reva.kernel.errors import CapabilityGap, ValidationProblem

SCHEMA = "reva-replay-manifest/1"
OPERATIONAL = "operational_as_known"
AVAILABLE_THEN = "available_then_reconstruction"
RETROSPECTIVE = RETROSPECTIVE_LABEL
REPLAY_MODES = (OPERATIONAL, AVAILABLE_THEN, RETROSPECTIVE)

HONESTY = {
    OPERATIONAL: (
        "Reproduces what REVA actually had published at the cutoff, from the original release manifest "
        "and its pinned inputs. No backfilled source version or later mapping is used."
    ),
    AVAILABLE_THEN: (
        "A later reconstruction from archived source vintages with independently evidenced availability "
        "at/before the cutoff; it is not what REVA actually believed. Algorithm-design date and source "
        "availability are reported separately."
    ),
    RETROSPECTIVE: (
        f"{RETROSPECTIVE_LABEL}: current revised historical data, temporally split; "
        f"{RETROSPECTIVE_DISCLAIMER}."
    ),
}


# --------------------------------------------------------------------------- helpers
def _plain(obj: Any) -> Any:
    """JSON-plain copy (datetimes ISO-8601, UUID/Decimal as strings)."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, (UUID, Decimal)):
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def _db_now(conn: Connection) -> datetime:
    return conn.execute(text("SELECT clock_timestamp()")).scalar_one()


def _check_cutoff(conn: Connection, cutoff: datetime) -> datetime:
    if not isinstance(cutoff, datetime) or cutoff.tzinfo is None:
        raise ValidationProblem("cutoff must be a timezone-aware datetime")
    now = _db_now(conn)
    if cutoff > now:
        # A cutoff in the future would make the replay depend on records that do
        # not exist yet; the manifest would not be reproducible.
        raise ValidationProblem("replay cutoff is in the future", cutoff=cutoff.isoformat(), now=now.isoformat())
    return now


def _base(mode: str, cutoff: datetime, replayed_at: datetime) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "mode": mode,
        "label": mode,
        "honesty_statement": HONESTY[mode],
        "cutoff": cutoff.isoformat(),
        "replayed_at": replayed_at.isoformat(),
        "llm_used": False,
        "status": "ok",
        "inputs": [],
        "exclusions": [],
        "outputs": [],
    }


def _availability_proof(row: dict[str, Any], *, operational: bool) -> dict[str, Any]:
    if row.get("source_available_at") is not None and row.get("availability_basis"):
        return {
            "kind": "source_availability",
            "source_available_at": _plain(row["source_available_at"]),
            "availability_basis": _plain(row["availability_basis"]),
        }
    if operational:
        return {
            "kind": "reva_first_commit",
            "recorded_at": _plain(row.get("recorded_at") or row.get("observation_recorded_at")),
            "note": "source availability unknown; the operational clock is REVA's own commit time",
        }
    return {"kind": "none"}


# --------------------------------------------------------------------------- 1. operational
def operational_as_known(
    conn: Connection,
    cutoff: datetime,
    *,
    origin: str | None = None,
    concepts: Sequence[str] | None = None,
    store: Any = None,
) -> dict[str, Any]:
    """What REVA actually had published at ``cutoff`` (original release, pinned inputs)."""
    now = _check_cutoff(conn, cutoff)
    m = _base(OPERATIONAL, cutoff, now)
    repo = CutoffRepository(conn, cutoff, CutoffMode.as_known, origins=[origin] if origin else ("live", "test_fixture"))
    rel = repo.release_at_cutoff(origin)
    if rel is None:
        m["status"] = "no_release_at_cutoff"
        m["detail"] = "no belief release had been published at/before the cutoff"
        m["origin"] = origin
        return m
    rows = repo.release_estimates(rel["id"])
    concept_set = set(concepts) if concepts else None
    filtered_out = 0
    outputs: dict[str, dict[str, Any]] = {}
    inputs: dict[str, dict[str, Any]] = {}
    for r in rows:
        if concept_set is not None and r["concept_id"] not in concept_set:
            filtered_out += 1
            continue
        eid = str(r["estimate_id"])
        out = outputs.setdefault(eid, {
            "estimate_id": eid,
            "subject_id": str(r["subject_id"]),
            "concept_id": r["concept_id"],
            "support_id": str(r["support_id"]),
            "temporal_extent": _plain(r["temporal_extent"]),
            "modality": r["modality"],
            "value": _plain(r["value"]),
            "uncertainty": _plain(r["uncertainty"]),
            "discrepancy_flags": _plain(r["discrepancy_flags"]),
            "input_observation_ids": [],
        })
        if r["observation_id"] is not None:
            oid = str(r["observation_id"])
            out["input_observation_ids"].append(oid)
            inputs[oid] = {
                "observation_id": oid,
                "logical_key": r["logical_key"],
                "revision_no": r["revision_no"],
                "source_key": r["source_key"],
                "recorded_at": _plain(r["observation_recorded_at"]),
                "retrieved_at": _plain(r["retrieved_at"]),
                "source_published_at": _plain(r["source_published_at"]),
                "availability_proof": _availability_proof(r, operational=True),
            }
    m["release"] = {
        "release_id": str(rel["id"]),
        "context_id": str(rel["context_id"]),
        "origin": rel["origin"],
        "sequence": int(rel["sequence"]),
        "published_at": _plain(rel["published_at"]),
        "knowledge_cutoff": _plain(rel["knowledge_cutoff"]),
        "method": rel["method"],
        "manifest_sha256": rel["manifest_sha256"],
        "ontology_manifest_sha256": rel["ontology_manifest_sha256"],
        "model_manifest_sha256": rel["model_manifest_sha256"],
    }
    m["origin"] = rel["origin"]
    m["outputs"] = sorted(outputs.values(), key=lambda o: (o["concept_id"], o["subject_id"], o["support_id"]))
    m["inputs"] = sorted(inputs.values(), key=lambda i: (i["logical_key"] or "", i["observation_id"]))
    if filtered_out:
        m["exclusions"] = [{"reason": "concept_not_requested", "count": filtered_out}]
    m["manifest_verified"] = _verify_release_manifest(store, rel["manifest_sha256"], rows) if store else None
    return m


def _verify_release_manifest(store: Any, sha: str, rows: list[dict[str, Any]]) -> bool:
    """The pinned release manifest lists exactly the pinned belief inputs."""
    doc = json.loads(store.get("belief", sha))
    pinned = {str(r["observation_id"]) for r in rows if r["observation_id"] is not None}
    return set(doc.get("input_observation_ids", [])) == pinned


# --------------------------------------------------------------------------- 2. available-then
def available_then_reconstruction(
    conn: Connection,
    cutoff: datetime,
    *,
    origins: Sequence[str] = ("live",),
    concepts: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Re-run source_select_v1 over observations with proven availability <= cutoff."""
    from reva.belief.selection import METHOD_KEY, METHOD_VERSION, select
    from reva.ontology.registry import load_registry

    now = _check_cutoff(conn, cutoff)
    m = _base(AVAILABLE_THEN, cutoff, now)
    repo = CutoffRepository(conn, cutoff, CutoffMode.available_then, origins=origins)
    scope = repo.observations(concepts)
    policy = load_registry().policies["selection"]
    prefs: dict[str, list[str]] = policy["preferences"]
    no_policy = sum(1 for r in scope.rows if not prefs.get(r["concept_id"]))
    if no_policy:
        scope.exclusions["no_published_selection_policy"] += no_policy
    selections = select(scope.rows, prefs)
    by_id = {str(r["id"]): r for r in scope.rows}
    inputs = []
    for s in selections:
        r = by_id[str(s.observation_id)]
        inputs.append({
            "observation_id": str(r["id"]),
            "logical_key": r["logical_key"],
            "revision_no": r["revision_no"],
            "source_key": r["source_key"],
            "recorded_at": _plain(r["recorded_at"]),
            "retrieved_at": _plain(r["retrieved_at"]),
            "source_published_at": _plain(r["source_published_at"]),
            "availability_proof": _availability_proof(r, operational=False),
            "eligibility_evaluated_at": _plain(r["eligibility_evaluated_at"]),
            "post_cutoff_flags": [f for f in ("retracted_after_cutoff", "ineligible_after_cutoff") if r.get(f)],
        })
    m["inputs"] = inputs
    m["outputs"] = [
        {
            "subject_id": str(s.subject_id),
            "concept_id": s.concept_id,
            "support_id": str(s.support_id),
            "modality": "selected_report",
            "observation_id": str(s.observation_id),
            "source_key": s.source_key,
            "value": _plain(s.value),
            "uncertainty": _plain(s.uncertainty),
            "alternatives": _plain(s.alternatives),
            "discrepancy_flags": list(s.discrepancy_flags),
        }
        for s in selections
    ]
    m["exclusions"] = scope.exclusion_report()
    m["post_cutoff_flags"] = scope.flag_report()
    m["excluded_without_proof"] = scope.exclusions.get("no_availability_proof", 0)
    m["candidate_revisions_with_proof"] = scope.candidate_revisions
    method_recorded = conn.execute(
        text("SELECT recorded_at FROM model.version WHERE model_key = :k AND version = :v"),
        {"k": METHOD_KEY, "v": METHOD_VERSION},
    ).scalar_one_or_none()
    avail = [r["source_available_at"] for r in (by_id[i["observation_id"]] for i in inputs)]
    elig_times = [r["eligibility_evaluated_at"] for r in scope.rows if r.get("eligibility_evaluated_at")]
    m["algorithm_design"] = {
        "method": f"{METHOD_KEY}@{METHOD_VERSION}",
        "method_registered_at": _plain(method_recorded),
        "algorithm_design_date": _plain(method_recorded),
        "selection_policy": policy["policy_version"],
        "selection_method_and_ontology_as_of": now.isoformat(),
        "design_date_note": ("the selection method and ontology are today's design; the design date is not the "
                             "availability date and not the cutoff"),
    }
    m["data_quality_decisions"] = {
        "retractions_as_of": cutoff.isoformat(),
        "eligibility_as_of": cutoff.isoformat(),
        "eligibility_rule": ("decision in force at the cutoff; where REVA had none yet for a (connector version, "
                             "concept), its first recorded decision (algorithm design) is used and dated"),
        "latest_eligibility_evaluated_at": _plain(max(elig_times)) if elig_times else None,
        "post_cutoff_knowledge": ("later retractions and eligibility changes are flagged on inputs "
                                  "(post_cutoff_flags), never used to exclude: excluding them would be hindsight"),
    }
    m["availability"] = {
        "earliest_source_available_at": _plain(min(avail)) if avail else None,
        "latest_source_available_at": _plain(max(avail)) if avail else None,
        "rule": "source_available_at <= cutoff with a recorded availability_basis",
    }
    m["origins"] = list(origins)
    if not selections:
        m["status"] = "no_available_inputs"
        m["detail"] = "no observation has proven availability at/before the cutoff"
    return m


# --------------------------------------------------------------------------- 3. retrospective
def _decimal(value: dict[str, Any] | None) -> float | None:
    if not value or value.get("kind") != "decimal":
        return None
    try:
        d = Decimal(str(value["value"]))
    except (InvalidOperation, KeyError):
        return None
    return float(d) if d.is_finite() else None


def _year(temporal: dict[str, Any]) -> int | None:
    start = (temporal or {}).get("start")
    if not start:
        return None
    try:
        return int(str(start)[:4])
    except ValueError:
        return None


def _default_models() -> list[Any]:
    from reva.models.forecasting import LastValue, LogDrift

    return [LastValue(), LogDrift()]  # the naive baseline is always present


DEFAULT_PUBLICATION_LAG_YEARS = 1
PUBLICATION_LAG_NOTE = (
    "Annual WDI/Comtrade values for reference year t are published months after t ends, so a value for t is "
    "assumed available only at 31 Dec of year t + publication_lag_years. A fold's information date is 31 Dec of "
    "origin_year; its training window ends at origin_year - publication_lag_years and the target is "
    "origin_year + horizon, i.e. horizon + lag steps after the last training observation. With lag 0, h=1 is "
    "partly a nowcast."
)


def _series_label(key: tuple[Any, ...]) -> str:
    return f"{key[0]}|{key[2]}|{key[3]}"


def revised_data_retrospective(
    conn: Connection,
    cutoff: datetime | None = None,
    *,
    origins: Sequence[str] = ("live",),
    concepts: Sequence[str] | None = None,
    min_train: int = 5,
    horizons: Sequence[int] = (1,),
    models: Sequence[Any] | None = None,
    publication_lag_years: int = DEFAULT_PUBLICATION_LAG_YEARS,
) -> dict[str, Any]:
    """Rolling-origin forecast evaluation on current revised data.

    ``cutoff`` fixes the data snapshot (latest revisions recorded at/before it;
    defaults to now) so the evaluation is reproducible. Every fold is labelled
    ``revised_data_retrospective``; inputs that a strict guard at the fold
    origin would block are counted, not hidden.

    ``publication_lag_years`` (default 1, documented in ``design``) is the
    assumed delay between the end of a reference year and the availability of
    its value: see :data:`PUBLICATION_LAG_NOTE`. Scores are reported per
    concept (pooled MAE, which mixes series of different scale and is labelled
    so), per series (MAE in the series' unit) and as a scale-free MASE
    (in-sample one-step naive scale of each fold's training window).
    """
    from reva.models.metrics import mase_scale, scaled_absolute_error

    if min_train < 1 or not horizons or any(h < 1 for h in horizons):
        raise ValidationProblem("min_train must be >= 1 and horizons positive")
    if isinstance(publication_lag_years, bool) or not isinstance(publication_lag_years, int) \
            or not 0 <= publication_lag_years <= 10:
        raise ValidationProblem("publication_lag_years must be an integer in [0, 10]",
                                publication_lag_years=str(publication_lag_years))
    lag = publication_lag_years
    now = _db_now(conn)
    snapshot = cutoff or now
    _check_cutoff(conn, snapshot)
    m = _base(RETROSPECTIVE, snapshot, now)
    m["disclaimer"] = RETROSPECTIVE_DISCLAIMER
    m["real_time_backtest"] = False
    repo = CutoffRepository(conn, snapshot, CutoffMode.revised_data_retrospective, origins=origins)
    scope = repo.observations(concepts)
    excl: Counter[str] = Counter()
    groups: dict[tuple[Any, ...], dict[int, dict[str, Any]]] = defaultdict(dict)
    dup: set[tuple[Any, ...]] = set()
    for r in scope.rows:
        v = _decimal(r["value"])
        y = _year(r["temporal_extent"])
        if v is None or y is None:
            excl["missing_or_non_decimal_value"] += 1
            continue
        key = (str(r["subject_id"]), r["concept_id"], r["source_key"], r["aggregation"],
               (r["temporal_extent"] or {}).get("kind"))
        if y in groups[key]:
            dup.add(key)
        groups[key][y] = {**r, "_v": v}
    models = list(models) if models is not None else _default_models()
    hs = sorted(set(int(h) for h in horizons))
    folds: list[dict[str, Any]] = []
    # Scores are pooled per concept only: errors in different units are never averaged together.
    per_model: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    per_series: dict[tuple[str, str, str], dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    undefined_scale: Counter[str] = Counter()
    ineligible: Counter[str] = Counter()
    flagged_inputs = 0
    series_used = 0
    for key in sorted(groups, key=lambda k: tuple(map(str, k))):
        if key in dup:
            excl["duplicate_period_in_series"] += 1
            continue
        pts = groups[key]
        years = sorted(pts)
        if any(b - a != 1 for a, b in itertools.pairwise(years)):
            excl["non_consecutive_series"] += 1
            continue
        if len(years) < min_train + 1:
            excl["series_shorter_than_min_train_plus_one"] += 1
            continue
        series_used += 1
        slabel = _series_label(key)
        for i in range(min_train - 1, len(years) - 1):
            last_train = years[i]
            origin_year = last_train + lag  # information date: 31 Dec of origin_year
            train_years = years[: i + 1]
            train = [(y, pts[y]["_v"]) for y in train_years]
            scale = mase_scale([v for _, v in train], train_years)
            # What a strict guard would say at the fold's information date: revised values
            # recorded long after it are admitted only under this label.
            origin_guard = LeakageGuard(datetime(origin_year, 12, 31, 23, 59, 59, tzinfo=UTC),
                                        GuardMode.revised_data_retrospective)
            for y in train_years:
                origin_guard.check(CutoffRepository.observation_input(pts[y]))
            n_flag = len(origin_guard.flagged())
            flagged_inputs += n_flag
            for h in hs:
                target = origin_year + h
                if target not in pts:
                    continue
                steps = target - last_train
                actual = pts[target]["_v"]
                first = _decimal(pts[target].get("first_release_value"))
                preds = []
                for mdl in models:
                    fitted = mdl.fit(train)
                    if not hasattr(fitted, "predict"):
                        ineligible[f"{mdl.key}:{getattr(fitted, 'reason', 'ineligible')}"] += 1
                        continue
                    point = float(fitted.predict([steps])[0].point)
                    ae = abs(point - actual)
                    rec: dict[str, Any] = {"model": f"{mdl.key}@{mdl.version}", "point": point,
                                           "abs_error_latest_revised": ae}
                    acc = per_model[(key[1], rec["model"])]
                    sacc = per_series[(key[1], slabel, rec["model"])]
                    acc["ae_latest"].append(ae)
                    sacc["ae_latest"].append(ae)
                    sae = scaled_absolute_error(point, actual, scale)
                    rec["scaled_abs_error_latest_revised"] = sae
                    if sae is None:
                        undefined_scale[scale.reason or "undefined"] += 1
                    else:
                        acc["sae_latest"].append(sae)
                        sacc["sae_latest"].append(sae)
                    if actual > 0:
                        acc["ape_latest"].append(ae / actual)
                    if first is not None:
                        rec["abs_error_first_release"] = abs(point - first)
                        acc["ae_first"].append(rec["abs_error_first_release"])
                        sacc["ae_first"].append(rec["abs_error_first_release"])
                    preds.append(rec)
                folds.append({
                    "label": RETROSPECTIVE_LABEL,
                    "series": {"subject_id": key[0], "concept_id": key[1], "source_key": key[2],
                               "aggregation": key[3]},
                    "origin_year": origin_year,
                    "information_date": f"{origin_year}-12-31",
                    "publication_lag_years": lag,
                    "last_train_year": last_train,
                    "target_year": target,
                    "horizon": h,
                    "steps_after_last_observation": steps,
                    "train_years": [years[0], last_train],
                    "mase_scale": scale.value,
                    "actual_latest_revised": actual,
                    "actual_first_release": first,
                    "inputs_recorded_after_origin": n_flag,
                    "predictions": preds,
                })

    def _mean(xs: list[float]) -> float | None:
        return sum(xs) / len(xs) if xs else None

    scores: dict[str, dict[str, Any]] = defaultdict(dict)
    for (concept, model), v in sorted(per_model.items()):
        scores[concept][model] = {
            "label": RETROSPECTIVE_LABEL,
            "n": len(v["ae_latest"]),
            "mae_latest_revised": _mean(v["ae_latest"]),
            "mae_pooling": "pooled across series of different scale (e.g. countries); see scores_by_series and "
                           "mase_latest_revised",
            "mase_latest_revised": _mean(v["sae_latest"]),
            "n_mase": len(v["sae_latest"]),
            "mape_latest_revised": _mean(v["ape_latest"]),
            "mae_first_release": _mean(v["ae_first"]),
            "n_first_release": len(v["ae_first"]),
        }
    by_series: dict[str, dict[str, dict[str, Any]]] = defaultdict(lambda: defaultdict(dict))
    for (concept, slabel, model), v in sorted(per_series.items()):
        by_series[concept][slabel][model] = {
            "label": RETROSPECTIVE_LABEL,
            "n": len(v["ae_latest"]),
            "mae_latest_revised": _mean(v["ae_latest"]),
            "mase_latest_revised": _mean(v["sae_latest"]),
            "mae_first_release": _mean(v["ae_first"]),
            "n_first_release": len(v["ae_first"]),
        }
    m["scores"] = dict(scores)
    m["scores_by_series"] = {c: {k: dict(v) for k, v in d.items()} for c, d in by_series.items()}
    m["scores_note"] = (
        "pooled per concept (one unit per concept), never across concepts. Pooled MAE mixes series of very "
        "different scale; compare models with the scale-free mase_latest_revised (in-sample one-step naive "
        "scale of each fold's training window) or per series in scores_by_series (series key: "
        "subject_id|source_key|aggregation)"
    )
    m["mase_undefined_scale"] = [{"reason": k, "count": v} for k, v in sorted(undefined_scale.items())]
    m["outputs"] = folds
    m["inputs"] = [{
        "observation_id": str(r["id"]), "logical_key": r["logical_key"], "revision_no": r["revision_no"],
        "recorded_at": _plain(r["recorded_at"]), "availability_proof": _availability_proof(r, operational=True),
    } for r in scope.rows]
    m["exclusions"] = [{"reason": k, "count": v} for k, v in sorted(excl.items())] + [
        {"reason": f"model_ineligible:{k}", "count": v} for k, v in sorted(ineligible.items())]
    m["design"] = {
        "split": "expanding-window rolling origins on annual reference years",
        "min_train": min_train,
        "horizons": hs,
        "publication_lag_years": lag,
        "publication_lag_note": PUBLICATION_LAG_NOTE,
        "models": [f"{x.key}@{x.version}" for x in models],
        "data_vintage_policy": "latest revision of each lineage recorded at/before the snapshot cutoff",
        "truth_vintage_policy": "latest revised value (primary); first release scored separately where recorded",
        "series_used": series_used,
        "inputs_recorded_after_fold_origin": flagged_inputs,
    }
    m["researcher_degrees_of_freedom"] = {
        "holdout": "none locked; the same revised history may have informed model design",
        "repeated_holdout_use": "possible; every rerun on this history reuses the same targets",
        "tuning": "no hyperparameter search performed in this replay",
    }
    if not folds:
        m["status"] = "insufficient_history"
    return m


# --------------------------------------------------------------------------- job handler
def _parse_cutoff(v: Any) -> datetime:
    if isinstance(v, datetime):
        dt = v
    elif isinstance(v, str):
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError as e:
            raise ValidationProblem("cutoff must be an ISO-8601 timestamp", cutoff=v) from e
    else:
        raise ValidationProblem("cutoff is required (ISO-8601 timestamp with time zone)")
    if dt.tzinfo is None:
        raise ValidationProblem("cutoff must carry a time zone", cutoff=str(v))
    return dt


def run_replay(conn: Connection, mode: str, cutoff: datetime, *, origin: str = "live",
               concepts: Sequence[str] | None = None, store: Any = None,
               options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Dispatch to exactly the requested mode; an unknown mode is a capability gap."""
    options = options or {}
    if mode == OPERATIONAL:
        return operational_as_known(conn, cutoff, origin=origin, concepts=concepts, store=store)
    if mode == AVAILABLE_THEN:
        return available_then_reconstruction(conn, cutoff, origins=[origin], concepts=concepts)
    if mode == RETROSPECTIVE:
        return revised_data_retrospective(
            conn, cutoff, origins=[origin], concepts=concepts,
            min_train=int(options.get("min_train", 5)), horizons=tuple(options.get("horizons", (1,))),
            publication_lag_years=options.get("publication_lag_years", DEFAULT_PUBLICATION_LAG_YEARS),
        )
    raise CapabilityGap(f"unknown replay mode {mode!r}", supported=list(REPLAY_MODES))


def handle_replay(payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Job handler: payload ``{mode, cutoff, concepts?, origin?, options?}``.

    Writes the replay manifest to the ``evaluation`` namespace, creates an
    evaluation-mode context and completes the job in the same transaction.
    """
    from reva.adapters.postgres import repositories as repo
    from reva.kernel.context import Origin, assert_publishable

    mode = payload.get("mode")
    if mode not in REPLAY_MODES:
        raise CapabilityGap(f"unknown replay mode {mode!r}", supported=list(REPLAY_MODES))
    cutoff = _parse_cutoff(payload.get("cutoff"))
    origin = payload.get("origin", "live")
    if origin not in ("live", "test_fixture"):
        raise ValidationProblem(f"invalid origin {origin!r}")
    assert_publishable(Origin(origin), ctx.settings.reva_env)
    concepts = payload.get("concepts")
    if concepts is not None and (not isinstance(concepts, list) or not all(isinstance(c, str) for c in concepts)):
        raise ValidationProblem("concepts must be a list of concept ids")
    with ctx.worker_engine.connect() as conn:
        manifest = run_replay(conn, mode, cutoff, origin=origin, concepts=concepts, store=ctx.store,
                              options=payload.get("options") or {})
    manifest["job_id"] = str(ctx.job_id)
    with ctx.worker_engine.begin() as conn:
        ctx.assert_lease(conn)
        sha = repo.store_json_artifact(conn, ctx.store, manifest, "evaluation", schema_version="replay-manifest/1")
        act = repo.simple_activity("evaluation.replay", "evaluation", f"worker:{ctx.worker_id}",
                                   {"mode": mode, "cutoff": cutoff.isoformat(), "origin": origin,
                                    "concepts": concepts, "job_id": str(ctx.job_id)})
        pinned = []
        parent = None
        if mode == OPERATIONAL and manifest.get("release"):
            pinned = [(manifest["release"]["manifest_sha256"], {"role": "pinned_release_manifest"})]
            parent = UUID(manifest["release"]["context_id"])
        repo.record_activity(conn, act, inputs=pinned, outputs=[sha])
        context_id = repo.create_context(conn, mode="evaluation", origin=origin, manifest_sha256=sha,
                                         knowledge_cutoff=cutoff, parent_context_id=parent)
        result = {
            "status": manifest["status"],
            "mode": mode,
            "label": manifest["label"],
            "honesty_statement": manifest["honesty_statement"],
            "manifest_sha256": sha,
            "context_id": str(context_id),
            "cutoff": cutoff.isoformat(),
            "inputs": len(manifest["inputs"]),
            "outputs": len(manifest["outputs"]),
            **({"disclaimer": manifest["disclaimer"]} if "disclaimer" in manifest else {}),
        }
        repo.emit_outbox(conn, "evaluation.replay_completed", {"job_id": str(ctx.job_id), **result})
        ctx.complete_in(conn, "succeeded", result)
    return result
