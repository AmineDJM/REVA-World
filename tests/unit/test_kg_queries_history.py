"""KGHIST: history query specs (registry/kg/queries/history) and anchor chains (registry/kg/anchor_chains.yaml).

Offline. Checks that

* every history spec validates against WDHARVEST's schema and allow-list (``reva.kg.harvest.validate_spec``);
* the recorded probe responses in ``registry/kg/queries/history/_recorded/`` are real WDQS responses of the
  *current* spec queries (query text rebuilt here from the spec + the probe QIDs must match the recorded text
  and its sha256; the raw body must match its sha256) -- a spec edit without a re-recording fails;
* the anchor QIDs (Columbus, Tordesillas, Brazil, Portugal, Spain, Portuguese, Cabral, United States) are
  reachable from the specs' seed sets: either selected by a spec, or the object/subject of an allow-listed
  claim of an entity a spec selects (recorded ``hop`` probes);
* the anchor chains file is well formed: endpoints and hop classes refer to known QIDs/properties, every
  required hop class maps to allow-listed (or requested) properties, and the expected facts are backed by a
  recorded response.
"""

from __future__ import annotations

import hashlib
import json
import re
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

ANCHORS = {
    "Q7322": "Christopher Columbus",
    "Q45": "Portugal",
    "Q29": "Spain",
    "Q155": "Brazil",
    "Q5146": "Portuguese",
    "Q30": "United States",
}
# Anchors whose QID was looked up (not known beforehand); filled from the chains file's verified entities.
LOOKED_UP_ANCHORS = ("treaty_of_tordesillas", "pedro_alvares_cabral")


# --------------------------------------------------------------------------- probe query construction
def probe_query(spec: dict[str, Any], qids: list[str]) -> str:
    """The query a probe sends: the spec's own SPARQL restricted to ``qids``.

    Split placeholders are filled so that the probe covers the spec's whole range: ``values`` gets the probe
    QIDs, a year window gets the spec's ``from``..``to``, a QID-prefix filter is dropped. For any other spec a
    ``VALUES ?<item|subject> { ... }`` line is injected right after the first ``WHERE {``.
    """
    q = spec["sparql"]
    split = spec.get("split") or {}
    values = " ".join(f"wd:{x}" for x in qids)
    if split.get("by") == "values":
        return q.replace("{{values}}", values).strip()
    if split.get("by") == "year":
        q = q.replace("{{year_from}}", str(split["from"])).replace("{{year_to}}", str(split["to"]))
    if split.get("by") == "qid_prefix":
        q = re.sub(r"[ \t]*\{\{qid_filter\}\}[ \t]*\n?", "", q)
    cols = spec.get("columns") or {}
    var = cols.get("item", "item") if spec["purpose"] == "seed_entities" else cols["subject"]
    m = re.search(r"WHERE\s*\{", q)
    assert m, "spec query has no WHERE {"
    return (q[: m.end()] + f"\n  VALUES ?{var} {{ {values} }}" + q[m.end():]).strip()


def _branch_body(query: str) -> str:
    """The group pattern of ``SELECT ... WHERE { <body> }`` (without the outer braces)."""
    m = re.search(r"WHERE\s*\{", query)
    assert m
    return query[m.end(): query.rindex("}")].strip("\n")


def _vals(qids: list[str]) -> str:
    return " ".join(f"wd:{q}" for q in qids)


def _lits(labels: list[str]) -> str:
    return " ".join(json.dumps(x, ensure_ascii=False) + "@en" for x in labels)


def combined_query(parts: list[dict[str, Any]], specs: dict[str, dict[str, Any]]) -> str:
    """One WDQS request answering several small probes (Wikidata traffic is scarce: one shared lock and long
    429 cool-downs). Each UNION branch tags its rows with ``?part``:

    * ``membership``: the spec's own probe query (:func:`probe_query`) -> ``?m_s`` (seed item / subject) and
      ``?m_o`` (statement object);
    * ``label_lookup``: English label -> items (``?lk_l ?lk_item ?lk_sl``), to confirm QIDs;
    * ``label_hop``: a label lookup whose items with at least ``hop_min_sitelinks`` sitelinks also return their
      truthy claims for the listed properties (``?h_s ?h_p ?h_o``);
    * ``hop``: truthy claims (``?h_s ?h_p ?h_o``) of listed subjects (QIDs, or English labels with at least
      ``min_sitelinks`` sitelinks) for listed properties;
    * ``hop_reverse``: truthy claims pointing at listed objects for listed properties.
    """
    branches = []
    for i, part in enumerate(parts):
        tag = f"{i}:{part['kind']}" + (f":{part['spec_id']}" if part.get("spec_id") else "")
        k = part["kind"]
        if k == "membership":
            spec = specs[part["spec_id"]]
            cols = spec.get("columns") or {}
            body = _branch_body(probe_query(spec, part["qids"]))
            if spec["purpose"] == "seed_entities":
                bind = f"BIND(?{cols.get('item', 'item')} AS ?m_s)"
            else:
                bind = f"BIND(?{cols['subject']} AS ?m_s) BIND(?{cols['object']} AS ?m_o)"
            branches.append(f"{{\n{body}\n  {bind}\n  BIND(\"{tag}\" AS ?part)\n}}")
        elif k == "label_lookup":
            branches.append(
                f"{{\n  VALUES ?lk_l {{ {_lits(part['labels'])} }}\n  ?lk_item rdfs:label ?lk_l ; wikibase:sitelinks ?lk_sl .\n"
                f"  FILTER(?lk_sl >= {int(part['min_sitelinks'])})\n  BIND(\"{tag}\" AS ?part)\n}}")
        elif k == "label_hop":
            branches.append(
                f"{{\n  VALUES ?lk_l {{ {_lits(part['labels'])} }}\n  ?lk_item rdfs:label ?lk_l ; wikibase:sitelinks ?lk_sl .\n"
                f"  FILTER(?lk_sl >= {int(part['min_sitelinks'])})\n"
                f"  OPTIONAL {{\n    FILTER(?lk_sl >= {int(part['hop_min_sitelinks'])})\n"
                f"    VALUES ?h_p {{ {_vals(part['properties'])} }}\n    ?h_p wikibase:directClaim ?h_w .\n"
                f"    ?lk_item ?h_w ?h_o .\n    BIND(?lk_item AS ?h_s)\n  }}\n  BIND(\"{tag}\" AS ?part)\n}}")
        elif k == "hop":
            subj = []
            if part.get("qids"):
                subj.append(f"{{ VALUES ?h_s {{ {_vals(part['qids'])} }} }}")
            if part.get("labels"):
                subj.append(f"{{ VALUES ?h_l {{ {_lits(part['labels'])} }} ?h_s rdfs:label ?h_l ; wikibase:sitelinks ?h_sl . "
                            f"FILTER(?h_sl >= {int(part['min_sitelinks'])}) }}")
            branches.append(
                f"{{\n  {' UNION '.join(subj)}\n  VALUES ?h_p {{ {_vals(part['properties'])} }}\n"
                f"  ?h_p wikibase:directClaim ?h_w .\n  ?h_s ?h_w ?h_o .\n  BIND(\"{tag}\" AS ?part)\n}}")
        elif k == "hop_reverse":
            branches.append(
                f"{{\n  VALUES ?h_o {{ {_vals(part['objects'])} }}\n  VALUES ?h_p {{ {_vals(part['properties'])} }}\n"
                f"  ?h_p wikibase:directClaim ?h_w .\n  ?h_s ?h_w ?h_o .\n  BIND(\"{tag}\" AS ?part)\n}}")
        else:  # pragma: no cover
            raise AssertionError(k)
    return ("SELECT DISTINCT ?part ?m_s ?m_o ?lk_l ?lk_item ?lk_sl ?h_s ?h_p ?h_o WHERE {\n"
            + "\nUNION\n".join(branches) + "\n}")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _specs() -> dict[str, Any]:
    return {s.id: s for s in discover_specs([HIST])}


def _recordings() -> list[dict[str, Any]]:
    out = []
    for p in sorted(RECORDED.glob("*.json")):
        rec = json.loads(p.read_text())
        rec["_path"] = p
        out.append(rec)
    return out


def _rows(rec: dict[str, Any]) -> list[dict[str, str]]:
    body = (RECORDED / rec["raw_file"]).read_bytes()
    doc = json.loads(body)
    return [{k: (qid_from_uri(v["value"]) or v["value"]) for k, v in b.items()} for b in doc["results"]["bindings"]]


def _requested() -> set[str]:
    if not REQUESTS.exists():
        return set()
    return {r["property"] for r in yaml.safe_load(REQUESTS.read_text()) or []}


# --------------------------------------------------------------------------- specs
def test_history_specs_validate_against_schema_and_allowlist() -> None:
    allow = load_allowlist()
    specs = discover_specs([HIST])
    assert len(specs) >= 10
    known = [s.id for s in discover_specs()]
    for s in specs:
        assert validate_spec(s, allow, known_ids=known) == [], s.path.name
        assert s.data["domain"] == "history" and s.data["id"].startswith("history.")
        assert s.data.get("owner") == "KGHIST"
        assert "LIMIT" not in s.data["sparql"].upper().split("}")[-1]


def test_history_specs_are_bounded_and_cover_both_purposes() -> None:
    specs = _specs()
    purposes = {s.data["purpose"] for s in specs.values()}
    assert purposes == {"seed_entities", "statements"}
    total = sum(int(s.data["max_rows"]) for s in specs.values())
    assert total <= 400_000, total  # the whole history harvest stays a bounded job
    for s in specs.values():
        assert int(s.data["max_rows"]) <= 150_000, s.id
        assert int(s.data.get("timeout_seconds", 55)) <= 60


def test_history_specs_do_not_use_label_service_or_deprecated_rows() -> None:
    for s in _specs().values():
        q = s.data["sparql"]
        assert "wikibase:label" not in q
        if s.data["purpose"] == "statements" and s.data["columns"].get("rank"):
            assert "DeprecatedRank" in q, s.id


# --------------------------------------------------------------------------- recordings
def test_recordings_match_current_specs_and_raw_bodies() -> None:
    specs = _specs()
    recs = _recordings()
    assert recs, "no recorded probes"
    for rec in recs:
        body = (RECORDED / rec["raw_file"]).read_bytes()
        assert _sha(body) == rec["raw_sha256"], rec["_path"].name
        sent = rec["query"]
        assert _sha(sent.strip().encode()) == rec["query_sha256"]
        if rec["kind"] == "membership":
            spec = specs[rec["spec_id"]].data
            expected = probe_query(spec, rec["probe_qids"])
            # the client appends "LIMIT n": the probe must be the current spec query + that limit
            assert re.sub(r"\nLIMIT \d+$", "", sent.strip()) == expected, (
                f"{rec['_path'].name}: spec {rec['spec_id']} changed since the probe was recorded; re-record")
        assert rec["endpoint"] == "https://query.wikidata.org/sparql"
        assert rec["retrieved_at"]


def _membership() -> dict[str, set[str]]:
    """QID -> spec ids that select it (seed item or statement subject/object)."""
    specs = _specs()
    out: dict[str, set[str]] = {}
    for rec in _recordings():
        if rec["kind"] != "membership":
            continue
        spec = specs[rec["spec_id"]].data
        cols = spec.get("columns") or {}
        if spec["purpose"] == "seed_entities":
            keys = [cols.get("item", "item")]
        else:
            keys = [cols["subject"], cols["object"]]
        for row in _rows(rec):
            for k in keys:
                if k in row and re.fullmatch(r"Q\d+", row[k]):
                    out.setdefault(row[k], set()).add(rec["spec_id"])
    return out


def _hops() -> list[dict[str, str]]:
    out = []
    for rec in _recordings():
        if rec["kind"] == "hop":
            for row in _rows(rec):
                out.append(row)
    return out


def _chains() -> dict[str, Any]:
    return yaml.safe_load(CHAINS.read_text())


def _anchor_qids() -> dict[str, str]:
    anchors = dict(ANCHORS)
    ents = _chains()["entities"]
    for key in LOOKED_UP_ANCHORS:
        anchors[ents[key]["qid"]] = ents[key]["label_en"]
    return anchors


def test_anchor_qids_are_reachable_from_history_seed_sets() -> None:
    member = _membership()
    hops = _hops()
    allow = load_allowlist()
    seeded = {q for q, ids in member.items() if any(i for i in ids)}
    missing = []
    for qid, label in _anchor_qids().items():
        if qid in member:
            continue
        # one allow-listed claim away from an entity a spec selects
        via = [h for h in hops if h["o"] == qid and h["s"] in seeded and h["p"] in allow]
        via += [h for h in hops if h["s"] == qid and h["o"] in seeded and h["p"] in allow]
        if not via:
            missing.append(f"{qid} {label}")
    assert not missing, f"anchors not reachable from history seeds: {missing}"


def test_seeded_entities_are_directly_selected_for_key_specs() -> None:
    member = _membership()
    ents = _chains()["entities"]
    expect = {
        ents["treaty_of_tordesillas"]["qid"]: "history.treaties",
        ents["portuguese_empire"]["qid"]: "history.empires",
        ents["colonial_brazil"]["qid"]: "history.country_predecessors",
        "Q155": "history.country_events",
    }
    for qid, spec_id in expect.items():
        assert spec_id in member.get(qid, set()), (qid, spec_id, member.get(qid))


# --------------------------------------------------------------------------- anchor chains
def test_anchor_chains_file_is_well_formed() -> None:
    doc = _chains()
    allow = load_allowlist()
    requested = _requested()
    classes = doc["hop_classes"]
    for name, c in classes.items():
        assert c["properties"], name
        for p in c["properties"]:
            assert p in allow or p in requested, f"hop class {name}: {p} neither allow-listed nor requested"
    for p in doc["excluded_properties"]:
        assert re.fullmatch(r"P\d+", p)
    ents = doc["entities"]
    for key, e in ents.items():
        assert re.fullmatch(r"Q\d+", e["qid"]), key
        assert e["label_en"]
        assert e["verified"] in ("wdqs_label_lookup", "known_anchor"), key
    chains = doc["chains"]
    assert len(chains) == 20
    assert len({c["id"] for c in chains}) == 20
    for c in chains:
        assert c["from"] in ents and c["to"] in ents, c["id"]
        assert 2 <= c["max_hops"] <= 8
        for cls in c.get("require_all", []):
            assert cls in classes, (c["id"], cls)
        for group in c.get("require_any", []):
            assert all(cls in classes for cls in group), (c["id"], group)
        for cls in c.get("exclude", []):
            assert cls in classes, (c["id"], cls)
        for w in c.get("via_any", []):
            assert w in ents, (c["id"], w)
        assert c.get("require_all") or c.get("require_any"), c["id"]
        assert c["expectation"] in ("path_expected", "path_may_be_absent"), c["id"]


def test_owner_examples_are_among_the_chains() -> None:
    ids = {c["id"] for c in _chains()["chains"]}
    for needed in ("columbus_portuguese_brazil", "tordesillas_brazil", "napoleon_louisiana_usa",
                   "berlin_conference_congo", "suez_crisis_usa_decolonisation"):
        assert needed in ids


def test_anchor_chain_entities_were_looked_up_in_wikidata() -> None:
    """Every chain entity QID was confirmed by a recorded WDQS label lookup (no QID typed from memory)."""
    ents = _chains()["entities"]
    seen: dict[str, str] = {}
    for rec in _recordings():
        if rec["kind"] == "label_lookup":
            for row in _rows(rec):
                seen[row["item"]] = row["l"]
    for key, e in ents.items():
        assert e["qid"] in seen, f"{key} {e['qid']} not confirmed by a recorded lookup"
        assert seen[e["qid"]] == e["label_en"], (key, seen[e["qid"]], e["label_en"])


@pytest.mark.parametrize("fact_index", range(0, 40))
def test_expected_facts_are_backed_by_recorded_responses(fact_index: int) -> None:
    facts = [(c["id"], f) for c in _chains()["chains"] for f in c.get("expected_facts", [])]
    if fact_index >= len(facts):
        pytest.skip("fewer facts")
    chain_id, f = facts[fact_index]
    ents = _chains()["entities"]
    s = ents[f["subject"]]["qid"]
    rows = [h for h in _hops() if h["s"] == s and h["p"] == f["property"]]
    if "object" in f:
        o = ents[f["object"]]["qid"]
        assert any(h["o"] == o for h in rows), (chain_id, f)
    if "year" in f:
        assert any(str(h["o"]).startswith(f"{f['year']:04d}-") or str(h["o"]).startswith(f"+{f['year']:04d}-")
                   for h in rows), (chain_id, f, rows)
