"""KGHIST: history query specs (registry/kg/queries/history) and anchor chains (registry/kg/anchor_chains.yaml).

Offline. Checks that

* every history spec validates against WDHARVEST's schema and allow-list (``reva.kg.harvest.validate_spec``) and
  stays bounded;
* the recorded Wikidata responses in ``registry/kg/queries/history/_recorded/`` are intact (sha256 of every
  committed body; query text vs. its sha256; snapshot provenance fields);
* the anchor QIDs (Columbus, Tordesillas, Brazil, Portugal, Spain, Portuguese, Cabral, United States) are
  reachable from the history specs: the specs' selection rules are re-applied offline to the recorded claims
  (class membership through P31/P279, the country fan-out, the succession closure, ...) and an anchor counts as
  reachable when a spec selects it or when it is the object of a harvested claim of a selected entity;
* every anchor-chain entity QID was confirmed by a recorded lookup (no QID typed from memory);
* each ``path_expected`` anchor chain has a witness path in the recorded claims that satisfies the chain's
  constraints (ordered hop-class steps, waypoints, allowed classes, non-transit languages/continents/eras), and
  each ``path_may_be_absent`` chain indeed has none (so its honesty label stays true);
* every ``expected_facts`` entry is a recorded statement.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict, deque
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml

from reva.adapters.connectors.wikidata import load_allowlist, qid_from_uri
from reva.kg.harvest import discover_specs, validate_spec

ROOT = Path(__file__).resolve().parents[2]
HIST = ROOT / "registry" / "kg" / "queries" / "history"
RECORDED = HIST / "_recorded"
CHAINS = ROOT / "registry" / "kg" / "anchor_chains.yaml"
REQUESTS = ROOT / "registry" / "kg" / "property_requests" / "KGHIST.yaml"
COUNTRY_QIDS = ROOT / "registry" / "entities" / "country_qids.json"

# Anchors the owner named (QIDs confirmed by the recorded lookups, see test_anchor_chain_entities_*).
ANCHORS = {
    "Q7322": "Christopher Columbus",
    "Q180897": "Treaty of Tordesillas",
    "Q155": "Brazil",
    "Q45": "Portugal",
    "Q29": "Spain",
    "Q5146": "Portuguese language",
    "Q174432": "Pedro Álvares Cabral",
    "Q30": "United States",
}
# Current countries used by the anchors and chains; all must be crosswalk countries (values: countries split).
COUNTRIES_USED = {"Q155", "Q45", "Q29", "Q30", "Q142", "Q145", "Q96", "Q843", "Q668", "Q262", "Q252", "Q916",
                  "Q928", "Q790", "Q43", "Q159", "Q717", "Q974", "Q1033"}

# Root classes of the class-closure seed specs (the test checks they occur in the spec's SPARQL).
CLASS_SPECS = {
    "history.empires": ["Q48349"],
    "history.colonies": ["Q133156", "Q164142", "Q161243"],
    "history.treaties": ["Q131569"],
    "history.wars": ["Q198"],
    "history.expeditions": ["Q2401485"],
    "history.revolutions": ["Q10931"],
    "history.eras": ["Q11514315"],
}
POLITY_CLASSES = ["Q3024240", "Q48349", "Q133156", "Q164142", "Q161243", "Q6256", "Q7275", "Q1048835"]
TERRITORY_CLASSES = ["Q3024240", "Q133156", "Q164142", "Q161243"]
NON_TRANSIT_PROPS = {"P37", "P2936", "P30", "P2348"}  # their objects are languages, continents, eras


# --------------------------------------------------------------------------- helpers
def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@cache
def _specs() -> dict[str, Any]:
    return {s.id: s for s in discover_specs([HIST])}


@cache
def _chains_doc() -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load(CHAINS.read_text())
    return doc


@cache
def _snapshot() -> dict[str, dict[str, Any]]:
    ents: dict[str, dict[str, Any]] = {}
    for p in sorted(RECORDED.glob("snapshot_claims_*.json")):
        ents.update(json.loads(p.read_text())["entities"])
    return ents


def _objs(q: str, prop: str) -> list[str]:
    e = _snapshot().get(q)
    if not e:
        return []
    return [t["o"] for t in e["claims"]
            if t["p"] == prop and isinstance(t["o"], str) and re.fullmatch(r"Q\d+", t["o"])]


def _requested() -> set[str]:
    if not REQUESTS.exists():
        return set()
    return {r["property"] for r in yaml.safe_load(REQUESTS.read_text()) or []}


def _qid(key: str) -> str:
    return str(_chains_doc()["entities"][key]["qid"])


# --------------------------------------------------------------------------- specs
def test_history_specs_validate_against_schema_and_allowlist() -> None:
    allow = load_allowlist()
    specs = discover_specs([HIST])
    assert len(specs) >= 12
    known = [s.id for s in discover_specs()]
    for s in specs:
        assert validate_spec(s, allow, known_ids=known) == [], s.path.name
        assert s.data["domain"] == "history" and s.data["id"].startswith("history.")
        assert s.data.get("owner") == "KGHIST"


def test_history_specs_are_bounded_and_cover_both_purposes() -> None:
    specs = _specs()
    assert {s.data["purpose"] for s in specs.values()} == {"seed_entities", "statements"}
    total = sum(int(s.data["max_rows"]) for s in specs.values())
    assert total <= 400_000, total  # the whole history harvest stays one bounded job
    for s in specs.values():
        assert int(s.data["max_rows"]) <= 150_000, s.id
        if s.data["purpose"] == "statements":
            assert s.data["columns"].get("statement"), f"{s.id}: keep the Wikidata statement id"
            assert "DeprecatedRank" in s.data["sparql"], s.id


def test_class_specs_use_the_documented_root_classes() -> None:
    specs = _specs()
    for spec_id, roots in CLASS_SPECS.items():
        q = specs[spec_id].data["sparql"]
        for r in roots:
            assert f"wd:{r}" in q, (spec_id, r)
        # rank-aware membership: wdt:P31 hides non-preferred P31 values (e.g. "empire" when "historical
        # country" is preferred), so the specs walk every non-deprecated P31 statement
        assert "ps:P31/wdt:P279*" in q and "wdt:P31/wdt:P279*" not in q, spec_id


# --------------------------------------------------------------------------- recordings
def test_recorded_label_lookup_is_intact() -> None:
    meta = json.loads((RECORDED / "label_lookup_1.json").read_text())
    body = (RECORDED / meta["raw_file"]).read_bytes()
    assert _sha(body) == meta["body_sha256"]
    assert _sha(meta["query"].strip().encode()) == meta["query_sha256"]
    assert meta["endpoint"] == "https://query.wikidata.org/sparql"
    assert "wikibase:label" not in meta["query"]


def test_recorded_counts_are_intact() -> None:
    metas = sorted(RECORDED.glob("wdqs_counts_*.json"))
    assert metas
    for m in metas:
        meta = json.loads(m.read_text())
        body = (RECORDED / meta["raw_file"]).read_bytes()
        assert meta["kind"] == "wdqs_counts" and _sha(body) == meta["body_sha256"]
        assert _sha(meta["query"].strip().encode()) == meta["query_sha256"]
        rows = json.loads(body)["results"]["bindings"]
        assert rows and all(int(b["n"]["value"]) >= 0 for b in rows)


def test_recorded_title_lookups_are_intact() -> None:
    metas = sorted(RECORDED.glob("title_lookup*.json"))
    assert metas
    for m in metas:
        meta = json.loads(m.read_text())
        assert meta["kind"] == "title_lookup" and meta["endpoint"] == "https://www.wikidata.org/w/api.php"
    for r in (r for m in metas for r in json.loads(m.read_text())["requests"]):
        body = (RECORDED / r["raw_file"]).read_bytes()
        assert _sha(body) == r["body_sha256"], r["raw_file"]
        assert "action=wbgetentities" in r["url"] and "sites=enwiki" in r["url"]
        assert json.loads(body)["success"] == 1


def test_recorded_snapshots_keep_provenance() -> None:
    files = sorted(RECORDED.glob("snapshot_claims_*.json"))
    assert files
    for p in files:
        doc = json.loads(p.read_text())
        kept = set(doc["properties_kept"])
        assert doc["kind"] == "snapshot_claims" and doc["retrieved_at"]
        for q, e in doc["entities"].items():
            assert re.fullmatch(r"Q\d+", q)
            assert re.fullmatch(r"[0-9a-f]{64}", e["archive_sha256"] or ""), (p.name, q)
            assert isinstance(e["lastrevid"], int)
            for t in e["claims"]:
                assert t["p"] in kept and t["rank"] in ("preferred", "normal"), (q, t)
                assert t["statement"].upper().startswith(q.upper() + "$"), (q, t["statement"])


# --------------------------------------------------------------------------- offline spec emulation
def _instance_of_any(q: str, roots: list[str]) -> bool:
    """q P31/P279* root, using the recorded class claims (classes not recorded stop the walk)."""
    roots_set = set(roots)
    seen: set[str] = set()
    todo = deque(_objs(q, "P31"))
    while todo:
        c = todo.popleft()
        if c in roots_set:
            return True
        if c in seen:
            continue
        seen.add(c)
        todo.extend(_objs(c, "P279"))
    return False


def _countries() -> set[str]:
    if COUNTRY_QIDS.exists():
        doc = json.loads(COUNTRY_QIDS.read_text())
        return {e["qid"] for e in doc["countries"].values() if e.get("qid")}
    return set(COUNTRIES_USED)


def _sitelinks(q: str) -> int:
    return int(_snapshot().get(q, {}).get("sitelinks") or 0)


@cache
def _selection() -> dict[str, tuple[frozenset[str], frozenset[str]]]:
    """spec id -> (entities whose claims the spec harvests [seeds], entities it reaches as statement ends)."""
    snap = _snapshot()
    countries = _countries() & set(snap)
    out: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
    for spec_id, roots in CLASS_SPECS.items():
        seeds = {q for q in snap if _instance_of_any(q, roots)}
        if spec_id == "history.treaties":
            seeds = {q for q in seeds if _objs(q, "P1891") or _sitelinks(q) >= 2}
        if spec_id == "history.eras":
            seeds = {q for q in seeds if _sitelinks(q) >= 5}
        out[spec_id] = (frozenset(seeds), frozenset())
    empires = out["history.empires"][0]
    terr = {q for q in snap if _instance_of_any(q, TERRITORY_CLASSES)
            and (set(_objs(q, "P361")) | set(_objs(q, "P17"))) & empires}
    for e in empires:
        terr |= {x for x in _objs(e, "P527") + _objs(e, "P150") if x in snap and _instance_of_any(x, TERRITORY_CLASSES)}
    out["history.empire_territories"] = (frozenset(terr), frozenset())
    # predecessors of countries: (P1365 | ^P1366)+ and (P155 | ^P156), polity filter
    inv1366: dict[str, set[str]] = defaultdict(set)
    inv156: dict[str, set[str]] = defaultdict(set)
    for q in snap:
        for o in _objs(q, "P1366"):
            inv1366[o].add(q)
        for o in _objs(q, "P156"):
            inv156[o].add(q)
    preds: set[str] = set()
    for c in countries:
        todo, seen = deque([c]), {c}
        while todo:
            n = todo.popleft()
            for m in set(_objs(n, "P1365")) | inv1366[n]:
                if m not in seen:
                    seen.add(m)
                    todo.append(m)
        preds |= (seen - {c}) | set(_objs(c, "P155")) | inv156[c]
    preds = {p for p in preds if _instance_of_any(p, POLITY_CLASSES)}
    out["history.country_predecessors"] = (frozenset(preds), frozenset())
    events = {o for c in countries for o in _objs(c, "P793")}
    out["history.country_event_items"] = (frozenset(events), frozenset())
    out["history.country_events"] = (frozenset(), frozenset(countries | events))
    out["history.empire_event_items"] = (frozenset(o for e in empires for o in _objs(e, "P793")), frozenset())
    discoverers = {(c, o) for c in countries for o in _objs(c, "P61")}
    out["history.country_discoverers"] = (frozenset(), frozenset({c for c, _ in discoverers} | {o for _, o in discoverers}))
    voyages = out["history.expeditions"][0]
    rows = {(q, o) for q in snap if "Q5" in _objs(q, "P31")
            for o in _objs(q, "P1344") + _objs(q, "P793") if o in voyages}
    out["history.explorer_voyages"] = (frozenset(), frozenset({q for q, _ in rows} | {o for _, o in rows}))
    polities = {q for q in snap if _instance_of_any(q, ["Q3024240", "Q48349", "Q133156", "Q164142", "Q161243", "Q6256"])}
    succ: set[str] = set()
    for q in polities:
        for p in ("P1365", "P1366", "P155", "P156"):
            ends = _objs(q, p)
            if ends:
                succ |= {q, *ends}
    out["history.state_succession"] = (frozenset(), frozenset(succ))
    decol = set()
    for q in snap:
        years = [int(t["o"][1:5]) for t in snap[q]["claims"] if t["p"] == "P571" and str(t["o"])[:1] == "+"]
        if _instance_of_any(q, ["Q6256"]) and any(1940 <= y <= 1979 for y in years):
            preds_q = [p for p in _objs(q, "P1365") if _instance_of_any(p, TERRITORY_CLASSES)]
            if preds_q:
                decol |= {q, *preds_q}
    out["history.decolonisation_successions"] = (frozenset(), frozenset(decol))
    return out


def test_emulation_covers_every_history_spec() -> None:
    assert set(_selection()) == set(_specs())


def test_anchor_qids_are_reachable_from_history_specs() -> None:
    allow = load_allowlist()
    reached: dict[str, set[str]] = defaultdict(set)
    for spec_id, (seeds, ends) in _selection().items():
        for q in seeds | ends:
            reached[q].add(spec_id)
        for q in seeds:  # every allow-listed claim of a seeded entity is harvested
            for t in _snapshot().get(q, {}).get("claims", []):
                if t["p"] in allow and isinstance(t["o"], str) and re.fullmatch(r"Q\d+", t["o"]):
                    reached[t["o"]].add(f"{spec_id} via {q} {t['p']}")
    missing = [f"{q} {label}" for q, label in ANCHORS.items() if q not in reached]
    assert not missing, f"anchors not reachable from history specs: {missing}"


def test_key_entities_are_selected_by_the_intended_specs() -> None:
    sel = _selection()
    expect = {
        "history.treaties": ["treaty_of_tordesillas", "treaty_of_paris_1783", "treaty_of_lausanne"],
        "history.empires": ["portuguese_empire", "spanish_empire", "british_empire", "empire_of_brazil"],
        "history.colonies": ["colonial_brazil", "british_raj", "belgian_congo", "french_algeria", "dutch_east_indies"],
        "history.country_predecessors": ["empire_of_brazil", "colonial_brazil", "new_spain", "dominion_of_pakistan"],
        "history.empire_event_items": ["treaty_of_tordesillas", "independence_of_brazil", "discovery_of_brazil"],
        "history.country_event_items": ["louisiana_purchase", "algerian_war"],
        "history.country_discoverers": ["pedro_alvares_cabral"],
        "history.explorer_voyages": ["christopher_columbus", "ferdinand_magellan"],
        "history.decolonisation_successions": ["indonesia", "dutch_east_indies"],
    }
    for spec_id, keys in expect.items():
        seeds, ends = sel[spec_id]
        for k in keys:
            assert _qid(k) in seeds | ends, (spec_id, k)


def test_countries_used_are_crosswalk_countries() -> None:
    if not COUNTRY_QIDS.exists():
        pytest.skip("registry/entities/country_qids.json (WDHARVEST) not yet published")
    assert COUNTRIES_USED <= _countries()


# --------------------------------------------------------------------------- anchor chains
def test_anchor_chains_file_is_well_formed() -> None:
    doc = _chains_doc()
    allow = load_allowlist()
    requested = _requested()
    classes = doc["hop_classes"]
    for name, c in classes.items():
        assert c["properties"] and c["label_en"], name
        for p in c["properties"]:
            assert p in allow or p in requested, f"hop class {name}: {p} neither allow-listed nor requested"
    for p in doc["excluded_properties"] + doc["non_transit_object_properties"]:
        assert re.fullmatch(r"P\d+", p)
    assert set(doc["non_transit_object_properties"]) == NON_TRANSIT_PROPS
    ents = doc["entities"]
    for key, e in ents.items():
        assert re.fullmatch(r"Q\d+", e["qid"]), key
        assert e["verified"] in ("enwiki_title_lookup", "wdqs_label_lookup"), key
    assert len({e["qid"] for e in ents.values()}) == len(ents)
    chains = doc["chains"]
    assert len(chains) == 20 and len({c["id"] for c in chains}) == 20
    for c in chains:
        assert c["from"] in ents and c["to"] in ents and c["from"] != c["to"], c["id"]
        assert 1 <= c["max_hops"] <= 8, c["id"]
        assert c["steps"] and len(c["steps"]) <= c["max_hops"], c["id"]
        for group in c["steps"]:
            assert group and all(x in classes for x in group), (c["id"], group)
        for x in c.get("exclude", []) + c.get("allowed_classes", []):
            assert x in classes, (c["id"], x)
        for w in c.get("via_any", []) + c.get("via_all", []):
            assert w in ents, (c["id"], w)
        assert c["expectation"] in ("path_expected", "path_may_be_absent"), c["id"]
        if c["expectation"] == "path_may_be_absent":
            assert c.get("note"), c["id"]
        assert c["question"].endswith("?")
        for f in c.get("expected_facts", []):
            assert f["subject"] in ents and re.fullmatch(r"P\d+", f["property"]), (c["id"], f)
            assert ("object" in f) != ("year" in f), (c["id"], f)
            assert "object" not in f or f["object"] in ents, (c["id"], f)


def test_owner_examples_are_among_the_chains() -> None:
    ids = {c["id"] for c in _chains_doc()["chains"]}
    for needed in ("columbus_portuguese_brazil", "tordesillas_brazil", "napoleon_louisiana_usa",
                   "berlin_conference_congo", "suez_crisis_usa_decolonisation"):
        assert needed in ids


def test_anchor_chain_entities_were_looked_up_in_wikidata() -> None:
    """Every chain entity QID was confirmed by a recorded lookup (never typed from memory)."""
    titles: dict[str, str] = {}
    for raw in RECORDED.glob("title_lookup*.json.raw"):
        for e in json.loads(raw.read_text())["entities"].values():
            if "missing" not in e:
                titles[e["id"]] = e["sitelinks"]["enwiki"]["title"]
    labels: set[tuple[str, str]] = set()
    meta = json.loads((RECORDED / "label_lookup_1.json").read_text())
    for b in json.loads((RECORDED / meta["raw_file"]).read_text())["results"]["bindings"]:
        labels.add((qid_from_uri(b["item"]["value"]) or "", b["l"]["value"]))
    for key, e in _chains_doc()["entities"].items():
        if e["verified"] == "enwiki_title_lookup":
            assert titles.get(e["qid"]) == e["label_en"], (key, e["qid"], titles.get(e["qid"]))
        else:
            assert (e["qid"], e["label_en"]) in labels, (key, e["qid"])
    for q, label in ANCHORS.items():
        assert titles.get(q) == label, q


# --------------------------------------------------------------------------- witness paths (offline acceptance)
def _graph(allowed: set[str]) -> dict[str, list[tuple[str, str, bool]]]:
    adj: dict[str, list[tuple[str, str, bool]]] = defaultdict(list)
    for s, e in _snapshot().items():
        for t in e["claims"]:
            o = t["o"]
            if t["p"] in allowed and isinstance(o, str) and re.fullmatch(r"Q\d+", o) and o != s:
                adj[s].append((o, t["p"], True))
                adj[o].append((s, t["p"], False))
    return adj


def _steps_match(hop_classes: list[set[str]], steps: list[list[str]]) -> bool:
    i = 0
    for cls in hop_classes:
        if i < len(steps) and cls & set(steps[i]):
            i += 1
    return i == len(steps)


def witness_path(chain: dict[str, Any], *, budget: int = 3_000_000) -> list[tuple[str, str, str]] | None:
    """A simple path satisfying the chain's constraints over the recorded claims, or None.

    Hops keep Wikidata's orientation (subject, property, object); the path is walked from ``from`` to ``to``.
    ``steps`` must be matched in order, each by a distinct hop; nodes that are objects of language/continent/era
    properties (``non_transit_object_properties``) are never passed through.
    """
    doc = _chains_doc()
    classes = {k: set(v["properties"]) for k, v in doc["hop_classes"].items()}
    allowed = set().union(*classes.values()) - set(doc["excluded_properties"])
    for x in chain.get("exclude", []):
        allowed -= classes[x]
    if chain.get("allowed_classes"):
        allowed &= set().union(*(classes[x] for x in chain["allowed_classes"]))
    prop_classes: dict[str, set[str]] = defaultdict(set)
    for name, props in classes.items():
        for p in props:
            prop_classes[p].add(name)
    non_transit = {t["o"] for e in _snapshot().values() for t in e["claims"] if t["p"] in NON_TRANSIT_PROPS}
    adj = _graph(allowed)
    src, dst = _qid(chain["from"]), _qid(chain["to"])
    via_any = {_qid(k) for k in chain.get("via_any", [])}
    via_all = {_qid(k) for k in chain.get("via_all", [])}
    max_hops = int(chain["max_hops"])
    dist = {dst: 0}
    dq = deque([dst])
    while dq:
        n = dq.popleft()
        if dist[n] >= max_hops or (n in non_transit and n != dst):
            continue
        for m, _, _ in adj.get(n, ()):
            if m not in dist:
                dist[m] = dist[n] + 1
                dq.append(m)
    if src not in dist:
        return None
    work = [0]
    found: list[list[tuple[str, str, str]]] = []

    # Prefer explanatory hops (succession, sovereignty, signatures, participation) over loose ones, and entities
    # whose claims were recorded; search by increasing length so the witness is a shortest admissible path.
    rank = {"state_succession": 0, "treaty_signatory": 0, "official_language": 0, "participation": 1,
            "territorial": 1, "voyage_geography": 1, "asserted_causation": 1, "leadership": 2, "location": 2,
            "significant_event": 3, "era": 3}

    def order(item: tuple[str, str, bool]) -> tuple[int, int, str]:
        m, p, _ = item
        return (min((rank.get(c, 4) for c in prop_classes[p]), default=4), 0 if m in _snapshot() else 1, m + p)

    nbrs = {n: sorted(v, key=order) for n, v in adj.items()}

    def dfs(node: str, nodes: list[str], hops: list[tuple[str, str, str]], limit: int) -> None:
        work[0] += 1
        if found or work[0] > budget:
            return
        if node == dst:
            ns = set(nodes)
            if (not via_any or via_any & ns) and via_all <= ns and _steps_match(
                    [prop_classes[h[1]] for h in hops], chain["steps"]):
                found.append(list(hops))
            return
        if len(hops) >= limit or (node in non_transit and node != src):
            return
        for m, p, out in nbrs.get(node, ()):
            if m in nodes or m not in dist or len(hops) + 1 + dist[m] > limit:
                continue
            nodes.append(m)
            hops.append((node, p, m) if out else (m, p, node))
            dfs(m, nodes, hops, limit)
            nodes.pop()
            hops.pop()

    for limit in range(dist[src], max_hops + 1):
        dfs(src, [src], [], limit)
        if found:
            break
    return found[0] if found else None


@pytest.mark.parametrize("chain_id", [c["id"] for c in yaml.safe_load(CHAINS.read_text())["chains"]])
def test_anchor_chain_witness_matches_its_expectation(chain_id: str) -> None:
    chain = next(c for c in _chains_doc()["chains"] if c["id"] == chain_id)
    path = witness_path(chain)
    if chain["expectation"] == "path_expected":
        assert path is not None, f"{chain_id}: no witness path in the recorded claims"
        assert len(path) <= chain["max_hops"]
    else:
        assert path is None, f"{chain_id}: a sourced path now exists ({path}); relabel it path_expected"


def test_expected_facts_are_recorded_statements() -> None:
    n = 0
    for c in _chains_doc()["chains"]:
        for f in c.get("expected_facts", []):
            n += 1
            claims = [t for t in _snapshot()[_qid(f["subject"])]["claims"] if t["p"] == f["property"]]
            if "object" in f:
                assert any(t["o"] == _qid(f["object"]) for t in claims), (c["id"], f)
            else:
                assert any(str(t["o"]).startswith(f"+{int(f['year']):04d}-") for t in claims), (c["id"], f)
    assert n >= 40
