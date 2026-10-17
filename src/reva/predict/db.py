"""Read-only database adapters of the decision-engine ports (V5, agent PRED-ENGINE).

* :class:`DbEffectSource` — PRED-EFFECT's readers (``reva.effects.list_estimates`` / ``effect_variables``) over
  ``kg.effect_estimate_current``; when that package or table is absent the layer is reported ``absent`` and
  nothing is propagated (never a fallback number).
* :class:`DbClaimSource` — sourced causal claims touching the decision's anchors (``reva.causal.graph``).
* :class:`AtlasBaseline` — latest *actual* atlas observation per (concept, country), projections excluded (provider
  flags, same rule as the atlas headline layer), held flat and labelled persistence (not a forecast). A variable
  reaches the atlas only through PRED-EFFECT's curated ``atlas_concepts`` links.
* :func:`plugin_data` — simulator inputs from the latest published live belief release: crude border values and
  GDP (scalars), the FIGARO IO table and the retirement model's datasets (pinned datasets; typed gaps otherwise).

Everything here only reads.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Connection, text

from reva.predict.context import PluginData
from reva.predict.decision import Decision, VarRef

log = logging.getLogger(__name__)

CRUDE_CONCEPTS = {
    "M": "trade.hs2709.import_value.world_total",
    "X": "trade.hs2709.export_value.world_total",
    "G": "economics.gdp.nominal_current_usd",
}


def _table_exists(conn: Connection, name: str) -> bool:
    return conn.execute(text("SELECT to_regclass(:n) IS NOT NULL"), {"n": name}).scalar_one() is True


# ------------------------------------------------------------------------------------------- effects
class DbEffectSource:
    def __init__(self, conn: Connection, *, origin: str = "any", as_of_max: str | None = None, limit: int = 500):
        self.conn = conn
        self.origin = origin
        self.as_of_max = as_of_max
        self.limit = limit
        self._seen = 0
        self._vars: dict[str, dict[str, Any]] | None = None
        self._reader: Any = None
        self._absent: str | None = None
        try:
            import reva.effects as fx  # type: ignore[import-not-found,unused-ignore]

            self._reader = fx
        except ImportError:
            self._absent = "package reva.effects (PRED-EFFECT) is not installed in this checkout"
        if self._absent is None and not _table_exists(conn, "kg.effect_estimate_current"):
            self._absent = "view kg.effect_estimate_current does not exist (migration 0019 not applied)"

    def status(self) -> dict[str, Any]:
        if self._absent:
            return {"layer": "absent", "detail": self._absent, "n_seen": 0}
        return {"layer": "present", "detail": "kg.effect_estimate_current via reva.effects", "n_seen": self._seen}

    def by_cause(self, ref: VarRef) -> list[dict[str, Any]]:
        if self._absent:
            return []
        kw: dict[str, Any] = {"cause": ref.id, "include_variables": True, "origin": self.origin, "limit": self.limit}
        if self.as_of_max:
            from datetime import date

            kw["as_of_max"] = date.fromisoformat(self.as_of_max)
        rows = [dict(r) for r in self._reader.list_estimates(self.conn, **kw)]
        out = []
        for r in rows:
            cause = r.get("cause") or {}
            # an exact id hit must also be the same kind; a curated variable hit is labelled by the reader
            if r.get("matched_via") or (cause.get("kind") == ref.kind and cause.get("id") == ref.id):
                out.append(r)
        self._seen += len(out)
        return out

    def variable_info(self, ref: VarRef) -> dict[str, Any] | None:
        if self._absent or ref.kind != "variable":
            return None
        if self._vars is None:
            try:
                self._vars = {str(v["id"]): dict(v) for v in self._reader.effect_variables()}
            except Exception:
                log.exception("effect variables unavailable")
                self._vars = {}
        v = self._vars.get(ref.id)
        if v is None:
            return None
        labels = v.get("labels") or {}
        return {**v, "label": labels.get("en"), "label_fr": labels.get("fr")}

    def atlas_concepts_of(self, ref: VarRef) -> list[str]:
        if ref.kind == "atlas_concept":
            return [ref.id]
        info = self.variable_info(ref) or {}
        return [str(c) for c in info.get("atlas_concepts") or []]


# ------------------------------------------------------------------------------------------- claims
class DbClaimSource:
    def __init__(self, conn: Connection, *, origin: str | None = None):
        self.conn = conn
        self.origin = origin

    def claims_touching(self, ids: Sequence[str], limit_per_id: int = 25) -> list[dict[str, Any]]:
        from reva.causal import graph as cg

        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for i in ids:
            for side in (cg.causes, cg.effects):
                try:
                    res = side(self.conn, i, origin=self.origin, limit=limit_per_id)
                except Exception:
                    log.warning("causal claims for anchor %s unavailable", i)
                    continue
                for c in res.get("claims") or []:
                    if c["claim_key"] in seen:
                        continue
                    seen.add(c["claim_key"])
                    out.append(c)
        return out


# ------------------------------------------------------------------------------------------- baseline
def _is_projection(flags: Mapping[str, Any] | None) -> bool:
    f = flags or {}
    proj = str(f.get("projection", "false")).lower()
    return (
        str(f.get("estimate_kind") or "") == "projection"
        or proj not in ("false", "")
        or str(f.get("period_after_vintage", "false")).lower() == "true"
    )


class AtlasBaseline:
    """Latest actual atlas cell per (concept, country); ``None`` when there is none (never zero)."""

    def __init__(self, query: Any, catalog: Any, effects: DbEffectSource | None = None, snapshot: str | None = None):
        self.q = query
        self.catalog = catalog
        self.effects = effects
        self.snapshot = snapshot

    def baseline(self, ref: VarRef, geo: str) -> dict[str, Any] | None:
        if self.q is None or geo == "WLD":
            return None
        concepts = (
            self.effects.atlas_concepts_of(ref)
            if self.effects is not None
            else ([ref.id] if ref.kind == "atlas_concept" else [])
        )
        for cid in concepts:
            if cid not in self.catalog.concepts:
                continue
            try:
                cells = self.q.best_cells(self.catalog, [cid], iso3=geo)
            except Exception:
                log.exception("atlas baseline for %s %s unavailable", cid, geo)
                return None
            actual = [c for c in cells if c.get("value") is not None and not _is_projection(c.get("flags"))]
            if not actual:
                continue
            c = max(actual, key=lambda c: c.get("year") or 0)
            return {
                "kind": "persistence_latest_observation",
                "label_fr": "dernière observation de l'atlas maintenue constante (persistance) — pas une prévision",
                "concept": cid,
                "value": c["value"],
                "unit": c.get("unit") or self.catalog.concepts[cid].unit,
                "year": c.get("year"),
                "source": f"{c.get('source')}.{c.get('dataset')}.{c.get('indicator')}",
                "provenance": c.get("provenance"),
                "rights": c.get("rights"),
                "snapshot": self.snapshot,
            }
        return None


def atlas_baseline(
    conn: Connection, store: Any, effects: DbEffectSource | None
) -> tuple[AtlasBaseline | None, str | None]:
    """The latest live atlas snapshot's query file, only if already materialised (never built here)."""
    try:
        from reva.atlas.catalog import cached_catalog
        from reva.atlas.query import query_for
        from reva.atlas.snapshot import resolve_snapshot

        snap = resolve_snapshot(conn, store, origin="live")
        q = query_for(snap, store, build=False)
        return AtlasBaseline(q, cached_catalog(), effects, snap.sha256), None
    except Exception as e:
        return None, f"atlas baseline unavailable: {type(e).__name__}: {str(e)[:200]}"


# ------------------------------------------------------------------------------------------- plugin data
def _value_decimal(vd: Mapping[str, Any] | None) -> Decimal | None:
    if not vd or vd.get("kind") != "decimal":
        return None
    try:
        v = Decimal(str(vd["value"]))
        scale = Decimal(str((vd.get("basis") or {}).get("scale_factor", "1")))
    except (InvalidOperation, KeyError, TypeError):
        return None
    return v * scale if v.is_finite() else None


def _year(temporal: Any) -> int | None:
    from reva.simulation.crude_plugin import calendar_year_of

    return calendar_year_of(temporal)


def latest_release(conn: Connection) -> dict[str, Any] | None:
    from reva.belief.snapshots import release_as_of

    return release_as_of(conn, None, origin="live")


def crude_inputs(conn: Connection, release: Mapping[str, Any], countries: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Per country: the latest year in which M, X and G all have a selected estimate in the release."""
    from reva.belief.snapshots import estimates
    from reva.simulation.compile import resolve_subject

    out: dict[str, dict[str, Any]] = {}
    for iso3 in countries:
        sid, _ = resolve_subject(conn, iso3)
        by: dict[str, dict[int, tuple[Decimal, str]]] = {}
        for name, concept in CRUDE_CONCEPTS.items():
            for r in estimates(conn, release["id"], subject_id=sid, concept_id=concept):
                if r["modality"] != "selected_report":
                    continue
                y = _year(r["temporal_extent"])
                v = _value_decimal(r["value"])
                if y is None or v is None:
                    continue
                by.setdefault(name, {})[y] = (v, str(r["id"]))
        common = set.intersection(*(set(by.get(k, {})) for k in CRUDE_CONCEPTS)) if by else set()
        if not common:
            continue
        y = max(common)
        out[iso3] = {
            **{k: str(by[k][y][0]) for k in CRUDE_CONCEPTS},
            "year": y,
            "provenance": {k: {"estimate_id": by[k][y][1], "concept": CRUDE_CONCEPTS[k]} for k in CRUDE_CONCEPTS},
        }
    return out


_IO_CACHE: dict[str, Any] = {}


def io_arrays(conn: Connection, store: Any, release: Mapping[str, Any]) -> tuple[Any, dict[str, Any], str | None]:
    from reva.models.economic import io_price as io
    from reva.simulation.compile import resolve_inputs
    from reva.simulation.plugins import InputRequirement

    req = InputRequirement(io.DATASET_INPUT, io.DATASET_CONCEPT, io.DATASET_SUBJECT, (), "dataset")
    try:
        inputs = resolve_inputs(conn, dict(release), [req], store)
    except Exception as e:
        return None, {}, f"IO table not resolvable: {type(e).__name__}: {str(e)[:200]}"
    d = inputs.dataset(io.DATASET_INPUT, io.DATASET_SUBJECT)
    if d.is_missing:
        return (
            None,
            {},
            (
                f"the baseline release {release['id']} pins no {io.DATASET_CONCEPT} dataset (FIGARO); "
                f"reason: {d.missing_reason}"
            ),
        )
    sha = str(d.artifact_sha256)
    if sha not in _IO_CACHE:
        _IO_CACHE.clear()
        _IO_CACHE[sha] = io.arrays_from_table(d.table, d.manifest.get("coverage") or d.manifest)
    prov = {"dataset_id": d.dataset_id, "artifact_sha256": sha, "release_id": str(release["id"])}
    return _IO_CACHE[sha], prov, None


def retirement_runner(conn: Connection, store: Any, release: Mapping[str, Any]) -> Any:
    from reva.simulation import plugins
    from reva.simulation.compile import resolve_inputs

    def run(spec: dict[str, Any]) -> dict[str, Any]:
        try:
            model = plugins.get(spec["model_key"], spec.get("model_version"))
            model.validate(spec)
            reqs = model.requirements(spec)
            inputs = resolve_inputs(conn, dict(release), reqs, store)
            compiled = model.compile(inputs, spec)
            own = getattr(compiled, "missing", None)
            missing = list(own) if own is not None else inputs.missing_problems()
            prov = {"release_id": str(release["id"]), "input_digest": inputs.digest()}
            if missing:
                return {"status": "missing_input", "problems": _jsonable(missing), "provenance": prov}
            rows = model.compute(compiled, "L1")
            labels = []
            hook = plugins.optional_hook(model, "explanation")
            if hook is not None:
                labels = [json.dumps(hook("L1"), default=str)[:2000]]
            return {
                "status": "succeeded",
                "rows": [
                    {
                        "output_key": r.output_key,
                        "variant": r.variant,
                        "value": r.value,
                        "year": r.period[0].year if r.period else None,
                    }
                    for r in rows
                ],
                "provenance": prov,
                "labels": labels,
            }
        except Exception as e:
            problem = e.problem() if hasattr(e, "problem") else {"detail": str(e)[:300]}
            return {"status": "unsupported", "problems": [_jsonable(problem)], "provenance": {}}

    return run


def _jsonable(o: Any) -> Any:
    return json.loads(json.dumps(o, default=str))


def plugin_data(conn: Connection, store: Any, decision: Decision, *, with_io: bool = True) -> PluginData:
    pd = PluginData()
    rel = latest_release(conn)
    if rel is None:
        msg = "no published live belief release on this database"
        pd.io_gap = pd.crude_gap = pd.retirement_gap = msg
        return pd
    pd.crude_inputs = crude_inputs(conn, rel, decision.countries)
    pd.crude_provenance = {"release_id": str(rel["id"]), "knowledge_cutoff": str(rel.get("knowledge_cutoff"))}
    if not pd.crude_inputs:
        pd.crude_gap = f"release {rel['id']} has no complete (M, X, G) year for {list(decision.countries)}"
    if with_io:
        pd.io_arrays, pd.io_provenance, pd.io_gap = io_arrays(conn, store, rel)
    else:
        pd.io_gap = "IO model not loaded for this request"
    pd.retirement_runner = retirement_runner(conn, store, rel)
    return pd
