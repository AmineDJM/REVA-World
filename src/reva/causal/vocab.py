"""Closed causal-relation vocabulary (``registry/causal/relations.yaml``) and Wikidata direction normalisation.

Every claim row stores ``cause_id`` and ``effect_id``. A Wikidata statement "Y has cause X" (P828 on Y) and
"X has effect Y" (P1542 on X) both become cause X, effect Y; ``asserted_on`` keeps the side the source wrote
it on. P1344 (participant in) and P710 (participant) are context only and never become claims.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
RELATIONS_FILE = REPO_ROOT / "registry" / "causal" / "relations.yaml"

RELATIONS = (
    "has_cause",
    "immediate_cause",
    "underlying_cause",
    "contributing_factor",
    "has_effect",
    "enabled",
    "precipitated",
    "prevented_or_delayed",
    "reaction_to",
    "coded_trigger",
    "coded_outcome",
)
FAMILIES = ("cause", "prevention")
LANES = ("immediate", "underlying", "contributing", "general", "coded", "reaction", "prevention", "outcome")
#: Lane of the dataset-coded outcomes (how an event ended): never causes, counted apart, never chained.
OUTCOME_LANE = "outcome"
POLARITIES = ("promotes", "inhibits")
STANCES = ("asserts", "denies")
#: Causal Wikidata properties (the same set as ``reva.kg.model.CAUSAL_PIDS``).
CAUSAL_PIDS = ("P828", "P1542", "P1478", "P1479", "P1536", "P1537")
#: Context-only properties: participation is not causation.
CONTEXT_ONLY_PIDS = ("P1344", "P710")


@dataclass(frozen=True)
class Relation:
    name: str
    family: str
    lane: str
    label_en: str
    label_fr: str
    inverse_label_en: str
    inverse_label_fr: str


@dataclass(frozen=True)
class WikidataMapping:
    pid: str
    relation: str
    cause_is: Literal["subject", "object"]


@dataclass(frozen=True)
class Vocabulary:
    version: str
    sha256: str
    relations: Mapping[str, Relation]
    wikidata: Mapping[str, WikidataMapping]
    lanes: Mapping[str, Mapping[str, str]]
    context: Mapping[str, Mapping[str, str]]
    #: relations never composed into chains nor counted in cycles (relations.yaml ``chain_excluded``)
    chain_excluded: frozenset[str] = frozenset()

    @property
    def outcome_relations(self) -> frozenset[str]:
        """Relations of the ``outcome`` lane (``coded_outcome``): counted apart from causes and effects."""
        return frozenset(r.name for r in self.relations.values() if r.lane == OUTCOME_LANE)

    def relation(self, name: str) -> Relation:
        return self.relations[name]

    def family(self, relation: str, polarity: str | None = None) -> str:
        """``prevention`` for prevented_or_delayed or an explicitly inhibiting claim, else the relation's."""
        if polarity == "inhibits":
            return "prevention"
        return self.relations[relation].family


def _parse(doc: Mapping[str, Any], sha: str) -> Vocabulary:
    rels: dict[str, Relation] = {}
    wd: dict[str, WikidataMapping] = {}
    for name, r in (doc.get("relations") or {}).items():
        if name not in RELATIONS:
            raise ValueError(f"relation {name!r} is not in the closed vocabulary")
        if r["family"] not in FAMILIES or r["lane"] not in LANES:
            raise ValueError(f"relation {name!r}: invalid family or lane")
        rels[name] = Relation(
            name,
            r["family"],
            r["lane"],
            r["label"]["en"],
            r["label"]["fr"],
            r["inverse_label"]["en"],
            r["inverse_label"]["fr"],
        )
        for m in r.get("wikidata") or []:
            if m["pid"] not in CAUSAL_PIDS or m["cause_is"] not in ("subject", "object"):
                raise ValueError(f"relation {name!r}: invalid Wikidata mapping {m!r}")
            if m["pid"] in wd:
                raise ValueError(f"{m['pid']} mapped twice")
            wd[m["pid"]] = WikidataMapping(m["pid"], name, m["cause_is"])
    missing = set(RELATIONS) - set(rels)
    if missing:
        raise ValueError(f"relations.yaml lacks {sorted(missing)}")
    if set(wd) != set(CAUSAL_PIDS):
        raise ValueError(f"relations.yaml must map every causal property: {sorted(set(CAUSAL_PIDS) - set(wd))}")
    for pid in CONTEXT_ONLY_PIDS:
        if pid in wd:
            raise ValueError(f"{pid} is context only")
    excluded = frozenset(str(x) for x in doc.get("chain_excluded") or [])
    if not excluded <= set(RELATIONS):
        raise ValueError(f"chain_excluded names unknown relations {sorted(excluded - set(RELATIONS))}")
    outcomes = {n for n, r in rels.items() if r.lane == OUTCOME_LANE}
    if not outcomes <= excluded:
        raise ValueError(f"outcome-lane relations must be chain_excluded: {sorted(outcomes - excluded)}")
    return Vocabulary(
        str(doc.get("version")),
        sha,
        rels,
        wd,
        dict(doc.get("lanes") or {}),
        dict(doc.get("context") or {}),
        excluded,
    )


@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> Vocabulary:
    body = Path(path).read_bytes()
    return _parse(yaml.safe_load(body), hashlib.sha256(body).hexdigest())


def vocabulary(path: Path = RELATIONS_FILE) -> Vocabulary:
    return _load(str(path), path.stat().st_mtime)


@dataclass(frozen=True)
class Normalised:
    cause_id: str
    effect_id: str
    relation: str
    asserted_on: Literal["cause", "effect"]


def normalise_wikidata(subject: str, pid: str, obj: str, vocab: Vocabulary | None = None) -> Normalised:
    """Direction normalisation of a causal Wikidata statement ``subject --pid--> obj``.

    >>> normalise_wikidata("Q8698", "P828", "Q201684")   # Great Depression has cause Wall Street crash
    Normalised(cause_id='Q201684', effect_id='Q8698', relation='has_cause', asserted_on='effect')
    """
    v = vocab or vocabulary()
    m = v.wikidata.get(pid)
    if m is None:
        raise ValueError(f"{pid} is not a causal property")
    if m.cause_is == "subject":
        return Normalised(subject, obj, m.relation, "cause")
    return Normalised(obj, subject, m.relation, "effect")
