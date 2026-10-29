"""Interruption timing: randomized intervals, quiet hours and active weekdays. Pure logic, no I/O."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from .config import SchedulerConfig

# Never schedule more often than this, however high `frequency` is set.
MIN_DELAY_SECONDS = 60.0


def parse_window(spec: str) -> tuple[time, time]:
    start_s, end_s = (part.strip() for part in spec.split("-", 1))
    return time.fromisoformat(start_s), time.fromisoformat(end_s)


def in_window(t: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= t < end
    # Window wraps midnight, e.g. 22:00-08:00.
    return t >= start or t < end


@dataclass
class Scheduler:
    cfg: SchedulerConfig
    rng: random.Random

    def __post_init__(self) -> None:
        self.windows = [parse_window(w) for w in self.cfg.quiet_hours]

    def is_allowed(self, dt: datetime) -> bool:
        if self.cfg.active_weekdays and dt.weekday() not in self.cfg.active_weekdays:
            return False
        t = dt.time()
        return not any(in_window(t, s, e) for s, e in self.windows)

    def next_delay(self) -> float:
        lo = self.cfg.min_interval_minutes * 60 / self.cfg.frequency
        hi = self.cfg.max_interval_minutes * 60 / self.cfg.frequency
        return max(MIN_DELAY_SECONDS, self.rng.uniform(lo, hi))

    def next_allowed(self, dt: datetime) -> datetime | None:
        """First minute at or after dt when interrogations are allowed (None if never within 8 days)."""
        probe = dt
        for _ in range(8 * 24 * 60):
            if self.is_allowed(probe):
                return probe
            probe = (probe + timedelta(minutes=1)).replace(second=0, microsecond=0)
        return None

    def next_fire_time(self, now: datetime) -> datetime | None:
        candidate = now + timedelta(seconds=self.next_delay())
        allowed = self.next_allowed(candidate)
        if allowed is None:
            return None
        if allowed != candidate:
            # Don't ambush the user the instant quiet hours end; let them get a coffee first.
            jitter = self.rng.uniform(0, self.cfg.min_interval_minutes * 60 / self.cfg.frequency)
            allowed = self.next_allowed(allowed + timedelta(seconds=jitter)) or allowed
        return allowed
