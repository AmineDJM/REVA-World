"""Causal claims backend of NLQA (``why`` / ``causal_chain``) over CAUSCORE's graph services.

CAUSCORE (``reva.causal``, contract ``docs/dev/v3_causal_interface.md``) serves claims as *claim JSON*: cause and
effect, relation, lane, stance, the asserting ``source`` (key, tier, dataset, revision, statement id or dataset
row selector, raw sha256, references, citation, codebook), the verbatim ``mechanism`` and the current ``grade``
object (grade, rule ids, temporal check, corroboration, contesting claims). :func:`claim_from_json` maps one to a
:class:`~reva.nlqa.ports.CausalClaim` and **refuses anything that is not a sourced claim of tier T1–T4**: a
candidate (T5/T6) or a row without a source never reaches an answer.

The adapter takes the two service calls as callables returning claim JSON (``explain`` → ``{"claims": [...],
"dissent": [...]}`` or a list; ``chains`` → chain JSON with ``paths[].hops[].claims``), so it is independent of
how CAUSCORE names its functions; :func:`wire_causal` binds them when the service module is importable, else the
backend stays absent (``capability_unavailable``) — never a fake.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from reva.kernel.errors import RevaError
from reva.nlqa.ports import (
    CLAIM_TIERS,
    CapabilityUnavailable,
    CausalChain,
    CausalChainHop,
    CausalClaim,
    CausalExplanation,
)

log = logging.getLogger("reva.nlqa.causal_adapter")

_GRADES = frozenset("ABCDE")


def _labels(node: Any) -> dict[str, str]:
    if not isinstance(node, Mapping):
        return {}
    out: dict[str, str] = {}
    labels = node.get("labels")
    if isinstance(labels, Mapping):
        out.update({str(k): str(v) for k, v in labels.items() if isinstance(v, str) and v})
    for k, lang in (("label_fr", "fr"), ("label_en", "en")):
        if isinstance(node.get(k), str) and node[k]:
            out.setdefault(lang, node[k])
    if isinstance(node.get("label"), str) and node["label"]:
        out.setdefault("en", node["label"])
        out.setdefault("fr", node["label"])
    return out


def _id(node: Any) -> str:
    if isinstance(node, Mapping):
        return str(node.get("id") or node.get("qid") or node.get("key") or "")
    return "" if node is None else str(node)


def _ids(v: Any) -> tuple[str, ...]:
    if not isinstance(v, list | tuple):
        return ()
    out = []
    for x in v:
        if isinstance(x, Mapping):
            x = x.get("claim_key") or x.get("id")
        if x:
            out.append(str(x))
    return tuple(out)


def claim_from_json(d: Mapping[str, Any]) -> CausalClaim | None:
    """CAUSCORE claim JSON → :class:`CausalClaim`; ``None`` for anything that is not a sourced T1–T4 claim."""
    if not isinstance(d, Mapping):
        return None
    raw_src = d.get("source")
    src: Mapping[str, Any] = raw_src if isinstance(raw_src, Mapping) else {}
    tier = str(src.get("tier") or d.get("tier") or "").upper()
    source_key = src.get("source_key") or d.get("source_key")
    key = d.get("claim_key") or d.get("id")
    cause, effect = d.get("cause"), d.get("effect")
    relation = d.get("relation")
    if tier not in CLAIM_TIERS or not source_key or not key or not relation or not _id(cause) or not _id(effect):
        return None
    g = d.get("grade")
    grade_obj: Mapping[str, Any] = g if isinstance(g, Mapping) else {}
    grade = grade_obj.get("grade") if isinstance(g, Mapping) else g
    grade = str(grade).upper() if grade is not None else None
    if grade is not None and grade not in _GRADES:
        return None
    corr = grade_obj.get("corroboration")
    return CausalClaim(
        claim_key=str(key),
        cause_id=_id(cause),
        effect_id=_id(effect),
        relation=str(relation),
        source_key=str(source_key),
        tier=tier,
        cause_labels=_labels(cause),
        effect_labels=_labels(effect),
        lane=d.get("lane") if isinstance(d.get("lane"), str) else None,
        stance="denies" if d.get("stance") == "denies" else "asserts",
        polarity=d.get("polarity") if isinstance(d.get("polarity"), str) else None,
        dataset=src.get("dataset"),
        source_revision=str(src["source_revision"]) if src.get("source_revision") is not None else None,
        statement_id=src.get("statement_id"),
        selector=src.get("selector") if isinstance(src.get("selector"), Mapping) else None,
        raw_sha256=src.get("raw_sha256"),
        citation=src.get("citation") if isinstance(src.get("citation"), Mapping) else None,
        codebook=src.get("codebook") if isinstance(src.get("codebook"), Mapping) else None,
        references=tuple(r for r in src.get("references") or () if isinstance(r, Mapping)),
        mechanism=d.get("mechanism") if isinstance(d.get("mechanism"), str) and d.get("mechanism") else None,
        grade=grade,
        grade_rules=tuple(str(r) for r in grade_obj.get("rule_ids") or ()),
        rules_version=str(grade_obj["rules_version"]) if grade_obj.get("rules_version") is not None else None,
        temporal_check=grade_obj.get("temporal_check") if isinstance(grade_obj.get("temporal_check"), str) else None,
        corroboration=corr if isinstance(corr, int) and not isinstance(corr, bool) else None,
        contested_by=_ids(grade_obj.get("contested_by")),
        review_status=d.get("review_status") if isinstance(d.get("review_status"), str) else None,
        origin=d.get("origin") if isinstance(d.get("origin"), str) else None,
    )


def _claims(raw: Any) -> list[CausalClaim]:
    out = []
    for x in raw if isinstance(raw, list | tuple) else ():
        c = claim_from_json(x)
        if c is not None:
            out.append(c)
    return out


def _lists(data: Any, keys: Sequence[str]) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, Mapping):
        for k in keys:
            v = data.get(k)
            if isinstance(v, list) and k == "lanes":  # why lanes: [{"lane": "immediate", "claims": [...]}, ...]
                return [c for lane in v if isinstance(lane, Mapping) for c in lane.get("claims") or []]
            if isinstance(v, list):
                return v
            if isinstance(v, Mapping):  # why lanes: {"immediate": [...], ...}
                return [c for lane in v.values() for c in (lane if isinstance(lane, list) else [])]
    return []


def chains_from_json(data: Any) -> list[CausalChain]:
    """Chain JSON (``paths[].hops[].claims``) → chains; a hop keeps only sourced T1–T4 claims."""
    paths = (data.get("paths") or data.get("chains") or []) if isinstance(data, Mapping) else (data or [])
    out: list[CausalChain] = []
    for p in paths if isinstance(paths, list) else []:
        hops_raw = p.get("hops") if isinstance(p, Mapping) else p
        hops: list[CausalChainHop] = []
        for h in hops_raw if isinstance(hops_raw, list) else []:
            if not isinstance(h, Mapping):
                continue
            claims = _claims(h.get("claims") if isinstance(h.get("claims"), list) else [h])
            first = claims[0] if claims else None
            hops.append(
                CausalChainHop(
                    from_id=_id(h.get("from")) or (first.cause_id if first else ""),
                    to_id=_id(h.get("to")) or (first.effect_id if first else ""),
                    claims=tuple(claims),
                    from_labels=_labels(h.get("from")) or (dict(first.cause_labels) if first else {}),
                    to_labels=_labels(h.get("to")) or (dict(first.effect_labels) if first else {}),
                )
            )
        out.append(CausalChain(tuple(hops)))
    return out


class CausalServiceAdapter:
    """:class:`~reva.nlqa.ports.CausalPort` over two callables returning CAUSCORE JSON."""

    name = "REVA causal claims (reva.causal)"

    def __init__(
        self,
        explain: Callable[[str, str, int], Any],
        chains: Callable[[str, str, int, int], Any],
        *,
        name: str | None = None,
    ) -> None:
        self._explain = explain
        self._chains = chains
        if name:
            self.name = name

    def explain(self, event_id: str, *, direction: str, limit: int) -> CausalExplanation:
        try:
            data = self._explain(event_id, direction, limit)
        except CapabilityUnavailable:
            raise
        except (RevaError, LookupError) as e:  # unknown event → no claims, said as missing
            if getattr(e, "http_status", None) == 404 or isinstance(e, LookupError):
                return CausalExplanation(event_id, direction, ())
            raise CapabilityUnavailable(f"causal service failed: {type(e).__name__}", backend="causal") from None
        except Exception as e:
            log.warning("causal explain failed: %s", type(e).__name__)
            raise CapabilityUnavailable(f"causal service failed: {type(e).__name__}", backend="causal") from None
        keys = ["claims", "causes"] if direction == "causes" else ["claims", "effects"]
        dissent = data.get("dissent") if isinstance(data, Mapping) else None
        if isinstance(dissent, Mapping):
            dissent = dissent.get("claims")
        claims = _claims(_lists(data, [*keys, "lanes"]))
        ev = data.get("event") if isinstance(data, Mapping) else None
        truncated = bool(isinstance(data, Mapping) and data.get("truncated"))
        return CausalExplanation(
            event_id, direction, tuple(claims), tuple(_claims(dissent)), _labels(ev), truncated=truncated
        )

    def chains(self, from_id: str, to_id: str, *, max_hops: int, limit: int) -> list[CausalChain]:
        try:
            data = self._chains(from_id, to_id, max_hops, limit)
        except CapabilityUnavailable:
            raise
        except (RevaError, LookupError) as e:
            if getattr(e, "http_status", None) == 404 or isinstance(e, LookupError):
                return []
            raise CapabilityUnavailable(f"causal chain search failed: {type(e).__name__}", backend="causal") from None
        except Exception as e:
            log.warning("causal chain failed: %s", type(e).__name__)
            raise CapabilityUnavailable(f"causal chain search failed: {type(e).__name__}", backend="causal") from None
        return chains_from_json(data)[:limit]


def wire_causal(engine: Any, *, origin: str | None = None) -> CausalServiceAdapter | None:
    """Bind CAUSCORE's read services (:mod:`reva.causal.graph`): causes through ``why`` (lanes + dissent),
    effects through ``effects``, chains through ``chain`` (only ``asserts`` claims of the ``cause`` family are
    traversed, graded at least D, ≤ 6 hops). ``None`` when the module is absent (capability unavailable).
    ``origin``: ``live`` in production (as the causal routes), else any origin."""
    try:
        from reva.causal import graph
    except ImportError:
        return None

    def explain(event_id: str, direction: str, limit: int) -> Any:
        with engine.connect() as conn:
            if direction == "causes":
                return graph.why(conn, event_id, origin=origin)
            return graph.effects(conn, event_id, origin=origin, limit=max(1, min(limit, graph.MAX_CLAIMS)))

    def chains(a: str, b: str, max_hops: int, limit: int) -> Any:
        with engine.connect() as conn:
            return graph.chain(
                conn,
                a,
                b,
                max_hops=min(max_hops, graph.MAX_HOPS),
                max_paths=max(1, min(limit, graph.MAX_PATHS)),
                origin=origin,
            )

    return CausalServiceAdapter(explain, chains)
