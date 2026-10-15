"""Clio-Infra historical indicators -> atlas table ``clio.infra`` (V3, agent A-HISTECON).

Source
------
Clio-Infra (Reconstructing Global Inequality, IISH / Utrecht University; https://clio-infra.eu). The
datasets were deposited in the IISH Dataverse (handles ``hdl:10622/...``) and are now served by
**DataverseNL** (collection ``clioinfra``, one DOI ``doi:10.34894/...`` per indicator). Each dataset
record states its own licence -- **CC0-1.0** for most, **CC-BY-SA-4.0** for a few (Global Hunger,
Infant Mortality, Numeracy, Gender Equality of Numeracy) -- and every atlas row carries the licence of
its dataset. The clio-infra.eu website itself states no licence; its "Compact" files are a derived view
of the same data (the parser was checked against them: all 1,499 Height cells identical), so only the
DataverseNL deposits are used.

Fetch plan (one host, ``dataverse.nl``; file access redirects to ``objectstore.surf.nl``):

1. the search API (collection ``clioinfra``, datasets, 100 per page) -- lists every DOI;
2. one dataset record per DOI (licence, version, files with SHA-1 checksums);
3. every unrestricted ``.xlsx`` data file of the record (SHA-1 checked against the record).

~180 small requests per full run (89 datasets, ~15 MB).

File layout (``Data`` sheet, or ``Sheet1``): title row (indicator name), unit row, header row
``Webmapper code | Webmapper numeric code | ccode | country name | start year | end year | 1500 ... 2015``
(some files to 2050), one row per *polity*:

* ``cshapes/<n>`` rows with a ``ccode`` (ISO 3166 numeric) are the provider's country series; values
  outside the polity's start/end years are the provider's back-cast for the country (flagged
  ``outside_polity_years``). ``iso3`` comes from the ccode through the crosswalk (``m49`` scheme).
* ``cshapes/<n>`` rows without ccode (USSR 1946-1991, Czechoslovakia, Yugoslavia, both Germanies, ...)
  and ``geacron/<n>`` rows (polities at *historical borders*: "France 1820-1879", "Ottoman 1800-1819",
  "Prussia", ...) are kept with ``iso3`` null; agent-mapped QIDs (``historical_states_qids.json``,
  provider ``clio``) are attached only for distinct historical states, never for earlier-border versions
  of a current country.

Indicator = the data file name without ``.xlsx`` (e.g. ``Height-historical``), unit = the file's unit
row verbatim. Empty cells are omitted; zeros are kept as reported (binary indicators such as armed
conflicts or the gold standard report 0 explicitly). No value is computed.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

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

NAME = "atlas.clio_infra"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_clio/1"
SOURCE_KEY = "clio_infra.dataverse"
ATLAS_SOURCE = "clio"
ATLAS_DATASET = "infra"
HOST = "dataverse.nl"
SUBTREE = "clioinfra"
PER_PAGE = 100
SEARCH_URL = f"https://{HOST}/api/search?q=*&subtree={SUBTREE}&type=dataset&per_page={PER_PAGE}&start={{start}}"
RECORD_URL = f"https://{HOST}/api/datasets/:persistentId/?persistentId={{doi}}"
FILE_URL = f"https://{HOST}/api/access/datafile/{{id}}"
MAX_SEARCH_BYTES = 2_000_000
MAX_RECORD_BYTES = 2_000_000
MAX_FILE_BYTES = 10_000_000
ALLOWED_LICENCES = {
    "CC0-1.0": ("CC0 1.0", "https://creativecommons.org/publicdomain/zero/1.0/"),
    "CC-BY-SA-4.0": ("CC BY-SA 4.0", "https://creativecommons.org/licenses/by-sa/4.0/"),
    "CC-BY-4.0": ("CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/"),
}
HEADER = ["Webmapper code", "Webmapper numeric code", "ccode", "country name", "start year", "end year"]
MIN_DATASETS = 60  # 89 datasets at 2026-10-15; far fewer = search truncated
RIGHTS: dict[str, Any] = {
    "licence": "per dataset (CC0 1.0 or CC BY-SA 4.0, stated in each DataverseNL record and on every row)",
    "attribution": "Clio-Infra (clio-infra.eu), IISH / Utrecht University; datasets on DataverseNL (collection clioinfra)",
    "share_alike": "rows from CC BY-SA 4.0 datasets: derived tables must be shared under CC BY-SA 4.0",
}
#: Provider period notes verified on the indicator pages of clio-infra.eu (2026-10-15).
PERIOD_NOTES = {
    "Height-historical": (
        "birth-decade averages: 1810 means born 1810-19; before 1800 sometimes half-centuries (clio-infra.eu Height)"
    ),
}
_DOI = re.compile(r"^doi:10\.34894/[A-Z0-9]{6}$")


class ClioParseError(SourceResponseError):
    pass


def _err(msg: str) -> ClioParseError:
    return ClioParseError(msg, retryable=False, kind="schema_drift")


def _json(body: bytes, what: str) -> Any:
    try:
        return json.loads(body)
    except ValueError as e:
        raise _err(f"{what} is not JSON") from e


# --------------------------------------------------------------------------- search + records
def parse_search(body: bytes) -> dict[str, Any]:
    doc = _json(body, "search response")
    try:
        data = doc["data"]
        items = data["items"]
        total = int(data["total_count"])
        start = int(data.get("start", 0))
    except (KeyError, TypeError, ValueError) as e:
        raise _err("search response is not the expected Dataverse JSON") from e
    datasets = []
    for it in items:
        gid = it.get("global_id")
        if it.get("type") != "dataset" or not isinstance(gid, str) or not _DOI.match(gid):
            raise _err(f"unexpected search item {str(gid)[:60]!r}")
        datasets.append({"doi": gid, "name": it.get("name"), "published_at": it.get("published_at")})
    return {"total": total, "start": start, "datasets": datasets}


def parse_record(body: bytes) -> dict[str, Any]:
    doc = _json(body, "dataset record")
    try:
        lv = doc["data"]["latestVersion"]
    except (KeyError, TypeError) as e:
        raise _err("dataset record is not the expected Dataverse JSON") from e
    lic = lv.get("license")
    lic_name = lic.get("name") if isinstance(lic, dict) else lic
    title = None
    for f in ((lv.get("metadataBlocks") or {}).get("citation") or {}).get("fields") or []:
        if f.get("typeName") == "title":
            title = f.get("value")
    files = []
    for f in lv.get("files") or []:
        df = f.get("dataFile") or {}
        files.append(
            {
                "id": df.get("id"),
                "filename": df.get("filename"),
                "size": df.get("filesize"),
                "restricted": bool(f.get("restricted") or df.get("restricted")),
                "checksum_type": (df.get("checksum") or {}).get("type"),
                "checksum": (df.get("checksum") or {}).get("value"),
            }
        )
    return {
        "doi": lv.get("datasetPersistentId") or doc["data"].get("persistentUrl"),
        "title": title,
        "licence": lic_name,
        "version": f"{lv.get('versionNumber')}.{lv.get('versionMinorNumber')}",
        "release_time": lv.get("releaseTime"),
        "files": files,
    }


def data_files(record: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(files to fetch, skipped files with reasons)."""
    take, skip = [], []
    for f in record["files"]:
        name = f.get("filename") or ""
        if not name.lower().endswith(".xlsx"):
            continue
        if f["restricted"]:
            skip.append({"file": name, "reason": "restricted_on_dataverse"})
        elif not isinstance(f.get("id"), int):
            skip.append({"file": name, "reason": "no_file_id"})
        else:
            take.append(f)
    return take, skip


# --------------------------------------------------------------------------- data files
def _year(text: str | None) -> int | None:
    if text is None:
        return None
    t = text.strip()
    if re.fullmatch(r"-?\d{1,4}(\.0+)?", t):
        return int(float(t))
    return None


def parse_data_file(body: bytes) -> dict[str, Any]:
    """Polity rows of one Clio-Infra data workbook (layout described in the module docstring)."""
    try:
        wb = Workbook(body)
    except XlsxError as e:
        raise _err(f"workbook unreadable: {e}") from e
    sheet = next(iter(wb.sheets))
    rows = list(wb.rows(sheet))
    hdr_idx = next((i for i, (_, c) in enumerate(rows[:8]) if c.get(0) is not None and c[0].text == HEADER[0]), None)
    if hdr_idx is None:
        return {"layout": "unsupported", "sheet": sheet}
    hrow, hcells = rows[hdr_idx]
    head = [hcells[i].text if i in hcells else None for i in range(6)]
    if head != HEADER:
        raise _err(f"header changed: {head}")
    years = {}
    for k, cell in hcells.items():
        if k >= 6:
            y = _year(cell.text)
            if y is None:
                raise _err(f"year header {cell.text!r} in column {k}")
            years[k] = y
    title = unit = None
    pre = [c for _, c in rows[:hdr_idx]]
    if pre and 0 in pre[0]:
        title = pre[0][0].text.strip() or None
    if len(pre) > 1 and 0 in pre[1]:
        unit = pre[1][0].text.strip() or None
    cells: list[dict[str, Any]] = []
    polities: dict[str, dict[str, Any]] = {}
    nonnum: list[dict[str, Any]] = []
    body_rows = rows[hdr_idx + 1 :]

    def g(c: Mapping[int, Any], i: int) -> str | None:
        v = c.get(i)
        return v.text.strip() if v is not None and v.text.strip() != "" else None

    code_count = Counter(g(c, 0) for _, c in body_rows if g(c, 0))
    for rnum, c in body_rows:
        code, num, ccode, name, start, end = (g(c, i) for i in range(6))
        if code and code_count[code] == 1:
            geo = code
        elif code:  # the provider repeats a few polity codes with another end year (e.g. 1919 vs 7305)
            geo = f"{code}|{start}-{end}"
        else:
            geo = f"name:{name}" if name else f"row:{rnum}"
        geo = geo[:64]
        pol = {
            "code": code,
            "num": num,
            "ccode": str(int(float(ccode))) if ccode and _year(ccode) is not None else ccode,
            "name": name,
            "start": _year(start),
            "end": _year(end),
            "start_text": start,
            "end_text": end,
        }
        for k, cell in c.items():
            if k < 6 or cell.kind == "empty" or cell.text.strip() == "":
                continue
            v = decimal_text(cell.text) if cell.kind in ("n", "s") else None
            if v is None:
                nonnum.append({"row": rnum, "column": k, "kind": cell.kind, "text": cell.text[:40]})
                continue
            if geo in polities and polities[geo] != pol:
                raise _err(f"polity {geo} appears on two different rows")
            polities[geo] = pol
            cells.append({"geo": geo, "year": years[k], "value": v, "row": rnum, "col": k})
    return {
        "layout": "clio_polity_wide",
        "sheet": sheet,
        "title": title,
        "unit": unit,
        "cells": cells,
        "polities": polities,
        "non_numeric": nonnum,
        "header_row": hrow,
    }


def atlas_rows(
    indicator: str, parsed: Mapping[str, Any], record: Mapping[str, Any], *, raw_sha256: str, filename: str
) -> list[dict[str, Any]]:
    from reva.atlas.tables import country_iso3

    lic = ALLOWED_LICENCES[record["licence"]]
    base = {
        "dataset_doi": record["doi"],
        "dataset_title": record["title"],
        "dataverse_version": record["version"],
        "licence": lic[0],
        "provider_indicator_title": parsed["title"],
    }
    if indicator in PERIOD_NOTES:
        base["provider_period_note"] = PERIOD_NOTES[indicator]
    unit = (parsed["unit"] or parsed["title"] or "as published")[:100]
    out: list[dict[str, Any]] = []
    for c in parsed["cells"]:
        pol = parsed["polities"][c["geo"]]
        iso3 = country_iso3(pol["ccode"], "m49") if pol["ccode"] else None
        flags = dict(base)
        flags["polity"] = {
            "name": pol["name"],
            "start": pol["start_text"],
            "end": pol["end_text"],
            "ccode": pol["ccode"],
        }
        if c["geo"].startswith("geacron/"):
            flags["territory"] = "historical_borders_geacron_polity"
        elif pol["ccode"]:
            flags["territory"] = "country_series_cshapes_polity_with_iso_numeric_code"
        else:
            flags["territory"] = "historical_cshapes_polity_without_iso_code"
        if pol["start"] is not None and pol["end"] is not None and not pol["start"] <= c["year"] <= pol["end"]:
            flags["outside_polity_years"] = True
        if pol["ccode"] and iso3 is None:
            flags["ccode_not_in_crosswalk"] = pol["ccode"]
        q = None if iso3 else historical_qid("clio", c["geo"])
        if q is not None:
            flags["entity_mapping"] = {
                "qid": q["qid"],
                "review": q.get("review_status", "agent_mapped"),
                "file": "registry/entities/historical_states_qids.json",
            }
        start, end = year_period(c["year"])
        out.append(
            {
                "source": ATLAS_SOURCE,
                "dataset": ATLAS_DATASET,
                "indicator": indicator,
                "concept_id": None,
                "iso3": iso3,
                "provider_geo": c["geo"][:64],
                "entity_qid": q["qid"] if q else None,
                "period_start": start,
                "period_end": end,
                "value": c["value"],
                "value_text": None,
                "unit": unit,
                "flags": canon(flags),
                "raw_sha256": raw_sha256,
                "selector": canon(file=filename, sheet=parsed["sheet"], row=c["row"], column=c["col"] + 1),
            }
        )
    return out


def _stem(filename: str) -> str:
    return re.sub(r"\.xlsx$", "", filename, flags=re.I)[:180]


class ClioConnector:
    """Clio-Infra datasets from DataverseNL (search -> records -> data files)."""

    def __init__(self, *, min_datasets: int = MIN_DATASETS) -> None:
        self.min_datasets = min_datasets  # lowered only by tests on trimmed fixtures

    def describe(self) -> Any:
        return atlas_source()

    def fetch_items(self) -> tuple[FetchItem, ...]:
        return (FetchItem(key="clio.search.0", url=SEARCH_URL.format(start=0), max_bytes=MAX_SEARCH_BYTES),)

    def follow_up(self, raw: RawAcquisition) -> tuple[FetchItem, ...]:
        key = raw.item.key
        if key.startswith("clio.search."):
            s = parse_search(raw.body)
            items = [
                FetchItem(
                    key=f"clio.record.{d['doi']}",
                    url=RECORD_URL.format(doi=quote(d["doi"], safe=":/")),
                    max_bytes=MAX_RECORD_BYTES,
                )
                for d in s["datasets"]
            ]
            nxt = s["start"] + len(s["datasets"])
            if s["datasets"] and nxt < s["total"]:
                items.append(
                    FetchItem(key=f"clio.search.{nxt}", url=SEARCH_URL.format(start=nxt), max_bytes=MAX_SEARCH_BYTES)
                )
            return tuple(items)
        if key.startswith("clio.record."):
            rec = parse_record(raw.body)
            take, _ = data_files(rec)
            return tuple(
                FetchItem(
                    key=f"clio.file.{f['id']}",
                    url=FILE_URL.format(id=f["id"]),
                    max_bytes=MAX_FILE_BYTES,
                    expected_media_types=(
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        "application/octet-stream",
                    ),
                    params={
                        "doi": key.removeprefix("clio.record."),
                        "filename": f["filename"],
                        "sha1": f["checksum"] if f["checksum_type"] == "SHA-1" else None,
                    },
                )
                for f in take
            )
        return ()

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition:
        return await fetch_plain(item, http)

    def build(self, raws: Sequence[RawAcquisition]) -> list[AtlasBuild]:
        searches = [r for r in raws if r.item.key.startswith("clio.search.")]
        records = {r.item.key.removeprefix("clio.record."): r for r in raws if r.item.key.startswith("clio.record.")}
        files = [r for r in raws if r.item.key.startswith("clio.file.")]
        listed = [d for s in searches for d in parse_search(s.body)["datasets"]]
        total = max((parse_search(s.body)["total"] for s in searches), default=0)
        if len(listed) != total:
            raise _err(f"search listed {len(listed)} of {total} datasets")
        if total < self.min_datasets:
            raise _err(f"only {total} datasets listed (< {self.min_datasets})")
        rows: list[dict[str, Any]] = []
        used: list[str] = [sha256_hex(s.body) for s in searches]
        report: dict[str, Any] = {"datasets_listed": total, "datasets": [], "skipped": []}
        recs: dict[str, dict[str, Any]] = {}
        for doi, rr in sorted(records.items()):
            rec = parse_record(rr.body)
            recs[doi] = rec
            used.append(sha256_hex(rr.body))
            _, skipped = data_files(rec)
            for s in skipped:
                report["skipped"].append({"doi": doi, "title": rec["title"], **s})
            if rec["licence"] not in ALLOWED_LICENCES:
                report["skipped"].append({"doi": doi, "title": rec["title"], "reason": f"licence {rec['licence']!r}"})
        indicators: Counter[str] = Counter()
        stems = Counter(_stem(fr.item.params["filename"]) for fr in files)
        for fr in files:
            doi = fr.item.params["doi"]
            fname = fr.item.params["filename"]
            frec = recs.get(doi)
            if frec is None:
                raise _err(f"data file {fname} without its dataset record")
            rec = frec
            if rec["licence"] not in ALLOWED_LICENCES:
                continue  # rights unknown: never published (listed in skipped)
            sha1 = fr.item.params.get("sha1")
            if sha1 and hashlib.sha1(fr.body).hexdigest() != sha1:  # noqa: S324 - provider checksum
                raise _err(f"{fname}: SHA-1 differs from the dataset record")
            parsed = parse_data_file(fr.body)
            if parsed["layout"] != "clio_polity_wide":
                report["skipped"].append(
                    {"doi": doi, "title": rec["title"], "file": fname, "reason": "layout_not_a_clio_data_sheet"}
                )
                continue
            indicator = _stem(fname)
            if stems[indicator] > 1:  # two datasets ship a file of the same name: qualify with the DOI suffix
                indicator = f"{indicator}@{doi.rsplit('/', 1)[-1]}"
            if indicators[indicator]:
                raise _err(f"indicator {indicator} appears twice")
            indicators[indicator] += 1
            sha = sha256_hex(fr.body)
            used.append(sha)
            rows.extend(atlas_rows(indicator, parsed, rec, raw_sha256=sha, filename=fname))
            yrs = [c["year"] for c in parsed["cells"]]
            report["datasets"].append(
                {
                    "doi": doi,
                    "title": rec["title"],
                    "licence": rec["licence"],
                    "indicator": indicator,
                    "unit": parsed["unit"],
                    "cells": len(parsed["cells"]),
                    "polities": len(parsed["polities"]),
                    "years": [min(yrs), max(yrs)] if yrs else None,
                    "non_numeric": parsed["non_numeric"][:5],
                }
            )
        missing_records = sorted({d["doi"] for d in listed} - set(records))
        if missing_records:
            raise _err(f"dataset records not fetched: {missing_records[:5]}")
        note = (
            f"Clio-Infra on DataverseNL (collection {SUBTREE}): {len(report['datasets'])} indicator files from "
            f"{total} datasets; polity rows (CShapes/GEACRON) kept, ISO numeric ccode mapped through the "
            "crosswalk; licence per row (CC0 1.0 or CC BY-SA 4.0)."
        )
        lic_set = sorted({ALLOWED_LICENCES[r["licence"]][0] for r in recs.values() if r["licence"] in ALLOWED_LICENCES})
        return [
            AtlasBuild(
                ATLAS_SOURCE,
                ATLAS_DATASET,
                frame(rows),
                sorted(set(used)),
                PARSER_VERSION,
                note,
                geo_scheme=None,
                rights={**RIGHTS, "licences_present": lic_set},
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
        provider="Clio-Infra (IISH / Utrecht University), DataverseNL collection clioinfra",
        licence="CC0 1.0 / CC BY-SA 4.0 (per dataset record)",
        allowed_hosts=(HOST, "objectstore.surf.nl"),
        dataset_ids=(f"dataverse:{SUBTREE}",),
        field_mappings={"indicator": "data file name", "geo": "Webmapper code", "iso3": "ccode (ISO numeric)"},
        units={"*": "unit row of each data file, verbatim"},
        geography=("CShapes / GEACRON polities (Webmapper codes); ISO 3166 numeric ccode when given",),
        time_semantics="calendar year columns as published (some indicators are decade/birth-cohort values)",
        lineage_family="clio_infra",
        cadence_seconds=90 * DAY,
        revision_strategy="new DataverseNL dataset version or file checksum -> new atlas table version",
        planned_requests=185,
        max_response_bytes=MAX_FILE_BYTES,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    out = await ingest_with_run(run, ClioConnector(min_datasets=1 if run.origin == "test_fixture" else MIN_DATASETS))
    return {k: v for k, v in out.items() if not k.startswith("_")}
