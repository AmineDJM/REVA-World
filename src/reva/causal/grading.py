"""Deterministic evidence grading of causal claims (``registry/causal/grading.yaml``).

Pure functions (no database): the rule table, independent-source dedupe (corroboration) and the grade
decision. ``reva.causal.grader`` feeds them from the database and writes ``kg.causal_grade`` rows.

Independence (spec: "sources sharing an upstream count once")
-------------------------------------------------------------
Each claim yields *upstream keys*:

* a Wikidata reference -> ``url:<normalised URL>`` (P854), ``item:<Q>`` (P248 stated in),
  ``imported_from:<Q>`` (P143), ``doi:<doi>`` (P356), ``isbn:<digits>`` (P212/P957); a reference with none of
  these -> ``refhash:<sha256 of the reference without its retrieval date P813>``;
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
_CONDITION_KEYS = {"tier", "temporal_check", "corroborated", "contested"}
#: Reference properties that identify a scholarly work verbatim (T2 when they arrive in a source's references).
DOI_PIDS = ("P356",)
ISBN_PIDS = ("P212", "P957")
RETRIEVAL_PIDS = ("P813",)


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

    def reason(self, rule_id: str) -> dict[str, str]:
        for r in self.rules:
            if r.id == rule_id:
                return {"rule_id": rule_id, "en": r.reason_en, "fr": r.reason_fr}
        f = self.facts.get(rule_id) or {}
        return {"rule_id": rule_id, "en": f.get("en", rule_id), "fr": f.get("fr", rule_id)}


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
    out = Rules(str(doc["version"]), sha256, int(doc.get("corroborated_min", 2)), tuple(rules), facts)
    for rid in _fact_ids():
        if rid not in facts:
            raise ValueError(f"grading.yaml lacks the fact rule {rid}")
    return out


def _fact_ids() -> list[str]:
    return (
        [f"tier.{t}" for t in TIERS]
        + [f"temporal.{t}" for t in TEMPORAL]
        + ["corroboration.single", "corroboration.corroborated", "contest.none", "contest.contested"]
    )


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


@dataclass(frozen=True)
class GradeResult:
    grade: str
    rule_ids: tuple[str, ...]
    deciding_rule: str
    reasons: tuple[dict[str, str], ...]


def decide(inp: GradeInputs, rules: Rules) -> GradeResult:
    """The first matching rule decides; fact rule ids record what it saw."""
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
            ids = (*facts, r.id)
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


def reference_keys(ref: Mapping[str, Any]) -> list[str]:
    keys: list[str] = []
    for v in _vals(ref, "P854"):
        t = _text(v)
        if t:
            keys.append("url:" + normalise_url(t))
    for v in _vals(ref, "P248"):
        q = _item(v)
        if q:
            keys.append("item:" + q)
    for v in _vals(ref, "P143"):
        q = _item(v)
        if q:
            keys.append("imported_from:" + q)
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
            keys += reference_keys(ref)
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
