"""One reproducible event-study / local-projection run: request -> inputs (pinned snapshot, event set) -> estimates
-> run document (archived) -> optional REVA-run effect estimates (through PRED-EFFECT's loader).

Reproducibility: the run id is a hash of the request, the snapshot sha256, the outcome panel digest, the event-set
digest and the code digest (sha256 of every ``src/reva/estimation/*.py`` file plus the two registry files). The
same inputs always give the same run id and the same numbers (no hidden randomness; the permutation placebo uses
the request's seed). The run document records all of them, and the git commit when available.

As-of: the outcome panel comes from one pinned atlas snapshot (knowledge cutoff recorded). With ``as_of`` the run
also ignores KG statements recorded after it, onsets after it, and outcome cells for years not finished by it.
Without a vintage accessor (PRED-VINTAGE) the outcome values are the snapshot's final vintages: such a run is
labelled ``final_vintage_pseudo_real_time`` and must never be reported as real-time skill.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from reva.estimation.core import (
    EstimationError,
    Grid,
    LhsMode,
    LPSpec,
    event_study,
    local_projection,
    permutation_placebo,
)
from reva.estimation.data import (
    TREATMENTS_PATH,
    EventSet,
    PanelData,
    SnapshotPanels,
    atlas_onsets,
    canonical_sha256,
    kg_onsets,
    load_treatments,
    treatment_countries,
)
from reva.estimation.replication import REPLICATIONS_PATH
from reva.kernel.errors import ValidationProblem

RUN_SCHEMA = "reva-eventstudy-run/1"
SOURCE_KEY = "reva.eventstudy"
PKG_DIR = Path(__file__).resolve().parent

CAVEATS = [
    "Association, not established causation: the estimate is the average change in the outcome after coded event "
    "onsets relative to country-years without an onset in the same year, conditional on the stated controls and "
    "fixed effects. It is causal only under the stated identifying assumptions (onset timing as good as random "
    "given controls, no anticipation, parallel pre-trends, no other shock coinciding with onsets), which REVA "
    "does not establish.",
    "Events are selected and dated by the coding dataset; selection on severity (e.g. deaths) mixes the event "
    "with its damage.",
    "Country-years outside the event dataset's coverage are excluded, never treated as 'no event'.",
    "Final-vintage data: revised figures as published in the pinned snapshot, not what was known at the time "
    "(unless a vintage accessor is used).",
    "Not a forecast for a particular country, and not advice.",
]
CAVEATS_FR = [
    "Association, pas une causalité établie : l'estimation est la variation moyenne du résultat après le début "
    "d'événements codés, comparée aux pays-années sans début d'événement la même année, conditionnellement aux "
    "contrôles et effets fixes déclarés. Elle n'est causale que sous les hypothèses d'identification déclarées, "
    "que REVA n'établit pas.",
    "Les événements sont sélectionnés et datés par le jeu de données qui les code.",
    "Données en dernier millésime (révisées), pas telles que connues à l'époque.",
    "Ni une prévision pour un pays donné, ni un conseil.",
]


class VintageAccessor(Protocol):
    """The outcome panel as it was published at ``as_of`` (implemented over PRED-VINTAGE by
    :class:`PredVintagePanels`)."""

    def panel(self, outcome: str, as_of: date, frequency: str) -> PanelData: ...


class PredVintagePanels:
    """Outcome panels from PRED-VINTAGE's accessor (``reva.evaluation.vintages.VintageAccessor``, e.g.
    ``CutoffRepository(conn, cutoff=..., mode="available_then").vintages(store)``): outcome refs are
    ``vintage:<family>.<variable>`` (``registry`` of families/variables in
    ``reva.adapters.connectors.vintage_common``); for every (country, period) the value of the latest vintage
    admissible at the accessor's cutoff; projections are never used as observations."""

    def __init__(self, accessor: Any, snapshot_sha256: str = "vintage"):
        self.accessor = accessor
        self.snapshot_sha256 = snapshot_sha256

    def panel(self, outcome: str, as_of: date, frequency: str) -> PanelData:
        kind, _, ident = outcome.partition(":")
        if kind != "vintage" or "." not in ident:
            raise ValidationProblem("vintage outcomes are 'vintage:<family>.<variable>'", outcome=outcome)
        cut = self.accessor.cutoff.date()
        if cut > as_of:
            raise ValidationProblem("the vintage accessor's cutoff is after the run's as_of", cutoff=str(cut))
        family, variable = ident.split(".", 1)
        scope = self.accessor.latest(family=family, variable=variable)
        values: dict[tuple[str, int], float] = {}
        rows = []
        for r in scope.as_records():
            iso3, ps, pe, v = r.get("iso3"), r["period_start"], r["period_end"], r.get("value")
            if iso3 is None or v is None:
                continue
            days = (pe - ps).days
            if frequency == "annual" and ps.month == 1 and ps.day == 1 and 365 <= days <= 366:
                key = (str(iso3), ps.year)
            elif frequency == "quarter" and ps.day == 1 and ps.month in (1, 4, 7, 10) and 89 <= days <= 92:
                key = (str(iso3), 4 * ps.year + (ps.month - 1) // 3)
            else:
                continue
            try:
                values[key] = float(v)
            except ValueError:
                continue
            rows.append([key[0], key[1], v, r.get("vintage"), r.get("raw_sha256")])
        return PanelData(
            ref=outcome,
            snapshot_sha256=self.snapshot_sha256,
            values=values,
            unit=None,
            series=[
                {
                    "series": outcome,
                    "cells": len(values),
                    "vintage_mode": scope.mode,
                    "cutoff": scope.cutoff.isoformat(),
                    "exclusions": scope.exclusion_report(),
                }
            ],
            dropped={},
            digest=canonical_sha256(sorted(rows)),
            label=variable,
        )


@dataclass(frozen=True)
class EventStudyRequest:
    treatment: str
    outcome: str
    horizons: int = 5
    lags: int = 2
    leads: int = 3
    lhs: LhsMode = "cumsum"
    universe: str = "all"
    frequency: Literal["annual", "quarter"] = "annual"
    start: int | None = None
    end: int | None = None
    as_of: date | None = None
    seed: int = 20261017
    n_perm: int = 200
    origin: str = "live"
    future_onsets: bool = True
    country_fe: bool = True
    year_fe: bool = True

    def canonical(self) -> dict[str, Any]:
        d = asdict(self)
        d["as_of"] = self.as_of.isoformat() if self.as_of else None
        return d

    def validate(self) -> None:
        if not 0 <= self.horizons <= 20:
            raise ValidationProblem("horizons must be 0..20")
        if not 0 <= self.lags <= 8 or not 2 <= self.leads <= 10:
            raise ValidationProblem("lags must be 0..8 and leads 2..10")
        if self.lhs not in ("level", "diff", "logdiff", "cumsum"):
            raise ValidationProblem("lhs must be level, diff, logdiff or cumsum")
        if not 0 <= self.n_perm <= 2000:
            raise ValidationProblem("n_perm must be 0..2000")


def git_head(start: Path) -> str | None:
    """The checked-out commit, read from ``.git`` files (no subprocess); ``None`` outside a git checkout."""
    for d in (start, *start.parents):
        g = d / ".git"
        if not g.is_dir():
            continue
        try:
            head = (g / "HEAD").read_text().strip()
            if not head.startswith("ref: "):
                return head or None
            ref = head[5:]
            p = g / ref
            if p.exists():
                return p.read_text().strip() or None
            packed = g / "packed-refs"
            if packed.exists():
                for line in packed.read_text().splitlines():
                    if line.endswith(" " + ref):
                        return line.split(" ", 1)[0]
        except OSError:
            return None
        return None
    return None


def code_digest() -> dict[str, Any]:
    files = [*sorted(PKG_DIR.glob("*.py")), TREATMENTS_PATH, REPLICATIONS_PATH]
    h = hashlib.sha256()
    for f in files:
        h.update(f.name.encode() + b"\0" + f.read_bytes() + b"\0")
    commit = git_head(PKG_DIR)
    return {"code_sha256": h.hexdigest(), "git_commit": commit, "files": [f.name for f in files]}


def _quarter_onsets(events: EventSet) -> tuple[dict[tuple[str, int], int], int]:
    out: dict[tuple[str, int], int] = {}
    dropped = 0
    for o in events.onsets:
        if o.precision is None or o.precision < 10:
            dropped += 1
            continue
        sign = -1 if o.date.startswith("-") else 1
        ys, ms = o.date.lstrip("+-").split("-")[:2]
        y, m = sign * int(ys), int(ms)
        k = (o.iso3, 4 * y + (m - 1) // 3)
        out[k] = out.get(k, 0) + 1
    return out, dropped


def run_event_study(
    req: EventStudyRequest,
    panels: SnapshotPanels,
    *,
    conn: Any | None = None,
    vintage: VintageAccessor | None = None,
    replication_report: Mapping[str, Any] | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run one event study; returns the (JSON-able) run document. ``conn`` is needed for ``source: kg``
    treatments. ``replication_report`` (from :func:`reva.estimation.replication.run_replications` on the same
    snapshot) is recorded; the caller enforces the gate before writing estimates."""
    req.validate()
    say = progress or (lambda _m: None)
    treatments = load_treatments()
    if req.treatment not in treatments:
        raise ValidationProblem("unknown treatment", treatment=req.treatment, known=sorted(treatments))
    t = treatments[req.treatment]
    as_of_dt = datetime.combine(req.as_of, datetime.max.time(), tzinfo=UTC) if req.as_of else None
    say(f"treatment {t.id} ({t.source})")
    if t.source == "kg":
        if conn is None:
            raise ValidationProblem("a database connection is needed for KG treatments")
        events = kg_onsets(conn, t, origin=req.origin, as_of=as_of_dt)
    else:
        events = atlas_onsets(panels, t)
    if req.as_of:
        events.onsets = [o for o in events.onsets if o.date <= req.as_of.isoformat()]
    say(f"outcome {req.outcome}")
    if vintage is not None and req.as_of is not None:
        panel = vintage.panel(req.outcome, req.as_of, req.frequency)
        vintage_mode = "vintage_as_published"
    else:
        panel = panels.outcome(req.outcome, req.frequency)
        vintage_mode = "final_vintage_pseudo_real_time"
    values = dict(panel.values)
    if req.frequency == "annual":
        onsets = events.counts()
        dropped_precision = 0
        if req.as_of:
            values = {k: v for k, v in values.items() if k[1] < req.as_of.year}
    else:
        onsets, dropped_precision = _quarter_onsets(events)
        if req.as_of:
            qa = 4 * req.as_of.year + (req.as_of.month - 1) // 3
            values = {k: v for k, v in values.items() if k[1] < qa}
    if not values:
        raise EstimationError("empty outcome panel")
    ys = [k[1] for k in values]
    lo, hi = min(ys), max(ys)
    cov = t.coverage_years if req.frequency == "annual" else (4 * t.coverage_years[0], 4 * t.coverage_years[1] + 3)
    if req.start is not None:
        lo = max(lo, req.start if req.frequency == "annual" else 4 * req.start)
    if req.end is not None:
        hi = min(hi, req.end if req.frequency == "annual" else 4 * req.end + 3)
    grid = Grid.build(
        values,
        onsets,
        years=(lo, hi),
        treat_years=cov,
        treat_countries=treatment_countries(events, panel, req.universe),
    )
    spec = LPSpec(
        horizons=req.horizons,
        leads=req.leads,
        lags=req.lags,
        lhs=req.lhs,
        country_fe=req.country_fe,
        year_fe=req.year_fe,
        future_onsets=req.future_onsets,
    )
    say("local projections")
    lp = local_projection(grid, spec)
    say("event study (pre-trend test)")
    es_levels = req.lhs if req.lhs in ("level", "diff") else None
    es: dict[str, Any] | None
    try:
        g_es = grid
        if es_levels is None and req.lhs == "logdiff":
            import numpy as np

            with np.errstate(divide="ignore", invalid="ignore"):
                ly = np.where(grid.y > 0, 100.0 * np.log(np.where(grid.y > 0, grid.y, 1.0)), np.nan)
            g_es = Grid(grid.countries, grid.years, ly, grid.treat, grid.at_risk)
        es = event_study(g_es, leads=req.leads, lags=req.horizons)
        es["outcome_in_regression"] = {
            "cumsum": "growth rate (coefficients are effects on the growth rate in each period)",
            "logdiff": "100 x log level",
            "diff": "level",
            "level": "level",
        }[req.lhs]
    except EstimationError as e:
        es = {"error": str(e)}
    placebo = None
    if req.n_perm > 0:
        say(f"permutation placebo ({req.n_perm} draws, seed {req.seed})")
        placebo = permutation_placebo(grid, spec, lp, n_perm=req.n_perm, seed=req.seed)
    sample_countries = sorted(
        {grid.countries[i] for i in range(len(grid.countries)) if grid.at_risk[i].any() and (grid.treat[i] > 0).any()}
    )
    code = code_digest()
    inputs = {
        "snapshot": panels.describe(),
        "outcome": panel.summary(),
        "events": events.summary(),
        "vintage_mode": vintage_mode,
        "onsets_dropped_for_precision": dropped_precision,
    }
    run_key = {
        "request": req.canonical(),
        "snapshot": panels.snapshot_sha256,
        "panel_digest": panel.digest,
        "event_set_digest": events.digest,
        "code_sha256": code["code_sha256"],
    }
    spec_sha = canonical_sha256(run_key)
    run_id = f"es-{spec_sha[:20]}"
    restricted = _restricted_sources(panel, events)
    doc: dict[str, Any] = {
        "schema": RUN_SCHEMA,
        "run_id": run_id,
        "spec_sha256": spec_sha,
        "method": "local_projection",
        "request": req.canonical(),
        "code": code,
        "inputs": inputs,
        "results": {
            "local_projection": lp.as_dict(),
            "event_study": es,
            "permutation_placebo": placebo,
            "treated_countries_in_sample": sample_countries,
        },
        "interpretation": interpret(lp, es, placebo, req),
        "replication_gate": _gate_summary(replication_report),
        "caveats": CAVEATS,
        "caveats_fr": CAVEATS_FR,
        "rights": {
            "restricted_sources": restricted,
            "non_commercial": any(r.get("non_commercial") for r in restricted),
        },
        "as_of": (req.as_of or _cutoff_date(panels.cutoff)).isoformat(),
    }
    doc["result_sha256"] = canonical_sha256(doc["results"])
    return doc


def _cutoff_date(cutoff: str) -> date:
    return datetime.fromisoformat(cutoff).date()


def _restricted_sources(panel: PanelData, events: EventSet) -> list[dict[str, Any]]:
    out = []
    for s in panel.series:
        r = s.get("rights") or {}
        if r.get("restricted") or r.get("non_commercial"):
            out.append({"series": s["series"], **r})
    note = events.treatment.notes.lower()
    if "non-commercial" in note or "not an open licence" in note:
        out.append(
            {
                "treatment": events.treatment.id,
                "note": events.treatment.notes,
                "restricted": True,
                "non_commercial": "non-commercial" in note,
            }
        )
    return out


def _gate_summary(rep: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if rep is None:
        return None
    return {
        "gate_open": rep.get("gate_open"),
        "passed": rep.get("passed"),
        "failed": rep.get("failed"),
        "registry_sha256": rep.get("registry_sha256"),
        "digest": rep.get("digest"),
        "snapshot_sha256": (rep.get("snapshot") or {}).get("snapshot_sha256"),
    }


def interpret(
    lp: Any, es: Mapping[str, Any] | None, placebo: Mapping[str, Any] | None, req: EventStudyRequest
) -> dict[str, Any]:
    """Plain flags (no prose verdict of causality): significance at 5 %, pre-trend test, placebo p-values."""
    sig = [e.h for e in lp.irf if e.ci_low > 0 or e.ci_high < 0]
    pre = (es or {}).get("pretrend_test") if isinstance(es, Mapping) else None
    lead_sig = [e.h for e in lp.leads if e.ci_low > 0 or e.ci_high < 0]
    perm = [h["h"] for h in (placebo or {}).get("horizons", []) if h.get("perm_p") is not None and h["perm_p"] < 0.05]
    flags = []
    if pre and pre.get("p") is not None and pre["p"] < 0.10:
        flags.append("pretrend_test_rejects_at_10pct")
    if lead_sig:
        flags.append("placebo_lead_significant")
    if not sig:
        flags.append("no_horizon_significant_at_5pct")
    return {
        "horizons_significant_5pct": sig,
        "placebo_leads_significant_5pct": lead_sig,
        "pretrend_p": pre.get("p") if pre else None,
        "horizons_perm_p_below_5pct": perm,
        "flags": flags,
        "unit_note": {
            "cumsum": "cumulative sum of the outcome (growth rate, %) from t to t+h: approx. % change in the level",
            "diff": "change of the outcome level from t-1 to t+h (outcome units, e.g. percentage points)",
            "logdiff": "100 x log change of the outcome from t-1 to t+h (approx. %)",
            "level": "outcome level at t+h (outcome units)",
        }[req.lhs],
    }


# --------------------------------------------------------------------------- persistence
def archive_run(engine: Any, store: Any, doc: Mapping[str, Any]) -> str:
    """Store the run document as a JSON artifact (namespace ``evaluation``) and register it; returns its sha256."""
    from reva.adapters.postgres import repositories as repo

    rights = {
        "licence": "internal-derived",
        "derived_from_restricted": bool(doc["rights"]["restricted_sources"]),
        "non_commercial": bool(doc["rights"]["non_commercial"]),
    }
    with engine.begin() as conn:
        return str(
            repo.store_json_artifact(conn, store, dict(doc), "evaluation", schema_version=RUN_SCHEMA, rights=rights)
        )


EFFECT_TRANSFORM = {"cumsum": "percent_change", "logdiff": "percent_change", "diff": "pp_change", "level": "level"}


def effect_estimates(doc: Mapping[str, Any], *, headline_h: int | None = None) -> list[dict[str, Any]]:
    """The REVA-run effect estimate(s) of a run, as keyword dicts for ``reva.effects.EffectEstimateIn``
    (docs/dev/v5_effect_interface.md). One estimate per run: the response at the last horizon, with the full
    lag profile in ``irf``. The outcome must be an atlas concept (``concept:<id>``)."""
    req = doc["request"]
    if not str(req["outcome"]).startswith("concept:"):
        raise ValidationProblem("effect estimates need an atlas concept outcome (concept:<id>)")
    lp = doc["results"]["local_projection"]
    irf = lp["irf"]
    h = headline_h if headline_h is not None else irf[-1]["h"]
    head = next(e for e in irf if e["h"] == h)
    tr = doc["inputs"]["events"]["treatment"]
    out_s = doc["inputs"]["outcome"]
    unit_time = "year" if req["frequency"] == "annual" else "quarter"
    lhs = req["lhs"]
    unit = "percentage_points" if lhs in ("diff", "level") else "percent"
    point = round(float(head["point"]), 6)
    sign = "negative" if point < 0 else ("positive" if point > 0 else "zero")
    ev = doc["inputs"]["events"]
    sample = lp["sample"]
    return [
        {
            "estimate_id": f"h{h}",
            "cause_kind": "event_type",
            "cause_id": tr["event_type"],
            "cause_definition": tr["definition"],
            "cause_transform": "occurrence",
            "effect_kind": "atlas_concept",
            "effect_id": str(req["outcome"]).split(":", 1)[1],
            "effect_definition": f"{out_s.get('label') or ''}: {out_s.get('definition') or ''} -- REVA LHS: "
            + doc["interpretation"]["unit_note"],
            "effect_transform": EFFECT_TRANSFORM[lhs],
            "measure": "response",
            "unit": unit,
            "shock_size": 1,
            "shock_unit": "occurrence",
            "sign": sign,
            "point": point,
            "point_kind": "point",
            "as_printed": repr(point),
            "se": round(float(head["se"]), 6),
            "ci_low": round(float(head["ci_low"]), 6),
            "ci_high": round(float(head["ci_high"]), 6),
            "ci_level": 0.95,
            "interval_kind": "confidence",
            "uncertainty_status": "reported",
            "horizon": {"kind": "at_horizon", "value": h, "unit": unit_time},
            "irf": [
                {
                    "h": e["h"],
                    "unit": unit_time,
                    "point": round(float(e["point"]), 6),
                    "se": round(float(e["se"]), 6),
                    "ci_low": round(float(e["ci_low"]), 6),
                    "ci_high": round(float(e["ci_high"]), 6),
                }
                for e in irf
            ],
            "scope": {
                "countries": doc["results"]["treated_countries_in_sample"],
                "country_group": "treated countries listed; controls: every country-year of the outcome panel at risk",
                "period": {"from": str(sample["years"][0]), "to": str(sample["years"][1])},
                "frequency": req["frequency"],
                "n_obs": head["nobs"],
                "n_countries": head["n_clusters"],
            },
            "method": "local_projection",
            "identification": (
                f"Jordà local projections, horizons 0..{req['horizons']}; treatment = onset of {tr['id']} "
                f"({ev['onsets']} onsets, {head['n_events']} in the horizon-{h} sample); controls: {req['lags']} lags of the outcome change and of the treatment, "
                "onsets inside the projection window (Teulings-Zubanov), country and year fixed effects; SE clustered "
                f"by country; pre-trend test p = {doc['interpretation']['pretrend_p']}; permutation placebo seed "
                f"{req['seed']}. Assumes onset timing as good as random given these controls (not established)."
            ),
            "run": {
                "run_id": doc["run_id"],
                "code_version": doc["code"]["git_commit"] or doc["code"]["code_sha256"],
                "data_snapshot": f"atlas:{doc['inputs']['snapshot']['snapshot_sha256']}",
                "as_of": doc["as_of"],
                "spec_sha256": doc["spec_sha256"],
                "replicates": [],
            },
            "as_of": doc["as_of"],
            "applicability": (
                "REVA note: average association across the sampled countries and years "
                f"({sample['years'][0]}-{sample['years'][1]}); not a forecast for a given country; "
                + " ".join(CAVEATS[:1])
            ),
            "causal_claim_keys": [],
        }
    ]


def build_effect_inputs(doc: Mapping[str, Any]) -> list[Any]:
    """``reva.effects.EffectEstimateIn`` objects for a run document (PRED-EFFECT's data model)."""
    from reva import effects as fx

    items = []
    for kw in effect_estimates(doc):
        kw = dict(kw)
        unc = fx.Uncertainty(
            se=kw.pop("se"),
            ci_low=kw.pop("ci_low"),
            ci_high=kw.pop("ci_high"),
            ci_level=kw.pop("ci_level"),
            interval_kind=kw.pop("interval_kind"),
            draws=None,
            status=kw.pop("uncertainty_status"),
        )
        hz = kw.pop("horizon")
        irf = tuple(fx.IrfPoint(**p) for p in kw.pop("irf"))
        r = kw.pop("run")
        run = fx.RevaRun(
            run_id=r["run_id"],
            code_version=r["code_version"],
            data_snapshot=r["data_snapshot"],
            as_of=r["as_of"],
            spec_sha256=r["spec_sha256"],
            replicates=tuple(r["replicates"]),
        )
        items.append(
            fx.EffectEstimateIn(
                **kw,
                uncertainty=unc,
                horizon=fx.Horizon(kind=hz["kind"], value=hz["value"], unit=hz["unit"]),
                irf=irf,
                run=run,
                transcription="computed",
                rights={
                    "licence": "internal-derived",
                    "restricted": bool(doc["rights"]["restricted_sources"]),
                    "non_commercial": bool(doc["rights"]["non_commercial"]),
                },
                curation={"curated_by": "agent PRED-EVENTSTUDY", "review_status": "maintainer_review_pending"},
            )
        )
    return items


def effect_dataset(doc: Mapping[str, Any]) -> str:
    return f"eventstudy.{doc['request']['treatment']}.{doc['request']['outcome'].split(':', 1)[1]}".lower()


def write_effect_estimates(
    engine: Any, store: Any, doc: Mapping[str, Any], run_sha: str, *, origin: str
) -> dict[str, Any]:
    """Write the run's estimate(s) through PRED-EFFECT's loader (``reva.effects.load_effect_estimates``) as
    ``source_kind="reva_run"`` with the archived run document as the delivery's raw artifact.

    Refused (rule declared before any write): a run whose replication gate is not open on its snapshot, or whose
    pre-trend test rejects at 10 % or has a significant placebo lead -- such runs stay archived and documented,
    never in the effect layer."""
    from reva import effects as fx

    gate = doc.get("replication_gate") or {}
    if not gate.get("gate_open"):
        raise ValidationProblem("run has no open replication gate on its snapshot; not written")
    bad = {"pretrend_test_rejects_at_10pct", "placebo_lead_significant"} & set(doc["interpretation"]["flags"])
    if bad:
        raise ValidationProblem("pre-trend diagnostics fail; the run is archived but not written", flags=sorted(bad))

    rep = fx.load_effect_estimates(
        engine,
        store,
        source_kind="reva_run",
        source_key=SOURCE_KEY,
        dataset=effect_dataset(doc),
        raw_sha256s=[run_sha],
        retrieved_at=datetime.now(UTC),
        origin=origin,
        complete=True,
        retraction_reason="absent_from_delivery",
        estimates=build_effect_inputs(doc),
        parameters={"run_id": doc["run_id"], "spec_sha256": doc["spec_sha256"], "result_sha256": doc["result_sha256"]},
    )
    return dict(rep.as_dict())
