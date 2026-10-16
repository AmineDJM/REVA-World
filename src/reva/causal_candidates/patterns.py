"""Explicit causal connectives (EN / FR), their direction rules, and negation / hedge cues (agent CAUSCAND).

A connective match never creates a claim; it proposes a *candidate* whose two arguments are then looked
for among the sentence's own wikilinks (:mod:`reva.causal_candidates.extract`).

Direction rules
---------------
* ``cause_left``: the cause is on the left of the connective, the effect on the right
  ("A caused B", "A led to B", "A a provoqué B").
* ``cause_right``: the effect is on the left, the cause on the right ("B was caused by A", "B due to A",
  "B à cause de A", "B est la conséquence de A"). Fronted forms ("Due to A, B ...", "En raison de A, B ...")
  are the same rule with the effect after the fronted phrase (flag ``fronted``).

The direction is always the one *written*; a negated or hedged sentence keeps it and is flagged.

Connective families (the mission list) and the inflections / passives REVA matches for them are listed in
:data:`CONNECTIVES`. Additions beyond the list, kept explicit: EN ``resulted from`` (the reverse of
"resulted in"), FR ``causer`` (the French verb of "caused"), and the passives of listed verbs.
``relation_hint`` maps to CAUSCORE's closed vocabulary: generic connectives -> ``has_cause``, trigger verbs
-> ``precipitated``, "contributed to" -> ``contributing_factor``. It is a hint about the wording only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Direction = Literal["cause_left", "cause_right"]

I = re.IGNORECASE  # noqa: E741


@dataclass(frozen=True)
class Connective:
    id: str
    lang: str
    family: str  # the listed connective this rule belongs to
    pattern: re.Pattern[str]
    direction: Direction
    relation_hint: str
    priority: int = 1  # lower wins when two matches overlap (passives before actives)
    listed: bool = True  # False: an explicit addition beyond the mission list


def _c(
    id_: str,
    lang: str,
    family: str,
    rx: str,
    direction: Direction,
    relation: str = "has_cause",
    *,
    priority: int = 1,
    listed: bool = True,
    flags: int = I,
) -> Connective:
    return Connective(id_, lang, family, re.compile(rx, flags), direction, relation, priority, listed)


_EN_ADV = (
    r"(?:(?:directly|indirectly|eventually|ultimately|largely|partly|partially|mainly|primarily|chiefly|"
    r"mostly|in\s+part|in\s+turn|also|further|soon|quickly|immediately|thus|therefore|subsequently|"
    r"significantly|greatly|heavily|substantially|strongly|directly)\s+)?"
)
_BY = (
    r"(?:(?:in\s+part|partly|largely|mainly|primarily|chiefly|mostly|directly|indirectly|ultimately|partially|"
    r"principally|initially|apparently|reportedly|probably|possibly|allegedly)\s+)?by\b"
)
_FR_AUX = r"(?:a|ont|avait|avaient|aura|auront|aurait|auraient|eut|eurent|ayant)"
_FR_ADV = (
    r"(?:(?:pas|jamais|point|guère|\w+ment|notamment|ainsi|aussi|également|alors|donc|surtout|en\s+partie|indirectement|"
    r"directement|rapidement|finalement|bientôt)\s+)?"
)
_FR_DE = r"(?:de\s+la|de\s+l['’]|des|du|de|d['’])"
_FR_A = r"(?:à\s+la|à\s+l['’]|aux|au|à)"

CONNECTIVES: tuple[Connective, ...] = (
    # ---------------------------------------------------------------- English
    _c("en.caused_by", "en", "caused", r"\bcaused\s+" + _BY, "cause_right", priority=0),
    _c("en.caused", "en", "caused", r"\b(?:caused|causing)\b", "cause_left"),
    _c(
        "en.cause_base",
        "en",
        "caused",
        r"\b(?:did|does|do|would|could|might|may|can|will|to|not)\s+(?:not\s+|never\s+|also\s+|help\s+)?cause\b",
        "cause_left",
    ),
    _c(
        "en.trigger_base",
        "en",
        "triggered",
        r"\b(?:did|does|do|would|could|might|may|can|will|to|not)\s+(?:not\s+|never\s+|also\s+|help\s+)?trigger\b",
        "cause_left",
        "precipitated",
    ),
    _c(
        "en.spark_base",
        "en",
        "sparked",
        r"\b(?:did|does|do|would|could|might|may|can|will|to|not)\s+(?:not\s+|never\s+|also\s+|help\s+)?spark\b",
        "cause_left",
        "precipitated",
    ),
    _c(
        "en.precipitate_base",
        "en",
        "precipitated",
        r"\b(?:did|does|do|would|could|might|may|can|will|to|not)\s+(?:not\s+|never\s+|also\s+|help\s+)?"
        r"precipitate\b",
        "cause_left",
        "precipitated",
    ),
    _c("en.led_to", "en", "led to", r"\b(?:led|leads|lead|leading)\s+" + _EN_ADV + r"to\b", "cause_left"),
    _c(
        "en.resulted_in",
        "en",
        "resulted in",
        r"\b(?:resulted|results|result|resulting)\s+" + _EN_ADV + r"in\b",
        "cause_left",
    ),
    _c(
        "en.resulted_from",
        "en",
        "resulted in",
        r"\b(?:resulted|results|resulting)\s+" + _EN_ADV + r"from\b",
        "cause_right",
        listed=False,
    ),
    _c(
        "en.triggered_by",
        "en",
        "triggered",
        r"\btriggered\s+(?:off\s+)?" + _BY,
        "cause_right",
        "precipitated",
        priority=0,
    ),
    _c("en.triggered", "en", "triggered", r"\b(?:triggered|triggering)\b(?:\s+off\b)?", "cause_left", "precipitated"),
    _c(
        "en.due_to",
        "en",
        "due to",
        r"\bdue\s+(?:(?:\w+ly|in\s+(?:large\s+)?part|in\s+no\s+small\s+part)\s+)?to\b"
        r"(?!\s+(?:be|begin|start|take|end|arrive|expire|return|meet|hold|have|open|close|finish|launch|retire)\b)",
        "cause_right",
    ),
    _c("en.because_of", "en", "because of", r"\bbecause\s+of\b", "cause_right"),
    _c(
        "en.as_a_result_of",
        "en",
        "as a result of",
        r"\bas\s+a\s+(?:(?:direct|indirect|partial|further|consequent)\s+)?result\s+of\b",
        "cause_right",
    ),
    _c("en.sparked_by", "en", "sparked", r"\bsparked\s+(?:off\s+)?" + _BY, "cause_right", "precipitated", priority=0),
    _c("en.sparked", "en", "sparked", r"\b(?:sparked|sparking)\b(?:\s+off\b)?", "cause_left", "precipitated"),
    _c(
        "en.precipitated_by",
        "en",
        "precipitated",
        r"\bprecipitated\s+" + _BY,
        "cause_right",
        "precipitated",
        priority=0,
    ),
    _c("en.precipitated", "en", "precipitated", r"\b(?:precipitated|precipitating)\b", "cause_left", "precipitated"),
    _c(
        "en.contributed_to",
        "en",
        "contributed to",
        r"\b(?:contributed|contributes|contributing|contribute)\s+(?:(?:\w+ly|much|greatly|heavily|in\s+part|in\s+no\s+small\s+"
        r"part|further|also)\s+)?to\b",
        "cause_left",
        "contributing_factor",
    ),
    # ---------------------------------------------------------------- French
    _c(
        "fr.provoque_par",
        "fr",
        "a provoqué",
        r"\bprovoqu(?:é|ée|és|ées)\s+" + _FR_ADV + r"par\b",
        "cause_right",
        priority=0,
    ),
    _c(
        "fr.a_provoque",
        "fr",
        "a provoqué",
        r"\b"
        + _FR_AUX
        + r"\s+"
        + _FR_ADV
        + r"provoqu(?:é|ée|és|ées)\b|\bprovoqu(?:a|èrent|e|ent|ant|era|eront|ait|aient)\b",
        "cause_left",
    ),
    _c(
        "fr.a_entraine",
        "fr",
        "a entraîné",
        r"\b"
        + _FR_AUX
        + r"\s+"
        + _FR_ADV
        + r"entra[îi]n(?:é|ée|és|ées)\b|(?<![sS]['’])\bentra[îi]n(?:a|èrent|e|ent|ant|era|eront|ait|aient)\b",
        "cause_left",
    ),
    _c(
        "fr.declenche_par",
        "fr",
        "a déclenché",
        r"\bdéclench(?:é|ée|és|ées)\s+" + _FR_ADV + r"par\b",
        "cause_right",
        "precipitated",
        priority=0,
    ),
    _c(
        "fr.a_declenche",
        "fr",
        "a déclenché",
        r"\b"
        + _FR_AUX
        + r"\s+"
        + _FR_ADV
        + r"déclench(?:é|ée|és|ées)\b|(?<!\bse\s)(?<![sS]['’])\bdéclench(?:a|èrent|e|ent|ant|era|eront|ait|aient)\b",
        "cause_left",
        "precipitated",
    ),
    _c(
        "fr.a_contribue",
        "fr",
        "a contribué à",
        r"\b(?:"
        + _FR_AUX
        + r"\s+"
        + _FR_ADV
        + r"contribué|contribu(?:a|èrent|e|ent|ant|era|eront|ait|aient))\s+"
        + _FR_ADV
        + _FR_A,
        "cause_left",
        "contributing_factor",
    ),
    _c(
        "fr.est_due_a",
        "fr",
        "est due à",
        r"\b(?:dû|due|dus|dues)\s+(?:(?:\w+ment|en\s+partie|notamment|surtout|avant\s+tout|aussi)\s+)?" + _FR_A,
        "cause_right",
    ),
    _c("fr.a_cause_de", "fr", "à cause de", r"\bà\s+cause\s+" + _FR_DE, "cause_right"),
    _c("fr.en_raison_de", "fr", "en raison de", r"\ben\s+raison\s+" + _FR_DE, "cause_right"),
    _c(
        "fr.consequence_de",
        "fr",
        "conséquence de",
        r"\b(?:(?:est|sont|était|étaient|fut|furent|serait|seraient|apparaît\s+comme|constitue)\s+"
        r"(?:(?:la|une|l['’]une\s+des|les|en\s+partie\s+la|aussi\s+la|donc\s+la)\s+)?|en\s+)"
        r"conséquences?\s+(?:(?:directe|indirecte|directes|indirectes|logique|principale|immédiate)\s+)?" + _FR_DE,
        "cause_right",
    ),
    _c(
        "fr.cause_par",
        "fr",
        "causer",
        r"\bcaus(?:é|ée|és|ées)\s+" + _FR_ADV + r"par\b",
        "cause_right",
        priority=0,
        listed=False,
    ),
    _c(
        "fr.a_cause",
        "fr",
        "causer",
        r"\b" + _FR_AUX + r"\s+" + _FR_ADV + r"caus(?:é|ée|és|ées)\b|\bcaus(?:a|èrent|ant|èrent)\b",
        "cause_left",
        listed=False,
    ),
)

#: Fronted backward connectives ("Due to A, B ...") are recognised when only this precedes them in the sentence.
FRONT_OK = re.compile(
    r"^\s*(?:(?:in|en|by|from|after|after\s+the|vers|dès|au|à\s+partir\s+de)\s+[^,]{1,30},\s*)?"
    r"(?:(?:but|and|however|yet|thus|mais|et|cependant|toutefois|ainsi|partly|largely|"
    r"mainly|en\s+partie|notamment)\s*,?\s*)?$",
    I,
)

# ----------------------------------------------------------------------------- negation and hedges

NEGATION = {
    "en": re.compile(
        r"\b(?:not(?!\s+only\b)|never|no\s+longer|neither|nor|without|no\s+(?:evidence|link|connection|"
        r"role|direct)|did\s+not|was\s+not|were\s+not|denied|denies|deny|rejected|disproved)\b|n['’]t\b",
        I,
    ),
    "fr": re.compile(
        r"\b(?:ne|n['’](?!\w*\s+seulement)|jamais|aucun|aucune|nullement|guère|sans|nié|niée|nient|"
        r"réfute\w*|dément\w*)\b|\bpas\b(?!\s+seulement)|\bnon\b(?!\s+seulement)",
        I,
    ),
}

HEDGE = {
    "en": re.compile(
        r"\b(?:might|could)\b|\b(?-i:may)\b(?!\s+\d)|\b(?:possibly|probably|perhaps|likely|unlikely|"
        r"allegedly|reportedly|arguably|supposedly|presumably|purportedly|apparently|seemingly)\b|"
        r"\b(?:historians?|scholars?|some|many|others|critics|researchers|economists|analysts|commentators|authors?)"
        r"\s+(?:have\s+|had\s+|also\s+)?(?:argue|argued|argues|believe|believed|believes|claim|claimed|claims|"
        r"suggest|suggested|suggests|debate|debated|contend|contended|contends|disagree|disagreed|dispute|disputed|"
        r"maintain|maintained|speculate|speculated|hypothesi[sz]ed?|posit|posited|attribute|attributed|blame|blamed)\b|"
        r"\bit\s+(?:is|was|has\s+been|had\s+been)\s+(?:widely\s+|often\s+|also\s+|sometimes\s+)?(?:argued|claimed|"
        r"suggested|thought|believed|speculated|alleged|said|debated|disputed|theorized|theorised|hypothesized)\b|"
        r"\b(?:debate|debated|disputed|controvers\w*|speculat\w*|hypothes\w*|theor(?:y|ies|ized|ised)|unclear|"
        r"uncertain|contested|questionable)\b|\b(?:is|are|was|were)\s+(?:said|thought|believed|alleged|claimed|"
        r"suspected)\s+to\b|\baccording\s+to\s+(?:some|one|several|certain|other)\b|\bwould\s+have\b|\bif\b",
        I,
    ),
    "fr": re.compile(
        r"\b(?:aurait|auraient|pourrait|pourraient|semble|semblent|semblerait|sembleraient|paraît|paraîtrait)\b|"
        r"\bpeut[- ]être\b|\b(?:probablement|vraisemblablement|possiblement|supposément|prétendument|apparemment|"
        r"hypothétiquement)\b|\bsans\s+doute\b|\bselon\s+(?:certains|d['’]autres|plusieurs|quelques|une\s+hypothèse)\b|"
        r"\b(?:certains|plusieurs|de\s+nombreux|d['’]autres|les|des)\s+(?:historiens|chercheurs|spécialistes|"
        r"auteurs|économistes)\b|\b(?:débat\w*|controvers\w*|contest\w*|discut\w*|hypoth\w*|incertain\w*|théorie\w*|"
        r"spécul\w*)\b|\bil\s+est\s+possible\b|\bon\s+(?:pense|estime|suppose|considère|croit|attribue)\b|"
        r"\bserait\s+(?:dû|due|dus|dues|la\s+conséquence)\b|\bsi\b",
        I,
    ),
}

#: Words that end a clause on either side of the connective (an argument never crosses them).
CLAUSE_BREAK = {
    "en": re.compile(r"[;:()\[\]]|\s[–—]\s|\b(?:but|while|whereas|although|though|however|because(?!\s+of))\b", I),
    "fr": re.compile(
        r"[;:()\[\]]|\s[–—]\s|\b(?:mais|tandis|alors\s+que|bien\s+que|quoique|cependant|toutefois|"
        r"parce\s+que|puisque)\b",
        I,
    ),
}

#: A subject pronoun or demonstrative between the left argument and the connective makes the subject
#: anaphoric ("..., and this led to"): no left argument is taken.
ANAPHORA = {
    "en": re.compile(
        r"\b(?:this|these|that|those|it|they|he|she|which(?=\s+(?:in\s+turn\s+)?\w+\s*$)|"
        r"who|whom)\b",
        I,
    ),
    "fr": re.compile(
        r"\b(?:cela|ceci|ce|celle-ci|celui-ci|ceux-ci|celles-ci|il|elle|ils|elles|lequel|laquelle|"
        r"lesquels|lesquelles)\b",
        I,
    ),
}

#: Relative pronoun bridges (", which led to", ", qui a provoqué") are allowed: the antecedent is the link
#: right before them (flag ``relative``).
RELATIVE = {
    "en": re.compile(
        r"^\s*,?\s*(?:which|that)\s+(?:(?:in\s+turn|eventually|ultimately|also|then|later|soon|itself|"
        r"subsequently|quickly|directly|indirectly)\s+)?(?:(?:has|had|have|would|will)\s+)?$",
        I,
    ),
    "fr": re.compile(
        r"^\s*,?\s*(?:qui|ce\s+qui)\s+(?:(?:à\s+son\s+tour|elle-même|lui-même|ensuite|aussi|alors)\s+)?$", I
    ),
}

#: Between two links, these gaps attach the right link to the left one's noun phrase ("[[A]] of [[B]]",
#: "[[A]] in [[B]]"): the left link is the head of the argument.
PP_GAP = {
    "en": re.compile(
        r"^\s*(?:[,]?\s*(?:\w+\s+){0,2})?(?:of|in|at|on|from|with|against|between|for|over|under|"
        r"into|near|across|toward|towards|during|within|throughout|led\s+by|ruled\s+by)\s+"
        r"(?:the\s+|a\s+|an\s+|its\s+|their\s+|his\s+|her\s+)?$",
        I,
    ),
    "fr": re.compile(
        r"^\s*(?:(?:\w+\s+){0,2})?(?:de|du|des|d['’]|en|à|au|aux|contre|entre|par|sur|dans|pour|sous|"
        r"vers|chez|avec|pendant|menée?s?\s+par)\s*(?:la\s+|le\s+|les\s+|l['’]|un\s+|une\s+|son\s+|sa\s+|"
        r"ses\s+|leur\s+|leurs\s+)?$",
        I,
    ),
}

#: Coordination gaps: both links are arguments ("[[A]] and [[B]] led to ...").
COORD_GAP = {
    "en": re.compile(r"^\s*,?\s*(?:and|or|as\s+well\s+as)\s*(?:the\s+|a\s+|an\s+)?$", I),
    "fr": re.compile(r"^\s*,?\s*(?:et|ou|ainsi\s+que)\s*(?:la\s+|le\s+|les\s+|l['’]|un\s+|une\s+)?$", I),
}
#: A bare comma coordinates only inside a list closed by "and"/"or" ("[[A]], [[B]] and [[C]]").
LIST_COMMA_GAP = {
    "en": re.compile(r"^\s*,\s*(?:the\s+|a\s+|an\s+)?$", I),
    "fr": re.compile(r"^\s*,\s*(?:la\s+|le\s+|les\s+|l['’]|un\s+|une\s+)?$", I),
}

#: Right after the connective, what may precede the argument's link (determiners, adjectives, a head noun
#: + preposition). A gap with a verb marker means the link is inside another clause: no argument.
RIGHT_GAP_STOP = {
    "en": re.compile(
        r"[,;:.()]|\b(?:to\s+\w+|that|which|who|whom|when|where|whose|because|while|and|or|but)\b|"
        r"\bto\s*$",
        I,
    ),
    "fr": re.compile(r"[,;:.()]|\b(?:que|qui|dont|où|quand|lorsque|pour|afin|et|ou|mais)\b", I),
}

#: The unlinked head noun of an argument whose link is a modifier ("the collapse of the [[Soviet Union]]"):
#: ``(head) of (det)`` at the end of the gap before the link.
MODIFIER_HEAD = {
    "en": re.compile(
        r"\b([a-z][a-z-]{2,})\s+(?:of|in|on|against|between|from|over|with|to|for|during|after|before|under|at|within|"
        r"through|into|across|amid|following|among)\s+"
        r"(?:the\s+|a\s+|an\s+|its\s+|their\s+|his\s+|her\s+)?(?:[\w.’'-]+\s+){0,3}$"
    ),
    "fr": re.compile(
        r"\b([a-zà-ÿ][a-zà-ÿ-]{2,})\s+(?:de|du|des|d['’]|en|contre|par|sur|entre|à|au|aux|avec|pour)\s*"
        r"(?:la\s+|le\s+|les\s+|l['’]|un\s+|une\s+|son\s+|sa\s+|ses\s+|leur\s+|leurs\s+)?"
        r"(?:[\w.’'-]+\s+){0,3}$"
    ),
}
NOT_HEADS = {
    "en": {
        "the",
        "and",
        "all",
        "most",
        "many",
        "some",
        "part",
        "parts",
        "one",
        "two",
        "three",
        "each",
        "both",
        "several",
        "much",
        "any",
        "none",
        "rest",
        "number",
        "series",
        "lot",
        "lots",
        "half",
        "out",
        "because",
        "result",
        "instead",
        "spite",
        "terms",
        "behalf",
        "favor",
        "favour",
        "front",
        "led",
        "due",
        "ruled",
    },
    "fr": {
        "les",
        "des",
        "une",
        "partie",
        "plupart",
        "ensemble",
        "nombre",
        "série",
        "cause",
        "raison",
        "suite",
        "lors",
        "face",
        "près",
        "loin",
        "afin",
        "auprès",
        "faveur",
        "menée",
        "mené",
        "menées",
        "menés",
        "dû",
        "due",
        "dus",
        "dues",
        "conséquence",
        "conséquences",
    },
}

#: Heads that do not change what the linked entity denotes ("the outbreak of [[World War I]]"): the link is
#: the argument itself. Other heads make the link a modifier (flag ``modifier_head``).
TRANSPARENT_HEADS = {
    "en": {"outbreak", "start", "beginning", "onset", "eruption", "escalation", "launch", "launching"},
    "fr": {"déclenchement", "début", "commencement", "éclatement", "escalade", "lancement"},
}

#: For effect-on-the-left connectives ("X due to Y", "X was caused by Y", "X à cause de Y"), only a copula /
#: auxiliary and adverbs may separate the effect's link from the connective: with a verb in between
#: ("the Court annulled the result due to ...") the effect is the predicate, not the linked subject.
BACKWARD_LEFT_GAP = {
    "en": re.compile(
        r"^\s*,?\s*(?:(?:which|that)\s+)?(?:(?:may|might|can|could|would|will|must|is|are|was|were|be|been|"
        r"being|has|have|had|not|never|also|often|sometimes|probably|possibly|reportedly|allegedly|"
        r"largely|mainly|partly|primarily|mostly|chiefly|partially|in\s+part|apparently|thought\s+to|"
        r"said\s+to|believed\s+to|\w+ly)\s+)*$",
        I,
    ),
    "fr": re.compile(
        r"^\s*,?\s*(?:(?:qui|ce\s+qui)\s+)?(?:(?:est|sont|était|étaient|fut|furent|a|ont|avait|avaient|été|"
        r"serait|seraient|sera|seront|peut|peuvent|pourrait|semble|semblent|être|ne|n['’]|pas|aussi|"
        r"notamment|surtout|en\s+partie|largement|probablement|sans\s+doute|\w+ment)\s*)*$",
        I,
    ),
}

#: English: what may follow an argument's link. Anything else (a noun: "[[supply chain]] limitations",
#: a possessive: "[[Germany]]'s defeat") means the link only modifies the argument.
EN_FOLLOWERS = frozenset(
    """
in on at of by for from with during after before and or but nor which that who whom whose where when as to
between against among across through throughout under over until till since while whereas despite into onto
upon within without beyond toward towards around near via per amid amidst along following including such
than so then also later soon eventually ultimately thus therefore however itself themselves
is are was were be been being has have had would will could may might can must should did does do not
led leads lead caused causes causing resulted results resulting triggered triggering sparked sparking
precipitated precipitating contributed contributes contributing due because
""".split()
)
