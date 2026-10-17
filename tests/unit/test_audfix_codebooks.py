"""Codebook quotes are verbatim in the archived codebook bytes, at the declared page / paragraph (AUDFIX, audit M4).

The quotes the T1 claims carry (``Codebook.definitions``) must be found, whitespace-normalised only, in the pinned
codebook files -- the same bytes the connectors fetch, archive and compare by sha256. The captured fixture of each
codebook is that file, byte for byte. PDF text is extracted with poppler's ``pdftotext`` (skipped when the binary is
not installed); docx text from ``word/document.xml`` (always checked).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from reva.adapters.connectors import atlas_massmob as M
from reva.adapters.connectors import atlas_navco as N
from reva.evidence.runner import _fixture_fingerprint

REPO = Path(__file__).resolve().parents[2]
FIX = REPO / "tests/fixtures/source"
PDFTOTEXT = shutil.which("pdftotext")
#: PDF ligature glyphs some codebooks map to these code points in their text layer (TIES manual)
LIGATURES = {"…": "fi", "¤": "ff"}


def _norm(s: str) -> str:
    s = re.sub(r"-\n\s*", "-", s)
    return re.sub(r"\s+", " ", s).strip()


def _fixture(connector: str, url: str) -> bytes:
    fp = _fixture_fingerprint(url)
    for e in json.loads((FIX / connector / "manifest.json").read_text()):
        if e["request_fingerprint"] == fp:
            return (FIX / connector / e["file"]).read_bytes()
    raise AssertionError(f"no fixture for {url}")


def _pdf_pages(body: bytes, tmp: Path, layout: bool) -> list[str]:
    assert PDFTOTEXT is not None
    f = tmp / "cb.pdf"
    f.write_bytes(body)
    out = subprocess.run(
        [PDFTOTEXT, *(["-layout"] if layout else []), str(f), "-"], capture_output=True, check=True
    ).stdout.decode("utf-8")
    pages = []
    for p in out.split("\f"):
        lines = [ln for ln in p.split("\n") if not re.fullmatch(r"\s*\d{1,3}\s*", ln)]
        pages.append("\n".join(lines))
    return pages


def _last_column(pages: list[str], min_col: int = 30) -> list[str]:
    """Table layouts (NAVCO 1.2): keep the text of the last column of each line."""
    out = []
    for p in pages:
        keep = []
        for ln in p.split("\n"):
            lead = len(ln) - len(ln.lstrip())
            m = None
            for m in re.finditer(r"\s{2,}(?=\S)", ln):
                pass
            if lead >= min_col:
                keep.append(ln.strip())
            elif m is not None and m.end() >= min_col:
                keep.append(ln[m.end() :].strip())
        out.append("\n".join(keep))
    return out


def _docx_paragraphs(body: bytes, tmp: Path) -> list[str]:
    f = tmp / "cb.docx"
    f.write_bytes(body)
    with zipfile.ZipFile(f) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    paras = []
    for p in re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S):
        t = "".join(re.findall(r"<w:t(?: [^>]*)?>(.*?)</w:t>", p, flags=re.S))
        for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"), ("&amp;", "&")):
            t = t.replace(a, b)
        paras.append(_norm(t))
    return paras


def _check(
    body: bytes, tmp: Path, quotes: Mapping[str, str], where: Mapping[str, Mapping[str, Any]], docx: bool
) -> None:
    if docx:
        paras = _docx_paragraphs(body, tmp)
        for term, q in quotes.items():
            i = where[term]["paragraph"]
            assert _norm(q) in paras[i - 1], (term, paras[i - 1][:200])
        return
    if PDFTOTEXT is None:
        pytest.skip("poppler pdftotext not installed")
    layout = _pdf_pages(body, tmp, True)
    modes = [layout, _pdf_pages(body, tmp, False), _last_column(layout)]
    for term, q in quotes.items():
        pages = where[term]["pages"]
        ok = False
        for units in modes:
            text = _norm(" ".join(units[p - 1] for p in pages))
            for a, b in LIGATURES.items():
                text = text.replace(a, b)
            ok = ok or _norm(q) in text
        assert ok, (term, pages)


CONTENTION = [
    ("atlas.navco", N.FILES["n12"], N.CODEBOOK["n12"]),
    ("atlas.navco", N.FILES["n13"], N.CODEBOOK["n13"]),
    ("atlas.navco", N.FILES["n21"], N.CODEBOOK["n21"]),
    ("atlas.mec", N.FILES["mec"], N.CODEBOOK["mec"]),
    ("atlas.massmob", M.FILE, M.MANUAL),
]


@pytest.mark.parametrize(("connector", "f", "quotes"), CONTENTION, ids=[c[1].key for c in CONTENTION])
def test_contention_quotes_are_verbatim_in_the_archived_codebook(
    connector: str, f: Any, quotes: Mapping[str, str], tmp_path: Path
) -> None:
    book = N.pinned_codebook(f)
    body = _fixture(connector, book.url)
    assert hashlib.sha256(body).hexdigest() == f.codebook_sha256 == book.sha256
    assert set(book.quotes) == set(quotes)  # every quoted term is located
    _check(body, tmp_path, quotes, book.quotes, f.codebook_name.endswith(".docx"))


def test_navco21_repression_levels_are_verbatim(tmp_path: Path) -> None:
    body = _fixture("atlas.navco", N.pinned_codebook(N.FILES["n21"]).url)
    quotes = {k: v for k, v in N.N21_REPRESSION_DEF.items()}
    _check(body, tmp_path, quotes, {k: {"pages": N.N21_REPRESSION_DEF_PAGES} for k in quotes}, False)


def test_esrb_annex_a_quote_is_verbatim(tmp_path: Path) -> None:
    from reva.adapters.connectors import atlas_ecrises as E

    book = E.esrb_codebook()
    body = _fixture("atlas.ecrises.esrb", book.url)
    assert hashlib.sha256(body).hexdigest() == E.ESRB_OP13_SHA256 == book.sha256
    _check(body, tmp_path, dict(E.CODEBOOKS[E.ESRB]["definitions"]), book.quotes, False)


def test_unshipped_codebooks_are_pinned_and_located() -> None:
    """TIES and Archigos codebooks grant no redistribution: no fixture; their quotes were checked against the same
    pinned bytes (docs/dev/v3_audfix.md) and every quoted term is located."""
    from reva.adapters.connectors import atlas_leaders as L
    from reva.adapters.connectors import atlas_sanctions as S

    t = S.ties_manual()
    assert t.sha256 in str(S.TIES_CODEBOOK["version"]) and set(t.quotes) == set(S.TIES_CODEBOOK["definitions"])
    a = L.archigos_codebook()
    assert a.sha256 == L.ARCHIGOS_CODEBOOK_SHA256 and set(a.quotes) == set(L.CODEBOOK_DEFINITIONS)
