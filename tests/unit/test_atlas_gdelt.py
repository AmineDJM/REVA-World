"""V6SIGNALS: GDELT media-coverage signals (atlas.gdelt_events -> gdelt.events_cd, atlas.gdelt_doc -> gdelt.doc_theme_d).

Fixtures: tests/fixtures/source/atlas.gdelt_{events,doc} — excerpts of the real GDELT files downloaded 2026-10-17
(event records kept verbatim and re-zipped, most records dropped; DOC timelines cut to a few days; the about page
reduced to its visible text). Licence (GDELT terms: free use, citation + link) in each manifest.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from reva.adapters.connectors import atlas_gdelt as g
from reva.adapters.connectors import hf_daily_common as hc
from reva.atlas.ingest import AtlasRun
from reva.atlas.sources import AtlasSourceSpec
from reva.atlas.tables import validate_atlas_frame
from reva.evidence.runner import FIXTURE_ROOT, FixtureHttp
from reva.kernel.errors import ValidationProblem

SPECS = {s.name: s for s in g.atlas_source()}
EV_DIR = Path(FIXTURE_ROOT) / g.NAME_EVENTS
DOC_DIR = Path(FIXTURE_ROOT) / g.NAME_DOC


class _Run(AtlasRun):
    """An offline run that archives to a dict and validates instead of registering (no database)."""

    def archive(self, raw: Any) -> str:
        sha = hashlib.sha256(raw.body).hexdigest()
        self.archived.append(sha)
        return sha

    def register(self, *, dataset: str, frame: pl.DataFrame, raw_sha256s: list[str], coverage_note: str | None = None,
                 geo_scheme: str | None = None, rights: dict[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
        self.wants(dataset)
        f, unmapped = validate_atlas_frame(frame, source=self.spec.atlas_source, dataset=dataset,
                                           raw_sha256s=raw_sha256s, geo_scheme=geo_scheme)  # fmt: skip
        assert rights and rights["signal_kind"] == g.SIGNAL_KIND and "policy_quotes" not in rights
        assert coverage_note and coverage_note.startswith("MEDIA-COVERAGE SIGNAL, NOT EVENT COUNTS")
        self.registered.append({"dataset": dataset, "frame": f, "unmapped": unmapped})
        return {"dataset_key": f"atlas.{self.spec.atlas_source}.{dataset}", "row_count": f.height, "created": True}


def _run(name: str, **options: Any) -> _Run:
    run = _Run.offline(SPECS[name], FixtureHttp(name))
    assert isinstance(run, _Run)
    run.options = options
    return run


@pytest.fixture(scope="module")
def events() -> tuple[_Run, dict[str, Any], pl.DataFrame]:
    run = _run(g.NAME_EVENTS, end="2026-10-15", days=3)
    out = asyncio.run(g.atlas_ingest(run))
    return run, out, run.registered[0]["frame"]


def _flags(r: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(r["flags"])
    return out


def _records(day: str) -> list[list[str]]:
    man = json.loads((EV_DIR / "manifest.json").read_text())
    e = next(e for e in man if f"/{day}120000.export" in e["url"])
    z = zipfile.ZipFile(io.BytesIO((EV_DIR / e["file"]).read_bytes()))
    return [ln.split("\t") for ln in z.read(z.namelist()[0]).decode().split("\n") if ln]


def test_specs_are_valid_atlas_sources() -> None:
    assert set(SPECS) == {g.NAME_EVENTS, g.NAME_DOC}
    for s in SPECS.values():
        assert isinstance(s, AtlasSourceSpec) and s.atlas_source == "gdelt"
        assert s.field_mappings["signal_kind"] == g.SIGNAL_KIND
    for f in g.FILES_EVENTS:
        assert f.url.split("/")[2] in SPECS[g.NAME_EVENTS].allowed_hosts
    assert g.export_url(date(2026, 1, 2), "120000").split("/")[2] in SPECS[g.NAME_EVENTS].allowed_hosts
    for f in g.FILES_DOC:
        assert f.url.split("/")[2] in SPECS[g.NAME_DOC].allowed_hosts
    w = g.Window(date(2026, 1, 1), date(2026, 1, 2))
    assert g.doc_url("PROTEST", "timelinetone", w).split("/")[2] in SPECS[g.NAME_DOC].allowed_hosts
    assert SPECS[g.NAME_DOC].min_interval_seconds >= 5  # GDELT: "one request every 5 seconds"
    assert SPECS[g.NAME_EVENTS].planned_requests >= g.DEFAULT_DAYS


def test_events_table_rows_flags_and_typed_missing(events: tuple[_Run, dict[str, Any], pl.DataFrame]) -> None:
    _, out, f = events
    rep = out["report"]["events_cd"]
    assert rep["fetched"] == 2 and list(rep["missing"]) == ["20261014120000"]
    assert rep["policy_quotes_found"] == {"use": True, "redistribution": True}
    assert out["tables"][0]["table"] == "gdelt.events_cd"
    rows = f.to_dicts()
    for r in rows:
        fl = _flags(r)
        assert fl["signal_kind"] == "media-coverage signal, not event counts"
        assert fl["licence"] == g.LICENCE and fl["attribution"].startswith("Source: The GDELT Project")
        assert fl["precision"] == "day" and fl["estimate_kind"] == "observation" and fl["slot_utc"] == "120000"
        assert r["period_end"] == r["period_start"] + (date(2026, 1, 2) - date(2026, 1, 1))
        assert r["value"] is None or r["value"] != "0" or r["indicator"].startswith("records_no")  # zeros: no row
    miss = [r for r in rows if r["period_start"] == date(2026, 10, 14)]
    assert len(miss) == 1 and miss[0]["provider_geo"] == "_ALL" and miss[0]["value"] is None
    assert "not available" in _flags(miss[0])["missing"]
    # identifiers only: FIPS -> ISO3 through the crosswalk (UK is the United Kingdom, UP Ukraine, RS Russia)
    iso = {r["provider_geo"]: r["iso3"] for r in rows}
    assert iso["UK"] == "GBR" and iso["UP"] == "UKR" and iso["RS"] == "RUS" and iso["_ALL"] is None
    # nothing from the article side reaches the table (no URLs, actor or place names)
    text = " ".join(str(v) for r in rows for k, v in r.items() if k != "flags")
    urls = set(re.findall(r"https?://[^\s\"]+", text))
    assert all(u.startswith("https://data.gdeltproject.org/gdeltv2/") for u in urls) and "Nottingham" not in text


def test_events_aggregates_match_an_independent_count(events: tuple[_Run, dict[str, Any], pl.DataFrame]) -> None:
    _, _, f = events
    recs = _records("20261015")
    day = f.filter(pl.col("period_start") == date(2026, 10, 15))

    def val(geo: str, ind: str) -> str | None:
        got = day.filter((pl.col("provider_geo") == geo) & (pl.col("indicator") == ind))["value"].to_list()
        return got[0] if got else None

    assert val("_ALL", "records_all") == str(len(recs))
    assert val("_ALL", "records_no_actiongeo") == str(sum(1 for c in recs if not c[53]))
    ua = [c for c in recs if c[53] == "UP"]
    assert val("UP", "records_all") == str(len(ua))
    assert sum(int(val("UP", f"records_quad{q}") or 0) for q in "1234") == len(ua)
    assert sum(int(val("UP", f"records_root{r}") or 0) for r in g.CAMEO_ROOTS) == len(ua)
    gold = sum((Decimal(c[30]) for c in ua), Decimal(0)) / len(ua)
    assert val("UP", "goldstein_mean") == str(gold.quantize(Decimal("0.0001")))
    tone = sum((Decimal(c[34]) for c in ua), Decimal(0)) / len(ua)
    assert val("UP", "avgtone_mean") == str(tone.quantize(Decimal("0.0001")))
    assert val("UP", "mentions_sum") == str(sum(int(c[31]) for c in ua))
    assert val("UP", "articles_sum") == str(sum(int(c[33]) for c in ua))
    r = day.filter((pl.col("provider_geo") == "UP") & (pl.col("indicator") == "goldstein_mean")).to_dicts()[0]
    assert _flags(r)["n"] == len(ua) and _flags(r)["geo_basis"].startswith("ActionGeo_CountryCode")
    assert json.loads(r["selector"])["where"] == "ActionGeo_CountryCode=UP"
    absent = {c[53] for c in recs}
    assert "GZ" not in absent and day.filter(pl.col("provider_geo") == "GZ").height == 0  # observed zero: no row


def test_window_options_are_bounded() -> None:
    today = date(2026, 10, 17)
    w, slot = g.events_window({}, today)
    assert (w.end, (w.end - w.start).days + 1, slot) == (date(2026, 10, 16), g.DEFAULT_DAYS, "120000")
    w, _ = g.events_window({"end": "2015-03-01", "days": "100"}, today)
    assert w.start == g.FIRST_EXPORT_DAY
    for bad in ({"end": "2026-10-17"}, {"days": "0"}, {"days": "99999"}, {"slot": "120500"}, {"end": "x"}):
        with pytest.raises(ValidationProblem):
            g.events_window(bad, today)
    w, themes = g.doc_window({"themes": "PROTEST,ECON_INFLATION"}, today)
    assert w.start == g.FIRST_DOC_DAY and themes == ("PROTEST", "ECON_INFLATION")
    with pytest.raises(ValidationProblem):
        g.doc_window({"themes": "NOT_A_THEME"}, today)


def _zip(stamp: str, text: str, member: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(member or f"{stamp}.export.CSV", text)
    return buf.getvalue()


def test_export_layout_drift_fails_closed() -> None:
    stamp = "20261015120000"
    good = "\t".join(_records("20261015")[0])
    assert g.parse_export(_zip(stamp, good + "\n"), stamp).records == 1
    with pytest.raises(hc.HfError, match="malformed"):
        g.parse_export(_zip(stamp, good + "\textra\n"), stamp)
    with pytest.raises(hc.HfError, match="members"):
        g.parse_export(_zip(stamp, good, member="other.CSV"), stamp)
    with pytest.raises(hc.HfError, match="zip"):
        g.parse_export(b"not a zip", stamp)


def test_cameo_labels_are_checked_against_the_lookup() -> None:
    man = json.loads((EV_DIR / "manifest.json").read_text())
    e = next(e for e in man if e["url"] == g.CAMEO_URL)
    body = (EV_DIR / e["file"]).read_bytes()
    f = hc.Fetched("cameo", g.CAMEO_URL, body, "0" * 64, datetime(2026, 10, 17, tzinfo=UTC))
    assert g.check_cameo(f) == {}
    drifted = hc.Fetched("cameo", g.CAMEO_URL, body.replace(b"14\tPROTEST", b"14\tDEMONSTRATE"), "0" * 64,
                         datetime(2026, 10, 17, tzinfo=UTC))  # fmt: skip
    assert g.check_cameo(drifted) == {"14": "DEMONSTRATE"}


@pytest.mark.skipif(not (DOC_DIR / "manifest.json").exists(), reason="DOC API fixture not captured")
def test_doc_theme_timelines() -> None:
    man = json.loads((DOC_DIR / "manifest.json").read_text())
    opts = json.loads((DOC_DIR / "run_options.json").read_text())
    run = _run(g.NAME_DOC, **opts)
    out = asyncio.run(g.atlas_ingest(run))
    f = run.registered[0]["frame"]
    rep = out["report"]["doc_theme_d"]
    assert rep["policy_quotes_found"] == {"use": True, "redistribution": True}
    rows = f.to_dicts()
    assert rows and {r["provider_geo"] for r in rows} == {"ALL_SOURCES"}
    assert all(r["iso3"] is None for r in rows)
    end = date.fromisoformat(opts["end"])
    assert max(r["period_start"] for r in rows) <= end  # complete days only
    for r in rows:
        fl = _flags(r)
        assert fl["signal_kind"] == g.SIGNAL_KIND and fl["gkg_theme"] in g.THEMES
        assert fl["mode"] in g.DOC_MODES and fl["as_of"] == "2026-10-17"
    inds = {r["indicator"] for r in rows}
    for t in opts["themes"].split(","):
        assert {f"articles_{t.lower()}", f"monitored_{t.lower()}"} <= inds
    # an article count is the provider's integer text, never derived
    e = next(e for e in man if "timelinevolraw" in e["url"])
    data = json.loads((DOC_DIR / e["file"]).read_text())["timeline"][0]["data"]
    p = next(p for p in data if p["norm"] and date.fromisoformat(
        f"{p['date'][:4]}-{p['date'][4:6]}-{p['date'][6:8]}") <= end)  # fmt: skip
    d = date.fromisoformat(f"{p['date'][:4]}-{p['date'][4:6]}-{p['date'][6:8]}")
    theme = e["url"].split("theme:")[1].split("&")[0].lower()
    got = f.filter((pl.col("indicator") == f"articles_{theme}") & (pl.col("period_start") == d))["value"].to_list()
    assert got == [str(p["value"])]


def test_doc_parser_drops_unmonitored_days_and_refuses_coarse_resolution() -> None:
    w = g.Window(date(2026, 10, 1), date(2026, 10, 3))
    at = datetime(2026, 10, 17, tzinfo=UTC)

    def fetched(key: str, doc: dict[str, Any]) -> hc.Fetched:
        return hc.Fetched(key, "https://api.gdeltproject.org/x", json.dumps(doc).encode(), "a" * 64, at)

    vol = {"query_details": {"title": "theme:PROTEST", "date_resolution": "day"}, "timeline": [{
        "series": "Article Count", "data": [
            {"date": "20261001T000000Z", "value": 12, "norm": 90000},
            {"date": "20261002T000000Z", "value": 0, "norm": 0},
            {"date": "20261003T000000Z", "value": 0, "norm": 85000},
            {"date": "20261004T000000Z", "value": 9, "norm": 70000}]}]}  # fmt: skip
    tone = {"query_details": {"title": "theme:PROTEST", "date_resolution": "day"}, "timeline": [{
        "series": "Average Tone", "data": [
            {"date": "20261001T000000Z", "value": -3.25}, {"date": "20261002T000000Z", "value": 0},
            {"date": "20261003T000000Z", "value": 0}]}]}  # fmt: skip
    rows = hc.DailyRows("gdelt", "doc_theme_d")
    rep: dict[str, Any] = {}
    g.parse_doc(rows, "PROTEST", fetched("v", vol), fetched("t", tone), w, rep)
    got = {(r["indicator"], r["period_start"].day): r["value"] for r in rows.rows()}
    assert got == {("articles_protest", 1): "12", ("monitored_protest", 1): "90000", ("articles_protest", 3): "0",
                   ("monitored_protest", 3): "85000", ("tone_protest", 1): "-3.25"}  # fmt: skip
    assert rep["themes"]["PROTEST"]["zero_norm_days"] == 1 and rep["themes"]["PROTEST"]["outside_window"] == 1
    coarse = {**vol, "query_details": {"date_resolution": "month"}}
    with pytest.raises(hc.HfError, match="date_resolution"):
        g.parse_doc(hc.DailyRows("gdelt", "doc_theme_d"), "PROTEST", fetched("v", coarse), None, w, {})
