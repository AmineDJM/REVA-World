"""KGPEOPLE: people query specs (registry/kg/queries/people) and their recorded anchor probes.

Offline. Checks that

* every people spec validates against WDHARVEST's schema and property allow-list
  (``reva.kg.harvest.validate_spec``), is bounded (``max_rows``, timeout <= 60 s) and that the person seed
  specs together stay within the people budget (sum of ``max_rows`` <= 300 000 -- an upper bound on the
  number of distinct people, since a person selected by several specs is fetched once);
* the recorded probe responses in ``registry/kg/queries/people/_recorded/`` are real WDQS responses of the
  *current* spec queries: each membership probe's text is rebuilt here from the specs
  (:func:`membership_query`) and must equal the recorded text and its sha256, and the body must match its
  sha256 -- a spec edit without a re-recording fails; every enabled people spec is covered by a probe;
* the anchor people (Columbus ... Einstein) are captured by the seed sets, each by the spec(s) one expects;
* every class/occupation/award QID written in a people spec was confirmed by the recorded label check
  (no QID typed from memory goes unverified).

Membership probe = the spec's own SPARQL with its split placeholders filled for the whole range (``values``
-> the full list, year window -> ``from``..``to``, QID-prefix filter dropped) and the selected variable
(seed ``?item``; statements: the object) restricted to the anchor QIDs, several specs per UNION request,
whitespace collapsed (the client sends GET requests capped at 7 500 URL characters). Wikidata traffic is
scarce (one machine-wide lock, long 429 cool-downs), hence few, combined requests. The anchor QIDs and the
class QIDs come from a recorded ``resolve`` request (English label lookups), not from memory.

Recorder: the probes were run with WDHARVEST's ``WikidataClient`` (machine lock, archive) by a small script
that imports :func:`resolve_query` / :func:`membership_query` / :func:`pack_membership` from this module and
copies the provider body out of the content-addressed archive (``var/kg/objects``) into ``_recorded/``.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from reva.adapters.connectors.wikidata import load_allowlist, qid_from_uri
from reva.kg.harvest import discover_specs, validate_spec

ROOT = Path(__file__).resolve().parents[2]
PEOPLE = ROOT / "registry" / "kg" / "queries" / "people"
RECORDED = PEOPLE / "_recorded"
PEOPLE_BUDGET = 300_000

# Anchor people: English Wikidata label -> QID. QIDs are confirmed by the recorded probe (anchor branch:
# label + instance of human + highest sitelinks among same-label humans), not typed from memory.
ANCHORS: dict[str, str] = {
    "Christopher Columbus": "Q7322",
    "Pedro Álvares Cabral": "Q174432",
    "Vasco da Gama": "Q7328",
    "Isabella I of Castile": "Q45859",
    "John II of Portugal": "Q217637",
    "Pedro I of Brazil": "Q939",
    "Dwight D. Eisenhower": "Q9916",
    "John F. Kennedy": "Q9696",
    "Charles de Gaulle": "Q2042",
    "Kwame Nkrumah": "Q8620",
    "Patrice Lumumba": "Q161672",
    "Ho Chi Minh": "Q36014",
    "Gamal Abdel Nasser": "Q39524",
    "Jawaharlal Nehru": "Q1047",
    "Simón Bolívar": "Q8605",
    "Napoleon": "Q517",
    "Marie Curie": "Q7186",
    "Albert Einstein": "Q937",
}

# Anchors whose Wikidata English label differs from the name used by the owner: name -> candidate labels.
WD_LABELS: dict[str, tuple[str, ...]] = {
    "Marie Curie": ("Marie Curie", "Marie Skłodowska-Curie", "Maria Skłodowska-Curie"),
}


def anchor_labels(names: list[str] | None = None) -> list[str]:
    """English labels to resolve for the given anchor names (default: all)."""
    out: list[str] = []
    for n in names or list(ANCHORS):
        out.extend(WD_LABELS.get(n, (n,)))
    return out


# Which spec(s) must select each anchor (at least one of the listed specs; most are in several).
EXPECTED: dict[str, tuple[str, ...]] = {
    "Christopher Columbus": ("people.explorers_navigators",),
    "Pedro Álvares Cabral": ("people.explorers_navigators",),
    "Vasco da Gama": ("people.explorers_navigators",),
    "Isabella I of Castile": ("people.monarchs",),
    "John II of Portugal": ("people.monarchs",),
    "Pedro I of Brazil": ("people.monarchs", "people.state_leaders"),
    "Dwight D. Eisenhower": ("people.state_leaders",),
    "John F. Kennedy": ("people.state_leaders",),
    "Charles de Gaulle": ("people.state_leaders",),
    "Kwame Nkrumah": ("people.state_leaders",),
    "Patrice Lumumba": ("people.state_leaders",),
    "Ho Chi Minh": ("people.state_leaders",),
    "Gamal Abdel Nasser": ("people.state_leaders",),
    "Jawaharlal Nehru": ("people.state_leaders",),
    "Simón Bolívar": ("people.state_leaders", "people.revolutionaries_activists"),
    "Napoleon": ("people.monarchs", "people.state_leaders"),
    "Marie Curie": ("people.nobel_laureates",),
    "Albert Einstein": ("people.nobel_laureates",),
}

# Extra English labels looked up in the same request (candidate classes for later spec revisions).
LOOKUP_LABELS: tuple[str, ...] = (
    "navigator", "conquistador", "viceroy", "governor-general", "colonial governor", "colonial administrator",
    "religious leader", "prophet", "trade unionist", "independence activist", "essayist", "polar explorer",
    "sailor", "military commander", "general officer", "admiral", "head of state", "head of government",
    "monarch", "pope", "caliph", "missionary", "cartographer",
)


# --------------------------------------------------------------------------- probe construction
def spec_whole_range_query(spec: dict[str, Any]) -> str:
    """The spec's SPARQL with split placeholders filled to cover every chunk at once."""
    q = spec["sparql"]
    split = spec.get("split") or {}
    if split.get("by") == "values":
        assert split["source"] == "list", "people probes only support split.source=list"
        q = q.replace("{{values}}", " ".join(f"wd:{x}" for x in split["qids"]))
    elif split.get("by") == "year":
        q = q.replace("{{year_from}}", str(split["from"])).replace("{{year_to}}", str(split["to"]))
    elif split.get("by") == "qid_prefix":
        q = re.sub(r"[ \t]*\{\{qid_filter\}\}[ \t]*\n?", "", q)
    return q


def probe_variable(spec: dict[str, Any]) -> str:
    cols = spec.get("columns") or {}
    return cols.get("item", "item") if spec["purpose"] == "seed_entities" else cols["object"]


def _lits(labels: list[str] | tuple[str, ...]) -> str:
    return " ".join(json.dumps(x, ensure_ascii=False) + "@en" for x in labels)


def spec_class_qids(spec: dict[str, Any]) -> set[str]:
    """Every wd:Q... written in a spec (classes, occupations, awards, positions) incl. split lists."""
    found = set(re.findall(r"\bwd:(Q[1-9][0-9]*)\b", spec["sparql"]))
    split = spec.get("split") or {}
    if split.get("by") == "values" and split.get("source") == "list":
        found |= set(split["qids"])
    return found


def _min(q: str) -> str:
    """Collapse whitespace (WDQS is called with GET; the client caps the URL at 7 500 characters)."""
    return re.sub(r"\s+", " ", q).strip()


def resolve_query(specs: dict[str, dict[str, Any]], labels: list[str],
                  lookup_labels: tuple[str, ...] = LOOKUP_LABELS, anchor_qids: tuple[str, ...] = ()) -> str:
    """Anchor label -> human QIDs (``?a_l ?a_item ?a_sl``), English labels of every class QID the specs use
    (``?c_item ?c_l``) and candidate-class label lookups (``?lk_l ?lk_item ?lk_sl``); rows tagged ``?part``."""
    classes = sorted(set().union(*(spec_class_qids(s) for s in specs.values())), key=lambda q: int(q[1:]))
    branches = [
        f"{{ VALUES ?a_l {{ {_lits(labels)} }} ?a_item rdfs:label ?a_l ; wdt:P31 wd:Q5 ; wikibase:sitelinks ?a_sl . "
        f"BIND(\"anchor\" AS ?part) }}",
        f"{{ VALUES ?c_item {{ {' '.join('wd:' + q for q in classes)} }} "
        f"OPTIONAL {{ ?c_item rdfs:label ?c_l . FILTER(LANG(?c_l) = \"en\") }} BIND(\"class\" AS ?part) }}",
    ]
    if anchor_qids:  # reverse check: QID -> English label (rows tagged "anchor" too)
        branches.append(f"{{ VALUES ?a_item {{ {' '.join('wd:' + q for q in anchor_qids)} }} ?a_item rdfs:label ?a_l ; "
                        f"wdt:P31 wd:Q5 ; wikibase:sitelinks ?a_sl . FILTER(LANG(?a_l) = \"en\") "
                        f"BIND(\"anchor\" AS ?part) }}")
    if lookup_labels:  # an empty VALUES would leave ?lk_l unbound: a scan of every label (WDQS timeout)
        branches.append(f"{{ VALUES ?lk_l {{ {_lits(lookup_labels)} }} ?lk_item rdfs:label ?lk_l ; "
                        f"wikibase:sitelinks ?lk_sl . FILTER(?lk_sl >= 10) BIND(\"lookup\" AS ?part) }}")
    return ("SELECT DISTINCT ?part ?a_l ?a_item ?a_sl ?c_item ?c_l ?lk_l ?lk_item ?lk_sl WHERE { "
            + " UNION ".join(branches) + " }")


def membership_branch(sid: str, spec: dict[str, Any], qids: list[str]) -> str:
    """The spec's own pattern (whole split range) with its selected variable restricted to ``qids``."""
    q = spec_whole_range_query(spec)
    m = re.search(r"WHERE\s*\{", q)
    assert m, "spec query has no WHERE {"
    body = q[m.end(): q.rindex("}")]
    var = probe_variable(spec)
    bind = f"BIND(?{var} AS ?m_s)"
    if spec["purpose"] == "statements":
        bind += f" BIND(?{spec['columns']['subject']} AS ?m_o)"
    return _min(f"{{ VALUES ?{var} {{ {' '.join('wd:' + x for x in qids)} }} {body} {bind} BIND(\"m:{sid}\" AS ?part) }}")


def membership_query(specs: dict[str, dict[str, Any]], spec_ids: list[str], qids: list[str]) -> str:
    """One request probing several specs: rows ``?part = m:<spec id>``, ``?m_s`` selected anchor, ``?m_o``
    statement subject (statements specs)."""
    return ("SELECT DISTINCT ?part ?m_s ?m_o WHERE { "
            + " UNION ".join(membership_branch(s, specs[s], qids) for s in spec_ids) + " }")


def pack_membership(specs: dict[str, dict[str, Any]], qids: list[str], max_url: int = 7000) -> list[list[str]]:
    """Greedy packing of spec ids (sorted) into requests whose GET URL stays under ``max_url``."""
    from urllib.parse import urlencode

    def url_len(ids: list[str]) -> int:
        q = membership_query(specs, ids, qids) + "\nLIMIT 2000"
        return len("https://query.wikidata.org/sparql?" + urlencode({"query": q, "format": "json"}))

    groups: list[list[str]] = []
    for sid in sorted(specs):
        if groups and url_len(groups[-1] + [sid]) <= max_url:
            groups[-1].append(sid)
        else:
            assert url_len([sid]) <= max_url, f"{sid}: probe alone exceeds the GET limit"
            groups.append([sid])
    return groups


# --------------------------------------------------------------------------- helpers
def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _specs() -> dict[str, Any]:
    return {s.id: s for s in discover_specs([PEOPLE])}


def _spec_data() -> dict[str, dict[str, Any]]:
    return {k: v.data for k, v in _specs().items() if v.data.get("enabled", True)}


def _recordings(kind: str) -> list[dict[str, Any]]:
    out = []
    for p in sorted(RECORDED.glob("*.json")):
        rec = json.loads(p.read_text())
        if rec.get("kind") != kind:
            continue
        rec["_path"] = p
        rec["_body"] = (RECORDED / rec["raw_file"]).read_bytes()
        out.append(rec)
    assert out, f"no recorded {kind} probe in {RECORDED}"
    return out


def _rows(rec: dict[str, Any]) -> list[dict[str, str]]:
    doc = json.loads(rec["_body"])
    return [{k: (qid_from_uri(v["value"]) or v["value"]) for k, v in b.items()} for b in doc["results"]["bindings"]]


def _all_rows(kind: str) -> list[dict[str, str]]:
    return [r for rec in _recordings(kind) for r in _rows(rec)]


# --------------------------------------------------------------------------- specs
def test_people_specs_validate_against_schema_and_allowlist() -> None:
    allow = load_allowlist()
    specs = discover_specs([PEOPLE])
    assert len(specs) >= 12
    known = [s.id for s in discover_specs()]
    for s in specs:
        assert validate_spec(s, allow, known_ids=known) == [], s.path.name
        assert s.data["domain"] == "people" and s.data["id"].startswith("people.")
        assert s.data.get("owner") == "KGPEOPLE"
        assert "wikibase:label" not in s.data["sparql"]


def test_people_specs_are_bounded_and_within_people_budget() -> None:
    specs = _specs()
    person_seeds = [s for s in specs.values()
                    if s.data["purpose"] == "seed_entities" and s.data.get("entity_kind") == "person"]
    assert len(person_seeds) >= 10
    total = sum(int(s.data["max_rows"]) for s in person_seeds)
    assert total <= PEOPLE_BUDGET, total
    for s in specs.values():
        assert 1 <= int(s.data["max_rows"]) <= 60_000, s.id
        assert int(s.data.get("timeout_seconds", 55)) <= 60, s.id
        # each request is either a single pattern or split into chunks; values lists are explicit
        split = s.data.get("split") or {}
        if split.get("by") == "values":
            assert split["source"] == "list" and split["qids"], s.id


def test_people_specs_never_keep_deprecated_rank() -> None:
    for s in _specs().values():
        q = s.data["sparql"]
        if re.search(r"\bp:P\d+", q):
            assert "DeprecatedRank" in q, s.id


def test_person_seeds_keep_biography_properties() -> None:
    need = {"P27", "P569", "P570", "P19", "P20", "P39", "P1344", "P800", "P737", "P108", "P463", "P106", "P166"}
    for s in _specs().values():
        if s.data["purpose"] == "seed_entities" and s.data.get("entity_kind") == "person":
            assert need <= set(s.data["properties"]), (s.id, sorted(need - set(s.data["properties"])))


def test_country_heads_statements_carry_time_qualifiers() -> None:
    spec = _specs()["people.country_heads"].data
    cols = spec["columns"]
    assert cols["property_var"] and cols["start"] and cols["end"] and cols["statement"] and cols["rank"]
    assert "p:P35" in spec["sparql"] and "p:P6" in spec["sparql"]


# --------------------------------------------------------------------------- recordings
@pytest.mark.parametrize("kind", ["resolve", "membership"])
def test_recordings_are_intact_wdqs_responses(kind: str) -> None:
    for rec in _recordings(kind):
        name = rec["_path"].name
        assert _sha(rec["_body"]) == rec["body_sha256"], name
        assert _sha(rec["query"].strip().encode()) == rec["query_sha256"], name
        assert rec["endpoint"] == "https://query.wikidata.org/sparql" and rec["retrieved_at"], name
        assert json.loads(rec["_body"])["results"]["bindings"] is not None
        assert rec["rows"] == len(json.loads(rec["_body"])["results"]["bindings"]), name


def test_membership_probes_match_current_specs() -> None:
    specs = _spec_data()
    covered: set[str] = set()
    for rec in _recordings("membership"):
        assert rec["probe_qids"] == list(ANCHORS.values()), rec["_path"].name
        expected = membership_query(specs, rec["spec_ids"], rec["probe_qids"])
        assert re.sub(r"\s*LIMIT \d+$", "", rec["query"].strip()) == expected, (
            f"{rec['_path'].name}: a people spec changed since the probe was recorded; re-record it")
        covered |= set(rec["spec_ids"])
    assert covered == set(specs), f"specs without a membership probe: {sorted(set(specs) - covered)}"


def test_anchor_qids_confirmed_by_recorded_lookup() -> None:
    """For each anchor, the human with the most sitelinks among those carrying one of its English labels."""
    best: dict[str, tuple[int, str]] = {}
    label_to_name = {lab: n for n in ANCHORS for lab in WD_LABELS.get(n, (n,))}
    for r in _all_rows("resolve"):
        if r.get("part") != "anchor" or r["a_l"] not in label_to_name:
            continue
        name, sl = label_to_name[r["a_l"]], int(r["a_sl"])
        if name not in best or sl > best[name][0]:
            best[name] = (sl, r["a_item"])
    for name, qid in ANCHORS.items():
        assert name in best, f"{name}: no human with this English label in the recordings"
        assert best[name][1] == qid, (name, best[name], qid)


@pytest.mark.parametrize("label", list(ANCHORS))
def test_anchor_people_are_captured_by_seed_sets(label: str) -> None:
    qid = ANCHORS[label]
    selected_by = {r["part"][2:] for r in _all_rows("membership")
                   if r.get("part", "").startswith("m:") and r.get("m_s") == qid}
    assert selected_by, f"{label} ({qid}) is selected by no people spec"
    assert selected_by & set(EXPECTED[label]), (label, sorted(selected_by), EXPECTED[label])


def test_class_qids_used_by_specs_are_confirmed() -> None:
    """Every QID written in a spec has an English label in a recorded response: either the class check
    (QID -> label) or a label lookup (label -> QID)."""
    labels: dict[str, str] = {}
    for r in _all_rows("resolve"):
        if r.get("part") == "class" and r.get("c_l"):
            labels[r["c_item"]] = r["c_l"]
        elif r.get("part") == "lookup":
            labels.setdefault(r["lk_item"], r["lk_l"])
    used = set().union(*(spec_class_qids(s) for s in _spec_data().values()))
    missing = sorted(q for q in used if q not in labels)
    assert not missing, f"class QIDs without a recorded English label: {missing}"


def test_country_heads_rows_link_people_to_their_states() -> None:
    """The P35/P6 statements probe returns (state, person) pairs for anchor leaders."""
    pairs = {(r["m_o"], r["m_s"]) for r in _all_rows("membership") if r.get("part") == "m:people.country_heads"}
    assert pairs, "no P35/P6 statement row for any anchor"
    for state, person in pairs:
        assert re.fullmatch(r"Q\d+", state) and person in ANCHORS.values()
