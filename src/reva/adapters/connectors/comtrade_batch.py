"""UN Comtrade public-preview connector for every crosswalk reporter (V2 step 1).

Same estimands, logical keys, supports and source flags as the reviewed
``un.comtrade.public_exact`` connector (``comtrade.py``), so a country both
connectors cover shares one source lineage (an identical re-read is
``unchanged``, never a second report). What differs is request shape and scope:

* **Reporters**: every Comtrade reporter mapped in
  ``registry/entities/country_crosswalk.json`` (composite, "nes", group and
  historical codes are not mapped there and are never requested as a country).
* **Multi-reporter requests.** The public preview accepts a comma-separated
  ``reporterCode`` list and ``flowCode=M,X`` in one request, and refuses more
  than one period ("Maximum number of periods for preview is 1", HTTP 400; live
  probes 2026-10-15, docs/dev/v2_allc.md). One request is therefore one period
  and one batch of reporters. World totals: up to ``TOTAL_BATCH`` reporters per
  request (two rows per reporter at most, far below ``maxRecords``).
  Bilateral rows: ``BILATERAL_BATCH`` reporters per request; a response that
  reaches ``maxRecords`` is blocked as possible truncation (nothing from it is
  published) and the batch is re-requested in two halves as follow-ups.
* **Rotation.** The full request list (all world-total batches for every year,
  then the bilateral batches) is a deterministic sequence of units. One run
  plans at most ``max_requests_per_run`` units starting at the cursor offset;
  the cursor advances when the run's required items are published, wrapping to
  the start (next sweep) at the end. A daily schedule therefore covers every
  reporter within a few days while each run stays far below the per-source
  daily cap of ``registry/policies/budgets.yaml``.
* **Missing is per reporter.** A requested reporter with no row for a flow and
  period gets a typed ``Missing(unavailable)`` with ``provider_returned_no_rows``
  (as the single-reporter connector does): the request asked for it, the
  provider returned nothing, which is not zero.
* **Unreviewed HS editions are per row.** A row reported under a known HS
  edition outside the reviewed mapping (H0-H3) is parsed and stored with its
  ``classificationCode``; the eligibility scope of the mapping (H4-H6) keeps it
  out of every release (``evidence.observation_is_eligible``), and the batch
  carries a ``classification_unreviewed_ineligible`` warning. A code that is not a
  known HS edition (e.g. ``H7``) still blocks the whole batch, as in 1.2.0:
  in a multi-reporter batch one unknown edition must not be silently mixed in.
* **Bilateral partners** keep the provider partner code; composite partners
  (e.g. 899 "Areas, nes", 490 "Other Asia, nes") stay provider-declared partner
  entities and are never mapped to a country. Partner 0 (World) rows of a
  bilateral response are skipped (captured by the world-total requests).
"""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Iterator, Sequence
from decimal import Decimal
from typing import Any

from reva.adapters.connectors import comtrade as base
from reva.adapters.connectors.base import dec_str, json_decimal, plain, schema_fingerprint
from reva.adapters.connectors.comtrade import (
    FIXED_DIMS,
    FLOW_CONCEPT,
    MAX_RECORDS,
    REVIEWED_CLASSIFICATIONS,
    VALUATION_UNDETERMINED,
    WEIGHT_CONCEPT,
    ComtradeConnector,
    _row_structure,
    _structure_findings,
)
from reva.adapters.connectors.worldbank import _report, require_json_like
from reva.kernel.errors import SourceResponseError
from reva.kernel.ids import logical_hash
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
    RelationSpec,
)

CONNECTOR_NAME = "un.comtrade.public_batch"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "comtrade-batch-parse-1"
BASE_URL = base.BASE
TOTAL_YEARS = (2019, 2020, 2021, 2022, 2023, 2024)
BILATERAL_YEARS = (2023,)
#: World-total request: <= 2 rows per reporter (flows M, X) -> 220 rows for 110 reporters < MAX_RECORDS.
TOTAL_BATCH = 110
#: Bilateral request: the largest 2023 reporter (NLD) returned 112 rows for both flows.
BILATERAL_BATCH = 4
#: Default units per scheduled run (budgets.yaml: un.comtrade.public_preview 100 requests/day,
#: shared with the 4-country connector's monthly 48-request run).
MAX_REQUESTS_PER_RUN = 30
#: Known HS editions that are not (yet) reviewed for HS2709: stored per row, ineligible by scope.
KNOWN_UNREVIEWED_EDITIONS = ("H0", "H1", "H2", "H3")
FLOWS = ("M", "X")


def _code_digest() -> str:
    """Hash of this module and the module it reuses parsing helpers from."""
    h = hashlib.sha256()
    for mod in (base, inspect.getmodule(_code_digest)):
        h.update(inspect.getsource(mod).encode())  # type: ignore[arg-type]
    return "sha256:" + h.hexdigest()


def _chunks(seq: Sequence[Any], n: int) -> list[list[Any]]:
    return [list(seq[i : i + n]) for i in range(0, len(seq), n)]


class ComtradeBatchConnector(ComtradeConnector):
    """All crosswalk reporters, multi-reporter one-period requests, rotating over runs."""

    def __init__(
        self,
        reporters: dict[str, int] | None = None,
        total_years: Sequence[int] = TOTAL_YEARS,
        bilateral_years: Sequence[int] = BILATERAL_YEARS,
        *,
        max_requests_per_run: int = MAX_REQUESTS_PER_RUN,
        total_batch: int = TOTAL_BATCH,
        bilateral_batch: int = BILATERAL_BATCH,
    ) -> None:
        if reporters is None:
            from reva.adapters.connectors.country_crosswalk import comtrade_reporters

            reporters = comtrade_reporters()
        self.codes: dict[str, int] = dict(sorted(reporters.items()))
        self.by_code: dict[int, str] = {c: k for k, c in self.codes.items()}
        if len(self.by_code) != len(self.codes):
            raise ValueError("two entities share one Comtrade reporter code")
        super().__init__(
            reporters=tuple(self.codes), total_years=tuple(total_years), bilateral_years=tuple(bilateral_years)
        )
        self.max_requests_per_run = max(1, int(max_requests_per_run))
        self.total_batch = max(1, int(total_batch))
        self.bilateral_batch = max(1, int(bilateral_batch))
        self._slice: dict[str, Any] = {}

    # ------------------------------------------------------------------ description
    def describe(self) -> ConnectorSpec:
        return ConnectorSpec(
            name=CONNECTOR_NAME,
            version=CONNECTOR_VERSION,
            template="comtrade_public_exact",
            source_key="un.comtrade.public_preview",
            provider="United Nations Statistics Division",
            licence="UN Comtrade terms of use",
            allowed_hosts=("comtradeapi.un.org",),
            dataset_ids=("C/A/HS/2709",),
            field_mappings={
                **{f"{k[0]}:{k[1]}": v for k, v in FLOW_CONCEPT.items()},
                **{f"{k}:netWgt:total": v for k, v in WEIGHT_CONCEPT.items()},
            },
            units={"primaryValue": "unit.usd", "netWgt": "unit.kg"},
            geography=tuple(self.codes),
            time_semantics="annual calendar-year flow totals",
            lineage_family="un_comtrade",
            cadence_seconds=86400,
            revision_strategy="rotating sweep over reporter batches; append revision on changed latest state",
            max_response_bytes=5_000_000,
            max_pages=1,
            parser_version=PARSER_VERSION,
            code_digest=_code_digest(),
        )

    # ------------------------------------------------------------------ planning
    def _batch_url(self, codes: Sequence[int], year: int, partner: int | None) -> str:
        url = (
            f"{BASE_URL}?period={year}&reporterCode={','.join(map(str, codes))}&cmdCode=2709&flowCode={','.join(FLOWS)}"
        )
        if partner is not None:
            url += f"&partnerCode={partner}"
        return url + f"&partner2Code=0&customsCode=C00&motCode=0&maxRecords={MAX_RECORDS}"

    def _item(self, scope: str, year: int, codes: Sequence[int], *, split: str = "") -> FetchItem:
        codes = sorted(codes)
        tag = f"{codes[0]}-{codes[-1]}x{len(codes)}{split}"
        return FetchItem(
            key=f"{scope}:{year}:{tag}",
            url=self._batch_url(codes, year, 0 if scope == "total" else None),
            max_bytes=5_000_000,
            params={"scope": scope, "year": year, "reporters": list(codes), "flows": list(FLOWS)},
            required=(scope == "total"),
        )

    def units(self) -> list[FetchItem]:
        """The full, deterministic sweep (world totals for every year first, then bilateral)."""
        codes = sorted(self.by_code)
        items = [self._item("total", y, b) for y in self.total_years for b in _chunks(codes, self.total_batch)]
        items += [
            self._item("bilateral", y, b) for y in self.bilateral_years for b in _chunks(codes, self.bilateral_batch)
        ]
        return items

    def plan(self, cursor: Cursor | None, budget: FetchBudget) -> FetchPlan:
        units = self.units()
        digest = logical_hash([u.url for u in units])
        state = cursor.state if cursor else {}
        offset = int(state.get("offset", 0)) if state.get("plan_digest") == digest else 0
        if not 0 <= offset < len(units):
            offset = 0
        n = max(0, min(self.max_requests_per_run, budget.max_requests, len(units) - offset))
        items = units[offset : offset + n]
        sweep = int(state.get("sweep", 0)) if state.get("plan_digest") == digest else 0
        self._slice = {"offset": offset, "end": offset + n, "units": len(units), "plan_digest": digest, "sweep": sweep}
        return FetchPlan(
            connector=CONNECTOR_NAME,
            items=tuple(items),
            pagination="none: one period and one reporter batch per request; maxRecords bound; a batch reaching "
            "maxRecords is blocked and re-requested in halves",
            availability_expectation="annual data released with lags; reporters report at different times",
            budget_exhausted=n == 0 and len(units) > 0,
        )

    def next_cursor(self, plan: FetchPlan, outcomes: Sequence[FetchOutcome]) -> Cursor:
        required = {i.key for i in plan.items if i.required}
        ok = all(o.status in ("published", "unchanged") for o in outcomes if o.item_key in required)
        ok = ok and all(k in {o.item_key for o in outcomes} for k in required)
        s = self._slice or {"offset": 0, "end": 0, "units": 0, "plan_digest": "", "sweep": 0}
        end, sweep = s["end"], s["sweep"]
        if end >= s["units"]:
            end, sweep = 0, sweep + 1
        return Cursor(
            state={
                "complete": ok and not plan.budget_exhausted,
                "offset": end,
                "sweep": sweep,
                "units": s["units"],
                "plan_digest": s["plan_digest"],
                "last_slice": [s["offset"], s["end"]],
                "recheck_years": list(self.total_years),
            }
        )

    # ------------------------------------------------------------------ parsing
    def parse(self, raw: RawAcquisition) -> Iterator[ObservationBatch]:
        require_json_like(raw)
        payload = json_decimal(raw.body)
        if not isinstance(payload, dict) or "data" not in payload:
            raise SourceResponseError("unexpected Comtrade payload shape", retryable=False, kind="schema_drift")
        if payload.get("error"):
            raise SourceResponseError(f"Comtrade API error: {payload['error']}", retryable=False)
        rows: list[dict[str, Any]] = payload.get("data") or []
        p = raw.item.params
        year, codes, scope = int(p["year"]), [int(c) for c in p["reporters"]], p["scope"]
        meta: dict[str, Any] = {
            "count": plain(payload.get("count")),
            "elapsedTime": payload.get("elapsedTime"),
            "returned_rows": len(rows),
            "request": {"scope": scope, "year": year, "reporters": codes, "flows": list(FLOWS)},
            "maxRecords": MAX_RECORDS,
            "rows": [self._dims(r) for r in rows][:MAX_RECORDS],
            "row_structure": _row_structure(rows),
        }
        obs: list[ParsedObservation] = []
        follow: tuple[FetchItem, ...] = ()
        if scope == "total":
            index: dict[tuple[int, str], dict[str, Any]] = {}
            for r in rows:
                try:
                    index.setdefault((int(r.get("reporterCode")), str(r.get("flowCode"))), r)
                except (TypeError, ValueError):
                    continue  # reported as a dimension mismatch by validate
            for code in codes:
                for flow in FLOWS:
                    r = index.get((code, flow))
                    obs.append(self._total_obs(code, flow, year, r, "value"))
                    obs.append(self._total_obs(code, flow, year, r, "weight"))
            expected = tuple(o.logical_key for o in obs)
        else:
            if len(rows) >= MAX_RECORDS and len(codes) > 1:
                half = len(codes) // 2
                follow = (
                    self._item("bilateral", year, codes[:half], split="a"),
                    self._item("bilateral", year, codes[half:], split="b"),
                )
                meta["split_into"] = [i.key for i in follow]
            for r in rows:
                if int(r.get("partnerCode", -1)) == 0:
                    continue  # World is a distinct support captured by the world-total requests
                if int(r.get("reporterCode", -1)) not in self.by_code:
                    continue  # reported as a dimension mismatch by validate
                obs.append(self._bilateral_obs(r))
            expected = ()
        yield ObservationBatch(
            connector=CONNECTOR_NAME,
            connector_version=CONNECTOR_VERSION,
            item_key=raw.item.key,
            observations=tuple(obs),
            expected_keys=expected,
            metadata=meta,
            schema_fingerprint=schema_fingerprint(rows[:5]),
            follow_up=follow,
        )

    @staticmethod
    def _support_for(code: int, year: int, partner: str) -> Support:
        """Identical to ``ComtradeConnector._support`` for the same reporter code."""
        return Support(
            temporal=TimeExtent.calendar_year(year),
            spatial=SpatialRef(
                scheme="provider_reporter_territory" if partner == "0" else "provider_reporter_partner_pair",
                key=f"COMTRADE:{code}" + ("" if partner == "0" else f"->{partner}"),
                authority="UN Comtrade",
                note="reporter territory as defined by provider reporterNote",
            ),
            population=PopulationRef(universe="not_applicable"),
            aggregation="period_flow_total",
            resolution={"classification": "HS", "heading": "2709", "partner": partner},
        )

    def _total_obs(self, code: int, flow: str, year: int, r: dict[str, Any] | None, what: str) -> ParsedObservation:
        """Same value/flags/keys as ``ComtradeConnector._total`` (shared lineage)."""
        if what == "value":
            concept = FLOW_CONCEPT[(flow, "total")]
            lk = f"comtrade:HS2709:{flow}:{code}:0:{year}:primaryValue"
            if r is None:
                value: dict[str, Any] = {"kind": "missing", "reason": "unavailable"}
                flags: dict[str, Any] = {"provider_returned_no_rows": True}
            elif r.get("primaryValue") is None:
                value = {"kind": "missing", "reason": "unavailable"}
                flags = self._value_flags(r)
            else:
                flags = self._value_flags(r)
                value = {
                    "kind": "decimal",
                    "value": dec_str(r["primaryValue"]),
                    "unit_ref": "unit.usd",
                    "basis": self._value_basis(flow, r, flags),
                }
        else:
            concept = WEIGHT_CONCEPT[flow]
            lk = f"comtrade:HS2709:{flow}:{code}:0:{year}:netWgt"
            if r is None:
                value, flags = {"kind": "missing", "reason": "unavailable"}, {"provider_returned_no_rows": True}
            else:
                flags = self._weight_flags(r)
                w = r.get("netWgt")
                if w is None or Decimal(w) <= 0:
                    value = {"kind": "missing", "reason": "unavailable"}
                    flags["weight_unusable"] = True
                else:
                    value = {
                        "kind": "decimal",
                        "value": dec_str(w),
                        "unit_ref": "unit.kg",
                        "basis": {"scale_factor": "1"},
                    }
        flags["flowCode"] = flow
        return ParsedObservation(
            subject_provider="comtrade_reporter",
            subject_key=str(code),
            concept_id=concept,
            concept_version="1.0.0",
            support=self._support_for(code, year, "0"),
            logical_key=lk,
            value=value,
            uncertainty=UncertaintySpec.unknown("UN Comtrade publishes no uncertainty for this value"),
            source_flags=flags,
            raw_selector={
                "match": {"period": str(year), "reporterCode": code, "partnerCode": 0, "flowCode": flow},
                "field": "primaryValue" if what == "value" else "netWgt",
            },
        )

    def _bilateral_obs(self, r: dict[str, Any]) -> ParsedObservation:
        code = int(r["reporterCode"])
        flow = str(r.get("flowCode"))
        year = int(str(r.get("period")))
        partner = str(plain(r.get("partnerCode")))
        v = r.get("primaryValue")
        flags = {**self._value_flags(r), "flowCode": flow, "partnerCode": partner}
        value: dict[str, Any] = (
            {"kind": "missing", "reason": "unavailable"}
            if v is None
            else {
                "kind": "decimal",
                "value": dec_str(v),
                "unit_ref": "unit.usd",
                "basis": self._value_basis(flow, r, flags),
            }
        )
        reporter_dir, partner_dir = ("in", "out") if flow == "M" else ("out", "in")
        return ParsedObservation(
            subject_provider="comtrade_reporter",
            subject_key=str(code),
            concept_id=FLOW_CONCEPT[(flow, "bilateral")],
            concept_version="1.0.0",
            support=self._support_for(code, year, partner),
            logical_key=f"comtrade:HS2709:{flow}:{code}:{partner}:{year}:primaryValue",
            value=value,
            uncertainty=UncertaintySpec.unknown("UN Comtrade publishes no uncertainty for this value"),
            source_flags=flags,
            raw_selector={
                "match": {"period": str(year), "reporterCode": code, "partnerCode": int(partner), "flowCode": flow},
                "field": "primaryValue",
            },
            relation=RelationSpec(
                predicate="predicate.trade_flow",
                layer="trade.crude_petroleum",
                participants=(
                    {
                        "role": "role.reporter",
                        "provider": "comtrade_reporter",
                        "key": str(code),
                        "direction": reporter_dir,
                    },
                    {"role": "role.partner", "provider": "comtrade_partner", "key": partner, "direction": partner_dir},
                    {"role": "role.product", "provider": "hs_heading", "key": "2709", "direction": "undirected"},
                ),
                component={
                    "flow": flow,
                    "valuation": value.get("basis", {}).get("valuation"),
                    "classification": r.get("classificationCode"),
                },
            ),
        )

    # ------------------------------------------------------------------ validation
    def validate(self, batch: ObservationBatch) -> QualityReport:
        findings: list[Finding] = []
        meta = batch.metadata
        req = meta["request"]
        count, n = meta.get("count"), meta["returned_rows"]
        if count is None or int(count) != n:
            findings.append(
                Finding(code="count_mismatch", severity="blocker", message=f"count={count} but {n} rows returned")
            )
        if n >= MAX_RECORDS:
            findings.append(
                Finding(
                    code="possible_truncation",
                    severity="blocker",
                    message="row count reached maxRecords; preview truncation cannot be excluded"
                    + (" (re-requested in halves)" if meta.get("split_into") else ""),
                )
            )
        wanted_codes = {str(c) for c in req["reporters"]}
        seen: set[tuple[Any, ...]] = set()
        for d in meta["rows"]:
            for k, v in FIXED_DIMS.items():
                if str(d.get(k)) != str(v):
                    findings.append(
                        Finding(
                            code="dimension_mismatch",
                            severity="blocker",
                            message=f"{k}={d.get(k)} differs from requested {v}",
                        )
                    )
            if str(d.get("reporterCode")) not in wanted_codes:
                findings.append(
                    Finding(
                        code="dimension_mismatch",
                        severity="blocker",
                        message=f"unrequested reporterCode {d.get('reporterCode')}",
                    )
                )
            if str(d.get("flowCode")) not in FLOWS:
                findings.append(
                    Finding(
                        code="dimension_mismatch",
                        severity="blocker",
                        message=f"unrequested flowCode {d.get('flowCode')}",
                    )
                )
            if req["scope"] == "total" and str(d.get("partnerCode")) != "0":
                findings.append(
                    Finding(
                        code="dimension_mismatch",
                        severity="blocker",
                        message=f"partnerCode={d.get('partnerCode')} in a world-total request",
                    )
                )
            if str(d.get("period")) != str(req["year"]):
                findings.append(
                    Finding(
                        code="dimension_mismatch", severity="blocker", message=f"unexpected period {d.get('period')}"
                    )
                )
            key = (d.get("reporterCode"), d.get("flowCode"), d.get("period"), d.get("partnerCode"))
            if key in seen:
                findings.append(
                    Finding(
                        code="ambiguous_duplicate",
                        severity="blocker",
                        message=f"duplicate rows for reporter/flow/period/partner {key}",
                    )
                )
            seen.add(key)
        # Classification: a known but unreviewed HS edition is per row (stored, ineligible by the
        # mapping scope); anything else outside the reviewed editions blocks as in connector 1.2.0.
        known_unreviewed = [d for d in meta["rows"] if d.get("classificationCode") in KNOWN_UNREVIEWED_EDITIONS]
        structural = {
            **meta,
            "rows": [d for d in meta["rows"] if d.get("classificationCode") not in KNOWN_UNREVIEWED_EDITIONS],
        }
        findings.extend(_structure_findings(structural))
        if known_unreviewed:
            reps = sorted(
                {f"{d.get('reporterCode')}:{d.get('flowCode')}:{d.get('classificationCode')}" for d in known_unreviewed}
            )
            findings.append(
                Finding(
                    code="classification_unreviewed_ineligible",
                    severity="warning",
                    message=f"{len(known_unreviewed)} rows are reported under an HS edition outside the reviewed "
                    f"mapping ({', '.join(REVIEWED_CLASSIFICATIONS)}): stored, ineligible for releases "
                    f"(e.g. {', '.join(reps[:8])})",
                )
            )
        missing = [
            o.logical_key
            for o in batch.observations
            if o.value.get("kind") == "missing" and o.logical_key.endswith("primaryValue")
        ]
        if missing:
            findings.append(
                Finding(
                    code="missing_flow",
                    severity="warning",
                    message=f"{len(missing)} requested reporter flows returned no value (kept Missing)",
                )
            )
        undetermined = [o for o in batch.observations if o.source_flags.get("valuation_warning")]
        if undetermined:
            codes = sorted({str(o.source_flags["valuation_warning"]) for o in undetermined})
            findings.append(
                Finding(
                    code="valuation_basis_undetermined",
                    severity="warning",
                    message=f"{len(undetermined)} values carry an undeterminable valuation basis "
                    f"({', '.join(codes)}); labelled '{VALUATION_UNDETERMINED}'",
                )
            )
        estimated = [o for o in batch.observations if o.source_flags.get("provider_quantity_estimated")]
        if estimated:
            findings.append(
                Finding(
                    code="provider_quantity_estimated",
                    severity="info",
                    message=f"{len(estimated)} net weights are estimated by the provider; trade values are as reported",
                )
            )
        unknown = sorted(
            {
                str(o.source_flags.get("legacyEstimationFlag"))
                for o in batch.observations
                if o.source_flags.get("legacy_estimation_code_unrecognised")
            }
        )
        if unknown:
            findings.append(
                Finding(
                    code="estimation_code_unrecognised",
                    severity="warning",
                    message=f"legacyEstimationFlag {', '.join(unknown)} is outside the UNSD codes "
                    "0/2/4/6; estimation status of these rows needs review",
                )
            )
        findings.extend(self._jump_signals(batch))
        return _report(findings, len(batch.expected_keys), len(batch.observations), ())


__all__ = ["CONNECTOR_NAME", "CONNECTOR_VERSION", "ComtradeBatchConnector"]
