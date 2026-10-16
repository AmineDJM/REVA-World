"""LOADALL: governed import of archived Wikidata harvests — extraction profiles and raw verification (no DB)."""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reva.adapters.connectors import wikidata as wd
from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.causal_harvest import core
from reva.kernel.errors import IntegrityFailure
from reva.kg import harvest as hv
from reva.kg import import_harvest as ih

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def _snak(pid: str, value: Any, kind: str = "item") -> dict[str, Any]:
    if kind == "item":
        return {
            "snaktype": "value",
            "property": pid,
            "datatype": "wikibase-item",
            "datavalue": {"type": "wikibase-entityid", "value": {"entity-type": "item", "id": value}},
        }
    return {"snaktype": "value", "property": pid, "datatype": "string", "datavalue": {"type": "string", "value": value}}


def _event() -> dict[str, Any]:
    return {
        "id": "Q8698",
        "type": "item",
        "lastrevid": 2_400_000_000,
        "modified": "2026-10-01T00:00:00Z",
        "labels": {
            "en": {"language": "en", "value": "Great Depression"},
            "it": {"language": "it", "value": "Grande depressione"},
        },
        "descriptions": {},
        "aliases": {},
        "sitelinks": {"enwiki": {"site": "enwiki", "title": "Great Depression"}},
        "claims": {
            "P31": [{"id": "Q8698$a", "rank": "normal", "mainsnak": _snak("P31", "Q1129232")}],
            "P828": [
                {
                    "id": "Q8698$b",
                    "rank": "normal",
                    "mainsnak": _snak("P828", "Q201684"),
                    "references": [
                        {
                            "snaks": {
                                "P248": [_snak("P248", "Q42")],
                                "P1683": [_snak("P1683", "the crash caused it", "string")],
                            }
                        }
                    ],
                }
            ],
        },
    }


def _load(
    params: dict[str, Any], raws: tuple[str, ...] = ("a" * 64,), source_key: str = "wikidata.wbapi"
) -> ih.SourceLoad:
    return ih.SourceLoad(1, source_key, "live", raws, NOW, params)


def test_causwd_profile_keeps_extended_references_and_does_not_prune_archived_entitydata() -> None:
    allow = wd.load_allowlist()
    assert "P1683" not in allow.references and "P1683" in core.CAUSAL_REFERENCE_PIDS
    docs = {"a" * 64: {"entities": {"Q8698": _event()}}}
    prof = ih.profile_for(_load({"harvester": ih.CAUSWD, "mode": "entitydata_archived"}), {})
    assert isinstance(prof, ih.Profile) and not prof.prune and prof.kind == "unclassified"
    ents, _ = ih.derive_entities(docs, prof, allow, retrieved_at=NOW)
    (e,) = ents
    causal = [s for s in e.statements if s.property == "P828"]
    assert causal[0].references == [{"P1683": ["the crash caused it"], "P248": ["Q42"]}]
    assert causal[0].statement_id == "Q8698$b" and causal[0].source_revision == 2_400_000_000
    assert causal[0].raw_sha256 == "a" * 64 and causal[0].retrieved_at == NOW
    assert "it" in e.labels  # CAUSWD read archived EntityData unpruned: so does the import
    prof2 = ih.profile_for(_load({"harvester": ih.CAUSWD, "mode": "wbgetentities"}), {})
    assert isinstance(prof2, ih.Profile) and prof2.prune
    (e2,) = ih.derive_entities(docs, prof2, allow, retrieved_at=NOW)[0]
    assert "it" not in e2.labels  # the client pruned EntityData fall-backs to the requested languages


def test_wdharvest_profile_uses_spec_kind_properties_and_p31_filter(tmp_path: Path) -> None:
    allow = wd.load_allowlist()
    p = tmp_path / "history" / "events.yaml"
    p.parent.mkdir(parents=True)
    p.write_text(
        json.dumps(
            {
                "id": "history.events",
                "domain": "history",
                "purpose": "seed_entities",
                "description": "x",
                "sparql": "SELECT ?item WHERE { ?item wdt:P31 wd:Q1 }",
                "max_rows": 10,
                "entity_kind": "crisis",
                "api_search": {"discover": "whatlinkshere", "instance_of": ["Q1129232"]},
            }
        )
    )
    spec = hv.load_spec(p)
    other = _event() | {
        "id": "Q9",
        "claims": {"P31": [{"id": "Q9$a", "rank": "normal", "mainsnak": _snak("P31", "Q5")}]},
    }
    docs = {"a" * 64: {"entities": {"Q8698": _event(), "Q9": other, "Q404": {"id": "Q404", "missing": ""}}}}
    prof = ih.profile_for(_load({"harvester": ih.WDHARVEST, "mode": "full", "spec": "history.events"}), {spec.id: spec})
    assert isinstance(prof, ih.Profile) and prof.kind == "crisis" and prof.require_p31 == frozenset({"Q1129232"})
    ents, notes = ih.derive_entities(docs, prof, allow, retrieved_at=NOW)
    assert [e.qid for e in ents] == ["Q8698"] and notes["candidate_rejected_p31"] == 1 and notes["missing_entity"] == 1
    refs = next(s.references for s in ents[0].statements if s.property == "P828")
    assert refs == [{"P248": ["Q42"]}]  # the generic extractor keeps the allow-listed reference properties only


def test_unsupported_loads_are_skipped_with_a_reason() -> None:
    assert ih.profile_for(_load({"harvester": "other/1"}), {}) == "unknown_harvester"
    assert ih.profile_for(_load({"harvester": ih.WDHARVEST, "mode": "full", "spec": "nope"}), {}) == "unknown_spec"
    assert ih.profile_for(_load({"harvester": ih.CAUSWD, "mode": "x"}), {}) == "unknown_mode"
    assert (
        ih.profile_for(_load({"harvester": ih.WDHARVEST}, source_key="wikidata.wdqs"), {}) == "unsupported_source_key"
    )


def test_import_key_is_stable_and_content_based() -> None:
    prof = ih.profile_for(_load({"harvester": ih.CAUSWD, "mode": "wbgetentities"}), {})
    assert isinstance(prof, ih.Profile)
    k1 = ih.import_key(_load({"harvester": ih.CAUSWD, "mode": "wbgetentities"}, raws=("b" * 64, "a" * 64)), prof)
    k2 = ih.import_key(_load({"harvester": ih.CAUSWD, "mode": "wbgetentities"}, raws=("a" * 64, "b" * 64)), prof)
    k3 = ih.import_key(_load({"harvester": ih.CAUSWD, "mode": "wbgetentities"}, raws=("a" * 64,)), prof)
    assert k1 == k2 != k3


def test_read_raw_verifies_address_and_body_hash(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / "o")
    body = json.dumps({"entities": {"Q8698": _event()}}).encode()
    obj = store.put_bytes(gzip.compress(body, mtime=0), "raw", "application/gzip")
    good = {obj.sha256: hashlib.sha256(body).hexdigest()}
    data, doc = ih.read_raw(store, obj.sha256, good)
    assert hashlib.sha256(data).hexdigest() == obj.sha256 and "Q8698" in doc["entities"]
    with pytest.raises(IntegrityFailure):
        ih.read_raw(store, obj.sha256, {obj.sha256: "0" * 64})

    class Tampered:
        def get(self, ns: str, sha: str) -> bytes:
            return gzip.compress(b'{"entities": {}}', mtime=0)

    with pytest.raises(IntegrityFailure):
        ih.read_raw(Tampered(), obj.sha256, {})
