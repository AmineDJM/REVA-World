"""Published effect estimates -> ``kg.effect_estimate`` (V5, agent PRED-EFFECT; contract
``docs/dev/v5_effect_interface.md``).

Each curated entry ``registry/effects/literature/<id>.yaml`` names one publication (the version actually read), its
document URL and pinned sha256, and the estimates transcribed from it with locator, page and verbatim quote. A run
fetches every document through the budget-metered client, archives the raw bytes, refuses an entry whose bytes no
longer match the pinned sha256 (``document_changed``: the quotes were read in other bytes), re-finds every quote in
the text extracted from the archived bytes (``pdftotext -layout``; an estimate whose quote is missing or on another
page is refused, never loaded) and loads the entry as one ``publication`` delivery (dataset = entry id). An estimate
removed from a curation file is retracted as ``withdrawn_by_curator``.

Fixture mode (``origin = test_fixture``) reads the synthetic entries of ``tests/fixtures/effects/literature`` (a
small text document served by the fixture client), never the registry ones: the source PDFs are all-rights-reserved
and are not committed to the repository.

Hosts: www.nber.org, www.cepii.fr, www.imf.org, www.ecb.europa.eu (open downloads, no account). No atlas table is
registered: the deliverable is the estimates layer.
"""

from __future__ import annotations

import hashlib
import inspect
import sys
from pathlib import Path
from typing import Any

from reva.atlas.sources import AtlasSourceSpec
from reva.effects import curation as cur
from reva.effects import model as effects_model

CONNECTOR_NAME = "atlas.effects_lit"
CONNECTOR_VERSION = "1.0.2"  # 1.0.0 was registered from an in-flight snapshot; 1.0.1 seeded before shock_profile
PARSER_VERSION = "atlas_effects_lit/1"
SOURCE_KEY = "effects.literature"
ATLAS_SOURCE = "effects"
DATASET = "literature_estimates"
HOSTS = ("www.nber.org", "www.cepii.fr", "www.imf.org", "www.ecb.europa.eu", "www.uni-trier.de")
MAX_BYTES = 20_000_000
REPO_ROOT = Path(__file__).resolve().parents[4]
FIXTURE_LITERATURE_DIR = REPO_ROOT / "tests" / "fixtures" / "effects" / "literature"
LICENCE = (
    "Per publication (each entry's rights): NBER working papers (c) authors, short quotations allowed with credit; "
    "IMF and ECB papers (c) IMF / ECB; CEPII working paper (no licence statement). Raw PDFs archived for provenance "
    "only, not redistributed; REVA stores the numbers with verbatim quotes and citations."
)


def code_digest() -> str:
    h = hashlib.sha256()
    for m in (sys.modules[__name__], cur, effects_model):
        h.update(inspect.getsource(m).encode())
    return "sha256:" + h.hexdigest()


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="Published research (NBER, CEPII, IMF and ECB working / occasional papers)",
        licence=LICENCE,
        allowed_hosts=HOSTS,
        datasets=(DATASET,),
        dataset_ids=tuple(e.id for e in cur.load_entries()),
        cadence_seconds=365 * 86400,
        planned_requests=max(1, len(cur.load_entries())),
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={
            "estimates": "registry/effects/literature/<entry>.yaml -> kg.effect_estimate (pub:effects.literature:<entry>:<id>)",
            "variables": "registry/effects/variables.yaml",
        },
        notes="One PDF per curated publication (~7.4 MB in all); quotes re-checked against the archived bytes.",
    )


def entries_for(origin: str) -> list[cur.Entry]:
    return cur.load_entries(FIXTURE_LITERATURE_DIR if origin == "test_fixture" else cur.LITERATURE_DIR)


async def atlas_ingest(run: Any) -> dict[str, Any]:
    if not run.wants(DATASET):
        return {"skipped": True}
    only = str(run.options.get("entry", "")).strip() or None
    out: dict[str, Any] = {"entries": {}, "estimates_loaded": 0, "estimates_refused": 0}
    for entry in entries_for(run.origin):
        if only and entry.id != only:
            continue
        doc = entry.document
        raw = await run.get(doc["url"], key=entry.id, media_types=(str(doc["media_type"]),), max_bytes=MAX_BYTES)
        sha = run.archive(raw)
        body = run.store.get("raw", sha)
        res: dict[str, Any] = {"document_sha256": sha, "bytes": len(body), "url": doc["url"]}
        if doc.get("sha256") and doc["sha256"] != sha:
            res["status"] = "document_changed"
            res["pinned_sha256"] = doc["sha256"]
            out["entries"][entry.id] = res
            continue
        check = cur.text_check(entry, body)
        ests, refused = cur.to_estimates(entry, raw_sha256=sha, check=check)
        rep = effects_model.load_effect_estimates(
            run.engine,
            run.store,
            source_kind="publication",
            source_key=entry.source_key,
            dataset=entry.id,
            raw_sha256s=[sha],
            retrieved_at=raw.retrieved_at,
            origin=run.origin,
            estimates=ests,
            complete=not refused,
            retraction_reason="withdrawn_by_curator",
            parameters={
                "connector": f"{CONNECTOR_NAME}@{CONNECTOR_VERSION}",
                "entry_sha256": entry.sha256,
                "document_url": doc["url"],
                "text_check": check.as_dict(),
                "refused_by_text_check": refused,
            },
        )
        res.update(
            {
                "status": "loaded",
                "text_check_tool": check.tool,
                "refused_by_text_check": refused,
                "load": rep.as_dict(),
            }
        )
        out["entries"][entry.id] = res
        out["estimates_loaded"] += rep.inserted
        out["estimates_refused"] += len(refused) + rep.rejected_total
    return out


__all__ = ["CONNECTOR_NAME", "atlas_ingest", "atlas_source", "entries_for"]
