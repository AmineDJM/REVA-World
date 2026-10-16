"""Multilateral treaty status per state from the UN Treaty Collection (V3, agent A-TREATIES).

Source
------
The Secretary-General is the depositary of > 560 multilateral treaties; the UN Office of Legal Affairs
publishes their status ("Multilateral Treaties Deposited with the Secretary-General", MTDSG) on
https://treaties.un.org, one HTML page per treaty (``Pages/ViewDetails.aspx?src=TREATY&mtdsg_no=<n>&
chapter=<c>``; some treaties use the ``ViewDetailsII`` (``Temp=mtdsg2``) or ``ViewDetailsIII``
(``Temp=mtdsg3``) templates — the catalog below stores the template each page answers with, so no
redirect is ever followed). There is no bulk or machine-readable export; each page is fetched once per
run (sequential, polite interval), archived as an evidence acquisition and parsed with bounded regular
expressions over the page's own table ids.

Rights: the UN Terms of Use (https://www.un.org/en/about-us/terms-of-use, fetched and archived on every
run) grant "personal, non-commercial use, without any right to resell or redistribute them or to compile
or create derivative works therefrom". **Not an open licence**: the tables and statements derived here
carry ``open_licence: false`` and must not be redistributed without permission (flagged for maintainer
review in ``registry/sources/untc.yaml``). The facts themselves (which State deposited which instrument,
when) are public acts of States notified by the depositary.

What one page gives (parsed, never inferred)
--------------------------------------------
* the participant table (``…_tblgrid``): participant name as UNTC writes it, signature date, the date of
  the instrument expressing consent to be bound, the instrument type from the column header's own legend
  (e.g. ``Accession(a), Succession(d), Ratification`` -> ``a`` = accession, ``d`` = succession, no code =
  ratification), footnote numbers, whether the participant name links to declarations/reservations, and
  UNTC's square brackets (a participant that has withdrawn/denounced keeps its dates in brackets);
* the footnotes (``divNotesAll``): their text, and two kinds of withdrawal evidence:
  (1) footnote tables with the headers ``Participant / Date of notification / Date of effect``
  (Rome Statute, Kyoto Protocol...) — ``(Withdrawn)`` in the effect column means the notification was
  revoked; (2) footnote sentences of the form "On <date>, the Government of <State> notified the
  Secretary-General of its decision to withdraw from / denounce … with effect from <date>" (regex,
  flagged ``basis: note_text``) attributed to a participant that references the note (and is named in
  the sentence when several do);
* the provider's own counts ("Signatories : n. Parties : m") — reported next to the parsed counts.

Historical successor-state actions are recorded exactly as UNTC states them: the participant name is the
one in the table (e.g. "Russian Federation" with the USSR's 1973 ratification date, "Czechia" with its
succession), and every note the row references is kept verbatim (atlas ``|note=<n>`` rows) so the
continuity/succession statement of the depositary travels with the date.

Outputs
-------
* atlas table ``untc.treaty_status`` (one row per participant × treaty × action): indicators
  ``<mtdsg>|action=signature|consent|withdrawal_notification|withdrawal_effect``, ``<mtdsg>|status`` (as
  listed on the retrieval day) and ``<mtdsg>|note=<n>`` (footnote text); dates are day periods.
* KG (``source_key untc.mtdsg``): ``treaty —P1891 signatory→ state`` (point in time = signature date) and
  ``treaty —untc:party→ state`` (start = date of the consent instrument, end = date of effect of a
  withdrawal when UNTC states one), ``untc:former_party`` when UNTC brackets a participant but states no
  effective date; ``untc:mtdsg_number`` on the treaty. Treaty subjects are Wikidata QIDs from
  ``registry/entities/treaty_qids.json`` (verified mapping) or ``untc:treaty:<mtdsg>``; states are QIDs
  from ``registry/entities/country_qids.json`` (else unambiguous P298 statements in the KG) or
  ``iso3:<KEY>``; participants outside the crosswalk (European Union...) are ``untc:participant:<slug>``.
"""

from __future__ import annotations

import html
import json
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from reva.atlas.sources import AtlasSourceSpec

NAME = "atlas.untc"
VERSION = "1.0.0"
SOURCE_KEY = "untc.mtdsg"
ATLAS_SOURCE = "untc"
DATASET = "treaty_status"
PARSER_VERSION = "atlas_untc/1"
GEO_SCHEME = "untc"
HOST = "treaties.un.org"
TERMS_HOST = "www.un.org"
TERMS_URL = "https://www.un.org/en/about-us/terms-of-use"
TERMS_PHRASE = "personal, non-commercial use"
LICENCE = (
    "UN Terms of Use (www.un.org/en/about-us/terms-of-use): personal, non-commercial use; no redistribution or "
    "derivative compilations without permission -- not an open licence"
)
MAX_PAGE_BYTES = 12_000_000
MAX_TEXT = 4000
MIN_PARTICIPANTS_LIVE = 20
REPO_ROOT = Path(__file__).resolve().parents[4]
TREATY_QIDS_FILE = REPO_ROOT / "registry" / "entities" / "treaty_qids.json"
P_PARTY = "untc:party"
P_FORMER = "untc:former_party"
P_MTDSG = "untc:mtdsg_number"
P_SIGNATORY = "P1891"
_P = "ctl00_ctl00_ContentPlaceHolder1_ContentPlaceHolderInnerPage_"


@dataclass(frozen=True)
class Treaty:
    mtdsg: str  # e.g. "XVIII-10"
    chapter: int
    template: str  # "" | "II" | "III" (ViewDetails, ViewDetailsII Temp=mtdsg2, ViewDetailsIII Temp=mtdsg3)
    key: str
    label_en: str
    label_fr: str
    group: str
    ohchr: str = ""  # OHCHR dashboard acronym for the 18 core human rights instruments

    @property
    def url(self) -> str:
        page = f"ViewDetails{self.template}.aspx"
        temp = f"&Temp=mtdsg{len(self.template)}" if self.template else ""  # II -> mtdsg2, III -> mtdsg3
        return f"https://{HOST}/Pages/{page}?src=TREATY&mtdsg_no={self.mtdsg}&chapter={self.chapter}{temp}&clang=_en"


T = Treaty
#: Bounded list: the 18 core human rights instruments + 35 major multilateral treaties deposited with the
#: Secretary-General. NPT/BWC (UNODA, three depositaries), CITES (Switzerland), Ramsar (UNESCO) and the Geneva
#: Conventions (Switzerland) are not deposited with the UN SG and are not on these pages (see the report).
TREATIES: tuple[Treaty, ...] = (
    # --- human rights (chapter IV)
    T("IV-1", 4, "", "genocide", "Genocide Convention (1948)", "Convention pour la prévention et la répression du crime de génocide (1948)", "human_rights"),
    T("IV-2", 4, "", "icerd", "International Convention on the Elimination of All Forms of Racial Discrimination", "Convention internationale sur l'élimination de toutes les formes de discrimination raciale", "human_rights_core", "ICERD"),
    T("IV-3", 4, "", "icescr", "International Covenant on Economic, Social and Cultural Rights", "Pacte international relatif aux droits économiques, sociaux et culturels", "human_rights_core", "ICESCR"),
    T("IV-3-a", 4, "", "icescr_op", "Optional Protocol to the ICESCR", "Protocole facultatif se rapportant au Pacte international relatif aux droits économiques, sociaux et culturels", "human_rights_core", "CESCR-OP"),
    T("IV-4", 4, "", "iccpr", "International Covenant on Civil and Political Rights", "Pacte international relatif aux droits civils et politiques", "human_rights_core", "ICCPR"),
    T("IV-5", 4, "", "iccpr_op1", "Optional Protocol to the ICCPR (individual communications)", "Protocole facultatif se rapportant au Pacte international relatif aux droits civils et politiques", "human_rights_core", "ICCPR-OP1"),
    T("IV-12", 4, "", "iccpr_op2", "Second Optional Protocol to the ICCPR (abolition of the death penalty)", "Deuxième Protocole facultatif se rapportant au Pacte international relatif aux droits civils et politiques, visant à abolir la peine de mort", "human_rights_core", "ICCPR-OP2"),
    T("IV-7", 4, "", "apartheid", "International Convention on the Suppression and Punishment of the Crime of Apartheid", "Convention internationale sur l'élimination et la répression du crime d'apartheid", "human_rights"),
    T("IV-8", 4, "", "cedaw", "Convention on the Elimination of All Forms of Discrimination against Women", "Convention sur l'élimination de toutes les formes de discrimination à l'égard des femmes", "human_rights_core", "CEDAW"),
    T("IV-8-b", 4, "", "cedaw_op", "Optional Protocol to CEDAW", "Protocole facultatif à la Convention sur l'élimination de toutes les formes de discrimination à l'égard des femmes", "human_rights_core", "OP-CEDAW"),
    T("IV-9", 4, "", "cat", "Convention against Torture", "Convention contre la torture et autres peines ou traitements cruels, inhumains ou dégradants", "human_rights_core", "CAT"),
    T("IV-9-b", 4, "", "cat_op", "Optional Protocol to the Convention against Torture", "Protocole facultatif se rapportant à la Convention contre la torture", "human_rights_core", "OP-CAT"),
    T("IV-11", 4, "", "crc", "Convention on the Rights of the Child", "Convention relative aux droits de l'enfant", "human_rights_core", "CRC"),
    T("IV-11-b", 4, "", "crc_opac", "Optional Protocol to the CRC on children in armed conflict", "Protocole facultatif à la Convention relative aux droits de l'enfant, concernant l'implication d'enfants dans les conflits armés", "human_rights_core", "OP-CRC-AC"),
    T("IV-11-c", 4, "", "crc_opsc", "Optional Protocol to the CRC on the sale of children, child prostitution and child pornography", "Protocole facultatif à la Convention relative aux droits de l'enfant, concernant la vente d'enfants, la prostitution des enfants et la pornographie mettant en scène des enfants", "human_rights_core", "OP-CRC-SC"),
    T("IV-11-d", 4, "", "crc_opic", "Optional Protocol to the CRC on a communications procedure", "Protocole facultatif à la Convention relative aux droits de l'enfant établissant une procédure de présentation de communications", "human_rights_core", "CRC-OP-IC"),
    T("IV-13", 4, "", "icrmw", "International Convention on the Protection of the Rights of All Migrant Workers and Members of Their Families", "Convention internationale sur la protection des droits de tous les travailleurs migrants et des membres de leur famille", "human_rights_core", "ICRMW"),
    T("IV-15", 4, "", "crpd", "Convention on the Rights of Persons with Disabilities", "Convention relative aux droits des personnes handicapées", "human_rights_core", "CRPD"),
    T("IV-15-a", 4, "", "crpd_op", "Optional Protocol to the CRPD", "Protocole facultatif se rapportant à la Convention relative aux droits des personnes handicapées", "human_rights_core", "CRPD-OP"),
    T("IV-16", 4, "", "cped", "International Convention for the Protection of All Persons from Enforced Disappearance", "Convention internationale pour la protection de toutes les personnes contre les disparitions forcées", "human_rights_core", "CPED"),
    # --- refugees and stateless persons (chapter V)
    T("V-2", 5, "II", "refugees_1951", "Convention relating to the Status of Refugees (1951)", "Convention relative au statut des réfugiés (1951)", "refugees"),
    T("V-5", 5, "", "refugees_1967", "Protocol relating to the Status of Refugees (1967)", "Protocole relatif au statut des réfugiés (1967)", "refugees"),
    T("V-3", 5, "II", "stateless_1954", "Convention relating to the Status of Stateless Persons (1954)", "Convention relative au statut des apatrides (1954)", "refugees"),
    T("V-4", 5, "", "stateless_1961", "Convention on the Reduction of Statelessness (1961)", "Convention sur la réduction des cas d'apatridie (1961)", "refugees"),
    # --- diplomatic relations, law of treaties, law of the sea
    T("III-3", 3, "", "vcdr", "Vienna Convention on Diplomatic Relations", "Convention de Vienne sur les relations diplomatiques", "diplomacy"),
    T("III-6", 3, "", "vccr", "Vienna Convention on Consular Relations", "Convention de Vienne sur les relations consulaires", "diplomacy"),
    T("XXIII-1", 23, "III", "vclt", "Vienna Convention on the Law of Treaties", "Convention de Vienne sur le droit des traités", "law_of_treaties"),
    T("XXI-6", 21, "II", "unclos", "United Nations Convention on the Law of the Sea", "Convention des Nations Unies sur le droit de la mer", "law_of_the_sea"),
    T("XXI-10", 21, "", "bbnj", "Agreement on Marine Biological Diversity of Areas beyond National Jurisdiction (BBNJ)", "Accord sur la biodiversité marine des zones ne relevant pas de la juridiction nationale (BBNJ)", "law_of_the_sea"),
    # --- penal matters, crime, drugs, health
    T("XVIII-10", 18, "", "rome_statute", "Rome Statute of the International Criminal Court", "Statut de Rome de la Cour pénale internationale", "penal"),
    T("XVIII-11", 18, "", "terrorism_financing", "International Convention for the Suppression of the Financing of Terrorism", "Convention internationale pour la répression du financement du terrorisme", "penal"),
    T("XVIII-12", 18, "", "untoc", "UN Convention against Transnational Organized Crime", "Convention des Nations Unies contre la criminalité transnationale organisée", "penal"),
    T("XVIII-12-a", 18, "", "trafficking_protocol", "Protocol to Prevent, Suppress and Punish Trafficking in Persons", "Protocole visant à prévenir, réprimer et punir la traite des personnes, en particulier des femmes et des enfants", "penal"),
    T("XVIII-14", 18, "", "uncac", "UN Convention against Corruption", "Convention des Nations Unies contre la corruption", "penal"),
    T("VI-19", 6, "", "drug_trafficking_1988", "UN Convention against Illicit Traffic in Narcotic Drugs and Psychotropic Substances (1988)", "Convention des Nations Unies contre le trafic illicite de stupéfiants et de substances psychotropes (1988)", "drugs"),
    T("IX-4", 9, "", "who_fctc", "WHO Framework Convention on Tobacco Control", "Convention-cadre de l'OMS pour la lutte antitabac", "health"),
    # --- disarmament (chapter XXVI)
    T("XXVI-2", 26, "", "ccw", "Convention on Certain Conventional Weapons", "Convention sur certaines armes classiques", "disarmament"),
    T("XXVI-3", 26, "", "cwc", "Chemical Weapons Convention", "Convention sur l'interdiction des armes chimiques", "disarmament"),
    T("XXVI-4", 26, "", "ctbt", "Comprehensive Nuclear-Test-Ban Treaty", "Traité d'interdiction complète des essais nucléaires", "disarmament"),
    T("XXVI-5", 26, "", "mine_ban", "Anti-Personnel Mine Ban Convention (Ottawa)", "Convention sur l'interdiction des mines antipersonnel (Ottawa)", "disarmament"),
    T("XXVI-6", 26, "", "cluster_munitions", "Convention on Cluster Munitions", "Convention sur les armes à sous-munitions", "disarmament"),
    T("XXVI-8", 26, "", "att", "Arms Trade Treaty", "Traité sur le commerce des armes", "disarmament"),
    T("XXVI-9", 26, "", "tpnw", "Treaty on the Prohibition of Nuclear Weapons", "Traité sur l'interdiction des armes nucléaires", "disarmament"),
    # --- environment (chapter XXVII)
    T("XXVII-2", 27, "", "vienna_ozone", "Vienna Convention for the Protection of the Ozone Layer", "Convention de Vienne pour la protection de la couche d'ozone", "environment"),
    T("XXVII-2-a", 27, "", "montreal_protocol", "Montreal Protocol on Substances that Deplete the Ozone Layer", "Protocole de Montréal relatif à des substances qui appauvrissent la couche d'ozone", "environment"),
    T("XXVII-3", 27, "", "basel", "Basel Convention on Hazardous Wastes", "Convention de Bâle sur les déchets dangereux", "environment"),
    T("XXVII-7", 27, "III", "unfccc", "UN Framework Convention on Climate Change", "Convention-cadre des Nations Unies sur les changements climatiques", "environment"),
    T("XXVII-7-a", 27, "", "kyoto_protocol", "Kyoto Protocol", "Protocole de Kyoto", "environment"),
    T("XXVII-7-d", 27, "", "paris_agreement", "Paris Agreement", "Accord de Paris", "environment"),
    T("XXVII-8", 27, "", "cbd", "Convention on Biological Diversity", "Convention sur la diversité biologique", "environment"),
    T("XXVII-8-a", 27, "", "cartagena_protocol", "Cartagena Protocol on Biosafety", "Protocole de Cartagena sur la prévention des risques biotechnologiques", "environment"),
    T("XXVII-10", 27, "", "unccd", "UN Convention to Combat Desertification", "Convention des Nations Unies sur la lutte contre la désertification", "environment"),
    T("XXVII-14", 27, "", "rotterdam", "Rotterdam Convention (prior informed consent)", "Convention de Rotterdam (consentement préalable en connaissance de cause)", "environment"),
    T("XXVII-15", 27, "", "stockholm", "Stockholm Convention on Persistent Organic Pollutants", "Convention de Stockholm sur les polluants organiques persistants", "environment"),
    T("XXVII-17", 27, "", "minamata", "Minamata Convention on Mercury", "Convention de Minamata sur le mercure", "environment"),
)  # fmt: skip
BY_MTDSG = {t.mtdsg: t for t in TREATIES}
#: treaties served in fixture mode (tests/fixtures/source/atlas.untc)
FIXTURE_TREATIES = ("IV-5", "XVIII-10", "XXVII-7-a", "XXVII-7-d")


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="United Nations, Office of Legal Affairs (Treaty Section): Multilateral Treaties Deposited with "
        "the Secretary-General",
        licence=LICENCE,
        allowed_hosts=(HOST, TERMS_HOST),
        datasets=(DATASET,),
        dataset_ids=tuple(t.mtdsg for t in TREATIES),
        cadence_seconds=30 * 86400,
        planned_requests=len(TREATIES) + 4,
        max_response_bytes=MAX_PAGE_BYTES,
        min_interval_seconds=3.0,
        parser_version=PARSER_VERSION,
        notes="one HTML status page per treaty (no bulk export); UN Terms of Use page archived each run",
    )


# ---------------------------------------------------------------------------------------------- parsing
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}  # fmt: skip
_DATE_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})")
_CELL_RE = re.compile(r"^(\[)?\s*(\d{1,2}\s+[A-Za-z]{3,9}\.?\s+\d{4})\s*([A-Za-z]{1,2})?\s*(\])?$")
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


class UntcParseError(ValueError):
    pass


def text_of(fragment: str) -> str:
    """Visible text of an HTML fragment (tags removed, entities decoded, whitespace collapsed)."""
    t = re.sub(r"<br\s*/?>", " ", fragment, flags=re.I)
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", t))).replace("\xa0", " ").strip()


def parse_date(s: str) -> date | None:
    m = _DATE_RE.search(s)
    if not m:
        return None
    mon = _MONTHS.get(m.group(2)[:3].lower())
    if mon is None:
        return None
    try:
        return date(int(m.group(3)), mon, int(m.group(1)))
    except ValueError:
        return None


def norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).replace("’", "'").replace("*", " ")
    return _WS_RE.sub(" ", s).strip().casefold()


def slug(s: str) -> str:
    a = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", a).strip("_")[:80] or "x"


def legend(header: str) -> tuple[dict[str, str], str | None]:
    """Column-header legend -> ({code: term}, default term). 'Accession(a), Succession(d), Ratification'."""
    codes: dict[str, str] = {}
    default = None
    for part in header.split(","):
        part = re.sub(r"\b\d+\b", "", part).strip()  # footnote numbers in headers
        if not part:
            continue
        m = re.match(r"^(.*?)\s*\(([A-Za-z]{1,2})\)\s*$", part)
        if m:
            codes[m.group(2)] = m.group(1).strip()
        elif default is None:
            default = part
    return codes, default


def term_key(term: str) -> str:
    return re.sub(r"[^a-z]+", "_", term.lower()).strip("_")


@dataclass
class Cell:
    raw: str
    date: date | None = None
    code: str | None = None
    term: str | None = None
    bracketed: bool = False


@dataclass
class Participant:
    row: int
    name: str
    notes: list[str]
    declarations_link: bool
    signature: Cell | None
    consent: Cell | None
    provisional: Cell | None = None


@dataclass
class WithdrawalEvent:
    participant: str
    note: str
    basis: str  # note_table | note_text
    notification: date | None
    effect: date | None
    revoked: bool = False
    reference: str | None = None
    excerpt: str = ""


@dataclass
class Page:
    treaty: Treaty
    title: str
    place_date: str
    entry_into_force: str
    provider_signatories: int | None
    provider_parties: int | None
    signature_header: str
    consent_header: str
    participants: list[Participant]
    notes: dict[str, str]
    withdrawals: list[WithdrawalEvent] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _div(s: str, idn: str) -> str | None:
    m = re.search(r'id="' + _P + re.escape(idn) + r'"[^>]*>(.*?)</div>', s, re.S)
    return text_of(m.group(1)) if m else None


def _cell(raw_html: str, codes: Mapping[str, str], default: str | None) -> Cell | None:
    raw = text_of(raw_html)
    if not raw:
        return None
    m = _CELL_RE.match(raw)
    if not m:
        raise UntcParseError(f"unrecognised date cell {raw[:60]!r}")
    d = parse_date(m.group(2))
    code = m.group(3)
    if d is None or (code is not None and code not in codes) or (code is None and default is None):
        raise UntcParseError(f"unrecognised date cell {raw[:60]!r} (legend {sorted(codes)})")
    term = codes[code] if code else default
    return Cell(raw=raw, date=d, code=code, term=term, bracketed=bool(m.group(1) or m.group(4)))


_NOTE_REF_RE = re.compile(r"<sup>(.*?)</sup>", re.I | re.S)


def _participant_cell(cell_html: str) -> tuple[str, list[str], bool]:
    notes = []
    for sup in _NOTE_REF_RE.findall(cell_html):
        notes += re.findall(r"\d+", text_of(sup))
    name = text_of(_NOTE_REF_RE.sub(" ", cell_html))
    return name, notes, "ShowDeclarationsNew" in cell_html


_WD_HEAD = (
    r"On (\d{1,2} [A-Za-z]{3,9} \d{4}),? (?:the Secretary-General received from )?the Government of (?:the )?"
    r"([^,.;:]{2,90}?) (?:notified|informed|deposited|a notification)[^.]{0,220}?"
    r"(?:withdraw from|withdrawal from|denounce|denunciation of|notification of withdrawal)"
)
#: the date of effect may follow in the next sentence ("... (See C.N.360.2025 ...). The withdrawal took effect for
#: Estonia on 27 December 2025"), but never past the next "On <date>" event of the note
_WD_EFFECT = (
    r"(?:(?!On \d{1,2} [A-Za-z]{3,9} \d{4}).){0,420}?(?:with effect from|which took effect on|"
    r"took effect(?: for [^,.;]{1,60})? on|effective (?:on|from|as from)|will take effect on|to take effect on|"
    r"as from) (\d{1,2} [A-Za-z]{3,9} \d{4})"
)
_WD_HEAD2 = (
    r"(?:By|In) a (?:notification|communication) received (?:by the Secretary-General )?on "
    r"(\d{1,2} [A-Za-z]{3,9} \d{4}),? the Government of (?:the )?([^,.;:]{2,90}?) (?:denounced|withdrew from|"
    r"(?:notified|informed)[^.]{0,220}?(?:withdraw from|withdrawal from|denounce|denunciation of))"
)
_WD_PATTERNS = (
    (re.compile(_WD_HEAD + _WD_EFFECT, re.S), True),
    (re.compile(_WD_HEAD2 + _WD_EFFECT, re.S), True),
    (re.compile(_WD_HEAD), False),
    (re.compile(_WD_HEAD2), False),
)


def _note_withdrawals(num: str, text: str, note_html: str, refs: Sequence[str]) -> list[WithdrawalEvent]:
    """Withdrawal/denunciation evidence in one footnote (tables first; sentences when no table)."""
    out: list[WithdrawalEvent] = []
    low = text.lower()
    if not re.search(r"withdraw from|withdrawal from|denounc|denunciation|notification of withdrawal", low):
        return out
    for tb in re.findall(r"<table[^>]*>(.*?)</table>", note_html, re.S | re.I):
        rows = [
            [text_of(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.S | re.I)]
            for r in re.findall(r"<tr[^>]*>(.*?)</tr>", tb, re.S | re.I)
        ]
        hdr_i = next((i for i, r in enumerate(rows) if r and r[0].rstrip(":").strip().lower() == "participant"), None)
        if hdr_i is None:
            continue
        hdr = [h.rstrip(":").strip().lower() for h in rows[hdr_i]]
        if "date of effect" not in hdr:
            continue
        i_n = hdr.index("date of notification") if "date of notification" in hdr else None
        i_e = hdr.index("date of effect")
        i_r = next((i for i, h in enumerate(hdr) if "depositary notification" in h), None)
        for r in rows[hdr_i + 1 :]:
            if len(r) < len(hdr):
                continue
            eff = r[i_e]
            out.append(
                WithdrawalEvent(
                    participant=r[0].replace("*", "").strip(),
                    note=num,
                    basis="note_table",
                    notification=parse_date(r[i_n]) if i_n is not None else None,
                    effect=parse_date(eff),
                    revoked="withdrawn" in eff.lower(),
                    reference=r[i_r] if i_r is not None else None,
                    excerpt=" | ".join(r)[:500],
                )
            )
    if out:
        return out
    seen: set[tuple[str, date | None, date | None]] = set()
    starts: set[int] = set()
    for rx, with_effect in _WD_PATTERNS:
        for m in rx.finditer(text):
            if m.start() in starts:
                continue
            starts.add(m.start())
            sentence_end = text.find(". ", m.end())
            excerpt = text[m.start() : (sentence_end if sentence_end > 0 else m.end()) + 1][:600]
            if re.search(r"withdraw (?:its|the|their) (?:reservation|declaration|objection)", excerpt, re.I):
                continue
            ev = WithdrawalEvent(
                participant=m.group(2).strip(),
                note=num,
                basis="note_text",
                notification=parse_date(m.group(1)),
                effect=parse_date(m.group(3)) if with_effect else None,
                excerpt=excerpt,
            )
            k = (norm_name(ev.participant), ev.notification, ev.effect)
            if k not in seen:
                seen.add(k)
                out.append(ev)
    return out


def parse_page(body: bytes, treaty: Treaty) -> Page:
    s = body.decode("utf-8", errors="strict")
    i = s.find(f'id="{_P}tblgrid"')
    if i < 0:
        raise UntcParseError(f"{treaty.mtdsg}: participant table not found (page layout changed?)")
    j = s.find("</table>", i)
    table = s[i:j]
    trs = re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S | re.I)
    if not trs:
        raise UntcParseError(f"{treaty.mtdsg}: empty participant table")
    hdr = [text_of(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", trs[0], re.S | re.I)]
    # columns: Participant, [Signature...], [Provisional application(n)], consent instrument legend (last)
    if len(hdr) < 2 or not hdr[0].startswith("Participant"):
        raise UntcParseError(f"{treaty.mtdsg}: unexpected table header {hdr}")
    i_sig = 1 if hdr[1].startswith("Signature") else None
    i_prov = next((i for i, h in enumerate(hdr) if h.lower().startswith("provisional application")), None)
    i_con = len(hdr) - 1
    if i_con in (i_sig, i_prov) or len(hdr) != 2 + (i_sig is not None) + (i_prov is not None):
        raise UntcParseError(f"{treaty.mtdsg}: unexpected table header {hdr}")
    sig_codes, sig_default = legend(hdr[i_sig]) if i_sig else ({}, "Signature")
    con_codes, con_default = legend(hdr[i_con])
    prov_codes, prov_default = legend(hdr[i_prov]) if i_prov else ({}, None)
    parts: list[Participant] = []
    for k, tr in enumerate(trs[1:], start=1):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)
        if len(cells) != len(hdr):
            raise UntcParseError(f"{treaty.mtdsg}: row {k} has {len(cells)} cells, header {len(hdr)}")
        name, notes, decl = _participant_cell(cells[0])
        if not name:
            raise UntcParseError(f"{treaty.mtdsg}: row {k} without participant name")
        parts.append(
            Participant(
                k, name, notes, decl,
                _cell(cells[i_sig], sig_codes, sig_default) if i_sig else None,
                _cell(cells[i_con], con_codes, con_default),
                _cell(cells[i_prov], prov_codes, prov_default) if i_prov else None,
            )
        )  # fmt: skip
    signature_header = hdr[i_sig] if i_sig else ""
    consent_header = hdr[i_con]
    # title / metadata
    title = ""
    m = re.search(r'id="' + _P + r'rptTreaty_ctl00_tcTreaty"[^>]*>(.*?)</div>', s, re.S)
    if m:
        title = re.sub(r"^\d+\s*\.\s*(?:[a-z]\b\s*)?", "", text_of(_NOTE_REF_RE.sub(" ", m.group(1))))
    status = _div(s, "rptStatus_ctl00_tcText") or ""
    ns = re.search(r"Signatories\s*:\s*(\d+)", status)
    np_ = re.search(r"Parties\s*:\s*(\d+)", status)
    # footnotes
    notes: dict[str, str] = {}
    note_html: dict[str, str] = {}
    for d in re.findall(r'class="divNotesAll"[^>]*>(.*?)</div>', s, re.S):
        mm = re.match(r"\s*<a name=['\"]?([0-9]+)['\"]?>", d)
        if not mm:
            continue
        num = mm.group(1)
        body_html = d[mm.end() :]
        body_html = re.sub(r"^\s*[0-9]+\s*</a>", "", body_html)
        notes[num] = text_of(body_html)
        note_html[num] = body_html
    page = Page(
        treaty=treaty,
        title=title,
        place_date=(_div(s, "rptText_ctl00_tcText") or "")[:300],
        entry_into_force=(_div(s, "rptEIF_ctl00_tcText") or "")[:300],
        provider_signatories=int(ns.group(1)) if ns else None,
        provider_parties=int(np_.group(1)) if np_ else None,
        signature_header=signature_header,
        consent_header=consent_header,
        participants=parts,
        notes=notes,
    )
    refs: dict[str, list[str]] = defaultdict(list)
    for p in parts:
        for n in p.notes:
            refs[n].append(p.name)
    by_norm = {norm_name(p.name): p.name for p in parts}
    for num, txt in notes.items():
        for ev in _note_withdrawals(num, txt, note_html[num], refs.get(num, [])):
            target = by_norm.get(norm_name(ev.participant))
            if target is None:  # sentence names "the Government of X": match a referencing participant
                cands = [n for n in refs.get(num, []) if norm_name(n) in norm_name(ev.participant)
                         or norm_name(ev.participant) in norm_name(n)]  # fmt: skip
                if len(cands) == 1:
                    target = cands[0]
                elif len(refs.get(num, [])) == 1:
                    target = refs[num][0]
            if target is None:
                page.problems.append(f"withdrawal in note {num} not attributed: {ev.participant[:60]}")
                continue
            ev.participant = target
            page.withdrawals.append(ev)
    return page


def check_terms(body: bytes) -> dict[str, Any]:
    t = text_of(body.decode("utf-8", errors="replace"))
    return {
        "url": TERMS_URL,
        "phrase_found": TERMS_PHRASE in t,
        "phrase": TERMS_PHRASE,
        "open_licence": False,
        "summary": "personal, non-commercial use; no redistribution or derivative compilations without permission",
    }


# ---------------------------------------------------------------------------------------------- status logic
def participant_status(p: Participant, events: Sequence[WithdrawalEvent], asof: date) -> dict[str, Any]:
    """UNTC's listing of one participant on the retrieval day (no inference beyond what the page states)."""
    wd = [e for e in events if not e.revoked]
    effects = sorted(e.effect for e in wd if e.effect)
    pending = [d for d in effects if d > asof]
    if p.consent and not p.consent.bracketed:
        status = "party"
    elif p.consent and p.consent.bracketed:
        status = "withdrawn"
    elif p.signature and not p.signature.bracketed:
        status = "signatory"
    elif p.signature:
        status = "signature_bracketed"
    else:
        status = "listed_without_dates"
    out: dict[str, Any] = {"status": status}
    if pending and status == "party":
        out["scheduled_withdrawal_effect"] = pending[-1].isoformat()
    if status == "withdrawn":
        start = p.consent.date if p.consent else None
        past = [d for d in effects if d <= asof and (start is None or d >= start)]
        out["withdrawal_effect"] = past[-1].isoformat() if past else None
    return out


# ---------------------------------------------------------------------------------------------- atlas rows
def _day(d: date) -> tuple[date, date]:
    return d, d + timedelta(days=1)


def atlas_rows(pages: Sequence[Page], raws: Mapping[str, str], asof: date) -> list[dict[str, Any]]:
    from reva.atlas.tables import json_obj

    rows: list[dict[str, Any]] = []

    def add(
        page: Page, p: Participant, ind: str, d: date, text_: str, flags: dict[str, Any], sel: dict[str, Any]
    ) -> None:
        a, b = _day(d)
        rows.append(
            {
                "source": ATLAS_SOURCE,
                "dataset": DATASET,
                "indicator": f"{page.treaty.mtdsg}|{ind}",
                "concept_id": None,
                "iso3": None,
                "provider_geo": p.name,
                "entity_qid": None,
                "period_start": a,
                "period_end": b,
                "value": None,
                "value_text": text_[:MAX_TEXT],
                "unit": "status",
                "flags": json_obj(flags),
                "raw_sha256": raws[page.treaty.mtdsg],
                "selector": json_obj({"file": page.treaty.url, **sel}),
            }
        )

    for page in pages:
        events: dict[str, list[WithdrawalEvent]] = defaultdict(list)
        for e in page.withdrawals:
            events[e.participant].append(e)
        seen: Counter[str] = Counter(p.name for p in page.participants)
        for p in page.participants:
            if seen[p.name] > 1:
                page.problems.append(f"participant listed {seen[p.name]} times: {p.name}")
                continue
            base = {"notes": p.notes, "declarations_or_reservations_link": p.declarations_link}
            for col, cell, ind in (
                ("Signature", p.signature, "action=signature"),
                ("Consent", p.consent, "action=consent"),
                ("Provisional application", p.provisional, "action=provisional_application"),
            ):
                if cell is None or cell.date is None:
                    continue
                add(
                    page,
                    p,
                    ind,
                    cell.date,
                    term_key(cell.term or ""),
                    {**base, "provider_text": cell.raw, "code": cell.code, "term": cell.term,
                     "bracketed_withdrawn": cell.bracketed,
                     "header": {"Signature": page.signature_header, "Consent": page.consent_header}.get(col, col)},
                    {"table": "tblgrid", "row": p.row, "column": col},
                )  # fmt: skip
            used: set[tuple[str, date]] = set()
            for e in events.get(p.name, []):
                ev_flags = {"basis": e.basis, "note": e.note, "revoked": e.revoked, "reference": e.reference,
                            "excerpt": e.excerpt, "parsed_from": "footnote table" if e.basis == "note_table"
                            else "footnote sentence (regex; check the note text)"}  # fmt: skip
                for kind, d in (("withdrawal_notification", e.notification), ("withdrawal_effect", e.effect)):
                    if d is None or (kind, d) in used:
                        continue
                    used.add((kind, d))
                    txt = (
                        "withdrawal_notification_revoked" if (e.revoked and kind == "withdrawal_notification") else kind
                    )
                    add(page, p, f"action={kind}", d, txt, ev_flags, {"notes_section": "divNotesAll", "note": e.note})
            st = participant_status(p, events.get(p.name, []), asof)
            add(
                page,
                p,
                "status",
                asof,
                st["status"],
                {**base, **{k: v for k, v in st.items() if k != "status"},
                 "as_listed_on": asof.isoformat(),
                 "meaning": "UNTC status list on the retrieval day: 'party' = a consent instrument is listed "
                 "(entry into force for the State may be later); 'withdrawn' = UNTC brackets the dates"},
                {"table": "tblgrid", "row": p.row},
            )  # fmt: skip
            for n in p.notes:
                if n in page.notes:
                    add(page, p, f"note={n}", asof, page.notes[n], {"note": n, "truncated": len(page.notes[n]) > MAX_TEXT},
                        {"notes_section": "divNotesAll", "note": n})  # fmt: skip
    return rows


def frame(rows: Sequence[Mapping[str, Any]]) -> Any:
    import polars as pl

    from reva.atlas.tables import ATLAS_SCHEMA, empty_atlas_frame

    if not rows:
        return empty_atlas_frame()
    return pl.DataFrame(list(rows), schema=ATLAS_SCHEMA)


# ---------------------------------------------------------------------------------------------- KG
KG_PROPERTIES = (
    {"pid": P_PARTY, "value_kind": "item", "reva_predicate": "treaty_party", "label_en": "has party (UNTC)",
     "label_fr": "a pour partie (UNTC)", "inverse_name": "party_to_treaty", "inverse_label_en": "party to",
     "inverse_label_fr": "partie à"},
    {"pid": P_FORMER, "value_kind": "item", "reva_predicate": "treaty_former_party",
     "label_en": "had as party, withdrawn (UNTC, date of effect not stated)",
     "label_fr": "a eu pour partie, retrait (UNTC, date d'effet non indiquée)", "inverse_name": "former_party_to_treaty",
     "inverse_label_en": "former party to", "inverse_label_fr": "ancienne partie à"},
    {"pid": P_MTDSG, "value_kind": "external_id", "reva_predicate": "untc_mtdsg_number",
     "label_en": "UN Treaty Collection MTDSG number", "label_fr": "numéro MTDSG (Collection des traités des Nations Unies)"},
)  # fmt: skip


def load_treaty_qids(path: Path = TREATY_QIDS_FILE) -> dict[str, str]:
    if not path.exists():
        return {}
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {e["mtdsg"]: e["qid"] for e in doc.get("entries", []) if e.get("qid") and e.get("status") == "verified"}


def resolve_country_qids(engine: Any, origin: str) -> tuple[dict[str, str], dict[str, Any]]:
    """ISO3 -> QID without guessing: WDHARVEST's ``country_qids.json`` when present, else unambiguous P298
    statements of the same origin already in the KG (an ISO3 claimed by several items stays unresolved)."""
    from sqlalchemy import text

    from reva.adapters.connectors.atlas_geo_common import load_country_qids

    q = load_country_qids()
    if q:
        return q, {"basis": "registry/entities/country_qids.json", "resolved": len(q)}
    if engine is None:
        return {}, {"basis": "none", "resolved": 0}
    with engine.connect() as c:
        has_kg = c.execute(text("SELECT to_regclass('kg.statement_current') IS NOT NULL")).scalar_one()
        if not has_kg:
            return {}, {"basis": "no kg schema", "resolved": 0}
        rows = c.execute(
            text("""SELECT subject_qid, upper(value->>'value') FROM kg.statement_current
                    WHERE property_pid = 'P298' AND origin = :o AND value IS NOT NULL"""),
            {"o": origin},
        ).all()
    claims: dict[str, set[str]] = defaultdict(set)
    for s, code in rows:
        if code:
            claims[code].add(s)
    out = {k: next(iter(v)) for k, v in claims.items() if len(v) == 1}
    return out, {
        "basis": "kg P298 statements",
        "resolved": len(out),
        "ambiguous": sorted(k for k, v in claims.items() if len(v) > 1),
    }


@dataclass
class KgBuild:
    entities: list[Any]
    statements: list[Any]
    report: dict[str, Any]


def kg_records(
    pages: Sequence[Page],
    raws: Mapping[str, str],
    *,
    asof: date,
    iso3_of: Mapping[str, str | None],
    country_qids: Mapping[str, str],
    treaty_qids: Mapping[str, str],
    revision: str,
) -> KgBuild:
    """``reva.kg.model`` records: P1891 signatures, ``untc:party`` / ``untc:former_party``, MTDSG numbers."""
    from reva.kg.model import KgEntity, KgStatement, KgTime

    ents: dict[str, Any] = {}
    sts: list[Any] = []
    nodes: Counter[str] = Counter()

    def day(d: date) -> KgTime:
        return KgTime(iso=d.isoformat(), precision=11)

    def state_node(name: str) -> str:
        iso3 = iso3_of.get(name)
        if iso3:
            q = country_qids.get(iso3)
            nodes["qid" if q else "iso3_node"] += 1
            return q or f"iso3:{iso3}"
        nid = f"untc:participant:{slug(name)}"
        nodes["untc_participant_node"] += 1
        ents.setdefault(nid, KgEntity(qid=nid, kind="treaty_participant", labels={"en": name},
                                      descriptions={"en": "participant listed by the UN Treaty Collection (not a "
                                                    "crosswalk country)"}, source_revision=revision))  # fmt: skip
        return nid

    for page in pages:
        t = page.treaty
        raw = raws[t.mtdsg]
        subj = treaty_qids.get(t.mtdsg) or f"untc:treaty:{t.mtdsg}"
        if subj.startswith("untc:"):
            ents[subj] = KgEntity(
                qid=subj, kind="treaty", labels={"en": page.title[:400] or t.label_en, "fr": t.label_fr[:400]},
                descriptions={"en": f"multilateral treaty deposited with the UN Secretary-General (MTDSG {t.mtdsg})",
                              "fr": f"traité multilatéral déposé auprès du Secrétaire général de l'ONU (MTDSG {t.mtdsg})"},
                aliases={"en": [t.label_en]}, source_revision=revision,
            )  # fmt: skip
        ref = {"untc_url": t.url, "raw_sha256": raw, "retrieved": asof.isoformat()}
        sts.append(KgStatement(subject_qid=subj, property_pid=P_MTDSG, value={"type": "external_id", "value": t.mtdsg},
                               statement_id=f"mtdsg:{t.mtdsg}", references=[ref], source_revision=revision,
                               raw_sha256=raw))  # fmt: skip
        events: dict[str, list[WithdrawalEvent]] = defaultdict(list)
        for e in page.withdrawals:
            events[e.participant].append(e)
        names = Counter(p.name for p in page.participants)
        for p in page.participants:
            if names[p.name] > 1:
                continue
            obj = state_node(p.name)
            pid_slug = slug(p.name)
            q_base: dict[str, Any] = {"untc_participant": p.name, "untc_row": p.row}
            if p.notes:
                q_base["untc_notes"] = p.notes
            if p.declarations_link:
                q_base["declarations_or_reservations"] = True
            if p.signature and p.signature.date:
                sts.append(KgStatement(
                    subject_qid=subj, property_pid=P_SIGNATORY, object_qid=obj, point_in_time=day(p.signature.date),
                    qualifiers={**q_base, "action": term_key(p.signature.term or "signature"),
                                "provider_text": p.signature.raw, "bracketed_withdrawn": p.signature.bracketed},
                    references=[{**ref, "selector": {"table": "tblgrid", "row": p.row, "column": "Signature"}}],
                    statement_id=f"sig:{t.mtdsg}:{pid_slug}", source_revision=revision, raw_sha256=raw,
                ))  # fmt: skip
            if p.consent and p.consent.date:
                st = participant_status(p, events.get(p.name, []), asof)
                q = {**q_base, "action": term_key(p.consent.term or ""), "provider_text": p.consent.raw,
                     "start_basis": "date of the consent instrument listed by UNTC (entry into force for the State may "
                     "be later)", "untc_status_on_retrieval": st["status"]}  # fmt: skip
                wd = [
                    {"notification": e.notification.isoformat() if e.notification else None,
                     "effect": e.effect.isoformat() if e.effect else None, "revoked": e.revoked, "note": e.note,
                     "basis": e.basis}
                    for e in events.get(p.name, [])
                ]  # fmt: skip
                if wd:
                    q["withdrawal_events"] = wd
                if "scheduled_withdrawal_effect" in st:
                    q["scheduled_withdrawal_effect"] = st["scheduled_withdrawal_effect"]
                end = None
                pid = P_PARTY
                if st["status"] == "withdrawn":
                    if st.get("withdrawal_effect"):
                        end = day(date.fromisoformat(st["withdrawal_effect"]))
                    else:
                        pid = P_FORMER
                        q["end_not_stated"] = "UNTC brackets this participant; no date of effect parsed from its notes"
                sts.append(KgStatement(
                    subject_qid=subj, property_pid=pid, object_qid=obj, start=day(p.consent.date), end=end,
                    qualifiers=q,
                    references=[{**ref, "selector": {"table": "tblgrid", "row": p.row, "column": "Consent"}}],
                    statement_id=f"party:{t.mtdsg}:{pid_slug}", source_revision=revision, raw_sha256=raw,
                ))  # fmt: skip
    report = {
        "entities": len(ents),
        "statements": dict(Counter(s.property_pid for s in sts)),
        "object_nodes": dict(nodes),
    }
    return KgBuild(list(ents.values()), sts, report)


def ensure_kg_properties(engine: Any) -> dict[str, Any]:
    from reva.adapters.connectors.wikidata import load_allowlist
    from reva.kg.harvest import kg_properties
    from reva.kg.model import KgProperty, register_properties

    props = [p for p in kg_properties(load_allowlist()) if p.pid == P_SIGNATORY]
    props += [KgProperty(**p) for p in KG_PROPERTIES]
    return register_properties(engine, props).__dict__


def load_kg(engine: Any, store: Any, build: KgBuild, *, raw_sha256s: Sequence[str], retrieved_at: datetime,
            origin: str, retract_missing: bool) -> dict[str, Any]:  # fmt: skip
    from sqlalchemy import text

    from reva.kg.model import MAX_STATEMENTS_PER_BATCH, load_batch, statement_key

    props = ensure_kg_properties(engine)
    retract: list[str] = []
    if retract_missing:
        with engine.connect() as c:
            current = set(c.execute(text("SELECT statement_key FROM kg.statement_current WHERE source_key = :s "
                                         "AND origin = :o"), {"s": SOURCE_KEY, "o": origin}).scalars())  # fmt: skip
        retract = sorted(current - {statement_key(s, SOURCE_KEY) for s in build.statements})
    reps = []
    for i in range(0, max(len(build.statements), 1), MAX_STATEMENTS_PER_BATCH):
        rep = load_batch(
            engine, store, entities=build.entities if i == 0 else (),
            statements=build.statements[i : i + MAX_STATEMENTS_PER_BATCH], source_key=SOURCE_KEY,
            raw_sha256s=sorted(set(raw_sha256s)), retrieved_at=retrieved_at, origin=origin,
            retract_keys=retract if i == 0 else (), parameters={"connector": NAME, "parser": PARSER_VERSION},
        )  # fmt: skip
        reps.append(rep.as_dict())
    keys = ("entities_inserted", "entities_unchanged", "statements_inserted", "statements_new_revision",
            "statements_unchanged", "statements_stale", "statements_conflicting", "retractions_inserted")  # fmt: skip
    rejected: Counter[str] = Counter()
    for r in reps:
        rejected.update(r["rejected_counts"])
    return {
        **{k: sum(r[k] for r in reps) for k in keys},
        "rejected": dict(rejected),
        "rejected_sample": [x for r in reps for x in r["rejected_sample"]][:20],
        "properties": {k: v for k, v in props.items() if not k.startswith("_")},
    }


def _kg_available(engine: Any) -> bool:
    from sqlalchemy import text

    with engine.connect() as c:
        return bool(c.execute(text("SELECT to_regclass('kg.statement') IS NOT NULL")).scalar_one())


# ---------------------------------------------------------------------------------------------- run
def page_summary(page: Page) -> dict[str, Any]:
    parties = sum(1 for p in page.participants if p.consent and not p.consent.bracketed)
    signed = sum(1 for p in page.participants if p.signature and not p.signature.bracketed)
    return {
        "title": page.title,
        "participants": len(page.participants),
        "listed_consents": parties,
        "listed_signatures": signed,
        "provider_parties": page.provider_parties,
        "provider_signatories": page.provider_signatories,
        "withdrawals": len(page.withdrawals),
        "problems": page.problems,
    }


def selected_treaties(run: Any) -> list[Treaty]:
    opt = (run.options or {}).get("treaties")
    if opt:
        keys = [k.strip() for k in (opt if isinstance(opt, list) else str(opt).split(",")) if k.strip()]
        bad = [k for k in keys if k not in BY_MTDSG]
        if bad:
            from reva.kernel.errors import ValidationProblem

            raise ValidationProblem("unknown treaty mtdsg number(s)", unknown=bad)
        return [BY_MTDSG[k] for k in keys]
    if run.origin == "test_fixture":
        return [BY_MTDSG[k] for k in FIXTURE_TREATIES]
    return list(TREATIES)


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """Terms page + one status page per treaty -> parse all (nothing archived on a parse failure) ->
    archive -> atlas table ``untc.treaty_status`` -> KG load (when the ``kg`` schema exists)."""
    from reva.atlas.tables import country_iso3
    from reva.kernel.errors import SourceResponseError

    if not run.wants(DATASET):
        return {"skipped": True}
    treaties = selected_treaties(run)
    terms_raw = await run.get(TERMS_URL, key="terms", media_types=("text/html",), max_bytes=2_000_000)
    terms = check_terms(terms_raw.body)
    fetched: list[tuple[Treaty, Any, Page]] = []
    for t in treaties:
        raw = await run.get(t.url, key=f"untc:{t.mtdsg}", media_types=("text/html",), max_bytes=MAX_PAGE_BYTES,
                            params={"mtdsg": t.mtdsg})  # fmt: skip
        try:
            page = parse_page(raw.body, t)
        except (UntcParseError, UnicodeDecodeError) as e:
            raise SourceResponseError(
                f"UNTC page {t.mtdsg} not parseable: {e}", retryable=False, kind="schema_drift"
            ) from e
        if run.origin == "live" and len(page.participants) < MIN_PARTICIPANTS_LIVE:
            raise SourceResponseError(
                f"UNTC page {t.mtdsg}: only {len(page.participants)} participants", retryable=False
            )
        fetched.append((t, raw, page))
    terms_sha = run.archive(terms_raw)
    raws = {t.mtdsg: run.archive(raw) for t, raw, _ in fetched}
    pages = [p for _, _, p in fetched]
    retrieved = max(r.retrieved_at for _, r, _ in fetched)
    asof = retrieved.astimezone(UTC).date()
    rows = atlas_rows(pages, raws, asof)
    names = sorted({p.name for pg in pages for p in pg.participants})
    iso3_of = {n: country_iso3(n, GEO_SCHEME) for n in names}
    reg = run.register(
        dataset=DATASET, frame=frame(rows), raw_sha256s=sorted(set(raws.values())), geo_scheme=GEO_SCHEME,
        coverage_note=f"UNTC status pages of {len(pages)} treaties retrieved {asof}; participant names mapped through "
        "registry/atlas/geo_aliases/a_treaties.yaml (scheme untc); European Union and other non-State participants "
        "kept unmapped. Not an open licence (UN Terms of Use).",
        rights={"licence": LICENCE, "open_licence": False, "provider": "UN OLA Treaty Section",
                "source_key": SOURCE_KEY, "terms_raw_sha256": terms_sha, "terms_check": terms},
    )  # fmt: skip
    out: dict[str, Any] = {
        "terms": terms,
        "atlas": {k: reg[k] for k in ("dataset_key", "row_count", "created")}
        | {"unmapped": reg.get("unmapped_provider_geo")},
        "treaties": {t.mtdsg: page_summary(p) for t, _, p in fetched},
        "unmapped_participants": sorted(n for n, v in iso3_of.items() if v is None),
    }
    if _kg_available(run.engine):
        qids, qinfo = resolve_country_qids(run.engine, run.origin)
        build = kg_records(pages, raws, asof=asof, iso3_of=iso3_of, country_qids=qids, treaty_qids=load_treaty_qids(),
                           revision=f"untc@{asof.isoformat()}")  # fmt: skip
        out["kg_build"] = build.report
        out["country_qids"] = qinfo
        out["kg"] = load_kg(run.engine, run.store, build, raw_sha256s=[*raws.values(), terms_sha],
                            retrieved_at=retrieved, origin=run.origin,
                            retract_missing=not (run.options or {}).get("treaties") and run.origin == "live")  # fmt: skip
    else:
        out["kg"] = {"skipped": "kg schema not present"}
    return out
