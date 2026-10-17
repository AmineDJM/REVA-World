"""VIEWS forecast connector (V6 PRED-CONFLICT): parsing of the captured API/wiki responses (offline).

Fixture: tests/fixtures/source/atlas.views -- real bytes downloaded 2026-10-17 (wiki dataset table, run list, two
codebooks, four data pages restricted by the API's own ``iso`` filter to SDN, COL and NOR). Licence: not stated by
VIEWS (citation requested), see the manifest.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import polars as pl
import pytest

from reva.adapters.connectors import atlas_views as av
from reva.atlas.ingest import AtlasRun
from reva.atlas.tables import validate_atlas_frame
from reva.evidence.runner import FixtureHttp

RUNS = "fatalities001_2022_12_t01,fatalities003_2026_08_t01"


@dataclass
class FakeRun:
    """AtlasRun stand-in: real fetch path (``AtlasRun.get`` over the fixture client), archive/register captured."""

    inner: AtlasRun
    options: dict[str, Any]
    archived: list[str] = field(default_factory=list)
    frames: dict[str, pl.DataFrame] = field(default_factory=dict)
    regs: list[dict[str, Any]] = field(default_factory=list)

    def wants(self, ds: str) -> bool:
        return self.inner.wants(ds)

    async def get(self, url: str, **kw: Any) -> Any:
        return await self.inner.get(url, **kw)

    def archive(self, raw: Any) -> str:
        sha = hashlib.sha256(raw.body).hexdigest()
        self.archived.append(sha)
        return sha

    def register(self, *, dataset: str, frame: pl.DataFrame, **kw: Any) -> dict[str, Any]:
        self.frames[dataset] = frame
        self.regs.append(kw)
        return {
            "dataset_key": f"atlas.views.{dataset}",
            "row_count": frame.height,
            "coverage": {"countries": 3},
            "created": True,
        }


@pytest.fixture(scope="module")
def ingested() -> tuple[FakeRun, dict[str, Any]]:
    spec = av.atlas_source()
    run = FakeRun(AtlasRun.offline(spec, FixtureHttp(av.CONNECTOR_NAME)), {"runs": RUNS, "iso": "SDN,COL,NOR"})
    out = asyncio.run(av.atlas_ingest(run))
    return run, out


def test_spec_is_a_valid_atlas_source() -> None:
    s = av.atlas_source()
    assert s.name == "atlas.views" and s.datasets == ("cm_sb",)
    assert set(s.allowed_hosts) == {"api.viewsforecasting.org", "raw.githubusercontent.com"}


def test_wiki_release_dates_are_parsed_verbatim() -> None:
    body = FixtureHttp(av.CONNECTOR_NAME)
    raw = asyncio.run(body.get(av.WIKI_URL)).body
    infos = av.parse_wiki(raw)
    assert infos["fatalities003_2026_08_t01"].release_date == "2026-09-25"
    assert infos["fatalities003_2026_08_t01"].last_input == "2026-08"
    assert infos["fatalities001_2022_12_t01"].release_date == "2023-01-27"
    # the 2021-12 run was released after the year it forecasts: why it is not a default run
    assert infos["fatalities001_2021_12_t01"].release_date == "2023-04-27"
    assert "fatalities001_2021_12_t01" not in av.DEFAULT_RUNS


def test_frame_is_atlas_valid_and_forecast_only(ingested: tuple[FakeRun, dict[str, Any]]) -> None:
    run, out = ingested
    f = run.frames["cm_sb"]
    validate_atlas_frame(f, source="views", dataset="cm_sb", raw_sha256s=sorted(set(f["raw_sha256"])))
    assert f.height == 2 * 2 * 3 * 36  # 2 runs x 2 variables x 3 countries x 36 months
    assert set(f["indicator"].unique()) == {
        "fatalities001_2022_12_t01|sc_cm_sb_main",
        "fatalities001_2022_12_t01|sc_cm_sb_dich_main",
        "fatalities003_2026_08_t01|main_mean",
        "fatalities003_2026_08_t01|main_dich",
    }
    flags = [json.loads(x) for x in f["flags"]]
    assert all(x["estimate_kind"] == "projection" for x in flags)
    assert {x["step"] for x in flags} == set(range(1, 37))
    assert set(f["provider_geo"].unique()) == {"gw:625", "gw:100", "gw:385"}
    assert set(f["iso3"].unique()) == {"SDN", "COL", "NOR"}
    assert out["report"]["null_values"] == 0
    assert run.regs[0]["rights"]["open_licence"] is False


def test_values_are_verbatim(ingested: tuple[FakeRun, dict[str, Any]]) -> None:
    run, _ = ingested
    f = run.frames["cm_sb"]
    sdn = f.filter(
        (pl.col("indicator") == "fatalities003_2026_08_t01|main_mean")
        & (pl.col("iso3") == "SDN")
        & (pl.col("period_start") == pl.date(2026, 9, 1))
    )
    assert sdn["value"].to_list() == ["266.9778"]
    fl = json.loads(sdn["flags"][0])
    assert fl["projection_upstream"]["release_date"] == "2026-09-25" and fl["step"] == 1


def test_codebook_quotes_both_layouts(ingested: tuple[FakeRun, dict[str, Any]]) -> None:
    _, out = ingested
    q = out["codebook_quotes"]
    assert q["fatalities003_2026_08_t01"]["main_dich"].startswith("Predicted probability of state-based armed conflict")
    assert "at least 25 battle-related deaths" in q["fatalities003_2026_08_t01"]["main_dich"]
    assert "ln(fatalities+1)" in q["fatalities001_2022_12_t01"]["sc_cm_sb_main"]


def test_guards() -> None:
    with pytest.raises(av.ViewsParseError):
        av.parse_run("escwa_2021_02_01")
    with pytest.raises(av.ViewsParseError):
        av.check_next(
            "https://api.viewsforecasting.org/r/cm/sb/main_mean?pagesize=1",
            "https://evil.example/r/cm/sb/main_mean?page=2",
        )
    assert av.month_id(2026, 9) == 561  # VIEWS: 1 = January 1980
