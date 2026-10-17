"""The typed query plan: a closed set of operations, validated server-side.

The LLM (or the keyword fallback) only proposes a plan; :func:`validate_plan` turns the
untrusted JSON into a :class:`QueryPlan` and enforces:

* operations from the closed set :data:`OPS` only, at most :data:`MAX_OPERATIONS`;
* entity references are *lookup refs* (ids of earlier ``entity_lookup`` steps): the model
  never supplies a QID — entity resolution happens on the server;
* concept ids, ISO3 codes, KG property ids and link kinds from the allow-lists in
  :class:`~reva.nlqa.catalog.QaCatalog`; anything else **rejects that operation** (and every
  operation depending on it) with a typed reason — never a substitution;
* bounded numbers (years, hops, limits); a server limit (hops ≤ 4, limit ≤ 50) is applied
  with a warning, a user-stated year is never changed (out of range → rejected);
* an ``entity_lookup`` text must come from the question (:func:`lookup_from_question`: the words
  as written or their dictionary form, e.g. "Brésil" for "Brésiliens"); a lookup the planner
  added on its own is rejected (``lookup_not_in_question``), with the operations that use it;
* at most :data:`MAX_PATH_OPERATIONS` path searches (``kg_path``, ``causal_chain``) per question
  (``too_many_path_operations``);
* causal operations ``why`` (causes or effects of a looked-up event) and ``causal_chain`` (≤ 6 hops) only name
  entities through lookup refs; they are answered from sourced causal claims only (never candidates).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from reva.nlqa.catalog import ENTITY_KINDS, QaCatalog, fold

OPS: tuple[str, ...] = (
    "entity_lookup",
    "kg_neighbours",
    "kg_timeline",
    "kg_path",
    "atlas_value",
    "atlas_rank",
    "atlas_series",
    "links",
    "links_pair",
    "why",
    "causal_chain",
    # V6 (agent NLQA6): relation families, cross-family lists, effect estimates, sealed forecasts, decision
    # scenarios and geography-free atlas series
    "kg_families",
    "kg_cross",
    "effects",
    "forecast",
    "decision",
    "atlas_period",
)
#: Reading of a family from the looked-up entity: as the source states it (``forward``), its inverse, or both.
READINGS = ("forward", "inverse", "any")
#: Units a decision magnitude may be stated in (normalised by the keyword planner; checked against the instrument).
DECISION_UNITS = ("bp", "pp", "percent", "mb/d", "years")
_MAGNITUDE_RE = re.compile(r"^-?\d{1,6}(\.\d{1,4})?$")
#: Causal operations (v3_causality.md): answered only from claims asserted by named sources (T1–T4).
CAUSAL_DIRECTIONS = ("causes", "effects")
MAX_CHAIN_HOPS = 6
DEFAULT_CHAIN_HOPS = 4
MAX_OPERATIONS = 12
#: Path searches are the expensive operations: at most this many per question (SEC-1).
MAX_PATH_OPERATIONS = 3
MAX_HOPS = 4
MAX_LIMIT = 50
DEFAULT_LIMIT = 10
MIN_YEAR, MAX_YEAR = -3000, 2100
MAX_LOOKUP_CHARS = 120
MAX_TEXT_CHARS = 300
_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,15}$")
_PID_RE = re.compile(r"^P[1-9][0-9]{0,6}$")
_ISO3_RE = re.compile(r"^[A-Z]{3}$")
_AS_OF_RE = re.compile(r"^-?\d{1,4}(-\d{2}(-\d{2})?)?$")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def _clean(v: Any, n: int = MAX_TEXT_CHARS) -> str:
    return _CTRL.sub(" ", str(v if v is not None else "")).strip()[:n]


@dataclass
class PlanOp:
    id: str
    op: str
    args: dict[str, Any]

    def as_json(self) -> dict[str, Any]:
        return {"id": self.id, "op": self.op, **self.args}


@dataclass
class QueryPlan:
    language: str
    operations: list[PlanOp]
    unsupported: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    llm_restatement: str = ""
    planner: str = "llm"  # llm | keyword_fallback
    #: Several catalog concepts matching the question equally (keyword planner): the candidates and how
    #: the tie was handled (``headline`` chose one, ``each_queried``, or ``unresolved`` → reported).
    ambiguities: list[dict[str, Any]] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return {
            "planner": self.planner,
            "language": self.language,
            "operations": [o.as_json() for o in self.operations],
            "rejected": self.rejected,
            **({"ambiguities": self.ambiguities} if self.ambiguities else {}),
        }

    def op(self, op_id: str) -> PlanOp | None:
        return next((o for o in self.operations if o.id == op_id), None)


def in_question(fragment: str, question: str) -> bool:
    """True when ``fragment`` occurs in the question (accent/case-insensitive) — the only way an
    LLM-written string may be quoted in an answer."""
    f = fold(fragment)
    return bool(f) and f in fold(question)


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", fold(text))


def lookup_from_question(text: str, question: str) -> bool:
    """True when a lookup text comes from the question: it occurs in it, or each of its words of 3+
    characters shares its first 5 characters (the whole word when shorter) with a word of the question
    (inflections and dictionary forms: "Brésil"/"Brésiliens", "portugais"/"portugaise")."""
    t = fold(text)
    if t and re.search(r"(?<!\w)" + re.escape(t) + r"(?!\w)", fold(question)):
        return True
    qwords = _words(question)
    words = [w for w in _words(text) if len(w) >= 3]
    if not words:
        return False
    for w in words:
        stem = w[:5]
        if not any(q.startswith(stem) or (len(q) >= 5 and w.startswith(q[:5])) for q in qwords):
            return False
    return True


def _year(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int | float) or int(v) != v:
        raise ValueError("year must be an integer")
    y = int(v)
    if not MIN_YEAR <= y <= MAX_YEAR:
        raise ValueError(f"year {y} outside {MIN_YEAR}..{MAX_YEAR}")
    return y


_HEADLINE_ID_RE = re.compile(r"^reva\.[a-z0-9_]+\.[a-z0-9_.]{1,120}$")
_RESOLUTIONS = ("headline", "each_queried", "unresolved")


def _ambiguities(raw: Any, catalog: QaCatalog) -> list[dict[str, Any]]:
    """Concept ambiguities of a plan, kept only with catalog candidates (identifiers never invented)."""
    out: list[dict[str, Any]] = []
    for a in raw if isinstance(raw, list) else []:
        if not isinstance(a, Mapping) or a.get("resolution") not in _RESOLUTIONS:
            continue
        cands = [c for c in a.get("candidates") or [] if isinstance(c, str) and c in catalog.concepts][:12]
        if len(cands) < 2:
            continue
        item: dict[str, Any] = {"candidates": cands, "resolution": a["resolution"]}
        if a["resolution"] == "headline":
            chosen, head = a.get("chosen"), a.get("headline")
            if chosen not in cands or not isinstance(head, str) or not _HEADLINE_ID_RE.match(head):
                continue
            item.update(chosen=chosen, headline=head)
        out.append(item)
    return out[:3]


class _Reject(Exception):
    def __init__(self, reason: str, **details: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details


def validate_plan(raw: Any, catalog: QaCatalog, question: str, *, planner: str = "llm") -> QueryPlan:
    """Untrusted planner output → :class:`QueryPlan` (rejects, never substitutes)."""
    if not isinstance(raw, Mapping):
        raise ValueError("plan is not a JSON object")
    lang = raw.get("input_language")
    lang = lang if lang in ("fr", "en") else ("fr" if _looks_french(question) else "en")
    plan = QueryPlan(language=lang, operations=[], planner=planner)
    plan.llm_restatement = _clean(raw.get("restatement"), 600)
    for u in raw.get("unsupported") or []:
        if isinstance(u, Mapping):
            text = _clean(u.get("text"))
            plan.unsupported.append(
                {
                    "text": text,
                    "reason": _clean(u.get("reason")),
                    "source": planner,
                    "in_question": in_question(text, question),
                }
            )
    plan.ambiguities = _ambiguities(raw.get("ambiguities"), catalog)
    ops = raw.get("operations") or []
    if not isinstance(ops, list):
        raise ValueError("operations is not a list")
    if len(ops) > MAX_OPERATIONS:
        plan.warnings.append(f"plan_truncated: {len(ops)} operations proposed, the first {MAX_OPERATIONS} kept")
        for extra in ops[MAX_OPERATIONS:]:
            plan.rejected.append(
                {
                    "id": _clean((extra or {}).get("id"), 16),
                    "op": _clean((extra or {}).get("op"), 32),
                    "reason": "too_many_operations",
                }
            )
        ops = ops[:MAX_OPERATIONS]
    lookups: dict[str, str | None] = {}  # id -> kind
    seen: set[str] = set()
    n_paths = 0
    for i, o in enumerate(ops):
        if not isinstance(o, Mapping):
            plan.rejected.append({"id": f"#{i}", "op": "", "reason": "not_an_object"})
            continue
        op_id = str(o.get("id") or f"s{i + 1}")
        name = str(o.get("op") or "")
        try:
            if not _ID_RE.match(op_id) or op_id in seen:
                raise _Reject("invalid_or_duplicate_id")
            if name not in OPS:
                raise _Reject("unknown_operation", op=_clean(name, 40))
            args = _validate_args(name, o, catalog, lookups, plan)
            if name == "entity_lookup" and not lookup_from_question(args["text"], question):
                raise _Reject("lookup_not_in_question")
            if name == "effects" and not all(
                lookup_from_question(args[k], question) for k in ("cause_text", "effect_text") if args.get(k)
            ):
                raise _Reject("text_not_in_question")
            if name in ("forecast", "decision") and not lookup_from_question(args["text"], question):
                raise _Reject("text_not_in_question")
            if name in ("kg_path", "causal_chain", "kg_cross"):
                if n_paths >= MAX_PATH_OPERATIONS:
                    raise _Reject("too_many_path_operations")
                n_paths += 1
        except _Reject as r:
            plan.rejected.append({"id": _clean(op_id, 16), "op": _clean(name, 32), "reason": r.reason, **r.details})
            continue
        except ValueError as e:
            plan.rejected.append(
                {"id": _clean(op_id, 16), "op": _clean(name, 32), "reason": "invalid_argument", "detail": str(e)[:200]}
            )
            continue
        seen.add(op_id)
        if name == "entity_lookup":
            lookups[op_id] = args.get("kind")
        plan.operations.append(PlanOp(op_id, name, args))
    for r in plan.rejected:
        plan.unsupported.append(
            {
                "text": "",
                "reason": f"operation {r.get('op') or '?'} rejected: {r['reason']}"
                + (f" ({r['value']})" if r.get("value") else ""),
                "source": "server",
                "in_question": False,
            }
        )
    return plan


def _ref(o: Mapping[str, Any], key: str, lookups: Mapping[str, Any]) -> str:
    v = o.get(key)
    if not isinstance(v, str) or v not in lookups:
        raise _Reject("unknown_entity_ref", field=key, value=_clean(v, 40))
    return v


def _concept(o: Mapping[str, Any], catalog: QaCatalog) -> str:
    c = o.get("concept")
    if not isinstance(c, str) or c not in catalog.concepts:
        raise _Reject("concept_not_in_catalog", value=_clean(c, 80))
    return c


def _country(
    o: Mapping[str, Any], catalog: QaCatalog, lookups: Mapping[str, Any], *, allow_all: bool, key: str = "country"
) -> str:
    c = o.get(key)
    if isinstance(c, str) and c in lookups:
        return c
    if isinstance(c, str) and c == "ALL" and allow_all:
        return c
    if isinstance(c, str) and _ISO3_RE.match(c) and c in catalog.countries:
        return c
    raise _Reject("country_not_in_list", value=_clean(c, 40))


def _limit(o: Mapping[str, Any], plan: QueryPlan) -> int:
    v = o.get("limit")
    if v is None:
        return DEFAULT_LIMIT
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        raise ValueError("limit must be a positive integer")
    if v > MAX_LIMIT:
        plan.warnings.append(f"limit_capped: {v} -> {MAX_LIMIT}")
        return MAX_LIMIT
    return int(v)


def _validate_args(
    name: str, o: Mapping[str, Any], catalog: QaCatalog, lookups: Mapping[str, Any], plan: QueryPlan
) -> dict[str, Any]:
    if name == "entity_lookup":
        text = _clean(o.get("text"), MAX_LOOKUP_CHARS + 1)
        if not text or len(text) > MAX_LOOKUP_CHARS:
            raise ValueError("lookup text empty or too long")
        kind = o.get("kind")
        if kind is not None and kind not in ENTITY_KINDS:
            kind = None
        return {"text": text, "kind": kind}
    if name in ("kg_neighbours", "kg_timeline"):
        args: dict[str, Any] = {"entity": _ref(o, "entity", lookups)}
        if name == "kg_neighbours":
            props = o.get("properties") or []
            if not isinstance(props, list):
                raise ValueError("properties must be a list")
            bad = [p for p in props if not (isinstance(p, str) and _PID_RE.match(p) and p in catalog.properties)]
            if bad:
                raise _Reject("property_not_in_list", value=", ".join(_clean(b, 12) for b in bad[:5]))
            args["properties"] = list(dict.fromkeys(props))[:20]
            args["as_of"] = _as_of(o.get("as_of"))
        else:
            args["from_year"], args["to_year"] = _range(o)
        return args
    if name == "kg_path":
        a, b = _ref(o, "from_entity", lookups), _ref(o, "to_entity", lookups)
        if a == b:
            raise _Reject("path_to_itself")
        hops = o.get("max_hops")
        if hops is None:
            hops = 3
        if isinstance(hops, bool) or not isinstance(hops, int) or hops < 1:
            raise ValueError("max_hops must be a positive integer")
        if hops > MAX_HOPS:
            plan.warnings.append(f"max_hops_capped: {hops} -> {MAX_HOPS}")
            hops = MAX_HOPS
        return {"from_entity": a, "to_entity": b, "max_hops": int(hops), "as_of": _as_of(o.get("as_of"))}
    if name == "why":
        d = o.get("causal_direction") or "causes"
        if d not in CAUSAL_DIRECTIONS:
            raise ValueError("causal_direction must be causes or effects")
        return {"entity": _ref(o, "entity", lookups), "direction": d}
    if name == "causal_chain":
        a, b = _ref(o, "from_entity", lookups), _ref(o, "to_entity", lookups)
        if a == b:
            raise _Reject("path_to_itself")
        hops = o.get("max_hops")
        if hops is None:
            hops = DEFAULT_CHAIN_HOPS
        if isinstance(hops, bool) or not isinstance(hops, int) or hops < 1:
            raise ValueError("max_hops must be a positive integer")
        if hops > MAX_CHAIN_HOPS:
            plan.warnings.append(f"max_hops_capped: {hops} -> {MAX_CHAIN_HOPS}")
            hops = MAX_CHAIN_HOPS
        return {"from_entity": a, "to_entity": b, "max_hops": int(hops)}
    if name == "kg_families":
        fams = _families(o.get("families"), catalog, required=True)
        reading = o.get("reading") or "forward"
        if reading not in READINGS:
            raise ValueError("reading must be forward, inverse or any")
        return {
            "entity": _ref(o, "entity", lookups),
            "families": fams,
            "reading": reading,
            "count": bool(o.get("count")),
        }
    if name == "kg_cross":
        a, b = _ref(o, "entity", lookups), _ref(o, "other_entity", lookups)
        if a == b:
            raise _Reject("path_to_itself")
        return {
            "entity": a,
            "families": _families(o.get("families"), catalog, required=False),
            "other_entity": b,
            "other_families": _families(o.get("other_families"), catalog, required=True),
            "limit": _limit(o, plan),
        }
    if name == "effects":
        cause, effect = _clean(o.get("cause_text"), MAX_LOOKUP_CHARS), _clean(o.get("effect_text"), MAX_LOOKUP_CHARS)
        if not cause and not effect:
            raise ValueError("cause_text or effect_text required")
        country = o.get("country")
        return {
            "cause_text": cause,
            "effect_text": effect,
            "country": None if country in (None, "") else _country(o, catalog, lookups, allow_all=False),
        }
    if name == "forecast":
        text = _clean(o.get("text"), MAX_LOOKUP_CHARS + 1)
        if not text or len(text) > MAX_LOOKUP_CHARS:
            raise ValueError("forecast text empty or too long")
        country = o.get("country")
        return {
            "text": text,
            "country": None if country in (None, "") else _country(o, catalog, lookups, allow_all=False),
        }
    if name == "decision":
        text = _clean(o.get("text"), MAX_TEXT_CHARS)
        if not text:
            raise ValueError("decision text empty")
        mag, unit = o.get("magnitude"), o.get("unit")
        if mag is not None and (not isinstance(mag, str) or not _MAGNITUDE_RE.match(mag)):
            raise ValueError("magnitude must be a decimal string")
        if unit is not None and unit not in DECISION_UNITS:
            raise _Reject("unit_not_in_list", value=_clean(unit, 20))
        if (mag is None) != (unit is None):
            raise ValueError("magnitude and unit go together")
        return {"text": text, "magnitude": mag, "unit": unit}
    if name == "atlas_period":
        f, t = _range(o)
        return {"concept": _concept(o, catalog), "from_year": f, "to_year": t}
    if name == "atlas_value":
        return {
            "concept": _concept(o, catalog),
            "country": _country(o, catalog, lookups, allow_all=True),
            "year": _year(o.get("year")),
        }
    if name == "atlas_rank":
        order = o.get("order") or "desc"
        if order not in ("desc", "asc"):
            raise ValueError("order must be desc or asc")
        return {"concept": _concept(o, catalog), "year": _year(o.get("year")), "order": order, "limit": _limit(o, plan)}
    if name == "atlas_series":
        f, t = _range(o)
        return {
            "concept": _concept(o, catalog),
            "country": _country(o, catalog, lookups, allow_all=False),
            "from_year": f,
            "to_year": t,
        }
    if name in ("links", "links_pair"):
        kinds = o.get("link_kinds") or []
        if not isinstance(kinds, list):
            raise ValueError("link_kinds must be a list")
        bad = [k for k in kinds if k not in catalog.link_kinds]
        if bad:
            raise _Reject("link_kind_not_in_list", value=", ".join(_clean(b, 30) for b in bad[:5]))
        if name == "links_pair":
            a = _country(o, catalog, lookups, allow_all=False)
            b = _country(o, catalog, lookups, allow_all=False, key="other_country")
            if a == b:
                raise _Reject("pair_with_itself")
            f, t = _range(o)
            return {"country": a, "other_country": b, "kinds": list(dict.fromkeys(kinds)), "from_year": f, "to_year": t}
        return {
            "country": _country(o, catalog, lookups, allow_all=False),
            "kinds": list(dict.fromkeys(kinds)),
            "limit": _limit(o, plan),
        }
    raise _Reject("unknown_operation")


def _families(v: Any, catalog: QaCatalog, *, required: bool) -> list[str]:
    if v is None:
        v = []
    if not isinstance(v, list):
        raise ValueError("families must be a list")
    bad = [f for f in v if not (isinstance(f, str) and f in catalog.families)]
    if bad:
        raise _Reject("family_not_in_list", value=", ".join(_clean(b, 30) for b in bad[:5]))
    if required and not v:
        raise ValueError("families must name at least one relation family")
    return list(dict.fromkeys(v))[:8]


def _range(o: Mapping[str, Any]) -> tuple[int | None, int | None]:
    f, t = _year(o.get("from_year")), _year(o.get("to_year"))
    if f is not None and t is not None and f > t:
        raise ValueError("from_year after to_year")
    return f, t


def _as_of(v: Any) -> str | None:
    if v is None or v == "":
        return None
    if isinstance(v, int) and not isinstance(v, bool):
        v = str(v)
    if not isinstance(v, str) or not _AS_OF_RE.match(v):
        raise ValueError("as_of must be YYYY, YYYY-MM or YYYY-MM-DD")
    return v


_FR_HINTS = re.compile(
    r"\b(le|la|les|des|du|est|sont|quel(le)?s?|comment|pourquoi|quand|où|ou|au|aux|une?|pays|salaire|"
    r"position|depuis|entre|avec|sur|dans|années)\b",
    re.IGNORECASE,
)
_EN_HINTS = re.compile(r"\b(the|is|are|what|which|how|why|when|where|of|in|with|between|countries)\b", re.IGNORECASE)


def _looks_french(text: str) -> bool:
    return len(_FR_HINTS.findall(text)) > len(_EN_HINTS.findall(text))


def detect_language(text: str) -> str:
    return "fr" if _looks_french(text) else "en"


def restate(plan: QueryPlan, catalog: QaCatalog) -> str:
    """Server restatement of the validated plan (fr/en); LLM text never enters it except lookup
    search terms, shown in quotes as search terms."""
    fr = plan.language == "fr"
    lookups = {o.id: o.args for o in plan.operations if o.op == "entity_lookup"}

    def ent(ref: str) -> str:
        a = lookups.get(ref) or {}
        return f"« {a.get('text', ref)} »" if fr else f"“{a.get('text', ref)}”"

    def country(c: str) -> str:
        if c in lookups:
            return ent(c)
        if c == "ALL":
            return "tous les pays" if fr else "all countries"
        return catalog.country_label(c, plan.language)

    def concept(cid: str) -> str:
        c = catalog.concepts.get(cid)
        return c.label(plan.language) if c else cid

    def year(y: Any) -> str:
        return str(y) if y is not None else ("dernière année disponible" if fr else "latest available year")

    parts: list[str] = []
    for o in plan.operations:
        a = o.args
        if o.op == "entity_lookup":
            continue
        if o.op == "kg_neighbours":
            props = ", ".join(
                catalog.properties[p].labels.get("en", p) if p in catalog.properties else p for p in a["properties"]
            )
            what = (
                (f"les relations ({props})" if props else "les relations")
                if fr
                else (f"the relations ({props})" if props else "the relations")
            )
            parts.append(f"{what} de {ent(a['entity'])}" if fr else f"{what} of {ent(a['entity'])}")
        elif o.op == "kg_timeline":
            parts.append(f"la chronologie de {ent(a['entity'])}" if fr else f"the timeline of {ent(a['entity'])}")
        elif o.op == "kg_path":
            parts.append(
                f"les chemins (≤ {a['max_hops']} étapes) reliant {ent(a['from_entity'])} à {ent(a['to_entity'])}"
                if fr
                else f"the paths (≤ {a['max_hops']} hops) connecting {ent(a['from_entity'])} to {ent(a['to_entity'])}"
            )
        elif o.op == "why":
            if a["direction"] == "effects":
                parts.append(
                    f"les conséquences de {ent(a['entity'])} affirmées par des sources"
                    if fr
                    else f"the consequences of {ent(a['entity'])} asserted by sources"
                )
            else:
                parts.append(
                    f"les causes de {ent(a['entity'])} affirmées par des sources"
                    if fr
                    else f"the causes of {ent(a['entity'])} asserted by sources"
                )
        elif o.op == "causal_chain":
            parts.append(
                f"les chaînes d'affirmations causales sourcées (≤ {a['max_hops']} maillons) de {ent(a['from_entity'])} "
                f"à {ent(a['to_entity'])}"
                if fr
                else f"the chains of sourced causal claims (≤ {a['max_hops']} hops) from {ent(a['from_entity'])} "
                f"to {ent(a['to_entity'])}"
            )
        elif o.op == "atlas_value":
            parts.append(f"{concept(a['concept'])} — {country(a['country'])}, {year(a['year'])}")
        elif o.op == "atlas_rank":
            order = (
                ("les plus élevés" if a["order"] == "desc" else "les plus faibles")
                if fr
                else ("highest" if a["order"] == "desc" else "lowest")
            )
            parts.append(
                f"le classement ({order}, {a['limit']} pays) de {concept(a['concept'])}, {year(a['year'])}"
                if fr
                else f"the ranking ({order}, {a['limit']} countries) of {concept(a['concept'])}, {year(a['year'])}"
            )
        elif o.op == "atlas_series":
            span = f"{a['from_year'] or '…'}–{a['to_year'] or '…'}"
            parts.append(f"{concept(a['concept'])} — {country(a['country'])}, {span}")
        elif o.op == "links":
            kinds = ", ".join(a["kinds"]) or ("tous types" if fr else "all kinds")
            parts.append(
                f"les liens ({kinds}) de {country(a['country'])}"
                if fr
                else f"the links ({kinds}) of {country(a['country'])}"
            )
        elif o.op == "links_pair":
            kinds = ", ".join(a["kinds"]) or ("tous types" if fr else "all kinds")
            span = (
                f", {a['from_year'] or '…'}–{a['to_year'] or '…'}"
                if a.get("from_year") is not None or a.get("to_year") is not None
                else ""
            )
            parts.append(
                f"les liens ({kinds}{span}) entre {country(a['country'])} et {country(a['other_country'])}"
                if fr
                else f"the links ({kinds}{span}) between {country(a['country'])} and {country(a['other_country'])}"
            )
        elif o.op in ("kg_families", "kg_cross"):
            fams = ", ".join(
                (catalog.families.get(f) or {}).get(plan.language, f) for f in (a.get("families") or [])
            ) or ("toute relation" if fr else "any relation")
            if o.op == "kg_families":
                what = ("le nombre de rattachements" if fr else "the number of links") if a["count"] else (
                    "les relations" if fr else "the relations"
                )
                inv = {"inverse": " (lecture inverse)" if fr else " (inverse reading)", "any": ""}.get(a["reading"], "")
                parts.append(
                    f"{what} « {fams} »{inv} de {ent(a['entity'])}" if fr else f"{what} “{fams}”{inv} of {ent(a['entity'])}"
                )
            else:
                other = ", ".join(
                    (catalog.families.get(f) or {}).get(plan.language, f) for f in a["other_families"]
                )
                parts.append(
                    f"les entités reliées à {ent(a['entity'])} ({fams}) et à {ent(a['other_entity'])} ({other})"
                    if fr
                    else f"the entities linked to {ent(a['entity'])} ({fams}) and to {ent(a['other_entity'])} ({other})"
                )
        elif o.op == "effects":
            c = f"« {a['cause_text']} »" if fr else f"“{a['cause_text']}”"
            e = f"« {a['effect_text']} »" if fr else f"“{a['effect_text']}”"
            where = f", {country(a['country'])}" if a.get("country") else ""
            parts.append(
                f"les estimations d'effet publiées de {c} sur {e}{where}"
                if fr
                else f"the published effect estimates of {c} on {e}{where}"
            )
        elif o.op == "forecast":
            where = f", {country(a['country'])}" if a.get("country") else ""
            parts.append(
                f"les prévisions scellées du registre REVA pour « {a['text']} »{where}"
                if fr
                else f"the sealed forecasts of REVA's registry for “{a['text']}”{where}"
            )
        elif o.op == "decision":
            mag = f" ({a['magnitude']} {a['unit']})" if a.get("magnitude") else ""
            parts.append(
                f"un scénario du moteur de décision (non validé) pour la décision décrite{mag}"
                if fr
                else f"a decision-engine scenario (unvalidated) for the decision described{mag}"
            )
        elif o.op == "atlas_period":
            span = f"{a['from_year'] or '…'}–{a['to_year'] or '…'}"
            parts.append(f"{concept(a['concept'])}, {span}")
    head = "Question interprétée comme : " if fr else "Question read as: "
    if not parts:
        body = "aucune requête exécutable" if fr else "no executable query"
    else:
        body = "; ".join(parts)
    tail = ""
    if plan.unsupported:
        tail = (
            f" Parties non couvertes : {len(plan.unsupported)}."
            if fr
            else f" Unsupported parts: {len(plan.unsupported)}."
        )
    origin = (" (plan par mots-clés, sans LLM)" if fr else " (keyword plan, no LLM)") if plan.planner != "llm" else ""
    return head + body + "." + origin + tail
