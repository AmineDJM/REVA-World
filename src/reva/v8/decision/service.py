"""Decision-studio services behind ``/v1/v8/decision/*``: resolve a scenario, run it once per input hash, and
derive sensitivity, comparison and brief from the cached run.

The callers pass the data fingerprint (cheap read) and a factory of read-only contexts that is only called on a
cache miss, so a cached scenario never touches the engine again. Tests use static contexts.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from reva.kernel.errors import ValidationProblem
from reva.predict.decision import Decision, reference_decisions
from reva.v8.decision import runner as rn
from reva.v8.decision.brief import brief_markdown
from reva.v8.decision.bundle import estimate_keys, make_bundle
from reva.v8.decision.templates import MARKET_INSTRUMENTS


class UnknownReference(LookupError):
    pass


@dataclass(frozen=True)
class Scenario:
    decision: Decision
    draws: int = 2000
    seed: int = 0
    as_of_max: str | None = None
    origin: str = "any"

    @property
    def hash(self) -> str:
        return rn.scenario_hash(
            self.decision, draws=self.draws, seed=self.seed, as_of_max=self.as_of_max, origin=self.origin
        )


def resolve(reference_id: str | None, decision: Decision | None) -> Decision:
    if (reference_id is None) == (decision is None):
        raise ValidationProblem("give exactly one of reference_id or decision")
    if reference_id is not None:
        d = reference_decisions().get(reference_id)
        if d is None:
            raise UnknownReference(reference_id)
        return d
    assert decision is not None
    return decision


ContextFactory = Callable[[Decision], rn.Contexts]


def _near(result: dict[str, Any], ctx: rn.Contexts) -> dict[str, list[dict[str, Any]]]:
    """Estimates next to where the chain stops at the decision itself (what *almost* matched)."""
    out: dict[str, list[dict[str, Any]]] = {}
    blocked = {
        g.get("node")
        for g in result.get("gaps") or []
        if g.get("code") in ("no_effect_estimate_for_cause", "variable_not_in_effect_vocabulary")
    }
    for h in result.get("hops") or []:
        if h.get("kind") != "decision":
            continue
        nid = h["to"]
        if (h.get("direct_change") or {}).get("transform") == "level":
            out[nid] = ctx.near(nid, "same")
        elif nid in blocked:
            out[nid] = ctx.near(nid, "related")
    return out


def run(sc: Scenario, fingerprint: str, make_ctx: ContextFactory) -> tuple[dict[str, Any], rn.Computed, bool]:
    """The page bundle of one scenario (cached by input hash + data fingerprint). Returns (bundle, computed, hit)."""
    key = f"{sc.hash}:{fingerprint}"
    hit = rn.RUNS.get(key)
    if hit is not None:
        return hit[0], hit[1], True
    t0 = time.perf_counter()
    ctx = make_ctx(sc.decision)
    comp = rn.compute(sc.decision, ctx, draws=sc.draws, seed=sc.seed, as_of_max=sc.as_of_max)
    lookup = ctx.lookup(estimate_keys(comp.result))
    bundle = make_bundle(
        comp.result, lookup, _near(comp.result, ctx), market=sc.decision.instrument in MARKET_INSTRUMENTS
    )
    bundle["result"] = comp.result
    bundle["scenario_hash"] = sc.hash
    bundle["cache_key"] = key
    bundle["compute_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    rn.RUNS.put(key, (bundle, comp))
    rn.mark_warm()
    return bundle, comp, False


def sensitivity(
    sc: Scenario, fingerprint: str, make_ctx: ContextFactory, *, variable: str | None = None
) -> tuple[dict[str, Any], bool]:
    key = f"{sc.hash}:{fingerprint}:{variable or '*'}"
    hit = rn.SENSITIVITY.get(key)
    if hit is not None:
        return hit, True
    bundle, comp, _ = run(sc, fingerprint, make_ctx)
    ctx = make_ctx(sc.decision)  # fresh read-only ports on the caller's connection
    shorts = {k: v.get("short") or k for k, v in (bundle.get("evidence") or {}).items()}
    out = rn.sensitivity(sc.decision, ctx, comp, as_of_max=sc.as_of_max, only=variable, labels=shorts)
    out["scenario_hash"] = sc.hash
    out["label"] = comp.result.get("label")
    rn.SENSITIVITY.put(key, out)
    return out, False


def compare(a: Scenario, b: Scenario, fingerprint: str, make_ctx: ContextFactory) -> dict[str, Any]:
    ba, ca, hit_a = run(a, fingerprint, make_ctx)
    bb, cb, hit_b = run(b, fingerprint, make_ctx)
    out = rn.compare(ca, cb)
    out["a"] = _summary(ba)
    out["b"] = _summary(bb)
    out["cached"] = {"a": hit_a, "b": hit_b}
    return out


def _summary(bundle: dict[str, Any]) -> dict[str, Any]:
    r = bundle["result"]
    return {
        "scenario_hash": bundle["scenario_hash"],
        "decision": r.get("decision"),
        "label": r.get("label"),
        "validation": r.get("validation"),
        "headline": bundle.get("headline"),
        "diagnosis": {k: bundle["diagnosis"].get(k) for k in ("has_result", "headline_fr")},
    }


def brief(
    sc: Scenario, fingerprint: str, make_ctx: ContextFactory, *, url: str | None, with_sensitivity: bool
) -> dict[str, Any]:
    bundle, _, _ = run(sc, fingerprint, make_ctx)
    sens = sensitivity(sc, fingerprint, make_ctx)[0] if with_sensitivity else None
    md = brief_markdown(bundle, url=url, sensitivity=sens, scenario_hash=sc.hash)
    return {
        "markdown": md,
        "filename": f"reva-decision-{sc.decision.id}-{sc.hash[:8]}.md",
        "scenario_hash": sc.hash,
        "label": bundle["result"].get("label"),
        "n_citations": sum(1 for v in (bundle.get("evidence") or {}).values() if v.get("citation") or v.get("run")),
    }
