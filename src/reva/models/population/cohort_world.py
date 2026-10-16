"""Plugin ``demography.cohort_component@1.1.0``: the cohort-component projection for every country.

Version 1.1.0 of the same model (V3 agent SIMWORLD). It adds one explicit choice to the
1.0.0 spec, ``assumptions.data_source``:

* ``"eurostat_demography"`` - the 1.0.0 path, unchanged: the same 31 EU/EFTA countries,
  the same Eurostat dataset inputs, the same numerics (the 1.0.0 plugin's code is called);
  values are identical to a 1.0.0 run of the same spec (tested);
* ``"un_wpp2024"`` - UN World Population Prospects 2024 estimates (connector ``un.wpp``) for
  any of the 237 WPP countries/areas (``countries: ["ALL"]`` selects every country the
  pinned release holds WPP datasets for). Migration ``zero`` or ``average_residual`` (the
  residual then is the migration implied by WPP's own reconstruction); EUROPOP2023 options
  are refused (``capability_gap``). Jump-off at most 1 January 2023 (last WPP estimate kept);
  base period from 1990.

The 1.0.0 plugin stays registered as the default version: a spec naming
``model_version: "1.0.0"`` runs exactly the 1.0.0 code. Every 1.1.0 result row says which
data source it rests on. Numerics: :mod:`reva.models.population.cohort` (unchanged).
A *conditional projection under stated assumptions, not a forecast*.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
from typing import Any

from reva.kernel.errors import CapabilityGap, MissingInput, RevaError, ValidationProblem
from reva.models.population import cohort as C
from reva.models.population import cohort_component as CC
from reva.models.population import wpp
from reva.simulation import plugins
from reva.simulation.plugins import InputRequirement, ResolvedInputs, ResultRow

MODEL_VERSION = "1.1.0"
BASE_VERSION = CC.C.MODEL_VERSION  # "1.0.0": the Eurostat path is delegated to it unchanged
DATA_SOURCES = ("eurostat_demography", "un_wpp2024")
ALL = "ALL"
COUNTRIES = {"eurostat_demography": CC.COUNTRIES, "un_wpp2024": wpp.COUNTRIES}
ALL_KNOWN = tuple(sorted(set(CC.COUNTRIES) | set(wpp.COUNTRIES)))
DATA_LABELS = {
    "eurostat_demography": "Eurostat demography (demo_pjan, demo_magec, demo_fasec): registered events",
    "un_wpp2024": wpp.DATA_LABEL,
}
BACKTEST_DOCS = {"eurostat_demography": "docs/dev/v2_pop_backtest.md", "un_wpp2024": "docs/dev/v3_simworld.md"}
WPP_MIGRATIONS = ("zero", "average_residual")


def _source(spec: dict[str, Any]) -> str:
    src = (spec.get("assumptions") or {}).get("data_source")
    if src not in DATA_SOURCES:
        raise ValidationProblem(f"assumptions.data_source must be one of {list(DATA_SOURCES)}", got=src)
    return str(src)


def base_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """The 1.0.0-shaped spec (no data_source, model_version 1.0.0) the shared parser reads."""
    s = copy.deepcopy(dict(spec))
    s["model_version"] = BASE_VERSION
    a = dict(s.get("assumptions") or {})
    a.pop("data_source", None)
    s["assumptions"] = a
    return s


def parse_spec(spec: Any) -> tuple[CC.ModelSpec, str]:
    s = spec.as_json() if hasattr(spec, "as_json") else dict(spec)
    if s.get("model_key") != C.MODEL_KEY or s.get("model_version") != MODEL_VERSION:
        raise ValidationProblem(f"spec is not for {C.MODEL_KEY}@{MODEL_VERSION}")
    source = _source(s)
    countries = list(s.get("countries") or [])
    if countries != [ALL]:
        bad = sorted(set(countries) - set(COUNTRIES[source]))
        if bad:
            raise CapabilityGap(
                f"countries {bad} are not covered by data source {source}",
                capability="demography.country_coverage",
                data_source=source,
                supported=list(COUNTRIES[source]),
            )
    if source == "un_wpp2024":
        a = s.get("assumptions") or {}
        sens = a.get("sensitivity") or {}
        used = {str(a.get("migration")), *[str(m) for m in sens.get("migration") or []]}
        used_f = {str(a.get("fertility")), *[str(f) for f in sens.get("fertility") or []]}
        if used - set(WPP_MIGRATIONS) or used_f - {"constant_base_period_rates"}:
            raise CapabilityGap(
                "EUROPOP2023 assumptions exist for EU/EFTA countries only (Eurostat); with data_source "
                "un_wpp2024 migration must be zero or average_residual and fertility constant_base_period_rates",
                capability="demography.assumption.europop2023",
                data_source=source,
            )
        if int(s.get("reference_year", 0)) > wpp.LAST_POPULATION_YEAR:
            raise CapabilityGap(
                f"WPP 2024 estimates end with the population of 1 January {wpp.LAST_POPULATION_YEAR}; a later "
                "jump-off would need WPP projection values as observed input (refused)",
                capability="demography.jump_off",
                data_source=source,
                latest_jump_off=wpp.LAST_POPULATION_YEAR,
            )
        bp = a.get("base_period") or {}
        if isinstance(bp.get("first_year"), int) and bp["first_year"] < wpp.FIRST_YEAR:
            raise CapabilityGap(
                f"WPP dataset inputs start in {wpp.FIRST_YEAR}",
                capability="demography.base_period",
                data_source=source,
            )
    b = base_spec(s)
    b["countries"] = [COUNTRIES["eurostat_demography"][0]]  # countries are checked above per source
    ms = CC.parse_spec(b)
    return replace(ms, countries=tuple(countries)), source


def _needed(ms: CC.ModelSpec, source: str) -> list[str]:
    return CC._needed(ms) if source == "eurostat_demography" else ["population", "deaths", "births", "wpp_indicators"]


class CohortComponentWorldModel(CC.CohortComponentModel):
    """``demography.cohort_component@1.1.0`` (experimental, review pending)."""

    model_version = MODEL_VERSION
    supports_all_countries = True

    def validate(self, spec: dict[str, Any]) -> None:
        parse_spec(spec)

    def requirements(self, spec: dict[str, Any]) -> list[InputRequirement]:
        ms, source = parse_spec(spec)
        countries = list(ms.countries)
        if countries == [ALL]:  # probe of compile.resolve_all_countries: concepts and years only
            countries = [ALL]
            if source == "eurostat_demography":
                reqs = super().requirements({**base_spec(spec), "countries": [CC.COUNTRIES[0]]})
                return [r._replace(subject=ALL) for r in reqs]
        if source == "eurostat_demography":
            return super().requirements({**base_spec(spec), "countries": countries})
        years = CC._input_years(ms)
        return [
            InputRequirement(name, wpp.CONCEPTS[name], iso3, years, "dataset")
            for iso3 in countries
            for name in _needed(ms, source)
        ]

    def compile(self, inputs: ResolvedInputs, spec: dict[str, Any]) -> CC.CompiledProjection:
        ms, source = parse_spec(spec)
        if source == "eurostat_demography":
            compiled = super().compile(inputs, {**base_spec(spec), "countries": list(ms.countries)})
        else:
            compiled = CC.CompiledProjection(
                ms, dict(inputs.release), [self._wpp_plan(inputs, ms, iso3) for iso3 in ms.countries]
            )
        compiled.data_source = source  # type: ignore[attr-defined]
        return compiled

    @staticmethod
    def _wpp_plan(inputs: ResolvedInputs, ms: CC.ModelSpec, iso3: str) -> CC.CountryPlan:
        plan = CC.CountryPlan(iso3, inputs.subject(iso3).entity_id)
        tables: dict[str, Any] = {}
        for name in ("population", "deaths", "births", "wpp_indicators"):
            d = inputs.dataset(name, iso3)
            plan.provenance[name] = d.provenance()
            if d.is_missing:
                if name == "wpp_indicators":
                    continue  # diagnostics only
                plan.status = "missing_input"
                plan.problems.append(
                    {
                        "code": "missing_input",
                        "country": iso3,
                        "input": name,
                        "concept": wpp.CONCEPTS[name],
                        "kind": "dataset",
                        "reason": d.missing_reason,
                    }
                )
            else:
                tables[name] = d.table
        if plan.status != "ready":
            return plan
        try:
            plan.data = wpp.country_demography(
                iso3,
                population=tables["population"],
                deaths=tables["deaths"],
                births=tables["births"],
                provenance=plan.provenance,
            )
            a = ms.central
            window = a.migration_window or a.base_period
            plan.rates = C.base_rates(plan.data, a.base_period[0], a.base_period[1], migration_window=window)
            C._vec(plan.data, "population", ms.jump_off, "F", plan.rates.omega)
            C._vec(plan.data, "population", ms.jump_off, "M", plan.rates.omega)
            plan.warnings = C.base_period_warnings(plan.rates)
            if any(v.migration == "average_residual" for v in (ms.central, *ms.grid)):
                plan.migration_window = C.resolve_migration_window(
                    plan.rates, window, breaks=a.migration_breaks, allow_single_year=a.allow_single_year_migration
                )
                plan.warnings += list(plan.migration_window.warnings)
            if "wpp_indicators" in tables:
                check = wpp.migration_check(plan.rates, range(window[0], window[1] + 1), tables["wpp_indicators"])
                if check:
                    plan.provenance["wpp_net_migration_check"] = check
        except MissingInput as e:
            plan.status = "missing_input"
            plan.problems = [{"code": "missing_input", "country": iso3, **m} for m in e.details.get("missing", [])] or [
                {**e.problem(), "country": iso3}
            ]
        except RevaError as e:
            plan.status = "unsupported"
            plan.problems = [{**e.problem(), "country": iso3}]
        return plan

    def compute(self, compiled: CC.CompiledProjection, fidelity: str) -> list[ResultRow]:
        source = getattr(compiled, "data_source", "eurostat_demography")
        rows = super().compute(compiled, fidelity)
        unc = _uncertainty(source)
        for r in rows:
            r.applicability = {
                **r.applicability,
                "model": f"{C.MODEL_KEY}@{MODEL_VERSION}",
                "data_source": source,
                "data_label": DATA_LABELS[source],
            }
            if r.variant != CC.RANGE_VARIANT:
                r.uncertainty = unc
        return rows

    def manifest(self) -> dict[str, Any]:
        return {
            **super().manifest(),
            "implementation_digest": plugins.source_digest(*self.implementation_files()),
            "model_version": MODEL_VERSION,
            "data_sources": DATA_LABELS,
            "backtest_doc": BACKTEST_DOCS,
            "base_version_delegated": f"{C.MODEL_KEY}@{BASE_VERSION} (data_source eurostat_demography)",
        }

    def spec_schema(self) -> dict[str, Any]:
        sch = copy.deepcopy(super().spec_schema())
        sch["countries"] = {
            "type": "array",
            "items": {"enum": [*ALL_KNOWN, ALL]},
            "minItems": 1,
            "maxItems": len(ALL_KNOWN),
            "uniqueItems": True,
        }
        a = sch["assumptions"]
        a["properties"]["data_source"] = {"enum": list(DATA_SOURCES)}
        a["required"] = [*a["required"], "data_source"]
        a["properties"]["base_period"]["properties"]["first_year"]["minimum"] = wpp.FIRST_YEAR
        return sch

    def explanation(self, fidelity: str) -> dict[str, Any]:
        out = super().explanation(fidelity)
        out["model"] = f"{C.MODEL_KEY}@{MODEL_VERSION}"
        out["data_sources"] = DATA_LABELS
        return out

    def run_applicability(self, compiled: CC.CompiledProjection, fidelity: str) -> dict[str, Any]:
        out = super().run_applicability(compiled, fidelity)
        source = getattr(compiled, "data_source", "eurostat_demography")
        out["data_source"] = source
        out["data_label"] = DATA_LABELS[source]
        out["backtest_doc"] = BACKTEST_DOCS[source]
        for c in compiled.countries:
            chk = c.provenance.get("wpp_net_migration_check")
            if chk and c.iso3 in out["countries"]:
                out["countries"][c.iso3]["wpp_net_migration_check"] = chk
        return out

    def seed_subjects(self, spec: dict[str, Any]) -> list[str]:
        return sorted(spec.get("countries") or [])

    def implementation_files(self) -> tuple[Path, ...]:
        return (Path(C.__file__), Path(CC.__file__), Path(wpp.__file__), Path(__file__))


def _uncertainty(source: str) -> dict[str, Any]:
    from reva.kernel.uncertainty import UncertaintySpec

    extra = (
        " WPP 2024 inputs are themselves model-based estimates; their back-test (docs/dev/v3_simworld.md) checks "
        "consistency with WPP, not truth."
        if source == "un_wpp2024"
        else ""
    )
    return UncertaintySpec.unknown(
        "Unquantified: a conditional projection under the stated assumptions, not a forecast; no probability is "
        "attached. Inputs carry no published uncertainty. The out-of-sample back-test error "
        f"({BACKTEST_DOCS[source]}) describes past performance of the method only.{extra}"
    ).model_dump(mode="json")


MODEL = plugins.register(CohortComponentWorldModel())
