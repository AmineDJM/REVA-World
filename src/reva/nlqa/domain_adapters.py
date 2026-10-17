"""Real adapters of the V6-domain ports (agent NLQA6), read-only.

* :class:`FamiliesDbAdapter` — ``reva.kg.family_query.entity_families`` (V4CORE) and a two-sided family join
  for cross lists (entities linked to A and located in B), with the licence of each statement's raw artifact;
* :class:`EffectsDbAdapter` — ``reva.effects.query`` (PRED-EFFECT), estimates as ``estimate_json`` dicts;
* :class:`ForecastRegistryAdapter` — ``reva.evaluation.registry.read`` (PRED-REGISTRY), seals re-verified;
* :class:`DecisionEngineAdapter` — ``reva.predict.run.run_on_connection`` (PRED-ENGINE) in a READ ONLY
  transaction.

A missing table or module raises :class:`~reva.nlqa.ports.CapabilityUnavailable`; nothing is guessed.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from reva.kernel.errors import RevaError
from reva.nlqa.adapters import statement_from_kg
from reva.nlqa.ports import (
    CapabilityUnavailable,
    CrossResult,
    CrossRow,
    EffectTerm,
    Entity,
    FamilyGroup,
    FamilyNeighbour,
    FamilyView,
)

#: Rows read on each side of a cross list (a country can have 100k+ incoming located_in statements).
CROSS_SCAN = 200_000
TERMS_TTL_S = 300.0


@contextmanager
def _conn(engine: Engine, backend: str) -> Any:
    try:
        with engine.connect() as conn:
            yield conn
    except SQLAlchemyError as e:
        raise CapabilityUnavailable(f"{backend} query failed: {type(e).__name__}", backend=backend) from None


def _entity(node: Mapping[str, Any], labels: Mapping[str, Mapping[str, str]]) -> Entity:
    q = str(node.get("qid"))
    lab = dict(labels.get(q) or {})
    if not lab and node.get("label") and node.get("label") != q:
        lab = {"en": str(node["label"])}
    return Entity(qid=q, labels=lab, kind=node.get("kind"), source="kg")


class FamiliesDbAdapter:
    name = "reva.kg.family_query (relation families)"

    def __init__(self, engine: Engine, *, origin: str | None = None) -> None:
        self.engine = engine
        self.origin = origin

    def _mods(self) -> tuple[Any, Any]:
        try:
            from reva.kg import family_query as fq
            from reva.kg import query as kq
        except ImportError:
            raise CapabilityUnavailable("reva.kg.family_query is not installed", backend="families") from None
        return fq, kq

    def families(self, qid: str, *, families: Sequence[str], per_family: int) -> FamilyView:
        fq, kq = self._mods()
        reg = fq.registry()
        fams = [f for f in families if f in reg.families]
        if "same_as" in reg.families and "same_as" not in fams:
            fams.append("same_as")
        try:
            with _conn(self.engine, "families") as conn:
                res = fq.entity_families(conn, qid, families=fams, per_family=per_family, origin=self.origin, reg=reg)
                qids = {qid} | {
                    n["statement"][side]["qid"]
                    for g in res["families"]
                    for n in g["neighbours"]
                    for side in ("subject", "object")
                    if n["statement"].get(side)
                }
                labels = kq.labels_for(conn, qids)
        except RevaError as e:
            if isinstance(e, CapabilityUnavailable):
                raise
            return FamilyView(qid=qid, labels={})
        groups: list[FamilyGroup] = []
        identities: list[FamilyNeighbour] = []
        for g in res["families"]:
            ns = []
            for n in g["neighbours"]:
                fn = FamilyNeighbour(
                    family=g["family"],
                    reading=dict((n.get("reading") or {}).get("label") or {}),
                    inverse=bool((n.get("reading") or {}).get("inverse")),
                    neighbour=_entity(n["neighbour"], labels),
                    statement=statement_from_kg(n["statement"], labels),
                    share=n.get("share"),
                    rights=n.get("rights"),
                    subtype=n.get("subtype"),
                    identity=bool(n.get("identity")),
                )
                (identities if fn.identity else ns).append(fn)
            if ns:
                groups.append(FamilyGroup(g["family"], dict(g["label"]), int(g["count"]), tuple(ns), bool(g.get("count_is_lower_bound"))))
        lits = tuple((statement_from_kg(s, labels), s.get("rights")) for s in res.get("literals") or [])
        return FamilyView(
            qid=qid,
            labels=dict(labels.get(qid) or res.get("labels") or {}),
            groups=tuple(groups),
            literals=lits,
            identities=tuple(identities),
            kind=res.get("kind"),
        )

    def cross(
        self, a_qid: str, a_families: Sequence[str] | None, b_qid: str, b_families: Sequence[str], *, limit: int
    ) -> CrossResult:
        fq, kq = self._mods()
        reg = fq.registry()
        kq._check_qid(a_qid)
        kq._check_qid(b_qid)

        def sides(fams: Sequence[str] | None) -> tuple[list[str], list[str]]:
            pairs = (
                reg.properties_for([f for f in fams if f in reg.families])
                if fams
                else {(p, d) for p in reg.mappings for d in ("out", "in")}
            )
            pairs = {(p, d) for p, d in pairs if (reg.edge_family(p, d) or None) and reg.edge_family(p, d).family != "same_as"}  # type: ignore[union-attr]
            return sorted({p for p, d in pairs if d == "out"}), sorted({p for p, d in pairs if d == "in"})

        a_out, a_in = sides(a_families)
        b_out, b_in = sides(b_families)
        osql = kq._origin_sql(self.origin)
        side = """SELECT s.id AS sid, CASE WHEN s.subject_qid = :{n} THEN s.object_qid ELSE s.subject_qid END AS x
                    FROM kg.statement_current s
                   WHERE ((s.object_qid = :{n} AND s.property_pid = ANY(:{n}_out))
                       OR (s.subject_qid = :{n} AND s.property_pid = ANY(:{n}_in)))
                     AND s.object_qid IS NOT NULL AND s.subject_qid <> s.object_qid{o} LIMIT :scan"""
        params = {
            "a": a_qid, "b": b_qid, "a_out": a_out, "a_in": a_in, "b_out": b_out, "b_in": b_in,
            "scan": CROSS_SCAN, "lim": max(1, int(limit)), "origin": self.origin,
        }  # fmt: skip
        try:
            with _conn(self.engine, "families") as conn:
                conn.exec_driver_sql("SET LOCAL jit = off")
                rows = conn.execute(
                    text(f"""WITH a AS ({side.format(n="a", o=osql)}), b AS ({side.format(n="b", o=osql)}),
                              j AS (SELECT a.x, min(a.sid) AS a_id, min(b.sid) AS b_id FROM a JOIN b ON a.x = b.x
                                     WHERE a.x NOT IN (:a, :b) GROUP BY a.x)
                             SELECT j.*, count(*) OVER () AS total FROM j ORDER BY j.x LIMIT :lim"""),
                    params,
                ).mappings().all()
                if not rows:
                    return CrossResult(rows=(), total=0)
                ids = [r["a_id"] for r in rows] + [r["b_id"] for r in rows]
                srows = {
                    r["id"]: dict(r)
                    for r in conn.execute(
                        text(f"SELECT {kq._ST_SELECT} FROM kg.statement_current s WHERE s.id = ANY(:ids)"), {"ids": ids}
                    ).mappings()
                }
                qids = {x for r in srows.values() for x in (r["subject_qid"], r["object_qid"]) if x}
                labels = kq.labels_for(conn, qids)
                kinds = kq.kinds_for(conn, qids)
                props = kq.property_info(conn, {r["property_pid"] for r in srows.values()})
                rights = fq.rights_for(conn, list(srows.values()))
        except RevaError as e:
            if isinstance(e, CapabilityUnavailable):
                raise
            return CrossResult(rows=(), total=0)

        def neighbour(sid: int, x: str) -> FamilyNeighbour:
            r = srows[sid]
            d = "out" if r["subject_qid"] == x else "in"
            ef = reg.edge_family(r["property_pid"], d)
            other = r["object_qid"] if d == "out" else r["subject_qid"]
            st = kq._statement_out(r, labels, props, kinds)
            return FamilyNeighbour(
                family=ef.family if ef else "?",
                reading=dict(ef.label) if ef else {},
                inverse=bool(ef.inverse) if ef else False,
                neighbour=Entity(qid=other, labels=dict(labels.get(other) or {}), kind=kinds.get(other)),
                statement=statement_from_kg(st, labels),
                rights=fq._rights(r, rights),
                subtype=ef.mapping.subtype if ef else None,
            )

        out = tuple(
            CrossRow(
                Entity(qid=r["x"], labels=dict(labels.get(r["x"]) or {}), kind=kinds.get(r["x"])),
                neighbour(r["a_id"], r["x"]),
                neighbour(r["b_id"], r["x"]),
            )
            for r in rows
            if r["a_id"] in srows and r["b_id"] in srows
        )
        return CrossResult(rows=out, total=int(rows[0]["total"]), total_is_lower_bound=False)


class EffectsDbAdapter:
    name = "reva.effects.query (kg.effect_estimate_current)"

    def __init__(self, engine: Engine, *, origin: str = "any") -> None:
        self.engine = engine
        self.origin = origin
        self._terms: tuple[float, list[EffectTerm]] | None = None

    def _eq(self) -> Any:
        try:
            from reva.effects import query as eq
        except ImportError:
            raise CapabilityUnavailable("reva.effects is not installed", backend="effects") from None
        return eq

    def terms(self) -> list[EffectTerm]:
        if self._terms and time.monotonic() - self._terms[0] < TERMS_TTL_S:
            return self._terms[1]
        eq = self._eq()
        from reva.effects.variables import default_resolver

        res = default_resolver()
        origins = ["live", "test_fixture"] if self.origin == "any" else [self.origin]
        with _conn(self.engine, "effects") as conn:
            rows = conn.execute(
                text("""SELECT DISTINCT cause_kind AS k, cause_id AS i FROM kg.effect_estimate_current
                         WHERE origin = ANY(:o)
                        UNION SELECT DISTINCT effect_kind, effect_id FROM kg.effect_estimate_current
                         WHERE origin = ANY(:o)"""),
                {"o": origins},
            ).all()
        out = []
        for k, i in rows:
            labels = res.labels(k, i) or {}
            out.append(EffectTerm(kind=str(k), id=str(i), labels=dict(labels) if labels else {"en": str(i)}))
        del eq
        self._terms = (time.monotonic(), out)
        return out

    def estimates(
        self, cause_ids: Sequence[str], effect_ids: Sequence[str], *, country: str | None, limit: int
    ) -> list[Mapping[str, Any]]:
        eq = self._eq()
        seen: dict[str, Mapping[str, Any]] = {}
        with _conn(self.engine, "effects") as conn:
            for c in list(cause_ids) or [None]:
                for e in list(effect_ids) or [None]:
                    for r in eq.list_estimates(
                        conn, cause=c, effect=e, country=country, origin=self.origin, limit=limit, include_variables=False
                    ):
                        seen.setdefault(str(r["estimate_key"]), r)
                    if len(seen) >= limit:
                        break
        return list(seen.values())[:limit]


class ForecastRegistryAdapter:
    name = "reva.evaluation.registry (sealed forecast registry)"

    def __init__(self, engine: Engine, store: Callable[[], Any], *, origin: str | None = None) -> None:
        self.engine = engine
        self._store = store
        self.origin = origin

    def _r(self) -> Any:
        try:
            from reva.evaluation.registry import read as R
        except ImportError:
            raise CapabilityUnavailable("the forecast registry is not installed", backend="forecasts") from None
        return R

    def families(self) -> list[Mapping[str, Any]]:
        R = self._r()
        with _conn(self.engine, "forecasts") as conn:
            return list(R.families(conn, self.origin))

    def questions(self, family: str, geo: str | None, *, limit: int) -> list[Mapping[str, Any]]:
        R = self._r()
        try:
            with _conn(self.engine, "forecasts") as conn:
                rows, _ = R.questions(conn, self._store(), self.origin, family=family, geo=geo, limit=limit)
        except (RevaError, OSError) as e:
            if isinstance(e, CapabilityUnavailable):
                raise
            raise CapabilityUnavailable(f"forecast registry read failed: {type(e).__name__}", backend="forecasts") from None
        return list(rows)

    def question(self, question_id: str) -> Mapping[str, Any] | None:
        R = self._r()
        with _conn(self.engine, "forecasts") as conn:
            return R.question(conn, self._store(), question_id)  # type: ignore[no-any-return]

    def models(self) -> Mapping[str, tuple[str, str]]:
        from reva.evaluation.registry.forecasts import all_models

        return all_models()


class DecisionEngineAdapter:
    name = "reva.predict (decision engine)"

    def __init__(self, engine: Engine, store: Callable[[], Any], *, origin: str = "any") -> None:
        self.engine = engine
        self._store = store
        self.origin = origin

    def _mods(self) -> tuple[Any, Any, Any]:
        try:
            from reva.predict.decision import Decision, instruments, reference_decisions
        except ImportError:
            raise CapabilityUnavailable("the decision engine is not installed", backend="decisions") from None
        return Decision, instruments, reference_decisions

    def references(self) -> list[Mapping[str, Any]]:
        _, _, refs = self._mods()
        return [d.model_dump(mode="json") for d in refs().values()]

    def instruments(self) -> list[Mapping[str, Any]]:
        _, ins, _ = self._mods()
        return [i.as_json() for i in ins().values()]

    def run(self, decision: Mapping[str, Any], *, draws: int, seed: int) -> Mapping[str, Any]:
        Decision, _, _ = self._mods()
        from reva.predict.run import run_on_connection

        d = Decision.model_validate(dict(decision))
        with _conn(self.engine, "decisions") as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            return run_on_connection(conn, self._store(), d, draws=draws, seed=seed, origin=self.origin)  # type: ignore[no-any-return]


def wire_domains(b: Any, engine: Engine, settings: Any, store: Any) -> None:
    """Attach the V6-domain adapters to backends ``b`` (each one absent → ``None`` with a note)."""
    live = getattr(settings, "reva_env", "") == "production"
    holder: dict[str, Any] = {"store": store}

    def get_store() -> Any:
        if holder["store"] is None:
            from reva.adapters.objects.factory import build_store

            holder["store"] = build_store(settings)
        return holder["store"]

    b.families = FamiliesDbAdapter(engine, origin="live" if live else None)
    b.effects = EffectsDbAdapter(engine, origin="live" if live else "any")
    b.forecasts = ForecastRegistryAdapter(engine, get_store, origin="live" if live else None)
    b.decisions = DecisionEngineAdapter(engine, get_store, origin="live" if live else "any")
