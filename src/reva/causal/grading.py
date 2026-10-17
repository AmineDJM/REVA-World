"""Deterministic evidence grading of causal claims (``registry/causal/grading.yaml``).

Pure functions (no database): the rule table, independent-source dedupe (corroboration) and the grade
decision. ``reva.causal.grader`` feeds them from the database and writes ``kg.causal_grade`` rows.

Independence (spec: "sources sharing an upstream count once")
-------------------------------------------------------------
Each claim yields *upstream keys*:

* a Wikidata reference -> ``url:<normalised URL>`` (P854), ``item:<Q>`` (P248 stated in), ``quote:<sha of the
  P1683 text>``, ``doi:<doi>`` (P356), ``isbn:<digits>`` (P212/P957); a reference with none of these ->
  ``refhash:<sha256 of the reference without its retrieval date P813>``; a reference made only of non-source
  pointers (P143 / P4656 imported from Wikimedia, P3452 inferred from, P887 heuristic; version 2) ->
  ``community:<source root>``: the editing community, not an independent source;
* a statement without references -> ``community:<source root>`` (e.g. ``community:wikidata``: the
  community of editors is one source, however many unreferenced statements it wrote);
* a dataset claim -> ``dataset:<source_key>/<dataset>`` plus its citation's DOI/ISBN.

Claims asserting the same (cause, effect, family) are merged into groups when they share any key (union-find);
corroboration = number of groups. A ``community:<root>`` group is not counted when a *referenced* group of the
same source root exists (an unreferenced restatement on the other entity's page is not independent).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
GRADING_FILE = REPO_ROOT / "registry" / "causal" / "grading.yaml"
GRADES = ("A", "B", "C", "D", "E")
TIERS = ("T1", "T2", "T3", "T4")
TEMPORAL = ("consistent", "overlapping", "inconsistent", "undetermined")
_CONDITION_KEYS = {"tier", "temporal_check", "corroborated", "contested", "attributed", "nature_qualified", "doubtful"}
#: Reference properties that identify a scholarly work verbatim (T2 when they arrive in a source's references).
DOI_PIDS = ("P356",)
ISBN_PIDS = ("P212", "P957")
RETRIEVAL_PIDS = ("P813",)
#: Reference properties that are not sources (grading.yaml version 2, audit H1): "imported from Wikimedia
#: project" (P143) and "Wikimedia import URL" (P4656) point at another wiki — where an editor copied the
#: statement from —, "inferred from" (P3452) points at another Wikidata item, "based on heuristic" (P887) names a
#: Wikidata editing heuristic. A reference made only of these (and P813 retrieval dates) does not raise a
#: statement to T3, and as an upstream it is the Wikidata community itself (``community:wikidata``), never an
#: independent source (audit H2).
NON_SOURCE_REF_PIDS = ("P143", "P4656", "P3452", "P887")
WIKIMEDIA_IMPORT_PIDS = NON_SOURCE_REF_PIDS  # name kept for callers
#: Verbatim quotation of the source (P1683): two references quoting the same text are one upstream (audit H2).
QUOTE_PIDS = ("P1683",)
#: Claim qualifiers that change who asserts the link / how (audit H3): "statement supported by" (P3680) names
#: the party Wikidata records as making the claim; "nature of statement" (P5102) qualifies it (allegedly,
#: hypothetically, rarely ...). Their values are kept verbatim and shown; the grade is capped (grading.yaml).
SUPPORTED_BY_PID = "P3680"
NATURE_OF_STATEMENT_PID = "P5102"
#: KG source keys whose statements were read without their references (WDQS listings select statement id,
#: object and rank only; ``reva.kg.harvest`` stores them with ``references=[]``): "references unknown", never
#: "unreferenced". Their tier is left pending until an entity snapshot of the statement is loaded.
REFERENCES_UNREAD_SOURCE_KEYS = ("wikidata.wdqs",)
#: Tier basis of a KG claim (why it has its tier); ``references_unknown`` means no tier (claim deferred).
TIER_BASES = ("citation", "references", "non_source_references_only", "no_references", "references_unknown")


# ---------------------------------------------------------------------------------------------- rules
@dataclass(frozen=True)
class Rule:
    id: str
    grade: str
    when: Mapping[str, Any]
    reason_en: str
    reason_fr: str

    def matches(self, inp: GradeInputs, corroborated_min: int) -> bool:
        w = self.when
        if "tier" in w and inp.tier not in w["tier"]:
            return False
        if "temporal_check" in w and inp.temporal_check not in w["temporal_check"]:
            return False
        if "corroborated" in w and (inp.corroboration >= corroborated_min) != bool(w["corroborated"]):
            return False
        for key in ("attributed", "nature_qualified", "doubtful"):
            if key in w and bool(getattr(inp, key)) != bool(w[key]):
                return False
        if "contested" in w and inp.contested != bool(w["contested"]):
            return False
        return True


@dataclass(frozen=True)
class Rules:
    version: str
    sha256: str
    corroborated_min: int
    rules: tuple[Rule, ...]
    facts: Mapping[str, Mapping[str, str]]

    index: Mapping[str, Mapping[str, str]] = field(default_factory=dict, compare=False, repr=False)
    #: grading.yaml ``identity.resolve_same_as``: corroboration resolves evidenced ``events:same_as`` clusters
    #: (reva.causal.identity) -- the same pair of events coded under two sources' own record ids is one link.
    resolve_same_as: bool = False

    def reason(self, rule_id: str) -> dict[str, str]:
        return dict(self.index.get(rule_id) or {"rule_id": rule_id, "en": rule_id, "fr": rule_id})


def _reason_index(rules: Sequence[Rule], facts: Mapping[str, Mapping[str, str]]) -> dict[str, dict[str, str]]:
    out = {k: {"rule_id": k, "en": v.get("en", k), "fr": v.get("fr", k)} for k, v in facts.items()}
    out.update({r.id: {"rule_id": r.id, "en": r.reason_en, "fr": r.reason_fr} for r in rules})
    return out


def parse_rules(doc: Mapping[str, Any], sha256: str) -> Rules:
    rules: list[Rule] = []
    seen: set[str] = set()
    for r in doc.get("rules") or []:
        rid = str(r["id"])
        if rid in seen:
            raise ValueError(f"duplicate rule id {rid}")
        seen.add(rid)
        if r["grade"] not in GRADES:
            raise ValueError(f"rule {rid}: invalid grade")
        when = dict(r.get("when") or {})
        if set(when) - _CONDITION_KEYS:
            raise ValueError(f"rule {rid}: unknown condition {sorted(set(when) - _CONDITION_KEYS)}")
        if "tier" in when and not set(when["tier"]) <= set(TIERS):
            raise ValueError(f"rule {rid}: invalid tier")
        if "temporal_check" in when and not set(when["temporal_check"]) <= set(TEMPORAL):
            raise ValueError(f"rule {rid}: invalid temporal_check")
        rules.append(Rule(rid, r["grade"], when, r["reason"]["en"], r["reason"]["fr"]))
    facts = {str(k): dict(v) for k, v in (doc.get("facts") or {}).items()}
    out = Rules(
        str(doc["version"]),
        sha256,
        int(doc.get("corroborated_min", 2)),
        tuple(rules),
        facts,
        _reason_index(rules, facts),
        bool((doc.get("identity") or {}).get("resolve_same_as", False)),
    )
    for rid in _fact_ids():
        if rid not in facts:
            raise ValueError(f"grading.yaml lacks the fact rule {rid}")
    return out


def _fact_ids() -> list[str]:
    return (
        [f"tier.{t}" for t in TIERS]
        + [f"temporal.{t}" for t in TEMPORAL]
        + ["corroboration.single", "corroboration.corroborated", "contest.none", "contest.contested"]
        + list(NOTE_FACTS)
    )


#: Explanatory fact rules (version 2) attached when they apply, after the four base facts.
NOTE_FACTS = (
    "tier.T4.non_source_references_only",
    "temporal.entity_dated_by_inception",
    "temporal.claim_time_qualifier",
    "corroboration.via_same_as",
    "attribution.supported_by",
    "nature.qualified",
    "certainty.doubtful",
    "consistency.self_loop",
    "consistency.cycle",
)
#: Facts that keep a claim out of chain composition (reva.causal.graph.chain): a self-loop or a cycle member
#: (audit L7), a claim Wikidata attributes to a named party or qualifies by its nature (audit H3).
CHAIN_QUARANTINE_FACTS = ("consistency.self_loop", "consistency.cycle", "attribution.supported_by", "nature.qualified")


@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> Rules:
    body = Path(path).read_bytes()
    return parse_rules(yaml.safe_load(body), hashlib.sha256(body).hexdigest())


def load_rules(path: Path = GRADING_FILE) -> Rules:
    return _load(str(path), path.stat().st_mtime)


# ---------------------------------------------------------------------------------------------- decision
@dataclass(frozen=True)
class GradeInputs:
    tier: str
    temporal_check: str
    corroboration: int
    contested: bool
    #: P3680 "statement supported by": Wikidata records that a named party makes the claim (audit H3)
    attributed: bool = False
    #: P5102 "nature of statement" qualifier present (audit H3)
    nature_qualified: bool = False
    #: the source codes the event / link as doubtful (``scope.source_validity[].certainty``, audit H4)
    doubtful: bool = False


@dataclass(frozen=True)
class GradeResult:
    grade: str
    rule_ids: tuple[str, ...]
    deciding_rule: str
    reasons: tuple[dict[str, str], ...]


def decide(inp: GradeInputs, rules: Rules, notes: Sequence[str] = ()) -> GradeResult:
    """The first matching rule decides; fact rule ids record what it saw (``notes``: explanatory fact ids from
    :data:`NOTE_FACTS`, placed after the base facts)."""
    if inp.tier not in TIERS or inp.temporal_check not in TEMPORAL or inp.corroboration < 1:
        raise ValueError(f"invalid grade inputs {inp!r}")
    for r in rules.rules:
        if r.matches(inp, rules.corroborated_min):
            facts = [
                f"tier.{inp.tier}",
                f"temporal.{inp.temporal_check}",
                "corroboration.corroborated" if inp.corroboration >= rules.corroborated_min else "corroboration.single",
                "contest.contested" if inp.contested else "contest.none",
            ]
            ids = (*facts, *(n for n in NOTE_FACTS if n in notes), r.id)
            return GradeResult(r.grade, ids, r.id, tuple(rules.reason(i) for i in ids))
    raise ValueError(f"no grading rule decides {inp!r} (grading.yaml must be complete)")


def weakest(grades: Iterable[str]) -> str:
    """The weakest grade (E < D < C < B < A)."""
    return max(grades, key=GRADES.index)


def best(grades: Iterable[str]) -> str:
    return min(grades, key=GRADES.index)


def at_least(grade: str, minimum: str) -> bool:
    return GRADES.index(grade) <= GRADES.index(minimum)


# ---------------------------------------------------------------------------------------------- upstream keys
_ENC = json.JSONEncoder(sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_sha(obj: Any) -> str:
    return hashlib.sha256(_ENC.encode(obj).encode("utf-8")).hexdigest()


def normalise_url(u: str) -> str:
    u = u.strip()
    try:
        p = urlsplit(u)
    except ValueError:
        return u
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if p.port:
        host = f"{host}:{p.port}"
    path = re.sub(r"/+$", "", p.path) or ""
    scheme = "https" if p.scheme.lower() in ("http", "https") else p.scheme.lower()
    return urlunsplit((scheme, host, path, p.query, ""))


def _vals(ref: Mapping[str, Any], pid: str) -> list[Any]:
    v = ref.get(pid)
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _item(v: Any) -> str | None:
    if isinstance(v, str):
        return v
    if isinstance(v, Mapping):
        x = v.get("id") or v.get("qid") or v.get("value")
        return str(x) if x is not None else None
    return None


def _text(v: Any) -> str | None:
    if isinstance(v, str):
        return v
    if isinstance(v, Mapping):
        x = v.get("value") or v.get("text") or v.get("url")
        return str(x) if x is not None else None
    return None


def citation_of(refs: Sequence[Mapping[str, Any]]) -> dict[str, list[str]] | None:
    """Verbatim DOI / ISBN values found in a statement's own references (T2 evidence), else ``None``."""
    out: dict[str, list[str]] = {}
    for ref in refs:
        if not isinstance(ref, Mapping):
            continue
        for pid in DOI_PIDS:
            out.setdefault("doi", []).extend(t for t in map(_text, _vals(ref, pid)) if t)
        for pid in ISBN_PIDS:
            out.setdefault("isbn", []).extend(t for t in map(_text, _vals(ref, pid)) if t)
    out = {k: sorted(set(v)) for k, v in out.items() if v}
    return out or None


def is_non_source_reference(ref: Mapping[str, Any]) -> bool:
    """A reference made only of non-source snaks (:data:`NON_SOURCE_REF_PIDS`: Wikimedia import, inferred from,
    heuristic) and retrieval dates: a pointer, not a source."""
    keys = set(ref)
    return bool(keys & set(NON_SOURCE_REF_PIDS)) and keys <= set(NON_SOURCE_REF_PIDS) | set(RETRIEVAL_PIDS)


is_wikimedia_import_only = is_non_source_reference  # name kept for callers


def kg_tier(source_key: str, refs: Sequence[Mapping[str, Any]]) -> tuple[str | None, str]:
    """Tier of a community-edited (Wikidata) KG causal statement and its basis (:data:`TIER_BASES`).

    * ``wikidata.wdqs`` rows (references not read) -> ``(None, "references_unknown")``: no tier is invented;
    * a DOI/ISBN in a reference -> T2 (``citation``);
    * at least one reference that is not a non-source pointer -> T3 (``references``);
    * only non-source pointers (:data:`NON_SOURCE_REF_PIDS`) -> T4 (``non_source_references_only``); no
      references -> T4 (``no_references``).
    """
    if source_key in REFERENCES_UNREAD_SOURCE_KEYS:
        return None, "references_unknown"
    mrefs = [r for r in refs if isinstance(r, Mapping)]
    if citation_of(mrefs):
        return "T2", "citation"
    if any(r and not is_non_source_reference(r) for r in mrefs):
        return "T3", "references"
    if mrefs and any(mrefs):
        return "T4", "non_source_references_only"
    return "T4", "no_references"


def reference_keys(ref: Mapping[str, Any], community: str = "community:wikidata") -> list[str]:
    """Upstream keys of one reference. A non-source reference (Wikimedia import, inferred from, heuristic: audit
    H1/H2) is the editing community itself (``community``), never an independent upstream; a verbatim quotation
    (P1683) is keyed by its text, so two references quoting the same text count once."""
    if is_non_source_reference(ref):
        return [community]
    keys: list[str] = []
    for v in _vals(ref, "P854"):
        t = _text(v)
        if t:
            keys.append("url:" + normalise_url(t))
    for v in _vals(ref, "P248"):
        q = _item(v)
        if q:
            keys.append("item:" + q)
    for pid in QUOTE_PIDS:
        for t in map(_text, _vals(ref, pid)):
            if t and t.strip():
                keys.append("quote:" + canonical_sha(" ".join(t.split()).casefold())[:32])
    for pid in DOI_PIDS:
        keys += ["doi:" + t.strip().lower() for t in map(_text, _vals(ref, pid)) if t]
    for pid in ISBN_PIDS:
        keys += ["isbn:" + re.sub(r"[^0-9Xx]", "", t).upper() for t in map(_text, _vals(ref, pid)) if t]
    if not keys:
        keys.append("refhash:" + canonical_sha({k: v for k, v in ref.items() if k not in RETRIEVAL_PIDS})[:32])
    return keys


def source_root(source_key: str) -> str:
    """``wikidata.causal_fixture`` -> ``wikidata``."""
    return source_key.split(".", 1)[0]


def upstream_keys(
    *,
    source_key: str,
    refs: Sequence[Mapping[str, Any]] = (),
    dataset: str | None = None,
    citation: Mapping[str, Sequence[str]] | None = None,
) -> list[str]:
    keys: list[str] = []
    if dataset is not None:
        keys.append(f"dataset:{source_key}/{dataset}")
    for ref in refs:
        if isinstance(ref, Mapping):
            keys += reference_keys(ref, "community:" + source_root(source_key))
    for doi in (citation or {}).get("doi", []):
        keys.append("doi:" + str(doi).strip().lower())
    for isbn in (citation or {}).get("isbn", []):
        keys.append("isbn:" + re.sub(r"[^0-9Xx]", "", str(isbn)).upper())
    if not keys:
        keys.append("community:" + source_root(source_key))
    return sorted(set(keys))


# ---------------------------------------------------------------------------------------------- corroboration
@dataclass(frozen=True)
class ClaimRef:
    """What corroboration needs to know about a claim."""

    key: str
    source_key: str
    upstream: tuple[str, ...]


@dataclass
class Corroboration:
    count: int
    groups: list[list[str]] = field(default_factory=list)  # each group: its upstream keys
    group_claims: list[list[str]] = field(default_factory=list)  # each group: its claim keys
    dropped_community: list[str] = field(default_factory=list)


def corroboration(claims: Sequence[ClaimRef]) -> Corroboration:
    """Independent source groups among claims asserting the same (cause, effect, family)."""
    if len(claims) == 1:  # the common case: one claim is one group
        return Corroboration(1, [sorted(set(claims[0].upstream))], [[claims[0].key]])
    parent = list(range(len(claims)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owner: dict[str, int] = {}
    for i, c in enumerate(claims):
        for k in c.upstream:
            j = owner.setdefault(k, i)
            if j != i:
                a, b = find(i), find(j)
                if a != b:
                    parent[max(a, b)] = min(a, b)
    groups: dict[int, list[int]] = {}
    for i in range(len(claims)):
        groups.setdefault(find(i), []).append(i)
    keyed = []
    for members in groups.values():
        ups = sorted({k for i in members for k in claims[i].upstream})
        keyed.append(
            (ups, sorted(claims[i].key for i in members), {source_root(claims[i].source_key) for i in members})
        )
    keyed.sort()
    # An unreferenced community group does not count beside a referenced group of the same source root.
    referenced_roots = {r for ups, _, roots in keyed if not all(u.startswith("community:") for u in ups) for r in roots}
    kept, dropped = [], []
    for ups, keys, _roots in keyed:
        if all(u.startswith("community:") for u in ups) and any(u.split(":", 1)[1] in referenced_roots for u in ups):
            dropped.extend(keys)
            continue
        kept.append((ups, keys))
    return Corroboration(max(1, len(kept)), [u for u, _ in kept], [k for _, k in kept], dropped)


def inputs_sha256(obj: Mapping[str, Any]) -> str:
    return canonical_sha({"v": "reva.causal.grade-inputs/1", **obj})
