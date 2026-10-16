"""V3 SIMFIX: connector ``ilostat.social_protection`` (ILOSTAT SDG 1.3.1 old-age pension coverage) and
``social.retirement_age@1.2.0`` (the legal-age change applied to the pension-covered share only).

Offline: excerpts of the real ILOSTAT bytes (SDG 1.3.1 retrieved 2026-10-16; rates and WPP 2024 of
SEN, JPN, BRA from the SIMWORLD fixtures).
"""

from __future__ import annotations

import asyncio
import gzip
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures" / "simworld"))

import simfix_fixtures as FX
import simworld_fixtures as SF

from reva.adapters.connectors import ilostat_social_protection as SP
from reva.adapters.connectors.registry import builtin_connectors
from reva.evidence.runner import FixtureHttp
from reva.kernel.errors import SourceResponseError
from reva.models.social import retirement_coverage as rc
from reva.models.social import retirement_world as rw
from reva.simulation import plugins
from reva.simulation.plugins import ResolvedInputs
from reva.simulation.scenario_schema import build_schema
from reva.simulation.spec import validate_scenario_spec

RELEASE = "3f1c2b4a-5d6e-4f70-8a91-b2c3d4e5f607"


# --------------------------------------------------------------------------- connector
def test_connector_is_builtin_on_the_ilostat_source_and_labour_connector_is_unchanged() -> None:
    conns = builtin_connectors()
    spec = conns[SP.NAME].describe()
    assert spec.version == "1.0.0" and spec.source_key == "ilostat.rplumber"
    assert spec.allowed_hosts == ("rplumber.ilo.org",) and spec.dataset_ids == ("SDG_0131_SEX_SOC_RT_A",)
    assert spec.field_mappings == {"SDG_0131_SEX_SOC_RT_A": f"{SP.CONCEPT_ID}@1.0.0"}
    assert conns["ilostat.labour_age5"].describe().version == "1.0.1"  # the registered version is untouched
    items = SP.IlostatSocialProtectionConnector().all_items()
    assert [i.url for i in items] == [
        "https://rplumber.ilo.org/data/indicator/?id=SDG_0131_SEX_SOC_RT_A&format=.csv.gz"
    ]


def test_parse_keeps_the_pension_function_by_sex_verbatim() -> None:
    tables, rep = SP.build_tables(FX.sp_body())
    assert {"SEN", "JPN", "BRA", "IND", "MEX", "IDN", "XKX"} <= set(tables)
    parse = rep["parse"]
    assert parse["other_function_rows"] > 0 and parse["unmapped_areas"] == {"X01": 45}  # aggregate: never mapped
    rows = tables["BRA"].to_pylist()
    assert {r["function"] for r in rows} == {"SOC_CONTIG_PENSION"} and {r["sex"] for r in rows} == {"F", "M", "T"}
    cell = {(r["year"], r["sex"]): r["value"] for r in rows}
    assert cell[(2022, "F")] == "67.4" and cell[(2022, "M")] == "100" and cell[(2022, "T")] == "88.6"
    ind = [r for r in tables["IND"].to_pylist() if r["year"] == 2022]
    assert {r["note_classif"] for r in ind} == {"C15:6330"}
    assert ind[0]["note_classif_label"] == "Social security coverage: Data not available for BOCW Fund"
    jor = [r for r in tables["JOR"].to_pylist() if r["year"] == 2022 and r["sex"] in ("F", "M")]
    assert jor and all(r["value"] is None and r["status"] == "U" for r in jor)  # kept as missing, never zero
    assert {r["ref_area"] for r in tables["XKX"].to_pylist()} == {"KOS"}


def test_drift_and_bad_bodies_are_refused() -> None:
    text = gzip.decompress(FX.sp_body()).decode("utf-8-sig")
    with pytest.raises(SourceResponseError, match="header drift"):
        SP.build_tables(gzip.compress(text.replace("note_classif", "note").encode()))
    with pytest.raises(SourceResponseError, match="not gzip"):
        SP.build_tables(b"")
    with pytest.raises(SourceResponseError, match="unreviewed ILOSTAT social protection function"):
        SP.build_tables(gzip.compress(text.replace("SOC_CONTIG_UNE", "SOC_NEW_UNE").encode()))
    head, *lines = text.splitlines(keepends=True)
    over = [ln.replace('"2022",88.6', '"2022",188.6') for ln in lines]
    tables, rep = SP.build_tables(gzip.compress("".join([head, *over]).encode()))
    assert "BRA" not in tables and "BRA" in rep["values_above_100_refused"]


def test_fetch_parse_validate_through_the_fixture_bank() -> None:
    c = SP.IlostatSocialProtectionConnector()
    raw = asyncio.run(c.fetch(c.all_items()[0], FixtureHttp(SP.NAME)))
    batch = next(iter(c.parse(raw)))
    assert batch.observations == () and batch.metadata["countries"] >= 7
    assert batch.metadata["dataset_input"] == {"schema": SP.DATASET_SCHEMA, "concept": SP.CONCEPT_ID}
    assert c.validate(batch).outcome == "pass"
    empty = batch.model_copy(update={"metadata": {**batch.metadata, "countries": 0}})
    assert c.validate(empty).outcome == "blocked"


# --------------------------------------------------------------------------- coverage selection
def test_coverage_selection_by_sex_total_and_year() -> None:
    bra = rc.select_coverage("BRA", FX.sp_table("BRA").to_pylist(), 2022)
    assert isinstance(bra, rc.Coverage) and bra.year == 2022 and bra.breakdown == "by_sex"
    assert bra.share == {"F": Decimal("0.674"), "M": Decimal("1")} and bra.total_percent == "88.6"
    sen = rc.select_coverage("SEN", FX.sp_table("SEN").to_pylist(), 2022)
    assert isinstance(sen, rc.Coverage) and sen.breakdown == "total_applied_to_both_sexes"
    assert sen.share == {"F": Decimal("0.253"), "M": Decimal("0.253")}
    assert any("applied to both sexes" in c for c in sen.caveats())
    jpn = rc.select_coverage("JPN", FX.sp_table("JPN").to_pylist(), 2022)
    assert isinstance(jpn, rc.Coverage) and jpn.year == 2020 and jpn.years_before_reference_year == 2
    assert any("held constant" in c for c in jpn.caveats())
    # reference year 2016: SEN's latest value <= 2016 is 2010 (6 years older: stated as old)
    old = rc.select_coverage("SEN", FX.sp_table("SEN").to_pylist(), 2016)
    assert isinstance(old, rc.Coverage) and old.year == 2010 and old.newer_years_not_used == (2019, 2022)
    assert any("(old)" in c for c in old.caveats())
    ind = rc.select_coverage("IND", FX.sp_table("IND").to_pylist(), 2022)
    assert isinstance(ind, rc.Coverage) and ind.notes == ({"code": "C15:6330", "label": SP.NOTE_CLASSIF["C15:6330"]},)
    # nothing within 10 years before the reference year: missing_input, never a default
    stale = rc.select_coverage("SEN", FX.sp_table("SEN").to_pylist(), 2003)
    assert isinstance(stale, dict) and stale["code"] == "missing_input" and "never assumed" in stale["reason"]
    # JOR 2022: F/M without a value (status U), T present -> the total is used
    jor = rc.select_coverage("JOR", FX.sp_table("JOR").to_pylist(), 2022)
    assert isinstance(jor, rc.Coverage) and jor.breakdown == "total_applied_to_both_sexes"


# --------------------------------------------------------------------------- model 1.2.0
def ret_spec(version: str = "1.2.0", **over: Any) -> dict[str, Any]:
    doc = {
        "schema_version": "2.0.0",
        "title": "SIMFIX retirement",
        "baseline_release_id": RELEASE,
        "knowledge_cutoff": "2026-10-16T00:00:00+00:00",
        "model_key": "social.retirement_age",
        "model_version": version,
        "countries": ["SEN", "JPN", "BRA"],
        "reference_year": 2022,
        "horizon": {"periods": 8, "unit": "calendar_year"},
        "intervention": {
            "kind": "retirement_age_target",
            "reference_path": "wbl2026_held_constant",
            "target_age_months": 744,
            "first_birth_year": 1965,
            "phase_in_months_per_cohort": 0,
        },
        "assumptions": {"employment_response": {"central": "rabate_rochut_2020_fra"}},
        "fidelity": "L1",
        "missing_input_policy": "fail",
        "outputs": sorted(rc.OUTPUTS),
        "seed": 0,
    }
    doc.update(over)
    return doc


def run(spec: dict[str, Any], fidelity: str = "L1", drop: tuple[tuple[str, str], ...] = ()) -> tuple[Any, list[Any]]:
    m = plugins.get("social.retirement_age", spec["model_version"])
    m.validate(spec)
    reqs = m.requirements(spec)
    ds = {}
    for r in reqs:
        if (r.name, r.subject) in drop:
            continue
        if r.name == "pension_coverage":
            t = FX.sp_table(r.subject)
        elif r.name in ("employment_rate", "activity_rate"):
            t = SF.ilo_table(r.name, r.subject)
        else:
            t = SF.wpp_table(SF.WPP_INPUTS[r.name], r.subject)
        if t is not None:
            ds[(r.name, r.subject)] = (t, {})
    compiled = m.compile(ResolvedInputs.from_values(reqs, datasets=ds), spec)
    return compiled, m.compute(compiled, fidelity)


def values(rows: list[Any]) -> dict[tuple[str, str, int], Decimal]:
    return {(r.iso3, r.output_key, r.period[0].year): Decimal(r.value["value"]) for r in rows if r.variant == "L1"}


def test_registered_as_1_2_0_with_its_own_schema_branch_and_coverage_input() -> None:
    plugins.load_all()
    assert set(plugins.versions_of("social.retirement_age")) == {"1.0.0", "1.1.0", "1.2.0"}
    assert plugins.get("social.retirement_age").model_version == "1.0.0"  # default unchanged
    m11, m12 = plugins.get("social.retirement_age", "1.1.0"), plugins.get("social.retirement_age", "1.2.0")
    assert "pension_coverage" not in {r.name for r in m11.requirements(ret_spec("1.1.0"))}
    cov = [r for r in m12.requirements(ret_spec()) if r.name == "pension_coverage"]
    assert {r.concept for r in cov} == {SP.CONCEPT_ID} and {r.kind for r in cov} == {"dataset"}
    schema = build_schema()
    assert schema["$defs"]["social.retirement_age@1.2.0"]["properties"]["model_version"] == {"const": "1.2.0"}
    assert validate_scenario_spec(ret_spec()).as_json()["model_version"] == "1.2.0"
    man = m12.manifest()
    assert man["model_version"] == "1.2.0" and any("never assumed covered" in a for a in man["assumptions"])
    assert "pension_coverage" not in m11.manifest()["data_sources"]


def test_effects_scale_with_coverage_and_full_coverage_equals_1_1_0() -> None:
    _c11, r11 = run(ret_spec("1.1.0"))
    c12, r12 = run(ret_spec())
    assert {p.status for p in c12.countries} == {"ready"}
    v11, v12 = values(r11), values(r12)
    tol = Decimal("0.002")
    for t in range(2023, 2031):
        # JPN: coverage 100 % (2020, both sexes) -> exactly 1.1.0
        for out in rc.OUTPUTS:
            assert v12[("JPN", out, t)] == v11[("JPN", out, t)]
        # SEN: total 25.3 % applied to both sexes -> every change scales by 0.253
        for out in ("employment_change_55_64", "pension_recipients_change_55_64"):
            assert abs(v12[("SEN", out, t)] - Decimal("0.253") * v11[("SEN", out, t)]) <= tol
        assert v12[("SEN", "old_age_dependency_ratio", t)] == v11[("SEN", "old_age_dependency_ratio", t)]
    # SEN 60 -> 62 from the 1965 cohort: same sign, smaller size
    assert 0 < v12[("SEN", "employment_change_55_64", 2029)] < v11[("SEN", "employment_change_55_64", 2029)]
    # BRA: men 100 %, women 67.4 % (2022). Target 60 moves both sexes (WBL: women 62, men 65): the women's rate
    # moves by 0.674 of the full-coverage (1.1.0) move, the men's by all of it
    target60 = {**ret_spec()["intervention"], "target_age_months": 720}
    cb, _rb = run(ret_spec(countries=["BRA"], intervention=target60))
    plan = cb.countries[0]
    one = {"F": Decimal(1), "M": Decimal(1)}
    full = rc.CountryPlan(**{**plan.__dict__, "coverage": rc.Coverage(**{**plan.coverage.__dict__, "share": one})})
    v_cov, _ = rc.compute_point(plan, plan.central, cb.years)
    v_full, _ = rc.compute_point(full, plan.central, cb.years)
    v_old, _ = rw.compute_point(plan, plan.central, cb.years)
    assert v_full["employment_rate.F.Y60-64"][2030] != plan.data.employment_rate[("F", "Y60-64")]  # women do move
    assert abs(v_cov["employment_change_55_64"][2030]) < abs(v_full["employment_change_55_64"][2030])
    for t in cb.years:
        for out in ("employment_change_55_64", "pension_recipients_change_55_64", "employed_persons_55_64"):
            assert v_full[out][t] == v_old[out][t]  # c = 1 is the 1.1.0 arithmetic, exactly
        for g in ("Y55-59", "Y60-64"):
            b_f = plan.data.employment_rate[("F", g)]
            moved_full = v_full[f"employment_rate.F.{g}"][t] - b_f
            moved = v_cov[f"employment_rate.F.{g}"][t] - b_f
            assert abs(moved - Decimal("0.674") * moved_full) < Decimal("1e-20")
            assert v_cov[f"employment_rate.M.{g}"][t] == v_full[f"employment_rate.M.{g}"][t]


def test_rows_state_coverage_and_l2_never_moves_it() -> None:
    _, rows = run(ret_spec(countries=["SEN"], fidelity="L2"), fidelity="L2")
    r = next(x for x in rows if x.variant == "L2:sensitivity_range")
    app = r.applicability
    assert app["model"] == "social.retirement_age@1.2.0" and app["coverage_year"] == 2022
    assert app["pension_coverage"]["breakdown"] == "total_applied_to_both_sexes"
    assert app["pension_coverage"]["percent_by_sex_used"] == {"F": "25.3", "M": "25.3"}
    assert "no coverage variant" in r.uncertainty["description"]
    assert any("non-contributory" in c for c in app["caveats"])
    assert "pension-covered share only" in r.value["basis"]["notes"]
    assert {x.applicability["pension_coverage"]["year"] for x in rows} == {2022}  # one coverage for every point


def test_missing_coverage_is_refused_never_assumed() -> None:
    compiled, _rows = run(ret_spec(countries=["SEN"]), drop=(("pension_coverage", "SEN"),))
    [p] = compiled.missing
    assert p["input"] == "pension_coverage" and p["concept"] == SP.CONCEPT_ID and "not 100 %" in p["reason"]
    compiled, rows = run(ret_spec(missing_input_policy="skip_missing_countries"), drop=(("pension_coverage", "BRA"),))
    st = {p.iso3: p.status for p in compiled.countries}
    assert st == {"SEN": "ready", "JPN": "ready", "BRA": "missing_input"} and "BRA" not in {r.iso3 for r in rows}
    assert compiled.skipped == ["BRA"]
    tables = {
        n: SF.ilo_table(n, "SEN")
        if n in ("employment_rate", "activity_rate")
        else SF.wpp_table(SF.WPP_INPUTS[n], "SEN")
        for n in rw.CONCEPTS
    }
    sen = rc.build_country_plan(
        "SEN",
        None,
        ret_spec(countries=["SEN"], reference_year=2012),
        {**tables, "pension_coverage": FX.sp_table("SEN")},
    )
    assert sen.coverage is not None and sen.coverage.year == 2010  # within the window, stated in notes
    assert any(n["code"] == "coverage_older_than_reference_year" for n in sen.notes)
