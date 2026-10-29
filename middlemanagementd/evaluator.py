"""Jargon evaluator: LLM scores blended with deterministic scores, falling back to rules alone."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from .config import EvaluationConfig
from .llm import LLMClient, LLMError, parse_json_object
from .logs import event
from .personalities import Personality
from .prompts import eval_messages
from .scoring import METRICS, JargonDictionary, clamp, load_dictionary, score_text, weighted_overall

log = logging.getLogger(__name__)


@dataclass
class Evaluation:
    scores: dict[str, float]
    overall: float
    threshold: float
    passed: bool
    detected_jargon: list[str]
    critique: str
    evaluator: str  # "llm+rules" | "rules" | "silence"
    word_count: int
    jargon_ratio: float
    llm_error: str | None = None
    rule_scores: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class JargonEvaluator:
    def __init__(self, cfg: EvaluationConfig, llm: LLMClient | None, max_tokens: int = 220,
                 temperature: float = 0.2, dictionary: JargonDictionary | None = None) -> None:
        self.cfg = cfg
        self.llm = llm
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.dictionary = dictionary or load_dictionary(cfg.jargon_dictionary)

    def threshold(self, persona: Personality) -> float:
        return self.cfg.pass_thresholds[self.cfg.difficulty] + persona.threshold_offset

    def weights(self, persona: Personality) -> dict[str, float]:
        return {m: self.cfg.weights.get(m, 0.0) * persona.weight_modifiers.get(m, 1.0) for m in METRICS}

    async def evaluate(self, question: str, transcript: str, persona: Personality) -> Evaluation:
        threshold = self.threshold(persona)
        rules = score_text(transcript, self.dictionary)
        if rules.word_count == 0:
            return Evaluation(
                scores={m: 0.0 for m in METRICS}, overall=0.0, threshold=threshold, passed=False,
                detected_jargon=[], critique="The employee said nothing at all.", evaluator="silence",
                word_count=0, jargon_ratio=0.0, rule_scores=rules.scores,
            )

        scores = dict(rules.scores)
        detected = list(dict.fromkeys(rules.detected))
        critique = _rules_critique(rules.density, rules.word_count, scores)
        evaluator = "rules"
        llm_error: str | None = None

        if self.llm is not None and self.llm.enabled and self.cfg.llm_weight > 0:
            try:
                llm_scores, llm_jargon, llm_critique = await self._llm_scores(question, transcript, persona)
                w = self.cfg.llm_weight
                scores = {m: round(w * llm_scores.get(m, rules.scores[m]) + (1 - w) * rules.scores[m], 1)
                          for m in METRICS}
                detected = list(dict.fromkeys(llm_jargon + detected))
                critique = llm_critique or critique
                evaluator = "llm+rules"
            except (LLMError, ValueError) as e:
                llm_error = str(e)[:200]
                event(log, "llm evaluation unavailable, using deterministic scoring", logging.WARNING,
                      error=llm_error)

        overall = weighted_overall(scores, self.weights(persona))
        return Evaluation(
            scores=scores, overall=overall, threshold=threshold, passed=overall >= threshold,
            detected_jargon=detected[:12], critique=critique, evaluator=evaluator,
            word_count=rules.word_count, jargon_ratio=rules.density, llm_error=llm_error,
            rule_scores=rules.scores,
        )

    async def _llm_scores(self, question: str, transcript: str,
                          persona: Personality) -> tuple[dict[str, float], list[str], str]:
        assert self.llm is not None
        raw = await self.llm.chat(eval_messages(question, transcript, persona),
                                  max_tokens=self.max_tokens, temperature=self.temperature, json_mode=True)
        return parse_llm_evaluation(raw)


def parse_llm_evaluation(raw: str) -> tuple[dict[str, float], list[str], str]:
    obj = parse_json_object(raw)
    src = obj.get("scores", obj)
    if not isinstance(src, dict):
        raise ValueError("scores is not an object")
    scores: dict[str, float] = {}
    for m in METRICS:
        v = src.get(m)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            scores[m] = clamp(float(v))
    if len(scores) < len(METRICS) // 2:
        raise ValueError(f"LLM returned too few scores: {sorted(scores)}")
    jargon = obj.get("detected_jargon") or []
    jargon = [str(j).strip().lower() for j in jargon if str(j).strip()][:8] if isinstance(jargon, list) else []
    critique = str(obj.get("critique") or "").strip()[:300]
    return scores, jargon, critique


def _rules_critique(density: float, words: int, scores: dict[str, float]) -> str:
    if words < 8:
        return "A curt reply with almost no corporate framing."
    if scores["question_evasion"] < 40:
        return "Contained alarming amounts of concrete, checkable information."
    if density >= 0.25:
        return "Dense, fluent corporate idiom with very little substance."
    if density >= 0.12:
        return "Some solid jargon, diluted by plain English."
    return "Mostly plain language, minimal corporate framing."
