"""V7 BEAT-ENERGY: sealed prospective forecasts for the pre-registered winners only (agent BEAT-ENERGY).

Winners (``registry/forecasts/beat_energy/protocol.yaml``, run ``docs/dev/v7_beat_energy_result.json``): W1 h = 0 for
Brent ``BREPUUS`` and WTI ``WTIPUUS`` — the monthly-mean nowcast of the CURRENT month from EIA daily spot prices, 1-4
weeks after an STEO release (timeliness win). Henry Hub h = 0 and every h >= 1 did not survive Holm: not issued.

The batch asks one quantity question per series on the issue month of the STEO issue in force (reference period
= that month, ``cycle_horizon`` 0 of the quarterly cycle) and freezes, besides the three winners, the two
benchmarks of the pre-registered comparison: EIA's STEO point (attributed) and STEO's fair log-normal density.
The forecasters are exactly the registered code (:mod:`reva.evaluation.beat_energy`, offset 7 days: daily prices
up to the seal day minus ``DAILY_LAG_DAYS``), placed at the seal instant. Resolution and scoring run unchanged
through ``registry.service.resolve_due`` (frozen family docs, pinned tables listed under ``data``). The
database entry point (``seal``) lives in :mod:`reva.cli.beat_energy` (invariant 11).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
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
LICENCE_REVIEW = "input daily prices are Refinitiv (LSEG) data republished by EIA (atlas.eia.spot_d licence_review)"
REDISTRIBUTION = "internal only: redistribution pending licence review (owner decision, Refinitiv/LSEG via EIA)"
NOWCAST_FAMILIES = {"BREPUUS": "eia.brent_monthly_nowcast", "WTIPUUS": "eia.wti_monthly_nowcast"}
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
    if now < inforce.available_at + timedelta(days=min(be.OFFSETS)):
        raise ValueError("less than 7 days after the STEO issue's availability: outside the registered window")
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
            )
        )
    recs = be.w1_forecasts(rows, daily)
    out = {r["code"]: r for r in recs if r["t"] == now.isoformat()}
    out["_inforce"] = {"vintage": inforce.vintage, "release": inforce.release_text}
    return out


def find_questions(batches: Sequence[Mapping[str, Any]], target: str) -> dict[str, tuple[Mapping[str, Any], Any]]:
    """Per winning family: the first sealed (non-supplement) batch holding a quantity question on ``target``.

    The registry asks monthly questions only from h = 1 of a quarterly cycle, so the issue month of a quarterly STEO
    issue is found in the PREVIOUS cycle's batches (e.g. 2027-01 = horizon 3 of cycle 2026-Q4)."""
    out: dict[str, tuple[Mapping[str, Any], Any]] = {}
    for man in batches:
        if man.get("supplement_of"):
            continue
        for q in man.get("questions") or []:
            f = q["family"]
            if f in WIN_CODES.values() and f not in out and q["type"] == "quantity" and q["reference_period"] == target:
                out[f] = (man, q)
    return out


def build_batch(
    original: Mapping[str, Any],
    questions: Mapping[str, Mapping[str, Any]],
    recs: Mapping[str, Mapping[str, Any]],
    steo_cells: Mapping[str, Mapping[str, Any]],
    *,
    now: datetime,
) -> dict[str, Any]:
    """Supplementary sealed batch on existing registry questions (same question records and pinned tables as the
    original batch; the daily table and the STEO archive version used are listed in ``evidence``)."""
    from reva.evaluation.registry.energy import to_predictive
    from reva.evaluation.registry.forecasts import _record, abstention
    from reva.evaluation.registry.service import BATCH_SCHEMA
    from reva.models.forecasting.registry_baselines import Predictive

    forecasts: list[dict[str, Any]] = []
    qs: list[Mapping[str, Any]] = []
    for code, fam_key in WIN_CODES.items():
        if fam_key not in questions:
            continue
        q = questions[fam_key]
        r = recs[code]
        if q["reference_period"] != r["target"]:
            raise ValueError(f"question period {q['reference_period']} != nowcast target {r['target']}")
        qs.append(q)
        detail = (
            ("steo_issue", str(recs["_inforce"]["vintage"])),
            ("steo_release_date", str(recs["_inforce"]["release"])),
            ("daily_information", f"EIA daily spot {be.SPOT_SERIES[code]}, days <= seal day - {be.DAILY_LAG_DAYS}"),
            ("horizon_months_after_issue", "0"),
            ("past_errors", str(r["n_past"])),
            ("protocol", be.PROTOCOL_PATH),
        )
        for m, mk in KEY_OF.items():
            v = r.get(m)
            if v is None:
                forecasts.append(abstention(q, mk, f"fewer than {be.MIN_PAST} past errors at this offset"))
                continue
            p = to_predictive(v, detail) if isinstance(v, em.Pred) else Predictive("point", float(v), detail=detail)
            rec = _record(q, mk, p, [])
            if m == "steo":
                rec["attribution"] = (
                    f"U.S. Energy Information Administration, Short-Term Energy Outlook {recs['_inforce']['vintage']} "
                    "(projection), as pinned at issue"
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
            if m != "steo":
                rec["licence_review"] = LICENCE_REVIEW
                rec["redistribution"] = REDISTRIBUTION
            forecasts.append(rec)
    return {
        "schema": BATCH_SCHEMA,
        "label": "V7 BEAT-ENERGY sealed winners (pre-registered W1 timeliness nowcasts) + benchmarks",
        "origin": original["origin"],
        "cycle": original["cycle"],
        "issued_at": now.isoformat(),
        "replay_mode": "operational_as_known",
        "supplement_of": original.get("_sha256"),
        "statement": (
            "Supplementary sealed batch: only the Holm winners of registry/forecasts/beat_energy/protocol.yaml (W1 "
            "h0: Brent, WTI monthly mean of the STEO issue month, 1-4 weeks after the release) with the "
            "pre-registered benchmarks (EIA STEO point, STEO fair log-normal), on the original batch's questions and "
            "pinned tables. The reference month is partly elapsed at issue (timeliness)."
        ),
        "specs": original["specs"],
        "families": {k: v for k, v in original["families"].items() if k in questions},
        "models": [{"model_key": k, "version": v, "description": d} for k, (v, d) in MODELS.items()],
        "data": original["data"],
        "evidence": {"beat_energy_inputs": recs.get("_pins")},
        "code_digest": original.get("code_digest"),
        "questions": qs,
        "forecasts": forecasts,
        "generation": {"skipped": [], "pending_families": [], "notes": []},
    }


def nowcast_questions(
    fam_docs: Mapping[str, Mapping[str, Any]],
    series_by_family: Mapping[str, Any],
    pin: Mapping[str, Any],
    *,
    now: datetime,
    cycle: str,
) -> dict[str, dict[str, Any]]:
    """Questions of the h = 0 nowcast families (one per series) for the cycle anchor month at ``now``."""
    from reva.evaluation.registry.questions import generate_questions
    from reva.evaluation.registry.service import _family_of

    out: dict[str, dict[str, Any]] = {}
    for fam_key in NOWCAST_FAMILIES.values():
        fam = _family_of(fam_docs[fam_key])
        if not fam.raw.get("nowcast"):
            raise ValueError(f"{fam_key} is not declared a nowcast family")
        qs, skipped = generate_questions(
            fam, series_by_family[fam_key], cycle=cycle, interval_level=0.8, source_pin=pin, issued_on=now.date()
        )
        if len(qs) != 1:
            raise LookupError(f"{fam_key}: expected one question, got {len(qs)} ({skipped})")
        out[fam_key] = qs[0]
    return out


def nowcast_batch(
    fam_docs: Mapping[str, Mapping[str, Any]],
    questions: Mapping[str, Mapping[str, Any]],
    recs: Mapping[str, Mapping[str, Any]],
    steo_cells: Mapping[str, Mapping[str, Any]],
    *,
    now: datetime,
    cycle: str,
    specs: Mapping[str, Any],
    tables: Mapping[str, Mapping[str, Any]],
    code_digest: str | None,
) -> dict[str, Any]:
    """An ORIGINAL sealed batch of the nowcast families: questions + winners + pre-registered benchmarks."""
    by_fam = {NOWCAST_FAMILIES[c]: q for c, q in ((c, questions[f]) for c, f in NOWCAST_FAMILIES.items())}
    orig = {"origin": "live", "cycle": cycle, "specs": specs, "families": fam_docs,
            "data": {"selection_rule": "per dataset_key: latest evidence.dataset version at issued_at",
                     "tables": dict(tables)}, "code_digest": code_digest}  # fmt: skip
    # same forecasts as a supplement, keyed on the nowcast families' questions
    remap = {code: by_fam[f] for code, f in NOWCAST_FAMILIES.items()}
    b = build_batch(orig, {WIN_CODES[c]: q for c, q in remap.items()}, recs, steo_cells, now=now)
    b["families"] = dict(fam_docs)
    b.pop("supplement_of", None)
    b["statement"] = (
        "Original sealed batch of the V7 BEAT-ENERGY current-month nowcast families (declared nowcasts, h = 0): only "
        "the Holm winners of registry/forecasts/beat_energy/protocol.yaml (W1 h0 Brent, WTI) with the "
        "pre-registered benchmarks (EIA STEO point, STEO fair log-normal), issued inside the registered window "
        "(7-35 days after the STEO issue in force, same month). The reference month is partly elapsed at issue. "
        + REDISTRIBUTION
    )
    return b


__all__ = [
    "KEY_OF",
    "MODELS",
    "NOWCAST_FAMILIES",
    "WIN_CODES",
    "build_batch",
    "find_questions",
    "nowcast_batch",
    "nowcast_questions",
    "seal_records",
]
