"""TEST FIXTURE causal world for the NLQA ``why`` / ``causal_chain`` operations (origin ``test_fixture``).

Claims are written as CAUSCORE *claim JSON* (``docs/dev/v3_causal_interface.md``) and go through the real
mapping :func:`reva.nlqa.causal_adapter.claim_from_json`, so the adapter's filtering (T1–T4 only, a source
required, grade A–E or null) is exercised. Statement ids end in ``FIXTURE``; dataset rows, codebook and the
mechanism quote are placeholders written for the test, not quotations of real sources. Q361 / Q8698 are the
Wikidata items of the First World War / Great Depression; the other events use ``Q9100000x`` test ids.
"""

from __future__ import annotations

from typing import Any

import nlqa_fakes as F

from reva.nlqa.catalog import fold
from reva.nlqa.causal_adapter import CausalServiceAdapter
from reva.nlqa.ports import Backends, Entity, EntityMatch

SHA = "e" * 64

EVENTS = {
    "Q361": Entity("Q361", {"en": "World War I", "fr": "Première Guerre mondiale"}, {}, "war", 300),
    "Q91000001": Entity(
        "Q91000001", {"en": "assassination in Sarajevo", "fr": "attentat de Sarajevo"}, {}, "event", 80
    ),
    "Q91000002": Entity("Q91000002", {"en": "July Crisis", "fr": "crise de juillet"}, {}, "event", 60),
    "Q91000003": Entity("Q91000003", {"en": "Treaty of Versailles", "fr": "traité de Versailles"}, {}, "treaty", 200),
    "Q91000004": Entity("Q91000004", {"en": "European alliance system", "fr": "système d'alliances"}, {}, "other", 20),
    "Q91000005": Entity("Q91000005", {"en": "imperial rivalries", "fr": "rivalités impériales"}, {}, "other", 20),
    "Q91000006": Entity("Q91000006", {"en": "Russian Revolution", "fr": "révolution russe"}, {}, "event", 150),
    "Q8698": Entity("Q8698", {"en": "Great Depression", "fr": "Grande Dépression"}, {}, "event", 150),
}


def ref(qid: str) -> dict[str, Any]:
    e = EVENTS[qid]
    return {"id": qid, "label": e.labels["en"], "labels": dict(e.labels), "kind": e.kind}


def claim(
    key: str,
    cause: str,
    effect: str,
    relation: str,
    tier: str,
    grade: str | None,
    *,
    lane: str = "general",
    temporal: str = "consistent",
    corroboration: int = 1,
    contested_by: tuple[str, ...] = (),
    stance: str = "asserts",
    mechanism: str | None = None,
    dataset: str | None = None,
    source_key: str | None = None,
) -> dict[str, Any]:
    wd = dataset is None
    sid = f"{effect}$FIXTURE-{key[-4:]}"
    return {
        "claim_key": key,
        "row_id": 1,
        "cause": ref(cause),
        "effect": ref(effect),
        "relation": relation,
        "relation_label": {"en": relation.replace("_", " "), "fr": relation.replace("_", " ")},
        "family": "cause",
        "lane": lane,
        "polarity": None,
        "stance": stance,
        "asserted_on": "effect",
        "source": {
            "source_key": source_key or ("wikidata" if wd else "icb.data"),
            "tier": tier,
            "dataset": dataset,
            "source_revision": "2250000000" if wd else "v16-FIXTURE",
            "statement_key": f"wikidata:{sid}" if wd else None,
            "statement_id": sid if wd else None,
            "selector": {"entity": effect, "property": "P828", "statement_id": sid}
            if wd
            else {"file": "fixture.csv", "row": 2},
            "raw_sha256": SHA,
            "references": [{"P854": ["https://example.org/fixture"]}] if tier == "T3" else [],
            "citation": None,
            "codebook": None if wd else {"name": "fixture codebook", "version": "16"},
            "verification": "community_edited_not_verified" if wd else "source_reported_not_verified",
        },
        "mechanism": mechanism,
        "scope": {},
        "grade": None
        if grade is None
        else {
            "grade": grade,
            "rules_version": "1",
            "rule_ids": [f"tier.{tier}", f"temporal.{temporal}", f"grade.{grade}"],
            "temporal_check": temporal,
            "corroboration": corroboration,
            "contested_by": list(contested_by),
        },
        "review_status": "unreviewed",
        "origin": "test_fixture",
    }


SARAJEVO = claim(
    "kg:wd:FIXTURE-0001", "Q91000001", "Q361", "immediate_cause", "T3", "B", lane="immediate", corroboration=2
)
JULY = claim(
    "ds:icb.data:icb2:FIXTURE-0002",
    "Q91000002",
    "Q361",
    "coded_trigger",
    "T1",
    "A",
    lane="coded",
    corroboration=2,
    dataset="icb2",
    mechanism="FIXTURE codebook definition of a crisis trigger",
)
ALLIANCES = claim("kg:wd:FIXTURE-0003", "Q91000004", "Q361", "underlying_cause", "T4", "D", lane="underlying")
IMPERIAL = claim(
    "kg:wd:FIXTURE-0004",
    "Q91000005",
    "Q361",
    "underlying_cause",
    "T3",
    "E",
    lane="underlying",
    temporal="undetermined",
    contested_by=("ds:fixture.refs:FIXTURE-0005",),
)
IMPERIAL_DENIED = claim(
    "ds:fixture.refs:FIXTURE-0005",
    "Q91000005",
    "Q361",
    "underlying_cause",
    "T2",
    "B",
    lane="underlying",
    stance="denies",
    dataset="refs",
    source_key="fixture.refs",
)
VERSAILLES_BAD = claim("kg:wd:FIXTURE-0006", "Q91000003", "Q361", "has_cause", "T4", "E", temporal="inconsistent")
#: a candidate (T5) and a source-less row: the adapter must drop both
CANDIDATE = {
    **claim("cand:FIXTURE-0007", "Q91000006", "Q361", "has_cause", "T3", None),
    "source": {"source_key": "wikipedia.en", "tier": "T5"},
}
NO_SOURCE = {**claim("kg:wd:FIXTURE-0008", "Q91000006", "Q361", "has_cause", "T3", "C"), "source": None}

E_VERSAILLES = claim("kg:wd:FIXTURE-0101", "Q361", "Q91000003", "has_effect", "T3", "C", temporal="consistent")
E_REVOLUTION = claim("kg:wd:FIXTURE-0102", "Q361", "Q91000006", "contributing_factor", "T3", "B", lane="contributing")

CAUSES = [SARAJEVO, JULY, ALLIANCES, IMPERIAL, IMPERIAL_DENIED, VERSAILLES_BAD, CANDIDATE, NO_SOURCE]
EFFECTS = [E_VERSAILLES, E_REVOLUTION]

CHAIN = {
    "from": "Q91000001",
    "to": "Q91000003",
    "found": True,
    "max_hops": 4,
    "paths": [
        {
            "length": 2,
            "chain_grade": "C",
            "hops": [
                {"from": ref("Q91000001"), "to": ref("Q361"), "hop_grade": "B", "claims": [SARAJEVO]},
                {"from": ref("Q361"), "to": ref("Q91000003"), "hop_grade": "C", "claims": [E_VERSAILLES]},
            ],
        }
    ],
}


class FakeCausalService:
    """Plays CAUSCORE's graph services from the dicts above; records calls."""

    def __init__(self, causes=CAUSES, effects=EFFECTS, chain=CHAIN) -> None:
        self.causes, self.effects, self.chain = list(causes), list(effects), chain
        self.calls: list[tuple[str, ...]] = []

    def explain(self, event_id: str, direction: str, limit: int) -> Any:
        self.calls.append(("explain", event_id, direction))
        rows = self.causes if direction == "causes" else self.effects
        mine = [c for c in rows if c["effect" if direction == "causes" else "cause"]["id"] == event_id]
        return {"event": ref(event_id) if event_id in EVENTS else {"id": event_id}, "claims": mine[:limit]}

    def chains(self, a: str, b: str, max_hops: int, limit: int) -> Any:
        self.calls.append(("chains", a, b, str(max_hops)))
        if self.chain and self.chain["from"] == a and self.chain["to"] == b:
            return self.chain
        return {"from": a, "to": b, "found": False, "paths": []}


def adapter(service: FakeCausalService | None = None) -> CausalServiceAdapter:
    s = service or FakeCausalService()
    return CausalServiceAdapter(s.explain, s.chains, name="fake causal claims (test fixture)")


class FakeKgEvents(F.FakeKg):
    """The NLQA fake KG plus the causal test events (search by exact label / prefix)."""

    def search_entities(self, text: str, *, kind: str | None, lang: str, limit: int) -> list[EntityMatch]:
        t = fold(text)
        out = [
            EntityMatch(e, "label_exact", text) for e in EVENTS.values() if any(fold(v) == t for v in e.labels.values())
        ]
        return out or super().search_entities(text, kind=kind, lang=lang, limit=limit)

    def entity(self, qid: str) -> Entity | None:
        return EVENTS.get(qid) or super().entity(qid)


def backends(**over: Any) -> Backends:
    b = F.backends(kg=FakeKgEvents(), causal=adapter())
    for k, v in over.items():
        setattr(b, k, v)
    return b
