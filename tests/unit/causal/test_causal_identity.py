"""Event identity (evidenced same_as clusters) in corroboration: reva.causal.identity + reva.causal.grader.compute
(agent SAMEAS, docs/dev/v3_sameas.md). Offline, pure."""

from __future__ import annotations

from typing import Any

from reva.causal import grading as gr
from reva.causal import identity as idn
from reva.causal.grader import compute
from reva.causal.temporal import EventDates, window_of_time

RULES = gr.load_rules()


def _claim(i: int, cause: str, effect: str, *, src: str, tier: str = "T1", **kw: Any) -> dict[str, Any]:
    return {
        "id": i,
        "claim_key": f"k{i}",
        "cause_id": cause,
        "effect_id": effect,
        "family": "cause",
        "stance": "asserts",
        "source_key": src,
        "source_tier": tier,
        "upstream_keys": [kw.get("up", f"dataset:{src}/d")],
        "contests": [],
        "disputed_by": [],
        "content_sha256": f"c{i}",
    }


def _dates(**years: int) -> dict[str, EventDates]:
    return {q: EventDates(window_of_time(f"{y:+05d}", 9, "P580"), None) for q, y in years.items()}


def _link(a: str, b: str, rule: str, independent: bool | None) -> idn.IdentityLink:
    return idn.IdentityLink(a, b, f"reva.events.same_as:{rule}:{a}={b}", rule, independent, {"type": "x"})


def test_rules_resolve_same_as() -> None:
    assert RULES.resolve_same_as is True
    assert "corroboration.via_same_as" in RULES.facts and "corroboration.via_same_as" in gr.NOTE_FACTS


def test_clusters_union_find_and_cap() -> None:
    ident = idn.build([_link("b", "a", "r", True), _link("c", "b", "r", True), _link("x", "y", "r", True)])
    assert ident.canon("c") == "a" and ident.members("b") == ["a", "b", "c"] and ident.canon("zz") == "zz"
    assert ident.summary()["cluster_sizes"] == {2: 1, 3: 1}
    big = idn.build([_link(f"n{i}", f"n{i + 1}", "r", True) for i in range(20)], max_cluster=5)
    assert big.canon("n3") == "n3" and len(big.oversized) == 1  # oversized: left unresolved, reported


def test_independent_sources_corroborate_through_same_as() -> None:
    # UCDP: conflict -> its agreement record; PA-X: same conflict -> its own record of the same agreement
    ident = idn.build([_link("pax:agt:1", "ucdp:pa:9", "pax_ucdp_agreement_id", True)])
    claims = [
        _claim(1, "ucdp:conflict:5", "ucdp:pa:9", src="ucdp.downloads"),
        _claim(2, "ucdp:conflict:5", "pax:agt:1", src="pax.peaceagreements"),
    ]
    dates = _dates(**{"ucdp:conflict:5": 1990, "ucdp:pa:9": 1995, "pax:agt:1": 1995})
    out = {c.claim_key: c for c in compute(claims, dates, RULES, ident)}
    for k in ("k1", "k2"):
        assert out[k].corroboration.count == 2 and out[k].grade.grade == "A"
        assert "corroboration.via_same_as" in out[k].grade.rule_ids
    assert out["k1"].corroborating == ["k2"]
    # without identity resolution: two separate links, single source each -> B
    plain = {c.claim_key: c for c in compute(claims, dates, RULES)}
    assert plain["k1"].grade.grade == plain["k2"].grade.grade == "B"
    assert plain["k1"].inputs_sha256 != out["k1"].inputs_sha256  # identity changes the inputs -> new grade rows


def test_derived_records_count_once() -> None:
    # EUSANCT took the case over from TIES: a link declared not independent -> shared lineage upstream
    ident = idn.build([_link("eusanct:case:1", "ties:case:1", "eusanct_ties_case", False)])
    claims = [
        _claim(1, "ties:case:1", "x:outcome", src="ties.unc"),
        _claim(2, "eusanct:case:1", "x:outcome", src="eusanct.kondata"),
    ]
    out = {c.claim_key: c for c in compute(claims, _dates(**{"ties:case:1": 1990, "x:outcome": 1992}), RULES, ident)}
    assert out["k1"].corroboration.count == 1 and out["k1"].grade.grade == "B"
    assert any(u.startswith("lineage:") for g in out["k1"].corroboration.groups for u in g)
    assert "corroboration.via_same_as" in out["k1"].grade.rule_ids  # identity used, but counted once


def test_same_source_twice_is_not_corroboration() -> None:
    ident = idn.build([_link("a:1", "a:2", "r", True)])
    claims = [_claim(1, "a:1", "e", src="a"), _claim(2, "a:2", "e", src="a")]
    out = {c.claim_key: c for c in compute(claims, _dates(**{"a:1": 1900, "e": 1901}), RULES, ident)}
    assert out["k1"].corroboration.count == 1  # same dataset upstream key


def test_fixture_rules_without_identity_are_unchanged() -> None:
    claims = [_claim(1, "a", "b", src="s1"), _claim(2, "a", "b", src="s2")]
    out = compute(claims, _dates(a=1900, b=1901), RULES, idn.Identity())
    assert all(c.grade.grade == "A" and "corroboration.via_same_as" not in c.grade.rule_ids for c in out)
