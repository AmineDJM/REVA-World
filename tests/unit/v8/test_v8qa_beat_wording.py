"""V8QA: BEAT verdicts in the Dossier say what the win is (h0 timeliness), with the scoreboard's caveats."""

from __future__ import annotations

from typing import Any, cast

from reva.v8.dossier.queries import beat_tests
from reva.v8.dossier.values import Miss


def _text(v: Any) -> str:
    return str(getattr(v, "text", None) or getattr(v, "value", None) or v)


def test_brent_beat_is_current_month_timeliness_with_scoreboard_caveats() -> None:
    out = beat_tests(
        cast(Any, None),
        file="docs/dev/v7_beat_energy_result.json",
        code="BREPUUS",
        benchmark_fr="l'EIA STEO",
        title="t",
    )
    vals = out.values
    assert not isinstance(vals["scope"], Miss)
    assert "mois en cours" in _text(vals["scope"])
    reading = _text(vals["reading"])
    assert "fraîcheur" in reading and "pas une meilleure prévision" in reading
    cav = _text(vals["caveats"])
    assert "Avantage de fraîcheur" in cav  # timeliness_edge, quoted from registry/scoreboard/tracks.yaml
    assert "jamais « REVA bat <officiel> »" in cav  # uses_official_forecast (the STEO combination)


def test_brainstorm_verdict_wording_follows_scoreboard_caveats() -> None:
    from reva.v8.ask.brainstorm import _verdict_wording

    vocab = {"bat": "bat", "ne_bat_pas": "ne bat pas"}
    combo = {"verdict": "bat", "caveats": ["uses_official_forecast", "timeliness_edge"]}
    w = _verdict_wording(combo, vocab)
    assert w.startswith("la combinaison qui utilise la prévision officielle bat l'officiel seul")
    assert "avantage de fraîcheur" in w and "pas une meilleure prévision en général" in w
    assert _verdict_wording({"verdict": "bat", "caveats": []}, vocab) == "bat"
    assert _verdict_wording({"verdict": "ne_bat_pas", "caveats": ["uses_official_forecast"]}, vocab) == "ne bat pas"


def test_every_scoreboard_win_on_an_official_forecast_carries_its_reading() -> None:
    """Each « bat » row against an official forecaster that uses the official forecast or fresher data says so."""
    import yaml

    from reva.v8.dossier.queries import REPO

    reg = yaml.safe_load((REPO / "registry/scoreboard/tracks.yaml").read_text())
    for r in reg["rows"]:
        if r.get("verdict") != "bat" or r.get("benchmark_kind") not in reg["headline_benchmark_kinds"]:
            continue
        if r.get("candidate_kind") in ("combination_with_official", "post_processed_official"):
            assert "uses_official_forecast" in (r.get("caveats") or []), r["id"]
