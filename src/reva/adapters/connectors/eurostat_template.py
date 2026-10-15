"""Approved, pre-audited Eurostat JSON-stat 2.0 template (template id ``eurostat_jsonstat``).

This module is reviewed code. A *declarative manifest* (contracts/connector.schema.json,
``generation_mode: declarative_only``) parameterises it; no generated code is ever
executed. The manifest can narrow what the template does (dataset, filters, pinned
dimension order/categories, semantic support) but can never widen it: hosts are fixed
to ``TEMPLATE_HOSTS`` and every identifier is pattern-checked.

Semantics preserved from the provider:

* dimension order is pinned; a different order/unit/frequency is schema drift;
* observation status flags (``p`` provisional, ``e`` estimated, ``b`` break, ...) are
  kept verbatim in ``source_flags`` with their declared labels;
* a position absent from the JSON-stat ``value`` map is a typed Missing (never zero);
* support is declared by the manifest (e.g. 1 January point support, point-in-time
  stock, Eurostat usually-resident population) and resolution dimensions (age, sex)
  are part of the support; each cell records ``cell_type`` leaf/subtotal/total so
  totals are never added to disjoint leaf cells downstream;
* the dataset-level ``updated`` timestamp is metadata, never a per-value publication
  time.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import math
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from reva.adapters.connectors.base import json_decimal, plain, schema_fingerprint, to_raw
from reva.adapters.http.safe_client import HttpResult, require_json
from reva.kernel.errors import SourceResponseError, ValidationProblem
from reva.kernel.support import PopulationRef, SpatialRef, Support
from reva.kernel.time import TimeExtent
from reva.kernel.uncertainty import UncertaintySpec
from reva.ports.connectors import (
    ConnectorSpec,
    Cursor,
    FetchBudget,
    FetchItem,
    FetchOutcome,
    FetchPlan,
    Finding,
    ObservationBatch,
    ParsedObservation,
    QualityReport,
    RawAcquisition,
)

TEMPLATE_ID = "eurostat_jsonstat"
TEMPLATE_HOSTS: tuple[str, ...] = ("ec.europa.eu",)
DATA_BASE = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"
PARSER_VERSION = "eurostat-jsonstat-1"
SCHEMA_PATH = Path(__file__).resolve().parents[4] / "contracts" / "connector.schema.json"
MAX_POSITIONS = 200_000  # bounded parse; a manifest must filter to a small cube

# Eurostat observation status (OBS_FLAG) vocabulary. Unknown codes are a blocker:
# their meaning is not reviewed.
EUROSTAT_FLAGS: dict[str, str] = {
    "b": "break in time series",
    "c": "confidential",
    "d": "definition differs",
    "e": "estimated",
    "f": "forecast",
    "n": "not significant",
    "p": "provisional",
    "r": "revised",
    "s": "Eurostat estimate",
    "u": "low reliability",
    "z": "not applicable",
}
MISSING_REASON_BY_FLAG = {"c": "suppressed", "z": "not_applicable"}

# Identifiers are checked with ``fullmatch`` (R6-11): Python's ``$`` also matches
# before a trailing newline, so ``re.match(r"...$")`` would accept ``"FR\n"``.
_DATASET_RE = re.compile(r"[a-z0-9_]{2,64}")
_CODE_RE = re.compile(r"[A-Za-z0-9_\-]{1,40}")
_DIM_RE = re.compile(r"[a-z0-9_]{1,32}")
_ALIAS_RE = re.compile(r"[a-z0-9_]{1,40}")
_REF_RE = re.compile(r"[a-z0-9_.]{1,80}")
_NAME_RE = re.compile(r"[a-z][a-z0-9_.-]{1,127}")
# Canonical semantic version (R6-05): no leading zeros, no suffix, no whitespace.
SEMVER_RE = re.compile(r"(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})")
_TEMPORAL_RE = re.compile(r"point:(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])")
_AGGREGATIONS = {"point_in_time_stock", "period_average_stock", "period_flow_total", "period_rate",
                 "category_cell"}
# Logical keys use a strict placeholder language (never ``str.format``): ``{name}``
# where name is ``dataset`` or a pinned dimension id; bounded template and output.
_KEY_TEMPLATE_RE = re.compile(r"^eurostat:\{dataset\}(?::\{[a-z0-9_]{1,32}\}){1,12}$")
_PLACEHOLDER_RE = re.compile(r"\{([a-z0-9_]{1,32})\}")
MAX_KEY_TEMPLATE_CHARS = 256
MAX_LOGICAL_KEY_CHARS = 512
DEFAULT_KEY_TEMPLATE = "eurostat:{dataset}:{geo}:{time}"
# Structural errors a malformed provider document can raise inside parsing; they
# are converted to a typed, non-retryable ``schema_drift`` SourceResponseError.
_MALFORMED = (ValueError, TypeError, AttributeError, KeyError, IndexError, RecursionError, ArithmeticError)


@lru_cache(maxsize=1)
def connector_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


# (R6-07) The template's behaviour depends on every reva module it imports (directly or
# transitively: base.json_decimal, safe_client.require_json, kernel support/time/uncertainty,
# ...) and on the connector schema. The pinned digest covers the bytes of that whole import
# closure, enumerated deterministically from the import statements of the loaded files.
TEMPLATE_MODULE = __name__
DIGEST_DATA_FILES: tuple[Path, ...] = (SCHEMA_PATH,)


def _reva_imports(source: bytes) -> list[str]:
    """``reva.*`` modules named by any import statement (module level or lazy) of ``source``."""
    out: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out += [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
        elif isinstance(node, ast.Import):
            out += [a.name for a in node.names]
    return [n for n in out if n == "reva" or n.startswith("reva.")]


def _module_file(name: str) -> str | None:
    mod = sys.modules.get(name)
    origin = getattr(mod, "__file__", None) if mod is not None else None
    if origin is None:
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, AttributeError, ValueError):  # e.g. ``pkg.module.function``
            return None
        origin = spec.origin if spec is not None else None
    return origin if origin and origin.endswith(".py") else None


@lru_cache(maxsize=1)
def template_closure() -> tuple[tuple[str, str], ...]:
    """Sorted ``(module or data file, sha256 of its bytes)`` for the template's reviewed closure."""
    seen: dict[str, str] = {}
    todo = [TEMPLATE_MODULE]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        path = _module_file(name)
        if path is None:
            continue
        data = Path(path).read_bytes()
        seen[name] = hashlib.sha256(data).hexdigest()
        todo.extend(_reva_imports(data))
    for f in DIGEST_DATA_FILES:
        seen[f"file:{f.name}"] = hashlib.sha256(f.read_bytes()).hexdigest()
    return tuple(sorted(seen.items()))


def _rebound_imports() -> list[str]:
    """Names this module imported from reva modules that no longer are those modules' objects."""
    out = []
    for node in ast.parse(Path(__file__).read_bytes()).body:
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("reva.") and node.level == 0:
            src = sys.modules.get(node.module)
            for a in node.names:
                local = a.asname or a.name
                if src is None or globals().get(local) is not getattr(src, a.name, None):
                    out.append(f"{node.module}.{a.name}")
    return sorted(out)


def template_digest() -> str:
    """Digest of the reviewed template *and its whole reva import closure* (what the policy pins).

    ``sha256`` over the sorted ``module:sha256(file bytes)`` lines of the closure plus the
    connector schema. If a name the template imported was re-bound at runtime (monkey
    patching), the digest includes that fact and can no longer equal the pin.
    """
    lines = [f"{k}:{v}" for k, v in template_closure()]
    lines += [f"rebound:{n}" for n in _rebound_imports()]
    return "sha256:" + hashlib.sha256("\n".join(lines).encode()).hexdigest()


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    data = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode()).hexdigest()


def validate_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Schema + template-specific checks. Raises ValidationProblem on any violation."""
    import jsonschema

    m = json.loads(json.dumps(manifest))  # detach; JSON types only
    try:
        jsonschema.Draft202012Validator(connector_schema()).validate(m)
    except jsonschema.ValidationError as e:
        raise ValidationProblem(f"connector manifest violates schema: {e.message}",
                                path=list(map(str, e.absolute_path))) from None
    if m["template"] != TEMPLATE_ID:
        raise ValidationProblem(f"template {m['template']!r} is not {TEMPLATE_ID}")
    if not set(m["allowed_hosts"]) <= set(TEMPLATE_HOSTS):
        raise ValidationProblem("manifest hosts exceed the template's fixed hosts",
                                hosts=sorted(set(m["allowed_hosts"]) - set(TEMPLATE_HOSTS)))
    if not isinstance(m["dataset"], str) or not _DATASET_RE.fullmatch(m["dataset"]):
        raise ValidationProblem("invalid Eurostat dataset code")
    if not isinstance(m["name"], str) or not _NAME_RE.fullmatch(m["name"]):
        raise ValidationProblem("invalid connector name")
    if not isinstance(m["version"], str) or not SEMVER_RE.fullmatch(m["version"]):
        raise ValidationProblem("connector version must be a canonical MAJOR.MINOR.PATCH string")
    rp = m["request_parameters"]
    for key in ("filters", "time", "dimension_order", "pinned_categories", "semantics", "subject",
                "resolution_dimensions", "total_categories"):
        if key not in rp:
            raise ValidationProblem(f"request_parameters.{key} is required by {TEMPLATE_ID}")
    order = rp["dimension_order"]
    if not (isinstance(order, list) and order and all(isinstance(d, str) and _DIM_RE.fullmatch(d) for d in order)):
        raise ValidationProblem("dimension_order must be a non-empty list of dimension ids")
    if len(set(order)) != len(order) or "time" not in order:
        raise ValidationProblem("dimension_order must be unique and contain 'time'")
    for dim, codes in {**rp["filters"], **rp["pinned_categories"]}.items():
        if dim not in order or not isinstance(codes, list) or not codes:
            raise ValidationProblem(f"filter/pin on unknown or empty dimension {dim!r}")
        if not all(isinstance(c, str) and _CODE_RE.fullmatch(c) for c in codes):
            raise ValidationProblem(f"invalid category code in {dim!r}")
    years = rp["time"]
    if not (isinstance(years, list) and 1 <= len(years) <= 30
            and all(isinstance(y, int) and 1900 <= y <= 2100 for y in years)):
        raise ValidationProblem("time must be a list of 1..30 integer years")
    for dim in rp["resolution_dimensions"]:
        if dim not in order or dim == "time":
            raise ValidationProblem(f"resolution dimension {dim!r} not in cube")
    sem = rp["semantics"]
    for key in ("concept_id", "concept_version", "unit_ref", "aggregation", "population_universe",
                "temporal_support", "spatial_scheme", "unit_category"):
        if not isinstance(sem.get(key), str) or not sem[key]:
            raise ValidationProblem(f"semantics.{key} is required")
    if sem["aggregation"] not in _AGGREGATIONS:
        raise ValidationProblem("unsupported aggregation")
    if not _TEMPORAL_RE.fullmatch(sem["temporal_support"]):
        raise ValidationProblem("template supports point:MM-DD temporal support only")
    if not _REF_RE.fullmatch(sem["concept_id"]) or not _REF_RE.fullmatch(sem["unit_ref"]):
        raise ValidationProblem("invalid concept/unit reference")
    subj = rp["subject"]
    if subj.get("dimension") not in order or not _ALIAS_RE.fullmatch(str(subj.get("alias_provider", ""))):
        raise ValidationProblem("subject must name a cube dimension and an alias provider")
    flags = rp.get("status_vocabulary", list(EUROSTAT_FLAGS))
    if not set(flags) <= set(EUROSTAT_FLAGS):
        raise ValidationProblem("status vocabulary contains unreviewed flags")
    check_logical_key_template(rp.get("logical_key_template", DEFAULT_KEY_TEMPLATE), order)
    return m


def canonical_logical_key_template(manifest: Mapping[str, Any]) -> str:
    """The logical-key template this reviewed code derives from a manifest's structure.

    ``eurostat:{dataset}:{<subject dim>}:{<resolution dims by (category count, id)>}:{time}``.
    Gates and generators compare/emit this; a manifest cannot choose another layout.
    """
    rp = manifest["request_parameters"]
    pinned = rp["pinned_categories"]
    res = sorted(rp["resolution_dimensions"], key=lambda d: (len(pinned.get(d) or []), d))
    dims = [rp["subject"]["dimension"], *res, "time"]
    return "eurostat:{dataset}:" + ":".join(f"{{{d}}}" for d in dims)


def check_logical_key_template(template: Any, order: Sequence[str]) -> str:
    if not isinstance(template, str) or len(template) > MAX_KEY_TEMPLATE_CHARS \
            or not _KEY_TEMPLATE_RE.fullmatch(template):
        raise ValidationProblem("logical_key_template must be 'eurostat:{dataset}:{dim}:...' with plain "
                                "{dimension} placeholders only")
    names = _PLACEHOLDER_RE.findall(template)
    unknown = [n for n in names if n != "dataset" and n not in order]
    if unknown:
        raise ValidationProblem("logical_key_template names unknown dimensions", unknown=unknown)
    if len(set(names)) != len(names) or "time" not in names:
        raise ValidationProblem("logical_key_template placeholders must be unique and include {time}")
    return template


def render_logical_key(template: str, parts: Mapping[str, str]) -> str:
    """Strict substitution of ``{name}`` placeholders (no attribute/index access, no format specs)."""
    def sub(m: re.Match[str]) -> str:
        v = parts.get(m.group(1))
        if v is None:
            raise SourceResponseError(f"logical key placeholder {m.group(1)!r} has no value", retryable=False,
                                      kind="schema_drift")
        return str(v)

    out = _PLACEHOLDER_RE.sub(sub, template)
    if len(out) > MAX_LOGICAL_KEY_CHARS:
        raise SourceResponseError("logical key exceeds the template bound", retryable=False, kind="schema_drift")
    return out


def _positions(index: Any, size: int, dim: str) -> list[str]:
    """Category codes ordered by position (JSON-stat index may be an object or array)."""
    if isinstance(index, list):
        codes = [str(c) for c in index]
    elif isinstance(index, dict):
        pairs = sorted(((int(str(v)), str(k)) for k, v in index.items()), key=lambda p: p[0])
        if [p for p, _ in pairs] != list(range(len(pairs))):
            raise SourceResponseError(f"dimension {dim} index positions are not 0..n-1",
                                      retryable=False, kind="schema_drift")
        codes = [k for _, k in pairs]
    else:
        raise SourceResponseError(f"dimension {dim} has no category index", retryable=False, kind="schema_drift")
    if len(codes) != size:
        raise SourceResponseError(f"dimension {dim}: {len(codes)} categories but size {size}",
                                  retryable=False, kind="schema_drift")
    return codes


def _sparse(obj: Any, n: int, what: str) -> dict[int, Any]:
    """JSON-stat value/status as {position: item}; arrays with null are sparse too."""
    if obj is None:
        return {}
    if isinstance(obj, list):
        if len(obj) != n:
            raise SourceResponseError(f"{what} array length {len(obj)} != cube size {n}",
                                      retryable=False, kind="schema_drift")
        return {i: v for i, v in enumerate(obj) if v is not None}
    if isinstance(obj, dict):
        out: dict[int, Any] = {}
        for k, v in obj.items():
            try:
                i = int(str(k))
            except ValueError:
                raise SourceResponseError(f"non-integer {what} key", retryable=False, kind="schema_drift") from None
            if not 0 <= i < n:
                raise SourceResponseError(f"{what} position {i} outside cube of size {n}",
                                          retryable=False, kind="schema_drift")
            out[i] = v
        return out
    if isinstance(obj, str) and what == "status":
        return dict.fromkeys(range(n), obj)
    raise SourceResponseError(f"unexpected JSON-stat {what} type", retryable=False, kind="schema_drift")


def structure_fingerprint(doc: Mapping[str, Any]) -> str:
    """Order-preserving for dimensions (semantic in JSON-stat), order-free for keys."""
    dims = doc.get("dimension") or {}
    shape = {
        "dimension_order": list(doc.get("id") or []),
        "categories": {
            d: sorted(dims.get(d, {}).get("category", {}).get("index") or {})
            for d in (doc.get("id") or [])
            if d != "time"
        },
        "value_type": "number",
        "top_level": schema_fingerprint({k: v for k, v in doc.items() if k not in ("value", "status")}),
    }
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()


class EurostatJsonStatConnector:
    """Generic JSON-stat 2.0 connector parameterised by a validated declarative manifest."""

    def __init__(self, manifest: Mapping[str, Any]) -> None:
        self.manifest = validate_manifest(manifest)
        rp = self.manifest["request_parameters"]
        self.rp = rp
        self.dataset: str = self.manifest["dataset"]
        self.order: list[str] = list(rp["dimension_order"])
        self.sem: dict[str, Any] = rp["semantics"]
        self.flags_vocab = set(rp.get("status_vocabulary", list(EUROSTAT_FLAGS)))
        self.total_categories: dict[str, list[str]] = rp["total_categories"]
        self.special: dict[str, dict[str, str]] = rp.get("special_categories", {})
        self.years: list[int] = sorted(rp["time"])

    # ------------------------------------------------------------------ describe/plan
    def describe(self) -> ConnectorSpec:
        m = self.manifest
        return ConnectorSpec(
            name=m["name"],
            version=m["version"],
            template=TEMPLATE_ID,
            source_key=m["source_key"],
            provider="Eurostat",
            licence=str(self.rp.get("licence", m["rights_ref"])),
            allowed_hosts=tuple(m["allowed_hosts"]),
            dataset_ids=(self.dataset,),
            field_mappings={
                "manifest": m,
                "manifest_sha256": manifest_digest(m),
                "concept": f"{self.sem['concept_id']}@{self.sem['concept_version']}",
            },
            units={self.sem["unit_category"]: self.sem["unit_ref"]},
            geography=tuple(self.rp["filters"].get(self.rp["subject"]["dimension"], [])),
            time_semantics=(f"{self.sem['temporal_support']} point support, {self.sem['aggregation']}, "
                            f"universe {self.sem['population_universe']}"),
            lineage_family=m["lineage_family"],
            cadence_seconds=int(m["cadence"]["minimum_interval_seconds"]),
            release_aware=bool(m["cadence"]["release_aware"]),
            revision_strategy="append revision when value or status flag changes (e.g. provisional -> final)",
            max_response_bytes=int(m["max_response_bytes"]),
            max_pages=int(m["max_pages"]),
            parser_version=PARSER_VERSION,
            code_digest=template_digest(),
        )

    def data_url(self) -> str:
        params: list[tuple[str, str]] = [("format", "JSON"), ("lang", str(self.rp.get("lang", "EN")))]
        for dim in sorted(self.rp["filters"]):
            for code in self.rp["filters"][dim]:
                params.append((dim, code))
        for y in self.years:
            params.append(("time", str(y)))
        return DATA_BASE + self.dataset + "?" + urlencode(params)

    def plan(self, cursor: Cursor | None, budget: FetchBudget) -> FetchPlan:
        item = FetchItem(
            key=f"jsonstat:{self.dataset}",
            url=self.data_url(),
            expected_media_types=("application/json",),
            max_bytes=int(self.manifest["max_response_bytes"]),
            params={"kind": "jsonstat", "dataset": self.dataset},
        )
        items = (item,) if budget.max_requests >= 1 else ()
        return FetchPlan(
            connector=self.manifest["name"],
            items=items,
            pagination="none: one filtered JSON-stat cube per request",
            availability_expectation="Eurostat dataset-level UPDATE_DATA; no per-value publication time",
            budget_exhausted=not items,
        )

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition:
        result = await http.get(item.url, max_bytes=item.max_bytes)
        return to_raw(item, result, getattr(http, "origin", "live"))

    # ------------------------------------------------------------------ parse
    def _doc(self, raw: RawAcquisition) -> dict[str, Any]:
        try:
            return self._doc_unchecked(raw)
        except SourceResponseError:
            raise
        except _MALFORMED as e:
            raise SourceResponseError(f"malformed JSON-stat document: {type(e).__name__}", retryable=False,
                                      kind="schema_drift") from None

    def _doc_unchecked(self, raw: RawAcquisition) -> dict[str, Any]:
        require_json(HttpResult(raw.item.url, raw.item.url, 200, {"content-type": raw.media_type}, raw.body,
                                raw.retrieved_at, 0.0, (), 1))
        doc = json_decimal(raw.body)
        if not isinstance(doc, dict):
            raise SourceResponseError("JSON-stat document is not an object", retryable=False, kind="schema_drift")
        if "error" in doc and "value" not in doc:
            raise SourceResponseError(f"Eurostat API error: {str(plain(doc['error']))[:300]}", retryable=False,
                                      kind="api_error")
        if str(doc.get("version")) != "2.0" or doc.get("class") != "dataset":
            raise SourceResponseError("not a JSON-stat 2.0 dataset", retryable=False, kind="schema_drift")
        ids, sizes = doc.get("id"), doc.get("size")
        if not (isinstance(ids, list) and isinstance(sizes, list) and len(ids) == len(sizes)):
            raise SourceResponseError("JSON-stat id/size missing or inconsistent", retryable=False,
                                      kind="schema_drift")
        if not isinstance(doc.get("dimension"), dict) or set(ids) - set(doc["dimension"]):
            raise SourceResponseError("JSON-stat dimension object incomplete", retryable=False, kind="schema_drift")
        return doc

    def parse(self, raw: RawAcquisition) -> Iterator[ObservationBatch]:
        doc = self._doc(raw)
        try:
            batch = self._batch(raw, doc)
        except SourceResponseError:
            raise
        except _MALFORMED as e:  # malformed provider data is a typed failure, never an untyped crash
            raise SourceResponseError(f"malformed JSON-stat structure: {type(e).__name__}", retryable=False,
                                      kind="schema_drift") from None
        yield batch

    def _batch(self, raw: RawAcquisition, doc: dict[str, Any]) -> ObservationBatch:
        ids: list[str] = [str(d) for d in doc["id"]]
        sizes = [int(str(s)) for s in doc["size"]]
        n = math.prod(sizes)
        if n > MAX_POSITIONS:
            raise SourceResponseError(f"cube of {n} positions exceeds template bound", retryable=False,
                                      kind="oversized_cube")
        cats = {d: _positions(doc["dimension"][d].get("category", {}).get("index"), s, d)
                for d, s in zip(ids, sizes, strict=True)}
        labels = {d: dict(doc["dimension"][d].get("category", {}).get("label") or {}) for d in ids}
        values = _sparse(doc.get("value"), n, "value")
        status = _sparse(doc.get("status"), n, "status")
        ext = doc.get("extension") if isinstance(doc.get("extension"), dict) else {}
        status_labels = dict(((ext.get("status") or {}).get("label")) or {})
        annotations = {
            str(a.get("type")): a.get("date") or a.get("title")
            for a in (ext.get("annotation") or [])
            if isinstance(a, dict) and a.get("type") in (
                "UPDATE_DATA", "UPDATE_STRUCTURE", "OBS_COUNT", "OBS_PERIOD_OVERALL_LATEST",
                "OBS_PERIOD_OVERALL_OLDEST", "DISSEMINATION_TIMESTAMP_DATA")
        }
        strides = [math.prod(sizes[i + 1:]) for i in range(len(sizes))]
        observations: list[ParsedObservation] = []
        if n and set(self.order) == set(ids) and ids:
            for pos in range(n):
                coord = {d: cats[d][(pos // strides[i]) % sizes[i]] for i, d in enumerate(ids)}
                observations.append(self._observation(pos, coord, values.get(pos), status.get(pos),
                                                      status_labels, labels))
        meta = {
            "kind": "jsonstat",
            "dataset": self.dataset,
            "label": doc.get("label"),
            "dataset_updated": doc.get("updated"),  # dataset-level; NOT a value publication time
            "extension_id": ext.get("id"),
            "datastructure": plain(ext.get("datastructure")),
            "annotations": plain(annotations),
            "dimension_order": ids,
            "size": sizes,
            "cube_positions": n,
            "value_count": len(values),
            "status_count": len(status),
            "status_labels": status_labels,
            "status_present": "status" in doc,
            "status_codes_seen": sorted({str(s) for s in status.values()}),
            "categories": {d: cats[d] for d in ids},
            "unit_codes": cats.get("unit", []),
            "freq_codes": cats.get("freq", []),
            "structure_fingerprint": structure_fingerprint(doc),
            "pinned_dimension_order": self.order,
        }
        expected = tuple(o.logical_key for o in observations)
        return ObservationBatch(
            connector=self.manifest["name"],
            connector_version=self.manifest["version"],
            item_key=raw.item.key,
            observations=tuple(observations),
            expected_keys=expected,
            metadata=meta,
            schema_fingerprint=meta["structure_fingerprint"],
        )

    def _cell_type(self, coord: Mapping[str, str]) -> str:
        res = self.rp["resolution_dimensions"]
        at_total = [d for d in res if coord.get(d) in set(self.total_categories.get(d, []))]
        if not at_total:
            return "leaf"
        return "total" if len(at_total) == len(res) else "subtotal"

    def _observation(self, pos: int, coord: dict[str, str], v: Any, st: Any, status_labels: Mapping[str, Any],
                     labels: Mapping[str, Mapping[str, Any]]) -> ParsedObservation:
        year = int(coord["time"])
        mm, dd = (int(x) for x in self.sem["temporal_support"].split(":", 1)[1].split("-"))
        subj_dim = self.rp["subject"]["dimension"]
        geo = coord[subj_dim]
        support = Support(
            temporal=TimeExtent.on_date(date(year, mm, dd), label=f"{year}-{mm:02d}-{dd:02d}"),
            spatial=SpatialRef(scheme=self.sem["spatial_scheme"], key=f"ESTAT:{geo}", authority="Eurostat"),
            population=PopulationRef(universe=self.sem["population_universe"],
                                     definition_source=f"Eurostat {self.dataset} metadata"),
            aggregation=self.sem["aggregation"],
            resolution={d: coord[d] for d in self.rp["resolution_dimensions"]},
        )
        codes = str(st) if st is not None else ""
        flag_chars = [c for c in codes if c.strip()]
        if v is None:
            reason = next((MISSING_REASON_BY_FLAG[c] for c in flag_chars if c in MISSING_REASON_BY_FLAG),
                          "unavailable")
            value: dict[str, Any] = {"kind": "missing", "reason": reason}
        else:
            d = v if isinstance(v, Decimal) else Decimal(str(v))
            if not d.is_finite():
                raise SourceResponseError("non-finite value", retryable=False, kind="schema_drift")
            s = format(d, "f")
            value = {"kind": "decimal", "value": s.rstrip("0").rstrip(".") if "." in s else s,
                     "unit_ref": self.sem["unit_ref"], "basis": {"scale_factor": "1"}}
        special = {d: self.special[d][coord[d]] for d in self.special if coord.get(d) in self.special[d]}
        flags: dict[str, Any] = {
            "dataset": self.dataset,
            "status": codes,
            "status_labels": {c: str(status_labels.get(c, "")) for c in flag_chars},
            "cell_type": self._cell_type(coord),
            "categories": {d: coord[d] for d in self.order if d != "time"},
            "unit_code": coord.get("unit", ""),
            "freq": coord.get("freq", ""),
        }
        if special:
            flags["special_categories"] = special
        if v is None:
            flags["absent_in_value_map"] = True
        key_parts = {**coord, "dataset": self.dataset}
        logical_key = render_logical_key(str(self.rp.get("logical_key_template", DEFAULT_KEY_TEMPLATE)), key_parts)
        return ParsedObservation(
            subject_provider=self.rp["subject"]["alias_provider"],
            subject_key=geo,
            concept_id=self.sem["concept_id"],
            concept_version=self.sem["concept_version"],
            support=support,
            logical_key=logical_key,
            value=value,
            uncertainty=UncertaintySpec.unknown("Eurostat publishes no uncertainty for this value"),
            source_flags=flags,
            raw_selector={"jsonstat_position": pos, "coordinates": coord,
                          "labels": {d: str(labels.get(d, {}).get(coord[d], "")) for d in coord}},
        )

    # ------------------------------------------------------------------ validate
    def validate(self, batch: ObservationBatch) -> QualityReport:
        f: list[Finding] = []
        md = batch.metadata
        order = list(md.get("dimension_order") or [])
        if order != self.order:
            f.append(Finding(code="dimension_order_changed", severity="blocker",
                             message=f"dimension order {order} differs from pinned {self.order}"))
        cats: dict[str, list[str]] = md.get("categories") or {}
        for dim, pinned in self.rp["pinned_categories"].items():
            got = cats.get(dim)
            if got is None or sorted(got) != sorted(pinned):
                f.append(Finding(code="pinned_categories_changed", severity="blocker",
                                 message=f"dimension {dim}: categories {got} differ from pinned {pinned}"))
        units = cats.get("unit", [])
        if units != [self.sem["unit_category"]]:
            f.append(Finding(code="unit_changed", severity="blocker",
                             message=f"unit categories {units} != pinned {self.sem['unit_category']}"))
        years = sorted(int(t) for t in cats.get("time", []))
        if years and years != self.years:
            f.append(Finding(code="time_mismatch", severity="blocker",
                             message=f"returned periods {years} differ from requested {self.years}"))
        n = int(md.get("cube_positions", 0))
        if n == 0 or not batch.observations:
            f.append(Finding(code="empty_response", severity="blocker",
                             message="empty cube: an empty response is never zero or complete"))
        if int(md.get("value_count", 0)) > n:
            f.append(Finding(code="value_count_exceeds_size", severity="blocker",
                             message="more values than JSON-stat size implies"))
        if len(batch.observations) not in (0, n):
            f.append(Finding(code="size_mismatch", severity="blocker",
                             message=f"{len(batch.observations)} cells parsed for a cube of {n}"))
        labels = md.get("status_labels") or {}
        seen = set(md.get("status_codes_seen") or [])
        if labels and not md.get("status_present"):
            f.append(Finding(code="status_missing", severity="blocker",
                             message="status labels declared but the status map is missing"))
        for code in sorted(seen):
            for c in code:
                if c not in self.flags_vocab:
                    f.append(Finding(code="unknown_status_flag", severity="blocker",
                                     message=f"status flag {c!r} is not in the reviewed vocabulary"))
                elif c not in labels:
                    f.append(Finding(code="undeclared_status_flag", severity="blocker",
                                     message=f"status flag {c!r} used without a declared label"))
        for c, label in labels.items():
            if c in EUROSTAT_FLAGS and str(label).strip().lower() != EUROSTAT_FLAGS[c]:
                f.append(Finding(code="status_label_changed", severity="warning",
                                 message=f"flag {c!r} label {label!r} differs from reviewed meaning"))
        keys = [o.logical_key for o in batch.observations]
        if len(keys) != len(set(keys)):
            f.append(Finding(code="duplicate_keys", severity="blocker", message="duplicate logical keys"))
        neg = [o.logical_key for o in batch.observations
               if o.value.get("kind") == "decimal" and Decimal(o.value["value"]) < 0]
        if neg:
            f.append(Finding(code="negative_count", severity="blocker", message=f"{len(neg)} negative counts"))
        n_missing = sum(1 for o in batch.observations if o.value.get("kind") == "missing")
        if n_missing:
            f.append(Finding(code="missing_values", severity="warning",
                             message=f"{n_missing} positions absent from the value map (kept Missing)"))
        f.extend(self._additivity(batch))
        outcome = "blocked" if any(x.severity == "blocker" for x in f) else "pass"
        return QualityReport(outcome=outcome, findings=tuple(f), expected_count=n, received_count=len(keys),
                             missing_keys=tuple(sorted(o.logical_key for o in batch.observations
                                                       if o.value.get("kind") == "missing")))

    def _additivity(self, batch: ObservationBatch) -> list[Finding]:
        """Leaf cells of each resolution dimension should sum to its declared total."""
        out: list[Finding] = []
        cells: dict[frozenset[tuple[str, str]], Decimal] = {}
        for o in batch.observations:
            if o.value.get("kind") == "decimal":
                coord = {**o.source_flags["categories"], "time": str(o.support.temporal.start)}
                cells[frozenset(coord.items())] = Decimal(o.value["value"])
        for dim, totals in sorted(self.total_categories.items()):
            if dim not in self.rp["resolution_dimensions"] or not totals:
                continue
            sums: dict[frozenset[tuple[str, str]], Decimal] = {}
            for key, val in cells.items():
                kd = dict(key)
                if kd[dim] in totals:
                    continue
                kd[dim] = totals[0]
                tkey = frozenset(kd.items())
                sums[tkey] = sums.get(tkey, Decimal(0)) + val
            bad = [k for k, s in sums.items() if k in cells and cells[k] != s]
            if bad:
                out.append(Finding(code="additivity_mismatch", severity="warning",
                                   message=f"{len(bad)} {dim} groups whose leaves do not sum to {totals[0]}"))
        return out

    def next_cursor(self, plan: FetchPlan, outcomes: Sequence[FetchOutcome]) -> Cursor:
        ok = bool(outcomes) and all(o.status in ("published", "unchanged") for o in outcomes)
        return Cursor(state={"complete": ok and not plan.budget_exhausted, "years": self.years})
