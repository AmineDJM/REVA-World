"""Optional LLM rephrasing of the composed answer, accepted only if it is *faithful*.

The model rewrites each template sentence (same ids). :func:`check_faithful` then verifies,
sentence by sentence, against the template sentence and the evidence it cites:

* every number (including years and date parts) in the rephrased sentence appears in the
  template sentence or its cited evidence, and every number of the template sentence is kept;
* every capitalised word (names, labels) not at the start of the sentence appears in the
  template sentence or its cited evidence;
* no causal wording ("because", "à cause de", "led to", …) unless the template sentence has it;
* the honesty markers of the template sentence ("selon", "Wikidata", "pas une cause", "aucune
  source", "community-edited", …) are kept;
* the length stays bounded and the ids match one to one.

If any sentence fails, the whole rephrase is discarded and the template text is returned with
a warning naming the reasons. The check is lexical and conservative: it may refuse a correct
paraphrase, it must not accept an added number, date or name.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from reva.nlqa.catalog import fold
from reva.nlqa.compose import Sentence

_NUM = re.compile(r"\d+(?:[   ]\d{3})*(?:[.,]\d+)*")
_WORD = re.compile(r"[^\W\d_][\w'’-]*", re.UNICODE)
CAUSAL = re.compile(
    r"\b(because|caused?|causing|due to|led to|leads? to|result(?:ed|s)? in|therefore|thus|hence|owing to|"
    r"à cause|a cause|en raison|cause[rs]?|causé|provoqu\w*|entraîn\w*|conduit|grâce à|donc|par conséquent|"
    r"explique|expliqu\w*|origine de)\b",
    re.IGNORECASE,
)
KEEP_MARKERS = (
    # attribution and status
    "selon",
    "according to",
    "wikidata",
    "communautaire",
    "community-edited",
    "affirmée par",
    "asserted by",
    "source",
    "≈",
    # connection, not cause
    "relié",
    "connected",
    "pas une cause",
    "not a cause",
    # negation / absence (a dropped negation flips the meaning without changing any number)
    "pas",
    "not",
    "no",
    "aucun",
    "aucune",
    "sans",
    "without",
    "jamais",
    "never",
    "ni",
    "nor",
    "indisponible",
    "unavailable",
)
_STOP_CAPS = {
    "selon",
    "according",
    "étape",
    "hop",
    "autre",
    "another",
    "other",
    "entité",
    "entity",
    "aucune",
    "aucun",
    "no",
    "the",
    "le",
    "la",
    "les",
    "dernière",
    "latest",
    "classement",
    "ranking",
    "indisponible",
    "unavailable",
    "non",
    "not",
    "cette",
    "this",
    "ce",
    "il",
    "elle",
    "en",
    "in",
    "de",
    "of",
    "values",
    "valeurs",
}


def numbers(text: str) -> set[str]:
    """Number tokens with grouping spaces removed (``26 337,5`` → ``26337,5``)."""
    out = set()
    for m in _NUM.finditer(text):
        tok = re.sub(r"[   ]", "", m.group(0))
        out.add(tok)
        # an English grouping comma ("26,337") and a bare number are the same value
        if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", tok):
            out.add(tok.replace(",", ""))
        for part in re.split(r"[.,]", tok):
            if part:
                out.add(part.lstrip("0") or "0")
    return out


def _numbers_strict(text: str) -> set[str]:
    return {re.sub(r"[   ]", "", m.group(0)) for m in _NUM.finditer(text)}


def capitalised(text: str) -> set[str]:
    out = set()
    for sent in re.split(r"(?<=[.:;!?])\s+", text):
        words = _WORD.findall(sent)
        for i, w in enumerate(words):
            if i == 0 or not w[:1].isupper():
                continue
            if fold(w) in _STOP_CAPS:
                continue
            out.add(fold(w))
    return out


def evidence_text(items: Sequence[Mapping[str, Any]]) -> str:
    return json.dumps(list(items), ensure_ascii=False, default=str)


@dataclass
class FaithReport:
    ok: bool
    problems: list[dict[str, Any]] = field(default_factory=list)


def check_sentence(original: str, rephrased: str, evidence: str) -> list[str]:
    problems: list[str] = []
    allowed_nums = numbers(original) | numbers(evidence)
    for n in _numbers_strict(rephrased):
        if n not in allowed_nums and n.replace(",", "") not in allowed_nums:
            problems.append(f"number_not_in_evidence:{n}")
    reph_nums = numbers(rephrased)
    for n in _numbers_strict(original):
        if n not in reph_nums and n.replace(",", "") not in reph_nums:
            problems.append(f"number_dropped:{n}")
    allowed_words = {fold(w) for w in _WORD.findall(original + " " + evidence)}
    for w in capitalised(rephrased):
        if w not in allowed_words:
            problems.append(f"name_not_in_evidence:{w}")
    if CAUSAL.search(rephrased) and not CAUSAL.search(original):
        problems.append("causal_wording_added")
    fo, fr_ = fold(original), fold(rephrased)
    for marker in KEEP_MARKERS:
        rx = re.compile(r"(?<!\w)" + re.escape(fold(marker).strip()) + r"(?!\w)")
        if rx.search(fo) and not rx.search(fr_):
            problems.append(f"marker_dropped:{marker.strip()}")
    if len(rephrased) > 2 * len(original) + 60:
        problems.append("too_long")
    if not rephrased.strip():
        problems.append("empty")
    return problems


def check_faithful(
    sentences: Sequence[Sentence], rephrased: Any, evidence: Mapping[str, Mapping[str, Any]]
) -> tuple[FaithReport, dict[str, str]]:
    """Validate the model output ``{"sentences": [{id, text}]}``; returns (report, id → accepted text)."""
    report = FaithReport(ok=True)
    out: dict[str, str] = {}
    items = (rephrased or {}).get("sentences") if isinstance(rephrased, Mapping) else None
    if not isinstance(items, list):
        return FaithReport(False, [{"id": None, "problems": ["not_a_sentence_list"]}]), {}
    by_id = {str(i.get("id")): str(i.get("text") or "") for i in items if isinstance(i, Mapping)}
    expected = [s.id for s in sentences]
    if sorted(by_id) != sorted(expected) or len(items) != len(expected):
        return FaithReport(False, [{"id": None, "problems": ["ids_do_not_match"]}]), {}
    for s in sentences:
        ev = evidence_text([evidence[c] for c in s.citations if c in evidence])
        problems = check_sentence(s.text, by_id[s.id], ev)
        if problems:
            report.ok = False
            report.problems.append({"id": s.id, "problems": problems[:10]})
        out[s.id] = by_id[s.id]
    return report, out


def rephrase_payload(language: str, sentences: Sequence[Sentence]) -> str:
    return json.dumps(
        {"language": language, "sentences": [{"id": s.id, "text": s.text} for s in sentences]}, ensure_ascii=False
    )
