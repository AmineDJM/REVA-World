"""Atlas tables: the fixed long schema, registration and country-code mapping (V3, agent ATLASCORE).

An **atlas table** is one provider dataset (``source`` × ``dataset``) parsed from archived raw
bytes into the long schema :data:`ATLAS_SCHEMA` — one row per reported cell
(indicator × provider geography × period). It is registered with
:func:`reva.simulation.datasets.register_dataset` as a content-addressed Parquet table plus a
JSON manifest (``evidence.dataset`` row, ``concept_id = 'atlas.table'``,
``dataset_key = 'atlas.<source>.<dataset>'``, ``subject_key = '<source>.<dataset>'``).
A re-registration of identical content is a no-op; changed content is a new version (the
atlas snapshot picks the latest version recorded at its cutoff).

Columns (``ATLAS_SCHEMA``, nullable ones marked ?):

* ``source``, ``dataset`` — short keys, ``[a-z0-9][a-z0-9_-]*`` (no dots), equal to the
  ``source``/``dataset`` arguments. Catalog source orders name a cell series as
  ``"<source>.<dataset>.<indicator>"`` (split on the first two dots).
* ``indicator`` — the provider's indicator code, with any extra dimension folded in
  (``"UNE_RT|sex=F|age=Y15-24"``): (indicator, provider_geo, period) is unique.
* ``concept_id``? — optional catalog concept hint; the catalog's ``source_order`` is authoritative.
* ``iso3``? — the crosswalk entity key (ISO 3166-1 alpha-3, or World Bank ``XKX``/``CHI`` for the
  two non-ISO entities of ``registry/entities/country_crosswalk.json``). Null when the provider
  geography does not map (regions, aggregates, historical areas): such rows are **kept** and
  listed in the manifest (``coverage.unmapped_provider_geo``), never dropped.
* ``provider_geo`` — the provider's own geography code, verbatim.
* ``entity_qid``? — Wikidata QID (``Q…``) when the provider geography is a KG entity.
* ``period_start``, ``period_end`` — half-open UTC date interval ``[start, end)``
  (calendar year 2020 = ``2020-01-01``..``2021-01-01``; see :func:`year_period`).
* ``value``? — the reported number as a **strict decimal string** (``-12.5``, ``3e-4``; no
  thousands separators, no NaN/inf, never a filled-in zero).
* ``value_text``? — a reported non-numeric value (a category, a legal text excerpt, ``"Yes"``).
* ``unit`` — the provider's unit as declared.
* ``flags`` — a JSON object: provider flags/notes (``{"provider_flag": "e"}``). A row with both
  ``value`` and ``value_text`` null must say why: ``{"missing": "<reason>"}`` (see
  :func:`missing_flags`). Prefer omitting cells the provider simply does not report.
* ``raw_sha256`` — sha256 of the archived raw artifact the cell was parsed from (one of
  ``raw_sha256s``; every one must be an ``evidence.acquisition`` with the table's origin).
* ``selector`` — a JSON object locating the cell in that raw artifact
  (``{"file": "WDICSV.csv", "row": 1234, "column": "2020"}``).
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from collections.abc import Mapping, Sequence
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
import yaml
from sqlalchemy import Engine, text

from reva.kernel.errors import IntegrityFailure, ValidationProblem
from reva.ontology.registry import REGISTRY_ROOT
from reva.ports.storage import ArtifactStore

ATLAS_TABLE_SCHEMA_VERSION = "atlas-table/1"
ATLAS_DATASET_CONCEPT = "atlas.table"
NAMESPACE = "normalised"

ATLAS_SCHEMA: pl.Schema = pl.Schema(
    {
        "source": pl.String,
        "dataset": pl.String,
        "indicator": pl.String,
        "concept_id": pl.String,
        "iso3": pl.String,
        "provider_geo": pl.String,
        "entity_qid": pl.String,
        "period_start": pl.Date,
        "period_end": pl.Date,
        "value": pl.String,
        "value_text": pl.String,
        "unit": pl.String,
        "flags": pl.String,
        "raw_sha256": pl.String,
        "selector": pl.String,
    }
)
ATLAS_COLUMNS: tuple[str, ...] = tuple(ATLAS_SCHEMA.names())
NULLABLE_COLUMNS = frozenset({"concept_id", "iso3", "entity_qid", "value", "value_text"})
REQUIRED_COLUMNS = tuple(c for c in ATLAS_COLUMNS if c not in NULLABLE_COLUMNS)

#: The Arrow schema of the registered Parquet files (exact; checked on read).
ATLAS_ARROW_SCHEMA = pa.schema(
    [
        pa.field(c, pa.date32() if ATLAS_SCHEMA[c] == pl.Date else pa.string(), nullable=c in NULLABLE_COLUMNS)
        for c in ATLAS_COLUMNS
    ]
)

SOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")
DATASET_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,38}$")
CONCEPT_RE = r"^[a-z0-9][a-z0-9_.-]{0,199}$"
QID_RE = r"^Q[1-9][0-9]{0,11}$"
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
#: Strict decimal: optional minus, no leading zeros, optional fraction and exponent; no NaN/inf/commas.
DECIMAL_RE = r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]{1,3})?$"
CONTROL_RE = r"[\x00-\x1f\x7f]"
MAX_TEXT = 4000
MAX_UNMAPPED_LISTED = 5000

MISSING_REASONS = (
    "not_reported",  # provider row exists, cell explicitly empty / "..": no value reported
    "confidential",
    "not_applicable",
    "not_available",
    "unreliable",  # provider withheld/flagged as unreliable
    "provider_missing_code",  # provider-specific missing code kept in flags.provider_flag
)


# --------------------------------------------------------------------------- small helpers
def year_period(year: int) -> tuple[date, date]:
    """Calendar year ``year`` as a half-open ``[start, end)`` interval."""
    return date(year, 1, 1), date(year + 1, 1, 1)


def missing_flags(reason: str, **extra: Any) -> str:
    """Flags JSON for a cell with no value: ``{"missing": reason, ...}``."""
    if reason not in MISSING_REASONS:
        raise ValueError(f"unknown missing reason {reason!r}; one of {MISSING_REASONS}")
    return json.dumps({"missing": reason, **extra}, sort_keys=True, separators=(",", ":"))


def json_obj(obj: Mapping[str, Any] | None = None, **kw: Any) -> str:
    """Canonical JSON object string for ``flags``/``selector``."""
    return json.dumps({**dict(obj or {}), **kw}, sort_keys=True, separators=(",", ":"), default=str)


def empty_atlas_frame() -> pl.DataFrame:
    return pl.DataFrame(schema=ATLAS_SCHEMA)


def dataset_key(source: str, dataset: str) -> str:
    return f"atlas.{source}.{dataset}"


def subject_key(source: str, dataset: str) -> str:
    return f"{source}.{dataset}"


# --------------------------------------------------------------------------- country codes
CROSSWALK_PATH = REGISTRY_ROOT / "entities" / "country_crosswalk.json"
EXTRA_CODES_PATH = REGISTRY_ROOT / "entities" / "country_codes_extra.json"
GEO_ALIASES_DIR = REGISTRY_ROOT / "atlas" / "geo_aliases"

#: Built-in schemes read from the crosswalk entity fields.
_CROSSWALK_FIELDS = {"iso3": "iso3", "iso2": "iso2", "m49": "m49", "wb": "worldbank", "comtrade": "comtrade_reporter"}
SCHEME_ALIASES = {
    "iso": "iso3",
    "iso3166_alpha3": "iso3",
    "alpha3": "iso3",
    "iso3166_alpha2": "iso2",
    "alpha2": "iso2",
    "un": "m49",
    "un_m49": "m49",
    "iso_numeric": "m49",
    "worldbank": "wb",
    "comtrade_reporter": "comtrade",
}
_NUMERIC_SCHEMES = {"m49", "comtrade"}


def _norm_code(code: str, numeric: bool) -> str:
    c = str(code).strip()
    if numeric and c.isdigit():
        return str(int(c))
    return c.upper()


class GeoIndex:
    """Provider code -> crosswalk entity key, per scheme (immutable after load)."""

    def __init__(self, maps: Mapping[str, Mapping[str, str]], keys: frozenset[str], sources: Mapping[str, Any]):
        self.maps = {k: dict(v) for k, v in maps.items()}
        self.keys = keys
        self.sources = dict(sources)

    def schemes(self) -> list[str]:
        return sorted(self.maps)

    def scheme(self, scheme: str) -> str:
        s = SCHEME_ALIASES.get(scheme.lower(), scheme.lower())
        if s not in self.maps:
            raise ValueError(f"unknown geo scheme {scheme!r}; known: {', '.join(self.schemes())}")
        return s

    def lookup(self, code: str | None, scheme: str) -> str | None:
        if code is None:
            return None
        s = self.scheme(scheme)
        return self.maps[s].get(_norm_code(code, s in _NUMERIC_SCHEMES))


def _load_extra(path: Path, keys: frozenset[str], maps: dict[str, dict[str, str]], sources: dict[str, Any]) -> None:
    """Merge ``country_codes_extra.json`` / ``geo_aliases/*.yaml`` scheme definitions.

    Format: ``{"schemes": {name: {"base": "iso3"|"iso2"|..|null, "exclude": [codes],
    "aliases": {code: entity_key} | [{"code", "iso3", "source"}], "source": {...}}}}``.
    Alias targets must be crosswalk entity keys; a code mapped two ways is an error.
    """
    doc = json.loads(path.read_text()) if path.suffix == ".json" else yaml.safe_load(path.read_text())
    if not isinstance(doc, Mapping):
        raise ValidationProblem(f"{path.name}: not a mapping")
    schemes = doc.get("schemes")
    if schemes is None and "scheme" in doc:  # single-scheme alias file
        schemes = {doc["scheme"]: {k: v for k, v in doc.items() if k != "scheme"}}
    if not isinstance(schemes, Mapping):
        raise ValidationProblem(f"{path.name}: 'schemes' must be a mapping")
    for name, spec in schemes.items():
        name = str(name).lower()
        if name in _CROSSWALK_FIELDS and spec.get("base") not in (None, name):
            raise ValidationProblem(f"{path.name}: built-in scheme {name} cannot be rebased")
        target = maps.setdefault(name, {})
        base = spec.get("base")
        if base is not None:
            b = SCHEME_ALIASES.get(base, base)
            if b not in _CROSSWALK_FIELDS:
                raise ValidationProblem(f"{path.name}: scheme {name} base {base!r} is not a crosswalk scheme")
            excl = {_norm_code(c, b in _NUMERIC_SCHEMES) for c in spec.get("exclude") or []}
            for code, key in maps[b].items():
                if code not in excl:
                    target.setdefault(code, key)
        aliases = spec.get("aliases") or {}
        items = [(a["code"], a["iso3"]) for a in aliases] if isinstance(aliases, list) else list(dict(aliases).items())
        for code, key in items:
            if key not in keys:
                raise ValidationProblem(f"{path.name}: alias {name}:{code} -> {key!r} is not a crosswalk entity")
            c = _norm_code(str(code), name in _NUMERIC_SCHEMES)
            if c in target and target[c] != key:
                raise ValidationProblem(
                    f"{path.name}: {name}:{code} already maps to {target[c]} (exclude it from the base first), not {key}"
                )
            target[c] = key
        if spec.get("source") is not None:
            sources.setdefault(name, []).append({"file": path.name, **dict(spec["source"])})


@lru_cache(maxsize=1)
def geo_index() -> GeoIndex:
    doc = json.loads(CROSSWALK_PATH.read_text())
    keys = frozenset(e["key"] for e in doc["entities"])
    maps: dict[str, dict[str, str]] = {s: {} for s in _CROSSWALK_FIELDS}
    for e in doc["entities"]:
        for scheme, field in _CROSSWALK_FIELDS.items():
            v = e.get(field)
            if v:
                maps[scheme][_norm_code(v, scheme in _NUMERIC_SCHEMES)] = e["key"]
        maps["iso3"].setdefault(e["key"], e["key"])  # XKX / CHI: the crosswalk key itself
    sources: dict[str, Any] = {"crosswalk": {"file": CROSSWALK_PATH.name, "content_sha256": doc.get("content_sha256")}}
    if EXTRA_CODES_PATH.exists():
        _load_extra(EXTRA_CODES_PATH, keys, maps, sources)
    if GEO_ALIASES_DIR.is_dir():
        for p in sorted(GEO_ALIASES_DIR.glob("*.y*ml")) + sorted(GEO_ALIASES_DIR.glob("*.json")):
            _load_extra(p, keys, maps, sources)
    return GeoIndex(maps, keys, sources)


def country_iso3(provider_code: str | None, scheme: str) -> str | None:
    """The crosswalk entity key (ISO3; ``XKX``/``CHI`` for the two non-ISO entities) of a
    provider geography code, or ``None`` when it does not map (aggregates, regions, unknown).

    Schemes: ``iso3``, ``iso2``, ``m49`` (also ISO numeric), ``wb``, ``comtrade`` from the
    crosswalk; ``eurostat``, ``fips``, ``imf``, ``ilo``, ``who``, ``owid``… from
    ``registry/entities/country_codes_extra.json`` and ``registry/atlas/geo_aliases/*.yaml``.
    Unknown scheme -> ``ValueError``.
    """
    return geo_index().lookup(provider_code, scheme)


def known_country_keys() -> frozenset[str]:
    return geo_index().keys


# --------------------------------------------------------------------------- validation
def _fail(msg: str, **ctx: Any) -> None:
    raise ValidationProblem(f"atlas table refused: {msg}", **ctx)


def _bad_examples(frame: pl.DataFrame, mask: pl.Expr, cols: Sequence[str], n: int = 5) -> list[dict[str, Any]]:
    return [
        {k: (str(v) if v is not None else None) for k, v in r.items()}
        for r in frame.filter(mask).select(cols).head(n).to_dicts()
    ]


def _json_objects(values: Sequence[str | None], column: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for v in values:
        if v is None:
            continue
        try:
            obj = json.loads(v)
        except (TypeError, ValueError):
            _fail(f"{column} is not valid JSON", example=v[:200])
        if not isinstance(obj, dict):
            _fail(f"{column} must be a JSON object", example=v[:200])
        out[v] = obj
    return out


def validate_atlas_frame(
    frame: pl.DataFrame,
    *,
    source: str,
    dataset: str,
    raw_sha256s: Sequence[str],
    geo_scheme: str | None = None,
) -> tuple[pl.DataFrame, list[dict[str, Any]]]:
    """Check and normalise an atlas frame. Returns ``(frame, unmapped)``.

    The frame keeps exactly :data:`ATLAS_COLUMNS` in order and is sorted by
    (indicator, provider_geo, period_start, period_end). ``iso3`` is filled from
    ``provider_geo`` through ``geo_scheme`` when given (a pre-filled ``iso3`` that disagrees
    is refused); without a scheme, pre-filled ``iso3`` values must be crosswalk keys.
    ``unmapped`` lists provider geographies left without ``iso3`` with their row counts.
    Every failure is a typed ``ValidationProblem``; nothing is coerced into a value.
    """
    if not isinstance(frame, pl.DataFrame):
        _fail("frame must be a polars.DataFrame")
    if not SOURCE_RE.match(source or ""):
        _fail("source must match [a-z0-9][a-z0-9_-]{0,23} (no dots)", source=source)
    if not DATASET_RE.match(dataset or ""):
        _fail("dataset must match [a-z0-9][a-z0-9_-]{0,38} (no dots)", dataset=dataset)
    raws = sorted(set(raw_sha256s))
    if not raws or any(not SHA_RE.match(r) for r in raws):
        _fail("raw_sha256s must be a non-empty list of sha256 hex digests")
    cols = set(frame.columns)
    missing = [c for c in ATLAS_COLUMNS if c not in cols]
    extra = sorted(cols - set(ATLAS_COLUMNS))
    if missing or extra:
        _fail("columns do not match ATLAS_SCHEMA", missing=missing, extra=extra)
    if frame.height == 0:
        _fail("an atlas table needs at least one row")
    try:
        f = frame.select([pl.col(c).cast(ATLAS_SCHEMA[c], strict=True) for c in ATLAS_COLUMNS])
    except Exception as e:  # polars raises several exception types for failed strict casts
        _fail("a column does not have its ATLAS_SCHEMA type", error=str(e)[:300])

    nulls = f.select([pl.col(c).null_count() for c in REQUIRED_COLUMNS]).row(0, named=True)
    bad_nulls = {c: n for c, n in nulls.items() if n}
    if bad_nulls:
        _fail("required columns contain nulls", null_counts=bad_nulls)

    for col, want in (("source", source), ("dataset", dataset)):
        got = f[col].unique().to_list()
        if got != [want]:
            _fail(f"column {col} must equal the {col} argument on every row", expected=want, found=got[:5])

    text_checks = {
        "indicator": (1, 200),
        "provider_geo": (1, 64),
        "unit": (1, 100),
        "value_text": (1, MAX_TEXT),
        "flags": (2, MAX_TEXT),
        "selector": (2, MAX_TEXT),
    }
    for col, (lo, hi) in text_checks.items():
        n = pl.col(col).str.len_chars()
        mask = pl.col(col).is_not_null() & ((n < lo) | (n > hi) | pl.col(col).str.contains(CONTROL_RE))
        if f.select(mask.any()).item():
            _fail(
                f"{col} must be {lo}..{hi} characters without control characters",
                examples=_bad_examples(f, mask, [col]),
            )

    pattern_checks = {"concept_id": CONCEPT_RE, "entity_qid": QID_RE, "value": DECIMAL_RE}
    for col, pat in pattern_checks.items():
        mask = pl.col(col).is_not_null() & ~pl.col(col).str.contains(pat)
        if f.select(mask.any()).item():
            what = "a strict decimal string (no NaN/inf, separators or invented fill)" if col == "value" else pat
            _fail(f"{col} must be {what}", examples=_bad_examples(f, mask, ["indicator", "provider_geo", col]))

    used_raw = set(f["raw_sha256"].unique().to_list())
    if not used_raw <= set(raws):
        _fail("raw_sha256 values must be listed in raw_sha256s", unknown=sorted(used_raw - set(raws))[:5])

    mask = pl.col("period_start") >= pl.col("period_end")
    if f.select(mask.any()).item():
        _fail(
            "period must be half-open with period_start < period_end",
            examples=_bad_examples(f, mask, ["indicator", "provider_geo", "period_start", "period_end"]),
        )

    flags = _json_objects(f["flags"].unique().to_list(), "flags")
    if any(not o for o in _json_objects(f["selector"].unique().to_list(), "selector").values()):
        _fail("selector must be a non-empty JSON object locating the cell in its raw artifact")
    with_missing = [k for k, o in flags.items() if isinstance(o.get("missing"), str) and o["missing"]]
    mask = pl.col("value").is_null() & pl.col("value_text").is_null() & ~pl.col("flags").is_in(with_missing)
    if f.select(mask.any()).item():
        _fail(
            "a row without value and value_text must carry flags.missing (a reason), never an invented value",
            examples=_bad_examples(f, mask, ["indicator", "provider_geo", "period_start", "flags"]),
        )

    keys = ["indicator", "provider_geo", "period_start", "period_end"]
    dup = f.select(pl.struct(keys).is_duplicated().any()).item()
    if dup:
        mask = pl.struct(keys).is_duplicated()
        _fail(
            "duplicate cells: (indicator, provider_geo, period_start, period_end) must be unique "
            "(fold extra dimensions into indicator)",
            examples=_bad_examples(f, mask, keys),
        )

    idx = geo_index()
    if geo_scheme is not None:
        scheme = idx.scheme(geo_scheme)
        geos = f["provider_geo"].unique().to_list()
        mapping = {g: idx.lookup(g, scheme) for g in geos}
        mapped = pl.col("provider_geo").replace_strict(mapping, default=None, return_dtype=pl.String)
        conflict = pl.col("iso3").is_not_null() & mapped.is_not_null() & (pl.col("iso3") != mapped)
        if f.select(conflict.any()).item():
            _fail(
                "pre-filled iso3 disagrees with the crosswalk mapping",
                scheme=scheme,
                examples=_bad_examples(f, conflict, ["provider_geo", "iso3"]),
            )
        f = f.with_columns(pl.coalesce(pl.col("iso3"), mapped).alias("iso3"))
    bad_iso = sorted(set(f["iso3"].drop_nulls().unique().to_list()) - idx.keys)
    if bad_iso:
        _fail("iso3 values must be crosswalk entity keys (use country_iso3 or geo_scheme)", unknown=bad_iso[:10])

    mapped_keys = ["indicator", "iso3", "period_start", "period_end"]
    clash = pl.col("iso3").is_not_null() & pl.struct(mapped_keys).is_duplicated()
    if f.select(clash.any()).item():
        _fail(
            "two provider geographies map to the same country for the same indicator and period; "
            "the table must not make REVA choose (leave one unmapped or fix the geo scheme)",
            examples=_bad_examples(f, clash, ["indicator", "provider_geo", "iso3", "period_start"]),
        )
    unmapped = (
        f.filter(pl.col("iso3").is_null())
        .group_by("provider_geo")
        .agg(pl.len().alias("rows"))
        .sort("provider_geo")
        .to_dicts()
    )
    f = f.sort(keys)
    return f, [{"provider_geo": u["provider_geo"], "rows": int(u["rows"])} for u in unmapped]


def to_arrow(frame: pl.DataFrame) -> pa.Table:
    """The exact Arrow table registered for a validated frame."""
    t = frame.select(list(ATLAS_COLUMNS)).to_arrow()
    return pa.Table.from_arrays(
        [t.column(c).cast(ATLAS_ARROW_SCHEMA.field(c).type) for c in ATLAS_COLUMNS], schema=ATLAS_ARROW_SCHEMA
    )


def coverage_summary(
    frame: pl.DataFrame, unmapped: list[dict[str, Any]], *, raw_sha256s: Sequence[str], geo_scheme: str | None
) -> dict[str, Any]:
    s = frame.select(
        pl.col("indicator").n_unique().alias("indicators"),
        pl.col("iso3").drop_nulls().n_unique().alias("countries"),
        pl.col("provider_geo").n_unique().alias("provider_geos"),
        pl.col("period_start").min().alias("period_min"),
        pl.col("period_end").max().alias("period_max"),
        pl.col("value").is_not_null().sum().alias("numeric_cells"),
        (pl.col("value").is_null() & pl.col("value_text").is_not_null()).sum().alias("text_cells"),
        (pl.col("value").is_null() & pl.col("value_text").is_null()).sum().alias("missing_cells"),
        pl.col("concept_id").drop_nulls().n_unique().alias("concept_hints"),
    ).row(0, named=True)
    return {
        "atlas_schema": ATLAS_TABLE_SCHEMA_VERSION,
        "row_count": frame.height,
        "indicators": int(s["indicators"]),
        "countries": int(s["countries"]),
        "provider_geos": int(s["provider_geos"]),
        "period_min": str(s["period_min"]),
        "period_max": str(s["period_max"]),
        "numeric_cells": int(s["numeric_cells"]),
        "text_cells": int(s["text_cells"]),
        "missing_cells": int(s["missing_cells"]),
        "concept_hints": int(s["concept_hints"]),
        "geo_scheme": geo_scheme,
        "unmapped_provider_geo_count": len(unmapped),
        "unmapped_rows": sum(u["rows"] for u in unmapped),
        "unmapped_provider_geo": unmapped[:MAX_UNMAPPED_LISTED],
        "unmapped_list_truncated": len(unmapped) > MAX_UNMAPPED_LISTED,
        "raw_sha256s": sorted(set(raw_sha256s)),
    }


# --------------------------------------------------------------------------- registration
def register_atlas_table(
    engine: Engine,
    store: ArtifactStore,
    *,
    source: str,
    dataset: str,
    frame: pl.DataFrame,
    raw_sha256s: list[str],
    parser_version: str,
    origin: str,
    coverage_note: str | None = None,
    geo_scheme: str | None = None,
    rights: Mapping[str, Any] | None = None,
    actor: str = "worker:atlas",
) -> dict[str, Any]:
    """Validate, map and register one atlas table (worker engine; its own transaction).

    ``raw_sha256s`` are the archived raw artifacts the frame was parsed from; each must be
    the ``raw_sha256`` of an ``evidence.acquisition`` with ``origin`` (archive the bytes first,
    e.g. with ``publish_acquisition``). Idempotent: the same content returns the existing
    registration (``created: False``). Returns a summary dict (dataset id/key, artifact and
    manifest sha256, row count, coverage incl. unmapped provider geographies).
    """
    from reva.evidence.locks import take_evidence_write_lock
    from reva.simulation.datasets import register_dataset

    if origin not in ("live", "test_fixture"):
        _fail("origin must be live or test_fixture", origin=origin)
    if not parser_version or len(parser_version) > 200:
        _fail("parser_version must be 1..200 characters")
    if coverage_note is not None and len(coverage_note) > MAX_TEXT:
        _fail("coverage_note is too long")
    f, unmapped = validate_atlas_frame(
        frame, source=source, dataset=dataset, raw_sha256s=raw_sha256s, geo_scheme=geo_scheme
    )
    table = to_arrow(f)
    coverage = coverage_summary(f, unmapped, raw_sha256s=raw_sha256s, geo_scheme=geo_scheme)
    coverage.update({"source": source, "dataset": dataset, "coverage_note": coverage_note})
    raws = sorted(set(raw_sha256s))
    with engine.begin() as conn:
        take_evidence_write_lock(conn)
        acqs = (
            conn.execute(
                text("""SELECT DISTINCT ON (raw_sha256) raw_sha256, id::text AS id FROM evidence.acquisition
                        WHERE raw_sha256 = ANY(:s) AND origin = :o
                        ORDER BY raw_sha256, recorded_at DESC, id DESC"""),
                {"s": raws, "o": origin},
            )
            .mappings()
            .all()
        )
        found = {a["raw_sha256"]: a["id"] for a in acqs}
        not_archived = [r for r in raws if r not in found]
        if not_archived:
            _fail(
                "raw artifacts are not archived acquisitions with this origin (archive the raw bytes first)",
                raw_sha256s=not_archived[:10],
                origin=origin,
            )
        rec = register_dataset(
            conn,
            store,
            dataset_key=dataset_key(source, dataset),
            concept_id=ATLAS_DATASET_CONCEPT,
            subject_key=subject_key(source, dataset),
            table=table,
            source_acquisition_ids=sorted(found.values()),
            parser_version=parser_version,
            origin=origin,
            coverage=coverage,
            actor=actor,
            rights=rights,
        )
    return {
        "dataset_id": str(rec.id),
        "dataset_key": rec.dataset_key,
        "subject_key": rec.subject_key,
        "origin": rec.origin,
        "artifact_sha256": rec.artifact_sha256,
        "manifest_sha256": rec.manifest_sha256,
        "row_count": rec.row_count,
        "recorded_at": rec.recorded_at.isoformat(),
        "created": rec.created,
        "unmapped_provider_geo": unmapped,
        "coverage": coverage,
    }


def read_atlas_table(store: ArtifactStore, artifact_sha256: str) -> pl.DataFrame:
    """Load a registered atlas table, verifying its bytes and schema."""
    data = store.get(NAMESPACE, artifact_sha256)
    if hashlib.sha256(data).hexdigest() != artifact_sha256:
        raise IntegrityFailure("atlas table bytes do not match their sha256", sha256=artifact_sha256)
    table = pq.read_table(io.BytesIO(data))
    if not table.schema.equals(ATLAS_ARROW_SCHEMA, check_metadata=False):
        raise IntegrityFailure("atlas table does not have the atlas schema", sha256=artifact_sha256)
    return pl.from_arrow(table)  # type: ignore[return-value]
