"""CAUSCAND: MediaWiki API parsing, the resumable harvest, linking, budgets and the CAUSCORE hand-off (offline).

Fixtures (``tests/fixtures/causal_candidates/``, see its ``manifest.json``): excerpts of real API responses
(verbatim sentences of pinned revisions, CC BY-SA 4.0) and reconstructed link-resolution responses.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from reva.causal_candidates import cli
from reva.causal_candidates import extract as X
from reva.causal_candidates import fetch as F
from reva.causal_candidates import insert as I

REPO = Path(__file__).resolve().parents[2]
FX = REPO / "tests" / "fixtures" / "causal_candidates"


class _Resp:
    def __init__(self, body: bytes, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.body, self.status, self.headers = body, status, headers or {}
        self.retrieved_at = datetime(2026, 10, 16, 20, 40, tzinfo=UTC)


class FakeWiki:
    """Serves the fixtures by request kind and language; records every URL (no network)."""

    origin = "test_fixture"

    def __init__(self, throttle_first: int = 0) -> None:
        self.urls: list[str] = []
        self.throttle = throttle_first

    async def get(self, url: str, **kw: Any) -> _Resp:
        self.urls.append(url)
        assert kw["headers"]["User-Agent"].startswith("REVA-World/") and "@" not in kw["headers"]["User-Agent"]
        if self.throttle:
            self.throttle -= 1
            return _Resp(b"", 429, {"retry-after": "0"})
        u = urlsplit(url)
        lang = u.hostname.split(".")[0]  # type: ignore[union-attr]
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        assert q["maxlag"] == "5" and q["format"] == "json" and q["formatversion"] == "2"
        kind = "content" if "revisions" in q["prop"] else "resolve"
        return _Resp((FX / f"{kind}_{lang}.json").read_bytes())

    async def aclose(self) -> None:
        return None


def test_parse_content_reads_revision_qid_and_wikitext() -> None:
    pages, missing = F.parse_content((FX / "content_en.json").read_bytes(), ["World War II", "Not there"])
    assert missing == ["Not there"]
    [p] = pages
    assert (p.title, p.page_id, p.qid, p.revision_id) == ("World War II", 32927, "Q362", 1379691426)
    assert "[[Norway Debate|British discontent over the Norwegian campaign]]" in p.wikitext


def test_parse_resolve_follows_normalisation_redirects_missing_and_disambiguation() -> None:
    body = (FX / "resolve_en.json").read_bytes()
    res = F.parse_resolve(body, ["Norway Debate", "winston Churchill", "Red link page", "Mercury"])
    assert res["Norway Debate"].qid == "Q128535" and not res["Norway Debate"].redirected
    w = res["winston Churchill"]
    assert (w.title, w.qid, w.redirected) == ("Winston Churchill (redirect test)", "Q8016", True)
    assert res["Red link page"].title is None and res["Red link page"].qid is None
    assert res["Mercury"].disambiguation


def test_api_errors_are_typed() -> None:
    from reva.kernel.errors import SourceResponseError

    with pytest.raises(SourceResponseError) as e:
        F.parse_content(b'{"error": {"code": "maxlag", "info": "Waiting"}}', ["X"])
    assert e.value.retryable and e.value.details["kind"] == "maxlag"
    with pytest.raises(ValueError):
        F.content_url("en", [])


def test_urls_are_get_only_on_the_two_wikis() -> None:
    u = F.content_url("fr", ["Seconde Guerre mondiale"])
    assert u.startswith("https://fr.wikipedia.org/w/api.php?") and "rvslots=main" in u and "maxlag=5" in u
    assert "ppprop=wikibase_item%7Cdisambiguation" in F.resolve_url("en", ["A"])
    assert set(F.HOSTS.values()) == {"en.wikipedia.org", "fr.wikipedia.org"}


def test_harvest_resolve_and_build_end_to_end_offline(tmp_path: Path) -> None:
    sess = F.Session(FakeWiki())
    arts = [("en", "World War II"), ("fr", "Seconde Guerre mondiale")]
    stats = asyncio.run(F.harvest_sentences(sess, arts, tmp_path))
    assert stats["articles"] == 2 and stats["missing"] == []
    recs = F.load_records(tmp_path)
    assert {r.lang for r in recs} == {"en", "fr"}
    assert not any("Munich" in r.sentence for r in recs)  # reference sections are skipped
    log = [json.loads(x) for x in (tmp_path / "fetch_log.jsonl").read_text().splitlines()]
    assert all(len(d["raw_sha256"]) == 64 and d["revision_id"] for d in log)
    # nothing but prefiltered sentences is stored: no full article text on disk
    stored = (tmp_path / "sentences.jsonl").read_text()
    assert "Notes et références" not in stored
    cands, _ = X.extract(recs)
    asyncio.run(F.resolve_targets(sess, X.needed_targets(cands), tmp_path / "resolutions.jsonl"))
    report = cli.build(tmp_path, linked_at="2026-10-16T21:00:00+00:00")
    docs = [json.loads(x) for x in (tmp_path / "candidates.jsonl").read_text().splitlines()]
    pairs = {(d["source"]["lang"], d["cause"]["resolved_title"], d["effect"]["resolved_title"]) for d in docs}
    assert ("fr", "Inondation du fleuve Jaune de 1938", "Noyade") in pairs
    # modifier arguments ("the transfer of [[Northern Transylvania]]") are not candidates
    assert not any(d["effect"]["resolved_title"] == "Northern Transylvania" for d in docs)
    assert report["candidates"] == len(docs) and report["dropped_at_linking"].get("modifier_argument", 0) >= 1
    for d in docs:
        assert d["tier"] == "T5" and d["promotion_status"] == "not_promoted"
        assert d["source"]["revision_id"] in (1379691426, 240347813)
        assert d["sentence_wikitext"] in (FX / f"content_{d['source']['lang']}.json").read_text(
            encoding="utf-8"
        ) or json.dumps(d["sentence_wikitext"], ensure_ascii=False)[1:-1] in (
            FX / f"content_{d['source']['lang']}.json"
        ).read_text(encoding="utf-8")
    # resumable: a second run fetches nothing already logged
    n = len(sess.http.urls)
    asyncio.run(F.harvest_sentences(sess, arts, tmp_path))
    assert len(sess.http.urls) == n


def test_throttling_is_waited_out_then_stops(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(F.asyncio, "sleep", no_sleep)
    sess = F.Session(FakeWiki(throttle_first=1))
    asyncio.run(F.harvest_sentences(sess, [("en", "World War II")], tmp_path))
    assert sess.throttled == 1 and sess.requests == 2
    sess = F.Session(FakeWiki(throttle_first=5))
    with pytest.raises(F.RateLimitedStop):
        asyncio.run(F.harvest_sentences(sess, [("fr", "Seconde Guerre mondiale")], tmp_path / "b"))
    assert sess.requests == 3


def test_live_requests_go_through_the_budget_meter(monkeypatch: pytest.MonkeyPatch) -> None:
    """The session's client is MeteredHttp: a refused top-up stops the request before the transport."""
    import reva.operations.budgets as budgets
    from reva.operations.budgets import BudgetExhausted, IngestBudget, Reservation, metered

    class Transport:
        origin = "live"
        called = 0

        async def get(self, url: str, **kw: Any) -> Any:  # pragma: no cover - must not be reached
            Transport.called += 1
            raise AssertionError("transport called although the budget is exhausted")

    r = Reservation(F.SOURCE_KEY, "op:r", "op:b", 0, 0, 0, "t")
    m = metered(Transport(), None, r, budget=IngestBudget("t", "", 0, 0, {F.SOURCE_KEY: 1}))  # type: ignore[arg-type]
    monkeypatch.setattr(budgets, "top_up", lambda *a, **k: 0)
    with pytest.raises(BudgetExhausted):
        asyncio.run(F.Session(m).get(F.content_url("en", ["X"]), key="k"))
    assert Transport.called == 0


def test_requested_budget_overlay_only_adds_missing_keys() -> None:
    from reva.operations.budgets import IngestBudget

    req = yaml.safe_load(F.BUDGET_REQUEST.read_text())
    assert req["agent"] == "CAUSCAND" and req["requests"][0]["source_key"] == F.SOURCE_KEY
    overlay = F.requested_budget_overlay()

    class Pol:
        content: ClassVar[dict[str, Any]] = {
            "policy_version": "p",
            "daily": {"http_requests_total": 10, "http_bytes_total": 10},
            "per_source_daily_requests": {"x.y": 3},
        }
        sha256 = ""

    b = overlay(Pol())
    assert isinstance(b, IngestBudget) and b.per_source_requests[F.SOURCE_KEY] == req["requests"][0]["daily_requests"]
    assert b.policy_version == "p+causcand_request"
    Pol.content["per_source_daily_requests"] = {F.SOURCE_KEY: 7}
    assert overlay(Pol()).per_source_requests[F.SOURCE_KEY] == 7  # the pinned value wins once merged


def test_source_registry_entry_matches_the_code() -> None:
    doc = yaml.safe_load((REPO / "registry/sources/wikipedia_causal_text.yaml").read_text())
    assert doc["source_key"] == F.SOURCE_KEY == X.SOURCE_KEY
    assert set(doc["metadata"]["allowed_hosts"]) == set(F.HOSTS.values())
    assert doc["rights"]["licence"] == "CC BY-SA 4.0"


def test_throwaway_database_only() -> None:
    from reva.kg.harvest import SpecError, harvest_settings

    for bad in ("reva", "postgres", "causcand"):
        with pytest.raises(SpecError):
            harvest_settings(bad)


def test_seed_snapshot_selection() -> None:
    from reva.causal_candidates.seeds import EVENT_CLASSES, load_seeds, top_seeds

    doc = load_seeds(FX / "event_seeds_2026-10-16.json")
    top = top_seeds(doc["seeds"], 250)
    assert len(top) == 250 and top[0].qid == "Q362" and top[0].sitelinks >= top[-1].sitelinks
    assert all(any(c in EVENT_CLASSES for c in s.classes) for s in top)
    assert "Q3002150" not in EVENT_CLASSES  # queried by mistake (aircraft crash): filtered out
    arts, chosen = cli.articles_for(FX / "event_seeds_2026-10-16.json", 250)
    assert len(arts) <= 500 and len(chosen) == 250


def test_causcore_handoff_mapping_and_bundles(tmp_path: Path) -> None:
    pytest.importorskip("reva.causal.model")
    sess = F.Session(FakeWiki())
    asyncio.run(F.harvest_sentences(sess, [("fr", "Seconde Guerre mondiale")], tmp_path))
    cands, _ = X.extract(F.load_records(tmp_path))
    asyncio.run(F.resolve_targets(sess, X.needed_targets(cands), tmp_path / "resolutions.jsonl"))
    cli.build(tmp_path)
    docs = [json.loads(x) for x in (tmp_path / "candidates.jsonl").read_text().splitlines()]
    assert docs
    log = [json.loads(x) for x in (tmp_path / "fetch_log.jsonl").read_text().splitlines()]
    keep = {f"fr:{d['source']['revision_id']}:{d['source']['paragraph']}:{d['source']['sentence_index']}" for d in docs}
    bundles = I.sentence_bundles(F.load_records(tmp_path), log, keep)
    [(key, data)] = bundles.items()
    b = json.loads(data)
    assert key == ("fr", 240347813) and b["rights"]["licence"] == "CC BY-SA 4.0"
    assert {s["paragraph"] for s in b["sentences"]} == {d["source"]["paragraph"] for d in docs}
    c = I.candidate_in(docs[0], hashlib.sha256(data).hexdigest())
    assert c.tier == "T5" and c.relation in ("has_cause", "precipitated", "contributing_factor")
    assert c.source_revision == "frwiki:240347813" and c.source_sentence == docs[0]["sentence"]
    assert c.selector["negation_hedge_flag"] == docs[0]["negation_hedge_flag"]
    assert len(json.dumps(c.selector)) < 32_768
