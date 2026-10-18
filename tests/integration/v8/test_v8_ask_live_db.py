"""V8ASK on the dev database `reva` (read-only; skipped when not reachable): the resolution bridges answer the
golden questions that need them, each with its identification wording and sources; the brainstorm reads the real
registries. The full 47-question measure is `uv run python -m reva.v8.ask.golden` (docs/dev/v8_v8ask.md)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def svc() -> Any:
    from reva.adapters.postgres.engine import make_engine
    from reva.nlqa.adapters import build_backends
    from reva.nlqa.catalog import build_catalog
    from reva.settings import get_settings
    from reva.v8.ask.service import AskService

    s = get_settings()
    try:
        eng = make_engine("api", s)
        with eng.connect() as c:
            n = c.execute(text("SELECT count(*) FROM kg.entity_label WHERE lower(label) = 'methane'")).scalar_one()
    except Exception:
        pytest.skip("dev database not reachable")
    if not n:
        pytest.skip("the dev KG has no ChEBI methane record")
    return AskService(build_backends(eng, s), build_catalog())


@pytest.mark.parametrize(
    ("question", "needle"),
    [
        ("De quoi est fait le méthane ?", "aux accents et à la casse près"),
        ("Quelles sont les parties du cœur ?", "libellé anglais « heart »"),
        ("Que possède Glencore ?", "forme juridique"),
    ],
)
def test_bridged_golden_questions_answer_with_sources(svc: Any, question: str, needle: str) -> None:
    b = asyncio.run(svc.answer(question))
    t = " ".join(s["text"] for s in b["sentences"])
    assert b["status"] == "answered", t
    assert needle in t
    assert b["sources"] and any(s["kind"] == "sources" for s in b["sentences"])


def test_brainstorm_reads_real_registries(svc: Any) -> None:
    out = svc.brainstorm("prévoir l'inflation française à 4 trimestres")
    assert out["target"]["iso3"] == "FRA"
    assert out["anchors"]["families"] and out["drivers"]
    assert all(d["n"] >= 1 and d["references"] for d in out["drivers"])
