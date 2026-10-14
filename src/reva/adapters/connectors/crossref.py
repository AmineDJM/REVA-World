"""Crossref DOI metadata connector (template crossref_doi).

Metadata is not full text and not causal validation. Honours X-Rate-Limit and
concurrency headers via the shared rate limiter and 429/Retry-After handling.

1.1.0: conditional requests. The ETag / Last-Modified validators of each DOI's
last response are kept in the connector cursor and sent as If-None-Match /
If-Modified-Since; a 304 is recorded as "unchanged" (no acquisition).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any
from urllib.parse import quote

from reva.adapters.connectors.base import json_decimal, module_digest, plain, schema_fingerprint, to_raw
from reva.adapters.connectors.worldbank import require_json_like
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import (
    ConnectorSpec,
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

CURATED_DOIS = ("10.1257/aer.99.3.1053",)
CONNECTOR_VERSION = "1.1.0"


class CrossrefConnector:
    def __init__(self, dois: Sequence[str] = CURATED_DOIS, mailto: str | None = None) -> None:
        self.dois = tuple(d.lower() for d in dois)
        self.mailto = mailto
        self._validators: dict[str, dict[str, str]] = {}  # doi -> {etag, last_modified} seen this run

    def describe(self) -> ConnectorSpec:
        return ConnectorSpec(
            name="crossref.works.doi",
            version=CONNECTOR_VERSION,
            template="crossref_doi",
            source_key="crossref.works",
            provider="Crossref",
            licence="CC0 metadata",
            allowed_hosts=("api.crossref.org",),
            dataset_ids=self.dois,
            field_mappings={"study": "evidence.study"},
            units={},
            geography=(),
            time_semantics="bibliographic dates as reported; update/retraction relations retained",
            lineage_family="crossref",
            cadence_seconds=7 * 86400,
            revision_strategy="new study metadata record when metadata changes",
            max_response_bytes=2_000_000,
            max_pages=1,
            parser_version="crossref-parse-1",
            code_digest=module_digest(CrossrefConnector),
        )

    def plan(self, cursor: Cursor | None, budget: FetchBudget) -> FetchPlan:
        suffix = f"?mailto={quote(self.mailto)}" if self.mailto else ""
        known = dict((cursor.state if cursor else {}).get("validators") or {})
        self._validators = {d: dict(v) for d, v in known.items() if d in self.dois and isinstance(v, dict)}
        items = tuple(
            FetchItem(key=f"doi:{d}", url=f"https://api.crossref.org/works/{quote(d, safe='/.')}{suffix}",
                      max_bytes=2_000_000,
                      params={"doi": d, **{k: str(v) for k, v in known.get(d, {}).items()
                                           if k in ("etag", "last_modified") and v}})
            for d in self.dois
        )
        return FetchPlan(connector="crossref.works.doi", items=items[: budget.max_requests], pagination="none")

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition:
        headers: dict[str, str] = {}
        if item.params.get("etag"):
            headers["If-None-Match"] = str(item.params["etag"])
        if item.params.get("last_modified"):
            headers["If-Modified-Since"] = str(item.params["last_modified"])
        if headers:
            result = await http.get(item.url, max_bytes=item.max_bytes, headers=headers, accept_status=(200, 304))
        else:
            result = await http.get(item.url, max_bytes=item.max_bytes)
        raw = to_raw(item, result, getattr(http, "origin", "live"))
        hdrs = {k.lower(): v for k, v in (raw.response_metadata.get("headers") or {}).items()}
        if raw.response_metadata.get("status") == 200:
            v = {k: hdrs[h] for k, h in (("etag", "etag"), ("last_modified", "last-modified")) if hdrs.get(h)}
            if v:
                self._validators[str(item.params["doi"])] = v
        return raw

    def parse(self, raw: RawAcquisition) -> Iterator[ObservationBatch]:
        if raw.response_metadata.get("status") == 304:
            yield ObservationBatch(connector="crossref.works.doi", connector_version=CONNECTOR_VERSION,
                                   item_key=raw.item.key, observations=(), metadata={"not_modified": True})
            return
        require_json_like(raw)
        payload = json_decimal(raw.body)
        if payload.get("status") != "ok" or payload.get("message-type") != "work":
            raise SourceResponseError("unexpected Crossref response", retryable=False, kind="schema_drift")
        m = payload["message"]
        study = {
            "doi": str(m.get("DOI", "")).lower(),
            "title": (m.get("title") or [None])[0],
            "authors": [
                {"given": a.get("given"), "family": a.get("family"), "ORCID": a.get("ORCID")}
                for a in m.get("author", [])
            ],
            "container_title": (m.get("container-title") or [None])[0],
            "volume": m.get("volume"),
            "issue": m.get("issue"),
            "page": m.get("page"),
            "published": plain((m.get("published") or {}).get("date-parts")),
            "type": m.get("type"),
            "publisher": m.get("publisher"),
            "url": m.get("URL"),
            "license": plain(m.get("license")),
            "update_to": plain(m.get("update-to")),
            "updated_by": plain(m.get("updated-by")),
            "relation": plain(m.get("relation")),
            "is_referenced_by_count": plain(m.get("is-referenced-by-count")),
            "crossref_indexed": plain((m.get("indexed") or {}).get("date-time")),
            "note": "Citation counts are ranking signals only, never effect validity.",
        }
        yield ObservationBatch(connector="crossref.works.doi", connector_version=CONNECTOR_VERSION,
                               item_key=raw.item.key,
                               observations=(), metadata={"study": study},
                               schema_fingerprint=schema_fingerprint({k: m.get(k) for k in sorted(m)[:40]}))

    def validate(self, batch: ObservationBatch) -> QualityReport:
        if batch.metadata.get("not_modified"):
            return QualityReport(outcome="pass")
        study = batch.metadata.get("study", {})
        findings = []
        if study.get("doi") not in self.dois:
            findings.append(Finding(code="identifier_mismatch", severity="blocker", message="DOI mismatch"))
        if not study.get("title"):
            findings.append(Finding(code="missing_title", severity="blocker", message="no title"))
        if study.get("updated_by"):
            findings.append(Finding(code="update_signal", severity="warning",
                                    message="Crossref reports an update/correction/retraction relation"))
        return QualityReport(outcome="blocked" if any(f.severity == "blocker" for f in findings) else "pass",
                             findings=tuple(findings))

    def next_cursor(self, plan: FetchPlan, outcomes: Sequence[FetchOutcome]) -> Cursor:
        return Cursor(state={"complete": all(o.status in ("published", "unchanged") for o in outcomes),
                             "validators": self._validators})
