"""Weekly performance reviews: the facts, the rating, the write-up, and the delivery."""

import json
import sqlite3
import time
from datetime import datetime

import pytest

from middlemanagementd.audio.port import MockAudioPort
from middlemanagementd.config import ReviewConfig
from middlemanagementd.llm import LLMError
from middlemanagementd.reputation import Store
from middlemanagementd.review import (RATINGS, ReviewWriter, due_at, next_due, parse_review, period, rate, script,
                                      summarize, week_start)

from .conftest import FakeLLM

REVIEW = {"headline": "A week of directional impact.", "strengths": ["Synergy, leveraged.", "Stakeholders, managed."],
          "development_areas": ["Fewer facts."], "objectives": ["Align the alignment."], "closing": "Let's circle back."}


def ev(passed: bool, overall: float, jargon=("synergy",)) -> dict:
    return {"overall": overall, "threshold": 58, "passed": passed, "word_count": 40, "jargon_ratio": 0.3,
            "evaluator": "rules", "scores": {"jargon_density": overall}, "detected_jargon": list(jargon)}


def record(store: Store, passed: bool, overall: float, *, level=(0, 0), barge_ins=0, pip=False, **kw) -> None:
    store.record(trigger="scheduled", personality="big_four", question="Status?", transcript="We synergised.",
                 evaluation=ev(passed, overall, **kw), interrupted=False, level_before=level[0],
                 level_after=level[1], feedback="Noted.", pip_issued=pip, barge_ins=barge_ins)


def test_week_boundaries_and_due_time():
    cfg = ReviewConfig(weekday=4, time="16:55")
    wed = datetime(2026, 10, 28, 11, 0)
    assert week_start(wed) == datetime(2026, 10, 26)
    assert due_at(week_start(wed), cfg) == datetime(2026, 10, 30, 16, 55)
    assert next_due(wed, cfg, done_this_week=False) == datetime(2026, 10, 30, 16, 55)
    # Overdue (Saturday, not yet done) -> this week's slot, already in the past.
    assert next_due(datetime(2026, 10, 31, 9, 0), cfg, done_this_week=False) < datetime(2026, 10, 31, 9, 0)
    assert next_due(datetime(2026, 10, 30, 17, 0), cfg, done_this_week=True) == datetime(2026, 11, 6, 16, 55)
    start, end = period(wed, previous=True)
    assert datetime.fromtimestamp(start) == datetime(2026, 10, 19) and datetime.fromtimestamp(end) == datetime(2026, 10, 26)


def test_summary_counts_only_the_period():
    store = Store(":memory:")
    record(store, True, 80, level=(1, 0), jargon=("synergy", "north star"))
    record(store, False, 40, level=(0, 1), barge_ins=2)
    record(store, True, 70, level=(1, 0))
    # One from last week.
    store.db.execute("UPDATE interrogations SET ts = ts - 8 * 86400 WHERE id = 1")
    store.db.commit()
    now = time.time()
    s = summarize(store, now - 3600, now + 1)
    assert s["interrogations"] == 2 and s["passed"] == 1 and s["failed"] == 1
    assert s["average_score"] == 55.0 and s["best_score"] == 70 and s["worst_score"] == 40
    assert s["times_talked_over_management"] == 2
    assert s["top_buzzwords"][0] == ("synergy", 2)
    assert s["escalation_start"] == 0 and s["escalation_end"] == 0 and s["highest_escalation"] == 1
    assert s["synergy_points_change"] == round(70 / 8 - 2, 1)
    assert s["previous_week_average"] is None  # 8 days ago is outside "the week before" too


def test_rating_is_decided_by_code():
    assert rate({"interrogations": 0}) == (0, RATINGS[0])
    assert rate({"interrogations": 5, "pass_rate": 1.0, "average_score": 85, "pips_issued": 0})[0] == 5
    assert rate({"interrogations": 5, "pass_rate": 0.6, "average_score": 60, "pips_issued": 0})[0] == 3
    assert rate({"interrogations": 5, "pass_rate": 0.6, "average_score": 60, "pips_issued": 2})[0] == 2
    assert rate({"interrogations": 5, "pass_rate": 0.6, "average_score": 60, "pips_issued": 3})[0] == 1


def test_parse_review_validates_shape():
    assert parse_review("```json\n" + json.dumps(REVIEW) + "\n```")["headline"] == REVIEW["headline"]
    with pytest.raises(ValueError):
        parse_review(json.dumps({**REVIEW, "strengths": []}))
    with pytest.raises(ValueError):
        parse_review(json.dumps({k: v for k, v in REVIEW.items() if k != "closing"}))


def test_script_reads_rating_and_sections():
    lines = script(REVIEW, "Meets Expectations", "Monday 26 October 2026")
    assert "Your overall rating is: Meets Expectations." in lines
    assert lines.index("Key strengths.") < lines.index("Areas for development.") < lines.index(
        "Your objectives for next week.")
    assert lines[-1] == "Let's circle back."


async def test_review_read_aloud_with_llm(cfg, rng):
    store = Store(cfg.storage.database_path)
    record(store, True, 82)
    llm = FakeLLM(chat_reply=json.dumps(REVIEW))
    events = []
    writer = ReviewWriter(cfg, store, llm, rng, emit=lambda k, d: events.append((k, d)))
    audio = MockAudioPort()
    now = time.time()
    r = await writer.run(audio, start=now - 3600, end=now + 1, personality="linkedin", trigger="manual")

    assert r["author"] == "llm" and r["delivered"] and r["personality"] == "linkedin" and r["trigger"] == "manual"
    assert r["rating"] == rate(r["summary"])[0]
    ops = [op for op, _ in audio.log]
    assert ops[0] == "chime" and "hold_music" in ops and "hold_music_stopped" in ops
    assert "Align the alignment." in audio.spoken
    assert [k for k, _ in events] == ["review_started", "review_ready"]
    # The prompt carries the facts and the fixed rating.
    prompt = json.dumps(llm.chat_calls[0])
    assert r["rating_name"] in prompt and "We synergised." in prompt
    assert not store.has_scheduled_review_for(now - 3600)  # manual reviews don't satisfy Friday's


async def test_review_filed_silently_with_template_when_llm_down(cfg, rng):
    store = Store(cfg.storage.database_path)
    writer = ReviewWriter(cfg, store, FakeLLM(chat_reply=LLMError("the LLM is napping")), rng)
    now = time.time()
    r = await writer.run(None, start=now - 3600, end=now + 1)
    assert r["author"] == "template" and not r["delivered"] and r["rating"] == 0
    assert r["personality"] == "disappointed_vp"  # review.personality default
    assert r["review"]["objectives"] and store.has_scheduled_review_for(now - 3600)


def test_old_database_gains_barge_ins_column(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript("""CREATE TABLE interrogations (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
        trigger TEXT NOT NULL, personality TEXT NOT NULL, question TEXT NOT NULL, transcript TEXT,
        word_count INTEGER NOT NULL, jargon_ratio REAL NOT NULL, overall REAL NOT NULL, threshold REAL NOT NULL,
        passed INTEGER NOT NULL, evaluator TEXT NOT NULL, interrupted INTEGER NOT NULL DEFAULT 0,
        level_before INTEGER NOT NULL, level_after INTEGER NOT NULL, scores_json TEXT NOT NULL,
        jargon_json TEXT NOT NULL, feedback TEXT, points REAL NOT NULL DEFAULT 0);""")
    db.close()
    store = Store(path)
    record(store, True, 75, barge_ins=3)
    assert store.history()[0]["barge_ins"] == 3
