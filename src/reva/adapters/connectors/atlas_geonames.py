"""GeoNames ``countryInfo.txt`` -> atlas table ``geonames.countryinfo`` (V3, agent A-GEO).

Source: ``https://download.geonames.org/export/dump/countryInfo.txt`` (one ~32 kB tab-separated file, licence
CC BY 4.0, attribution "GeoNames, www.geonames.org"). One row per country/territory (252 at 2026-10-15,
including the historical codes ``CS`` Serbia and Montenegro and ``AN`` Netherlands Antilles, which stay
unmapped and are kept).

Every cell is what GeoNames declares; nothing is recomputed:

* ``area_km2`` and ``population`` are numeric. GeoNames does **not** date them (the population is a
  snapshot GeoNames maintains; areas are mostly CIA World Factbook-style totals). Rows therefore carry the
  one-day retrieval snapshot as period and say so in flags (``period_basis``). A declared area of ``0``
  (e.g. U.S. Minor Outlying Islands) is kept as reported and flagged ``provider_zero_implausible``.
* Text cells: ``capital``, ``continent`` (GeoNames continent code), ``currency_code``, ``currency_name``,
  ``languages`` (GeoNames locale list ordered by number of speakers, verbatim), ``neighbours`` (ISO 3166
  alpha-2 list, verbatim), ``tld``, ``phone``, ``iso_numeric``, ``fips``, ``geonameid``, ``name``.
* Empty cells are omitted (GeoNames reports nothing), never filled.

The neighbour lists also become KG ``shares border with`` (P47) statements (source ``geonames.countryinfo``)
for countries whose QIDs are known (:func:`neighbour_statements`); GeoNames does not give border lengths.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from reva.adapters.connectors.atlas_geo_common import fetch_raw, snapshot_period
from reva.adapters.connectors.base import dec_str, module_digest, schema_fingerprint
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import (
    Cursor,
    FetchBudget,
    FetchItem,
    FetchOutcome,
    FetchPlan,
    Finding,
    ObservationBatch,
    QualityReport,
    RawAcquisition,
)

CONNECTOR_NAME = "atlas.geonames_countryinfo"
CONNECTOR_VERSION = "1.0.1"  # ATLASOPS: patch bump, code changed after 1.0.0 was registered in the dev DB (TWNMAP/agent edits)
SOURCE_KEY = "geonames.countryinfo"
PARSER_VERSION = "atlas-geonames-countryinfo/1"
ATLAS_SOURCE = "geonames"
ATLAS_DATASET = "countryinfo"
HOST = "download.geonames.org"
URL = f"https://{HOST}/export/dump/countryInfo.txt"
MAX_BYTES = 1_000_000
MIN_ROWS = 240

RIGHTS = {
    "licence": "CC BY 4.0",
    "licence_url": "https://creativecommons.org/licenses/by/4.0/",
    "attribution": "GeoNames (www.geonames.org), countryInfo.txt",
}

COLUMNS = (
    "ISO",
    "ISO3",
    "ISO-Numeric",
    "fips",
    "Country",
    "Capital",
    "Area(in sq km)",
    "Population",
    "Continent",
    "tld",
    "CurrencyCode",
    "CurrencyName",
    "Phone",
    "Postal Code Format",
    "Postal Code Regex",
    "Languages",
    "geonameid",
    "neighbours",
    "EquivalentFipsCode",
)
NUMERIC = {"Area(in sq km)": ("area_km2", "km2"), "Population": ("population", "persons")}
TEXT = {
    "Country": "name",
    "Capital": "capital",
    "Continent": "continent",
    "CurrencyCode": "currency_code",
    "CurrencyName": "currency_name",
    "Languages": "languages",
    "neighbours": "neighbours",
    "tld": "tld",
    "Phone": "phone",
    "ISO-Numeric": "iso_numeric",
    "fips": "fips",
    "geonameid": "geonameid",
}
CONTINENTS = {"AF", "AN", "AS", "EU", "NA", "OC", "SA"}


class GeoNamesError(SourceResponseError):
    pass


def _err(msg: str) -> GeoNamesError:
    return GeoNamesError(msg, retryable=False, kind="schema_drift")


@dataclass(frozen=True)
class CountryInfo:
    line: int  # 1-based line number in the raw file
    cells: dict[str, str]

    @property
    def iso2(self) -> str:
        return self.cells["ISO"]

    @property
    def iso3(self) -> str:
        return self.cells["ISO3"]


def parse_country_info(body: bytes) -> list[CountryInfo]:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as e:
        raise _err("countryInfo.txt is not UTF-8") from e
    header: list[str] | None = None
    rows: list[CountryInfo] = []
    for n, line in enumerate(text.splitlines(), start=1):
        if line.startswith("#ISO\t"):
            header = [c.strip() for c in line[1:].split("\t")]
            continue
        if line.startswith("#") or not line.strip():
            continue
        if header is None:
            raise _err("data line before the '#ISO' header line")
        cells = line.split("\t")
        if len(cells) != len(header):
            raise _err(f"line {n}: {len(cells)} columns, header has {len(header)}")
        rows.append(CountryInfo(n, dict(zip(header, cells, strict=True))))
    if header is None:
        raise _err("no '#ISO' header line")
    if tuple(header) != COLUMNS:
        raise _err(f"column layout changed: {header}")
    return rows


def validate_rows(rows: Sequence[CountryInfo]) -> list[str]:
    problems = []
    if len(rows) < MIN_ROWS:
        problems.append(f"only {len(rows)} countries (expected >= {MIN_ROWS})")
    iso3 = [r.iso3 for r in rows]
    if len(set(iso3)) != len(iso3):
        problems.append("duplicate ISO3 codes")
    for r in rows:
        if len(r.iso2) != 2 or len(r.iso3) != 3:
            problems.append(f"line {r.line}: bad ISO codes {r.iso2!r}/{r.iso3!r}")
        if r.cells["Continent"] not in CONTINENTS:
            problems.append(f"line {r.line}: unknown continent {r.cells['Continent']!r}")
    return problems


def _num(s: str) -> str | None:
    s = s.strip()
    if not s:
        return None
    try:
        d = Decimal(s)
    except InvalidOperation as e:
        raise _err(f"not a number: {s!r}") from e
    return dec_str(d)


def atlas_rows(rows: Sequence[CountryInfo], *, raw_sha256: str, retrieved_at: datetime) -> list[dict[str, Any]]:
    from reva.atlas.tables import country_iso3

    start, end = snapshot_period(retrieved_at)
    out = []
    base = {"period_basis": "retrieval snapshot: GeoNames does not date these figures", "as_declared_by": "GeoNames"}
    for r in rows:
        iso3 = country_iso3(r.iso3, "iso3")

        def add(
            indicator: str,
            value: str | None,
            text: str | None,
            unit: str,
            col: str,
            flags: dict[str, Any],
            r: CountryInfo = r,
            iso3: str | None = iso3,
        ) -> None:
            out.append(
                {
                    "source": ATLAS_SOURCE,
                    "dataset": ATLAS_DATASET,
                    "indicator": indicator,
                    "concept_id": None,
                    "iso3": iso3,
                    "provider_geo": r.iso3,
                    "entity_qid": None,
                    "period_start": start,
                    "period_end": end,
                    "value": value,
                    "value_text": text,
                    "unit": unit,
                    "flags": json.dumps({**base, **flags}, sort_keys=True, separators=(",", ":")),
                    "raw_sha256": raw_sha256,
                    "selector": json.dumps(
                        {"file": "countryInfo.txt", "line": r.line, "column": col},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                }
            )

        for col, (ind, unit) in NUMERIC.items():
            v = _num(r.cells[col])
            if v is None:
                continue
            extra: dict[str, Any] = (
                {"provider_zero_implausible": True} if (col == "Area(in sq km)" and v == "0") else {}
            )
            add(ind, v, None, unit, col, flags=extra)
        for col, ind in TEXT.items():
            raw = r.cells[col]
            t = raw.strip()
            if not t:
                continue
            extra_t: dict[str, Any] = {"whitespace_trimmed": True} if t != raw else {}
            if col == "ISO-Numeric" and t == "0":
                extra_t["provider_note"] = "GeoNames uses 0 when ISO 3166 assigns no numeric code"
            add(ind, None, t, "1", col, flags=extra_t)
    return out


def neighbour_pairs(rows: Sequence[CountryInfo]) -> tuple[list[tuple[str, str, int]], list[str]]:
    """(iso3 subject, iso3 neighbour, line) from the ``neighbours`` column, resolving ISO2 through the file
    itself; unresolvable codes are returned separately."""
    by2 = {r.iso2: r.iso3 for r in rows}
    out, bad = [], []
    for r in rows:
        for code in filter(None, (c.strip() for c in r.cells["neighbours"].split(","))):
            if code in by2:
                out.append((r.iso3, by2[code], r.line))
            else:
                bad.append(f"{r.iso3}:{code}")
    return out, bad


def neighbour_statements(
    rows: Sequence[CountryInfo], qids: Mapping[str, str], *, raw_sha256: str, retrieved_at: datetime
) -> tuple[list[Any], dict[str, Any]]:
    from reva.atlas.tables import country_iso3
    from reva.kg.model import KgStatement

    pairs, bad = neighbour_pairs(rows)
    sts, skipped = [], 0
    for a, b, line in pairs:
        ka, kb = country_iso3(a, "iso3"), country_iso3(b, "iso3")
        if not ka or not kb or ka not in qids or kb not in qids:
            skipped += 1
            continue
        sts.append(
            KgStatement(
                subject_qid=qids[ka],
                property_pid="P47",
                object_qid=qids[kb],
                qualifiers={
                    "subject_iso3": ka,
                    "object_iso3": kb,
                    "border_kind": "as declared by GeoNames (no length)",
                },
                references=[
                    {
                        "source": "GeoNames countryInfo.txt",
                        "url": URL,
                        "line": line,
                        "raw_sha256": raw_sha256,
                        "retrieved_at": retrieved_at.isoformat(),
                    }
                ],
                source_key=SOURCE_KEY,
                source_revision=raw_sha256[:16],
                statement_id=f"geonames-neighbour:{ka}:{kb}",
                raw_sha256=raw_sha256,
            )
        )
    return sts, {"statements": len(sts), "skipped_no_qid_or_unmapped": skipped, "unresolved_codes": bad}


class GeoNamesCountryInfoConnector:
    def describe(self) -> AtlasSourceSpec:
        return atlas_source()

    def plan(self, cursor: Cursor | None, budget: FetchBudget) -> FetchPlan:
        return FetchPlan(connector=CONNECTOR_NAME, items=fetch_items(), pagination="single file")

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition:
        return await fetch_raw(item, http)

    def parse(self, raw: RawAcquisition) -> Iterator[ObservationBatch]:
        rows = parse_country_info(raw.body)
        yield ObservationBatch(
            connector=CONNECTOR_NAME,
            connector_version=CONNECTOR_VERSION,
            item_key=raw.item.key,
            observations=(),
            metadata={"row_count": len(rows), "problems": validate_rows(rows)},
            schema_fingerprint=schema_fingerprint(list(COLUMNS)),
        )

    def validate(self, batch: ObservationBatch) -> QualityReport:
        problems = batch.metadata.get("problems") or []
        return QualityReport(
            outcome="blocked" if problems else "pass",
            findings=tuple(Finding(code="incomplete", severity="blocker", message=m) for m in problems[:20]),
            received_count=int(batch.metadata.get("row_count") or 0),
        )

    def next_cursor(self, plan: FetchPlan, outcomes: Sequence[FetchOutcome]) -> Cursor:
        return Cursor(state={"complete": all(o.status in ("published", "unchanged") for o in outcomes)})


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="GeoNames",
        licence=RIGHTS["licence"],
        allowed_hosts=(HOST,),
        datasets=(ATLAS_DATASET,),
        dataset_ids=("countryInfo.txt",),
        cadence_seconds=30 * 86400,
        planned_requests=1,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=module_digest(GeoNamesCountryInfoConnector),
        field_mappings={"geo": "ISO3", "numeric": sorted(NUMERIC), "text": sorted(TEXT)},
        notes="undated provider snapshot; atlas rows dated by retrieval day",
    )


def fetch_items() -> tuple[FetchItem, ...]:
    return (
        FetchItem(
            key="countryinfo",
            url=URL,
            max_bytes=MAX_BYTES,
            expected_media_types=("text/plain", "application/octet-stream"),
        ),
    )


async def _ingest(
    run: Any, *, qids: Mapping[str, str] | None, load_kg: bool
) -> tuple[dict[str, Any], list[CountryInfo], RawAcquisition]:
    import polars as pl

    from reva.adapters.connectors.atlas_geo_common import run_get
    from reva.atlas.tables import ATLAS_SCHEMA

    raw = await run_get(run, fetch_items()[0])
    sha = run.archive(raw)
    rows = parse_country_info(raw.body)
    problems = validate_rows(rows)
    if problems:
        raise _err("; ".join(problems[:10]))
    frame = pl.DataFrame(atlas_rows(rows, raw_sha256=sha, retrieved_at=raw.retrieved_at), schema=ATLAS_SCHEMA)
    table = run.register(
        dataset=ATLAS_DATASET,
        frame=frame,
        raw_sha256s=[sha],
        geo_scheme="iso3",
        rights=RIGHTS,
        coverage_note=f"GeoNames countryInfo.txt: {len(rows)} countries/territories as declared by GeoNames "
        "(undated; retrieval snapshot); codes outside the crosswalk (historical SCG/ANT) kept unmapped",
    )
    kg: dict[str, Any] = {"loaded": False}
    if load_kg:
        kg = load_neighbours_kg(
            run.engine, run.store, rows, raw_sha256=sha, retrieved_at=raw.retrieved_at, origin=run.origin, qids=qids
        )
    return {"connector": CONNECTOR_NAME, "raw_sha256": sha, "table": table, "kg": kg}, rows, raw


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """Single atlas ingest entry point: one request, archive, register ``geonames.countryinfo``, load the
    neighbour statements (P47) when ``registry/entities/country_qids.json`` provides QIDs."""
    summary, _, _ = await _ingest(run, qids=None, load_kg=True)
    return summary


async def ingest(
    engine: Any, store: Any, http: Any, *, qids: Mapping[str, str] | None = None, load_kg: bool = True
) -> dict[str, Any]:
    """Same flow with an explicit client (tests); also returns the parsed rows for a later KG load."""
    from reva.adapters.connectors.atlas_geo_common import make_run

    run = make_run(atlas_source(), engine, store, http)
    summary, rows, raw = await _ingest(run, qids=qids, load_kg=load_kg)
    return {**summary, "rows": rows, "retrieved_at": raw.retrieved_at, "origin": run.origin}


def load_neighbours_kg(
    engine: Any,
    store: Any,
    rows: Sequence[CountryInfo],
    *,
    raw_sha256: str,
    retrieved_at: datetime,
    origin: str,
    qids: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Load GeoNames neighbour statements (P47, no length) through KGCORE's ``load_batch``; blocked without
    an ISO3 -> QID map (``registry/entities/country_qids.json``)."""
    from reva.adapters.connectors.atlas_geo_common import ensure_kg_property, load_country_qids
    from reva.kg.model import load_batch

    q = dict(qids) if qids is not None else load_country_qids()
    if not q:
        return {"loaded": False, "blocked": "registry/entities/country_qids.json (ISO3 -> QID) not available"}
    ensure_kg_property(engine, "P47")
    sts, info = neighbour_statements(rows, q, raw_sha256=raw_sha256, retrieved_at=retrieved_at)
    rep = load_batch(
        engine,
        store,
        statements=sts,
        source_key=SOURCE_KEY,
        raw_sha256s=[raw_sha256],
        retrieved_at=retrieved_at,
        origin=origin,
        parameters={"connector": CONNECTOR_NAME},
    )
    return {"loaded": True, **info, "report": rep.as_dict()}
