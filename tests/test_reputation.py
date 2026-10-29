from middlemanagementd.reputation import RANKS, Store, rank_for


def ev(passed: bool, overall: float = 80.0, jargon=("synergy",)) -> dict:
    return {"overall": overall, "threshold": 58, "passed": passed, "word_count": 40, "jargon_ratio": 0.3,
            "evaluator": "rules", "scores": {}, "detected_jargon": list(jargon)}


def record(store: Store, passed: bool, **kw) -> None:
    store.record(trigger="t", personality="p", question="q", transcript="x", evaluation=ev(passed, **kw),
                 interrupted=False, level_before=0, level_after=0, feedback="f", pip_issued=False)


def test_rank_thresholds():
    assert rank_for(-50) == (1, RANKS[0][1])
    assert rank_for(25) == (2, RANKS[1][1])
    assert rank_for(10_000) == (8, "Global Thought Leadership Evangelist")


def test_stats_and_streaks():
    s = Store(":memory:")
    record(s, True)
    record(s, True, jargon=("synergy", "north star"))
    record(s, False)
    record(s, True)
    st = s.stats()
    assert st["total_interrogations"] == 4 and st["successful_reports"] == 3 and st["failed_evaluations"] == 1
    assert st["consecutive_successes"] == 1 and st["best_streak"] == 2
    assert st["top_buzzwords"][0] == ("synergy", 4)
    assert st["synergy_points"] == 3 * 10 - 2


def test_state_persists_across_connections(tmp_path):
    path = tmp_path / "x.db"
    s = Store(path)
    s.level = 3
    s.set("killswitch", True)
    s.issue_pip(["synergy"])
    s.close()
    s2 = Store(path)
    assert s2.level == 3 and s2.get("killswitch") is True and s2.open_pip().terms == ["synergy"]


def test_delete_interrogation_rebuilds_derived_state():
    s = Store(":memory:")
    record(s, True)
    record(s, True, jargon=("synergy", "north star"))
    record(s, False)
    s.issue_pip(["synergy"])  # mirrors the session: PIP issued, then the interrogation recorded
    s.record(trigger="t", personality="p", question="q", transcript="x", evaluation=ev(False), interrupted=False,
             level_before=4, level_after=5, feedback="f", pip_issued=True, barge_ins=2)
    s.set("barge_ins", 2)
    ids = [r["id"] for r in s.history()]  # newest first

    assert s.delete_interrogation(ids[0])  # the PIP failure
    st = s.stats()
    assert st["total_interrogations"] == 3 and st["pips_issued"] == 0 and st["open_pip"] is None
    assert st["escalation_level"] == 0 and st["times_talked_over_management"] == 0
    assert st["synergy_points"] == 2 * 10 - 2

    assert s.delete_interrogation(ids[1])  # the plain failure: the streak is whole again
    st = s.stats()
    assert st["consecutive_successes"] == 2 and st["best_streak"] == 2

    assert s.delete_interrogation(ids[2])
    assert s.stats()["top_buzzwords"] == [("synergy", 1)]
    assert not s.delete_interrogation(ids[2])


def test_reset_keeps_switches():
    s = Store(":memory:")
    record(s, True)
    s.issue_pip(["synergy"])
    s.level = 4
    s.set("paused", True)
    s.reset()
    st = s.stats()
    assert st["total_interrogations"] == 0 and st["top_buzzwords"] == [] and st["pips_issued"] == 0
    assert st["escalation_level"] == 0 and st["best_streak"] == 0 and st["synergy_points"] == 0
    assert s.get("paused") is True
