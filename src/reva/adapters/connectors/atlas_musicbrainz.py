"""MusicBrainz database statistics: artists, releases, labels, events and places per country; releases and works
per language; releases per script (V6, agent V6CULTURE).

Source
------
MusicBrainz (MetaBrainz Foundation). The *core data* of the MusicBrainz Database is CC0 ("The core data of the
database is licensed under the CC0, which is effectively placing the data into the Public Domain",
https://musicbrainz.org/doc/About/Data_License, read 2026-10-17); supplementary data (tags, ratings,
annotations, editor data) is CC BY-NC-SA 3.0 and is **not** used here. The statistics pages are daily counts the
MusicBrainz server computes over core entities (artists, releases, labels, events, places, works); they carry no
separate licence statement, so every cell records ``licence_basis = counts_of_cc0_core_data``.

Two requests per run (one host, two HTML pages, ~0.7 MB):

* ``https://musicbrainz.org/statistics/countries`` -- one row per ISO 3166-1 code MusicBrainz knows, plus
  "Unknown country" (statistic code ``null``), with the columns Artists / Releases / Labels / Events / Places
  (the page's "Total" column adds different entity types and is not stored). The statistic behind each column
  is named on the page's timeline links (``count.artist.country.<code>``).
* ``https://musicbrainz.org/statistics/languages-scripts`` -- releases and works per language (ISO 639-3 code, from
  ``count.release.language.<code>`` / ``count.work.language.<code>``) and releases per script (ISO 15924 code).

Definitions (MusicBrainz server, ``lib/MusicBrainz/Server/Data/Statistics.pm``, read 2026-10-17; quoted DESC +
summarised SQL):

* ``count.artist.country`` -- "Distribution of artists per country": artists counted under the country whose area
  is the artist's area **or contains it** (area containment), else ``null``. Same rule for labels
  (``count.label.country``), places (``count.place.country``) and events (``count.event.country``, area of the event
  or of its place).
* ``count.release.country`` -- "Distribution of releases per country": releases counted once per **release event
  country** (``release_country``); a release with no release event counts under ``null``. Digital releases often
  carry release events in ~200 storefront countries, so small territories show tens of thousands of releases
  (2026-10-17: Kiribati 35,563, Tokelau 27,876): this is a count of *release events in a country*, not of music
  *from* the country (flag ``release_events_not_origin``).
* ``count.release.language`` -- "Distribution of releases by language" (the release's tracklist language);
  ``count.work.language`` -- "Distribution of works by lyrics language"; ``count.release.script`` --
  "Distribution of releases by script".

Honesty
-------
* Counts measure **what MusicBrainz volunteers have catalogued**, not national music output: coverage is very
  uneven between countries and over time; the "Unknown country" row (1.5 M of ~2.6 M artists on 2026-10-17) is
  kept (provider_geo ``mb:null``, unmapped) so the share without a country is always visible.
* MusicBrainz also uses codes that are not current countries (``XE`` Europe, ``XW`` Worldwide, ``SU`` Soviet Union,
  ``YU`` Yugoslavia, ``CS`` Serbia and Montenegro, ``XC`` Czechoslovakia, ``XG`` East Germany, ``XK`` Kosovo, ``AN``
  Netherlands Antilles): kept verbatim and never assigned to a successor (the crosswalk leaves them unmapped).
* Snapshot period = the page's "Last updated" day ``[d, d+1)``. A missing or unparsable count is not a row (never 0).
* Language and script rows have provider_geo ``world`` (counts over the whole database, not a country).
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from typing import Any

from reva.adapters.connectors.atlas_sci_common import atlas_frame, canon, code_digest, trim_registration
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

NAME = "atlas.musicbrainz_stats"
VERSION = "1.0.0"
SOURCE_KEY = "musicbrainz.stats"
HOST = "musicbrainz.org"
ATLAS_SOURCE = "musicbrainz"
DS_COUNTRY = "country_stats"
DS_LANGUAGE = "language_stats"
PARSER_VERSION = "atlas_musicbrainz/1"
COUNTRIES_URL = f"https://{HOST}/statistics/countries"
LANGUAGES_URL = f"https://{HOST}/statistics/languages-scripts"
MAX_BYTES = 4_000_000  # ~0.41 MB and ~0.31 MB on 2026-10-17
MIN_COUNTRY_ROWS = 200  # 258 rows on 2026-10-17 (fewer = truncated page or changed layout)
MIN_LANGUAGE_ROWS = 100  # 391 language rows on 2026-10-17

RIGHTS: dict[str, str] = {
    "licence": "CC0-1.0",
    "licence_url": "https://creativecommons.org/publicdomain/zero/1.0/",
    "licence_basis": "counts_of_cc0_core_data",
    "licence_statement": "https://musicbrainz.org/doc/About/Data_License",
    "attribution": "MusicBrainz database statistics (MetaBrainz Foundation), musicbrainz.org/statistics",
    "note": "statistics pages carry no separate licence; counts are over CC0 core entities only",
}

#: page column header -> (indicator, MusicBrainz statistic family, catalog concept)
COUNTRY_COLUMNS: dict[str, tuple[str, str, str]] = {
    "Artists": ("mb_artists", "count.artist.country", "culture_media.mb_artists"),
    "Releases": ("mb_release_events", "count.release.country", "culture_media.mb_release_events"),
    "Labels": ("mb_labels", "count.label.country", "culture_media.mb_labels"),
    "Events": ("mb_events", "count.event.country", "culture_media.mb_events"),
    "Places": ("mb_places", "count.place.country", "culture_media.mb_places"),
}
LANGUAGE_COLUMNS: dict[str, tuple[str, str]] = {
    "Releases": ("mb_releases_by_language", "count.release.language"),
    "Works": ("mb_works_by_lyrics_language", "count.work.language"),
}
SCRIPT_COLUMNS: dict[str, tuple[str, str]] = {"Releases": ("mb_releases_by_script", "count.release.script")}
NOT_CURRENT_COUNTRY = {
    "XE": "Europe", "XW": "Worldwide", "SU": "Soviet Union", "YU": "Yugoslavia", "CS": "Serbia and Montenegro",
    "XC": "Czechoslovakia", "XG": "East Germany", "XK": "Kosovo (no ISO 3166-1 code)", "AN": "Netherlands Antilles",
}  # fmt: skip

_UPDATED = re.compile(r"Last updated:\s*(\d{4}-\d{2}-\d{2})")
_STAT = re.compile(r"/statistics/timeline/(count\.[a-z]+\.[a-z]+)\.([A-Za-z0-9]+|null)$")
_NUM = re.compile(r"^\d{1,3}(,\d{3})*$|^\d+$")


class MusicBrainzStatsError(SourceResponseError):
    pass


def _err(msg: str) -> MusicBrainzStatsError:
    return MusicBrainzStatsError(msg[:2000], retryable=False, kind="schema_drift")


# ----------------------------------------------------------------------------- HTML parsing
@dataclass
class Cell:
    text: str = ""
    hrefs: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)


@dataclass
class Table:
    headers: list[str] = field(default_factory=list)
    rows: list[list[Cell]] = field(default_factory=list)


class _TableParser(HTMLParser):
    """Collects every ``<table id=...>``: header texts and body cells (text, hrefs, classes inside the cell)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: dict[str, Table] = {}
        self._table: Table | None = None
        self._in_thead = False
        self._row: list[Cell] | None = None
        self._cell: Cell | None = None
        self._th: list[str] | None = None
        self._skip = 0  # inside <div class="arrow"> etc. (no text there anyway)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "table" and a.get("id"):
            self._table = self.tables.setdefault(a["id"], Table())
        elif self._table is None:
            return
        elif tag == "thead":
            self._in_thead = True
        elif tag == "tr" and not self._in_thead:
            self._row = []
        elif tag == "th" and self._in_thead:
            self._th = []
        elif tag == "td" and self._row is not None:
            self._cell = Cell()
        elif self._cell is not None:
            if tag == "a" and a.get("href"):
                self._cell.hrefs.append(a["href"])
            if a.get("class"):
                self._cell.classes.extend(a["class"].split())

    def handle_endtag(self, tag: str) -> None:
        if self._table is None:
            return
        if tag == "table":
            self._table = None
        elif tag == "thead":
            self._in_thead = False
        elif tag == "th" and self._th is not None:
            self._table.headers.append(" ".join("".join(self._th).split()))
            self._th = None
        elif tag == "td" and self._cell is not None and self._row is not None:
            self._cell.text = " ".join(self._cell.text.split())
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self._table.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._th is not None:
            self._th.append(data)
        elif self._cell is not None:
            self._cell.text += data


def parse_tables(body: bytes) -> tuple[dict[str, Table], date]:
    """All id'd tables of a statistics page and its "Last updated" day."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as e:
        raise _err("statistics page is not UTF-8") from e
    m = _UPDATED.search(text)
    if not m:
        raise _err("statistics page has no 'Last updated: YYYY-MM-DD'")
    p = _TableParser()
    p.feed(text)
    p.close()
    return p.tables, date.fromisoformat(m.group(1))


def _count(cell: Cell, where: str) -> int | None:
    """``"1,518,606"`` -> 1518606; empty cell -> None (missing, never 0); anything else -> schema drift."""
    t = cell.text.strip()
    if not t:
        return None
    if not _NUM.match(t):
        raise _err(f"{where}: count {t!r} is not an integer")
    return int(t.replace(",", ""))


def _stat_code(cell: Cell, family: str) -> str | None:
    for h in cell.hrefs:
        m = _STAT.search(h)
        if m and m.group(1) == family:
            return m.group(2)
    return None


@dataclass(frozen=True)
class StatRow:
    table: str  # countries | languages | scripts
    code: str  # ISO code as MusicBrainz names the statistic, or "null"
    label: str
    counts: Mapping[str, int | None]  # header -> count
    rank: int


def _rows(
    tables: Mapping[str, Table], table_id: str, columns: Mapping[str, tuple[Any, ...]], key_header: str
) -> list[StatRow]:
    t = tables.get(table_id)
    if t is None:
        raise _err(f"table #{table_id} not found")
    hdr = t.headers
    for need in ("Rank", key_header, *columns):
        if need not in hdr:
            raise _err(f"table #{table_id}: column {need!r} missing (headers {hdr})")
    out: list[StatRow] = []
    seen: set[str] = set()
    for i, row in enumerate(t.rows):
        if len(row) != len(hdr):
            raise _err(f"table #{table_id} row {i}: {len(row)} cells for {len(hdr)} headers")
        by = dict(zip(hdr, row, strict=True))
        codes = {c for h, spec in columns.items() if (c := _stat_code(by[h], spec[1])) is not None}
        if len(codes) != 1:
            raise _err(f"table #{table_id} row {i} ({by[key_header].text!r}): statistic codes {sorted(codes)}")
        code = codes.pop()
        if code in seen:
            raise _err(f"table #{table_id}: duplicate code {code}")
        seen.add(code)
        flag = next((c[5:] for c in by[key_header].classes if c.startswith("flag-")), None)
        if flag is not None and flag != code:
            raise _err(f"table #{table_id} row {i}: flag {flag} differs from statistic code {code}")
        counts = {h: _count(by[h], f"#{table_id} {code} {h}") for h in columns}
        if "Total" in by:  # layout check: the page's Total is the sum of the counted columns (not stored)
            total = _count(by["Total"], f"#{table_id} {code} Total")
            if total is not None and None not in counts.values() and sum(v or 0 for v in counts.values()) != total:
                raise _err(f"table #{table_id} {code}: columns do not add up to Total {total} ({counts})")
        rank = by["Rank"].text
        out.append(
            StatRow(
                table=table_id.removesuffix("-table"),
                code=code,
                label=by[key_header].text,
                counts=counts,
                rank=int(rank) if rank.isdigit() else i + 1,
            )
        )
    return out


def parse_countries(body: bytes) -> tuple[list[StatRow], date]:
    tables, updated = parse_tables(body)
    rows = _rows(tables, "countries-table", COUNTRY_COLUMNS, "Country")
    return rows, updated


def parse_languages(body: bytes) -> tuple[list[StatRow], list[StatRow], date]:
    tables, updated = parse_tables(body)
    langs = _rows(tables, "languages-table", LANGUAGE_COLUMNS, "Language")
    scripts = _rows(tables, "scripts-table", SCRIPT_COLUMNS, "Script")
    return langs, scripts, updated


def validate_countries(rows: Sequence[StatRow], *, full: bool = True) -> list[str]:
    """Blocking problems of the countries page (``full=False`` for labelled fixture excerpts)."""
    problems = []
    if full and len(rows) < MIN_COUNTRY_ROWS:
        problems.append(f"only {len(rows)} country rows (< {MIN_COUNTRY_ROWS}): truncated page?")
    if not any(r.code == "null" for r in rows):
        problems.append("no 'Unknown country' row: the coverage denominator would be lost")
    return problems


def validate_languages(langs: Sequence[StatRow], *, full: bool = True) -> list[str]:
    if full and len(langs) < MIN_LANGUAGE_ROWS:
        return [f"only {len(langs)} language rows (< {MIN_LANGUAGE_ROWS}): truncated page?"]
    return []


# ----------------------------------------------------------------------------- atlas rows
def provider_geo(code: str) -> str:
    """ISO alpha-2 code verbatim (the crosswalk maps current codes; others stay unmapped); ``null`` -> ``mb:null``."""
    return "mb:null" if code == "null" else code


def _row(
    *, dataset: str, indicator: str, concept: str | None, geo: str, day: date, n: int, raw_sha256: str,
    selector: Mapping[str, Any], flags: Mapping[str, Any],
) -> dict[str, Any]:  # fmt: skip
    return {
        "source": ATLAS_SOURCE,
        "dataset": dataset,
        "indicator": indicator,
        "concept_id": concept,
        "iso3": None,
        "provider_geo": geo,
        "entity_qid": None,
        "period_start": day,
        "period_end": day + timedelta(days=1),
        "value": str(n),
        "value_text": None,
        "unit": "count",
        "flags": canon(flags),
        "raw_sha256": raw_sha256,
        "selector": canon(selector),
    }


def _base_flags(updated: date, retrieved_at: datetime) -> dict[str, Any]:
    return {
        "aggregation": "count_by_provider",
        "computed_by": "MusicBrainz server (daily statistics)",
        "stats_last_updated": updated.isoformat(),
        "retrieved_at": retrieved_at.isoformat(),
        "measures": "entries catalogued in MusicBrainz (volunteer-edited), not national output",
        **{k: RIGHTS[k] for k in ("licence", "licence_basis", "attribution")},
    }


def country_rows(rows: Sequence[StatRow], updated: date, *, raw_sha256: str, retrieved_at: datetime) -> list[dict[str, Any]]:  # fmt: skip
    base = _base_flags(updated, retrieved_at)
    out = []
    for r in rows:
        for header, (indicator, stat, concept) in COUNTRY_COLUMNS.items():
            n = r.counts[header]
            if n is None:
                continue
            flags = dict(base)
            flags["statistic"] = f"{stat}.{r.code}"
            if r.code == "null":
                flags["unknown_country"] = True
            if r.code in NOT_CURRENT_COUNTRY:
                flags["not_a_current_country"] = NOT_CURRENT_COUNTRY[r.code]
            if stat == "count.release.country":
                flags["release_events_not_origin"] = True
            out.append(
                _row(dataset=DS_COUNTRY, indicator=indicator, concept=concept, geo=provider_geo(r.code), day=updated,
                     n=n, raw_sha256=raw_sha256,
                     selector={"page": "statistics/countries", "column": header, "code": r.code, "label": r.label},
                     flags=flags)
            )  # fmt: skip
    return out


def language_rows(
    langs: Sequence[StatRow], scripts: Sequence[StatRow], updated: date, *, raw_sha256: str, retrieved_at: datetime
) -> list[dict[str, Any]]:
    base = _base_flags(updated, retrieved_at)
    out = []
    for rows, columns, dim, page_table in ((langs, LANGUAGE_COLUMNS, "language", "languages"),
                                           (scripts, SCRIPT_COLUMNS, "script", "scripts")):  # fmt: skip
        for r in rows:
            for header, (indicator, stat) in columns.items():
                n = r.counts[header]
                if n is None:
                    continue
                flags = {**base, "statistic": f"{stat}.{r.code}", "code_scheme": "ISO 639-3" if dim == "language"
                         else "ISO 15924"}  # fmt: skip
                if r.code == "null":
                    flags["unknown_" + dim] = True
                out.append(
                    _row(dataset=DS_LANGUAGE, indicator=f"{indicator}|{dim}={r.code}", concept=None, geo="world",
                         day=updated, n=n, raw_sha256=raw_sha256,
                         selector={"page": "statistics/languages-scripts", "table": page_table, "column": header,
                                   "code": r.code, "label": r.label},
                         flags=flags)
                )  # fmt: skip
    return out


# ----------------------------------------------------------------------------- atlas source protocol
def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="MusicBrainz (MetaBrainz Foundation), database statistics pages",
        licence=f"{RIGHTS['licence']} (core data; {RIGHTS['licence_statement']}); statistics = counts of core data",
        allowed_hosts=(HOST,),
        datasets=(DS_COUNTRY, DS_LANGUAGE),
        dataset_ids=("statistics/countries", "statistics/languages-scripts"),
        cadence_seconds=30 * 86400,
        planned_requests=2,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,  # MusicBrainz asks for <= 1 request/s
        parser_version=PARSER_VERSION,
        code_digest=code_digest(sys.modules[__name__]),
        field_mappings={"country_columns": {h: v[1] for h, v in COUNTRY_COLUMNS.items()},
                        "language_columns": {h: v[1] for h, v in LANGUAGE_COLUMNS.items()},
                        "geo": "ISO 3166-1 alpha-2 from count.<entity>.country.<code>"},  # fmt: skip
        notes="Two HTML statistics pages (~0.7 MB). The /ws/2 search API (release counts per country-year) answered "
        "HTTP 503 'busy' to this egress IP on 2026-10-17 and is not used.",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    want_c, want_l = run.wants(DS_COUNTRY), run.wants(DS_LANGUAGE)
    out: dict[str, Any] = {}
    full = run.origin != "test_fixture"  # fixtures are labelled excerpts of the live pages
    if want_c:
        raw = await run.get(COUNTRIES_URL, key="statistics/countries", media_types=("text/html",))
        rows, updated = parse_countries(raw.body)
        problems = validate_countries(rows, full=full)
        if problems:
            raise MusicBrainzStatsError("; ".join(problems), retryable=False, kind="incomplete")
        sha = run.archive(raw)
        frame = atlas_frame(country_rows(rows, updated, raw_sha256=sha, retrieved_at=raw.retrieved_at))
        reg = run.register(
            dataset=DS_COUNTRY, frame=frame, raw_sha256s=[sha], geo_scheme="iso2", rights=dict(RIGHTS),
            coverage_note=f"MusicBrainz statistics/countries, last updated {updated}: {len(rows)} codes incl. "
            "'Unknown country' (mb:null) and non-current codes (XE, XW, SU, YU, CS, XC, XG, XK, AN) kept unmapped; "
            "release counts are release *events* per country (digital releases list many storefront countries)",
        )  # fmt: skip
        out[DS_COUNTRY] = {"stats_last_updated": updated.isoformat(), "codes": len(rows), **trim_registration(reg)}
    if want_l:
        raw = await run.get(LANGUAGES_URL, key="statistics/languages-scripts", media_types=("text/html",))
        langs, scripts, updated = parse_languages(raw.body)
        problems = validate_languages(langs, full=full)
        if problems:
            raise MusicBrainzStatsError("; ".join(problems), retryable=False, kind="incomplete")
        sha = run.archive(raw)
        frame = atlas_frame(language_rows(langs, scripts, updated, raw_sha256=sha, retrieved_at=raw.retrieved_at))
        reg = run.register(
            dataset=DS_LANGUAGE, frame=frame, raw_sha256s=[sha], rights=dict(RIGHTS),
            coverage_note=f"MusicBrainz statistics/languages-scripts, last updated {updated}: {len(langs)} languages "
            f"(ISO 639-3), {len(scripts)} scripts (ISO 15924); provider_geo 'world' (whole database)",
        )  # fmt: skip
        out[DS_LANGUAGE] = {"stats_last_updated": updated.isoformat(), "languages": len(langs),
                            "scripts": len(scripts), **trim_registration(reg)}  # fmt: skip
    return out or {"skipped": True}


__all__ = [
    "COUNTRIES_URL",
    "LANGUAGES_URL",
    "NAME",
    "atlas_ingest",
    "atlas_source",
    "country_rows",
    "language_rows",
    "parse_countries",
    "parse_languages",
    "validate_countries",
    "validate_languages",
]
