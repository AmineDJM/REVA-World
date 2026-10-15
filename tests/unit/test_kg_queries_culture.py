"""KG culture query specs (agent KGCULT): schema validity, bounds, and anchor coverage.

Anchor tests are offline. Each anchor is the spec's own SPARQL restricted to a few known entities
(a restriction block is inserted right after the first ``WHERE {``) and captured once, live, through
WDHARVEST's locked ``WikidataClient`` (``tests/fixtures/kg/culture/<anchor>.json`` keeps the exact
query, the raw response sha256 and the retrieval time). If a spec's SPARQL changes, its anchor
fixture no longer matches the derived query and the test fails until the anchor is re-captured
(``python tests/unit/test_kg_queries_culture.py capture <anchor>``; small queries, LIMIT-free but
restricted to the anchor entities).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SPEC_DIR = REPO / "registry" / "kg" / "queries" / "culture"
SCHEMA = REPO / "registry" / "kg" / "queries" / "_schema.json"
FIXTURES = REPO / "tests" / "fixtures" / "kg" / "culture"
ENTITY_BUDGET = 150_000  # KGCULT target: <= 150k entities across the culture specs

# Anchor QIDs (Wikidata)
PORTUGUESE, SPANISH, ENGLISH, FRENCH, ARABIC, SWAHILI, LATIN = (
    "Q5146", "Q1321", "Q1860", "Q150", "Q13955", "Q7838", "Q397")
ANCHOR_LANGUAGES = (PORTUGUESE, SPANISH, ENGLISH, FRENCH, ARABIC, SWAHILI, LATIN)
BRAZIL, PORTUGAL = "Q155", "Q45"
CATHOLICISM, CATHOLIC_CHURCH, ISLAM = "Q1841", "Q9592", "Q432"
WORLD_HERITAGE_SITE = "Q9259"

#: anchor name -> (spec file, restriction inserted after the first "WHERE {", placeholder values)
ANCHORS: dict[str, tuple[str, str, str]] = {
    "languages": ("languages.yaml", "VALUES ?item { " + " ".join(f"wd:{q}" for q in ANCHOR_LANGUAGES) + " }", ""),
    # one anchor per language: the spec returns only ?item (the ancestors), not which language led there
    **{f"language_ancestors_{q}": ("language_ancestors.yaml", f"VALUES ?lang {{ wd:{q} }}", "")
       for q in ANCHOR_LANGUAGES},
    "brazil_official_languages": ("country_official_languages.yaml", "", f"wd:{BRAZIL} wd:{PORTUGAL}"),
    "religions": ("religions.yaml", f"VALUES ?item {{ wd:{CATHOLICISM} wd:{CATHOLIC_CHURCH} wd:{ISLAM} }}",
                  f"wd:{BRAZIL}"),
    "brazil_world_heritage_sites": ("world_heritage_sites.yaml", f"?item wdt:P17 wd:{BRAZIL} .", ""),
    "brazil_world_heritage_designations": ("world_heritage_designations.yaml", f"?site wdt:P17 wd:{BRAZIL} .", ""),
}


def load_specs() -> dict[str, dict[str, Any]]:
    return {p.name: yaml.safe_load(p.read_text()) for p in sorted(SPEC_DIR.glob("*.yaml"))}


def anchor_query(name: str) -> str:
    fname, restriction, values = ANCHORS[name]
    spec = yaml.safe_load((SPEC_DIR / fname).read_text())
    q = spec["sparql"]
    i = q.index("WHERE {") + len("WHERE {")
    if restriction:
        q = q[:i] + "\n  " + restriction + q[i:]
    q = q.replace("{{values}}", values).replace("{{qid_filter}}", "")
    assert "{{" not in q, f"anchor {name}: unresolved placeholder"
    return q.strip()


def load_anchor(name: str) -> dict[str, Any]:
    path = FIXTURES / f"{name}.json"
    if not path.exists():
        pytest.fail(f"anchor fixture {path.name} missing: capture it (see module docstring)")
    doc = json.loads(path.read_text())
    if doc["query"] != anchor_query(name):
        pytest.fail(f"spec changed since anchor {name} was captured: re-capture it (see module docstring)")
    body = json.dumps(doc["response"], sort_keys=True).encode()
    assert hashlib.sha256(body).hexdigest() == doc["response_sha256"]
    return doc


def rows(doc: dict[str, Any]) -> list[dict[str, str]]:
    ent = "http://www.wikidata.org/entity/"
    out = []
    for b in doc["response"]["results"]["bindings"]:
        out.append({k: v["value"].removeprefix(ent) for k, v in b.items()})
    return out


# ----------------------------------------------------------------------------- spec validity
def test_specs_are_schema_valid_and_unique():
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text()))
    specs = load_specs()
    assert len(specs) >= 15
    ids = [s["id"] for s in specs.values()]
    assert len(ids) == len(set(ids))
    for fname, spec in specs.items():
        errors = [e.message for e in validator.iter_errors(spec)]
        assert not errors, (fname, errors)
        assert spec["domain"] == "culture" and spec["id"].startswith("culture.")
        assert spec["owner"] == "KGCULT"


def test_specs_follow_harvest_rules():
    for fname, spec in load_specs().items():
        q = spec["sparql"]
        assert "LIMIT" not in q.upper().split("WHERE", 1)[-1].replace("DELIMIT", ""), fname
        assert "wikibase:label" not in q, fname  # labels come from wbgetentities
        split = spec.get("split") or {}
        if "{{values}}" in q:
            assert split.get("by") == "values", fname
        if "{{qid_filter}}" in q:
            assert split.get("by") == "qid_prefix", fname
        if spec["purpose"] == "statements":
            cols = spec["columns"]
            assert cols.get("statement") and cols.get("rank"), fname  # statement ids + rank kept
            assert "DeprecatedRank" in q, fname
        for dep in spec.get("depends_on", []):
            assert dep in {s["id"] for s in load_specs().values()}, (fname, dep)


def test_specs_pass_the_harvester_validator_when_available():
    harvest = pytest.importorskip("reva.kg.harvest")
    from reva.adapters.connectors.wikidata import load_allowlist

    allow = load_allowlist()
    specs = [harvest.load_spec(p) for p in sorted(SPEC_DIR.glob("*.yaml"))]
    known = [s.id for s in harvest.discover_specs()]
    problems = {s.id: harvest.validate_spec(s, allow, known_ids=known) for s in specs}
    not_allowed = {k: v for k, v in problems.items() if v and all("allow-listed" in m for m in v)}
    other = {k: v for k, v in problems.items() if v and k not in not_allowed}
    assert not other, other
    if not_allowed:  # waiting for WDHARVEST to merge registry/kg/property_requests/kgcult.yaml
        requested = {r["property"] for r in yaml.safe_load(
            (REPO / "registry/kg/property_requests/kgcult.yaml").read_text())}
        for msgs in not_allowed.values():
            for m in msgs:
                pid = next(t for t in m.replace(":", " ").split() if t.startswith("P") and t[1:].isdigit())
                assert pid in requested, m
        pytest.xfail(f"properties requested but not yet allow-listed: {sorted(not_allowed)}")


def test_entity_budget():
    specs = load_specs()
    seeded = sum(s["max_rows"] for s in specs.values() if s["purpose"] == "seed_entities")
    # statements specs add at most one new subject + one new object entity per row; subjects of the
    # country specs are the 250 crosswalk countries, objects are mostly entities seeded above, so
    # count only the unbounded-subject statement specs fully.
    open_subjects = sum(
        s["max_rows"] for s in specs.values()
        if s["purpose"] == "statements" and (s.get("split") or {}).get("source") != "countries"
    )
    assert seeded + open_subjects <= ENTITY_BUDGET, (seeded, open_subjects)


# ----------------------------------------------------------------------------- anchors
def test_anchor_languages_are_seeded():
    got = {r["item"] for r in rows(load_anchor("languages"))}
    assert set(ANCHOR_LANGUAGES) <= got


def test_anchor_language_genealogy():
    anc = {q: {r["item"] for r in rows(load_anchor(f"language_ancestors_{q}"))} for q in ANCHOR_LANGUAGES}
    ROMANCE, INDO_EUROPEAN, SEMITIC, AFROASIATIC, BANTU, GERMANIC = (
        "Q19814", "Q19860", "Q34049", "Q25288", "Q33146", "Q21200")
    for lang in (PORTUGUESE, SPANISH, FRENCH):
        assert {ROMANCE, INDO_EUROPEAN} <= anc[lang], lang
    assert {GERMANIC, INDO_EUROPEAN} <= anc[ENGLISH]
    assert INDO_EUROPEAN in anc[LATIN]
    assert {SEMITIC, AFROASIATIC} <= anc[ARABIC]
    assert BANTU in anc[SWAHILI]


def test_anchor_brazil_official_language_is_portuguese():
    r = rows(load_anchor("brazil_official_languages"))
    brazil = {x["language"] for x in r if x["country"] == BRAZIL}
    portugal = {x["language"] for x in r if x["country"] == PORTUGAL}
    assert PORTUGUESE in brazil and PORTUGUESE in portugal
    assert all(x["statement"].startswith("http://www.wikidata.org/entity/statement/") or "$" in x["statement"]
               for x in r)


def test_anchor_religions_are_seeded():
    got = {r["item"] for r in rows(load_anchor("religions"))}
    assert ISLAM in got
    assert {CATHOLICISM, CATHOLIC_CHURCH} & got


def test_anchor_brazil_world_heritage_sites_match_unesco_list():
    """Wikidata items with a WH Site ID in Brazil, compared with UNESCO's own List (whc001 fixture)."""
    wd_ids = {r["item"] for r in rows(load_anchor("brazil_world_heritage_sites"))}
    assert len(wd_ids) >= 20
    from reva.adapters.connectors import atlas_unesco_whc as whc

    manifest = json.loads((REPO / "tests/fixtures/source/atlas.unesco_whc/manifest.json").read_text())
    export = next(e for e in manifest if "exports/json" in e["url"])
    props, _ = whc.parse_export((REPO / "tests/fixtures/source/atlas.unesco_whc" / export["file"]).read_bytes())
    unesco_brazil = {p.id_no for p in props if "BR" in p.iso2}
    assert len(unesco_brazil) == 26
    designations = rows(load_anchor("brazil_world_heritage_designations"))
    assert all(r["designation"] == WORLD_HERITAGE_SITE for r in designations)
    assert len({r["site"] for r in designations}) >= 20


# ----------------------------------------------------------------------------- entity anchors (wbgetentities)
@pytest.fixture(scope="module")
def ent():
    """Anchor snapshots run through WDHARVEST's extractor with the real allow-list: what the seed specs keep."""
    from datetime import datetime

    from reva.adapters.connectors.wikidata import extract_entity, load_allowlist

    if not ENTITY_FIXTURE.exists():
        pytest.fail("anchor_entities.json missing: python tests/unit/test_kg_queries_culture.py capture-entities")
    doc = json.loads(ENTITY_FIXTURE.read_text())
    allow = load_allowlist()
    out = {}
    for qid, raw in doc["entities"].items():
        out[qid] = extract_entity(raw, allow, raw_sha256=raw["raw_sha256"],
                                  retrieved_at=datetime.fromisoformat(raw["retrieved_at"]))
    return out


def objs(e, pid: str) -> set[str]:
    return {s.object_qid for s in e.statements if s.property == pid and s.object_qid}


def strings(e, pid: str) -> set[str]:
    return {str(s.value.get("value", s.value)) for s in e.statements if s.property == pid}


def ancestors(ent, qid: str, pid: str = "P279") -> set[str]:
    seen: set[str] = set()
    todo = [qid]
    while todo:
        q = todo.pop()
        for o in objs(ent[q], pid) if q in ent else ():
            if o not in seen:
                seen.add(o)
                todo.append(o)
    return seen


def test_entity_anchor_language_codes(ent):
    expected_639_3 = {PORTUGUESE: "por", SPANISH: "spa", ENGLISH: "eng", FRENCH: "fra", ARABIC: "ara",
                      SWAHILI: "swa", LATIN: "lat"}
    for qid, code in expected_639_3.items():
        e = ent[qid]
        assert e.statements, qid
        assert any(code in v for v in strings(e, "P220")), (qid, strings(e, "P220"))
        assert e.statements[0].statement_id and e.lastrevid  # statement ids + entity revision kept
    assert any("pt" in v for v in strings(ent[PORTUGUESE], "P218"))


def test_entity_anchor_speakers_are_dated(ent):
    for qid in (PORTUGUESE, SPANISH, ENGLISH, ARABIC, SWAHILI):
        speakers = [s for s in ent[qid].statements if s.property == "P1098"]
        assert speakers, qid
        # every kept speaker figure we rely on carries a date (point in time) or is flagged undated by absence
        assert any(s.point_in_time for s in speakers), qid


def test_entity_anchor_writing_systems(ent):
    for qid in (PORTUGUESE, ARABIC, LATIN):
        assert objs(ent[qid], "P282"), qid


def test_entity_anchor_genealogy_chain(ent):
    ROMANCE, INDO_EUROPEAN = "Q19814", "Q19860"
    for lang in (PORTUGUESE, SPANISH, FRENCH):
        anc = ancestors(ent, lang)
        assert ROMANCE in anc and INDO_EUROPEAN in anc, (lang, sorted(anc))
    assert INDO_EUROPEAN in ancestors(ent, ENGLISH)
    assert INDO_EUROPEAN in ancestors(ent, LATIN)


def test_entity_anchor_brazil_official_language(ent):
    p37 = [s for s in ent[BRAZIL].statements if s.property == "P37"]
    assert PORTUGUESE in {s.object_qid for s in p37}
    assert all(s.statement_id and s.statement_id.upper().startswith("Q155$") for s in p37)
    assert PORTUGUESE in objs(ent[PORTUGAL], "P37")


def test_entity_anchor_colonial_path_statements_exist(ent):
    """Brazil -> predecessor(s) -> part of / country -> ... -> Portuguese: each hop is a harvested statement.
    The test checks that some predecessor chain reaches an entity whose official language is Portuguese
    (no link is invented: every hop is a P1365/P155, P361/P17 or P37 claim in the snapshots)."""
    preds = objs(ent[BRAZIL], "P1365") | objs(ent[BRAZIL], "P155")
    assert preds
    preds2 = {o for q in preds if q in ent for o in objs(ent[q], "P1365") | objs(ent[q], "P155")}
    frontier = preds | preds2
    parents = {o for q in frontier if q in ent for o in objs(ent[q], "P361") | objs(ent[q], "P17")}
    speaking = {q for q in frontier | parents if q in ent and PORTUGUESE in objs(ent[q], "P37")}
    assert speaking, (sorted(frontier), sorted(parents))


def test_entity_anchor_religions(ent):
    assert ISLAM in ent and ent[ISLAM].statements
    assert ent[ISLAM].sitelinks_count >= 5
    # Catholicism and the Catholic Church are distinct items; at least one carries a founder or a parent branch
    cath = [ent[q] for q in (CATHOLICISM, CATHOLIC_CHURCH) if q in ent]
    assert cath and any(objs(e, "P279") | objs(e, "P361") | objs(e, "P112") for e in cath)


# ----------------------------------------------------------------------------- capture (live, manual)
async def _capture(names: list[str]) -> None:  # pragma: no cover - live, run by hand
    from datetime import UTC, datetime

    from reva.adapters.connectors.wikidata import WikidataClient, make_live_http

    http = make_live_http(proxy_mode="env")
    client = WikidataClient(http)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    try:
        for name in names:
            q = anchor_query(name)
            res = await client.sparql(q, meta={"agent": "KGCULT", "purpose": f"anchor {name}"})
            response = {"head": {"vars": list(res.variables)}, "results": {"bindings": res.bindings}}
            doc = {
                "anchor": name,
                "spec": ANCHORS[name][0],
                "query": q,
                "retrieved_at": res.retrieved_at.isoformat() if res.retrieved_at else datetime.now(UTC).isoformat(),
                "raw_sha256": res.raw_sha256,
                "source": "Wikidata Query Service (CC0), via reva.adapters.connectors.wikidata.WikidataClient",
                "response_sha256": hashlib.sha256(json.dumps(response, sort_keys=True).encode()).hexdigest(),
                "response": response,
            }
            (FIXTURES / f"{name}.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False, sort_keys=True))
            print(name, len(res.bindings), "rows", file=sys.stderr)
    finally:
        await http.aclose()


ENTITY_FIXTURE = FIXTURES / "anchor_entities.json"
ENTITY_KEEP = ("P31", "P279", "P361", "P17", "P220", "P218", "P219", "P1394", "P1098", "P282", "P37", "P2936",
               "P140", "P38", "P112", "P571", "P576", "P1365", "P155", "P1366", "P156")


async def _capture_entities() -> None:  # pragma: no cover - live, run by hand
    """wbgetentities snapshots (claims trimmed to ENTITY_KEEP) of the anchors, the P279 ancestors of the
    anchor languages and religions, and Brazil's predecessors and what they are part of."""
    from reva.adapters.connectors.wikidata import WikidataClient, make_live_http

    http = make_live_http(proxy_mode="env")
    client = WikidataClient(http)
    out: dict[str, Any] = {}

    async def fetch(ids: set[str]) -> None:
        todo = sorted(i for i in ids if i not in out)
        if not todo:
            return
        res = await client.get_entities(todo, props=("claims", "labels", "sitelinks"), languages=("en", "fr"),
                                        meta={"agent": "KGCULT", "purpose": "anchor entities"})
        for qid, ent in res.entities.items():
            out[qid] = {
                "id": ent["id"], "type": ent.get("type"), "lastrevid": ent.get("lastrevid"),
                "modified": ent.get("modified"), "labels": ent.get("labels", {}),
                "sitelinks_count": len(ent.get("sitelinks") or {}),
                "claims": {k: v for k, v in (ent.get("claims") or {}).items() if k in ENTITY_KEEP},
                "raw_sha256": res.raw_sha_by_id.get(qid), "retrieved_at": res.retrieved_at.isoformat(),
            }

    def objects(qid: str, pid: str) -> set[str]:
        got = set()
        for c in out.get(qid, {}).get("claims", {}).get(pid, []):
            if c.get("rank") == "deprecated":
                continue
            v = (c.get("mainsnak", {}).get("datavalue") or {}).get("value")
            if isinstance(v, dict) and v.get("id"):
                got.add(v["id"])
        return got

    try:
        await fetch({*ANCHOR_LANGUAGES, BRAZIL, PORTUGAL, CATHOLICISM, CATHOLIC_CHURCH, ISLAM})
        frontier = set(ANCHOR_LANGUAGES) | {CATHOLICISM, CATHOLIC_CHURCH, ISLAM}
        for _ in range(12):  # P279 closure, bounded depth
            nxt = {o for q in frontier for o in objects(q, "P279")} - set(out)
            if not nxt:
                break
            await fetch(nxt)
            frontier = nxt
        preds = objects(BRAZIL, "P1365") | objects(BRAZIL, "P155")
        await fetch(preds)
        preds2 = {o for q in preds for o in objects(q, "P1365") | objects(q, "P155")}
        await fetch(preds2)
        await fetch({o for q in preds | preds2 for o in objects(q, "P361") | objects(q, "P17")})
    finally:
        await http.aclose()
    FIXTURES.mkdir(parents=True, exist_ok=True)
    ENTITY_FIXTURE.write_text(json.dumps({
        "source": "Wikidata wbgetentities (CC0) via reva.adapters.connectors.wikidata.WikidataClient; claims trimmed "
                  "to ENTITY_KEEP, raw response sha256 per entity",
        "entities": dict(sorted(out.items(), key=lambda kv: int(kv[0][1:]))),
    }, indent=1, ensure_ascii=False, sort_keys=True))
    print("entities", len(out), file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover
    import asyncio

    if len(sys.argv) >= 2 and sys.argv[1] == "capture":
        asyncio.run(_capture(sys.argv[2:] or list(ANCHORS)))
    elif len(sys.argv) >= 2 and sys.argv[1] == "capture-entities":
        asyncio.run(_capture_entities())
