"""Maddison Project Database 2023 -> atlas table ``maddison.mpd2023`` (V3, agent A-HISTECON).

Source
------
Bolt, J. and J. L. van Zanden (2024), "Maddison style estimates of the evolution of the world economy:
A new 2023 update", Journal of Economic Surveys; data: *Maddison Project Database, version 2023*,
DataverseNL ``doi:10.34894/INZBF2`` (Groningen Growth and Development Centre), licence **CC BY 4.0**
(stated in the dataset record, checked on every run: a different licence blocks the run).

Two requests per run, one host (``dataverse.nl``; the file access URL redirects to the SURF object
store ``objectstore.surf.nl``):

1. the dataset record (JSON: version, release time, licence, file list with SHA-1 checksums);
2. ``mpd2023_web.xlsx`` (4.9 MB), checked against the record's SHA-1 before parsing.

What is parsed
--------------
* ``Full data`` (long format: countrycode, country, region, year, gdppc, pop): one atlas row per
  reported cell -- indicator ``gdppc`` (real GDP per capita, "2011$" as the provider labels it) and
  ``pop`` (mid-year population, thousands). Empty cells are omitted (the provider reports nothing for
  them; nothing is filled). Year 1 CE .. 2022; 169 provider codes, of which CSK (Czechoslovakia),
  SUN (former USSR), YUG (former Yugoslavia) and TWN (Taiwan) are not crosswalk countries: they are kept
  (``iso3`` null) with the agent-mapped Wikidata QID of ``registry/entities/historical_states_qids.json``.
* ``Regional data``: regional and world GDP per capita / population (provider_geo ``region:<name>`` and
  ``world``; never mapped to a country).

Provider notes kept as flags
----------------------------
MPD 2023 publishes **no per-cell benchmark/interpolation flag** (earlier releases had ``i_cig``/``i_bm``;
2023 has none). Each row therefore carries ``mpd_cell_flag: "none_published"`` and, for ``gdppc``, the
provider's own source list for that country and year: the spans of the ``Sources`` sheet (sources
added since MPD 2013) that cover the year (``mpd_sources``), the general note for 2008-2022 (Total
Economy Database / UN national accounts) and, when no span covers the year, a pointer to the
``Maddison original sources`` sheet (Maddison 2010) when the country is listed there. Spans the parser
cannot read are listed per country (``mpd_unparsed_spans``), never guessed. Before 1820 the database
reports isolated benchmark *years* only (e.g. 1, 730, 1000, 1150, ...); rows for those years carry
``mpd_sparse_period: true`` (a statement about which years exist in the data, not an inference about
the estimation method).

Citation duty (provider "Notes" sheet): when shown graphically, or when fewer than 12 countries are
used, the original country papers must be cited -- the ``mpd_sources`` flag carries them.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from reva.adapters.connectors.atlas_history_common import (
    DAY,
    AtlasBuild,
    canon,
    code_digest,
    decimal_text,
    fetch_plain,
    frame,
    historical_qid,
    ingest_with_run,
    sha256_hex,
    source_spec,
    year_period,
)
from reva.adapters.connectors.atlas_xlsx import Workbook, XlsxError
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import FetchItem, RawAcquisition

NAME = "atlas.maddison.mpd2023"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_maddison/1"
SOURCE_KEY = "maddison.mpd"
ATLAS_SOURCE = "maddison"
ATLAS_DATASET = "mpd2023"
DOI = "doi:10.34894/INZBF2"
HOST = "dataverse.nl"
RECORD_URL = f"https://{HOST}/api/datasets/:persistentId/?persistentId={DOI}"
FILE_URL = f"https://{HOST}/api/access/datafile/{{id}}"
DATA_FILENAME = "mpd2023_web.xlsx"
EXPECTED_LICENCE = "CC-BY-4.0"
MAX_RECORD_BYTES = 2_000_000
MAX_FILE_BYTES = 20_000_000
MIN_ROWS = 20_000  # 2023 release: 21,366 gdppc + 17,875 pop cells; far fewer = truncated/changed file

RIGHTS: dict[str, str] = {
    "licence": "CC BY 4.0",
    "licence_url": "https://creativecommons.org/licenses/by/4.0/",
    "attribution": (
        "Maddison Project Database, version 2023. Bolt, Jutta and Jan Luiten van Zanden (2024), "
        "'Maddison style estimates of the evolution of the world economy: A new 2023 update', "
        "Journal of Economic Surveys, 1-41. DOI 10.34894/INZBF2"
    ),
    "citation_policy": (
        "Original country papers must be cited when the data are shown graphically or when fewer than "
        "12 countries are used (MPD 'Notes' sheet)"
    ),
}
UNITS = {
    "gdppc": "2011$ (real GDP per capita, provider: 'Real GDP per capita in 2011$')",
    "pop": "thousands (mid-year population)",
}
GENERAL_GDPPC_NOTE = (
    "GDP pc: 2008 - 2022: Total Economy Database the Conference Board for all countries included in TED. "
    "Otherwise UN national accounts statistics"
)
SPARSE_BEFORE = 1820
_SPAN = re.compile(r"^\s*(\d{1,4})\s*(?:[-–—]\s*(\d{1,4}))?\s*(?:\(([^()]{1,60})\))?\s*$")
_OPEN = re.compile(r"^\s*(\d{3,4})\s*[-\u2013\u2014]\s*$")
_ISO = re.compile(r"^[A-Z]{3}$")


class MaddisonParseError(SourceResponseError):
    pass


def _err(msg: str) -> MaddisonParseError:
    return MaddisonParseError(msg, retryable=False, kind="schema_drift")


# --------------------------------------------------------------------------- record
def parse_record(body: bytes) -> dict[str, Any]:
    """Dataset record -> version, licence and the data file (id, SHA-1). Licence change blocks."""
    try:
        doc = json.loads(body)
        data = doc["data"]
        lv = data["latestVersion"]
    except (ValueError, KeyError, TypeError) as e:
        raise _err("dataset record is not the expected Dataverse JSON") from e
    if lv.get("datasetPersistentId") not in (None, DOI):
        raise _err(f"record is for {lv.get('datasetPersistentId')!r}, expected {DOI}")
    lic = lv.get("license")
    lic_name = lic.get("name") if isinstance(lic, dict) else lic
    if lic_name != EXPECTED_LICENCE:
        raise SourceResponseError(
            f"Maddison licence changed to {lic_name!r} (expected {EXPECTED_LICENCE})", retryable=False, kind="licence"
        )
    files = [f.get("dataFile") or {} for f in lv.get("files") or []]
    data_files = [f for f in files if f.get("filename") == DATA_FILENAME]
    if len(data_files) != 1:
        raise _err(f"record lists {len(data_files)} files named {DATA_FILENAME}")
    df = data_files[0]
    checksum = df.get("checksum") or {}
    return {
        "version": f"{lv.get('versionNumber')}.{lv.get('versionMinorNumber')}",
        "release_time": lv.get("releaseTime"),
        "last_update_time": lv.get("lastUpdateTime"),
        "licence": lic_name,
        "file_id": int(df["id"]),
        "file_size": df.get("filesize"),
        "checksum_type": checksum.get("type"),
        "checksum": checksum.get("value"),
    }


# --------------------------------------------------------------------------- workbook
def parse_spans(rows: Sequence[Sequence[Any]], header: str = "ISO code") -> dict[str, dict[str, Any]]:
    """The provider's per-country source lists.

    Layout (both source sheets): a country line ``[ISO, name, -]`` followed by span lines
    ``[-, span, reference]`` (``span`` like ``1820-1870``, ``1`` or ``1252-1700 (England)``).
    Returns ``{iso: {"name", "spans": [(lo, hi, reference, qualifier)], "unparsed": [...]}}``.
    """
    out: dict[str, dict[str, Any]] = {}
    started = False
    cur: dict[str, Any] | None = None
    for r in rows:
        cells = [(c.strip() if isinstance(c, str) else c) for c in [*list(r), None, None, None][:3]]
        a, b, c = cells
        if not started:
            started = a == header
            continue
        if not (a or b or c):
            continue
        if isinstance(a, str) and _ISO.match(a) and b and not c:
            cur = out.setdefault(a, {"name": b, "spans": [], "unparsed": []})
            continue
        if cur is None:
            continue
        span, ref = (b, c) if a is None else (a, b)
        if not ref:
            continue
        spans = span_years(str(span or ""))
        if spans:
            cur["spans"].extend((lo, hi, str(ref), q) for lo, hi, q in spans)
        else:
            cur["unparsed"].append({"span": str(span), "source": str(ref)[:300]})
    return out


def span_years(text: str) -> list[tuple[int, int, str | None]]:
    """``'1800 - 1870'`` -> [(1800, 1870, None)]; ``'1820, 1870'`` -> two single years; ``'1993 -'`` -> open
    end (9999); ``'1252-1700 (England)'`` keeps the qualifier. Anything else -> [] (listed as unparsed)."""
    out: list[tuple[int, int, str | None]] = []
    for tok in re.split(r"\s*(?:,|&)\s*", text.strip()):
        m = _SPAN.match(tok)
        if m:
            lo = int(m.group(1))
            out.append((lo, int(m.group(2)) if m.group(2) else lo, m.group(3)))
            continue
        m = _OPEN.match(tok)
        if m:
            out.append((int(m.group(1)), 9999, None))
            continue
        return []
    return out


def _cell(cells: Mapping[int, Any], i: int) -> str | None:
    c = cells.get(i)
    return c.text if c is not None and c.kind != "empty" else None


def parse_workbook(body: bytes) -> dict[str, Any]:
    try:
        wb = Workbook(body)
    except XlsxError as e:
        raise _err(f"workbook unreadable: {e}") from e
    for need in ("Full data", "Sources", "Regional data", "Maddison original sources"):
        if need not in wb.sheets:
            raise _err(f"sheet {need!r} missing (has {sorted(wb.sheets)})")
    cells: list[dict[str, Any]] = []
    countries: dict[str, dict[str, str]] = {}
    bad: list[dict[str, Any]] = []
    header_ok = False
    for rnum, c in wb.rows("Full data"):
        if rnum == 1:
            hdr = [_cell(c, i) for i in range(6)]
            if hdr != ["countrycode", "country", "region", "year", "gdppc", "pop"]:
                raise _err(f"'Full data' header changed: {hdr}")
            header_ok = True
            continue
        code, name, region, year_s = (_cell(c, i) for i in range(4))
        if not code or not _ISO.match(code) or not year_s or not re.fullmatch(r"-?\d{1,4}", year_s):
            bad.append({"row": rnum, "countrycode": code, "year": year_s})
            continue
        countries.setdefault(code, {"name": name or "", "region": region or ""})
        for col, ind in ((4, "gdppc"), (5, "pop")):
            cell = c.get(col)
            if cell is None or cell.kind == "empty" or (cell.kind == "s" and not cell.text.strip()):
                continue
            v = decimal_text(cell.text) if cell.kind in ("n", "s") else None
            if v is None:
                bad.append({"row": rnum, "column": ind, "text": cell.text[:40], "kind": cell.kind})
                continue
            cells.append(
                {
                    "code": code,
                    "year": int(year_s),
                    "indicator": ind,
                    "value": v,
                    "row": rnum,
                    "col": "E" if col == 4 else "F",
                }
            )
    if not header_ok:
        raise _err("'Full data' sheet is empty")
    regional = _parse_regional(wb.table("Regional data"))
    return {
        "cells": cells,
        "countries": countries,
        "bad_cells": bad,
        "sources": parse_spans(wb.table("Sources")),
        "original_sources": parse_spans(wb.table("Maddison original sources")),
        "regional": regional,
    }


def _parse_regional(t: list[list[Any]]) -> list[dict[str, Any]]:
    """Regional sheet: row 2 holds GDP pc regions (cols B-I), World GDP pc (col J), population regions
    (cols L-S) and World population (col T); data rows start at row 3 (year in col A)."""
    if len(t) < 3:
        raise _err("'Regional data' sheet too short")
    top, names = t[0], t[1]
    gdp_cols: dict[int, str] = {}
    pop_cols: dict[int, str] = {}
    pop_start = next(
        (i for i, x in enumerate(top) if isinstance(x, str) and x.strip().startswith("Regional Population")), None
    )
    world_gdp = next((i for i, x in enumerate(top) if isinstance(x, str) and x.strip() == "World GDP pc"), None)
    if pop_start is None or world_gdp is None:
        raise _err("'Regional data' layout changed (no 'World GDP pc' / 'Regional Population' header)")
    for i, n in enumerate(names):
        if i == 0:
            continue
        if i < world_gdp and n:
            gdp_cols[i] = f"region:{n.strip()}"
        elif i >= pop_start and n:
            pop_cols[i] = "world" if n.strip() == "World Population" else f"region:{n.strip()}"
    gdp_cols[world_gdp] = "world"
    out: list[dict[str, Any]] = []
    for ridx, r in enumerate(t[2:], start=3):
        y = r[0] if r else None
        if not y or not re.fullmatch(r"\d{1,4}", str(y).strip()):
            continue
        for cols, ind in ((gdp_cols, "gdppc"), (pop_cols, "pop")):
            for i, geo in cols.items():
                v = decimal_text(r[i]) if i < len(r) and r[i] is not None else None
                if v is None:
                    continue
                out.append(
                    {
                        "geo": geo,
                        "year": int(str(y).strip()),
                        "indicator": ind,
                        "value": v,
                        "row": ridx,
                        "col": i,
                        "label": names[i],
                    }
                )
    return out


# --------------------------------------------------------------------------- atlas rows
def _sources_flags(code: str, year: int, parsed: Mapping[str, Any]) -> dict[str, Any]:
    src = parsed["sources"].get(code)
    covering = [
        {"span": f"{lo}-{hi}", "source": text[:300], **({"territory": q} if q else {})}
        for lo, hi, text, q in (src["spans"] if src else [])
        if lo <= year <= hi
    ]
    flags: dict[str, Any] = {"mpd_sources": covering}
    if 2008 <= year <= 2022:
        flags["mpd_general_note"] = GENERAL_GDPPC_NOTE
    if not covering:
        if code in parsed["original_sources"]:
            flags["mpd_source_list"] = "no MPD2013+ source span covers this year; see sheet 'Maddison original sources'"
        else:
            flags["mpd_source_list"] = "no source span listed by the provider for this country and year"
    if src and src["unparsed"]:
        flags["mpd_unparsed_spans"] = src["unparsed"][:5]
    return flags


def atlas_rows(
    parsed: Mapping[str, Any], *, raw_sha256: str, record: Mapping[str, Any], retrieved_at: datetime
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    base = {
        "mpd_cell_flag": "none_published",
        "release": "MPD 2023",
        "dataverse_version": record.get("version"),
        "licence": RIGHTS["licence"],
    }
    for c in parsed["cells"]:
        code, year, ind = c["code"], c["year"], c["indicator"]
        flags = dict(base)
        if year < SPARSE_BEFORE:
            flags["mpd_sparse_period"] = True
        if ind == "gdppc":
            flags.update(_sources_flags(code, year, parsed))
        q = historical_qid("maddison", code)
        if q is not None:
            flags["entity_mapping"] = {
                "qid": q["qid"],
                "review": q.get("review_status", "agent_mapped"),
                "file": "registry/entities/historical_states_qids.json",
            }
        meta = parsed["countries"].get(code, {})
        flags["provider_name"] = meta.get("name")
        start, end = year_period(year)
        rows.append(
            {
                "source": ATLAS_SOURCE,
                "dataset": ATLAS_DATASET,
                "indicator": ind,
                "concept_id": "history.gdp_per_capita_ppp_2011" if ind == "gdppc" else "history.population_thousands",
                "iso3": None,
                "provider_geo": code,
                "entity_qid": q["qid"] if q else None,
                "period_start": start,
                "period_end": end,
                "value": c["value"],
                "value_text": None,
                "unit": UNITS[ind],
                "flags": canon(flags),
                "raw_sha256": raw_sha256,
                "selector": canon(file=DATA_FILENAME, sheet="Full data", row=c["row"], column=c["col"]),
            }
        )
    for c in parsed["regional"]:
        start, end = year_period(c["year"])
        flags = {
            **base,
            "aggregate": "provider_region" if c["geo"] != "world" else "world",
            "provider_label": c["label"],
        }
        rows.append(
            {
                "source": ATLAS_SOURCE,
                "dataset": ATLAS_DATASET,
                "indicator": c["indicator"],
                "concept_id": None,
                "iso3": None,
                "provider_geo": c["geo"][:64],
                "entity_qid": None,
                "period_start": start,
                "period_end": end,
                "value": c["value"],
                "value_text": None,
                "unit": UNITS[c["indicator"]],
                "flags": canon(flags),
                "raw_sha256": raw_sha256,
                "selector": canon(file=DATA_FILENAME, sheet="Regional data", row=c["row"], column=c["col"] + 1),
            }
        )
    return rows


class MaddisonConnector:
    """Maddison Project Database 2023 (bulk workbook)."""

    def __init__(self, *, min_rows: int = MIN_ROWS) -> None:
        self.min_rows = min_rows  # lowered only by tests on trimmed fixtures

    def describe(self) -> Any:
        return atlas_source()

    def fetch_items(self) -> tuple[FetchItem, ...]:
        return (FetchItem(key="mpd.record", url=RECORD_URL, max_bytes=MAX_RECORD_BYTES),)

    def follow_up(self, raw: RawAcquisition) -> tuple[FetchItem, ...]:
        if raw.item.key != "mpd.record":
            return ()
        rec = parse_record(raw.body)
        return (
            FetchItem(
                key="mpd.xlsx",
                url=FILE_URL.format(id=rec["file_id"]),
                max_bytes=MAX_FILE_BYTES,
                expected_media_types=(
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "application/octet-stream",
                ),
                params={"sha1": rec["checksum"], "file_id": rec["file_id"]},
            ),
        )

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition:
        return await fetch_plain(item, http)

    def build(self, raws: Sequence[RawAcquisition]) -> list[AtlasBuild]:
        by_key = {r.item.key: r for r in raws}
        if set(by_key) != {"mpd.record", "mpd.xlsx"}:
            raise _err(f"expected record + workbook, got {sorted(by_key)}")
        rec_raw, xl_raw = by_key["mpd.record"], by_key["mpd.xlsx"]
        record = parse_record(rec_raw.body)
        if record["checksum_type"] == "SHA-1" and hashlib.sha1(xl_raw.body).hexdigest() != record["checksum"]:  # noqa: S324
            raise _err("workbook SHA-1 differs from the checksum in the dataset record")
        parsed = parse_workbook(xl_raw.body)
        if len(parsed["cells"]) < self.min_rows:
            raise _err(f"only {len(parsed['cells'])} reported cells (< {self.min_rows}): truncated or changed workbook")
        sha = sha256_hex(xl_raw.body)
        rows = atlas_rows(parsed, raw_sha256=sha, record=record, retrieved_at=xl_raw.retrieved_at)
        countries = parsed["countries"]
        unmapped = sorted(c for c in countries if historical_qid("maddison", c) is not None)
        per_century: dict[str, int] = defaultdict(int)
        for c in parsed["cells"]:
            per_century[f"{(c['year'] - 1) // 100 + 1:02d}"] += 1
        report = {
            "record": record,
            "cells": len(parsed["cells"]),
            "regional_cells": len(parsed["regional"]),
            "bad_cells": parsed["bad_cells"][:50],
            "provider_codes": len(countries),
            "historical_or_noncrosswalk_codes": unmapped,
            "cells_per_century": dict(sorted(per_century.items())),
            "retrieved_at": xl_raw.retrieved_at.astimezone(UTC).isoformat(),
        }
        note = (
            f"MPD 2023 (DataverseNL {DOI} v{record['version']}, released {record['release_time']}): "
            f"{len(parsed['cells'])} country cells (gdppc, pop) for {len(countries)} provider codes, years 1-2022, "
            f"+ {len(parsed['regional'])} regional/world cells. No per-cell benchmark/interpolation flag is "
            "published; flags carry the provider's source spans. CSK/SUN/YUG/TWN kept unmapped with QIDs."
        )
        raws_used = [sha, sha256_hex(rec_raw.body)]  # the record carries the licence + checksum evidence
        return [
            AtlasBuild(
                ATLAS_SOURCE,
                ATLAS_DATASET,
                frame(rows),
                raws_used,
                PARSER_VERSION,
                note,
                geo_scheme="iso3",
                rights={**RIGHTS, "doi": DOI},
                report=report,
            )
        ]


def atlas_source() -> Any:
    """The atlas platform declaration (``reva.atlas.sources``)."""
    return source_spec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        datasets=(ATLAS_DATASET,),
        provider="Groningen Growth and Development Centre (Maddison Project)",
        licence=RIGHTS["licence"],
        allowed_hosts=(HOST, "objectstore.surf.nl"),
        dataset_ids=(DOI,),
        field_mappings={"gdppc": "Full data!E", "pop": "Full data!F", "regional": "Regional data"},
        units=dict(UNITS),
        geography=("ISO 3166-1 alpha-3 (provider) + historical CSK/SUN/YUG + TWN + regions",),
        time_semantics="calendar year (provider year column); half-open [Y-01-01, Y+1-01-01)",
        lineage_family="maddison_project",
        cadence_seconds=30 * DAY,
        revision_strategy="new DataverseNL version or file checksum -> new atlas table version",
        planned_requests=2,
        max_response_bytes=MAX_FILE_BYTES,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    out = await ingest_with_run(run, MaddisonConnector(min_rows=1 if run.origin == "test_fixture" else MIN_ROWS))
    return {k: v for k, v in out.items() if not k.startswith("_")}
