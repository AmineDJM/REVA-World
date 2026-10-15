"""Server-side cross-checks between the user's text and the drafted spec (independent of the LLM).

The keyword guards in :mod:`reva.nl.guards` add gaps for asks no model computes. The checks here
compare what the *text* says with what the *draft* does, for cases where a valid spec can still
describe a different scenario than the one asked (scientific review V2, findings H2, H3, M6):

* **Geography** (every model): an exclusion ("tous les pays sauf la Chine", "all countries except
  China") cannot be expressed by any scenario schema (there is no exclusion list), so it always
  blocks the draft (``needs_input``); a named region or country group with ``countries ["ALL"]``
  contradicts the draft and blocks it; with an explicit country list the server cannot verify the
  membership and warns. An LLM-reported ``geography.*`` gap also blocks.
* **economic.io_price**: every shock origin must be stated in the text (never defaulted to the
  reported country); a world-wide price change cannot be drafted (it needs a shock in every FIGARO
  economy); the chosen industry is compared with the words used (``B`` is all of mining and
  quarrying, ``C19`` is refined products, not crude oil, ``D35`` is electricity *and* gas supply).
* **social.retirement_age**: a stated age *level* ("passe à 65 ans", "raise to 66") is never
  accepted as a shift in months; the draft asks instead (or uses a target-age intervention when
  the registered model offers one).

All checks are lexical (French and English patterns, documented as such). A miss leaves the draft
to the user's review of the server restatement; a false hit only asks a question. A blocking
finding turns a ``draft_ready`` (or ``draft_invalid``) draft into ``needs_input``: the draft spec
is withheld and the question is put in the server restatement.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Finding:
    """One server cross-check result. Every text here is written by the server, never by the LLM."""

    capability_key: str
    reason_en: str
    reason_fr: str
    block: bool  # True: the draft is withheld (needs_input) until the user answers
    field: str | None = None  # spec field the question is about (listed in missing_fields when blocking)
    excerpt: str = ""  # the matched words of the user's own text (shown quoted, never LLM text)
    warning: str | None = None  # warning code + text (non-blocking findings)

    def reason(self, language: str) -> str:
        return self.reason_fr if language == "fr" else self.reason_en


def _rx(p: str, flags: int = re.IGNORECASE) -> re.Pattern[str]:
    return re.compile(p, flags | re.UNICODE)


# --------------------------------------------------------------------------- geography
_COLLECTION = (
    r"(?:pays|countr(?:y|ies)|nations?|[ée]conomies?|econom(?:y|ies)|monde|world|partout|everywhere"
    r"|r[ée]gions?|[ée]tats?(?: membres)?|states|members?|membres?|producteurs?|producers?|exportateurs?"
    r"|exporters?|importateurs?|importers?)"
)
_EXCLUDE = (
    r"(?:sauf|hormis|hors|except[ée]?s?|exception(?:\s+(?:de|du|des|d'))?|à l'exception|a l'exception"
    r"|à part|a part|mis à part|en dehors|autres? que|excluding|except(?:\s+for)?|exclud\w*|exclu\w*"
    r"|other than|apart from|but not)"
)
#: an exclusion attached to a set of countries ("tous les pays sauf ...", "all countries except ...")
EXCLUSION_SCOPED = _rx(_COLLECTION + r"\b[^.;:!?]{0,60}?\b" + _EXCLUDE + r"\b")
#: any exclusion word (used only when the draft says countries ["ALL"])
EXCLUSION_ANY = _rx(
    r"\b(?:sauf|hormis|à l'exception|a l'exception|mis à part|en dehors d[eu']|autres? que"
    r"|excluding|except|exclud\w*|exclu\w*|other than|apart from|but not)\b"
)
#: named regions / country groups (no reviewed membership list exists for drafting)
REGION = _rx(
    r"\b(?:europ\w*|union europ[ée]enne|european union|zone euro|euro[- ]?area|eurozone|ocde|oecd"
    r"|opep|opec|brics|afrique(?! du sud)|(?<!south )african?\b|asi[ae]\b|asiatique\w*|asian?\b"
    r"|am[ée]rique (?:latine|du sud|du nord|centrale)|latin america|south america|north america"
    r"|moyen[- ]orient|middle east|proche[- ]orient|golfe|gulf|maghreb|scandinav\w*|nordi(?:c|que)s?"
    r"|balkans?|baltes?|baltic|oc[ée]anie|oceania|pays (?:[ée]mergents|d[ée]velopp[ée]s|riches|pauvres)"
    r"|emerging|developed countries|developing countries)\b"
)
REGION_CASED = _rx(r"\b(?:UE|EU|G7|G8|G20|EEE|EEA|AELE|EFTA)\b", 0)


def _excerpt(m: re.Match[str] | None) -> str:
    return m.group(0)[:120] if m else ""


def geography_findings(text: str, countries: Sequence[str], llm_geography_gap: bool) -> list[Finding]:
    out: list[Finding] = []
    all_countries = "ALL" in countries
    scoped = EXCLUSION_SCOPED.search(text)
    loose = EXCLUSION_ANY.search(text) if all_countries else None
    if scoped or loose:
        out.append(
            Finding(
                "geography.exclusion_not_supported",
                "The text excludes countries from a set; a scenario cannot express an exclusion (there is no "
                "exclusion list), so the draft is withheld. List the countries to include explicitly.",
                "Le texte exclut des pays d'un ensemble ; un scénario ne peut pas exprimer d'exclusion (il n'existe "
                "pas de liste d'exclusion) : le brouillon est retenu. Indiquez explicitement les pays à inclure.",
                block=True,
                field="countries",
                excerpt=_excerpt(scoped or loose),
            )
        )
    region = REGION.search(text) or REGION_CASED.search(text)
    if region:
        if all_countries:
            out.append(
                Finding(
                    "geography.region_not_resolved",
                    "The text names a region or country group but the draft covers all countries; REVA has no "
                    "reviewed membership list for drafting. List the countries explicitly.",
                    "Le texte nomme une région ou un groupe de pays mais le brouillon couvre tous les pays ; REVA n'a "
                    "pas de liste d'appartenance vérifiée pour la rédaction. Indiquez explicitement les pays.",
                    block=True,
                    field="countries",
                    excerpt=_excerpt(region),
                )
            )
        else:
            out.append(
                Finding(
                    "geography.region_not_resolved",
                    "The text names a region or country group; the server cannot verify that the listed countries "
                    "are exactly its members. Check the country list.",
                    "Le texte nomme une région ou un groupe de pays ; le serveur ne peut pas vérifier que les pays "
                    "listés en sont exactement les membres. Vérifiez la liste des pays.",
                    block=False,
                    field="countries",
                    excerpt=_excerpt(region),
                    warning="geography_qualifier_unverified: the text names a region or country group; check that "
                    "the listed countries are exactly the ones meant",
                )
            )
    if llm_geography_gap and not any(f.block for f in out):
        out.append(
            Finding(
                "geography.not_covered",
                "The drafting model reported that part of the requested geography is not reflected in the draft; "
                "the draft is withheld. Restate the countries.",
                "Le modèle de rédaction signale qu'une partie de la géographie demandée n'est pas reflétée dans le "
                "brouillon ; le brouillon est retenu. Reformulez les pays.",
                block=True,
                field="countries",
            )
        )
    return out


# --------------------------------------------------------------------------- economic.io_price
#: Names (EN/FR) of the FIGARO economies, for "is this origin stated?" (regex fragments, case-insensitive).
ECONOMY_NAMES: Mapping[str, str] = {
    "BEL": r"belgium|belgique",
    "BGR": r"bulgaria|bulgarie",
    "CZE": r"czechia|czech republic|tch[èe]quie"
    r"|r[ée]publique tch[èe]que",
    "DNK": r"denmark|danemark",
    "DEU": r"germany|allemagne",
    "EST": r"estonia|estonie",
    "IRL": r"ireland|irlande",
    "GRC": r"greece|gr[èe]ce",
    "ESP": r"spain|espagne",
    "FRA": r"france",
    "HRV": r"croatia|croatie",
    "ITA": r"italy|italie",
    "CYP": r"cyprus|chypre",
    "LVA": r"latvia|lettonie",
    "LTU": r"lithuania|lituanie",
    "LUX": r"luxembourg",
    "HUN": r"hungary|hongrie",
    "MLT": r"malta|malte",
    "NLD": r"netherlands|the netherlands|pays-bas|holland|hollande",
    "AUT": r"austria|autriche",
    "POL": r"poland|pologne",
    "PRT": r"portugal",
    "ROU": r"romania|roumanie",
    "SVN": r"slovenia|slov[ée]nie",
    "SVK": r"slovakia|slovaquie",
    "FIN": r"finland|finlande",
    "SWE": r"sweden|su[èe]de",
    "NOR": r"norway|norv[èe]ge",
    "CHE": r"switzerland|suisse",
    "GBR": r"united kingdom|royaume-uni|great britain|britain|grande-bretagne"
    r"|england|angleterre",
    "MNE": r"montenegro|mont[ée]n[ée]gro",
    "MKD": r"north macedonia|macedonia"
    r"|mac[ée]doine du nord|mac[ée]doine",
    "ALB": r"albania|albanie",
    "SRB": r"serbia|serbie",
    "TUR": r"turkey|t[üu]rkiye|turquie",
    "RUS": r"russia|russian federation|russie",
    "ZAF": r"south africa"
    r"|afrique du sud",
    "CAN": r"canada",
    "USA": r"united states(?: of america)?|[ée]tats-unis|etats-unis",
    "MEX": r"mexico|mexique",
    "ARG": r"argentina|argentine",
    "BRA": r"brazil|br[ée]sil",
    "CHN": r"china|chine",
    "JPN": r"japan|japon",
    "KOR": r"south korea|korea|cor[ée]e du sud|cor[ée]e",
    "IND": r"india|inde",
    "IDN": r"indonesia|indon[ée]sie",
    "SAU": r"saudi arabia|arabie saoudite",
    "AUS": r"australia|australie",
    "FIGARO_ROW": r"rest of the world|reste du monde",
}
#: Demonyms (EN/FR): "Norwegian oil", "pétrole norvégien", "raffineries françaises" name the origin.
ECONOMY_DEMONYMS: Mapping[str, str] = {
    "BEL": r"belgian|belges?",
    "BGR": r"bulgarian|bulgares?",
    "CZE": r"czech|tch[èe]ques?",
    "DNK": r"danish"
    r"|danois\w*",
    "DEU": r"german|allemand\w*",
    "EST": r"estonian|estonien\w*",
    "IRL": r"irish|irlandais\w*",
    "GRC": r"greek|grec(?:que)?s?",
    "ESP": r"spanish|espagnol\w*",
    "FRA": r"french|fran[çc]ais\w*",
    "HRV": r"croatian|croates?",
    "ITA": r"italian|italien\w*",
    "CYP": r"cypriot|chypriotes?",
    "LVA": r"latvian"
    r"|letton\w*",
    "LTU": r"lithuanian|lituanien\w*",
    "LUX": r"luxembourgish|luxembourgeois\w*",
    "HUN": r"hungarian|hongrois\w*",
    "MLT": r"maltese|maltais\w*",
    "NLD": r"dutch|n[ée]erlandais\w*",
    "AUT": r"austrian|autrichien\w*",
    "POL": r"polish|polonais\w*",
    "PRT": r"portuguese|portugais\w*",
    "ROU": r"romanian|roumain\w*",
    "SVN": r"slovenian|slov[èe]nes?",
    "SVK": r"slovak|slovaques?",
    "FIN": r"finnish|finlandais\w*",
    "SWE": r"swedish|su[ée]dois\w*",
    "NOR": r"norwegian|norv[ée]gien\w*",
    "CHE": r"swiss",
    "GBR": r"british|britanniques?",
    "MNE": r"montenegrin|mont[ée]n[ée]grin\w*",
    "MKD": r"macedonian|mac[ée]donien\w*",
    "ALB": r"albanian|albanais\w*",
    "SRB": r"serbian|serbes?",
    "TUR": r"turkish|turcs?|turques?",
    "RUS": r"russian|russes?",
    "ZAF": r"south african|sud-africain\w*",
    "CAN": r"canadian|canadien\w*",
    "USA": r"american|am[ée]ricain\w*",
    "MEX": r"mexican|mexicain\w*",
    "ARG": r"argentinian|argentins?",
    "BRA": r"brazilian|br[ée]silien\w*",
    "CHN": r"chinese|chinois\w*",
    "JPN": r"japanese|japonais\w*",
    "KOR": r"korean|cor[ée]en\w*",
    "IND": r"indian|indien\w*",
    "IDN": r"indonesian|indon[ée]sien\w*",
    "SAU": r"saudi|saoudien\w*",
    "AUS": r"australian|australien\w*",
}
_CODES_CASED = {"USA": r"US|U\.S\.|USA", "GBR": r"UK|GBR"}
_ORIGIN_MARKER = _rx(
    r"(?:\bfrom|\bde|\bd'|\bdu|\bdes|\bprovenance\s+(?:de|du|des|d')|\bproduced\s+in|\bmade\s+in"
    r"|\boriginating\s+(?:in|from)|\bproduit\w*\s+(?:en|au|aux)|\bimport\w*\s+(?:from|de|du|des|d')"
    r"|\bexport\w*\s+(?:by|par)|\bsupplied\s+by|\bfourni\w*\s+par)\s*(?:la\s+|le\s+|les\s+|l'|the\s+)?$"
)
COMMODITY = _rx(
    r"\b(?:p[ée]trol\w*|brut|crude|oil|gaz|gas|carburants?|fuels?|essence|gazole|diesel|k[ée]ros[èe]ne"
    r"|charbon|coal|barils?|barrels?|brent|wti|opep|opec|gnl|lng)\b"
)
WORLD = _rx(
    r"\b(?:mondia\w*|world\w*|global\w*|internationa\w*|partout|everywhere|dans le monde"
    r"|tous les pays producteurs|all producers|toutes les [ée]conomies|all economies|every economy"
    r"|chaque [ée]conomie)\b"
)
_CRUDE = _rx(r"\b(?:p[ée]trole(?! raffin)|brut|crude|oil|barils?|barrels?|brent|wti)\b")
_REFINED = _rx(
    r"\b(?:raffin\w*|refin\w*|carburants?|fuels?|essence|gazole|diesel|k[ée]ros[èe]ne|petrol\b|gasoline"
    r"|produits p[ée]troliers|petroleum products|coke)\b"
)
_ELECTRICITY = _rx(r"\b(?:[ée]lectri\w*)\b")
_GAS = _rx(r"\b(?:gaz|gas)\b")
_MINING_OTHER = _rx(
    r"\b(?:charbon|coal|lignite|minerai\w*|ores?|metal\w*|m[ée]ta\w*|carri[èe]res?|quarr\w*"
    r"|mining|mines?|extracti\w*)\b"
)


def _matches(pattern: str | None, text: str, flags: int) -> list[re.Match[str]]:
    if not pattern:
        return []
    return list(re.finditer(r"(?<![\w-])(?:" + pattern + r")(?![\w-])", text, flags))


def origin_mentions(text: str, key: str) -> tuple[bool, bool]:
    """(named anywhere, named *as an origin*).

    A demonym ("Norwegian", "norvégien", "raffineries françaises") names an origin; a country name does
    when an origin marker precedes it ("from Norway", "en provenance de Norvège", "du Japon") or it is
    possessive ("Norway's"). A bare location ("en France", "in France") does not.
    """
    if _matches(ECONOMY_DEMONYMS.get(key), text, re.IGNORECASE):
        return True, True
    cased = _CODES_CASED.get(key, key if re.fullmatch(r"[A-Z]{3}", key) else None)
    names = _matches(ECONOMY_NAMES.get(key), text, re.IGNORECASE) + _matches(cased, text, 0)
    as_origin = any(
        _ORIGIN_MARKER.search(text[max(0, m.start() - 40) : m.start()]) or text[m.end() : m.end() + 2] in ("'s", "’s")
        for m in names
    )
    return bool(names), as_origin


def io_price_findings(text: str, spec: Mapping[str, Any], industries: Mapping[str, str]) -> list[Finding]:
    out: list[Finding] = []
    raw = spec.get("intervention")
    inter: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    shocks = [s for s in (inter.get("shocks") or []) if isinstance(s, Mapping)]
    reported = {str(c) for c in spec.get("countries") or []}
    commodity = COMMODITY.search(text)
    world = WORLD.search(text)
    if world:
        out.append(
            Finding(
                "nl.shock_origin_not_stated",
                "The text describes a world-wide price change. In this model that is a price change in every "
                "producing FIGARO economy (49 countries and the rest of the world), which a draft cannot hold (at most "
                "10 shocks). Name the economies whose producer prices change, or write the specification directly.",
                "Le texte décrit une variation de prix mondiale. Dans ce modèle, c'est une variation de prix dans chaque "
                "économie FIGARO productrice (49 pays et le reste du monde), qu'un brouillon ne peut pas contenir (au "
                "plus 10 chocs). Nommez les économies dont les prix à la production changent, ou écrivez la "
                "spécification directement.",
                block=True,
                field="intervention.shocks",
                excerpt=_excerpt(world),
            )
        )
    unstated: list[str] = []
    defaulted: list[str] = []
    for s in shocks:
        origin = str(s.get("origin") or "")
        if not origin:
            continue
        named, as_origin = origin_mentions(text, origin)
        if not named:
            unstated.append(origin)
        elif origin in reported and commodity and not as_origin:
            defaulted.append(origin)
    if unstated:
        codes = ", ".join(sorted(set(unstated)))
        out.append(
            Finding(
                "nl.shock_origin_not_stated",
                f"The shock origin(s) {codes} are not stated in the text. Say in which economies the producer price "
                "changes (e.g. 'from Norway and Saudi Arabia', 'French refineries'); REVA does not choose them.",
                f"Le texte n'indique pas l'origine du choc ({codes}). Précisez dans quelles économies le prix à la "
                "production change (par ex. « en provenance de Norvège et d'Arabie saoudite », « raffineries "
                "françaises ») ; REVA ne les choisit pas.",
                block=True,
                field="intervention.shocks[].origin",
            )
        )
    if defaulted:
        codes = ", ".join(sorted(set(defaulted)))
        out.append(
            Finding(
                "nl.shock_origin_not_stated",
                f"The text names {codes} as the place where the effect is reported, not as the origin of the price "
                f"change of a traded commodity; the draft would shock only producers in {codes} (imports unchanged). "
                "State the origin economies explicitly; REVA does not default them to the reported country.",
                f"Le texte cite {codes} comme pays où l'effet est mesuré, pas comme origine de la hausse du prix d'un "
                f"produit échangé ; le brouillon ne choquerait que les producteurs de {codes} (importations "
                "inchangées). Indiquez explicitement les économies d'origine ; REVA ne les remplace pas par le pays "
                "étudié.",
                block=True,
                field="intervention.shocks[].origin",
            )
        )
    chosen = sorted({str(s.get("industry")) for s in shocks if s.get("industry")})
    for code in chosen:
        meaning = industries.get(code, code)
        if code == "B":
            out.append(
                Finding(
                    "nl.industry_scope",
                    f"B = {meaning}",
                    f"B = {meaning}",
                    block=False,
                    field="intervention.shocks",
                    warning=f"industry_broader_than_request: B is all of mining and quarrying ({meaning}); the shock "
                    "moves the whole industry's price, not only crude petroleum or natural gas"
                    + ("" if _MINING_OTHER.search(text) else "; the text names no other mining product"),
                )
            )
        elif code == "C19" and _CRUDE.search(text) and not _REFINED.search(text):
            out.append(
                Finding(
                    "nl.industry_scope",
                    f"C19 = {meaning}",
                    f"C19 = {meaning}",
                    block=False,
                    field="intervention.shocks",
                    warning=f"industry_differs_from_request: C19 is {meaning} The text speaks of oil/crude: crude "
                    "petroleum itself is inside B (mining and quarrying, broader than crude); a C19 shock leaves "
                    "the crude price unchanged",
                )
            )
        elif code == "D35" and (_ELECTRICITY.search(text) or _GAS.search(text)):
            extra = "; natural-gas extraction is in B, not D35" if _GAS.search(text) else ""
            out.append(
                Finding(
                    "nl.industry_scope",
                    f"D35 = {meaning}",
                    f"D35 = {meaning}",
                    block=False,
                    field="intervention.shocks",
                    warning=f"industry_broader_than_request: D35 is {meaning} It covers electricity, gas, steam and "
                    f"air-conditioning supply together{extra}",
                )
            )
    return out


# --------------------------------------------------------------------------- social.retirement_age
_LEVEL_FR = _rx(r"(?:[âa]ge|d[ée]part|retraite|pension)\b[^.;!?]{0,60}?\b(?:à|a)\s+(\d{2})\s*ans\b")
_LEVEL_EN = _rx(
    r"(?:\bage|retirement|pension)\b[^.;!?]{0,60}?\b(?:to|at|of)\s+(?:age\s+)?(\d{2})\b"
    r"(?!\s*(?:months?|%))"
)
_SHIFT = _rx(
    r"(?:\bde|\bby|\+)\s*(\d{1,2})\s*(?:mois|months?|ans?|ann[ée]es?|years?|trimestres?|quarters?)\b"
    r"|\b(\d{1,2})\s*(?:mois|months?|trimestres?|quarters?)\s+"
    r"(?:de plus|later|plus tard|more|earlier|plus t[ôo]t)"
)
SHIFT_KIND = "retirement_age_shift"


def stated_age_level(text: str) -> tuple[int, str] | None:
    """A retirement-age *level* the text states (50-75), with the matched words, else ``None``."""
    for rx in (_LEVEL_FR, _LEVEL_EN):
        for m in rx.finditer(text):
            age = int(m.group(1))
            if 50 <= age <= 75:
                return age, m.group(0)[-80:]
    return None


_MONTHS_STATED = _rx(r"\b\d{1,2}\s*(?:mois|months?)\b")


def _target_months(inter: Mapping[str, Any]) -> tuple[str, Any] | None:
    for name, value in inter.items():
        if re.search(r"age", str(name)) and re.search(r"month", str(name)) and not isinstance(value, bool):
            return str(name), value
    return None


def retirement_findings(text: str, spec: Mapping[str, Any], level_kinds: Sequence[str]) -> list[Finding]:
    raw = spec.get("intervention")
    inter: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    kind = inter.get("kind")
    level = stated_age_level(text)
    if level is None:
        return []
    age, words = level
    if kind in level_kinds:
        target = _target_months(inter)
        if target is None or _MONTHS_STATED.search(text) or target[1] == age * 12:
            return []
        name, value = target
        return [
            Finding(
                "nl.age_level_not_shift",
                f"The text states a retirement age of {age} years ({age * 12} months) but the draft sets "
                f"{name} = {value}. Confirm the target age; REVA does not change a stated level.",
                f"Le texte indique un âge de départ de {age} ans ({age * 12} mois) mais le brouillon fixe {name} = "
                f"{value}. Confirmez l'âge cible ; REVA ne modifie pas un niveau indiqué.",
                block=True,
                field=f"intervention.{name}",
                excerpt=words,
            )
        ]
    if kind != SHIFT_KIND or _SHIFT.search(text):
        return []
    if level_kinds:
        names = ", ".join(level_kinds)
        en = (
            f"The text states a retirement age of {age} (a level), but the draft is a shift in months. Use the "
            f"target-age intervention ({names}) or confirm the shift in months you mean; REVA does not convert "
            "a level into a shift."
        )
        fr = (
            f"Le texte indique un âge de départ de {age} ans (un niveau), mais le brouillon est un décalage en mois. "
            f"Utilisez l'intervention d'âge cible ({names}) ou confirmez le décalage en mois voulu ; REVA ne "
            "convertit pas un niveau en décalage."
        )
    else:
        en = (
            f"The text states a retirement age of {age} (a level). The registered model can only shift the "
            "reference path by a number of months (or use a sourced legal path); it has no 'set the age to N' "
            "intervention. Was a shift in months meant, and from which birth cohort? REVA does not convert a "
            "level into a shift."
        )
        fr = (
            f"Le texte indique un âge de départ de {age} ans (un niveau). Le modèle enregistré ne peut que décaler "
            "le chemin de référence d'un nombre de mois (ou utiliser un chemin légal sourcé) ; il n'a pas "
            "d'intervention « fixer l'âge à N ». Vouliez-vous un décalage en mois, et à partir de quelle "
            "génération ? REVA ne convertit pas un niveau en décalage."
        )
    return [Finding("nl.age_level_not_shift", en, fr, block=True, field="intervention.kind", excerpt=words)]


def level_intervention_kinds(model_branch: Mapping[str, Any]) -> list[str]:
    """Intervention kinds of a model branch that set a target age level (none = only shifts / legal paths)."""
    from reva.nl.catalog import _const, discriminator, object_variants

    node = (model_branch.get("properties") or {}).get("intervention") or {}
    variants = object_variants(node) if isinstance(node, Mapping) else []
    disc = discriminator(variants)
    out: list[str] = []
    for v in variants:
        props = v.get("properties") or {}
        kind = _const(props.get(disc))[1] if disc else None
        if not isinstance(kind, str) or kind == SHIFT_KIND:
            continue
        if any(
            re.search(r"(?:^|_)(?:target|minimum|legal|level)_?age|age_(?:years|level|target)", str(name))
            for name in props
        ) or re.search(r"target|set_(?:minimum|legal)", kind):
            out.append(kind)
    return out
