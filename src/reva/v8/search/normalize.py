"""Text normalisation for the V8 search index (accent-insensitive, case-insensitive, punctuation-light)."""

from __future__ import annotations

import re
import unicodedata

_SEP = re.compile(r"[^0-9a-z]+")
_CODE_SPACE = re.compile(r"\s+")
# ligatures and letters that NFKD does not decompose into ASCII
_EXTRA = str.maketrans({"œ": "oe", "æ": "ae", "ß": "ss", "ø": "o", "đ": "d", "ł": "l", "ı": "i", "þ": "th"})


def fold(s: str) -> str:
    """Lower-case, strip accents (``Pétrole`` → ``petrole``, ``Œuvre`` → ``oeuvre``)."""
    s = unicodedata.normalize("NFKD", s.casefold().translate(_EXTRA))
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def norm_text(s: str) -> str:
    """Folded text, every run of non-alphanumerics → one space (``Côte d'Ivoire`` → ``cote d ivoire``)."""
    return _SEP.sub(" ", fold(s)).strip()


def norm_code(s: str) -> str:
    """Identifier form: folded, whitespace removed, punctuation kept (``HGNC: 1100`` → ``hgnc:1100``)."""
    return _CODE_SPACE.sub("", fold(s)).strip()


def words(s: str) -> list[str]:
    return [w for w in norm_text(s).split(" ") if w]


def trigrams(s: str) -> set[str]:
    """Character trigrams of the normalised text padded with spaces (word boundaries count)."""
    t = f"  {norm_text(s)} "
    return {t[i : i + 3] for i in range(len(t) - 2)}
