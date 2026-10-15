"""Offline tests for the Wikidata client and the KG harvest orchestrator (agent WDHARVEST).

Recorded responses: ``tests/fixtures/source/wikidata/`` (small real WDQS / wbgetentities captures, see its
manifest.json). Synthetic responses are used for protocol edge cases (429, maxlag, time-outs).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from reva.adapters.connectors import wikidata as wd
from reva.adapters.http.safe_client import HttpResult
from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.kg import harvest as hv

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "source" / "wikidata"
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- helpers
class FakeClock:
    def __init__(self) -> None:
        self.t = 1_000_000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


class ScriptedHttp:
    """Serves queued (status, body, headers) per endpoint host; records requests."""

    origin = "test_fixture"

    def __init__(self, script: dict[str, list[tuple[int, bytes, dict[str, str]]]] | None = None,
                 router: Any = None) -> None:
        self.script = script or {}
        self.router = router
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get(self, url: str, **kw: Any) -> HttpResult:
        self.calls.append((url, kw))
        if self.router is not None:
            status, body, headers = self.router(url)
        else:
            host = "wdqs" if url.startswith(wd.WDQS_URL) else "wbapi"
            status, body, headers = self.script[host].pop(0)
        return HttpResult(url, url, status, {"content-type": "application/json", **headers}, body, NOW, 0.01, (), 1)

    async def aclose(self) -> None:
        return None


def sparql_body(vars_: list[str], rows: list[dict[str, dict[str, str]]]) -> bytes:
    return json.dumps({"head": {"vars": vars_}, "results": {"bindings": rows}}).encode()


def uri(q: str) -> dict[str, str]:
    return {"type": "uri", "value": wd.ENTITY_PREFIX + q}


def client_for(http: Any, tmp_path: Path, clock: FakeClock | None = None) -> wd.WikidataClient:
    clock = clock or FakeClock()
    lock = wd.MachineLock(tmp_path / "locks", clock=clock, sleep=clock.sleep)
    store = FilesystemArtifactStore(tmp_path / "objects")
    return wd.WikidataClient(http, archive=wd.Archive(store, tmp_path / "index.jsonl"), lock=lock,
                             sleep=clock.sleep)


def snak(pid: str, value: Any, kind: str = "item") -> dict[str, Any]:
    if kind == "item":
        dv = {"type": "wikibase-entityid", "value": {"entity-type": "item", "id": value}}
        dt = "wikibase-item"
    elif kind == "time":
        dv = {"type": "time", "value": value}
        dt = "time"
    elif kind == "quantity":
        dv = {"type": "quantity", "value": value}
        dt = "quantity"
    else:
        dv = {"type": "string", "value": value}
        dt = kind
    return {"snaktype": "value", "property": pid, "datavalue": dv, "datatype": dt}


def entity_json() -> dict[str, Any]:
    t = {"time": "-0044-03-15T00:00:00Z", "precision": 11, "calendarmodel": wd.ENTITY_PREFIX + "Q1985786",
         "timezone": 0, "before": 0, "after": 0}
    return {
        "id": "Q17167", "type": "item", "lastrevid": 2_000_000_001, "modified": "2026-10-01T00:00:00Z",
        "labels": {"en": {"language": "en", "value": "Roman Republic"},
                   "fr": {"language": "fr", "value": "République romaine"}},
        "descriptions": {"en": {"language": "en", "value": "period of ancient Roman civilization"}},
        "aliases": {"en": [{"language": "en", "value": "Res publica Romana"}]},
        "sitelinks": {"enwiki": {"site": "enwiki", "title": "Roman Republic"},
                      "frwiki": {"site": "frwiki", "title": "République romaine"},
                      "lawiki": {"site": "lawiki", "title": "Res publica Romana"}},
        "claims": {
            "P31": [{"id": "Q17167$A", "rank": "normal", "mainsnak": snak("P31", "Q3024240")},
                    {"id": "Q17167$B", "rank": "deprecated", "mainsnak": snak("P31", "Q6256")}],
            "P576": [{"id": "Q17167$C", "rank": "preferred", "mainsnak": snak("P576", t, "time"),
                      "references": [{"snaks": {"P248": [snak("P248", "Q123")],
                                                "P854": [snak("P854", "https://example.org/x", "url")],
                                                "P9999": [snak("P9999", "dropped", "string")]}}]}],
            "P36": [{"id": "Q17167$D", "rank": "normal", "mainsnak": snak("P36", "Q220"),
                     "qualifiers": {"P580": [snak("P580", {**t, "time": "-0509-00-00T00:00:00Z",
                                                           "precision": 9}, "time")],
                                    "P1480": [snak("P1480", "Q5727902")],
                                    "P9998": [snak("P9998", "Q1")]}}],
            "P1082": [{"id": "Q17167$E", "rank": "normal",
                       "mainsnak": snak("P1082", {"amount": "+4063000", "unit": "1"}, "quantity"),
                       "qualifiers": {"P585": [{"snaktype": "somevalue", "property": "P585"}]}}],
            "P18": [{"id": "Q17167$F", "rank": "normal", "mainsnak": snak("P18", "x.jpg", "commonsMedia")}],
            "P47": [{"id": "Q17167$G", "rank": "normal", "mainsnak": {"snaktype": "novalue", "property": "P47"}}],
        },
    }


# --------------------------------------------------------------------------- lock and pacing
async def test_lock_paces_requests_and_shares_cooldowns_per_endpoint(tmp_path: Path) -> None:
    clock = FakeClock()
    lock = wd.MachineLock(tmp_path, min_interval=1.0, clock=clock, sleep=clock.sleep)
    async with lock.slot("wdqs"):
        pass
    async with lock.slot("wdqs") as s:  # immediately after: waits the pacing interval
        s.cooldown(120)
    assert clock.sleeps and abs(clock.sleeps[0] - 1.0) < 1e-6
    state = json.loads((tmp_path / "wikidata.state.json").read_text())
    assert state["cooldown_until:wdqs"] >= clock.t + 119
    clock.sleeps.clear()
    other = wd.MachineLock(tmp_path, min_interval=1.0, clock=clock, sleep=clock.sleep)  # "another process"
    async with other.slot("wbapi"):  # API is not throttled by the WDQS cool-down
        pass
    assert sum(clock.sleeps) <= 1.0 + 1e-6
    clock.sleeps.clear()
    async with other.slot("wdqs"):  # WDQS waits the shared cool-down (in <= 60 s steps, lock released)
        pass
    assert sum(clock.sleeps) >= 118
    assert max(clock.sleeps) <= 60.0


async def test_user_agent_is_descriptive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REVA_CONTACT_EMAIL", raising=False)
    monkeypatch.delenv("REVA_HTTP_CONTACT_EMAIL", raising=False)
    ua = wd.user_agent()
    assert ua.startswith("REVA-World/") and "github.com" in ua
    monkeypatch.setenv("REVA_CONTACT_EMAIL", "ops@example.org")
    assert "mailto:ops@example.org" in wd.user_agent()


# --------------------------------------------------------------------------- client protocol
async def test_429_retry_after_is_honoured_and_archived(tmp_path: Path) -> None:
    body = sparql_body(["item"], [{"item": uri("Q1")}])
    http = ScriptedHttp({"wdqs": [(429, b"slow down", {"retry-after": "7"}), (200, body, {})]})
    clock = FakeClock()
    c = client_for(http, tmp_path, clock)
    res = await c.sparql("SELECT ?item WHERE { ?item wdt:P31 wd:Q5 }")
    assert res.column("item") == [wd.ENTITY_PREFIX + "Q1"]
    assert c.stats.throttled == 1 and c.stats.retries == 1 and c.stats.requests == 2
    assert any(s >= 7 for s in clock.sleeps)
    ua = http.calls[0][1]["headers"]["User-Agent"]
    assert ua.startswith("REVA-World/")
    index = [json.loads(x) for x in (tmp_path / "index.jsonl").read_text().splitlines()]
    kinds = [e["kind"] for e in index]
    assert kinds == ["sparql_query", "sparql_results"]  # the 429 body is not archived
    import gzip

    store = FilesystemArtifactStore(tmp_path / "objects")
    assert gzip.decompress(store.get("raw", res.raw_sha256)) == body  # deterministic gzip of the provider bytes
    assert index[1]["body_sha256"] == hashlib.sha256(body).hexdigest()
    q = next(e for e in index if e["kind"] == "sparql_query")
    assert gzip.decompress(store.get("raw", q["sha256"])).decode().startswith("SELECT ?item")


async def test_long_retry_after_is_honoured_in_full_with_margin(tmp_path: Path) -> None:
    """WDQS answers 429 with Retry-After=1000: the shared cool-down must cover all of it (+ margin), never a
    capped value that releases the next request early and renews the ban."""
    assert wd._retry_after_seconds("1000") >= 1030
    body = sparql_body(["item"], [{"item": uri("Q1")}])
    http = ScriptedHttp({"wdqs": [(429, b"Too Many Requests", {"retry-after": "1000"}), (200, body, {})]})
    clock = FakeClock()
    c = client_for(http, tmp_path, clock)
    t0 = clock.t
    await c.sparql("SELECT ?item WHERE { ?item wdt:P31 wd:Q5 }")
    state = json.loads((tmp_path / "locks" / "wikidata.state.json").read_text())
    assert sum(clock.sleeps) >= 1030 and clock.t - t0 >= 1030
    assert all(s <= 60.0 for s in clock.sleeps)  # waited in steps, lock released between them
    assert "cooldown_until:wbapi" not in state  # the API is not throttled by a WDQS cool-down


async def test_wdqs_timeout_is_typed_and_not_retried(tmp_path: Path) -> None:
    http = ScriptedHttp({"wdqs": [(500, b"java.util.concurrent.TimeoutException\n\tat ...", {})]})
    c = client_for(http, tmp_path)
    with pytest.raises(wd.QueryTimeout):
        await c.sparql("SELECT ?item WHERE { ?item wdt:P31/wdt:P279* wd:Q5 }")
    assert len(http.calls) == 1


async def test_bad_query_is_typed(tmp_path: Path) -> None:
    http = ScriptedHttp({"wdqs": [(400, b"MalformedQueryException: Encountered \" \"}\"", {})]})
    with pytest.raises(wd.SparqlSyntaxError):
        await client_for(http, tmp_path).sparql("SELECT ?x WHERE { ")


async def test_maxlag_is_backed_off_and_entities_parsed(tmp_path: Path) -> None:
    maxlag = json.dumps({"error": {"code": "maxlag", "info": "Waiting for a database server: 6 seconds lagged"}})
    ok = json.dumps({"entities": {"Q17167": entity_json(), "Q404": {"id": "Q404", "missing": ""}},
                     "success": 1})
    http = ScriptedHttp({"wbapi": [(200, maxlag.encode(), {"retry-after": "5"}), (200, ok.encode(), {})]})
    c = client_for(http, tmp_path)
    res = await c.get_entities(["Q404", "Q17167"])
    assert set(res.entities) == {"Q17167"} and res.missing == ("Q404",)
    assert c.stats.throttled == 1 and res.requests == 1
    url = http.calls[-1][0]
    assert "ids=Q404|Q17167" in url and "languages=fr|en|es|pt|ar|zh|ru|de" in url and "maxlag=5" in url
    assert "props=info|claims|labels|descriptions|aliases|sitelinks" in url  # info -> lastrevid


async def test_entities_are_requested_50_per_call(tmp_path: Path) -> None:
    def router(url: str) -> tuple[int, bytes, dict[str, str]]:
        ids = url.split("ids=")[1].split("&")[0].split("|")
        return 200, json.dumps({"entities": {i: {"id": i, "labels": {}} for i in ids}}).encode(), {}

    http = ScriptedHttp(router=router)
    res = await client_for(http, tmp_path).get_entities([f"Q{i}" for i in range(1, 121)], props=wd.LABEL_PROPS)
    assert res.requests == 3 and len(res.entities) == 120
    assert [len(u.split("ids=")[1].split("&")[0].split("|")) for u, _ in http.calls] == [50, 50, 20]


# --------------------------------------------------------------------------- extraction
def test_extraction_keeps_allowlisted_non_deprecated_with_compact_refs() -> None:
    allow = wd.load_allowlist()
    e = wd.extract_entity(entity_json(), allow, raw_sha256="a" * 64, retrieved_at=NOW, kind_hint="historical_state")
    by = {s.statement_id: s for s in e.statements}
    assert "Q17167$B" not in by and e.dropped["deprecated"] == 1  # deprecated rank dropped
    assert "Q17167$F" not in by and e.dropped["not_allowlisted"] == 1  # P18 not allow-listed
    assert e.instance_of == ("Q3024240",)
    dis = by["Q17167$C"]
    assert dis.rank == "preferred" and dis.value_kind == "time" and dis.source_revision == 2_000_000_001
    assert dis.references == [{"P248": ["Q123"], "P854": ["https://example.org/x"]}]  # P9999 dropped
    cap = by["Q17167$D"]
    assert cap.object_qid == "Q220" and cap.start and cap.start["precision"] == 9
    assert cap.qualifiers == {"P1480": ["Q5727902"]}  # P580 -> start, P9998 dropped
    pop = by["Q17167$E"]
    assert pop.value == {"amount": "+4063000", "unit": None}
    assert pop.point_in_time == {"snaktype": "somevalue"}
    assert by["Q17167$G"].value_kind == "novalue"
    assert e.sitelinks_count == 3 and set(e.sitelinks) == {"enwiki", "frwiki"}
    assert e.labels["fr"] == "République romaine"


def test_kg_conversion_handles_bce_julian_and_inferred_precision() -> None:
    allow = wd.load_allowlist()
    e = wd.extract_entity(entity_json(), allow, raw_sha256="a" * 64, retrieved_at=NOW)
    by = {s.statement_id: s for s in e.statements}
    st = hv.to_kg_statement(by["Q17167$C"])
    assert st.value["type"] == "time" and st.value["time"].startswith("-0043-03-15")  # 44 BCE -> astro -43
    assert st.value["calendar"] == "julian" and st.object_qid is None
    cap = hv.to_kg_statement(by["Q17167$D"])
    assert cap.object_qid == "Q220" and cap.value is None and cap.start.iso.startswith("-0508")
    pop = hv.to_kg_statement(by["Q17167$E"])
    assert pop.value == {"type": "quantity", "amount": "+4063000", "unit": None}
    assert pop.point_in_time is None and pop.qualifiers["P585"] == [{"snaktype": "somevalue"}]
    assert hv.to_kg_statement(by["Q17167$G"]).value == {"type": "novalue"}
    ent = hv.to_kg_entity(e)
    assert ent.kind == hv.LABEL_KIND and ent.source_revision == "2000000001" and ent.sitelinks == 3
    labels_only = wd.extract_entity({k: v for k, v in entity_json().items() if k != "claims"}, allow,
                                    raw_sha256="a" * 64, retrieved_at=NOW)
    assert not labels_only.claims_fetched and hv.to_kg_entity(labels_only).source_revision == "2000000001:labels"
    # WDQS dates carry no precision: the coarsest consistent precision is used and flagged
    assert hv.coarse_precision({"time": "+1492-01-01T00:00:00Z", "precision": None})["precision"] == 9
    assert hv.coarse_precision({"time": "+1492-10-01T00:00:00Z", "precision": None})["precision"] == 10
    t = hv.coarse_precision({"time": "+1492-10-12T00:00:00Z", "precision": None, "astronomical": True})
    assert t["precision"] == 11 and t["precision_inferred"] is True


def test_recorded_fixture_extracts() -> None:
    manifest = json.loads((FIX / "manifest.json").read_text())
    allow = wd.load_allowlist()
    ents = [e for e in manifest if "wbgetentities" in e["url"]]
    assert ents
    for m in ents:
        body = (FIX / m["file"]).read_bytes()
        doc = json.loads(body)
        for _qid, raw in doc["entities"].items():
            e = wd.extract_entity(raw, allow, raw_sha256=m["sha256"], retrieved_at=NOW)
            assert e.labels.get("en")
            assert all(s.property in allow and s.rank in ("preferred", "normal") for s in e.statements)
            assert e.lastrevid and e.statements  # props=info -> every snapshot has its revision
            for s in e.statements:
                assert s.source_revision == e.lastrevid
                kg = hv.to_kg_statement(s)
                assert kg.source_revision == str(e.lastrevid) and kg.statement_id


# --------------------------------------------------------------------------- specs
def _spec(tmp_path: Path, data: dict[str, Any], domain: str = "history") -> hv.Spec:
    d = tmp_path / domain
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{data['id'].split('.')[-1]}.yaml"
    p.write_text(yaml.safe_dump(data))
    return hv.load_spec(p)


BASE = {"id": "history.empires", "domain": "history", "description": "Every empire item.",
        "purpose": "seed_entities", "sparql": "SELECT DISTINCT ?item WHERE { ?item wdt:P31 wd:Q48349 . }",
        "max_rows": 10}


def test_registry_specs_are_valid() -> None:
    specs = hv.discover_specs([hv.SPEC_ROOT / "core"])
    res = hv.validate_all(specs, wd.load_allowlist())
    assert specs and not any(res.values()), res


def test_readme_examples_validate(tmp_path: Path) -> None:
    text = (hv.SPEC_ROOT / "README.md").read_text()
    blocks = [b.split("```", 1)[0] for b in text.split("```yaml\n")[1:]]
    examples = [yaml.safe_load(b) for b in blocks if b.lstrip().startswith("id:")]
    assert len(examples) == 2
    allow = wd.load_allowlist()
    for ex in examples:
        assert hv.validate_spec(_spec(tmp_path, ex, ex["domain"]), allow) == []


@pytest.mark.parametrize(
    ("patch", "needle"),
    [
        ({"sparql": "SELECT ?item WHERE { ?item wdt:P31 wd:Q5 } LIMIT 10"}, "LIMIT"),
        ({"sparql": "SELECT ?item WHERE { ?item wdt:P31 wd:Q5 . SERVICE wikibase:label { } }"}, "label"),
        ({"split": {"by": "year", "from": 1500, "to": 1600, "step": 10}}, "placeholders"),
        ({"purpose": "statements", "columns": {"subject": "item", "object": "item", "property": "P18"}},
         "not allow-listed"),
        ({"id": "people.empires"}, "must start with"),
        ({"max_rows": 0}, "schema"),
        ({"unknown_field": 1}, "schema"),
    ],
)
def test_invalid_specs_are_rejected(tmp_path: Path, patch: dict[str, Any], needle: str) -> None:
    errs = hv.validate_spec(_spec(tmp_path, {**BASE, **patch}), wd.load_allowlist())
    assert any(needle in e for e in errs), errs


def test_chunk_rendering(tmp_path: Path) -> None:
    years = _spec(tmp_path, {**BASE, "sparql": "SELECT ?item WHERE { ?item wdt:P571 ?d . "
                             "FILTER(YEAR(?d) >= {{year_from}} && YEAR(?d) <= {{year_to}}) }",
                             "split": {"by": "year", "from": -100, "to": 120, "step": 100}})
    ch = hv.render_chunks(years)
    assert [c.key for c in ch] == ["year:-100..-1", "year:0..99", "year:100..120"]
    assert "YEAR(?d) >= -100 && YEAR(?d) <= -1" in ch[0].query
    vals = _spec(tmp_path, {**BASE, "id": "history.v", "sparql": "SELECT ?item WHERE { VALUES ?item { {{values}} } }",
                            "split": {"by": "values", "source": "list", "qids": ["Q1", "Q2", "Q3"], "chunk_size": 2}})
    ch = hv.render_chunks(vals)
    assert len(ch) == 2 and "wd:Q1 wd:Q2" in ch[0].query and "wd:Q3" in ch[1].query
    pre = _spec(tmp_path, {**BASE, "id": "history.p", "sparql": "SELECT ?item WHERE { ?item wdt:P31 wd:Q5 {{qid_filter}} }",
                           "split": {"by": "qid_prefix", "prefixes": ["1", "2"]}})
    ch = hv.render_chunks(pre)
    assert 'STRSTARTS(STR(?item), "http://www.wikidata.org/entity/Q2")' in ch[1].query
    assert hv.with_limit("SELECT ?x WHERE {}", 11).endswith("LIMIT 11")


# --------------------------------------------------------------------------- orchestration
def _router(rows: int, entities_seen: list[list[str]]) -> Any:
    def route(url: str) -> tuple[int, bytes, dict[str, str]]:
        if url.startswith(wd.WDQS_URL):
            return 200, sparql_body(["item"], [{"item": uri(f"Q{17167 + i}")} for i in range(rows)]), {}
        ids = url.split("ids=")[1].split("&")[0].split("|")
        entities_seen.append(ids)
        ents = {}
        for i in ids:
            e = entity_json()
            e["id"] = i
            if "|claims|" not in url:
                e.pop("claims")
            ents[i] = e
        return 200, json.dumps({"entities": ents}).encode(), {}

    return route


async def test_seed_spec_end_to_end_resumable_and_idempotent(tmp_path: Path) -> None:
    seen: list[list[str]] = []
    http = ScriptedHttp(router=_router(3, seen))
    sink = hv.JsonlSink(tmp_path / "out.jsonl.gz")
    h = hv.Harvester(client_for(http, tmp_path), sink, state_dir=tmp_path / "state")
    spec = _spec(tmp_path, {**BASE, "entity_kind": "empire", "expand_hops": 1})
    rep = await h.run_spec(spec)
    assert rep.status == "done", rep.failures
    assert rep.seeds == 3 and rep.entities_full == 3
    # objects of the seeds (Q3024240, Q220, Q123?, Q5727902...) got label snapshots, not claims
    assert rep.entities_labels >= 2 and "Q220" in seen[-1]
    assert rep.statements > 0 and rep.statements_dropped_deprecated == 3
    n_calls = len(http.calls)
    again = await h.run_spec(spec)  # completed with the same spec sha256: no request
    assert again.status == "skipped" and len(http.calls) == n_calls
    state = json.loads((tmp_path / "state" / "history.empires.json").read_text())
    assert state["status"] == "done" and len(state["seeds"]) == 3
    sink.close()


async def test_row_bound_is_enforced_not_truncated(tmp_path: Path) -> None:
    http = ScriptedHttp(router=_router(11, []))
    h = hv.Harvester(client_for(http, tmp_path), hv.NullSink(), state_dir=tmp_path / "state")
    rep = await h.run_spec(_spec(tmp_path, BASE))
    assert rep.status == "failed" and rep.failures[0]["code"] == "row_bound_exceeded"
    assert "%0ALIMIT+11&format=json" in http.calls[0][0]  # max_rows + 1 appended


async def test_statement_rows(tmp_path: Path) -> None:
    rows = [
        {"person": uri("Q1"), "st": {"type": "uri", "value": wd.ENTITY_PREFIX + "statement/Q1-ABC"},
         "position": uri("Q30461"), "start": {"type": "literal", "datatype": wd.XSD + "dateTime",
                                              "value": "1789-04-30T00:00:00Z"},
         "rank": {"type": "uri", "value": "http://wikiba.se/ontology#NormalRank"}},
        {"person": uri("Q2"), "position": uri("Q30461"),
         "rank": {"type": "uri", "value": "http://wikiba.se/ontology#DeprecatedRank"}},
        {"person": {"type": "literal", "value": "not an item"}, "position": uri("Q30461")},
    ]
    spec = _spec(tmp_path, {
        "id": "people.potus", "domain": "people", "description": "Holders of a position.", "purpose": "statements",
        "sparql": "SELECT ?person ?st ?position ?start ?rank WHERE { ?person p:P39 ?st . ?st ps:P39 ?position ; "
                  "wikibase:rank ?rank . OPTIONAL { ?st pq:P580 ?start } }",
        "columns": {"subject": "person", "property": "P39", "object": "position", "statement": "st",
                    "start": "start", "rank": "rank"},
        "max_rows": 10}, domain="people")
    http = ScriptedHttp(router=lambda url: (200, sparql_body(["person", "st", "position", "start", "rank"], rows), {})
                        if url.startswith(wd.WDQS_URL) else
                        (200, json.dumps({"entities": {"Q1": {"id": "Q1", "labels": {}}}}).encode(), {}))
    h = hv.Harvester(client_for(http, tmp_path), hv.NullSink(), state_dir=tmp_path / "state")
    res = await h.client.sparql("SELECT ...")
    stmts, skipped = h.rows_to_statements(spec, res)
    assert skipped == 1 and stmts[1] is None  # deprecated row dropped
    s = stmts[0]
    assert s is not None and s.statement_id == "Q1$ABC" and s.source_key == wd.SOURCE_WDQS
    assert s.start == {"time": "+1789-04-30T00:00:00Z", "precision": 11, "calendar": None, "astronomical": True,
                       "precision_inferred": True}
    kg = hv.to_kg_statement(s)
    assert kg.start.iso.startswith("+1789-04-30") and kg.start.precision == 11 and kg.object_qid == "Q30461"
    # WDQS gives no entity revision: explicitly unknown, versioned by retrieval time
    assert kg.source_revision == "wdqs@2026-10-15T12:00:00Z"
    assert kg.qualifiers["_revision"]["wikidata_revision"] == "unknown"
    rep = await h.run_spec(spec)
    assert rep.status == "done" and rep.statements == 1 and rep.entities_labels == 1


def test_harvest_refuses_the_dev_database() -> None:
    for name in ("reva", "postgres", "other"):
        with pytest.raises(hv.SpecError):
            hv.harvest_settings(name)


# --------------------------------------------------------------------------- WDQS-free (API) paths
def test_or_groups_respect_the_cirrussearch_length_limit() -> None:
    terms = [f"P298={c}" for c in ("FRA", "DEU", "ESP") * 40]
    groups = hv.or_groups("haswbstatement:", terms, " -haswbstatement:P576")
    assert all(len(g) <= 300 for g in groups) and len(groups) > 1
    assert sum(g.count("P298=") for g in groups) == len(terms)
    assert all(g.endswith(" -haswbstatement:P576") for g in groups)


def _search_router(pages: dict[str, list[str]], entities: dict[str, dict[str, Any]]) -> Any:
    from urllib.parse import parse_qs, urlsplit

    def route(url: str) -> tuple[int, bytes, dict[str, str]]:
        q = parse_qs(urlsplit(url).query)
        if q.get("list") == ["search"]:
            hits = pages.get(q["srsearch"][0], [])
            off, lim = int(q["sroffset"][0]), int(q["srlimit"][0])
            page = [{"title": h} for h in hits[off:off + lim]]
            return 200, json.dumps({"query": {"searchinfo": {"totalhits": len(hits)}, "search": page}}).encode(), {}
        ids = q["ids"][0].split("|")
        return 200, json.dumps({"entities": {i: entities[i] for i in ids}}).encode(), {}

    return route


async def test_api_seeds_follow_subclasses_and_page(tmp_path: Path) -> None:
    pages = {
        "haswbstatement:P279=Q3024240": ["Q100"],
        "haswbstatement:P279=Q100": [],
        "haswbstatement:P31=Q3024240|P31=Q100": [f"Q{1000 + i}" for i in range(700)],  # 2 pages
    }
    http = ScriptedHttp(router=_search_router(pages, {}))
    h = hv.Harvester(client_for(http, tmp_path), hv.NullSink(), state_dir=tmp_path / "state", via="api")
    spec = _spec(tmp_path, {**BASE, "max_rows": 1000,
                            "api_search": {"instance_of": ["Q3024240"], "subclass_depth": 2}})
    seeds, info = await h.api_seeds(spec)
    assert len(seeds) == 700 and info["classes"] == 2 and not info["window_exceeded"]
    offsets = [u.split("sroffset=")[1].split("&")[0] for u, _ in http.calls if "P31%3D" in u]
    assert offsets == ["0", "500"]


async def test_map_countries_from_p298_claims(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cw = tmp_path / "cw.json"
    cw.write_text(json.dumps({"entities": [{"key": "FRA", "iso3": "FRA", "label": "France"},
                                           {"key": "SRB", "iso3": "SRB", "label": "Serbia"}]}))
    monkeypatch.setattr(hv, "CROSSWALK", cw)

    def p298(code: str, *, ended: bool = False) -> dict[str, Any]:
        c: dict[str, Any] = {"rank": "normal", "mainsnak": snak("P298", code, "external-id")}
        if ended:
            c["qualifiers"] = {"P582": [snak("P582", {"time": "+2006-06-05T00:00:00Z", "precision": 11}, "time")]}
        return c

    ents = {
        "Q142": {"id": "Q142", "lastrevid": 1, "labels": {"fr": {"value": "France"}},
                 "claims": {"P298": [p298("FRA")], "P31": [{"rank": "normal", "mainsnak": snak("P31", "Q3624078")}]}},
        "Q403": {"id": "Q403", "lastrevid": 2, "claims": {"P298": [p298("SRB")]}},
        "Q37024": {"id": "Q37024", "lastrevid": 3,  # Serbia and Montenegro: dissolved
                   "claims": {"P298": [p298("SRB", ended=True)], "P576": [{"rank": "normal", "mainsnak": snak(
                       "P576", {"time": "+2006-06-05T00:00:00Z", "precision": 11}, "time")}]}},
        "Q865": {"id": "Q865", "lastrevid": 4, "claims": {"P298": [p298("TWN")]}},
    }
    pages = {"haswbstatement:P298=FRA|P298=SRB|P298=TWN": ["Q142", "Q403", "Q37024", "Q865"]}
    http = ScriptedHttp(router=_search_router(pages, ents))
    out = tmp_path / "country_qids.json"
    stats = await hv.map_countries_search(client_for(http, tmp_path), out_path=out)
    doc = json.loads(out.read_text())
    assert doc["countries"]["FRA"]["qid"] == "Q142" and doc["countries"]["FRA"]["lastrevid"] == 1
    assert doc["countries"]["SRB"]["qid"] == "Q403"  # the dissolved holder is excluded
    assert doc["countries"]["TWN"]["not_in_crosswalk"] is True
    assert stats["mapped_crosswalk"] == 2 and doc["unmapped"] == []
