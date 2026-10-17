"""V6FINDATA finance connectors, offline on the captured fixture excerpts (real provider bytes, subsets)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reva.adapters.connectors import atlas_bis_fin as bis
from reva.adapters.connectors import atlas_cb_balance as cb
from reva.adapters.connectors import atlas_ff_factors as ff
from reva.adapters.connectors import atlas_imf_fin as imf
from reva.adapters.connectors import findata_common as fd
from reva.adapters.connectors import hf_daily_common as hc
from reva.atlas.snapshot import normalise_rights

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "source"


def _files(conn: str, specs: tuple[hc.FileSpec, ...]) -> dict[str, hc.Fetched]:
    man = {e["url"]: e for e in json.loads((FIX / conn / "manifest.json").read_text())}
    out = {}
    for s in specs:
        e = man[s.url]
        body = (FIX / conn / e["file"]).read_bytes()
        out[s.key] = hc.Fetched(s.key, s.url, body, e["sha256"], datetime(2026, 10, 17, tzinfo=UTC))
    return out


def _rows(build: Any, files: dict[str, hc.Fetched], wanted: tuple[str, ...]) -> tuple[dict[str, Any], dict[str, Any]]:
    out, rep = build(files, wanted)
    return {ds: b.rows.frame() for ds, b in out.items()}, rep


def _flags(frame: Any) -> list[dict[str, Any]]:
    return [json.loads(x) for x in frame["flags"].unique().to_list()]


def test_bis_api_tables() -> None:
    frames, rep = _rows(bis.build, _files("atlas.bis_fin", bis.API_FILES), bis.API_DATASETS)
    assert all(rep["policy_quotes_found"].values())
    lbs = frames["lbs_rep"]
    assert set(lbs["indicator"].unique()) <= {f"LBS_REP|l_measure={m}|l_position={p}" for m in "SF" for p in "CL"}
    assert lbs["unit"].unique().to_list() == ["US dollar (USD) x10^6"]
    assert set(frames["lbs_cp"]["provider_geo"].unique()) <= {"FR", "CN", "5J", "2D"}
    ds = frames["dsec_gov"]
    assert ds["unit"].unique().to_list() == ["US dollar (USD) x10^9"]
    for fl in _flags(ds) + _flags(lbs):
        assert fl["licence"] == bis.LICENCE and fl["as_of"] == "2026-10-17" and fl["precision"] == "quarter"
    assert (ds["period_end"] - ds["period_start"]).dt.total_days().max() <= 92


def test_bis_unquoted_code_fails_closed() -> None:
    files = _files("atlas.bis_fin", bis.API_FILES)
    f = files["dsec_csv"]
    files["dsec_csv"] = hc.Fetched(f.key, f.url, f.body.replace(b",S13,", b",S1314,"), f.sha256, f.retrieved_at)
    with pytest.raises(hc.HfError):
        bis.build(files, ("dsec_gov",))


def test_bis_bulk_tables() -> None:
    frames, rep = _rows(bis.build, _files("atlas.bis_fin_bulk", bis.BULK_FILES), bis.BULK_DATASETS)
    cbta = frames["cbta"]
    assert cbta.height and any(i.startswith("CBTA|freq=M|") for i in cbta["indicator"].unique())
    us = [fl for fl in _flags(cbta) if "data_comp" in fl]
    assert us and all(fl["precision"] in ("month", "quarter", "year") for fl in _flags(cbta))
    assert frames["gli"].height and all("title" in fl for fl in _flags(frames["gli"]))
    assert all(rep["policy_quotes_found"].values())


def test_imf_tables() -> None:
    frames, rep = _rows(imf.build, _files("atlas.imf_fin", imf.FILES), imf.DATASETS)
    cofer = frames["cofer"]
    assert set(cofer["unit"].unique()) <= {"US dollars", "percent (share)"}
    assert set(cofer["provider_geo"].unique()) == {"G001"}
    gdd = frames["gdd"]
    fl = _flags(gdd)
    assert all("data_source" in f and "source" not in f for f in fl)  # obs-level SOURCE placeholder not copied
    assert all(f["licence_review"] for f in fl)
    assert rep["gdd"]["no_value"] >= 0 and frames["hpd"]["unit"].unique().to_list() == ["percent of fiscal-year GDP"]


def test_ecb_tables() -> None:
    frames, rep = _rows(cb.build, _files("atlas.ecb_fin", cb.ECB_FILES), cb.ECB_DATASETS)
    assert all(rep["policy_quotes_found"].values()), rep["policy_quotes_found"]
    w = frames["wfs_w"]
    assert (w["period_end"] - w["period_start"]).dt.total_days().unique().to_list() == [7]
    assert w["period_start"].dt.weekday().unique().to_list() == [1]  # ISO Monday
    assert w["unit"].unique().to_list() == ["Euro (EUR) x10^6"]
    lty = frames["lty_m"]
    assert {"LTIR|currency_trans=EUR", "LTIR|currency_trans=BGN"} <= set(lty["indicator"].unique())
    yc = frames["yc_d"]
    assert all(f["data_licence_note"] for f in _flags(yc))


def test_h41_levels_and_week_averages() -> None:
    frames, rep = _rows(cb.build, _files("atlas.frb_h41", cb.FRB_FILES), cb.FRB_DATASETS)
    h = frames["h41_w"]
    r = rep["h41_w"]
    assert r["series_type_L_skipped"] >= 1 and r["series_type_ACW_skipped"] >= 1  # district + change series skipped
    assert r.get("no_row_status_NA", 0) >= 1 and "-999999" not in h["value"].to_list()
    days = {
        f["series_type"][0]: d
        for f, d in zip(
            [json.loads(x) for x in h["flags"]], (h["period_end"] - h["period_start"]).dt.total_days(), strict=True
        )
    }
    assert days == {"L": 1, "A": 7}
    assert all(rep["policy_quotes_found"].values())


def test_boe_tables() -> None:
    frames, rep = _rows(cb.build, _files("atlas.boe_balance", cb.BOE_FILES), cb.BOE_DATASETS)
    assert frames["weekly_w"].height and frames["bs_q"].height
    assert all(rep["policy_quotes_found"].values())
    assert frames["bs_q"]["unit"].unique().to_list() == ["Pounds sterling (GBP) x10^6"]


def test_ff_factors_restricted() -> None:
    frames, rep = _rows(ff.build, _files("atlas.ff_factors", ff.FILES), ff.DATASETS)
    for ds, fr in frames.items():
        assert fr.height, ds
        assert all(f["licence_restriction"] == "no_licence_granted" for f in _flags(fr))
    assert "FF|factor=Mkt-RF|freq=A" in frames["ff3"]["indicator"].unique().to_list()
    r = normalise_rights({k: v for k, v in ff.RIGHTS["ff3"].items() if k != "policy_quotes"}, basis="artifact")
    assert r is not None and r["restricted"] and r["open_licence"] is False


def test_iso_week_and_periods() -> None:
    s, e = fd.iso_week("2026-W41")
    assert (s.isoformat(), e.isoformat()) == ("2026-10-05", "2026-10-12")
    with pytest.raises(hc.HfError):
        fd.iso_week("2026-41")
    assert fd.period_of("Q", "2025-Q4")[1].isoformat() == "2026-01-01"


def test_specs_declared() -> None:
    names = {s.name for s in bis.atlas_source()} | {s.name for s in cb.atlas_source()}
    assert names == {"atlas.bis_fin", "atlas.bis_fin_bulk", "atlas.ecb_fin", "atlas.frb_h41", "atlas.boe_balance"}
    assert imf.atlas_source().source_key == "imf.sdmx" and ff.atlas_source().source_key == "kfrench.library"
