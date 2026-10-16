"""A-MILITARY: UN peacekeeping connector (atlas.peacekeeping), offline on the recorded fixture slices."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from reva.adapters.connectors import atlas_peacekeeping as pk
from reva.adapters.connectors.atlas_geopol_common import atlas_frame
from reva.atlas.ingest import AtlasRun
from reva.atlas.tables import validate_atlas_frame
from reva.evidence.runner import FixtureHttp
from reva.kernel.errors import ValidationProblem

REPO = Path(__file__).resolve().parents[2]


def _fetch_all() -> dict[str, Any]:
    run = AtlasRun.offline(pk.atlas_source(), FixtureHttp(pk.CONNECTOR_NAME))

    async def go() -> dict[str, Any]:
        out: dict[str, Any] = {"hdx": {}, "csv": {}}
        for k, (hub, name, _, _) in pk.FILES.items():
            out["hdx"][k] = await run.get(pk.HDX_URL.format(name=name), key=k)
            out["csv"][k] = await run.get(pk.API_URL.format(id=hub), key=k, media_types=("text/csv",))
        return out

    return asyncio.run(go())


@pytest.fixture(scope="module")
def raws() -> dict[str, Any]:
    return _fetch_all()


@pytest.fixture(scope="module")
def built(raws: dict[str, Any]) -> dict[str, Any]:
    qids = json.loads((REPO / "registry/entities/country_qids.json").read_text())["countries"]
    return pk.build(
        raws["csv"], current_year=2026, strict=False, qid_for_iso3={k: v["qid"] for k, v in qids.items()}
    )


def test_spec_is_discovered_and_budgeted_by_the_platform() -> None:
    from reva.atlas.sources import discover

    found, errors = discover(["reva.adapters.connectors.atlas_peacekeeping"])
    assert not errors and pk.CONNECTOR_NAME in found
    spec = found[pk.CONNECTOR_NAME].spec
    assert spec.source_key == "un.psdata" and spec.atlas_source == "unpk"
    assert set(spec.allowed_hosts) == {"api.psdata.un.org", "data.humdata.org"}
    assert spec.planned_requests >= 2 * len(pk.FILES)
    src = (REPO / "src/reva/adapters/connectors/atlas_peacekeeping.py").read_text()
    assert not any(m in src for m in ("make_live_client(", "SafeHttpClient(", "httpx.", "urlopen("))
    reg = yaml.safe_load((REPO / "registry/sources/un_psdata.yaml").read_text())
    assert reg["source_key"] == spec.source_key and set(reg["metadata"]["allowed_hosts"]) == set(spec.allowed_hosts)
    req = yaml.safe_load((REPO / "registry/atlas/budget_requests/a_military.yaml").read_text())
    assert req["requests"][0]["source_key"] == spec.source_key
    assert req["requests"][0]["daily_requests"] >= spec.planned_requests


def test_licence_records_are_checked(raws: dict[str, Any]) -> None:
    for k, raw in raws["hdx"].items():
        rec = pk.check_hdx_record(raw.body, k)
        assert rec["licence_id"] == "cc-by-igo" and rec["hdx_dataset"] == pk.FILES[k][1]
    doc = json.loads(raws["hdx"]["monthly"].body)
    doc["result"]["license_id"] = "other-nc"
    with pytest.raises(ValidationProblem, match="licence"):
        pk.check_hdx_record(json.dumps(doc).encode(), "monthly")
    doc["result"]["license_id"] = "cc-by-igo"
    doc["result"]["organization"]["name"] = "someone-else"
    with pytest.raises(ValidationProblem):
        pk.check_hdx_record(json.dumps(doc).encode(), "monthly")
    with pytest.raises(ValidationProblem):  # record of another dataset
        pk.check_hdx_record(raws["hdx"]["annual"].body, "monthly")


def test_schema_drift_and_truncation_are_refused(raws: dict[str, Any]) -> None:
    body = raws["csv"]["monthly"].body.replace(b"male_personnel,female_personnel", b"men,female_personnel", 1)
    with pytest.raises(ValidationProblem, match="schema"):
        pk.read_csv(body, "monthly", strict=False)
    with pytest.raises(ValidationProblem, match="fewer rows"):
        pk.read_csv(raws["csv"]["monthly"].body, "monthly", strict=True)  # fixture slice < live minimum


def test_historical_states_are_kept_unmapped_with_qids() -> None:
    def day(y: int, m: int, d: int) -> tuple[date, date]:
        return pk.day_period(date(y, m, d))

    assert pk.resolve_geo("RUS", *day(1990, 11, 30))[:2] == (None, "Q15180")
    assert pk.resolve_geo("RUS", *day(1992, 1, 31))[:2] == ("RUS", None)
    assert pk.resolve_geo("CZE", *day(1990, 11, 30))[:2] == (None, "Q33946")
    assert pk.resolve_geo("CZE", *day(1993, 4, 30))[:2] == ("CZE", None)
    assert pk.resolve_geo("SRB", *day(1991, 6, 30))[:2] == (None, "Q36704")
    assert pk.resolve_geo("SRB", *day(2000, 6, 30))[:2] == (None, "Q838261")
    assert pk.resolve_geo("SRB", *day(2004, 12, 31))[:2] == (None, "Q37024")
    assert pk.resolve_geo("SRB", *day(2007, 12, 31))[:2] == ("SRB", None)
    assert pk.resolve_geo("YUG", *pk.year_bounds(1960))[:2] == (None, "Q36704")
    assert pk.resolve_geo("SCG", *pk.year_bounds(2004))[:2] == (None, "Q37024")
    # a year that straddles a dissolution: no country, no historical state (never guessed)
    for code, y in (("RUS", 1991), ("YUG", 1992), ("SRB", 2006), ("SCG", 2003)):
        iso3, qid, flags = pk.resolve_geo(code, *pk.year_bounds(y))
        assert iso3 is None and qid is None and flags["state_transition_in_period"], (code, y)
    assert pk.resolve_geo("YUG", *pk.year_bounds(2005))[:2] == (None, None)  # dissolved code, later date
    assert pk.resolve_geo("FRA", *pk.year_bounds(1948)) == ("FRA", None, {})


def test_historical_qids_come_from_the_registry() -> None:
    doc = json.loads((REPO / "registry/entities/historical_states_qids.json").read_text())
    known = {e["qid"]: e["wikidata_label_en"] for e in doc["entries"]}
    for windows in pk.HISTORICAL_WINDOWS.values():
        for _, qid, _label in windows:
            assert qid in known, qid
    assert known["Q15180"] == "Soviet Union" and known["Q33946"] == "Czechoslovakia"
    assert known["Q838261"] == "Federal Republic of Yugoslavia" and known["Q37024"] == "Serbia and Montenegro"


def test_atlas_frames_validate(built: dict[str, Any]) -> None:
    shas = sorted(built["shas"].values())
    assert set(built["atlas"]) == set(pk.DATASETS)
    for ds, rows in built["atlas"].items():
        f, unmapped = validate_atlas_frame(atlas_frame(rows), source="unpk", dataset=ds, raw_sha256s=shas)
        assert f.height > 0, ds
        if ds in ("contrib_monthly", "contrib_annual"):
            assert not unmapped, (ds, unmapped)


def _rows(built: dict[str, Any], ds: str) -> list[dict[str, Any]]:
    return list(built["atlas"][ds])


def test_values_verbatim_no_fill(built: dict[str, Any], raws: dict[str, Any]) -> None:
    rows = _rows(built, "contrib_monthly")
    # one value per (row, sex) actually reported: empty cells give no row, reported zeros are kept
    zeros = [r for r in rows if r["value"] == "0.0"]
    assert zeros and all(json.loads(r["flags"]).get("missing") is None for r in zeros)
    assert all(r["value"] is not None for r in rows)
    import csv
    import io

    src = list(csv.DictReader(io.StringIO(raws["csv"]["monthly"].body.decode())))
    reported = sum(1 for r in src for c in ("male_personnel", "female_personnel") if r[c].strip())
    assert len(rows) == reported
    r0 = next(r for r in rows if r["provider_geo"] == "IND" and r["indicator"].startswith("uniformed_personnel|"))
    assert r0["unit"] == "persons" and (r0["period_end"] - r0["period_start"]).days == 1


def test_pre2010_overlap_and_totals(built: dict[str, Any]) -> None:
    check = built["pre2010_overlap_check"]
    assert check["overlap_rows"] > 0 and check["overlap_inconsistent"] == 0
    pre = _rows(built, "contrib_pre2010")
    new = [r for r in pre if "type=Individual_Police" in r["indicator"]]
    assert new and all("category_overlap" in json.loads(r["flags"]) for r in new)
    tot = {
        (r["indicator"], r["provider_geo"], r["period_start"]): r
        for r in _rows(built, "totals")
        if r["indicator"] == "uniformed_personnel_total"
    }
    ita = [r for r in pre if r["provider_geo"] == "ITA" and r["period_start"] == date(2009, 12, 31)]
    legacy = sum(float(r["value"]) for r in ita if r["indicator"].rsplit("type=", 1)[1] in pk.PRE2010_LEGACY)
    allcats = sum(float(r["value"]) for r in ita)
    t = tot[("uniformed_personnel_total", "ITA", date(2009, 12, 31))]
    assert float(t["value"]) == legacy < allcats  # the overlapping late-2009 categories are not added
    flags = json.loads(t["flags"])
    assert flags["computed_from"].startswith("unpk ") and flags["cells_summed"] == len(
        [r for r in ita if r["indicator"].rsplit("type=", 1)[1] in pk.PRE2010_LEGACY]
    )
    rus = tot[("uniformed_personnel_total", "RUS", date(1990, 11, 30))]
    assert rus["iso3"] is None and rus["entity_qid"] == "Q15180"


def test_fatalities_repeats_collapsed_and_flagged(built: dict[str, Any]) -> None:
    rows = _rows(built, "fatalities")
    assert built["fatalities_repeats"]["collapsed_identical_repeats"] > 0
    unef = [r for r in rows if r["provider_geo"] == "CAN" and "mission=UNEF|" in r["indicator"]]
    assert unef and all("provider_repeated_rows" in json.loads(r["flags"]) for r in unef)
    assert all(len(json.loads(r["flags"])["provider_repeated_rows"]["mission_ids"]) == 3 for r in unef)
    yug = [r for r in rows if r["provider_geo"] == "YUG"]
    assert yug and all(r["iso3"] is None for r in yug)
    assert {r["entity_qid"] for r in yug if r["period_start"].year <= 1991} == {"Q36704"}
    blank = [r for r in rows if r["provider_geo"] in ("(blank)", "ZZ [Undetermined]", "Occupied Palestinian Territory")]
    assert blank and all(r["iso3"] is None for r in blank)


def test_annual_trailing_space_type_and_incomplete_year(built: dict[str, Any]) -> None:
    rows = _rows(built, "contrib_annual")
    assert not any(r["indicator"].endswith(" |sex=male") or "Troops |" in r["indicator"] for r in rows)
    ury = [r for r in rows if r["provider_geo"] == "ARM" and r["period_start"].year == 2017 and "Troops" in r["indicator"]]
    assert ury and json.loads(ury[0]["flags"]).get("personnel_type_as_published") == "Troops "
    cur = [r for r in rows if r["period_start"].year == 2026]
    assert cur and all(json.loads(r["flags"]).get("year_incomplete_at_retrieval") for r in cur)


def test_catalog_series_exist(built: dict[str, Any]) -> None:
    from reva.atlas.catalog import load_catalog

    cat = load_catalog(strict=False)
    concepts = {k: v for k, v in cat.concepts.items() if k.startswith("security.")}
    assert len(concepts) >= 10
    have = {(r["dataset"], r["indicator"]) for ds in built["atlas"] for r in built["atlas"][ds]}
    present = 0
    for c in concepts.values():
        assert c.theme == "politics"
        for s in c.source_order:
            assert s.source == "unpk"
            present += (s.dataset, s.indicator) in have
    assert present >= 7  # the fixture slice lacks a few personnel types


def test_kg_records(built: dict[str, Any]) -> None:
    ents, stmts, rep = built["entities"], built["statements"], built["kg_report"]
    ids = {e.qid for e in ents}
    assert "unpk:mission:UNIFIL" in ids and "unpk:mission:UNTSO" in ids
    unifil = next(e for e in ents if e.qid == "unpk:mission:UNIFIL")
    assert "Interim Force in Lebanon" in unifil.labels["en"]
    contrib = [s for s in stmts if s.property == "unpk:contributed_personnel_to"]
    assert contrib and all(s.object_qid.startswith("unpk:mission:") for s in contrib)
    ussr = [s for s in contrib if s.subject == "Q15180"]
    assert ussr and ussr[0].start == "1990-11-30" and ussr[0].object_qid == "unpk:mission:UNTSO"
    assert all(s.qualifiers["reporting_dates"] >= 1 and s.start <= s.end for s in contrib)
    keys = [s.key for s in stmts]
    assert len(keys) == len(set(keys))
    mc = {(s.subject, s.object_qid, s.qualifiers["provider_field"].split(" ")[0]) for s in stmts if s.property == "unpk:mission_country"}
    assert ("unpk:mission:UNIFIL", "Q822", "location") in mc  # Lebanon (Field Missions Dashboard)
    assert ("unpk:mission:UNIFIL", "Q822", "Mission_Country") in mc  # Lebanon (pre-2010 file)
    unmik = {o for s, o, f in mc if s == "unpk:mission:UNMIK"}
    assert {"Q37024", "Q403"} <= unmik  # coded SRB by the provider: Serbia and Montenegro in 2005, Serbia in 2009
    assert {"mission": "UNDOF", "location": "Golan Heights"} in rep["mission_country_unmapped"]
    starts = {s.subject: s.value["time"] for s in stmts if s.property == "unpk:start_date"}
    assert starts["unpk:mission:UNTSO"] == "1948-05-29"
    hq = next(s for s in stmts if s.property == "unpk:hq_location" and s.subject == "unpk:mission:MINUSMA")
    assert hq.qualifiers["latitude_as_published"] == "-8,0000"  # verbatim, not corrected


def test_kg_properties_are_valid() -> None:
    from reva.kg.model import PID_RE

    props = pk.kg_properties()
    assert {p.pid for p in props} == set(pk.UNPK_PROPERTIES)
    assert all(PID_RE.match(p.pid) for p in props)
    assert not any(w in (p.label_en or "").lower() for p in props for w in ("cause", "because"))


def test_offline_run_records_fetch_only() -> None:
    run = AtlasRun.offline(pk.atlas_source(), SimpleNamespace(origin="test_fixture"))
    assert run.wants("totals") and pk.files_for("totals") == ["annual", "fatalities", "monthly", "pre2010"]
