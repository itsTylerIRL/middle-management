"""Deterministic jargon scoring. Used on its own when the LLM is down, and blended otherwise."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path

import yaml

METRICS = (
    "jargon_density",
    "buzzword_diversity",
    "executive_confidence",
    "strategic_alignment",
    "stakeholder_awareness",
    "actionability_theater",
    "corporate_verbosity",
    "question_evasion",
)

JARGON_CATEGORIES = ("buzzwords", "strategy", "stakeholders", "action_theater", "confidence", "evasion")
MODIFIER_CATEGORIES = ("hedges", "concrete")

_WORD_RE = re.compile(r"[a-z0-9]+(?:['’\-][a-z0-9]+)*")


def tokenize(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower().replace("’", "'"))


def clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


@dataclass(frozen=True)
class JargonDictionary:
    # category -> list of phrases, each phrase pre-tokenized
    categories: dict[str, tuple[tuple[str, ...], ...]]

    @classmethod
    def from_mapping(cls, data: dict[str, list[str]]) -> "JargonDictionary":
        cats: dict[str, tuple[tuple[str, ...], ...]] = {}
        for cat, phrases in data.items():
            toks = {tuple(tokenize(p)) for p in phrases or []}
            cats[cat] = tuple(sorted((t for t in toks if t), key=len, reverse=True))
        return cls(cats)

    def terms(self, category: str) -> list[str]:
        return [" ".join(t) for t in self.categories.get(category, ())]


@lru_cache(maxsize=4)
def load_dictionary(path: str = "") -> JargonDictionary:
    if path:
        text = Path(path).expanduser().read_text()
    else:
        text = resources.files("middlemanagementd.data").joinpath("jargon.yaml").read_text()
    return JargonDictionary.from_mapping(yaml.safe_load(text))


def _match(tokens: list[str], phrases: list[tuple[tuple[str, ...], str]]) -> list[tuple[str, str, int]]:
    """Greedy longest-first, non-overlapping phrase matching. Returns (phrase, category, n_words)."""
    taken = [False] * len(tokens)
    found: list[tuple[int, str, str, int]] = []
    for phrase, cat in phrases:
        n = len(phrase)
        i = 0
        while i <= len(tokens) - n:
            if not any(taken[i : i + n]) and _phrase_at(tokens, i, phrase):
                for j in range(i, i + n):
                    taken[j] = True
                found.append((i, " ".join(phrase), cat, n))
                i += n
            else:
                i += 1
    found.sort()
    return [(p, c, n) for _, p, c, n in found]


def _phrase_at(tokens: list[str], i: int, phrase: tuple[str, ...]) -> bool:
    for k, word in enumerate(phrase):
        tok = tokens[i + k]
        # Allow a simple plural on the last word of a phrase.
        if tok != word and not (k == len(phrase) - 1 and tok == word + "s"):
            return False
    return True


@dataclass
class RuleScore:
    scores: dict[str, float]
    word_count: int
    jargon_words: int
    density: float
    detected: list[str]
    category_counts: dict[str, int] = field(default_factory=dict)


def score_text(text: str, dictionary: JargonDictionary | None = None) -> RuleScore:
    d = dictionary or load_dictionary()
    tokens = tokenize(text)
    n = len(tokens)

    jargon_phrases = sorted(
        ((p, cat) for cat in JARGON_CATEGORIES for p in d.categories.get(cat, ())),
        key=lambda pc: len(pc[0]),
        reverse=True,
    )
    matches = _match(tokens, jargon_phrases)
    hedges = _match(tokens, [(p, "hedges") for p in d.categories.get("hedges", ())])
    concrete = _match(tokens, [(p, "concrete") for p in d.categories.get("concrete", ())])
    numbers = sum(1 for t in tokens if any(ch.isdigit() for ch in t))

    counts = Counter(cat for _, cat, _ in matches)
    jargon_words = sum(nw for _, _, nw in matches)
    unique = {p for p, _, _ in matches}
    density = jargon_words / n if n else 0.0
    n_concrete = len(concrete) + numbers

    if n == 0:
        return RuleScore({m: 0.0 for m in METRICS}, 0, 0, 0.0, [], {})

    scores = {
        # ~30% of words being jargon is peak executive presence.
        "jargon_density": clamp(density / 0.30 * 100),
        "buzzword_diversity": clamp(len(unique) / 12 * 80 + len(counts) / len(JARGON_CATEGORIES) * 20),
        "executive_confidence": clamp(
            40 + 15 * counts["confidence"] - 12 * len(hedges) + (10 if n >= 30 else -15 if n < 10 else 0)
        ),
        "strategic_alignment": clamp(counts["strategy"] * 20),
        "stakeholder_awareness": clamp(counts["stakeholders"] * 22),
        "actionability_theater": clamp(counts["action_theater"] * 16),
        "corporate_verbosity": clamp(n / 90 * 100),
        "question_evasion": 10.0 if n < 8 else clamp(70 + 10 * counts["evasion"] - 15 * n_concrete),
    }
    return RuleScore(
        scores={k: round(v, 1) for k, v in scores.items()},
        word_count=n,
        jargon_words=jargon_words,
        density=round(density, 3),
        detected=[p for p, _, _ in matches],
        category_counts=dict(counts),
    )


def weighted_overall(scores: dict[str, float], weights: dict[str, float]) -> float:
    total_w = sum(max(0.0, weights.get(m, 0.0)) for m in METRICS)
    if total_w <= 0:
        return 0.0
    return round(sum(scores.get(m, 0.0) * max(0.0, weights.get(m, 0.0)) for m in METRICS) / total_w, 1)


def terms_used(text: str, terms: list[str]) -> list[str]:
    """Which of the given terms appear in the text (used for PIP remedial exercises)."""
    tokens = tokenize(text)
    used = []
    for term in terms:
        phrase = tuple(tokenize(term))
        if phrase and any(_phrase_at(tokens, i, phrase) for i in range(len(tokens) - len(phrase) + 1)):
            used.append(term)
    return used
