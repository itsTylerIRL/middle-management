"""Procedurally generated sounds: the Synergy Chime and corporate hold music. No assets to ship."""

from __future__ import annotations

from functools import lru_cache

import numpy as np

SR = 48000


def _note(freq: float, dur: float, *, harmonics: tuple[float, ...] = (1.0, 0.5, 0.25), decay: float = 6.0,
          attack: float = 0.004) -> np.ndarray:
    t = np.arange(int(SR * dur)) / SR
    wave = sum(a * np.sin(2 * np.pi * freq * (i + 1) * t) for i, a in enumerate(harmonics))
    env = np.minimum(t / attack, 1.0) * np.exp(-decay * t)
    return (wave * env).astype(np.float32)


def _fm_bell(freq: float, dur: float, index: float = 2.5, ratio: float = 3.5, decay: float = 5.0) -> np.ndarray:
    t = np.arange(int(SR * dur)) / SR
    mod = index * np.exp(-3 * t) * np.sin(2 * np.pi * freq * ratio * t)
    env = np.minimum(t / 0.002, 1.0) * np.exp(-decay * t)
    return (np.sin(2 * np.pi * freq * t + mod) * env).astype(np.float32)


def _place(buf: np.ndarray, sound: np.ndarray, at: float, gain: float = 1.0) -> None:
    i = int(at * SR)
    end = min(len(buf), i + len(sound))
    buf[i:end] += gain * sound[: end - i]


def _normalize(x: np.ndarray, peak: float = 0.95) -> np.ndarray:
    m = float(np.max(np.abs(x))) or 1.0
    return (x / m * peak).astype(np.float32)


@lru_cache(maxsize=1)
def synergy_chime() -> np.ndarray:
    """An aggressively upbeat four-note bell arpeggio, a buzzy double-knock, then all of it again, higher."""
    buf = np.zeros(int(SR * 2.4), dtype=np.float32)
    arpeggio = (659.25, 830.61, 987.77, 1318.51)  # E5 G#5 B5 E6: relentlessly major
    for rep, shift in enumerate((1.0, 1.122)):
        base = rep * 1.05
        for k, f in enumerate(arpeggio):
            _place(buf, _fm_bell(f * shift, 0.6), base + k * 0.085, 0.8)
        # "ba-DUM" marimba knock with a square-ish edge: the obnoxious part.
        for k, f in enumerate((392.0 * shift, 784.0 * shift)):
            knock = _note(f, 0.25, harmonics=(1.0, 0.0, 0.33, 0.0, 0.2, 0.0, 0.14), decay=14.0)
            _place(buf, knock, base + 0.42 + k * 0.13, 0.9)
    return _normalize(buf)


@lru_cache(maxsize=1)
def hold_music_loop() -> np.ndarray:
    """Eight seconds of soothing elevator jazz: Cmaj7 - Am7 - Dm7 - G7, electric-piano-ish with tremolo."""
    chords = (
        (261.63, 329.63, 392.00, 493.88),
        (220.00, 261.63, 329.63, 392.00),
        (293.66, 349.23, 440.00, 523.25),
        (196.00, 246.94, 293.66, 349.23),
    )
    melody = (659.25, 587.33, 523.25, 587.33, 523.25, 440.00, 493.88, 587.33)
    bar = 2.0
    buf = np.zeros(int(SR * bar * len(chords)), dtype=np.float32)
    for i, chord in enumerate(chords):
        for f in chord:
            _place(buf, _note(f, bar * 1.1, harmonics=(1.0, 0.3, 0.08), decay=1.2, attack=0.02), i * bar, 0.25)
        _place(buf, _note(chord[0] / 2, bar, harmonics=(1.0, 0.2), decay=1.5), i * bar, 0.5)
    for j, f in enumerate(melody):
        _place(buf, _note(f, 0.9, harmonics=(1.0, 0.15), decay=3.0, attack=0.01), j * bar / 2 + 0.25, 0.35)
    t = np.arange(len(buf)) / SR
    buf *= 0.85 + 0.15 * np.sin(2 * np.pi * 5.0 * t)
    # Short crossfade so the loop seam doesn't click.
    fade = int(0.05 * SR)
    buf[:fade] *= np.linspace(0, 1, fade)
    buf[-fade:] *= np.linspace(1, 0, fade)
    return _normalize(buf, 0.8)
