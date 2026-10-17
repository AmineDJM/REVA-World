"""Decision → distributions: the V5 decision engine (agent PRED-ENGINE).

Propagation
-----------
1. The decision becomes *direct changes* (``reva.predict.decision.direct_changes``; stated scenario assumptions).
2. Breadth-first over variables: from each reached (variable, geography), the engine looks up the effect
   estimates whose cause matches (identifier match, or PRED-EFFECT's curated variable ↔ atlas links). Only an
   estimate with a number (``point``) propagates; sign-only estimates and sourced causal claims touching the
   decision's analogue anchors are listed as **liens non chiffrés** and never receive a number.
3. Existing simulators run as plugins on the variables they consume (``reva.predict.propagators``): IO price
   model (FIGARO cost push), crude border-value exposure, retirement age. Their outputs are terminal.
4. Monte Carlo over the uncertainty each estimate publishes (``normal_approx``: SE, CI or labelled range; none
   = scenario point, flagged), with one named counter-based stream per estimate
   (``reva.simulation.rng.stream(seed, ENGINE_ID, estimate_key, 0)``: common random numbers across decisions).
   One draw = one coefficient per estimate, the same standard-normal draw at every horizon of its lag profile
   (comonotone horizons, stated).
5. Time: a quarterly grid from the decision's start. A lag profile is read as the response to a *sustained*
   change of the cause (step response, an assumption) and composed by superposition of the cause path's
   increments: ``y_t = Σ_s R(t − s) · (x_s − x_{s−1})``. A lag the source did not publish leaves the period
   missing (never interpolated). Undated estimates (cumulative / peak / long-run / unspecified) give an undated
   result.

Honesty gates
-------------
* Label: every result is "scénario non validé — pas une probabilité" unless the exact chain (signature of its
  estimate keys and plugin versions) has a back-test and a calibration record in
  ``registry/predict/validated_chains.yaml`` (written by the judge only; empty today).
* Double counting: one estimate per edge (deterministic choice, ``estimates.CHOICE_RULE``, alternatives listed,
  never summed or averaged) and one path per variable (breadth-first: the shortest path wins; any other path
  reaching the same variable is listed as "chemin alternatif non cumulé").
* Applicability: estimate scope vs geography (out-of-scope estimates are excluded with a reason; world panels and
  unverified country groups are allowed with a warning), estimation period vs the decision's start, unit
  compatibility (no implicit conversion), as-of filter for replays.

Pure: the engine reads only through the ports of ``reva.predict.context``.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import numpy as np
import yaml

from reva.predict import estimates as es
from reva.predict import propagators as pp
from reva.predict.context import (
    BaselineSource,
    ClaimSource,
    EffectSource,
    PluginData,
    engine_variables,
)
from reva.predict.decision import REGISTRY_DIR, Decision, DirectChange, VarRef, direct_changes, quarter_labels
from reva.simulation import rng

ENGINE_ID = "reva.predict.engine@1.0.0"
DEFAULT_DRAWS = 2000
MAX_DRAWS = 20000
MAX_DEPTH = 4
QUANTILES = (0.05, 0.25, 0.5, 0.75, 0.95)
LABEL_UNVALIDATED = "scénario non validé — pas une probabilité"
LABEL_UNVALIDATED_EN = "unvalidated scenario — not a probability"
DISCLAIMERS_FR = (
    "Scénario conditionnel à la décision et aux hypothèses énoncées : ni une prévision, ni une probabilité, tant "
    "que la chaîne de modèles n'a pas passé le juge (rétro-test en millésimes + calibration).",
    "Les intervalles ne reflètent que l'incertitude publiée par les estimations utilisées : ni l'incertitude de "
    "modèle, ni celle de la transposition à un autre pays ou à une autre époque, ni celle de la chaîne.",
    "REVA n'établit aucun effet : chaque nombre vient d'une estimation attribuée (publication citée, calcul REVA "
    "ou identité comptable) ou d'un modèle REVA sous hypothèses énoncées ; un lien causal sans estimation chiffrée "
    "reste sans nombre.",
    "Une seule estimation par lien et un seul chemin par variable : rien n'est additionné ni moyenné.",
    "Recherche, pas un conseil financier ni une recommandation.",
)
NO_DRIFT_NOTE = (
    "baseline = latest observation held flat (persistence), not a forecast; scenario level = baseline + change"
)


# ------------------------------------------------------------------------------------------- validation
def chain_signature(ids: list[str]) -> str:
    return hashlib.sha256(json.dumps(sorted(set(ids)), separators=(",", ":")).encode()).hexdigest()


@lru_cache(maxsize=1)
def _validated() -> dict[str, dict[str, Any]]:
    with open(REGISTRY_DIR / "validated_chains.yaml", encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    return {str(c["signature"]): dict(c) for c in doc.get("chains") or [] if c.get("signature")}


def validation_status(ids: list[str], registry: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    sig = chain_signature(ids)
    rec = (registry if registry is not None else _validated()).get(sig)
    if rec and rec.get("backtest_record") and rec.get("calibration_record"):
        return {
            "status": "validated",
            "label": "chaîne validée par le juge (rétro-test et calibration enregistrés)",
            "chain_signature": sig,
            "record": dict(rec),
        }
    return {
        "status": "unvalidated",
        "label": LABEL_UNVALIDATED,
        "label_en": LABEL_UNVALIDATED_EN,
        "chain_signature": sig,
        "reason": "no back-test and calibration record for this model chain (registry/predict/validated_chains.yaml)"
        if not rec
        else "the record lacks a back-test or a calibration record",
        "judge_requirements": [
            "as-of replay on vintage data (PRED-VINTAGE), never final-vintage pseudo-real-time reported as real-time",
            "calibration record (interval coverage / PIT) from the prospective registry (PRED-REGISTRY)",
        ],
    }


# ------------------------------------------------------------------------------------------- nodes
@dataclass
class Node:
    ref: VarRef
    geo: str
    unit: str
    depth: int
    dated: np.ndarray | None = None  # (n, H); NaN = missing
    reasons: list[str | None] = field(default_factory=list)  # per period missing reason
    undated: np.ndarray | None = None  # (n,)
    undated_kind: str | None = None
    path: list[str] = field(default_factory=list)  # hop ids from the decision
    point_estimates: set[str] = field(default_factory=set)  # estimate keys without published uncertainty
    notes: list[str] = field(default_factory=list)
    decision_change: DirectChange | None = None
    propagate: bool = True

    @property
    def id(self) -> str:
        return f"{self.ref.key}@{self.geo}"


def _summary(a: np.ndarray) -> dict[str, Any]:
    q = np.quantile(a, QUANTILES)
    return {
        "p05": _r(q[0]),
        "p25": _r(q[1]),
        "p50": _r(q[2]),
        "p75": _r(q[3]),
        "p95": _r(q[4]),
        "mean": _r(float(np.mean(a))),
        "degenerate": bool(np.ptp(a) == 0),
    }


def _r(x: float) -> float:
    return float(f"{float(x):.6g}")


class Engine:
    def __init__(
        self,
        decision: Decision,
        effects: EffectSource,
        claims: ClaimSource,
        baseline: BaselineSource,
        plugin_data: PluginData | None = None,
        *,
        draws: int = DEFAULT_DRAWS,
        seed: int = 0,
        as_of_max: str | None = None,
        max_depth: int = MAX_DEPTH,
        validated: Mapping[str, Mapping[str, Any]] | None = None,
    ):
        if not 100 <= draws <= MAX_DRAWS:
            raise ValueError(f"draws must be in [100, {MAX_DRAWS}]")
        self.d = decision
        self.effects = effects
        self.claims = claims
        self.baseline = baseline
        self.pdata = plugin_data or PluginData()
        self.n = draws
        self.seed = seed
        self.as_of_max = as_of_max
        self.max_depth = max_depth
        self.validated = validated
        self.H = decision.horizon_quarters
        self.periods = quarter_labels(decision.start, self.H)
        self.vars = engine_variables()
        self.nodes: dict[str, Node] = {}
        self.hops: list[dict[str, Any]] = []
        self.unquantified: list[dict[str, Any]] = []
        self.excluded: list[dict[str, Any]] = []
        self.alt_paths: list[dict[str, Any]] = []
        self.gaps: list[dict[str, Any]] = []
        self.plugin_outputs: list[dict[str, Any]] = []
        self._z: dict[str, np.ndarray] = {}
        self._est_cache: dict[str, list[dict[str, Any]]] = {}
        self._used: list[str] = []
        self._plugins_run: set[str] = set()

    # -------------------------------------------------------------------------------- helpers
    def _hop_id(self) -> str:
        return f"h{len(self.hops) + 1}"

    def _zs(self, key: str) -> np.ndarray:
        if key not in self._z:
            self._z[key] = rng.stream(self.seed, ENGINE_ID, key, 0).standard_normal(self.n)
        return self._z[key]

    def _unit_of(self, ref: VarRef) -> str | None:
        info = self.effects.variable_info(ref)
        if info and info.get("unit"):
            return str(info["unit"])
        if ref.kind == "atlas_concept":
            return _atlas_unit(ref.id)
        return None

    def _label(self, ref: VarRef) -> dict[str, str | None]:
        info = self.effects.variable_info(ref) or {}
        fr = info.get("label_fr") or (info.get("labels") or {}).get("fr")
        en = info.get("label") or (info.get("labels") or {}).get("en")
        if ref.kind == "atlas_concept" and not (fr or en):
            fr, en = _atlas_labels(ref.id)
        return {"label_fr": fr, "label_en": en}

    def _estimates_for(self, ref: VarRef) -> list[dict[str, Any]]:
        if ref.key not in self._est_cache:
            self._est_cache[ref.key] = list(self.effects.by_cause(ref))
        return self._est_cache[ref.key]

    # -------------------------------------------------------------------------------- decision
    def _decision_node(self, dc: DirectChange) -> Node:
        unit = es.TRANSFORM_UNIT.get(dc.transform) or es.canonical_unit(dc.unit)[0] or dc.unit
        node = Node(dc.variable, dc.geo, unit, 0, decision_change=dc)
        x = np.zeros((self.n, self.H))
        change = float(dc.change)
        if dc.timing == "occurrence":
            x[:, :] = 1.0
            node.notes.append("occurrence at the start quarter: published responses to an occurrence are read as is")
        else:
            end = self.H if dc.duration is None else min(self.H, dc.duration)
            x[:, :end] = change
        node.dated = x
        node.reasons = [None] * self.H
        if dc.transform == "level":
            node.propagate = False
            node.notes.append(
                "target level, not a change: only model plugins that read a level use it; effect estimates of a "
                "change of this variable are not applied to a level"
            )
        hid = self._hop_id()
        self.hops.append(
            {
                "id": hid,
                "kind": "decision",
                "to": node.id,
                "direct_change": dc.as_json(),
                "label_fr": "Changement direct décidé (hypothèse de scénario)",
            }
        )
        node.path = [hid]
        return node

    # -------------------------------------------------------------------------------- estimates
    def _apply(self, node: Node, hop: es.Hop, hid: str) -> tuple[np.ndarray | None, list[str | None], np.ndarray | None, list[str]]:
        notes: list[str] = []
        z = self._zs(hop.estimate_key)
        if hop.dated is not None:
            if node.dated is not None:
                x = node.dated
            else:
                assert node.undated is not None
                x = np.repeat(node.undated[:, None], self.H, axis=1)
                notes.append("undated upstream change treated as a step at the decision's start")
            inc = np.diff(x, axis=1, prepend=0.0)
            y = np.full((self.n, self.H), np.nan)
            reasons: list[str | None] = [None] * self.H
            prof = {q: (p.coef + (p.sd * z if p.sd is not None else 0.0)) for q, p in hop.dated.items()}
            for t in range(self.H):
                acc = np.zeros(self.n)
                why: str | None = None
                for s in range(t + 1):
                    col = inc[:, s]
                    if np.isnan(col).any():
                        why = (node.reasons[s] if node.reasons and s < len(node.reasons) else None) or "upstream_missing"
                        break
                    if not col.any():
                        continue
                    r = prof.get(t - s)
                    if r is None:
                        why = f"lag_not_published (lag {t - s} quarters)"
                        break
                    acc = acc + r * col
                if why is None:
                    y[:, t] = acc
                reasons[t] = why
            return y, reasons, None, notes
        coef = hop.coef if hop.coef is not None else 0.0
        b = coef + (hop.sd * z if hop.sd is not None else 0.0)
        if node.undated is not None:
            x1 = node.undated
        elif node.decision_change is not None:
            x1 = np.full(self.n, float(node.decision_change.change) if node.decision_change.timing != "occurrence" else 1.0)
            notes.append("undated estimate applied to the decided change (the decision's duration is not used)")
        else:
            assert node.dated is not None
            ok = [t for t in range(self.H) if not np.isnan(node.dated[:, t]).any()]
            if not ok:
                return None, [], None, ["upstream path entirely missing"]
            x1 = node.dated[:, ok[-1]]
            notes.append(f"undated estimate applied to the upstream value at {self.periods[ok[-1]]} (last available)")
        return None, [], b * x1, notes

    def _out_geos(self, node: Node, effect: VarRef) -> list[str]:
        if self.vars.geo_kind(effect) == "global":
            return ["WLD"]
        if node.geo == "WLD":
            return list(self.d.countries) or []
        return [node.geo]

    def _expand(self, node: Node, queue: deque[Node]) -> None:
        ests = self._estimates_for(node.ref)
        if not ests:
            self.gaps.append(
                {
                    "code": "no_effect_estimate_for_cause",
                    "node": node.id,
                    "message_fr": f"aucune estimation d'effet n'a pour cause {node.ref.id} : la propagation s'arrête ici",
                }
            )
            return
        groups: dict[str, list[es.Hop]] = {}
        for e in ests:
            hop = es.hop_from_estimate(e, self._unit_of)
            if self.as_of_max and hop.as_of and hop.as_of.isoformat() > self.as_of_max:
                self.excluded.append(self._excl(node, hop, "not_knowable_at_as_of", f"as_of {hop.as_of} > {self.as_of_max}"))
                continue
            if not hop.quantified:
                self.unquantified.append(
                    {
                        "kind": "sign_only_estimate",
                        "from": node.id,
                        "to": hop.effect.as_json(),
                        "sign": hop.sign,
                        "estimate_key": hop.estimate_key,
                        "wording_fr": hop.raw.get("wording_fr"),
                        "source": hop.provenance(),
                        "note_fr": "estimation sans valeur chiffrée (signe seulement) : aucun nombre n'est propagé",
                    }
                )
                continue
            if hop.problems:
                self.excluded.append(self._excl(node, hop, "not_propagatable", ", ".join(hop.problems)))
                continue
            groups.setdefault(hop.effect.key, []).append(hop)
        for _, hops in sorted(groups.items()):
            effect = hops[0].effect
            for g in self._out_geos(node, effect):
                cands: list[tuple[es.Hop, str, list[str], str | None]] = []
                for h in hops:
                    app, app_notes = es.applicability(h.scope, g if g != "WLD" or node.geo == "WLD" else node.geo, self.d.start_year)
                    if app == "out_of_scope":
                        self.excluded.append(self._excl(node, h, "out_of_scope", "; ".join(app_notes), geo=g))
                        continue
                    ok, approx = es.units_compatible(node.unit, h.in_unit or "")
                    if not ok:
                        self.excluded.append(
                            self._excl(
                                node,
                                h,
                                "unit_mismatch",
                                f"the estimate's cause is stated in {h.in_unit}, the reached change is in {node.unit} "
                                "(no implicit conversion)",
                                geo=g,
                            )
                        )
                        continue
                    cands.append((h, app, app_notes, approx))
                if not cands:
                    continue
                cands.sort(key=lambda c: es.choice_key(c[0], c[1]))
                chosen, app, app_notes, approx = cands[0]
                target_id = f"{effect.key}@{g}"
                if target_id in self.nodes:
                    self.alt_paths.append(
                        {
                            "to": target_id,
                            "via": node.id,
                            "estimate_key": chosen.estimate_key,
                            "reason_fr": "chemin alternatif non cumulé : la variable est déjà atteinte par un chemin plus "
                            "court ; les effets ne sont jamais additionnés",
                        }
                    )
                    continue
                hid = self._hop_id()
                y, reasons, u, notes = self._apply(node, chosen, hid)
                if y is None and u is None:
                    self.gaps.append({"code": "upstream_missing", "node": target_id, "message": "; ".join(notes)})
                    continue
                child = Node(
                    effect,
                    g,
                    chosen.out_unit or "",
                    node.depth + 1,
                    dated=y,
                    reasons=reasons,
                    undated=u,
                    undated_kind=chosen.undated_kind if u is not None else None,
                    path=[*node.path, hid],
                    point_estimates=set(node.point_estimates)
                    | ({chosen.estimate_key} if not chosen.has_uncertainty else set()),
                    notes=[*notes, *chosen.notes, *app_notes, *([approx] if approx else [])],
                )
                if u is not None and node.undated_kind:
                    child.undated_kind = node.undated_kind if chosen.undated_kind is None else chosen.undated_kind
                self.nodes[target_id] = child
                self._used.append(chosen.estimate_key)
                self.hops.append(
                    {
                        "id": hid,
                        "kind": "estimate",
                        "from": node.id,
                        "to": target_id,
                        "estimate_key": chosen.estimate_key,
                        "matched_via": chosen.raw.get("matched_via", "direct"),
                        "measure": chosen.raw.get("measure"),
                        "unit": chosen.raw.get("unit"),
                        "cause_definition": (chosen.raw.get("cause") or {}).get("definition"),
                        "effect_definition": (chosen.raw.get("effect") or {}).get("definition"),
                        "coefficient": {
                            "point": chosen.coef,
                            "sd": chosen.sd,
                            "uncertainty_basis": chosen.basis,
                            "in_unit": chosen.in_unit,
                            "out_unit": chosen.out_unit,
                        },
                        "time_profile": (
                            {"kind": "dated", "quarters": sorted(chosen.dated)}
                            if chosen.dated is not None
                            else {"kind": "undated", "horizon_kind": chosen.undated_kind}
                        ),
                        "applicability": {"status": app, "notes": app_notes, "geo": g},
                        "notes": chosen.notes,
                        "source": chosen.provenance(),
                        "alternatives": [
                            {
                                "estimate_key": h.estimate_key,
                                "point": h.coef,
                                "applicability": a,
                                "reason_fr": "non retenue (règle de choix déterministe) ; jamais additionnée ni moyennée",
                            }
                            for h, a, _, _ in cands[1:]
                        ],
                        "choice_rule": es.CHOICE_RULE,
                    }
                )
                queue.append(child)

    def _excl(self, node: Node, hop: es.Hop, code: str, detail: str, geo: str | None = None) -> dict[str, Any]:
        return {
            "code": code,
            "from": node.id,
            "to": hop.effect.as_json(),
            "geo": geo,
            "estimate_key": hop.estimate_key,
            "detail": detail,
        }

    # -------------------------------------------------------------------------------- plugins
    def _plugin_input(self, node: Node) -> tuple[np.ndarray | None, list[str]]:
        if node.undated is not None:
            return node.undated, []
        assert node.dated is not None
        k = min(4, self.H)
        block = node.dated[:, :k]
        if np.isnan(block).any():
            return None, ["upstream path missing in the first year"]
        return block.mean(axis=1), [
            f"annual accounting period: mean change over the first {k} quarters of the decision ({self.periods[0]}"
            f"–{self.periods[k - 1]})"
        ]

    def _run_plugins(self, node: Node) -> None:
        for key, wiring in sorted(self.vars.plugins.items()):
            if not any(node.ref.kind == k and node.ref.id == i and es.units_compatible(node.unit, u)[0] for k, i, u in wiring.inputs):
                continue
            run_key = f"{key}|{node.id}"
            if run_key in self._plugins_run:
                continue
            self._plugins_run.add(run_key)
            hid = self._hop_id()
            if key == pp.RETIREMENT_KEY:
                res = self._retirement(node)
                notes: list[str] = []
            else:
                x, notes = self._plugin_input(node)
                if x is None:
                    self.gaps.append({"code": "plugin_input_missing", "plugin": key, "node": node.id, "message": notes})
                    continue
                if key == pp.IO_KEY:
                    if self.pdata.io_arrays is None:
                        self.gaps.append(
                            {
                                "code": "plugin_data_missing",
                                "plugin": key,
                                "message": self.pdata.io_gap or "no IO table pinned in the baseline release",
                            }
                        )
                        continue
                    res = pp.run_io_price(self.pdata.io_arrays, x, list(self.d.countries), self.pdata.io_provenance)
                elif key == pp.CRUDE_KEY:
                    if not self.pdata.crude_inputs:
                        self.gaps.append(
                            {
                                "code": "plugin_data_missing",
                                "plugin": key,
                                "message": self.pdata.crude_gap or "no crude border values in the baseline release",
                            }
                        )
                        continue
                    res = pp.run_crude_exposure(
                        self.pdata.crude_inputs, x, list(self.d.countries), self.pdata.crude_provenance
                    )
                else:
                    continue
            for gap in res.gaps:
                self.gaps.append({**gap, "plugin": res.model})
            self._used.append(res.model)
            self.hops.append(
                {
                    "id": hid,
                    "kind": "plugin",
                    "from": node.id,
                    "to": [f"{o.variable.key}@{o.geo}" for o in res.outputs],
                    "model": res.model,
                    "label_fr": wiring.label_fr,
                    "assumption_fr": wiring.assumption_fr,
                    "model_labels": res.labels,
                    "notes": notes,
                    "provenance": _jsonable(res.provenance),
                }
            )
            for o in res.outputs:
                self.plugin_outputs.append(self._plugin_variable(o, node, hid, res.model))

    def _retirement(self, node: Node) -> pp.PluginResult:
        dc = node.decision_change
        opts = dict(self.d.plugin_options.get(pp.RETIREMENT_KEY) or {})
        months = int(dc.change) if dc is not None else 0
        derived = "first_birth_year" not in opts
        if derived:
            opts["first_birth_year"] = self.d.start_year - months // 12
        if self.pdata.retirement_runner is None:
            r = pp.PluginResult(model=pp.RETIREMENT_KEY)
            r.gaps.append(
                {
                    "code": "plugin_data_missing",
                    "message": self.pdata.retirement_gap or "the retirement model cannot run (no pinned inputs)",
                }
            )
            return r
        res = pp.run_retirement(self.pdata.retirement_runner, node.geo, months, opts, self.n)
        if derived:
            res.provenance["first_birth_year_derivation"] = (
                f"start year {self.d.start_year} minus target age {months // 12} (derived, not stated by the decision)"
            )
        return res

    def _plugin_variable(self, o: pp.PluginOutput, node: Node, hid: str, model: str) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "id": f"{o.variable.key}@{o.geo}",
            "variable": {**o.variable.as_json(), "label_fr": o.label_fr, "label_en": None},
            "geo": o.geo,
            "unit": o.unit,
            "kind": "plugin_output",
            "model": model,
            "depth": node.depth + 1,
            "path": [*node.path, hid],
            "notes": o.notes,
            "uncertainty_complete": not node.point_estimates,
            "point_estimates": sorted(node.point_estimates),
            "baseline": None,
        }
        if o.frequency == "undated":
            assert o.undated is not None
            entry.update(frequency="undated", undated={"kind": "first_round", **_summary(o.undated)}, periods=[])
        else:
            entry.update(
                frequency="year",
                undated=None,
                periods=[{"period": str(y), **_summary(v), "missing_reason": None} for y, v in sorted(o.yearly.items())],
            )
        return entry

    # -------------------------------------------------------------------------------- run
    def run(self) -> dict[str, Any]:
        queue: deque[Node] = deque()
        dcs = direct_changes(self.d)
        for dc in dcs:
            node = self._decision_node(dc)
            if node.id in self.nodes:
                continue
            self.nodes[node.id] = node
            queue.append(node)
        while queue:
            node = queue.popleft()
            self._run_plugins(node)
            if not node.propagate:
                continue
            if node.depth >= self.max_depth:
                self.gaps.append({"code": "max_depth", "node": node.id, "message": f"propagation stops at {self.max_depth} hops"})
                continue
            self._expand(node, queue)
        self._anchor_claims()
        variables = [self._variable(n) for n in self.nodes.values() if n.depth > 0]
        variables += self.plugin_outputs
        validation = validation_status(self._used, self.validated)
        label = LABEL_UNVALIDATED if validation["status"] != "validated" else validation["label"]
        return {
            "engine": ENGINE_ID,
            "label": label,
            "validation": validation,
            "disclaimers_fr": list(DISCLAIMERS_FR),
            "decision": self.d.model_dump(mode="json"),
            "settings": {
                "draws": self.n,
                "seed": self.seed,
                "horizon_quarters": self.H,
                "periods": self.periods,
                "as_of_max": self.as_of_max,
                "max_depth": self.max_depth,
                "quantiles": list(QUANTILES),
                "seed_plan": rng.seed_plan(self.seed, ENGINE_ID, sorted(set(self._z)), self.n),
            },
            "direct_changes": [dc.as_json() for dc in dcs],
            "variables": variables,
            "hops": self.hops,
            "unquantified_links": self.unquantified,
            "excluded_links": self.excluded,
            "alternative_paths": self.alt_paths,
            "gaps": self.gaps,
            "effect_layer": self.effects.status(),
            "chain": sorted(set(self._used)),
        }

    def _anchor_claims(self) -> None:
        if not self.d.anchors:
            return
        for c in self.claims.claims_touching(self.d.anchors):
            self.unquantified.append(
                {
                    "kind": "causal_claim",
                    "claim_key": c.get("claim_key"),
                    "from": {"id": (c.get("cause") or {}).get("id"), "label": _lbl(c.get("cause"))},
                    "to": {"id": (c.get("effect") or {}).get("id"), "label": _lbl(c.get("effect"))},
                    "relation": c.get("relation"),
                    "wording_fr": c.get("wording_fr"),
                    "source": {
                        "source_key": (c.get("source") or {}).get("source_key"),
                        "tier": (c.get("source") or {}).get("tier"),
                    },
                    "grade": (c.get("grade") or {}).get("grade") if isinstance(c.get("grade"), Mapping) else None,
                    "note_fr": "affirmation causale sourcée sur un épisode analogue choisi par REVA (curation) : "
                    "aucune taille d'effet, aucun nombre propagé",
                }
            )

    def _variable(self, n: Node) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "id": n.id,
            "variable": {**n.ref.as_json(), **self._label(n.ref)},
            "geo": n.geo,
            "unit": n.unit,
            "kind": "estimate_chain",
            "depth": n.depth,
            "path": n.path,
            "notes": n.notes,
            "uncertainty_complete": not n.point_estimates,
            "point_estimates": sorted(n.point_estimates),
        }
        base = self.baseline.baseline(n.ref, n.geo)
        entry["baseline"] = base
        if n.dated is not None:
            periods = []
            for t in range(self.H):
                col = n.dated[:, t]
                if np.isnan(col).any():
                    periods.append({"period": self.periods[t], "missing_reason": n.reasons[t] or "missing"})
                    continue
                p = {"period": self.periods[t], **_summary(col), "missing_reason": None}
                lvl = _scenario_level(base, n.unit, col)
                if lvl is not None:
                    p["scenario_level"] = lvl
                periods.append(p)
            entry.update(frequency="quarter", periods=periods, undated=None)
        else:
            assert n.undated is not None
            u = {"kind": n.undated_kind or "unspecified", **_summary(n.undated)}
            u["kind_label_fr"] = es.UNDATED_LABEL_FR.get(u["kind"], u["kind"])
            entry.update(frequency="undated", periods=[], undated=u)
        return entry


def _scenario_level(base: Mapping[str, Any] | None, unit: str, delta: np.ndarray) -> dict[str, Any] | None:
    if not base or base.get("value") is None:
        return None
    try:
        b = float(base["value"])
    except (TypeError, ValueError):
        return None
    bu = str(base.get("unit") or "")
    if unit == "pp" and "%" in bu:
        out = _summary(b + delta)
    elif unit == "percent" and "%" not in bu:
        out = _summary(b * (1 + delta / 100.0))
    else:
        return None
    out["note"] = NO_DRIFT_NOTE
    return out


def _lbl(side: Any) -> str | None:
    if not isinstance(side, Mapping):
        return None
    return side.get("label_fr") or side.get("label")


def _jsonable(o: Any) -> Any:
    return json.loads(json.dumps(o, default=str))


@lru_cache(maxsize=1)
def _atlas_catalog() -> dict[str, dict[str, Any]]:
    try:
        from reva.atlas.catalog import cached_catalog, load_catalog

        try:
            cat = cached_catalog()
        except Exception:  # noqa: BLE001 - another agent's catalog file may be mid-edit: read leniently
            cat = load_catalog(strict=False)
        return {cid: {"unit": c.unit, "fr": c.labels.get("fr"), "en": c.labels.get("en")} for cid, c in cat.concepts.items()}
    except Exception:  # noqa: BLE001 - a missing catalog only removes labels/units (typed later as unknown)
        return {}


def _atlas_unit(cid: str) -> str | None:
    c = _atlas_catalog().get(cid)
    return c.get("unit") if c else None


def _atlas_labels(cid: str) -> tuple[str | None, str | None]:
    c = _atlas_catalog().get(cid) or {}
    return c.get("fr"), c.get("en")


def predict(
    decision: Decision,
    effects: EffectSource,
    claims: ClaimSource,
    baseline: BaselineSource,
    plugin_data: PluginData | None = None,
    **kw: Any,
) -> dict[str, Any]:
    """Propagate one decision; see the module docstring. Returns a JSON-able result."""
    return Engine(decision, effects, claims, baseline, plugin_data, **kw).run()
