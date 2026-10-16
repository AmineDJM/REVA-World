"""CAUSCAND: wikitext rendering, connective rules, argument linking rules, flags, linking (offline).

Sentences here are written for the tests (not Wikipedia text). Recorded API excerpts are tested in
``test_causal_candidates_fetch.py``.
"""

from __future__ import annotations

import pytest

from reva.causal_candidates import extract as X
from reva.causal_candidates import patterns as P
from reva.causal_candidates.wikitext import paragraphs, render_template, sentences


def _cands(wikitext: str, lang: str = "en") -> tuple[list[X.RawCandidate], list[dict]]:
    recs = X.sentence_records(
        lang=lang,
        title="T",
        page_id=1,
        article_qid="Q1",
        revision_id=7,
        revision_timestamp="2026-10-16T00:00:00Z",
        wikitext=wikitext,
    )
    return X.extract(recs)


def _pairs(wikitext: str, lang: str = "en", keep_modifiers: bool = False) -> list[tuple[str, str]]:
    cands, _ = _cands(wikitext, lang)
    return [
        (c.cause.target, c.effect.target)
        for c in cands
        if keep_modifiers or not (c.cause.modifier_head or c.effect.modifier_head)
    ]


# ----------------------------------------------------------------------------- wikitext


def test_render_keeps_links_offsets_and_verbatim_slice() -> None:
    wt = (
        "{{Infobox event|name=X}}\nThe '''[[Alpha Treaty|treaty]]''' of [[Beta]] in [[Gamma]] on {{date|1 May 1900}} "
        "triggered the [[Delta Crisis]].<ref>{{cite book|title=Z}}</ref> It was [[Epsilon]]s fault.\n"
    )
    [p] = paragraphs(wt)
    assert p.text.startswith("The treaty of Beta in Gamma on 1 May 1900 triggered the Delta Crisis.")
    assert [(lk.target, lk.anchor) for lk in p.links][:2] == [("Alpha Treaty", "treaty"), ("Beta", "Beta")]
    assert p.links[-1].anchor == "Epsilons"  # link trail
    a, b = sentences(p.text, "en")[0]
    s0, s1 = p.src_span(a, b)
    assert wt[s0:s1] == (
        "The '''[[Alpha Treaty|treaty]]''' of [[Beta]] in [[Gamma]] on {{date|1 May 1900}} "
        "triggered the [[Delta Crisis]]."
    )
    for lk in p.links:
        assert p.text[lk.start : lk.end] == lk.anchor


def test_render_drops_files_tables_categories_and_reference_sections() -> None:
    wt = (
        "Intro [[A]] text.\n\n== Background ==\n[[File:X.jpg|thumb|caption with [[Link]]]]\nBody [[B]].\n"
        '{| class="wikitable"\n| [[C]] || x\n|}\n[[Category:Wars]]\n== References ==\n[[D]] led to [[E]].\n'
    )
    ps = paragraphs(wt)
    assert [p.section for p in ps] == ["", "Background"]
    assert {lk.target for p in ps for lk in p.links} == {"A", "B"}


def test_templates_rendered_or_counted() -> None:
    assert render_template("date|28|juin|1914") == "28 juin 1914"
    assert render_template("s|XIX") == "XIXe siècle"
    assert render_template("lang|fr|Grande Guerre") == "Grande Guerre"
    assert render_template("cite web|url=x") is None
    recs = X.sentence_records(
        lang="en",
        title="T",
        page_id=1,
        article_qid=None,
        revision_id=1,
        revision_timestamp="",
        wikitext="The [[A]] {{unknown template|x}} led to [[B]].",
    )
    assert recs[0].templates_dropped == 1


def test_sentence_split_keeps_abbreviations_and_initials() -> None:
    t = "The U.S. Army under Gen. John F. Kennedy arrived. It left in 1945. Next one."
    assert [t[a:b] for a, b in sentences(t, "en")] == [
        "The U.S. Army under Gen. John F. Kennedy arrived.",
        "It left in 1945.",
        "Next one.",
    ]
    f = "En 44 av. J.-C. César meurt. M. Dupont arrive."
    assert len(sentences(f, "fr")) == 2


# ----------------------------------------------------------------------------- directions


@pytest.mark.parametrize(
    ("sentence", "expected", "connective"),
    [
        ("The [[A]] caused the [[B]].", ("A", "B"), "en.caused"),
        ("The [[B]] was caused by the [[A]].", ("A", "B"), "en.caused_by"),
        ("The [[A]] led to the [[B]].", ("A", "B"), "en.led_to"),
        ("The [[A]] resulted in the [[B]].", ("A", "B"), "en.resulted_in"),
        ("The [[B]] resulted from the [[A]].", ("A", "B"), "en.resulted_from"),
        ("The [[A]] triggered the [[B]].", ("A", "B"), "en.triggered"),
        ("The [[B]] was triggered by the [[A]].", ("A", "B"), "en.triggered_by"),
        ("The [[B]] was due to the [[A]].", ("A", "B"), "en.due_to"),
        ("The [[B]] happened because of the [[A]].", None, "en.because_of"),  # verb between: effect not linked
        ("[[B]] because of the [[A]].", ("A", "B"), "en.because_of"),
        ("The [[B]] occurred as a result of the [[A]].", None, "en.as_a_result_of"),
        ("The [[B]] was largely a result of [[A]].", None, None),
        ("The [[A]] sparked the [[B]].", ("A", "B"), "en.sparked"),
        ("The [[A]] precipitated the [[B]].", ("A", "B"), "en.precipitated"),
        ("The [[A]] contributed greatly to the [[B]].", ("A", "B"), "en.contributed_to"),
        ("Due to the [[A]], [[B]] rose.", ("A", "B"), "en.due_to"),
    ],
)
def test_english_directions(sentence: str, expected: tuple[str, str] | None, connective: str | None) -> None:
    cands, _ = _cands(sentence)
    got = [(c.cause.target, c.effect.target, c.connective_id) for c in cands]
    if expected is None:
        assert got == []
    else:
        assert got == [(*expected, connective)]


@pytest.mark.parametrize(
    ("sentence", "expected", "connective"),
    [
        ("La [[A]] a provoqué la [[B]].", ("A", "B"), "fr.a_provoque"),
        ("La [[A]] provoqua la [[B]].", ("A", "B"), "fr.a_provoque"),
        ("La [[B]] a été provoquée par la [[A]].", ("A", "B"), "fr.provoque_par"),
        ("La [[A]] a entraîné la [[B]].", ("A", "B"), "fr.a_entraine"),
        ("La [[B]] est due à la [[A]].", ("A", "B"), "fr.est_due_a"),
        ("La [[B]] à cause de la [[A]].", ("A", "B"), "fr.a_cause_de"),
        ("La [[A]] a déclenché la [[B]].", ("A", "B"), "fr.a_declenche"),
        ("La [[A]] a contribué à la [[B]].", ("A", "B"), "fr.a_contribue"),
        ("En raison de la [[A]], le [[B]] augmenta.", ("A", "B"), "fr.en_raison_de"),
        ("La [[B]] est la conséquence directe de la [[A]].", ("A", "B"), "fr.consequence_de"),
        ("La [[B]] a été causée par la [[A]].", ("A", "B"), "fr.cause_par"),
        ("La guerre se déclenche en [[A]].", None, None),
    ],
)
def test_french_directions(sentence: str, expected: tuple[str, str] | None, connective: str | None) -> None:
    cands, _ = _cands(sentence, "fr")
    got = [(c.cause.target, c.effect.target, c.connective_id) for c in cands]
    assert got == ([] if expected is None else [(*expected, connective)])


def test_every_listed_connective_family_has_a_rule() -> None:
    listed = {
        "caused",
        "led to",
        "resulted in",
        "triggered",
        "due to",
        "because of",
        "as a result of",
        "sparked",
        "precipitated",
        "contributed to",
        "a provoqué",
        "a entraîné",
        "est due à",
        "à cause de",
        "a déclenché",
        "a contribué à",
        "en raison de",
        "conséquence de",
    }
    assert listed <= {c.family for c in P.CONNECTIVES if c.listed}
    assert {c.relation_hint for c in P.CONNECTIVES} <= {"has_cause", "precipitated", "contributing_factor"}


# ----------------------------------------------------------------------------- argument rules


def test_prepositional_chain_takes_the_head_link() -> None:
    assert _pairs("The [[A]] of [[Duke]] in [[City]] triggered the [[B]].") == [("A", "B")]


def test_coordination_yields_each_pair() -> None:
    got = _pairs("[[A1]] and [[A2]] led to [[B1]] and [[B2]].")
    assert sorted(got) == [("A1", "B1"), ("A1", "B2"), ("A2", "B1"), ("A2", "B2")]


def test_relative_bridge_and_anaphora() -> None:
    assert _pairs("The [[A]], which led to the [[B]], ended.") == [("A", "B")]
    assert _pairs("The [[X]] ended, and this led to the [[B]].") == []


def test_left_argument_must_be_clause_subject() -> None:
    # "[[X]] invaded [[A]], which led to": the object of another verb is not the cause
    assert _pairs("[[X]] invaded [[A]], which led to [[B]].") == []
    # an introductory phrase with its own link is allowed
    assert _pairs("During the [[War]], the [[A]] led to the [[B]].") == [("A", "B")]
    # odd comma count between link and connective: the link is in another clause
    assert _pairs("They signed the [[A]] in 1936, meeting little opposition due to [[B]].") == []


def test_right_argument_must_follow_the_connective_closely() -> None:
    assert _pairs("The [[A]] led to a long period, which saw the [[B]].") == []
    assert _pairs("The [[A]] caused the King to sign the [[B]].") == []


def test_modifier_heads_are_detected_and_dropped_by_default() -> None:
    cands, _ = _cands("The collapse of the [[A]] led to the independence of [[B]].")
    [c] = cands
    assert (c.cause.modifier_head, c.effect.modifier_head) == ("collapse", "independence")
    assert _pairs("The [[A]] led to the outbreak of [[B]].") == [("A", "B")]  # transparent head
    assert _pairs("The [[A]] led to [[B]]'s defeat.") == []  # possessive: B only modifies the effect
    assert _pairs("The [[A]] led to [[B]] shortages.") == []  # compound modifier


def test_negation_and_hedge_flags_keep_direction() -> None:
    [c] = _cands("The [[A]] did not cause the [[B]].")[0]
    assert (c.cause.target, c.effect.target, c.flags["negated"]) == ("A", "B", True)
    [c] = _cands("Historians debate whether the [[A]] caused the [[B]].")[0]
    assert c.flags["hedged"] and not c.flags["negated"]
    [c] = _cands("La [[A]] aurait provoqué la [[B]].", "fr")[0]
    assert c.flags["hedged"]
    [c] = _cands("La [[A]] n'a pas provoqué la [[B]].", "fr")[0]
    assert c.flags["negated"]
    [c] = _cands("The [[A]] not only caused the [[B]].")[0]
    assert not c.flags["negated"]


def test_date_links_are_never_arguments() -> None:
    assert X.is_date_target("1914") and X.is_date_target("28 juin") and X.is_date_target("1914 en France")
    assert X.is_date_target("XIXe siècle") and X.is_date_target("June 28")
    assert not X.is_date_target("1929 Wall Street crash")
    assert _pairs("The [[A]] led to [[1914]].") == []


# ----------------------------------------------------------------------------- linking


def _res(target: str, qid: str | None, **kw: object) -> X.Resolution:
    return X.Resolution(target=target, title=kw.pop("title", target), qid=qid, **kw)  # type: ignore[arg-type]


def test_link_candidates_resolves_only_through_the_wiki_and_drops_bad_targets() -> None:
    cands, _ = _cands(
        "The [[Ax]] led to the [[Bx]]. The [[Cx]] led to the [[Dx]]. The [[Ex]] led to the [[Fx]]. "
        "The [[Gx]] led to the [[Hx]]. The [[Ix]] led to the [[Jx]]."
    )
    res = {
        "en": {
            "Ax": _res("Ax", "Q10"),
            "Bx": _res("Bx", "Q20", redirected=True, title="Bee"),
            "Cx": _res("Cx", "Q30"),
            "Dx": _res("Dx", None, title=None),  # missing page
            "Ex": _res("Ex", "Q50"),
            "Fx": _res("Fx", "Q60", disambiguation=True),
            "Gx": _res("Gx", "Q70"),
            "Hx": _res("Hx", "Q70"),  # self-loop after redirects
            "Ix": _res("Ix", "Q90"),  # J never resolved
        }
    }
    docs, dropped = X.link_candidates(cands, res, linked_at="t")
    assert [(d["cause_qid"], d["effect_qid"]) for d in docs] == [("Q10", "Q20")]
    assert dropped == {"missing_page": 1, "disambiguation_page": 1, "self_loop": 1, "unresolved": 1}
    d = docs[0]
    assert d["tier"] == "T5" and d["promotion_status"] == "not_promoted" and d["review_status"] == "unreviewed"
    assert d["source"]["revision_id"] == 7 and d["source"]["url"].endswith("oldid=7")
    assert d["rights"]["licence"] == "CC BY-SA 4.0" and "revision 7" in d["rights"]["attribution"]
    assert d["effect"]["resolved_title"] == "Bee" and d["effect"]["redirected"]
    assert d["sentence"] == "The Ax led to the Bx." and d["sentence_wikitext"] == "The [[Ax]] led to the [[Bx]]."
    assert d["negation_hedge_flag"] == "none" and d["direction_rule"] == "en.led_to:cause_left"
    assert d["extractor"] == {"id": X.EXTRACTOR_ID, "version": X.EXTRACTOR_VERSION}


def test_candidate_ids_are_stable_and_versioned() -> None:
    cands, _ = _cands("The [[A]] led to the [[B]].")
    res = {"en": {"A": _res("A", "Q1"), "B": _res("B", "Q2")}}
    a, _ = X.link_candidates(cands, res)
    b, _ = X.link_candidates(cands, res)
    assert a[0]["candidate_id"] == b[0]["candidate_id"] and len(a[0]["candidate_id"]) == 32


def test_anchor_target_hint() -> None:
    assert X.anchor_matches_target("depression", "Great Depression")
    assert not X.anchor_matches_target("raids by allied tribes", "Northern theater of the war")


def test_coordinated_arguments_share_the_modifier_status() -> None:
    # "between the [[A]] and the [[B]]": both only modify "trouble"
    assert _pairs("The [[H]] resulted from the trouble between the [[Aa]] and the [[Bb]] during the crisis.") == []
    # a participle after the subject's link is not a compound noun
    assert _pairs("A [[Ceasefire]] brokered by mediators resulted in the [[Commission]].") == [
        ("Ceasefire", "Commission")
    ]


def test_literal_uses_without_links_on_both_sides_yield_nothing() -> None:
    cands, misses = _cands("Two cables were cut off [[Beirut]], one of which led to Alexandria.")
    assert cands == [] and misses


# ----------------------------------------------------------------------------- hand-labelled evaluation


def _eval() -> dict:
    import json
    from pathlib import Path

    return json.loads((Path(__file__).resolve().parents[1] / "fixtures/causal_candidates/eval_v1.1.0.json").read_text())


def test_hand_labelled_metrics_match_the_published_notes() -> None:
    items = _eval()["items"]

    def tally(name: str) -> tuple[int, ...]:
        it = [x for x in items if x["set"] == name]
        return (
            len(it),
            *(sum(x["labels"][m] for x in it) for m in ("direction", "cause_link", "effect_link", "correct")),
        )

    assert tally("strict_census") == (21, 21, 19, 19, 18)  # docs/dev/v3_causcand.md section 5
    assert tally("lenient_sample50") == (50, 50, 31, 22, 15)


def test_labelled_correct_candidates_are_still_extracted() -> None:
    """Regression: every candidate judged correct in the strict census is still produced, same pair, same rule."""
    for x in _eval()["items"]:
        if x["set"] != "strict_census" or not x["labels"]["correct"]:
            continue
        cands, _ = X.extract([X.record_from_dict(x["record"])])
        got = {
            (c.connective_id, c.cause.target, c.effect.target)
            for c in cands
            if not (c.cause.modifier_head or c.effect.modifier_head)
        }
        assert (x["connective"], x["cause_target"], x["effect_target"]) in got, x["record"]["sentence"][:120]
