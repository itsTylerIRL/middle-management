from middlemanagementd.scoring import METRICS, load_dictionary, score_text, terms_used, tokenize, weighted_overall

from .conftest import JARGON, PLAIN


def test_tokenize_handles_hyphens_and_apostrophes():
    assert tokenize("We're cross-functional, OK?") == ["we're", "cross-functional", "ok"]


def test_jargon_beats_useful_update():
    j, p = score_text(JARGON), score_text(PLAIN)
    weights = {m: 1.0 for m in METRICS}
    assert weighted_overall(j.scores, weights) > 60
    assert weighted_overall(p.scores, weights) < 25
    assert j.density > 0.3 > p.density


def test_concrete_information_is_penalised_as_poor_evasion():
    evasive = score_text("At the end of the day it's a journey, and we're exploring options going forward in this space.")
    concrete = score_text("I fixed 3 bugs yesterday, merged the commit, and deployed the server at 4pm because it crashed.")
    assert evasive.scores["question_evasion"] > concrete.scores["question_evasion"] + 40


def test_hedging_lowers_confidence():
    confident = score_text("Absolutely. I'm confident we will deliver, and I own the outcome, without question.")
    hedgy = score_text("Um, I think maybe we sort of delivered, I'm not sure, sorry, uh, probably.")
    assert confident.scores["executive_confidence"] > hedgy.scores["executive_confidence"] + 40


def test_longest_phrase_wins_without_double_counting():
    r = score_text("our north star metric")
    assert r.detected == ["north star metric"]
    assert r.jargon_words == 3


def test_plural_match():
    assert score_text("two tiger teams").detected == ["tiger team"]
    assert score_text("multiple deliverables and milestones").category_counts["action_theater"] == 2


def test_empty_text_scores_zero():
    r = score_text("   ")
    assert r.word_count == 0 and all(v == 0 for v in r.scores.values())


def test_scores_are_bounded():
    r = score_text(" ".join(["synergy leverage stakeholders roadmap absolutely"] * 50))
    assert all(0 <= v <= 100 for v in r.scores.values())


def test_weighted_overall_respects_weights():
    scores = {m: 0.0 for m in METRICS} | {"jargon_density": 100.0}
    assert weighted_overall(scores, {m: 0.0 for m in METRICS} | {"jargon_density": 1.0}) == 100.0
    assert weighted_overall(scores, {m: 1.0 for m in METRICS}) == 12.5
    assert weighted_overall(scores, {m: 0.0 for m in METRICS}) == 0.0


def test_terms_used():
    assert terms_used("We will leverage the north star", ["north star", "synergy", "leverage"]) == ["north star", "leverage"]


def test_bundled_dictionary_has_all_categories():
    d = load_dictionary()
    for cat in ("buzzwords", "strategy", "stakeholders", "action_theater", "confidence", "evasion", "hedges", "concrete"):
        assert d.categories[cat], cat
