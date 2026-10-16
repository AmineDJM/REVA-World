"""EVENTS harvester specs (registry/kg/queries/events): schema-valid, API-mode capable, rank-aware, causal
properties in the snapshots, verified root classes, no duplicate of other agents' specs, and an offline probe:
the specs' class selection, re-applied to recorded claims, selects known events."""

from __future__ import annotations

import json
from pathlib import Path

from reva.adapters.connectors.wikidata import load_allowlist
from reva.events.taxonomy import load_verification, recorded_classes
from reva.kg.harvest import discover_specs, validate_all
from reva.kg.model import CAUSAL_PIDS

ROOT = Path(__file__).resolve().parents[3]
EVENTS = ROOT / "registry" / "kg" / "queries" / "events"
HIST_RECORDED = ROOT / "registry" / "kg" / "queries" / "history" / "_recorded"
REUSED = {
    "history.revolutions",
    "economy.economic_crises",
    "geography.nature.epidemics",
    "geography.nature.famines",
    "history.wars",
    "history.treaties",
}


def _specs():  # type: ignore[no-untyped-def]
    return discover_specs([EVENTS])


def test_event_specs_validate() -> None:
    specs = _specs()
    assert len(specs) >= 10
    res = validate_all(specs, load_allowlist())
    assert all(not errs for errs in res.values()), {k: v for k, v in res.items() if v}


def test_specs_are_api_capable_rank_aware_and_carry_causal_properties() -> None:
    for sp in _specs():
        d = sp.data
        assert d["id"].startswith("history.events.") and d["owner"] == "EVENTS" and d["purpose"] == "seed_entities"
        assert d["api_search"]["instance_of"], sp.id
        assert "p:P31" in d["sparql"] and "wikibase:rank" in d["sparql"] and "DeprecatedRank" in d["sparql"], sp.id
        assert "wdt:P31 " not in d["sparql"] and "wdt:P31/" not in d["sparql"], sp.id  # never truthy-only
        assert CAUSAL_PIDS <= set(d["properties"]) and "P31" in d["properties"], sp.id
        assert {"P585", "P580", "P582", "P361", "P155", "P156", "P710", "P276", "P17"} <= set(d["properties"]), sp.id
        assert "sitelinks" in d["sparql"] and d["max_rows"] <= 20_000, sp.id


def test_root_classes_are_verified_and_not_duplicated() -> None:
    ents = load_verification()["entities"]
    others = [s for s in discover_specs() if not s.id.startswith("history.events.")]
    other_roots = {q for s in others for q in (s.get("api_search") or {}).get("instance_of", [])}
    assert REUSED <= {s.id for s in others}  # reused, not duplicated
    for sp in _specs():
        for q in sp.data["api_search"]["instance_of"]:
            assert ents[q]["labels"]["en"], q
            assert q not in other_roots, (sp.id, q)


def _recorded_claims() -> dict[str, list[dict[str, str]]]:
    out: dict[str, list[dict[str, str]]] = {}
    for f in sorted(HIST_RECORDED.glob("snapshot_claims_*.json")):
        for q, e in json.loads(f.read_text())["entities"].items():
            out[q] = e["claims"]
    return out


def test_offline_probe_selects_known_events() -> None:
    """KGHIST's recorded snapshots (registry/kg/queries/history/_recorded) + the recorded P279 claims: the items
    each spec's api_search classes select (non-deprecated P31 within subclass_depth)."""
    claims = _recorded_claims()
    parents = {c: [p for p, _r, _s in ps] for c, ps in recorded_classes().parents.items()}
    for q, cl in claims.items():  # recorded P279 claims of KGHIST's class items too
        parents.setdefault(q, [c["o"] for c in cl if c["p"] == "P279"])

    def reaches(cls: str, root: str, depth: int) -> bool:
        frontier, seen = {cls}, {cls}
        for _ in range(depth + 1):
            if root in frontier:
                return True
            frontier = {p for c in frontier for p in parents.get(c, [])} - seen
            seen |= frontier
        return False

    selected: dict[str, set[str]] = {}
    for sp in _specs():
        a = sp.data["api_search"]
        for q, cl in claims.items():
            p31 = [c["o"] for c in cl if c["p"] == "P31" and c.get("rank") != "deprecated"]
            if any(reaches(c, r, a.get("subclass_depth", 0)) for c in p31 for r in a["instance_of"]):
                selected.setdefault(sp.id, set()).add(q)
    assert {"Q620965", "Q193245"} <= selected["history.events.coups"]  # 18 Brumaire, Carnation Revolution
    assert {"Q193155", "Q626191", "Q129053"} <= selected["history.events.transfers_of_sovereignty"]
    assert "Q13582" in selected["history.events.international_conferences"]  # Berlin Conference
    assert "Q1773926" in selected["history.events.political_crises"]  # Congo Crisis
    assert "Q689128" in selected["history.events.protests"]  # Haitian Revolution (slave rebellion < rebellion)
    assert "Q49101" not in set().union(*selected.values())  # Suez Crisis is a military expedition in Wikidata
