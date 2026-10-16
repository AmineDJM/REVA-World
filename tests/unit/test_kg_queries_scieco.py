"""KGSCIECO: science / economy / nature query specs (registry/kg/queries/{science,economy,nature}).

Offline. Checks that

* every spec validates with WDHARVEST's validator (``reva.kg.harvest.validate_spec``: JSON schema, allow-list,
  placeholders, no LIMIT / label service) and is bounded (``max_rows``, timeout <= 60 s), and that the specs
  together stay within the KGSCIECO entity budget;
* nature specs live in ``nature/<topic>/`` with domain ``geography`` and ids ``geography.nature.*`` (the schema's
  domain enum has no ``nature``; the harvester checks domain == directory only one level below the root);
* every QID and PID written in a KGSCIECO spec (classes, anchor items, VALUES lists) or requested in
  ``registry/kg/property_requests/kgscieco.yaml`` was confirmed by the recorded label check
  ``tests/fixtures/kg/scieco/qid_labels.json`` -- labels read from real wbgetentities responses fetched through
  WDHARVEST's budgeted, locked client (bodies archived content-addressed, sha256 kept and re-checked when the
  archive is present); the English label must equal the label expected here (no id typed from memory goes
  unverified). Re-capture after adding ids: ``uv run python tests/unit/test_kg_queries_scieco.py capture``.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from reva.adapters.connectors.wikidata import load_allowlist
from reva.kg.harvest import SPEC_ROOT, discover_specs, validate_all

ROOT = Path(__file__).resolve().parents[2]
DIRS = [SPEC_ROOT / "science", SPEC_ROOT / "economy", SPEC_ROOT / "nature"]
REQUESTS = ROOT / "registry" / "kg" / "property_requests" / "kgscieco.yaml"
FIXTURE = ROOT / "tests" / "fixtures" / "kg" / "scieco" / "qid_labels.json"
ENTITY_BUDGET = 250_000  # KGSCIECO target: <= 250k seeded entities (KG budget 6 GB ~ 8M statements overall)
ID_RE = re.compile(r"\bwd:(Q[1-9][0-9]*)\b")

#: every id used by a KGSCIECO spec or property request -> its English Wikidata label (checked in the fixture)
EXPECTED: dict[str, str] = {
    "P138": "named after", "P176": "manufacturer", "P178": "developer", "P205": "basin country",
    "P403": "mouth of the watercourse", "P414": "stock exchange", "P562": "central bank/issuer",
    "P610": "highest point", "P619": "UTC date of spacecraft launch", "P814": "IUCN protected areas category",
    "P885": "origin of the watercourse", "P974": "tributary", "P1027": "conferred by",
    "P1056": "product or material produced", "P1304": "central bank", "P1589": "lowest point",
    "P1603": "number of cases", "P1686": "for work", "P1924": "vaccine for", "P2043": "length",
    "P2044": "elevation above sea level", "P2527": "earthquake magnitude on the moment magnitude scale",
    "P2528": "earthquake magnitude on the Richter magnitude scale", "P2541": "operating area",
    "P4552": "mountain range", "P4614": "drainage basin", "Q5": "human", "Q165": "sea", "Q3918": "university",
    "Q4022": "river", "Q4576": "Marshall Plan", "Q7944": "earthquake", "Q8065": "natural disaster",
    "Q8068": "flood", "Q8070": "tsunami", "Q8072": "volcano", "Q8092": "tropical cyclone", "Q8502": "mountain",
    "Q8514": "desert", "Q9430": "ocean", "Q11344": "chemical element", "Q11345": "equation",
    "Q11691": "stock exchange", "Q12184": "pandemic", "Q23397": "lake", "Q26529": "space probe",
    "Q26540": "artificial satellite", "Q31855": "research institute", "Q35637": "Nobel Peace Prize",
    "Q37901": "strait", "Q37922": "Nobel Prize in Literature", "Q38104": "Nobel Prize in Physics", "Q39594": "bay",
    "Q42944": "CERN", "Q44512": "epidemic", "Q44585": "Nobel Prize in Chemistry", "Q46169": "national park",
    "Q46831": "mountain range", "Q47170": "Prize in Economic Sciences in Memory of Alfred Nobel",
    "Q47574": "unit of measurement", "Q62832": "observatory", "Q65943": "theorem", "Q66344": "central bank",
    "Q76039": "Hudson's Bay Company", "Q80061": "Nobel Prize in Physiology or Medicine",
    "Q83164": "East India Company", "Q114380": "financial crisis", "Q131681": "reservoir", "Q134808": "vaccine",
    "Q148578": "space telescope", "Q158085": "Max Planck Society", "Q159766": "Dutch East India Company",
    "Q162157": "chartered company", "Q168247": "famine", "Q169358": "stratovolcano",
    "Q173227": "physical constant", "Q176494": "recession", "Q185565": "hyperinflation", "Q188025": "salt lake",
    "Q188532": "Bretton Woods system", "Q204894": "marginal sea", "Q207781": "economic bubble",
    "Q208469": "SI derived unit", "Q214070": "physical law", "Q218819": "customs union", "Q223662": "SI base unit",
    "Q290178": "economic crisis", "Q319141": "conjecture", "Q379231": "gas law", "Q391083": "Pasteur Institute",
    "Q408891": "scientific law", "Q414147": "academy of sciences", "Q617066": "Dutch West India Company",
    "Q752783": "human spaceflight", "Q769519": "Royal African Company", "Q875538": "public university",
    "Q902104": "private university", "Q926921": "Imperial British East Africa Company",
    "Q1020018": "stock market crash", "Q1061151": "massif", "Q1125321": "currency union", "Q1129645": "trade bloc",
    "Q1152507": "Virginia Company", "Q1254933": "astronomical observatory", "Q1293220": "physical phenomenon",
    "Q1322134": "gulf", "Q1371037": "university of technology", "Q1471432": "space laboratory",
    "Q1700154": "colonial society", "Q1765656": "Muscovy Company", "Q1814941": "depression",
    "Q1973404": "adjacent sea", "Q2133344": "space mission", "Q2465832": "branch of science",
    "Q2865305": "trading company", "Q3239681": "scientific theory", "Q3241045": "disease outbreak",
    "Q3354859": "collegiate university", "Q3536928": "free trade agreement", "Q3557480": "Levant Company",
    "Q5335686": "free trade area", "Q7692360": "volcanic eruption", "Q9019918": "endorheic lake",
    "Q11448906": "science award", "Q11862829": "academic discipline",
    "Q12832168": "artificial satellite of the Earth", "Q15936437": "research university",
    "Q17505024": "space agency", "Q18819669": "flyby probe", "Q26236686": "East India company",
    "Q105967696": "vaccine type", "Q123193388": "oil crisis",
}


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
    assert len(mine) >= 29
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
def _id_key(i: str) -> tuple[str, int]:
    return (i[0], int(i[1:]))


def load_fixture() -> dict[str, Any]:
    if not FIXTURE.exists():
        pytest.fail(f"{FIXTURE.name} missing: capture it (see module docstring)")
    doc: dict[str, Any] = json.loads(FIXTURE.read_text())
    body = json.dumps(doc["labels"], sort_keys=True, ensure_ascii=False).encode()
    assert hashlib.sha256(body).hexdigest() == doc["labels_sha256"]
    assert doc["raw_sha256"] and all(re.fullmatch(r"[0-9a-f]{64}", h) for h in doc["raw_sha256"])
    return doc


def test_fixture_matches_archived_raw_responses() -> None:
    """When the raw archive is present (var/kg/objects, not versioned), the labels are re-read from it."""
    doc = load_fixture()
    base = ROOT / "var" / "kg" / "objects" / "reva" / "raw" / "sha256"
    paths = [base / h[:2] / h[2:4] / h for h in doc["raw_sha256"]]
    if not all(p.exists() for p in paths):
        pytest.skip("raw archive not present on this machine")
    seen: dict[str, str | None] = {}
    for h, p in zip(doc["raw_sha256"], paths, strict=True):
        stored = p.read_bytes()
        body = gzip.decompress(stored) if stored[:2] == b"\x1f\x8b" else stored  # archive may keep gzip bytes
        assert h in {hashlib.sha256(stored).hexdigest(), hashlib.sha256(body).hexdigest()}
        for q, e in json.loads(body)["entities"].items():
            if "missing" not in e:
                seen[q] = e.get("labels", {}).get("en", {}).get("value")
    assert {i: seen.get(i) for i in doc["labels"]} == doc["labels"]


def test_every_id_is_expected_and_recorded() -> None:
    used = spec_ids_used() | requested_pids()
    missing = used - set(EXPECTED)
    assert not missing, f"ids used in specs/requests without an expected label: {sorted(missing)}"
    got: dict[str, str | None] = load_fixture()["labels"]
    not_recorded = used - set(got)
    assert not not_recorded, f"re-capture the label fixture: {sorted(not_recorded)}"
    wrong = {i: (EXPECTED[i], got[i]) for i in used if got[i] != EXPECTED[i]}
    assert not wrong, wrong


def test_requested_property_labels_match_wikidata() -> None:
    got = load_fixture()["labels"]
    for r in yaml.safe_load(REQUESTS.read_text()):
        assert r["label_en"] == got[str(r["property"])], r["property"]


# ----------------------------------------------------------------------------- capture (live, by hand)
async def _capture() -> None:  # pragma: no cover - live, run by hand
    from reva.kg.harvest import budgeted_client

    ids = sorted(spec_ids_used() | requested_pids() | set(EXPECTED), key=_id_key)
    labels: dict[str, str | None] = {}
    raws: list[str] = []
    async with budgeted_client(purpose="scieco_labels", planned_wdqs=0,
                               planned_wbapi=(len(ids) + 49) // 50) as sess:
        c = sess.client
        for i in range(0, len(ids), 50):
            part = ids[i:i + 50]
            url = ("https://www.wikidata.org/w/api.php?action=wbgetentities&ids=" + "|".join(part)
                   + "&props=labels&languages=en&format=json&maxlag=5")
            res, sha = await c._request(c.wbapi_http, url, endpoint="wbapi", accept="application/json",
                                        meta={"agent": "KGSCIECO", "purpose": "id label check"})
            raws.append(sha)
            retrieved_at = res.retrieved_at.isoformat()
            for q, e in json.loads(res.body)["entities"].items():
                labels[q] = None if "missing" in e else e.get("labels", {}).get("en", {}).get("value")
    doc = {
        "ids": ids,
        "retrieved_at": retrieved_at,
        "source": "Wikidata wbgetentities props=labels (CC0), via reva.kg.harvest.budgeted_client (WDHARVEST)",
        "raw_sha256": raws,
        "labels_sha256": hashlib.sha256(json.dumps(labels, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        "labels": labels,
    }
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(doc, indent=1, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"{len(ids)} ids, {sum(v is not None for v in labels.values())} labelled", file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover
    import asyncio

    if sys.argv[1:] == ["capture"]:
        asyncio.run(_capture())
    else:
        print(__doc__)
