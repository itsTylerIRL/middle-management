"""The weekly performance review: summarise a week of interrogations, rate it, have a manager write it up."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections import Counter
from collections.abc import Callable
from datetime import datetime, time as dtime, timedelta
from typing import Any

from .audio.port import AudioError, AudioPort
from .config import Config, ReviewConfig
from .llm import LLMClient, LLMError, parse_json_object
from .logs import event
from .personalities import Personality, pick_personality
from .prompts import review_messages
from .reputation import Store, rank_for
from .scoring import METRICS

log = logging.getLogger(__name__)

Emit = Callable[[str, dict[str, Any]], None]

RATINGS = {
    5: "Transformational Thought Leader",
    4: "Exceeds Alignment Expectations",
    3: "Meets Expectations",
    2: "Needs Synergy Improvement",
    1: "Does Not Meet Expectations",
    0: "Insufficient Visibility",
}

REVIEW_FIELDS = ("strengths", "development_areas", "objectives")


# --- calendar ------------------------------------------------------------------
def week_start(now: datetime) -> datetime:
    """Monday 00:00 of the week containing `now` (local time)."""
    return datetime.combine((now - timedelta(days=now.weekday())).date(), dtime())


def due_at(start: datetime, cfg: ReviewConfig) -> datetime:
    return datetime.combine((start + timedelta(days=cfg.weekday)).date(), dtime.fromisoformat(cfg.time))


def next_due(now: datetime, cfg: ReviewConfig, done_this_week: bool) -> datetime:
    """This week's slot (possibly already past, meaning: overdue), or next week's once this week is done."""
    start = week_start(now)
    return due_at(start + timedelta(days=7), cfg) if done_this_week else due_at(start, cfg)


# --- facts ---------------------------------------------------------------------
def summarize(store: Store, start: float, end: float) -> dict[str, Any]:
    """Everything the review is allowed to talk about, computed by code."""
    rows = store.between(start, end)
    points_before, points_after = store.points(before=start), store.points(before=end)
    summary: dict[str, Any] = {
        "week_of": datetime.fromtimestamp(start).strftime("%A %d %B %Y"),
        "interrogations": len(rows),
        "rank_at_start": rank_for(points_before)[1],
        "rank_at_end": rank_for(points_after)[1],
        "synergy_points_change": round(points_after - points_before, 1),
        "pips_issued": store.pips_between(start, end),
    }
    if not rows:
        return summary
    passed = [r for r in rows if r["passed"]]
    buzz = Counter(t.lower() for r in rows for t in r["detected_jargon"])
    managers = Counter(r["personality"] for r in rows)
    best = max(rows, key=lambda r: r["overall"])
    worst = min(rows, key=lambda r: r["overall"])
    previous = store.between(start - 7 * 86400, start)
    summary.update({
        "passed": len(passed),
        "failed": len(rows) - len(passed),
        "pass_rate": round(len(passed) / len(rows), 2),
        "average_score": round(sum(r["overall"] for r in rows) / len(rows), 1),
        "previous_week_average": (round(sum(r["overall"] for r in previous) / len(previous), 1)
                                  if previous else None),
        "best_score": round(best["overall"], 1),
        "best_answer": (best.get("transcript") or "")[:200],
        "worst_score": round(worst["overall"], 1),
        "metric_averages": {m: round(sum(r["scores"].get(m, 0) for r in rows) / len(rows)) for m in METRICS},
        "top_buzzwords": buzz.most_common(5),
        "silent_answers": sum(1 for r in rows if not r["word_count"]),
        "times_cut_off": sum(1 for r in rows if r["interrupted"]),
        "times_talked_over_management": sum(int(r.get("barge_ins") or 0) for r in rows),
        "escalation_start": rows[0]["level_before"],
        "escalation_end": rows[-1]["level_after"],
        "highest_escalation": max(r["level_after"] for r in rows),
        "most_frequent_manager": managers.most_common(1)[0][0],
    })
    return summary


def rate(summary: dict[str, Any]) -> tuple[int, str]:
    """HR calibration: code decides the rating, the manager only gets to explain it."""
    if not summary["interrogations"]:
        return 0, RATINGS[0]
    score = summary["pass_rate"] * 60 + summary["average_score"] * 0.4 - 10 * summary["pips_issued"]
    rating = 5 if score >= 80 else 4 if score >= 65 else 3 if score >= 50 else 2 if score >= 35 else 1
    return rating, RATINGS[rating]


# --- writing -------------------------------------------------------------------
def parse_review(text: str) -> dict[str, Any]:
    data = parse_json_object(text)
    out: dict[str, Any] = {}
    for key in ("headline", "closing"):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"review missing {key}")
        out[key] = value.strip()
    for key in REVIEW_FIELDS:
        items = data.get(key)
        if not isinstance(items, list):
            raise ValueError(f"review missing {key}")
        out[key] = [str(i).strip() for i in items if str(i).strip()][:3]
        if not out[key]:
            raise ValueError(f"review has empty {key}")
    return out


def fallback_review(summary: dict[str, Any], rating_name: str, rng: random.Random) -> dict[str, Any]:
    """A review written by the HR template when the LLM is unavailable."""
    n = summary["interrogations"]
    if not n:
        return {
            "headline": "This week you achieved a level of invisibility normally reserved for senior leadership.",
            "strengths": ["You avoided every opportunity to say anything that could be held against you."],
            "development_areas": ["Leadership has no visibility into your visibility, which is a visibility issue."],
            "objectives": ["Be seen being seen.", "Socialise your presence with key stakeholders."],
            "closing": "Let's touch base next week. Or the week after. My calendar is very aligned.",
        }
    strengths = [f"You completed {n} status update{'s' if n != 1 else ''}, which is {n} more than strictly necessary."]
    if summary.get("top_buzzwords"):
        term, count = summary["top_buzzwords"][0]
        strengths.append(f"Your commitment to the word {term}, {count} time{'s' if count != 1 else ''}, "
                         "shows real ownership of the narrative.")
    areas = []
    if summary.get("failed"):
        areas.append(f"{summary['failed']} of your updates contained actual information. We need to socialise less of that.")
    if summary.get("times_cut_off"):
        areas.append("You were cut off for talking too long, which is a privilege of management, not of you.")
    if summary.get("pips_issued"):
        areas.append("You were placed on a Performance Improvement Plan, which is not a growth mindset. Or it is. Unclear.")
    if not areas:
        areas.append("Your average score of "
                     f"{summary['average_score']:.0f} leaves room to unlock additional value-add.")
    return {
        "headline": f"An overall rating of {rating_name}, reflecting a week of {rng.choice(['measurable', 'directional', 'holistic', 'scalable'])} impact.",
        "strengths": strengths,
        "development_areas": areas[:2],
        "objectives": rng.sample([
            "Move the needle on moving the needle.",
            "Drive alignment on what alignment means.",
            "Increase stakeholder visibility by twenty percent, measured qualitatively.",
            "Take the north star offline and circle back with a revised north star.",
        ], 2),
        "closing": "Thanks for your time. Let's keep this conversation going, asynchronously, forever.",
    }


def script(review: dict[str, Any], rating_name: str, week_of: str) -> list[str]:
    """What gets read aloud, sentence group by sentence group."""
    lines = [f"Thanks for making time. This is your weekly performance review for the week of {week_of}.",
             "Please hold your questions until the end.",
             f"Your overall rating is: {rating_name}.",
             review["headline"],
             "Key strengths.", *review["strengths"],
             "Areas for development.", *review["development_areas"],
             "Your objectives for next week.", *review["objectives"],
             review["closing"]]
    return [line for line in lines if line]


class ReviewWriter:
    def __init__(self, cfg: Config, store: Store, llm: LLMClient | None, rng: random.Random | None = None,
                 emit: Emit | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self.llm = llm
        self.rng = rng or random.Random()
        self.emit: Emit = emit or (lambda kind, data: None)

    async def write(self, summary: dict[str, Any], rating_name: str,
                    persona: Personality) -> tuple[dict[str, Any], str]:
        """Return (review, author) where author is 'llm' or 'template'."""
        if self.llm is not None and self.llm.enabled:
            try:
                text = await self.llm.chat(review_messages(persona, summary, rating_name),
                                           max_tokens=self.cfg.llm.max_tokens_review,
                                           temperature=self.cfg.llm.temperature, json_mode=self.cfg.llm.json_mode)
                return parse_review(text), "llm"
            except (LLMError, ValueError) as e:
                event(log, "review fell back to the HR template", logging.WARNING, error=str(e)[:200])
        return fallback_review(summary, rating_name, self.rng), "template"

    async def run(self, audio: AudioPort | None, *, start: float, end: float, personality: str | None = None,
                  trigger: str = "scheduled") -> dict[str, Any]:
        """Write the review for [start, end), read it aloud if `audio` is given, and file it."""
        persona = pick_personality(personality or self.cfg.review.personality, self.cfg.personality.enabled,
                                   self.rng)
        summary = summarize(self.store, start, end)
        rating, rating_name = rate(summary)
        self.emit("review_started", {"personality": persona.title, "week_of": summary["week_of"],
                                     "aloud": audio is not None, "trigger": trigger})
        event(log, "weekly review started", personality=persona.key, aloud=audio is not None, rating=rating)

        if audio is not None:
            await audio.chime()
            await audio.say("Do you have a minute? It's time for your weekly performance review. "
                            "I'm just pulling up your file.", length_scale=persona.length_scale)
            review, author = await self._with_hold_music(audio, self.write(summary, rating_name, persona))
        else:
            review, author = await self.write(summary, rating_name, persona)

        review_id = self.store.add_review(period_start=start, period_end=end, personality=persona.key,
                                          rating=rating, rating_name=rating_name, summary=summary, review=review,
                                          author=author, delivered=False, trigger=trigger)
        if audio is not None:
            for line in script(review, rating_name, summary["week_of"]):
                await audio.say(line, length_scale=persona.length_scale)
            self.store.mark_review_delivered(review_id)
        record = self.store.review(review_id)
        assert record is not None
        self.emit("review_ready", {"id": review_id, "personality": persona.title, "rating": rating,
                                   "rating_name": rating_name, "headline": review["headline"],
                                   "delivered": record["delivered"], "week_of": summary["week_of"]})
        event(log, "weekly review filed", id=review_id, rating=rating, author=author, delivered=record["delivered"])
        return record

    async def _with_hold_music(self, audio: AudioPort, coro: Any) -> Any:
        if not self.cfg.audio.hold_music:
            return await coro
        hold = asyncio.create_task(audio.hold_music(self.cfg.llm.timeout_seconds + 15))
        try:
            return await coro
        finally:
            hold.cancel()
            with contextlib.suppress(asyncio.CancelledError, AudioError):
                await hold


def period(now: datetime, previous: bool = False) -> tuple[float, float]:
    """The week to review: this week so far, or all of last week."""
    start = week_start(now)
    if previous:
        return (start - timedelta(days=7)).timestamp(), start.timestamp()
    return start.timestamp(), max(now.timestamp(), start.timestamp() + 1)

