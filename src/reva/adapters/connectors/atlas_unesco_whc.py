"""UNESCO World Heritage List (agent KGCULT): official list -> per-country atlas counts.

Source
------
UNESCO publishes the World Heritage List as dataset ``whc001`` on its open-data portal
(Opendatasoft, ``https://data.unesco.org``), licence **CC BY-SA 4.0**, attribution "UNESCO",
update frequency "annual" (the portal re-processes it daily; ``metas.default.modified`` is the
source revision). The dataset metadata links the World Heritage Centre's own "Terms and
Conditions of Use" (``https://whc.unesco.org/en/syndication``); that page and the legacy XML/JSON
exports on ``whc.unesco.org`` sit behind a Cloudflare browser challenge (HTTP 403 to non-browser
clients, checked 2026-10-15), so this connector uses only the open-data portal, whose dataset
record states the licence explicitly. CC BY-SA obligations: attribute "UNESCO World Heritage
Centre / data.unesco.org (whc001)" and share derived tables under the same licence; both are
recorded in :data:`RIGHTS` and on every atlas row's flags.

Two requests per run (both small, one host): the dataset record (revision + licence) and the
JSON export of a fixed column selection (``SELECT_FIELDS``; ~0.8 MB, all ~1,270 properties in one
response, no pagination).

What the counts mean (and do not mean)
--------------------------------------
* A *property* is one entry of the List (``id_no``); serial/transboundary properties count once
  per State Party listed for them (``iso_codes``). Counts are therefore per State Party and do not
  sum to the number of properties.
* The dataset holds the **current** List only: properties removed from it (Arabian Oryx Sanctuary
  2007, Dresden Elbe Valley 2009, Liverpool 2021) are absent, and extensions keep the original
  inscription year in ``date_inscribed``. Per-year counts are therefore "properties *on the List at
  the source revision* first inscribed in year Y", flagged ``excludes_removed_properties``; they
  are not a reconstruction of the List as it stood in year Y.
* A property without an ISO code (the Old City of Jerusalem, "site proposed by Jordan") is kept
  in :func:`unmapped` and contributes to no country; it is never assigned to anyone.
* Every count is a deterministic aggregation of the source's own records, flagged
  ``aggregation: count`` with the selector; nothing is imputed and a country without a property
  simply has no row (a count of zero is not emitted for countries absent from the List, because the
  List does not report on non-State-Parties).
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from reva.adapters.connectors.base import schema_fingerprint, to_raw
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import FetchItem, RawAcquisition

SOURCE_KEY = "unesco.whc"
DATASET = "whc001"
HOST = "data.unesco.org"
PARSER_VERSION = "unesco-whc-parse-1"
CONNECTOR_NAME = "atlas.unesco_whc"
CONNECTOR_VERSION = "1.0.0"

SELECT_FIELDS: tuple[str, ...] = (
    "id_no", "name_en", "name_fr", "date_inscribed", "secondary_dates", "danger", "date_end",
    "danger_list", "cultural_criteria", "natural_criteria", "criteria_txt", "category", "states_names",
    "iso_codes", "region", "transboundary", "area_hectares", "uuid", "components_count",
)
METADATA_URL = f"https://{HOST}/api/explore/v2.1/catalog/datasets/{DATASET}"
EXPORT_URL = f"https://{HOST}/api/explore/v2.1/catalog/datasets/{DATASET}/exports/json?select=" + ",".join(
    SELECT_FIELDS
)
MAX_EXPORT_BYTES = 8_000_000
MAX_METADATA_BYTES = 1_000_000
MIN_EXPECTED_PROPERTIES = 1000  # the List had 1,273 properties at 2026-10-15; fewer than 1,000 = truncation

RIGHTS: dict[str, str] = {
    "licence": "CC BY-SA 4.0",
    "licence_url": "https://creativecommons.org/licenses/by-sa/4.0/",
    "attribution": "UNESCO World Heritage Centre - World Heritage List, data.unesco.org dataset whc001",
    "share_alike": "derived tables must be shared under CC BY-SA 4.0",
    "terms_url": "https://whc.unesco.org/en/syndication",
}

CATEGORIES = ("Cultural", "Natural", "Mixed")
_ROMAN = ("i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x")
_CRIT = re.compile(r"\(([ivx]+)\)")
_YEAR = re.compile(r"^\d{4}$")
_DANGER = re.compile(r"^Y (\d{4})$")


class WhcParseError(SourceResponseError):
    pass


def _err(msg: str) -> WhcParseError:
    return WhcParseError(msg, retryable=False, kind="schema_drift")


@dataclass(frozen=True)
class WhProperty:
    id_no: str
    name_en: str
    name_fr: str | None
    inscribed_year: int
    extension_years: tuple[int, ...]
    category: str
    criteria: tuple[str, ...]
    iso2: tuple[str, ...]
    states_names: tuple[str, ...]
    transboundary: bool
    in_danger: bool
    danger_since: int | None
    region: str | None
    area_hectares: str | None
    uuid: str | None
    record_index: int
    raw_flags: dict[str, Any] = field(default_factory=dict)


# ----------------------------------------------------------------------------- fetch plan


def fetch_items() -> tuple[FetchItem, ...]:
    return (
        FetchItem(key="whc001.metadata", url=METADATA_URL, max_bytes=MAX_METADATA_BYTES),
        FetchItem(key="whc001.export", url=EXPORT_URL, max_bytes=MAX_EXPORT_BYTES),
    )


async def fetch(item: FetchItem, http: Any) -> RawAcquisition:
    result = await http.get(item.url, max_bytes=item.max_bytes)
    return to_raw(item, result, getattr(http, "origin", "live"))


# ----------------------------------------------------------------------------- parsing


def _bool(v: Any, name: str, idx: int) -> bool:
    if v in ("True", True):
        return True
    if v in ("False", False):
        return False
    raise _err(f"record {idx}: {name} is not a boolean ({v!r})")


def _year(v: Any, name: str, idx: int) -> int:
    s = str(v).strip() if v is not None else ""
    if not _YEAR.match(s):
        raise _err(f"record {idx}: {name} is not a 4-digit year ({v!r})")
    return int(s)


def parse_criteria(text: str | None) -> tuple[str, ...]:
    """'(ii)(iv)' -> ('ii', 'iv'); unknown numerals fail (schema drift, never guessed)."""
    if not text:
        return ()
    found = tuple(_CRIT.findall(text))
    leftover = _CRIT.sub("", text).strip()
    if leftover or any(c not in _ROMAN for c in found):
        raise _err(f"unrecognised criteria text {text!r}")
    return found


def parse_metadata(body: bytes) -> dict[str, Any]:
    try:
        doc = json.loads(body)
        meta = doc["metas"]["default"]
    except (ValueError, KeyError, TypeError) as e:
        raise _err("dataset record is not the expected Opendatasoft JSON") from e
    if doc.get("dataset_id") != DATASET:
        raise _err(f"dataset record is for {doc.get('dataset_id')!r}, expected {DATASET}")
    licence = meta.get("license")
    if licence != RIGHTS["licence"]:
        # A licence change is a rights event: block, never publish under assumed rights.
        raise SourceResponseError(
            f"whc001 licence changed to {licence!r} (expected {RIGHTS['licence']})", retryable=False, kind="licence"
        )
    return {
        "modified": meta.get("modified"),
        "data_processed": meta.get("data_processed"),
        "records_count": meta.get("records_count"),
        "licence": licence,
        "licence_url": meta.get("license_url"),
        "publisher": meta.get("publisher"),
        "update_frequency": meta.get("update_frequency"),
    }


def parse_export(body: bytes) -> tuple[list[WhProperty], dict[str, Any]]:
    try:
        rows = json.loads(body)
    except ValueError as e:
        raise _err("export is not JSON") from e
    if not isinstance(rows, list):
        raise _err("export is not a JSON array")
    out: list[WhProperty] = []
    seen: set[str] = set()
    for idx, r in enumerate(rows):
        if not isinstance(r, dict):
            raise _err(f"record {idx} is not an object")
        missing = [f for f in ("id_no", "name_en", "date_inscribed", "category", "iso_codes") if f not in r]
        if missing:
            raise _err(f"record {idx} lacks {missing}")
        id_no = str(r["id_no"]).strip()
        if not id_no.isdigit():
            raise _err(f"record {idx}: id_no {r['id_no']!r} is not numeric")
        if id_no in seen:
            raise _err(f"duplicate id_no {id_no}")
        seen.add(id_no)
        category = r["category"]
        if category not in CATEGORIES:
            raise _err(f"record {idx}: unknown category {category!r}")
        iso2 = tuple(c.strip().upper() for c in (r.get("iso_codes") or "").split(",") if c.strip())
        if any(not re.fullmatch(r"[A-Z]{2}", c) for c in iso2):
            raise _err(f"record {idx}: malformed iso_codes {r.get('iso_codes')!r}")
        ext: list[int] = []
        for piece in str(r.get("secondary_dates") or "").split(","):
            piece = piece.strip()
            if piece:
                ext.append(_year(piece, "secondary_dates", idx))
        inscribed = _year(r["date_inscribed"], "date_inscribed", idx)
        in_danger = _bool(r.get("danger", "False"), "danger", idx)
        danger_since = None
        dl = r.get("danger_list")
        if dl:
            m = _DANGER.match(str(dl).strip())
            danger_since = int(m.group(1)) if m else None
        flags: dict[str, Any] = {}
        if dl and danger_since is None:
            flags["danger_list_unparsed"] = str(dl)
        if not r.get("criteria_txt"):
            # 17 records of the 2026-10-15 export carry no criteria at all (criteria_txt and the
            # cultural_/natural_criteria fields are null): reported as not reported, never inferred.
            flags["criteria_not_reported"] = True
        if r.get("date_end"):
            flags["date_end"] = str(r["date_end"])
        area = r.get("area_hectares")
        out.append(
            WhProperty(
                id_no=id_no,
                name_en=str(r["name_en"]),
                name_fr=r.get("name_fr"),
                inscribed_year=inscribed,
                extension_years=tuple(sorted(set(y for y in ext if y != inscribed))),
                category=category,
                criteria=parse_criteria(r.get("criteria_txt")),
                iso2=iso2,
                states_names=tuple(r.get("states_names") or ()),
                transboundary=_bool(r.get("transboundary", "False"), "transboundary", idx),
                in_danger=in_danger,
                danger_since=danger_since,
                region=r.get("region"),
                area_hectares=None if area is None else format(area, "f") if isinstance(area, float) else str(area),
                uuid=r.get("uuid"),
                record_index=idx,
                raw_flags=flags,
            )
        )
    fields = sorted({k for r in rows if isinstance(r, dict) for k in r})
    return out, {"record_count": len(rows), "fields": fields, "schema_fingerprint": schema_fingerprint(fields)}


def validate_properties(props: Sequence[WhProperty], metadata: Mapping[str, Any] | None = None) -> list[str]:
    """Blocking problems (empty list = pass)."""
    problems = []
    if len(props) < MIN_EXPECTED_PROPERTIES:
        problems.append(f"only {len(props)} properties (< {MIN_EXPECTED_PROPERTIES}): truncated export?")
    if metadata and metadata.get("records_count") is not None and int(metadata["records_count"]) != len(props):
        problems.append(f"export has {len(props)} records but the dataset record says {metadata['records_count']}")
    return problems


# ----------------------------------------------------------------------------- country mapping


def unmapped(props: Iterable[WhProperty]) -> list[dict[str, Any]]:
    """Properties (or State Party codes) that map to no crosswalk entity -- listed, never dropped."""
    from reva.atlas.tables import country_iso3

    out = []
    for p in props:
        if not p.iso2:
            out.append({"id_no": p.id_no, "name_en": p.name_en, "states_names": list(p.states_names),
                        "reason": "no_iso_code"})
        for c in p.iso2:
            if country_iso3(c, "iso2") is None:
                out.append({"id_no": p.id_no, "name_en": p.name_en, "iso2": c, "reason": "iso2_not_in_crosswalk"})
    return out


# ----------------------------------------------------------------------------- atlas rows

ATLAS_SOURCE = "unesco_whc"  # atlas table keys: no dots (reva.atlas.tables.SOURCE_RE)
ATLAS_DATASET = "whc001"
NO_CODE_PREFIX = "states_names:"  # provider_geo of a property listed without any State Party code

#: indicator -> (concept id in registry/atlas/catalog/culture.yaml, predicate over a property)
SNAPSHOT_INDICATORS: dict[str, tuple[str, Any]] = {
    "whc_properties_total": ("culture.whc_properties_total", lambda p: True),
    "whc_properties_cultural": ("culture.whc_properties_cultural", lambda p: p.category == "Cultural"),
    "whc_properties_natural": ("culture.whc_properties_natural", lambda p: p.category == "Natural"),
    "whc_properties_mixed": ("culture.whc_properties_mixed", lambda p: p.category == "Mixed"),
    "whc_properties_in_danger": ("culture.whc_properties_in_danger", lambda p: p.in_danger),
    "whc_properties_transboundary": ("culture.whc_properties_transboundary", lambda p: p.transboundary),
}
YEARLY_INDICATOR = ("whc_properties_inscribed_in_year", "culture.whc_properties_inscribed_in_year")


def provider_geos(p: WhProperty) -> tuple[str, ...]:
    """The provider geographies a property counts for: its ISO alpha-2 State Party codes, or -- when
    the List gives none -- its ``states_names`` verbatim (kept as an unmapped geography)."""
    if p.iso2:
        return p.iso2
    return tuple(f"{NO_CODE_PREFIX}{n}"[:64] for n in p.states_names) or (f"{NO_CODE_PREFIX}(none)",)


def atlas_rows(
    props: Sequence[WhProperty],
    *,
    raw_sha256: str,
    source_revision: str,
    retrieved_at: datetime,
) -> list[dict[str, Any]]:
    """Rows in the ATLASCORE long schema (``reva.atlas.tables.ATLAS_COLUMNS``).

    ``provider_geo`` is the ISO 3166-1 alpha-2 code as published; ``iso3`` is left null and
    filled by ``register_atlas_table(..., geo_scheme="iso2")``. Snapshot counts cover the day
    of the source revision ``[d, d+1)``; per-year counts cover the calendar year of inscription.
    Values are exact integer strings; a geography with no property in a category gets no row
    for that category only when it has no property at all (zeros are reported for State
    Parties that are on the List, because the List itself establishes them).
    """
    rev_day = date.fromisoformat(source_revision[:10])
    snap = (rev_day, rev_day + timedelta(days=1))
    base_flags = {
        "aggregation": "count",
        "unit_of_count": "World Heritage property (id_no) per State Party",
        "excludes_removed_properties": True,
        "source_revision": source_revision,
        "retrieved_at": retrieved_at.isoformat(),
        "licence": RIGHTS["licence"],
        "attribution": RIGHTS["attribution"],
    }
    by_geo: dict[str, list[WhProperty]] = defaultdict(list)
    for p in props:
        for g in provider_geos(p):
            by_geo[g].append(p)
    rows: list[dict[str, Any]] = []
    for geo in sorted(by_geo):
        plist = by_geo[geo]
        for indicator, (concept, pred) in SNAPSHOT_INDICATORS.items():
            ids = sorted((p.id_no for p in plist if pred(p)), key=int)
            rows.append(_row(indicator, concept, geo, snap, len(ids), raw_sha256,
                             {"dataset": DATASET, "geo": geo, "filter": indicator, "id_no": ids}, base_flags))
        per_year: Counter[int] = Counter(p.inscribed_year for p in plist)
        for year in sorted(per_year):
            ids = sorted((p.id_no for p in plist if p.inscribed_year == year), key=int)
            rows.append(_row(YEARLY_INDICATOR[0], YEARLY_INDICATOR[1], geo,
                             (date(year, 1, 1), date(year + 1, 1, 1)), per_year[year], raw_sha256,
                             {"dataset": DATASET, "geo": geo, "field": "date_inscribed", "year": year, "id_no": ids},
                             {**base_flags, "counts_only_properties_on_list_at_revision": True}))
    return rows


def _row(indicator: str, concept: str, geo: str, period: tuple[date, date], n: int, raw_sha256: str,
         selector: dict[str, Any], flags: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": ATLAS_SOURCE,
        "dataset": ATLAS_DATASET,
        "indicator": indicator,
        "concept_id": concept,
        "iso3": None,
        "provider_geo": geo,
        "entity_qid": None,
        "period_start": period[0],
        "period_end": period[1],
        "value": str(n),
        "value_text": None,
        "unit": "count",
        "flags": _canon(flags),
        "raw_sha256": raw_sha256,
        "selector": _canon(selector),
    }


def _canon(obj: Mapping[str, Any]) -> str:
    return json.dumps(dict(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def atlas_frame(rows: Sequence[Mapping[str, Any]]) -> Any:
    """Polars frame with exactly the atlas schema (import kept local: polars is heavy)."""
    import polars as pl

    from reva.atlas.tables import ATLAS_SCHEMA

    return pl.DataFrame(list(rows), schema=ATLAS_SCHEMA)


# ----------------------------------------------------------------------------- connector + ingest

#: ``ConnectorSpec.template`` value shared by the V3 atlas connectors. The ``Literal`` in
#: ``reva.ports.connectors.ConnectorSpec`` has no atlas value yet (change requested from the lead:
#: add ``"atlas_bulk"``); until then every other field is validated with a placeholder template and
#: the template is set to ``atlas_bulk`` afterwards (same approach as the A-SCI connectors).
TEMPLATE = "atlas_bulk"
_PLACEHOLDER_TEMPLATE = "naturalearth_asset"


def template_supported() -> bool:
    from typing import get_args

    from reva.ports.connectors import ConnectorSpec

    return TEMPLATE in get_args(ConnectorSpec.model_fields["template"].annotation)


class UnescoWhcConnector:
    """Archive-only connector: the raw List bytes are published as evidence acquisitions (zero
    observation batches); the counts become an atlas table (:func:`ingest`)."""

    def describe(self) -> Any:
        from reva.adapters.connectors.base import module_digest
        from reva.ports.connectors import ConnectorSpec

        fields: dict[str, Any] = dict(
            name=CONNECTOR_NAME,
            version=CONNECTOR_VERSION,
            source_key=SOURCE_KEY,
            provider="UNESCO World Heritage Centre (data.unesco.org)",
            licence=RIGHTS["licence"],
            allowed_hosts=(HOST,),
            dataset_ids=(DATASET,),
            field_mappings={"geo": "iso_codes (ISO 3166-1 alpha-2)", "id": "id_no", "year": "date_inscribed"},
            units={"*": "count"},
            geography=("world",),
            time_semantics="current List at the dataset revision; inscription year per property",
            lineage_family="unesco_whc",
            cadence_seconds=7 * 86400,
            revision_strategy="dataset record 'modified' is the source revision; identical bytes are a no-op",
            max_response_bytes=MAX_EXPORT_BYTES,
            max_pages=1,
            parser_version=PARSER_VERSION,
            code_digest=module_digest(UnescoWhcConnector),
        )
        if template_supported():
            return ConnectorSpec(template=TEMPLATE, **fields)  # type: ignore[arg-type]
        checked = ConnectorSpec(template=_PLACEHOLDER_TEMPLATE, **fields)  # type: ignore[arg-type]  # validates the rest
        return checked.model_copy(update={"template": TEMPLATE})

    def plan(self, cursor: Any, budget: Any) -> Any:
        from reva.ports.connectors import FetchPlan

        return FetchPlan(connector=CONNECTOR_NAME, items=fetch_items(), pagination="none (single JSON export)")

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition:
        return await fetch(item, http)

    def parse(self, raw: RawAcquisition) -> Any:
        from reva.ports.connectors import ObservationBatch

        if raw.item.key == "whc001.metadata":
            meta = parse_metadata(raw.body)
            yield ObservationBatch(connector=CONNECTOR_NAME, connector_version=CONNECTOR_VERSION,
                                   item_key=raw.item.key, observations=(), metadata={"dataset": meta})
            return
        props, info = parse_export(raw.body)
        yield ObservationBatch(connector=CONNECTOR_NAME, connector_version=CONNECTOR_VERSION, item_key=raw.item.key,
                               observations=(), metadata={"record_count": info["record_count"],
                                                          "problems": validate_properties(props)},
                               schema_fingerprint=info["schema_fingerprint"])

    def validate(self, batch: Any) -> Any:
        from reva.ports.connectors import Finding, QualityReport

        problems = batch.metadata.get("problems") or []
        return QualityReport(
            outcome="blocked" if problems else "pass",
            findings=tuple(Finding(code="incomplete", severity="blocker", message=m) for m in problems),
            received_count=int(batch.metadata.get("record_count") or 0),
        )

    def next_cursor(self, plan: Any, outcomes: Sequence[Any]) -> Any:
        from reva.ports.connectors import Cursor

        return Cursor(state={"complete": all(o.status in ("published", "unchanged") for o in outcomes)})


async def ingest(engine: Any, store: Any, http: Any) -> dict[str, Any]:
    """Fetch (2 requests), archive both raw responses, register the atlas table ``unesco_whc.whc001``.

    ``http`` is a budget-metered SafeHttpClient for live runs or ``FixtureHttp`` (origin test_fixture).
    Blocks (raises) on schema drift, licence change or a truncated export; nothing is registered then.
    """
    from reva.atlas.tables import register_atlas_table
    from reva.evidence.ingest import publish_acquisition

    conn = UnescoWhcConnector()
    spec = conn.describe()
    raws = {it.key: await conn.fetch(it, http) for it in fetch_items()}
    meta = parse_metadata(raws["whc001.metadata"].body)
    props, _ = parse_export(raws["whc001.export"].body)
    problems = validate_properties(props, meta)
    if problems:
        raise WhcParseError("; ".join(problems), retryable=False, kind="incomplete")
    shas = []
    for raw in raws.values():
        summary = publish_acquisition(engine, store, spec, raw, [], [], actor="worker:atlas")
        if summary.namespace == "quarantine":  # pragma: no cover - no quality report is passed
            raise WhcParseError("raw acquisition was quarantined", retryable=False)
        shas.append(summary.raw_sha256)
    export_sha = shas[list(raws).index("whc001.export")]
    rows = atlas_rows(props, raw_sha256=export_sha, source_revision=str(meta["modified"]),
                      retrieved_at=raws["whc001.export"].retrieved_at)
    origin = raws["whc001.export"].origin
    return register_atlas_table(
        engine, store, source=ATLAS_SOURCE, dataset=ATLAS_DATASET, frame=atlas_frame(rows), raw_sha256s=shas,
        parser_version=PARSER_VERSION, origin=origin, geo_scheme="iso2", rights=RIGHTS,
        coverage_note=(f"UNESCO World Heritage List (whc001, revision {meta['modified']}): {len(props)} properties; "
                       "counts per State Party; removed properties absent; 1 property without State Party code "
                       "kept unmapped"),
    )
