"""SourceDiscoverer V1: allowlisted Eurostat catalogue search (no web crawling).

1. Fetch the official Eurostat table of contents (tab-separated text, ~2 MB) with
   one bounded request through the safe HTTP client (explicit ``max_bytes``; an
   oversized catalogue is an error, never silently truncated).
2. Rank datasets by transparent token overlap between the registered concept's
   label and the catalogue title (Jaccard); keep the top ``top_k``.
3. *Inspect* the best candidate(s) with one small filtered JSON-stat request for
   the gap's geography/period (dimension order, categories, unit, status labels).

Catalogue titles and dataset metadata are untrusted *data*: they are sanitised,
length-capped and stored, and nothing in them can change hosts, policies or
tiers. ``allowed_hosts`` of every candidate come from this module's fixed
allowlist.
"""

from __future__ import annotations

import csv
import io
import re
import time
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlencode, urlsplit

from reva.adapters.connectors.base import json_decimal, plain
from reva.adapters.connectors.eurostat_template import DATA_BASE, structure_fingerprint
from reva.autonomy.common import ArtifactIO, AutonomyBlocked, now_utc, stable_id
from reva.autonomy.policy import AutonomyGuard
from reva.kernel.errors import CapabilityGap, RevaError, SourceResponseError
from reva.kernel.ids import sha256_bytes
from reva.ontology.registry import Registry, load_registry
from reva.ports.autonomy import KnowledgeGap, ResourceBudget, SourceCandidate

EUROSTAT_TOC_URL = "https://ec.europa.eu/eurostat/api/dissemination/catalogue/toc/txt?lang=en"
CATALOGUE_ALLOWLIST: dict[str, dict[str, Any]] = {
    "eurostat": {"url": EUROSTAT_TOC_URL, "hosts": ("ec.europa.eu",), "source_key": "eurostat.dissemination"},
}
STOPWORDS = {"on", "by", "and", "the", "of", "in", "at", "for", "a", "an", "to", "as", "per", "eurostat", "wdi"}
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
TOC_COLUMNS = ("title", "code", "type", "last update of data", "last table structure change", "data start",
               "data end", "values")


def sanitise(text: str, limit: int = 500) -> str:
    return _CTRL.sub(" ", text).strip()[:limit]


def tokens(text: str) -> set[str]:
    text = re.sub(r"\([^)]*\)", " ", text.lower())  # parenthetical provenance notes are not semantics
    return {t for t in re.findall(r"[a-z0-9]+", text) if t not in STOPWORDS}


def parse_toc(body: bytes) -> list[dict[str, Any]]:
    """Parse Eurostat TOC text: quoted tab-separated rows; title indentation = folder depth."""
    text = body.decode("utf-8-sig", errors="strict")
    reader = csv.reader(io.StringIO(text), delimiter="\t", quotechar='"')
    header = next(reader, None)
    if header is None or [h.strip().lower() for h in header[: len(TOC_COLUMNS)]] != list(TOC_COLUMNS):
        raise SourceResponseError("Eurostat catalogue header changed", retryable=False, kind="schema_drift",
                                  header=[sanitise(h, 60) for h in (header or [])][:10])
    path: list[str] = []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in reader:
        if len(row) < 3:
            continue
        raw_title, code, kind = row[0], row[1].strip(), row[2].strip()
        depth = (len(raw_title) - len(raw_title.lstrip(" "))) // 4
        title = sanitise(raw_title)
        if kind == "folder":
            path = [*path[:depth], code]
            continue
        if kind not in ("dataset", "table") or code in seen or not re.fullmatch(r"[a-z0-9_]{2,64}", code):
            continue
        seen.add(code)
        def cell(i: int, r: list[str] = row) -> str:
            return sanitise(r[i], 40) if len(r) > i else ""
        out.append({
            "title": title, "code": code, "type": kind, "folder_path": path[:depth],
            "last_update_of_data": cell(3), "last_structure_change": cell(4),
            "data_start": cell(5), "data_end": cell(6), "values": cell(7),
        })
    return out


def score(query: set[str], title: str) -> tuple[float, float]:
    """(recall of concept tokens in the title, Jaccard similarity) — transparent and deterministic."""
    t = tokens(title)
    if not query or not t:
        return 0.0, 0.0
    inter = len(query & t)
    return inter / len(query), inter / len(query | t)


class EurostatCatalogueDiscoverer:
    """Allowlisted catalogue search + bounded structural inspection."""

    def __init__(self, io_: ArtifactIO, http: Any, *, registry: Registry | None = None,
                 guard: AutonomyGuard | None = None, max_toc_bytes: int = 4_000_000, top_k: int = 5,
                 max_inspections: int = 1, max_inspection_attempts: int = 3, min_score: float = 0.6,
                 max_sample_bytes: int = 2_000_000) -> None:
        self.io = io_
        self.http = http
        self.registry = registry or load_registry()
        self.guard = guard
        self.max_toc_bytes = max_toc_bytes
        self.top_k = top_k
        self.max_inspections = max_inspections
        self.max_inspection_attempts = max_inspection_attempts
        self.min_score = min_score
        self.max_sample_bytes = max_sample_bytes
        self.last_search: dict[str, Any] = {}

    # The only hosts discovery may contact; never derived from fetched content.
    @staticmethod
    def _check_allowlisted(url: str) -> None:
        host = (urlsplit(url).hostname or "").lower()
        allowed = {h for c in CATALOGUE_ALLOWLIST.values() for h in c["hosts"]}
        if urlsplit(url).scheme != "https" or host not in allowed:
            raise AutonomyBlocked(f"discovery host {host!r} is not an allowlisted catalogue host")

    def _concept(self, gap: KnowledgeGap) -> Any:
        concept_id = gap.concept_version.split("@", 1)[0]
        try:
            return self.registry.concept(concept_id)
        except KeyError:
            raise CapabilityGap(f"gap concept {concept_id} is not registered") from None

    def _geo_codes(self, gap: KnowledgeGap) -> list[str]:
        codes = []
        for iso3 in gap.geography:
            ent = next((e for e in self.registry.entities if e["key"] == iso3), None)
            alias = next((a for a in (ent or {}).get("aliases", []) if a["provider"] == "eurostat_geo"), None)
            if alias:
                codes.append(str(alias["key"]))
        return codes

    async def discover(self, gap: KnowledgeGap, budget: ResourceBudget) -> Sequence[SourceCandidate]:
        if self.guard is not None:
            self.guard.check("discover")
        started = time.monotonic()
        concept = self._concept(gap)
        query = tokens(concept.labels.get("en", concept.concept_id))
        geo = self._geo_codes(gap)
        years = [int(y) for y in (gap.temporal_support.get("years") or [])][:5]  # type: ignore[union-attr]
        requests = 0
        used_bytes = 0
        if budget.max_requests < 1:
            raise AutonomyBlocked("discovery budget allows no requests")
        url = CATALOGUE_ALLOWLIST["eurostat"]["url"]
        self._check_allowlisted(url)
        limit = min(self.max_toc_bytes, budget.max_bytes)
        res = await self.http.get(url, max_bytes=limit)
        requests += 1
        if len(res.body) > limit:
            raise SourceResponseError("catalogue exceeds byte budget", retryable=False)
        used_bytes += len(res.body)
        toc_ref = self.io.put_bytes(res.body, "raw", "text/plain")
        entries = parse_toc(res.body)
        scored = []
        for e in entries:
            if e["type"] != "dataset":
                continue
            recall, jacc = score(query, e["title"])
            scored.append({**e, "score": round(recall, 6), "jaccard": round(jacc, 6)})
        scored.sort(key=lambda e: (-e["score"], -e["jaccard"], e["code"]))
        ranked = [e for e in scored if e["score"] >= self.min_score][: self.top_k]
        self.last_search = {"query_tokens": sorted(query), "datasets_in_catalogue": len(entries),
                            "ranked": [{"code": e["code"], "score": e["score"], "jaccard": e["jaccard"]}
                                       for e in ranked], "inspections": []}
        source = next(s for s in self.registry.sources if s["source_key"] == "eurostat.dissemination")
        rights = "verified" if (source.get("rights") or {}).get("licence") else "unknown"
        out: list[SourceCandidate] = []
        inspected = attempts = 0
        for entry in ranked:
            inspection: dict[str, Any] | None = None
            inspection_error = None
            if (inspected < self.max_inspections and attempts < self.max_inspection_attempts and geo and years
                    and requests < budget.max_requests and budget.max_bytes - used_bytes > 0):
                attempts += 1
                requests += 1
                try:
                    inspection, nbytes = await self._inspect(
                        entry["code"], geo, years, min(self.max_sample_bytes, budget.max_bytes - used_bytes))
                    used_bytes += nbytes
                    inspected += 1
                except RevaError as e:  # one failed inspection never aborts discovery
                    inspection_error = f"{e.code}: {e.message[:300]}"
                except ValueError as e:
                    inspection_error = f"malformed metadata: {str(e)[:300]}"
                self.last_search["inspections"].append({"code": entry["code"], "ok": inspection is not None,
                                                        "error": inspection_error})
            meta = {
                "schema": "reva-source-candidate/1",
                "provider": "eurostat",
                "gap_id": str(gap.gap_id),
                "requirement_id": gap.requirement_id,
                "catalogue": {"url": url, "raw_sha256": toc_ref.sha256, "bytes": len(res.body),
                              "retrieved_at": res.retrieved_at.isoformat(),
                              "max_bytes_override": limit},
                "entry": entry,
                "match": {"method": "title_token_recall_then_jaccard@1", "query_tokens": sorted(query),
                          "score": entry["score"], "jaccard": entry["jaccard"]},
                "geography": {"iso3": list(gap.geography), "eurostat_geo": geo},
                "years": years,
                "inspection": inspection,
                "inspection_error": inspection_error,
                "lifecycle": "inspected" if inspection else "discovered",
            }
            ref = self.io.put_json(meta, "normalised")
            out.append(SourceCandidate(
                candidate_id=stable_id("candidate", str(gap.gap_id), entry["code"], toc_ref.sha256),
                provider="eurostat",
                catalogue_url=url,
                dataset_id=entry["code"],
                metadata=ref,
                rights_status=rights,  # type: ignore[arg-type]
                allowed_hosts=tuple(CATALOGUE_ALLOWLIST["eurostat"]["hosts"]),
            ))
        self.last_search["usage"] = {"requests": requests, "bytes": used_bytes,
                                     "wall_seconds": round(time.monotonic() - started, 3),
                                     "at": now_utc().isoformat()}
        return out

    @staticmethod
    def inspection_url(dataset: str, geo: list[str], years: list[int]) -> str:
        params = [("format", "JSON"), ("lang", "EN"), *[("geo", g) for g in sorted(geo)],
                  *[("time", str(y)) for y in sorted(years)]]
        return DATA_BASE + dataset + "?" + urlencode(params)

    async def _inspect(self, dataset: str, geo: list[str], years: list[int],
                       max_bytes: int) -> tuple[dict[str, Any], int]:
        url = self.inspection_url(dataset, geo, years)
        self._check_allowlisted(url)
        if max_bytes <= 0:
            raise AutonomyBlocked("discovery byte budget exhausted before inspection")
        res = await self.http.get(url, max_bytes=max_bytes)
        doc = json_decimal(res.body)
        if not isinstance(doc, dict) or str(doc.get("version")) != "2.0":
            raise SourceResponseError("inspection did not return a JSON-stat 2.0 object", retryable=False,
                                      kind="schema_drift")
        raw_ref = self.io.put_bytes(res.body, "raw", "application/json")
        ids = [str(d) for d in doc.get("id", [])]
        dims = doc.get("dimension") or {}
        cats = {}
        for d in ids:
            idx = (dims.get(d) or {}).get("category", {}).get("index") or {}
            order = sorted(idx.items(), key=lambda kv: int(kv[1])) if isinstance(idx, dict) else list(
                enumerate(idx))
            cats[d] = [str(k) for k, _ in order] if isinstance(idx, dict) else [str(v) for _, v in order]
        ext = doc.get("extension") or {}
        inspection = {
            "url": url,
            "raw_sha256": raw_ref.sha256,
            "body_sha256": sha256_bytes(res.body),
            "bytes": len(res.body),
            "retrieved_at": res.retrieved_at.isoformat(),
            "label": sanitise(str(doc.get("label", ""))),
            "jsonstat_version": str(doc.get("version")),
            "dimension_order": ids,
            "size": [int(str(s)) for s in doc.get("size", [])],
            "categories": cats,
            "status_labels": {sanitise(str(k), 4): sanitise(str(v), 80)
                              for k, v in (((ext.get("status") or {}).get("label")) or {}).items()},
            "status_codes_seen": sorted({str(v) for v in (doc.get("status") or {}).values()})
            if isinstance(doc.get("status"), dict) else [],
            "value_count": len(doc.get("value") or {}),
            "dataset_updated": str(doc.get("updated", "")),
            "structure_fingerprint": structure_fingerprint(doc),
            "datastructure": plain(ext.get("datastructure")),
        }
        return inspection, len(res.body)
