"""KGSCIECO: science / economy / nature query specs (registry/kg/queries/{science,economy,nature}).

Offline. Checks that

* every spec validates with WDHARVEST's validator (``reva.kg.harvest.validate_spec``: JSON schema, allow-list,
  placeholders, no LIMIT / label service) and is bounded (``max_rows``, timeout <= 60 s), and that the specs
  together stay within the KGSCIECO entity budget;
* nature specs live in ``nature/<topic>/`` with domain ``geography`` and ids ``geography.nature.*`` (the schema's
  domain enum has no ``nature``; the harvester checks domain == directory only one level below the root);
* every QID and PID written in a KGSCIECO spec (classes, anchor items, VALUES lists) or requested in
  ``registry/kg/property_requests/kgscieco.yaml`` was confirmed by the recorded label check
  ``tests/fixtures/kg/scieco/qid_labels.json`` -- one real WDQS response, captured through WDHARVEST's budgeted,
  locked client, whose English label must equal the label expected here (no id typed from memory goes
  unverified). Re-capture after adding ids: ``uv run python tests/unit/test_kg_queries_scieco.py capture``.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from reva.adapters.connectors.wikidata import load_allowlist
from reva.kg.harvest import SPEC_ROOT, discover_specs, load_spec, validate_all

ROOT = Path(__file__).resolve().parents[2]
DIRS = [SPEC_ROOT / "science", SPEC_ROOT / "economy", SPEC_ROOT / "nature"]
REQUESTS = ROOT / "registry" / "kg" / "property_requests" / "kgscieco.yaml"
FIXTURE = ROOT / "tests" / "fixtures" / "kg" / "scieco" / "qid_labels.json"
ENTITY_BUDGET = 250_000  # KGSCIECO target: <= 250k seeded entities (KG budget 6 GB ~ 8M statements overall)
ID_RE = re.compile(r"\bwd:(Q[1-9][0-9]*)\b")

#: every id used by a KGSCIECO spec or property request -> its English Wikidata label (checked in the fixture)
EXPECTED: dict[str, str] = json.loads((Path(__file__).with_name("kg_scieco_expected_labels.json")).read_text())


def specs() -> list[Any]:
    return discover_specs(DIRS)


def spec_ids_used() -> set[str]:
    out: set[str] = set()
    for s in specs():
        text = s.data["sparql"] + (s.data.get("sparql_undated") or "")
        out |= set(ID_RE.findall(text))
        out |= set((s.split or {}).get("qids") or [])
    return out


def requested_pids() -> set[str]:
    return {str(r["property"]) for r in yaml.safe_load(REQUESTS.read_text())}


# ----------------------------------------------------------------------------- validity and bounds
def test_specs_validate_with_harvester() -> None:
    mine = specs()
    assert len(mine) >= 30
    res = validate_all(mine, load_allowlist())
    bad = {k: v for k, v in res.items() if v}
    assert not bad, bad
    all_ids = [s.id for s in discover_specs()]
    assert len(all_ids) == len(set(all_ids)), "spec ids must be unique across every domain"


def test_layout_owner_and_rules() -> None:
    for s in specs():
        d = s.data
        rel = s.path.relative_to(SPEC_ROOT)
        top = rel.parts[0]
        assert d.get("owner") == "KGSCIECO", rel
        if top == "nature":
            assert len(rel.parts) == 3, f"{rel}: nature specs live in nature/<topic>/"
            assert d["domain"] == "geography" and d["id"].startswith("geography.nature."), rel
        else:
            assert len(rel.parts) == 2 and d["domain"] == top and d["id"].startswith(top + "."), rel
        assert d.get("timeout_seconds", 55) <= 60
        q = d["sparql"]
        assert "wikibase:label" not in q and not re.search(r"\bLIMIT\b", q, re.I), rel
        if d["purpose"] == "statements":
            cols = d["columns"]
            assert cols.get("statement") and cols.get("rank"), rel
            assert "DeprecatedRank" in q, rel


def test_entity_budget() -> None:
    seeded = sum(s.data["max_rows"] for s in specs() if s.data["purpose"] == "seed_entities")
    open_subjects = sum(s.data["max_rows"] for s in specs() if s.data["purpose"] == "statements")
    assert seeded + open_subjects <= ENTITY_BUDGET, (seeded, open_subjects)


def test_requested_properties_are_not_yet_used_as_columns() -> None:
    """Specs only name allow-listed properties; requested ones arrive through unrestricted seed snapshots."""
    allow = load_allowlist()
    for s in specs():
        cols = s.columns
        if cols.get("property"):
            assert cols["property"] in allow
        for pid in s.data.get("properties") or []:
            assert pid in allow


# ----------------------------------------------------------------------------- recorded id check
def load_fixture() -> dict[str, Any]:
    if not FIXTURE.exists():
        pytest.fail(f"{FIXTURE.name} missing: capture it (see module docstring)")
    doc: dict[str, Any] = json.loads(FIXTURE.read_text())
    body = json.dumps(doc["response"], sort_keys=True).encode()
    assert hashlib.sha256(body).hexdigest() == doc["response_sha256"]
    assert doc["query"] == label_query(sorted(doc["ids"], key=_id_key))
    return doc


def fixture_labels(doc: dict[str, Any]) -> dict[str, str]:
    ent = "http://www.wikidata.org/entity/"
    return {b["id"]["value"].removeprefix(ent): b["label"]["value"]
            for b in doc["response"]["results"]["bindings"] if "label" in b}


def test_every_id_is_expected_and_recorded() -> None:
    used = spec_ids_used() | requested_pids()
    missing = used - set(EXPECTED)
    assert not missing, f"ids used in specs/requests without an expected label: {sorted(missing)}"
    doc = load_fixture()
    got = fixture_labels(doc)
    not_recorded = used - set(got)
    assert not not_recorded, f"re-capture the label fixture: {sorted(not_recorded)}"
    wrong = {i: (EXPECTED[i], got[i]) for i in used if got[i] != EXPECTED[i]}
    assert not wrong, wrong


# ----------------------------------------------------------------------------- capture (live, by hand)
def _id_key(i: str) -> tuple[str, int]:
    return (i[0], int(i[1:]))


def label_query(ids: list[str]) -> str:
    vals = " ".join(f"wd:{i}" for i in ids)
    return ('SELECT ?id ?label WHERE {\n  VALUES ?id { ' + vals + ' }\n'
            '  OPTIONAL { ?id rdfs:label ?label . FILTER(LANG(?label) = "en") }\n}')


async def _capture() -> None:  # pragma: no cover - live, run by hand
    from reva.kg.harvest import budgeted_client

    ids = sorted(spec_ids_used() | requested_pids() | set(EXPECTED), key=_id_key)
    q = label_query(ids)
    async with budgeted_client(purpose="scieco_labels", planned_wdqs=1, planned_wbapi=0) as sess:
        res = await sess.client.sparql(q, meta={"agent": "KGSCIECO", "purpose": "id label check"})
    response = {"head": {"vars": list(res.variables)}, "results": {"bindings": res.bindings}}
    doc = {
        "ids": ids,
        "query": q,
        "query_sha256": res.query_sha256,
        "raw_sha256": res.raw_sha256,
        "retrieved_at": res.retrieved_at.isoformat(),
        "source": "Wikidata Query Service (CC0), via reva.kg.harvest.budgeted_client (WDHARVEST)",
        "response_sha256": hashlib.sha256(json.dumps(response, sort_keys=True).encode()).hexdigest(),
        "response": response,
    }
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(doc, indent=1, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"{len(ids)} ids, {len(res.bindings)} rows", file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover
    import asyncio

    if sys.argv[1:] == ["capture"]:
        asyncio.run(_capture())
    else:
        print(__doc__)
