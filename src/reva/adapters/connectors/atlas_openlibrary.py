"""Open Library subjects: works per subject, per edition language and per edition year; the subjects' most
catalogued authors as KG entities with the identifiers Open Library publishes (V6, agent V6CULTURE).

Source
------
Open Library (Internet Archive), public JSON API, no key. Licensing statement (verbatim,
https://openlibrary.org/developers/licensing, read 2026-10-17): "The Internet Archive does not assert any new
copyright or other proprietary rights over any of the material in the Open Library database. There may be existing
rights issues on some contributions and in some jurisdictions." This is **not** an explicit open licence: every
cell and statement carries ``licence = no_rights_asserted_by_internet_archive`` and ``licence_review = required``
(owner decision listed in ``docs/dev/v6_culture.md``). Only counts, names, dates and identifiers are stored; author
biographies, photos and covers are never requested or kept.

Requests (one host, ≤ 1 per second):

1. ``https://openlibrary.org/subjects`` -- the "Browse Subjects" page. The subject list is the one Open Library
   itself links there (70 subjects on 2026-10-17: arts, fiction genres, sciences, business, children's, history,
   health, biography, social sciences, four place subjects, textbooks). Documented selection criterion: *the
   provider's own browse list*, not a REVA judgement of importance.
2. ``https://openlibrary.org/subjects/<key>.json?details=true&limit=1`` per subject. Open Library's subject
   engine (``openlibrary/plugins/worksearch/subjects.py``) answers from its Solr work index:
   ``work_count`` = works carrying the subject; ``ebook_count`` = those with ``has_fulltext``;
   ``languages`` = the facet ``language`` (works having an edition in that MARC language code; facet limited to
   the **25** largest values); ``publishing_history`` = the facet ``publish_year`` (works having an edition
   published that year; *all* years, Open Library drops years ≤ 1000 and > next year); ``authors`` = facet
   ``author_key`` sorted by count (25 largest).
3. ``https://openlibrary.org/authors/<OLID>.json`` for the ``authors_per_subject`` (default 5) authors with most
   works in each subject (bounded: ≤ 70 × 5 = 350 lookups, fewer after de-duplication).

Atlas table ``openlibrary.subjects`` (provider_geo ``world``: Open Library's catalogue, not a country):

* ``ol_works|subject=<key>``, ``ol_ebooks|subject=<key>`` -- snapshot at the retrieval day ``[d, d+1)``;
* ``ol_works_with_edition_language|subject=<key>|language=<MARC code>`` -- snapshot, top-25 languages only
  (flag ``facet_top_25_only``: a language absent from the facet is *not reported*, not zero);
* ``ol_works_with_edition_in_year|subject=<key>`` -- one row per calendar year: a work with editions in several
  years counts in each of them (flag ``counted_per_edition_year``), so the yearly values do not sum to
  ``ol_works``. Live index: counts change as the catalogue is edited (``live_index``).

KG (source key ``openlibrary.api``): ``openlibrary:subject.<key>`` (kind ``book_subject``) and
``openlibrary:<OLID>`` (kind ``author``, label = Open Library's ``name``). Statements:
``openlibrary:has_works_in_subject`` author -> subject (quantity of works as qualifier, snapshot date);
``openlibrary:wikidata_id`` / ``openlibrary:viaf_id`` / ``openlibrary:isni`` external identifiers exactly as Open
Library's ``remote_ids`` give them (join keys for V4LINK/WDOPS -- nothing is merged here, and an author is never
matched to a Wikidata item by name); ``openlibrary:birth_date`` / ``openlibrary:death_date`` as Open Library's
free-text strings (e.g. "15 October 70 BCE"), never parsed into dates.

Associations between subjects, languages and authors are catalogue co-occurrences, never causes.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote, unquote

from reva.adapters.connectors.atlas_sci_common import atlas_frame, canon, code_digest, trim_registration
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

NAME = "atlas.openlibrary_subjects"
VERSION = "1.0.1"  # 1.0.0 was registered in the dev DB from an in-progress copy of this module
SOURCE_KEY = "openlibrary.api"
HOST = "openlibrary.org"
ATLAS_SOURCE = "openlibrary"
DATASET = "subjects"
PARSER_VERSION = "atlas_openlibrary/1"
BROWSE_URL = f"https://{HOST}/subjects"
MAX_BYTES = 2_000_000  # a subject answer is ~15-25 kB, an author record ~2-5 kB
MIN_SUBJECTS = 20  # 70 on 2026-10-17; fewer = changed page layout
AUTHORS_PER_SUBJECT = 5
FIXTURE_SUBJECTS = ("poetry", "film", "place:brazil")
FIXTURE_AUTHORS_PER_SUBJECT = 2
LANGUAGE_FACET_LIMIT = 25  # facet.limit of the subject engine

RIGHTS: dict[str, str] = {
    "licence": "no_rights_asserted_by_internet_archive",
    "licence_statement": (
        "The Internet Archive does not assert any new copyright or other proprietary rights over any of the material "
        "in the Open Library database. There may be existing rights issues on some contributions and in some "
        "jurisdictions."
    ),
    "licence_url": "https://openlibrary.org/developers/licensing",
    "licence_review": "required",
    "attribution": "Open Library (Internet Archive), openlibrary.org subjects and authors API",
}

_SUBJECT_HREF = re.compile(r'href="/subjects/([^"#?/]+)"')
_SUBJECT_KEY = re.compile(r"^[A-Za-z0-9_.:&'()+,-]{1,120}$")
_OLID = re.compile(r"^OL[1-9][0-9]{0,11}A$")
_QID = re.compile(r"^Q[1-9][0-9]{0,11}$")


class OpenLibraryError(SourceResponseError):
    pass


def _err(msg: str) -> OpenLibraryError:
    return OpenLibraryError(msg[:2000], retryable=False, kind="schema_drift")


# ----------------------------------------------------------------------------- parsing
def parse_browse_page(body: bytes) -> list[str]:
    """Subject keys linked from the Browse Subjects page, in page order, de-duplicated (``place%3Abrazil`` ->
    ``place:brazil``)."""
    text = body.decode("utf-8", errors="replace")
    out: list[str] = []
    for m in _SUBJECT_HREF.finditer(text):
        key = unquote(m.group(1))
        if not _SUBJECT_KEY.match(key):
            raise _err(f"unexpected subject key {key!r} on the browse page")
        if key not in out:
            out.append(key)
    return out


def subject_url(key: str) -> str:
    return f"https://{HOST}/subjects/{quote(key, safe='_-.')}.json?details=true&limit=1"


def author_url(olid: str) -> str:
    return f"https://{HOST}/authors/{olid}.json"


@dataclass(frozen=True)
class Subject:
    key: str  # browse key, e.g. "poetry", "place:brazil"
    name: str
    subject_type: str
    work_count: int
    ebook_count: int | None
    languages: tuple[tuple[str, int], ...]
    publishing_history: tuple[tuple[int, int], ...]
    authors: tuple[tuple[str, str, int], ...]  # (OLID, name, works in subject)


def _int(v: Any, what: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise _err(f"{what}: {v!r} is not a non-negative integer")
    return v


def parse_subject(body: bytes, key: str) -> Subject:
    try:
        d = json.loads(body)
    except ValueError as e:
        raise _err(f"subject {key}: not JSON") from e
    if not isinstance(d, dict) or "work_count" not in d or "publishing_history" not in d:
        raise _err(f"subject {key}: details missing (keys {sorted(d)[:12] if isinstance(d, dict) else type(d)})")
    expect = "/subjects/" + key
    if str(d.get("key", "")).lower() != expect.lower():
        raise _err(f"subject {key}: answer is for {d.get('key')!r}")
    langs = []
    for x in d.get("languages") or []:
        code = str(x.get("name", ""))
        if not re.fullmatch(r"[a-z]{3}", code):
            raise _err(f"subject {key}: language facet value {code!r} is not a 3-letter MARC code")
        langs.append((code, _int(x.get("count"), f"{key} language {code}")))
    hist = []
    for pair in d.get("publishing_history") or []:
        if not (isinstance(pair, list) and len(pair) == 2):
            raise _err(f"subject {key}: publishing_history entry {pair!r}")
        hist.append((_int(pair[0], f"{key} year"), _int(pair[1], f"{key} year count")))
    authors = []
    for a in d.get("authors") or []:
        olid = str(a.get("key", "")).removeprefix("/authors/")
        if not _OLID.match(olid):
            raise _err(f"subject {key}: author key {a.get('key')!r}")
        authors.append((olid, str(a.get("name", ""))[:480], _int(a.get("count"), f"{key} author {olid}")))
    ebook = d.get("ebook_count")
    return Subject(
        key=key,
        name=str(d.get("name") or key)[:480],
        subject_type=str(d.get("subject_type") or "subject"),
        work_count=_int(d["work_count"], f"{key} work_count"),
        ebook_count=None if ebook is None else _int(ebook, f"{key} ebook_count"),
        languages=tuple(langs),
        publishing_history=tuple(sorted(hist)),
        authors=tuple(authors),
    )


@dataclass(frozen=True)
class Author:
    olid: str
    name: str
    birth_date: str | None
    death_date: str | None
    wikidata: str | None
    viaf: str | None
    isni: str | None
    redirect_to: str | None = None
    revision: str | None = None  # Open Library record revision number


def parse_author(body: bytes, olid: str) -> Author:
    try:
        d = json.loads(body)
    except ValueError as e:
        raise _err(f"author {olid}: not JSON") from e
    if not isinstance(d, dict) or d.get("key") != f"/authors/{olid}":
        raise _err(f"author {olid}: answer is for {d.get('key') if isinstance(d, dict) else d!r}")
    typ = (d.get("type") or {}).get("key")
    if typ == "/type/redirect":
        target = str(d.get("location", "")).removeprefix("/authors/")
        return Author(olid, olid, None, None, None, None, None, redirect_to=target if _OLID.match(target) else None)
    if typ != "/type/author":
        raise _err(f"author {olid}: record type {typ!r}")
    rid = d.get("remote_ids") or {}
    wd = str(rid.get("wikidata") or "").strip() or None
    return Author(
        olid=olid,
        name=str(d.get("name") or d.get("personal_name") or olid)[:480],
        birth_date=(str(d["birth_date"])[:100] if d.get("birth_date") else None),
        death_date=(str(d["death_date"])[:100] if d.get("death_date") else None),
        wikidata=wd if wd and _QID.match(wd) else None,
        viaf=(str(rid["viaf"])[:64] if rid.get("viaf") else None),
        isni=(str(rid["isni"]).replace(" ", "")[:32] if rid.get("isni") else None),
        revision=str(d.get("latest_revision") or d.get("revision") or "") or None,
    )


def top_authors(subjects: Sequence[Subject], per_subject: int) -> list[str]:
    """The ``per_subject`` authors with most works in each subject (facet order), de-duplicated, in request order."""
    out: list[str] = []
    for s in subjects:
        for olid, _name, _n in s.authors[:per_subject]:
            if olid not in out:
                out.append(olid)
    return out


# ----------------------------------------------------------------------------- atlas rows
def _row(
    indicator: str,
    period: tuple[date, date],
    n: int,
    raw_sha256: str,
    selector: Mapping[str, Any],
    flags: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "source": ATLAS_SOURCE,
        "dataset": DATASET,
        "indicator": indicator,
        "concept_id": None,
        "iso3": None,
        "provider_geo": "world",
        "entity_qid": None,
        "period_start": period[0],
        "period_end": period[1],
        "value": str(n),
        "value_text": None,
        "unit": "count",
        "flags": canon(flags),
        "raw_sha256": raw_sha256,
        "selector": canon(selector),
    }


def atlas_rows(subjects: Sequence[tuple[Subject, str, datetime]]) -> list[dict[str, Any]]:
    """``subjects`` = (parsed subject, raw sha256 of its answer, retrieved at)."""
    rows: list[dict[str, Any]] = []
    for s, sha, at in subjects:
        day = at.astimezone(UTC).date()
        snap = (day, day + timedelta(days=1))
        base = {
            "aggregation": "count_by_provider",
            "computed_by": "Open Library subject engine (Solr work index)",
            "live_index": True,
            "retrieved_at": at.isoformat(),
            "subject_name": s.name,
            "subject_type": s.subject_type,
            "geo_scope": "Open Library catalogue (not a country)",
            **{k: RIGHTS[k] for k in ("licence", "licence_review", "attribution")},
        }
        sel = {"subject": s.key, "url": subject_url(s.key)}
        rows.append(_row(f"ol_works|subject={s.key}", snap, s.work_count, sha, {**sel, "field": "work_count"}, base))
        if s.ebook_count is not None:
            rows.append(
                _row(
                    f"ol_ebooks|subject={s.key}",
                    snap,
                    s.ebook_count,
                    sha,
                    {**sel, "field": "ebook_count (facet has_fulltext=true)"},
                    base,
                )
            )
        for code, n in s.languages:
            rows.append(
                _row(
                    f"ol_works_with_edition_language|subject={s.key}|language={code}",
                    snap,
                    n,
                    sha,
                    {**sel, "field": "languages", "code": code},
                    {
                        **base,
                        "code_scheme": "MARC 21 language code",
                        "facet_top_25_only": len(s.languages) >= LANGUAGE_FACET_LIMIT,
                    },
                )
            )
        for year, n in s.publishing_history:
            rows.append(
                _row(
                    f"ol_works_with_edition_in_year|subject={s.key}",
                    (date(year, 1, 1), date(year + 1, 1, 1)),
                    n,
                    sha,
                    {**sel, "field": "publishing_history", "year": year},
                    {**base, "counted_per_edition_year": True},
                )
            )
    return rows


# ----------------------------------------------------------------------------- KG
PROPERTIES: dict[str, tuple[str, str, str, str, str | None, str | None]] = {
    "openlibrary:has_works_in_subject": (
        "item",
        "has_works_in_subject",
        "has works in subject (Open Library)",
        "a des œuvres dans le sujet (Open Library)",
        "subject_of_works_by",
        "subject of works by (Open Library)",
    ),
    "openlibrary:wikidata_id": (
        "external_id",
        "wikidata_id_per_openlibrary",
        "Wikidata ID (as Open Library gives it)",
        "identifiant Wikidata (selon Open Library)",
        None,
        None,
    ),
    "openlibrary:viaf_id": (
        "external_id",
        "viaf_id_per_openlibrary",
        "VIAF ID (as Open Library gives it)",
        "identifiant VIAF (selon Open Library)",
        None,
        None,
    ),
    "openlibrary:isni": (
        "external_id",
        "isni_per_openlibrary",
        "ISNI (as Open Library gives it)",
        "ISNI (selon Open Library)",
        None,
        None,
    ),
    "openlibrary:birth_date": (
        "string",
        "birth_date_text",
        "birth date (Open Library text)",
        "date de naissance (texte Open Library)",
        None,
        None,
    ),
    "openlibrary:death_date": (
        "string",
        "death_date_text",
        "death date (Open Library text)",
        "date de décès (texte Open Library)",
        None,
        None,
    ),
}


def subject_qid(key: str) -> str:
    return f"openlibrary:subject.{key}"


def kg_records(
    subjects: Sequence[tuple[Subject, str, datetime]], authors: Mapping[str, tuple[Author, str]], per_subject: int
) -> tuple[list[Any], list[Any], dict[str, int]]:
    from reva.kg.model import KgEntity, KgStatement, KgTime

    ents: list[Any] = []
    sts: list[Any] = []
    stats = {"subjects": 0, "authors": 0, "author_redirects": 0, "wikidata_ids": 0}
    for s, sha, at in subjects:
        ents.append(
            KgEntity(
                qid=subject_qid(s.key),
                kind="book_subject",
                labels={"en": s.name},
                descriptions={"en": f"Open Library subject ({s.subject_type}) /subjects/{s.key}"},
            )
        )
        stats["subjects"] += 1
        day = at.astimezone(UTC).date().isoformat()
        for olid, _name, n in s.authors[:per_subject]:
            got = authors.get(olid)
            if got is None or got[0].redirect_to is not None:
                continue
            sts.append(
                KgStatement(
                    subject_qid=f"openlibrary:{olid}",
                    property_pid="openlibrary:has_works_in_subject",
                    object_qid=subject_qid(s.key),
                    point_in_time=KgTime(f"+{day}T00:00:00Z", 11),
                    qualifiers={
                        "works_in_subject": n,
                        "snapshot_date": day,
                        "live_index": True,
                        "selection": f"top {per_subject} authors of the subject's author facet",
                    },
                    source_key=SOURCE_KEY,
                    raw_sha256=sha,
                    references=({"raw_sha256": sha, "selector": {"subject": s.key, "field": "authors", "key": olid}},),
                )
            )
    for olid, (a, sha) in authors.items():
        if a.redirect_to is not None:
            stats["author_redirects"] += 1
            continue
        ents.append(
            KgEntity(
                qid=f"openlibrary:{olid}",
                kind="author",
                labels={"en": a.name},
                descriptions={"en": f"Open Library author record /authors/{olid} (persons and corporate authors)"},
                source_revision=a.revision,
            )
        )
        stats["authors"] += 1
        common: dict[str, Any] = {
            "source_key": SOURCE_KEY,
            "raw_sha256": sha,
            "references": ({"raw_sha256": sha, "selector": {"author": olid}},),
        }
        for pid, v, typ in (
            ("openlibrary:wikidata_id", a.wikidata, "external_id"),
            ("openlibrary:viaf_id", a.viaf, "external_id"),
            ("openlibrary:isni", a.isni, "external_id"),
            ("openlibrary:birth_date", a.birth_date, "string"),
            ("openlibrary:death_date", a.death_date, "string"),
        ):
            if v:
                sts.append(
                    KgStatement(
                        subject_qid=f"openlibrary:{olid}", property_pid=pid, value={"type": typ, "value": v}, **common
                    )
                )
        if a.wikidata:
            stats["wikidata_ids"] += 1
    return ents, sts, stats


# ----------------------------------------------------------------------------- atlas source protocol
def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="Open Library (Internet Archive), subjects and authors JSON API",
        licence="no rights asserted by the Internet Archive (openlibrary.org/developers/licensing); licence review "
        "required",
        allowed_hosts=(HOST,),
        datasets=(DATASET,),
        dataset_ids=("subjects", "subjects/<key>.json?details=true", "authors/<OLID>.json"),
        cadence_seconds=90 * 86400,
        planned_requests=430,  # 1 browse page + 70 subjects + <= 350 authors (+ margin)
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(sys.modules[__name__]),
        field_mappings={
            "selection": "subjects linked from openlibrary.org/subjects; top authors per subject facet",
            "authors_per_subject": AUTHORS_PER_SUBJECT,
            "kg_source_key": SOURCE_KEY,
        },
        notes="~420 small GETs at 1/s (~3 MB). Options: subjects=a,b (restrict), authors_per_subject=N (0-25).",
    )


def _options(run: Any) -> tuple[list[str] | None, int]:
    opts = dict(getattr(run, "options", {}) or {})
    fixture = run.origin == "test_fixture"
    subjects = opts.get("subjects")
    if isinstance(subjects, str):
        subjects = [s for s in subjects.split(",") if s]
    if subjects is None and fixture:
        subjects = list(FIXTURE_SUBJECTS)
    per = int(opts.get("authors_per_subject", FIXTURE_AUTHORS_PER_SUBJECT if fixture else AUTHORS_PER_SUBJECT))
    if not 0 <= per <= 25:
        raise _err("authors_per_subject must be 0..25")
    return subjects, per


async def atlas_ingest(run: Any) -> dict[str, Any]:
    if not run.wants(DATASET):
        return {"skipped": True}
    from reva.adapters.connectors import atlas_mind_common as mc

    only, per = _options(run)
    page = await run.get(BROWSE_URL, key="subjects", media_types=("text/html",))
    keys = parse_browse_page(page.body)
    if len(keys) < MIN_SUBJECTS:
        raise OpenLibraryError(f"only {len(keys)} subjects on the browse page", retryable=False, kind="incomplete")
    page_sha = run.archive(page)
    if only is not None:
        unknown = [k for k in only if k not in keys]
        if unknown:
            raise _err(f"subjects not on the browse page: {unknown}")
        keys = [k for k in keys if k in only]
    subjects: list[tuple[Subject, str, datetime]] = []
    for k in keys:
        raw = await run.get(subject_url(k), key=f"subject:{k}")
        s = parse_subject(raw.body, k)
        subjects.append((s, run.archive(raw), raw.retrieved_at))
    authors: dict[str, tuple[Author, str]] = {}
    for olid in top_authors([s for s, _, _ in subjects], per):
        raw = await run.get(author_url(olid), key=f"author:{olid}")
        authors[olid] = (parse_author(raw.body, olid), run.archive(raw))
    rows = atlas_rows(subjects)
    shas = sorted({page_sha, *(sha for _, sha, _ in subjects)})
    reg = run.register(
        dataset=DATASET,
        frame=atlas_frame(rows),
        raw_sha256s=shas,
        rights=dict(RIGHTS),
        coverage_note=f"Open Library subjects linked from openlibrary.org/subjects ({len(subjects)} subjects): works, "
        "ebooks, works per edition language (top 25) and per edition year; provider_geo 'world' (the catalogue); "
        "licence: no rights asserted by the Internet Archive, review required",
    )
    out: dict[str, Any] = {"subjects": len(subjects), "authors_fetched": len(authors), "atlas": trim_registration(reg)}
    ents, sts, stats = kg_records(subjects, authors, per)
    out["kg_records"] = stats
    if not mc.kg_available(run.engine):
        out["kg"] = {"skipped": "no KG schema"}
        return out
    from reva.kg.model import KgProperty, register_properties

    register_properties(
        run.engine,
        [
            KgProperty(
                pid=p, value_kind=k, reva_predicate=rp, label_en=le, label_fr=lf, inverse_name=inv, inverse_label_en=ile
            )
            for p, (k, rp, le, lf, inv, ile) in PROPERTIES.items()
        ],
    )
    kg_shas = sorted({*shas, *(sha for _, sha in authors.values())})
    out["kg"] = kg_load(
        run.engine,
        run.store,
        entities=ents,
        statements=sts,
        raw_sha256s=kg_shas,
        retrieved_at=max((at for _, _, at in subjects), default=datetime.now(UTC)),
        origin=run.origin,
    )
    return out


def kg_load(
    engine: Any,
    store: Any,
    *,
    entities: Sequence[Any],
    statements: Sequence[Any],
    raw_sha256s: Sequence[str],
    retrieved_at: datetime,
    origin: str,
) -> dict[str, Any]:
    """``load_batch`` in bounded batches (entities first); returns summed counters and rejection reasons."""
    from collections import Counter

    from reva.kg.model import MAX_ENTITIES_PER_BATCH, MAX_STATEMENTS_PER_BATCH, load_batch

    totals: Counter[str] = Counter()
    rejected: Counter[str] = Counter()
    batches: list[tuple[list[Any], list[Any]]] = [
        (list(entities[i : i + MAX_ENTITIES_PER_BATCH]), []) for i in range(0, len(entities), MAX_ENTITIES_PER_BATCH)
    ]
    batches += [
        ([], list(statements[i : i + MAX_STATEMENTS_PER_BATCH]))
        for i in range(0, len(statements), MAX_STATEMENTS_PER_BATCH)
    ]
    for ents, sts in batches:
        used = sorted({s.raw_sha256 for s in sts if s.raw_sha256} or set(raw_sha256s))
        rep = load_batch(
            engine,
            store,
            entities=ents,
            statements=sts,
            source_key=SOURCE_KEY,
            raw_sha256s=used,
            retrieved_at=retrieved_at,
            origin=origin,
        )
        for k in (
            "entities_inserted",
            "entities_unchanged",
            "statements_inserted",
            "statements_unchanged",
            "statements_new_revision",
            "statements_conflicting",
        ):
            totals[k] += getattr(rep, k)
        rejected.update(rep.rejected_counts)
    return {**totals, "rejected": dict(rejected)}


__all__ = [
    "BROWSE_URL",
    "NAME",
    "atlas_ingest",
    "atlas_rows",
    "atlas_source",
    "author_url",
    "kg_records",
    "parse_author",
    "parse_browse_page",
    "parse_subject",
    "subject_url",
    "top_authors",
]
