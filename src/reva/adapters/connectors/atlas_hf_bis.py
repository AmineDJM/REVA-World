"""BIS daily series (bulk flat files) -> atlas tables (V5, agent PRED-HF).

A-FISCAL's :mod:`atlas_bis` takes the **monthly** central bank policy rates and effective exchange rates through
the SDMX API (``bis.cbpol``, ``bis.eer``); the daily series of the same dataflows are only practical through the
BIS Data Portal bulk files (``https://data.bis.org/static/bulk/<FLOW>_csv_flat.zip``: 4.1 MB / 6.6 MB zipped,
476 MB / 296 MB inflated, read as a stream). This connector keeps only ``FREQ = D`` observations:

===================  =================================================================================  ========
dataset              content                                                                            rows
===================  =================================================================================  ========
``cbpol_d``          WS_CBPOL central bank policy rates, daily, end of period, % per year (49 areas)       ~640 k
``cbpol_d_series``   the series-level texts of those daily series (``COMPILATION`` = which rate the BIS     ~150
                     uses in which period, ``SOURCE_REF``, ``SUPP_INFO_BREAKS``, ``TITLE``) as ``value_text``
                     cells spanning the series' first..last observation (kept out of the 640 k data rows)
``eer_d``            WS_EER nominal effective exchange rates, daily, broad (64 economies) and narrow (27)   ~800 k
                     baskets, index 2020 = 100 (the real EERs are monthly only)
===================  =================================================================================  ========

Rules: observation status ``A: Normal value`` -> a row; ``M: Missing value; data cannot exist`` and ``H: Missing
value; holiday or weekend`` -> **no row even when the file carries a number** (the BIS fills holiday/weekend EER
cells; REVA never turns a flagged-missing cell into an observation; counts in the report); any other status is
kept verbatim in ``flags.obs_status``. Series-level attribute rows of the flat file (empty ``TIME_PERIOD``, partial
keys such as ``N.B.XM`` without ``FREQ``) supply attributes (``UNIT_MEASURE`` of the EER) to every series they
match; they must precede the observations (fail closed otherwise). ``UNIT_MULT`` kept in flags, never applied.
Geography: ``REF_AREA`` ISO 3166 alpha-2 -> ``iso3`` through the crosswalk (scheme ``iso2``); BIS aggregates
(``XM`` euro area …) stay unmapped.

Publication / revisions: the bulk files are replaced at each BIS release (``Last-Modified`` of the archived
response); the BIS states no per-observation publication date or revision policy for these series, so
``flags.as_of`` (fetch date) is the earliest availability REVA can prove. Terms: BIS terms of permitted use
(unrestricted use with citation, quoted and checked against the archived ``data.bis.org/help/legal`` page).
"""

from __future__ import annotations

import csv
import io
import sys
import zipfile
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from reva.adapters.connectors import hf_daily_common as hc
from reva.atlas.sources import AtlasSourceSpec

NAME = "atlas.bis_daily"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_hf_bis/1"
SOURCE_KEY = "bis.bulk"
ATLAS_SOURCE = "bis"
DATASETS = ("cbpol_d", "cbpol_d_series", "eer_d")
HOST = "data.bis.org"
GEO_SCHEME = "iso2"
BULK = f"https://{HOST}/static/bulk"
LEGAL_URL = f"https://{HOST}/help/legal"
ZIP_MEDIA = ("application/zip", "application/x-zip-compressed", "application/octet-stream")
MAX_MEMBER = 1_500_000_000

FILES: tuple[hc.FileSpec, ...] = (
    hc.FileSpec("cbpol_zip", f"{BULK}/WS_CBPOL_csv_flat.zip", ("cbpol_d", "cbpol_d_series"), ZIP_MEDIA, 60_000_000),
    hc.FileSpec("eer_zip", f"{BULK}/WS_EER_csv_flat.zip", ("eer_d",), ZIP_MEDIA, 80_000_000),
    hc.FileSpec("legal", LEGAL_URL, DATASETS, ("text/html",), 3_000_000, role="policy"),
)

LICENCE = (
    "BIS terms of permitted use of BIS statistics: use unrestricted provided the BIS is cited as the source,"
    " translations marked unofficial, no misleading use, no extra charge in commercial products"
)
_TERMS = (
    "The use of the statistics is unrestricted, provided that: if the statistics are reproduced, the BIS must be cited "
    "in your publication or product as the source of the statistics;"
)
_BASE: dict[str, Any] = {
    "licence": LICENCE,
    "licence_url": LEGAL_URL + "#terms-of-permitted-use-of-bis-statistics",
    "terms_quote": _TERMS,
    "attribution": "Source: Bank for International Settlements (BIS) statistics",
    "licence_checked": "2026-10-17",
    "provider": "Bank for International Settlements",
    "publication": "BIS bulk file replaced at each release; per-observation publication date not stated",
    "pub_lag": "not_stated",
    "revision_policy": "not stated by provider; bulk files replaced at each release; value as fetched (as_of)",
    "policy_quotes": {
        "terms": ("legal", _TERMS),
        "no_other_use": ("legal", "No other use is permissible."),
        "national_sources": ("legal", "Most of the data sets rely on national statistics as communicated to the BIS "
                             "by central banks and other national authorities and are compiled in cooperation with "
                             "them."),
    },
}  # fmt: skip
RIGHTS: dict[str, dict[str, Any]] = {ds: dict(_BASE) for ds in DATASETS}


@dataclass(frozen=True)
class Flow:
    dataset: str
    file: str
    member: str
    dims: tuple[str, ...]  # dimension column codes, FREQ first
    fold: tuple[str, ...]  # dimensions folded into the indicator (besides FREQ and REF_AREA)
    prefix: str
    meta_attrs: tuple[str, ...] = ()  # series texts written to <dataset>_series (cbpol) instead of flags
    flag_attrs: tuple[str, ...] = ()  # short series attributes kept in flags


FLOWS = (
    Flow("cbpol_d", "cbpol_zip", "WS_CBPOL_csv_flat.csv", ("FREQ", "REF_AREA"), (), "CBPOL",
         meta_attrs=("COMPILATION", "SOURCE_REF", "SUPP_INFO_BREAKS", "TITLE")),
    Flow("eer_d", "eer_zip", "WS_EER_csv_flat.csv", ("FREQ", "EER_TYPE", "EER_BASKET", "REF_AREA"),
         ("EER_TYPE", "EER_BASKET"), "EER", flag_attrs=("TITLE_TS", "COLLECTION")),
)  # fmt: skip
STATUS_NORMAL = "A"
STATUS_NO_ROW = {"M", "H"}


def code_digest() -> str:
    return hc.code_digest(sys.modules[__name__])


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="Bank for International Settlements (BIS Data Portal bulk downloads)",
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=DATASETS,
        dataset_ids=("BIS:WS_CBPOL(daily)", "BIS:WS_CBPOL(daily series attributes)", "BIS:WS_EER(daily)"),
        cadence_seconds=7 * hc.DAY,
        planned_requests=len(FILES),
        max_response_bytes=80_000_000,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"geo_scheme": GEO_SCHEME, "tables": [f"bis.{d}" for d in DATASETS]},
        notes="two flat bulk zips (~11 MB) + terms page per full run; daily observations only",
    )


def _code(cell: str) -> str:
    """``"D: Daily"`` -> ``"D"``; ``""`` -> ``""`` (a wildcard in a series-level attribute row)."""
    return cell.split(":", 1)[0].strip()


def _label(cell: str) -> str:
    return cell.split(":", 1)[1].strip() if ":" in cell else cell.strip()


def parse_flow(flow: Flow, f: hc.Fetched, out: Mapping[str, hc.TableBuild], report: dict[str, Any]) -> None:
    try:
        z = zipfile.ZipFile(io.BytesIO(f.body))
    except zipfile.BadZipFile as e:
        raise hc.err(f"{flow.dataset}: not a zip ({e})") from e
    if z.namelist() != [flow.member]:
        raise hc.err(f"{flow.dataset}: unexpected members {z.namelist()[:5]}")
    if z.getinfo(flow.member).file_size > MAX_MEMBER:
        raise hc.err(f"{flow.dataset}: member too large", kind="too_large")
    with z.open(flow.member) as raw:
        reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
        header = next(reader, None) or []
        codes = [h.split(":", 1)[0] for h in header]
        col = {c: i for i, c in enumerate(codes)}
        need = {*flow.dims, "TIME_PERIOD", "OBS_VALUE", "OBS_STATUS", "UNIT_MEASURE", *flow.meta_attrs, *flow.flag_attrs}
        if not need <= set(col):
            raise hc.err(f"{flow.dataset}: columns missing {sorted(need - set(col))}")
        attr_cols = [c for c in codes if c not in (*flow.dims, "STRUCTURE", "STRUCTURE_ID", "ACTION", "TIME_PERIOD",
                                                   "OBS_VALUE", "OBS_STATUS", "OBS_CONF", "OBS_PRE_BREAK")]  # fmt: skip
        groups: list[tuple[tuple[str, ...], dict[str, str]]] = []
        series_attrs: dict[tuple[str, ...], dict[str, str]] = {}
        spans: dict[tuple[str, ...], list[Any]] = {}
        rows = out[flow.dataset].rows
        rights = RIGHTS[flow.dataset]
        dropped: Counter[str] = Counter()
        kept_status: Counter[str] = Counter()
        seen_obs = False
        for line, rec in enumerate(reader, start=2):
            if len(rec) != len(header):
                raise hc.err(f"{flow.dataset}: line {line} has {len(rec)} fields, header {len(header)}")
            dims = tuple(_code(rec[col[d]]) for d in flow.dims)
            tp = rec[col["TIME_PERIOD"]].strip()
            if not tp:  # series-level (or partial-key group) attribute row
                if seen_obs:
                    raise hc.err(f"{flow.dataset}: attribute row after observations (line {line})")
                groups.append((dims, {a: rec[col[a]].strip() for a in attr_cols if rec[col[a]].strip()}))
                continue
            seen_obs = True
            if dims[0] != "D":
                continue
            status = _code(rec[col["OBS_STATUS"]])
            if status in STATUS_NO_ROW:
                dropped[f"status_{status}"] += 1
                continue
            v, normalised = hc.number(rec[col["OBS_VALUE"]])
            if v is None:
                dropped["not_a_number"] += 1
                continue
            try:
                d = date.fromisoformat(tp)
            except ValueError as e:
                raise hc.err(f"{flow.dataset}: line {line}: bad TIME_PERIOD {tp!r}") from e
            attrs = series_attrs.get(dims)
            if attrs is None:
                attrs = {}
                for gdims, gattrs in groups:
                    if all(g in ("", s) for g, s in zip(gdims, dims, strict=True)):
                        attrs.update(gattrs)
                series_attrs[dims] = attrs
            own = {a: rec[col[a]].strip() for a in attr_cols if rec[col[a]].strip()}
            merged = {**attrs, **own}
            unit = merged.get("UNIT_MEASURE")
            if not unit:
                raise hc.err(f"{flow.dataset}: series {'.'.join(dims)} has no UNIT_MEASURE", kind="unit")
            area = dims[flow.dims.index("REF_AREA")]
            ind = flow.prefix + "".join(f"|{d.lower()}={dims[flow.dims.index(d)]}" for d in flow.fold)
            extra: dict[str, Any] = {"frequency": "D", "series_key": ".".join(dims)}
            if "UNIT_MULT" in merged:
                extra["unit_mult"] = merged["UNIT_MULT"]
            for a in flow.flag_attrs:
                if merged.get(a):
                    extra[a.lower()] = _label(merged[a]) if a == "COLLECTION" else merged[a].strip()
            if flow.meta_attrs:
                extra["series_texts"] = f"bis.{flow.dataset}_series"
            if status != STATUS_NORMAL:
                extra["obs_status"] = rec[col["OBS_STATUS"]].strip()
                kept_status[status] += 1
            conf = _code(rec[col["OBS_CONF"]]) if "OBS_CONF" in col else ""
            if conf and conf != "F":
                extra["confidentiality"] = rec[col["OBS_CONF"]].strip()
            if "OBS_PRE_BREAK" in col and rec[col["OBS_PRE_BREAK"]].strip():
                extra["pre_break_value"] = rec[col["OBS_PRE_BREAK"]].strip()
            if normalised:
                extra["value_normalised"] = True
            rows.add(indicator=ind, provider_geo=area, day=d, value=v, unit=_label(unit),
                     flags=rows.flags(**hc.base_flags(rights, f, **extra)), raw_sha256=f.sha256,
                     selector={"file": flow.member, "record": line})  # fmt: skip
            sp = spans.get(dims)
            if sp is None:
                spans[dims] = [d, d, line, merged]
            else:
                sp[0], sp[1] = min(sp[0], d), max(sp[1], d)
    if flow.meta_attrs:
        meta = out.get(f"{flow.dataset}_series")
        if meta is not None:
            mflags = meta.rows.flags(**hc.base_flags(RIGHTS[f"{flow.dataset}_series"], f, frequency="D",
                                                     series_texts_of=f"bis.{flow.dataset}"))  # fmt: skip
            for dims, (first, last, line, merged) in sorted(spans.items()):
                area = dims[flow.dims.index("REF_AREA")]
                for a in flow.meta_attrs:
                    text = (merged.get(a) or "").strip()
                    if text:
                        meta.rows.add_text(indicator=f"{flow.prefix}_SERIES|attr={a}", provider_geo=area,
                                           span=(first, hc.day_span(last)[1]), text=text[: hc.MAX_JSON_CHARS],
                                           unit="text", flags=mflags, raw_sha256=f.sha256,
                                           selector={"file": flow.member, "record": line, "column": a})  # fmt: skip
    report[flow.dataset] = {"series": len(spans), "dropped": dict(dropped), "kept_non_normal_status": dict(kept_status),
                            "attribute_rows": len(groups)}  # fmt: skip


def build(files: Mapping[str, hc.Fetched], wanted: tuple[str, ...] = DATASETS) -> tuple[dict[str, hc.TableBuild], dict]:
    out = {ds: hc.TableBuild(hc.DailyRows(ATLAS_SOURCE, ds)) for ds in wanted}
    report: dict[str, Any] = {}
    for flow in FLOWS:
        if flow.dataset in wanted or f"{flow.dataset}_series" in wanted:
            tmp = dict(out)
            if flow.dataset not in tmp:  # only the series texts were asked for
                tmp[flow.dataset] = hc.TableBuild(hc.DailyRows(ATLAS_SOURCE, flow.dataset))
            parse_flow(flow, files[flow.file], tmp, report)
    report["policy_quotes_found"] = hc.check_policy_pages(files, FILES, RIGHTS, wanted)
    return out, report


NOTES = {
    "cbpol_d": "BIS central bank policy rates, daily end-of-period series (FREQ D) of WS_CBPOL from the bulk flat "
    "file; status M (data cannot exist) and NaN cells are no row. Which rate the BIS uses in which period: table "
    "bis.cbpol_d_series (COMPILATION). Monthly series: A-FISCAL's bis.cbpol. flags.as_of = fetch date.",
    "cbpol_d_series": "Series-level texts (COMPILATION, SOURCE_REF, SUPP_INFO_BREAKS, TITLE) of the daily WS_CBPOL "
    "series as value_text cells spanning each series' first..last daily observation in this vintage.",
    "eer_d": "BIS nominal effective exchange rates, daily (FREQ D), broad and narrow baskets, index 2020 = 100. "
    "Status H (holiday or weekend) cells are no row although the BIS file carries a value for them. Real EERs are "
    "monthly only (A-FISCAL's bis.eer).",
}


async def atlas_ingest(run: Any) -> dict[str, Any]:
    wanted = tuple(d for d in DATASETS if run.wants(d))
    files = await hc.fetch_all(run, FILES, wanted)
    builds, report = build(files, wanted)
    tables = hc.register_tables(run, builds, rights=RIGHTS, notes=NOTES, source_key=SOURCE_KEY, geo_scheme=GEO_SCHEME)
    return {"ok": True, "tables": tables, "report": report}


__all__ = ["DATASETS", "FILES", "FLOWS", "NAME", "RIGHTS", "atlas_ingest", "atlas_source", "build"]
