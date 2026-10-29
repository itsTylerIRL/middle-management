"""Learning aids: coaching after each interrogation, and a word of the day to work into your answers."""

from __future__ import annotations

import logging
import random
from datetime import date
from typing import Any

from .llm import LLMClient, LLMError, parse_json_object
from .logs import event
from .prompts import coach_messages, word_messages
from .reputation import Store
from .scoring import METRICS, JargonDictionary, score_text, terms_used

log = logging.getLogger(__name__)

# Word-of-the-day categories, with the one-line hint shown when there's no LLM definition.
WORD_CATEGORIES = {
    "buzzwords": "A general-purpose buzzword: drop it anywhere to sound current.",
    "strategy": "Strategy talk: frame anything as part of the bigger picture.",
    "stakeholders": "Stakeholder talk: show you know who matters.",
    "action_theater": "Action theater: sound busy without committing to anything.",
    "confidence": "Executive confidence: say it like it's already been decided.",
    "evasion": "Evasion: appear to answer while answering nothing.",
}

# What to do about a weak metric, and which category of terms to suggest for it.
METRIC_ADVICE: dict[str, tuple[str, str | None]] = {
    "jargon_density": ("Pack more corporate idiom into every sentence.", "buzzwords"),
    "buzzword_diversity": ("Vary your vocabulary; one buzzword on repeat reads as a tic.", "buzzwords"),
    "executive_confidence": ("Drop the hedges and state it like a decision already made.", "confidence"),
    "strategic_alignment": ("Tie the update to strategy, the roadmap or the north star.", "strategy"),
    "stakeholder_awareness": ("Name-check the people who matter.", "stakeholders"),
    "actionability_theater": ("Promise next steps and owners without committing to anything.", "action_theater"),
    "corporate_verbosity": ("Use more words to say the same thing.", None),
    "question_evasion": ("Fewer specifics: numbers and plain facts can be checked.", "evasion"),
}


# --- word of the day -------------------------------------------------------
def pick_word(dictionary: JargonDictionary, usage: dict[str, int], rng: random.Random,
              avoid: str | None = None) -> tuple[str, str] | None:
    """A term weighted towards the ones you rarely use. Returns (term, category)."""
    pool = [(t, cat) for cat in WORD_CATEGORIES for t in dictionary.terms(cat) if len(t) > 4 and t != avoid]
    if not pool:
        return None
    return rng.choices(pool, [1 / (1 + usage.get(t, 0)) ** 2 for t, _ in pool])[0]


def word_of_day(store: Store, dictionary: JargonDictionary, today: date | None = None) -> dict[str, Any] | None:
    """Today's word, picked (and stored) on first use each day."""
    day = (today or date.today()).isoformat()
    current = store.get("word_of_day")
    if isinstance(current, dict) and current.get("date") == day:
        return current
    picked = pick_word(dictionary, store.buzzword_counts(), random.Random(day), (current or {}).get("term"))
    if picked is None:
        return None
    term, category = picked
    word = {"date": day, "term": term, "category": category, "hint": WORD_CATEGORIES[category],
            "definition": "", "example": "", "claimed_by": None}
    store.set("word_of_day", word)
    return word


def word_used(word: dict[str, Any] | None, transcript: str) -> bool:
    """Whether this answer earns today's bonus (once a day)."""
    return bool(word and not word.get("claimed_by") and terms_used(transcript, [word["term"]]))


async def define_word(store: Store, llm: LLMClient, max_tokens: int) -> None:
    """Ask the LLM for today's definition and example. The hint stands in if this fails."""
    word = store.get("word_of_day")
    if not isinstance(word, dict) or word.get("definition"):
        return
    try:
        obj = parse_json_object(await llm.chat(word_messages(word["term"]), max_tokens=max_tokens, temperature=0.7,
                                               json_mode=True))
    except (LLMError, ValueError) as e:
        event(log, "word of the day definition unavailable", logging.WARNING, error=str(e)[:200])
        return
    current = store.get("word_of_day")
    if isinstance(current, dict) and current.get("date") == word["date"]:
        current["definition"] = str(obj.get("definition") or "").strip()[:300]
        current["example"] = str(obj.get("example") or "").strip()[:300]
        store.set("word_of_day", current)


# --- coaching --------------------------------------------------------------
def weakest_metrics(scores: dict[str, float], n: int = 2) -> list[str]:
    return sorted(METRICS, key=lambda m: scores.get(m, 0.0))[:n]


def rules_coaching(evaluation: dict[str, Any], dictionary: JargonDictionary, rng: random.Random) -> dict[str, Any]:
    """Tips from the scores alone: what was weakest, and terms to try that you didn't use."""
    used = set(evaluation.get("detected_jargon") or [])
    tips: list[str] = []
    categories: set[str | None] = set()
    for metric in weakest_metrics(evaluation["scores"], len(METRICS)):
        advice, category = METRIC_ADVICE[metric]
        if category in categories:
            continue  # two tips suggesting the same kind of term is one tip
        if len(tips) == 2:
            break
        categories.add(category)
        terms = [t for t in dictionary.terms(category) if t not in used] if category else []
        if terms:
            advice += " Try: " + ", ".join(f"“{t}”" for t in rng.sample(terms, k=min(3, len(terms)))) + "."
        tips.append(advice)
    return {"source": "rules", "rewrite": None, "rewrite_jargon_ratio": None, "highlights": [], "tips": tips}


async def coach(llm: LLMClient | None, max_tokens: int, *, question: str, transcript: str,
                evaluation: dict[str, Any], dictionary: JargonDictionary, rng: random.Random,
                word: str | None = None) -> dict[str, Any]:
    """Your answer as an executive would have said it, with the phrases doing the work. Rules-only tips without an LLM."""
    fallback = rules_coaching(evaluation, dictionary, rng)
    if llm is None or not llm.enabled:
        return fallback
    try:
        raw = await llm.chat(coach_messages(question, transcript, weakest_metrics(evaluation["scores"]),
                                            evaluation.get("critique", ""), word),
                             max_tokens=max_tokens, temperature=0.7, json_mode=True)
        obj = parse_json_object(raw)
        rewrite = str(obj.get("rewrite") or "").strip()
        if not rewrite:
            raise ValueError("no rewrite in coaching response")
    except (LLMError, ValueError) as e:
        event(log, "coaching fell back to rules", logging.WARNING, error=str(e)[:200])
        return fallback
    highlights = [{"phrase": str(h["phrase"]).strip(), "why": str(h.get("why") or "").strip()}
                  for h in obj.get("highlights") or [] if isinstance(h, dict) and h.get("phrase")][:4]
    tip = str(obj.get("tip") or "").strip()
    return {"source": "llm", "rewrite": rewrite, "rewrite_jargon_ratio": score_text(rewrite, dictionary).density,
            "highlights": highlights, "tips": [tip] if tip else fallback["tips"][:1]}
