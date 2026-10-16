"""A-LAW atlas connectors: offline parser/mapping tests on recorded (or derived-subset) fixtures."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest

from reva.adapters.connectors import (
    atlas_ccp,
    atlas_constitute,
    atlas_factbook,
    atlas_law_common,
    atlas_ssa_ssptw,
    atlas_wbl,
)
from reva.adapters.http.safe_client import HttpResult
from reva.atlas.catalog import load_catalog
from reva.atlas.tables import country_iso3, validate_atlas_frame
from reva.kernel.errors import SourceResponseError, UnsafeRequest

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "source"


def _fixture(conn: str, name: str) -> tuple[bytes, str]:
    body = (FIX / conn / name).read_bytes()
    return body, hashlib.sha256(body).hexdigest()


def _frame(rows, source, dataset, shas, scheme):
    return validate_atlas_frame(
        atlas_law_common.atlas_frame(rows), source=source, dataset=dataset, raw_sha256s=shas, geo_scheme=scheme
    )


def _flags(r) -> dict:
    return json.loads(r["flags"])


# ----------------------------------------------------------------------------- WBL


@pytest.fixture(scope="module")
def panel():
    body, sha = _fixture("atlas.wbl", "panel.xlsx")
    rows = atlas_wbl.parse_panel(body, sha)
    frame, unmapped = _frame(rows, "wbl", "panel_1971_2024", [sha], "wbl")
    return body, rows, frame, unmapped


def test_wbl_panel_values_scores_and_legal_basis(panel):
    _, _rows, frame, unmapped = panel
    assert unmapped == []  # KSV is mapped to XKX by the agent's wbl scheme
    assert set(frame["iso3"].unique()) == {"AFG", "FRA", "SAU", "USA", "XKX"}
    fra = frame.filter(pl.col("iso3") == "FRA")
    assert fra["period_start"].min() == date(1970, 10, 1) and fra["period_start"].max() == date(2023, 10, 1)
    q = "Is the age at which women and men can retire with full pension benefits the same?"
    ans = fra.filter((pl.col("indicator") == q) & (pl.col("period_start") == date(2023, 10, 1))).to_dicts()
    assert len(ans) == 1 and ans[0]["value_text"] in ("Yes", "No") and ans[0]["value"] is None
    f = _flags(ans[0])
    assert f["report_year"] == 2024 and f["as_of"] == "2023-10-01" and f["transcription"] == "verbatim"
    assert any("Code de la S" in lb["text"] for lb in f["legal_basis"]) and f["legal_basis"][0]["column"] == "Pension!J"
    score = fra.filter((pl.col("indicator") == f"{q}|score") & (pl.col("period_start") == date(2023, 10, 1)))
    assert score["value"].to_list() in (["0"], ["1"]) and score["unit"].to_list() == ["score (0/1)"]
    idx = fra.filter(pl.col("indicator") == "WBL INDEX")
    assert idx.height == 54 and idx["unit"].unique().to_list() == ["score (0-100)"]
    sel = json.loads(ans[0]["selector"])
    assert sel["sheet"] == "WBL Panel 2024" and sel["file"].endswith("Historical-Panel-Data.xlsx")
    lb = fra.filter(pl.col("indicator") == "Length of paid maternity leave")
    assert lb["unit"].unique().to_list() == ["days"]


def test_wbl1_2026_aliases_and_header_drift(panel):
    body_panel = panel[0]
    body, sha = _fixture("atlas.wbl", "wbl1_2026.xlsx")
    hdrs = atlas_wbl.panel_headers(body_panel)
    rows = atlas_wbl.parse_wbl1_2026(body, sha, expected_headers=hdrs)
    frame, _ = _frame(rows, "wbl", "wbl1_2026", [sha], "wbl")
    assert frame["period_start"].unique().to_list() == [date(2025, 10, 1)]
    assert set(frame["indicator"].unique()) <= set(hdrs)  # same indicators as the panel
    renamed = frame.filter(pl.col("indicator") == "WBL INDEX").to_dicts()[0]
    assert _flags(renamed)["provider_header"] == "WBL 1.0 INDEX"
    assert "October 1, 2026" in _flags(renamed)["as_of_basis"]  # the file's own inconsistent statement is kept
    bad = list(hdrs)
    bad[3] = "Some other question?"
    with pytest.raises(SourceResponseError, match="headers differ"):
        atlas_wbl.parse_wbl1_2026(body, sha, expected_headers=bad)


def test_wbl2026_retirement_ages_with_statutory_citation():
    body, sha = _fixture("atlas.wbl", "wbl2026.xlsx")
    rows, notes = atlas_wbl.parse_wbl2026(body, sha)
    frame, unmapped = _frame(rows, "wbl", "wbl2026", [sha], "wbl")
    assert unmapped == []
    assert notes["skipped_sheets"] == list(atlas_wbl.WBL26_BROKEN_SHEETS)  # #REF! caches never read
    assert notes["duplicate_cells_identical"] > 0
    fra = frame.filter(pl.col("iso3") == "FRA")
    w = fra.filter(pl.col("indicator") == "Pen_Law_PensAgeEq_FullW").to_dicts()[0]
    assert w["value"] == "67" and w["unit"] == "years" and w["period_start"] == date(2025, 10, 1)
    f = _flags(w)
    assert f["question"].startswith("10.1.1.2") and f["legal_basis"][0]["code"] == "Pen_Law_PensAgeEq_Full_LB"
    assert "L351-8" in f["legal_basis"][0]["text"]
    lf = fra.filter(pl.col("indicator") == "LF_econ_index")
    assert sorted(d.year for d in lf["period_start"]) == [2023, 2024, 2025]  # report years 2024-2026
    # ".." = not reported (too few expert responses): a typed missing row, never a value
    dots = frame.filter(pl.col("flags").str.contains('"provider_notation":".."'))
    assert dots.height == 0 or (dots["value"].is_null().all() and dots["value_text"].is_null().all())
    # "NS"/"N/A" notations stay text
    texts = set(frame.filter(pl.col("indicator") == "Pen_Law_MandAgeEq_Men")["value_text"].drop_nulls())
    assert texts <= {"N/A", "NS"}


def test_wbl2026_error_cell_in_a_topic_sheet_is_drift():
    body, sha = _fixture("atlas.wbl", "wbl2026.xlsx")
    z = zipfile.ZipFile(io.BytesIO(body))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as w:
        for info in z.infolist():
            data = z.read(info)
            if info.filename == "xl/worksheets/sheet15.xml":  # a topic sheet
                data = data.replace(b'<is><t xml:space="preserve">Yes</t></is>', b"<is><t>#REF!</t></is>", 1)
            w.writestr(info, data)
    with pytest.raises(SourceResponseError, match="#REF!"):
        atlas_wbl.parse_wbl2026(out.getvalue(), sha)


# ----------------------------------------------------------------------------- Constitute


def test_constitute_list():
    body, sha = _fixture("atlas.constitute", "constitutions.json")
    rows = atlas_constitute.parse_list(body, sha, datetime(2026, 10, 15, 22, 5, tzinfo=UTC))
    frame, unmapped = _frame(rows, "constitute", "constitutions", [sha], "constitute")
    assert {u["provider_geo"] for u in unmapped} == {"Baden", "Bavaria", "Germany_Prussia"}
    # crosswalk 1.1.0: provider "Taiwan" / "Republic of China" -> TWN; "China" stays CHN (no double mapping)
    geo = dict(zip(frame["provider_geo"], frame["iso3"], strict=True))
    assert geo["Taiwan"] == "TWN" and geo["China"] == "CHN"
    fra = {r["indicator"]: r for r in frame.filter(pl.col("iso3") == "FRA").to_dicts()}
    assert fra["in_force.year_enacted"]["value"] == "1958" and fra["in_force.id"]["value_text"].startswith("France_")
    assert fra["in_force.year_enacted"]["period_start"] == date(2026, 10, 15)
    f = _flags(fra["in_force.year_enacted"])
    assert f["commercial_use"] == "prohibited" and f["transcription"] == "verbatim"
    assert frame.filter(pl.col("iso3") == "USA").filter(pl.col("indicator") == "in_force.year_enacted")[
        "value"
    ].to_list() == ["1789"]
    assert frame.filter(pl.col("indicator").str.starts_with("in_force.year_enacted")).height >= 185
    with pytest.raises(SourceResponseError, match="truncated"):
        atlas_constitute.parse_list(json.dumps(json.loads(body)[:20]).encode(), sha, datetime(2026, 10, 15, tzinfo=UTC))


# ----------------------------------------------------------------------------- CCP


def test_ccp_chronology():
    body, sha = _fixture("atlas.ccp", "chronology_subset.zip")
    rows = atlas_ccp.parse_chronology(body, sha)
    frame, unmapped = _frame(rows, "ccp", "chronology_v6", [sha], "ccp")
    un = {u["provider_geo"] for u in unmapped}
    assert {"340|Yugoslavia", "340|Serbia and Montenegro", "255|Germany (Prussia)"} <= un
    usa = frame.filter((pl.col("iso3") == "USA") & (pl.col("indicator") == "evnttype"))
    assert usa.filter(pl.col("period_start") == date(1789, 1, 1))["value_text"].to_list() == ["new"]
    rus = frame.filter(pl.col("iso3") == "RUS").to_dicts()[0]
    assert "Soviet Union" in _flags(rus)["provider_continuity"]
    srb = frame.filter((pl.col("provider_geo") == "340|Serbia") & (pl.col("period_start") == date(2006, 1, 1)))
    assert srb.filter(pl.col("indicator") == "evnttype")["value_text"].to_list() == ["new"]


def test_taiwan_maps_where_the_provider_labels_it_taiwan():
    # crosswalk 1.1.0 added TWN; each provider's own Taiwan unit maps to it, its China unit stays CHN
    assert country_iso3("713|Taiwan", "ccp") == "TWN"  # CCP COW 713 "Taiwan" (1949-2019)
    assert country_iso3("710|China", "ccp") == "CHN"  # incl. the 1947 ROC constitution on the mainland
    assert country_iso3("taiwan", "ssptw") == "TWN"  # SSPTW page "Taiwan (China)"
    assert country_iso3("Taiwan", "constitute") == "TWN"  # "Taiwan (Republic of China) 1947 (rev. 2005)"
    assert country_iso3("China", "constitute") == "CHN"


def test_ccp_characteristics_codebook_transcription():
    body, sha = _fixture("atlas.ccp", "characteristics_subset.zip")
    meta = {e["file"]: e for e in json.loads((FIX / "atlas.ccp" / "manifest.json").read_text())}
    derived = meta["characteristics_subset.zip"]["derived_from"]
    assert meta["characteristics_subset.zip"]["sha256"] == sha and derived["real_bytes"] > 100_000_000
    rows = atlas_ccp.parse_characteristics(body, sha)
    frame, _ = _frame(rows, "ccp", "characteristics_v5", [sha], "ccp")
    usa = frame.filter((pl.col("iso3") == "USA") & (pl.col("period_start") == date(2020, 1, 1)))
    by = {r["indicator"]: r for r in usa.to_dicts()}
    assert by["systyear"]["value"] == "1789"
    fra = frame.filter((pl.col("iso3") == "FRA") & (pl.col("indicator") == "votemin")).to_dicts()[-1]
    assert fra["value"] is None and fra["value_text"] == "adult/majority/age of majority"
    f = _flags(fra)
    assert f["provider_code"] == "4" and f["codebook"]["page"] == 103 and f["constitution_article"] == "3"
    assert f["transcription"].startswith("transcribed by agent A-LAW from") and "review pending" in f["transcription"]
    num = frame.filter((pl.col("indicator") == "votemin") & pl.col("value").is_not_null())
    assert set(num["value"]) <= {"18", "20", "21"} and num.height > 0
    # Serbia 2006: two systems in one country-year -> the second observation is folded into the indicator
    assert frame.filter(pl.col("indicator").str.contains(r"\|systid=")).height > 0


def test_ccp_unknown_code_is_drift():
    body, sha = _fixture("atlas.ccp", "characteristics_subset.zip")
    z = zipfile.ZipFile(io.BytesIO(body))
    member = atlas_ccp.ITEMS["characteristics"]["member"]
    text = z.read(member).decode()
    header, *lines = text.splitlines()
    cols = header.split(",")
    i = cols.index("cappun")
    first = lines[0].split(",")
    first[i] = "42"
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as w:
        w.writestr(member, "\n".join([header, ",".join(first)]) + "\n")
    with pytest.raises(SourceResponseError, match="not in the codebook"):
        atlas_ccp.parse_characteristics(out.getvalue(), sha)


def test_ccp_source_declares_the_bulk_response_bound():
    spec = atlas_ccp.atlas_source()
    assert spec.max_response_bytes >= 118_231_611  # the real characteristics ZIP (2026-10-15)
    assert {"utexas.box.com", "utexas.app.box.com", "public.boxcloud.com"} <= set(spec.allowed_hosts)


# ----------------------------------------------------------------------------- Factbook


def test_factbook_profiles():
    rows, shas = [], []
    for name, region, gec in (
        ("europe_fr.json", "europe", "fr"),
        ("east-n-southeast-asia_tw.json", "east-n-southeast-asia", "tw"),
        ("europe_kv.json", "europe", "kv"),
        ("middle-east_sa.json", "middle-east", "sa"),
    ):
        body, sha = _fixture("atlas.factbook", name)
        shas.append(sha)
        rows += atlas_factbook.parse_profile(body, gec, region, sha)
    frame, unmapped = _frame(rows, "factbook", "legal_2026_01", shas, "factbook_gec")  # A-FACTBOOK's GEC scheme
    assert unmapped == []  # TW -> TWN since TWN joined the crosswalk
    assert frame.filter(pl.col("provider_geo") == "TW")["iso3"].unique().to_list() == ["TWN"]
    fra = {r["indicator"]: r for r in frame.filter(pl.col("iso3") == "FRA").to_dicts()}
    assert fra["legal_system"]["value_text"].startswith("civil law")
    assert fra["suffrage.minimum_age"]["value"] == "18"
    assert fra["legal_system.families_mentioned"]["value_text"] == "civil"
    assert "transcribed by agent A-LAW" in _flags(fra["suffrage.minimum_age"])["transcription"]
    assert _flags(fra["legal_system"])["transcription"] == "verbatim"
    assert fra["legal_system"]["period_start"] == date(2026, 1, 22)
    sau = {r["indicator"]: r for r in frame.filter(pl.col("iso3") == "SAU").to_dicts()}
    assert "islamic" in sau["legal_system.families_mentioned"]["value_text"]
    assert frame.filter(pl.col("iso3") == "XKX").height > 0  # GEC KV -> Kosovo via FIPS


def test_factbook_age_rule_is_conservative():
    assert atlas_factbook._AGE.match("18 years of age; universal").group(1) == "18"
    assert atlas_factbook._AGE.match("18-65 years of age; universal") is None
    assert atlas_factbook._AGE.match("limited; men only") is None
    assert atlas_factbook.clean_text("Parliament&rsquo;s <br> vote") == "Parliament’s vote"


# ----------------------------------------------------------------------------- SSA, fetch outcomes


class _Run:
    """Minimal stand-in for reva.atlas.ingest.AtlasRun (fetch-only paths; archive/register unused)."""

    def __init__(self, http, datasets=("ssptw",)):
        self.http, self.origin, self.datasets = http, "test_fixture", datasets

    def wants(self, dataset):
        return dataset in self.datasets

    async def get(self, url, *, key, media_types=("application/json",), headers=None, max_bytes=None, params=None):
        from reva.adapters.http.safe_client import request_fingerprint
        from reva.ports.connectors import FetchItem, RawAcquisition

        r = await self.http.get(url, max_bytes=max_bytes)
        return RawAcquisition(
            item=FetchItem(key=key, url=url, max_bytes=max_bytes or 1),
            body=r.body,
            media_type=r.media_type or media_types[0],
            retrieved_at=r.retrieved_at,
            response_metadata=r.response_metadata(),
            request_fingerprint=request_fingerprint("GET", url),
            origin="test_fixture",
        )

    def archive(self, raw):  # pragma: no cover - a blocked probe archives nothing
        raise AssertionError("nothing must be archived")


def _ssa_page(name):
    body, sha = _fixture("atlas.ssa_ssptw", f"europe_{name}.html")
    guide, _ = _fixture("atlas.ssa_ssptw", "europe_guide.html")
    period, quote = atlas_ssa_ssptw.as_of_month(guide, "europe")
    return atlas_ssa_ssptw.parse_country_page(body, "europe", name, sha, period, quote), sha


def test_ssptw_index_guide_and_pages():
    idx, _ = _fixture("atlas.ssa_ssptw", "europe_index.html")
    slugs = atlas_ssa_ssptw.index_pages(idx, "europe")
    assert len(slugs) == 45 and "france" in slugs and "guide" not in slugs
    guide, _ = _fixture("atlas.ssa_ssptw", "europe_guide.html")
    period, quote = atlas_ssa_ssptw.as_of_month(guide, "europe")
    assert period == (date(2018, 1, 1), date(2018, 2, 1)) and "in force in January 2018" in quote
    rows, sha = [], []
    for name in ("france", "germany", "switzerland", "slovakia", "poland"):
        r, h = _ssa_page(name)
        rows += r
        sha.append(h)
    frame, unmapped = _frame(rows, "ssa", "ssptw_2018_2019", sha, "ssptw")
    assert unmapped == []
    by = {(r["iso3"], r["indicator"]): r for r in frame.to_dicts()}
    # France: rule is stated as "legal minimum retirement age is 62 ... full pension is 67" -> text only
    assert ("FRA", "old_age.pension_age.men") not in by
    q = by[("FRA", "old_age.qualifying_conditions.first")]
    assert "legal minimum retirement age is 62" in q["value_text"] and q["period_start"] == date(2018, 1, 1)
    sel = json.loads(q["selector"])
    assert sel["file"] == "2018-2019/europe/france.html" and sel["section"].endswith("Qualifying Conditions")
    assert "6.9%" in by[("FRA", "old_age.contributions.insured_person")]["value_text"]
    assert by[("FRA", "old_age.first_law")]["value_text"].startswith("1910")
    # Switzerland: "Age 65 (men) or age 64 (women)"
    assert by[("CHE", "old_age.pension_age.men")]["value"] == "65"
    assert by[("CHE", "old_age.pension_age.women")]["value"] == "64"
    f = _flags(by[("CHE", "old_age.pension_age.women")])
    assert (
        f["rule"] == "age_men_or_women"
        and "review pending" in f["transcription"]
        and "Age 65 (men)" in f["source_text"]
    )
    # Germany: "Age 65 and 7 months" -> exact months, not a rounded decimal year
    assert by[("DEU", "old_age.pension_age_months.men")]["value"] == str(65 * 12 + 7)
    assert ("DEU", "old_age.pension_age.men") not in by
    # Poland: "Age 65 with at least 25 years of coverage (men) or age 60 with ... (women)"
    assert (by[("POL", "old_age.pension_age.men")]["value"], by[("POL", "old_age.pension_age.women")]["value"]) == (
        "65",
        "60",
    )
    # Slovakia: "Age 62 and 139 days" -> nothing derived
    assert not any(k[0] == "SVK" and "pension_age" in k[1] for k in by)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Old-age pension: Age 60 (men) or age 55 (women and veterans) with 15 years.", ((60, 0), (55, 0))),
        ("Old-age pension: Age 60 (women) or age 65 (men).", ((65, 0), (60, 0))),
        (
            "Old-age pension: Age 64 and one month (men, rising) or age 61 and two months (women, rising).",
            ((64, 1), (61, 2)),
        ),
        ("Old-age pension: Age 67 (age 60 for some categories of seamen) with residence.", ((67, 0), (67, 0))),
        ("Old-age pension: Age 61 (gradually rising to age 65 by 2034) with 20 years.", ((61, 0), (61, 0))),
        ("Old-age pension: Age 56 (blue-collar workers), age 58 (white-collar workers).", None),
        ("Old-age pension: Age 65 (men), age 60 (women), age 62 (civil servants).", None),
        ("Old-age pension: Age 65 (gradually rising) or age 60 (women insured before 1975).", None),
        ("Old-age pension: The retirement age is flexible, from age 60 to age 65.", None),
        ("Old-age pension: Age 62 and 139 days with 15 years.", None),
    ],
)
def test_pension_age_rules_are_conservative(text, expected):
    got = atlas_ssa_ssptw.pension_ages(text)
    assert (None if got is None else (got["men"], got["women"])) == expected


async def test_fetch_maps_refusals_to_blocked():
    class Raises:
        def __init__(self, exc):
            self.exc = exc

        async def get(self, url, **kw):
            raise self.exc

    url = "https://example.org/x"
    got = await atlas_law_common.fetch(
        _Run(Raises(SourceResponseError("HTTP 403", retryable=False, status=403))), url, key="x", source_key="s"
    )
    assert isinstance(got, atlas_law_common.Blocked) and got.reason == "http_403"
    got = await atlas_law_common.fetch(
        _Run(Raises(UnsafeRequest("declared content-length 9 exceeds limit 5"))), url, key="x", source_key="s"
    )
    assert got.reason == "response_cap"
    got = await atlas_law_common.fetch(
        _Run(Raises(SourceResponseError("no fixture", retryable=False, kind="fixture_missing"))),
        url,
        key="x",
        source_key="s",
    )
    assert got.reason == "fixture_missing"

    class Ok:
        async def get(self, url, **kw):
            return HttpResult(url, url, 200, {"content-type": "application/json"}, b"[]", datetime.now(UTC), 0.0, (), 1)

    raw = await atlas_law_common.fetch(_Run(Ok()), url, key="x", source_key="s")
    assert raw.body == b"[]" and raw.origin == "test_fixture"


def test_sources_discovered_and_geo_schemes_load():
    from reva.atlas.sources import discover

    mods = [f"reva.adapters.connectors.atlas_{m}" for m in ("wbl", "constitute", "ccp", "factbook", "ssa_ssptw")]
    sources, errors = discover(mods)
    assert errors == [] and set(sources) == {
        "atlas.wbl",
        "atlas.constitute",
        "atlas.ccp",
        "atlas.factbook",
        "atlas.factbook_disputes",  # A-FACTBOOK's second Factbook source (same module)
        "atlas.ssa_ssptw",
    }
    for e in sources.values():
        assert e.spec.template == "atlas_bulk" and e.spec.code_digest.startswith("sha256:")
        assert e.spec.budget_key == e.spec.source_key
    # atlas.factbook is now A-FACTBOOK's merged source (261 profiles incl. oceans/World; 3 tables)
    assert sources["atlas.factbook"].spec.planned_requests == len(atlas_factbook.items()) == 261
    assert country_iso3("KSV", "wbl") == "XKX" and country_iso3("FRA", "wbl") == "FRA"
    assert country_iso3("Cote_d_Ivoire", "constitute") == "CIV"
    assert country_iso3("Germany_Prussia", "constitute") is None
    assert country_iso3("365|Russia (Soviet Union)", "ccp") == "RUS"
    assert country_iso3("340|Yugoslavia", "ccp") is None
    # every predecessor+successor name the parser flags is mapped (to the successor) by the ccp scheme
    assert all(country_iso3(g, "ccp") is not None for g in atlas_ccp.PROVIDER_CONTINUITY)


# ----------------------------------------------------------------------------- catalog


def test_law_catalog_series_exist_in_parsed_tables(panel):
    cat = load_catalog()
    law = cat.by_domain("law")
    assert len(law) >= 25 and all(c.provider_declared for c in law)
    produced: set[tuple[str, str, str]] = set()
    produced |= {("wbl", "panel_1971_2024", i) for i in panel[2]["indicator"].unique()}
    b, s = _fixture("atlas.wbl", "wbl2026.xlsx")
    produced |= {("wbl", "wbl2026", r["indicator"]) for r in atlas_wbl.parse_wbl2026(b, s)[0]}
    b, s = _fixture("atlas.wbl", "wbl1_2026.xlsx")
    produced |= {("wbl", "wbl1_2026", r["indicator"]) for r in atlas_wbl.parse_wbl1_2026(b, s)}
    b, s = _fixture("atlas.constitute", "constitutions.json")
    produced |= {
        ("constitute", "constitutions", r["indicator"]) for r in atlas_constitute.parse_list(b, s, datetime.now(UTC))
    }
    b, s = _fixture("atlas.ccp", "chronology_subset.zip")
    produced |= {("ccp", "chronology_v6", r["indicator"]) for r in atlas_ccp.parse_chronology(b, s)}
    b, s = _fixture("atlas.ccp", "characteristics_subset.zip")
    produced |= {("ccp", "characteristics_v5", r["indicator"]) for r in atlas_ccp.parse_characteristics(b, s)}
    b, s = _fixture("atlas.factbook", "europe_fr.json")
    produced |= {
        ("factbook", "legal_2026_01", r["indicator"]) for r in atlas_factbook.parse_profile(b, "fr", "europe", s)
    }
    for name in ("germany", "switzerland", "france"):
        produced |= {("ssa", "ssptw_2018_2019", r["indicator"]) for r in _ssa_page(name)[0]}
    missing = [(c.id, x.key) for c in law for x in c.source_order if (x.source, x.dataset, x.indicator) not in produced]
    assert missing == []
