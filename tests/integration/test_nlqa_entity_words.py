"""NLQA entity lookup by the words of KG labels and aliases (APIFIX, docs/dev/v3_apifix.md §4).

"Pourquoi la dissolution de l'URSS ?" found nothing: no label or alias *contains* "dissolution de l'URSS" (the
item is labelled "dislocation de l'URSS" in French and "dissolution of the Soviet Union" in English), and "l URSS"
written without its apostrophe matched nothing either. The KG adapter now falls back to the entities whose fr/en
labels and aliases hold every content word of the text (kg.entity_label, the existing label index), never a
hard-coded name -> id map. Synthetic ids (``Q9900000x``), labels shaped like the real items'."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from reva.kg.model import KgEntity, load_batch
from reva.nlqa.adapters import KgDbAdapter, label_words

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def kg(db: Any) -> KgDbAdapter:
    raw = db.store.put_bytes(b"apifix words fixture", "raw", "application/json").sha256
    ents = [
        KgEntity(
            "Q99000001",
            "event",
            {"fr": "dislocation de l'URSS", "en": "dissolution of the Soviet Union"},
            aliases={"fr": ["chute de l'URSS"], "en": ["collapse of the USSR"]},
            sitelinks=90,
        ),
        KgEntity("Q99000002", "event", {"fr": "guerre russo-ukrainienne", "en": "Russo-Ukrainian war"}, sitelinks=80),
        KgEntity("Q99000003", "event", {"fr": "dissolution de l'Assemblée", "en": "dissolution of the Assembly"}),
    ]
    rep = load_batch(
        db.worker,
        db.store,
        entities=ents,
        statements=[],
        source_key="apifix.kg",
        raw_sha256s=[raw],
        retrieved_at=datetime.now(UTC),
        origin="test_fixture",
    )
    assert rep.rejected_total == 0
    return KgDbAdapter(db.api)


def test_words_of_the_labels_find_the_event(kg: KgDbAdapter) -> None:
    for q in ("dissolution de l'URSS", "dislocation de l URSS", "URSS dissolution"):
        hits = kg.search_entities(q, kind=None, lang="fr", limit=10)
        assert [h.entity.qid for h in hits] == ["Q99000001"], (q, hits)
        assert hits[0].match == "label_words"
    # the label as written still matches first (exact), never through the words fallback
    exact = kg.search_entities("guerre russo-ukrainienne", kind=None, lang="fr", limit=10)
    assert exact[0].entity.qid == "Q99000002" and exact[0].match == "label_exact"


def test_one_word_or_unknown_words_find_nothing(kg: KgDbAdapter) -> None:
    assert label_words("la dissolution de l'URSS") == ["dissolution", "urss"]
    assert kg.search_entities("dissolution du Bundestag", kind=None, lang="fr", limit=10) == []
    assert kg.search_entities("URSS", kind=None, lang="fr", limit=10)[0].match != "label_words"
