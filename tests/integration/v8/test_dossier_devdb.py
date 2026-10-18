"""V8DOSSIER on the real dev DB `reva` (read-only): the six showcase briefs generate, every value part carries a
footnote with a source title, every sentence comes from its template, the exports keep every footnote.

Opt-in (reads the 7 GB atlas query file and the forecast registry): ``REVA_V8_DEVDB=1 uv run pytest -m db
tests/integration/v8/test_dossier_devdb.py``. Uses the disk cache of briefs when present (same data snapshot).
"""

from __future__ import annotations

import os
import re

import pytest

from reva.v8.dossier import model as M
from reva.v8.dossier.export import to_html, to_markdown
from reva.v8.dossier.templates import PLACEHOLDER, load_template

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(os.environ.get("REVA_V8_DEVDB") != "1", reason="set REVA_V8_DEVDB=1 to read the dev DB"),
]

SHOWCASE = [
    ("country", "FRA"),
    ("commodity", "crude_oil"),
    ("disease", "malaria"),
    ("drug", "chembl:CHEMBL941"),
    ("conflict", "ucdp:conflict:13243"),
    ("decision", "opec_plus_cut_2mbd_2027q1"),
]


@pytest.fixture(scope="module")
def gen() -> object:
    from reva.adapters.objects.factory import build_store
    from reva.adapters.postgres.engine import make_engine
    from reva.settings import get_settings
    from reva.v8.dossier.generator import generate

    s = get_settings()
    eng = make_engine("api", s)
    store = build_store(s)
    return lambda kind, sid: generate(eng.connect, store, kind, sid)


def _sentences(doc: M.Document) -> list[M.Sentence]:
    out = list(doc.summary)
    for s in doc.sections:
        for b in s.blocks:
            if isinstance(b, M.ParagraphBlock):
                out += b.sentences
            elif isinstance(b, M.ListBlock):
                out += b.items
    return out


@pytest.mark.parametrize(("kind", "sid"), SHOWCASE)
def test_showcase_brief(gen: object, kind: str, sid: str) -> None:
    doc: M.Document = gen(kind, sid)  # type: ignore[operator]
    tpl = load_template(kind)
    assert tpl is not None
    ids = tpl.sentence_ids()
    fns = {f.n: f for f in doc.footnotes}
    assert doc.meta.values >= 40, f"{kind}/{sid}: only {doc.meta.values} values"
    for s in _sentences(doc):
        assert s.template_id in ids
        for p in s.parts:
            if p.t == "value":
                assert p.fn in fns and fns[p.fn].title
            if p.t == "text":
                assert not re.search(r"\d{2,}", p.text) or "CIM-11" in p.text
    for sec in doc.sections:
        for b in sec.blocks:
            if isinstance(b, M.TableBlock):
                for row in b.rows:
                    for c in row:
                        assert all(p.fn in fns for p in c.parts if p.t == "value")
            if isinstance(b, M.ChartBlock):
                assert all(s.fn in fns for s in b.series)
    nums = [
        p for s in _sentences(doc) for p in s.parts if p.t == "value" and p.exact and re.fullmatch(r"-?[\d.]+", p.exact)
    ]
    assert all(fns[p.fn].licence or fns[p.fn].source.startswith("reva") for p in nums if p.fn)
    assert doc.not_covered, "every brief states what it does not cover"
    md, html = to_markdown(doc), to_html(doc)
    assert all(f"[^{f.n}]:" in md and f'id="fn{f.n}"' in html for f in doc.footnotes)
    assert PLACEHOLDER.search(md) is None or "{" not in md.split("## Sources")[0]
