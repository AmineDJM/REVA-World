"""Online combination (V7 BEAT-ENSEMBLE): weights from resolved losses, shrinkage, equal weights when short."""

from __future__ import annotations

import pytest

from reva.models.forecasting import online_combination as OC
from reva.models.forecasting.registry_baselines import Predictive


def rows(n: int, losses: dict[str, float]) -> list[OC.LossRow]:
    return [OC.LossRow(f"q{i:03d}", m, v * (1 + i % 3)) for i in range(n) for m, v in losses.items()]


def test_relative_losses_are_scale_free_and_drop_uninformative_questions() -> None:
    rs = [
        OC.LossRow("a", "x", 1.0), OC.LossRow("a", "y", 3.0), OC.LossRow("a", "z", 2.0),
        OC.LossRow("b", "x", 100.0), OC.LossRow("b", "y", 300.0), OC.LossRow("b", "z", 200.0),
        OC.LossRow("c", "x", 1.0),  # one component only: no relative information
        OC.LossRow("d", "x", 0.0), OC.LossRow("d", "y", 0.0),  # every loss 0
    ]  # fmt: skip
    rel, n, dropped = OC.relative_losses(rs, cap=5.0)
    assert n == 2 and dropped == ["c", "d"]
    assert rel["x"] == [0.5, 0.5] and rel["y"] == [1.5, 1.5] and rel["z"] == [1.0, 1.0]
    three = [OC.LossRow("a", "x", 1.0), OC.LossRow("a", "y", 1.0), OC.LossRow("a", "z", 1000.0)]
    rel2, _, _ = OC.relative_losses(three, cap=5.0)
    assert rel2["z"] == [5.0]  # clipped


def test_equal_weights_until_enough_resolutions_and_labelled() -> None:
    rule = OC.WeightRule(min_resolved=20)
    w = OC.online_weights(["b", "a"], rows(19, {"a": 1.0, "b": 2.0}), rule)
    assert w["status"] == OC.EQUAL and w["weights"] == {"a": 0.5, "b": 0.5} and w["n_resolved_questions"] == 19
    assert OC.online_weights(["a"], [], rule)["status"] == OC.SINGLE
    assert OC.online_weights([], [], rule)["status"] == "no_component"


def test_online_weights_favour_the_lower_loss_and_shrink_new_components() -> None:
    rule = OC.WeightRule(min_resolved=20)
    hist = rows(60, {"a": 1.0, "b": 2.0})
    w = OC.online_weights(["a", "b", "new"], hist, rule)
    assert w["status"] == OC.ONLINE and w["eta"] is not None
    assert w["weights"]["a"] > w["weights"]["new"] > w["weights"]["b"]
    assert sum(w["weights"].values()) == pytest.approx(1.0)
    assert w["per_component"]["new"] == {"n": 0, "mean_relative_loss": None, "shrunk_relative_loss": 1.0}
    assert OC.online_weights(["a", "b", "new"], list(reversed(hist)), rule) == w  # order-free, deterministic


def test_combination_rules() -> None:
    preds = {
        "n": Predictive("normal", 1.0, mu=1.0, sigma=1.0),
        "e": Predictive("empirical", 5.0, sample=(3.0, 4.0, 5.0, 6.0, 7.0)),
    }
    w = OC.restrict({"n": 0.25, "e": 0.25, "absent": 0.5}, ["n", "e"])
    assert w == {"e": 0.5, "n": 0.5}
    loc = OC.combine_location({"n": 1.0, "e": 5.0}, w)
    assert loc == 3.0
    qs = OC.combine_quantiles(loc, preds, w, OC.SAMPLE_LEVELS)
    assert qs == sorted(qs) and len(qs) == 50
    med = OC.combine_quantiles(loc, preds, w, (0.5,))[0]
    assert med == pytest.approx(loc)  # the combined median is the combined point
    assert OC.combine_probability({"x": 0.2, "y": 0.8}, {"x": 0.75, "y": 0.25}) == pytest.approx(0.35)
    assert OC.restrict({}, ["a", "b"]) == {"a": 0.5, "b": 0.5}


def test_one_weight_per_information_source_split_equally_inside() -> None:
    """Method 2 (reva.ensemble@2): forecasters built around the same official point share one source weight."""
    weo = {"weo": "imf.weo.point", "conformal": "imf.weo.point", "bias": "imf.weo.point", "fan": "imf.weo.point"}
    comps = ["weo", "conformal", "bias", "fan", "ar", "persistence"]
    rule = OC.WeightRule(min_resolved=20, by_source=True)
    assert rule.doc()["method"] == OC.METHOD_BY_SOURCE and "by_source" not in rule.doc()
    eq = OC.grouped_weights(comps, weo, [], rule)
    assert eq["status"] == OC.EQUAL  # equal-weights mode applies per source
    assert eq["source_weights"] == pytest.approx({"imf.weo.point": 1 / 3, "ar": 1 / 3, "persistence": 1 / 3})
    assert eq["weights"]["weo"] == pytest.approx(1 / 12) and eq["weights"]["ar"] == pytest.approx(1 / 3)
    # one question where the WEO point is absent (e.g. the shape role): the group's weight goes to its present members
    one = OC.restrict_grouped(eq["source_weights"], eq["sources"], ["conformal", "bias", "fan", "ar"])
    assert one["ar"] == pytest.approx(0.5) and one["fan"] == pytest.approx(1 / 6)
    assert sum(one.values()) == pytest.approx(1.0)
    # per-source losses = mean of the members' losses; online weights then favour the better source
    hist = rows(60, {"weo": 1.0, "conformal": 1.0, "bias": 1.0, "fan": 1.0, "ar": 3.0, "persistence": 3.0})
    on = OC.grouped_weights(comps, weo, hist, rule)
    assert on["status"] == OC.ONLINE and on["source_weights"]["imf.weo.point"] > on["source_weights"]["ar"]
    assert on["weights"]["weo"] == pytest.approx(on["source_weights"]["imf.weo.point"] / 4)
    assert OC.source_rows([OC.LossRow("q", "weo", 1.0), OC.LossRow("q", "fan", 3.0)], weo) == [
        OC.LossRow("q", "imf.weo.point", 2.0)
    ]
