"""What the decision engine reads, as ports (V5, agent PRED-ENGINE).

The engine (``reva.predict.engine``) is pure; everything it reads comes through these ports:

* :class:`EffectSource` — effect estimates whose cause matches a variable (PRED-EFFECT's layer
  ``kg.effect_estimate_current`` in production, :class:`StaticEffectSource` in tests);
* :class:`ClaimSource` — sourced causal claims touching the decision's analogue anchors (never quantified);
* :class:`BaselineSource` — a baseline value per variable and geography (PRED-REGISTRY baseline forecasts when
  available, otherwise the latest atlas observation held flat, labelled as persistence, not a forecast);
* :class:`PluginData` — inputs of the simulator plugins (IO table arrays, crude border values, retirement inputs).

``Static*`` implementations hold plain data (tests, documentation runs); ``reva.predict.db`` implements the ports
over the database read-only.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Protocol

import yaml

from reva.predict.decision import REGISTRY_DIR, VarRef


# ------------------------------------------------------------------------------------------- registry
@dataclass(frozen=True)
class PluginWiring:
    model_key: str
    label_fr: str
    inputs: tuple[tuple[str, str, str], ...]  # (kind, id, unit)
    assumption_fr: str


@dataclass(frozen=True)
class EngineVariables:
    global_vars: frozenset[str]  # "kind:id" keys of world-level variables
    plugins: Mapping[str, PluginWiring]

    def geo_kind(self, ref: VarRef) -> str:
        return "global" if ref.key in self.global_vars else "country"


@lru_cache(maxsize=1)
def engine_variables() -> EngineVariables:
    with open(REGISTRY_DIR / "variables.yaml", encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    glob = frozenset(f"{v['kind']}:{v['id']}" for v in doc.get("variables", []) if v.get("geo") == "global")
    plugins = {
        k: PluginWiring(
            model_key=k,
            label_fr=str(p["label_fr"]),
            inputs=tuple((str(i["kind"]), str(i["id"]), str(i["unit"])) for i in p.get("inputs", [])),
            assumption_fr=" ".join(str(p.get("assumption_fr", "")).split()),
        )
        for k, p in (doc.get("plugins") or {}).items()
    }
    return EngineVariables(glob, plugins)


# ------------------------------------------------------------------------------------------- ports
class EffectSource(Protocol):
    def status(self) -> dict[str, Any]: ...  # {"layer": "present" | "absent", "detail": ..., "n_seen": int}

    def by_cause(self, ref: VarRef) -> list[dict[str, Any]]: ...  # estimate JSON dicts (current, filtered)

    def variable_info(self, ref: VarRef) -> dict[str, Any] | None: ...  # {"label", "label_fr", "unit", ...}


class ClaimSource(Protocol):
    def claims_touching(self, ids: Sequence[str], limit_per_id: int = 25) -> list[dict[str, Any]]: ...


class BaselineSource(Protocol):
    def baseline(self, ref: VarRef, geo: str) -> dict[str, Any] | None: ...


@dataclass
class PluginData:
    """Inputs of the simulator plugins (all optional; absent = the plugin reports a typed gap).

    * ``io_arrays``: ``reva.models.economic.io_price.IOArrays`` + ``io_provenance`` (dataset id, sha256, release);
    * ``crude_inputs``: ``{iso3: {"M": value, "X": value, "G": value, "year": int, "provenance": {...}}}`` from the
      pinned belief release (values are kernel decimal strings; a missing one is absent, never zero);
    * ``retirement_runner``: ``callable(spec) -> {"status", "rows" | "problems", "provenance"}`` running the
      registered ``social.retirement_age`` plugin on the pinned release.
    """

    io_arrays: Any = None
    io_provenance: dict[str, Any] = field(default_factory=dict)
    io_gap: str | None = None
    crude_inputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    crude_provenance: dict[str, Any] = field(default_factory=dict)
    crude_gap: str | None = None
    retirement_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    retirement_gap: str | None = None


# ------------------------------------------------------------------------------------------- static ports
def _matches(est_side: Mapping[str, Any], ref: VarRef, variables: Mapping[str, Mapping[str, Any]]) -> str | None:
    kind, ident = str(est_side.get("kind")), str(est_side.get("id"))
    if kind == ref.kind and ident == ref.id:
        return "direct"
    # curated links of PRED-EFFECT's variables (atlas_concepts), in both directions — identifiers only
    if ref.kind == "atlas_concept" and kind == "variable":
        v = variables.get(ident)
        if v and ref.id in (v.get("atlas_concepts") or []):
            return f"variable:{ident}"
    if ref.kind == "variable" and kind == "atlas_concept":
        v = variables.get(ref.id)
        if v and ident in (v.get("atlas_concepts") or []):
            return f"variable:{ref.id}"
    return None


class StaticEffectSource:
    """Estimates held in memory (estimate JSON dicts as PRED-EFFECT serves them)."""

    def __init__(self, estimates: Sequence[Mapping[str, Any]], variables: Sequence[Mapping[str, Any]] = ()):
        self._estimates = [dict(e) for e in estimates]
        self._variables = {str(v["id"]): dict(v) for v in variables}

    def status(self) -> dict[str, Any]:
        return {"layer": "present", "detail": "static estimates", "n_seen": len(self._estimates)}

    def by_cause(self, ref: VarRef) -> list[dict[str, Any]]:
        out = []
        for e in self._estimates:
            via = _matches(e.get("cause") or {}, ref, self._variables)
            if via is not None:
                out.append({**e, "matched_via": via} if via != "direct" else e)
        return out

    def variable_info(self, ref: VarRef) -> dict[str, Any] | None:
        if ref.kind == "variable":
            return self._variables.get(ref.id)
        return None


class StaticClaimSource:
    def __init__(self, claims: Sequence[Mapping[str, Any]] = ()):
        self._claims = [dict(c) for c in claims]

    def claims_touching(self, ids: Sequence[str], limit_per_id: int = 25) -> list[dict[str, Any]]:
        want = set(ids)
        return [
            c for c in self._claims if (c.get("cause") or {}).get("id") in want or (c.get("effect") or {}).get("id") in want
        ]


class StaticBaselineSource:
    def __init__(self, values: Mapping[tuple[str, str], Mapping[str, Any]] | None = None):
        self._values = {k: dict(v) for k, v in (values or {}).items()}

    def baseline(self, ref: VarRef, geo: str) -> dict[str, Any] | None:
        return self._values.get((ref.key, geo))


class NoClaims:
    def claims_touching(self, ids: Sequence[str], limit_per_id: int = 25) -> list[dict[str, Any]]:
        return []


class NoBaseline:
    def baseline(self, ref: VarRef, geo: str) -> dict[str, Any] | None:
        return None
