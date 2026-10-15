"""UNESCO Institute for Statistics (UIS) Data API -> atlas table ``uis.api`` (agent A-HEALTHEDU).

Source
------
UIS public Data API, ``https://api.uis.unesco.org/api/public`` (launched 2024; replaces the retired
SDMX API). Checked 2026-10-15: unauthenticated, no rate limiting stated, **a query is capped at
100,000 records** (HTTP 400 beyond); the alternative is the Bulk Data Download Service (BDDS)
zip files. The API was chosen because one run of five batched queries covers the curated
indicators for every country with per-record qualifiers and footnotes, pinned to one data
version; the BDDS gives the same data per theme as multi-hundred-MB zips.

Licence: "The work of the UIS is licensed under the Creative Commons Attribution-ShareAlike 4.0
International license" (https://databrowser.uis.unesco.org/terms-and-conditions). Attribution
format: "Source: UNESCO Institute for Statistics (UIS), <URL>, <date of extraction>"; derived tables
must be shared alike. Recorded in :data:`RIGHTS`.

Requests (one run): ``/versions/default`` (the current data version, e.g. ``20260507-91260335``,
"February 2026 Data Release"), then one data query per indicator batch (:data:`BATCHES`, each
well under the 100k-record cap) with ``geoUnitType=NATIONAL``, ``version=<that version>`` (all
batches from the same release), ``indicatorMetadata=true`` and ``footnotes=true``.

What a cell is
--------------
* ``indicator`` = the UIS indicator id verbatim (``LR.AG15T99``, ``25053``); sex and age are part of
  the UIS id and its name (``…, population 15+ years, female (%)``), recorded per concept in the
  catalog (``registry/atlas/catalog/education.yaml``).
* ``value`` = the JSON number exactly as the API serialises it. Some values carry binary-float
  artefacts (``97.29000091552734``); they are kept as served (no rounding by REVA), and the catalog
  notes that UIS publishes rates to at most 2-3 meaningful decimals.
* ``flags.qualifier`` = UIS qualifier verbatim (``UIS_EST`` = UIS estimate, ``NAT_EST`` = national
  estimate); ``flags.magnitude`` = UIS magnitude verbatim (``NIL`` = nil or negligible: the value 0
  is then the provider's report, not a filled zero); ``flags.footnotes`` = the record's footnotes
  verbatim (data sources such as "Population Census", coverage notes such as "Partial data").
* ``flags.uis_data_version`` = the pinned data version; ``flags.estimate_class`` = this agent's
  classification of the indicator (``country_reported`` — administrative/census/survey data
  reported by countries, with UIS estimates flagged per row; ``uis_modelled`` — UIS/GEM model
  output, e.g. ``CR.MOD.*``; ``uis_estimate`` — indicators UIS labels "(UIS estimate)").
* A record with a null value is kept with ``flags.missing = "not_reported"`` (none observed in the
  2026-02 release for these indicators).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from reva.adapters.connectors.atlas_healthedu_common import canon, fetch_archive_register, fit_flags
from reva.adapters.connectors.base import dec_str, json_decimal, module_digest
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError, ValidationProblem
from reva.ports.connectors import FetchItem

CONNECTOR_NAME = "atlas.unesco_uis"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas-uis-parse-1"
SOURCE_KEY = "unesco.uis"
HOST = "api.uis.unesco.org"
BASE = f"https://{HOST}/api/public"
VERSION_URL = f"{BASE}/versions/default"
RECORD_CAP = 100_000  # API limit per query (documented); a response at the cap may be truncated
MAX_ITEM_BYTES = 104_857_600  # batches were 3-20 MB on 2026-10-15

RIGHTS: dict[str, str] = {
    "licence": "CC BY-SA 4.0",
    "licence_url": "https://creativecommons.org/licenses/by-sa/4.0/",
    "terms_url": "https://databrowser.uis.unesco.org/terms-and-conditions",
    "attribution": "Source: UNESCO Institute for Statistics (UIS), https://api.uis.unesco.org, <date of extraction>",
    "share_alike": "derived tables must be shared under CC BY-SA 4.0",
    "source_key": SOURCE_KEY,
}

REPORTED = "country_reported"
MODELLED = "uis_modelled"
UIS_ESTIMATE = "uis_estimate"
CLASSIFIED_BY = "A-HEALTHEDU catalog, from UIS indicator names/metadata"


@dataclass(frozen=True)
class UisIndicator:
    code: str
    unit: str
    estimate_class: str = REPORTED
    note: str = ""


#: Curated indicators, in batches (each batch's total record count — all geographies, 2026-02
#: release — is < 55k, well under the 100k cap; the national subset is smaller).
BATCHES: tuple[tuple[UisIndicator, ...], ...] = (
    (  # literacy and out-of-school rates (~53k records)
        UisIndicator("LR.AG15T99", "%"),
        UisIndicator("LR.AG15T99.F", "%"),
        UisIndicator("LR.AG15T99.M", "%"),
        UisIndicator("LR.AG15T24", "%"),
        UisIndicator("ROFST.1.CP", "%"),
        UisIndicator("ROFST.2.CP", "%"),
        UisIndicator("ROFST.3.CP", "%"),
    ),
    (  # out-of-school numbers, completion, net enrolment, attainment, learning (~52k)
        UisIndicator("OFST.1.CP", "persons"),
        UisIndicator("OFST.1T3.CP", "persons"),
        UisIndicator("CR.1", "%", REPORTED, "household survey data"),
        UisIndicator("CR.2", "%", REPORTED, "household survey data"),
        UisIndicator("CR.3", "%", REPORTED, "household survey data"),
        UisIndicator("CR.MOD.1", "%", MODELLED, "UIS/GEM Report completion-rate model"),
        UisIndicator("CR.MOD.2", "%", MODELLED, "UIS/GEM Report completion-rate model"),
        UisIndicator("CR.MOD.3", "%", MODELLED, "UIS/GEM Report completion-rate model"),
        UisIndicator("NERT.1.CP", "%"),
        UisIndicator("MYS.1T8.AG25T99", "years"),
        UisIndicator("MYS.1T8.AG25T99.F", "years"),
        UisIndicator("MYS.1T8.AG25T99.M", "years"),
        UisIndicator("EA.5T8.AG25T99", "%"),
        UisIndicator("MATH.LOWERSEC", "%", REPORTED, "learning assessments (PISA, TIMSS, regional)"),
        UisIndicator("READ.LOWERSEC", "%", REPORTED, "learning assessments (PISA, PIRLS, regional)"),
    ),
    (  # gross enrolment ratios (~54k)
        UisIndicator("GER.1", "%"),
        UisIndicator("GER.2", "%"),
        UisIndicator("GER.3", "%"),
        UisIndicator("GER.5T8", "%"),
    ),
    (  # tertiary enrolment, school life expectancy, education expenditure (~43k)
        UisIndicator("25053", "persons"),
        UisIndicator("SLE.1T8", "years"),
        UisIndicator("XGDP.FSGOV", "% of GDP"),
        UisIndicator("XGOVEXP.IMF", "% of total government expenditure"),
    ),
    (  # student mobility, R&D (~33k)
        UisIndicator("MSEP.5T8", "%", UIS_ESTIMATE),
        UisIndicator("26637", "persons"),
        UisIndicator("MOR.5T8.40510", "%", UIS_ESTIMATE),
        UisIndicator("EXPGDP.TOT", "% of GDP"),
        UisIndicator("RESDEN.INHAB.TFTE", "per million inhabitants (FTE)"),
        UisIndicator("FRESP.THC", "% of researchers (headcount)"),
    ),
)
INDICATORS: tuple[UisIndicator, ...] = tuple(i for b in BATCHES for i in b)
INDICATORS_BY_CODE = {i.code: i for i in INDICATORS}
RECORD_FIELDS = ("indicatorId", "geoUnit", "year", "value", "magnitude", "qualifier", "footnotes")


class UisParseError(SourceResponseError):
    pass


def _err(msg: str) -> UisParseError:
    return UisParseError(msg, retryable=False, kind="schema_drift")


def data_url(codes: Sequence[str], version: str, countries: Sequence[str] | None = None) -> str:
    q = [("indicator", c) for c in codes]
    q += [("geoUnit", g) for g in countries] if countries else [("geoUnitType", "NATIONAL")]
    q += [("version", version), ("indicatorMetadata", "true"), ("footnotes", "true")]
    return f"{BASE}/data/indicators?" + "&".join(f"{k}={quote(v, safe='')}" for k, v in q)


class UisConnector:
    """UNESCO UIS Data API connector for the atlas (archives raw; :meth:`atlas_frame` builds the table)."""

    ATLAS_SOURCE = "uis"
    ATLAS_DATASET = "api"
    GEO_SCHEME = "iso3"
    RIGHTS = RIGHTS

    def __init__(self, batches: Sequence[Sequence[str]] | None = None, countries: Sequence[str] | None = None) -> None:
        if batches is None:
            batches = [[i.code for i in b] for b in BATCHES]
        unknown = [c for b in batches for c in b if c not in INDICATORS_BY_CODE]
        if unknown:
            raise ValueError(f"indicators not in the curated list: {unknown}")
        self.batches = tuple(tuple(b) for b in batches if b)
        self.countries = tuple(sorted(countries)) if countries else None

    @property
    def indicators(self) -> tuple[str, ...]:
        return tuple(c for b in self.batches for c in b)

    # ------------------------------------------------------------------ run protocol (atlas_healthedu_common)
    def items(self) -> tuple[FetchItem, ...]:
        return (FetchItem(key="uis.version", url=VERSION_URL, max_bytes=100_000, params={"kind": "version"}),)

    def data_items(self, version: str) -> tuple[FetchItem, ...]:
        return tuple(
            FetchItem(
                key=f"uis.data:{n}",
                url=data_url(codes, version, self.countries),
                max_bytes=MAX_ITEM_BYTES,
                params={
                    "kind": "data",
                    "batch": n,
                    "indicators": list(codes),
                    "version": version,
                    "depends_on": "uis.version",
                },
            )
            for n, codes in enumerate(self.batches, start=1)
        )

    def follow_ups(self, item: FetchItem, body: bytes) -> tuple[FetchItem, ...]:
        """The data queries, pinned to the version the version item returned."""
        if item.params.get("kind") != "version":
            return ()
        return self.data_items(parse_version(body)["version"])

    def check(self, item: FetchItem, body: bytes) -> dict[str, Any]:
        """Structural check of one archived response (drift / empty / at the record cap -> ``UisParseError``)."""
        if item.params.get("kind") == "version":
            return parse_version(body)
        records, meta = parse_data(body, tuple(item.params["indicators"]))
        if not records:
            raise _err(f"batch {item.params.get('batch')}: the UIS query returned no records")
        if len(records) >= RECORD_CAP:
            raise _err(f"{len(records)} records: at the API's 100k cap, the response may be truncated")
        return {"records": len(records), "hints": meta["hints"]}

    # ------------------------------------------------------------------ atlas table
    def atlas_frame(self, bodies: Mapping[str, bytes], raws: Mapping[str, str]) -> tuple[Any, str, dict[str, Any]]:
        v = parse_version(bodies["uis.version"])
        rows: list[dict[str, Any]] = []
        names: dict[str, str] = {}
        last_update: dict[str, str] = {}
        absent: list[str] = []
        for n, codes in enumerate(self.batches, start=1):
            key = f"uis.data:{n}"
            records, meta = parse_data(bodies[key], codes)
            names.update(meta["names"])
            last_update.update(meta["last_update"])
            absent += [c for c in codes if c not in meta["names"]]
            rows.extend(atlas_rows(records, raw_sha256=raws[key], item_key=key, version=v["version"]))
        if absent:
            raise ValidationProblem(
                "UIS indicator metadata missing for requested indicators: registration refused", indicators=absent
            )
        frame = _frame(rows)
        note = (
            f"UNESCO UIS Data API, version {v['version']} ({v.get('release', '')}), {len(self.indicators)} "
            f"indicators, national geo units{'; subset ' + ','.join(self.countries) if self.countries else ''}. "
            "Values as served (float artefacts kept); qualifier/magnitude/footnotes verbatim in flags."
        )
        return frame, note, {"uis_version": v, "indicator_names": names, "last_data_update": last_update}


# ---------------------------------------------------------------------- parsing (pure functions)
def parse_version(body: bytes) -> dict[str, Any]:
    try:
        doc = json.loads(body)
        version = doc["version"]
    except (ValueError, KeyError, TypeError) as e:
        raise _err("version response is not the expected JSON") from e
    if not isinstance(version, str) or not version or len(version) > 64:
        raise _err(f"unexpected version value {version!r}")
    themes = {t.get("theme"): t.get("description") for t in doc.get("themeDataStatus") or [] if isinstance(t, dict)}
    return {
        "version": version,
        "publication_date": doc.get("publicationDate"),
        "release": themes.get("EDUCATION") or "",
    }


def parse_data(body: bytes, codes: Sequence[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        doc = json_decimal(body)
        records = doc["records"]
        metadata = doc.get("indicatorMetadata") or []
        hints = doc.get("hints") or []
    except (ValueError, KeyError, TypeError) as e:
        raise _err("data response is not the expected JSON") from e
    if not isinstance(records, list):
        raise _err("'records' is not a list")
    wanted = set(codes)
    for i, r in enumerate(records):
        if not isinstance(r, dict):
            raise _err(f"record {i} is not an object")
        missing = [f for f in RECORD_FIELDS if f not in r]
        if missing:
            raise _err(f"record {i} lacks {missing}")
        if r["indicatorId"] not in wanted:
            raise _err(f"record {i} is for unrequested indicator {r['indicatorId']!r}")
        if not isinstance(r["year"], Decimal) or r["year"] != int(r["year"]):
            raise _err(f"record {i} has a non-integer year {r['year']!r}")
        if r["value"] is not None and not isinstance(r["value"], Decimal):
            raise _err(f"record {i} value is not a number ({r['value']!r})")
        if not isinstance(r["footnotes"], list):
            raise _err(f"record {i} footnotes is not a list")
        if not isinstance(r["geoUnit"], str) or not r["geoUnit"]:
            raise _err(f"record {i} has no geoUnit")
    names, last = {}, {}
    for m in metadata:
        if isinstance(m, dict) and m.get("indicatorCode") in wanted:
            names[m["indicatorCode"]] = str(m.get("name") or "")
            last[m["indicatorCode"]] = str(m.get("lastDataUpdate") or "")
    return records, {
        "names": names,
        "last_update": last,
        "hints": [str(h)[:300] for h in hints][:20],
        "indicators_with_records": sorted({r["indicatorId"] for r in records}),
    }


def atlas_rows(
    records: Sequence[Mapping[str, Any]], *, raw_sha256: str, item_key: str, version: str
) -> list[dict[str, Any]]:
    from reva.atlas.tables import missing_flags, year_period

    out: list[dict[str, Any]] = []
    for idx, r in enumerate(records):
        ind = INDICATORS_BY_CODE[r["indicatorId"]]
        start, end = year_period(int(r["year"]))
        flags: dict[str, Any] = {
            "estimate_class": ind.estimate_class,
            "classified_by": CLASSIFIED_BY,
            "uis_data_version": version,
        }
        if r["qualifier"]:
            flags["qualifier"] = str(r["qualifier"])
        if r["magnitude"]:
            flags["magnitude"] = str(r["magnitude"])
        if r["footnotes"]:
            flags["footnotes"] = [
                {k: str(f.get(k)) for k in ("type", "subtype", "value") if f.get(k) is not None}
                for f in r["footnotes"]
                if isinstance(f, dict)
            ]
        if ind.note:
            flags["indicator_note"] = ind.note
        value = None if r["value"] is None else dec_str(r["value"])
        flags_s = (
            fit_flags(flags, long_keys=("footnotes",))
            if value is not None
            else missing_flags("not_reported", **json.loads(fit_flags(flags, long_keys=("footnotes",))))
        )
        out.append(
            {
                "source": UisConnector.ATLAS_SOURCE,
                "dataset": UisConnector.ATLAS_DATASET,
                "indicator": str(r["indicatorId"]),
                "concept_id": CONCEPT_HINTS.get(str(r["indicatorId"])),
                "iso3": None,
                "provider_geo": str(r["geoUnit"]),
                "entity_qid": None,
                "period_start": start,
                "period_end": end,
                "value": value,
                "value_text": None,
                "unit": ind.unit,
                "flags": flags_s,
                "raw_sha256": raw_sha256,
                "selector": canon(
                    {
                        "item": item_key,
                        "record": idx,
                        "indicatorId": r["indicatorId"],
                        "geoUnit": r["geoUnit"],
                        "year": int(r["year"]),
                    }
                ),
            }
        )
    return out


def _frame(rows: Sequence[Mapping[str, Any]]) -> Any:
    import polars as pl

    from reva.atlas.tables import ATLAS_SCHEMA

    return pl.DataFrame(list(rows), schema=ATLAS_SCHEMA)


#: Fixture mode (``--fixture`` / tests) replays the captured subset in tests/fixtures/source/atlas.unesco_uis.
FIXTURE_BATCHES = (("LR.AG15T99", "LR.AG15T99.F"), ("XGDP.FSGOV", "EXPGDP.TOT", "CR.MOD.1"))
FIXTURE_COUNTRIES = ("DZA", "FRA", "IND", "NGA", "TUR")


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=UisConnector.ATLAS_SOURCE,
        provider="UNESCO Institute for Statistics",
        licence=RIGHTS["licence"],
        allowed_hosts=(HOST,),
        datasets=(UisConnector.ATLAS_DATASET,),
        dataset_ids=tuple(i.code for i in INDICATORS),
        cadence_seconds=30 * 86400,
        planned_requests=1 + len(BATCHES),
        max_response_bytes=MAX_ITEM_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=module_digest(UisConnector),
        field_mappings={
            "value": "value",
            "flags": ["qualifier", "magnitude", "footnotes"],
            "batches": [[i.code for i in b] for b in BATCHES],
            "units": {i.code: i.unit for i in INDICATORS},
        },
        notes="Data queries pinned to the current UIS data version; a partial run registers nothing.",
    )


def connector_for(run: Any) -> UisConnector:
    if run.origin == "test_fixture":
        return UisConnector(FIXTURE_BATCHES, FIXTURE_COUNTRIES)
    return UisConnector()


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """ATLASCORE entry point (``uv run python -m reva.atlas.ingest atlas.unesco_uis``)."""
    return await fetch_archive_register(run, connector_for(run), dataset=UisConnector.ATLAS_DATASET)


#: UIS indicator -> catalog concept (registry/atlas/catalog/education.yaml; a test keeps them in sync).
CONCEPT_HINTS: dict[str, str] = {}


def _load_hints() -> None:
    from reva.ontology.registry import REGISTRY_ROOT

    path = REGISTRY_ROOT / "atlas" / "catalog" / "education.yaml"
    if not path.exists():
        return
    import yaml

    doc = yaml.safe_load(path.read_text())
    prefix = f"{UisConnector.ATLAS_SOURCE}.{UisConnector.ATLAS_DATASET}."
    for c in doc.get("concepts") or []:
        for s in c.get("source_order") or []:
            key = s if isinstance(s, str) else f"{s['source']}.{s['dataset']}.{s['indicator']}"
            if key.startswith(prefix):
                CONCEPT_HINTS.setdefault(key[len(prefix) :], c["id"])


_load_hints()

__all__ = [
    "BATCHES",
    "CONNECTOR_NAME",
    "INDICATORS",
    "RIGHTS",
    "SOURCE_KEY",
    "UisConnector",
    "atlas_ingest",
    "atlas_rows",
    "atlas_source",
    "data_url",
    "parse_data",
    "parse_version",
]
