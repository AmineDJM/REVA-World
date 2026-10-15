"""Constitute Project constitutions list -> atlas table ``constitute.constitutions`` (V3, agent A-LAW).

Source: ``https://www.constituteproject.org/service/constitutions?lang=en`` — the public JSON service
behind constituteproject.org (run by the Comparative Constitutions Project). One request per run
(~150 kB, 240 records on 2026-10-15). **Terms** (constituteproject.org/content/terms, checked
2026-10-15): no fee may be charged and commercial use is expressly prohibited; there is no open
data licence. This connector therefore takes only the metadata list (years, status) — never the
constitution texts, several of which are copyrighted translations — and every cell carries the
rights restriction in flags.

The service is undocumented. Fields are stored verbatim under their own names; REVA does not
reinterpret them (e.g. ``years_in_force`` looks computed against a fixed reference year, and
``years_amended`` is Constitute's own count). Indicators:

* in-force constitution of the country (``in_force = true``; one per country_id):
  ``in_force.<field>`` for ``year_enacted``, ``year_revised``, ``year_updated``, ``year_reinstated``,
  ``years_amended``, ``years_in_force``, ``word_length`` (numbers) and ``id``, ``title_long``
  (text);
* every other record (former constitutions and drafts): ``constitution.<field>|id=<id>`` for
  ``year_enacted``, ``year_to`` and ``status`` (``former`` / ``draft`` / ``historic``).

Period: the retrieval day ``[d, d+1)`` (a snapshot of the list; ``year_*`` values are years
reported by the source, not the period of the cell). Geography: ``provider_geo`` = ``country_id``,
mapped with the agent's ``constitute`` scheme (``registry/atlas/geo_aliases/a_law.yaml``);
Baden, Bavaria, Germany (Prussia) and Taiwan stay unmapped (kept, listed).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from typing import Any

from reva.adapters.connectors import atlas_law_common as common
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

SOURCE_KEY = "constitute.service"
ATLAS_SOURCE = "constitute"
ATLAS_DATASET = "constitutions"
CONNECTOR_NAME = "atlas.constitute"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas-constitute-parse-1"
HOST = "www.constituteproject.org"
LIST_URL = f"https://{HOST}/service/constitutions?lang=en"
MAX_BYTES = 5_000_000
MIN_RECORDS = 150  # 240 on 2026-10-15; fewer than 150 = truncated/changed service

RIGHTS = {
    "licence": "Constitute terms of use: no fee, commercial use prohibited (no open data licence)",
    "licence_url": "https://www.constituteproject.org/content/terms",
    "attribution": "Constitute Project (Comparative Constitutions Project), constituteproject.org",
    "commercial_use": "prohibited",
}
IN_FORCE_NUMERIC = (
    "year_enacted",
    "year_revised",
    "year_updated",
    "year_reinstated",
    "years_amended",
    "years_in_force",
    "word_length",
)
IN_FORCE_TEXT = ("id", "title_long")
OTHER_NUMERIC = ("year_enacted", "year_to")
REQUIRED = ("id", "country_id", "country", "in_force", "is_draft", "is_historic", "year_enacted")


class ConstituteParseError(SourceResponseError):
    pass


def _err(msg: str) -> ConstituteParseError:
    return ConstituteParseError(msg, retryable=False, kind="schema_drift")


def atlas_source() -> AtlasSourceSpec:
    """The ATLASCORE source spec (connector ``atlas.constitute``; budget key ``constitute.service``)."""
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="Constitute Project (Comparative Constitutions Project)",
        licence=RIGHTS["licence"],
        allowed_hosts=(HOST,),
        datasets=(ATLAS_DATASET,),
        dataset_ids=("service/constitutions",),
        cadence_seconds=90 * 86400,
        planned_requests=1,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=common.code_digest(sys.modules[__name__]),
        field_mappings={"time_semantics": "snapshot of the list on the retrieval day", "texts_fetched": False},
    )


def _num(v: Any, field: str, idx: int) -> str | None:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        raise _err(f"record {idx}: {field} is a boolean")
    s = str(v).strip()
    if not s.lstrip("-").isdigit():
        raise _err(f"record {idx}: {field} is not an integer ({v!r})")
    return str(int(s))


def parse_list(body: bytes, raw_sha256: str, retrieved_at: datetime) -> list[dict[str, Any]]:
    try:
        records = json.loads(body)
    except ValueError as e:
        raise _err("constitutions list is not JSON") from e
    if not isinstance(records, list):
        raise _err("constitutions list is not a JSON array")
    if len(records) < MIN_RECORDS:
        raise _err(f"only {len(records)} records (< {MIN_RECORDS}): truncated or changed service")
    day = retrieved_at.date() if isinstance(retrieved_at, datetime) else retrieved_at
    period = (day, day + timedelta(days=1))
    base = {
        "snapshot_retrieved_at": retrieved_at.isoformat() if isinstance(retrieved_at, datetime) else str(day),
        "transcription": common.VERBATIM,
        "licence": RIGHTS["licence"],
        "commercial_use": "prohibited",
        "field_semantics": "undocumented Constitute service field, stored verbatim",
    }
    rows: list[dict[str, Any]] = []
    in_force_seen: dict[str, str] = {}
    for idx, r in enumerate(records):
        if not isinstance(r, dict):
            raise _err(f"record {idx} is not an object")
        miss = [f for f in REQUIRED if f not in r]
        if miss:
            raise _err(f"record {idx} lacks {miss}")
        cid, geo = str(r["id"]), str(r["country_id"])
        if not geo or len(geo) > 64:
            raise _err(f"record {idx}: bad country_id {geo!r}")
        flags = {**base, "country": r.get("country"), "constitution_id": cid}
        if r.get("copyright"):
            flags["provider_copyright_note"] = common.clip_text(str(r["copyright"]), 500)[0]
        sel = {"file": "constitutions?lang=en", "index": idx, "id": cid}

        def add(
            indicator: str,
            field: str,
            value: str | None = None,
            text: str | None = None,
            unit: str = "year",
            geo: str = geo,
            sel: dict[str, Any] = sel,
            flags: dict[str, Any] = flags,
        ) -> None:
            if value is None and text is None:
                return
            rows.append(
                common.row(
                    source=ATLAS_SOURCE,
                    dataset=ATLAS_DATASET,
                    indicator=indicator,
                    provider_geo=geo,
                    period=period,
                    raw_sha256=raw_sha256,
                    selector={**sel, "field": field},
                    flags=flags,
                    unit=unit,
                    value=value,
                    value_text=text,
                )
            )

        if r["in_force"] is True:
            if geo in in_force_seen:
                raise _err(f"two in-force constitutions for {geo}: {in_force_seen[geo]} and {cid}")
            in_force_seen[geo] = cid
            for f in IN_FORCE_NUMERIC:
                unit = (
                    "count" if f in ("years_amended", "word_length") else ("years" if f == "years_in_force" else "year")
                )
                add(f"in_force.{f}", f, value=_num(r.get(f), f, idx), unit=unit)
            for f in IN_FORCE_TEXT:
                v = r.get(f)
                if v:
                    add(f"in_force.{f}", f, text=common.clip_text(str(v), 1000)[0], unit="text")
        else:
            status = "draft" if r.get("is_draft") else ("historic" if r.get("is_historic") else "former")
            for f in OTHER_NUMERIC:
                add(f"constitution.{f}|id={cid}", f, value=_num(r.get(f), f, idx))
            add(f"constitution.status|id={cid}", "in_force/is_draft/is_historic", text=status, unit="category")
    return rows


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """One request: the constitutions list -> atlas table ``constitute.constitutions``."""
    report = common.IngestReport(CONNECTOR_NAME)
    if not run.wants(ATLAS_DATASET):
        return report.as_dict()
    got = await common.fetch(run, LIST_URL, key="constitutions", source_key=SOURCE_KEY, max_bytes=MAX_BYTES)
    if isinstance(got, common.Blocked):
        report.blocked.append(got)
        return report.as_dict()
    sha = run.archive(got)
    report.raw.append({"item": "constitutions", "raw_sha256": sha, "bytes": len(got.body), "url": LIST_URL})
    rows = parse_list(got.body, sha, got.retrieved_at)
    out = run.register(
        dataset=ATLAS_DATASET,
        frame=common.atlas_frame(rows),
        raw_sha256s=[sha],
        geo_scheme="constitute",
        rights=RIGHTS,
        coverage_note="Constitute constitutions list (metadata only, no texts); non-commercial terms",
    )
    report.tables.append(common.table_summary(out))
    return report.as_dict()


__all__ = ["ATLAS_DATASET", "ATLAS_SOURCE", "CONNECTOR_NAME", "atlas_ingest", "atlas_source", "parse_list"]
