"""The allow-list a drafting LLM may use, built from the registries at request time.

Nothing here is hand-maintained per model. A model is *draftable* when

1. ``contracts/scenario.schema.json`` (through :func:`reva.simulation.spec.scenario_schema`)
   has a branch whose ``model_key`` is a constant, and
2. the simulation plugin registry (``reva.simulation.plugins.REGISTRY`` after
   ``load_all()``) registers that ``model_key`` — when the plugin registry exists.
   Before it exists (V1 builds) the schema alone decides.

So a model plugin becomes draftable automatically once it is registered and its
sub-schema is part of the scenario schema. For each model the catalogue lists the
allowed countries, reference years, fidelities, outputs and the *parameter paths*
the LLM may set (every non-constant leaf of the model-specific objects such as
``intervention`` and ``assumptions``). Constants are filled by the server, never
by the LLM. Capability-gap keys come from the capability catalogue (every
non-implemented entry), from the roadmap models that are not registered yet
(``model.<model_key>``) and from a few drafting-specific refusal keys.

The catalogue is trusted server data; it goes into the fixed system prompt. The
LLM's output is checked against the same catalogue by :mod:`reva.nl.service`.
"""

from __future__ import annotations

import copy
import logging
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from reva.kernel.ids import logical_hash

log = logging.getLogger("reva.nl")

#: Top-level spec fields the server owns (never taken from the LLM).
SERVER_FIELDS = frozenset({"schema_version", "baseline_release_id", "knowledge_cutoff", "model_version", "seed"})
#: Top-level fields the LLM output carries explicitly (not as parameters).
EXPLICIT_FIELDS = frozenset({"model_key", "title", "countries", "reference_year", "fidelity", "outputs"})

#: Roadmap models (docs/roadmap/v2_design.md). Until registered they are capability gaps ``model.<key>``.
PLANNED_MODELS: Mapping[str, str] = {
    "demography.cohort_component": "Cohort-component population projection (multi-period, by age and sex)",
    "economic.io_price": "Multi-region input-output cost-push price model",
    "social.retirement_age": "Retirement-age module (employment, pension recipients, dependency ratio)",
}

#: Drafting-specific gap/refusal keys (not capabilities of the simulation engine).
NL_GAP_KEYS: Mapping[str, tuple[str, str]] = {
    "geography.not_covered": ("capability_gap", "A named country or region is not covered by the selected model"),
    "nl.parameter_not_draftable": ("capability_gap", "A requested setting has no parameter in the selected model"),
    "nl.input_value_override": ("policy_refusal", "Setting, replacing or assuming data values is refused: inputs "
                                "come only from the pinned baseline release and missing stays missing"),
    "nl.instruction_in_text": ("policy_refusal", "The text contains instructions addressed to the system; "
                               "they were treated as data and not followed"),
    "nl.out_of_scope": ("capability_gap", "The text is not a scenario request REVA can draft"),
    "nl.unclassified": ("capability_gap", "Unsupported request that matches no more specific key"),
}

#: Short, reviewed hints for parameters whose schema carries no description (keyed by model_key, path).
PARAMETER_HINTS: Mapping[tuple[str, str], str] = {
    ("economic.crude_exposure", "intervention.import_multiplier"):
        "ratio applied to the crude-oil import border price (1.2 = +20 %, 0.8 = -20 %; neutral value 1.0 = unchanged)",
    ("economic.crude_exposure", "intervention.export_multiplier"):
        "ratio applied to the crude-oil export border price (1.2 = +20 %, 0.8 = -20 %; neutral value 1.0 = unchanged)",
    ("economic.crude_exposure", "assumptions.elasticity_grid[].import_elasticity"):
        "import quantity elasticity for the L2 sensitivity grid (a stated scenario assumption)",
    ("economic.crude_exposure", "assumptions.elasticity_grid[].export_elasticity"):
        "export quantity elasticity for the L2 sensitivity grid (a stated scenario assumption)",
    ("economic.crude_exposure", "assumptions.quantity_response"):
        "fixed (L1) or elasticity_sensitivity (L2); normally implied by the fidelity",
}


@dataclass(frozen=True)
class ParamSpec:
    """One LLM-settable leaf. ``path`` uses ``a.b`` and ``a.list[].c`` (indexed as ``a.list[0].c``)."""

    path: str
    kind: str  # number | integer | choice | text | boolean
    choices: tuple[Any, ...] = ()
    description: str = ""

    def prompt_view(self) -> dict[str, Any]:
        out: dict[str, Any] = {"path": self.path, "type": self.kind}
        if self.choices:
            out["choices"] = list(self.choices)
        if self.description:
            out["meaning"] = self.description
        return out


@dataclass(frozen=True)
class DraftableModel:
    model_key: str
    model_version: str | None
    schema_version: str | None
    description: str
    review_status: str
    countries: tuple[str, ...]
    reference_years: tuple[int, int] | None
    fidelities: tuple[str, ...]
    outputs: tuple[str, ...]
    output_units: Mapping[str, str]
    parameters: tuple[ParamSpec, ...]
    not_computed: tuple[str, ...]
    branch: Mapping[str, Any] = field(repr=False, compare=False, default_factory=dict)

    @property
    def experimental(self) -> bool:
        return self.review_status not in ("approved", "reviewed")

    def param(self, path: str) -> ParamSpec | None:
        generic = _generic_path(path)
        return next((p for p in self.parameters if p.path == generic), None)

    def prompt_view(self) -> dict[str, Any]:
        return {
            "model_key": self.model_key,
            "description": self.description,
            "countries": list(self.countries),
            "reference_years": (f"any year from {self.reference_years[0]} to {self.reference_years[1]} inclusive"
                                if self.reference_years else None),
            "fidelities": {f: _fidelity_title(f) for f in self.fidelities},
            "outputs": [{"id": o, "unit": self.output_units.get(o, "")} for o in self.outputs],
            "parameters": [p.prompt_view() for p in self.parameters],
            "does_not_compute": list(self.not_computed),
        }


@dataclass(frozen=True)
class GapKey:
    key: str
    kind: str  # capability_gap | policy_refusal
    title: str


@dataclass(frozen=True)
class DraftCatalog:
    models: Mapping[str, DraftableModel]
    gap_keys: Mapping[str, GapKey]
    plugin_registry: str  # "registered" | "absent" | "unavailable"
    horizon_multi_period: bool

    def prompt_view(self) -> dict[str, Any]:
        return {
            "models": [m.prompt_view() for m in self.models.values()],
            "capability_keys": [{"key": g.key, "kind": g.kind, "title": g.title} for g in self.gap_keys.values()],
        }

    def digest(self) -> str:
        return logical_hash(self.prompt_view())

    def model_gap(self, model_key: str) -> str | None:
        """The gap key for an unregistered model: the capability id if catalogued, else ``model.<key>``."""
        for k in (model_key, f"model.{model_key}"):
            if k in self.gap_keys:
                return k
        return None

    def normalize_gap(self, key: str) -> str | None:
        """A gap key from the LLM, accepting ``model.<id>`` for the catalogued ``<id>`` and vice versa."""
        if key in self.gap_keys:
            return key
        if key.startswith("model."):
            return key[6:] if key[6:] in self.gap_keys else None
        return f"model.{key}" if f"model.{key}" in self.gap_keys else None

    def countries(self) -> frozenset[str]:
        return frozenset(c for m in self.models.values() for c in m.countries)


# --------------------------------------------------------------------------- schema walking
def _generic_path(path: str) -> str:
    return re.sub(r"\[\d+\]", "[]", path)


#: Model-neutral meaning of the fidelity levels (the capability titles are crude-model specific).
FIDELITY_MEANING: Mapping[str, str] = {
    "L0": "exact cache lookup of an identical, already computed specification",
    "L1": "deterministic computation under the stated assumptions",
    "L2": "labelled sensitivity range over a stated assumption grid (no probabilities)",
}


def _fidelity_title(f: str) -> str:
    return FIDELITY_MEANING.get(f, f)


def _resolve(node: Any, root: Mapping[str, Any]) -> Any:
    """Resolve local ``$ref`` (``#/$defs/...``) recursively."""
    if isinstance(node, dict):
        if "$ref" in node and isinstance(node["$ref"], str) and node["$ref"].startswith("#/"):
            target: Any = root
            for part in node["$ref"][2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
            merged = {**_resolve(target, root), **{k: v for k, v in node.items() if k != "$ref"}}
            return _resolve(merged, root) if "$ref" in merged else merged
        return {k: _resolve(v, root) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve(v, root) for v in node]
    return node


def _merge(base: Mapping[str, Any], branch: Mapping[str, Any]) -> dict[str, Any]:
    out = {k: copy.deepcopy(v) for k, v in base.items() if k not in ("oneOf", "anyOf", "$defs", "definitions")}
    # root placeholders ({}: "defined by the model branch") are not properties of a model
    props = {k: v for k, v in (out.get("properties") or {}).items() if v != {}}
    props.update(branch.get("properties") or {})
    out["properties"] = props
    out["required"] = sorted(set(out.get("required") or []) | set(branch.get("required") or []))
    all_of = list(out.get("allOf") or []) + list(branch.get("allOf") or [])
    if all_of:
        out["allOf"] = all_of
    for k, v in branch.items():
        if k not in ("properties", "required", "allOf"):
            out[k] = v
    return out


def _const(node: Any) -> tuple[bool, Any]:
    if isinstance(node, dict):
        if "const" in node:
            return True, node["const"]
        if isinstance(node.get("enum"), list) and len(node["enum"]) == 1:
            return True, node["enum"][0]
    return False, None


def model_branches(schema: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    """Each model's merged schema branch (single schema, oneOf/anyOf union, or if/then on model_key)."""
    root = _resolve(dict(schema), schema)
    found = False
    for key in ("oneOf", "anyOf"):
        for br in root.get(key) or []:
            merged = _merge(root, br)
            if _const((merged.get("properties") or {}).get("model_key"))[0]:
                found = True
                yield merged
    for clause in root.get("allOf") or []:
        cond = ((clause.get("if") or {}).get("properties") or {}).get("model_key")
        if _const(cond)[0] and isinstance(clause.get("then"), dict):
            base = {k: v for k, v in root.items() if k != "allOf"}
            merged = _merge(base, clause["then"])
            merged["properties"]["model_key"] = {"const": _const(cond)[1]}
            found = True
            yield merged
    if not found and _const((root.get("properties") or {}).get("model_key"))[0]:
        yield root


def _leaves(node: Mapping[str, Any], prefix: str) -> Iterator[tuple[str, dict[str, Any]]]:
    if _const(node)[0] or not node:
        return
    t = node.get("type")
    if t == "object" or "properties" in node:
        for name, sub in (node.get("properties") or {}).items():
            yield from _leaves(sub, f"{prefix}.{name}" if prefix else name)
        return
    if t == "array":
        items = node.get("items") or {}
        if items.get("type") == "object" or "properties" in items:
            yield from _leaves(items, f"{prefix}[]")
        else:
            yield f"{prefix}[]", dict(items)
        return
    yield prefix, dict(node)


def _param(model_key: str, path: str, node: Mapping[str, Any]) -> ParamSpec:
    hint = PARAMETER_HINTS.get((model_key, path)) or str(node.get("description") or "")
    if isinstance(node.get("enum"), list):
        return ParamSpec(path, "choice", tuple(node["enum"]), hint)
    t = node.get("type")
    alternatives = [a.get("type") for a in (node.get("oneOf") or node.get("anyOf") or []) if isinstance(a, dict)]
    if t is None and alternatives:  # e.g. a number or a decimal string: the draft always sends a JSON number
        t = next((k for k in ("number", "integer", "boolean") if k in alternatives), None)
    if t in ("number", "integer", "boolean"):
        return ParamSpec(path, t, (), hint)
    return ParamSpec(path, "text", (), hint)


def _latest(node: Any) -> str | None:
    """A constant, or the highest dotted version of an enum (new drafts use the current version)."""
    values = [v for v in _enum(node) if isinstance(v, str)]
    if not values:
        return None

    def key(v: str) -> tuple[int, ...]:
        try:
            return tuple(int(x) for x in v.split("."))
        except ValueError:
            return (-1,)

    return max(values, key=key)


def _enum(node: Any) -> tuple[Any, ...]:
    if not isinstance(node, dict):
        return ()
    if "const" in node:
        return (node["const"],)
    if isinstance(node.get("enum"), list):
        return tuple(node["enum"])
    items = node.get("items")
    return _enum(items) if isinstance(items, dict) else ()


def registered_countries() -> tuple[str, ...]:
    """ISO3 keys of the registered country entities (reviewed ``countries.yaml`` + the verified crosswalk)."""
    from reva.ontology.registry import REGISTRY_ROOT, load_yaml

    keys: set[str] = set()
    try:
        for e in load_yaml(REGISTRY_ROOT / "entities" / "countries.yaml").get("entities") or []:
            if isinstance(e, dict) and e.get("kind") == "entity.country" and re.fullmatch(r"[A-Z]{3}",
                                                                                        str(e.get("key"))):
                keys.add(str(e["key"]))
    except (OSError, AttributeError):
        pass
    try:
        from reva.adapters.connectors.country_crosswalk import load_crosswalk

        for e in load_crosswalk().get("entities") or []:
            if e.get("kind") == "entity.country" and re.fullmatch(r"[A-Z]{3}", str(e.get("key"))):
                keys.add(str(e["key"]))
    except Exception as e:  # an unverifiable crosswalk adds nothing (fail closed to the reviewed list)
        log.warning("country crosswalk not used for drafting: %s", type(e).__name__)
    return tuple(sorted(keys))


def _countries(node: Any, plugin: Any) -> tuple[str, ...]:
    """A model's country allow-list: the schema enum, else registered entities matching the item pattern."""
    listed = tuple(str(c) for c in _enum(node))
    if listed:
        return listed
    items = node.get("items") if isinstance(node, dict) else None
    pattern = items.get("pattern") if isinstance(items, dict) else None
    out = [c for c in registered_countries() if pattern is None or re.search(str(pattern), c)]
    if getattr(plugin, "supports_all_countries", False):
        out.append("ALL")
    return tuple(out)


def _model_registry_entries() -> dict[str, dict[str, Any]]:
    from reva.ontology.registry import REGISTRY_ROOT, load_yaml

    try:
        models = load_yaml(REGISTRY_ROOT / "models" / "methods.yaml")["models"]
    except (OSError, KeyError, TypeError):
        return {}
    return {str(m["model_key"]): m for m in models if isinstance(m, dict) and "model_key" in m}


def registered_plugins() -> tuple[str, dict[str, Any]]:
    """(state, {model_key: plugin}) from ``reva.simulation.plugins`` if that module exists."""
    try:
        from reva.simulation import plugins
    except ImportError as e:
        if getattr(e, "name", "") in ("reva.simulation.plugins",):
            return "absent", {}
        log.warning("simulation plugin registry failed to import (%s); drafting uses the schema alone",
                    type(e).__name__)
        return "unavailable", {}
    try:
        load_all = getattr(plugins, "load_all", None)
        if callable(load_all):
            load_all()
        reg = getattr(plugins, "REGISTRY", None)
    except Exception as e:  # a broken plugin must not take drafting down silently
        log.warning("simulation plugin registry failed to load (%s); drafting uses the schema alone",
                    type(e).__name__)
        return "unavailable", {}
    if not isinstance(reg, Mapping):
        return "unavailable", {}
    return "registered", {str(k): v for k, v in reg.items()}


def _plugin_manifest(plugin: Any) -> dict[str, Any]:
    try:
        m = plugin.manifest() if hasattr(plugin, "manifest") else {}
    except Exception:  # manifest is descriptive only
        return {}
    return m if isinstance(m, dict) else {}


def _description(key: str, manifest: Mapping[str, Any], reg_entry: Mapping[str, Any]) -> str:
    """Registry/plugin text describing what the model computes (label and stated assumptions)."""
    parts = [str(manifest.get("description") or reg_entry.get("description") or manifest.get("label") or key)]
    if not (manifest.get("description") or reg_entry.get("description")):
        assumptions = manifest.get("assumptions") or reg_entry.get("assumptions") or []
        if isinstance(assumptions, list) and assumptions:
            parts.append("Assumptions: " + "; ".join(str(a) for a in assumptions[:4]))
    return " ".join(" ".join(parts).split())[:1200]


def build_catalog(schema: Mapping[str, Any] | None = None, *,
                  plugins: tuple[str, Mapping[str, Any]] | None = None) -> DraftCatalog:
    """Build the allow-list from the scenario schema, the plugin registry and the capability catalogue."""
    from reva import capabilities
    from reva.simulation.spec import scenario_schema

    schema = schema if schema is not None else scenario_schema()
    state, registry = plugins if plugins is not None else registered_plugins()
    methods = _model_registry_entries()
    models: dict[str, DraftableModel] = {}
    multi_period = False
    for br in model_branches(schema):
        props = br.get("properties") or {}
        key = str(_const(props.get("model_key"))[1])
        if state == "registered" and key not in registry:
            log.info("scenario schema branch %s has no registered plugin; not draftable", key)
            continue
        reg_entry = methods.get(key, {})
        manifest = _plugin_manifest(registry[key]) if key in registry else {}
        years = props.get("reference_year") or {}
        ref_years = (int(years["minimum"]), int(years["maximum"])) if "minimum" in years and "maximum" in years \
            else None
        horizon = props.get("horizon") or {}
        periods = (horizon.get("properties") or {}).get("periods") or {}
        if not (_const(periods)[0] and _const(periods)[1] == 1):
            multi_period = True
        params: list[ParamSpec] = []
        for name, sub in props.items():
            if name in SERVER_FIELDS or name in EXPLICIT_FIELDS:
                continue
            params.extend(_param(key, path, node) for path, node in _leaves(sub, name))
        units = {str(p["name"]): str(p.get("unit", "")) for p in reg_entry.get("output_ports", [])
                 if isinstance(p, dict) and "name" in p}
        applicability = reg_entry.get("applicability") or {}
        models[key] = DraftableModel(
            model_key=key,
            model_version=_latest(props.get("model_version")),
            schema_version=_latest(props.get("schema_version")),
            description=_description(key, manifest, reg_entry),
            review_status=str(manifest.get("review_status") or reg_entry.get("approval_status") or "unknown"),
            countries=_countries(props.get("countries"), registry.get(key)),
            reference_years=ref_years,
            fidelities=tuple(str(f) for f in _enum(props.get("fidelity"))),
            outputs=tuple(str(o) for o in _enum(props.get("outputs"))),
            output_units=units,
            parameters=tuple(params),
            not_computed=tuple(str(x) for x in applicability.get("unsupported_outputs") or ()),
            branch=br,
        )
    gaps: dict[str, GapKey] = {}
    for cap in capabilities.catalogue():
        if not cap.available and not cap.verification_only:
            gaps[cap.capability_id] = GapKey(cap.capability_id, "capability_gap", cap.title)
    for key, title in PLANNED_MODELS.items():
        if key not in models and key not in gaps:  # the capability catalogue's own id wins when it exists
            gaps[f"model.{key}"] = GapKey(f"model.{key}", "capability_gap", f"{title}: not registered yet")
    for key, (kind, title) in NL_GAP_KEYS.items():
        gaps[key] = GapKey(key, kind, title)
    return DraftCatalog(models=models, gap_keys=gaps, plugin_registry=state, horizon_multi_period=multi_period)


@lru_cache(maxsize=2)
def _cached(schema_digest: str, plugin_keys: tuple[str, ...], state: str, entity_stamp: tuple[int, ...]
            ) -> DraftCatalog:
    return build_catalog()


def current_catalog() -> DraftCatalog:
    """The catalogue for this request (rebuilt when the schema, the plugins or the entity files change)."""
    from reva.ontology.registry import REGISTRY_ROOT
    from reva.simulation.spec import scenario_schema

    state, registry = registered_plugins()
    stamp = tuple(p.stat().st_mtime_ns if p.exists() else 0 for p in
                  (REGISTRY_ROOT / "entities" / "countries.yaml", REGISTRY_ROOT / "entities" / "country_crosswalk.json"))
    return _cached(logical_hash(scenario_schema()), tuple(sorted(registry)), state, stamp)
