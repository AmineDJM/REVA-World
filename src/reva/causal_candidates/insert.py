"""Hand candidates to CAUSCORE's quarantined table ``kg.causal_candidate`` (agent CAUSCAND).

``reva.causal.insert_candidates`` requires every candidate to cite an archived raw artifact. Full articles
are deliberately not archived (CC BY-SA text: only the matched sentences are stored), so the artifact
each candidate cites is the **sentence bundle** of its article revision: canonical JSON with the article
title, page id, revision id and timestamp, the sha256 of the API response and of the revision's wikitext
(both computed at fetch time, the bytes discarded), the licence/attribution, and the stored sentences of
that revision (rendered + verbatim wikitext + wikilinks). The bundle is what REVA kept; the revision id
pins the full text at the source.

Only throwaway databases (``reva_*``, never ``reva``) are written by :func:`insert_into_db`.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from reva.causal_candidates.extract import (
    EXTRACTOR_VERSION,
    LICENCE,
    LICENCE_URL,
    SOURCE_KEY,
    SentenceRecord,
    record_to_dict,
)

#: ``kg.causal_candidate.extractor_id`` (CAUSCORE pattern ``^[a-z0-9][a-z0-9_.-]{0,99}$``).
CAUSCORE_EXTRACTOR_ID = "causcand.wikipedia_patterns"
BUNDLE_SCHEMA = "causcand-sentence-bundle/1"


def _canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sentence_bundles(
    records: Iterable[SentenceRecord], fetch_log: Iterable[Mapping[str, Any]] = (), keep: set[str] | None = None
) -> dict[tuple[str, int], bytes]:
    """(lang, revision id) -> canonical bundle bytes. ``keep``: only these sentence keys
    (``lang:revid:paragraph:index``), e.g. the sentences candidates were extracted from."""
    meta = {(d["lang"], int(d["revision_id"])): d for d in fetch_log if d.get("revision_id")}
    by: dict[tuple[str, int], list[SentenceRecord]] = defaultdict(list)
    for r in records:
        if keep is None or r.key in keep:
            by[(r.lang, r.revision_id)].append(r)
    out: dict[tuple[str, int], bytes] = {}
    for (lang, rev), recs in sorted(by.items()):
        recs.sort(key=lambda r: (r.paragraph, r.index))
        m = meta.get((lang, rev), {})
        first = recs[0]
        out[(lang, rev)] = _canon(
            {
                "schema": BUNDLE_SCHEMA,
                "source_key": SOURCE_KEY,
                "wiki": f"{lang}wiki",
                "article_title": first.title,
                "article_qid": first.article_qid,
                "page_id": first.page_id,
                "revision_id": rev,
                "revision_timestamp": first.revision_timestamp,
                "url": f"https://{lang}.wikipedia.org/w/index.php?oldid={rev}",
                "api_response_sha256": first.raw_sha256,
                "wikitext_sha256": m.get("wikitext_sha256"),
                "wikitext_chars": m.get("wikitext_chars"),
                "retrieved_at": first.retrieved_at,
                "content": "the sentences candidates were extracted from; the full article is not archived",
                "rights": {
                    "licence": LICENCE,
                    "licence_url": LICENCE_URL,
                    "attribution": f'Wikipedia contributors, "{first.title}", {lang}.wikipedia.org, revision {rev}',
                },
                "sentences": [record_to_dict(r) for r in recs],
            }
        )
    return out


def candidate_in(doc: Mapping[str, Any], bundle_sha: str) -> Any:
    from reva.causal.model import CandidateIn

    s = doc["source"]
    selector = {
        "candidate_id": doc["candidate_id"],
        "wiki": s["wiki"],
        "page": s["article_title"],
        "page_id": s["page_id"],
        "article_qid": s["article_qid"],
        "revision_id": s["revision_id"],
        "section": s["section"],
        "paragraph": s["paragraph"],
        "sentence": s["sentence_index"],
        "url": s["url"],
        "api_response_sha256": s["raw_sha256"],
        "connective": doc["connective"],
        "direction_rule": doc["direction_rule"],
        "cause": {k: doc["cause"][k] for k in ("link_target", "resolved_title", "anchor", "span")},
        "effect": {k: doc["effect"][k] for k in ("link_target", "resolved_title", "anchor", "span")},
        "negation_hedge_flag": doc["negation_hedge_flag"],
        "cues": {"negation": doc["flags"].get("negation_cues", []), "hedge": doc["flags"].get("hedge_cues", [])},
        "sentence_wikitext": doc["sentence_wikitext"][:8000],
        "extractor_version": doc["extractor"]["version"],
        "licence": LICENCE,
    }
    return CandidateIn(
        cause_id=doc["cause_qid"],
        effect_id=doc["effect_qid"],
        relation=doc["relation_hint"],
        tier="T5",
        source_revision=f"{s['wiki']}:{s['revision_id']}",
        source_sentence=doc["sentence"],
        selector=selector,
        raw_sha256=bundle_sha,
    )


def insert(
    engine: Any,
    store: Any,
    docs: Sequence[Mapping[str, Any]],
    records: Sequence[SentenceRecord],
    fetch_log: Sequence[Mapping[str, Any]],
    *,
    origin: str = "live",
) -> dict[str, Any]:
    """Archive the sentence bundles the candidates cite and call ``reva.causal.insert_candidates``."""
    from reva.causal.model import insert_candidates

    keep = {
        f"{d['source']['lang']}:{d['source']['revision_id']}:{d['source']['paragraph']}:{d['source']['sentence_index']}"
        for d in docs
    }
    bundles = sentence_bundles(records, fetch_log, keep)
    sha_of: dict[tuple[str, int], str] = {}
    for k, data in bundles.items():
        obj = store.put_bytes(data, "raw", "application/json")
        sha_of[k] = obj.sha256
        assert obj.sha256 == hashlib.sha256(data).hexdigest()
    cands, used = [], []
    for d in docs:
        k = (d["source"]["lang"], int(d["source"]["revision_id"]))
        cands.append(candidate_in(d, sha_of[k]))
        used.append(sha_of[k])
    raws = sorted(set(used))
    retrieved = max(datetime.fromisoformat(d["source"]["retrieved_at"]) for d in docs)
    rep = insert_candidates(
        engine,
        store,
        extractor_id=CAUSCORE_EXTRACTOR_ID,
        extractor_version=EXTRACTOR_VERSION,
        source_key=SOURCE_KEY,
        raw_sha256s=raws,
        retrieved_at=retrieved,
        origin=origin,
        candidates=cands,
    )
    out = rep.as_dict() if hasattr(rep, "as_dict") else dict(rep.__dict__)
    out.update(bundles_archived=len(bundles), bundles_cited=len(raws))
    return out


def insert_into_db(out_dir: Path, *, db_name: str, object_root: Path) -> dict[str, Any]:
    """Insert ``out_dir/candidates.jsonl`` into a throwaway database (created/migrated if needed)."""
    from reva.adapters.objects.filesystem import FilesystemArtifactStore
    from reva.adapters.postgres.engine import make_engine
    from reva.causal_candidates.fetch import load_records
    from reva.kg.harvest import ensure_database

    settings = ensure_database(db_name)  # refuses "reva" and non reva_* names
    engine = make_engine("worker", settings)
    try:
        store = FilesystemArtifactStore(object_root, settings.reva_s3_prefix)
        docs = [json.loads(x) for x in (out_dir / "candidates.jsonl").read_text().splitlines()]
        log = [json.loads(x) for x in (out_dir / "fetch_log.jsonl").read_text().splitlines()]
        return insert(engine, store, docs, load_records(out_dir), log)
    finally:
        engine.dispose()
