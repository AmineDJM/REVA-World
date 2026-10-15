"""World Bank Indicators API v2 connector (template worldbank_v2).

Fetches country metadata, indicator metadata (definition/source notes) and the
SP.POP.TOTL / NY.GDP.MKTP.CD series for FRA, DZA, CHN, USA, 2000-2024, fully
consuming pagination. Nulls stay typed Missing; dataset-level 'lastupdated' is
kept as metadata and never used as a source publication time.

Validation of series pages blocks (the acquisition is quarantined, nothing is
coerced) when a row's provider markers differ from what the reviewed mapping
expects: the row's ``indicator.id`` must be the requested indicator, ``unit``
and ``decimal`` must be present and equal the reviewed values in
``INDICATORS`` (a unit change such as "" -> "thousands" means the values are on
a different scale), and ``value`` must be a JSON number or null (a string-typed
number is a type drift, not a value). ``decimal`` is the provider's display
precision; a change is not necessarily a value change, but it is reviewed
before anything is published. A silent rescale that keeps every marker
unchanged cannot be told apart from a genuine revision in one payload: it is
detected by the repair advisor's comparison with the accepted vintage of the
same request (``reva.autonomy.repair.value_comparison``), not here.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from reva.adapters.connectors.base import (
    dec_str,
    json_decimal,
    module_digest,
    plain,
    schema_fingerprint,
    to_raw,
)
from reva.adapters.http.safe_client import require_json
from reva.kernel.errors import SourceResponseError
from reva.kernel.support import PopulationRef, SpatialRef, Support
from reva.kernel.time import Precision, TimeExtent
from reva.kernel.uncertainty import UncertaintySpec
from reva.kernel.values import MeasurementBasis
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

BASE = "https://api.worldbank.org/v2"
INDICATORS: dict[str, dict[str, Any]] = {
    "SP.POP.TOTL": {
        "concept": "demography.population.wdi_midyear_total",
        "unit": "unit.persons",
        # Reviewed provider markers of every series row (captured live 2026-10-08).
        "provider_unit": "",
        "provider_decimal": "0",
        "definition_must_contain": ["midyear"],
    },
    "NY.GDP.MKTP.CD": {
        "concept": "economics.gdp.nominal_current_usd",
        "unit": "unit.usd",
        "provider_unit": "",
        "provider_decimal": "0",
        "definition_must_contain": ["current prices", "United States dollars"],
    },
}


class WorldBankConnector:
    def __init__(
        self,
        countries: Sequence[str] = ("FRA", "DZA", "CHN", "USA"),
        years: tuple[int, int] = (2000, 2024),
        per_page: int = 1000,
    ) -> None:
        self.countries = tuple(countries)
        self.years = years
        self.per_page = per_page

    def describe(self) -> ConnectorSpec:
        return ConnectorSpec(
            name="worldbank.wdi.indicators",
            version="1.0.0",
            template="worldbank_v2",
            source_key="worldbank.wdi",
            provider="World Bank",
            licence="CC-BY-4.0",
            allowed_hosts=("api.worldbank.org",),
            dataset_ids=tuple(INDICATORS),
            field_mappings={k: v["concept"] for k, v in INDICATORS.items()},
            units={k: v["unit"] for k, v in INDICATORS.items()},
            geography=self.countries,
            time_semantics="annual; population=midyear point estimate; GDP=annual total",
            lineage_family="worldbank_wdi",
            cadence_seconds=30 * 86400,
            revision_strategy="append revision when latest semantic state changes",
            max_response_bytes=5_000_000,
            max_pages=50,
            parser_version="wb-parse-1",
            code_digest=module_digest(WorldBankConnector),
        )

    def _series_url(self, indicator: str, page: int) -> str:
        c = ";".join(self.countries)
        return (
            f"{BASE}/country/{c}/indicator/{indicator}?format=json"
            f"&date={self.years[0]}:{self.years[1]}&per_page={self.per_page}&page={page}"
        )

    def plan(self, cursor: Cursor | None, budget: FetchBudget) -> FetchPlan:
        items = [
            FetchItem(
                key="countries",
                url=f"{BASE}/country/{';'.join(self.countries)}?format=json&per_page=100",
                max_bytes=1_000_000,
                params={"kind": "country_metadata"},
            )
        ]
        for ind in INDICATORS:
            items.append(
                FetchItem(
                    key=f"indicator:{ind}",
                    url=f"{BASE}/indicator/{ind}?format=json",
                    max_bytes=500_000,
                    params={"kind": "indicator_metadata", "indicator": ind},
                )
            )
            items.append(
                FetchItem(
                    key=f"series:{ind}:p1",
                    url=self._series_url(ind, 1),
                    max_bytes=5_000_000,
                    params={"kind": "series", "indicator": ind, "page": 1, "depends_on": f"indicator:{ind}"},
                )
            )
        return FetchPlan(
            connector="worldbank.wdi.indicators",
            items=tuple(items[: budget.max_requests]),
            pagination="page/pages header; follow-up pages planned after page 1",
            availability_expectation="annual updates; no per-value publication timestamp",
            budget_exhausted=len(items) > budget.max_requests,
        )

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition:
        result = await http.get(item.url, max_bytes=item.max_bytes)
        return to_raw(item, result, getattr(http, "origin", "live"))

    # -------------------------------------------------------------- parsing
    def _payload(self, raw: RawAcquisition) -> list[Any]:
        require_json_like(raw)
        data = json_decimal(raw.body)
        if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict) and "message" in data[0]:
            raise SourceResponseError(f"World Bank API error: {plain(data[0]['message'])}", retryable=False)
        if not (isinstance(data, list) and len(data) == 2 and isinstance(data[0], dict)):
            raise SourceResponseError("unexpected World Bank payload shape", retryable=False, kind="schema_drift")
        return data

    def parse(self, raw: RawAcquisition) -> Iterator[ObservationBatch]:
        kind = raw.item.params.get("kind")
        header, rows = self._payload(raw)
        rows = rows or []
        meta = {"page_header": plain(header), "kind": kind}
        fp = schema_fingerprint([header, rows[:5]])
        if kind == "country_metadata":
            meta["countries"] = [
                {
                    "id": r.get("id"),
                    "iso2Code": r.get("iso2Code"),
                    "name": r.get("name"),
                    "region": (r.get("region") or {}).get("value"),
                    "incomeLevel": (r.get("incomeLevel") or {}).get("value"),
                    "capitalCity": r.get("capitalCity"),
                    "longitude": plain(r.get("longitude")),
                    "latitude": plain(r.get("latitude")),
                }
                for r in rows
            ]
            yield ObservationBatch(connector="worldbank.wdi.indicators", connector_version="1.0.0",
                                   item_key=raw.item.key, observations=(), metadata=meta, schema_fingerprint=fp)
            return
        if kind == "indicator_metadata":
            r = rows[0] if rows else {}
            meta["indicator"] = {
                "id": r.get("id"),
                "name": r.get("name"),
                "sourceNote": r.get("sourceNote"),
                "sourceOrganization": r.get("sourceOrganization"),
                "source": (r.get("source") or {}).get("value"),
            }
            yield ObservationBatch(connector="worldbank.wdi.indicators", connector_version="1.0.0",
                                   item_key=raw.item.key, observations=(), metadata=meta, schema_fingerprint=fp)
            return
        indicator = raw.item.params["indicator"]
        page = int(header.get("page", 1))
        pages = int(header.get("pages", 1))
        follow = ()
        if page == 1 and pages > 1:
            follow = tuple(
                FetchItem(
                    key=f"series:{indicator}:p{p}",
                    url=self._series_url(indicator, p),
                    max_bytes=raw.item.max_bytes,
                    params={"kind": "series", "indicator": indicator, "page": p},
                )
                for p in range(2, pages + 1)
            )
        meta["dataset_last_updated"] = header.get("lastupdated")  # dataset-level; NOT a publication time
        meta["requested_indicator"] = indicator
        meta["row_markers"] = _row_markers(rows)
        obs = tuple(self._observation(indicator, r) for r in rows)
        expected = ()
        if pages == 1:
            expected = tuple(
                f"wb:{indicator}:{c}:{y}" for c in self.countries for y in range(self.years[0], self.years[1] + 1)
            )
        yield ObservationBatch(
            connector="worldbank.wdi.indicators",
            connector_version="1.0.0",
            item_key=raw.item.key,
            observations=obs,
            expected_keys=expected,
            metadata=meta,
            schema_fingerprint=fp,
            follow_up=follow,
        )

    def _observation(self, indicator: str, r: dict[str, Any]) -> ParsedObservation:
        spec = INDICATORS[indicator]
        iso3 = r.get("countryiso3code") or ""
        year = int(str(r["date"]))
        if spec["concept"].startswith("demography"):
            temporal = TimeExtent(kind="instant", precision=Precision.day, start=date(year, 7, 1),
                                  label=f"{year} midyear")
            agg = "point_in_time_stock"
            universe = "de_facto_residents_wdi"
            basis = MeasurementBasis()
        else:
            temporal = TimeExtent.calendar_year(year)
            agg = "period_flow_total"
            universe = "resident_producers_of_economy"
            basis = MeasurementBasis(
                currency="USD",
                price_basis="nominal",
                exchange_rate_transform="provider_converted_from_LCU (WDI methodology)",
            )
        support = Support(
            temporal=temporal,
            spatial=SpatialRef(scheme="provider_reporter_territory", key=f"WB:{iso3}", authority="World Bank"),
            population=PopulationRef(universe=universe, definition_source=f"WDI {indicator} metadata"),
            aggregation=agg,  # type: ignore[arg-type]
        )
        v = r.get("value")
        if v is None:
            value: dict[str, Any] = {"kind": "missing", "reason": "unavailable"}
        else:
            value = {
                "kind": "decimal",
                "value": dec_str(v),
                "unit_ref": spec["unit"],
                "basis": basis.model_dump(mode="json", exclude_none=True),
            }
        return ParsedObservation(
            subject_provider="worldbank",
            subject_key=iso3,
            concept_id=spec["concept"],
            concept_version="1.0.0",
            support=support,
            logical_key=f"wb:{indicator}:{iso3}:{year}",
            value=value,
            uncertainty=UncertaintySpec.unknown("World Bank WDI publishes no uncertainty for this value"),
            source_flags={
                "indicator": indicator,
                "obs_status": r.get("obs_status") or "",
                "decimal": plain(r.get("decimal")),
                "unit_field": r.get("unit") or "",
                "provider_country_id": (r.get("country") or {}).get("id"),
            },
            raw_selector={"json_pointer_hint": f"/1/*[countryiso3code={iso3},date={year}]",
                          "match": {"countryiso3code": iso3, "date": str(year)}},
        )

    # -------------------------------------------------------------- validation
    def validate(self, batch: ObservationBatch) -> QualityReport:
        findings: list[Finding] = []
        header = batch.metadata.get("page_header", {})
        kind = batch.metadata.get("kind")
        if kind == "indicator_metadata":
            ind = batch.metadata.get("indicator", {})
            note = (ind.get("sourceNote") or "").lower()
            required = INDICATORS.get(ind.get("id") or "", {}).get("definition_must_contain", [])
            for word in required:
                if word.lower() not in note:
                    findings.append(Finding(code="semantic_drift", severity="blocker",
                                            message=f"indicator definition no longer mentions '{word}'"))
            return _report(findings, 0, 0, ())
        if kind == "series":
            per_page = int(str(header.get("per_page", "0")))
            total = int(str(header.get("total", "0")))
            page = int(str(header.get("page", "1")))
            expected_rows = max(0, min(per_page, total - (page - 1) * per_page))
            if len(batch.observations) != expected_rows:
                findings.append(Finding(
                    code="pagination_truncated", severity="blocker",
                    message=f"page has {len(batch.observations)} rows, header implies {expected_rows}"))
            findings.extend(_marker_findings(batch.metadata))
            keys = [o.logical_key for o in batch.observations]
            if len(keys) != len(set(keys)):
                findings.append(Finding(code="duplicate_keys", severity="blocker", message="duplicate series keys"))
            missing = tuple(sorted(set(batch.expected_keys) - set(keys)))
            if missing:
                findings.append(Finding(code="incomplete", severity="blocker",
                                        message=f"{len(missing)} expected keys absent"))
            unknown = [o.subject_key for o in batch.observations if o.subject_key not in self.countries]
            if unknown:
                findings.append(Finding(code="unexpected_subject", severity="blocker",
                                        message=f"unexpected countries {sorted(set(unknown))}"))
            n_missing = sum(1 for o in batch.observations if o.value.get("kind") == "missing")
            if n_missing:
                findings.append(Finding(code="missing_values", severity="info",
                                        message=f"{n_missing} values are null at source (kept as Missing)"))
            return _report(findings, len(batch.expected_keys), len(keys), missing)
        return _report(findings, 0, 0, ())

    def next_cursor(self, plan: FetchPlan, outcomes: Sequence[FetchOutcome]) -> Cursor:
        ok = all(o.status in ("published", "unchanged") for o in outcomes) and not plan.budget_exhausted
        return Cursor(state={"complete": ok, "years": list(self.years)})


def _value_type(r: dict[str, Any], key: str) -> str:
    if key not in r:
        return "absent"
    v = r[key]
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, Decimal | int | float):
        return "number"
    return "string" if isinstance(v, str) else type(v).__name__


def _row_markers(rows: Sequence[Any]) -> list[dict[str, Any]]:
    """Distinct provider markers of the series rows, as observed (absent kept distinct from null/"")."""
    seen: Counter[tuple[Any, ...]] = Counter()
    for r in rows:
        if not isinstance(r, dict):
            seen[(None, False, None, False, None, "not_an_object")] += 1
            continue
        ind = r.get("indicator")
        seen[(
            plain(ind.get("id")) if isinstance(ind, dict) else None,
            "unit" in r, plain(r.get("unit")),
            "decimal" in r, plain(r.get("decimal")),
            _value_type(r, "value"),
        )] += 1
    keys = ("indicator", "unit_present", "unit", "decimal_present", "decimal", "value_type")
    return [{**dict(zip(keys, k, strict=True)), "rows": n} for k, n in sorted(seen.items(), key=lambda kv: str(kv[0]))]


def _marker_findings(meta: dict[str, Any]) -> list[Finding]:
    """Blockers for rows whose indicator, unit, decimal or value type differ from the reviewed mapping."""
    indicator = meta.get("requested_indicator")
    spec = INDICATORS.get(str(indicator))
    markers = meta.get("row_markers")
    if spec is None or markers is None:
        return [Finding(code="mapping_unverifiable", severity="blocker",
                        message=f"series page for {indicator!r} carries no reviewed row markers")]
    out: list[Finding] = []
    for m in markers:
        n = m["rows"]
        if m["indicator"] != indicator:
            out.append(Finding(code="indicator_mismatch", severity="blocker",
                               message=f"{n} rows carry indicator {m['indicator']!r}, requested {indicator}"))
        if not m["unit_present"] or m["unit"] != spec["provider_unit"]:
            got = repr(m["unit"]) if m["unit_present"] else "no unit field"
            out.append(Finding(code="unit_drift", severity="blocker",
                               message=f"{n} rows carry unit {got}; reviewed mapping expects "
                                       f"{spec['provider_unit']!r} (values may be on another scale)"))
        if not m["decimal_present"] or str(m["decimal"]) != spec["provider_decimal"]:
            got = repr(m["decimal"]) if m["decimal_present"] else "no decimal field"
            out.append(Finding(code="decimal_drift", severity="blocker",
                               message=f"{n} rows carry decimal {got}; reviewed mapping expects "
                                       f"{spec['provider_decimal']}"))
        if m["value_type"] not in ("number", "null"):
            out.append(Finding(code="value_type_drift", severity="blocker",
                               message=f"{n} rows carry a {m['value_type']} value; reviewed mapping expects a "
                                       "JSON number or null (never coerced)"))
    return out


def require_json_like(raw: RawAcquisition) -> None:
    from reva.adapters.http.safe_client import HttpResult

    require_json(
        HttpResult(raw.item.url, raw.item.url, 200, {"content-type": raw.media_type}, raw.body,
                   raw.retrieved_at, 0.0, (), 1)
    )


def _report(findings: list[Finding], expected: int, received: int, missing: tuple[str, ...]) -> QualityReport:
    outcome = "blocked" if any(f.severity == "blocker" for f in findings) else "pass"
    return QualityReport(outcome=outcome, findings=tuple(findings), expected_count=expected,
                         received_count=received, missing_keys=missing)
