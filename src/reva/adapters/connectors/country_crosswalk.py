"""Country/economy crosswalk built from the providers' own reference lists (V2 step 1; V3 XWALK).

Inputs (archived raw bytes under ``registry/reference/``, digests in
``registry/reference/manifest.yaml`` and in the crosswalk itself):

* ISO 3166-1 as mirrored by Debian ``iso-codes`` 4.20.1 (``data/iso_3166-1.json``, tag
  ``v4.20.1``): the 249 officially assigned alpha-2/alpha-3/numeric codes and ISO's English
  short names (``name``, kept verbatim as ``iso_name``). Its French translation
  (``iso_3166-1/fr.po``) gives ``label_fr`` for the entities whose label comes from ISO.
* UNSD M49 overview (``unstats.un.org/unsd/methodology/m49/overview/``): M49 codes, regions,
  and the ISO codes UNSD lists (all of ISO 3166-1 except Taiwan, TWN/158; the builder checks
  that every M49 ISO code, alpha-2 and numeric agrees with the ISO list and fails otherwise).
* UNSD M49 home page (``unstats.un.org/unsd/methodology/m49/``): its FAQ notes on Taiwan
  Province of China and on Kosovo, quoted verbatim in the CHN/TWN and SRB/XKX ``aggregation_notes``.
* UN Member States and Non-member States pages (``www.un.org/en/about-us/...``): the 193
  member states with their dates of admission and the two non-member observer states.
* UN Comtrade ``Reporters.json`` and ``partnerAreas.json``: provider reporter and
  partner codes (M49-based, but not M49: France 251 vs M49 250, USA 842 vs 840).
* World Bank ``/v2/country`` (all 296 rows): economies and aggregates.

Output: ``registry/entities/country_crosswalk.json``, one entity per ISO 3166-1 alpha-3 code
(249), plus World Bank economies without an ISO 3166 code (``CHI`` Channel Islands, ``XKX``
Kosovo: keyed by the World Bank code, flagged ``not_iso3166``). World Bank aggregates (region
"Aggregates": regions, income groups, lending groups) are excluded and listed.

Status fields are copied from the sources, never inferred: ``in_iso3166_1``, ``iso_name`` (ISO's
own short name, e.g. "Taiwan, Province of China"), ``un_membership`` (``member_state`` /
``non_member_observer_state`` / ``not_listed`` on the UN pages), ``un_name``,
``un_admission_date``. ``label`` is the display label: the M49 name, else the World Bank name,
else ISO's ``common_name`` (Debian iso-codes field), else ISO's name (``label_source`` says which).

Mapping rules (deterministic, conservative; nothing is guessed):

1. A Comtrade *reporter* code maps to an entity only when it is not a group, is
   valid at some point in the coverage window, its label is not a "nes" area,
   its provider ISO3 field is an ISO 3166-1 alpha-3 code (1.0.0: one listed by M49), and it is
   the only such reporter for that ISO3. Anything else is listed under
   ``unmapped_provider_codes`` with flags (``group``, ``nes_area``,
   ``historical``, ``provider_iso3_not_iso3166``, ``ambiguous_iso3``) and is
   never mapped to an ISO3 -- e.g. 490 "Other Asia, nes", 97 "European Union",
   899 "Areas, nes" (partner), "(...1990)" former entities.
2. A reporter whose provider note lists more than its label (``"France,
   Monaco"``, ``"USA, Puerto Rico and US Virgin Islands"``, ``"Switzerland,
   Liechtenstein"``, ``"Norway, Svalbard and Jan Mayen"``) is mapped, as the
   reviewed FRA/USA aliases already are, but flagged
   ``comtrade_territory_note_lists_additional_areas`` with the note recorded:
   its trade figures cover a wider territory than the World Bank economy.
3. Partner codes are recorded per entity for information only (same rules as
   reporters; the provider ISO3 field of partner "nes" areas is unreliable, e.g.
   492 "Europe EU, nes" carries "MCO"). They are not seeded as aliases: bilateral
   rows keep the provider partner code and resolve to provider-declared partner
   entities (``reva.cli.seed``), so no partner territory is merged into a country.
4. Taiwan: the UNSD M49 FAQ says that within M49 Taiwan Province of China is considered part
   of China (156) and that 158 can be used for strictly statistical purposes (likewise Kosovo /
   Serbia 688 / 412). TWN and CHN (XKX and SRB) carry the flag ``aggregation_overlap`` and that
   note verbatim in ``aggregation_notes``: a provider's "China" may or may not include Taiwan.
   REVA never sums TWN into CHN nor splits CHN; each provider row keeps its own territorial
   definition. Comtrade 490 "Other Asia, nes" is a "nes" area and is never mapped
   to TWN; partner 158 maps only because the provider labels it "Taiwan, Province of China".

Entities defined in ``registry/entities/countries.yaml`` (human-reviewed) keep
their reviewed aliases; the crosswalk records them with ``reviewed_entry: true``
and the seed does not add crosswalk aliases for them.

CLI (offline; this module makes no network request)::

    python -m reva.adapters.connectors.country_crosswalk build   # rebuild the JSON from the archives
    python -m reva.adapters.connectors.country_crosswalk verify  # digests + rebuild equality

Refreshing the raw archives is a maintainer step outside ``src/reva`` (every live
provider path inside it must be budgeted, invariant 30): download the
``SOURCES`` URLs, store them under ``registry/reference/`` (the HTML pages as
``gzip.compress(raw, mtime=0)``), update the sha256/retrieved_at entries of
``registry/reference/manifest.yaml``, then ``build`` and review the diff.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from html import unescape as html_unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import yaml

REGISTRY_ROOT = Path(__file__).resolve().parents[4] / "registry"
REFERENCE_DIR = REGISTRY_ROOT / "reference"
CROSSWALK_PATH = REGISTRY_ROOT / "entities" / "country_crosswalk.json"
REVIEWED_ENTITIES_PATH = REGISTRY_ROOT / "entities" / "countries.yaml"

SCHEMA = "reva-country-crosswalk/1"
#: 1.0.0 (V2, ALLC): M49 + World Bank, 250 entities. 1.1.0 (V3, XWALK): ISO 3166-1 (adds TWN),
#: ISO names, UN membership, CHN/TWN aggregation note; 251 entities.
CROSSWALK_VERSION = "1.1.0"
#: Alias rows seeded from the crosswalk carry this mapping_version (world.alias). It versions the
#: alias rules, not the file: 1.1.0 adds an entity (TWN, alias iso3166_alpha3 under the same rule)
#: and changes no existing alias, so re-seeding must not duplicate every alias row.
ALIAS_MAPPING_VERSION = "crosswalk-1.0.0"
WINDOW = (2019, 2024)
ISO_CODES_RAW = "https://salsa.debian.org/iso-codes-team/iso-codes/-/raw/v4.20.1"


@dataclass(frozen=True)
class SourceFile:
    name: str
    file: str  # archive file under registry/reference/
    url: str
    gzip: bool = False


SOURCES: tuple[SourceFile, ...] = (
    SourceFile(
        "unsd_m49", "unsd_m49_overview.html.gz", "https://unstats.un.org/unsd/methodology/m49/overview/", gzip=True
    ),
    SourceFile(
        "comtrade_reporters",
        "comtrade_Reporters.json",
        "https://comtradeapi.un.org/files/v1/app/reference/Reporters.json",
    ),
    SourceFile(
        "comtrade_partners",
        "comtrade_partnerAreas.json",
        "https://comtradeapi.un.org/files/v1/app/reference/partnerAreas.json",
    ),
    SourceFile(
        "worldbank_countries", "worldbank_country.json", "https://api.worldbank.org/v2/country?format=json&per_page=400"
    ),
    # V3 XWALK (crosswalk 1.1.0)
    SourceFile(
        "iso3166_1",
        "iso-codes_4.20.1_iso_3166-1.json",
        f"{ISO_CODES_RAW}/data/iso_3166-1.json",
    ),
    SourceFile(
        "iso3166_1_fr",
        "iso-codes_4.20.1_iso_3166-1_fr.po",
        f"{ISO_CODES_RAW}/iso_3166-1/fr.po",
    ),
    SourceFile("unsd_m49_home", "unsd_m49_home.html.gz", "https://unstats.un.org/unsd/methodology/m49/", gzip=True),
    SourceFile(
        "un_member_states", "un_member_states.html.gz", "https://www.un.org/en/about-us/member-states", gzip=True
    ),
    SourceFile(
        "un_non_member_states",
        "un_non_member_states.html.gz",
        "https://www.un.org/en/about-us/non-member-states",
        gzip=True,
    ),
)
#: Version of the ISO 3166-1 mirror (Debian iso-codes) recorded in the crosswalk.
ISO_CODES_VERSION = {
    "package": "Debian iso-codes",
    "version": "4.20.1",
    "tag": "v4.20.1",
    "commit": "74623235ae99f6e835e1e465017ce4c544ae6b53",
    "licence": "LGPL-2.1-or-later",
}


class CrosswalkError(ValueError):
    """The archived sources or the crosswalk file are inconsistent (fails closed)."""


# --------------------------------------------------------------------------- parsing
class _M49Table(HTMLParser):
    """Rows of the English M49 table (``<table id="downloadTableEN">``)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_table = False
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "table" and (a.get("id") or "").strip() == "downloadTableEN":
            self.in_table = True
        elif self.in_table and tag == "tr":
            self._row = []
        elif self.in_table and tag == "td" and self._row is not None:
            self._cell = ""

    def handle_endtag(self, tag: str) -> None:
        if not self.in_table:
            return
        if tag == "table":
            self.in_table = False
        elif tag == "td" and self._cell is not None and self._row is not None:
            self._row.append(" ".join(self._cell.split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell += data


M49_COLUMNS = ("Country or Area", "M49 Code", "ISO-alpha2 Code", "ISO-alpha3 Code", "Region Name", "Sub-region Name")


def parse_m49(html: bytes) -> list[dict[str, str]]:
    p = _M49Table()
    p.feed(html.decode("utf-8"))
    if not p.rows:
        raise CrosswalkError("M49 overview: English table not found (page structure changed)")
    header, body = p.rows[0], p.rows[1:]
    missing = [c for c in M49_COLUMNS if c not in header]
    if missing:
        raise CrosswalkError(f"M49 overview: columns {missing} absent (page structure changed)")
    rows = [dict(zip(header, r, strict=False)) for r in body]
    if len(rows) < 200:
        raise CrosswalkError(f"M49 overview: only {len(rows)} rows parsed; refusing a partial table")
    return rows


def _results(data: bytes, what: str) -> list[dict[str, Any]]:
    doc = json.loads(data)
    if not isinstance(doc, dict) or not isinstance(doc.get("results"), list):
        raise CrosswalkError(f"{what}: expected an object with a 'results' list")
    return doc["results"]


def parse_worldbank(data: bytes) -> list[dict[str, Any]]:
    doc = json.loads(data)
    if not (isinstance(doc, list) and len(doc) == 2 and isinstance(doc[0], dict) and isinstance(doc[1], list)):
        raise CrosswalkError("World Bank country list: unexpected payload shape")
    header, rows = doc
    if int(header.get("pages", 0)) != 1 or int(header.get("total", -1)) != len(rows):
        raise CrosswalkError(f"World Bank country list is paged or truncated (header {header}, {len(rows)} rows)")
    return rows


def parse_iso3166(data: bytes) -> dict[str, dict[str, str]]:
    """ISO 3166-1 entries of Debian iso-codes ``iso_3166-1.json``, keyed by alpha-3."""
    doc = json.loads(data)
    rows = doc.get("3166-1") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        raise CrosswalkError("iso_3166-1.json: expected an object with a '3166-1' list")
    out: dict[str, dict[str, str]] = {}
    for r in rows:
        a3, a2, num, name = (str(r.get(k) or "") for k in ("alpha_3", "alpha_2", "numeric", "name"))
        if len(a3) != 3 or len(a2) != 2 or not num.isdigit() or not name:
            raise CrosswalkError(f"iso_3166-1.json: incomplete entry {r!r}")
        if a3 in out:
            raise CrosswalkError(f"iso_3166-1.json lists {a3} twice")
        out[a3] = r
    if len(out) < 240:
        raise CrosswalkError(f"iso_3166-1.json: only {len(out)} codes; refusing a partial list")
    return out


def parse_po(data: bytes) -> dict[str, str]:
    """msgid -> msgstr of a gettext PO file (fuzzy and untranslated entries skipped)."""

    def unq(line: str) -> str:
        return json.loads(line)  # PO strings use C escapes compatible with JSON for this file

    out: dict[str, str] = {}
    for block in data.decode("utf-8").split("\n\n"):
        lines = [x.strip() for x in block.splitlines() if x.strip()]
        if any(x.startswith("#,") and "fuzzy" in x for x in lines):
            continue
        cur: str | None = None
        parts: dict[str, str] = {}
        for x in lines:
            if x.startswith("#"):
                continue
            if x.startswith(("msgid ", "msgstr ")):
                cur, _, rest = x.partition(" ")
                parts[cur] = unq(rest)
            elif x.startswith('"') and cur is not None:
                parts[cur] += unq(x)
        if parts.get("msgid") and parts.get("msgstr"):
            out[parts["msgid"]] = parts["msgstr"]
    return out


def _text(fragment: str) -> str:
    return " ".join(html_unescape(re.sub(r"<[^>]+>", "", fragment)).split())


def parse_un_members(page: bytes) -> list[dict[str, str]]:
    """Cards of the UN "Member States" page: name, flag code (CSS class), date of admission."""
    text = page.decode("utf-8")
    out = []
    for card in text.split('<div class="card-body')[1:]:
        flag = re.search(r'class="flags flags-([a-z]{2})"', card)
        name = re.search(r"<h2[^>]*>(.*?)</h2>", card, re.S)
        admitted = re.search(r"Date of Admission:\s*(\d{2})-(\d{2})-(\d{4})", card)
        if not (flag and name and admitted):
            raise CrosswalkError("UN Member States page: a card without flag, name or date (page structure changed)")
        d, m, y = admitted.groups()
        out.append({"name": _text(name.group(1)), "flag": flag.group(1).upper(), "admitted": f"{y}-{m}-{d}"})
    if len(out) < 190:
        raise CrosswalkError(f"UN Member States page: only {len(out)} cards parsed; refusing a partial list")
    return out


UN_OBSERVER_HEADING = "Non-Member States having received a standing invitation to participate as Observers"


def parse_un_observers(page: bytes) -> list[str]:
    """``<h3>`` names in the observer-states section of the UN "Non-member States" page."""
    text = page.decode("utf-8")
    i = text.find(UN_OBSERVER_HEADING)
    if i < 0:
        raise CrosswalkError("UN Non-member States page: observer heading not found (page structure changed)")
    section = text[i : text.find("</div>", i)]
    names = [_text(h) for h in re.findall(r"<h3[^>]*>(.*?)</h3>", section, re.S)]
    if not names:
        raise CrosswalkError("UN Non-member States page: no observer state listed (page structure changed)")
    return names


#: UNSD M49 FAQ "Q: Why is <area> not shown in the M49?" -> (area entity, entity M49 counts it in).
M49_NOT_SHOWN = {"Taiwan Province of China": ("TWN", "CHN"), "Kosovo": ("XKX", "SRB")}


def m49_not_shown_notes(page: bytes) -> dict[str, dict[str, str]]:
    """UNSD M49 FAQ answers for the areas of ``M49_NOT_SHOWN``, verbatim (question and answer)."""
    text = page.decode("utf-8")
    faq = {}
    for m in re.finditer(r"<strong>(Q:[^<]*)</strong>.*?<div class=\"panel-body\">(.*?)</div>", text, re.S):
        q = _text(m.group(1))
        hit = re.fullmatch(r"Q: Why is (.+) not shown in the M49\?", q)
        if hit:
            faq[hit.group(1)] = {"question": q, "answer": _text(m.group(2)).removeprefix("A: ")}
    out = {}
    for area in M49_NOT_SHOWN:
        note = faq.get(area)
        if note is None or "considered part of" not in note["answer"]:
            raise CrosswalkError(f"UNSD M49 page: FAQ note on {area} not found (page changed)")
        out[area] = note
    return out


#: UN page labels that are neither the M49 nor an ISO name; each must also agree with the
#: card's flag code (ISO alpha-2), checked in build().
UN_NAME_OVERRIDES = {
    "Bahamas (The)": "BHS",
    "China (the People's Republic of)": "CHN",
    "Gambia (Republic of The)": "GMB",
    "Guinea Bissau": "GNB",
}


def _norm_name(s: str) -> str:
    return " ".join(s.replace("’", "'").casefold().split())


def _year(value: Any) -> int | None:
    return int(str(value)[:4]) if value else None


def _active_in_window(effective: Any, expired: Any) -> bool:
    start, end = _year(effective), _year(expired)
    return (start is None or start <= WINDOW[1]) and (end is None or end >= WINDOW[0])


def _is_nes(label: str) -> bool:
    words = label.lower().replace(",", " ").split()
    return "nes" in words


def _code_flags(
    *, label: str, iso3_field: str, is_group: bool, effective: Any, expired: Any, iso3_known: set[str]
) -> list[str]:
    flags = []
    if is_group:
        flags.append("group")
    if _is_nes(label):
        flags.append("nes_area")
    if not _active_in_window(effective, expired):
        flags.append("historical")
    if (iso3_field or "").strip() not in iso3_known:
        flags.append("provider_iso3_not_iso3166")
    return flags


# --------------------------------------------------------------------------- build
def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def read_source(src: SourceFile, root: Path = REFERENCE_DIR) -> bytes:
    data = (root / src.file).read_bytes()
    return gzip.decompress(data) if src.gzip else data


def _reviewed_keys(path: Path = REVIEWED_ENTITIES_PATH) -> set[str]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {e["key"] for e in doc["entities"] if e.get("kind") == "entity.country"}


def build(raw: dict[str, bytes], *, retrieved: dict[str, str], reviewed_keys: set[str]) -> dict[str, Any]:
    """Pure: the crosswalk document from the raw reference payloads (``SOURCES``)."""
    m49 = parse_m49(raw["unsd_m49"])
    reporters = _results(raw["comtrade_reporters"], "Comtrade Reporters.json")
    partners = _results(raw["comtrade_partners"], "Comtrade partnerAreas.json")
    wb_rows = parse_worldbank(raw["worldbank_countries"])
    iso = parse_iso3166(raw["iso3166_1"])
    iso_fr = parse_po(raw["iso3166_1_fr"])
    m49_notes = m49_not_shown_notes(raw["unsd_m49_home"])
    un_members = parse_un_members(raw["un_member_states"])
    un_observers = parse_un_observers(raw["un_non_member_states"])

    by_iso3: dict[str, dict[str, str]] = {}
    for r in m49:
        iso3 = r["ISO-alpha3 Code"].strip()
        if not iso3:
            continue
        if iso3 in by_iso3:
            raise CrosswalkError(f"M49 overview lists ISO3 {iso3} twice")
        by_iso3[iso3] = r
    # M49 lists ISO codes; they must be ISO 3166-1 codes with the same alpha-2 and numeric code.
    for iso3, r in by_iso3.items():
        i = iso.get(iso3)
        if i is None:
            raise CrosswalkError(f"M49 lists ISO3 {iso3}, absent from ISO 3166-1 (iso-codes)")
        if (i["alpha_2"], int(i["numeric"])) != (r["ISO-alpha2 Code"].strip(), int(r["M49 Code"])):
            raise CrosswalkError(f"{iso3}: ISO 3166-1 {i['alpha_2']}/{i['numeric']} != M49 listing")
    iso3_known = set(iso)

    # UN membership: page labels -> ISO3 by M49/ISO names (or a listed override), checked
    # against the card's flag code (an ISO alpha-2) so a name match can never pick another code.
    name_index: dict[str, set[str]] = defaultdict(set)
    for iso3, r in by_iso3.items():
        name_index[_norm_name(r["Country or Area"])].add(iso3)
    for iso3, i in iso.items():
        for f in ("name", "official_name", "common_name"):
            if i.get(f):
                name_index[_norm_name(i[f])].add(iso3)
    by_alpha2 = {i["alpha_2"]: a3 for a3, i in iso.items()}

    def un_iso3(name: str, flag: str | None) -> str:
        hits = {UN_NAME_OVERRIDES[name]} if name in UN_NAME_OVERRIDES else name_index.get(_norm_name(name), set())
        if len(hits) != 1:
            raise CrosswalkError(f"UN page name {name!r} matches {sorted(hits)} (add a reviewed override)")
        (hit,) = hits
        if flag is not None and by_alpha2.get(flag) != hit:
            raise CrosswalkError(f"UN page {name!r}: name gives {hit}, flag code {flag} gives {by_alpha2.get(flag)}")
        return hit

    un_status: dict[str, dict[str, Any]] = {}
    for m in un_members:
        k = un_iso3(m["name"], m["flag"])
        if k in un_status:
            raise CrosswalkError(f"UN Member States page lists {k} twice")
        un_status[k] = {"un_membership": "member_state", "un_name": m["name"], "un_admission_date": m["admitted"]}
    for name in un_observers:
        k = un_iso3(name, None)
        if k in un_status:
            raise CrosswalkError(f"{k} is both a UN member state and an observer state")
        un_status[k] = {"un_membership": "non_member_observer_state", "un_name": name, "un_admission_date": None}
    not_listed = {"un_membership": "not_listed", "un_name": None, "un_admission_date": None}

    # CHN/TWN and SRB/XKX aggregation semantics: UNSD's own words, recorded on both entities.
    overlap: dict[str, list[dict[str, str]]] = defaultdict(list)
    for area, (part, whole) in M49_NOT_SHOWN.items():
        for key, other in ((part, whole), (whole, part)):
            overlap[key].append(
                {
                    "with": other,
                    "source": "unsd_m49_home",
                    "question": m49_notes[area]["question"],
                    "quote": m49_notes[area]["answer"],
                    "reva_policy": (
                        f"REVA never sums {part} into {whole} nor splits {whole}: a provider's {whole} may or "
                        f"may not include the area of {part} (see that provider's notes); each row keeps its "
                        "provider geography and territorial definition."
                    ),
                }
            )

    wb_economies = {r["id"]: r for r in wb_rows if ((r.get("region") or {}).get("value") or "").strip() != "Aggregates"}
    wb_aggregates = sorted(r["id"] for r in wb_rows if r["id"] not in wb_economies)

    unmapped: list[dict[str, Any]] = []

    def candidates(
        rows: list[dict[str, Any]],
        code_f: str,
        iso_f: str,
        note_f: str,
        provider: str,
    ) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            label = str(r.get("text") or "")
            iso = str(r.get(iso_f) or "").strip()
            flags = _code_flags(
                label=label,
                iso3_field=iso,
                is_group=bool(r.get("isGroup")),
                effective=r.get("entryEffectiveDate"),
                expired=r.get("entryExpiredDate"),
                iso3_known=iso3_known,
            )
            if flags:
                unmapped.append(
                    {
                        "provider": provider,
                        "code": str(r[code_f]),
                        "label": label,
                        "provider_note": r.get(note_f),
                        "provider_iso3_field": iso or None,
                        "valid_from": r.get("entryEffectiveDate"),
                        "valid_to": r.get("entryExpiredDate"),
                        "flags": flags,
                        "mapped_iso3": None,
                    }
                )
                continue
            out[iso].append(r)
        for iso, rs in list(out.items()):
            if len(rs) > 1:
                for r in rs:
                    unmapped.append(
                        {
                            "provider": provider,
                            "code": str(r[code_f]),
                            "label": r.get("text"),
                            "provider_note": r.get(note_f),
                            "provider_iso3_field": iso,
                            "valid_from": r.get("entryEffectiveDate"),
                            "valid_to": r.get("entryExpiredDate"),
                            "flags": ["ambiguous_iso3"],
                            "mapped_iso3": None,
                        }
                    )
                del out[iso]
        return out

    rep_by_iso = candidates(reporters, "reporterCode", "reporterCodeIsoAlpha3", "reporterNote", "comtrade_reporter")
    par_by_iso = candidates(partners, "PartnerCode", "PartnerCodeIsoAlpha3", "partnerNote", "comtrade_partner")

    keys = sorted(set(iso) | set(wb_economies))
    entities: list[dict[str, Any]] = []
    for key in keys:
        m = by_iso3.get(key)
        i = iso.get(key)
        wb = wb_economies.get(key)
        rep = (rep_by_iso.get(key) or [None])[0]
        par = (par_by_iso.get(key) or [None])[0]
        flags: list[str] = []
        if i is None:
            flags.append("not_iso3166")  # World Bank-specific economy code (CHI, XKX)
        elif m is None:
            flags.append("not_listed_in_un_m49")  # TWN (see aggregation_notes)
        if wb is None:
            flags.append("no_worldbank_economy")
        if rep is None:
            flags.append("no_comtrade_reporter")
        if key in overlap:
            flags.append("aggregation_overlap")
        aliases: list[dict[str, str]] = []
        if i is not None:
            aliases.append({"provider": "iso3166_alpha3", "key": key})
        if m is not None:
            aliases.append({"provider": "un_m49", "key": str(int(m["M49 Code"]))})
        if wb is not None:
            aliases.append({"provider": "worldbank", "key": wb["id"]})
        if m is not None:
            label, label_source = m["Country or Area"], "un_m49"
        elif wb is not None:
            label, label_source = wb["name"], "worldbank"
        elif i is not None and i.get("common_name"):
            label, label_source = i["common_name"], "iso3166_1_common_name"
        else:
            assert i is not None
            label, label_source = i["name"], "iso3166_1_name"
        label_fr = None
        if label_source.startswith("iso3166_1"):
            label_fr = iso_fr.get(label)
            if not label_fr:
                raise CrosswalkError(f"{key}: no French translation of the ISO label {label!r} in iso-codes fr.po")
        entry: dict[str, Any] = {
            "key": key,
            "kind": "entity.country",
            "label": label.strip(),
            "iso3": key if i else None,
            "iso2": i["alpha_2"] if i else None,
            "m49": str(int(m["M49 Code"])) if m else None,
            "m49_region": (m.get("Region Name") or None) if m else None,
            "m49_subregion": (m.get("Sub-region Name") or None) if m else None,
            "worldbank": wb["id"] if wb else None,
            "worldbank_name": wb["name"].strip() if wb else None,
            "comtrade_reporter": None,
            "comtrade_partner": str(par["PartnerCode"]) if par else None,
            "reviewed_entry": key in reviewed_keys,
            "label_source": label_source,
            "label_fr": label_fr,
            "in_iso3166_1": i is not None,
            "iso_numeric": i["numeric"] if i else None,
            "iso_name": i["name"] if i else None,
            **un_status.get(key, not_listed),
            "aggregation_notes": overlap.get(key, []),
        }
        if rep is not None:
            code = str(rep["reporterCode"])
            note = rep.get("reporterNote")
            entry["comtrade_reporter"] = code
            entry["comtrade_reporter_label"] = rep.get("text")
            entry["comtrade_reporter_note"] = note
            aliases.append({"provider": "comtrade_reporter", "key": code})
            if m is not None and str(int(m["M49 Code"])) != code:
                flags.append("comtrade_code_differs_from_m49")
            label = str(rep.get("text") or "")
            if note and note != label and str(note).startswith(label + ", "):
                flags.append("comtrade_territory_note_lists_additional_areas")
        entry["flags"] = flags
        entry["aliases"] = aliases
        entities.append(entry)

    unmapped.sort(key=lambda u: (u["provider"], int(u["code"]) if str(u["code"]).isdigit() else 0, u["code"]))
    body = {"entities": entities, "unmapped_provider_codes": unmapped, "excluded_worldbank_aggregates": wb_aggregates}
    sources = [
        {
            "name": s.name,
            "file": s.file,
            "url": s.url,
            "sha256": _sha256(raw[s.name]),
            "archive_gzip": s.gzip,
            "retrieved_at": retrieved.get(s.name),
        }
        for s in SOURCES
    ]
    missing_un = sorted(set(un_status) - {e["key"] for e in entities})
    if missing_un:
        raise CrosswalkError(f"UN members/observers without a crosswalk entity: {missing_un}")
    stats = {
        "entities": len(entities),
        "iso3166_entities": sum(1 for e in entities if e["iso3"]),
        "iso3166_1_codes": len(iso),
        "not_iso3166_entities": sum(1 for e in entities if not e["in_iso3166_1"]),
        "not_listed_in_un_m49": sum(1 for e in entities if "not_listed_in_un_m49" in e["flags"]),
        "un_member_states": sum(1 for e in entities if e["un_membership"] == "member_state"),
        "un_non_member_observer_states": sum(1 for e in entities if e["un_membership"] == "non_member_observer_state"),
        "worldbank_economies": sum(1 for e in entities if e["worldbank"]),
        "worldbank_aggregates_excluded": len(wb_aggregates),
        "comtrade_reporters_mapped": sum(1 for e in entities if e["comtrade_reporter"]),
        "comtrade_reporter_codes_unmapped": sum(1 for u in unmapped if u["provider"] == "comtrade_reporter"),
        "comtrade_partner_codes_unmapped": sum(1 for u in unmapped if u["provider"] == "comtrade_partner"),
        "with_worldbank_and_comtrade": sum(1 for e in entities if e["worldbank"] and e["comtrade_reporter"]),
    }
    return {
        "schema": SCHEMA,
        "version": CROSSWALK_VERSION,
        "review_status": "machine_derived_pending_maintainer_review",
        "review_note": "agent-built (V2 ALLC, V3 XWALK); not reviewed by a human maintainer",
        "built_by": "reva.adapters.connectors.country_crosswalk (V2 step 1, agent ALLC; V3, agent XWALK)",
        "window": {"from": WINDOW[0], "to": WINDOW[1]},
        "iso3166_1_mirror": ISO_CODES_VERSION,
        "sources": sources,
        "rules": [
            "entity per ISO 3166-1 alpha-3 code (Debian iso-codes mirror, 249 codes), plus World Bank "
            "economies without one (not_iso3166: CHI, XKX)",
            "every M49-listed ISO code must agree with ISO 3166-1 (alpha-2, numeric); ISO codes M49 does not "
            "list are flagged not_listed_in_un_m49 (TWN) and have no M49 code, region or un_m49 alias",
            "status fields are copied from the sources: iso_name (ISO short name, verbatim), un_membership / "
            "un_name / un_admission_date (UN Member States and Non-member States pages); nothing is inferred",
            "label: M49 name, else World Bank name, else ISO common_name, else ISO name (label_source); "
            "label_fr only for ISO-derived labels (iso-codes French translation)",
            "CHN/TWN and SRB/XKX carry aggregation_overlap with the UNSD M49 FAQ note (verbatim): within M49 the "
            "area (158, 412) is counted in CHN/SRB; REVA never sums or splits them",
            "World Bank rows with region 'Aggregates' are excluded (regions, income and lending groups)",
            "a Comtrade code maps only if: not a group, not a 'nes' area, valid in the window, provider ISO3 "
            "field is an ISO 3166-1 alpha-3 code, and unique for that ISO3; otherwise it is listed unmapped "
            "(490 'Other Asia, nes' is never Taiwan)",
            "a reporter whose provider note lists more areas than its label is mapped but flagged "
            "comtrade_territory_note_lists_additional_areas",
            "partner codes are informational only; bilateral rows keep provider partner codes",
            "entities in countries.yaml keep their human-reviewed aliases (reviewed_entry)",
        ],
        "stats": stats,
        **body,
        "content_sha256": _sha256(_canonical(body)),
    }


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def dumps(doc: dict[str, Any]) -> str:
    return json.dumps(doc, indent=1, ensure_ascii=False, sort_keys=False) + "\n"


def _manifest_retrieved() -> dict[str, str]:
    manifest = yaml.safe_load((REFERENCE_DIR / "manifest.yaml").read_text(encoding="utf-8")) or []
    by_file = {m["file"]: m for m in manifest}
    out = {}
    for s in SOURCES:
        m = by_file.get(s.file)
        if m is None:
            raise CrosswalkError(f"registry/reference/manifest.yaml has no entry for {s.file}")
        out[s.name] = str(m.get("retrieved_at"))
    return out


def build_from_archive(root: Path = REFERENCE_DIR) -> dict[str, Any]:
    raw = {s.name: read_source(s, root) for s in SOURCES}
    return build(raw, retrieved=_manifest_retrieved(), reviewed_keys=_reviewed_keys())


# --------------------------------------------------------------------------- loading
def verify_sources(doc: dict[str, Any], root: Path = REFERENCE_DIR) -> None:
    """Each archived raw file must match the digest recorded in the crosswalk and the manifest."""
    manifest = yaml.safe_load((root / "manifest.yaml").read_text(encoding="utf-8")) or []
    by_file = {m["file"]: m for m in manifest}
    for s in doc["sources"]:
        src = next((x for x in SOURCES if x.name == s["name"]), None)
        if src is None:
            raise CrosswalkError(f"unknown crosswalk source {s['name']!r}")
        got = _sha256(read_source(src, root))
        if got != s["sha256"]:
            raise CrosswalkError(f"archived {src.file} sha256 {got[:12]} != crosswalk {s['sha256'][:12]}")
        m = by_file.get(src.file)
        if m is None or m.get("sha256") != got:
            raise CrosswalkError(f"registry/reference/manifest.yaml digest for {src.file} does not match the archive")
    body = {k: doc[k] for k in ("entities", "unmapped_provider_codes", "excluded_worldbank_aggregates")}
    if _sha256(_canonical(body)) != doc.get("content_sha256"):
        raise CrosswalkError("crosswalk content_sha256 does not match its entities (edited by hand?)")


@lru_cache(maxsize=4)
def _load_cached(path: str, mtime_ns: int) -> dict[str, Any]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("schema") != SCHEMA:
        raise CrosswalkError(f"{path}: schema {doc.get('schema')!r} != {SCHEMA}")
    verify_sources(doc)
    return doc


def load_crosswalk(path: Path | None = None) -> dict[str, Any]:
    """The verified crosswalk document (raw archive digests and content digest checked)."""
    p = path or CROSSWALK_PATH
    return _load_cached(str(p), p.stat().st_mtime_ns)


def crosswalk_sha256(path: Path | None = None) -> str:
    return _sha256((path or CROSSWALK_PATH).read_bytes())


def comtrade_reporters(doc: dict[str, Any] | None = None) -> dict[str, int]:
    """Entity key -> mapped Comtrade reporter code (sorted by key)."""
    doc = doc or load_crosswalk()
    return {e["key"]: int(e["comtrade_reporter"]) for e in doc["entities"] if e.get("comtrade_reporter")}


def worldbank_economies(doc: dict[str, Any] | None = None) -> dict[str, str]:
    """Entity key -> World Bank economy code."""
    doc = doc or load_crosswalk()
    return {e["key"]: e["worldbank"] for e in doc["entities"] if e.get("worldbank")}


def seed_entities(doc: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Entities to seed from the crosswalk (those not already in countries.yaml)."""
    doc = doc or load_crosswalk()
    return [e for e in doc["entities"] if not e["reviewed_entry"]]


# --------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "verify"
    if cmd == "build":
        doc = build_from_archive()
        CROSSWALK_PATH.write_text(dumps(doc), encoding="utf-8")
        print(json.dumps(doc["stats"], indent=1))
        return 0
    if cmd == "verify":
        doc = load_crosswalk()
        rebuilt = build_from_archive()
        if rebuilt["content_sha256"] != doc["content_sha256"]:
            print("crosswalk differs from a rebuild of the archived sources", file=sys.stderr)
            return 1
        print(f"ok {datetime.now(UTC).date().isoformat()} content_sha256={doc['content_sha256']}")
        return 0
    print(f"unknown command {cmd!r}; use build | verify", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "ALIAS_MAPPING_VERSION",
    "CROSSWALK_PATH",
    "CrosswalkError",
    "build",
    "build_from_archive",
    "comtrade_reporters",
    "crosswalk_sha256",
    "load_crosswalk",
    "parse_m49",
    "seed_entities",
    "verify_sources",
    "worldbank_economies",
]
