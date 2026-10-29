"""Energy-based voice activity detection with an adaptive noise floor. Simple, dependency-free, adequate."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class VADState:
    frame_seconds: float
    energy_threshold: float = 0.010
    noise_multiplier: float = 3.0
    # Never require more than this RMS, however noisy calibration looked.
    max_threshold: float = 0.15
    start_frames: int = 4  # consecutive voiced frames needed to declare speech
    noise_floor: float = 0.0
    calibration_frames: int = 10
    _seen: int = 0
    _calibration: list[float] = field(default_factory=list)
    _run: int = 0
    in_speech: bool = False
    speech_frames: int = 0
    silence_frames: int = 0
    frames: list[np.ndarray] = field(default_factory=list)

    @property
    def threshold(self) -> float:
        return max(self.energy_threshold, min(self.max_threshold, self.noise_floor * self.noise_multiplier))

    @property
    def speech_seconds(self) -> float:
        return self.speech_frames * self.frame_seconds

    @property
    def silence_seconds(self) -> float:
        return self.silence_frames * self.frame_seconds

    def push(self, frame: np.ndarray) -> bool:
        """Feed a frame; returns whether it was voiced."""
        rms = float(np.sqrt(np.mean(frame * frame))) if len(frame) else 0.0
        self._seen += 1
        if self._seen <= self.calibration_frames:
            # Median of the first frames estimates room noise, ignoring clicks and keyboard spikes.
            self._calibration.append(rms)
            self.noise_floor = float(np.median(self._calibration))
            return False
        voiced = rms >= self.threshold
        if not self.in_speech:
            # Keep a short pre-roll so the first syllable isn't clipped.
            self.frames.append(frame)
            self.frames = self.frames[-(self.start_frames + 8):]
            self._run = self._run + 1 if voiced else 0
            if self._run >= self.start_frames:
                self.in_speech = True
                self.speech_frames = self._run
            elif not voiced:
                # Slowly track drifting background noise while waiting.
                self.noise_floor = 0.97 * self.noise_floor + 0.03 * rms
            return voiced
        self.frames.append(frame)
        if voiced:
            self.speech_frames += 1
            self.silence_frames = 0
        else:
            self.silence_frames += 1
        return voiced

    def audio(self) -> np.ndarray:
        return np.concatenate(self.frames) if self.frames else np.zeros(0, dtype=np.float32)
