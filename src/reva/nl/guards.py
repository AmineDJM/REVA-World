"""Deterministic server-side checks on the user's text (independent of the LLM).

* :func:`sanitize_text` — Unicode NFC, control and bidi-override characters
  removed, size bound (4096 UTF-8 bytes).
* :func:`keyword_gaps` — French/English patterns for asks that no registered
  model computes (GDP loss, unemployment, inflation, welfare, retirement age,
  population projection, multi-year dynamics), for requests to override data
  values and for instructions addressed to the system. Each hit becomes a typed
  unsupported item *in addition to* whatever the LLM reported, so a model that
  silently drops or approximates such an ask is still caught. A pattern is
  skipped when a registered model provides the capability.
* :func:`stated_numbers` — numbers literally present in the text, used to label
  each number in a draft as stated, converted from a stated number, or not in
  the text (all still need the user's confirmation).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from reva.kernel.errors import RevaError

MAX_TEXT_BYTES = 4096
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f​-‏‪-‮⁠-⁩﻿]")


class TextRejected(RevaError):
    code = "invalid_draft_text"
    http_status = 422


class TextTooLong(RevaError):
    code = "payload_too_large"
    http_status = 413


def sanitize_text(text: object) -> str:
    if not isinstance(text, str):
        raise TextRejected("text must be a string")
    if len(text.encode("utf-8", "surrogatepass")) > MAX_TEXT_BYTES:
        raise TextTooLong(f"text is larger than {MAX_TEXT_BYTES} bytes (UTF-8)", max_bytes=MAX_TEXT_BYTES)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise TextRejected("text is not valid Unicode (lone surrogates)") from None
    clean = _CONTROL.sub(" ", unicodedata.normalize("NFC", text)).strip()
    if not clean:
        raise TextRejected("text is empty")
    return clean


@dataclass(frozen=True)
class GuardRule:
    capability_key: str
    pattern: re.Pattern[str]
    reason_en: str
    reason_fr: str
    provided_by: tuple[str, ...] = ()  # registered model keys that make this rule moot
    # capability_key of a planned-model rule is the model key; the service maps it to the catalogue's gap id


def _rx(p: str) -> re.Pattern[str]:
    return re.compile(p, re.IGNORECASE | re.UNICODE)


RULES: tuple[GuardRule, ...] = (
    GuardRule("outputs.macroeconomic",
              _rx(r"\b(gdp|pib)\b.{0,40}\b(loss|losses|perte|pertes|baisse|drop|fall|recul|impact|effet|effect)"
                  r"|\b(loss|losses|perte|pertes|baisse|recul)\b.{0,20}\b(de |of )?(gdp|pib)\b"
                  r"|\b(croissance|growth|r[ée]cession|recession)\b"),
              "GDP loss, growth or recession effects are not computed by any registered model",
              "La perte de PIB, la croissance ou la récession ne sont calculées par aucun modèle enregistré"),
    GuardRule("outputs.macroeconomic", _rx(r"\b(unemploy\w*|ch[ôo]mage|jobless\w*)"),
              "Unemployment is not computed by any registered model",
              "Le chômage n'est calculé par aucun modèle enregistré"),
    GuardRule("outputs.macroeconomic", _rx(r"\b(inflation|consumer prices?|prix [àa] la consommation|cpi)\b"),
              "Consumer inflation is not computed (at most a listed model's first-round price effect)",
              "L'inflation n'est pas calculée (au plus l'effet prix de premier tour d'un modèle listé)"),
    GuardRule("outputs.macroeconomic", _rx(r"\b(welfare|bien-[êe]tre|pouvoir d'achat|purchasing power)\b"),
              "Household welfare or purchasing power is not computed by any registered model",
              "Le bien-être ou le pouvoir d'achat des ménages n'est calculé par aucun modèle enregistré"),
    GuardRule("social.retirement_age",
              _rx(r"\b(retraite|retirement|pension\w*|[âa]ge l[ée]gal|retire)\b"),
              "No retirement-age model is registered", "Aucun modèle d'âge de départ à la retraite n'est enregistré",
              provided_by=("social.retirement_age",)),
    GuardRule("demography.cohort_component",
              _rx(r"\b(projection (de la |d[ue] )?population|population projection|vieillissement|ageing|aging"
                  r"|fertilit\w*|f[ée]condit[ée]|mortalit\w*|cohort\w*)"),
              "No population projection model is registered", "Aucun modèle de projection démographique n'est "
              "enregistré", provided_by=("demography.cohort_component",)),
    GuardRule("horizon.multi_period",
              _rx(r"\b(over|during|for|sur|pendant)\s+(the next\s+|les\s+)?\d{1,2}\s+(years|ans|ann[ée]es)\b"
                  r"|\b(d'ici|by|until|jusqu'en|jusqu'à)\s+20\d\d\b|\b(long[- ]term|long terme|dynamics?|dynamique)"),
              "Multi-year dynamics are not computed by a single-period model",
              "Les dynamiques pluriannuelles ne sont pas calculées par un modèle à une seule période"),
    GuardRule("nl.input_value_override",
              _rx(r"\b(value|valeur|data|donn[ée]es?|figure|chiffre)s?\b\s*(=|:|of|de|à|a|to|is|est|égale?)?\s*"
                  r"([-+−]?0(?![.,]?\d)|z[ée]ro\b|nulle?\b|missing\b|manquante?s?\b)"
                  r"|\b(treat|traite[rz]?|consid[èe]re[rz]?)\b.{0,30}\b(missing|manquant\w*)\b.{0,20}\b(as|comme)\b"),
              "Data values come only from the pinned baseline release; they cannot be set in a scenario",
              "Les valeurs des données viennent uniquement de la publication de référence ; elles ne peuvent pas "
              "être fixées dans un scénario"),
    GuardRule("nl.instruction_in_text",
              _rx(r"\b(ignore|disregard|forget|oublie[rz]?|ignore[rz]?)\b.{0,40}\b(instructions?|rules?|r[èe]gles?"
                  r"|prompts?|consignes?)\b|\bsystem prompt\b|\byou are now\b|\btu es (maintenant|d[ée]sormais)\b"
                  r"|\bnew instructions?\b|\bnouvelles? instructions?\b|\bdeveloper mode\b"),
              "Instructions addressed to the system were treated as data and not followed",
              "Les instructions adressées au système ont été traitées comme des données et non suivies"),
)


@dataclass(frozen=True)
class GuardHit:
    capability_key: str
    text: str
    reason: str


def keyword_gaps(text: str, *, language: str, registered: Iterable[str],
                 multi_period_available: bool) -> list[GuardHit]:
    regs = set(registered)
    hits: list[GuardHit] = []
    seen: set[tuple[str, str]] = set()
    for rule in RULES:
        if any(k in regs for k in rule.provided_by):
            continue
        if rule.capability_key == "horizon.multi_period" and multi_period_available:
            continue
        m = rule.pattern.search(text)
        if not m:
            continue
        reason = rule.reason_fr if language == "fr" else rule.reason_en
        if (rule.capability_key, reason) in seen:
            continue
        seen.add((rule.capability_key, reason))
        hits.append(GuardHit(rule.capability_key, m.group(0)[:200], reason))
    return hits


_NUMBER = re.compile(r"(?<![\w.,])([-+−]?\d+(?:[.,]\d+)?)\s*(%|pour ?cent|per ?cent|percent)?", re.IGNORECASE)


def stated_numbers(text: str) -> list[tuple[float, bool]]:
    """(value, is_percent) for every number written in the text (French decimal comma accepted)."""
    out = []
    for m in _NUMBER.finditer(text):
        raw = m.group(1).replace("−", "-").replace(",", ".")
        try:
            out.append((float(raw), bool(m.group(2))))
        except ValueError:
            continue
    return out


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


def number_provenance(value: float, numbers: list[tuple[float, bool]]) -> str:
    """``stated`` (literally in the text), ``converted_from_stated`` (a percentage form of a stated number)
    or ``not_in_text``."""
    for n, _pct in numbers:
        if _close(value, n):
            return "stated"
    for n, _pct in numbers:
        if any(_close(value, c) for c in (n / 100, 1 + n / 100, 1 - n / 100, -n / 100)):
            return "converted_from_stated"
    return "not_in_text"
