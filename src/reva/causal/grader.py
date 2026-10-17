"""The grader: current claims + KG event dates -> ``kg.causal_grade`` rows (``registry/causal/grading.yaml``).

Grades are recomputed from scratch for every current claim of an origin, but a row is written only when the
claim's grading *inputs* changed (rules version/sha, claim content, the dates used, corroboration groups,
contesting claims): an unchanged recompute writes nothing. Nothing is ever updated.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection, Engine, text

from reva.causal import grading as gr
from reva.causal import identity as idn
from reva.causal.model import _json, _new_load
from reva.causal.temporal import DATE_PIDS, EventDates, claim_time_window, dates_from_rows, temporal_detail
from reva.causal.vocab import vocabulary
from reva.kernel.errors import ValidationProblem
from reva.kg.query import is_community_edited

GRADER_SOURCE = "reva.causal.grader"
MAX_CLAIMS_GRADED = 1_000_000
ENTITY_CHUNK = 5_000
INSERT_CHUNK = 5_000
ORIGINS = ("live", "test_fixture")


@dataclass
class GradeReport:
    origin: str | None = None
    claims: int = 0
    written: int = 0
    unchanged: int = 0
    load_ids: list[int] = field(default_factory=list)
    rules_version: str = ""
    by_grade: dict[str, int] = field(default_factory=dict)
    identity: dict[str, Any] = field(default_factory=dict)
    elapsed_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def fetch_event_dates(conn: Connection, ids: Iterable[str], origin: str | None) -> dict[str, EventDates]:
    """KG dates (P580/P585/P571 start, P582/P585/P576 end) of entities, with precision windows."""
    qs = sorted(set(ids))
    rows: list[Mapping[str, Any]] = []
    for i in range(0, len(qs), ENTITY_CHUNK):
        rows += [
            dict(r)
            for r in conn.execute(
                text(f"""SELECT s.subject_qid, s.property_pid, s.rank, s.value->>'time' AS time,
                               (s.value->>'precision')::int AS precision,
                               to_char(s.value_time_lo, 'YYYY-MM-DD BC') AS lo,
                               to_char(s.value_time_hi, 'YYYY-MM-DD BC') AS hi
                          FROM kg.statement_current s
                         WHERE s.subject_qid = ANY(:q) AND s.property_pid = ANY(:p)
                           AND s.value_time_lo IS NOT NULL AND s.value_time_hi IS NOT NULL
                           {"AND s.origin = :o" if origin else ""}"""),
                {"q": qs[i : i + ENTITY_CHUNK], "p": list(DATE_PIDS), "o": origin},
            )
            .mappings()
            .all()
        ]
    return dates_from_rows(rows)


@dataclass(frozen=True)
class Computed:
    claim_row_id: int
    claim_key: str
    grade: gr.GradeResult
    temporal_check: str
    temporal: dict[str, Any]
    corroboration: gr.Corroboration
    corroborating: list[str]
    contested_by: list[str]
    inputs_sha256: str


def _qualified(c: Mapping[str, Any]) -> tuple[bool, bool, bool]:
    """(attributed P3680, nature-qualified P5102, source codes it doubtful) — audit H3 / H4, grading.yaml v2."""
    q = c.get("qualifiers") or {}
    validity = (c.get("scope") or {}).get("source_validity") or []
    doubtful = any(isinstance(v, Mapping) and v.get("certainty") == "doubtful" for v in validity)
    return bool(q.get(gr.SUPPORTED_BY_PID)), bool(q.get(gr.NATURE_OF_STATEMENT_PID)), doubtful


def _hedged(c: Mapping[str, Any]) -> bool:
    """P1480 "sourcing circumstances" qualifier present (audit2 N2, grading.yaml v3)."""
    return bool((c.get("qualifiers") or {}).get(gr.SOURCING_CIRCUMSTANCES_PID))


def _scope_qualified(c: Mapping[str, Any]) -> bool:
    q = c.get("qualifiers") or {}
    return any(q.get(p) for p in gr.SCOPE_QUALIFIER_PIDS)


EFFECT_DATED_BY_CAUSE_END = "effect_dated_by_cause_end"


def outcome_dated_by_cause_end(
    c: Mapping[str, Any], cause: EventDates | None, effect: EventDates | None, outcome_relations: Iterable[str]
) -> dict[str, Any] | None:
    """The cause's end window and the effect's start window when a coded outcome is dated by its cause's own end
    (the two windows are equal): "the cause starts before the effect" is then true by construction (audit2 N7)."""
    if c.get("relation") not in set(outcome_relations) or cause is None or effect is None:
        return None
    end, start = cause.end, effect.start
    if end is None or start is None or (end.lo, end.hi) != (start.lo, start.hi):
        return None
    return {"cause_end": end.as_dict(), "effect_start": start.as_dict()}


def structural_flags(claims: Sequence[Mapping[str, Any]], excluded_relations: Iterable[str] = ()) -> dict[int, str]:
    """``{claim row id: fact}`` for self-loops (``consistency.self_loop``) and for asserted causal claims whose
    cause and effect lie in the same strongly connected component (``consistency.cycle``) — audit L7. Such claims
    stay stored, graded and shown; chain composition does not traverse them."""
    from reva.causal.graph import _sccs  # local import: graph imports this module

    excl = set(excluded_relations)
    out: dict[int, str] = {}
    adj: dict[str, set[str]] = defaultdict(set)
    for c in claims:
        if c["cause_id"] == c["effect_id"]:
            out[c["id"]] = "consistency.self_loop"
        elif c["stance"] == "asserts" and c["family"] == "cause" and c.get("relation") not in excl:
            adj[c["cause_id"]].add(c["effect_id"])
    comps, _ = _sccs({k: sorted(v) for k, v in adj.items()}, time.perf_counter() + 60.0)
    member = {x: i for i, comp in enumerate(comps) for x in comp}
    for c in claims:
        if c["id"] in out or c["stance"] != "asserts" or c["family"] != "cause" or c.get("relation") in excl:
            continue
        i = member.get(c["cause_id"])
        if i is not None and member.get(c["effect_id"]) == i:
            out[c["id"]] = "consistency.cycle"
    return out


def _notes(c: Mapping[str, Any], tdetail: Mapping[str, Any]) -> list[str]:
    """Explanatory fact ids (grading.yaml version 2): why a KG claim is T4 despite references, how time was read."""
    notes = []
    if c.get("statement_key") and c["source_tier"] == "T4" and c.get("refs"):
        if gr.kg_tier(c["source_key"], c["refs"])[1] == "non_source_references_only":
            notes.append("tier.T4.non_source_references_only")
    if tdetail.get("entity_dated_by_inception"):
        notes.append("temporal.entity_dated_by_inception")
    if (tdetail.get("effect_time") or {}).get("kind") == "claim_time_qualifier":
        notes.append("temporal.claim_time_qualifier")
    return notes


def compute(
    claims: Sequence[Mapping[str, Any]],
    dates: Mapping[str, EventDates],
    rules: gr.Rules,
    identity: idn.Identity | None = None,
) -> list[Computed]:
    """Grades of ``claims`` (current claim rows of ONE origin) — pure, deterministic.

    With ``identity`` (evidenced ``same_as`` clusters, used when ``rules.resolve_same_as``) the cause and the effect
    are resolved to their clusters: two sources coding the same pair of events under their own record ids assert the
    same link. Records joined by a link declared not independent share a ``lineage:`` upstream key (count once)."""
    ident = identity if (identity is not None and rules.resolve_same_as) else None
    vocab = vocabulary()
    flags = structural_flags(claims, vocab.chain_excluded)
    outcome_rels = vocab.outcome_relations

    def canon(x: str) -> str:
        return ident.canon(x) if ident is not None else x

    def upstream(x: Mapping[str, Any]) -> tuple[str, ...]:
        ups = tuple(x["upstream_keys"] or ())
        if ident is None:
            return ups
        return tuple(sorted({*ups, *ident.lineage(x["cause_id"]), *ident.lineage(x["effect_id"])}))

    # Claims on the same (cause, effect, family) with the same stance corroborate each other; the opposite
    # stance (a source denying the link vs one asserting it) contests them, symmetrically.
    by_stance: dict[tuple[str, str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    contests_of: dict[str, list[str]] = defaultdict(list)
    for c in claims:
        by_stance[(canon(c["cause_id"]), canon(c["effect_id"]), c["family"], c["stance"])].append(c)
        for target in c.get("contests") or []:
            contests_of[target].append(c["claim_key"])
    corr_cache: dict[tuple[str, str, str, str], gr.Corroboration] = {}
    out: list[Computed] = []
    for c in claims:
        k = (canon(c["cause_id"]), canon(c["effect_id"]), c["family"], c["stance"])
        if k not in corr_cache:
            corr_cache[k] = gr.corroboration(
                [gr.ClaimRef(x["claim_key"], x["source_key"], upstream(x)) for x in by_stance[k]]
            )
        corr = corr_cache[k]
        corroborating = sorted(x["claim_key"] for x in by_stance[k] if x["claim_key"] != c["claim_key"])
        # other records of the same events (joined by same_as) whose claims were counted with this one
        via_identity = sorted(
            {
                f"{x['cause_id']}->{x['effect_id']}"
                for x in by_stance[k]
                if (x["cause_id"], x["effect_id"]) != (c["cause_id"], c["effect_id"])
            }
        )
        opposite = (k[0], k[1], k[2], "denies" if c["stance"] == "asserts" else "asserts")
        contested = sorted(
            {x["claim_key"] for x in by_stance.get(opposite, [])}
            | set(contests_of.get(c["claim_key"], []))
            | {f"qualifier:{d.get('property')}:{d.get('by')}" for d in (c.get("disputed_by") or [])}
        )
        tcheck, tdetail = temporal_detail(
            dates.get(c["cause_id"]), dates.get(c["effect_id"]), claim_time_window((c.get("scope") or {}).get("period"))
        )
        missing = (c.get("scope") or {}).get("effect_time")
        if tcheck == "undetermined" and isinstance(missing, Mapping) and missing.get("kind") == "missing":
            # the source codes no date for the effect: typed missing, never a date copied from the cause (M3)
            tdetail = {**tdetail, "effect_time_missing": dict(missing)}
        same_record = outcome_dated_by_cause_end(c, dates.get(c["cause_id"]), dates.get(c["effect_id"]), outcome_rels)
        if same_record is not None:
            tdetail = {**tdetail, "reason": EFFECT_DATED_BY_CAUSE_END, EFFECT_DATED_BY_CAUSE_END: same_record}
        notes = _notes(c, tdetail)
        if via_identity:
            notes.append("corroboration.via_same_as")
        attributed, nature, doubtful = _qualified(c)
        hedged = _hedged(c)
        community = bool(c.get("statement_key")) or is_community_edited(c["source_key"])
        notes += [
            f
            for f, on in (
                ("attribution.supported_by", attributed),
                ("nature.qualified", nature),
                ("nature.sourcing_circumstances", hedged),
                ("scope.qualified", _scope_qualified(c)),
                ("certainty.doubtful", doubtful),
                ("temporal.effect_dated_by_cause_end", same_record is not None),
                ("tier.T2.wikidata_reference", c["source_tier"] == "T2" and community),
            )
            if on
        ]
        if c["id"] in flags:
            notes.append(flags[c["id"]])
        g = gr.decide(
            gr.GradeInputs(
                c["source_tier"],
                tcheck,
                corr.count,
                bool(contested),
                attributed,
                nature,
                doubtful,
                hedged=hedged,
                effect_dated_by_cause_end=same_record is not None,
                community_edited=community,
            ),
            rules,
            notes,
        )
        sha_inputs: dict[str, Any] = {
            "rules": [rules.version, rules.sha256],
            "claim": [c["id"], c["content_sha256"]],
            "temporal": tdetail,
            "corroboration": [corr.count, corr.groups, corroborating],
            "contested_by": contested,
        }
        if via_identity:
            sha_inputs["identity"] = via_identity
        sha_inputs["facts"] = list(g.rule_ids)
        sha = gr.inputs_sha256(sha_inputs)
        out.append(Computed(c["id"], c["claim_key"], g, tcheck, tdetail, corr, corroborating, contested, sha))
    return out


def grade_claims(engine: Engine, *, origin: str | None = None, rules: gr.Rules | None = None) -> GradeReport:
    """Grade every current claim (per origin, one transaction each); write rows only for changed inputs."""
    t0 = time.perf_counter()
    if origin is not None and origin not in ORIGINS:
        raise ValidationProblem("origin must be live or test_fixture")
    rules = rules or gr.load_rules()
    total = GradeReport(origin=origin, rules_version=rules.version)
    for o in [origin] if origin else list(ORIGINS):
        with engine.begin() as conn:
            conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": "reva:causal:grade:" + o})
            claims = (
                conn.execute(
                    text("""SELECT c.id, c.claim_key, c.cause_id, c.effect_id, c.family, c.stance, c.source_key,
                                   c.source_tier, c.upstream_keys, c.contests, c.disputed_by, c.content_sha256,
                                   c.statement_key, c.refs, c.scope, c.qualifiers, c.relation
                              FROM kg.causal_claim_current c WHERE c.origin = :o ORDER BY c.id LIMIT :cap"""),
                    {"o": o, "cap": MAX_CLAIMS_GRADED + 1},
                )
                .mappings()
                .all()
            )
            if len(claims) > MAX_CLAIMS_GRADED:
                raise ValidationProblem("too many claims to grade in one pass", max_claims=MAX_CLAIMS_GRADED)
            if not claims:
                continue
            dates = fetch_event_dates(conn, [c["cause_id"] for c in claims] + [c["effect_id"] for c in claims], o)
            identity = idn.load(conn, origin=o) if rules.resolve_same_as else None
            if identity is not None:
                total.identity[o] = identity.summary()
            computed = compute([dict(c) for c in claims], dates, rules, identity)
            latest: dict[int, str] = dict(
                conn.execute(
                    text("""SELECT claim_row_id, inputs_sha256 FROM kg.causal_grade_current
                             WHERE origin = :o AND claim_row_id = ANY(:ids)"""),
                    {"o": o, "ids": [c["id"] for c in claims]},
                ).all()
            )
            changed = [x for x in computed if latest.get(x.claim_row_id) != x.inputs_sha256]
            total.claims += len(computed)
            total.unchanged += len(computed) - len(changed)
            for x in computed:
                total.by_grade[x.grade.grade] = total.by_grade.get(x.grade.grade, 0) + 1
            if not changed:
                continue
            load_id = _new_load(
                conn,
                kind="grading",
                source_key=GRADER_SOURCE,
                origin=o,
                parameters={"rules_version": rules.version, "rules_sha256": rules.sha256, "claims": len(computed)},
            )
            total.load_ids.append(load_id)
            for i in range(0, len(changed), INSERT_CHUNK):
                payload = [
                    [
                        x.claim_row_id,
                        x.claim_key,
                        x.grade.grade,
                        list(x.grade.rule_ids),
                        x.temporal_check,
                        x.temporal,
                        x.corroboration.count,
                        x.corroborating[:500],
                        x.corroboration.groups[:100],
                        x.contested_by[:500],
                        x.inputs_sha256,
                    ]
                    for x in changed[i : i + INSERT_CHUNK]
                ]
                total.written += int(
                    conn.execute(
                        text("""INSERT INTO kg.causal_grade(load_id, origin, rules_version, rules_sha256, claim_row_id,
                                    claim_key, grade, rule_ids, temporal_check, temporal, corroboration,
                                    corroborating_claim_keys, upstream_groups, contested_by, inputs_sha256)
                                SELECT :load, :o, :rv, :rs, (x->>0)::bigint, x->>1, x->>2,
                                       ARRAY(SELECT jsonb_array_elements_text(x->3)), x->>4, x->5, (x->>6)::int,
                                       ARRAY(SELECT jsonb_array_elements_text(x->7)), x->8,
                                       ARRAY(SELECT jsonb_array_elements_text(x->9)), x->>10
                                  FROM jsonb_array_elements(CAST(:rows AS jsonb)) x"""),
                        {"load": load_id, "o": o, "rv": rules.version, "rs": rules.sha256, "rows": _json(payload)},
                    ).rowcount
                )
    total.elapsed_s = round(time.perf_counter() - t0, 4)
    return total
