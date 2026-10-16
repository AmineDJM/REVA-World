"""Culture and soft power (agent A-CULTURE): UNESCO intangible heritage, World Heritage links, UIS culture.

Three atlas sources (``atlas_source()`` returns all three; ``atlas_ingest`` dispatches on the spec name).
Every request is a GET through ``run.get`` (budget-metered live, fixture replay offline).

``atlas.unesco_ich`` -- Intangible Cultural Heritage lists (``unesco.ich``)
    UNESCO open-data portal dataset ``ich001`` (``data.unesco.org``, Opendatasoft API v2.1), licence
    **CC BY-SA 4.0** declared in the dataset record (a licence change blocks the run). Two requests:
    the dataset record (revision = ``metas.default.modified``) and a JSON export of a fixed column
    selection (:data:`ICH_FIELDS`; descriptions, images and videos are not requested). One record per
    inscribed element: ``ich_public_ref``, ``inscription_year``, list (``RL`` Representative List,
    ``USL`` Urgent Safeguarding List, ``Art18`` Register of Good Safeguarding Practices), the submitting
    States as ISO 3166-1 alpha-2 codes, and the World Heritage properties UNESCO associates with it.

    * Atlas table ``unesco_ich.ich001``: per State counts at the source revision (total, per list,
      multinational) and per inscription year. A multinational element counts once for each State.
      The dataset gives **one** year per element and the **current** list of States: a State that
      joined an element through a later extension (e.g. falconry) is counted in the element's first
      inscription year (flag ``country_may_have_joined_by_extension``). Elements removed or transferred
      between lists are not distinguishable in this dataset.
    * KG (``source_key unesco.ich``): entity ``ich:<ref>`` per element; ``P17`` country -> Wikidata
      country QID (``registry/entities/country_qids.json``) for every submitting State (the cross-country
      link of multinational elements); ``P1435`` heritage designation -> ``unesco:ich_list:<RL|USL|Art18>``
      with start time = inscription year; ``unesco:associated_whc_property`` -> ``whc:<id_no>``.

``atlas.unesco_whc_links`` -- World Heritage List as KG statements (``unesco.whc``, KGCULT's source)
    Same two requests and parser as KGCULT's ``atlas_unesco_whc`` (whose atlas table already holds the
    per-country counts); this source only loads KG statements: entity ``whc:<id_no>``; ``P17`` for every
    State Party (transboundary properties -> several countries: the LINKS material); ``P1435`` -> World
    Heritage Site (Q9259, the object KGCULT's Wikidata spec uses) with start = inscription year and the
    category, criteria, danger status, extension years as qualifiers; ``P757`` (World Heritage Site ID)
    literal, so Wikidata items harvested by KGCULT (which carry P757) can be joined on the id.

``atlas.uis_culture`` -- UNESCO UIS Data API, CULTURE theme (``unesco.uis``, shared with A-HEALTHEDU)
    The 30 indicators the UIS lists under theme CULTURE (February 2026 release): indoor cinemas,
    cultural employment (2010-2016), SDG 11.4.1 heritage expenditure per capita (2017-2024). Two
    requests: ``/versions/default`` then one data query pinned to that version. Parsing reuses
    A-HEALTHEDU's ``atlas_uis.parse_data``; rows are built here (atlas table ``uis.culture``). A record
    with magnitude ``NA`` (not applicable) whose value is served as ``0`` becomes a value-less cell with
    ``flags.missing = not_applicable`` -- never a filled zero; ``NIL`` (nil or negligible) keeps the 0.

Honesty: every count is REVA's deterministic aggregation of the source's own records
(``flags.aggregation = count``); nothing is imputed; a country without a record has no row (not a 0).
KG statements are ``source_reported_not_verified``; country objects come only from the reviewed
ISO3 -> QID registry; a State whose code has no QID there is reported, never guessed.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote

from reva.kernel.errors import SourceResponseError

ODS_HOST = "data.unesco.org"
ODS_BASE = f"https://{ODS_HOST}/api/explore/v2.1/catalog/datasets"
CC_BY_SA = "CC BY-SA 4.0"
CC_BY_SA_URL = "https://creativecommons.org/licenses/by-sa/4.0/"

# ----------------------------------------------------------------------------- ICH constants
ICH_NAME = "atlas.unesco_ich"
ICH_VERSION = "1.0.0"
ICH_PARSER = "atlas-culture-ich-1"
ICH_SOURCE_KEY = "unesco.ich"
ICH_ATLAS_SOURCE = "unesco_ich"
ICH_DATASET = "ich001"
ICH_FIELDS: tuple[str, ...] = (
    "uuid", "ich_public_ref", "inscription_year", "title_en", "title_fr", "type_acronym",
    "type_of_element_en", "type_of_element_fr", "countries", "whc_sites", "http_url_en",
)  # fmt: skip
ICH_META_URL = f"{ODS_BASE}/{ICH_DATASET}"
ICH_EXPORT_URL = f"{ODS_BASE}/{ICH_DATASET}/exports/json?select=" + ",".join(ICH_FIELDS)
ICH_MIN_ELEMENTS = 700  # 849 elements at the 2026-10-15 revision; fewer than 700 = truncated export
ICH_MAX_BYTES = 5_000_000  # ~0.4 MB on 2026-10-16
ICH_LISTS: dict[str, str] = {"RL": "rl", "USL": "usl", "Art18": "gsp"}
ICH_RIGHTS: dict[str, str] = {
    "licence": CC_BY_SA,
    "licence_url": CC_BY_SA_URL,
    "attribution": "UNESCO Intangible Cultural Heritage - Lists (data.unesco.org dataset ich001)",
    "share_alike": "derived tables must be shared under CC BY-SA 4.0",
    "source_key": ICH_SOURCE_KEY,
}

# ----------------------------------------------------------------------------- WHC links constants
WHC_LINKS_NAME = "atlas.unesco_whc_links"
WHC_LINKS_VERSION = "1.0.0"
WHC_LINKS_PARSER = "atlas-culture-whc-links-1"
WHC_LINKS_DATASET = "whc001_kg"  # KG only: no atlas table (KGCULT's unesco_whc.whc001 has the counts)
WHC_SITE_QID = "Q9259"  # "World Heritage Site", object of P1435 in KGCULT's culture.world_heritage_designations

# ----------------------------------------------------------------------------- UIS culture constants
UIS_NAME = "atlas.uis_culture"
UIS_VERSION = "1.0.0"
UIS_PARSER = "atlas-culture-uis-1"
UIS_DATASET = "culture"


@dataclass(frozen=True)
class CultureIndicator:
    code: str
    concept: str
    unit: str
    en: str
    fr: str
    subdomain: str


_PCT = "%"
_PPP = "constant 2017 PPP$ per capita"
_OCC = "Percentage of persons employed in cultural occupations that are working in "
_OCC_FR = "Part des personnes exerçant une profession culturelle qui travaillent dans "
_FEM = "Percentage of persons employed in {} that are females"
_FEM_FR = "Part des femmes parmi les personnes employées dans {}"
_DOMAINS = (  # (UIS domain number, en, fr, slug)
    ("1", "cultural and natural heritage", "le patrimoine culturel et naturel", "heritage"),
    ("2", "performance and celebration", "les arts de la scène et les célébrations", "performance"),
    ("3", "visual arts and crafts", "les arts visuels et l'artisanat", "visual_arts"),
    ("4", "books and press", "le livre et la presse", "books_press"),
    ("5", "audio-visual and interactive media", "l'audiovisuel et les médias interactifs", "audiovisual"),
    ("6", "design and creative services", "le design et les services créatifs", "design"),
    ("7", "cultural education and training", "l'éducation et la formation culturelles", "education"),
)

#: The UIS CULTURE theme (definitions endpoint, February 2026 release): code -> concept, unit, labels.
UIS_CULTURE: tuple[CultureIndicator, ...] = (
    CultureIndicator("220174", "culture.uis_indoor_cinemas", "count", "Total number of indoor cinemas",
                     "Nombre total de cinémas en salle", "cinema"),
    CultureIndicator("CE.RA.CULOCC1.1.O.CULIND1.1.OV.ILOSTAT.1", "culture.uis_cultural_employment_share", _PCT,
                     "Percentage of persons in cultural employment", "Part des personnes dans l'emploi culturel",
                     "employment"),
    CultureIndicator("CE.RA.CULOCC1.1.OV.ILOSTAT.1", "culture.uis_cultural_occupations_share", _PCT,
                     "Percentage of persons employed in cultural occupations",
                     "Part des personnes employées exerçant une profession culturelle", "employment"),
    CultureIndicator("CE.RA.CULOCC1.1.OV.ILOSTAT.1.A.SEX.1", "culture.uis_cultural_occupations_share_male", _PCT,
                     "Percentage of persons employed in cultural occupations, males",
                     "Part des personnes employées exerçant une profession culturelle, hommes", "employment"),
    CultureIndicator("CE.RA.CULOCC1.1.OV.ILOSTAT.1.A.SEX.2", "culture.uis_cultural_occupations_share_female", _PCT,
                     "Percentage of persons employed in cultural occupations, females",
                     "Part des personnes employées exerçant une profession culturelle, femmes", "employment"),
    *(
        CultureIndicator(f"CE.RA.OCCDOMAIN1.{n}.OV.CULOCC1.1", f"culture.uis_cultural_occupations_in_{slug}", _PCT,
                         _OCC + en, _OCC_FR + fr, "employment")
        for n, en, fr, slug in _DOMAINS
    ),
    CultureIndicator("CE.RA.OCCICH1.1.OV.CULOCC1.0", "culture.uis_noncultural_occupations_in_ich", _PCT,
                     "Percentage of persons employed in non cultural occupations that are working in intangible "
                     "cultural heritage",
                     "Part des personnes exerçant une profession non culturelle qui travaillent dans le patrimoine "
                     "culturel immatériel", "employment"),
    CultureIndicator("CE.RA.SEX.2.OV.CULOCC1.1", "culture.uis_cultural_occupations_female_share", _PCT,
                     "Percentage of persons employed in cultural occupations that are females",
                     "Part des femmes parmi les personnes exerçant une profession culturelle", "employment"),
    *(
        CultureIndicator(f"CE.RA.SEX.2.OV.OCCDOMAIN1.{n}", f"culture.uis_female_share_{slug}", _PCT,
                         _FEM.format("education and training" if n == "7" else en),
                         _FEM_FR.format("l'éducation et la formation" if n == "7" else fr), "employment")
        for n, en, fr, slug in _DOMAINS
    ),
    CultureIndicator("CE.RA.SEX.2.OV.OCCICH1.1", "culture.uis_female_share_ich", _PCT,
                     "Percentage of persons employed in intangible cultural heritage that are females",
                     "Part des femmes parmi les personnes employées dans le patrimoine culturel immatériel",
                     "employment"),
    CultureIndicator("HEXPCSTPPPCAP.CULHER", "culture.uis_heritage_exp_cultural_pc", _PPP,
                     "Total per capita expenditure (public and private) on cultural heritage (constant PPP$ - 2017)",
                     "Dépense totale par habitant (publique et privée) pour le patrimoine culturel ($ PPA constants "
                     "2017)", "heritage_expenditure"),
    CultureIndicator("HEXPCSTPPPCAP.HER", "culture.uis_heritage_exp_total_pc", _PPP,
                     "Total per capita expenditure on the preservation, protection and conservation of all cultural "
                     "and natural heritage (constant PPP$ - 2017)",
                     "Dépense totale par habitant pour la préservation, la protection et la conservation de "
                     "l'ensemble du patrimoine culturel et naturel ($ PPA constants 2017)", "heritage_expenditure"),
    CultureIndicator("HEXPCSTPPPCAP.HER.PR", "culture.uis_heritage_exp_private_pc", _PPP,
                     "Total per capita private expenditure on cultural and natural heritage (constant PPP$ - 2017)",
                     "Dépense privée totale par habitant pour le patrimoine culturel et naturel ($ PPA constants "
                     "2017)", "heritage_expenditure"),
    CultureIndicator("HEXPCSTPPPCAP.HER.PU", "culture.uis_heritage_exp_public_pc", _PPP,
                     "Total per capita public expenditure on cultural and natural heritage (constant PPP$ - 2017)",
                     "Dépense publique totale par habitant pour le patrimoine culturel et naturel ($ PPA constants "
                     "2017)", "heritage_expenditure"),
    CultureIndicator("HEXPCSTPPPCAP.NATHER", "culture.uis_heritage_exp_natural_pc", _PPP,
                     "Total per capita expenditure (public and private) on natural heritage (constant PPP$ - 2017)",
                     "Dépense totale par habitant (publique et privée) pour le patrimoine naturel ($ PPA constants "
                     "2017)", "heritage_expenditure"),
    CultureIndicator("HEXPCSTPPPCAPGOV.LOC.HER.PU", "culture.uis_heritage_exp_public_local_pc", _PPP,
                     "Total per capita public expenditure at local/municipal level on cultural and natural heritage "
                     "(constant PPP$ - 2017)",
                     "Dépense publique par habitant au niveau local/municipal pour le patrimoine culturel et naturel "
                     "($ PPA constants 2017)", "heritage_expenditure"),
    CultureIndicator("HEXPCSTPPPCAPGOV.NTL.HER.PU", "culture.uis_heritage_exp_public_national_pc", _PPP,
                     "Total per capita public expenditure at national/federal level on cultural and natural heritage "
                     "(constant PPP$ - 2017)",
                     "Dépense publique par habitant au niveau national/fédéral pour le patrimoine culturel et naturel "
                     "($ PPA constants 2017)", "heritage_expenditure"),
    CultureIndicator("HEXPCSTPPPCAPGOV.REG.HER.PU", "culture.uis_heritage_exp_public_regional_pc", _PPP,
                     "Total per capita public expenditure at regional level on cultural and natural heritage "
                     "(constant PPP$ - 2017)",
                     "Dépense publique par habitant au niveau régional pour le patrimoine culturel et naturel "
                     "($ PPA constants 2017)", "heritage_expenditure"),
)  # fmt: skip
UIS_BY_CODE: dict[str, CultureIndicator] = {i.code: i for i in UIS_CULTURE}
UIS_MAX_BYTES = 20_000_000  # ~0.9 MB on 2026-10-16


class CultureParseError(SourceResponseError):
    pass


def _err(msg: str) -> CultureParseError:
    return CultureParseError(msg, retryable=False, kind="schema_drift")


def _canon(obj: Mapping[str, Any]) -> str:
    return json.dumps(dict(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def atlas_frame(rows: Sequence[Mapping[str, Any]]) -> Any:
    import polars as pl

    from reva.atlas.tables import ATLAS_SCHEMA

    return pl.DataFrame(list(rows), schema=ATLAS_SCHEMA)


def _row(
    source: str,
    dataset: str,
    indicator: str,
    concept: str | None,
    geo: str,
    period: tuple[date, date],
    value: str | None,
    unit: str,
    flags: str,
    raw_sha256: str,
    selector: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "source": source, "dataset": dataset, "indicator": indicator, "concept_id": concept, "iso3": None,
        "provider_geo": geo, "entity_qid": None, "period_start": period[0], "period_end": period[1],
        "value": value, "value_text": None, "unit": unit, "flags": flags, "raw_sha256": raw_sha256,
        "selector": _canon(selector),
    }  # fmt: skip


# ----------------------------------------------------------------------------- Opendatasoft dataset record


def parse_ods_metadata(body: bytes, dataset_id: str, licence: str = CC_BY_SA) -> dict[str, Any]:
    """Revision and licence of a data.unesco.org dataset record; a licence change blocks the run."""
    try:
        doc = json.loads(body)
        meta = doc["metas"]["default"]
    except (ValueError, KeyError, TypeError) as e:
        raise _err("dataset record is not the expected Opendatasoft JSON") from e
    if doc.get("dataset_id") != dataset_id:
        raise _err(f"dataset record is for {doc.get('dataset_id')!r}, expected {dataset_id}")
    if meta.get("license") != licence:
        raise SourceResponseError(
            f"{dataset_id} licence changed to {meta.get('license')!r} (expected {licence})",
            retryable=False,
            kind="licence",
        )
    if not isinstance(meta.get("modified"), str) or not re.match(r"^\d{4}-\d{2}-\d{2}", meta["modified"]):
        raise _err(f"{dataset_id}: no usable 'modified' revision ({meta.get('modified')!r})")
    return {
        "modified": meta["modified"],
        "records_count": meta.get("records_count"),
        "licence": meta.get("license"),
        "licence_url": meta.get("license_url"),
        "publisher": meta.get("publisher"),
    }


# ----------------------------------------------------------------------------- ICH parsing


@dataclass(frozen=True)
class IchElement:
    ref: str
    year: int
    title_en: str
    title_fr: str | None
    list_code: str
    list_en: str
    list_fr: str | None
    iso2: tuple[str, ...]
    whc_refs: tuple[tuple[str, str], ...]  # (id_no, name_en)
    url: str | None
    uuid: str | None
    record_index: int


def _whc_refs(v: Any, idx: int) -> tuple[tuple[str, str], ...]:
    if v in (None, "", "[]"):
        return ()
    try:
        items = json.loads(v) if isinstance(v, str) else v
    except ValueError as e:
        raise _err(f"record {idx}: whc_sites is not JSON") from e
    if not isinstance(items, list):
        raise _err(f"record {idx}: whc_sites is not a list")
    out = []
    for it in items:
        ref = str(it.get("ref", "")).strip() if isinstance(it, dict) else ""
        if not ref.isdigit():
            raise _err(f"record {idx}: whc_sites entry without a numeric ref ({it!r})")
        out.append((ref, str(it.get("name_en") or "")))
    return tuple(sorted(set(out), key=lambda t: int(t[0])))


def parse_ich_export(body: bytes, *, min_expected: int = ICH_MIN_ELEMENTS) -> tuple[list[IchElement], dict[str, Any]]:
    try:
        rows = json.loads(body)
    except ValueError as e:
        raise _err("ich001 export is not JSON") from e
    if not isinstance(rows, list):
        raise _err("ich001 export is not a JSON array")
    out: list[IchElement] = []
    seen: set[str] = set()
    for idx, r in enumerate(rows):
        if not isinstance(r, dict):
            raise _err(f"record {idx} is not an object")
        missing = [
            f for f in ("ich_public_ref", "inscription_year", "title_en", "type_acronym", "countries") if f not in r
        ]
        if missing:
            raise _err(f"record {idx} lacks {missing}")
        ref = str(r["ich_public_ref"]).strip()
        if not ref.isdigit():
            raise _err(f"record {idx}: ich_public_ref {r['ich_public_ref']!r} is not numeric")
        if ref in seen:
            raise _err(f"duplicate ich_public_ref {ref}")
        seen.add(ref)
        year = str(r["inscription_year"]).strip()
        if not re.fullmatch(r"\d{4}", year):
            raise _err(f"record {idx}: inscription_year {r['inscription_year']!r} is not a year")
        if r["type_acronym"] not in ICH_LISTS:
            raise _err(f"record {idx}: unknown list {r['type_acronym']!r}")
        countries = r["countries"]
        if not isinstance(countries, list) or not countries:
            raise _err(f"record {idx}: countries is not a non-empty list ({countries!r})")
        iso2 = tuple(str(c).strip().upper() for c in countries)
        if any(not re.fullmatch(r"[A-Z]{2}", c) for c in iso2) or len(set(iso2)) != len(iso2):
            raise _err(f"record {idx}: malformed countries {countries!r}")
        out.append(
            IchElement(
                ref=ref,
                year=int(year),
                title_en=str(r["title_en"]),
                title_fr=r.get("title_fr"),
                list_code=r["type_acronym"],
                list_en=str(r.get("type_of_element_en") or r["type_acronym"]),
                list_fr=r.get("type_of_element_fr"),
                iso2=iso2,
                whc_refs=_whc_refs(r.get("whc_sites"), idx),
                url=r.get("http_url_en"),
                uuid=r.get("uuid"),
                record_index=idx,
            )
        )
    if len(out) < min_expected:
        raise CultureParseError(
            f"ich001 export has {len(out)} elements (< {min_expected}): truncated?", retryable=False, kind="incomplete"
        )
    return out, {
        "elements": len(out),
        "by_list": dict(Counter(e.list_code for e in out)),
        "multinational": sum(1 for e in out if len(e.iso2) > 1),
        "states": len({c for e in out for c in e.iso2}),
        "years": [min(e.year for e in out), max(e.year for e in out)] if out else [],
    }


ICH_SNAPSHOT: dict[str, tuple[str, Any]] = {
    "ich_elements_total": ("culture.ich_elements_total", lambda e: True),
    "ich_elements_rl": ("culture.ich_elements_representative_list", lambda e: e.list_code == "RL"),
    "ich_elements_usl": ("culture.ich_elements_urgent_safeguarding", lambda e: e.list_code == "USL"),
    "ich_elements_gsp": ("culture.ich_good_safeguarding_practices", lambda e: e.list_code == "Art18"),
    "ich_elements_multinational": ("culture.ich_elements_multinational", lambda e: len(e.iso2) > 1),
}
ICH_YEARLY = ("ich_elements_inscribed_in_year", "culture.ich_elements_inscribed_in_year")


def ich_atlas_rows(
    elements: Sequence[IchElement], *, raw_sha256: str, source_revision: str, retrieved_at: datetime
) -> list[dict[str, Any]]:
    """Per-State counts (``provider_geo`` = ISO alpha-2 as published; ``geo_scheme='iso2'``)."""
    day = date.fromisoformat(source_revision[:10])
    snap = (day, day + timedelta(days=1))
    base = {
        "aggregation": "count",
        "computed_by": "reva",
        "unit_of_count": "inscribed element (ich_public_ref) per submitting State",
        "source_revision": source_revision,
        "retrieved_at": retrieved_at.isoformat(),
        "licence": CC_BY_SA,
        "attribution": ICH_RIGHTS["attribution"],
    }
    by_geo: dict[str, list[IchElement]] = defaultdict(list)
    for e in elements:
        for c in e.iso2:
            by_geo[c].append(e)
    rows: list[dict[str, Any]] = []
    for geo in sorted(by_geo):
        els = by_geo[geo]
        for ind, (concept, pred) in ICH_SNAPSHOT.items():
            refs = sorted((e.ref for e in els if pred(e)), key=int)
            rows.append(
                _row(
                    ICH_ATLAS_SOURCE,
                    ICH_DATASET,
                    ind,
                    concept,
                    geo,
                    snap,
                    str(len(refs)),
                    "count",
                    _canon(base),
                    raw_sha256,
                    {"dataset": ICH_DATASET, "geo": geo, "filter": ind, "ich_public_ref": refs},
                )
            )
        per_year = Counter(e.year for e in els)
        for y in sorted(per_year):
            refs = sorted((e.ref for e in els if e.year == y), key=int)
            flags = {
                **base,
                "country_may_have_joined_by_extension": True,
                "counts_only_elements_on_list_at_revision": True,
            }
            rows.append(
                _row(
                    ICH_ATLAS_SOURCE,
                    ICH_DATASET,
                    ICH_YEARLY[0],
                    ICH_YEARLY[1],
                    geo,
                    (date(y, 1, 1), date(y + 1, 1, 1)),
                    str(per_year[y]),
                    "count",
                    _canon(flags),
                    raw_sha256,
                    {
                        "dataset": ICH_DATASET,
                        "geo": geo,
                        "field": "inscription_year",
                        "year": y,
                        "ich_public_ref": refs,
                    },
                )
            )
    return rows


# ----------------------------------------------------------------------------- KG building (ICH + WHC)

#: Dataset predicate registered by this module (UNESCO's own ICH -> World Heritage association).
KG_CUSTOM_PROPERTIES: list[dict[str, Any]] = [
    {
        "pid": "unesco:associated_whc_property",
        "value_kind": "item",
        "reva_predicate": "associated_world_heritage_property",
        "label_en": "associated World Heritage property (UNESCO ICH list)",
        "label_fr": "bien du patrimoine mondial associé (liste du PCI de l'UNESCO)",
        "inverse_name": "associated_ich_element",
        "inverse_label_en": "associated intangible heritage element (UNESCO ICH list)",
        "inverse_label_fr": "élément du patrimoine immatériel associé (liste du PCI de l'UNESCO)",
    }
]
KG_WIKIDATA_PIDS = ("P17", "P1435", "P757")


@dataclass
class KgBuild:
    entities: list[Any] = field(default_factory=list)
    statements: list[Any] = field(default_factory=list)
    unmapped_countries: list[dict[str, str]] = field(default_factory=list)


def _year_time(y: int) -> Any:
    from reva.kg.model import KgTime

    return KgTime(f"{y:04d}", 9)


def _country_qid(iso2: str, qids: Mapping[str, str]) -> tuple[str | None, str | None]:
    from reva.atlas.tables import country_iso3

    iso3 = country_iso3(iso2, "iso2")
    return iso3, (qids.get(iso3) if iso3 else None)


def ich_kg(
    elements: Sequence[IchElement], *, qids: Mapping[str, str], raw_sha256: str, revision: str, retrieved: str
) -> KgBuild:
    from reva.kg.model import KgEntity, KgStatement

    out = KgBuild()
    rev = revision[:100]
    lists: dict[str, IchElement] = {}
    for e in elements:
        lists.setdefault(e.list_code, e)
        out.entities.append(
            KgEntity(
                qid=f"ich:{e.ref}",
                kind="intangible_heritage_element",
                labels={k: v for k, v in (("en", e.title_en), ("fr", e.title_fr)) if v},
                descriptions={"en": f"UNESCO {e.list_en}, inscribed {e.year}"},
                source_revision=rev,
            )
        )
        ref = {
            "P854": e.url or f"https://ich.unesco.org/en/{e.list_code}/{int(e.ref):05d}",
            "P813": retrieved,
            "publisher": "UNESCO (data.unesco.org ich001, CC BY-SA 4.0)",
            "selector": {"dataset": ICH_DATASET, "record": e.record_index, "ich_public_ref": e.ref},
        }
        quals_common = {"ich_public_ref": e.ref, "states_count": len(e.iso2)}

        def st(sid: str, pid: str, obj: str, _e: IchElement = e, _ref: dict[str, Any] = ref, **kw: Any) -> Any:
            return KgStatement(subject_qid=f"ich:{_e.ref}", property_pid=pid, object_qid=obj, rank="normal",
                               references=(_ref,), source_key=ICH_SOURCE_KEY, source_revision=rev,
                               statement_id=f"ich:{_e.ref}:{sid}", raw_sha256=raw_sha256, **kw)  # fmt: skip

        out.statements.append(
            st(
                "list",
                "P1435",
                f"unesco:ich_list:{e.list_code}",
                start=_year_time(e.year),
                qualifiers={
                    **quals_common,
                    "list": e.list_en,
                    "note": "inscription year as published; later extensions to more States not dated",
                },
            )
        )
        for c in e.iso2:
            iso3, q = _country_qid(c, qids)
            if q is None:
                out.unmapped_countries.append(
                    {
                        "element": e.ref,
                        "iso2": c,
                        "iso3": iso3 or "",
                        "reason": "no ISO3->QID in registry" if iso3 else "iso2 not in crosswalk",
                    }
                )
                continue
            out.statements.append(
                st(f"country:{c}", "P17", q, qualifiers={**quals_common, "iso2": c, "role": "submitting State"})
            )
        for whc_ref, name in e.whc_refs:
            out.statements.append(
                st(
                    f"whc:{whc_ref}",
                    "unesco:associated_whc_property",
                    f"whc:{whc_ref}",
                    qualifiers={**quals_common, "whc_name_en": name},
                )
            )
    for code, e in sorted(lists.items()):
        out.entities.append(
            KgEntity(qid=f"unesco:ich_list:{code}", kind="heritage_list",
                     labels={k: v for k, v in (("en", e.list_en), ("fr", e.list_fr)) if v},
                     descriptions={"en": "UNESCO Convention for the Safeguarding of the Intangible Cultural Heritage"},
                     source_revision=rev)
        )  # fmt: skip
    return out


def whc_kg(props: Sequence[Any], *, qids: Mapping[str, str], raw_sha256: str, revision: str, retrieved: str) -> KgBuild:
    """KG entities/statements from KGCULT's parsed :class:`WhProperty` records."""
    from reva.kg.model import KgEntity, KgStatement

    out = KgBuild()
    rev = revision[:100]
    for p in props:
        sub = f"whc:{p.id_no}"
        out.entities.append(
            KgEntity(qid=sub, kind="heritage_site",
                     labels={k: v for k, v in (("en", p.name_en), ("fr", p.name_fr)) if v},
                     descriptions={"en": f"UNESCO World Heritage property ({p.category}), inscribed {p.inscribed_year}"},
                     source_revision=rev)
        )  # fmt: skip
        ref = {
            "P854": f"https://whc.unesco.org/en/list/{p.id_no}",
            "P813": retrieved,
            "publisher": "UNESCO World Heritage Centre (data.unesco.org whc001, CC BY-SA 4.0)",
            "selector": {"dataset": "whc001", "record": p.record_index, "id_no": p.id_no},
        }

        def st(
            sid: str,
            pid: str,
            obj: str | None = None,
            value: Mapping[str, Any] | None = None,
            _sub: str = sub,
            _ref: dict[str, Any] = ref,
            **kw: Any,
        ) -> Any:
            return KgStatement(subject_qid=_sub, property_pid=pid, object_qid=obj, value=value, rank="normal",
                               references=(_ref,), source_key="unesco.whc", source_revision=rev,
                               statement_id=f"{_sub}:{sid}", raw_sha256=raw_sha256, **kw)  # fmt: skip

        quals: dict[str, Any] = {
            "whc_id_no": p.id_no,
            "category": p.category,
            "criteria": list(p.criteria),
            "transboundary": p.transboundary,
            "in_danger": p.in_danger,
            "states_count": len(p.iso2),
        }
        if p.danger_since is not None:
            quals["danger_list_since"] = p.danger_since
        if p.extension_years:
            quals["extension_years"] = list(p.extension_years)
        if p.raw_flags:
            quals["source_flags"] = dict(p.raw_flags)
        out.statements.append(
            st("designation", "P1435", WHC_SITE_QID, start=_year_time(p.inscribed_year), qualifiers=quals)
        )
        out.statements.append(st("id", "P757", value={"type": "external_id", "value": p.id_no}))
        if not p.iso2:
            out.unmapped_countries.append(
                {
                    "element": p.id_no,
                    "iso2": "",
                    "iso3": "",
                    "reason": "no State Party code in the List: " + "; ".join(p.states_names),
                }
            )
        for c in p.iso2:
            iso3, q = _country_qid(c, qids)
            if q is None:
                out.unmapped_countries.append(
                    {
                        "element": p.id_no,
                        "iso2": c,
                        "iso3": iso3 or "",
                        "reason": "no ISO3->QID in registry" if iso3 else "iso2 not in crosswalk",
                    }
                )
                continue
            out.statements.append(
                st(
                    f"country:{c}",
                    "P17",
                    q,
                    qualifiers={
                        "whc_id_no": p.id_no,
                        "iso2": c,
                        "role": "State Party",
                        "transboundary": p.transboundary,
                    },
                )
            )
    return out


def ensure_kg_properties(engine: Any) -> dict[str, Any]:
    from reva.adapters.connectors.wikidata import load_allowlist
    from reva.kg.harvest import kg_properties
    from reva.kg.model import KgProperty, register_properties

    props = [p for p in kg_properties(load_allowlist()) if p.pid in KG_WIKIDATA_PIDS]
    if len(props) != len(KG_WIKIDATA_PIDS):
        raise ValueError(f"{KG_WIKIDATA_PIDS} must all be in registry/kg/properties.yaml")
    props += [KgProperty(**p) for p in KG_CUSTOM_PROPERTIES]
    return dict(register_properties(engine, props).__dict__)


def load_kg(
    run: Any,
    build: KgBuild,
    *,
    source_key: str,
    raw_sha256s: Sequence[str],
    retrieved_at: datetime,
    connector: str,
    parser: str,
) -> dict[str, Any]:
    """One ``load_batch``; a typed ``failed`` result (nothing written) instead of an exception."""
    from sqlalchemy.exc import DBAPIError

    from reva.kernel.errors import RevaError
    from reva.kg.model import load_batch

    try:
        ensure_kg_properties(run.engine)
        rep = load_batch(
            run.engine, run.store, entities=build.entities, statements=build.statements, source_key=source_key,
            raw_sha256s=sorted(set(raw_sha256s)), retrieved_at=retrieved_at, origin=run.origin,
            parameters={"connector": connector, "parser": parser},
        )  # fmt: skip
    except RevaError as e:
        return {"status": "failed", "error": e.message, "details": e.details}
    except DBAPIError as e:
        return {"status": "failed", "error": f"{type(e.orig).__name__}: {str(e.orig)[:300]}"}
    d = rep.as_dict()
    d.update(status="loaded", entities_built=len(build.entities), statements_built=len(build.statements),
             countries_without_qid=build.unmapped_countries[:200],
             countries_without_qid_count=len(build.unmapped_countries))  # fmt: skip
    return d


# ----------------------------------------------------------------------------- UIS culture


def uis_data_url(version: str, codes: Sequence[str] | None = None) -> str:
    from reva.adapters.connectors.atlas_uis import BASE

    q = [("indicator", c) for c in (codes or [i.code for i in UIS_CULTURE])]
    q += [("geoUnitType", "NATIONAL"), ("version", version), ("indicatorMetadata", "true"), ("footnotes", "true")]
    return f"{BASE}/data/indicators?" + "&".join(f"{k}={quote(v, safe='')}" for k, v in q)


def parse_uis_version(body: bytes) -> dict[str, Any]:
    from reva.adapters.connectors.atlas_uis import parse_version

    v = parse_version(body)
    doc = json.loads(body)
    themes = {t.get("theme"): t for t in doc.get("themeDataStatus") or [] if isinstance(t, dict)}
    cult = themes.get("CULTURE")
    if not cult:
        raise _err("UIS version record has no CULTURE theme")
    return {**v, "release": str(cult.get("description") or ""), "culture_last_update": cult.get("lastUpdate")}


def uis_rows(
    body: bytes, *, raw_sha256: str, version: str, item_key: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from reva.adapters.connectors.atlas_uis import RIGHTS as UIS_RIGHTS
    from reva.adapters.connectors.atlas_uis import parse_data
    from reva.adapters.connectors.base import dec_str
    from reva.atlas.tables import missing_flags, year_period

    records, meta = parse_data(body, tuple(UIS_BY_CODE))
    if not records:
        raise _err("the UIS CULTURE query returned no records")
    absent = sorted(set(UIS_BY_CODE) - set(meta["names"]))
    if absent:
        raise _err(f"UIS indicator metadata missing for {absent}")
    renamed = {c: n for c, n in meta["names"].items() if n.strip() != UIS_BY_CODE[c].en}
    rows = []
    for idx, r in enumerate(records):
        ind = UIS_BY_CODE[r["indicatorId"]]
        flags: dict[str, Any] = {"uis_data_version": version, "licence": UIS_RIGHTS["licence"]}
        if r["qualifier"]:
            flags["qualifier"] = str(r["qualifier"])
        if r["magnitude"]:
            flags["magnitude"] = str(r["magnitude"])
        if r["footnotes"]:
            flags["footnotes"] = [{k: str(f.get(k)) for k in ("type", "subtype", "value") if f.get(k) is not None}
                                  for f in r["footnotes"] if isinstance(f, dict)][:10]  # fmt: skip
        value: str | None = None if r["value"] is None else dec_str(r["value"])
        if r["magnitude"] == "NA":
            # UIS magnitude NA = not applicable: the served 0 is a placeholder, not a reported zero.
            flags["provider_value"] = value
            fl = missing_flags("not_applicable", **flags)
            value = None
        elif value is None:
            fl = missing_flags("not_reported", **flags)
        else:
            fl = _canon(flags)
        if len(fl) > 3900:
            raise _err(f"record {idx}: flags too long")
        rows.append(_row("uis", UIS_DATASET, ind.code, ind.concept, str(r["geoUnit"]), year_period(int(r["year"])),
                         value, ind.unit, fl, raw_sha256,
                         {"item": item_key, "record": idx, "indicatorId": r["indicatorId"], "geoUnit": r["geoUnit"],
                          "year": int(r["year"])}))  # fmt: skip
    return rows, {"records": len(records), "renamed_indicators": renamed, "hints": meta["hints"],
                  "indicators_with_records": meta["indicators_with_records"]}  # fmt: skip


# ----------------------------------------------------------------------------- protocol


def atlas_source() -> list[Any]:
    from reva.adapters.connectors.atlas_uis import HOST as UIS_HOST
    from reva.adapters.connectors.base import module_digest
    from reva.atlas.sources import AtlasSourceSpec

    digest = module_digest(atlas_source)
    return [
        AtlasSourceSpec(
            name=ICH_NAME, version=ICH_VERSION, source_key=ICH_SOURCE_KEY, atlas_source=ICH_ATLAS_SOURCE,
            provider="UNESCO Intangible Cultural Heritage (data.unesco.org)",
            licence=f"{CC_BY_SA} ({CC_BY_SA_URL}); attribution: {ICH_RIGHTS['attribution']}",
            allowed_hosts=(ODS_HOST,), datasets=(ICH_DATASET,), dataset_ids=(ICH_DATASET,),
            cadence_seconds=7 * 86400, planned_requests=2, max_response_bytes=ICH_MAX_BYTES,
            min_interval_seconds=1.0, parser_version=ICH_PARSER, code_digest=digest,
            field_mappings={"geo": "countries (ISO 3166-1 alpha-2)", "id": "ich_public_ref",
                            "year": "inscription_year", "kg": ["P17", "P1435", "unesco:associated_whc_property"]},
            notes="Current ICH lists; counts per submitting State; KG statements for elements and their States.",
        ),
        AtlasSourceSpec(
            name=WHC_LINKS_NAME, version=WHC_LINKS_VERSION, source_key="unesco.whc", budget_key="unesco.whc",
            atlas_source="unesco_whc", provider="UNESCO World Heritage Centre (data.unesco.org)",
            licence=f"{CC_BY_SA} ({CC_BY_SA_URL}); attribution: UNESCO World Heritage Centre (whc001)",
            allowed_hosts=(ODS_HOST,), datasets=(WHC_LINKS_DATASET,), dataset_ids=("whc001",),
            cadence_seconds=7 * 86400, planned_requests=2, max_response_bytes=8_000_000,
            min_interval_seconds=1.0, parser_version=WHC_LINKS_PARSER, code_digest=digest,
            field_mappings={"kg": ["P17", "P1435", "P757"], "parser": "atlas_unesco_whc.parse_export (KGCULT)"},
            notes="KG only: World Heritage properties and their States Parties (atlas counts: atlas.unesco_whc).",
        ),
        AtlasSourceSpec(
            name=UIS_NAME, version=UIS_VERSION, source_key="unesco.uis", budget_key="unesco.uis",
            atlas_source="uis", provider="UNESCO Institute for Statistics (UIS Data API, theme CULTURE)",
            licence=f"{CC_BY_SA} ({CC_BY_SA_URL}); Source: UNESCO Institute for Statistics (UIS)",
            allowed_hosts=(UIS_HOST,), datasets=(UIS_DATASET,), dataset_ids=("CULTURE",),
            cadence_seconds=30 * 86400, planned_requests=2, max_response_bytes=UIS_MAX_BYTES,
            min_interval_seconds=1.0, parser_version=UIS_PARSER, code_digest=digest,
            field_mappings={"indicators": [i.code for i in UIS_CULTURE], "geo": "geoUnit (ISO3)"},
            notes="30 CULTURE-theme indicators, national geo units, one data version per run.",
        ),
    ]  # fmt: skip


def _min_expected(run: Any, live: int) -> int:
    return 1 if run.origin == "test_fixture" else live


async def _ingest_ich(run: Any) -> dict[str, Any]:
    from reva.adapters.connectors.atlas_geo_common import load_country_qids

    if not run.wants(ICH_DATASET):
        return {"skipped": ICH_DATASET}
    raw_meta = await run.get(ICH_META_URL, key="ich001.metadata", max_bytes=1_000_000)
    meta = parse_ods_metadata(raw_meta.body, ICH_DATASET)
    raw = await run.get(ICH_EXPORT_URL, key="ich001.export", max_bytes=ICH_MAX_BYTES)
    elements, info = parse_ich_export(raw.body, min_expected=_min_expected(run, ICH_MIN_ELEMENTS))
    if run.origin == "live" and meta.get("records_count") is not None and int(meta["records_count"]) != len(elements):
        raise CultureParseError(f"export has {len(elements)} records, dataset record says {meta['records_count']}",
                                retryable=False, kind="incomplete")  # fmt: skip
    meta_sha, sha = run.archive(raw_meta), run.archive(raw)
    rows = ich_atlas_rows(elements, raw_sha256=sha, source_revision=meta["modified"], retrieved_at=raw.retrieved_at)
    reg = run.register(
        dataset=ICH_DATASET, frame=atlas_frame(rows), raw_sha256s=[meta_sha, sha], geo_scheme="iso2",
        rights=dict(ICH_RIGHTS),
        coverage_note=(f"UNESCO ICH lists (ich001, revision {meta['modified']}): {len(elements)} elements; counts "
                       "per submitting State; one inscription year per element (later extensions undated)"),
    )  # fmt: skip
    build = ich_kg(
        elements,
        qids=load_country_qids(),
        raw_sha256=sha,
        revision=meta["modified"],
        retrieved=raw.retrieved_at.date().isoformat(),
    )
    kg = load_kg(run, build, source_key=ICH_SOURCE_KEY, raw_sha256s=[sha], retrieved_at=raw.retrieved_at,
                 connector=ICH_NAME, parser=ICH_PARSER)  # fmt: skip
    return {"source_revision": meta["modified"], "parse": info,
            "atlas": {k: reg[k] for k in ("dataset_key", "artifact_sha256", "row_count", "created",
                                          "unmapped_provider_geo")}, "kg": kg}  # fmt: skip


async def _ingest_whc_links(run: Any) -> dict[str, Any]:
    from reva.adapters.connectors import atlas_unesco_whc as whc
    from reva.adapters.connectors.atlas_geo_common import load_country_qids

    if not run.wants(WHC_LINKS_DATASET):
        return {"skipped": WHC_LINKS_DATASET}
    meta_item, export_item = whc.fetch_items()
    raw_meta = await run.get(meta_item.url, key=meta_item.key, max_bytes=meta_item.max_bytes)
    meta = whc.parse_metadata(raw_meta.body)
    raw = await run.get(export_item.url, key=export_item.key, max_bytes=export_item.max_bytes)
    props, info = whc.parse_export(raw.body)
    if run.origin == "live":
        problems = whc.validate_properties(props, meta)
        if problems:
            raise CultureParseError("; ".join(problems), retryable=False, kind="incomplete")
    elif not props:
        raise _err("whc001 export is empty")
    sha = run.archive(raw)
    run.archive(raw_meta)
    build = whc_kg(
        props,
        qids=load_country_qids(),
        raw_sha256=sha,
        revision=str(meta["modified"]),
        retrieved=raw.retrieved_at.date().isoformat(),
    )
    kg = load_kg(run, build, source_key="unesco.whc", raw_sha256s=[sha], retrieved_at=raw.retrieved_at,
                 connector=WHC_LINKS_NAME, parser=WHC_LINKS_PARSER)  # fmt: skip
    return {"source_revision": meta["modified"], "properties": len(props),
            "transboundary": sum(1 for p in props if len(p.iso2) > 1),
            "schema_fingerprint": info["schema_fingerprint"], "kg": kg}  # fmt: skip


async def _ingest_uis(run: Any) -> dict[str, Any]:
    from reva.adapters.connectors.atlas_uis import RIGHTS as UIS_RIGHTS
    from reva.adapters.connectors.atlas_uis import VERSION_URL

    if not run.wants(UIS_DATASET):
        return {"skipped": UIS_DATASET}
    raw_v = await run.get(VERSION_URL, key="uis.version", max_bytes=100_000)
    v = parse_uis_version(raw_v.body)
    raw = await run.get(
        uis_data_url(v["version"]),
        key="uis.culture",
        max_bytes=UIS_MAX_BYTES,
        params={"version": v["version"], "theme": "CULTURE"},
    )
    uis_rows(raw.body, raw_sha256="0" * 64, version=v["version"], item_key="uis.culture")  # validate first
    v_sha, sha = run.archive(raw_v), run.archive(raw)
    rows, info = uis_rows(raw.body, raw_sha256=sha, version=v["version"], item_key="uis.culture")
    reg = run.register(
        dataset=UIS_DATASET, frame=atlas_frame(rows), raw_sha256s=[v_sha, sha], geo_scheme="iso3",
        rights={**UIS_RIGHTS, "source_key": "unesco.uis"},
        coverage_note=(f"UNESCO UIS Data API version {v['version']} ({v['release']}), theme CULTURE: "
                       f"{len(UIS_CULTURE)} indicators, national geo units; magnitude NA -> missing not_applicable"),
    )  # fmt: skip
    return {"uis_version": v, "parse": info,
            "atlas": {k: reg[k] for k in ("dataset_key", "artifact_sha256", "row_count", "created",
                                          "unmapped_provider_geo")}}  # fmt: skip


async def atlas_ingest(run: Any) -> dict[str, Any]:
    name = run.spec.name
    if name == ICH_NAME:
        return await _ingest_ich(run)
    if name == WHC_LINKS_NAME:
        return await _ingest_whc_links(run)
    if name == UIS_NAME:
        return await _ingest_uis(run)
    raise ValueError(f"atlas_culture has no source {name!r}")


__all__ = [
    "ICH_FIELDS",
    "UIS_CULTURE",
    "IchElement",
    "atlas_ingest",
    "atlas_source",
    "ich_atlas_rows",
    "ich_kg",
    "parse_ich_export",
    "parse_ods_metadata",
    "uis_rows",
    "whc_kg",
]
