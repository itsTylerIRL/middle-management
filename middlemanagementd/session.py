"""One interrogation, start to finish: chime, question, listen, evaluate, reprimand, persist."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from .audio.port import AudioError, AudioPort, BargeIn, ListenRequest, ListenResult
from .config import Config
from .escalation import (INTERRUPT_LINES, LEVEL_NAMES, SILENCE_LINES, TALK_OVER_LINES, YIELD_RESUME_LINES,
                         YIELD_WIN_LINES, EscalationEngine, fallback_line)
from .evaluator import Evaluation, JargonEvaluator
from .llm import LLMClient, LLMError, sentences
from .logs import event
from .personalities import Personality, pick_personality
from .practice import coach, word_of_day, word_used
from .prompts import character_messages
from .reputation import Store, rank_for
from .scoring import score_text, terms_used, weighted_overall

log = logging.getLogger(__name__)

Emit = Callable[[str, dict[str, Any]], None]

_UNSPEAKABLE = re.compile(r"[*_#`~>\[\]]|[\U0001F000-\U0001FAFF☀-➿]")


def speakable(text: str) -> str:
    text = _UNSPEAKABLE.sub("", text)
    return re.sub(r"\s+", " ", text).strip().strip('"').strip()


@dataclass
class _Feedback:
    """Mutable state while the manager delivers feedback and the user may talk over it."""

    talk_over: bool
    gain: float = 1.0
    spoken: list[str] = field(default_factory=list)
    interjections: list[str] = field(default_factory=list)
    barge_ins: int = 0
    retort_owed: bool = False
    conceded: bool = False


@dataclass
class SessionOutcome:
    personality: str
    question: str
    transcript: str
    interrupted: bool
    evaluation: dict[str, Any]
    level_before: int
    level_after: int
    stage: str
    feedback: str
    pip: dict[str, Any] | None
    rank_before: str
    rank_after: str
    record_id: int
    duration_seconds: float
    barge_ins: int = 0
    word_of_day: str | None = None
    word_bonus: float = 0.0
    coaching: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class InterrogationRunner:
    def __init__(self, cfg: Config, store: Store, llm: LLMClient | None, evaluator: JargonEvaluator,
                 rng: random.Random | None = None, emit: Emit | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self.llm = llm
        self.evaluator = evaluator
        self.escalation = EscalationEngine(cfg.escalation)
        self.rng = rng or random.Random()
        self.emit: Emit = emit or (lambda kind, data: None)

    async def run(self, audio: AudioPort, trigger: str = "manual", personality: str | None = None) -> SessionOutcome:
        started = time.monotonic()
        persona = pick_personality(personality or self.cfg.personality.default, self.cfg.personality.enabled, self.rng)
        level_before = self.store.level
        pip = self.store.open_pip()
        word = word_of_day(self.store, self.evaluator.dictionary) if self.cfg.practice.word_of_day else None
        _, rank_before = rank_for(self.store.points())
        self.emit("session_started", {"trigger": trigger, "personality": persona.title, "level": level_before})
        event(log, "interrogation started", trigger=trigger, personality=persona.key, level=level_before)

        await audio.chime()
        if pip:
            question = (f"This is your Performance Improvement Plan check-in. Deliver a status update that uses "
                        f"all of the following terms: {', '.join(pip.terms)}.")
        else:
            question = persona.random_question(self.rng)
        self.emit("question", {"personality": persona.title, "text": question})
        lc = self.cfg.listen
        listen_req = ListenRequest(
            max_seconds=lc.speech_timeout_seconds, start_timeout=lc.grace_period_seconds,
            end_silence=lc.end_silence_seconds, min_speech=lc.min_speech_seconds,
            interrupt_after=self.escalation.interrupt_after(level_before),
        )
        # Eager employees may start answering before the question is over; the manager lets them.
        asked = await audio.say(question, length_scale=persona.length_scale,
                                barge_in=self._barge_in("yield", listen_req))
        barge_ins = 0
        if asked.barged_in and asked.text:
            barge_ins += 1
            self.emit("barge_in", {"phase": "question", "mode": "yield", "text": asked.text})
            result = ListenResult(text=asked.text, speech_seconds=asked.speech_seconds, interrupted=asked.interrupted)
        else:
            result = await audio.listen(listen_req)
        transcript = result.text.strip()
        self.emit("transcript", {"text": transcript, "interrupted": result.interrupted,
                                 "speech_seconds": round(result.speech_seconds, 1)})

        if result.interrupted:
            await audio.say(self.rng.choice(persona.interrupt_lines + INTERRUPT_LINES),
                            length_scale=persona.length_scale)
        elif not transcript:
            await audio.say(self.rng.choice(SILENCE_LINES), length_scale=persona.length_scale)
        else:
            await audio.say(self.rng.choice(persona.filler_lines), length_scale=persona.length_scale)

        evaluation = await self._with_hold_music(audio, self.evaluator.evaluate(question, transcript, persona))
        self.emit("evaluation", evaluation.to_dict())

        stage, level_after, pip_info, pip_issued = self._decide(evaluation, transcript, level_before, pip)
        earned_word = word_used(word, transcript)
        fb = _Feedback(talk_over=level_after >= self.cfg.escalation.talk_over_level)
        await self._speak_character(
            audio, persona, stage, fb, question=question, transcript=transcript, evaluation=evaluation,
            interrupted=result.interrupted, pip_terms=(pip_info or {}).get("terms"),
            pip_used=len((pip_info or {}).get("used", [])), word=word["term"] if word else None,
            used_word=earned_word,
        )
        feedback = " ".join(fb.spoken)
        barge_ins += fb.barge_ins
        if fb.conceded and level_after > 0:
            # A sufficiently corporate interjection talks the manager down a notch.
            level_after -= 1
            self.emit("conceded", {"level": level_after})
        if barge_ins:
            self.store.set("barge_ins", int(self.store.get("barge_ins", 0)) + barge_ins)
        self.emit("feedback", {"stage": stage, "level": level_after, "text": feedback})

        bonus = self.cfg.practice.word_of_day_points if earned_word else 0.0
        record_id = self.store.record(
            trigger=trigger, personality=persona.key, question=question,
            transcript=transcript if self.cfg.storage.store_transcripts else None,
            evaluation=evaluation.to_dict(), interrupted=result.interrupted, level_before=level_before,
            level_after=level_after, feedback=feedback, pip_issued=pip_issued, barge_ins=barge_ins, bonus=bonus,
        )
        if word and earned_word:
            self.store.set("word_of_day", {**word, "claimed_by": record_id})
            self.emit("word_of_day", {"term": word["term"], "points": bonus})
            await audio.say(f"I also noticed you worked in today's word, {word['term']}. That has been noted in "
                            f"your file.", length_scale=persona.length_scale)
        _, rank_after = rank_for(self.store.points())
        if rank_after != rank_before:
            verb = "promoted" if evaluation.passed else "reassigned"
            await audio.say(f"Effective immediately, you have been {verb}. Your new title is {rank_after}.",
                            length_scale=persona.length_scale)
            self.emit("rank_change", {"from": rank_before, "to": rank_after})

        coaching = None
        if self.cfg.practice.coaching:
            # Talking is over; the coaching is for reading, so no hold music.
            coaching = await coach(self.llm, self.cfg.llm.max_tokens_coaching, question=question,
                                   transcript=transcript, evaluation=evaluation.to_dict(),
                                   dictionary=self.evaluator.dictionary, rng=self.rng,
                                   word=word["term"] if word else None)
            self.store.set_coaching(record_id, coaching)
            self.emit("coaching", coaching)

        outcome = SessionOutcome(
            personality=persona.title, question=question, transcript=transcript, interrupted=result.interrupted,
            evaluation=evaluation.to_dict(), level_before=level_before, level_after=level_after, stage=str(stage),
            feedback=feedback, pip=pip_info, rank_before=rank_before, rank_after=rank_after, record_id=record_id,
            duration_seconds=round(time.monotonic() - started, 1), barge_ins=barge_ins,
            word_of_day=word["term"] if word else None, word_bonus=bonus, coaching=coaching,
        )
        event(log, "interrogation finished", passed=evaluation.passed, overall=evaluation.overall,
              evaluator=evaluation.evaluator, level=level_after, stage=str(stage))
        self.emit("session_finished", outcome.to_dict())
        return outcome

    def _decide(self, ev: Evaluation, transcript: str, level_before: int,
                pip: Any) -> tuple[str | int, int, dict[str, Any] | None, bool]:
        """Return (stage, level_after, pip_info, pip_issued)."""
        if pip is not None:
            used = terms_used(transcript, pip.terms)
            if ev.passed and len(used) >= self.cfg.escalation.pip_terms_to_pass:
                self.store.resolve_pip(pip.id, "completed")
                return "pip_completed", 2, {"id": pip.id, "terms": pip.terms, "used": used, "status": "completed"}, False
            self.store.resolve_pip(pip.id, "failed")
            new = self.store.issue_pip(pip.terms)
            return "pip_failed", 5, {"id": new.id, "terms": pip.terms, "used": used, "status": "reissued"}, True

        level_after = self.escalation.next_level(level_before, ev.passed, self.rng)
        stage = self.escalation.stage(level_after, ev.passed)
        if self.escalation.should_issue_pip(level_after, ev.passed, pip_open=False):
            new = self.store.issue_pip(self._pip_terms())
            return 5, level_after, {"id": new.id, "terms": new.terms, "used": [], "status": "issued"}, True
        if stage == 5:
            # Level 5 without a fresh PIP can't happen above; keep the stage consistent anyway.
            stage = 4
        return stage, level_after, None, False

    def _pip_terms(self) -> list[str]:
        d = self.evaluator.dictionary
        pool = sorted({t for cat in ("buzzwords", "strategy", "stakeholders") for t in d.terms(cat) if len(t) > 4})
        return self.rng.sample(pool, k=min(self.cfg.escalation.pip_required_terms, len(pool)))

    async def _with_hold_music(self, audio: AudioPort, coro: Any) -> Evaluation:
        if not self.cfg.audio.hold_music:
            return await coro
        hold = asyncio.create_task(audio.hold_music(self.cfg.llm.timeout_seconds + 15))
        try:
            return await coro
        finally:
            hold.cancel()
            with contextlib.suppress(asyncio.CancelledError, AudioError):
                await hold

    def _barge_in(self, mode: str, listen: ListenRequest | None = None) -> BargeIn | None:
        if not self.cfg.audio.barge_in:
            return None
        if listen is None:
            lc = self.cfg.listen
            listen = ListenRequest(max_seconds=20, start_timeout=2, end_silence=lc.end_silence_seconds,
                                   min_speech=lc.min_speech_seconds)
        return BargeIn(mode=mode, listen=listen, boost=self.cfg.escalation.talk_over_boost)

    def _jargon_passes(self, text: str, persona: Personality) -> bool:
        """Instant rules-only verdict on an interjection (no time to consult the LLM mid-sentence)."""
        rules = score_text(text, self.evaluator.dictionary)
        return weighted_overall(rules.scores, self.evaluator.weights(persona)) >= self.evaluator.threshold(persona)

    async def _say_feedback(self, audio: AudioPort, persona: Personality, text: str, length_scale: float,
                            fb: _Feedback) -> None:
        if fb.retort_owed:
            retort = self.rng.choice(TALK_OVER_LINES)
            await audio.say(retort, length_scale=length_scale, gain=fb.gain)
            fb.spoken.append(retort)
            fb.retort_owed = False
        res = await audio.say(text, length_scale=length_scale, gain=fb.gain,
                              barge_in=self._barge_in("talk_over" if fb.talk_over else "yield"))
        fb.spoken.append(text)
        if not res.barged_in:
            return
        fb.barge_ins += 1
        if fb.talk_over:
            # Refuse to yield: get louder (the audio client still enforces max_volume) and say so.
            fb.gain *= self.cfg.escalation.talk_over_boost
            fb.retort_owed = True
            self.emit("barge_in", {"phase": "feedback", "mode": "talk_over"})
            return
        self.emit("barge_in", {"phase": "feedback", "mode": "yield", "text": res.text})
        fb.interjections.append(res.text)
        if res.text and self._jargon_passes(res.text, persona):
            line, fb.conceded = self.rng.choice(YIELD_WIN_LINES), True
        else:
            line = self.rng.choice(YIELD_RESUME_LINES)
        await audio.say(line, length_scale=length_scale, gain=fb.gain)
        fb.spoken.append(line)

    async def _speak_character(self, audio: AudioPort, persona: Personality, stage: str | int, fb: _Feedback, *,
                               question: str, transcript: str, evaluation: Evaluation, interrupted: bool,
                               pip_terms: list[str] | None, pip_used: int, word: str | None = None,
                               used_word: bool = False) -> None:
        meltdown = self.escalation.is_monologue(stage)
        length_scale = persona.length_scale * (0.85 if meltdown else 1.0)

        if self.llm is not None and self.llm.enabled and self.cfg.escalation.use_llm:
            max_tokens = (self.cfg.llm.max_tokens_monologue if meltdown else
                          self.cfg.llm.max_tokens_pip if stage in (5, "pip_failed") else
                          self.cfg.llm.max_tokens_feedback)
            messages = character_messages(
                persona, stage, question=question, transcript=transcript, critique=evaluation.critique,
                detected=evaluation.detected_jargon, overall=evaluation.overall, pip_terms=pip_terms,
                pip_used=pip_used, interrupted=interrupted, word_of_day=word, used_word_of_day=used_word,
            )
            deadline = time.monotonic() + (self.cfg.escalation.monologue_max_seconds if meltdown
                                           else self.cfg.audio.max_utterance_seconds)
            queue: asyncio.Queue[str | None] = asyncio.Queue()
            failure: list[Exception] = []
            llm = self.llm

            async def produce() -> None:
                try:
                    async for sentence in sentences(llm.stream(messages, max_tokens=max_tokens)):
                        await queue.put(sentence)
                except LLMError as e:
                    failure.append(e)
                finally:
                    await queue.put(None)

            # Generation runs ahead while earlier sentences are being spoken.
            producer = asyncio.create_task(produce())
            try:
                while (sentence := await queue.get()) is not None:
                    if text := speakable(sentence):
                        await self._say_feedback(audio, persona, text, length_scale, fb)
                    if fb.conceded or time.monotonic() > deadline:
                        break
            finally:
                producer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await producer
            if failure:
                event(log, "character speech fell back to fixed lines", logging.WARNING, error=str(failure[0])[:200])

        if not fb.spoken:
            await self._say_feedback(audio, persona, fallback_line(stage, self.rng, pip_terms), length_scale, fb)
        if fb.retort_owed:
            retort = self.rng.choice(TALK_OVER_LINES)
            await audio.say(retort, length_scale=length_scale, gain=fb.gain)
            fb.spoken.append(retort)
        if stage == 5 and pip_terms and not any(pip_terms[0].lower() in s.lower() for s in fb.spoken):
            # The exercise terms must be heard, whatever the LLM decided to say.
            reminder = f"Your required terms are: {', '.join(pip_terms)}."
            await audio.say(reminder, length_scale=persona.length_scale)
            fb.spoken.append(reminder)


def level_name(level: int) -> str:
    return LEVEL_NAMES.get(level, "Unknown")
