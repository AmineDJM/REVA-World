"""Offline tests of the reviewed connector ``eurostat.demography`` (recorded Eurostat responses).

Fixtures: ``tests/fixtures/source/eurostat.demography`` — LI and LU since 2008 plus the three
EUROPOP2023 tables for both (Liechtenstein is not projected by Eurostat), captured live on
2026-10-15 with ``RecordingHttp``.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

import pytest

from reva.adapters.connectors import eurostat_demography as ed
from reva.adapters.connectors.registry import builtin_connectors
from reva.evidence.runner import FixtureHttp
from reva.ports.connectors import Cursor, FetchBudget, FetchOutcome, RawAcquisition

FIXTURE_COUNTRIES = ("LI", "LU")


def connector() -> ed.EurostatDemographyConnector:
    return ed.EurostatDemographyConnector(countries=FIXTURE_COUNTRIES, since_year=2008, window_years=ed.WINDOW_YEARS)


def raws() -> dict[str, RawAcquisition]:
    c = connector()
    http = FixtureHttp(ed.NAME)

    async def go() -> dict[str, RawAcquisition]:
        return {i.key: await c.fetch(i, http) for i in c.all_items()}

    return asyncio.run(go())


@pytest.fixture(scope="module")
def fixture_raws() -> dict[str, RawAcquisition]:
    return raws()


def test_name_does_not_collide_with_autonomy_templates_and_is_builtin() -> None:
    spec = connector().describe()
    assert spec.name == "eurostat.demography"
    assert len(spec.name.split(".")) == 2  # template instances are eurostat.<dataset>.<geo>
    assert spec.source_key == "eurostat.demography" and spec.lineage_family == "eurostat"
    assert set(spec.allowed_hosts) == {"ec.europa.eu"}
    assert "eurostat.demography" in builtin_connectors()


def test_plan_covers_every_country_and_dataset_and_resumes_from_cursor() -> None:
    c = builtin_connectors()["eurostat.demography"]  # the windowed built-in instance
    items = c.all_items()
    assert len(items) == 31 * 3 * 3 + 3  # 3 time windows per observed table
    assert all(i.url.startswith(ed.DATA_BASE) for i in items)
    assert "geo=EL" in next(i.url for i in items if i.key == "demo_pjan:EL:2000")  # Eurostat code for Greece
    plan = c.plan(None, FetchBudget(max_requests=40))
    assert len(plan.items) == 40 and plan.budget_exhausted
    done = [FetchOutcome(item_key=i.key, status="published") for i in plan.items]
    cur = c.next_cursor(plan, done)
    # 1.1.0: always a savable checkpoint (the runner persists only ``complete: true``)
    assert cur.state["complete"] and not cur.state["cycle_finished"] and len(cur.state["cycle_done"]) == 40
    plan2 = c.plan(cur, FetchBudget(max_requests=1000))
    assert {i.key for i in plan2.items} == {i.key for i in items} - set(cur.state["cycle_done"])
    cur2 = c.next_cursor(plan2, [FetchOutcome(item_key=i.key, status="published") for i in plan2.items])
    assert cur2.state["complete"] and cur2.state["cycle_finished"] and cur2.state["cycle_done"] == []
    assert len(c.plan(Cursor(state=cur2.state), FetchBudget(max_requests=1000)).items) == len(items)  # new cycle


def test_cursor_is_always_a_savable_checkpoint_and_resumes_across_budgeted_runs() -> None:
    """Regression (V6 FIXPOP): with the live budget of 100 requests per run, the 282-item cycle
    needs 3 runs; each run's cursor must be persistable (``complete: true``) and the next run
    must continue where the previous one stopped (1.0.0 restarted at AT every run, FR never came)."""
    c = builtin_connectors()["eurostat.demography"]
    all_keys = [i.key for i in c.all_items()]
    cursor: Cursor | None = None
    fetched: list[str] = []
    for _ in range(3):
        plan = c.plan(cursor, FetchBudget(max_requests=100))
        fetched += [i.key for i in plan.items]
        cursor = c.next_cursor(plan, [FetchOutcome(item_key=i.key, status="published") for i in plan.items])
        assert cursor.state["complete"] is True  # the runner saves it
    assert fetched == all_keys  # each item exactly once, in plan order, across the three runs
    assert cursor is not None and cursor.state["cycle_finished"] and cursor.state["cycle_progress"] == [282, 282]
    assert any(k.startswith("demo_pjan:FR:") for k in fetched[100:200])  # FR comes in the 2nd run


def test_failed_items_are_retried_and_quarantined_items_do_not_stall_the_cycle() -> None:
    c = builtin_connectors()["eurostat.demography"]
    plan = c.plan(None, FetchBudget(max_requests=3))
    a, b, q = (i.key for i in plan.items)
    cur = c.next_cursor(
        plan,
        [
            FetchOutcome(item_key=a, status="published"),
            FetchOutcome(item_key=b, status="failed", detail="budget"),
            FetchOutcome(item_key=q, status="quarantined"),
        ],
    )
    assert cur.state["cycle_done"] == sorted([a, q]) and cur.state["quarantined_this_run"] == [q]
    nxt = [i.key for i in c.plan(cur, FetchBudget(max_requests=2)).items]
    assert nxt[0] == b and a not in nxt and q not in nxt  # the failed item first, nothing re-fetched


def test_legacy_1_0_0_cursor_states_are_read() -> None:
    c = builtin_connectors()["eurostat.demography"]
    keys = [i.key for i in c.all_items()]
    legacy_unfinished = Cursor(state={"complete": False, "done": keys[:100], "since_year": 2000})
    assert [i.key for i in c.plan(legacy_unfinished, FetchBudget(max_requests=1000)).items] == keys[100:]
    legacy_finished = Cursor(state={"complete": True, "done": [], "since_year": 2000})
    assert len(c.plan(legacy_finished, FetchBudget(max_requests=1000)).items) == len(keys)
    assert ed.cycle_done_from(None) == set()


def test_every_fixture_item_parses_and_passes_validation(fixture_raws: dict[str, RawAcquisition]) -> None:
    c = connector()
    for key, raw in fixture_raws.items():
        [batch] = list(c.parse(raw))
        q = c.validate(batch)
        assert q.outcome == "pass", (key, q.findings)
        if key.startswith("proj_"):
            assert batch.observations == ()  # projections are never observations
            assert any(f.code == "geo_absent" and "LI" in f.message for f in q.findings)
        else:
            assert len(batch.observations) == batch.metadata["cube_positions"]


def test_status_flags_open_and_unknown_ages_are_preserved(fixture_raws: dict[str, RawAcquisition]) -> None:
    c = connector()
    [batch] = list(c.parse(fixture_raws["demo_pjan:LU:2017"]))
    by_age = {}
    for o in batch.observations:
        by_age.setdefault(o.source_flags["categories"]["age"], []).append(o)
    assert {"Y_OPEN", "UNK", "TOTAL", "Y_LT1"} <= set(by_age)
    assert all(o.source_flags["age_kind"] == "open" and "special_category" in o.source_flags for o in by_age["Y_OPEN"])
    assert all(o.source_flags["cell_type"] != "leaf" for o in by_age["TOTAL"])
    assert all(o.subject_provider == "iso3166_alpha3" and o.subject_key == "LUX" for o in batch.observations)
    assert all(o.support.resolution.keys() == {"age", "sex"} for o in batch.observations)
    flagged = [o for o in batch.observations if o.source_flags["status"]]
    assert flagged and all(o.source_flags["status_labels"] for o in flagged)  # 'b' breaks for LU
    # an open class is a single category, never spread: no single age >= its start in that table
    starts = batch.metadata["open_age_starts"]["LU"]
    assert starts and all(85 <= s <= 100 for s in starts)


def test_windows_merge_into_one_table_without_overlap(fixture_raws: dict[str, RawAcquisition]) -> None:
    c = connector()
    assert c.windows() == [(2008, 2016), (2017, None)]
    parts = [r for k, r in fixture_raws.items() if k.startswith("demo_pjan:LU:")]
    merged = ed.merged_tables("demo_pjan", parts)["LUX"].to_pylist()
    years = sorted({r["year"] for r in merged})
    assert years[0] == 2008 and years[-1] >= 2025 and years == list(range(years[0], years[-1] + 1))
    with pytest.raises(Exception, match="overlapping"):
        ed.merged_tables("demo_pjan", parts + parts[:1])
    for raw in parts:  # each acquisition stays small (lock-table friendly)
        [b] = list(c.parse(raw))
        assert len(b.observations) <= 103 * 3 * 10


def test_dataset_tables_are_pure_functions_of_the_raw_bytes(fixture_raws: dict[str, RawAcquisition]) -> None:
    t1 = ed.dataset_tables(fixture_raws["demo_magec:LI:2008"])
    t2 = ed.dataset_tables(fixture_raws["demo_magec:LI:2008"])
    assert list(t1) == ["LIE"] and t1["LIE"].equals(t2["LIE"])
    tab = t1["LIE"]
    assert tab.schema.names == [n for n, _ in ed.DATASET_COLUMNS]
    rows = tab.to_pylist()
    assert all(isinstance(r["value"], str) or r["value"] is None for r in rows)  # exact text, never float
    assert {r["age_kind"] for r in rows} >= {"single", "open", "unknown", "total"}
    for r in rows:
        if r["value"] is None:
            assert r["missing_reason"] in ("unavailable", "suppressed", "not_applicable")
    europop = ed.dataset_tables(fixture_raws["proj_23nanmig:all"])
    assert list(europop) == ["LUX"]  # no EUROPOP2023 projection for Liechtenstein: a gap, not zeros
    ages = {r["age"]: r for r in europop["LUX"].to_pylist()}
    assert ages["Y_GE100"]["age_kind"] == "open_upper" and ages["Y_GE100"]["age_start"] == 100
    assert any(Decimal(r["value"]) < 0 for r in europop["LUX"].to_pylist() if r["value"])  # negative net migration kept


def test_fertility_age_classes_classified(fixture_raws: dict[str, RawAcquisition]) -> None:
    rows = ed.dataset_tables(fixture_raws["demo_fasec:LU:2008"])["LUX"].to_pylist()
    kinds = {r["age"]: (r["age_kind"], r["cell_type"]) for r in rows}
    assert kinds["Y10-14"] == ("group", "leaf")  # no single ages 10..14 published: a leaf class
    assert kinds["Y15-19"] == ("group", "subtotal")  # overlaps the single ages 15..19
    assert kinds["Y_GE50"] == ("open_upper", "leaf")
    assert kinds["UNK"] == ("unknown", "leaf")


def _override(raw: RawAcquisition, mutate: Any) -> RawAcquisition:
    doc = json.loads(raw.body)
    mutate(doc)
    return raw.model_copy(update={"body": json.dumps(doc).encode()})


def test_unknown_flag_negative_count_and_drift_block(fixture_raws: dict[str, RawAcquisition]) -> None:
    c = connector()
    raw = fixture_raws["demo_pjan:LI:2008"]

    def unknown_flag(d: dict[str, Any]) -> None:
        d["status"] = {"5": "q"}
        d["extension"]["status"] = {"label": {"q": "who knows"}}

    def negative(d: dict[str, Any]) -> None:
        k = next(iter(d["value"]))
        d["value"][k] = -3

    def reorder(d: dict[str, Any]) -> None:
        d["id"] = list(reversed(d["id"]))
        d["size"] = list(reversed(d["size"]))

    def unit(d: dict[str, Any]) -> None:
        d["dimension"]["unit"]["category"]["index"] = {"THS": 0}

    for mutate, code in ((unknown_flag, "unknown_status_flag"), (negative, "negative_count"), (unit, "unit_changed")):
        [b] = list(c.parse(_override(raw, mutate)))
        q = c.validate(b)
        assert q.outcome == "blocked" and any(f.code == code for f in q.findings), code
    [b] = list(c.parse(_override(raw, reorder)))
    assert c.validate(b).outcome == "blocked"


def test_confidentiality_status_is_missing_suppressed_never_zero(fixture_raws: dict[str, RawAcquisition]) -> None:
    c = connector()

    def conf(d: dict[str, Any]) -> None:
        d["value"].pop("0", None)
        d["status"] = {**(d.get("status") or {}), "0": "|N"}
        d["extension"]["status"] = {
            "label": {**((d["extension"].get("status") or {}).get("label") or {}), "|N": "|not for publication"}
        }

    [b] = list(c.parse(_override(fixture_raws["demo_fasec:LU:2008"], conf)))
    assert c.validate(b).outcome == "pass"
    o = next(o for o in b.observations if o.raw_selector["jsonstat_position"] == 0)
    assert o.value == {"kind": "missing", "reason": "suppressed"}
    assert o.source_flags["confidentiality_status"] == "N"
    assert ed.split_status("bep") == ("bep", "") and ed.split_status("p|N") == ("p", "N")


def test_registry_entries_exist() -> None:
    from reva.autonomy.policy import budgets_policy
    from reva.ontology.registry import load_registry

    reg = load_registry()
    ids = {c.concept_id for c in reg.concepts}
    for d in ed.DATASETS.values():
        assert d.concept_id in ids
    assert any(s["source_key"] == "eurostat.demography" for s in reg.sources)
    maps = [m for m in reg.mappings if m["connector"]["name"] == "eurostat.demography"]
    assert {m["target"]["concept_id"] for m in maps} == {ed.DATASETS[k].concept_id for k in ed.OBSERVED}
    assert all(m["mapping_kind"] == "exact" for m in maps)
    assert budgets_policy().content["per_source_daily_requests"]["eurostat.demography"] >= len(
        builtin_connectors()["eurostat.demography"].all_items()
    )
