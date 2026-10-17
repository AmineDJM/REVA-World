"""V8SCORES: the scoreboard registry cannot claim more than its sources.

* the committed ``registry/scoreboard/tracks.yaml`` validates against the repository's result files and reports;
* every committed row whose source file is unchanged is exactly what a rebuild produces (no hand edit);
* tampering (an upgraded verdict, a changed ratio, a missing source, a win on development data, a downgrade
  without a quote, an invented quote, a pending row without a date) is caught;
* the headline counts only comparisons against official forecasters.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from reva.v8.scores import build as B
from reva.v8.scores import catalog as C
from reva.v8.scores import registry as REG
from reva.v8.scores import service as S

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def doc() -> dict[str, Any]:
    return yaml.safe_load((REPO / REG.PATH).read_text(encoding="utf-8"))


def _row(doc: dict[str, Any], pred: Any) -> dict[str, Any]:
    return next(r for r in doc["rows"] if pred(r))


def test_committed_registry_is_valid(doc: dict[str, Any]) -> None:
    assert doc["schema"] == C.SCHEMA
    assert REG.validate(doc, REPO) == []
    assert len(doc["rows"]) > 50


def test_committed_rows_equal_a_rebuild_when_their_source_is_unchanged(doc: dict[str, Any]) -> None:
    fresh = {r["id"]: r for r in B.build(B.Sources(REPO))["rows"]}
    checked = 0
    for r in doc["rows"]:
        src = REPO / r["source"]["file"]
        if not src.is_file() or B.Sources(REPO, tracked_only=False).sha(r["source"]["file"]) != r["source"]["sha256"]:
            continue  # the source moved on: the API flags it stale; a rebuild refreshes it
        assert r["id"] in fresh, f"{r['id']} is not produced by the builder (hand-added?)"
        assert r == fresh[r["id"]], f"{r['id']} differs from a rebuild (hand-edited?)"
        checked += 1
    assert checked > 50


@pytest.mark.parametrize(
    ("mutate", "expect"),
    [
        (lambda d: _row(d, lambda r: r["verdict"] == "ne_bat_pas" and "pointer" in r["source"]).update(verdict="bat"),
         "win"),
        (lambda d: _row(d, lambda r: r["verdict"] == "bat").update(ratio=0.1), "differs from source"),
        (lambda d: _row(d, lambda r: r["verdict"] == "bat")["source"].update(file="docs/dev/nope.json"), "missing"),
        (lambda d: _row(d, lambda r: r["verdict"] == "bat").update(evaluation="development"), "development"),
        (lambda d: _row(d, lambda r: "verdict_quote" in r and r["verdict"] == "ne_bat_pas"
                        and r["track"] == "beat_energy").pop("verdict_quote"), "downgrade needs"),
        (lambda d: _row(d, lambda r: r["verdict"] == "egalite").update(verdict_quote=None), "égalité"),
        (lambda d: _row(d, lambda r: "verdict_quote" in r and r["verdict_quote"])["verdict_quote"].update(
            text="REVA beats the IMF everywhere"), "not found verbatim"),
        (lambda d: _row(d, lambda r: r["verdict"] == "en_attente").update(resolution={}), "pending without"),
        (lambda d: _row(d, lambda r: r["verdict"] == "ne_bat_pas" and "quote" in r["source"]).update(verdict="bat"),
         "quote"),
        (lambda d: _row(d, lambda r: r.get("p_holm_basis") == "computed" and not r["holm_rejected"]).update(
            p_holm=0.001), "Holm"),
        (lambda d: d["rows"].append(copy.deepcopy(d["rows"][0])), "duplicate"),
    ],
)  # fmt: skip
def test_tampering_is_caught(doc: dict[str, Any], mutate: Any, expect: str) -> None:
    d = copy.deepcopy(doc)
    mutate(d)
    problems = REG.validate(d, REPO)
    assert problems, "tampering not detected"
    assert any(expect in p for p in problems), problems


def test_load_quarantines_rows_whose_source_no_longer_supports_them(tmp_path: Path, doc: dict[str, Any]) -> None:
    d = copy.deepcopy(doc)
    bad = _row(d, lambda r: r["verdict"] == "ne_bat_pas" and "pointer" in r["source"] and r["benchmark_kind"]
               in C.HEADLINE_BENCHMARKS)  # fmt: skip
    bad["verdict"] = "bat"
    (tmp_path / "registry/scoreboard").mkdir(parents=True)
    (tmp_path / REG.PATH).write_text(yaml.safe_dump(d), encoding="utf-8")
    for f in {s["file"] for s in d["sources"]} | {r["source"]["file"] for r in d["rows"]}:
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_bytes((REPO / f).read_bytes())
    for r in d["rows"]:
        for k in ("report",):
            if r.get(k):
                (tmp_path / r[k]).parent.mkdir(parents=True, exist_ok=True)
                (tmp_path / r[k]).write_bytes((REPO / r[k]).read_bytes())
        for q in (r.get("verdict_quote"), (r.get("resolution") or {}).get("quote"), r.get("protocol")):
            if q and q.get("file"):
                (tmp_path / q["file"]).parent.mkdir(parents=True, exist_ok=True)
                (tmp_path / q["file"]).write_bytes((REPO / q["file"]).read_bytes())
    for q in [n["quote"] for n in d["no_track"]] + [e["quote"] for e in d["events"]]:
        (tmp_path / q["file"]).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / q["file"]).write_bytes((REPO / q["file"]).read_bytes())
    with pytest.raises(REG.ScoreboardInvalid):
        REG.load(tmp_path, strict=True)
    loaded = REG.load(tmp_path)
    q = next(r for r in loaded["rows"] if r["id"] == bad["id"])
    assert q["quarantined"] and q["problems"]
    assert REG.headline(loaded["rows"])["bat"] == REG.headline(doc["rows"])["bat"]


def test_holm_adjust_matches_the_step_down_rule() -> None:
    assert B.holm_adjust([0.01, 0.04, 0.03, None]) == pytest.approx([0.04, 0.09, 0.09, 1.0])
    assert B.holm_adjust([]) == []


def test_headline_counts_only_official_benchmarks(doc: dict[str, Any]) -> None:
    rows = doc["rows"]
    h = REG.headline(rows)
    assert sum(h.values()) == sum(1 for r in rows if r["benchmark_kind"] in C.HEADLINE_BENCHMARKS)
    weak_wins = [r for r in rows if r["benchmark_kind"] == "weak_reference" and r["verdict"] == "bat"]
    assert weak_wins, "the V6 weak-benchmark wins must stay visible (and counted apart)"
    for r in weak_wins:
        assert "weak_benchmark" in r["caveats"]
        assert not REG.counts_in_headline(r)


def test_every_win_is_a_structured_non_development_result_with_its_caveats(doc: dict[str, Any]) -> None:
    for r in doc["rows"]:
        if r["verdict"] != "bat":
            continue
        assert r["evaluation"] != "development"
        assert "pointer" in r["source"] and r["ratio"] < 1
        if r["candidate_kind"] in ("combination_with_official", "post_processed_official"):
            assert "uses_official_forecast" in r["caveats"], r["id"]


def test_known_verdicts_are_copied_exactly(doc: dict[str, Any]) -> None:
    by = {r["id"]: r for r in doc["rows"]}
    macro = by.get("beat_macro.holdout.g7rt.unemployment_rate.cy.bm_stack.imf_weo.ae")
    if macro is not None:  # BEAT-MACRO: one confirmed hypothesis of 7
        assert macro["verdict"] == "bat" and macro["ratio"] == pytest.approx(0.629713)
        assert macro["holm_family_size"] == 7
    void = by.get("beat_energy.W2.PAPR_NONOPEC.h3.steo_bc.steo.ae")
    if void is not None:  # formal Holm win, void per the BEAT-ENERGY report: downgraded with the quote
        assert void["verdict"] == "ne_bat_pas" and "voided_by_report" in void["caveats"]
    fair = [r for r in doc["rows"] if r["track"] == "fix7_fair" and r["evaluation"] == "development"]
    assert fair and all(r["verdict"] == "ne_bat_pas" for r in fair)


def test_page_bundle_matrix_and_calendar(doc: dict[str, Any]) -> None:
    p = S.page(doc, sealed={"forecast.bm_stack": 7}, root=REPO)
    assert p["headline"] == REG.headline(doc["rows"])
    statuses = {c["status"] for d in p["matrix"] for t in d["targets"] for c in t["cells"].values()}
    assert "no_track" in statuses
    assert {"bat", "ne_bat_pas"} <= statuses
    dates = [g["sort"] for g in p["calendar"]]
    assert dates == sorted(dates)
    macro = [g for g in p["calendar"] if g["track"] == "beat_macro"]
    assert macro and macro[0]["sealed"] is True


def test_track_detail_and_doc_whitelist(doc: dict[str, Any]) -> None:
    d = S.track_detail(doc, "beat_health", root=REPO)
    assert d is not None and d["rows"]
    assert any(c["kind"] == "coverage" for c in d["charts"])
    assert S.track_detail(doc, "nope", root=REPO) is None
    assert S.report_text(doc, "docs/dev/v7_beat_health.md", root=REPO) is not None
    assert S.report_text(doc, "pyproject.toml", root=REPO) is None
    assert S.report_text(doc, "../etc/passwd", root=REPO) is None
