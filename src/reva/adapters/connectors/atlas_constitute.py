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
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import ConnectorSpec, FetchItem

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


class ConstituteConnector:
    def describe(self) -> ConnectorSpec:
        return common.law_spec(
            name=CONNECTOR_NAME,
            version=CONNECTOR_VERSION,
            source_key=SOURCE_KEY,
            provider="Constitute Project (Comparative Constitutions Project)",
            licence=RIGHTS["licence"],
            allowed_hosts=(HOST,),
            dataset_ids=(ATLAS_DATASET,),
            field_mappings={"tables": {"list": f"{ATLAS_SOURCE}.{ATLAS_DATASET}"}},
            units={"years": "year", "counts": "count"},
            geography=("ALL_CONSTITUTE_COUNTRIES",),
            time_semantics="snapshot of the list on the retrieval day",
            lineage_family="ccp",
            cadence_seconds=90 * 86400,
            revision_strategy="snapshot per retrieval; new atlas table version when the list changes",
            max_response_bytes=MAX_BYTES,
            max_pages=1,
            parser_version=PARSER_VERSION,
            code_digest=common.code_digest(sys.modules[__name__]),
        )

    def items(self) -> tuple[FetchItem, ...]:
        return (FetchItem(key="constitutions", url=LIST_URL, max_bytes=MAX_BYTES),)


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


async def run(
    engine: Any, store: Any, http: Any, *, origin: str, actor: str = "worker:atlas_constitute"
) -> common.IngestReport:
    from reva.atlas.tables import register_atlas_table

    conn = ConstituteConnector()
    spec = conn.describe()
    report = common.IngestReport(CONNECTOR_NAME)
    (item,) = conn.items()
    got = await common.fetch_raw(http, item, SOURCE_KEY)
    if isinstance(got, common.Blocked):
        report.blocked.append(got)
        return report
    sha = common.archive_raw(engine, store, spec, got, actor=actor)
    report.raw.append({"item": item.key, "raw_sha256": sha, "bytes": len(got.body), "url": item.url})
    rows = parse_list(got.body, sha, got.retrieved_at)
    s = register_atlas_table(
        engine,
        store,
        source=ATLAS_SOURCE,
        dataset=ATLAS_DATASET,
        frame=common.atlas_frame(rows),
        raw_sha256s=[sha],
        parser_version=PARSER_VERSION,
        origin=origin,
        geo_scheme="constitute",
        rights=RIGHTS,
        coverage_note="Constitute constitutions list (metadata only, no texts); non-commercial terms",
    )
    report.tables.append(
        {k: s[k] for k in ("dataset_key", "artifact_sha256", "row_count", "created", "unmapped_provider_geo")}
        | {"countries": s["coverage"]["countries"]}
    )
    return report


__all__ = ["ATLAS_DATASET", "ATLAS_SOURCE", "CONNECTOR_NAME", "ConstituteConnector", "parse_list", "run"]
