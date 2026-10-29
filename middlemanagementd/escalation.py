"""Escalation: failure levels 0-5, interruption policy, and fixed fallback lines."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from .config import EscalationConfig

MAX_LEVEL = 5

LEVEL_NAMES = {
    0: "Good Standing",
    1: "Gentle Pushback",
    2: "Alignment Concerns",
    3: "Executive Intervention",
    4: "Corporate Meltdown",
    5: "Performance Improvement Plan",
}

# Spoken when the LLM is unavailable. Each list is sampled at random.
FALLBACK_LINES: dict[str | int, tuple[str, ...]] = {
    "praise": (
        "Now that is what I call executive presence. You said a great deal and committed to absolutely nothing. "
        "I'm putting you forward for the leadership track.",
        "Wow. Just wow. That was so aligned I could feel the synergy. You are going places, and those places have "
        "corner offices.",
        "Love the energy, love the framing, love that I still have no idea what you did. That's leadership.",
    ),
    1: (
        "I'd really like to see you take a more strategic approach to that response.",
        "Good start, but I'm not hearing the strategic narrative. Let's elevate that next time.",
    ),
    2: (
        "I'm sensing some disconnect between your deliverables and the broader organizational priorities.",
        "I'm a little concerned we're not aligned on what alignment means. Let's put a pin in that, a big pin.",
    ),
    3: (
        "I'm going to stop you right there. We need to circle back and establish a shared understanding of what "
        "success looks like.",
        "Okay. Let's level set. I'll set up a working session to define the definition of done for your definition "
        "of done.",
    ),
    4: (
        "Let me be crystal clear about the opportunity space here. When we talk about strategic alignment, we are "
        "talking about aligning the alignment itself, holistically, at scale, across every swim lane in the "
        "ecosystem. Stakeholder engagement is not a task, it is a journey, and that journey has a roadmap, and that "
        "roadmap has a framework, and that framework needs to be scalable, repeatable, and frankly, synergistic. "
        "Operational excellence does not happen by accident. It happens through cross-functional synergies, "
        "proactive visibility, and relentless prioritization of the priorities we have prioritized. So going "
        "forward, I need you to double-click on your value-add, unpack your north star, and socialize your "
        "learnings with the broader enterprise. Are we aligned? Don't answer that. I'll answer it. We are not.",
    ),
    5: (
        "This is a formal Performance Improvement Plan. Your recent status reports have demonstrated insufficient "
        "synergy and chronic under-alignment with enterprise priorities. As a remedial exercise, at our next check "
        "in you must deliver a status update using all of the following terms: {terms}. Your success will be "
        "measured against criteria that will be defined later.",
    ),
    "pip_failed": (
        "I'm sorry to inform you that you did not satisfy the conditions of your Performance Improvement Plan. "
        "This plan is therefore being escalated to a second, more rigorous plan. Your terms remain: {terms}.",
    ),
    "pip_completed": (
        "Congratulations. You have completed your Performance Improvement Plan and are provisionally restored to "
        "good standing, pending further alignment.",
    ),
}

SILENCE_LINES = (
    "Silence is not a strategy.",
    "I'm going to take that silence as a lack of alignment.",
    "Hello? Are you on mute? You're on mute. Classic.",
)

INTERRUPT_LINES = (
    "I'm going to stop you right there.",
    "Let me just jump in here.",
)


@dataclass
class EscalationEngine:
    cfg: EscalationConfig

    def next_level(self, level: int, passed: bool, rng: random.Random) -> int:
        if passed:
            return max(0, level - self.cfg.decay_on_pass)
        a = max(0.0, self.cfg.aggressiveness)
        step = math.floor(a) + (1 if rng.random() < a - math.floor(a) else 0)
        return min(MAX_LEVEL, level + step)

    def stage(self, level_after: int, passed: bool) -> str | int:
        """Which reprimand (or praise) to deliver after an evaluation."""
        if passed:
            return "praise"
        return max(1, min(MAX_LEVEL, level_after))

    def interrupt_after(self, current_level: int) -> float | None:
        """Seconds of user speech allowed before the manager talks over them, or None."""
        if not self.cfg.interrupt_enabled:
            return None
        eligible = [lvl for lvl in self.cfg.interrupt_after_seconds if lvl <= current_level]
        if not eligible:
            return None
        return self.cfg.interrupt_after_seconds[max(eligible)]

    def is_monologue(self, stage: str | int) -> bool:
        return stage == 4

    def should_issue_pip(self, level_after: int, passed: bool, pip_open: bool) -> bool:
        return not passed and level_after >= MAX_LEVEL and not pip_open


def fallback_line(stage: str | int, rng: random.Random, terms: list[str] | None = None) -> str:
    line = rng.choice(FALLBACK_LINES[stage])
    return line.format(terms=", ".join(terms or []))
