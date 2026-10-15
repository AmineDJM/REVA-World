"""End-to-end ingest of the A-SCI atlas connectors (OpenAlex, Nobel, WIPO) — V3, agent A-SCI.

Each ``ingest_*`` coroutine: plan -> fetch every item through ``http`` (a ``SafeHttpClient``, the
budget-metered wrapper of one, ``FixtureHttp`` or ``RecordingHttp``) -> archive the raw bytes as an
``evidence.acquisition`` of the connector (:func:`archive_raw`) -> parse -> ``register_atlas_table``
(and, for Nobel, ``reva.kg.model.load_batch``). Nothing is written when parsing fails: the error is
reported per item and the other items continue. ``origin`` follows ``http.origin``.

No live HTTP client is created in this module (invariant 30: live provider paths inside ``src/reva``
must be budgeted). Callers pass ``http``: the budgeted job path (``MeteredHttp`` once the
A-SCI budget request is merged), ``FixtureHttp`` in tests, or a maintainer's one-off client for a
throwaway database (see docs/dev/v3_a_sci.md).
"""

from __future__ import annotations

import zipfile
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from reva.adapters.connectors import atlas_nobel, atlas_openalex, atlas_wipo
from reva.adapters.connectors.atlas_sci_common import archive_raw, atlas_frame
from reva.kernel.errors import RevaError

ACTOR = "worker:atlas:a_sci"


def _origin(http: Any) -> str:
    return str(getattr(http, "origin", "live"))


def _register(engine: Any, store: Any, **kw: Any) -> dict[str, Any]:
    from reva.atlas.tables import register_atlas_table

    s = register_atlas_table(engine, store, actor=ACTOR, **kw)
    s.pop("coverage", None)
    s["unmapped_provider_geo"] = s.get("unmapped_provider_geo", [])[:50]
    return s


# ----------------------------------------------------------------------------- Nobel
async def ingest_nobel(
    engine: Any, store: Any, http: Any, *, load_kg: bool = True, min_expected: int = atlas_nobel.MIN_EXPECTED_LAUREATES
) -> dict[str, Any]:
    conn = atlas_nobel.NobelAtlasConnector()
    spec = conn.describe()
    item = conn.plan().items[0]
    report: dict[str, Any] = {"connector": spec.name, "origin": _origin(http)}
    raw = await conn.fetch(item, http)
    sha = archive_raw(engine, store, spec, raw, actor=ACTOR)
    records, info = atlas_nobel.parse_laureates(raw.body, min_expected=min_expected)
    report["parse"] = info
    report["unmapped_country_names"] = atlas_nobel.unmapped_names(records)
    frame = atlas_nobel.frame(records, raw_sha256=sha, retrieved_at=raw.retrieved_at)
    report["atlas"] = _register(
        engine,
        store,
        source=atlas_nobel.ATLAS_SOURCE,
        dataset=atlas_nobel.ATLAS_DATASET,
        frame=frame,
        raw_sha256s=[sha],
        parser_version=atlas_nobel.PARSER_VERSION,
        origin=_origin(http),
        geo_scheme="iso3",
        coverage_note=(
            "Counts of laureate-prize records per present-day birth country and per affiliation country "
            "at award, computed by REVA from the complete Nobel API laureate list; Taiwan unmapped."
        ),
        rights=atlas_nobel.RIGHTS,
    )
    if load_kg:
        report["kg"] = load_nobel_kg(engine, store, records, sha, raw.retrieved_at, _origin(http))
    return report


def load_nobel_kg(
    engine: Any, store: Any, records: Sequence[Any], sha: str, retrieved_at: datetime, origin: str
) -> dict[str, Any]:
    """Load the Nobel statements; blocked (typed result, nothing written) if the KG is unavailable."""
    try:
        from reva.kg.model import load_batch
    except ImportError as e:  # pragma: no cover - KG module absent
        return {"status": "blocked", "reason": f"reva.kg.model unavailable: {e}"}
    from sqlalchemy.exc import DBAPIError

    try:
        ensure_kg_properties(engine, tuple(atlas_nobel.KG_PROPERTIES))
    except (RevaError, DBAPIError) as e:
        return {"status": "blocked", "reason": f"KG properties not registrable: {str(getattr(e, 'message', e))[:300]}"}
    stmts = atlas_nobel.kg_statements(records, raw_sha256=sha, retrieved_at=retrieved_at)
    kg = atlas_nobel.to_kg_statements(stmts)
    without_qid = sum(1 for r in records if r.laureate_qid is None)
    try:
        rep = load_batch(
            engine,
            store,
            statements=kg,
            source_key=atlas_nobel.SOURCE_KEY,
            raw_sha256s=[sha],
            retrieved_at=retrieved_at,
            origin=origin,
            parameters={"connector": atlas_nobel.CONNECTOR_NAME, "parser": atlas_nobel.PARSER_VERSION},
        )
    except RevaError as e:
        return {"status": "failed", "error": e.message, "details": e.details}
    d = rep.as_dict()
    d["status"] = "loaded"
    d["statements_built"] = len(kg)
    d["laureate_prizes_without_qid"] = without_qid
    return d


def ensure_kg_properties(engine: Any, pids: Sequence[str]) -> dict[str, int]:
    """Register the allow-listed properties ``pids`` (idempotent; definitions from WDHARVEST's
    ``registry/kg/properties.yaml`` so labels never diverge from the harvest's)."""
    from reva.adapters.connectors.wikidata import load_allowlist
    from reva.kg.harvest import kg_properties
    from reva.kg.model import register_properties

    allow = load_allowlist()
    missing = [p for p in pids if p not in allow]
    if missing:
        raise RevaError(f"properties {missing} are not in registry/kg/properties.yaml")
    props = [p for p in kg_properties(allow) if p.pid in set(pids)]
    return register_properties(engine, props).__dict__


# ----------------------------------------------------------------------------- WIPO
async def ingest_wipo(
    engine: Any,
    store: Any,
    http: Any,
    *,
    zips: Sequence[str] = ("patent", "trademark", "design"),
) -> dict[str, Any]:
    conn = atlas_wipo.WipoIpStatsConnector()
    spec = conn.describe()
    report: dict[str, Any] = {"connector": spec.name, "origin": _origin(http), "tables": {}, "errors": []}
    for item in conn.plan().items:
        key = item.params["zip"]
        if key not in zips:
            continue
        try:
            raw = await conn.fetch(item, http)
            if not raw.body.startswith(b"PK"):
                raise atlas_wipo.WipoParseError(f"{key}: response is not a ZIP ({raw.media_type})", retryable=False)
            sha = archive_raw(engine, store, spec, raw, actor=ACTOR)
            zf = atlas_wipo.open_zip(raw.body)
            if key == "patent":
                report["tech_field_names"] = atlas_wipo.tech_field_names(zf)
            for bf in atlas_wipo.bulk_members(key):
                frame = atlas_wipo.bulk_frame(zf, bf, raw_sha256=sha, retrieved_at=raw.retrieved_at)
                report["tables"][bf.dataset] = _register(
                    engine,
                    store,
                    source=atlas_wipo.ATLAS_SOURCE,
                    dataset=bf.dataset,
                    frame=frame,
                    raw_sha256s=[sha],
                    parser_version=atlas_wipo.PARSER_VERSION,
                    origin=_origin(http),
                    geo_scheme=atlas_wipo.GEO_SCHEME,
                    coverage_note=(
                        f"WIPO bulk file {bf.member} (dated 2023-12-06): {bf.unit} by office x origin x "
                        f"{bf.id_name}; origin '**' and historical codes unmapped; nothing summed."
                    ),
                    rights=atlas_wipo.RIGHTS,
                )
                del frame
        except (RevaError, zipfile.BadZipFile) as e:
            report["errors"].append({"item": item.key, "error": str(getattr(e, "message", e))[:500]})
    return report


async def ingest_gii(engine: Any, store: Any, http: Any) -> dict[str, Any]:
    conn = atlas_wipo.WipoGiiConnector()
    spec = conn.describe()
    item = conn.plan().items[0]
    raw = await conn.fetch(item, http)
    if not raw.body.startswith(b"PK"):
        raise atlas_wipo.WipoParseError(f"GII: response is not an xlsx ({raw.media_type})", retryable=False)
    sha = archive_raw(engine, store, spec, raw, actor=ACTOR)
    rows, info = atlas_wipo.gii_rows(raw.body, raw_sha256=sha, retrieved_at=raw.retrieved_at)
    return {
        "connector": spec.name,
        "origin": _origin(http),
        "economies": info["economies"],
        "atlas": _register(
            engine,
            store,
            source=atlas_wipo.ATLAS_SOURCE,
            dataset=f"gii_{atlas_wipo.GII_EDITION}",
            frame=atlas_frame(rows),
            raw_sha256s=[sha],
            parser_version=atlas_wipo.PARSER_VERSION,
            origin=_origin(http),
            geo_scheme="iso3",
            coverage_note="GII 2025 composite levels (Index, Sub-Index, Pillar, Sub-Pillar): score and rank only.",
            rights=atlas_wipo.GII_RIGHTS,
        ),
    }


# ----------------------------------------------------------------------------- OpenAlex
async def ingest_openalex(
    engine: Any, store: Any, http: Any, connector: atlas_openalex.OpenAlexAtlasConnector, *, max_requests: int = 400
) -> dict[str, Any]:
    """Query every planned group_by; register one table per dataset with the pages that succeeded.

    A query whose pages did not all succeed contributes no rows (a partial country list would read as
    absences); it is listed under ``incomplete``. A provider 429 stops the run (remaining queries
    reported ``not_attempted``)."""
    spec = connector.describe()
    origin = _origin(http)
    rows: dict[str, list[dict[str, Any]]] = {}
    shas: dict[str, list[str]] = {}
    incomplete: list[dict[str, Any]] = []
    requests = 0
    stopped: str | None = None
    retrieved_at = datetime.now(UTC)
    for q in connector.queries:
        if stopped:
            incomplete.append({"query": q.key, "status": "not_attempted", "reason": stopped})
            continue
        pages: list[tuple[atlas_openalex.GroupPage, str, int]] = []
        cursor, pno = "*", 1
        try:
            while True:
                if requests >= max_requests:
                    raise _Stop("request budget for this run used up")
                requests += 1
                raw = await connector.fetch(connector.item(q, cursor, pno), http)
                page = atlas_openalex.parse_group_page(raw.body)
                sha = archive_raw(engine, store, spec, raw, actor=ACTOR)
                retrieved_at = raw.retrieved_at
                pages.append((page, sha, pno))
                if page.next_cursor and len(page.groups) >= atlas_openalex.PER_PAGE and pno < spec.max_pages:
                    cursor, pno = page.next_cursor, pno + 1
                    continue
                if page.next_cursor and len(page.groups) >= atlas_openalex.PER_PAGE:
                    raise atlas_openalex.OpenAlexError("more group pages than max_pages", retryable=False)
                break
        except _Stop as e:
            stopped = str(e)
            incomplete.append({"query": q.key, "status": "not_attempted", "reason": stopped})
            continue
        except RevaError as e:
            status = e.details.get("status")
            incomplete.append({"query": q.key, "status": "failed", "http_status": status, "error": e.message[:300]})
            if status in (429, 401, 403):
                stopped = f"provider refused (HTTP {status}); no further requests this run"
            continue
        rows.setdefault(q.dataset, []).extend(atlas_openalex.atlas_rows(q, pages, retrieved_at=retrieved_at))
        shas.setdefault(q.dataset, []).extend(s for _, s, _ in pages)
    tables = {}
    for ds, rs in rows.items():
        tables[ds] = _register(
            engine,
            store,
            source=atlas_openalex.ATLAS_SOURCE,
            dataset=ds,
            frame=atlas_frame(rs),
            raw_sha256s=sorted(set(shas[ds])),
            parser_version=atlas_openalex.PARSER_VERSION,
            origin=origin,
            geo_scheme=atlas_openalex.GEO_SCHEME,
            coverage_note=(
                "OpenAlex group_by authorships.countries (articles+reviews, full counting) as indexed at "
                "retrieval; '_all_works' and 'unknown' rows kept unmapped."
            ),
            rights=atlas_openalex.RIGHTS,
        )
    return {
        "connector": spec.name,
        "origin": origin,
        "requests": requests,
        "tables": tables,
        "incomplete": incomplete,
        "stopped": stopped,
    }


class _Stop(Exception):
    pass
