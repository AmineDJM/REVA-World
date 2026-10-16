"""Glottolog (CLDF release on Zenodo) -> KG languoids, classification, countries, endangerment; per-country
language counts (atlas table ``glottolog.languages``); Wikidata P1394 cross-references (V3, agent KGLANG).

Source
------
Glottolog, Max Planck Institute for Evolutionary Anthropology (https://glottolog.org), released as the CLDF
dataset ``glottolog/glottolog-cldf`` on Zenodo (concept record 3260727, concept DOI 10.5281/zenodo.3260727;
**CC BY 4.0**, checked on the record of every run: another licence blocks the run). The run asks Zenodo for the
latest version of the concept record, then downloads that version's single release zip and checks its md5 and
size against the record. Both the record JSON and the zip are archived content-addressed (``run.archive``);
the zip is the ``raw_sha256`` of every statement and atlas cell. Source revision = ``<version> doi:<version DOI>``
(e.g. ``v5.3 doi:10.5281/zenodo.18840967``). Cite: Hammarström, Harald & Forkel, Robert & Haspelmath, Martin &
Bank, Sebastian. Glottolog <version>. Leipzig: Max Planck Institute for Evolutionary Anthropology (CC BY 4.0).

What is read (CLDF tables, nothing computed except counts)
----------------------------------------------------------
* ``cldf/languages.csv`` (LanguageTable): Glottocode, Name, Level, Macroarea, Latitude/Longitude, ISO639P3code,
  Countries (ISO 3166-1 alpha-2, ``;``-separated), Family_ID, Is_Isolate.
* ``cldf/values.csv`` (ValueTable): ``classification`` (path of Glottocodes from the family root),
  ``category``, ``aes`` (Agglomerated Endangerment Status with comment and source ids).
* ``cldf/codes.csv`` (CodeTable): category / AES names and the AES EGIDS-UNESCO-ElCat equivalences.
* ``cldf/names.csv``: French names (``lang = fr``, provider Lexvo) -> fr label when there is exactly one, else
  fr aliases. No other name is used and nothing is ever matched by name.

KG (``source_key`` ``glottolog.cldf``; predicates in ``registry/kg/predicates_lang.yaml``)
---------------------------------------------------------------------------------------
Entities ``glottolog:<glottocode>`` of kind ``family`` / ``language`` / ``dialect`` (all levels: ~27k entities,
well inside one batch). Statements (statement ids are stable across releases, so an unchanged fact is not
re-stored by a new release; a fact a new release drops is retracted):

* ``glottolog:parent`` (immediate parent from ``classification``) and ``glottolog:family`` (``Family_ID``):
  e.g. Portuguese -> Galician-Portuguese ... -> Romance -> ... -> Italic -> Classical Indo-European ->
  Indo-European, and Portuguese -> Indo-European in one hop. Creoles are wherever Glottolog classifies them
  (e.g. Haitian Creole under Gallo-Romance): REVA adds no lexifier link of its own.
* ``glottolog:spoken_in`` per country code of ``Countries`` -> the country QID when known
  (``country_qids.json`` / unambiguous P298 statements in the KG, as KGPOLITY resolves it) else ``iso3:<KEY>``.
  A code the crosswalk does not know is not linked and is listed in the run report.
* ``glottolog:endangerment`` (AES literal), ``glottolog:iso639_3``, ``glottolog:macroarea`` (families and
  languages), ``glottolog:coordinates`` (languages), ``glottolog:category`` (families and languages).

Every statement keeps its selector (CLDF table + row id + column/parameter) as its own reference.

Atlas (``glottolog.languages``): per country (``provider_geo`` = ISO alpha-2 of ``Countries``), exact counts of
language-level languoids listing the country, by category and, for spoken L1 languages, by AES status, with the
derived "living" (AES not endangered..nearly extinct) and "endangered" (threatened..nearly extinct) counts. Period
= the release day. Counts of Glottolog records, labelled ``aggregation: count`` -- not speaker numbers.

Wikidata cross-reference (``atlas.glottolog_wikidata``, budget/source key ``wikidata.wdqs``)
--------------------------------------------------------------------------------------------
One WDQS query through WDHARVEST's :class:`~reva.adapters.connectors.wikidata.WikidataClient` (machine-wide
lock, pacing, Retry-After back-off, raw response archived) selects every non-deprecated P1394 (Glottolog code)
statement (~12k). Each becomes ``glottolog:<code> --glottolog:wikidata_item--> Q...`` under source key
``wikidata.wdqs`` (community-edited), with the Wikidata statement id and rank. The Wikibase API has no
"items having P1394 with their values" listing (CirrusSearch returns ids only, at most 10,000 per query, and
values would need ~240 full-claims ``wbgetentities`` calls of large language items), so one WDQS request of a
few MB is the lighter path. Never mapped by name.
"""

from __future__ import annotations

import csv
import hashlib
import inspect
import io
import json
import re
import zipfile
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

NAME = "atlas.glottolog"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_glottolog/1"
SOURCE_KEY = "glottolog.cldf"
ATLAS_SOURCE = "glottolog"
DATASET = "languages"
WD_NAME = "atlas.glottolog_wikidata"
WD_SOURCE_KEY = "wikidata.wdqs"
WD_DATASET = "wikidata_xref"
WD_HOST = "query.wikidata.org"
ZENODO_HOST = "zenodo.org"
CONCEPT_RECID = "3260727"
DAY = 86400
MAX_RECORD_BYTES = 2_000_000
MAX_ZIP_BYTES = 150_000_000
MAX_MEMBER_BYTES = 300_000_000
MAX_WD_BYTES = 40_000_000
MIN_LANGUOIDS_LIVE = 20_000  # truncation guard (5.3 has 27,177); fixture mode is trimmed
CC_BY = "cc-by-4.0"
REPO_ROOT = Path(__file__).resolve().parents[4]
PREDICATES_FILE = REPO_ROOT / "registry" / "kg" / "predicates_lang.yaml"
GLOTTOCODE_RE = re.compile(r"^[a-z0-9]{4}[0-9]{4}$")
ISO2_RE = re.compile(r"^[A-Z]{2}$")
DECIMAL_TEXT_RE = re.compile(r"^-?[0-9]{1,3}(\.[0-9]{1,20})?$")

P_PARENT = "glottolog:parent"
P_FAMILY = "glottolog:family"
P_SPOKEN_IN = "glottolog:spoken_in"
P_AES = "glottolog:endangerment"
P_ISO = "glottolog:iso639_3"
P_MACROAREA = "glottolog:macroarea"
P_COORDS = "glottolog:coordinates"
P_CATEGORY = "glottolog:category"
P_WIKIDATA = "glottolog:wikidata_item"

RIGHTS = {
    "licence": "CC BY 4.0",
    "licence_url": "https://creativecommons.org/licenses/by/4.0/",
    "attribution": (
        "Hammarström, Harald & Forkel, Robert & Haspelmath, Martin & Bank, Sebastian. Glottolog (CLDF release on "
        "Zenodo, concept DOI 10.5281/zenodo.3260727). Leipzig: Max Planck Institute for Evolutionary Anthropology. "
        "https://glottolog.org"
    ),
}
LEVELS = ("family", "language", "dialect")
LEVEL_FR = {"family": "famille", "language": "langue", "dialect": "dialecte"}
#: AES code ids in order (CodeTable aes-*; numerical value 1..6)
AES_ORDER = ("aes-not_endangered", "aes-threatened", "aes-shifting", "aes-moribund", "aes-nearly_extinct", "aes-extinct")
LIVING_AES = frozenset(AES_ORDER[:5])
ENDANGERED_AES = frozenset(AES_ORDER[1:5])


class GlottologError(SourceResponseError):
    pass


def _err(msg: str, kind: str = "schema_drift") -> GlottologError:
    return GlottologError(msg, retryable=False, kind=kind)


def _digest(*modules: Any) -> str:
    h = hashlib.sha256()
    for m in modules:
        h.update(inspect.getsource(m).encode())
    h.update(PREDICATES_FILE.read_bytes())
    return "sha256:" + h.hexdigest()


def code_digest() -> str:
    return _digest(inspect.getmodule(code_digest))


# ============================================================================================== Zenodo releases
@dataclass(frozen=True)
class Release:
    """One version of a Zenodo concept record and its single release zip."""

    record_id: str
    doi: str
    concept_doi: str
    version: str
    title: str
    published: str  # YYYY-MM-DD
    licence: str
    file_key: str
    file_url: str
    size: int
    md5: str

    @property
    def revision(self) -> str:
        return f"{self.version} doi:{self.doi}"[:100]

    @property
    def day(self) -> date:
        return date.fromisoformat(self.published[:10])

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "doi": self.doi,
            "concept_doi": self.concept_doi,
            "version": self.version,
            "title": self.title,
            "published": self.published,
            "licence": self.licence,
            "file": self.file_key,
            "bytes": self.size,
            "md5": self.md5,
        }


def record_url(concept_recid: str) -> str:
    return f"https://{ZENODO_HOST}/api/records/{concept_recid}/versions/latest"


def parse_release(body: bytes, *, title_marker: str) -> Release:
    """The Zenodo record JSON -> :class:`Release`. Refuses another licence, a record without exactly one zip,
    a file URL outside zenodo.org, or a record whose title does not name the expected dataset."""
    try:
        doc = json.loads(body)
        meta = doc["metadata"]
        lic = str((meta.get("license") or {}).get("id") or "").lower()
        files = [f for f in doc.get("files") or [] if str(f.get("key", "")).endswith(".zip")]
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise _err(f"Zenodo record is not the expected JSON: {type(e).__name__}") from e
    if lic != CC_BY:
        raise _err(f"Zenodo record licence is {lic or 'missing'!r}, not CC BY 4.0: run blocked", kind="licence_changed")
    title = str(meta.get("title") or "")
    if title_marker.lower() not in title.lower():
        raise _err(f"Zenodo record title {title[:120]!r} does not name {title_marker!r}")
    if len(files) != 1:
        raise _err(f"expected exactly one release zip on the record, got {len(files)}")
    f = files[0]
    url = str((f.get("links") or {}).get("self") or "")
    if urlsplit(url).hostname != ZENODO_HOST or urlsplit(url).scheme != "https":
        raise _err("release file URL is not on https://zenodo.org")
    md5 = str(f.get("checksum") or "")
    if not md5.startswith("md5:") or len(md5) != 36:
        raise _err("release file has no md5 checksum")
    pub = str(meta.get("publication_date") or "")
    try:
        date.fromisoformat(pub[:10])
    except ValueError as e:
        raise _err("record has no publication date") from e
    return Release(
        record_id=str(doc["id"]),
        doi=str(doc.get("doi") or ""),
        concept_doi=str(doc.get("conceptdoi") or ""),
        version=str(meta.get("version") or "")[:40] or "unversioned",
        title=title[:300],
        published=pub[:10],
        licence="CC BY 4.0",
        file_key=str(f["key"]),
        file_url=url,
        size=int(f.get("size") or 0),
        md5=md5[4:],
    )


@dataclass
class Fetched:
    release: Release
    record_sha: str
    zip_sha: str
    body: bytes
    retrieved_at: datetime


async def fetch_release(run: Any, *, concept_recid: str, title_marker: str, max_zip_bytes: int) -> Fetched:
    """Record (latest version) -> release zip; both archived; md5 and size checked against the record."""
    rec = await run.get(
        record_url(concept_recid),
        key=f"zenodo:{concept_recid}:record",
        media_types=("application/json",),
        max_bytes=MAX_RECORD_BYTES,
    )
    rec_sha = run.archive(rec)
    rel = parse_release(rec.body, title_marker=title_marker)
    if rel.size > max_zip_bytes:
        raise _err(f"release zip is {rel.size} bytes, above the connector cap {max_zip_bytes}", kind="too_large")
    z = await run.get(
        rel.file_url,
        key=f"zenodo:{rel.record_id}:{rel.file_key}",
        media_types=("application/zip", "application/octet-stream"),
        max_bytes=max_zip_bytes,
        params={"doi": rel.doi, "version": rel.version},
    )
    if hashlib.md5(z.body, usedforsecurity=False).hexdigest() != rel.md5 or len(z.body) != rel.size:
        raise _err("release zip does not match the record's md5/size (truncated or changed)", kind="checksum")
    zip_sha = run.archive(z)
    return Fetched(rel, rec_sha, zip_sha, z.body, max(rec.retrieved_at, z.retrieved_at))


# ============================================================================================== CLDF zip
class CldfZip:
    """Read-only view of a CLDF release zip (one top-level folder, tables under ``cldf/``)."""

    def __init__(self, body: bytes, what: str) -> None:
        try:
            self.zf = zipfile.ZipFile(io.BytesIO(body))
        except zipfile.BadZipFile as e:
            raise _err(f"{what}: not a zip archive") from e
        self.what = what
        self.names = [i.filename for i in self.zf.infolist() if not i.is_dir()]

    def member(self, rel: str) -> str:
        hits = [n for n in self.names if n == rel or n.endswith("/" + rel)]
        hits = [n for n in hits if n.count("/") <= rel.count("/") + 1]
        if len(hits) != 1:
            raise _err(f"{self.what}: expected one member {rel!r}, found {len(hits)}")
        return hits[0]

    def has(self, rel: str) -> bool:
        return any(n.endswith("/" + rel) for n in self.names)

    def read(self, rel: str) -> bytes:
        name = self.member(rel)
        if self.zf.getinfo(name).file_size > MAX_MEMBER_BYTES:
            raise _err(f"{self.what}: member {rel} larger than {MAX_MEMBER_BYTES} bytes", kind="too_large")
        return self.zf.read(name)

    def rows(self, rel: str, required: Iterable[str]) -> list[dict[str, str]]:
        """CSV rows (dicts) with ``_line`` = the physical line number of the row's start in the file."""
        text_ = self.read(rel).decode("utf-8-sig")
        rd = csv.DictReader(io.StringIO(text_, newline=""))
        missing = [c for c in required if c not in (rd.fieldnames or [])]
        if missing:
            raise _err(f"{self.what}: {rel} lacks columns {missing}")
        out = []
        line = 2
        for r in rd:
            r = {k: (v or "") for k, v in r.items() if k is not None}
            r["_line"] = str(line)
            out.append(r)
            line = rd.reader.line_num + 1
        return out


# ============================================================================================== predicates
def load_predicates(path: Path = PREDICATES_FILE) -> list[Any]:
    from reva.kg.model import KgProperty

    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    keys = ("pid", "value_kind", "reva_predicate", "label_en", "label_fr", "inverse_name", "inverse_label_en",
            "inverse_label_fr")  # fmt: skip
    return [KgProperty(**{k: p[k] for k in keys if k in p}) for p in doc["predicates"]]


def ensure_kg_properties(engine: Any, extra: Sequence[Any] = ()) -> dict[str, Any]:
    from reva.kg.model import register_properties

    return register_properties(engine, [*load_predicates(), *extra]).__dict__


def current_keys(engine: Any, source_key: str, origin: str, prefix: str = "") -> set[str]:
    """Current statement keys of a source (optionally only ``<source_key>:<prefix>...``)."""
    from sqlalchemy import text

    like = f"{source_key}:{prefix}".replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    with engine.connect() as c:
        return set(
            c.execute(
                text("""SELECT statement_key FROM kg.statement_current
                        WHERE source_key = :s AND origin = :o AND statement_key LIKE :p"""),
                {"s": source_key, "o": origin, "p": like},
            ).scalars()
        )


def load_kg(
    engine: Any,
    store: Any,
    *,
    entities: Sequence[Any],
    statements: Sequence[Any],
    source_key: str,
    raw_sha256s: Sequence[str],
    retrieved_at: datetime,
    origin: str,
    parameters: Mapping[str, Any],
    retract_prefix: str | None = "",
    extra_properties: Sequence[Any] = (),
) -> dict[str, Any]:
    """Register the predicates, then load in bounded batches; statements of this source (and prefix) that the
    new build no longer has are retracted (``removed_at_source``). ``retract_prefix=None``: no retraction."""
    from reva.kg.model import MAX_ENTITIES_PER_BATCH, MAX_RETRACTIONS_PER_BATCH, MAX_STATEMENTS_PER_BATCH
    from reva.kg.model import load_batch, statement_key

    props = ensure_kg_properties(engine, extra_properties)
    retract: list[str] = []
    if retract_prefix is not None:
        new = {statement_key(s, source_key) for s in statements}
        retract = sorted(current_keys(engine, source_key, origin, retract_prefix) - new)
    reports = []
    n = max(
        1,
        -(-len(statements) // MAX_STATEMENTS_PER_BATCH),
        -(-len(entities) // MAX_ENTITIES_PER_BATCH),
        -(-len(retract) // MAX_RETRACTIONS_PER_BATCH),
    )
    for i in range(n):
        rep = load_batch(
            engine,
            store,
            entities=entities[i * MAX_ENTITIES_PER_BATCH : (i + 1) * MAX_ENTITIES_PER_BATCH],
            statements=statements[i * MAX_STATEMENTS_PER_BATCH : (i + 1) * MAX_STATEMENTS_PER_BATCH],
            source_key=source_key,
            raw_sha256s=sorted(set(raw_sha256s)),
            retrieved_at=retrieved_at,
            origin=origin,
            retract_keys=retract[i * MAX_RETRACTIONS_PER_BATCH : (i + 1) * MAX_RETRACTIONS_PER_BATCH],
            parameters=dict(parameters),
        )
        reports.append(rep.as_dict())
    keys = ("entities_inserted", "entities_unchanged", "entities_conflicting", "labels_inserted",
            "statements_inserted", "statements_new_revision", "statements_unchanged", "statements_stale",
            "statements_conflicting", "retractions_inserted")  # fmt: skip
    rejected: Counter[str] = Counter()
    sample: list[Any] = []
    for r in reports:
        rejected.update(r.get("rejected_counts") or {})
        sample.extend((r.get("rejected") or [])[: 20 - len(sample)])
    return {
        **{k: sum(int(r.get(k) or 0) for r in reports) for k in keys},
        "batches": len(reports),
        "load_ids": [r.get("load_id") for r in reports],
        "rejected_counts": dict(rejected),
        "rejected_sample": sample,
        "properties": props,
        "retracted_keys_sample": retract[:20],
    }


# ============================================================================================== Glottolog parse
@dataclass
class Languoid:
    gc: str
    name: str
    level: str
    line: int
    macroareas: tuple[str, ...] = ()
    lat: str | None = None
    lon: str | None = None
    iso: str | None = None
    countries: tuple[str, ...] = ()
    family_id: str | None = None
    is_isolate: bool = False
    category: str | None = None  # CodeTable id, e.g. category-Spoken_L1_Language
    classification: tuple[str, ...] = ()
    aes: str | None = None  # CodeTable id, e.g. aes-shifting
    aes_comment: str = ""
    aes_source: str = ""
    names_fr: list[str] = field(default_factory=list)

    @property
    def parent(self) -> str | None:
        return self.classification[-1] if self.classification else None


@dataclass
class GlottologData:
    languoids: dict[str, Languoid]
    codes: dict[str, dict[str, str]]  # CodeTable id -> row
    report: dict[str, Any]


LANG_COLS = ("ID", "Name", "Level", "Macroarea", "Latitude", "Longitude", "Glottocode", "ISO639P3code", "Countries",
             "Family_ID")  # fmt: skip
VALUE_COLS = ("ID", "Language_ID", "Parameter_ID", "Value", "Code_ID", "Comment", "Source")
CODE_COLS = ("ID", "Parameter_ID", "Name", "Description")
NAME_COLS = ("Language_ID", "Name", "Provider", "lang")


def _decimal_text(v: str) -> str | None:
    v = v.strip()
    return v if DECIMAL_TEXT_RE.match(v) else None


def parse_glottolog(body: bytes) -> GlottologData:
    z = CldfZip(body, "Glottolog CLDF")
    counters: Counter[str] = Counter()
    langs: dict[str, Languoid] = {}
    for r in z.rows("cldf/languages.csv", LANG_COLS):
        gc = r["Glottocode"] or r["ID"]
        if not GLOTTOCODE_RE.match(gc) or r["ID"] != gc:
            counters["languages_bad_glottocode"] += 1
            continue
        level = r["Level"].strip()
        if level not in LEVELS:
            raise _err(f"unknown Level {level!r} for {gc}")
        iso = r["ISO639P3code"].strip()
        lat, lon = _decimal_text(r["Latitude"]), _decimal_text(r["Longitude"])
        if (r["Latitude"] or r["Longitude"]) and (lat is None or lon is None):
            counters["coordinates_not_decimal"] += 1
        langs[gc] = Languoid(
            gc=gc,
            name=r["Name"].strip() or gc,
            level=level,
            line=int(r["_line"]),
            macroareas=tuple(m.strip() for m in r["Macroarea"].split(";") if m.strip()),
            lat=lat,
            lon=lon,
            iso=iso if re.match(r"^[a-z]{3}$", iso) else None,
            countries=tuple(c.strip() for c in r["Countries"].split(";") if c.strip()),
            family_id=r["Family_ID"].strip() or None,
            is_isolate=r.get("Is_Isolate", "").strip().lower() == "true",
        )
    codes = {r["ID"]: r for r in z.rows("cldf/codes.csv", CODE_COLS)}
    for r in z.rows("cldf/values.csv", VALUE_COLS):
        lg = langs.get(r["Language_ID"])
        p = r["Parameter_ID"]
        if lg is None:
            counters[f"values_unknown_language:{p}"] += 1
            continue
        if p == "category":
            lg.category = r["Code_ID"] or f"category-{r['Value']}"
        elif p == "classification":
            path = tuple(x for x in r["Value"].split("/") if x)
            if not all(GLOTTOCODE_RE.match(x) for x in path):
                counters["classification_malformed"] += 1
                continue
            lg.classification = path
        elif p == "aes":
            if r["Code_ID"] not in AES_ORDER:
                counters["aes_unknown_code"] += 1
                continue
            lg.aes, lg.aes_comment, lg.aes_source = r["Code_ID"], r["Comment"], r["Source"]
    if z.has("cldf/names.csv"):
        for r in z.rows("cldf/names.csv", NAME_COLS):
            if r["lang"] == "fr" and r["Language_ID"] in langs and r["Name"].strip():
                fr = langs[r["Language_ID"]].names_fr
                if r["Name"].strip() not in fr:
                    fr.append(r["Name"].strip())
    unknown_parent = sum(1 for lg in langs.values() if lg.parent and lg.parent not in langs)
    report = {
        "languoids": len(langs),
        "by_level": dict(Counter(lg.level for lg in langs.values())),
        "by_category": dict(Counter((lg.category or "none") for lg in langs.values())),
        "with_iso639_3": sum(1 for lg in langs.values() if lg.iso),
        "with_aes": sum(1 for lg in langs.values() if lg.aes),
        "parent_not_in_release": unknown_parent,
        "counters": dict(counters),
    }
    return GlottologData(langs, codes, report)


# ============================================================================================== Glottolog KG
@dataclass
class KgParts:
    entities: list[Any]
    statements: list[Any]
    report: dict[str, Any]


def _code_name(codes: Mapping[str, Mapping[str, str]], cid: str | None) -> str:
    if not cid:
        return "none"
    row = codes.get(cid)
    return row["Name"] if row else cid.split("-", 1)[-1].replace("_", " ")


def country_node(iso3: str, qids: Mapping[str, str]) -> tuple[str, str]:
    q = qids.get(iso3)
    return (q, "qid") if q else (f"iso3:{iso3}", "iso3_node_qid_unresolved")


def glottolog_kg(
    data: GlottologData,
    *,
    raw_sha256: str,
    source_revision: str,
    country_qids: Mapping[str, str],
    iso3_of: Callable[[str], str | None] | None = None,
) -> KgParts:
    """Entities and statements (``reva.kg.model`` records) for :func:`load_kg`."""
    from reva.kg.model import KgEntity, KgStatement

    if iso3_of is None:
        from reva.atlas.tables import country_iso3

        def iso3_of(c: str) -> str | None:
            return country_iso3(c, "iso2")

    L, codes = data.languoids, data.codes
    ents: list[Any] = []
    sts: list[Any] = []
    rep: Counter[str] = Counter()
    unmapped: Counter[str] = Counter()

    def ref(table: str, row_id: str, what: str) -> list[dict[str, Any]]:
        key = "parameter" if table == "values.csv" else "column"
        return [{"stated_in": "Glottolog (CLDF)", "selector": {"table": table, "row_id": row_id, key: what}}]

    def st(subject: str, pid: str, sid: str, refs: list[dict[str, Any]], *, obj: str | None = None,
           value: Mapping[str, Any] | None = None, quals: Mapping[str, Any] | None = None) -> None:  # fmt: skip
        sts.append(
            KgStatement(
                subject_qid=subject,
                property_pid=pid,
                object_qid=obj,
                value=dict(value) if value is not None else None,
                qualifiers=dict(quals or {}),
                references=refs,
                statement_id=sid,
                source_revision=source_revision,
                raw_sha256=raw_sha256,
            )
        )
        rep[pid] += 1

    for gc in sorted(L):
        lg = L[gc]
        node = f"glottolog:{gc}"
        cat = _code_name(codes, lg.category)
        fam = L.get(lg.family_id or "")
        desc_en = f"{lg.level} in the Glottolog classification ({cat})"
        desc_fr = f"{LEVEL_FR[lg.level]} de la classification Glottolog ({cat})"
        if fam is not None:
            desc_en += f"; family: {fam.name}"
            desc_fr += f" ; famille : {fam.name}"
        elif lg.is_isolate:
            desc_en += "; isolate"
            desc_fr += " ; isolat"
        labels = {"en": lg.name}
        aliases: dict[str, list[str]] = {}
        if len(lg.names_fr) == 1:
            labels["fr"] = lg.names_fr[0]
        elif lg.names_fr:
            aliases["fr"] = list(lg.names_fr)
        ents.append(
            KgEntity(
                qid=node,
                kind=lg.level,
                labels=labels,
                descriptions={"en": desc_en, "fr": desc_fr},
                aliases=aliases,
                source_revision=source_revision,
            )
        )
        if lg.parent:
            st(node, P_PARENT, f"parent:{gc}", ref("values.csv", f"{gc}-classification", "classification"),
               obj=f"glottolog:{lg.parent}", quals={"depth": len(lg.classification), "level": lg.level})  # fmt: skip
        if lg.family_id and lg.family_id != gc:
            st(node, P_FAMILY, f"family:{gc}", ref("languages.csv", gc, "Family_ID"), obj=f"glottolog:{lg.family_id}")
        for c in lg.countries:
            iso3 = iso3_of(c) if ISO2_RE.match(c) else None
            if iso3 is None:
                unmapped[c] += 1
                continue
            obj, how = country_node(iso3, country_qids)
            st(node, P_SPOKEN_IN, f"spoken_in:{gc}:{c}", ref("languages.csv", gc, "Countries"), obj=obj,
               quals={"iso2": c, "iso3": iso3, "country_node": how, "level": lg.level})  # fmt: skip
        if lg.aes:
            row = codes.get(lg.aes, {})
            q: dict[str, Any] = {"aes_code": lg.aes, "aes_rank": AES_ORDER.index(lg.aes) + 1}
            if row.get("Description"):
                q["equivalences"] = row["Description"]
            if lg.aes_comment:
                q["comment"] = lg.aes_comment[:2000]
            if lg.aes_source:
                q["glottolog_source_ids"] = lg.aes_source[:500]
            st(node, P_AES, f"aes:{gc}", ref("values.csv", f"{gc}-aes", "aes"),
               value={"type": "string", "value": _code_name(codes, lg.aes)}, quals=q)  # fmt: skip
        if lg.iso:
            st(node, P_ISO, f"iso639_3:{gc}", ref("languages.csv", gc, "ISO639P3code"),
               value={"type": "external_id", "value": lg.iso})  # fmt: skip
        if lg.level != "dialect":
            for m in lg.macroareas:
                st(node, P_MACROAREA, f"macroarea:{gc}:{m}", ref("languages.csv", gc, "Macroarea"),
                   value={"type": "string", "value": m})  # fmt: skip
            if lg.category:
                st(node, P_CATEGORY, f"category:{gc}", ref("values.csv", f"{gc}-category", "category"),
                   value={"type": "string", "value": cat}, quals={"category_code": lg.category})  # fmt: skip
        if lg.level == "language" and lg.lat is not None and lg.lon is not None:
            st(node, P_COORDS, f"coords:{gc}", ref("languages.csv", gc, "Latitude;Longitude"),
               value={"type": "globe_coordinate", "lat": lg.lat, "lon": lg.lon, "globe": "Q2"})  # fmt: skip
    report = {
        "entities": len(ents),
        "statements": len(sts),
        "by_property": dict(sorted(rep.items())),
        "country_codes_unmapped": dict(sorted(unmapped.items())),
        "countries_linked": len({s.qualifiers["iso3"] for s in sts if s.property_pid == P_SPOKEN_IN}),
        "country_nodes": dict(Counter(s.qualifiers["country_node"] for s in sts if s.property_pid == P_SPOKEN_IN)),
    }
    return KgParts(ents, sts, report)


def chain(data: GlottologData, gc: str) -> list[tuple[str, str]]:
    """(glottocode, name) from the languoid up to its top-level family (report/doc helper)."""
    lg = data.languoids[gc]
    path = [*lg.classification, gc][::-1]
    return [(c, data.languoids[c].name if c in data.languoids else "?") for c in path]


# ============================================================================================== atlas counts
def atlas_rows(data: GlottologData, *, raw_sha256: str, release: Release) -> list[dict[str, Any]]:
    """Exact counts of language-level languoids per country code of ``Countries`` (see module doc)."""
    L = data.languoids
    by_geo: dict[str, list[Languoid]] = defaultdict(list)
    for lg in L.values():
        if lg.level == "language":
            for c in lg.countries:
                by_geo[c].append(lg)
    cats = sorted({lg.category or "none" for lgs in by_geo.values() for lg in lgs})
    day = release.day
    period = (day, day + timedelta(days=1))
    base = {
        "aggregation": "count",
        "unit_of_count": "Glottolog language-level languoid listing the country in Countries",
        "derived_by": "REVA count of Glottolog records (not speaker numbers)",
        "glottolog_version": release.version,
        "doi": release.doi,
        "licence": RIGHTS["licence"],
        "attribution": RIGHTS["attribution"],
    }

    def spoken(lgs: Sequence[Languoid]) -> list[Languoid]:
        return [x for x in lgs if x.category == "category-Spoken_L1_Language"]

    indicators: list[tuple[str, str, Callable[[Sequence[Languoid]], list[Languoid]]]] = [
        ("languages_listed", "all language-level languoids", lambda xs: list(xs))
    ]
    for c in cats:
        indicators.append(
            (f"languages_listed|category={c.removeprefix('category-')}", f"category {c}",
             lambda xs, c=c: [x for x in xs if (x.category or "none") == c])  # fmt: skip
        )
    for a in (*AES_ORDER, None):
        name = a.removeprefix("aes-") if a else "none"
        indicators.append(
            (f"spoken_l1|aes={name}", f"category-Spoken_L1_Language and AES {a or 'not given'}",
             lambda xs, a=a: [x for x in spoken(xs) if x.aes == a])  # fmt: skip
        )
    indicators.append(
        ("spoken_l1_living", "category-Spoken_L1_Language and AES in not_endangered..nearly_extinct",
         lambda xs: [x for x in spoken(xs) if x.aes in LIVING_AES])  # fmt: skip
    )
    indicators.append(
        ("spoken_l1_endangered", "category-Spoken_L1_Language and AES in threatened..nearly_extinct",
         lambda xs: [x for x in spoken(xs) if x.aes in ENDANGERED_AES])  # fmt: skip
    )
    rows = []
    for geo in sorted(by_geo):
        lgs = by_geo[geo]
        for ind, criteria, pick in indicators:
            got = sorted(x.gc for x in pick(lgs))
            selector = {
                "file": "cldf/languages.csv",
                "column": "Countries",
                "provider_geo": geo,
                "criteria": f"Level=language; {criteria}" + ("; AES from values.csv" if "aes" in ind else ""),
                "glottocodes_sha256": hashlib.sha256(",".join(got).encode()).hexdigest(),
            }
            if len(got) <= 25:
                selector["glottocodes"] = got
            rows.append(
                {
                    "source": ATLAS_SOURCE,
                    "dataset": DATASET,
                    "indicator": ind,
                    "concept_id": CONCEPT_OF.get(ind),
                    "iso3": None,
                    "provider_geo": geo,
                    "entity_qid": None,
                    "period_start": period[0],
                    "period_end": period[1],
                    "value": str(len(got)),
                    "value_text": None,
                    "unit": "count",
                    "flags": _canon(base),
                    "raw_sha256": raw_sha256,
                    "selector": _canon(selector),
                }
            )
    return rows


CONCEPT_OF = {
    "languages_listed": "languages.languages_listed",
    "spoken_l1_living": "languages.living_spoken_l1",
    "spoken_l1_endangered": "languages.endangered_spoken_l1",
    "spoken_l1|aes=extinct": "languages.extinct_spoken_l1",
    "spoken_l1|aes=not_endangered": "languages.not_endangered_spoken_l1",
    "spoken_l1|aes=nearly_extinct": "languages.nearly_extinct_spoken_l1",
    "languages_listed|category=Sign_Language": "languages.sign_languages_listed",
    "languages_listed|category=Spoken_L1_Language": "languages.spoken_l1_listed",
}


def _canon(obj: Mapping[str, Any]) -> str:
    return json.dumps(dict(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def atlas_frame(rows: Sequence[Mapping[str, Any]]) -> Any:
    import polars as pl

    from reva.atlas.tables import ATLAS_SCHEMA

    return pl.DataFrame(list(rows), schema=ATLAS_SCHEMA)


# ============================================================================================== CLDF feature datasets
@dataclass(frozen=True)
class FeatureSource:
    """A CLDF StructureDataset whose values become per-feature literal statements on Glottolog entities."""

    ns: str  # predicate namespace (wals, grambank)
    tag: str  # provider tag in labels ("WALS", "Grambank")
    stated_in: str
    skip_values: frozenset[str] = frozenset()
    #: extra ValueTable columns kept as qualifiers (column -> qualifier name)
    keep: tuple[tuple[str, str], ...] = (("Comment", "comment"), ("Source", "sources"))


def feature_pid(ns: str, feature_id: str) -> str:
    fid = feature_id.lower()
    return f"{ns}:{fid}" if fid[:1].isalpha() else f"{ns}:f{fid}"


def feature_properties(src: FeatureSource, params: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    """One string-valued predicate per ParameterTable row, labelled with the provider's feature name."""
    from reva.kg.model import KgProperty

    out: dict[str, Any] = {}
    for p in params:
        fid = p["ID"].strip()
        if not re.match(r"^[A-Za-z0-9]{1,20}$", fid):
            raise _err(f"{src.tag}: unexpected feature id {fid[:30]!r}")
        pid = feature_pid(src.ns, fid)
        suffix = f" ({src.tag} {fid})"
        name = re.sub(r"\s+", " ", p.get("Name", "").strip()) or fid
        out[fid] = KgProperty(
            pid=pid,
            value_kind="string",
            reva_predicate=pid.replace(":", "_"),
            label_en=(name[: 200 - len(suffix)] + suffix),
        )
    return out


def feature_statements(
    src: FeatureSource,
    z: CldfZip,
    *,
    subject_of: Mapping[str, str],
    language_names: Mapping[str, str],
    raw_sha256: str,
    source_revision: str,
) -> tuple[list[Any], dict[str, Any], dict[str, Any]]:
    """ValueTable rows -> statements ``subject --<ns>:<feature>--> "<code name>"``; returns
    (statements, properties by feature id, report). ``subject_of``: provider language id -> KG node."""
    from reva.kg.model import KgStatement

    params = z.rows("cldf/parameters.csv", ("ID", "Name"))
    props = feature_properties(src, params)
    codes = {r["ID"]: r for r in z.rows("cldf/codes.csv", ("ID", "Parameter_ID", "Name"))}
    cols = ("ID", "Language_ID", "Parameter_ID", "Value", "Code_ID")
    out: list[Any] = []
    counters: Counter[str] = Counter()
    for r in z.rows("cldf/values.csv", cols):
        fid, lid, val = r["Parameter_ID"], r["Language_ID"], r["Value"].strip()
        if val in src.skip_values:
            counters[f"skipped_value:{val}"] += 1
            continue
        prop = props.get(fid)
        subject = subject_of.get(lid)
        if prop is None:
            counters["unknown_feature"] += 1
            continue
        if subject is None:
            counters["unknown_language"] += 1
            continue
        code = codes.get(r["Code_ID"]) if r["Code_ID"] else None
        if code is not None and code["Parameter_ID"] != fid:
            counters["code_of_other_feature"] += 1
            continue
        q: dict[str, Any] = {"language_id": lid, "language_name": language_names.get(lid, "")}
        if code is not None:
            q["code_id"] = code["ID"]
        q["value_as_coded"] = val
        for col, name in src.keep:
            v = (r.get(col) or "").strip()
            if v:
                q[name] = v[:1000]
        out.append(
            KgStatement(
                subject_qid=subject,
                property_pid=prop.pid,
                value={"type": "string", "value": code["Name"] if code is not None else val},
                qualifiers=q,
                references=[{"stated_in": src.stated_in, "selector": {"table": "values.csv", "row_id": r["ID"]}}],
                statement_id=f"value:{r['ID']}",
                source_revision=source_revision,
                raw_sha256=raw_sha256,
            )
        )
        counters["statements"] += 1
    report = {
        "features": len(props),
        "values_rows": sum(counters.values()),
        "counters": dict(counters),
        "subjects": len({s.subject_qid for s in out}),
    }
    return out, props, report


async def ingest_feature_dataset(
    run: Any,
    *,
    src: FeatureSource,
    concept_recid: str,
    title_marker: str,
    dataset: str,
    source_key: str,
    connector: str,
    parser_version: str,
    max_zip_bytes: int,
    subjects: Callable[[CldfZip], tuple[dict[str, str], dict[str, str], list[Any], dict[str, Any]]],
) -> dict[str, Any]:
    """Fetch the release, build entity/statement records and load them (KG only, no atlas table)."""
    if not run.wants(dataset):
        return {"skipped": True}
    f = await fetch_release(run, concept_recid=concept_recid, title_marker=title_marker, max_zip_bytes=max_zip_bytes)
    z = CldfZip(f.body, src.tag)
    subject_of, names, entities, srep = subjects(z)
    sts, props, frep = feature_statements(
        src, z, subject_of=subject_of, language_names=names, raw_sha256=f.zip_sha, source_revision=f.release.revision
    )
    known = _known_glottocodes(run.engine, run.origin)
    gl_subjects = {s.subject_qid for s in sts if s.subject_qid.startswith("glottolog:")}
    if known:
        missing = sorted(gl_subjects - known)
        frep["glottocodes_not_in_loaded_glottolog"] = {"count": len(missing), "sample": missing[:50]}
    kg = load_kg(
        run.engine,
        run.store,
        entities=entities,
        statements=sts,
        source_key=source_key,
        raw_sha256s=[f.zip_sha],
        retrieved_at=f.retrieved_at,
        origin=run.origin,
        parameters={"connector": connector, "parser": parser_version, "release": f.release.as_dict()},
        extra_properties=list(props.values()),
    )
    return {
        "release": f.release.as_dict(),
        "raw": {"record_sha256": f.record_sha, "zip_sha256": f.zip_sha, "zip_bytes": len(f.body)},
        "languages": srep,
        "build": frep,
        "kg": kg,
    }


# ============================================================================================== country QIDs
def resolve_country_qids(engine: Any, origin: str) -> tuple[dict[str, str], dict[str, Any]]:
    """ISO3 -> QID without guessing, exactly as KGPOLITY resolves it (``atlas_cliopatria.resolve_country_qids``:
    ``country_qids.json`` else unambiguous P298 statements of the same origin)."""
    from reva.adapters.connectors.atlas_cliopatria import resolve_country_qids as resolve

    return resolve(engine, origin)


# ============================================================================================== Wikidata xref
WD_QUERY = """SELECT ?item ?statement ?code ?rank WHERE {
  ?item p:P1394 ?statement .
  ?statement ps:P1394 ?code ;
             wikibase:rank ?rank .
  FILTER(?rank != wikibase:DeprecatedRank)
}"""
WD_PREFIX = "glottolog_xref:"
RANKS = {"http://wikiba.se/ontology#PreferredRank": "preferred", "http://wikiba.se/ontology#NormalRank": "normal"}


def wikidata_statements(bindings: Sequence[Mapping[str, Any]], *, raw_sha256: str, retrieved_at: datetime,
                        query_sha256: str) -> tuple[list[Any], dict[str, Any]]:  # fmt: skip
    """WDQS rows -> ``glottolog:<code> --glottolog:wikidata_item--> Q`` statements (source wikidata.wdqs)."""
    from reva.adapters.connectors.wikidata import qid_from_uri, statement_id_from_uri
    from reva.kg.model import KgStatement

    out: list[Any] = []
    counters: Counter[str] = Counter()
    seen: set[str] = set()
    revision = f"wdqs@{retrieved_at.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    for b in bindings:
        qid = qid_from_uri((b.get("item") or {}).get("value"))
        sid = statement_id_from_uri((b.get("statement") or {}).get("value"))
        code = str((b.get("code") or {}).get("value") or "")
        rank = RANKS.get(str((b.get("rank") or {}).get("value") or ""))
        if not qid or not sid or rank is None:
            counters["row_incomplete_or_deprecated"] += 1
            continue
        if not GLOTTOCODE_RE.match(code):
            counters["code_not_a_glottocode"] += 1
            continue
        if sid in seen:
            counters["duplicate_row"] += 1
            continue
        seen.add(sid)
        out.append(
            KgStatement(
                subject_qid=f"glottolog:{code}",
                property_pid=P_WIKIDATA,
                object_qid=qid,
                qualifiers={
                    "wikidata_property": "P1394",
                    "wikidata_statement_id": sid,
                    "glottocode_as_stated": code,
                    "_revision": {
                        "wikidata_revision": "unknown",
                        "reason": "WDQS rows carry no entity revision",
                        "retrieved_at": retrieved_at.isoformat(),
                    },
                },
                rank=rank,
                references=[{"stated_in": "Wikidata (P1394)", "query_sha256": query_sha256}],
                statement_id=f"{WD_PREFIX}{sid}",
                source_revision=revision,
                raw_sha256=raw_sha256,
            )
        )
    by_code = Counter(s.subject_qid for s in out)
    by_item = Counter(s.object_qid for s in out)
    report = {
        "rows": len(bindings),
        "statements": len(out),
        "glottocodes": len(by_code),
        "items": len(by_item),
        "codes_on_several_items": sum(1 for v in by_code.values() if v > 1),
        "items_with_several_codes": sum(1 for v in by_item.values() if v > 1),
        "preferred_rank": sum(1 for s in out if s.rank == "preferred"),
        "skipped": dict(counters),
    }
    return out, report


async def ingest_wikidata(run: Any) -> dict[str, Any]:
    import tempfile

    from reva.adapters.connectors.wikidata import Archive, MachineLock, WikidataClient

    if not run.wants(WD_DATASET):
        return {"skipped": True}
    lock = None
    if run.origin != "live":  # fixture replay: never wait on the machine-wide Wikidata cool-down
        lock = MachineLock(Path(tempfile.mkdtemp(prefix="reva-kglang-lock-")), min_interval=0.0)
    client = WikidataClient(run.http, archive=Archive(run.store), lock=lock, max_retries=6)
    res = await client.sparql(WD_QUERY, meta={"purpose": "glottolog_xref", "connector": WD_NAME})
    sts, report = wikidata_statements(
        res.bindings, raw_sha256=res.raw_sha256, retrieved_at=res.retrieved_at, query_sha256=res.query_sha256
    )
    known = _known_glottocodes(run.engine, run.origin)
    if known:
        report["codes_not_in_loaded_glottolog"] = sorted({s.subject_qid for s in sts} - known)[:200]
        report["codes_not_in_loaded_glottolog_count"] = len({s.subject_qid for s in sts} - known)
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
        retract_prefix=WD_PREFIX,
    )
    return {"raw_sha256": res.raw_sha256, "query_sha256": res.query_sha256, "build": report, "kg": kg,
            "client": client.stats.as_dict()}  # fmt: skip


def _known_glottocodes(engine: Any, origin: str) -> set[str]:
    from sqlalchemy import text

    with engine.connect() as c:
        return set(
            c.execute(
                text("""SELECT qid FROM kg.entity_current WHERE qid LIKE 'glottolog:%' AND origin = :o"""),
                {"o": origin},
            ).scalars()
        )


# ============================================================================================== atlas protocol
def atlas_source() -> list[AtlasSourceSpec]:
    digest = code_digest()
    return [
        AtlasSourceSpec(
            name=NAME,
            version=VERSION,
            source_key=SOURCE_KEY,
            budget_key=SOURCE_KEY,
            atlas_source=ATLAS_SOURCE,
            provider="Glottolog (Max Planck Institute for Evolutionary Anthropology), CLDF release on Zenodo",
            licence=f"{RIGHTS['licence']} ({RIGHTS['licence_url']}); attribution: {RIGHTS['attribution']}",
            allowed_hosts=(ZENODO_HOST,),
            datasets=(DATASET,),
            dataset_ids=(f"zenodo:{CONCEPT_RECID}", "doi:10.5281/zenodo.3260727"),
            cadence_seconds=30 * DAY,
            planned_requests=2,
            max_response_bytes=MAX_ZIP_BYTES,
            min_interval_seconds=1.0,
            parser_version=PARSER_VERSION,
            code_digest=digest,
            field_mappings={
                "entity": "languages.csv Glottocode -> glottolog:<glottocode>",
                "classification": "values.csv classification (parent) + languages.csv Family_ID",
                "countries": "languages.csv Countries (ISO 3166-1 alpha-2)",
                "endangerment": "values.csv aes (CodeTable aes-*)",
            },
            notes=(
                "KG languoids (all levels) with Glottolog's classification, countries, AES, ISO 639-3, macroareas, "
                "coordinates, categories; atlas table glottolog.languages = exact counts of Glottolog records per "
                "country (not speaker numbers)."
            ),
        ),
        AtlasSourceSpec(
            name=WD_NAME,
            version=VERSION,
            source_key=WD_SOURCE_KEY,
            budget_key=WD_SOURCE_KEY,
            atlas_source=ATLAS_SOURCE,
            provider="Wikidata (WDQS), community-edited; P1394 Glottolog code statements",
            licence="CC0-1.0 (https://www.wikidata.org/wiki/Wikidata:Licensing)",
            allowed_hosts=(WD_HOST,),
            datasets=(WD_DATASET,),
            dataset_ids=("wikidata:P1394",),
            cadence_seconds=30 * DAY,
            planned_requests=3,
            max_response_bytes=MAX_WD_BYTES,
            min_interval_seconds=2.0,
            parser_version=PARSER_VERSION,
            code_digest=digest,
            field_mappings={"statement": "?item p:P1394 ?statement (non-deprecated)", "code": "ps:P1394"},
            notes="KG only (no atlas table): glottolog:<code> -> Wikidata item edges, source key wikidata.wdqs.",
        ),
    ]


async def atlas_ingest(run: Any) -> dict[str, Any]:
    if run.spec.name == WD_NAME:
        return await ingest_wikidata(run)
    if not run.wants(DATASET):
        return {"skipped": True}
    f = await fetch_release(run, concept_recid=CONCEPT_RECID, title_marker="Glottolog", max_zip_bytes=MAX_ZIP_BYTES)
    data = parse_glottolog(f.body)
    if run.origin == "live" and len(data.languoids) < MIN_LANGUOIDS_LIVE:
        raise _err(f"only {len(data.languoids)} languoids: truncated release?", kind="truncated")
    qids, qinfo = resolve_country_qids(run.engine, run.origin)
    parts = glottolog_kg(data, raw_sha256=f.zip_sha, source_revision=f.release.revision, country_qids=qids)
    kg = load_kg(
        run.engine,
        run.store,
        entities=parts.entities,
        statements=parts.statements,
        source_key=SOURCE_KEY,
        raw_sha256s=[f.zip_sha],
        retrieved_at=f.retrieved_at,
        origin=run.origin,
        parameters={"connector": NAME, "parser": PARSER_VERSION, "release": f.release.as_dict()},
    )
    rows = atlas_rows(data, raw_sha256=f.zip_sha, release=f.release)
    reg = run.register(
        dataset=DATASET,
        frame=atlas_frame(rows),
        raw_sha256s=[f.zip_sha],
        geo_scheme="iso2",
        coverage_note=(
            f"Glottolog {f.release.version} (doi:{f.release.doi}): counts of language-level languoids listing each "
            "country (Countries column), by category and AES; exact counts of records, not speaker numbers."
        ),
        rights={**RIGHTS, "provider": "Glottolog", "source_key": SOURCE_KEY, "doi": f.release.doi},
    )
    return {
        "release": f.release.as_dict(),
        "raw": {"record_sha256": f.record_sha, "zip_sha256": f.zip_sha, "zip_bytes": len(f.body)},
        "parse": data.report,
        "country_qids": qinfo,
        "build": parts.report,
        "kg": kg,
        "atlas": {k: reg.get(k) for k in ("dataset_key", "row_count", "created", "unmapped_provider_geo")},
    }
