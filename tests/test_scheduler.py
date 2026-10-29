import random
from datetime import datetime

from middlemanagementd.config import SchedulerConfig
from middlemanagementd.scheduler import MIN_DELAY_SECONDS, Scheduler, in_window, parse_window


def sched(**kw) -> Scheduler:
    return Scheduler(SchedulerConfig(**kw), random.Random(7))


def test_delay_within_bounds():
    s = sched(min_interval_minutes=10, max_interval_minutes=20)
    delays = [s.next_delay() for _ in range(500)]
    assert min(delays) >= 600 and max(delays) <= 1200


def test_frequency_scales_delay():
    s = sched(min_interval_minutes=10, max_interval_minutes=20, frequency=2.0)
    assert all(300 <= s.next_delay() <= 600 for _ in range(200))


def test_minimum_delay_floor():
    s = sched(min_interval_minutes=0.01, max_interval_minutes=0.02, frequency=100)
    assert s.next_delay() == MIN_DELAY_SECONDS


def test_window_wraps_midnight():
    start, end = parse_window("22:00-08:00")
    assert in_window(datetime(2026, 1, 1, 23, 0).time(), start, end)
    assert in_window(datetime(2026, 1, 1, 3, 0).time(), start, end)
    assert not in_window(datetime(2026, 1, 1, 12, 0).time(), start, end)


def test_quiet_hours_and_weekdays():
    s = sched(quiet_hours=["12:00-13:00"], active_weekdays=[0, 1, 2, 3, 4])
    assert s.is_allowed(datetime(2026, 10, 28, 11, 0))      # Wednesday
    assert not s.is_allowed(datetime(2026, 10, 28, 12, 30))  # lunch
    assert not s.is_allowed(datetime(2026, 10, 31, 11, 0))  # Saturday


def test_fire_time_skips_quiet_hours():
    s = sched(min_interval_minutes=60, max_interval_minutes=60, quiet_hours=["21:00-09:00"], active_weekdays=[])
    fire = s.next_fire_time(datetime(2026, 10, 28, 20, 30))
    assert fire is not None and s.is_allowed(fire)
    assert fire >= datetime(2026, 10, 29, 9, 0)


def test_fire_time_skips_weekend():
    s = sched(min_interval_minutes=60, max_interval_minutes=60, quiet_hours=[], active_weekdays=[0, 1, 2, 3, 4])
    fire = s.next_fire_time(datetime(2026, 10, 30, 23, 30))  # Friday night
    assert fire is not None and fire.weekday() == 0


def test_never_allowed_returns_none():
    s = sched(quiet_hours=["00:00-23:59", "23:59-00:00"], active_weekdays=[])
    assert s.next_fire_time(datetime(2026, 10, 28, 10, 0)) is None
