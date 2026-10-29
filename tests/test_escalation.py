import random

from middlemanagementd.config import EscalationConfig
from middlemanagementd.escalation import MAX_LEVEL, EscalationEngine, fallback_line


def engine(**kw) -> EscalationEngine:
    return EscalationEngine(EscalationConfig(**kw))


def test_fail_increments_and_caps():
    e, rng = engine(), random.Random(0)
    level = 0
    for expected in (1, 2, 3, 4, 5, 5):
        level = e.next_level(level, False, rng)
        assert level == expected


def test_pass_decays():
    e = engine(decay_on_pass=2)
    assert e.next_level(4, True, random.Random(0)) == 2
    assert e.next_level(1, True, random.Random(0)) == 0


def test_aggressiveness_two_skips_levels():
    assert engine(aggressiveness=2.0).next_level(1, False, random.Random(0)) == 3


def test_fractional_aggressiveness_is_probabilistic():
    e, rng = engine(aggressiveness=0.5), random.Random(42)
    steps = [e.next_level(0, False, rng) for _ in range(1000)]
    assert 400 < sum(steps) < 600


def test_stage_mapping():
    e = engine()
    assert e.stage(0, True) == "praise"
    assert e.stage(0, False) == 1
    assert e.stage(4, False) == 4 and e.is_monologue(4)


def test_interrupt_policy():
    e = engine(interrupt_after_seconds={3: 30.0, 4: 12.0})
    assert e.interrupt_after(2) is None
    assert e.interrupt_after(3) == 30.0
    assert e.interrupt_after(5) == 12.0
    assert engine(interrupt_enabled=False).interrupt_after(5) is None


def test_pip_issued_only_at_max_on_failure():
    e = engine()
    assert e.should_issue_pip(MAX_LEVEL, False, pip_open=False)
    assert not e.should_issue_pip(MAX_LEVEL, True, pip_open=False)
    assert not e.should_issue_pip(MAX_LEVEL, False, pip_open=True)
    assert not e.should_issue_pip(4, False, pip_open=False)


def test_fallback_lines_exist_for_every_stage():
    rng = random.Random(0)
    for stage in ("praise", 1, 2, 3, 4, 5, "pip_failed", "pip_completed"):
        assert fallback_line(stage, rng, ["synergy"])
    assert "synergy" in fallback_line(5, rng, ["synergy"])
