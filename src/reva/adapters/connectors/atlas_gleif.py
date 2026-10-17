"""GLEIF Global LEI Index (api.gleif.org) -> atlas counts + KG legal entities and ownership (V4, agent V4INDUSTRY).

Source
------
GLEIF API v1 (``https://api.gleif.org/api/v1``, no key; 60 requests/minute per client). LEI data (Level 1 "who is
who", Level 2 "who owns whom", reporting exceptions) are published under **CC0 1.0** (GLEIF LEI data terms of use).
Every response carries ``meta.goldenCopy.publishDate``: the Golden Copy (published three times a day) the answer
was computed from. The Golden Copy bulk files (several GB) are never downloaded here: only API aggregations
(``meta.pagination.total`` of filtered collections) and bounded selections are fetched.

Datasets of ``atlas.gleif`` (source key ``gleif.api``)
------------------------------------------------------
``lei_counts`` (atlas table ``gleif.lei_counts``)
    For every country of GLEIF's country list (``/countries``, ISO 3166-1 alpha-2) the number of LEI records whose
    **legal address** is in that country (``filter[entity.legalAddress.country]``), by filter: ``all`` records,
    ``issued`` (``registration.status = ISSUED``, i.e. LEIs currently maintained), and entity categories
    ``general`` / ``fund`` / ``branch`` (``entity.category``). A country whose ``all`` total is 0 is reported with
    that 0 (an API total over GLEIF's complete index) and its other filters are not requested (no row: not
    fetched, never assumed 0). Counts of *LEI records* -- entities that obtained an LEI (mostly financial-market
    participants, their groups and funds) -- not counts of firms.

``legal_form_counts`` (atlas table ``gleif.legal_form_counts``)
    For the countries of :data:`LEGAL_FORM_COUNTRIES` (or run option ``legal_form_countries``), the number of LEI
    records with legal address in the country per ISO 20275 ELF code GLEIF lists for that country
    (``/entity-legal-forms``, every status). Codes are verbatim; the ELF local names are in the flags. The residual
    (records with other or foreign legal forms) is not derived.

``groups`` (KG)
    The ``top_groups`` (default 200) LEI records with the most ultimately consolidated LEI records
    (``sort=-ultimateChildCount``), each with ``gleif:ultimate_child_count`` and ``gleif:direct_child_count``
    (pagination totals of ``/ultimate-children`` and ``/direct-children``) at the Golden Copy date. A count of LEI
    records in the GLEIF hierarchy, **not** an economic size (banks and fund managers register far more entities
    than manufacturers of the same size).

``entities`` (KG)
    Level 1 + Level 2 for a bounded seed set, 200 LEIs per request with ``include=direct-parent,ultimate-parent``
    (the response embeds the parent records and reporting exceptions). Seeds, in order: run option ``leis``; LEIs
    referenced in the KG by other sources (``lei:`` keys); LEIs that Wikidata items with at least ``min_sitelinks``
    (default 20) sitelinks carry in P1278 (``gleif:wikidata_item`` statements loaded by ``atlas.gleif_wikidata``);
    the ``groups`` records of the same origin. Parents are loaded as entities with their Level 1 data; their own
    parents are not followed (one hop).

KG model (predicates in ``registry/kg/predicates_industry.yaml``)
-----------------------------------------------------------------
Entity ``lei:<LEI>`` (kind by GLEIF category: ``legal_entity`` for GENERAL, ``fund``, ``branch``,
``sole_proprietor``, ``government_entity``, ``international_organisation``), labelled with the legal name in the
language the record states (``und`` when it states none) and the English alternative-language legal name when
present. Statements: ``gleif:direct_parent`` / ``gleif:ultimate_parent`` (accounting consolidation, as reported
by the child) or ``gleif:parent_reporting_exception`` (verbatim category + reason), ``gleif:successor``,
``gleif:legal_address_country`` / ``gleif:headquarters_country`` (country QID via ISO code), ISO 3166-2 regions
and the headquarters city as literal strings, jurisdiction, ELF legal form (+ local names), category, entity and
registration status, creation date, ``gleif:gem_entity`` (``gem:<id>``, GLEIF's own LEI->GEM mapping) and
``gleif:opencorporates_id``. Nothing is matched by name.

``atlas.gleif_wikidata`` (source key ``wikidata.wdqs``, KG only)
----------------------------------------------------------------
One WDQS query through WDHARVEST's :class:`~reva.adapters.connectors.wikidata.WikidataClient` selects every
non-deprecated P1278 (Legal Entity Identifier) statement with the item's sitelink count; each becomes
``lei:<LEI> --gleif:wikidata_item--> Q...`` (community-edited) with the Wikidata statement id. A value that is not
a valid ISO 17442 LEI (20 characters, MOD 97-10 check digits) is skipped and counted, never repaired.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

from reva.adapters.connectors.atlas_industry_common import (
    atlas_frame,
    canon,
    code_digest,
    iso2_to_key,
    iso2_to_qid,
    load_kg,
    trim_registration,
)
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.gleif"
WD_NAME = "atlas.gleif_wikidata"
# 1.0.1: 1.0.0 was registered in the dev DB from a WIP snapshot taken before the B023 refactor.
# 1.0.2: groups also attach the ELF names to gleif:legal_form (1.0.1 live run: 200 group legal-form statements
#        conflicted with the entities dataset's richer version under the same Golden Copy revision).
CONNECTOR_VERSION = "1.0.2"
PARSER_VERSION = "atlas-gleif-1"
SOURCE_KEY = "gleif.api"
WD_SOURCE_KEY = "wikidata.wdqs"
ATLAS_SOURCE = "gleif"
DS_COUNTS = "lei_counts"
DS_FORMS = "legal_form_counts"
DS_GROUPS = "groups"
DS_ENTITIES = "entities"
WD_DATASET = "lei_wikidata"
HOST = "api.gleif.org"
WD_HOST = "query.wikidata.org"
API = f"https://{HOST}/api/v1"
PAGE_MAX = 200
MAX_BYTES = 20_000_000
MAX_WD_BYTES = 60_000_000
DAY = 86400

RIGHTS: dict[str, str] = {
    "licence": "CC0-1.0",
    "licence_url": "https://www.gleif.org/en/meta/lei-data-terms-of-use",
    "attribution": "Global Legal Entity Identifier Foundation (GLEIF), Global LEI Index (CC0 1.0)",
}

#: Count filters of ``lei_counts``: key -> extra API filters (legal-address country is always added).
COUNT_FILTERS: dict[str, dict[str, str]] = {
    "all": {},
    "issued": {"registration.status": "ISSUED"},
    "general": {"entity.category": "GENERAL"},
    "fund": {"entity.category": "FUND"},
    "branch": {"entity.category": "BRANCH"},
}
#: Countries whose per-legal-form counts are fetched by default: GLEIF's ELF list has <= 60 codes for each
#: (US, CA: one list per state/province, 700+ codes; FR: 270+; RU: 127 -- left for a run with a larger budget).
LEGAL_FORM_COUNTRIES: tuple[str, ...] = (
    "DE", "GB", "IT", "ES", "NL", "CN", "JP", "IN", "LU", "IE",
    "SE", "CH", "AU", "BR", "KR", "DK", "PL", "AT", "NO", "SA",
)  # fmt: skip
TOP_GROUPS = 200
MIN_SITELINKS = 20
MIN_COUNTRIES_LIVE = 200  # GLEIF lists 250 ISO countries (2026-10-17)

CATEGORY_KIND = {
    "GENERAL": "legal_entity",
    "FUND": "fund",
    "BRANCH": "branch",
    "SOLE_PROPRIETOR": "sole_proprietor",
    "RESIDENT_GOVERNMENT_ENTITY": "government_entity",
    "INTERNATIONAL_ORGANIZATION": "international_organisation",
}
LEI_RE = re.compile(r"^[0-9A-Z]{18}[0-9]{2}$")
GEM_RE = re.compile(r"^[A-Z][0-9]{6,15}$")
LANG_OK = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{1,8}){0,3}$")

P = {
    "direct_parent": "gleif:direct_parent",
    "ultimate_parent": "gleif:ultimate_parent",
    "exception": "gleif:parent_reporting_exception",
    "successor": "gleif:successor",
    "legal_country": "gleif:legal_address_country",
    "hq_country": "gleif:headquarters_country",
    "legal_region": "gleif:legal_address_region",
    "hq_region": "gleif:headquarters_region",
    "hq_city": "gleif:headquarters_city",
    "jurisdiction": "gleif:legal_jurisdiction",
    "legal_form": "gleif:legal_form",
    "category": "gleif:entity_category",
    "status": "gleif:entity_status",
    "registration": "gleif:registration_status",
    "created": "gleif:entity_creation_date",
    "gem": "gleif:gem_entity",
    "ocid": "gleif:opencorporates_id",
    "ult_children": "gleif:ultimate_child_count",
    "dir_children": "gleif:direct_child_count",
    "wikidata": "gleif:wikidata_item",
}


class GleifParseError(SourceResponseError):
    pass


def _err(msg: str, kind: str = "schema_drift") -> GleifParseError:
    return GleifParseError(msg, retryable=False, kind=kind)


# ============================================================================================== identifiers
def lei_valid(s: str) -> bool:
    """ISO 17442: 18 alphanumerics + 2 check digits, MOD 97-10 (ISO 7064) over the letter-expanded string == 1."""
    if not isinstance(s, str) or not LEI_RE.match(s):
        return False
    digits = "".join(str(int(c, 36)) for c in s)
    return int(digits) % 97 == 1


def lei_key(lei: str) -> str:
    return f"lei:{lei}"


# ============================================================================================== URLs
def url(path: str, params: Sequence[tuple[str, str]] = ()) -> str:
    q = urlencode(list(params), quote_via=quote, safe="")
    return f"{API}/{path}" + (f"?{q}" if q else "")


def count_url(country: str | None, filters: Mapping[str, str]) -> str:
    ps: list[tuple[str, str]] = []
    if country is not None:
        ps.append(("filter[entity.legalAddress.country]", country))
    ps += [(f"filter[{k}]", v) for k, v in sorted(filters.items())]
    ps.append(("page[size]", "1"))
    return url("lei-records", ps)


def countries_url() -> str:
    return url("countries", [("page[size]", "300")])


def elf_url(page: int) -> str:
    return url("entity-legal-forms", [("page[size]", str(PAGE_MAX)), ("page[number]", str(page))])


def records_url(leis: Sequence[str], *, with_parents: bool = True) -> str:
    ps = [("filter[lei]", ",".join(leis))]
    if with_parents:
        ps.append(("include", "direct-parent,ultimate-parent"))
    ps.append(("page[size]", str(PAGE_MAX)))
    return url("lei-records", ps)


def top_groups_url(n: int) -> str:
    return url("lei-records", [("sort", "-ultimateChildCount"), ("page[size]", str(n))])


def children_count_url(lei: str, which: str) -> str:
    if which not in ("ultimate-children", "direct-children"):
        raise ValueError(which)
    return url(f"lei-records/{lei}/{which}", [("page[size]", "1")])


# ============================================================================================== parsing
def _doc(body: bytes, what: str) -> dict[str, Any]:
    try:
        doc = json.loads(body)
    except ValueError as e:
        raise _err(f"{what}: not JSON ({e})") from None
    if not isinstance(doc, dict):
        raise _err(f"{what}: not a JSON object")
    if doc.get("errors"):
        raise _err(f"{what}: API errors {str(doc['errors'])[:300]}", kind="api_error")
    if not isinstance(doc.get("data"), list) or not isinstance(doc.get("meta"), dict):
        raise _err(f"{what}: expected JSON:API 'data' list and 'meta'")
    return doc


def _total(doc: Mapping[str, Any], what: str) -> int:
    t = ((doc.get("meta") or {}).get("pagination") or {}).get("total")
    if not isinstance(t, int) or t < 0:
        raise _err(f"{what}: meta.pagination.total missing")
    return t


def _publish(doc: Mapping[str, Any], what: str) -> str:
    pd = ((doc.get("meta") or {}).get("goldenCopy") or {}).get("publishDate")
    if not isinstance(pd, str) or not re.match(r"^\d{4}-\d{2}-\d{2}T", pd):
        raise _err(f"{what}: meta.goldenCopy.publishDate missing")
    return pd


def parse_count(body: bytes, what: str = "count") -> tuple[int, str]:
    """``(pagination total, golden copy publish date)`` of a filtered collection page."""
    doc = _doc(body, what)
    return _total(doc, what), _publish(doc, what)


def parse_countries(body: bytes, *, min_countries: int = MIN_COUNTRIES_LIVE) -> list[tuple[str, str]]:
    doc = _doc(body, "countries")
    out = []
    for x in doc["data"]:
        a = x.get("attributes") or {}
        code, name = a.get("code"), a.get("name")
        if not (isinstance(code, str) and re.match(r"^[A-Z]{2}$", code) and isinstance(name, str)):
            raise _err(f"countries: bad entry {str(x)[:200]}")
        out.append((code, name))
    if _total(doc, "countries") != len(out):
        raise _err("countries: partial page", kind="incomplete")
    if len(out) < min_countries:
        raise _err(f"countries: only {len(out)}", kind="incomplete")
    return out


@dataclass(frozen=True)
class LegalForm:
    code: str
    country: str | None
    subdivision: str | None
    status: str
    names: tuple[dict[str, Any], ...]


def parse_elf_page(body: bytes) -> tuple[list[LegalForm], int, int]:
    """One ``/entity-legal-forms`` page -> (forms, current page, last page)."""
    doc = _doc(body, "entity-legal-forms")
    pag = doc["meta"].get("pagination") or {}
    out = []
    for x in doc["data"]:
        a = x.get("attributes") or {}
        code = a.get("code")
        if not isinstance(code, str) or not code:
            raise _err(f"entity-legal-forms: entry without code {str(x)[:200]}")
        names = tuple(
            {k: n.get(k) for k in ("localName", "languageCode", "transliteratedName") if n.get(k)}
            for n in a.get("names") or []
            if isinstance(n, Mapping)
        )
        out.append(LegalForm(code, a.get("countryCode"), a.get("subdivisionCode"), str(a.get("status") or ""), names))
    return out, int(pag.get("currentPage", 0)), int(pag.get("lastPage", 0))


@dataclass(frozen=True)
class Parent:
    kind: str  # "lei" | "exception"
    lei: str | None = None
    category: str | None = None
    reason: str | None = None
    reference: str | None = None


@dataclass
class LeiRecord:
    lei: str
    legal_name: str
    name_lang: str | None
    other_names: tuple[tuple[str, str | None, str | None], ...]
    legal_country: str | None
    legal_region: str | None
    hq_country: str | None
    hq_region: str | None
    hq_city: str | None
    jurisdiction: str | None
    category: str | None
    sub_category: str | None
    legal_form: str | None
    legal_form_other: str | None
    status: str | None
    expiration_reason: str | None
    creation_date: str | None
    registration_status: str | None
    corroboration: str | None
    last_update: str | None
    successors: tuple[str, ...]
    gem: tuple[str, ...]
    ocid: str | None
    direct_parent: Parent | None = None
    ultimate_parent: Parent | None = None
    index: int = 0
    included: bool = False  # True: a parent record embedded by include=, not a requested one


def _s(v: Any) -> str | None:
    return v if isinstance(v, str) and v.strip() else None


def _record(x: Mapping[str, Any], i: int, included: bool) -> LeiRecord:
    a = x.get("attributes") or {}
    e = a.get("entity") or {}
    reg = a.get("registration") or {}
    lei = a.get("lei")
    if not isinstance(lei, str) or lei != x.get("id"):
        raise _err(f"lei-record {i}: lei/id mismatch")
    ln = e.get("legalName") or {}
    if not _s(ln.get("name")):
        raise _err(f"lei-record {lei}: no legal name")
    la = e.get("legalAddress") or {}
    hq = e.get("headquartersAddress") or {}
    others = tuple(
        (str(n.get("name")), _s(n.get("language")), _s(n.get("type")))
        for n in [*(e.get("otherNames") or []), *(e.get("transliteratedOtherNames") or [])]
        if isinstance(n, Mapping) and _s(n.get("name"))
    )
    succ = []
    se = e.get("successorEntity") or {}
    if _s(se.get("lei")):
        succ.append(se["lei"])
    for s in e.get("successorEntities") or []:
        if isinstance(s, Mapping) and _s(s.get("lei")) and s["lei"] not in succ:
            succ.append(s["lei"])
    gem = a.get("gem")
    gems = tuple(g for g in ([gem] if isinstance(gem, str) else gem or []) if isinstance(g, str) and g)
    lf = e.get("legalForm") or {}
    exp = e.get("expiration") or {}
    return LeiRecord(
        lei=lei,
        legal_name=str(ln["name"]).strip(),
        name_lang=_s(ln.get("language")),
        other_names=others,
        legal_country=_s(la.get("country")),
        legal_region=_s(la.get("region")),
        hq_country=_s(hq.get("country")),
        hq_region=_s(hq.get("region")),
        hq_city=_s(hq.get("city")),
        jurisdiction=_s(e.get("jurisdiction")),
        category=_s(e.get("category")),
        sub_category=_s(e.get("subCategory")),
        legal_form=_s(lf.get("id")),
        legal_form_other=_s(lf.get("other")),
        status=_s(e.get("status")),
        expiration_reason=_s(exp.get("reason")),
        creation_date=_s(e.get("creationDate")),
        registration_status=_s(reg.get("status")),
        corroboration=_s(reg.get("corroborationLevel")),
        last_update=_s(reg.get("lastUpdateDate")),
        successors=tuple(succ),
        gem=gems,
        ocid=_s(a.get("ocid")),
        index=i,
        included=included,
    )


def _parent(rel: Any, exceptions: Mapping[str, Mapping[str, Any]]) -> Parent | None:
    """The ``direct-parent`` / ``ultimate-parent`` relationship of a record (``include`` makes ``data`` present)."""
    if not isinstance(rel, Mapping):
        return None
    d = rel.get("data")
    if isinstance(d, Mapping):
        if d.get("type") == "lei-records" and isinstance(d.get("id"), str):
            return Parent("lei", lei=d["id"])
        if d.get("type") == "reporting-exceptions":
            ex = exceptions.get(str(d.get("id")))
            if ex is None:
                raise _err(f"reporting exception {str(d.get('id'))[:80]} referenced but not included")
            return Parent("exception", category=_s(ex.get("category")), reason=_s(ex.get("reason")),
                          reference=_s(ex.get("reference")))  # fmt: skip
        raise _err(f"unexpected relationship data {str(d)[:200]}")
    links = rel.get("links") or {}
    if "relationship-record" in links or "lei-record" in links:
        raise _err("parent relationship without included data (request must use include=)", kind="request_shape")
    return None


@dataclass
class RecordsPage:
    records: list[LeiRecord]
    publish_date: str
    total: int
    missing: list[str] = field(default_factory=list)
    #: parent LEIs named by ``include`` (the API embeds only their identifiers; their records need a 2nd request)
    parent_leis: list[str] = field(default_factory=list)
    #: True: a page of parent records (Level 1 only, their own parents are not followed)
    parents_page: bool = False


def parse_records(
    body: bytes, *, requested: Sequence[str] = (), with_parents: bool = True, parents_page: bool = False
) -> RecordsPage:
    """A ``lei-records`` page (``filter[lei]`` with ``include=direct-parent,ultimate-parent``, a parent page without
    include, or the top-groups sort). ``missing``: requested LEIs the index did not return."""
    doc = _doc(body, "lei-records")
    pd = _publish(doc, "lei-records")
    total = _total(doc, "lei-records")
    inc = doc.get("included") or []
    exceptions = {str(x.get("id")): (x.get("attributes") or {}) for x in inc if x.get("type") == "reporting-exceptions"}
    recs: list[LeiRecord] = []
    for i, x in enumerate(doc["data"]):
        r = _record(x, i, parents_page)
        if with_parents:
            rel = x.get("relationships") or {}
            r.direct_parent = _parent(rel.get("direct-parent"), exceptions)
            r.ultimate_parent = _parent(rel.get("ultimate-parent"), exceptions)
        recs.append(r)
    parents = [str(x["id"]) for x in inc if x.get("type") == "lei-records" and isinstance(x.get("id"), str)]
    if requested and total != len(recs):
        raise _err(f"lei-records: {len(recs)} of {total} returned (page too small)", kind="incomplete")
    got = {r.lei for r in recs}
    return RecordsPage(recs, pd, total, [lei for lei in requested if lei not in got], parents, parents_page)


# ============================================================================================== atlas rows
def _period(publish: str) -> tuple[date, date]:
    d = date.fromisoformat(publish[:10])
    return d, d + timedelta(days=1)


def count_row(
    *, dataset: str, indicator: str, concept: str, iso2: str, value: int, publish: str, raw_sha256: str,
    request_url: str, flags: Mapping[str, Any],
) -> dict[str, Any]:  # fmt: skip
    p0, p1 = _period(publish)
    fl = {
        "aggregation": "api_pagination_total",
        "golden_copy_publish_date": publish,
        "counts_of": "LEI records (legal entities registered in the Global LEI System), not all firms",
        "licence": RIGHTS["licence"],
        **flags,
    }
    return {
        "source": ATLAS_SOURCE,
        "dataset": dataset,
        "indicator": indicator,
        "concept_id": concept,
        "iso3": None,
        "provider_geo": iso2,
        "entity_qid": None,
        "period_start": p0,
        "period_end": p1,
        "value": str(value),
        "value_text": None,
        "unit": "LEI records",
        "flags": canon(fl),
        "raw_sha256": raw_sha256,
        "selector": canon({"url": request_url, "path": "meta.pagination.total"}),
    }


# ============================================================================================== KG
def _labels(r: LeiRecord) -> tuple[dict[str, str], dict[str, list[str]]]:
    labels: dict[str, str] = {}
    lang = (r.name_lang or "und").lower()
    labels[lang if LANG_OK.match(lang) else "und"] = r.legal_name[:500]
    aliases: dict[str, list[str]] = {}
    for name, nl, typ in r.other_names:
        lg = (nl or "und").lower()
        lg = lg if LANG_OK.match(lg) else "und"
        if typ == "ALTERNATIVE_LANGUAGE_LEGAL_NAME" and lg not in labels:
            labels[lg] = name[:500]
        elif name not in labels.values():
            aliases.setdefault(lg, [])
            if name[:500] not in aliases[lg]:
                aliases[lg].append(name[:500])
    return labels, aliases


def kg_records(
    pages: Sequence[RecordsPage],
    raws: Sequence[str],
    *,
    elf: Mapping[str, LegalForm] | None = None,
    counts: Mapping[str, Mapping[str, tuple[int, str, str]]] | None = None,
) -> tuple[list[Any], list[Any], dict[str, Any]]:
    """KG entities + statements of record pages (``raws[i]`` = raw sha256 of ``pages[i]``).

    ``counts``: LEI -> {"ultimate-children"|"direct-children": (total, publish date, raw sha256)}."""
    from reva.kg.model import KgEntity, KgStatement, KgTime

    ents: dict[str, Any] = {}
    sts: dict[str, Any] = {}
    stats: Counter[str] = Counter()

    def ref(lei: str, pd: str, path: str) -> tuple[dict[str, Any], ...]:
        return (
            {
                "stated_in": "GLEIF Global LEI Index (Golden Copy via api.gleif.org)",
                "url": f"{API}/lei-records/{lei}",
                "golden_copy_publish_date": pd,
                "selector": {"lei": lei, "path": path},
            },
        )

    def add(st: Any) -> None:
        sts.setdefault(st.statement_id, st)

    for page, raw in zip(pages, raws, strict=True):
        rev = f"gc:{page.publish_date[:19]}"
        for r in page.records:
            key = lei_key(r.lei)
            if not lei_valid(r.lei):
                stats["invalid_lei_record"] += 1
                continue
            labels, aliases = _labels(r)
            ents.setdefault(
                key,
                KgEntity(
                    qid=key,
                    kind=CATEGORY_KIND.get(r.category or "", "legal_entity"),
                    labels=labels,
                    descriptions={
                        "en": f"Legal entity with LEI {r.lei} (GLEIF, category {r.category or 'unknown'}"
                        f"{', legal address ' + r.legal_country if r.legal_country else ''})"
                    },
                    aliases=aliases,
                    source_revision=f"gleif:{r.last_update or page.publish_date}"[:100],
                ),
            )
            q = {"corroboration_level": r.corroboration, "registration_status": r.registration_status}
            ctx = (key, q, r.lei, page.publish_date, rev, raw)

            def emit(
                prop: str,
                sid: str,
                path: str,
                *,
                value: Any = None,
                obj: str | None = None,
                extra: Mapping[str, Any] | None = None,
                c: tuple[Any, ...] = ctx,
            ) -> None:
                k, qual, lei, pd, rv, rw = c
                add(
                    KgStatement(
                        subject_qid=k,
                        property_pid=P[prop],
                        object_qid=obj,
                        value=value,
                        qualifiers={**qual, **(extra or {})},
                        references=ref(lei, pd, path),
                        source_revision=rv,
                        statement_id=f"{lei}:{sid}",
                        raw_sha256=rw,
                    )
                )

            def lit(prop: str, sid: str, value: Any, path: str, extra: Mapping[str, Any] | None = None) -> None:
                emit(prop, sid, path, value=value, extra=extra)

            def item(prop: str, sid: str, obj: str, path: str, extra: Mapping[str, Any] | None = None) -> None:
                emit(prop, sid, path, obj=obj, extra=extra)

            for prop, code, path in (
                ("legal_country", r.legal_country, "entity.legalAddress.country"),
                ("hq_country", r.hq_country, "entity.headquartersAddress.country"),
            ):
                if code is None:
                    continue
                qid = iso2_to_qid(code)
                if qid is None:
                    stats[f"country_unresolved:{code}"] += 1
                    continue
                item(prop, prop, qid, path, {"iso3166_1_alpha2": code, "join": "iso2 -> crosswalk -> country_qids"})
            for prop, v, path in (
                ("legal_region", r.legal_region, "entity.legalAddress.region"),
                ("hq_region", r.hq_region, "entity.headquartersAddress.region"),
                ("hq_city", r.hq_city, "entity.headquartersAddress.city"),
                ("jurisdiction", r.jurisdiction, "entity.jurisdiction"),
                ("category", r.category, "entity.category"),
                ("registration", r.registration_status, "registration.status"),
            ):
                if v is not None:
                    lit(prop, prop, {"type": "string", "value": v[:400]}, path)
            if r.status is not None:
                lit("status", "status", {"type": "string", "value": r.status}, "entity.status",
                    {"expiration_reason": r.expiration_reason} if r.expiration_reason else None)  # fmt: skip
            if r.legal_form is not None:
                extra: dict[str, Any] = {}
                lf = (elf or {}).get(r.legal_form)
                if lf is not None:
                    extra = {"elf_names": list(lf.names), "elf_country": lf.country, "elf_status": lf.status}
                if r.legal_form_other:
                    extra["legal_form_other"] = r.legal_form_other[:400]
                lit("legal_form", "legal_form", {"type": "string", "value": r.legal_form}, "entity.legalForm", extra)
            if r.creation_date and re.match(r"^\d{4}-\d{2}-\d{2}", r.creation_date):
                lit("created", "created", {"type": "time", "time": r.creation_date[:10], "precision": 11},
                    "entity.creationDate")  # fmt: skip
            for g in r.gem:
                if GEM_RE.match(g):
                    item("gem", f"gem:{g}", f"gem:{g}", "gem", {"gem_id_as_published": g})
                else:
                    stats["gem_id_unexpected_shape"] += 1
            if r.ocid:
                lit("ocid", "ocid", {"type": "external_id", "value": r.ocid[:200]}, "ocid")
            for s in r.successors:
                if lei_valid(s):
                    item("successor", f"successor:{s}", lei_key(s), "entity.successorEntity")
            if r.included:
                continue  # parents: Level 1 only (their own parents are not requested: one hop)
            for which, par in (("direct", r.direct_parent), ("ultimate", r.ultimate_parent)):
                if par is None:
                    stats[f"{which}_parent_not_stated"] += 1
                    continue
                if par.kind == "lei" and par.lei and lei_valid(par.lei):
                    item(f"{which}_parent", f"{which}_parent", lei_key(par.lei), f"relationships.{which}-parent",
                         {"relationship_type": "IS_DIRECTLY_CONSOLIDATED_BY" if which == "direct"
                          else "IS_ULTIMATELY_CONSOLIDATED_BY", "as_of_golden_copy": page.publish_date})  # fmt: skip
                    stats[f"{which}_parent_lei"] += 1
                elif par.kind == "exception":
                    lit("exception", f"{which}_exception",
                        {"type": "string", "value": f"{par.category}: {par.reason}"},
                        f"relationships.{which}-parent (reporting exception)",
                        {"exception_category": par.category, "exception_reason": par.reason,
                         **({"exception_reference": par.reference} if par.reference else {})})  # fmt: skip
                    stats[f"{which}_parent_exception:{par.reason}"] += 1
    for lei, cs in (counts or {}).items():
        key = lei_key(lei)
        for which, (total, pd, raw) in cs.items():
            prop = "ult_children" if which == "ultimate-children" else "dir_children"
            d = pd[:10]
            add(
                KgStatement(
                    subject_qid=key,
                    property_pid=P[prop],
                    value={"type": "quantity", "amount": str(total), "unit": "1"},
                    point_in_time=KgTime(d, 11),
                    qualifiers={"counts_of": "LEI records in the GLEIF hierarchy (not an economic size)"},
                    references=(
                        {
                            "stated_in": "GLEIF API pagination total",
                            "url": children_count_url(lei, which),
                            "golden_copy_publish_date": pd,
                            "selector": {"path": "meta.pagination.total"},
                        },
                    ),
                    source_revision=f"gc:{pd[:19]}",
                    statement_id=f"{lei}:{prop}:{d}",
                    raw_sha256=raw,
                )
            )
    info = {
        "entities": len(ents),
        "statements": len(sts),
        "by_property": dict(Counter(s.property_pid for s in sts.values())),
        "stats": dict(stats),
    }
    return list(ents.values()), list(sts.values()), info


# ============================================================================================== Wikidata P1278
WD_QUERY = """SELECT ?item ?statement ?lei ?rank ?sitelinks WHERE {
  ?item p:P1278 ?statement .
  ?statement ps:P1278 ?lei ;
             wikibase:rank ?rank .
  FILTER(?rank != wikibase:DeprecatedRank)
  OPTIONAL { ?item wikibase:sitelinks ?sitelinks . }
}"""
WD_PREFIX = "lei_xref:"
RANKS = {"http://wikiba.se/ontology#PreferredRank": "preferred", "http://wikiba.se/ontology#NormalRank": "normal"}


def wikidata_statements(
    bindings: Sequence[Mapping[str, Any]], *, raw_sha256: str, retrieved_at: datetime, query_sha256: str
) -> tuple[list[Any], dict[str, Any]]:
    """WDQS rows -> ``lei:<LEI> --gleif:wikidata_item--> Q`` statements (source wikidata.wdqs)."""
    from reva.adapters.connectors.wikidata import qid_from_uri, statement_id_from_uri
    from reva.kg.model import KgStatement

    out: list[Any] = []
    counters: Counter[str] = Counter()
    seen: set[str] = set()
    revision = f"wdqs@{retrieved_at.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    for b in bindings:
        qid = qid_from_uri((b.get("item") or {}).get("value"))
        sid = statement_id_from_uri((b.get("statement") or {}).get("value"))
        lei = str((b.get("lei") or {}).get("value") or "")
        rank = RANKS.get(str((b.get("rank") or {}).get("value") or ""))
        sl_raw = (b.get("sitelinks") or {}).get("value")
        if not qid or not sid or rank is None:
            counters["row_incomplete_or_deprecated"] += 1
            continue
        if not lei_valid(lei):
            counters["value_not_a_valid_lei"] += 1
            continue
        if sid in seen:
            counters["duplicate_row"] += 1
            continue
        seen.add(sid)
        quals: dict[str, Any] = {
            "wikidata_property": "P1278",
            "wikidata_statement_id": sid,
            "lei_as_stated": lei,
            "_revision": {
                "wikidata_revision": "unknown",
                "reason": "WDQS rows carry no entity revision",
                "retrieved_at": retrieved_at.isoformat(),
            },
        }
        if isinstance(sl_raw, str) and sl_raw.isdigit():
            quals["item_sitelinks"] = int(sl_raw)
        out.append(
            KgStatement(
                subject_qid=lei_key(lei),
                property_pid=P["wikidata"],
                object_qid=qid,
                qualifiers=quals,
                rank=rank,
                references=[{"stated_in": "Wikidata (P1278)", "query_sha256": query_sha256}],
                statement_id=f"{WD_PREFIX}{sid}",
                source_revision=revision,
                raw_sha256=raw_sha256,
            )
        )
    by_lei = Counter(s.subject_qid for s in out)
    by_item = Counter(s.object_qid for s in out)
    report = {
        "rows": len(bindings),
        "statements": len(out),
        "leis": len(by_lei),
        "items": len(by_item),
        "leis_on_several_items": sum(1 for v in by_lei.values() if v > 1),
        "items_with_several_leis": sum(1 for v in by_item.values() if v > 1),
        "skipped": dict(counters),
    }
    return out, report


async def ingest_wikidata(run: Any) -> dict[str, Any]:
    import tempfile
    from pathlib import Path

    from reva.adapters.connectors.wikidata import Archive, MachineLock, WikidataClient

    if not run.wants(WD_DATASET):
        return {"skipped": True}
    with tempfile.TemporaryDirectory(prefix="reva-v4industry-lock-") as tmp:
        lock = None
        if run.origin != "live":  # fixture replay: never wait on the machine-wide Wikidata cool-down
            lock = MachineLock(Path(tmp), min_interval=0.0)
        client = WikidataClient(run.http, archive=Archive(run.store), lock=lock, max_retries=6)
        res = await client.sparql(WD_QUERY, meta={"purpose": "lei_xref", "connector": WD_NAME})
    sts, report = wikidata_statements(
        res.bindings, raw_sha256=res.raw_sha256, retrieved_at=res.retrieved_at, query_sha256=res.query_sha256
    )
    kg = load_kg(
        run.engine,
        run.store,
        entities=[],
        statements=sts,
        source_key=WD_SOURCE_KEY,
        raw_sha256s=[res.raw_sha256],
        retrieved_at=res.retrieved_at,
        origin=run.origin,
        parameters={"connector": WD_NAME, "parser": PARSER_VERSION, "query_sha256": res.query_sha256},
        predicate_prefix="gleif:",
        retract_prefix=WD_PREFIX,
    )
    return {"raw_sha256": res.raw_sha256, "query_sha256": res.query_sha256, "build": report, "kg": kg,
            "client": client.stats.as_dict()}  # fmt: skip


# ============================================================================================== fetch helpers
async def _get(run: Any, u: str, key: str, max_bytes: int = MAX_BYTES) -> tuple[Any, str]:
    raw = await run.get(u, key=key, media_types=("application/vnd.api+json", "application/json"), max_bytes=max_bytes)
    return raw, run.archive(raw)


async def fetch_elf(run: Any) -> tuple[dict[str, LegalForm], list[str]]:
    forms: dict[str, LegalForm] = {}
    shas: list[str] = []
    page, last = 1, 1
    while page <= last:
        raw, sha = await _get(run, elf_url(page), f"elf:{page}")
        shas.append(sha)
        got, cur, last = parse_elf_page(raw.body)
        if cur != page:
            raise _err(f"entity-legal-forms: asked page {page}, got {cur}")
        for f in got:
            forms[f.code] = f
        page += 1
    return forms, shas


def _opt_list(run: Any, name: str) -> list[str] | None:
    v = (run.options or {}).get(name)
    if v is None:
        return None
    items = v if isinstance(v, list) else str(v).split(",")
    return [x.strip().upper() for x in items if x.strip()]


def _opt_int(run: Any, name: str, default: int) -> int:
    v = (run.options or {}).get(name)
    return int(v) if v is not None else default


# ============================================================================================== datasets
async def ingest_counts(run: Any) -> dict[str, Any]:
    raw, csha = await _get(run, countries_url(), "countries")
    countries = parse_countries(raw.body, min_countries=MIN_COUNTRIES_LIVE if run.origin == "live" else 1)
    only = _opt_list(run, "countries")
    if only:
        countries = [c for c in countries if c[0] in only]
    rows: list[dict[str, Any]] = []
    shas = [csha]
    stopped: dict[str, Any] | None = None
    fetched: dict[str, int] = {}
    for code, name in countries:
        for fkey, filt in COUNT_FILTERS.items():
            if fkey != "all" and fetched.get(code) == 0:
                break
            u = count_url(code, filt)
            try:
                r, sha = await _get(run, u, f"count:{code}:{fkey}")
            except SourceResponseError as e:
                if not e.details.get("retryable", getattr(e, "retryable", False)) or not rows:
                    raise
                stopped = {"at": f"{code}:{fkey}", "error": e.message[:300]}
                break
            total, pd = parse_count(r.body, f"count {code} {fkey}")
            if fkey == "all":
                fetched[code] = total
            shas.append(sha)
            rows.append(
                count_row(
                    dataset=DS_COUNTS,
                    indicator=f"lei_records|filter={fkey}",
                    concept=f"companies.gleif.lei_records.{fkey}",
                    iso2=code,
                    value=total,
                    publish=pd,
                    raw_sha256=sha,
                    request_url=u,
                    flags={"filter": {"entity.legalAddress.country": code, **filt}, "gleif_country_name": name},
                )
            )
        if stopped:
            break
    reg = run.register(
        dataset=DS_COUNTS,
        frame=atlas_frame(rows),
        raw_sha256s=shas,
        geo_scheme="iso2",
        coverage_note=(
            "Number of LEI records per legal-address country (GLEIF API pagination totals at the Golden Copy date of "
            "each row), all records and by registration status / entity category. Counts of LEI records, not of firms."
            + (f" Run stopped early at {stopped['at']}: later countries not fetched." if stopped else "")
        ),
        rights=RIGHTS,
    )
    return {"countries": len(countries), "rows": len(rows), "stopped": stopped, "atlas": trim_registration(reg)}


async def ingest_legal_forms(run: Any) -> dict[str, Any]:
    forms, elf_shas = await fetch_elf(run)
    wanted = _opt_list(run, "legal_form_countries") or list(LEGAL_FORM_COUNTRIES)
    rows: list[dict[str, Any]] = []
    shas = list(elf_shas)
    per_country: Counter[str] = Counter()
    for cc in wanted:
        codes = sorted(f.code for f in forms.values() if f.country == cc)
        for code in codes:
            u = count_url(cc, {"entity.legalForm": code})
            r, sha = await _get(run, u, f"form:{cc}:{code}")
            total, pd = parse_count(r.body, f"legal form {cc} {code}")
            shas.append(sha)
            per_country[cc] += 1
            lf = forms[code]
            rows.append(
                count_row(
                    dataset=DS_FORMS,
                    indicator=f"lei_records|legal_form={code}",
                    concept="companies.gleif.lei_records.by_legal_form",
                    iso2=cc,
                    value=total,
                    publish=pd,
                    raw_sha256=sha,
                    request_url=u,
                    flags={
                        "filter": {"entity.legalAddress.country": cc, "entity.legalForm": code},
                        "elf_code": code,
                        "elf_names": list(lf.names),
                        "elf_status": lf.status,
                        "elf_subdivision": lf.subdivision,
                        "standard": "ISO 20275 Entity Legal Forms (GLEIF code list)",
                    },
                )
            )
    if not rows:
        return {"forms": len(forms), "rows": 0, "atlas": None, "note": "no ELF codes for the requested countries"}
    reg = run.register(
        dataset=DS_FORMS,
        frame=atlas_frame(rows),
        raw_sha256s=shas,
        geo_scheme="iso2",
        coverage_note=(
            "Number of LEI records per legal-address country and ISO 20275 legal form (GLEIF API pagination totals) "
            f"for {len(per_country)} countries; residual (other / foreign forms) not derived."
        ),
        rights=RIGHTS,
    )
    return {"forms": len(forms), "countries": dict(per_country), "rows": len(rows), "atlas": trim_registration(reg)}


async def ingest_groups(run: Any) -> dict[str, Any]:
    n = min(_opt_int(run, "top_groups", TOP_GROUPS), PAGE_MAX)
    raw, sha = await _get(run, top_groups_url(n), "groups:top")
    page = parse_records(raw.body, with_parents=False)
    counts: dict[str, dict[str, tuple[int, str, str]]] = {}
    shas = [sha]
    for r in page.records:
        for which in ("ultimate-children", "direct-children"):
            cr, csha = await _get(run, children_count_url(r.lei, which), f"groups:{r.lei}:{which}")
            total, pd = parse_count(cr.body, f"{which} {r.lei}")
            counts.setdefault(r.lei, {})[which] = (total, pd, csha)
            shas.append(csha)
    # same ELF names as the entities dataset: a Level 1 statement must not differ between datasets of one revision
    forms, elf_shas = await fetch_elf(run)
    shas += elf_shas
    ents, sts, info = kg_records([page], [sha], counts=counts, elf=forms)
    kg = load_kg(
        run.engine,
        run.store,
        entities=ents,
        statements=sts,
        source_key=SOURCE_KEY,
        raw_sha256s=shas,
        retrieved_at=raw.retrieved_at,
        origin=run.origin,
        parameters={"connector": CONNECTOR_NAME, "parser": PARSER_VERSION, "dataset": DS_GROUPS},
        predicate_prefix="gleif:",
    )
    top = sorted(((v["ultimate-children"][0], k) for k, v in counts.items()), reverse=True)[:10]
    return {"groups": len(page.records), "golden_copy": page.publish_date, "top10": top, "build": info, "kg": kg}


def seed_leis(engine: Any, origin: str, *, min_sitelinks: int) -> tuple[list[str], dict[str, int]]:
    """LEIs the KG already references (other sources), Wikidata P1278 LEIs of items with >= ``min_sitelinks``
    sitelinks, and the ``groups`` records -- all of the same origin. Ordered, de-duplicated, valid only."""
    from sqlalchemy import text

    with engine.connect() as c:
        others = c.execute(
            text("""SELECT DISTINCT q FROM (
                      SELECT subject_qid AS q, source_key FROM kg.statement_current WHERE subject_qid LIKE 'lei:%'
                       AND origin = :o
                      UNION ALL
                      SELECT object_qid AS q, source_key FROM kg.statement_current WHERE object_qid LIKE 'lei:%'
                       AND origin = :o) x
                    WHERE source_key NOT IN (:s1, :s2)"""),
            {"o": origin, "s1": SOURCE_KEY, "s2": WD_SOURCE_KEY},
        ).scalars()
        wd = c.execute(
            text("""SELECT subject_qid FROM kg.statement_current
                    WHERE property_pid = :p AND origin = :o
                      AND COALESCE((qualifiers->>'item_sitelinks')::int, 0) >= :m
                    ORDER BY (qualifiers->>'item_sitelinks')::int DESC, subject_qid"""),
            {"p": P["wikidata"], "o": origin, "m": min_sitelinks},
        ).scalars()
        groups = c.execute(
            text("""SELECT DISTINCT subject_qid FROM kg.statement_current
                    WHERE property_pid = :p AND origin = :o AND source_key = :s"""),
            {"p": P["ult_children"], "o": origin, "s": SOURCE_KEY},
        ).scalars()
        lists = {"kg_other_sources": list(others), "wikidata_p1278": list(wd), "groups": list(groups)}
    out: list[str] = []
    seen: set[str] = set()
    counts: dict[str, int] = {}
    for name, items in lists.items():
        counts[name] = 0
        for q in items:
            lei = q.split(":", 1)[1] if isinstance(q, str) and q.startswith("lei:") else ""
            if lei_valid(lei) and lei not in seen:
                seen.add(lei)
                out.append(lei)
                counts[name] += 1
    return out, counts


async def ingest_entities(run: Any) -> dict[str, Any]:
    explicit = [x for x in (_opt_list(run, "leis") or []) if lei_valid(x)]
    seeds: list[str] = list(dict.fromkeys(explicit))
    seed_counts: dict[str, int] = {"option_leis": len(seeds)}
    if str((run.options or {}).get("only_option_leis", "")).lower() not in ("1", "true", "yes"):
        more, c = seed_leis(run.engine, run.origin, min_sitelinks=_opt_int(run, "min_sitelinks", MIN_SITELINKS))
        seed_counts.update(c)
        seeds += [x for x in more if x not in set(seeds)]
    cap = _opt_int(run, "max_seeds", 6000)
    seeds = seeds[:cap]
    if not seeds:
        return {"seeds": 0, "note": "no seed LEIs (run atlas.gleif_wikidata first or pass --option leis=...)"}
    forms, elf_shas = await fetch_elf(run)
    pages: list[RecordsPage] = []
    shas: list[str] = []
    missing: list[str] = []
    for i in range(0, len(seeds), PAGE_MAX):
        chunk = seeds[i : i + PAGE_MAX]
        raw, sha = await _get(run, records_url(chunk), f"records:{i // PAGE_MAX}")
        page = parse_records(raw.body, requested=chunk)
        pages.append(page)
        shas.append(sha)
        missing += page.missing
    got = {r.lei for p in pages for r in p.records}
    parents = sorted({lei for p in pages for lei in p.parent_leis if lei not in got and lei_valid(lei)})
    for i in range(0, len(parents), PAGE_MAX):
        chunk = parents[i : i + PAGE_MAX]
        raw, sha = await _get(run, records_url(chunk, with_parents=False), f"parents:{i // PAGE_MAX}")
        page = parse_records(raw.body, requested=chunk, with_parents=False, parents_page=True)
        pages.append(page)
        shas.append(sha)
        missing += page.missing
    ents, sts, info = kg_records(pages, shas, elf=forms)
    kg = load_kg(
        run.engine,
        run.store,
        entities=ents,
        statements=sts,
        source_key=SOURCE_KEY,
        raw_sha256s=[*shas, *elf_shas],
        retrieved_at=datetime.fromisoformat(max(p.publish_date for p in pages).replace("Z", "+00:00")),
        origin=run.origin,
        parameters={"connector": CONNECTOR_NAME, "parser": PARSER_VERSION, "dataset": DS_ENTITIES},
        predicate_prefix="gleif:",
    )
    return {
        "seeds": len(seeds),
        "seed_sources": seed_counts,
        "requests": len(pages),
        "missing_from_index": missing[:100],
        "missing_count": len(missing),
        "parents_fetched": len(parents),
        "build": info,
        "kg": kg,
    }


# ============================================================================================== atlas protocol
def atlas_source() -> list[Any]:
    from reva.atlas.sources import AtlasSourceSpec

    digest = code_digest(sys.modules[__name__])
    return [
        AtlasSourceSpec(
            name=CONNECTOR_NAME,
            version=CONNECTOR_VERSION,
            source_key=SOURCE_KEY,
            budget_key=SOURCE_KEY,
            atlas_source=ATLAS_SOURCE,
            provider="Global Legal Entity Identifier Foundation (GLEIF) - Global LEI Index API v1",
            licence=f"CC0-1.0 ({RIGHTS['licence_url']}); attribution: {RIGHTS['attribution']}",
            allowed_hosts=(HOST,),
            datasets=(DS_COUNTS, DS_FORMS, DS_GROUPS, DS_ENTITIES),
            dataset_ids=("GLEIF Golden Copy (LEI-CDF 3.1, RR-CDF 2.1, reporting exceptions)",),
            cadence_seconds=30 * DAY,
            planned_requests=2600,
            max_response_bytes=MAX_BYTES,
            min_interval_seconds=1.1,
            parser_version=PARSER_VERSION,
            code_digest=digest,
            field_mappings={
                "count_filters": COUNT_FILTERS,
                "legal_form_countries": list(LEGAL_FORM_COUNTRIES),
                "top_groups": TOP_GROUPS,
                "min_sitelinks": MIN_SITELINKS,
                "kg": sorted(v for k, v in P.items() if k != "wikidata"),
            },
            notes=(
                "API aggregations only (pagination totals) + bounded LEI selections; Golden Copy bulk files never "
                "downloaded. Datasets: lei_counts / legal_form_counts (atlas), groups / entities (KG)."
            ),
        ),
        AtlasSourceSpec(
            name=WD_NAME,
            version=CONNECTOR_VERSION,
            source_key=WD_SOURCE_KEY,
            budget_key=WD_SOURCE_KEY,
            atlas_source=ATLAS_SOURCE,
            provider="Wikidata (WDQS), community-edited; P1278 Legal Entity Identifier statements",
            licence="CC0-1.0 (https://www.wikidata.org/wiki/Wikidata:Licensing)",
            allowed_hosts=(WD_HOST,),
            datasets=(WD_DATASET,),
            dataset_ids=("wikidata:P1278",),
            cadence_seconds=30 * DAY,
            planned_requests=3,
            max_response_bytes=MAX_WD_BYTES,
            min_interval_seconds=2.0,
            parser_version=PARSER_VERSION,
            code_digest=digest,
            field_mappings={"statement": "?item p:P1278 ?statement (non-deprecated)", "lei": "ps:P1278"},
            notes="KG only (no atlas table): lei:<LEI> -> Wikidata item edges, source key wikidata.wdqs.",
        ),
    ]


async def atlas_ingest(run: Any) -> dict[str, Any]:
    if run.spec.name == WD_NAME:
        return await ingest_wikidata(run)
    out: dict[str, Any] = {}
    for ds, fn in ((DS_COUNTS, ingest_counts), (DS_FORMS, ingest_legal_forms), (DS_GROUPS, ingest_groups),
                   (DS_ENTITIES, ingest_entities)):  # fmt: skip
        if run.wants(ds):
            out[ds] = await fn(run)
    return out


__all__ = [
    "CONNECTOR_NAME",
    "COUNT_FILTERS",
    "WD_NAME",
    "LeiRecord",
    "atlas_ingest",
    "atlas_source",
    "children_count_url",
    "count_url",
    "countries_url",
    "elf_url",
    "iso2_to_key",
    "kg_records",
    "lei_valid",
    "parse_count",
    "parse_countries",
    "parse_elf_page",
    "parse_records",
    "records_url",
    "seed_leis",
    "top_groups_url",
    "wikidata_statements",
]
