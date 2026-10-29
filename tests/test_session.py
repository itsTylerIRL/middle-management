"""End-to-end MVP slice with mock audio and a fake LLM: no devices, no network."""

import json

import pytest

from middlemanagementd.audio.port import AudioUnavailable, MockAudioPort
from middlemanagementd.evaluator import JargonEvaluator
from middlemanagementd.llm import LLMError
from middlemanagementd.reputation import Store
from middlemanagementd.scoring import METRICS
from middlemanagementd.session import InterrogationRunner

from .conftest import JARGON, PLAIN, FakeLLM


def make_runner(cfg, rng, llm=None):
    store = Store(cfg.storage.database_path)
    events = []
    runner = InterrogationRunner(cfg, store, llm, JargonEvaluator(cfg.evaluation, llm), rng,
                                 emit=lambda k, d: events.append((k, d)))
    return runner, store, events


async def test_mvp_vertical_slice_pass(cfg, rng):
    llm = FakeLLM(chat_reply=json.dumps({"scores": {m: 90 for m in METRICS}, "detected_jargon": ["synergy"],
                                         "critique": "Superb nothing."}),
                  stream_reply="Outstanding executive presence. You said nothing. Promote this person.")
    runner, store, events = make_runner(cfg, rng, llm)
    audio = MockAudioPort(responses=[JARGON])
    out = await runner.run(audio, trigger="manual")

    ops = [op for op, _ in audio.log]
    assert ops[0] == "chime" and ops[1] == "say" and "listen" in ops and "hold_music" in ops
    assert "hold_music_stopped" in ops  # hold music cancelled once evaluation returned
    assert out.evaluation["passed"] and out.stage == "praise" and out.level_after == 0
    assert any("Promote this person." in s for s in audio.spoken)
    assert store.stats()["successful_reports"] == 1
    assert store.history()[0]["transcript"] == JARGON
    kinds = [k for k, _ in events]
    assert kinds[0] == "session_started" and kinds[-1] == "session_finished"


async def test_failure_escalates_and_uses_fallback_when_llm_down(cfg, rng):
    runner, store, _ = make_runner(cfg, rng, FakeLLM(LLMError("down"), LLMError("down")))
    out = await runner.run(MockAudioPort(responses=[PLAIN]))
    assert not out.evaluation["passed"]
    assert out.evaluation["evaluator"] == "rules"
    assert (out.level_before, out.level_after, out.stage) == (0, 1, "1")
    assert out.feedback  # a fixed fallback line was spoken


async def test_silence_fails(cfg, rng):
    runner, store, _ = make_runner(cfg, rng)
    audio = MockAudioPort(responses=[""])
    out = await runner.run(audio)
    assert out.evaluation["evaluator"] == "silence" and not out.evaluation["passed"]
    assert store.level == 1


async def test_full_escalation_to_pip_and_recovery(cfg, rng):
    runner, store, _ = make_runner(cfg, rng)
    for _ in range(5):
        out = await runner.run(MockAudioPort(responses=[PLAIN]))
    assert out.level_after == 5 and out.stage == "5"
    pip = store.open_pip()
    assert pip is not None and len(pip.terms) == cfg.escalation.pip_required_terms
    assert any(t in out.feedback for t in pip.terms)
    assert store.stats()["pips_issued"] == 1

    # The PIP check-in question lists the required terms.
    audio = MockAudioPort(responses=[PLAIN])
    out = await runner.run(audio)
    assert "Performance Improvement Plan" in audio.spoken[0]
    assert out.stage == "pip_failed" and store.stats()["pips_issued"] == 2

    # Complete the PIP: jargon answer that uses every required term.
    pip = store.open_pip()
    answer = JARGON + " Also " + ", ".join(pip.terms) + ", absolutely, holistically aligned."
    out = await runner.run(MockAudioPort(responses=[answer]))
    assert out.evaluation["passed"], out.evaluation
    assert out.stage == "pip_completed" and out.level_after == 2 and store.open_pip() is None


async def test_meltdown_monologue_is_streamed_sentence_by_sentence(cfg, rng):
    llm = FakeLLM(chat_reply=None, stream_reply="Synergy is a journey. Alignment is a mindset! Are we aligned? No.")
    runner, store, _ = make_runner(cfg, rng, llm)
    store.level = 3
    out = await runner.run(MockAudioPort(responses=[PLAIN]))
    assert out.stage == "4"
    assert out.feedback == "Synergy is a journey. Alignment is a mindset! Are we aligned? No."
    assert "Level 4" in llm.stream_calls[0][1]["content"]


async def test_interruption_requested_at_high_levels(cfg, rng):
    runner, store, _ = make_runner(cfg, rng)
    store.level = 4
    audio = MockAudioPort(responses=[JARGON], interrupted=True)
    out = await runner.run(audio)
    listen_req = next(arg for op, arg in audio.log if op == "listen")
    assert listen_req.interrupt_after == cfg.escalation.interrupt_after_seconds[4]
    assert out.interrupted


async def test_no_interruption_at_low_levels(cfg, rng):
    runner, _, _ = make_runner(cfg, rng)
    audio = MockAudioPort(responses=[JARGON], interrupted=True)
    out = await runner.run(audio)
    assert next(arg for op, arg in audio.log if op == "listen").interrupt_after is None
    assert not out.interrupted


async def test_audio_failure_propagates_and_records_nothing(cfg, rng):
    runner, store, _ = make_runner(cfg, rng)
    with pytest.raises(AudioUnavailable):
        await runner.run(MockAudioPort(responses=[JARGON], fail_on="listen"))
    assert store.stats()["total_interrogations"] == 0


async def test_transcripts_not_stored_when_disabled(cfg, rng):
    cfg.storage.store_transcripts = False
    runner, store, _ = make_runner(cfg, rng)
    await runner.run(MockAudioPort(responses=[JARGON]))
    assert store.history()[0]["transcript"] is None


async def test_rank_promotion_announced(cfg, rng):
    runner, store, events = make_runner(cfg, rng)
    for _ in range(4):
        await runner.run(MockAudioPort(responses=[JARGON]))
    assert any(k == "rank_change" for k, _ in events)
    assert store.stats()["rank_number"] >= 2


async def test_answering_during_the_question_skips_listen(cfg, rng):
    runner, _, events = make_runner(cfg, rng)
    audio = MockAudioPort(barge_at={0: JARGON})  # cut in on the question itself
    out = await runner.run(audio)
    assert "listen" not in [op for op, _ in audio.log]
    assert out.transcript == JARGON and out.evaluation["passed"] and out.barge_ins == 1
    assert any(k == "barge_in" for k, _ in events)


async def test_low_level_manager_yields_and_concedes_to_good_jargon(cfg, rng):
    llm = FakeLLM(chat_reply=None, stream_reply="You need more strategy. Much more. Think bigger.")
    runner, store, events = make_runner(cfg, rng, llm)
    store.level = 1
    # say #0 question, #1 filler, #2 first feedback sentence: talk over it with jargon.
    audio = MockAudioPort(responses=[PLAIN], barge_at={2: JARGON})
    out = await runner.run(audio)
    assert ("barge_in", ("yield", JARGON)) in audio.log
    assert out.level_after == 1  # would have been 2; the manager conceded a level
    assert "Much more." not in out.feedback  # stopped talking
    assert store.stats()["times_talked_over_management"] == 1


async def test_low_level_manager_resumes_after_weak_interjection(cfg, rng):
    llm = FakeLLM(chat_reply=None, stream_reply="You need more strategy. Much more. Think bigger.")
    runner, store, _ = make_runner(cfg, rng, llm)
    audio = MockAudioPort(responses=[PLAIN], barge_at={2: "no I fixed the bug"})
    out = await runner.run(audio)
    assert "As I was saying" in out.feedback and "Think bigger." in out.feedback
    assert out.level_after == 1


async def test_high_level_manager_talks_over_you_louder(cfg, rng):
    llm = FakeLLM(chat_reply=None, stream_reply="Synergy is a journey. Alignment is a mindset. Are we aligned?")
    runner, store, _ = make_runner(cfg, rng, llm)
    store.level = 3
    audio = MockAudioPort(responses=[PLAIN], barge_at={2: "x", 4: "x"})
    out = await runner.run(audio)
    assert out.stage == "4" and out.barge_ins == 2
    modes = [arg[0] for op, arg in audio.log if op == "barge_in"]
    assert modes == ["talk_over", "talk_over"]
    assert max(audio.gains) == pytest.approx(cfg.escalation.talk_over_boost ** 2)
    # The monologue continued, and retorts were inserted.
    assert "Are we aligned?" in out.feedback
    from middlemanagementd.escalation import TALK_OVER_LINES
    assert sum(line in out.feedback for line in TALK_OVER_LINES) >= 1


async def test_barge_in_disabled(cfg, rng):
    cfg.audio.barge_in = False
    runner, _, _ = make_runner(cfg, rng)
    audio = MockAudioPort(responses=[JARGON], barge_at={0: "ignored"})
    out = await runner.run(audio)
    assert out.barge_ins == 0 and "listen" in [op for op, _ in audio.log]
