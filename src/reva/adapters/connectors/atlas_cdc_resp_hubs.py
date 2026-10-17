"""US CDC respiratory forecast hubs (FluSight, COVID-19 Forecast Hub): NHSN target data, its weekly vintages and the
hubs' ensemble forecasts -> atlas tables ``cdc.*`` (V6, agent PRED-HEALTH).

Why: the hubs' ensembles are the strongest public, prospective, *attributed* benchmarks for US weekly
hospital-admission forecasts, and their target data are published with dated vintages, so REVA's baselines can be
back-tested **as of** each forecast round (the data as published then, not the revised values) and compared to the
ensemble with the hubs' own metric (WIS). REVA never presents an ensemble forecast as its own.

Sources (public AWS S3 mirrors of the hubverse GitHub repositories; anonymous ``ListObjectsV2`` + GET, no account):

* FluSight (``cdcepi/FluSight-forecast-hub``, bucket ``cdcepi-flusight-forecast-hub``):
  ``target-data/target-hospital-admissions.csv`` (current NHSN weekly confirmed influenza admissions by
  jurisdiction), ``auxiliary-data/target-data-archive/target-hospital-admissions_<date>.csv`` (the hub's weekly
  archive of that file; ``<date>`` = the latest epiweek it contains, per the hub's ``archive_target_data.R``),
  ``model-output/FluSight-ensemble/<reference_date>-FluSight-ensemble.parquet``.
  Licence: repository MIT (code); README: "Anyone interested in using these data for additional research or
  publications is requested to contact flusight@cdc.gov for information regarding attribution of the source
  forecasts." -> flagged ``attribution_request`` (owner decision before any publication).
* COVID-19 Forecast Hub (``CDCgov/covid19-forecast-hub``, bucket ``covid19-forecast-hub``):
  ``target-data/covid-hospital-admissions.csv`` (current), ``target-data/time-series.parquet`` (hubverse time
  series with ``as_of`` = each weekly data vintage), ``model-output/CovidHub-ensemble/<ref>-CovidHub-ensemble.parquet``.
  Licence: README "Public Domain Standard Notice": US Government work, CC0 1.0 worldwide; same attribution request
  (covidhub@cdc.gov).

Underlying data: NHSN Hospital Respiratory Data (HRD), weekly (epiweek Sunday-Saturday) new admissions of patients
with laboratory-confirmed influenza / COVID-19, as compiled by each hub (``totalconfflunewadm`` /
``totalconfc19newadm``).

Atlas tables (all counts; ``provider_geo`` = the hub ``location`` code verbatim: 2-digit state FIPS or ``US``;
geo scheme ``cdc_hub`` maps only ``US`` -> USA; states stay unmapped, no ADM1 code is invented):

* ``cdc.flusight_target`` / ``cdc.covidhub_target`` — indicator ``nhsn.wk_inc_flu_hosp`` / ``nhsn.wk_inc_covid_hosp``,
  period = the epiweek ``[target_end_date - 6 d, target_end_date + 1 d)``. These are the resolution tables.
* ``cdc.flusight_vintages`` / ``cdc.covidhub_vintages`` — indicator ``…|vintage=<date>`` (FluSight: archive file date
  = last epiweek in the file; COVID: the hub's ``as_of``); each vintage keeps the epiweeks of the
  :data:`VINTAGE_WINDOW_WEEKS` weeks up to its last epiweek, the earliest vintage keeps its full history.
* ``cdc.flusight_ensemble`` / ``cdc.covidhub_ensemble`` — indicator ``<model_id>.<target>|ref=<reference_date>|q=<level>``,
  quantile forecasts of horizons 0..3 (horizon -1 is not scored by the hubs and not kept), flags
  ``estimate_kind = projection``, ``producer`` = the hub, ``model_id`` verbatim. Projections are never observations.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from xml.etree import ElementTree

from reva.adapters.connectors.atlas_healthedu_common import canon, fetch_archive_register, fit_flags
from reva.adapters.connectors.base import dec_str, module_digest
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError, ValidationProblem
from reva.ports.connectors import FetchItem

CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas-cdc-resp-hubs-parse-1"
SOURCE_KEY = "cdc.resp_hubs"
VINTAGE_WINDOW_WEEKS = 52
FIRST_REFERENCE_DATE = date(2024, 11, 1)  # back-test seasons 2024-25 onwards (budget: <= 100 MB raw per run)
MAX_ITEM_BYTES = 12_000_000
HORIZONS = (0, 1, 2, 3)
S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
CSV_TYPES = ("text/csv", "application/octet-stream", "binary/octet-stream", "text/plain", "application/vnd.ms-excel")
PARQUET_TYPES = (
    "application/octet-stream",
    "binary/octet-stream",
    "application/vnd.apache.parquet",
    "application/x-parquet",
)
XML_TYPES = ("application/xml", "text/xml")


@dataclass(frozen=True)
class Hub:
    key: str  # flusight | covidhub
    name: str
    bucket: str
    target: str  # hub target name verbatim
    indicator: str
    ensemble_model: str
    target_csv: str
    rights: Mapping[str, str]

    @property
    def host(self) -> str:
        return f"{self.bucket}.s3.amazonaws.com"

    @property
    def base(self) -> str:
        return f"https://{self.host}"

    def list_url(self, prefix: str) -> str:
        return f"{self.base}/?list-type=2&prefix={prefix}&max-keys=1000"


FLUSIGHT = Hub(
    key="flusight",
    name="CDC FluSight forecast hub",
    bucket="cdcepi-flusight-forecast-hub",
    target="wk inc flu hosp",
    indicator="nhsn.wk_inc_flu_hosp",
    ensemble_model="FluSight-ensemble",
    target_csv="target-data/target-hospital-admissions.csv",
    rights={
        "licence": "MIT (repository); data use: attribution requested by CDC (flusight@cdc.gov)",
        "licence_url": "https://github.com/cdcepi/FluSight-forecast-hub/blob/main/LICENSE",
        "attribution": "CDC FluSight forecast hub (cdcepi/FluSight-forecast-hub): NHSN Hospital Respiratory Data "
        "target data and FluSight-ensemble forecasts",
        "attribution_request": "README: 'Anyone interested in using these data for additional research or "
        "publications is requested to contact flusight@cdc.gov for information regarding attribution of the "
        "source forecasts.'",
        "source_key": SOURCE_KEY,
    },
)
COVIDHUB = Hub(
    key="covidhub",
    name="CDC COVID-19 forecast hub",
    bucket="covid19-forecast-hub",
    target="wk inc covid hosp",
    indicator="nhsn.wk_inc_covid_hosp",
    ensemble_model="CovidHub-ensemble",
    target_csv="target-data/covid-hospital-admissions.csv",
    rights={
        "licence": "CC0 1.0 / US public domain (README 'Public Domain Standard Notice'); attribution requested "
        "(covidhub@cdc.gov)",
        "licence_url": "https://creativecommons.org/publicdomain/zero/1.0/",
        "attribution": "CDC COVID-19 Forecast Hub (CDCgov/covid19-forecast-hub): NHSN Hospital Respiratory Data "
        "target data and CovidHub-ensemble forecasts",
        "attribution_request": "README: 'If you are interested in using these data for additional research or "
        "publications, please contact covidhub@cdc.gov for information regarding attribution of the source "
        "forecasts.'",
        "source_key": SOURCE_KEY,
    },
)
HUBS = {h.key: h for h in (FLUSIGHT, COVIDHUB)}
_LOC_RE = re.compile(r"^(US|[0-9]{2})$")


class HubParseError(SourceResponseError):
    pass


def _err(msg: str) -> HubParseError:
    return HubParseError(msg, retryable=False, kind="schema_drift")


def epiweek(tend: date) -> tuple[date, date]:
    """``[start, end)`` of the CDC epiweek (Sunday-Saturday) ending on ``tend`` (a Saturday)."""
    if tend.weekday() != 5:
        raise _err(f"target_end_date {tend} is not a Saturday")
    return tend - timedelta(days=6), tend + timedelta(days=1)


# --------------------------------------------------------------------------- parsers (pure)


def parse_listing(body: bytes) -> list[tuple[str, int]]:
    """(key, size) of an S3 ListObjectsV2 response; truncated listings and DTDs are drift."""
    if b"<!DOCTYPE" in body or b"<!ENTITY" in body:
        raise _err("S3 listing carries a DTD")
    try:
        root = ElementTree.fromstring(body)  # noqa: S314 - DTD/entity declarations refused above
    except ElementTree.ParseError as e:
        raise _err("S3 listing is not XML") from e
    if root.tag != f"{S3_NS}ListBucketResult":
        raise _err(f"S3 listing root is {root.tag!r}")
    if (root.findtext(f"{S3_NS}IsTruncated") or "").strip() != "false":
        raise _err("S3 listing is truncated (paging is not implemented)")
    out = []
    for c in root.findall(f"{S3_NS}Contents"):
        k, s = c.findtext(f"{S3_NS}Key"), c.findtext(f"{S3_NS}Size")
        if not k or s is None or not s.isdigit():
            raise _err("S3 listing entry without Key/Size")
        out.append((k, int(s)))
    return out


def _dec(v: Any, what: str) -> str:
    try:
        d = Decimal(str(v).strip())
    except InvalidOperation as e:
        raise _err(f"{what}: not a number {v!r}") from e
    if not d.is_finite() or d < 0:
        raise _err(f"{what}: not a finite non-negative number {v!r}")
    return dec_str(d)


def parse_target_csv(body: bytes, hub: Hub) -> list[dict[str, Any]]:
    """Rows ``{tend, location, value, line}`` of a hub target CSV (current file or FluSight archive)."""
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise _err("target CSV is not UTF-8") from e
    rd = csv.DictReader(io.StringIO(text))
    cols = set(rd.fieldnames or ())
    if hub.key == "flusight":
        need, dcol = {"date", "location", "value"}, "date"
    else:
        need, dcol = {"target_end_date", "location", "value"}, "target_end_date"
    if not need <= cols:
        raise _err(f"target CSV columns {sorted(cols)} lack {sorted(need - cols)}")
    out: list[dict[str, Any]] = []
    seen: set[tuple[date, str]] = set()
    for i, r in enumerate(rd, start=2):
        loc = (r["location"] or "").strip()
        if not _LOC_RE.match(loc):
            raise _err(f"line {i}: location {loc!r}")
        try:
            tend = date.fromisoformat(r[dcol].strip())
        except ValueError as e:
            raise _err(f"line {i}: date {r[dcol]!r}") from e
        epiweek(tend)
        raw_v = (r["value"] or "").strip()
        value = None if raw_v in ("", "NA") else _dec(raw_v, f"line {i}")
        if (tend, loc) in seen:
            raise _err(f"line {i}: duplicate ({tend}, {loc})")
        seen.add((tend, loc))
        out.append(
            {"tend": tend, "location": loc, "value": value, "line": i, "name": r.get("location_name") or r.get("state")}
        )
    if not out:
        raise _err("target CSV has no rows")
    return out


def parse_time_series(body: bytes, hub: Hub) -> list[dict[str, Any]]:
    """Rows ``{as_of, tend, location, value, row}`` of the hub's hubverse ``time-series.parquet`` (target only)."""
    import polars as pl

    try:
        f = pl.read_parquet(io.BytesIO(body))
    except Exception as e:  # polars raises several types on a corrupt file
        raise _err(f"time-series parquet unreadable: {type(e).__name__}") from e
    need = {"as_of", "target", "target_end_date", "location", "observation"}
    if not need <= set(f.columns):
        raise _err(f"time-series columns {f.columns} lack {sorted(need - set(f.columns))}")
    f = f.with_row_index("row").filter(pl.col("target") == hub.target)
    if f.height == 0:
        raise _err(f"time-series has no {hub.target!r} rows")
    out = []
    for r in f.select("row", "as_of", "target_end_date", "location", "observation").iter_rows(named=True):
        loc = str(r["location"])
        if not _LOC_RE.match(loc):
            raise _err(f"time-series row {r['row']}: location {loc!r}")
        epiweek(r["target_end_date"])
        v = r["observation"]
        out.append(
            {
                "as_of": r["as_of"],
                "tend": r["target_end_date"],
                "location": loc,
                "value": None if v is None else _dec(repr(float(v)), f"time-series row {r['row']}"),
                "row": int(r["row"]),
            }
        )
    return out


def parse_ensemble(body: bytes, hub: Hub, reference_date: date) -> list[dict[str, Any]]:
    """Quantile rows of one ensemble file for the hub target, horizons 0..3."""
    import polars as pl

    try:
        f = pl.read_parquet(io.BytesIO(body))
    except Exception as e:
        raise _err(f"ensemble parquet unreadable: {type(e).__name__}") from e
    need = {
        "reference_date",
        "location",
        "horizon",
        "target",
        "target_end_date",
        "output_type",
        "output_type_id",
        "value",
        "model_id",
    }
    if not need <= set(f.columns):
        raise _err(f"ensemble columns {f.columns} lack {sorted(need - set(f.columns))}")
    f = f.with_row_index("row").filter(
        (pl.col("target") == hub.target) & (pl.col("output_type") == "quantile") & pl.col("horizon").is_in(HORIZONS)
    )
    out = []
    seen: set[tuple[str, int, str]] = set()
    for r in f.iter_rows(named=True):
        if r["model_id"] != hub.ensemble_model:
            raise _err(f"ensemble row {r['row']}: model_id {r['model_id']!r}")
        if r["reference_date"] != reference_date:
            raise _err(f"ensemble row {r['row']}: reference_date {r['reference_date']} != file date {reference_date}")
        if r["target_end_date"] != reference_date + timedelta(days=7 * int(r["horizon"])):
            raise _err(f"ensemble row {r['row']}: target_end_date does not match reference_date + 7 * horizon")
        loc = str(r["location"])
        if not _LOC_RE.match(loc):
            raise _err(f"ensemble row {r['row']}: location {loc!r}")
        q = str(r["output_type_id"])
        try:
            ql = Decimal(q)
        except InvalidOperation as e:
            raise _err(f"ensemble row {r['row']}: quantile level {q!r}") from e
        if not 0 < ql < 1:
            raise _err(f"ensemble row {r['row']}: quantile level {q!r}")
        key = (loc, int(r["horizon"]), q)
        if key in seen:
            raise _err(f"ensemble row {r['row']}: duplicate {key}")
        seen.add(key)
        v = r["value"]
        if v is None:
            raise _err(f"ensemble row {r['row']}: null quantile value")
        out.append(
            {
                "location": loc,
                "horizon": int(r["horizon"]),
                "tend": r["target_end_date"],
                "q": q,
                "value": dec_str(Decimal(repr(float(v)))),
                "row": int(r["row"]),
            }
        )
    return out


# --------------------------------------------------------------------------- atlas rows


def _row(
    *,
    dataset: str,
    indicator: str,
    loc: str,
    tend: date,
    value: str | None,
    flags: dict[str, Any],
    raw: str,
    sel: dict[str, Any],
) -> dict[str, Any]:
    from reva.atlas.tables import missing_flags

    start, end = epiweek(tend)
    if value is None:
        flags = json.loads(missing_flags("not_reported", **flags))
    return {
        "source": "cdc",
        "dataset": dataset,
        "indicator": indicator,
        "concept_id": None,
        "iso3": None,
        "provider_geo": loc,
        "entity_qid": None,
        "period_start": start,
        "period_end": end,
        "value": value,
        "value_text": None,
        "unit": "hospital admissions (count, epiweek)",
        "flags": fit_flags(flags),
        "raw_sha256": raw,
        "selector": canon(sel),
    }


def _obs_flags(hub: Hub, tend: date, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "producer": "NHSN Hospital Respiratory Data, as compiled by the " + hub.name,
        "hub_target": hub.target,
        "target_end_date": tend.isoformat(),
        **dict(extra or {}),
    }


def target_rows(rows: Sequence[Mapping[str, Any]], hub: Hub, *, raw: str, item: str) -> list[dict[str, Any]]:
    return [
        _row(
            dataset=f"{hub.key}_target",
            indicator=hub.indicator,
            loc=r["location"],
            tend=r["tend"],
            value=r["value"],
            flags=_obs_flags(hub, r["tend"]),
            raw=raw,
            sel={"item": item, "line": r["line"]},
        )
        for r in rows
    ]


def vintage_rows(
    rows: Sequence[Mapping[str, Any]], hub: Hub, *, vintage: date, full: bool, raw: str, item: str
) -> list[dict[str, Any]]:
    """One vintage: the window of :data:`VINTAGE_WINDOW_WEEKS` weeks up to its last epiweek (full history if ``full``)."""
    last = max(r["tend"] for r in rows)
    lo = last - timedelta(weeks=VINTAGE_WINDOW_WEEKS)
    out = []
    for r in rows:
        if not full and r["tend"] <= lo:
            continue
        sel = {"item": item, **({"line": r["line"]} if "line" in r else {"row": r["row"]})}
        out.append(
            _row(
                dataset=f"{hub.key}_vintages",
                indicator=f"{hub.indicator}|vintage={vintage.isoformat()}",
                loc=r["location"],
                tend=r["tend"],
                value=r["value"],
                flags=_obs_flags(
                    hub, r["tend"], {"vintage": vintage.isoformat(), "vintage_last_epiweek": last.isoformat()}
                ),
                raw=raw,
                sel=sel,
            )
        )
    return out


def ensemble_rows(
    rows: Sequence[Mapping[str, Any]], hub: Hub, *, ref: date, raw: str, item: str
) -> list[dict[str, Any]]:
    tgt = hub.target.replace(" ", "_")
    return [
        _row(
            dataset=f"{hub.key}_ensemble",
            indicator=f"{hub.ensemble_model}.{tgt}|ref={ref.isoformat()}|q={r['q']}",
            loc=r["location"],
            tend=r["tend"],
            value=r["value"],
            flags={
                "estimate_kind": "projection",
                "producer": hub.name,
                "model_id": hub.ensemble_model,
                "hub_target": hub.target,
                "reference_date": ref.isoformat(),
                "horizon": r["horizon"],
                "quantile_level": r["q"],
                "target_end_date": r["tend"].isoformat(),
            },
            raw=raw,
            sel={"item": item, "row": r["row"]},
        )
        for r in rows
    ]


# --------------------------------------------------------------------------- run objects


_ENS_RE = re.compile(r"/(\d{4}-\d{2}-\d{2})-[A-Za-z0-9_.-]+\.parquet$")
_ARCH_RE = re.compile(r"/target-hospital-admissions_(\d{4}-\d{2}-\d{2})\.csv$")


class _HubDataset:
    ATLAS_SOURCE = "cdc"
    GEO_SCHEME = "cdc_hub"

    def __init__(self, hub: Hub, *, first_ref: date = FIRST_REFERENCE_DATE) -> None:
        self.hub = hub
        self.RIGHTS = dict(hub.rights)
        self.first_ref = first_ref

    def _want(self, d: date) -> bool:
        """A file whose round (reference date, or vintage date + 7 days) is in the back-test window."""
        return d >= self.first_ref - timedelta(days=7)

    def follow_ups(self, item: FetchItem, body: bytes) -> tuple[FetchItem, ...]:
        return ()


class TargetDataset(_HubDataset):
    def items(self) -> tuple[FetchItem, ...]:
        return (
            FetchItem(
                key=f"{self.hub.key}.target",
                url=f"{self.hub.base}/{self.hub.target_csv}",
                max_bytes=MAX_ITEM_BYTES,
                expected_media_types=CSV_TYPES,
            ),
        )

    def check(self, item: FetchItem, body: bytes) -> dict[str, Any]:
        return {"rows": len(parse_target_csv(body, self.hub))}

    def atlas_frame(self, bodies: Mapping[str, bytes], raws: Mapping[str, str]) -> tuple[Any, str, dict[str, Any]]:
        import polars as pl

        from reva.atlas.tables import ATLAS_SCHEMA

        key = f"{self.hub.key}.target"
        rows = parse_target_csv(bodies[key], self.hub)
        frame = pl.DataFrame(target_rows(rows, self.hub, raw=raws[key], item=key), schema=ATLAS_SCHEMA)
        last = max(r["tend"] for r in rows)
        note = f"{self.hub.name} target data ({self.hub.target_csv}): NHSN weekly admissions through epiweek ending {last}."
        return frame, note, {"last_epiweek": last.isoformat(), "rows": len(rows)}


class EnsembleDataset(_HubDataset):
    def items(self) -> tuple[FetchItem, ...]:
        url = self.hub.list_url(f"model-output/{self.hub.ensemble_model}/")
        return (
            FetchItem(
                key=f"{self.hub.key}.ensemble.list",
                url=url,
                max_bytes=2_000_000,
                expected_media_types=XML_TYPES,
                params={"kind": "list"},
            ),
        )

    def follow_ups(self, item: FetchItem, body: bytes) -> tuple[FetchItem, ...]:
        if item.params.get("kind") != "list":
            return ()
        out = []
        for k, _size in parse_listing(body):
            m = _ENS_RE.search("/" + k)
            if not m or not k.endswith(f"-{self.hub.ensemble_model}.parquet"):
                continue
            ref = date.fromisoformat(m.group(1))
            if ref < self.first_ref:
                continue
            out.append(
                FetchItem(
                    key=f"{self.hub.key}.ensemble:{ref}",
                    url=f"{self.hub.base}/{k}",
                    max_bytes=MAX_ITEM_BYTES,
                    expected_media_types=PARQUET_TYPES,
                    params={"kind": "ensemble", "reference_date": ref.isoformat()},
                )
            )
        return tuple(sorted(out, key=lambda i: i.key))

    def check(self, item: FetchItem, body: bytes) -> dict[str, Any]:
        if item.params.get("kind") == "list":
            return {"files": len(parse_listing(body))}
        ref = date.fromisoformat(str(item.params["reference_date"]))
        rows = parse_ensemble(body, self.hub, ref)
        if not rows:
            raise _err(f"{item.key}: no {self.hub.target!r} quantile rows")
        return {"rows": len(rows)}

    def atlas_frame(self, bodies: Mapping[str, bytes], raws: Mapping[str, str]) -> tuple[Any, str, dict[str, Any]]:
        import polars as pl

        from reva.atlas.tables import ATLAS_SCHEMA

        parts, refs = [], []
        for key in sorted(k for k in bodies if k.startswith(f"{self.hub.key}.ensemble:")):
            ref = date.fromisoformat(key.split(":", 1)[1])
            rows = parse_ensemble(bodies[key], self.hub, ref)
            parts.append(
                pl.DataFrame(ensemble_rows(rows, self.hub, ref=ref, raw=raws[key], item=key), schema=ATLAS_SCHEMA)
            )
            refs.append(ref)
        if not parts:
            raise ValidationProblem("no ensemble file in the listing window: registration refused", hub=self.hub.key)
        note = (
            f"{self.hub.ensemble_model} quantile forecasts of '{self.hub.target}' (horizons 0-3), reference dates "
            f"{min(refs)}..{max(refs)} ({len(refs)} rounds); projections attributed to the {self.hub.name}."
        )
        return pl.concat(parts), note, {"rounds": len(refs), "first_ref": str(min(refs)), "last_ref": str(max(refs))}


class FluVintageDataset(_HubDataset):
    """FluSight target-data archive files (one dated file per vintage)."""

    def items(self) -> tuple[FetchItem, ...]:
        url = self.hub.list_url("auxiliary-data/target-data-archive/target-hospital-admissions_")
        return (
            FetchItem(
                key="flusight.vintages.list",
                url=url,
                max_bytes=2_000_000,
                expected_media_types=XML_TYPES,
                params={"kind": "list"},
            ),
        )

    def follow_ups(self, item: FetchItem, body: bytes) -> tuple[FetchItem, ...]:
        if item.params.get("kind") != "list":
            return ()
        out = []
        for k, _size in parse_listing(body):
            m = _ARCH_RE.search("/" + k)
            if not m:
                continue
            v = date.fromisoformat(m.group(1))
            if not self._want(v):
                continue
            out.append(
                FetchItem(
                    key=f"flusight.vintage:{v}",
                    url=f"{self.hub.base}/{k}",
                    max_bytes=MAX_ITEM_BYTES,
                    expected_media_types=CSV_TYPES,
                    params={"kind": "vintage", "vintage": v.isoformat()},
                )
            )
        return tuple(sorted(out, key=lambda i: i.key))

    def check(self, item: FetchItem, body: bytes) -> dict[str, Any]:
        if item.params.get("kind") == "list":
            return {"files": len(parse_listing(body))}
        rows = parse_target_csv(body, self.hub)
        v = date.fromisoformat(str(item.params["vintage"]))
        last = max(r["tend"] for r in rows)
        if last != v:
            raise _err(f"{item.key}: archive file date {v} is not its last epiweek {last}")
        return {"rows": len(rows)}

    def atlas_frame(self, bodies: Mapping[str, bytes], raws: Mapping[str, str]) -> tuple[Any, str, dict[str, Any]]:
        import polars as pl

        from reva.atlas.tables import ATLAS_SCHEMA

        keys = sorted(k for k in bodies if k.startswith("flusight.vintage:"))
        if not keys:
            raise ValidationProblem("no FluSight archive vintage in the window: registration refused")
        parts = []
        for i, key in enumerate(keys):
            v = date.fromisoformat(key.split(":", 1)[1])
            rows = parse_target_csv(bodies[key], self.hub)
            parts.append(
                pl.DataFrame(
                    vintage_rows(rows, self.hub, vintage=v, full=i == 0, raw=raws[key], item=key), schema=ATLAS_SCHEMA
                )
            )
        vs = [k.split(":", 1)[1] for k in keys]
        note = (
            f"FluSight target-data archive: {len(vs)} vintages {vs[0]}..{vs[-1]} (file date = last epiweek); "
            f"window {VINTAGE_WINDOW_WEEKS} weeks per vintage, full history for the earliest."
        )
        return pl.concat(parts), note, {"vintages": vs}


class CovidVintageDataset(_HubDataset):
    """COVID-19 hub hubverse time series: one ``as_of`` per weekly data vintage."""

    def items(self) -> tuple[FetchItem, ...]:
        return (
            FetchItem(
                key="covidhub.time_series",
                url=f"{self.hub.base}/target-data/time-series.parquet",
                max_bytes=MAX_ITEM_BYTES,
                expected_media_types=PARQUET_TYPES,
            ),
        )

    def check(self, item: FetchItem, body: bytes) -> dict[str, Any]:
        return {"rows": len(parse_time_series(body, self.hub))}

    def atlas_frame(self, bodies: Mapping[str, bytes], raws: Mapping[str, str]) -> tuple[Any, str, dict[str, Any]]:
        import polars as pl

        from reva.atlas.tables import ATLAS_SCHEMA

        key = "covidhub.time_series"
        rows = parse_time_series(bodies[key], self.hub)
        by: dict[date, list[dict[str, Any]]] = {}
        for r in rows:
            by.setdefault(r["as_of"], []).append(r)
        asofs = sorted(a for a in by if self._want(a + timedelta(days=3)))
        if not asofs:
            raise ValidationProblem("no COVID hub vintage in the window: registration refused")
        parts = [
            pl.DataFrame(
                vintage_rows(by[a], self.hub, vintage=a, full=i == 0, raw=raws[key], item=key), schema=ATLAS_SCHEMA
            )
            for i, a in enumerate(asofs)
        ]
        note = (
            f"COVID-19 Forecast Hub time-series.parquet ('{self.hub.target}'): {len(asofs)} vintages as_of "
            f"{asofs[0]}..{asofs[-1]}; window {VINTAGE_WINDOW_WEEKS} weeks per vintage, full history for the earliest."
        )
        return pl.concat(parts), note, {"vintages": [a.isoformat() for a in asofs]}


# --------------------------------------------------------------------------- sources


def _datasets(hub: Hub) -> list[tuple[str, Any]]:
    """Fixture mode replays tests/fixtures/source/atlas.cdc_{flusight,covidhub} (listings trimmed to the captured files)."""
    vint: Any = FluVintageDataset(hub) if hub.key == "flusight" else CovidVintageDataset(hub)
    return [
        (f"{hub.key}_target", TargetDataset(hub)),
        (f"{hub.key}_vintages", vint),
        (f"{hub.key}_ensemble", EnsembleDataset(hub)),
    ]


def _spec(hub: Hub) -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=f"atlas.cdc_{hub.key}",
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source="cdc",
        provider=f"US CDC {hub.name} (hubverse S3 mirror of the GitHub repository)",
        licence=hub.rights["licence"],
        allowed_hosts=(hub.host,),
        datasets=(f"{hub.key}_target", f"{hub.key}_vintages", f"{hub.key}_ensemble"),
        dataset_ids=(hub.target_csv, f"model-output/{hub.ensemble_model}"),
        cadence_seconds=7 * 86400,
        planned_requests=120 if hub.key == "flusight" else 110,
        max_response_bytes=MAX_ITEM_BYTES,
        min_interval_seconds=0.5,
        parser_version=PARSER_VERSION,
        code_digest=module_digest(Hub),
        field_mappings={
            "target": hub.target,
            "ensemble_model": hub.ensemble_model,
            "horizons": list(HORIZONS),
            "first_reference_date": FIRST_REFERENCE_DATE.isoformat(),
            "vintage_window_weeks": VINTAGE_WINDOW_WEEKS,
        },
        notes="Three datasets per hub (target, vintages, ensemble), each registered only if all its files parse.",
    )


def atlas_source() -> list[AtlasSourceSpec]:
    return [_spec(FLUSIGHT), _spec(COVIDHUB)]


async def _ingest(run: Any, hub: Hub) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for ds, obj in _datasets(hub):
        if run.wants(ds):
            out[ds] = await fetch_archive_register(run, obj, dataset=ds)
    return out


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """ATLASCORE entry point (``uv run reva atlas ingest atlas.cdc_flusight`` / ``atlas.cdc_covidhub``)."""
    hub = HUBS[run.spec.name.removeprefix("atlas.cdc_")]
    return await _ingest(run, hub)


__all__ = [
    "COVIDHUB",
    "FLUSIGHT",
    "HUBS",
    "atlas_ingest",
    "atlas_source",
    "epiweek",
    "parse_ensemble",
    "parse_listing",
    "parse_target_csv",
    "parse_time_series",
]
