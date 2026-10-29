import json

from middlemanagementd.evaluator import JargonEvaluator, parse_llm_evaluation
from middlemanagementd.llm import LLMError
from middlemanagementd.personalities import PERSONALITIES
from middlemanagementd.scoring import METRICS

from .conftest import JARGON, PLAIN, FakeLLM

PERSONA = PERSONALITIES["passive_aggressive"]


def llm_json(score: int) -> str:
    return json.dumps({"scores": {m: score for m in METRICS}, "detected_jargon": ["Synergy"], "critique": "ok"})


async def test_falls_back_to_rules_when_llm_down(cfg):
    ev = await JargonEvaluator(cfg.evaluation, FakeLLM(LLMError("down"))).evaluate("q", JARGON, PERSONA)
    assert ev.evaluator == "rules" and ev.llm_error and ev.passed


async def test_blends_llm_and_rules(cfg):
    cfg.evaluation.llm_weight = 0.5
    ev = await JargonEvaluator(cfg.evaluation, FakeLLM(llm_json(0))).evaluate("q", JARGON, PERSONA)
    assert ev.evaluator == "llm+rules"
    for m in METRICS:
        assert ev.scores[m] == round(0.5 * ev.rule_scores[m], 1)
    assert "synergy" in ev.detected_jargon


async def test_llm_can_fail_a_jargon_answer(cfg):
    cfg.evaluation.llm_weight = 1.0
    ev = await JargonEvaluator(cfg.evaluation, FakeLLM(llm_json(5))).evaluate("q", JARGON, PERSONA)
    assert not ev.passed and ev.overall == 5.0


async def test_garbage_llm_output_falls_back(cfg):
    ev = await JargonEvaluator(cfg.evaluation, FakeLLM("I refuse to output JSON")).evaluate("q", PLAIN, PERSONA)
    assert ev.evaluator == "rules" and not ev.passed


async def test_silence_never_calls_llm(cfg):
    llm = FakeLLM(llm_json(100))
    ev = await JargonEvaluator(cfg.evaluation, llm).evaluate("q", "", PERSONA)
    assert ev.evaluator == "silence" and not ev.passed and not llm.chat_calls


async def test_difficulty_and_personality_shift_threshold(cfg):
    e = JargonEvaluator(cfg.evaluation, None)
    cfg.evaluation.difficulty = "executive"
    assert e.threshold(PERSONALITIES["chief_synergy"]) == 78 + PERSONALITIES["chief_synergy"].threshold_offset


def test_parse_tolerates_fences_and_partial_scores():
    raw = "```json\n" + json.dumps({"scores": {"jargon_density": 150, "buzzword_diversity": 50,
                                               "executive_confidence": 50, "question_evasion": 10}}) + "\n```"
    scores, jargon, critique = parse_llm_evaluation(raw)
    assert scores["jargon_density"] == 100 and jargon == [] and critique == ""


def test_parse_rejects_too_few_scores():
    import pytest

    with pytest.raises(ValueError):
        parse_llm_evaluation('{"scores": {"jargon_density": 50}}')
