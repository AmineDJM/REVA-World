"""Dichotomous / lexical regime datasets -> atlas tables (V3, agent A-POL).

Two open (CC0) long-run regime classifications, each read from its own Harvard Dataverse release so
that the licence and the file checksum come from the provider's record:

* **LIED** -- Lexical Index of Electoral Democracy v6.9 (Skaaning, Gerring, Bartusevicius;
  doi:10.7910/DVN/WPKNIT, ``LIED_6.9.xlsx``), 1789-2025, all independent countries and most
  semi-sovereign polities and colonies (244 units). Scores describe the status **on 31 December**
  of each year (LIED description); cells are calendar-year cells flagged ``reference_date``.
* **BMR** -- Boix-Miller-Rosato Dichotomous Coding of Democracy v4.0 (doi:10.7910/DVN/FENWWR,
  ``democracy-v4.0.csv``), 1800-2020, sovereign countries (223 units).

Polity5 is deliberately absent: the Center for Systemic Peace prohibits reproduction or
redistribution without written permission (https://www.systemicpeace.org/inscrdata.html, read
2026-10-15), which an atlas served over an API would be.

Geography
---------
* LIED: ``provider_geo`` = ``"<countryn>|vdem=<id>"`` (``|cow=<code>`` without a V-Dem id), two
  verbatim LIED fields (see :func:`lied_geo`; neither the name nor the COW code alone is a key). The
  ``lied`` geo scheme maps a key through LIED's own ``vdem`` id to V-Dem's ``country_text_id`` when
  that is a crosswalk key, else by exact crosswalk name (``registry/atlas/geo_aliases/a_pol.yaml``).
  Units follow LIED/V-Dem continuity (e.g. "Prussia|vdem=77" is Germany's lineage).
* BMR: ``provider_geo`` = ``ccode`` (COW code). The ``bmr`` scheme maps a code through BMR's own
  ``abbreviation`` ("World Bank 3-letter code" per the BMR codebook) when it is a current World Bank
  code of the crosswalk, plus a few listed aliases (old World Bank codes ROM/ZAR/TMP, rows without an
  abbreviation). West Germany (260), the USSR (364), Yugoslavia (345/347), Czechoslovakia (315), the
  GDR, both Vietnams before 1976, South Yemen and the pre-1871 German/Italian states stay unmapped.

Every cell is the provider's integer code as text, with the codebook label in ``value_text`` for
categorical variables. ``NA`` cells are omitted, except BMR ``democracy_omitteddata`` where ``NA``
is meaningful (occupation during an international war or state collapse) and is kept as a missing
cell with that reason.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

import polars as pl

from reva.adapters.connectors.base import to_raw
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import FetchItem, RawAcquisition

CONNECTOR_NAME = "atlas.regimes"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas_regimes/1"
DATAVERSE = "dataverse.harvard.edu"
S3_HOST = "dvn-cloud-iqss.s3.amazonaws.com"  # Dataverse /api/access redirects (303) to signed S3 URLs

LIED_DOI = "doi:10.7910/DVN/WPKNIT"
LIED_FILE_ID = 13640161
LIED_FILENAME = "LIED_6.9.xlsx"
LIED_VERSION = "6.9"
BMR_DOI = "doi:10.7910/DVN/FENWWR"
BMR_FILE_ID = 5707006
BMR_FILENAME = "democracy-v4.0.csv"  # original format of the ingested .tab
BMR_VERSION = "4.0"
EXPECTED_LICENCE = "CC0 1.0"

RIGHTS_LIED: dict[str, Any] = {
    "licence": "CC0-1.0",
    "licence_url": "http://creativecommons.org/publicdomain/zero/1.0",
    "attribution": "Skaaning, Gerring & Bartusevicius, Lexical Index of Electoral Democracy (LIED) v6.9, "
    "Harvard Dataverse doi:10.7910/DVN/WPKNIT",
}
RIGHTS_BMR: dict[str, Any] = {
    "licence": "CC0-1.0",
    "licence_url": "http://creativecommons.org/publicdomain/zero/1.0",
    "attribution": "Boix, Miller & Rosato, Dichotomous Coding of Democracy 1800-2020 v4.0, "
    "Harvard Dataverse doi:10.7910/DVN/FENWWR",
}


class RegimeParseError(SourceResponseError):
    pass


def _err(msg: str, kind: str = "schema_drift") -> RegimeParseError:
    return RegimeParseError(msg, retryable=False, kind=kind)


# ----------------------------------------------------------------------------- fetch plan
def _record_url(doi: str) -> str:
    return f"https://{DATAVERSE}/api/datasets/:persistentId/?persistentId={doi}"


def fetch_items() -> tuple[FetchItem, ...]:
    return (
        FetchItem(key="lied.record", url=_record_url(LIED_DOI), max_bytes=2_000_000),
        FetchItem(
            key="lied.file", url=f"https://{DATAVERSE}/api/access/datafile/{LIED_FILE_ID}", max_bytes=10_000_000,
            expected_media_types=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                  "application/octet-stream"),
        ),
        FetchItem(key="bmr.record", url=_record_url(BMR_DOI), max_bytes=2_000_000),
        FetchItem(
            key="bmr.file", url=f"https://{DATAVERSE}/api/access/datafile/{BMR_FILE_ID}?format=original",
            max_bytes=5_000_000, expected_media_types=("text/csv", "application/octet-stream"),
        ),
    )


async def fetch(item: FetchItem, http: Any) -> RawAcquisition:
    result = await http.get(item.url, max_bytes=item.max_bytes)
    return to_raw(item, result, getattr(http, "origin", "live"))


def connector_spec() -> Any:
    from reva.adapters.connectors import atlas_sci_common as common

    return common.atlas_spec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key="lied.dataverse",
        provider="Harvard Dataverse (LIED: Skaaning et al.; BMR: Boix, Miller, Rosato)",
        licence="CC0-1.0",
        allowed_hosts=(DATAVERSE, S3_HOST),
        dataset_ids=("lied.lied", "bmr.bmr"),
        field_mappings={"lied": "countryn x year", "bmr": "ccode x year"},
        units={"lexical_index": "ordinal 0-6", "democracy": "0/1"},
        geography=("LIED units (244)", "BMR units (223)"),
        time_semantics="country-year; LIED = status on 31 December",
        lineage_family="regimes",
        cadence_seconds=30 * 86400,
        release_aware=True,
        revision_strategy="new atlas table version when the file checksum changes",
        max_response_bytes=10_000_000,
        max_pages=1,
        parser_version=PARSER_VERSION,
        code_digest=common.code_digest(__import__(__name__, fromlist=["_"])),
    )


def bmr_connector_spec() -> Any:
    """BMR is archived under its own source key (``bmr.dataverse``)."""
    spec = connector_spec()
    return spec.model_copy(update={"name": CONNECTOR_NAME + ".bmr", "source_key": "bmr.dataverse",
                                   "dataset_ids": ("bmr.bmr",)})


def lied_connector_spec() -> Any:
    spec = connector_spec()
    return spec.model_copy(update={"name": CONNECTOR_NAME + ".lied", "dataset_ids": ("lied.lied",)})


# ----------------------------------------------------------------------------- dataverse record
def check_record(body: bytes, *, file_id: int, filename: str, payload: bytes) -> dict[str, Any]:
    """Dataverse dataset record: licence must be CC0 and the payload must match the file's MD5."""
    try:
        doc = json.loads(body)
        v = doc["data"]["latestVersion"]
    except (ValueError, KeyError, TypeError) as e:
        raise _err(f"Dataverse record is not the expected JSON ({e})") from e
    lic = (v.get("license") or {}).get("name")
    if lic != EXPECTED_LICENCE:
        raise _err(f"{filename}: dataset licence is {lic!r}, expected {EXPECTED_LICENCE!r}", kind="licence")
    files = {f["dataFile"]["id"]: f["dataFile"] for f in v.get("files", []) if "dataFile" in f}
    df = files.get(file_id)
    if df is None:
        raise _err(f"file {file_id} ({filename}) is no longer in the dataset record", kind="stale_release")
    want = (df.get("checksum") or {}).get("value")
    # Ingested tabular files: the record's MD5 is of the original upload (format=original).
    got = hashlib.md5(payload, usedforsecurity=False).hexdigest()
    if want and want != got:
        raise _err(f"{filename}: MD5 {got} does not match the Dataverse record ({want})", kind="integrity")
    return {
        "doi": doc["data"].get("latestVersion", {}).get("datasetPersistentId") or doc["data"].get("persistentUrl"),
        "version": f"{v.get('versionNumber')}.{v.get('versionMinorNumber')}",
        "release_time": v.get("releaseTime"),
        "licence": lic,
        "file_md5": want,
        "file_label": df.get("filename"),
    }


# ----------------------------------------------------------------------------- labels
_PRESENT = {"0": "Absent", "1": "Present"}
LIED_INDICATORS: tuple[str, ...] = (
    "lexical_index", "lexical_index_plus", "male_suffrage", "female_suffrage", "executive_elections",
    "legislative_elections", "multi-party_legislative_elections", "competitive_elections",
    "political_liberties", "democratic_transition", "transition_type", "democratic_breakdown",
    "breakdown_type", "turnover_period", "turnover_event", "two_turnover_period", "sovereign",
)
#: Labels quoted/abridged from "Description of the LIED 6.8 dataset" (Dataverse file 11068562).
LIED_LABELS: dict[str, dict[str, str]] = {
    "lexical_index": {
        "0": "Non-electoral autocracy", "1": "One-party autocracy", "2": "Multiparty autocracy without elected executive",
        "3": "Multiparty autocracy", "4": "Exclusive democracy", "5": "Male democracy", "6": "Electoral democracy"},
    "lexical_index_plus": {
        "0": "Non-electoral autocracy", "1": "One-party autocracy", "2": "Multiparty autocracy without elected executive",
        "3": "Multiparty autocracy", "4": "Exclusive democracy", "5": "Male democracy", "6": "Electoral democracy",
        "7": "Polyarchy"},
    "transition_type": {"0": "No democratic transition", "1": "Conversion (incumbent-led)",
                        "2": "Cooperative (pact)", "3": "Collapse (opposition-led)", "4": "Foreign supervision",
                        "5": "Foreign liberalization"},
    "breakdown_type": {"0": "No democratic breakdown", "1": "Implicit regression induced by incumbents",
                       "2": "Military coup", "3": "Foreign occupation", "4": "Self-coup",
                       "5": "Coup or civil conflict headed by opposition", "6": "Coup headed by monarch"},
    "sovereign": {"0": "Not sovereign (colonised or occupied)", "1": "Sovereign"},
    **{k: _PRESENT for k in ("male_suffrage", "female_suffrage", "executive_elections", "legislative_elections",
                              "multi-party_legislative_elections", "competitive_elections", "political_liberties",
                              "democratic_transition", "democratic_breakdown", "turnover_period", "turnover_event",
                              "two_turnover_period")},
}
LIED_UNITS: dict[str, str] = {"lexical_index": "ordinal 0-6", "lexical_index_plus": "ordinal 0-7",
                              "transition_type": "category code", "breakdown_type": "category code"}

BMR_INDICATORS: tuple[str, ...] = (
    "democracy", "democracy_trans", "democracy_breakdowns", "democracy_duration", "democracy_omitteddata",
    "democracy_femalesuffrage",
)
#: Labels from the BMR v4.0 codebook (Dataverse file 5707009).
BMR_LABELS: dict[str, dict[str, str]] = {
    "democracy": {"0": "Not a democracy", "1": "Democracy"},
    "democracy_omitteddata": {"0": "Not a democracy", "1": "Democracy"},
    "democracy_femalesuffrage": {"0": "Not a democracy (female suffrage required)",
                                 "1": "Democracy (female suffrage required)"},
    "democracy_trans": {"-1": "Democratic breakdown", "0": "No change", "1": "Transition to democracy"},
}
BMR_UNITS: dict[str, str] = {"democracy": "0/1", "democracy_omitteddata": "0/1", "democracy_femalesuffrage": "0/1",
                             "democracy_trans": "category code", "democracy_breakdowns": "count",
                             "democracy_duration": "years"}
BMR_OMITTED_NOTE = (
    "NA in democracy_omitteddata: country occupied during an international war or in state collapse during a "
    "civil war (BMR v4.0 codebook)"
)


# ----------------------------------------------------------------------------- parsing
def _int_text(v: Any, what: str) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "NA", ".", "NaN"):
        return None
    try:
        f = float(s)
    except ValueError as e:
        raise _err(f"{what}: {s!r} is not a number") from e
    if f != int(f):
        raise _err(f"{what}: {s!r} is not an integer code")
    return str(int(f))


def parse_lied(body: bytes) -> list[dict[str, Any]]:
    """``LIED_6.9.xlsx`` -> rows (dicts of exact cell text); checks the header."""
    from reva.adapters.connectors.atlas_xlsx import Workbook, XlsxError

    try:
        wb = Workbook(body)
        sheets = list(wb.sheets)
        if not sheets:
            raise _err("LIED workbook has no sheet")
        table = wb.table(sheets[0])
    except XlsxError as e:
        raise _err(f"LIED workbook: {e}") from e
    header = [str(h).strip() if h is not None else "" for h in table[0]]
    need = {"countryn", "cow", "vdem", "year", *LIED_INDICATORS}
    miss = sorted(need - set(header))
    if miss:
        raise _err(f"LIED header lacks {miss}")
    rows = []
    for n, r in enumerate(table[1:], start=2):
        d = dict(zip(header, r, strict=False))
        if not d.get("countryn"):
            continue
        d["_row"] = n
        rows.append(d)
    if len(rows) < 20_000:
        raise _err(f"LIED has only {len(rows)} rows; refusing a partial file")
    return rows


def parse_bmr(body: bytes) -> list[dict[str, Any]]:
    text = body.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    need = {"country", "ccode", "abbreviation", "year", *BMR_INDICATORS}
    miss = sorted(need - set(reader.fieldnames or []))
    if miss:
        raise _err(f"BMR header lacks {miss}")
    rows = []
    for n, r in enumerate(reader, start=2):
        r = dict(r)
        r["_row"] = n
        rows.append(r)
    if len(rows) < 15_000:
        raise _err(f"BMR has only {len(rows)} rows; refusing a partial file")
    return rows


def _canon(obj: Mapping[str, Any]) -> str:
    return json.dumps(dict(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _frame(records: list[dict[str, Any]]) -> pl.DataFrame:
    from reva.atlas.tables import ATLAS_SCHEMA

    return pl.DataFrame({c: [r[c] for r in records] for c in ATLAS_SCHEMA.names()}, schema=ATLAS_SCHEMA)


def lied_geo(r: Mapping[str, Any]) -> str:
    """LIED unit key: ``"<countryn>|vdem=<id>"`` (``|cow=<code>`` when LIED gives no V-Dem id).

    Both parts are verbatim LIED fields. The name alone is ambiguous across V-Dem lineages (LIED
    "Vietnam" carries V-Dem id 35 up to 1953 and 34 from 1976; "Vietnam, North" carries 34), and the
    COW code alone too (325 = "Sardinia" and "Italy").
    """
    name = str(r["countryn"]).strip()
    vdem = _int_text(r.get("vdem"), "vdem")
    if vdem is not None:
        return f"{name}|vdem={vdem}"
    cow = _int_text(r.get("cow"), "cow")
    return f"{name}|cow={cow}" if cow is not None else name


def lied_frame(rows: Sequence[Mapping[str, Any]], *, raw_sha256: str, qids: Mapping[str, str] | None = None) -> pl.DataFrame:
    """LIED rows -> atlas frame (``iso3`` filled later by ``geo_scheme="lied"``)."""
    qids = dict(qids or {})
    out: list[dict[str, Any]] = []
    for r in rows:
        year = _int_text(r.get("year"), "LIED year")
        if year is None:
            raise _err(f"LIED row {r['_row']} has no year")
        y = int(year)
        geo = lied_geo(r)
        flags = _canon({
            "lied_version": LIED_VERSION, "reference_date": f"{y}-12-31",
            "cow": _int_text(r.get("cow"), "cow"), "vdem_id": _int_text(r.get("vdem"), "vdem"),
        })
        for ind in LIED_INDICATORS:
            v = _int_text(r.get(ind), f"LIED {ind} row {r['_row']}")
            if v is None:
                continue
            labels = LIED_LABELS.get(ind)
            label = labels.get(v) if labels else None
            if labels and label is None:
                raise _err(f"LIED {ind}: code {v} has no label (row {r['_row']})")
            out.append({
                "source": "lied", "dataset": "lied", "indicator": ind, "concept_id": None, "iso3": None,
                "provider_geo": geo, "entity_qid": qids.get(geo), "period_start": date(y, 1, 1),
                "period_end": date(y + 1, 1, 1), "value": v, "value_text": label,
                "unit": LIED_UNITS.get(ind, "0/1"), "flags": flags, "raw_sha256": raw_sha256,
                "selector": _canon({"file": LIED_FILENAME, "sheet": "Sheet1", "row": r["_row"], "column": ind}),
            })
    return _frame(out)


def bmr_frame(rows: Sequence[Mapping[str, Any]], *, raw_sha256: str, qids: Mapping[str, str] | None = None) -> pl.DataFrame:
    """BMR rows -> atlas frame (``iso3`` filled later by ``geo_scheme="bmr"``)."""
    qids = dict(qids or {})
    out: list[dict[str, Any]] = []
    for r in rows:
        y = int(_int_text(r.get("year"), "BMR year") or 0)
        geo = _int_text(r.get("ccode"), "BMR ccode")
        if not y or geo is None:
            raise _err(f"BMR row {r['_row']} lacks year or ccode")
        flags = {"bmr_version": BMR_VERSION, "bmr_country": r["country"], "bmr_abbreviation": r["abbreviation"]}
        democracy = _int_text(r.get("democracy"), "democracy")
        for ind in BMR_INDICATORS:
            v = _int_text(r.get(ind), f"BMR {ind} row {r['_row']}")
            cell_flags = flags
            label = None
            if v is None:
                if ind == "democracy_omitteddata" and democracy is not None:
                    cell_flags = {**flags, "missing": "not_applicable", "provider_note": BMR_OMITTED_NOTE}
                else:
                    continue
            else:
                labels = BMR_LABELS.get(ind)
                label = labels.get(v) if labels else None
                if labels and label is None:
                    raise _err(f"BMR {ind}: code {v} has no label (row {r['_row']})")
            out.append({
                "source": "bmr", "dataset": "bmr", "indicator": ind, "concept_id": None, "iso3": None,
                "provider_geo": geo, "entity_qid": qids.get(geo), "period_start": date(y, 1, 1),
                "period_end": date(y + 1, 1, 1), "value": v, "value_text": label,
                "unit": BMR_UNITS[ind], "flags": _canon(cell_flags), "raw_sha256": raw_sha256,
                "selector": _canon({"file": BMR_FILENAME, "row": r["_row"], "column": ind}),
            })
    return _frame(out)


# ----------------------------------------------------------------------------- QIDs
def unit_qids(dataset: str) -> dict[str, str]:
    """provider_geo -> QID for LIED/BMR units listed in ``registry/entities/vdem_qids.json``."""
    from reva.adapters.connectors.atlas_vdem import QIDS_PATH

    if not QIDS_PATH.exists():
        return {}
    doc = json.loads(QIDS_PATH.read_text())
    key = {"lied": "lied_countryn", "bmr": "bmr_ccode"}[dataset]
    out: dict[str, str] = {}
    for u in doc.get("units", []) + doc.get("other_units", []):
        for code in u.get(key) or []:
            if u.get("qid"):
                out[str(code)] = u["qid"]
    return out


# ----------------------------------------------------------------------------- ingest
def ingest(engine: Any, store: Any, raws: Mapping[str, RawAcquisition], *, origin: str,
           actor: str = "worker:atlas_regimes") -> dict[str, Any]:
    """Archive the four raw files and register ``lied.lied`` and ``bmr.bmr``."""
    from reva.adapters.connectors import atlas_sci_common as common
    from reva.atlas.tables import register_atlas_table

    lied_meta = check_record(raws["lied.record"].body, file_id=LIED_FILE_ID, filename=LIED_FILENAME,
                             payload=raws["lied.file"].body)
    bmr_meta = check_record(raws["bmr.record"].body, file_id=BMR_FILE_ID, filename=BMR_FILENAME,
                            payload=raws["bmr.file"].body)
    lspec, bspec = lied_connector_spec(), bmr_connector_spec()
    shas = {k: common.archive_raw(engine, store, lspec if k.startswith("lied") else bspec, r, actor=actor)
            for k, r in raws.items()}
    out: dict[str, Any] = {}
    lf = lied_frame(parse_lied(raws["lied.file"].body), raw_sha256=shas["lied.file"], qids=unit_qids("lied"))
    out["lied"] = register_atlas_table(
        engine, store, source="lied", dataset="lied", frame=lf, raw_sha256s=[shas["lied.file"]],
        parser_version=PARSER_VERSION, origin=origin, geo_scheme="lied",
        coverage_note=(f"LIED v{LIED_VERSION} ({LIED_FILENAME}), 1789-2025, status on 31 December; units keyed by "
                       "LIED name; historical polities and colonies without a crosswalk entity kept unmapped. CC0."),
        rights={**RIGHTS_LIED, "record": lied_meta, "record_raw_sha256": shas["lied.record"]}, actor=actor,
    )
    bf = bmr_frame(parse_bmr(raws["bmr.file"].body), raw_sha256=shas["bmr.file"], qids=unit_qids("bmr"))
    out["bmr"] = register_atlas_table(
        engine, store, source="bmr", dataset="bmr", frame=bf, raw_sha256s=[shas["bmr.file"]],
        parser_version=PARSER_VERSION, origin=origin, geo_scheme="bmr",
        coverage_note=(f"BMR v{BMR_VERSION} ({BMR_FILENAME}), 1800-2020, sovereign states keyed by COW code; "
                       "West Germany, USSR, Yugoslavia, Czechoslovakia and historical states kept unmapped. CC0."),
        rights={**RIGHTS_BMR, "record": bmr_meta, "record_raw_sha256": shas["bmr.record"]}, actor=actor,
    )
    return out


__all__ = [
    "BMR_INDICATORS",
    "LIED_INDICATORS",
    "bmr_frame",
    "check_record",
    "fetch",
    "fetch_items",
    "ingest",
    "lied_frame",
    "parse_bmr",
    "parse_lied",
]
