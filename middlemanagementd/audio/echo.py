"""Echo-aware barge-in detection.

The client knows exactly what it is playing, so it can predict how loud the manager's own voice should
be at the microphone (an envelope model: gain x reference energy, delayed, plus a reverb tail). The user
talking over the manager shows up as mic energy well beyond that prediction. Gain and delay are
calibrated from the chime at the start of each session. With headphones the measured gain is ~0 and
this reduces to a plain voice detector.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

FRAME_SECONDS = 0.03


def envelope(samples: np.ndarray, rate: int, frame_seconds: float = FRAME_SECONDS) -> np.ndarray:
    n = max(1, int(rate * frame_seconds))
    usable = len(samples) // n * n
    if usable == 0:
        return np.zeros(0, dtype=np.float32)
    frames = samples[:usable].reshape(-1, n)
    return np.sqrt(np.mean(frames * frames, axis=1)).astype(np.float32)


@dataclass
class EchoModel:
    # Conservative defaults until calibrated: loud speakers, slow (HDMI) output, reverberant room.
    gain: float = 1.5
    delay_frames: int = 25
    tail_frames: int = 15
    calibrated: bool = False
    # Correlation between the fitted prediction and what the mic heard (1.0 = perfect).
    quality: float = 0.0

    def predicted(self, ref_env: np.ndarray, idx: int) -> float:
        """Expected echo energy at mic frame idx (relative to playback start)."""
        hi = idx - self.delay_frames + 2
        lo = idx - self.delay_frames - self.tail_frames
        if hi <= 0 or lo >= len(ref_env):
            return 0.0
        return self.gain * float(ref_env[max(0, lo) : min(len(ref_env), hi)].max())


def calibrate(ref_env: np.ndarray, mic_env: np.ndarray, noise_floor: float,
              max_delay_frames: int = 50) -> EchoModel | None:
    """Fit echo gain/delay from a known playback (the chime) and the mic's recording of it."""
    n = min(len(ref_env) + max_delay_frames, len(mic_env))
    if n < 10 or float(ref_env.max(initial=0)) <= 1e-4:
        return None
    y = np.maximum(mic_env[:n] - noise_floor, 0.0)
    if float(y.max()) < max(0.01, noise_floor):
        # The mic didn't hear the playback at all: headphones, or echo cancellation doing its job.
        return EchoModel(gain=0.0, delay_frames=0, calibrated=True, quality=1.0)
    best: tuple[float, int, float] | None = None
    for d in range(0, max_delay_frames):
        x = np.zeros(n, dtype=np.float32)
        seg = ref_env[: max(0, n - d)]
        x[d : d + len(seg)] = seg
        denom = float(x @ x)
        if denom <= 0:
            continue
        g = max(0.0, float(x @ y) / denom)
        err = float(np.mean((y - g * x) ** 2))
        if best is None or err < best[0]:
            best = (err, d, g)
    if best is None:
        return None
    _, d, g = best
    x = np.zeros(n, dtype=np.float32)
    seg = ref_env[: max(0, n - d)]
    x[d : d + len(seg)] = seg
    quality = float(np.corrcoef(x, y)[0, 1]) if float(x.std()) > 0 and float(y.std()) > 0 else 0.0
    # Pad the gain: reverb and nonlinearity make real echo spikier than the fit.
    return EchoModel(gain=min(20.0, g * 1.3), delay_frames=d, calibrated=True, quality=quality)


class BargeInDetector:
    """Feed mic frames during playback; reports True once the user has clearly been talking over it."""

    def __init__(self, ref_env: np.ndarray, model: EchoModel, noise_floor: float, *, ratio: float = 2.5,
                 min_seconds: float = 0.35, window_seconds: float = 0.6, abs_margin: float = 0.02) -> None:
        self.ref_env = ref_env
        self.model = model
        self.noise_floor = noise_floor
        self.ratio = ratio
        self.abs_margin = abs_margin
        self.window: deque[bool] = deque(maxlen=max(1, round(window_seconds / FRAME_SECONDS)))
        self.need = max(1, round(min_seconds / FRAME_SECONDS))
        self.excess_frames = 0
        self._ratios: list[float] = []

    @property
    def sustained_ratio(self) -> float:
        """Mic/threshold ratio exceeded by at least `need` frames: >= 1 means barge-in level. For tuning."""
        if len(self._ratios) < self.need:
            return 0.0
        return sorted(self._ratios, reverse=True)[self.need - 1]

    def threshold(self, idx: int) -> float:
        echo = self.model.predicted(self.ref_env, idx)
        return self.ratio * (echo + self.noise_floor) + self.abs_margin

    def push(self, idx: int, mic_rms: float) -> bool:
        thr = self.threshold(idx)
        self._ratios.append(mic_rms / thr)
        over = mic_rms > thr
        self.window.append(over)
        self.excess_frames += over
        return sum(self.window) >= self.need
