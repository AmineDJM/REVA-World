"""Live Wikidata checks (deselected by default: ``pytest -m live``). Two tiny requests through the locked,
paced client: one WDQS query and one wbgetentities call. Raw responses go to a temporary archive."""

from __future__ import annotations

from pathlib import Path

import pytest

from reva.adapters.connectors import wikidata as wd
from reva.adapters.objects.filesystem import FilesystemArtifactStore

pytestmark = pytest.mark.live


async def test_live_wdqs_and_wbgetentities(tmp_path: Path) -> None:
    http = wd.make_live_http()
    client = wd.WikidataClient(http, archive=wd.Archive(FilesystemArtifactStore(tmp_path), tmp_path / "i.jsonl"))
    try:
        res = await client.sparql('SELECT ?item WHERE { ?item wdt:P298 "FRA" } LIMIT 5')
        assert wd.ENTITY_PREFIX + "Q142" in res.column("item")
        ents = await client.get_entities(["Q142"], props=wd.ENTITY_PROPS)
        e = wd.extract_entity(
            ents.entities["Q142"],
            wd.load_allowlist(),
            raw_sha256=ents.raw_sha256s[0],
            retrieved_at=ents.retrieved_at,
            kind_hint="country",
        )
        assert e.labels["fr"] == "France" and e.lastrevid
        assert any(s.property == "P36" and s.object_qid == "Q90" for s in e.statements)  # capital Paris
        assert all(s.rank != "deprecated" for s in e.statements)
    finally:
        await http.aclose()
