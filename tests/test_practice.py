import json
import random
from datetime import date

from middlemanagementd.audio.port import MockAudioPort
from middlemanagementd.evaluator import JargonEvaluator
from middlemanagementd.llm import LLMError
from middlemanagementd.practice import coach, pick_word, rules_coaching, word_of_day
from middlemanagementd.reputation import Store
from middlemanagementd.scoring import METRICS, JargonDictionary
from middlemanagementd.session import InterrogationRunner

from .conftest import JARGON, PLAIN, FakeLLM

DICT = JargonDictionary.from_mapping({"buzzwords": ["synergy", "paradigm shift"], "strategy": ["north star"],
                                      "stakeholders": ["leadership"]})


def test_pick_word_favours_unused_terms():
    usage = {"synergy": 50, "paradigm shift": 50, "leadership": 50}
    picks = [pick_word(DICT, usage, random.Random(i))[0] for i in range(200)]
    assert picks.count("north star") > 190


def test_word_of_day_is_stable_within_a_day():
    s = Store(":memory:")
    first = word_of_day(s, DICT, date(2026, 10, 29))
    s.set("word_of_day", {**first, "definition": "kept"})
    assert word_of_day(s, DICT, date(2026, 10, 29))["definition"] == "kept"
    tomorrow = word_of_day(s, DICT, date(2026, 10, 30))
    assert tomorrow["date"] == "2026-10-30" and tomorrow["term"] != first["term"] and not tomorrow["definition"]


async def test_word_of_day_earns_bonus_once(cfg, rng):
    cfg.practice.word_of_day = True
    cfg.practice.coaching = False
    store = Store(cfg.storage.database_path)
    events = []
    runner = InterrogationRunner(cfg, store, None, JargonEvaluator(cfg.evaluation, None), rng,
                                 emit=lambda k, d: events.append((k, d)))
    term = word_of_day(store, runner.evaluator.dictionary)["term"]
    answer = f"{JARGON} Also, {term}."

    audio = MockAudioPort(responses=[answer])
    out = await runner.run(audio)
    assert out.word_bonus == cfg.practice.word_of_day_points
    assert ("word_of_day", {"term": term, "points": out.word_bonus}) in events
    assert any(term in line for line in audio.spoken)
    first_points = store.history()[0]["points"]

    out = await runner.run(MockAudioPort(responses=[answer]))
    assert out.word_bonus == 0  # once a day
    assert store.history()[0]["points"] == first_points - cfg.practice.word_of_day_points

    # Deleting the interrogation that earned it lets you earn it again.
    store.delete_interrogation(store.history()[1]["id"])
    assert store.get("word_of_day")["claimed_by"] is None


def test_rules_coaching_suggests_new_terms_for_weak_metrics():
    ev = {"scores": {m: 90.0 for m in METRICS} | {"strategic_alignment": 0.0, "stakeholder_awareness": 5.0},
          "detected_jargon": ["leadership"]}
    c = rules_coaching(ev, DICT, random.Random(1))
    assert c["source"] == "rules" and c["rewrite"] is None
    assert "north star" in c["tips"][0] and "leadership" not in c["tips"][1]


async def test_coaching_from_llm_and_fallback():
    ev = {"scores": {m: 50.0 for m in METRICS}, "detected_jargon": [], "critique": "meh"}
    reply = {"rewrite": "We are leveraging synergy toward our north star.",
             "highlights": [{"phrase": "north star", "why": "sounds visionary"}, {"no": "phrase"}],
             "tip": "Name the north star."}
    llm = FakeLLM(chat_reply=json.dumps(reply))
    c = await coach(llm, 200, question="q", transcript="I fixed it", evaluation=ev, dictionary=DICT,
                    rng=random.Random(1), word="paradigm shift")
    assert c["source"] == "llm" and c["rewrite"] == reply["rewrite"] and c["tips"] == ["Name the north star."]
    assert c["highlights"] == [{"phrase": "north star", "why": "sounds visionary"}]
    assert c["rewrite_jargon_ratio"] > 0
    assert "paradigm shift" in llm.chat_calls[0][0]["content"]

    c = await coach(FakeLLM(chat_reply=LLMError("down")), 200, question="q", transcript="x", evaluation=ev,
                    dictionary=DICT, rng=random.Random(1))
    assert c["source"] == "rules" and c["tips"]


async def test_session_stores_coaching(cfg, rng):
    store = Store(cfg.storage.database_path)
    events = []
    runner = InterrogationRunner(cfg, store, None, JargonEvaluator(cfg.evaluation, None), rng,
                                 emit=lambda k, d: events.append((k, d)))
    out = await runner.run(MockAudioPort(responses=[PLAIN]))
    kinds = [k for k, _ in events]
    assert kinds.index("coaching") < kinds.index("session_finished")
    assert out.coaching["tips"] and store.history()[0]["coaching"] == out.coaching


def test_pip_issued_backfilled_on_old_databases(tmp_path):
    path = tmp_path / "old.db"
    s = Store(path)
    ev = {"overall": 10.0, "threshold": 58, "passed": False, "word_count": 4, "jargon_ratio": 0, "evaluator": "rules",
          "scores": {}, "detected_jargon": []}
    for pip in (True, False):
        s.record(trigger="t", personality="p", question="q", transcript="x", evaluation=ev, interrupted=False,
                 level_before=5, level_after=5, feedback="f", pip_issued=pip)
    s.db.execute("ALTER TABLE interrogations DROP COLUMN pip_issued")  # as the 0.1.0 schema had it
    s.db.commit()
    s.close()
    assert [r["pip_issued"] for r in Store(path).history()] == [False, True]
